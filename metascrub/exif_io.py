"""
Shared exiftool session.

Starting exiftool is expensive relative to the work of one file, so a single
`-stay_open` helper process is reused for the life of the run. Every engine
reads through this module even when it writes with something else, which is
deliberate: reading with a different tool than the one that wrote is the whole
point of the verification in verify.py.
"""

from __future__ import annotations

import atexit
import logging
from dataclasses import dataclass, field
from typing import Any, Dict, Optional, Tuple

logger = logging.getLogger("metascrub.exif_io")
logger.addHandler(logging.NullHandler())

try:
    import exiftool  # pyexiftool
    EXIFTOOL_AVAILABLE = True
except ImportError:
    exiftool = None
    EXIFTOOL_AVAILABLE = False


@dataclass(frozen=True)
class MetadataRead:
    """
    The outcome of one metadata read, with "we could not read it" kept
    structurally distinct from "it had nothing".

    These two states used to share one representation: read() returned a bare
    dict and swallowed every exception into an empty one. A caller then could
    not tell a genuinely metadata-free file from a file it had failed to read,
    and the only caller that mattered guessed wrong. With the exiftool binary
    absent, every file of every format was reported "no metadata carriers
    found" -> CLEAN -> verified clean, without any engine ever running.
    Measured 2026-09-04: a PDF carrying its author string twice came back
    CLEAN, exit 0, and still carried it twice afterwards.

    Two states that must never be confused must never share a representation,
    so the fix is this type rather than a check at the one call site that was
    observed to be wrong.
    """

    metadata: Dict[str, Any] = field(default_factory=dict)
    ok: bool = True
    error: str = ""

    @property
    def failed(self) -> bool:
        return not self.ok

    def __bool__(self) -> bool:
        # Deliberately NOT the truthiness of the dict. A failed read must never
        # look like an empty one to a caller writing `if not read:`.
        return self.ok


class ExifSession:
    """Lazily started, explicitly closed. Safe to use as a context manager."""

    def __init__(self) -> None:
        self._helper = None

    def __enter__(self) -> "ExifSession":
        return self

    def __exit__(self, exc_type, exc, tb) -> None:
        self.close()

    @property
    def helper(self):
        if not EXIFTOOL_AVAILABLE:
            raise RuntimeError(
                "pyexiftool is required. Install with: pip install pyexiftool "
                "(and ensure the exiftool binary is on PATH)"
            )
        if self._helper is None:
            # Both arguments are required for non-ASCII paths, and users have
            # them. Measured 2026-09-04 on Windows against a directory name
            # containing accented Latin and CJK characters:
            #   default helper        -> UnicodeEncodeError, 'charmap' codec
            #   without the -charset  -> exiftool answers "No matching files"
            # Neither failure mentions encoding, so without this the tool looks
            # like it silently skipped the file.
            self._helper = exiftool.ExifToolHelper(
                encoding="utf-8",
                common_args=["-G", "-n", "-charset", "filename=UTF8"],
            )
            self._helper.run()
        return self._helper

    def read(self, path: str) -> MetadataRead:
        """
        Return exiftool's view of a file, group-prefixed.

        Never raises, but never lies either: a read that could not be performed
        comes back as MetadataRead(ok=False) carrying the reason, not as an
        empty dict. Callers decide what an unreadable file means; what they can
        no longer do is mistake it for a clean one.
        """
        try:
            return MetadataRead(metadata=self.helper.get_metadata(path)[0])
        except Exception as exc:
            logger.debug("exiftool read failed for %s: %s", path, exc)
            return MetadataRead(metadata={}, ok=False, error=str(exc))

    def probe(self) -> Tuple[bool, str]:
        """
        Report whether metadata can be read at all on this machine.

        Every engine reads its baseline through exiftool even when it writes
        with pikepdf, zipfile or ffmpeg, because reading with a different tool
        than the one that wrote is what makes verify.py meaningful. So exiftool
        is a hard dependency of the whole tool, not just of the exiftool engine,
        and a run should say so up front rather than per file.
        """
        if not EXIFTOOL_AVAILABLE:
            return False, "pyexiftool is not installed (pip install pyexiftool)"
        try:
            self.helper
        except Exception as exc:
            return False, f"exiftool binary not usable: {exc}"
        return True, ""

    def execute(self, *args: str) -> None:
        """Run a raw exiftool argument list. Raises on failure."""
        self.helper.execute(*args)

    def close(self) -> None:
        if self._helper is not None:
            try:
                self._helper.terminate()
            except Exception:
                pass
            self._helper = None


_SESSION: Optional[ExifSession] = None


def session() -> ExifSession:
    """Process-wide session, created on first use."""
    global _SESSION
    if _SESSION is None:
        _SESSION = ExifSession()
    return _SESSION


def close_session() -> None:
    global _SESSION
    if _SESSION is not None:
        _SESSION.close()
        _SESSION = None


# pyexiftool terminates its helper process from ExifTool.__del__, which calls
# subprocess.communicate() and therefore starts a thread. On Python 3.14 that
# runs during interpreter finalization, where new threads are forbidden, and
# the collection fails with a PythonFinalizationError traceback printed after
# the program has otherwise exited cleanly. It looks like a crash and is not
# one. Closing at atexit shuts the helper down while threads are still legal.
atexit.register(close_session)


__all__ = [
    "MetadataRead", "ExifSession", "EXIFTOOL_AVAILABLE",
    "session", "close_session",
]
