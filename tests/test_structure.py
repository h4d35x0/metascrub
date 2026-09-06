"""
The structural scan: does every byte of an output belong to a structure the
format explains?

These tests exist because of three false CLEAN verdicts measured against the
shipped 1.0.1 on 2026-09-06. In each case exiftool removed everything it could
see, the residual scan found nothing because a carrier it never parsed produces
no needles, and the tool reported "verified clean" over a file that still
carried an embedded payload.

The three regression cases below are the exact shapes that were measured. The
overcorrection tests beside them are not optional decoration: a check that
reports an unaccounted region is one narrow keep-list away from calling every
legitimate animated WebP or APNG dirty, and per CLAUDE.md trap 11 the
overcorrection test lands in the same commit as the exclusion it guards.
"""

from __future__ import annotations

import struct
import zlib

import pytest
from PIL import Image

from metascrub import structure
from metascrub.verify import Verdict

PAYLOAD = b"HIDDENPAYLOAD_XY77" * 16


# --------------------------------------------------------------- builders

def _png(path, extra_chunk=None, trailing=b""):
    Image.new("RGB", (64, 64), "green").save(path)
    data = path.read_bytes()
    if extra_chunk is not None:
        ctype, payload = extra_chunk
        body = ctype + payload
        chunk = (
            struct.pack(">I", len(payload))
            + body
            + struct.pack(">I", zlib.crc32(body) & 0xFFFFFFFF)
        )
        cut = data.index(b"IDAT") - 4
        data = data[:cut] + chunk + data[cut:]
    path.write_bytes(data + trailing)
    return path


def _webp(path, extra_chunk=None, trailing_outside=b"", lossless=True):
    Image.new("RGB", (64, 64), "green").save(path, lossless=lossless)
    data = bytearray(path.read_bytes())
    if extra_chunk is not None:
        fourcc, payload = extra_chunk
        pad = b"\x00" if len(payload) % 2 else b""
        data.extend(fourcc + struct.pack("<I", len(payload)) + payload + pad)
        struct.pack_into("<I", data, 4, len(data) - 8)
    path.write_bytes(bytes(data) + trailing_outside)
    return path


def _gif(path, trailing=b""):
    Image.new("RGB", (48, 48), "red").save(path)
    path.write_bytes(path.read_bytes() + trailing)
    return path


# ------------------------------------------------- the measured regressions

def test_unknown_webp_chunk_is_reported(tmp_path):
    """
    The original defect. An 8 KB MP4 in an unknown MPVD chunk inside the
    declared RIFF size rode through a file reported as verified clean.
    """
    path = _webp(tmp_path / "a.webp", extra_chunk=(b"MPVD", PAYLOAD))
    report = structure.scan(str(path))
    assert report.applicable
    assert not report.is_clean
    assert any("MPVD" in region for region in report.unaccounted)


def test_unknown_png_chunk_is_reported(tmp_path):
    """An unknown ancillary chunk. Same false CLEAN shape as the WebP case."""
    path = _png(tmp_path / "a.png", extra_chunk=(b"prVt", PAYLOAD))
    report = structure.scan(str(path))
    assert report.applicable
    assert not report.is_clean
    assert any("prVt" in region for region in report.unaccounted)


def test_trailing_data_after_gif_trailer_is_reported(tmp_path):
    """Bytes appended after the GIF trailer. Measured as a false CLEAN."""
    path = _gif(tmp_path / "a.gif", trailing=PAYLOAD)
    report = structure.scan(str(path))
    assert report.applicable
    assert not report.is_clean
    assert any("after the GIF trailer" in region for region in report.unaccounted)


def test_trailing_data_after_png_iend_is_reported(tmp_path):
    path = _png(tmp_path / "a.png", trailing=PAYLOAD)
    report = structure.scan(str(path))
    assert any("after the IEND" in region for region in report.unaccounted)


def test_data_past_declared_riff_size_is_reported(tmp_path):
    path = _webp(tmp_path / "a.webp", trailing_outside=PAYLOAD)
    report = structure.scan(str(path))
    assert any("past the declared RIFF size" in region for region in report.unaccounted)


# ------------------------------------------------------ overcorrection tests

@pytest.mark.parametrize("colour", ["red", "blue"])
def test_ordinary_png_is_clean(tmp_path, colour):
    path = tmp_path / "p.png"
    Image.new("RGB", (64, 64), colour).save(path)
    assert structure.scan(str(path)).is_clean


def test_apng_animation_chunks_are_not_unaccounted(tmp_path):
    """acTL, fcTL and fdAT are legitimate. Flagging them would break every APNG."""
    path = tmp_path / "anim.png"
    frames = [Image.new("RGB", (32, 32), c) for c in ("red", "green", "blue")]
    frames[0].save(path, save_all=True, append_images=frames[1:], duration=100, loop=0)
    report = structure.scan(str(path))
    assert report.applicable
    assert report.is_clean, report.unaccounted


def test_png_with_icc_profile_is_not_unaccounted(tmp_path):
    """iCCP is a recognised chunk. Whether ICC should be REMOVED is a separate question."""
    path = tmp_path / "icc.png"
    img = Image.new("RGB", (32, 32), "green")
    img.save(path)
    data = path.read_bytes()
    body = b"iCCP" + b"n\x00\x00" + zlib.compress(b"\x00" * 128)
    chunk = struct.pack(">I", len(body) - 4) + body + struct.pack(">I", zlib.crc32(body) & 0xFFFFFFFF)
    cut = data.index(b"IDAT") - 4
    path.write_bytes(data[:cut] + chunk + data[cut:])
    report = structure.scan(str(path))
    assert report.is_clean, report.unaccounted


def test_animated_webp_is_not_unaccounted(tmp_path):
    """
    ANIM and ANMF are legitimate. This is also the case that killed an earlier
    proposal to assert on the VP8X flag bits: a valid animated WebP sets the
    alpha bit while alpha lives inside ANMF with no top-level ALPH chunk, so a
    flags-agree-with-chunks assertion produces a false positive here.
    """
    path = tmp_path / "anim.webp"
    frames = [Image.new("RGB", (32, 32), c) for c in ("red", "green", "blue")]
    frames[0].save(path, save_all=True, append_images=frames[1:], duration=100, loop=0)
    report = structure.scan(str(path))
    assert report.applicable
    assert report.is_clean, report.unaccounted


def test_lossy_and_lossless_webp_are_clean(tmp_path):
    for name, lossless in (("l.webp", True), ("y.webp", False)):
        path = tmp_path / name
        Image.new("RGB", (64, 64), "green").save(path, lossless=lossless)
        assert structure.scan(str(path)).is_clean


def test_webp_carrying_real_metadata_is_structurally_clean(tmp_path):
    """
    EXIF, XMP and ICCP are RECOGNISED chunks. This scan reports unaccounted
    regions, not policy. A file that still carries EXIF is caught by the
    residual scan; conflating the two jobs here would make the verdict unusable.
    """
    path = _webp(tmp_path / "m.webp", extra_chunk=(b"EXIF", b"\x00" * 64))
    assert structure.scan(str(path)).is_clean


def test_gif_with_comment_and_application_extensions_is_clean(tmp_path):
    """0xFE and 0xFF are defined GIF89a extension labels, not unknown blocks."""
    path = tmp_path / "c.gif"
    Image.new("RGB", (48, 48), "red").save(path, comment=b"a comment")
    report = structure.scan(str(path))
    assert report.applicable
    assert report.is_clean, report.unaccounted


def test_animated_gif_is_clean(tmp_path):
    path = tmp_path / "a.gif"
    frames = [Image.new("P", (32, 32), i) for i in (1, 2, 3)]
    frames[0].save(path, save_all=True, append_images=frames[1:], duration=100, loop=0)
    report = structure.scan(str(path))
    assert report.is_clean, report.unaccounted


# ------------------------------------------------- "did not look" is not "clean"

def test_unregistered_format_is_not_applicable(tmp_path):
    """
    A JPEG has no walker. The report must say so rather than return an empty
    unaccounted list that a caller could mistake for evidence. Same rule as
    ReadOutcome in exif_io.py: we did not look and we found nothing must never
    share a representation.
    """
    path = tmp_path / "a.jpg"
    Image.new("RGB", (32, 32), "green").save(path)
    report = structure.scan(str(path))
    assert report.applicable is False
    assert report.unaccounted == []
    assert report.is_clean is False


def test_unparseable_output_is_an_error_not_a_pass(tmp_path):
    """
    A file whose signature does not match the extension cannot be walked at
    all. That is reported through `error`, and `is_clean` stays False: a parse
    failure is never evidence of cleanliness.
    """
    path = tmp_path / "broken.png"
    path.write_bytes(b"NOTAPNG\x00" + b"\x00" * 32)
    report = structure.scan(str(path))
    assert report.applicable
    assert report.error is not None
    assert report.is_clean is False


def test_declared_length_past_eof_is_reported_and_not_clean(tmp_path):
    """
    A chunk claiming more bytes than the file holds is reported as an
    unaccounted region rather than raised. Either channel is acceptable; what
    must never happen is is_clean returning True.
    """
    path = tmp_path / "short.png"
    path.write_bytes(b"\x89PNG\r\n\x1a\n" + b"\x00\x00\xff\xffJUNK")
    report = structure.scan(str(path))
    assert report.applicable
    assert report.is_clean is False


def test_truncated_chunk_length_does_not_crash(tmp_path):
    """A declared length running past EOF is reported, never raised."""
    path = tmp_path / "t.png"
    Image.new("RGB", (32, 32), "green").save(path)
    data = bytearray(path.read_bytes())
    cut = data.index(b"IDAT") - 4
    struct.pack_into(">I", data, cut, 0xFFFF)
    path.write_bytes(bytes(data))
    report = structure.scan(str(path))
    assert report.applicable
    assert not report.is_clean


def test_every_registered_extension_has_a_walker():
    assert structure.supported_extensions() == frozenset({".png", ".webp", ".gif"})


def test_structure_unaccounted_is_never_clean():
    """The new verdict must not be readable as success anywhere."""
    assert Verdict.STRUCTURE_UNACCOUNTED is not Verdict.VERIFIED_CLEAN
    assert Verdict.STRUCTURE_UNACCOUNTED.value == "structure_unaccounted"
