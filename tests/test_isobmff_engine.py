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

  7. THE BITSTREAM AND THE AUDIO TRACK. Added when Phase 3 landed; see the
     section headed PHASE 3 near the end of this file, which carries its own
     parsers, its own fixtures and its own account of what is proven.

WHAT IS NOT ASSERTED, ON PURPOSE

No test here asserts anything about a video bitstream that is not H.264 or
H.265. An AV1 or VP9 track carries the same class of value in structures this
engine does not parse, and nothing in this file would notice one.

No test here runs the file through `MetadataScrubber`. That was because the
engine was not wired into CAPABILITIES at all; since 2026-09-07 the .heic,
.heif and .avif rows route here, and the end-to-end pipeline tests for those
three live in tests/test_heic_routing.py rather than being bolted onto this
file. Everything below still drives the engine directly, which is what keeps
these assertions about the ENGINE and not about the routing.
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

    # CORRECTED 2026-09-06, when Phase 3 landed. This comment used to say the
    # surviving 'Lavc61.26.100' inside mdat was the x264 SEI user-data NAL and
    # that removing it needed a re-encode. Both halves were wrong. It does not
    # need a re-encode, it is gone now, and the copy that survives in THIS
    # fixture is a different carrier entirely: ffmpeg's AAC encoder writes
    # 'Lavc61.26.100' into a data stream element in the first AUDIO sample.
    # That one is only removed by the opt-in audio removal, and it has its own
    # test, `test_the_aac_encoder_signature_goes_with_the_audio_and_not_before`.
    # The assertion below is unchanged and still worth keeping: every surviving
    # copy must be inside mdat, so a copy reappearing anywhere in the box tree
    # fails this test.
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


def test_only_the_still_image_rows_are_routed_to_this_engine():
    """
    WHICH rows this engine is wired into, asserted rather than assumed.

    It used to assert that NO row was wired in. On 2026-09-07 .heic, .heif and
    .avif were routed here, because exiftool cannot remove a HEIF ICC profile:
    it lives in iprp/ipco/colr, an item PROPERTY rather than a metadata item,
    and three real iPhone HEICs came back status=error with the Apple Display
    P3 strings still in the output bytes. The evidence, the COMPLETE decision
    and the end-to-end proof are in tests/test_heic_routing.py and in the
    ISOBMFF block of capabilities.py.

    The guard is still a guard, and it is the same one. Flipping .mp4 and .mov
    away from the ffmpeg engine that shipped is a separate decision with its own
    evidence, and an accidental flip of ANY other row would change what every
    user of this tool runs, silently. So the routed set is pinned exactly rather
    than merely being required to be non-empty.
    """
    assert CAPABILITIES[".mp4"].engine is Engine.AV
    assert CAPABILITIES[".mov"].engine is Engine.AV
    routed = sorted(ext for ext, row in CAPABILITIES.items()
                    if row.engine is Engine.ISOBMFF)
    assert routed == [".avif", ".heic", ".heif"], (
        f"the isobmff engine is now routed for {routed}. Adding or removing a "
        "row there is a shipping decision, not a refactor: see the note at the "
        "top of the engine and the ISOBMFF block in capabilities.py.")


# ------------------------------------------------------- the box inventory
#
# THE DEFECT THIS EXISTS FOR, stated once so nobody has to guess later.
#
# On 2026-09-07 a real Galaxy S10+ HEIC was found to carry a top-level `sefd`
# box, Samsung Extended Format Data, holding a capture wall-clock time and a
# mobile country code. It survived a run that reported sanitized and
# verified_clean, because no rule in this engine mentioned that box type and
# nothing anywhere required one to. The file decoded, the structural
# post-conditions held, and the residual scan could not see either value.
#
# The bug was therefore not a broken rule. It was the ABSENCE of an opinion,
# which nothing can fail on. isobmff_engine.DECIDED_TYPES makes the absence
# visible: every box type met at the top level or directly under `moov`, with
# what happens to it. This test walks the fixtures with the INDEPENDENT parser
# at the top of this file, not with isobmff.parse, and fails on a type the
# table has never heard of. The real-device half of the sweep is in
# tests/test_heic_routing.py.


def surface_types(data):
    """Every box type at the top level or directly under `moov`, per walk()."""
    nodes = walk(data)
    return {node.type for node in nodes
            if node.depth == 0
            or (node.depth == 1 and node.parent is not None
                and node.parent.type == b"moov")}


@pytest.mark.parametrize("kind", ALL_ISOBMFF)
def test_every_box_type_in_the_fixtures_is_one_the_engine_has_decided(
        kind, containers):
    """
    An undecided box type is the `sefd` defect one name over.

    A failure here means somebody owes a decision, not that the engine is
    broken: read what the box carries, then remove it, zero it, or record it in
    DECIDED_TYPES as structural with the measurement written down. Adding the
    name to the table to turn this green, with no look at the payload, is the
    one response that reintroduces the bug it was written for.
    """
    if kind not in containers:
        pytest.skip(f"no {kind} fixture on this machine")
    undecided = sorted(
        kind_bytes.decode("latin-1")
        for kind_bytes in surface_types(read(containers[kind]))
        if kind_bytes not in isobmff_engine.DECIDED_TYPES)
    assert undecided == [], (
        f"the {kind} fixture carries box types this engine has no opinion "
        f"about: {undecided}")


@pytest.mark.parametrize("kind", ALL_ISOBMFF)
def test_the_output_carries_no_box_type_the_table_calls_removed(
        kind, containers, tmp_path):
    """
    The table's `removed` rows, checked against the OUTPUT with the independent
    parser rather than against the engine's own post-conditions.

    _assert_clean already makes a version of this check, and says of itself
    that it re-reads the output with the walker that wrote it, so it buys
    fail-closed behaviour and not evidence. This is the same claim measured
    with a different parser, which is what makes it evidence.
    """
    if kind not in containers:
        pytest.skip(f"no {kind} fixture on this machine")
    removed = {kind_bytes
               for kind_bytes, decision in isobmff_engine.DECIDED_TYPES.items()
               if decision == isobmff_engine.DECISION_REMOVED}
    path = stage(containers, kind, tmp_path)
    scrub(path)
    survivors = sorted(
        kind_bytes.decode("latin-1")
        for kind_bytes in surface_types(read(path)) & removed)
    assert survivors == [], f"{kind} output still carries {survivors}"


def test_a_wide_box_carrying_a_payload_is_zeroed_like_a_free_box(
        containers, tmp_path):
    """
    `wide` is QuickTime's reserved-space placeholder, and it was decided in the
    same sweep that found `sefd`: a box type the engine had no opinion about.

    Every `wide` in the corpus is header-only, so the rule is a no-op on real
    files and asserting it there would assert nothing. This builds the case the
    name and the content disagree about instead: the orphaned tag strings from
    the `orphan_free` fixture, renamed to `wide`. Same construction as
    test_mutation_leaving_free_box_contents_alone_is_caught, aimed at the type
    that was undecided.

    The rename keeps the payload and the length, so nothing moves, and the
    sentinel is checked to be inside the renamed box BEFORE the scrub. A
    fixture that lost its sentinel would make this pass while measuring
    nothing.
    """
    path = stage(containers, "orphan_free", tmp_path)
    value = sentinel("isoorphan").encode()

    data = bytearray(read(path))
    renamed = 0
    for node in walk(bytes(data)):
        if node.type != b"free":
            continue
        payload = bytes(data[node.payload_offset:
                             node.payload_offset + node.payload_len])
        if value in payload:
            data[node.offset + 4:node.offset + 8] = b"wide"
            renamed += 1
    assert renamed == 1, (
        f"expected exactly one free box holding the sentinel, found {renamed}")
    with open(path, "wb") as handle:
        handle.write(bytes(data))

    before = read(path)
    assert value in before, "the rename lost the sentinel"
    assert any(node.type == b"wide" for node in walk(before)), (
        "the rename did not produce a wide box")
    length = len(before)

    scrub(path)

    after = read(path)
    assert value not in after, (
        "the orphaned tag strings survived inside a `wide` box. exiftool "
        "reports nothing for that box, so the residual scan is never given a "
        "needle for its contents and would call this file clean.")
    assert len(after) == length, "zeroing a wide box changed the file length"
    for node in walk(after):
        if node.type == b"wide":
            assert not any(
                after[node.payload_offset:
                      node.payload_offset + node.payload_len]), (
                f"the wide box at offset {node.offset} still holds non-zero "
                "bytes")


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


# ===========================================================================
# PHASE 3: THE VIDEO BITSTREAM, AND THE AUDIO TRACK
# ===========================================================================
#
# THE PARSERS BELOW ARE A SECOND IMPLEMENTATION, FOR THE SAME REASON `walk` IS
#
# `nal_units`, `rbsp_of`, `sei_list` and `video_samples` do not import
# `metascrub.isobmff`. They are written from ISO/IEC 14496-12 (the sample
# table), ISO/IEC 14496-15 (the configuration records) and H.264/H.265 Annex D
# (the SEI message syntax), and they exist so that "the encoder signature is
# gone" is a statement the test makes about the bytes rather than a statement
# the engine makes about itself.
#
# WHAT IS BEING PROVEN HERE, AND WHY EACH ONE IS SEPARATE
#
#  1. THE SIGNATURE IS GONE FROM THE WHOLE FILE, the length is unchanged, and a
#     full decode produces a BIT IDENTICAL framemd5. ffprobe appears nowhere,
#     for the reason at the top of this file.
#  2. TWO CODECS, TWO PLACES. Measured 2026-09-06: x264 puts its SEI in `mdat`
#     and x265 puts its SEI in the `hvcC` configuration record inside the
#     sample entry. An engine that scans one of those and not the other is
#     right about one codec and silently wrong about the other, so both
#     locations get their own assertion.
#  3. THE REPLACEMENT IS A CONFORMANT SEI, not a hole: the output NAL is still
#     an SEI NAL, its messages are all filler_payload, every filler byte is
#     0xFF, and the whole NAL contains no 00 00 pair, which is what makes the
#     emulation prevention question moot.
#  4. EMULATION PREVENTION, on a real file rather than in the abstract. A
#     00 00 03 is injected into the middle of the x264 payload and the whole
#     acceptance set is re-run over it.
#  5. OVERCORRECTION, three ways: a video with no SEI at all keeps every sample
#     byte, an SEI that is not user-data is left alone, and audio is bit
#     identical unless removal is asked for.
#  6. MUTATION, one per feature.


def _have_encoder(name):
    proc = _run(["ffmpeg", "-hide_banner", "-encoders"])
    return proc.returncode == 0 and name.encode() in proc.stdout


# --------------------------------------------------- an independent NAL reader


def nal_units(buf, length_size):
    """[(offset, length)] of the length-prefixed NAL units tiling `buf`."""
    out = []
    offset = 0
    while offset < len(buf):
        assert offset + length_size <= len(buf), "a NAL length field is cut off"
        size = int.from_bytes(buf[offset:offset + length_size], "big")
        offset += length_size
        assert size and offset + size <= len(buf), (
            f"a NAL at {offset} declares {size} bytes, past the end of the buffer")
        out.append((offset, size))
        offset += size
    return out


def rbsp_of(nal):
    """A NAL's payload with emulation prevention bytes removed."""
    out = bytearray()
    index = 0
    while index < len(nal):
        if (index + 2 < len(nal) and nal[index] == 0 and nal[index + 1] == 0
                and nal[index + 2] == 3):
            out += nal[index:index + 2]
            index += 3
            continue
        out.append(nal[index])
        index += 1
    return bytes(out)


def sei_list(payload):
    """
    [(payload_type, payload_size, payload_bytes)] of one SEI NAL's RBSP.

    Written from the sei_message() syntax: the type and the size are each a run
    of 0xFF bytes worth 255 apiece plus one final byte, and the list ends at
    the rbsp_trailing_bits byte 0x80.
    """
    out = []
    index = 0
    while index < len(payload):
        if payload[index] == 0x80:
            break
        kind = 0
        while payload[index] == 0xFF:
            kind += 255
            index += 1
        kind += payload[index]
        index += 1
        size = 0
        while payload[index] == 0xFF:
            size += 255
            index += 1
        size += payload[index]
        index += 1
        assert index + size <= len(payload), "an SEI message runs past its NAL"
        out.append((kind, size, payload[index:index + size]))
        index += size
    return out


def annexb_units(buf):
    """
    [(offset, length)] of the NAL units of an Annex B byte stream.

    Arrived at differently from `isobmff.annexb_nals` on purpose, which is the
    same discipline `walk` follows for the box tree: this one finds each NAL's
    END by searching FORWARD from its start for the next 00 00 01, where the
    engine's derives the end from the next start code's recorded position. The
    two readings are asserted equal in the Annex B test, so a mistake in either
    is a disagreement rather than a shared blind spot.

    That is not hypothetical here. The first version of the engine's reader
    backed off only the ZERO bytes before the next NAL and left the 0x01
    attached, reporting a 686-byte SEI as 690 bytes. Overwriting all 690 wiped
    the SPS that followed it and ffmpeg answered "non-existing PPS 0
    referenced" on every frame. The decode assertion caught it; no structural
    check would have.
    """
    out = []
    index = 0
    while index + 3 <= len(buf):
        width = 0
        if buf[index] == 0 and buf[index + 1] == 0:
            if buf[index + 2] == 1:
                width = 3
            elif (buf[index + 2] == 0 and index + 4 <= len(buf)
                    and buf[index + 3] == 1):
                width = 4
        if not width:
            index += 1
            continue
        begin = index + width
        finish = begin
        while finish + 3 <= len(buf):
            if (buf[finish] == 0 and buf[finish + 1] == 0
                    and buf[finish + 2] == 1):
                break
            finish += 1
        else:
            finish = len(buf)
        while finish > begin and buf[finish - 1] == 0:
            finish -= 1
        if finish > begin:
            out.append((begin, finish - begin))
        index = begin
    return out


# ------------------------------------------- an independent sample table reader
#
# Non-fragmented files only, which is all the tests that need byte-level sample
# locations use. The fragmented assertions are made over the whole file instead,
# where no sample table is needed to state them.


def _fullbox_u32(data, node, relative):
    return struct.unpack_from(">I", data, node.payload_offset + relative)[0]


def track_nodes(data, nodes):
    """[(handler, trak node)] for every track in the file."""
    out = []
    for node in nodes:
        if node.type != b"trak":
            continue
        mdia = next(c for c in node.children if c.type == b"mdia")
        hdlr = next(c for c in mdia.children if c.type == b"hdlr")
        out.append((bytes(data[hdlr.payload_offset + 8:hdlr.payload_offset + 12]),
                    node))
    return out


def stbl_samples(data, trak):
    """[(absolute offset, size)] for one track, from stsz, stsc and stco/co64."""
    mdia = next(c for c in trak.children if c.type == b"mdia")
    minf = next(c for c in mdia.children if c.type == b"minf")
    stbl = next(c for c in minf.children if c.type == b"stbl")
    table = {c.type: c for c in stbl.children}
    stsz = table[b"stsz"]
    fixed = _fullbox_u32(data, stsz, 4)
    count = _fullbox_u32(data, stsz, 8)
    sizes = ([fixed] * count if fixed
             else [_fullbox_u32(data, stsz, 12 + 4 * i) for i in range(count)])
    stsc = table[b"stsc"]
    runs = [(_fullbox_u32(data, stsc, 8 + 12 * i),
             _fullbox_u32(data, stsc, 12 + 12 * i))
            for i in range(_fullbox_u32(data, stsc, 4))]
    chunks_box = table.get(b"stco") or table[b"co64"]
    width = 8 if chunks_box.type == b"co64" else 4
    chunk_count = _fullbox_u32(data, chunks_box, 4)
    chunks = [int.from_bytes(
        data[chunks_box.payload_offset + 8 + width * i:
             chunks_box.payload_offset + 8 + width * (i + 1)], "big")
        for i in range(chunk_count)]

    out = []
    index = 0
    for position, chunk_offset in enumerate(chunks):
        per_chunk = 0
        for first_chunk, samples_per_chunk in runs:
            if first_chunk - 1 <= position:
                per_chunk = samples_per_chunk
        offset = chunk_offset
        for _ in range(per_chunk):
            if index >= len(sizes):
                break
            out.append((offset, sizes[index]))
            offset += sizes[index]
            index += 1
    return out


def config_record(data, trak):
    """
    (name, nal length size, [(offset, length)] of the NALs the record stores).

    Reached by walking the child boxes that follow a VisualSampleEntry's fixed
    78-byte prefix, and by reading `lengthSizeMinusOne` out of the record
    rather than assuming a 4-byte NAL length field.
    """
    mdia = next(c for c in trak.children if c.type == b"mdia")
    minf = next(c for c in mdia.children if c.type == b"minf")
    stbl = next(c for c in minf.children if c.type == b"stbl")
    stsd = next(c for c in stbl.children if c.type == b"stsd")
    for entry in stsd.children:
        cursor = entry.payload_offset + 78
        end = entry.offset + entry.size
        while cursor + 8 <= end:
            size = struct.unpack_from(">I", data, cursor)[0]
            kind = bytes(data[cursor + 4:cursor + 8])
            body = cursor + 8
            if kind == b"avcC":
                length_size = (data[body + 4] & 0x03) + 1
                nals = []
                at = body + 5
                for mask in (0x1F, 0xFF):
                    count = data[at] & mask
                    at += 1
                    for _ in range(count):
                        span = struct.unpack_from(">H", data, at)[0]
                        nals.append((at + 2, span))
                        at += 2 + span
                return "avcC", length_size, nals
            if kind == b"hvcC":
                length_size = (data[body + 21] & 0x03) + 1
                nals = []
                at = body + 23
                for _ in range(data[body + 22]):
                    at += 1
                    count = struct.unpack_from(">H", data, at)[0]
                    at += 2
                    for _ in range(count):
                        span = struct.unpack_from(">H", data, at)[0]
                        nals.append((at + 2, span))
                        at += 2 + span
                return "hvcC", length_size, nals
            cursor += size
    return None, None, []


def sei_nals(data, ranges, length_size, hevc):
    """
    [(offset, length, header length, [(type, size, bytes)])] for every SEI NAL.

    An SEI NAL is type 6 in H.264 (one header byte) and 39 or 40 in H.265 (two).
    """
    out = []
    for start, span in ranges:
        for offset, length in nal_units(data[start:start + span], length_size):
            offset += start
            first = data[offset]
            if hevc:
                hit = ((first >> 1) & 0x3F) in (39, 40)
                header = 2
            else:
                hit = (first & 0x1F) == 6
                header = 1
            if not hit:
                continue
            out.append((offset, length, header,
                        sei_list(rbsp_of(data[offset + header:offset + length]))))
    return out


def all_sei_nals(path, hevc):
    """
    Every SEI NAL of the first video track: in mdat, and in the config record.

    Both, because the two codecs measured here put their signature in different
    ones and a reader that looks in one place proves nothing about the other.
    """
    data = read(path)
    nodes = walk(data)
    trak = next(node for handler, node in track_nodes(data, nodes)
                if handler == b"vide")
    name, length_size, config_nals = config_record(data, trak)
    assert name is not None, "the video track has no avcC or hvcC"
    out = []
    for offset, length in config_nals:
        first = data[offset]
        if hevc:
            hit = ((first >> 1) & 0x3F) in (39, 40)
            header = 2
        else:
            hit = (first & 0x1F) == 6
            header = 1
        if hit:
            out.append(("config", offset, length, header,
                        sei_list(rbsp_of(data[offset + header:offset + length]))))
    for entry in sei_nals(data, stbl_samples(data, trak), length_size, hevc):
        out.append(("mdat",) + entry)
    return out


ENCODER_NEEDLES = (b"x264 - core", b"x265 (build", b"options:",
                   b"videolan.org", b"H.264/MPEG-4 AVC codec")


def assert_no_encoder_signature(path, note=""):
    """No encoder version or settings string anywhere in the file."""
    data = read(path)
    found = [needle for needle in ENCODER_NEEDLES if needle in data]
    assert not found, f"{note}: the encoder signature survived: {found}"


def assert_video_decodes_cleanly(path, note=""):
    """
    A full decode of the VIDEO stream only, warnings treated as failures.

    Separate from `assert_decodes_cleanly` because `-f null` with an audio
    stream that now decodes to nothing prints
    'No filtered frames for output stream, trying to initialize anyway.'
    That is the null MUXER remarking on an empty audio output, not a decode
    error, and measured on a file whose audio was deliberately emptied. Rather
    than widen the general assertion to tolerate a string, the audio tests
    assert the video decode is clean and assert the audio content separately.
    """
    proc = _run(["ffmpeg", "-v", "warning", "-i", path, "-map", "0:v",
                 "-f", "null", "-"])
    stderr = proc.stderr.decode(errors="replace").strip()
    assert proc.returncode == 0 and not stderr, (
        f"{note}: the video did not decode cleanly (exit {proc.returncode})\n"
        f"{stderr}")


def video_framemd5(path):
    """
    The decoded frame hashes of the VIDEO stream only.

    `framemd5` above hashes every stream, which is the right assertion
    everywhere else in this file and the WRONG one for audio removal: the audio
    frames are supposed to change, so comparing all of them would fail for the
    reason the feature exists. This isolates the claim that actually matters,
    that no decoded video pixel moved.
    """
    proc = _run(["ffmpeg", "-v", "error", "-i", path, "-map", "0:v",
                 "-f", "framemd5", "-"])
    assert proc.returncode == 0, (
        f"ffmpeg could not decode the video of {path}: "
        f"{proc.stderr.decode(errors='replace')}")
    lines = [line for line in proc.stdout.decode().splitlines()
             if line and not line.startswith("#")]
    assert lines, f"ffmpeg decoded no video frames at all from {path}"
    return lines


def audio_md5(path):
    """The md5 of the DECODED audio, or None when the file has no audio."""
    proc = _run(["ffmpeg", "-v", "error", "-i", path, "-map", "0:a",
                 "-f", "md5", "-"])
    if proc.returncode != 0:
        return None
    return proc.stdout.decode().strip()


EMPTY_MD5 = "MD5=d41d8cd98f00b204e9800998ecf8427e"


# ------------------------------------------------------------ Phase 3 fixtures


HAVE_LIBX265 = None


@pytest.fixture(scope="session")
def bitstreams(tmp_path_factory):
    """
    Fixtures for the bitstream and audio work, kept out of `containers`.

    Separate because `containers` is the parametrize source for the gate,
    decode and oracle tests, and adding rows there would silently change what
    those assert. These are built by ffmpeg and CHECKED: a fixture that failed
    to carry the signature it promises would give every test below nothing to
    remove and they would all pass.
    """
    if not HAVE_FFMPEG:
        pytest.skip("ffmpeg not available; cannot build bitstream fixtures")
    root = tmp_path_factory.mktemp("isobmff-bitstream")
    paths = {}

    def path(name):
        return str(root / name)

    source = ["-f", "lavfi", "-i", "testsrc=size=320x240:rate=15:duration=1"]
    audio = ["-f", "lavfi", "-i", "sine=frequency=440:duration=1"]

    # 1. H.264. MEASURED: the x264 SEI user-data NAL lands in mdat, as the
    #    first NAL of the first sample.
    h264 = path("x264.mp4")
    _ffmpeg(source + ["-c:v", "libx264", "-pix_fmt", "yuv420p", h264])
    assert b"x264 - core" in read(h264), (
        "the H.264 fixture carries no x264 signature, so it proves nothing")
    paths["h264"] = h264

    # 2. H.264 with the SEI removed by a bitstream filter that is not this
    #    engine. The OVERCORRECTION fixture: there is nothing here to remove.
    nosei = path("x264_no_sei.mp4")
    _ffmpeg(["-i", h264, "-c", "copy", "-bsf:v", "filter_units=remove_types=6",
             nosei])
    assert b"options:" not in read(nosei), (
        "filter_units did not actually remove the SEI NAL, so the "
        "overcorrection fixture is not what it claims to be")
    paths["h264_no_sei"] = nosei

    # 3. H.265, if this ffmpeg can encode it. MEASURED, and it is the whole
    #    reason the configuration record is scanned: the x265 SEI is NOT in
    #    mdat, it is in hvcC inside the hvc1 sample entry.
    global HAVE_LIBX265
    HAVE_LIBX265 = _have_encoder("libx265")
    if HAVE_LIBX265:
        h265 = path("x265.mp4")
        _ffmpeg(source + ["-c:v", "libx265", "-pix_fmt", "yuv420p", h265])
        assert b"x265 (build" in read(h265), (
            "the H.265 fixture carries no x265 signature")
        paths["h265"] = h265

    # 4. H.264 plus AAC, so the SEI walk has to survive an mdat holding two
    #    tracks' samples interleaved, and so audio removal has something to
    #    remove.
    both = path("x264_aac.mp4")
    _ffmpeg(source + audio + ["-c:v", "libx264", "-pix_fmt", "yuv420p",
                              "-c:a", "aac", both])
    paths["aac"] = both

    # 5. H.264 plus PCM in a QuickTime container. Its stsz uses the FIXED form,
    #    which has no per-sample table to zero, and that is a different code
    #    path from every other fixture here.
    pcm = path("x264_pcm.mov")
    _ffmpeg(source + audio + ["-c:v", "libx264", "-pix_fmt", "yuv420p",
                              "-c:a", "pcm_s16le", pcm])
    paths["pcm"] = pcm

    # 6. Fragmented, with audio. Audio removal must REFUSE this one.
    frag = path("fragmented_aac.mp4")
    _ffmpeg(source + audio + ["-c:v", "libx264", "-pix_fmt", "yuv420p",
                              "-c:a", "aac",
                              "-movflags", "+frag_keyframe+empty_moov", frag])
    paths["fragmented_aac"] = frag

    return paths


def stage_file(paths, name, tmp_path):
    target = str(tmp_path / os.path.basename(paths[name]))
    shutil.copyfile(paths[name], target)
    return target


def require_hevc(bitstreams):
    if "h265" not in bitstreams:
        pytest.skip("this ffmpeg has no libx265; the HEVC path is NOT MEASURED "
                    "on this machine")
    return bitstreams["h265"]


# ------------------------------------------------------- the filler, in the pure


def test_the_sei_filler_tiles_every_length_it_can_be_asked_for():
    """
    `sei_filler_bytes(n)` returns exactly n bytes, for every n it can be given.

    Not an illustrative example: the cost of one SEI message is
    size + size // 255 + 2, which SKIPS a value every 256 bytes, so there are
    lengths no single message can fill and the function has to reach for a
    second one. A test over one convenient length would never meet a gap. The
    range below covers three of them (257, 513, 769) plus every value between.
    """
    from metascrub import isobmff

    for length in list(range(3, 3000)) + [4096, 65535, 65536, 100003]:
        blob = isobmff.sei_filler_bytes(length)
        assert len(blob) == length, length
        assert blob[-1] == 0x80, f"{length}: no rbsp_trailing_bits"
        messages = sei_list(blob)
        assert messages, length
        for kind, size, body in messages:
            assert kind == 3, f"{length}: payload type {kind} is not filler"
            assert body == b"\xff" * size, f"{length}: a filler byte is not 0xFF"
        consumed = sum(1 + (size // 255 + 1) + size for _k, size, _b in messages)
        assert consumed == length - 1, length


def test_the_sei_filler_never_needs_an_emulation_prevention_byte():
    """
    THE INVARIANT THE WHOLE REWRITE RESTS ON, asserted rather than argued.

    A NAL payload is an escaped RBSP: 00 00 00, 00 00 01, 00 00 02 and 00 00 03
    are all illegal and a 0x03 is inserted to break them up. Inserting one
    lengthens the buffer, and this engine's single invariant is that no length
    ever changes. The replacement is safe only because it can never contain two
    adjacent zero bytes, which is a property of the bytes it emits and is
    checked here over every length rather than reasoned about once.
    """
    from metascrub import isobmff

    for length in list(range(3, 3000)) + [65535, 100003]:
        blob = isobmff.sei_filler_bytes(length)
        assert b"\x00\x00" not in blob, length
        # And therefore no start code emulation of any kind.
        for pattern in (b"\x00\x00\x00", b"\x00\x00\x01", b"\x00\x00\x02",
                        b"\x00\x00\x03"):
            assert pattern not in blob, (length, pattern)


def test_a_length_too_short_for_an_sei_message_is_refused():
    """Fail closed. Two bytes cannot hold a message and its trailing bits."""
    from metascrub import isobmff
    from metascrub.isobmff import IsobmffError

    for length in (0, 1, 2):
        with pytest.raises(IsobmffError):
            isobmff.sei_filler_bytes(length)


def test_the_nal_length_field_size_is_read_and_not_assumed():
    """
    A configuration record may declare a 1, 2 or 4 byte NAL length field.

    An engine that assumes 4 reads a sample as garbage from its first byte, and
    the garbage still looks like a NAL chain often enough to be dangerous. This
    walks the same three NALs at all three widths and asserts the wrong width
    is REFUSED rather than silently misread.
    """
    from metascrub import isobmff
    from metascrub.isobmff import IsobmffError

    payloads = [b"\x06\x05\x10" + b"a" * 16, b"\x65hello", b"\x41world"]
    for width in (1, 2, 4):
        buf = b"".join(len(p).to_bytes(width, "big") + p for p in payloads)
        found = isobmff.length_prefixed_nals(buf, 0, len(buf), width)
        assert [buf[o:o + n] for o, n in found] == payloads, width

    four = b"".join(len(p).to_bytes(4, "big") + p for p in payloads)
    with pytest.raises(IsobmffError):
        isobmff.length_prefixed_nals(four, 0, len(four), 1)
    with pytest.raises(IsobmffError):
        isobmff.length_prefixed_nals(four, 0, len(four), 3)


def test_an_sei_carrying_no_user_data_is_not_selected():
    """
    OVERCORRECTION, at the level where it is cheapest to state.

    A pic_timing SEI is not an encoder signature and must not be rewritten.
    Asserted for both NAL header shapes, because H.264 spends one byte on its
    header and H.265 spends two, and an off-by-one there reads the payload type
    out of the header.
    """
    from metascrub import isobmff

    def sei(header, kind, size):
        return header + bytes([kind, size]) + b"\xaa" * size + b"\x80"

    for header, hevc in ((b"\x06", False), (b"\x4e\x01", True)):
        harmless = sei(header, 1, 20)          # pic_timing
        signature = sei(header, 5, 20)         # user_data_unregistered
        buf = (len(harmless).to_bytes(4, "big") + harmless
               + len(signature).to_bytes(4, "big") + signature)
        nals = isobmff.length_prefixed_nals(buf, 0, len(buf), 4)
        assert len(nals) == 2
        hits = isobmff.sei_user_data_nals(buf, nals, hevc)
        assert len(hits) == 1, (hevc, hits)
        assert hits[0][0] == nals[1][0], (
            "the wrong NAL was selected: the pic_timing one is not a signature")
        assert hits[0][2] == len(header)


def test_emulation_prevention_bytes_are_removed_before_the_sei_is_parsed():
    """
    A user-data SEI whose payload happens to contain 00 00 03 is still found.

    The 0x03 is an ESCAPE, not a payload byte. A parser that reads the escaped
    bytes straight through counts it toward the payload size, runs one byte
    long, and either misses the message or reads the next one at the wrong
    offset.
    """
    from metascrub import isobmff

    body = b"\x11" * 4 + b"\x00\x00\x03\x00" + b"\x22" * 12
    raw = isobmff.unescape_rbsp(body, 0, len(body))
    assert raw == b"\x11" * 4 + b"\x00\x00\x00" + b"\x22" * 12
    assert len(raw) == len(body) - 1

    escaped = b"\x06\x05" + bytes([len(raw)]) + body + b"\x80"
    buf = len(escaped).to_bytes(4, "big") + escaped
    nals = isobmff.length_prefixed_nals(buf, 0, len(buf), 4)
    hits = isobmff.sei_user_data_nals(buf, nals, False)
    assert len(hits) == 1, (
        "the message was missed because its escape byte was counted as payload")


# ---------------------------------------------------------------- acceptance


@pytest.mark.parametrize("kind", ["h264", "h265"])
def test_the_encoder_signature_is_removed_with_no_re_encode(bitstreams, kind,
                                                            tmp_path):
    """
    THE ACCEPTANCE CRITERION, in full.

    The version string is gone from the WHOLE file, the file length is
    unchanged, the file decodes cleanly, and a full decode produces a
    framemd5 BIT IDENTICAL to the input's. ffprobe is not consulted: measured
    by the Phase 0 team, it exited 0 with plausible stream and frame counts on
    a file whose every frame was garbage.
    """
    if kind == "h265":
        require_hevc(bitstreams)
    path = stage_file(bitstreams, kind, tmp_path)
    before = read(path)
    before_frames = framemd5(path)
    needle = b"x264 - core" if kind == "h264" else b"x265 (build"
    assert needle in before, "the fixture carries no signature to remove"

    reported = scrub(path)

    out = read(path)
    assert len(out) == len(before), (
        f"{kind}: the length changed from {len(before)} to {len(out)}. Every "
        "sample offset in the file is now wrong.")
    assert_no_encoder_signature(path, note=kind)
    assert framemd5(path) == before_frames, (
        f"{kind}: the decoded frames changed. Removing an SEI must not touch "
        "one decoded pixel.")
    assert_decodes_cleanly(path, note=f"{kind} after the SEI rewrite")
    assert any("SEI user-data" in line for line in reported), reported


def test_the_hevc_signature_is_in_the_configuration_record_and_not_in_mdat(
        bitstreams, tmp_path):
    """
    THE MEASUREMENT THAT MAKES THIS FEATURE TWO FEATURES, asserted on the file.

    x265 muxed by ffmpeg writes its SEI user-data NAL into the `hvcC` arrays
    inside the `hvc1` sample entry, which lives in `moov`. There is no copy in
    mdat at all. An implementation that scans the bitstream and not the
    configuration record finds nothing here and reports success.
    """
    require_hevc(bitstreams)
    path = stage_file(bitstreams, "h265", tmp_path)

    before = all_sei_nals(path, hevc=True)
    carriers = [where for where, _o, _l, _h, messages in before
                if any(kind == 5 for kind, _s, _b in messages)]
    assert carriers == ["config"], (
        f"the x265 signature is no longer in the configuration record alone "
        f"({carriers}); this fixture no longer demonstrates the split")

    scrub(path)

    after = all_sei_nals(path, hevc=True)
    assert not [1 for _w, _o, _l, _h, messages in after
                for kind, _s, _b in messages if kind == 5], (
        "a user_data_unregistered SEI survived in the configuration record")
    assert_no_encoder_signature(path, note="h265")


def test_the_h264_signature_is_in_mdat_and_not_in_the_configuration_record(
        bitstreams, tmp_path):
    """
    The other half of the same measurement. x264's SEI is a real NAL in a real
    sample, located through stsz, stsc and stco, and `avcC` carries only the
    SPS and the PPS.
    """
    path = stage_file(bitstreams, "h264", tmp_path)

    before = all_sei_nals(path, hevc=False)
    carriers = [where for where, _o, _l, _h, messages in before
                if any(kind == 5 for kind, _s, _b in messages)]
    assert carriers == ["mdat"], (
        f"the x264 signature is no longer in mdat alone ({carriers})")

    scrub(path)

    after = all_sei_nals(path, hevc=False)
    assert not [1 for _w, _o, _l, _h, messages in after
                for kind, _s, _b in messages if kind == 5]
    assert_no_encoder_signature(path, note="h264")


@pytest.mark.parametrize("kind,hevc", [("h264", False), ("h265", True)])
def test_the_rewritten_nal_is_a_conformant_filler_sei(bitstreams, kind, hevc,
                                                      tmp_path):
    """
    The replacement is a valid SEI message, not a hole punched in a bitstream.

    Read back with the independent parser above: the NAL is still an SEI NAL of
    the same length, every message in it is filler_payload (type 3), every
    filler byte is 0xFF as Annex D defines, the RBSP ends with the trailing
    bits byte, and the NAL contains no 00 00 pair anywhere, which is what makes
    the emulation prevention question moot rather than handled.
    """
    if kind == "h265":
        require_hevc(bitstreams)
    path = stage_file(bitstreams, kind, tmp_path)

    before = {(where, offset): length
              for where, offset, length, _h, messages in all_sei_nals(path, hevc)
              if any(k == 5 for k, _s, _b in messages)}
    assert before, "the fixture has no user-data SEI"

    scrub(path)

    data = read(path)
    after = {(where, offset): (length, header, messages)
             for where, offset, length, header, messages
             in all_sei_nals(path, hevc)}
    for key, length in before.items():
        assert key in after, f"the SEI NAL at {key} disappeared, so a length moved"
        new_length, header, messages = after[key]
        assert new_length == length, (key, length, new_length)
        assert messages, f"the rewritten NAL at {key} holds no SEI message"
        for message_type, size, body in messages:
            assert message_type == 3, (
                f"{key}: payload type {message_type} is not filler_payload")
            assert body == b"\xff" * size, f"{key}: a filler byte is not 0xFF"
        nal = data[key[1]:key[1] + length]
        assert b"\x00\x00" not in nal[header:], (
            f"{key}: the rewritten payload holds two adjacent zero bytes, which "
            "would need an emulation prevention byte and therefore a length "
            "change")
        assert rbsp_of(nal[header:])[-1] == 0x80, f"{key}: no trailing bits"


def test_an_emulation_prevention_byte_inside_the_signature_is_handled(
        bitstreams, tmp_path):
    """
    THE BOUNDARY CASE, on a real file rather than in the abstract.

    Nothing x264 writes contains 00 00 03: its UUID is fixed and its settings
    string is ASCII, so the payload measured here has ZERO emulation prevention
    bytes and the happy path never meets one. That is exactly the input a test
    must not settle for. Three bytes in the middle of the payload are replaced
    with 00 00 03, which is a legal escape and shortens the RBSP by one without
    changing the NAL's length, and the whole acceptance set is re-run.
    """
    path = stage_file(bitstreams, "h264", tmp_path)
    data = bytearray(read(path))

    entries = [entry for entry in all_sei_nals(path, hevc=False)
               if any(kind == 5 for kind, _s, _b in entry[4])]
    assert len(entries) == 1, entries
    _where, offset, length, header, _messages = entries[0]
    assert b"\x00\x00\x03" not in data[offset:offset + length], (
        "the x264 payload already contains an escape; this test would then be "
        "measuring the happy path twice")

    # A spot well inside the settings text, so the UUID and the size field are
    # untouched and the message stays a user_data_unregistered one.
    spot = offset + header + 60
    data[spot:spot + 3] = b"\x00\x00\x03"
    with open(path, "wb") as handle:
        handle.write(bytes(data))
    before = read(path)
    before_frames = framemd5(path)
    assert b"x264 - core" in before

    scrub(path)

    out = read(path)
    assert len(out) == len(before)
    assert_no_encoder_signature(path, note="escaped payload")
    assert framemd5(path) == before_frames
    assert_decodes_cleanly(path, note="escaped payload after the SEI rewrite")
    rewritten = out[offset:offset + length]
    assert b"\x00\x00" not in rewritten[header:], (
        "the rewrite left an escape sequence behind rather than replacing the "
        "whole payload region")


def test_the_signature_is_removed_from_a_track_sharing_mdat_with_audio(
        bitstreams, tmp_path):
    """
    `mdat` is not a NAL stream. Measured: in this fixture the video samples
    start at offset 48 and the audio samples at 4612, INTERLEAVED in one mdat.
    A walk of the mdat payload as a chain of length-prefixed NALs reads an AAC
    frame as a NAL length and lands wherever that says, so the sample table is
    the only honest way in.
    """
    path = stage_file(bitstreams, "aac", tmp_path)
    data = read(path)
    nodes = walk(data)
    handlers = {handler for handler, _node in track_nodes(data, nodes)}
    assert handlers == {b"vide", b"soun"}, handlers
    mdats = [node for node in nodes if node.type == b"mdat"]
    assert len(mdats) == 1, "the fixture no longer interleaves into one mdat"

    before_frames = framemd5(path)
    scrub(path)

    assert_no_encoder_signature(path, note="interleaved")
    assert framemd5(path) == before_frames
    assert_decodes_cleanly(path, note="interleaved after the SEI rewrite")


def test_a_fragmented_file_loses_its_signature_too(containers, tmp_path):
    """
    A fragmented MP4 keeps nothing in `stbl`: its sample sizes and offsets live
    in `moof/traf/trun` and `moof/traf/tfhd`. A feature that silently does
    nothing to a whole container shape is worse than one that refuses, so the
    fragment tables are parsed and this asserts the result.
    """
    path = stage(containers, "fragmented", tmp_path)
    before = read(path)
    assert b"x264 - core" in before
    before_frames = framemd5(path)

    scrub(path)

    assert len(read(path)) == len(before)
    assert_no_encoder_signature(path, note="fragmented")
    assert framemd5(path) == before_frames
    assert_decodes_cleanly(path, note="fragmented after the SEI rewrite")


def test_the_annex_b_form_is_handled_as_well_as_the_length_prefixed_one(
        bitstreams, tmp_path):
    """
    ISO base media never uses Annex B, and this proves the helper handles it.

    Stated plainly because the temptation is to claim more: every NAL inside an
    MP4 is length-prefixed, so nothing in the engine's path exercises the start
    code form. The library handles both, so the same rewrite serves a raw
    elementary stream, and that is what is exercised here: ffmpeg converts the
    fixture to Annex B, the SEI is located and overwritten at identical length,
    and ffmpeg decodes the result to the same frames.
    """
    source = stage_file(bitstreams, "h264", tmp_path)
    raw = str(tmp_path / "elementary.h264")
    _ffmpeg(["-i", source, "-c", "copy", "-bsf:v", "h264_mp4toannexb",
             "-f", "h264", raw])
    original = read(raw)
    assert b"x264 - core" in original, "the elementary stream carries no signature"
    before_frames = framemd5(raw)

    from metascrub import isobmff

    nals = isobmff.annexb_nals(original, 0, len(original))
    assert nals, "no start codes found"
    # The independent reader must agree about where the NALs are, or one of the
    # two is wrong and neither result means anything.
    assert nals == annexb_units(original), (
        "the two Annex B readers disagree about the NAL boundaries")

    hits = isobmff.sei_user_data_nals(original, nals, hevc=False)
    assert len(hits) == 1, hits

    out = bytearray(original)
    for offset, length, header in hits:
        out[offset + header:offset + length] = isobmff.sei_filler_bytes(
            length - header)
    assert len(out) == len(original), "the Annex B rewrite changed a length"
    with open(raw, "wb") as handle:
        handle.write(bytes(out))

    assert b"x264 - core" not in read(raw)
    assert framemd5(raw) == before_frames, (
        "the Annex B rewrite changed the decoded frames")


# -------------------------------------------------------------- overcorrection


def test_a_video_with_no_encoder_sei_keeps_every_sample_byte(bitstreams,
                                                             tmp_path):
    """
    OVERCORRECTION. A file with nothing to remove must come out with its
    bitstream untouched, byte for byte.

    The fixture is built by ffmpeg's own `filter_units` bitstream filter, which
    is not this engine, and the assertion is over the sample ranges rather than
    over the whole file: the engine still zeroes this file's compressorname,
    hdlr name and times, and it should.
    """
    path = stage_file(bitstreams, "h264_no_sei", tmp_path)
    before = read(path)
    nodes = walk(before)
    trak = next(node for handler, node in track_nodes(before, nodes)
                if handler == b"vide")
    ranges = stbl_samples(before, trak)
    assert ranges, "no video samples in the fixture"
    assert not [1 for _w, _o, _l, _h, messages in all_sei_nals(path, hevc=False)
                for kind, _s, _b in messages if kind == 5], (
        "the no-SEI fixture still carries a user-data SEI")
    original = [bytes(before[offset:offset + size]) for offset, size in ranges]

    reported = scrub(path)

    out = read(path)
    after = [bytes(out[offset:offset + size]) for offset, size in ranges]
    assert after == original, (
        "the engine rewrote sample bytes in a file with no encoder SEI in it")
    assert not [line for line in reported if "SEI" in line], reported
    assert_decodes_cleanly(path, note="no-SEI fixture")


def test_an_sei_that_is_not_user_data_is_left_alone(bitstreams, tmp_path):
    """
    OVERCORRECTION, the sharper half: an SEI NAL is present and must SURVIVE.

    "Rewrite every SEI NAL" is one edit away from "rewrite every SEI NAL that
    carries a signature", and it would pass every removal test above. The
    fixture's one SEI message has its payload TYPE byte changed from 5
    (user_data_unregistered) to 1 (pic_timing), a single-byte, length-preserving
    edit made by this test. The engine must then leave the NAL completely
    alone, which is visible because the x264 string is still sitting in it.
    """
    path = stage_file(bitstreams, "h264", tmp_path)
    data = bytearray(read(path))
    entries = [entry for entry in all_sei_nals(path, hevc=False)
               if any(kind == 5 for kind, _s, _b in entry[4])]
    assert len(entries) == 1, entries
    _where, offset, length, header, _messages = entries[0]
    assert data[offset + header] == 5, "the type byte is not where it was measured"
    data[offset + header] = 1
    with open(path, "wb") as handle:
        handle.write(bytes(data))

    reported = scrub(path)

    out = read(path)
    assert out[offset:offset + length] == bytes(data[offset:offset + length]), (
        "a pic_timing SEI was rewritten; the engine is keying on the NAL type "
        "rather than on the message type")
    assert b"x264 - core" in out, (
        "the mutation did not leave anything visible to notice, so this test "
        "would pass against an engine that rewrote the NAL anyway")
    assert not [line for line in reported if "SEI" in line], reported


def test_audio_is_bit_identical_when_removal_is_not_asked_for(bitstreams,
                                                              tmp_path):
    """
    OVERCORRECTION for the second feature, and the reason it is opt-in.

    Dropping the audio changes what the file IS, not what it says about itself.
    The default must therefore leave every audio sample byte exactly where it
    was, and the decoded audio must hash the same.
    """
    for name in ("aac", "pcm"):
        path = stage_file(bitstreams, name, tmp_path)
        before = read(path)
        nodes = walk(before)
        trak = next(node for handler, node in track_nodes(before, nodes)
                    if handler == b"soun")
        ranges = stbl_samples(before, trak)
        assert ranges, f"{name}: no audio samples in the fixture"
        original = [bytes(before[offset:offset + size]) for offset, size in ranges]
        before_audio = audio_md5(path)
        assert before_audio and before_audio != EMPTY_MD5, name

        IsobmffEngine().strip_all(path)

        out = read(path)
        assert [bytes(out[offset:offset + size])
                for offset, size in ranges] == original, (
            f"{name}: audio sample bytes changed with removal off")
        assert audio_md5(path) == before_audio, (
            f"{name}: the decoded audio changed with removal off")


# ------------------------------------------------------------- audio removal


@pytest.mark.parametrize("name,expected", [("aac", "empty"), ("pcm", "silence")])
def test_audio_removal_zeroes_the_recording_and_leaves_the_video_alone(
        bitstreams, name, expected, tmp_path):
    """
    WHAT AUDIO REMOVAL ACTUALLY ACHIEVES, measured, in both stsz shapes.

    The samples are zeroed IN PLACE, so the recording is gone from the bytes
    and carving mdat recovers nothing; there is no ENF signature left to
    analyse. The sample sizes are zeroed so a decoder is never handed a
    zero-filled frame. The file is the same length, the video framemd5 is bit
    identical, and the video decodes with no warning at all.

    The two codecs end in two different places and both are correct:
      AAC   ffmpeg decodes ZERO audio frames, md5 d41d8cd9..., the empty digest
      PCM   ffmpeg reconstructs the sample count from the chunk sizes rather
            than from stsz, so the track still decodes; it decodes to
            e9ded829730eccd2d0273d7cc06be58c, which is byte for byte the digest
            of one second of `anullsrc` silence.
    """
    path = stage_file(bitstreams, name, tmp_path)
    before = read(path)
    before_frames = video_framemd5(path)
    before_audio = audio_md5(path)
    assert before_audio and before_audio != EMPTY_MD5

    nodes = walk(before)
    trak = next(node for handler, node in track_nodes(before, nodes)
                if handler == b"soun")
    ranges = stbl_samples(before, trak)
    assert any(any(before[offset:offset + size]) for offset, size in ranges)

    reported = IsobmffEngine(remove_audio=True).strip_all(path)

    out = read(path)
    assert len(out) == len(before), "audio removal changed the file length"
    for offset, size in ranges:
        assert not any(out[offset:offset + size]), (
            f"{name}: audio sample bytes survived at offset {offset}")
    assert video_framemd5(path) == before_frames, (
        f"{name}: removing the audio changed a decoded video frame")
    assert_video_decodes_cleanly(path, note=f"{name} after audio removal")

    # AND the WHOLE file, audio included, because this is the assertion that
    # separates a removed track from a broken one. Measured: zeroing the
    # samples without zeroing the sizes gives ffmpeg exit 69 and
    # 'channel element 0.0 is not allocated' on every frame, and a video-only
    # decode never sees it. Exactly one line is tolerated, and it is the null
    # MUXER remarking that an output stream received nothing, which is the
    # correct outcome rather than an error.
    full = _run(["ffmpeg", "-v", "warning", "-i", path, "-f", "null", "-"])
    noise = [line for line in full.stderr.decode(errors="replace").splitlines()
             if line.strip()
             and "No filtered frames for output stream" not in line]
    assert full.returncode == 0 and not noise, (
        f"{name}: the file no longer decodes cleanly with its audio track in "
        f"place (exit {full.returncode}): " + " | ".join(noise[:6]))

    after_audio = audio_md5(path)
    if expected == "empty":
        assert after_audio == EMPTY_MD5, (
            f"{name}: the audio still decodes to something: {after_audio}")
    else:
        silence = _run(["ffmpeg", "-v", "error", "-f", "lavfi", "-i",
                        "anullsrc=r=44100:cl=mono", "-t", "1",
                        "-c:a", "pcm_s16le", "-f", "md5", "-"])
        assert silence.returncode == 0
        assert after_audio == silence.stdout.decode().strip(), (
            f"{name}: the zeroed PCM track does not decode to silence: "
            f"{after_audio}")
    assert any("audio samples" in line for line in reported), reported
    assert any("stsz sample sizes" in line for line in reported), reported


def test_the_audio_track_still_exists_after_removal(bitstreams, tmp_path):
    """
    WHAT AUDIO REMOVAL DOES NOT ACHIEVE, asserted so nobody has to guess.

    The track is NOT deleted, because deleting a `trak` moves every box after
    it and deleting its samples moves every sample offset in the file. `trak`,
    `tkhd`, `stsd` and the `mp4a` sample entry all survive, and exiftool still
    reports the format, the channel count and the sample rate. A viewer can
    still see that the file HAD audio. What they cannot get is one sample of it.

    The three tkhd flags that select a track are cleared, which is the most a
    length-preserving edit can say about "do not play this".
    """
    path = stage_file(bitstreams, "aac", tmp_path)
    IsobmffEngine(remove_audio=True).strip_all(path)

    data = read(path)
    nodes = walk(data)
    handlers = [handler for handler, _node in track_nodes(data, nodes)]
    assert b"soun" in handlers, (
        "the audio track was removed structurally, which this engine cannot do "
        "at a fixed length")
    assert b"mp4a" in data, "the audio sample entry was destroyed"

    trak = next(node for handler, node in track_nodes(data, nodes)
                if handler == b"soun")
    tkhd = next(child for child in trak.children if child.type == b"tkhd")
    flags = int.from_bytes(data[tkhd.payload_offset + 1:tkhd.payload_offset + 4],
                           "big")
    assert flags & 0x7 == 0, (
        f"the audio track is still enabled or still in the movie (flags {flags:#x})")

    video = next(node for handler, node in track_nodes(data, nodes)
                 if handler == b"vide")
    video_tkhd = next(child for child in video.children if child.type == b"tkhd")
    video_flags = int.from_bytes(
        data[video_tkhd.payload_offset + 1:video_tkhd.payload_offset + 4], "big")
    assert video_flags & 0x1, (
        "the VIDEO track was disabled too, which would make the file unplayable")


def test_the_aac_encoder_signature_goes_with_the_audio_and_not_before(
        bitstreams, tmp_path):
    """
    MEASURED 2026-09-06, and reported rather than asserted away.

    ffmpeg's AAC encoder writes 'Lavc61.26.100' into a data stream element in
    the FIRST AUDIO SAMPLE. It is an encoder signature, it is in mdat, and it
    is NOT in the video bitstream, so feature one does not remove it and no
    amount of SEI work would. Audio removal does remove it, because it zeroes
    the samples that hold it.

    This is stated as its own test so the residual is a measured fact with an
    assertion behind it rather than a sentence in a docstring that nobody
    re-checks.
    """
    path = stage_file(bitstreams, "aac", tmp_path)
    assert b"Lavc" in read(path)

    scrub(path)

    data = read(path)
    survivors = []
    start = 0
    while True:
        found = data.find(b"Lavc", start)
        if found == -1:
            break
        survivors.append(found)
        start = found + 1
    assert survivors, (
        "the AAC data stream element no longer survives the default scrub. If "
        "that is because something else now removes it, this test should say "
        "so rather than be deleted.")

    nodes = walk(data)
    trak = next(node for handler, node in track_nodes(data, nodes)
                if handler == b"soun")
    audio = stbl_samples(data, trak)
    for position in survivors:
        assert any(offset <= position < offset + size for offset, size in audio), (
            f"a Lavc string survived at offset {position}, which is not inside "
            "an audio sample; that is a leak this engine claims to have removed")

    # And it goes when the audio goes.
    again = stage_file(bitstreams, "aac", tmp_path)
    IsobmffEngine(remove_audio=True).strip_all(again)
    assert b"Lavc" not in read(again)


def test_a_fragmented_audio_track_is_refused_rather_than_half_removed(
        bitstreams, tmp_path):
    """
    FAIL CLOSED, and the reason it is a refusal and not a silent skip.

    A fragmented file's sample sizes live in each `trun`, not in `stsz`. The
    samples could be zeroed, but nothing would neutralise the sizes, so a
    decoder would be handed zero-filled AAC frames and answer with errors on
    every one of them (measured exit 69 on the non-fragmented equivalent). A
    half-removed audio track is worse than a refusal, so it refuses, before a
    temporary file exists.
    """
    path = stage_file(bitstreams, "fragmented_aac", tmp_path)
    pristine = read(path)

    with pytest.raises(EngineError, match="fragmented file"):
        IsobmffEngine(remove_audio=True).strip_all(path)

    assert read(path) == pristine, "the input was modified by a refusal"
    assert not [name for name in os.listdir(tmp_path)
                if name.startswith(".metascrub-")]

    # And the SAME file scrubs fine with audio removal off, so the refusal is
    # about the feature and not about the container.
    IsobmffEngine().strip_all(path)
    assert_no_encoder_signature(path, note="fragmented with audio")


def test_the_default_engine_instance_does_not_remove_audio():
    """
    The registry in metascrub/engines/__init__.py builds this with no
    arguments. If the default ever flips, every existing caller starts
    silently returning silent video.
    """
    assert IsobmffEngine().remove_audio is False
    assert IsobmffEngine(remove_audio=True).remove_audio is True

    from metascrub.engines import get_engine
    from metascrub.capabilities import Engine

    assert get_engine(Engine.ISOBMFF).remove_audio is False


# ------------------------------------------------------------------ mutation


def test_mutation_scanning_only_mdat_misses_the_hevc_signature(
        bitstreams, tmp_path, monkeypatch):
    """
    THE MUTATION FOR FEATURE ONE, and it is the exact bug the measurement warns
    about: scan the bitstream in `mdat` and not the configuration record.

    Everything else still works. The H.264 file still loses its signature, the
    file still decodes, the framemd5 is still identical, every structural
    assertion in this file still passes. What survives is 2334 bytes naming the
    x265 build, the compiler, the operating system and every encoder setting,
    sitting in `moov` where no mdat walk will ever look.

    Only `assert_no_encoder_signature` catches it, which is the argument for
    asserting over the whole file rather than over the part the engine looked at.
    """
    require_hevc(bitstreams)
    real = isobmff_engine._video_configs

    def blinded(data, track):
        # The configuration record is still needed for the NAL length size, so
        # the mutation drops only its NAL list. That is what an implementation
        # written from "the SEI is in the bitstream" would look like.
        return [(box, hevc, length_size, [])
                for box, hevc, length_size, _nals in real(data, track)]

    monkeypatch.setattr(isobmff_engine, "_video_configs", blinded)

    path = stage_file(bitstreams, "h265", tmp_path)
    scrub(path)

    assert b"x265 (build" in read(path), (
        "the mutation did not change the behaviour, so this test proves "
        "nothing about the assertion below")
    with pytest.raises(AssertionError, match="encoder signature survived"):
        assert_no_encoder_signature(path, note="mutation")

    # The same mutation on H.264 changes nothing, which is what makes it a
    # SILENT bug rather than an obvious one.
    h264 = stage_file(bitstreams, "h264", tmp_path)
    scrub(h264)
    assert_no_encoder_signature(h264, note="mutation, h264")


def test_mutation_leaving_the_audio_sample_sizes_alone_is_caught(
        bitstreams, tmp_path, monkeypatch):
    """
    THE MUTATION FOR FEATURE TWO: zero the audio samples and stop there.

    That is the obvious implementation, and it is the one that was measured
    first. The privacy work is done, the file is the same length, the video is
    bit identical, and every structural assertion passes. What it produces is a
    file whose audio decoder now errors on every frame: measured, ffmpeg exit
    69 with 'channel element 0.0 is not allocated'. A file that no longer plays
    is not a scrubbed file.

    Caught by the decode assertion and by nothing else.
    """
    real = isobmff_engine._audio_edits

    def half_done(data, boxes, tracks, edits):
        before = len(edits)
        real(data, boxes, tracks, edits)
        kept = []
        for edit in edits[before:]:
            if "stsz sample sizes" in edit.note:
                continue
            kept.append(edit)
        del edits[before:]
        edits.extend(kept)

    monkeypatch.setattr(isobmff_engine, "_audio_edits", half_done)

    path = stage_file(bitstreams, "aac", tmp_path)
    reported = IsobmffEngine(remove_audio=True).strip_all(path)
    assert not [line for line in reported if "stsz sample sizes" in line], (
        "the mutation did not change the behaviour")

    proc = _run(["ffmpeg", "-v", "warning", "-i", path, "-f", "null", "-"])
    assert proc.returncode != 0, (
        "the half-done removal decoded cleanly, so this test proves nothing")

    with pytest.raises(AssertionError):
        assert_decodes_cleanly(path, note="mutation")
