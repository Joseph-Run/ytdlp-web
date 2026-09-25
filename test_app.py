"""App tests — runs app.py through Streamlit's own harness.

A 200 from /_stcore/health proves nothing: Streamlit does not execute the
script until a browser session connects, so a broken app still serves a
healthy shell. AppTest actually runs it.

Run:  python test_app.py
"""

from __future__ import annotations

import shutil
import subprocess
import sys
import tempfile
from pathlib import Path

import yt_dlp
from streamlit.testing.v1 import AppTest

import engine

APP = str(Path(__file__).resolve().parent / "app.py")

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


def run_ffmpeg(*args):
    proc = subprocess.run([shutil.which("ffmpeg"), "-y", "-nostdin", "-loglevel", "error", *args],
                          capture_output=True, text=True)
    if proc.returncode != 0:
        raise RuntimeError(proc.stderr.strip()[-400:])


def make_clip(path, vcodec, acodec=None, seconds=1):
    """A real, tiny clip — the app's H.264 pass needs something ffmpeg can read."""
    path = Path(path)
    args = ["-f", "lavfi", "-i", f"testsrc=size=128x72:rate=10:duration={seconds}"]
    if acodec:
        args += ["-f", "lavfi", "-i", f"sine=frequency=440:duration={seconds}",
                 "-map", "0:v:0", "-map", "1:a:0", "-c:a", acodec, "-shortest"]
    args += ["-c:v", vcodec]
    if vcodec == "libx264":
        args += ["-pix_fmt", "yuv420p"]
    if path.suffix.lower() == ".mp4" and vcodec != "libx264":
        args += ["-strict", "-2"]  # VP9/AV1 in MP4 is what Instagram serves
    args.append(str(path))
    run_ffmpeg(*args)
    return path


class FakeYDL:
    """No network: copies a real clip into whatever output dir we're given.

    The fixture is genuine (ffmpeg-generated) because the app converts video to
    H.264: an H.264/MP4 fixture exercises the no-op path, a VP9/WebM one the
    re-encode path.
    """

    fixture = None
    filename = "Fake Clip [abc123].mp4"

    def __init__(self, opts):
        self.opts = opts

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False

    def download(self, urls):
        outdir = Path(self.opts["outtmpl"]).parent
        shutil.copy(FakeYDL.fixture, outdir / FakeYDL.filename)


def use_fixture(vcodec, filename, acodec=None, container=None):
    """Point FakeYDL at a fresh clip of the given codec; return its temp dir."""
    d = Path(tempfile.mkdtemp(prefix="ytdlp-fixture-"))
    suffix = container or (".mp4" if vcodec == "libx264" else ".webm")
    FakeYDL.fixture = make_clip(d / f"source{suffix}", vcodec, acodec=acodec)
    FakeYDL.filename = filename
    return d


def label_of(element):
    return getattr(element, "label", "") or ""


H264_LABEL = [label for label, value in engine.VIDEO_CODEC_OPTIONS.items() if value == "h264"][0]
ORIGINAL_LABEL = [label for label, value in engine.VIDEO_CODEC_OPTIONS.items() if value == "original"][0]


def codec_box(at):
    """The video-codec selectbox, or None when it isn't on the page (audio mode)."""
    for box in at.selectbox:
        if label_of(box) == "Video codec":
            return box
    return None


def button(at, text):
    for b in at.button:
        if text.lower() in label_of(b).lower():
            return b
    return None


def fresh_app():
    at = AppTest.from_file(APP, default_timeout=60)
    at.run()
    return at


def test_boots():
    print("app boots")
    at = fresh_app()
    check("no exception on first run", len(at.exception) == 0)
    check("title rendered", any("yt-dlp Web" in t.value for t in at.title))
    check("url box present", len(at.text_area) == 1)
    check("mode radio defaults to Video", at.radio[0].value == "Video")
    check("quality box defaults to Best available",
          at.selectbox[0].value == "Best available")
    check("codec box present in video mode", codec_box(at) is not None)
    check("codec defaults to H.264",
          codec_box(at) is not None and codec_box(at).value == H264_LABEL)
    check("codec box explains what it does", "H.264" in codec_box(at).help)
    check("playlist slider disabled while playlist off", at.slider[0].disabled is True)
    check("download button present", button(at, "Download") is not None)
    check("cancel button present", button(at, "Cancel") is not None)
    check("clear button present", button(at, "Clear results") is not None)


def test_reencode_notice():
    print("re-encode notice above 1080p")
    at = fresh_app()
    check("no notice at Best available",
          not any("re-encoded" in c.value for c in at.caption))
    at.selectbox[0].set_value("1080p or lower").run()
    check("no notice at 1080p (H.264 exists there)",
          not any("re-encoded" in c.value for c in at.caption))
    at.selectbox[0].set_value("2160p (4K) or lower").run()
    check("4K warns that it will be re-encoded",
          any("re-encode" in c.value and "H.264" in c.value for c in at.caption))
    at = fresh_app()
    at.selectbox[0].set_value("2160p (4K) or lower").run()
    at.selectbox[1].set_value(ORIGINAL_LABEL).run()
    check("the notice disappears once the original codec is chosen",
          not any("re-encode" in c.value for c in at.caption))


def test_empty_url_is_graceful():
    print("empty input")
    at = fresh_app()
    button(at, "Download").click().run()
    check("no exception", len(at.exception) == 0)
    check("warns about missing URL",
          any("at least one URL" in w.value for w in at.warning))
    check("no download button offered", len(at.download_button) == 0)


def test_mode_switch_shows_audio_options():
    print("audio mode")
    at = fresh_app()
    at.radio[0].set_value("Audio only").run()
    check("no exception", len(at.exception) == 0)
    values = [s.value for s in at.selectbox]
    check("audio format box appeared", "mp3" in values)
    check("audio quality box appeared", any("(default)" in str(v) for v in values))
    check("quality box gone", not any("Best available" in str(v) for v in values))
    check("codec box gone in audio mode", codec_box(at) is None)


def test_playlist_slider_enables():
    print("playlist toggle")
    at = fresh_app()
    playlist_box = [c for c in at.checkbox if "playlist" in label_of(c).lower()][0]
    playlist_box.set_value(True).run()
    check("no exception", len(at.exception) == 0)
    check("slider now enabled", at.slider[0].disabled is False)
    check("slider default 25", at.slider[0].value == 25)


def test_download_flow():
    print("download flow (fake yt-dlp, real H.264 clip)")
    use_fixture("libx264", "Fake Clip [abc123].mp4", acodec="aac")
    real = yt_dlp.YoutubeDL
    yt_dlp.YoutubeDL = FakeYDL
    try:
        at = fresh_app()
        at.text_area[0].set_value("https://example.com/watch?v=abc123").run()
        button(at, "Download").click().run()
    finally:
        yt_dlp.YoutubeDL = real

    check("no exception", len(at.exception) == 0)
    check("success message", any("Finished" in s.value for s in at.success))
    check("a download button was offered", len(at.download_button) == 1)
    if len(at.download_button):
        check("button names the file",
              "Fake Clip [abc123].mp4" in label_of(at.download_button[0]))
        # 1.64's download_button proto carries only id/label/url/type: the bytes
        # go into a deferred-file record, and in AppTest the url is mock media.
        # What matters is that it is this app's own media, not an upstream link.
        url = str(getattr(at.download_button[0], "url", ""))
        check("button serves this app's media, not an upstream link",
              "/media/" in url and url.endswith(".mp4"))
    check("log mentions the URL", any("example.com" in c.value for c in at.code))
    check("an already-H.264 file is handed over without conversion",
          any("already H.264" in c.value for c in at.code))


def test_vp9_download_is_converted_in_the_ui():
    print("download flow — VP9 source reaches the browser as H.264 MP4")
    use_fixture("libvpx-vp9", "Fake Clip [vp9clip].webm", acodec="libopus")
    real = yt_dlp.YoutubeDL
    yt_dlp.YoutubeDL = FakeYDL
    try:
        at = fresh_app()
        at.text_area[0].set_value("https://example.com/watch?v=vp9clip").run()
        button(at, "Download").click().run()
    finally:
        yt_dlp.YoutubeDL = real

    check("no exception", len(at.exception) == 0)
    check("success message", any("Finished in" in s.value for s in at.success))
    check("one file offered", len(at.download_button) == 1)
    if len(at.download_button):
        check("the offered file is the converted MP4, not the WebM",
              "Fake Clip [vp9clip].mp4" in label_of(at.download_button[0]))
        check("button serves mp4 media",
              str(getattr(at.download_button[0], "url", "")).endswith(".mp4"))
    check("conversion is visible in the log",
          any("Converting" in c.value for c in at.code))


def test_original_codec_skips_conversion():
    print("download flow — Original codec")
    use_fixture("libvpx-vp9", "Fake Clip [keepme].webm")
    real = yt_dlp.YoutubeDL
    yt_dlp.YoutubeDL = FakeYDL
    try:
        at = fresh_app()
        at.text_area[0].set_value("https://example.com/watch?v=keepme").run()
        codec_box(at).set_value(ORIGINAL_LABEL).run()
        button(at, "Download").click().run()
    finally:
        yt_dlp.YoutubeDL = real

    check("no exception", len(at.exception) == 0)
    check("the WebM is handed over as-is",
          len(at.download_button) == 1
          and "Fake Clip [keepme].webm" in label_of(at.download_button[0]))
    check("nothing was converted", not any("Converting" in c.value for c in at.code))


def test_partial_failure_still_offers_the_good_file():
    print("partial failure — Instagram post, second item is an image")
    # Exactly what the live app saw: an .mp4 whose video stream is VP9.
    use_fixture("libvpx-vp9", "Video by wilsplendortoys_bicycle [DdrxDS0iFKR].mp4",
                acodec="libopus", container=".mp4")

    class PartialYDL(FakeYDL):
        """One item downloads, a second has no video (an image in a carousel)."""

        def download(self, urls):
            super().download(urls)
            self.opts["logger"].error("[Instagram] DdrxCjsI-RD: No video formats found!")

    real = yt_dlp.YoutubeDL
    yt_dlp.YoutubeDL = PartialYDL
    try:
        at = fresh_app()
        at.text_area[0].set_value("https://www.instagram.com/p/DdrxO12iBXA/").run()
        button(at, "Download").click().run()
    finally:
        yt_dlp.YoutubeDL = real

    label = label_of(at.download_button[0]) if len(at.download_button) else ""
    check("no exception", len(at.exception) == 0)
    check("the item that worked is still offered, converted",
          "Video by wilsplendortoys_bicycle [DdrxDS0iFKR].mp4" in label)
    check("its name carries no '.h264' machinery suffix", ".h264" not in label)
    check("the failure is a warning, not a red error",
          len(at.error) == 0 and any("problem" in w.value for w in at.warning))
    check("the warning counts both the file and the problem",
          any("1 file(s)" in w.value and "1 problem(s)" in w.value for w in at.warning))
    check("it is not passed off as a plain success",
          not any("Finished in" in s.value for s in at.success))
    check("the underlying reason is in the log",
          any("No video formats" in c.value for c in at.code))


def test_failed_download_reports_error():
    print("failed download (fake yt-dlp)")
    class FailingYDL(FakeYDL):
        def download(self, urls):
            # What a YouTube bot-check looks like from a datacenter IP.
            self.opts["logger"].error("Sign in to confirm you're not a bot")

    real = yt_dlp.YoutubeDL
    yt_dlp.YoutubeDL = FailingYDL
    try:
        at = fresh_app()
        at.text_area[0].set_value("https://example.com/watch?v=abc123").run()
        button(at, "Download").click().run()
    finally:
        yt_dlp.YoutubeDL = real

    check("no exception", len(at.exception) == 0)
    check("error surfaced to the user", any("Finished with errors" in e.value for e in at.error))
    check("no download button for a failed run", len(at.download_button) == 0)
    check("the underlying message is in the log",
          any("not a bot" in c.value for c in at.code))
    check("no false success message", not any("Finished in" in s.value for s in at.success))


def test_results_survive_rerun():
    print("results persist across reruns")
    use_fixture("libx264", "Fake Clip [abc123].mp4")
    real = yt_dlp.YoutubeDL
    yt_dlp.YoutubeDL = FakeYDL
    try:
        at = fresh_app()
        at.text_area[0].set_value("https://example.com/watch?v=abc123").run()
        button(at, "Download").click().run()
        first = len(at.download_button)
        at.run()  # a plain rerun, as if the visitor moved another widget
        second = len(at.download_button)
    finally:
        yt_dlp.YoutubeDL = real
    check("first run offers a file", first == 1)
    check("still offered after a rerun", second == 1)


def test_clear_button():
    print("clear results")
    use_fixture("libx264", "Fake Clip [abc123].mp4")
    real = yt_dlp.YoutubeDL
    yt_dlp.YoutubeDL = FakeYDL
    try:
        at = fresh_app()
        at.text_area[0].set_value("https://example.com/watch?v=abc123").run()
        button(at, "Download").click().run()
        button(at, "Clear results").click().run()
    finally:
        yt_dlp.YoutubeDL = real
    check("no exception", len(at.exception) == 0)
    check("file no longer offered", len(at.download_button) == 0)
    check("log emptied", not any("Fake Clip" in c.value for c in at.code))


def main():
    if not (engine.ffmpeg_available() and engine.ffprobe_available()):
        print("ffmpeg/ffprobe missing — the app converts video to H.264 and the "
              "download flow cannot be tested without them.")
        sys.exit(1)

    for fn in [
        test_boots,
        test_reencode_notice,
        test_empty_url_is_graceful,
        test_mode_switch_shows_audio_options,
        test_playlist_slider_enables,
        test_download_flow,
        test_vp9_download_is_converted_in_the_ui,
        test_original_codec_skips_conversion,
        test_partial_failure_still_offers_the_good_file,
        test_failed_download_reports_error,
        test_results_survive_rerun,
        test_clear_button,
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
