"""
OpenDocument fixture builder.

Like tests/fixtures/ole2/, this directory holds a generator and not committed
binaries: a committed .odt is an opaque blob nobody can review, while the
sentinels a generator seeds are visible in the source and are regenerated on
whatever machine runs the tests.

TWO ROUTES, AND WHY BOTH EXIST

  build_odf()          hand-writes the whole package with zipfile and nothing
                       else. No LibreOffice, no imaging library, no skip, no
                       platform variance, and exact control over what is in the
                       file. That control is what makes the interesting cases
                       testable at all: LibreOffice will not reliably put a
                       chosen printer name, a populated meta:template href or an
                       officeooo:rsid attribute into a file on demand.

  libreoffice_odf()    converts a python-docx / openpyxl / python-pptx seed
                       through LibreOffice's own ODF export filter. This is the
                       control on the route above: a hand-built fixture proves
                       the engine handles a file the TEST wrote, and nothing
                       more. Returns None, never raises, when LibreOffice is
                       absent or the conversion produced nothing; the caller
                       must translate None into a SKIP. A machine without the
                       converter says nothing about whether the engine works.

WHAT THE HAND-BUILT PACKAGE IS NOT

  It does not prove LibreOffice's exact spelling of officeooo:rsid or of
  office:annotation, because producing either needs an interactive edit and
  these fixtures are built headless. It tests the engine's regex and its
  reporting path against the spelling the ODF schema defines. That is what it
  proves, and nothing beyond it.

SENTINELS

  sentinels() derives a family of distinct values from one base sentinel. Three
  of them are meant to SURVIVE (the body text, the annotation author, and the
  layout config-items), so none of the derived values may contain the base value
  as a substring: a cross-product test searching the output for the base value
  would otherwise find a survivor that was supposed to stay and report the file
  as still leaking. The derivation inserts its tag BEFORE the numeric serial for
  exactly that reason, and _check_disjoint() asserts it rather than trusting it.
"""

from __future__ import annotations

import base64
import os
import zipfile
from typing import Callable, Dict, Optional

# A 1x1 RGBA PNG, written as a hex literal so that building a fixture needs no
# imaging library. exiftool reports it as File:PreviewPNG, which is exactly the
# ease-of-extraction the engine's thumbnail argument rests on.
THUMBNAIL_PNG = bytes.fromhex(
    "89504e470d0a1a0a"
    "0000000d49484452000000010000000108060000001f15c489"
    "0000000a49444154789c6300010000050001"
    "0d0a2db4"
    "0000000049454e44ae426082"
)

MIMETYPES: Dict[str, str] = {
    ".odt": "application/vnd.oasis.opendocument.text",
    ".ott": "application/vnd.oasis.opendocument.text-template",
    ".ods": "application/vnd.oasis.opendocument.spreadsheet",
    ".ots": "application/vnd.oasis.opendocument.spreadsheet-template",
    ".odp": "application/vnd.oasis.opendocument.presentation",
    ".otp": "application/vnd.oasis.opendocument.presentation-template",
    ".odg": "application/vnd.oasis.opendocument.graphics",
    ".otg": "application/vnd.oasis.opendocument.graphics-template",
}

# Which document body an extension carries. Templates share the body of the
# document type they are a template for; they differ only in the mimetype
# string, which is why they cost nothing to cover.
FAMILY: Dict[str, str] = {
    ".odt": "text", ".ott": "text",
    ".ods": "spreadsheet", ".ots": "spreadsheet",
    ".odp": "presentation", ".otp": "presentation",
    ".odg": "drawing", ".otg": "drawing",
}

RSID = "00abc123"

_XMLNS = (
    'xmlns:office="urn:oasis:names:tc:opendocument:xmlns:office:1.0" '
    'xmlns:style="urn:oasis:names:tc:opendocument:xmlns:style:1.0" '
    'xmlns:text="urn:oasis:names:tc:opendocument:xmlns:text:1.0" '
    'xmlns:table="urn:oasis:names:tc:opendocument:xmlns:table:1.0" '
    'xmlns:draw="urn:oasis:names:tc:opendocument:xmlns:drawing:1.0" '
    'xmlns:fo="urn:oasis:names:tc:opendocument:xmlns:xsl-fo-compatible:1.0" '
    'xmlns:xlink="http://www.w3.org/1999/xlink" '
    'xmlns:dc="http://purl.org/dc/elements/1.1/" '
    'xmlns:meta="urn:oasis:names:tc:opendocument:xmlns:meta:1.0" '
    'xmlns:svg="urn:oasis:names:tc:opendocument:xmlns:svg-compatible:1.0" '
    'xmlns:presentation="urn:oasis:names:tc:opendocument:xmlns:presentation:1.0" '
    'xmlns:officeooo="http://openoffice.org/2009/office"'
)


def sentinels(value: str) -> Dict[str, str]:
    """
    Derive the sentinel family from one base metadata sentinel.

    The base value ends in a numeric serial (conftest.sentinel builds it that
    way). The tag is inserted before the serial so that no derived value
    contains the base value, and no derived value contains another.
    """
    stem = value.rstrip("0123456789")
    serial = value[len(stem):]
    if not serial:
        raise ValueError(
            "the base sentinel must end in a numeric serial; without one the "
            "derived sentinels contain it as a prefix and a value meant to "
            "survive would be read as a survivor of the value meant to go"
        )
    out = {"meta": value}
    for tag in ("printer", "db", "body", "annot", "rdf"):
        out[tag] = stem + tag.upper() + serial
    _check_disjoint(out)
    return out


def _check_disjoint(values: Dict[str, str]) -> None:
    for a_key, a in values.items():
        for b_key, b in values.items():
            if a_key != b_key and a in b:
                raise ValueError(
                    f"sentinel {a_key} is a substring of {b_key}; a search for "
                    "one would find the other and the test would lie"
                )


def printer_blob(printer: str) -> bytes:
    """
    A fabricated Windows DEVMODE carrying the printer sentinel the way a real
    one carries the printer name: twice in ASCII, once in UTF-16LE, alongside a
    driver string.

    Fabricated rather than copied from this machine's real PrinterSetup, because
    a committed fixture must not carry the owner's actual printer and driver
    into the repository. The shape is what the test needs; the bytes are not.
    """
    ascii_name = printer.encode("ascii")
    return (
        b"\x27\x21\xfe\xff"
        + ascii_name + b"\x00" * 8
        + b"PRINTER_NAME\x00" + ascii_name + b"\x00"
        + b"DRIVER_NAME\x00" + ascii_name + b" Series\x00"
        + b"COMPAT_DUPLEX_MODE\x00DuplexMode::Off\x00"
        + printer.encode("utf-16-le") + b"\x00" * 16
    )


def _meta_xml(value: str) -> bytes:
    return (
        '<?xml version="1.0" encoding="UTF-8"?>\n'
        '<office:document-meta ' + _XMLNS + ' office:version="1.3">'
        '<office:meta>'
        '<dc:title>' + value + '</dc:title>'
        '<dc:subject>' + value + '</dc:subject>'
        '<dc:description>' + value + '</dc:description>'
        '<dc:creator>' + value + '</dc:creator>'
        '<meta:initial-creator>' + value + '</meta:initial-creator>'
        '<meta:keyword>' + value + '</meta:keyword>'
        '<meta:creation-date>2019-03-04T11:22:33</meta:creation-date>'
        '<dc:date>2019-03-04T11:22:33</dc:date>'
        '<meta:editing-cycles>7</meta:editing-cycles>'
        '<meta:editing-duration>PT13M</meta:editing-duration>'
        '<meta:generator>' + value + '</meta:generator>'
        '<meta:document-statistic meta:page-count="1" meta:word-count="6"/>'
        '<meta:user-defined meta:name="Reviewer">' + value + '</meta:user-defined>'
        '<meta:template xlink:type="simple" xlink:actuate="onRequest" '
        'xlink:title="' + value + '" '
        'xlink:href="file:///C:/Users/' + value + '/Templates/t.ott"/>'
        '</office:meta></office:document-meta>'
    ).encode("utf-8")


def _body(family: str, body: str, annot: Optional[str]) -> str:
    paragraph = (
        '<text:p officeooo:rsid="' + RSID + '" '
        'officeooo:paragraph-rsid="' + RSID + '">' + body + '</text:p>'
    )
    if family == "text":
        extra = ""
        if annot:
            extra = (
                '<text:tracked-changes>'
                '<text:changed-region text:id="ct1">'
                '<text:insertion><office:change-info>'
                '<dc:creator>' + annot + '</dc:creator>'
                '<dc:date>2019-03-04T11:22:33</dc:date>'
                '</office:change-info></text:insertion>'
                '</text:changed-region></text:tracked-changes>'
            )
            paragraph += (
                '<text:p><office:annotation office:name="__Annotation__1">'
                '<dc:creator>' + annot + '</dc:creator>'
                '<dc:date>2019-03-04T11:22:33</dc:date>'
                '<text:p>review comment</text:p>'
                '</office:annotation>annotated</text:p>'
            )
        return '<office:text>' + extra + paragraph + '</office:text>'
    if family == "spreadsheet":
        return (
            '<office:spreadsheet><table:table table:name="Sheet1">'
            '<table:table-row><table:table-cell office:value-type="string">'
            + paragraph +
            '</table:table-cell></table:table-row></table:table>'
            '</office:spreadsheet>'
        )
    element = "office:presentation" if family == "presentation" else "office:drawing"
    return (
        '<' + element + '>'
        '<draw:page draw:name="page1" draw:style-name="dp1" '
        'draw:master-page-name="Default">'
        '<draw:frame draw:style-name="gr1" svg:width="10cm" svg:height="2cm" '
        'svg:x="1cm" svg:y="1cm"><draw:text-box>' + paragraph +
        '</draw:text-box></draw:frame></draw:page>'
        '</' + element + '>'
    )


def _content_xml(family: str, body: str, annot: Optional[str]) -> bytes:
    return (
        '<?xml version="1.0" encoding="UTF-8"?>\n'
        '<office:document-content ' + _XMLNS + ' office:version="1.3">'
        '<office:automatic-styles>'
        '<style:style style:name="gr1" style:family="graphic"/>'
        '<style:style style:name="dp1" style:family="drawing-page"/>'
        '</office:automatic-styles>'
        '<office:body>' + _body(family, body, annot) + '</office:body>'
        '</office:document-content>'
    ).encode("utf-8")


def _styles_xml() -> bytes:
    """
    Carries an officeooo:rsid of its own, so the engine is proven to sweep
    styles.xml and not only content.xml.
    """
    return (
        '<?xml version="1.0" encoding="UTF-8"?>\n'
        '<office:document-styles ' + _XMLNS + ' office:version="1.3">'
        '<office:styles>'
        '<style:style style:name="Standard" style:family="paragraph" '
        'officeooo:rsid="' + RSID + '"/>'
        '</office:styles>'
        '<office:automatic-styles>'
        '<style:page-layout style:name="pm1">'
        '<style:page-layout-properties fo:page-width="28cm" fo:page-height="21cm"/>'
        '</style:page-layout>'
        '<style:style style:name="dp1" style:family="drawing-page"/>'
        '<style:style style:name="gr1" style:family="graphic"/>'
        '</office:automatic-styles>'
        '<office:master-styles>'
        '<style:master-page style:name="Default" style:page-layout-name="pm1" '
        'draw:style-name="dp1"/>'
        '</office:master-styles>'
        '</office:document-styles>'
    ).encode("utf-8")


def _settings_xml(printer: str, db: str) -> bytes:
    """
    settings.xml with every config-item on the engine's removal allowlist
    POPULATED, plus two layout flags that must survive.

    The four database and redline items were on the plan's removal list without
    ever having been observed carrying a value. Measured 2026-09-04 on a
    LibreOffice .odt: all four are written, and all four are empty. Populating
    them here is what turns "the schema defines this field" into a claim with a
    measurement behind it, which is the condition for keeping them on the list.
    """
    setup = base64.b64encode(printer_blob(printer)).decode("ascii")
    redline = base64.b64encode(db.encode("utf-8")).decode("ascii")
    return (
        '<?xml version="1.0" encoding="UTF-8"?>\n'
        '<office:document-settings ' + _XMLNS + ' '
        'xmlns:config="urn:oasis:names:tc:opendocument:xmlns:config:1.0" '
        'office:version="1.3"><office:settings>'
        '<config:config-item-set config:name="ooo:view-settings">'
        '<config:config-item config:name="PrinterIndependentLayout" '
        'config:type="string">high-resolution</config:config-item>'
        '</config:config-item-set>'
        '<config:config-item-set config:name="ooo:configuration-settings">'
        '<config:config-item config:name="PrinterName" config:type="string">'
        + printer + '</config:config-item>'
        '<config:config-item config:name="PrinterSetup" '
        'config:type="base64Binary">' + setup + '</config:config-item>'
        '<config:config-item config:name="PrintFaxName" config:type="string">'
        + db + '</config:config-item>'
        '<config:config-item config:name="RedlineProtectionKey" '
        'config:type="base64Binary">' + redline + '</config:config-item>'
        '<config:config-item config:name="CurrentDatabaseDataSource" '
        'config:type="string">' + db + '</config:config-item>'
        '<config:config-item config:name="CurrentDatabaseCommand" '
        'config:type="string">' + db + '</config:config-item>'
        '<config:config-item config:name="EmbeddedDatabaseName" '
        'config:type="string">' + db + '</config:config-item>'
        '<config:config-item config:name="Rsid" config:type="int">48189'
        '</config:config-item>'
        '<config:config-item config:name="RsidRoot" config:type="int">48189'
        '</config:config-item>'
        '<config:config-item config:name="UseFormerLineSpacing" '
        'config:type="boolean">false</config:config-item>'
        '<config:config-item config:name="SaveThumbnail" '
        'config:type="boolean">true</config:config-item>'
        '</config:config-item-set>'
        '</office:settings></office:document-settings>'
    ).encode("utf-8")


_RDF_HEAD = '<?xml version="1.0" encoding="utf-8"?>\n<rdf:RDF ' \
            'xmlns:rdf="http://www.w3.org/1999/02/22-rdf-syntax-ns#" ' \
            'xmlns:dc="http://purl.org/dc/elements/1.1/">\n'


def _manifest_rdf(custom: Optional[str]) -> bytes:
    extra = ""
    if custom:
        extra = "    <dc:creator>" + custom + "</dc:creator>\n"
    return (
        _RDF_HEAD +
        '  <rdf:Description rdf:about="">\n'
        '    <rdf:type rdf:resource="'
        'http://docs.oasis-open.org/ns/office/1.2/meta/pkg#Document"/>\n'
        + extra +
        '  </rdf:Description>\n'
        '</rdf:RDF>\n'
    ).encode("utf-8")


def _extra_rdf(value: str) -> bytes:
    return (
        _RDF_HEAD +
        '  <rdf:Description rdf:about="content.xml">\n'
        '    <dc:creator>' + value + '</dc:creator>\n'
        '  </rdf:Description>\n'
        '</rdf:RDF>\n'
    ).encode("utf-8")


def _manifest_xml(mimetype: str, members) -> bytes:
    entries = [
        '<manifest:file-entry manifest:full-path="/" manifest:version="1.3" '
        'manifest:media-type="' + mimetype + '"/>'
    ]
    media = {
        ".xml": "text/xml",
        ".rdf": "application/rdf+xml",
        ".png": "image/png",
    }
    for name in members:
        if name in ("mimetype", "META-INF/manifest.xml"):
            continue
        if name.endswith("/"):
            kind = "application/vnd.sun.xml.ui.configuration"
        else:
            kind = media.get(os.path.splitext(name)[1], "application/octet-stream")
        entries.append(
            '<manifest:file-entry manifest:full-path="' + name +
            '" manifest:media-type="' + kind + '"/>'
        )
    return (
        '<?xml version="1.0" encoding="UTF-8"?>\n'
        '<manifest:manifest '
        'xmlns:manifest="urn:oasis:names:tc:opendocument:xmlns:manifest:1.0" '
        'manifest:version="1.3">\n ' + "\n ".join(entries) + '\n'
        '</manifest:manifest>\n'
    ).encode("utf-8")


def build_odf(ext: str, path: str, value: str, *, thumbnail: bool = True,
              annotations: bool = True, custom_rdf: bool = True,
              extra_rdf: bool = True) -> str:
    """
    Hand-write one OpenDocument package carrying the sentinel family.

    `mimetype` is written first and STORED, which is what a real ODF writer
    does and what the engine must preserve. Every other member is deflated, the
    way LibreOffice writes them, so the fixture does not accidentally make the
    engine's job easier than reality.
    """
    if ext not in MIMETYPES:
        raise AssertionError(f"no ODF fixture recipe for {ext}")
    marks = sentinels(value)
    family = FAMILY[ext]
    annot = marks["annot"] if (annotations and family == "text") else None

    members = []
    payload = {}

    def add(name: str, data: bytes) -> None:
        members.append(name)
        payload[name] = data

    add("manifest.rdf", _manifest_rdf(marks["rdf"] if custom_rdf else None))
    add("Configurations2/", b"")
    add("styles.xml", _styles_xml())
    add("settings.xml", _settings_xml(marks["printer"], marks["db"]))
    add("meta.xml", _meta_xml(marks["meta"]))
    if thumbnail:
        add("Thumbnails/thumbnail.png", THUMBNAIL_PNG)
    add("content.xml", _content_xml(family, marks["body"], annot))
    if extra_rdf:
        add("metadata/custom.rdf", _extra_rdf(marks["rdf"]))

    manifest = _manifest_xml(MIMETYPES[ext], members)

    with zipfile.ZipFile(path, "w", zipfile.ZIP_DEFLATED) as zf:
        info = zipfile.ZipInfo("mimetype", date_time=(2019, 3, 4, 11, 22, 33))
        info.compress_type = zipfile.ZIP_STORED
        zf.writestr(info, MIMETYPES[ext].encode("ascii"))
        for name in members:
            item = zipfile.ZipInfo(name, date_time=(2019, 3, 4, 11, 22, 33))
            item.compress_type = (
                zipfile.ZIP_STORED if name.endswith("/") else zipfile.ZIP_DEFLATED
            )
            zf.writestr(item, payload[name])
        item = zipfile.ZipInfo("META-INF/manifest.xml",
                               date_time=(2019, 3, 4, 11, 22, 33))
        item.compress_type = zipfile.ZIP_DEFLATED
        zf.writestr(item, manifest)
    return path


# THE LIBREOFFICE CONTROL LAYER

_SEED_FOR = {".odt": ".docx", ".ott": ".docx", ".ods": ".xlsx", ".ots": ".xlsx",
             ".odp": ".pptx", ".otp": ".pptx", ".odg": ".pptx", ".otg": ".pptx"}


def _seed_docx(path: str, value: str) -> None:
    import docx

    document = docx.Document()
    document.add_paragraph("visible body text that must survive")
    props = document.core_properties
    props.author = value
    props.last_modified_by = value
    props.title = value
    props.subject = value
    props.keywords = value
    props.comments = value
    document.save(path)


def _seed_xlsx(path: str, value: str) -> None:
    import openpyxl

    workbook = openpyxl.Workbook()
    workbook.active["A1"] = "visible cell value"
    props = workbook.properties
    props.creator = value
    props.lastModifiedBy = value
    props.title = value
    props.subject = value
    workbook.save(path)


def _seed_pptx(path: str, value: str) -> None:
    import pptx

    presentation = pptx.Presentation()
    presentation.slides.add_slide(presentation.slide_layouts[6])
    props = presentation.core_properties
    props.author = value
    props.last_modified_by = value
    props.title = value
    presentation.save(path)


_SEED_BUILDERS = {".docx": _seed_docx, ".xlsx": _seed_xlsx, ".pptx": _seed_pptx}


def libreoffice_odf(ext: str, outdir: str, value: str, soffice: Optional[str],
                    convert: Callable) -> Optional[str]:
    """
    Build one ODF document through LibreOffice's own export filter.

    `soffice` and `convert` are passed in rather than looked up here, so this
    module reuses the one profile-isolated converter in
    tests/fixtures/ole2/build_ole2.py instead of starting a second LibreOffice
    profile of its own. Sharing that matters: LibreOffice serialises on its user
    profile and reports the contention as a timeout, so the number of profiles
    in a test run is a property worth keeping at one.

    Returns None for every failure mode. The caller must translate None into a
    skip.
    """
    if soffice is None:
        return None
    seed_ext = _SEED_FOR[ext]
    os.makedirs(outdir, exist_ok=True)
    seed = os.path.join(outdir, "odfseed" + ext.replace(".", "") + seed_ext)
    _SEED_BUILDERS[seed_ext](seed, value)
    produced = convert(soffice, ext, seed, outdir)
    if produced is None:
        return None
    # Checked rather than trusted: a conversion that quietly produced something
    # that is not a zip would make every test after it exercise the wrong
    # container.
    if not zipfile.is_zipfile(produced):
        return None
    with zipfile.ZipFile(produced) as zf:
        if not zf.namelist() or zf.namelist()[0] != "mimetype":
            return None
    return produced


if __name__ == "__main__":  # pragma: no cover - manual inspection aid
    import sys

    out = sys.argv[1] if len(sys.argv) > 1 else "."
    os.makedirs(out, exist_ok=True)
    for extension in sorted(MIMETYPES):
        built = build_odf(extension, os.path.join(out, "probe" + extension),
                          "METASCRUBMANUALPROBE9999")
        print(extension, built, os.path.getsize(built))
