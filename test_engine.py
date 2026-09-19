"""Engine tests — no network, no browser, no Streamlit.

Run:  python test_engine.py
"""

from __future__ import annotations

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
    check("format is bestvideo+bestaudio", opts["format"] == "bestvideo*+bestaudio/best")
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
    check("720p format string", opts["format"] == "bestvideo[height<=720]+bestaudio/best[height<=720]")
    opts = engine.build_opts(engine.DownloadSettings(video_quality="nonsense"), tmpdir())
    check("bad quality falls back to best", opts["format"] == engine.VIDEO_QUALITY_OPTIONS["Best available"])


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


def test_download_success():
    print("download — success path")
    FakeYDL.behavior = "ok"
    FakeYDL.seen_opts = []
    d = tmpdir()
    logs, progress = [], []
    res = engine.download(["https://example.com/aaa"], engine.DownloadSettings(), d,
                          on_log=logs.append, on_progress=lambda p, t: progress.append((p, t)),
                          ydl_factory=FakeYDL)
    check("ok", res.ok is True)
    check("not cancelled", res.cancelled is False)
    check("one file collected", len(res.files) == 1 and res.files[0].name.endswith("].mp4"))
    check("file actually on disk", res.files[0].exists() and res.files[0].stat().st_size == 4096)
    check("url logged", any("https://example.com/aaa" in str(x) for x in logs))
    check("final progress = 100", progress[-1][0] == 100.0)
    check("string input also accepted", engine.download("https://example.com/bbb",
          engine.DownloadSettings(), tmpdir(), ydl_factory=FakeYDL).ok)


def test_download_progress_hook():
    print("download — progress hook math")
    d = tmpdir()
    FakeYDL.behavior = "ok"
    FakeYDL.seen_opts = []  # clear
    FakeYDL.behavior = "hook-cancel"
    FakeYDL.seen_opts = []
    progress = []
    res = engine.download(["https://example.com/ccc"], engine.DownloadSettings(), d,
                          on_progress=lambda p, t: progress.append((p, t)),
                          should_cancel=lambda: True, ydl_factory=FakeYDL)
    check("cancel inside the hook aborts", res.cancelled is True)
    check("cancelled run is not ok", res.ok is False)

    finished = []
    FakeYDL.behavior = "ok"
    res = engine.download(["https://example.com/ddd"], engine.DownloadSettings(), tmpdir(),
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
                          engine.DownloadSettings(), d, should_cancel=should_cancel,
                          ydl_factory=FakeYDL)
    check("cancelled flag set", res.cancelled is True)
    check("first URL was downloaded", calls["n"] == 2)
    check("first file still returned", len(res.files) == 1)


def test_download_errors():
    print("download — error paths")
    FakeYDL.behavior = "raise"
    logs = []
    res = engine.download(["https://example.com/eee"], engine.DownloadSettings(), tmpdir(),
                          on_log=logs.append, ydl_factory=FakeYDL)
    check("not ok", res.ok is False)
    check("error recorded", res.errors and "403" in res.errors[0])
    check("error logged", any("ERROR" in x for x in logs))
    check("no files", res.files == [])

    FakeYDL.behavior = "boom"
    res = engine.download(["https://example.com/fff"], engine.DownloadSettings(), tmpdir(),
                          on_log=lambda m: None, ydl_factory=FakeYDL)
    check("unexpected exception contained", res.ok is False and "unexpected" in res.errors[0])

    FakeYDL.behavior = "log-error"
    res = engine.download(["https://example.com/ggg"], engine.DownloadSettings(), tmpdir(),
                          on_log=lambda m: None, ydl_factory=FakeYDL)
    check("error yt-dlp swallowed still flips ok to False", res.ok is False)
    check("swallowed error recorded", any("not available" in e for e in res.errors))
    check("no files", res.files == [])

    FakeYDL.behavior = "raise+log"
    res = engine.download(["https://example.com/hhh"], engine.DownloadSettings(), tmpdir(),
                          on_log=lambda m: None, ydl_factory=FakeYDL)
    check("same error raised and logged is recorded once",
          res.errors.count("HTTP Error 403") == 1)


def main():
    for fn in [
        test_parse_urls,
        test_human_size,
        test_mime_and_ffmpeg,
        test_build_opts_video,
        test_build_opts_quality,
        test_build_opts_audio,
        test_build_opts_extras,
        test_settings_normalized,
        test_logger_routing,
        test_collect_new_files,
        test_download_success,
        test_download_progress_hook,
        test_download_cancel_between_urls,
        test_download_errors,
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
