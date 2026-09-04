"""
Command line interface.

Subcommands:
    scrub    remove metadata from files or directories
    inspect  show what metadata a file carries, without changing it
    restore  put a file back from its .backup
    formats  list handled formats, their engine and their guarantee
    doctor   report which engines can actually run on this machine
    gui      open the desktop window
"""

from __future__ import annotations

import argparse
import json
import logging
import os
import sys
from typing import Dict, List

from . import exif_io
from .capabilities import CAPABILITIES, DEFERRED, Completeness, spec_for
from .engines import engine_status
from .scrubber import (
    STATUS_CLEAN, STATUS_DEFERRED, STATUS_ERROR, STATUS_SANITIZED,
    STATUS_UNSUPPORTED, MetadataScrubber,
)

def _stdout_is_tty() -> bool:
    """
    Whether stdout is a terminal, without assuming there is a stdout.

    `sys.stdout` is None in a detached GUI process: pythonw.exe has no console
    and no redirection, so the standard streams are None rather than closed
    files. `sys.stdout.isatty()` at import time therefore raised
    AttributeError before this module finished loading, which killed
    `pythonw -m metascrub gui` with no window and no error message, because
    there was nowhere for the traceback to go either.

    Measured 2026-09-04: importing metascrub.cli with sys.stdout set to None
    raised `AttributeError: 'NoneType' object has no attribute 'isatty'` at
    cli.py line 31. The pip-generated metascrub-gui.exe never hit it because it
    imports metascrub.gui:main directly and does not touch this module.

    A closed stream raises ValueError instead, so both are treated as "not a
    terminal", which is the safe answer: it only ever suppresses colour.
    """
    stream = getattr(sys, "stdout", None)
    try:
        return bool(stream.isatty())
    except (AttributeError, ValueError):
        return False


# ANSI colours, suppressed when stdout is redirected so piped output stays clean.
_TTY = _stdout_is_tty()


def _c(text: str, code: str) -> str:
    return f"\033[{code}m{text}\033[0m" if _TTY else text


GREEN, RED, YELLOW, GREY, BOLD = "32", "31", "33", "90", "1"

_STATUS_STYLE = {
    STATUS_SANITIZED: (GREEN, "SANITIZED"),
    STATUS_CLEAN: (GREY, "CLEAN"),
    STATUS_UNSUPPORTED: (GREY, "SKIPPED"),
    STATUS_DEFERRED: (YELLOW, "DEFERRED"),
    STATUS_ERROR: (RED, "FAILED"),
}


def _print_result(result: Dict) -> None:
    status = result.get("status", STATUS_ERROR)
    colour, label = _STATUS_STYLE.get(status, (RED, status.upper()))
    name = os.path.basename(result.get("file", "?"))
    line = f"  {_c(label.ljust(10), colour)} {name}"

    engine = result.get("engine")
    if engine:
        line += _c(f"  [{engine}]", GREY)
    print(line)

    verification = result.get("verification") or {}
    if verification.get("verdict") == "residual_found":
        values = verification.get("residual_values", [])
        print(_c(f"      STILL PRESENT after sanitizing: {len(values)} value(s)", RED))
        for value in values[:5]:
            print(_c(f"        {value[:70]}", RED))

    if result.get("error"):
        print(_c(f"      {result['error']}", RED))
    if result.get("detail") and status in (STATUS_DEFERRED, STATUS_UNSUPPORTED):
        print(_c(f"      {result['detail']}", GREY))

    # A PARTIAL format that succeeded still has to say so, or the user walks
    # away believing more than was actually delivered.
    if status == STATUS_SANITIZED and result.get("completeness") == Completeness.PARTIAL.value:
        print(_c(f"      PARTIAL: {result.get('engine_note', 'residue may remain')}", YELLOW))
    for note in result.get("removed_fields", []):
        if isinstance(note, str) and note.startswith("NOTE:"):
            print(_c(f"      {note}", YELLOW))


def _collect(paths: List[str], recursive: bool) -> List[str]:
    return [p for p in paths if os.path.exists(p)]


def cmd_scrub(args: argparse.Namespace) -> int:
    # Every engine reads its baseline through exiftool even when it writes with
    # something else, so a run without it cannot verify anything. Refuse up
    # front with the fix, rather than failing once per file partway through.
    ok, reason = exif_io.session().probe()
    if not ok:
        print(_c(f"  cannot scrub: {reason}", RED))
        print(_c("  exiftool is required to read the baseline metadata that "
                 "verification searches for.", GREY))
        print(_c("  Install it, then re-run:  winget install OliverBetz.ExifTool", GREY))
        exif_io.close_session()
        return 1

    scrubber = MetadataScrubber(backup=not args.no_backup, reset_times=args.reset_times)
    results: List[Dict] = []

    with scrubber:
        for target in args.paths:
            if not os.path.exists(target):
                print(_c(f"  MISSING    {target}", RED))
                continue
            if os.path.isdir(target):
                results.extend(
                    scrubber.sanitize_directory(
                        target,
                        fields_to_remove=args.remove_field,
                        fields_to_sanitize=args.sanitize_field,
                        remove_all=not (args.remove_field or args.sanitize_field),
                        recursive=args.recursive,
                    )
                )
            else:
                results.append(
                    scrubber.sanitize_file(
                        target,
                        fields_to_remove=args.remove_field,
                        fields_to_sanitize=args.sanitize_field,
                        remove_all=not (args.remove_field or args.sanitize_field),
                    )
                )

        for result in results:
            _print_result(result)

        report = scrubber.generate_sanitization_report(results, args.report)

    if not results:
        print("No files processed.")
        return 0

    print()
    print(_c("Summary", BOLD))
    print(f"  files processed   {report['total_files']}")
    print(f"  sanitized         {report['sanitized_files']}")
    print(f"  already clean     {report['clean_files']}")
    print(f"  verified clean    {report['verified_clean']}")
    if report["deferred_files"]:
        print(_c(f"  deferred format   {report['deferred_files']}", YELLOW))
    if report["error_files"]:
        print(_c(f"  failed            {report['error_files']}", RED))
    if report["files_with_residual_metadata"]:
        print(_c(
            f"  STILL LEAKING     {len(report['files_with_residual_metadata'])}", RED
        ))
    if args.report:
        print(f"  report written to {args.report}")

    # Non-zero exit when anything failed or anything still leaks, so this can be
    # used in a pipeline without the caller having to parse the report.
    return 1 if (report["error_files"] or report["files_with_residual_metadata"]) else 0


def cmd_inspect(args: argparse.Namespace) -> int:
    session = exif_io.session()
    try:
        for path in args.paths:
            if not os.path.isfile(path):
                print(_c(f"not a file: {path}", RED))
                continue
            spec = spec_for(path)
            print(_c(os.path.basename(path), BOLD))
            if spec:
                print(f"  engine {spec.engine.value}, {spec.completeness.value}"
                      + (f", {spec.note}" if spec.note else ""))
            else:
                reason = DEFERRED.get(os.path.splitext(path.lower())[1])
                print(_c(f"  {reason or 'format not handled'}", YELLOW if reason else GREY))
            read = session.read(path)
            if read.failed:
                # "could not read" must not print like "carries nothing".
                print(_c(f"    cannot read metadata: {read.error}", RED))
                print()
                continue
            shown = 0
            for key, value in sorted(read.metadata.items()):
                group = key.split(":", 1)[0] if ":" in key else key
                if group in ("File", "System", "Composite", "ExifTool") or key == "SourceFile":
                    continue
                print(f"    {key:<44} {str(value)[:60]}")
                shown += 1
            if not shown:
                print(_c("    no metadata carriers found", GREY))
            print()
    finally:
        exif_io.close_session()
    return 0


def cmd_restore(args: argparse.Namespace) -> int:
    scrubber = MetadataScrubber()
    failures = 0
    for path in args.paths:
        if scrubber.restore_backup(path):
            print(_c(f"  RESTORED   {os.path.basename(path)}", GREEN))
        else:
            print(_c(f"  NO BACKUP  {os.path.basename(path)}", RED))
            failures += 1
    return 1 if failures else 0


def cmd_formats(_args: argparse.Namespace) -> int:
    print(_c(f"{'EXT':<8} {'ENGINE':<10} {'GUARANTEE':<10} NOTE", BOLD))
    for ext in sorted(CAPABILITIES):
        spec = CAPABILITIES[ext]
        guarantee = spec.completeness.value
        colour = GREEN if spec.completeness is Completeness.COMPLETE else YELLOW
        print(f"{ext:<8} {spec.engine.value:<10} {_c(guarantee.ljust(10), colour)} {spec.note}")
    if DEFERRED:
        print()
        print(_c("Deferred, not handled yet:", YELLOW))
        for ext, reason in sorted(DEFERRED.items()):
            print(f"{ext:<8} {reason}")
    return 0


def cmd_doctor(_args: argparse.Namespace) -> int:
    print(_c("Engine status", BOLD))
    failed = 0
    for name, status in sorted(engine_status().items()):
        ok = status == "ready"
        if not ok:
            failed += 1
        print(f"  {name:<10} {_c(status, GREEN if ok else RED)}")
    exif_io.close_session()
    return 1 if failed else 0


def cmd_gui(args: argparse.Namespace) -> int:
    """
    Launch the desktop window.

    Imported here rather than at module scope so the CLI keeps working on a
    machine with no Tk (a headless server, a stripped Python), instead of
    failing at import time for users who never wanted the GUI.
    """
    try:
        from .gui import main as gui_main
    except ImportError as exc:
        print(_c(f"  cannot start the GUI: {exc}", RED))
        print(_c("  tkinter is part of the standard library but is packaged "
                 "separately on some Linux distributions", GREY))
        print(_c("  Debian/Ubuntu:  sudo apt install python3-tk", GREY))
        return 1
    return gui_main(args.paths)


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="metascrub",
        description="Remove metadata from documents, images, audio and video, "
                    "and verify the removal.",
    )
    parser.add_argument("-v", "--verbose", action="store_true", help="log engine detail")
    sub = parser.add_subparsers(dest="command", required=True)

    scrub = sub.add_parser("scrub", help="remove metadata")
    scrub.add_argument("paths", nargs="+", help="files or directories")
    scrub.add_argument("-r", "--recursive", action="store_true",
                       help="descend into subdirectories")
    scrub.add_argument("--no-backup", action="store_true",
                       help="do not write <file>.backup before modifying")
    scrub.add_argument("--reset-times", action="store_true",
                       help="also normalise filesystem timestamps")
    scrub.add_argument("--remove-field", action="append", metavar="TAG",
                       help="remove one tag instead of everything (exiftool formats only)")
    scrub.add_argument("--sanitize-field", action="append", metavar="TAG",
                       help="blank one tag but keep it present (exiftool formats only)")
    scrub.add_argument("--report", metavar="FILE", help="write a JSON report")
    scrub.set_defaults(func=cmd_scrub)

    inspect = sub.add_parser("inspect", help="show metadata without changing anything")
    inspect.add_argument("paths", nargs="+")
    inspect.set_defaults(func=cmd_inspect)

    restore = sub.add_parser("restore", help="restore files from their .backup")
    restore.add_argument("paths", nargs="+")
    restore.set_defaults(func=cmd_restore)

    formats = sub.add_parser("formats", help="list handled formats")
    formats.set_defaults(func=cmd_formats)

    doctor = sub.add_parser("doctor", help="check engine availability")
    doctor.set_defaults(func=cmd_doctor)

    gui = sub.add_parser("gui", help="open the desktop window")
    gui.add_argument("paths", nargs="*", help="files or directories to pre-queue")
    gui.set_defaults(func=cmd_gui)
    return parser


def main(argv=None) -> int:
    args = build_parser().parse_args(argv)
    logging.basicConfig(
        level=logging.DEBUG if args.verbose else logging.WARNING,
        format="%(levelname)s %(name)s: %(message)s",
    )
    try:
        return args.func(args)
    except KeyboardInterrupt:
        print("\ninterrupted", file=sys.stderr)
        return 130
    finally:
        exif_io.close_session()


if __name__ == "__main__":
    sys.exit(main())
