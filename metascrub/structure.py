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


# ---------------------------------------------------------------- dispatch

_WALKERS: Dict[str, Callable[[bytes], List[str]]] = {
    ".png": _walk_png,
    ".webp": _walk_webp,
    ".gif": _walk_gif,
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
