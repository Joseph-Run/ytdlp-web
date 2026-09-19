"""yt-dlp Web — Streamlit front-end for the yt-dlp download engine.

Deployed on Streamlit Community Cloud; engine.py holds the actual work.
"""

from __future__ import annotations

import shutil
import tempfile
import time
from pathlib import Path

import streamlit as st

import engine

st.set_page_config(page_title="yt-dlp Web", page_icon="⬇️", layout="centered")

# Browser-side handoff is the only way out of the container, so everything the
# user gets passes through memory once per file. Keep a free tier (about 1 GB)
# alive by refusing to buffer more than this.
MAX_FILE_BYTES = 400 * 1024 * 1024
MAX_TOTAL_BYTES = 900 * 1024 * 1024
LOG_LINES_KEPT = 300
LOG_RENDER_INTERVAL = 0.5  # seconds between log repaints


def session_dir() -> Path:
    """A private scratch directory for this browser session."""
    if "workdir" not in st.session_state:
        st.session_state.workdir = tempfile.mkdtemp(prefix="ytdlpweb-")
    return Path(st.session_state.workdir)


def reset_outputs() -> None:
    """Drop the previous run's files (and the bytes held for them)."""
    st.session_state.results = []
    old = st.session_state.get("workdir")
    if old:
        shutil.rmtree(old, ignore_errors=True)
    st.session_state.workdir = tempfile.mkdtemp(prefix="ytdlpweb-")
    st.session_state.log_lines = []


def render_log() -> None:
    box = st.session_state.get("log_box")
    if box is None:
        return
    lines = st.session_state.log_lines[-LOG_LINES_KEPT:]
    box.code("\n".join(lines) if lines else "(nothing yet)", language="text")


def log(message: str) -> None:
    st.session_state.log_lines.append(str(message).rstrip())
    now = time.monotonic()
    if now - st.session_state.get("last_log_render", 0) > LOG_RENDER_INTERVAL:
        st.session_state.last_log_render = now
        render_log()


def store_results(files: list[Path]) -> None:
    """Read finished files into session state so their buttons survive a rerun."""
    results = []
    total = 0
    for path in files:
        try:
            size = path.stat().st_size
        except OSError:
            continue
        if size > MAX_FILE_BYTES or total + size > MAX_TOTAL_BYTES:
            results.append({"name": path.name, "size": size, "data": None,
                            "mime": engine.mime_for(path)})
            continue
        total += size
        results.append({"name": path.name, "size": size, "data": path.read_bytes(),
                        "mime": engine.mime_for(path)})
    st.session_state.results = results


# ------------------------------------------------------------------- defaults

for key, value in {
    "results": [],
    "log_lines": [],
    "last_log_render": 0.0,
    "cancel_requested": False,
    "log_box": None,
}.items():
    st.session_state.setdefault(key, value)

workdir = session_dir()

st.title("⬇️ yt-dlp Web")
st.caption(
    "Paste video or playlist URLs and download them as video or audio. "
    "Web port of a Tkinter desktop app: the server fetches the media and hands "
    "it straight back to your browser."
)

with st.expander("Read this before you paste a YouTube link"):
    st.markdown(
        "- **This runs on someone else's computer.** The link you paste and the "
        "media it produces go through a shared cloud server, not your machine. "
        "Use the desktop app for anything private.\n"
        "- **YouTube often refuses cloud IPs.** Streamlit's servers sit in a "
        "datacenter, and YouTube answers those with *\"Sign in to confirm you're "
        "not a bot\"* even when the same link works at home. Uploading a "
        "`cookies.txt` below is the usual workaround; plenty of other sites work "
        "with no cookies at all.\n"
        "- **Storage is temporary.** Files live in a scratch directory for this "
        "browser session only and are deleted when you clear results.\n"
        "- **Respect the rules you're operating under.** Downloading content you "
        "don't own, or that a site's terms forbid, is on you — not on this app."
    )

# ---------------------------------------------------------------------- inputs

url_text = st.text_area(
    "Video / playlist URLs — one per line",
    height=110,
    placeholder="https://www.youtube.com/watch?v=...",
)

col_mode, col_quality = st.columns(2)
with col_mode:
    mode_label = st.radio("Mode", ["Video", "Audio only"], horizontal=True)
with col_quality:
    if mode_label == "Video":
        video_quality = st.selectbox("Quality", list(engine.VIDEO_QUALITY_OPTIONS.keys()))
        audio_format, audio_quality = "mp3", "5 (default)"
    else:
        video_quality = "Best available"
        audio_format = st.selectbox("Audio format", engine.AUDIO_FORMAT_OPTIONS)
        audio_quality = st.selectbox("Audio quality",
                                     list(engine.AUDIO_QUALITY_OPTIONS.keys()), index=2)

col_a, col_b, col_c = st.columns(3)
with col_a:
    subtitles = st.checkbox("Embed English subtitles", value=False)
with col_b:
    thumbnail = st.checkbox("Embed thumbnail", value=False)
with col_c:
    metadata = st.checkbox("Embed metadata", value=True)

playlist = st.checkbox("Download full playlist", value=False,
                       help="Off: only the video in the link. On: the whole playlist.")
max_items = st.slider("Max items per playlist", 1, 100, 25, disabled=not playlist)

template = st.text_input("Filename template", value=engine.DEFAULT_OUTPUT_TEMPLATE,
                         help="yt-dlp output template: %(title)s, %(id)s, %(ext)s …")

cookies_file = st.file_uploader(
    "cookies.txt (optional, Netscape format)",
    type=["txt"],
    help="Only needed for sites that demand a signed-in session (YouTube bot "
         "checks). Export it from a browser where you are logged in.",
)
if cookies_file is not None:
    st.warning(
        "A cookie file is a live session token: uploading it lets this server act "
        "as your logged-in browser on that site. Prefer a throwaway account and "
        "delete the file when you're done."
    )

if not engine.ffmpeg_available():
    st.error(
        "ffmpeg is missing on this server, so merging separate video+audio streams "
        "and audio conversion will fail. On Streamlit Community Cloud, add "
        "`ffmpeg` to `packages.txt`."
    )

col_run, col_cancel, col_clear = st.columns([2, 1, 1])
run = col_run.button("⬇ Download", type="primary", width="stretch")
cancel = col_cancel.button("Cancel", width="stretch")
clear = col_clear.button("Clear results", width="stretch")

# The log container has to exist before the download starts writing into it, so
# it is created here rather than at the bottom with the results.
with st.expander("Log", expanded=False):
    st.session_state.log_box = st.container()
render_log()

# ------------------------------------------------------------------------- run

if clear:
    reset_outputs()
    st.rerun()

if cancel:
    st.session_state.cancel_requested = True
    log("Cancel requested — the running download stops at its next progress tick.")

if run:
    urls = engine.parse_urls(url_text)
    if not urls:
        st.warning("Please enter at least one URL.")
    else:
        reset_outputs()
        st.session_state.cancel_requested = False
        workdir = session_dir()

        cookiefile = None
        if cookies_file is not None:
            cookiefile = workdir / "cookies.txt"
            cookiefile.write_bytes(cookies_file.getvalue())

        settings = engine.DownloadSettings(
            mode="audio" if mode_label == "Audio only" else "video",
            video_quality=video_quality,
            audio_format=audio_format,
            audio_quality=audio_quality,
            playlist=playlist,
            max_items=max_items,
            subtitles=subtitles,
            thumbnail=thumbnail,
            metadata=metadata,
            template=template,
        ).normalized()

        bar = st.progress(0.0, text="Starting…")
        log(f"yt-dlp {engine.yt_dlp.version.__version__} · mode {settings.mode} · "
            f"{len(urls)} URL(s)")
        log("This can take a while: the server downloads the file first, then "
            "sends it to you.")

        def on_progress(percent, text):
            bar.progress(0.0 if percent is None else min(percent, 100.0) / 100.0,
                         text=text)

        started = time.monotonic()
        result = engine.download(
            urls,
            settings,
            workdir,
            on_log=log,
            on_progress=on_progress,
            should_cancel=lambda: bool(st.session_state.get("cancel_requested")),
            cookiefile=cookiefile,
        )
        elapsed = time.monotonic() - started

        render_log()
        st.session_state.cancel_requested = False
        store_results(result.files)

        if result.cancelled:
            bar.progress(0.0, text="Cancelled")
            st.warning("Download cancelled.")
        elif result.ok and result.files:
            bar.progress(1.0, text="Done")
            st.success(f"Finished in {elapsed:.0f}s — {len(result.files)} file(s) below.")
        elif result.ok:
            bar.progress(0.0, text="Nothing new")
            st.info("yt-dlp finished but produced no new files — check the log.")
        else:
            bar.progress(0.0, text="Finished with errors")
            st.error("Finished with errors — see the log above.")

# --------------------------------------------------------------------- results

results = st.session_state.get("results") or []
if results:
    st.subheader("Your files")
    st.caption("These live on the server for this session only — download them now.")
    for item in results:
        if item["data"] is None:
            st.warning(
                f"{item['name']} is {engine.human_size(item['size'])} — too large to "
                "hand back through a free container. Run the desktop app instead, or "
                "pick a lower quality / audio only."
            )
            continue
        st.download_button(
            f"⬇ {item['name']}  ({engine.human_size(item['size'])})",
            data=item["data"],
            file_name=item["name"],
            mime=item["mime"],
            width="stretch",
        )
