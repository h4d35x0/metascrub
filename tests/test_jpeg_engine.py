"""
The pure-Python JPEG engine: docs/ANDROID-MEDIA-BUILD.md section 2.1.

WHAT THIS FILE IS FOR

Section 2.1 is a whitelist of marker segments, and section 3.2 says the gate on
a whitelist is a STRUCTURAL assertion over the output rather than a search for
the values that used to be in it. Three measurements in 3.2 say why: GPS is
stored as rationals so the decimal form is not in the bytes at all, a
compressed carrier is not in the bytes by construction, and a post-EOI trailer
is invisible to exiftool so no needle is ever generated for it.

So the gate here is `structural_violations()`, which asks a question that does
not depend on having read anything first: is every marker in this output on the
keep-list, is every kept APPn the identifier it claims to be, and is there
anything at all after the first top-level EOI. The sentinel searches and the
exiftool oracle are the belt.

WHY THE WALKER BELOW IS NOT IMPORTED FROM metascrub

Section 3.1: if the reader and the writer are the same code, the tool only ever
looks for what it already knows how to remove. `regions()` is written from the
JPEG marker syntax, not from `jpeg_engine.py`, and it locates the top-level EOI
by a DIFFERENT method than the engine does. The engine walks segment lengths
until it reaches EOI; this file walks to the LAST SOS and then searches forward
from there, which is where `FF D9` cannot occur by accident.
`test_the_two_independent_eoi_methods_agree` asserts the two agree, so a bug
that fooled one would have to fool both in the same direction.

tests/test_motion_photo.py takes the same position for the same reason, and
this file is its successor: that file demonstrated the leak against a walker
written inside itself, and this one asserts the shipped engine does not have it.

THE MUTATION CHECK IS PERMANENT AND IN THE SUITE

`test_the_structural_gate_rejects_every_broken_removal` runs the gate against
four deliberately broken strippers and requires each to be caught, and
`test_the_broken_strippers_are_not_caught_by_accident` requires each broken
output to still be a decodable JPEG showing the same picture, so the gate
cannot be rejecting them for being damaged. A green suite that would also pass
on broken code proves nothing, and saying so in a comment is not asserting it.
"""

from __future__ import annotations

import importlib.util
import io
import os
import shutil
import struct

import pytest

from conftest import (
    ORACLE_ALLOW_JPEG,
    assert_oracle_sees_nothing,
    exiftool_or_fail,
    oracle_metadata,
    oracle_tags,
    sentinel,
)
from metascrub.capabilities import CAPABILITIES, Engine
from metascrub.engines import get_engine
from metascrub.engines.base import EngineError
from metascrub.engines.jpeg_engine import JpegEngine

HAVE_FFMPEG = shutil.which("ffmpeg") is not None


# ---------------------------------------------------------------------------
# THE INDEPENDENT STRUCTURAL WALKER
#
# Written from the JPEG marker syntax. Not imported from the engine, and not
# imported from test_motion_photo.py either: a shared helper would let one bug
# silence two files.
# ---------------------------------------------------------------------------

# Markers with no length field: TEM, SOI, EOI and RST0..RST7.
STANDALONE = {0x01, 0xD8, 0xD9} | set(range(0xD0, 0xD8))

# Section 2.1's keep-list expressed as marker numbers. APP0 and APP14 are NOT
# in here: they are conditional on the identifier and are checked separately,
# which is the whole point of the 2026-09-06 correction.
KEEP_BY_NUMBER = (
    {0xD8, 0xD9, 0xDA, 0xDB, 0xC4, 0xDD}           # SOI EOI SOS DQT DHT DRI
    | set(range(0xD0, 0xD8))                        # RST0..RST7
    | {code for code in range(0xC0, 0xD0) if code not in (0xC4, 0xC8, 0xCC)}
)

# The two conditional rows. APP0's identifier is NUL terminated; APP14's is
# not, because the Adobe segment is the literal "Adobe" followed immediately by
# a two-byte version.
KEEP_BY_IDENTIFIER = {0xE0: b"JFIF\x00", 0xEE: b"Adobe"}

MARKER, SCAN, TRAILER = "marker", "scan", "trailer"


def regions(data: bytes):
    """
    Yield (kind, code, start, end, payload) covering every byte of the file.

    kind is MARKER, SCAN (entropy-coded data, which cannot be walked
    segment-wise) or TRAILER (anything after the first top-level EOI).
    """
    assert data[:2] == b"\xff\xd8", "not a JPEG"
    at = 0
    while at < len(data):
        assert data[at] == 0xFF, "marker desync at offset %d" % at
        cursor = at
        while cursor < len(data) and data[cursor] == 0xFF:
            cursor += 1
        assert cursor < len(data), "file ends in marker padding"
        code = data[cursor]
        if code in STANDALONE:
            yield MARKER, code, at, cursor + 1, b""
            at = cursor + 1
            if code == 0xD9:
                if at < len(data):
                    yield TRAILER, None, at, len(data), b""
                return
            continue
        length = struct.unpack(">H", data[cursor + 1:cursor + 3])[0]
        end = cursor + 1 + length
        assert end <= len(data), "segment at %d runs past the end of the file" % at
        yield MARKER, code, at, end, data[cursor + 3:end]
        at = end
        if code == 0xDA:
            stop = end_of_scan(data, end)
            yield SCAN, None, end, stop, b""
            at = stop
    raise AssertionError("no EOI: this JPEG is truncated")


def segments(data: bytes):
    """Just the marker segments, in order, up to and including the first EOI."""
    for kind, code, start, end, payload in regions(data):
        if kind == MARKER:
            yield code, start, end, payload


def end_of_scan(data: bytes, start: int) -> int:
    """
    The offset of the next real marker after entropy-coded data.

    Inside a scan an 0xFF is stuffed as FF 00, the only bare markers allowed
    are RST0..RST7, and a run of 0xFF is legal fill.
    """
    at = start
    while at < len(data) - 1:
        if data[at] != 0xFF:
            at += 1
        elif data[at + 1] == 0xFF:
            at += 1
        elif data[at + 1] == 0x00 or 0xD0 <= data[at + 1] <= 0xD7:
            at += 2
        else:
            return at
    raise AssertionError("entropy data ran off the end of the file")


def identifier(payload: bytes):
    index = payload.find(b"\x00")
    if index < 0 or index > 80:
        return None
    try:
        payload[:index].decode("ascii")
    except UnicodeDecodeError:
        return None
    return payload[:index]


def first_top_level_eoi(data: bytes) -> int:
    """
    Index just past the outer image's EOI, found by a DIFFERENT method than the
    engine uses.

    Deliberately not `data.index(b"\\xff\\xd9")`. An APP0/JFXX or an EXIF
    thumbnail is a complete JPEG with its own EOI, so a search from byte zero
    finds the thumbnail's and a stripper built on it truncates the real image
    away. This walks to the last SOS first, then searches forward.
    """
    last_sos_end = None
    for code, _start, end, _payload in segments(data):
        if code == 0xDA:
            last_sos_end = end
    if last_sos_end is None:
        raise AssertionError("no SOS in this JPEG")
    return data.index(b"\xff\xd9", last_sos_end) + 2


def marker_codes(data: bytes):
    return [code for code, _start, _end, _payload in segments(data)]


def app_identifiers(data: bytes, marker: int):
    return [identifier(payload) for code, _start, _end, payload in segments(data)
            if code == marker]


def scan_bytes(data: bytes) -> bytes:
    """Every entropy-coded run concatenated: the compressed image itself."""
    out = bytearray()
    for kind, _code, start, end, _payload in regions(data):
        if kind == SCAN:
            out += data[start:end]
    return bytes(out)


def structural_violations(data: bytes):
    """
    THE GATE. Section 3.2's JPEG line, in full and in one place.

    Returns human-readable violations; empty means the output cannot contain a
    marker-borne carrier or a trailer, whatever it used to contain.
    """
    violations = []
    for code, start, end, payload in segments(data):
        if code in KEEP_BY_IDENTIFIER:
            expected = KEEP_BY_IDENTIFIER[code]
            if not payload.startswith(expected):
                violations.append(
                    "marker 0x%02X at %d survives with identifier %r, not %r; "
                    "the keep-list is keyed on the identifier, not the number"
                    % (code, start, identifier(payload), expected))
            continue
        if code not in KEEP_BY_NUMBER:
            violations.append(
                "marker 0x%02X at %d (%d bytes, identifier %r) is not on the "
                "section 2.1 keep-list"
                % (code, start, end - start, identifier(payload)))
    trailing = len(data) - first_top_level_eoi(data)
    if trailing:
        violations.append(
            "%d bytes survive after the first top-level EOI, which is where a "
            "motion photo trailer lives" % trailing)
    return violations


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def scrub(path: str):
    """Run the real engine through its real contract and return (bytes, report)."""
    removed = JpegEngine().strip_all(path)
    with open(path, "rb") as handle:
        return handle.read(), removed


def pixels(data: bytes) -> bytes:
    from PIL import Image

    image = Image.open(io.BytesIO(data))
    image.load()
    return image.tobytes()


def write(tmp_path, name: str, data: bytes) -> str:
    path = str(tmp_path / name)
    with open(path, "wb") as handle:
        handle.write(data)
    return path


def drop_markers(data: bytes, codes) -> bytes:
    """Remove the named marker numbers and nothing else, keeping the trailer."""
    out = bytearray()
    for kind, code, start, end, _payload in regions(data):
        if kind == MARKER and code in codes:
            continue
        out += data[start:end]
    return bytes(out)


def app0_jfif_segment(data: bytes):
    for code, start, end, payload in segments(data):
        if code == 0xE0 and payload.startswith(b"JFIF\x00"):
            return data[start:end]
    return None


def insert_after_leading_apps(data: bytes, segment: bytes) -> bytes:
    """Splice a segment in after SOI and any APPn already at the front."""
    at = 2
    for code, _start, end, _payload in segments(data):
        if code == 0xD8:
            continue
        if 0xE0 <= code <= 0xEF:
            at = end
            continue
        break
    return data[:at] + segment + data[at:]


def _plain(size=(96, 72), mode="RGB", **save):
    from PIL import Image

    image = Image.new("RGB", size, (120, 40, 40))
    for x in range(size[0]):
        for y in range(size[1]):
            image.putpixel((x, y), (x * 2 % 256, y * 3 % 256, (x + y) % 256))
    if mode != "RGB":
        image = image.convert(mode)
    buffer = io.BytesIO()
    image.save(buffer, format="JPEG", quality=90, **save)
    return buffer.getvalue()


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------

@pytest.fixture(scope="session")
def rich_bytes(tmp_path_factory):
    """
    A JPEG carrying every marker-borne carrier section 2.1 names as removable,
    each with its own sentinel where a sentinel can be stored.

    exiftool writes it, so the shape is a real one rather than a guess:
    measured 2026-09-06, this produces APP0/JFIF, APP1/Exif, APP13/Photoshop
    3.0, APP1/XMP and COM, with the COM marker written AFTER every APPn and
    immediately before DQT.
    """
    directory = tmp_path_factory.mktemp("jpegrich")
    path = str(directory / "rich.jpg")
    with open(path, "wb") as handle:
        handle.write(_plain())
    values = {
        "exif": sentinel("jpegengineexif"),
        "xmp": sentinel("jpegenginexmp"),
        "iptc": sentinel("jpegengineiptc"),
        "com": sentinel("jpegenginecom"),
    }
    exiftool_or_fail().execute(
        "-EXIF:Artist=" + values["exif"],
        "-EXIF:ImageDescription=" + values["exif"],
        "-EXIF:Make=Google",
        "-EXIF:Model=Pixel 7",
        "-GPS:GPSLatitude=37.4220",
        "-GPS:GPSLatitudeRef=N",
        "-GPS:GPSLongitude=-122.0841",
        "-GPS:GPSLongitudeRef=W",
        "-XMP-dc:Description=" + values["xmp"],
        # Caption-Abstract rather than By-line: measured 2026-09-06, exiftool
        # truncates By-line at the IPTC-defined 32 bytes, which is shorter than
        # a sentinel, so the fixture guard below reported it missing. That is
        # the guard working, and the fix is a field with room in it.
        "-IPTC:Caption-Abstract=" + values["iptc"],
        "-Comment=" + values["com"],
        "-overwrite_original",
        path,
    )
    with open(path, "rb") as handle:
        data = handle.read()
    # Asserted, not assumed. A fixture that stored nothing would make every
    # removal assertion below pass by having nothing to remove.
    for name, value in values.items():
        assert value.encode() in data, (
            "the rich fixture did not store the %s sentinel" % name)
    codes = set(marker_codes(data))
    assert {0xE0, 0xE1, 0xED, 0xFE} <= codes, (
        "the rich fixture is missing a carrier it claims to have: %r"
        % (sorted(codes),))
    return data, values


@pytest.fixture
def rich(tmp_path, rich_bytes):
    data, values = rich_bytes
    return write(tmp_path, "rich.jpg", data), values


@pytest.fixture(scope="session")
def jfxx_bytes():
    """
    An APP0 whose identifier is JFXX, carrying a complete embedded JPEG.

    Section 2.1's first 2026-09-06 correction, as a file. exiftool reports the
    payload as `[JFIF] ThumbnailImage`; a keep-list implemented as "keep marker
    0xE0" keeps it, and with it a pre-edit thumbnail of whatever the photo
    looked like before it was cropped.

    The sentinel is inside the thumbnail's own COM marker, so it belongs to the
    embedded IMAGE rather than to a tag value: finding it in an output means
    the whole embedded JPEG is still there.
    """
    value = sentinel("jpegenginejfxx")
    thumbnail = _plain(size=(32, 24))
    comment = value.encode()
    thumbnail = (thumbnail[:2]
                 + b"\xff\xfe" + struct.pack(">H", len(comment) + 2) + comment
                 + thumbnail[2:])
    payload = b"JFXX\x00\x10" + thumbnail
    assert len(payload) + 2 <= 0xFFFF, "thumbnail too large for one segment"
    segment = b"\xff\xe0" + struct.pack(">H", len(payload) + 2) + payload
    data = insert_after_leading_apps(_plain(), segment)
    assert comment in data
    assert app_identifiers(data, 0xE0) == [b"JFIF", b"JFXX"], (
        "the JFXX fixture is not shaped the way it claims: %r"
        % (app_identifiers(data, 0xE0),))
    return data, value


@pytest.fixture(scope="session")
def multi_app1_bytes(tmp_path_factory):
    """
    Several APP1 segments carrying three DIFFERENT identifiers.

    Section 2.1's second 2026-09-06 correction: an oversized XMP payload
    produces Exif\\0\\0, http://ns.adobe.com/xap/1.0/\\0 and
    http://ns.adobe.com/xmp/extension/\\0, the last repeated. A stripper that
    finds "the" APP1 and stops leaves the rest in place.

    Built with exiftool so the split is exiftool's own, not a guess about how
    it splits.
    """
    directory = tmp_path_factory.mktemp("jpegxmp")
    path = str(directory / "multi.jpg")
    with open(path, "wb") as handle:
        handle.write(_plain())
    value = sentinel("jpegengineextxmp")
    payload = str(directory / "big.txt")
    with open(payload, "w", encoding="ascii") as handle:
        handle.write((value + " ") * 4500)
    exiftool_or_fail().execute(
        "-XMP-dc:Description<=" + payload,
        "-EXIF:Artist=" + value,
        "-overwrite_original",
        path,
    )
    with open(path, "rb") as handle:
        data = handle.read()
    identifiers = app_identifiers(data, 0xE1)
    assert len(identifiers) >= 3, (
        "expected at least three APP1 segments, got %r" % (identifiers,))
    assert set(identifiers) == {
        b"Exif",
        b"http://ns.adobe.com/xap/1.0/",
        b"http://ns.adobe.com/xmp/extension/",
    }, identifiers
    return data, value


def _load_motion_photo_builder():
    location = os.path.join(
        os.path.dirname(os.path.abspath(__file__)), "fixtures", "build_motion_photo.py")
    spec = importlib.util.spec_from_file_location(
        "metascrub_motion_photo_fixtures_jpegengine", location)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


@pytest.fixture(scope="session")
def motion_photos(tmp_path_factory):
    if not HAVE_FFMPEG:
        pytest.skip("ffmpeg not available; cannot build the trailer video")
    builder = _load_motion_photo_builder()
    return builder, builder.build_all(str(tmp_path_factory.mktemp("jpegtrailer")))


# ---------------------------------------------------------------------------
# 1. The engine exists, runs, and is deliberately not wired in
# ---------------------------------------------------------------------------

def test_the_engine_reports_itself_available():
    """
    Pure Python and the standard library, which is the point of the mobile
    build: there is no dependency that can be missing.
    """
    usable, reason = JpegEngine().available()
    assert usable, reason
    assert get_engine(Engine.JPEG).available()[0]


def test_the_jpeg_rows_still_route_to_exiftool():
    """
    A guard on the OTHER half of the instruction, and the half a passing suite
    would never notice: implementing the engine must not change the shipped
    desktop behaviour. capabilities.py says switching the default is a separate
    and deliberate decision.
    """
    for extension in (".jpg", ".jpeg", ".jpe"):
        assert CAPABILITIES[extension].engine is Engine.EXIFTOOL, (
            "%s was rewired to the Phase 1 engine; that is a separate decision "
            "and this commit is not it" % extension)


# ---------------------------------------------------------------------------
# 2. THE GATE: the structural assertion of section 3.2
# ---------------------------------------------------------------------------

def test_the_structural_gate_is_not_vacuous_on_the_input(rich_bytes):
    """
    The gate has to FAIL on the fixture, or every assertion using it below is
    measuring nothing. Same species as the 2026-09-04 empty-DEFERRED finding: a
    check over a container that turns out to be empty is the quietest possible
    pass.
    """
    data, _values = rich_bytes
    assert structural_violations(data), (
        "the rich fixture already satisfies the keep-list, so the gate proves "
        "nothing about the engine")


def test_no_marker_outside_the_keep_list_survives(rich):
    out, _removed = scrub(rich[0])
    assert structural_violations(out) == []


def test_every_kept_appn_is_checked_by_identifier(rich):
    """
    Section 2.1, corrected 2026-09-06: the rule is the identifier string, not
    the marker number. Asserted directly as well as through the gate, so a
    failure names the actual problem.
    """
    out, _removed = scrub(rich[0])
    for code, start, _end, payload in segments(out):
        if 0xE0 <= code <= 0xEF:
            expected = KEEP_BY_IDENTIFIER.get(code)
            assert expected is not None, (
                "APP%d survived at offset %d and no keep-list row allows it"
                % (code - 0xE0, start))
            assert payload.startswith(expected), (
                "APP%d survived with identifier %r rather than %r"
                % (code - 0xE0, identifier(payload), expected))


def test_zero_bytes_survive_after_the_first_top_level_eoi(rich):
    out, _removed = scrub(rich[0])
    assert len(out) == first_top_level_eoi(out)
    assert out.endswith(b"\xff\xd9")


def test_the_two_independent_eoi_methods_agree(rich_bytes, jfxx_bytes):
    """
    The engine finds the top-level EOI by walking segment lengths until it
    reaches one. This file finds it by walking to the last SOS and searching
    forward. Two methods; a bug would have to fool both the same way.

    The JFXX fixture is the case that separates both of them from the naive
    search, because it carries a complete inner JPEG whose own EOI comes first.
    """
    from metascrub.engines.jpeg_engine import _walk

    for data in (rich_bytes[0], jfxx_bytes[0]):
        walked = [segment.end for segment in _walk(data) if segment.code == 0xD9]
        assert walked == [first_top_level_eoi(data)]

    poisoned = jfxx_bytes[0]
    assert poisoned.index(b"\xff\xd9") + 2 < first_top_level_eoi(poisoned), (
        "the JFXX fixture does not carry an inner EOI before the real one, so "
        "this test is not exercising the case it exists for")


# ---------------------------------------------------------------------------
# 3. THE MUTATION CHECK
# ---------------------------------------------------------------------------

def _broken_no_removal(data: bytes) -> bytes:
    """A stripper that does nothing at all."""
    return data


def _broken_copy_from_sos(data: bytes) -> bytes:
    """
    The leaking shape section 2.1 names: drop APPn and COM, then copy the rest
    of the file from SOS onward. The photo's own EXIF goes; the trailer stays.
    """
    out = bytearray(b"\xff\xd8")
    for code, start, end, _payload in segments(data):
        if code == 0xD8 or 0xE1 <= code <= 0xEF or code == 0xFE:
            continue
        out += data[start:end]
        if code == 0xDA:
            out += data[end:]
            break
    return bytes(out)


def _broken_keep_app0_by_number(data: bytes) -> bytes:
    """
    Section 2.1's first correction, un-applied: keep marker 0xE0 whatever its
    identifier is, so an APP0/JFXX thumbnail survives.
    """
    out = bytearray()
    for kind, code, start, end, payload in regions(data):
        if kind == TRAILER:
            continue
        if kind == MARKER and not (
                code == 0xE0
                or code in KEEP_BY_NUMBER
                or (code == 0xEE and payload.startswith(b"Adobe"))):
            continue
        out += data[start:end]
    return bytes(out)


def _broken_only_the_first_app1(data: bytes) -> bytes:
    """A stripper that finds "the" APP1 and stops looking."""
    out = bytearray()
    dropped = False
    for kind, code, start, end, _payload in regions(data):
        if kind == TRAILER:
            continue
        if kind == MARKER and code == 0xE1 and not dropped:
            dropped = True
            continue
        out += data[start:end]
    return bytes(out)


BROKEN = {
    "does no removal at all": _broken_no_removal,
    "copies the file from SOS onward, keeping the trailer": _broken_copy_from_sos,
    "keeps APP0 by marker number, so a JFXX thumbnail survives":
        _broken_keep_app0_by_number,
    "removes only the first APP1": _broken_only_the_first_app1,
}


@pytest.fixture
def mutation_inputs(rich_bytes, jfxx_bytes, multi_app1_bytes):
    return {
        "rich": rich_bytes[0],
        "jfxx": jfxx_bytes[0],
        "multi-app1": multi_app1_bytes[0],
    }


@pytest.mark.parametrize("label", sorted(BROKEN))
def test_the_structural_gate_rejects_every_broken_removal(label, mutation_inputs):
    """
    THE MUTATION CHECK, permanently in the suite rather than run once by hand.

    Every one of these is a real stripper shape and three of them are named in
    section 2.1 as failures it was corrected to describe. If the gate passes
    any of them it is not a gate, and every green result above is worthless.

    A broken stripper is required to be caught on at least one fixture, not on
    all three: "keeps APP0 by number" is genuinely harmless on a file that has
    no JFXX segment, and demanding otherwise would be asserting a falsehood.
    """
    breaker = BROKEN[label]
    caught = sorted(name for name, data in mutation_inputs.items()
                    if structural_violations(breaker(data)))
    assert caught, (
        "the structural gate accepted a stripper that %s. The gate is not a "
        "gate, and nothing else in this file means anything." % label)


def test_the_broken_strippers_are_not_caught_by_accident(mutation_inputs):
    """
    A control on the control.

    The gate must reject the broken strippers BECAUSE of what they left behind,
    not because they produced something unparseable. Each broken output has to
    still be a decodable JPEG showing the same picture, which is exactly what
    makes these leaks hard to notice in the field.
    """
    for name, data in sorted(mutation_inputs.items()):
        expected = pixels(data)
        for label, breaker in sorted(BROKEN.items()):
            out = breaker(data)
            assert pixels(out) == expected, (
                "on %s, the broken stripper that %s damaged the image, so the "
                "gate may be rejecting it for the wrong reason" % (name, label))


def test_the_real_engine_passes_the_gate_on_every_mutation_fixture(
        tmp_path, mutation_inputs):
    """The other side of the mutation check: only the real engine gets through."""
    for name, data in sorted(mutation_inputs.items()):
        path = write(tmp_path, name + ".jpg", data)
        out, _removed = scrub(path)
        assert structural_violations(out) == [], name
        assert pixels(out) == pixels(data), name


# ---------------------------------------------------------------------------
# 4. The image is not allowed to change
# ---------------------------------------------------------------------------

def test_the_decoded_pixels_are_byte_identical(rich):
    """Removal is not allowed to cost anything the user can see."""
    with open(rich[0], "rb") as handle:
        before = handle.read()
    out, _removed = scrub(rich[0])
    assert pixels(out) == pixels(before)


def test_the_compressed_scan_is_copied_through_untouched(rich):
    """
    Stronger than pixel equality, and the assertion that actually says "never
    re-encoded" rather than "re-encoded to something that happened to match".
    Two different quantization tables can decode to the same pixels; the same
    entropy-coded bytes cannot have been through an encoder.
    """
    with open(rich[0], "rb") as handle:
        before = handle.read()
    out, _removed = scrub(rich[0])
    assert len(scan_bytes(before)) > 0
    assert scan_bytes(out) == scan_bytes(before)


# ---------------------------------------------------------------------------
# 5. The two carriers the 2026-09-06 corrections were written for
# ---------------------------------------------------------------------------

def test_an_app0_jfxx_thumbnail_is_removed(tmp_path, jfxx_bytes):
    """
    Section 2.1's first correction. The sentinel lives inside the embedded
    thumbnail's own COM marker, so its absence means the whole embedded JPEG is
    gone rather than merely a tag that named it.
    """
    data, value = jfxx_bytes
    path = write(tmp_path, "jfxx.jpg", data)
    out, removed = scrub(path)
    assert value.encode() not in out, (
        "the JFXX thumbnail survived; the keep-list is keyed on the marker "
        "number rather than on the identifier")
    assert any("JFXX" in line for line in removed), removed
    assert structural_violations(out) == []
    assert pixels(out) == pixels(data)
    # The JFIF APP0 in the same file is on the keep-list and must NOT have been
    # taken with it. Removing both would satisfy the sentinel check for the
    # wrong reason.
    assert app0_jfif_segment(out) == app0_jfif_segment(data)
    assert app_identifiers(out, 0xE0) == [b"JFIF"]


def test_every_app1_segment_is_removed_not_just_the_first(tmp_path, multi_app1_bytes):
    """
    Section 2.1's second correction. Four APP1 segments carrying three distinct
    identifiers, which is what exiftool actually produces for an oversized XMP
    packet, measured 2026-09-06.
    """
    data, value = multi_app1_bytes
    path = write(tmp_path, "multi.jpg", data)
    out, removed = scrub(path)
    assert app_identifiers(out, 0xE1) == []
    assert value.encode() not in out
    assert b"http://ns.adobe.com/xmp/extension/" not in out
    assert b"<?xpacket" not in out
    reported = {line.split("'")[1] for line in removed if "'" in line}
    assert {"Exif", "http://ns.adobe.com/xap/1.0/",
            "http://ns.adobe.com/xmp/extension/"} <= reported, removed
    assert structural_violations(out) == []


def test_every_removable_carrier_in_the_rich_fixture_goes(rich):
    """The belt: the sentinel search, seeded from what the fixture knows."""
    path, values = rich
    out, removed = scrub(path)
    for name, value in values.items():
        assert value.encode() not in out, (
            "the %s sentinel survived the scrub" % name)
    assert b"8BIM" not in out, "the Photoshop APP13 block survived"
    assert b"Exif\x00\x00" not in out
    assert b"<?xpacket" not in out
    assert len(removed) >= 4, removed


# ---------------------------------------------------------------------------
# 6. The post-EOI trailer, the highest-severity item in section 2.1
# ---------------------------------------------------------------------------

@pytest.mark.parametrize("flavour", ["google", "samsung"])
def test_the_motion_photo_trailer_is_removed(tmp_path, motion_photos, flavour):
    """
    The regression guard tests/test_motion_photo.py was written to install, now
    asserted against the real engine rather than against a walker that file
    wrote for itself.

    The gate is structural because it has to be: section 3.3 measured exiftool
    silent on this carrier at every verbosity, so the residual scan can never
    build a needle for it and the oracle can never see it.
    """
    builder, photos = motion_photos
    photo = photos[flavour]
    path = write(tmp_path, photo.filename, photo.jpeg)

    assert len(photo.jpeg) > first_top_level_eoi(photo.jpeg), (
        "the fixture carries no trailer, so this test would assert nothing")

    out, removed = scrub(path)

    assert len(out) == first_top_level_eoi(out), (
        "%s: %d bytes survive after EOI"
        % (flavour, len(out) - first_top_level_eoi(out)))
    assert structural_violations(out) == []
    assert any("after EOI" in line for line in removed), removed

    # The belt: every carrier the fixture knows it wrote, on both sides.
    for needle in (builder.PHOTO_GPS_ASCII, builder.PHOTO_DESC_ASCII,
                   builder.PHOTO_GPS_NEEDLE, builder.VIDEO_GPS_ASCII,
                   builder.VIDEO_TITLE_ASCII, builder.XMP_SENTINEL,
                   b"ftyp", b"SEFH", builder.SAMSUNG_MARKER):
        assert out.count(needle) == 0, (
            "%s: %r survived the scrub" % (flavour, needle))
    assert builder.sef_extract(out) is None
    assert builder.google_declared_video_length(out) is None
    assert pixels(out) == pixels(photo.jpeg)


# What exiftool 13.29 reports for a JPEG stripped to the section 2.1 keep-list
# with the trailer DELIBERATELY retained, over and above the keep-list's own
# JFIF tags. MEASURED 2026-09-06 on the fixtures built here.
#
# This corrects a reading of section 3.3 that would have been easy to assert
# and is wrong: "reports zero tags and zero warnings" is true of the section's
# own fixture, which was an unlabelled MP4 append, and of the Google flavour
# here, where removing APP1 orphans the container directory. It is NOT true of
# the Samsung flavour, because a SEF trailer is discovered backward from the
# last six bytes of the file and needs nothing in any APPn segment. exiftool
# finds it and names it.
#
# Which does not rescue the oracle. It names the blob and stops: the video's
# GPS and title are in neither flavour's report, which is what the second half
# of this test measures and what makes the structural gate the only instrument
# that works here.
ORACLE_ON_A_RETAINED_TRAILER = {
    "google": frozenset(),
    "samsung": frozenset({"MakerNotes:EmbeddedVideoFile",
                          "MakerNotes:EmbeddedVideoType",
                          "MakerNotes:TimeStamp"}),
}


@pytest.mark.parametrize("flavour", ["google", "samsung"])
def test_the_oracle_cannot_see_what_is_inside_a_retained_trailer(
        tmp_path, motion_photos, flavour):
    """
    Why the assertion above has to be structural, measured rather than quoted.

    If this test fails, a newer exiftool has changed what it can see and
    section 3.3's reasoning needs revisiting. It must not be deleted quietly:
    the structural gate exists because of what is measured here.
    """
    builder, photos = motion_photos
    photo = photos[flavour]
    leaking = _broken_copy_from_sos(photo.jpeg)
    assert b"ftyp" in leaking, "the leaking output does not carry the trailer"
    path = write(tmp_path, "leaking_" + photo.filename, leaking)

    surplus = set(oracle_tags(path)) - set(ORACLE_ALLOW_JPEG_KEEPLIST)
    assert surplus == ORACLE_ON_A_RETAINED_TRAILER[flavour], surplus

    # The load-bearing half. Whatever exiftool names, it never says what is
    # inside: the video's coordinate and title are in the bytes and in no
    # reported value, so no needle for them can ever be constructed.
    metadata = oracle_metadata(path)
    for label, needle in (("GPS", builder.VIDEO_GPS_ASCII),
                          ("title", builder.VIDEO_TITLE_ASCII)):
        assert needle in leaking, "the fixture lost its video %s" % label
        assert not [key for key, value in metadata.items()
                    if needle.decode() in str(value)], (
            "exiftool now reports the trailer video's %s; the premise of the "
            "structural gate has changed" % label)

    assert structural_violations(leaking), (
        "and yet the structural gate sees it, which is the entire point")


# ---------------------------------------------------------------------------
# 7. The exiftool oracle, section 3.3
#
# ORACLE_ALLOW_JPEG is the empty set, and conftest states that as a measurement
# of the CURRENT exiftool engine, whose `-all=` removes APP0 outright. This
# engine keeps APP0/JFIF and APP14/Adobe because section 2.1's keep-list says
# to, so exiftool has something left to report and the empty set no longer
# describes correct output. conftest says so itself: "Phase 1 and Phase 2
# replace those engines. When they do, RE-MEASURE. Do not inherit these lists."
#
# MEASURED 2026-09-06, exiftool 13.29, on THIS engine's output:
#
#   APP0/JFIF kept, no APP14   -> JFIF:JFIFVersion, JFIF:ResolutionUnit,
#                                 JFIF:XResolution, JFIF:YResolution
#   APP14/Adobe kept, no APP0  -> APP14:DCTEncodeVersion, APP14:APP14Flags0,
#                                 APP14:APP14Flags1, APP14:ColorTransform
#   neither kept               -> nothing at all
#
# All eight are fixed-width numeric fields of the two segments the keep-list
# keeps, and none of them can hold a string, so none can carry identity. That
# is the same justification ORACLE_ALLOW_PNG gives for the seven IHDR fields.
#
# Trap 11 applies in full: every name here is a name nobody checks again, so
# the overcorrection tests land in this same commit. There are three, and they
# are the three tests immediately below.
# ---------------------------------------------------------------------------

ORACLE_ALLOW_JPEG_KEEPLIST = ORACLE_ALLOW_JPEG | frozenset({
    "JFIF:JFIFVersion",        # APP0/JFIF, two version bytes
    "JFIF:ResolutionUnit",     # APP0/JFIF, one unit byte, 0/1/2
    "JFIF:XResolution",        # APP0/JFIF, one 16-bit density
    "JFIF:YResolution",        # APP0/JFIF, one 16-bit density
    "APP14:DCTEncodeVersion",  # APP14/Adobe, 16-bit version, 100 in practice
    "APP14:APP14Flags0",       # APP14/Adobe, 16-bit flags
    "APP14:APP14Flags1",       # APP14/Adobe, 16-bit flags
    "APP14:ColorTransform",    # APP14/Adobe, one byte, 0/1/2
})


def test_the_oracle_sees_nothing_after_a_keep_list_scrub(rich):
    path, _values = rich
    scrub(path)
    assert_oracle_sees_nothing(
        path, allow=ORACLE_ALLOW_JPEG_KEEPLIST,
        note="pure-Python JPEG engine, section 2.1 keep-list")


def test_the_unwidened_allowlist_still_holds_where_it_was_measured(tmp_path):
    """
    OVERCORRECTION TEST 1, and the reason the widening above is honest.

    conftest's ORACLE_ALLOW_JPEG is the empty set. On a JPEG that carries
    neither of the two kept identifier segments, this engine's output must
    still satisfy it EXACTLY. If it did not, the widening would be covering for
    the engine rather than for the keep-list.
    """
    path = write(tmp_path, "withapp0.jpg", _plain())
    exiftool_or_fail().execute(
        "-EXIF:Artist=OVERCORRECTION-ONE", "-overwrite_original", path)
    with open(path, "rb") as handle:
        stamped = handle.read()
    bare = drop_markers(stamped, {0xE0})
    assert app_identifiers(bare, 0xE0) == []
    assert 0xEE not in marker_codes(bare)
    bare_path = write(tmp_path, "noapp0.jpg", bare)
    scrub(bare_path)
    assert_oracle_sees_nothing(
        bare_path, allow=ORACLE_ALLOW_JPEG,
        note="a JPEG with no APP0/JFIF and no APP14/Adobe; conftest's empty "
             "set must still describe this output exactly")


def test_the_widened_allowlist_does_not_hide_a_real_leak(rich):
    """
    OVERCORRECTION TEST 2. Trap 11: a name added to an allowlist is a name
    nobody checks again, so prove the widened list still fails on a leak.

    An EXIF Artist is written back onto the scrubbed output and the same
    assertion must now raise. Without this, an allowlist accidentally widened
    to everything would look exactly as green as a correct one.
    """
    path, _values = rich
    scrub(path)
    exiftool_or_fail().execute(
        "-EXIF:Artist=REINJECTED-LEAK", "-overwrite_original", path)
    with pytest.raises(AssertionError):
        assert_oracle_sees_nothing(path, allow=ORACLE_ALLOW_JPEG_KEEPLIST)


def test_the_widened_allowlist_is_exactly_the_two_kept_segments(rich, tmp_path):
    """
    OVERCORRECTION TEST 3. Every name in the widened list must be attributable
    to APP0/JFIF or APP14/Adobe, and nothing else may be in it.

    Measured by removing each kept segment and reading the difference, rather
    than by asserting the list against itself.
    """
    path, _values = rich
    scrub(path)
    with open(path, "rb") as handle:
        out = handle.read()
    from_jfif = set(oracle_tags(path))
    assert from_jfif == {"JFIF:JFIFVersion", "JFIF:ResolutionUnit",
                         "JFIF:XResolution", "JFIF:YResolution"}, from_jfif

    without_app0 = write(tmp_path, "nojfif.jpg", drop_markers(out, {0xE0}))
    assert oracle_tags(without_app0) == [], (
        "removing APP0/JFIF should leave the oracle with nothing at all to say")

    cmyk = write(tmp_path, "cmyk.jpg", _plain(mode="CMYK"))
    scrub(cmyk)
    from_app14 = set(oracle_tags(cmyk))
    assert from_app14 == {"APP14:DCTEncodeVersion", "APP14:APP14Flags0",
                          "APP14:APP14Flags1", "APP14:ColorTransform"}, from_app14

    assert from_jfif | from_app14 | ORACLE_ALLOW_JPEG == ORACLE_ALLOW_JPEG_KEEPLIST, (
        "the widened allowlist contains a name that neither kept segment "
        "produces, which means it is allowing something nobody measured")


def test_the_com_marker_is_invisible_to_the_oracle_and_removed_anyway(rich):
    """
    conftest names this blind spot; it is asserted here rather than trusted.

    A COM marker comes back from exiftool as `File:Comment`, and `File` is in
    scrubber._PSEUDO, so oracle_tags() never sees it. The oracle would report a
    JPEG full of comments as clean. Only the byte search and the marker
    inventory catch it, so both are asserted.
    """
    path, values = rich
    with open(path, "rb") as handle:
        before = handle.read()
    assert values["com"].encode() in before
    assert 0xFE in marker_codes(before)
    assert "File:Comment" not in oracle_tags(path), (
        "oracle_tags now surfaces the COM marker; conftest's note about this "
        "blind spot has changed")
    out, _removed = scrub(path)
    assert values["com"].encode() not in out
    assert 0xFE not in marker_codes(out)


# ---------------------------------------------------------------------------
# 8. OVERCORRECTION: every keep-list row, and the cost of getting it wrong
#
# A test that only proves removal would be satisfied by an engine that removed
# everything, so each keep-list row gets an assertion that it SURVIVED and,
# where a decoder can tell the difference, that its survival still matters.
# ---------------------------------------------------------------------------

def test_soi_eoi_dqt_dht_sof_and_sos_survive_and_the_file_still_decodes(rich):
    """SOI, EOI, DQT, DHT, SOF*, SOS: without any of them there is no image."""
    out, _removed = scrub(rich[0])
    codes = marker_codes(out)
    assert codes[0] == 0xD8, "SOI was removed"
    assert codes[-1] == 0xD9, "EOI was removed"
    assert 0xDB in codes, "DQT was removed; the image cannot be decoded"
    assert 0xC4 in codes, "DHT was removed; the image cannot be decoded"
    assert any(0xC0 <= code <= 0xCF and code not in (0xC4, 0xC8, 0xCC)
               for code in codes), "no SOF survived"
    assert 0xDA in codes, "SOS was removed; there is no scan"
    assert pixels(out)


def test_the_jfif_app0_survives_byte_identical(rich):
    """
    APP0/JFIF.

    Asserted structurally rather than through the decoder, on purpose. Measured
    2026-09-06: removing APP0/JFIF from an ordinary 3-component JPEG changes
    nothing Pillow or ffmpeg can see, and `exiftool -all=` removes it outright
    and the result still decodes. A pixel comparison here would therefore pass
    whether the engine kept it or not, which is a test that proves nothing. The
    row is a compatibility hedge for older decoders, so the honest assertion is
    that the bytes are still there and unchanged.
    """
    with open(rich[0], "rb") as handle:
        before = handle.read()
    out, _removed = scrub(rich[0])
    segment = app0_jfif_segment(out)
    assert segment is not None, "the JFIF APP0 was removed"
    assert segment == app0_jfif_segment(before)


def test_a_ycck_jpeg_still_decodes_correctly_because_app14_survives(tmp_path):
    """
    APP14/Adobe, and the one keep-list row where a decoder measurably disagrees.

    Section 2.1, corrected 2026-09-06: APP14 changes decoding only for YCCK, or
    for a 3-component image with no JFIF present, because libjpeg lets JFIF win
    when both are there. It is a no-op for transform=0 CMYK, and ffmpeg ignores
    it entirely.

    So the fixture is YCCK: Pillow's CMYK output, whose APP14 transform byte is
    patched from 0 to 2. The control at the end is what makes this a real test
    rather than a tautology, because it proves the pixels WOULD change if the
    engine dropped APP14.
    """
    data = _plain(mode="CMYK")
    assert 0xEE in marker_codes(data), "Pillow's CMYK path did not emit APP14"
    assert 0xE0 not in marker_codes(data), (
        "Pillow's CMYK path is supposed to emit APP14 and no APP0; if it now "
        "emits APP0 too then JFIF wins and this fixture no longer tests APP14")

    ycck = bytearray(data)
    for code, _start, end, payload in segments(data):
        if code == 0xEE and payload.startswith(b"Adobe"):
            ycck[end - 1] = 2          # transform: 0 (CMYK) -> 2 (YCCK)
    ycck = bytes(ycck)

    path = write(tmp_path, "ycck.jpg", ycck)
    out, _removed = scrub(path)
    assert pixels(out) == pixels(ycck), (
        "APP14 did not survive, or survived altered; a YCCK image decodes to "
        "different pixels without it")
    assert structural_violations(out) == []

    # THE CONTROL. Drop APP14 by hand and the pixels must change, or the
    # assertion above would pass on an engine that removed it.
    assert pixels(drop_markers(ycck, {0xEE})) != pixels(ycck), (
        "dropping APP14 no longer changes the decode of this fixture, so the "
        "assertion above is not measuring the keep-list row it claims to")


def test_a_plain_cmyk_jpeg_survives_intact(tmp_path):
    """
    The other half of the same row, and the half section 2.1 was corrected
    about: for transform=0 CMYK, APP14 is a no-op for the decoder. The engine
    must still keep it, and must not damage a 4-component image.
    """
    data = _plain(mode="CMYK")
    path = write(tmp_path, "cmyk.jpg", data)
    out, _removed = scrub(path)
    assert pixels(out) == pixels(data)
    assert 0xEE in marker_codes(out)
    assert structural_violations(out) == []


def test_a_progressive_jpeg_still_decodes_identically(tmp_path):
    """
    SOF2 and the many-SOS shape. Measured 2026-09-06: Pillow's progressive
    output carries ten SOS segments with DHT segments interleaved between them.
    A stripper that handled one SOS and then copied the tail would still
    produce something that decodes, so the scan count is asserted too.
    """
    data = _plain(progressive=True)
    scans = marker_codes(data).count(0xDA)
    assert scans > 1, "this Pillow build did not produce a multi-scan file"

    path = write(tmp_path, "progressive.jpg", data)
    exiftool_or_fail().execute(
        "-EXIF:Artist=PROGRESSIVE-OVERCORRECTION", "-overwrite_original", path)
    with open(path, "rb") as handle:
        stamped = handle.read()

    out, _removed = scrub(path)
    assert pixels(out) == pixels(stamped)
    assert marker_codes(out).count(0xDA) == scans
    assert 0xC2 in marker_codes(out), "SOF2 was removed"
    assert scan_bytes(out) == scan_bytes(stamped)
    assert structural_violations(out) == []


def test_dri_and_the_restart_markers_survive(tmp_path):
    """
    DRI and RST*. The restart markers live inside the entropy-coded run, so
    keeping them is really a claim that the run is copied through untouched;
    DRI is a header segment and can be dropped on its own, which would
    desynchronise every decoder at the first restart interval.
    """
    data = _plain(restart_marker_blocks=4)
    assert 0xDD in marker_codes(data), "this Pillow build did not emit DRI"
    assert b"\xff\xd0" in data, "no RST0 in the fixture"

    path = write(tmp_path, "restart.jpg", data)
    exiftool_or_fail().execute(
        "-EXIF:Artist=RESTART-OVERCORRECTION", "-overwrite_original", path)
    with open(path, "rb") as handle:
        stamped = handle.read()

    out, _removed = scrub(path)
    assert 0xDD in marker_codes(out), "DRI was removed"
    assert b"\xff\xd0" in out, "the restart markers were removed"
    assert scan_bytes(out) == scan_bytes(stamped)
    assert pixels(out) == pixels(stamped)
    assert structural_violations(out) == []


# ---------------------------------------------------------------------------
# 9. ICC, and everything else in section 2.1's removal table
# ---------------------------------------------------------------------------

def test_an_icc_profile_is_removed_unconditionally(tmp_path):
    """
    Section 2.1's judgement call, implemented as the section recommends.

    The proposed rule was to keep APP2/ICC_PROFILE when the profile bytes hash
    to a known standard profile. That does not work as written, measured
    2026-09-06: two sRGB profiles generated 1.2 seconds apart differ at exactly
    one byte, offset 35, the seconds field of the ICC header, so a raw byte
    hash rejects a profile it should accept.

    So ICC goes unconditionally, and the cost is real and deliberate: a Display
    P3 photo renders with wrong colour on a naive viewer afterwards. A colour
    shift is visible and recoverable; a custom identifying profile is neither.
    That trade is asserted here rather than left implicit, so removing it is a
    decision somebody has to make on purpose.
    """
    from PIL import Image, ImageCms

    profile = ImageCms.ImageCmsProfile(ImageCms.createProfile("sRGB")).tobytes()
    buffer = io.BytesIO()
    Image.new("RGB", (96, 72), (10, 120, 200)).save(
        buffer, format="JPEG", quality=90, icc_profile=profile)
    data = buffer.getvalue()
    assert b"ICC_PROFILE" in data, "Pillow did not embed the profile"
    assert profile in data, "the profile bytes are not in the fixture"

    path = write(tmp_path, "icc.jpg", data)
    out, removed = scrub(path)
    assert b"ICC_PROFILE" not in out
    assert profile not in out
    assert 0xE2 not in marker_codes(out), "the APP2 segment survived"
    assert any("ICC" in line for line in removed), removed
    assert structural_violations(out) == []
    # The pixels are unchanged: Pillow does not apply the profile on decode, so
    # what was lost is the instruction to a colour-managed viewer, not data.
    assert pixels(out) == pixels(data)


# Section 2.1's removal table, one row each, as synthetic segments carrying a
# sentinel. Synthetic because no file on this machine carries a real MPF index,
# a real C2PA manifest or a real Kodak Meta block; the phase 0 recon says the
# same and declines to guess at their contents. What is being asserted is the
# WHITELIST, which does not depend on knowing what is inside a segment: a
# marker not on the keep-list is removed whatever it holds, including one
# nobody has thought of yet, which is why the last row is an unassigned APPn.
VENDOR_SEGMENTS = {
    "APP2 MPF, used by burst and motion photos": (0xE2, b"MPF\x00"),
    "APP3 Meta/Kodak": (0xE3, b"Meta\x00\x00"),
    "APP4 vendor block": (0xE4, b"VENDOR4\x00"),
    "APP5 vendor block": (0xE5, b"VENDOR5\x00"),
    "APP11 JUMBF, where C2PA content credentials live": (0xEB, b"JP\x00\x00"),
    "APP12 Ducky": (0xEC, b"Ducky\x00"),
    "APP13 Photoshop, where IPTC lives": (0xED, b"Photoshop 3.0\x008BIM"),
    "APP14 that is NOT Adobe": (0xEE, b"NotAdobe\x00"),
    "APP15, unassigned and unknown": (0xEF, b"WHOKNOWS\x00"),
    "COM": (0xFE, b""),
}


# Pillow recognises an APP2 beginning "MPF\0" and warns that the synthetic
# index below is not a real MPO one, which it is not and does not need to be:
# the assertion is about the whitelist, not about MPF's contents. Filtered by
# message so any OTHER warning still surfaces.
@pytest.mark.filterwarnings("ignore:Image appears to be a malformed MPO file")
@pytest.mark.parametrize("label", sorted(VENDOR_SEGMENTS))
def test_every_other_marker_is_removed_whatever_it_holds(tmp_path, label):
    marker, prefix = VENDOR_SEGMENTS[label]
    value = sentinel("jpegenginevendor").encode()
    payload = prefix + value
    segment = (bytes([0xFF, marker]) + struct.pack(">H", len(payload) + 2) + payload)
    data = insert_after_leading_apps(_plain(), segment)
    assert value in data
    assert marker in marker_codes(data), (
        "%s: the fixture does not carry the segment it claims" % label)

    path = write(tmp_path, "vendor.jpg", data)
    out, removed = scrub(path)
    assert value not in out, "%s: survived the scrub" % label
    assert structural_violations(out) == []
    assert removed, "%s: the engine removed it without reporting it" % label
    assert pixels(out) == pixels(data)


# ---------------------------------------------------------------------------
# 10. Refusing, rather than half-rewriting
# ---------------------------------------------------------------------------

def _first_app1_start(data: bytes) -> int:
    for code, start, _end, _payload in segments(data):
        if code == 0xE1:
            return start
    raise AssertionError("no APP1 in this fixture")


MANGLERS = {
    "no SOI": lambda data: b"\x00\x00" + data[2:],
    "truncated mid-file": lambda data: data[:len(data) // 2],
    "segment length runs past the end of the file":
        lambda data: (data[:_first_app1_start(data)] + b"\xff\xe1\xff\xf0"
                      + data[_first_app1_start(data) + 4:]),
    "no EOI": lambda data: data[:first_top_level_eoi(data) - 2],
}


@pytest.mark.parametrize("label", sorted(MANGLERS))
def test_the_engine_refuses_a_file_it_cannot_walk(tmp_path, rich_bytes, label):
    """
    Traps 2 and 12: "could not read" and "carries nothing" must never share a
    representation. A file the walker cannot parse is refused, and refusing
    means leaving the input exactly as it was rather than writing a half
    rewrite over what may be the user's only copy.
    """
    broken = MANGLERS[label](rich_bytes[0])
    path = write(tmp_path, "broken.jpg", broken)
    with pytest.raises(EngineError):
        JpegEngine().strip_all(path)
    with open(path, "rb") as handle:
        assert handle.read() == broken, (
            "%s: the engine modified a file it could not parse" % label)
    assert not [name for name in os.listdir(str(tmp_path))
                if name.startswith(".metascrub-")], (
        "%s: a temporary file was left behind" % label)


def test_a_file_with_nothing_to_remove_is_left_alone(tmp_path):
    """
    An engine that rewrote a clean file byte-identically would still touch it
    for no reason, and the report must not claim a removal that did not happen.
    """
    data = _plain()
    assert structural_violations(data) == [], (
        "a plain Pillow JPEG is expected to already satisfy the keep-list")
    path = write(tmp_path, "clean.jpg", data)
    before = os.stat(path)
    out, removed = scrub(path)
    assert out == data
    assert os.stat(path).st_mtime == before.st_mtime
    assert removed == ["jpeg marker stream walked; nothing outside the "
                       "keep-list was present"], removed
