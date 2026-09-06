"""
The motion photo trailer: a JPEG that carries a second file after its EOI.

WHAT IS BEING GUARDED

Google Motion Photos and Samsung Motion Photos append a complete MP4 after the
JPEG end-of-image marker, and that MP4 carries its own GPS. A JPEG scrubber
that parses the header segments and then copies the remainder of the file from
SOS onward, which is the shape almost every hand-rolled JPEG scrubber has
because entropy-coded data cannot be walked segment-wise, removes the photo's
own EXIF and leaves the video and its location byte-for-byte intact. The file
opens everywhere, Pillow decodes identical pixels, and `exiftool -validate`
says OK.

WHY THE GATE HERE IS STRUCTURAL AND NOT A SENTINEL SEARCH

Measured 2026-09-06 and re-measured by this file: exiftool never reports the
trailer video's GPS at any verbosity, on either flavour. It names the blob
(MotionPhotoVideo, EmbeddedVideoFile) and stops. metascrub's residual byte scan
builds its needles out of what exiftool reported on the BASELINE read, so the
video's coordinate is never in the needle set and the scan is structurally
incapable of catching this leak. Same asymmetry as CLAUDE.md trap 10, where
settings.xml in an ODF package carries a printer name exiftool does not report.

So the gate is `test_no_bytes_survive_after_the_first_top_level_eoi`, a
structural assertion. The sentinel searches are the belt that proves the
structural assertion is pointed at the right thing.

STATUS: REGRESSION GUARD, NOT A BUG REPORT

The shipped tool scrubs JPEG through exiftool, and `exiftool -all=` does remove
post-EOI data including a trailer it does not recognise. That correct behaviour
is asserted here so that the planned in-house marker walker cannot land without
reproducing it.

THE FIXTURES ARE SYNTHETIC

No real Pixel or Samsung device media was available on the machine that wrote
this, and none was used. tests/fixtures/build_motion_photo.py names its sources
(the published Google Motion Photo format, and ProcessSamsung() in ExifTool
13.29's Samsung.pm for the SEF layout) and lists every difference from real
device output in DIFFERENCES_FROM_REAL, which is asserted below so a reader who
opens this file is told. In short: a 320x240 Pillow gradient rather than a
camera capture, a 1 second ffmpeg encode rather than a multi-megabyte one, no
MakerNotes, no MPF index, no depth map, two SEF fields rather than a dozen, and
no file that carries both conventions at once.
"""

from __future__ import annotations

import hashlib
import importlib.util
import io
import os
import shutil
import struct
import subprocess

import pytest

from metascrub import STATUS_SANITIZED, MetadataScrubber


def _load_motion_photo_builder():
    """
    Load tests/fixtures/build_motion_photo.py by path.

    By path rather than by package import for the same reason conftest loads
    the OLE2 and ODF builders that way: tests/fixtures/ has no __init__.py, and
    adding one would put fixture data on the import path where a module named
    like a stdlib module would shadow it.
    """
    location = os.path.join(
        os.path.dirname(os.path.abspath(__file__)), "fixtures", "build_motion_photo.py"
    )
    spec = importlib.util.spec_from_file_location("metascrub_motion_photo_fixtures", location)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


build = _load_motion_photo_builder()

HAVE_FFMPEG = shutil.which("ffmpeg") is not None
HAVE_EXIFTOOL = shutil.which("exiftool") is not None

FLAVOURS = ["google", "samsung"]


# ---------------------------------------------------------------------------
# Fixtures
#
# Built once for the session, into pytest's own tmp directory rather than into
# the repo, because building costs one ffmpeg encode. Every test works on the
# returned BYTES, so nothing can mutate what another test sees.
# ---------------------------------------------------------------------------

@pytest.fixture(scope="session")
def motion_photos(tmp_path_factory):
    if not HAVE_FFMPEG:
        pytest.skip("ffmpeg not available; cannot build the trailer video")
    workdir = str(tmp_path_factory.mktemp("motionphoto"))
    return build.build_all(workdir)


@pytest.fixture(scope="session")
def written(motion_photos, tmp_path_factory):
    """Each fixture written to disk once, for the tests that shell out."""
    directory = tmp_path_factory.mktemp("motionphoto-files")
    return {flavour: fixture.write(str(directory))
            for flavour, fixture in motion_photos.items()}


@pytest.fixture(params=FLAVOURS)
def photo(request, motion_photos):
    return motion_photos[request.param]


# ---------------------------------------------------------------------------
# JPEG walking
#
# These model the planned in-house marker walker. They are NOT imported from
# metascrub: the point of the file is to demonstrate the failure shape that a
# walker can have, and a demonstration that imports the implementation under
# test proves nothing about a walker that has not been written yet.
# ---------------------------------------------------------------------------

# Markers that carry no length field.
_STANDALONE = {0xD8, 0xD9, 0x01} | set(range(0xD0, 0xD8))
# APP1..APP15 and COM. APP0/JFIF is kept, as the removal table in
# docs/ANDROID-MEDIA-BUILD.md section 2.1 specifies.
_DROP = set(range(0xE1, 0xF0)) | {0xFE}


def walk_header_segments(data: bytes):
    """Yield (marker, start, end) for each header segment, stopping after SOS."""
    assert data[:2] == b"\xff\xd8", "not a JPEG"
    at = 2
    while at + 2 <= len(data):
        if data[at] != 0xFF:
            raise ValueError("marker desync at %d" % at)
        marker = data[at + 1]
        if marker in _STANDALONE:
            yield marker, at, at + 2
            at += 2
            continue
        length = struct.unpack(">H", data[at + 2:at + 4])[0]
        end = at + 2 + length
        yield marker, at, end
        at = end
        if marker == 0xDA:  # SOS, entropy coded data follows
            return


def first_top_level_eoi(data: bytes) -> int:
    """Index just past the first EOI belonging to the OUTER image.

    Deliberately not `data.index(b"\\xff\\xd9")`. An APPn segment can contain a
    complete JPEG of its own, an EXIF or JFXX thumbnail being the ordinary
    case, and that inner image ends with its own EOI. A search from byte zero
    finds the thumbnail's EOI, and a walker that truncated there would destroy
    the file while appearing to do the right thing. This searches from the end
    of SOS instead, where FF D9 cannot occur by accident: inside entropy-coded
    data FF is byte-stuffed as FF 00, and the only bare markers permitted there
    are RST0..RST7, which are FF D0 through FF D7.
    """
    sos_end = None
    for marker, _start, end in walk_header_segments(data):
        if marker == 0xDA:
            sos_end = end
    if sos_end is None:
        raise ValueError("no SOS in this JPEG")
    return data.index(b"\xff\xd9", sos_end) + 2


def naive_strip(data: bytes) -> bytes:
    """Drop APPn and COM, then copy the rest of the file from SOS onward.

    The leaking shape. Note that this is NOT the "parses to EOI and stops"
    walker: one that genuinely stopped at EOI would drop the trailer by
    accident and be safe. This one is the common one, and it is the one to
    guard against.
    """
    out = bytearray(b"\xff\xd8")
    for marker, start, end in walk_header_segments(data):
        if marker == 0xD8 or marker in _DROP:
            continue
        out += data[start:end]
        if marker == 0xDA:
            out += data[end:]  # the whole tail, trailer and all
            break
    return bytes(out)


def fixed_strip(data: bytes) -> bytes:
    """Identical to naive_strip except the copy stops at the first EOI."""
    out = bytearray(b"\xff\xd8")
    for marker, start, end in walk_header_segments(data):
        if marker == 0xD8 or marker in _DROP:
            continue
        out += data[start:end]
        if marker == 0xDA:
            out += data[end:first_top_level_eoi(data)]
            break
    return bytes(out)


def strip_app1_only(data: bytes) -> bytes:
    """Remove every APP1 segment and change nothing else.

    Not a scrubber. This isolates one operation, so that what happens to each
    trailer convention afterwards is attributable to that operation alone.
    """
    out = bytearray()
    at = 0
    for marker, start, end in walk_header_segments(data):
        if marker == 0xE1:
            out += data[at:start]
            at = end
        if marker == 0xDA:
            out += data[at:]
            return bytes(out)
    raise ValueError("no SOS in this JPEG")


# ---------------------------------------------------------------------------
# Needles
# ---------------------------------------------------------------------------

def photo_needles():
    """Carriers belonging to the still image. Any scrubber must remove these."""
    return [
        ("photo EXIF GPS, ASCII in the GPS IFD", build.PHOTO_GPS_ASCII),
        ("photo EXIF GPS, rational numerator bytes", build.PHOTO_GPS_NEEDLE),
        ("photo EXIF ImageDescription", build.PHOTO_DESC_ASCII),
    ]


def video_needles(photo):
    """Carriers belonging to the trailer video.

    The residual byte scan can never construct these on its own, because
    exiftool does not report them. They exist here only because the fixture
    knows independently what it wrote, which is the same reason ODF's
    settings.xml needs its own test.
    """
    needles = [
        ("video GPS, ISO6709 ASCII in the (c)xyz box", build.VIDEO_GPS_ASCII),
        ("video title and comment in the MP4 udta", build.VIDEO_TITLE_ASCII),
        ("MP4 ftyp box signature", b"ftyp"),
    ]
    if photo.loci_needle is not None:
        # ffmpeg's binary 16.16 fixed point encoding of the same coordinate,
        # read out of the built file rather than hardcoded. Kept alongside the
        # ASCII form because a fixture rebuilt on a different ffmpeg could
        # silently lose one of the two encodings.
        needles.append(("video GPS, 16.16 binary in the loci box", photo.loci_needle))
    if photo.flavour == "samsung":
        needles.append(("Samsung SEF field name", build.SAMSUNG_MARKER))
        needles.append(("Samsung SEF directory", b"SEFH"))
    else:
        needles.append(("Google XMP container packet", build.XMP_SENTINEL))
    return needles


def pixel_digest(data: bytes) -> str:
    from PIL import Image

    image = Image.open(io.BytesIO(data))
    image.load()
    return hashlib.sha256(image.tobytes()).hexdigest()


def exiftool_report(path: str, *extra: str) -> str:
    result = subprocess.run(
        ["exiftool", "-a", "-G1", "-s", *extra, path],
        capture_output=True, text=True,
    )
    return result.stdout


# ---------------------------------------------------------------------------
# 1. The fixture is what it claims to be
# ---------------------------------------------------------------------------

def test_the_fixture_carries_both_sentinel_families(photo):
    """
    A fixture that does not carry what it claims proves nothing.

    Both families must be present in the ORIGINAL bytes, or every removal
    assertion below would pass on a file that never had anything to remove.
    """
    for label, needle in photo_needles():
        assert photo.jpeg.count(needle) >= 1, (
            "%s: fixture does not carry the %s" % (photo.flavour, label))
    for label, needle in video_needles(photo):
        assert photo.jpeg.count(needle) >= 1, (
            "%s: fixture does not carry the %s" % (photo.flavour, label))


def test_the_fixture_is_a_still_image_plus_an_appended_trailer(photo):
    """The structure the whole file depends on: EOI, then a second file."""
    eoi = first_top_level_eoi(photo.jpeg)
    assert eoi == len(photo.still), "the still does not end where expected"
    assert photo.jpeg[eoi:] == photo.trailer
    assert len(photo.trailer) > 0, "there is no trailer to test"
    assert photo.video in photo.jpeg[eoi:], "the trailer does not contain the MP4"


def test_the_fixture_opens_and_decodes(photo):
    """It has to be a real image, or a viewer would never have shown it."""
    assert pixel_digest(photo.jpeg)


def test_the_differences_from_real_output_are_stated():
    """
    The builder must keep saying out loud that it is synthetic.

    Deleting that paragraph would turn a labelled approximation into an
    unlabelled claim about device behaviour, which is the failure this asserts
    against.
    """
    text = build.DIFFERENCES_FROM_REAL
    assert "synthetic" in text.lower()
    for expected in ("MakerNotes", "ffmpeg", "SEF", "sentinels"):
        assert expected in text, "the differences paragraph lost mention of " + expected


@pytest.mark.skipif(not HAVE_EXIFTOOL, reason="exiftool not available")
def test_exiftool_agrees_the_fixture_is_a_motion_photo(photo, written):
    """
    The independent oracle on fixture faithfulness.

    exiftool's own Google and Samsung handlers are asked to locate the trailer.
    If they do, the structure is faithful enough that a reference reader
    follows it, which is the strongest check available without device media. It
    is also what tells a guessed layout from an accepted one: an earlier
    Samsung attempt with a guessed record layout produced "Error processing
    Samsung trailer" here.
    """
    report = exiftool_report(written[photo.flavour])
    assert "Error processing" not in report, report
    if photo.flavour == "google":
        assert "MotionPhotoVideo" in report, report
        assert "DirectoryItemLength" in report, report
        # exiftool followed the container directory and measured the payload.
        assert str(len(photo.video)) in report, report
    else:
        assert "EmbeddedVideoType" in report, report
        assert "MotionPhoto_Data" in report, report
        assert "EmbeddedVideoFile" in report, report


# ---------------------------------------------------------------------------
# 2. Why the gate cannot be a needle search
# ---------------------------------------------------------------------------

@pytest.mark.skipif(not HAVE_EXIFTOOL, reason="exiftool not available")
def test_exiftool_never_reports_the_trailer_videos_gps(photo, written):
    """
    Measured 2026-09-06 on exiftool 13.29, and re-measured on every run.

    Zero mentions of the video's coordinate across three verbosities on both
    flavours. exiftool will tell you a blob is there; it will never tell you
    what is inside it.

    If this test ever FAILS, that is good news and a change of premise, not a
    defect: a newer exiftool would then surface the coordinate, the residual
    scan could finally make a needle from it, and the reasoning in
    docs/ANDROID-MEDIA-BUILD.md section 2.1 needs revisiting. It must not be
    deleted quietly, because the structural gate below exists because of it.
    """
    path = written[photo.flavour]
    coordinate = build.VIDEO_GPS_ASCII.decode()
    for extra in ([], ["-ee3"], ["-ee3", "-U"]):
        report = exiftool_report(path, *extra)
        assert coordinate not in report, (
            "exiftool now reports the trailer video's GPS with %s; the premise "
            "of the structural gate has changed" % (extra or "no extra flags"))


@pytest.mark.skipif(not HAVE_EXIFTOOL, reason="exiftool not available")
def test_the_residual_scan_cannot_make_a_needle_for_the_trailer(photo, written):
    """
    The same claim, asserted against this project's own code rather than
    against the exiftool command line.

    verify.meaningful_values() is what builds the needle set from the baseline
    read. The trailer video's coordinate is not in it, so no amount of
    searching afterwards could catch this leak. That is precisely why the gate
    is structural.
    """
    from metascrub import exif_io, verify

    read = exif_io.session().read(written[photo.flavour])
    values = verify.meaningful_values(read.metadata)
    coordinate = build.VIDEO_GPS_ASCII.decode()
    assert not any(coordinate in value for value in values), (
        "meaningful_values() now yields the trailer video's GPS; the residual "
        "scan can see this leak and the reasoning here needs revisiting")
    # The photo's own GPS IS in the needle set, which proves the assertion
    # above is a real asymmetry and not an empty needle set.
    assert any(build.PHOTO_GPS_ASCII.decode() in value for value in values), values


# ---------------------------------------------------------------------------
# 3. The trap is real
# ---------------------------------------------------------------------------

def test_a_naive_marker_strip_leaves_the_whole_trailer(photo):
    """
    The failure this file exists for.

    Every carrier belonging to the photo is gone. Every carrier belonging to
    the video survives, and the trailer is byte-for-byte what it was.
    """
    out = naive_strip(photo.jpeg)

    for label, needle in photo_needles():
        assert out.count(needle) == 0, (
            "%s: the naive strip left the %s" % (photo.flavour, label))

    removed = [label for label, needle in video_needles(photo)
               if out.count(needle) == 0]
    # XMP lives in APP1, so the Google packet is the one video-side carrier
    # this walker does remove. Everything else in the trailer survives.
    assert removed in ([], ["Google XMP container packet"]), (
        "%s: expected only the XMP to be removed, but also lost %s"
        % (photo.flavour, removed))

    eoi = first_top_level_eoi(out)
    assert out[eoi:] == photo.trailer, (
        "%s: expected the trailer to survive intact" % photo.flavour)
    assert build.VIDEO_GPS_ASCII in out[eoi:]


def test_the_leaked_trailer_is_still_a_complete_mp4(photo):
    """
    Not merely bytes: a usable video, with its location, recoverable by
    something that knows nothing about how it got there.
    """
    out = naive_strip(photo.jpeg)
    at = out.find(b"ftyp")
    assert at >= 4, "no MP4 signature in the leaking output"
    carved = out[at - 4:]
    if photo.flavour == "samsung":
        # The SEF directory sits after the video, so carving forward from ftyp
        # picks it up too. Recover the exact payload the way a reader would.
        fields = build.sef_extract(out)
        assert fields is not None
        carved = fields[build.SAMSUNG_MARKER]
    assert carved.startswith(photo.video[:64])
    assert build.VIDEO_GPS_ASCII in carved
    assert build.VIDEO_TITLE_ASCII in carved


def test_the_leaking_output_still_decodes_to_the_same_picture(photo):
    """
    Nothing about the leak is visible. The file opens and the pixels match, so
    no viewer, and no human looking at the file, has any reason to suspect it.
    """
    assert pixel_digest(naive_strip(photo.jpeg)) == pixel_digest(photo.jpeg)


@pytest.mark.skipif(not HAVE_EXIFTOOL, reason="exiftool not available")
def test_the_leaking_output_passes_exiftool_validate(photo, tmp_path):
    """The leak does not even look like damage to the strictest ordinary check."""
    path = str(tmp_path / ("naive_" + photo.filename))
    with open(path, "wb") as handle:
        handle.write(naive_strip(photo.jpeg))
    result = subprocess.run(["exiftool", "-validate", "-warning", "-a", path],
                            capture_output=True, text=True)
    assert "Validate" in result.stdout and "OK" in result.stdout, result.stdout


# ---------------------------------------------------------------------------
# 4. The fix, and the structural gate
# ---------------------------------------------------------------------------

def test_truncating_at_eoi_removes_every_trace_of_the_trailer(photo):
    """One line of difference from naive_strip, and every needle goes to zero."""
    out = fixed_strip(photo.jpeg)
    for label, needle in photo_needles() + video_needles(photo):
        assert out.count(needle) == 0, (
            "%s: %s survived the truncating strip" % (photo.flavour, label))


def test_truncating_at_eoi_leaves_the_picture_identical(photo):
    """
    Removal is not allowed to cost anything. Pillow must open the result and
    decode exactly the same pixels, or the fix would be trading a leak for
    damage.
    """
    out = fixed_strip(photo.jpeg)
    assert pixel_digest(out) == pixel_digest(photo.jpeg)
    assert out.endswith(b"\xff\xd9")


def test_no_bytes_survive_after_the_first_top_level_eoi(photo):
    """
    THE GATE.

    Structural, not a needle search, because exiftool never surfaces the
    trailer's contents and so the residual scan can never build a needle for
    them. This assertion holds regardless of which convention built the
    trailer, which is exactly why it is the right instrument: a
    convention-aware rule that found the Google XMP and followed its offset
    would miss the Samsung SEF, and both would miss the next convention.
    """
    out = fixed_strip(photo.jpeg)
    assert len(out) == first_top_level_eoi(out), (
        "%s: %d bytes survive after EOI" % (
            photo.flavour, len(out) - first_top_level_eoi(out)))

    # The same assertion must FAIL on the input, or it is measuring nothing.
    assert len(photo.jpeg) > first_top_level_eoi(photo.jpeg), (
        "the fixture carries no trailer, so the gate above is vacuous")


def test_the_gate_is_not_fooled_by_an_eoi_inside_a_segment(photo):
    """
    A boundary case that would turn the gate into a file-destroying bug.

    An APP0/JFXX segment carries a complete embedded JPEG thumbnail, which ends
    with its own EOI. A gate written as `data.index(b"\\xff\\xd9")` would find
    that inner marker, declare the file to have thousands of trailing bytes,
    and a walker built on it would truncate the real image away. The helper
    walks to SOS first, so it is immune.
    """
    thumbnail = photo.still
    payload = b"JFXX\x00\x10" + thumbnail
    assert len(payload) + 2 <= 0xFFFF, "thumbnail too large for one segment"
    segment = b"\xff\xe0" + struct.pack(">H", len(payload) + 2) + payload
    poisoned = build.insert_after_leading_apps(photo.jpeg, segment)

    naive_index = poisoned.index(b"\xff\xd9") + 2
    real_index = first_top_level_eoi(poisoned)
    assert naive_index < real_index, (
        "the poisoned fixture does not actually carry an inner EOI first")
    assert real_index == first_top_level_eoi(photo.jpeg) + len(segment)
    # And the gate still reports the trailer correctly on the poisoned file.
    assert len(poisoned) - real_index == len(photo.trailer)


# ---------------------------------------------------------------------------
# 5. The Samsung asymmetry
# ---------------------------------------------------------------------------

def test_removing_app1_orphans_the_google_trailer(motion_photos):
    """
    Google discovery runs FORWARD from the XMP in APP1. Remove APP1 and the
    payload survives while nothing left in the file points at it.

    This cuts against intuition and it is worse for an auditor, not better: the
    bytes are still there and the spec's own reader can no longer find them.
    """
    photo = motion_photos["google"]
    assert build.google_declared_video_length(photo.jpeg) == len(photo.video)

    out = strip_app1_only(photo.jpeg)

    assert build.XMP_SENTINEL not in out, "APP1 removal did not remove the XMP"
    assert build.google_declared_video_length(out) is None, (
        "the container directory survived APP1 removal")
    assert b"Camera:MotionPhoto" not in out
    # ... and yet the video, with its GPS, is untouched.
    assert photo.video in out
    assert build.VIDEO_GPS_ASCII in out
    # Nothing else describes it either: there is no SEF footer to fall back on.
    assert build.sef_extract(out) is None


def test_removing_app1_leaves_the_samsung_trailer_self_describing(motion_photos):
    """
    Samsung discovery runs BACKWARD from the last six bytes of the file. The
    SEF directory depends on nothing in any APPn segment, so removing APP1
    leaves the trailer fully indexed and machine-readable.

    The extraction below is the proof: it reads the trailer the way a reader
    does, from the end, and recovers the video byte-for-byte.
    """
    photo = motion_photos["samsung"]
    out = strip_app1_only(photo.jpeg)

    assert build.PHOTO_GPS_ASCII not in out, "APP1 removal did not remove the EXIF"
    assert out[-4:] == b"SEFT", "the SEF footer did not survive"

    fields = build.sef_extract(out)
    assert fields is not None, "the SEF trailer became undiscoverable"
    assert build.SAMSUNG_MARKER in fields
    assert fields[build.SAMSUNG_MARKER] == photo.video, (
        "the recovered video is not byte-identical to the one embedded")
    assert build.VIDEO_GPS_ASCII in fields[build.SAMSUNG_MARKER]


@pytest.mark.skipif(not HAVE_EXIFTOOL, reason="exiftool not available")
def test_exiftool_still_finds_the_samsung_trailer_after_app1_removal(
        motion_photos, tmp_path):
    """The asymmetry, confirmed by an independent reader rather than by ours."""
    photo = motion_photos["samsung"]
    path = str(tmp_path / "app1_removed.jpg")
    with open(path, "wb") as handle:
        handle.write(strip_app1_only(photo.jpeg))
    report = exiftool_report(path)
    assert "MotionPhoto_Data" in report, report
    assert "PHOTOGPS" not in report, "the photo EXIF should be gone"


# ---------------------------------------------------------------------------
# 6. The shipped tool: current, correct behaviour
# ---------------------------------------------------------------------------

@pytest.mark.skipif(not HAVE_EXIFTOOL, reason="exiftool is required for every format")
def test_the_shipped_tool_removes_the_trailer_today(photo, tmp_path):
    """
    A REGRESSION GUARD, not a bug report.

    metascrub scrubs JPEG through exiftool, and `exiftool -all=` removes all
    post-EOI data including a trailer it does not recognise. That was measured
    2026-09-06 on three fixtures, the Google and Samsung flavours rebuilt here
    plus a bare unlabelled MP4 append; the two rebuilt here are re-measured on
    every run. This asserts the behaviour so that the planned in-house marker
    walker cannot replace it with the leaking shape demonstrated above without
    the suite going red.
    """
    path = str(tmp_path / photo.filename)
    with open(path, "wb") as handle:
        handle.write(photo.jpeg)
    assert len(photo.jpeg) > first_top_level_eoi(photo.jpeg), (
        "the input carries no trailer, so this test would assert nothing")

    with MetadataScrubber(backup=False) as scrubber:
        result = scrubber.sanitize_file(path, remove_all=True)
    assert result["status"] == STATUS_SANITIZED, result.get("error")

    with open(path, "rb") as handle:
        out = handle.read()

    # The gate, applied to the real tool.
    assert len(out) == first_top_level_eoi(out), (
        "%s: metascrub left %d bytes after EOI" % (
            photo.flavour, len(out) - first_top_level_eoi(out)))
    # The belt.
    for label, needle in photo_needles() + video_needles(photo):
        assert out.count(needle) == 0, (
            "%s: metascrub left the %s" % (photo.flavour, label))
    # And it is still the same picture.
    assert pixel_digest(out) == pixel_digest(photo.jpeg)
