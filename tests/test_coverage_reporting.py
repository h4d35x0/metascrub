"""
What the verdict does NOT cover, and whether it reaches anybody.

WHY THIS FILE EXISTS

docs/WHAT-THE-TOOL-CLAIMS.md measured, on 2026-09-07, that a `.dng` which has
just been scrubbed produces at one moment: `Completeness.PARTIAL`,
`ReadOutcome.PARSED`, `GpsStatus.NOT_CHECKED`,
`StructureReport.applicable=False`, and `Verdict.VERIFIED_CLEAN`. Four of the
five say some version of "we did not look". The one the user reads says
`verified clean`. Of the 71 extensions in CAPABILITIES, 50 had neither a GPS
walker nor a structural walker, and 29 of those 50 are declared COMPLETE.

Option B of that document adds one additive `coverage` field carrying which
checks ran and which did not, and prints it. This file is the measurement that
it says something TRUE, that it says it in TEXT, and that it did not break the
JSON report on its way.

WHAT IS ASSERTED HERE AND NOT ELSEWHERE

  - the additive-schema guarantee, key by key, against the shape measured on
    git HEAD before the change,
  - trap 2 inside the interface: `unaccounted_regions: []` used to mean two
    different things and `structure_applicable` now separates them,
  - a format with no GPS walker reporting NOT_CHECKED rather than nothing,
  - a real geotagged device file, measured before and after a real scrub,
  - the zero-needle decision, which is still OPEN and is collected here.

NO COORDINATE FROM THE DEVICE CORPUS APPEARS IN THIS FILE, and none is
asserted. Where a device file has to be characterised, it is characterised by
its GPS STATUS, by a byte count, or by a carrier class name, never by a value.
That is the same rule the design document held itself to.
"""

from __future__ import annotations

import io
import json
import os
import shutil
from contextlib import redirect_stdout

import pytest

from conftest import HAVE_FFMPEG, build
from metascrub import cli, gps_verify, structure, verify
from metascrub.capabilities import CAPABILITIES, Completeness, spec_for
from metascrub.gps_verify import GpsStatus
from metascrub.scrubber import MetadataScrubber


def scrub(path: str):
    """Run the shipping tool over one file and return its result dict."""
    with MetadataScrubber(backup=False) as scrubber:
        return scrubber.sanitize_file(path, remove_all=True)


# ===========================================================================
# THE JSON REPORT IS AN INTERFACE
# ===========================================================================

# The exact shape of `Verification.as_dict()` BEFORE the coverage field, key by
# key with the type of each value.
#
# Not copied out of a document: measured on 2026-09-07 by loading
# `git show HEAD:metascrub/verify.py` (HEAD was 69a0c96, "docs: add a
# changelog") as a module and dumping `Verification().as_dict()`. Seven keys,
# these seven types.
#
# `--report FILE` is documented in README.md and people parse it, so a change
# to any name, type or meaning below is a BREAKING change no matter how small
# the diff looks. The whole point of the coverage field being additive is that
# this table keeps passing unchanged.
SCHEMA_BEFORE_COVERAGE = {
    "verdict": str,
    "clean": bool,
    "residual_values": list,
    "remaining_tags": list,
    "checked_values": int,
    "detail": str,
    "unaccounted_regions": list,
}

# Per-result keys, measured the same day by running `--report` over a real
# scrub. The conditional ones (`error`, `name_leaks`, `renamed_to`,
# `rename_error`) are deliberately not here: they were conditional before this
# change and are conditional after it.
RESULT_KEYS_BEFORE_COVERAGE = {
    "file", "status", "sanitize_time", "removed_fields", "sanitized_fields",
    "engine", "completeness", "rewrites_container", "engine_note",
    "tags_before", "backup", "verification",
}

# Top level report keys, same measurement.
REPORT_KEYS_BEFORE_COVERAGE = {
    "sanitize_date", "total_files", "sanitized_files", "clean_files",
    "unsupported_files", "deferred_files", "error_files", "verified_clean",
    "files_with_residual_metadata", "filenames_with_leaks",
    "filenames_still_leaking", "renamed_files", "rename_failures",
    "total_fields_removed", "total_fields_sanitized", "sanitize_time",
    "results",
}


def test_the_report_schema_is_additive(tmp_path):
    """
    Every key that existed before this change is still present, with the same
    type, on a real scrub and on a bare Verification.

    A parser reading `verdict` or `clean` and ignoring everything else must be
    exactly as correct after this commit as before it. That is grade 1 in the
    design document's three grades of change, and it is the only grade this
    commit is allowed to be.
    """
    document, _sentinel = build(".pdf", tmp_path)
    result = scrub(document)
    verification = result["verification"]

    for key, expected in SCHEMA_BEFORE_COVERAGE.items():
        assert key in verification, f"{key} disappeared from the report"
        assert isinstance(verification[key], expected), (
            f"{key} changed type: {type(verification[key]).__name__} is not "
            f"{expected.__name__}"
        )

    # And on a hand-built one, which is the shape scrubber.py emits for a file
    # that was already clean.
    bare = verify.Verification().as_dict()
    for key, expected in SCHEMA_BEFORE_COVERAGE.items():
        assert key in bare and isinstance(bare[key], expected), key

    # Exactly one key was added, and it is the one that was reviewed.
    assert set(bare) - set(SCHEMA_BEFORE_COVERAGE) == {"coverage"}
    assert set(SCHEMA_BEFORE_COVERAGE) - set(bare) == set()

    assert RESULT_KEYS_BEFORE_COVERAGE <= set(result), (
        "a result key vanished: %s" % sorted(RESULT_KEYS_BEFORE_COVERAGE - set(result))
    )

    report_path = tmp_path / "report.json"
    with MetadataScrubber(backup=False) as scrubber:
        report = scrubber.generate_sanitization_report([result], str(report_path))
    assert REPORT_KEYS_BEFORE_COVERAGE <= set(report), (
        "a top level report key vanished: %s"
        % sorted(REPORT_KEYS_BEFORE_COVERAGE - set(report))
    )

    # The file on disk is still JSON, and the new field survives the trip.
    written = json.loads(report_path.read_text(encoding="utf-8"))
    assert written["results"][0]["verification"]["coverage"]["gps_status"] == \
        GpsStatus.NOT_CHECKED.value


def test_a_verification_with_no_coverage_emits_null_rather_than_a_default(tmp_path):
    """
    The absent case has to stay distinguishable too.

    `scrubber.py` builds a Verification by hand for a file that carried no
    metadata: nothing was scrubbed, so no check was run. Emitting a DEFAULT
    Coverage there would publish `structure_applicable: false`, which is the
    "we did not look" versus "we looked and found nothing" conflation this
    field was added to end, reintroduced one level down. Null is the honest
    answer and a parser can tell it apart.
    """
    assert verify.Verification().as_dict()["coverage"] is None

    # And a Coverage that nobody filled in must not claim otherwise.
    empty = verify.Coverage()
    assert empty.every_check_ran is False
    assert empty.as_dict()["unchecked"] == [
        verify.CHECK_GPS_CARRIERS, verify.CHECK_STRUCTURE_REGIONS,
    ]

    # A Coverage whose summary contradicts its own evidence cannot be built.
    with pytest.raises(ValueError):
        verify.Coverage(gps_status=GpsStatus.CLEAN.value)
    with pytest.raises(ValueError):
        verify.Coverage(gps_status=GpsStatus.NOT_CHECKED.value, unchecked=[])


# ===========================================================================
# CASE D: TRAP 2, LIVE, INSIDE THE INTERFACE
# ===========================================================================


def test_structure_applicable_separates_a_walk_that_happened_from_one_that_did_not(
        tmp_path):
    """
    Case D of docs/WHAT-THE-TOOL-CLAIMS.md, measured as a pair.

    Before this change, these two files produced `unaccounted_regions: []` in
    the same schema at the same moment and nothing in the JSON told them apart:

      a .gif  the structural walker RAN over every byte and found nothing
      a .pdf  no structural walker exists, so nothing was ever walked

    `structure.py`'s own docstring says those two states must never share a
    representation. `Verification.as_dict()` discarded `applicable` and emitted
    only the list, so in the interface people parse, they did.

    This asserts both halves: that the OLD keys really are identical on the two
    files, which is what makes the defect real rather than theoretical, and
    that the new key separates them.
    """
    walked, _s1 = build(".gif", tmp_path)
    never, _s2 = build(".pdf", tmp_path)

    assert ".gif" in structure.supported_extensions()
    assert ".pdf" not in structure.supported_extensions()

    walked_result = scrub(walked)["verification"]
    never_result = scrub(never)["verification"]

    # The defect, still reproducible on the keys that existed before.
    assert walked_result["unaccounted_regions"] == []
    assert never_result["unaccounted_regions"] == []
    assert walked_result["verdict"] == never_result["verdict"] == "verified_clean"
    assert walked_result["clean"] is never_result["clean"] is True

    # The fix.
    assert walked_result["coverage"]["structure_applicable"] is True
    assert never_result["coverage"]["structure_applicable"] is False

    # And in words, because a caller that renders the JSON should not have to
    # know which boolean to read.
    assert "every region" in walked_result["coverage"]["structure_detail"]
    assert "NOT CHECKED" in never_result["coverage"]["structure_detail"]
    assert ".pdf" in never_result["coverage"]["structure_detail"]


def test_a_leaking_file_still_reports_whether_the_structure_walk_happened(tmp_path):
    """
    The same trap on the RESIDUAL_FOUND path, which is where it would have been
    reintroduced.

    The structural scan used to run only after the residual scan came back
    empty, so on a leaking file there was no walk to report and
    `structure_applicable` would have had to say False about a walk that was
    never attempted. It now runs for every file, and this is the measurement
    that it does: a forced RESIDUAL_FOUND on a .gif must still say the walker
    ran.

    The needle is a string this test plants in the baseline dict rather than in
    the file, so RESIDUAL_FOUND is reached deterministically without depending
    on any engine failing.
    """
    path, _sentinel = build(".gif", tmp_path)
    with open(path, "rb") as handle:
        blob = handle.read()
    # A run of bytes that IS in the file, long enough to clear _MIN_NEEDLE and
    # not all digits, so meaningful_values keeps it.
    planted = "GIF89a" + "PLANTEDRESIDUE112603"
    with open(path, "wb") as handle:
        handle.write(blob.replace(b"GIF89a", planted.encode("latin-1"), 1))

    verification = verify.verify(
        path, spec_for(path), {"XMP:Creator": planted}, {},
    )

    assert verification.verdict is verify.Verdict.RESIDUAL_FOUND
    assert verification.coverage is not None, (
        "a leaking file lost its coverage statement"
    )
    assert verification.coverage.structure_applicable is True
    assert verification.coverage.checked_values == 1


# ===========================================================================
# A FORMAT THE GPS CHECK DOES NOT COVER
# ===========================================================================


def test_an_uncovered_format_says_not_checked_rather_than_implying_clean(tmp_path):
    """
    The 29 COMPLETE and blind extensions, in the form a user meets them.

    A .pdf is declared COMPLETE, has no GPS walker and no structural walker.
    Before this change the entire output for such a file was `SANITIZED` and
    `verified clean 1`, with nothing hedging anywhere. The verdict is
    deliberately unchanged; what is new is that it no longer travels alone.
    """
    document, _sentinel = build(".pdf", tmp_path)
    assert CAPABILITIES[".pdf"].completeness is Completeness.COMPLETE
    assert ".pdf" not in gps_verify.supported_extensions()

    verification = scrub(document)["verification"]
    coverage = verification["coverage"]

    # Unchanged, on purpose. Option D is not this commit.
    assert verification["verdict"] == "verified_clean"
    assert verification["clean"] is True

    # NOT_CHECKED, in gps_verify's own vocabulary rather than a seventh one.
    assert coverage["gps_status"] == GpsStatus.NOT_CHECKED.value
    assert coverage["gps_carriers_checked"] == []
    assert coverage["gps_findings"] == []
    assert coverage["structure_applicable"] is False
    assert coverage["every_check_ran"] is False
    assert sorted(coverage["unchecked"]) == [
        verify.CHECK_GPS_CARRIERS, verify.CHECK_STRUCTURE_REGIONS,
    ]

    # It says "we did not look". It does not say "clean", and it does not say
    # nothing, which is what it said before.
    assert "NOT CHECKED" in coverage["gps_detail"]
    assert "clean" not in coverage["gps_detail"].lower()
    assert coverage["detail"].strip()


def test_a_covered_format_reports_the_check_that_actually_ran(tmp_path):
    """
    The other side of the same coin, so the field cannot pass by always saying
    "not checked". A .jpg has both walkers; both must report as having run, and
    `every_check_ran` must be True.
    """
    path, _sentinel = build(".jpg", tmp_path)
    assert ".jpg" in gps_verify.supported_extensions()
    assert ".jpg" in structure.supported_extensions()

    coverage = scrub(path)["verification"]["coverage"]

    assert coverage["gps_status"] == GpsStatus.CLEAN.value
    assert coverage["gps_carriers_checked"] == ["exif-gps-ifd", "xmp-gps"]
    assert coverage["structure_applicable"] is True
    assert coverage["unchecked"] == []
    assert coverage["every_check_ran"] is True


def test_a_selective_run_that_did_not_target_gps_says_why_it_did_not_look(tmp_path):
    """
    The one case where NOT_CHECKED does not mean "no walker for this format".

    A selective removal of the Artist must not be failed for the GPS the user
    deliberately kept; `gps_verify.gps_in_scope()` exists for that reason. So
    the check is not run, the status is NOT_CHECKED, and the detail says which
    kind of "not checked" it was. Two states that must not be confused must not
    share a sentence either.
    """
    path, _sentinel = build(".jpg", tmp_path)
    with MetadataScrubber(backup=False) as scrubber:
        result = scrubber.sanitize_file(
            path, fields_to_remove=["Artist"], remove_all=False)

    coverage = result["verification"]["coverage"]
    assert coverage["gps_status"] == GpsStatus.NOT_CHECKED.value
    assert "Artist" in coverage["gps_detail"]
    assert "no GPS walker" not in coverage["gps_detail"], (
        "a scoped-out check must not claim the format is uncovered"
    )
    assert coverage["every_check_ran"] is False


# ===========================================================================
# IT HAS TO REACH SOMEBODY
# ===========================================================================


def _fake_result(**coverage):
    base = {
        "checked_values": 0,
        "gps_status": GpsStatus.NOT_CHECKED.value,
        "gps_carriers_checked": [],
        "gps_findings": [],
        "gps_detail": "GPS carriers: NOT CHECKED (no GPS walker for .dng)",
        "structure_applicable": False,
        "structure_detail": "structure: NOT CHECKED (no structural walker for .dng)",
        "unchecked": [verify.CHECK_GPS_CARRIERS, verify.CHECK_STRUCTURE_REGIONS],
        "every_check_ran": False,
        "detail": "residual byte scan: 0 value(s) searched for; GPS carriers: "
                  "NOT CHECKED (no GPS walker for .dng)",
    }
    base.update(coverage)
    return {
        "file": "photo.dng",
        "status": "sanitized",
        "engine": "exiftool",
        "verification": {"verdict": "verified_clean", "clean": True,
                         "residual_values": [], "remaining_tags": [],
                         "checked_values": base["checked_values"], "detail": "",
                         "unaccounted_regions": [], "coverage": base},
    }


def test_the_cli_prints_what_was_not_checked():
    """
    The channel, at the far end. gps_verify computed a correct and specific
    sentence about an 18.4 MB trailer and there was nowhere for it to go; a
    field nobody prints is the same defect one layer down.

    Printed in full and never truncated, because the specific sentence IS the
    value of the line.
    """
    buffer = io.StringIO()
    with redirect_stdout(buffer):
        cli._print_result(_fake_result())
    printed = buffer.getvalue()

    assert "NOT FULLY CHECKED" in printed
    assert "no GPS walker for .dng" in printed


def test_the_cli_stays_quiet_when_every_check_ran():
    """
    A line printed on every file is a line nobody reads. The ordinary,
    fully-checked case must look exactly as it did before this change.
    """
    buffer = io.StringIO()
    with redirect_stdout(buffer):
        cli._print_result(_fake_result(
            gps_status=GpsStatus.CLEAN.value,
            gps_carriers_checked=["exif-gps-ifd"],
            gps_detail="GPS carriers: none present [checked exif-gps-ifd]",
            structure_applicable=True,
            structure_detail="structure: every region of the output is accounted for",
            unchecked=[],
            every_check_ran=True,
        ))
    printed = buffer.getvalue()

    assert "NOT FULLY CHECKED" not in printed
    assert "SANITIZED" in printed


def test_the_cli_reports_a_gps_carrier_found_in_the_output():
    """
    The one part of this block that is a finding rather than a limit. The
    verdict does not change (that is Option D and it is not this commit), so
    the words are the only channel, and they have to be unmistakable.
    """
    buffer = io.StringIO()
    with redirect_stdout(buffer):
        cli._print_result(_fake_result(
            gps_status=GpsStatus.CARRIER_FOUND.value,
            gps_carriers_checked=["exif-gps-ifd"],
            gps_findings=["exif-gps-ifd: a GPS IFD with 4 entr(ies)"],
            gps_detail="GPS carriers: FOUND 1",
            structure_applicable=True,
            structure_detail="structure: every region of the output is accounted for",
            unchecked=[],
            every_check_ran=True,
        ))
    printed = buffer.getvalue()

    assert "GPS CARRIER STILL PRESENT" in printed
    assert "a GPS IFD with 4 entr(ies)" in printed


def test_the_gui_carries_the_distinction_as_text():
    """
    Trap 6. DANGER and OK measure a 1.07 contrast ratio against each other, so
    anything the GUI says only in colour is not said at all. The Verification
    cell reads "verified clean" for both a fully checked file and one where two
    of three checks never ran, so the Detail cell has to carry the difference
    in words.
    """
    gui = pytest.importorskip("metascrub.gui")

    unchecked = gui._with_coverage("", _fake_result())
    checked = gui._with_coverage("", _fake_result(
        gps_status=GpsStatus.CLEAN.value,
        gps_carriers_checked=["exif-gps-ifd"],
        gps_detail="GPS carriers: none present [checked exif-gps-ifd]",
        structure_applicable=True,
        structure_detail="structure: every region of the output is accounted for",
        unchecked=[],
        every_check_ran=True,
    ))

    assert "NOT FULLY CHECKED" in unchecked
    assert unchecked != checked
    assert checked == "", "a fully checked file must not gain a hedge"

    # An existing detail is kept, never replaced. Losing the PARTIAL line to
    # make room for this one would be a straight trade of one warning for
    # another.
    kept = gui._with_coverage("PARTIAL: raw container", _fake_result())
    assert kept.startswith("PARTIAL: raw container")
    assert "NOT FULLY CHECKED" in kept


# ===========================================================================
# THE ZERO-NEEDLE DECISION: CLOSED
# ===========================================================================


def test_the_zero_needle_decision_is_closed(tmp_path):
    """
    THE DEFERRAL COLLECTOR, DISCHARGED. This test used to assert that the
    decision was still open. It now asserts that it is closed and that the flag
    which recorded it as open is GONE, so the deferral cannot be reopened by
    accident and cannot linger as a constant nobody reads.

    Decided 2026-09-07 by the owner: `checked_values == 0` gets its own verdict,
    `Verdict.NO_BASELINE_VALUES`, and NOT a second meaning for `UNVERIFIED`.
    UNVERIFIED means verification could not run; this is verification that ran
    over an empty set. The full measurement of the new behaviour lives in
    tests/test_zero_needle.py; what is asserted HERE is only that the deferral
    is discharged and that the coverage wiring this file owns still reports the
    same number beside it.
    """
    assert not hasattr(verify, "ZERO_NEEDLE_DECISION_IS_OPEN"), (
        "the zero-needle deferral flag is back. The decision was made on "
        "2026-09-07 and the flag was deleted rather than set False, precisely "
        "so nothing could read it and reopen the question quietly."
    )

    path, _sentinel = build(".png", tmp_path)
    scrub(path)

    # An empty baseline is the zero-needle case in its purest form: the
    # residual scan has nothing to look for, and now says so.
    verification = verify.verify(path, spec_for(path), {}, {})

    assert verification.checked_values == 0
    assert verification.verdict is verify.Verdict.NO_BASELINE_VALUES
    assert verification.clean is False

    # The coverage statement this file exists to measure is unchanged by the
    # decision: the number is still reported next to the verdict, in words.
    assert verification.coverage is not None
    assert verification.coverage.checked_values == 0
    assert "0 value(s) searched for" in verification.coverage.detail


# ===========================================================================
# REAL DEVICE MEDIA
#
# Read in place, never copied into this repository, and scrubbed only in
# pytest's temporary directory. Several of these files carry real coordinates
# belonging to strangers; nothing below asserts, prints or records one. The
# corpus is described in
# <device-corpus>\MANIFEST.md.
# ===========================================================================

_DEVICE_CORPUS = os.environ.get(
    "METASCRUB_DEVICE_MEDIA",
    os.path.join("D:", os.sep, "Projects", "_Meta",
                 "metascrub-device-media", "sourced"),
)

requires_device_corpus = pytest.mark.skipif(
    not os.path.isdir(_DEVICE_CORPUS),
    reason="the real device corpus is not on this machine: " + _DEVICE_CORPUS,
)

# The Galaxy S8's SEF trailer, measured on 2026-09-06 with a separate
# instrument and recorded in the corpus manifest. gps_verify reaches the same
# figure from the other direction, by consuming every JPEG marker segment
# rather than by searching, which is what makes either number worth anything.
_SAMSUNG_TRAILER_BYTES = 18469861


@requires_device_corpus
def test_a_real_geotagged_jpeg_is_measured_before_and_after(tmp_path):
    """
    THE SENTENCE THAT HAD NOWHERE TO GO, now measured at both ends.

    Before the scrub, `gps_verify` says of this file: PARTLY CHECKED, none
    found, but 18,469,861 bytes follow the JPEG EOI and were not interrogated
    for GPS carriers. That is exactly what a user needs to know about that
    file, it was computed correctly on 2026-09-07, and it appeared nowhere.

    After the scrub the trailer is gone, so the same check reaches the whole
    file and reports CLEAN. The coverage field therefore says something
    different before and after, and both statements are true. A field that said
    the same thing either way would be measuring nothing.
    """
    name = "Samsung SM-G950F (Galaxy S8).jpg"
    original = os.path.join(_DEVICE_CORPUS, name)

    # BEFORE, on the untouched original, read in place.
    before = gps_verify.scan(original)
    assert before.status is GpsStatus.INCOMPLETE, before.describe()
    assert before.findings == ()
    assert len(before.limits) == 1
    assert before.limits[0].startswith("%d bytes follow" % _SAMSUNG_TRAILER_BYTES), (
        before.limits[0]
    )

    # AFTER, on a copy, through the shipping tool.
    working = str(tmp_path / name)
    shutil.copy2(original, working)
    coverage = scrub(working)["verification"]["coverage"]

    assert coverage["gps_status"] == GpsStatus.CLEAN.value, coverage["gps_detail"]
    assert coverage["gps_carriers_checked"] == ["exif-gps-ifd", "xmp-gps"]
    assert coverage["gps_findings"] == []
    assert coverage["structure_applicable"] is True
    assert coverage["every_check_ran"] is True
    assert coverage["checked_values"] > 0

    # The 18 MB sentence is gone because the trailer is gone, not because the
    # check stopped saying it.
    assert str(_SAMSUNG_TRAILER_BYTES) not in coverage["detail"]
    assert os.path.getsize(working) < os.path.getsize(original)


@requires_device_corpus
@pytest.mark.skipif(not HAVE_FFMPEG, reason="the av engine needs ffmpeg")
def test_a_real_geotagged_video_carries_a_gps_carrier_until_it_is_scrubbed(tmp_path):
    """
    The other direction: a carrier that is genuinely THERE before the scrub.

    `Nokia 6.1.mp4` is real geotagged Android video with an ISO 6709 `(c)xyz`
    atom in `moov/udta`. It is also the file that produced the design
    document's Case B: after the scrub its verdict is VERIFIED_CLEAN over a
    residual scan of ZERO strings. Both facts are asserted here, on one file,
    because they are the argument for the coverage field and for the decision
    that is still open, respectively.

    The carrier is identified by its BOX TYPE and its byte length. No
    coordinate is read out of it, here or by gps_verify.
    """
    name = "Nokia 6.1.mp4"
    original = os.path.join(_DEVICE_CORPUS, name)

    before = gps_verify.scan(original)
    assert before.status is GpsStatus.CARRIER_FOUND, before.describe()
    assert len(before.findings) == 1
    assert before.findings[0].carrier == gps_verify.CARRIER_ISOBMFF_LOCATION
    assert "(c)xyz" in before.findings[0].detail

    working = str(tmp_path / name)
    shutil.copy2(original, working)
    coverage = scrub(working)["verification"]["coverage"]

    assert coverage["gps_status"] == GpsStatus.CLEAN.value, coverage["gps_detail"]
    assert coverage["gps_findings"] == []
    assert coverage["every_check_ran"] is True

    # Case B, on a real device file, still true and now at least visible: the
    # residual byte scan searched for nothing at all, and the verdict is
    # unchanged because the zero-needle decision is the owner's.
    assert coverage["checked_values"] == 0
    assert "0 value(s) searched for" in coverage["detail"]
