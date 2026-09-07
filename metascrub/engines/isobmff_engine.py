"""
ISO base media engine: pure-Python box surgery for MP4, MOV, HEIC, AVIF, 3GP.
No ffmpeg, no exiftool, because neither can run on a phone.

Phase 2 of the Android media build. docs/ANDROID-MEDIA-BUILD.md section 2.4 is
the removal table, 2.5 is the offset strategy, 3.2 is the structural assertion
the tests make over the output. The container rules live next door in
`metascrub/isobmff.py`; this file is only the policy.

Deliberately NOT wired into CAPABILITIES. The .mp4, .mov and .heic rows still
route to the av and exiftool engines. Flipping the default is a separate
decision with its own evidence, and `tests/test_isobmff_engine.py` asserts the
rows are unchanged so that flip cannot happen by accident.

THE STRATEGY, IN ONE SENTENCE

Overwrite each removed box in place with a zero-filled `free` box of
byte-identical size, so the file length never changes and no `stco` or `co64`
offset ever moves.

WHY IT IS THAT AND NOT COMPACTION

Sample tables hold ABSOLUTE file offsets into `mdat`. Removing any box that
sits before `mdat` shifts every one of them. Measured 2026-09-06: excising the
2824-byte XMP `uuid` box from an MP4 and fixing nothing produced
"Invalid NAL unit size (93001346 > 4081)" on essentially every frame and
ffmpeg exit 69. The file was still 30507 plausible-looking bytes.

The in-place strategy was measured on five containers (moov after mdat, moov
before mdat, +faststart, fragmented, QuickTime .mov, 3GP): length unchanged,
full decode clean, decoded framemd5 bit identical, exiftool reporting no
metadata group at all. It also preserves the ftyp brand, which the existing
ffmpeg-remux av engine cannot: its own docstring records that the mp4 muxer
rewrites an `mp41` input as `isom`.

`co64` is NOT MEASURED. It needs an mdat over 4 GiB and none was available.
That is acceptable here only because this engine moves no offset at all. It
becomes a blocker the moment anyone attempts compaction.

WHAT GETS REMOVED, AND WHY EACH ONE IS HERE

  udta         the GPS atom 0xA9 'xyz' (bytes a9 78 79 7a, payload ISO 6709
               like '+44.5588-072.5778+315.000/'), 0xA9 'mak', 0xA9 'mod',
               Apple and Samsung vendor boxes, AND `moov/udta/XMP_`, which is
               where XMP lives in a QuickTime .mov. Removed as a whole subtree.

  uuid         XMP under be7acfcb-97a9-42e8-9c71-999491e3afac, and every other
               uuid. THIS IS NOT OPTIONAL AND IT IS NOT A FALLBACK. Measured
               2026-09-06: exiftool's DEFAULT GPS write on an MP4 puts the
               coordinates in XMP in the uuid box and writes NO 0xA9 xyz atom
               at all. The two carriers are alternatives; an engine that
               implements `udta` alone leaks the location completely.

  meta, ilst   iTunes and QuickTime Keys tag dictionaries, but ONLY where
               `meta` is a dictionary. See the next section.

  colr         where it carries an ICC profile ('prof' or 'rICC'). Section 2.4
               did not mention this box at all and it survives everything the
               original table described. Measured: 552 bytes of ICC on a real
               HEIC, naming a device manufacturer, a profile date and a
               profile ID.

  hdlr name    the free-string handler name at the tail of every hdlr box.
               ffmpeg writes 'VideoHandler'; devices and editors write their
               own product names in the same field. Zeroed, not removed.

  compressor-  the 32-byte `compressorname` and the 4-byte vendor field of
  name, vendor  every VISUAL sample entry. Section 2.4 does not mention either.
               Measured on this engine's own output before they were handled:
               `QuickTime:CompressorName = 'Lavc61.26.100 libx264'` and
               `QuickTime:VendorID = 'FFMP'` both survived every other removal
               here. They name the software that wrote the file, which is the
               same class of value as a PDF Producer string, and on a phone
               they name the phone's encoder. Both sit at fixed offsets inside
               a VisualSampleEntry and both are zeroed in place.

               The ONLY place this engine reads inside a sample entry, and it
               is gated twice: the enclosing track's hdlr must say `vide`, and
               the entry must be at least the 78 bytes a VisualSampleEntry
               occupies before its optional child boxes start. An audio or
               timed-metadata entry is never touched, because their layouts put
               different things at those offsets.

  HEIF items   the bytes `iloc` points at, for every item whose type is not an
               image. You edit NONE of iinf, iloc or iref. Measured on a real
               HEIC: 150 exiftool tags down to 68, Make, Model, timestamps and
               the whole XMP packet gone, decoded pixels bit identical, length
               unchanged to the byte.

  free, skip   their CONTENTS are zeroed rather than the boxes being removed.
               They can hold orphaned data from a previous edit, and they
               cannot move without moving everything after them.

  times        creation_time and modification_time in mvhd, tkhd and mdhd are
               zeroed, not removed: they are fixed-position fields inside a
               required header. Zero is 1904-01-01, which reads as obviously
               scrubbed rather than as a plausible false time.

THE ONE THING THAT MUST NEVER BE REMOVED

The top-level `meta` box in HEIF and AVIF is the file structure. It holds the
item table describing where the image data lives, and removing it produces a
file with no image in it. Measured: free-filling it made libheif answer
"cannot identify image file" and ffprobe answer "moov atom not found".

`meta` in MP4 and MOV is a tag dictionary and is removable. The two are told
apart BY CONTENT, in `isobmff.item_metas`: a `meta` holding both `iinf` and
`iloc` is structural. Not by brand, not by extension. A single exiftool-written
MP4 holds two `meta` boxes with two different HEADER shapes, so a name cannot
settle it and neither can a brand.

WHERE IT FAILS CLOSED

Anything `isobmff.parse` will not fully account for raises EngineError, and it
raises before a temporary file exists, so the input is untouched and nothing is
left behind. A HEIF item extent that does not land inside an `mdat` or `idat`
payload raises too: an item table can point anywhere, and zeroing what it says
without checking where that is would let a malformed file steer an edit into
its own `moov`.

WHAT THIS ENGINE DOES NOT REACH, STATED RATHER THAN IMPLIED

  - Sample entry CHILD boxes. `avc1`, `hvc1` and `mp4a` inside `stsd` can
    legally hold child boxes, including a `colr` and vendor atoms, after their
    fixed prefix. `isobmff.py` treats sample entries as leaves rather than
    guessing at the prefix length of every sample entry class, so this engine
    reaches the two fixed VisualSampleEntry fields named above and nothing
    past them.
  - The video bitstream. An H.264 or HEVC SEI user-data NAL inside `mdat` is
    untouched. That is Phase 3's, and it is why no honest verdict for this
    format can be better than PARTIAL until it is done.
  - `mdhd` language. Left as it is: a language code is a weak signal and
    rewriting it changes track selection semantics.
  - Item table tidiness. A zeroed Exif item stays DECLARED in `iinf`, pointing
    at zeros. exiftool's own `-all=` leaves exactly the same dangling
    declaration with length 0, so this matches the reference implementation.
"""

from __future__ import annotations

import os
import struct
from typing import List, Optional, Sequence, Set, Tuple

from .. import isobmff
from ..isobmff import Box, IsobmffError
from .base import BaseEngine, EngineError, atomic_replace, temp_beside

FREE = b"free"

# Removed wherever they appear in the tree, as whole subtrees.
REMOVED_TYPES = frozenset({b"udta", b"uuid", b"ilst"})

# Free-space boxes: kept in place, contents zeroed.
FREE_SPACE_TYPES = frozenset({b"free", b"skip"})

# HEIF/AVIF item types that ARE the picture. Everything else is a carrier and
# its bytes are zeroed where `iloc` says they are.
#
# A KEEP-LIST rather than a remove-list, which is the same call webp_engine
# makes about RIFF chunks and for the same reason: a remove-list is blind to
# the carrier nobody has met yet, and 'Exif' and 'mime' are not the only two
# item types that can hold identity ('uri ' and vendor types exist). The cost
# is stated rather than hidden: an image item type absent from this list would
# be zeroed, so the primary item is protected unconditionally below and the
# tests decode every output rather than trusting an exit status.
IMAGE_ITEM_TYPES = frozenset({
    b"hvc1", b"hev1", b"hvt1",          # HEVC coded images and tiles
    b"avc1", b"avc3",                   # H.264 coded images
    b"av01",                            # AV1, that is AVIF
    b"vvc1", b"vvi1",                   # VVC
    b"jpeg", b"j2k1", b"mjpg",          # legacy coded images
    b"unci",                            # uncompressed
    b"grid", b"iovl", b"iden", b"mask", b"tmap",   # derived images
})

# Named only so the report can say which carrier the user just lost. Anything
# outside IMAGE_ITEM_TYPES is removed whether or not it is named here.
KNOWN_METADATA_ITEM_TYPES = {
    b"Exif": "EXIF",
    b"mime": "XMP or another MIME-typed packet",
    b"uri ": "a URI-typed item",
}


# VisualSampleEntry, ISO/IEC 14496-12. Offsets from the first payload byte:
#
#   +0   6 reserved, 2 data_reference_index      (the SampleEntry base)
#   +8   2 pre_defined, 2 reserved
#   +12  12 pre_defined[3]        QuickTime puts vendor + quality here
#   +24  2 width, 2 height
#   +28  4 horizresolution, 4 vertresolution
#   +36  4 reserved, 2 frame_count
#   +42  32 compressorname        a Pascal string: one length byte, then text
#   +74  2 depth, 2 pre_defined
#   +78  optional child boxes start here
#
# Only the first four bytes of the +12 block are zeroed. In ISO they are
# pre_defined and required to be zero already; in QuickTime they are the
# four-character vendor code. The remaining eight are QuickTime's temporal and
# spatial quality, which carry nothing and are left alone rather than widening
# the edit for no gain.
VISUAL_HANDLER = b"vide"
VISUAL_SAMPLE_ENTRY_LEN = 78
SAMPLE_ENTRY_VENDOR = (12, 4)
SAMPLE_ENTRY_COMPRESSORNAME = (42, 32)


class _Edit:
    """One length-preserving change, recorded before anything is written."""

    __slots__ = ("offset", "length", "note")

    def __init__(self, offset: int, length: int, note: str) -> None:
        self.offset = offset
        self.length = length
        self.note = note


def _ranges(boxes: Sequence[Box]) -> List[Tuple[int, int]]:
    return [(box.offset, box.offset + box.size) for box in boxes]


def _inside(offset: int, ranges: Sequence[Tuple[int, int]]) -> bool:
    return any(start <= offset < end for start, end in ranges)


def _free_fill(out: bytearray, box: Box) -> None:
    """
    Overwrite one box with a zero-filled `free` box of byte-identical size.

    The size FIELD is left in whatever shape it already had, so a 64-bit box
    stays 64-bit and the header length does not change. A `uuid` box's 16-byte
    extended type stops being a header and becomes payload, so it is zeroed
    with everything else.
    """
    if box.size_field == 64:
        header = isobmff.LARGE_HEADER_LEN
    elif box.size_field == 0:
        # A size of 0 means "runs to the end of the file". A `free` box has to
        # declare its length, so an explicit size is written in its place.
        if box.size > 0xFFFFFFFF:
            raise EngineError(
                "box '%s' at offset %d uses the run-to-end size form and is "
                "%d bytes, too large to restate in a 32-bit size field"
                % (box.name, box.offset, box.size)
            )
        struct.pack_into(">I", out, box.offset, box.size)
        header = isobmff.HEADER_LEN
    else:
        header = isobmff.HEADER_LEN
    out[box.offset + 4:box.offset + 8] = FREE
    out[box.offset + header:box.offset + box.size] = b"\x00" * (box.size - header)


def _collect_removals(boxes: Sequence[Box], structural: Set[int],
                      acc: List[Box]) -> None:
    """
    Every box to be replaced by a zero-filled `free` box.

    A matched box's children are not visited: the whole subtree goes, and
    listing its insides would report the same bytes twice.
    """
    for box in boxes:
        if box.type in REMOVED_TYPES:
            acc.append(box)
            continue
        if box.type == isobmff.META and box.offset not in structural:
            acc.append(box)
            continue
        _collect_removals(box.children, structural, acc)


def _describe_removal(box: Box) -> str:
    if box.type == b"uuid":
        which = " (the XMP uuid)" if box.uuid == isobmff.XMP_UUID else ""
        return "uuid box {%s}%s at offset %d, %d bytes" % (
            box.uuid.hex() if box.uuid else "?", which, box.offset, box.size)
    return "%s box at offset %d, %d bytes" % (box.name, box.offset, box.size)


def _mdat_and_idat_payloads(boxes: Sequence[Box]) -> List[Tuple[int, int]]:
    """Where HEIF item data is allowed to live: mdat and idat payloads."""
    out = []
    for box in isobmff.iter_boxes(list(boxes)):
        if box.type in (b"mdat", b"idat"):
            out.append((box.payload_offset, box.payload_offset + box.payload_len))
    return out


def _child(box: Box, kind: bytes) -> Optional[Box]:
    for candidate in box.children:
        if candidate.type == kind:
            return candidate
    return None


def _handler_type(mdia: Box, data: bytes) -> Optional[bytes]:
    """The four-character handler_type of a track, from mdia/hdlr."""
    hdlr = _child(mdia, b"hdlr")
    if hdlr is None or hdlr.payload_len < 12:
        return None
    return bytes(data[hdlr.payload_offset + 8:hdlr.payload_offset + 12])


def visual_sample_entries(data: bytes, boxes: Sequence[Box]) -> List[Box]:
    """
    Every sample entry that is a VisualSampleEntry, by the route through hdlr.

    Gated on the TRACK's handler rather than on the entry's four-character
    format code. A codec nobody here has met still sits in a video track, and
    the fixed prefix is a property of the entry CLASS rather than of the codec.
    """
    out: List[Box] = []
    for trak in isobmff.find_all(list(boxes), b"trak"):
        mdia = _child(trak, b"mdia")
        if mdia is None or _handler_type(mdia, data) != VISUAL_HANDLER:
            continue
        minf = _child(mdia, b"minf")
        stbl = _child(minf, b"stbl") if minf else None
        stsd = _child(stbl, b"stsd") if stbl else None
        if stsd is None:
            continue
        for entry in stsd.children:
            if entry.payload_len >= VISUAL_SAMPLE_ENTRY_LEN:
                out.append(entry)
    return out


def _zero_heif_items(data: bytes, boxes: Sequence[Box], metas: Sequence[Box],
                     edits: List[_Edit]) -> None:
    holders = _mdat_and_idat_payloads(boxes)
    for meta in metas:
        items, primary = isobmff.item_table(data, meta)
        for item_id in sorted(items):
            item = items[item_id]
            if item.item_type in IMAGE_ITEM_TYPES:
                continue
            if primary is not None and item_id == primary:
                # The primary item is the picture whatever its type says. An
                # item type this engine has not met is not a reason to destroy
                # the image the file exists to carry.
                continue
            label = KNOWN_METADATA_ITEM_TYPES.get(
                item.item_type, "an item of unrecognised type '%s'" % item.type_name)
            for start, length in item.extents:
                if length == 0:
                    continue
                if not any(low <= start and start + length <= high
                           for low, high in holders):
                    raise EngineError(
                        "HEIF item %d ('%s') claims bytes %d..%d, which are "
                        "not inside any mdat or idat payload; this engine will "
                        "not zero bytes an item table points at arbitrarily"
                        % (item_id, item.type_name, start, start + length)
                    )
                edits.append(_Edit(
                    start, length,
                    "HEIF item %d, %s (%d bytes at offset %d)"
                    % (item_id, label, length, start)))
            # Stop declaring the item as carrying content. Without this the
            # zeroed bytes are still announced as an application/rdf+xml
            # packet, and exiftool answers "Invalid XMP", which trap 12 makes
            # this project treat as an UNPARSED read: an output that cannot be
            # parsed is never evidence that it is clean. Zeroing the length
            # field is length-preserving and is exactly the shape exiftool's
            # own `-all=` leaves behind, an item whose offset survives with
            # length 0.
            for field_offset, width in item.length_fields:
                if any(data[field_offset:field_offset + width]):
                    edits.append(_Edit(
                        field_offset, width,
                        "the iloc length field for item %d at offset %d"
                        % (item_id, field_offset)))


def scrub_bytes(data: bytes) -> Tuple[bytes, List[str]]:
    """
    The whole removal, as a pure function over bytes.

    Split out from strip_all so the decision logic can be measured without a
    filesystem, and so a caller on a phone can use it directly. The output is
    always exactly as long as the input.
    """
    try:
        boxes = isobmff.parse(data)
    except IsobmffError as exc:
        raise EngineError(
            "this file is not a box tree this engine can fully account for, "
            "so it will not be rewritten: %s" % exc
        ) from exc

    try:
        metas = isobmff.item_metas(boxes)
        structural = {meta.offset for meta in metas}

        removals: List[Box] = []
        _collect_removals(boxes, structural, removals)
        removed_ranges = _ranges(removals)

        out = bytearray(data)
        targeted: List[str] = []

        for box in removals:
            _free_fill(out, box)
            targeted.append(_describe_removal(box))

        edits: List[_Edit] = []

        # colr, only where it carries an ICC profile. An 'nclx' colr is three
        # enumerations and a range flag: it cannot hold a string, and removing
        # it would change how the image renders for no privacy gain.
        for box in isobmff.iter_boxes(boxes):
            if box.type != b"colr" or _inside(box.offset, removed_ranges):
                continue
            kind = isobmff.colr_type(data, box)
            if kind in isobmff.ICC_COLOUR_TYPES:
                _free_fill(out, box)
                targeted.append(
                    "colr box carrying an ICC profile ('%s') at offset %d, "
                    "%d bytes" % (isobmff._safe(kind), box.offset, box.size))

        if metas:
            _zero_heif_items(data, boxes, metas, edits)

        # free and skip: contents zeroed, boxes left where they are.
        for box in isobmff.iter_boxes(boxes):
            if box.type not in FREE_SPACE_TYPES:
                continue
            if _inside(box.offset, removed_ranges) or box.payload_len == 0:
                continue
            if not any(data[box.payload_offset:box.payload_offset + box.payload_len]):
                continue
            edits.append(_Edit(
                box.payload_offset, box.payload_len,
                "%d non-zero bytes inside a %s box at offset %d"
                % (box.payload_len, box.name, box.offset)))

        # VisualSampleEntry vendor and compressorname.
        for entry in visual_sample_entries(data, boxes):
            if _inside(entry.offset, removed_ranges):
                continue
            for (relative, width), label in (
                    (SAMPLE_ENTRY_VENDOR, "vendor code"),
                    (SAMPLE_ENTRY_COMPRESSORNAME, "compressorname")):
                start = entry.payload_offset + relative
                if not any(data[start:start + width]):
                    continue
                text = bytes(data[start:start + width])
                edits.append(_Edit(
                    start, width,
                    "%s %r in the %s sample entry at offset %d"
                    % (label,
                       text.strip(b"\x00").decode("utf-8", "replace")[:40],
                       entry.name, entry.offset)))

        # hdlr names.
        for box in isobmff.iter_boxes(boxes):
            if box.type != b"hdlr" or _inside(box.offset, removed_ranges):
                continue
            span = isobmff.hdlr_name_range(box)
            if span is None:
                continue
            start, length = span
            if not any(data[start:start + length]):
                continue
            edits.append(_Edit(
                start, length,
                "hdlr name %r at offset %d"
                % (bytes(data[start:start + length]).split(b"\x00")[0]
                   .decode("utf-8", "replace"), start)))

        for edit in edits:
            out[edit.offset:edit.offset + edit.length] = b"\x00" * edit.length
            targeted.append(edit.note)

        # Times last, so a time inside a box that was removed is never reported
        # as a separate edit.
        for box in isobmff.iter_boxes(boxes):
            if box.type not in isobmff.TIME_BOXES:
                continue
            if _inside(box.offset, removed_ranges):
                continue
            fields = isobmff.time_fields(data, box)
            wrote = False
            for offset, width in fields:
                if any(data[offset:offset + width]):
                    wrote = True
                out[offset:offset + width] = b"\x00" * width
            if wrote:
                targeted.append(
                    "creation_time and modification_time in the %s box at "
                    "offset %d" % (box.name, box.offset))
    except IsobmffError as exc:
        raise EngineError(
            "this file's box tree could not be read far enough to clean it "
            "safely: %s" % exc
        ) from exc

    rebuilt = bytes(out)
    try:
        _assert_clean(data, rebuilt)
    except IsobmffError as exc:
        # The post-condition check reads the output back, so anything it finds
        # unreadable has to leave by the same door every other refusal does.
        # An IsobmffError escaping strip_all would be a ValueError the scrubber
        # does not catch, and a refusal nobody catches is a crash rather than a
        # fail-closed.
        raise EngineError(
            "the rewritten box tree could not be checked: %s" % exc) from exc
    return rebuilt, targeted


def _assert_clean(original: bytes, rebuilt: bytes) -> None:
    """
    Post-conditions, checked before anything reaches the filesystem.

    This is a guard and NOT the verification. It re-reads the output with the
    same walker that wrote it, which is exactly the circular check traps 2 and
    12 forbid as evidence. The evidence is in tests/test_isobmff_engine.py: an
    independent walker, a full decode, a framemd5 comparison and the exiftool
    oracle. What this does buy is fail-closed behaviour, so a bug leaves the
    user's file alone instead of replacing it with something broken.
    """
    if len(rebuilt) != len(original):
        raise EngineError(
            "the rewrite changed the file length from %d to %d bytes; every "
            "sample offset in the file would now be wrong"
            % (len(original), len(rebuilt))
        )
    try:
        boxes = isobmff.parse(rebuilt)
    except IsobmffError as exc:
        raise EngineError("the rewritten box tree does not parse: %s" % exc) from exc

    structural = {meta.offset for meta in isobmff.item_metas(boxes)}
    for box in isobmff.iter_boxes(boxes):
        if box.type in REMOVED_TYPES:
            raise EngineError(
                "a %s box survived the rewrite at offset %d" % (box.name, box.offset))
        if box.type == isobmff.META and box.offset not in structural:
            raise EngineError(
                "a non-structural meta box survived the rewrite at offset %d"
                % box.offset)
        if box.type in FREE_SPACE_TYPES and any(
                rebuilt[box.payload_offset:box.payload_offset + box.payload_len]):
            raise EngineError(
                "a %s box at offset %d still holds non-zero bytes"
                % (box.name, box.offset))
        if box.type in isobmff.TIME_BOXES:
            for offset, width in isobmff.time_fields(rebuilt, box):
                if any(rebuilt[offset:offset + width]):
                    raise EngineError(
                        "a time field at offset %d in the %s box is not zero"
                        % (offset, box.name))


class IsobmffEngine(BaseEngine):
    name = "isobmff"
    supports_selective = False

    def available(self) -> Tuple[bool, str]:
        return True, ""

    def strip_all(self, path: str) -> List[str]:
        self.require()
        try:
            with open(path, "rb") as handle:
                data = handle.read()
        except OSError as exc:
            raise EngineError(f"could not read ISO base media file: {exc}") from exc

        # Everything that can refuse the file refuses it here, before a
        # temporary file exists. A refusal must leave the directory exactly as
        # it found it.
        rebuilt, targeted = scrub_bytes(data)

        tmp = temp_beside(path, ".isobmff")
        try:
            with open(tmp, "wb") as handle:
                handle.write(rebuilt)
        except OSError as exc:
            try:
                os.unlink(tmp)
            except OSError:
                pass
            raise EngineError(
                f"could not write the rewritten container: {exc}") from exc
        atomic_replace(tmp, path)
        return targeted or ["iso base media container rewrite"]
