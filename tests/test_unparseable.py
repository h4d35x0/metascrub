"""
A file exiftool could not parse must never be reported CLEAN.

The defect these tests close, measured 2026-09-04 with exiftool 13.29 on a
truncated .svg carrying sodipodi:docname:

    [ExifTool] Warning  : XMP format error (no closing tag for svg) [x2]
    [File]     FileType : SVG
    (no [SVG] tags at all)

The read SUCCEEDS. exiftool identifies the file, exits zero, and surfaces
nothing, because it could not parse the document. Everything it emits lands in
the File and ExifTool pseudo-groups, `_real_tags()` is empty, and the scrubber's
CLEAN short circuit then told the user "no metadata carriers found" about a file
carrying an editor's docname.

The obvious fix, "any ExifTool warning means do not take the CLEAN branch", is
wrong, and this file proves it is wrong rather than asserting it. exiftool emits
`Unrecognized MIMEType application/vnd.oasis.opendocument.text-template` on a
perfectly good .ott, and a scrubbed .ott reports ZERO document tags: it lands in
the exact shape of the defect. So the two populations below are both required,
and the .ott case is the one that decides whether the predicate is honest.

POPULATION A: parseable and empty. Must stay CLEAN and must not be rewritten.
POPULATION B: unparseable. Must not be CLEAN, must not be touched, exit non-zero.
"""

from __future__ import annotations

import os

import pytest

from conftest import ALL_KINDS, build, contains_anywhere, raw_contains, sentinel
from metascrub import STATUS_CLEAN, STATUS_ERROR, MetadataScrubber
from metascrub import exif_io
from metascrub.exif_io import ReadOutcome, parse_failures
from metascrub.scrubber import _real_tags


# The measured vocabulary. Both lists came out of a run over the whole fixture
# corpus, not out of anyone's idea of what exiftool says. They are here so the
# predicate is asserted against evidence rather than against itself.

# Seen on files exiftool read successfully: on .ott, .ots, .otp and .otg, both
# as built and after scrubbing, on reads that ALSO returned the document's
# Title and Creator.
BENIGN_WARNINGS = [
    "Unrecognized MIMEType application/vnd.oasis.opendocument.text-template",
    "Unrecognized MIMEType application/vnd.oasis.opendocument.spreadsheet-template",
    "Unrecognized MIMEType application/vnd.oasis.opendocument.presentation-template",
    "Unrecognized MIMEType application/vnd.oasis.opendocument.graphics-template",
]

# Seen only on deliberately truncated copies of those same fixtures.
PARSE_FAILURE_WARNINGS = [
    "XMP format error (no closing tag for svg) [x2]",
    "Format error reading ZIP file",
    "JPEG format error",
    "Truncated PNG image",
    "Error reading RIFF file (corrupted?)",
    "Invalid xref table",
    "Truncated 'mdat' data at offset 0x28",
    "Truncated 'moov' data (missing 452 bytes)",
    "Format error in FLAC file",
]

# The four formats that reach the CLEAN branch AND carry a benign warning while
# doing it. Named rather than derived, so this cannot quietly become an empty
# set the way DEFERRED once did.
BENIGN_WARNING_KINDS = [".ott", ".ots", ".otp", ".otg"]


def _scrub(path, backup=False):
    with MetadataScrubber(backup=backup) as scrubber:
        return scrubber.sanitize_file(path, remove_all=True)


def _read(path):
    return exif_io.session().read(path)


def _truncate(source, dest, fraction):
    with open(source, "rb") as fh:
        blob = fh.read()
    with open(dest, "wb") as fh:
        fh.write(blob[: max(1, int(len(blob) * fraction))])
    return dest


# THE PREDICATE ITSELF, over the measured cross-product. No exiftool needed, so
# these run everywhere and run fast.


@pytest.mark.parametrize("warning", BENIGN_WARNINGS)
def test_a_measured_benign_warning_does_not_make_a_read_unparsed(warning):
    assert parse_failures({"ExifTool:Warning": warning}) == ()


@pytest.mark.parametrize("warning", PARSE_FAILURE_WARNINGS)
def test_a_measured_parse_failure_warning_makes_a_read_unparsed(warning):
    assert parse_failures({"ExifTool:Warning": warning}) == (warning,)


@pytest.mark.parametrize("warning", PARSE_FAILURE_WARNINGS)
@pytest.mark.parametrize("benign", BENIGN_WARNINGS)
def test_a_benign_warning_never_masks_a_parse_failure_beside_it(warning, benign):
    """
    exiftool can raise both at once, and a list-valued Warning is how pyexiftool
    reports that. The benign one must not launder the other.
    """
    assert parse_failures({"ExifTool:Warning": [benign, warning]}) == (warning,)


def test_an_exiftool_error_is_a_parse_failure_too():
    assert parse_failures({"ExifTool:Error": "Unknown file type"}) == ("Unknown file type",)


def test_document_tags_are_never_mistaken_for_warnings():
    """The predicate reads the ExifTool group only. A document tag whose name
    happens to contain a warning-ish word is not a complaint about parsing."""
    metadata = {
        "SVG:Warning": "this is a document value, not exiftool speaking",
        "XMP:Errors": "so is this",
        "File:FileType": "SVG",
    }
    assert parse_failures(metadata) == ()


def test_the_predicate_has_teeth():
    """
    The guard the DEFERRED table did not have. An allowlist that matched
    everything, or a classifier that matched nothing, would let every test above
    pass for the wrong reason, so assert the two directions are actually
    distinguishable rather than trusting that they are.
    """
    assert exif_io._BENIGN_WARNINGS, "the benign allowlist is empty"
    matched = [
        w for w in PARSE_FAILURE_WARNINGS
        if any(p.search(w) for p in exif_io._BENIGN_WARNINGS)
    ]
    assert matched == [], f"the allowlist swallows real parse failures: {matched}"
    unmatched = [
        w for w in BENIGN_WARNINGS
        if not any(p.search(w) for p in exif_io._BENIGN_WARNINGS)
    ]
    assert unmatched == [], f"a measured benign warning is not allowed: {unmatched}"


# THE TYPE. Three states, not two.


def test_the_three_read_outcomes_are_distinguishable():
    parsed = exif_io.MetadataRead(metadata={"File:FileType": "SVG"})
    unparsed = exif_io.MetadataRead(
        metadata={}, outcome=ReadOutcome.UNPARSED,
        error="JPEG format error", parse_failures=("JPEG format error",),
    )
    failed = exif_io.MetadataRead(metadata={}, outcome=ReadOutcome.FAILED, error="boom")

    assert (parsed.parsed, parsed.unparsed, parsed.failed, parsed.ok) == (
        True, False, False, True)
    assert (unparsed.parsed, unparsed.unparsed, unparsed.failed, unparsed.ok) == (
        False, True, False, True)
    assert (failed.parsed, failed.unparsed, failed.failed, failed.ok) == (
        False, False, True, False)

    # `if not read:` must not treat any of the two bad states as an empty file.
    assert bool(parsed) is True
    assert bool(unparsed) is False
    assert bool(failed) is False


# POPULATION A: parseable and empty. The regression risk of the fix.


@pytest.mark.parametrize("kind", ALL_KINDS)
def test_a_good_file_is_parsed_before_and_after_scrubbing(kind, tmp_path):
    """
    The broadest guard on the predicate. If it ever classifies a healthy file as
    unparsed, that file stops being cleanable, and this says so per format
    rather than waiting for a user to hit it.
    """
    path, _ = build(kind, tmp_path)
    assert _read(path).outcome is ReadOutcome.PARSED
    _scrub(path)
    assert _read(path).outcome is ReadOutcome.PARSED


@pytest.mark.parametrize("kind", ALL_KINDS)
def test_a_file_with_nothing_left_to_remove_is_clean_and_untouched(kind, tmp_path):
    """
    Scrub twice. The second pass sees a file that genuinely carries nothing.

    The "not rewritten" claim is scoped to the CLEAN branch on purpose, because
    that is the branch the fix touches. A format whose engine rebuilds its
    container legitimately produces different bytes every run: measured, .docx,
    .xlsx and .pptx still report their ZIP structural tags after scrubbing, so
    they take the SANITIZED path and the zip is rebuilt. Asserting byte equality
    for them would be asserting something that was never true, and a test that
    has to be weakened later is worse than one scoped correctly now.
    """
    path, _ = build(kind, tmp_path)
    _scrub(path)
    with open(path, "rb") as fh:
        after_first = fh.read()

    result = _scrub(path)

    assert result["status"] != STATUS_ERROR, result.get("error")
    if not _real_tags(_read(path).metadata):
        assert result["status"] == STATUS_CLEAN, result
    if result["status"] == STATUS_CLEAN:
        with open(path, "rb") as fh:
            assert fh.read() == after_first, (
                "a file reported CLEAN was rewritten anyway"
            )


@pytest.mark.parametrize("kind", BENIGN_WARNING_KINDS)
def test_the_collision_case_stays_clean(kind, tmp_path):
    """
    THE case that decides whether the predicate is honest rather than merely
    strict. A scrubbed ODF template has zero document tags AND an ExifTool
    warning, which is the exact shape of the defect. It is a good file. The
    naive fix reports it as an error and refuses to touch a document it could
    have cleaned.
    """
    path, _ = build(kind, tmp_path)
    _scrub(path)
    read = _read(path)

    # Both halves of the collision, asserted rather than assumed. If exiftool
    # ever stops warning here, this test has stopped testing anything and should
    # say so instead of passing.
    assert _real_tags(read.metadata) == [], "no longer the zero-tag case"
    warnings = [
        v for k, v in read.metadata.items()
        if k.startswith("ExifTool:") and "Warning" in k
    ]
    assert warnings, f"{kind} no longer carries the benign warning"

    assert read.outcome is ReadOutcome.PARSED
    result = _scrub(path)
    assert result["status"] == STATUS_CLEAN, result
    assert result["detail"] == "no metadata carriers found"


def test_the_benign_allowlist_is_backed_by_a_real_parsed_read(tmp_path):
    """
    The rule every entry in the allowlist has to meet, enforced instead of
    written down. A warning is benign because exiftool raises it on a read that
    ALSO returns the document's own tags, not because of how it is worded. This
    finds a real file that proves it for the family that is allowed today.
    """
    path, value = build(".ott", tmp_path)
    read = _read(path)

    warnings = [
        v for k, v in read.metadata.items()
        if k.startswith("ExifTool:") and "Warning" in k
    ]
    assert any(
        any(p.search(str(w)) for p in exif_io._BENIGN_WARNINGS) for w in warnings
    ), f"no allowlisted warning on this read: {warnings}"
    assert _real_tags(read.metadata), (
        "the justification for allowing this warning is that exiftool reports "
        "document tags alongside it; it did not"
    )
    assert read.outcome is ReadOutcome.PARSED


# POPULATION B: unparseable. The defect itself.


@pytest.mark.parametrize("fraction", [0.4, 0.85], ids=["trunc40", "trunc85"])
@pytest.mark.parametrize("kind", ALL_KINDS)
def test_a_damaged_file_is_never_reported_clean_while_carrying_its_sentinel(
    kind, fraction, tmp_path
):
    """
    The invariant, over every shipped format and two truncation points.

    Not every truncation produces an unparseable file, and that is the point of
    running the whole cross-product rather than the one SVG the defect was found
    on: a truncated .mkv or .mp3 is still a valid stream and gets genuinely
    sanitized, while a truncated .zip container is not and must be refused. What
    may never happen, for any of them, is CLEAN over a surviving sentinel.
    """
    path, value = build(kind, tmp_path)
    damaged = _truncate(path, str(tmp_path / ("damaged" + kind)), fraction)
    with open(damaged, "rb") as fh:
        before_bytes = fh.read()

    result = _scrub(damaged)

    with open(damaged, "rb") as fh:
        after_bytes = fh.read()

    if result["status"] == STATUS_CLEAN:
        assert not raw_contains(damaged, value), (
            "reported CLEAN while the sentinel is still in the output bytes"
        )
    if "could not parse" in (result.get("error") or ""):
        # A refusal must be a refusal. No engine ran, so no byte moved.
        #
        # Only THIS error means refusal. A STATUS_ERROR can also mean an engine
        # ran and its output failed verification, which legitimately leaves
        # changed bytes: measured on a .pdf truncated to 85 percent, where
        # exiftool cannot parse the file but still reports PDF:PDFVersion and
        # PDF:PageCount, so there are real tags, no short circuit, and pikepdf
        # rewrites it. That case is recorded as open work rather than silently
        # covered by a loose assertion here.
        assert after_bytes == before_bytes, "a refused file was modified anyway"


@pytest.mark.parametrize("kind", ALL_KINDS)
def test_an_unparseable_baseline_is_refused_before_any_engine_runs(
    kind, tmp_path, monkeypatch
):
    """
    The refusal, over every format, with the read forced rather than provoked.

    Truncation cannot reach this branch for most formats: measured over the
    whole corpus, a truncated .png or .docx still reports enough structural tags
    that the CLEAN short circuit is never taken, and .svg was the only format
    where truncation produced BOTH an unparseable read and zero tags. That is a
    fact about how those containers degrade, not about the orchestrator, and
    testing only .svg would leave six of the seven engines unexercised on the
    one path that must never rewrite a file.

    So the read is forced to the state that matters and the orchestrator is
    asked what it does. Each format dispatches to its own engine, and the claim
    under test is that NONE of them is reached: an unparseable baseline gives
    verify.py no needles, so running an engine would produce a rewrite nothing
    could check.
    """
    path, value = build(kind, tmp_path)
    with open(path, "rb") as fh:
        before_bytes = fh.read()

    complaint = "Format error reading %s file" % kind.lstrip(".").upper()

    def unparsed_read(self, target):
        return exif_io.MetadataRead(
            metadata={"File:FileType": kind.lstrip(".").upper(),
                      "ExifTool:Warning": complaint},
            outcome=ReadOutcome.UNPARSED,
            error=complaint,
            parse_failures=(complaint,),
        )

    monkeypatch.setattr(exif_io.ExifSession, "read", unparsed_read)
    result = _scrub(path, backup=True)

    assert result["status"] == STATUS_ERROR, result
    assert "could not parse" in result["error"]
    assert complaint in result["error"]
    assert result["removed_fields"] == []
    # No engine ran, so there is no verification to report and nothing was
    # written. Both of those are the point: a refusal that edits the file is not
    # a refusal.
    assert "verification" not in result
    assert not os.path.exists(path + ".backup")
    with open(path, "rb") as fh:
        assert fh.read() == before_bytes
    # And the file still carries what it carried. The refusal is honest only if
    # the user's metadata is where they left it rather than half removed.
    assert contains_anywhere(path, value)


def test_the_truncated_svg_that_started_this_is_refused(tmp_path):
    """
    The original reproduction, kept as itself.

    This is the file the pinned test in test_svg.py described. It is here as
    well because that test lives with the SVG engine, and the defect was never
    an SVG defect: it was the orchestrator's, and this file is where the
    orchestrator's behaviour is held.
    """


    value = sentinel("unparseablesvg")
    path = str(tmp_path / "truncated.svg")
    with open(path, "wb") as fh:
        fh.write(
            ('<?xml version="1.0" encoding="UTF-8"?>\n'
             '<svg xmlns="http://www.w3.org/2000/svg" width="10" height="10"\n'
             '     sodipodi:docname="' + value + '.svg"><rect x="1" y="1"'
             ).encode("utf-8")
        )
    with open(path, "rb") as fh:
        original = fh.read()

    read = _read(path)
    assert read.ok, "the read still succeeds; that is what made this dangerous"
    assert read.outcome is ReadOutcome.UNPARSED
    assert read.parse_failures

    result = _scrub(path)

    assert result["status"] == STATUS_ERROR, result
    assert "could not parse" in result["error"]
    assert raw_contains(path, value), "the sentinel was the whole point"
    with open(path, "rb") as fh:
        assert fh.read() == original, "a file we refused to clean was modified"


def test_a_refused_file_makes_the_run_exit_non_zero(tmp_path):
    """
    A wrong verdict a script cannot see is not much better than no verdict. The
    report is what the CLI's exit status is computed from.
    """


    value = sentinel("exitcodesvg")
    path = str(tmp_path / "truncated.svg")
    with open(path, "wb") as fh:
        fh.write(
            ('<?xml version="1.0" encoding="UTF-8"?>\n'
             '<svg xmlns="http://www.w3.org/2000/svg" width="10" height="10"\n'
             '     sodipodi:docname="' + value + '.svg"><rect x="1" y="1"'
             ).encode("utf-8")
        )
    with MetadataScrubber(backup=False) as scrubber:
        results = [scrubber.sanitize_file(path, remove_all=True)]
        report = scrubber.generate_sanitization_report(results)

    assert report["error_files"] == 1
    assert report["clean_files"] == 0
    assert report["sanitized_files"] == 0
    # This expression is the CLI's exit status.
    assert bool(report["error_files"] or report["files_with_residual_metadata"])
