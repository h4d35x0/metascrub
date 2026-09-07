"""
JPEG engine: pure-Python marker-stream surgery. No exiftool, no ffmpeg, no
Pillow, because none of the three can run on a phone.

Phase 1 of the Android media build. The removal table this implements is
docs/ANDROID-MEDIA-BUILD.md section 2.1, and the structural assertions that
verify it are section 3.2. Both were corrected by measurement on 2026-09-06;
the corrections are the interesting part and each one is named below where it
changes the code.

WHAT THIS ENGINE DOES

It rebuilds the marker stream, keeping only the segments section 2.1 names and
copying the entropy-coded scan data through byte for byte. It never decodes and
never re-encodes: the compressed image data in the output is the same bytes it
was in the input, so a scrub can cost image quality only if this file has a bug.

THE THREE THINGS THAT MAKE IT DIFFERENT FROM A NAIVE STRIPPER

1. THE KEEP-LIST IS KEYED ON THE IDENTIFIER STRING, NOT THE MARKER NUMBER.
   Measured 2026-09-06: an APP0 whose identifier is `JFXX` carries a complete
   embedded JPEG thumbnail, 675 bytes with an intact sentinel, reported by
   exiftool as `[JFIF] ThumbnailImage`. "Keep APP0" implemented as "keep marker
   0xE0" therefore keeps a pre-edit thumbnail through the scrub. The same
   applies to APP14: an APP14 whose identifier is not `Adobe` is a vendor
   block, not a colour-transform declaration.

   And APP1 is not one segment. An oversized XMP payload produced THREE APP1
   segments carrying `Exif\\0\\0`, `http://ns.adobe.com/xap/1.0/\\0` and
   `http://ns.adobe.com/xmp/extension/\\0`, the last repeated. Nothing here
   looks for "the" APP1; the walk visits every segment and the keep-list is
   consulted for each.

2. EVERYTHING AFTER THE FIRST TOP-LEVEL EOI IS REMOVED, and finding that EOI
   requires walking rather than searching. Google and Samsung Motion Photos
   append a complete MP4, with its own GPS, after the JPEG's EOI. Section 2.1
   calls it the highest-severity carrier in the document, and section 3.3
   measured exiftool blind to it.

   Re-measured here, with one correction to how 3.3 reads. "Reports zero tags
   and zero warnings" holds for the Google flavour, where removing APP1 orphans
   the container directory, and for an unlabelled MP4 append. It does NOT hold
   for the Samsung flavour: a SEF trailer is discovered backward from the last
   six bytes of the file and needs nothing in any APPn segment, so exiftool
   still reports EmbeddedVideoFile, EmbeddedVideoType and TimeStamp. What holds
   for both is the part that matters: exiftool names the blob and never says
   what is inside it, so the video's GPS and title appear in no reported value
   and no needle for them can be constructed. See
   test_the_oracle_cannot_see_what_is_inside_a_retained_trailer.

   The leaking shape is NOT "parse to EOI and stop", which drops the trailer as
   a side effect. It is the common one: parse the headers, then copy the
   remainder of the file from SOS onward. tests/test_motion_photo.py
   demonstrates it and this engine is asserted against it.

   A search for the first `FF D9` in the file is worse than either, because an
   APP0/JFXX or an EXIF thumbnail is a complete JPEG with its own EOI, so the
   search finds the thumbnail's EOI and a walker built on it truncates the real
   image away. `_walk` consumes every segment by its declared length, so the
   only EOI it can reach is the outer one.

3. ICC IS REMOVED UNCONDITIONALLY. Section 2.1 originally proposed keeping an
   APP2/ICC_PROFILE segment when the profile bytes hash to a known standard
   profile. That rule does not work as written, measured 2026-09-06: two sRGB
   profiles generated 1.2 seconds apart differ at exactly one byte, offset 35,
   which is the seconds field of the ICC header. A raw byte hash therefore
   rejects a profile it should accept, and making it work needs the header
   normalised (creation timestamp and profile ID zeroed) before hashing. The
   section's own recommendation, implemented here, is to remove ICC
   unconditionally and accept sRGB rendering: a colour shift is visible and
   recoverable, a custom identifying profile is neither.

WHERE THIS ENGINE IS WIRED IN

Nowhere. The .jpg and .jpeg rows in CAPABILITIES still route to the exiftool
engine, which works on the desktop and shipped in 1.0.2. Flipping the default
is a separate decision. available() returns True because the engine can now
actually run, and `doctor` should say so.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Iterator, List, Optional, Tuple

from .base import BaseEngine, EngineError, atomic_replace, temp_beside

# Markers that carry no length field. TEM, SOI, EOI and RST0..RST7.
_STANDALONE = {0x01, 0xD8, 0xD9} | set(range(0xD0, 0xD8))

SOI = 0xD8
EOI = 0xD9
SOS = 0xDA
DQT = 0xDB
DHT = 0xC4
DRI = 0xDD
COM = 0xFE
APP0 = 0xE0
APP14 = 0xEE

# Start Of Frame, every flavour: baseline, extended, progressive, lossless,
# differential and arithmetic. 0xC4 is DHT, 0xC8 is the reserved JPG marker and
# 0xCC is DAC; none of the three is a SOF even though they sit in the range.
_SOF_CODES = frozenset(
    code for code in range(0xC0, 0xD0) if code not in (0xC4, 0xC8, 0xCC)
)

# The keep-list rows that are a marker number and nothing else, because they
# carry no identifier and cannot carry a payload that is not decode data.
# RST0..RST7 are in here for completeness; in practice they live inside the
# entropy-coded run and are copied through with it rather than visited.
#
# DNL (0xDC), DHP (0xDE), EXP (0xDF), DAC (0xCC) and TEM (0x01) are NOT here,
# and that is a decision rather than an oversight. Section 2.1 says "keep only
# these markers" and names none of the five; Part 2's opening says a whitelist
# removes everything else INCLUDING unknown structures. The reference stripper
# written during Phase 0 recon keeps DNL, DHP and EXP, so the two disagree and
# this file follows the specification.
#
# What that costs, stated rather than hidden: DHP and EXP belong to
# hierarchical mode and DNL to an encoder that does not know the image height
# in advance. Removing one from a file that genuinely carried it would damage
# that file. NOT MEASURED either way, because no encoder available here emits
# any of them: Pillow's baseline, progressive, optimised, CMYK and
# restart-interval paths were walked on 2026-09-06 and none appeared, so no
# fixture here has ever carried one and neither choice has been tested against
# a real file. If one ever turns up, refusing it outright is probably a better
# answer than either keeping or dropping the marker, because this engine has
# no other handling for hierarchical mode; that is a decision to make with a
# fixture in hand rather than now.
_KEEP_CODES = frozenset(
    {SOI, EOI, SOS, DQT, DHT, DRI} | set(range(0xD0, 0xD8))
) | _SOF_CODES

# The two keep-list rows that are keyed on the identifier string. See point 1
# in the module docstring: this table, not the marker number, is the rule.
#
# APP0's identifier is NUL terminated, so the test is the five bytes `JFIF\0`.
# APP14's is not: the Adobe segment is the literal `Adobe` followed
# immediately by a two-byte version, so the test is the five bytes `Adobe` and
# the NUL that `_identifier` finds after them is the high byte of the version
# rather than a terminator. Measured 2026-09-06 on Pillow's CMYK output:
# payload b'Adobe\x00d\x00\x00\x00\x00\x00', version 100, flags 0, transform 0.
_KEEP_IDENTIFIERS = {
    APP0: b"JFIF\x00",
    APP14: b"Adobe",
}

_NAMES = {
    0x01: "TEM", 0xC4: "DHT", 0xCC: "DAC", 0xD8: "SOI", 0xD9: "EOI",
    0xDA: "SOS", 0xDB: "DQT", 0xDC: "DNL", 0xDD: "DRI", 0xDE: "DHP",
    0xDF: "EXP", 0xFE: "COM",
}
for _index in range(16):
    _NAMES.setdefault(0xE0 + _index, "APP%d" % _index)
for _index in range(8):
    _NAMES[0xD0 + _index] = "RST%d" % _index
for _code in _SOF_CODES:
    _NAMES[_code] = "SOF%d" % (_code - 0xC0)

# What each removed APPn is known to carry, for the description strip_all
# returns. A name absent from here is not a name the engine treats differently;
# it is only a label. Section 2.1's removal table is the source.
_CARRIER_NOTES = {
    b"Exif": "EXIF, including GPS and the embedded thumbnail",
    b"JFXX": "an embedded JFIF thumbnail, a complete JPEG of its own",
    b"http://ns.adobe.com/xap/1.0/": "XMP",
    b"http://ns.adobe.com/xmp/extension/": "ExtendedXMP",
    b"ICC_PROFILE": "an ICC colour profile",
    b"MPF": "Multi-Picture Format, used by burst and motion photos",
    b"Photoshop 3.0": "Photoshop image resource blocks, where IPTC lives",
    b"Ducky": "Ducky / Picture Info",
    b"JP": "JUMBF, where C2PA content credentials live",
}

# The identifier is a leading NUL-terminated ASCII string. A NUL a long way in
# is not an identifier, it is payload that happens to contain a zero byte.
_MAX_IDENTIFIER = 80


@dataclass(frozen=True)
class Segment:
    """One region of the marker stream: a segment, a scan, or the trailer."""

    kind: str            # "marker", "entropy" or "trailer"
    start: int
    end: int
    code: Optional[int] = None
    name: str = ""
    payload: bytes = b""

    @property
    def size(self) -> int:
        return self.end - self.start

    @property
    def identifier(self) -> Optional[bytes]:
        """The leading NUL-terminated ASCII identifier of an APPn payload."""
        if self.code is None or not (0xE0 <= self.code <= 0xEF):
            return None
        return _identifier(self.payload)


def _identifier(payload: bytes) -> Optional[bytes]:
    index = payload.find(b"\x00")
    if index < 0 or index > _MAX_IDENTIFIER:
        return None
    candidate = payload[:index]
    try:
        candidate.decode("ascii")
    except UnicodeDecodeError:
        return None
    return candidate


def _scan_entropy(data: bytes, start: int) -> int:
    """
    Return the offset of the next real marker after entropy-coded scan data.

    Inside a scan an 0xFF byte is stuffed as `FF 00`, the only bare markers
    permitted are RST0..RST7 (`FF D0` through `FF D7`), and a run of 0xFF is
    legal fill. Everything else terminates the scan.
    """
    length = len(data)
    at = start
    while at < length - 1:
        if data[at] != 0xFF:
            at += 1
            continue
        following = data[at + 1]
        if following == 0xFF:
            at += 1          # fill; the next byte may still be the marker
            continue
        if following == 0x00 or 0xD0 <= following <= 0xD7:
            at += 2          # stuffed byte, or a restart marker
            continue
        return at
    raise EngineError(
        "entropy-coded data runs to the end of the file with no closing marker; "
        "this JPEG is truncated"
    )


def _walk(data: bytes) -> Iterator[Segment]:
    """
    Walk the marker stream, yielding every region exactly once and in order.

    Raises EngineError rather than guessing at anything it cannot parse. A file
    this cannot walk is one the engine must refuse, not one it may half-rewrite:
    "could not read" and "carries nothing" must never share a representation
    (CLAUDE.md traps 2 and 12).
    """
    length = len(data)
    if data[:2] != b"\xff\xd8":
        raise EngineError("not a JPEG: no SOI marker at offset 0")

    at = 0
    while at < length:
        if data[at] != 0xFF:
            raise EngineError(f"marker desync: expected 0xFF at offset {at}")
        # A run of 0xFF before a marker is legal fill and belongs to the
        # segment that follows it, so `start` is the beginning of the run. That
        # keeps a kept segment byte-identical to its input, fill included.
        cursor = at
        while cursor < length and data[cursor] == 0xFF:
            cursor += 1
        if cursor >= length:
            raise EngineError("file ends inside marker padding")
        marker = data[cursor]
        if marker == 0x00:
            raise EngineError(
                f"stuffed byte 0xFF00 outside entropy-coded data at offset {at}"
            )
        name = _NAMES.get(marker, "UNKNOWN_0x%02X" % marker)

        if marker in _STANDALONE:
            yield Segment("marker", at, cursor + 1, marker, name)
            at = cursor + 1
            if marker == EOI:
                # Point 2 in the module docstring. Everything past here is a
                # trailer: a motion photo's MP4, a Samsung SEF block, or
                # anything else somebody appended.
                if at < length:
                    yield Segment("trailer", at, length, None, "trailer")
                return
            continue

        if cursor + 3 > length:
            raise EngineError(f"truncated segment length field at offset {cursor}")
        declared = int.from_bytes(data[cursor + 1:cursor + 3], "big")
        if declared < 2:
            raise EngineError(
                f"{name} at offset {at} declares a length of {declared}, "
                "which is below the two bytes the length field itself occupies"
            )
        end = cursor + 1 + declared
        if end > length:
            raise EngineError(
                f"{name} at offset {at} declares {declared} bytes but only "
                f"{length - cursor - 1} remain; this JPEG is truncated"
            )
        yield Segment("marker", at, end, marker, name, data[cursor + 3:end])
        at = end

        if marker == SOS:
            # Progressive JPEGs have many SOS segments, so this is inside the
            # loop rather than a one-shot at the end of the header.
            scan_end = _scan_entropy(data, at)
            yield Segment("entropy", at, scan_end, None, "<scan>")
            at = scan_end

    raise EngineError("no EOI marker: this JPEG is truncated")


def _keep(segment: Segment) -> bool:
    """The section 2.1 keep-list, keyed on the identifier where there is one."""
    code = segment.code
    if code in _KEEP_IDENTIFIERS:
        return segment.payload.startswith(_KEEP_IDENTIFIERS[code])
    return code in _KEEP_CODES


def _describe(segment: Segment) -> str:
    identifier = segment.identifier
    if identifier is None and segment.code == COM:
        return f"COM comment marker ({segment.size} bytes)"
    if identifier is None:
        return f"{segment.name} marker ({segment.size} bytes)"
    note = _CARRIER_NOTES.get(identifier)
    label = f"{segment.name} '{identifier.decode('ascii', 'replace')}'"
    if note:
        label += f", carrying {note}"
    return f"{label} ({segment.size} bytes)"


def rebuild(data: bytes) -> Tuple[bytes, List[str]]:
    """
    Return (output bytes, descriptions of what was removed).

    Pure, and exported so a test can exercise it on bytes without touching the
    filesystem.
    """
    out = bytearray()
    removed: List[str] = []
    for segment in _walk(data):
        if segment.kind == "entropy":
            # Point 2 of the contract: the compressed scan is copied through,
            # never decoded and never re-encoded.
            out += data[segment.start:segment.end]
        elif segment.kind == "trailer":
            removed.append(
                f"trailing data after EOI ({segment.size} bytes); a motion "
                "photo trailer is a complete MP4 carrying its own GPS"
            )
        elif _keep(segment):
            out += data[segment.start:segment.end]
        else:
            removed.append(_describe(segment))
    return bytes(out), removed


def _postcondition(out: bytes) -> None:
    """
    Re-walk the output and refuse to ship one that still breaks the rules.

    This is a POSTCONDITION, not verification. It is the writer checking its
    own arithmetic with the writer's own parser, which is exactly the circular
    check section 3.1 says proves nothing on its own. Real verification is the
    independent structural walk in tests/test_jpeg_engine.py plus the exiftool
    oracle. This is here because it is nearly free and it fails closed on an
    internal bug rather than writing the file anyway.
    """
    _, still_removable = rebuild(out)
    if still_removable:
        raise EngineError(
            "internal error: the rebuilt JPEG still contains "
            + "; ".join(still_removable)
        )
    if not out.endswith(b"\xff\xd9"):
        raise EngineError("internal error: the rebuilt JPEG does not end at EOI")


class JpegEngine(BaseEngine):
    name = "jpeg"
    supports_selective = False

    def available(self) -> Tuple[bool, str]:
        # Pure Python and the standard library. There is nothing to be missing,
        # which is the entire point of the mobile build.
        return True, ""

    def strip_all(self, path: str) -> List[str]:
        self.require()
        try:
            with open(path, "rb") as handle:
                data = handle.read()
        except OSError as exc:
            raise EngineError(f"could not read {path}: {exc}") from exc

        out, removed = rebuild(data)
        _postcondition(out)

        if out == data:
            # Nothing outside the keep-list was present. Writing a
            # byte-identical file would still touch the inode for no reason.
            return ["jpeg marker stream walked; nothing outside the keep-list "
                    "was present"]

        tmp = temp_beside(path, ".jpg")
        try:
            with open(tmp, "wb") as handle:
                handle.write(out)
        except OSError as exc:
            raise EngineError(f"could not write the rebuilt JPEG: {exc}") from exc
        atomic_replace(tmp, path)
        return removed
