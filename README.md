# ISL Dataset API

FastAPI layer over [indiansignlanguage.org](https://indiansignlanguage.org). Lists the
3185-word sign dictionary, and serves any word's sign-language video as MP4 via `yt-dlp`.

Cloudflare is bypassed with [CloakBrowser](https://github.com/CloakHQ/cloakbrowser).

## Why CloakBrowser

The site sits behind a Cloudflare **managed challenge**. Verified: plain `requests` with full
desktop browser headers → `403 Just a moment...`, and TLS-fingerprint impersonation
(`curl_cffi` + `impersonate="chrome"`) → also `403`, on every path including `wp-json` and
the sitemaps. No header-level trick clears it.

`cloakbrowser` drives a patched stealth Chromium through Playwright and passes the
challenge. The browser context is **persistent** (`ISL_PROFILE_DIR`), so the `cf_clearance`
cookie is reused: first load pays ~10 s, every load after that is ~1 s.

## Endpoints

| Method | Path | Behavior |
| --- | --- | --- |
| `GET` | `/fetch` | JSON array of dictionary slugs (3185 entries) |
| `GET` | `/fetch?word=<word>` | Resolve → scrape → download → stream `video/mp4` attachment |
| `GET` | `/healthz` | Liveness + cache and browser stats |
| `POST` | `/admin/refresh` | Force a dictionary index re-scrape |
| `GET` | `/docs` | Swagger UI |

Status codes: `400` empty/malformed `word` · `404` word not in index, or page has no YouTube
embed (body includes fuzzy `suggestions`) · `500` yt-dlp failure · `502` navigation failure ·
`503` challenge never cleared.

## Site structure (as scraped)

* Index `ul.az-columns li a` → 3185 word links, single page, A–Z, no pagination.
  (The `Next` / `Previous` links on the page are dictionary *words*, not pagination.)
* Word pages: `<iframe class="youtube-player" src="https://www.youtube.com/embed/<ID>">`.
* Menu/nav anchors live outside `.az-columns`, so the primary selector excludes them; a
  generic anchor scan with a nav-slug blocklist is the fallback if the theme changes.

## How it meets the requirements

* **Non-blocking** — page fetches are natively async (Playwright). HTML parsing
  (BeautifulSoup) and yt-dlp downloads are sync, so both go through
  `fastapi.concurrency.run_in_threadpool`. Nothing blocking runs on the event loop.
* **Index cache** — `DictionaryCache`, TTL-based (`ISL_CACHE_TTL`, default 6 h), refreshes
  serialized by an `asyncio.Lock`, warmed at app startup, serves stale data if a refresh
  fails. The index page is never re-fetched per request.
* **Selector fallbacks** — youtube iframes → `youtu.be` iframes → lazy-load attrs
  (`data-src`, `data-lazy-src`, `data-litespeed-src`) → `<embed>`/`<object>` → `<a href>` →
  raw-markup regex. Protocol-relative `//host/...` URLs get `https:` prepended.
* **Normalization** — `slugify()` handles case, whitespace, unicode and parentheticals:
  `"A (Alphabet)"` → `a-alphabet`, `"  How Are YOU "` → `how-are-you`. Unique-prefix
  fallback resolves `apple` → `apple-fruit` style slugs.
* **yt-dlp** — Python API (`yt_dlp.YoutubeDL`), never `subprocess`. Format
  `bestvideo[ext=mp4]+bestaudio[ext=m4a]/best[ext=mp4]/best` + `merge_output_format=mp4`.
  If ffmpeg is missing it degrades to progressive MP4 instead of aborting.
* **Check-and-skip** — an existing non-empty `./downloads/<slug>.mp4` is served immediately,
  skipping both the scrape and yt-dlp (measured: 0.02 s vs 3.1 s). Per-slug locks stop
  concurrent duplicate downloads.

## Configuration

| Env var | Default | Purpose |
| --- | --- | --- |
| `ISL_BASE_URL` | `https://indiansignlanguage.org` | Upstream origin |
| `ISL_INDEX_URL` | `<base>/search-dictionary/` | Dictionary index page |
| `ISL_INDEX_SELECTOR` | `ul.az-columns li a[href]` | Primary index selector |
| `ISL_DOWNLOAD_DIR` | `./downloads` (`/data/downloads` in Docker) | Video cache |
| `ISL_PROFILE_DIR` | `./.browser-profile` (`/data/browser-profile`) | Browser profile (keeps CF cookie) |
| `ISL_CACHE_TTL` | `21600` | Index cache TTL, seconds |
| `ISL_PAGE_CONCURRENCY` | `4` | Max concurrent browser tabs |
| `ISL_NAV_TIMEOUT_MS` | `90000` | Playwright navigation timeout |
| `ISL_CHALLENGE_TIMEOUT` | `120` | Max seconds to wait out a challenge |
| `ISL_YTDLP_TIMEOUT` | `120` | yt-dlp socket timeout |
| `ISL_DOWNLOAD_ATTEMPTS` | `3` | yt-dlp retries on transient YouTube 403s |
| `ISL_WORKERS` | `1` | Uvicorn workers (Linux only; see below) |
| `ISL_HEADLESS` | `1` | Set `0` for a visible browser when debugging |
| `ISL_PROXY` | — | Proxy for the browser |
| `CLOAKBROWSER_LICENSE_KEY` | — | Pro key; lifts the 1-concurrent-session limit |
| `ISL_HOST` / `ISL_PORT` | `0.0.0.0` / `6767` | Bind address |
| `ISL_LOG_LEVEL` | `INFO` | Log level |

## Run locally

```bash
uv sync
```

Or with pip:

```bash
pip install -r requirements.txt
```

Install ffmpeg for best quality (`apt install ffmpeg`, `brew install ffmpeg`, or
`winget install Gyan.FFmpeg`). Without it the app still works, at progressive-MP4 quality.

```bash
python main.py
```

Serves on `http://0.0.0.0:6767`. First run downloads the stealth Chromium (~150 MB) once.

### Windows notes

Start with `python main.py`, **not** the `uvicorn` CLI. Uvicorn switches to
`SelectorEventLoop` whenever it needs subprocesses (`--reload`, `--workers > 1`), and that
loop cannot spawn Chromium on Windows (`NotImplementedError`). `main.py` patches uvicorn's
loop factory to `ProactorEventLoop` at import time, which also covers reload children.

If the app is ever hosted on a selector loop anyway, `/healthz` reports a clear
`last_error` instead of a stack trace.

`ISL_WORKERS > 1` is refused on Windows and falls back to 1: the Proactor loop cannot share
a listening socket across worker processes (`OSError WinError 87`). Scale out with
containers instead. On Linux, multi-worker works and each worker automatically gets its own
Chromium profile directory (`<ISL_PROFILE_DIR>/p<pid>`), since Chromium locks its
user-data dir.

### About YouTube 403s

YouTube intermittently rejects bursts of requests from one IP; the same URL succeeds a few
seconds later. `download_video()` retries with a linear backoff
(`ISL_DOWNLOAD_ATTEMPTS`, default 3) rather than surfacing a transient failure.

## Run with Docker

```bash
docker build -t isl-dataset-api:latest .
```

```bash
docker run -d --name isl-api -p 6767:6767 --shm-size=1g -v isl-data:/data isl-dataset-api:latest
```

Or with compose (ports, `/data` volume, and `shm_size` already wired):

```bash
docker compose up --build -d
```

Image notes: multi-stage `python:3.11-slim`; installs `ffmpeg` plus the Chromium shared
libraries and fonts; **bakes the stealth Chromium binary into the image** at build time
(`cloakbrowser.ensure_binary()`) so startup does not download it; runs as non-root uid 10001;
`VOLUME /data`, `EXPOSE 6767`, `HEALTHCHECK` on `/healthz`.

Uvicorn runs **1 worker** deliberately — each worker process would launch its own browser and
the free CloakBrowser tier permits one concurrent session. Scale with replicas plus a license
key. `--shm-size=1g` matters: Chromium crashes on Docker's default 64 MB `/dev/shm`.

State lives only in the `/data` volume, so the image itself is stateless and safe to push to
GHCR and redeploy.

## Test with curl

```bash
curl -s http://localhost:6767/healthz
```

```bash
curl -s http://localhost:6767/fetch
```

```bash
curl -sL -o apple.mp4 -D - "http://localhost:6767/fetch?word=apple"
```

```bash
curl -sG -o alphabet.mp4 --data-urlencode "word=A (Alphabet)" http://localhost:6767/fetch
```

```bash
curl -si "http://localhost:6767/fetch?word=" ; curl -si "http://localhost:6767/fetch?word=zzzznotaword"
```

```bash
curl -s -X POST http://localhost:6767/admin/refresh
```

## Verification status

Live run against the real site, all checks passing:

* `GET /fetch` → **3185 slugs**, `abbreviation` … `zoo`, no nav slugs leaking in. Cold
  start (challenge + parse) ~10 s; cached thereafter.
* `GET /fetch?word=  ApPle ` → **200**, `video/mp4`, `attachment; filename="apple.mp4"`,
  223 307 bytes with a valid `ftyp` MP4 header, in 3.1 s.
* Repeat request served from disk in **0.02 s** (no scrape, no yt-dlp).
* `400` on empty and malformed `word`; `404` on unknown word; `/healthz` reports a live
  browser and a 3185-entry cache.
* Verified under `ISL_RELOAD=1` on Windows (browser launches, download returns 223 307
  bytes), and the `ISL_WORKERS=2` guard falls back to 1 with a warning.
* Retry path exercised in a real run: a YouTube 403 was retried and the request still
  returned 200.

Not verified here: `docker build` — the Docker daemon was not running on this machine.
