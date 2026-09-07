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


# ===========================================================================
# PHASE 3: SAMPLE TABLES AND THE VIDEO BITSTREAM
# ===========================================================================
#
# Everything above this line reads the box tree. Everything below reads what
# the box tree POINTS AT, which is a different job with a different failure
# mode: a wrong box is a wrong edit, and a wrong sample offset is an edit
# inside somebody's picture.
#
# WHY SAMPLE TABLES ARE PARSED AT ALL, RATHER THAN SCANNING mdat
#
# `mdat` is not a NAL stream. Measured 2026-09-06 on an ffmpeg MP4 with one
# H.264 track and one AAC track: the video samples and the audio samples are
# INTERLEAVED inside one 15814-byte mdat, video first at offset 48 and audio
# first at offset 4612. Walking that payload as a chain of length-prefixed NAL
# units reads an AAC frame as a NAL length and lands wherever that says.
#
# So the only honest way to find the video bitstream is the way a decoder
# finds it: stsz for the sizes, stsc for how samples group into chunks, and
# stco or co64 for where each chunk starts. Measured on eight containers here
# (plain MP4, +faststart, QuickTime .mov, 3GP, video only, video plus AAC,
# video plus PCM, HEVC): the sample ranges derived below TILE the mdat payload
# exactly, with zero bytes left over, in every non-fragmented case.
#
# A fragmented MP4 keeps nothing in stbl at all: `moof/traf/trun` describes
# each fragment's samples and `moof/traf/tfhd` says where they start. That is
# parsed too, because the fixture set has one and a feature that silently does
# nothing on a whole container shape is worse than one that refuses.
#
# FAIL CLOSED, again. A sample table this module cannot resolve raises
# IsobmffError. It does not return an empty list: "there are no video samples"
# and "I could not find the video samples" are the two states CLAUDE.md trap 2
# is about, and they must never share a representation.


class Track:
    """One `trak`, resolved far enough to find its samples in the file."""

    __slots__ = ("trak", "track_id", "handler", "sample_entries", "samples")

    def __init__(self) -> None:
        self.sample_entries: List[Box] = []
        self.samples: List[Tuple[int, int]] = []

    def __repr__(self) -> str:  # pragma: no cover - debugging aid
        return "<Track %d %s %d samples>" % (
            self.track_id, _safe(self.handler or b"????"), len(self.samples))


def _first_child(box: Optional[Box], kind: bytes) -> Optional[Box]:
    if box is None:
        return None
    for child in box.children:
        if child.type == kind:
            return child
    return None


def _path(box: Optional[Box], *kinds: bytes) -> Optional[Box]:
    for kind in kinds:
        box = _first_child(box, kind)
        if box is None:
            return None
    return box


def tkhd_track_id(data: bytes, tkhd: Box) -> int:
    """
    The track_ID, at the one offset that depends on the tkhd version.

    version 0: creation(4) modification(4) track_ID(4)  -> payload + 12
    version 1: creation(8) modification(8) track_ID(4)  -> payload + 20
    """
    if tkhd.payload_len < 1:
        raise IsobmffError("tkhd at offset %d has no payload" % tkhd.offset)
    version = data[tkhd.payload_offset]
    if version == 0:
        rel = 12
    elif version == 1:
        rel = 20
    else:
        raise IsobmffError(
            "tkhd at offset %d declares version %d; only 0 and 1 are defined"
            % (tkhd.offset, version))
    if tkhd.payload_len < rel + 4:
        raise IsobmffError(
            "tkhd at offset %d is too short to hold a track_ID" % tkhd.offset)
    return _be32(data, tkhd.payload_offset + rel)


def tkhd_flags_field(tkhd: Box) -> Tuple[int, int]:
    """(absolute offset, width) of the 24-bit flags word of a tkhd FullBox."""
    if tkhd.payload_len < 4:
        raise IsobmffError(
            "tkhd at offset %d has no version/flags word" % tkhd.offset)
    return (tkhd.payload_offset + 1, 3)


# tkhd flags, ISO/IEC 14496-12 8.3.2.3.
TRACK_ENABLED = 0x000001
TRACK_IN_MOVIE = 0x000002
TRACK_IN_PREVIEW = 0x000004


def _stsz_sizes(data: bytes, stsz: Box) -> Tuple[List[int], bool]:
    """
    (sample sizes, whether they came from the per-sample table).

    `stz2`, the compact form, is deliberately NOT handled. Nothing in this
    fixture set produces one and guessing at a field width is how a sample
    offset lands inside a picture. The caller raises.
    """
    if stsz.payload_len < 12:
        raise IsobmffError("stsz at offset %d is too short" % stsz.offset)
    fixed = _be32(data, stsz.payload_offset + 4)
    count = _be32(data, stsz.payload_offset + 8)
    if fixed:
        return [fixed] * count, False
    needed = 12 + 4 * count
    if stsz.payload_len < needed:
        raise IsobmffError(
            "stsz at offset %d declares %d samples but holds only %d payload "
            "bytes" % (stsz.offset, count, stsz.payload_len))
    base = stsz.payload_offset + 12
    return [_be32(data, base + 4 * i) for i in range(count)], True


def _stbl_samples(data: bytes, stbl: Box) -> List[Tuple[int, int]]:
    """[(absolute offset, size)] for every sample a `stbl` describes."""
    stsz = _first_child(stbl, b"stsz")
    if stsz is None:
        if _first_child(stbl, b"stz2") is not None:
            raise IsobmffError(
                "stbl at offset %d uses the compact stz2 sample size table, "
                "which this engine does not parse" % stbl.offset)
        raise IsobmffError(
            "stbl at offset %d has no stsz sample size table" % stbl.offset)
    stsc = _first_child(stbl, b"stsc")
    stco = _first_child(stbl, b"stco") or _first_child(stbl, b"co64")
    if stsc is None or stco is None:
        raise IsobmffError(
            "stbl at offset %d has no stsc or no stco/co64; its samples cannot "
            "be located" % stbl.offset)

    sizes, _ = _stsz_sizes(data, stsz)

    if stsc.payload_len < 8:
        raise IsobmffError("stsc at offset %d is too short" % stsc.offset)
    runs_count = _be32(data, stsc.payload_offset + 4)
    if stsc.payload_len < 8 + 12 * runs_count:
        raise IsobmffError(
            "stsc at offset %d declares %d runs it does not hold"
            % (stsc.offset, runs_count))
    runs = []
    for i in range(runs_count):
        at = stsc.payload_offset + 8 + 12 * i
        runs.append((_be32(data, at), _be32(data, at + 4)))

    width = 8 if stco.type == b"co64" else 4
    if stco.payload_len < 8:
        raise IsobmffError("%s at offset %d is too short" % (stco.name, stco.offset))
    chunk_count = _be32(data, stco.payload_offset + 4)
    if stco.payload_len < 8 + width * chunk_count:
        raise IsobmffError(
            "%s at offset %d declares %d chunks it does not hold"
            % (stco.name, stco.offset, chunk_count))
    chunks = []
    for i in range(chunk_count):
        at = stco.payload_offset + 8 + width * i
        chunks.append(int.from_bytes(data[at:at + width], "big"))

    out: List[Tuple[int, int]] = []
    index = 0
    for chunk_index, chunk_offset in enumerate(chunks):
        per_chunk = 0
        for first_chunk, samples_per_chunk in runs:
            if first_chunk - 1 <= chunk_index:
                per_chunk = samples_per_chunk
        offset = chunk_offset
        for _ in range(per_chunk):
            if index >= len(sizes):
                break
            size = sizes[index]
            if offset < 0 or offset + size > len(data):
                raise IsobmffError(
                    "sample %d of the chunk at offset %d runs to %d, outside a "
                    "%d-byte file" % (index, chunk_offset, offset + size, len(data)))
            out.append((offset, size))
            offset += size
            index += 1
    return out


def _trex_defaults(data: bytes, boxes: Sequence[Box]) -> Dict[int, Tuple[int, int]]:
    """track_ID -> (default_sample_description_index, default_sample_size)."""
    out: Dict[int, Tuple[int, int]] = {}
    for trex in find_all(list(boxes), b"trex"):
        if trex.payload_len < 20:
            raise IsobmffError("trex at offset %d is too short" % trex.offset)
        base = trex.payload_offset + 4
        out[_be32(data, base)] = (_be32(data, base + 4), _be32(data, base + 12))
    return out


# tfhd and trun flag bits, ISO/IEC 14496-12 8.8.7 and 8.8.8.
TFHD_BASE_DATA_OFFSET = 0x000001
TFHD_SAMPLE_DESCRIPTION_INDEX = 0x000002
TFHD_DEFAULT_SAMPLE_DURATION = 0x000008
TFHD_DEFAULT_SAMPLE_SIZE = 0x000010
TFHD_DEFAULT_SAMPLE_FLAGS = 0x000020
TFHD_DEFAULT_BASE_IS_MOOF = 0x020000

TRUN_DATA_OFFSET = 0x000001
TRUN_FIRST_SAMPLE_FLAGS = 0x000004
TRUN_SAMPLE_DURATION = 0x000100
TRUN_SAMPLE_SIZE = 0x000200
TRUN_SAMPLE_FLAGS = 0x000400
TRUN_SAMPLE_CTS = 0x000800


def _fragment_samples(data: bytes, boxes: Sequence[Box],
                      defaults: Dict[int, Tuple[int, int]]
                      ) -> Dict[int, List[Tuple[int, int]]]:
    """
    track_ID -> [(absolute offset, size)] for every `moof` in the file.

    The base offset rules are the specification's, spelled out because they are
    the part a reader gets wrong: base-data-offset when present, else the
    enclosing `moof` when default-base-is-moof is set, else the `moof` for the
    FIRST track fragment and the end of the previous fragment's data for each
    one after it.
    """
    out: Dict[int, List[Tuple[int, int]]] = {}
    for moof in find_all(list(boxes), b"moof"):
        running = moof.offset
        first_traf = True
        for traf in moof.children:
            if traf.type != b"traf":
                continue
            tfhd = _first_child(traf, b"tfhd")
            if tfhd is None or tfhd.payload_len < 8:
                raise IsobmffError(
                    "traf at offset %d has no usable tfhd" % traf.offset)
            reader = _Reader(data, tfhd.payload_offset,
                             tfhd.payload_offset + tfhd.payload_len, "tfhd")
            reader.uint(1)
            flags = reader.uint(3)
            track_id = reader.uint(4)
            base = None
            if flags & TFHD_BASE_DATA_OFFSET:
                base = reader.uint(8)
            if flags & TFHD_SAMPLE_DESCRIPTION_INDEX:
                reader.uint(4)
            if flags & TFHD_DEFAULT_SAMPLE_DURATION:
                reader.uint(4)
            default_size = defaults.get(track_id, (1, 0))[1]
            if flags & TFHD_DEFAULT_SAMPLE_SIZE:
                default_size = reader.uint(4)
            if flags & TFHD_DEFAULT_SAMPLE_FLAGS:
                reader.uint(4)
            if base is None:
                if flags & TFHD_DEFAULT_BASE_IS_MOOF or first_traf:
                    base = moof.offset
                else:
                    base = running
            first_traf = False

            cursor = base
            for trun in traf.children:
                if trun.type != b"trun":
                    continue
                run = _Reader(data, trun.payload_offset,
                              trun.payload_offset + trun.payload_len, "trun")
                run.uint(1)
                run_flags = run.uint(3)
                count = run.uint(4)
                if run_flags & TRUN_DATA_OFFSET:
                    raw = run.uint(4)
                    signed = raw - (1 << 32) if raw & 0x80000000 else raw
                    cursor = base + signed
                if run_flags & TRUN_FIRST_SAMPLE_FLAGS:
                    run.uint(4)
                for _ in range(count):
                    if run_flags & TRUN_SAMPLE_DURATION:
                        run.uint(4)
                    size = run.uint(4) if run_flags & TRUN_SAMPLE_SIZE else default_size
                    if run_flags & TRUN_SAMPLE_FLAGS:
                        run.uint(4)
                    if run_flags & TRUN_SAMPLE_CTS:
                        run.uint(4)
                    if size and (cursor < 0 or cursor + size > len(data)):
                        raise IsobmffError(
                            "a fragment sample of track %d runs to %d, outside "
                            "a %d-byte file" % (track_id, cursor + size, len(data)))
                    out.setdefault(track_id, []).append((cursor, size))
                    cursor += size
            running = max(running, cursor)
    return out


def tracks(data: bytes, boxes: Sequence[Box]) -> List[Track]:
    """
    Every `trak`, with its handler, its sample entries and its sample ranges.

    Fragment samples are appended to the track they belong to, so a caller
    never has to know whether a file is fragmented.
    """
    fragments = _fragment_samples(data, boxes, _trex_defaults(data, boxes))
    out: List[Track] = []
    for trak in find_all(list(boxes), b"trak"):
        tkhd = _first_child(trak, b"tkhd")
        mdia = _first_child(trak, b"mdia")
        if tkhd is None or mdia is None:
            raise IsobmffError(
                "trak at offset %d has no tkhd or no mdia" % trak.offset)
        track = Track()
        track.trak = trak
        track.track_id = tkhd_track_id(data, tkhd)
        hdlr = _first_child(mdia, b"hdlr")
        if hdlr is None or hdlr.payload_len < 12:
            raise IsobmffError(
                "the mdia at offset %d has no readable hdlr" % mdia.offset)
        track.handler = bytes(
            data[hdlr.payload_offset + 8:hdlr.payload_offset + 12])
        stbl = _path(mdia, b"minf", b"stbl")
        if stbl is not None:
            stsd = _first_child(stbl, b"stsd")
            if stsd is not None:
                track.sample_entries = list(stsd.children)
            track.samples = _stbl_samples(data, stbl)
        track.samples.extend(fragments.get(track.track_id, []))
        out.append(track)
    return out


def stbl_of(track: Track) -> Optional[Box]:
    """The sample table box of a track, or None if it has none."""
    return _path(track.trak, b"mdia", b"minf", b"stbl")


def tkhd_of(track: Track) -> Optional[Box]:
    return _first_child(track.trak, b"tkhd")


# ---------------------------------------------------------- codec config boxes
#
# Rule 2 at the top of this file says a sample entry is a leaf, because opening
# one means knowing the fixed prefix of every sample entry CLASS. That still
# stands for the general walk. The function below opens exactly one class, the
# VisualSampleEntry, whose prefix is 78 bytes and is fixed by ISO/IEC 14496-12,
# and it is used for exactly one purpose: finding `avcC` or `hvcC`. The caller
# gates it on the enclosing track's handler being 'vide', which is the same
# gate the compressorname edit already uses.

VISUAL_SAMPLE_ENTRY_PREFIX = 78
AVCC = b"avcC"
HVCC = b"hvcC"


def visual_sample_entry_children(data: bytes, entry: Box) -> List[Box]:
    """The child boxes that follow a VisualSampleEntry's 78-byte prefix."""
    if entry.payload_len < VISUAL_SAMPLE_ENTRY_PREFIX + HEADER_LEN:
        return []
    start = entry.payload_offset + VISUAL_SAMPLE_ENTRY_PREFIX
    end = entry.payload_offset + entry.payload_len
    out: List[Box] = []
    _parse_range(data, start, end, entry.depth + 1, entry, out)
    return out


def avc_config(data: bytes, box: Box) -> Tuple[int, List[Tuple[int, int]]]:
    """
    (NAL length field size, [(offset, length)] of the NAL units avcC stores).

    AVCDecoderConfigurationRecord, ISO/IEC 14496-15. The length size is the low
    two bits of the byte at +4, plus one. It is READ rather than assumed: a
    record may say 1, 2 or 4, and a walk that assumes 4 reads a sample as
    garbage from its first byte.
    """
    reader = _Reader(data, box.payload_offset,
                     box.payload_offset + box.payload_len, "avcC")
    reader.uint(4)
    length_size = (reader.uint(1) & 0x03) + 1
    nals: List[Tuple[int, int]] = []
    for count_mask in (0x1F, 0xFF):
        count = reader.uint(1) & count_mask
        for _ in range(count):
            size = reader.uint(2)
            if reader.offset + size > reader.end:
                raise IsobmffError(
                    "avcC at offset %d declares a %d-byte parameter set that "
                    "runs past its end" % (box.offset, size))
            nals.append((reader.offset, size))
            reader.offset += size
    return length_size, nals


# HEVCDecoderConfigurationRecord fixed prefix, ISO/IEC 14496-15:
#   1 configurationVersion, 1 profile_space/tier/profile_idc,
#   4 profile_compatibility, 6 constraint_indicator, 1 level_idc,
#   2 min_spatial_segmentation, 1 parallelismType, 1 chromaFormat,
#   1 bitDepthLuma, 1 bitDepthChroma, 2 avgFrameRate,
#   1 constantFrameRate/numTemporalLayers/temporalIdNested/lengthSizeMinusOne,
#   1 numOfArrays  = 23 bytes, so the arrays start at +23.
HVCC_LENGTH_SIZE_OFFSET = 21
HVCC_ARRAY_COUNT_OFFSET = 22
HVCC_ARRAYS_OFFSET = 23


def hevc_config(data: bytes, box: Box) -> Tuple[int, List[Tuple[int, int]]]:
    """
    (NAL length field size, [(offset, length)] of the NAL units hvcC stores).

    MEASURED 2026-09-06 and it is the whole reason this function exists: an
    x265 stream muxed by ffmpeg puts its user-data SEI NAL in the hvcC ARRAYS,
    not in mdat. A 2334-byte prefix SEI sat in the sample entry of a 7407-byte
    file, and no amount of mdat scanning would have found it. The same file
    muxed with `-tag:v hev1` puts it in exactly the same place.
    """
    if box.payload_len < HVCC_ARRAYS_OFFSET:
        raise IsobmffError(
            "hvcC at offset %d is %d bytes, too short for its fixed prefix"
            % (box.offset, box.payload_len))
    base = box.payload_offset
    length_size = (data[base + HVCC_LENGTH_SIZE_OFFSET] & 0x03) + 1
    array_count = data[base + HVCC_ARRAY_COUNT_OFFSET]
    reader = _Reader(data, base + HVCC_ARRAYS_OFFSET,
                     base + box.payload_len, "hvcC")
    nals: List[Tuple[int, int]] = []
    for _ in range(array_count):
        reader.uint(1)
        count = reader.uint(2)
        for _ in range(count):
            size = reader.uint(2)
            if reader.offset + size > reader.end:
                raise IsobmffError(
                    "hvcC at offset %d declares a %d-byte NAL that runs past "
                    "its end" % (box.offset, size))
            nals.append((reader.offset, size))
            reader.offset += size
    return length_size, nals


# --------------------------------------------------------------- NAL units
#
# TWO FORMS, AND ISO BASE MEDIA ONLY EVER USES ONE OF THEM.
#
# Length-prefixed (AVCC/HVCC): every NAL is preceded by a big-endian length
# field whose WIDTH comes from the configuration record, not from a constant.
# This is the form inside `mdat` samples, and, with a fixed width of 2, the
# form inside the avcC and hvcC arrays themselves.
#
# Annex B: NALs separated by 00 00 01 or 00 00 00 01 start codes. This is the
# form of a raw .h264 or .h265 elementary stream. It is handled here because
# the SEI rewrite is the same operation in both forms and a helper that knows
# only one of them invites a caller to reach for the wrong one; it is NOT here
# because ISO base media uses it, and no caller in this tree passes it an ISO
# base media buffer.

H264_NAL_TYPE_SEI = 6
H265_NAL_TYPE_PREFIX_SEI = 39
H265_NAL_TYPE_SUFFIX_SEI = 40

SEI_USER_DATA_UNREGISTERED = 5
SEI_FILLER_PAYLOAD = 3

RBSP_TRAILING_BITS = 0x80
FF_BYTE = 0xFF


def length_prefixed_nals(data: bytes, start: int, end: int,
                         length_size: int) -> List[Tuple[int, int]]:
    """
    [(offset, length)] of the NAL units in [start, end), length-prefixed.

    The whole range must tile exactly. A trailing fragment that is not a whole
    NAL means the length size or the range was wrong, and continuing from a
    wrong offset is how an edit lands inside a picture.
    """
    if length_size not in (1, 2, 4):
        raise IsobmffError(
            "a NAL length field of %d bytes is not one the specification "
            "defines" % length_size)
    out: List[Tuple[int, int]] = []
    offset = start
    while offset < end:
        if offset + length_size > end:
            raise IsobmffError(
                "%d bytes at offset %d are too few to hold a NAL length field"
                % (end - offset, offset))
        size = int.from_bytes(data[offset:offset + length_size], "big")
        offset += length_size
        if size == 0 or offset + size > end:
            raise IsobmffError(
                "a NAL at offset %d declares %d bytes, which do not fit in the "
                "%d bytes remaining" % (offset, size, end - offset))
        out.append((offset, size))
        offset += size
    return out


def annexb_nals(data: bytes, start: int, end: int) -> List[Tuple[int, int]]:
    """[(offset, length)] of the NAL units in an Annex B byte stream."""
    starts: List[int] = []
    offset = start
    while offset + 3 <= end:
        if data[offset] == 0 and data[offset + 1] == 0:
            if data[offset + 2] == 1:
                starts.append(offset + 3)
                offset += 3
                continue
            if (data[offset + 2] == 0 and offset + 4 <= end
                    and data[offset + 3] == 1):
                starts.append(offset + 4)
                offset += 4
                continue
        offset += 1
    out: List[Tuple[int, int]] = []
    for index, begin in enumerate(starts):
        if index + 1 == len(starts):
            finish = end
        else:
            # MEASURED and got wrong once, so it is spelled out. `starts` holds
            # the offset AFTER a start code, so the next NAL's three-byte
            # 00 00 01 sits immediately before it and is not part of this NAL.
            # Backing off only the ZERO bytes leaves the 0x01 attached, which
            # made a 686-byte SEI read as 690 bytes; overwriting all 690 then
            # destroyed the SPS that followed and ffmpeg answered
            # "non-existing PPS 0 referenced" on every frame.
            finish = starts[index + 1] - 3
        # A four-byte start code, and any trailing_zero_8bits before it, are
        # padding that belongs to the separator rather than to the payload.
        while finish > begin and data[finish - 1] == 0:
            finish -= 1
        if finish > begin:
            out.append((begin, finish - begin))
    return out


def unescape_rbsp(data: bytes, start: int, end: int) -> bytes:
    """
    The RBSP of a NAL payload, with emulation prevention bytes removed.

    A 0x03 preceded by two zero bytes is an escape and is dropped. Read only:
    nothing in this module ever writes an escaped byte, for the reason spelled
    out over `sei_filler_bytes`.
    """
    out = bytearray()
    offset = start
    while offset < end:
        if (offset + 2 < end and data[offset] == 0 and data[offset + 1] == 0
                and data[offset + 2] == 3):
            out += data[offset:offset + 2]
            offset += 3
            continue
        out.append(data[offset])
        offset += 1
    return bytes(out)


def sei_messages(rbsp: bytes) -> List[Tuple[int, int, int]]:
    """
    [(payload_type, payload_size, payload_offset)] for one SEI NAL's RBSP.

    Raises rather than returning what it managed to read. An SEI NAL whose
    message list does not close is one whose contents are unknown, and an
    unknown carrier must never be reported as an absent one.
    """
    out: List[Tuple[int, int, int]] = []
    offset = 0
    total = len(rbsp)
    while offset < total:
        if rbsp[offset] == RBSP_TRAILING_BITS:
            return out
        payload_type = 0
        while offset < total and rbsp[offset] == FF_BYTE:
            payload_type += 255
            offset += 1
        if offset >= total:
            raise IsobmffError("an SEI payload type ran off the end of its NAL")
        payload_type += rbsp[offset]
        offset += 1
        payload_size = 0
        while offset < total and rbsp[offset] == FF_BYTE:
            payload_size += 255
            offset += 1
        if offset >= total:
            raise IsobmffError("an SEI payload size ran off the end of its NAL")
        payload_size += rbsp[offset]
        offset += 1
        if offset + payload_size > total:
            raise IsobmffError(
                "an SEI message declares %d payload bytes but only %d remain "
                "in its NAL" % (payload_size, total - offset))
        out.append((payload_type, payload_size, offset))
        offset += payload_size
    return out


def sei_nal_header_len(hevc: bool) -> int:
    """1 byte in H.264, 2 in H.265. The one number the two codecs disagree on."""
    return 2 if hevc else 1


def is_sei_nal(data: bytes, offset: int, length: int, hevc: bool) -> bool:
    """Whether the NAL at `offset` is an SEI NAL, read from its header byte."""
    header = sei_nal_header_len(hevc)
    if length < header + 1:
        return False
    first = data[offset]
    if first & 0x80:
        # forbidden_zero_bit. A NAL with it set is not a NAL.
        return False
    if hevc:
        return ((first >> 1) & 0x3F) in (H265_NAL_TYPE_PREFIX_SEI,
                                         H265_NAL_TYPE_SUFFIX_SEI)
    return (first & 0x1F) == H264_NAL_TYPE_SEI


def sei_user_data_nals(data: bytes, nals: Sequence[Tuple[int, int]],
                       hevc: bool) -> List[Tuple[int, int, int]]:
    """
    [(nal offset, nal length, header length)] for the SEI NALs that carry a
    user_data_unregistered message.

    An SEI NAL with no such message is not returned, which is what makes the
    overcorrection test possible: a stream carrying only pic_timing SEIs comes
    back empty and is left byte-identical.
    """
    out: List[Tuple[int, int, int]] = []
    header = sei_nal_header_len(hevc)
    for offset, length in nals:
        if not is_sei_nal(data, offset, length, hevc):
            continue
        rbsp = unescape_rbsp(data, offset + header, offset + length)
        for payload_type, _size, _at in sei_messages(rbsp):
            if payload_type == SEI_USER_DATA_UNREGISTERED:
                out.append((offset, length, header))
                break
    return out


def _fit_filler_message(budget: int) -> Optional[int]:
    """
    The filler payload size whose whole message occupies exactly `budget` bytes.

    A message costs 1 type byte, then its size field, then its payload. The
    size field is `size // 255 + 1` bytes: an 0xFF for each whole 255, then the
    remainder. So a message of payload S costs S + S // 255 + 2, which SKIPS a
    value every 256 bytes (257, 513, 769 and so on). Those gaps are why the
    caller can need two messages, and why this returns None rather than an
    approximation.
    """
    for size_bytes in range(1, budget // 255 + 3):
        size = budget - 1 - size_bytes
        if size >= 0 and size // 255 + 1 == size_bytes:
            return size
    return None


def sei_filler_bytes(length: int) -> bytes:
    """
    Exactly `length` bytes of SEI filler_payload messages plus trailing bits.

    THIS IS THE WHOLE ANSWER TO THE EMULATION PREVENTION PROBLEM, so it is
    written down here rather than left to be rediscovered.

    A NAL payload is an ESCAPED RBSP: a 0x03 is inserted wherever the raw bytes
    would otherwise contain 00 00 00, 00 00 01, 00 00 02 or 00 00 03. Removing
    those bytes shortens the buffer and inserting them lengthens it, and this
    engine's entire strategy is that no length ever changes. So the only safe
    replacement is one that needs NO escaping at all, and the only way to know
    that is to prove it about the bytes emitted here.

    The proof: every byte written below is 0x03 (a payload type), 0xFF (a size
    continuation or a filler byte), a size remainder in 0x00..0xFE, or the
    single trailing 0x80. A 0x00 can therefore appear only as a size remainder,
    and a size remainder is always followed by a filler byte (0xFF), by the
    next message's type byte (0x03), or by the trailing 0x80. Two zero bytes
    never become adjacent, so the sequence 00 00 never occurs, so no escape is
    ever required and the escaped form is byte for byte the form written here.
    That invariant is ASSERTED at the end of this function rather than trusted.

    filler_payload is SEI payload type 3 in both H.264 and H.265 Annex D, and
    its payload is defined as ff_byte repeated, which is exactly what is
    written. The replacement is a conformant SEI message, not a hole.
    """
    if length < 3:
        raise IsobmffError(
            "%d bytes cannot hold an SEI message and its trailing bits" % length)
    budget = length - 1
    payloads: List[int] = []
    while budget:
        size = _fit_filler_message(budget)
        if size is not None:
            payloads.append(size)
            budget = 0
            break
        # A gap in the cost function. The cheapest message is a zero-length one
        # costing two bytes, and stepping down by two always lands on a
        # reachable value; `test_sei_filler_tiles_every_length` proves it over
        # every length this can be asked for.
        payloads.append(0)
        budget -= 2
        if budget == 1:
            raise IsobmffError(
                "%d bytes cannot be tiled by SEI filler messages" % length)

    out = bytearray()
    for size in payloads:
        out.append(SEI_FILLER_PAYLOAD)
        remainder = size
        while remainder >= 255:
            out.append(FF_BYTE)
            remainder -= 255
        out.append(remainder)
        out += b"\xff" * size
    out.append(RBSP_TRAILING_BITS)

    if len(out) != length:
        raise IsobmffError(
            "the SEI filler came out %d bytes long, not %d" % (len(out), length))
    for index in range(len(out) - 1):
        if out[index] == 0 and out[index + 1] == 0:
            raise IsobmffError(
                "the SEI filler put two zero bytes together at offset %d, which "
                "would need an emulation prevention byte" % index)
    return bytes(out)
