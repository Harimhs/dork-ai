"""Offline hearing tests with synthetic speakers (edge-tts voices stand in for different people).
Run: .venv/Scripts/python.exe tests/test_hearing.py"""
import glob, os, sys, time
import numpy as np
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
os.environ["DORK_DATA"] = os.path.join(os.path.dirname(__file__), "_data")  # throwaway DB, not Hari's
from dork import hearing

clips = {}
for p in sorted(glob.glob("models/testvoices/*.mp3")):
    v, i = os.path.basename(p)[:-4].rsplit("_", 1)
    clips[(v, int(i))] = hearing.decode(open(p, "rb").read())
voices = sorted({v for v, _ in clips})
owner, others = voices[0], voices[1:]
print("voices:", voices, "owner stand-in:", owner)

# 1. enroll everyone from 4 clips, test on the other 4
for v in voices:
    hearing.enroll(v, [clips[(v, i)] for i in range(4)], "owner" if v == owner else "guest")
ok = tot = fa = 0
scores_same, scores_other = [], []
t = time.time()
for v in voices:
    for i in range(4, 8):
        r = hearing.identify(clips[(v, i)])
        tot += 1; ok += r["name"] == v
        if r["name"] and r["name"] != v: fa += 1
print(f"identify: {ok}/{tot} correct, {fa} wrong-person, {(time.time()-t)/tot*1000:.0f} ms each")

# 2. owner gate: owner vs everyone else at the configured threshold
th = hearing.CFG["voice_match_threshold"]
own = hearing.owner()["vec"]
for v in voices:
    for i in range(4, 8):
        e = hearing.embed(clips[(v, i)]); s = float(e @ own)
        (scores_same if v == owner else scores_other).append(s)
fr = sum(s < th for s in scores_same); fa2 = sum(s >= th for s in scores_other)
print(f"owner gate @ {th}: false rejects {fr}/{len(scores_same)}, false accepts {fa2}/{len(scores_other)}; "
      f"owner min {min(scores_same):.2f}, others max {max(scores_other):.2f}")

# 3. crowd: owner talking while 1-3 others talk at the same time (mixed), and others talking in turns
def mix(*xs, gains=None):
    n = max(len(x) for x in xs); out = np.zeros(n, np.float32)
    for x, g in zip(xs, gains or [1] * len(xs)):
        out[:len(x)] += x * g
    return out / max(1e-6, np.abs(out).max()) * 0.9
for k in (1, 2, 3):
    hits = 0
    for i in range(4, 8):
        m = mix(clips[(owner, i)], *[clips[(o, (i + j) % 8)] for j, o in enumerate(others[:k])], gains=[1] + [0.5] * k)
        hits += hearing.identify(m)["name"] == owner
    print(f"owner + {k} quieter voice(s) at once (overlap): recognised {hits}/4")
for k in (1, 2, 3):
    hits = 0
    for i in range(4, 8):
        m = mix(clips[(owner, i)], *[clips[(o, (i + j) % 8)] for j, o in enumerate(others[:k])], gains=[1] + [1.0] * k)
        hits += hearing.identify(m)["name"] == owner
    print(f"owner + {k} EQUALLY loud voice(s) at once (overlap): recognised {hits}/4")
# turn-taking: conversation where people alternate; VAD splits phrases, each is identified separately
gap = np.zeros(int(16000 * 0.9), np.float32)
convo = [clips[(owner, 4)], clips[(others[0], 5)], clips[(others[1], 6)], clips[(owner, 7)], clips[(others[2], 4)]]
truth = [owner, others[0], others[1], owner, others[2]]
segs = hearing.speech_segments(np.concatenate([x for c in convo for x in (c, gap)]))
got = [hearing.identify(s)["name"] for s in segs]
print(f"turn-taking, 5 phrases by 4 people: VAD found {len(segs)} segments; owner phrases picked out: "
      f"{sum(1 for g in got if g == owner)}/2; others mistaken for owner: {sum(1 for g, s in zip(got, segs) if g == owner) - min(2, sum(1 for g in got if g == owner))}")
print("  speakers per segment:", got)

# 4. transcription speed + accuracy
t = time.time(); txt = hearing.transcribe(clips[(owner, 0)]); dt = time.time() - t
t = time.time(); txt2 = hearing.transcribe(clips[(others[2], 4)]); dt2 = time.time() - t
print(f"whisper: {txt!r} ({dt:.2f}s, first call incl. load) / {txt2!r} ({dt2:.2f}s)")

# 5. impostor check for the window search: crowds WITHOUT the owner must not be recognised as the owner
fa = 0; n = 0
for k in (2, 3, 4):
    for i in range(8):
        m = mix(*[clips[(o, (i + j) % 8)] for j, o in enumerate(others[:k])])
        n += 1; fa += hearing.identify(m)["role"] == "owner"
print(f"crowds of 2-4 OTHER people talking at once: mistaken for owner {fa}/{n}")
