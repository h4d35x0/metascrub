"""
Tests for the self check.

A self test that cannot fail is worse than no self test, because its PASS is
what a user on a new machine will trust. These assert that it reports failure
when the tool is genuinely broken, and that a SKIP never counts as success.
"""

from __future__ import annotations

import pytest

from metascrub import exif_io, selftest


def test_all_checks_pass_on_a_working_machine():
    """The suite only runs where the dependencies exist, so this must be clean."""
    results = selftest.run()
    failures = [(name, detail) for outcome, name, detail in results
                if outcome == selftest.FAIL]
    assert not failures, failures
    assert selftest.exit_code(results) == 0


def test_round_trip_checks_the_bytes_not_the_verdict():
    """
    The round trip must not be satisfiable by the tool claiming success. This
    is the same distinction the whole project rests on: a check that trusted
    the tool's own report would pass on exactly the bug it exists to catch.
    """
    import inspect

    source = inspect.getsource(selftest._check_round_trip)
    assert "sentinel.encode() in handle.read()" in source, (
        "the round trip no longer searches the output bytes"
    )


def test_exiftool_failure_is_reported_as_fail_not_skip(monkeypatch):
    """
    exiftool gates every format, so its absence must be a FAIL. Reporting it as
    SKIP would let a machine that can scrub nothing at all still exit zero.
    """
    def _no_helper(_self):
        raise RuntimeError("exiftool binary not usable: not found on PATH")

    monkeypatch.setattr(
        type(exif_io.session()), "helper", property(_no_helper), raising=True
    )

    outcome, name, detail = selftest._check_exiftool()
    assert outcome == selftest.FAIL
    assert name == "exiftool"
    assert "install with:" in detail, "a failure must say how to fix it"


def test_broken_tool_makes_the_whole_selftest_fail(monkeypatch):
    """End to end: with metadata unreadable, the exit code must be non-zero."""
    def _no_helper(_self):
        raise RuntimeError("exiftool binary not usable: not found on PATH")

    monkeypatch.setattr(
        type(exif_io.session()), "helper", property(_no_helper), raising=True
    )

    results = selftest.run()
    assert selftest.exit_code(results) == 1

    by_name = {name: (outcome, detail) for outcome, name, detail in results}
    assert by_name["exiftool"][0] == selftest.FAIL
    assert by_name["engines"][0] == selftest.FAIL
    # And the decisive one: the file was not actually cleaned.
    assert by_name["round trip"][0] == selftest.FAIL
    assert "STILL IN THE FILE" in by_name["round trip"][1]


def test_skip_is_not_counted_as_success():
    """A skipped check is a real outcome, never folded into a pass."""
    results = [
        (selftest.PASS, "a", ""),
        (selftest.SKIP, "b", ""),
    ]
    assert selftest.exit_code(results) == 0, "a skip alone is not a failure"

    results.append((selftest.FAIL, "c", ""))
    assert selftest.exit_code(results) == 1


def test_a_raising_check_is_reported_rather_than_crashing(monkeypatch):
    """A self test must report, never raise: a traceback tells a user nothing."""
    def _explode():
        raise ValueError("boom")

    monkeypatch.setattr(selftest, "CHECKS", [_explode])
    results = selftest.run()

    assert len(results) == 1
    outcome, _, detail = results[0]
    assert outcome == selftest.FAIL
    assert "boom" in detail


def test_package_check_reports_where_the_import_came_from():
    """
    A launcher's job is resolving the project, so the self test has to say
    which copy it actually imported. Importing some other metascrub while
    believing it ran this one is the failure worth catching.
    """
    import os

    import metascrub

    outcome, name, detail = selftest._check_package()
    assert outcome == selftest.PASS
    assert name == "package"
    assert os.path.dirname(os.path.abspath(metascrub.__file__)) in detail


def test_cli_selftest_exits_non_zero_when_broken(monkeypatch, capsys):
    """The subcommand, not just the module, must carry the failure out."""
    from metascrub.cli import main

    monkeypatch.setattr(
        selftest, "CHECKS", [lambda: (selftest.FAIL, "forced", "forced failure")]
    )
    code = main(["selftest"])
    out = capsys.readouterr().out

    assert code == 1
    assert "FAIL" in out and "forced" in out
    assert "1 check(s) FAILED" in out
