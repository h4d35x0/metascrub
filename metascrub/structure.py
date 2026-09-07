"""
Structural scan of an OUTPUT file: does every byte belong to a structure we
recognise?

This module exists because of a defect measured on 2026-09-06 against the
shipped 1.0.1 tool. A WebP carrying an 8 KB MP4 inside an unknown `MPVD` RIFF
chunk was reported:

    SANITIZED  mine.webp  [exiftool]
    verified clean         1

while the embedded video and its `ftyp` were still in the output. The same
shape was then measured for an unknown PNG ancillary chunk and for trailing
data appended after a GIF trailer. Three formats, three false CLEAN verdicts.

The root cause is not the engine. exiftool removed everything it could see.
The root cause is that verification asked exiftool what it could see and then
reported a verdict whose confidence exceeded the question. `verify.py` searches
the output for values a baseline read captured; a carrier the baseline reader
never parsed produces no values, so it produces no needles, so the scan passes
having looked for nothing. That is trap 10 in CLAUDE.md generalised: the
residual scan cannot see a carrier exiftool does not read.

So this scan asks a different question, and one that does not depend on having
read anything first: after scrubbing, is there any region of the file that the
format's own structure does not account for? That is a proof about what the
file can no longer hide, rather than a search for something it used to contain.

WHAT THIS MODULE DELIBERATELY DOES NOT DO

It does not remove anything. It reports. A file with an unaccounted region is
one this tool cannot currently clean, and the honest outcome is to say so and
fail closed, not to claim a removal that did not happen. Removing these
carriers requires a container rewriter per format, which is the mobile build's
Phase 1 work and needs its fixture corpus first.

It also does not flag known metadata chunks that survived. Those are the
engines' job and the residual scan's job, and widening this check in a patch
release would change the verdict on files that are actually fine. The measured
defect is unknown and unaccounted regions, and that is exactly what this covers.

ADDING A FORMAT

Add a walker returning a list of human-readable descriptions of unaccounted
regions, and register it in _WALKERS. A format with no walker returns
`applicable=False`, which means this check has no opinion and the verdict is
unchanged. That is why adding this module cannot regress the other formats.

ADDING A WALKER TO A FORMAT THAT ALREADY SHIPS IS A DIFFERENT ACT, AND IT CAN.

That sentence above holds for introducing the module. It does not hold for
registering a new extension, which moves that format from "no opinion" to a
verdict `verify.py` maps to STRUCTURE_UNACCOUNTED or, on a parse failure, to
UNVERIFIED. One false positive turns a correctly scrubbed holiday photo into a
failed verification, which is worse than the gap it closes. So the corpus is
the deliverable, not the walker: a new walker is measured against real device
output for the format, before and after scrubbing, and it does not ship if
there is a region of a real untouched device file it cannot account for.

JPEG and ISO base media were added on 2026-09-07 under that rule and measured
against sixteen real device files: iPhone HEICs including one whose Exif item
ends on the last byte of the file, an iPhone Live Photo `.mov` with no `ftyp`
box at all, geotagged Nokia and Samsung MP4s, an AVIF, a Pixel 2 with nothing
after its EOI, and three real trailers (two Google motion photo conventions and
a Samsung SEF block). Zero false positives before or after scrubbing; the three
trailers were reported with byte counts matching an independent instrument's
measurement exactly. tests/test_structure.py holds that corpus check, and skips
it where the corpus is absent.
"""

from __future__ import annotations

import os
import struct
from dataclasses import dataclass, field
from typing import Callable, Dict, List, Optional


@dataclass(frozen=True)
class StructureReport:
    """
    Result of the scan.

    `applicable` False means no walker is registered for this format, so the
    caller must not read anything into an empty `unaccounted`. "We did not look"
    and "we looked and found nothing" must never share a representation; that is
    the same rule ReadOutcome enforces in exif_io.py.
    """

    applicable: bool
    unaccounted: List[str] = field(default_factory=list)
    error: Optional[str] = None

    @property
    def is_clean(self) -> bool:
        return self.applicable and not self.unaccounted and self.error is None


# ---------------------------------------------------------------- PNG

# Every chunk type defined by the PNG specification, its extensions registry,
# and APNG. The point of listing the metadata carriers here too (tEXt, zTXt,
# iTXt, eXIf, tIME, iCCP) is that they are RECOGNISED, not that they are
# acceptable: this scan reports unaccounted regions, and a surviving tEXt is
# accounted for. Whether it should have survived is the residual scan's
# question and the engine's responsibility, not this module's.
_PNG_KNOWN = frozenset({
    b"IHDR", b"PLTE", b"IDAT", b"IEND",
    b"tRNS", b"cHRM", b"gAMA", b"iCCP", b"sBIT", b"sRGB", b"cICP", b"mDCv",
    b"cLLi", b"bKGD", b"hIST", b"pHYs", b"sPLT", b"eXIf", b"tIME",
    b"tEXt", b"zTXt", b"iTXt",
    b"acTL", b"fcTL", b"fdAT",
})

_PNG_SIGNATURE = b"\x89PNG\r\n\x1a\n"


def _walk_png(data: bytes) -> List[str]:
    if not data.startswith(_PNG_SIGNATURE):
        raise ValueError("not a PNG: signature mismatch")
    out: List[str] = []
    off = len(_PNG_SIGNATURE)
    saw_iend = False
    while off + 8 <= len(data):
        (length,) = struct.unpack(">I", data[off:off + 4])
        ctype = data[off + 4:off + 8]
        end = off + 8 + length + 4          # +4 for the CRC
        if end > len(data):
            out.append(
                f"chunk {ctype.decode('latin1')!r} at offset {off} declares "
                f"{length} bytes, which runs past the end of the file"
            )
            return out
        if ctype not in _PNG_KNOWN:
            out.append(
                f"unknown PNG chunk {ctype.decode('latin1')!r} "
                f"({length} bytes) at offset {off}"
            )
        if ctype == b"IEND":
            saw_iend = True
            off = end
            break
        off = end
    if saw_iend and off < len(data):
        out.append(f"{len(data) - off} bytes after the IEND chunk")
    return out


# ---------------------------------------------------------------- WebP

# VP8X is listed because a file that legitimately needs alpha or animation
# carries it. The mobile design doc recommends dropping it where possible,
# since a WebP without VP8X cannot carry metadata at all, but that is a removal
# decision and this module does not make removal decisions.
_WEBP_KNOWN = frozenset({
    b"VP8 ", b"VP8L", b"VP8X", b"ALPH", b"ANIM", b"ANMF",
    b"ICCP", b"EXIF", b"XMP ",
})


def _walk_webp(data: bytes) -> List[str]:
    if len(data) < 12 or data[0:4] != b"RIFF" or data[8:12] != b"WEBP":
        raise ValueError("not a WebP: RIFF/WEBP header mismatch")
    out: List[str] = []
    (declared,) = struct.unpack("<I", data[4:8])
    total = declared + 8
    if total < len(data):
        out.append(
            f"{len(data) - total} bytes past the declared RIFF size "
            f"(declared {total}, file is {len(data)})"
        )
    limit = min(total, len(data))
    off = 12
    while off + 8 <= limit:
        fourcc = data[off:off + 4]
        (size,) = struct.unpack("<I", data[off + 4:off + 8])
        payload_end = off + 8 + size
        if payload_end > limit:
            out.append(
                f"chunk {fourcc.decode('latin1')!r} at offset {off} declares "
                f"{size} bytes, which runs past the end of the RIFF container"
            )
            return out
        if fourcc not in _WEBP_KNOWN:
            out.append(
                f"unknown WebP chunk {fourcc.decode('latin1')!r} "
                f"({size} bytes) at offset {off}"
            )
        off = payload_end + (size & 1)      # RIFF chunks are padded to even
    return out


# ---------------------------------------------------------------- GIF

_GIF_TRAILER = 0x3B
_GIF_EXTENSION = 0x21
_GIF_IMAGE = 0x2C
# Graphic control, comment, plain text, application. All four are defined by
# GIF89a; two of them carry metadata, which again is not this module's call.
_GIF_KNOWN_LABELS = frozenset({0xF9, 0xFE, 0x01, 0xFF})


def _skip_sub_blocks(data: bytes, off: int) -> int:
    """Advance past a GIF sub-block chain, returning the offset after its terminator."""
    while off < len(data):
        size = data[off]
        off += 1
        if size == 0:
            return off
        off += size
    raise ValueError("truncated GIF sub-block chain")


def _walk_gif(data: bytes) -> List[str]:
    if len(data) < 13 or data[0:3] != b"GIF":
        raise ValueError("not a GIF: signature mismatch")
    out: List[str] = []
    flags = data[10]
    off = 13
    if flags & 0x80:                                  # global colour table
        off += 3 * (2 ** ((flags & 0x07) + 1))
    while off < len(data):
        block = data[off]
        if block == _GIF_TRAILER:
            off += 1
            break
        if block == _GIF_EXTENSION:
            if off + 2 > len(data):
                raise ValueError("truncated GIF extension header")
            label = data[off + 1]
            if label not in _GIF_KNOWN_LABELS:
                out.append(f"unknown GIF extension label 0x{label:02X} at offset {off}")
            off = _skip_sub_blocks(data, off + 2)
        elif block == _GIF_IMAGE:
            if off + 10 > len(data):
                raise ValueError("truncated GIF image descriptor")
            local = data[off + 9]
            off += 10
            if local & 0x80:                          # local colour table
                off += 3 * (2 ** ((local & 0x07) + 1))
            off += 1                                  # LZW minimum code size
            off = _skip_sub_blocks(data, off)
        else:
            out.append(f"unknown GIF block introducer 0x{block:02X} at offset {off}")
            return out
    if off < len(data):
        out.append(f"{len(data) - off} bytes after the GIF trailer")
    return out


# ---------------------------------------------------------------- JPEG

# JPEG has no chunk table to compare against, so the question this walker asks
# is the other half of the module contract: does every byte belong to a marker
# segment, to the entropy-coded scan that follows a SOS, or to the EOI. There
# is no third place for a byte to live, which makes "unaccounted" unusually
# crisp here compared with the chunked formats above.
#
# The measured carrier is the TRAILER. A Google Motion Photo appends a complete
# MP4, with its own GPS, after the JPEG's EOI, and a Samsung SEF block is
# discovered backward from the last bytes of the file and is referenced by
# nothing in any marker. exiftool names such a blob and never says what is
# inside it, so the residual scan can construct no needle for the video's
# coordinates. That is trap 10 again, and it is exactly what this walker sees.
#
# REACHING THE EOI REQUIRES WALKING, NEVER SEARCHING. An APP0/JFXX segment or
# an EXIF thumbnail is a complete JPEG with its own `FF D9` inside it, so a
# search for the first EOI byte pair lands in the thumbnail and reports the
# entire real image as a trailer. Every segment below is consumed by its
# declared length, so the only EOI this can reach is the outer one.

_JPEG_SOI = 0xD8
_JPEG_EOI = 0xD9
_JPEG_SOS = 0xDA

# Markers with no length field: TEM, SOI, EOI and RST0..RST7. Everything else
# is `FF <code> <two-byte length including itself> <payload>`.
_JPEG_STANDALONE = frozenset({0x01, _JPEG_SOI, _JPEG_EOI} | set(range(0xD0, 0xD8)))

_JPEG_NAMES = {
    0x01: "TEM", 0xC4: "DHT", 0xCC: "DAC", 0xD8: "SOI", 0xD9: "EOI",
    0xDA: "SOS", 0xDB: "DQT", 0xDC: "DNL", 0xDD: "DRI", 0xDE: "DHP",
    0xDF: "EXP", 0xFE: "COM",
}


def _jpeg_name(code: int) -> str:
    if 0xE0 <= code <= 0xEF:
        return "APP%d" % (code - 0xE0)
    if 0xD0 <= code <= 0xD7:
        return "RST%d" % (code - 0xD0)
    if 0xC0 <= code <= 0xCF and code not in (0xC4, 0xC8, 0xCC):
        return "SOF%d" % (code - 0xC0)
    return _JPEG_NAMES.get(code, "marker 0x%02X" % code)


def _jpeg_scan_end(data: bytes, start: int) -> Optional[int]:
    """
    Offset of the marker that ends the entropy-coded scan starting at `start`.

    Inside a scan T.81 permits exactly three things after an 0xFF byte: a
    stuffed 0x00, a restart marker RST0..RST7, or more 0xFF fill. Anything else
    is the next real marker and ends the scan. Returns None when the data runs
    to the end of the file with no closing marker, which means truncation.
    """
    length = len(data)
    at = start
    while at + 1 < length:
        if data[at] != 0xFF:
            at += 1
            continue
        following = data[at + 1]
        if following == 0xFF:
            at += 1                     # fill; the marker may still follow
            continue
        if following == 0x00 or 0xD0 <= following <= 0xD7:
            at += 2                     # stuffed byte, or a restart marker
            continue
        return at
    return None


def _walk_jpeg(data: bytes) -> List[str]:
    length = len(data)
    if length < 2 or data[0] != 0xFF or data[1] != _JPEG_SOI:
        raise ValueError("not a JPEG: no SOI marker at offset 0")
    out: List[str] = []
    off = 0
    while off < length:
        if data[off] != 0xFF:
            out.append(
                f"{length - off} bytes from offset {off} that no marker "
                f"segment accounts for (expected a marker, found "
                f"0x{data[off]:02X})"
            )
            return out
        # A run of 0xFF before a marker is legal fill and belongs to the
        # segment that follows it, so the segment starts at the run.
        cursor = off
        while cursor < length and data[cursor] == 0xFF:
            cursor += 1
        if cursor >= length:
            out.append(f"{length - off} bytes of marker padding at offset {off} "
                       f"with no marker after them")
            return out
        marker = data[cursor]
        if marker == 0x00:
            out.append(
                f"{length - off} bytes from offset {off} that no marker "
                f"segment accounts for (a stuffed 0xFF00 outside a scan)"
            )
            return out
        name = _jpeg_name(marker)

        if marker in _JPEG_STANDALONE:
            off = cursor + 1
            if marker == _JPEG_EOI:
                if off < length:
                    out.append(
                        f"{length - off} bytes after the first top-level EOI "
                        f"at offset {off}; this is where a motion photo's "
                        f"appended video and a Samsung SEF block live"
                    )
                return out
            continue

        if cursor + 3 > length:
            out.append(f"{length - off} bytes from offset {off}: {name} has a "
                       f"truncated length field")
            return out
        declared = int.from_bytes(data[cursor + 1:cursor + 3], "big")
        if declared < 2:
            out.append(
                f"{length - off} bytes from offset {off}: {name} declares a "
                f"length of {declared}, below the two bytes the length field "
                f"itself occupies"
            )
            return out
        end = cursor + 1 + declared
        if end > length:
            out.append(
                f"{name} at offset {off} declares {declared} bytes, which runs "
                f"past the end of the file"
            )
            return out
        off = end

        if marker == _JPEG_SOS:
            # Progressive JPEGs carry many SOS segments, so this is inside the
            # loop and not a one-shot after the header.
            scan_end = _jpeg_scan_end(data, off)
            if scan_end is None:
                out.append(
                    f"{length - off} bytes of entropy-coded data from offset "
                    f"{off} run to the end of the file with no closing marker"
                )
                return out
            off = scan_end

    raise ValueError("no EOI marker: this JPEG is truncated")


# ------------------------------------------------------- ISO base media

# `.mp4`, `.mov`, `.m4v`, `.heic`, `.heif`, `.avif` and `.3gp` are all the same
# container: a tree of boxes, each `<4-byte size><4-byte type>`, that must tile
# the range it sits in with nothing left over. Every byte belongs to a box or
# it belongs to nothing, and belonging to nothing is what this reports.
#
# TWO THINGS THIS WALKER DOES NOT DO, both deliberately.
#
# It does not require an `ftyp` box, and it must not. Measured on a real iPhone
# 14 Pro Live Photo `.mov`: the top level is `wide`, `mdat`, `moov` and a byte
# search for the literal `ftyp` across the whole file returns nothing. That is
# device output, not corruption, and refusing it would turn a correct file into
# an UNVERIFIED one.
#
# An unknown box is a LEAF, never a guess. `_ISO_CONTAINERS` is the complete
# list of boxes this will descend into; everything else accounts for its own
# payload and is not opened. Descending into a box whose layout is unknown is
# how a walker invents children and then reports the nonsense as unaccounted.
# `udta` is the measured case for keeping that rule: a QuickTime `udta` holds
# atoms whose type begins with 0xA9 (`(c)xyz`, the ISO 6709 GPS atom on real
# Android output), which is not printable ASCII, and it may end with a four
# byte zero terminator that is not a box header. Opening it buys nothing here,
# because a surviving metadata atom is the residual scan's question and not
# this module's, and it costs a false positive on every geotagged Android MP4.

_ISO_HEADER = 8
_ISO_LARGE_HEADER = 16
_ISO_UUID_LEN = 16
_ISO_MAX_DEPTH = 32

# Value is the number of fixed payload bytes before the first child:
#   0  children start at the first payload byte
#   4  FullBox: a version byte and three flag bytes
#   8  FullBox then a 32-bit entry count
# `meta` is absent on purpose; its skip is sniffed. See _iso_meta_skip.
_ISO_CONTAINERS: Dict[bytes, int] = {
    b"moov": 0, b"trak": 0, b"edts": 0, b"mdia": 0, b"minf": 0, b"dinf": 0,
    b"stbl": 0, b"mvex": 0, b"moof": 0, b"traf": 0, b"mfra": 0, b"tapt": 0,
    b"sinf": 0, b"schi": 0, b"rinf": 0, b"strk": 0, b"strd": 0, b"cinf": 0,
    b"paen": 0, b"fiin": 0, b"segr": 0, b"gitn": 0, b"clip": 0, b"matt": 0,
    b"mdra": 0, b"iprp": 0, b"ipco": 0, b"grpl": 0,
    b"iref": 4,
    b"stsd": 8, b"dref": 8,
}

# Never descended into even if one of these ever reaches _ISO_CONTAINERS. Each
# is padding, opaque payload, or a table of records rather than of boxes.
_ISO_OPAQUE = frozenset({
    b"free", b"skip", b"mdat", b"udta", b"uuid", b"ilst", b"ipma", b"iloc",
    b"iinf", b"pitm", b"idat", b"wide", b"ftyp", b"keys", b"hdlr",
})

_ISO_META = b"meta"


def _iso_printable(kind: bytes) -> bool:
    return len(kind) == 4 and all(32 <= byte < 127 for byte in kind)


def _iso_label(kind: bytes) -> str:
    return kind.decode("latin1")


def _iso_looks_like_box(data: bytes, offset: int, end: int) -> bool:
    """
    Do the eight bytes at `offset` read as a plausible box header?

    Used only by the `meta` sniff, and deliberately strict: a printable
    four-character type, and a size that is at least a header and fits inside
    the range. A version and flags word of `00 00 00 00` fails both halves,
    which is the whole reason the sniff works.
    """
    if offset + _ISO_HEADER > end:
        return False
    if not _iso_printable(bytes(data[offset + 4:offset + 8])):
        return False
    size = int.from_bytes(data[offset:offset + 4], "big")
    return _ISO_HEADER <= size <= (end - offset)


def _iso_meta_skip(data: bytes, payload_start: int, payload_end: int) -> Optional[int]:
    """
    Bytes of `meta` payload before its first child: 0, 4, or None for a leaf.

    CLAUDE.md trap 8. `meta` has TWO header shapes and both occur in ONE FILE.
    ISO/IEC 14496-12 defines MetaBox as a FullBox, so `moov/udta/meta` carries
    a version and flags word before its children. Apple's QuickTime Keys
    `moov/meta` is the same four characters and is NOT a FullBox. A walker that
    assumes either shape misparses the other; the prior instrument read the
    Apple shape as a child box of size 1751411826, which is the ASCII of
    "hdlr" read as a length.

    So the bytes are sniffed and the name is not trusted. When neither reading
    produces a plausible box, this returns None and the box is treated as a
    leaf that accounts for its own payload, rather than descended into on a
    guess and then reported as unaccounted.
    """
    if _iso_looks_like_box(data, payload_start, payload_end):
        return 0
    if _iso_looks_like_box(data, payload_start + 4, payload_end):
        return 4
    return None


def _iso_child_skip(data: bytes, kind: bytes, payload_start: int,
                    payload_end: int) -> Optional[int]:
    if kind in _ISO_OPAQUE:
        return None
    if kind == _ISO_META:
        return _iso_meta_skip(data, payload_start, payload_end)
    return _ISO_CONTAINERS.get(kind)


def _iso_level(data: bytes, start: int, end: int, parent: str, depth: int,
               out: List[str]) -> None:
    """Tile [start, end) with boxes, appending a description of anything left."""
    if depth > _ISO_MAX_DEPTH:
        out.append(
            f"box nesting deeper than {_ISO_MAX_DEPTH} levels at offset "
            f"{start}; refusing to walk further"
        )
        return
    off = start
    while off < end:
        left = end - off
        if left < _ISO_HEADER:
            # Fewer than eight bytes cannot be a box header. A QuickTime
            # terminator is four zero bytes and other producers pad the same
            # way, so an all-zero remainder is padding and anything else is a
            # region no box accounts for.
            if any(data[off:end]):
                out.append(_iso_leftover(left, off, parent, depth))
            return
        kind = bytes(data[off + 4:off + 8])
        if not _iso_printable(kind):
            out.append(_iso_leftover(left, off, parent, depth))
            return
        size = int.from_bytes(data[off:off + 4], "big")
        header = _ISO_HEADER
        if size == 1:
            if left < _ISO_LARGE_HEADER:
                out.append(
                    f"box '{_iso_label(kind)}' at offset {off} declares a "
                    f"64-bit size but there is no room for it"
                )
                return
            size = int.from_bytes(data[off + 8:off + 16], "big")
            header = _ISO_LARGE_HEADER
        elif size == 0:
            # Legal only for the last box at a level: it runs to the end.
            size = left
        if kind == b"uuid":
            header += _ISO_UUID_LEN
        if size < header:
            out.append(
                f"box '{_iso_label(kind)}' at offset {off} declares {size} "
                f"bytes, smaller than its own {header}-byte header"
            )
            return
        if off + size > end:
            out.append(
                f"box '{_iso_label(kind)}' at offset {off} declares {size} "
                f"bytes, which runs past the end of {parent}"
            )
            return

        payload_start = off + header
        payload_end = off + size
        skip = _iso_child_skip(data, kind, payload_start, payload_end)
        if skip is not None and payload_end - payload_start >= skip + _ISO_HEADER:
            _iso_level(data, payload_start + skip, payload_end,
                       f"box '{_iso_label(kind)}'", depth + 1, out)
        off += size


def _iso_leftover(count: int, offset: int, parent: str, depth: int) -> str:
    if depth == 0:
        return (f"{count} bytes at offset {offset} after the last top-level "
                f"box, which no box accounts for")
    return (f"{count} bytes at offset {offset} inside {parent} that no box "
            f"accounts for")


def _walk_isobmff(data: bytes) -> List[str]:
    if len(data) < _ISO_HEADER:
        raise ValueError(
            f"not an ISO base media file: {len(data)} bytes is too short to "
            f"hold one box"
        )
    if not _iso_looks_like_box(data, 0, len(data)):
        raise ValueError(
            "not an ISO base media file: the bytes at offset 0 do not read as "
            "a box header, whatever the extension says"
        )
    out: List[str] = []
    _iso_level(data, 0, len(data), "the file", 0, out)
    return out


# ---------------------------------------------------------------- dispatch

_WALKERS: Dict[str, Callable[[bytes], List[str]]] = {
    ".png": _walk_png,
    ".webp": _walk_webp,
    ".gif": _walk_gif,
    ".jpg": _walk_jpeg,
    ".jpeg": _walk_jpeg,
    ".jpe": _walk_jpeg,
    ".mp4": _walk_isobmff,
    ".mov": _walk_isobmff,
    ".m4v": _walk_isobmff,
    ".heic": _walk_isobmff,
    ".heif": _walk_isobmff,
    ".avif": _walk_isobmff,
    ".3gp": _walk_isobmff,
}


def supported_extensions() -> frozenset:
    """Extensions this scan has an opinion about."""
    return frozenset(_WALKERS)


def scan(path: str) -> StructureReport:
    """
    Walk `path` and report every region its format does not account for.

    Never raises for a file it cannot parse: an unparseable output is reported
    through `error`, which the caller must treat as "could not check" rather
    than as "clean". A parse failure here is itself worth surfacing, because a
    file this tool just wrote should be parseable by this tool.
    """
    ext = os.path.splitext(path)[1].lower()
    walker = _WALKERS.get(ext)
    if walker is None:
        return StructureReport(applicable=False)
    try:
        with open(path, "rb") as handle:
            data = handle.read()
    except OSError as exc:
        return StructureReport(applicable=True, error=f"could not read the output: {exc}")
    try:
        return StructureReport(applicable=True, unaccounted=walker(data))
    except (ValueError, IndexError, struct.error) as exc:
        return StructureReport(applicable=True, error=f"could not parse the output: {exc}")
