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

import os
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
    A TIFF has no walker. The report must say so rather than return an empty
    unaccounted list that a caller could mistake for evidence. Same rule as
    ReadOutcome in exif_io.py: we did not look and we found nothing must never
    share a representation.

    This test used to use a `.jpg`, which is the honest measure of what changed
    on 2026-09-07: JPEG acquired a walker, so it is no longer an example of a
    format this module has no opinion about.
    """
    path = tmp_path / "a.tiff"
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
    assert structure.supported_extensions() == frozenset({
        ".png", ".webp", ".gif",
        ".jpg", ".jpeg", ".jpe",
        ".mp4", ".mov", ".m4v", ".heic", ".heif", ".avif", ".3gp",
    })


def test_the_iso_extensions_this_does_not_cover_are_named_not_forgotten():
    """
    `.qt`, `.mqv`, `.lrv`, `.f4a`, `.f4v`, `.m4a` and `.m4b` are ISO base media
    too, and they are NOT registered. That is a stated boundary rather than an
    oversight, and this test is what stops it becoming one.

    Measured 2026-09-07: `_walk_isobmff` reports clean on the suite's `.qt`,
    `.mqv`, `.lrv`, `.f4a` and `.m4a` fixtures, before and after scrubbing, so
    the omission is scope and not a known failure. Registering them is a
    decision that needs its own device corpus, because the fixtures above are
    ffmpeg output and CLAUDE.md trap 8 is specifically about these four
    extensions carrying two incompatible container flavours.
    """
    from metascrub import capabilities

    unregistered = {
        ext for ext in capabilities.supported_extensions()
        if ext in {".qt", ".mqv", ".lrv", ".f4a", ".f4v", ".m4a", ".m4b"}
    }
    assert unregistered, "the capability table no longer lists these at all"
    assert not (unregistered & structure.supported_extensions()), (
        "an ISO base media extension was registered without updating the "
        "measurement this test records"
    )


def test_structure_unaccounted_is_never_clean():
    """The new verdict must not be readable as success anywhere."""
    assert Verdict.STRUCTURE_UNACCOUNTED is not Verdict.VERIFIED_CLEAN
    assert Verdict.STRUCTURE_UNACCOUNTED.value == "structure_unaccounted"


# ===========================================================================
# JPEG, added 2026-09-07
#
# JPEG was the largest hole in this module and the most dangerous format to
# close it on, because it is the commonest thing anyone will ever scrub. A
# false positive here turns a correctly cleaned holiday photo into a failed
# verification, which is worse than the gap.
#
# So the walker was measured against 16 real device files before it shipped:
# a Pixel 2, a Pixel 3a motion photo, a Nokia 7 plus motion photo in the older
# GCamera convention, and a 22 MB Galaxy S8 photo with a real Samsung SEF
# trailer. Those measurements are the corpus tests at the bottom of this file.
# The synthetic tests here run everywhere and cover the same shapes.
# ===========================================================================

# A stand-in for the appended MP4 in a motion photo. It only has to be bytes
# that follow the EOI; what makes it dangerous is where it sits, not what it
# says, and that is the entire point of a structural check.
TRAILER = b"\x00\x00\x00\x18ftypmp42" + b"HIDDEN_TRAILER_XY77" * 32


def _jpeg(path, quality=90, progressive=False):
    Image.new("RGB", (64, 64), "green").save(
        path, "JPEG", quality=quality, progressive=progressive)
    return path


def _insert_after_soi(data: bytes, segment: bytes) -> bytes:
    """Splice one marker segment in immediately after the SOI."""
    assert data[:2] == b"\xff\xd8"
    return data[:2] + segment + data[2:]


def _app_segment(code: int, payload: bytes) -> bytes:
    assert len(payload) + 2 <= 0xFFFF
    return bytes((0xFF, code)) + struct.pack(">H", len(payload) + 2) + payload


# --------------------------------------------------- the JPEG gate

def test_bytes_after_the_jpeg_eoi_are_reported(tmp_path):
    """
    The measured carrier. A Google Motion Photo appends a complete MP4 with its
    own GPS after the EOI, and exiftool names the blob without ever saying what
    is inside it, so the residual scan can build no needle for the video's
    coordinates. Only a structural check can see this.
    """
    path = tmp_path / "a.jpg"
    _jpeg(path)
    path.write_bytes(path.read_bytes() + TRAILER)
    report = structure.scan(str(path))
    assert report.applicable
    assert not report.is_clean
    assert any("after the first top-level EOI" in region
               for region in report.unaccounted), report.unaccounted


def test_the_reported_trailer_length_is_exact(tmp_path):
    """The count must be the trailer, not an approximation of it."""
    path = tmp_path / "a.jpg"
    _jpeg(path)
    path.write_bytes(path.read_bytes() + TRAILER)
    (region,) = structure.scan(str(path)).unaccounted
    assert region.startswith("%d bytes after" % len(TRAILER)), region


def test_a_single_appended_byte_is_still_reported(tmp_path):
    """
    A boundary the count above cannot reach. One byte after EOI is as
    unaccounted as three megabytes of it, and a walker that only notices a
    trailer worth noticing is a walker with a threshold nobody chose.
    """
    path = tmp_path / "a.jpg"
    _jpeg(path)
    path.write_bytes(path.read_bytes() + b"\x00")
    report = structure.scan(str(path))
    assert not report.is_clean
    assert report.unaccounted[0].startswith("1 bytes after")


def test_the_walker_reaches_sos_before_it_can_see_an_eoi(tmp_path):
    """
    The case that turns this check into a file-destroying bug if it is written
    as a search. An APP0/JFXX segment carries a complete embedded JPEG
    thumbnail, which ends with its own EOI. A search for the first EOI byte
    pair lands on that inner marker and reports the entire real image as a
    trailer.

    Measured, and already guarded on the engine side by
    tests/test_motion_photo.py::test_the_gate_is_not_fooled_by_an_eoi_inside_a_segment.
    This is the same boundary asserted against this module's own walker, which
    consumes every segment by its declared length and so can only ever reach
    the outer EOI.
    """
    thumb = tmp_path / "t.jpg"
    Image.new("RGB", (16, 16), "blue").save(thumb, "JPEG", quality=20)
    thumbnail = thumb.read_bytes()

    path = tmp_path / "poisoned.jpg"
    _jpeg(path)
    poisoned = _insert_after_soi(
        path.read_bytes(), _app_segment(0xE0, b"JFXX\x00\x10" + thumbnail))
    path.write_bytes(poisoned)

    naive = poisoned.find(b"\xff\xd9") + 2
    assert naive < len(poisoned), (
        "the fixture does not actually carry an inner EOI before the outer one")
    report = structure.scan(str(path))
    assert report.applicable
    assert report.is_clean, report.unaccounted


def test_a_trailer_behind_an_embedded_thumbnail_is_still_found(tmp_path):
    """
    Both halves at once: the inner EOI must not be mistaken for the outer one,
    AND the real trailer behind it must still be reported. A walker that fixed
    the first by stopping early would pass the test above and leak here.
    """
    thumb = tmp_path / "t.jpg"
    Image.new("RGB", (16, 16), "blue").save(thumb, "JPEG", quality=20)
    path = tmp_path / "both.jpg"
    _jpeg(path)
    data = _insert_after_soi(
        path.read_bytes(),
        _app_segment(0xE0, b"JFXX\x00\x10" + thumb.read_bytes()))
    path.write_bytes(data + TRAILER)
    (region,) = structure.scan(str(path)).unaccounted
    assert region.startswith("%d bytes after" % len(TRAILER)), region


def test_a_declared_segment_length_past_eof_is_reported(tmp_path):
    path = tmp_path / "a.jpg"
    _jpeg(path)
    data = bytearray(path.read_bytes())
    # The first segment after SOI; inflate its length beyond the file.
    struct.pack_into(">H", data, 4, 0xFFFF)
    path.write_bytes(bytes(data))
    report = structure.scan(str(path))
    assert report.applicable
    assert not report.is_clean


def test_a_truncated_jpeg_is_an_error_not_a_pass(tmp_path):
    """No EOI at all. `error`, and `is_clean` stays False."""
    path = tmp_path / "cut.jpg"
    _jpeg(path)
    path.write_bytes(path.read_bytes()[:-64])
    report = structure.scan(str(path))
    assert report.applicable
    assert report.is_clean is False


def test_a_non_jpeg_under_a_jpg_extension_is_an_error(tmp_path):
    png = tmp_path / "real.png"
    Image.new("RGB", (32, 32), "green").save(png)
    path = tmp_path / "lying.jpg"
    path.write_bytes(png.read_bytes())
    report = structure.scan(str(path))
    assert report.applicable
    assert report.error is not None
    assert report.is_clean is False


# ------------------------------------ JPEG overcorrection: trap 11 applies

@pytest.mark.parametrize("extension", [".jpg", ".jpeg", ".jpe"])
def test_an_ordinary_jpeg_is_clean_under_every_registered_extension(
        tmp_path, extension):
    path = tmp_path / ("p" + extension)
    _jpeg(path)
    report = structure.scan(str(path))
    assert report.applicable
    assert report.is_clean, report.unaccounted


@pytest.mark.parametrize("quality", [10, 50, 95])
def test_jpegs_across_the_quality_range_are_clean(tmp_path, quality):
    """
    Quality drives how much entropy-coded data there is and how often an 0xFF
    needs stuffing, which is the only part of the scan walk that can go wrong.
    One illustrative example would not exercise it; this is the cross-product
    CLAUDE.md asks for, over the input that actually varies.
    """
    path = tmp_path / "q.jpg"
    _jpeg(path, quality=quality)
    assert structure.scan(str(path)).is_clean


def test_a_progressive_jpeg_is_clean(tmp_path):
    """Many SOS segments, so the scan walk must run once per scan, not once."""
    path = tmp_path / "prog.jpg"
    _jpeg(path, progressive=True)
    report = structure.scan(str(path))
    assert report.is_clean, report.unaccounted


def test_a_jpeg_with_restart_markers_is_clean(tmp_path):
    """
    RST0..RST7 appear inside the entropy-coded data and are the one bare marker
    a scan is allowed to contain. A walk that ended the scan at the first RST
    would report the rest of the image as unaccounted.
    """
    path = tmp_path / "rst.jpg"
    Image.new("RGB", (256, 256), "green").save(
        path, "JPEG", restart_marker_blocks=1)
    assert b"\xff\xd0" in path.read_bytes(), "the fixture has no restart markers"
    report = structure.scan(str(path))
    assert report.is_clean, report.unaccounted


def test_a_cmyk_jpeg_with_an_adobe_app14_is_clean(tmp_path):
    path = tmp_path / "cmyk.jpg"
    Image.new("CMYK", (32, 32), (0, 0, 0, 0)).save(path, "JPEG")
    assert b"Adobe" in path.read_bytes(), "the fixture carries no APP14"
    report = structure.scan(str(path))
    assert report.is_clean, report.unaccounted


def test_a_greyscale_jpeg_is_clean(tmp_path):
    path = tmp_path / "grey.jpg"
    Image.new("L", (64, 64), 128).save(path, "JPEG")
    assert structure.scan(str(path)).is_clean


def test_a_jpeg_that_still_carries_exif_is_structurally_clean(tmp_path):
    """
    An APP1 that survived is a RECOGNISED segment. This scan reports
    unaccounted regions, not policy: a file that still carries EXIF is the
    residual scan's catch, and conflating the two jobs makes both unusable.
    Same rule as test_webp_carrying_real_metadata_is_structurally_clean.
    """
    path = tmp_path / "exif.jpg"
    _jpeg(path)
    data = _insert_after_soi(
        path.read_bytes(),
        _app_segment(0xE1, b"Exif\x00\x00" + b"MM\x00*" + b"\x00" * 64))
    path.write_bytes(data)
    report = structure.scan(str(path))
    assert report.is_clean, report.unaccounted


def test_an_unknown_appn_segment_is_structurally_clean(tmp_path):
    """
    The deliberate difference from the PNG and WebP walkers above, and it is
    worth being explicit about. Those flag an unrecognised CHUNK TYPE, because
    the type space is small and enumerable. APPn is not: APP0 through APP15 are
    all defined markers and their contents are keyed on an identifier string
    that any vendor may mint. There is no unknown MARKER to flag here, only an
    unknown payload inside a marker the format fully accounts for.

    So the JPEG walker's finding is the TRAILER, which is where the measured
    carrier actually lives. Widening it to "an APPn whose identifier I do not
    recognise" would fire on ordinary camera output, which is exactly the
    overcorrection trap 11 is about.
    """
    path = tmp_path / "vendor.jpg"
    _jpeg(path)
    data = _insert_after_soi(
        path.read_bytes(), _app_segment(0xEB, b"SomeVendor\x00" + PAYLOAD))
    path.write_bytes(data)
    report = structure.scan(str(path))
    assert report.is_clean, report.unaccounted


def test_fill_bytes_before_a_marker_are_accounted_for(tmp_path):
    """
    A run of 0xFF before a marker is legal padding that belongs to the segment
    after it. Producers emit it, and a walker that treats it as a desync
    reports the rest of the file as unaccounted.
    """
    path = tmp_path / "fill.jpg"
    _jpeg(path)
    data = path.read_bytes()
    path.write_bytes(data[:-2] + b"\xff\xff\xff" + data[-2:])
    report = structure.scan(str(path))
    assert report.is_clean, report.unaccounted


# ===========================================================================
# ISO base media, added 2026-09-07
# ===========================================================================

def _box(kind: bytes, payload: bytes = b"") -> bytes:
    return struct.pack(">I", 8 + len(payload)) + kind + payload


def _full_box(kind: bytes, payload: bytes) -> bytes:
    """A FullBox: a version byte and three flag bytes before the payload."""
    return _box(kind, b"\x00\x00\x00\x00" + payload)


def _hdlr(handler: bytes = b"pict") -> bytes:
    return _full_box(b"hdlr", struct.pack(">I", 0) + handler + b"\x00" * 13)


FTYP = _box(b"ftyp", b"isom" + struct.pack(">I", 512) + b"isomiso2mp41")
MDAT = _box(b"mdat", b"\x00" * 128)


def _iso_meta(children: bytes) -> bytes:
    """The ISO/IEC 14496-12 MetaBox: a FullBox. Used by HEIF and AVIF."""
    return _full_box(b"meta", children)


def _apple_meta(children: bytes) -> bytes:
    """Apple's QuickTime Keys meta: the same four characters, NOT a FullBox."""
    return _box(b"meta", children)


APPLE_META_CHILDREN = (
    _hdlr(b"mdta") + _box(b"keys", b"\x00" * 12) + _box(b"ilst", b"\x00" * 16))
ISO_META_CHILDREN = _hdlr(b"pict") + _full_box(b"pitm", b"\x00\x01")


def _both_meta_shapes() -> bytes:
    """
    One file carrying BOTH `meta` header shapes, which is CLAUDE.md trap 8.

    A top-level `meta` in the ISO FullBox shape, as HEIF and AVIF write it, and
    a `moov/meta` in the Apple QuickTime Keys shape, which is not a FullBox. A
    walker that assumes either shape misparses the other; the prior instrument
    read the Apple one as a child box of size 1751411826, which is the ASCII of
    "hdlr" read as a length.
    """
    moov = _box(b"moov",
                _box(b"mvhd", b"\x00" * 100) + _apple_meta(APPLE_META_CHILDREN))
    return FTYP + _iso_meta(ISO_META_CHILDREN) + moov + MDAT


def _minimal_iso() -> bytes:
    return FTYP + _box(b"moov", _box(b"mvhd", b"\x00" * 100)) + MDAT


# --------------------------------------------------- the ISO gate

def test_trailing_bytes_after_the_last_top_level_box_are_reported(tmp_path):
    path = tmp_path / "a.mp4"
    path.write_bytes(_minimal_iso() + b"\xff" * 64)
    report = structure.scan(str(path))
    assert report.applicable
    assert not report.is_clean
    assert any("after the last top-level box" in region
               for region in report.unaccounted), report.unaccounted


def test_a_box_declaring_more_than_its_parent_holds_is_reported(tmp_path):
    path = tmp_path / "a.mp4"
    liar = struct.pack(">I", 100000) + b"mvhd" + b"\x00" * 8
    path.write_bytes(FTYP + _box(b"moov", liar) + MDAT)
    report = structure.scan(str(path))
    assert not report.is_clean
    assert any("runs past the end of box 'moov'" in region
               for region in report.unaccounted), report.unaccounted


def test_a_gap_inside_a_container_is_reported(tmp_path):
    """Bytes inside `moov` that no child box tiles."""
    path = tmp_path / "a.mp4"
    path.write_bytes(
        FTYP + _box(b"moov", _box(b"mvhd", b"\x00" * 100) + b"\x01" * 24) + MDAT)
    report = structure.scan(str(path))
    assert not report.is_clean
    assert any("no box accounts for" in region
               for region in report.unaccounted), report.unaccounted


def test_a_box_smaller_than_its_own_header_is_reported(tmp_path):
    path = tmp_path / "a.mp4"
    path.write_bytes(FTYP + struct.pack(">I", 4) + b"moov" + MDAT)
    assert not structure.scan(str(path)).is_clean


def test_a_top_level_box_running_past_the_end_of_the_file_is_reported(tmp_path):
    path = tmp_path / "a.mp4"
    path.write_bytes(FTYP + struct.pack(">I", 900000) + b"mdat" + b"\x00" * 32)
    report = structure.scan(str(path))
    assert not report.is_clean
    assert any("runs past the end of the file" in region
               for region in report.unaccounted), report.unaccounted


def test_a_non_iso_file_under_an_mp4_extension_is_an_error(tmp_path):
    path = tmp_path / "lying.mp4"
    path.write_bytes(b"\xff\xd8\xff\xe0\x00\x10JFIF\x00" + b"\x00" * 64)
    report = structure.scan(str(path))
    assert report.applicable
    assert report.error is not None
    assert report.is_clean is False


# ----------------------------------------------- trap 8, the two meta shapes

def test_both_meta_header_shapes_in_one_file_are_walked(tmp_path):
    """
    CLAUDE.md trap 8. `meta` has TWO header shapes and both occur in ONE FILE.
    The sniff must read the bytes, because the name is the same either way and
    the file's brand cannot tell them apart.
    """
    path = tmp_path / "two.mp4"
    path.write_bytes(_both_meta_shapes())
    report = structure.scan(str(path))
    assert report.applicable
    assert report.is_clean, report.unaccounted


def test_the_sniff_returns_the_two_shapes_it_measured():
    """
    The verdict above is the outcome; this is the mechanism, asserted directly
    so a walker that got the right answer for the wrong reason cannot hide.
    """
    data = _both_meta_shapes()
    iso_at = len(FTYP)
    assert data[iso_at + 4:iso_at + 8] == b"meta"
    iso_size = struct.unpack(">I", data[iso_at:iso_at + 4])[0]
    assert structure._iso_meta_skip(data, iso_at + 8, iso_at + iso_size) == 4

    apple_at = data.index(b"meta", iso_at + 8) - 4
    apple_size = struct.unpack(">I", data[apple_at:apple_at + 4])[0]
    assert structure._iso_meta_skip(
        data, apple_at + 8, apple_at + apple_size) == 0


def test_the_apple_meta_shape_misread_as_a_fullbox_yields_the_measured_nonsense():
    """
    Why the sniff exists, stated as a number. Reading the Apple Keys `meta` as
    a FullBox starts four bytes into the `hdlr` box header, so the next four
    bytes read as a size are the ASCII of "hdlr": 1751411826. That exact value
    is what the prior instrument reported, and it is recorded here so the
    failure has a fingerprint rather than a description.
    """
    assert int.from_bytes(b"hdlr", "big") == 1751411826
    data = _both_meta_shapes()
    apple_at = data.index(b"meta", len(FTYP) + 8) - 4
    misread = struct.unpack(">I", data[apple_at + 12:apple_at + 16])[0]
    assert misread == 1751411826


def test_a_meta_whose_payload_reads_as_neither_shape_is_a_leaf(tmp_path):
    """
    The third answer. When neither reading produces a plausible box the sniff
    returns None and the box accounts for its own payload, rather than being
    descended into on a guess and then reported as unaccounted. Guessing here
    would turn an opaque `meta` into a false positive.
    """
    path = tmp_path / "odd.mp4"
    path.write_bytes(FTYP + _box(b"meta", b"\x01" * 40) + MDAT)
    report = structure.scan(str(path))
    assert report.applicable
    assert report.is_clean, report.unaccounted


# ------------------------------------ ISO overcorrection: trap 11 applies

@pytest.mark.parametrize(
    "extension", [".mp4", ".mov", ".m4v", ".heic", ".heif", ".avif", ".3gp"])
def test_a_minimal_iso_file_is_clean_under_every_registered_extension(
        tmp_path, extension):
    path = tmp_path / ("v" + extension)
    path.write_bytes(_minimal_iso())
    report = structure.scan(str(path))
    assert report.applicable
    assert report.is_clean, report.unaccounted


def test_a_file_with_no_ftyp_box_is_clean(tmp_path):
    """
    Measured on a real iPhone 14 Pro Live Photo `.mov`: the top level is
    `wide`, `mdat`, `moov` and a byte search for the literal `ftyp` across the
    whole file returns nothing. That is device output, not corruption. A walker
    that required an `ftyp`, as `metascrub/isobmff.py` deliberately does for
    its own different purpose, would turn that file into an UNVERIFIED one.
    """
    path = tmp_path / "live.mov"
    path.write_bytes(
        _box(b"wide") + MDAT + _box(b"moov", _box(b"mvhd", b"\x00" * 100)))
    report = structure.scan(str(path))
    assert report.applicable
    assert report.is_clean, report.unaccounted


def test_a_quicktime_udta_holding_a_c_xyz_atom_is_clean(tmp_path):
    """
    The measured false positive this walker is built to avoid. Real Android
    output puts its ISO 6709 GPS string in a `moov/udta/(c)xyz` atom whose type
    begins with 0xA9, which is not printable ASCII. A walker that descended
    into `udta` would report every geotagged Android MP4 as unaccounted.

    `udta` is a leaf here for that reason. A surviving GPS atom is the residual
    scan's catch and gps_verify.py's catch, not this module's.
    """
    xyz = _box(b"\xa9xyz", b"\x00\x12\x15\xc7" + b"+00.0000-000.0000/")
    path = tmp_path / "android.mp4"
    path.write_bytes(
        FTYP
        + _box(b"moov", _box(b"mvhd", b"\x00" * 100) + _box(b"udta", xyz))
        + MDAT)
    report = structure.scan(str(path))
    assert report.applicable
    assert report.is_clean, report.unaccounted


def test_a_quicktime_udta_terminator_is_not_a_false_positive(tmp_path):
    """A QuickTime `udta` may end with four zero bytes that are not a box."""
    path = tmp_path / "term.mp4"
    udta = _box(b"udta", _box(b"\xa9nam", b"a name") + b"\x00\x00\x00\x00")
    path.write_bytes(FTYP + _box(b"moov", udta) + MDAT)
    assert structure.scan(str(path)).is_clean


def test_a_trailing_run_of_fewer_than_eight_zero_bytes_is_padding(tmp_path):
    path = tmp_path / "pad.mp4"
    path.write_bytes(_minimal_iso() + b"\x00\x00\x00\x00")
    report = structure.scan(str(path))
    assert report.is_clean, report.unaccounted


def test_a_trailing_run_of_fewer_than_eight_nonzero_bytes_is_reported(tmp_path):
    """The other half of the padding tolerance, so it cannot widen unnoticed."""
    path = tmp_path / "notpad.mp4"
    path.write_bytes(_minimal_iso() + b"\x01\x02\x03\x04")
    assert not structure.scan(str(path)).is_clean


def test_a_sixty_four_bit_largesize_box_is_clean(tmp_path):
    """`size == 1` means the real size is a 64-bit field after the type."""
    payload = b"\x00" * 64
    large = (struct.pack(">I", 1) + b"mdat"
             + struct.pack(">Q", 16 + len(payload)) + payload)
    path = tmp_path / "large.mp4"
    path.write_bytes(FTYP + large)
    report = structure.scan(str(path))
    assert report.is_clean, report.unaccounted


def test_a_size_zero_box_runs_to_the_end_of_its_level(tmp_path):
    """`size == 0` is legal for the last box at a level. It is not a gap."""
    path = tmp_path / "zero.mp4"
    path.write_bytes(FTYP + struct.pack(">I", 0) + b"mdat" + b"\x00" * 64)
    report = structure.scan(str(path))
    assert report.is_clean, report.unaccounted


def test_a_uuid_box_accounts_for_its_extended_type(tmp_path):
    """
    A `uuid` box carries 16 extra header bytes. Charging them to the payload
    instead would shift every offset inside it.
    """
    path = tmp_path / "uuid.mp4"
    extended = bytes.fromhex("be7acfcb97a942e89c71999491e3afac")
    path.write_bytes(FTYP + _box(b"uuid", extended + b"<x:xmpmeta/>") + MDAT)
    report = structure.scan(str(path))
    assert report.is_clean, report.unaccounted


def test_a_uuid_box_too_small_for_its_extended_type_is_reported(tmp_path):
    path = tmp_path / "shortuuid.mp4"
    path.write_bytes(FTYP + _box(b"uuid", b"\x00" * 4) + MDAT)
    assert not structure.scan(str(path)).is_clean


def test_an_unknown_box_type_is_accounted_for_not_flagged(tmp_path):
    """
    A DELIBERATE and stated limitation, pinned here so it is a decision rather
    than an accident.

    The PNG and WebP walkers above flag an unrecognised chunk type, because
    those type spaces are small and enumerable. The ISO box space is not: the
    specification's whole design is that an unknown box is skippable, hundreds
    are registered, and real device output carries vendor boxes freely
    (`smrd`, `smta` and `SDLN` were measured in one Samsung MP4). A box-type
    keep-list here would fire on ordinary phone output, which is the
    overcorrection trap 11 forbids and worse than the gap it would close.

    So an unknown box is ACCOUNTED FOR: it is a box, it tiles its level, and
    this module reports regions the format does not account for. Closing this
    needs a box-type keep-list with its own device corpus behind it, and that
    corpus does not exist yet.
    """
    path = tmp_path / "vendor.mp4"
    path.write_bytes(FTYP + _box(b"MPVD", PAYLOAD) + MDAT)
    report = structure.scan(str(path))
    assert report.applicable
    assert report.is_clean, report.unaccounted


def test_deep_nesting_is_bounded_rather_than_recursing_forever(tmp_path):
    """A hostile file must not take the interpreter's stack with it."""
    blob = _box(b"mvhd", b"\x00" * 8)
    for _ in range(structure._ISO_MAX_DEPTH + 4):
        blob = _box(b"moov", blob)
    path = tmp_path / "deep.mp4"
    path.write_bytes(FTYP + blob)
    report = structure.scan(str(path))
    assert report.applicable
    assert not report.is_clean


# ===========================================================================
# MUTATION CHECKS
#
# A green suite is not evidence that the tests above are load-bearing. Each
# check below breaks the walker in a specific, plausible way and asserts that a
# NAMED test starts failing. The name is in the docstring and in the assertion
# message, so a mutation that stops killing its test says which guard went
# quiet rather than just going green.
#
# The mutants are not arbitrary. Each one is a real implementation somebody
# would plausibly write, and two of them are implementations that were actually
# written and measured wrong before.
# ===========================================================================

def _mutation_dir(tmp_path, name):
    """A fresh directory, because a guarding test is called twice."""
    target = tmp_path / name
    target.mkdir()
    return target


def test_mutation_jpeg_searching_for_the_eoi_instead_of_walking(
        tmp_path, monkeypatch):
    """
    MUTATION CHECK 1, JPEG.

    Mutant: find the EOI with `data.find(b"\\xff\\xd9")` instead of consuming
    every segment by its declared length. This is the naive implementation, and
    it is the one that turns this check into a file-destroying bug when an
    engine acts on it: an APP0/JFXX segment carries a complete embedded JPEG
    with its own EOI, so the search lands inside the thumbnail.

    TEST THAT MUST FAIL:
    test_the_walker_reaches_sos_before_it_can_see_an_eoi
    """
    def mutant(data):
        index = data.find(b"\xff\xd9")
        if index < 0:
            raise ValueError("no EOI marker: this JPEG is truncated")
        end = index + 2
        if end < len(data):
            return ["%d bytes after the first top-level EOI at offset %d"
                    % (len(data) - end, end)]
        return []

    healthy = _mutation_dir(tmp_path, "healthy")
    test_the_walker_reaches_sos_before_it_can_see_an_eoi(healthy)

    monkeypatch.setitem(structure._WALKERS, ".jpg", mutant)
    broken = _mutation_dir(tmp_path, "broken")
    with pytest.raises(AssertionError):
        test_the_walker_reaches_sos_before_it_can_see_an_eoi(broken)


def test_mutation_jpeg_not_reporting_the_trailer(tmp_path, monkeypatch):
    """
    MUTATION CHECK 2, JPEG.

    Mutant: walk correctly, then drop the trailer finding. This is the shape of
    a walker that "parses to EOI and stops", which looks complete and reports
    nothing about the 2.7 MB of MP4 sitting behind the EOI.

    TEST THAT MUST FAIL:
    test_bytes_after_the_jpeg_eoi_are_reported
    """
    def mutant(data):
        return [region for region in structure._walk_jpeg(data)
                if "after the first top-level EOI" not in region]

    healthy = _mutation_dir(tmp_path, "healthy")
    test_bytes_after_the_jpeg_eoi_are_reported(healthy)

    monkeypatch.setitem(structure._WALKERS, ".jpg", mutant)
    broken = _mutation_dir(tmp_path, "broken")
    with pytest.raises(AssertionError):
        test_bytes_after_the_jpeg_eoi_are_reported(broken)


def test_mutation_jpeg_ending_the_scan_at_a_restart_marker(
        tmp_path, monkeypatch):
    """
    MUTATION CHECK 3, JPEG.

    Mutant: treat RST0..RST7 as ending the entropy-coded scan. That is the
    single easiest thing to get wrong in `_jpeg_scan_end`, it is invisible on
    every small fixture, and it reports most of a large photograph as
    unaccounted.

    TEST THAT MUST FAIL:
    test_a_jpeg_with_restart_markers_is_clean
    """
    real = structure._jpeg_scan_end

    def mutant(data, start):
        at = start
        while at + 1 < len(data):
            if data[at] == 0xFF and data[at + 1] != 0x00 and data[at + 1] != 0xFF:
                return at
            at += 1
        return real(data, start)

    healthy = _mutation_dir(tmp_path, "healthy")
    test_a_jpeg_with_restart_markers_is_clean(healthy)

    monkeypatch.setattr(structure, "_jpeg_scan_end", mutant)
    broken = _mutation_dir(tmp_path, "broken")
    with pytest.raises(AssertionError):
        test_a_jpeg_with_restart_markers_is_clean(broken)


def test_mutation_iso_assuming_meta_is_always_a_fullbox(tmp_path, monkeypatch):
    """
    MUTATION CHECK 4, ISO base media. CLAUDE.md trap 8.

    Mutant: `meta` is always the ISO FullBox, so skip four bytes. Correct for
    HEIF and AVIF, wrong for Apple's QuickTime Keys `meta`, and both shapes
    occur in one file. This is the bug that produced the child box of size
    1751411826.

    TEST THAT MUST FAIL:
    test_both_meta_header_shapes_in_one_file_are_walked
    """
    healthy = _mutation_dir(tmp_path, "healthy")
    test_both_meta_header_shapes_in_one_file_are_walked(healthy)

    monkeypatch.setattr(structure, "_iso_meta_skip",
                        lambda data, start, end: 4)
    broken = _mutation_dir(tmp_path, "broken")
    with pytest.raises(AssertionError):
        test_both_meta_header_shapes_in_one_file_are_walked(broken)


def test_mutation_iso_assuming_meta_is_never_a_fullbox(tmp_path, monkeypatch):
    """
    MUTATION CHECK 5, ISO base media. Trap 8 from the other side, which is the
    half a single-shape fixture would never catch.

    Mutant: `meta` is never a FullBox, so skip nothing. Correct for the Apple
    Keys layout, wrong for every HEIC and AVIF.

    TEST THAT MUST FAIL:
    test_both_meta_header_shapes_in_one_file_are_walked
    """
    healthy = _mutation_dir(tmp_path, "healthy")
    test_both_meta_header_shapes_in_one_file_are_walked(healthy)

    monkeypatch.setattr(structure, "_iso_meta_skip",
                        lambda data, start, end: 0)
    broken = _mutation_dir(tmp_path, "broken")
    with pytest.raises(AssertionError):
        test_both_meta_header_shapes_in_one_file_are_walked(broken)


def test_mutation_iso_not_reporting_bytes_no_box_accounts_for(
        tmp_path, monkeypatch):
    """
    MUTATION CHECK 6, ISO base media.

    Mutant: stop reporting the leftover at the end of a level. A walker that
    tiles perfectly and then says nothing about what is left over is the exact
    false CLEAN this whole module exists to stop.

    TEST THAT MUST FAIL:
    test_trailing_bytes_after_the_last_top_level_box_are_reported
    """
    healthy = _mutation_dir(tmp_path, "healthy")
    test_trailing_bytes_after_the_last_top_level_box_are_reported(healthy)

    monkeypatch.setattr(structure, "_iso_level", _iso_level_without_leftovers)
    broken = _mutation_dir(tmp_path, "broken")
    with pytest.raises(AssertionError):
        test_trailing_bytes_after_the_last_top_level_box_are_reported(broken)


def _iso_level_without_leftovers(data, start, end, parent, depth, out):
    """The mutant walker for check 6: tile the level, report nothing left."""
    off = start
    while off < end:
        if end - off < structure._ISO_HEADER:
            return
        kind = bytes(data[off + 4:off + 8])
        if not structure._iso_printable(kind):
            return
        size = int.from_bytes(data[off:off + 4], "big")
        if size == 1:
            size = int.from_bytes(data[off + 8:off + 16], "big")
        elif size == 0:
            size = end - off
        if size < structure._ISO_HEADER or off + size > end:
            return
        off += size


# ===========================================================================
# THE REAL DEVICE CORPUS
#
# Sixteen files downloaded from published test corpora and measured byte by
# byte on 2026-09-06, described in
# <device-corpus>\MANIFEST.md. They are NOT in
# this repository and never will be: several carry real coordinates belonging
# to strangers, and the manifest withholds them for that reason. These tests
# skip when the folder is absent, which is every machine but the one the
# measurement was made on.
#
# This is the part of the work that mattered most. Adding a JPEG walker changes
# behaviour for the commonest format in a tool that shipped two days ago, and a
# false positive turns a correctly scrubbed holiday photo into a failed
# verification. That is worse than the gap being closed, so the corpus, not the
# walker, was the deliverable.
#
# MEASURED 2026-09-07 with these walkers, on all sixteen files, twice: once on
# the untouched original and once on the output of `metascrub scrub`.
#
#   Before scrubbing: 13 of 16 clean. The three that report are the three real
#   trailers, and each reported length matches the manifest's independently
#   measured figure exactly.
#   After scrubbing:  16 of 16 clean. The desktop exiftool engine removes all
#   three trailers, so on the desktop these walkers confirm a removal rather
#   than blocking one.
#
# Zero false positives, before or after, on either walker.
# ===========================================================================

_DEVICE_CORPUS = os.environ.get(
    "METASCRUB_DEVICE_MEDIA",
    os.path.join("D:", os.sep, "Projects", "_Meta",
                 "metascrub-device-media", "sourced"),
)

requires_device_corpus = pytest.mark.skipif(
    not os.path.isdir(_DEVICE_CORPUS),
    reason="the real device corpus is not on this machine: " + _DEVICE_CORPUS,
)

# Real device output that must report CLEAN. A regression here is the false
# positive this whole exercise was about.
DEVICE_CLEAN = [
    # JPEG. A geotagged Pixel 2 from the phone generation that INTRODUCED
    # motion photos, with exactly zero bytes after its EOI. A trailer detector
    # that fires on this file is wrong.
    "Google Pixel 2.jpg",
    # HEIC. iPhone XR, iPhone 11 Pro with its Exif item ending on the last byte
    # of the file, iPhone 8 with no XMP item at all, and a Galaxy S10+ whose
    # `meta` sits at the TAIL rather than the head.
    "Issue 263 dotnet.heic",
    "exif-at-eof.heic",
    "IMG_1034.heic",
    "Issue 487.heic",
    # AVIF.
    "Issue 649.avif",
    # MP4. Nokia 6.1 with a real `(c)xyz` GPS atom in `moov/udta`, and a
    # Samsung with four udta children of which exiftool reports only two.
    "Nokia 6.1.mp4",
    "with-gps.mp4",
    # QuickTime. An iPhone 6 with brand `qt  ` and the Apple Keys layout, and
    # an iPhone 14 Pro Live Photo with NO ftyp box anywhere in the file.
    "with-gps.mov",
    "apple-livephoto-quicktime.mov",
]

# Real trailers, with the byte counts the manifest measured with a different
# instrument on a different day. Asserting the exact figure is what makes this
# a measurement rather than a shrug.
DEVICE_TRAILERS = {
    # Pixel 3a, GContainer plus GCamera:MotionPhoto, a 2.7 MB MP4 after EOI.
    "test.MP.jpg": 2835277,
    # Nokia 7 plus, the older GCamera:MicroVideoOffset convention with no
    # GContainer to cross-check against.
    "MVIMG_20180910_124410.jpg": 1714905,
    # Galaxy S8, a real Samsung SEF trailer holding two complete embedded
    # JPEGs. Nothing in any marker points at it; it is reachable only from the
    # last bytes of the file.
    "Samsung SM-G950F (Galaxy S8).jpg": 18469861,
}

# The three WebPs are the pre-existing walker's territory, recorded so the
# corpus table below covers all sixteen files and cannot quietly shrink.
DEVICE_OTHER = [
    "HTC Desire.webp",
    "Nikon Coolpix P7000.webp",
    "Issue 473 (Java).webp",
]


@requires_device_corpus
def test_the_device_corpus_is_all_sixteen_files():
    """
    A corpus that lost half its files would make every test below pass by
    covering nothing. This is the denominator, checked rather than assumed.
    """
    present = {name for name in os.listdir(_DEVICE_CORPUS)
               if not name.endswith(".md")}
    expected = set(DEVICE_CLEAN) | set(DEVICE_TRAILERS) | set(DEVICE_OTHER)
    assert len(expected) == 16
    assert present == expected, (
        "corpus drift; missing: %s, unexpected: %s"
        % (sorted(expected - present), sorted(present - expected)))


@requires_device_corpus
@pytest.mark.parametrize("name", DEVICE_CLEAN)
def test_a_real_device_file_reports_structurally_clean(name):
    """
    THE OVERCORRECTION TEST FOR BOTH NEW WALKERS, over real phone output.

    Every file here is untouched device output carrying real metadata, real
    maker notes and in most cases real GPS. All of that is the residual scan's
    business. Structurally, every byte belongs to a marker segment or to a box,
    and this check must say so.
    """
    report = structure.scan(os.path.join(_DEVICE_CORPUS, name))
    assert report.applicable, name
    assert report.error is None, report.error
    assert report.is_clean, report.unaccounted


@requires_device_corpus
@pytest.mark.parametrize("name,length", sorted(DEVICE_TRAILERS.items()))
def test_a_real_motion_photo_trailer_is_reported_with_its_exact_length(
        name, length):
    """
    THE TRUE POSITIVES. Three real trailers on three real phones, two Google
    motion photo conventions and one Samsung SEF block.

    The byte counts come from the corpus manifest, which measured them on
    2026-09-06 with a separate instrument. This walker reaching the same figure
    from the other direction, by consuming every marker segment rather than by
    searching, is the cross-check that makes either number worth anything.
    """
    report = structure.scan(os.path.join(_DEVICE_CORPUS, name))
    assert report.applicable
    assert report.error is None, report.error
    assert len(report.unaccounted) == 1, report.unaccounted
    assert report.unaccounted[0].startswith("%d bytes after" % length), (
        report.unaccounted[0])


@requires_device_corpus
def test_every_device_file_has_an_extension_one_of_the_walkers_covers():
    """
    A file added to the corpus in a format with no walker would be scanned,
    reported not applicable, and prove nothing while looking covered.
    """
    for name in os.listdir(_DEVICE_CORPUS):
        if name.endswith(".md"):
            continue
        extension = os.path.splitext(name)[1].lower()
        assert extension in structure.supported_extensions(), (
            "%s has no walker; this corpus file proves nothing" % name)
