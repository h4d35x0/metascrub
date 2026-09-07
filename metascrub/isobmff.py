"""
ISO base media file format box walker. A library, not an engine.

Phase 2 of the Android media build. See docs/ANDROID-MEDIA-BUILD.md sections
2.4 and 2.5. `metascrub/engines/isobmff_engine.py` is the only caller in the
tree; this module holds the parsing so that the removal policy and the
container rules can be read, tested and broken independently of each other.

Pure Python. No ffmpeg, no exiftool, no third-party module. That is the whole
point: neither of those can run on a phone, and the phone is where this format
family actually lives.

THE THREE RULES THIS WALKER IS BUILT AROUND

1. A NAME DOES NOT DETERMINE A LAYOUT. CLAUDE.md trap 8, in a new place.
   `meta` has TWO header shapes and both occur in ONE FILE. ISO/IEC 14496-12
   defines MetaBox as a FullBox, carrying a version and flags word before its
   children. Apple's QuickTime Keys `meta` is the same four characters and is
   NOT one. Measured 2026-09-06 in a single exiftool-written MP4:

       moov/udta/meta @2390: ... "meta" 00 00 00 00 | 00 00 00 21 "hdlr"
       moov/meta      @2598: ... "meta" 00 00 00 20 "hdlr"

   A walker that assumes either shape misparses the other. The prior team's
   instrument, before it was fixed, read the second one as a child box of size
   1751411826, which is the ASCII of "hdlr" read as a length. `meta_child_skip`
   sniffs the bytes instead of trusting the name, and it is the reason this
   file has a function whose entire job is to look at four bytes.

2. AN UNKNOWN BOX IS A LEAF, NEVER A GUESS. Descending into a box whose
   internal layout is not known is how a walker invents children. The
   `CONTAINERS` table below is the complete list of boxes this walker will
   descend into, and everything absent from it is treated as opaque. That is
   deliberately conservative and it is stated as a limitation, not hidden:
   sample entries (`avc1`, `mp4a`, `hvc1` inside `stsd`) can legally hold
   child boxes and this walker does not open them.

3. FAIL CLOSED. Every structural surprise raises `IsobmffError`. A box whose
   size runs past its parent, a size smaller than its own header, a `uuid`
   with no room for its extended type, a level whose boxes do not tile the
   range they were given: all of them raise. A container this walker cannot
   fully account for is one the caller must not claim to have cleaned, which
   is the same refusal `webp_engine.parse` makes for RIFF.

TWO BUGS INHERITED FROM THE PHASE 0 INSTRUMENT AND FIXED HERE

   `skip` is NOT a container. ISO/IEC 14496-12 defines `free` and `skip` as
   the same FreeSpaceBox: their payload is padding, not boxes. The Phase 0
   walker listed `skip` as a container, which would have made it descend into
   arbitrary padding bytes and read whatever they happened to say.

   `ipma` is NOT a container either. It is the item property ASSOCIATION
   table, a FullBox holding counts and indices. Descending into it produces
   nonsense.

Neither mattered for reconnaissance, where a wrong child is visible in a dump.
Both matter here, where a wrong child is an edit at the wrong offset.
"""

from __future__ import annotations

import struct
from typing import Dict, Iterator, List, Optional, Tuple

HEADER_LEN = 8
LARGE_HEADER_LEN = 16
UUID_LEN = 16

# A depth this deep is a malformed or hostile file, not a real one. Real trees
# reach about 7 (moov/trak/mdia/minf/stbl/stsd/avc1).
MAX_DEPTH = 32

# The XMP uuid, confirmed byte for byte 2026-09-06 in both an MP4 and a 3GP.
# Recorded for documentation and for tests. NOTHING in this tree keys removal
# on it: `uuid` is a generic extension mechanism and other producers put DRM
# headers and vendor blocks in it, so the engine removes every `uuid` rather
# than only this one. Do not "improve" that into an allowlist.
XMP_UUID = bytes.fromhex("be7acfcb97a942e89c71999491e3afac")


class IsobmffError(ValueError):
    """The bytes are not a box tree this walker can fully account for."""


# EVERY box this walker will descend into, and how many bytes of fixed payload
# come before the first child.
#
#   0  children start at the first payload byte
#   4  FullBox: a version byte and three flag bytes, then children
#   8  FullBox then a 32-bit entry count, then children
#
# `meta` is absent on purpose: its skip is sniffed, see rule 1 above.
CONTAINERS: Dict[bytes, int] = {
    b"moov": 0, b"trak": 0, b"edts": 0, b"mdia": 0, b"minf": 0, b"dinf": 0,
    b"stbl": 0, b"mvex": 0, b"moof": 0, b"traf": 0, b"mfra": 0, b"tapt": 0,
    b"sinf": 0, b"schi": 0, b"rinf": 0, b"strk": 0, b"strd": 0, b"cinf": 0,
    b"paen": 0, b"fiin": 0, b"segr": 0, b"gitn": 0, b"clip": 0, b"matt": 0,
    b"mdra": 0, b"iprp": 0, b"ipco": 0, b"grpl": 0,
    b"iref": 4,
    b"stsd": 8, b"dref": 8,
}

META = b"meta"

# Boxes this walker refuses to descend into even if some future edit adds them
# to CONTAINERS. Each is either padding, opaque payload, or a box the engine
# removes whole, so its internals are never needed and opening them can only
# add ways to misparse.
#
# `udta` is here for a measured reason and not for tidiness: a QuickTime `udta`
# may end with a four byte zero terminator that is not a box header, and the
# engine zeroes the entire `udta` subtree anyway. Parsing bytes nobody will
# read is pure downside.
OPAQUE = frozenset({
    b"free", b"skip", b"mdat", b"udta", b"uuid", b"ilst", b"ipma", b"iloc",
    b"iinf", b"pitm", b"idat", b"wide",
})


class Box:
    """One box, located in the buffer it was parsed from."""

    __slots__ = ("offset", "size", "type", "header_len", "uuid",
                 "payload_offset", "payload_len", "depth", "parent_type",
                 "children", "size_field")

    def __init__(self) -> None:
        self.children: List["Box"] = []
        self.uuid: Optional[bytes] = None

    @property
    def end(self) -> int:
        return self.offset + self.size

    @property
    def name(self) -> str:
        """A type safe to put in a message, whatever bytes it actually holds."""
        return "".join(
            chr(b) if 32 <= b < 127 else "\\x%02x" % b for b in self.type
        )

    def path_name(self) -> str:
        if self.uuid is not None:
            return "uuid{%s}" % self.uuid.hex()
        return self.name

    def __repr__(self) -> str:  # pragma: no cover - debugging aid
        return "<Box %s @%d size=%d>" % (self.path_name(), self.offset, self.size)


def _safe(kind: bytes) -> str:
    return "".join(chr(b) if 32 <= b < 127 else "\\x%02x" % b for b in kind)


def _be32(data: bytes, offset: int) -> int:
    return struct.unpack_from(">I", data, offset)[0]


def _looks_like_box(data: bytes, offset: int, end: int) -> bool:
    """
    Do the eight bytes at `offset` read as a plausible box header.

    Used only by the `meta` sniff. "Plausible" is deliberately strict: a size
    that fits inside the remaining range and is at least a header long, and
    four type bytes that are all printable ASCII. A four byte version/flags
    word of 00 00 00 00 fails both halves, which is what makes the sniff work.
    """
    if offset + HEADER_LEN > end:
        return False
    size = _be32(data, offset)
    kind = data[offset + 4:offset + 8]
    if not all(32 <= byte < 127 for byte in kind):
        return False
    return HEADER_LEN <= size <= (end - offset)


def meta_child_skip(data: bytes, box: Box) -> int:
    """
    Bytes of `meta` payload before its first child: 0 or 4. Trap 8, measured.

    Returns 0 for the Apple QuickTime Keys shape, which is not a FullBox, and 4
    for the ISO shape, which is. The sniff reads the bytes; it never reads the
    file's brand, because a single file contains both shapes and a brand cannot
    tell them apart.
    """
    start = box.payload_offset
    end = start + box.payload_len
    if _looks_like_box(data, start, end):
        return 0
    if _looks_like_box(data, start + 4, end):
        return 4
    # Neither reading produces a child. An empty or unparseable meta payload is
    # reported as the ISO shape and simply yields no children; the tiling check
    # in _parse_range then decides whether that is acceptable.
    return 4


def _child_skip(data: bytes, box: Box) -> Optional[int]:
    """How far into this box's payload its children start, or None for a leaf."""
    if box.type in OPAQUE:
        return None
    if box.type == META:
        return meta_child_skip(data, box)
    return CONTAINERS.get(box.type)


def _parse_range(data: bytes, start: int, end: int, depth: int,
                 parent: Optional[Box], out: List[Box]) -> None:
    if depth > MAX_DEPTH:
        raise IsobmffError(
            "box nesting deeper than %d levels at offset %d; refusing to walk "
            "further" % (MAX_DEPTH, start)
        )
    parent_name = parent.path_name() if parent is not None else "the file"
    offset = start
    while offset + HEADER_LEN <= end:
        size = _be32(data, offset)
        kind = bytes(data[offset + 4:offset + 8])
        header_len = HEADER_LEN
        size_field = 32
        if size == 1:
            if offset + LARGE_HEADER_LEN > end:
                raise IsobmffError(
                    "box '%s' at offset %d declares a 64-bit size but there is "
                    "no room for it" % (_safe(kind), offset)
                )
            size = struct.unpack_from(">Q", data, offset + HEADER_LEN)[0]
            header_len = LARGE_HEADER_LEN
            size_field = 64
        elif size == 0:
            # Legal only for the last box at a level: it runs to the end.
            size = end - offset
            size_field = 0

        box = Box()
        box.offset = offset
        box.size = size
        box.type = kind
        box.header_len = header_len
        box.size_field = size_field
        box.depth = depth
        box.parent_type = parent.type if parent is not None else None

        if kind == b"uuid":
            if size < header_len + UUID_LEN:
                raise IsobmffError(
                    "uuid box at offset %d is %d bytes, too small to hold its "
                    "16-byte extended type" % (offset, size)
                )
            box.uuid = bytes(data[offset + header_len:offset + header_len + UUID_LEN])
            header_len += UUID_LEN
            box.header_len = header_len

        if size < header_len:
            raise IsobmffError(
                "box '%s' at offset %d declares %d bytes, smaller than its own "
                "%d-byte header" % (_safe(kind), offset, size, header_len)
            )
        if offset + size > end:
            raise IsobmffError(
                "box '%s' at offset %d declares %d bytes, which runs past the "
                "end of %s" % (_safe(kind), offset, size, parent_name)
            )

        box.payload_offset = offset + header_len
        box.payload_len = size - header_len
        out.append(box)

        skip = _child_skip(data, box)
        if skip is not None and box.payload_len >= skip + HEADER_LEN:
            _parse_range(data, box.payload_offset + skip,
                         box.payload_offset + box.payload_len,
                         depth + 1, box, box.children)

        offset += size

    if offset != end:
        leftover = end - offset
        # Fewer than eight bytes cannot be a box header. A QuickTime `udta`
        # terminator is four zero bytes, and other producers pad the same way,
        # so an all-zero remainder is accepted as padding and anything else is
        # a range this walker cannot account for.
        if leftover < HEADER_LEN and not any(data[offset:end]):
            return
        raise IsobmffError(
            "%d bytes at offset %d are not accounted for by any box inside %s"
            % (leftover, offset, parent_name)
        )


def parse(data: bytes) -> List[Box]:
    """
    Walk a whole file into its top-level boxes. Raises IsobmffError otherwise.

    The first box must be `ftyp`. That is a stricter entry test than a decoder
    applies, and it is here because routing by extension is what CLAUDE.md trap
    8 is about: a real `.heic` on this machine turned out to be a JPEG that
    Instagram had renamed. A file whose first box is not `ftyp` is refused
    rather than half-parsed.
    """
    if len(data) < HEADER_LEN:
        raise IsobmffError("file is %d bytes, too short to hold one box" % len(data))
    if data[4:8] != b"ftyp":
        raise IsobmffError(
            "the first box is '%s', not 'ftyp'; this is not an ISO base media "
            "file whatever its extension says" % _safe(bytes(data[4:8]))
        )
    boxes: List[Box] = []
    _parse_range(data, 0, len(data), 0, None, boxes)
    if not boxes:
        raise IsobmffError("no boxes found")
    return boxes


def iter_boxes(boxes: List[Box]) -> Iterator[Box]:
    """Every box in the tree, parents before children."""
    for box in boxes:
        yield box
        for child in iter_boxes(box.children):
            yield child


def find_all(boxes: List[Box], kind: bytes) -> List[Box]:
    return [box for box in iter_boxes(boxes) if box.type == kind]


def ftyp_brands(data: bytes) -> Tuple[bytes, List[bytes]]:
    """
    (major_brand, compatible_brands) from the file type box.

    Raises rather than returning a sentinel. `av_engine.ftyp_brand` returns
    None for "this file does not say" because its caller has a sensible
    fallback; this one has none, and a brand nobody could read must not look
    like a brand that said something.
    """
    if len(data) < 16 or data[4:8] != b"ftyp":
        raise IsobmffError("no ftyp box at the start of the file")
    size = _be32(data, 0)
    if size < 16 or size > len(data):
        raise IsobmffError("ftyp box declares an impossible size %d" % size)
    major = bytes(data[8:12])
    compatible = [bytes(data[i:i + 4]) for i in range(16, size - 3, 4)]
    return major, compatible


# TIME FIELDS
#
# creation_time and modification_time, measured 2026-09-06 and re-confirmed
# here. Offsets are from the first payload byte, that is the byte after the
# size+type header.
#
#   version 0   creation +4 (4 bytes)   modification  +8 (4 bytes)
#   version 1   creation +4 (8 bytes)   modification +12 (8 bytes)
#
# All three of mvhd, tkhd and mdhd share this much. They stop agreeing
# immediately afterwards (tkhd has a track_ID and a reserved word where mvhd
# has a timescale), so nothing here may be generalised past these two fields.
TIME_BOXES = frozenset({b"mvhd", b"tkhd", b"mdhd"})


def time_fields(data: bytes, box: Box) -> List[Tuple[int, int]]:
    """
    [(absolute offset, width)] for creation_time and modification_time.

    Zero, do not remove: they are fixed-position fields inside a required
    header. Zero in this format is 1904-01-01, which reads as obviously
    scrubbed rather than as a plausible false time.
    """
    if box.type not in TIME_BOXES:
        raise IsobmffError("%s has no creation_time field" % box.name)
    if box.payload_len < 1:
        raise IsobmffError("%s box at offset %d has no payload" % (box.name, box.offset))
    version = data[box.payload_offset]
    if version == 0:
        fields = [(4, 4), (8, 4)]
    elif version == 1:
        fields = [(4, 8), (12, 8)]
    else:
        raise IsobmffError(
            "%s box at offset %d declares version %d; only 0 and 1 are defined "
            "and this engine will not guess at a third layout"
            % (box.name, box.offset, version)
        )
    needed = fields[-1][0] + fields[-1][1]
    if box.payload_len < needed:
        raise IsobmffError(
            "%s box at offset %d is %d payload bytes, too short for its "
            "version %d time fields" % (box.name, box.offset, box.payload_len, version)
        )
    return [(box.payload_offset + rel, width) for rel, width in fields]


# hdlr: FullBox(4) + pre_defined(4) + handler_type(4) + reserved[3](12) = 24,
# then a name that runs to the end of the box. The name is a free string and a
# real carrier: ffmpeg writes 'VideoHandler', devices and editors write their
# own product names in the same field.
HDLR_NAME_OFFSET = 24


def hdlr_name_range(box: Box) -> Optional[Tuple[int, int]]:
    """(offset, length) of a hdlr box's trailing name, or None if it has none."""
    if box.type != b"hdlr" or box.payload_len <= HDLR_NAME_OFFSET:
        return None
    return (box.payload_offset + HDLR_NAME_OFFSET,
            box.payload_len - HDLR_NAME_OFFSET)


# HEIF / AVIF ITEM TABLES
#
# `meta` in HEIF and AVIF is the file structure, not metadata. It holds iinf,
# iloc, iref and pitm, and removing it produces a file with no image in it.
# The Exif payload and the XMP packet do not live in `meta` at all: they live
# in `mdat`, and `iloc` says where. Measured 2026-09-06 on a real HEIC, and it
# is why HEIC is not the hard part: you edit none of iinf, iloc or iref, you
# zero the bytes iloc already points at.

class Item:
    """One entry of the HEIF item table, resolved to absolute byte extents."""

    __slots__ = ("item_id", "item_type", "name", "content_type", "extents",
                 "length_fields", "construction_method")

    def __init__(self, item_id: int, item_type: bytes, name: bytes,
                 content_type: bytes) -> None:
        self.item_id = item_id
        self.item_type = item_type
        self.name = name
        self.content_type = content_type
        self.extents: List[Tuple[int, int]] = []
        # Where each extent's LENGTH field lives in the iloc box, as
        # (absolute offset, width in bytes). Empty when length_size is 0, which
        # means the format did not store a length at all and there is nothing
        # to rewrite. The engine zeroes these so a removed item stops being
        # declared as present-with-content; exiftool's own `-all=` on a HEIC
        # leaves exactly the same shape, an item whose offset survives with
        # length 0.
        self.length_fields: List[Tuple[int, int]] = []
        self.construction_method = 0

    @property
    def type_name(self) -> str:
        return _safe(self.item_type)

    def __repr__(self) -> str:  # pragma: no cover - debugging aid
        return "<Item %d %s %r>" % (self.item_id, self.type_name, self.extents)


class _Reader:
    """A bounds-checked big-endian cursor. Every overrun is an IsobmffError."""

    def __init__(self, data: bytes, offset: int, end: int, what: str) -> None:
        self.data = data
        self.offset = offset
        self.end = end
        self.what = what

    def uint(self, width: int) -> int:
        if width == 0:
            return 0
        if self.offset + width > self.end:
            raise IsobmffError(
                "%s ran past the end of its box while reading %d bytes at "
                "offset %d" % (self.what, width, self.offset)
            )
        value = int.from_bytes(self.data[self.offset:self.offset + width], "big")
        self.offset += width
        return value


def parse_iinf(data: bytes, box: Box) -> Dict[int, Item]:
    """item_ID -> Item, from the item information box."""
    reader = _Reader(data, box.payload_offset,
                     box.payload_offset + box.payload_len, "iinf")
    version = reader.uint(1)
    reader.uint(3)
    count = reader.uint(2 if version == 0 else 4)
    items: Dict[int, Item] = {}
    for _ in range(count):
        entry_start = reader.offset
        size = reader.uint(4)
        kind = bytes(data[reader.offset:reader.offset + 4])
        reader.uint(4)
        if kind != b"infe":
            raise IsobmffError(
                "iinf entry at offset %d is '%s', not 'infe'"
                % (entry_start, _safe(kind))
            )
        if size < HEADER_LEN or entry_start + size > reader.end:
            raise IsobmffError(
                "infe entry at offset %d declares %d bytes, which runs past "
                "the end of iinf" % (entry_start, size)
            )
        entry_end = entry_start + size
        infe_version = reader.uint(1)
        reader.uint(3)
        if infe_version < 2:
            # Version 0 and 1 describe a file whose items are all images and
            # carry no item_type at all. Nothing to route on, so refuse rather
            # than guess which extents are metadata.
            raise IsobmffError(
                "infe version %d at offset %d carries no item_type; this "
                "engine will not guess which items are metadata"
                % (infe_version, entry_start)
            )
        item_id = reader.uint(2 if infe_version == 2 else 4)
        reader.uint(2)                      # item_protection_index
        item_type = bytes(data[reader.offset:reader.offset + 4])
        reader.uint(4)
        rest = bytes(data[reader.offset:entry_end])
        name, _, tail = rest.partition(b"\x00")
        content_type = b""
        if item_type == b"mime":
            content_type = tail.partition(b"\x00")[0]
        items[item_id] = Item(item_id, item_type, name, content_type)
        reader.offset = entry_end
    return items


def parse_iloc(data: bytes, box: Box, idat_offset: Optional[int]) -> Dict[int, Item]:
    """
    item_ID -> extents, resolved to ABSOLUTE file offsets.

    `construction_method` is read and honoured rather than skipped, which is
    the one place the Phase 0 instrument was wrong in a way that would have
    written at the wrong offsets. Method 0 is file-relative, method 1 is
    relative to the `idat` box payload, and method 2 (extents stored inside
    another item) is refused: resolving it needs the other item's own extents
    and nothing in this engine's job requires it.
    """
    reader = _Reader(data, box.payload_offset,
                     box.payload_offset + box.payload_len, "iloc")
    version = reader.uint(1)
    reader.uint(3)
    packed = reader.uint(1)
    offset_size, length_size = packed >> 4, packed & 0xF
    packed = reader.uint(1)
    base_offset_size = packed >> 4
    index_size = (packed & 0xF) if version in (1, 2) else 0
    count = reader.uint(2 if version < 2 else 4)

    out: Dict[int, Item] = {}
    for _ in range(count):
        item_id = reader.uint(2 if version < 2 else 4)
        method = 0
        if version in (1, 2):
            method = reader.uint(2) & 0xF
        reader.uint(2)                       # data_reference_index
        base = reader.uint(base_offset_size)
        extent_count = reader.uint(2)
        if method == 2:
            raise IsobmffError(
                "iloc item %d uses construction_method 2 (item-relative "
                "extents), which this engine will not resolve" % item_id
            )
        if method == 1:
            if idat_offset is None:
                raise IsobmffError(
                    "iloc item %d is idat-relative but the file has no idat box"
                    % item_id
                )
            origin = idat_offset + base
        else:
            origin = base
        item = Item(item_id, b"", b"", b"")
        item.construction_method = method
        for _ in range(extent_count):
            if index_size:
                reader.uint(index_size)
            extent_offset = reader.uint(offset_size)
            length_field = reader.offset
            extent_length = reader.uint(length_size)
            if length_size:
                item.length_fields.append((length_field, length_size))
            start = origin + extent_offset
            if start < 0 or start + extent_length > len(data):
                raise IsobmffError(
                    "iloc item %d points at bytes %d..%d, outside a %d-byte "
                    "file" % (item_id, start, start + extent_length, len(data))
                )
            item.extents.append((start, extent_length))
        out[item_id] = item
    return out


def parse_pitm(data: bytes, box: Box) -> int:
    """The primary item's ID."""
    reader = _Reader(data, box.payload_offset,
                     box.payload_offset + box.payload_len, "pitm")
    version = reader.uint(1)
    reader.uint(3)
    return reader.uint(2 if version == 0 else 4)


def item_table(data: bytes, meta: Box) -> Tuple[Dict[int, Item], Optional[int]]:
    """
    (item_ID -> Item with type and absolute extents, primary item ID).

    `meta` must be one of the boxes `item_metas` returned, so iinf and iloc are
    both known to be present.
    """
    children = {child.type: child for child in meta.children}
    iinf, iloc = children.get(b"iinf"), children.get(b"iloc")
    if iinf is None or iloc is None:
        raise IsobmffError("meta box at offset %d has no item table" % meta.offset)
    idat = children.get(b"idat")
    items = parse_iinf(data, iinf)
    located = parse_iloc(data, iloc, idat.payload_offset if idat else None)
    for item_id, placed in located.items():
        target = items.get(item_id)
        if target is None:
            raise IsobmffError(
                "iloc describes item %d, which iinf never declared" % item_id
            )
        target.extents = placed.extents
        target.length_fields = placed.length_fields
        target.construction_method = placed.construction_method
    primary = None
    pitm = children.get(b"pitm")
    if pitm is not None:
        primary = parse_pitm(data, pitm)
    return items, primary


def item_metas(boxes: List[Box]) -> List[Box]:
    """
    Every `meta` box that is a FILE STRUCTURE rather than a tag dictionary.

    Routed by CONTENT, never by brand or extension: a `meta` holding both
    `iinf` and `iloc` describes where the image data lives, and destroying it
    produces a file with no image in it. A `meta` without them is an iTunes or
    QuickTime Keys dictionary and is removable. That distinction is measured;
    it is not read off the ftyp brand, because a brand cannot tell the two
    `meta` HEADER shapes apart either.
    """
    out = []
    for box in iter_boxes(boxes):
        if box.type != META:
            continue
        names = {child.type for child in box.children}
        if b"iinf" in names and b"iloc" in names:
            out.append(box)
    return out


# colr, the box section 2.4 originally did not mention at all.
#
# 'prof' and 'rICC' carry an ICC profile. Measured on a real HEIC: 552 bytes of
# an ICC profile naming a device manufacturer, with a profile ID and a profile
# date, and it survives everything else in this engine. 'nclx' is three
# enumerations (primaries, transfer, matrix) plus a range flag: it cannot hold
# a string and it cannot carry identity.
ICC_COLOUR_TYPES = frozenset({b"prof", b"rICC"})


def colr_type(data: bytes, box: Box) -> bytes:
    """The four-byte colour_type of a colr box, or b'' if it has none."""
    if box.type != b"colr" or box.payload_len < 4:
        return b""
    return bytes(data[box.payload_offset:box.payload_offset + 4])
