"""
Fixture factories.

Every fixture embeds a unique sentinel string in its metadata. That sentinel is
what makes the tests meaningful: after sanitizing, the test searches the output
bytes for the sentinel directly, rather than asking the same library that wrote
the file whether it thinks the file is clean.

This distinction is the whole point. Measured on 2026-09-04, a PDF stripped with
`exiftool -all=` reads back as having no author or title while still carrying
both strings in its bytes. A test built on engine read-back would have passed on
that file. A sentinel test fails on it, correctly.

Each factory asserts the sentinel is actually present before the test runs, so a
format that silently refused to store metadata can never produce a test that
passes by having nothing to remove.
"""

from __future__ import annotations

import atexit
import os
import shutil
import subprocess
import tempfile
import zipfile

import pytest

# Sample identifier used to make every sentinel unique per format and per run.
_FIXTURE_SERIAL = "112603"


def sentinel(tag: str) -> str:
    """A distinctive, searchable value that will not occur by chance."""
    return f"METASCRUBSENTINEL{tag.upper()}{_FIXTURE_SERIAL}"


def raw_contains(path: str, needle: str) -> bool:
    """Search the file bytes directly, in the encodings metadata carriers use."""
    with open(path, "rb") as fh:
        blob = fh.read()
    return any(
        needle.encode(enc) in blob
        for enc in ("utf-8", "utf-16-le", "latin-1")
    )


def zip_contains(path: str, needle: str) -> bool:
    """
    Search inflated zip members. A .docx keeps its metadata in deflated parts,
    so a raw byte search of the package would find nothing and report a false
    clean.
    """
    try:
        with zipfile.ZipFile(path) as zf:
            for name in zf.namelist():
                try:
                    data = zf.read(name)
                except Exception:
                    continue
                if any(needle.encode(e) in data for e in ("utf-8", "utf-16-le", "latin-1")):
                    return True
                if needle.encode("utf-8") in name.encode("utf-8"):
                    return True
    except Exception:
        return False
    return False


def contains_anywhere(path: str, needle: str) -> bool:
    """Container-aware search, used by tests to decide if a value survived."""
    if zipfile.is_zipfile(path):
        return zip_contains(path, needle)
    return raw_contains(path, needle)


HAVE_FFMPEG = shutil.which("ffmpeg") is not None
HAVE_EXIFTOOL = shutil.which("exiftool") is not None


def _load_ole2_builder():
    """
    Load tests/fixtures/ole2/build_ole2.py by path.

    By path rather than by package import because tests/fixtures/ has no
    __init__.py and adding one would put fixture data on the import path, where
    a module named like a stdlib module would shadow it. That is not
    hypothetical: a scratch file named inspect.py shadowed the stdlib `inspect`
    during this work and broke olefile's import with a circular-import error
    that named neither file.
    """
    import importlib.util

    location = os.path.join(
        os.path.dirname(os.path.abspath(__file__)), "fixtures", "ole2", "build_ole2.py"
    )
    spec = importlib.util.spec_from_file_location("metascrub_ole2_fixtures", location)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def _load_odf_builder():
    """Load tests/fixtures/odf/build_odf.py by path, for the reason above."""
    import importlib.util

    location = os.path.join(
        os.path.dirname(os.path.abspath(__file__)), "fixtures", "odf", "build_odf.py"
    )
    spec = importlib.util.spec_from_file_location("metascrub_odf_fixtures", location)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


build_ole2 = _load_ole2_builder()
build_odf = _load_odf_builder()
HAVE_LIBREOFFICE = build_ole2.find_soffice() is not None
HAVE_LIBMAGIC = shutil.which("file") is not None


# THE EXIFTOOL ORACLE
#
# Phase 0 of the Android media build. docs/ANDROID-MEDIA-BUILD.md Part 3.3.
#
# exiftool is not shipped to the phone. It is still required to develop and test
# the mobile engines, because a pure-Python reader checking a pure-Python writer
# is precisely the circularity traps 2 and 12 exist to prevent. The oracle
# scrubs nothing itself: it asks a mature, independent, 25-year-old
# implementation what it can still see in output some other engine produced.
#
# THREE THINGS THE ORACLE IS NOT.
#
# 1. It is not sufficient, and Part 3.3 was corrected to say so. A JPEG stripped
#    to the Part 2.1 keep-list but still carrying a motion photo trailer reports
#    ZERO tags and ZERO warnings to exiftool while carrying a complete MP4 and
#    its sentinel; the same silence holds for a WebP with an MP4 past its
#    declared RIFF size. The gate is the oracle PLUS the structural assertions
#    of Part 3.2, never the oracle alone. An oracle that is silent on the worst
#    case manufactures exactly the false confidence this project refuses.
# 2. It cannot assert "zero tags". See the allowlists below.
# 3. It is not the byte search. raw_contains() stays, seeded from the values the
#    reader found, because it catches value-level leaks the oracle is not
#    looking for.
#
# WHY IT FAILS RATHER THAN SKIPS, AND HOW THAT SITS WITH THE REST OF THE SUITE.
#
# metascrub/selftest.py:55-58 already states this policy for the shipping self
# check: "exiftool gates every format, so its absence is a FAIL rather than a
# SKIP." tests/test_selftest.py:39,
# test_exiftool_failure_is_reported_as_fail_not_skip, enforces it. The oracle
# applies the same rule inside the suite.
#
# Five tests do the opposite today, and they are exactly the oracle-shaped ones:
# test_verify_structural.py:145, :188, :304 and test_zip_timestamps.py:38, :54
# all carry @pytest.mark.skipif(not HAVE_EXIFTOOL, ...).
#
# DECIDED, and written down rather than smoothed over: this file does NOT change
# those five, and HAVE_EXIFTOOL stays exactly as it is so those two modules keep
# importing and behaving unchanged. Converting them belongs in a separate commit
# in files this change does not own. The cost of leaving them is small and worth
# naming: on a machine with no exiftool those five vanish quietly while the rest
# of the suite goes red anyway, so the skip never buys a green run, it only
# hides which five tests stopped measuring. (That "goes red anyway" is REASONED
# from the code and not measured here: make_image calls _exiftool_write, which
# reaches ExifSession.helper, which raises when pyexiftool or the binary is
# missing. No machine without exiftool was available to run it on.)
# New oracle assertions must not join them.
#
# There is deliberately NO environment-variable escape hatch. A switch that
# turns the independence check off is a switch someone in a hurry will pull. A
# contributor genuinely without exiftool can say so in the invocation, where it
# is visible, rather than in the environment, where it is not.

# CI pins an exact exiftool version, and its workflow comment records that
# Ubuntu noble's 12.76 produced 106 failures. A FLOOR rather than a pin here,
# because a dev box ahead of CI must not fail for being ahead: 13.29 measured on
# this machine 2026-09-06, and CLAUDE.md records 13.59 working. An oracle
# silently running against 12.x is a weaker oracle reporting the same green.
_ORACLE_MIN_EXIFTOOL = (13, 0)

# Set once the version floor has been checked. exiftool cannot change version
# mid-run, and the check costs a round trip to the helper process, so it is done
# on first use rather than on every assertion.
_oracle_version_checked = False


def _parse_exiftool_version(raw):
    """Turn an exiftool version string into a tuple, or None if it is not one."""
    import re

    match = re.match(r"^\s*(\d+)\.(\d+)", str(raw or ""))
    if not match:
        return None
    return (int(match.group(1)), int(match.group(2)))


def exiftool_or_fail():
    """
    Return the shared exiftool session, or FAIL the test with the fix in hand.

    Never skips. See the policy note above.
    """
    global _oracle_version_checked

    from metascrub import exif_io

    exif_session = exif_io.session()
    ok, reason = exif_session.probe()
    if not ok:
        pytest.fail(
            "exiftool is REQUIRED as the independent oracle and is not usable: "
            f"{reason}. Install it (winget install OliverBetz.ExifTool, "
            "brew install exiftool, or the GitHub tag archive as CI does), "
            f"version {_ORACLE_MIN_EXIFTOOL[0]}.x or newer. This is a FAIL and "
            "not a SKIP on purpose: without exiftool the suite cannot tell a "
            "clean file from an unread one."
        )
    if not _oracle_version_checked:
        raw = getattr(exif_session.helper, "version", None)
        parsed = _parse_exiftool_version(raw)
        if parsed is None:
            pytest.fail(
                f"could not read the exiftool version (got {raw!r}); the oracle "
                "will not run against an unknown build"
            )
        if parsed < _ORACLE_MIN_EXIFTOOL:
            pytest.fail(
                f"exiftool {raw} is below the oracle floor "
                f"{_ORACLE_MIN_EXIFTOOL[0]}.{_ORACLE_MIN_EXIFTOOL[1]}. CI's "
                "workflow records 106 test failures on 12.76. A weaker oracle "
                "reporting the same green is worse than no oracle."
            )
        _oracle_version_checked = True
    return exif_session


def oracle_metadata(path: str):
    """
    Everything exiftool can still see in `path`, group-prefixed and raw.

    Three ways this fails the build rather than returning something, each of
    them a state that must never be confused with "the file is clean":
      - exiftool absent            -> exiftool_or_fail()
      - the read could not be done -> MetadataRead.failed
      - exiftool could not PARSE   -> MetadataRead.unparsed, trap 12: silence
                                      from an unparsed read is not evidence
    """
    read = exiftool_or_fail().read(path)
    if read.failed:
        pytest.fail(f"the oracle could not read {path}: {read.error}")
    if read.unparsed:
        pytest.fail(
            f"the oracle could not PARSE {path}: {read.error}. A file exiftool "
            "cannot parse is a corrupted or unreadable output, never a clean "
            "one. Trap 12."
        )
    return read.metadata


def oracle_tags(path: str):
    """
    Sorted "Group:Tag" keys the oracle can still see, filesystem facts removed.

    Uses scrubber._real_tags, which filters scrubber._PSEUDO (File, System,
    Composite, ExifTool, SourceFile) and NOT verify._PSEUDO_GROUPS, which also
    drops ZIP. None of the mobile containers is a zip, and the stricter set is
    the safer default.

    Deliberately NOT verify.meaningful_values(): that answers a different
    question, and it would additionally drop everything named in
    verify._STRUCTURAL_TAGS, fourteen bare ISO base media tag names. Using it
    here would make the oracle blind inside the very container the mobile build
    targets.
    """
    from metascrub.scrubber import _real_tags

    return _real_tags(oracle_metadata(path))


# THE ALLOWLIST, AND WHY IT HAS THE SHAPE verify.py ALREADY USES
#
# The oracle cannot assert zero tags: a correctly scrubbed file still reports
# the fields that describe its own pixels. So there has to be an allowlist, and
# an allowlist carries trap 11's danger in full. Every name in one of these
# lists is a name that is never checked again in any file that passes that list.
#
# Two tiers, mirroring verify._STRUCTURAL_TAGS and verify._STRUCTURAL_VALUES:
#
#   ORACLE_ALLOW_*   bare keys, allowed whatever their value. ONLY for fields
#                    that cannot carry identity at all: image geometry, bit
#                    depths, box versions, byte offsets, codec four-character
#                    codes.
#   ORACLE_GATED_*   keys allowed only when a predicate accepts the VALUE.
#                    Everything that can hold a free string or a wall-clock time
#                    lives here, because that is where a leak would land.
#
# Nothing here is a default. Every assert_oracle_sees_nothing() call names the
# list it is using, so a list built for one container can never silently widen
# to another. That is trap 11's rule applied to the oracle.
#
# ALL of the measurements below: exiftool 13.29, this machine, 2026-09-06,
# against output the CURRENT shipping engines produced. Phase 1 and Phase 2
# replace those engines. When they do, RE-MEASURE. Do not inherit these lists.

# JPEG. Measured: the exiftool engine leaves ZERO real tags on a scrubbed JPEG
# that carried EXIF (Make, Model, Software, Artist, Copyright, UserComment),
# XMP, GPS and a COM marker. The empty set is the measurement, not an ambition.
#
# One blind spot to carry into Phase 1, found while measuring this: a JPEG COM
# marker comes back from exiftool as `File:Comment`, and `File` is in
# scrubber._PSEUDO, so _real_tags() drops it before the oracle ever sees it. A
# COM marker is invisible to oracle_tags(). Byte-search it, or assert over the
# marker inventory per Part 3.2.
ORACLE_ALLOW_JPEG = frozenset()

# PNG. Measured on the exiftool engine's output. All seven are IHDR fields.
# IHDR is mandatory: strip it and there is no image. None of the seven can hold
# a string, so none of them can carry identity.
ORACLE_ALLOW_PNG = frozenset({
    "PNG:BitDepth",      # IHDR bit depth
    "PNG:ColorType",     # IHDR colour type
    "PNG:Compression",   # IHDR compression method, always 0 in a valid PNG
    "PNG:Filter",        # IHDR filter method, always 0 in a valid PNG
    "PNG:ImageHeight",   # IHDR height
    "PNG:ImageWidth",    # IHDR width
    "PNG:Interlace",     # IHDR interlace method
})

# PNG, Phase 1 ONLY, and opt-in rather than folded into the line above.
#
# The Part 2.2 keep-list keeps gAMA, cHRM, sBIT, pHYs and sRGB. The CURRENT
# exiftool engine strips all five, which is why they are absent from
# ORACLE_ALLOW_PNG. Part 3.3 names five tags a correctly scrubbed keep-list PNG
# still reports.
#
# What was measured here and what was not, stated separately because they are
# not the same claim:
#   MEASURED 2026-09-06: exiftool reports PNG:Gamma (gAMA), PNG:PixelsPerUnitX,
#   PNG:PixelsPerUnitY and PNG:PixelUnits (pHYs), and PNG:SRGBRendering (sRGB)
#   when those chunks are present.
#   MEASURED 2026-09-06 by the PNG team, and RE-MEASURED INDEPENDENTLY
#   2026-09-06 before the nine names below were added: cHRM and sBIT. Not
#   inherited. exiftool 13.29 will not WRITE those two chunks on request, which
#   is what stopped the earlier measurement; building the chunks by hand and
#   handing exiftool the result works, and that is what was done. A PNG
#   carrying cHRM 31270/32900/64000/33000/30000/60000/15000/6000 and sBIT
#   08 08 08 reported, through scrubber._real_tags:
#     PNG:WhitePointX 0.3127   PNG:WhitePointY 0.329
#     PNG:RedX 0.64            PNG:RedY 0.33
#     PNG:GreenX 0.3           PNG:GreenY 0.6
#     PNG:BlueX 0.15           PNG:BlueY 0.06
#     PNG:SignificantBits '8 8 8'
#   EIGHT tags out of cHRM, not the two Part 3.3 names. Part 3.3 is wrong about
#   that and the measurement is what is encoded here.
#
# tests/test_oracle_allowlist.py is the trap 11 overcorrection test that landed
# with these nine, and it is where the widening is proven not to have blinded
# the oracle: it re-injects real identity tags into a fixture carrying every one
# of the nine and requires assert_oracle_sees_nothing to still FAIL.
#
# ONE HONEST QUALIFICATION, because this list claims its members "cannot carry
# identity at all" and cHRM's eight can carry arbitrary bits: a chromaticity is
# a 4-byte fixed-point number, so eight of them are 32 bytes an attacker could
# in principle write anything into. What they cannot hold is a STRING or a
# WALL-CLOCK TIME, which is what the oracle exists to catch, and any value that
# is not the true chromaticity changes how the image renders. That is the same
# bar the seven IHDR fields clear and a weaker one than it sounds: pHYs is
# already on this list and PixelsPerUnitX 5669 is a producer signature in its
# own right (png_engine.py says so). Structural assertion over the chunk
# inventory is what covers that gap, not this list, and it always was.
ORACLE_ALLOW_PNG_KEEPLIST = ORACLE_ALLOW_PNG | frozenset({
    "PNG:Gamma",           # gAMA, one 4-byte fixed-point number
    "PNG:PixelsPerUnitX",  # pHYs, physical pixel dimensions
    "PNG:PixelsPerUnitY",  # pHYs
    "PNG:PixelUnits",      # pHYs unit specifier, 0 or 1
    "PNG:SRGBRendering",   # sRGB, one rendering-intent byte
    # cHRM, eight 4-byte fixed-point chromaticity coordinates. Each one names a
    # point in the CIE xy plane: where the display's white, red, green and blue
    # primaries sit. There is no string field and no time field in the chunk,
    # its length is fixed at 32 bytes so nothing can be appended to it, and a
    # value that is not the real primary makes the picture render in the wrong
    # colours. It describes the pixels; it cannot name the person.
    "PNG:WhitePointX",     # cHRM, white point x
    "PNG:WhitePointY",     # cHRM, white point y
    "PNG:RedX",            # cHRM, red primary x
    "PNG:RedY",            # cHRM, red primary y
    "PNG:GreenX",          # cHRM, green primary x
    "PNG:GreenY",          # cHRM, green primary y
    "PNG:BlueX",           # cHRM, blue primary x
    "PNG:BlueY",           # cHRM, blue primary y
    # sBIT, one byte per channel saying how many bits of each sample were
    # significant in the original. Between one and four bytes, fixed by the
    # colour type, and every byte is bounded to 1..bit depth, so the whole
    # chunk holds at most four numbers in the range 1..16. Not a string, not a
    # time, and too small to hold a name even if it were free.
    "PNG:SignificantBits",
})

# WebP. Measured on the exiftool engine's output. All five come out of the VP8
# bitstream frame header, which is the compressed image itself.
ORACLE_ALLOW_WEBP = frozenset({
    "RIFF:ImageWidth",       # VP8 keyframe header
    "RIFF:ImageHeight",      # VP8 keyframe header
    "RIFF:HorizontalScale",  # VP8 keyframe header upscaling hint
    "RIFF:VerticalScale",    # VP8 keyframe header upscaling hint
    "RIFF:VP8Version",       # VP8 bitstream profile number
})
# Measured ABSENT after a scrub and therefore deliberately NOT allowlisted:
# RIFF:WebP_Flags. That is the VP8X flag field, and Part 2.3 says to assert over
# the actual chunk inventory rather than trust it, because it is an
# attacker-controlled field. If it reappears, the oracle should say so.


def _is_zeroed_isobmff_time(value) -> bool:
    """
    An ISO base media time that has actually been zeroed.

    Part 3.2 requires mvhd/tkhd/mdhd times to be zero. Measured 2026-09-06 on
    the current av engine's output: exiftool renders the zeroed field as
    '0000:00:00 00:00:00'. '1904:01:01 00:00:00' is the same instant rendered by
    an exiftool that applied the QuickTime epoch instead, so both are accepted
    and nothing else is. A real capture time is a leak.
    """
    return str(value).strip() in {"0000:00:00 00:00:00", "1904:01:01 00:00:00"}


def _is_neutral_handler_description(value) -> bool:
    """
    hdlr `name`, which is a free-form string and a real carrier.

    Measured 2026-09-06: ffmpeg's mov muxer writes 'VideoHandler' and
    'SoundHandler'. Devices and editors write their own product names in the
    same field, which is exactly what must not survive, so this is an exact set
    and not a "looks harmless" judgement.
    """
    return str(value).strip() in {"", "VideoHandler", "SoundHandler"}


def _is_neutral_handler_vendor(value) -> bool:
    """
    hdlr vendor field. ffmpeg writes 'appl'; the zero vendor is four NUL bytes,
    which exiftool renders as an empty string. Anything else names a product.
    """
    return str(value).strip() in {"", "appl"}


def _is_undetermined_language(value) -> bool:
    """
    mdhd language. 'und' is ISO 639-2 for undetermined, which is what a scrubbed
    track should say. A real language code is a weak but real signal about the
    person who recorded it, so it does not pass.
    """
    return str(value).strip() in {"", "und"}


# ISO base media, as produced by the CURRENT av engine, which is an ffmpeg
# remux.
#
# THIS IS THE LARGEST LIST HERE AND THE MOST DANGEROUS. It exists so Phase 2 has
# a measured starting point rather than a guess, NOT as a target to be matched.
# The current engine remuxes; the Part 2.5 engine does in-place box surgery and
# leaves mdat alone, so it will produce a different and probably shorter list.
# Phase 2 must re-measure and shrink this, and must not inherit it unexamined.
#
# Every name below was measured present after MetadataScrubber ran on an MP4,
# and every one of them is a number, a fixed-point matrix, a byte offset, a box
# version or a four-character code. Anything that came back as a free string or
# a wall-clock time is NOT here: it is in ORACLE_GATED_ISOBMFF_REMUX with a
# predicate over its value.
ORACLE_ALLOW_ISOBMFF_REMUX = frozenset({
    # ftyp
    "QuickTime:MajorBrand",
    "QuickTime:MinorVersion",
    "QuickTime:CompatibleBrands",
    # mvhd, tkhd, mdhd: versions, ids, counters, geometry, timing
    "QuickTime:MovieHeaderVersion",
    "QuickTime:TrackHeaderVersion",
    "QuickTime:MediaHeaderVersion",
    "QuickTime:NextTrackID",
    "QuickTime:TrackID",
    "QuickTime:TrackLayer",
    "QuickTime:TrackVolume",
    "QuickTime:TrackDuration",
    "QuickTime:PreferredRate",
    "QuickTime:PreferredVolume",
    "QuickTime:MatrixStructure",
    "QuickTime:TimeScale",
    "QuickTime:Duration",
    "QuickTime:MediaTimeScale",
    "QuickTime:MediaDuration",
    "QuickTime:PosterTime",
    "QuickTime:PreviewTime",
    "QuickTime:PreviewDuration",
    "QuickTime:SelectionTime",
    "QuickTime:SelectionDuration",
    "QuickTime:CurrentTime",
    # stsd, vmhd, pasp and the bitstream description
    "QuickTime:CompressorID",
    "QuickTime:HandlerType",
    "QuickTime:GraphicsMode",
    "QuickTime:OpColor",
    "QuickTime:BitDepth",
    "QuickTime:ImageWidth",
    "QuickTime:ImageHeight",
    "QuickTime:SourceImageWidth",
    "QuickTime:SourceImageHeight",
    "QuickTime:PixelAspectRatio",
    "QuickTime:XResolution",
    "QuickTime:YResolution",
    "QuickTime:VideoFrameRate",
    "QuickTime:AverageBitrate",
    "QuickTime:MaxBitrate",
    "QuickTime:BufferSize",
    # mdat, which is the picture itself
    "QuickTime:MediaDataOffset",
    "QuickTime:MediaDataSize",
})

ORACLE_GATED_ISOBMFF_REMUX = {
    # Six wall-clock times. The scrubber zeroes them; a real one is a leak.
    "QuickTime:CreateDate": _is_zeroed_isobmff_time,
    "QuickTime:ModifyDate": _is_zeroed_isobmff_time,
    "QuickTime:TrackCreateDate": _is_zeroed_isobmff_time,
    "QuickTime:TrackModifyDate": _is_zeroed_isobmff_time,
    "QuickTime:MediaCreateDate": _is_zeroed_isobmff_time,
    "QuickTime:MediaModifyDate": _is_zeroed_isobmff_time,
    # Three free strings that survive the remux.
    "QuickTime:HandlerDescription": _is_neutral_handler_description,
    "QuickTime:HandlerVendorID": _is_neutral_handler_vendor,
    "QuickTime:MediaLanguageCode": _is_undetermined_language,
}

# HEIC has NO allowlist here, on purpose.
#
# MEASURED 2026-09-06 with the current exiftool engine, on a copy of the real
# HEIC named by MOBILE_HEIC_SOURCES below: MetadataScrubber.sanitize_file()
# returns status 'error'. The ICC profile survives `exiftool -all=` on HEIC, and
# verify.py finds two of its strings in the output bytes: 'Adobe RGB (1998)' and
# 'Copyright 2000 Adobe Systems Incorporated'.
#
# Writing an allowlist that turned that green would manufacture exactly the
# false confidence Part 3.3 warns about. Phase 2 owns HEIC. Until an engine can
# clean it, there is nothing honest to allow.


def assert_oracle_sees_nothing(path: str, allow=(), gated=None, note: str = ""):
    """
    The build-breaking assertion of Part 3.3.

    `allow` and `gated` are PARAMETERS rather than module-level defaults, on
    purpose: a shared implicit allowlist is how a blind spot spreads from the
    one format that needed it to every format that did not. Trap 11, restated.
    Pass one of the ORACLE_ALLOW_* / ORACLE_GATED_* constants by name.

    This assertion is NECESSARY AND NOT SUFFICIENT. Part 3.3: a JPEG carrying a
    motion photo trailer passes it while carrying a whole MP4. Pair it with the
    structural assertions of Part 3.2 and with a byte search. It is never the
    only check on an output.
    """
    allowed = frozenset(allow)
    predicates = dict(gated or {})
    metadata = oracle_metadata(path)

    from metascrub.scrubber import _real_tags

    survivors = []
    for key in _real_tags(metadata):
        if key in allowed:
            continue
        predicate = predicates.get(key)
        if predicate is not None and predicate(metadata.get(key)):
            continue
        survivors.append(f"{key}={metadata.get(key)!r}")

    assert not survivors, (
        f"exiftool still sees {len(survivors)} tag(s) in an output that was "
        f"called clean{(': ' + note) if note else ''}\n  "
        + "\n  ".join(survivors)
        + "\nEither the engine left them or the allowlist is wrong. Adding a "
        "name to an allowlist is adding a name nobody checks again: measure "
        "first, and land the overcorrection test in the same commit."
    )



def _exiftool_write(path: str, **tags: str) -> None:
    """
    Write fixture tags through the same configured exiftool session the tool
    uses. Invoking the exiftool binary directly through subprocess cannot handle
    non-ASCII paths on Windows: Python hands the path over in the ANSI codepage
    and exiftool answers "Wildcards don't work in the directory specification /
    No matching files". That is a fixture limitation, not a tool limitation, and
    routing through the session keeps it from masquerading as one.
    """
    from metascrub import exif_io
    session = exif_io.session()
    args = [f"-{key}={value}" for key, value in tags.items()]
    session.execute(*args, "-overwrite_original", path)


# IMAGE FIXTURES

def _blank_image(path: str, fmt: str, size=(32, 32)) -> None:
    from PIL import Image
    Image.new("RGB", size, (120, 40, 40)).save(path, format=fmt)


def make_image(tmp_path, ext: str, fmt: str):
    """Create an image of the given format carrying a sentinel in its metadata."""
    path = str(tmp_path / f"fixture{ext}")
    _blank_image(path, fmt)
    value = sentinel(ext.lstrip("."))
    # Artist and Copyright are writable across every image container handled
    # here; Comment is not, so it is not relied on.
    _exiftool_write(path, Artist=value, Copyright=value)
    if not raw_contains(path, value):
        pytest.skip(f"exiftool could not store a sentinel in {ext}; fixture unusable")
    return path, value


# PDF

def make_pdf(tmp_path):
    import pikepdf
    path = str(tmp_path / "fixture.pdf")
    value = sentinel("pdf")
    pdf = pikepdf.new()
    pdf.add_blank_page(page_size=(200, 200))
    with pdf.open_metadata() as meta:
        meta["dc:title"] = value
        meta["dc:creator"] = [value]
    pdf.docinfo["/Author"] = value
    pdf.docinfo["/Title"] = value
    pdf.docinfo["/Producer"] = value
    pdf.save(path)
    assert raw_contains(path, value), "pdf fixture did not store the sentinel"
    return path, value


# OOXML

def make_docx(tmp_path):
    import docx
    path = str(tmp_path / "fixture.docx")
    value = sentinel("docx")
    document = docx.Document()
    document.add_paragraph("visible body text that must survive")
    props = document.core_properties
    props.author = value
    props.last_modified_by = value
    props.title = value
    props.comments = value
    document.save(path)
    assert zip_contains(path, value), "docx fixture did not store the sentinel"
    return path, value


def make_xlsx(tmp_path):
    import openpyxl
    path = str(tmp_path / "fixture.xlsx")
    value = sentinel("xlsx")
    workbook = openpyxl.Workbook()
    workbook.active["A1"] = "visible cell value"
    workbook.properties.creator = value
    workbook.properties.lastModifiedBy = value
    workbook.properties.title = value
    workbook.save(path)
    assert zip_contains(path, value), "xlsx fixture did not store the sentinel"
    return path, value


def make_pptx(tmp_path):
    import pptx
    path = str(tmp_path / "fixture.pptx")
    value = sentinel("pptx")
    presentation = pptx.Presentation()
    presentation.slides.add_slide(presentation.slide_layouts[6])
    props = presentation.core_properties
    props.author = value
    props.last_modified_by = value
    props.title = value
    presentation.save(path)
    assert zip_contains(path, value), "pptx fixture did not store the sentinel"
    return path, value


# OPENDOCUMENT
#
# Two routes, and the difference between them is the point. The hand-built
# package needs nothing but the stdlib, so it never skips and never varies by
# platform, and it can carry a chosen printer name, a populated meta:template
# href and an officeooo:rsid, none of which LibreOffice can be asked for. The
# LibreOffice route is the control on it: a hand-built fixture proves the engine
# handles a file the TEST wrote and nothing more.

ODF_KINDS = [".odt", ".ott", ".ods", ".ots", ".odp", ".otp", ".odg", ".otg"]

# The subset LibreOffice has an export filter for. It will not produce a genuine
# Draw document from a Writer, Calc or Impress seed: measured 2026-09-04,
# `--convert-to odg` and `--convert-to odg:draw8` on a .pptx both wrote a
# package whose mimetype member says ...opendocument.presentation, and libmagic
# calls the result an OpenDocument Presentation. The hand-built .odg is the
# genuine Draw document here; libmagic calls it OpenDocument Drawing.
ODF_LIBREOFFICE_KINDS = [".odt", ".ott", ".ods", ".odp"]


def make_odf(tmp_path, ext: str):
    """Hand-built ODF package carrying the whole sentinel family."""
    path = str(tmp_path / f"fixture{ext}")
    value = sentinel(ext.lstrip("."))
    build_odf.build_odf(ext, path, value)
    assert zip_contains(path, value), f"{ext} fixture did not store the sentinel"
    return path, value


_ODF_LO_CACHE = {}
_ODF_LO_CACHE_DIR = None


def _odf_lo_cache_dir():
    global _ODF_LO_CACHE_DIR
    if _ODF_LO_CACHE_DIR is None:
        _ODF_LO_CACHE_DIR = tempfile.mkdtemp(prefix="metascrub-odf-")
        atexit.register(shutil.rmtree, _ODF_LO_CACHE_DIR, True)
    return _ODF_LO_CACHE_DIR


def make_odf_from_libreoffice(tmp_path, ext: str):
    """
    Return (path, sentinel) for an ODF document LibreOffice actually wrote.

    Skips rather than fails when LibreOffice is absent or the conversion did not
    produce a package. Cached for the session and copied per test, because each
    conversion costs a LibreOffice start.
    """
    if not HAVE_LIBREOFFICE:
        pytest.skip("LibreOffice not available; cannot build a real ODF fixture")
    value = sentinel("lo" + ext.lstrip("."))
    if ext not in _ODF_LO_CACHE:
        built = build_odf.libreoffice_odf(
            ext, _odf_lo_cache_dir(), value,
            build_ole2.find_soffice(), build_ole2.convert,
        )
        if built is None:
            pytest.skip(f"LibreOffice did not produce an ODF package for {ext}")
        _ODF_LO_CACHE[ext] = built
    path = str(tmp_path / f"lofixture{ext}")
    shutil.copyfile(_ODF_LO_CACHE[ext], path)
    assert zip_contains(path, value), f"{ext} LibreOffice fixture lost the sentinel"
    return path, value


# SVG
#
# Hand-written, entirely here. SVG is text, so there is no external tool to
# shell out to, nothing to skip and no platform variance. The one exception is
# the embedded JPEG, which is built with Pillow and stamped through the same
# exiftool session every other fixture uses.
#
# Every string in this builder is a RAW string. The export-filename carrier is
# an absolute Windows path, and "C:\Users\..." in a normal Python literal is a
# \U escape, which is a syntax error; "\bin" would silently become a backspace.
# test_svg.py byte-scans the generated file for control characters so a future
# edit cannot reintroduce that quietly.


def _embedded_jpeg_data_uri(tmp_path, value: str) -> str:
    """
    A real JPEG carrying real EXIF, base64ed into a data: URI.

    This is the fixture for the hole that makes SVG PARTIAL. The sentinel goes
    in as Artist and Copyright, and a GPS coordinate goes in beside it, because
    the point being tested is that an SVG can carry a camera's location while
    every check this tool makes reports the file clean.
    """
    import base64

    from PIL import Image

    jpeg = str(tmp_path / "embedded.jpg")
    Image.new("RGB", (32, 32), (10, 90, 160)).save(jpeg, format="JPEG")
    _exiftool_write(jpeg, Artist=value, Copyright=value, GPSLatitude="40.7128")
    if not raw_contains(jpeg, value):
        pytest.skip("exiftool could not stamp the embedded JPEG; fixture unusable")
    with open(jpeg, "rb") as fh:
        encoded = base64.b64encode(fh.read()).decode("ascii")
    os.unlink(jpeg)
    return "data:image/jpeg;base64," + encoded


def make_svg(tmp_path):
    """
    Return (path, sentinel) for an Inkscape-shaped SVG carrying every carrier
    this engine handles, plus the two it deliberately does not.

    The returned sentinel is the one that MUST be gone afterwards. The three
    that must survive (body text, a nested accessibility title, the drawing
    itself) and the two that are known to survive (the file:/// username and the
    embedded JPEG's EXIF) carry their own distinct sentinels, so no assertion
    can pass by accident on the wrong one.
    """
    path = str(tmp_path / "fixture.svg")
    value = sentinel("svg")             # removed: docname, dc:*, comment, export path
    user = sentinel("svguser")          # survives: inside a file:/// href
    embedded = sentinel("svgembedded")  # survives: EXIF inside a base64 JPEG
    body = sentinel("svgbody")          # must survive: <text> content
    nested = sentinel("svgnested")      # must survive: a per-shape <title>

    data_uri = _embedded_jpeg_data_uri(tmp_path, embedded)

    text = "".join([
        r'<?xml version="1.0" encoding="UTF-8" standalone="no"?>' "\n",
        r'<!-- Created with Inkscape. ' + value + r' -->' "\n",
        r'<svg' "\n",
        r'   xmlns:dc="http://purl.org/dc/elements/1.1/"' "\n",
        r'   xmlns:cc="http://creativecommons.org/ns#"' "\n",
        r'   xmlns:rdf="http://www.w3.org/1999/02/22-rdf-syntax-ns#"' "\n",
        r'   xmlns="http://www.w3.org/2000/svg"' "\n",
        r'   xmlns:sodipodi="http://sodipodi.sourceforge.net/DTD/sodipodi-0.0.dtd"' "\n",
        r'   xmlns:inkscape="http://www.inkscape.org/namespaces/inkscape"' "\n",
        r'   xmlns:xlink="http://www.w3.org/1999/xlink"' "\n",
        r'   width="744" height="1052" viewBox="0 0 744 1052"' "\n",
        r'   preserveAspectRatio="xMidYMid meet" version="1.1" id="svgroot"' "\n",
        r'   sodipodi:docname="' + value + r'.svg"' "\n",
        r'   inkscape:version="1.3.2 (091e20e, 2023-11-25)"' "\n",
        # A raw string cannot END in a single backslash, and r'...\\' would put
        # TWO of them in the path, which is a different path from the one under
        # test. The separator is spliced in explicitly instead.
        r'   inkscape:export-filename="C:\Users' + "\\" + value + r'\Desktop\out.png">' "\n",
        r'  <title id="roottitle">' + value + r'</title>' "\n",
        r'  <desc id="rootdesc">' + value + r'</desc>' "\n",
        r'  <sodipodi:namedview id="namedview" pagecolor="#ffffff"' "\n",
        r'     inkscape:zoom="0.35" inkscape:cx="372" inkscape:cy="526"' "\n",
        r'     inkscape:window-x="120" inkscape:window-y="64"' "\n",
        r'     inkscape:window-width="1920" inkscape:window-height="1017"' "\n",
        r'     inkscape:current-layer="layer1" />' "\n",
        r'  <metadata id="metadata7">' "\n",
        r'    <rdf:RDF><cc:Work rdf:about="">' "\n",
        r'      <dc:format>image/svg+xml</dc:format>' "\n",
        r'      <dc:title>' + value + r'</dc:title>' "\n",
        r'      <dc:creator><cc:Agent><dc:title>' + value + r'</dc:title></cc:Agent></dc:creator>' "\n",
        r'      <dc:rights><cc:Agent><dc:title>' + value + r'</dc:title></cc:Agent></dc:rights>' "\n",
        r'      <dc:description>' + value + r'</dc:description>' "\n",
        r'      <dc:date>2026-09-04</dc:date>' "\n",
        r'    </cc:Work></rdf:RDF>' "\n",
        r'  </metadata>' "\n",
        r'  <defs>' "\n",
        r'    <linearGradient id="gradient3757">' "\n",
        r'      <stop offset="0" stop-color="#ff0000"/><stop offset="1" stop-color="#0000ff"/>' "\n",
        r'    </linearGradient>' "\n",
        r'  </defs>' "\n",
        r'  <style><![CDATA[ .keep { stroke: #00ff00; } ]]></style>' "\n",
        r'  <g inkscape:label="Layer 1" inkscape:groupmode="layer" id="layer1">' "\n",
        r'    <title id="shapetitle">' + nested + r'</title>' "\n",
        r'    <rect id="rect1" class="keep" x="10" y="10" width="100" height="50"' "\n",
        r'       style="fill:url(#gradient3757)" sodipodi:nodetypes="cccc" />' "\n",
        r'    <use id="use1" xlink:href="#rect1" transform="translate(0,80)" />' "\n",
        r'    <text id="text1" x="20" y="200">' + body + r'</text>' "\n",
        r'    <image id="linked" x="0" y="300" width="64" height="64"' "\n",
        r'       sodipodi:absref="C:\Users' + "\\" + user + r'\Pictures\logo.png"' "\n",
        r'       xlink:href="file:///C:/Users/' + user + r'/Pictures/logo.png" />' "\n",
        r'    <image id="embedded" x="0" y="400" width="32" height="32"' "\n",
        r'       xlink:href="' + data_uri + r'" />' "\n",
        r'  </g>' "\n",
        r'</svg>' "\n",
    ])
    # newline="" so a Windows run does not rewrite every line ending in the
    # fixture and hand the line-ending test a file that was never LF-only.
    with open(path, "w", encoding="utf-8", newline="") as fh:
        fh.write(text)

    assert raw_contains(path, value), "svg fixture did not store the sentinel"
    return path, value


# AUDIO AND VIDEO

def _ffmpeg(args) -> None:
    subprocess.run(["ffmpeg", "-y", "-loglevel", "error"] + args,
                   check=True, capture_output=True)


# ffmpeg picks its output muxer from the filename, and binds none to these four
# extensions, so a fixture named with one of them cannot be written without an
# explicit -f either. This is the same wall av_engine.py hits.
#
# Deliberately a SECOND, independent copy of the engine's MUXER_FOR_EXT rather
# than an import of it. A fixture built through the engine's own map inherits
# the engine's mistakes: point .qt at the mp4 muxer in both places and the round
# trip passes while the tool quietly rewrites QuickTime files as MP4.
# tests/test_av_muxer.py asserts the two maps agree, and separately asserts the
# output's ftyp brand still matches the input's, so drift is caught without the
# fixture depending on the thing under test.
FIXTURE_MUXER = {".qt": "mov", ".mqv": "mov", ".lrv": "mp4", ".f4a": "mp4"}


def _fixture_muxer_args(ext: str):
    muxer = FIXTURE_MUXER.get((ext or "").lower())
    return ["-f", muxer] if muxer else []


def make_video(tmp_path, ext: str = ".mp4"):
    path = str(tmp_path / f"fixture{ext}")
    value = sentinel(ext.lstrip("."))
    _ffmpeg([
        "-f", "lavfi", "-i", "testsrc=duration=1:size=64x64:rate=10",
        "-metadata", f"title={value}",
        "-metadata", f"comment={value}",
        "-metadata", f"artist={value}",
        "-pix_fmt", "yuv420p",
    ] + _fixture_muxer_args(ext) + [path])
    assert raw_contains(path, value), f"{ext} fixture did not store the sentinel"
    return path, value


def make_audio(tmp_path, ext: str = ".mp3"):
    path = str(tmp_path / f"fixture{ext}")
    value = sentinel(ext.lstrip("."))
    _ffmpeg([
        "-f", "lavfi", "-i", "sine=frequency=440:duration=1",
        "-metadata", f"title={value}",
        "-metadata", f"artist={value}",
    ] + _fixture_muxer_args(ext) + [path])
    assert raw_contains(path, value), f"{ext} fixture did not store the sentinel"
    return path, value


def make_transport_stream(tmp_path, ext: str = ".ts"):
    """
    MPEG transport stream. Genuinely a different container from MP4 and
    Matroska, so it gets a real fixture rather than claiming coverage through
    one of them.
    """
    path = str(tmp_path / f"fixture{ext}")
    value = sentinel(ext.lstrip("."))
    _ffmpeg([
        "-f", "lavfi", "-i", "testsrc=size=64x64:rate=5:duration=1",
        "-pix_fmt", "yuv420p",
        "-metadata", f"title={value}",
        "-metadata", f"artist={value}",
        path,
    ])
    assert raw_contains(path, value), f"{ext} fixture did not store the sentinel"
    return path, value


def make_aiff(tmp_path, ext: str = ".aiff"):
    """AIFF, which is IFF-chunked rather than ISO-BMFF or Ogg-framed."""
    path = str(tmp_path / f"fixture{ext}")
    value = sentinel(ext.lstrip("."))
    _ffmpeg([
        "-f", "lavfi", "-i", "sine=frequency=440:duration=1",
        "-metadata", f"title={value}",
        "-metadata", f"artist={value}",
        path,
    ])
    assert raw_contains(path, value), f"{ext} fixture did not store the sentinel"
    return path, value


# LEGACY OLE2
#
# Cached for the whole session and copied per test. Every other builder here is
# cheap enough to rerun, but this one starts LibreOffice, which measured about
# four seconds per conversion on a warm profile. ALL_KINDS drives roughly ten
# tests per format, so rebuilding per test would have added minutes to the suite
# for three formats whose bytes are identical every time.

_OLE2_CACHE = {}
_OLE2_CACHE_DIR = None


def _ole2_cache_dir():
    global _OLE2_CACHE_DIR
    if _OLE2_CACHE_DIR is None:
        _OLE2_CACHE_DIR = tempfile.mkdtemp(prefix="metascrub-ole2-")
        atexit.register(shutil.rmtree, _OLE2_CACHE_DIR, True)
    return _OLE2_CACHE_DIR


def make_ole2(tmp_path, ext: str):
    """
    Return (path, sentinel) for a legacy .doc/.xls/.ppt built by LibreOffice.

    Skips rather than fails when LibreOffice is absent or its conversion did not
    produce a compound file. A machine without the converter says nothing about
    whether the engine works, and a failure here would claim it did.
    """
    if not HAVE_LIBREOFFICE:
        pytest.skip("LibreOffice not available; cannot build a legacy OLE2 fixture")
    value = sentinel(ext.lstrip("."))
    if ext not in _OLE2_CACHE:
        built = build_ole2.build_ole2(ext, _ole2_cache_dir(), value)
        if built is None:
            pytest.skip(f"LibreOffice did not produce a compound file for {ext}")
        _OLE2_CACHE[ext] = built
    path = str(tmp_path / f"fixture{ext}")
    shutil.copyfile(_OLE2_CACHE[ext], path)
    # Asserted, not assumed. A conversion that dropped the properties would
    # otherwise give every OLE2 test a file with nothing to remove, and they
    # would all pass.
    assert raw_contains(path, value), f"{ext} fixture did not store the sentinel"
    return path, value



# MOBILE MEDIA FIXTURES
#
# Phase 0 of the Android media build. docs/ANDROID-MEDIA-BUILD.md Part 3.4.
#
# Five kinds: .jpg, .png, .webp, .mp4 and .heic. They follow the same four-step
# contract as every other factory in this file: build with something that is not
# the engine under test, stamp a sentinel, assert the sentinel is really there
# before the test runs, return (path, primary_sentinel).
#
# EVERY ONE OF THESE EXCEPT THE HEIC CONTAINER IS SYNTHETIC, AND EACH DOCSTRING
# SAYS HOW IT DIFFERS FROM REAL DEVICE OUTPUT. No real Pixel or Samsung media
# was available on this machine. A synthetic fixture is honest about what it
# proves: it proves the engine removes the carriers the fixture has, and it
# proves nothing at all about carriers only a real device writes. Part 3.4 asks
# for real device output for at least one of each family, and that is still
# outstanding: see the note above MOBILE_HEIC_SOURCES.
#
# Following make_svg's precedent, a fixture that carries more than one sentinel
# returns only the one that MUST be gone. The others are recomputable, because
# sentinel() is a pure function of its tag: a test that wants the XMP sentinel
# of the JPEG fixture asks for sentinel("mobilejpgxmp").
#
# NOT ADDED TO ALL_KINDS, deliberately. ALL_KINDS is the parametrize source for
# test_removal, test_failopen, test_unparseable and test_crossproduct, and it is
# keyed on EXTENSION: .jpg, .png, .webp and .mp4 are already in there through
# make_image and make_video. Adding a second builder for the same extension
# would either collide with those rows or need a non-extension key, which
# capabilities.deferral_for() and the coverage gate both assume cannot happen.
# These live in their own registry until Phase 1 owns the extensions outright.

# The one real file available on this machine. Not device output, and the
# difference matters, so it is measured here rather than repeated:
#
#   MEASURED 2026-09-06 with exiftool 13.29 on DA-1p.heic: ftyp brand `heic`,
#   compatible brands heic/mif1, a real HEVC still-image item, 596x842.
#   It carries NO EXIF at all: no Make, no Model, no Software, no
#   DateTimeOriginal, no GPS, no XMP. The only identity-shaped strings in it
#   belong to an Adobe RGB (1998) ICC profile, whose header names
#   'Apple Computer Inc.' as the ICC primary platform. That is a field of the
#   1998 Adobe profile and is present in any file carrying it.
#
#   So: this is a REAL HEIC CONTAINER and it is NOT demonstrably iPhone output.
#   596x842 is a page at 72 dpi, not a camera frame; an iPhone 11 shoots
#   4032x3024. A note handed to this session described it as "a genuine
#   iPhone 11 HEIC"; that is not what the bytes say, and the difference is the
#   whole value of a real fixture, so it is corrected here rather than repeated.
#
# What it is still worth: a genuine heic/mif1 box tree, with real iloc, iinf,
# iprp and pitm structures that no synthesiser here produces. The device-shaped
# EXIF and GPS are written on top by make_mobile_heic, and are synthetic.
#
# Part 3.4's requirement for real device media is therefore NOT met by this
# file, for any of the five kinds.
MOBILE_HEIC_SOURCES = (
    os.environ.get("METASCRUB_HEIC_SOURCE", ""),
    r"<volume>/Projects/AI-ML/unstructured/example-docs/img/DA-1p.heic",
)


def _mobile_heic_source():
    """The first readable candidate HEIC, or None."""
    for candidate in MOBILE_HEIC_SOURCES:
        if candidate and os.path.isfile(candidate):
            return candidate
    return None


# THE GUARD
#
# Two tables, kept OUTSIDE the factory bodies on purpose. A factory that quietly
# stops writing a carrier still has to get past the table that says it writes
# one, and the table is the thing a reviewer reads. This is the same reasoning
# as the comment above the OLE2 assertion: "Asserted, not assumed. A conversion
# that dropped the properties would otherwise give every test a file with
# nothing to remove, and they would all pass."
#
# It is not hypothetical here, and the guard caught it while being written.
# MEASURED 2026-09-06: exif_io's shared session passes `-n` in its common args,
# and under `-n` exiftool refuses the human-readable GPS form. Given
#   -UserData:Comment=CV -UserData:GPSCoordinates="37.4220, -122.0841"
# exiftool prints "Warning: Error converting value for UserData:GPSCoordinates
# (ValueConvInv)", writes the Comment, reports "1 image files updated" and
# EXITS ZERO. The (c)xyz atom is not in the file. _exiftool_write cannot see
# that: pyexiftool only raises on a non-zero status. Every byte-findable
# sentinel was still present, so a sentinel-only check would have called the
# fixture good and every GPS assertion downstream would have been vacuous.
# The value form `-n` accepts is space separated: "37.422 -122.0841 0".
#
# _MOBILE_SENTINELS names the byte-findable sentinel carriers each factory
# promises. _MOBILE_TAG_CARRIERS names the identity carriers that are NOT
# byte-findable as a sentinel, so they can only be confirmed by reading the
# INPUT with the oracle. Reading the input is a precondition check on the
# fixture and not a verification of removal, so it is not the circular check
# traps 2 and 12 forbid: nothing here is being asked whether a scrub worked.
#
# GPS is the reason the second table exists. exiftool -n returns 37.422, which
# is five characters after the separators are stripped, under verify._MIN_NEEDLE
# of 8 and all digits, so verify.meaningful_values() will never make a needle of
# it and raw_contains() cannot find it in the binary EXIF rationals either. For
# a photo scrubber that is the single most important value in the file. Part 3.2
# is blunt about it: for GPS, the structural assertion is not belt and braces,
# it is the only instrument there is.
_MOBILE_SENTINELS = {
    ".jpg": ("exif", "xmp", "com"),
    ".png": ("text", "xmp"),
    ".webp": ("exif", "xmp"),
    ".mp4": ("udta", "keys"),
    ".heic": ("exif", "xmp"),
}

_MOBILE_TAG_CARRIERS = {
    ".jpg": ("EXIF:Make", "EXIF:Model", "EXIF:DateTimeOriginal",
             "EXIF:GPSLatitude", "EXIF:GPSLongitude"),
    # An Android screenshot carries no EXIF and no GPS. Its identity carriers
    # are the tEXt chunks and the filename, so the tag carrier here is the
    # creation time and nothing else.
    ".png": ("PNG:CreationTime",),
    ".webp": ("EXIF:Make", "EXIF:Model", "EXIF:GPSLatitude", "EXIF:GPSLongitude"),
    ".mp4": ("QuickTime:Model", "QuickTime:GPSCoordinates"),
    ".heic": ("EXIF:Make", "EXIF:Model", "EXIF:DateTimeOriginal",
              "EXIF:GPSLatitude", "EXIF:GPSLongitude"),
}


def _finish_mobile_fixture(kind: str, path: str, sentinels: dict):
    """
    Prove a mobile fixture carries what its table says it carries.

    Fails rather than skips. A fixture that cannot be stamped is a broken
    measuring instrument, and a skipped test is how a broken instrument stays
    green: the same species as the 2026-09-04 empty-DEFERRED finding, where a
    loop over an empty container was the quietest possible pass.
    """
    declared = set(_MOBILE_SENTINELS[kind])
    presented = set(sentinels)
    if presented != declared:
        pytest.fail(
            f"{kind} fixture factory presented sentinel carriers {sorted(presented)} "
            f"but _MOBILE_SENTINELS declares {sorted(declared)}. One of the two "
            "was changed without the other."
        )

    missing = sorted(
        name for name, value in sentinels.items() if not contains_anywhere(path, value)
    )
    if missing:
        pytest.fail(
            f"{kind} fixture did not store the sentinel for {missing}. The "
            "fixture proves nothing in this state: a scrub of it would pass by "
            "having nothing to remove."
        )

    metadata = oracle_metadata(path)
    absent = [tag for tag in _MOBILE_TAG_CARRIERS[kind] if tag not in metadata]
    if absent:
        pytest.fail(
            f"{kind} fixture is missing the identity carriers {absent}, which "
            "are not byte-findable and so can only be confirmed by reading the "
            "input. GPS in particular can never be a sentinel: see the note "
            "above this guard."
        )
    return path


def make_mobile_jpeg(tmp_path):
    """
    SYNTHETIC. A Pillow JPEG with device-shaped EXIF, XMP and a COM marker.

    HOW IT DIFFERS FROM REAL DEVICE OUTPUT, all of which matter to Phase 1:
      - no MakerNotes, so the maker-note carriers are untested here;
      - no motion photo trailer past EOI, which Part 2.1 calls the
        highest-severity carrier and Part 3.3 measured the oracle blind to;
      - no C2PA/JUMBF manifest in APP11;
      - no EXIF thumbnail, so the pre-edit thumbnail leak is untested here;
      - Pillow's quantization tables and Huffman tables, not a phone encoder's,
        so nothing here exercises the Tier 4 and 5 fingerprinting surface;
      - a 96x72 frame, not a 4080x3072 one.

    Three sentinels: EXIF, XMP and the COM marker. The returned one is the EXIF
    sentinel, which is the one that MUST be gone.
    """
    from PIL import Image

    path = str(tmp_path / "mobile.jpg")
    Image.new("RGB", (96, 72), (120, 40, 40)).save(path, format="JPEG", quality=90)

    exif_value = sentinel("mobilejpg")
    xmp_value = sentinel("mobilejpgxmp")
    com_value = sentinel("mobilejpgcom")

    _exiftool_write(
        path,
        Make="Google",
        Model="Pixel 7",
        Software="HDR+ 1.0.540750569zd",
        Artist=exif_value,
        Copyright=exif_value,
        UserComment=exif_value,
        Comment=com_value,
        DateTimeOriginal="2026:03:14 09:26:53",
        OffsetTimeOriginal="-07:00",
        GPSLatitude="37.4220",
        GPSLatitudeRef="N",
        GPSLongitude="-122.0841",
        GPSLongitudeRef="W",
        **{"XMP-dc:Description": xmp_value},
    )
    _finish_mobile_fixture(
        ".jpg", path, {"exif": exif_value, "xmp": xmp_value, "com": com_value}
    )
    return path, exif_value


def make_mobile_png(tmp_path):
    """
    SYNTHETIC. A Pillow PNG shaped like an Android screenshot: tEXt chunks and
    XMP, no EXIF and no GPS, which is what a screenshot actually carries.

    HOW IT DIFFERS FROM REAL DEVICE OUTPUT:
      - a real screenshot's strongest identifier is its FILENAME
        (Screenshot_20260314-092653_Signal.png names the app and the second it
        was taken) and this fixture's name carries none of that, because the
        filename is Part 3's problem and not the engine's;
      - Pillow writes no zTXt, so the compressed-carrier case that Part 3.2
        calls invisible to a byte search is not covered here;
      - no trailing bytes past IEND;
      - 108x192, not a phone's screen resolution.

    Two sentinels: the tEXt chunks and XMP. The returned one is the tEXt one.
    """
    from PIL import Image

    path = str(tmp_path / "mobile.png")
    Image.new("RGB", (108, 192), (30, 90, 160)).save(path, format="PNG")

    text_value = sentinel("mobilepng")
    xmp_value = sentinel("mobilepngxmp")

    _exiftool_write(
        path,
        **{
            "PNG:Software": text_value,
            "PNG:Author": text_value,
            "PNG:Comment": text_value,
            "PNG:CreationTime": "2026:03:14 09:26:53",
            "XMP-dc:Description": xmp_value,
        },
    )
    _finish_mobile_fixture(".png", path, {"text": text_value, "xmp": xmp_value})
    return path, text_value


def make_mobile_webp(tmp_path):
    """
    SYNTHETIC. A Pillow WebP carrying an EXIF chunk with device tags and GPS,
    plus an XMP chunk.

    HOW IT DIFFERS FROM REAL DEVICE OUTPUT:
      - lossy VP8 written by libwebp through Pillow, not by a phone;
      - no unknown chunk hidden inside the RIFF and no bytes past the declared
        RIFF size, which are the two WebP carriers Part 2.3 cares about most and
        Part 3.3 measured exiftool silent on;
      - the VP8X flag bits are whatever exiftool set when it added the chunks,
        which is exactly why Part 2.3 says to assert over the chunk inventory
        instead of over those flags.

    Two sentinels: EXIF and XMP. The returned one is the EXIF one.
    """
    from PIL import Image

    path = str(tmp_path / "mobile.webp")
    Image.new("RGB", (96, 72), (60, 140, 60)).save(path, format="WEBP")

    exif_value = sentinel("mobilewebp")
    xmp_value = sentinel("mobilewebpxmp")

    _exiftool_write(
        path,
        Make="Google",
        Model="Pixel 7",
        Artist=exif_value,
        Copyright=exif_value,
        GPSLatitude="37.4220",
        GPSLatitudeRef="N",
        GPSLongitude="-122.0841",
        GPSLongitudeRef="W",
        **{"XMP-dc:Description": xmp_value},
    )
    _finish_mobile_fixture(".webp", path, {"exif": exif_value, "xmp": xmp_value})
    return path, exif_value


def make_mobile_mp4(tmp_path):
    """
    SYNTHETIC. An ffmpeg MP4 with a populated udta, including the (c)xyz GPS
    atom a Pixel writes, plus a Keys/meta entry.

    The GPS value is space separated on purpose. MEASURED 2026-09-06: the shared
    session writes with `-n`, and under `-n` exiftool rejects the readable form
    "37.4220, -122.0841" with a ValueConvInv warning, writes every OTHER tag in
    the same call, and still exits zero. See the note above the guard; the guard
    is what caught it.

    HOW IT DIFFERS FROM REAL DEVICE OUTPUT:
      - ffmpeg's box layout, not a phone's: no `free` padding, no fragmented
        moov, no sample-level metadata track;
      - no SEI user-data NAL in the bitstream, which Part 5 phase 3 owns;
      - no uuid box and no vendor `mett`/`meta` timed-metadata track, both of
        which real Android video carries;
      - one second of testsrc at 64x64, not 4K H.265.

    Two sentinels: the udta strings and the Keys entry. The returned one is the
    udta one.
    """
    if not HAVE_FFMPEG:
        pytest.skip("ffmpeg not available; cannot build a mobile MP4 fixture")

    path = str(tmp_path / "mobile.mp4")
    _ffmpeg([
        "-f", "lavfi", "-i", "testsrc=duration=1:size=64x64:rate=10",
        "-pix_fmt", "yuv420p", path,
    ])

    udta_value = sentinel("mobilemp4")
    keys_value = sentinel("mobilemp4keys")

    _exiftool_write(
        path,
        **{
            "Keys:Description": keys_value,
            "UserData:Comment": udta_value,
            "UserData:Title": udta_value,
            "UserData:Artist": udta_value,
            "UserData:Model": "Pixel 7",
            # Space separated, not "37.4220, -122.0841". See the docstring.
            "UserData:GPSCoordinates": "37.422 -122.0841 0",
        },
    )
    _finish_mobile_fixture(".mp4", path, {"udta": udta_value, "keys": keys_value})
    return path, udta_value


def make_mobile_heic(tmp_path):
    """
    A REAL HEIC CONTAINER with SYNTHETIC device metadata written on top.

    The container is a copy of the file named by MOBILE_HEIC_SOURCES, which is a
    genuine heic/mif1 HEVC still image with a real iloc/iinf/iprp/pitm tree. The
    original is copied and never modified.

    The EXIF, GPS and XMP are written here by exiftool and are synthetic. See
    the measured note above MOBILE_HEIC_SOURCES: the source carries no EXIF at
    all, so it is a real container and NOT real device output.

    HOW IT DIFFERS FROM REAL DEVICE OUTPUT:
      - the EXIF item was added by exiftool, so its item layout is exiftool's
        and not Apple's;
      - no depth map, no auxiliary image items, no Apple maker notes, no HDR
        gain map, none of which any synthesiser here can produce;
      - no `mdat`-embedded thumbnail item pair;
      - 596x842, a page-shaped image, not a camera frame.

    Skips, rather than fails, when no source file is present. A machine without
    that file says nothing about whether an engine works, which is the same
    reasoning make_ole2 gives for LibreOffice. The exiftool policy above is the
    opposite and deliberately so: exiftool absence breaks the measurement
    itself, a missing sample file only narrows what can be measured.

    MEASURED 2026-09-06, and it is why there is no ORACLE_ALLOW_HEIC: running
    the CURRENT engine over this fixture returns status 'error', because the ICC
    profile survives `exiftool -all=` on HEIC and verify.py finds
    'Adobe RGB (1998)' and 'Copyright 2000 Adobe Systems Incorporated' in the
    output bytes. HEIC is Phase 2's.

    Two sentinels: EXIF and XMP. The returned one is the EXIF one.
    """
    source = _mobile_heic_source()
    if source is None:
        pytest.skip(
            "no HEIC source file available; set METASCRUB_HEIC_SOURCE to one. "
            f"Tried: {[c for c in MOBILE_HEIC_SOURCES if c]}"
        )

    path = str(tmp_path / "mobile.heic")
    shutil.copyfile(source, path)

    exif_value = sentinel("mobileheic")
    xmp_value = sentinel("mobileheicxmp")

    _exiftool_write(
        path,
        Make="Apple",
        Model="iPhone 11",
        Software=exif_value,
        Artist=exif_value,
        Copyright=exif_value,
        DateTimeOriginal="2026:03:14 09:26:53",
        GPSLatitude="37.4220",
        GPSLatitudeRef="N",
        GPSLongitude="-122.0841",
        GPSLongitudeRef="W",
        **{"XMP-dc:Description": xmp_value},
    )
    _finish_mobile_fixture(".heic", path, {"exif": exif_value, "xmp": xmp_value})
    return path, exif_value


# The mobile registry. Separate from build()/ALL_KINDS for the reason given at
# the top of this section.
MOBILE_KINDS = [".jpg", ".png", ".webp", ".mp4", ".heic"]

_MOBILE_BUILDERS = {
    ".jpg": make_mobile_jpeg,
    ".png": make_mobile_png,
    ".webp": make_mobile_webp,
    ".mp4": make_mobile_mp4,
    ".heic": make_mobile_heic,
}


def build_mobile(kind: str, tmp_path):
    """Return (path, primary_sentinel) for a named mobile fixture kind."""
    try:
        builder = _MOBILE_BUILDERS[kind]
    except KeyError:
        raise AssertionError(f"no mobile fixture builder for {kind}") from None
    return builder(tmp_path)


# Every mobile kind has a builder, a sentinel table and a tag table. Checked at
# import so a kind added to one of the four and not the others cannot sit
# unnoticed until some later test happens to ask for it.
assert set(MOBILE_KINDS) == set(_MOBILE_BUILDERS) == set(_MOBILE_SENTINELS) \
    == set(_MOBILE_TAG_CARRIERS), (
        "MOBILE_KINDS, _MOBILE_BUILDERS, _MOBILE_SENTINELS and "
        "_MOBILE_TAG_CARRIERS disagree about which mobile kinds exist"
    )


@pytest.fixture
def mobile_fixture(tmp_path):
    """
    Factory fixture: `mobile_fixture(".jpg")` -> (path, primary_sentinel).

    Every fixture it hands back has already been through _finish_mobile_fixture,
    so a test never receives one that failed to embed what it claims.
    """
    def _build(kind: str):
        return build_mobile(kind, tmp_path)

    return _build


# BUILDER REGISTRY, used to drive the cross-product tests

IMAGE_FORMATS = [(".jpg", "JPEG"), (".png", "PNG"), (".gif", "GIF"),
                 (".webp", "WEBP"), (".tiff", "TIFF")]


def build(kind: str, tmp_path):
    """Return (path, sentinel) for a named fixture kind."""
    for ext, fmt in IMAGE_FORMATS:
        if kind == ext:
            return make_image(tmp_path, ext, fmt)
    if kind == ".pdf":
        return make_pdf(tmp_path)
    if kind == ".svg":
        return make_svg(tmp_path)
    if kind == ".docx":
        return make_docx(tmp_path)
    if kind == ".xlsx":
        return make_xlsx(tmp_path)
    if kind == ".pptx":
        return make_pptx(tmp_path)
    if kind in ODF_KINDS:
        return make_odf(tmp_path, kind)
    if kind in (".doc", ".xls", ".ppt"):
        return make_ole2(tmp_path, kind)
    if kind in (".mp4", ".mkv", ".mov", ".qt", ".mqv", ".lrv"):
        if not HAVE_FFMPEG:
            pytest.skip("ffmpeg not available")
        return make_video(tmp_path, kind)
    if kind in (".mp3", ".flac", ".m4a", ".f4a"):
        if not HAVE_FFMPEG:
            pytest.skip("ffmpeg not available")
        return make_audio(tmp_path, kind)
    if kind in (".ts", ".m2ts"):
        if not HAVE_FFMPEG:
            pytest.skip("ffmpeg not available")
        return make_transport_stream(tmp_path, kind)
    if kind in (".aiff", ".aif"):
        if not HAVE_FFMPEG:
            pytest.skip("ffmpeg not available")
        return make_aiff(tmp_path, kind)
    raise AssertionError(f"no fixture builder for {kind}")


# Every format the cross-product tests must cover. A format handled by the
# capability table but absent here is caught by test_coverage.py.
ALL_KINDS = [ext for ext, _ in IMAGE_FORMATS] + [
    ".pdf", ".svg", ".docx", ".xlsx", ".pptx", ".doc", ".xls", ".ppt",
    ".mp4", ".mkv", ".mp3", ".flac",
    # OpenDocument. All eight get a real fixture rather than a proxy: the
    # builder is stdlib-only, so a template costs the same as a document and a
    # declared proxy would be a claim where a measurement was available.
] + ODF_KINDS + [
    # Added 2026-09-04. Only the containers that are genuinely distinct get
    # a real fixture; the rest declare a proxy in
    # test_coverage_gate._COVERED_BY.
    ".ts", ".aiff",
    # Added 2026-09-04 with the extension-to-muxer map. Each gets a real fixture
    # rather than a declared proxy, because the thing under test IS the per
    # extension map entry, and a wrong entry is exactly what a proxy hides.
    ".qt", ".mqv", ".lrv", ".f4a",
]


@pytest.fixture(autouse=True)
def _close_exiftool():
    """Stop the shared exiftool process between tests so temp dirs can be removed."""
    yield
    from metascrub import exif_io
    exif_io.close_session()


# TK ROOT

@pytest.fixture(scope="session")
def tk_root():
    """
    One Tk interpreter for the whole session, shared by every test file.

    Two separate problems make this session-scoped rather than per-test:

    - Creating and destroying interpreters repeatedly intermittently fails on
      Windows with "tk wasn't installed properly", and a fixture that skips on
      that failure hides the GUI tests while the suite still reports green.
    - A SECOND live interpreter in the same process fails outright, so any test
      that builds its own tk.Tk() breaks every other GUI test in the run.

    It is a TkinterDnD root when that package is installed, matching what
    gui.main() builds, so the drag-and-drop path is genuinely exercised.
    """
    import tkinter as tk

    from metascrub import gui as gui_module

    try:
        root = gui_module.TkinterDnD.Tk() if gui_module._HAVE_DND else tk.Tk()
    except tk.TclError as exc:  # pragma: no cover - genuinely headless
        pytest.skip(f"no display available: {exc}")
    root.withdraw()
    yield root
    try:
        root.destroy()
    except tk.TclError:
        pass
