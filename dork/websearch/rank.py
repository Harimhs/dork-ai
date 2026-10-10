"""Scoring: relevance (meaning + keywords), freshness and agreement between sources, then a
diversity pass so near-duplicate passages don't crowd out other sources."""

import math
import re
from datetime import datetime, timezone
from urllib.parse import urlparse

WEIGHTS = {"semantic": 0.45, "bm25": 0.30, "fresh": 0.15, "agree": 0.10}

# Half-life (days) per freshness class; None = age doesn't matter.
HALF_LIFE = {"breaking": 2, "recent": 30, "general": 365, "timeless": None}

STOP = set("""a an the and or but if of to in on at for from by with about as is are was were be been being this that
these those it its i me my you your we our they them their what which who whom when where why how do does did can
could should would will shall may might not no yes so than then there here just also very more most some any all
please tell show give find search look up dork""".split())


def tokens(text: str) -> list[str]:
    return [w for w in re.findall(r"[a-z0-9]+", text.lower()) if w not in STOP and len(w) > 1]


def bm25(query: str, docs: list[str], k1: float = 1.4, b: float = 0.75) -> list[float]:
    q = set(tokens(query))
    toks = [tokens(d) for d in docs]
    if not q or not toks:
        return [0.0] * len(docs)
    n = len(toks)
    avg = sum(len(t) for t in toks) / n or 1
    df = {w: sum(1 for t in toks if w in t) for w in q}
    scores = []
    for t in toks:
        tf = {}
        for w in t:
            if w in q:
                tf[w] = tf.get(w, 0) + 1
        s = 0.0
        for w, f in tf.items():
            idf = math.log(1 + (n - df[w] + 0.5) / (df[w] + 0.5))
            s += idf * f * (k1 + 1) / (f + k1 * (1 - b + b * len(t) / avg))
        scores.append(s)
    top = max(scores) or 1
    return [s / top for s in scores]


def freshness(published: datetime | None, klass: str) -> float:
    half = HALF_LIFE.get(klass)
    if half is None:
        return 1.0
    if not published:
        return 0.35  # unknown date: below average, a dated source wins ties for time-sensitive questions
    age_days = max(0.0, (datetime.now(timezone.utc) - published).total_seconds() / 86400)
    return math.exp(-age_days * math.log(2) / half)


def agreement(hits: int) -> float:
    """How many sources/queries independently returned this page: 1 → 0, 2 → 0.5, 3+ → 1.
    Replaces any hand-made list of 'trusted' sites."""
    return min(1.0, max(0, hits - 1) / 2)


def cosine(a: list[float], b: list[float]) -> float:
    if not a or not b or len(a) != len(b):
        return 0.0
    dot = sum(x * y for x, y in zip(a, b))
    na = math.sqrt(sum(x * x for x in a))
    nb = math.sqrt(sum(y * y for y in b))
    return dot / (na * nb) if na and nb else 0.0


def jaccard(a: str, b: str) -> float:
    sa, sb = set(tokens(a)), set(tokens(b))
    return len(sa & sb) / len(sa | sb) if sa and sb else 0.0


def score(items: list[dict], query: str, klass: str, qvec: list[float] | None) -> None:
    """Adds item['score'] (0-1) to each {text, url, published, vec?}."""
    kw = bm25(query, [it["text"] for it in items])
    sem_ok = qvec is not None and all(it.get("vec") for it in items)
    w = dict(WEIGHTS)
    if not sem_ok:  # no embeddings: shift that weight onto keywords
        w["bm25"] += w.pop("semantic")
    if HALF_LIFE.get(klass) is None:  # timeless: freshness says nothing, give it to relevance
        w["bm25"] += w.pop("fresh")
    sems = []
    if sem_ok:
        raw = [cosine(qvec, it["vec"]) for it in items]
        lo, hi = min(raw), max(raw)
        sems = [(r - lo) / (hi - lo) if hi > lo else 0.5 for r in raw]
    for i, it in enumerate(items):
        s = w["bm25"] * kw[i] + w["agree"] * agreement(it.get("hits", 1))
        if "semantic" in w:
            s += w["semantic"] * sems[i]
        if "fresh" in w:
            s += w["fresh"] * freshness(it.get("published"), klass)
        it["score"] = s


def diverse_top(items: list[dict], k: int, char_budget: int, per_url: int = 2, lam: float = 0.75) -> list[dict]:
    """Maximal-marginal-relevance pick: high score, low overlap with what's already chosen."""
    pool = sorted(items, key=lambda it: it["score"], reverse=True)[: k * 6]
    chosen, used, per = [], 0, {}
    while pool and len(chosen) < k:
        def mmr(it):
            sim = max((cosine(it["vec"], c["vec"]) if it.get("vec") and c.get("vec") else jaccard(it["text"], c["text"])
                       for c in chosen), default=0.0)
            return lam * it["score"] - (1 - lam) * sim
        best = max(pool, key=mmr)
        pool.remove(best)
        if per.get(best["url"], 0) >= per_url or used + len(best["text"]) > char_budget:
            continue
        if chosen and max(jaccard(best["text"], c["text"]) for c in chosen) > 0.6:
            continue  # near-duplicate (syndicated copy)
        chosen.append(best)
        per[best["url"]] = per.get(best["url"], 0) + 1
        used += len(best["text"])
    return chosen
