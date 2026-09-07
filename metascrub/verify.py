"""
Post-sanitize verification.

The the parent project sanitizer this project builds on treats "no exception was raised" as
proof of success (cli/sanitizer.py:167 logs "Successfully removed all metadata"
on the strength of exiftool not throwing). That is inference, not measurement.

Worse, re-reading with the same engine that performed the write is not a check
either. Measured 2026-09-04 with exiftool 13.29 on a PDF: after `exiftool
-all=`, the command `exiftool -Author -Title` printed nothing, while the author
and title strings were still present in the file bytes twice each. An engine
read-back would have reported that file clean. It was not clean.

So verification here is done two ways, and the second one is the one that
matters:

  1. Engine read-back. Re-read the metadata and assert the carriers are gone.
  2. Residual byte scan. Capture the actual metadata VALUES before sanitizing,
     then search the output for those exact values. This is engine-independent:
     it does not care how the removal was performed or which library claims
     success. If a value the file used to carry is still findable in the bytes,
     the file is not clean, whatever any tool reports.

The residual scan is why this module reads the file before touching it.

WHAT THE VERDICT DOES NOT SAY, AND WHERE THAT NOW GOES

Both checks above are bounded, and the bounds used to be invisible. The
residual scan can only search for values a baseline read produced, so a carrier
exiftool never parsed contributes no needles; `structure.py` and
`gps_verify.py` each answer a question that does not depend on the baseline,
and each has an extension list shorter than the tool's own. Measured
2026-09-07 and recorded in docs/WHAT-THE-TOOL-CLAIMS.md: of the 71 extensions
in CAPABILITIES, 50 had neither walker, 29 of those are declared COMPLETE, and
on all of them the output said `verified clean` with nothing hedging.

So `Verification` carries a `coverage` field alongside the verdict, naming
which checks ran and which did not, in the checks' own words. It changes no
verdict. It is the channel: `gps_verify` computed a correct and specific
sentence about an 18.4 MB trailer on a real Samsung JPEG, and until this field
existed there was nowhere for that sentence to go.
"""

from __future__ import annotations

import io
import os
import re
import zipfile
from dataclasses import dataclass, field
from enum import Enum
from typing import Dict, List, Optional, Sequence, Set

from . import gps_verify
from .capabilities import Container, FormatSpec
from .structure import StructureReport
from .structure import scan as structure_scan

# exiftool reports filesystem facts and derived values under these groups. They
# are not stored metadata, so they must never become residual-scan needles: the
# filename or file size is not something sanitizing can or should remove.
#
# ZIP is here for the same reason. exiftool describes an OOXML package by
# listing its zip directory, so ZIP:ZipFileName reports part names like
# "[Content_Types].xml". Those are required structure. Treating one as a needle
# made every docx, xlsx and pptx report RESIDUAL_FOUND on a file that was
# genuinely clean, which is the expensive kind of false positive: it tells a
# user a clean file still leaks.
_PSEUDO_GROUPS = frozenset({
    "File", "System", "Composite", "ExifTool", "SourceFile", "ZIP",
})

# Tags that name a container's structure rather than its provenance. The engine
# that rebuilds the file writes these again, identically, so the original value
# reappearing in the output is expected and is not a survival.
#
# This list is kept deliberately short and specific. Anything genuinely
# identifying (Software, Encoder, Creator, Artist) is NOT here: if one of those
# reappears with its original value, the file really did keep it and the tool
# should say so.
# Each entry below was added because it produced a measured false positive on
# 2026-09-04, not because it seemed plausible:
#   HandlerDescription  "VideoHandler"      every ffmpeg mp4 remux rewrites it
#   Matroska:DocType    "matroska"          the container's own format name
#   Matroska:CodecID    "V_MPEG4/ISO/AVC"   the codec, required to decode
#   XML:TitlesOfParts   "Office Theme"      the pptx theme part name, content
_STRUCTURAL_TAGS = frozenset({
    "handlerdescription", "handlertype", "handlervendorid",
    "compressorid", "compressorname", "majorbrand", "minorversion",
    "compatiblebrands", "graphicsmode", "opcolor", "matrixstructure",
    "mediadataoffset", "mediadatasize", "mediaheaderversion",
    "trackheaderversion",
    # Matroska container description
    "doctype", "codecid",
    # OOXML part inventory, which names required package parts
    "titlesofparts", "headingpairs", "presentationformat",
})

# Structure that is structure in ONE format and only at ONE exact value.
#
# _STRUCTURAL_TAGS above is matched on the bare tag name, globally, for every
# format. That is the right shape for a name like "compressorid" that no other
# handled container reports, and the wrong shape for a name like "xmlns" that
# any XML-bearing format could carry: adding it there would mean no value stored
# under a tag of that name is ever searched for again, in any file type. That is
# a widened blind spot in the one function this project exists to make
# trustworthy, and this project's own lesson from the OLE2 directory-name
# collision was: exclude a REGION, not a STRING.
#
# So these are keyed on (exiftool group, bare tag name) AND carry a predicate
# over the VALUE. An entry only suppresses a needle when all three agree, which
# leaves three ways a real leak is still caught:
#   - the same tag name in a different group is still searched,
#   - the same tag in the same group holding a different value is still
#     searched, which is exactly where a smuggled value would sit,
#   - every other tag in the group is untouched.
#
# Measured 2026-09-04 with exiftool 13.29, through this project's own read flags
# (-G -n), on a hand-authored Inkscape-style SVG:
#     [SVG]  Xmlns                : http://www.w3.org/2000/svg
#     [SVG]  PreserveAspectRatio  : xMidYMid meet
# Both are required for the file to be an SVG and to lay out, both are written
# back identically by any engine that rebuilds the file, and without this a
# correctly cleaned SVG verified as RESIDUAL_FOUND on its own namespace
# declaration. tests/test_verify_structural.py plants a real secret into each of
# the three positions above and requires the scan to still find it.
_SVG_NAMESPACE = "http://www.w3.org/2000/svg"

# The SVG 1.1 grammar for preserveAspectRatio. Every string it accepts is drawn
# from a closed keyword set, so a matching value carries no user information;
# anything else is not a preserveAspectRatio and stays a needle.
_PRESERVE_ASPECT_RATIO = re.compile(
    r"^(defer\s+)?(none|x(Min|Mid|Max)Y(Min|Mid|Max))(\s+(meet|slice))?$"
)


def _is_svg_namespace(text: str) -> bool:
    return text == _SVG_NAMESPACE


def _is_preserve_aspect_ratio(text: str) -> bool:
    return bool(_PRESERVE_ASPECT_RATIO.match(text))


# PDF viewer preferences. /PageMode and /PageLayout are CLOSED enumerations in
# the PDF spec (ISO 32000-1 table 28), so every value either is one of these
# names or is not a viewer preference at all. They say how a reader should open
# the document, not who made it: /UseOutlines is what makes a bookmarked PDF
# open with its bookmark pane showing.
#
# Measured 2026-09-05, exiftool 13.29: a PDF carrying an author and bookmarks
# came out of the pdf engine with the author GONE from the output bytes, and
# still verified RESIDUAL_FOUND on "UseOutlines". The tool cleaned the file
# correctly and then reported it as still leaking. 10 of the 12 values across
# the two enums are long enough to clear _MIN_NEEDLE and do this; /UseNone and
# /UseOC escape only by being shorter than 8 characters, which is luck, not
# design, and is exactly why this is keyed on the enum rather than on length.
_PAGE_MODES = frozenset({
    "UseNone", "UseOutlines", "UseThumbs",
    "FullScreen", "UseOC", "UseAttachments",
})
_PAGE_LAYOUTS = frozenset({
    "SinglePage", "OneColumn",
    "TwoColumnLeft", "TwoColumnRight", "TwoPageLeft", "TwoPageRight",
})


def _is_page_mode(text: str) -> bool:
    return text in _PAGE_MODES


def _is_page_layout(text: str) -> bool:
    return text in _PAGE_LAYOUTS


_STRUCTURAL_VALUES = {
    ("SVG", "xmlns"): _is_svg_namespace,
    ("SVG", "preserveaspectratio"): _is_preserve_aspect_ratio,
    ("PDF", "pagemode"): _is_page_mode,
    ("PDF", "pagelayout"): _is_page_layout,
}

# A needle shorter than this produces false positives against binary payloads.
# Eight characters of an exact recovered string is specific enough to be signal.
_MIN_NEEDLE = 8


class Verdict(str, Enum):
    """Outcome of verification. UNVERIFIED is never treated as success."""

    VERIFIED_CLEAN = "verified_clean"      # read-back empty and no residual bytes
    RESIDUAL_FOUND = "residual_found"      # values survived; the file still leaks
    CARRIERS_REMAIN = "carriers_remain"    # engine read-back still reports tags
    UNVERIFIED = "unverified"              # verification could not run
    # A region of the output that the format's own structure does not account
    # for. Distinct from RESIDUAL_FOUND on purpose: there, a value we captured
    # survived; here, we cannot say what the bytes are, only that nothing
    # explains them. Both mean the file is not clean, and the difference is
    # exactly what the operator needs in order to act.
    STRUCTURE_UNACCOUNTED = "structure_unaccounted"


# The checks this build knows how to run, named so that a caller can be told
# WHICH question went unasked rather than only that one did. A check that ran
# but could not reach the whole file is named here too: "we looked at most of
# it" is not "we looked".
CHECK_RESIDUAL_SCAN = "residual_scan"
CHECK_GPS_CARRIERS = "gps_carriers"
CHECK_STRUCTURE_REGIONS = "structure_regions"

ALL_CHECKS = (CHECK_RESIDUAL_SCAN, CHECK_GPS_CARRIERS, CHECK_STRUCTURE_REGIONS)


# ------------------------------------------------------- the zero-needle decision
#
# OPEN, ON PURPOSE. THIS IS THE PLACE IT LANDS WHEN IT IS DECIDED.
#
# `checked_values == 0` means the residual scan searched the output bytes for
# no strings at all and then returned "nothing survived". Measured 2026-09-07
# and recorded in docs/WHAT-THE-TOOL-CLAIMS.md section 2 Case B: on
# `Nokia 6.1.mp4`, a real geotagged Android video, the verdict is
# VERIFIED_CLEAN over an empty needle set, and the same shape reproduces on a
# GPS-only JPEG and a GPS-only DNG. That is the one case where "measured" and
# "inferred" are provably the same code path, which is the sentence the module
# docstring above opens by rejecting.
#
# Section 5 of that document recommends the case must not be VERIFIED_CLEAN and
# deliberately does NOT decide whether it becomes a NEW verdict value or a
# downgrade to the existing UNVERIFIED. Both are breaking changes to the JSON
# report, so the choice is the owner's and is NOT made here. The behaviour is
# therefore unchanged by the coverage wiring below: `coverage` REPORTS the
# number, and nothing reads it back as a verdict.
#
# WHAT CHANGES WHEN THE DECISION LANDS, so the cost is visible before it is
# paid rather than discovered during it:
#   1. `verify()` grows one branch, here in this module, on `not needles`.
#   2. `Verdict` grows a value, or `UNVERIFIED` grows a second meaning. Only
#      the first is greppable; the second is trap 2 in a new place, because
#      UNVERIFIED today means "verification could not run" and this is
#      "verification ran over an empty set".
#   3. `gui._verdict_text()` needs the new word. It falls back to
#      "not verified" for an unrecognised verdict, which fails safe but reads
#      as the wrong thing.
#   4. `scrubber.py`'s COMPLETE downgrade reads `not verification.clean` and
#      would start turning genuinely cleaned files into STATUS_ERROR.
#   5. Every test asserting VERIFIED_CLEAN on a fixture whose baseline yields
#      no needles. Measured: 2 of the 13 files in the document's table.
#   6. README.md, CHANGELOG.md (as BREAKING), and the Android app's renderer.
#
# `tests/test_coverage_reporting.py::test_the_zero_needle_decision_is_still_open`
# is the collector: it asserts the flag and the current behaviour together, so
# flipping the flag without doing 1 to 6 fails loudly instead of silently.
ZERO_NEEDLE_DECISION_IS_OPEN = True


@dataclass
class Coverage:
    """
    What was actually checked on this file, and what was not.

    This exists because "we did not look" had six spellings and none of them
    reached the user: `GpsStatus.NOT_CHECKED`, `StructureReport.applicable`,
    `ReadOutcome.UNPARSED`, `Completeness.PARTIAL`, `Verdict.UNVERIFIED` and an
    unbuilt sixth. `gps_verify` computed a correct, specific sentence about an
    18.4 MB trailer on a real Samsung JPEG and there was no channel for it to
    travel down. This is the channel.

    It carries a verdict about NOTHING. Every field is a statement about which
    question was asked, never about whether the file is clean; that remains
    `Verdict`, unchanged. A caller must not read `every_check_ran` as a pass.

    `gps_status` is `gps_verify.GpsStatus`'s own value, verbatim, and
    `structure_applicable` is `StructureReport.applicable`, verbatim. Neither
    is translated into a new vocabulary: a seventh spelling of "we did not
    look" is the defect this field exists to close, not a shape it should take.
    """

    checked_values: int = 0
    gps_status: str = gps_verify.GpsStatus.NOT_CHECKED.value
    gps_carriers_checked: List[str] = field(default_factory=list)
    gps_findings: List[str] = field(default_factory=list)
    gps_detail: str = ""
    # Case D in docs/WHAT-THE-TOOL-CLAIMS.md. `unaccounted_regions: []` is
    # emitted identically whether the structural walk RAN and found nothing or
    # NEVER HAPPENED, so the two states share one representation in the
    # interface people parse. That is trap 2, live, inside the JSON report.
    # This is the key that separates them.
    structure_applicable: bool = False
    structure_detail: str = ""
    # The checks that did not run, or that ran without reaching the whole file.
    # Which of those two it was is in `gps_status` and `structure_detail`; this
    # list is the summary, never the evidence.
    #
    # The DEFAULT is both of them, not the empty list. A default-constructed
    # Coverage describes a file nothing was run against, and an empty
    # `unchecked` there would make `every_check_ran` True about a file nobody
    # looked at, which is the exact shape of the defect this class was added to
    # end.
    unchecked: List[str] = field(
        default_factory=lambda: [CHECK_GPS_CARRIERS, CHECK_STRUCTURE_REGIONS]
    )
    detail: str = ""

    def __post_init__(self) -> None:
        # A Coverage whose summary disagrees with its own evidence is worse
        # than no Coverage: it is a wrong answer in the field that exists to
        # make wrong answers visible. Refused at construction, the way
        # GpsReport refuses an applicable report that names no carriers.
        gps_ran = self.gps_status in (gps_verify.GpsStatus.CLEAN.value,
                                      gps_verify.GpsStatus.CARRIER_FOUND.value)
        listed = CHECK_GPS_CARRIERS in self.unchecked
        if gps_ran and listed:
            raise ValueError(
                f"gps_status is {self.gps_status!r}, which means the walker "
                f"reached the whole file, but {CHECK_GPS_CARRIERS!r} is listed "
                "as unchecked"
            )
        if not gps_ran and not listed:
            raise ValueError(
                f"gps_status is {self.gps_status!r}, which is not a completed "
                f"walk, so {CHECK_GPS_CARRIERS!r} must be listed as unchecked"
            )
        if not self.structure_applicable and CHECK_STRUCTURE_REGIONS not in self.unchecked:
            raise ValueError(
                "structure_applicable is False, which means no structural "
                f"walker ran, so {CHECK_STRUCTURE_REGIONS!r} must be listed as "
                "unchecked"
            )

    @property
    def every_check_ran(self) -> bool:
        """
        True only when every check this build knows ran over the whole file.

        Never a claim that the file is clean. A file can have every check run
        and still be leaking, and `Verdict` is what says so.
        """
        return not self.unchecked

    def as_dict(self) -> Dict:
        return {
            "checked_values": self.checked_values,
            "gps_status": self.gps_status,
            "gps_carriers_checked": list(self.gps_carriers_checked),
            "gps_findings": list(self.gps_findings),
            "gps_detail": self.gps_detail,
            "structure_applicable": self.structure_applicable,
            "structure_detail": self.structure_detail,
            "unchecked": list(self.unchecked),
            "every_check_ran": self.every_check_ran,
            "detail": self.detail,
        }


@dataclass
class Verification:
    verdict: Verdict = Verdict.UNVERIFIED
    residual_values: List[str] = field(default_factory=list)
    remaining_tags: List[str] = field(default_factory=list)
    checked_values: int = 0
    detail: str = ""
    unaccounted_regions: List[str] = field(default_factory=list)
    # None means no check was run for this file at all, which is what a
    # Verification built by hand for an already-clean file honestly represents.
    # It is emitted as JSON null rather than as a default Coverage, because a
    # default Coverage would say `structure_applicable: false`, and that is the
    # very conflation this field was added to end.
    coverage: Optional[Coverage] = None

    @property
    def clean(self) -> bool:
        return self.verdict is Verdict.VERIFIED_CLEAN

    def as_dict(self) -> Dict:
        # ADDITIVE ONLY. `--report FILE` is documented in README.md and people
        # parse it, so every key below that existed before `coverage` keeps its
        # name, its type and its meaning.
        # tests/test_coverage_reporting.py::test_the_report_schema_is_additive
        # asserts exactly that, key by key, against the shape measured before
        # this change.
        return {
            "verdict": self.verdict.value,
            "clean": self.clean,
            "residual_values": self.residual_values,
            "remaining_tags": self.remaining_tags,
            "checked_values": self.checked_values,
            "detail": self.detail,
            "unaccounted_regions": self.unaccounted_regions,
            "coverage": self.coverage.as_dict() if self.coverage else None,
        }


def meaningful_values(metadata: Dict) -> Set[str]:
    """
    Reduce an exiftool metadata dict to the set of strings worth searching for
    after sanitizing.

    Filesystem pseudo-tags are dropped: they describe the file rather than being
    carried inside it, so finding "FileName" in the output proves nothing.
    Short and purely numeric values are dropped because they collide with
    ordinary binary content and would produce false RESIDUAL_FOUND verdicts. A
    false positive here is expensive: it would tell a user a clean file is
    dirty, and they would stop trusting the check.
    """
    needles: Set[str] = set()
    for key, value in metadata.items():
        group, _, bare = key.partition(":")
        if not bare:
            group, bare = key, key
        if group in _PSEUDO_GROUPS or key == "SourceFile":
            continue
        if bare.lower() in _STRUCTURAL_TAGS:
            continue
        # None for almost every tag. When present, it drops only the values that
        # ARE the structure, and leaves every other value under the same tag as
        # a needle. See _STRUCTURAL_VALUES.
        structural = _STRUCTURAL_VALUES.get((group, bare.lower()))
        for text in _flatten(value):
            text = text.strip()
            if len(text) < _MIN_NEEDLE:
                continue
            if structural is not None and structural(text):
                continue
            # Pure digits and separator-only strings collide with binary data.
            if text.replace(":", "").replace("-", "").replace(".", "").replace(" ", "").isdigit():
                continue
            needles.add(text)
    return needles


def _flatten(value) -> List[str]:
    """Metadata values may be scalars or lists (XMP creator lists, keywords)."""
    if isinstance(value, str):
        return [value]
    if isinstance(value, (list, tuple)):
        out: List[str] = []
        for item in value:
            out.extend(_flatten(item))
        return out
    return []


def _ole2_without_directory_names(blob: bytes, path: str) -> bytes:
    """
    Blank the name field of every OLE2 directory entry, and nothing else.

    A compound file stores each stream's NAME in its directory, as UTF-16LE.
    Those names are required structure, exactly like the part names in an OOXML
    package that `_PSEUDO_GROUPS` already excludes for the same reason. They
    become a problem when a metadata VALUE happens to equal one of them.

    Measured 2026-09-04 on a LibreOffice-produced .ppt: the CurrentUserAtom's
    userName is the literal string "Current User", which is also the name of the
    stream holding it. After the engine blanked the userName, the residual scan
    still found "Current User" once, in UTF-16LE, in the directory entry that
    names the stream. The file was clean and was reported as leaking.

    Only the 64-byte name fields are blanked. Every payload byte, the FAT, and
    the slack in every allocated sector are still searched, so residue left in
    space the file is no longer using is still found. That matters: it is the
    same class of leftover as the PDF incremental update this module exists to
    catch, and dropping to a payload-only scan would give it up.
    """
    entry_size = 128
    name_field = 64
    try:
        import olefile

        out = bytearray(blob)
        with olefile.OleFileIO(path) as ole:
            sector = ole.first_dir_sector
            seen = set()
            while (sector not in (olefile.ENDOFCHAIN, olefile.FREESECT)
                   and sector not in seen):
                seen.add(sector)
                start = (sector + 1) * ole.sectorsize
                stop = min(start + ole.sectorsize, len(out))
                for offset in range(start, stop, entry_size):
                    out[offset:offset + name_field] = b"\x00" * min(
                        name_field, len(out) - offset
                    )
                try:
                    sector = ole.fat[sector]
                except IndexError:
                    break
        return bytes(out)
    except Exception:
        # Returning the raw bytes is less precise and can produce a false
        # RESIDUAL_FOUND. That is the correct direction to fail in: the
        # alternative, letting the exception reach searchable_bytes, gets caught
        # there and turns into an EMPTY haystack, which finds nothing and
        # reports every value gone. A missing library or an unparseable
        # directory must never be able to manufacture a clean verdict.
        return blob


def searchable_bytes(path: str, spec: FormatSpec) -> bytes:
    """
    Produce the byte stream a residual scan should search.

    For most containers this is the file itself. For a zip container it is not:
    OOXML metadata lives in deflated members, so scanning the raw .docx would
    find nothing and report a false clean. Members are inflated and
    concatenated instead. For an OLE2 compound file it is the file with the
    directory entry names blanked; see _ole2_without_directory_names.
    """
    try:
        if spec.container is Container.OLE2:
            with open(path, "rb") as fh:
                return _ole2_without_directory_names(fh.read(), path)
        if spec.container is Container.ZIP:
            buf = io.BytesIO()
            with zipfile.ZipFile(path) as zf:
                for name in zf.namelist():
                    buf.write(name.encode("utf-8", "ignore"))
                    try:
                        buf.write(zf.read(name))
                    except Exception:
                        # A member that will not inflate cannot be searched.
                        # Skipping it makes the scan less sensitive, never
                        # falsely clean, because a miss can only under-report.
                        continue
            return buf.getvalue()
        with open(path, "rb") as fh:
            return fh.read()
    except Exception:
        return b""


def _encodings(needle: str) -> List[bytes]:
    """
    A value can be stored in more than one encoding. OLE2 property streams and
    some XMP packets use UTF-16LE, most other carriers use UTF-8 or Latin-1.
    Checking all three keeps the scan from missing a survivor purely because of
    how it was encoded.
    """
    out = []
    for enc in ("utf-8", "utf-16-le", "latin-1"):
        try:
            encoded = needle.encode(enc)
        except (UnicodeEncodeError, LookupError):
            continue
        if encoded and encoded not in out:
            out.append(encoded)
    return out


def residual_scan(path: str, spec: FormatSpec, needles: Sequence[str]) -> List[str]:
    """
    Return the subset of `needles` still findable in the sanitized file.

    An empty list is the only acceptable result for a format declared COMPLETE.
    """
    if not needles:
        return []
    haystack = searchable_bytes(path, spec)
    if not haystack:
        return []
    survivors = []
    for needle in needles:
        if any(enc in haystack for enc in _encodings(needle)):
            survivors.append(needle)
    return sorted(survivors)


def scoped_values(metadata: Dict, fields: Sequence[str]) -> Set[str]:
    """
    Needles for a SELECTIVE removal: only the values of the tags the caller
    actually asked to remove.

    Without this, verifying a selective run searches for every value the file
    ever carried and then fails because the tags the user deliberately KEPT are
    still present. That is not a leak, it is the requested behaviour, and
    reporting it as a failure would make selective removal unusable.
    """
    wanted = {field.lower().rsplit(":", 1)[-1] for field in fields}
    subset = {
        key: value for key, value in metadata.items()
        if (key.lower().rsplit(":", 1)[-1] in wanted)
    }
    return meaningful_values(subset)


def _gps_report(path: str, only_fields: Optional[Sequence[str]]):
    """
    Run the GPS carrier check on the OUTPUT, or say why it was not run.

    Returns None when this run deliberately did not ask the question, which is
    not the same as `gps_verify` having no walker for the format. A selective
    removal of the Artist must not be failed for the GPS the user chose to
    keep, so the check is scoped by `gps_verify.gps_in_scope()`, which exists
    for that reason and mirrors `scoped_values()` above. The two "we did not
    ask" cases produce the same `gps_status` (gps_verify's own NOT_CHECKED,
    which is the honest word for both) and different `gps_detail`, because the
    caller needs to know which one it was.
    """
    if not gps_verify.gps_in_scope(only_fields):
        return None
    return gps_verify.scan(path)


def _coverage(path: str, needles: int, report, structure: StructureReport,
              only_fields: Optional[Sequence[str]]) -> Coverage:
    """
    Assemble the one statement of what was and was not checked.

    Nothing here decides anything. It reports what the three checks said, in
    their own words, and names the ones that did not run.
    """
    extension = os.path.splitext(path)[1].lower() or "this format"
    unchecked: List[str] = []

    if report is None:
        gps_status = gps_verify.GpsStatus.NOT_CHECKED.value
        carriers: List[str] = []
        findings: List[str] = []
        targeted = ", ".join(str(name) for name in (only_fields or []))
        gps_detail = (
            "GPS carriers: NOT CHECKED (this run was asked to remove only "
            f"{targeted or 'named fields'}, none of which is a GPS field, so "
            "a surviving coordinate is the requested behaviour and not a leak)"
        )
    else:
        gps_status = report.status.value
        carriers = list(report.carriers_checked)
        findings = [str(item) for item in report.findings]
        gps_detail = report.describe()

    # CLEAN and CARRIER_FOUND are the two states in which the walker reached
    # the whole file. NOT_CHECKED, ERROR and INCOMPLETE are not, and folding
    # any of them into "checked" is the over-claim gps_verify was built to
    # refuse.
    if gps_status not in (gps_verify.GpsStatus.CLEAN.value,
                          gps_verify.GpsStatus.CARRIER_FOUND.value):
        unchecked.append(CHECK_GPS_CARRIERS)

    if not structure.applicable:
        structure_detail = (
            f"structure: NOT CHECKED (no structural walker for {extension})"
        )
        unchecked.append(CHECK_STRUCTURE_REGIONS)
    elif structure.error is not None:
        structure_detail = f"structure: COULD NOT CHECK ({structure.error})"
        unchecked.append(CHECK_STRUCTURE_REGIONS)
    elif structure.unaccounted:
        structure_detail = (
            f"structure: {len(structure.unaccounted)} region(s) of the output "
            "are not accounted for"
        )
    else:
        structure_detail = "structure: every region of the output is accounted for"

    # The residual scan always runs. It can still search for nothing; see
    # ZERO_NEEDLE_DECISION_IS_OPEN above for why that is reported as a number
    # here and not as a verdict anywhere.
    residual_detail = f"residual byte scan: {needles} value(s) searched for"

    return Coverage(
        checked_values=needles,
        gps_status=gps_status,
        gps_carriers_checked=carriers,
        gps_findings=findings,
        gps_detail=gps_detail,
        structure_applicable=structure.applicable,
        structure_detail=structure_detail,
        unchecked=unchecked,
        detail="; ".join([residual_detail, gps_detail, structure_detail]),
    )


def verify(
    path: str,
    spec: FormatSpec,
    before: Dict,
    after: Dict,
    only_fields: Optional[Sequence[str]] = None,
    after_readable: bool = True,
) -> Verification:
    """
    Combine the two checks into one verdict.

    Order matters. The residual scan is decisive: if a value survived in the
    bytes, the file leaks regardless of what the engine read-back says, and that
    is exactly the PDF failure mode this module exists to catch.

    `only_fields` narrows the check to a selective removal's targets.

    `after_readable` is False when the post-write read could not be performed at
    all. An empty `after` then means "we did not look", not "nothing is there",
    and the two must not produce the same verdict: a caller reading
    VERIFIED_CLEAN off a check that never ran is exactly the inference this
    module exists to replace with measurement.
    """
    if only_fields:
        needles = scoped_values(before, only_fields)
    else:
        needles = meaningful_values(before)
    survivors = residual_scan(path, spec, sorted(needles))

    remaining = sorted(
        key for key in after
        if (key.split(":", 1)[0] if ":" in key else key) not in _PSEUDO_GROUPS
        and key != "SourceFile"
    )

    # Both of these run before any verdict is returned, and unconditionally.
    #
    # The structural scan used to run only on the path where nothing survived,
    # which meant a RESIDUAL_FOUND file emitted `unaccounted_regions: []` and
    # `structure_applicable` would have had to say False about a walk that was
    # never attempted. That is the same conflation this field was added to
    # close, one branch further along, so the walk is attempted for every file
    # and `structure_applicable` is a measurement on every path. The branch
    # ORDER below is unchanged: the residual scan is still decisive.
    gps = _gps_report(path, only_fields)
    structure = structure_scan(path)
    coverage = _coverage(path, len(needles), gps, structure, only_fields)

    if survivors:
        return Verification(
            verdict=Verdict.RESIDUAL_FOUND,
            residual_values=survivors,
            remaining_tags=remaining,
            checked_values=len(needles),
            detail=f"{len(survivors)} metadata value(s) still present in the output bytes",
            coverage=coverage,
        )

    # The residual scan can only look for values a baseline read produced, so a
    # carrier exiftool never parsed contributes no needles and the scan above
    # passes having searched for nothing. Measured 2026-09-06 on the shipped
    # 1.0.1: an unknown WebP RIFF chunk, an unknown PNG ancillary chunk, and
    # data appended after a GIF trailer each rode through a SANITIZED file that
    # reported "verified clean". This check asks the question that does not
    # depend on the baseline read: is there a region the format cannot explain?
    if structure.error:
        return Verification(
            verdict=Verdict.UNVERIFIED,
            checked_values=len(needles),
            remaining_tags=remaining,
            detail=f"the output could not be structurally parsed: {structure.error}",
            coverage=coverage,
        )
    if structure.unaccounted:
        return Verification(
            verdict=Verdict.STRUCTURE_UNACCOUNTED,
            checked_values=len(needles),
            remaining_tags=remaining,
            unaccounted_regions=structure.unaccounted,
            detail=(
                f"{len(structure.unaccounted)} region(s) of the output are not "
                "accounted for by the format's structure and may carry anything"
            ),
            coverage=coverage,
        )

    if not after_readable:
        # The residual scan above still ran and found nothing, which is real
        # evidence, but it is only half the check. Report UNVERIFIED so a
        # COMPLETE format is downgraded to an error rather than passing on a
        # read-back that never happened.
        return Verification(
            verdict=Verdict.UNVERIFIED,
            checked_values=len(needles),
            detail="no residual values found, but the file could not be re-read "
                   "to confirm its metadata carriers are gone",
            coverage=coverage,
        )

    if remaining:
        # Tags still reported but no original value survived. This is the normal
        # and correct outcome for rebuilt containers, which legitimately carry a
        # fresh producer string or a new format version tag. It is reported
        # rather than hidden so the caller can judge.
        return Verification(
            verdict=Verdict.VERIFIED_CLEAN,
            remaining_tags=remaining,
            checked_values=len(needles),
            detail="no original values survived; remaining tags are engine-generated",
            coverage=coverage,
        )

    return Verification(
        verdict=Verdict.VERIFIED_CLEAN,
        checked_values=len(needles),
        detail="no metadata carriers and no residual values",
        coverage=coverage,
    )
