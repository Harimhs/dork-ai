"""Hands on the PC for Linux and macOS: the same functions dork/desktop.py offers on Windows.

Linux: apps from .desktop entries (system, user, Flatpak, Snap); windows via wmctrl / xdotool (X11 and XWayland;
pure Wayland compositors don't let other programs list or move windows); media via playerctl, volume via pactl.
macOS: apps from /Applications; windows (per app) via AppleScript; volume via AppleScript.
Missing helper tools never crash Dork: the action just says it isn't available (setup installs them on Linux)."""

import configparser
import logging
import os
import re
import shlex
import shutil
import subprocess
import time
from pathlib import Path

from . import oskit

log = logging.getLogger("dork.desktop")

POSIX_PROTECTED = {"dork", "dork-desktop", "python", "python3", "ollama", "llama-server", "claude", "gnome-shell",
                   "plasmashell", "kwin_x11", "kwin_wayland", "xfwm4", "xfce4-panel", "Xorg", "Xwayland", "Finder",
                   "Dock", "SystemUIServer", "loginwindow", "WindowServer"}


def _run(args: list[str], timeout: float = 5) -> str:
    try:
        return subprocess.run(args, capture_output=True, text=True, timeout=timeout).stdout
    except (OSError, subprocess.SubprocessError):
        return ""


def _osa(script: str) -> str:
    return _run(["osascript", "-e", script]).strip()


# ---------------------------------------------------------------- installed apps

_apps: list[dict] = []
_apps_at = 0.0
LINUX_APP_DIRS = ["/usr/share/applications", "/usr/local/share/applications", "~/.local/share/applications",
                  "/var/lib/flatpak/exports/share/applications", "~/.local/share/flatpak/exports/share/applications",
                  "/var/lib/snapd/desktop/applications"]


def apps() -> list[dict]:
    """[{name, id}] — id is the .desktop file (Linux) or the .app bundle path (macOS)."""
    global _apps, _apps_at
    if _apps and time.time() - _apps_at < 600:
        return _apps
    found: dict[str, dict] = {}
    if oskit.MAC:
        for base in ("/Applications", "/System/Applications", str(Path.home() / "Applications")):
            for app in Path(base).glob("*.app"):
                found.setdefault(app.stem.lower(), {"name": app.stem, "id": str(app)})
    else:
        for base in LINUX_APP_DIRS:
            for f in Path(os.path.expanduser(base)).glob("*.desktop"):
                cp = configparser.ConfigParser(interpolation=None, strict=False)
                try:
                    cp.read(f, encoding="utf-8")
                    e = cp["Desktop Entry"]
                except (KeyError, configparser.Error, UnicodeDecodeError):
                    continue
                if e.get("NoDisplay", "false").lower() == "true" or e.get("Type", "Application") != "Application":
                    continue
                if name := e.get("Name"):
                    found.setdefault(name.lower(), {"name": name, "id": str(f), "exec": e.get("Exec", "")})
    _apps, _apps_at = list(found.values()), time.time()
    return _apps


def launch(app: dict) -> None:
    if oskit.MAC:
        subprocess.Popen(["open", "-a", app["id"]], **oskit.detached())
        return
    desktop_id = Path(app["id"]).name
    if shutil.which("gtk-launch"):
        subprocess.Popen(["gtk-launch", desktop_id], **oskit.detached())
        return
    cmd = re.sub(r"%[fFuUdDnNickvm]", "", app.get("exec", "")).strip()  # .desktop field codes
    if cmd:
        subprocess.Popen(shlex.split(cmd), **oskit.detached())


# ---------------------------------------------------------------- windows

def windows() -> list[dict]:
    """Open app windows: [{hwnd, title, process, minimized}] (hwnd = X11 window id / macOS app name)."""
    out = []
    if oskit.MAC:
        names = _osa('tell application "System Events" to get name of every process whose visible is true')
        for n in [x.strip() for x in names.split(",") if x.strip()]:
            out.append({"hwnd": n, "title": n, "process": n.lower(), "minimized": False})
        return out
    if not shutil.which("wmctrl"):
        return out
    for line in _run(["wmctrl", "-lp"]).splitlines():
        parts = line.split(None, 4)
        if len(parts) < 5 or parts[1] == "-1":  # -1 = sticky panels / desktop
            continue
        wid, _, pid, _host_title = parts[0], parts[1], parts[2], parts[4]
        title = _host_title.split(None, 1)[1] if " " in _host_title else _host_title
        try:
            proc = Path(f"/proc/{pid}/comm").read_text().strip()
        except OSError:
            proc = ""
        out.append({"hwnd": wid, "title": title, "process": proc.lower(), "minimized": False})
    return out


_last_foreground: dict | None = None


def _active() -> dict | None:
    if oskit.MAC:
        n = _osa('tell application "System Events" to get name of first process whose frontmost is true')
        return {"hwnd": n, "title": n, "process": n, "minimized": False} if n else None
    if not shutil.which("xdotool"):
        return None
    wid = _run(["xdotool", "getactivewindow"]).strip()
    if not wid.isdigit():
        return None
    hexid = f"0x{int(wid):08x}"
    return next((w for w in windows() if int(w["hwnd"], 16) == int(wid)), {"hwnd": hexid, "title": "", "process": "", "minimized": False})


def track_foreground() -> None:
    global _last_foreground
    from . import desktop

    w = _active()
    if w and w["title"] and not desktop._is_dork(w):
        _last_foreground = w


def current() -> dict | None:
    from . import desktop

    if _last_foreground and any(w["hwnd"] == _last_foreground["hwnd"] for w in windows()):
        return _last_foreground
    return next((w for w in windows() if not desktop.protected(w)), None)


def _apply(action: str, w: dict) -> bool:
    h = w["hwnd"]
    if oskit.MAC:
        app = h.replace('"', "")
        script = {
            "close": f'tell application "{app}" to close front window',
            "minimize": f'tell application "System Events" to set value of attribute "AXMinimized" of window 1 of process "{app}" to true',
            "maximize": f'tell application "{app}" to activate',
            "focus": f'tell application "{app}" to activate',
            "restore": f'tell application "{app}" to activate',
        }[action]
        _osa(script)
        return True
    if not shutil.which("wmctrl"):
        return False
    if action == "close":
        _run(["wmctrl", "-ic", h])  # graceful: the app can still ask to save
    elif action == "minimize":
        if shutil.which("xdotool"):
            _run(["xdotool", "windowminimize", str(int(h, 16))])
    elif action == "maximize":
        _run(["wmctrl", "-ir", h, "-b", "add,maximized_vert,maximized_horz"])
        _run(["wmctrl", "-ia", h])
    else:  # focus / restore
        _run(["wmctrl", "-ia", h])
    return True


def window_action(action: str, target: str, confirm_many: bool = False) -> dict:
    from . import desktop

    if oskit.LINUX and not shutil.which("wmctrl"):
        return {"ok": False, "said": "I can't manage windows here yet: the wmctrl tool isn't installed."}
    found = desktop.find_windows(target)
    if not found:
        what = "that window" if desktop._norm(target) in ("", "this", "that", "it") else target
        return {"ok": False, "said": f"I don't see {what} open."}
    if action in ("close", "minimize"):
        found = [w for w in found if not desktop.protected(w)]
    if not found:
        return {"ok": False, "said": "That one's off limits. I don't close myself or system windows."}
    if action == "close" and len(found) > 1 and not confirm_many:
        return {"ok": False, "confirm": True, "count": len(found),
                "said": f"There are {len(found)} {target} windows open. Close all of them?"}
    for w in found:
        _apply(action, w)
    name = desktop._friendly(found[0])
    verb = {"close": "Closed", "minimize": "Minimised", "maximize": "Maximised", "focus": "Switched to",
            "restore": "Brought back"}[action]
    return {"ok": True, "said": f"{verb} {name}" + (f" ({len(found)} windows)." if len(found) > 1 else "."), "target": name}


# ---------------------------------------------------------------- media, folders

def media(key: str, times: int = 1) -> dict:
    key = {"pause": "play_pause", "play": "play_pause", "resume": "play_pause", "stop": "play_pause", "skip": "next",
           "back": "previous", "louder": "volume_up", "quieter": "volume_down", "unmute": "mute"}.get(key, key)
    times = max(1, min(int(times or 1), 25))
    said = {"play_pause": "Done.", "next": "Next track.", "previous": "Previous track.", "volume_up": "Louder.",
            "volume_down": "Quieter.", "mute": "Toggled mute."}.get(key)
    if not said:
        return {"ok": False, "said": "I can't do that one."}
    if oskit.MAC:
        step = 6 * times
        script = {"volume_up": f"set volume output volume ((output volume of (get volume settings)) + {step})",
                  "volume_down": f"set volume output volume ((output volume of (get volume settings)) - {step})",
                  "mute": "set volume output muted not (output muted of (get volume settings))",
                  "play_pause": 'tell application "Music" to playpause', "next": 'tell application "Music" to next track',
                  "previous": 'tell application "Music" to previous track'}[key]
        _osa(script)
        return {"ok": True, "said": said}
    if key in ("play_pause", "next", "previous"):
        if not shutil.which("playerctl"):
            return {"ok": False, "said": "Media control needs the playerctl tool, which isn't installed."}
        _run(["playerctl", {"play_pause": "play-pause", "next": "next", "previous": "previous"}[key]])
    elif shutil.which("pactl"):
        if key == "mute":
            _run(["pactl", "set-sink-mute", "@DEFAULT_SINK@", "toggle"])
        else:
            _run(["pactl", "set-sink-volume", "@DEFAULT_SINK@", f"{'+' if key == 'volume_up' else '-'}{2 * times}%"])
    elif shutil.which("amixer"):
        if key == "mute":
            _run(["amixer", "-q", "set", "Master", "toggle"])
        else:
            _run(["amixer", "-q", "set", "Master", f"{2 * times}%{'+' if key == 'volume_up' else '-'}"])
    else:
        return {"ok": False, "said": "I can't reach the volume control here."}
    return {"ok": True, "said": said}


XDG = {"downloads": "DOWNLOAD", "documents": "DOCUMENTS", "desktop": "DESKTOP", "pictures": "PICTURES",
       "photos": "PICTURES", "music": "MUSIC", "videos": "VIDEOS"}


def known_folder(key: str) -> Path:
    if key == "home":
        return Path.home()
    if oskit.LINUX and shutil.which("xdg-user-dir") and key in XDG:
        if p := _run(["xdg-user-dir", XDG[key]]).strip():
            return Path(p)
    return Path.home() / {"photos": "Pictures"}.get(key, key.capitalize())
