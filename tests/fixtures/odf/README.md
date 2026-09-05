# tests/fixtures/odf

The fixture set behind the ODF engine, added 2026-09-04.

`build_odf.py` generates every fixture at test time and ships no binaries, for
the same reasons as `../ole2/`: a committed `.odt` is an opaque blob nobody can
review, and a generated one cannot drift away from the seed it claims to carry.

## Two routes, and why both are here

### `build_odf()` hand-writes the package with the stdlib

No LibreOffice, no imaging library, no skip, no platform variance. Measured
2026-09-04 on the hand-built output:

| Check | Result |
|---|---|
| `file` (libmagic 5.45) | `OpenDocument Text` / `Spreadsheet` / `Presentation` / `Drawing` and the four matching `... Template` strings |
| exiftool 13.29 `-FileType` | `ODT` / `ODS` / `ODP` / `ODG`; the four templates come back as `ZIP` with the correct `MIMEType`, which is exiftool's own behaviour on templates and not a fixture defect |
| exiftool tag read | Title, Subject, Description, Creator, Initial-creator, Keyword, Generator, User-defined, Template Title, Template Href, Preview PNG |
| `soffice --convert-to pdf` | rc 0 for all eight |

The control this route buys is the reason it exists: LibreOffice cannot be asked
for a chosen printer name, a populated `meta:template` href, an
`officeooo:rsid` attribute, an `<office:annotation>` or a custom `manifest.rdf`
triple without an interactive edit. Every one of those is a real carrier and
every one of them is seeded here.

**It is also the only route that produces a genuine Draw document.** Measured:
`soffice --convert-to odg` and `--convert-to odg:draw8` on a `.pptx` both write
a package whose `mimetype` member reads
`application/vnd.oasis.opendocument.presentation`, and libmagic calls the result
an OpenDocument Presentation. The hand-built `.odg` reads
`...opendocument.graphics` and libmagic calls it an OpenDocument Drawing.

### `libreoffice_odf()` is the control on the route above

A hand-built fixture proves the engine handles a file the TEST wrote, and
nothing more. So `.odt`, `.ott`, `.ods` and `.odp` are additionally built by
LibreOffice's own export filter from a `python-docx` / `openpyxl` /
`python-pptx` seed, and `tests/test_odf.py::test_a_real_libreoffice_document_is_cleaned`
runs the same byte search against those. That file has its own member order, its
own namespace declarations and a 127-item `settings.xml`.

It returns `None` for every failure mode and the tests turn `None` into a skip.
A machine without LibreOffice is not evidence that the engine is broken.

`libreoffice_odf()` takes the `soffice` path and the `convert` callable as
arguments instead of finding them itself, so the run keeps using the single
profile-isolated converter in `../ole2/build_ole2.py`. That matters: LibreOffice
serialises on its user profile and reports the contention as a timeout, and the
number of LibreOffice profiles in a test run is a property worth keeping at one.

## The sentinel family

`sentinels()` derives six distinct values from one base sentinel:

| Key | Where it goes | Expected fate |
|---|---|---|
| `meta` | every field in `meta.xml` | removed |
| `printer` | `PrinterName`, and inside the base64 `PrinterSetup` blob in ASCII and UTF-16LE | removed |
| `db` | `PrintFaxName`, `RedlineProtectionKey` (base64), `CurrentDatabaseDataSource`, `CurrentDatabaseCommand`, `EmbeddedDatabaseName` | removed |
| `body` | the document body text in `content.xml` | **survives** |
| `annot` | `<dc:creator>` of an `<office:annotation>` and of a tracked change | **survives** |
| `rdf` | a custom triple in `manifest.rdf` and a `metadata/custom.rdf` member | removed |

Three of those must survive, so none of the derived values may contain the base
value as a substring: a cross-product test searching the output for the base
value would otherwise find a survivor that was supposed to stay and report a
clean file as still leaking. The tag is inserted BEFORE the numeric serial for
exactly that reason, and `_check_disjoint()` asserts it rather than trusting it.

The `PrinterSetup` blob is FABRICATED, not copied from this machine's real
`settings.xml`. A committed fixture must not carry the owner's actual printer
name and driver version into the repository. The shape is what the test needs.

## What this fixture does not prove

It does not prove LibreOffice's exact spelling of `officeooo:rsid` or of
`<office:annotation>`; producing either needs an interactive edit and these
fixtures are built headless. It tests the engine's regex and its reporting path
against the spelling the ODF schema defines. That is what it proves, and nothing
beyond it.
