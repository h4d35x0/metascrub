"""
Format capability table.

This replaces the boolean `_is_supported_file()` gate inherited from the the parent project
sanitizer (cli/sanitizer.py:326), which answered a single yes/no for a set of
eight image extensions. A boolean was adequate while exiftool handled every
supported format. It stops being adequate the moment PDF, OOXML, OLE2 and video
enter, because those need different engines and carry different guarantees.

"Supported" and "completely scrubbable" are two different states and must never
share one representation. They are separate fields here for that reason.
"""

from __future__ import annotations

from dataclasses import dataclass
from enum import Enum
from typing import Dict, Optional
import os


class Engine(str, Enum):
    """The tool that actually performs the removal for a given format."""

    EXIFTOOL = "exiftool"   # exiftool -all=, in-place, safe for these containers
    PDF = "pdf"             # pikepdf full rewrite; exiftool is NOT safe here
    OOXML = "ooxml"         # zip container rewrite (docx/xlsx/pptx)
    OLE2 = "ole2"           # legacy compound-file property streams
    AV = "av"               # ffmpeg remux, stream copy


class Completeness(str, Enum):
    """
    How much of the file's metadata the engine can actually remove.

    COMPLETE means: after a successful run there is no known metadata carrier of
    this format left behind, and the residual scan is expected to come back
    clean.

    PARTIAL means: the engine removes the standard carriers but known residue
    can survive (for example application-specific records this tool does not
    parse). A PARTIAL result must be reported as PARTIAL. A silent partial is
    worse than a refusal, because the user acts on the belief the file is clean.
    """

    COMPLETE = "complete"
    PARTIAL = "partial"


class Container(str, Enum):
    """
    How the bytes are packed. This drives the residual scan strategy: a value
    sitting inside a deflated zip member will not appear in the raw file bytes,
    so scanning the raw bytes of a .docx would produce a false "clean".
    """

    RAW = "raw"      # metadata is findable in the file bytes as-is
    ZIP = "zip"      # must inflate members before scanning
    PDF = "pdf"      # raw, but streams may be compressed
    OLE2 = "ole2"    # raw; property streams are uncompressed


@dataclass(frozen=True)
class FormatSpec:
    """One row of the capability table."""

    engine: Engine
    completeness: Completeness
    container: Container
    rewrites_container: bool  # True when output is a rebuilt file, not patched in place
    note: str = ""


def _exif(note: str = "") -> FormatSpec:
    """Formats where exiftool -all= is a genuine, in-place, complete removal."""
    return FormatSpec(Engine.EXIFTOOL, Completeness.COMPLETE, Container.RAW, False, note)


# TIER 1: exiftool is sufficient and complete.
_IMAGE: Dict[str, FormatSpec] = {
    ".jpg": _exif(), ".jpeg": _exif(), ".jpe": _exif(),
    # TIFF is structurally EXIF: exiftool answers "Can't delete IFD0 from TIFF"
    # and `-all=` leaves Artist and Copyright in place. The exiftool engine
    # follows up with a targeted sweep for these, which does remove them.
    ".tif": _exif("IFD0 cannot be dropped wholesale; identity tags swept individually"),
    ".tiff": _exif("IFD0 cannot be dropped wholesale; identity tags swept individually"),
    ".png": _exif("also drops iTXt/tEXt/zTXt text chunks"),
    ".heic": _exif(), ".heif": _exif(), ".avif": _exif(),
    ".webp": _exif("EXIF, XMP and ICC chunks"),
    ".gif": _exif("comment blocks, XMP and application extension blocks"),
    ".jp2": _exif(), ".psd": _exif(),
    # .bmp is deliberately absent. Measured 2026-09-04: exiftool 13.29 answers
    # "Writing of BMP files is not yet supported" and exits 1. Listing a format
    # this tool cannot actually write would be a promise it cannot keep.
    # Raw camera formats. exiftool edits these safely, but a raw file is a
    # container of maker-specific records; treat as PARTIAL rather than claim
    # more than can be verified.
    ".dng": FormatSpec(Engine.EXIFTOOL, Completeness.PARTIAL, Container.RAW, False,
                       "raw container; maker notes may retain private records"),
    ".cr2": FormatSpec(Engine.EXIFTOOL, Completeness.PARTIAL, Container.RAW, False,
                       "raw container; maker notes may retain private records"),
    ".nef": FormatSpec(Engine.EXIFTOOL, Completeness.PARTIAL, Container.RAW, False,
                       "raw container; maker notes may retain private records"),
    ".arw": FormatSpec(Engine.EXIFTOOL, Completeness.PARTIAL, Container.RAW, False,
                       "raw container; maker notes may retain private records"),
    ".orf": FormatSpec(Engine.EXIFTOOL, Completeness.PARTIAL, Container.RAW, False,
                       "raw container; maker notes may retain private records"),
    ".rw2": FormatSpec(Engine.EXIFTOOL, Completeness.PARTIAL, Container.RAW, False,
                       "raw container; maker notes may retain private records"),
}

# TIER 2: PDF. exiftool is NOT used here and must not be.
#
# Measured 2026-09-04 with exiftool 13.29: `exiftool -all=` on a PDF performs an
# incremental update. It appends the change and leaves the previous metadata in
# the file. The test document grew from 1519 to 1846 bytes and the original
# author and title strings were still present in the raw bytes twice each, while
# `exiftool -Author -Title` reported them absent. exiftool itself warns:
# "ExifTool PDF edits are reversible. Deleted tags may be recovered!"
#
# A pikepdf rewrite of the same document produced zero residual hits and shrank
# the file to 1025 bytes.
_PDF: Dict[str, FormatSpec] = {
    ".pdf": FormatSpec(Engine.PDF, Completeness.COMPLETE, Container.PDF, True,
                       "full rewrite; exiftool would leave recoverable residue"),
}

# TIER 3: OOXML. exiftool will not rewrite inside the zip container.
_OOXML_NOTE = ("docProps core/app/custom, lastModifiedBy, revision, edit time, "
               "template path, and w:rsid revision-save identifiers")
_OOXML: Dict[str, FormatSpec] = {
    ext: FormatSpec(Engine.OOXML, Completeness.COMPLETE, Container.ZIP, True, _OOXML_NOTE)
    for ext in (".docx", ".docm", ".xlsx", ".xlsm", ".pptx", ".pptm")
}

# TIER 4: legacy OLE2 compound files. DEFERRED, NOT SHIPPED.
#
# .doc, .xls and .ppt keep their metadata in the \005SummaryInformation and
# \005DocumentSummaryInformation property streams of an OLE2 compound file.
# Removing them means rewriting a compound-file container, and olefile can only
# overwrite a stream in place at its existing length.
#
# It is not shipped because it could not be TESTED. There is no way on this
# machine to generate a genuine legacy OLE2 document to build a fixture from,
# and an untested rewrite path for legacy Office documents can corrupt a file
# that the user cannot regenerate. An unsupported format returns a clear
# refusal; a half-working one destroys documents. The refusal is better.
#
# DISCHARGE CONDITION: these entries move into CAPABILITIES when, and only
# when, a real .doc/.xls/.ppt fixture set exists under tests/fixtures/ole2/ and
# tests/test_ole2.py passes against it.
#
# The condition is enforced, not just written down. tests/test_coverage_gate.py
# fails the moment any of these extensions appears in CAPABILITIES while the
# fixture directory or the test module is missing, so the deferral cannot be
# discharged by quietly adding a row to the table.
DEFERRED: Dict[str, str] = {
    ".doc": "legacy OLE2; needs a real fixture before the rewrite path can ship",
    ".xls": "legacy OLE2; needs a real fixture before the rewrite path can ship",
    ".ppt": "legacy OLE2; needs a real fixture before the rewrite path can ship",
}

# TIER 5: audio and video. ffmpeg remux, not exiftool.
_AV_NOTE = "container, per-stream and chapter metadata; remux without re-encoding"
_AV: Dict[str, FormatSpec] = {
    ext: FormatSpec(Engine.AV, Completeness.COMPLETE, Container.RAW, True, _AV_NOTE)
    for ext in (".mp4", ".m4v", ".mov", ".mkv", ".webm", ".avi",
                ".m4a", ".mp3", ".flac", ".wav", ".ogg", ".opus")
}

CAPABILITIES: Dict[str, FormatSpec] = {}
for _table in (_IMAGE, _PDF, _OOXML, _AV):
    CAPABILITIES.update(_table)


def deferral_for(path: str) -> Optional[str]:
    """
    Return the reason a format is knowingly not handled yet, or None.

    This exists so the CLI can tell a user "this format is deferred, here is
    why" instead of the same flat "unsupported" it gives for a .txt file. Those
    are different situations and a user acts differently on each.
    """
    _, ext = os.path.splitext(path.lower())
    return DEFERRED.get(ext)


def spec_for(path: str) -> Optional[FormatSpec]:
    """
    Return the capability row for a path, or None when the format is not
    handled. Callers must branch on None rather than on a truthiness test, so
    that an unsupported format is never confused with a format that simply has
    no metadata.
    """
    _, ext = os.path.splitext(path.lower())
    return CAPABILITIES.get(ext)


def is_supported(path: str) -> bool:
    """
    Kept for drop-in compatibility with the the parent project ExifSanitizer API. Prefer
    spec_for(), which tells you which engine will run and what it guarantees.
    """
    return spec_for(path) is not None


def supported_extensions() -> list:
    """Sorted extension list, for CLI help and documentation."""
    return sorted(CAPABILITIES)
