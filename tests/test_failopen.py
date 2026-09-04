"""
The fail-open regression.

Measured 2026-09-04 on a machine where the exiftool binary was not on PATH:

    demo.pdf carries SENTINEL_AUTHOR_9871 twice (docinfo /Author + XMP)
      metascrub scrub demo.pdf  ->  "CLEAN [pdf]", "verified clean 1", exit 0
      grep -c SENTINEL_AUTHOR_9871 demo.pdf  ->  2   (file never touched)

The cause was one shared representation for two states that must never be
confused. ExifSession.read() caught every exception and returned an empty dict,
so "we could not read this file" and "this file carries nothing" were the same
value, and the scrubber read that value as CLEAN before any engine ran. It hit
every format, not just images, because all four engines read their baseline
through exiftool even when they write with pikepdf, zipfile or ffmpeg.

These tests assert the invariant over the whole format cross-product rather than
over one illustrative file, because the defect was never specific to PDFs: it
was specific to the read path every format shares.
"""

from __future__ import annotations

import sys

import pytest

from conftest import ALL_KINDS, build, contains_anywhere
from metascrub import exif_io
from metascrub.scrubber import (
    STATUS_CLEAN, STATUS_ERROR, STATUS_SANITIZED, MetadataScrubber,
)


@pytest.fixture
def break_exiftool(monkeypatch):
    """
    Return a callable that makes exiftool unavailable from that point on.

    It is a callable rather than a plain fixture because the fixtures under test
    need a WORKING exiftool to write their sentinel in the first place. Breaking
    it at setup time would break fixture construction instead of the code path
    being tested, and the test would pass for the wrong reason.

    Patching the helper rather than read() is deliberate: it exercises the real
    exception path, so a future change that reintroduces a bare
    `except: return {}` anywhere below this point is still caught.
    """
    def activate():
        def _no_helper(_self):
            raise RuntimeError("exiftool binary not usable: not found on PATH")

        monkeypatch.setattr(
            type(exif_io.session()), "helper", property(_no_helper), raising=True
        )

    return activate


def test_read_reports_failure_rather_than_emptiness(break_exiftool):
    """The two states must be distinguishable at the type level."""
    break_exiftool()
    read = exif_io.session().read("anything.jpg")
    assert read.failed is True
    assert read.ok is False
    assert read.metadata == {}
    assert "exiftool" in read.error
    # The trap the old code fell into: a failed read must not be falsy-empty
    # in the way an empty-but-successful read is.
    assert not bool(read)
    assert bool(exif_io.MetadataRead(metadata={}))


@pytest.mark.parametrize("kind", ALL_KINDS)
def test_unreadable_baseline_is_never_reported_clean(kind, tmp_path, break_exiftool):
    """
    The load-bearing assertion. With metadata unreadable, no file of any format
    may come back CLEAN, SANITIZED, or verified clean.
    """
    path, value = build(kind, tmp_path)
    break_exiftool()

    with MetadataScrubber(backup=False) as scrubber:
        result = scrubber.sanitize_file(path, remove_all=True)

    assert result["status"] == STATUS_ERROR, (
        f"{kind}: unreadable baseline reported as {result['status']!r}; "
        "a file that was never examined must not be reported as handled"
    )
    assert result["status"] not in (STATUS_CLEAN, STATUS_SANITIZED)
    assert (result.get("verification") or {}).get("clean") is not True

    # The error must name the actual cause. Which of the two guards fires
    # depends on the format: for exiftool-backed formats the engine
    # availability check catches it first, for pikepdf/zipfile/ffmpeg formats
    # the engine is genuinely fine and the unreadable baseline is what stops
    # the run. Both are correct, and both must say exiftool.
    assert "exiftool" in result["error"].lower(), result["error"]

    # And the ground truth the whole tool exists to protect: the file was not
    # silently left leaking under a success verdict.
    assert contains_anywhere(path, value), "fixture precondition failed"


@pytest.mark.parametrize("kind", ALL_KINDS)
def test_report_and_exit_status_reflect_the_failure(kind, tmp_path, break_exiftool):
    """
    A batch caller reads the report and the exit code, not the console. Both
    must show the failure, or a pipeline treats an untouched file as scrubbed.
    """
    path, _ = build(kind, tmp_path)
    break_exiftool()

    with MetadataScrubber(backup=False) as scrubber:
        results = [scrubber.sanitize_file(path, remove_all=True)]
        report = scrubber.generate_sanitization_report(results)

    assert report["error_files"] == 1
    assert report["clean_files"] == 0
    assert report["sanitized_files"] == 0
    assert report["verified_clean"] == 0


def test_directory_run_fails_every_file_rather_than_passing_them(tmp_path, break_exiftool):
    """A directory sweep is where a silent pass does the most damage."""
    for kind in (".pdf", ".docx", ".png"):
        build(kind, tmp_path)
    break_exiftool()

    with MetadataScrubber(backup=False) as scrubber:
        results = scrubber.sanitize_directory(str(tmp_path), remove_all=True)

    assert results, "no files were processed"
    assert all(r["status"] == STATUS_ERROR for r in results), (
        [(r["file"], r["status"]) for r in results]
    )
    assert not any((r.get("verification") or {}).get("clean") for r in results)


def test_unreadable_post_write_read_is_not_verified_clean(tmp_path):
    """
    The mirror case. If the file cannot be re-read AFTER the write, the
    read-back half of verification did not happen, so the result must not be
    VERIFIED_CLEAN even when the residual scan finds nothing.
    """
    from metascrub.capabilities import spec_for
    from metascrub.verify import Verdict, verify

    path, _ = build(".pdf", tmp_path)
    spec = spec_for(path)

    verification = verify(
        path, spec, before={}, after={}, after_readable=False
    )
    assert verification.verdict is Verdict.UNVERIFIED
    assert verification.clean is False


# DETACHED PROCESS (no console)

def test_cli_imports_when_there_is_no_stdout():
    """
    A detached GUI process has no console and no redirection, so sys.stdout is
    None rather than a closed file. Importing the CLI must survive that.

    Before this was handled, `pythonw -m metascrub gui` died at import with
    `AttributeError: 'NoneType' object has no attribute 'isatty'` and showed
    nothing at all: no window, and no traceback either, because there was
    nowhere to print one. A launcher using that invocation appeared to do
    nothing whatsoever.
    """
    import importlib
    import sys

    real_stdout, real_stderr = sys.stdout, sys.stderr
    sys.stdout = None
    sys.stderr = None
    try:
        module = importlib.reload(importlib.import_module("metascrub.cli"))
    finally:
        sys.stdout, sys.stderr = real_stdout, real_stderr

    assert module._TTY is False, "no stdout cannot be a terminal"
    # Reload again with the real streams so later tests see a normal module.
    importlib.reload(module)


def test_tty_detection_handles_a_closed_stream():
    """A closed stream raises ValueError, not AttributeError. Both mean 'no'."""
    import io as _io

    from metascrub.cli import _stdout_is_tty

    real = sys.stdout
    closed = _io.StringIO()
    closed.close()
    sys.stdout = closed
    try:
        assert _stdout_is_tty() is False
    finally:
        sys.stdout = real
