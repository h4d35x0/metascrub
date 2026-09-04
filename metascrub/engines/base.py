"""
Engine interface.

An engine knows how to strip one family of containers. It reports whether it can
run at all (its dependency may be absent) and whether it can do selective field
work, because only exiftool can.

Engines write to a temporary file beside the target and replace the original
with os.replace, which is atomic on the same filesystem. That matters: a crash
partway through a container rewrite must not leave the user with a truncated
document where they used to have a working one with some metadata in it.
"""

from __future__ import annotations

import os
import shutil
import tempfile
from typing import Dict, List, Optional, Sequence, Tuple


class EngineError(RuntimeError):
    """Raised when an engine cannot complete a removal it was asked to do."""


class EngineUnavailable(EngineError):
    """Raised when the engine's dependency (binary or library) is missing."""


class BaseEngine:
    name = "base"
    supports_selective = False

    def available(self) -> Tuple[bool, str]:
        """Return (usable, reason). Reason is only meaningful when usable is False."""
        return True, ""

    def require(self) -> None:
        ok, reason = self.available()
        if not ok:
            raise EngineUnavailable(f"{self.name} engine unavailable: {reason}")

    def strip_all(self, path: str) -> List[str]:
        """
        Remove every metadata carrier this engine knows about, in place.
        Returns a list of human-readable descriptions of what was targeted.
        """
        raise NotImplementedError

    def strip_fields(
        self,
        path: str,
        fields_to_remove: Sequence[str],
        fields_to_sanitize: Sequence[str],
    ) -> Tuple[List[str], List[str]]:
        """Selective removal. Only exiftool-backed formats support this."""
        raise EngineError(
            f"{self.name} engine cannot remove individual fields; "
            "use remove_all for this format"
        )


def atomic_replace(src_tmp: str, dst: str) -> None:
    """
    Move a rebuilt file over the original, preserving the original's timestamps
    and mode.

    Preserving mtime is a deliberate choice and worth stating: the filesystem
    timestamp is itself metadata, but it belongs to the filesystem rather than
    to the file's contents, and silently changing it would break the caller's
    own backup and sync tooling. Callers who want the timestamp reset can do it
    explicitly; see the --reset-times CLI flag.
    """
    try:
        stat = os.stat(dst)
        shutil.copystat(dst, src_tmp)
        os.replace(src_tmp, dst)
        os.utime(dst, (stat.st_atime, stat.st_mtime))
    except Exception:
        # Never leave the temporary file behind on a failed replace.
        if os.path.exists(src_tmp):
            try:
                os.unlink(src_tmp)
            except OSError:
                pass
        raise


def temp_beside(path: str, suffix: str = ".tmp") -> str:
    """
    Create a temporary path in the same directory as the target so that
    os.replace stays atomic. A temp file on another volume would turn the
    replace into a copy, which is not atomic and can half-write.
    """
    directory = os.path.dirname(os.path.abspath(path)) or "."
    fd, tmp = tempfile.mkstemp(prefix=".metascrub-", suffix=suffix, dir=directory)
    os.close(fd)
    return tmp
