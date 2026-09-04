"""Engine registry. One instance per engine, reused across files in a run."""

from __future__ import annotations

from typing import Dict

from ..capabilities import Engine
from .av_engine import AvEngine
from .base import BaseEngine, EngineError, EngineUnavailable
from .exiftool_engine import ExifToolEngine
from .ole2_engine import Ole2Engine
from .ooxml_engine import OoxmlEngine
from .pdf_engine import PdfEngine

_REGISTRY: Dict[Engine, BaseEngine] = {
    Engine.EXIFTOOL: ExifToolEngine(),
    Engine.PDF: PdfEngine(),
    Engine.OOXML: OoxmlEngine(),
    Engine.OLE2: Ole2Engine(),
    Engine.AV: AvEngine(),
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
