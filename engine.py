"""yt-dlp download engine.

Deliberately imports no Streamlit: the widget layer is disposable, this module
is not. Everything here is exercised by test_engine.py with no browser, no
network and no running server.

Ported from Youtubedownload.pyw, which drove yt-dlp from a Tkinter GUI and
wrote files straight to a user-chosen folder. A web app has no visitor
filesystem and the server's disk is ephemeral, so `build_opts()` takes an
output directory and `download()` reports the files it produced, which the UI
hands back to the browser as download buttons.
"""

from __future__ import annotations

import os
import shutil
from dataclasses import dataclass, field
from pathlib import Path

import yt_dlp

# --------------------------------------------------------------------- options

VIDEO_QUALITY_OPTIONS = {
    "Best available": "bestvideo*+bestaudio/best",
    "2160p (4K) or lower": "bestvideo[height<=2160]+bestaudio/best[height<=2160]",
    "1440p (2K) or lower": "bestvideo[height<=1440]+bestaudio/best[height<=1440]",
    "1080p or lower": "bestvideo[height<=1080]+bestaudio/best[height<=1080]",
    "720p or lower": "bestvideo[height<=720]+bestaudio/best[height<=720]",
    "480p or lower": "bestvideo[height<=480]+bestaudio/best[height<=480]",
    "360p or lower": "bestvideo[height<=360]+bestaudio/best[height<=360]",
}

AUDIO_FORMAT_OPTIONS = ["mp3", "m4a", "opus", "flac", "wav", "aac", "vorbis", "best"]

AUDIO_QUALITY_OPTIONS = {
    "0 (best)": "0",
    "2": "2",
    "5 (default)": "5",
    "7": "7",
    "9 (smallest)": "9",
}

DEFAULT_OUTPUT_TEMPLATE = "%(title).150B [%(id)s].%(ext)s"

# Intermediate/junk files yt-dlp leaves behind in a still-running download.
DISCARD_SUFFIXES = {".part", ".ytdl", ".temp", ".tmp", ".json", ".description"}

MIME_TYPES = {
    ".mp4": "video/mp4",
    ".mkv": "video/x-matroska",
    ".webm": "video/webm",
    ".mov": "video/quicktime",
    ".avi": "video/x-msvideo",
    ".mp3": "audio/mpeg",
    ".m4a": "audio/mp4",
    ".opus": "audio/opus",
    ".ogg": "audio/ogg",
    ".flac": "audio/flac",
    ".wav": "audio/wav",
    ".aac": "audio/aac",
    ".vtt": "text/vtt",
    ".srt": "application/x-subrip",
    ".ass": "text/plain",
}


class DownloadCancelled(Exception):
    """Raised inside a progress hook to abort an in-flight download."""


def ffmpeg_available() -> bool:
    """ffmpeg is required to merge streams and to convert/extract audio."""
    return shutil.which("ffmpeg") is not None


def human_size(num_bytes) -> str:
    if not num_bytes:
        return "?"
    num_bytes = float(num_bytes)
    for unit in ("B", "KB", "MB", "GB", "TB"):
        if num_bytes < 1024:
            return f"{num_bytes:.1f}{unit}"
        num_bytes /= 1024
    return f"{num_bytes:.1f}PB"


def mime_for(path) -> str:
    return MIME_TYPES.get(Path(path).suffix.lower(), "application/octet-stream")


def parse_urls(text) -> list[str]:
    """One URL per line, blanks and surrounding whitespace dropped."""
    return [line.strip() for line in (text or "").splitlines() if line.strip()]


def _slug(url: str) -> str:
    return url if len(url) <= 80 else url[:77] + "..."


# ------------------------------------------------------------------- settings


@dataclass
class DownloadSettings:
    mode: str = "video"  # "video" | "audio"
    video_quality: str = "Best available"
    audio_format: str = "mp3"
    audio_quality: str = "5 (default)"
    playlist: bool = False
    max_items: int = 25
    subtitles: bool = False
    thumbnail: bool = False
    metadata: bool = True
    template: str = DEFAULT_OUTPUT_TEMPLATE

    def normalized(self) -> "DownloadSettings":
        """Clamp anything the UI (or a caller) got wrong."""
        return DownloadSettings(
            mode="audio" if self.mode == "audio" else "video",
            video_quality=self.video_quality if self.video_quality in VIDEO_QUALITY_OPTIONS
            else "Best available",
            audio_format=self.audio_format if self.audio_format in AUDIO_FORMAT_OPTIONS
            else "mp3",
            audio_quality=self.audio_quality if self.audio_quality in AUDIO_QUALITY_OPTIONS
            else "5 (default)",
            playlist=bool(self.playlist),
            max_items=max(1, min(int(self.max_items or 1), 100)),
            subtitles=bool(self.subtitles),
            thumbnail=bool(self.thumbnail),
            metadata=bool(self.metadata),
            template=(self.template or "").strip() or DEFAULT_OUTPUT_TEMPLATE,
        )


@dataclass
class DownloadResult:
    ok: bool = True
    cancelled: bool = False
    files: list[Path] = field(default_factory=list)
    errors: list[str] = field(default_factory=list)


# ------------------------------------------------------------------ yt-dlp glue


def build_opts(
    settings: DownloadSettings,
    out_dir,
    progress_hook=None,
    logger=None,
    cookiefile=None,
) -> dict:
    """Translate settings into a yt-dlp option dict.

    Same logic as the desktop original, minus the folder picker and minus the
    "pip install -U yt-dlp" escape hatch (the server redeploys from git).
    """
    settings = settings.normalized()
    out_dir = Path(out_dir)
    out_dir.mkdir(parents=True, exist_ok=True)

    opts = {
        "outtmpl": os.path.join(str(out_dir), settings.template),
        "noplaylist": not settings.playlist,
        "ignoreerrors": True,
        "quiet": True,
        "no_warnings": False,
        "retries": 3,
        "fragment_retries": 3,
        # Server-side rendering: force names that also survive a trip back to
        # Windows, whatever the container's filesystem would allow.
        "windowsfilenames": True,
        "progress_hooks": [progress_hook] if progress_hook else [],
        "postprocessors": [],
    }

    if settings.playlist:
        # Uncapped playlists are how a free container gets OOM-killed.
        opts["playlist_items"] = f"1:{settings.max_items}"

    if logger is not None:
        opts["logger"] = logger

    if settings.mode == "audio":
        opts["format"] = "bestaudio/best"
        opts["postprocessors"].append({
            "key": "FFmpegExtractAudio",
            "preferredcodec": settings.audio_format if settings.audio_format != "best" else None,
            "preferredquality": AUDIO_QUALITY_OPTIONS[settings.audio_quality],
        })
    else:
        opts["format"] = VIDEO_QUALITY_OPTIONS[settings.video_quality]
        opts["merge_output_format"] = "mp4"

    if settings.subtitles:
        opts["writesubtitles"] = True
        opts["writeautomaticsub"] = True
        opts["subtitleslangs"] = ["en"]
        opts["postprocessors"].append({"key": "FFmpegEmbedSubtitle"})

    if settings.thumbnail:
        opts["writethumbnail"] = True
        opts["postprocessors"].append({"key": "EmbedThumbnail"})

    if settings.metadata:
        opts["postprocessors"].append({"key": "FFmpegMetadata", "add_metadata": True})

    if cookiefile:
        opts["cookiefile"] = str(cookiefile)

    return opts


def snapshot(out_dir) -> set[str]:
    """Names of files already present, so leftovers can be told from results."""
    out_dir = Path(out_dir)
    if not out_dir.is_dir():
        return set()
    return {p.name for p in out_dir.iterdir() if p.is_file()}


def collect_new_files(out_dir, before: set[str]) -> list[Path]:
    """New, complete media files produced in out_dir, newest last."""
    out_dir = Path(out_dir)
    if not out_dir.is_dir():
        return []
    found = [
        p for p in out_dir.iterdir()
        if p.is_file()
        and p.name not in before
        and p.suffix.lower() not in DISCARD_SUFFIXES
        and p.stat().st_size > 0
    ]
    return sorted(found, key=lambda p: p.stat().st_mtime)


class _ForwardingLogger:
    """Sends yt-dlp's own log lines to the UI callback.

    yt-dlp is run with ignoreerrors=True so one dead URL in a playlist doesn't
    abort the rest — which means it swallows DownloadError internally and just
    logs it. Recording error lines here is what keeps `DownloadResult.ok`
    honest: without it, a totally failed download returns ok=True and no files.
    """

    def __init__(self, on_log):
        self.on_log = on_log
        self.errors = []

    def debug(self, msg):
        if msg.startswith("[debug] "):
            return
        self.on_log(msg)

    def info(self, msg):
        self.on_log(msg)

    def warning(self, msg):
        self.on_log(f"WARNING: {msg}")

    def error(self, msg):
        self.errors.append(str(msg))
        self.on_log(f"ERROR: {msg}")


def download(
    urls,
    settings: DownloadSettings,
    out_dir,
    on_log=lambda msg: None,
    on_progress=lambda percent, text: None,
    should_cancel=lambda: False,
    cookiefile=None,
    ydl_factory=None,
) -> DownloadResult:
    """Run the downloads for `urls`.

    on_progress(percent, text): percent is 0-100 or None when the total size is
    unknown. should_cancel() is polled on every progress tick, which is what
    makes the UI's Cancel button work.
    """
    ydl_factory = ydl_factory or yt_dlp.YoutubeDL
    result = DownloadResult()
    before = snapshot(out_dir)

    def progress_hook(d):
        if should_cancel():
            raise DownloadCancelled("Cancelled by user")
        status = d.get("status")
        if status == "downloading":
            total = d.get("total_bytes") or d.get("total_bytes_estimate")
            downloaded = d.get("downloaded_bytes") or 0
            percent = (downloaded / total * 100) if total else None
            speed = d.get("speed")
            eta = d.get("eta")
            template = d.get("info_dict", {}).get("title") or os.path.basename(d.get("filename") or "")
            text = (f"{template[:60]} — "
                    f"{human_size(speed) + '/s' if speed else '?'} "
                    f"(ETA {str(eta) + 's' if eta is not None else '?'})")
            on_progress(percent, text)
        elif status == "finished":
            on_progress(100.0, "Post-processing (merging / converting)…")
        elif status == "error":
            on_progress(None, "Error during download")

    logger = _ForwardingLogger(on_log)
    try:
        opts = build_opts(settings, out_dir, progress_hook=progress_hook,
                          logger=logger, cookiefile=cookiefile)
    except Exception as exc:  # pragma: no cover - defensive
        result.ok = False
        result.errors.append(f"Could not prepare download options: {exc}")
        return result

    for url in parse_urls(urls) if isinstance(urls, str) else list(urls):
        if should_cancel():
            result.cancelled = True
            break
        on_log(f"=== Fetching: {_slug(url)} ===")
        try:
            with ydl_factory(opts) as ydl:
                ydl.download([url])
        except DownloadCancelled:
            result.cancelled = True
            on_log("Cancelled.")
            break
        except yt_dlp.utils.DownloadError as exc:
            result.ok = False
            result.errors.append(str(exc))
            on_log(f"ERROR: {exc}")
        except Exception as exc:
            result.ok = False
            result.errors.append(str(exc))
            on_log(f"Unexpected error: {exc}")

    # Errors yt-dlp swallowed because of ignoreerrors=True (see _ForwardingLogger).
    for message in logger.errors:
        if message not in result.errors:
            result.errors.append(message)
    if logger.errors:
        result.ok = False

    result.files = collect_new_files(out_dir, before)
    if result.cancelled:
        result.ok = False
        on_progress(None, "Cancelled")
    elif result.ok:
        on_progress(100.0, "Done")
    else:
        on_progress(None, "Finished with errors — see log")
    return result
