"""
Does this OUTPUT file still contain a GPS-bearing structure?

WHY THIS EXISTS, AND WHY IT IS NOT A NEEDLE

`verify.py` proves a removal by capturing the metadata VALUES before scrubbing
and then searching the output bytes for those exact values. For a coordinate
that instrument cannot work, and the reason is not a filter that can be
loosened. Measured 2026-09-06 and recorded in docs/D2-GPS-VERIFICATION.md
section 2.1: EXIF stores a coordinate as three rational pairs
(`43/1 39/1 14517/1250`), so the decimal string exiftool prints is not in the
file at all. A JPEG, a PNG, a WebP and a TIFF each plainly carrying
`43.653226` were searched for eleven candidate string forms of it in three
encodings; the coordinate was found in NONE of them. Three needle policies were
then run over eight GPS-bearing files: the shipped one found the coordinate in
0 of 8, and the maximally loosened one found half of one coordinate in 1 of 8.

So the question is asked structurally instead, of the OUTPUT alone, with no
dependence on a baseline read having produced anything: is there still an EXIF
GPS IFD, an XMP `GPS*` property, or an ISO base media location box in this
file? That is the same argument that produced `structure.py` in 1.0.2, applied
to a carrier rather than to a region.

WHY IT IS NOT IN structure.py

`structure.py` answers "which regions does the format fail to account for", and
`verify.py` maps a non-empty `unaccounted` to `STRUCTURE_UNACCOUNTED`, whose
meaning is "we cannot say what these bytes are". A surviving GPS IFD is the
opposite: we know exactly what it is. Folding the two together would change
what `STRUCTURE_UNACCOUNTED` means for every file that already reports it, and
would make the 1.0.2 CHANGELOG entry wrong retroactively. Hence a sibling
module with its own report type.

THE THING THIS MODULE IS BUILT TO PREVENT

docs/D2-GPS-VERIFICATION.md section 9 names the biggest risk in this work: that
GPS verification ships covering the easy containers, the verdict string does not
change, and `verified clean` on a `.heic` or a `.mkv` goes on meaning exactly
what it meant before while the CHANGELOG says GPS verification was added. Every
structural decision below exists to make that impossible rather than unlikely:

  - `GpsReport` has no boolean that is True for a file nobody looked at.
    `__bool__` RAISES, so `if report:` cannot silently read as "fine".
  - `status` is a five-state enum. NOT_CHECKED, ERROR and INCOMPLETE are all
    distinct from CLEAN, and `is_clean` is True for exactly one of the five.
  - a walker that reaches a region it cannot interrogate records a LIMIT, and a
    report carrying a limit is INCOMPLETE, never CLEAN. "We looked at most of
    it" is not "it is clean" either.
  - `coverage()` answers at RUNTIME which of the tool's own extensions this
    check has an opinion about, so the gap can be printed rather than recalled.

WIRED IN as of 2026-09-07. verify.py calls scan() and reports the result
through Verification.coverage, which is additive to the JSON report: no
existing key changed name, type or meaning. The check does NOT change any
verdict. A GPS carrier found in an output is reported in
coverage.gps_findings and printed in red by the CLI, and whether that
should fail a run is a separate, breaking decision that has not been made.

The reason this module does not share a parser with the engines it checks
still stands and still matters: a checker sharing a parser with the remover
it checks cannot catch a parser bug.

`verify.py` does not import this module and must not until the wiring is
reviewed on its own. Phase 1 set that precedent for the PNG engine, and
`tests/test_gps_verify.py::test_verify_does_not_import_this_module_yet` makes
the unwired state a deliberate, visible fact rather than a forgotten one.

WHAT IS COVERED, AND AT WHICH STRENGTH OF EVIDENCE

Two tiers, kept apart because they are not the same claim.

Built, geotagged, scrubbed by the shipping tool and re-walked, on this machine
on 2026-09-06: `.jpg` (APP1 EXIF and APP1 XMP), `.tif`, `.png` (the eXIf chunk,
and the XMP text chunks including a zTXt and a compressed iTXt, which no byte
search can see into), `.webp` (EXIF and XMP chunks), `.mp4` (a real ffmpeg
container with a `(c)xyz` written by exiftool), and `.heic` (a real HEIF
container whose EXIF item is reachable only through `meta/iloc`). Eight
GPS-bearing files were found, and fourteen files with no GPS, including all
eight scrubbed outputs, reported clean. Zero false positives.

Registered and DISPATCH-measured only: `.jpeg .jpe .tiff` and the rest of the
ISO base media family, `.m4v .mov .qt .mqv .lrv .f4v .f4a .m4a .m4b .heif
.avif`. Each gets a GPS-bearing file of its container family built and walked
by `test_every_registered_extension_actually_dispatches_to_a_working_walker`,
so the routing is measured; no separate muxer's real box layout is.

NOT covered, and this is the list that matters: every raw format (17 of them,
all TIFF-based and all of which CAN carry a GPS IFD, none of which has a
fixture on this machine), Matroska and WebM, AVI, the transport streams, PDF,
SVG, GIF, JPEG 2000, PSD, and every document format. `coverage()` returns that
list rather than this comment, because a comment goes stale and a function does
not.
"""

from __future__ import annotations

import os
import struct
import zlib
from dataclasses import dataclass
from enum import Enum
from typing import Callable, Dict, List, Optional, Sequence, Tuple
from xml.etree import ElementTree

# ---------------------------------------------------------------- carrier classes

# The three carrier CLASSES this module knows how to interrogate. A report says
# which of these it actually looked for, so "we checked EXIF but this container
# can also hold XMP" is a statement the caller can make.
CARRIER_EXIF_GPS_IFD = "exif-gps-ifd"
CARRIER_XMP_GPS = "xmp-gps"
CARRIER_ISOBMFF_LOCATION = "isobmff-location"

ALL_CARRIERS = (CARRIER_EXIF_GPS_IFD, CARRIER_XMP_GPS, CARRIER_ISOBMFF_LOCATION)


class GpsStatus(str, Enum):
    """
    Five states, and only one of them means the file was checked and is clean.

    The four that are not CLEAN are not interchangeable, and collapsing any of
    them into CLEAN is the defect this module exists to prevent:

      NOT_CHECKED   no walker is registered for this extension. This check has
                    no opinion; the caller must not read one into it.
      ERROR         a walker could not parse the output. Trap 12: silence from
                    a read that failed is not evidence of absence.
      INCOMPLETE    the walk succeeded, found no carrier, and could not reach
                    part of the file. Half a check is not a pass.
      CARRIER_FOUND a GPS-bearing structure is still in the output.
      CLEAN         every carrier class this walker knows was interrogated,
                    across the whole file, and none is present.
    """

    NOT_CHECKED = "not_checked"
    ERROR = "error"
    INCOMPLETE = "incomplete"
    CARRIER_FOUND = "carrier_found"
    CLEAN = "clean"


@dataclass(frozen=True)
class GpsCarrier:
    """One surviving GPS-bearing structure, named and located."""

    carrier: str
    detail: str

    def __str__(self) -> str:
        return f"{self.carrier}: {self.detail}"


@dataclass(frozen=True)
class GpsReport:
    """
    What was checked, what was found, and what could not be reached.

    `carriers_checked` is the load-bearing field. `findings` being empty is only
    evidence in the company of a non-empty `carriers_checked` and an empty
    `limits`; that is why `is_clean` reads all three and why there is no
    shortcut around it.
    """

    applicable: bool
    carriers_checked: Tuple[str, ...] = ()
    findings: Tuple[GpsCarrier, ...] = ()
    limits: Tuple[str, ...] = ()
    error: Optional[str] = None
    extension: str = ""

    def __post_init__(self) -> None:
        # A report that says it checked nothing while claiming to be applicable
        # is the exact shape this module refuses to produce. Same for the
        # reverse: an inapplicable report that names carriers it checked.
        if self.applicable and self.error is None and not self.carriers_checked:
            raise ValueError(
                "an applicable GpsReport must name the carrier classes it "
                "checked; an empty carriers_checked would read as clean"
            )
        if not self.applicable and (self.carriers_checked or self.findings):
            raise ValueError(
                "an inapplicable GpsReport cannot have checked carriers or "
                "found any"
            )

    @property
    def status(self) -> GpsStatus:
        if not self.applicable:
            return GpsStatus.NOT_CHECKED
        if self.error is not None:
            return GpsStatus.ERROR
        if self.findings:
            return GpsStatus.CARRIER_FOUND
        if self.limits:
            return GpsStatus.INCOMPLETE
        return GpsStatus.CLEAN

    @property
    def is_clean(self) -> bool:
        """True for exactly one of the five states. Never for NOT_CHECKED."""
        return self.status is GpsStatus.CLEAN

    def __bool__(self) -> bool:
        """
        Refuse truthiness.

        A dataclass is truthy by default, so `if gps_report:` would be True for
        a `.mkv` nobody has a walker for, which is precisely the over-claim in
        section 9 of the design document. Raising here turns that mistake into
        a stack trace at the call site instead of a wrong verdict in a report.
        """
        raise TypeError(
            "GpsReport has no truth value: 'not checked', 'could not check', "
            "'partly checked' and 'checked and clean' are four different "
            "answers. Read .status or .is_clean."
        )

    def describe(self) -> str:
        """One line of TEXT, per trap 6: the distinction is never colour alone."""
        if not self.applicable:
            return (
                f"GPS carriers: NOT CHECKED (no GPS walker for "
                f"{self.extension or 'this format'})"
            )
        checked = ", ".join(self.carriers_checked)
        if self.error is not None:
            return f"GPS carriers: COULD NOT CHECK ({self.error})"
        if self.findings:
            found = "; ".join(str(item) for item in self.findings)
            return f"GPS carriers: FOUND {len(self.findings)} ({found})"
        if self.limits:
            return (
                f"GPS carriers: PARTLY CHECKED [{checked}]; none found, but "
                + "; ".join(self.limits)
            )
        return f"GPS carriers: none present [checked {checked}]"

    def as_dict(self) -> Dict:
        return {
            "status": self.status.value,
            "applicable": self.applicable,
            "is_clean": self.is_clean,
            "carriers_checked": list(self.carriers_checked),
            "findings": [str(item) for item in self.findings],
            "limits": list(self.limits),
            "error": self.error,
            "extension": self.extension,
            "detail": self.describe(),
        }


class _Walk:
    """Mutable accumulator handed to a walker. Turned into a GpsReport by scan()."""

    def __init__(self, *carriers: str) -> None:
        for carrier in carriers:
            if carrier not in ALL_CARRIERS:
                raise ValueError(f"unknown carrier class {carrier!r}")
        self.checked: Tuple[str, ...] = tuple(carriers)
        self.findings: List[GpsCarrier] = []
        self.limits: List[str] = []

    def found(self, carrier: str, detail: str) -> None:
        self.findings.append(GpsCarrier(carrier, detail))

    def limit(self, detail: str) -> None:
        self.limits.append(detail)


# ---------------------------------------------------------------- GPS tag names

# The EXIF GPS IFD's own tag numbers, from EXIF 2.32 table 15. Used only to
# DESCRIBE what was found; detection is "this IFD exists and has entries", not
# "one of these names is present", so an unknown tag inside a GPS IFD is still
# reported rather than skipped.
_GPS_IFD_TAGS = {
    0x0000: "GPSVersionID", 0x0001: "GPSLatitudeRef", 0x0002: "GPSLatitude",
    0x0003: "GPSLongitudeRef", 0x0004: "GPSLongitude", 0x0005: "GPSAltitudeRef",
    0x0006: "GPSAltitude", 0x0007: "GPSTimeStamp", 0x0008: "GPSSatellites",
    0x0009: "GPSStatus", 0x000A: "GPSMeasureMode", 0x000B: "GPSDOP",
    0x000C: "GPSSpeedRef", 0x000D: "GPSSpeed", 0x000E: "GPSTrackRef",
    0x000F: "GPSTrack", 0x0010: "GPSImgDirectionRef", 0x0011: "GPSImgDirection",
    0x0012: "GPSMapDatum", 0x0013: "GPSDestLatitudeRef", 0x0014: "GPSDestLatitude",
    0x0015: "GPSDestLongitudeRef", 0x0016: "GPSDestLongitude",
    0x0017: "GPSDestBearingRef", 0x0018: "GPSDestBearing",
    0x0019: "GPSDestDistanceRef", 0x001A: "GPSDestDistance",
    0x001B: "GPSProcessingMethod", 0x001C: "GPSAreaInformation",
    0x001D: "GPSDateStamp", 0x001E: "GPSDifferential", 0x001F: "GPSHPositioningError",
}

_TIFF_GPS_IFD_POINTER = 0x8825
_TIFF_EXIF_IFD_POINTER = 0x8769
_TIFF_SUB_IFDS = 0x014A
_TIFF_XMP = 0x02BC

# Byte width of each TIFF field type, indexed by the type code. 0 means "a type
# this walker does not size", which is not an error: the walker only needs the
# byte length in order to read a value out, and an unknown type is simply not
# read out. It never causes a GPS IFD to be missed, because a GPS IFD is found
# by its POINTER tag, whose type is always LONG.
_TIFF_TYPE_SIZE = {
    1: 1, 2: 1, 3: 2, 4: 4, 5: 8, 6: 1, 7: 1, 8: 2, 9: 4, 10: 8, 11: 4, 12: 8,
    13: 4, 16: 8, 17: 8, 18: 8,
}

_MAX_IFDS = 64          # a chain longer than this is a malformed or hostile file


# ---------------------------------------------------------------- XMP

def _xmp_text(blob: bytes) -> str:
    """
    Decode an XMP packet.

    An XMP packet is required to be UTF-8, UTF-16 or UTF-32 and to say which
    through its BOM. Latin-1 is the last resort and cannot fail, which is the
    right direction: a packet decoded imperfectly still yields the ELEMENT
    NAMES this module looks at, and failing to decode would mean failing to
    look.
    """
    for encoding in ("utf-8-sig", "utf-16", "utf-32", "latin-1"):
        try:
            text = blob.decode(encoding)
        except (UnicodeDecodeError, LookupError, ValueError):
            continue
        if "<" in text:
            return text
    return blob.decode("latin-1", "replace")


def _xmp_document(text: str) -> str:
    """
    Trim an XMP packet down to the one XML document inside it.

    A packet is wrapped in `<?xpacket begin=...?>` and `<?xpacket end=...?>`
    processing instructions and is padded with whitespace. ElementTree accepts
    a leading PI and rejects a trailing one as junk after the document element,
    so the wrapper is removed rather than parsed.
    """
    start = text.find("<x:xmpmeta")
    if start == -1:
        start = text.find("<rdf:RDF")
    if start == -1:
        # No recognised root. Fall back to the first element that is not a PI.
        index = 0
        while True:
            index = text.find("<", index)
            if index == -1:
                return text
            if text[index + 1:index + 2] not in ("?", "!"):
                start = index
                break
            index += 1
    end = text.rfind(">")
    if end == -1 or end < start:
        return text[start:]
    return text[start:end + 1]


def _scan_xmp(walk: _Walk, blob: bytes, where: str) -> None:
    """
    Report every XMP property whose LOCAL NAME begins with GPS, in any
    namespace.

    Parsed as XML, never grepped. docs/D2-GPS-VERIFICATION.md section 3
    measured the literal string `GPS` 7 times in a 102 MB MP4 that carries no
    GPS metadata at all, so a substring search of a packet's TEXT would report
    a coordinate that is not there. Matching on the qualified name of an
    element or an attribute cannot do that: a description that mentions GPS is
    character data and has no name.

    Any namespace, not just `http://ns.adobe.com/exif/1.0/`, because exifEX,
    Google's camera namespaces and a device vendor's own all carry the same
    values under the same local names, and a namespace allowlist here would be
    trap 11's blind spot in a new place.
    """
    text = _xmp_document(_xmp_text(blob))
    try:
        root = ElementTree.fromstring(text)
    except ElementTree.ParseError as exc:
        # Trap 12. An XMP packet we could not parse is not an XMP packet with
        # no GPS in it, and the two must not produce the same report.
        walk.limit(f"the XMP packet in {where} could not be parsed as XML ({exc})")
        return

    names: List[str] = []
    for element in root.iter():
        for qname in [element.tag] + list(element.attrib):
            if not isinstance(qname, str):
                continue
            local = qname.rsplit("}", 1)[-1]
            if local.startswith("GPS") and qname not in names:
                names.append(qname)
    if names:
        walk.found(
            CARRIER_XMP_GPS,
            f"{len(names)} GPS propert(ies) in the XMP packet in {where}: "
            + ", ".join(sorted(names)),
        )


# ---------------------------------------------------------------- TIFF / EXIF

def _tiff_byte_order(blob: bytes) -> str:
    if len(blob) < 8:
        raise ValueError("TIFF header is shorter than 8 bytes")
    order = blob[0:2]
    if order == b"II":
        endian = "<"
    elif order == b"MM":
        endian = ">"
    else:
        raise ValueError(f"not a TIFF header: byte order {order!r}")
    (magic,) = struct.unpack(endian + "H", blob[2:4])
    if magic == 43:
        # BigTIFF. A different IFD layout, so this walker cannot read it and
        # must say so rather than return an empty answer.
        raise ValueError("BigTIFF (magic 43) is not walked by this module")
    if magic != 42:
        raise ValueError(f"not a TIFF header: magic {magic}")
    return endian


def _ifd_entries(blob: bytes, endian: str, offset: int) -> Tuple[List[Tuple[int, int, int, int]], int]:
    """Return (entries, next_ifd_offset). Each entry is (tag, type, count, value_offset)."""
    if offset < 0 or offset + 2 > len(blob):
        raise ValueError(f"IFD offset {offset} is outside the TIFF block")
    (count,) = struct.unpack(endian + "H", blob[offset:offset + 2])
    end = offset + 2 + count * 12
    if end + 4 > len(blob):
        raise ValueError(
            f"the IFD at {offset} declares {count} entries, which runs past the "
            f"end of the {len(blob)}-byte TIFF block"
        )
    entries = []
    for index in range(count):
        at = offset + 2 + index * 12
        tag, typ, num = struct.unpack(endian + "HHI", blob[at:at + 8])
        (raw,) = struct.unpack(endian + "I", blob[at + 8:at + 12])
        entries.append((tag, typ, num, raw))
    (nxt,) = struct.unpack(endian + "I", blob[end:end + 4])
    return entries, nxt


def _entry_bytes(blob: bytes, endian: str, entry: Tuple[int, int, int, int],
                 at: int) -> Optional[bytes]:
    """The value bytes of one IFD entry, or None when its type has no known width."""
    _tag, typ, num, raw = entry
    width = _TIFF_TYPE_SIZE.get(typ, 0)
    if width == 0:
        return None
    length = width * num
    if length <= 4:
        # TIFF stores a value of four bytes or fewer in the FIRST bytes of the
        # value field, in both byte orders. Re-packing with the same endianness
        # it was unpacked with reproduces those four bytes exactly, so the
        # leading slice is right for II and for MM alike.
        packed = struct.pack(endian + "I", raw)
        return packed[:length]
    if raw + length > len(blob):
        raise ValueError(
            f"the value of tag 0x{entry[0]:04X} at {at} runs past the end of "
            f"the TIFF block"
        )
    return blob[raw:raw + length]


def _walk_tiff_block(walk: _Walk, blob: bytes, where: str) -> None:
    """
    Walk a TIFF/EXIF block and report a GPS IFD that still has entries.

    Detection is by POINTER, never by content: tag 0x8825 in any IFD names the
    GPS IFD's offset, and the IFD there is then read for its entry count. That
    is why this cannot false-positive the way a byte search does.
    docs/D2-GPS-VERIFICATION.md section 3 measured the 24 bytes of a Null
    Island latitude (`0/1 0/1 0/1`) occurring once in a 3.4 MB DLL and `1/1`
    occurring 257 times, so a coordinate at 0,0 or at a whole degree is
    indistinguishable
    from ordinary binary by any content-matching rule. A pointer walk does not
    care what the numbers are.

    The whole IFD chain is walked, thumbnails (IFD1) and SubIFDs included,
    because a GPS pointer can sit in any of them.
    """
    endian = _tiff_byte_order(blob)
    (first,) = struct.unpack(endian + "I", blob[4:8])

    pending = [(first, "IFD0")]
    seen = set()
    gps_pointers: List[Tuple[int, str]] = []
    walked = 0

    while pending:
        offset, name = pending.pop(0)
        if offset == 0 or offset in seen:
            continue
        seen.add(offset)
        walked += 1
        if walked > _MAX_IFDS:
            raise ValueError(f"more than {_MAX_IFDS} IFDs; refusing to keep walking")
        entries, nxt = _ifd_entries(blob, endian, offset)
        if nxt:
            pending.append((nxt, f"{name}+1"))
        for entry in entries:
            tag, typ, num, raw = entry
            if tag == _TIFF_GPS_IFD_POINTER:
                gps_pointers.append((raw, name))
            elif tag in (_TIFF_EXIF_IFD_POINTER, _TIFF_SUB_IFDS):
                if tag == _TIFF_SUB_IFDS and num > 1:
                    values = _entry_bytes(blob, endian, entry, offset)
                    if values is not None:
                        for index in range(num):
                            (sub,) = struct.unpack(
                                endian + "I", values[index * 4:index * 4 + 4]
                            )
                            pending.append((sub, f"{name}/SubIFD{index}"))
                else:
                    pending.append((raw, f"{name}/{'Exif' if tag == _TIFF_EXIF_IFD_POINTER else 'Sub'}IFD"))
            elif tag == _TIFF_XMP:
                value = _entry_bytes(blob, endian, entry, offset)
                if value:
                    _scan_xmp(walk, value, f"{where} {name} tag 0x02BC")

    for pointer, name in gps_pointers:
        entries, _ = _ifd_entries(blob, endian, pointer)
        if not entries:
            # A pointer to an EMPTY IFD carries no coordinate. Reported as a
            # limit rather than a finding or a pass: nothing is leaking, and a
            # scrubber that leaves the pointer behind is worth knowing about.
            walk.limit(
                f"{where} {name} still points at a GPS IFD, but that IFD has "
                f"zero entries"
            )
            continue
        described = ", ".join(
            _GPS_IFD_TAGS.get(tag, f"0x{tag:04X}") for tag, _t, _c, _v in entries
        )
        walk.found(
            CARRIER_EXIF_GPS_IFD,
            f"{where} {name} tag 0x8825 points at a GPS IFD at offset "
            f"{pointer} with {len(entries)} entr(ies): {described}",
        )


# ---------------------------------------------------------------- JPEG

_JPEG_EXIF_PREFIX = b"Exif\x00\x00"
_JPEG_XMP_PREFIX = b"http://ns.adobe.com/xap/1.0/\x00"
_JPEG_XMP_EXT_PREFIX = b"http://ns.adobe.com/xmp/extension/\x00"


def _next_jpeg_marker(data: bytes, offset: int) -> int:
    """
    Advance past entropy-coded data to the next real marker.

    Inside a scan, 0xFF00 is a stuffed byte and 0xFFD0..0xFFD7 are restart
    markers; neither ends the scan. Anything else after a run of 0xFF does.
    """
    total = len(data)
    while offset < total:
        if data[offset] != 0xFF:
            offset += 1
            continue
        probe = offset + 1
        while probe < total and data[probe] == 0xFF:
            probe += 1
        if probe >= total:
            return total
        marker = data[probe]
        if marker == 0x00 or 0xD0 <= marker <= 0xD7:
            offset = probe + 1
            continue
        return probe - 1
    return total


def _walk_jpeg(walk: _Walk, data: bytes) -> None:
    if not data.startswith(b"\xff\xd8"):
        raise ValueError("not a JPEG: no SOI marker")
    offset = 2
    total = len(data)
    saw_eoi = False
    extended_xmp = 0

    while offset + 2 <= total:
        if data[offset] != 0xFF:
            raise ValueError(f"expected a marker at offset {offset}, found 0x{data[offset]:02X}")
        probe = offset
        while probe < total and data[probe] == 0xFF:
            probe += 1
        if probe >= total:
            raise ValueError("the file ends inside a marker")
        marker = data[probe]
        offset = probe - 1
        if marker == 0xD9:                              # EOI
            offset = probe + 1
            saw_eoi = True
            break
        if marker == 0x01 or 0xD0 <= marker <= 0xD7:    # TEM, RSTn: no payload
            offset = probe + 1
            continue
        if offset + 4 > total:
            raise ValueError(f"the segment at offset {offset} has no length field")
        (length,) = struct.unpack(">H", data[offset + 2:offset + 4])
        if length < 2 or offset + 2 + length > total:
            raise ValueError(
                f"the segment at offset {offset} declares {length} bytes, which "
                f"runs past the end of the file"
            )
        payload = data[offset + 4:offset + 2 + length]
        if marker == 0xE1:                              # APP1
            if payload.startswith(_JPEG_EXIF_PREFIX):
                _walk_tiff_block(walk, payload[len(_JPEG_EXIF_PREFIX):],
                                 f"the APP1 EXIF block at offset {offset}")
            elif payload.startswith(_JPEG_XMP_PREFIX):
                _scan_xmp(walk, payload[len(_JPEG_XMP_PREFIX):],
                          f"the APP1 XMP segment at offset {offset}")
            elif payload.startswith(_JPEG_XMP_EXT_PREFIX):
                extended_xmp += 1
        offset = offset + 2 + length
        if marker == 0xDA:                              # SOS: entropy data follows
            offset = _next_jpeg_marker(data, offset)

    if extended_xmp:
        # ExtendedXMP is split across numbered APP1 chunks that have to be
        # reassembled in offset order before they are valid XML. Not doing that
        # is a LIMIT and never a pass.
        walk.limit(
            f"{extended_xmp} ExtendedXMP APP1 chunk(s) were not reassembled, so "
            f"any GPS property inside them was not interrogated"
        )
    if not saw_eoi:
        raise ValueError("no EOI marker: the JPEG is truncated")
    if offset < len(data):
        # A motion photo hides an entire second file here, and it can carry its
        # own EXIF GPS. Not walking it is honest; calling the file clean anyway
        # would not be.
        walk.limit(
            f"{len(data) - offset} bytes follow the JPEG EOI and were not "
            f"interrogated for GPS carriers"
        )


# ---------------------------------------------------------------- PNG

_PNG_SIGNATURE = b"\x89PNG\r\n\x1a\n"
_PNG_XMP_KEYWORD = b"XML:com.adobe.xmp"


def _walk_png(walk: _Walk, data: bytes) -> None:
    if not data.startswith(_PNG_SIGNATURE):
        raise ValueError("not a PNG: signature mismatch")
    offset = len(_PNG_SIGNATURE)
    total = len(data)
    while offset + 8 <= total:
        (length,) = struct.unpack(">I", data[offset:offset + 4])
        ctype = data[offset + 4:offset + 8]
        end = offset + 12 + length
        if end > total:
            raise ValueError(
                f"chunk {ctype.decode('latin-1', 'replace')!r} at offset "
                f"{offset} declares {length} bytes, which runs past the end"
            )
        payload = data[offset + 8:end - 4]
        if ctype == b"eXIf":
            _walk_tiff_block(walk, payload, f"the eXIf chunk at offset {offset}")
        elif ctype in (b"iTXt", b"tEXt", b"zTXt"):
            unpacked = _png_text_payload(walk, ctype, payload, offset)
            if unpacked is not None:
                keyword, value = unpacked
                if keyword == _PNG_XMP_KEYWORD:
                    _scan_xmp(walk, value,
                              f"the {ctype.decode('latin-1')} chunk at offset {offset}")
        offset = end
        if ctype == b"IEND":
            break
    if offset < total:
        walk.limit(
            f"{total - offset} bytes follow the PNG IEND chunk and were not "
            f"interrogated for GPS carriers"
        )


def _png_text_payload(walk: _Walk, ctype: bytes, payload: bytes,
                      offset: int) -> Optional[Tuple[bytes, bytes]]:
    """
    Return (keyword, value) for a PNG text chunk, inflating it when it is
    compressed.

    A zTXt or a compressed iTXt is the measured blind spot the PNG engine's
    tests are built around: a sentinel inside one is not in the file's bytes at
    all, so a byte search of the file finds nothing whether or not it survived.
    Inflating here is what lets the XMP check see inside one.
    """
    try:
        keyword, rest = payload.split(b"\x00", 1)
    except ValueError:
        walk.limit(f"the {ctype.decode('latin-1')} chunk at offset {offset} has no keyword")
        return None
    try:
        if ctype == b"tEXt":
            return keyword, rest
        if ctype == b"zTXt":
            if not rest:
                return keyword, b""
            return keyword, zlib.decompress(rest[1:])
        # iTXt: flag(1) method(1) language\0 translated\0 text
        flag = rest[0]
        body = rest[2:]
        _language, body = body.split(b"\x00", 1)
        _translated, body = body.split(b"\x00", 1)
        if flag:
            body = zlib.decompress(body)
        return keyword, body
    except (IndexError, ValueError, zlib.error) as exc:
        walk.limit(
            f"the {ctype.decode('latin-1')} chunk at offset {offset} could not "
            f"be decoded ({exc}), so any XMP inside it was not interrogated"
        )
        return None


# ---------------------------------------------------------------- WebP

def _walk_webp(walk: _Walk, data: bytes) -> None:
    if len(data) < 12 or data[0:4] != b"RIFF" or data[8:12] != b"WEBP":
        raise ValueError("not a WebP: RIFF/WEBP header mismatch")
    (declared,) = struct.unpack("<I", data[4:8])
    limit = min(declared + 8, len(data))
    offset = 12
    while offset + 8 <= limit:
        fourcc = data[offset:offset + 4]
        (size,) = struct.unpack("<I", data[offset + 4:offset + 8])
        end = offset + 8 + size
        if end > limit:
            raise ValueError(
                f"chunk {fourcc.decode('latin-1', 'replace')!r} at offset "
                f"{offset} runs past the end of the RIFF container"
            )
        payload = data[offset + 8:end]
        if fourcc == b"EXIF":
            # Some encoders prefix the EXIF chunk with the JPEG APP1 signature.
            if payload.startswith(_JPEG_EXIF_PREFIX):
                payload = payload[len(_JPEG_EXIF_PREFIX):]
            _walk_tiff_block(walk, payload, f"the EXIF chunk at offset {offset}")
        elif fourcc == b"XMP ":
            _scan_xmp(walk, payload, f"the XMP chunk at offset {offset}")
        offset = end + (size & 1)
    if declared + 8 < len(data):
        walk.limit(
            f"{len(data) - (declared + 8)} bytes follow the declared RIFF size "
            f"and were not interrogated for GPS carriers"
        )


# ---------------------------------------------------------------- ISO base media

# THIS BOX WALKER IS DELIBERATELY NOT `metascrub/isobmff.py`.
#
# That module landed the same day as this one and is the ISO base media parser
# the REMOVAL engine uses. This one is the parser the CHECK uses, and the two
# must not be the same code: a checker that shares a parser with the thing it
# checks cannot catch a parser bug, which is the rule `test_av_muxer` set by
# refusing to import the engine's own `ftyp_brand` and `test_png_engine`
# repeated by writing its own chunk walker. If a box-tree bug ever lets the
# engine leave a `(c)xyz` behind, a shared walker would miss it in exactly the
# same way and the verdict would agree with the mistake. Merging them is not a
# cleanup; it is the removal of one of two independent measurements.

# The XMP box's user type, ISO 16684-1 / the Adobe XMP specification part 3.
_ISOBMFF_XMP_UUID = bytes.fromhex("BE7ACFCB97A942E89C71999491E3AFAC")

# Boxes whose payload is a sequence of further boxes. A type that is not here
# is NEVER descended into, which is what keeps the `mdat` payload out of the
# walk: docs/D2-GPS-VERIFICATION.md section 3 measured 3620 sequences of 0xA9
# followed by three ASCII letters per 102 MB of ordinary H.264, so a search of
# the whole file for the four bytes of a `(c)xyz` atom would eventually invent
# a location box. Only a box tree walk cannot.
_ISOBMFF_CONTAINERS = frozenset({
    b"moov", b"trak", b"mdia", b"minf", b"edts", b"udta", b"ilst",
    b"moof", b"traf", b"mvex", b"dinf", b"stbl", b"gmhd",
})

# `meta` is a FullBox in ISO base media (4 bytes of version and flags before
# its children) and a plain container in QuickTime. Sniffed rather than assumed.
_ISOBMFF_FULLBOX_CONTAINERS = frozenset({b"meta"})

# The location atoms. `(c)xyz` is the ISO 6709 string a Pixel writes;
# `loci` is the 3GPP location box; `gps ` is the GoPro/Garmin location table.
_ISOBMFF_LOCATION_BOXES = {
    b"\xa9xyz": "ISO 6709 location atom (c)xyz",
    b"loci": "3GPP location box loci",
    b"gps ": "location table box gps",
}

_MAX_ISOBMFF_DEPTH = 12


def _isobmff_boxes(data: bytes, start: int, end: int):
    """Yield (btype, usertype, body_start, body_end, box_start) for one level."""
    offset = start
    while offset + 8 <= end:
        (size,) = struct.unpack(">I", data[offset:offset + 4])
        btype = data[offset + 4:offset + 8]
        header = 8
        if size == 1:
            if offset + 16 > end:
                raise ValueError(f"the 64-bit box at offset {offset} is truncated")
            (size,) = struct.unpack(">Q", data[offset + 8:offset + 16])
            header = 16
        elif size == 0:
            size = end - offset
        if size < header or offset + size > end:
            raise ValueError(
                f"box {btype.decode('latin-1', 'replace')!r} at offset {offset} "
                f"declares {size} bytes, which does not fit in its parent"
            )
        usertype = None
        if btype == b"uuid":
            if offset + header + 16 > end:
                raise ValueError(f"the uuid box at offset {offset} has no user type")
            usertype = data[offset + header:offset + header + 16]
            header += 16
        yield btype, usertype, offset + header, offset + size, offset
        offset += size


def _isobmff_is_box_run(data: bytes, start: int, end: int) -> bool:
    """Does a plausible box header sit at `start`? Used to sniff meta's flavour."""
    if start + 8 > end:
        return False
    (size,) = struct.unpack(">I", data[start:start + 4])
    btype = data[start + 4:start + 8]
    if not all(0x20 <= byte <= 0x7E or byte == 0xA9 for byte in btype):
        return False
    return size == 0 or size == 1 or (8 <= size <= end - start)


def _walk_isobmff(walk: _Walk, data: bytes) -> None:
    if len(data) < 8:
        raise ValueError("too short to be an ISO base media file")
    if data[4:8] not in (b"ftyp", b"styp", b"moov", b"skip", b"free", b"mdat"):
        raise ValueError(
            f"not an ISO base media file: the first box is "
            f"{data[4:8].decode('latin-1', 'replace')!r}, not ftyp"
        )
    exif_items: Dict[int, str] = {}
    item_locations: Dict[int, List[Tuple[int, int]]] = {}
    _walk_isobmff_level(walk, data, 0, len(data), (), 0, exif_items, item_locations)
    _walk_isobmff_items(walk, data, exif_items, item_locations)


def _walk_isobmff_level(walk: _Walk, data: bytes, start: int, end: int,
                        path: Tuple[bytes, ...], depth: int,
                        exif_items: Dict[int, str],
                        item_locations: Dict[int, List[Tuple[int, int]]]) -> None:
    if depth > _MAX_ISOBMFF_DEPTH:
        walk.limit(f"stopped descending at depth {depth} under /{_path(path)}")
        return
    for btype, usertype, body, body_end, box in _isobmff_boxes(data, start, end):
        here = path + (btype,)
        if btype in _ISOBMFF_LOCATION_BOXES and b"udta" in path:
            walk.found(
                CARRIER_ISOBMFF_LOCATION,
                f"{_ISOBMFF_LOCATION_BOXES[btype]} at offset {box} in "
                f"/{_path(path)} ({body_end - body} bytes)",
            )
            continue
        if btype in _ISOBMFF_LOCATION_BOXES and b"ilst" in path:
            walk.found(
                CARRIER_ISOBMFF_LOCATION,
                f"{_ISOBMFF_LOCATION_BOXES[btype]} at offset {box} in "
                f"/{_path(path)} ({body_end - body} bytes)",
            )
            continue
        if btype == b"uuid" and usertype == _ISOBMFF_XMP_UUID:
            _scan_xmp(walk, data[body:body_end], f"the XMP uuid box at offset {box}")
            continue
        if btype == b"XMP_" and b"udta" in path:
            _scan_xmp(walk, data[body:body_end], f"the udta XMP_ box at offset {box}")
            continue
        if btype == b"keys":
            _isobmff_keys(walk, data, body, body_end, box)
            continue
        if btype == b"iinf":
            _isobmff_iinf(walk, data, body, body_end, exif_items)
            continue
        if btype == b"iloc":
            _isobmff_iloc(walk, data, body, body_end, item_locations)
            continue
        if btype in _ISOBMFF_CONTAINERS:
            _walk_isobmff_level(walk, data, body, body_end, here, depth + 1,
                                exif_items, item_locations)
        elif btype in _ISOBMFF_FULLBOX_CONTAINERS:
            # FullBox flavour has 4 bytes of version/flags first; QuickTime's
            # does not. Sniff which, rather than pick one and be wrong on half
            # the containers, the way a static extension-to-muxer map was wrong
            # on four of eight in trap 8.
            child = body + 4 if _isobmff_is_box_run(data, body + 4, body_end) else body
            _walk_isobmff_level(walk, data, child, body_end, here, depth + 1,
                                exif_items, item_locations)


def _path(path: Tuple[bytes, ...]) -> str:
    return "/".join(part.decode("latin-1", "replace") for part in path) or "(top level)"


def _isobmff_keys(walk: _Walk, data: bytes, body: int, end: int, box: int) -> None:
    """
    Read a QuickTime `keys` table and report a location key by NAME.

    The metadata a modern iPhone writes lives here as
    `com.apple.quicktime.location.ISO6709`. This reads the key table, which is
    a length-prefixed list of names, and never searches the file for the text.
    """
    if body + 8 > end:
        walk.limit(f"the keys box at offset {box} is too short to read")
        return
    (count,) = struct.unpack(">I", data[body + 4:body + 8])
    offset = body + 8
    for _ in range(count):
        if offset + 8 > end:
            walk.limit(f"the keys box at offset {box} ends inside an entry")
            return
        (size,) = struct.unpack(">I", data[offset:offset + 4])
        if size < 8 or offset + size > end:
            walk.limit(f"the keys box at offset {box} has a malformed entry")
            return
        namespace = data[offset + 4:offset + 8]
        name = data[offset + 8:offset + size]
        lowered = name.lower()
        if b"location" in lowered or b"gps" in lowered:
            walk.found(
                CARRIER_ISOBMFF_LOCATION,
                f"the metadata key {name.decode('latin-1', 'replace')!r} "
                f"(namespace {namespace.decode('latin-1', 'replace')!r}) at "
                f"offset {offset}",
            )
        offset += size


def _isobmff_iinf(walk: _Walk, data: bytes, body: int, end: int,
                  exif_items: Dict[int, str]) -> None:
    """Record the item IDs whose item_type is Exif, for the iloc walk to find."""
    if body + 4 > end:
        return
    version = data[body]
    offset = body + 4
    if version == 0:
        if offset + 2 > end:
            return
        offset += 2
    else:
        if offset + 4 > end:
            return
        offset += 4
    for btype, _usertype, ibody, iend, _box in _isobmff_boxes(data, offset, end):
        if btype != b"infe" or ibody + 4 > iend:
            continue
        iversion = data[ibody]
        at = ibody + 4
        if iversion in (2, 3):
            width = 2 if iversion == 2 else 4
            if at + width + 2 + 4 > iend:
                continue
            item_id = int.from_bytes(data[at:at + width], "big")
            item_type = data[at + width + 2:at + width + 6]
            if item_type == b"Exif":
                exif_items[item_id] = f"item {item_id}"


def _isobmff_iloc(walk: _Walk, data: bytes, body: int, end: int,
                  item_locations: Dict[int, List[Tuple[int, int]]]) -> None:
    """Read the item location box into {item_id: [(offset, length), ...]}."""
    if body + 6 > end:
        return
    version = data[body]
    offset_size = data[body + 4] >> 4
    length_size = data[body + 4] & 0x0F
    base_size = data[body + 5] >> 4
    index_size = data[body + 5] & 0x0F if version in (1, 2) else 0
    at = body + 6
    if version < 2:
        if at + 2 > end:
            return
        count = int.from_bytes(data[at:at + 2], "big")
        at += 2
        id_width = 2
    else:
        if at + 4 > end:
            return
        count = int.from_bytes(data[at:at + 4], "big")
        at += 4
        id_width = 4

    def take(width: int) -> Optional[int]:
        nonlocal at
        if width == 0:
            return 0
        if at + width > end:
            return None
        value = int.from_bytes(data[at:at + width], "big")
        at += width
        return value

    for _ in range(count):
        item_id = take(id_width)
        if item_id is None:
            return
        if version in (1, 2) and take(2) is None:
            return
        if take(2) is None:                     # data_reference_index
            return
        base = take(base_size)
        if base is None:
            return
        extents = take(2)
        if extents is None:
            return
        found: List[Tuple[int, int]] = []
        for _index in range(extents):
            if index_size and take(index_size) is None:
                return
            start = take(offset_size)
            length = take(length_size)
            if start is None or length is None:
                return
            found.append((base + start, length))
        item_locations[item_id] = found


def _walk_isobmff_items(walk: _Walk, data: bytes, exif_items: Dict[int, str],
                        item_locations: Dict[int, List[Tuple[int, int]]]) -> None:
    """
    Walk the EXIF payload of every HEIF/AVIF `Exif` item.

    The item's bytes live in `mdat` and are reachable only through `iloc`, so
    an ISO base media still image keeps its GPS somewhere the box tree alone
    does not show. An item this walker cannot locate is a LIMIT, because "we
    know there is an EXIF item and we could not read it" is not a clean file.
    """
    for item_id, name in sorted(exif_items.items()):
        extents = item_locations.get(item_id)
        if not extents:
            walk.limit(
                f"the ISO base media {name} is an Exif item and its location "
                f"could not be read, so its GPS IFD was not interrogated"
            )
            continue
        for start, length in extents:
            if length == 0:
                # A zero-length extent is a MEASURED absence, not an unchecked
                # region: there are provably no EXIF bytes to walk. Measured
                # 2026-09-06 on a scrubbed HEIC, which is exactly the shape the
                # exiftool engine leaves behind: the `infe` Exif item survives
                # in `iinf` while its `iloc` extent is rewritten from 216 bytes
                # to 0. Calling that a limit would report INCOMPLETE on every
                # correctly scrubbed HEIC.
                continue
            if start + length > len(data):
                walk.limit(
                    f"the ISO base media {name} points at {length} bytes at "
                    f"{start}, which is outside the file"
                )
                continue
            payload = data[start:start + length]
            # ISO 23008-12: the Exif item payload begins with a 4-byte offset
            # to the TIFF header, and that offset is normally zero.
            if len(payload) < 4:
                walk.limit(f"the ISO base media {name} is too short to hold EXIF")
                continue
            (skip,) = struct.unpack(">I", payload[:4])
            block = payload[4 + skip:]
            _walk_tiff_block(walk, block, f"the EXIF {name}")


# ---------------------------------------------------------------- dispatch


def _jpeg_walker(walk: _Walk, data: bytes) -> None:
    _walk_jpeg(walk, data)


def _tiff_walker(walk: _Walk, data: bytes) -> None:
    _walk_tiff_block(walk, data, "the TIFF")


# Every ISO base media extension the TOOL itself handles, and no others.
# `.3gp` and `.3g2` are ISO base media too and are deliberately absent: neither
# has a row in CAPABILITIES, so the tool cannot scrub one, and a GPS walker for
# a file this tool refuses is a coverage claim with nothing behind it.
# `test_coverage_reports_the_gap_at_runtime` asserts that emptiness rather than
# trusting this comment.
_ISOBMFF_EXTENSIONS = (
    ".mp4", ".m4v", ".mov", ".qt", ".mqv", ".lrv", ".f4v", ".f4a",
    ".m4a", ".m4b", ".heic", ".heif", ".avif",
)

# Which carrier classes each walker actually interrogates. Kept beside the
# walker table rather than inside the walkers so that a walker which stops
# looking at one of them cannot quietly keep claiming it.
_WALKERS: Dict[str, Tuple[Callable[[_Walk, bytes], None], Tuple[str, ...]]] = {}

for _ext in (".jpg", ".jpeg", ".jpe"):
    _WALKERS[_ext] = (_jpeg_walker, (CARRIER_EXIF_GPS_IFD, CARRIER_XMP_GPS))
for _ext in (".tif", ".tiff"):
    _WALKERS[_ext] = (_tiff_walker, (CARRIER_EXIF_GPS_IFD, CARRIER_XMP_GPS))
_WALKERS[".png"] = (_walk_png, (CARRIER_EXIF_GPS_IFD, CARRIER_XMP_GPS))
_WALKERS[".webp"] = (_walk_webp, (CARRIER_EXIF_GPS_IFD, CARRIER_XMP_GPS))
for _ext in _ISOBMFF_EXTENSIONS:
    _WALKERS[_ext] = (_walk_isobmff, ALL_CARRIERS)
del _ext


def supported_extensions() -> frozenset:
    """Extensions this check has an opinion about."""
    return frozenset(_WALKERS)


def carriers_for(extension: str) -> Tuple[str, ...]:
    """The carrier classes the walker for `extension` interrogates, or ()."""
    entry = _WALKERS.get(extension.lower())
    return entry[1] if entry else ()


def coverage() -> Dict[str, List[str]]:
    """
    Which of the tool's own formats this check covers, answered at RUNTIME.

    The point of computing this rather than writing it down is section 9 of the
    design document: the failure mode being guarded against is a CHANGELOG that
    says "GPS verification" while most extensions still have no walker. A
    caller, a test and `doctor` can all print this, and it cannot go stale.
    """
    from .capabilities import CAPABILITIES

    covered = sorted(ext for ext in CAPABILITIES if ext in _WALKERS)
    return {
        "covered": covered,
        "uncovered": sorted(ext for ext in CAPABILITIES if ext not in _WALKERS),
        "walkers_without_a_capability_row": sorted(
            ext for ext in _WALKERS if ext not in CAPABILITIES
        ),
    }


# Field names that scope this check for a SELECTIVE removal. A run that was
# asked to remove only the Artist must not be failed for the GPS the user
# deliberately kept: measured 2026-09-06 by
# tests/test_gps_verify.py::test_a_selective_run_that_keeps_gps_must_still_
# verify_clean, a selective removal of Artist on a geotagged JPEG removes the
# Artist, leaves the GPS carrier in the output, and correctly reports
# "verified clean". An unconditional GPS assertion would turn that correct run
# into a failure. This mirrors verify.scoped_values(), which exists for the
# same reason.
_GPS_FIELD_MARKER = "gps"
_GPS_FIELD_NAMES = frozenset({
    "location", "geotag", "geolocation", "iso6709", "coordinates",
})


def gps_in_scope(fields: Optional[Sequence[str]]) -> bool:
    """
    Should the GPS check run for a selective removal targeting `fields`?

    `None` or an empty sequence means a full strip, where everything is in
    scope. Otherwise the check runs only when at least one named field is a GPS
    field, matched on the bare tag name with any group prefix removed.
    """
    if not fields:
        return True
    for name in fields:
        bare = str(name).lower().rsplit(":", 1)[-1].strip()
        if bare.startswith(_GPS_FIELD_MARKER) or bare in _GPS_FIELD_NAMES:
            return True
    return False


def scan(path: str) -> GpsReport:
    """
    Walk `path` and report every GPS-bearing structure it still contains.

    Never raises for a file it cannot parse. An unparseable output is reported
    through `error`, which is GpsStatus.ERROR and is never GpsStatus.CLEAN:
    trap 12, in a new place. An extension with no walker is
    GpsStatus.NOT_CHECKED, which changes no verdict and claims nothing.
    """
    extension = os.path.splitext(path)[1].lower()
    entry = _WALKERS.get(extension)
    if entry is None:
        return GpsReport(applicable=False, extension=extension)
    walker, carriers = entry
    try:
        with open(path, "rb") as handle:
            data = handle.read()
    except OSError as exc:
        return GpsReport(applicable=True, carriers_checked=carriers,
                         extension=extension,
                         error=f"could not read the output: {exc}")
    walk = _Walk(*carriers)
    try:
        walker(walk, data)
    except (ValueError, IndexError, KeyError, struct.error) as exc:
        return GpsReport(applicable=True, carriers_checked=carriers,
                         extension=extension,
                         error=f"could not parse the output: {exc}")
    return GpsReport(
        applicable=True,
        carriers_checked=carriers,
        findings=tuple(walk.findings),
        limits=tuple(walk.limits),
        extension=extension,
    )
