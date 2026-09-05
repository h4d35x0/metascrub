"""
SVG engine.

exiftool cannot write SVG. Measured 2026-09-04 with exiftool 13.29:

    exiftool -listwf                          SVG is not in the writable list
    exiftool -all= -overwrite_original w.svg  "ExifTool does not yet support
                                               writing of SVG images", exit 1

It READS SVG usefully, which is what keeps the baseline and the verification
honest, so this engine writes and exiftool still reads. That is the same split
the PDF, OOXML, OLE2 and AV engines already use.

WHY REGEX AND NOT AN XML ROUND TRIP

Parsing with ElementTree and serialising back changes namespace prefixes,
rewrites self-closing tags, drops the DOCTYPE, reorders attributes and
normalises entities. Any of those can change rendering, and all of them change
bytes far away from the edit, which would make a byte-level test meaningless.
So the file is edited as text and everything not matched is passed through
untouched. The OOXML engine made the same call for w:rsid and the reasoning
carries.

The output IS parsed afterwards, as a guard rather than as a transform: if the
input parsed and the output does not, the engine refuses to write. A regex that
ate a closing tag must never reach the user's file.

WHAT IS REMOVED, AND WHY EACH IS SAFE

  XML comments                  never rendered. Inkscape writes "Created with
                                Inkscape"; hand-authored files carry author
                                notes. Comments inside <style>, <script> and
                                CDATA are NOT touched: the legacy
                                <style><!-- css --></style> idiom hides real
                                stylesheet text inside a comment, and deleting
                                it would delete the drawing's styling.
  <metadata>                    the SVG spec defines it as non-rendered. It is
                                where the RDF/cc:Work/dc:* block lives: title,
                                creator, rights, description, date.
  <sodipodi:namedview>          a foreign-namespace element renderers ignore.
                                It is a picture of the author's desktop: window
                                geometry and position, zoom, cursor position,
                                current layer, document units.
  sodipodi:*, inkscape:*, ooo:* foreign-namespace attributes. Carries
                                sodipodi:docname (the file's name on the
                                author's disk), inkscape:version (the exact
                                editor build), inkscape:export-filename (an
                                ABSOLUTE PATH), sodipodi:absref (an absolute
                                path to a linked image), and LibreOffice's
                                ooo:* export attributes.
  xmlns:sodipodi|inkscape|ooo   only once the prefix is unused everywhere else
                                in the document, which is checked rather than
                                assumed. An unused declaration cannot affect
                                rendering, and leaving it announces the editor.
  root <title> and <desc>       document metadata, see below.

WHAT IS KEPT

Everything else, and specifically: xmlns, viewBox, width, height,
preserveAspectRatio, version, style, geometry, transform, id attributes (they
are referenced by <use>, by CSS and by gradient links, so removing them breaks
documents), and <text> content.

THE ROOT <title>/<desc> SPLIT

<title> is the accessibility name of an SVG or of a shape inside it. Screen
readers announce it. Removing every one of them would be an accessibility
regression on user content. But the ROOT one is also where editors put the
document title.

Measured 2026-09-04, exiftool 13.29, on a file carrying both: exiftool reports
the root <title> and <desc> as SVG:Title and SVG:Desc and does NOT report the
nested ones at all. So the split is not a guess about intent, it lands exactly
on the line the verification can see: the elements that become residual-scan
needles are removed, and the per-shape accessibility labels are kept and
reported. A nested <title> left in place can never produce a residual_found,
because it never becomes a needle.

WHAT SURVIVES, WHICH IS WHY THIS FORMAT IS PARTIAL

  base64-embedded rasters   an <image> can carry a whole JPEG as a data: URI,
                            EXIF and GPS included. Measured: exiftool reports
                            nothing about it, so it produces no needle, and the
                            residual scan searches raw bytes while the value is
                            base64-wrapped, so it finds nothing either. The
                            file would report clean while carrying a
                            photographer's name and coordinates. Reported as a
                            note; not removed, because removing it deletes the
                            picture.
  external local references an xlink:href of file:///C:/Users/... leaks a
                            username. Measured: exiftool does not read it.
                            Removing it deletes the image from the drawing,
                            which this tool does not do to user content.
                            Reported as a note instead.
  -inkscape-* CSS           a style attribute can carry
                            "-inkscape-font-specification:Droid Sans Mono",
                            which is a CSS property rather than a namespaced
                            attribute, so the attribute sweep does not see it.
                            Measured on a genuine Inkscape 0.48.3.1 file: 15
                            occurrences survived a strip that removed everything
                            else. Reported, not removed, because a style
                            attribute is the drawing and Inkscape uses that
                            property to round-trip a font choice.

Both are named in the capability note for .svg, and tests/test_svg.py asserts
the residue is exactly what the note says.
"""

from __future__ import annotations

import codecs
import os
import re
import xml.etree.ElementTree as ET
from typing import List, Sequence, Tuple

from .base import BaseEngine, EngineError, atomic_replace, temp_beside

# Regions whose text is not markup and must never be edited. The legacy
# <style><!-- ... --></style> wrapper is the concrete reason: the comment
# remover would otherwise delete a real stylesheet.
_PROTECTED = re.compile(
    r"<style\b[^>]*>.*?</style\s*>"
    r"|<script\b[^>]*>.*?</script\s*>"
    r"|<!\[CDATA\[.*?\]\]>",
    re.DOTALL | re.IGNORECASE,
)

_COMMENT = re.compile(r"<!--.*?-->", re.DOTALL)

# One start or end tag, with attribute values that may themselves contain ">".
# Leading "<?" and "<!" do not match, so the XML declaration, the DOCTYPE and
# comments are skipped by construction.
_TAG = re.compile(r"""<(/?)([A-Za-z_][-\w.:]*)((?:[^>"']|"[^"]*"|'[^']*')*)>""")

# Editor-private namespaces. Every attribute in one of these is removed.
_PRIVATE_PREFIXES = ("sodipodi", "inkscape", "ooo")

_PRIVATE_ATTR = re.compile(
    r"""\s+(?:%s):[A-Za-z_][-\w.]*\s*=\s*(?:"[^"]*"|'[^']*')"""
    % "|".join(_PRIVATE_PREFIXES)
)

# A namespace declaration for one of those prefixes. Removed only after the
# prefix is confirmed unused, see _drop_unused_namespace_declarations.
_PRIVATE_NS_DECL = {
    prefix: re.compile(
        r"""\s+xmlns:%s\s*=\s*(?:"[^"]*"|'[^']*')""" % prefix
    )
    for prefix in _PRIVATE_PREFIXES
}

# Elements removed only as a direct child of the root element.
_DROP_AT_ROOT = frozenset({"title", "desc"})

# A data: URI carrying an image. Not removed; counted and reported.
_DATA_URI = re.compile(r"""(?:xlink:)?href\s*=\s*["']\s*data:image/[^;,"']+""",
                       re.IGNORECASE)

# An href pointing at a local file. "file:" scheme, a Windows drive letter, or a
# UNC path. A bare leading "/" is deliberately NOT flagged: in an href that is a
# site-root-relative URL, not a filesystem path, and flagging it would train
# users to ignore the note.
_LOCAL_HREF = re.compile(
    r"""(?:xlink:)?href\s*=\s*["']\s*"""
    r"""(file:[^"']*|[A-Za-z]:[\\/][^"']*|\\\\[^"']*)["']""",
    re.IGNORECASE,
)

# A vendor-prefixed CSS property inside a style attribute. Not removed; counted
# and reported.
#
# Measured 2026-09-04 on a genuine Inkscape 0.48.3.1 file
# (a genuine Inkscape file from a CTF asset set, ic.svg): after every
# inkscape: attribute, the namedview, the metadata block and the namespace
# declarations were gone, the string "inkscape" was still in the file 15 times,
# every one of them "-inkscape-font-specification:" inside a style attribute.
# It is a CSS property rather than a namespaced attribute, so the attribute
# sweep does not see it, and it still says which editor produced the file.
#
# It is reported rather than removed because a style attribute is the drawing.
# Inkscape uses this property to round-trip a font choice, and font-weight or
# font-style are not always present beside it, so deleting it can change which
# face the file resolves to. Reporting it is the honest outcome; the option to
# remove it belongs behind an explicit flag. See tasks/todo.md.
_EDITOR_CSS_PROPERTY = re.compile(r"-inkscape-[A-Za-z-]+\s*:")


def _decode(blob: bytes) -> Tuple[str, str, bytes]:
    """
    Return (text, codec, bom) such that _encode(_decode(b)) is byte-identical.

    latin-1 is used for everything that is not UTF-16, and that is deliberate
    rather than lazy: it maps every byte to exactly one character and back, so
    an untouched region of a UTF-8, windows-1252 or ASCII file survives this
    engine byte for byte, whatever its declared encoding says. The patterns here
    only ever match ASCII markup, so mojibake in the intermediate string cannot
    reach a match. UTF-16 is not byte-transparent under any single-byte codec,
    so it gets a real decode.
    """
    if blob.startswith(codecs.BOM_UTF16_LE):
        return blob[2:].decode("utf-16-le"), "utf-16-le", codecs.BOM_UTF16_LE
    if blob.startswith(codecs.BOM_UTF16_BE):
        return blob[2:].decode("utf-16-be"), "utf-16-be", codecs.BOM_UTF16_BE
    return blob.decode("latin-1"), "latin-1", b""


def _encode(text: str, codec: str, bom: bytes) -> bytes:
    return bom + text.encode(codec)


def _protected_spans(text: str) -> List[Tuple[int, int]]:
    return [match.span() for match in _PROTECTED.finditer(text)]


def _overlaps(span: Tuple[int, int], spans: Sequence[Tuple[int, int]]) -> bool:
    start, end = span
    return any(start < other_end and other_start < end
               for other_start, other_end in spans)


def _cut(text: str, spans: Sequence[Tuple[int, int]]) -> str:
    """Remove spans from the text, right to left so offsets stay valid."""
    out = text
    for start, end in sorted(spans, reverse=True):
        out = out[:start] + out[end:]
    return out


def _element_spans(text: str, names: frozenset, root_child_only: bool) -> List[Tuple[int, int]]:
    """
    Locate whole elements by name, using a depth-tracking walk rather than a
    non-greedy regex.

    A regex cannot tell a root <title> from one nested inside a <g>, and that
    distinction is the whole of the title decision above. The walk also handles
    an element containing a same-named descendant, which a non-greedy match
    would truncate.
    """
    protected = _protected_spans(text)
    stack: List[Tuple[str, int]] = []
    found: List[Tuple[int, int]] = []

    for match in _TAG.finditer(text):
        if _overlaps(match.span(), protected):
            continue
        closing, name, attrs = match.group(1), match.group(2).lower(), match.group(3)
        self_closing = attrs.rstrip().endswith("/")

        if closing:
            while stack:
                open_name, open_start = stack.pop()
                if open_name == name:
                    depth = len(stack)
                    if name in names and (not root_child_only or depth == 1):
                        found.append((open_start, match.end()))
                    break
            continue

        if self_closing:
            depth = len(stack)
            if name in names and (not root_child_only or depth == 1):
                found.append(match.span())
            continue

        stack.append((name, match.start()))

    # An element nested inside another element that is also being removed would
    # be listed twice; keep only the outermost spans.
    found.sort()
    merged: List[Tuple[int, int]] = []
    for start, end in found:
        if merged and start < merged[-1][1]:
            continue
        merged.append((start, end))
    return merged


def _strip_private_attributes(text: str) -> str:
    protected = _protected_spans(text)
    spans = [m.span() for m in _PRIVATE_ATTR.finditer(text)
             if not _overlaps(m.span(), protected)]
    return _cut(text, spans)


def _drop_unused_namespace_declarations(text: str) -> Tuple[str, List[str]]:
    """
    Remove xmlns:sodipodi / xmlns:inkscape / xmlns:ooo once nothing uses them.

    Checked, not assumed: the declaration goes only when the prefix appears
    nowhere else in the document. An unused declaration cannot change what is
    rendered, and leaving it behind announces which editor produced the file
    after every trace of that editor's attributes has been removed.
    """
    dropped = []
    for prefix, pattern in _PRIVATE_NS_DECL.items():
        decl = pattern.search(text)
        if decl is None:
            continue
        without = text[:decl.start()] + text[decl.end():]
        # A remaining use is either an element name (<inkscape:foo) or an
        # attribute name ( inkscape:bar=). Either keeps the declaration.
        used = re.search(r"[<\s]%s:" % prefix, without)
        if used:
            continue
        text = without
        dropped.append(f"xmlns:{prefix} declaration (unused after the strip)")
    return text, dropped


def _parses(text: str, codec: str, bom: bytes) -> bool:
    try:
        ET.fromstring(_encode(text, codec, bom))
        return True
    except Exception:
        return False


class SvgEngine(BaseEngine):
    name = "svg"
    supports_selective = False

    def available(self) -> Tuple[bool, str]:
        return True, ""  # stdlib only

    def strip_all(self, path: str) -> List[str]:
        self.require()
        try:
            with open(path, "rb") as handle:
                blob = handle.read()
        except OSError as exc:
            raise EngineError(f"cannot read {path}: {exc}") from exc

        try:
            text, codec, bom = _decode(blob)
        except (UnicodeDecodeError, LookupError) as exc:
            raise EngineError(f"cannot decode SVG text: {exc}") from exc

        original_parsed = _parses(text, codec, bom)
        cleaned, targeted = self._clean(text)
        notes = self._notes(cleaned)

        if cleaned == text:
            # Nothing matched. Leaving the file completely untouched is the
            # correct outcome: rewriting identical content would still change
            # the file's mtime and give a user a diff to explain.
            return notes

        if original_parsed and not _parses(cleaned, codec, bom):
            raise EngineError(
                "refusing to write: the input parsed as XML and the stripped "
                "output does not. No change was made to the file."
            )

        tmp = temp_beside(path, ".svg")
        try:
            with open(tmp, "wb") as handle:
                handle.write(_encode(cleaned, codec, bom))
        except OSError as exc:
            if os.path.exists(tmp):
                try:
                    os.unlink(tmp)
                except OSError:
                    pass
            raise EngineError(f"failed to write stripped SVG: {exc}") from exc

        atomic_replace(tmp, path)
        return targeted + notes

    def _clean(self, text: str) -> Tuple[str, List[str]]:
        targeted: List[str] = []

        protected = _protected_spans(text)
        comments = [m.span() for m in _COMMENT.finditer(text)
                    if not _overlaps(m.span(), protected)]
        if comments:
            text = _cut(text, comments)
            targeted.append(f"{len(comments)} XML comment(s)")

        for name, label in (("metadata", "<metadata> RDF block"),
                            ("sodipodi:namedview", "<sodipodi:namedview> editor state")):
            spans = _element_spans(text, frozenset({name}), root_child_only=False)
            if spans:
                text = _cut(text, spans)
                targeted.append(f"{label} x{len(spans)}")

        root_children = _element_spans(text, _DROP_AT_ROOT, root_child_only=True)
        if root_children:
            text = _cut(text, root_children)
            targeted.append(f"root <title>/<desc> x{len(root_children)}")

        before_attrs = text
        text = _strip_private_attributes(text)
        if text != before_attrs:
            targeted.append(
                "editor-private attributes ("
                + ", ".join(f"{p}:*" for p in _PRIVATE_PREFIXES)
                + ")"
            )

        text, dropped = _drop_unused_namespace_declarations(text)
        targeted.extend(dropped)

        return text, targeted

    def _notes(self, text: str) -> List[str]:
        """
        Name what was left behind, in the shape the OOXML engine already uses.

        This is the honest half of a PARTIAL. Both items below are things this
        engine can see and deliberately does not remove, and neither the
        exiftool read nor the residual byte scan can see them, so the note is
        the only place a user learns they are there.
        """
        notes: List[str] = []
        embedded = len(_DATA_URI.findall(text))
        if embedded:
            notes.append(
                f"NOTE: {embedded} embedded base64 image(s) present and NOT "
                "inspected; EXIF and GPS inside them survive"
            )
        for match in _LOCAL_HREF.finditer(text):
            notes.append(
                "NOTE: external local file reference present and NOT removed: "
                + match.group(1)
            )
        editor_css = len(_EDITOR_CSS_PROPERTY.findall(text))
        if editor_css:
            notes.append(
                f"NOTE: {editor_css} -inkscape-* CSS propert(ies) inside style "
                "attributes still name the editor and are NOT removed; a style "
                "attribute is the drawing"
            )
        return notes

    def strip_fields(
        self,
        path: str,
        fields_to_remove: Sequence[str],
        fields_to_sanitize: Sequence[str],
    ) -> Tuple[List[str], List[str]]:
        raise EngineError(
            "the svg engine cannot remove individual fields; use remove_all"
        )
