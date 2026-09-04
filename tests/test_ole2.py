"""
Legacy OLE2 compound files: .doc, .xls, .ppt.

This module is half of the discharge condition for the OLE2 deferral;
tests/fixtures/ole2/ is the other half, and tests/test_coverage_gate.py demands
both before the extensions may appear in the capability table.

Two things every test here refuses to do:

  - Ask an engine whether it succeeded. Survival is decided by searching the
    OUTPUT BYTES for the exact sentinel the fixture seeded, in all three
    encodings a property stream can use.
  - Treat a missing LibreOffice as a failure. It is a skip. A machine without
    the converter says nothing about whether the engine works.

The fixtures come from LibreOffice's MS Word 97 / MS Excel 97 / MS PowerPoint 97
export filters, not from Microsoft Office. That bounds what these tests prove
and is stated in tests/fixtures/ole2/README.md; the .doc row in capabilities.py
is PARTIAL for the same reason.
"""

from __future__ import annotations

import os
import shutil
import struct

import pytest

from conftest import (
    HAVE_LIBREOFFICE, build, build_ole2, contains_anywhere, raw_contains, sentinel,
)
from metascrub import (
    STATUS_ERROR, STATUS_SANITIZED, Completeness, Engine, MetadataScrubber, spec_for,
)

olefile = pytest.importorskip("olefile")

OLE2_KINDS = [".doc", ".xls", ".ppt"]

_SUMMARY = "\x05SummaryInformation"
_DOCSUMMARY = "\x05DocumentSummaryInformation"


def _streams(path):
    ole = olefile.OleFileIO(path)
    try:
        return {"/".join(entry): ole.openstream(entry).read()
                for entry in ole.listdir(streams=True)}
    finally:
        ole.close()


# THE CAPABILITY TABLE

def test_ole2_extensions_are_shipped_and_honest():
    """
    The deferral is discharged, and it is discharged honestly.

    PARTIAL is asserted rather than merely allowed. Measured with exiftool 13.59
    on a scrubbed .doc, the [MS-DOC] group still reports CreateDate, ModifyDate,
    RevisionNumber, TotalEditTime and the document statistics out of the DOP in
    the WordDocument/Table streams. A row claiming COMPLETE would be a promise
    this engine does not keep.
    """
    for ext in OLE2_KINDS:
        spec = spec_for(f"x{ext}")
        assert spec is not None, f"{ext} is not in the capability table"
        assert spec.engine is Engine.OLE2
        assert spec.completeness is Completeness.PARTIAL
        assert "surviv" in spec.note, f"{ext} is PARTIAL without saying what remains"
    # The notes must not be one shared string: the carriers differ per format,
    # and a .doc row promising Excel's user name was handled is noise a reader
    # cannot tell from a kept promise.
    notes = {spec_for(f"x{ext}").note for ext in OLE2_KINDS}
    assert len(notes) == len(OLE2_KINDS), "the OLE2 rows share one generic note"


# THE FIXTURE ITSELF

@pytest.mark.parametrize("kind", OLE2_KINDS)
def test_the_fixture_is_a_real_compound_file_carrying_the_sentinel(kind, tmp_path):
    """
    The precondition every other test here rests on.

    A fixture that was not actually an OLE2 file, or that lost its metadata in
    conversion, would make every removal test below pass by having nothing to
    remove. That is the failure mode this project exists to catch, so it is
    checked directly rather than inferred from the converter's exit status.
    """
    path, value = build(kind, tmp_path)
    with open(path, "rb") as handle:
        assert handle.read(8) == b"\xd0\xcf\x11\xe0\xa1\xb1\x1a\xe1", (
            f"{kind} fixture is not an OLE2 compound file"
        )
    assert raw_contains(path, value)

    streams = _streams(path)
    assert _SUMMARY in streams, f"{kind} fixture has no summary property stream"
    # Every sentinel occurrence must be accounted for inside the property
    # streams. If one lives somewhere else, the engine's stated scope is wrong
    # and the note in capabilities.py is understating what survives.
    with open(path, "rb") as handle:
        in_file = _count(handle.read(), value)
    in_property_streams = sum(
        _count(data, value)
        for name, data in streams.items()
        if name.split("/")[-1] in (_SUMMARY, _DOCSUMMARY)
    )
    assert in_file > 0
    assert in_property_streams == in_file, (
        f"{kind}: {in_file - in_property_streams} sentinel occurrence(s) live "
        "outside the property streams this engine cleans"
    )


def _count(blob: bytes, needle: str) -> int:
    return sum(blob.count(needle.encode(e)) for e in ("utf-8", "utf-16-le"))


# REMOVAL, MEASURED IN THE BYTES

@pytest.mark.parametrize("kind", OLE2_KINDS)
def test_sentinel_is_gone_from_the_bytes(kind, tmp_path):
    """The load-bearing test. No engine is asked for its opinion."""
    path, value = build(kind, tmp_path)
    assert contains_anywhere(path, value), "fixture precondition failed"

    with MetadataScrubber(backup=False) as scrubber:
        result = scrubber.sanitize_file(path, remove_all=True)

    assert result["status"] == STATUS_SANITIZED, result.get("error")
    assert result["engine"] == "ole2"
    assert not contains_anywhere(path, value), (
        f"{kind}: the metadata value survived; the file still leaks"
    )


@pytest.mark.parametrize("kind", OLE2_KINDS)
def test_property_streams_are_emptied_not_merely_shortened(kind, tmp_path):
    """
    The old property set bytes must be gone, not just unreferenced.

    This is the PDF incremental-update mistake in a different container: leaving
    the previous values in the allocation while the header no longer points at
    them means every value is still recoverable, and a tool that reported that
    file clean would be wrong in exactly the way this project exists to fix.
    """
    path, value = build(kind, tmp_path)
    before = _streams(path)
    with MetadataScrubber(backup=False) as scrubber:
        scrubber.sanitize_file(path, remove_all=True)
    after = _streams(path)

    for name in (_SUMMARY, _DOCSUMMARY):
        if name not in before:
            continue
        # olefile cannot resize a stream, so the allocation is expected to be
        # identical. That is the constraint the engine is designed around.
        assert len(after[name]) == len(before[name]), (
            f"{kind}: {name!r} changed length; the compound file was restructured"
        )
        assert _count(after[name], value) == 0
        # A valid, empty property set: byte order marker, then a property set
        # declaring zero properties, then nothing but zero padding.
        assert struct.unpack("<H", after[name][:2])[0] == 0xFFFE
        (set_count,) = struct.unpack("<I", after[name][24:28])
        for i in range(set_count):
            (offset,) = struct.unpack("<I", after[name][28 + i * 20 + 16:48 + i * 20])
            size, properties = struct.unpack("<II", after[name][offset:offset + 8])
            assert (size, properties) == (8, 0), (
                f"{kind}: {name!r} set {i} still declares {properties} properties"
            )
        tail_start = 28 + 20 * set_count + 8 * set_count
        assert set(after[name][tail_start:]) <= {0}, (
            f"{kind}: {name!r} still carries the old property bytes after the "
            "empty property set"
        )


@pytest.mark.parametrize("kind", OLE2_KINDS)
def test_the_application_user_type_is_removed(kind, tmp_path):
    """
    \\001CompObj names the producing application and its UI language.

    Measured 2026-09-04: LibreOffice writes "Microsoft Word-Dokument" there on
    an English-language document, so this field discloses the writer's locale
    even when every property stream is empty.
    """
    path, _ = build(kind, tmp_path)
    before = _streams(path).get("\x01CompObj")
    if before is None:
        pytest.skip(f"{kind} fixture has no CompObj stream")
    (length,) = struct.unpack("<I", before[28:32])
    original = before[32:32 + length - 1]
    assert original.strip(b"\x00 "), "fixture precondition: CompObj user type is empty"

    with MetadataScrubber(backup=False) as scrubber:
        scrubber.sanitize_file(path, remove_all=True)

    after = _streams(path)["\x01CompObj"]
    assert len(after) == len(before)
    assert original not in after, f"{kind}: the CompObj user type survived"
    assert after[32:32 + length - 1] == b" " * (length - 1)


def test_powerpoint_last_editor_name_is_removed(tmp_path):
    """
    The `Current User` stream holds the name of the last person to edit the
    presentation. exiftool reports it as FlashPix:CurrentUser, and it survives a
    property stream wipe completely.
    """
    path, _ = build(".ppt", tmp_path)
    before = _streams(path).get("Current User")
    if before is None:
        pytest.skip("ppt fixture has no Current User stream")
    (name_len,) = struct.unpack("<H", before[20:22])
    original = before[28:28 + name_len]
    assert original.strip(), "fixture precondition: the user name is already blank"

    with MetadataScrubber(backup=False) as scrubber:
        scrubber.sanitize_file(path, remove_all=True)

    after = _streams(path)["Current User"]
    assert len(after) == len(before)
    assert after[28:28 + name_len] == b" " * name_len
    assert original not in after, "the PowerPoint last-editor name survived"


def test_excel_write_access_user_name_is_removed(tmp_path):
    """
    BIFF record 0x005C carries Excel's user name in the Workbook globals. It is
    not in a property stream and is not touched by emptying one.
    """
    path, _ = build(".xls", tmp_path)
    before = _streams(path)["Workbook"]
    offset = _find_write_access(before)
    if offset is None:
        pytest.skip("xls fixture has no WRITEACCESS record")
    original = before[offset + 4: offset + 4 + 112]
    assert original.strip(b"\x00 "), "fixture precondition: WRITEACCESS is blank"

    with MetadataScrubber(backup=False) as scrubber:
        scrubber.sanitize_file(path, remove_all=True)

    after = _streams(path)["Workbook"]
    assert len(after) == len(before)
    assert after[offset:offset + 4] == before[offset:offset + 4], (
        "the record header moved; the workbook was restructured"
    )
    assert after[offset + 4: offset + 7] == b"\x00\x00\x00", (
        "WRITEACCESS still declares a user name"
    )
    assert original not in after, "the Excel user name survived"


def _find_write_access(data: bytes):
    cursor = 0
    while cursor + 4 <= len(data) and cursor < 8192:
        record_id, record_len = struct.unpack("<HH", data[cursor:cursor + 4])
        if record_id == 0x005C and record_len == 112:
            return cursor
        if record_id == 0x000A:
            return None
        cursor += 4 + record_len
    return None


# THE FILE MUST STILL BE A DOCUMENT AFTERWARDS

@pytest.mark.parametrize("kind", OLE2_KINDS)
def test_the_compound_file_structure_is_untouched(kind, tmp_path):
    """
    Only stream payload may change. The header, FAT, mini-FAT, directory entries
    and sector chains must be byte-identical, because a compound file whose
    structure this tool rewrote is a compound file this tool could corrupt.

    Three checks, each a different kind of evidence:

      - the compound-file header, compared byte for byte;
      - the stream inventory and every stream's length, which olefile
        reconstructs by walking the directory, FAT and mini-FAT, so an identical
        result means the walk found the same graph;
      - every stream the engine does not target, compared byte for byte.
    """
    targeted = {_SUMMARY, _DOCSUMMARY, "\x01CompObj", "Current User", "Workbook"}
    path, _ = build(kind, tmp_path)
    original = str(tmp_path / f"original{kind}")
    shutil.copyfile(path, original)

    with MetadataScrubber(backup=False) as scrubber:
        scrubber.sanitize_file(path, remove_all=True)

    before, after = _streams(original), _streams(path)
    assert sorted(before) == sorted(after), "the stream inventory changed"
    for name in before:
        assert len(after[name]) == len(before[name]), f"{name!r} changed length"
        if name.split("/")[-1] not in targeted:
            assert after[name] == before[name], (
                f"{name!r} was modified and the engine does not target it"
            )
    assert os.path.getsize(path) == os.path.getsize(original), "the file size changed"

    # The header carries the sector shift, the FAT sector count, the first
    # directory sector and the DIFAT. If any of those moved, the file was
    # restructured whatever the stream walk says.
    with open(original, "rb") as a, open(path, "rb") as b:
        assert a.read(512) == b.read(512), "the compound file header changed"


# The export filter each legacy format can actually be converted OUT to. Writer
# has a plain-text filter; Calc does not, and Impress has neither, so each is
# round-tripped through the filter its own application does have.
#
# This mapping is measured, not assumed, and getting it wrong is not harmless:
# `--convert-to txt` on a .xls fails with "no export filter ... found" on a
# pristine file straight out of the converter. Read as a validity check that
# would have said the scrubber destroyed a document it had not touched.
_REOPEN_AS = {".doc": ".txt", ".xls": ".csv", ".ppt": ".ppt"}


@pytest.mark.parametrize("kind", OLE2_KINDS)
def test_the_scrubbed_file_still_opens(kind, tmp_path):
    """
    An independent reader must still accept the document.

    olefile proves the container parses. LibreOffice proves more: it is the only
    reader available here that understands the Word, Excel and PowerPoint
    payloads, and it is a different program from the one that wrote the file
    through this tool. A scrubber that produced documents nothing can open would
    be worse than no scrubber, because the damage is discovered late and the
    original may be gone.

    The unscrubbed fixture is converted FIRST, as a control. Without it, a
    conversion that cannot work on this machine for reasons of its own is
    indistinguishable from a document this tool damaged, and the test would be
    reporting corruption that never happened.
    """
    path, _ = build(kind, tmp_path)
    original = str(tmp_path / f"original{kind}")
    shutil.copyfile(path, original)

    soffice = build_ole2.find_soffice()
    if soffice is None:  # pragma: no cover - guarded by build() skipping first
        pytest.skip("LibreOffice not available to re-open the scrubbed file")
    target = _REOPEN_AS[kind]
    control = build_ole2.convert(soffice, target, original, str(tmp_path / "control"))
    if control is None:
        pytest.skip(f"LibreOffice cannot convert an untouched {kind} to {target} here")

    with MetadataScrubber(backup=False) as scrubber:
        result = scrubber.sanitize_file(path, remove_all=True)
    assert result["status"] == STATUS_SANITIZED, result.get("error")

    ole = olefile.OleFileIO(path)
    try:
        assert ole.listdir(streams=True), "the scrubbed file has no streams"
    finally:
        ole.close()

    produced = build_ole2.convert(soffice, target, path, str(tmp_path / "reopen"))
    assert produced is not None, (
        f"LibreOffice re-opened the untouched {kind} but not the scrubbed one; "
        "the document is damaged"
    )
    assert os.path.getsize(produced) > 0


def test_the_visible_content_survives(tmp_path):
    """
    Removing metadata must not remove the document. The seed text is asserted in
    the converted output, so this is a check of the payload rather than of the
    file's size.
    """
    path, _ = build(".doc", tmp_path)
    original = str(tmp_path / "original.doc")
    shutil.copyfile(path, original)
    soffice = build_ole2.find_soffice()
    if build_ole2.convert(soffice, ".txt", original, str(tmp_path / "control")) is None:
        pytest.skip("LibreOffice cannot convert an untouched .doc to text here")

    with MetadataScrubber(backup=False) as scrubber:
        scrubber.sanitize_file(path, remove_all=True)

    produced = build_ole2.convert(soffice, ".txt", path, str(tmp_path / "text"))
    assert produced is not None, "LibreOffice could not read the scrubbed .doc"
    with open(produced, "rb") as handle:
        text = handle.read().decode("utf-8", "replace")
    assert "visible body text" in text, "sanitizing destroyed the document body"


# THE RESIDUAL SCAN'S ONE OLE2-SPECIFIC EXCLUSION

def test_directory_names_are_excluded_but_payloads_are_not(tmp_path):
    """
    verify.py blanks the OLE2 directory's name fields before scanning. That
    exclusion has to be positional, not textual, or it would hide a real leak
    whose value happens to equal a stream's name.

    The case is not hypothetical: LibreOffice writes "Current User" as the
    PowerPoint user name, which is also the name of the stream holding it, so
    before this exclusion existed a genuinely clean .ppt was reported as still
    leaking. The proof it did not overcorrect is here: the same string is
    planted back into a stream's PAYLOAD and must still be found.
    """
    from metascrub.verify import residual_scan, searchable_bytes

    path, value = build(".ppt", tmp_path)
    spec = spec_for(path)

    # The exclusion must not gut the haystack: the sentinel lives in a property
    # stream payload and must still be visible before anything is scrubbed.
    with open(path, "rb") as handle:
        raw = handle.read()
    haystack = searchable_bytes(path, spec)
    assert len(haystack) == len(raw), "the scan is no longer over the whole file"
    assert residual_scan(path, spec, [value]) == [value]

    with MetadataScrubber(backup=False) as scrubber:
        scrubber.sanitize_file(path, remove_all=True)
    assert residual_scan(path, spec, ["Current User"]) == [], (
        "the directory entry naming the stream was treated as a surviving value"
    )

    planted = "Current User"
    ole = olefile.OleFileIO(path, write_mode=True)
    try:
        data = bytearray(ole.openstream(_SUMMARY).read())
        encoded = planted.encode("utf-16-le")
        data[-len(encoded):] = encoded
        ole.write_stream(_SUMMARY, bytes(data))
    finally:
        ole.close()
    assert residual_scan(path, spec, [planted]) == [planted], (
        "a value sitting in a stream payload was hidden by the directory "
        "name exclusion"
    )


# REFUSALS

def test_a_non_ole2_file_is_refused_rather_than_mangled(tmp_path):
    """
    An extension is a claim, not a fact. A .doc that is really something else
    must be refused, because the engine's whole safety argument rests on the
    structures it patches actually being where it expects them.
    """
    from metascrub.engines import get_engine
    from metascrub.engines.base import EngineError

    path = str(tmp_path / "liar.doc")
    with open(path, "wb") as handle:
        handle.write(b"PK\x03\x04 this is not a compound file" * 40)
    original = open(path, "rb").read()

    with pytest.raises(EngineError):
        get_engine(Engine.OLE2).strip_all(path)
    assert open(path, "rb").read() == original, "a refused file was modified anyway"


def test_a_property_stream_that_cannot_be_emptied_is_a_refusal(tmp_path):
    """
    The engine must not skip a property stream it does not understand.

    Silently leaving one in place while returning success is the exact failure
    this project exists to prevent: the caller reads "sanitized" off a file that
    still carries everything. The input must also come back untouched, since the
    engine works on a copy for precisely this reason.
    """
    from metascrub.engines import get_engine
    from metascrub.engines.base import EngineError

    path, value = build(".doc", tmp_path)
    ole = olefile.OleFileIO(path, write_mode=True)
    try:
        data = ole.openstream(_SUMMARY).read()
        # Corrupt only the byte order marker. Everything else, including the
        # sentinel, stays exactly where it was.
        ole.write_stream(_SUMMARY, b"\x00\x00" + data[2:])
    finally:
        ole.close()
    original = open(path, "rb").read()

    with pytest.raises(EngineError):
        get_engine(Engine.OLE2).strip_all(path)
    assert open(path, "rb").read() == original, "a refused file was modified anyway"
    assert raw_contains(path, value), "the refusal still destroyed the input"


def test_selective_field_removal_is_refused(tmp_path):
    """
    Only exiftool-backed formats can remove one named tag. Pretending otherwise
    would report success for a field that is still in the file.
    """
    path, value = build(".doc", tmp_path)
    with MetadataScrubber(backup=False) as scrubber:
        result = scrubber.sanitize_file(path, fields_to_remove=["Author"])
    assert result["status"] == STATUS_ERROR
    assert "remove_all" in result["error"]
    assert raw_contains(path, value), "a refused selective run modified the file"


# BACKUPS

@pytest.mark.parametrize("kind", OLE2_KINDS)
def test_the_backup_keeps_the_original(kind, tmp_path):
    """A backup that does not hold the pre-scrub bytes is not a backup."""
    path, value = build(kind, tmp_path)
    with MetadataScrubber(backup=True) as scrubber:
        result = scrubber.sanitize_file(path, remove_all=True)
    assert result["status"] == STATUS_SANITIZED, result.get("error")
    assert not raw_contains(path, value)
    assert raw_contains(path + ".backup", value), "the backup lost the original"


def test_libreoffice_absence_skips_rather_than_fails():
    """
    Guards the skip policy itself. A machine without the converter must not be
    able to turn these tests red: a red suite there would be read as a broken
    engine, and someone would go looking for a bug that is not in this repo.
    """
    if HAVE_LIBREOFFICE:
        pytest.skip("LibreOffice is present; the absent-converter path is not exercised")
    assert build_ole2.build_ole2(".doc", ".", sentinel("probe")) is None
