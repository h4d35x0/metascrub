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
import re
from dataclasses import dataclass, field
from enum import Enum
from typing import Any, Dict, List, Optional, Tuple

logger = logging.getLogger("metascrub.exif_io")
logger.addHandler(logging.NullHandler())

try:
    import exiftool  # pyexiftool
    EXIFTOOL_AVAILABLE = True
except ImportError:
    exiftool = None
    EXIFTOOL_AVAILABLE = False


class ReadOutcome(str, Enum):
    """
    How much of a read actually happened. Three states, not two.

    PARSED   exiftool ran and understood the document. Its silence about
             metadata is evidence: this file really carries none.
    UNPARSED exiftool ran, identified the file, and said in a warning that it
             could not parse it. Its silence means nothing at all, and treating
             it as evidence produces a false CLEAN.
    FAILED   the read could not be performed. No output to reason about.

    PARSED and UNPARSED used to be one state, which is the same conflation that
    produced MetadataRead in the first place, one level up. See the class
    docstring below.
    """

    PARSED = "parsed"
    UNPARSED = "unparsed"
    FAILED = "failed"


# Warnings exiftool emits about documents it read SUCCESSFULLY.
#
# MEASURED 2026-09-04 with exiftool 13.29 over the whole fixture corpus, 31
# formats read twice each (as built, and again after MetadataScrubber ran).
# Exactly one warning family appeared on a file that was fine:
#
#   Unrecognized MIMEType application/vnd.oasis.opendocument.text-template
#   Unrecognized MIMEType application/vnd.oasis.opendocument.spreadsheet-template
#   Unrecognized MIMEType application/vnd.oasis.opendocument.presentation-template
#   Unrecognized MIMEType application/vnd.oasis.opendocument.graphics-template
#
# on .ott, .ots, .otp and .otg. Those four matter more than the rest of the
# corpus put together, because after scrubbing they report ZERO document tags:
# they land in the exact shape of the defect this list exists to separate, and
# they are good files that must stay CLEAN and must not be touched.
#
# The reason this family is benign is not its wording. It is that exiftool emits
# it on reads that ALSO returned the document's full metadata: the as-built .ott
# fixture came back with its Title and Creator and this warning together. A
# warning exiftool raises while successfully reporting document tags cannot be
# evidence that it failed to parse the document. That is the test any future
# entry here has to meet, and tests/test_unparseable.py enforces it.
#
# This is an ALLOWLIST, and the direction is deliberate. An unrecognised warning
# is treated as a parse failure, which costs a refusal on a file that may have
# been fine. A denylist would fail the other way, toward CLEAN, on the first
# warning nobody had measured yet. A false CLEAN is the one direction this tool
# must never fail in.
_BENIGN_WARNINGS: Tuple["re.Pattern[str]", ...] = (
    re.compile(r"^\s*unrecognized mimetype\b", re.IGNORECASE),
)


def _exiftool_complaints(metadata: Dict[str, Any]) -> List[str]:
    """Every Warning or Error string exiftool attached to this read."""
    out: List[str] = []
    for key, value in metadata.items():
        group, _, bare = key.partition(":")
        if group != "ExifTool":
            continue
        if not (bare.startswith("Warning") or bare.startswith("Error")):
            continue
        values = value if isinstance(value, (list, tuple)) else [value]
        out.extend(str(item) for item in values)
    return out


def parse_failures(metadata: Dict[str, Any]) -> Tuple[str, ...]:
    """
    The complaints in a read that say exiftool could not parse the document.

    Measured examples, all from deliberately truncated copies of the fixture
    corpus: "XMP format error (no closing tag for svg)", "Format error reading
    ZIP file", "JPEG format error", "Truncated PNG image", "Error reading RIFF
    file (corrupted?)", "Invalid xref table", "Truncated 'moov' data",
    "Format error in FLAC file".
    """
    return tuple(
        complaint for complaint in _exiftool_complaints(metadata)
        if not any(pattern.search(complaint) for pattern in _BENIGN_WARNINGS)
    )


@dataclass(frozen=True)
class MetadataRead:
    """
    The outcome of one metadata read, with "we could not read it" and "we could
    not parse it" both kept structurally distinct from "it had nothing".

    The first two states used to share one representation: read() returned a
    bare dict and swallowed every exception into an empty one. A caller then
    could not tell a genuinely metadata-free file from a file it had failed to
    read, and the only caller that mattered guessed wrong. With the exiftool
    binary absent, every file of every format was reported "no metadata carriers
    found" -> CLEAN -> verified clean, without any engine ever running.
    Measured 2026-09-04: a PDF carrying its author string twice came back
    CLEAN, exit 0, and still carried it twice afterwards.

    The third state was found by audit on the same day, one level up and wearing
    the same clothes. Measured with exiftool 13.29 on a truncated SVG carrying
    sodipodi:docname:

        [ExifTool] Warning  : XMP format error (no closing tag for svg) [x2]
        [File]     FileType : SVG
        (no [SVG] tags at all)

    The read SUCCEEDS. exiftool identifies the file, does not fail, surfaces
    nothing, and says in a warning that it could not parse the document. A
    caller checking only `ok` sees a successful read of an empty file and
    reports CLEAN about a file that carries an editor's docname. So "parsed and
    empty" and "not parsed" are separate states here, not a check bolted onto
    the one call site observed to be wrong.
    """

    metadata: Dict[str, Any] = field(default_factory=dict)
    outcome: ReadOutcome = ReadOutcome.PARSED
    error: str = ""
    parse_failures: Tuple[str, ...] = field(default_factory=tuple)

    @property
    def ok(self) -> bool:
        """
        The read was PERFORMED. Says nothing about whether it was understood.

        Kept because callers that only want to know whether there is any output
        to look at are still correct with it. A caller reasoning about what the
        ABSENCE of metadata means wants `parsed`, not this.
        """
        return self.outcome is not ReadOutcome.FAILED

    @property
    def failed(self) -> bool:
        return self.outcome is ReadOutcome.FAILED

    @property
    def parsed(self) -> bool:
        """exiftool understood the document, so its silence is evidence."""
        return self.outcome is ReadOutcome.PARSED

    @property
    def unparsed(self) -> bool:
        return self.outcome is ReadOutcome.UNPARSED

    def __bool__(self) -> bool:
        # Deliberately NOT the truthiness of the dict. Neither a failed read nor
        # an unparsed one may look like an empty one to a caller writing
        # `if not read:`.
        return self.parsed


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

        Never raises, but never lies either. A read that could not be performed
        comes back as ReadOutcome.FAILED carrying the reason; a read exiftool
        performed but could not parse comes back as ReadOutcome.UNPARSED
        carrying the warnings that say so. Callers decide what each means; what
        they can no longer do is mistake either for a clean file.

        The classification happens HERE, once, rather than at each call site.
        Three call sites reason about an empty read (the scrubber, the CLI
        inspector and the GUI inspector) and all three had the same bug.
        """
        try:
            metadata = self.helper.get_metadata(path)[0]
        except Exception as exc:
            logger.debug("exiftool read failed for %s: %s", path, exc)
            return MetadataRead(metadata={}, outcome=ReadOutcome.FAILED, error=str(exc))

        failures = parse_failures(metadata)
        if failures:
            logger.debug("exiftool could not parse %s: %s", path, "; ".join(failures))
            return MetadataRead(
                metadata=metadata,
                outcome=ReadOutcome.UNPARSED,
                error="; ".join(failures),
                parse_failures=failures,
            )
        return MetadataRead(metadata=metadata)

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
    "MetadataRead", "ReadOutcome", "parse_failures",
    "ExifSession", "EXIFTOOL_AVAILABLE",
    "session", "close_session",
]
