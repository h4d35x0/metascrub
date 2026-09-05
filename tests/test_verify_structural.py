"""
The structural exclusions in verify.py, and the proof they did not overcorrect.

An exclusion in `meaningful_values()` is a decision to STOP SEARCHING for
something. It is the only kind of change in this project that can turn a leaking
file into a VERIFIED_CLEAN verdict, so every exclusion has to arrive with the
test that shows what it still catches. The project's own lesson from the OLE2
directory-name collision says it plainly: exclude a REGION, not a STRING, and
write the overcorrection test in the same change.

These tests cover `_STRUCTURAL_VALUES`, the (group, tag, value-shape) exclusion
added for SVG. They deliberately do NOT ask an engine whether anything worked:
the end-to-end case reads a real file with exiftool and then searches the file's
own bytes for the value.
"""

from __future__ import annotations

import pytest

from conftest import HAVE_EXIFTOOL, sentinel
from metascrub.capabilities import Completeness, Container, Engine, FormatSpec
from metascrub.verify import meaningful_values, residual_scan

_SVG_NS = "http://www.w3.org/2000/svg"

# Container.RAW is what makes residual_scan search the file bytes directly. The
# engine field is not consulted by the scan; only the container is.
_RAW_SPEC = FormatSpec(Engine.EXIFTOOL, Completeness.PARTIAL, Container.RAW, False, "test")

# Groups exiftool actually emits for the formats this tool ships, plus SVG. Used
# to assert the exclusion is scoped to one group rather than applied globally.
_SHIPPED_GROUPS = [
    "XMP", "EXIF", "IFD0", "PNG", "GIF", "RIFF", "QuickTime", "Matroska",
    "PDF", "XML", "FlashPix", "MS-DOC", "ID3", "Vorbis", "ICC_Profile",
]


# WHAT THE EXCLUSION SUPPRESSES

def test_the_svg_namespace_declaration_is_not_a_needle():
    """
    The measured false positive this exclusion exists for.

    Measured 2026-09-04, exiftool 13.29, project read flags (-G -n): an SVG with
    every trace of metadata removed still reports SVG:Xmlns, and its value is
    the namespace without which the file is not an SVG. Searching for it made a
    genuinely clean SVG verify as residual_found.
    """
    assert meaningful_values({"SVG:Xmlns": _SVG_NS}) == set()


@pytest.mark.parametrize("value", [
    "xMidYMid meet", "xMidYMid slice", "xMinYMin meet", "xMaxYMax slice",
    "defer xMidYMid meet", "xMidYMid",
])
def test_preserve_aspect_ratio_grammar_is_not_a_needle(value):
    """Every string the SVG grammar accepts is drawn from a closed keyword set."""
    assert meaningful_values({"SVG:PreserveAspectRatio": value}) == set()


# WHAT THE EXCLUSION MUST STILL CATCH
#
# Three positions, one per axis of the (group, tag, value) key. If any of these
# stops finding its secret, the exclusion has become a blind spot.

def test_a_secret_smuggled_into_the_xmlns_value_is_still_a_needle():
    """
    Axis 1: same group, same tag, different value.

    A namespace URI is attacker-controlled text in a position nobody inspects.
    An exclusion keyed on the tag NAME would stop searching for this; the
    exclusion keyed on the exact structural value does not.
    """
    secret = f"http://{sentinel('xmlnsleak')}.example/ns"
    assert meaningful_values({"SVG:Xmlns": secret}) == {secret}


def test_a_secret_smuggled_into_preserve_aspect_ratio_is_still_a_needle():
    """Axis 1 again, on the other excluded tag."""
    secret = sentinel("parleak")
    assert meaningful_values({"SVG:PreserveAspectRatio": secret}) == {secret}


@pytest.mark.parametrize("group", _SHIPPED_GROUPS)
@pytest.mark.parametrize("tag", ["Xmlns", "PreserveAspectRatio"])
def test_the_same_tag_name_in_another_group_is_still_a_needle(group, tag):
    """
    Axis 2: same tag name, different format.

    This is the cross-product that a single illustrative example would miss. It
    is the difference between this change and the wider one it replaced: a tag
    name added to _STRUCTURAL_TAGS is matched globally, so a value carried under
    a tag of that name in a docx, an mp4 or a PDF would never be searched for
    again.
    """
    secret = sentinel("crossgroup")
    assert meaningful_values({f"{group}:{tag}": secret}) == {secret}


@pytest.mark.parametrize("key", [
    "SVG:Docname", "SVG:Version", "SVG:Export-filename", "SVG:Title",
    "SVG:Desc", "SVG:MetadataID", "XMP:WorkCreatorAgentTitle",
])
def test_every_other_svg_tag_is_untouched(key):
    """Axis 3: same group, different tag. Only two tags were excluded."""
    secret = sentinel("othertag")
    assert meaningful_values({key: secret}) == {secret}


def test_the_structural_value_does_not_hide_the_same_string_under_another_tag():
    """
    The exact string that IS suppressed under SVG:Xmlns must still be a needle
    when it turns up as the value of a tag that carries provenance. This is the
    collision the OLE2 lesson was about, stated for the SVG case.
    """
    assert meaningful_values({"SVG:Docname": _SVG_NS}) == {_SVG_NS}


# END TO END, AGAINST THE BYTES
#
# The rule this project rests on: never ask an engine whether it succeeded. The
# test below reads a real file with exiftool and then searches that same file's
# bytes for the value.

def _write_svg(path, preserve_aspect_ratio: str, creator: str) -> None:
    text = (
        '<?xml version="1.0" encoding="UTF-8"?>\n'
        '<svg xmlns="http://www.w3.org/2000/svg"\n'
        '     xmlns:dc="http://purl.org/dc/elements/1.1/"\n'
        '     xmlns:cc="http://creativecommons.org/ns#"\n'
        '     xmlns:rdf="http://www.w3.org/1999/02/22-rdf-syntax-ns#"\n'
        '     width="64" height="64" viewBox="0 0 64 64"\n'
        f'     preserveAspectRatio="{preserve_aspect_ratio}">\n'
        '  <metadata><rdf:RDF><cc:Work rdf:about="">\n'
        f'    <dc:creator><cc:Agent><dc:title>{creator}</dc:title></cc:Agent></dc:creator>\n'
        '  </cc:Work></rdf:RDF></metadata>\n'
        '  <rect x="1" y="1" width="10" height="10"/>\n'
        '</svg>\n'
    )
    with open(path, "w", encoding="utf-8", newline="") as fh:
        fh.write(text)


@pytest.mark.skipif(not HAVE_EXIFTOOL, reason="exiftool is required to read a baseline")
def test_a_real_leak_is_still_found_in_the_bytes_of_a_real_file():
    """
    The overcorrection test in its strongest form: a genuine secret in the
    position a real leak occupies, read through the real exiftool session and
    searched for in the real file bytes.

    Two secrets, one in each of the positions the exclusion touches:
      - dc:creator, ordinary document metadata that must always be a needle,
      - preserveAspectRatio, the attribute the exclusion covers, holding a value
        that is not a preserveAspectRatio.
    """
    import tempfile
    import os

    from metascrub import exif_io

    creator = sentinel("e2ecreator")
    smuggled = sentinel("e2epar")
    directory = tempfile.mkdtemp(prefix="metascrub-structural-")
    path = os.path.join(directory, "leak.svg")
    try:
        _write_svg(path, smuggled, creator)
        read = exif_io.session().read(path)
        assert read.ok, read.error

        needles = meaningful_values(read.metadata)
        assert creator in needles, "dc:creator did not survive as a needle"
        assert smuggled in needles, (
            "the exclusion swallowed a value that is not a preserveAspectRatio"
        )

        survivors = residual_scan(path, _RAW_SPEC, sorted(needles))
        assert creator in survivors
        assert smuggled in survivors
        # And the structural value is not among them, which is the whole point.
        assert _SVG_NS not in survivors
    finally:
        exif_io.close_session()
        import shutil
        shutil.rmtree(directory, ignore_errors=True)


@pytest.mark.skipif(not HAVE_EXIFTOOL, reason="exiftool is required to read a baseline")
def test_a_structurally_clean_svg_yields_no_needles():
    """
    The other half of the measurement: an SVG carrying nothing but structure
    produces an EMPTY needle set, so a scrub of it can verify clean.

    This is weak evidence on its own and is recorded as such: zero needles means
    the residual scan proves nothing about this particular file. It is here to
    pin the false positive that made SVG unshippable, not to claim more.
    """
    import tempfile
    import os

    from metascrub import exif_io

    directory = tempfile.mkdtemp(prefix="metascrub-structural-")
    path = os.path.join(directory, "plain.svg")
    try:
        text = (
            '<?xml version="1.0" encoding="UTF-8"?>\n'
            '<svg xmlns="http://www.w3.org/2000/svg" width="64" height="64"\n'
            '     viewBox="0 0 64 64" preserveAspectRatio="xMidYMid meet">\n'
            '  <rect x="1" y="1" width="10" height="10"/>\n'
            '</svg>\n'
        )
        with open(path, "w", encoding="utf-8", newline="") as fh:
            fh.write(text)
        read = exif_io.session().read(path)
        assert read.ok, read.error
        assert meaningful_values(read.metadata) == set()
    finally:
        exif_io.close_session()
        import shutil
        shutil.rmtree(directory, ignore_errors=True)


# ---------------------------------------------------------------------------
# PDF VIEWER PREFERENCES
#
# Same shape as the SVG pair above, found the same way: by running the tool on
# a real file rather than by reading the table. A PDF with bookmarks came out
# of the pdf engine with its author GONE from the output bytes and was reported
# FAILED / STILL LEAKING on "UseOutlines", the viewer preference that makes a
# bookmarked PDF open with its bookmark pane showing.
# ---------------------------------------------------------------------------

_PAGE_MODE_VALUES = [
    "UseNone", "UseOutlines", "UseThumbs",
    "FullScreen", "UseOC", "UseAttachments",
]
_PAGE_LAYOUT_VALUES = [
    "SinglePage", "OneColumn",
    "TwoColumnLeft", "TwoColumnRight", "TwoPageLeft", "TwoPageRight",
]


@pytest.mark.parametrize("value", _PAGE_MODE_VALUES)
def test_page_mode_enum_is_not_a_needle(value):
    """
    ISO 32000-1 table 28 makes /PageMode a CLOSED enumeration, so a matching
    value carries no user information.

    Parametrized over the whole enum on purpose. Ten of these twelve values are
    long enough to clear _MIN_NEEDLE; UseNone and UseOC escaped only by being
    shorter than eight characters, and a fix keyed on length rather than on the
    enum would have left that difference in place as a silent inconsistency.
    """
    assert meaningful_values({"PDF:PageMode": value}) == set()


@pytest.mark.parametrize("value", _PAGE_LAYOUT_VALUES)
def test_page_layout_enum_is_not_a_needle(value):
    """/PageLayout is the same kind of closed enumeration as /PageMode."""
    assert meaningful_values({"PDF:PageLayout": value}) == set()


@pytest.mark.parametrize("tag", ["PageMode", "PageLayout"])
def test_a_secret_smuggled_into_a_viewer_preference_is_still_a_needle(tag):
    """
    Axis 1: same group, same tag, a value that is not in the enumeration.

    This is where a smuggled value would sit, and it is the whole reason the
    entry carries a predicate over the value instead of suppressing the tag.
    """
    secret = sentinel("viewerpref")
    assert meaningful_values({f"PDF:{tag}": secret}) == {secret}


@pytest.mark.parametrize("group", [g for g in _SHIPPED_GROUPS if g != "PDF"])
@pytest.mark.parametrize("tag", ["PageMode", "PageLayout"])
def test_a_viewer_preference_tag_name_in_another_group_is_still_a_needle(group, tag):
    """Axis 2: same tag name, different format. The key is scoped to PDF."""
    secret = sentinel("pdfcrossgroup")
    assert meaningful_values({f"{group}:{tag}": secret}) == {secret}


@pytest.mark.parametrize("key", [
    "PDF:Author", "PDF:Creator", "PDF:Producer", "PDF:Title",
    "PDF:Subject", "PDF:Keywords", "PDF:CreateDate",
])
def test_every_other_pdf_tag_is_untouched(key):
    """Axis 3: same group, different tag. Only two tags were excluded."""
    secret = sentinel("otherpdftag")
    assert meaningful_values({key: secret}) == {secret}


@pytest.mark.parametrize("value", ["UseOutlines", "TwoColumnLeft"])
def test_an_enum_string_under_an_identity_tag_is_still_a_needle(value):
    """
    A user whose name or title happens to be one of these strings is still
    entitled to have it found. Suppression is bound to the viewer-preference
    tags, not to the strings themselves.
    """
    assert meaningful_values({"PDF:Author": value}) == {value}


@pytest.mark.skipif(not HAVE_EXIFTOOL, reason="exiftool is required")
def test_a_bookmarked_pdf_verifies_clean_end_to_end():
    """
    The reported defect, asserted against the OUTPUT BYTES.

    Builds a PDF with real bookmarks and an author, runs the whole pipeline,
    and requires two things at once: the author is gone from the bytes, and the
    verdict is clean. Before this fix the first held and the second did not,
    which is the worst combination available, because the tool did its job and
    then told the user it had failed.
    """
    import os
    import shutil
    import tempfile

    pikepdf = pytest.importorskip("pikepdf")
    from metascrub import MetadataScrubber

    directory = tempfile.mkdtemp(prefix="metascrub-pdfbookmarks-")
    path = os.path.join(directory, "book.pdf")
    secret = sentinel("pdfbookmark")
    try:
        pdf = pikepdf.Pdf.new()
        for _ in range(3):
            pdf.add_blank_page(page_size=(200, 200))
        with pdf.open_outline() as outline:
            outline.root.append(pikepdf.OutlineItem("Chapter One", pdf.pages[0].obj))
            outline.root.append(pikepdf.OutlineItem("Chapter Two", pdf.pages[1].obj))
        pdf.Root["/PageMode"] = pikepdf.Name("/UseOutlines")
        pdf.docinfo["/Author"] = secret
        pdf.save(path)

        with open(path, "rb") as fh:
            assert secret.encode() in fh.read(), "fixture did not store the sentinel"

        with MetadataScrubber(backup=False) as scrubber:
            result = scrubber.sanitize_file(path, remove_all=True)

        with open(path, "rb") as fh:
            after = fh.read()
        assert secret.encode() not in after, "the real leak survived"
        assert b"UseOutlines" in after, "the viewer preference should not be destroyed"
        assert result["verification"]["clean"], (
            "a correctly cleaned PDF was reported as still leaking: %r"
            % (result["verification"],)
        )
    finally:
        shutil.rmtree(directory, ignore_errors=True)
