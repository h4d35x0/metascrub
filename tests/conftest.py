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


build_ole2 = _load_ole2_builder()
HAVE_LIBREOFFICE = build_ole2.find_soffice() is not None


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
    if kind == ".docx":
        return make_docx(tmp_path)
    if kind == ".xlsx":
        return make_xlsx(tmp_path)
    if kind == ".pptx":
        return make_pptx(tmp_path)
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
    ".pdf", ".docx", ".xlsx", ".pptx", ".doc", ".xls", ".ppt",
    ".mp4", ".mkv", ".mp3", ".flac",
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
