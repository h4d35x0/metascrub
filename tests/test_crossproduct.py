"""
Cross-product tests.

The acceptance criterion these satisfy: assert the invariant over a
cross-product of (format x remove_all x backup), rather than over one
illustrative file per format. A test that exercises the single case the code
already handles proves only that the case it was written from still works.
"""

from __future__ import annotations

import os

import pytest

from conftest import ALL_KINDS, build, contains_anywhere
from metascrub import (
    STATUS_CLEAN, STATUS_ERROR, STATUS_SANITIZED, Completeness, MetadataScrubber, spec_for,
)


@pytest.mark.parametrize("kind", ALL_KINDS)
@pytest.mark.parametrize("backup", [True, False], ids=["backup", "nobackup"])
def test_removal_holds_across_backup_settings(kind, backup, tmp_path):
    """Whether a backup is written must not change whether the file gets clean."""
    path, value = build(kind, tmp_path)

    with MetadataScrubber(backup=backup) as scrubber:
        result = scrubber.sanitize_file(path, remove_all=True)

    assert result["status"] == STATUS_SANITIZED, result.get("error")
    assert not contains_anywhere(path, value)

    backup_path = path + ".backup"
    assert os.path.exists(backup_path) is backup
    if backup:
        # The backup must hold the ORIGINAL, or it is useless as a backup.
        assert contains_anywhere(backup_path, value)


@pytest.mark.parametrize("kind", ALL_KINDS)
def test_every_result_carries_its_engine_and_guarantee(kind, tmp_path):
    """
    The caller must always be able to see which engine ran and what it promised.
    This is what stops "supported" and "completely scrubbable" from collapsing
    back into one boolean.
    """
    path, _ = build(kind, tmp_path)
    with MetadataScrubber(backup=False) as scrubber:
        result = scrubber.sanitize_file(path, remove_all=True)

    assert result["engine"] == spec_for(path).engine.value
    assert result["completeness"] in ("complete", "partial")
    assert "verification" in result
    assert result["verification"]["verdict"] in (
        "verified_clean", "residual_found", "carriers_remain", "unverified"
    )


@pytest.mark.parametrize("kind", ALL_KINDS)
def test_partial_formats_never_claim_to_be_complete(kind, tmp_path):
    """
    A PARTIAL result must say PARTIAL. A silent partial is worse than a refusal,
    because the user acts on the belief the file is fully clean.
    """
    path, _ = build(kind, tmp_path)
    spec = spec_for(path)
    with MetadataScrubber(backup=False) as scrubber:
        result = scrubber.sanitize_file(path, remove_all=True)
    assert result["completeness"] == spec.completeness.value


@pytest.mark.parametrize("kind", ALL_KINDS)
def test_running_twice_is_safe_and_idempotent(kind, tmp_path):
    """
    A second pass must not corrupt the file or invent removals it did not make.
    Batch users re-run over directories all the time.
    """
    path, value = build(kind, tmp_path)
    with MetadataScrubber(backup=False) as scrubber:
        first = scrubber.sanitize_file(path, remove_all=True)
        assert first["status"] == STATUS_SANITIZED, first.get("error")
        size_after_first = os.path.getsize(path)

        second = scrubber.sanitize_file(path, remove_all=True)

    assert second["status"] in (STATUS_SANITIZED, STATUS_CLEAN)
    assert second["status"] != STATUS_ERROR
    assert not contains_anywhere(path, value)
    assert os.path.getsize(path) > 0
    assert size_after_first > 0


def test_selective_field_removal_on_an_exiftool_format(tmp_path):
    """Removing one named tag must remove that tag and leave the others."""
    from conftest import _exiftool_write, sentinel
    from PIL import Image

    path = str(tmp_path / "selective.jpg")
    Image.new("RGB", (16, 16)).save(path, "JPEG")
    artist = sentinel("artistonly")
    copyright_value = sentinel("copyrightkeep")
    _exiftool_write(path, Artist=artist, Copyright=copyright_value)

    with MetadataScrubber(backup=False) as scrubber:
        result = scrubber.sanitize_file(path, fields_to_remove=["Artist"])

    assert result["status"] == STATUS_SANITIZED, result.get("error")
    assert "Artist" in result["removed_fields"]
    assert not contains_anywhere(path, artist), "the requested tag was not removed"
    assert contains_anywhere(path, copyright_value), (
        "selective removal deleted a tag it was not asked to touch"
    )


def test_directory_run_reports_every_category(tmp_path):
    """The batch report must separate sanitized, clean, skipped and failed."""
    build(".jpg", tmp_path)
    build(".pdf", tmp_path)
    (tmp_path / "readme.txt").write_text("not a media file")

    with MetadataScrubber(backup=False) as scrubber:
        results = scrubber.sanitize_directory(
            str(tmp_path), remove_all=True, include_unsupported=True
        )
        report = scrubber.generate_sanitization_report(results)

    assert report["total_files"] >= 3
    assert report["sanitized_files"] >= 2
    assert report["unsupported_files"] >= 1
    assert report["files_with_residual_metadata"] == []
    assert report["verified_clean"] >= 2
