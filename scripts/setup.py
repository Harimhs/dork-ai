#!/usr/bin/env python3
"""Dork setup: one script for Windows, Linux and macOS.

    python scripts/setup.py              scan this machine, install everything Dork needs, start it
    python scripts/setup.py --scan       only scan and report (nothing installed)

It reads the system first (OS, CPU, RAM, GPU, every drive / mount and its free space, what's already installed,
where existing models live), then decides where the large models go, installs what's missing, writes
data/config.json for this machine and starts Dork. Safe to re-run: finished steps are skipped.

Standard library only (it runs before Dork's own packages exist)."""

import argparse
import ctypes
import json
import os
import platform
import re
import shutil
import string
import subprocess
import sys
import time
import urllib.request
import zipfile
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
DATA = ROOT / "data"
WINDOWS, MAC, LINUX = sys.platform == "win32", sys.platform == "darwin", sys.platform.startswith("linux")
EXE = ".exe" if WINDOWS else ""
GB = 1024 ** 3
UA = {"User-Agent": "dork-setup"}

# Downloads that aren't in the repo (models are far too big for git)
SPEAKER_MODEL = ("https://github.com/k2-fsa/sherpa-onnx/releases/download/speaker-recongition-models/"
                 "3dspeaker_speech_eres2net_base_sv_zh-cn_3dspeaker_16k.onnx")
VAD_MODEL = "https://github.com/k2-fsa/sherpa-onnx/releases/download/asr-models/silero_vad.onnx"
KOKORO = ("https://github.com/thewh1teagle/kokoro-onnx/releases/download/model-files-v1.0/kokoro-v1.0.onnx",
          "https://github.com/thewh1teagle/kokoro-onnx/releases/download/model-files-v1.0/voices-v1.0.bin")
LLAMA_RELEASES = "https://api.github.com/repos/ggml-org/llama.cpp/releases/latest"


def say(msg: str = "", kind: str = "") -> None:
    mark = {"ok": "[ ok ]", "do": "[ .. ]", "warn": "[ !! ]", "skip": "[ -- ]", "head": "\n=="}.get(kind, "      ")
    print(f"{mark} {msg}" if kind != "head" else f"{mark} {msg}", flush=True)


def run(cmd: list[str], check: bool = True, **kw) -> int:
    say(" ".join(str(c) for c in cmd), "do")
    r = subprocess.run([str(c) for c in cmd], **kw)
    if check and r.returncode != 0:
        raise SystemExit(f"Command failed ({r.returncode}): {' '.join(map(str, cmd))}")
    return r.returncode


def out(cmd: list[str], timeout: float = 15) -> str:
    try:
        return subprocess.run(cmd, capture_output=True, text=True, timeout=timeout).stdout.strip()
    except (OSError, subprocess.SubprocessError):
        return ""


# ================================================================ 1. read the system

def ram_gb() -> float:
    if WINDOWS:
        class MEM(ctypes.Structure):
            _fields_ = [("dwLength", ctypes.c_ulong), ("dwMemoryLoad", ctypes.c_ulong),
                        ("ullTotalPhys", ctypes.c_ulonglong), ("ullAvailPhys", ctypes.c_ulonglong),
                        ("ullTotalPageFile", ctypes.c_ulonglong), ("ullAvailPageFile", ctypes.c_ulonglong),
                        ("ullTotalVirtual", ctypes.c_ulonglong), ("ullAvailVirtual", ctypes.c_ulonglong),
                        ("sullAvailExtendedVirtual", ctypes.c_ulonglong)]
        m = MEM(); m.dwLength = ctypes.sizeof(MEM)
        ctypes.windll.kernel32.GlobalMemoryStatusEx(ctypes.byref(m))
        return m.ullTotalPhys / GB
    if MAC:
        return int(out(["sysctl", "-n", "hw.memsize"]) or 0) / GB
    try:
        kb = int(re.search(r"MemTotal:\s+(\d+)", Path("/proc/meminfo").read_text()).group(1))
        return kb * 1024 / GB
    except (OSError, AttributeError):
        return 0.0


def cpu_name() -> str:
    if WINDOWS:
        return out(["powershell", "-NoProfile", "-Command", "(Get-CimInstance Win32_Processor).Name"]) or platform.processor()
    if MAC:
        return out(["sysctl", "-n", "machdep.cpu.brand_string"]) or platform.machine()
    try:
        return re.search(r"model name\s*:\s*(.+)", Path("/proc/cpuinfo").read_text()).group(1).strip()
    except (OSError, AttributeError):
        return platform.machine()


def cpu_flags() -> set[str]:
    if LINUX:
        try:
            return set(re.search(r"flags\s*:\s*(.+)", Path("/proc/cpuinfo").read_text()).group(1).split())
        except (OSError, AttributeError):
            return set()
    return set()


def gpus() -> list[dict]:
    found = []
    if shutil.which("nvidia-smi"):
        for line in out(["nvidia-smi", "--query-gpu=name,memory.total", "--format=csv,noheader,nounits"]).splitlines():
            name, mem = [x.strip() for x in line.split(",")[:2]]
            found.append({"vendor": "nvidia", "name": name, "vram_gb": round(int(mem) / 1024, 1)})
    if MAC and platform.machine() == "arm64":
        found.append({"vendor": "apple", "name": "Apple Silicon (Metal)", "vram_gb": None})
    if WINDOWS:
        for line in out(["powershell", "-NoProfile", "-Command",
                         "Get-CimInstance Win32_VideoController | ForEach-Object { $_.Name }"]).splitlines():
            if line and not any(g["name"] == line for g in found):
                vendor = "nvidia" if "NVIDIA" in line else "amd" if ("AMD" in line or "Radeon" in line) else "intel" if "Intel" in line else "other"
                found.append({"vendor": vendor, "name": line.strip(), "vram_gb": None})
    elif LINUX and shutil.which("lspci"):
        for line in out(["lspci"]).splitlines():
            if re.search(r"VGA|3D controller|Display", line) and not any(v in line for v in ("NVIDIA",) if found):
                vendor = "nvidia" if "NVIDIA" in line else "amd" if ("AMD" in line or "ATI" in line) else "intel" if "Intel" in line else "other"
                found.append({"vendor": vendor, "name": line.split(":", 2)[-1].strip(), "vram_gb": None})
    return found


def drives() -> list[dict]:
    """Every real drive / mount with its size and free space (where big models could go)."""
    mounts = []
    if WINDOWS:
        mask = ctypes.windll.kernel32.GetLogicalDrives()
        mounts = [f"{c}:\\" for i, c in enumerate(string.ascii_uppercase) if mask >> i & 1]
    elif MAC:
        mounts = ["/"] + [str(p) for p in Path("/Volumes").glob("*") if p.is_dir() and not p.is_symlink()]
    else:
        real = {"ext2", "ext3", "ext4", "xfs", "btrfs", "f2fs", "zfs", "ntfs", "ntfs3", "fuseblk", "vfat", "exfat"}
        try:
            for line in Path("/proc/mounts").read_text().splitlines():
                dev, mnt, fs = line.split()[:3]
                mnt = mnt.replace("\\040", " ")
                if fs in real and not mnt.startswith(("/snap", "/boot", "/var/lib/docker")):
                    mounts.append(mnt)
        except OSError:
            mounts = ["/"]
    seen, res = set(), []
    for m in mounts:
        try:
            u = shutil.disk_usage(m)
        except OSError:
            continue  # empty card reader, unmounted network drive, ...
        key = (u.total, u.used)
        if key in seen or u.total < 2 * GB:
            continue
        seen.add(key)
        res.append({"path": m, "total_gb": round(u.total / GB, 1), "free_gb": round(u.free / GB, 1),
                    "writable": os.access(m, os.W_OK)})
    return res


def ollama_exe() -> Path | None:
    if found := shutil.which("ollama"):
        return Path(found)
    cands = ([Path(os.environ.get("LOCALAPPDATA", "")) / "Programs/Ollama/ollama.exe", Path("C:/Program Files/Ollama/ollama.exe")]
             if WINDOWS else [Path("/Applications/Ollama.app/Contents/Resources/ollama"), Path("/usr/local/bin/ollama")]
             if MAC else [Path("/usr/local/bin/ollama"), Path("/usr/bin/ollama")])
    # an Ollama unpacked somewhere else (e.g. F:\Ollama\app\ollama.exe): look one level into each drive
    for d in drives():
        cands += [Path(d["path"]) / "Ollama" / "app" / f"ollama{EXE}", Path(d["path"]) / "Ollama" / f"ollama{EXE}"]
    return next((c for c in cands if c.exists()), None)


def ollama_model_dirs() -> list[Path]:
    """Existing Ollama model folders anywhere on this machine (so nothing is downloaded twice)."""
    cands = []
    if env := os.environ.get("OLLAMA_MODELS"):
        cands.append(Path(env))
    cands += [Path.home() / ".ollama" / "models", Path("/usr/share/ollama/.ollama/models"), Path("/var/lib/ollama/models")]
    for d in drives():
        base = Path(d["path"])
        cands += [base / "Ollama" / "models", base / "ollama" / "models", base / "dork-models" / "ollama"]
    seen, res = set(), []
    for c in cands:
        try:
            if (c / "manifests").exists() and c.resolve() not in seen:
                seen.add(c.resolve())
                res.append(c)
        except OSError:
            pass
    return res


def models_in(d: Path) -> list[str]:
    lib = d / "manifests" / "registry.ollama.ai" / "library"
    return sorted(f"{m.name}:{t.name}" for m in lib.glob("*") if m.is_dir() for t in m.glob("*")) if lib.exists() else []


def llama_server() -> Path | None:
    cands = [ROOT / "runtime" / "llama.cpp" / f"llama-server{EXE}", *sorted((ROOT / "runtime" / "llama.cpp").glob(f"**/llama-server{EXE}"))]
    if found := shutil.which("llama-server"):
        cands.append(Path(found))
    if exe := ollama_exe():
        base = exe.resolve().parent
        cands += [base / "lib" / "ollama" / f"llama-server{EXE}", base.parent / "lib" / "ollama" / f"llama-server{EXE}"]
    return next((c for c in cands if c.exists()), None)


def scan() -> dict:
    distro = ""
    if LINUX and Path("/etc/os-release").exists():
        m = re.search(r'PRETTY_NAME="?([^"\n]+)', Path("/etc/os-release").read_text())
        distro = m.group(1) if m else ""
    tools = {t: bool(shutil.which(t)) for t in ("git", "node", "npm", "curl", "ffmpeg", "wmctrl", "xdotool",
                                                 "playerctl", "pactl", "gtk-launch", "xdg-open", "brew", "winget")}
    browsers = {b: bool(shutil.which(b)) for b in ("msedge", "google-chrome", "chromium", "chromium-browser", "firefox")}
    if WINDOWS:
        browsers["msedge"] = Path("C:/Program Files (x86)/Microsoft/Edge/Application/msedge.exe").exists()
    model_dirs = ollama_model_dirs()
    return {
        "os": "windows" if WINDOWS else "macos" if MAC else "linux", "os_version": platform.platform(),
        "distro": distro, "arch": platform.machine().lower(), "python": platform.python_version(),
        "cpu": cpu_name(), "threads": os.cpu_count() or 4, "avx512": "avx512f" in cpu_flags(),
        "ram_gb": round(ram_gb(), 1), "gpus": gpus(), "drives": drives(),
        "home": str(Path.home()), "repo": str(ROOT),
        "ollama": str(ollama_exe() or ""), "ollama_model_dirs": [{"path": str(d), "models": models_in(d)} for d in model_dirs],
        "llama_server": str(llama_server() or ""), "tools": tools, "browsers": browsers,
        "wayland": os.environ.get("XDG_SESSION_TYPE") == "wayland",
    }


def report(s: dict) -> None:
    say("This machine", "head")
    say(f"OS        {s['os']} {s['distro'] or s['os_version']} ({s['arch']}), Python {s['python']}")
    say(f"CPU       {s['cpu']} - {s['threads']} threads{', AVX-512' if s['avx512'] else ''}")
    say(f"RAM       {s['ram_gb']} GB")
    say("GPU       " + (", ".join(g["name"] + (f" ({g['vram_gb']} GB)" if g["vram_gb"] else "") for g in s["gpus"]) or "none found"))
    for d in s["drives"]:
        say(f"Drive     {d['path']:<22} {d['free_gb']:>7} GB free of {d['total_gb']} GB{'' if d['writable'] else ' (read-only)'}")
    say(f"Ollama    {s['ollama'] or 'not installed'}")
    for d in s["ollama_model_dirs"]:
        say(f"Models    {d['path']}: {', '.join(d['models']) or 'empty'}")
    say(f"llama.cpp {s['llama_server'] or 'not found'}")
    missing = [t for t in ("wmctrl", "xdotool", "playerctl", "pactl") if LINUX and not s["tools"][t]]
    if missing:
        say(f"Linux tools missing for PC control: {', '.join(missing)} (run.sh installs them)", "warn")
    if s["wayland"]:
        say("Wayland session: listing / closing other windows is limited (X11 or XWayland apps only)", "warn")


# ================================================================ 2. decide where things go

def pick_locations(s: dict, args) -> dict:
    """Big models go to the drive with room. Existing Ollama models are reused where they are."""
    def free_at(p: Path) -> float:
        try:
            return shutil.disk_usage(p if p.exists() else p.anchor or "/").free / GB
        except OSError:
            return 0

    if args.models_dir:
        models = Path(args.models_dir)
    elif free_at(ROOT) >= 6:
        models = ROOT / "models"
    else:
        best = max((d for d in s["drives"] if d["writable"]), key=lambda d: d["free_gb"], default=None)
        models = Path(best["path"]) / "dork-models" if best else ROOT / "models"
    existing = [d for d in s["ollama_model_dirs"] if d["models"]]
    if args.ollama_models:
        ollama_models = Path(args.ollama_models)
    elif existing:
        ollama_models = Path(existing[0]["path"])
    elif free_at(Path(s["home"])) >= 8:
        ollama_models = Path(s["home"]) / ".ollama" / "models"
    else:
        ollama_models = models.parent / "ollama-models" if models.parent != ROOT else models / "ollama"
    # brain size by memory: 3B needs ~2.5 GB free while running; small machines get 1.5B
    brain = args.brain or ("qwen2.5:3b" if s["ram_gb"] >= 7.5 else "qwen2.5:1.5b")
    return {"models_dir": models, "ollama_models": ollama_models, "brain": brain}


# ================================================================ 3. install

def download(url: str, dest: Path, what: str) -> None:
    if dest.exists() and dest.stat().st_size > 0:
        say(f"{what}: already there", "skip")
        return
    dest.parent.mkdir(parents=True, exist_ok=True)
    part = dest.with_suffix(dest.suffix + ".part")
    have = part.stat().st_size if part.exists() else 0
    req = urllib.request.Request(url, headers={**UA, **({"Range": f"bytes={have}-"} if have else {})})
    for attempt in range(6):
        try:
            with urllib.request.urlopen(req, timeout=60) as r, open(part, "ab" if have else "wb") as f:
                total = int(r.headers.get("Content-Length") or 0) + have
                done, t0 = have, time.time()
                while chunk := r.read(1 << 16):
                    f.write(chunk)
                    done += len(chunk)
                    if time.time() - t0 > 2:
                        t0 = time.time()
                        print(f"\r       {what}: {done / 1e6:.0f}/{total / 1e6:.0f} MB", end="", flush=True)
            print()
            part.rename(dest)
            say(f"{what}: downloaded", "ok")
            return
        except (OSError, urllib.error.URLError) as e:
            have = part.stat().st_size if part.exists() else 0
            req = urllib.request.Request(url, headers={**UA, **({"Range": f"bytes={have}-"} if have else {})})
            say(f"{what}: {e.__class__.__name__}, retrying ({attempt + 1}/5)", "warn")
            time.sleep(3 * (attempt + 1))
    raise SystemExit(f"Couldn't download {url}")


def venv_python() -> Path:
    return ROOT / ".venv" / ("Scripts/python.exe" if WINDOWS else "bin/python")


def python_env(args) -> None:
    say("Python packages", "head")
    if not venv_python().exists():
        run([sys.executable, "-m", "venv", ROOT / ".venv"])
    py = venv_python()
    run([py, "-m", "pip", "install", "--upgrade", "pip", "-q"], check=False)
    run([py, "-m", "pip", "install", "-r", ROOT / "requirements.txt"])
    if not WINDOWS:  # Windows uses the Edge that ships with it; elsewhere Playwright's own Chromium
        run([py, "-m", "playwright", "install", "chromium"] + (["--with-deps"] if LINUX and os.geteuid() == 0 else []),
            check=False)
    say("Python packages ready", "ok")


def install_ollama(s: dict) -> Path | None:
    exe = ollama_exe()
    if exe:
        say(f"Ollama: {exe}", "skip")
        return exe
    say("Ollama (local brain server)", "head")
    if LINUX:
        run(["sh", "-c", "curl -fsSL https://ollama.com/install.sh | sh"], check=False)
    elif MAC:
        if shutil.which("brew"):
            run(["brew", "install", "ollama"], check=False)
        else:
            say("Install Ollama from https://ollama.com/download then re-run this script", "warn")
    elif shutil.which("winget"):
        run(["winget", "install", "--id", "Ollama.Ollama", "-e", "--accept-source-agreements",
             "--accept-package-agreements"], check=False)
    else:
        say("Install Ollama from https://ollama.com/download then re-run this script", "warn")
    return ollama_exe()


def ollama_up(url="http://127.0.0.1:11434") -> bool:
    try:
        with urllib.request.urlopen(url + "/api/tags", timeout=3):
            return True
    except OSError:
        return False


def pull_models(exe: Path | None, loc: dict) -> None:
    say("Brain models (Ollama)", "head")
    if not exe:
        say("Ollama isn't installed: skipping the local brain models", "warn")
        return
    loc["ollama_models"].mkdir(parents=True, exist_ok=True)
    if not ollama_up():
        env = {**os.environ, "OLLAMA_MODELS": str(loc["ollama_models"])}
        kw = {"creationflags": 0x08000008} if WINDOWS else {"start_new_session": True}
        subprocess.Popen([str(exe), "serve"], env=env, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, **kw)
        for _ in range(40):
            time.sleep(0.5)
            if ollama_up():
                break
    have = set(models_in(loc["ollama_models"]))
    for m in (loc["brain"], "nomic-embed-text:latest"):
        if m in have or (m.endswith(":latest") and m[:-7] in have):
            say(f"{m}: already there", "skip")
            continue
        run([exe, "pull", m], check=False, env={**os.environ, "OLLAMA_MODELS": str(loc["ollama_models"])})


def llama_cpp(s: dict) -> Path | None:
    """llama.cpp's C++ server: use one that's already here, else download the right prebuilt release."""
    say("C++ engine (llama.cpp server)", "head")
    if found := llama_server():
        say(f"llama.cpp server: {found}", "skip")
        return found
    try:
        rel = json.load(urllib.request.urlopen(urllib.request.Request(LLAMA_RELEASES, headers=UA), timeout=30))
    except OSError as e:
        say(f"Couldn't reach GitHub for llama.cpp ({e.__class__.__name__}); Dork will use Ollama for the brain", "warn")
        return None
    nvidia = any(g["vendor"] == "nvidia" for g in s["gpus"])
    arch = "arm64" if s["arch"] in ("arm64", "aarch64") else "x64"
    if WINDOWS:
        prefs = ([r"win-cuda-12[\d.]*-x64\.zip"] if nvidia else []) + [rf"win-cpu-{arch}\.zip", r"win-avx2-x64\.zip"]
    elif MAC:
        prefs = [rf"macos-{arch}\.(zip|tar\.gz)"]
    else:
        prefs = ([r"ubuntu-vulkan-x64\.(zip|tar\.gz)"] if nvidia else []) + [rf"ubuntu-{arch}\.(zip|tar\.gz)"]
    assets = {a["name"]: a["browser_download_url"] for a in rel.get("assets", [])}
    pick = next((n for p in prefs for n in assets if re.search(p, n) and "cudart" not in n), None)
    if not pick:
        say("No prebuilt llama.cpp for this system; Dork will use Ollama for the brain", "warn")
        return None
    dest_dir = ROOT / "runtime" / "llama.cpp"
    archive = ROOT / "runtime" / pick
    download(assets[pick], archive, f"llama.cpp {rel.get('tag_name', '')}")
    dest_dir.mkdir(parents=True, exist_ok=True)
    if pick.endswith(".zip"):
        zipfile.ZipFile(archive).extractall(dest_dir)
    else:
        import tarfile
        tarfile.open(archive).extractall(dest_dir)
    for f in dest_dir.rglob("*"):
        if not WINDOWS and f.is_file() and (f.name.startswith("llama-") or f.suffix in (".so", ".dylib")):
            f.chmod(0o755)
    return llama_server()


def hearing_models(loc: dict, args) -> None:
    say("Speech models (offline hearing, voiceprints, offline voice)", "head")
    m = loc["models_dir"]
    download(SPEAKER_MODEL, m / "speaker" / Path(SPEAKER_MODEL).name, "voiceprint model (40 MB)")
    download(VAD_MODEL, m / "speaker" / "silero_vad.onnx", "speech detector (1 MB)")
    whisper_dir = m / "whisper"
    if any(whisper_dir.glob("models--Systran--faster-whisper-base.en/snapshots/*/model.bin")):
        say("Whisper base.en: already there", "skip")
    else:
        run([venv_python(), "-c", "from faster_whisper import WhisperModel; "
             f"WhisperModel('base.en', device='cpu', compute_type='int8', download_root=r'{whisper_dir}')"],
            check=False, env={**os.environ, "HF_HUB_DISABLE_SYMLINKS_WARNING": "1"})
    if args.no_kokoro:
        say("Kokoro offline voice: skipped (--no-kokoro)", "skip")
    else:
        download(KOKORO[0], m / "kokoro" / "kokoro-v1.0.onnx", "offline voice Kokoro (310 MB)")
        download(KOKORO[1], m / "kokoro" / "voices-v1.0.bin", "offline voice styles (27 MB)")


def write_config(s: dict, loc: dict, server: Path | None) -> None:
    say("Dork config for this machine", "head")
    DATA.mkdir(exist_ok=True)
    path = DATA / "config.json"
    cfg = json.loads(path.read_text(encoding="utf-8")) if path.exists() else {}
    cfg.update({
        "models_dir": "" if loc["models_dir"] == ROOT / "models" else str(loc["models_dir"]),
        "ollama_models": str(loc["ollama_models"]),
        "ollama_model": loc["brain"],
        "ollama_threads": s["threads"],
        "llamacpp_exe": str(server or ""),
        "local_engine": "llamacpp" if server else "ollama",
    })
    cfg.setdefault("provider", "ollama")
    path.write_text(json.dumps(cfg, indent=2), encoding="utf-8")
    (DATA / "system.json").write_text(json.dumps(s, indent=2), encoding="utf-8")
    say(f"{path} (brain {loc['brain']}, engine {cfg['local_engine']}, {s['threads']} threads)", "ok")
    say(f"machine profile saved to {DATA / 'system.json'}", "ok")


def record_home() -> None:
    """Tell the desktop app where Dork lives (it reads dork-home.txt from its data folder)."""
    base = (Path(os.environ.get("APPDATA", "")) if WINDOWS else Path.home() / "Library" / "Application Support"
            if MAC else Path(os.environ.get("XDG_CONFIG_HOME", Path.home() / ".config")))
    for name in ("Dork", "dork-desktop"):
        (base / name).mkdir(parents=True, exist_ok=True)
        (base / name / "dork-home.txt").write_text(str(ROOT), encoding="utf-8")


def autostart() -> None:
    say("Start Dork at login", "head")
    if WINDOWS:
        startup = Path(os.environ["APPDATA"]) / "Microsoft/Windows/Start Menu/Programs/Startup"
        (startup / "Dork.vbs").write_text(
            f'CreateObject("WScript.Shell").Run """{ROOT / "start-dork.vbs"}""", 0, False\n', encoding="utf-8")
        say(f"added {startup / 'Dork.vbs'}", "ok")
    elif MAC:
        plist = Path.home() / "Library/LaunchAgents/dev.hari.dork.plist"
        plist.parent.mkdir(parents=True, exist_ok=True)
        plist.write_text(f"""<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0"><dict><key>Label</key><string>dev.hari.dork</string>
<key>ProgramArguments</key><array><string>/bin/bash</string><string>{ROOT / 'start-dork.sh'}</string></array>
<key>RunAtLoad</key><true/></dict></plist>
""")
        say(f"added {plist}", "ok")
    else:
        auto = Path(os.environ.get("XDG_CONFIG_HOME", Path.home() / ".config")) / "autostart" / "dork.desktop"
        auto.parent.mkdir(parents=True, exist_ok=True)
        auto.write_text(f"[Desktop Entry]\nType=Application\nName=Dork\nExec=bash {ROOT / 'start-dork.sh'}\n"
                        "X-GNOME-Autostart-enabled=true\nNoDisplay=true\n")
        say(f"added {auto}", "ok")


def desktop_app() -> None:
    say("Desktop app (Electron)", "head")
    if not shutil.which("npm"):
        say("npm not found: skipping the desktop app (install Node.js, then re-run with --desktop)", "warn")
        return
    npm = "npm.cmd" if WINDOWS else "npm"
    run([npm, "install"], cwd=ROOT / "desktop", check=False)
    run([npm, "run", "dist"], cwd=ROOT / "desktop", check=False,
        env={k: v for k, v in os.environ.items() if k != "ELECTRON_RUN_AS_NODE"})
    say(f"installers are in {ROOT / 'desktop' / 'dist'}", "ok")


def start_dork() -> None:
    say("Starting Dork", "head")
    if WINDOWS:
        subprocess.Popen(["wscript.exe", str(ROOT / "start-dork.vbs")], cwd=ROOT)
    else:
        subprocess.Popen(["bash", str(ROOT / "start-dork.sh")], cwd=ROOT)
    for _ in range(120):
        time.sleep(1)
        try:
            with urllib.request.urlopen("http://127.0.0.1:7777/manifest.json", timeout=2):
                say("Dork is running: http://localhost:7777  (assistant: http://localhost:7777/assistant)", "ok")
                say(f"First run? Your master key is printed at the top of {DATA / 'dork.log'}: write it down.")
                return
        except OSError:
            pass
    say(f"Dork didn't answer yet; see {DATA / 'dork.log'}", "warn")


def main() -> None:
    ap = argparse.ArgumentParser(description="Set up and start Dork on this machine (Windows / Linux / macOS).")
    ap.add_argument("--scan", action="store_true", help="only read the system and report; install nothing")
    ap.add_argument("--models-dir", help="where speech models go (default: <repo>/models, or the roomiest drive)")
    ap.add_argument("--ollama-models", help="where Ollama's brain models go (default: reuse existing, else home)")
    ap.add_argument("--brain", help="local brain model (default: qwen2.5:3b, or qwen2.5:1.5b under 8 GB RAM)")
    ap.add_argument("--no-models", action="store_true", help="skip all model downloads")
    ap.add_argument("--no-kokoro", action="store_true", help="skip the 340 MB offline voice")
    ap.add_argument("--no-cpp", action="store_true", help="don't set up the llama.cpp C++ engine (use Ollama only)")
    ap.add_argument("--desktop", action="store_true", help="also build the desktop app (needs Node.js)")
    ap.add_argument("--autostart", action="store_true", help="start Dork automatically at login")
    ap.add_argument("--no-start", action="store_true", help="don't start Dork at the end")
    args = ap.parse_args()

    if sys.version_info < (3, 10):
        raise SystemExit(f"Dork needs Python 3.10 or newer (this is {platform.python_version()}).")
    s = scan()
    report(s)
    loc = pick_locations(s, args)
    say("Plan", "head")
    say(f"speech models  -> {loc['models_dir']}")
    say(f"brain models   -> {loc['ollama_models']}  ({loc['brain']} + nomic-embed-text)")
    if args.scan:
        DATA.mkdir(exist_ok=True)
        (DATA / "system.json").write_text(json.dumps(s, indent=2), encoding="utf-8")
        return

    python_env(args)
    exe = install_ollama(s)
    server = None if args.no_cpp else llama_cpp(s)
    if not args.no_models:
        pull_models(exe, loc)
        hearing_models(loc, args)
    write_config(s, loc, server)
    record_home()
    if args.autostart:
        autostart()
    if args.desktop:
        desktop_app()
    if not args.no_start:
        start_dork()
    say("Done. Optional: a free Gemini key makes voice replies faster: .venv python -m dork set-api-key gemini", "head")


if __name__ == "__main__":
    main()
