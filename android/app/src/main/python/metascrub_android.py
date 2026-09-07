"""
The only Python module written for the Android app. Everything below it is the
unmodified `metascrub` package copied out of the repository root at build time.

WHY THIS MODULE EXISTS AT ALL, RATHER THAN CALLING MetadataScrubber

`metascrub.scrubber.MetadataScrubber` cannot run on a phone, and that is not a
packaging problem that a wheel would fix. It reads a metadata baseline through
exiftool before touching a file, and it refuses to proceed when that read is
unavailable or unparseable. That refusal is correct desktop behaviour and it is
the fix for the fail-open defect recorded in CLAUDE.md trap 2: "could not read"
and "carries nothing" must never share a representation. exiftool is Perl and
does not run under Android's W^X and exec restrictions, so on this platform the
baseline read is unavailable for every file, and MetadataScrubber would
correctly refuse every file.

So the app drives the four pure-Python engines directly, and is honest in the
UI about what that costs: the residual-value scan the desktop tool runs after a
write needs baseline values it cannot obtain here. What IS available on device
is the structural scan (`metascrub.structure`), which asks a question that needs
no baseline: is there any region of the output that the format's own structure
does not account for? Where a walker exists for the container that is a real
proof; where one does not, this module reports `applicable: false` rather than
reporting a clean it did not measure.

THE OUTPUT IS ALWAYS A NEW FILE

The input is never modified. The activity hands this module a path it copied
out of the incoming content:// URI, and this module writes to a separate output
directory. Nothing here can reach a MediaStore entry, which is what makes the
"never destroy the original" promise structural rather than a policy the code
has to remember. It also means the desktop tool's `<path>.backup` policy has no
job on this platform.

THE OUTPUT NAME IS NEUTRAL, BY DEFAULT AND WITHOUT A TOGGLE

docs/ANDROID-MEDIA-BUILD.md section 1.2: `PXL_20260906_143022891.jpg` carries
the capture time to the millisecond and names the device family, and no byte
scan of the file will ever catch it because it is not in the file. The output
name here is `clean_<random>.<ext>` and the extension comes from what the bytes
say the container is, never from the name the caller supplied.

CONTAINER DETECTION IS BY MAGIC BYTES, NEVER BY EXTENSION

CLAUDE.md trap 8, measured on this project: a static extension-to-container map
returned four of eight extension/flavour combinations in the wrong flavour,
silently. On Android the input arrives as a content:// URI which frequently has
no usable name at all, so extension-based dispatch is not merely risky here, it
is often impossible.
"""

from __future__ import annotations

import json
import os
import secrets
import shutil
import traceback

from metascrub import structure
from metascrub.engines.isobmff_engine import IsobmffEngine
from metascrub.engines.jpeg_engine import JpegEngine
from metascrub.engines.png_engine import PngEngine
from metascrub.engines.webp_engine import WebpEngine

_JPEG = JpegEngine()
_PNG = PngEngine()
_WEBP = WebpEngine()

# remove_audio stays False here so the app default matches the desktop default
# exactly. Section 1.2 argues audio should be a prominent toggle; a toggle is UI
# work this shell does not have yet, so the honest state is the documented
# default rather than a silently different one.
_ISOBMFF = IsobmffEngine(remove_audio=False)

# ISO base media brands mapped to the extension the OUTPUT should carry. The
# brand is read out of the file, so a `.qt` input that is really ISO/MP4 gets
# `.mp4` and a `.mp4` input that is really QuickTime gets `.mov`.
_BRAND_EXTENSION = {
    b"qt  ": ".mov",
    b"heic": ".heic",
    b"heix": ".heic",
    b"hevc": ".heic",
    b"heim": ".heic",
    b"heis": ".heic",
    b"mif1": ".heif",
    b"msf1": ".heif",
    b"avif": ".avif",
    b"avis": ".avif",
    b"3gp4": ".3gp",
    b"3gp5": ".3gp",
    b"3gp6": ".3gp",
    b"3g2a": ".3g2",
}

_HEADER_BYTES = 32


def _read_header(path):
    with open(path, "rb") as handle:
        return handle.read(_HEADER_BYTES)


def detect(path):
    """
    Return (container_name, engine, output_extension), or (None, None, None).

    Nothing about the supplied filename is consulted.
    """
    header = _read_header(path)

    if header[:3] == b"\xff\xd8\xff":
        return "jpeg", _JPEG, ".jpg"

    if header[:8] == b"\x89PNG\r\n\x1a\n":
        return "png", _PNG, ".png"

    if header[:4] == b"RIFF" and header[8:12] == b"WEBP":
        return "webp", _WEBP, ".webp"

    if header[4:8] == b"ftyp":
        brand = header[8:12]
        return "isobmff", _ISOBMFF, _BRAND_EXTENSION.get(brand, ".mp4")

    return None, None, None


def _structure_report(path):
    report = structure.scan(path)
    return {
        "applicable": report.applicable,
        "unaccounted": list(report.unaccounted),
        "error": report.error,
    }


def scrub(input_path, output_dir):
    """
    Copy `input_path` into `output_dir` under a neutral name, strip it there,
    and return a JSON report as a string.

    JSON rather than an object because the Java side reads it with org.json, and
    a single string crossing the boundary is one thing to get right instead of a
    mapping of Python types.
    """
    report = {
        "ok": False,
        "container": None,
        "engine": None,
        "input_bytes": None,
        "output_bytes": None,
        "output_path": None,
        "removed": [],
        "structure": None,
        "error": None,
    }

    try:
        report["input_bytes"] = os.path.getsize(input_path)

        container, engine, extension = detect(input_path)
        if engine is None:
            header = _read_header(input_path)
            report["error"] = (
                "unsupported container. The first bytes are %r, which is not "
                "JPEG, PNG, WebP or ISO base media. This build carries only the "
                "four pure-Python engines; exiftool and ffmpeg cannot run here."
                % header[:12]
            )
            return json.dumps(report)

        report["container"] = container
        report["engine"] = engine.name

        os.makedirs(output_dir, exist_ok=True)
        output_path = os.path.join(
            output_dir, "clean_%s%s" % (secrets.token_hex(4), extension))

        # copyfile, not copy2: copy2 would carry the source mtime onto the
        # output, and the filesystem timestamp of the original is exactly the
        # kind of tier-2 identifier section 1.2 says to drop.
        shutil.copyfile(input_path, output_path)

        try:
            report["removed"] = list(engine.strip_all(output_path))
        except Exception:
            # A failed strip must not leave a half-written output behind for
            # the user to mistake for a cleaned file.
            if os.path.exists(output_path):
                try:
                    os.unlink(output_path)
                except OSError:
                    pass
            raise

        report["output_path"] = output_path
        report["output_bytes"] = os.path.getsize(output_path)
        report["structure"] = _structure_report(output_path)
        report["ok"] = True

    except Exception as exc:
        report["error"] = "%s: %s" % (type(exc).__name__, exc)
        report["traceback"] = traceback.format_exc()

    return json.dumps(report)


def diagnostics():
    """
    What actually loaded on this device. Used by the launcher screen and by the
    build notes, so that a claim about what runs on a phone is a reading rather
    than an assumption.
    """
    import platform
    import sys

    info = {
        "python": sys.version,
        "platform": platform.platform(),
        "engines": {},
        "structure_walkers": sorted(structure.supported_extensions()),
        "optional_desktop_dependencies": {},
    }

    for engine in (_JPEG, _PNG, _WEBP, _ISOBMFF):
        ok, reason = engine.available()
        info["engines"][engine.name] = "ready" if ok else reason

    # These are what the desktop tool needs and the phone cannot have. Reporting
    # them absent is the point: it is the evidence that the pure-Python engines
    # are the ones doing the work.
    for name in ("exiftool", "pikepdf", "olefile", "PIL"):
        try:
            __import__(name)
            info["optional_desktop_dependencies"][name] = "present"
        except Exception as exc:
            info["optional_desktop_dependencies"][name] = (
                "absent (%s)" % type(exc).__name__)

    try:
        import metascrub
        info["metascrub_version"] = metascrub.__version__
    except Exception as exc:
        info["metascrub_version"] = "import failed: %s" % exc

    return json.dumps(info)
