"""
The ISO base media engine: what it removes, and the proof that it removed it.

THE WALKER IN THIS FILE IS DELIBERATELY NOT THE ENGINE'S WALKER

`walk` and `assert_structurally_clean` below are a second, independent
implementation of the box rules. They do not import `metascrub.isobmff`, and
they resolve the `meta` header shape by a DIFFERENT test than the engine does:
the engine sniffs whether the bytes at the start of the payload look like a box
header, this file checks whether they are the four zero bytes an ISO MetaBox
writes for version 0 with no flags. Two readings of the same trap, so a wrong
one is a disagreement rather than a shared blind spot.

That is the precedent set by `test_av_muxer.read_brand` and followed by
`test_webp_engine.walk`: a test that asks the code under test whether it
succeeded is the circularity this whole project exists to refuse.

FFPROBE IS NEVER USED HERE, AND THAT IS THE MOST IMPORTANT LINE IN THIS FILE

Measured 2026-09-06 by the Phase 0 team: ffprobe exited 0 and reported
`codec_name=h264 nb_frames=30` on an MP4 whose every frame was garbage,
because those numbers come out of the sample tables rather than out of the
samples. exiftool likewise exited 0 and reported `FileType: AVIF` on an AVIF
whose entire `meta` box had been destroyed and which no decoder would open.

So acceptance here is a FULL DECODE plus a `framemd5` comparison against the
input, for every container, including the still images: ffmpeg decodes HEIC and
AVIF and prints a frame hash for them too. ffmpeg is a TEST INSTRUMENT in this
file and never a runtime dependency of the engine, which is pure Python.

WHAT IS BEING PROVEN, AND WHY EACH ONE IS A SEPARATE TEST

  1. STRUCTURE, the Part 3.2 gate. No `udta`, no `uuid`, no `ilst` anywhere in
     the tree; no `meta` outside HEIF and AVIF; every `free` and `skip` payload
     all zero; `mvhd`, `tkhd` and `mdhd` times zero; output length equal to
     input length. A proof about what the file can no longer contain, which
     does not depend on having read anything out of the input first.

  2. BOTH GPS CARRIERS. `moov/udta/(c)xyz` and XMP in the top-level `uuid` box
     are ALTERNATIVES, not a primary and a fallback. Measured: exiftool's
     default GPS write on an MP4 produces the second and no `(c)xyz` atom at
     all, so an engine that implements only the first leaks the coordinates
     for every file exiftool has touched. Two fixtures, two tests.

  3. THE TWO `meta` HEADER SHAPES, trap 8. One file, both shapes, and a walker
     that assumes either misparses the other.

  4. PIXELS. Decoded `framemd5` identical for every video container and for the
     HEIC and the AVIF. Not re-encoded, not remuxed, not moved.

  5. OVERCORRECTION. The HEIF `meta` box and its whole item table survive, an
     `nclx` colr survives, `mdat` is byte-identical wherever the file does not
     store metadata items in it, and the ftyp brand is preserved.

  6. MUTATION. Two tests break a removal on purpose and assert the structural
     gate catches it. A test that cannot fail is not evidence.

WHAT IS NOT ASSERTED, ON PURPOSE

No test here asserts anything about the video bitstream. An H.264 or HEVC SEI
user-data NAL inside `mdat` is untouched by this engine and no assertion in
this file would notice one.

No test here runs the file through `MetadataScrubber`. The engine is not wired
into CAPABILITIES and `test_the_capabilities_rows_are_unchanged` asserts it
stays that way, so there is no scrubber path to exercise yet.
"""

from __future__ import annotations

import os
import shutil
import struct
import subprocess

import pytest

from conftest import (
    HAVE_FFMPEG,
    _exiftool_write,
    _mobile_heic_source,
    assert_oracle_sees_nothing,
    exiftool_or_fail,
    oracle_tags,
    raw_contains,
    sentinel,
)
from metascrub.capabilities import CAPABILITIES, Engine
from metascrub.engines import isobmff_engine
from metascrub.engines.base import EngineError
from metascrub.engines.isobmff_engine import IsobmffEngine


# ----------------------------------------------------------------- the walker
#
# Written from ISO/IEC 14496-12, not from metascrub/isobmff.py.

HEADER_LEN = 8
LARGE_HEADER_LEN = 16
UUID_LEN = 16

# Boxes whose payload is a list of child boxes, and the fixed bytes that come
# first. Independently chosen: this table is shorter than the engine's because
# a test only has to reach the boxes the assertions are about.
CONTAINERS = {
    b"moov": 0, b"trak": 0, b"mdia": 0, b"minf": 0, b"stbl": 0, b"edts": 0,
    b"dinf": 0, b"udta": 0, b"ilst": 0, b"moof": 0, b"traf": 0, b"mvex": 0,
    b"mfra": 0, b"iprp": 0, b"ipco": 0, b"tapt": 0,
    b"iref": 4,
    b"stsd": 8, b"dref": 8,
}

# Free-space boxes hold padding, never boxes. The Phase 0 instrument listed
# `skip` as a container, which is wrong in the specification and would make a
# walker read padding as a box list.
NEVER_DESCEND = {b"free", b"skip", b"mdat", b"idat", b"ipma", b"iloc",
                 b"iinf", b"pitm", b"hdlr"}

TIME_BOXES = {b"mvhd", b"tkhd", b"mdhd"}


class Node:
    __slots__ = ("type", "offset", "size", "header_len", "payload_offset",
                 "payload_len", "depth", "parent", "uuid", "children")

    def __init__(self):
        self.children = []
        self.uuid = None

    @property
    def label(self):
        text = "".join(chr(b) if 32 <= b < 127 else "." for b in self.type)
        if self.uuid:
            return "uuid{%s}" % self.uuid.hex()
        return text


def _meta_skip(data, payload_offset):
    """
    The `meta` header shape, decided by a different test than the engine uses.

    An ISO MetaBox is a FullBox, so its first four payload bytes are a version
    byte and three flag bytes: 00 00 00 00 for the version 0, no-flags form
    every producer measured here writes. Apple's QuickTime Keys `meta` is not a
    FullBox and its first four payload bytes are the size of its first child,
    which is never zero.

    A `meta` with non-zero flags would be misread by this rule. That failure is
    LOUD rather than silent: the child boxes would not tile the payload and
    `walk` raises. The engine's sniff is the more general one; this is the
    second opinion, and the point of a second opinion is that it is arrived at
    differently.
    """
    return 4 if data[payload_offset:payload_offset + 4] == b"\x00\x00\x00\x00" else 0


def _parse(data, start, end, depth, parent, out):
    offset = start
    while offset + HEADER_LEN <= end:
        size = struct.unpack_from(">I", data, offset)[0]
        kind = bytes(data[offset + 4:offset + 8])
        header_len = HEADER_LEN
        if size == 1:
            size = struct.unpack_from(">Q", data, offset + HEADER_LEN)[0]
            header_len = LARGE_HEADER_LEN
        elif size == 0:
            size = end - offset
        node = Node()
        node.type = kind
        node.offset = offset
        node.size = size
        node.depth = depth
        node.parent = parent
        if kind == b"uuid":
            node.uuid = bytes(data[offset + header_len:offset + header_len + UUID_LEN])
            header_len += UUID_LEN
        node.header_len = header_len
        node.payload_offset = offset + header_len
        node.payload_len = size - header_len
        assert size >= header_len, (
            f"box {node.label} at {offset} declares {size} bytes, less than its "
            f"own {header_len}-byte header")
        assert offset + size <= end, (
            f"box {node.label} at {offset} declares {size} bytes, past the end "
            f"of its parent at {end}")
        out.append(node)

        skip = None
        if kind in NEVER_DESCEND:
            skip = None
        elif kind == b"meta":
            skip = _meta_skip(data, node.payload_offset)
        elif kind in CONTAINERS:
            skip = CONTAINERS[kind]
        if skip is not None and node.payload_len >= skip + HEADER_LEN:
            _parse(data, node.payload_offset + skip,
                   node.payload_offset + node.payload_len,
                   depth + 1, node, node.children)
        offset += size

    if offset != end:
        leftover = end - offset
        assert leftover < HEADER_LEN and not any(data[offset:end]), (
            f"{leftover} bytes at offset {offset} are not accounted for by any "
            f"box")


def walk(data):
    """Every box in the file, parents before children, flattened."""
    assert data[4:8] == b"ftyp", "the file does not start with an ftyp box"
    top = []
    _parse(data, 0, len(data), 0, None, top)
    flat = []

    def collect(nodes):
        for node in nodes:
            flat.append(node)
            collect(node.children)

    collect(top)
    return flat


def read(path):
    with open(path, "rb") as handle:
        return handle.read()


def structural_metas(nodes):
    """
    Offsets of the `meta` boxes that are the FILE STRUCTURE, not a dictionary.

    A `meta` holding both `iinf` and `iloc` describes where the image data
    lives. Removing it produces a file with no image in it, so it is the one
    box in this format family that must survive. Decided by content, because a
    brand cannot tell the two `meta` header shapes apart either.
    """
    out = set()
    for node in nodes:
        if node.type != b"meta":
            continue
        names = {child.type for child in node.children}
        if b"iinf" in names and b"iloc" in names:
            out.add(node.offset)
    return out


def assert_structurally_clean(path, note="", original_length=None):
    """
    The Part 3.2 gate for ISO base media, asserted over the OUTPUT alone.

    Everything here is a statement about what the file can no longer contain.
    None of it needs the input, except the length, which the caller passes when
    it has it.
    """
    data = read(path)
    if original_length is not None:
        assert len(data) == original_length, (
            f"{note}: the output is {len(data)} bytes and the input was "
            f"{original_length}. Every sample offset in the file is now wrong.")

    nodes = walk(data)
    structural = structural_metas(nodes)

    for node in nodes:
        assert node.type != b"udta", (
            f"{note}: a udta box survived at offset {node.offset}")
        assert node.type != b"uuid", (
            f"{note}: a uuid box survived at offset {node.offset} "
            f"({node.label})")
        assert node.type != b"ilst", (
            f"{note}: an ilst box survived at offset {node.offset}")
        if node.type == b"meta":
            assert node.offset in structural, (
                f"{note}: a meta box with no item table survived at offset "
                f"{node.offset}; only HEIF and AVIF keep a meta box")
        if node.type in (b"free", b"skip"):
            payload = data[node.payload_offset:node.payload_offset + node.payload_len]
            assert not any(payload), (
                f"{note}: the {node.label} box at offset {node.offset} holds "
                f"{sum(1 for b in payload if b)} non-zero bytes")
        if node.type in TIME_BOXES:
            version = data[node.payload_offset]
            width = 8 if version == 1 else 4
            first = node.payload_offset + 4
            fields = data[first:first + 2 * width]
            assert not any(fields), (
                f"{note}: the {node.label} box at offset {node.offset} still "
                f"carries a creation or modification time")
    return nodes


# ------------------------------------------------------------ decode evidence


def _run(args):
    return subprocess.run(args, capture_output=True)


def framemd5(path):
    """
    The decoded frame hashes of a file, as a list of lines.

    A FULL DECODE: ffmpeg decodes every frame and hashes the decoded pixels.
    Not `-c copy`, which would hash the compressed packets and prove only that
    the bitstream bytes were not touched.
    """
    proc = _run(["ffmpeg", "-v", "error", "-i", path, "-f", "framemd5", "-"])
    assert proc.returncode == 0, (
        f"ffmpeg could not decode {path}: {proc.stderr.decode(errors='replace')}")
    lines = [line for line in proc.stdout.decode().splitlines()
             if line and not line.startswith("#")]
    assert lines, f"ffmpeg decoded no frames at all from {path}"
    return lines


def assert_decodes_cleanly(path, note=""):
    """A full decode to nowhere, with warnings treated as failures."""
    proc = _run(["ffmpeg", "-v", "warning", "-i", path, "-f", "null", "-"])
    stderr = proc.stderr.decode(errors="replace").strip()
    assert proc.returncode == 0 and not stderr, (
        f"{note}: the output did not decode cleanly (exit {proc.returncode})\n"
        f"{stderr}")


def scrub(path):
    return IsobmffEngine().strip_all(path)


def stage(containers, name, tmp_path):
    """Copy a shared fixture so a test can scrub it without disturbing others."""
    source = containers[name]
    target = str(tmp_path / os.path.basename(source))
    shutil.copyfile(source, target)
    return target


# ------------------------------------------------------------------- fixtures
#
# Built with ffmpeg and exiftool, which are not the engine under test, then
# stamped and CHECKED before any test runs. Session-scoped because ffmpeg is
# slow and every test copies before it writes.


def _ffmpeg(args):
    proc = _run(["ffmpeg", "-y", "-loglevel", "error"] + args)
    assert proc.returncode == 0, (
        "fixture ffmpeg call failed: " + proc.stderr.decode(errors="replace"))


def _retype_box_in_place(path, from_type, to_type):
    """
    Rename one box, keeping its payload and its length.

    Used to manufacture an ORPHANED `free` box: a `udta` full of tag strings
    that some earlier editor abandoned in place rather than reclaiming. Section
    2.4 says free-space boxes can hold exactly that, and this is the only way
    to build one without changing a single offset.
    """
    data = bytearray(read(path))
    for node in walk(bytes(data)):
        if node.type == from_type:
            data[node.offset + 4:node.offset + 8] = to_type
            with open(path, "wb") as handle:
                handle.write(bytes(data))
            return node.size
    raise AssertionError(f"no {from_type!r} box to rename in {path}")


def _upgrade_times_to_version_1(src, dst):
    """
    Rewrite mvhd, tkhd and mdhd from version 0 to version 1.

    Adapted from the Phase 0 instrument `mk_version1.py`, which exists because
    ffmpeg 2024-12-11 will not emit version 1 headers: given a creation_time
    past 2040 it wrote version 0 with the value silently reduced modulo 2**32.
    A 64-bit time field has to be constructed to be tested.

    Only valid where `moov` follows `mdat`, which is ffmpeg's default layout:
    the boxes grow by 12 bytes each, and nothing that holds a file offset sits
    after them. The caller decodes the result, so a mistake here shows up as a
    broken fixture rather than as a passing test.
    """
    data = read(src)
    nodes = walk(data)
    top = [node for node in nodes if node.depth == 0]
    kinds = [node.type for node in top]
    assert kinds.index(b"mdat") < kinds.index(b"moov"), (
        "this rewrite only works when moov follows mdat")

    def upgrade(node):
        payload = node.payload_offset
        assert data[payload] == 0, "already version 1"
        flags = data[payload + 1:payload + 4]
        if node.type == b"tkhd":
            create, modify, track, reserved, duration = struct.unpack_from(
                ">IIIII", data, payload + 4)
            rest = data[payload + 24:payload + node.payload_len]
            body = struct.pack(">QQIIQ", create, modify, track, reserved,
                               duration) + rest
        else:
            create, modify, scale, duration = struct.unpack_from(
                ">IIII", data, payload + 4)
            rest = data[payload + 20:payload + node.payload_len]
            body = struct.pack(">QQIQ", create, modify, scale, duration) + rest
        body = bytes([1]) + flags + body
        return struct.pack(">I", HEADER_LEN + len(body)) + node.type + body

    def emit(node):
        if node.type in TIME_BOXES:
            return upgrade(node)
        if not node.children:
            return data[node.offset:node.offset + node.size]
        first = node.children[0].offset
        prefix = data[node.payload_offset:first]
        body = b"".join(emit(child) for child in node.children)
        assert node.children[-1].offset + node.children[-1].size == \
            node.payload_offset + node.payload_len, (
                f"children of {node.label} do not tile its payload")
        return (struct.pack(">I", HEADER_LEN + len(prefix) + len(body))
                + node.type + prefix + body)

    with open(dst, "wb") as handle:
        handle.write(b"".join(emit(node) for node in top))


@pytest.fixture(scope="session")
def containers(tmp_path_factory):
    """
    One of every ISO base media shape this engine claims to handle.

    Each is built by something that is not the engine, then checked: a fixture
    that failed to store what it promises would give every test below a file
    with nothing to remove, and they would all pass.
    """
    if not HAVE_FFMPEG:
        pytest.skip("ffmpeg not available; cannot build ISO base media fixtures")
    exiftool_or_fail()

    root = tmp_path_factory.mktemp("isobmff")
    paths = {}

    def path(name):
        return str(root / name)

    # ffmpeg reads options positionally: everything before an -i is an INPUT
    # option. Encoder flags placed between two inputs become decoder flags for
    # the second one, and ffmpeg answers "Unknown decoder 'libx264'". So the
    # inputs and the output options are kept as separate lists rather than one
    # convenient prefix.
    video_in = ["-f", "lavfi", "-i", "testsrc=size=320x240:rate=15:duration=1"]
    audio_in = ["-f", "lavfi", "-i", "sine=frequency=440:duration=1"]
    video_out = ["-c:v", "libx264", "-pix_fmt", "yuv420p"]
    audio_out = ["-c:a", "aac"]
    video = video_in + video_out

    # 1. MP4 whose GPS is in the udta (c)xyz atom, plus an iTunes ilst and a
    #    QuickTime Keys meta. This is the file that carries BOTH meta header
    #    shapes at once.
    udta = path("udta_gps.mp4")
    _ffmpeg(video_in + audio_in + video_out + audio_out + [udta])
    _exiftool_write(
        udta,
        **{
            "UserData:GPSCoordinates": "44.5588 -72.5778 315",
            "UserData:Artist": sentinel("isoudta"),
            "UserData:Comment": sentinel("isoudta"),
            "UserData:Model": "Pixel 7",
            "Keys:Title": sentinel("isokeys"),
        },
    )
    assert raw_contains(udta, sentinel("isoudta"))
    assert raw_contains(udta, sentinel("isokeys"))
    assert b"\xa9xyz" in read(udta), (
        "the udta fixture has no (c)xyz atom, so it proves nothing about the "
        "first GPS carrier")
    paths["udta_gps"] = udta

    # 2. MP4 whose GPS is ONLY in XMP in the top-level uuid box. This is what a
    #    plain exiftool -GPSLatitude write produces, and it is the carrier an
    #    engine that implements the 2.4 table's first row alone would miss.
    xmp = path("xmp_gps.mp4")
    _ffmpeg(video + [xmp])
    _exiftool_write(xmp, GPSLatitude="44.5588", GPSLatitudeRef="N",
                    GPSLongitude="-72.5778", GPSLongitudeRef="W",
                    **{"XMP-dc:Description": sentinel("isoxmp")})
    assert raw_contains(xmp, sentinel("isoxmp"))
    assert b"\xa9xyz" not in read(xmp), (
        "the XMP fixture unexpectedly grew a (c)xyz atom, so it no longer "
        "isolates the second carrier")
    paths["xmp_gps"] = xmp

    # 3. QuickTime .mov. XMP lands in moov/udta/XMP_ here, NOT in a uuid box.
    mov = path("quicktime.mov")
    _ffmpeg(video_in + audio_in + video_out + audio_out
            + ["-metadata", "title=" + sentinel("isomov"), mov])
    _exiftool_write(mov, **{"UserData:GPSCoordinates": "44.5588 -72.5778 315",
                            "XMP-dc:Description": sentinel("isomovxmp")})
    assert read(mov)[8:12] == b"qt  ", "the .mov fixture is not QuickTime-branded"
    assert b"XMP_" in read(mov), (
        "the .mov fixture has no moov/udta/XMP_ box, so it does not exercise "
        "the QuickTime XMP carrier")
    paths["mov"] = mov

    # 4. 3GP.
    third = path("phone.3gp")
    _ffmpeg(["-f", "lavfi", "-i", "testsrc=size=176x144:rate=15:duration=1",
             "-c:v", "libx264", "-pix_fmt", "yuv420p", "-profile:v", "baseline",
             third])
    _exiftool_write(third, **{"UserData:Artist": sentinel("iso3gp"),
                              "XMP-dc:Description": sentinel("iso3gpxmp")})
    assert raw_contains(third, sentinel("iso3gp"))
    paths["3gp"] = third

    # 5. +faststart, which puts moov before mdat, and 6. a fragmented MP4,
    #    whose moof/traf and mfra/tfra hold their own absolute offsets.
    fast = path("faststart.mp4")
    _ffmpeg(video + ["-movflags", "+faststart",
                     "-metadata", "comment=" + sentinel("isofast"), fast])
    assert raw_contains(fast, sentinel("isofast"))
    paths["faststart"] = fast

    frag = path("fragmented.mp4")
    _ffmpeg(video + ["-movflags", "+frag_keyframe+empty_moov",
                     "-metadata", "comment=" + sentinel("isofrag"), frag])
    assert raw_contains(frag, sentinel("isofrag"))
    paths["fragmented"] = frag

    # 7. Version 0 times, explicitly set. An untouched ffmpeg MP4 already has
    #    creation_time 0, so a fixture meant to prove the zeroing works has to
    #    carry a real time first or the test measures nothing.
    times = path("times_v0.mp4")
    _ffmpeg(video + ["-metadata", "creation_time=2026-09-06T14:30:22Z", times])
    paths["times_v0"] = times

    # 8. The same file with 64-bit time fields.
    times_v1 = path("times_v1.mp4")
    _upgrade_times_to_version_1(times, times_v1)
    paths["times_v1"] = times_v1

    # 9. An orphaned free box holding abandoned tag strings.
    orphan = path("orphan_free.mp4")
    _ffmpeg(video + ["-metadata", "comment=" + sentinel("isoorphan"), orphan])
    _retype_box_in_place(orphan, b"udta", b"free")
    assert raw_contains(orphan, sentinel("isoorphan"))
    paths["orphan_free"] = orphan

    # 10. AVIF, whose colr is nclx and must SURVIVE.
    avif = path("still.avif")
    _ffmpeg(["-f", "lavfi", "-i", "testsrc=size=160x120:rate=1:duration=1",
             "-frames:v", "1", "-c:v", "libaom-av1", "-still-picture", "1",
             "-f", "avif", avif])
    paths["avif"] = avif

    # 11. HEIC: a real container with synthetic device metadata written on top.
    source = _mobile_heic_source()
    if source is not None:
        heic = path("tagged.heic")
        shutil.copyfile(source, heic)
        _exiftool_write(
            heic, Make="Apple", Model="iPhone 11",
            Software=sentinel("isoheic"), Artist=sentinel("isoheic"),
            DateTimeOriginal="2026:03:14 09:26:53",
            GPSLatitude="37.4220", GPSLatitudeRef="N",
            GPSLongitude="-122.0841", GPSLongitudeRef="W",
            **{"XMP-dc:Description": sentinel("isoheicxmp")})
        assert raw_contains(heic, sentinel("isoheic"))
        assert raw_contains(heic, sentinel("isoheicxmp"))
        paths["heic"] = heic

    return paths


def require_heic(containers):
    if "heic" not in containers:
        pytest.skip("no HEIC source file available; set METASCRUB_HEIC_SOURCE")
    return containers["heic"]


# EVERY container this engine handles, for the tests that apply to all of them.
# The HEIC row is added by the fixture only when a source file exists.
VIDEO_KINDS = ["udta_gps", "xmp_gps", "mov", "3gp", "faststart", "fragmented",
               "times_v0", "times_v1", "orphan_free"]
STILL_KINDS = ["avif", "heic"]
ALL_ISOBMFF = VIDEO_KINDS + STILL_KINDS


# ------------------------------------------------------------- the oracle list
#
# MEASURED 2026-09-06, exiftool 13.29, on THIS engine's output over all eleven
# fixtures above. It is a local list and not conftest's
# ORACLE_ALLOW_ISOBMFF_REMUX, which conftest itself marks "Phase 2 must
# re-measure and shrink this, and must not inherit it unexamined".
#
# Re-measuring did not shrink it. It grew, from 42 names to 79, and the reason
# is the whole difference between the two engines: the remux engine throws the
# container away and writes a new one, so exiftool sees only what ffmpeg chose
# to write. This engine keeps the container, so exiftool still reads the AV1
# and HEVC configuration records, the HEIF item geometry and the fragmented
# MP4's sequence number. Every one of the extra names is a codec configuration
# number or an image dimension.
#
# What SHRANK is the part that matters. Three of the nine gated names in the
# remux list are gone from this one entirely, because this engine removes the
# values rather than tolerating them:
#   QuickTime:HandlerDescription  the hdlr name, now zeroed
#   QuickTime:HandlerVendorID     the hdlr vendor, now zeroed
# and two names the remux list does not mention at all were measured surviving
# HERE before they were handled, then removed:
#   QuickTime:CompressorName = 'Lavc61.26.100 libx264'
#   QuickTime:VendorID       = 'FFMP'
#
# The rule for this list is conftest's rule: a BARE name may only be a field
# that cannot carry identity at all. Everything below is a number, a
# fixed-point matrix, a byte offset, a box version, or a four-character codec
# or brand code. Anything that came back as a free string or a wall-clock time
# is in the gated table instead, with a predicate over its value.
ORACLE_ALLOW_ISOBMFF_INPLACE = frozenset({
    # ftyp
    "QuickTime:MajorBrand",
    "QuickTime:MinorVersion",
    "QuickTime:CompatibleBrands",
    # mvhd, tkhd, mdhd: versions, ids, counters, geometry, timing
    "QuickTime:MovieHeaderVersion",
    "QuickTime:TrackHeaderVersion",
    "QuickTime:MediaHeaderVersion",
    "QuickTime:NextTrackID",
    "QuickTime:TrackID",
    "QuickTime:TrackLayer",
    "QuickTime:TrackVolume",
    "QuickTime:TrackDuration",
    "QuickTime:PreferredRate",
    "QuickTime:PreferredVolume",
    "QuickTime:MatrixStructure",
    "QuickTime:TimeScale",
    "QuickTime:Duration",
    "QuickTime:MediaTimeScale",
    "QuickTime:MediaDuration",
    "QuickTime:PosterTime",
    "QuickTime:PreviewTime",
    "QuickTime:PreviewDuration",
    "QuickTime:SelectionTime",
    "QuickTime:SelectionDuration",
    "QuickTime:CurrentTime",
    "QuickTime:Balance",
    "QuickTime:LayoutFlags",
    "QuickTime:MovieFragmentSequence",
    # hdlr, now that its name and vendor are zeroed: only the four-character
    # handler type and class remain, and both are structural.
    "QuickTime:HandlerType",
    "QuickTime:HandlerClass",
    # stsd, vmhd, pasp and the sample entry geometry
    "QuickTime:CompressorID",
    "QuickTime:GraphicsMode",
    "QuickTime:OpColor",
    "QuickTime:BitDepth",
    "QuickTime:ImageWidth",
    "QuickTime:ImageHeight",
    "QuickTime:SourceImageWidth",
    "QuickTime:SourceImageHeight",
    "QuickTime:PixelAspectRatio",
    "QuickTime:CleanAperture",
    "QuickTime:XResolution",
    "QuickTime:YResolution",
    "QuickTime:VideoFrameRate",
    "QuickTime:AverageFrameRate",
    "QuickTime:ConstantFrameRate",
    "QuickTime:AverageBitrate",
    "QuickTime:MaxBitrate",
    "QuickTime:BufferSize",
    "QuickTime:Rotation",
    "QuickTime:AudioFormat",
    "QuickTime:AudioChannels",
    "QuickTime:AudioBitsPerSample",
    "QuickTime:AudioSampleRate",
    "QuickTime:PurchaseFileFormat",
    # avcC / hvcC / av1C: the codec configuration records, which are the
    # bitstream's own parameters and are read out of stsd.
    "QuickTime:AV1ConfigurationVersion",
    "QuickTime:HEVCConfigurationVersion",
    "QuickTime:GeneralProfileSpace",
    "QuickTime:GeneralTierFlag",
    "QuickTime:GeneralProfileIDC",
    "QuickTime:GenProfileCompatibilityFlags",
    "QuickTime:GeneralLevelIDC",
    "QuickTime:ConstraintIndicatorFlags",
    "QuickTime:MinSpatialSegmentationIDC",
    "QuickTime:ParallelismType",
    "QuickTime:ChromaFormat",
    "QuickTime:BitDepthLuma",
    "QuickTime:BitDepthChroma",
    "QuickTime:NumTemporalLayers",
    "QuickTime:TemporalIDNested",
    "QuickTime:ChromaSamplePosition",
    "QuickTime:ColorPrimaries",
    "QuickTime:TransferCharacteristics",
    "QuickTime:MatrixCoefficients",
    "QuickTime:VideoFullRangeFlag",
    "QuickTime:ColorProfiles",
    # HEIF and AVIF item properties: ispe, pixi, pitm.
    "QuickTime:ImageSpatialExtent",
    "QuickTime:ImagePixelDepth",
    "QuickTime:PrimaryItemReference",
    # mdat, which is the picture itself
    "QuickTime:MediaDataOffset",
    "QuickTime:MediaDataSize",
})


def _is_zeroed_time(value):
    """
    An ISO base media time that has actually been zeroed.

    Measured on THIS engine's output: exiftool renders the zeroed field as
    '0000:00:00 00:00:00'. '1904:01:01 00:00:00' is the same instant rendered
    by an exiftool that applied the QuickTime epoch instead, so both are
    accepted and nothing else is. A real capture time is a leak.
    """
    return str(value).strip() in {"0000:00:00 00:00:00", "1904:01:01 00:00:00"}


def _is_unspecified_language(value):
    """
    mdhd language, which this engine deliberately does NOT rewrite.

    Two spellings of "not stated", both measured and both produced by the
    fixture builders rather than by the engine: 'und' is ISO 639-2 undetermined
    and is what ffmpeg's mp4 muxer writes, 32767 is 0x7FFF, QuickTime's
    unspecified language, and is what its mov muxer writes.

    This is the one gated name here that the engine does not control, and it is
    stated rather than hidden: a file whose track really is tagged 'eng' keeps
    that tag and this predicate rejects it, which is the correct outcome for a
    residual the engine does not remove.
    """
    return str(value).strip() in {"", "und", "32767"}


ORACLE_GATED_ISOBMFF_INPLACE = {
    "QuickTime:CreateDate": _is_zeroed_time,
    "QuickTime:ModifyDate": _is_zeroed_time,
    "QuickTime:TrackCreateDate": _is_zeroed_time,
    "QuickTime:TrackModifyDate": _is_zeroed_time,
    "QuickTime:MediaCreateDate": _is_zeroed_time,
    "QuickTime:MediaModifyDate": _is_zeroed_time,
    "QuickTime:MediaLanguageCode": _is_unspecified_language,
}


# ------------------------------------------------------------- the walker itself


def test_both_meta_header_shapes_are_parsed_from_one_file(containers):
    """
    TRAP 8, in its new place. One file, two `meta` boxes, two header shapes.

    ISO/IEC 14496-12 defines MetaBox as a FullBox; Apple's QuickTime Keys
    `meta` is the same four characters and is not one. The Phase 0 instrument
    assumed FullBox and read the second one as a child box of size 1751411826,
    which is the ASCII of 'hdlr' read as a length. This test asserts the two
    shapes really do coexist, then asserts the ENGINE's sniff answers 0 and 4
    for them, and finally asserts what the naive reading would have produced,
    so the trap is measured rather than remembered.
    """
    from metascrub import isobmff

    data = read(containers["udta_gps"])

    # Located with THIS file's walker, which descends into udta. The engine's
    # walker deliberately does not: udta is removed as a whole subtree, so
    # opening it can only add ways to misparse. That is why the engine's tree
    # shows one meta and this one shows two, and it is asserted rather than
    # left as a surprise for the next reader.
    mine = [node for node in walk(data) if node.type == b"meta"]
    assert len(mine) == 2, (
        f"the fixture no longer carries two meta boxes ({len(mine)} found), "
        "so it cannot exercise the two header shapes")
    parents = sorted(node.parent.type for node in mine)
    assert parents == [b"moov", b"udta"], parents
    assert b"udta" in isobmff.OPAQUE

    def as_box(node):
        box = isobmff.Box()
        box.type = node.type
        box.offset = node.offset
        box.size = node.size
        box.header_len = node.header_len
        box.payload_offset = node.payload_offset
        box.payload_len = node.payload_len
        return box

    skips = {node.parent.type: isobmff.meta_child_skip(data, as_box(node))
             for node in mine}
    assert skips[b"udta"] == 4, "the ISO meta under udta is a FullBox"
    assert skips[b"moov"] == 0, "the QuickTime Keys meta under moov is not"

    # The shape the engine actually walks, reached through its own parser, so
    # the sniff is exercised in production and not only in this test.
    engine_metas = [box for box in isobmff.iter_boxes(isobmff.parse(data))
                    if box.type == b"meta"]
    assert len(engine_metas) == 1 and engine_metas[0].parent_type == b"moov"
    assert [child.type for child in engine_metas[0].children][:1] == [b"hdlr"], (
        "the Keys meta was misparsed: its first child should be hdlr")

    keys_meta = engine_metas[0]
    naive = struct.unpack_from(">I", data, keys_meta.payload_offset + 4)[0]
    assert naive > len(data), (
        "the FullBox-assuming reading of this box no longer produces an "
        f"impossible child size ({naive}); the fixture has changed shape and "
        "this test no longer demonstrates the trap")


def test_the_walkers_agree_on_the_box_inventory(containers):
    """
    The independent walker in this file and the engine's walker see the same
    boxes, on every fixture.

    Not a proof that either is right. It is a proof that a later change to one
    of them cannot silently diverge from the other, which is the whole reason
    for maintaining two.
    """
    from metascrub import isobmff

    for name in ALL_ISOBMFF:
        if name not in containers:
            continue
        data = read(containers[name])
        mine = sorted((node.offset, node.size, node.type) for node in walk(data))
        theirs = sorted((box.offset, box.size, box.type)
                        for box in isobmff.iter_boxes(isobmff.parse(data)))
        # The engine treats udta, ilst and hdlr as opaque, so it reports fewer
        # boxes. Everything it DOES report must be one this file also found.
        missing = [entry for entry in theirs if entry not in mine]
        assert not missing, f"{name}: the engine reports boxes this file does not: {missing}"


def test_skip_and_ipma_are_not_containers():
    """
    Two Phase 0 instrument bugs, asserted fixed rather than remembered.

    `skip` is a FreeSpaceBox: its payload is padding, and a walker that
    descends into it reads whatever the padding happens to say. `ipma` is the
    item property ASSOCIATION table, a FullBox of counts and indices. Neither
    mattered for reconnaissance, where a wrong child is visible in a dump. Both
    matter here, where a wrong child is an edit at the wrong offset.
    """
    from metascrub import isobmff

    for kind in (b"skip", b"ipma", b"free", b"mdat"):
        assert kind not in isobmff.CONTAINERS, (
            f"{kind!r} is in the container table; it holds bytes, not boxes")
        assert kind in isobmff.OPAQUE


# ---------------------------------------------------------------- the gate


@pytest.mark.parametrize("kind", ALL_ISOBMFF)
def test_the_structural_gate_holds_on_every_container(containers, kind, tmp_path):
    """
    Part 3.2 over the output, plus the length invariant of 2.5, for every
    container shape: moov before mdat, moov after mdat, +faststart, fragmented,
    QuickTime, 3GP, AVIF and HEIC.
    """
    if kind == "heic":
        require_heic(containers)
    elif kind not in containers:
        pytest.skip(f"no {kind} fixture")

    path = stage(containers, kind, tmp_path)
    before = len(read(path))
    scrub(path)
    assert_structurally_clean(path, note=kind, original_length=before)


@pytest.mark.parametrize("kind", ALL_ISOBMFF)
def test_the_decoded_pixels_are_bit_identical(containers, kind, tmp_path):
    """
    The acceptance criterion of 2.5, and the reason ffprobe appears nowhere in
    this file: a full decode plus a framemd5 comparison against the input.

    ffprobe exited 0 with plausible stream and frame counts on a file whose
    every frame was garbage. These hashes come out of the decoded pixels.
    """
    if kind == "heic":
        require_heic(containers)
    elif kind not in containers:
        pytest.skip(f"no {kind} fixture")

    path = stage(containers, kind, tmp_path)
    before = framemd5(path)
    scrub(path)
    assert framemd5(path) == before, (
        f"{kind}: the decoded frames changed. The engine is supposed to move "
        "no byte of sample data and no offset that points at one.")
    assert_decodes_cleanly(path, note=kind)


@pytest.mark.parametrize("kind", ALL_ISOBMFF)
def test_the_oracle_sees_nothing_it_should_not(containers, kind, tmp_path):
    """
    exiftool, an independent implementation, over this engine's output.

    Necessary and not sufficient: Part 3.3 is blunt that a byte search and an
    oracle read both miss carriers the structural gate catches, so this is
    always paired with the gate above.
    """
    if kind == "heic":
        require_heic(containers)
    elif kind not in containers:
        pytest.skip(f"no {kind} fixture")

    path = stage(containers, kind, tmp_path)
    scrub(path)
    assert_oracle_sees_nothing(
        path,
        allow=ORACLE_ALLOW_ISOBMFF_INPLACE,
        gated=ORACLE_GATED_ISOBMFF_INPLACE,
        note=f"pure-Python isobmff engine, {kind} fixture",
    )


def test_the_allowlist_is_not_wide_enough_to_pass_an_unscrubbed_file(containers):
    """
    OVERCORRECTION TEST for the allowlist itself.

    Every name in an allowlist is a name nobody checks again. This asserts the
    list is narrow enough that the UNSCRUBBED fixture fails it, which is the
    cheapest possible proof that the assertion above is doing work rather than
    accepting anything the format can produce.
    """
    with pytest.raises(AssertionError) as caught:
        assert_oracle_sees_nothing(
            containers["udta_gps"],
            allow=ORACLE_ALLOW_ISOBMFF_INPLACE,
            gated=ORACLE_GATED_ISOBMFF_INPLACE,
            note="the INPUT, which is supposed to fail",
        )
    message = str(caught.value)
    assert "GPSCoordinates" in message or "Artist" in message, message


# --------------------------------------------------------------- the carriers


def test_the_udta_gps_atom_is_removed(containers, tmp_path):
    """
    Carrier one: `moov/udta` and the 0xA9 'xyz' atom inside it, bytes
    a9 78 79 7a, payload an ISO 6709 string.
    """
    path = stage(containers, "udta_gps", tmp_path)
    assert b"\xa9xyz" in read(path)
    before = read(path)

    reported = scrub(path)

    out = read(path)
    assert b"\xa9xyz" not in out, "the GPS atom survived"
    assert b"+44.5588" not in out and b"44.5588" not in out
    assert not raw_contains(path, sentinel("isoudta"))
    assert not raw_contains(path, sentinel("isokeys"))
    assert "Pixel 7" not in out.decode("latin-1")
    assert len(out) == len(before)
    assert any("udta" in line for line in reported), reported

    surviving = oracle_tags(path)
    leaks = [tag for tag in surviving if "GPS" in tag or "Model" in tag]
    assert not leaks, leaks


def test_the_xmp_uuid_gps_is_removed(containers, tmp_path):
    """
    Carrier two, and the one an engine written from the ORIGINAL 2.4 table
    misses completely.

    MEASURED 2026-09-06: exiftool's default GPS write on an MP4 puts the
    coordinates in XMP inside the top-level `uuid` box and writes NO 0xA9 xyz
    atom at all. The fixture asserts that shape before the scrub, so this test
    fails loudly if a future exiftool changes where it writes rather than
    quietly proving nothing.
    """
    path = stage(containers, "xmp_gps", tmp_path)
    before = read(path)
    assert b"\xa9xyz" not in before, "the fixture is no longer XMP-only"
    assert isobmff_engine.isobmff.XMP_UUID in before, (
        "the fixture has no XMP uuid box")
    assert b"xmpmeta" in before

    scrub(path)

    out = read(path)
    assert b"xmpmeta" not in out, "the XMP packet survived"
    assert isobmff_engine.isobmff.XMP_UUID not in out
    assert not raw_contains(path, sentinel("isoxmp"))
    assert len(out) == len(before)
    assert not [tag for tag in oracle_tags(path) if "GPS" in tag or "XMP" in tag]


def test_quicktime_xmp_lives_in_udta_and_is_removed(containers, tmp_path):
    """
    Carrier three. 2.4 said XMP lives in a `uuid` box with a well-known UUID.
    That is true for MP4 and 3GP and FALSE for QuickTime: measured, a .mov
    written by exiftool carries `moov/udta/XMP_` and no `uuid` box at all.

    The whitelist approach saves the engine here, because `XMP_` is inside
    `udta` and `udta` goes as a whole subtree. The structural gate in 3.2 names
    `udta`, `uuid` and `ilst` and would pass a file that kept `XMP_` some other
    way, so the byte search below is the belt.
    """
    path = stage(containers, "mov", tmp_path)
    before = read(path)
    assert b"XMP_" in before
    assert isobmff_engine.isobmff.XMP_UUID not in before, (
        "the .mov fixture grew a uuid box; it no longer isolates the "
        "QuickTime carrier")

    scrub(path)

    out = read(path)
    assert b"XMP_" not in out, "the QuickTime XMP box survived"
    assert b"xmpmeta" not in out
    assert b"\xa9xyz" not in out
    assert not raw_contains(path, sentinel("isomov"))
    assert not raw_contains(path, sentinel("isomovxmp"))
    assert out[8:12] == b"qt  ", (
        "the QuickTime brand was rewritten. Preserving it is the measured "
        "advantage of in-place surgery over the ffmpeg remux engine, whose own "
        "docstring records that the mp4 muxer turns an mp41 input into isom.")


@pytest.mark.parametrize("kind,version", [("times_v0", 0), ("times_v1", 1)])
def test_times_are_zeroed_at_both_field_widths(containers, kind, version, tmp_path):
    """
    creation_time and modification_time, zeroed rather than removed, at 32 bits
    and at 64.

    The version 1 fixture has to be constructed: ffmpeg will not emit one, and
    given a creation_time past 2040 it wrote version 0 with the value silently
    reduced modulo 2**32. The assertion below reads the version byte from the
    file rather than trusting the fixture name.
    """
    path = stage(containers, kind, tmp_path)
    data = read(path)
    headers = [node for node in walk(data) if node.type in TIME_BOXES]
    assert headers, "no mvhd/tkhd/mdhd in the fixture"
    for node in headers:
        assert data[node.payload_offset] == version, (
            f"{node.label} is version {data[node.payload_offset]}, not {version}")
    width = 8 if version == 1 else 4
    assert any(
        any(data[node.payload_offset + 4:node.payload_offset + 4 + 2 * width])
        for node in headers), (
        "every time in the fixture is already zero, so this test would pass "
        "against an engine that did nothing")

    reported = scrub(path)

    out = read(path)
    for node in walk(out):
        if node.type not in TIME_BOXES:
            continue
        assert out[node.payload_offset] == version, "the version byte changed"
        fields = out[node.payload_offset + 4:node.payload_offset + 4 + 2 * width]
        assert not any(fields), f"{node.label} still carries a time"
    assert len(out) == len(data)
    assert sum("creation_time" in line for line in reported) == len(headers)


def test_an_orphaned_free_box_is_zeroed(containers, tmp_path):
    """
    Section 2.4: zero the CONTENTS of `free` and `skip` rather than removing
    them, because they can hold orphaned data from a previous edit.

    The fixture is exactly that: a `udta` full of tag strings, renamed to
    `free` in place, which is what an editor that abandoned a box rather than
    reclaiming it leaves behind. Nothing else in this engine would look at it:
    it is not a udta any more, exiftool reports nothing for it, and so the
    residual byte search would never be given a needle for its contents.
    """
    path = stage(containers, "orphan_free", tmp_path)
    before = read(path)
    assert raw_contains(path, sentinel("isoorphan"))
    orphan = [node for node in walk(before)
              if node.type == b"free" and node.payload_len > 0
              and any(before[node.payload_offset:
                             node.payload_offset + node.payload_len])]
    assert orphan, "the fixture has no free box with content to zero"

    reported = scrub(path)

    out = read(path)
    assert not raw_contains(path, sentinel("isoorphan"))
    assert len(out) == len(before)
    assert any("free" in line for line in reported), reported
    for node in walk(out):
        if node.type in (b"free", b"skip"):
            payload = out[node.payload_offset:node.payload_offset + node.payload_len]
            assert not any(payload)


def test_the_visual_sample_entry_strings_are_removed(containers, tmp_path):
    """
    `compressorname` and the vendor code, neither of which section 2.4
    mentions.

    MEASURED on this engine's own output before they were handled:
    `QuickTime:CompressorName = 'Lavc61.26.100 libx264'` and
    `QuickTime:VendorID = 'FFMP'` survived every other removal in this engine.
    They name the software that wrote the file, which is the same class of
    value as a PDF Producer string, and on a phone they name the phone's
    encoder.
    """
    path = stage(containers, "mov", tmp_path)
    before = read(path)
    assert b"libx264" in before, "the fixture no longer carries an encoder name"
    assert b"FFMP" in before, "the fixture no longer carries a vendor code"

    scrub(path)

    out = read(path)
    assert b"libx264" not in out, "the compressorname survived"
    assert b"FFMP" not in out, "the vendor code survived"
    surviving = oracle_tags(path)
    assert "QuickTime:CompressorName" not in surviving
    assert "QuickTime:VendorID" not in surviving

    # MEASURED, and reported rather than asserted away: 'Lavc61.26.100' still
    # occurs once, at offset 10451 of this fixture, INSIDE mdat. It is the
    # x264 SEI user-data NAL in the video bitstream, which this engine does not
    # touch and cannot touch without re-encoding. That is Phase 3's, and it is
    # why no honest verdict for this format can be better than PARTIAL yet.
    # The assertion is that every surviving copy is in mdat, so a copy
    # reappearing anywhere in the box tree fails this test.
    mdat = next(node for node in walk(out) if node.type == b"mdat")
    start = 0
    outside = []
    while True:
        found = out.find(b"Lavc", start)
        if found == -1:
            break
        if not (mdat.payload_offset <= found < mdat.payload_offset + mdat.payload_len):
            outside.append(found)
        start = found + 1
    assert not outside, (
        f"an encoder name survived in the box tree at offsets {outside}; only "
        "the SEI copy inside mdat is a known residual")


def test_the_handler_name_is_zeroed(containers, tmp_path):
    """
    The hdlr name, a free string at the tail of a required box. ffmpeg writes
    'VideoHandler'; a device or an editor writes its own product name there.
    """
    path = stage(containers, "udta_gps", tmp_path)
    assert b"VideoHandler" in read(path)

    scrub(path)

    out = read(path)
    assert b"VideoHandler" not in out and b"SoundHandler" not in out
    assert "QuickTime:HandlerDescription" not in oracle_tags(path)


# ------------------------------------------------------------------ HEIF/AVIF


def test_the_heic_items_are_zeroed_and_the_picture_is_untouched(containers, tmp_path):
    """
    HEIC is not the hard part. You edit none of iinf, iloc or iref; you zero the
    bytes iloc already points at.

    Measured on a real HEIC container: Make, Model, timestamps and the whole
    XMP packet gone, decoded pixels bit identical, length unchanged to the byte.
    """
    require_heic(containers)
    path = stage(containers, "heic", tmp_path)
    before = read(path)
    before_frames = framemd5(path)

    reported = scrub(path)

    out = read(path)
    assert len(out) == len(before)
    assert framemd5(path) == before_frames, "the decoded image changed"
    assert not raw_contains(path, sentinel("isoheic"))
    assert not raw_contains(path, sentinel("isoheicxmp"))
    assert b"xmpmeta" not in out
    assert b"iPhone 11" not in out
    assert b"Exif\x00\x00" not in out[out.index(b"mdat"):], (
        "an Exif payload survived inside mdat")
    assert any("HEIF item" in line for line in reported), reported

    surviving = oracle_tags(path)
    leaked = [tag for tag in surviving
              if tag.startswith(("EXIF:", "XMP", "IPTC:", "ICC_Profile:",
                                 "ICC-header:"))]
    assert not leaked, leaked


def test_the_heif_item_table_survives_intact(containers, tmp_path):
    """
    OVERCORRECTION TEST, and the single most important one in this file.

    The top-level `meta` box in HEIF and AVIF is the FILE STRUCTURE. Measured:
    free-filling it made libheif answer "cannot identify image file" and
    ffprobe answer "moov atom not found". Free-filling `iinf` and `iref` while
    leaving the rest produced a file libheif could not identify while ffprobe
    exited 0 and printed nothing.

    "Remove every meta and ilst" is one edit away from "remove the picture",
    and that edit would still pass every removal test above.
    """
    require_heic(containers)
    path = stage(containers, "heic", tmp_path)
    required = {b"meta", b"iinf", b"iloc", b"iref", b"pitm", b"iprp", b"ipco",
                b"ipma", b"hvcC", b"ispe"}
    present = {node.type for node in walk(read(path))}
    assert required <= present, sorted(required - present)

    scrub(path)

    survivors = {node.type for node in walk(read(path))}
    missing = sorted(kind.decode() for kind in required if kind not in survivors)
    assert not missing, f"structural HEIF boxes were removed: {missing}"
    assert_decodes_cleanly(path, note="heic after scrub")


def test_the_icc_colr_goes_and_an_nclx_colr_stays(containers, tmp_path):
    """
    OVERCORRECTION TEST for the `colr` rule, in both directions.

    2.4 as corrected says to remove `colr` in HEIF and AVIF. Taken literally
    that removes an `nclx` colr too, which is three enumerations and a range
    flag: it cannot hold a string, it cannot carry identity, and dropping it
    changes how the image renders for no privacy gain. So the engine keys on
    the colour_type, and this asserts both halves.

    The HEIC's colr is 'prof', 552 bytes of ICC naming a device manufacturer, a
    profile date and a profile ID. The AVIF's is 'nclx'.
    """
    require_heic(containers)

    heic = stage(containers, "heic", tmp_path)
    data = read(heic)
    colr = [node for node in walk(data) if node.type == b"colr"]
    assert colr, "the HEIC fixture has no colr box"
    assert data[colr[0].payload_offset:colr[0].payload_offset + 4] == b"prof"
    scrub(heic)
    assert not [node for node in walk(read(heic)) if node.type == b"colr"], (
        "the ICC-carrying colr box survived")
    # 'acsp' is the ICC profile file signature, four bytes at offset 36 of
    # every ICC profile. Its absence is a stronger statement than any tag
    # check, and the tag check below is deliberately narrowed to the ICC
    # groups: 'Profile' as a substring also matches QuickTime:GeneralProfileIDC
    # and two other HEVC configuration numbers, which are the bitstream's own
    # parameters and have nothing to do with a colour profile.
    assert b"acsp" not in read(heic), "an ICC profile survived in the bytes"
    icc = [tag for tag in oracle_tags(heic)
           if tag.startswith(("ICC_Profile:", "ICC-header:"))]
    assert not icc, icc

    avif = stage(containers, "avif", tmp_path)
    data = read(avif)
    colr = [node for node in walk(data) if node.type == b"colr"]
    assert colr, "the AVIF fixture has no colr box"
    assert data[colr[0].payload_offset:colr[0].payload_offset + 4] == b"nclx"
    scrub(avif)
    survivors = [node for node in walk(read(avif)) if node.type == b"colr"]
    assert survivors, (
        "the nclx colr was removed. It carries no identity and removing it "
        "changes how the image renders.")
    assert_decodes_cleanly(avif, note="avif after scrub")


def test_an_item_extent_pointing_outside_mdat_is_refused(containers, tmp_path):
    """
    FAIL CLOSED. An item table can point anywhere, and zeroing what it says
    without checking where that is would let a malformed file steer an edit
    into its own `ftyp` or `moov`.

    The fixture edits one iloc extent offset to 0, which is the ftyp box. The
    edit is length-preserving, so nothing else about the file changes.
    """
    require_heic(containers)
    path = stage(containers, "heic", tmp_path)
    data = bytearray(read(path))
    iloc = next(node for node in walk(bytes(data)) if node.type == b"iloc")
    # iloc version 1, offset_size 4: the first extent offset of the FIRST item
    # sits at a fixed place in the payload. Rather than re-deriving the layout,
    # find the extent offset the engine reported and zero it wherever it is.
    from metascrub import isobmff

    boxes = isobmff.parse(bytes(data))
    meta = isobmff.item_metas(boxes)[0]
    items, primary = isobmff.item_table(bytes(data), meta)
    victim = next(item for item in items.values()
                  if item.item_type not in (b"hvc1", b"av01")
                  and item.item_id != primary)
    target = victim.extents[0][0]
    needle = struct.pack(">I", target)
    where = bytes(data).find(needle, iloc.payload_offset,
                             iloc.payload_offset + iloc.payload_len)
    assert where != -1, "could not locate the extent offset field to corrupt"
    data[where:where + 4] = b"\x00\x00\x00\x00"

    corrupted = str(tmp_path / "corrupt.heic")
    with open(corrupted, "wb") as handle:
        handle.write(bytes(data))
    pristine = read(corrupted)

    with pytest.raises(EngineError, match="not inside any mdat or idat"):
        scrub(corrupted)
    assert read(corrupted) == pristine, "the input was modified by a refusal"
    assert not [name for name in os.listdir(tmp_path) if name.startswith(".metascrub-")]


# ----------------------------------------------------------- fail-closed paths


def test_a_file_that_is_not_isobmff_is_refused(tmp_path):
    """
    Route by CONTENT, never by extension. A real `.heic` on this machine turned
    out to be a JPEG that Instagram had renamed, which is trap 8 again from the
    other end.
    """
    path = str(tmp_path / "pretend.heic")
    with open(path, "wb") as handle:
        handle.write(b"\xff\xd8\xff\xe0\x00\x10JFIF\x00" + b"\x00" * 200)
    pristine = read(path)

    with pytest.raises(EngineError, match="not 'ftyp'"):
        scrub(path)
    assert read(path) == pristine
    assert os.listdir(tmp_path) == ["pretend.heic"]


def test_a_truncated_box_is_refused(containers, tmp_path):
    """
    A box that declares more bytes than its parent holds. Truncating to what
    parsed would turn a corrupt input into a confident-looking output, which is
    the failure this project exists to refuse.
    """
    path = stage(containers, "faststart", tmp_path)
    data = bytearray(read(path))
    moov = next(node for node in walk(bytes(data)) if node.type == b"moov")
    struct.pack_into(">I", data, moov.offset, moov.size + 4096)
    with open(path, "wb") as handle:
        handle.write(bytes(data))
    pristine = read(path)

    with pytest.raises(EngineError, match="runs past the end"):
        scrub(path)
    assert read(path) == pristine
    assert not [name for name in os.listdir(tmp_path) if name.startswith(".metascrub-")]


def test_the_engine_is_available_and_needs_nothing_installed():
    """
    Pure Python. The stub reported False; the implementation reports True, and
    `metascrub doctor` reads exactly this.
    """
    ok, reason = IsobmffEngine().available()
    assert ok, reason
    assert not IsobmffEngine().supports_selective


def test_the_capabilities_rows_are_unchanged():
    """
    This engine is NOT wired in, and that is asserted rather than assumed.

    Flipping .mp4, .mov and .heic away from the engines that shipped is a
    separate decision with its own evidence. An accidental flip would change
    what every user of this tool runs, silently.
    """
    assert CAPABILITIES[".mp4"].engine is Engine.AV
    assert CAPABILITIES[".mov"].engine is Engine.AV
    assert CAPABILITIES[".heic"].engine is Engine.EXIFTOOL
    assert CAPABILITIES[".heif"].engine is Engine.EXIFTOOL
    assert CAPABILITIES[".avif"].engine is Engine.EXIFTOOL
    routed = sorted(ext for ext, row in CAPABILITIES.items()
                    if row.engine is Engine.ISOBMFF)
    assert not routed, (
        f"the isobmff engine is now routed for {routed}. That is a shipping "
        "decision, not a refactor: see the note at the top of the engine.")


# ---------------------------------------------------------------- mutation


def test_mutation_forgetting_the_uuid_carrier_is_caught(containers, tmp_path, monkeypatch):
    """
    THE MUTATION: implement the 2.4 table's first row and not its third, which
    is the exact shape of the bug the corrected section warns about.

    Everything else about the engine still works. The `udta` still goes, the
    file still decodes, the framemd5 is still identical. What survives is the
    XMP packet in the uuid box, and with it the GPS coordinates of every file
    exiftool has ever written, because exiftool's DEFAULT GPS write goes there
    and nowhere else.

    Only the structural gate catches it, which is the argument for having one.
    """
    path = stage(containers, "xmp_gps", tmp_path)
    monkeypatch.setattr(isobmff_engine, "REMOVED_TYPES",
                        frozenset({b"udta", b"ilst"}))
    scrub(path)

    out = read(path)
    assert b"xmpmeta" in out, (
        "the mutation did not actually change the behaviour, so this test "
        "proves nothing about the assertion below")
    assert raw_contains(path, sentinel("isoxmp"))

    with pytest.raises(AssertionError, match="uuid box survived"):
        assert_structurally_clean(path, note="mutation")


def test_mutation_leaving_free_box_contents_alone_is_caught(containers, tmp_path, monkeypatch):
    """
    THE SECOND MUTATION: treat `free` and `skip` as inert padding rather than
    as a place a previous editor abandoned data.

    Caught by the free-payload clause of the gate, and by nothing else: the
    orphaned box is not a udta any more, exiftool reports nothing for it, and
    so the residual byte search is never given a needle for its contents.
    """
    path = stage(containers, "orphan_free", tmp_path)
    monkeypatch.setattr(isobmff_engine, "FREE_SPACE_TYPES", frozenset())
    scrub(path)

    assert raw_contains(path, sentinel("isoorphan")), (
        "the mutation did not change the behaviour")

    with pytest.raises(AssertionError, match="non-zero bytes"):
        assert_structurally_clean(path, note="mutation")
