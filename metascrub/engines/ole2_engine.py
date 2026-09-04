"""
OLE2 engine: legacy compound files (.doc, .xls, .ppt).

This format was DEFERRED until 2026-09-04 because it could not be tested. That
is no longer true: LibreOffice's "MS Word 97" / "MS Excel 97" / "MS PowerPoint
97" export filters produce genuine compound files (magic d0cf11e0a1b11ae1), so
tests/fixtures/ole2/ can build a real fixture at test time and every claim below
is measured rather than assumed.

WHY THE STREAM CONTENTS ARE REPLACED RATHER THAN THE FILE REBUILT

olefile can only overwrite a stream at its existing length; it cannot add,
remove or resize one. That was the reason the format looked hard. It stops being
a problem once you notice that a property set stream does not have to be full to
be valid: MS-OLEPS allows a property set with zero properties, and any bytes
after the last property set are ignored by readers. So the fix is to write a
structurally valid, EMPTY property set stream and zero-fill the remainder of the
original allocation.

That choice is deliberate and conservative. write_stream is the only mutation
here, and it writes into the sectors a stream already owns, so the header, the
FAT, the mini-FAT, the directory entries and every sector chain are untouched
by construction. There is no code path that can produce a compound file whose
structure differs from the input, which matters more than elegance for a format
whose files a user usually cannot regenerate.

Measured on LibreOffice-produced .doc, .xls and .ppt fixtures, and asserted in
tests/test_ole2.py: the 512-byte header is byte-identical, the stream inventory
and every stream length are unchanged, every stream this engine does not target
is byte-identical, the file size is unchanged, no sentinel byte survives, and
LibreOffice still opens the result.

WHAT IS REMOVED

  \\005SummaryInformation          title, subject, author, keywords, comments,
                                  last-saved-by, template, revision number,
                                  edit time, created/saved/printed timestamps,
                                  and the embedded thumbnail (PID 17, which was
                                  589 KB of the 650 KB .ppt fixture)
  \\005DocumentSummaryInformation  company, manager, category, content type,
                                  and the whole user-defined property set that
                                  document management systems stamp IDs into
  \\001CompObj                     the display user type, which names the
                                  producing application AND its UI language.
                                  Measured: LibreOffice writes "Microsoft
                                  Word-Dokument" there on an English document,
                                  so this field leaks the writer's locale.
  Current User                    PowerPoint's CurrentUserAtom, whose userName
                                  field is the name of the last person to edit
                                  the presentation.
  WRITEACCESS (BIFF 0x005C)       Excel's user name record in the Workbook
                                  globals substream. It survives a property
                                  stream wipe completely.

Property streams are matched by basename at ANY depth, so the copy inside an
embedded OLE object's storage is cleaned too, not just the one at the root.
No fixture here contains an embedded object, so that is a statement about what
the code does rather than something measured.

WHAT SURVIVES, AND WHY IT IS NOT REMOVED

This engine is PARTIAL, and the note in capabilities.py says so. Measured with
exiftool 13.59 on a scrubbed .doc fixture, this remains:

    [MS-DOC] CreateDate, ModifyDate, LastPrinted, RevisionNumber,
             TotalEditTime, Words, Characters, Pages, Paragraphs, Lines

Those come from the DOP structure inside the WordDocument/Table streams, which
duplicates most of SummaryInformation. The give-away that it is a second carrier
and not a leftover: after the property streams are emptied, exiftool still
prints a CreateDate, but shifted by the local UTC offset, because it is now
reading a local-time field in the DOP instead of the FILETIME in the property
set.

Two more Word carriers are known and also survive: SttbfAssoc (title, subject,
keywords, comments, author and last-author, duplicated in the Table stream) and
SttbSavedBy (the last ten author names and the full paths they saved to).
Neither is touched here for one reason: LibreOffice's Word 97 filter does not
write them, measured as zero sentinel hits in 1Table, so any code targeting them
would be UNTESTED binary surgery against a structure located through a
version-dependent FIB offset table. Shipping untested rewrites of legacy Office
internals is what the original deferral existed to prevent, and finding a way to
test the property streams does not make the rest testable.
"""

from __future__ import annotations

import os
import shutil
import struct
from typing import List, Optional, Tuple

from .base import BaseEngine, EngineError, atomic_replace, temp_beside

try:
    import olefile
    OLEFILE_AVAILABLE = True
except ImportError:
    olefile = None
    OLEFILE_AVAILABLE = False


_SUMMARY_STREAMS = ("\x05SummaryInformation", "\x05DocumentSummaryInformation")
_COMPOBJ_STREAM = "\x01CompObj"
_CURRENT_USER_STREAM = "Current User"
_WORKBOOK_STREAM = "Workbook"
# Application payload streams. Present only to decide which survival note to
# report; none of them is edited.
_WORD_STREAM = "WordDocument"
_PPT_STREAM = "PowerPoint Document"

# MS-OLEPS PropertySetStream header: ByteOrder(2) Version(2) SystemIdentifier(4)
# CLSID(16) NumPropertySets(4), then NumPropertySets x (FMTID(16) Offset(4)).
_PS_HEADER = 28
_PS_ENTRY = 20
_PS_BYTE_ORDER = 0xFFFE
# A PropertySet with no properties is Size(4) + NumProperties(4) and nothing
# else. This is what makes the whole in-place approach work.
_PS_EMPTY_SET = struct.pack("<II", 8, 0)

# MS-OLEDS CompObjStream: Reserved1(4) Version(4) Reserved2(20), then the
# length-prefixed AnsiUserType. Measured on all three fixtures: the length word
# sits at offset 28 every time.
_COMPOBJ_HEADER = 28
_COMPOBJ_UNICODE_MARKER = 0x71B239F4

# MS-PPT CurrentUserAtom: an 8-byte record header, then size(4) headerToken(4)
# offsetToCurrentEdit(4) lenUserName(2) docFileVersion(2) majorVersion(1)
# minorVersion(1) unused(2), then ansiUserName. Measured on the .ppt fixture:
# lenUserName 12, ansiUserName "Current User" at offset 28, relVersion at 40.
_CU_NAME_OFFSET = 28
_CU_LEN_OFFSET = 20
_CU_REL_VERSION = 4

# BIFF8 WRITEACCESS. Fixed 112-byte body holding an XLUnicodeString padded with
# spaces. Measured on the .xls fixture: cch 4, "Calc", then 105 spaces.
_BIFF_WRITEACCESS = 0x005C
_BIFF_EOF = 0x000A
_BIFF_WRITEACCESS_LEN = 112
# The record lives in the workbook globals substream, which is the first thing
# in the stream. Walking further than this is walking into cell data, where a
# record id that happens to equal 0x005C is not a WRITEACCESS record.
_BIFF_SCAN_LIMIT = 8192


class Ole2Engine(BaseEngine):
    name = "ole2"
    supports_selective = False

    def available(self) -> Tuple[bool, str]:
        if not OLEFILE_AVAILABLE:
            return False, "olefile is not installed (pip install olefile)"
        return True, ""

    def strip_all(self, path: str) -> List[str]:
        self.require()
        if not olefile.isOleFile(path):
            raise EngineError("not a valid OLE2 compound file")

        # Work on a copy and swap it in, because olefile writes in place. An
        # interrupted in-place edit of a compound file leaves a document that
        # opens as garbage, and the user's only other copy may be the backup
        # this run just made.
        tmp = temp_beside(path, os.path.splitext(path)[1] or ".ole")
        targeted: List[str] = []
        try:
            shutil.copy2(path, tmp)
            ole = olefile.OleFileIO(tmp, write_mode=True)
            try:
                targeted = self._clean_streams(ole)
            finally:
                ole.close()
        except EngineError:
            self._discard(tmp)
            raise
        except Exception as exc:
            self._discard(tmp)
            raise EngineError(f"failed to rewrite OLE2 compound file: {exc}") from exc

        atomic_replace(tmp, path)
        return targeted or ["ole2 property stream rewrite"]

    @staticmethod
    def _discard(tmp: str) -> None:
        if os.path.exists(tmp):
            try:
                os.unlink(tmp)
            except OSError:
                pass

    def _clean_streams(self, ole) -> List[str]:
        targeted: List[str] = []
        for entry in ole.listdir(streams=True):
            name = entry[-1]
            where = "/".join(entry)
            if name in _SUMMARY_STREAMS:
                data = ole.openstream(entry).read()
                replacement = _empty_property_set(data)
                if replacement is None:
                    # Refusing the whole file is the right answer here. Leaving
                    # a property stream we could not neutralise while reporting
                    # a successful scrub is the exact failure this project
                    # exists to prevent, and the input is still untouched.
                    raise EngineError(
                        f"{where} is not a property set this engine can safely "
                        "empty; refusing to rewrite the document"
                    )
                ole.write_stream(entry, replacement)
                targeted.append(f"{where} (property set emptied)")
            elif name == _COMPOBJ_STREAM:
                data = ole.openstream(entry).read()
                replacement = _blank_compobj_user_type(data)
                if replacement is not None:
                    ole.write_stream(entry, replacement)
                    targeted.append(f"{where} (application user type)")
            elif name == _CURRENT_USER_STREAM:
                data = ole.openstream(entry).read()
                replacement = _blank_current_user(data)
                if replacement is not None:
                    ole.write_stream(entry, replacement)
                    targeted.append(f"{where} (PowerPoint last-editor name)")
            elif name == _WORKBOOK_STREAM:
                data = ole.openstream(entry).read()
                replacement = _blank_write_access(data)
                if replacement is not None:
                    ole.write_stream(entry, replacement)
                    targeted.append(f"{where} (Excel WRITEACCESS user name)")
        targeted.extend(self._survival_notes(ole))
        return targeted

    @staticmethod
    def _survival_notes(ole) -> List[str]:
        """
        Report what was left behind, based on the streams this file actually
        has rather than on a constant.

        A user who asked for metadata removal will assume the document's
        timestamps went with it, and on this format they did not. Naming the
        wrong application's carriers would be almost as bad as saying nothing:
        a reader cannot tell a promise that does not apply from one that was
        kept.
        """
        present = {entry[-1] for entry in ole.listdir(streams=True)}
        notes = []
        if _WORD_STREAM in present:
            notes.append(
                "NOTE: the Word DOP keeps its own copy of the created, saved "
                "and printed timestamps, the edit time, the revision count and "
                "the document statistics; these are NOT removed, and neither "
                "are SttbfAssoc and SttbSavedBy on files written by Microsoft "
                "Word"
            )
        if _WORKBOOK_STREAM in present:
            notes.append(
                "NOTE: BIFF records other than WRITEACCESS are not parsed and "
                "are NOT removed"
            )
        if _PPT_STREAM in present:
            notes.append(
                "NOTE: per-edit user records inside the PowerPoint Document "
                "stream are NOT removed"
            )
        return notes


def _empty_property_set(data: bytes) -> Optional[bytes]:
    """
    Build a valid, empty MS-OLEPS property set stream of exactly len(data).

    The original FMTIDs and version are preserved so a reader still recognises
    the stream as SummaryInformation; the CLSID and the system identifier are
    zeroed, because those name the operating system that wrote the file and are
    themselves provenance. Everything after the empty property sets is zeroed:
    readers reach a property set through the offset table and never look past
    it, and leaving the old bytes there would leave the metadata recoverable,
    which is the PDF incremental-update mistake in a different container.

    Returns None when the stream cannot be parsed as a property set stream or is
    too short to hold an empty one. The caller must treat None as a refusal, not
    as "nothing to do".
    """
    if len(data) < _PS_HEADER:
        return None
    byte_order, version = struct.unpack("<HH", data[:4])
    if byte_order != _PS_BYTE_ORDER:
        return None
    (set_count,) = struct.unpack("<I", data[24:_PS_HEADER])
    # MS-OLEPS allows 1 or 2. Anything else means this is not the structure we
    # think it is, and guessing at it is how documents get corrupted.
    if set_count not in (1, 2):
        return None
    if len(data) < _PS_HEADER + _PS_ENTRY * set_count:
        return None

    fmtids = [
        data[_PS_HEADER + i * _PS_ENTRY: _PS_HEADER + i * _PS_ENTRY + 16]
        for i in range(set_count)
    ]

    header = bytearray()
    header += struct.pack("<HHI", _PS_BYTE_ORDER, version, 0)
    header += b"\x00" * 16
    header += struct.pack("<I", set_count)
    offset = _PS_HEADER + _PS_ENTRY * set_count
    for fmtid in fmtids:
        header += fmtid + struct.pack("<I", offset)
        offset += len(_PS_EMPTY_SET)
    out = bytes(header) + _PS_EMPTY_SET * set_count
    if len(out) > len(data):
        return None
    return out + b"\x00" * (len(data) - len(out))


def _blank_compobj_user_type(data: bytes) -> Optional[bytes]:
    """
    Blank the AnsiUserType, and the UnicodeUserType when one follows.

    The ProgID that comes after it ("Word.Document.8") is deliberately left
    alone: that is what tells Windows and OLE containers which application owns
    the file, so removing it would change how the document behaves rather than
    what it discloses. The user type is a display string and discloses both the
    application and its UI language.
    """
    if len(data) < _COMPOBJ_HEADER + 4:
        return None
    out = bytearray(data)
    cursor = _COMPOBJ_HEADER
    (length,) = struct.unpack("<I", data[cursor:cursor + 4])
    cursor += 4
    if length == 0 or cursor + length > len(data):
        return None
    # Keep the trailing NUL so the string stays a valid C string of the length
    # the length word still claims.
    for i in range(cursor, cursor + length - 1):
        out[i] = 0x20
    changed = out[cursor:cursor + length] != data[cursor:cursor + length]

    marker = data.find(struct.pack("<I", _COMPOBJ_UNICODE_MARKER), cursor + length)
    if marker != -1 and marker + 8 <= len(data):
        (ulength,) = struct.unpack("<I", data[marker + 4:marker + 8])
        start = marker + 8
        if 0 < ulength and start + ulength * 2 <= len(data):
            for i in range(ulength - 1):
                out[start + i * 2] = 0x20
                out[start + i * 2 + 1] = 0x00
            changed = True
    return bytes(out) if changed else None


def _blank_current_user(data: bytes) -> Optional[bytes]:
    """
    Blank the userName in PowerPoint's CurrentUserAtom.

    lenUserName is left as it was and the characters are overwritten with
    spaces. Setting the length to zero would move relVersion, which is read at
    an offset computed from it, so the field after the name would be parsed out
    of the middle of the name.
    """
    if len(data) < _CU_NAME_OFFSET:
        return None
    (name_len,) = struct.unpack("<H", data[_CU_LEN_OFFSET:_CU_LEN_OFFSET + 2])
    if name_len == 0:
        return None
    ansi_end = _CU_NAME_OFFSET + name_len
    if ansi_end + _CU_REL_VERSION > len(data):
        return None
    out = bytearray(data)
    for i in range(_CU_NAME_OFFSET, ansi_end):
        out[i] = 0x20
    # unicodeUserName is optional and only present when the stream is long
    # enough to hold it after relVersion.
    unicode_start = ansi_end + _CU_REL_VERSION
    if unicode_start + name_len * 2 <= len(data):
        for i in range(name_len):
            out[unicode_start + i * 2] = 0x20
            out[unicode_start + i * 2 + 1] = 0x00
    return bytes(out)


def _blank_write_access(data: bytes) -> Optional[bytes]:
    """
    Blank Excel's WRITEACCESS user name, in place, inside the Workbook stream.

    The record is walked to rather than searched for. A byte search for the
    record id would match cell data by coincidence, and a coincidental match
    would be overwritten with spaces in the middle of a spreadsheet's contents.
    The walk stops at the end of the globals substream, or at the scan limit,
    because past that point the id is no longer a record boundary.
    """
    cursor = 0
    out = None
    while cursor + 4 <= len(data) and cursor < _BIFF_SCAN_LIMIT:
        record_id, record_len = struct.unpack("<HH", data[cursor:cursor + 4])
        body = cursor + 4
        if body + record_len > len(data):
            break
        if record_id == _BIFF_WRITEACCESS and record_len == _BIFF_WRITEACCESS_LEN:
            out = bytearray(data)
            # An XLUnicodeString of zero characters, then the space padding
            # Excel itself writes to fill the fixed-width record.
            out[body:body + record_len] = (
                struct.pack("<HB", 0, 0) + b"\x20" * (record_len - 3)
            )
            return bytes(out)
        if record_id == _BIFF_EOF:
            break
        cursor = body + record_len
    return None
