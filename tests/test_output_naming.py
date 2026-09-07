"""
Identifiers that are not inside the file: the filename and the filesystem
timestamps.

Every other test in this suite proves something about BYTES, by capturing a
value before the scrub and searching the output for it afterwards. Nothing
here can work that way, and that is the entire point of this file. The leak
under test lives in the directory entry, not in the file, so the residual scan
in verify.py has no needle to look for and will report a perfectly clean
verdict on PXL_20260906_143022891.jpg while the capture time sits in plain
sight in the name.

From docs/ANDROID-MEDIA-BUILD.md 1.2: "This is not in the file, so no byte
scan of the file will ever catch it, and it is the cheapest fix in this
document."

So these tests assert against the filesystem: what the directory holds, what
os.stat returns, and what the result dictionary says happened. Three things
they are shaped to catch, in order of how expensive the mistake would be:

1. A rename that overwrites something. Every other bug here is recoverable.
2. A detector that cries wolf. A false "your filename leaks" on report.pdf is
   noise, noise trains people to ignore the warning, and an ignored warning is
   worse than no warning because it costs attention and buys nothing. The
   overcorrection test below is not a nicety; it is the test that decides
   whether the detector is worth shipping.
3. A privacy action reported as done that did not happen.
"""

from __future__ import annotations

import os
import sys

import pytest

from conftest import build, contains_anywhere
from metascrub import STATUS_CLEAN, STATUS_SANITIZED, MetadataScrubber
from metascrub.scrubber import (
    LEAK_APP, LEAK_TIMESTAMP, NEUTRAL_TIMESTAMP, detect_name_leaks,
)

# The two shapes named in the design document, quoted rather than paraphrased.
PXL_NAME = "PXL_20260906_143022891.jpg"
SCREENSHOT_NAME = "Screenshot_2026-09-06-14-30-22_instagram.png"


def _relocate(path: str, new_name: str) -> str:
    """Give a built fixture the filename under test, keeping its bytes."""
    target = os.path.join(os.path.dirname(path), new_name)
    os.rename(path, target)
    return target


# DETECTION: THE TWO REAL SHAPES


def test_pixel_camera_name_is_detected():
    """PXL_20260906_143022891.jpg: capture time to the millisecond."""
    leaks = detect_name_leaks("/photos/" + PXL_NAME)
    assert LEAK_TIMESTAMP in [leak.kind for leak in leaks]
    stamp = next(leak for leak in leaks if leak.kind == LEAK_TIMESTAMP)
    assert "20260906" in stamp.evidence and "143022891" in stamp.evidence
    # The device family is reported too, but only as detail on a finding that
    # already fired. It is never a trigger of its own: see _DEVICE_PREFIXES.
    assert "Pixel" in stamp.detail


def test_android_screenshot_name_is_detected():
    """
    Screenshot_2026-09-06-14-30-22_instagram.png leaks TWO things, and they are
    reported as two findings rather than one. The rename fixes both at once,
    which is exactly why they would be easy to conflate, and a caller filtering
    for "which of my files name an app" must not silently receive every file
    that merely carries a timestamp.
    """
    leaks = detect_name_leaks("/pictures/" + SCREENSHOT_NAME)
    kinds = [leak.kind for leak in leaks]
    assert LEAK_TIMESTAMP in kinds
    assert LEAK_APP in kinds
    app = next(leak for leak in leaks if leak.kind == LEAK_APP)
    assert app.evidence == "instagram"


# DETECTION: THE OVERCORRECTION TEST
#
# CLAUDE.md trap 11 states the rule this follows: an exclusion and the test
# that proves it did not overcorrect land in the SAME commit. Here the whole
# detector is the exclusion, so the negative cases are the load-bearing half.

@pytest.mark.parametrize("name", [
    # The three named in the brief.
    "photo.jpg",
    "report.pdf",
    "IMG_1234.JPG",
    # A date the user chose, with no time of day. This is a filing convention,
    # not a capture stamp, and it is extremely common.
    "meeting-notes-2026-09-06.md",
    "20260906.txt",
    "2026-09-06 board pack.pdf",
    # Long digit runs that contain something date-shaped. Without the
    # digit-boundary lookarounds these all match.
    "invoice_1234567890.pdf",
    "invoice-12345678.pdf",
    "order 20260906143022891.csv",
    # Not a calendar date: month 56, day 78.
    "batch-12345678-final.xlsx",
    # A version number that looks like a time but has no date beside it.
    "release-v1.12.34.56.zip",
    # HH.MM with no seconds, next to a real date. Machines write seconds.
    "budget-2026-09-06-10.30.xlsx",
    # App names in ordinary documents. The app kind never fires without a
    # timestamp, which is what keeps these quiet.
    "netflix-cancellation-letter.pdf",
    "instagram marketing plan.docx",
    # Words that are also app names and are deliberately not in the token list.
    "signal-processing.pdf",
    "teams-offsite.pptx",
    # Already neutral output.
    "file0001.jpg",
    # No stem at all.
    ".gitignore",
])
def test_ordinary_names_are_not_flagged(name):
    assert detect_name_leaks("/documents/" + name) == [], (
        f"{name} was reported as leaking; a false positive here is the failure "
        "that makes users stop reading the warnings"
    )


def test_detection_reads_only_the_basename(tmp_path):
    """
    A leaky DIRECTORY name is not a leaky filename. Reporting the parent
    directory would fire on every file in a folder called 2026-09-06_14-30-22,
    which is one finding per file for one fact the user already knows.
    """
    directory = tmp_path / "2026-09-06_14-30-22"
    directory.mkdir()
    assert detect_name_leaks(str(directory / "photo.jpg")) == []


# REPORTING THE LEAK WITHOUT FIXING IT


def test_leak_is_reported_on_a_plain_scrub(tmp_path):
    """
    A user who does not know the filename leaks cannot choose to fix it, so
    the finding is attached with no flag passed and nothing renamed.
    """
    path, _ = build(".jpg", tmp_path)
    leaky = _relocate(path, PXL_NAME)

    with MetadataScrubber(backup=False) as scrubber:
        result = scrubber.sanitize_file(leaky, remove_all=True)

    assert result["status"] == STATUS_SANITIZED, result.get("error")
    assert [leak["kind"] for leak in result["name_leaks"]] == [LEAK_TIMESTAMP]
    assert "renamed_to" not in result


def test_ordinary_name_adds_no_key_at_all(tmp_path):
    """
    The key is absent, not empty. An always-present "name_leaks": [] would
    change the JSON report for every user who never asked for any of this.
    """
    path, _ = build(".jpg", tmp_path)
    with MetadataScrubber(backup=False) as scrubber:
        result = scrubber.sanitize_file(path, remove_all=True)
    assert "name_leaks" not in result


def test_leak_is_reported_on_a_file_the_tool_cannot_clean(tmp_path):
    """
    An unsupported file is the case where the filename is the ONLY thing the
    tool can tell the user anything about.
    """
    target = tmp_path / "PXL_20260906_143022891.unknownext"
    target.write_bytes(b"not a format metascrub handles")
    with MetadataScrubber(backup=False) as scrubber:
        result = scrubber.sanitize_file(str(target), remove_all=True)
    assert result["status"] != STATUS_SANITIZED
    assert [leak["kind"] for leak in result["name_leaks"]] == [LEAK_TIMESTAMP]


# RENAMING IS OPT IN


def test_renaming_is_off_by_default(tmp_path):
    """
    Silently renaming files a user already had is a surprise, and surprise in a
    privacy tool is how people lose data. The design document says "Do it by
    default"; that sentence is about the Android build, which writes a NEW file
    and never touches the original. This tool edits in place.
    """
    path, value = build(".jpg", tmp_path)
    leaky = _relocate(path, PXL_NAME)

    with MetadataScrubber(backup=False) as scrubber:
        result = scrubber.sanitize_file(leaky, remove_all=True)

    assert result["status"] == STATUS_SANITIZED, result.get("error")
    assert os.path.exists(leaky), "the file was renamed without being asked"
    assert os.listdir(tmp_path) == [PXL_NAME]
    assert not contains_anywhere(leaky, value)


def test_opting_in_renames_and_keeps_the_extension(tmp_path):
    path, value = build(".jpg", tmp_path)
    leaky = _relocate(path, PXL_NAME)

    with MetadataScrubber(backup=False, neutral_names=True) as scrubber:
        result = scrubber.sanitize_file(leaky, remove_all=True)

    assert result["status"] == STATUS_SANITIZED, result.get("error")
    new_path = result["renamed_to"]
    assert os.path.basename(new_path) == "file0001.jpg"
    assert os.path.exists(new_path)
    assert not os.path.exists(leaky)
    assert detect_name_leaks(new_path) == []
    assert not contains_anywhere(new_path, value)
    # The identity the caller passed in is preserved. A caller that reads
    # result["file"] must get the path it asked about, not one whose meaning
    # silently changed under it.
    assert result["file"] == leaky


def test_uppercase_extension_is_lowercased(tmp_path):
    path, _ = build(".jpg", tmp_path)
    leaky = _relocate(path, "PXL_20260906_143022891.JPG")
    with MetadataScrubber(backup=False, neutral_names=True) as scrubber:
        result = scrubber.sanitize_file(leaky, remove_all=True)
    assert os.path.basename(result["renamed_to"]) == "file0001.jpg"


def test_an_already_neutral_name_is_left_alone(tmp_path):
    """
    Rerunning must be idempotent. Renumbering file0001.jpg to file0002.jpg on
    every pass churns the directory for no privacy gain at all.
    """
    path, _ = build(".jpg", tmp_path)
    neutral = _relocate(path, "file0001.jpg")
    with MetadataScrubber(backup=False, neutral_names=True) as scrubber:
        result = scrubber.sanitize_file(neutral, remove_all=True)
    assert "renamed_to" not in result
    assert os.path.exists(neutral)


def test_a_clean_file_is_renamed_too(tmp_path):
    """
    A file carrying no metadata can still be named after the second it was
    captured. Skipping CLEAN would mean the feature silently did nothing for
    exactly the files that needed only the rename.
    """
    path, _ = build(".jpg", tmp_path)
    with MetadataScrubber(backup=False) as scrubber:
        scrubber.sanitize_file(path, remove_all=True)
    leaky = _relocate(path, PXL_NAME)

    with MetadataScrubber(backup=False, neutral_names=True) as scrubber:
        result = scrubber.sanitize_file(leaky, remove_all=True)

    assert result["status"] == STATUS_CLEAN, result
    assert os.path.basename(result["renamed_to"]) == "file0001.jpg"


def test_a_file_we_refused_to_touch_is_not_renamed(tmp_path):
    """
    A neutral name is a claim that the file was handled. An unsupported file
    was not handled, and renaming it would both hide it from the user and
    assert something untrue about it.
    """
    target = tmp_path / (PXL_NAME + ".unknownext")
    target.write_bytes(b"not a format metascrub handles")
    with MetadataScrubber(backup=False, neutral_names=True) as scrubber:
        result = scrubber.sanitize_file(str(target), remove_all=True)
    assert "renamed_to" not in result
    assert target.exists()


# COLLISIONS: NEVER OVERWRITE ANYTHING, EVER


def test_renaming_never_overwrites_an_existing_file(tmp_path):
    """
    The one unrecoverable failure in this whole feature.

    Note what is NOT relied on: os.rename() and os.replace() both clobber an
    existing destination silently on Linux and macOS, and only Windows raises,
    so a test that passed here on Windows would prove nothing about the
    platforms where it matters most. The guard is an O_CREAT|O_EXCL claim on
    the name, which behaves identically everywhere.
    """
    occupant = tmp_path / "file0001.jpg"
    occupant.write_bytes(b"SOMEONE ELSES FILE, MUST SURVIVE")

    path, _ = build(".jpg", tmp_path)
    leaky = _relocate(path, PXL_NAME)

    with MetadataScrubber(backup=False, neutral_names=True) as scrubber:
        result = scrubber.sanitize_file(leaky, remove_all=True)

    assert occupant.read_bytes() == b"SOMEONE ELSES FILE, MUST SURVIVE"
    assert os.path.basename(result["renamed_to"]) == "file0002.jpg"


def test_a_run_of_files_never_collides_with_itself(tmp_path):
    """
    Several files in one directory, allocated one after another. The counter
    cache is only a starting hint; the filesystem is what decides a name is
    free, so this also covers a stale hint.
    """
    names = [
        "PXL_20260906_143022891.jpg",
        "PXL_20260906_143023001.jpg",
        "PXL_20260906_143024117.jpg",
    ]
    values = []
    for name in names:
        path, value = build(".jpg", tmp_path)
        _relocate(path, name)
        values.append(value)

    with MetadataScrubber(backup=False, neutral_names=True) as scrubber:
        results = scrubber.sanitize_directory(str(tmp_path), remove_all=True)

    renamed = sorted(os.path.basename(r["renamed_to"]) for r in results)
    assert renamed == ["file0001.jpg", "file0002.jpg", "file0003.jpg"]
    assert sorted(os.listdir(tmp_path)) == renamed
    for value in values:
        assert not any(
            contains_anywhere(str(tmp_path / name), value) for name in renamed
        )


def test_no_placeholder_is_left_behind_when_the_move_fails(tmp_path, monkeypatch):
    """
    The name is claimed by creating an empty file and then moving onto it. If
    the move fails, that empty file must not survive: a zero-byte file0001.jpg
    sitting in the directory looks exactly like output and is not.
    """
    from metascrub import scrubber as scrubber_module

    path, _ = build(".jpg", tmp_path)
    leaky = _relocate(path, PXL_NAME)

    def exploding_replace(src, dst):
        raise OSError("simulated cross-device failure")

    monkeypatch.setattr(scrubber_module.os, "replace", exploding_replace)

    with MetadataScrubber(backup=False, neutral_names=True) as scrubber:
        result = scrubber.sanitize_file(leaky, remove_all=True)

    # The scrub itself succeeded; only the rename did not. Those are separate
    # states and the result must carry both, not average them into one.
    assert result["status"] == STATUS_SANITIZED, result.get("error")
    assert "renamed_to" not in result
    assert "simulated cross-device failure" in result["rename_error"]
    assert os.path.exists(leaky)
    assert not os.path.exists(tmp_path / "file0001.jpg")


# THE BACKUP RULE STILL HOLDS


def test_renaming_leaves_the_backup_untouched(tmp_path):
    """
    CLAUDE.md: never overwrite an existing <file>.backup, it is the only
    remaining copy of the pre-sanitize original.

    The backup keeps the ORIGINAL filename on purpose. The backup IS the
    original, and the original's name is part of it; a backup renamed to
    file0001.jpg.backup would no longer say which file it came from. The
    consequence is that the leaking name still exists in the directory as long
    as the backup does, which is why the CLI says so out loud.
    """
    path, value = build(".jpg", tmp_path)
    leaky = _relocate(path, PXL_NAME)

    with MetadataScrubber(backup=True, neutral_names=True) as scrubber:
        result = scrubber.sanitize_file(leaky, remove_all=True)

    backup = leaky + ".backup"
    assert result["backup"] == backup
    assert os.path.exists(backup)
    assert contains_anywhere(backup, value), "the backup is not the original"
    assert os.path.basename(result["renamed_to"]) == "file0001.jpg"


def test_an_existing_backup_is_never_clobbered_by_a_neutral_run(tmp_path):
    """
    The inherited data-loss bug, re-checked with renaming on. An already
    neutral file is not renamed, so a second pass writes to the same backup
    path, which is precisely the shape that destroyed originals before.
    """
    path, value = build(".jpg", tmp_path)
    neutral = _relocate(path, "file0001.jpg")
    backup = neutral + ".backup"

    with MetadataScrubber(backup=True, neutral_names=True) as scrubber:
        scrubber.sanitize_file(neutral, remove_all=True)
    assert contains_anywhere(backup, value)

    with MetadataScrubber(backup=True, neutral_names=True) as scrubber:
        scrubber.sanitize_file(neutral, remove_all=True)

    assert contains_anywhere(backup, value), (
        "the pristine backup was overwritten by a second neutral-names run"
    )


def test_a_backup_is_never_chosen_as_a_neutral_name(tmp_path):
    """
    The allocator must not be able to land on something.backup. It cannot,
    because a candidate always ends in the source extension, but the guard
    that actually enforces it is the exclusive claim, so this pins the
    behaviour rather than the reasoning.
    """
    hostage = tmp_path / "file0001.jpg.backup"
    hostage.write_bytes(b"PRISTINE ORIGINAL")
    occupant = tmp_path / "file0001.jpg"
    occupant.write_bytes(b"the file that backup belongs to")

    path, _ = build(".jpg", tmp_path)
    leaky = _relocate(path, PXL_NAME)

    with MetadataScrubber(backup=False, neutral_names=True) as scrubber:
        result = scrubber.sanitize_file(leaky, remove_all=True)

    assert hostage.read_bytes() == b"PRISTINE ORIGINAL"
    assert occupant.read_bytes() == b"the file that backup belongs to"
    assert os.path.basename(result["renamed_to"]) == "file0002.jpg"


# FILESYSTEM TIMESTAMPS
#
# --reset-times already existed. These tests are the audit of it, written
# after measuring what it does rather than after reading what it claims.

OLD_MTIME = 1757000111
OLD_ATIME = 1757000000


def test_reset_times_is_off_by_default(tmp_path):
    path, _ = build(".jpg", tmp_path)
    os.utime(path, (OLD_ATIME, OLD_MTIME))
    with MetadataScrubber(backup=False) as scrubber:
        result = scrubber.sanitize_file(path, remove_all=True)
    assert result["status"] == STATUS_SANITIZED, result.get("error")
    assert "timestamps_reset" not in result
    # The engine rewrites the file, so mtime moves on its own; what must NOT
    # happen is that it silently becomes the epoch value the flag would set.
    assert os.stat(path).st_mtime != NEUTRAL_TIMESTAMP


def test_reset_times_sets_mtime_and_atime_to_the_epoch(tmp_path):
    path, _ = build(".jpg", tmp_path)
    os.utime(path, (OLD_ATIME, OLD_MTIME))

    with MetadataScrubber(backup=False, reset_times=True) as scrubber:
        result = scrubber.sanitize_file(path, remove_all=True)

    assert result["status"] == STATUS_SANITIZED, result.get("error")
    info = os.stat(path)
    assert info.st_mtime == NEUTRAL_TIMESTAMP
    assert info.st_atime == NEUTRAL_TIMESTAMP


def test_reset_times_also_covers_a_clean_file(tmp_path):
    """
    A deliberate change of behaviour for opted-in users, recorded here rather
    than left to be discovered.

    Before this work --reset-times only ran on the SANITIZED path, so a file
    that carried no metadata kept its original mtime. Combined with
    --neutral-names that produced the worst of both: the file was renamed to
    file0001.jpg and its modification time still said the second it was
    captured. Nobody who passes the flag wants the exception.
    """
    path, _ = build(".jpg", tmp_path)
    with MetadataScrubber(backup=False) as scrubber:
        scrubber.sanitize_file(path, remove_all=True)
    os.utime(path, (OLD_ATIME, OLD_MTIME))

    with MetadataScrubber(backup=False, reset_times=True) as scrubber:
        result = scrubber.sanitize_file(path, remove_all=True)

    assert result["status"] == STATUS_CLEAN, result
    assert os.stat(path).st_mtime == NEUTRAL_TIMESTAMP


def test_reset_times_reports_each_timestamp_separately(tmp_path):
    """
    "creation time set to the epoch" and "creation time left alone because
    this platform will not let anything write it" are different states with
    different consequences, and a single boolean cannot carry that. Trap 2.
    """
    path, _ = build(".jpg", tmp_path)
    with MetadataScrubber(backup=False, reset_times=True) as scrubber:
        result = scrubber.sanitize_file(path, remove_all=True)

    record = result["timestamps_reset"]
    assert set(record) == {"mtime", "atime", "creation"}
    assert record["mtime"] == "epoch"
    assert record["atime"] == "epoch"
    if sys.platform == "win32":
        assert record["creation"] == "epoch"
    else:
        assert record["creation"].startswith("not settable")


@pytest.mark.skipif(sys.platform != "win32", reason="NTFS creation time")
def test_reset_times_covers_the_windows_creation_time(tmp_path):
    """
    The gap this audit found and closed.

    Measured 2026-09-06, Windows 11, Python 3.11.3, before the fix: after
    os.utime(path, (0, 0)) the file reported st_mtime 0.0, st_atime 0.0 and
    st_ctime 1788750045.99, unchanged. On Windows st_ctime IS the creation
    time, so --reset-times produced a file whose mtime said 1970 while the
    creation time still named the minute it was captured or downloaded, and
    the unnormalised one is the one that carries the truth.
    """
    path, _ = build(".jpg", tmp_path)
    before = os.stat(path).st_ctime
    assert before != NEUTRAL_TIMESTAMP, "fixture creation time is already epoch"

    with MetadataScrubber(backup=False, reset_times=True) as scrubber:
        scrubber.sanitize_file(path, remove_all=True)

    assert os.stat(path).st_ctime == NEUTRAL_TIMESTAMP


def test_timestamps_survive_the_rename(tmp_path):
    """
    Ordering guard. The rename claims its destination by creating a
    placeholder and replacing it, so the destination name is deleted and
    reused within milliseconds; NTFS file system tunneling restores the
    creation time of a name reused inside about 15 seconds. Stamping the times
    first and renaming afterwards can therefore hand the old creation time
    straight back. Renaming first, stamping the final path afterwards, is
    correct everywhere.
    """
    path, _ = build(".jpg", tmp_path)
    leaky = _relocate(path, PXL_NAME)

    with MetadataScrubber(
        backup=False, reset_times=True, neutral_names=True
    ) as scrubber:
        result = scrubber.sanitize_file(leaky, remove_all=True)

    final = result["renamed_to"]
    info = os.stat(final)
    assert info.st_mtime == NEUTRAL_TIMESTAMP
    assert info.st_atime == NEUTRAL_TIMESTAMP
    if sys.platform == "win32":
        assert info.st_ctime == NEUTRAL_TIMESTAMP


def test_the_backup_keeps_the_original_timestamps(tmp_path):
    """
    Measured, and deliberately NOT changed.

    shutil.copy2 preserves mtime, so with a backup the scrubbed file carries
    the epoch while <file>.backup right beside it still carries the original
    time. That is correct: the backup is the original and normalising it would
    destroy the only remaining copy of the truth. It is also a real residual
    disclosure for anyone who shares a whole directory, and the only fix is
    --no-backup. Asserted here so the behaviour is measured and documented
    rather than assumed.
    """
    path, _ = build(".jpg", tmp_path)
    os.utime(path, (OLD_ATIME, OLD_MTIME))

    with MetadataScrubber(backup=True, reset_times=True) as scrubber:
        result = scrubber.sanitize_file(path, remove_all=True)

    assert os.stat(path).st_mtime == NEUTRAL_TIMESTAMP
    assert os.stat(result["backup"]).st_mtime == OLD_MTIME


# THE REPORT


def test_report_counts_filename_leaks_apart_from_byte_leaks(tmp_path):
    """
    A filename finding was read off a name; a residual finding was measured by
    scanning output bytes. Folding them into one number would let the cheap
    one wear the expensive one's authority.
    """
    leaky_path, _ = build(".jpg", tmp_path)
    _relocate(leaky_path, PXL_NAME)
    build(".png", tmp_path)

    with MetadataScrubber(backup=False, neutral_names=True) as scrubber:
        results = scrubber.sanitize_directory(str(tmp_path), remove_all=True)
        report = scrubber.generate_sanitization_report(results)

    assert len(report["filenames_with_leaks"]) == 1
    assert PXL_NAME in report["filenames_with_leaks"][0]
    # Found is not the same as still leaking. It was renamed, so nothing on
    # disk carries the name any more, and a summary that still said "1 leaking
    # filename" would be telling the user a problem persists that does not.
    assert report["filenames_still_leaking"] == []
    assert report["renamed_files"] == 2
    assert report["rename_failures"] == []
    assert report["files_with_residual_metadata"] == []


def test_report_keeps_a_leak_it_could_not_fix(tmp_path):
    """The other half: no opt in, so the finding stands as still leaking."""
    leaky_path, _ = build(".jpg", tmp_path)
    _relocate(leaky_path, PXL_NAME)

    with MetadataScrubber(backup=False) as scrubber:
        results = scrubber.sanitize_directory(str(tmp_path), remove_all=True)
        report = scrubber.generate_sanitization_report(results)

    assert len(report["filenames_with_leaks"]) == 1
    assert len(report["filenames_still_leaking"]) == 1
    assert report["renamed_files"] == 0
