"""
Audio and video engine: ffmpeg remux.

exiftool can read these containers but is the wrong tool for removing metadata
from them. ffmpeg rebuilds the container, which drops the old atom or element
tree rather than patching around it.

The remux is a stream copy (`-c copy`), so nothing is re-encoded: no quality
loss and it runs at IO speed rather than encode speed. This matters for the
obvious use case, which is a phone video that should stop carrying the GPS
coordinates of the house it was shot in.

Three metadata scopes have to be named separately, because clearing one leaves
the others intact:
  -map_metadata -1      container-level tags
  -map_metadata:s -1    per-stream tags, where creation_time usually hides
  -map_chapters -1      chapter markers and their titles

For a few extensions the output muxer has to be named explicitly. ffmpeg picks
the muxer from the output filename, and this engine writes to a temp file
carrying the SAME extension as the input, so for any extension ffmpeg has no
muxer bound to, the remux dies before it starts:

    Unable to choose an output format for 'fixture.qt'
    Error opening output files: Invalid argument

That is a container ffmpeg reads and writes perfectly well, refused only
because of how the output was named. Everything ffmpeg can infer is still left
to it: the inference is right for the common cases, and a map that had to list
every extension would go stale.

WHICH muxer is decided by the input, not by the extension. That distinction is
the whole design of this part, and it was measured rather than assumed.
`.qt`, `.mqv`, `.lrv` and `.f4a` all name ISO base media files, but two
incompatible flavours of them: the QuickTime flavour, whose ftyp major brand is
`qt  `, and the ISO/MP4 flavour. Extension and flavour do not agree in the
wild; a `.lrv` proxy clip is written both ways depending on the camera.

A static extension-to-muxer map therefore silently CONVERTS half the files it
touches. Measured 2026-09-04 across the eight combinations of the four
extensions and the two flavours: with a static map, four of the eight came out
of the remux with a different ftyp brand than they went in with. The user asked
for metadata to be removed and would have got a different container back. This
engine reads the input's brand and matches it, so all eight round trip in their
own flavour, and MUXER_FOR_EXT is only the fallback for an input with no ftyp
box at all.

The one thing that is NOT preserved is the exact brand WITHIN the ISO flavour:
ffmpeg's mp4 muxer writes `isom` whatever it read, so an `mp41` input comes
back `isom`. Measured, and unchanged by anything here; it is what every
MP4-family format this tool already supports has always done.
"""

from __future__ import annotations

import os
import shutil
import subprocess
from typing import List, Optional, Tuple

from .base import BaseEngine, EngineError, atomic_replace, temp_beside

# A remux is IO bound, but a large file on slow storage still takes time. This
# bound exists so a wedged ffmpeg cannot hang a batch run indefinitely.
_TIMEOUT_SECONDS = 900

# Extensions ffmpeg binds no output muxer to, and the flavour each one usually
# means. Measured 2026-09-04 on ffmpeg 2024-12-11-git-a518b5540d: writing to a
# path with any of these extensions and no -f fails with "Unable to choose an
# output format", and succeeds with it.
#
#   .qt  .mqv   QuickTime. .mqv is Sony's movie variant.
#   .lrv .f4a   ISO/MP4. .lrv is the GoPro and DJI low resolution proxy clip,
#               .f4a the Flash audio container.
#
# This is only the FALLBACK. The flavour is read out of the file itself
# whenever the file says, because the extension is a weak signal: the same
# .lrv extension is written in both flavours by different cameras. See the
# module docstring for the measurement.
#
# Keyed lowercase; lookups fold case. A missing key is not an error, it means
# "let ffmpeg infer", which is correct for .mp4, .mkv and the rest.
MUXER_FOR_EXT = {
    ".qt": "mov",
    ".mqv": "mov",
    ".lrv": "mp4",
    ".f4a": "mp4",
}

# The ftyp major brand that marks the QuickTime flavour of ISO base media.
# Anything else in an ftyp box is the ISO/MP4 flavour as far as this choice
# goes, which is the only distinction the two muxers make.
QUICKTIME_BRAND = b"qt  "


def ftyp_brand(path: str) -> Optional[bytes]:
    """
    The four byte major brand from the file type box, or None.

    None means "this file does not say", not "this file is ISO". The two must
    stay distinguishable: the caller falls back to the extension for the first
    and would make a wrong choice if a read failure looked like an answer.
    """
    try:
        with open(path, "rb") as fh:
            head = fh.read(12)
    except OSError:
        return None
    if len(head) < 12 or head[4:8] != b"ftyp":
        return None
    return head[8:12]


def output_format_args(path: str, ext: str) -> List[str]:
    """
    Return the ["-f", muxer] ffmpeg needs to rewrite this file, or [].

    Empty for every extension ffmpeg can infer, which is almost all of them.

    Split out as a function because BOTH command lines in this engine have to
    carry it. A retry path that quietly dropped the flag would fail only on
    files that already needed a retry, which is the hardest possible place to
    notice a missing argument.
    """
    fallback = MUXER_FOR_EXT.get((ext or "").lower())
    if not fallback:
        return []
    brand = ftyp_brand(path)
    if brand is None:
        return ["-f", fallback]
    return ["-f", "mov" if brand == QUICKTIME_BRAND else "mp4"]


class AvEngine(BaseEngine):
    name = "av"
    supports_selective = False

    def __init__(self) -> None:
        self._ffmpeg = shutil.which("ffmpeg")

    def available(self) -> Tuple[bool, str]:
        if not self._ffmpeg:
            return False, "ffmpeg not found on PATH"
        return True, ""

    def strip_all(self, path: str) -> List[str]:
        self.require()
        ext = os.path.splitext(path)[1] or ".mp4"
        tmp = temp_beside(path, ext)

        cmd = [
            self._ffmpeg, "-y",
            "-loglevel", "error",
            "-i", path,
            "-map", "0",              # keep every stream, do not let ffmpeg pick
            "-map_metadata", "-1",    # container tags
            "-map_metadata:s", "-1",  # per-stream tags, including creation_time
            "-map_chapters", "-1",    # chapter markers
            "-c", "copy",             # remux, never re-encode
            "-bitexact",              # suppress the encoder/version stamp
        ] + output_format_args(path, ext) + [
            tmp,                      # -f, where needed, must precede this
        ]

        try:
            proc = subprocess.run(
                cmd, capture_output=True, text=True, timeout=_TIMEOUT_SECONDS
            )
        except subprocess.TimeoutExpired as exc:
            self._cleanup(tmp)
            raise EngineError(f"ffmpeg timed out after {_TIMEOUT_SECONDS}s") from exc
        except Exception as exc:
            self._cleanup(tmp)
            raise EngineError(f"ffmpeg could not be run: {exc}") from exc

        if proc.returncode != 0 or not os.path.exists(tmp):
            detail = (proc.stderr or "").strip().splitlines()
            tail = detail[-1] if detail else f"exit code {proc.returncode}"
            self._cleanup(tmp)
            # -bitexact is rejected by a few muxers. Retry without it before
            # giving up, rather than failing a file that is otherwise fine.
            return self._retry_without_bitexact(path, ext, tail)

        if os.path.getsize(tmp) == 0:
            self._cleanup(tmp)
            raise EngineError("ffmpeg produced an empty file")

        atomic_replace(tmp, path)
        return ["container metadata", "per-stream metadata", "chapters"]

    def _retry_without_bitexact(self, path: str, ext: str, first_error: str) -> List[str]:
        tmp = temp_beside(path, ext)
        cmd = [
            self._ffmpeg, "-y", "-loglevel", "error", "-i", path,
            "-map", "0", "-map_metadata", "-1", "-map_metadata:s", "-1",
            "-map_chapters", "-1", "-c", "copy",
        ] + output_format_args(path, ext) + [tmp]
        try:
            proc = subprocess.run(
                cmd, capture_output=True, text=True, timeout=_TIMEOUT_SECONDS
            )
        except Exception as exc:
            self._cleanup(tmp)
            raise EngineError(f"ffmpeg remux failed: {first_error}") from exc

        if proc.returncode != 0 or not os.path.exists(tmp) or os.path.getsize(tmp) == 0:
            self._cleanup(tmp)
            raise EngineError(f"ffmpeg remux failed: {first_error}")

        atomic_replace(tmp, path)
        return ["container metadata", "per-stream metadata", "chapters"]

    @staticmethod
    def _cleanup(tmp: str) -> None:
        if os.path.exists(tmp):
            try:
                os.unlink(tmp)
            except OSError:
                pass
