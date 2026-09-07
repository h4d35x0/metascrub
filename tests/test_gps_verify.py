"""
The GPS structural check: does it see a coordinate the byte scan cannot, and
does it stay silent on a file that is actually clean.

THE ORDERING THIS FILE ENFORCES

The residual byte scan cannot be the gate here, and that is a measurement
rather than a preference. docs/D2-GPS-VERIFICATION.md section 2.1 measured that
an EXIF coordinate is stored as three rational pairs, so the decimal exiftool
prints is not in the file in any form; `test_the_coordinate_is_not_in_the_bytes
_in_any_form` reproduces that measurement here, on this machine, before
anything else is asserted. A byte search for a coordinate returns nothing on
the ORIGINAL file, which means a scrubber that removed nothing would pass it.

So: the STRUCTURAL assertion is the gate, exiftool is the independent third
party, and the byte search is used only to prove that it cannot be the
instrument. This is the same ordering `test_png_engine.py` adopted for zTXt and
for the same reason.

TWO INDEPENDENT PARSERS, ON PURPOSE

The fixtures below are built with Pillow, ffmpeg and exiftool, and the oracle
that says whether a file carries GPS is exiftool, never `gps_verify` itself. A
checker that shares a parser with the thing it checks cannot catch a parser
bug; that is the precedent `test_av_muxer.read_brand` and `test_png_engine.walk`
both set. The one place this file builds a container by hand
(`isobmff()` below) is a WRITER, not a reader, and it is written from the ISO
base media specification rather than from `gps_verify._isobmff_boxes`.

WHAT IS DELIBERATELY NOT ASSERTED HERE

That `verify.verify()` reports a different VERDICT. It does not, and that is
Option B in docs/WHAT-THE-TOOL-CLAIMS.md working as designed: as of 2026-09-07
`verify.py` imports this module and carries its answer in the additive
`Verification.coverage` field, while `Verdict` is untouched. A file whose GPS
check reported NOT_CHECKED still reads `verified_clean`, and now says so with
its limits attached. Whether the verdict value itself should change is the
still-open half of the old deferral;
`test_an_uncovered_format_can_never_read_as_checked` writes out both halves and
names the test that now collects the open one. The rest of the coverage wiring
is measured in `tests/test_coverage_reporting.py`, not here.

That a raw file's GPS is caught. No raw fixture exists on this machine, so no
raw extension has a walker, and
`test_the_covered_extensions_are_exactly_the_measured_ones` fails if one is
added without one.
"""

from __future__ import annotations

import os
import shutil
import struct

import pytest

from conftest import (
    HAVE_FFMPEG,
    build,
    exiftool_or_fail,
    oracle_metadata,
    sentinel,
)
from metascrub import gps_verify, verify
from metascrub.capabilities import CAPABILITIES, Completeness
from metascrub.gps_verify import CARRIER_EXIF_GPS_IFD, GpsReport, GpsStatus
from metascrub.scrubber import MetadataScrubber

# One coordinate, used everywhere so that a failure names a number that can be
# looked up in the design document. Toronto, from section 1.1.
LAT = "43.653226"
LON = "-79.383184"


# ---------------------------------------------------------------- fixture tools


def write_tags(path, **tags):
    """
    Stamp tags through the project's own exiftool session.

    Routed through the session rather than a subprocess for the reason
    conftest._exiftool_write gives: a direct subprocess hands the path over in
    the ANSI codepage on Windows and exiftool cannot open a non-ASCII one. The
    session also supplies `-n`, so every value written here must be in
    exiftool's NUMERIC form.
    """
    session = exiftool_or_fail()
    session.execute(*[f"-{key}={value}" for key, value in tags.items()],
                    "-overwrite_original", path)


def gps_tags(lat=LAT, lon=LON):
    return {
        "GPSLatitude": lat, "GPSLatitudeRef": "N",
        "GPSLongitude": lon, "GPSLongitudeRef": "W",
        "GPSAltitude": "76.5", "GPSAltitudeRef": "0",
    }


def assert_oracle_sees_gps(path, note=""):
    """
    The independent third party. A fixture the oracle cannot see GPS in proves
    nothing, and every assertion downstream of it would be vacuous: the same
    species as the 2026-09-04 empty-DEFERRED finding, where a loop over an
    empty container was the quietest possible pass.
    """
    metadata = oracle_metadata(path)
    seen = sorted(key for key in metadata if "GPS" in key.rsplit(":", 1)[-1])
    if not seen:
        pytest.fail(
            f"the fixture {os.path.basename(path)} carries no GPS according to "
            f"exiftool, so it cannot measure anything{' (' + note + ')' if note else ''}"
        )
    return seen


def image(tmp_path, name, fmt, size=(64, 48)):
    from PIL import Image

    path = str(tmp_path / name)
    Image.new("RGB", size, (30, 90, 160)).save(path, format=fmt)
    return path


def geotagged(tmp_path, name, fmt, lat=LAT, lon=LON, **extra):
    """An image of `fmt` carrying a full EXIF GPS fix and an Artist sentinel."""
    path = image(tmp_path, name, fmt)
    value = sentinel("gps" + fmt.lower())
    write_tags(path, Artist=value, **gps_tags(lat, lon), **extra)
    assert_oracle_sees_gps(path, fmt)
    return path, value


def read(path):
    with open(path, "rb") as handle:
        return handle.read()


def write(path, data):
    with open(path, "wb") as handle:
        handle.write(data)
    return path


def box(btype, payload=b""):
    """One ISO base media box. A WRITER, written from ISO/IEC 14496-12."""
    return struct.pack(">I", len(payload) + 8) + btype + payload


def isobmff(*moov_children, mdat=b"\x00" * 64):
    """
    A minimal, structurally valid ISO base media file.

    Not playable and not meant to be: every assertion in this file is about the
    box tree, and building the tree by hand is what lets a box be placed
    somewhere a real muxer would never put it.
    """
    return (box(b"ftyp", b"isom" + struct.pack(">I", 512) + b"isomiso2mp41")
            + box(b"moov", b"".join(moov_children))
            + box(b"mdat", mdat))


def xyz_atom(lat=LAT, lon=LON):
    """A QuickTime (c)xyz atom holding an ISO 6709 string, as a Pixel writes it."""
    text = f"+{float(lat):.6f}{float(lon):+011.6f}+076.500/".encode("ascii")
    return box(b"\xa9xyz", struct.pack(">HH", len(text), 0x15C7) + text)


def scrubbed(path):
    """Run the shipping tool over `path` in place and return its result dict."""
    with MetadataScrubber(backup=False) as scrubber:
        return scrubber.sanitize_file(path)


# ---------------------------------------------------------------- the premise


def test_the_coordinate_is_not_in_the_bytes_in_any_form(tmp_path):
    """
    Section 2.1, reproduced here rather than cited.

    Eleven candidate string forms of the same coordinate, in three encodings,
    against a JPEG that plainly carries it. None of them is in the file, which
    is why no needle derived from a printed value can ever work and why the
    structural check below is the only instrument there is.

    If this test ever FAILS, that is not a regression in this module: it means
    a container started storing the coordinate as text, and the byte scan
    became able to see something it could not see before. Read it, do not
    delete it.
    """
    path, _value = geotagged(tmp_path, "gps.jpg", "JPEG")
    blob = read(path)

    forms = [
        LAT, LAT.lstrip("-"), f"+{LAT}", "43,39.19356N", "43 39 11.6136",
        "43 deg 39' 11.61\"", "43/1 39/1 14517/1250", "43:39:11.6",
        "43.65", "43.653226N", "+43.653226-079.383184+076.500/",
    ]
    assert len(forms) == 11, "section 2.1 measured eleven forms"

    present = [
        form for form in forms
        for enc in ("utf-8", "utf-16-le", "latin-1")
        if form.encode(enc) in blob
    ]
    assert not present, (
        f"the coordinate IS findable in the JPEG bytes as {present}, which "
        f"contradicts the measurement this whole design rests on"
    )
    # And the structural check sees it anyway. That is the entire point.
    report = gps_verify.scan(path)
    assert report.status is GpsStatus.CARRIER_FOUND, report.describe()


def test_the_residual_scan_still_produces_no_gps_needle(tmp_path):
    """
    Overcorrection test 1, and the anchor for test 2.

    This commit does not touch `meaningful_values()`, and this asserts that the
    D2 defect is still exactly where the design document measured it: on a
    geotagged JPEG the needle set contains the Artist and nothing that is a
    coordinate. A future change that "fixes" GPS by loosening the filter has to
    come through here.
    """
    path, value = geotagged(tmp_path, "gps.jpg", "JPEG")
    metadata = oracle_metadata(path)
    assert any("GPSLatitude" in key for key in metadata), sorted(metadata)

    needles = verify.meaningful_values(metadata)
    assert value in needles, f"the Artist sentinel should be a needle: {sorted(needles)}"
    coordinate_like = sorted(
        needle for needle in needles
        if LAT in needle or LAT.lstrip("-") in needle or LON.lstrip("-") in needle
    )
    assert not coordinate_like, (
        f"meaningful_values() produced coordinate needles {coordinate_like}. "
        f"If that was deliberate, test_every_needle_is_findable_in_its_own_file "
        f"is the one that decides whether they buy any coverage."
    )


def test_every_needle_is_findable_in_its_own_file(tmp_path):
    """
    Overcorrection test 2: a needle count that goes up must buy coverage.

    Section 3 calls option 2 the worst design in the set precisely because it
    raises `checked_values` while detection stays at zero: stringifying a float
    produces the needle '43.653226', which is measurably NOT in an EXIF file,
    so the scan searches for it, finds nothing, and reports clean for the wrong
    reason.

    The guard is the file used as its own haystack. Every needle
    `meaningful_values()` derives from a file MUST be present in that file; a
    needle that cannot match its own source can never match a survivor either.
    Measured 2026-09-06 across the four containers below: 0 needles fail this
    today, so it is a live invariant and not a vacuous one.
    """
    made = [
        geotagged(tmp_path, "gps.jpg", "JPEG")[0],
        geotagged(tmp_path, "gps.tif", "TIFF")[0],
        geotagged(tmp_path, "gps.png", "PNG")[0],
        geotagged(tmp_path, "gps.webp", "WEBP")[0],
    ]
    unmatchable = {}
    for path in made:
        blob = read(path)
        for needle in verify.meaningful_values(oracle_metadata(path)):
            if not any(needle.encode(enc) in blob
                       for enc in ("utf-8", "utf-16-le", "latin-1")):
                unmatchable.setdefault(os.path.basename(path), []).append(needle)
    assert not unmatchable, (
        f"these needles are not present in the file they were derived from, so "
        f"they raise checked_values while being unable to match anything: "
        f"{unmatchable}"
    )


# ---------------------------------------------------------------- detection


@pytest.mark.parametrize("name,fmt", [
    ("gps.jpg", "JPEG"), ("gps.tif", "TIFF"),
    ("gps.png", "PNG"), ("gps.webp", "WEBP"),
])
def test_an_exif_gps_ifd_is_found_in_every_container_that_embeds_tiff(
        tmp_path, name, fmt):
    """
    One walker, four containers. JPEG APP1, a bare TIFF, a PNG eXIf chunk and a
    WebP EXIF chunk all embed the same TIFF header, which is why section 5
    calls this the realistic first increment.
    """
    path, _value = geotagged(tmp_path, name, fmt)
    report = gps_verify.scan(path)
    assert report.status is GpsStatus.CARRIER_FOUND, report.describe()
    assert [item.carrier for item in report.findings] == [CARRIER_EXIF_GPS_IFD]
    assert "0x8825" in report.findings[0].detail


def test_the_gps_ifd_is_found_in_both_byte_orders(tmp_path):
    """
    Overcorrection test 4, the re-encode case, and the reason option 3 was
    rejected.

    Section 3 measured that the same coordinate written by the same exiftool is
    stored big-endian (MM) in a JPEG and little-endian (II) in a Pillow TIFF,
    so a 24-byte slice lifted from one is absent from the other. This asserts
    both halves: that the two files really do disagree about byte order and
    have no 24-byte value block in common, and that the structural check finds
    the GPS IFD in both regardless.
    """
    jpeg, _ = geotagged(tmp_path, "order.jpg", "JPEG")
    tiff, _ = geotagged(tmp_path, "order.tif", "TIFF")

    jpeg_bytes, tiff_bytes = read(jpeg), read(tiff)
    exif_at = jpeg_bytes.find(b"Exif\x00\x00")
    assert exif_at != -1, "the JPEG lost its APP1 EXIF block"
    assert jpeg_bytes[exif_at + 6:exif_at + 8] == b"MM", "the JPEG EXIF is not big-endian"
    assert tiff_bytes[0:2] == b"II", "the TIFF is not little-endian"

    # The stored latitude, big-endian: 43/1, 39/1, 14517/1250.
    mm_slice = struct.pack(">6I", 43, 1, 39, 1, 14517, 1250)
    assert mm_slice in jpeg_bytes, "the JPEG does not hold the expected rationals"
    assert mm_slice not in tiff_bytes, (
        "the II TIFF contains the MM slice, so this test is not measuring what "
        "it claims to measure"
    )

    for path in (jpeg, tiff):
        report = gps_verify.scan(path)
        assert report.status is GpsStatus.CARRIER_FOUND, (path, report.describe())


@pytest.mark.parametrize("lat,lon,label", [
    ("0", "0", "Null Island"),
    ("43", "-79", "whole degrees"),
])
def test_a_boring_coordinate_is_found_and_does_not_survive_a_scrub(
        tmp_path, lat, lon, label):
    """
    Overcorrection test 3, the boring-coordinate fixture.

    Section 3 measured the 24-byte Null Island latitude slice (`0/1 0/1 0/1`)
    occurring once in a 3.4 MB DLL and `1/1` occurring 257 times, so any design
    that matches on the VALUE bytes false-positives on a photo taken at 0,0 or
    at a whole degree. This module matches on the POINTER, so the coordinate's
    entropy is irrelevant, and both halves are asserted: found before, and the
    correctly scrubbed output does not report a carrier.
    """
    path, _value = geotagged(tmp_path, f"boring{lat}.jpg", "JPEG", lat=lat, lon=lon)
    if lat == "0":
        assert struct.pack(">6I", 0, 1, 0, 1, 0, 1) in read(path), (
            "the Null Island fixture does not hold the low-entropy slice the "
            "design document measured colliding with ordinary binary"
        )

    before = gps_verify.scan(path)
    assert before.status is GpsStatus.CARRIER_FOUND, (label, before.describe())

    result = scrubbed(path)
    assert result["status"] == "sanitized", result
    after = gps_verify.scan(path)
    assert after.status is GpsStatus.CLEAN, (label, after.describe())
    assert not [key for key in oracle_metadata(path) if "GPS" in key], (
        "the oracle still sees GPS after a scrub the tool called sanitized"
    )


def test_an_xmp_gps_property_is_found_in_a_png_text_chunk(tmp_path):
    """
    The second carrier class. XMP GPS lives in a PNG iTXt, which
    `structure.py`'s PNG walker deliberately does not open, and which a
    compressed variant would hide from any byte search entirely.
    """
    path = image(tmp_path, "xmp.png", "PNG")
    write_tags(path, **{
        "XMP-exif:GPSLatitude": LAT,
        "XMP-exif:GPSLongitude": LON,
    })
    assert_oracle_sees_gps(path, "png xmp")

    report = gps_verify.scan(path)
    assert report.status is GpsStatus.CARRIER_FOUND, report.describe()
    assert [item.carrier for item in report.findings] == [gps_verify.CARRIER_XMP_GPS]
    assert "GPSLatitude" in report.findings[0].detail

    result = scrubbed(path)
    assert result["status"] == "sanitized", result
    assert gps_verify.scan(path).status is GpsStatus.CLEAN


@pytest.mark.skipif(not HAVE_FFMPEG, reason="ffmpeg is needed to build an MP4")
def test_a_real_mp4_location_atom_is_found_and_then_gone(mobile_fixture):
    """
    The third carrier class, on a real ffmpeg MP4 with a real `(c)xyz` written
    by exiftool, not on a hand-built container. The mobile fixture's own guard
    has already proved that `QuickTime:GPSCoordinates` is present, so a factory
    that silently stopped writing it cannot make this vacuous.
    """
    path, _value = mobile_fixture(".mp4")
    assert_oracle_sees_gps(path, "mobile mp4")

    report = gps_verify.scan(path)
    assert report.status is GpsStatus.CARRIER_FOUND, report.describe()
    assert any(item.carrier == gps_verify.CARRIER_ISOBMFF_LOCATION
               for item in report.findings), report.describe()

    result = scrubbed(path)
    assert result["status"] == "sanitized", result
    after = gps_verify.scan(path)
    assert after.status is GpsStatus.CLEAN, after.describe()


def test_the_three_axis_overcorrection_of_the_location_box_rule(tmp_path):
    """
    Overcorrection test 1 in its structural form, on the one rule in this
    module that is an exclusion: a location box counts only when it is inside a
    `udta` or an `ilst`, which is what stops a `mdat` payload from firing.

    Trap 11's three axes, translated to a box tree. Plant a real carrier in
    each of the three positions the exclusion touches and require the check to
    still find it:
      - the same box type under a DIFFERENT legal parent (`ilst`, not `udta`),
      - a DIFFERENT location box under the same parent (`loci`, not `(c)xyz`),
      - a different carrier CLASS in the same container (the XMP uuid box).
    """
    cases = {
        "udta/(c)xyz": isobmff(box(b"udta", xyz_atom())),
        "meta/ilst/(c)xyz": isobmff(box(b"meta", b"\x00" * 4 + box(b"ilst", xyz_atom()))),
        "udta/loci": isobmff(box(b"udta", box(b"loci", b"\x00" * 4 + b"eng\x00here\x00"
                                              + struct.pack(">iii", 0, 0, 0)))),
        "uuid/xmp": isobmff(box(
            b"udta",
            box(b"uuid", gps_verify._ISOBMFF_XMP_UUID
                + b'<x:xmpmeta xmlns:x="adobe:ns:meta/">'
                  b'<rdf:RDF xmlns:rdf="http://www.w3.org/1999/02/22-rdf-syntax-ns#">'
                  b'<rdf:Description xmlns:exif="http://ns.adobe.com/exif/1.0/" '
                  b'exif:GPSLatitude="43,39.19356N"/></rdf:RDF></x:xmpmeta>'),
        )),
    }
    for label, data in cases.items():
        path = write(str(tmp_path / f"{label.replace('/', '_')}.mp4"), data)
        report = gps_verify.scan(path)
        assert report.status is GpsStatus.CARRIER_FOUND, (label, report.describe())


def test_a_gps_carrier_hidden_in_a_heic_exif_item_is_found(tmp_path, mobile_fixture):
    """
    The HEIF item case, which section 8 lists as NOT measured by the design
    document. A HEIC keeps its EXIF in `mdat`, reachable only through
    `meta/iloc`, so a box-tree walk alone would report a geotagged HEIC clean.

    Skips only when no HEIC source file exists on this machine; the mobile
    fixture makes that call.
    """
    path, _value = mobile_fixture(".heic")
    assert_oracle_sees_gps(path, "mobile heic")

    report = gps_verify.scan(path)
    assert report.status is GpsStatus.CARRIER_FOUND, report.describe()
    assert any(item.carrier == CARRIER_EXIF_GPS_IFD for item in report.findings), \
        report.describe()
    assert "EXIF item" in report.findings[0].detail, report.describe()


# The XMP packet the two compressed-chunk tests hide. Written out here so the
# test can prove the coordinate is absent from the file's bytes, which is the
# whole point of hiding it in a compressed chunk.
_XMP_GPS_PACKET = (
    b'<x:xmpmeta xmlns:x="adobe:ns:meta/">'
    b'<rdf:RDF xmlns:rdf="http://www.w3.org/1999/02/22-rdf-syntax-ns#">'
    b'<rdf:Description xmlns:exif="http://ns.adobe.com/exif/1.0/" '
    b'exif:GPSLatitude="43,39.19356N" exif:GPSLongitude="79,22.99104W"/>'
    b'</rdf:RDF></x:xmpmeta>'
)


def png_chunk(ctype, payload):
    """One syntactically valid PNG chunk, CRC included. A writer, not a reader."""
    import zlib

    body = ctype + payload
    return (struct.pack(">I", len(payload)) + body
            + struct.pack(">I", zlib.crc32(body) & 0xFFFFFFFF))


def png_with_chunk_after_ihdr(path, chunk):
    """Splice a chunk in immediately after IHDR, where ancillary chunks belong."""
    data = read(path)
    at = data.index(b"IHDR") - 4
    (length,) = struct.unpack(">I", data[at:at + 4])
    at += 12 + length
    return write(path, data[:at] + chunk + data[at:])


@pytest.mark.parametrize("kind", ["zTXt", "iTXt"])
def test_a_compressed_xmp_chunk_hides_gps_from_the_byte_scan_but_not_from_this(
        tmp_path, kind):
    """
    The carrier the 1.0.2 CHANGELOG lists as a known limit of the byte scan,
    and the one `test_png_engine.py` built its whole ordering around: a
    compressed PNG text chunk. A value inside one is NOT in the file's bytes,
    so a byte search for it returns nothing whether or not it survived, and a
    scrubber that removed nothing would pass that search.

    Both halves are asserted. First that the coordinate really is invisible to
    a byte search, so the test is measuring the hard case. Then that the
    structural check finds it anyway, because it inflates the chunk before
    parsing the packet.
    """
    import zlib

    path = image(tmp_path, f"hidden{kind}.png", "PNG")
    keyword = b"XML:com.adobe.xmp"
    if kind == "zTXt":
        # zTXt: keyword, NUL, compression method, then deflated text.
        payload = keyword + bytes([0, 0]) + zlib.compress(_XMP_GPS_PACKET)
    else:
        # iTXt: keyword, NUL, compression flag, method, language, NUL,
        # translated keyword, NUL, then deflated text.
        payload = (keyword + bytes([0, 1, 0, 0, 0])
                   + zlib.compress(_XMP_GPS_PACKET))
    png_with_chunk_after_ihdr(path, png_chunk(kind.encode("ascii"), payload))

    blob = read(path)
    assert kind.encode("ascii") in blob, "the fixture did not get its chunk"
    for form in (b"GPSLatitude", b"43,39.19356N", b"exif:"):
        assert form not in blob, (
            f"{form!r} is findable in the raw bytes, so this fixture is not "
            f"hiding anything and the test proves nothing"
        )

    report = gps_verify.scan(path)
    assert report.status is GpsStatus.CARRIER_FOUND, report.describe()
    assert "GPSLatitude" in report.findings[0].detail, report.describe()


# ---------------------------------------------------------------- no grep


def test_a_location_atom_inside_mdat_is_not_a_location_atom(tmp_path):
    """
    Overcorrection test 6, the no-grep test, in both directions.

    Section 3 measured 3620 sequences of 0xA9 followed by three ASCII letters
    per 102 MB of ordinary H.264, which is one chance in 17,576 of spelling
    `xyz` each time, and measured the bare string `GPS` seven times in a 102 MB
    file with no GPS metadata at all. A file whose `mdat` payload happens to
    contain those bytes must not be reported as carrying a location box, and
    only a box tree walk can tell the difference.
    """
    payload = (b"\xa9xyz\x00\x19\x15\xc7+43.653226-079.383184+076.500/"
               b"loci\x00GPSLatitude GPSLongitude GPSCoordinates")
    path = write(str(tmp_path / "planted.mp4"),
                 isobmff(box(b"udta", box(b"name", b"nothing here")),
                         mdat=b"\x00" * 32 + payload + b"\x00" * 32))

    blob = read(path)
    assert b"\xa9xyz" in blob and b"GPSLatitude" in blob, (
        "the fixture does not contain the bytes a grep would trip over, so it "
        "is not testing anything"
    )
    report = gps_verify.scan(path)
    assert report.status is GpsStatus.CLEAN, report.describe()


def test_the_word_gps_in_an_xmp_description_is_not_a_coordinate(tmp_path):
    """
    The same test one level in. The XMP check parses XML and looks at the
    QUALIFIED NAMES of elements and attributes, so a description that talks
    about GPS is character data and has no name to match.
    """
    path = image(tmp_path, "words.png", "PNG")
    write_tags(path, **{
        "XMP-dc:Description": "Taken with GPS off. GPSLatitude was not recorded.",
    })
    assert b"GPSLatitude" in read(path), "the fixture lost the words it is about"

    report = gps_verify.scan(path)
    assert report.status is GpsStatus.CLEAN, report.describe()


def test_a_low_entropy_rational_block_in_the_image_data_is_not_a_coordinate(tmp_path):
    """
    The value-matching false positive that disqualified option 3, constructed
    rather than argued. `0/1 0/1 0/1` as 24 big-endian bytes, planted in a JPEG
    COM marker, is exactly the slice section 3 measured occurring in a 3.4 MB
    DLL. A pointer walk does not look at it.
    """
    path = image(tmp_path, "slice.jpg", "JPEG")
    write_tags(path, Comment="x")
    blob = read(path)
    slice_bytes = struct.pack(">6I", 0, 1, 0, 1, 0, 1)
    at = blob.find(b"\xff\xd8") + 2
    planted = blob[:at] + b"\xff\xfe" + struct.pack(">H", 26) + slice_bytes + blob[at:]
    write(path, planted)

    assert slice_bytes in read(path)
    report = gps_verify.scan(path)
    assert report.status is GpsStatus.CLEAN, report.describe()


# ---------------------------------------------------------------- fail closed


def test_an_unparseable_output_is_never_clean(tmp_path):
    """
    Overcorrection test 7, and trap 12 in a new place. Truncation, garbage and
    a file of the wrong type wearing the right extension must all report
    "could not check" and never "clean".
    """
    good, _value = geotagged(tmp_path, "good.jpg", "JPEG")
    blob = read(good)

    cases = {
        "truncated": blob[:len(blob) // 3],
        "header only": blob[:4],
        "not a jpeg at all": b"\x00" * 4096,
    }
    for label, data in cases.items():
        path = write(str(tmp_path / f"{label.replace(' ', '_')}.jpg"), data)
        report = gps_verify.scan(path)
        assert report.status is GpsStatus.ERROR, (label, report.describe())
        assert report.is_clean is False, label
        assert report.error, label

    for label, data in {"truncated mp4": isobmff(box(b"udta", xyz_atom()))[:20],
                        "lying box size": box(b"ftyp", b"isom") + b"\xff\xff\xff\xffmoov"}.items():
        path = write(str(tmp_path / f"{label.replace(' ', '_')}.mp4"), data)
        report = gps_verify.scan(path)
        assert report.status is GpsStatus.ERROR, (label, report.describe())


def test_a_region_that_could_not_be_reached_is_not_a_pass(tmp_path):
    """
    INCOMPLETE is a distinct state, and the reason is a motion photo: a whole
    second JPEG, with its own EXIF GPS, lives after the first one's EOI. This
    walker does not open it, and a check that reported CLEAN on a file it only
    partly read would be the same over-claim in miniature.
    """
    clean = image(tmp_path, "trailer.jpg", "JPEG")
    write_tags(clean, Artist=sentinel("trailer"))
    assert gps_verify.scan(clean).status is GpsStatus.CLEAN

    write(clean, read(clean) + b"TRAILING PAYLOAD" * 8)
    report = gps_verify.scan(clean)
    assert report.status is GpsStatus.INCOMPLETE, report.describe()
    assert report.is_clean is False
    assert report.limits and "EOI" in report.limits[0], report.limits
    assert "PARTLY CHECKED" in report.describe()


def test_an_xmp_packet_that_will_not_parse_is_a_limit_not_a_pass(tmp_path):
    """
    Trap 12 inside the XMP check. A packet that is not well-formed XML has not
    been checked; reporting nothing found would make a broken packet safer than
    a readable one.
    """
    path = write(str(tmp_path / "badxmp.mp4"), isobmff(box(
        b"udta",
        box(b"uuid", gps_verify._ISOBMFF_XMP_UUID
            + b'<x:xmpmeta xmlns:x="adobe:ns:meta/"><rdf:RDF unclosed'),
    )))
    report = gps_verify.scan(path)
    assert report.status is GpsStatus.INCOMPLETE, report.describe()
    assert report.is_clean is False
    assert any("could not be parsed" in limit for limit in report.limits), report.limits


# ---------------------------------------------------------------- the false-positive gate


def test_a_selective_run_that_keeps_gps_must_still_verify_clean(tmp_path):
    """
    Overcorrection test 5, and section 3 calls it the important one.

    Measured 2026-09-06 on the shipped tool: `scrub --remove-field Artist` on a
    geotagged photo removes the Artist, deliberately KEEPS the GPS, and
    correctly reports "verified clean". An unconditional GPS assertion would
    turn that correct run into a failure, which is the expensive kind of false
    positive.

    So three things are asserted together: the shipped behaviour still holds,
    the GPS carrier really is still in the output, and `gps_in_scope()` says
    the check must not run for those fields. The third is what makes the wiring
    commit safe, and it is asserted BEFORE the wiring exists on purpose.
    """
    path, value = geotagged(tmp_path, "selective.jpg", "JPEG")

    with MetadataScrubber(backup=False) as scrubber:
        result = scrubber.sanitize_file(path, fields_to_remove=["Artist"])

    assert result["status"] == "sanitized", result
    assert result["verification"]["verdict"] == "verified_clean", result["verification"]
    assert value not in str(read(path)), "the Artist should have been removed"

    assert gps_verify.scan(path).status is GpsStatus.CARRIER_FOUND, (
        "this test is not measuring anything unless the GPS really survived"
    )
    assert gps_verify.gps_in_scope(["Artist"]) is False
    assert gps_verify.gps_in_scope(["EXIF:Artist", "Copyright"]) is False


@pytest.mark.parametrize("fields,expected", [
    (None, True),
    ([], True),
    (["Artist"], False),
    (["GPSLatitude"], True),
    (["EXIF:GPSLongitude"], True),
    (["gpsposition"], True),
    (["Artist", "GPSAltitude"], True),
    (["Location"], True),
    (["Coordinates"], True),
    (["Author", "Title", "Producer"], False),
])
def test_gps_in_scope_reads_the_field_list(fields, expected):
    """
    The scoping rule, over a cross-product rather than one illustrative case.
    A full strip (None or empty) is always in scope; a selective run is in
    scope only when it names a GPS field.
    """
    assert gps_verify.gps_in_scope(fields) is expected


# ---------------------------------------------------------------- not checked


def test_an_uncovered_format_can_never_read_as_checked(tmp_path):
    """
    Overcorrection test 8 and test 10 together, now in the form the wiring
    allows.

    Section 9 names the single biggest risk in this work: that GPS
    verification ships covering the easy containers while `verified clean` on a
    .mkv goes on meaning exactly what it meant before. Every mechanism that
    prevents it is asserted here.

    THE DEFERRAL THIS TEST CARRIED, IN TWO HALVES. The old wording was: when
    gps_verify is wired into verify.py, assert that "a COMPLETE format whose
    GPS check reported NOT_CHECKED cannot produce a BARE verified_clean". It
    was collected by test_verify_does_not_import_this_module_yet, which was
    deleted on 2026-09-07 when the wiring landed. That sentence admits two
    readings and they are not the same size, so both are written down rather
    than the convenient one being picked:

      DISCHARGED HERE. "Not BARE": such a file must carry, in the same result,
      a coverage statement naming the checks that did not run. That is Option B
      in docs/WHAT-THE-TOOL-CLAIMS.md, it is what shipped, and the second half
      of this test measures it end to end on a real .pdf scrub.

      STILL OPEN, AND NOT DECIDED HERE. "Cannot produce verified_clean AT ALL":
      the verdict VALUE itself would change. That is Option D, it is breaking
      for every parser matching on `verdict`, and section 5 leaves the choice
      to the owner. It is NOT closed by this commit and must not be read as
      closed. Its collector is now
      tests/test_coverage_reporting.py::test_the_zero_needle_decision_is_still_open
      and the named constant `verify.ZERO_NEEDLE_DECISION_IS_OPEN`, which that
      test asserts alongside the behaviour it would change.
    """
    path = write(str(tmp_path / "video.mkv"), b"\x1aE\xdf\xa3" + b"\x00" * 512)
    report = gps_verify.scan(path)

    assert report.status is GpsStatus.NOT_CHECKED
    assert report.is_clean is False
    assert report.carriers_checked == ()
    assert report.findings == ()
    assert "NOT CHECKED" in report.describe()
    assert report.as_dict()["status"] == "not_checked"
    assert report.as_dict()["is_clean"] is False

    with pytest.raises(TypeError):
        bool(report)
    with pytest.raises(TypeError):
        if report:                      # noqa: B015 - this is the thing being refused
            pass

    # The half the wiring discharges, measured through the shipping tool rather
    # than asserted about it. A .pdf is declared COMPLETE, has no GPS walker
    # and no structural walker, so it is the sharpest available case: the tool
    # says "verified clean" about a file on which two of its three checks never
    # ran.
    document, _sentinel = build(".pdf", tmp_path)
    assert CAPABILITIES[".pdf"].completeness is Completeness.COMPLETE
    assert ".pdf" not in gps_verify.supported_extensions()

    with MetadataScrubber(backup=False) as scrubber:
        result = scrubber.sanitize_file(document, remove_all=True)

    verification = result["verification"]
    coverage = verification["coverage"]
    assert verification["verdict"] == "verified_clean", verification

    # NOT bare. The claim now travels with its own limits attached.
    assert coverage is not None, "verified_clean with no coverage statement"
    assert coverage["gps_status"] == GpsStatus.NOT_CHECKED.value
    assert coverage["gps_carriers_checked"] == []
    assert coverage["every_check_ran"] is False
    assert "gps_carriers" in coverage["unchecked"]
    # Trap 6: the distinction is TEXT, so it survives into anything that
    # renders it, including a terminal with no colour.
    assert "NOT CHECKED" in coverage["detail"]
    assert ".pdf" in coverage["detail"]


def test_only_one_of_the_five_states_is_clean():
    """
    The cross-product, not one illustrative example. Four of the five states
    must be distinguishable from CLEAN by `is_clean`, by `status`, and by the
    TEXT of `describe()`, which is trap 6's rule: a distinction a caller can
    only see as a colour is not a distinction.
    """
    reports = {
        GpsStatus.NOT_CHECKED: GpsReport(applicable=False, extension=".mkv"),
        GpsStatus.ERROR: GpsReport(applicable=True, carriers_checked=("exif-gps-ifd",),
                                   error="boom"),
        GpsStatus.INCOMPLETE: GpsReport(applicable=True,
                                        carriers_checked=("exif-gps-ifd",),
                                        limits=("a trailer was not read",)),
        GpsStatus.CARRIER_FOUND: GpsReport(
            applicable=True, carriers_checked=("exif-gps-ifd",),
            findings=(gps_verify.GpsCarrier("exif-gps-ifd", "a GPS IFD"),)),
        GpsStatus.CLEAN: GpsReport(applicable=True, carriers_checked=("exif-gps-ifd",)),
    }
    for expected, report in reports.items():
        assert report.status is expected, report
        assert report.is_clean is (expected is GpsStatus.CLEAN), expected

    texts = {status: report.describe() for status, report in reports.items()}
    assert len(set(texts.values())) == 5, texts
    for status, text in texts.items():
        assert text.strip(), status
        if status is not GpsStatus.CLEAN:
            assert "none present" not in text, (status, text)


def test_a_report_cannot_claim_to_be_applicable_without_naming_what_it_checked():
    """
    The invariant that makes `findings == ()` mean something. An applicable
    report with an empty `carriers_checked` would serialise as "checked, found
    nothing", which is the over-claim this module exists to prevent, so the
    constructor refuses it outright.
    """
    with pytest.raises(ValueError):
        GpsReport(applicable=True)
    with pytest.raises(ValueError):
        GpsReport(applicable=False, carriers_checked=("exif-gps-ifd",))
    # An applicable report that ERRORED is allowed to name carriers it did not
    # finish, because ERROR already says the answer is not evidence.
    assert GpsReport(applicable=True, error="x").status is GpsStatus.ERROR


def test_the_covered_extensions_are_exactly_the_measured_ones():
    """
    Trap 11's rule applied to the walker table, in both directions.

    An extension removed from this list silently drops GPS coverage. An
    extension ADDED to it claims coverage that nothing here measured, which is
    section 9's over-claim in its purest form: every name below has a fixture
    in this file or in conftest that was built, geotagged, scrubbed and
    re-walked on this machine. Adding a raw format, Matroska or PDF means
    building the fixture in the same commit.
    """
    measured = {
        ".jpg", ".jpeg", ".jpe",
        ".tif", ".tiff",
        ".png", ".webp",
        ".mp4", ".m4v", ".mov", ".qt", ".mqv", ".lrv", ".f4v", ".f4a",
        ".m4a", ".m4b", ".heic", ".heif", ".avif",
    }
    assert set(gps_verify.supported_extensions()) == measured, (
        f"walker table only: "
        f"{sorted(set(gps_verify.supported_extensions()) - measured)}, "
        f"measured only: {sorted(measured - set(gps_verify.supported_extensions()))}"
    )


def test_every_registered_extension_actually_dispatches_to_a_working_walker(tmp_path):
    """
    The registry claim, measured for every name in it rather than for the two
    extensions that happened to have a fixture.

    An extension in `_WALKERS` is a promise that a file with that name gets
    walked. Without this test, `.mov` and `.m4v` would be riding on `.mp4`'s
    measurement, which is the same inference-instead-of-measurement this
    project exists to refuse. One GPS-bearing file per registered extension,
    and every one of them must report CARRIER_FOUND.

    The ISO base media files here are the hand-built container, so this test
    measures DISPATCH and the walk, not any particular muxer's box layout;
    `test_a_real_mp4_location_atom_is_found_and_then_gone` covers real ffmpeg
    output.
    """
    jpeg, _ = geotagged(tmp_path, "dispatch.jpg", "JPEG")
    tiff, _ = geotagged(tmp_path, "dispatch.tif", "TIFF")
    png, _ = geotagged(tmp_path, "dispatch.png", "PNG")
    webp, _ = geotagged(tmp_path, "dispatch.webp", "WEBP")
    sources = {".jpg": jpeg, ".jpeg": jpeg, ".jpe": jpeg,
               ".tif": tiff, ".tiff": tiff, ".png": png, ".webp": webp}

    verdicts = {}
    for extension in sorted(gps_verify.supported_extensions()):
        target = str(tmp_path / f"copy{extension}")
        if extension in sources:
            shutil.copyfile(sources[extension], target)
        else:
            write(target, isobmff(box(b"udta", xyz_atom())))
        verdicts[extension] = gps_verify.scan(target)

    missed = {
        extension: report.describe()
        for extension, report in verdicts.items()
        if report.status is not GpsStatus.CARRIER_FOUND
    }
    assert not missed, missed
    assert set(verdicts) == set(gps_verify.supported_extensions())


def test_coverage_reports_the_gap_at_runtime():
    """
    The module must be able to SAY what it does not cover, because a comment
    goes stale and section 9's failure mode is a claim nobody can check.

    The named formats below are asserted individually rather than by count: a
    count would go stale the moment the capability table grows, and a stale
    denominator is worse than no denominator.
    """
    gaps = gps_verify.coverage()
    assert gaps["walkers_without_a_capability_row"] == [], gaps

    covered, uncovered = set(gaps["covered"]), set(gaps["uncovered"])
    assert covered and uncovered
    assert covered.isdisjoint(uncovered)
    assert covered | uncovered == set(CAPABILITIES)

    for extension in (".jpg", ".png", ".webp", ".mp4", ".heic", ".tif"):
        assert extension in covered, extension
    # Named because each is a real GPS carrier this check has no opinion about.
    for extension in (".mkv", ".webm", ".dng", ".cr2", ".nef", ".pdf", ".svg",
                      ".gif", ".avi"):
        assert extension in uncovered, extension

    complete_and_uncovered = sorted(
        extension for extension in uncovered
        if CAPABILITIES[extension].completeness is Completeness.COMPLETE
    )
    assert ".mkv" in complete_and_uncovered, (
        "a COMPLETE format with no GPS walker is exactly the state section 9 "
        "warns about, and it must stay visible rather than become an empty list"
    )


def test_this_module_is_wired_into_verify():
    """
    The replacement for test_verify_does_not_import_this_module_yet, which was
    deleted on 2026-09-07 in the commit that wired this module in. That test
    existed to make the UNWIRED state a deliberate, visible fact; the state it
    guarded no longer exists, and a test asserting it would now be asserting
    the opposite of the design.

    This is its inverse, and it is not decoration: `verify.py` importing this
    module is the whole of Option B in
    docs/WHAT-THE-TOOL-CLAIMS.md. If someone unwires it, every coverage
    assertion below still needs to fail loudly rather than quietly start
    reporting NOT_CHECKED for every file on earth, which is exactly what a
    missing import would look like.

    THE HALF OF THE OLD DEFERRAL THAT IS DISCHARGED, AND THE HALF THAT IS NOT,
    are both written out in test_an_uncovered_format_can_never_read_as_checked
    below. Read that docstring before assuming this one closed anything.
    """
    assert hasattr(verify, "gps_verify"), (
        "verify.py no longer imports gps_verify. Without it every file reports "
        "GPS NOT_CHECKED, which reads as a coverage gap rather than as a "
        "missing wire."
    )
    assert verify.gps_verify is gps_verify

    # And the wire carries something. An import nothing calls is not a wire.
    coverage = verify.Coverage.__doc__ or ""
    assert "gps_verify" in coverage


def test_the_findings_and_limits_survive_serialisation(tmp_path):
    """
    Overcorrection test 9. Whatever eventually renders this has to be able to,
    and it has to render the distinction as TEXT.
    """
    path, _value = geotagged(tmp_path, "serialise.jpg", "JPEG")
    payload = gps_verify.scan(path).as_dict()

    assert payload["status"] == "carrier_found"
    assert payload["is_clean"] is False
    assert payload["applicable"] is True
    assert payload["extension"] == ".jpg"
    assert payload["carriers_checked"] == ["exif-gps-ifd", "xmp-gps"]
    assert len(payload["findings"]) == 1
    assert "0x8825" in payload["findings"][0]
    assert payload["error"] is None
    assert payload["detail"].startswith("GPS carriers: FOUND")

    import json

    assert json.loads(json.dumps(payload)) == payload, "as_dict() is not JSON-safe"


# ---------------------------------------------------------------- the whole corpus


def test_every_container_this_module_covers_is_clean_after_a_real_scrub(tmp_path):
    """
    The end-to-end direction, over the four containers that can be built
    without ffmpeg. Each one carries a coordinate the byte scan provably cannot
    see, is scrubbed by the shipping tool, and must then report CLEAN rather
    than NOT_CHECKED, INCOMPLETE or ERROR.

    A single formats-loop rather than four tests because the interesting
    failure is one container behaving differently from the others.
    """
    outcomes = {}
    for name, fmt in (("all.jpg", "JPEG"), ("all.tif", "TIFF"),
                      ("all.png", "PNG"), ("all.webp", "WEBP")):
        path, _value = geotagged(tmp_path, name, fmt)
        assert gps_verify.scan(path).status is GpsStatus.CARRIER_FOUND, name
        result = scrubbed(path)
        outcomes[name] = (result["status"], gps_verify.scan(path))

    dirty = {
        name: report.describe()
        for name, (status, report) in outcomes.items()
        if status != "sanitized" or report.status is not GpsStatus.CLEAN
    }
    assert not dirty, dirty
