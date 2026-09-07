"""
WebP engine: pure-Python RIFF container surgery. No exiftool, no ffmpeg,
because neither can run on a phone.

Phase 1 of the Android media build. See docs/ANDROID-MEDIA-BUILD.md section 2.3
for the measured removal table and section 3.2 for the structural assertions
that verify it.

This engine is deliberately NOT wired into CAPABILITIES. The .webp row still
routes to the exiftool engine, which works on the desktop and shipped in 1.0.2.
Flipping the default is a separate decision.

WHAT THIS REMOVES, AND WHY EACH ONE IS HERE

  EXIF, XMP , ICCP   the three metadata chunks. Measured 2026-09-06: exiftool
                     writing EXIF/GPS/XMP/ICC into a WebP produces exactly
                     these three plus a synthesised VP8X header.

  any unknown chunk  THE LOAD-BEARING CLAUSE, and the reason this engine
                     exists. Measured against the shipped 1.0.1: a WebP
                     carrying an 8 KB MP4 in an unknown `MPVD` chunk INSIDE the
                     declared RIFF size was reported "SANITIZED ... verified
                     clean" while the video and its `ftyp` survived intact.
                     1.0.2 fixed the REPORTING, through structure.py. This is
                     the code that actually removes it.

  unknown ANMF       the same carrier one level down. An animation frame is a
  sub-chunks         container of its own, and nothing above walks into it.

  trailing bytes     everything past the declared RIFF size. Measured: silent
                     on a normal exiftool read, so no needle is ever generated
                     for it and the residual scan cannot see it. Removed AND
                     reported loudly, with the byte count, because a user whose
                     photo was carrying a second file should be told so rather
                     than quietly handed a smaller one. See
                     _TRAILING_IS_REPORTED_LOUDLY for why this engine removes
                     what structure.py can only refuse.

  VP8X, where the    a WebP with no VP8X chunk cannot carry metadata at all:
  file does not      the simple form is RIFF/WEBP plus exactly one VP8 or VP8L
  need it            chunk, and there is nowhere to put anything else. Dropping
                     the header is therefore strictly stronger than reconciling
                     its flags, and it retires the whole question rather than
                     policing it.

  VP8X reserved      byte 0 bits 0x80, 0x40 and 0x01, plus the whole 24-bit
  bits, where VP8X   reserved field in bytes 1..3. The specification says they
  stays              are zero; 27 bits that nothing reads is a covert channel.

THE VP8X TRAP IS POINTED THE OTHER WAY, AND THIS MATTERS

The obvious worry is removing a chunk while leaving its VP8X flag bit set.
Measured 2026-09-06 across four variants: it breaks nothing. Pillow/libwebp,
exiftool 13.29 and ffmpeg all produced byte-identical pixels with no warning.

The dangerous direction is the opposite, and it is a fail-open. With the VP8X
flag byte cleared to 0x00 and the ICCP/EXIF/XMP chunks still present, Pillow
reports no exif, no xmp and no icc_profile while exiftool reads all three out
of the same bytes. So this engine decides what to remove from the CHUNK
INVENTORY and never from the flag byte, which is a field an attacker controls.
Flags are rewritten only as a consequence of what was actually removed.

For the same reason there is no "the flags agree with the chunks present"
assertion anywhere in this engine or its tests. Measured: that assertion fails
on a valid Pillow-written animated WebP, where the alpha bit is set while alpha
lives inside the ANMF frames with no top-level ALPH chunk.

WHERE IT FAILS CLOSED

A chunk whose declared size runs past the end of the container, a VP8X that is
not 10 bytes, a RIFF that declares more bytes than the file holds, an ANMF
payload whose sub-chunk chain does not add up: all of these raise EngineError.
A container this engine cannot fully account for is one it must not claim to
have cleaned. Truncating to what parsed would turn a corrupt input into a
confident-looking output, which is the failure this project exists to refuse.
"""

from __future__ import annotations

import struct
from typing import List, Optional, Tuple

from .base import BaseEngine, EngineError, atomic_replace, temp_beside

# Top-level chunks that carry image data and are kept verbatim. VP8X is NOT in
# here: it is the extended header and gets its own decision below.
KEEP = frozenset({b"VP8 ", b"VP8L", b"ALPH", b"ANIM", b"ANMF"})

# The three named metadata chunks. Removing an unknown chunk and removing one of
# these is the same operation; they are listed separately only so the report can
# say which carrier the user just lost.
METADATA_CHUNKS = frozenset({b"EXIF", b"XMP ", b"ICCP"})

VP8X = b"VP8X"
VP8X_PAYLOAD_LEN = 10

# Sub-chunks that may appear inside an ANMF animation frame. Anything else in
# there is an unknown chunk one level down and is removed for the same reason.
ANMF_KEEP = frozenset({b"ALPH", b"VP8 ", b"VP8L"})
ANMF_HEADER_LEN = 16

# VP8X byte 0, MSB first: Rsv Rsv ICC Alpha Exif XMP Anim Rsv
FLAG_ICC = 0x20
FLAG_ALPHA = 0x10
FLAG_EXIF = 0x08
FLAG_XMP = 0x04
FLAG_ANIM = 0x02
# The metadata bits. Cleared whenever VP8X survives, because the chunks they
# describe never do.
FLAGS_METADATA = FLAG_ICC | FLAG_EXIF | FLAG_XMP
# Bits the specification defines as reserved and requires to be zero.
FLAGS_RESERVED = 0x80 | 0x40 | 0x01

VP8_START_CODE = b"\x9d\x01\x2a"
VP8L_SIGNATURE = 0x2F

# TRAILING DATA IS REMOVED HERE AND REPORTED LOUDLY, AND THE TWO ARE SEPARATE
# DECISIONS.
#
# docs/ANDROID-MEDIA-BUILD.md section 2.3 describes the SHIPPED tool failing
# closed on bytes past the declared RIFF size and calls that correct. It is
# correct there and it is not correct here, because the two are answering
# different questions. metascrub/structure.py is a REPORTER: it can see that a
# region is unaccounted for and it cannot clean it, so refusing is the whole of
# what it can honestly do. This engine rebuilds the container from its chunk
# inventory, so the trailer is gone by construction and the image bytes are
# byte-identical either way. Refusing to do a thing you can provably do, and
# handing the user back their file with the payload still in it, is the worse
# of the two outcomes.
#
# What must NOT be lost in that trade is the telling. A silent removal of a
# smuggled 8 KB MP4 leaves the user believing they had an ordinary photo. They
# did not, and that is a fact about their file worth more than the cleaning is.
# So this is surfaced the way png_engine.py surfaces its iCCP removal: a NOTE
# line naming the size and saying in words that nothing in the container
# accounted for it.
#
# The size is in the message on purpose. "Trailing data was removed" is one
# byte of padding or a whole video, and the user cannot tell which without it.
_TRAILING_IS_REPORTED_LOUDLY = True


def _trailing_note(trailing: int, declared_total: int) -> str:
    """The one line a user gets about a hidden payload. Keep the size in it."""
    return (
        f"NOTE: removed {trailing} bytes of trailing data past the declared "
        f"RIFF size. The container declares this WebP is {declared_total} "
        f"bytes and the file held {declared_total + trailing}; nothing in the "
        f"container accounted for the difference. Data hidden there is not "
        f"reported by an exiftool read, so no residual scan can generate a "
        f"needle for it. A trailer this shape is how a complete second file, "
        f"with its own metadata and its own GPS, rides along inside a photo."
    )


class Chunk:
    """One top-level RIFF chunk, with its payload already unpadded."""

    __slots__ = ("fourcc", "payload", "offset")

    def __init__(self, fourcc: bytes, payload: bytes, offset: int):
        self.fourcc = fourcc
        self.payload = payload
        self.offset = offset

    @property
    def name(self) -> str:
        return self.fourcc.decode("latin-1")


def _fourcc_repr(fourcc: bytes) -> str:
    """A FourCC safe to put in a message, whatever bytes it actually holds."""
    text = "".join(chr(b) if 32 <= b < 127 else "." for b in fourcc)
    return f"{text!r}"


def parse(data: bytes) -> Tuple[List[Chunk], int]:
    """
    Walk a WebP into its top-level chunks.

    Returns (chunks, trailing), where `trailing` counts the bytes past the
    declared RIFF size. The declared size is authoritative: a walker that reads
    to end-of-file instead reads whatever a trailer says its length is, and a
    measured MP4 trailer produced a 1.8 GB chunk length that way.
    """
    if len(data) < 12 or data[0:4] != b"RIFF" or data[8:12] != b"WEBP":
        raise EngineError("not a WebP: RIFF/WEBP header mismatch")

    (declared,) = struct.unpack("<I", data[4:8])
    total = declared + 8
    if total > len(data):
        raise EngineError(
            f"WebP declares {total} bytes but the file holds {len(data)}; "
            "the container is truncated and will not be rewritten"
        )
    trailing = len(data) - total

    chunks: List[Chunk] = []
    off = 12
    while off + 8 <= total:
        fourcc = data[off:off + 4]
        (size,) = struct.unpack("<I", data[off + 4:off + 8])
        end = off + 8 + size
        if end > total:
            raise EngineError(
                f"WebP chunk {_fourcc_repr(fourcc)} at offset {off} declares "
                f"{size} bytes, which runs past the end of the RIFF container"
            )
        chunks.append(Chunk(fourcc, data[off:off + 8 + size][8:], off))
        off = end + (size & 1)          # RIFF chunks are padded to even length

    if off < total:
        # Fewer than 8 bytes left: not a chunk header, so nothing accounts for
        # them. They are dropped by the rebuild; the caller reports them.
        raise EngineError(
            f"{total - off} bytes inside the declared RIFF size at offset "
            f"{off} are too short to be a chunk header"
        )
    return chunks, trailing


def _build(chunks: List[Chunk]) -> bytes:
    """Reassemble a WebP, recomputing the RIFF size and the even padding."""
    body = bytearray()
    for chunk in chunks:
        body += chunk.fourcc
        body += struct.pack("<I", len(chunk.payload))
        body += chunk.payload
        if len(chunk.payload) & 1:
            body += b"\x00"
    return b"RIFF" + struct.pack("<I", len(body) + 4) + b"WEBP" + bytes(body)


def image_dimensions(chunk: Chunk) -> Optional[Tuple[int, int]]:
    """
    (width, height) from a VP8 or VP8L bitstream header, or None if this engine
    cannot read them. None is a refusal to guess, and the caller treats it that
    way: it keeps VP8X rather than dropping a header it cannot prove redundant.
    """
    payload = chunk.payload
    if chunk.fourcc == b"VP8 ":
        if len(payload) < 10:
            return None
        tag = payload[0] | (payload[1] << 8) | (payload[2] << 16)
        if tag & 1:                       # interframe, not a keyframe
            return None
        if payload[3:6] != VP8_START_CODE:
            return None
        width = struct.unpack("<H", payload[6:8])[0] & 0x3FFF
        height = struct.unpack("<H", payload[8:10])[0] & 0x3FFF
        return width, height
    if chunk.fourcc == b"VP8L":
        if len(payload) < 5 or payload[0] != VP8L_SIGNATURE:
            return None
        (bits,) = struct.unpack("<I", payload[1:5])
        return (bits & 0x3FFF) + 1, ((bits >> 14) & 0x3FFF) + 1
    return None


def vp8x_canvas(payload: bytes) -> Tuple[int, int]:
    """Canvas size a VP8X header declares. Two 24-bit little-endian minus-ones."""
    width = int.from_bytes(payload[4:7], "little") + 1
    height = int.from_bytes(payload[7:10], "little") + 1
    return width, height


def _clean_anmf(payload: bytes, frame_index: int, targeted: List[str]) -> bytes:
    """
    Rebuild one animation frame, dropping any sub-chunk that is not image data.

    An ANMF payload is a 16-byte frame header followed by its own RIFF-style
    sub-chunk chain, which nothing above this engine walks into. That makes it
    the same hiding place as a top-level unknown chunk, one level down.

    A payload whose sub-chunk chain does not add up raises rather than being
    passed through: an animation frame this engine cannot account for is not one
    it can honestly say it cleaned.
    """
    if len(payload) < ANMF_HEADER_LEN:
        raise EngineError(
            f"ANMF frame {frame_index} is {len(payload)} bytes, shorter than "
            "its 16-byte frame header"
        )
    kept = bytearray(payload[:ANMF_HEADER_LEN])
    dropped = False
    off = ANMF_HEADER_LEN
    while off + 8 <= len(payload):
        fourcc = payload[off:off + 4]
        (size,) = struct.unpack("<I", payload[off + 4:off + 8])
        end = off + 8 + size
        if end > len(payload):
            raise EngineError(
                f"ANMF frame {frame_index} sub-chunk {_fourcc_repr(fourcc)} "
                f"declares {size} bytes, which runs past the end of the frame"
            )
        if fourcc in ANMF_KEEP:
            kept += payload[off:end]
            if size & 1:
                kept += b"\x00"
        else:
            dropped = True
            targeted.append(
                f"unknown ANMF sub-chunk {_fourcc_repr(fourcc)} ({size} bytes) "
                f"in animation frame {frame_index}"
            )
        off = end + (size & 1)
    if off != len(payload):
        raise EngineError(
            f"ANMF frame {frame_index} has {len(payload) - off} trailing bytes "
            "its sub-chunk chain does not account for"
        )
    # Byte-identical when nothing was dropped, which keeps the no-re-encode
    # promise literal rather than approximate.
    return payload if not dropped else bytes(kept)


def scrub_bytes(data: bytes) -> Tuple[bytes, List[str]]:
    """
    The whole removal, as a pure function over bytes.

    Split out from strip_all so that the decision logic can be measured without
    a filesystem, and so a caller on a phone can use it directly.
    """
    chunks, trailing = parse(data)
    targeted: List[str] = []
    if trailing:
        # REPORTED LOUDLY, not just removed. See _TRAILING_IS_REPORTED_LOUDLY.
        targeted.append(_trailing_note(trailing, len(data) - trailing))

    kept: List[Chunk] = []
    vp8x: Optional[Chunk] = None
    frame_index = 0
    for chunk in chunks:
        if chunk.fourcc == VP8X:
            if len(chunk.payload) != VP8X_PAYLOAD_LEN:
                raise EngineError(
                    f"VP8X header is {len(chunk.payload)} bytes, not "
                    f"{VP8X_PAYLOAD_LEN}; this is not a container this engine "
                    "will rewrite"
                )
            vp8x = chunk
            kept.append(chunk)
            continue
        if chunk.fourcc in KEEP:
            if chunk.fourcc == b"ANMF":
                frame_index += 1
                chunk.payload = _clean_anmf(chunk.payload, frame_index, targeted)
            kept.append(chunk)
            continue
        if chunk.fourcc in METADATA_CHUNKS:
            targeted.append(f"{chunk.name} chunk ({len(chunk.payload)} bytes)")
        else:
            targeted.append(
                f"unknown RIFF chunk {_fourcc_repr(chunk.fourcc)} "
                f"({len(chunk.payload)} bytes)"
            )

    if vp8x is not None:
        image = [c for c in kept if c.fourcc in (b"VP8 ", b"VP8L")]
        needs = [c for c in kept if c.fourcc in (b"ALPH", b"ANIM", b"ANMF")]
        dims = image_dimensions(image[0]) if len(image) == 1 else None
        # Drop the header only when the file demonstrably does not need it AND
        # the bitstream agrees with the canvas it declares. A VP8X whose canvas
        # differs from the image is doing real work (scaled or cropped canvas),
        # and dropping it would change what the file renders as.
        if not needs and dims is not None and dims == vp8x_canvas(vp8x.payload):
            kept = [c for c in kept if c is not vp8x]
            targeted.append(
                "VP8X extended header (a WebP without it cannot carry "
                "metadata at all)"
            )
        else:
            flags = vp8x.payload[0]
            new_flags = flags & ~(FLAGS_METADATA | FLAGS_RESERVED) & 0xFF
            reserved = vp8x.payload[1:4]
            if new_flags != flags:
                targeted.append(
                    f"VP8X flag bits 0x{flags:02X} -> 0x{new_flags:02X}"
                )
            if reserved != b"\x00\x00\x00":
                targeted.append("VP8X 24-bit reserved field")
            vp8x.payload = (
                bytes([new_flags]) + b"\x00\x00\x00" + vp8x.payload[4:]
            )

    return _build(kept), targeted


class WebpEngine(BaseEngine):
    name = "webp"
    supports_selective = False

    def available(self) -> Tuple[bool, str]:
        return True, ""

    def strip_all(self, path: str) -> List[str]:
        self.require()
        try:
            with open(path, "rb") as handle:
                data = handle.read()
        except OSError as exc:
            raise EngineError(f"could not read WebP: {exc}") from exc

        rebuilt, targeted = scrub_bytes(data)

        tmp = temp_beside(path, ".webp")
        try:
            with open(tmp, "wb") as handle:
                handle.write(rebuilt)
        except OSError as exc:
            raise EngineError(f"could not write rebuilt WebP: {exc}") from exc
        atomic_replace(tmp, path)
        return targeted or ["webp container rewrite"]
