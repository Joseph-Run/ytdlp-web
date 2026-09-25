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

import json
import os
import re
import shutil
import subprocess
from dataclasses import dataclass, field
from pathlib import Path

import yt_dlp

# --------------------------------------------------------------------- options

# Quality labels -> maximum height (None = no ceiling). Stored as numbers rather
# than ready-made format strings so the H.264 preference below can be applied to
# every label without string surgery.
VIDEO_QUALITY_OPTIONS = {
    "Best available": None,
    "2160p (4K) or lower": 2160,
    "1440p (2K) or lower": 1440,
    "1080p or lower": 1080,
    "720p or lower": 720,
    "480p or lower": 480,
    "360p or lower": 360,
}

# What ends up inside the delivered file. H.264 in MP4 is the one combination
# every player, editor and phone plays; the alternative is "whatever the site
# served", which is often VP9/AV1 that Windows and most editors refuse.
VIDEO_CODEC_OPTIONS = {
    "H.264 · MP4 (plays anywhere)": "h264",
    "Original codec (fastest, no re-encode)": "original",
}

# Codec names that mean "already H.264": ffprobe says `h264`, containers and
# some builds say `avc1`/`avc`.
H264_CODECS = {"h264", "avc1", "avc", "x264"}
AVC1_FILTER = "vcodec^=avc1"

# Audio that every player handles inside an MP4. Opus/Vorbis in MP4 is legal and
# plays in VLC or a browser, but not in Windows' own players or most editors, and
# yt-dlp happily pairs an H.264 video stream with an Opus track when you let it
# choose — so the selector asks for m4a and the converter swaps anything else.
COMPATIBLE_AUDIO_CODECS = {"aac", "mp3"}
AUDIO_COMPAT_FILTER = "ext=m4a"

# Where H.264 streams stop existing. YouTube publishes avc1 up to 1080p and only
# VP9/AV1 above it, so a ceiling above this height is satisfied by fetching the
# real stream (whatever codec) and converting it afterwards.
H264_MAX_HEIGHT = 1080

# Video containers we would convert. Anything else (audio, subtitle files) is
# left alone.
VIDEO_SUFFIXES = {".mp4", ".m4v", ".mkv", ".webm", ".mov", ".avi", ".flv", ".ts", ".ogv", ".3gp"}

H264_CRF = "20"
H264_PRESET = "veryfast"
H264_AUDIO_BITRATE = "192k"
FFMPEG_TIMEOUT = 60 * 30  # a long 4K re-encode on a free container


def format_selector(quality_label: str, prefer_h264: bool = False) -> str:
    """yt-dlp `format` string for a quality label, preferring H.264 streams.

    Two rules, because H.264 streams stop at 1080p on YouTube (and most sites):

    * At or below `H264_MAX_HEIGHT`, and for "Best available", the H.264 stream is
      what we ask for — an avc1 video with an m4a (AAC) track first, then avc1
      with whatever audio exists, then the site's own best, then any single file.
      Nothing gets re-encoded.
    * Above it (a 1440p/4K ceiling) the ceiling *is* the request: no H.264 stream
      exists at that size, so the best real stream is fetched and `ensure_h264`
      converts it afterwards instead of handing back a silent downgrade.
    """
    height = VIDEO_QUALITY_OPTIONS.get(quality_label)
    if height is None:
        base = "bestvideo*+bestaudio/best"
    else:
        limit = f"[height<={height}]"
        # The final unconstrained `/best` matters: a plain link to one media file
        # (a Wikimedia .webm, say) reports no height at all, and every
        # `[height<=N]`-filtered alternative would miss it, failing a download
        # that has nothing to choose from in the first place.
        base = f"bestvideo{limit}+bestaudio/best{limit}/best"
    if not prefer_h264 or (height is not None and height > H264_MAX_HEIGHT):
        return base
    limit = "" if height is None else f"[height<={height}]"
    h264 = f"bestvideo[{AVC1_FILTER}]{limit}"
    return f"{h264}+bestaudio[{AUDIO_COMPAT_FILTER}]/{h264}+bestaudio/{base}"

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


def ffprobe_available() -> bool:
    """ffprobe is how we tell an already-H.264 file from one needing a pass."""
    return shutil.which("ffprobe") is not None


def is_h264(codec) -> bool:
    return bool(codec) and str(codec).strip().lower() in H264_CODECS


def probe_streams(path, timeout: int = 60) -> dict:
    """First video and first audio codec in `path`: {"video": name, "audio": name}.

    Missing streams come back as None. ffprobe's JSON is the reliable route; when
    only ffmpeg is present we read both codecs out of its banner instead of
    giving up, because a silent None here would skip a conversion the user asked
    for.
    """
    path = str(path)
    found = {"video": None, "audio": None}

    ffprobe = shutil.which("ffprobe")
    if ffprobe:
        try:
            proc = subprocess.run(
                [ffprobe, "-v", "error", "-show_entries", "stream=codec_type,codec_name",
                 "-of", "json", path],
                capture_output=True, text=True, timeout=timeout,
            )
            if proc.returncode == 0:
                for stream in (json.loads(proc.stdout or "{}") or {}).get("streams") or []:
                    kind = stream.get("codec_type")
                    name = (stream.get("codec_name") or "").strip().lower()
                    if kind in found and found[kind] is None and name:
                        found[kind] = name
                if any(found.values()):
                    return found
        except (OSError, subprocess.SubprocessError, ValueError):
            pass  # fall through to the ffmpeg banner

    ffmpeg = shutil.which("ffmpeg")
    if ffmpeg:
        try:
            proc = subprocess.run([ffmpeg, "-hide_banner", "-i", path],
                                  capture_output=True, text=True, timeout=timeout)
            banner = proc.stderr or ""
            for kind in ("Video", "Audio"):
                match = re.search(rf"Stream #\d+:\d+.*?: {kind}: ([A-Za-z0-9_]+)", banner)
                if match:
                    found[kind.lower()] = match.group(1).strip().lower()
        except (OSError, subprocess.SubprocessError):
            pass
    return found


def probe_video_codec(path, timeout: int = 60):
    """Name of the first video stream's codec in `path`, or None if unknown."""
    return probe_streams(path, timeout=timeout)["video"]


def h264_command(
    ffmpeg,
    source,
    target,
    copy_video: bool = False,
    crf: str = H264_CRF,
    preset: str = H264_PRESET,
    audio_bitrate: str = H264_AUDIO_BITRATE,
) -> list[str]:
    """The ffmpeg argv that turns `source` into an H.264/AAC MP4 at `target`.

    `copy_video=True` keeps the existing H.264 video stream as-is and only
    re-encodes the audio — the case where the video is already fine but the track
    next to it is Opus, which Windows and most editors refuse inside an MP4.
    """
    cmd = [
        str(ffmpeg), "-y", "-nostdin", "-loglevel", "error",
        "-i", str(source),
        # Video + audio only: subtitle/thumbnail streams copied over from a
        # WebM/MKV source make the MP4 mux fail more often than they help.
        "-map", "0:v:0", "-map", "0:a?",
    ]
    if copy_video:
        cmd += ["-c:v", "copy"]
    else:
        # yuv420p keeps 10-bit VP9/AV1 sources playable in hardware decoders.
        cmd += ["-c:v", "libx264", "-preset", preset, "-crf", crf, "-pix_fmt", "yuv420p"]
    cmd += [
        "-c:a", "aac", "-b:a", audio_bitrate,
        # +faststart moves the index to the front so the file streams/seeks from
        # a browser instead of being fetched whole first.
        "-movflags", "+faststart",
        str(target),
    ]
    return cmd


def transcode_to_h264(
    path,
    on_log=lambda msg: None,
    ffmpeg=None,
    copy_video: bool = False,
    crf: str = H264_CRF,
    preset: str = H264_PRESET,
    audio_bitrate: str = H264_AUDIO_BITRATE,
) -> Path:
    """Re-encode `path` to H.264/AAC in MP4 and return the new path.

    Raises RuntimeError with ffmpeg's own message on failure — the caller keeps
    the original file.
    """
    path = Path(path)
    ffmpeg = ffmpeg or shutil.which("ffmpeg")
    if not ffmpeg:
        raise RuntimeError("ffmpeg is not installed, so the video cannot be converted to H.264")

    target = path.with_suffix(".mp4")
    if target == path or target.exists():
        target = path.with_name(f"{path.stem}.{'aac' if copy_video else 'h264'}.mp4")

    cmd = h264_command(ffmpeg, path, target, copy_video=copy_video,
                       crf=crf, preset=preset, audio_bitrate=audio_bitrate)
    if copy_video:
        on_log(f"Video in {path.name} is already H.264 — converting its audio to AAC in MP4…")
    else:
        on_log(f"Converting {path.name} → H.264/MP4 (libx264, crf {crf}, preset {preset})…")
    try:
        proc = subprocess.run(cmd, capture_output=True, text=True, timeout=FFMPEG_TIMEOUT)
    except subprocess.TimeoutExpired:
        target.unlink(missing_ok=True)
        raise RuntimeError(f"ffmpeg timed out after {FFMPEG_TIMEOUT // 60} minutes")
    if proc.returncode != 0 or not target.exists() or target.stat().st_size == 0:
        target.unlink(missing_ok=True)
        detail = (proc.stderr or proc.stdout or "").strip().splitlines()
        raise RuntimeError(detail[-1] if detail else f"ffmpeg exited {proc.returncode}")
    return target


def ensure_h264(path, on_log=lambda msg: None, ffmpeg=None) -> Path:
    """Return an H.264/MP4 version of `path`, converting only when needed.

    A file that is already H.264 *and* carries AAC/MP3 audio is returned
    untouched, so the common YouTube case (avc1 + m4a picked by
    `format_selector`) costs nothing. An H.264 file with an incompatible audio
    track gets a fast audio-only pass instead of a full re-encode.
    """
    path = Path(path)
    if path.suffix.lower() not in VIDEO_SUFFIXES:
        on_log(f"Leaving {path.name} alone — not a video container.")
        return path
    streams = probe_streams(path)
    video, audio = streams["video"], streams["audio"]
    audio_ok = audio is None or audio in COMPATIBLE_AUDIO_CODECS
    if is_h264(video) and audio_ok:
        on_log(f"{path.name} is already H.264 with compatible audio — no conversion needed.")
        return path
    if is_h264(video):
        on_log(f"{path.name} is H.264 but its audio is {audio} — remuxing the audio to AAC.")
        return transcode_to_h264(path, on_log=on_log, ffmpeg=ffmpeg, copy_video=True)
    on_log(f"{path.name} is {video or 'an unknown codec'} — converting to H.264.")
    return transcode_to_h264(path, on_log=on_log, ffmpeg=ffmpeg)


def convert_files_to_h264(files, result, on_log, on_progress, should_cancel) -> list[Path]:
    """H.264/MP4 versions of `files`, converted one at a time.

    Runs after yt-dlp has finished (streams merged, metadata embedded) so the
    conversion sees the final file. A file that is already H.264 passes through
    untouched; a conversion that fails keeps the original file, records the
    reason on `result` and flips `result.ok`, so the UI never claims success for
    a file that isn't what the user asked for.
    """
    converted = []
    total = len(files)
    for index, path in enumerate(files, start=1):
        if should_cancel():
            on_log(f"Cancel requested — leaving {total - index + 1} file(s) unconverted.")
            converted.extend(files[index - 1:])
            break
        try:
            on_progress(None, f"Converting to H.264 ({index}/{total})…")
            new_path = ensure_h264(path, on_log=on_log)
        except Exception as exc:
            result.ok = False
            message = f"H.264 conversion failed for {path.name}: {exc}"
            result.errors.append(message)
            on_log(f"ERROR: {message}")
            converted.append(path)
            continue
        if new_path != path:
            try:
                path.unlink()
                # Only reclaim the source name when the source already ended in
                # the right extension (an .mp4 whose stream we replaced, so the
                # converter had to write "x.h264.mp4"). A WebM source converted to
                # "x.mp4" is already named the way it should be — renaming it back
                # would hand over an .mp4 called ".webm".
                if (new_path.parent == path.parent
                        and new_path.suffix.lower() == path.suffix.lower()
                        and not path.exists()):
                    new_path = new_path.replace(path)
            except OSError as exc:  # the converted file is what matters
                on_log(f"WARNING: could not rename/remove {path.name}: {exc}")
        converted.append(new_path)
    return converted


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
    video_codec: str = "h264"  # "h264" | "original"
    audio_format: str = "mp3"
    audio_quality: str = "5 (default)"
    playlist: bool = False
    max_items: int = 25
    subtitles: bool = False
    thumbnail: bool = False
    metadata: bool = True
    template: str = DEFAULT_OUTPUT_TEMPLATE

    @property
    def prefer_h264(self) -> bool:
        return self.video_codec == "h264"

    def normalized(self) -> "DownloadSettings":
        """Clamp anything the UI (or a caller) got wrong."""
        return DownloadSettings(
            mode="audio" if self.mode == "audio" else "video",
            video_quality=self.video_quality if self.video_quality in VIDEO_QUALITY_OPTIONS
            else "Best available",
            video_codec=self.video_codec if self.video_codec in set(VIDEO_CODEC_OPTIONS.values())
            else "h264",
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
        opts["format"] = format_selector(settings.video_quality, prefer_h264=settings.prefer_h264)
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
    if settings.mode == "video" and settings.prefer_h264 and result.files:
        result.files = convert_files_to_h264(result.files, result, on_log, on_progress, should_cancel)
    if result.cancelled:
        result.ok = False
        on_progress(None, "Cancelled")
    elif result.ok:
        on_progress(100.0, "Done")
    else:
        on_progress(None, "Finished with errors — see log")
    return result
