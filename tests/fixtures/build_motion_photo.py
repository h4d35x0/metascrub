"""
Build SYNTHETIC Google-style and Samsung-style motion photo fixtures.

WHAT THIS IS FOR

Google Motion Photos and Samsung Motion Photos append a complete MP4 after the
JPEG end-of-image marker. That MP4 carries its own GPS. A JPEG scrubber that
parses the header segments and then copies the remainder of the file from SOS
onward removes the photo's own EXIF and leaves the video, with its location,
completely intact. The file still opens everywhere.

Worse, and this is what makes the fixture necessary: exiftool never reports the
trailer video's GPS at any verbosity. It names the blob (MotionPhotoVideo,
EmbeddedVideoFile) and stops. So metascrub's residual byte scan, which builds
its needles out of what exiftool reported on the BASELINE read, can never make a
needle for it. Same asymmetry as CLAUDE.md trap 10 (settings.xml in ODF). The
gate therefore has to be a structural assertion, zero bytes after the first
top-level EOI, and the sentinels below are the belt that proves the structural
assertion is pointed at the right thing.

THESE FIXTURES ARE SYNTHETIC

No real Pixel or Samsung device media was available on the machine that wrote
this, and none was used. See DIFFERENCES_FROM_REAL, which is repeated in
tests/test_motion_photo.py so a reader of the tests sees it too. Every
structural claim made here is sourced:

  - Google Motion Photo XMP: https://developer.android.com/media/platform/
    motion-photo-format (namespaces, attribute names, the Container directory).
  - Samsung SEF trailer: ProcessSamsung() in ExifTool 13.29,
    exiftool_files/lib/Image/ExifTool/Samsung.pm lines 1578-1700. Read, not
    guessed. A first attempt with a guessed record layout produced
    "Warning : [minor] Error processing Samsung trailer", which is how the
    current layout is known to be one a real parser accepts.
  - The Android geo box: MPEG4Writer::writeGeoDataBox writes an ASCII ISO 6709
    string in a "\\xA9xyz" box. RESEARCHED from the Android source convention.

DEPENDENCIES

Pillow and ffmpeg only. The EXIF APP1 is assembled here by hand rather than
through piexif, which the prior measurement used, because piexif is not a
dependency of this project and a fixture builder that needs one more package
than the tool does is a fixture builder that silently skips on CI.

Usage:  python tests/fixtures/build_motion_photo.py [outdir]
"""

from __future__ import annotations

import os
import struct
import subprocess
import sys

# ---------------------------------------------------------------------------
# Sentinels. Each is unique and improbable, so a byte search cannot false-hit.
# The project rule is that no test asks an engine whether it succeeded; it
# searches the OUTPUT BYTES for the exact value the fixture put in.
# ---------------------------------------------------------------------------

PHOTO_GPS_ASCII = b"PHOTOGPS-A1B2C3D4E5F6-SENTINEL"       # EXIF GPS IFD
PHOTO_DESC_ASCII = b"PHOTODESC-1122334455-SENTINEL"        # EXIF IFD0
XMP_SENTINEL = b"XMPCONTAINER-DEADBEEFCAFE-SENTINEL"       # the XMP packet
VIDEO_GPS_ASCII = b"+11.2233-044.5566/"                    # MP4 udta (c)xyz
VIDEO_TITLE_ASCII = b"VIDEOTITLE-9F8E7D6C5B4A-SENTINEL"    # MP4 udta ilst

# A distinctive GPS rational: the third latitude component's numerator. Encoded
# big-endian in the EXIF TIFF stream this is a 4 byte needle no ordinary photo
# would carry, and unlike the ASCII sentinels it proves the binary TIFF stream
# itself is gone rather than just a string that happened to sit beside it.
PHOTO_GPS_RATIONAL_NUM = 918273645
PHOTO_GPS_RATIONAL_DEN = 10000000
PHOTO_GPS_NEEDLE = struct.pack(">I", PHOTO_GPS_RATIONAL_NUM)

SAMSUNG_MARKER = b"MotionPhoto_Data"

DIFFERENCES_FROM_REAL = """\
Differences between these synthetic fixtures and real device output:
 1. The JPEG is a 320x240 Pillow gradient, not a camera capture. No MakerNotes,
    no Google APP4 "Device" block, no depth map, no APP2 MPF index.
 2. The MP4 is a 1 second ffmpeg testsrc encode of a few kilobytes, not a 3
    second 1080p capture of a few megabytes. No audio track.
 3. Only the Camera and Container XMP namespaces are written. Real Pixel output
    usually also carries GDepth, GImage and Profile.
 4. The Samsung fixture carries two SEF fields (MotionPhoto_Data and
    Image_UTC_Data). Real Samsung files carry several more, such as
    DualShot_Meta_Info, Front_Cam_Selfie_Info and Burst_Shot_Info.
 5. The Samsung fixture carries NO Google XMP. Modern One UI devices are
    reported to write both conventions in one file; that combined case is not
    built here.
 6. GPS values are sentinels, not plausible coordinates.
 7. The EXIF is a hand-built minimal TIFF stream: IFD0 plus a GPS IFD, no
    Exif SubIFD, no thumbnail IFD, no maker notes.
"""

# ---------------------------------------------------------------------------
# EXIF, assembled by hand
# ---------------------------------------------------------------------------

# TIFF field types used here.
_BYTE, _ASCII, _SHORT, _LONG, _RATIONAL, _UNDEFINED = 1, 2, 3, 4, 5, 7
_TYPE_SIZE = {_BYTE: 1, _ASCII: 1, _SHORT: 2, _LONG: 4, _RATIONAL: 8, _UNDEFINED: 1}


def _ifd(entries, ifd_offset):
    """Serialise one TIFF IFD.

    entries is a list of (tag, type, count, payload) sorted ascending by tag,
    which the TIFF specification requires. Returns (ifd_bytes, data_bytes); the
    data area is written immediately after the IFD, and any payload longer than
    four bytes is placed there and referenced by an offset measured from the
    start of the TIFF header.
    """
    ifd_size = 2 + 12 * len(entries) + 4
    data = bytearray()
    out = bytearray(struct.pack(">H", len(entries)))
    for tag, typ, count, payload in entries:
        assert len(payload) == _TYPE_SIZE[typ] * count, (tag, len(payload))
        if len(payload) <= 4:
            field = payload + b"\x00" * (4 - len(payload))
        else:
            field = struct.pack(">I", ifd_offset + ifd_size + len(data))
            data += payload
            if len(data) % 2:
                data += b"\x00"  # IFD values are word aligned
        out += struct.pack(">HHI", tag, typ, count) + field
    out += struct.pack(">I", 0)  # no next IFD
    return bytes(out), bytes(data)


def _rationals(pairs):
    return b"".join(struct.pack(">II", n, d) for n, d in pairs)


def exif_app1_segment() -> bytes:
    """A complete APP1 EXIF segment carrying both photo sentinels."""
    gps_entries = [
        (0x0000, _BYTE, 4, b"\x02\x03\x00\x00"),           # GPSVersionID
        (0x0001, _ASCII, 2, b"N\x00"),                      # GPSLatitudeRef
        (0x0002, _RATIONAL, 3, _rationals([
            (47, 1), (37, 1),
            (PHOTO_GPS_RATIONAL_NUM, PHOTO_GPS_RATIONAL_DEN),
        ])),
        (0x0003, _ASCII, 2, b"W\x00"),                      # GPSLongitudeRef
        (0x0004, _RATIONAL, 3, _rationals([
            (122, 1), (20, 1), (123456789, 10000000),
        ])),
        # GPSAreaInformation is UNDEFINED: an 8 byte character-code prefix and
        # then free bytes. Real cameras rarely fill it, which is exactly why it
        # is a clean place to park an ASCII needle that provably lives inside
        # the GPS IFD and nowhere else.
        (0x001C, _UNDEFINED, 8 + len(PHOTO_GPS_ASCII),
         b"ASCII\x00\x00\x00" + PHOTO_GPS_ASCII),
    ]

    def ifd0(gps_offset):
        return [
            (0x010E, _ASCII, len(PHOTO_DESC_ASCII) + 1, PHOTO_DESC_ASCII + b"\x00"),
            (0x010F, _ASCII, 15, b"SyntheticPixel\x00"),
            (0x0110, _ASCII, 14, b"FixtureCam MP\x00"),
            (0x0131, _ASCII, 26, b"metascrub-fixture-builder\x00"),
            # Required by the JPEG profile of TIFF. Without it exiftool
            # -validate reports a warning, and a fixture that is already
            # slightly invalid muddies any later measurement of whether the
            # tool damaged it.
            (0x0213, _SHORT, 1, struct.pack(">H", 1)),
            (0x8825, _LONG, 1, struct.pack(">I", gps_offset)),
        ]

    # Two passes. The GPS pointer is an inline four byte value, so it changes
    # no size; the first pass exists only to learn where the GPS IFD lands.
    head, body = _ifd(ifd0(0), 8)
    gps_offset = 8 + len(head) + len(body)
    head, body = _ifd(ifd0(gps_offset), 8)
    gps_head, gps_body = _ifd(gps_entries, gps_offset)

    tiff = b"MM\x00\x2a" + struct.pack(">I", 8) + head + body + gps_head + gps_body
    payload = b"Exif\x00\x00" + tiff
    return b"\xff\xe1" + struct.pack(">H", len(payload) + 2) + payload


# ---------------------------------------------------------------------------
# JPEG assembly
# ---------------------------------------------------------------------------

def build_base_jpeg(path: str) -> None:
    from PIL import Image

    img = Image.new("RGB", (320, 240))
    px = img.load()
    for y in range(240):
        for x in range(320):
            px[x, y] = (x % 256, y % 256, (x + y) % 256)
    img.save(path, "JPEG", quality=90)


def insert_after_leading_apps(jpeg: bytes, segment: bytes) -> bytes:
    """Insert a segment after the run of APPn/COM segments at the head."""
    i = 2
    while i + 4 <= len(jpeg):
        assert jpeg[i] == 0xFF, "not at a marker"
        marker = jpeg[i + 1]
        if 0xE0 <= marker <= 0xEF or marker == 0xFE:  # APPn or COM
            i += 2 + struct.unpack(">H", jpeg[i + 2:i + 4])[0]
            continue
        break
    return jpeg[:i] + segment + jpeg[i:]


XMP_NS_SIG = b"http://ns.adobe.com/xap/1.0/\x00"


def xmp_app1_segment(packet: bytes) -> bytes:
    payload = XMP_NS_SIG + packet
    return b"\xff\xe1" + struct.pack(">H", len(payload) + 2) + payload


def google_xmp_packet(video_len: int) -> bytes:
    """Google Motion Photo v1 XMP.

    Per developer.android.com/media/platform/motion-photo-format. The
    namespaces and attribute names are as specified there, which is why
    exiftool's own Google Motion Photo handler accepts the result and reports
    MotionPhoto=1 together with a DirectoryItemLength.
    """
    return (
        # The XMP packet header carries a UTF-8 BOM, written as an escape so
        # this source file stays pure ASCII.
        '<?xpacket begin="\ufeff" id="W5M0MpCehiHzreSzNTczkc9d"?>'
        '<x:xmpmeta xmlns:x="adobe:ns:meta/" x:xmptk="metascrub-fixture">'
        '<rdf:RDF xmlns:rdf="http://www.w3.org/1999/02/22-rdf-syntax-ns#">'
        '<rdf:Description rdf:about=""'
        ' xmlns:Camera="http://ns.google.com/photos/1.0/camera/"'
        ' xmlns:Container="http://ns.google.com/photos/1.0/container/"'
        ' xmlns:Item="http://ns.google.com/photos/1.0/container/item/"'
        ' xmlns:dc="http://purl.org/dc/elements/1.1/"'
        ' Camera:MotionPhoto="1"'
        ' Camera:MotionPhotoVersion="1"'
        ' Camera:MotionPhotoPresentationTimestampUs="500000">'
        "<dc:description>" + XMP_SENTINEL.decode() + "</dc:description>"
        "<Container:Directory><rdf:Seq>"
        '<rdf:li rdf:parseType="Resource">'
        '<Container:Item Item:Mime="image/jpeg" Item:Semantic="Primary" Item:Padding="0"/>'
        "</rdf:li>"
        '<rdf:li rdf:parseType="Resource">'
        '<Container:Item Item:Mime="video/mp4" Item:Semantic="MotionPhoto"'
        ' Item:Length="' + str(video_len) + '" Item:Padding="0"/>'
        "</rdf:li>"
        "</rdf:Seq></Container:Directory>"
        "</rdf:Description></rdf:RDF></x:xmpmeta>"
        '<?xpacket end="w"?>'
    ).encode("utf-8")


def google_declared_video_length(jpeg: bytes):
    """The Item:Length the XMP declares for the MotionPhoto item, or None.

    This is the Google convention's ONLY pointer at the trailer: discovery runs
    forward from the XMP in APP1. Remove APP1 and this returns None, while the
    trailer bytes are still sitting in the file. That is the orphaning half of
    the asymmetry test.
    """
    key = b'Item:Semantic="MotionPhoto"'
    at = jpeg.find(key)
    if at < 0:
        return None
    tail = jpeg[at:at + 200]
    marker = b'Item:Length="'
    start = tail.find(marker)
    if start < 0:
        return None
    start += len(marker)
    end = tail.find(b'"', start)
    return int(tail[start:end])


# ---------------------------------------------------------------------------
# The trailer video
# ---------------------------------------------------------------------------

def _inject_xyz_box(mp4: bytes, iso6709: bytes) -> bytes:
    """Append a QuickTime/Android "(c)xyz" geo box to the top level udta.

    ffmpeg's mp4 muxer writes GPS as a 3GPP "loci" box with BINARY 16.16 fixed
    point coordinates. Real Android does NOT: MPEG4Writer::writeGeoDataBox
    writes a "\\xA9xyz" box holding an ASCII ISO 6709 string. Both are injected
    here so the fixture matches device output and so there is a clean ASCII
    needle. Written with moov LAST so growing it cannot invalidate any stco
    chunk offset, which all point into the preceding mdat.
    """
    payload = struct.pack(">HH", len(iso6709), 0x15C7) + iso6709
    box = struct.pack(">I", 8 + len(payload)) + b"\xa9xyz" + payload

    pos = 0
    moov = None
    while pos + 8 <= len(mp4):
        size = struct.unpack(">I", mp4[pos:pos + 4])[0]
        if mp4[pos + 4:pos + 8] == b"moov":
            moov = (pos, size)
            break
        if size < 8:
            raise ValueError("bad box size at %d" % pos)
        pos += size
    if moov is None:
        raise ValueError("no moov")
    moov_pos, moov_size = moov
    if moov_pos + moov_size != len(mp4):
        raise ValueError("moov is not last; injection would shift mdat")

    pos = moov_pos + 8
    end = moov_pos + moov_size
    udta = None
    while pos + 8 <= end:
        size = struct.unpack(">I", mp4[pos:pos + 4])[0]
        if mp4[pos + 4:pos + 8] == b"udta":
            udta = (pos, size)
            break
        if size < 8:
            raise ValueError("bad box size at %d" % pos)
        pos += size
    if udta is None:
        raise ValueError("no udta")
    udta_pos, udta_size = udta

    out = bytearray(mp4)
    out[udta_pos + udta_size:udta_pos + udta_size] = box          # insert
    struct.pack_into(">I", out, udta_pos, udta_size + len(box))   # grow udta
    struct.pack_into(">I", out, moov_pos, moov_size + len(box))   # grow moov
    return bytes(out)


def find_loci_payload(mp4: bytes):
    """Return the bytes of ffmpeg's "loci" box payload, or None.

    Measured 2026-09-06 on ffmpeg 2024-12-11-git-a518b5540d, the longitude
    inside this box begins ff d3 71 83, the 16.16 fixed point encoding of the
    same coordinate the ASCII (c)xyz box carries. It is read out of the built
    file rather than hardcoded, because a different ffmpeg could encode it
    differently and a hardcoded needle would then fail for the wrong reason.
    """
    at = mp4.find(b"loci")
    if at < 4:
        return None
    size = struct.unpack(">I", mp4[at - 4:at])[0]
    if size < 8 or at - 4 + size > len(mp4):
        return None
    return mp4[at + 4:at - 4 + size]


def build_mp4(path: str) -> None:
    """1 second MP4 carrying its own GPS location and a title, in udta."""
    if os.path.exists(path):
        os.remove(path)
    command = [
        "ffmpeg", "-hide_banner", "-loglevel", "error",
        "-f", "lavfi", "-i", "testsrc=size=320x240:rate=15:duration=1",
        "-c:v", "libx264", "-pix_fmt", "yuv420p", "-t", "1",
        "-metadata", "location=" + VIDEO_GPS_ASCII.decode(),
        "-metadata", "location-eng=" + VIDEO_GPS_ASCII.decode(),
        "-metadata", "title=" + VIDEO_TITLE_ASCII.decode(),
        "-metadata", "comment=" + VIDEO_TITLE_ASCII.decode(),
        path,
    ]
    subprocess.run(command, check=True, capture_output=True)
    with open(path, "rb") as fh:
        data = fh.read()
    data = _inject_xyz_box(data, VIDEO_GPS_ASCII)
    with open(path, "wb") as fh:
        fh.write(data)


# ---------------------------------------------------------------------------
# Samsung SEF
#
# Layout taken from the authoritative reader, ProcessSamsung() in ExifTool
# 13.29, exiftool_files/lib/Image/ExifTool/Samsung.pm lines 1578-1700:
#
#   <complete JPEG ending in FF D9>
#   <SEF block>*      each: 00 00 <u16 type LE> <u32 name_len> <name> <data>
#   b"SEFH" <u32 version=106> <u32 count> <12 byte entry>*
#   <u32 length of the SEFH body> b"SEFT"
#
#   entry: 00 00 <u16 type LE> <u32 noff> <u32 size>
#     noff = bytes from the start of "SEFH" back to the start of the block
#     size = 8 + name_len + len(data)
#
# Block type 0x0a30 with the name "MotionPhoto_Data" is the embedded video.
# The direction matters and is the point of the asymmetry test: SEF is
# discovered BACKWARD from the last six bytes of the file, so nothing it needs
# lives in an APPn segment and removing APP1 leaves it fully self-describing.
# ---------------------------------------------------------------------------

def sef_block(type_id: int, name: bytes, data: bytes) -> bytes:
    return struct.pack("<HHI", 0, type_id, len(name)) + name + data


def sef_trailer(blocks) -> bytes:
    """blocks is a list of (type_id, name, payload)."""
    data_area = b""
    placed = []
    for type_id, name, payload in blocks:
        blob = sef_block(type_id, name, payload)
        placed.append((type_id, len(data_area), len(blob)))
        data_area += blob
    total = len(data_area)
    entries = b""
    for type_id, offset, size in placed:
        # noff counts BACKWARD from the start of "SEFH" to the block start.
        entries += struct.pack("<HHII", 0, type_id, total - offset, size)
    body = b"SEFH" + struct.pack("<II", 106, len(placed)) + entries
    return data_area + body + struct.pack("<I", len(body)) + b"SEFT"


def sef_extract(data: bytes):
    """Read a SEF trailer BACKWARD from the end of the file.

    Returns {name: payload}, or None when the file carries no SEF trailer.
    This is a reader, not a writer, and it is what proves the Samsung trailer
    stays discoverable after every APP1 segment is removed: it uses only
    information that lives at the end of the file.
    """
    if len(data) < 12 or data[-4:] != b"SEFT":
        return None
    body_length = struct.unpack("<I", data[-8:-4])[0]
    start = len(data) - 8 - body_length
    if start < 0 or data[start:start + 4] != b"SEFH":
        return None
    _version, count = struct.unpack("<II", data[start + 4:start + 12])
    found = {}
    for index in range(count):
        at = start + 12 + 12 * index
        _pad, _type, noff, size = struct.unpack("<HHII", data[at:at + 12])
        block = data[start - noff:start - noff + size]
        name_length = struct.unpack("<I", block[4:8])[0]
        found[block[8:8 + name_length]] = block[8 + name_length:]
    return found


# ---------------------------------------------------------------------------
# The fixtures
# ---------------------------------------------------------------------------

class MotionPhotoFixture:
    """One built fixture, plus everything a test needs in order to check it."""

    def __init__(self, flavour, jpeg, video, still, loci_needle):
        self.flavour = flavour          # "google" or "samsung"
        self.jpeg = jpeg                # the complete fixture bytes
        self.video = video              # the trailer MP4 bytes
        self.still = still              # the JPEG alone, ending at EOI
        self.loci_needle = loci_needle  # ffmpeg's binary GPS bytes, or None

    @property
    def trailer(self):
        """Everything after the still image's EOI.

        For the Google flavour this is the bare MP4. For the Samsung flavour it
        is the SEF trailer, which wraps the same MP4 in a block header and adds
        the backward-readable directory.
        """
        return self.jpeg[len(self.still):]

    @property
    def filename(self):
        # Google's spec matches motion photos by a *MP.jpg filename. No
        # filename convention was found for Samsung.
        if self.flavour == "google":
            return "synthetic_google_motion_photo_MP.jpg"
        return "synthetic_samsung_motion_photo.jpg"

    def write(self, directory: str) -> str:
        path = os.path.join(directory, self.filename)
        with open(path, "wb") as fh:
            fh.write(self.jpeg)
        return path


def build_still(workdir: str, name: str = "_base.jpg") -> bytes:
    """A JPEG with EXIF (IFD0 and a GPS IFD) and nothing after EOI."""
    path = os.path.join(workdir, name)
    build_base_jpeg(path)
    with open(path, "rb") as fh:
        jpeg = fh.read()
    jpeg = insert_after_leading_apps(jpeg, exif_app1_segment())
    assert jpeg.endswith(b"\xff\xd9"), "still image does not end with EOI"
    with open(path, "wb") as fh:
        fh.write(jpeg)
    return jpeg


def build_video(workdir: str) -> bytes:
    path = os.path.join(workdir, "_trailer.mp4")
    build_mp4(path)
    with open(path, "rb") as fh:
        return fh.read()


def build_google(workdir: str, video: bytes = None, still: bytes = None):
    still = build_still(workdir, "_base_g.jpg") if still is None else still
    video = build_video(workdir) if video is None else video
    # The XMP goes INTO the still, so the still for this flavour is the one
    # carrying it, and the trailer starts immediately after its EOI.
    with_xmp = insert_after_leading_apps(
        still, xmp_app1_segment(google_xmp_packet(len(video))))
    return MotionPhotoFixture("google", with_xmp + video, video, with_xmp,
                              find_loci_payload(video))


def build_samsung(workdir: str, video: bytes = None, still: bytes = None):
    still = build_still(workdir, "_base_s.jpg") if still is None else still
    video = build_video(workdir) if video is None else video
    trailer = sef_trailer([
        (0x0A30, SAMSUNG_MARKER, video),
        (0x0A01, b"Image_UTC_Data", b"1788700800000"),
    ])
    return MotionPhotoFixture("samsung", still + trailer, video, still,
                              find_loci_payload(video))


def build_all(workdir: str):
    """Both fixtures, sharing one still and one video so ffmpeg runs once."""
    still = build_still(workdir)
    video = build_video(workdir)
    return {
        "google": build_google(workdir, video=video, still=still),
        "samsung": build_samsung(workdir, video=video, still=still),
    }


def main() -> None:
    outdir = sys.argv[1] if len(sys.argv) > 1 else os.path.dirname(os.path.abspath(__file__))
    for flavour, fixture in build_all(outdir).items():
        fixture.write(outdir)
        print("%-8s %-42s %6d bytes  video %5d bytes" % (
            flavour, fixture.filename, len(fixture.jpeg), len(fixture.video)))
    print("photo gps rational needle (hex):", PHOTO_GPS_NEEDLE.hex())
    print(DIFFERENCES_FROM_REAL)


if __name__ == "__main__":
    main()
