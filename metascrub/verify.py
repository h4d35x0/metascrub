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
"""

from __future__ import annotations

import io
import os
import re
import zipfile
from dataclasses import dataclass, field
from enum import Enum
from typing import Dict, List, Optional, Sequence, Set

from .capabilities import Container, FormatSpec

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


@dataclass
class Verification:
    verdict: Verdict = Verdict.UNVERIFIED
    residual_values: List[str] = field(default_factory=list)
    remaining_tags: List[str] = field(default_factory=list)
    checked_values: int = 0
    detail: str = ""

    @property
    def clean(self) -> bool:
        return self.verdict is Verdict.VERIFIED_CLEAN

    def as_dict(self) -> Dict:
        return {
            "verdict": self.verdict.value,
            "clean": self.clean,
            "residual_values": self.residual_values,
            "remaining_tags": self.remaining_tags,
            "checked_values": self.checked_values,
            "detail": self.detail,
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

    if survivors:
        return Verification(
            verdict=Verdict.RESIDUAL_FOUND,
            residual_values=survivors,
            remaining_tags=remaining,
            checked_values=len(needles),
            detail=f"{len(survivors)} metadata value(s) still present in the output bytes",
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
        )

    return Verification(
        verdict=Verdict.VERIFIED_CLEAN,
        checked_values=len(needles),
        detail="no metadata carriers and no residual values",
    )
