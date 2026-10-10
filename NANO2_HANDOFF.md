# Nano 2 (voice) — handoff notes

Work stopped early on 2026-10-05, about 20:45. **No Dork source files have been changed yet.**

## Done
- Installed in `.venv`: `edge-tts` (free Microsoft neural voices) and `faster-whisper` (offline speech-to-text). pip downloads work now.
- edge-tts works. The test clip `models/test.mp3` (en-GB-RyanNeural) was saved; play it to hear the voice.
- **The Whisper model download did NOT finish.** The large `model.bin` file is 0 bytes and still marked `.incomplete`, probably because huggingface.co is blocked by the same DNS issue. Before using faster-whisper, delete `models/whisper` and download it again, outside the sandbox and with `HF_HUB_DISABLE_SYMLINKS_WARNING=1`.
- Disabled the scheduled task "Dork Nano 2 — Voice", so it won't run on Oct 6.

## Findings (web search)
1. **Wikipedia has failed with 403 on every search.** Wikimedia now requires a contact URL in the User-Agent. Tested working value: `DorkAssistant/2.0 (https://github.com/harimhs/dork)`. Change `WIKI_UA` in `dork/websearch/sources.py`.
2. **The router never picks a topic,** so every search uses the `general` route and news questions never reach Bing News. Add `topic` (news/tech/fact/general) to `ROUTE_SCHEMA` in `dork/agent.py` and pass it on as `args["topic"]`.
3. **The router adds today's date to queries** (for example "droidal today 05 October 2026"), which hurts results. Strip dates and "today" from the query afterwards.
4. **Searches take 6 to 35 seconds.** Cap the whole page-reading step at about 10 seconds in `engine.search()` (use `asyncio.wait` with a timeout and keep whatever finished). Currently each page gets 18 seconds and 2 attempts.

## Voice plan
- **Voice output:** a server endpoint `/api/voice/tts` using edge-tts with one fixed character voice (for example en-GB-RyanNeural), so Dork sounds the same on every device. Fall back to the browser's `speechSynthesis`.
- **Voice input:** the browser's `SpeechRecognition` works in Edge, Chrome and on Android. It does **not** work in Electron (it fails with a "network" error), so the desktop app needs a fallback: record with `MediaRecorder`, send to `/api/voice/stt`, transcribe locally with faster-whisper (`base.en`, int8, CPU).
- **Electron:** allow microphone access with `session.setPermissionRequestHandler` in `desktop/main.js`.
- **Character:** add a voice-mode flag to `ChatBody`. When it's on, add an instruction for short, spoken replies with no markdown. Strengthen `PERSONA` in `dork/context.py`.
- **UI:** a reactor (canvas or SVG) driven by an `AnalyserNode` on mic input and audio output, with idle, listening, thinking and speaking states. Use SVG icons, no emojis.
- **Version:** set `VERSION = "nano-2"` in `dork/__init__.py`.
