"""
Gates.

Two things this suite must not allow to happen quietly:

  1. A format appears in the capability table with no test behind it. The table
     is a set of promises; an untested row is a promise nobody checked.

  2. A deferred format gets shipped without the fixture and test that were the
     stated condition for shipping it. A deferral is an obligation with a due
     date, and something other than memory has to collect it.
"""

from __future__ import annotations

import os

import pytest

from conftest import ALL_KINDS
from metascrub import CAPABILITIES, DEFERRED, Completeness, Engine

_TESTS_DIR = os.path.dirname(os.path.abspath(__file__))
_PROJECT_DIR = os.path.dirname(_TESTS_DIR)

# Extensions handled by an engine already covered by a tested sibling, where
# building a fixture needs hardware or a licensed encoder this machine does not
# have. Each one must name the tested extension that exercises the same code
# path, so the exemption is a reference rather than a loophole.
_COVERED_BY = {
    ".jpe": ".jpg", ".jpeg": ".jpg", ".tif": ".tiff", ".heic": ".jpg", ".heif": ".jpg",
    ".avif": ".jpg", ".jp2": ".jpg", ".psd": ".jpg",
    ".dng": ".tiff", ".cr2": ".tiff", ".nef": ".tiff", ".arw": ".tiff",
    ".orf": ".tiff", ".rw2": ".tiff",
    ".docm": ".docx", ".xlsm": ".xlsx", ".pptm": ".pptx",
    ".m4v": ".mp4", ".mov": ".mp4", ".webm": ".mkv", ".avi": ".mkv",
    ".m4a": ".mp3", ".wav": ".flac", ".ogg": ".mp3", ".opus": ".mp3",
}


def test_every_supported_format_is_tested_or_explicitly_covered():
    tested = set(ALL_KINDS)
    gaps = []
    for ext in CAPABILITIES:
        if ext in tested:
            continue
        proxy = _COVERED_BY.get(ext)
        if proxy is None:
            gaps.append(f"{ext} has no fixture and no declared proxy")
        elif proxy not in tested:
            gaps.append(f"{ext} claims coverage via {proxy}, which is not tested")
    assert not gaps, "untested formats in the capability table: " + "; ".join(gaps)


def test_no_extension_is_both_supported_and_deferred():
    """A format cannot be shipped and deferred at the same time."""
    overlap = set(CAPABILITIES) & set(DEFERRED)
    assert not overlap, f"extensions in both tables: {sorted(overlap)}"


def test_every_deferral_states_a_reason():
    for ext, reason in DEFERRED.items():
        assert reason and len(reason) > 20, f"{ext} is deferred without a real reason"


def test_ole2_cannot_ship_without_its_fixtures_and_tests():
    """
    The discharge condition for the OLE2 deferral, enforced.

    While .doc/.xls/.ppt stay deferred this passes trivially. The moment someone
    moves them into CAPABILITIES, this test demands the fixture directory and
    the test module that were the stated price of shipping them. That is what
    stops the deferral from being discharged by simply forgetting it.
    """
    ole2_exts = {".doc", ".xls", ".ppt"}
    shipped = ole2_exts & set(CAPABILITIES)
    if not shipped:
        pytest.skip("OLE2 still deferred; nothing to enforce yet")

    fixtures = os.path.join(_TESTS_DIR, "fixtures", "ole2")
    test_module = os.path.join(_TESTS_DIR, "test_ole2.py")
    assert os.path.isdir(fixtures), (
        f"{sorted(shipped)} shipped without tests/fixtures/ole2/"
    )
    assert os.listdir(fixtures), "tests/fixtures/ole2/ is empty"
    assert os.path.isfile(test_module), (
        f"{sorted(shipped)} shipped without tests/test_ole2.py"
    )


def test_deferrals_are_documented_in_the_readme():
    """
    A deferral nobody can find is a deferral nobody will discharge. The README
    is where a user looks to learn what this tool does not do.
    """
    readme = os.path.join(_PROJECT_DIR, "README.md")
    assert os.path.isfile(readme), "README.md is missing"
    with open(readme, encoding="utf-8") as fh:
        text = fh.read().lower()
    for ext in DEFERRED:
        assert ext in text, f"deferred format {ext} is not mentioned in README.md"


def test_capability_rows_are_internally_consistent():
    """A row that contradicts itself would mislead every caller reading it."""
    for ext, spec in CAPABILITIES.items():
        assert isinstance(spec.engine, Engine), ext
        assert isinstance(spec.completeness, Completeness), ext
        # Container rebuilds are how the non-exiftool engines work; exiftool
        # edits in place. A row claiming otherwise means the table drifted from
        # the engine it names.
        if spec.engine is Engine.EXIFTOOL:
            assert spec.rewrites_container is False, f"{ext} claims exiftool rebuilds"
        else:
            assert spec.rewrites_container is True, f"{ext} claims a rebuild-free rewrite"
        if spec.completeness is Completeness.PARTIAL:
            assert spec.note, f"{ext} is PARTIAL but does not say what remains"
