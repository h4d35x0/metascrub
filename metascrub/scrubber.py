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

import logging
import os
import shutil
from datetime import datetime
from typing import Any, Dict, List, Optional, Sequence

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


class MetadataScrubber:
    """Removes metadata from a file and proves that it did."""

    def __init__(self, backup: bool = True, reset_times: bool = False) -> None:
        """
        Args:
            backup: copy the original to <path>.backup before modifying it.
            reset_times: also normalise the filesystem mtime/atime. Off by
                default because the filesystem timestamp belongs to the
                filesystem rather than the file's contents, and rewriting it
                breaks incremental backup and sync tooling. Users who are
                defending against timeline analysis want it on.
        """
        self.backup = backup
        self.reset_times = reset_times

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
                return self._terminal(
                    file_path, STATUS_CLEAN, "file is empty; nothing to remove", start,
                    spec=spec,
                )
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
            return self._terminal(
                file_path, STATUS_CLEAN, "no metadata carriers found", start, spec=spec
            )

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

        if self.reset_times:
            try:
                os.utime(file_path, (0, 0))
            except OSError as exc:
                logger.warning("could not reset timestamps on %s: %s", file_path, exc)

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
        if spec.completeness is Completeness.COMPLETE and not verification.clean:
            result["status"] = STATUS_ERROR
            result["error"] = (
                "verification failed: "
                f"{verification.detail or verification.verdict.value}"
            )
        return result

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
]
