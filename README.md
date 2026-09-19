# yt-dlp Web

A browser front-end for [yt-dlp](https://github.com/yt-dlp/yt-dlp), running on
Streamlit Community Cloud. Web port of a Tkinter desktop app.

Paste one or more URLs, pick video or audio, hit Download. The server fetches
the media and hands it straight back to your browser.

## What's here

| File | Purpose |
|---|---|
| `app.py` | Streamlit UI: URL box, options, progress bar, log, download buttons. |
| `engine.py` | The engine — option building, download orchestration, progress and cancellation. No Streamlit import, so it's testable and reusable on its own. |
| `test_engine.py` | Headless test suite for the engine (78 checks, no network). |
| `test_app.py` | Runs `app.py` through Streamlit's own `AppTest` harness (34 checks). |
| `requirements.txt` | Deploy dependencies (`streamlit`, `yt-dlp`). |
| `packages.txt` | System packages — `ffmpeg`, required to merge streams and convert audio. |
| `.streamlit/config.toml` | Telemetry off, headless server, upload cap. |

## Run locally

```bash
python -m venv .venv
.venv/Scripts/activate        # source .venv/bin/activate on Linux/macOS
pip install -r requirements.txt
streamlit run app.py
```

Opens at <http://localhost:8501>. `ffmpeg` must be on your PATH — without it
video+audio merging and audio extraction fail (the app says so at the top).

## Tests

```bash
.venv/Scripts/python.exe test_engine.py
.venv/Scripts/python.exe test_app.py
```

The engine suite covers option building for every mode, the playlist cap,
settings clamping, the log router, file collection (`.part`/`.json`/zero-byte
filtering) and download outcomes — success, per-URL cancellation, a
`DownloadError`, an unexpected exception, and the error yt-dlp *swallows*
because `ignoreerrors=True`.

The app suite runs `app.py` through `AppTest`, which actually executes the
script. Worth knowing: a 200 from `/_stcore/health` does **not** prove the app
works, because Streamlit only runs your code once a browser session connects —
the server will happily serve a healthy shell over completely broken code.

## Differences from the desktop original

- **Folder picker → download buttons.** A web app can't write to the visitor's
  disk, and the server's disk is temporary. Files land in a per-session scratch
  directory and are streamed to the browser, then dropped. There is a size
  ceiling (400 MB per file, 900 MB per session) because every byte crosses the
  free tier's memory on the way out.
- **"Install / Update yt-dlp" button → gone.** The server installs from
  `requirements.txt` at deploy time; there's no pip at runtime.
- **Playlist cap.** Uncapped playlists are how a 1 GB container gets killed, so
  a playlist download is capped (default 25 items, max 100).
- **Cookies.** The desktop app inherited your logged-in browser for free. Here
  you can upload a `cookies.txt`, which is what gets you past YouTube's bot
  checks from a cloud IP.
- **Cancel** polls a flag on every progress tick instead of setting a
  `threading.Event`.

## Deploy on Streamlit Community Cloud

1. Push this folder to a **public** GitHub repo.
2. Sign in at <https://share.streamlit.io> with GitHub.
3. **Create app** → pick the repo, branch `main`, main file `app.py` → Deploy.

`packages.txt` is read automatically and installs `ffmpeg`. Leave the repo
public: on the free tier, app visibility follows the repo's.

## The honest caveats

- **YouTube often refuses cloud IPs.** Streamlit's servers are in a datacenter,
  and YouTube answers those with *"Sign in to confirm you're not a bot"* even
  when the same link works from your laptop. Uploading a `cookies.txt` is the
  usual fix — and because a cookie file is a live session token, the server can
  then act as your logged-in browser on that site. Use a throwaway account, and
  revoke the session when you're done.
- **Your links and media pass through a shared server**, not your machine. For
  anything private, run the desktop app.
- **yt-dlp breaks on schedule.** Sites change their players; a pinned version
  eventually goes stale. `yt-dlp>=2025.1.1` lets a redeploy pick up fixes.
- **This is a tool, not a licence.** Downloading content you don't own, or that
  a site's terms forbid, is on you.
