"""
SVG.

Two things have to be true at once here, and they pull against each other more
than in any other format this tool handles: the editor's fingerprints have to be
gone, and the picture has to still be a picture. So the assertions come in
matched pairs. Every "this is gone" test has a "this survived" test beside it,
because the cheapest way to pass a removal test is to delete the drawing.

Every assertion is against the OUTPUT BYTES or against a parse of them. None of
them asks an engine whether it succeeded.
"""

from __future__ import annotations

import base64
import os
import re
import xml.etree.ElementTree as ET

import pytest

from conftest import make_svg, raw_contains, sentinel
from metascrub import (
    STATUS_ERROR, STATUS_SANITIZED, Completeness, MetadataScrubber, spec_for,
)


def _scrub(path):
    with MetadataScrubber(backup=False) as scrubber:
        return scrubber.sanitize_file(path, remove_all=True)


def _text(path) -> str:
    with open(path, "rb") as fh:
        return fh.read().decode("utf-8")


# WHAT MUST BE GONE

@pytest.mark.parametrize("carrier", [
    "the XML comment",
    "sodipodi:docname",
    "inkscape:export-filename",
    "the root <title>",
    "the root <desc>",
    "dc:title, dc:creator, dc:rights and dc:description",
    "sodipodi:absref",
])
def test_every_metadata_sentinel_is_gone_from_the_bytes(carrier, tmp_path):
    """
    One sentinel carries all seven carriers, so one byte search covers them all.
    The parametrisation exists to name each carrier in the test report: a bare
    "sentinel is gone" tells a later reader nothing about what was checked.
    """
    path, value = make_svg(tmp_path)
    assert raw_contains(path, value), f"fixture did not carry {carrier}"

    result = _scrub(path)
    assert result["status"] == STATUS_SANITIZED, result.get("error")
    assert not raw_contains(path, value), f"{carrier} survived the strip"


def test_the_editor_is_no_longer_named_anywhere(tmp_path):
    """
    Provenance is not only in the values. A file with every inkscape: attribute
    removed but the inkscape namespace still declared still says which editor
    made it, and so does the "Created with Inkscape" comment.
    """
    path, _ = make_svg(tmp_path)
    _scrub(path)
    text = _text(path).lower()
    assert "inkscape" not in text
    assert "sodipodi" not in text
    assert "<!--" not in text


def test_the_absolute_export_path_is_gone_while_the_drawing_remains(tmp_path):
    """
    inkscape:export-filename is an absolute path into the author's home
    directory. It is the single most identifying string in a typical Inkscape
    file and it is free to remove.
    """
    path, value = make_svg(tmp_path)
    assert "C:" in _text(path)
    _scrub(path)
    text = _text(path)
    assert value not in text
    assert "Desktop" not in text
    assert "<rect" in text, "the drawing was removed along with the path"


def test_absref_is_removed_and_the_href_beside_it_is_not(tmp_path):
    """
    sodipodi:absref is an absolute filesystem path that duplicates the sibling
    xlink:href. exiftool does not read it, so the residual scan is blind to it
    and only this test can prove it went. Removing it cannot change what is
    fetched, because the href is what is fetched.
    """
    path, _ = make_svg(tmp_path)
    assert "sodipodi:absref" in _text(path)
    _scrub(path)
    text = _text(path)
    assert "absref" not in text
    assert 'xlink:href="file:///C:/Users/' in text, (
        "removing absref took the real reference with it"
    )


# WHAT MUST SURVIVE
#
# This is the half that stops someone "cleaning" a drawing into a blank page.

@pytest.mark.parametrize("fragment", [
    'xmlns="http://www.w3.org/2000/svg"',   # without this it is not an SVG
    'xmlns:xlink=',                         # still used by <use> and <image>
    'viewBox="0 0 744 1052"',
    'width="744"',
    'height="1052"',
    'preserveAspectRatio="xMidYMid meet"',
    'version="1.1"',
    '<rect',
    'style="fill:url(#gradient3757)"',      # the paint reference
    'id="gradient3757"',                    # and the gradient it points at
    '<use id="use1" xlink:href="#rect1"',   # an internal reference by id
    'transform="translate(0,80)"',
    '<style>',
    '.keep { stroke: #00ff00; }',           # CSS hidden inside CDATA
])
def test_rendering_survives_the_strip(fragment, tmp_path):
    path, _ = make_svg(tmp_path)
    _scrub(path)
    assert fragment in _text(path), f"the strip removed {fragment!r}"


@pytest.mark.parametrize("kept", ["svgbody", "svgnested"])
def test_user_content_survives(kept, tmp_path):
    """
    <text> content is the document's words. A <title> nested inside a shape is
    that shape's accessibility name, which a screen reader announces.

    The root/nested split is not a guess. Measured 2026-09-04 with exiftool
    13.29 on a file carrying both: only the ROOT <title> and <desc> are reported
    (as SVG:Title and SVG:Desc), so only those become residual-scan needles.
    A nested one can never produce a residual_found, which is what makes keeping
    it compatible with the verification contract.
    """
    path, _ = make_svg(tmp_path)
    _scrub(path)
    assert raw_contains(path, sentinel(kept))


def test_a_comment_inside_a_style_element_is_not_eaten(tmp_path):
    """
    The legacy <style><!-- css --></style> idiom hides a real stylesheet inside
    an XML comment. A comment remover that does not know about protected regions
    deletes the styling and changes the picture.
    """
    path = str(tmp_path / "styled.svg")
    css = ".brand { fill: #123456; }"
    with open(path, "w", encoding="utf-8", newline="") as fh:
        fh.write(
            '<?xml version="1.0" encoding="UTF-8"?>\n'
            '<!-- Created with Inkscape -->\n'
            '<svg xmlns="http://www.w3.org/2000/svg" width="10" height="10"\n'
            '     viewBox="0 0 10 10" sodipodi:docname="x.svg"\n'
            '     xmlns:sodipodi="http://sodipodi.sourceforge.net/DTD/sodipodi-0.0.dtd">\n'
            '  <style type="text/css"><!-- ' + css + ' --></style>\n'
            '  <rect class="brand" x="1" y="1" width="2" height="2"/>\n'
            '</svg>\n'
        )
    _scrub(path)
    text = _text(path)
    assert css in text, "the stylesheet inside the comment was deleted"
    assert "Created with Inkscape" not in text, "the real comment survived"


def test_file_still_parses_as_xml(tmp_path):
    """Cheap, and it catches a regex that ate a closing tag."""
    path, _ = make_svg(tmp_path)
    ET.parse(path)  # the fixture is well formed to begin with
    _scrub(path)
    tree = ET.parse(path)
    assert tree.getroot().tag.endswith("svg")


# THE PARTIAL, AND THE PROOF THAT IT SAYS THE TRUTH
#
# A PARTIAL claim nothing checks is just a comment.

def test_svg_is_declared_partial_and_says_what_remains():
    spec = spec_for("fixture.svg")
    assert spec is not None, "svg is not in the capability table"
    assert spec.completeness is Completeness.PARTIAL
    note = spec.note.lower()
    assert "base64" in note
    assert "external local file reference" in note


def test_the_declared_residue_is_exactly_what_the_note_says(tmp_path):
    """
    The measured hole, asserted rather than described.

    An SVG can carry a whole JPEG as a base64 data: URI, EXIF and GPS included.
    exiftool reports nothing about it, so there is no needle; residual_scan
    searches raw bytes while the value is base64-wrapped, so it finds nothing
    either. The file therefore reports VERIFIED_CLEAN while carrying a
    photographer's name. That is the same failure class this whole project
    exists to prevent, and it is the reason .svg is PARTIAL rather than
    COMPLETE.

    This test decodes the data URI out of the OUTPUT and finds the sentinel in
    the decoded bytes, which is the only way to see it at all.
    """
    path, _ = make_svg(tmp_path)
    result = _scrub(path)
    text = _text(path)

    match = re.search(r"data:image/jpeg;base64,([A-Za-z0-9+/=]+)", text)
    assert match, "the embedded image was removed; this test no longer measures the hole"
    decoded = base64.b64decode(match.group(1))
    assert sentinel("svgembedded").encode() in decoded, (
        "the embedded EXIF is gone, so the PARTIAL note now overstates the residue"
    )

    # And the two checks the tool makes are both blind to it, which is the
    # point. If either of these ever starts catching it, the note is wrong.
    assert not raw_contains(path, sentinel("svgembedded"))
    assert result["verification"]["verdict"] == "verified_clean"
    assert result["completeness"] == "partial"

    notes = [item for item in result["removed_fields"] if item.startswith("NOTE:")]
    assert any("base64" in item for item in notes), (
        "the residue survives and nothing told the user"
    )


def test_inkscape_css_properties_are_reported_not_removed(tmp_path):
    """
    Found by running the engine against a genuine Inkscape file rather than only
    against the hand-authored fixture, which is why it is here.

    Measured 2026-09-04 on Inkscape 0.48.3.1 output: after every inkscape:
    attribute, the <sodipodi:namedview>, the <metadata> block and both namespace
    declarations were removed, the string "inkscape" was STILL in the file 15
    times. Every one was "-inkscape-font-specification:" inside a style
    attribute. It is a CSS property, not a namespaced attribute, so the
    attribute sweep does not see it, and it still names the editor.

    It is reported rather than removed: a style attribute is the drawing, and
    Inkscape uses that property to round-trip a font choice, so deleting it can
    change which face the file resolves to. Removing it belongs behind an
    explicit flag.
    """
    path = str(tmp_path / "styled.svg")
    style = ("font-size:12px;font-family:Droid Sans Mono;"
             "-inkscape-font-specification:Droid Sans Mono")
    with open(path, "w", encoding="utf-8", newline="") as fh:
        fh.write(
            '<?xml version="1.0" encoding="UTF-8"?>\n'
            '<svg xmlns="http://www.w3.org/2000/svg"\n'
            '     xmlns:inkscape="http://www.inkscape.org/namespaces/inkscape"\n'
            '     width="10" height="10" viewBox="0 0 10 10"\n'
            '     inkscape:version="0.48.3.1 r9886">\n'
            '  <text x="1" y="5" style="' + style + '">hello</text>\n'
            '</svg>\n'
        )
    result = _scrub(path)
    text = _text(path)

    assert "inkscape:version" not in text, "the namespaced attribute survived"
    assert style in text, "the style attribute was edited"
    notes = [item for item in result["removed_fields"] if item.startswith("NOTE:")]
    assert any("-inkscape-" in item for item in notes), (
        "the editor is still named in the file and nothing told the user"
    )


def test_the_partial_note_names_every_reported_survivor(tmp_path):
    """
    The capability note is a promise about what remains. Every NOTE: the engine
    can emit has to be findable in it, or the note is out of date and a user
    reading only the table is misinformed.
    """
    note = spec_for("x.svg").note.lower()
    for phrase in ("base64", "external local file reference", "-inkscape-"):
        assert phrase in note, f"the .svg note does not mention {phrase}"


def test_external_local_reference_is_reported_not_removed(tmp_path):
    """
    A file:/// href leaks a username. Removing it deletes the image from the
    drawing, which this tool does not do to user content, so it is reported.
    Measured: exiftool does not read it, so the note is the only signal.
    """
    path, _ = make_svg(tmp_path)
    result = _scrub(path)

    assert raw_contains(path, sentinel("svguser")), (
        "the external reference was deleted; that is content destruction"
    )
    notes = [item for item in result["removed_fields"] if item.startswith("NOTE:")]
    assert any("external local file reference" in item for item in notes)
    assert any(sentinel("svguser") in item for item in notes), (
        "the note must name the reference, or a user cannot act on it"
    )


# VERIFICATION
#
# The standing project rule: the verdict and the bytes must agree.

def test_verification_agrees_with_the_bytes(tmp_path):
    """
    This is the regression guard for the _STRUCTURAL_VALUES change in verify.py.

    Before it, this file verified as residual_found on the value of SVG:Xmlns,
    "http://www.w3.org/2000/svg", the namespace declaration without which the
    file is not an SVG. Every SVG scrub would have reported a leak that was not
    there.
    """
    path, value = make_svg(tmp_path)
    result = _scrub(path)

    verification = result["verification"]
    assert not raw_contains(path, value)
    assert verification["verdict"] == "verified_clean", verification
    assert verification["residual_values"] == []
    # The scan has to have actually looked at something. A verdict reached over
    # an empty needle set proves nothing, and would pass this test silently.
    assert verification["checked_values"] > 0


def test_a_planted_survivor_is_still_caught(tmp_path):
    """
    The other direction, which is the one that matters: prove the SVG path can
    still FAIL. A file whose metadata was not removed must report residual_found
    rather than sailing through on the structural exclusions.
    """
    from metascrub.capabilities import spec_for as _spec_for
    from metascrub.verify import meaningful_values, residual_scan
    from metascrub import exif_io

    path, value = make_svg(tmp_path)
    before = exif_io.session().read(path)
    assert before.ok, before.error
    needles = meaningful_values(before.metadata)
    exif_io.close_session()

    # No scrub at all. Every needle must still be found.
    survivors = residual_scan(path, _spec_for(path), sorted(needles))
    assert value in survivors
    assert len(survivors) >= 4, survivors


# BOUNDARIES

def test_an_svg_with_nothing_to_remove_is_left_byte_identical(tmp_path):
    """
    Boundary case, and the plan's expectation had to be corrected against a
    measurement.

    The plan said this file should report STATUS_CLEAN. It cannot: exiftool
    reports SVG:Xmlns, ImageWidth, ImageHeight and ViewBox for ANY svg, so
    _real_tags() is never empty and the scrubber never takes its CLEAN branch.
    What actually matters is the property underneath that expectation, which is
    asserted here instead: nothing is removed, so not one byte changes.
    """
    path = str(tmp_path / "plain.svg")
    with open(path, "w", encoding="utf-8", newline="") as fh:
        fh.write(
            '<?xml version="1.0" encoding="UTF-8"?>\n'
            '<svg xmlns="http://www.w3.org/2000/svg" width="10" height="10"\n'
            '     viewBox="0 0 10 10"><rect x="1" y="1" width="2" height="2"/></svg>\n'
        )
    with open(path, "rb") as fh:
        original = fh.read()

    result = _scrub(path)

    with open(path, "rb") as fh:
        assert fh.read() == original, "a file with no metadata was rewritten anyway"
    assert result["status"] != STATUS_ERROR, result.get("error")
    assert result["removed_fields"] == []

    # CHANGED 2026-09-07 with the zero-needle decision, and this fixture is
    # exactly the case that decision is about. Measured with exiftool 13.29
    # through this project's own read flags, the four tags above are
    # SVG:Xmlns (excluded as the namespace declaration by _STRUCTURAL_VALUES),
    # SVG:ImageWidth, SVG:ImageHeight and SVG:ViewBox "0 0 10 10" (all
    # numeric). So `meaningful_values()` returns the empty set, the residual
    # scan searches this output for nothing, and the old "verified_clean"
    # was a pass over an empty measurement.
    #
    # Nothing about the file changed and nothing about the removal changed;
    # the tool stopped claiming it had proven something it had not.
    assert result["verification"]["verdict"] == "no_baseline_values"
    assert result["verification"]["checked_values"] == 0
    assert result["verification"]["clean"] is False


def test_a_malformed_svg_is_refused_rather_than_reported_clean(tmp_path):
    """
    Was a pinned DEFECT here, asserting the wrong behaviour on purpose. Fixed
    2026-09-04 on fix/unparseable-reported-clean; this now asserts the fix.

    Measured 2026-09-04, exiftool 13.29, on a truncated SVG carrying
    sodipodi:docname:

        [ExifTool] Warning  : XMP format error (no closing tag for svg) [x2]
        [File]     FileType : SVG
        (no [SVG] tags at all)

    exiftool identifies the file, does not fail, and surfaces zero SVG tags.
    Everything it emits sat in the File and ExifTool pseudo-groups, so
    _real_tags() was empty and MetadataScrubber.sanitize_file() took its
    STATUS_CLEAN short circuit BEFORE any engine ran. The user was told "no
    metadata carriers found" about a file that carries an editor's docname.

    That was never the fail-open MetadataRead fixed: the read genuinely
    succeeded. It was the orchestrator treating "the reader found nothing" and
    "there is nothing" as the same state when the reader had also said it could
    not parse the document. ExifSession.read() now returns
    ReadOutcome.UNPARSED for that, and an unparseable baseline is an ERROR for
    the same reason an unreadable one is: it gives verify.py no needles, so
    anything the engine did afterwards would be unverifiable.

    The orchestrator-level cross-product, both populations and every format,
    lives in tests/test_unparseable.py. This one stays here because it is the
    file the defect was found on and it must never come back on SVG.
    """
    path = str(tmp_path / "truncated.svg")
    value = sentinel("svgtruncated")
    with open(path, "wb") as fh:
        fh.write(
            ('<?xml version="1.0" encoding="UTF-8"?>\n'
             '<svg xmlns="http://www.w3.org/2000/svg" width="10" height="10"\n'
             '     sodipodi:docname="' + value + '.svg"><rect x="1" y="1"'
             ).encode("utf-8")
        )
    with open(path, "rb") as fh:
        original = fh.read()

    result = _scrub(path)

    assert result["status"] == STATUS_ERROR, result
    assert "could not parse" in result["error"]
    # Never a claim that anything was done to it.
    assert result["removed_fields"] == []
    assert "verification" not in result
    # A refusal must leave the file exactly as it was. The sentinel is still
    # there, and that is honest: we said we could not clean it.
    with open(path, "rb") as fh:
        assert fh.read() == original
    assert raw_contains(path, value)


def test_line_endings_are_not_rewritten(tmp_path):
    """
    An LF-only file must come out LF-only. A Windows run that quietly converts
    every line ending produces a 100 percent diff for no reason, and this repo's
    own history says how expensive that class of mistake is.
    """
    path, _ = make_svg(tmp_path)
    with open(path, "rb") as fh:
        assert b"\r\n" not in fh.read(), "the fixture was not LF-only to begin with"
    _scrub(path)
    with open(path, "rb") as fh:
        assert b"\r\n" not in fh.read()


def test_crlf_line_endings_are_not_rewritten_either(tmp_path):
    """The same property in the other direction, so neither is special-cased."""
    path = str(tmp_path / "crlf.svg")
    value = sentinel("svgcrlf")
    with open(path, "wb") as fh:
        fh.write(
            ('<?xml version="1.0" encoding="UTF-8"?>\r\n'
             '<!-- ' + value + ' -->\r\n'
             '<svg xmlns="http://www.w3.org/2000/svg" width="10" height="10"\r\n'
             '     viewBox="0 0 10 10"><rect x="1" y="1" width="2" height="2"/></svg>\r\n'
             ).encode("utf-8")
        )
    _scrub(path)
    with open(path, "rb") as fh:
        blob = fh.read()
    assert value.encode() not in blob
    assert b"\r\n" in blob, "CRLF line endings were converted"
    # Every LF must still be part of a CRLF pair. A bare LF would mean the
    # engine normalised part of the file and left the rest.
    assert blob.count(b"\n") == blob.count(b"\r\n")


def test_non_ascii_content_survives_byte_for_byte(tmp_path):
    """
    The engine decodes as latin-1 for byte transparency. That has to be
    invisible to a UTF-8 document: text the strip does not touch must come out
    with the same bytes it went in with.
    """
    path = str(tmp_path / "utf8.svg")
    words = "\u00e9\u00e0\u4f60\u597d"
    with open(path, "w", encoding="utf-8", newline="") as fh:
        fh.write(
            '<?xml version="1.0" encoding="UTF-8"?>\n'
            '<!-- ' + sentinel("svgutf8") + ' -->\n'
            '<svg xmlns="http://www.w3.org/2000/svg" width="10" height="10"\n'
            '     viewBox="0 0 10 10"><text x="1" y="5">' + words + '</text></svg>\n'
        )
    _scrub(path)
    with open(path, "rb") as fh:
        blob = fh.read()
    assert words.encode("utf-8") in blob, "non-ASCII text was re-encoded or mangled"
    assert sentinel("svgutf8").encode() not in blob
    ET.parse(path)


def test_a_utf8_bom_is_preserved(tmp_path):
    """Some editors write one. Dropping it is a byte change nobody asked for."""
    path = str(tmp_path / "bom.svg")
    with open(path, "wb") as fh:
        fh.write(b"\xef\xbb\xbf")
        fh.write(
            ('<?xml version="1.0" encoding="UTF-8"?>\n'
             '<!-- ' + sentinel("svgbom") + ' -->\n'
             '<svg xmlns="http://www.w3.org/2000/svg" width="10" height="10"\n'
             '     viewBox="0 0 10 10"><rect x="1" y="1" width="2" height="2"/></svg>\n'
             ).encode("utf-8")
        )
    _scrub(path)
    with open(path, "rb") as fh:
        blob = fh.read()
    assert blob.startswith(b"\xef\xbb\xbf"), "the BOM was dropped"
    assert sentinel("svgbom").encode() not in blob


def test_a_namespace_declaration_still_in_use_is_kept(tmp_path):
    """
    The unused-declaration removal is checked, not assumed. A document that
    still uses the prefix after the strip must keep the declaration, or it stops
    being well-formed XML.
    """
    path = str(tmp_path / "used.svg")
    with open(path, "w", encoding="utf-8", newline="") as fh:
        fh.write(
            '<?xml version="1.0" encoding="UTF-8"?>\n'
            '<svg xmlns="http://www.w3.org/2000/svg"\n'
            '     xmlns:inkscape="http://www.inkscape.org/namespaces/inkscape"\n'
            '     width="10" height="10" viewBox="0 0 10 10">\n'
            '  <!-- ' + sentinel("svgnsused") + ' -->\n'
            '  <inkscape:custom id="c1"/>\n'
            '  <rect x="1" y="1" width="2" height="2"/>\n'
            '</svg>\n'
        )
    _scrub(path)
    text = _text(path)
    assert "<inkscape:custom" in text, "an element was removed, not just attributes"
    assert "xmlns:inkscape" in text, (
        "the declaration went while the prefix was still in use; the file is "
        "no longer well-formed"
    )
    ET.parse(path)


def test_the_engine_refuses_to_write_a_file_it_broke(tmp_path, monkeypatch):
    """
    The last line of defence. If a future pattern eats a closing tag, the user's
    file must not be the place that gets found out.
    """
    from metascrub.engines import svg_engine

    path, value = make_svg(tmp_path)
    with open(path, "rb") as fh:
        original = fh.read()

    monkeypatch.setattr(
        svg_engine.SvgEngine, "_clean",
        lambda self, text: (text.replace("</svg>", ""), ["broken on purpose"]),
    )
    result = _scrub(path)

    assert result["status"] == STATUS_ERROR
    assert "refusing to write" in result["error"]
    with open(path, "rb") as fh:
        assert fh.read() == original, "a broken rewrite reached the file anyway"


def test_selective_removal_is_refused_rather_than_half_done(tmp_path):
    """The engine cannot address individual fields, and must say so."""
    path, value = make_svg(tmp_path)
    with MetadataScrubber(backup=False) as scrubber:
        result = scrubber.sanitize_file(path, fields_to_remove=["Docname"])
    assert result["status"] == STATUS_ERROR
    assert "remove_all" in result["error"]
    assert raw_contains(path, value), "a refused run modified the file"


def test_the_fixture_source_has_no_control_characters(tmp_path):
    """
    The project's own trap: writing docs and fixtures through Python string
    literals has turned "\\bin" into a backspace three times. The fixture is
    built from raw strings; this asserts the result.
    """
    path, _ = make_svg(tmp_path)
    with open(path, "rb") as fh:
        blob = fh.read()
    bad = [byte for byte in blob if byte < 32 and byte not in (9, 10, 13)]
    assert not bad, f"control characters in the generated fixture: {sorted(set(bad))}"


def test_backup_holds_the_original(tmp_path):
    """The backup is the only remaining copy of the pre-strip file."""
    path, value = make_svg(tmp_path)
    with MetadataScrubber(backup=True) as scrubber:
        scrubber.sanitize_file(path, remove_all=True)
    backup = path + ".backup"
    assert os.path.exists(backup)
    assert raw_contains(backup, value)
    assert not raw_contains(path, value)
