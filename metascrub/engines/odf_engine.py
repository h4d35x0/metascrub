"""
ODF engine: OpenDocument text, spreadsheet, presentation, drawing and their
template variants.

exiftool READS every one of these and REFUSES to write any of them. Measured
2026-09-04 with exiftool 13.29 on LibreOffice-produced files:

    exiftool -all= -overwrite_original w.odt
      Error: Writing of ODT files is not yet supported          exit 1
    exiftool -all= -overwrite_original w.ott
      Error: Writing of this type of file is not supported      exit 1

So the package is rebuilt here, in the same shape as ooxml_engine.py. This is a
separate engine rather than a flag on that one, because the two share no
constant and disagree on the one thing that decides package validity; see the
mimetype note below.

WHAT GETS REMOVED, AND WHY EACH ITEM IS LISTED RATHER THAN ASSUMED COVERED

  meta.xml              the whole part is metadata: dc:title, dc:subject,
                        dc:description, dc:creator (the LAST person to save),
                        meta:initial-creator (the ORIGINAL author, a field
                        OOXML does not have in this shape), meta:keyword,
                        meta:generator (the exact LibreOffice build and git
                        commit), meta:creation-date, dc:date,
                        meta:editing-cycles, meta:editing-duration,
                        meta:document-statistic, meta:user-defined and
                        meta:template. It is REPLACED with a valid empty
                        skeleton rather than deleted, because
                        META-INF/manifest.xml lists it and a package whose
                        manifest names a part that is not there is malformed.

  settings.xml          PrinterName and PrinterSetup. This is the carrier that
                        most justifies a dedicated ODF engine and it has no
                        OOXML equivalent in this tool. Measured on a
                        LibreOffice .odt built on this machine: PrinterName
                        held the workstation's default printer name, and
                        PrinterSetup held 11316 base64 characters decoding to
                        8487 bytes of Windows DEVMODE which carried the printer
                        name twice in ASCII and once in UTF-16LE plus the exact
                        driver string "<printer model redacted>". A document
                        published after "metadata removal" would still name the
                        owner's printer and driver.
                        Also removed: Rsid and RsidRoot, which are the
                        LibreOffice analogue of the w:rsid revision-save
                        identifiers the OOXML engine strips, and
                        PrintFaxName, RedlineProtectionKey,
                        CurrentDatabaseDataSource, CurrentDatabaseCommand and
                        EmbeddedDatabaseName, each of which names a person, a
                        data source, a query or a password hash.

  Thumbnails/           a rendered raster of page 1. It leaks CONTENT, not just
                        metadata: a document edited after its last save carries
                        a picture of the pre-edit page, and exiftool hands it
                        over as File:PreviewPNG without any skill required.
                        ooxml_engine._DROP already drops the OOXML equivalent,
                        so treating this one differently would be an
                        inconsistency with no reason behind it. The application
                        regenerates it on the next save.

  manifest.rdf          package-level RDF. Replaced with the default skeleton
                        LibreOffice itself writes. ODF 1.2 and later allow
                        arbitrary RDF to be attached to a document, exiftool
                        does not read this part at all, and therefore the
                        residual scan is blind to whatever it held. When the
                        original carried anything beyond the structural triples
                        a NOTE says so, because that is content the user cannot
                        get back.

  *.rdf (other)         in-content RDF metadata files. Dropped, with a NOTE,
                        and their META-INF/manifest.xml entries pruned.

  officeooo:rsid        revision-save identifiers on paragraphs in content.xml
  officeooo:paragraph-rsid   and styles.xml. Same vector as w:rsid: they link
                        two documents to the same editing session.

  zip member timestamps normalised to (1980, 1, 1, 0, 0, 0). They leak the
                        editing session's wall-clock time.

WHAT DELIBERATELY SURVIVES

  office:annotation and text:tracked-changes are user-visible content, not
  metadata. They are REPORTED and not removed, matching the OOXML engine's
  handling of comments and revisions. An annotation carries a dc:creator naming
  the commenter, so the report is the only thing standing between the user and
  a surprise.

  Every config-item in settings.xml that is not on the removal ALLOWLIST.
  settings.xml also holds PrinterIndependentLayout, UseFormerLineSpacing and
  roughly 120 other compatibility flags that decide how the document LAYS OUT.
  A denylist here would silently reflow the user's document, so the list below
  names what goes rather than what stays.

THE PART THAT BREAKS QUIETLY IF SOMEBODY "SIMPLIFIES" IT

  The `mimetype` member MUST be the first entry in the zip and MUST be STORED,
  not deflated. A rebuild in ooxml_engine's style writes every member DEFLATED
  and passes both obvious checks: LibreOffice still opens the file and exiftool
  still reports FileType ODT. Only a content sniffer notices. Measured:
  libmagic reports `Zip data (MIME type "K,("?)` for the naive rebuild and
  `OpenDocument Text` for the correct one. Producing a file that a mail gateway
  classifies as an unknown zip instead of an OpenDocument is a real,
  user-visible break introduced by a metadata tool.
  tests/test_odf.py::test_mimetype_is_first_and_stored is the always-on guard
  and does not need libmagic to run.

  Member order beyond `mimetype` does not matter, and is preserved from the
  input rather than sorted. Measured: LibreOffice's own output uses two
  different orders for .odt and .odp.

A BLIND SPOT WORTH WRITING DOWN

  The runtime residual scan cannot see the printer leak. exiftool does not
  report settings.xml at all, so PrinterName never becomes a needle and
  verify.meaningful_values() never searches for it. This engine removes it; the
  verification does not confirm the removal. The TEST confirms it, because the
  test knows the sentinel independently. That asymmetry is the same one w:rsid
  already lives with, and it is stated here so a later reader does not assume
  the residual scan covers it.
"""

from __future__ import annotations

import os
import re
import xml.etree.ElementTree as ET
import zipfile
from typing import List, Optional, Set, Tuple

from .base import BaseEngine, EngineError, atomic_replace, temp_beside

_MANIFEST_NS = "urn:oasis:names:tc:opendocument:xmlns:manifest:1.0"
_RDF_NS = "http://www.w3.org/1999/02/22-rdf-syntax-ns#"
_PKG_NS = "http://docs.oasis-open.org/ns/office/1.2/meta/pkg#"
_ODF_META_NS = "http://docs.oasis-open.org/ns/office/1.2/meta/odf#"

_MIMETYPE = "mimetype"
_MANIFEST = "META-INF/manifest.xml"
_META = "meta.xml"
_SETTINGS = "settings.xml"
_PACKAGE_RDF = "manifest.rdf"
_THUMBNAIL_DIR = "Thumbnails/"

# A valid, empty office:document-meta. office:version is deliberately not
# copied from the input: it is a format version, the same for every document
# LibreOffice writes, so it identifies nothing.
_META_SKELETON = (
    '<?xml version="1.0" encoding="UTF-8"?>\n'
    '<office:document-meta '
    'xmlns:office="urn:oasis:names:tc:opendocument:xmlns:office:1.0" '
    'xmlns:dc="http://purl.org/dc/elements/1.1/" '
    'xmlns:meta="urn:oasis:names:tc:opendocument:xmlns:meta:1.0" '
    'xmlns:xlink="http://www.w3.org/1999/xlink" '
    'office:version="1.3"><office:meta/></office:document-meta>'
).encode("utf-8")

# Byte-for-byte what LibreOffice writes into a .odt that has no custom RDF.
_RDF_SKELETON = (
    '<?xml version="1.0" encoding="utf-8"?>\n'
    '<rdf:RDF xmlns:rdf="http://www.w3.org/1999/02/22-rdf-syntax-ns#">\n'
    '  <rdf:Description rdf:about="">\n'
    '    <rdf:type rdf:resource="'
    'http://docs.oasis-open.org/ns/office/1.2/meta/pkg#Document"/>\n'
    '  </rdf:Description>\n'
    '</rdf:RDF>\n'
).encode("utf-8")

# ALLOWLIST of config-item names to delete from settings.xml. Never invert this
# into a list of names to keep; see the module docstring.
_SETTINGS_DROP: Set[bytes] = {
    b"PrinterName",
    b"PrinterSetup",
    b"PrintFaxName",
    b"Rsid",
    b"RsidRoot",
    b"CurrentDatabaseDataSource",
    b"CurrentDatabaseCommand",
    b"EmbeddedDatabaseName",
    b"RedlineProtectionKey",
}

# <config:config-item config:name="X" config:type="T"/> and the form with a
# value and a closing tag. Both spellings occur in one LibreOffice settings.xml.
# Handled by regex over the raw bytes rather than by ElementTree for the same
# reason ooxml_engine handles w:rsid that way: an ElementTree round-trip of a
# namespace-heavy document rewrites prefixes and changes bytes far from the edit.
_CONFIG_ITEM = re.compile(
    rb'<config:config-item\s[^>]*?config:name="([^"]*)"'
    rb'[^>]*?(?:/>|>.*?</config:config-item>)',
    re.DOTALL,
)

_RSID_ATTR = re.compile(rb'\s+officeooo:(?:rsid|paragraph-rsid)="[^"]*"')

# Structure that a default manifest.rdf is allowed to describe. Anything else
# in the part is custom RDF, and its removal is announced rather than silent.
_RDF_STRUCTURAL_TAGS = {
    "{%s}RDF" % _RDF_NS,
    "{%s}Description" % _RDF_NS,
    "{%s}type" % _RDF_NS,
    "{%s}hasPart" % _PKG_NS,
}
_RDF_STRUCTURAL_VALUES = {
    "",
    "content.xml",
    "styles.xml",
    _PKG_NS + "Document",
    _ODF_META_NS + "ContentFile",
    _ODF_META_NS + "StylesFile",
}


class OdfEngine(BaseEngine):
    name = "odf"
    supports_selective = False

    def available(self) -> Tuple[bool, str]:
        return True, ""  # stdlib only

    def strip_all(self, path: str) -> List[str]:
        self.require()
        if not zipfile.is_zipfile(path):
            raise EngineError("not a valid ODF package (not a zip container)")

        tmp = temp_beside(path, ".zip")
        targeted: List[str] = []
        notes: List[str] = []
        try:
            with zipfile.ZipFile(path) as src:
                names = src.namelist()
                dropped = self._members_to_drop(names)
                manifest = self._rewrite_manifest(src, names, dropped)

                with zipfile.ZipFile(tmp, "w", zipfile.ZIP_DEFLATED) as dst:
                    self._write_mimetype(src, names, dst)
                    for item in src.infolist():
                        name = item.filename
                        if name == _MIMETYPE:
                            continue
                        if name in dropped:
                            targeted.append(name)
                            continue

                        data, note = self._member_bytes(src, item, manifest)
                        if note is not None:
                            targeted.append(note)
                        self._write_member(dst, item, data)

                notes = self._notes(src, names, dropped)
        except EngineError:
            raise
        except Exception as exc:
            self._discard(tmp)
            raise EngineError(f"failed to rebuild ODF package: {exc}") from exc

        atomic_replace(tmp, path)
        targeted.extend(notes)
        return targeted or ["odf package rebuild"]

    # PACKAGE SHAPE

    def _write_mimetype(self, src: zipfile.ZipFile, names: List[str],
                        dst: zipfile.ZipFile) -> None:
        """
        Write `mimetype` first and STORED, or write nothing at all.

        Nothing is invented when the input has no mimetype member: guessing one
        from the file extension would let this tool relabel a package as a
        format it is not.
        """
        if _MIMETYPE not in names:
            return
        info = zipfile.ZipInfo(_MIMETYPE, date_time=(1980, 1, 1, 0, 0, 0))
        info.compress_type = zipfile.ZIP_STORED
        dst.writestr(info, src.read(_MIMETYPE))

    def _write_member(self, dst: zipfile.ZipFile, item: zipfile.ZipInfo,
                      data: bytes) -> None:
        """
        Copy one member across with its timestamp normalised.

        Directory entries stay STORED. They are zero-byte and a deflated
        directory entry is a needless deviation from what every ODF writer
        produces.
        """
        info = zipfile.ZipInfo(item.filename, date_time=(1980, 1, 1, 0, 0, 0))
        info.compress_type = (
            zipfile.ZIP_STORED if item.is_dir() else zipfile.ZIP_DEFLATED
        )
        info.external_attr = item.external_attr
        dst.writestr(info, data)

    def _members_to_drop(self, names: List[str]) -> Set[str]:
        drop = {n for n in names if n.startswith(_THUMBNAIL_DIR) and not n.endswith("/")}
        drop.update(
            n for n in names
            if n.lower().endswith(".rdf") and n != _PACKAGE_RDF
        )
        return drop

    # MEMBER REWRITES

    def _member_bytes(self, src: zipfile.ZipFile, item: zipfile.ZipInfo,
                      manifest: Optional[bytes]) -> Tuple[bytes, Optional[str]]:
        """Return the bytes to write for one member, and what to report."""
        name = item.filename
        if name == _META:
            return _META_SKELETON, name
        if name == _PACKAGE_RDF:
            return _RDF_SKELETON, name
        if name == _MANIFEST:
            return (manifest if manifest is not None else src.read(name)), None
        if item.is_dir():
            return b"", None

        data = src.read(name)
        if name == _SETTINGS:
            cleaned, removed = self._strip_settings(data)
            if removed:
                return cleaned, f"{name} ({', '.join(removed)})"
            return cleaned, None
        if not name.endswith(".xml"):
            return data, None

        cleaned = _RSID_ATTR.sub(b"", data)
        if cleaned != data:
            return cleaned, f"{name} (revision identifiers)"
        return data, None

    def _strip_settings(self, data: bytes) -> Tuple[bytes, List[str]]:
        """Delete the allowlisted config-items, leave every other one alone."""
        removed: List[str] = []

        def replace(match: "re.Match") -> bytes:
            name = match.group(1)
            if name in _SETTINGS_DROP:
                removed.append(name.decode("utf-8", "replace"))
                return b""
            return match.group(0)

        return _CONFIG_ITEM.sub(replace, data), removed

    def _rewrite_manifest(self, src: zipfile.ZipFile, names: List[str],
                          dropped: Set[str]) -> Optional[bytes]:
        """Remove manifest entries pointing at parts that will not be written."""
        if _MANIFEST not in names or not dropped:
            return None
        try:
            ET.register_namespace("manifest", _MANIFEST_NS)
            root = ET.fromstring(src.read(_MANIFEST))
            attr = "{%s}full-path" % _MANIFEST_NS
            for child in list(root):
                full = (child.get(attr) or "").lstrip("/")
                if full in dropped:
                    root.remove(child)
            return ET.tostring(root, encoding="UTF-8", xml_declaration=True)
        except Exception:
            # A manifest we cannot parse is one we must not half-edit. Leaving
            # it alone leaves an entry for a part that is gone, which readers
            # tolerate; a truncated manifest is not tolerated at all.
            return None

    # REPORTING

    def _notes(self, src: zipfile.ZipFile, names: List[str],
               dropped: Set[str]) -> List[str]:
        """
        Flag what was intentionally left in place, and what was removed that the
        user cannot get back. Not removing annotations is the correct default;
        not mentioning them would not be.
        """
        notes: List[str] = []
        body = b""
        for part in ("content.xml", "styles.xml"):
            if part in names:
                try:
                    body += src.read(part)
                except Exception:
                    continue
        if b"<office:annotation" in body:
            notes.append("NOTE: document annotations present and NOT removed")
        if b"<text:tracked-changes" in body:
            notes.append("NOTE: tracked changes present and NOT removed")

        if _PACKAGE_RDF in names:
            try:
                custom = not self._rdf_is_default_structure(src.read(_PACKAGE_RDF))
            except Exception:
                custom = True
            if custom:
                notes.append(
                    "NOTE: custom RDF metadata in manifest.rdf was removed"
                )
        extra_rdf = sorted(n for n in dropped if n.lower().endswith(".rdf"))
        if extra_rdf:
            notes.append(
                "NOTE: in-content RDF metadata removed: " + ", ".join(extra_rdf)
            )
        return notes

    def _rdf_is_default_structure(self, data: bytes) -> bool:
        """
        True when manifest.rdf says nothing except which parts the package has.

        This decides whether replacing the part is silent or announced, so it
        errs toward announcing: any element, any subject, any object and any
        literal outside the structural set below counts as custom.
        """
        root = ET.fromstring(data)
        about = "{%s}about" % _RDF_NS
        resource = "{%s}resource" % _RDF_NS
        for element in root.iter():
            if element.tag not in _RDF_STRUCTURAL_TAGS:
                return False
            if (element.text or "").strip():
                return False
            for key, value in element.attrib.items():
                if key not in (about, resource):
                    return False
                if value not in _RDF_STRUCTURAL_VALUES:
                    return False
        return True

    def _discard(self, tmp: str) -> None:
        if os.path.exists(tmp):
            try:
                os.unlink(tmp)
            except OSError:
                pass
