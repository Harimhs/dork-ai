"""Tools the brain can call. The brain decides when and how; Dork just runs them.

Each tool returns text for the brain. Every page a tool reads gets a source number for this
turn, so the answer can cite [n] and the UI can list the sources."""

import re
from urllib.parse import urlparse

import httpx

from . import llm, websearch
from .websearch.rank import tokens as rank_tokens

TOOLS = [
    {
        "name": "web_search",
        "description": (
            "Search the web and read the best pages. Use for anything current, factual, or that you don't know "
            "for sure: news, prices, releases, people, places, documentation, how-tos. Returns numbered passages "
            "with their site and date."
        ),
        "parameters": {
            "type": "object",
            "properties": {
                "query": {"type": "string", "description": "Search-engine keywords, e.g. 'ISRO Gaganyaan latest test'"},
                "freshness": {
                    "type": "string", "enum": ["breaking", "recent", "general", "timeless"],
                    "description": "breaking = hours/days matter; recent = weeks/months; timeless = age doesn't matter",
                },
            },
            "required": ["query"],
        },
    },
    {
        "name": "open_page",
        "description": (
            "Open a specific web page and read its text. Use when the user gives or refers to a URL or website, "
            "or to read a page found by web_search in full."
        ),
        "parameters": {
            "type": "object",
            "properties": {"url": {"type": "string", "description": "Full URL, e.g. https://isro.gov.in"}},
            "required": ["url"],
        },
    },
    {
        "name": "github",
        "description": (
            "Look at a GitHub account (profile, bio, recent repositories) or one repository (details + README). "
            "Use for anything about someone's GitHub, repos or projects on GitHub."
        ),
        "parameters": {
            "type": "object",
            "properties": {
                "username": {"type": "string", "description": "GitHub username"},
                "repo": {"type": "string", "description": "Repository name, to look at one repo (optional)"},
            },
            "required": ["username"],
        },
    },
]


class Turn:
    """Source bookkeeping for one reply: each distinct URL gets a stable number."""

    def __init__(self):
        self.sources: list[dict] = []
        self._by_url: dict[str, int] = {}

    def cite(self, url: str, title: str, site: str, published) -> int:
        if url not in self._by_url:
            self._by_url[url] = len(self.sources) + 1
            self.sources.append({"n": self._by_url[url], "title": (title or url)[:160], "url": url, "site": site,
                                 "published": published.isoformat() if published else None,
                                 "age": websearch.age_text(published)})
        return self._by_url[url]


def _limit(text: str) -> int:
    return 2500 if llm.LOCAL else 9000  # a 3B model on CPU needs tens of seconds per ~1k tokens read


def _header(n: int, page: dict) -> str:
    when = websearch.age_text(page.get("published"))
    date = f" ({page['published']:%Y-%m-%d})" if page.get("published") else ""
    return f"[{n}] {page.get('title') or page['url']} — {page.get('site', '')} — {when}{date}"


def label(name: str, args: dict) -> str:
    """Short human description of a call, for the live activity log."""
    if name == "web_search":
        return f"Searching: {args.get('query', '')}"
    if name == "open_page":
        return f"Opening {re.sub(r'^https?://', '', str(args.get('url', '')))[:60]}"
    if name == "github":
        who = args.get("username", "")
        return f"Reading github.com/{who}" + (f"/{args['repo']}" if args.get("repo") else "")
    return name


async def run(name: str, args: dict, turn: Turn, progress=None) -> str:
    """Execute one tool call → text for the brain (never raises)."""
    try:
        if name == "web_search":
            return await _web_search(args, turn, progress)
        if name == "open_page":
            return await _open_page(str(args.get("url", "")), turn)
        if name == "github":
            return await _github(str(args.get("username", "")), args.get("repo"), turn)
        return f"Unknown tool {name}."
    except httpx.HTTPError as e:
        return f"The {name} call failed ({e.__class__.__name__}). Say so, or try a different query/URL."
    except Exception as e:  # noqa: BLE001 — a broken tool must not break the reply
        return f"The {name} call failed ({e.__class__.__name__}: {str(e)[:120]})."


SITE = re.compile(r"\b(?:https?://)?((?:[a-z0-9-]+\.)+(?:com|org|net|io|in|dev|ai|edu|gov|co|me|app|info|tech|uk|us))\b(/[^\s]*)?", re.I)

# Appended to every tool result: small models follow rules placed right next to the data.
CITE_RULE = ("\n\n(Each [n] block is a separate source. Use only blocks relevant to the question, don't mix facts "
             "between blocks, cite as [n], and don't write any links yourself.)")


async def _web_search(args: dict, turn: Turn, progress) -> str:
    query = str(args.get("query", "")).strip()
    if not query:
        return "No query given."
    out = []
    opened: set[str] = set()
    # A query that names a website ("isro.gov.in what's new"): read that site itself first.
    if m := SITE.search(query):
        site_text = await _open_page(m.group(0), turn, cite_rule=False, chars=1800)
        if not site_text.startswith("Opened"):
            out.append(site_text)
            opened.add(_domain(m.group(1)))
    best = await websearch.search(
        [query], freshness=args.get("freshness") or "general",
        progress=progress, k=4 if llm.LOCAL else 8, budget=_limit(""),
    )
    # The thing asked about has its own site among the results (droidal.ai for "droidal"):
    # read that site properly instead of relying on snippets.
    if official := _official_site(query, best, opened):
        if progress:
            progress(f"Opening {official}")
        site_text = await _open_page(official, turn, cite_rule=False, chars=1800)
        if not site_text.startswith("Opened"):
            out.insert(0, site_text)
    for it in best:
        n = turn.cite(it["url"], it["title"], it["site"], it["published"])
        out.append(f"{_header(n, it)}\n{it['text']}")
    if not out:
        return f"No useful results for '{query}'. Try different keywords."
    return "\n\n".join(out) + CITE_RULE


def _domain(host: str) -> str:
    return host.lower().removeprefix("www.")


def _official_site(query: str, results: list[dict], already: set[str]) -> str | None:
    """A result whose domain name matches a word of the query — e.g. droidal.ai for 'droidal company' —
    is most likely the official site. Returns its homepage URL, or None."""
    words = [w for w in rank_tokens(query) if len(w) >= 4]
    for it in results:
        host = _domain(urlparse(it["url"]).netloc)
        if host in already:
            continue
        name = host.split(".")[-2] if host.count(".") >= 1 else host
        if any(w in name or (len(name) >= 4 and name in w) for w in words):
            return f"https://{host}/"
    return None


async def _open_page(url: str, turn: Turn, cite_rule: bool = True, chars: int | None = None) -> str:
    if not re.match(r"^https?://", url):
        url = "https://" + url.lstrip("/")
    parts = urlparse(url)
    if parts.netloc.lower().removeprefix("www.") == "github.com":
        bits = [b for b in parts.path.split("/") if b]
        if bits:  # github.com/user or github.com/user/repo → use the API, it's far richer
            return await _github(bits[0], bits[1] if len(bits) > 1 else None, turn)
    page = await websearch.read_page(url)
    if len(page.get("text", "")) < 80 and not parts.netloc.lower().startswith("www."):
        # Many sites (e.g. isro.gov.in) only exist under www.
        url = parts._replace(netloc="www." + parts.netloc).geturl()
        page = await websearch.read_page(url)
    if len(page.get("text", "")) < 80:
        return f"Opened {url} but couldn't read any text (it may need JavaScript or a login)."
    n = turn.cite(url, page["title"], page["site"], page["published"])
    return f"{_header(n, page)}\n{page['text'][:chars or _limit('')]}" + (CITE_RULE if cite_rule else "")


async def _github(user: str, repo, turn: Turn) -> str:
    user = user.strip().lstrip("@").strip("/")
    if not user:
        return "No GitHub username given."
    page = await websearch.github(user, str(repo).strip() if repo else None)
    n = turn.cite(page["url"], page["title"], page["site"], page["published"])
    return f"{_header(n, page)}\n{page['text'][:_limit('')]}" + CITE_RULE
