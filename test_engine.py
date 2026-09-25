"""Engine tests — no network, no browser, no Streamlit.

Run:  python test_engine.py
"""

from __future__ import annotations

import json
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path

import yt_dlp

import engine

FAILURES = []
CHECKS = 0


def check(label, condition):
    global CHECKS
    CHECKS += 1
    if condition:
        print(f"  ok   {label}")
    else:
        print(f"  FAIL {label}")
        FAILURES.append(label)


def tmpdir():
    return Path(tempfile.mkdtemp(prefix="ytdlp-test-"))


# --------------------------------------------------------------- pure helpers

def test_parse_urls():
    print("parse_urls")
    urls = engine.parse_urls("  https://a.example/1 \n\n\thttps://b.example/2\n   \n")
    check("drops blanks and whitespace", urls == ["https://a.example/1", "https://b.example/2"])
    check("handles None", engine.parse_urls(None) == [])
    check("handles empty string", engine.parse_urls("") == [])


def test_human_size():
    print("human_size")
    check("zero/None -> ?", engine.human_size(0) == "?" and engine.human_size(None) == "?")
    check("bytes", engine.human_size(512) == "512.0B")
    check("kilobytes", engine.human_size(2048) == "2.0KB")
    check("megabytes", engine.human_size(5 * 1024 * 1024) == "5.0MB")


def test_mime_and_ffmpeg():
    print("mime_for / ffmpeg_available")
    check("mp4", engine.mime_for("a.mp4") == "video/mp4")
    check("MP3 uppercase", engine.mime_for("A.MP3") == "audio/mpeg")
    check("unknown -> octet-stream", engine.mime_for("a.xyz") == "application/octet-stream")
    check("ffmpeg_available is a bool", isinstance(engine.ffmpeg_available(), bool))


# ------------------------------------------------------------- option building

def test_build_opts_video():
    print("build_opts — video")
    opts = engine.build_opts(engine.DownloadSettings(), tmpdir())
    check("format asks for H.264 + AAC first, then falls back",
          opts["format"] == "bestvideo[vcodec^=avc1]+bestaudio[ext=m4a]/"
                             "bestvideo[vcodec^=avc1]+bestaudio/bestvideo*+bestaudio/best")
    check("merges to mp4", opts["merge_output_format"] == "mp4")
    check("single video, not playlist", opts["noplaylist"] is True)
    check("metadata postprocessor on by default",
          {"key": "FFmpegMetadata", "add_metadata": True} in opts["postprocessors"])
    check("no subtitle options", "writesubtitles" not in opts)
    check("no thumbnail options", "writethumbnail" not in opts)
    check("no playlist cap when playlist off", "playlist_items" not in opts)
    check("windows-safe filenames", opts["windowsfilenames"] is True)
    check("outtmpl points into the output dir", str(tmpdir()) != "" and opts["outtmpl"].endswith(".%(ext)s"))


def test_build_opts_quality():
    print("build_opts — quality selection")
    opts = engine.build_opts(engine.DownloadSettings(video_quality="720p or lower"), tmpdir())
    check("720p asks for H.264 first, then falls back, then gives up on the ceiling",
          opts["format"] == "bestvideo[vcodec^=avc1][height<=720]+bestaudio[ext=m4a]/"
                             "bestvideo[vcodec^=avc1][height<=720]+bestaudio/"
                             "bestvideo[height<=720]+bestaudio/best[height<=720]/best")
    opts = engine.build_opts(
        engine.DownloadSettings(video_quality="720p or lower", video_codec="original"), tmpdir()
    )
    check("original codec keeps the plain 720p selector",
          opts["format"] == "bestvideo[height<=720]+bestaudio/best[height<=720]/best")
    opts = engine.build_opts(engine.DownloadSettings(video_quality="nonsense"), tmpdir())
    check("bad quality falls back to best",
          opts["format"] == engine.format_selector("Best available", prefer_h264=True))
    opts = engine.build_opts(engine.DownloadSettings(video_quality="2160p (4K) or lower"), tmpdir())
    check("a 4K request keeps 4K (the conversion happens after the download)",
          opts["format"] == engine.format_selector("2160p (4K) or lower") ==
                            "bestvideo[height<=2160]+bestaudio/best[height<=2160]/best")


def test_format_selector():
    print("format_selector")
    for label, height in engine.VIDEO_QUALITY_OPTIONS.items():
        plain = engine.format_selector(label)
        avc1 = engine.format_selector(label, prefer_h264=True)
        ceiling = "" if height is None else f"[height<={height}]"
        h264 = f"bestvideo[{engine.AVC1_FILTER}]{ceiling}"
        check(f"{label}: plain selector has no codec filter", "avc1" not in plain)
        if height is None or height <= engine.H264_MAX_HEIGHT:
            check(f"{label}: H.264 selector prefers avc1 video with AAC audio, then avc1 "
                  f"with any audio, then the site's own best",
                  avc1 == f"{h264}+bestaudio[{engine.AUDIO_COMPAT_FILTER}]/{h264}+bestaudio/{plain}")
        else:
            check(f"{label}: above {engine.H264_MAX_HEIGHT}p the ceiling wins — no silent "
                  f"downgrade to 1080p H.264, the real stream gets converted instead",
                  avc1 == plain)
    check("unknown label behaves like Best available",
          engine.format_selector("nope") == "bestvideo*+bestaudio/best")
    check("a height ceiling keeps an unconstrained last resort (sources with no height metadata)",
          engine.format_selector("1080p or lower").endswith("/best")
          and engine.format_selector("1080p or lower", prefer_h264=True).endswith("/best"))
    check("the H.264 chain never loses the plain selector as a fallback",
          engine.format_selector("Best available", prefer_h264=True).endswith(
              engine.format_selector("Best available")))


def test_build_opts_audio():
    print("build_opts — audio")
    opts = engine.build_opts(
        engine.DownloadSettings(mode="audio", audio_format="flac", audio_quality="0 (best)"), tmpdir()
    )
    check("bestaudio format", opts["format"] == "bestaudio/best")
    pp = opts["postprocessors"][0]
    check("FFmpegExtractAudio first", pp["key"] == "FFmpegExtractAudio")
    check("codec = flac", pp["preferredcodec"] == "flac")
    check("quality = 0", pp["preferredquality"] == "0")
    check("no merge format for audio", "merge_output_format" not in opts)

    opts = engine.build_opts(engine.DownloadSettings(mode="audio", audio_format="best"), tmpdir())
    check("'best' leaves the codec alone", opts["postprocessors"][0]["preferredcodec"] is None)

    opts = engine.build_opts(engine.DownloadSettings(mode="audio", audio_format="nope"), tmpdir())
    check("bad audio format falls back to mp3", opts["postprocessors"][0]["preferredcodec"] == "mp3")


def test_build_opts_extras():
    print("build_opts — subtitles / thumbnail / playlist / cookies")
    opts = engine.build_opts(engine.DownloadSettings(subtitles=True, thumbnail=True, metadata=False), tmpdir())
    check("writes subtitles", opts["writesubtitles"] is True and opts["writeautomaticsub"] is True)
    check("english subs only", opts["subtitleslangs"] == ["en"])
    check("writes thumbnail", opts["writethumbnail"] is True)
    keys = [pp["key"] for pp in opts["postprocessors"]]
    check("embeds subtitles", "FFmpegEmbedSubtitle" in keys)
    check("embeds thumbnail", "EmbedThumbnail" in keys)
    check("metadata off means no FFmpegMetadata", "FFmpegMetadata" not in keys)

    opts = engine.build_opts(engine.DownloadSettings(playlist=True, max_items=7), tmpdir())
    check("playlist allowed", opts["noplaylist"] is False)
    check("items capped at 1:7", opts["playlist_items"] == "1:7")

    cookies = tmpdir() / "cookies.txt"
    cookies.write_text("# Netscape HTTP Cookie File\n")
    opts = engine.build_opts(engine.DownloadSettings(), tmpdir(), cookiefile=cookies)
    check("cookiefile passed through", opts["cookiefile"] == str(cookies))
    check("no cookiefile when none given", "cookiefile" not in engine.build_opts(engine.DownloadSettings(), tmpdir()))


def test_settings_normalized():
    print("DownloadSettings.normalized")
    s = engine.DownloadSettings(mode="weird", video_quality="?", audio_format="?", audio_quality="?",
                                max_items=9999, template="   ").normalized()
    check("bad mode -> video", s.mode == "video")
    check("bad quality -> best", s.video_quality == "Best available")
    check("bad format -> mp3", s.audio_format == "mp3")
    check("bad quality label -> default", s.audio_quality == "5 (default)")
    check("max_items clamped to 100", s.max_items == 100)
    check("blank template -> default", s.template == engine.DEFAULT_OUTPUT_TEMPLATE)
    check("max_items floor of 1", engine.DownloadSettings(max_items=0).normalized().max_items == 1)
    check("bad codec -> h264", engine.DownloadSettings(video_codec="mpeg2").normalized().video_codec == "h264")
    check("codec survives normalization",
          engine.DownloadSettings(video_codec="original").normalized().video_codec == "original")
    check("H.264 is the default and reports prefers_h264",
          engine.DownloadSettings().prefer_h264
          and not engine.DownloadSettings(video_codec="original").prefer_h264)


def test_logger_routing():
    print("_ForwardingLogger")
    seen = []
    lg = engine._ForwardingLogger(seen.append)
    lg.debug("[debug] noisy internal detail")
    lg.debug("[youtube] real info line")
    lg.info("plain info")
    lg.warning("careful")
    lg.error("broken")
    check("debug lines dropped", seen[0] == "[youtube] real info line")
    check("info routed", "plain info" in seen)
    check("warning prefixed", "WARNING: careful" in seen)
    check("error prefixed", "ERROR: broken" in seen)


# -------------------------------------------------------------- file collection

def test_collect_new_files():
    print("collect_new_files")
    d = tmpdir()
    (d / "old.mp4").write_bytes(b"x")
    before = engine.snapshot(d)
    (d / "new.mp4").write_bytes(b"y" * 10)
    (d / "half.mp4.part").write_bytes(b"z")
    (d / "info.info.json").write_bytes(b"{}")
    (d / "empty.mp4").write_bytes(b"")
    files = [p.name for p in engine.collect_new_files(d, before)]
    check("new complete file reported", "new.mp4" in files)
    check(".part excluded", not any(f.endswith(".part") for f in files))
    check(".json excluded", not any(f.endswith(".json") for f in files))
    check("zero-byte excluded", "empty.mp4" not in files)
    check("pre-existing file excluded", "old.mp4" not in files)
    check("snapshot of a missing dir is empty", engine.snapshot(tmpdir() / "nope") == set())


# ------------------------------------------------------------- H.264 conversion

def run_ffmpeg(*args):
    proc = subprocess.run([shutil.which("ffmpeg"), "-y", "-nostdin", "-loglevel", "error", *args],
                          capture_output=True, text=True)
    if proc.returncode != 0:
        raise RuntimeError(proc.stderr.strip()[-400:])
    return proc


def make_clip(path, vcodec, acodec=None, seconds=1):
    """A real, tiny video: 1 s of test pattern, optionally with a sine track."""
    path = Path(path)
    args = ["-f", "lavfi", "-i", f"testsrc=size=128x72:rate=10:duration={seconds}"]
    if acodec:
        args += ["-f", "lavfi", "-i", f"sine=frequency=440:duration={seconds}",
                 "-map", "0:v:0", "-map", "1:a:0", "-c:a", acodec, "-shortest"]
    args += ["-c:v", vcodec]
    if vcodec == "libx264":
        args += ["-pix_fmt", "yuv420p"]
    if path.suffix.lower() == ".mp4" and vcodec != "libx264":
        # VP9/AV1 inside MP4 still sits behind ffmpeg's experimental gate. Sites
        # serve exactly that (Instagram does), so the tests must build it too.
        args += ["-strict", "-2"]
    args.append(str(path))
    run_ffmpeg(*args)
    return path


def stream_codecs(path):
    proc = subprocess.run([shutil.which("ffprobe"), "-v", "error",
                           "-show_entries", "stream=codec_type,codec_name", "-of", "json", str(path)],
                          capture_output=True, text=True)
    streams = json.loads(proc.stdout or "{}").get("streams") or []
    return {s.get("codec_type"): s.get("codec_name") for s in streams}


def video_packet_sizes(path):
    """Sizes of the encoded video packets — identical iff the stream was copied."""
    proc = subprocess.run([shutil.which("ffprobe"), "-v", "error", "-select_streams", "v:0",
                           "-show_entries", "packet=size", "-of", "csv=p=0", str(path)],
                          capture_output=True, text=True)
    return [line.strip() for line in proc.stdout.splitlines() if line.strip()]


def ffmpeg_ready():
    """The app declares ffmpeg a hard dependency, so the conversion tests are too."""
    ready = engine.ffmpeg_available() and engine.ffprobe_available()
    check("ffmpeg + ffprobe available for the conversion tests", ready)
    return ready


def test_is_h264():
    print("is_h264")
    check("h264", engine.is_h264("h264"))
    check("AVC1 uppercase", engine.is_h264("AVC1"))
    check("avc", engine.is_h264(" avc "))
    check("vp9 is not H.264", not engine.is_h264("vp9"))
    check("av1 is not H.264", not engine.is_h264("av01"))
    check("None and empty are not H.264", not engine.is_h264(None) and not engine.is_h264(""))


def test_transcode_to_h264():
    print("transcode_to_h264 (real ffmpeg)")
    if not ffmpeg_ready():
        return
    d = tmpdir()
    src = make_clip(d / "input.webm", "libvpx-vp9", acodec="libopus")
    check("fixture really is VP9/Opus", engine.probe_video_codec(src) == "vp9"
          and stream_codecs(src)["audio"] == "opus")
    logs = []
    out = engine.transcode_to_h264(src, on_log=logs.append)
    check("output is an mp4 named after the source", out.name == "input.mp4" and out.exists())
    check("output is really H.264", engine.probe_video_codec(out) == "h264")
    check("audio survived and is now AAC (playable in MP4)",
          stream_codecs(out).get("audio") == "aac")
    data = out.read_bytes()
    check("+faststart puts the moov atom before the media data",
          0 <= data.find(b"moov") < data.find(b"mdat"))
    check("the source was left in place for the caller to delete", src.exists())
    check("conversion logged", any("Converting" in m for m in logs))


def test_probe_streams():
    print("probe_streams")
    if not ffmpeg_ready():
        return
    d = tmpdir()
    both = make_clip(d / "both.webm", "libvpx-vp9", acodec="libopus")
    check("reads the video codec", engine.probe_streams(both)["video"] == "vp9")
    check("reads the audio codec", engine.probe_streams(both)["audio"] == "opus")
    check("probe_video_codec agrees", engine.probe_video_codec(both) == "vp9")

    silent = make_clip(d / "silent.mp4", "libx264")
    check("a file with no audio reports None for audio", engine.probe_streams(silent)["audio"] is None)
    check("and still reports the video codec", engine.probe_streams(silent)["video"] == "h264")

    junk = d / "junk.mp4"
    junk.write_bytes(b"not media")
    check("an unreadable file reports both as None",
          engine.probe_streams(junk) == {"video": None, "audio": None})


def test_h264_command():
    print("h264_command")
    reencode = engine.h264_command("ffmpeg", "in.mkv", "out.mp4")
    copy = engine.h264_command("ffmpeg", "in.mp4", "out.mp4", copy_video=True)
    check("re-encode uses libx264", "libx264" in reencode and "copy" not in reencode)
    check("re-encode forces yuv420p", "-pix_fmt" in reencode and "yuv420p" in reencode)
    check("copy mode copies the video stream", "-c:v" in copy and "copy" in copy
          and "libx264" not in copy and "-pix_fmt" not in copy)
    check("both encode audio to AAC", "aac" in reencode and "aac" in copy)
    check("both use faststart", "+faststart" in reencode and "+faststart" in copy)
    check("both take only the first video and optional audio streams",
          reencode.count("-map") == 2 and copy.count("-map") == 2)


def test_ensure_h264_remuxes_incompatible_audio():
    print("ensure_h264 — H.264 video with Opus audio")
    if not ffmpeg_ready():
        return
    d = tmpdir()
    src = make_clip(d / "clip.mp4", "libx264", acodec="libopus")
    streams = engine.probe_streams(src)
    check("fixture is H.264 video with Opus audio",
          streams["video"] == "h264" and streams["audio"] == "opus")
    packets_before = video_packet_sizes(src)

    logs = []
    out = engine.ensure_h264(src, on_log=logs.append)
    check("a second MP4 is produced beside the original", out != src and out.exists())
    check("its audio is AAC",
          engine.probe_streams(out)["audio"] == "aac" and engine.probe_streams(out)["video"] == "h264")
    check("the video stream was copied, not re-encoded (packet sizes identical)",
          video_packet_sizes(out) == packets_before)
    check("the log explains it only touched the audio",
          any("remuxing the audio" in m for m in logs))


def test_ensure_h264_is_idempotent():
    print("ensure_h264")
    if not ffmpeg_ready():
        return
    d = tmpdir()
    already = make_clip(d / "already.mp4", "libx264", acodec="aac")
    size_before = already.stat().st_size
    logs = []
    same = engine.ensure_h264(already, on_log=logs.append)
    check("an H.264 + AAC file is returned untouched", same == already)
    check("and nothing is rewritten", already.stat().st_size == size_before
          and not list(d.glob("*.h264.mp4")) and not list(d.glob("*.aac.mp4")))
    check("the log says no conversion was needed", any("already H.264" in m for m in logs))

    other = d / "subs.srt"
    other.write_text("1\n00:00:00,000 --> 00:00:01,000\nhi\n")
    check("a non-video file is left alone", engine.ensure_h264(other) == other)


def test_unreadable_file_fails_loudly():
    print("ensure_h264 — unreadable input")
    if not ffmpeg_ready():
        return
    d = tmpdir()
    broken = d / "broken.mp4"
    broken.write_bytes(b"this is not a video")
    try:
        engine.ensure_h264(broken, on_log=lambda m: None)
        check("a file ffmpeg cannot read raises", False)
    except RuntimeError as exc:
        check("a file ffmpeg cannot read raises with ffmpeg's own message",
              bool(str(exc)) and "not a video" not in str(exc))
    check("no half-written or empty mp4 is left behind",
          not list(d.glob("*.h264.mp4")) and broken.exists())


def test_convert_files_skips_after_cancel():
    print("convert_files_to_h264 — cancel")
    d = tmpdir()
    a = d / "a.webm"
    a.write_bytes(b"x")
    result = engine.DownloadResult()
    logs = []
    returned = engine.convert_files_to_h264([a], result, logs.append, lambda p, t: None, lambda: True)
    check("nothing is converted once cancel is set", returned == [a] and a.exists())
    check("a cancel is not a failure", result.ok is True and result.errors == [])
    check("the user is told what was left alone", any("Cancel requested" in m for m in logs))


# ---------------------------------------------------------------- fake yt-dlp

class FakeYDL:
    """Stands in for yt_dlp.YoutubeDL: writes a file, touches no network."""

    behavior = "ok"
    seen_opts = []

    def __init__(self, opts):
        self.opts = opts
        FakeYDL.seen_opts.append(opts)

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False

    def download(self, urls):
        outdir = Path(self.opts["outtmpl"]).parent
        if FakeYDL.behavior == "raise":
            raise yt_dlp.utils.DownloadError("HTTP Error 403")
        if FakeYDL.behavior == "raise+log":
            self.opts["logger"].error("HTTP Error 403")
            raise yt_dlp.utils.DownloadError("HTTP Error 403")
        if FakeYDL.behavior == "log-error":
            # This is what ignoreerrors=True actually does: swallow it and log.
            self.opts["logger"].error("Requested format is not available")
            return
        if FakeYDL.behavior == "boom":
            raise RuntimeError("unexpected")
        if FakeYDL.behavior == "hook-cancel":
            hook = self.opts["progress_hooks"][0]
            hook({"status": "downloading", "downloaded_bytes": 50, "total_bytes": 100,
                  "speed": 1024 * 500, "eta": 12, "filename": str(outdir / "x.mp4"),
                  "info_dict": {"title": "T"}})
            return
        name = f"Sample Video [{urls[0][-3:]}].mp4"
        (outdir / name).write_bytes(b"m" * 4096)


def plumbing_settings(**kwargs):
    """Settings for the download-plumbing tests.

    FakeYDL writes junk bytes instead of a real clip, so the H.264 default would
    try to re-encode them and (correctly) fail. Conversion has its own tests
    below, run against real ffmpeg-generated clips.
    """
    return engine.DownloadSettings(video_codec="original", **kwargs)


def test_download_success():
    print("download — success path")
    FakeYDL.behavior = "ok"
    FakeYDL.seen_opts = []
    d = tmpdir()
    logs, progress = [], []
    res = engine.download(["https://example.com/aaa"], plumbing_settings(), d,
                          on_log=logs.append, on_progress=lambda p, t: progress.append((p, t)),
                          ydl_factory=FakeYDL)
    check("ok", res.ok is True)
    check("not cancelled", res.cancelled is False)
    check("one file collected", len(res.files) == 1 and res.files[0].name.endswith("].mp4"))
    check("file actually on disk", res.files[0].exists() and res.files[0].stat().st_size == 4096)
    check("url logged", any("https://example.com/aaa" in str(x) for x in logs))
    check("final progress = 100", progress[-1][0] == 100.0)
    check("string input also accepted", engine.download("https://example.com/bbb",
          plumbing_settings(), tmpdir(), ydl_factory=FakeYDL).ok)


def test_download_progress_hook():
    print("download — progress hook math")
    d = tmpdir()
    FakeYDL.behavior = "ok"
    FakeYDL.seen_opts = []  # clear
    FakeYDL.behavior = "hook-cancel"
    FakeYDL.seen_opts = []
    progress = []
    res = engine.download(["https://example.com/ccc"], plumbing_settings(), d,
                          on_progress=lambda p, t: progress.append((p, t)),
                          should_cancel=lambda: True, ydl_factory=FakeYDL)
    check("cancel inside the hook aborts", res.cancelled is True)
    check("cancelled run is not ok", res.ok is False)

    finished = []
    FakeYDL.behavior = "ok"
    res = engine.download(["https://example.com/ddd"], plumbing_settings(), tmpdir(),
                          on_progress=lambda p, t: finished.append((p, t)),
                          ydl_factory=FakeYDL)
    check("successful run reports done", finished[-1][1] == "Done")


def test_download_cancel_between_urls():
    print("download — cancel between URLs")
    FakeYDL.behavior = "ok"
    d = tmpdir()
    calls = {"n": 0}

    def should_cancel():
        calls["n"] += 1
        return calls["n"] > 1  # false for the first URL, true afterwards

    res = engine.download(["https://example.com/one", "https://example.com/two"],
                          plumbing_settings(), d, should_cancel=should_cancel,
                          ydl_factory=FakeYDL)
    check("cancelled flag set", res.cancelled is True)
    check("first URL was downloaded", calls["n"] == 2)
    check("first file still returned", len(res.files) == 1)


def test_download_errors():
    print("download — error paths")
    FakeYDL.behavior = "raise"
    logs = []
    res = engine.download(["https://example.com/eee"], plumbing_settings(), tmpdir(),
                          on_log=logs.append, ydl_factory=FakeYDL)
    check("not ok", res.ok is False)
    check("error recorded", res.errors and "403" in res.errors[0])
    check("error logged", any("ERROR" in x for x in logs))
    check("no files", res.files == [])

    FakeYDL.behavior = "boom"
    res = engine.download(["https://example.com/fff"], plumbing_settings(), tmpdir(),
                          on_log=lambda m: None, ydl_factory=FakeYDL)
    check("unexpected exception contained", res.ok is False and "unexpected" in res.errors[0])

    FakeYDL.behavior = "log-error"
    res = engine.download(["https://example.com/ggg"], plumbing_settings(), tmpdir(),
                          on_log=lambda m: None, ydl_factory=FakeYDL)
    check("error yt-dlp swallowed still flips ok to False", res.ok is False)
    check("swallowed error recorded", any("not available" in e for e in res.errors))
    check("no files", res.files == [])

    FakeYDL.behavior = "raise+log"
    res = engine.download(["https://example.com/hhh"], plumbing_settings(), tmpdir(),
                          on_log=lambda m: None, ydl_factory=FakeYDL)
    check("same error raised and logged is recorded once",
          res.errors.count("HTTP Error 403") == 1)


class ClipWritingYDL(FakeYDL):
    """Writes a real fixture clip, so the conversion step runs for real."""

    source = None
    filename = "Sample Clip [xyz789].webm"

    def download(self, urls):
        outdir = Path(self.opts["outtmpl"]).parent
        shutil.copy(self.source, outdir / ClipWritingYDL.filename)


def test_download_converts_vp9_to_h264():
    print("download — VP9 source gets converted")
    if not ffmpeg_ready():
        return
    d = tmpdir()
    ClipWritingYDL.source = make_clip(d / "fixture.webm", "libvpx-vp9", acodec="libopus")
    ClipWritingYDL.filename = "Sample Clip [xyz789].webm"
    outdir = tmpdir()
    logs, progress = [], []
    res = engine.download(["https://example.com/aaa"], engine.DownloadSettings(), outdir,
                          on_log=logs.append, on_progress=lambda p, t: progress.append((p, t)),
                          ydl_factory=ClipWritingYDL)
    check("ok", res.ok is True)
    check("exactly one file is handed over", len(res.files) == 1)
    check("the WebM was replaced by an MP4",
          res.files[0].suffix == ".mp4" and res.files[0].exists())
    check("the delivered file really is H.264",
          engine.probe_video_codec(res.files[0]) == "h264")
    check("audio made the trip as AAC", stream_codecs(res.files[0]).get("audio") == "aac")
    check("the original VP9 file was deleted, not left for the user to trip over",
          not (outdir / "Sample Clip [xyz789].webm").exists())
    check("only the converted file remains in the output dir",
          [p.name for p in outdir.iterdir()] == [res.files[0].name])
    check("conversion logged", any("Converting" in m for m in logs))
    check("progress told the user it was converting",
          any("H.264" in str(text) for _, text in progress))


def test_download_skips_conversion_when_already_h264():
    print("download — H.264 source is not re-encoded")
    if not ffmpeg_ready():
        return
    d = tmpdir()
    ClipWritingYDL.source = make_clip(d / "fixture.mp4", "libx264", acodec="aac")
    ClipWritingYDL.filename = "Sample Clip [abc123].mp4"
    outdir = tmpdir()
    logs = []
    res = engine.download(["https://example.com/bbb"], engine.DownloadSettings(), outdir,
                          on_log=logs.append, ydl_factory=ClipWritingYDL)
    check("ok", res.ok is True)
    check("the file keeps its own name", res.files[0].name == "Sample Clip [abc123].mp4")
    check("nothing extra was written", len(list(outdir.iterdir())) == 1)
    check("log says no conversion was needed", any("already H.264" in m for m in logs))
    check("no conversion was announced", not any("Converting" in m for m in logs))


def test_download_keeps_original_codec_when_asked():
    print("download — Original codec leaves the file alone")
    if not ffmpeg_ready():
        return
    d = tmpdir()
    ClipWritingYDL.source = make_clip(d / "fixture.webm", "libvpx-vp9")
    ClipWritingYDL.filename = "Sample Clip [def456].webm"
    outdir = tmpdir()
    res = engine.download(["https://example.com/ccc"],
                          engine.DownloadSettings(video_codec="original"), outdir,
                          on_log=lambda m: None, ydl_factory=ClipWritingYDL)
    check("the WebM is handed over untouched", res.files[0].suffix == ".webm")
    check("and is still VP9", engine.probe_video_codec(res.files[0]) == "vp9")

    audio_out = tmpdir()
    res = engine.download(["https://example.com/ccc2"],
                          engine.DownloadSettings(mode="audio"), audio_out,
                          on_log=lambda m: None, ydl_factory=ClipWritingYDL)
    check("audio mode does not run the video conversion",
          res.files[0].suffix == ".webm" and not list(audio_out.glob("*.mp4")))


def test_download_gives_a_converted_mp4_its_own_name():
    print("download — a VP9-in-MP4 source (Instagram) keeps its name")
    if not ffmpeg_ready():
        return
    d = tmpdir()
    ClipWritingYDL.source = make_clip(d / "fixture.mp4", "libvpx-vp9", acodec="libopus")
    ClipWritingYDL.filename = "Video by wilsplendortoys_bicycle [DdrxDS0iFKR].mp4"
    outdir = tmpdir()
    res = engine.download(["https://example.com/ig"], engine.DownloadSettings(), outdir,
                          on_log=lambda m: None, ydl_factory=ClipWritingYDL)
    check("the source really is VP9 inside an .mp4",
          engine.probe_streams(ClipWritingYDL.source)["video"] == "vp9")
    check("the delivered file keeps the natural name — no '.h264.mp4'",
          res.files[0].name == "Video by wilsplendortoys_bicycle [DdrxDS0iFKR].mp4"
          and res.files[0].parent == outdir)
    check("and it really is H.264",
          engine.probe_video_codec(res.files[0]) == "h264")
    check("nothing else is left in the output dir",
          [p.name for p in outdir.iterdir()] == [res.files[0].name])


def test_conversion_failure_is_reported():
    print("download — conversion failure")
    if not ffmpeg_ready():
        return
    d = tmpdir()
    ClipWritingYDL.source = make_clip(d / "fixture.webm", "libvpx-vp9")
    ClipWritingYDL.filename = "Sample Clip [ghi789].webm"

    real = engine.transcode_to_h264

    def explode(*args, **kwargs):
        raise RuntimeError("libx264 exploded")

    engine.transcode_to_h264 = explode
    try:
        res = engine.download(["https://example.com/ddd"], engine.DownloadSettings(), tmpdir(),
                              on_log=lambda m: None, ydl_factory=ClipWritingYDL)
    finally:
        engine.transcode_to_h264 = real

    check("the run is not reported as a success", res.ok is False)
    check("the reason names the file and the failure",
          any("libx264 exploded" in e and "Sample Clip [ghi789].webm" in e for e in res.errors))
    check("the unconverted file is still handed over rather than dropped",
          len(res.files) == 1 and res.files[0].suffix == ".webm")


def main():
    for fn in [
        test_parse_urls,
        test_human_size,
        test_mime_and_ffmpeg,
        test_build_opts_video,
        test_build_opts_quality,
        test_format_selector,
        test_build_opts_audio,
        test_build_opts_extras,
        test_settings_normalized,
        test_logger_routing,
        test_collect_new_files,
        test_is_h264,
        test_probe_streams,
        test_h264_command,
        test_transcode_to_h264,
        test_ensure_h264_remuxes_incompatible_audio,
        test_ensure_h264_is_idempotent,
        test_unreadable_file_fails_loudly,
        test_convert_files_skips_after_cancel,
        test_download_success,
        test_download_progress_hook,
        test_download_cancel_between_urls,
        test_download_errors,
        test_download_converts_vp9_to_h264,
        test_download_skips_conversion_when_already_h264,
        test_download_keeps_original_codec_when_asked,
        test_download_gives_a_converted_mp4_its_own_name,
        test_conversion_failure_is_reported,
    ]:
        fn()

    print()
    if FAILURES:
        print(f"{len(FAILURES)}/{CHECKS} checks FAILED:")
        for f in FAILURES:
            print(f"  - {f}")
        sys.exit(1)
    print(f"all {CHECKS} checks passed")


if __name__ == "__main__":
    main()
