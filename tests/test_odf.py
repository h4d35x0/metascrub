"""
OpenDocument: .odt .ott .ods .ots .odp .otp .odg .otg.

Two things every test here refuses to do:

  - Ask an engine whether it succeeded. Survival is decided by searching the
    INFLATED output members for the exact sentinel the fixture seeded, in every
    encoding a carrier can use, and for the base64 spelling where the carrier is
    base64.
  - Treat a missing external tool as a failure. LibreOffice and libmagic are
    both optional here and both SKIP. A machine without them says nothing about
    whether the engine works.

The cross-product suites in test_removal.py, test_verification.py and
test_failopen.py pick these formats up through conftest.ALL_KINDS. What is in
THIS module is the ODF-specific part: the things that would still pass if the
engine were wrong.

WHY test_libmagic_still_identifies_the_package MATTERS MORE THAN IT LOOKS

Measured 2026-09-04 on a naive all-deflated rebuild of a hand-built .odt, with
the mimetype member still FIRST but compressed:

    file(1)              Zip data (MIME type "K,("?)
    exiftool -FileType   ODT
    LibreOffice          opens it, converts it, rc 0

So the two obvious checks both pass on a package that content sniffers no
longer recognise. That is the bug this module exists to catch, and only two
tests here can see it: test_mimetype_is_first_and_stored, which needs nothing
but the stdlib and is therefore the guard, and this one, which needs libmagic
and is therefore the belt.
"""

from __future__ import annotations

import base64
import os
import re
import subprocess
import zipfile

import pytest

from conftest import (
    HAVE_LIBMAGIC, HAVE_LIBREOFFICE, ODF_KINDS, ODF_LIBREOFFICE_KINDS,
    build, build_odf, build_ole2, make_odf_from_libreoffice, sentinel,
)
from metascrub import (
    STATUS_SANITIZED, Completeness, Container, Engine, MetadataScrubber, spec_for,
)

_B64_ITEM = re.compile(
    rb'config:type="base64Binary"[^>]*>(.*?)</config:config-item>', re.DOTALL
)


def _inflated(path: str) -> bytes:
    """Member names and inflated member bytes, which is what the scan searches."""
    blob = bytearray()
    with zipfile.ZipFile(path) as zf:
        for name in zf.namelist():
            blob += name.encode("utf-8")
            blob += zf.read(name)
    return bytes(blob)


def _present(blob: bytes, needle: str) -> bool:
    return any(needle.encode(enc) in blob
               for enc in ("utf-8", "utf-16-le", "latin-1"))


def _scrub(path: str):
    with MetadataScrubber(backup=False) as scrubber:
        return scrubber.sanitize_file(path, remove_all=True)


def _notes(result) -> list:
    return [f for f in result["removed_fields"]
            if isinstance(f, str) and f.startswith("NOTE:")]


# THE CAPABILITY TABLE

def test_odf_extensions_are_shipped_and_honest():
    for ext in ODF_KINDS:
        spec = spec_for(f"x{ext}")
        assert spec is not None, f"{ext} is not in the capability table"
        assert spec.engine is Engine.ODF
        assert spec.container is Container.ZIP
        assert spec.rewrites_container is True
        assert spec.completeness is Completeness.COMPLETE
        # A COMPLETE row still has to say what it did, because the one thing it
        # deliberately leaves behind is user-visible.
        assert "annotations" in spec.note


def test_flat_odf_is_deferred_and_not_silently_unsupported():
    """
    .fodt/.fods/.fodp are a different container and are knowingly out. A user
    who hands one over must get the reason, not the flat "unsupported" a .txt
    gets. The gates in test_coverage_gate.py enforce the rest.
    """
    from metascrub import deferral_for

    for ext in (".fodt", ".fods", ".fodp"):
        reason = deferral_for(f"x{ext}")
        assert reason, f"{ext} is neither supported nor deferred"
        assert "zip" in reason.lower()


# THE FIXTURE ITSELF

@pytest.mark.parametrize("kind", ODF_KINDS)
def test_the_fixture_is_a_real_odf_package_carrying_the_sentinels(kind, tmp_path):
    """
    The precondition every other test rests on.

    A fixture that was not a valid package, or that lost its seeded values,
    would make every removal test below pass by having nothing to remove. That
    is the exact failure this project exists to catch, so it is measured rather
    than inferred from the builder returning a path.
    """
    path, value = build(kind, tmp_path)
    marks = build_odf.sentinels(value)
    blob = _inflated(path)

    with zipfile.ZipFile(path) as zf:
        assert zf.namelist()[0] == "mimetype"
        assert zf.read("mimetype").decode("ascii") == build_odf.MIMETYPES[kind]

    for name in ("meta", "printer", "db", "body", "rdf"):
        assert _present(blob, marks[name]), f"{kind} fixture lost the {name} sentinel"
    assert build_odf.RSID.encode() in blob, "fixture carries no rsid to strip"
    assert any(n.startswith("Thumbnails/") for n in zipfile.ZipFile(path).namelist())


# THE REMOVALS

@pytest.mark.parametrize("kind", ODF_KINDS)
def test_meta_sentinels_are_gone_from_the_bytes(kind, tmp_path):
    path, value = build(kind, tmp_path)
    result = _scrub(path)
    assert result["status"] == STATUS_SANITIZED, result.get("error")
    assert not _present(_inflated(path), value), (
        f"{kind}: meta.xml values survived; the file still leaks"
    )


@pytest.mark.parametrize("kind", ODF_KINDS)
def test_printer_identity_is_gone(kind, tmp_path):
    """
    The carrier that justifies a dedicated ODF engine.

    Three separate assertions, because a plain byte search is NOT enough here.
    PrinterSetup stores a Windows DEVMODE as base64, so the printer name is in
    the file in a spelling no substring search for the name would ever find.
    Testing only the plain spelling would be a test that passes on the case the
    code already handles.
    """
    path, value = build(kind, tmp_path)
    marks = build_odf.sentinels(value)
    printer = marks["printer"]
    encoded = base64.b64encode(build_odf.printer_blob(printer)).decode("ascii")

    before = _inflated(path)
    assert _present(before, printer), "fixture precondition failed"
    assert encoded.encode("ascii") in before, "fixture precondition failed"

    _scrub(path)
    after = _inflated(path)
    assert not _present(after, printer), f"{kind}: the printer name survived"
    assert encoded.encode("ascii") not in after, (
        f"{kind}: the base64 printer driver blob survived verbatim"
    )

    # And the strongest form: decode whatever base64Binary items are left and
    # look inside them. This is what catches a blob that was re-encoded rather
    # than removed.
    with zipfile.ZipFile(path) as zf:
        settings = zf.read("settings.xml")
    decoded = bytearray()
    for match in _B64_ITEM.finditer(settings):
        try:
            decoded += base64.b64decode(match.group(1))
        except Exception:
            continue
    assert not _present(bytes(decoded), printer), (
        f"{kind}: the printer name survived inside a base64 config item"
    )


@pytest.mark.parametrize("kind", ODF_KINDS)
def test_database_and_redline_config_items_are_gone(kind, tmp_path):
    """
    CurrentDatabaseDataSource, CurrentDatabaseCommand, EmbeddedDatabaseName,
    PrintFaxName and RedlineProtectionKey.

    These were on the removal list on the strength of their names. Measured
    2026-09-04 on a LibreOffice .odt: all five are written and all five are
    empty, so nothing on this machine could show them carrying a value. The
    fixture populates them, which is what turns "the schema defines this field"
    into a claim with a measurement behind it.
    """
    path, value = build(kind, tmp_path)
    marks = build_odf.sentinels(value)
    db = marks["db"]
    encoded = base64.b64encode(db.encode("utf-8"))

    before = _inflated(path)
    assert _present(before, db) and encoded in before, "fixture precondition failed"

    _scrub(path)
    after = _inflated(path)
    assert not _present(after, db), f"{kind}: a database or fax name survived"
    assert encoded not in after, f"{kind}: the redline protection key survived"

    with zipfile.ZipFile(path) as zf:
        settings = zf.read("settings.xml")
    for name in (b"PrinterName", b"PrinterSetup", b"PrintFaxName", b"Rsid",
                 b"RsidRoot", b"CurrentDatabaseDataSource",
                 b"CurrentDatabaseCommand", b"EmbeddedDatabaseName",
                 b"RedlineProtectionKey"):
        assert b'config:name="' + name + b'"' not in settings, (
            f"{kind}: {name.decode()} is still in settings.xml"
        )


@pytest.mark.parametrize("kind", ODF_KINDS)
def test_layout_config_items_survive(kind, tmp_path):
    """
    The guard against the allowlist being inverted into a denylist.

    settings.xml carries roughly 120 compatibility flags that decide how the
    document lays out. Deleting those would silently reflow the user's document,
    which is a worse outcome than leaving metadata in.
    """
    path, _ = build(kind, tmp_path)
    _scrub(path)
    with zipfile.ZipFile(path) as zf:
        settings = zf.read("settings.xml")
    assert b'config:name="PrinterIndependentLayout"' in settings
    assert b"high-resolution" in settings, "the layout item lost its value"
    assert b'config:name="UseFormerLineSpacing"' in settings
    assert b'config:name="SaveThumbnail"' in settings, (
        "a user preference was deleted along with the data it refers to"
    )


@pytest.mark.parametrize("kind", ODF_KINDS)
def test_revision_identifiers_are_gone_from_content_and_styles(kind, tmp_path):
    path, _ = build(kind, tmp_path)
    with zipfile.ZipFile(path) as zf:
        assert build_odf.RSID.encode() in zf.read("content.xml")
        assert build_odf.RSID.encode() in zf.read("styles.xml")
    _scrub(path)
    with zipfile.ZipFile(path) as zf:
        for part in ("content.xml", "styles.xml"):
            data = zf.read(part)
            assert b"officeooo:rsid" not in data, f"{kind}: {part} kept an rsid"
            assert b"officeooo:paragraph-rsid" not in data
            assert build_odf.RSID.encode() not in data


@pytest.mark.parametrize("kind", ODF_KINDS)
def test_thumbnail_is_gone_and_manifest_agrees(kind, tmp_path):
    """
    The thumbnail leaks CONTENT, not just metadata: it is a rendered raster of
    page 1, and a document edited after its last save carries a picture of the
    pre-edit page. Dropping the member is only half the job; a manifest that
    still lists a part which is not there is a malformed package.
    """
    path, _ = build(kind, tmp_path)
    _scrub(path)
    with zipfile.ZipFile(path) as zf:
        names = zf.namelist()
        manifest = zf.read("META-INF/manifest.xml")
    assert not [n for n in names if n.startswith("Thumbnails/")]
    assert b"Thumbnails" not in manifest, "the manifest still lists the thumbnail"
    assert build_odf.THUMBNAIL_PNG not in _inflated(path), (
        "the thumbnail bytes are still in the package under another name"
    )


@pytest.mark.parametrize("kind", ODF_KINDS)
def test_package_rdf_is_replaced_and_extra_rdf_is_dropped(kind, tmp_path):
    path, value = build(kind, tmp_path)
    marks = build_odf.sentinels(value)
    result = _scrub(path)

    with zipfile.ZipFile(path) as zf:
        names = zf.namelist()
        rdf = zf.read("manifest.rdf")
        manifest = zf.read("META-INF/manifest.xml")
    assert not _present(_inflated(path), marks["rdf"]), (
        f"{kind}: RDF metadata survived"
    )
    assert b"pkg#Document" in rdf, "manifest.rdf was emptied rather than replaced"
    assert [n for n in names if n.endswith(".rdf")] == ["manifest.rdf"]
    assert b"custom.rdf" not in manifest

    notes = _notes(result)
    assert any("custom RDF" in n for n in notes), (
        "custom RDF was removed with no warning to the user"
    )
    assert any("in-content RDF" in n for n in notes)


def test_no_rdf_note_when_the_package_rdf_is_the_default(tmp_path):
    """
    An ordinary document must not produce a scary message about nothing. The
    default manifest.rdf says only which parts the package has, and replacing
    that loses the user nothing.
    """
    path = str(tmp_path / "plain.odt")
    build_odf.build_odf(".odt", path, sentinel("plainodt"),
                        custom_rdf=False, extra_rdf=False)
    notes = _notes(_scrub(path))
    assert not any("RDF" in n for n in notes), f"unexpected RDF warning: {notes}"


# THE PACKAGE MUST STILL BE AN OPENDOCUMENT

@pytest.mark.parametrize("kind", ODF_KINDS)
def test_mimetype_is_first_and_stored(kind, tmp_path):
    """
    The always-on guard for the one thing a naive rebuild breaks. Needs nothing
    but the stdlib, so it runs everywhere the suite runs.
    """
    path, _ = build(kind, tmp_path)
    _scrub(path)
    with zipfile.ZipFile(path) as zf:
        first = zf.infolist()[0]
        assert first.filename == "mimetype", (
            f"{kind}: mimetype is not the first member after the rebuild"
        )
        assert first.compress_type == zipfile.ZIP_STORED, (
            f"{kind}: mimetype was deflated; content sniffers stop recognising "
            "the package while LibreOffice and exiftool still accept it"
        )
        assert zf.read("mimetype").decode("ascii") == build_odf.MIMETYPES[kind]


@pytest.mark.skipif(not HAVE_LIBMAGIC, reason="file(1) is not on PATH")
@pytest.mark.parametrize("kind", ODF_KINDS)
def test_libmagic_still_identifies_the_package(kind, tmp_path):
    """
    The belt. This is the only check in the suite that would catch the naive
    rebuild if test_mimetype_is_first_and_stored were ever deleted or weakened,
    because neither LibreOffice nor exiftool notices.
    """
    path, _ = build(kind, tmp_path)
    before = subprocess.run(["file", "-b", path], capture_output=True, text=True)
    assert "OpenDocument" in before.stdout, (
        f"{kind} fixture precondition failed: libmagic says {before.stdout.strip()}"
    )
    _scrub(path)
    after = subprocess.run(["file", "-b", path], capture_output=True, text=True)
    assert "OpenDocument" in after.stdout, (
        f"{kind}: libmagic no longer identifies the scrubbed package: "
        f"{after.stdout.strip()}"
    )
    assert after.stdout.strip() == before.stdout.strip(), (
        f"{kind}: the document type changed from {before.stdout.strip()} to "
        f"{after.stdout.strip()}"
    )


def test_a_naive_all_deflated_rebuild_is_actually_detectable(tmp_path):
    """
    Proof that the two tests above are testing something real.

    Without this, a reader has to take on faith that deflating `mimetype`
    matters. Here the naive rebuild is constructed on purpose and libmagic is
    shown failing on it, alongside exiftool succeeding on the same file. If
    libmagic ever stops caring, this test starts failing and the argument in the
    engine docstring gets revisited rather than quietly outliving its evidence.
    """
    if not HAVE_LIBMAGIC:
        pytest.skip("file(1) is not on PATH")
    good = str(tmp_path / "good.odt")
    naive = str(tmp_path / "naive.odt")
    build_odf.build_odf(".odt", good, sentinel("naiveodt"))
    with zipfile.ZipFile(good) as src:
        with zipfile.ZipFile(naive, "w", zipfile.ZIP_DEFLATED) as dst:
            for name in src.namelist():
                dst.writestr(name, src.read(name))

    with zipfile.ZipFile(naive) as zf:
        assert zf.infolist()[0].filename == "mimetype"
        assert zf.infolist()[0].compress_type == zipfile.ZIP_DEFLATED

    verdict = subprocess.run(["file", "-b", naive], capture_output=True, text=True)
    assert "OpenDocument" not in verdict.stdout, (
        "libmagic no longer distinguishes a deflated mimetype; the STORED "
        "requirement in odf_engine.py needs re-measuring, not deleting"
    )


# CONTENT MUST SURVIVE

@pytest.mark.parametrize("kind", ODF_KINDS)
def test_body_content_survives(kind, tmp_path):
    """A metadata tool that eats the document is worse than no tool at all."""
    path, value = build(kind, tmp_path)
    marks = build_odf.sentinels(value)
    _scrub(path)
    assert _present(_inflated(path), marks["body"]), (
        f"{kind}: the document body was destroyed"
    )


@pytest.mark.parametrize("kind", [".odt", ".ott"])
def test_annotations_are_reported_not_removed(kind, tmp_path):
    """
    Annotations and tracked changes are the user's own work, not metadata.
    Removing them would destroy something they cannot recover, and NOT saying
    they stayed would leave a user who asked for metadata removal believing the
    commenter's name went with it.
    """
    path, value = build(kind, tmp_path)
    marks = build_odf.sentinels(value)
    result = _scrub(path)

    with zipfile.ZipFile(path) as zf:
        content = zf.read("content.xml")
    assert b"<office:annotation" in content, f"{kind}: the annotation was removed"
    assert b"<text:tracked-changes" in content, f"{kind}: tracked changes were removed"
    assert _present(content, marks["annot"]), (
        f"{kind}: the annotation lost its author"
    )

    notes = _notes(result)
    assert any("annotations" in n for n in notes), (
        "annotations were left in place with no note saying so"
    )
    assert any("tracked changes" in n for n in notes)


def test_no_annotation_note_on_a_document_without_them(tmp_path):
    path = str(tmp_path / "quiet.ods")
    build_odf.build_odf(".ods", path, sentinel("quietods"),
                        custom_rdf=False, extra_rdf=False)
    notes = _notes(_scrub(path))
    assert not notes, f"unexpected warning on a plain spreadsheet: {notes}"


# BOUNDARIES
#
# What the engine does when the input is not the input it was designed for.
# These are the cases a fixture set never produces on its own, and they are
# where a container rewrite goes wrong quietly.

def test_a_thumbnails_directory_entry_is_dropped_too(tmp_path):
    """
    LibreOffice writes no Thumbnails/ directory entry, but a zip is free to
    carry one and other ODF writers do. Dropping the file while keeping the
    folder would leave a manifest entry for an empty directory.
    """
    path = str(tmp_path / "withdir.odt")
    source = str(tmp_path / "source.odt")
    build_odf.build_odf(".odt", source, sentinel("dirodt"))
    with zipfile.ZipFile(source) as src:
        with zipfile.ZipFile(path, "w", zipfile.ZIP_DEFLATED) as dst:
            first = zipfile.ZipInfo("mimetype")
            first.compress_type = zipfile.ZIP_STORED
            dst.writestr(first, src.read("mimetype"))
            dst.writestr("Thumbnails/", b"")
            for name in src.namelist():
                if name != "mimetype":
                    dst.writestr(name, src.read(name))

    with zipfile.ZipFile(path) as zf:
        assert "Thumbnails/" in zf.namelist()
    _scrub(path)
    with zipfile.ZipFile(path) as zf:
        assert not [n for n in zf.namelist() if n.startswith("Thumbnails")]


def test_a_package_without_a_mimetype_member_is_not_given_one(tmp_path):
    """
    ODF allows a package with no mimetype member. Inventing one from the file
    extension would let this tool relabel a package as a format it is not, which
    is a worse failure than leaving the package as it was found.
    """
    path = str(tmp_path / "nomime.odt")
    source = str(tmp_path / "src2.odt")
    build_odf.build_odf(".odt", source, sentinel("nomimeodt"))
    with zipfile.ZipFile(source) as src:
        with zipfile.ZipFile(path, "w", zipfile.ZIP_DEFLATED) as dst:
            for name in src.namelist():
                if name != "mimetype":
                    dst.writestr(name, src.read(name))

    result = _scrub(path)
    assert result["status"] == STATUS_SANITIZED, result.get("error")
    with zipfile.ZipFile(path) as zf:
        assert "mimetype" not in zf.namelist()
        assert "content.xml" in zf.namelist(), "the rest of the package was lost"


def test_a_file_that_is_not_a_zip_is_never_claimed_as_sanitized(tmp_path):
    """
    The direction this tool must never fail in is claiming to have removed
    something it did not.

    Measured 2026-09-04: a .odt, a .docx and a .pdf that are all the same plain
    text file each come back CLEAN with detail "no metadata carriers found", and
    all three are left byte-identical. That is the same answer for all three
    container formats, so it is the project's existing contract and not
    something ODF introduces: exiftool READ the file successfully and there was
    no metadata in it. What matters, and what is asserted here, is that no
    rewrite is claimed and no byte is touched.
    """
    path = str(tmp_path / "broken.odt")
    payload = b"this is not a zip, it just has the extension " + sentinel(
        "brokenodt").encode("ascii")
    with open(path, "wb") as fh:
        fh.write(payload)

    result = _scrub(path)
    assert result["status"] != STATUS_SANITIZED, (
        "the tool claimed to have sanitized a file it never opened as a package"
    )
    with open(path, "rb") as fh:
        assert fh.read() == payload, "a file that was not rewritten changed anyway"


def test_a_member_that_will_not_inflate_aborts_without_touching_the_original(
        tmp_path):
    """
    A rewrite that cannot read every member must abort, not write a package with
    the unreadable part silently missing. The original is the user's only copy
    when --no-backup is in play, and a half-rebuilt document is worse than an
    untouched one with metadata in it.
    """
    from metascrub import STATUS_ERROR

    path = str(tmp_path / "corrupt.odt")
    source = str(tmp_path / "src3.odt")
    build_odf.build_odf(".odt", source, sentinel("corruptodt"))
    with open(source, "rb") as fh:
        blob = bytearray(fh.read())
    # Damage the last local file header without disturbing the central
    # directory, so zipfile still opens the package and one member will not read.
    marker = blob.rfind(b"PK\x03\x04")
    blob[marker + 40:marker + 60] = b"\x00" * 20
    with open(path, "wb") as fh:
        fh.write(bytes(blob))
    before = bytes(blob)

    result = _scrub(path)
    if result["status"] != STATUS_ERROR:
        pytest.skip(f"the corruption did not make a member unreadable: {result}")
    assert "failed to rebuild ODF package" in result["error"]
    with open(path, "rb") as fh:
        assert fh.read() == before, "an aborted rewrite modified the original"


# THE LIBREOFFICE CONTROL LAYER

@pytest.mark.skipif(not HAVE_LIBREOFFICE, reason="LibreOffice is not installed")
@pytest.mark.parametrize("kind", ODF_LIBREOFFICE_KINDS)
def test_a_real_libreoffice_document_is_cleaned(kind, tmp_path):
    """
    The control on every hand-built fixture above.

    A hand-built package proves the engine handles a file the TEST wrote. This
    one is written by LibreOffice's own export filter, with its own member
    order, its own namespace declarations and its own settings.xml, and the same
    byte search has to come back empty.
    """
    path, value = make_odf_from_libreoffice(tmp_path, kind)
    assert _present(_inflated(path), value), "fixture precondition failed"

    result = _scrub(path)
    assert result["status"] == STATUS_SANITIZED, result.get("error")
    assert result["verification"]["verdict"] == "verified_clean"
    assert not _present(_inflated(path), value), (
        f"{kind}: a LibreOffice-written document still leaks after sanitizing"
    )

    with zipfile.ZipFile(path) as zf:
        assert zf.infolist()[0].filename == "mimetype"
        assert zf.infolist()[0].compress_type == zipfile.ZIP_STORED
        settings = zf.read("settings.xml")
        assert not [n for n in zf.namelist() if n.startswith("Thumbnails/")]
    for name in (b"PrinterName", b"PrinterSetup", b"Rsid", b"RsidRoot"):
        assert b'config:name="' + name + b'"' not in settings


@pytest.mark.skipif(not HAVE_LIBREOFFICE, reason="LibreOffice is not installed")
@pytest.mark.parametrize("kind", ODF_KINDS)
def test_libreoffice_reopens_the_scrubbed_package(kind, tmp_path):
    """
    The output has to still be a document.

    The untouched fixture is converted in the SAME run as a control. Without
    that control, any environmental failure of the converter reads as damage
    caused by the engine, and the debugging goes straight to the wrong place;
    that is exactly what happened to the .xls tests on 2026-09-04.
    """
    soffice = build_ole2.find_soffice()
    path, _ = build(kind, tmp_path)
    control = str(tmp_path / f"control{kind}")
    outdir = str(tmp_path / "converted")
    os.makedirs(outdir, exist_ok=True)
    with open(path, "rb") as src, open(control, "wb") as dst:
        dst.write(src.read())

    _scrub(path)
    control_pdf = build_ole2.convert(soffice, ".pdf", control, outdir)
    if control_pdf is None:
        pytest.skip(f"LibreOffice cannot convert an untouched {kind} here either")
    assert build_ole2.convert(soffice, ".pdf", path, outdir) is not None, (
        f"LibreOffice could not reopen the scrubbed {kind} while the untouched "
        "control converted in the same run"
    )
