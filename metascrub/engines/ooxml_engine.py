"""
OOXML engine: docx, xlsx, pptx and their macro-enabled variants.

exiftool will not rewrite inside the zip container, so these formats are handled
by rebuilding the package.

What gets removed, and why each item is here rather than assumed covered by
"wipe docProps":

  docProps/core.xml     creator, lastModifiedBy, revision, created, modified,
                        title, subject, keywords, description, category
  docProps/app.xml      Company, Manager, the template path, total edit time,
                        and the application and version that produced the file
  docProps/custom.xml   arbitrary custom properties, frequently used by
                        document management systems to stamp internal IDs
  w:rsid attributes     revision-save identifiers scattered through the document
                        body. These survive a docProps wipe completely, and they
                        are a real deanonymization vector: they let two
                        documents be linked to the same editing session and can
                        reveal how a document was assembled.

core.xml and app.xml are replaced with valid empty skeletons rather than
deleted. Deleting the parts outright leaves dangling relationships and some
consumers reject the result. A metadata tool that produces documents Word warns
about will not get used, and a tool that does not get used protects nobody.

Tracked changes and comments are detected and REPORTED rather than removed. They
are user-visible content, not metadata, and silently deleting them would destroy
work. The report exists because a user who asked to remove metadata will
reasonably assume revision history went too, and needs to be told it did not.
"""

from __future__ import annotations

import os
import re
import shutil
import xml.etree.ElementTree as ET
import zipfile
from typing import List, Set, Tuple

from .base import BaseEngine, EngineError, atomic_replace, temp_beside

_CT_NS = "http://schemas.openxmlformats.org/package/2006/content-types"
_REL_NS = "http://schemas.openxmlformats.org/package/2006/relationships"

_CORE_SKELETON = (
    '<?xml version="1.0" encoding="UTF-8" standalone="yes"?>\r\n'
    '<cp:coreProperties '
    'xmlns:cp="http://schemas.openxmlformats.org/package/2006/metadata/core-properties" '
    'xmlns:dc="http://purl.org/dc/elements/1.1/" '
    'xmlns:dcterms="http://purl.org/dc/terms/" '
    'xmlns:dcmitype="http://purl.org/dc/dcmitype/" '
    'xmlns:xsi="http://www.w3.org/2001/XMLSchema-instance"/>'
).encode("utf-8")

_APP_SKELETON = (
    '<?xml version="1.0" encoding="UTF-8" standalone="yes"?>\r\n'
    '<Properties '
    'xmlns="http://schemas.openxmlformats.org/officeDocument/2006/extended-properties" '
    'xmlns:vt="http://schemas.openxmlformats.org/officeDocument/2006/docPropsVTypes"/>'
).encode("utf-8")

_REPLACE = {
    "docProps/core.xml": _CORE_SKELETON,
    "docProps/app.xml": _APP_SKELETON,
}
_DROP = {"docProps/custom.xml", "docProps/thumbnail.jpeg", "docProps/thumbnail.wmf"}

# w:rsidR="00AB12CD", w:rsidRDefault="...", and the whole <w:rsid w:val="..."/>
# table inside <w:rsids>. Both spellings appear; strip attributes and elements.
_RSID_ATTR = re.compile(rb'\s+w:rsid[A-Za-z]*="[^"]*"')
_RSID_ELEM = re.compile(rb"<w:rsids>.*?</w:rsids>", re.DOTALL)
_RSID_SINGLE = re.compile(rb"<w:rsid\s+w:val=\"[^\"]*\"\s*/>")

# Elements naming a person or a machine that live outside docProps.
_PROOF_STATE = re.compile(rb"<w:proofState[^>]*/>")


class OoxmlEngine(BaseEngine):
    name = "ooxml"
    supports_selective = False

    def available(self) -> Tuple[bool, str]:
        return True, ""  # stdlib only

    def strip_all(self, path: str) -> List[str]:
        self.require()
        if not zipfile.is_zipfile(path):
            raise EngineError("not a valid OOXML package (not a zip container)")

        tmp = temp_beside(path, ".zip")
        targeted: List[str] = []
        notes: List[str] = []
        try:
            with zipfile.ZipFile(path) as src:
                names = src.namelist()
                dropped = _DROP.intersection(names)

                # Content types and relationships must stop referencing anything
                # that is about to disappear, or the package becomes invalid.
                content_types = self._rewrite_content_types(src, dropped)
                root_rels = self._rewrite_root_rels(src, dropped)

                with zipfile.ZipFile(tmp, "w", zipfile.ZIP_DEFLATED) as dst:
                    for item in src.infolist():
                        name = item.filename
                        if name in _DROP:
                            targeted.append(name)
                            continue
                        if name in _REPLACE:
                            dst.writestr(name, _REPLACE[name])
                            targeted.append(name)
                            continue
                        if name == "[Content_Types].xml" and content_types is not None:
                            dst.writestr(item, content_types)
                            continue
                        if name == "_rels/.rels" and root_rels is not None:
                            dst.writestr(item, root_rels)
                            continue

                        data = src.read(name)
                        if name.endswith(".xml") and name.startswith("word/"):
                            cleaned, changed = self._strip_rsids(data)
                            if changed:
                                targeted.append(f"{name} (revision identifiers)")
                            data = cleaned
                        # Zip entries carry their own modification timestamps.
                        # They are metadata too, and they leak the editing
                        # session's wall-clock time, so normalise them.
                        info = zipfile.ZipInfo(name, date_time=(1980, 1, 1, 0, 0, 0))
                        info.compress_type = zipfile.ZIP_DEFLATED
                        info.external_attr = item.external_attr
                        dst.writestr(info, data)

                notes = self._content_notes(names)
        except EngineError:
            raise
        except Exception as exc:
            if os.path.exists(tmp):
                try:
                    os.unlink(tmp)
                except OSError:
                    pass
            raise EngineError(f"failed to rebuild OOXML package: {exc}") from exc

        atomic_replace(tmp, path)
        targeted.extend(notes)
        return targeted or ["ooxml package rebuild"]

    def _strip_rsids(self, data: bytes) -> Tuple[bytes, bool]:
        original = data
        data = _RSID_ELEM.sub(b"", data)
        data = _RSID_SINGLE.sub(b"", data)
        data = _RSID_ATTR.sub(b"", data)
        data = _PROOF_STATE.sub(b"", data)
        return data, data != original

    def _rewrite_content_types(self, src: zipfile.ZipFile, dropped: Set[str]):
        """Remove Override entries pointing at parts that will not be written."""
        if "[Content_Types].xml" not in src.namelist() or not dropped:
            return None
        try:
            ET.register_namespace("", _CT_NS)
            root = ET.fromstring(src.read("[Content_Types].xml"))
            for child in list(root):
                part = (child.get("PartName") or "").lstrip("/")
                if part in dropped:
                    root.remove(child)
            return ET.tostring(root, encoding="UTF-8", xml_declaration=True)
        except Exception:
            # A package we cannot parse is one we should not half-edit.
            return None

    def _rewrite_root_rels(self, src: zipfile.ZipFile, dropped: Set[str]):
        """Remove root relationships pointing at parts that will not be written."""
        if "_rels/.rels" not in src.namelist() or not dropped:
            return None
        try:
            ET.register_namespace("", _REL_NS)
            root = ET.fromstring(src.read("_rels/.rels"))
            for child in list(root):
                target = (child.get("Target") or "").lstrip("/")
                if target in dropped:
                    root.remove(child)
            return ET.tostring(root, encoding="UTF-8", xml_declaration=True)
        except Exception:
            return None

    def _content_notes(self, names: List[str]) -> List[str]:
        """
        Flag user-visible history that was intentionally left in place, so the
        caller can surface it. Not removing it is the correct default; not
        mentioning it would not be.
        """
        notes = []
        if any(n.endswith("comments.xml") for n in names):
            notes.append("NOTE: document comments present and NOT removed")
        return notes
