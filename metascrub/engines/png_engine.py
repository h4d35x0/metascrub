"""
PNG engine: pure-Python chunk surgery. No exiftool, no ffmpeg, no Pillow,
because none of the three can run on a phone.

Phase 1 of the Android media build. See docs/ANDROID-MEDIA-BUILD.md section 2.2
for the measured removal table this implements, and section 3.2 for the
structural assertions that verify it.

WHAT THIS ENGINE DOES

A PNG is a signature followed by a flat sequence of chunks, each one
`length(4) type(4) payload(length) crc(4)`. Metadata lives in whole chunks, so
removing it is deletion of complete chunks and nothing else. The surviving
chunks are copied through BYTE FOR BYTE, which means:

  - IDAT is never touched, so the compressed image data is never re-encoded and
    the decoded pixels cannot change. `test_png_engine.py` asserts that with a
    Pillow decode of the input and the output.
  - a kept chunk keeps the CRC it arrived with, and that CRC is verified before
    it is written, so every chunk in the output has a CRC that is correct for
    its own bytes.

WHY IT DOES NOT SHARE CODE WITH structure.py

`metascrub/structure.py` also walks PNG chunks, and it deliberately answers a
different question: "is any region of this file unaccounted for". It reports and
never removes. This engine removes. Keeping the two implementations separate is
the same rule test_av_muxer applies when it refuses to import the engine's own
`ftyp_brand`: a checker that shares a parser with the thing it checks cannot
catch a parser bug. The engine, structure.py and the test file each walk the
chunk stream independently, on purpose.

DELIBERATELY NOT WIRED INTO CAPABILITIES

The `.png` row still routes to the exiftool engine, which works on the desktop
and shipped in 1.0.2. Flipping the default is a separate decision with its own
evidence. `available()` reports True because the engine is real now: `doctor`
must not describe a working engine as missing.
"""

from __future__ import annotations

import os
import struct
import zlib
from typing import List, Tuple

from .base import BaseEngine, EngineError, atomic_replace, temp_beside

SIGNATURE = b"\x89PNG\r\n\x1a\n"

# The keep-list of docs/ANDROID-MEDIA-BUILD.md section 2.2, verbatim. Every
# chunk type NOT in this set is removed, including any unknown ancillary chunk.
#
# Two entries carry a caveat that has to be stated rather than assumed:
#
#   pHYs is KEPT for correct display, and it carries the source pixel density.
#   Measured 2026-09-06 (scratchpad phase0-findings-jpeg-png-webp.md, Q7): a
#   rich.png carried PixelsPerUnitX 5669, which is 144 dpi, a producer
#   signature on its own. Dropping it changed no decoded pixel under either
#   Pillow or ffmpeg but lost `dpi` from im.info, i.e. it degrades physical
#   sizing. Section 2.2 keeps it. This engine keeps it. It is a known, named,
#   accepted residue and NOT an oversight.
#
#   gAMA + cHRM + sBIT + bKGD present together with specific values are
#   themselves a producer fingerprint (same measurement, note (c)). Kept for the
#   same reason: they describe how to render the pixels correctly.
KEEP_CHUNKS = frozenset({
    b"IHDR", b"PLTE", b"IDAT", b"IEND",
    b"tRNS", b"sRGB", b"gAMA", b"cHRM", b"sBIT", b"bKGD", b"pHYs",
    b"acTL", b"fcTL", b"fdAT",
})

# Named carriers from section 2.2, used only to describe a removal to the user.
# Removal is driven by KEEP_CHUNKS and never by this table: a carrier that is
# not listed here is still removed, it is just described generically. That
# ordering is the point. A removal driven by a list of known-bad names is
# exactly the fail-open that let an unknown chunk through in 1.0.1.
_CARRIER_NOTES = {
    b"tEXt": "uncompressed text chunk",
    b"zTXt": "zlib-compressed text chunk",
    b"iTXt": "international text chunk (XMP lives here)",
    b"eXIf": "EXIF block",
    b"tIME": "last-modification time",
    b"dSIG": "digital signature",
    b"caBX": "C2PA content credentials",
    b"iCCP": "embedded ICC colour profile",
    b"sPLT": "suggested palette",
    b"hIST": "palette histogram",
    b"cICP": "coding-independent code points",
    b"mDCv": "mastering display colour volume",
    b"cLLi": "content light level",
}

# Chunks whose removal a user can actually SEE, so it is reported rather than
# swallowed. Same principle as /PieceInfo in pdf_engine.py: a removal that
# changes what the file looks like must not be silent.
_RENDERING_AFFECTING = {
    b"iCCP": ("NOTE: removed the embedded ICC colour profile (iCCP); colour-"
              "managed renderers will fall back to sRGB"),
    b"cICP": ("NOTE: removed the coding-independent code points (cICP); HDR "
              "colour signalling is lost"),
    b"mDCv": ("NOTE: removed the mastering display colour volume (mDCv); HDR "
              "tone mapping information is lost"),
    b"cLLi": ("NOTE: removed the content light level (cLLi); HDR tone mapping "
              "information is lost"),
}

# iCCP, DECIDED EXPLICITLY, because section 2.2 says it must be and because it
# was missing from both of that section's lists.
#
# Removed unconditionally, matching the JPEG APP2 decision. Two measurements
# from scratchpad phase0-findings-jpeg-png-webp.md, Q7 note (b), 2026-09-06:
#
#   1. An ICC profile IS a metadata carrier in its own right. exiftool reads
#      `ProfileDateTime : 2026:09:06 13:09:29` and `ProfileCMMType : Little CMS`
#      straight out of an embedded sRGB profile. That is a wall-clock timestamp
#      and a producing-library name, which is precisely what this tool exists
#      to remove.
#   2. The "keep it when it hashes to a known standard profile" rule does not
#      survive contact with real profiles. Two sRGB profiles generated 1.2
#      seconds apart differed at exactly one byte, offset 35, the seconds field
#      of the header date. A hash allowlist rejects genuine sRGB profiles for
#      carrying a clock, so it would either fail open (loosen until it matches)
#      or fail useless (never match).
#
# The honest consequence, stated rather than hidden: a wide-gamut PNG (a Display
# P3 Android screenshot, for instance) loses its colour management and will be
# rendered as sRGB. `sRGB`, `gAMA` and `cHRM` are kept, so a profile that only
# said "this is sRGB" loses nothing that matters.
_ICCP_IS_REMOVED_ON_PURPOSE = True


def _chunk_is_ancillary(ctype: bytes) -> bool:
    """PNG spec: bit 5 of the first byte, i.e. a lowercase first letter."""
    return bool(ctype[0] & 0x20)


class PngEngine(BaseEngine):
    name = "png"
    supports_selective = False

    def available(self) -> Tuple[bool, str]:
        # struct and zlib are stdlib. There is nothing to be unavailable.
        return True, ""

    def strip_all(self, path: str) -> List[str]:
        self.require()
        with open(path, "rb") as handle:
            data = handle.read()

        rebuilt, targeted = self._rebuild(data, path)

        tmp = temp_beside(path, ".png")
        try:
            with open(tmp, "wb") as handle:
                handle.write(rebuilt)
        except Exception as exc:
            # Never leave the temporary file behind, the same guarantee
            # atomic_replace makes for its own failures.
            try:
                os.unlink(tmp)
            except OSError:
                pass
            raise EngineError(f"could not write the rebuilt PNG: {exc}") from exc

        atomic_replace(tmp, path)
        return targeted or ["png chunk sweep (no metadata chunk was present)"]

    # The whole engine, separated from the file handling so that it can be
    # exercised on bytes.
    def _rebuild(self, data: bytes, path: str = "<bytes>") -> Tuple[bytes, List[str]]:
        if not data.startswith(SIGNATURE):
            raise EngineError(f"{path} is not a PNG: signature mismatch")

        out = bytearray(SIGNATURE)
        targeted: List[str] = []
        offset = len(SIGNATURE)
        total = len(data)
        saw_ihdr = False
        saw_iend = False

        while offset + 8 <= total:
            (length,) = struct.unpack(">I", data[offset:offset + 4])
            ctype = data[offset + 4:offset + 8]
            end = offset + 12 + length          # 4 length + 4 type + payload + 4 crc
            if end > total:
                # Fail closed. A chunk that runs off the end of the file means
                # this is truncated or is not the file we think it is, and a
                # "best effort" rewrite of a damaged PNG would hand the user a
                # plausible-looking file built from a guess.
                raise EngineError(
                    f"{path} is malformed: chunk "
                    f"{ctype.decode('latin-1', 'replace')!r} at offset {offset} "
                    f"declares {length} bytes, which runs past the end of the file"
                )

            if not saw_ihdr:
                if ctype != b"IHDR":
                    raise EngineError(
                        f"{path} is malformed: the first chunk is "
                        f"{ctype.decode('latin-1', 'replace')!r}, not IHDR"
                    )
                saw_ihdr = True

            keep = ctype in KEEP_CHUNKS

            if not keep and not _chunk_is_ancillary(ctype):
                # An unknown CRITICAL chunk. The PNG spec says a decoder that
                # does not recognise a critical chunk must not proceed, so this
                # is not something a metadata tool may quietly delete: it could
                # be image data. Refuse the file instead of shipping a
                # confidently broken one.
                raise EngineError(
                    f"{path} carries an unrecognised CRITICAL chunk "
                    f"{ctype.decode('latin-1', 'replace')!r} ({length} bytes) at "
                    f"offset {offset}. Removing it could destroy image data and "
                    f"keeping it could keep a carrier, so this file is refused "
                    f"rather than guessed at."
                )

            if keep:
                stored_crc = data[end - 4:end]
                computed_crc = struct.pack(
                    ">I", zlib.crc32(data[offset + 4:end - 4]) & 0xFFFFFFFF
                )
                if stored_crc != computed_crc:
                    # Recomputing here would turn a damaged file into a
                    # plausible one and call it sanitized. Fail closed.
                    raise EngineError(
                        f"{path} is damaged: chunk "
                        f"{ctype.decode('latin-1', 'replace')!r} at offset "
                        f"{offset} has a bad CRC (stored {stored_crc.hex()}, "
                        f"computed {computed_crc.hex()}). This engine will not "
                        f"rewrite a corrupt file into one that looks intact."
                    )
                # Byte-for-byte, CRC included. Nothing is re-encoded, so the
                # decoded pixels cannot move.
                out += data[offset:end]
            else:
                note = _RENDERING_AFFECTING.get(ctype)
                if note:
                    targeted.append(note)
                described = _CARRIER_NOTES.get(ctype)
                if described is None:
                    described = "unknown ancillary chunk"
                targeted.append(
                    f"{ctype.decode('latin-1', 'replace')} chunk, {described} "
                    f"({length} bytes)"
                )

            offset = end
            if ctype == b"IEND":
                saw_iend = True
                break

        if not saw_iend:
            raise EngineError(
                f"{path} is malformed: no IEND chunk, so the file is truncated"
            )

        if offset < total:
            # Everything past IEND's CRC. Measured 2026-09-06: a whole MP4
            # concatenated onto a PNG survives every read exiftool does apart
            # from one [minor] warning, and Pillow opens the image without
            # complaint. Section 2.2's trailing-data rule, and the PNG sibling
            # of the JPEG post-EOI case.
            targeted.append(f"trailing data after IEND ({total - offset} bytes)")

        return bytes(out), targeted
