"""Hands on the PC (Windows): open apps, sites and folders; find, focus, minimise, maximise and close windows.

The brain decides WHAT to do (assistant.py); this module only does it, safely:
- closing is always graceful (WM_CLOSE, so apps can still ask to save); nothing is force-killed
- Dork itself, Claude and Windows' own shell/system windows are never touched
- names are matched fuzzily against what's actually installed / open"""

import ctypes
try:
    import ctypes.wintypes as wt  # Windows types (not needed on Linux / macOS)
except (ImportError, ValueError):
    wt = None
import difflib
import json
import logging
import os
import re
import subprocess
import sys
import time
import webbrowser
from pathlib import Path

log = logging.getLogger("dork.desktop")
WINDOWS = sys.platform == "win32"

if WINDOWS:
    user32 = ctypes.windll.user32
    kernel32 = ctypes.windll.kernel32

# Processes whose windows Dork never closes / minimises.
PROTECTED = {"dork.exe", "claude.exe", "python.exe", "pythonw.exe", "ollama.exe", "csrss.exe", "winlogon.exe",
             "dwm.exe", "lsass.exe", "services.exe", "smss.exe", "taskmgr.exe", "searchhost.exe",
             "startmenuexperiencehost.exe", "shellexperiencehost.exe", "textinputhost.exe", "lockapp.exe",
             "systemsettingsbroker.exe"}
SHELL_CLASSES = {"Progman", "WorkerW", "Shell_TrayWnd", "Shell_SecondaryTrayWnd", "NotifyIconOverflowWindow"}
DORK_TITLES = re.compile(r"^Dork( Assistant)?$")

# Windows known folders (they can be moved, e.g. Downloads on another drive or in OneDrive)
KNOWN_FOLDERS = {
    "downloads": "{374DE290-123F-4565-9164-39C4925E467B}", "documents": "{FDD39AD0-238F-46AF-ADB4-6C85480369C7}",
    "desktop": "{B4BFCC3A-DB2C-424C-B029-7FE99A87C641}", "pictures": "{33E28130-4E1E-4676-835A-98395C3BC3BB}",
    "photos": "{33E28130-4E1E-4676-835A-98395C3BC3BB}", "music": "{4BD8D571-6D19-48D3-BE97-422220080E43}",
    "videos": "{18989B1D-99B5-455B-841C-AB7C74E4DDFC}", "home": "",
}


def known_folder(key: str) -> Path:
    guid = KNOWN_FOLDERS[key]
    if not guid or not WINDOWS:
        return Path.home() / (key.capitalize() if guid else "")

    class GUID(ctypes.Structure):
        _fields_ = [("a", ctypes.c_ulong), ("b", ctypes.c_ushort), ("c", ctypes.c_ushort), ("d", ctypes.c_ubyte * 8)]

    g = GUID()
    ctypes.windll.ole32.CLSIDFromString(ctypes.c_wchar_p(guid), ctypes.byref(g))
    out = ctypes.c_wchar_p()
    if ctypes.windll.shell32.SHGetKnownFolderPath(ctypes.byref(g), 0, None, ctypes.byref(out)) == 0:
        path = Path(out.value)
        ctypes.windll.ole32.CoTaskMemFree(out)
        return path
    return Path.home() / key.capitalize()

# ---------------------------------------------------------------- installed apps

_apps: list[dict] = []
_apps_at = 0.0


def apps() -> list[dict]:
    """Start-menu apps: [{name, id}] (Win32 + Store apps), cached for 10 minutes."""
    global _apps, _apps_at
    if _apps and time.time() - _apps_at < 600:
        return _apps
    if not WINDOWS:
        return []
    try:
        out = subprocess.run(
            ["powershell", "-NoProfile", "-Command", "Get-StartApps | ConvertTo-Json -Compress"],
            capture_output=True, text=True, timeout=20, creationflags=0x08000000)
        rows = json.loads(out.stdout or "[]")
        _apps = [{"name": r["Name"], "id": r["AppID"]} for r in (rows if isinstance(rows, list) else [rows])]
        _apps_at = time.time()
    except Exception as e:  # noqa: BLE001
        log.warning("listing apps failed: %s", e)
    return _apps


def _norm(s: str) -> str:
    return re.sub(r"[^a-z0-9 ]+", " ", s.lower()).strip()


ALIASES = {"vs code": "visual studio code", "vscode": "visual studio code", "code": "visual studio code",
           "chrome": "google chrome", "word": "word", "excel": "excel", "files": "file explorer",
           "explorer": "file explorer", "terminal": "terminal", "cmd": "command prompt", "notepad": "notepad",
           "calc": "calculator", "settings": "settings", "store": "microsoft store"}


def _score(query: str, name: str) -> float:
    q, n = _norm(query), _norm(name)
    if not q or not n:
        return 0.0
    if q == n:
        return 1.0
    if n.startswith(q + " ") or n.endswith(" " + q) or f" {q} " in f" {n} ":
        return 0.92 - 0.01 * max(0, len(n.split()) - len(q.split()))
    if q in n:
        return 0.8
    return difflib.SequenceMatcher(None, q, n).ratio() * 0.85


def find_app(query: str) -> dict | None:
    q = ALIASES.get(_norm(query), query)
    best = max(apps(), key=lambda a: _score(q, a["name"]), default=None)
    if best and _score(q, best["name"]) >= 0.62:
        return best
    return None


# ---------------------------------------------------------------- windows

def _proc_name(hwnd) -> str:
    pid = wt.DWORD()
    user32.GetWindowThreadProcessId(hwnd, ctypes.byref(pid))
    h = kernel32.OpenProcess(0x1000, False, pid.value)  # PROCESS_QUERY_LIMITED_INFORMATION
    if not h:
        return ""
    try:
        buf = ctypes.create_unicode_buffer(520)
        size = wt.DWORD(520)
        if kernel32.QueryFullProcessImageNameW(h, 0, buf, ctypes.byref(size)):
            return os.path.basename(buf.value).lower()
    finally:
        kernel32.CloseHandle(h)
    return ""


def _title(hwnd) -> str:
    n = user32.GetWindowTextLengthW(hwnd)
    buf = ctypes.create_unicode_buffer(n + 1)
    user32.GetWindowTextW(hwnd, buf, n + 1)
    return buf.value


def _class(hwnd) -> str:
    buf = ctypes.create_unicode_buffer(256)
    user32.GetClassNameW(hwnd, buf, 256)
    return buf.value


def _is_app_window(hwnd) -> bool:
    if not user32.IsWindowVisible(hwnd) or user32.GetWindow(hwnd, 4):  # GW_OWNER: owned popups aren't apps
        return False
    ex = user32.GetWindowLongW(hwnd, -20)  # GWL_EXSTYLE
    if ex & 0x80:  # WS_EX_TOOLWINDOW
        return False
    cloaked = ctypes.c_int(0)
    ctypes.windll.dwmapi.DwmGetWindowAttribute(hwnd, 14, ctypes.byref(cloaked), 4)  # DWMWA_CLOAKED
    return not cloaked.value and bool(_title(hwnd).strip())


def windows() -> list[dict]:
    """Open app windows, front-most first: [{hwnd, title, process, minimized}]."""
    if not WINDOWS:
        return []
    out = []

    @ctypes.WINFUNCTYPE(ctypes.c_bool, wt.HWND, wt.LPARAM)
    def cb(hwnd, _):
        if _is_app_window(hwnd) and _class(hwnd) not in SHELL_CLASSES:
            out.append({"hwnd": hwnd, "title": _title(hwnd), "process": _proc_name(hwnd),
                        "minimized": bool(user32.IsIconic(hwnd))})
        return True

    user32.EnumWindows(cb, 0)
    return out


def _is_dork(w: dict) -> bool:
    return w["process"] == "dork.exe" or bool(DORK_TITLES.match(w["title"])) or \
        (w["process"] == "electron.exe" and "Dork" in w["title"])


def protected(w: dict) -> bool:
    return _is_dork(w) or w["process"] in PROTECTED


_last_foreground: dict | None = None


def track_foreground() -> None:
    """Remember the last app window Hari used (not Dork), so "close this" means that window."""
    global _last_foreground
    if not WINDOWS:
        return
    hwnd = user32.GetForegroundWindow()
    if not hwnd or not _is_app_window(hwnd):
        return
    w = {"hwnd": hwnd, "title": _title(hwnd), "process": _proc_name(hwnd), "minimized": False}
    if not _is_dork(w) and _class(hwnd) not in SHELL_CLASSES:
        _last_foreground = w


def current() -> dict | None:
    """The window "this" refers to: the last non-Dork window in front."""
    if _last_foreground and user32.IsWindow(_last_foreground["hwnd"]):
        return _last_foreground
    return next((w for w in windows() if not protected(w)), None)


def _window_score(query: str, w: dict) -> float:
    q = ALIASES.get(_norm(query), query)
    proc = w["process"].removesuffix(".exe")
    title_parts = [p for p in re.split(r" [-–—|] ", w["title"]) if p]
    return max([_score(q, proc), _score(query, proc)] + [_score(q, p) for p in title_parts] + [_score(q, w["title"]) * 0.95])


def find_windows(query: str) -> list[dict]:
    """Windows matching a name ("chrome", "spotify", "my downloads folder"), best first."""
    if not query or _norm(query) in ("this", "that", "it", "current", "this window", "that window"):
        w = current()
        return [w] if w else []
    scored = [(w, _window_score(query, w)) for w in windows() if not _is_dork(w)]
    scored = [x for x in scored if x[1] >= 0.62]
    scored.sort(key=lambda x: x[1], reverse=True)
    if not scored:
        return []
    top = scored[0][1]
    return [w for w, s in scored if s >= top - 0.05]


def _focus(hwnd) -> None:
    if user32.IsIconic(hwnd):
        user32.ShowWindow(hwnd, 9)  # SW_RESTORE
    # Windows only lets the foreground app hand over focus; a synthetic Alt press unlocks it.
    user32.keybd_event(0x12, 0, 0, 0)
    user32.keybd_event(0x12, 0, 2, 0)
    user32.SetForegroundWindow(hwnd)


# ---------------------------------------------------------------- actions (each returns {ok, said})

def open_app(name: str) -> dict:
    app = find_app(name)
    if not app:
        return {"ok": False, "said": f"I couldn't find an app called {name} on this PC."}
    if WINDOWS:
        subprocess.Popen(["explorer.exe", f"shell:AppsFolder\\{app['id']}"], creationflags=0x08000000)
    else:
        from . import desktop_posix
        desktop_posix.launch(app)
    return {"ok": True, "said": f"Opening {app['name']}.", "target": app["name"]}


def open_url(url: str) -> dict:
    url = url.strip()
    if not re.match(r"^https?://", url):
        url = "https://" + url.lstrip("/")
    webbrowser.open(url)
    host = re.sub(r"^https?://(www\.)?", "", url).split("/")[0]
    return {"ok": True, "said": f"Opening {host}.", "target": url}


def open_folder(name: str) -> dict:
    key = re.sub(r"^(my|the) ", "", _norm(name).replace(" folder", "").strip())
    if key in KNOWN_FOLDERS:
        path = known_folder(key)
    else:
        path = Path(os.path.expandvars(os.path.expanduser(name)))
    if not path.exists() and _norm(path.name) in KNOWN_FOLDERS:  # "C:\Users\x\Downloads" → the real one
        path = known_folder(_norm(path.name))
    if not path.exists():
        return {"ok": False, "said": f"I can't find the folder {name}."}
    from . import oskit
    oskit.open_path(str(path))  # Explorer / Finder / the Linux file manager
    return {"ok": True, "said": f"Opening {path.name or 'your home folder'}.", "target": str(path)}


def window_action(action: str, target: str, confirm_many: bool = False) -> dict:
    """action: close | minimize | maximize | focus | restore."""
    found = find_windows(target)
    if not found:
        what = "that window" if _norm(target) in ("", "this", "that", "it") else target
        return {"ok": False, "said": f"I don't see {what} open."}
    found = [w for w in found if not protected(w)] if action in ("close", "minimize") else found
    if not found:
        return {"ok": False, "said": "That one's off limits. I don't close myself or system windows."}
    if action == "close" and len(found) > 1 and not confirm_many:
        return {"ok": False, "confirm": True, "count": len(found),
                "said": f"There are {len(found)} {target} windows open. Close all of them?"}
    for w in found:
        h = w["hwnd"]
        if action == "close":
            user32.PostMessageW(h, 0x0010, 0, 0)  # WM_CLOSE — graceful, the app can still ask to save
        elif action == "minimize":
            user32.ShowWindow(h, 6)
        elif action == "maximize":
            user32.ShowWindow(h, 3)
            _focus(h)
        elif action in ("focus", "restore"):
            _focus(h)
    name = _friendly(found[0])
    verb = {"close": "Closed", "minimize": "Minimised", "maximize": "Maximised", "focus": "Switched to",
            "restore": "Brought back"}[action]
    many = f" ({len(found)} windows)" if len(found) > 1 else ""
    return {"ok": True, "said": f"{verb} {name}{many}.", "target": name}


def _friendly(w: dict) -> str:
    parts = [p for p in re.split(r" [-–—|] ", w["title"]) if p]
    proc = w["process"].removesuffix(".exe")
    if proc in ("applicationframehost", "explorer"):  # Store apps / folders: the title is the name
        return parts[-1] if parts else w["title"]
    app = next((a["name"] for a in apps() if _score(proc, a["name"]) > 0.85), None)
    return app or (parts[-1] if len(parts) > 1 else proc.capitalize() or w["title"])


MEDIA = {"play_pause": 0xB3, "next": 0xB0, "previous": 0xB1, "volume_up": 0xAF, "volume_down": 0xAE, "mute": 0xAD}


def media(key: str, times: int = 1) -> dict:
    key = {"pause": "play_pause", "play": "play_pause", "resume": "play_pause", "stop": "play_pause",
           "skip": "next", "back": "previous", "louder": "volume_up", "quieter": "volume_down",
           "unmute": "mute"}.get(key.strip().lower(), key.strip().lower())
    vk = MEDIA.get(key)
    if not vk:
        return {"ok": False, "said": "I can't do that one."}
    for _ in range(max(1, min(times, 25))):
        user32.keybd_event(vk, 0, 0, 0)
        user32.keybd_event(vk, 0, 2, 0)
    said = {"play_pause": "Done.", "next": "Next track.", "previous": "Previous track.", "volume_up": "Louder.",
            "volume_down": "Quieter.", "mute": "Toggled mute."}[key]
    return {"ok": True, "said": said}


def system_info() -> dict:
    import psutil

    info = {"cpu_percent": psutil.cpu_percent(interval=0.3), "ram_percent": psutil.virtual_memory().percent}
    b = psutil.sensors_battery()
    if b:
        info.update(battery_percent=round(b.percent), plugged_in=bool(b.power_plugged))
    info["open_windows"] = [_friendly(w) for w in windows() if not _is_dork(w)][:12]
    return info


def describe() -> dict:
    """What's on screen right now, for the brain: the window in front and the other open windows."""
    cur = current()
    return {"in_front": f"{_friendly(cur)} — {cur['title']}" if cur else None,
            "open": [f"{_friendly(w)} — {w['title'][:60]}" for w in windows() if not _is_dork(w)][:10]}


# ---------------------------------------------------------------- Linux / macOS: same functions, other backend
if not WINDOWS:
    from . import desktop_posix as _posix

    apps = _posix.apps
    windows = _posix.windows
    track_foreground = _posix.track_foreground
    current = _posix.current
    window_action = _posix.window_action
    media = _posix.media
    known_folder = _posix.known_folder
    PROTECTED |= {p.lower() for p in _posix.POSIX_PROTECTED}
