#!/usr/bin/env bash
# Starts Dork in the background on Linux / macOS. Output goes to data/dork.log.
# (run.sh --autostart sets this up to run at login.) Dork starts its own backup brain (Ollama) and C++ engine.
cd "$(dirname "$(readlink -f "$0" 2>/dev/null || echo "$0")")" || exit 1
mkdir -p data
export PYTHONIOENCODING=utf-8 PYTHONUNBUFFERED=1
nohup .venv/bin/python -m dork >> data/dork.log 2>&1 &
echo "Dork starting in the background (log: data/dork.log). Open http://localhost:7777"
