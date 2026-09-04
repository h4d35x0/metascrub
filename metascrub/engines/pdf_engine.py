"""
PDF engine: full rewrite with pikepdf. exiftool is deliberately not used.

Measured on 2026-09-04 with exiftool 13.29 against a one-page test document:

    exiftool -all= -overwrite_original test.pdf
      -> file GREW from 1519 to 1846 bytes
      -> "SECRETAUTHOR12345" still present in the raw bytes, twice
      -> "SECRETTITLE12345"  still present in the raw bytes, twice
      -> but `exiftool -Author -Title` printed nothing at all
      -> exiftool's own warning: "ExifTool PDF edits are reversible.
         Deleted tags may be recovered!"

    pikepdf rewrite of the same input
      -> file SHRANK from 1519 to 1025 bytes
      -> zero residual hits for any of the three secrets

exiftool edits a PDF by appending an incremental update. The old cross-reference
table and the old objects stay in the file and are trivially recoverable. A tool
that reported that file as sanitized would be lying, and it would be lying in
exactly the way the parent project's own article "Residual metadata recovery: why
sanitized files still betray their origins" describes.

Rewriting the document from the object graph is the only way to actually drop
the previous revisions.
"""

from __future__ import annotations

from typing import List, Tuple

from .base import BaseEngine, EngineError, atomic_replace, temp_beside

try:
    import pikepdf
    PIKEPDF_AVAILABLE = True
except ImportError:
    pikepdf = None
    PIKEPDF_AVAILABLE = False


# Root-level keys that carry authoring or application state rather than page
# content. PieceInfo in particular holds private application data (Illustrator
# and InDesign both use it) and survives a docinfo wipe.
_ROOT_METADATA_KEYS = ("/Metadata", "/PieceInfo", "/LastModified", "/AcroForm")


class PdfEngine(BaseEngine):
    name = "pdf"
    supports_selective = False

    def available(self) -> Tuple[bool, str]:
        if not PIKEPDF_AVAILABLE:
            return False, "pikepdf is not installed (pip install pikepdf)"
        return True, ""

    def strip_all(self, path: str) -> List[str]:
        self.require()
        tmp = temp_beside(path, ".pdf")
        targeted: List[str] = []
        try:
            with pikepdf.open(path) as pdf:
                # The document information dictionary: Author, Title, Subject,
                # Keywords, Creator, Producer, CreationDate, ModDate.
                if "/Info" in pdf.trailer:
                    del pdf.trailer["/Info"]
                    targeted.append("document info dictionary")

                for key in _ROOT_METADATA_KEYS:
                    if key in pdf.Root:
                        # AcroForm is only removed when it carries no fields, so
                        # that a real fillable form is not silently destroyed by
                        # a metadata operation. Destroying user-visible content
                        # is not this tool's job.
                        if key == "/AcroForm":
                            try:
                                fields = pdf.Root["/AcroForm"].get("/Fields", None)
                                if fields is not None and len(fields) > 0:
                                    continue
                            except Exception:
                                continue
                        del pdf.Root[key]
                        targeted.append(f"root {key}")

                # linearize forces a complete rewrite of the object graph and
                # cross-reference table, which is what actually drops the
                # previous revisions rather than appending to them.
                pdf.save(tmp, linearize=True, deterministic_id=True)
        except Exception as exc:
            raise EngineError(f"pikepdf failed to rewrite PDF: {exc}") from exc

        atomic_replace(tmp, path)
        return targeted or ["pdf rewrite"]
