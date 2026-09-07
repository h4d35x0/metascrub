"""Engine registry. One instance per engine, reused across files in a run."""

from __future__ import annotations

from typing import Dict

from ..capabilities import Engine
from .av_engine import AvEngine
from .base import BaseEngine, EngineError, EngineUnavailable
from .exiftool_engine import ExifToolEngine
from .isobmff_engine import IsobmffEngine
from .jpeg_engine import JpegEngine
from .odf_engine import OdfEngine
from .png_engine import PngEngine
from .ole2_engine import Ole2Engine
from .ooxml_engine import OoxmlEngine
from .pdf_engine import PdfEngine
from .svg_engine import SvgEngine
from .webp_engine import WebpEngine

_REGISTRY: Dict[Engine, BaseEngine] = {
    Engine.EXIFTOOL: ExifToolEngine(),
    Engine.PDF: PdfEngine(),
    Engine.OOXML: OoxmlEngine(),
    Engine.ODF: OdfEngine(),
    Engine.OLE2: Ole2Engine(),
    Engine.AV: AvEngine(),
    Engine.SVG: SvgEngine(),
    # Phase 1 and 2 engines for the mobile build: pure Python, no exiftool and
    # no ffmpeg, because neither can run on a phone.
    #
    # ISOBMFF is WIRED UP as of 2026-09-07: .heic, .heif and .avif route to it,
    # because exiftool cannot remove a HEIF ICC profile at all. Measured, asked
    # explicitly, `exiftool -icc_profile:all=` reports "1 image files unchanged"
    # and every real Apple HEIC therefore failed verification on the two strings
    # inside its Display P3 profile. This engine removes them at unchanged file
    # length with the coded picture byte-identical.
    #
    # JPEG, PNG and WEBP are NOT wired: .jpg, .png and .webp still route to
    # exiftool, which handles them correctly on the desktop. `doctor` lists all
    # of them as ready because they are; `selftest` gates only on engines
    # reachable from CAPABILITIES, so an unwired engine cannot fail a working
    # installation.
    Engine.JPEG: JpegEngine(),
    Engine.PNG: PngEngine(),
    Engine.WEBP: WebpEngine(),
    Engine.ISOBMFF: IsobmffEngine(),
}


def get_engine(engine: Engine) -> BaseEngine:
    try:
        return _REGISTRY[engine]
    except KeyError:
        raise EngineError(f"no engine registered for {engine}") from None


def engine_status() -> Dict[str, str]:
    """
    Report which engines can actually run right now. Used by `metascrub doctor`
    so a user finds out a dependency is missing before a batch run rather than
    partway through one.
    """
    status = {}
    for key, engine in _REGISTRY.items():
        ok, reason = engine.available()
        status[key.value] = "ready" if ok else reason
    return status


__all__ = [
    "BaseEngine", "EngineError", "EngineUnavailable",
    "get_engine", "engine_status",
]
