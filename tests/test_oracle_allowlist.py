"""
The oracle allowlists in conftest.py, and specifically the nine names
ORACLE_ALLOW_PNG_KEEPLIST gained on 2026-09-06.

WHY THIS FILE EXISTS AND WHY IT IS NOT IN test_png_engine.py

conftest.py holds every ORACLE_ALLOW_* list, and every name in one of them is a
name that is never checked again in any file that passes that list. That is
trap 11 in its purest form, and trap 11's rule is that an exclusion and its
overcorrection test land together. The exclusion lives in conftest, which is not
a module pytest collects tests from, and test_png_engine.py belongs to another
team this session. So the test that guards the widening gets its own file rather
than being left unwritten, and it is named for the thing it guards rather than
for the format that happened to need it first.

WHAT WAS WIDENED, AND WHAT WAS MEASURED BEFORE IT WAS

conftest previously recorded PNG:WhitePointX, PNG:WhitePointY and
PNG:SignificantBits as NOT MEASURED, on the grounds that exiftool 13.29 would
not write cHRM or sBIT on request, so no fixture had ever carried them. That is
true of exiftool's WRITER and it is not a limit on the format: a cHRM and an
sBIT chunk can be built by hand, and exiftool reads what it is handed.

MEASURED 2026-09-06 on this machine, exiftool 13.29, Pillow 12.1.1, on the
fixture below and re-measured rather than inherited from the PNG team's note:

  cHRM 31270 32900 64000 33000 30000 60000 15000 6000 reports EIGHT tags, not
  the two docs/ANDROID-MEDIA-BUILD.md section 3.3 names:
    PNG:WhitePointX 0.3127  PNG:WhitePointY 0.329
    PNG:RedX 0.64           PNG:RedY 0.33
    PNG:GreenX 0.3          PNG:GreenY 0.6
    PNG:BlueX 0.15          PNG:BlueY 0.06
  sBIT 08 08 08 reports one: PNG:SignificantBits '8 8 8'.

Nine names. Section 3.3 is wrong about the count and the measurement is what
conftest now encodes.

THE TWO DIRECTIONS THIS FILE CHECKS

  1. NO NAME IS ALLOWED THAT NOTHING MEASURED. The widened list, minus the
     seven IHDR fields, must be EXACTLY the set of tags a PNG carrying the
     section 2.2 keep-list chunks actually reports. Not a superset: a name
     nobody could produce is a name nobody could have measured, and it would sit
     there permanently blinding the oracle to whatever else ever reports under
     it.
  2. THE WIDENING DID NOT BLIND THE ORACLE. Real identity tags re-injected into
     the same fixture must still make assert_oracle_sees_nothing FAIL. Both
     halves matter: a widened list that stopped catching an Artist string would
     be worse than no oracle at all, because it would report green.
"""

from __future__ import annotations

import struct
import zlib

import pytest
from PIL import Image

from conftest import (
    ORACLE_ALLOW_ISOBMFF_REMUX,
    ORACLE_ALLOW_JPEG,
    ORACLE_ALLOW_PNG,
    ORACLE_ALLOW_PNG_KEEPLIST,
    ORACLE_ALLOW_WEBP,
    _exiftool_write,
    assert_oracle_sees_nothing,
    oracle_tags,
    sentinel,
)

SIGNATURE = b"\x89PNG\r\n\x1a\n"

# The nine, written out here as literals rather than derived from conftest.
# A test that computed the expected set from the set under test would agree
# with any value it was given.
FROM_CHRM = (
    "PNG:WhitePointX", "PNG:WhitePointY",
    "PNG:RedX", "PNG:RedY",
    "PNG:GreenX", "PNG:GreenY",
    "PNG:BlueX", "PNG:BlueY",
)
FROM_SBIT = ("PNG:SignificantBits",)
WIDENED = frozenset(FROM_CHRM + FROM_SBIT)

# The five that were already there, so that the delta below is an exact set
# rather than a subset relation.
ALREADY_THERE = frozenset({
    "PNG:Gamma",           # gAMA
    "PNG:PixelsPerUnitX",  # pHYs
    "PNG:PixelsPerUnitY",  # pHYs
    "PNG:PixelUnits",      # pHYs
    "PNG:SRGBRendering",   # sRGB
})


def chunk(ctype: bytes, payload: bytes) -> bytes:
    """One PNG chunk, CRC included. Written from the format, not imported."""
    return (struct.pack(">I", len(payload)) + ctype + payload
            + struct.pack(">I", zlib.crc32(ctype + payload) & 0xFFFFFFFF))


def after_ihdr(data: bytes, chunks) -> bytes:
    at = len(SIGNATURE)
    (length,) = struct.unpack(">I", data[at:at + 4])
    end = at + 12 + length
    return data[:end] + b"".join(chunks) + data[end:]


def keeplist_png(tmp_path, name: str = "keeplist.png") -> str:
    """
    A PNG carrying the section 2.2 keep-list chunks that exiftool reports.

    bKGD and tRNS are deliberately absent: bKGD reports PNG:BackgroundColor,
    which is NOT on the allowlist, and putting it in this fixture would make
    the exact-delta assertion below fail for a reason that has nothing to do
    with the nine names. Naming that here rather than letting a future reader
    wonder why the fixture is not simply "every keep-list chunk".
    """
    path = str(tmp_path / name)
    Image.new("RGB", (24, 16), (10, 20, 30)).save(path, format="PNG")
    with open(path, "rb") as handle:
        data = handle.read()
    data = after_ihdr(data, [
        chunk(b"gAMA", struct.pack(">I", 45455)),
        chunk(b"cHRM", struct.pack(">8I", 31270, 32900, 64000, 33000,
                                  30000, 60000, 15000, 6000)),
        chunk(b"sBIT", b"\x08\x08\x08"),
        chunk(b"sRGB", b"\x00"),
        chunk(b"pHYs", struct.pack(">IIB", 5669, 5669, 1)),
    ])
    with open(path, "wb") as handle:
        handle.write(data)
    # The fixture has to be a real PNG, not merely a plausible byte string.
    image = Image.open(path)
    image.load()
    assert image.size == (24, 16)
    return path


# --------------------------------------------------- 1. nothing unmeasured got in


def test_the_fixture_really_carries_the_chunks_the_measurement_needed(tmp_path):
    """
    The instrument first. If cHRM or sBIT is not in the file, every assertion
    below is about a PNG that does not have them and proves nothing.
    """
    path = keeplist_png(tmp_path)
    with open(path, "rb") as handle:
        data = handle.read()
    for ctype in (b"gAMA", b"cHRM", b"sBIT", b"sRGB", b"pHYs"):
        assert ctype in data, f"the fixture does not carry {ctype!r}"


def test_every_widened_name_is_one_this_machine_actually_reported(tmp_path):
    """
    RE-MEASURED, not inherited. The PNG team measured these nine and could not
    add them because conftest was not their file; a name allowlisted on someone
    else's measurement is a name nobody ever checked, so they are measured again
    here, on this exiftool, before being trusted.
    """
    reported = set(oracle_tags(keeplist_png(tmp_path)))
    missing = sorted(WIDENED - reported)
    assert not missing, (
        "these names are on the allowlist and this exiftool does not produce "
        "them, so nothing here has ever checked what they hide: "
        + ", ".join(missing)
    )
    assert WIDENED <= ORACLE_ALLOW_PNG_KEEPLIST, sorted(WIDENED - ORACLE_ALLOW_PNG_KEEPLIST)


def test_the_widened_list_is_exactly_what_a_keeplist_png_reports(tmp_path):
    """
    THE EXACT-DELTA ASSERTION, and the reason this is equality and not a subset.

    A subset check would let a tenth name be added later on nobody's
    measurement, and it would sit in the list forever making the oracle blind to
    whatever else reports under that name. Equality means the list cannot grow
    without a fixture that produces the new name, which is the only cost that
    makes an allowlist honest.

    If this fails after an exiftool upgrade, that is the test working: exiftool
    changing what it calls a field is exactly the rot this guards. Re-measure,
    then edit both this file and conftest, in that order.
    """
    path = keeplist_png(tmp_path)
    reported = set(oracle_tags(path))
    assert ORACLE_ALLOW_PNG <= reported, (
        "the fixture no longer reports the IHDR fields, so the delta below is "
        "not the delta this test means: "
        + repr(sorted(ORACLE_ALLOW_PNG - reported))
    )
    assert ORACLE_ALLOW_PNG_KEEPLIST - ORACLE_ALLOW_PNG == reported - ORACLE_ALLOW_PNG, (
        "the keep-list allowlist and what a keep-list PNG actually reports "
        "have drifted apart.\n  on the allowlist and not measured: "
        + repr(sorted(ORACLE_ALLOW_PNG_KEEPLIST - ORACLE_ALLOW_PNG - reported))
        + "\n  measured and not on the allowlist: "
        + repr(sorted(reported - ORACLE_ALLOW_PNG_KEEPLIST))
    )
    assert ORACLE_ALLOW_PNG_KEEPLIST - ORACLE_ALLOW_PNG == WIDENED | ALREADY_THERE


def test_a_scrubbed_keeplist_png_is_not_what_this_list_is_for():
    """
    The widened list is opt-in and stays opt-in.

    ORACLE_ALLOW_PNG is what an ordinary scrubbed PNG is measured against, and
    it is still empty of all fourteen keep-list names. A list built for one
    situation silently widening to every other one is how a blind spot spreads,
    which is the whole reason assert_oracle_sees_nothing takes its allowlist as
    an argument instead of a default.
    """
    assert not (WIDENED | ALREADY_THERE) & ORACLE_ALLOW_PNG


def test_the_widened_names_did_not_leak_into_another_container(tmp_path):
    """
    Trap 11's second clause: a name allowed for one format must not become a
    name allowed for every format. PNG:BitDepth and QuickTime:BitDepth are
    different fields with the same idea behind them, and the lists are keyed on
    the fully qualified name for exactly that reason.
    """
    others = ORACLE_ALLOW_JPEG | ORACLE_ALLOW_WEBP | ORACLE_ALLOW_ISOBMFF_REMUX
    assert not WIDENED & others, sorted(WIDENED & others)


# ------------------------------------- 2. the overcorrection: the oracle still sees


def _assert_the_oracle_still_fails(path, note):
    """
    assert_oracle_sees_nothing is an assertion, so making it fail is the whole
    measurement. Returning the message means the caller can check the leak was
    named rather than merely that something failed.
    """
    with pytest.raises(AssertionError) as raised:
        assert_oracle_sees_nothing(
            path, allow=ORACLE_ALLOW_PNG_KEEPLIST, note=note)
    return str(raised.value)


# Real carriers, each written by exiftool so the shape is one a real file could
# have. Every value is a sentinel, so "the oracle caught it" is checked against
# the actual string rather than against a tag name.
IDENTITY_TAGS = {
    "PNG:Artist, a person's name in a tEXt chunk": "PNG:Artist",
    "PNG:Software, the producing application": "PNG:Software",
    "PNG:Comment, free text": "PNG:Comment",
    "XMP:Creator, the same identity one container deeper": "XMP:Creator",
}


@pytest.mark.parametrize("label", sorted(IDENTITY_TAGS))
def test_the_widened_allowlist_still_fails_on_a_real_identity_tag(
        tmp_path, label):
    """
    THE OVERCORRECTION TEST for the widening, and the only reason the nine
    names were allowed to land.

    Nine more names is nine more places a leak could hide behind. This puts a
    real identity value into a file that carries all nine and requires the
    oracle to still say so, by value and not merely by count.
    """
    tag = IDENTITY_TAGS[label]
    value = sentinel("oraclewidening")
    path = keeplist_png(tmp_path, "identity.png")
    _exiftool_write(path, **{tag: value})

    reported = set(oracle_tags(path))
    assert WIDENED <= reported, (
        "%s: exiftool's write dropped the cHRM or sBIT chunks, so this file no "
        "longer exercises the widened list at all: missing %r"
        % (label, sorted(WIDENED - reported)))

    message = _assert_the_oracle_still_fails(path, label)
    assert value in message, (
        "%s: the oracle failed but did not name the leaking value, so the "
        "failure could have been about anything: %s" % (label, message))


def test_a_hand_built_text_chunk_is_caught_without_exiftool_writing_it(tmp_path):
    """
    The same overcorrection, with the carrier built here rather than by
    exiftool.

    Every case above depends on exiftool's WRITER to create the leak, and a
    writer that quietly refused would turn all four into tests that pass
    because nothing happened. This one cannot: the tEXt chunk is assembled from
    the PNG format and its bytes are asserted present in the file first.
    """
    value = sentinel("oraclehandbuilt")
    path = keeplist_png(tmp_path, "handbuilt.png")
    with open(path, "rb") as handle:
        data = handle.read()
    data = after_ihdr(data, [chunk(b"tEXt", b"Author\x00" + value.encode())])
    with open(path, "wb") as handle:
        handle.write(data)
    with open(path, "rb") as handle:
        assert value.encode() in handle.read()

    message = _assert_the_oracle_still_fails(path, "hand-built tEXt")
    assert value in message, message


def test_the_oracle_passes_the_same_fixture_with_nothing_added(tmp_path):
    """
    And the other half, which is what makes the four tests above mean
    something: with no identity tag present the widened list DOES pass. A test
    that only ever demanded failure would pass against an allowlist of nothing.
    """
    assert_oracle_sees_nothing(
        keeplist_png(tmp_path), allow=ORACLE_ALLOW_PNG_KEEPLIST,
        note="the nine widened names, with nothing else in the file")
