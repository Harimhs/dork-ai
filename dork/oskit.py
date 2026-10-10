"""OS differences in one place: Windows, Linux and macOS.

Everything else in Dork asks this module instead of assuming Windows (or a particular drive letter)."""

import os
import shutil
import subprocess
import sys
from pathlib import Path

WINDOWS = sys.platform == "win32"
MAC = sys.platform == "darwin"
LINUX = sys.platform.startswith("linux")
EXE = ".exe" if WINDOWS else ""


def hidden() -> dict:
    """Popen kwargs for a background process with no console window."""
    return {"creationflags": 0x08000000} if WINDOWS else {}


def detached() -> dict:
    """Popen kwargs for a process that outlives Dork."""
    if WINDOWS:
        return {"creationflags": 0x00000008 | 0x08000000, "close_fds": True}
    return {"start_new_session": True, "close_fds": True}


def open_path(target: str) -> None:
    """Open a file, folder or URL with the system's default app."""
    if WINDOWS:
        os.startfile(target)  # noqa: S606
    elif MAC:
        subprocess.Popen(["open", target], **detached())
    else:
        subprocess.Popen(["xdg-open", target], **detached())


def venv_python(root: Path) -> Path:
    return root / ".venv" / ("Scripts/python.exe" if WINDOWS else "bin/python")


def ollama_models_dir() -> Path:
    """Where Ollama keeps its models: $OLLAMA_MODELS, else the per-OS default."""
    if env := os.environ.get("OLLAMA_MODELS"):
        return Path(env)
    home = Path.home() / ".ollama" / "models"
    if LINUX and not home.exists() and Path("/usr/share/ollama/.ollama/models").exists():
        return Path("/usr/share/ollama/.ollama/models")  # the Linux system service's home
    return home


def ollama_exe() -> Path | None:
    """The Ollama binary: on PATH, or in its usual install places."""
    if found := shutil.which("ollama"):
        return Path(found)
    candidates = []
    if WINDOWS:
        candidates = [Path(os.environ.get("LOCALAPPDATA", "")) / "Programs" / "Ollama" / "ollama.exe",
                      Path("C:/Program Files/Ollama/ollama.exe")]
    elif MAC:
        candidates = [Path("/Applications/Ollama.app/Contents/Resources/ollama"), Path("/usr/local/bin/ollama")]
    else:
        candidates = [Path("/usr/local/bin/ollama"), Path("/usr/bin/ollama")]
    return next((c for c in candidates if c.exists()), None)


def llama_server_candidates(root: Path) -> list[Path]:
    """Where a llama.cpp server binary may be: Dork's own runtime folder, PATH, and next to Ollama."""
    out = [root / "runtime" / "llama.cpp" / ("llama-server" + EXE)]
    out += sorted((root / "runtime" / "llama.cpp").glob("**/llama-server" + EXE))
    if found := shutil.which("llama-server"):
        out.append(Path(found))
    if exe := ollama_exe():
        base = exe.resolve().parent
        out += [base / "lib" / "ollama" / ("llama-server" + EXE), base.parent / "lib" / "ollama" / ("llama-server" + EXE)]
    return out


def desktop_app() -> Path | None:
    """The installed Dork Desktop app, if any."""
    if WINDOWS:
        p = Path(os.environ.get("LOCALAPPDATA", "")) / "Programs" / "dork-desktop" / "Dork.exe"
    elif MAC:
        p = Path("/Applications/Dork.app/Contents/MacOS/Dork")
    else:
        p = next((Path(x) for x in ("/opt/Dork/dork-desktop", str(Path.home() / "Applications" / "Dork.AppImage"))
                  if Path(x).exists()), Path("/opt/Dork/dork-desktop"))
    return p if p.exists() else None


def ensure_ollama(url: str, models_dir: str = "") -> bool:
    """Start `ollama serve` in the background if Ollama isn't answering (it's Dork's backup brain)."""
    import time
    import urllib.request

    def up() -> bool:
        try:
            with urllib.request.urlopen(url.rstrip("/") + "/api/tags", timeout=2):
                return True
        except OSError:
            return False

    if up():
        return True
    exe = ollama_exe()
    if not exe:
        return False
    env = dict(os.environ)
    if models_dir:
        env["OLLAMA_MODELS"] = models_dir
    subprocess.Popen([str(exe), "serve"], env=env, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, **detached())
    for _ in range(30):
        time.sleep(0.5)
        if up():
            return True
    return False
