"""
The WebP engine: what it removes, and the proof that it removed it.

THE WALKER IN THIS FILE IS DELIBERATELY NOT THE ENGINE'S WALKER

`walk` and `assert_structurally_clean` below are a second, independent
implementation of the RIFF container rules. They do not import
`metascrub.engines.webp_engine.parse`, and they do not import
`metascrub.structure._walk_webp` either. That is the precedent set by
`test_av_muxer.read_brand`, which reimplements `ftyp_brand` rather than calling
it: a test that asks the code under test whether it succeeded is the circularity
this whole project exists to refuse, and a shared parser makes a shared parsing
bug invisible in exactly the place it matters.

WHAT IS BEING PROVEN, AND WHY EACH ONE IS A SEPARATE TEST

  1. STRUCTURE. The output's chunk inventory is a subset of the keep-list, no
     unknown chunk is present, nothing sits past the declared RIFF size, the
     size field is correct, and the even-padding rule holds. This is the gate.
     Part 3.2: it is a proof about what the file can no longer contain, and it
     does not depend on having read anything out of the input first.

  2. THE UNKNOWN CHUNK. An 8 KB MP4 in an `MPVD` chunk INSIDE the declared RIFF
     size rode through the shipped 1.0.1 as "SANITIZED ... verified clean".
     exiftool is silent about it, so the residual byte search never gets a
     needle for it. This is the single most important removal in the engine and
     it gets its own test with its own sentinel.

  3. THE INVERTED VP8X LEAK. Flags cleared to 0x00, chunks left in place. Pillow
     reports no exif, no xmp and no icc_profile; exiftool reads them all out of
     the same bytes. The test asserts BOTH halves of that measurement before
     scrubbing, so the fixture is proven to be the trap before the engine is
     asked to survive it.

  4. PIXELS. Never re-encoded. For every shape measured here the output is
     byte-for-byte the file Pillow wrote before any metadata was added to it,
     which is a stronger statement than decoded equality, so both are asserted.

  5. OVERCORRECTION. An animated WebP still animates, a lossy and a lossless
     still both survive, real alpha is still there, and a VP8X that is doing
     real work is kept. Correction (b) in Part 2.3 is here: the animated case is
     the one a flag-reconciling implementation gets wrong.

  6. MUTATION. `test_mutation_...` breaks the removal on purpose and asserts the
     structural gate catches it. A test that cannot fail is not evidence.

WHAT IS NOT ASSERTED, ON PURPOSE

There is no "the VP8X flags agree with the chunks present" assertion anywhere in
this file. Measured 2026-09-06: that assertion fails on a valid Pillow-written
animated WebP, where the alpha bit is set while alpha lives inside the ANMF
frames and there is no top-level ALPH chunk. Part 2.3 correction 2.
"""

from __future__ import annotations

import os
import struct

import pytest
from PIL import Image

from conftest import (
    ORACLE_ALLOW_WEBP,
    assert_oracle_sees_nothing,
    exiftool_or_fail,
    oracle_metadata,
    oracle_tags,
    sentinel,
)
from metascrub.engines import webp_engine
from metascrub.engines.base import EngineError
from metascrub.engines.webp_engine import WebpEngine


# ----------------------------------------------------------------- the walker
#
# Written from the RIFF and WebP container rules, not from the engine.

RIFF_HEADER_LEN = 12
CHUNK_HEADER_LEN = 8

# Everything a scrubbed WebP is allowed to still contain at the top level.
# VP8X is in the list because a file that genuinely needs alpha or animation
# keeps it; the engine drops it wherever the file does not.
TOP_LEVEL_KEEPLIST = {b"VP8 ", b"VP8L", b"VP8X", b"ALPH", b"ANIM", b"ANMF"}

# Everything an animation frame is allowed to still contain.
FRAME_KEEPLIST = {b"ALPH", b"VP8 ", b"VP8L"}

# The three named metadata chunks, asserted absent by name as well as by the
# subset rule. Belt over the gate: a keep-list typo that let one of these
# through would otherwise only show up as a subset failure.
METADATA_CHUNKS = {b"EXIF", b"XMP ", b"ICCP"}

ANMF_FRAME_HEADER_LEN = 16


def walk(data):
    """
    Every top-level chunk as (fourcc, payload), bounded by the DECLARED RIFF
    size rather than by the end of the file.

    The bound is the point. Measured 2026-09-06: a walker that reads to
    end-of-file instead reads the first four bytes of an appended MP4 as a chunk
    length and reports a 1.8 GB chunk. The declared size is what a decoder
    honours, so it is what a verifier has to honour too.
    """
    assert data[0:4] == b"RIFF", "not a RIFF file"
    assert data[8:12] == b"WEBP", "RIFF file is not a WEBP"
    total = struct.unpack("<I", data[4:8])[0] + CHUNK_HEADER_LEN
    out = []
    off = RIFF_HEADER_LEN
    while off + CHUNK_HEADER_LEN <= min(total, len(data)):
        fourcc = data[off:off + 4]
        size = struct.unpack("<I", data[off + 4:off + CHUNK_HEADER_LEN])[0]
        start = off + CHUNK_HEADER_LEN
        end = start + size
        assert end <= total, (
            f"chunk {fourcc!r} at {off} declares {size} bytes, past the "
            f"declared RIFF end {total}"
        )
        out.append((fourcc, data[start:end]))
        off = end + (size & 1)
    return out


def walk_frame(payload):
    """Sub-chunks of one ANMF animation frame, past its 16-byte frame header."""
    out = []
    off = ANMF_FRAME_HEADER_LEN
    while off + CHUNK_HEADER_LEN <= len(payload):
        fourcc = payload[off:off + 4]
        size = struct.unpack("<I", payload[off + 4:off + CHUNK_HEADER_LEN])[0]
        start = off + CHUNK_HEADER_LEN
        end = start + size
        assert end <= len(payload), f"ANMF sub-chunk {fourcc!r} runs past the frame"
        out.append((fourcc, payload[start:end]))
        off = end + (size & 1)
    return out


def trailing_bytes(data):
    """How many bytes sit past the declared RIFF size."""
    return len(data) - (struct.unpack("<I", data[4:8])[0] + CHUNK_HEADER_LEN)


def assert_structurally_clean(path):
    """
    Part 3.2's WebP gate, over the actual chunk inventory and never over the
    VP8X flag byte, which is a field an attacker controls.
    """
    with open(path, "rb") as handle:
        data = handle.read()

    assert data[0:4] == b"RIFF" and data[8:12] == b"WEBP", (
        f"{os.path.basename(path)} is no longer a WebP")

    extra = trailing_bytes(data)
    assert extra == 0, (
        f"{extra} bytes past the declared RIFF size in "
        f"{os.path.basename(path)}; that is the carrier exiftool is silent "
        "about, so nothing else in the suite would notice it")

    declared = struct.unpack("<I", data[4:8])[0]
    assert declared == len(data) - CHUNK_HEADER_LEN, (
        f"RIFF size field says {declared}, file body is "
        f"{len(data) - CHUNK_HEADER_LEN}")

    chunks = walk(data)
    assert chunks, "a WebP with no chunks at all is not a rewrite, it is a loss"

    present = [fourcc for fourcc, _ in chunks]
    # The named metadata chunks are checked FIRST. They would also fail the
    # subset rule below, but "unknown chunk MPVD" and "metadata chunk EXIF" are
    # different findings and a test that distinguishes them needs the messages
    # to distinguish them.
    leftover = sorted({f for f in present if f in METADATA_CHUNKS})
    assert not leftover, (
        f"metadata chunk(s) {leftover} survived in {os.path.basename(path)}")
    unknown = sorted({f for f in present
                      if f not in TOP_LEVEL_KEEPLIST and f not in METADATA_CHUNKS})
    assert not unknown, (
        f"unknown chunk(s) {unknown} survived in {os.path.basename(path)}. An "
        "unknown chunk inside the declared RIFF size is the 1.0.1 false-clean: "
        "an 8 KB MP4 rode through one of these")

    # The chunk chain must account for every byte of the declared payload,
    # padding included. A file that parses but does not add up has a gap in it.
    consumed = RIFF_HEADER_LEN
    for fourcc, payload in chunks:
        consumed += CHUNK_HEADER_LEN + len(payload) + (len(payload) & 1)
        if len(payload) & 1:
            pad = data[consumed - 1:consumed]
            assert pad == b"\x00", (
                f"odd-length chunk {fourcc!r} is padded with {pad!r}, not a "
                "zero byte; a pad byte is one more place to put something")
    assert consumed == len(data), (
        f"the chunk chain accounts for {consumed} of {len(data)} bytes")

    for index, (fourcc, payload) in enumerate(chunks, start=1):
        if fourcc != b"ANMF":
            continue
        sub = [name for name, _ in walk_frame(payload)]
        strange = sorted({name for name in sub if name not in FRAME_KEEPLIST})
        assert not strange, (
            f"unknown sub-chunk(s) {strange} inside ANMF frame {index}. An "
            "animation frame is a container of its own and nothing above this "
            "engine walks into it")


# --------------------------------------------------------------- the fixtures


def _write(path, image, **kwargs):
    image.save(path, format="WEBP", **kwargs)
    return path


def plain_lossy(tmp_path, name="lossy.webp", size=(64, 48)):
    return _write(str(tmp_path / name), Image.new("RGB", size, (200, 30, 30)),
                  lossless=False, quality=80)


def plain_lossless(tmp_path, name="lossless.webp", size=(64, 48)):
    return _write(str(tmp_path / name), Image.new("RGB", size, (30, 200, 30)),
                  lossless=True)


def _alpha_image(size=(64, 48)):
    image = Image.new("RGBA", size, (30, 30, 200, 255))
    for x in range(min(20, size[0])):
        for y in range(min(20, size[1])):
            image.putpixel((x, y), (255, 255, 0, 17))
    return image


def alpha_lossy(tmp_path, name="alpha_lossy.webp"):
    """A lossy WebP with a real top-level ALPH chunk, so VP8X must survive."""
    return _write(str(tmp_path / name), _alpha_image(), lossless=False, quality=80)


def alpha_lossless(tmp_path, name="alpha_lossless.webp"):
    """A lossless WebP whose alpha lives inside the VP8L bitstream, no VP8X."""
    return _write(str(tmp_path / name), _alpha_image(), lossless=True)


def animated(tmp_path, name="anim.webp"):
    """
    Two frames with alpha. Measured 2026-09-06: Pillow writes VP8X with the
    ALPH bit set and NO top-level ALPH chunk, because alpha is inside each
    ANMF. This is correction (b)'s file, and the reason there is no
    flags-agree-with-chunks assertion in this suite.
    """
    first = Image.new("RGBA", (64, 48), (10, 10, 10, 255))
    second = Image.new("RGBA", (64, 48), (200, 200, 10, 120))
    return _write(str(tmp_path / name), first, save_all=True,
                  append_images=[second], duration=100, loop=0)


def odd_length_chunk(tmp_path, name="odd.webp"):
    """
    A real, decodable WebP whose kept image chunk has an ODD payload length, so
    the even-padding rule is exercised on a chunk the engine keeps rather than
    only on one it drops. Measured: Pillow's lossless 8x9 gives VP8L = 17 bytes.
    """
    path = _write(str(tmp_path / name), Image.new("RGB", (8, 9), (40, 90, 7)),
                  lossless=True)
    with open(path, "rb") as handle:
        chunks = walk(handle.read())
    assert len(chunks) == 1 and chunks[0][0] == b"VP8L", chunks
    assert len(chunks[0][1]) % 2 == 1, (
        "this fixture only tests anything while its VP8L payload is odd; "
        f"it is {len(chunks[0][1])} bytes")
    return path


def load_metadata(path):
    """
    Put EXIF (with GPS) and XMP into a WebP with exiftool, and prove both
    landed. exiftool synthesises the VP8X header on the way in, which is the
    measured fact that makes correction 3 available at all: a simple WebP has
    nowhere to put metadata, so writing any forces the extended format.

    ICCP is loaded separately, by test_the_icc_profile_chunk_is_removed, so
    that a failure there names the ICC path rather than every WebP test at
    once.

    Returns (exif_sentinel, xmp_sentinel).
    """
    exiftool_or_fail()
    exif_value = sentinel("webpexif")
    xmp_value = sentinel("webpxmp")

    from metascrub import exif_io

    args = [
        f"-EXIF:Artist={exif_value}",
        "-EXIF:Make=SyntheticVendor",
        f"-XMP-dc:Creator={xmp_value}",
        "-GPSLatitude=44.9537",
        "-GPSLatitudeRef=N",
        "-GPSLongitude=-72.5778",
        "-GPSLongitudeRef=W",
    ]
    exif_io.session().execute(*args, "-overwrite_original", path)

    with open(path, "rb") as handle:
        raw = handle.read()
    assert exif_value.encode() in raw, (
        "the fixture did not store its EXIF sentinel; a scrub of it would pass "
        "by having nothing to remove")
    assert xmp_value.encode() in raw, "the fixture did not store its XMP sentinel"
    present = {fourcc for fourcc, _ in walk(raw)}
    assert b"EXIF" in present and b"XMP " in present, (
        f"expected EXIF and XMP chunks in the loaded fixture, got {sorted(present)}")
    return exif_value, xmp_value


def rebuild(chunks):
    """Reassemble a WebP from (fourcc, payload) pairs. Used to BUILD traps."""
    body = bytearray()
    for fourcc, payload in chunks:
        body += fourcc + struct.pack("<I", len(payload)) + payload
        if len(payload) & 1:
            body += b"\x00"
    return b"RIFF" + struct.pack("<I", len(body) + 4) + b"WEBP" + bytes(body)


def frames_of(path):
    """Every frame of a WebP as raw RGBA bytes, plus its duration."""
    out = []
    with Image.open(path) as image:
        while True:
            out.append((image.convert("RGBA").tobytes(),
                        image.info.get("duration")))
            try:
                image.seek(image.tell() + 1)
            except EOFError:
                break
    return out


def scrub(path):
    """Run the engine and hand back what it said it removed."""
    return WebpEngine().strip_all(path)


# --------------------------------------------------------------------- basics


def test_the_engine_reports_itself_available():
    ok, reason = WebpEngine().available()
    assert ok, f"the webp engine reports itself unavailable: {reason}"


def test_the_walker_in_this_file_can_actually_fail(tmp_path):
    """
    The instrument, checked before anything is measured with it.

    A structural assertion that cannot fail is the quietest possible pass, and
    this suite leans on `assert_structurally_clean` for almost everything.
    """
    good = plain_lossy(tmp_path)
    assert_structurally_clean(good)

    with open(good, "rb") as handle:
        data = handle.read()

    with open(str(tmp_path / "trailing.webp"), "wb") as handle:
        handle.write(data + b"NOT PART OF THE IMAGE")
    with pytest.raises(AssertionError, match="past the declared RIFF size"):
        assert_structurally_clean(str(tmp_path / "trailing.webp"))

    chunks = walk(data)
    with open(str(tmp_path / "unknown.webp"), "wb") as handle:
        handle.write(rebuild(chunks + [(b"MPVD", b"x" * 32)]))
    with pytest.raises(AssertionError, match="unknown chunk"):
        assert_structurally_clean(str(tmp_path / "unknown.webp"))

    with open(str(tmp_path / "meta.webp"), "wb") as handle:
        handle.write(rebuild(chunks + [(b"EXIF", b"MM\x00*" + b"y" * 30)]))
    with pytest.raises(AssertionError, match="metadata chunk"):
        assert_structurally_clean(str(tmp_path / "meta.webp"))

    lying = bytearray(data)
    lying[4:8] = struct.pack("<I", struct.unpack("<I", data[4:8])[0] - 2)
    with open(str(tmp_path / "lying.webp"), "wb") as handle:
        handle.write(bytes(lying))
    with pytest.raises(AssertionError):
        assert_structurally_clean(str(tmp_path / "lying.webp"))


# ------------------------------------------------------------ the three chunks


def test_metadata_chunks_are_removed_from_a_lossy_still(tmp_path):
    path = plain_lossy(tmp_path)
    with open(path, "rb") as handle:
        pristine = handle.read()

    exif_value, xmp_value = load_metadata(path)
    scrub(path)

    with open(path, "rb") as handle:
        out = handle.read()
    assert exif_value.encode() not in out, "the EXIF sentinel survived the scrub"
    assert xmp_value.encode() not in out, "the XMP sentinel survived the scrub"
    assert_structurally_clean(path)

    # Stronger than "the metadata is gone": the output is the file Pillow wrote
    # before exiftool ever touched it, byte for byte.
    assert out == pristine, (
        "the rewritten WebP is not byte-identical to the pre-metadata original")

    assert_oracle_sees_nothing(
        path, allow=ORACLE_ALLOW_WEBP,
        note="lossy still WebP scrubbed by the pure-Python engine; the five "
             "allowed names are VP8 frame-header fields, which are the "
             "compressed image itself")


def test_the_icc_profile_chunk_is_removed(tmp_path):
    """
    ICCP gets its own test because it is the one of the three that carries a
    wall-clock timestamp: measured 2026-09-06, exiftool reports
    ICC-header:ProfileDateTime out of a WebP whose VP8X flags claim no ICC.
    """
    from PIL import ImageCms

    path = plain_lossy(tmp_path)
    profile = str(tmp_path / "srgb.icc")
    with open(profile, "wb") as handle:
        handle.write(ImageCms.ImageCmsProfile(ImageCms.createProfile("sRGB")).tobytes())

    from metascrub import exif_io
    exiftool_or_fail()
    exif_io.session().execute(f"-ICC_Profile<={profile}", "-overwrite_original", path)

    with open(path, "rb") as handle:
        before = {fourcc for fourcc, _ in walk(handle.read())}
    assert b"ICCP" in before, f"fixture has no ICCP chunk, only {sorted(before)}"

    removed = scrub(path)
    assert any("ICCP" in line for line in removed), removed
    assert_structurally_clean(path)
    assert not any(key.startswith("ICC") for key in oracle_tags(path)), (
        f"the oracle still sees ICC tags: {sorted(oracle_tags(path))}")


def test_the_mobile_fixture_scrubs_clean(mobile_fixture):
    """
    The shared synthetic Pixel WebP, so this engine is measured against the same
    fixture the rest of the mobile work uses rather than only against its own.
    """
    path, exif_value = mobile_fixture(".webp")
    scrub(path)

    with open(path, "rb") as handle:
        out = handle.read()
    assert exif_value.encode() not in out
    assert_structurally_clean(path)
    assert_oracle_sees_nothing(
        path, allow=ORACLE_ALLOW_WEBP,
        note="the shared mobile WebP fixture, lossy still, after the "
             "pure-Python engine")

    survivors = [key for key in oracle_tags(path)
                 if key.split(":", 1)[0] in ("EXIF", "IFD0", "GPS", "XMP",
                                             "XMP-dc", "ICC_Profile")]
    assert not survivors, f"identity carriers survived: {survivors}"


# --------------------------------------------------- the unknown chunk, and it
# --------------------------------------------------- is the whole reason for
# --------------------------------------------------- this engine


def test_an_unknown_chunk_inside_the_riff_size_is_removed(tmp_path):
    """
    The 1.0.1 false clean, reproduced and then fixed.

    An 8 KB MP4 in an unknown `MPVD` chunk, INSIDE the declared RIFF size, was
    reported "SANITIZED ... verified clean" by the shipped tool while the video
    and its `ftyp` survived intact. structure.py made that visible in 1.0.2.
    This is the code that removes it.
    """
    path = plain_lossy(tmp_path)
    with open(path, "rb") as handle:
        pristine = handle.read()

    hidden = sentinel("webpmpvd")
    video = (b"\x00\x00\x00\x18ftypmp42\x00\x00\x00\x00mp42isom"
             + hidden.encode() + b"\x00" * 8192)
    with open(path, "wb") as handle:
        handle.write(rebuild(walk(pristine) + [(b"MPVD", video)]))

    with open(path, "rb") as handle:
        loaded = handle.read()
    assert hidden.encode() in loaded and b"ftypmp42" in loaded
    assert trailing_bytes(loaded) == 0, (
        "this fixture is only the 1.0.1 case while the MP4 is INSIDE the "
        "declared RIFF size; a trailer is a different carrier with a "
        "different test")

    # The oracle is silent on this. Asserted, not assumed, because it is the
    # reason a needle search can never find this carrier: Part 3.3.
    assert not [key for key in oracle_tags(path) if not key.startswith("RIFF:")], (
        "exiftool suddenly reports something about a hidden MPVD chunk; that "
        "would be good news, but it would also mean this test is no longer "
        "measuring the blind spot it was written for")

    removed = scrub(path)
    assert any("MPVD" in line for line in removed), (
        f"the engine did not say it removed the unknown chunk: {removed}")

    with open(path, "rb") as handle:
        out = handle.read()
    assert hidden.encode() not in out, "the embedded MP4's sentinel survived"
    assert b"ftypmp42" not in out, "the embedded MP4's ftyp survived"
    assert out == pristine
    assert_structurally_clean(path)


def test_an_unknown_sub_chunk_inside_an_animation_frame_is_removed(tmp_path):
    """
    The same carrier one level down. An ANMF payload is its own sub-chunk chain
    and nothing above this engine walks into it, which makes it the natural
    second place to hide 8 KB of MP4 once the top level is swept.
    """
    path = animated(tmp_path)
    with open(path, "rb") as handle:
        pristine = handle.read()
    before = frames_of(path)

    hidden = sentinel("webpanmfsub")
    chunks = walk(pristine)
    rebuilt = []
    injected = 0
    for fourcc, payload in chunks:
        if fourcc == b"ANMF" and not injected:
            blob = b"HIDE" + struct.pack("<I", len(hidden) + 64) \
                + hidden.encode() + b"\x00" * 64
            payload = payload + blob
            injected += 1
        rebuilt.append((fourcc, payload))
    assert injected == 1, "no ANMF frame to inject into"
    with open(path, "wb") as handle:
        handle.write(rebuild(rebuilt))

    with open(path, "rb") as handle:
        assert hidden.encode() in handle.read()

    removed = scrub(path)
    assert any("HIDE" in line for line in removed), removed

    with open(path, "rb") as handle:
        out = handle.read()
    assert hidden.encode() not in out
    assert out == pristine, "the frame did not come back byte-identical"
    assert_structurally_clean(path)
    assert frames_of(path) == before, "the animation changed"


def test_bytes_past_the_declared_riff_size_are_removed(tmp_path):
    """
    Measured: exiftool 13.29 emits no warning at all for this, unlike the PNG
    trailer case. So nothing else in the pipeline can see it.
    """
    path = plain_lossy(tmp_path)
    with open(path, "rb") as handle:
        pristine = handle.read()

    hidden = sentinel("webptrailer")
    with open(path, "ab") as handle:
        handle.write(hidden.encode() + b"\x00" * 4096)

    with open(path, "rb") as handle:
        loaded = handle.read()
    assert trailing_bytes(loaded) == len(hidden) + 4096

    removed = scrub(path)
    assert any("past the declared RIFF size" in line for line in removed), removed

    with open(path, "rb") as handle:
        out = handle.read()
    assert hidden.encode() not in out
    assert out == pristine
    assert_structurally_clean(path)


# ------------------------------------------------------- the inverted VP8X leak


def test_flags_cleared_but_chunks_present_is_still_cleaned(tmp_path):
    """
    Part 2.3 correction 1, the direction the original text did not warn about.

    With the VP8X flag byte cleared to 0x00 and the chunks left in place,
    Pillow reports no exif and no xmp while exiftool reads both out of the same
    bytes. A verifier that asks a flag byte, or asks Pillow, says CLEAN on a
    file that still has GPS in it.

    Both halves of that are asserted BEFORE the scrub. A fixture that is not
    actually the trap cannot prove the engine survives the trap.
    """
    path = plain_lossy(tmp_path)
    with open(path, "rb") as handle:
        pristine = handle.read()
    exif_value, xmp_value = load_metadata(path)

    with open(path, "rb") as handle:
        data = bytearray(handle.read())
    assert data[12:16] == b"VP8X", (
        f"expected exiftool to synthesise VP8X first, found {bytes(data[12:16])!r}")
    flag_offset = 12 + 8            # chunk header, then payload byte 0
    assert data[flag_offset] != 0x00, "the fixture's flags are already zero"
    data[flag_offset] = 0x00
    with open(path, "wb") as handle:
        handle.write(bytes(data))

    # Half one: libwebp trusts the flags and stops looking.
    with Image.open(path) as image:
        keys = sorted(image.info)
    assert "exif" not in keys and "xmp" not in keys, (
        f"Pillow still reports metadata keys {keys}; this fixture is supposed "
        "to be the one Pillow cannot see")

    # Half two: exiftool walks the chunks and finds everything anyway.
    metadata = oracle_metadata(path)
    values = {str(value) for value in metadata.values()}
    assert exif_value in values and xmp_value in values, (
        "exiftool cannot see the sentinels either, so this fixture is not the "
        "leak it claims to be")

    removed = scrub(path)
    assert any("EXIF" in line for line in removed), removed
    assert any("XMP" in line for line in removed), removed

    with open(path, "rb") as handle:
        out = handle.read()
    assert exif_value.encode() not in out, (
        "the engine trusted the flag byte instead of the chunk inventory")
    assert xmp_value.encode() not in out
    assert out == pristine
    assert_structurally_clean(path)
    assert_oracle_sees_nothing(
        path, allow=ORACLE_ALLOW_WEBP,
        note="the inverted VP8X fixture: flags cleared, chunks present")


# ------------------------------------------------------------------- VP8X policy


def test_vp8x_is_dropped_when_the_file_does_not_need_it(tmp_path):
    """
    Part 2.3 correction 3, and the strongest claim available on this container:
    a WebP with no VP8X cannot carry metadata at all, because the simple form is
    RIFF/WEBP plus exactly one image chunk and there is nowhere to put anything
    else. Dropping the header retires the flag question instead of policing it.
    """
    path = plain_lossy(tmp_path)
    load_metadata(path)
    assert b"VP8X" in {fourcc for fourcc, _ in walk(open(path, "rb").read())}

    removed = scrub(path)
    assert any("VP8X" in line for line in removed), removed

    with open(path, "rb") as handle:
        chunks = walk(handle.read())
    assert [fourcc for fourcc, _ in chunks] == [b"VP8 "], (
        f"expected a simple WebP, got {[f for f, _ in chunks]}")


def test_vp8x_is_kept_when_the_file_needs_it_for_alpha(tmp_path):
    """
    The overcorrection guard on the rule above. A top-level ALPH chunk is only
    legal in an extended-format file, so dropping VP8X here would produce a
    container no decoder is required to understand.
    """
    path = alpha_lossy(tmp_path)
    before = frames_of(path)
    load_metadata(path)
    scrub(path)

    with open(path, "rb") as handle:
        present = [fourcc for fourcc, _ in walk(handle.read())]
    assert present[0] == b"VP8X", f"VP8X was dropped from an alpha file: {present}"
    assert b"ALPH" in present, f"the alpha chunk was dropped: {present}"
    assert_structurally_clean(path)
    assert frames_of(path) == before, "the alpha channel changed"


def test_vp8x_is_kept_when_its_canvas_does_not_match_the_image(tmp_path):
    """
    A VP8X whose canvas differs from the bitstream is doing real work: it is
    what makes the file render at a size the image chunk does not declare.
    Dropping it would change what the file looks like, which is not a metadata
    operation. So the drop is conditional on the two agreeing, and this is the
    test that keeps that condition honest.
    """
    path = plain_lossy(tmp_path)
    load_metadata(path)

    with open(path, "rb") as handle:
        chunks = walk(handle.read())
    patched = []
    for fourcc, payload in chunks:
        if fourcc == b"VP8X":
            # canvas width - 1, three bytes little-endian, set to 128 wide
            payload = payload[:4] + (127).to_bytes(3, "little") + payload[7:]
        patched.append((fourcc, payload))
    with open(path, "wb") as handle:
        handle.write(rebuild(patched))

    scrub(path)

    with open(path, "rb") as handle:
        out = handle.read()
    present = [fourcc for fourcc, _ in walk(out)]
    assert present[0] == b"VP8X", (
        f"VP8X was dropped even though its canvas is 128 wide and the VP8 "
        f"bitstream is 64 wide: {present}")
    assert_structurally_clean(path)


def test_the_surviving_vp8x_has_no_metadata_or_reserved_bits_set(tmp_path):
    """
    Where VP8X stays, the metadata flag bits are cleared to match reality, and
    so are the 27 reserved bits: 3 in the flag byte and the whole 24-bit
    reserved field. The specification says they are zero. Bits nothing reads are
    a place to put something.
    """
    path = alpha_lossy(tmp_path)
    load_metadata(path)

    with open(path, "rb") as handle:
        chunks = walk(handle.read())
    patched = [(f, bytes([p[0]]) + b"\xAB\xCD\xEF" + p[4:]) if f == b"VP8X"
               else (f, p) for f, p in chunks]
    with open(path, "wb") as handle:
        handle.write(rebuild(patched))

    scrub(path)

    with open(path, "rb") as handle:
        vp8x = dict(walk(handle.read()))[b"VP8X"]
    flags = vp8x[0]
    assert flags & 0x20 == 0, "the ICC flag bit is still set"
    assert flags & 0x08 == 0, "the EXIF flag bit is still set"
    assert flags & 0x04 == 0, "the XMP flag bit is still set"
    assert flags & 0xC1 == 0, f"reserved flag bits are set: 0x{flags:02X}"
    assert vp8x[1:4] == b"\x00\x00\x00", (
        f"the 24-bit reserved field still holds {vp8x[1:4]!r}")
    assert flags & 0x10, "the alpha bit was cleared on a file that has alpha"


# ------------------------------------------------------------- overcorrection


SHAPES = ["lossy", "lossless", "alpha_lossy", "alpha_lossless", "animated",
          "odd_length_chunk"]

_BUILDERS = {
    "lossy": plain_lossy,
    "lossless": plain_lossless,
    "alpha_lossy": alpha_lossy,
    "alpha_lossless": alpha_lossless,
    "animated": animated,
    "odd_length_chunk": odd_length_chunk,
}


@pytest.mark.parametrize("shape", SHAPES)
def test_every_shape_survives_with_identical_pixels(tmp_path, shape):
    """
    Never re-encode. Asserted twice, because the two say different things:
    decoded frames are identical (the image did not change) AND the output is
    byte-for-byte the file Pillow wrote before any metadata went in (the
    bitstream was copied, not recompressed into something that happens to
    decode the same).
    """
    path = _BUILDERS[shape](tmp_path)
    with open(path, "rb") as handle:
        pristine = handle.read()
    before = frames_of(path)

    exif_value, xmp_value = load_metadata(path)
    scrub(path)

    with open(path, "rb") as handle:
        out = handle.read()
    assert exif_value.encode() not in out
    assert xmp_value.encode() not in out
    assert_structurally_clean(path)
    assert frames_of(path) == before, f"{shape}: decoded frames changed"
    assert out == pristine, f"{shape}: output is not the pre-metadata original"


def test_an_animated_webp_still_animates(tmp_path):
    """
    Correction (b)'s file, and the one most likely to break. Pillow writes VP8X
    with the ALPH bit set and no top-level ALPH chunk here, so an engine that
    reconciled flags against the chunk inventory would either fail the file or
    clear a bit the frames rely on.
    """
    path = animated(tmp_path)
    load_metadata(path)
    scrub(path)

    with Image.open(path) as image:
        assert getattr(image, "is_animated", False), "the output no longer animates"
        assert image.n_frames == 2, f"expected 2 frames, got {image.n_frames}"

    # Frame timing is read out of the ANMF frame headers rather than out of
    # Pillow. Measured 2026-09-06 on Pillow 12.1.1: `info['duration']` is None
    # for every frame of a WebP animation it wrote itself, so an assertion on
    # that field would have passed whatever the engine did to the timing.
    with open(path, "rb") as handle:
        chunks = walk(handle.read())
    present = [fourcc for fourcc, _ in chunks]
    durations = [int.from_bytes(payload[12:15], "little")
                 for fourcc, payload in chunks if fourcc == b"ANMF"]
    assert durations == [100, 100], f"frame durations changed: {durations}"
    assert present.count(b"ANMF") == 2, f"animation frames were lost: {present}"
    assert b"ANIM" in present, f"the animation header was lost: {present}"
    assert_structurally_clean(path)


def test_real_alpha_is_still_there_after_the_scrub(tmp_path):
    """
    Both alpha carriers, because they are structurally different: a top-level
    ALPH chunk beside a lossy VP8, and an alpha channel inside a VP8L bitstream
    with no VP8X at all.
    """
    for builder, name in ((alpha_lossy, "alpha_lossy"),
                          (alpha_lossless, "alpha_lossless")):
        path = builder(tmp_path, name=f"{name}.webp")
        with Image.open(path) as image:
            before = image.convert("RGBA").getpixel((0, 0))
        assert before[3] != 255, (
            f"{name} fixture has no transparency to lose: {before}")

        load_metadata(path)
        scrub(path)

        with Image.open(path) as image:
            after = image.convert("RGBA")
            assert after.mode == "RGBA"
            assert after.getpixel((0, 0)) == before, (
                f"{name}: the transparent pixel changed from {before} to "
                f"{after.getpixel((0, 0))}")
        assert_structurally_clean(path)


@pytest.mark.parametrize("shape", ["lossless", "alpha_lossless", "animated",
                                   "alpha_lossy"])
def test_the_oracle_sees_no_identity_carrier_in_any_shape(tmp_path, shape):
    """
    The oracle cross-check for the shapes ORACLE_ALLOW_WEBP does not cover.

    Measured 2026-09-06 on this engine's output: a lossless still reports
    RIFF:AlphaIsUsed, an alpha lossy adds RIFF:AlphaCompression,
    RIFF:AlphaFiltering, RIFF:AlphaPreprocessing and RIFF:WebP_Flags, and an
    animation reports RIFF:AnimationLoopCount, RIFF:BackgroundColor and
    RIFF:Duration. None of those five-name lists is ORACLE_ALLOW_WEBP, which was
    measured on the exiftool engine's output and holds exactly for a lossy
    still.

    Widening a shared allowlist is conftest's decision and not this file's, so
    this asserts the thing that needs no allowlist at all: no tag from any group
    that can carry identity survives, and no sentinel value is reachable through
    exiftool. Trap 11 - every name added to an allowlist is a name nobody checks
    again, so the check that names nothing is the one that cannot rot.
    """
    path = _BUILDERS[shape](tmp_path)
    exif_value, xmp_value = load_metadata(path)
    scrub(path)

    identity_groups = ("EXIF", "IFD0", "IFD1", "GPS", "XMP", "XMP-dc", "XMP-x",
                       "ICC_Profile", "ICC-header", "Photoshop", "IPTC")
    survivors = [key for key in oracle_tags(path)
                 if key.split(":", 1)[0] in identity_groups]
    assert not survivors, f"{shape}: identity carriers survived: {survivors}"

    values = {str(value) for value in oracle_metadata(path).values()}
    assert exif_value not in values and xmp_value not in values, (
        f"{shape}: the oracle can still read a sentinel back")


# ------------------------------------------------------------------ fail closed


def test_a_chunk_running_past_the_container_is_refused(tmp_path):
    path = plain_lossy(tmp_path)
    with open(path, "rb") as handle:
        data = bytearray(handle.read())
    # First chunk's length field, made larger than the container.
    data[16:20] = struct.pack("<I", 0x00FFFFFF)
    with open(path, "wb") as handle:
        handle.write(bytes(data))
    with pytest.raises(EngineError, match="runs past the end"):
        scrub(path)


def test_a_truncated_container_is_refused(tmp_path):
    path = plain_lossy(tmp_path)
    with open(path, "rb") as handle:
        data = handle.read()
    with open(path, "wb") as handle:
        handle.write(data[:-6])
    with pytest.raises(EngineError, match="truncated"):
        scrub(path)


def test_a_file_that_is_not_a_webp_is_refused(tmp_path):
    path = str(tmp_path / "not.webp")
    with open(path, "wb") as handle:
        handle.write(b"\x89PNG\r\n\x1a\n" + b"\x00" * 64)
    with pytest.raises(EngineError, match="RIFF/WEBP header mismatch"):
        scrub(path)


def test_a_failed_rewrite_leaves_no_temporary_file(tmp_path):
    """
    A refusal must not litter the user's directory with .metascrub-* files, and
    must leave the input exactly as it was.
    """
    path = plain_lossy(tmp_path)
    with open(path, "rb") as handle:
        data = bytearray(handle.read())
    before = bytes(data)
    data[16:20] = struct.pack("<I", 0x00FFFFFF)
    with open(path, "wb") as handle:
        handle.write(bytes(data))

    with pytest.raises(EngineError):
        scrub(path)
    leftovers = [name for name in os.listdir(str(tmp_path))
                 if name.startswith(".metascrub-")]
    assert not leftovers, f"temporary files left behind: {leftovers}"
    with open(path, "rb") as handle:
        assert handle.read() == bytes(data), "a refused input was modified"
    assert before != bytes(data)   # the fixture really was the broken one


# --------------------------------------------------------------- the mutation
#
# "A green test suite is not evidence of correctness." These break the removal
# on purpose and assert that something in this file notices.


def test_mutation_treating_the_unknown_chunk_as_known_is_caught(tmp_path, monkeypatch):
    """
    THE MUTATION: add MPVD to the engine's keep-list, which is precisely the
    1.0.1 behaviour, and re-run the assertions of
    test_an_unknown_chunk_inside_the_riff_size_is_removed.

    Everything else about the engine still works: the EXIF and XMP chunks still
    go, the oracle is still silent, the file still decodes. The only thing that
    catches it is the structural inventory check, which is the argument for
    having one.
    """
    path = plain_lossy(tmp_path)
    with open(path, "rb") as handle:
        pristine = handle.read()
    hidden = sentinel("webpmutant")
    video = b"\x00\x00\x00\x18ftypmp42" + hidden.encode() + b"\x00" * 512
    with open(path, "wb") as handle:
        handle.write(rebuild(walk(pristine) + [(b"MPVD", video)]))

    monkeypatch.setattr(webp_engine, "KEEP", webp_engine.KEEP | {b"MPVD"})
    scrub(path)

    with open(path, "rb") as handle:
        out = handle.read()
    assert hidden.encode() in out, (
        "the mutation did not actually change the behaviour, so this test "
        "proves nothing about the assertion below")

    with pytest.raises(AssertionError, match="unknown chunk"):
        assert_structurally_clean(path)


def test_mutation_keeping_the_trailer_is_caught(tmp_path, monkeypatch):
    """
    THE SECOND MUTATION: rebuild from the whole file instead of from the
    declared RIFF size, which is the mistake a walker makes when it reads to
    end-of-file. Caught by the trailing-bytes clause.
    """
    path = plain_lossy(tmp_path)
    with open(path, "rb") as handle:
        pristine = handle.read()
    hidden = sentinel("webpmutanttrail")
    with open(path, "wb") as handle:
        handle.write(pristine + hidden.encode() + b"\x00" * 256)

    real_build = webp_engine._build
    with open(path, "rb") as handle:
        appended = handle.read()[len(pristine):]

    def leaky_build(chunks):
        return real_build(chunks) + appended

    monkeypatch.setattr(webp_engine, "_build", leaky_build)
    scrub(path)

    with open(path, "rb") as handle:
        out = handle.read()
    assert hidden.encode() in out, "the mutation did not change the behaviour"
    with pytest.raises(AssertionError, match="past the declared RIFF size"):
        assert_structurally_clean(path)


def test_mutation_clearing_the_flags_instead_of_removing_the_chunks_is_caught(
        tmp_path, monkeypatch):
    """
    THE THIRD MUTATION, and the one the whole 2.3 correction is about: an engine
    that "removes" metadata by clearing the VP8X flag bits and leaving the
    chunks where they are. Pillow calls the result clean. The structural
    inventory check does not.
    """
    # An alpha file, so VP8X is not eligible for the drop and the mutant
    # produces exactly the measured w_E shape: flags cleared, chunks present.
    path = alpha_lossy(tmp_path)
    load_metadata(path)

    monkeypatch.setattr(webp_engine, "METADATA_CHUNKS", frozenset())
    monkeypatch.setattr(webp_engine, "KEEP",
                        webp_engine.KEEP | {b"EXIF", b"XMP ", b"ICCP"})
    scrub(path)

    with Image.open(path) as image:
        assert "exif" not in image.info, (
            "the mutation did not produce the fail-open shape it is testing")

    with pytest.raises(AssertionError, match="metadata chunk"):
        assert_structurally_clean(path)
