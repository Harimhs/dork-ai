"""A real (headless) browser for reading pages, via Crawl4AI + Playwright on the installed Microsoft Edge.

Unlike a plain HTTP fetch, it runs the page's JavaScript, so app-style sites render, and Crawl4AI's
content filter keeps the main text by text/link density instead of hand-written rules.

Playwright needs a Proactor event loop on Windows, so the browser lives in its own thread with its
own loop; Dork's server talks to it through `read_many()`."""

import asyncio
import logging
import os
import threading

from ..config import DATA_DIR

os.environ.setdefault("CRAWL4AI_BASE_DIRECTORY", str(DATA_DIR / "crawl4ai"))

log = logging.getLogger("dork.browser")

PAGE_TIMEOUT_MS = 9000

_loop: asyncio.AbstractEventLoop | None = None
_crawler = None
_start_lock = threading.Lock()
_ready: asyncio.Future | None = None
available = True


def _thread_main(loop: asyncio.AbstractEventLoop) -> None:
    asyncio.set_event_loop(loop)
    loop.run_forever()


def _ensure_loop() -> asyncio.AbstractEventLoop:
    global _loop
    with _start_lock:
        if _loop is None:
            _loop = asyncio.ProactorEventLoop() if os.name == "nt" else asyncio.new_event_loop()
            threading.Thread(target=_thread_main, args=(_loop,), name="dork-browser", daemon=True).start()
    return _loop


async def _get_crawler():
    global _crawler
    if _crawler is None:
        from crawl4ai import AsyncWebCrawler, BrowserConfig

        import sys

        channel = {"chrome_channel": "msedge", "channel": "msedge"} if sys.platform == "win32" else {}
        cfg = BrowserConfig(browser_type="chromium", headless=True, text_mode=True, light_mode=True, verbose=False,
                            **channel)
        crawler = AsyncWebCrawler(config=cfg)
        await crawler.start()
        _crawler = crawler
        log.info("headless browser started")
    return _crawler


def _run_config():
    from crawl4ai import CacheMode, CrawlerRunConfig
    from crawl4ai.content_filter_strategy import PruningContentFilter
    from crawl4ai.markdown_generation_strategy import DefaultMarkdownGenerator

    return CrawlerRunConfig(
        cache_mode=CacheMode.BYPASS,
        markdown_generator=DefaultMarkdownGenerator(
            content_filter=PruningContentFilter(threshold=0.45, threshold_type="dynamic", min_word_threshold=6),
            options={"ignore_links": True, "ignore_images": True, "body_width": 0},
        ),
        excluded_tags=["nav", "footer", "aside", "form", "script", "style"],
        remove_overlay_elements=True,
        page_timeout=PAGE_TIMEOUT_MS,
        wait_until="domcontentloaded",
        verbose=False,
    )


async def _crawl(urls: list[str]) -> dict[str, dict]:
    crawler = await _get_crawler()
    results = await crawler.arun_many(urls, config=_run_config())
    out = {}
    for r in results:
        if not getattr(r, "success", False):
            continue
        md = r.markdown
        text = (getattr(md, "fit_markdown", "") or getattr(md, "raw_markdown", "") or str(md or "")).strip()
        if len(text) < 200:
            text = (getattr(md, "raw_markdown", "") or "").strip()
        out[r.url] = {"text": text, "html": r.html or "", "title": (r.metadata or {}).get("title") or ""}
    return out


async def read_many(urls: list[str], timeout: float = 12) -> dict[str, dict]:
    """Render and read several pages in the browser → {url: {text, html, title}}; failures are just missing.
    Never raises: if the browser can't start, returns {} and callers fall back to plain fetching."""
    global available
    if not urls or not available:
        return {}
    loop = _ensure_loop()
    fut = asyncio.run_coroutine_threadsafe(_crawl(urls), loop)
    try:
        return await asyncio.wait_for(asyncio.wrap_future(fut), timeout)
    except asyncio.TimeoutError:
        fut.cancel()
        log.info("browser read timed out after %ss", timeout)
        return {}
    except ImportError:
        available = False
        log.warning("crawl4ai not installed; using the plain page reader")
        return {}
    except Exception as e:  # noqa: BLE001
        log.warning("browser read failed: %s: %s", e.__class__.__name__, str(e)[:200])
        return {}


async def warm() -> None:
    """Start the browser in the background at Dork's startup, so the first search doesn't pay for it."""
    if not available:
        return
    try:
        await asyncio.wrap_future(asyncio.run_coroutine_threadsafe(_get_crawler(), _ensure_loop()))
    except Exception as e:  # noqa: BLE001
        log.warning("browser warm-up failed: %s", e)
