# Dork

A personal JARVIS-like assistant that runs on your own machine: voice assistant with a reactor UI, text chat,
long-term memory, web lookups, and hands on the PC (open / close / switch apps, timers, media). Every device you own
can reach it; only the machine running it does the thinking.

Works on **Windows, Linux and macOS**.

## Install and run

One command sets everything up: it reads the machine first (OS, CPU, RAM, GPU, every drive and its free space,
what's already installed and where existing models are), then installs what's missing and starts Dork.

**Linux / macOS**
```bash
./run.sh
```

**Windows** (PowerShell, or double-click `run.bat`)
```powershell
.\run.ps1
```

Useful options (same on every OS):

| Option | What it does |
|---|---|
| `--scan` | only read the machine and print the plan; install nothing |
| `--autostart` | start Dork at every login |
| `--desktop` | also build the desktop app (needs Node.js) |
| `--brain qwen2.5:1.5b` | pick the local brain model (default: 3B, or 1.5B under 8 GB RAM) |
| `--models-dir PATH` / `--ollama-models PATH` | put the models somewhere specific |
| `--no-kokoro` / `--no-models` / `--no-cpp` | skip the offline voice / all models / the C++ engine |

What setup installs (nothing large is kept in git):

| Piece | What it is |
|---|---|
| Python packages | in `.venv` (`requirements.txt`) |
| System tools | Linux: PortAudio, libsndfile, wmctrl, xdotool, playerctl (`run.sh`). macOS: PortAudio via Homebrew |
| Ollama + `qwen2.5:3b`, `nomic-embed-text` | local brain + meaning search (existing models anywhere on the machine are reused) |
| llama.cpp server | the C++ engine Dork serves the models with (uses the one inside Ollama if present, else the prebuilt release for this OS/CPU/GPU) |
| Whisper `base.en`, 3D-Speaker voiceprint, Silero VAD | offline hearing and voice recognition |
| Kokoro | offline voice |

The first start prints your **master key** at the top of `data/dork.log`: write it down. It signs in new devices.

## Use it

- Assistant (voice, reactor): `http://localhost:7777/assistant`. On the PC, just say **"Dork, ..."**.
- Chat: `http://localhost:7777`
- Other devices: the "Same Wi-Fi" / "Anywhere" (Tailscale) address printed at start-up.
- Start / stop later: `start-dork.sh` (Linux / macOS) or `start-dork.vbs` (Windows).

Optional: a free Gemini key makes voice replies faster and smarter when online (the local brain is the fallback):
```bash
.venv/bin/python -m dork set-api-key gemini        # Windows: .venv\Scripts\python -m dork set-api-key gemini
```

## Layout

```
dork/          server: brain (llm, engine), assistant (voice), hearing, voice, memory, desktop control, web search
web/           chat UI and the assistant reactor (Three.js / 2D)
desktop/       Electron desktop app (tray, hotkey, hidden assistant window)
scripts/       setup.py (system scan + install)
tests/         speed / accuracy / voice tests
data/          your database, config and keys (created at setup, never committed)
models/        downloaded models (never committed)
```

## Linux notes

- PC control (list / close / switch windows) uses `wmctrl` and `xdotool`. That works on X11 and for X11 apps under
  XWayland; pure Wayland sessions don't let other programs manage windows.
- Media keys use `playerctl`, and volume uses `pactl` (PulseAudio / PipeWire).
- The microphone needs PortAudio (`libportaudio2`), which `run.sh` installs.
