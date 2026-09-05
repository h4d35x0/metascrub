"""
Zip entry timestamps are metadata, and every member must be normalised.

WHY THIS FILE EXISTS

Written on 2026-09-05 to justify a change that turned out to be wrong, and kept
because it found a real leak on the way.

The OOXML engine normalised the timestamp of every member it COPIED, and none
of the members it REPLACED. `writestr(name, ...)` with a string name stamps the
CURRENT clock, so every scrubbed Office document recorded the minute it was
sanitized; and `writestr(item, ...)` reused the source ZipInfo, so
[Content_Types].xml and _rels/.rels kept the ORIGINAL editing session's
timestamp. Measured on a real .docx: four members carrying (2026, 9, 5, 12, 53).

The engine's own comment said timestamps "leak the editing session's wall-clock
time, so normalise them". Two code paths bypassed it. The comment was right and
the code was incomplete, which is why the assertion lives here rather than
staying a comment.
"""
from __future__ import annotations

import zipfile

import pytest

from conftest import HAVE_EXIFTOOL, ODF_KINDS, make_docx, make_odf, make_pptx, make_xlsx
from metascrub import MetadataScrubber

_EPOCH = (1980, 1, 1, 0, 0, 0)


def _entry_timestamps(path):
    with zipfile.ZipFile(path) as z:
        return {i.filename: i.date_time for i in z.infolist()}


@pytest.mark.skipif(not HAVE_EXIFTOOL, reason="exiftool is required")
@pytest.mark.parametrize("builder", [make_docx, make_xlsx, make_pptx])
def test_ooxml_normalises_every_zip_entry_timestamp(tmp_path, builder):
    path, _ = builder(tmp_path)
    with MetadataScrubber(backup=False) as s:
        s.sanitize_file(path, remove_all=True)
    stamps = _entry_timestamps(path)
    assert stamps, "no zip members"
    bad = {n: t for n, t in stamps.items() if t != _EPOCH}
    assert not bad, (
        "zip entry timestamps are a metadata carrier and are no longer "
        "normalised, so treating the ZIP group as structural is now unsafe: %r"
        % (bad,)
    )


@pytest.mark.skipif(not HAVE_EXIFTOOL, reason="exiftool is required")
@pytest.mark.parametrize("ext", ODF_KINDS)
def test_odf_normalises_every_zip_entry_timestamp(tmp_path, ext):
    path, _ = make_odf(tmp_path, ext)
    with MetadataScrubber(backup=False) as s:
        s.sanitize_file(path, remove_all=True)
    stamps = _entry_timestamps(path)
    assert stamps, "no zip members"
    bad = {n: t for n, t in stamps.items() if t != _EPOCH}
    assert not bad, (
        "zip entry timestamps are a metadata carrier and are no longer "
        "normalised, so treating the ZIP group as structural is now unsafe: %r"
        % (bad,)
    )
