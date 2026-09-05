"""
Gates.

Two things this suite must not allow to happen quietly:

  1. A format appears in the capability table with no test behind it. The table
     is a set of promises; an untested row is a promise nobody checked.

  2. A deferred format gets shipped without the fixture and test that were the
     stated condition for shipping it. A deferral is an obligation with a due
     date, and something other than memory has to collect it.

And one thing this suite must not do quietly either: pass because there was
nothing to check. Three of the gates below read DEFERRED, so while that table
is empty they run zero assertions and report green. That is not hypothetical.
It was the real state of the project until 2026-09-04, while four AV
containers were called deferred in README.md, in tasks/todo.md and in a
comment in capabilities.py. A loop over an empty container is the quietest
possible pass.

The fix is structural rather than a promise to remember. Each deferral rule is
a pure function of the tables it judges and RETURNS the violations it finds
instead of asserting. The public tests point those functions at the real
tables; test_the_deferral_gates_have_teeth points the same functions at
synthetic tables that are known to be in violation. So the logic is proven to
fail when it should, whether or not DEFERRED happens to hold anything today.
"""

from __future__ import annotations

import os
import re

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
    # Added 2026-09-04. Each raw format below was verified individually through
    # the full pipeline: exiftool identified it as its own FileType rather than
    # as TIFF, accepted a write, and the sentinel was gone from the output bytes
    # afterwards with the file still opening. They run the identical exiftool
    # code path as .tiff, which is what makes the proxy honest rather than a
    # convenience. Formats where exiftool refused, or reported the file as plain
    # TIFF, were left out of the capability table entirely.
    ".pef": ".tiff", ".srw": ".tiff", ".erf": ".tiff", ".mos": ".tiff",
    ".iiq": ".tiff", ".arq": ".tiff", ".sr2": ".tiff", ".rwl": ".tiff",
    ".nrw": ".tiff", ".raw": ".tiff", ".gpr": ".tiff",
    # MP4-family variants remux through the same ffmpeg path as .mp4.
    ".f4v": ".mp4", ".m4b": ".mp4",
    # Same container family as their tested sibling.
    ".m2ts": ".ts", ".aif": ".aiff",
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


# THE DEFERRAL RULES, AS FUNCTIONS
#
# Each takes the tables to judge and returns the violations it found rather
# than asserting. That is what lets the same code be aimed at the real tables
# by the public tests below AND at a synthetic table known to be in violation,
# which is the only way to show the rule can still fail.

# A "reason" shorter than this is a label, not a reason. Named so that the
# synthetic test and the real test cannot drift apart.
MIN_REASON_CHARS = 20


def shipped_and_deferred(capabilities, deferred):
    """Extensions claimed as supported and deferred at the same time."""
    return sorted(set(capabilities) & set(deferred))


def deferrals_without_a_reason(deferred):
    """Extensions deferred with no reason, or one too short to be one."""
    return sorted(ext for ext, reason in deferred.items()
                  if not reason or len(reason) <= MIN_REASON_CHARS)


def deferrals_missing_from_the_readme(deferred, readme_text):
    """Extensions deferred but named nowhere in the README."""
    lowered = readme_text.lower()
    return sorted(ext for ext in deferred if ext.lower() not in lowered)


def _read_readme():
    readme = os.path.join(_PROJECT_DIR, "README.md")
    assert os.path.isfile(readme), "README.md is missing"
    with open(readme, encoding="utf-8") as fh:
        return fh.read()


def test_no_extension_is_both_supported_and_deferred():
    """A format cannot be shipped and deferred at the same time."""
    overlap = shipped_and_deferred(CAPABILITIES, DEFERRED)
    assert not overlap, f"extensions in both tables: {overlap}"


def test_every_deferral_states_a_reason():
    bad = deferrals_without_a_reason(DEFERRED)
    assert not bad, f"deferred without a real reason: {bad}"


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
    missing = deferrals_missing_from_the_readme(DEFERRED, _read_readme())
    assert not missing, f"deferred but not mentioned in README.md: {missing}"


# A table that breaks all three rules at once. Its extensions are deliberately
# unreal, so this can never accidentally agree with the world.
_BAD_DEFERRED = {
    ".metascrubshipped": "a reason easily long enough to count as one",
    ".metascrubterse": "too short",
    ".metascrubempty": "",
}
_BAD_CAPABILITIES = {".metascrubshipped": "a spec object stands in here"}
_GOOD_DEFERRED = {".metascrubfine": "a measured reason of more than twenty chars"}


def test_the_deferral_gates_have_teeth():
    """
    Prove the three deferral rules can still FAIL.

    They read DEFERRED, so an empty DEFERRED makes all three pass having
    asserted nothing. A gate that cannot fail is not a gate, and its greenness
    carries no information. Exercising the rules against a synthetic table
    instead of against whatever the project defers today is what keeps this
    test meaningful when DEFERRED is emptied again, which is exactly what
    happens each time a deferral is discharged.
    """
    assert shipped_and_deferred(_BAD_CAPABILITIES, _BAD_DEFERRED) == [
        ".metascrubshipped"
    ]
    assert deferrals_without_a_reason(_BAD_DEFERRED) == [
        ".metascrubempty", ".metascrubterse"
    ]
    assert deferrals_missing_from_the_readme(
        _BAD_DEFERRED, "a README that names none of them") == sorted(_BAD_DEFERRED)

    # The other direction. Without this the rules could be unfailable the
    # opposite way, flagging every honest deferral, and the suite would say the
    # same thing either way.
    assert shipped_and_deferred({".mp4": "spec"}, _GOOD_DEFERRED) == []
    assert deferrals_without_a_reason(_GOOD_DEFERRED) == []
    assert deferrals_missing_from_the_readme(
        _GOOD_DEFERRED, "we do not handle .metascrubfine yet") == []


def test_an_empty_deferral_table_is_the_only_reason_the_gates_can_be_silent():
    """
    Name the vacuity rather than leaving it implicit.

    When DEFERRED is empty the two per-entry rules return no violations because
    there are no entries, not because the project checked anything. This test
    asserts that reading, so the next person to see three green deferral gates
    against an empty table knows what that green means.
    """
    assert deferrals_without_a_reason({}) == []
    assert deferrals_missing_from_the_readme({}, "") == []
    assert shipped_and_deferred(CAPABILITIES, {}) == []


def test_the_reason_length_floor_is_the_one_the_gate_uses():
    """
    The boundary, asserted rather than assumed. A reason of exactly
    MIN_REASON_CHARS is rejected and one character more is accepted, so the
    constant cannot be changed without this saying so.
    """
    assert deferrals_without_a_reason({".e": "x" * MIN_REASON_CHARS}) == [".e"]
    assert deferrals_without_a_reason({".e": "x" * (MIN_REASON_CHARS + 1)}) == []


def test_prose_calling_a_format_deferred_is_backed_by_a_table_entry():
    """
    The failure that produced all of the above: four extensions were called
    deferred in three documents while DEFERRED was empty, so nothing collected
    them. Prose is where a reader looks; a table is what the gates read; the
    two must not disagree.

    Every extension the README names on a line that calls something deferred
    has to appear in DEFERRED or in CAPABILITIES. A README line that says a
    format WAS deferred and now ships is satisfied by CAPABILITIES, which is
    why both tables count.
    """
    deferred_prose = set()
    for line in _read_readme().lower().splitlines():
        if "deferred" not in line:
            continue
        deferred_prose |= set(re.findall(r"`(\.[a-z0-9]{1,5})`", line))
    unaccounted = sorted(ext for ext in deferred_prose
                         if ext not in DEFERRED and ext not in CAPABILITIES)
    assert not unaccounted, (
        "README calls these deferred but no table collects them: "
        f"{unaccounted}")


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
