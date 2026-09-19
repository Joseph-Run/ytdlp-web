"""App tests — runs app.py through Streamlit's own harness.

A 200 from /_stcore/health proves nothing: Streamlit does not execute the
script until a browser session connects, so a broken app still serves a
healthy shell. AppTest actually runs it.

Run:  python test_app.py
"""

from __future__ import annotations

import sys
import tempfile
from pathlib import Path

import yt_dlp
from streamlit.testing.v1 import AppTest

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


class FakeYDL:
    """No network: writes a plausible output file into whatever dir we're given."""

    def __init__(self, opts):
        self.opts = opts

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False

    def download(self, urls):
        outdir = Path(self.opts["outtmpl"]).parent
        (outdir / "Fake Clip [abc123].mp4").write_bytes(b"v" * 8192)


def label_of(element):
    return getattr(element, "label", "") or ""


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
    check("playlist slider disabled while playlist off", at.slider[0].disabled is True)
    check("download button present", button(at, "Download") is not None)
    check("cancel button present", button(at, "Cancel") is not None)
    check("clear button present", button(at, "Clear results") is not None)


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


def test_playlist_slider_enables():
    print("playlist toggle")
    at = fresh_app()
    playlist_box = [c for c in at.checkbox if "playlist" in label_of(c).lower()][0]
    playlist_box.set_value(True).run()
    check("no exception", len(at.exception) == 0)
    check("slider now enabled", at.slider[0].disabled is False)
    check("slider default 25", at.slider[0].value == 25)


def test_download_flow():
    print("download flow (fake yt-dlp)")
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
    for fn in [
        test_boots,
        test_empty_url_is_graceful,
        test_mode_switch_shows_audio_options,
        test_playlist_slider_enables,
        test_download_flow,
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
