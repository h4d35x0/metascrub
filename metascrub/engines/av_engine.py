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
"""

from __future__ import annotations

import os
import shutil
import subprocess
from typing import List, Tuple

from .base import BaseEngine, EngineError, atomic_replace, temp_beside

# A remux is IO bound, but a large file on slow storage still takes time. This
# bound exists so a wedged ffmpeg cannot hang a batch run indefinitely.
_TIMEOUT_SECONDS = 900


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
            tmp,
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
            "-map_chapters", "-1", "-c", "copy", tmp,
        ]
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
