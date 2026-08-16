"""
ISL Dataset API
===============

FastAPI layer over https://indiansignlanguage.org.

Endpoints
---------
GET  /fetch                -> JSON array of dictionary slugs
GET  /fetch?word=<word>    -> MP4 binary (FileResponse) of that word's sign video
GET  /healthz              -> liveness / cache / browser stats
POST /admin/refresh        -> force re-scrape of the dictionary index

Why CloakBrowser
----------------
The upstream site sits behind a Cloudflare *managed challenge*: plain ``requests`` (even
with full desktop browser headers) and TLS-fingerprint impersonation both get
``403 Just a moment...`` on every path, including ``wp-json`` and the sitemaps.
``cloakbrowser`` drives a patched stealth Chromium via Playwright, which passes the
challenge and yields the real HTML.

The browser context is persistent (``ISL_PROFILE_DIR``), so the ``cf_clearance`` cookie is
reused: the first page load pays the ~12 s challenge, subsequent loads are ~1 s.

Concurrency model
-----------------
* Page fetches are natively async (Playwright) — they never block the event loop.
* HTML parsing (BeautifulSoup) and video downloads (yt-dlp) are synchronous and are
  pushed onto worker threads via ``fastapi.concurrency.run_in_threadpool``.
* A semaphore caps concurrent browser tabs; a single shared context is reused because the
  free CloakBrowser tier allows one concurrent browser session.
"""

from __future__ import annotations

import asyncio
import logging
import os
import re
import sys
import threading
import time
import unicodedata
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Any, Final
from urllib.parse import urljoin, urlparse

import cloakbrowser
import uvicorn
import yt_dlp
from bs4 import BeautifulSoup
from bs4.element import Tag
from fastapi import FastAPI, HTTPException, Query
from fastapi.concurrency import run_in_threadpool
from fastapi.responses import FileResponse, JSONResponse

# --------------------------------------------------------------------------------------
# Windows event-loop fix (must run at import time, before uvicorn builds its loop)
# --------------------------------------------------------------------------------------
if sys.platform == "win32":  # pragma: no cover - platform specific
    # uvicorn selects SelectorEventLoop whenever it needs subprocesses of its own
    # (--reload, or --workers > 1). Playwright launches Chromium as a subprocess, and
    # asyncio's Windows selector loop cannot do that -> NotImplementedError on startup.
    # Force the Proactor loop, which supports subprocesses. Patching the factory (rather
    # than the loop policy) also covers uvicorn's reload children, because they re-import
    # this module to load the app.
    import uvicorn.loops.asyncio as _uvicorn_asyncio_loop

    def _proactor_loop_factory(use_subprocess: bool = False):  # noqa: ARG001
        return asyncio.ProactorEventLoop

    _uvicorn_asyncio_loop.asyncio_loop_factory = _proactor_loop_factory

# --------------------------------------------------------------------------------------
# Configuration (env-overridable so one image works across CI/CD environments)
# --------------------------------------------------------------------------------------

BASE_URL: Final[str] = os.getenv("ISL_BASE_URL", "https://indiansignlanguage.org")
INDEX_URL: Final[str] = os.getenv("ISL_INDEX_URL", f"{BASE_URL}/search-dictionary/")

DOWNLOAD_DIR: Final[Path] = Path(os.getenv("ISL_DOWNLOAD_DIR", "./downloads")).resolve()
WORKERS: Final[int] = int(os.getenv("ISL_WORKERS", "1"))

# Chromium locks its user-data dir, so multi-worker deployments need one profile per
# process. Each worker then pays the Cloudflare challenge once, on its own first request.
_PROFILE_BASE: Final[Path] = Path(
    os.getenv("ISL_PROFILE_DIR", "./.browser-profile")
).resolve()
PROFILE_DIR: Final[Path] = (
    _PROFILE_BASE if WORKERS <= 1 else _PROFILE_BASE / f"p{os.getpid()}"
)

CACHE_TTL_SECONDS: Final[int] = int(os.getenv("ISL_CACHE_TTL", str(6 * 60 * 60)))
NAV_TIMEOUT_MS: Final[int] = int(os.getenv("ISL_NAV_TIMEOUT_MS", "90000"))
CHALLENGE_TIMEOUT: Final[int] = int(os.getenv("ISL_CHALLENGE_TIMEOUT", "120"))
YTDLP_TIMEOUT: Final[int] = int(os.getenv("ISL_YTDLP_TIMEOUT", "120"))
DOWNLOAD_ATTEMPTS: Final[int] = int(os.getenv("ISL_DOWNLOAD_ATTEMPTS", "3"))
PAGE_CONCURRENCY: Final[int] = int(os.getenv("ISL_PAGE_CONCURRENCY", "4"))
HEADLESS: Final[bool] = os.getenv("ISL_HEADLESS", "1").lower() not in ("0", "false", "no")
PROXY: Final[str] = os.getenv("ISL_PROXY", "").strip()
LICENSE_KEY: Final[str] = os.getenv("CLOAKBROWSER_LICENSE_KEY", "").strip()

# Primary selector for the A–Z dictionary listing on /search-dictionary/ (3185 entries at
# time of writing); the generic anchor scan below is the fallback if the theme changes.
INDEX_SELECTOR: Final[str] = os.getenv("ISL_INDEX_SELECTOR", "ul.az-columns li a[href]")

# Root-level slugs that are site navigation, not dictionary entries. Only consulted by the
# fallback scan — the az-columns selector already excludes the menus.
NON_WORD_SLUGS: Final[frozenset[str]] = frozenset(
    {
        "", "search-dictionary", "about-us", "contact", "contact-us", "privacy-policy",
        "terms", "terms-of-use", "disclaimer", "sitemap", "home", "blog", "news", "faq",
        "donate", "support", "login", "register", "wp-login.php", "wp-admin", "feed",
        "category", "tag", "author", "page", "courses", "services", "dissertations",
        "history", "indian-sign-language", "educational-services", "guidance-counselling",
        "speech-language-therapy", "audiological-evaluation", "tutorial-for-the-deaf",
        "purchase-the-isl-dictionary", "android-app-for-indian-sign-language",
        "what-is-the-purpose-of-this-website",
    }
)

# Cloudflare interstitial fingerprints.
CHALLENGE_MARKERS: Final[tuple[str, ...]] = (
    "just a moment",
    "cf_chl_opt",
    "challenge-platform",
    "enable javascript and cookies to continue",
)

YOUTUBE_ID_RE: Final[re.Pattern[str]] = re.compile(
    r"(?:youtube(?:-nocookie)?\.com/(?:embed/|v/|watch\?(?:.*&)?v=)|youtu\.be/)"
    r"([A-Za-z0-9_-]{11})"
)

logging.basicConfig(
    level=os.getenv("ISL_LOG_LEVEL", "INFO"),
    format="%(asctime)s %(levelname)-8s %(name)s: %(message)s",
)
log = logging.getLogger("isl-api")


class UpstreamChallengeError(RuntimeError):
    """Cloudflare challenge never resolved within the timeout."""


class UpstreamFetchError(RuntimeError):
    """Navigation failed, or the page returned a non-OK status."""


# --------------------------------------------------------------------------------------
# CloakBrowser fetch layer
# --------------------------------------------------------------------------------------


def _looks_challenged(html: str) -> bool:
    head = html[:8192].lower()
    return any(marker in head for marker in CHALLENGE_MARKERS)


def _assert_subprocess_capable_loop() -> None:
    """
    Fail with an actionable message instead of a bare NotImplementedError.

    On Windows only the Proactor loop can spawn subprocesses, and Playwright needs one to
    start Chromium. The import-time patch above normally prevents this.
    """
    if sys.platform != "win32":
        return
    loop = asyncio.get_running_loop()
    if isinstance(loop, asyncio.ProactorEventLoop):
        return
    raise UpstreamFetchError(
        f"cannot launch the browser: running on {type(loop).__name__}, which cannot spawn "
        "subprocesses on Windows. Start the app with 'python main.py' (or set "
        "ISL_RELOAD/--workers aside), or run it under Docker/WSL."
    )


class BrowserManager:
    """
    Owns one persistent stealth browser context and hands out pages.

    The context is created lazily on first use and reused for the process lifetime, so the
    Cloudflare clearance cookie is paid for once. A crashed context is transparently
    rebuilt on the next request.
    """

    def __init__(self) -> None:
        self._context: Any = None
        self._lock = asyncio.Lock()
        self._pages = asyncio.Semaphore(PAGE_CONCURRENCY)
        self._started_at: float | None = None
        self._fetches = 0

    @property
    def stats(self) -> dict[str, Any]:
        return {
            "running": self._context is not None,
            "started_at": self._started_at,
            "fetches": self._fetches,
            "page_concurrency": PAGE_CONCURRENCY,
            "headless": HEADLESS,
            "profile_dir": str(PROFILE_DIR),
        }

    async def _ensure_context(self) -> Any:
        async with self._lock:
            if self._context is not None:
                return self._context
            _assert_subprocess_capable_loop()
            PROFILE_DIR.mkdir(parents=True, exist_ok=True)
            kwargs: dict[str, Any] = {
                "user_data_dir": str(PROFILE_DIR),
                "headless": HEADLESS,
                # Stealth fingerprint args are what actually clear the challenge.
                "stealth_args": True,
                "locale": os.getenv("ISL_LOCALE", "en-US"),
            }
            if PROXY:
                kwargs["proxy"] = PROXY
            if LICENSE_KEY:
                kwargs["license_key"] = LICENSE_KEY

            log.info("launching CloakBrowser (profile=%s)", PROFILE_DIR)
            self._context = await cloakbrowser.launch_persistent_context_async(**kwargs)
            self._context.set_default_navigation_timeout(NAV_TIMEOUT_MS)
            self._started_at = time.time()
            return self._context

    async def _reset(self) -> None:
        """Tear the context down so the next call rebuilds it."""
        async with self._lock:
            context, self._context = self._context, None
            self._started_at = None
        if context is not None:
            try:
                await context.close()
            except Exception:  # noqa: BLE001 - already failing; nothing useful to do
                pass

    async def fetch_html(self, url: str, *, referer: str | None = None) -> str:
        """Load `url` in a stealth tab, wait out any challenge, return the final HTML."""
        last_error: Exception | None = None

        for attempt in (1, 2):  # one transparent retry on a dead/crashed context
            context = await self._ensure_context()
            async with self._pages:
                page = None
                try:
                    page = await context.new_page()
                    await page.goto(
                        url,
                        wait_until="domcontentloaded",
                        timeout=NAV_TIMEOUT_MS,
                        referer=referer,
                    )
                    html = await self._await_challenge(page)
                    self._fetches += 1
                    return html
                except (UpstreamChallengeError, UpstreamFetchError):
                    raise
                except Exception as exc:  # noqa: BLE001 - playwright/browser-level failure
                    last_error = exc
                    log.warning("browser fetch failed (attempt %d) for %s: %s", attempt, url, exc)
                finally:
                    if page is not None:
                        try:
                            await page.close()
                        except Exception:  # noqa: BLE001
                            pass
            await self._reset()

        raise UpstreamFetchError(f"browser navigation failed for {url}: {last_error}")

    @staticmethod
    async def _await_challenge(page: Any) -> str:
        """
        Poll page content until the Cloudflare interstitial is replaced by real HTML.

        ``page.content()`` raises while the challenge is mid-redirect, so those errors are
        swallowed and retried rather than treated as failures.
        """
        deadline = time.monotonic() + CHALLENGE_TIMEOUT
        while time.monotonic() < deadline:
            try:
                html = await page.content()
            except Exception:  # noqa: BLE001 - navigating; content not readable yet
                await page.wait_for_timeout(1000)
                continue
            if not _looks_challenged(html):
                return html
            await page.wait_for_timeout(1500)
        raise UpstreamChallengeError(
            f"Cloudflare challenge did not clear within {CHALLENGE_TIMEOUT}s for {page.url}. "
            "Try ISL_HEADLESS=0, a residential ISL_PROXY, or a CloakBrowser license key."
        )

    async def close(self) -> None:
        await self._reset()


BROWSER: Final[BrowserManager] = BrowserManager()


# --------------------------------------------------------------------------------------
# Normalization helpers
# --------------------------------------------------------------------------------------


def slugify(value: str) -> str:
    """
    Normalize arbitrary user input / link text into the site's slug shape.

    "A (Alphabet)" -> "a-alphabet"; "  How Are YOU " -> "how-are-you".
    """
    text = unicodedata.normalize("NFKD", value)
    text = text.encode("ascii", "ignore").decode("ascii").lower().strip()
    text = re.sub(r"[^a-z0-9]+", "-", text)
    return text.strip("-")


def normalize_media_url(url: str, *, page_url: str) -> str:
    """Turn protocol-relative (`//host/...`) and root-relative URLs into absolute https."""
    url = url.strip()
    if url.startswith("//"):
        return f"https:{url}"
    if url.startswith(("http://", "https://")):
        return url
    return urljoin(page_url, url)


def extract_youtube_url(html: str, *, page_url: str) -> str | None:
    """
    Locate the primary YouTube embed on a word page.

    Word pages use ``<iframe class="youtube-player" src="https://www.youtube.com/embed/ID">``,
    but the fallback chain also covers lazy-loaded iframes, legacy <embed>/<object> players,
    plain anchors, and finally a raw regex sweep of the markup.
    """
    soup = BeautifulSoup(html, "html.parser")
    candidates: list[str] = []

    # 1/2. iframes — including lazy-load plugins that park the real URL in data-* attrs.
    for frame in soup.find_all("iframe"):
        if not isinstance(frame, Tag):
            continue
        for attr in ("src", "data-src", "data-lazy-src", "data-litespeed-src"):
            src = frame.get(attr)
            if isinstance(src, str) and src.strip():
                candidates.append(src)

    # 3. Legacy <embed> / <object> players.
    for node in soup.find_all(["embed", "object"]):
        if not isinstance(node, Tag):
            continue
        for attr in ("src", "data"):
            src = node.get(attr)
            if isinstance(src, str) and src.strip():
                candidates.append(src)

    # 4. Plain anchors pointing at YouTube.
    for anchor in soup.find_all("a", href=True):
        if isinstance(anchor, Tag) and isinstance(anchor["href"], str):
            candidates.append(anchor["href"])

    for raw in candidates:
        match = YOUTUBE_ID_RE.search(normalize_media_url(raw, page_url=page_url))
        if match:
            return f"https://www.youtube.com/watch?v={match.group(1)}"

    # 5. Last resort: any YouTube id anywhere in the document (JSON player configs etc).
    match = YOUTUBE_ID_RE.search(html)
    return f"https://www.youtube.com/watch?v={match.group(1)}" if match else None


# --------------------------------------------------------------------------------------
# Dictionary index parsing + cache
# --------------------------------------------------------------------------------------


def _slug_from_href(href: str, *, strict: bool) -> str | None:
    """Return the slug if `href` is a root-level dictionary post URL, else None."""
    parsed = urlparse(href)
    if parsed.scheme and parsed.scheme not in ("http", "https"):
        return None
    if parsed.netloc and "indiansignlanguage.org" not in parsed.netloc.lower():
        return None

    parts = [p for p in parsed.path.split("/") if p]
    if len(parts) != 1:  # word pages are exactly one path segment deep
        return None

    slug = parts[0].lower()
    if "." in slug or not re.fullmatch(r"[a-z0-9][a-z0-9-]*", slug):
        return None
    # The az-columns list is already menu-free, so only the fallback scan filters slugs.
    if strict and slug in NON_WORD_SLUGS:
        return None
    return slug


def parse_index(html: str) -> dict[str, str]:
    """Parse the dictionary index HTML into ``{slug: absolute_page_url}``."""
    soup = BeautifulSoup(html, "html.parser")

    def collect(anchors: list[Any], *, strict: bool) -> dict[str, str]:
        found: dict[str, str] = {}
        for anchor in anchors:
            if not isinstance(anchor, Tag):
                continue
            href = anchor.get("href")
            if not isinstance(href, str):
                continue
            slug = _slug_from_href(href, strict=strict)
            if slug and slug not in found:
                found[slug] = normalize_media_url(href, page_url=INDEX_URL).rstrip("/") + "/"
        return found

    # Preferred: the A–Z listing block. Fallback: every anchor on the page, menu-filtered.
    entries = collect(soup.select(INDEX_SELECTOR), strict=False)
    if not entries:
        log.warning("index selector %r matched nothing; falling back to anchor scan", INDEX_SELECTOR)
        entries = collect(soup.find_all("a", href=True), strict=True)

    if not entries:
        raise UpstreamFetchError(
            "dictionary index parsed but produced zero entries — upstream markup changed"
        )
    return dict(sorted(entries.items()))


class DictionaryCache:
    """TTL cache for the dictionary index. Refreshes are serialized by an asyncio lock."""

    def __init__(self, ttl: int) -> None:
        self._ttl = ttl
        self._lock = asyncio.Lock()
        self._entries: dict[str, str] = {}
        self._loaded_at: float = 0.0
        self._last_error: str | None = None

    @property
    def stats(self) -> dict[str, Any]:
        return {
            "entries": len(self._entries),
            "loaded_at": self._loaded_at or None,
            "age_seconds": round(time.time() - self._loaded_at, 1) if self._loaded_at else None,
            "ttl_seconds": self._ttl,
            "last_error": self._last_error,
        }

    def _is_fresh(self) -> bool:
        return bool(self._entries) and (time.time() - self._loaded_at) < self._ttl

    async def get(self, *, force: bool = False) -> dict[str, str]:
        """Return the cached index, re-scraping when stale or forced."""
        if not force and self._is_fresh():
            return self._entries

        async with self._lock:
            # Another coroutine may have refreshed while we waited for the lock.
            if not force and self._is_fresh():
                return self._entries
            try:
                html = await BROWSER.fetch_html(INDEX_URL)
                entries = await run_in_threadpool(parse_index, html)
            except Exception as exc:  # noqa: BLE001 - surfaced to the caller below
                self._last_error = str(exc)
                if self._entries:  # serve stale rather than nothing
                    log.warning("index refresh failed, serving stale cache: %s", exc)
                    return self._entries
                raise
            self._entries = entries
            self._loaded_at = time.time()
            self._last_error = None
            log.info("dictionary index cached: %d entries", len(entries))
            return entries


CACHE: Final[DictionaryCache] = DictionaryCache(CACHE_TTL_SECONDS)


# --------------------------------------------------------------------------------------
# yt-dlp download engine (blocking; always called via run_in_threadpool)
# --------------------------------------------------------------------------------------

_download_locks: dict[str, threading.Lock] = {}
_locks_guard = threading.Lock()


def _lock_for(slug: str) -> threading.Lock:
    """One lock per slug so concurrent requests for the same word download exactly once."""
    with _locks_guard:
        return _download_locks.setdefault(slug, threading.Lock())


def _ffmpeg_available() -> bool:
    """yt-dlp can only merge separate video/audio streams when ffmpeg is on PATH."""
    from shutil import which

    return which("ffmpeg") is not None


def existing_download(slug: str) -> Path | None:
    """Return a previously downloaded, non-empty MP4 for `slug`, if present."""
    path = DOWNLOAD_DIR / f"{slug}.mp4"
    return path if path.is_file() and path.stat().st_size > 0 else None


def download_video(youtube_url: str, slug: str) -> Path:
    """Blocking yt-dlp download to ``./downloads/<slug>.mp4`` via the Python API."""
    cached = existing_download(slug)
    if cached:
        return cached

    with _lock_for(slug):
        cached = existing_download(slug)  # another thread may have finished meanwhile
        if cached:
            return cached

        DOWNLOAD_DIR.mkdir(parents=True, exist_ok=True)
        has_ffmpeg = _ffmpeg_available()
        if not has_ffmpeg:
            # Without ffmpeg, requesting a merged format makes yt-dlp abort outright, so
            # fall back to progressive (pre-muxed) MP4. The Docker image ships ffmpeg.
            log.warning(
                "ffmpeg not found on PATH — falling back to progressive MP4 formats "
                "(install ffmpeg for best available quality)"
            )

        ydl_opts: dict[str, Any] = {
            "format": (
                "bestvideo[ext=mp4]+bestaudio[ext=m4a]/best[ext=mp4]/best"
                if has_ffmpeg
                else "best[ext=mp4]/best"
            ),
            "merge_output_format": "mp4",
            "outtmpl": str(DOWNLOAD_DIR / f"{slug}.%(ext)s"),
            "noplaylist": True,
            "quiet": True,
            "no_warnings": True,
            "noprogress": True,
            "socket_timeout": YTDLP_TIMEOUT,
            "retries": 3,
            "fragment_retries": 3,
            "concurrent_fragment_downloads": 4,
            "overwrites": False,
            # Force a clean .mp4 container even when the chosen streams are webm.
            "postprocessors": (
                [{"key": "FFmpegVideoConvertor", "preferedformat": "mp4"}]
                if has_ffmpeg
                else []
            ),
        }

        # YouTube intermittently answers 403 to bursts of requests from one IP; the same
        # URL succeeds moments later. Retry the whole extraction with a backoff instead of
        # surfacing a transient failure to the client.
        last_error: Exception | None = None
        for attempt in range(1, DOWNLOAD_ATTEMPTS + 1):
            try:
                with yt_dlp.YoutubeDL(ydl_opts) as ydl:
                    ydl.download([youtube_url])
                last_error = None
                break
            except yt_dlp.utils.DownloadError as exc:
                last_error = exc
                if attempt < DOWNLOAD_ATTEMPTS:
                    delay = 2.0 * attempt
                    log.warning(
                        "yt-dlp attempt %d/%d failed for '%s' (%s); retrying in %.0fs",
                        attempt, DOWNLOAD_ATTEMPTS, slug, exc, delay,
                    )
                    time.sleep(delay)
        if last_error is not None:
            raise last_error

        final = existing_download(slug)
        if final:
            return final
        # Postprocessing edge case: container ended up as something other than mp4.
        for leftover in sorted(DOWNLOAD_DIR.glob(f"{slug}.*")):
            if leftover.is_file() and leftover.stat().st_size > 0:
                return leftover
        raise RuntimeError(f"yt-dlp produced no output file for '{slug}'")


# --------------------------------------------------------------------------------------
# FastAPI application
# --------------------------------------------------------------------------------------


@asynccontextmanager
async def lifespan(_: FastAPI):
    """Warm the browser + index at boot; tear the browser down on shutdown."""
    DOWNLOAD_DIR.mkdir(parents=True, exist_ok=True)
    try:
        await CACHE.get()
    except Exception as exc:  # noqa: BLE001 - never block startup on upstream health
        log.warning("startup warm-up failed (will retry on first request): %s", exc)
    try:
        yield
    finally:
        await BROWSER.close()


app = FastAPI(
    title="ISL Dataset API",
    version="2.0.0",
    description=(
        "API layer over indiansignlanguage.org. Lists dictionary entries and serves the "
        "sign-language video for any word as MP4. Cloudflare is bypassed with CloakBrowser."
    ),
    lifespan=lifespan,
)


@app.get("/healthz", summary="Liveness probe with cache and browser stats")
async def healthz() -> dict[str, Any]:
    return {
        "status": "ok",
        "cache": CACHE.stats,
        "browser": BROWSER.stats,
        "download_dir": str(DOWNLOAD_DIR),
    }


@app.post("/admin/refresh", summary="Force a dictionary index re-scrape")
async def refresh_index() -> dict[str, Any]:
    entries = await _get_index(force=True)
    return {"refreshed": True, "entries": len(entries)}


@app.get(
    "/fetch",
    summary="List dictionary entries, or download one word's MP4",
    response_description="JSON array of slugs, or an MP4 file stream",
)
async def fetch(
    word: str | None = Query(
        default=None,
        description="Word or slug to download. Omit to list all dictionary entries.",
    ),
) -> Any:
    # ---- Mode 1: no query param -> list the whole dictionary --------------------------
    if word is None:
        index = await _get_index()
        return JSONResponse(content=list(index.keys()))

    # ---- Mode 2: ?word=... -> resolve, scrape, download, stream -----------------------
    if not word.strip():
        raise HTTPException(status_code=400, detail="Query parameter 'word' is empty.")

    slug = slugify(word)
    if not slug:
        raise HTTPException(
            status_code=400,
            detail=f"Query parameter 'word' ({word!r}) contains no usable characters.",
        )

    # Fast path: already on disk — skip the page scrape and yt-dlp entirely.
    cached_file = existing_download(slug)
    if cached_file:
        return _video_response(cached_file, slug)

    index = await _get_index()
    page_url = _resolve_page_url(index, slug)

    try:
        html = await BROWSER.fetch_html(page_url, referer=INDEX_URL)
    except UpstreamChallengeError as exc:
        raise HTTPException(status_code=503, detail=str(exc)) from exc
    except UpstreamFetchError as exc:
        raise HTTPException(status_code=502, detail=f"Failed to load word page: {exc}") from exc

    youtube_url = await run_in_threadpool(extract_youtube_url, html, page_url=page_url)
    if not youtube_url:
        raise HTTPException(
            status_code=404,
            detail=f"No YouTube embed found on the page for '{slug}' ({page_url}).",
        )

    try:
        path = await run_in_threadpool(download_video, youtube_url, slug)
    except yt_dlp.utils.DownloadError as exc:
        raise HTTPException(status_code=500, detail=f"yt-dlp failed for '{slug}': {exc}") from exc
    except Exception as exc:  # noqa: BLE001
        raise HTTPException(
            status_code=500, detail=f"Unexpected download failure for '{slug}': {exc}"
        ) from exc

    return _video_response(path, slug)


async def _get_index(*, force: bool = False) -> dict[str, str]:
    """Cached index accessor that maps scrape failures onto clean HTTP errors."""
    try:
        return await CACHE.get(force=force)
    except UpstreamChallengeError as exc:
        raise HTTPException(status_code=503, detail=str(exc)) from exc
    except UpstreamFetchError as exc:
        raise HTTPException(
            status_code=502, detail=f"Failed to scrape dictionary index: {exc}"
        ) from exc
    except Exception as exc:  # noqa: BLE001
        raise HTTPException(status_code=500, detail=f"Index error: {exc}") from exc


def _resolve_page_url(index: dict[str, str], slug: str) -> str:
    """Exact slug match, then a forgiving unique-prefix match ('apple' -> 'apple-fruit')."""
    if slug in index:
        return index[slug]

    prefixed = [s for s in index if s.startswith(f"{slug}-")]
    if len(prefixed) == 1:
        return index[prefixed[0]]

    raise HTTPException(
        status_code=404,
        detail={
            "message": f"Word '{slug}' not found in the dictionary index.",
            "suggestions": [s for s in index if slug in s][:5],
        },
    )


def _video_response(path: Path, slug: str) -> FileResponse:
    """Serve the file as a download-friendly MP4 attachment."""
    return FileResponse(
        path=path,
        media_type="video/mp4",
        filename=f"{slug}.mp4",
        headers={"Content-Disposition": f'attachment; filename="{slug}.mp4"'},
    )


if __name__ == "__main__":
    # Launch through this module (not the `uvicorn` CLI) on Windows: the CLI builds its
    # event loop before importing the app, so the Proactor patch above would land too late
    # and the browser could not start. `python main.py` re-imports this module in every
    # reload/worker child, so the patch is always in place first.
    workers = WORKERS
    if sys.platform == "win32" and workers > 1:
        # On Windows the Proactor loop cannot share a listening socket between worker
        # processes (OSError WinError 87), and Chromium needs the Proactor loop. Scale out
        # with containers instead.
        log.warning("ISL_WORKERS=%d is unsupported on Windows; falling back to 1", workers)
        workers = 1

    uvicorn.run(
        "main:app",
        host=os.getenv("ISL_HOST", "0.0.0.0"),
        port=int(os.getenv("ISL_PORT", "6767")),
        reload=bool(os.getenv("ISL_RELOAD")),
        workers=workers,
        proxy_headers=True,
    )
