"""Turn a web page into clean main text, a title and a publish date — stdlib only."""

import html
import json
import re
from datetime import datetime
from html.parser import HTMLParser

from .sources import parse_date

SKIP = {"script", "style", "noscript", "nav", "footer", "header", "aside", "form", "svg", "iframe",
        "button", "select", "figure", "template", "canvas"}
BLOCK = {"p", "li", "h1", "h2", "h3", "h4", "h5", "td", "th", "pre", "blockquote", "dd", "dt", "div", "section",
         "article", "main", "br", "tr"}
VOID = {"br", "img", "meta", "link", "input", "hr", "source", "wbr", "area", "base", "col", "embed", "track"}

DATE_META = ("article:published_time", "og:published_time", "datepublished", "publishdate", "publish-date",
             "pubdate", "date", "dc.date", "dc.date.issued", "sailthru.date", "parsely-pub-date",
             "article:modified_time", "og:updated_time", "datemodified", "last-modified")


class _Reader(HTMLParser):
    def __init__(self):
        super().__init__(convert_charrefs=True)
        self.skip_depth = 0
        self.main_depth = 0
        self.all_parts: list[str] = []
        self.main_parts: list[str] = []
        self.title = ""
        self.in_title = False
        self.meta: dict[str, str] = {}
        self.times: list[str] = []
        self.ld_json: list[str] = []
        self.in_ld = False

    def handle_starttag(self, tag, attrs):
        a = dict(attrs)
        if tag == "meta":
            key = (a.get("property") or a.get("name") or a.get("itemprop") or "").lower()
            if key and a.get("content"):
                self.meta.setdefault(key, a["content"])
            return
        if tag == "time" and a.get("datetime"):
            self.times.append(a["datetime"])
        if tag == "script" and (a.get("type") or "").lower() == "application/ld+json":
            self.in_ld = True
        if tag == "title":
            self.in_title = True
        if tag in VOID:
            if tag == "br":
                self._emit("\n")
            return
        if tag in SKIP:
            self.skip_depth += 1
        if tag in ("article", "main") or a.get("role") == "main":
            self.main_depth += 1
        if tag in BLOCK:
            self._emit("\n")

    def handle_endtag(self, tag):
        if tag == "title":
            self.in_title = False
        if tag == "script":
            self.in_ld = False
        if tag in VOID:
            return
        if tag in SKIP and self.skip_depth:
            self.skip_depth -= 1
        if tag in ("article", "main") and self.main_depth:
            self.main_depth -= 1
        if tag in BLOCK:
            self._emit("\n")

    def handle_data(self, data):
        if self.in_ld:
            self.ld_json.append(data)
            return
        if self.in_title:
            self.title += data
            return
        if not self.skip_depth:
            self._emit(data)

    def _emit(self, text):
        self.all_parts.append(text)
        if self.main_depth:
            self.main_parts.append(text)


def _clean(parts: list[str]) -> str:
    text = re.sub(r"[ \t\r\f\v]+", " ", "".join(parts))
    lines = [ln.strip() for ln in text.split("\n")]
    # Drop menu-ish fragments: very short lines that aren't sentence-like.
    lines = [ln for ln in lines if len(ln) > 40 or (len(ln) > 15 and ln[-1:] in ".:?!")]
    return "\n".join(lines)


def _ld_date(blobs: list[str]) -> str | None:
    for blob in blobs:
        for key in ("datePublished", "dateCreated", "uploadDate", "dateModified"):
            m = re.search(rf'"{key}"\s*:\s*"([^"]+)"', blob)
            if m:
                return m.group(1)
    return None


def _url_date(url: str) -> str | None:
    m = re.search(r"/(20\d{2})[/-](\d{1,2})[/-](\d{1,2})(?:/|-|$)", url)
    if m:
        return f"{m.group(1)}-{int(m.group(2)):02d}-{int(m.group(3)):02d}"
    return None


def extract(page_html: str, url: str) -> dict:
    """→ {title, text, published (datetime|None)}"""
    r = _Reader()
    try:
        r.feed(page_html)
        r.close()
    except Exception:  # noqa: BLE001 — malformed HTML: keep whatever was parsed
        pass
    main = _clean(r.main_parts)
    text = main if len(main) > 400 else _clean(r.all_parts)

    published = None
    for key in DATE_META:
        if key in r.meta:
            published = parse_date(r.meta[key])
            if published:
                break
    published = published or parse_date(_ld_date(r.ld_json)) or parse_date(_url_date(url))
    if not published:
        for t in r.times:
            if published := parse_date(t):
                break

    title = html.unescape(r.meta.get("og:title") or r.title).strip()
    return {"title": title, "text": text, "published": published, "article": _is_article(r)}


def _is_article(r: "_Reader") -> bool | None:
    """What the page says it is, via standard metadata (any site): True = an article/post,
    False = a site/listing page, None = it doesn't say."""
    ld = " ".join(r.ld_json)
    if re.search(r'"@type"\s*:\s*\[?\s*"(NewsArticle|Article|BlogPosting|ReportageNewsArticle|AnalysisNewsArticle|'
                 r'TechArticle|ScholarlyArticle|Report|LiveBlogPosting)"', ld):
        return True
    og = r.meta.get("og:type", "").lower()
    if og.startswith("article") or "article:published_time" in r.meta:
        return True
    if og in ("website", "profile") or re.search(r'"@type"\s*:\s*"(CollectionPage|WebSite|SearchResultsPage)"', ld):
        return False
    return None


def passages(text: str, size: int = 650, min_len: int = 80, limit: int = 40) -> list[str]:
    """Split text into ~size-char passages along paragraph boundaries."""
    out, buf = [], ""
    for para in text.split("\n"):
        if len(buf) + len(para) > size and buf:
            out.append(buf.strip())
            buf = ""
        buf += para + "\n"
        while len(buf) > size * 1.6:  # a single giant paragraph: cut at a sentence end
            cut = buf.rfind(". ", 0, size) + 1 or size
            out.append(buf[:cut].strip())
            buf = buf[cut:]
    if buf.strip():
        out.append(buf.strip())
    return [p for p in out if len(p) >= min_len and not is_link_list(p)][:limit]


def is_link_list(passage: str) -> bool:
    """Headline/menu blocks (news topic pages, sidebars): many short lines that aren't sentences.
    They mix unrelated stories, so a small model stitches them into false 'facts'."""
    lines = [ln for ln in passage.split("\n") if ln.strip()]
    if len(lines) < 3:
        return False
    sentence_like = sum(1 for ln in lines if ln.rstrip()[-1:] in ".!?:;\"'”)" and len(ln) > 60)
    avg = sum(len(ln) for ln in lines) / len(lines)
    return sentence_like / len(lines) < 0.3 and avg < 110
