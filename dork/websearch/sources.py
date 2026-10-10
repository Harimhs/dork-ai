"""Free, key-less search sources. Each returns a list of Candidate; any source may fail
(rate limit, captcha, network) and the engine simply carries on with the others."""

import email.utils
import html
import json
import re
import urllib.parse
from dataclasses import dataclass, field
from datetime import datetime, timezone

import httpx

UA = "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/130.0 Safari/537.36"


@dataclass
class Candidate:
    url: str
    title: str
    snippet: str = ""
    published: datetime | None = None
    source: str = ""            # which search source found it
    fetchable: bool = True      # False: use title/snippet only (e.g. encoded news links)
    text: str = ""              # pre-fetched body text (e.g. Wikipedia extracts)
    extra: dict = field(default_factory=dict)


def client() -> httpx.AsyncClient:
    return httpx.AsyncClient(
        headers={"User-Agent": UA, "Accept-Language": "en-IN,en;q=0.9"},
        follow_redirects=True,
        timeout=httpx.Timeout(8, connect=5),
    )


def strip_tags(s: str) -> str:
    return html.unescape(re.sub(r"<[^>]+>", "", s or "")).strip()


def parse_date(s: str | None) -> datetime | None:
    """ISO 8601, RFC 2822, or bare YYYY-MM-DD → aware UTC datetime."""
    if not s:
        return None
    s = s.strip()
    try:
        d = email.utils.parsedate_to_datetime(s)
    except (TypeError, ValueError, IndexError):
        d = None
    if d is None:
        m = re.match(r"(\d{4}-\d{2}-\d{2})(?:[T ](\d{2}:\d{2}(?::\d{2})?))?", s)
        if not m:
            return None
        try:
            d = datetime.fromisoformat(m.group(1) + ("T" + m.group(2) if m.group(2) else ""))
        except ValueError:
            return None
    if d.tzinfo is None:
        d = d.replace(tzinfo=timezone.utc)
    now = datetime.now(timezone.utc)
    return d if datetime(1995, 1, 1, tzinfo=timezone.utc) < d <= now.replace(year=now.year + 1) else None


# DuckDuckGo time filter: d / w / m / y
_DDG_RANGE = {"breaking": "d", "recent": "m"}


async def duckduckgo(http: httpx.AsyncClient, query: str, freshness: str) -> list[Candidate]:
    data = {"q": query, "kl": "in-en"}
    if df := _DDG_RANGE.get(freshness):
        data["df"] = df
    resp = await http.post("https://html.duckduckgo.com/html/", data=data)
    resp.raise_for_status()
    out = []
    for block in resp.text.split('class="result results_links')[1:]:
        a = re.search(r'class="result__a" href="([^"]+)"[^>]*>(.*?)</a>', block, re.S)
        if not a:
            continue
        url = html.unescape(a.group(1))
        if "duckduckgo.com/l/?" in url:  # occasionally wrapped in a redirect
            url = urllib.parse.parse_qs(urllib.parse.urlparse(url).query).get("uddg", [url])[0]
        if url.startswith("//"):
            url = "https:" + url
        if "duckduckgo.com/y.js" in url:  # ad
            continue
        snip = re.search(r'class="result__snippet"[^>]*>(.*?)</a>', block, re.S)
        date = re.search(r"&nbsp;\s*(\d{4}-\d{2}-\d{2}T[\d:.]+)", block)
        out.append(Candidate(url=url, title=strip_tags(a.group(2)), snippet=strip_tags(snip.group(1)) if snip else "",
                             published=parse_date(date.group(1)) if date else None, source="duckduckgo"))
    return out


async def google_news(http: httpx.AsyncClient, query: str, freshness: str) -> list[Candidate]:
    q = query + (" when:1d" if freshness == "breaking" else " when:30d" if freshness == "recent" else "")
    resp = await http.get("https://news.google.com/rss/search",
                          params={"q": q, "hl": "en-IN", "gl": "IN", "ceid": "IN:en"})
    resp.raise_for_status()
    out = []
    for item in re.findall(r"<item>(.*?)</item>", resp.text, re.S)[:10]:
        title = strip_tags(re.search(r"<title>(.*?)</title>", item, re.S).group(1))
        link = re.search(r"<link>(.*?)</link>", item, re.S)
        date = re.search(r"<pubDate>(.*?)</pubDate>", item, re.S)
        src = re.search(r'<source url="([^"]*)">(.*?)</source>', item, re.S)
        # Links are Google-encoded redirects: keep the headline + date as a fresh signal.
        out.append(Candidate(url=link.group(1).strip() if link else "", title=title,
                             snippet=f"Headline from {strip_tags(src.group(2)) if src else 'news'}",
                             published=parse_date(date.group(1)) if date else None, source="google-news",
                             fetchable=False, extra={"site": src.group(1) if src else ""}))
    return out


async def bing_news(http: httpx.AsyncClient, query: str, freshness: str) -> list[Candidate]:
    resp = await http.get("https://www.bing.com/news/search", params={"q": query, "format": "rss", "setlang": "en"})
    resp.raise_for_status()
    out = []
    for item in re.findall(r"<item>(.*?)</item>", resp.text, re.S)[:10]:
        link = html.unescape(re.search(r"<link>(.*?)</link>", item, re.S).group(1))
        real = urllib.parse.parse_qs(urllib.parse.urlparse(link).query).get("url", [link])[0]
        desc = re.search(r"<description>(.*?)</description>", item, re.S)
        date = re.search(r"<pubDate>(.*?)</pubDate>", item, re.S)
        out.append(Candidate(url=real, title=strip_tags(re.search(r"<title>(.*?)</title>", item, re.S).group(1)),
                             snippet=strip_tags(html.unescape(desc.group(1))) if desc else "",
                             published=parse_date(date.group(1)) if date else None, source="bing-news"))
    return out


WIKI_UA = "DorkAssistant/2.0 (https://github.com/Harimhs)"  # Wikimedia requires a contact URL


async def wikipedia(http: httpx.AsyncClient, query: str, freshness: str) -> list[Candidate]:
    # Wikimedia's API policy: identify honestly (a browser User-Agent from a script gets 403).
    resp = await http.get("https://en.wikipedia.org/w/api.php", headers={"User-Agent": WIKI_UA}, params={
        "action": "query", "generator": "search", "gsrsearch": query, "gsrlimit": 2, "format": "json",
        "prop": "extracts|info", "explaintext": 1, "exchars": 6000, "inprop": "url",
    })
    resp.raise_for_status()
    pages = sorted(resp.json().get("query", {}).get("pages", {}).values(), key=lambda p: p.get("index", 99))
    return [Candidate(url=p["fullurl"], title=p["title"], snippet=p.get("extract", "")[:300],
                      published=parse_date(p.get("touched")), source="wikipedia", text=p.get("extract", ""))
            for p in pages if p.get("extract")]


async def hacker_news(http: httpx.AsyncClient, query: str, freshness: str) -> list[Candidate]:
    params = {"query": query, "tags": "story", "hitsPerPage": 6}
    resp = await http.get("https://hn.algolia.com/api/v1/search" + ("_by_date" if freshness in ("breaking", "recent") else ""),
                          params=params)
    resp.raise_for_status()
    return [Candidate(url=h["url"], title=h["title"], snippet=f"Hacker News discussion, {h.get('points', 0)} points",
                      published=parse_date(h.get("created_at")), source="hacker-news")
            for h in resp.json().get("hits", []) if h.get("url") and h.get("title")]


SOURCES = {
    "duckduckgo": duckduckgo,
    "google-news": google_news,
    "bing-news": bing_news,
    "wikipedia": wikipedia,
    "hacker-news": hacker_news,
}
