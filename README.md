# yt-dlp Web

A browser front-end for [yt-dlp](https://github.com/yt-dlp/yt-dlp), running on
Streamlit Community Cloud. Web port of a Tkinter desktop app.

Paste one or more URLs, pick video or audio, hit Download. The server fetches
the media and hands it straight back to your browser.

## What's here

| File | Purpose |
|---|---|
| `app.py` | Streamlit UI: URL box, options, progress bar, log, download buttons. |
| `engine.py` | The engine — option building, format selection, download orchestration, progress, cancellation and the H.264 conversion. No Streamlit import, so it's testable and reusable on its own. |
| `test_engine.py` | Headless test suite for the engine (168 checks, no network; the conversion checks use real ffmpeg-generated clips). |
| `test_app.py` | Runs `app.py` through Streamlit's own `AppTest` harness (59 checks). |
| `requirements.txt` | Deploy dependencies (`streamlit`, `yt-dlp`). |
| `packages.txt` | System packages — `ffmpeg`, required to merge streams, convert audio and re-encode video. |
| `.streamlit/config.toml` | Telemetry off, headless server, upload cap. |

## Video always arrives as H.264

H.264 in MP4 is the one combination every player, editor, phone and browser
plays, so it is what the app delivers by default. Two steps get you there, and
the first is free:

1. **Ask for H.264.** `engine.format_selector()` builds the yt-dlp format string
   with an `avc1` filter — and an `ext=m4a` filter on the audio — in front of the
   normal selector, so wherever the site offers H.264 (YouTube up to 1080p, most
   other sites) that stream plus its AAC track is fetched and merged straight into
   an MP4. Nothing is re-encoded, which is why "Best available" and every ceiling
   up to 1080p are as fast as a plain download.
   A ceiling *above* 1080p (`H264_MAX_HEIGHT`) skips the avc1 filter on purpose:
   no H.264 exists at 1440p/4K, and quietly handing back a 1080p file when 4K was
   asked for would be worse than a slow conversion.
2. **Convert the rest.** `engine.ensure_h264()` probes the finished file's video
   *and* audio streams and does the least work that makes it compatible:
   - already H.264 with AAC/MP3 audio → untouched (the YouTube ≤1080p case);
   - H.264 video but an Opus/Vorbis track (yt-dlp will pair those; Windows and
     most editors refuse Opus-in-MP4) → fast pass with `-c:v copy` and the audio
     re-encoded to AAC;
   - anything else (VP9/AV1, WebM, a 4K ceiling) → full `libx264` re-encode at
     crf 20 / `veryfast` / `yuv420p` with AAC audio and `+faststart`.

Either way the source file is deleted and only the converted file is offered, and
*Original codec* in the UI skips step 2 entirely.

## Run locally

```bash
python -m venv .venv
.venv/Scripts/activate        # source .venv/bin/activate on Linux/macOS
pip install -r requirements.txt
streamlit run app.py
```

Opens at <http://localhost:8501>. `ffmpeg` (and `ffprobe`, which ships with it)
must be on your PATH — without them video+audio merging, audio extraction and
the H.264 conversion all fail, and the app says so at the top.

## Tests

```bash
.venv/Scripts/python.exe test_engine.py
.venv/Scripts/python.exe test_app.py
```

The engine suite covers option building for every mode, format selection (the
H.264 preference and its fallback chain), the playlist cap, settings clamping,
the log router, file collection (`.part`/`.json`/zero-byte filtering) and
download outcomes — success, per-URL cancellation, a `DownloadError`, an
unexpected exception, and the error yt-dlp *swallows* because
`ignoreerrors=True`. The conversion checks run against real clips that the tests
generate with ffmpeg (VP9/Opus and H.264/AAC), so they assert what the delivered
file actually contains: probe the output, not just its name.

The app suite runs `app.py` through `AppTest`, which actually executes the
script. Worth knowing: a 200 from `/_stcore/health` does **not** prove the app
works, because Streamlit only runs your code once a browser session connects —
the server will happily serve a healthy shell over completely broken code.

## Differences from the desktop original

- **Folder picker → download buttons.** A web app can't write to the visitor's
  disk, and the server's disk is temporary. Files land in a per-session scratch
  directory and are streamed to the browser, then dropped. There is a size
  ceiling (300 MB per file, 600 MB per session): every byte crosses the free
  tier's memory twice on the way out, and Community Cloud allots 690 MB–2.7 GB.
  For a 4K download, use the desktop app.
- **"Install / Update yt-dlp" button → gone.** The server installs from
  `requirements.txt` at deploy time; there's no pip at runtime.
- **Playlist cap.** Uncapped playlists are how a 1 GB container gets killed, so
  a playlist download is capped (default 25 items, max 100).
- **Cookies.** The desktop app inherited your logged-in browser for free. Here
  you can upload a `cookies.txt`, which is what gets you past YouTube's bot
  checks from a cloud IP.
- **Video comes out as H.264/MP4, whatever the site served.** The desktop
  original handed over whatever yt-dlp produced (often VP9/AV1 in WebM, which
  Windows and most editors refuse). Here the H.264 video stream and its AAC track
  are preferred during selection, and anything else is converted on the server
  before the download button appears — the video re-encoded, or just the audio
  when that is the only incompatible part. The conversion costs CPU and time
  (roughly as long as the video) and the re-encoded file can be larger than the
  source. *Original codec* turns it off.
- **Cancel** polls a flag on every progress tick instead of setting a
  `threading.Event`.

## Deploy on Streamlit Community Cloud

1. Push this folder to a GitHub repo you own (**admin** rights required).
2. Sign in at <https://share.streamlit.io> with GitHub.
3. **Create app** → pick the repo, branch `main`, main file `app.py` → Deploy.

`packages.txt` is read automatically and installs `ffmpeg`. Public repos are
simplest; the free tier does support private repos, but only **one private app
at a time**, and a private repo's app inherits that privacy — so a tool you
want to share wants a public repo.

Free-tier facts worth knowing (per Streamlit's docs, subject to change):

| | |
|---|---|
| RAM / CPU | 690 MB – 2.7 GB, 0.078 – 2 cores |
| Disk | up to 50 GB |
| Sleep | **after 12 h with no traffic** — any visitor can wake it with one click |
| Reboot | Manage app → ⋮ → Reboot app, when a redeploy seems to serve stale code |

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
- **The H.264 pass needs ffmpeg *and* ffprobe**, and it is the slowest step in
  the app: a 20-minute 1440p VP9 video takes roughly that long to re-encode on
  the free tier's ~1 core, and the result can be several times larger. Pick a
  resolution whose H.264 stream exists (1080p and below on YouTube) or choose
  *Original codec* if your player handles VP9/AV1.
- **A multi-item post is not all or nothing.** Instagram hands a carousel back as
  a playlist, so an item that is an image (or a dead URL in a playlist) ends up in
  the log with *"No video formats found"* while the rest still download. That is
  reported as a partial result — "N file(s) below and ready, M problem(s)" — not
  as a failed run, because the files that came through are the run.
- **This is a tool, not a licence.** Downloading content you don't own, or that
  a site's terms forbid, is on you.
