"""
The muxer selection, and the four containers it unblocked.

ffmpeg chooses its output muxer from the output filename. av_engine.py writes
to a temp file carrying the input's extension, so for any extension ffmpeg has
no muxer bound to, the remux dies before it starts:

    Unable to choose an output format for 'fixture.qt'
    Error opening output files: Invalid argument

That is not a container ffmpeg cannot handle. Measured 2026-09-04 on ffmpeg
2024-12-11-git-a518b5540d, all four of .qt, .mqv, .lrv and .f4a mux, store
metadata and remux fine the moment the format is named with -f.

Four things are asserted per extension, and the fourth is the one a passing
round trip would otherwise hide:

  1. The fixture really stores the sentinel. Asserted before anything is
     scrubbed, because a fixture that stored nothing gives a test that passes
     by having nothing to remove.
  2. The sentinel is gone from the OUTPUT BYTES. Never a read-back through the
     engine that wrote the file.
  3. ffprobe still parses the result, so the file was not destroyed.
  4. The ftyp brand is unchanged. A scrubber may rebuild a container; it may
     not change which container it is.

Point 4 is why test_the_flavour_survives_whichever_one_it_was exists and why
it builds its own inputs in BOTH flavours rather than reusing the fixtures.
All four of these extensions name ISO base media files in two incompatible
flavours, QuickTime (ftyp brand `qt  `) and ISO/MP4, and extension does not
determine flavour in the wild. A test that only ever feeds the engine the
flavour the engine expects cannot see a wrong choice: measured on a static
extension-to-muxer map, four of the eight extension/flavour combinations came
back with a different ftyp brand than they went in with, and every other
assertion in this file still passed. The cross product is what caught it.
"""

from __future__ import annotations

import os
import subprocess

import pytest

from conftest import FIXTURE_MUXER, HAVE_FFMPEG, build, contains_anywhere, sentinel
from metascrub import STATUS_SANITIZED, MetadataScrubber
from metascrub.engines import av_engine
from metascrub.engines.av_engine import (
    MUXER_FOR_EXT, QUICKTIME_BRAND, AvEngine, ftyp_brand, output_format_args)

pytestmark = pytest.mark.skipif(not HAVE_FFMPEG, reason="ffmpeg not available")

# The extensions this change shipped. Named literally rather than read from
# MUXER_FOR_EXT, so deleting a map entry breaks this suite instead of quietly
# shrinking it. A test list that shrinks with the thing it tests asserts
# progressively less while staying green.
MUXER_EXTENSIONS = [".qt", ".mqv", ".lrv", ".f4a"]

# The two flavours of ISO base media these extensions are written in, and the
# ffmpeg muxer that produces each. Stated here from the format, not read from
# the engine, so this file has an opinion of its own to check the engine
# against.
FLAVOURS = {"mov": b"qt  ", "mp4": b"isom"}


def read_brand(path: str) -> bytes:
    """
    The ftyp major brand, read straight out of the bytes.

    Deliberately not ffprobe and not the engine's own helper. What is being
    guarded against is the engine handing a file to the wrong muxer, so the
    bytes are the primary source and no tool's opinion is needed.
    """
    with open(path, "rb") as fh:
        head = fh.read(16)
    assert head[4:8] == b"ftyp", f"{path} does not start with an ftyp box"
    return head[8:12]


def ffprobe_ok(path: str) -> bool:
    """Does an independent demuxer still parse this file."""
    proc = subprocess.run(
        ["ffprobe", "-v", "error", "-show_entries", "format=format_name",
         "-of", "default=nw=1", path],
        capture_output=True, text=True)
    return proc.returncode == 0 and "format_name=" in (proc.stdout or "")


@pytest.mark.parametrize("ext", MUXER_EXTENSIONS)
def test_the_sentinel_is_gone_from_the_bytes(ext, tmp_path):
    """The load-bearing assertion. The value the file carried must not survive."""
    path, value = build(ext, tmp_path)
    assert contains_anywhere(path, value), (
        f"{ext} fixture precondition failed; there is nothing to remove and "
        "therefore nothing this test could prove")

    with MetadataScrubber(backup=False) as scrubber:
        result = scrubber.sanitize_file(path, remove_all=True)

    assert result["status"] == STATUS_SANITIZED, result.get("error")
    assert not contains_anywhere(path, value), (
        f"{ext}: the metadata value survived sanitizing; the file still leaks")


@pytest.mark.parametrize("ext", MUXER_EXTENSIONS)
def test_the_output_still_parses(ext, tmp_path):
    path, _ = build(ext, tmp_path)
    with MetadataScrubber(backup=False) as scrubber:
        scrubber.sanitize_file(path, remove_all=True)
    assert ffprobe_ok(path), f"{ext}: ffprobe cannot parse the scrubbed file"


@pytest.mark.parametrize("ext", MUXER_EXTENSIONS)
def test_the_verifier_agrees_with_the_bytes(ext, tmp_path):
    """A verdict that disagrees with the bytes is worse than none, being trusted."""
    path, value = build(ext, tmp_path)
    with MetadataScrubber(backup=False) as scrubber:
        result = scrubber.sanitize_file(path, remove_all=True)
    assert result["verification"]["clean"] is (not contains_anywhere(path, value))
    assert result["verification"]["verdict"] == "verified_clean"


# THE FLAVOUR CROSS PRODUCT
#
# The boundary audit, and the reason the engine reads the input instead of
# trusting the extension.

def _build_in_flavour(tmp_path, ext, muxer):
    """A file with this extension deliberately written in THIS flavour."""
    path = str(tmp_path / f"flavour_{muxer}{ext}")
    value = sentinel("flavour" + muxer + ext.lstrip("."))
    source = (["-f", "lavfi", "-i", "sine=frequency=440:duration=1"]
              if ext == ".f4a" else
              ["-f", "lavfi", "-i", "testsrc=duration=1:size=64x64:rate=10",
               "-pix_fmt", "yuv420p"])
    subprocess.run(
        ["ffmpeg", "-y", "-loglevel", "error"] + source +
        ["-metadata", f"title={value}", "-metadata", f"artist={value}",
         "-f", muxer, path],
        check=True, capture_output=True)
    assert read_brand(path) == FLAVOURS[muxer], (
        f"{ext}: the {muxer} muxer did not produce the flavour this test "
        "expects, so the case below would not be testing what it says")
    assert contains_anywhere(path, value), f"{ext}/{muxer} stored no sentinel"
    return path, value


@pytest.mark.parametrize("ext", MUXER_EXTENSIONS)
@pytest.mark.parametrize("muxer", sorted(FLAVOURS))
def test_the_flavour_survives_whichever_one_it_was(ext, muxer, tmp_path):
    """
    Both flavours of each extension must come back as themselves.

    This is the case a static extension map gets wrong, and gets wrong
    silently: the sentinel still leaves, the file still parses, and the user
    still gets a QuickTime file back as an MP4 or the reverse. Removing
    metadata is not licence to change the container.
    """
    path, value = _build_in_flavour(tmp_path, ext, muxer)
    before = read_brand(path)

    with MetadataScrubber(backup=False) as scrubber:
        result = scrubber.sanitize_file(path, remove_all=True)

    assert result["status"] == STATUS_SANITIZED, result.get("error")
    assert not contains_anywhere(path, value), (
        f"{ext} written as {muxer}: the sentinel survived")
    assert ffprobe_ok(path), f"{ext} written as {muxer}: unparseable output"
    assert read_brand(path) == before, (
        f"{ext} written as {muxer}: ftyp brand changed from {before!r} to "
        f"{read_brand(path)!r}; the remux wrote a different container than it "
        "read")


# THE SELECTION LOGIC

def test_the_flavour_is_read_from_the_file_not_the_extension():
    """
    State the rule directly, so it cannot be weakened back into a lookup
    without this failing. The extension only decides WHETHER a -f is needed;
    the file decides WHICH.
    """
    assert output_format_args.__doc__, "the selector lost its explanation"
    for ext in MUXER_EXTENSIONS:
        assert MUXER_FOR_EXT[ext] in FLAVOURS, f"{ext} falls back to no flavour"


def test_a_file_that_does_not_say_falls_back_to_the_extension(tmp_path):
    """
    "Could not tell" and "is ISO" must not share a representation. A file with
    no ftyp box is the first; answering mp4 for it would be the second, and
    would be a guess wearing the clothes of a measurement.
    """
    for ext in MUXER_EXTENSIONS:
        p = str(tmp_path / f"nobox{ext}")
        with open(p, "wb") as fh:
            fh.write(b"not an ISO base media file at all")
        assert ftyp_brand(p) is None
        assert output_format_args(p, ext) == ["-f", MUXER_FOR_EXT[ext]]

    missing = str(tmp_path / "absent.qt")
    assert ftyp_brand(missing) is None, "an unreadable file must not claim a brand"
    assert output_format_args(missing, ".qt") == ["-f", "mov"]


def test_the_brand_reader_is_not_fooled_by_a_short_or_shifted_file(tmp_path):
    for payload in (b"", b"\x00\x00\x00\x14ftyp", b"\x00\x00\x00\x14moovqt  aaaa"):
        p = str(tmp_path / "odd.qt")
        with open(p, "wb") as fh:
            fh.write(payload)
        assert ftyp_brand(p) is None, f"claimed a brand from {payload!r}"


def test_a_quicktime_input_selects_the_mov_muxer(tmp_path):
    """The mapping from brand to muxer, pinned in both directions."""
    for muxer, brand in FLAVOURS.items():
        p = str(tmp_path / f"stub_{muxer}.lrv")
        with open(p, "wb") as fh:
            fh.write(b"\x00\x00\x00\x14ftyp" + brand + b"\x00\x00\x02\x00")
        assert output_format_args(p, ".lrv") == ["-f", muxer], (
            f"brand {brand!r} should select {muxer} regardless of the .lrv "
            "fallback")
    assert QUICKTIME_BRAND == b"qt  "


def test_an_unmapped_extension_still_lets_ffmpeg_infer(tmp_path):
    """
    The map is an exception list, not a registry. The containers that already
    worked must keep working by inference, or this change would have quietly
    required an entry for every format.
    """
    p = str(tmp_path / "any.bin")
    with open(p, "wb") as fh:
        fh.write(b"\x00\x00\x00\x14ftyp" + QUICKTIME_BRAND + b"\x00\x00\x02\x00")
    for ext in (".mp4", ".mkv", ".mp3", ".flac", ".ts", ".aiff", ""):
        assert output_format_args(p, ext) == [], f"{ext} should be inferred"
    assert output_format_args(p, ".QT") == ["-f", "mov"], "lookup must fold case"


def test_the_engine_map_and_the_fixture_map_agree():
    """
    conftest builds these fixtures through its own copy of the fallback map, on
    purpose, so a fixture is never produced by the code under test. That
    independence is only worth having if drift is caught.
    """
    assert MUXER_FOR_EXT == FIXTURE_MUXER, (
        "av_engine.MUXER_FOR_EXT and conftest.FIXTURE_MUXER disagree; one of "
        f"them is wrong: {MUXER_FOR_EXT} vs {FIXTURE_MUXER}")


def test_every_shipped_extension_has_a_fallback():
    missing = [e for e in MUXER_EXTENSIONS if e not in MUXER_FOR_EXT]
    assert not missing, f"shipped without a fallback muxer: {missing}"


# BOTH COMMAND LINES

def _capture_ffmpeg_argv(monkeypatch):
    """Record every ffmpeg command line the engine builds, and still run it."""
    calls = []
    real_run = subprocess.run

    def recording_run(cmd, **kwargs):
        calls.append(list(cmd))
        return real_run(cmd, **kwargs)

    monkeypatch.setattr(av_engine.subprocess, "run", recording_run)
    return calls


def _assert_format_flag_precedes_the_output(argv, ext, expected_muxer):
    assert "-f" in argv, f"{ext}: the command line carries no -f\n{argv}"
    i = argv.index("-f")
    assert argv[i + 1] == expected_muxer, (
        f"{ext}: -f {argv[i + 1]}, expected {expected_muxer}")
    # ffmpeg applies -f to the file that FOLLOWS it. A -f after the output path
    # is not an output option at all, and the command would fail exactly the
    # way it did before any of this existed.
    assert i < len(argv) - 1, f"{ext}: -f is not before the output path\n{argv}"
    assert argv[-1].lower().endswith(ext), f"{ext}: output path is {argv[-1]}"


@pytest.mark.parametrize("ext", MUXER_EXTENSIONS)
def test_strip_all_names_the_muxer(ext, tmp_path, monkeypatch):
    path, _ = build(ext, tmp_path)
    calls = _capture_ffmpeg_argv(monkeypatch)
    AvEngine().strip_all(path)
    assert calls, "the engine ran no ffmpeg command"
    _assert_format_flag_precedes_the_output(calls[0], ext, MUXER_FOR_EXT[ext])


@pytest.mark.parametrize("ext", MUXER_EXTENSIONS)
def test_the_retry_path_names_the_muxer_too(ext, tmp_path, monkeypatch):
    """
    _retry_without_bitexact builds a SECOND command line from scratch. Forget
    the flag there and the bug hides until a muxer rejects -bitexact, which is
    the only time that path runs, and by then the failure reads as a bad file
    rather than as a missing argument.

    Exercised directly rather than by arranging for the first attempt to fail,
    so the test does not depend on which muxers happen to reject -bitexact on
    the ffmpeg build in front of it.
    """
    path, value = build(ext, tmp_path)
    calls = _capture_ffmpeg_argv(monkeypatch)

    removed = AvEngine()._retry_without_bitexact(path, ext, "forced by the test")

    assert removed, "the retry path reported removing nothing"
    assert calls, "the retry path ran no ffmpeg command"
    _assert_format_flag_precedes_the_output(calls[0], ext, MUXER_FOR_EXT[ext])
    assert "-bitexact" not in calls[0], "the retry must not re-send -bitexact"
    # The retry is a real scrub, not just a command line. Same standard.
    assert not contains_anywhere(path, value), (
        f"{ext}: the retry path left the sentinel in the bytes")
    assert ffprobe_ok(path), f"{ext}: the retry path produced an unparseable file"


@pytest.mark.parametrize("ext", MUXER_EXTENSIONS)
def test_without_the_flag_ffmpeg_refuses(ext, tmp_path):
    """
    The premise, measured rather than quoted from a comment.

    If ffmpeg ever starts inferring a muxer for these extensions, the whole
    mechanism becomes dead weight and this says so, instead of leaving a future
    reader to guess whether it is still load bearing.
    """
    src, _ = build(ext, tmp_path)
    out = str(tmp_path / f"inferred{ext}")
    proc = subprocess.run(
        ["ffmpeg", "-y", "-loglevel", "error", "-i", src, "-map", "0",
         "-c", "copy", out],
        capture_output=True, text=True)
    assert proc.returncode != 0, (
        f"ffmpeg now infers a muxer for {ext}; the -f machinery may be obsolete")
    assert "output format" in (proc.stderr or "").lower(), proc.stderr
    assert not os.path.exists(out) or os.path.getsize(out) == 0
