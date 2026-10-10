# Dork Nano 2: Voice Assistant report

Built 2026-10-06, 10:15 to 13:35, on top of your VS Code voice work (`voice.py`, voices, Voice tab). None of that was replaced.
Backup of everything before these changes: `backups/pre-nano2-assistant/`.

## What Dork can do now

**1. Lives in the background and wakes by voice.**
The Dork server now listens on this PC's microphone all the time.
- **Wake it naturally:** "Dork, wake up", "hey Dork, you there?", "wake up buddy". It understands what you mean (there's no list of fixed phrases), and still wakes if speech-to-text mishears the name as "Doc" or "Dorg".
- **Talking about Dork doesn't wake it:** "he's such a dork", "it's getting dark" and "I left the door open" are all ignored.
- **When it wakes,** the reactor window jumps to the front, greets you and listens.
- **It goes back to sleep** when you say "that's all" or "go to sleep", or after 40 seconds of quiet.
- **Hotkey:** Ctrl+Shift+Space shows or hides the reactor.

**2. A voice-only reactor screen.** No chat, just the 3D arc reactor (Three.js) and captions.
- **Visual states:** standby (dim amber), waking (power-up burst), listening (reacts to your voice), thinking (scanning sweep) and speaking (pulses with Dork's voice).
- **Where to open it:** `http://localhost:7777/assistant`, or on your phone through Tailscale (`/assistant`, then tap the mic).

**3. Voice commands, decided by the brain** (no keyword lists).
- **Apps:** "open VS Code / Chrome / Calculator" (it finds any of your 154 installed apps). If the app is already open, Dork brings it to the front instead.
- **Windows:** "close Chrome", "close this", "minimise that", "maximise the calculator", "switch to Notepad".
- **Websites and folders:** "open YouTube", "open my Downloads" (it finds your real Downloads folder at `F:\Downloads`).
- **Media:** "volume up", "pause the music", "next track".
- **Timers:** "remind me in 10 minutes to call mom". Dork speaks when the time is up.
- **PC status:** "how's my battery?" is answered instantly from the PC's sensors, with no web search.
- **Dork's screens:** "open chat" opens the chat window on the same voice conversation. "Open settings" and "show my memories" also work.
- **Everything else** is a normal conversation in character, with memories, your profile and web lookups when needed.

**4. It knows your voice (voiceprints).**
- **Set up your voice:** say "Dork, learn my voice", then read 6 short phrases.
- **Other people:** "this is my sister Priya, learn her voice" adds her as a guest. Dork then knows who's talking and addresses that person.
- **Once you're enrolled, only your voice can** wake Dork from the background and control the PC (open, close, timers and so on). Other people get "I only take that kind of order from Hari."
- **Guard mode:** "Dork, lock yourself". To wake it again, Dork asks you to repeat 3 random words, and checks both the words and your voiceprint. That stops someone replaying a recording of you.
- **Voice sign-in limits:** a voice match will **never** change the master key, delete data or approve new devices. Those still need the master key or a device code.
- **Privacy:** voiceprints and the log of voice sign-in attempts stay in `data/dork.db` on this PC only.

**5. It doesn't listen to itself.**
- **The problem:** during testing, Dork heard its own replies through the mic and acted on them. It even set a timer for "sync".
- **The fix, in three layers:**
  - Dork learned a voiceprint of its own voice and ignores it.
  - Anything said while Dork's voice was playing is ignored, unless it's you interrupting.
  - A phrase matching something Dork just said is ignored.

**6. Offline speech-to-text.** The chat mic used to need Gemini. It now falls back to Whisper on this PC when Gemini is out of quota or offline.

## Test results

| Test | Result |
|---|---|
| Who is speaking: 6 different voices, enroll on 4 phrases, test on 4 new ones | **24/24 correct**, 0 mix-ups |
| Your-voice check at threshold 0.6 | 0 false accepts (out of 20), 0 false rejects (out of 4). Owner ≥ 0.83, others ≤ 0.46 |
| People taking turns (4 people, 5 phrases) | Both of your phrases picked out, nobody else mistaken for you |
| You plus 1 other person talking **at the same moment** | 4/4 recognised |
| You plus 2–3 people all talking at once | 0–2 out of 4 (**known limit**, see below) |
| Crowds of 2–4 *other* people (impostor check) | **0/24** mistaken for you |
| Wake detection (10 wake calls + 10 near-misses) | **20/20**, 0.3 s each |
| Brain picks the right command (38 phrases, 9 never seen before) | **35/38**, then fixed: "pause" → media key, "bring up X" opens X when it isn't open |
| Live on your PC (typed and recorded phrases) | **14/15**, then fixed: Downloads folder path |
| **Spoken through the speakers into the real mic** | Wake → greeting → "open Notepad" (opened) → "close the Notepad window" (closed) → "go to sleep" (hidden): **all passed** |
| Echo: Dork answers out loud while the mic listens | No phantom commands |
| Transcription | About 1 s per phrase (base.en, CPU) |
| Web search, 4 real queries | 21–29 s each (was 28–37 s): sends only the best 40 passages to the meaning-ranking step |

Test scripts are in `tests/`. Run them with `.venv\Scripts\python.exe tests\test_hearing.py`, and the same for the other `test_*.py` files.
The voices in the tests were Microsoft neural voices standing in for different people. **Your real voice still has to be enrolled by you.**

## What you need to do

1. **Install the new desktop app:** run `desktop\dist\Dork Setup 2.1.0.exe`. It replaces 2.0.0, starts with Windows hidden in the tray, and holds the reactor window.
   Until you install it, a test copy of the new app (started from the `desktop` folder) is running hidden on this PC, so the reactor already pops up on wake.
2. **Enroll your voice:** say "Dork, wake up", then "learn my voice", and read the 6 phrases. Do it in a normal room, at the distance you usually sit from the laptop.
3. **Test with your own voice:** wake it from across the room, try "close this" on a real window, and check that a friend's voice is rejected after you enroll.
4. If it misses your voice too often, lower `voice_match_threshold` in `data/config.json` (default 0.6, try 0.55). If someone else gets through, raise it.

## Known limits (honest)

- **Speed is the main weakness.** The local brain (qwen2.5:3b on CPU) takes about 4–6 s to decide on a command. Small talk takes about 15–40 s, and a web lookup about a minute.
  - Your laptop has 12 GB of RAM with about 1 GB free while everything runs, and a background memory job can hold the brain for up to a minute. In one run, a command waited 47 s.
  - **Switching `provider` to `gemini`** (free key already set) would make voice much snappier. I didn't change it without asking.
- **Overlapping voices:** if 2–3 people talk at exactly the same moment as you, Dork may miss your phrase. It won't act on theirs, though. Normal turn-taking works perfectly.
- **Speakers into the mic:** in one test, the laptop's own speaker-to-mic audio was misheard ("Dork, wake up" became "We'll be back for a week"). A real voice in the room is clearer, but a noisy room will hurt.
- **Timers live in memory:** they're lost if Dork restarts.
- **Learning from voice chats:** voice conversations go into the "Voice" thread, but aren't yet reviewed for long-term memories the way typed chats are.
- **Web search:** your VS Code session had already fixed Wikipedia, dates in queries, and the reading time cap. Its search engine is deliberately designed without topic routing, so I left that alone and only reduced the ranking cost.

## Files

- **New:** `dork/assistant.py` (ears, wake, commands, enrollment, guard mode), `dork/hearing.py` (voice detection, voiceprints, Whisper), `dork/desktop.py` (opening and closing apps and windows), `web/assistant.html`, `assistant.js`, `assistant.css`, `reactor.js`, `web/vendor/three/` (Three.js, stored locally so it works offline), `desktop/preload.js`, `tests/*`.
- **Changed:**
  - `dork/server.py`: assistant, voiceprint and progress endpoints, plus offline speech-to-text fallback.
  - `dork/config.py`: new assistant settings.
  - `web/app.js`: opens the voice thread or settings from the reactor.
  - `dork/websearch/engine.py`: embedding cap.
  - `desktop/main.js` and `package.json`: hidden assistant window, start with Windows, version 2.1.0.
- **Models** (all in `models/`, all on F:):
  - Whisper base.en (141 MB)
  - 3D-Speaker voiceprint model (40 MB)
  - Silero voice-activity model (0.6 MB)
- **Installed Python packages:** edge-tts, faster-whisper, sherpa-onnx, sounddevice.

## Next steps (suggested)

1. Enroll your voice, then tune the threshold.
2. Decide on Gemini versus local speed for voice.
3. Run the memory review on voice conversations.
4. Save timers to the database.
5. Add a "what's on my screen" description using window titles (the data is already collected in `desktop.describe()`).

## Update: speed and experience tuning (afternoon, 2026-10-06)

| | Before | After |
|---|---|---|
| Everyday commands (volume, mute, time, battery, open/close/minimise apps) | ~10 s | **0.1–0.6 s** (fast path) |
| Conversation (local brain) | 20–75 s | **~10–14 s**, with "Hmm / give me a sec" after 2.5 s |
| First conversation after a restart | ~40 s | ~14 s (cache warmed at startup) |
| Hidden desktop app CPU use | ~17% | **~2%** (reactor stops drawing while hidden) |

**How it works:**
- **Fast path** (`assistant.fast_intent`): a meaning match against example phrasings, plus a real app or window found in what you said. Anything unclear goes to the brain. Tests: 32/34 correct, and every look-alike ("don't open chrome", "how's it going", "close the deal tomorrow") correctly went to the brain. `tests/test_fast.py`.
- **One brain call per voice turn:** the command step also writes the spoken reply. Prompts put stable parts first so the cache stays warm.
- **8 CPU threads** for Ollama (`ollama_threads`, measured 35% faster writing).
- **Background memory jobs wait** while you're talking to Dork.
- **Listening rule:** after Dork answers, the PC mic takes follow-ups for 15 s. After that it needs "Dork, ..." again. Song lyrics and repetitive transcripts are ignored. Talking from the phone doesn't arm the PC mic. This fixes Dork replying to a song or video playing nearby, which happened during testing.
- **Tone:** filler like "How can I assist you today?" is removed from replies, in voice and chat.

**Local brain comparison** (`tests/compare_models.py`):

| | Commands | Chat questions | Speed |
|---|---|---|---|
| qwen2.5 1.5B | 28/38 | 10/12, garbled Tamil | 2x faster |
| **qwen2.5 3B** | **33/38** | **12/12** | baseline |

Decision: **kept the 3B.** The 1.5B model stays downloaded; remove it with `ollama rm qwen2.5:1.5b`.

**Why it's still around 10 s for conversation:** the CPU writes only about 5–6 tokens per second with this model. Faster needs a GPU or a cloud brain; the Gemini hybrid was measured at about 2 s.

## Update: speed research and streaming (2026-10-08)

**Measured on this laptop** (writing speed of the 3B brain):

| Technique | Writing speed | Kept? |
|---|---|---|
| CPU, Ollama | 9.6 tok/s | ✅ baseline |
| Intel UHD GPU (Vulkan, `OLLAMA_VULKAN=1` + `OLLAMA_IGPU_ENABLE=1`) | 4.3 tok/s | ❌ slower |
| Speculative decoding (llama.cpp + 0.5B draft) | 11.4 tok/s in long text | ❌ in real voice turns, no measurable gain (2.0–3.1 s vs 2.2–3.1 s to first words). Code kept: `local_engine: "llamacpp"` (dork/engine.py) |
| TensorRT / CUDA / IPEX-LLM | n/a | ❌ needs an NVIDIA or newer Intel GPU |

The limit is memory bandwidth: mismatched 8 GB + 4 GB DDR4-2667 sticks, with part of the memory running single-channel. Cheapest hardware win: a matching 8 GB stick. Free win, which you set yourself: Windows power mode "Best performance" when plugged in (it's currently on Balanced).

**What made the real difference: streaming.** The brain now writes `ACTION / TARGET / NUMBER / SAY` as plain text. Dork acts as soon as the action is known (≈2 s) and speaks each sentence as soon as it's written.

| | Before | Now |
|---|---|---|
| Conversation, time to first words | ~10 s | **~2 s with Gemini, ~2–3 s on the local brain** |
| PC commands via the brain | ~10 s | **~2 s** |
| Everyday commands (instant path) | 0.1–0.6 s | same, now including timers ("remind me in 5 minutes to…") |

**Also fixed:**
- Gemini rate limits: Dork now rests Gemini for ~75 s (or 1 h on a daily limit) instead of failing and waiting on every turn. Before, a turn could lose up to 8 s to `natural()` timeouts.
- Typed or spoken commands from a signed-in device (your phone) are trusted again. A device paired with a code is stronger proof than a voice match.
- The decision step uses a lower temperature (0.2), so actions are decided, not guessed.

## Update: deeper optimization research (2026-10-08 evening)

All measured on this laptop (i5-1035G1, 8+4 GB DDR4-2667, Intel UHD):

| Idea | Result | Kept |
|---|---|---|
| Intel iGPU (Vulkan) | 2x slower | ❌ |
| Speculative decoding (0.5B draft) | +19% in long text, ~0 in voice turns | ❌ (code kept, off) |
| Flash attention + q8 KV cache | slower reading, slower writing | ❌ |
| Q4_0 / IQ4_NL formats (AVX-512 repacked) | ±10%, within noise | ❌ |
| IQ4_XS format | +17% writing, +34% reading in llama.cpp; **lost accuracy** (31/38 vs 35/38 commands, Tamil wrong) and no gain inside Ollama | ❌ |
| Compact reply header (fewer tokens before speaking) | the 3B copied the examples / said "chat" | ❌ reverted |
| Custom-compiled engine | Ollama already ships the Ice Lake AVX-512 build (`ggml-cpu-icelake`) | — |
| **Streaming replies + cache-friendly prompt** | **first words ~2.5–3.6 s on the local brain (8 s on the first turn)** | ✅ |
| **Instant path for commands** | **0.1–1.3 s** | ✅ |

**Conclusion:** on this hardware the 3B brain writes ~10–12 tokens/s, and no software trick changed that meaningfully without losing accuracy. What still works is writing *less* before speaking (done) and handling everyday commands without the brain (done). Beyond that, it takes hardware: a matching RAM stick, or any GPU.

**Also fixed:**
- Internal notes no longer get read aloud.
- Questions about earlier reminders are answered from memory.
- Format-slip lines are never spoken.
- Small talk no longer triggers web searches (a meaning check runs first).
- Gemini transcription shares the rate-limit rest.
- The local brain logs its read/write timing per call ("local brain: read … wrote …" in dork.log).

## Update: everything through C++, streamlined (2026-10-08 night)

- **All local model calls go through llama.cpp's C++ servers** (`local_engine: "llamacpp"`): the brain on port 8091, embeddings on port 8092 (vectors identical to Ollama's, similarity 1.0000). Ollama stays as the automatic fallback and keeps no models loaded.
- **C++ RAM-bandwidth test** (`tests/cpp/membw.cpp`): 26.9 GB/s, so the hard ceiling for the 3B brain is about 14 tokens/s. The engine already reaches 70–87% of it. A custom engine can't do much better; more speed needs hardware.
- **Gemini for voice:** Flash-Lite (`voice_gemini_model`), one call per turn. The extra phrasing call is off (`voice_natural: false`). Gemini transcription shares the rate-limit rest.
- **Hearing in parallel:** the voiceprint and Whisper run at the same time. The slow overlap scan only runs for the always-on PC microphone while it's waiting for "Dork".

**Spoken conversation, measured from speech arriving to Dork starting to talk:**

| | Before | Now |
|---|---|---|
| Conversation (Gemini Flash-Lite) | 6–12 s | **2.3–2.5 s** |
| Commands (time, volume, timers, goodbye) | 1–5 s | **0.9 s** |
| Conversation offline (C++ local brain) | 7–14 s | **3.5–4.6 s** |
| First turn after a restart | 7–23 s | 7–23 s (cold cache) |

## Update: custom C++ build of llama.cpp (2026-10-09)

Built llama.cpp v0.6.0 from source with GCC 16 (w64devkit) + CMake 4.4.4 in `F:\tools`, `-march=native`, LTO. A/B on the same model:

| Build | Writing | Reading |
|---|---|---|
| **Ollama's bundled llama.cpp server** (Clang 18, Ice Lake AVX-512 variant), used by Dork's engine | **10.6–10.9 tok/s** | **57–58 tok/s** |
| Custom GCC, AVX-512 + OpenMP | 5.4 | 51–53 |
| Custom GCC, AVX-512, ggml threads | 7.9 | n/a |
| Custom GCC, AVX2 only | 8.8 | 34 |

Conclusion: the C++ binary Dork already runs is the best-optimised build for this CPU. GCC on Windows generates slower AVX-512 code; Clang (what Ollama uses) is what llama.cpp recommends. TensorRT needs an NVIDIA GPU (none here). ONNX: the hearing models already run on C++ runtimes (ONNX Runtime via sherpa-onnx, CTranslate2 for Whisper). ONNX GenAI for the brain would need a ~2 GB model download, and published numbers (~8 tok/s) are below llama.cpp.

## Update: startup (2026-10-09 morning)

The morning cold start was slow (6 min until the C++ brain was ready) because:
- **Two Dorks launched at login**: the Startup folder (`Dork.vbs`) and the desktop app (`Dork.exe --hidden`, which starts the server if it isn't up yet).
- **Background jobs ran before the C++ brain was ready**, so they loaded a second copy of the brain in Ollama.

Fixes:
- **Single instance:** `python -m dork` exits immediately if a Dork is already listening on the port.
- **The C++ engine starts first,** in its own thread, before the background jobs are queued.
- **No Ollama fallback while loading:** local brain calls and embeddings wait for the engine (`engine.starting`) instead of loading the same models in Ollama.

**Measured with a cold double launch:** the C++ brain is serving after **38 s**, Dork is fully warm after ~2 min, there's one Dork process, and Ollama has nothing loaded.

**Your choice, not changed:** "Ollama app" (the tray app, ~700 MB) also starts at login from the Startup folder (`Ollama.lnk`). Dork no longer needs it running: `start-dork.vbs` starts the Ollama server itself as the backup. Removing that shortcut would free RAM at login.
