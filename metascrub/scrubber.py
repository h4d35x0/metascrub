"""
MetadataScrubber: the orchestrator.

The public API is deliberately shaped like the the parent project ExifSanitizer this project
builds on (sanitize_file, sanitize_directory, restore_backup,
generate_sanitization_report, context manager, `backup` flag), so that the parent project can
adopt this engine later without rewriting its callers. The result dictionary
keeps every key the parent project already returns and adds new ones alongside them.

Three behaviours differ from the original, each for a reason recorded here:

1. Dispatch goes through the capability table, not a boolean extension check,
   so the caller learns which engine ran and what it guarantees.

2. Every removal is verified by re-reading the file and by scanning the output
   for the values the file used to carry. Success is measured, never inferred
   from the absence of an exception.

3. An existing backup is never overwritten. The original code wrote
   f"{path}.backup" unconditionally; sanitizing the same file twice therefore
   replaced the pristine backup with the already-sanitized copy and destroyed
   the only remaining original. That is silent, unrecoverable data loss.
"""

from __future__ import annotations

import datetime as _datetime
import logging
import os
import re
import shutil
import sys
from dataclasses import dataclass
from datetime import datetime
from typing import Any, Dict, List, Optional, Sequence, Tuple

from . import exif_io
from .capabilities import (
    CAPABILITIES, Completeness, FormatSpec, deferral_for, is_supported, spec_for,
)
from .engines import EngineError, get_engine
from .verify import Verdict, Verification, verify

logger = logging.getLogger("metascrub")
logger.addHandler(logging.NullHandler())

# Result statuses. These are distinct on purpose: a caller scripting a batch run
# needs to tell "we cleaned it" apart from "there was nothing to clean" and from
# "we refused to touch it", and a single boolean cannot carry that.
STATUS_SANITIZED = "sanitized"      # metadata was present and was removed
STATUS_CLEAN = "clean"              # no metadata carriers found, nothing done
STATUS_UNSUPPORTED = "unsupported"  # format not handled
STATUS_DEFERRED = "deferred"        # format knowingly not handled yet
STATUS_ERROR = "error"

# Verdicts that must NOT downgrade a COMPLETE format to STATUS_ERROR.
#
# The downgrade below exists because a COMPLETE format that did not verify
# clean has broken this tool's central promise. That is a claim about the FILE,
# and only a verdict that is itself a claim about the file may trigger it.
#
# NO_BASELINE_VALUES is not one. It says the baseline read produced no value
# the residual scan could search for, so the scan ran over an empty set. The
# file was genuinely cleaned; nothing was found in it; nothing was measured
# either. Decided 2026-09-07, with both directions priced:
#
#   Making it an error would write "verification failed" into the result of a
#   file whose verification did not fail, would fail the exit code on every
#   GPS-only photo and on `Nokia 6.1.mp4` (measured: real, geotagged,
#   genuinely cleaned, zero needles), and would collapse "we found a problem"
#   into "we have no evidence" - the same trap 2 conflation that was just
#   refused one layer down in the verdict vocabulary. The documented risk of
#   Option D in docs/WHAT-THE-TOOL-CLAIMS.md is exactly this: too loud, then
#   tuned back down one quiet commit at a time.
#
#   Leaving it a success would reintroduce the hole. It does not: `clean` is
#   False for this verdict, so the file leaves the `verified_clean` count, and
#   the CLI and GUI both render it in their own words. Nothing anywhere says
#   "verified clean" about it any more, which was the entire defect.
#
# So: the status stays SANITIZED, which is true (metadata was present and was
# removed), and the verdict carries the limit, which is where the limit
# belongs. tests/test_zero_needle.py asserts this BOTH ways: a zero-needle
# COMPLETE file is not an error, and a COMPLETE file with a real survivor
# still is.
_VERDICTS_THAT_ARE_NOT_A_FINDING = frozenset({
    Verdict.VERIFIED_CLEAN,
    Verdict.NO_BASELINE_VALUES,
})

_PSEUDO = frozenset({"File", "System", "Composite", "ExifTool", "SourceFile"})

BACKUP_SUFFIX = ".backup"


def _real_tags(metadata: Dict[str, Any]) -> List[str]:
    """Tags that are actually carried in the file, excluding filesystem facts."""
    out = []
    for key in metadata:
        group = key.split(":", 1)[0] if ":" in key else key
        if group in _PSEUDO or key == "SourceFile":
            continue
        out.append(key)
    return sorted(out)


# ===========================================================================
# IDENTIFIERS THAT ARE NOT INSIDE THE FILE
#
# Everything above this line is about bytes. This section is about the two
# identifiers that travel with a file without ever being in it: its NAME and
# its filesystem TIMESTAMPS. No byte scan of the output can ever catch either,
# which means verify.py cannot be the thing that proves these are gone, and
# the code here has to be correct by construction instead of by measurement
# after the fact.
#
# From docs/ANDROID-MEDIA-BUILD.md 1.2: "PXL_20260906_143022891.jpg carries
# the capture timestamp to the millisecond and identifies the device family.
# Screenshot_2026-09-06-14-30-22_instagram.png does the same and also names
# the app. This is not in the file, so no byte scan of the file will ever
# catch it, and it is the cheapest fix in this document: write the output
# under a neutral name. Do it by default."
#
# That last sentence is the one place this implementation deliberately
# disagrees with the document it is implementing, and the disagreement is
# about a different platform rather than about the risk. The document's
# Phase 4 says of the Android build: "Write a new scrubbed file rather than
# editing the original in MediaStore". On Android the output is a NEW file, so
# naming it neutrally by default takes nothing away from the user. On the
# desktop the tool edits the user's own file IN PLACE, so the same default
# would silently rename files the user already had, in their own directories,
# under names they chose. Surprise in a privacy tool is how people lose data.
# So on the desktop it is opt in (`neutral_names=False`, `--neutral-names`),
# and the document's default stands for the Android build when that is written.
# ===========================================================================

# The two leak kinds are separate values and not one boolean, for the reason
# trap 2 in CLAUDE.md exists: "the file is named after when it was taken" and
# "the file is named after the app that made it" are different disclosures,
# they are fixed by the same rename but they are not the same finding, and a
# caller filtering on one must not silently get the other.
LEAK_TIMESTAMP = "timestamp"
LEAK_APP = "app"

# Detection is CONSERVATIVE by construction, because a false "your filename
# leaks" on an ordinary name is noise that trains people to ignore the warning,
# and a warning people ignore is worse than no warning: it costs attention and
# buys nothing. Two independent guards do that work:
#
#   1. Nothing is reported without a date that is a REAL CALENDAR DATE in a
#      plausible year range. "invoice-12345678.pdf" parses as 1234-56-78 and is
#      rejected on the month; "IMG_1234.JPG" has four digits and never reaches
#      the calendar at all.
#   2. A date ALONE is never reported. "meeting-notes-2026-09-06.md" and
#      "20260906.txt" are names a person chose on purpose, and telling them
#      their own filing convention is a privacy leak is exactly the noise
#      described above. A time of day has to be there too, and a time of day
#      to the second is a machine's doing, not a person's.
#
# The digit-boundary lookarounds matter more than they look: without them an
# 8-digit run inside a longer number ("invoice_1234567890.pdf") yields a
# spurious date, and the whole detector becomes the thing it is guarding
# against.
_DATE_PATTERNS = (
    # 20260906
    re.compile(r"(?<!\d)(\d{4})(\d{2})(\d{2})(?!\d)"),
    # 2026-09-06, 2026_09_06, 2026.09.06
    re.compile(r"(?<!\d)(\d{4})([-_.])(\d{2})\2(\d{2})(?!\d)"),
)

# HHMMSS with an optional sub-second tail, separated consistently or not at
# all: 143022891, 14-30-22, 14.30.22, 14:30:22. Seconds are REQUIRED. HH:MM
# alone appears in ordinary names ("budget-2026-09-06-10.30.xlsx" is a version,
# not a capture time) and demanding seconds costs nothing real: every camera,
# screenshot and messenger name in 1.2 carries them.
_TIME_RE = re.compile(
    r"(?<!\d)([01]\d|2[0-3])([-_.:]?)([0-5]\d)\2([0-5]\d)(?:\2?\d{1,3})?(?!\d)"
)

# Plausible years only. A capture timestamp is recent by definition, and the
# bound is what rejects "12345678" before datetime ever sees it.
_YEAR_MIN, _YEAR_MAX = 1970, 2099

# Prefixes that name the device or the capture app. This list NEVER fires on
# its own; it only enriches the detail of a timestamp finding that already
# fired. That is deliberate. "IMG_" is the generic DCF prefix used by most of
# the camera industry, so "IMG_1234.JPG" must not be flagged, and the moment a
# prefix can trigger a finding by itself the list has to be argued about
# prefix by prefix. Keeping it advisory-only means adding one can never create
# a false positive.
_DEVICE_PREFIXES = {
    "pxl": "Google Pixel camera (PXL_)",
    "mvimg": "Google motion photo (MVIMG_)",
    "img": "a DCF camera or phone (IMG_)",
    "vid": "a phone video recorder (VID_)",
    "dsc": "a Sony/Nikon-family camera (DSC)",
    "dscn": "a Nikon camera (DSCN)",
    "screenshot": "a screen capture",
}

# App names carried in filenames. Every entry here is a proper noun that is
# not also an ordinary English word, and that is the rule for adding one, not
# a preference. "signal", "teams", "slack", "zoom", "chrome" and "safari" are
# all real app names AND ordinary words, so a delimited match on them says
# nothing; they are deliberately absent, and a Signal screenshot is still
# reported through its timestamp. Under-reporting the app name is a known,
# named gap. Over-reporting it is the failure this list is shaped to avoid.
_APP_TOKENS = frozenset({
    "instagram", "whatsapp", "snapchat", "facebook", "messenger", "telegram",
    "tiktok", "twitter", "discord", "reddit", "linkedin", "youtube", "twitch",
    "wechat", "viber", "threema", "wickr", "tinder", "grindr", "bumble",
    "gmail", "outlook", "onlyfans", "pinterest", "tumblr", "dropbox",
    "imgur", "flickr", "spotify", "netflix", "kakaotalk",
})

_TOKEN_RE = re.compile(r"[a-z0-9]+")


@dataclass(frozen=True)
class NameLeak:
    """One identifier found in a filename. Advisory: it never fails a run."""

    kind: str
    evidence: str
    detail: str

    def as_dict(self) -> Dict[str, str]:
        return {"kind": self.kind, "evidence": self.evidence, "detail": self.detail}


def _valid_date(parts: Sequence[str]) -> bool:
    try:
        year, month, day = int(parts[0]), int(parts[1]), int(parts[2])
    except (TypeError, ValueError):
        return False
    if not _YEAR_MIN <= year <= _YEAR_MAX:
        return False
    try:
        _datetime.date(year, month, day)
    except ValueError:
        return False
    return True


def _dates_in(stem: str) -> List[Tuple[int, int, str]]:
    """Every real calendar date in the stem, as (start, end, text)."""
    found: List[Tuple[int, int, str]] = []
    for pattern in _DATE_PATTERNS:
        for match in pattern.finditer(stem):
            groups = match.groups()
            parts = (groups[0], groups[1], groups[2]) if len(groups) == 3 \
                else (groups[0], groups[2], groups[3])
            if _valid_date(parts):
                found.append((match.start(), match.end(), match.group(0)))
    return found


def _timestamp_in(stem: str) -> Optional[Tuple[str, str]]:
    """
    (date_text, time_text) when the stem carries both, else None.

    The date's own characters are MASKED OUT before the time is searched for,
    and that is load-bearing rather than tidy. Measured while writing this:
    "20260906" on its own matches _TIME_RE as 20:26:09.06, so searching the
    unmasked stem reports a timestamp leak on "20260906.txt", which is a bare
    date and exactly the name guard 2 above exists to leave alone. Masking is
    what keeps a date from manufacturing its own corroboration.
    """
    for start, end, date_text in _dates_in(stem):
        remainder = stem[:start] + " " + stem[end:]
        time_match = _TIME_RE.search(remainder)
        if time_match:
            return date_text, time_match.group(0)
    return None


def _device_hint(stem: str) -> Optional[str]:
    lowered = stem.lower()
    for prefix, description in _DEVICE_PREFIXES.items():
        if lowered.startswith(prefix) and lowered[len(prefix):len(prefix) + 1] in ("", "_", "-", "."):
            return description
    return None


def detect_name_leaks(file_path: str) -> List[NameLeak]:
    """
    Identifiers visible in a filename. Reported whether or not the caller asked
    for the rename, because a user who does not know the filename leaks cannot
    choose to fix it.

    Reporting is the whole job here: this returns findings, never an error and
    never a status, and nothing downstream turns a finding into a failure. A
    detector that can fail a pipeline is a detector whose false positives cost
    real money, and this one is heuristic by nature.
    """
    stem = os.path.splitext(os.path.basename(file_path))[0]
    if not stem:
        return []

    leaks: List[NameLeak] = []
    stamp = _timestamp_in(stem)
    if stamp:
        date_text, time_text = stamp
        detail = (
            f"the filename encodes a date and a time of day ({date_text}, "
            f"{time_text}); this is outside the file, so no byte scan can catch it"
        )
        hint = _device_hint(stem)
        if hint:
            detail += f", and the prefix identifies {hint}"
        leaks.append(NameLeak(LEAK_TIMESTAMP, f"{date_text} {time_text}", detail))

        # The app name is only ever reported ALONGSIDE a timestamp, never on
        # its own. Without that requirement "signal-processing.pdf" and
        # "netflix-cancellation-letter.pdf" become app disclosures, which they
        # are not. Requiring the timestamp costs one real-world shape (an app
        # name with no date in it, which none of the shapes in 1.2 have) and
        # removes the entire class of ordinary documents that merely mention a
        # product.
        for token in _TOKEN_RE.findall(stem.lower()):
            if token in _APP_TOKENS:
                leaks.append(NameLeak(
                    LEAK_APP, token,
                    f"the filename names an application ({token}), which says "
                    "what produced the file and, for a screenshot, what the "
                    "user was doing",
                ))
                break
    return leaks


# NEUTRAL OUTPUT NAMES
#
# The rule: <prefix><counter><ext>, e.g. file0001.jpg. Zero padded to four
# digits, extension preserved and lowercased. Stated with its failure mode,
# because every option here has one and picking the one whose failure is
# smallest is the whole decision:
#
#   A random name (file_a7f3c9e1.jpg) collides with nothing and carries
#   nothing, and destroys ordering and every trace of user meaning. Fifty
#   photos come back in an order nobody can reconstruct, including the owner.
#
#   A counter carries no capture time, no device and no app, keeps the set
#   browsable, and is stable across reruns. Its failure mode, stated plainly:
#   THE COUNTER PRESERVES ORDER. sanitize_directory() walks sorted() by
#   original filename, and the original filenames are the ones that encode
#   capture time, so file0001..file0050 come out in chronological order. An
#   observer who receives the whole set learns the relative chronology of the
#   photos. That is strictly less than a timestamp to the millisecond and it
#   is not nothing. A user who cares scrubs the files in a shuffled order, or
#   one at a time.
#
#   The second failure mode: the counter is unique per DIRECTORY, allocated
#   against what is on disk right now. Two directories each scrubbed to
#   file0001.jpg collide the day someone merges them into one folder. The
#   allocator cannot see that coming, and a user merging two neutral sets has
#   to expect it.
#
# Keeping the extension is not a choice: without it the format is
# unidentifiable to every tool and every operating system, and an unopenable
# file is not a privacy win. Lowercasing it is free and removes one small
# convention signal (.JPG is what DCF cameras write, .jpg is what everything
# else writes).
NEUTRAL_NAME_PREFIX = "file"
NEUTRAL_NAME_DIGITS = 4
_NEUTRAL_NAME_RE = re.compile(r"^" + NEUTRAL_NAME_PREFIX + r"\d{" + str(NEUTRAL_NAME_DIGITS) + r",}$")

# Bound on the search for a free name. An unbounded loop against a directory
# that cannot accept new files spins forever instead of reporting.
_MAX_NAME_ATTEMPTS = 100000


def _is_neutral_name(file_path: str) -> bool:
    """Already neutral, so a rerun must leave it alone rather than renumber it."""
    stem, ext = os.path.splitext(os.path.basename(file_path))
    return bool(_NEUTRAL_NAME_RE.match(stem)) and ext == ext.lower()


# FILESYSTEM TIMESTAMPS
#
# Epoch zero, kept as the value after auditing it rather than changed. It is
# uniform, deterministic and carries nothing from the original. Every uniform
# value is equally a tell that a tool ran; the only alternatives are a value
# derived from the original, which leaks, or a random one, which is a tell
# too and is not reproducible. So "epoch zero says a scrubber touched this"
# is not a defect of epoch zero, it is the unavoidable cost of normalising at
# all, and it is the cheap half of the trade.
NEUTRAL_TIMESTAMP = 0

# Windows FILETIME is 100ns ticks since 1601-01-01; the offset to the Unix
# epoch is fixed.
_FILETIME_EPOCH_DELTA = 116444736000000000


def _set_windows_creation_time(file_path: str, epoch_seconds: int) -> None:
    """
    Set the NTFS creation time, which os.utime() cannot reach.

    Measured 2026-09-06 on Windows 11, Python 3.11.3: after
    os.utime(path, (0, 0)) the file reported st_mtime 0.0 and st_atime 0.0 and
    st_ctime 1788750045.99, the moment of creation, unchanged. On Windows
    st_ctime IS the creation time, so before this function existed a file
    scrubbed with --reset-times sat on disk with an mtime of 1970 and a
    creation time still naming the minute it was captured or downloaded. Both
    are visible to anything that reads the directory, and the one that was not
    normalised is the one that carries the truth.

    ctypes and kernel32 only: no new dependency for a feature this small.
    """
    import ctypes
    from ctypes import wintypes

    class FILETIME(ctypes.Structure):
        _fields_ = [("dwLowDateTime", wintypes.DWORD),
                    ("dwHighDateTime", wintypes.DWORD)]

    ticks = _FILETIME_EPOCH_DELTA + int(epoch_seconds) * 10000000
    if ticks < 0:
        raise OSError("timestamp is before 1601 and cannot be stored on NTFS")

    kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
    kernel32.CreateFileW.restype = wintypes.HANDLE
    kernel32.CreateFileW.argtypes = [
        wintypes.LPCWSTR, wintypes.DWORD, wintypes.DWORD, wintypes.LPVOID,
        wintypes.DWORD, wintypes.DWORD, wintypes.HANDLE,
    ]
    kernel32.SetFileTime.argtypes = [
        wintypes.HANDLE, ctypes.POINTER(FILETIME),
        ctypes.POINTER(FILETIME), ctypes.POINTER(FILETIME),
    ]

    FILE_WRITE_ATTRIBUTES = 0x0100
    FILE_SHARE_ALL = 0x00000001 | 0x00000002 | 0x00000004
    OPEN_EXISTING = 3
    FILE_ATTRIBUTE_NORMAL = 0x00000080
    INVALID_HANDLE = wintypes.HANDLE(-1).value

    handle = kernel32.CreateFileW(
        file_path, FILE_WRITE_ATTRIBUTES, FILE_SHARE_ALL, None,
        OPEN_EXISTING, FILE_ATTRIBUTE_NORMAL, None,
    )
    if handle == INVALID_HANDLE:
        raise ctypes.WinError(ctypes.get_last_error())
    try:
        created = FILETIME(ticks & 0xFFFFFFFF, ticks >> 32)
        if not kernel32.SetFileTime(handle, ctypes.byref(created), None, None):
            raise ctypes.WinError(ctypes.get_last_error())
    finally:
        kernel32.CloseHandle(handle)


def reset_filesystem_times(file_path: str) -> Dict[str, str]:
    """
    Normalise every filesystem timestamp this platform lets us write, and
    report per timestamp what actually happened.

    The return value is a record and not a boolean on purpose. "creation time
    set to epoch" and "creation time left alone because this platform will not
    let anything set it" are different states with different consequences for
    the user, and collapsing them into "reset_times: true" is the same mistake
    trap 2 records: a caller cannot tell a normalised file from a partially
    normalised one, and the partially normalised one still carries a date.
    """
    record: Dict[str, str] = {}
    try:
        os.utime(file_path, (NEUTRAL_TIMESTAMP, NEUTRAL_TIMESTAMP))
        record["mtime"] = "epoch"
        record["atime"] = "epoch"
    except OSError as exc:
        record["mtime"] = f"failed: {exc}"
        record["atime"] = f"failed: {exc}"

    if sys.platform == "win32":
        try:
            _set_windows_creation_time(file_path, NEUTRAL_TIMESTAMP)
            record["creation"] = "epoch"
        except OSError as exc:
            record["creation"] = f"failed: {exc}"
    elif sys.platform == "darwin":
        # HFS+/APFS store a birth time and expose it as st_birthtime, but
        # there is no portable syscall to set it; the documented route is the
        # SetFile developer tool. Named rather than silently skipped: a macOS
        # user reading "reset" would otherwise believe a date was normalised
        # that is still sitting in the inode.
        record["creation"] = "not settable on macOS without developer tools"
    else:
        # Linux exposes a birth time through statx on some filesystems and
        # provides no interface at all for writing it.
        record["creation"] = "not settable on this platform"
    return record


class MetadataScrubber:
    """Removes metadata from a file and proves that it did."""

    def __init__(
        self,
        backup: bool = True,
        reset_times: bool = False,
        neutral_names: bool = False,
    ) -> None:
        """
        Args:
            backup: copy the original to <path>.backup before modifying it.
            reset_times: also normalise the filesystem mtime/atime, and the
                creation time where the platform allows it. Off by default
                because the filesystem timestamp belongs to the filesystem
                rather than the file's contents, and rewriting it breaks
                incremental backup and sync tooling. Users who are defending
                against timeline analysis want it on.
            neutral_names: rename the scrubbed file to a neutral name, in
                place. Off by default: see the block comment above on why the
                design document's "do it by default" is right for Android and
                wrong here. A user who does not pass this sees exactly the
                behaviour this tool had before it existed, except that a
                leaking filename is now REPORTED.
        """
        self.backup = backup
        self.reset_times = reset_times
        self.neutral_names = neutral_names
        # Next counter to try, per directory. Only a starting point: the
        # filesystem, not this cache, is what decides a name is free, so a
        # stale entry costs a wasted attempt and can never cause a collision.
        self._next_index: Dict[str, int] = {}

    def __enter__(self) -> "MetadataScrubber":
        return self

    def __exit__(self, exc_type, exc, tb) -> None:
        self.close()

    def close(self) -> None:
        exif_io.close_session()

    # SINGLE FILE

    def sanitize_file(
        self,
        file_path: str,
        fields_to_remove: Optional[Sequence[str]] = None,
        fields_to_sanitize: Optional[Sequence[str]] = None,
        remove_all: bool = False,
    ) -> Dict[str, Any]:
        start = datetime.now()

        if not os.path.exists(file_path):
            return self._error(file_path, "File not found", start)
        if not os.path.isfile(file_path):
            return self._error(file_path, "Not a regular file", start)

        spec = spec_for(file_path)
        if spec is None:
            reason = deferral_for(file_path)
            if reason:
                return self._terminal(file_path, STATUS_DEFERRED, reason, start)
            return self._terminal(
                file_path, STATUS_UNSUPPORTED,
                f"no engine handles {os.path.splitext(file_path)[1] or 'this file'}",
                start,
            )

        # A zero-byte file has no container to rewrite. Engines would either
        # fail confusingly or produce something invalid.
        try:
            if os.path.getsize(file_path) == 0:
                return self._finish_outside_the_file(self._terminal(
                    file_path, STATUS_CLEAN, "file is empty; nothing to remove", start,
                    spec=spec,
                ))
        except OSError as exc:
            return self._error(file_path, f"cannot stat file: {exc}", start)

        # Availability is checked BEFORE the baseline read, not after. A missing
        # dependency is a property of the machine rather than of the file, and
        # discovering it only after deciding the file was clean is how a run
        # reports success without an engine ever having been invoked.
        engine = get_engine(spec.engine)
        ok, reason = engine.available()
        if not ok:
            return self._error(file_path, f"{spec.engine.value} engine unavailable: {reason}", start)

        session = exif_io.session()
        before = session.read(file_path)

        # An unreadable baseline is an ERROR, never CLEAN. Without the baseline
        # there is nothing to search the output for, so the residual scan in
        # verify.py has no needles and would pass on any file at all. Reporting
        # "no metadata carriers found" here is the one wrong answer: it is
        # indistinguishable from success to every caller, including the exit
        # code and the report.
        if before.failed:
            return self._error(
                file_path,
                f"cannot read metadata to establish a baseline: {before.error}",
                start,
            )

        before_tags = _real_tags(before.metadata)

        # A baseline exiftool could not PARSE is an ERROR for exactly the same
        # reason an unreadable one is, and it arrives wearing the successful
        # read's clothes. Measured 2026-09-04 with exiftool 13.29 on a truncated
        # SVG carrying sodipodi:docname: exiftool identifies FileType SVG, exits
        # zero, reports no SVG tags at all, and says
        # "XMP format error (no closing tag for svg)". Everything it emits lands
        # in the File and ExifTool pseudo-groups, _real_tags() is empty, and the
        # CLEAN short circuit below then told the user "no metadata carriers
        # found" about a file that carries an editor's docname.
        #
        # Checked only when there are no real tags. When exiftool did surface
        # document tags there is a baseline to verify against and the engine
        # runs as before, so no format runs an engine it did not run before this
        # check existed. The failure mode this closes is the empty one.
        if not before_tags and before.unparsed:
            return self._error(
                file_path,
                "exiftool could not parse this file, so it reported no metadata "
                "carriers; that is not evidence the file carries none: "
                + "; ".join(before.parse_failures),
                start,
            )

        # Nothing to do. Reported honestly as CLEAN rather than as a successful
        # sanitize, so a user is never told metadata was removed from a file
        # that never carried any. Reachable only on a read that actually
        # succeeded AND that exiftool understood.
        if not before_tags:
            # A file that carries no metadata can still be named
            # PXL_20260906_143022891.jpg. The two leaks are independent, so
            # CLEAN gets the same outside-the-file treatment SANITIZED does;
            # skipping it here would mean the feature silently did nothing for
            # exactly the files that needed only the rename.
            return self._finish_outside_the_file(self._terminal(
                file_path, STATUS_CLEAN, "no metadata carriers found", start, spec=spec
            ))

        backup_path = None
        if self.backup:
            try:
                backup_path = self._make_backup(file_path)
            except OSError as exc:
                return self._error(file_path, f"backup failed: {exc}", start)

        removed: List[str] = []
        sanitized: List[str] = []
        try:
            selective = bool(fields_to_remove or fields_to_sanitize) and not remove_all
            if selective:
                if not engine.supports_selective:
                    return self._error(
                        file_path,
                        f"{spec.engine.value} engine cannot remove individual fields; "
                        "rerun with remove_all",
                        start,
                    )
                removed, sanitized = engine.strip_fields(
                    file_path, fields_to_remove or [], fields_to_sanitize or []
                )
            else:
                removed = engine.strip_all(file_path)
        except EngineError as exc:
            return self._error(file_path, str(exc), start, backup_path=backup_path)
        except Exception as exc:  # noqa: BLE001 - an engine must never escape untyped
            logger.exception("unexpected engine failure on %s", file_path)
            return self._error(file_path, f"unexpected failure: {exc}", start,
                               backup_path=backup_path)

        after = session.read(file_path)
        # A selective run is only responsible for the fields it was asked
        # to remove; the tags the user chose to keep are not leaks.
        targeted_fields = list(fields_to_remove or []) + list(fields_to_sanitize or [])
        verification = verify(
            file_path, spec, before.metadata, after.metadata,
            only_fields=targeted_fields if selective else None,
            # `parsed`, not `ok`. The same hole exists on the way out: if the
            # engine left a file exiftool can no longer parse, an empty `after`
            # would read as "no carriers remain" when it means "we could not
            # look". Measured across all 31 fixture formats, no scrubbed output
            # is UNPARSED, so this tightening changes no measured outcome; it
            # closes the symmetric case rather than leaving the identical bug
            # standing one line later.
            after_readable=after.parsed,
        )

        result = self._base_result(file_path, STATUS_SANITIZED, start, spec)
        result.update({
            "removed_fields": removed,
            "sanitized_fields": sanitized,
            "tags_before": before_tags,
            "backup": backup_path,
            "verification": verification.as_dict(),
        })

        # A format declared COMPLETE that did not verify clean is a failure of
        # this tool's central promise, not a warning. Downgrade the status so no
        # caller can read it as success.
        #
        # Read against the verdict rather than against `not verification.clean`,
        # because those two stopped being the same question on 2026-09-07:
        # NO_BASELINE_VALUES is not clean and is not a failure either. See
        # _VERDICTS_THAT_ARE_NOT_A_FINDING for why, at length.
        if (spec.completeness is Completeness.COMPLETE
                and verification.verdict not in _VERDICTS_THAT_ARE_NOT_A_FINDING):
            result["status"] = STATUS_ERROR
            result["error"] = (
                "verification failed: "
                f"{verification.detail or verification.verdict.value}"
            )

        # After the downgrade, never before it. A file that failed verification
        # is still leaking, and giving it a neutral name would hide the one
        # file in the run the user most needs to find again.
        return self._finish_outside_the_file(result)

    # IDENTIFIERS OUTSIDE THE FILE

    def _finish_outside_the_file(self, result: Dict[str, Any]) -> Dict[str, Any]:
        """
        Apply the opt-in filename and timestamp treatments, in that order.

        THE ORDER IS LOAD BEARING on Windows. The rename claims its
        destination name by creating a placeholder and replacing it, so the
        destination name is deleted and immediately reused. NTFS file system
        tunneling restores the creation time of a name reused within about 15
        seconds, so resetting the creation time first and renaming afterwards
        risks the rename handing the epoch-zero creation time straight back to
        the value tunneling remembered. Renaming first and stamping the final
        path afterwards is correct on every platform and does not depend on
        knowing whether tunneling is enabled on this volume.

        `result["file"]` is deliberately NOT rewritten. It is the path the
        caller asked about and the path the .backup is named after; a caller
        that reads it after a rename must get the identity it passed in, not a
        path whose meaning silently changed. The new location is a separate
        key, so "not renamed" and "renamed" cannot be confused.
        """
        if result.get("status") not in (STATUS_SANITIZED, STATUS_CLEAN):
            return result

        path = result["file"]
        if self.neutral_names:
            try:
                renamed = self._rename_neutral(path)
            except OSError as exc:
                # Not an error status: the metadata removal succeeded and the
                # file on disk is clean. But it is not silence either. The user
                # asked for a neutral name and does not have one, and only they
                # can decide whether that matters.
                result["rename_error"] = (
                    f"scrubbed, but could not rename to a neutral name: {exc}"
                )
                logger.warning("neutral rename failed for %s: %s", path, exc)
            else:
                if renamed is not None:
                    result["renamed_to"] = renamed
                    path = renamed

        if self.reset_times:
            result["timestamps_reset"] = reset_filesystem_times(path)
        return result

    def _rename_neutral(self, file_path: str) -> Optional[str]:
        """
        Move the file to <prefix><counter><ext> in the same directory.

        Returns the new path, or None when the name was already neutral and
        nothing needed to happen. Raises OSError when a neutral name could not
        be taken; it never returns quietly on failure, because a caller that
        cannot tell "renamed" from "failed to rename" will report the leak as
        fixed.

        NEVER OVERWRITES ANYTHING. The guard is not "check then rename", which
        is a race and, worse, is silently destructive on POSIX: os.rename()
        and os.replace() both clobber an existing destination on Linux and
        macOS without a word, and only Windows raises. So the name is CLAIMED
        atomically with O_CREAT|O_EXCL, which fails if anything already holds
        it, and only then is the file moved onto the placeholder this call
        just created and therefore owns. The same guard rules out clobbering a
        <file>.backup: a backup's name never matches the neutral pattern, so
        the placeholder open is what stops it, not a special case.
        """
        if _is_neutral_name(file_path):
            return None

        directory = os.path.dirname(os.path.abspath(file_path))
        ext = os.path.splitext(file_path)[1].lower()
        index = self._next_index.get(directory, 1)

        for _ in range(_MAX_NAME_ATTEMPTS):
            candidate = os.path.join(
                directory, f"{NEUTRAL_NAME_PREFIX}{index:0{NEUTRAL_NAME_DIGITS}d}{ext}"
            )
            index += 1
            try:
                handle = os.open(candidate, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600)
            except FileExistsError:
                continue
            os.close(handle)
            try:
                os.replace(file_path, candidate)
            except OSError:
                # Never leave the reservation behind as a zero-byte file
                # pretending to be output.
                try:
                    os.unlink(candidate)
                except OSError:
                    logger.warning("could not remove the placeholder %s", candidate)
                raise
            self._next_index[directory] = index
            return candidate

        raise OSError(
            f"no free neutral name in {directory} after {_MAX_NAME_ATTEMPTS} attempts"
        )

    # DIRECTORY

    def sanitize_directory(
        self,
        directory_path: str,
        fields_to_remove: Optional[Sequence[str]] = None,
        fields_to_sanitize: Optional[Sequence[str]] = None,
        remove_all: bool = False,
        recursive: bool = False,
        include_unsupported: bool = False,
    ) -> List[Dict[str, Any]]:
        """
        Process a directory. Files are handled one at a time, not in parallel:
        sanitizing mutates files in place, and concurrency here buys little
        while making a partial failure much harder to reason about.
        """
        if not os.path.isdir(directory_path):
            logger.error("not a directory: %s", directory_path)
            return []

        targets: List[str] = []
        for path in self._walk(directory_path, recursive):
            # Never treat our own backups as input. Without this, a second run
            # over the same directory sanitizes the backups too, which defeats
            # the point of having them.
            if path.endswith(BACKUP_SUFFIX):
                continue
            if is_supported(path) or include_unsupported:
                targets.append(path)

        if not targets:
            logger.warning("no supported files found in %s", directory_path)
            return []

        results = []
        for path in targets:
            results.append(
                self.sanitize_file(
                    path,
                    fields_to_remove=fields_to_remove,
                    fields_to_sanitize=fields_to_sanitize,
                    remove_all=remove_all,
                )
            )
        return results

    @staticmethod
    def _walk(directory_path: str, recursive: bool) -> List[str]:
        found = []
        if recursive:
            for root, _dirs, files in os.walk(directory_path):
                for name in files:
                    found.append(os.path.join(root, name))
        else:
            for name in os.listdir(directory_path):
                full = os.path.join(directory_path, name)
                if os.path.isfile(full):
                    found.append(full)
        return sorted(found)

    # BACKUP AND RESTORE

    def _make_backup(self, file_path: str) -> str:
        """
        Copy the original aside. An existing backup is preserved, never
        replaced: it is the only copy of the pre-sanitize file, and overwriting
        it on a second run would destroy the original permanently.
        """
        backup_path = file_path + BACKUP_SUFFIX
        if os.path.exists(backup_path):
            logger.info("backup already exists, keeping the original one: %s", backup_path)
            return backup_path
        shutil.copy2(file_path, backup_path)
        return backup_path

    def restore_backup(self, file_path: str) -> bool:
        backup_path = file_path + BACKUP_SUFFIX
        if not os.path.exists(backup_path):
            logger.error("backup not found: %s", backup_path)
            return False
        try:
            shutil.copy2(backup_path, file_path)
            return True
        except OSError as exc:
            logger.error("failed to restore %s: %s", file_path, exc)
            return False

    # REPORTING

    def generate_sanitization_report(
        self, results: Sequence[Dict[str, Any]], output_file: Optional[str] = None
    ) -> Dict[str, Any]:
        if not results:
            return {"error": "No sanitization results to report"}

        def count(status: str) -> int:
            return sum(1 for r in results if r.get("status") == status)

        verified = sum(
            1 for r in results
            if (r.get("verification") or {}).get("clean") is True
        )
        residual = [
            r["file"] for r in results
            if (r.get("verification") or {}).get("verdict") == Verdict.RESIDUAL_FOUND.value
        ]

        report = {
            "sanitize_date": datetime.now().isoformat(),
            "total_files": len(results),
            "sanitized_files": count(STATUS_SANITIZED),
            "clean_files": count(STATUS_CLEAN),
            "unsupported_files": count(STATUS_UNSUPPORTED),
            "deferred_files": count(STATUS_DEFERRED),
            "error_files": count(STATUS_ERROR),
            "verified_clean": verified,
            "files_with_residual_metadata": residual,
            # Outside-the-file identifiers. Counted separately from the byte
            # verdicts because they are a different claim: nothing here was
            # measured by scanning the output, it was read off the filename,
            # and folding the two into one number would let a filename finding
            # look like a residual-metadata finding.
            # Two keys, because "we found a leaking filename" and "a leaking
            # filename is still on disk" are different facts and the rename
            # turns the first into the second only sometimes. One key would
            # make a fixed leak and an unfixed one indistinguishable in the
            # report, which is the failure trap 2 names.
            "filenames_with_leaks": [
                r["file"] for r in results if r.get("name_leaks")
            ],
            "filenames_still_leaking": [
                r["file"] for r in results
                if r.get("name_leaks") and not r.get("renamed_to")
            ],
            "renamed_files": sum(1 for r in results if r.get("renamed_to")),
            "rename_failures": [
                r["file"] for r in results if r.get("rename_error")
            ],
            "total_fields_removed": sum(len(r.get("removed_fields", [])) for r in results),
            "total_fields_sanitized": sum(len(r.get("sanitized_fields", [])) for r in results),
            "sanitize_time": sum(r.get("sanitize_time") or 0 for r in results),
            "results": list(results),
        }

        if output_file:
            import json
            try:
                with open(output_file, "w", encoding="utf-8") as fh:
                    json.dump(report, fh, indent=2, default=str)
            except OSError as exc:
                logger.error("failed to write report: %s", exc)
        return report

    # RESULT HELPERS

    def _base_result(
        self, file_path: str, status: str, start: datetime,
        spec: Optional[FormatSpec] = None,
    ) -> Dict[str, Any]:
        result: Dict[str, Any] = {
            "file": file_path,
            "status": status,
            "sanitize_time": (datetime.now() - start).total_seconds(),
            "removed_fields": [],
            "sanitized_fields": [],
        }
        if spec is not None:
            result["engine"] = spec.engine.value
            result["completeness"] = spec.completeness.value
            result["rewrites_container"] = spec.rewrites_container
            if spec.note:
                result["engine_note"] = spec.note

        # Reported on EVERY status, including unsupported, deferred and error:
        # a file we refused to touch is still sitting there with its capture
        # time in its name, and that is worth knowing about a file the tool
        # could not clean more than one it could.
        #
        # The key is present only when there is something to say, so a run over
        # ordinary filenames produces byte-identical results to the ones this
        # tool produced before the detector existed. That is the difference
        # between adding a warning and changing everyone's output.
        leaks = detect_name_leaks(file_path)
        if leaks:
            result["name_leaks"] = [leak.as_dict() for leak in leaks]
        return result

    def _terminal(
        self, file_path: str, status: str, detail: str, start: datetime,
        spec: Optional[FormatSpec] = None,
    ) -> Dict[str, Any]:
        result = self._base_result(file_path, status, start, spec)
        result["detail"] = detail
        if status == STATUS_CLEAN:
            result["verification"] = Verification(
                verdict=Verdict.VERIFIED_CLEAN, detail=detail
            ).as_dict()
        return result

    def _error(
        self, file_path: str, message: str, start: datetime,
        backup_path: Optional[str] = None,
    ) -> Dict[str, Any]:
        result = self._base_result(file_path, STATUS_ERROR, start)
        result["error"] = message
        if backup_path:
            # Tell the user where the intact original is. An error message that
            # does not say how to get the file back is half an error message.
            result["backup"] = backup_path
        return result


# Backwards-compatible alias so the parent project call sites that do
# `from sanitizer import ExifSanitizer` keep working against this engine.
ExifSanitizer = MetadataScrubber

__all__ = [
    "MetadataScrubber", "ExifSanitizer", "CAPABILITIES",
    "STATUS_SANITIZED", "STATUS_CLEAN", "STATUS_UNSUPPORTED",
    "STATUS_DEFERRED", "STATUS_ERROR",
    "NameLeak", "detect_name_leaks", "reset_filesystem_times",
    "LEAK_TIMESTAMP", "LEAK_APP", "NEUTRAL_NAME_PREFIX", "NEUTRAL_TIMESTAMP",
]
