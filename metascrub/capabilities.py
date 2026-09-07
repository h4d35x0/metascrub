"""
Format capability table.

This replaces the boolean `_is_supported_file()` gate inherited from the the parent project
sanitizer (cli/sanitizer.py:326), which answered a single yes/no for a set of
eight image extensions. A boolean was adequate while exiftool handled every
supported format. It stops being adequate the moment PDF, OOXML, OLE2 and video
enter, because those need different engines and carry different guarantees.

"Supported" and "completely scrubbable" are two different states and must never
share one representation. They are separate fields here for that reason.
"""

from __future__ import annotations

from dataclasses import dataclass
from enum import Enum
from typing import Dict, Optional
import os


class Engine(str, Enum):
    """The tool that actually performs the removal for a given format."""

    EXIFTOOL = "exiftool"   # exiftool -all=, in-place, safe for these containers
    PDF = "pdf"             # pikepdf full rewrite; exiftool is NOT safe here
    OOXML = "ooxml"         # zip container rewrite (docx/xlsx/pptx)
    ODF = "odf"             # zip container rewrite (odt/ods/odp/odg + templates)
    OLE2 = "ole2"           # legacy compound-file property streams
    AV = "av"               # ffmpeg remux, stream copy
    SVG = "svg"             # XML text rewrite; exiftool cannot write SVG at all

    # Phase 1 of the Android media build. Pure-Python container surgery, no
    # exiftool and no ffmpeg, because neither can run on a phone.
    #
    # These are NOT wired into CAPABILITIES. Every row for .jpg, .png and .webp
    # still says EXIFTOOL, which works correctly on the desktop and shipped in
    # 1.0.2. Switching the desktop default to these engines is a separate and
    # deliberate decision, to be made once they are proven against the exiftool
    # oracle, not a side effect of adding them. Until then they are reachable
    # only from tests and from the future mobile build.
    JPEG = "jpeg"           # APPn/COM marker surgery, plus the post-EOI trailer
    PNG = "png"             # ancillary chunk whitelist
    WEBP = "webp"           # RIFF chunk whitelist; drop VP8X where possible
    ISOBMFF = "isobmff"     # ISO base media box surgery: mp4/mov/heic/avif/3gp


class Completeness(str, Enum):
    """
    How much of the file's metadata the engine can actually remove.

    COMPLETE means: after a successful run there is no known metadata carrier of
    this format left behind, and the residual scan is expected to come back
    clean.

    PARTIAL means: the engine removes the standard carriers but known residue
    can survive (for example application-specific records this tool does not
    parse). A PARTIAL result must be reported as PARTIAL. A silent partial is
    worse than a refusal, because the user acts on the belief the file is clean.
    """

    COMPLETE = "complete"
    PARTIAL = "partial"


class Container(str, Enum):
    """
    How the bytes are packed. This drives the residual scan strategy: a value
    sitting inside a deflated zip member will not appear in the raw file bytes,
    so scanning the raw bytes of a .docx would produce a false "clean".
    """

    RAW = "raw"      # metadata is findable in the file bytes as-is
    ZIP = "zip"      # must inflate members before scanning
    PDF = "pdf"      # raw, but streams may be compressed
    OLE2 = "ole2"    # raw; property streams are uncompressed


@dataclass(frozen=True)
class FormatSpec:
    """One row of the capability table."""

    engine: Engine
    completeness: Completeness
    container: Container
    rewrites_container: bool  # True when output is a rebuilt file, not patched in place
    note: str = ""


def _exif(note: str = "") -> FormatSpec:
    """Formats where exiftool -all= is a genuine, in-place, complete removal."""
    return FormatSpec(Engine.EXIFTOOL, Completeness.COMPLETE, Container.RAW, False, note)


# TIER 1: exiftool is sufficient and complete.
_IMAGE: Dict[str, FormatSpec] = {
    ".jpg": _exif(), ".jpeg": _exif(), ".jpe": _exif(),
    # TIFF is structurally EXIF: exiftool answers "Can't delete IFD0 from TIFF"
    # and `-all=` leaves Artist and Copyright in place. The exiftool engine
    # follows up with a targeted sweep for these, which does remove them.
    ".tif": _exif("IFD0 cannot be dropped wholesale; identity tags swept individually"),
    ".tiff": _exif("IFD0 cannot be dropped wholesale; identity tags swept individually"),
    ".png": _exif("also drops iTXt/tEXt/zTXt text chunks"),
    # .heic/.heif/.avif: see the ISO base media block below.
    ".webp": _exif("EXIF, XMP and ICC chunks"),
    ".gif": _exif("comment blocks, XMP and application extension blocks"),
    ".jp2": _exif(), ".psd": _exif(),
    # .bmp is deliberately absent. Measured 2026-09-04: exiftool 13.29 answers
    # "Writing of BMP files is not yet supported" and exits 1. Listing a format
    # this tool cannot actually write would be a promise it cannot keep.
    # Raw camera formats. exiftool edits these safely, but a raw file is a
    # container of maker-specific records; treat as PARTIAL rather than claim
    # more than can be verified.
    ".dng": FormatSpec(Engine.EXIFTOOL, Completeness.PARTIAL, Container.RAW, False,
                       "raw container; maker notes may retain private records"),
    ".cr2": FormatSpec(Engine.EXIFTOOL, Completeness.PARTIAL, Container.RAW, False,
                       "raw container; maker notes may retain private records"),
    ".nef": FormatSpec(Engine.EXIFTOOL, Completeness.PARTIAL, Container.RAW, False,
                       "raw container; maker notes may retain private records"),
    ".arw": FormatSpec(Engine.EXIFTOOL, Completeness.PARTIAL, Container.RAW, False,
                       "raw container; maker notes may retain private records"),
    ".orf": FormatSpec(Engine.EXIFTOOL, Completeness.PARTIAL, Container.RAW, False,
                       "raw container; maker notes may retain private records"),
    ".rw2": FormatSpec(Engine.EXIFTOOL, Completeness.PARTIAL, Container.RAW, False,
                       "raw container; maker notes may retain private records"),

    # Added 2026-09-04. Each one was measured, not assumed: a TIFF was written
    # under the extension, exiftool identified it as that specific FileType
    # (not as TIFF) and accepted a write, and the full metascrub pipeline then
    # removed the sentinel from the output bytes with the file still opening.
    #
    # Formats deliberately NOT added, with the measured reason:
    #   .cr3 .raf .x3f .crw .mrw .cs1 .psb  exiftool reports FileType TIFF for a
    #       file with that extension and refuses to write it as the target
    #       format. These containers are not TIFF-based, so no fixture can be
    #       built without genuine camera samples, and the .tiff proxy used by
    #       the coverage gate would be a false claim rather than a shortcut.
    #   .3fr .fff  exiftool identifies them correctly but will not write one
    #       that was synthesised rather than produced by a camera.
    ".pef": FormatSpec(Engine.EXIFTOOL, Completeness.PARTIAL, Container.RAW, False,
                       "raw container; maker notes may retain private records"),
    ".srw": FormatSpec(Engine.EXIFTOOL, Completeness.PARTIAL, Container.RAW, False,
                       "raw container; maker notes may retain private records"),
    ".erf": FormatSpec(Engine.EXIFTOOL, Completeness.PARTIAL, Container.RAW, False,
                       "raw container; maker notes may retain private records"),
    ".mos": FormatSpec(Engine.EXIFTOOL, Completeness.PARTIAL, Container.RAW, False,
                       "raw container; maker notes may retain private records"),
    ".iiq": FormatSpec(Engine.EXIFTOOL, Completeness.PARTIAL, Container.RAW, False,
                       "raw container; maker notes may retain private records"),
    ".arq": FormatSpec(Engine.EXIFTOOL, Completeness.PARTIAL, Container.RAW, False,
                       "raw container; maker notes may retain private records"),
    ".sr2": FormatSpec(Engine.EXIFTOOL, Completeness.PARTIAL, Container.RAW, False,
                       "raw container; maker notes may retain private records"),
    ".rwl": FormatSpec(Engine.EXIFTOOL, Completeness.PARTIAL, Container.RAW, False,
                       "raw container; maker notes may retain private records"),
    ".nrw": FormatSpec(Engine.EXIFTOOL, Completeness.PARTIAL, Container.RAW, False,
                       "raw container; maker notes may retain private records"),
    ".raw": FormatSpec(Engine.EXIFTOOL, Completeness.PARTIAL, Container.RAW, False,
                       "raw container; maker notes may retain private records"),
    ".gpr": FormatSpec(Engine.EXIFTOOL, Completeness.PARTIAL, Container.RAW, False,
                       "raw container; maker notes may retain private records"),
}

# TIER 2: PDF. exiftool is NOT used here and must not be.
#
# Measured 2026-09-04 with exiftool 13.29: `exiftool -all=` on a PDF performs an
# incremental update. It appends the change and leaves the previous metadata in
# the file. The test document grew from 1519 to 1846 bytes and the original
# author and title strings were still present in the raw bytes twice each, while
# `exiftool -Author -Title` reported them absent. exiftool itself warns:
# "ExifTool PDF edits are reversible. Deleted tags may be recovered!"
#
# A pikepdf rewrite of the same document produced zero residual hits and shrank
# the file to 1025 bytes.
_PDF: Dict[str, FormatSpec] = {
    ".pdf": FormatSpec(Engine.PDF, Completeness.COMPLETE, Container.PDF, True,
                       "full rewrite; exiftool would leave recoverable residue"),
}

# TIER 3: OOXML. exiftool will not rewrite inside the zip container.
_OOXML_NOTE = ("docProps core/app/custom, lastModifiedBy, revision, edit time, "
               "template path, and w:rsid revision-save identifiers")
_OOXML: Dict[str, FormatSpec] = {
    ext: FormatSpec(Engine.OOXML, Completeness.COMPLETE, Container.ZIP, True, _OOXML_NOTE)
    for ext in (".docx", ".docm", ".xlsx", ".xlsm", ".pptx", ".pptm")
}

# ---------------------------------------------------------------------------
# ODF BLOCK BEGINS. Kept together so it can be moved or merged in one piece.
# ---------------------------------------------------------------------------
#
# TIER 3B: OpenDocument. Also a zip container, but NOT the OOXML engine.
#
# exiftool reads every one of these and writes none of them. Measured 2026-09-04
# with exiftool 13.29 on LibreOffice-produced files:
#   .odt/.ods/.odp/.odg  "Writing of ODT files is not yet supported", exit 1
#   .ott                 "Writing of this type of file is not supported", exit 1
# (.ott differs because exiftool identifies it as FileType ZIP; it still reads
# the metadata, which is all the baseline read needs.)
#
# COMPLETE, and the note says what that covers. The claim rests on measurement,
# not on the engine reporting success: for every extension below a fixture
# seeded a sentinel into meta.xml, settings.xml and the package RDF, and
# tests/test_odf.py searches the INFLATED output members for each sentinel
# afterwards, including the base64 spelling of the printer blob.
#
# settings.xml is the carrier that justifies a dedicated engine. Measured on a
# LibreOffice .odt built on this machine: PrinterName held the workstation's
# default printer name and PrinterSetup held 11316 base64 characters decoding to
# 8487 bytes of Windows DEVMODE carrying that printer name twice in ASCII, once
# in UTF-16LE, and the driver string "<printer model redacted>". Nothing else in
# this tool looks at settings.xml, and exiftool does not report it at all.
#
# Formats deliberately NOT added, with the measured reason:
#   .fodt .fods .fodp  flat XML ODF, a single uncompressed XML file rather than
#       a package. Measured: exiftool sees .fodt as FileType XML, reads Title
#       and Creator, and refuses to write it with "[minor] Can't handle XMP
#       attribute 'office:mimetype'". They need a Container.RAW engine that
#       edits XML in place, which is a different engine and not a table row.
#       Recorded in DEFERRED below rather than left silent.
_ODF_NOTE = ("meta.xml replaced with an empty skeleton; printer name and "
             "printer driver blob, revision-save identifiers, thumbnails and "
             "package RDF removed; annotations and tracked changes are "
             "reported, not removed")
_ODF: Dict[str, FormatSpec] = {
    ext: FormatSpec(Engine.ODF, Completeness.COMPLETE, Container.ZIP, True, _ODF_NOTE)
    for ext in (".odt", ".ott", ".ods", ".ots", ".odp", ".otp", ".odg", ".otg")
}
# ---------------------------------------------------------------------------
# ODF BLOCK ENDS.
# ---------------------------------------------------------------------------

# TIER 4: legacy OLE2 compound files. Deferred until 2026-09-04, now shipped.
#
# .doc, .xls and .ppt keep their metadata in the \005SummaryInformation and
# \005DocumentSummaryInformation property streams of an OLE2 compound file, and
# olefile can only overwrite a stream in place at its existing length. The
# deferral was never about that constraint; it was about not being able to build
# a fixture, and therefore not being able to test a rewrite path for documents a
# user cannot regenerate.
#
# The discharge condition was: a real fixture set under tests/fixtures/ole2/ and
# tests/test_ole2.py passing against it. Both now exist. LibreOffice's MS Word
# 97 / MS Excel 97 / MS PowerPoint 97 export filters produce genuine compound
# files (magic d0cf11e0a1b11ae1) carrying seeded metadata, so the fixture is
# generated at test time and the tests skip, never fail, where LibreOffice is
# absent. tests/test_coverage_gate.py still enforces the condition.
#
# PARTIAL, not COMPLETE, and the note says exactly what survives. Measured with
# exiftool 13.59 on a scrubbed .doc: the [MS-DOC] group still reports CreateDate,
# ModifyDate, RevisionNumber, TotalEditTime and the document statistics, because
# the DOP inside the WordDocument/Table streams keeps its own copy of them. The
# give-away that it is a second carrier rather than a leftover is that the
# surviving CreateDate is shifted by the local UTC offset: it is a local-time
# field in the DOP, not the FILETIME in the property set.
# The notes are per extension rather than shared, because the carriers differ.
# A .doc row promising that Excel's user name was removed would be describing a
# structure the file does not have, and a reader cannot tell an irrelevant
# promise from a kept one.
_OLE2_COMMON = ("summary and document-summary property sets and the CompObj "
                "application user type are removed")
_OLE2: Dict[str, FormatSpec] = {
    ".doc": FormatSpec(
        Engine.OLE2, Completeness.PARTIAL, Container.OLE2, True,
        _OLE2_COMMON + "; the DOP copy of the timestamps, edit time, revision "
        "count and document statistics survives in the WordDocument/Table "
        "streams, as do SttbfAssoc and SttbSavedBy on documents written by "
        "Microsoft Word"),
    ".xls": FormatSpec(
        Engine.OLE2, Completeness.PARTIAL, Container.OLE2, True,
        _OLE2_COMMON + ", as is the WRITEACCESS user name in the Workbook "
        "globals; other BIFF records this engine does not parse survive"),
    ".ppt": FormatSpec(
        Engine.OLE2, Completeness.PARTIAL, Container.OLE2, True,
        _OLE2_COMMON + ", as is the last-editor name in the Current User "
        "stream; per-edit user records in the PowerPoint Document stream "
        "survive"),
}

# A deferral is an obligation with a due date, and tests/test_coverage_gate.py
# is what collects it rather than anybody's memory. Prose in the README and in
# the README is NOT the mechanism: an entry that is not in this dict is
# collected by nothing, because every gate that reads deferrals iterates this
# table and a loop over an empty container is the quietest possible pass.
# Every entry must name the measured reason and the condition that discharges
# it.
#
# `.qt`, `.mqv`, `.lrv` and `.f4a` were deferred here until the
# flavour-preserving muxer selection shipped on 2026-09-04 and each of the four
# passed the sentinel byte search; they are in the AV tier below now.
#
# The gates in tests/test_coverage_gate.py do NOT lose their teeth when this
# table happens to be empty. They are written as functions over a table and are
# exercised against synthetic tables, so the mechanism is proven whether or not
# anything is deferred today. That was the 2026-09-04 audit finding: an empty
# table used to make three gates pass having asserted nothing at all.
DEFERRED: Dict[str, str] = {
    # --- ODF BLOCK BEGINS ---
    # Flat XML ODF: one uncompressed XML file rather than a zip package, so the
    # ODF engine's whole strategy (rebuild the package, hoist mimetype, replace
    # meta.xml) does not apply. Measured 2026-09-04 with exiftool 13.29: a
    # .fodt reports FileType XML, reads Title and Creator correctly, and
    # refuses to write with "[minor] Can't handle XMP attribute
    # 'office:mimetype'". Discharge condition: a Container.RAW engine that
    # edits the office:document XML in place, plus a fixture per extension and
    # a tests/test_flat_odf.py that searches the output bytes.
    ".fodt": ("flat XML OpenDocument, not a zip package; exiftool reads it as "
              "XML and refuses to write it. Needs a raw-XML engine, not a "
              "table row. Discharged by a Container.RAW engine plus fixtures "
              "and tests/test_flat_odf.py"),
    ".fods": ("flat XML OpenDocument, not a zip package; exiftool reads it as "
              "XML and refuses to write it. Needs a raw-XML engine, not a "
              "table row. Discharged by a Container.RAW engine plus fixtures "
              "and tests/test_flat_odf.py"),
    ".fodp": ("flat XML OpenDocument, not a zip package; exiftool reads it as "
              "XML and refuses to write it. Needs a raw-XML engine, not a "
              "table row. Discharged by a Container.RAW engine plus fixtures "
              "and tests/test_flat_odf.py"),
    # --- ODF BLOCK ENDS ---
}

# TIER 5: audio and video. ffmpeg remux, not exiftool.
_AV_NOTE = "container, per-stream and chapter metadata; remux without re-encoding"
#
# The 2026-09-04 additions (.f4v .m4b .ts .m2ts .aiff .aif) were each measured
# end to end: ffmpeg muxed a fixture that stored the tag, the pipeline removed
# the sentinel from the output bytes, and ffprobe still parsed the result.
#
# Containers deliberately NOT added, with the measured reason:
#   .wmv .wma   the remux leaves a value behind and verification reports
#               residual_found. ASF is not cleanable by this engine, so
#               listing it would be a promise the tool cannot keep.
#   .aac .ac3   raw bitstreams with no metadata container; ffmpeg accepts the
#               -metadata flag and stores nothing, so there is no fixture and
#               nothing to remove.
#   .3gp .mpg .amr  no fixture could be built that actually stored the tag with
#               the codecs available here.
#   .qt .mqv .lrv .f4a  DEFERRED rather than refused; see the DEFERRED table
#               above, which is what the coverage gates actually read.
_AV: Dict[str, FormatSpec] = {
    ext: FormatSpec(Engine.AV, Completeness.COMPLETE, Container.RAW, True, _AV_NOTE)
    for ext in (".mp4", ".m4v", ".mov", ".mkv", ".webm", ".avi",
                ".m4a", ".mp3", ".flac", ".wav", ".ogg", ".opus",
                ".f4v", ".m4b", ".ts", ".m2ts", ".aiff", ".aif",
                # Shipped 2026-09-04 with av_engine.MUXER_FOR_EXT. Each was
                # measured end to end and separately: a fixture that really
                # stored the sentinel, the sentinel gone from the output bytes
                # afterwards, ffprobe still parsing the result, and the ftyp
                # brand unchanged, so the remux is not quietly converting the
                # container to a different one.
                ".qt", ".mqv", ".lrv", ".f4a")
}

# ---------------------------------------------------------------------------
# TIER 6: SVG. Begin SVG block.
#
# exiftool cannot write SVG at all. Measured 2026-09-04 with exiftool 13.29:
# SVG is absent from `-listwf`, and `exiftool -all= -overwrite_original` answers
# "ExifTool does not yet support writing of SVG images" and exits 1. It READS
# SVG usefully, so the baseline and the verification still come from exiftool
# while svg_engine.py does the writing.
#
# PARTIAL, and the note says exactly what remains. Both survivors were measured
# rather than assumed, and both are invisible to the two checks this tool makes:
#
#   base64-embedded rasters   a 32x32 JPEG carrying Artist and GPSLatitude was
#       base64ed into an <image> data: URI. exiftool reported only Xmlns,
#       ImageWidth, ImageHeight and ViewBox on the wrapping SVG, so there was no
#       needle, and the value is not findable in the raw bytes because it is
#       base64-wrapped, so the residual scan cannot see it either. The file
#       would report VERIFIED_CLEAN while carrying a photographer's name and
#       coordinates. Removing it would delete the picture.
#
#   external local references  an xlink:href of file:///C:/Users/<name>/... is
#       not read by exiftool and is not removed, because removing it deletes the
#       image from the drawing. It is reported as a note instead.
#
# .svgz is deliberately absent. It is gzipped SVG, so Container.RAW scanning
# would be blind to every byte of it, and shipping a format whose residual scan
# silently cannot see anything is worse than not shipping it. It needs its own
# Container value so searchable_bytes() decompresses first, which is a verify.py
# change of the same shape as the existing ZIP branch.
_SVG_NOTE = ("XML comments, the <metadata> RDF block, editor state and "
             "editor-private sodipodi/inkscape/ooo attributes are removed; "
             "EXIF inside base64-embedded raster images, external local "
             "file references, and -inkscape-* CSS properties inside style "
             "attributes survive and are reported")
_SVG: Dict[str, FormatSpec] = {
    ".svg": FormatSpec(Engine.SVG, Completeness.PARTIAL, Container.RAW, True, _SVG_NOTE),
}

# .svgz is DEFERRED rather than merely absent, so a user is told why instead of
# getting the same flat "unsupported" a .txt file gets, and so the gates in
# tests/test_coverage_gate.py collect it. This project's rule is that a deferral
# is enforced by a test, not by memory.
#
# Written as an update rather than as an entry in the DEFERRED literal above so
# that the three engine branches in flight can each add their own deferrals
# without colliding on one dict.
DEFERRED.update({
    ".svgz": (
        "gzipped SVG. The engine would be a one-line gzip wrapper, but "
        "Container.RAW makes the residual scan search the compressed bytes, "
        "where it can see nothing at all, so the format would verify clean "
        "unconditionally. Discharge condition: a Container value whose "
        "searchable_bytes() branch decompresses first, of the same shape as "
        "the existing ZIP branch, plus a fixture whose sentinel is found "
        "before the scrub and absent after."
    ),
})
# End SVG block.
# ---------------------------------------------------------------------------


# ---------------------------------------------------------------------------
# TIER 7: still images in an ISO base media container. Begin ISOBMFF block.
#
# .heic, .heif and .avif routed away from exiftool on 2026-09-07. This is a
# change to shipped behaviour for a whole format family, so the measurement is
# recorded here rather than in a commit message.
#
# THE DEFECT THAT FORCED IT. exiftool CANNOT remove a HEIF ICC profile. In HEIF
# the ICC lives in iprp/ipco/colr, which is an item PROPERTY and not a metadata
# item, and exiftool does not rewrite the property structure. Measured
# 2026-09-07 with exiftool 13.29 through the full MetadataScrubber pipeline, on
# real iPhone HEICs from <device-corpus>:
#
#   IMG_1034.heic (iPhone 8), Issue 263 dotnet.heic (iPhone XR) and
#   exif-at-eof.heic (iPhone 11 Pro) each came back status=error,
#   verdict=residual_found, with the Apple Display P3 strings 'Copyright Apple
#   Inc., 2017' and 'Display P3' still in the output bytes, and 26 ICC_Profile
#   tags still readable, including DeviceManufacturer, DeviceModel,
#   ProfileDateTime and ProfileID. The three files reported 53 surviving tags
#   each. Asked explicitly, `exiftool -icc_profile:all=` answers "1 image files
#   unchanged".
#
# With Engine.ISOBMFF the same three files come back status=sanitized,
# verdict=verified_clean, 27 surviving tags each, byte length unchanged to the
# byte, and the picture item payloads byte-identical.
#
# WHY COMPLETE, AND WHY IT WAS PARTIAL FOR A DAY. These rows shipped PARTIAL
# for exactly one measured reason, and that reason is now closed. The history is
# kept rather than deleted, because the mechanism is the lesson.
#
#   THE CARRIER. MEASURED 2026-09-07 on Issue 487.heic, a real Galaxy S10+
#   HEIC: a top-level `sefd` box, Samsung Extended Format Data, 106 bytes,
#   holding the ASCII keys `Image_UTC_Data` with a unix-millisecond capture
#   time and `MCC_Data` with a mobile country code. exiftool renders those as
#   Samsung:TimeStamp, a wall clock time carrying the phone's local UTC offset,
#   and Samsung:MCCData, a country. The shipping exiftool engine removed the
#   box; the first cut of the ISOBMFF engine did not, so routing the family
#   here traded a hard failure on every Apple HEIC for a SILENT leak on Samsung
#   ones.
#
#   WHY NOTHING CAUGHT IT, which is trap 10 again and is the part worth
#   remembering. `sefd` stores the time as a digit string and the country as
#   'MCC_Data466'. exiftool RENDERS them as a formatted date and as a country
#   name plus a number, so the needle verify.meaningful_values() would build is
#   a string that is not in the file, the formatted date is dropped by the
#   all-digits rule before it is even a needle, and the integer never survives
#   verify._flatten at all. Total needles for that file: 3. So the run reported
#   status=sanitized, verdict=verified_clean, and the file was still leaking.
#   A verified_clean verdict is not evidence that a vendor carrier is gone.
#
#   THE FIX. isobmff_engine.REMOVED_TYPES now free-fills `sefd` like any other
#   metadata box, length-preserving. MEASURED 2026-09-07 on that same file:
#   output length 1622642 bytes, unchanged to the byte; the literals `sefd`,
#   `Image_UTC_Data`, `MCC_Data`, `SEFH` and `SEFT` all absent from the output
#   bytes; exiftool reports no Samsung group and no MakerNotes group at all;
#   ffmpeg decodes one frame whose framemd5 is identical to the original's.
#
# THE EVIDENCE FOR COMPLETE, which is a measured diff and not a feeling. Every
# still in the real-device corpus was run through the FULL pipeline twice on
# 2026-09-07 with exiftool 13.29, once routed to Engine.EXIFTOOL and once to
# Engine.ISOBMFF, and the surviving tag sets were compared. Counts below are
# `exiftool -G1 -a -s -j -n` tags with the ExifTool, System and File groups
# dropped, so they are NOT comparable to the 53/27 figures quoted further up,
# which were counted a different way:
#
#   file                   original  exiftool routing   isobmff routing
#   IMG_1034.heic               168  81, residual_found  29, verified_clean
#   Issue 263 dotnet.heic       151  55, residual_found  29, verified_clean
#   exif-at-eof.heic            146  55, residual_found  29, verified_clean
#   Issue 487.heic               79  28, verified_clean  28, verified_clean
#   Issue 649.avif              363  19, verified_clean  19, verified_clean
#
#   Tags surviving the ISOBMFF routing that did NOT survive the exiftool
#   routing, per file: 0, 0, 0, 0, 0. The regression set is EMPTY on every
#   file. In the other direction the ISOBMFF routing removes 52, 26, 26, 0 and
#   0 tags that exiftool left behind, all of them the ICC profile.
#
#   Every tag that does survive is codec configuration, brand, image geometry
#   or a byte offset: MajorBrand, CompatibleBrands, HandlerType, the HEVC or
#   AV1 configuration record, ImageSpatialExtent, Rotation, ImagePixelDepth,
#   MediaDataOffset, MediaDataSize, PrimaryItemReference. No provenance, no
#   identity, no time, no place.
#
# WHY NOT KEEP PARTIAL ANYWAY. Because every other PARTIAL row in this table
# names a carrier that was MEASURED to survive: maker notes in the raw
# formats, SttbfAssoc and SttbSavedBy in OLE2, EXIF inside a base64 raster in
# SVG. After the fix this family names none. A PARTIAL that means "we are
# nervous" rather than "this specific thing survives" drains the word, and it
# is not free: scrubber.py downgrades a COMPLETE format to STATUS_ERROR when
# verification is not clean, so PARTIAL forfeits the one AUTOMATIC guard that
# would catch the next leak the residual scan CAN see. Given the engine that
# just shipped a silent leak, forfeiting an automatic guard is the wrong trade.
#
# THE COST OF SAYING COMPLETE, stated rather than glossed:
#   1. scrubber.py:662 now turns any non-clean verification on a .heic, .heif
#      or .avif into status=error. That is more hard failures, not fewer, and
#      it is deliberate: this project prefers a refusal to a quiet partial.
#   2. tests/test_removal.py:59 stops skipping test_complete_formats_verify_
#      clean for these formats. MEASURED 2026-09-07: .heic, .heif and .avif are
#      not in conftest.ALL_KINDS, so that test does not enumerate them today
#      and the change is inert until somebody adds one. tests/test_heic_routing
#      .py makes the equivalent assertion unconditionally on real device files
#      so nothing depends on that.
#   3. tests/test_gps_verify.py requires a COMPLETE format to be GPS-covered or
#      to stay visible in the uncovered list. MEASURED: .heic, .heif and .avif
#      are all in gps_verify.supported_extensions(), so they are covered.
#
# WHAT COMPLETE DOES NOT CLAIM. It claims no KNOWN carrier is left behind, in
# the sense Completeness defines. It does not claim the engine has an opinion
# about a vendor box no corpus file carries: `sefd` was found by inventory, not
# by the residual scan, and the general blindness that hid it is unchanged.
# What is new is that isobmff_engine.DECIDED_TYPES names every box type met at
# the top level or under `moov`, and the inventory tests FAIL on an unlisted
# one, so the next vendor box becomes a red test on the day a file carrying it
# enters the corpus rather than a leak nobody notices. MEASURED 2026-09-07
# across the whole corpus and every synthetic fixture, the only undecided types
# were `sefd` and `wide`; both are now decided. Also unclaimed and unchanged:
# the coded bitstream of a still-image ITEM, where an SEI user-data NAL would
# be out of reach. MEASURED 2026-09-07: zero SEI user-data NALs in the image
# items of all five corpus stills, so that gap has no instance here.
#
# Container.RAW is correct: HEIF stores metadata items uncompressed, and the
# residual scan found the ICC strings in the raw bytes, which is how the defect
# was measured in the first place. rewrites_container is True because the engine
# writes a temporary file and atomically replaces the original, even though the
# output is the same length.
_ISOBMFF_NOTE = (
    "ISO base media box surgery: EXIF, XMP and other non-image items, the ICC "
    "profile in iprp/ipco/colr, item and movie metadata boxes, vendor boxes "
    "including the Samsung `sefd` box, handler names and header timestamps are "
    "removed in place, and the file length and the coded picture are unchanged"
)
_ISOBMFF: Dict[str, FormatSpec] = {
    ext: FormatSpec(Engine.ISOBMFF, Completeness.COMPLETE, Container.RAW, True,
                    _ISOBMFF_NOTE)
    for ext in (".heic", ".heif", ".avif")
}
# End ISOBMFF block.
# ---------------------------------------------------------------------------

CAPABILITIES: Dict[str, FormatSpec] = {}
for _table in (_IMAGE, _PDF, _OOXML, _ODF, _OLE2, _AV, _SVG, _ISOBMFF):
    CAPABILITIES.update(_table)


def deferral_for(path: str) -> Optional[str]:
    """
    Return the reason a format is knowingly not handled yet, or None.

    This exists so the CLI can tell a user "this format is deferred, here is
    why" instead of the same flat "unsupported" it gives for a .txt file. Those
    are different situations and a user acts differently on each.
    """
    _, ext = os.path.splitext(path.lower())
    return DEFERRED.get(ext)


def spec_for(path: str) -> Optional[FormatSpec]:
    """
    Return the capability row for a path, or None when the format is not
    handled. Callers must branch on None rather than on a truthiness test, so
    that an unsupported format is never confused with a format that simply has
    no metadata.
    """
    _, ext = os.path.splitext(path.lower())
    return CAPABILITIES.get(ext)


def is_supported(path: str) -> bool:
    """
    Kept for drop-in compatibility with the the parent project ExifSanitizer API. Prefer
    spec_for(), which tells you which engine will run and what it guarantees.
    """
    return spec_for(path) is not None


def supported_extensions() -> list:
    """Sorted extension list, for CLI help and documentation."""
    return sorted(CAPABILITIES)
