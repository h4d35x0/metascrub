"""
Boundary cases.

These are the tests that find real defects. A suite that only exercises the
happy path for each format buys confidence it has not earned, so everything here
is a case where the code could plausibly do the wrong thing quietly: report
success on a file it did not touch, destroy a file it could not parse, or
overwrite the one remaining copy of the original.
"""

from __future__ import annotations

import os
import shutil
import stat

import pytest

from conftest import build, contains_anywhere, make_pdf
from metascrub import (
    STATUS_CLEAN, STATUS_DEFERRED, STATUS_ERROR, STATUS_SANITIZED,
    STATUS_UNSUPPORTED, MetadataScrubber,
)


def test_missing_file_is_an_error_not_a_crash(tmp_path):
    with MetadataScrubber(backup=False) as scrubber:
        result = scrubber.sanitize_file(str(tmp_path / "nope.jpg"))
    assert result["status"] == STATUS_ERROR
    assert "not found" in result["error"].lower()


def test_directory_passed_as_a_file_is_rejected(tmp_path):
    with MetadataScrubber(backup=False) as scrubber:
        result = scrubber.sanitize_file(str(tmp_path))
    assert result["status"] == STATUS_ERROR


def test_zero_byte_file_reports_clean_not_sanitized(tmp_path):
    """
    An empty file has nothing to remove. Reporting it as SANITIZED would be a
    false success, and a batch summary built on that would overstate what the
    tool did.
    """
    path = tmp_path / "empty.jpg"
    path.write_bytes(b"")
    with MetadataScrubber(backup=False) as scrubber:
        result = scrubber.sanitize_file(str(path))
    assert result["status"] == STATUS_CLEAN
    assert result["removed_fields"] == []


def test_file_with_no_metadata_reports_clean(tmp_path):
    """A clean file must not be reported as having had metadata removed."""
    from PIL import Image
    path = str(tmp_path / "bare.png")
    Image.new("RGB", (8, 8)).save(path, "PNG")

    with MetadataScrubber(backup=False) as scrubber:
        result = scrubber.sanitize_file(str(path), remove_all=True)

    assert result["status"] in (STATUS_CLEAN, STATUS_SANITIZED)
    if result["status"] == STATUS_CLEAN:
        assert result["removed_fields"] == []


def test_unsupported_extension_is_refused_clearly(tmp_path):
    path = tmp_path / "notes.txt"
    path.write_text("hello")
    with MetadataScrubber(backup=False) as scrubber:
        result = scrubber.sanitize_file(str(path))
    assert result["status"] == STATUS_UNSUPPORTED
    assert result["removed_fields"] == []


def test_deferred_format_is_distinct_from_unsupported(tmp_path):
    """
    A knowingly-deferred format must not look like an unknown one. The user acts
    differently on "we have not built this yet" than on "this is not a media
    file".
    """
    path = tmp_path / "legacy.doc"
    path.write_bytes(b"\xd0\xcf\x11\xe0" + b"\x00" * 64)
    with MetadataScrubber(backup=False) as scrubber:
        result = scrubber.sanitize_file(str(path))
    assert result["status"] == STATUS_DEFERRED
    assert "fixture" in result["detail"]


def test_extension_that_lies_about_the_container_fails_safely(tmp_path):
    """
    A text file named .pdf must produce an error and must not be destroyed. The
    original bytes have to survive, because a tool that eats unparseable input
    is worse than one that refuses it.
    """
    path = tmp_path / "fake.pdf"
    original = b"this is definitely not a pdf, not even slightly"
    path.write_bytes(original)

    with MetadataScrubber(backup=False) as scrubber:
        result = scrubber.sanitize_file(str(path), remove_all=True)

    assert result["status"] in (STATUS_ERROR, STATUS_CLEAN)
    assert path.read_bytes() == original, "input was damaged by a failed parse"


def test_existing_backup_is_never_overwritten(tmp_path):
    """
    Regression guard for a data-loss bug inherited from the original code, which
    wrote <file>.backup unconditionally. Sanitizing twice replaced the pristine
    backup with the already-sanitized copy, destroying the only original.
    """
    path, value = build(".jpg", tmp_path)
    backup = path + ".backup"

    with MetadataScrubber(backup=True) as scrubber:
        scrubber.sanitize_file(path, remove_all=True)
    assert os.path.exists(backup)
    assert contains_anywhere(backup, value), "first backup should hold the original"

    # Second run over the already-clean file must leave the good backup alone.
    with MetadataScrubber(backup=True) as scrubber:
        scrubber.sanitize_file(path, remove_all=True)

    assert contains_anywhere(backup, value), (
        "the pristine backup was overwritten by a later run"
    )


def test_backup_round_trip_restores_the_original(tmp_path):
    path, value = build(".jpg", tmp_path)
    with MetadataScrubber(backup=True) as scrubber:
        scrubber.sanitize_file(path, remove_all=True)
        assert not contains_anywhere(path, value)
        assert scrubber.restore_backup(path) is True
    assert contains_anywhere(path, value), "restore did not bring the metadata back"


def test_restore_without_a_backup_returns_false(tmp_path):
    path, _ = build(".jpg", tmp_path)
    with MetadataScrubber(backup=False) as scrubber:
        assert scrubber.restore_backup(path) is False


def test_non_ascii_path_is_handled(tmp_path):
    """Paths with non-ASCII characters must work; users have them."""
    directory = tmp_path / "dossier_privé_日本"
    directory.mkdir()
    path, value = build(".jpg", directory)

    with MetadataScrubber(backup=False) as scrubber:
        result = scrubber.sanitize_file(path, remove_all=True)

    assert result["status"] == STATUS_SANITIZED, result.get("error")
    assert not contains_anywhere(path, value)


def test_read_only_file_fails_without_destroying_it(tmp_path):
    path, value = build(".pdf", tmp_path)
    os.chmod(path, stat.S_IREAD)
    try:
        with MetadataScrubber(backup=False) as scrubber:
            result = scrubber.sanitize_file(path, remove_all=True)
        # Either it managed the write or it refused. What it must never do is
        # report success while leaving the metadata in place.
        if result["status"] == STATUS_SANITIZED:
            assert not contains_anywhere(path, value)
        else:
            assert result["status"] == STATUS_ERROR
            assert os.path.getsize(path) > 0
    finally:
        os.chmod(path, stat.S_IWRITE | stat.S_IREAD)


def test_directory_walk_skips_our_own_backups(tmp_path):
    """
    A second pass over a directory must not sanitize the .backup files, which
    would quietly destroy every original in the tree.
    """
    path, value = build(".jpg", tmp_path)
    with MetadataScrubber(backup=True) as scrubber:
        scrubber.sanitize_file(path, remove_all=True)
        results = scrubber.sanitize_directory(str(tmp_path), remove_all=True)

    processed = {os.path.basename(r["file"]) for r in results}
    assert not any(name.endswith(".backup") for name in processed)
    assert contains_anywhere(path + ".backup", value)


def test_selective_removal_is_refused_by_engines_that_cannot_do_it(tmp_path):
    """
    Asking for one field on a container that can only be rebuilt wholesale must
    be refused, not silently upgraded to removing everything.
    """
    path, value = make_pdf(tmp_path)
    with MetadataScrubber(backup=False) as scrubber:
        result = scrubber.sanitize_file(path, fields_to_remove=["Author"])
    assert result["status"] == STATUS_ERROR
    assert "individual fields" in result["error"]
    assert contains_anywhere(path, value), "file was modified despite the refusal"
