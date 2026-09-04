"""
End-to-end self check.

`doctor` answers "can the engines start". This answers the larger question a
launcher on a new machine actually raises: did the launcher resolve the right
project, are the dependencies really present, and does a real file go in dirty
and come out clean.

It exists because the POSIX launcher cannot be tested from the machine that
wrote it. Rather than claim it works on macOS and Linux, this gives one command
to run there that either proves it or says exactly what is missing.

Every check reports PASS, FAIL or SKIP. SKIP is a real outcome and never
counted as success: ffmpeg being absent is not a failure of metascrub, but it
is also not evidence that audio and video work.
"""

from __future__ import annotations

import os
import platform
import shutil
import sys
import tempfile
from typing import Callable, List, Optional, Tuple

PASS, FAIL, SKIP = "PASS", "FAIL", "SKIP"

Result = Tuple[str, str, str]  # (outcome, name, detail)


def _check_python() -> Result:
    version = ".".join(str(n) for n in sys.version_info[:3])
    if sys.version_info < (3, 9):
        return FAIL, "python", f"{version}; metascrub needs 3.9 or newer"
    return PASS, "python", f"{version} at {sys.executable}"


def _check_package() -> Result:
    """
    Confirm the package that got imported is the one the launcher intended.

    A launcher's whole job is resolving the project, and the failure mode worth
    catching is importing some OTHER metascrub - an old pip install, a stale
    copy on PYTHONPATH - while believing it ran this one.
    """
    try:
        import metascrub
    except ImportError as exc:
        return FAIL, "package", f"cannot import metascrub: {exc}"
    location = os.path.dirname(os.path.abspath(metascrub.__file__))
    return PASS, "package", f"{metascrub.__version__} from {location}"


def _check_exiftool() -> Result:
    """
    exiftool gates every format, so its absence is a FAIL rather than a SKIP.
    Nothing can be scrubbed or verified without it.
    """
    from . import exif_io

    binary = shutil.which("exiftool")
    ok, reason = exif_io.session().probe()
    if not ok:
        hint = {
            "Darwin": "brew install exiftool",
            "Linux": "sudo apt install libimage-exiftool-perl",
        }.get(platform.system(), "winget install OliverBetz.ExifTool")
        return FAIL, "exiftool", f"{reason}; install with: {hint}"
    return PASS, "exiftool", binary or "on PATH"


def _check_ffmpeg() -> Result:
    binary = shutil.which("ffmpeg")
    if not binary:
        return SKIP, "ffmpeg", "not on PATH; audio and video formats unavailable"
    return PASS, "ffmpeg", binary


def _check_tkinter() -> Result:
    try:
        import tkinter  # noqa: F401
    except ImportError as exc:
        hint = ("sudo apt install python3-tk"
                if platform.system() == "Linux" else "reinstall Python with Tk support")
        return SKIP, "tkinter", f"{exc}; the GUI is unavailable ({hint}). CLI unaffected"
    return PASS, "tkinter", "available; `metascrub gui` will run"


def _check_engines() -> Result:
    from .engines import engine_status

    statuses = engine_status()
    broken = {name: why for name, why in statuses.items() if why != "ready"}
    if broken:
        detail = "; ".join(f"{n}: {w}" for n, w in sorted(broken.items()))
        return FAIL, "engines", f"{len(broken)} of {len(statuses)} unavailable - {detail}"
    return PASS, "engines", f"all {len(statuses)} ready"


def _check_round_trip() -> Result:
    """
    The only check that proves anything: build a file carrying a known value,
    scrub it, and search the OUTPUT BYTES for that value.

    It deliberately does not ask the tool whether it succeeded. That is the
    same distinction the whole project rests on, and a self test that trusted
    the tool's own verdict would pass on precisely the bug this project was
    built to catch.
    """
    try:
        import pikepdf
    except ImportError as exc:
        return SKIP, "round trip", f"pikepdf not installed: {exc}"

    from .scrubber import STATUS_SANITIZED, MetadataScrubber

    sentinel = "METASCRUBSELFTEST0F3A91"
    directory = tempfile.mkdtemp(prefix="metascrub-selftest-")
    path = os.path.join(directory, "selftest.pdf")
    try:
        pdf = pikepdf.new()
        pdf.add_blank_page()
        with pdf.open_metadata() as meta:
            meta["dc:creator"] = [sentinel]
        pdf.docinfo["/Author"] = sentinel
        pdf.save(path)

        with open(path, "rb") as handle:
            if sentinel.encode() not in handle.read():
                return FAIL, "round trip", "fixture did not store the sentinel"

        with MetadataScrubber(backup=False) as scrubber:
            result = scrubber.sanitize_file(path, remove_all=True)

        with open(path, "rb") as handle:
            survived = sentinel.encode() in handle.read()

        if survived:
            return FAIL, "round trip", "the sentinel is STILL IN THE FILE after scrubbing"
        if result.get("status") != STATUS_SANITIZED:
            return FAIL, "round trip", f"status was {result.get('status')}: {result.get('error')}"
        if not (result.get("verification") or {}).get("clean"):
            return FAIL, "round trip", "bytes are clean but the tool did not verify it"
        return PASS, "round trip", "a PDF went in carrying a value and came out without it"
    except Exception as exc:  # noqa: BLE001 - a self test must report, never raise
        return FAIL, "round trip", f"{type(exc).__name__}: {exc}"
    finally:
        shutil.rmtree(directory, ignore_errors=True)


CHECKS: List[Callable[[], Result]] = [
    _check_python,
    _check_package,
    _check_exiftool,
    _check_ffmpeg,
    _check_tkinter,
    _check_engines,
    _check_round_trip,
]


def run(emit: Optional[Callable[[str, str, str], None]] = None) -> List[Result]:
    """Run every check. `emit` is called per result so a caller can stream them."""
    results = []
    for check in CHECKS:
        try:
            result = check()
        except Exception as exc:  # noqa: BLE001
            result = (FAIL, check.__name__, f"check itself raised: {exc}")
        results.append(result)
        if emit:
            emit(*result)
    return results


def exit_code(results: List[Result]) -> int:
    """Non-zero if anything failed. A SKIP is not a failure, and not a pass."""
    return 1 if any(outcome == FAIL for outcome, _, _ in results) else 0
