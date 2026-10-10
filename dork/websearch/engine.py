"""Dork Search: gather → pre-rank → read → rank. Called by the agent's tools (dork/tools.py);
the brain decides *when* to search and *what* to search for — there are no trigger rules here.

Self-contained — no search API keys, no single provider."""

import asyncio
import json
import logging
import re
import time
from datetime import datetime, timezone
from urllib.parse import urlparse, urlunparse, parse_qsl, urlencode

import httpx

from .. import db, llm
from . import browser, rank
from .extract import extract, passages
from .sources import SOURCES, Candidate, client

log = logging.getLogger("dork.search")

db.x("""CREATE TABLE IF NOT EXISTS web_cache (key TEXT PRIMARY KEY, value TEXT, expires REAL)""")

# ---------------------------------------------------------------- cache

def _cache_get(key: str):
    row = db.one("SELECT value, expires FROM web_cache WHERE key=?", (key,))
    return json.loads(row["value"]) if row and row["expires"] > time.time() else None


def _cache_put(key: str, value, ttl: float) -> None:
    db.x("INSERT OR REPLACE INTO web_cache(key, value, expires) VALUES(?,?,?)", (key, json.dumps(value), time.time() + ttl))


def _purge_cache() -> None:
    db.x("DELETE FROM web_cache WHERE expires < ?", (time.time(),))


def _to_json(c: Candidate) -> dict:
    d = dict(c.__dict__)
    d["published"] = c.published.isoformat() if c.published else None
    return d


def _from_json(d: dict) -> Candidate:
    d = dict(d)
    d["published"] = datetime.fromisoformat(d["published"]) if d.get("published") else None
    return Candidate(**d)


# ---------------------------------------------------------------- gather / read

def normalize(url: str) -> str:
    p = urlparse(url)
    query = urlencode([(k, v) for k, v in parse_qsl(p.query) if not k.lower().startswith(("utm_", "fbclid", "gclid", "ref"))])
    host = p.netloc.lower().removeprefix("www.")
    return urlunparse((p.scheme.lower() or "https", host, p.path.rstrip("/") or "/", "", query, ""))


_net = asyncio.Semaphore(4)  # flaky DNS/networks cope better with a few connections at a time


async def _retry(make, attempts: int = 3, timeout: float = 9):
    """Run a network call, retrying connection failures (common on patchy Wi-Fi/DNS)."""
    for i in range(attempts):
        try:
            async with _net:
                return await asyncio.wait_for(make(), timeout=timeout)
        except (httpx.ConnectError, httpx.ConnectTimeout, httpx.RemoteProtocolError):
            if i == attempts - 1:
                raise
            await asyncio.sleep(0.6 * (i + 1))


async def _ask(http, name: str, query: str, klass: str) -> list[Candidate]:
    key = f"s:{name}:{klass}:{query.lower()}"
    if (hit := _cache_get(key)) is not None:
        return [_from_json(d) for d in hit]
    try:
        found = await _retry(lambda: SOURCES[name](http, query, klass))
    except Exception as e:  # noqa: BLE001 — one source failing must not stop the search
        log.info("source %s failed: %s", name, e.__class__.__name__)
        return []
    _cache_put(key, [_to_json(c) for c in found], 1800 if klass in ("breaking", "recent") else 86400)
    return found


async def _read(http, c: Candidate) -> dict:
    """Fetch + extract one page (cached). Falls back to the search snippet."""
    if c.text or not c.fetchable:
        return {"title": c.title, "text": c.text or f"{c.title}. {c.snippet}", "published": c.published}
    key = f"p:{normalize(c.url)}"
    if (hit := _cache_get(key)) is not None:
        hit["published"] = datetime.fromisoformat(hit["published"]) if hit["published"] else None
        return hit
    page = {"title": c.title, "text": c.snippet, "published": c.published}

    async def fetch():
        async with http.stream("GET", c.url) as resp:
            ctype = resp.headers.get("content-type", "")
            if resp.status_code != 200 or "html" not in ctype:
                return None
            body = b""
            async for chunk in resp.aiter_bytes():
                body += chunk
                if len(body) > 1_500_000:
                    break
            return extract(body.decode(resp.encoding or "utf-8", errors="ignore"), str(resp.url))

    try:
        got = await _retry(fetch, attempts=1, timeout=8)
        if got and len(got["text"]) > 200:
            page = {"title": got["title"] or c.title, "text": got["text"], "published": got["published"] or c.published,
                    "article": got.get("article")}
            # Cache only successes: a timeout on a slow network must not hide the page for hours.
            _cache_put(key, {**page, "published": page["published"].isoformat() if page["published"] else None}, 6 * 3600)
    except Exception as e:  # noqa: BLE001
        log.info("read failed %s: %s", c.url[:80], e.__class__.__name__)
    return page


def age_text(d: datetime | None) -> str:
    if not d:
        return "date unknown"
    s = (datetime.now(timezone.utc) - d).total_seconds()
    if s < 3600:
        return f"{max(1, int(s // 60))} min ago"
    if s < 86400:
        return f"{int(s // 3600)} h ago"
    if s < 86400 * 45:
        return f"{int(s // 86400)} days ago"
    return d.strftime("%b %Y")


# ---------------------------------------------------------------- public: used by the agent's tools

def _site(url: str) -> str:
    return urlparse(url).netloc.lower().removeprefix("www.")


# Words about *when*, not *what*: they belong in the freshness setting, not in the relevance check.
DATE_WORDS = re.compile(r"^(today|tonight|now|latest|recent|news|update[sd]?|new|this|week|month|year|\d{1,4}|"
                        r"jan(uary)?|feb(ruary)?|mar(ch)?|apr(il)?|may|june?|july?|aug(ust)?|sep(t(ember)?)?|"
                        r"oct(ober)?|nov(ember)?|dec(ember)?)$", re.I)


async def _read_pages(http, cands: list[Candidate], budget: float = 12) -> list[dict]:
    """Read pages: real browser first (renders JavaScript), plain fetch for whatever it couldn't load.
    The whole step is capped at `budget` seconds; slow pages fall back to their search snippet."""
    pages = {}
    for c in cands:  # read recently? use the cached copy
        if c.fetchable and not c.text and (hit := _cache_get(f"p:{normalize(c.url)}")) is not None:
            hit["published"] = datetime.fromisoformat(hit["published"]) if hit["published"] else None
            pages[c.url] = hit
    fetchable = [c for c in cands if c.fetchable and not c.text and c.url not in pages]
    rendered = await browser.read_many([c.url for c in fetchable], timeout=budget * 0.75) if fetchable else {}
    for c in fetchable:
        got = rendered.get(c.url)
        if got and len(got["text"]) > 200:
            meta = extract(got["html"], c.url) if got["html"] else {}
            pages[c.url] = {"title": got["title"] or meta.get("title") or c.title, "text": got["text"],
                            "published": meta.get("published") or c.published, "article": meta.get("article")}
            _cache_put(f"p:{normalize(c.url)}", {**pages[c.url], "published": pages[c.url]["published"].isoformat()
                                                 if pages[c.url]["published"] else None}, 6 * 3600)
    missing = [c for c in cands if c.url not in pages]
    if missing:
        tasks = {asyncio.create_task(_read(http, c)): c for c in missing}
        done, pending = await asyncio.wait(tasks, timeout=max(3.0, budget * 0.4))
        for t in pending:
            t.cancel()
        for t, c in tasks.items():
            pages[c.url] = t.result() if t in done and not t.exception() else \
                {"title": c.title, "text": f"{c.title}. {c.snippet}", "published": c.published}
    return [pages[c.url] for c in cands]


async def search(queries: list[str], freshness: str = "general", progress=None, k: int = 5, budget: int = 3500) -> list[dict]:
    """Ask every source, read the most promising pages, return the top passages:
    [{text, url, site, title, published}], most relevant first. No topic routing, no site lists:
    relevance (keywords + meaning), freshness and how many sources agree decide."""
    started = time.time()
    say = progress or (lambda *_: None)
    _purge_cache()
    queries = [q.strip() for q in queries if q and q.strip()][:3]
    if not queries:
        return []
    async with client() as http:
        found = await asyncio.gather(*[_ask(http, n, q, freshness) for q in queries for n in SOURCES])
        seen: dict[str, Candidate] = {}
        hits: dict[str, int] = {}
        for c in (c for batch in found for c in batch if c.url):
            key = normalize(c.url)
            hits[key] = hits.get(key, 0) + 1
            if key not in seen or (c.published and not seen[key].published) or len(c.snippet) > len(seen[key].snippet):
                seen[key] = c
        if not seen:
            return []

        # Pre-rank on title + snippet (cheap), max 2 pages per site, then read the best ones.
        q_text = " ".join(queries)
        pre = [{"text": f"{c.title}. {c.snippet}", "url": c.url, "published": c.published, "c": c,
                "hits": hits[normalize(c.url)]} for c in seen.values()]
        rank.score(pre, q_text, freshness, None)
        pre.sort(key=lambda it: it["score"], reverse=True)
        per_site, chosen = {}, []
        for it in pre:
            site = _site(it["url"])
            if per_site.get(site, 0) >= 2:
                continue
            per_site[site] = per_site.get(site, 0) + 1
            chosen.append(it)
            if len(chosen) >= 7:
                break
        sites = list(dict.fromkeys(_site(it["url"]) for it in chosen))
        say("Reading " + ", ".join(sites[:4]) + (f" +{len(sites) - 4} more" if len(sites) > 4 else ""))
        pages = await _read_pages(http, [it["c"] for it in chosen])

    items = []
    for it, page in zip(chosen, pages):
        c = it["c"]
        site = _site(c.extra.get("site") or c.url) or c.url
        for text in (passages(page["text"]) or [page["text"][:650]])[:12]:
            items.append({"text": text, "url": c.url, "site": site, "title": page["title"] or c.title,
                          "published": page["published"], "hits": it["hits"], "article": page.get("article")})
    # Drop passages sharing no topic word with the query (sidebars, unrelated sections).
    q_words = {w for w in rank.tokens(q_text) if not DATE_WORDS.match(w)}
    if q_words:
        items = [it for it in items if q_words & set(rank.tokens(it["text"] + " " + it["title"]))] or items
    if not items:
        return []
    if len(items) > 40:  # meaning-ranking on a CPU costs ~0.1 s per passage: keyword-rank first, embed the best 40
        rank.score(items, q_text, freshness, None)
        items = sorted(items, key=lambda it: it["score"], reverse=True)[:40]
    vecs = await llm.embed([q_text] + [it["text"] for it in items])
    qvec = None
    if vecs and len(vecs) == len(items) + 1:
        qvec = vecs[0]
        for it, v in zip(items, vecs[1:]):
            it["vec"] = v
    rank.score(items, q_text, freshness, qvec)
    if freshness in ("breaking", "recent"):
        # For "what's new" questions, pages that declare themselves articles beat site/listing pages
        # (which string together unrelated headlines). Pages that don't say are left alone.
        for it in items:
            it["score"] *= {True: 1.15, False: 0.6}.get(it.get("article"), 1.0)
    best = rank.diverse_top(items, k=k, char_budget=budget)
    log.info("search %r → %d passages in %.1fs", queries, len(best), time.time() - started)
    return best


async def read_page(url: str) -> dict:
    """Open one page in the browser (plain fetch as fallback): {url, site, title, text, published}."""
    async with client() as http:
        page = (await _read_pages(http, [Candidate(url=url, title=url)], budget=14))[0]
    return {"url": url, "site": _site(url), **page}
