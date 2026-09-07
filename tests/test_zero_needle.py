"""
The zero-needle case: a verification that ran over an empty set.

WHAT THIS FILE MEASURES

`verify.py` opens by rejecting inference: "Success is measured, never
inferred". docs/WHAT-THE-TOOL-CLAIMS.md section 2 Case B then measured, on
2026-09-07, the one place where measured and inferred were provably the same
code path. When a baseline read produces no searchable value, `residual_scan()`
returns immediately, nothing survives because nothing was looked for, and the
tool printed the same word it prints after searching fifteen values.
`Nokia 6.1.mp4`, a real geotagged Android video, did exactly this: 58 baseline
tags, `checked_values=0`, `VERIFIED_CLEAN`.

The owner decided it on 2026-09-07: the case gets its OWN verdict,
`Verdict.NO_BASELINE_VALUES`, and not a second meaning for `UNVERIFIED`.
UNVERIFIED means verification COULD NOT RUN. This is verification that RAN and
had nothing to measure. Two different facts, and this codebase does not let two
such facts share a representation; `MetadataRead`/`ReadOutcome`, `GpsStatus`
and `StructureReport.applicable` all exist for that reason.

WHAT IS ASSERTED HERE AND NOT ELSEWHERE

  - the verdict is produced for a genuinely empty needle set, and NOT for a
    file whose baseline yields needles,
  - it never satisfies `Verification.clean`, asserted over the whole enum
    rather than over the one interesting member,
  - the COMPLETE-format downgrade in `scrubber.py` treats it as NOT a finding,
    asserted BOTH ways and over the whole enum, because getting that wrong
    either fails clean files or reintroduces the hole,
  - the CLI and the GUI both render it distinctly in TEXT (trap 6),
  - it holds end to end on a real device file, on a real GPS-only JPEG, and
    on a file whose only metadata is a coordinate.

NO COORDINATE IS PRINTED OR ASSERTED. The device corpus is read in place and
never copied into this repository; the one synthetic coordinate below is a
made-up number that was never on a device, and it is used only to produce a
baseline whose every value is numeric.
"""

from __future__ import annotations

import io
import os
import shutil
from contextlib import redirect_stdout

import pytest

from conftest import HAVE_FFMPEG, build, exiftool_or_fail
from metascrub import cli, verify
from metascrub.capabilities import CAPABILITIES, Completeness, spec_for
from metascrub.scrubber import (
    STATUS_ERROR, STATUS_SANITIZED, MetadataScrubber,
)
from metascrub.verify import Verdict, Verification

# A made-up fix. It is not a place anyone has been, and nothing below prints it.
# Its only job is to be a baseline made entirely of numbers: exiftool reports
# GPS numerically under `-n`, and `meaningful_values()` drops every purely
# numeric string because those collide with ordinary binary content. So a file
# carrying nothing but a coordinate produces a baseline with tags in it and no
# needles out of it, which is the case under test.
_LAT, _LON = 11.25, -22.5


def scrub(path):
    """Run the shipping tool over one file and return its result dict."""
    with MetadataScrubber(backup=False) as scrubber:
        return scrubber.sanitize_file(path, remove_all=True)


def gps_only_jpeg(tmp_path, name="gpsonly.jpg"):
    """
    A JPEG whose only metadata is a GPS fix.

    This is the synthetic `gpsonly.jpg` row of the design document's table,
    rebuilt here. Measured 2026-09-07 with exiftool 13.29 through this
    project's own read flags: 8 real tags in the baseline, 0 needles out of
    `meaningful_values()`.

    The `-all=` pass matters. Without it the fixture keeps whatever PIL wrote,
    and a single string tag would give the residual scan something to search
    for, which would make every assertion below vacuous. The needle count is
    asserted before the file is handed back, so this can never silently stop
    being a zero-needle fixture.
    """
    from PIL import Image

    path = str(tmp_path / name)
    Image.new("RGB", (64, 48), (30, 90, 160)).save(path, format="JPEG")

    session = exiftool_or_fail()
    session.execute("-all=", "-overwrite_original", path)
    session.execute(
        f"-GPSLatitude={_LAT}", "-GPSLatitudeRef=N",
        f"-GPSLongitude={_LON}", "-GPSLongitudeRef=W",
        "-GPSAltitude=76.5", "-GPSAltitudeRef=0",
        "-overwrite_original", path,
    )

    read = session.read(path)
    needles = verify.meaningful_values(read.metadata)
    assert needles == set(), (
        "this fixture is only worth anything if its baseline yields NO "
        f"needles; it yielded {sorted(needles)}"
    )
    assert any("GPS" in key for key in read.metadata), (
        "the fixture carries no GPS, so it is not the file this test means"
    )
    return path


# ===========================================================================
# THE VERDICT ITSELF
# ===========================================================================


def test_an_empty_needle_set_is_not_verified_clean(tmp_path):
    """
    The branch, in its purest form: a baseline with nothing searchable in it.

    Before 2026-09-07 this returned VERIFIED_CLEAN with `checked_values=0`,
    which is the sentence the module docstring opens by rejecting.
    """
    path, _sentinel = build(".png", tmp_path)
    scrub(path)

    verification = verify.verify(path, spec_for(path), {}, {})

    assert verification.verdict is Verdict.NO_BASELINE_VALUES
    assert verification.checked_values == 0
    assert verification.clean is False
    assert "searched the output for nothing" in verification.detail


def test_a_baseline_with_values_still_verifies_clean(tmp_path):
    """
    The other half, and the one that keeps the change honest.

    A new verdict that fires everywhere would be a regression wearing a
    correction's clothes. A fixture carrying a real sentinel produces needles,
    the scan searches for them, and the verdict is unchanged.
    """
    path, sentinel_value = build(".png", tmp_path)
    result = scrub(path)
    verification = result["verification"]

    assert verification["verdict"] == Verdict.VERIFIED_CLEAN.value, verification
    assert verification["clean"] is True
    assert verification["checked_values"] > 0, (
        "this test measures nothing unless the baseline produced needles"
    )
    assert sentinel_value not in verification["residual_values"]


@pytest.mark.parametrize("verdict", list(Verdict))
def test_only_verified_clean_is_clean(verdict):
    """
    `Verification.clean` over the WHOLE enum, not over the one new member.

    A cross-product rather than an illustrative example, because this property
    is what every count, every exit code and every green row downstream is
    computed from.
    """
    assert Verification(verdict=verdict).clean is (verdict is Verdict.VERIFIED_CLEAN)


def test_the_new_verdict_is_distinct_from_unverified():
    """
    The decision, stated as an assertion so it cannot be undone by a merge.

    "Verification could not run" and "verification ran over an empty set" are
    different facts. If someone later aliases one to the other, this fails.
    """
    assert Verdict.NO_BASELINE_VALUES is not Verdict.UNVERIFIED
    assert Verdict.NO_BASELINE_VALUES.value != Verdict.UNVERIFIED.value
    assert Verdict.NO_BASELINE_VALUES.value == "no_baseline_values"
    assert len({v.value for v in Verdict}) == len(list(Verdict)), (
        "two verdicts share a wire value, which is the conflation this "
        "verdict exists to refuse"
    )


def test_a_positive_finding_still_wins_over_an_empty_needle_set(tmp_path):
    """
    Branch ORDER. A file with zero needles AND an unaccounted region must
    report the region: that is a measurement of something that is there, and
    it is the more useful sentence.

    The trailing bytes are appended AFTER the scrub so the file under
    verification really does carry them.
    """
    path, _sentinel = build(".png", tmp_path)
    scrub(path)
    with open(path, "ab") as handle:
        handle.write(b"TRAILING PAYLOAD" * 64)

    verification = verify.verify(path, spec_for(path), {}, {})

    assert verification.checked_values == 0
    assert verification.verdict is Verdict.STRUCTURE_UNACCOUNTED, (
        "an empty needle set must not be allowed to mask a structural finding"
    )


# ===========================================================================
# THE COMPLETE-FORMAT DOWNGRADE
#
# The dangerous item on the cost list. `scrubber.py` turns a COMPLETE format
# that did not verify clean into STATUS_ERROR. Getting the new verdict's place
# in that rule wrong either fails files that were genuinely cleaned or lets the
# hole back in quietly, so it is asserted in both directions and over the whole
# enum rather than on one example.
# ===========================================================================


_VERDICTS_THAT_MUST_FAIL_A_COMPLETE_FORMAT = {
    Verdict.RESIDUAL_FOUND,
    Verdict.CARRIERS_REMAIN,
    Verdict.UNVERIFIED,
    Verdict.STRUCTURE_UNACCOUNTED,
}


@pytest.mark.parametrize("verdict", list(Verdict))
def test_the_complete_downgrade_over_every_verdict(verdict, tmp_path, monkeypatch):
    """
    Every verdict, against a real COMPLETE-format scrub of a real file.

    `verify` is replaced so the verdict is the only variable; everything else
    on the path, including the downgrade under test, is the shipping code.
    A test that only exercised the two verdicts a fixture happens to produce
    would buy confidence it had not earned.
    """
    path, _sentinel = build(".png", tmp_path)
    assert CAPABILITIES[".png"].completeness is Completeness.COMPLETE

    def fake_verify(*_args, **_kwargs):
        return Verification(verdict=verdict, detail="planted for this test")

    monkeypatch.setattr("metascrub.scrubber.verify", fake_verify)
    result = scrub(path)

    if verdict in _VERDICTS_THAT_MUST_FAIL_A_COMPLETE_FORMAT:
        assert result["status"] == STATUS_ERROR, result
        assert "verification failed" in result["error"]
    else:
        assert result["status"] == STATUS_SANITIZED, result
        assert "error" not in result, (
            "a file that was cleaned and carried no finding must not be "
            "reported as a failure"
        )


def test_a_zero_needle_complete_format_is_not_an_error(tmp_path):
    """
    End to end, no monkeypatch: a real COMPLETE-format file whose baseline
    yields no needles stays SANITIZED, and is not counted as verified clean.

    Both halves matter. The status is true (metadata was present and was
    removed) and the count is true (nothing was proven). Failing the file would
    be a false statement about a file that is fine; counting it would be the
    original defect.
    """
    path = gps_only_jpeg(tmp_path)
    assert CAPABILITIES[".jpg"].completeness is Completeness.COMPLETE

    with MetadataScrubber(backup=False) as scrubber:
        result = scrubber.sanitize_file(path, remove_all=True)
        report = scrubber.generate_sanitization_report([result])

    assert result["status"] == STATUS_SANITIZED, result
    assert "error" not in result, result
    assert result["tags_before"], (
        "a file with no baseline tags takes the STATUS_CLEAN path and is not "
        "the case under test"
    )

    verification = result["verification"]
    assert verification["verdict"] == "no_baseline_values", verification
    assert verification["clean"] is False
    assert verification["checked_values"] == 0

    assert report["verified_clean"] == 0, (
        "a file nothing was measured on must not be counted as verified clean"
    )
    assert report["files_with_residual_metadata"] == [], (
        "nothing was found in this file, so it must not be reported as leaking"
    )
    assert report["error_files"] == 0


# ===========================================================================
# WHAT THE USER ACTUALLY READS
#
# Trap 6: DANGER and OK measure a 1.07 contrast ratio against each other, so
# anything said only in colour is not said at all. The row is still SANITIZED
# and still carries the SANITIZED colour, which makes text the ONLY channel
# this distinction can travel down.
# ===========================================================================


def _result(verdict, **verification):
    payload = {
        "verdict": verdict,
        "clean": verdict == "verified_clean",
        "residual_values": [],
        "remaining_tags": [],
        "checked_values": 0,
        "detail": "",
        "unaccounted_regions": [],
        "coverage": None,
    }
    payload.update(verification)
    return {
        "file": "photo.jpg",
        "status": STATUS_SANITIZED,
        "engine": "exiftool",
        "verification": payload,
    }


def test_the_cli_says_it_in_text():
    buffer = io.StringIO()
    with redirect_stdout(buffer):
        cli._print_result(_result("no_baseline_values"))
    printed = buffer.getvalue()

    assert "NOT PROVEN CLEAN" in printed
    assert "0 value(s)" in printed
    assert "verified clean" not in printed

    clean = io.StringIO()
    with redirect_stdout(clean):
        cli._print_result(_result("verified_clean", checked_values=9, clean=True))
    assert "NOT PROVEN CLEAN" not in clean.getvalue(), (
        "a file that really was measured must not gain a hedge"
    )


def test_the_gui_says_it_in_text():
    """
    The fifth branch of `_verdict_text`. Before it, this verdict fell through
    to "not verified", which fails safe and reads as the wrong thing: that
    phrase means the check could not run.
    """
    gui = pytest.importorskip("metascrub.gui")

    text = gui._verdict_text(_result("no_baseline_values"))

    assert text not in ("verified clean", "clean (partial format)"), (
        "the GUI must not call an unmeasured file clean"
    )
    assert text != "not verified", (
        "this verdict must not fall through to the unrecognised-verdict text"
    )
    assert text == "not proven (0 values)"

    # Every verdict the enum can produce reaches a cell of its own, so a new
    # one cannot land silently in the fallback again.
    rendered = {
        verdict: gui._verdict_text(_result(verdict.value)) for verdict in Verdict
    }
    fell_through = [
        verdict.value for verdict, text in rendered.items()
        if text == "not verified" and verdict is not Verdict.UNVERIFIED
    ]
    assert not fell_through, (
        f"these verdicts render as the fallback text: {fell_through}"
    )


# ===========================================================================
# REAL DEVICE MEDIA
#
# Read in place, never copied into this repository, scrubbed only in pytest's
# temporary directory, and characterised only by tag counts and verdicts.
# Nothing here asserts, prints or records a coordinate.
# ===========================================================================

_DEVICE_CORPUS = os.environ.get(
    "METASCRUB_DEVICE_MEDIA",
    os.path.join("D:", os.sep, "Projects", "_Meta",
                 "metascrub-device-media", "sourced"),
)

_ZERO_NEEDLE_DEVICE_FILE = "Nokia 6.1.mp4"

requires_device_corpus = pytest.mark.skipif(
    not os.path.isfile(os.path.join(_DEVICE_CORPUS, _ZERO_NEEDLE_DEVICE_FILE)),
    reason="the real device corpus is not on this machine: " + _DEVICE_CORPUS,
)


@requires_device_corpus
@pytest.mark.skipif(not HAVE_FFMPEG, reason="the av engine needs ffmpeg")
def test_the_real_device_file_that_started_this(tmp_path):
    """
    `Nokia 6.1.mp4`, the file the design document measured.

    Measured there on 2026-09-07: a real geotagged Android video, 58 baseline
    tags, one of them a GPS coordinate, `checked_values=0`, verdict
    VERIFIED_CLEAN. Re-measured here after the decision: same 0, new verdict,
    and the status is still SANITIZED because the file really was cleaned.

    This is the whole change in one file: what it says changed, what it does
    did not.
    """
    source = os.path.join(_DEVICE_CORPUS, _ZERO_NEEDLE_DEVICE_FILE)
    working = str(tmp_path / "device.mp4")
    shutil.copyfile(source, working)

    result = scrub(working)
    verification = result["verification"]

    assert result["status"] == STATUS_SANITIZED, result.get("error")
    assert len(result["tags_before"]) > 20, (
        "this file is only interesting because its baseline is rich and its "
        "needle set is still empty"
    )
    assert verification["checked_values"] == 0
    assert verification["verdict"] == "no_baseline_values", verification
    assert verification["clean"] is False
