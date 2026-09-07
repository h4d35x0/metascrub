"""
ISO base media engine: pure-Python box surgery for MP4, MOV, HEIC, AVIF, 3GP.
No ffmpeg, no exiftool, because neither can run on a phone.

Phase 2 of the Android media build. docs/ANDROID-MEDIA-BUILD.md section 2.4 is
the removal table, 2.5 is the offset strategy, 3.2 is the structural assertion
the tests make over the output. The container rules live next door in
`metascrub/isobmff.py`; this file is only the policy.

WIRED INTO CAPABILITIES FOR STILLS ONLY. The .heic, .heif and .avif rows route
here; the .mp4, .mov and the rest of the AV family still route to the ffmpeg av
engine. Flipping those is a separate decision with its own evidence, and
`tests/test_isobmff_engine.py` asserts the AV rows are unchanged so that flip
cannot happen by accident. The stills flip has its own evidence and its own
file, `tests/test_heic_routing.py`: exiftool cannot remove a HEIF ICC profile,
because in HEIF the ICC sits in iprp/ipco/colr as an item PROPERTY rather than
as a metadata item, so the shipped exiftool routing left the Apple Display P3
strings in the output bytes of every real iPhone HEIC and returned an error.

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

  sefd         Samsung Extended Format Data, a top-level box on Galaxy stills.
               Measured 2026-09-07 on a real Galaxy S10+ HEIC: 106 bytes
               holding `Image_UTC_Data` with a unix-millisecond capture time
               and `MCC_Data` with a mobile country code. exiftool renders them
               as Samsung:TimeStamp, a wall clock time carrying the phone's
               local UTC offset, and Samsung:MCCData, a country. The residual
               scan cannot catch this one, which is trap 10 again: the date is
               dropped by verify.py's all-digits rule and the country comes
               back as an int, so neither ever becomes a needle.

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

  free, skip,  their CONTENTS are zeroed rather than the boxes being removed.
  wide         They can hold orphaned data from a previous edit, and they
               cannot move without moving everything after them. `wide` is
               QuickTime's 8-byte reserved-space placeholder and is header-only
               in every file measured here, so its zeroing is a no-op today and
               is listed so a `wide` with a payload is handled rather than met
               for the first time in the field.

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
  - Any video bitstream that is not H.264 or H.265. The SEI user-data NAL
    those two write is removed, in `mdat` AND in the `avcC`/`hvcC`
    configuration record; see PHASE 3, PART ONE below. AV1 and VP9 carry the
    same class of value in structures this engine does not parse, and a video
    track whose sample entry holds neither `avcC` nor `hvcC` is left alone.
  - The bitstream of a HEIF or AVIF still-image ITEM, as opposed to a track.
    Same gap, stated separately because it is reached by a different route.
  - `mdhd` language. Left as it is: a language code is a weak signal and
    rewriting it changes track selection semantics.
  - Item table tidiness. A zeroed Exif item stays DECLARED in `iinf`, pointing
    at zeros. exiftool's own `-all=` leaves exactly the same dangling
    declaration with length 0, so this matches the reference implementation.
  - A vendor box type nobody has met. This engine does not refuse a box it has
    no rule for, because most of them are structural and refusing would break
    ordinary files. What it does instead is name every type it HAS met in
    DECIDED_TYPES below, so an unlisted type at the top level or under `moov`
    fails the inventory tests the moment a file carrying one enters the corpus.
    That is a build failure and not a runtime refusal, which is a real limit
    and is stated here rather than implied: a Galaxy box type that no corpus
    file carries is still a box this engine will silently keep.
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
#
# `sefd` is Samsung Extended Format Data. It is a top-level box on Galaxy
# stills and it is definitionally metadata: measured 2026-09-07 on a real
# Galaxy S10+ HEIC, 106 bytes holding the ASCII keys `Image_UTC_Data` with a
# unix-millisecond capture time and `MCC_Data` with a mobile country code,
# which exiftool renders as Samsung:TimeStamp (a wall clock time carrying the
# local UTC offset) and Samsung:MCCData (a country). The old exiftool routing
# removed it and this engine did not, so routing .heic here without this entry
# traded a hard failure on Apple files for a SILENT one on Samsung files.
# See tests/test_heic_routing.py::test_the_samsung_sefd_box_is_removed.
REMOVED_TYPES = frozenset({b"udta", b"uuid", b"ilst", b"sefd"})

# Free-space boxes: kept in place, contents zeroed.
#
# `wide` is QuickTime's reserved-space placeholder, the 8 bytes that let a
# writer promote an `mdat` to a 64-bit header without moving anything. It is
# padding by definition, so anything found in its payload is orphaned data of
# exactly the kind the `free` rule exists for. Every `wide` measured here is
# header-only and the zeroing is a no-op on it; it is listed so that a `wide`
# with a payload is handled rather than met for the first time in the field.
FREE_SPACE_TYPES = frozenset({b"free", b"skip", b"wide"})

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


# ------------------------------------------------------- the decided box types
#
# WHY THIS TABLE EXISTS
#
# `sefd` was not a bug in a removal rule. It was a box this engine had never
# formed an opinion about, sitting at the TOP LEVEL of a mass-market phone's
# stills, and nothing anywhere failed when it survived: the file decoded, the
# structural post-conditions held, and the residual scan could not see either
# value it carried (one is rendered by exiftool as a formatted date and dropped
# by verify.py's all-digits rule, the other comes back as an int and never
# becomes a needle at all). A silent pass is the failure mode this project
# exists to prevent, so the gap itself has to be what fails, not the one name
# that happened to be found.
#
# So: every box type met at the top level or directly under `moov`, across the
# whole real-device corpus and every synthetic fixture, is listed here with the
# decision it carries. A type that is NOT listed is not "probably harmless", it
# is UNDECIDED, and the inventory tests fail on it. That converts the next
# vendor box from a leak nobody notices into a red test on the day a file
# carrying it is first added to the corpus.
#
# This table is documentation plus a test oracle. It does not drive removal:
# REMOVED_TYPES and FREE_SPACE_TYPES above do that, and duplicating the policy
# into a second structure would let the two disagree. The inventory test
# asserts they agree.
DECISION_STRUCTURAL = "structural"      # kept whole; the file is this box
DECISION_REMOVED = "removed"            # free-filled as a whole subtree
DECISION_ZEROED = "zeroed"              # box kept, payload zero-filled
DECISION_FIELDS = "fields"              # box kept, named fields overwritten
DECISION_DESCENDED = "descended"        # a container; its children are decided

# MEASURED 2026-09-07: the union of every box type seen at the top level or
# directly under `moov`, across the real-device corpus and all eleven synthetic
# fixtures, is exactly:
#
#   free ftyp mdat meta mfra moof moov mvex mvhd sefd trak udta uuid wide
#
# The corpus holds nine files with an ISO base media extension. Eight of them
# parse and are counted above; apple-livephoto-quicktime.mov is refused by
# isobmff.parse because it carries no `ftyp` box at all, which is the
# documented fail-closed behaviour, and a file this engine will not touch
# cannot leak through it.
#
# Every one of those is in the first group below. The second group is boxes
# this engine has NOT met at the surface here and has an opinion about from the
# specification alone. That split is written down rather than glossed, because
# an unmeasured name in a table is the same thing as an unmeasured name on an
# allowlist: nobody checks it again. A second-group entry that later turns up
# in a real file deserves a look at its payload before it is trusted.
DECIDED_TYPES: dict = {
    # ---- MEASURED at the surface in this corpus ----------------------------
    b"ftyp": DECISION_STRUCTURAL,   # brand; deliberately preserved, see above
    b"mdat": DECISION_STRUCTURAL,   # the picture and the samples themselves
    b"moov": DECISION_DESCENDED,
    b"trak": DECISION_DESCENDED,
    b"mvex": DECISION_DESCENDED,    # fragment defaults
    b"moof": DECISION_DESCENDED,    # fragment headers
    b"mfra": DECISION_DESCENDED,    # random access tables
    b"mvhd": DECISION_FIELDS,       # creation_time, modification_time zeroed
    b"meta": DECISION_FIELDS,       # structural in HEIF/AVIF: items zeroed via
                                    # iloc. A `meta` that is a tag dictionary is
                                    # REMOVED instead, told apart by content in
                                    # isobmff.item_metas, never by name.
    b"udta": DECISION_REMOVED,
    b"uuid": DECISION_REMOVED,
    b"sefd": DECISION_REMOVED,
    b"free": DECISION_ZEROED,
    b"wide": DECISION_ZEROED,

    # ---- decided from the specification, NOT met at the surface here -------
    b"ilst": DECISION_REMOVED,      # met, but only under moov/meta
    b"idat": DECISION_STRUCTURAL,   # met, but only under meta; item data,
                                    # reached through iloc rather than directly
    b"skip": DECISION_ZEROED,       # `free` under its other registered name
    b"styp": DECISION_STRUCTURAL,   # segment type of a fragmented stream
    b"sidx": DECISION_STRUCTURAL,   # segment index: byte ranges and durations
    b"ssix": DECISION_STRUCTURAL,   # sub-segment index, same
    b"pdin": DECISION_STRUCTURAL,   # progressive download rate hints
    b"iods": DECISION_STRUCTURAL,   # MPEG-4 initial object descriptor
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
    """
    One length-preserving change, recorded before anything is written.

    `replacement` is None for the common case, which zero-fills. Phase 3 needs
    two edits that are NOT zeros: an SEI NAL rewritten to filler (zeros there
    would be an emulation prevention violation, see isobmff.sei_filler_bytes)
    and a tkhd flags word with three bits cleared and the rest kept. Both must
    still be exactly `length` bytes, which is asserted at write time rather
    than trusted.

    `note` may be empty, which applies the edit and reports nothing. Zeroing a
    PCM audio track means zeroing tens of thousands of samples, and a report
    line per sample is a report nobody reads.
    """

    __slots__ = ("offset", "length", "note", "replacement")

    def __init__(self, offset: int, length: int, note: str,
                 replacement: Optional[bytes] = None) -> None:
        self.offset = offset
        self.length = length
        self.note = note
        self.replacement = replacement


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


# ===========================================================================
# PHASE 3, PART ONE: THE ENCODER SIGNATURE IN THE VIDEO BITSTREAM
# ===========================================================================
#
# x264 and x265 sign their work. Both write an SEI user_data_unregistered NAL
# holding the encoder version and the FULL settings string: every rate control
# parameter, every tool that was enabled, the build date, the compiler. It is a
# fingerprint of the software AND of how it was invoked, and on a phone it
# names the phone's encoder.
#
# MEASURED 2026-09-06 on this machine, and the two measurements disagree with
# each other in a way that matters:
#
#   ffmpeg + libx264 -> mp4    the SEI NAL is in `mdat`, 686 bytes, the first
#                              NAL of the first video sample, carrying
#                              'x264 - core 164 r3198 ... - options: ...'
#   ffmpeg + libx265 -> mp4    the SEI NAL is NOT in mdat at all. It is in the
#                              `hvcC` configuration record inside the `hvc1`
#                              sample entry, 2334 bytes, carrying
#                              'x265 (build 215) - 4.1+54-fa2770934: ...'
#                              The same file muxed with `-tag:v hev1` puts it
#                              in exactly the same place.
#
# So an implementation that scans only mdat removes it from H.264 and misses it
# completely in H.265, and an implementation that scans only the configuration
# record does the reverse. Both are scanned. This is CLAUDE.md trap 8 again:
# behaviour keyed on where a thing "obviously" lives, where the two codecs put
# it in different places.
#
# HOW IT IS REMOVED WITHOUT RE-ENCODING AND WITHOUT MOVING A BYTE
#
# Deleting the NAL is not available. Its bytes are inside a sample whose size
# is in `stsz` and whose chunk offset is in `stco`, so removing them changes a
# sample size and every offset after it. Measured by the Phase 2 team: moving a
# pre-mdat box without fixing offsets produced "Invalid NAL unit size" on
# essentially every frame and ffmpeg exit 69.
#
# So the NAL is overwritten IN PLACE at identical length with SEI
# filler_payload messages, which is payload type 3 in both H.264 and H.265
# Annex D and whose payload is defined as a run of 0xFF. The NAL header is
# kept, so an SEI NAL stays an SEI NAL and its position in the access unit
# stays legal; only its contents change. `isobmff.sei_filler_bytes` builds the
# replacement and carries the proof that it can never need an emulation
# prevention byte.
#
# WHAT THIS COSTS, STATED RATHER THAN HIDDEN
#
# The rewrite replaces EVERY message in an SEI NAL that carries a user-data
# message, not only the user-data one. That is a deliberate trade and the
# reason is boundary safety: rewriting one message in the middle of a NAL means
# writing bytes whose escaping context is set by the bytes on either side of
# them, and a 0x03 written immediately after two zero bytes silently becomes an
# emulation prevention byte and shifts everything after it. Rewriting from the
# first byte after the NAL header to the end of the NAL has no such neighbour:
# the byte before it is the NAL header and the byte after it is the end of the
# NAL, and escaping never spans a NAL boundary in either form.
#
# The consequence is that a pic_timing or buffering_period SEI sharing a NAL
# with the encoder signature becomes filler too. That cannot change decoded
# pixels, and the acceptance test is a full decode with a framemd5 comparison
# against the input, so the claim is measured rather than argued.
#
# WHAT IS NOT REACHED
#
#   - Codecs that are not H.264 or H.265. AV1 and VP9 have no SEI; their
#     equivalents are OBU metadata and are not implemented. A video track whose
#     sample entry holds neither `avcC` nor `hvcC` is left alone, and this is a
#     gap rather than a claim.
#   - HEIF and AVIF still-image ITEMS. A `hvc1` item is an HEVC bitstream and
#     can in principle carry the same SEI, but its configuration lives in
#     `ipco` rather than in a sample entry and its data is located by `iloc`
#     rather than by a sample table. Measured on the HEIC fixture available
#     here: it carries no x265 or x264 signature at all, so there is nothing to
#     test against, and an untested path is not shipped.
#   - `stz2`, the compact sample size table. `isobmff` refuses it rather than
#     guessing at a field width, so a file using one is refused whole.

_CONFIG_IS_HEVC = {isobmff.AVCC: False, isobmff.HVCC: True}

# The SEI payload of a user_data_unregistered message opens with a 16-byte
# UUID; the human-readable part is what follows it.
SEI_UUID_LEN = 16


def _video_configs(data: bytes, track: "isobmff.Track"
                   ) -> List[Tuple[Box, bool, int, List[Tuple[int, int]]]]:
    """
    [(config box, is HEVC, NAL length size, NALs the record itself stores)].

    Reached through the sample entries of one track, and only for entries long
    enough to be a VisualSampleEntry. The NAL length size is READ from the
    record: 1, 2 and 4 are all legal, and assuming 4 reads a sample as garbage
    from its first byte.
    """
    out: List[Tuple[Box, bool, int, List[Tuple[int, int]]]] = []
    for entry in track.sample_entries:
        if entry.payload_len < VISUAL_SAMPLE_ENTRY_LEN:
            continue
        for child in isobmff.visual_sample_entry_children(data, entry):
            hevc = _CONFIG_IS_HEVC.get(child.type)
            if hevc is None:
                continue
            if hevc:
                length_size, nals = isobmff.hevc_config(data, child)
            else:
                length_size, nals = isobmff.avc_config(data, child)
            out.append((child, hevc, length_size, nals))
    return out


def _user_data_preview(data: bytes, offset: int, length: int,
                       header: int) -> str:
    """The printable tail of an SEI user-data payload, for the report line."""
    rbsp = isobmff.unescape_rbsp(data, offset + header, offset + length)
    for payload_type, size, at in isobmff.sei_messages(rbsp):
        if payload_type != isobmff.SEI_USER_DATA_UNREGISTERED:
            continue
        body = rbsp[at + SEI_UUID_LEN:at + size]
        text = "".join(chr(b) for b in body if 32 <= b < 127)
        return text.strip()[:60]
    return ""


def _sei_edits(data: bytes, tracks: Sequence["isobmff.Track"],
               removed_ranges: Sequence[Tuple[int, int]],
               edits: List[_Edit]) -> None:
    """Rewrite every SEI NAL that carries an encoder signature, in place."""
    for track in tracks:
        if track.handler != VISUAL_HANDLER:
            continue
        configs = _video_configs(data, track)
        if not configs:
            continue
        shapes = {(hevc, length_size) for _box, hevc, length_size, _n in configs}
        if len(shapes) > 1:
            raise EngineError(
                "video track %d has sample entries whose configuration records "
                "disagree about the codec or the NAL length field size %r; "
                "this engine will not guess which one a given sample follows"
                % (track.track_id, sorted(shapes)))
        hevc, length_size = shapes.pop()

        found: List[Tuple[int, int, int, str]] = []

        # 1. The NAL units the configuration record itself stores. This is
        #    where x265 puts its signature, and it is inside `moov`.
        for box, box_hevc, _size, nals in configs:
            if _inside(box.offset, removed_ranges):
                continue
            for offset, length, header in isobmff.sei_user_data_nals(
                    data, nals, box_hevc):
                found.append((offset, length, header,
                              "the %s configuration record at offset %d"
                              % (box.name, box.offset)))

        # 2. The NAL units in the track's samples, located through stsz, stsc
        #    and stco/co64, or through trun for a fragmented file. NOT by
        #    walking mdat: an interleaved audio frame read as a NAL length
        #    lands wherever it says.
        for sample_offset, sample_size in track.samples:
            if sample_size == 0:
                continue
            nals = isobmff.length_prefixed_nals(
                data, sample_offset, sample_offset + sample_size, length_size)
            for offset, length, header in isobmff.sei_user_data_nals(
                    data, nals, hevc):
                found.append((offset, length, header,
                              "the sample at offset %d" % sample_offset))

        for offset, length, header, where in found:
            preview = _user_data_preview(data, offset, length, header)
            try:
                filler = isobmff.sei_filler_bytes(length - header)
            except IsobmffError as exc:
                raise EngineError(
                    "an SEI NAL at offset %d could not be replaced with filler "
                    "of the same length: %s" % (offset, exc)) from exc
            edits.append(_Edit(
                offset + header, length - header,
                "an SEI user-data NAL carrying %s (%d bytes at offset %d, in "
                "%s), overwritten with SEI filler"
                % ("'%s'" % preview if preview else "an encoder signature",
                   length, offset, where),
                replacement=filler))


# ===========================================================================
# PHASE 3, PART TWO: AUDIO TRACK REMOVAL, OPT IN AND OFF BY DEFAULT
# ===========================================================================
#
# WHY. docs/ANDROID-MEDIA-BUILD.md 1.2: "Traffic noise, accents, a television
# in the background, and mains hum. Electrical Network Frequency analysis can
# place and time a recording from the power grid signature in the audio track
# alone."
#
# WHAT THIS DOES, EXACTLY, AND WHAT IT DOES NOT
#
# The track is NOT deleted, and it cannot be. Deleting a `trak` moves every box
# after it, and deleting its samples from `mdat` moves every sample offset in
# the file. This engine's one invariant is that no length ever changes. So:
#
#   1. Every byte of every audio sample in `mdat` is overwritten with zero.
#      This is the part that actually does the privacy work: the recording is
#      gone from the file, not merely unreferenced, so carving `mdat` recovers
#      nothing and there is no ENF signature left to analyse.
#   2. The sample SIZES in the track's `stsz` are set to zero, so a decoder is
#      never handed a zero-filled frame to fail on.
#   3. The `track_enabled`, `track_in_movie` and `track_in_preview` flags in
#      the track's `tkhd` are cleared, so a player that honours them does not
#      select the track at all.
#
# The track STILL EXISTS STRUCTURALLY. `trak`, `tkhd`, `mdia`, `stsd` and the
# `mp4a` sample entry are all still there, and exiftool still reports
# AudioFormat, AudioChannels, AudioBitsPerSample and AudioSampleRate. A viewer
# can still see that the file HAD an audio track, how many channels it had and
# at what rate it was sampled. What they cannot get is one sample of it. If
# hiding the fact that audio was ever present is the requirement, this feature
# does not meet it and compaction is the only thing that would.
#
# MEASURED 2026-09-06, and step 2 is not optional. Four combinations were
# tried on the same MP4 (H.264 plus AAC) and the same QuickTime .mov:
#
#   zero the samples only                 ffmpeg exit 69, 'channel element
#                                         0.0 is not allocated' on every frame
#   zero the samples, clear tkhd flags    ffmpeg exit 69, identical errors.
#                                         ffmpeg does not honour the flag.
#   zero the samples, zero stco's count   exit 0 but 'stream 1, missing
#                                         mandatory atoms, broken header'
#   zero the samples, zero the stsz sizes exit 0, no error on either container,
#                                         the video framemd5 bit identical and
#                                         the audio decoding to nothing at all
#                                         (md5 d41d8cd9..., the empty digest)
#
# A PCM track measured separately, because its `stsz` uses the FIXED form and
# has no per-sample table to zero: sample_size and sample_count are both set to
# zero instead, which keeps the box self-consistent at 12 payload bytes.
# ffmpeg reconstructs PCM sample counts from the chunk sizes rather than from
# stsz, so that track still decodes; it decodes to
# md5 e9ded829730eccd2d0273d7cc06be58c, which is byte for byte the digest of
# one second of `anullsrc` silence. Zeroed PCM is silence, which is the correct
# outcome and is stated because it differs from the AAC case.
#
# WHAT IS NOT REACHED
#
#   - Fragmented files. `stsz` holds nothing there and the sizes live in each
#     `trun`. The samples are still zeroed, because `isobmff.tracks` resolves
#     fragment samples too, but nothing neutralises the sizes, so a decoder
#     would be handed zero-filled frames. Rather than ship that, a fragmented
#     file with an audio track REFUSES when audio removal is asked for.
#   - Timed metadata and subtitle tracks. Only handler 'soun' is touched.


AUDIO_HANDLER = b"soun"
AUDIO_DISABLED_FLAGS = (isobmff.TRACK_ENABLED | isobmff.TRACK_IN_MOVIE
                        | isobmff.TRACK_IN_PREVIEW)


def _merge_ranges(ranges: Sequence[Tuple[int, int]]) -> List[Tuple[int, int]]:
    """Sorted, coalesced (offset, length) runs, so 44100 PCM samples are one."""
    ordered = sorted(ranges)
    out: List[Tuple[int, int]] = []
    for offset, length in ordered:
        if length <= 0:
            continue
        if out and offset <= out[-1][0] + out[-1][1]:
            start, span = out[-1]
            out[-1] = (start, max(span, offset + length - start))
            continue
        out.append((offset, length))
    return out


def _audio_edits(data: bytes, boxes: Sequence[Box],
                 tracks: Sequence["isobmff.Track"],
                 edits: List[_Edit]) -> None:
    """Zero every audio sample and neutralise the tracks that held them."""
    fragmented = bool(isobmff.find_all(list(boxes), b"moof"))
    for track in tracks:
        if track.handler != AUDIO_HANDLER:
            continue
        if fragmented:
            raise EngineError(
                "track %d is an audio track in a fragmented file. Its sample "
                "sizes live in each trun rather than in stsz, and zeroing the "
                "samples without neutralising the sizes hands a decoder "
                "zero-filled frames. This engine will not half-remove an "
                "audio track" % track.track_id)

        # Counted over the runs that will ACTUALLY be written, not over every
        # run the track has. A note that says 8991 bytes when 8991 bytes were
        # already zero is a fabricated number, and the rule about those is that
        # they are never one error.
        pending = [(offset, length) for offset, length
                   in _merge_ranges(track.samples)
                   if any(data[offset:offset + length])]
        total = sum(length for _offset, length in pending)
        for position, (offset, length) in enumerate(pending):
            edits.append(_Edit(
                offset, length,
                "the audio samples of track %d, %d bytes over %d run(s) in "
                "mdat, zeroed" % (track.track_id, total, len(pending))
                if position == 0 else "",
            ))

        stbl = isobmff.stbl_of(track)
        stsz = None
        if stbl is not None:
            for child in stbl.children:
                if child.type == b"stsz":
                    stsz = child
                    break
        if stsz is None or stsz.payload_len < 12:
            raise EngineError(
                "audio track %d has no readable stsz, so its sample sizes "
                "cannot be neutralised and a decoder would be handed "
                "zero-filled frames" % track.track_id)
        fixed = struct.unpack_from(">I", data, stsz.payload_offset + 4)[0]
        count = struct.unpack_from(">I", data, stsz.payload_offset + 8)[0]
        if fixed:
            # The fixed form has no per-sample table. sample_size and
            # sample_count both go to zero, which leaves a self-consistent box
            # declaring no samples and an empty table, at the same 12 bytes.
            span = (stsz.payload_offset + 4, 8)
        else:
            if stsz.payload_len < 12 + 4 * count:
                raise EngineError(
                    "the stsz of audio track %d declares %d samples but holds "
                    "only %d payload bytes; zeroing the table it claims to "
                    "have would write past the box"
                    % (track.track_id, count, stsz.payload_len))
            span = (stsz.payload_offset + 12, 4 * count)
        if span[1] and any(data[span[0]:span[0] + span[1]]):
            edits.append(_Edit(
                span[0], span[1],
                "the stsz sample sizes of audio track %d at offset %d, zeroed "
                "so no decoder is handed a zero-filled frame"
                % (track.track_id, stsz.offset)))

        tkhd = isobmff.tkhd_of(track)
        if tkhd is None:
            raise EngineError(
                "audio track %d has no tkhd to disable" % track.track_id)
        offset, width = isobmff.tkhd_flags_field(tkhd)
        flags = int.from_bytes(data[offset:offset + width], "big")
        if flags & AUDIO_DISABLED_FLAGS:
            edits.append(_Edit(
                offset, width,
                "the track_enabled, track_in_movie and track_in_preview flags "
                "of audio track %d" % track.track_id,
                replacement=(flags & ~AUDIO_DISABLED_FLAGS).to_bytes(width, "big")))


def scrub_bytes(data: bytes, remove_audio: bool = False) -> Tuple[bytes, List[str]]:
    """
    The whole removal, as a pure function over bytes.

    Split out from strip_all so the decision logic can be measured without a
    filesystem, and so a caller on a phone can use it directly. The output is
    always exactly as long as the input.

    `remove_audio` is OFF by default and stays off. Dropping the audio is a
    change to what the file IS rather than to what it says about itself, and a
    tool that silently returns a silent video is a tool nobody can trust with a
    recording. See PHASE 3, PART TWO for what it does and does not achieve.
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

        # The video bitstream, and the audio track if it was asked for. Both
        # need the sample tables, so both go through isobmff.tracks, and an
        # unresolvable sample table raises rather than quietly finding nothing.
        track_list = isobmff.tracks(data, boxes)
        _sei_edits(data, track_list, removed_ranges, edits)
        if remove_audio:
            _audio_edits(data, boxes, track_list, edits)

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
            fill = edit.replacement
            if fill is None:
                fill = b"\x00" * edit.length
            if len(fill) != edit.length:
                raise EngineError(
                    "an edit at offset %d would write %d bytes over %d; this "
                    "engine never changes a length"
                    % (edit.offset, len(fill), edit.length))
            out[edit.offset:edit.offset + edit.length] = fill
            if edit.note:
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
        _assert_clean(data, rebuilt, remove_audio)
    except IsobmffError as exc:
        # The post-condition check reads the output back, so anything it finds
        # unreadable has to leave by the same door every other refusal does.
        # An IsobmffError escaping strip_all would be a ValueError the scrubber
        # does not catch, and a refusal nobody catches is a crash rather than a
        # fail-closed.
        raise EngineError(
            "the rewritten box tree could not be checked: %s" % exc) from exc
    return rebuilt, targeted


def _assert_clean(original: bytes, rebuilt: bytes,
                  remove_audio: bool = False) -> None:
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

    # The bitstream post-conditions. Same circularity caveat as everything
    # above: this re-reads the output with the walker that wrote it, so it buys
    # fail-closed behaviour and not evidence. The evidence is a full decode and
    # a byte search in tests/test_isobmff_engine.py.
    tracks = isobmff.tracks(rebuilt, boxes)
    for track in tracks:
        if track.handler == VISUAL_HANDLER:
            for _box, hevc, length_size, nals in _video_configs(rebuilt, track):
                if isobmff.sei_user_data_nals(rebuilt, nals, hevc):
                    raise EngineError(
                        "an SEI user-data NAL survived in a configuration "
                        "record of track %d" % track.track_id)
                for offset, size in track.samples:
                    if size == 0:
                        continue
                    survivors = isobmff.sei_user_data_nals(
                        rebuilt,
                        isobmff.length_prefixed_nals(
                            rebuilt, offset, offset + size, length_size),
                        hevc)
                    if survivors:
                        raise EngineError(
                            "an SEI user-data NAL survived in the sample at "
                            "offset %d" % offset)
        if remove_audio and track.handler == AUDIO_HANDLER:
            for offset, size in track.samples:
                if size and any(rebuilt[offset:offset + size]):
                    raise EngineError(
                        "audio sample data at offset %d survived the removal"
                        % offset)


class IsobmffEngine(BaseEngine):
    """
    The pure-Python ISO base media engine.

    `remove_audio` is a constructor argument rather than a strip_all argument
    because `BaseEngine.strip_all` takes a path and nothing else, and widening
    that interface would touch every other engine in the tree for the benefit
    of one. The registry in `metascrub/engines/__init__.py` builds this with no
    arguments, so the shipped instance has audio removal OFF, which is the
    documented default and the only behaviour any current caller gets.
    """

    name = "isobmff"
    supports_selective = False

    def __init__(self, remove_audio: bool = False) -> None:
        self.remove_audio = remove_audio

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
        rebuilt, targeted = scrub_bytes(data, self.remove_audio)

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
