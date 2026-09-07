"""
The PNG engine: does it actually remove the chunks, and does it leave the image
alone while doing it.

THE ORDERING THIS FILE ENFORCES, AND WHY IT IS NOT THE PROJECT'S USUAL ONE

Everywhere else in this suite the residual byte search is the gate. For PNG it
cannot be, and that is a measurement rather than a preference. `zTXt` is
zlib-compressed, so a sentinel written into one is NOT PRESENT in the file's
bytes at all, before or after any scrubbing. A byte search for it returns
nothing on the ORIGINAL file, which means a scrubber that removed nothing would
pass the byte search. `test_ztxt_defeats_the_byte_search_so_the_structure_is_
the_gate` proves exactly that, on the fixture, before it proves the removal.

So: the STRUCTURAL assertion is the gate here, the byte search is the belt, and
the exiftool oracle is the independent third party. Section 3.2, and the
2026-09-06 correction to it.

THREE INDEPENDENT WALKERS, ON PURPOSE

`walk()` below is written from the PNG specification and is NOT
`metascrub.structure._walk_png` and NOT the engine's `_rebuild`. A checker that
shares a parser with the thing it checks cannot catch a parser bug. This is the
precedent `test_av_muxer.read_brand` set by deliberately not importing the
engine's own `ftyp_brand`.

WHAT IS DELIBERATELY NOT ASSERTED HERE

That removing `gAMA`, `sRGB`, `cHRM` or `sBIT` would change how the image
renders. Measured 2026-09-06 (scratchpad phase0-findings, Q7): Pillow and ffmpeg
produce byte-identical pixel data with or without any of them, so there is no
rendering difference to assert with the decoders on this machine. The
overcorrection tests below therefore assert that the chunks SURVIVE and that
the metadata a decoder exposes from them survives, which is what is actually
measurable.
"""

from __future__ import annotations

import os
import struct
import zlib

import pytest

from conftest import (
    ORACLE_ALLOW_PNG,
    ORACLE_ALLOW_PNG_KEEPLIST,
    assert_oracle_sees_nothing,
    exiftool_or_fail,
    oracle_metadata,
    oracle_tags,
    raw_contains,
    sentinel,
)
from metascrub.engines import png_engine
from metascrub.engines.base import EngineError

SIG = b"\x89PNG\r\n\x1a\n"

# The section 2.2 keep-list, written out literally rather than imported from the
# engine. Importing it would make this test agree with any keep-list the engine
# happened to have, including a widened one. A list that shrinks or grows with
# the thing it tests asserts progressively less while staying green; same reason
# test_av_muxer names its four extensions literally.
SECTION_2_2_KEEP = {
    b"IHDR", b"PLTE", b"IDAT", b"IEND",
    b"tRNS", b"sRGB", b"gAMA", b"cHRM", b"sBIT", b"bKGD", b"pHYs",
    b"acTL", b"fcTL", b"fdAT",
}


# ---------------------------------------------------------------- the walker


def walk(data):
    """
    Yield one record per chunk, plus a final `<TRAILER>` record when there are
    bytes after IEND.

    Written from the PNG specification: signature, then
    `length(4) type(4) payload(length) crc(4)` until IEND. Independent of the
    engine and of metascrub.structure by construction.
    """
    if not data.startswith(SIG):
        raise AssertionError("not a PNG: signature mismatch")
    offset = len(SIG)
    total = len(data)
    while offset + 8 <= total:
        (length,) = struct.unpack(">I", data[offset:offset + 4])
        ctype = data[offset + 4:offset + 8]
        end = offset + 12 + length
        if end > total:
            raise AssertionError(
                f"chunk {ctype!r} at {offset} runs past the end of the file"
            )
        stored = data[end - 4:end]
        computed = struct.pack(">I", zlib.crc32(data[offset + 4:end - 4]) & 0xFFFFFFFF)
        yield {
            "type": ctype,
            "offset": offset,
            "length": length,
            "payload": data[offset + 8:end - 4],
            "crc_ok": stored == computed,
            "ancillary": bool(ctype[0] & 0x20),
        }
        offset = end
        if ctype == b"IEND":
            if offset < total:
                yield {
                    "type": b"<TRAILER>",
                    "offset": offset,
                    "length": total - offset,
                    "payload": data[offset:],
                    "crc_ok": None,
                    "ancillary": None,
                }
            return
    raise AssertionError("walked off the end without finding IEND")


def assert_structurally_clean(path, note=""):
    """
    Section 3.2's PNG rule: the set of chunk types present is a subset of the
    keep-list, and there are zero bytes after IEND.

    This is the gate. It does not depend on having read a value first, so it
    reaches the carriers a needle search cannot: a compressed zTXt, an unknown
    chunk exiftool never reports, a trailer past IEND.
    """
    with open(path, "rb") as handle:
        data = handle.read()
    records = list(walk(data))
    present = [rec["type"] for rec in records]
    trailing = [rec for rec in records if rec["type"] == b"<TRAILER>"]
    strays = sorted({t for t in present if t not in SECTION_2_2_KEEP and t != b"<TRAILER>"})
    assert not strays, (
        f"chunk types outside the section 2.2 keep-list survived: "
        f"{[t.decode('latin-1', 'replace') for t in strays]}"
        f"{(' (' + note + ')') if note else ''}"
    )
    assert not trailing, (
        f"{sum(rec['length'] for rec in trailing)} bytes survive after IEND"
        f"{(' (' + note + ')') if note else ''}"
    )
    assert present and present[0] == b"IHDR", "IHDR must still be the first chunk"
    assert b"IDAT" in present and b"IEND" in present, "the image itself must survive"


# ---------------------------------------------------------------- fixtures


def make_chunk(ctype, payload):
    """Build one syntactically valid chunk, CRC included."""
    body = ctype + payload
    return (struct.pack(">I", len(payload)) + body
            + struct.pack(">I", zlib.crc32(body) & 0xFFFFFFFF))


def insert_after_ihdr(data, chunks):
    """Splice chunks in immediately after IHDR, where ancillary chunks belong."""
    for rec in walk(data):
        if rec["type"] == b"IHDR":
            at = rec["offset"] + 12 + rec["length"]
            return data[:at] + b"".join(chunks) + data[at:]
    raise AssertionError("no IHDR")


def plain_png(tmp_path, name="plain.png", size=(48, 32), mode="RGB", colour=(30, 90, 160)):
    from PIL import Image

    path = str(tmp_path / name)
    Image.new(mode, size, colour).save(path, format="PNG")
    return path


def write(path, data):
    with open(path, "wb") as handle:
        handle.write(data)
    return path


def read(path):
    with open(path, "rb") as handle:
        return handle.read()


def pixels(path):
    """Decoded pixel bytes plus the geometry, for a before/after comparison."""
    from PIL import Image

    with Image.open(path) as im:
        im.load()
        return im.mode, im.size, im.convert("RGBA").tobytes()


def scrub(path):
    return png_engine.PngEngine().strip_all(path)


# ---------------------------------------------------------------- the engine's own claims


def test_the_engine_reports_itself_available():
    """
    `doctor` must not describe a working engine as missing. struct and zlib are
    stdlib, so there is nothing here that can be absent.
    """
    ok, reason = png_engine.PngEngine().available()
    assert ok, reason


def test_the_keeplist_is_exactly_section_2_2():
    """
    Both directions matter. A name removed from KEEP_CHUNKS destroys image data
    or rendering information; a name added to it is a carrier nobody checks
    again. Trap 11's rule, applied to the engine's own table.
    """
    assert set(png_engine.KEEP_CHUNKS) == SECTION_2_2_KEEP, (
        "the engine's keep-list and section 2.2 disagree; "
        f"engine only: {sorted(set(png_engine.KEEP_CHUNKS) - SECTION_2_2_KEEP)}, "
        f"section only: {sorted(SECTION_2_2_KEEP - set(png_engine.KEEP_CHUNKS))}"
    )


def test_the_png_row_is_still_routed_to_exiftool():
    """
    Phase 1 builds the engine; it does not flip the default. A CAPABILITIES row
    pointing here would ship an untested default change inside an engine commit.
    """
    from metascrub.capabilities import CAPABILITIES, Engine

    assert CAPABILITIES[".png"].engine is Engine.EXIFTOOL


# ---------------------------------------------------------------- the gate


def test_structural_assertion_on_a_loaded_png(mobile_fixture):
    """
    The Part 3.2 gate on the Android-screenshot-shaped fixture: tEXt chunks,
    an XMP iTXt and a tIME, all of which must be gone, and an IDAT that must
    not be.
    """
    path, text_value = mobile_fixture(".png")

    before = {rec["type"] for rec in walk(read(path))}
    assert b"tEXt" in before and b"iTXt" in before, (
        f"the fixture is not carrying what this test is about: {sorted(before)}"
    )
    assert raw_contains(path, text_value), "fixture lost its sentinel"

    targeted = scrub(path)

    assert_structurally_clean(path, "mobile png fixture")
    assert not raw_contains(path, text_value), "the tEXt sentinel is still in the bytes"
    assert any("tEXt" in line for line in targeted), targeted


def test_pixels_are_byte_identical_because_idat_is_never_re_encoded(tmp_path):
    """
    The engine deletes whole chunks and copies the survivors verbatim, so the
    compressed image data is never decoded, never re-encoded, and cannot move.
    Asserted on the DECODED pixels, not on the IDAT bytes, because identical
    IDAT is the mechanism and identical pixels is the promise.
    """
    path = plain_png(tmp_path)
    payload = sentinel("pngpixels")
    write(path, insert_after_ihdr(read(path), [
        make_chunk(b"tEXt", b"Comment\x00" + payload.encode()),
        make_chunk(b"prVt", payload.encode()),
    ]))

    before_mode, before_size, before_pixels = pixels(path)
    before_idat = b"".join(r["payload"] for r in walk(read(path)) if r["type"] == b"IDAT")

    scrub(path)

    after_mode, after_size, after_pixels = pixels(path)
    after_idat = b"".join(r["payload"] for r in walk(read(path)) if r["type"] == b"IDAT")

    assert (after_mode, after_size) == (before_mode, before_size)
    assert after_pixels == before_pixels, "the decoded pixels changed"
    assert after_idat == before_idat, "IDAT was rewritten; something re-encoded"


def test_every_surviving_chunk_has_a_valid_crc(tmp_path):
    """
    A rebuilt container whose CRCs no longer match is a corrupt file that
    happens to still open in a forgiving decoder. Checked with this file's own
    CRC computation over the bytes on disk.
    """
    path = plain_png(tmp_path)
    write(path, insert_after_ihdr(read(path), [
        make_chunk(b"tEXt", b"Author\x00" + sentinel("pngcrc").encode()),
        make_chunk(b"tIME", struct.pack(">HBBBBB", 2026, 9, 6, 14, 30, 22)),
        make_chunk(b"gAMA", struct.pack(">I", 45455)),
    ]))

    scrub(path)

    records = [r for r in walk(read(path)) if r["type"] != b"<TRAILER>"]
    bad = [r["type"] for r in records if not r["crc_ok"]]
    assert not bad, f"chunks with a broken CRC: {bad}"
    assert len(records) >= 4, f"only {len(records)} chunks survived: {records}"


def test_a_second_scrub_changes_nothing(tmp_path):
    """Idempotence. A clean file must be a fixed point, not a slow rewrite."""
    path = plain_png(tmp_path)
    write(path, insert_after_ihdr(read(path), [
        make_chunk(b"tEXt", b"Comment\x00" + sentinel("pngidem").encode()),
    ]))
    scrub(path)
    once = read(path)
    scrub(path)
    assert read(path) == once


# ---------------------------------------------------------------- zTXt: the measured blind spot


def test_ztxt_defeats_the_byte_search_so_the_structure_is_the_gate(tmp_path):
    """
    The clearest demonstration in this project of why structural assertion is
    the gate and the byte search only the belt.

    A zTXt payload is raw DEFLATE. The sentinel inside one is NOT in the file's
    bytes, so `raw_contains` returns False on the ORIGINAL, before anything has
    been removed. An engine that removed nothing at all would pass a needle
    search on this fixture. Only the chunk inventory can tell the difference.

    exiftool is used here to prove the chunk is a REAL carrier and not just a
    blob this test invented: a mature independent reader inflates it and hands
    the sentinel back.
    """
    path = plain_png(tmp_path)
    value = sentinel("pngztxt")
    # exiftool refuses to compress a short value ("uncompressed data is
    # smaller"), so the payload is padded past the point where DEFLATE wins.
    text = (value + " " + "compressme " * 400).encode()
    payload = b"Software\x00\x00" + zlib.compress(text, 9)
    write(path, insert_after_ihdr(read(path), [make_chunk(b"zTXt", payload)]))

    # 1. The chunk is really there, and it really carries the value.
    assert b"zTXt" in {r["type"] for r in walk(read(path))}
    exiftool_or_fail()
    assert value in str(oracle_metadata(path).get("PNG:Software", "")), (
        "exiftool did not read the sentinel out of the zTXt, so this fixture "
        "is not the carrier this test claims it is"
    )

    # 2. And the byte search cannot see it, on the UNSCRUBBED file. This is the
    #    assertion that makes the point: the needle was never findable.
    assert not raw_contains(path, value), (
        "the zTXt payload was not actually compressed; this fixture no longer "
        "demonstrates the blind spot it exists for"
    )

    # 3. So the removal is proved structurally.
    scrub(path)
    assert b"zTXt" not in {r["type"] for r in walk(read(path))}
    assert_structurally_clean(path, "zTXt fixture")
    assert "PNG:Software" not in oracle_tags(path)


def test_a_compressed_itxt_lands_in_the_same_blind_spot(tmp_path):
    """
    iTXt has a per-chunk compression flag, so an XMP packet from another
    producer can arrive compressed and be equally invisible to a byte search.
    Same gate, same proof.
    """
    path = plain_png(tmp_path)
    value = sentinel("pngitxt")
    text = (value + " " + "xmppadding " * 400).encode()
    # keyword \0 compflag(1) compmethod(0) lang \0 translated \0 compressed text
    payload = b"XML:com.adobe.xmp\x00\x01\x00\x00\x00" + zlib.compress(text, 9)
    write(path, insert_after_ihdr(read(path), [make_chunk(b"iTXt", payload)]))

    assert not raw_contains(path, value), "the iTXt payload was not compressed"
    scrub(path)
    assert b"iTXt" not in {r["type"] for r in walk(read(path))}
    assert_structurally_clean(path, "compressed iTXt fixture")


# ---------------------------------------------------------------- the carriers 2.2 names


@pytest.mark.parametrize("ctype,payload_suffix", [
    (b"tEXt", b"Author\x00"),
    (b"eXIf", b"MM\x00*\x00\x00\x00\x08"),
    (b"dSIG", b""),
    (b"caBX", b""),
    (b"iCCP", b"ICCProfile\x00\x00"),
    (b"sPLT", b"name\x00\x08"),
])
def test_every_named_carrier_is_removed(tmp_path, ctype, payload_suffix):
    """
    Section 2.2's named carriers, one per case, each stamped with a sentinel so
    that the byte search can corroborate the structural result where the
    payload is not compressed.
    """
    path = plain_png(tmp_path, name=f"c{ctype.decode()}.png")
    value = sentinel("png" + ctype.decode().lower())
    write(path, insert_after_ihdr(read(path),
                                  [make_chunk(ctype, payload_suffix + value.encode())]))
    assert raw_contains(path, value), "fixture did not store the sentinel"

    scrub(path)

    assert ctype not in {r["type"] for r in walk(read(path))}
    assert not raw_contains(path, value)
    assert_structurally_clean(path, ctype.decode())


def test_tIME_is_removed(tmp_path):
    """tIME is a wall clock, and a wall clock is not byte-searchable."""
    path = plain_png(tmp_path)
    write(path, insert_after_ihdr(read(path), [
        make_chunk(b"tIME", struct.pack(">HBBBBB", 2026, 3, 14, 9, 26, 53)),
    ]))
    assert "PNG:ModifyDate" in oracle_metadata(path), "fixture carries no tIME"

    scrub(path)

    assert b"tIME" not in {r["type"] for r in walk(read(path))}
    assert "PNG:ModifyDate" not in oracle_tags(path)


def test_an_unknown_ancillary_chunk_is_actually_removed(tmp_path):
    """
    The case that was a false CLEAN in the shipped tool.

    1.0.1 reported `SANITIZED ... verified clean` on a file with an unknown
    chunk still in it, because the residual scan only ever looked for values a
    baseline read had produced and exiftool never reported the chunk at all.
    1.0.2's structural scan made that visible. Visible is not removed: this
    engine has to actually take it out.
    """
    path = plain_png(tmp_path)
    value = sentinel("pngunknown")
    write(path, insert_after_ihdr(read(path), [
        make_chunk(b"prVt", b"private\x00" + value.encode()),
    ]))
    assert raw_contains(path, value)
    # The oracle is blind to it, which is the whole reason the structural
    # assertion has to exist. Recorded here rather than assumed.
    assert not any("prVt" in tag for tag in oracle_tags(path))

    targeted = scrub(path)

    assert b"prVt" not in {r["type"] for r in walk(read(path))}
    assert not raw_contains(path, value)
    assert any("unknown ancillary chunk" in line for line in targeted), targeted
    assert_structurally_clean(path, "unknown ancillary chunk")


def test_an_8kb_payload_hidden_in_an_unknown_chunk_is_removed(tmp_path):
    """
    The WebP MPVD defect, ported to PNG: a whole file hidden inside a chunk the
    reader does not know. Size matters here only because a big payload is what
    a real smuggled video looks like.
    """
    path = plain_png(tmp_path)
    value = sentinel("pngsmuggled")
    hidden = value.encode() + b"\x00\x00\x00\x18ftypmp42" + os.urandom(8000)
    before = os.path.getsize(path)
    write(path, insert_after_ihdr(read(path), [make_chunk(b"smUg", hidden)]))
    assert os.path.getsize(path) > before + 8000

    scrub(path)

    assert not raw_contains(path, value)
    assert b"ftypmp42" not in read(path)
    assert_structurally_clean(path, "smuggled payload")


def test_bytes_after_iend_are_removed(tmp_path):
    """
    Section 2.2's trailing-data rule. Measured: Pillow opens such a file without
    complaint and exiftool emits only a [minor] warning, so nothing in the
    normal read path notices.
    """
    path = plain_png(tmp_path)
    value = sentinel("pngtrailer")
    original = read(path)
    write(path, original + value.encode() + os.urandom(4096))
    assert raw_contains(path, value)

    targeted = scrub(path)

    assert read(path) == original, "the output is not the original PNG plus nothing"
    assert not raw_contains(path, value)
    assert any("trailing data after IEND" in line for line in targeted), targeted
    assert_structurally_clean(path, "trailer")


# ---------------------------------------------------------------- failing closed


def test_an_unknown_critical_chunk_is_refused_rather_than_guessed_at(tmp_path):
    """
    EXPLICIT DECISION, and the one place this engine refuses a file instead of
    cleaning it.

    Section 2.2 says to remove any unknown ANCILLARY chunk. An unknown CRITICAL
    chunk is a different thing: the PNG specification says a decoder that does
    not recognise one must not proceed, so it may be image data. Deleting it
    could destroy the picture and keeping it could keep a carrier. Neither is
    defensible silently, so the file is refused and the original is left alone.
    """
    path = plain_png(tmp_path)
    write(path, insert_after_ihdr(read(path), [make_chunk(b"CRIT", b"payload")]))
    before = read(path)

    with pytest.raises(EngineError) as excinfo:
        scrub(path)

    assert "CRITICAL" in str(excinfo.value)
    assert read(path) == before, "a refused file must be left untouched"


def test_a_bad_crc_is_refused_rather_than_silently_repaired(tmp_path):
    """
    Recomputing the CRC of a damaged chunk would turn a corrupt file into a
    plausible one and then call it sanitized. That is the shape of every failure
    this project exists to refuse.
    """
    path = plain_png(tmp_path)
    data = bytearray(read(path))
    # Corrupt the IDAT payload so its stored CRC no longer matches.
    for rec in walk(bytes(data)):
        if rec["type"] == b"IDAT":
            data[rec["offset"] + 8] ^= 0xFF
            break
    write(path, bytes(data))
    before = read(path)

    with pytest.raises(EngineError) as excinfo:
        scrub(path)

    assert "bad CRC" in str(excinfo.value)
    assert read(path) == before


@pytest.mark.parametrize("name,payload", [
    ("not a png", b"GIF89a" + b"\x00" * 64),
    ("truncated mid chunk", None),
    ("no IEND", None),
])
def test_malformed_input_is_refused(tmp_path, name, payload):
    """Fail closed on anything this engine cannot account for byte by byte."""
    path = plain_png(tmp_path, name="malformed.png")
    good = read(path)
    if payload is None and name == "truncated mid chunk":
        payload = good[:len(good) - 20]
    elif payload is None:
        payload = b"".join(
            good[r["offset"]:r["offset"] + 12 + r["length"]]
            for r in walk(good) if r["type"] != b"IEND"
        )
        payload = SIG + payload
    write(path, payload)

    with pytest.raises(EngineError):
        scrub(path)


# ---------------------------------------------------------------- the oracle


def test_the_oracle_sees_nothing_left_in_a_scrubbed_screenshot(mobile_fixture):
    """
    Part 3.3: the engine's output is handed to a mature, independent,
    25-year-old implementation, and it must find nothing outside the IHDR
    fields. Necessary and not sufficient: it is paired with the structural
    assertion above, never used alone.
    """
    path, _ = mobile_fixture(".png")
    scrub(path)
    assert_oracle_sees_nothing(
        path, allow=ORACLE_ALLOW_PNG,
        note="pure-Python png engine, mobile screenshot fixture",
    )
    assert_structurally_clean(path, "oracle fixture")


def test_the_oracle_sees_only_keeplist_tags_on_a_keeplist_png(tmp_path):
    """
    A correctly scrubbed PNG is not tag-free: the chunks section 2.2 KEEPS are
    reported by exiftool, which is why ORACLE_ALLOW_PNG_KEEPLIST exists.

    cHRM and sBIT are deliberately NOT in this fixture. conftest records that
    PNG:WhitePointX / PNG:WhitePointY / PNG:SignificantBits were never measured
    there, and widening someone else's allowlist on the strength of my own
    convenience is trap 11 exactly. Those two chunks are covered structurally
    by test_the_keeplist_chunks_all_survive instead.

    MEASURED 2026-09-06, exiftool 13.29, this engine's output, and handed to
    whoever owns conftest rather than acted on here: a kept cHRM reports EIGHT
    tags, not the two Part 3.3 names. PNG:WhitePointX, PNG:WhitePointY,
    PNG:RedX, PNG:RedY, PNG:GreenX, PNG:GreenY, PNG:BlueX, PNG:BlueY. A kept
    sBIT reports PNG:SignificantBits. So ORACLE_ALLOW_PNG_KEEPLIST needs nine
    more names before an oracle assertion can be made over a full keep-list
    PNG, and each of them is a name nobody checks again afterwards.
    """
    path = plain_png(tmp_path)
    value = sentinel("pngkeeplist")
    write(path, insert_after_ihdr(read(path), [
        make_chunk(b"gAMA", struct.pack(">I", 45455)),
        make_chunk(b"sRGB", b"\x00"),
        make_chunk(b"pHYs", struct.pack(">IIB", 5669, 5669, 1)),
        make_chunk(b"tEXt", b"Author\x00" + value.encode()),
    ]))

    scrub(path)

    assert_oracle_sees_nothing(
        path, allow=ORACLE_ALLOW_PNG_KEEPLIST,
        note="pure-Python png engine, section 2.2 keep-list fixture",
    )
    surviving = oracle_tags(path)
    assert "PNG:Gamma" in surviving, (
        "the fixture no longer proves the allowlist is needed; if gAMA is gone, "
        "this test would pass against an engine that removed the whole keep-list"
    )
    assert not raw_contains(path, value)


# ---------------------------------------------------------------- overcorrection: the keep-list


def test_the_keeplist_chunks_all_survive(tmp_path):
    """
    OVERCORRECTION TEST for the removal rule. "Remove everything else" is one
    edit away from "remove everything", and that edit would still pass every
    removal test in this file.
    """
    path = plain_png(tmp_path)
    added = [
        (b"gAMA", struct.pack(">I", 45455)),
        (b"cHRM", struct.pack(">8I", 31270, 32900, 64000, 33000,
                              30000, 60000, 15000, 6000)),
        (b"sRGB", b"\x00"),
        (b"sBIT", b"\x08\x08\x08"),
        (b"bKGD", struct.pack(">3H", 0, 0, 0)),
        (b"pHYs", struct.pack(">IIB", 5669, 5669, 1)),
    ]
    write(path, insert_after_ihdr(read(path),
                                  [make_chunk(t, p) for t, p in added]
                                  + [make_chunk(b"tEXt", b"Comment\x00gone")]))

    scrub(path)

    survivors = {r["type"] for r in walk(read(path))}
    missing = sorted(t.decode() for t, _ in added if t not in survivors)
    assert not missing, f"the keep-list chunks were removed: {missing}"
    assert b"tEXt" not in survivors, "the fixture proves nothing if nothing was removed"

    from PIL import Image
    with Image.open(path) as im:
        im.load()
        assert im.info.get("gamma") is not None, "gAMA no longer reaches a decoder"
        assert im.info.get("dpi") is not None, "pHYs no longer reaches a decoder"
        assert "srgb" in im.info, "sRGB no longer reaches a decoder"


def test_an_apng_still_animates(tmp_path):
    """
    OVERCORRECTION TEST. acTL, fcTL and fdAT are in the keep-list because
    removing them turns an animation into a still image while every removal
    test in this file keeps passing.
    """
    from PIL import Image

    path = str(tmp_path / "anim.png")
    frames = [Image.new("RGB", (24, 24), c) for c in ((200, 10, 10), (10, 200, 10), (10, 10, 200))]
    frames[0].save(path, save_all=True, append_images=frames[1:], duration=80, loop=0)
    write(path, insert_after_ihdr(read(path), [
        make_chunk(b"tEXt", b"Software\x00" + sentinel("pngapng").encode()),
    ]))

    with Image.open(path) as im:
        before_frames = im.n_frames
        before = []
        for index in range(before_frames):
            im.seek(index)
            before.append(im.convert("RGBA").tobytes())
    assert before_frames == 3

    scrub(path)

    survivors = {r["type"] for r in walk(read(path))}
    for required in (b"acTL", b"fcTL", b"fdAT"):
        assert required in survivors, f"{required.decode()} was removed; the APNG is now a still"
    assert b"tEXt" not in survivors

    with Image.open(path) as im:
        assert im.is_animated
        assert im.n_frames == before_frames
        after = []
        for index in range(im.n_frames):
            im.seek(index)
            after.append(im.convert("RGBA").tobytes())
    assert after == before, "the animation's pixels changed"


def test_a_palette_png_with_trns_still_renders(tmp_path):
    """
    OVERCORRECTION TEST. PLTE is critical and tRNS carries the per-entry alpha.
    Dropping tRNS makes every transparent pixel opaque, which no removal test
    would notice.
    """
    from PIL import Image

    path = str(tmp_path / "pal.png")
    image = Image.new("P", (24, 24), 3)
    image.putpalette([0, 0, 0, 255, 0, 0, 0, 255, 0, 0, 0, 255] * 64)
    for x in range(12):
        for y in range(12):
            image.putpixel((x, y), 1)
    image.save(path, transparency=3)
    write(path, insert_after_ihdr(read(path), [
        make_chunk(b"tEXt", b"Comment\x00" + sentinel("pngpal").encode()),
    ]))

    before = pixels(path)

    scrub(path)

    survivors = {r["type"] for r in walk(read(path))}
    assert b"PLTE" in survivors, "the palette was removed; the image is undecodable"
    assert b"tRNS" in survivors, "tRNS was removed; transparent pixels are now opaque"
    assert b"tEXt" not in survivors
    assert pixels(path) == before, "the rendered image changed"
    with Image.open(path) as im:
        assert im.info.get("transparency") is not None


# ---------------------------------------------------------------- the mutation check


def test_mutation_a_widened_keeplist_is_caught_by_the_structural_assertion(
        tmp_path, monkeypatch):
    """
    THE MUTATION CHECK, made permanent instead of run once by hand.

    The removal is broken deliberately, in the smallest way a real edit would
    break it: two names are added to the engine's keep-list, so tEXt and an
    unknown chunk survive. The point is not that the engine then leaks; it is
    that THIS FILE NOTICES. A test suite that stays green under a deliberately
    broken engine is measuring nothing.

    It asserts on the structural check specifically, because that is the gate
    for PNG and because for a compressed carrier it is the only instrument that
    would fire at all.
    """
    path = plain_png(tmp_path)
    write(path, insert_after_ihdr(read(path), [
        make_chunk(b"tEXt", b"Author\x00" + sentinel("pngmutation").encode()),
        make_chunk(b"prVt", b"private"),
    ]))

    monkeypatch.setattr(
        png_engine, "KEEP_CHUNKS",
        png_engine.KEEP_CHUNKS | {b"tEXt", b"prVt"},
    )
    scrub(path)

    survivors = {r["type"] for r in walk(read(path))}
    assert b"tEXt" in survivors and b"prVt" in survivors, (
        "the mutation did not take effect, so this test proves nothing"
    )

    with pytest.raises(AssertionError) as excinfo:
        assert_structurally_clean(path, "mutated engine")
    assert "keep-list" in str(excinfo.value)


def test_mutation_a_surviving_trailer_is_caught_by_the_structural_assertion(tmp_path):
    """
    The second half of the mutation check: the trailer rule. Rather than
    monkeypatching the loop, the broken OUTPUT is constructed directly, which
    is what an engine that forgot to stop at IEND would have written.
    """
    path = plain_png(tmp_path)
    scrub(path)
    write(path, read(path) + sentinel("pngmutationtrailer").encode() + os.urandom(512))

    with pytest.raises(AssertionError) as excinfo:
        assert_structurally_clean(path, "engine that kept the trailer")
    assert "after IEND" in str(excinfo.value)
