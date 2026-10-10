"""Dork's brain. Interchangeable providers, picked by `provider` in config:

  gemini    — Google Gemini API (free tier available, needs internet + API key)
  groq      — Groq API (free tier, open models like Llama)
  anthropic — Claude via the Anthropic API (paid, needs internet + API key)
  ollama    — a local model on this machine (offline, needs the model downloaded)

Everything else in Dork talks only to the functions at the bottom of this file.
"""

import asyncio
import contextlib
import contextvars
import json
import logging
import os
import time
from typing import AsyncIterator

import httpx

from . import engine
from .config import CFG

log = logging.getLogger("dork.llm")

PROVIDER = CFG["provider"]
LOCAL = PROVIDER == "ollama"

# Context budget: for a local model this is the hard window (the reply must fit too);
# for Claude it's a cost cap on how much history is sent per message.
CONTEXT_LIMIT = CFG["context_tokens"]

# Identifies which model made the memory vectors (see memory.reindex).
EMBED_ID = {
    "gemini": f"gemini:{CFG['gemini_embed_model']}:{CFG['gemini_embed_dims']}",
    "ollama": f"ollama:{CFG['embed_model']}",
}.get(PROVIDER, "none")
REPLY_RESERVE = CFG["ollama_max_tokens"] if LOCAL else 0

# A local model shares one CPU: serialize generations, chat first (see server.run_in_background).
# Cloud providers run remotely, so no lock is needed.
gen_lock = asyncio.Lock()
chat_waiting = 0
voice_until = 0.0  # while Hari is talking to the voice assistant, background jobs wait (see server.run_in_background)


# Per-task brain override: the voice assistant runs on Gemini (fast) while text chat keeps the configured
# provider. Set with `with llm.cloud_brain():`; anything that fails in the cloud falls back to the local brain.
_BRAIN: contextvars.ContextVar[str | None] = contextvars.ContextVar("dork_brain", default=None)

GEMINI = {
    "base_url": "https://generativelanguage.googleapis.com/v1beta/openai",
    "key": CFG.get("gemini_api_key") or os.environ.get("GEMINI_API_KEY", ""),
    "model": CFG.get("voice_gemini_model") or CFG["gemini_model"],  # Flash-Lite: faster, bigger free allowance
    "background_model": CFG.get("voice_gemini_model") or CFG["gemini_model"],
    "embed_model": None,
    "extra": {"reasoning_effort": CFG.get("voice_reasoning", "none")},
}
_gemini = httpx.AsyncClient(base_url=GEMINI["base_url"], headers={"Authorization": f"Bearer {GEMINI['key']}"},
                            timeout=httpx.Timeout(45, connect=8))


_cloud_cool_until = 0.0  # Gemini said "rate limited": don't ask it again until then (straight to the local brain)


def cloud_cooling() -> bool:
    return time.time() < _cloud_cool_until


def cloud_failed(err: Exception) -> None:
    """Free tier hit its per-minute (or per-day) limit: rest the cloud brain instead of failing on every turn."""
    global _cloud_cool_until
    msg = str(err).lower()
    if "rate limit" in msg or "quota" in msg or "429" in msg:
        _cloud_cool_until = time.time() + (3600 if "day" in msg else 75)
        log.info("cloud brain rate-limited; using the local brain for %ds", int(_cloud_cool_until - time.time()))


@contextlib.contextmanager
def cloud_brain():
    use = GEMINI["key"] and CFG.get("voice_brain") == "gemini" and not cloud_cooling()
    token = _BRAIN.set("cloud" if use else None)
    try:
        yield
    finally:
        _BRAIN.reset(token)


def using_cloud() -> bool:
    return _BRAIN.get() == "cloud"


def is_local() -> bool:
    """Is THIS call going to the local model? (False inside cloud_brain(), even when the provider is ollama.)"""
    return LOCAL and not using_cloud()


def exclusive():
    return gen_lock if is_local() else contextlib.nullcontext()


# ---------------------------------------------------------------- Anthropic

if PROVIDER == "anthropic":
    import anthropic

    _claude = anthropic.AsyncAnthropic(api_key=CFG.get("anthropic_api_key") or os.environ.get("ANTHROPIC_API_KEY") or None)
    _CLAUDE_EXTRA = {
        # If a safety classifier declines, re-run on Anthropic's recommended fallback model.
        "betas": ["server-side-fallback-2026-07-01"],
        "extra_body": {"fallbacks": "default"},
    }

    def _claude_messages(history: list[dict], note: str) -> list[dict]:
        msgs = [dict(m) for m in history]
        # Cache everything up to the newest user message; the next turn reads it back cheaply.
        last = msgs[-1]
        last["content"] = [{"type": "text", "text": last["content"], "cache_control": {"type": "ephemeral"}}]
        # Per-turn context (time, recalled memories, summary) goes in a trailing system
        # message so it doesn't invalidate the cached prefix.
        if note:
            msgs.append({"role": "system", "content": note})
        return msgs

    async def _claude_stream(system: str, history: list[dict], note: str) -> AsyncIterator[str]:
        if not _claude.api_key:
            raise RuntimeError("No Anthropic API key. On the Dork PC run: python -m dork set-api-key, then restart Dork.")
        async with _claude.beta.messages.stream(
            model=CFG["claude_model"],
            max_tokens=CFG["claude_max_tokens"],
            system=system,
            messages=_claude_messages(history, note),
            output_config={"effort": CFG["claude_effort"]},
            **_CLAUDE_EXTRA,
        ) as stream:
            async for text in stream.text_stream:
                yield text
            final = await stream.get_final_message()
        if final.stop_reason == "refusal":
            yield "\n\n*(I can't help with that one.)*"
        elif final.stop_reason == "max_tokens":
            yield "\n\n*(reply cut off — hit the length limit)*"

    async def _claude_complete(system: str, prompt: str, schema: dict | None, max_tokens: int) -> str:
        kwargs = {"output_config": {"effort": "low"}}
        if schema:
            kwargs["output_config"]["format"] = {"type": "json_schema", "schema": schema}
        resp = await _claude.beta.messages.create(
            model=CFG["claude_model"],
            max_tokens=max_tokens,
            system=system,
            messages=[{"role": "user", "content": prompt}],
            **kwargs,
            **_CLAUDE_EXTRA,
        )
        if resp.stop_reason == "refusal":
            return ""
        return next((b.text for b in resp.content if b.type == "text"), "")


# ---------------------------------------------------------------- Gemini / Groq (OpenAI-compatible APIs)

COMPAT = {
    "gemini": {
        "base_url": "https://generativelanguage.googleapis.com/v1beta/openai",
        "key": CFG.get("gemini_api_key") or os.environ.get("GEMINI_API_KEY", ""),
        "model": CFG["gemini_model"],
        "background_model": CFG["gemini_background_model"],
        "embed_model": CFG["gemini_embed_model"],
        "extra": {"reasoning_effort": CFG["gemini_reasoning"]},
    },
    "groq": {
        "base_url": "https://api.groq.com/openai/v1",
        "key": CFG.get("groq_api_key") or os.environ.get("GROQ_API_KEY", ""),
        "model": CFG["groq_model"],
        "background_model": CFG["groq_model"],
        "embed_model": None,
        "extra": {},
    },
}.get(PROVIDER)

if COMPAT:
    _compat = httpx.AsyncClient(
        base_url=COMPAT["base_url"],
        headers={"Authorization": f"Bearer {COMPAT['key']}"},
        timeout=httpx.Timeout(300, connect=10),
    )


def _compat_error(resp: httpx.Response, body: str) -> RuntimeError:
    if resp.status_code == 429:
        return RuntimeError("Free-tier rate limit reached. Give it a minute and try again.")
    if resp.status_code in (401, 403) or "API key" in body:
        return RuntimeError(f"{PROVIDER} rejected the API key. Run: python -m dork set-api-key {PROVIDER}")
    return RuntimeError(f"{PROVIDER} error {resp.status_code}: {body[:300]}")


async def _compat_stream(system: str, history: list[dict], note: str) -> AsyncIterator[str]:
    if not COMPAT["key"]:
        raise RuntimeError(f"No {PROVIDER} API key. On the Dork PC run: python -m dork set-api-key {PROVIDER}")
    payload = {
        "model": COMPAT["model"],
        "messages": [{"role": "system", "content": f"{system}\n\n{note}".strip()}, *history],
        "stream": True,
        "max_tokens": CFG["cloud_max_tokens"],
        **COMPAT["extra"],
    }
    async with _compat.stream("POST", "/chat/completions", json=payload) as resp:
        if resp.status_code != 200:
            raise _compat_error(resp, (await resp.aread()).decode(errors="ignore"))
        async for line in resp.aiter_lines():
            if not line.startswith("data:"):
                continue
            data = line[5:].strip()
            if data == "[DONE]":
                break
            chunk = json.loads(data)
            choices = chunk.get("choices") or []
            if choices and (text := (choices[0].get("delta") or {}).get("content")):
                yield text


async def _compat_complete(system: str, prompt: str, schema: dict | None, max_tokens: int, cfg=None, client=None) -> str:
    cfg, client = cfg or COMPAT, client or _compat
    payload = {
        "model": cfg["background_model"],
        "messages": [{"role": "system", "content": system}, {"role": "user", "content": prompt}],
        "max_tokens": max_tokens,
        **cfg["extra"],
    }
    if schema:
        payload["response_format"] = {"type": "json_schema", "json_schema": {"name": "result", "schema": schema}}
    resp = await client.post("/chat/completions", json=payload)
    if resp.status_code == 400 and schema:
        # Model doesn't support schemas: fall back to plain JSON mode (the prompt describes the shape).
        payload["response_format"] = {"type": "json_object"}
        resp = await client.post("/chat/completions", json=payload)
    if resp.status_code != 200:
        raise _compat_error(resp, resp.text)
    return resp.json()["choices"][0]["message"].get("content") or ""


async def _compat_embed(texts: list[str]) -> list[list[float]] | None:
    if not (COMPAT["embed_model"] and COMPAT["key"]):
        return None
    try:
        payload = {"model": COMPAT["embed_model"], "input": texts}
        if PROVIDER == "gemini":
            payload["dimensions"] = CFG["gemini_embed_dims"]
        resp = await _compat.post("/embeddings", json=payload)
        if resp.status_code != 200:
            log.warning("embedding failed (%s): %s", resp.status_code, resp.text[:200])
            return None
        # Gemini omits "index" when it's 0.
        return [d["embedding"] for d in sorted(resp.json()["data"], key=lambda d: d.get("index", 0))]
    except (httpx.HTTPError, KeyError, ValueError):
        return None



# ---------------------------------------------------------------- llama.cpp server (local_engine = "llamacpp")
#
# Same local model, served by llama.cpp with speculative decoding (dork/engine.py). OpenAI-style API. The per-turn
# note goes just before the newest user message (as with Ollama) so the cached prompt prefix survives.
# Any connection failure marks the engine down and the call goes to Ollama instead.

_lcpp = httpx.AsyncClient(timeout=httpx.Timeout(600, connect=3))


def _use_lcpp() -> bool:
    return LOCAL and engine.ready


async def _engine_loading(what: str = "brain", limit: float = 300) -> None:
    """The C++ engine is still loading: wait for it rather than loading the same model in Ollama too."""
    waited = 0.0
    while engine.starting and waited < limit and not (engine.embed_ready if what == "embed" else engine.ready):
        await asyncio.sleep(0.5)
        waited += 0.5


def _lcpp_down(e: Exception) -> None:
    log.warning("llama.cpp engine unavailable (%s); using Ollama", e.__class__.__name__)
    engine.ready = False


def _lcpp_msgs(system: str, note: str, messages: list[dict]) -> list[dict]:
    out = [{"role": "system", "content": system}]
    last_user = max((i for i, m in enumerate(messages) if m["role"] == "user"), default=-1)
    for i, m in enumerate(messages):
        if i == last_user and note:
            out.append({"role": "system", "content": note})
        if m.get("tool_calls"):
            out.append({"role": "assistant", "content": m.get("content") or "", "tool_calls": [
                {"id": c["id"], "type": "function", "function": {"name": c["name"], "arguments": json.dumps(c["arguments"])}}
                for c in m["tool_calls"]]})
        elif m["role"] == "tool":
            out.append({"role": "tool", "tool_call_id": m.get("tool_call_id", ""), "content": m["content"]})
        else:
            out.append({"role": m["role"], "content": m["content"]})
    return out


async def _lcpp_step(system, note, messages, tools, required, temperature=None):
    payload = {"messages": _lcpp_msgs(system, note, messages), "stream": True, "cache_prompt": True,
               "max_tokens": CFG["ollama_max_tokens"], "temperature": 0.7 if temperature is None else temperature}
    if tools:
        payload["tools"] = [{"type": "function", "function": t} for t in tools]
        payload["tool_choice"] = "required" if required else "auto"
    parts: dict[int, dict] = {}
    async with _lcpp.stream("POST", engine.url() + "/v1/chat/completions", json=payload) as resp:
        if resp.status_code != 200:
            raise RuntimeError(f"llama.cpp error {resp.status_code}: {(await resp.aread()).decode(errors='ignore')[:300]}")
        async for line in resp.aiter_lines():
            if not line.startswith("data:"):
                continue
            data = line[5:].strip()
            if data == "[DONE]":
                break
            choices = json.loads(data).get("choices") or []
            if not choices:
                continue
            delta = choices[0].get("delta") or {}
            if text := delta.get("content"):
                yield "text", text
            for tc in delta.get("tool_calls") or []:
                p = parts.setdefault(tc.get("index", len(parts)), {"id": "", "name": "", "args": ""})
                p["id"] = tc.get("id") or p["id"]
                fn = tc.get("function") or {}
                p["name"] += fn.get("name") or ""
                p["args"] += fn.get("arguments") or ""
    if parts:
        calls = []
        for i, p in sorted(parts.items()):
            try:
                args = json.loads(p["args"] or "{}")
            except ValueError:
                args = {}
            calls.append({"id": p["id"] or f"call_{i}", "name": p["name"], "arguments": args})
        yield "tools", calls


async def _lcpp_complete(system: str, prompt: str, schema: dict | None, max_tokens: int) -> str:
    payload = {"messages": [{"role": "system", "content": system}, {"role": "user", "content": prompt}],
               "max_tokens": max_tokens, "temperature": 0.2, "cache_prompt": True}
    if schema:
        payload["response_format"] = {"type": "json_schema", "json_schema": {"schema": schema}}
    resp = await _lcpp.post(engine.url() + "/v1/chat/completions", json=payload)
    if resp.status_code != 200:
        raise RuntimeError(f"llama.cpp error {resp.status_code}: {resp.text[:300]}")
    return resp.json()["choices"][0]["message"].get("content") or ""


# ---------------------------------------------------------------- Ollama

_ollama = httpx.AsyncClient(base_url=CFG["ollama_url"], timeout=httpx.Timeout(600, connect=5))


def _ollama_options(**extra) -> dict:
    return {"num_ctx": CONTEXT_LIMIT, "temperature": 0.7, "num_thread": CFG["ollama_threads"], **extra}


async def _ollama_stream(system: str, history: list[dict], note: str) -> AsyncIterator[str]:
    await _engine_loading()
    if _use_lcpp():
        try:
            async for kind, value in _lcpp_step(system, note, history, None, False):
                if kind == "text":
                    yield value
            return
        except httpx.ConnectError as e:
            _lcpp_down(e)
    payload = {
        "model": CFG["ollama_model"],
        # Stable system prompt first, per-turn note LAST: Ollama reuses its cache for the
        # unchanged prefix, so the model doesn't re-read the whole chat on every message.
        "messages": [{"role": "system", "content": system}, *history,
                     *([{"role": "system", "content": note}] if note else [])],
        "stream": True,
        "keep_alive": CFG["ollama_keep_alive"],
        "options": _ollama_options(num_predict=CFG["ollama_max_tokens"]),
    }
    async with _ollama.stream("POST", "/api/chat", json=payload) as resp:
        if resp.status_code != 200:
            body = (await resp.aread()).decode(errors="ignore")
            raise RuntimeError(f"Ollama error {resp.status_code}: {body[:300]}")
        async for line in resp.aiter_lines():
            if not line:
                continue
            chunk = json.loads(line)
            if chunk.get("error"):
                raise RuntimeError(chunk["error"])
            if text := chunk.get("message", {}).get("content", ""):
                yield text
            if chunk.get("done"):
                break


async def _ollama_complete(system: str, prompt: str, schema: dict | None, max_tokens: int) -> str:
    await _engine_loading()
    if _use_lcpp():
        try:
            return await _lcpp_complete(system, prompt, schema, max_tokens)
        except httpx.ConnectError as e:
            _lcpp_down(e)
    payload = {
        "model": CFG["ollama_model"],
        "messages": [{"role": "system", "content": system}, {"role": "user", "content": prompt}],
        "stream": False,
        "keep_alive": CFG["ollama_keep_alive"],
        "options": _ollama_options(num_predict=max_tokens, temperature=0.2),
    }
    if schema:
        payload["format"] = schema
    resp = await _ollama.post("/api/chat", json=payload)
    resp.raise_for_status()
    return resp.json().get("message", {}).get("content", "")


# ---------------------------------------------------------------- tool-calling step (agent)
#
# Messages use one internal shape for every provider:
#   {"role": "user"|"assistant", "content": str}
#   {"role": "assistant", "content": str, "tool_calls": [{"id", "name", "arguments": dict, "extra"?}]}
#   {"role": "tool", "tool_call_id": str, "name": str, "content": str}
# chat_step streams ("text", str) pieces and finally ("tools", [calls]) if the model asked for tools.

class ToolsUnsupported(RuntimeError):
    pass


def _ollama_msgs(system: str, note: str, messages: list[dict]) -> list[dict]:
    out = [{"role": "system", "content": system}]
    # Per-turn note goes just before the newest user message: the history prefix stays cache-friendly.
    last_user = max((i for i, m in enumerate(messages) if m["role"] == "user"), default=-1)
    for i, m in enumerate(messages):
        if i == last_user and note:
            out.append({"role": "system", "content": note})
        if m.get("tool_calls"):
            out.append({"role": "assistant", "content": m.get("content") or "",
                        "tool_calls": [{"function": {"name": c["name"], "arguments": c["arguments"]}} for c in m["tool_calls"]]})
        elif m["role"] == "tool":
            out.append({"role": "tool", "content": m["content"], "tool_name": m["name"]})
        else:
            out.append({"role": m["role"], "content": m["content"]})
    return out


async def _ollama_step(system, note, messages, tools, required, temperature=None):
    await _engine_loading()
    if _use_lcpp():
        started = False
        try:
            async for item in _lcpp_step(system, note, messages, tools, required, temperature):
                started = True
                yield item
            return
        except httpx.ConnectError as e:
            if started:
                raise
            _lcpp_down(e)
    payload = {
        "model": CFG["ollama_model"],
        "messages": _ollama_msgs(system, note, messages),
        "stream": True,
        "keep_alive": CFG["ollama_keep_alive"],
        "options": _ollama_options(num_predict=CFG["ollama_max_tokens"],
                                   **({"temperature": temperature} if temperature is not None else {})),
    }
    if tools:
        payload["tools"] = [{"type": "function", "function": t} for t in tools]
    calls = []
    async with _ollama.stream("POST", "/api/chat", json=payload) as resp:
        if resp.status_code != 200:
            body = (await resp.aread()).decode(errors="ignore")
            if "does not support tools" in body:
                raise ToolsUnsupported(body[:200])
            raise RuntimeError(f"Ollama error {resp.status_code}: {body[:300]}")
        async for line in resp.aiter_lines():
            if not line:
                continue
            chunk = json.loads(line)
            if chunk.get("error"):
                raise RuntimeError(chunk["error"])
            msg = chunk.get("message", {})
            for c in msg.get("tool_calls") or []:
                fn = c.get("function", {})
                args = fn.get("arguments") or {}
                if isinstance(args, str):
                    with contextlib.suppress(ValueError):
                        args = json.loads(args)
                calls.append({"id": f"call_{len(calls)}", "name": fn.get("name", ""), "arguments": args if isinstance(args, dict) else {}})
            if text := msg.get("content", ""):
                yield "text", text
            if chunk.get("done"):
                # where the time went: tokens re-read (not cached) vs written
                pe, pc = chunk.get("prompt_eval_duration", 0) / 1e9, chunk.get("prompt_eval_count", 0)
                ed, ec = chunk.get("eval_duration", 0) / 1e9, chunk.get("eval_count", 0)
                log.info("local brain: read %d tok in %.1fs, wrote %d tok in %.1fs", pc, pe, ec, ed)
                break
    if calls:
        yield "tools", calls


def _compat_msgs(system: str, note: str, messages: list[dict]) -> list[dict]:
    out = [{"role": "system", "content": f"{system}\n\n{note}".strip()}]
    for m in messages:
        if m.get("tool_calls"):
            out.append({"role": "assistant", "content": m.get("content") or None, "tool_calls": [
                {"id": c["id"], "type": "function", "function": {"name": c["name"], "arguments": json.dumps(c["arguments"])},
                 **({"extra_content": c["extra"]} if c.get("extra") else {})}
                for c in m["tool_calls"]]})
        elif m["role"] == "tool":
            out.append({"role": "tool", "tool_call_id": m["tool_call_id"], "content": m["content"]})
        else:
            out.append({"role": m["role"], "content": m["content"]})
    return out


async def _compat_step(system, note, messages, tools, required, cfg=None, client=None):
    cfg, client = cfg or COMPAT, client or _compat
    if not cfg["key"]:
        raise RuntimeError(f"No {PROVIDER} API key. On the Dork PC run: python -m dork set-api-key {PROVIDER}")
    payload = {
        "model": cfg["model"],
        "messages": _compat_msgs(system, note, messages),
        "stream": True,
        "max_tokens": CFG["cloud_max_tokens"],
        **cfg["extra"],
    }
    if tools:
        payload["tools"] = [{"type": "function", "function": t} for t in tools]
        payload["tool_choice"] = "required" if required else "auto"
    parts: dict[int, dict] = {}
    async with client.stream("POST", "/chat/completions", json=payload) as resp:
        if resp.status_code != 200:
            raise _compat_error(resp, (await resp.aread()).decode(errors="ignore"))
        async for line in resp.aiter_lines():
            if not line.startswith("data:"):
                continue
            data = line[5:].strip()
            if data == "[DONE]":
                break
            choices = json.loads(data).get("choices") or []
            if not choices:
                continue
            delta = choices[0].get("delta") or {}
            if text := delta.get("content"):
                yield "text", text
            for tc in delta.get("tool_calls") or []:
                p = parts.setdefault(tc.get("index", len(parts)), {"id": "", "name": "", "args": "", "extra": None})
                p["id"] = tc.get("id") or p["id"]
                fn = tc.get("function") or {}
                p["name"] += fn.get("name") or ""
                p["args"] += fn.get("arguments") or ""
                p["extra"] = tc.get("extra_content") or p["extra"]  # Gemini thought signatures: echo back
    if parts:
        calls = []
        for i, p in sorted(parts.items()):
            try:
                args = json.loads(p["args"] or "{}")
            except ValueError:
                args = {}
            calls.append({"id": p["id"] or f"call_{i}", "name": p["name"], "arguments": args, "extra": p["extra"]})
        yield "tools", calls


async def chat_step(system: str, note: str, messages: list[dict], tools: list[dict] | None = None,
                    required: bool = False, temperature: float | None = None):
    """One model turn with optional tools. Yields ("text", str)… then maybe ("tools", [calls])."""
    if using_cloud():
        started = False
        try:
            async for item in _compat_step(system, note, messages, tools, required, cfg=GEMINI, client=_gemini):
                started = True
                yield item
            return
        except Exception as e:  # noqa: BLE001 — offline / quota: the local brain takes over (if nothing was said yet)
            if started:
                raise
            log.info("cloud brain unavailable (%s); using the local brain", str(e)[:120])
            cloud_failed(e)
            _BRAIN.set(None)
    if COMPAT:
        gen = _compat_step(system, note, messages, tools, required)
    elif LOCAL:
        gen = _ollama_step(system, note, messages, tools, required, temperature)
    else:
        if tools:
            raise ToolsUnsupported("Tool use with Claude isn't wired up yet.")
        gen = (("text", t) async for t in _claude_stream(system, messages, note))
    async for item in gen:
        yield item


# ---------------------------------------------------------------- public API

async def stream_chat(system: str, history: list[dict], note: str = "") -> AsyncIterator[str]:
    """Stream a reply. `system` is stable (cacheable); `note` is per-turn context."""
    if COMPAT:
        gen = _compat_stream(system, history, note)
    elif LOCAL:
        gen = _ollama_stream(system, history, note)
    else:
        gen = _claude_stream(system, history, note)
    async for text in gen:
        yield text


async def complete(system: str, prompt: str, schema: dict | None = None, max_tokens: int = 1024) -> str:
    """One-shot generation for background work. With `schema`, returns JSON matching it."""
    if using_cloud():
        try:
            return await _compat_complete(system, prompt, schema, max(max_tokens, 400), cfg=GEMINI, client=_gemini)
        except Exception as e:  # noqa: BLE001
            log.info("cloud brain unavailable (%s); using the local brain", str(e)[:120])
            cloud_failed(e)
    if COMPAT:
        return await _compat_complete(system, prompt, schema, max_tokens)
    if LOCAL:
        return await _ollama_complete(system, prompt, schema, max_tokens)
    return await _claude_complete(system, prompt, schema, max_tokens)


async def embed(texts: list[str]) -> list[list[float]] | None:
    """Embeddings for memory search. None => callers fall back to keyword matching."""
    if COMPAT:
        return await _compat_embed(texts)
    if not LOCAL:
        return None
    await _engine_loading("embed", 60)
    if engine.embed_ready:
        try:
            resp = await _lcpp.post(engine.embed_url() + "/v1/embeddings", json={"input": texts})
            if resp.status_code == 200:
                return [d["embedding"] for d in sorted(resp.json()["data"], key=lambda d: d["index"])]
        except httpx.ConnectError as e:
            log.warning("llama.cpp embeddings unavailable (%s); using Ollama", e.__class__.__name__)
            engine.embed_ready = False
    try:
        resp = await _ollama.post("/api/embed", json={"model": CFG["embed_model"], "input": texts, "keep_alive": CFG["ollama_keep_alive"]})
        return resp.json().get("embeddings") if resp.status_code == 200 else None
    except httpx.HTTPError:
        return None


async def status() -> dict:
    if COMPAT:
        base = {"provider": PROVIDER, "model": COMPAT["model"], "ready": False, "memory_search": "keyword"}
        if not COMPAT["key"]:
            return {**base, "detail": f"No API key. Run: python -m dork set-api-key {PROVIDER}"}
        try:
            resp = await _compat.get("/models")
        except httpx.HTTPError as e:
            return {**base, "detail": f"Can't reach {PROVIDER}: {e.__class__.__name__}"}
        if resp.status_code != 200:
            return {**base, "detail": str(_compat_error(resp, resp.text))}
        names = {m["id"].removeprefix("models/") for m in resp.json().get("data", [])}
        ok = COMPAT["model"] in names
        return {
            **base,
            "ready": ok,
            "detail": "connected" if ok else f"model {COMPAT['model']} not available on this key",
            "memory_search": "embeddings" if COMPAT["embed_model"] in names else "keyword",
        }
    if PROVIDER == "anthropic":
        has_key = bool(CFG.get("anthropic_api_key") or os.environ.get("ANTHROPIC_API_KEY"))
        return {
            "provider": "anthropic",
            "model": CFG["claude_model"],
            "ready": has_key,
            "detail": "API key set" if has_key else "No API key — run: python -m dork set-api-key",
            "memory_search": "keyword",
        }
    try:
        names = [m["name"] for m in (await _ollama.get("/api/tags")).json().get("models", [])]
    except httpx.HTTPError:
        return {"provider": "ollama", "model": CFG["ollama_model"], "ready": False,
                "detail": "Ollama is not running", "memory_search": "keyword"}

    def present(model: str) -> bool:
        return any(n in (model, f"{model}:latest") for n in names)

    chat_ok = present(CFG["ollama_model"])
    return {
        "provider": "ollama",
        "model": CFG["ollama_model"],
        "ready": chat_ok,
        "detail": "model ready" if chat_ok else f"run: ollama pull {CFG['ollama_model']}",
        "memory_search": "embeddings" if present(CFG["embed_model"]) else "keyword",
    }
