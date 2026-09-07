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
containers were called deferred in README.md, in the backlog and in a
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
#
# THAT SENTENCE WAS A COMMENT AND NOT A CHECK UNTIL 2026-09-07, and in the gap
# it went false. The gate below only asked whether the proxy appeared in
# ALL_KINDS. So when .heic, .heif and .avif were routed from Engine.EXIFTOOL to
# Engine.ISOBMFF, their entries went on naming .jpg, which is an exiftool
# format: three extensions claiming coverage from a code path they no longer
# take, and a green gate saying so. Nothing was measuring the only thing the
# comment actually promised.
#
# proxy_violations() below now measures it. An entry has to name a proxy that
# is tested, that is in the capability table, that routes to the SAME engine,
# and whose completeness is no weaker; and an extension that has grown its own
# fixture may not keep an entry here at all. See _COMPLETENESS_RANK for why the
# completeness rule is one-directional.
_COVERED_BY = {
    ".jpe": ".jpg", ".jpeg": ".jpg", ".tif": ".tiff",
    ".jp2": ".jpg", ".psd": ".jpg",
    # .heic and .avif have real fixtures in conftest.ALL_KINDS since 2026-09-07,
    # so they are not exempted at all any more. .heif is the one extension of
    # the three still standing on a proxy, and it names .heic: the identical
    # HEIF container, the identical Engine.ISOBMFF code path, COMPLETE on both
    # rows. It is here rather than in ALL_KINDS because a .heif fixture would
    # need the same HEVC still the .heic one needs, and a second copy of that
    # file under a different extension would buy a second skip on every machine
    # without it rather than a second measurement.
    ".heif": ".heic",
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


# THE PROXY RULE, AS A FUNCTION
#
# Same shape as the deferral rules further down, and for the same reason: a
# rule that returns its violations can be pointed at the real tables by one
# test and at a table known to be in violation by another, which is the only
# way to show it can still fail.


# Completeness is ordered, and the rule over it is ONE-DIRECTIONAL on purpose.
#
# A proxy may be STRONGER than the extension it stands in for; it may not be
# weaker. Seventeen raw formats are PARTIAL and proxy to .tiff, which is
# COMPLETE, and that is fine: .tiff exercises the same exiftool code path AND
# runs the stricter assertions on top, including
# test_removal.test_complete_formats_verify_clean, which PARTIAL rows skip. The
# reverse would be the loophole. A COMPLETE extension standing on a PARTIAL
# proxy would be exempted by a sibling that is allowed to leave carriers
# behind, so the strongest check in the suite would never run for it and the
# COMPLETE promise would rest on nothing.
_COMPLETENESS_RANK = {Completeness.PARTIAL: 0, Completeness.COMPLETE: 1}


def proxy_violations(capabilities, covered_by, tested):
    """
    Every way a `_COVERED_BY` entry can be a claim rather than a reference.

    `tested` is the set of extensions that have their own fixture. Returns a
    list of human-readable violations, empty when every entry holds.
    """
    problems = []
    for ext, proxy in sorted(covered_by.items()):
        spec = capabilities.get(ext)
        if spec is None:
            problems.append(
                f"{ext} is exempted via {proxy} but is not in the capability "
                "table at all; the entry is stale")
            continue
        if ext in tested:
            problems.append(
                f"{ext} has its own fixture AND an exemption naming {proxy}; "
                "the exemption is stale and hides which one is load-bearing")
            continue
        if proxy not in tested:
            problems.append(f"{ext} claims coverage via {proxy}, which is not tested")
            continue
        proxy_spec = capabilities.get(proxy)
        if proxy_spec is None:
            problems.append(
                f"{ext} claims coverage via {proxy}, which is not in the "
                "capability table")
            continue
        if spec.engine is not proxy_spec.engine:
            problems.append(
                f"{ext} routes to {spec.engine.value} but its proxy {proxy} "
                f"routes to {proxy_spec.engine.value}; the exemption names a "
                "different code path and so exercises nothing")
            continue
        if _COMPLETENESS_RANK[proxy_spec.completeness] < \
                _COMPLETENESS_RANK[spec.completeness]:
            problems.append(
                f"{ext} is {spec.completeness.value} but its proxy {proxy} is "
                f"{proxy_spec.completeness.value}; a weaker sibling cannot "
                "stand in for a stronger promise")
    return problems


def untested_formats(capabilities, covered_by, tested):
    """Capability rows with neither a fixture nor a declared proxy."""
    return sorted(f"{ext} has no fixture and no declared proxy"
                  for ext in capabilities
                  if ext not in tested and ext not in covered_by)


def test_every_supported_format_is_tested_or_explicitly_covered():
    gaps = untested_formats(CAPABILITIES, _COVERED_BY, set(ALL_KINDS))
    assert not gaps, "untested formats in the capability table: " + "; ".join(gaps)


def test_every_proxy_names_the_same_code_path_it_stands_in_for():
    """
    The check whose absence let three entries rot.

    Existing in ALL_KINDS was the whole test. It says the proxy is tested; it
    says nothing about whether the proxy tests THIS extension. On 2026-09-07
    .heic, .heif and .avif were routed to Engine.ISOBMFF while their entries
    still named .jpg, an Engine.EXIFTOOL format, and the gate stayed green
    because .jpg was still tested. An exemption that names the wrong engine is
    not weak coverage, it is zero coverage wearing a citation.
    """
    problems = proxy_violations(CAPABILITIES, _COVERED_BY, set(ALL_KINDS))
    assert not problems, (
        "declared proxies that do not exercise what they claim: "
        + "; ".join(problems))


def test_the_proxy_gate_has_teeth():
    """
    Prove the proxy rule can still FAIL, and fail on the exact historical shape.

    The synthetic capability table below is a miniature of the real defect: an
    ISOBMFF extension exempted by an EXIFTOOL one. It also carries the three
    other ways an entry can be a claim rather than a reference. The extensions
    are deliberately unreal, so this can never accidentally agree with the
    world, and the good-direction assertions at the end stop the rule from
    being unfailable the opposite way.
    """
    class _Spec:
        def __init__(self, engine, completeness):
            self.engine = engine
            self.completeness = completeness

    caps = {
        ".metascrubbox": _Spec(Engine.ISOBMFF, Completeness.COMPLETE),
        ".metascrubboxtested": _Spec(Engine.ISOBMFF, Completeness.COMPLETE),
        ".metascrubtag": _Spec(Engine.EXIFTOOL, Completeness.COMPLETE),
        ".metascrubweak": _Spec(Engine.EXIFTOOL, Completeness.PARTIAL),
        ".metascrubrawlike": _Spec(Engine.EXIFTOOL, Completeness.PARTIAL),
        ".metascrubstrong": _Spec(Engine.EXIFTOOL, Completeness.COMPLETE),
        ".metascrubuntested": _Spec(Engine.EXIFTOOL, Completeness.COMPLETE),
    }
    tested = {".metascrubboxtested", ".metascrubtag", ".metascrubweak"}

    # 1. The historical defect itself: an ISOBMFF row exempted by an EXIFTOOL
    #    one. This is exactly ".heic": ".jpg" with the names changed.
    wrong_engine = proxy_violations(
        caps, {".metascrubbox": ".metascrubtag"}, tested)
    assert len(wrong_engine) == 1, wrong_engine
    assert "different code path" in wrong_engine[0], wrong_engine

    # 2. A COMPLETE row standing on a PARTIAL sibling.
    weaker_proxy = proxy_violations(
        caps, {".metascrubstrong": ".metascrubweak"}, tested)
    assert len(weaker_proxy) == 1, weaker_proxy
    assert "weaker sibling" in weaker_proxy[0], weaker_proxy

    # 3. An extension that grew its own fixture and kept the exemption anyway.
    stale_entry = proxy_violations(
        caps, {".metascrubboxtested": ".metascrubtag"}, tested)
    assert len(stale_entry) == 1, stale_entry
    assert "has its own fixture" in stale_entry[0], stale_entry

    # 4. An entry for an extension the capability table no longer carries.
    not_in_table = proxy_violations(
        caps, {".metascrubabsent": ".metascrubtag"}, tested)
    assert len(not_in_table) == 1, not_in_table
    assert "not in the capability table at all" in not_in_table[0], not_in_table

    # 5. The rule the gate already had, still enforced.
    untested_proxy = proxy_violations(
        caps, {".metascrubbox": ".metascrubuntested"}, tested)
    assert len(untested_proxy) == 1, untested_proxy
    assert "not tested" in untested_proxy[0], untested_proxy

    # The other direction, three ways, or the rule could be unfailable the
    # opposite way and every honest entry would be flagged. A same-engine proxy
    # passes; a PARTIAL extension standing on a COMPLETE proxy passes, which is
    # the seventeen raw formats; and an empty table finds nothing.
    assert proxy_violations(
        caps, {".metascrubbox": ".metascrubboxtested"}, tested) == []
    assert proxy_violations(
        caps, {".metascrubrawlike": ".metascrubtag"}, tested) == []
    assert proxy_violations(
        caps, {".metascrubuntested": ".metascrubtag"}, tested) == []
    assert proxy_violations(caps, {}, tested) == []


def test_the_untested_format_rule_has_teeth():
    """
    The companion to the above. An extension with neither fixture nor entry has
    to be reported, and one with either must not be.
    """
    caps = {".metascruba": object(), ".metascrubb": object(), ".metascrubc": object()}
    found = untested_formats(caps, {".metascrubb": ".metascrubc"}, {".metascrubc"})
    assert found == [".metascruba has no fixture and no declared proxy"]
    assert untested_formats(caps, {}, set(caps)) == []


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


def test_odf_cannot_ship_without_its_fixtures_and_tests():
    """
    The same enforcement the OLE2 row gets, for the same reason.

    The extensions are named literally rather than read out of DEFERRED. That is
    the whole lesson of 2026-09-04: three gates iterated an empty DEFERRED and
    reported green while running zero assertions. A gate that names its subject
    has teeth whatever the tables say.
    """
    odf_exts = {".odt", ".ott", ".ods", ".ots", ".odp", ".otp", ".odg", ".otg"}
    shipped = odf_exts & set(CAPABILITIES)
    if not shipped:
        pytest.skip("ODF not shipped; nothing to enforce yet")

    fixtures = os.path.join(_TESTS_DIR, "fixtures", "odf")
    test_module = os.path.join(_TESTS_DIR, "test_odf.py")
    assert os.path.isdir(fixtures), (
        f"{sorted(shipped)} shipped without tests/fixtures/odf/"
    )
    assert os.listdir(fixtures), "tests/fixtures/odf/ is empty"
    assert os.path.isfile(test_module), (
        f"{sorted(shipped)} shipped without tests/test_odf.py"
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
