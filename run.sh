#!/usr/bin/env bash
# Dork — install / update / start on Linux or macOS.
#
#   ./run.sh                 set everything up and start Dork
#   ./run.sh --autostart     ...and start Dork at every login
#   ./run.sh --scan          just read this machine and report
#   (all options: ./run.sh --help)
#
# Installs the system pieces Python can't (Python itself, the audio library, PC-control tools), then hands over
# to scripts/setup.py, which scans drives / RAM / GPU, downloads the models and the C++ engine, and starts Dork.
set -euo pipefail
cd "$(dirname "$(readlink -f "$0" 2>/dev/null || echo "$0")")"

OS="$(uname -s)"
say() { printf '\n== %s\n' "$*"; }
need_sudo() { if [ "$(id -u)" -eq 0 ]; then "$@"; else sudo "$@"; fi; }

if [ "$OS" = "Linux" ]; then
  say "Linux: system packages (audio, PC control, Python)"
  PKGS_APT="python3 python3-venv python3-pip libportaudio2 libsndfile1 curl git wmctrl xdotool playerctl pulseaudio-utils xdg-utils libgtk-3-bin"
  PKGS_DNF="python3 python3-pip portaudio libsndfile curl git wmctrl xdotool playerctl pulseaudio-utils xdg-utils gtk3"
  PKGS_PAC="python python-pip portaudio libsndfile curl git wmctrl xdotool playerctl libpulse xdg-utils gtk3"
  PKGS_ZYP="python3 python3-pip portaudio libsndfile1 curl git wmctrl xdotool playerctl pulseaudio-utils xdg-utils gtk3-tools"
  if command -v apt-get >/dev/null; then need_sudo apt-get update -qq && need_sudo apt-get install -y $PKGS_APT
  elif command -v dnf >/dev/null; then need_sudo dnf install -y $PKGS_DNF
  elif command -v pacman >/dev/null; then need_sudo pacman -S --needed --noconfirm $PKGS_PAC
  elif command -v zypper >/dev/null; then need_sudo zypper install -y $PKGS_ZYP
  else echo "Unknown package manager: install Python 3.10+, PortAudio, libsndfile, wmctrl, xdotool, playerctl yourself."; fi
elif [ "$OS" = "Darwin" ]; then
  say "macOS: system packages"
  if ! command -v brew >/dev/null; then
    echo "Homebrew is needed: https://brew.sh  (then run ./run.sh again)"; exit 1
  fi
  brew list python@3.11 >/dev/null 2>&1 || brew install python@3.11
  brew list portaudio >/dev/null 2>&1 || brew install portaudio
else
  echo "This is $OS: on Windows use run.ps1 (or run.bat)."; exit 1
fi

# a Python 3.10+ to build the environment with
PY=""
for c in python3.12 python3.11 python3.10 python3; do
  if command -v "$c" >/dev/null && "$c" -c 'import sys; sys.exit(sys.version_info < (3, 10))'; then PY="$c"; break; fi
done
[ -n "$PY" ] || { echo "Python 3.10+ not found."; exit 1; }

chmod +x start-dork.sh 2>/dev/null || true
exec "$PY" scripts/setup.py "$@"
