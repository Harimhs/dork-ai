"""Optional local engine: llama.cpp's own server with speculative decoding, instead of Ollama for the brain.

A tiny draft model (qwen2.5:0.5b) guesses the next few words and the real brain (qwen2.5:3b) checks them in one
pass, so the answers are identical but come ~20% faster on this CPU (measured: 9.6 -> 11.4 tok/s).
Ollama stays for embeddings (memory / wake / fast-path meaning search). The model files are Ollama's own:
nothing is downloaded twice. If this server can't start, Dork uses Ollama as before.

Enable with "local_engine": "llamacpp" in data/config.json."""

import json
import logging
import os
import subprocess
import time
from pathlib import Path

import httpx

from . import oskit
from .config import CFG, DATA_DIR, ROOT

log = logging.getLogger("dork.engine")


def _server_exe() -> Path:
    """llama.cpp's server: the configured one, else Dork's runtime folder, PATH, or the copy inside Ollama."""
    if CFG.get("llamacpp_exe") and Path(CFG["llamacpp_exe"]).exists():
        return Path(CFG["llamacpp_exe"])
    return next((p for p in oskit.llama_server_candidates(ROOT) if p.exists()), Path("llama-server" + oskit.EXE))


SERVER_EXE = _server_exe()
MODELS = Path(CFG.get("ollama_models") or oskit.ollama_models_dir())
_proc: subprocess.Popen | None = None
_embed_proc: subprocess.Popen | None = None
starting = False     # the C++ servers are loading: callers wait instead of loading the models in Ollama too
ready = False        # the brain is served by llama.cpp
embed_ready = False  # embeddings (memory / wake / fast-path meaning search) are served by llama.cpp


def url() -> str:
    return f"http://127.0.0.1:{CFG['llamacpp_port']}"


def embed_url() -> str:
    return f"http://127.0.0.1:{CFG['llamacpp_embed_port']}"


def _up(u: str) -> bool:
    try:
        return httpx.get(u + "/health", timeout=2).status_code == 200
    except httpx.HTTPError:
        return False


def _launch(args: list[str], log_name: str) -> subprocess.Popen:
    log_file = open(DATA_DIR / log_name, "ab")
    return subprocess.Popen(args, stdout=log_file, stderr=log_file, **oskit.hidden())


def _wait(proc: subprocess.Popen, u: str, what: str) -> bool:
    for _ in range(240):
        time.sleep(0.5)
        if proc.poll() is not None:
            log.warning("engine: %s server exited (%s); using Ollama for it", what, proc.returncode)
            return False
        if _up(u):
            return True
    log.warning("engine: %s server didn't come up; using Ollama for it", what)
    return False


def start_embed() -> bool:
    """The embedding model (nomic-embed-text) on its own small C++ server."""
    global _embed_proc, embed_ready
    if CFG.get("local_engine") != "llamacpp" or not CFG.get("llamacpp_embed", True):
        return False
    if _up(embed_url()):
        embed_ready = True
        return True
    model = gguf(CFG["embed_model"] if ":" in CFG["embed_model"] else CFG["embed_model"] + ":latest")
    if not SERVER_EXE.exists() or not model:
        return False
    _embed_proc = _launch([str(SERVER_EXE), "-m", str(model), "--port", str(CFG["llamacpp_embed_port"]),
                           "--host", "127.0.0.1", "--embedding", "-t", "4", "-c", "8192", "-b", "2048", "-ub", "2048",
                           "--no-webui"], "llamacpp-embed.log")
    embed_ready = _wait(_embed_proc, embed_url(), "embedding")
    if embed_ready:
        log.info("engine: llama.cpp serving embeddings (%s)", CFG["embed_model"])
    return embed_ready


def gguf(name: str) -> Path | None:
    """Ollama model name ("qwen2.5:3b") -> the GGUF file Ollama already downloaded."""
    model, _, tag = name.partition(":")
    manifest = MODELS / "manifests" / "registry.ollama.ai" / "library" / model / (tag or "latest")
    if not manifest.exists():
        return None
    layers = json.loads(manifest.read_text(encoding="utf-8"))["layers"]
    digest = next((l["digest"] for l in layers if l["mediaType"].endswith(".model")), None)
    path = MODELS / "blobs" / digest.replace(":", "-") if digest else None
    return path if path and path.exists() else None


def start() -> bool:
    """Start llama-server (blocking up to ~2 min while the model loads). True when it's serving."""
    global _proc, ready, starting
    if CFG.get("local_engine") != "llamacpp":
        return False
    starting = True
    try:
        return _start()
    finally:
        starting = False


def _start() -> bool:
    global _proc, ready
    start_embed()
    try:
        if httpx.get(url() + "/health", timeout=2).status_code == 200:
            ready = True  # already running (e.g. Dork restarted, server kept)
            return True
    except httpx.HTTPError:
        pass
    main, draft = gguf(CFG["ollama_model"]), gguf(CFG.get("llamacpp_draft") or "")
    if not SERVER_EXE.exists() or not main:
        log.warning("engine: llama-server or model file missing; using Ollama")
        return False
    args = [str(SERVER_EXE), "-m", str(main), "--port", str(CFG["llamacpp_port"]), "--host", "127.0.0.1",
            "-t", str(CFG["ollama_threads"]), "-c", str(CFG["context_tokens"] * 2), "-np", "2",  # 2 slots: chat + voice caches
            "--jinja", "--no-webui", "--cache-reuse", "256"]
    if draft and CFG.get("llamacpp_draft"):
        args += ["-md", str(draft), "--spec-draft-n-max", "6"]
    log_file = open(DATA_DIR / "llamacpp.log", "ab")
    _proc = subprocess.Popen(args, stdout=log_file, stderr=log_file, **oskit.hidden())
    for _ in range(240):
        time.sleep(0.5)
        if _proc.poll() is not None:
            log.warning("engine: llama-server exited (%s); using Ollama", _proc.returncode)
            return False
        try:
            if httpx.get(url() + "/health", timeout=2).status_code == 200:
                ready = True
                log.info("engine: llama.cpp serving %s%s", CFG["ollama_model"], f" + draft {CFG['llamacpp_draft']}" if draft else "")
                return True
        except httpx.HTTPError:
            pass
    log.warning("engine: llama-server didn't come up; using Ollama")
    return False


def stop() -> None:
    global ready, embed_ready
    ready = embed_ready = False
    for p in (_proc, _embed_proc):
        if p and p.poll() is None:
            p.terminate()
