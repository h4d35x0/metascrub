# Metadata-Scrubber (`metascrub`)

Removes metadata from documents, images, audio and video, then **proves the
removal actually happened** instead of assuming it did.

Built on the `ExifSanitizer` from `the parent project` (`cli/sanitizer.py`), which
handled eight image extensions through one engine. This extends that to five
engines across 62 extensions, and replaces "no exception was raised" with
measured verification.

---

## Why the verification exists

Measured on 2026-09-04 with exiftool 13.29, on a one-page test PDF carrying
`SECRETAUTHOR12345` in both its docinfo dictionary and its XMP packet:

```
exiftool -all= -overwrite_original test.pdf

  file GREW              1519 -> 1846 bytes
  exiftool -Author       prints nothing
  grep SECRETAUTHOR      still there, twice
  exiftool's own warning "PDF edits are reversible. Deleted tags may be recovered!"
```

exiftool edits a PDF by appending an incremental update. The old objects stay in
the file. **A tool that re-read the file with exiftool would have reported that
document clean.** It was not clean.

The same document through this tool:

```
metascrub scrub test.pdf

  file SHRANK            1519 -> 1025 bytes
  grep SECRETAUTHOR      zero hits
  verdict                verified_clean
```

Re-confirmed on exiftool 13.59, which behaves identically: the same
`-all= -overwrite_original` grew the file by the same 327 bytes, left both
copies of the string in place, printed nothing for `-Author`, and emitted the
same warning. This is not a quirk of one release.

So verification here never trusts the engine that performed the write. It reads
the file's metadata **before** touching it, then searches the output bytes for
those exact values. If a value the file used to carry is still findable, the
file is reported as still leaking, whatever any tool claims.

### What that proves, and what it does not

The values being searched for come from exiftool's view of the file before it
was touched. So the scan proves that **nothing exiftool could see has
survived**. That is not the same as proving nothing survived.

A carrier exiftool does not parse never becomes something to search for, so its
survival cannot be detected either. Measured 2026-09-04 on a `.docx`: the author
in `docProps` becomes a search value, while a string written into
`customXml/item1.xml` does not, even though it is still in the file afterwards.

This is why formats get engine-specific handling rather than one generic
exiftool pass. The engines deliberately remove carriers the runtime scan cannot
see: the OOXML engine drops `w:rsid` revision identifiers and the thumbnail, and
the PDF engine rewrites the whole object graph. The test suite catches those
because it embeds its own sentinels and knows where it put them, independently
of what any tool reports.

The runtime verdict is bounded by what exiftool can read, and `verified_clean`
should be read as exactly that claim and no wider.

---

## Install

```bash
pip install -e ".[gui]"          # installs the `metascrub` and `metascrub-gui` commands
```

That puts two launchers on PATH: `metascrub` (console) and `metascrub-gui`
(window, no console, because it is declared under `gui-scripts`). On Windows pip
writes them as real `.exe` shims into the Python **Scripts** directory; if pip
warns that directory is not on PATH, add it, or keep using `python -m metascrub`
from the project folder.

An editable install (`-e`) points at this source tree rather than copying it, so
the commands stop working if the drive holding the project is not mounted. Drop
the `-e` for an install that does not depend on the project directory.

Or, without installing anything:

```bash
pip install -r requirements.txt
python -m metascrub ...          # run from the project directory
```

### On a fresh machine, from a clone

The repo is self-contained. Cloned anywhere, `bin\` still works, because the
launchers resolve the project relative to themselves rather than to any fixed
path:

```bash
git clone <remote> metascrub
cd metascrub
./bin/metascrub selftest              # macOS, Linux
bin\metascrub.cmd selftest            # Windows
```

`selftest` is the right first command: it reports what is missing rather than
failing on the first file. Nothing needs installing for the launchers to work,
but two binaries must be on PATH, and **exiftool is required for every format,
not just images**:

| | exiftool | ffmpeg (audio and video only) |
|---|---|---|
| Windows | `winget install OliverBetz.ExifTool` | `winget install Gyan.FFmpeg` |
| macOS | `brew install exiftool` | `brew install ffmpeg` |
| Debian, Ubuntu | `sudo apt install libimage-exiftool-perl` | `sudo apt install ffmpeg` |

On Linux, `metascrub gui` also needs `sudo apt install python3-tk`. Tkinter is
in the standard library but several distributions package it separately. The
CLI does not need it, and `selftest` reports its absence as SKIP rather than a
failure.

A clone off the portable volume has no `.tools\exiftool\` beside it, so the
Windows launchers fall back to a system exiftool. That is checked and verified:
a clone at an unrelated path passes `selftest` using the system copy, and each
launcher runs the source sitting next to it rather than any other copy that may
be pip-installed on the machine. The `package` line of `selftest` prints which
directory it actually imported, so this is never a guess.

### Running it from the portable volume

`pip install` cannot produce a launcher that travels. On Windows it writes a
stub into the Python installation's Scripts directory with the absolute path of
that interpreter baked in (`C:\Python314\python.exe`), so the launcher is tied
to one machine even though the code is not.

The real launchers live in the project's own `bin\` directory, so the project
directory is self-contained: copy or clone it anywhere and it is runnable, with
no install step and nothing to register.

| Launcher | For |
|---|---|
| `bin\metascrub.cmd` | Windows, console |
| `bin\metascrub-gui.cmd` | Windows, window with no console (`pythonw`) |
| `bin\metascrub` | macOS and Linux (`sh`) |

They resolve the project **relative to themselves**, so the volume can mount at
any drive letter or any `/Volumes` path and they still find it. They set
`PYTHONPATH` and run from source rather than from an install, so the code that
runs is always the code sitting next to them.

`<volume>\Projects\.tools\bin\` holds two-line **forwarders** to these, and
nothing else. That directory is on PATH, which is the only reason a bare
`metascrub` works from anywhere; keeping the logic out of it means the project
does not depend on a directory outside itself, and that shared directory does
not accumulate per-project launchers.

The Windows launchers also prepend `<volume>\Projects\.tools\exiftool\` to
PATH, falling back to a system exiftool if the volume has none. That copy is
what makes the tool portable in practice: exiftool is required for **every**
format, so without one metascrub correctly refuses to do anything, and a
launcher that did not carry its own would be useless on a machine that happens
not to have it installed. The bundled binary is Windows-only, so the `sh`
launcher deliberately does not add it and tells you how to install a native one
instead.

Also needs two binaries on PATH:

| Binary | Used for | Check | Install (Windows) |
|---|---|---|---|
| `exiftool` | **reading metadata for every format**, and writing images | `exiftool -ver` | `winget install OliverBetz.ExifTool` |
| `ffmpeg` | audio and video | `ffmpeg -version` | `winget install Gyan.FFmpeg` |

**`exiftool` is required for all formats, not just images.** Every engine reads
its baseline metadata through exiftool even when it writes with pikepdf,
zipfile or ffmpeg, because reading with a different tool than the one that
wrote is what makes the verification meaningful. Without it, `scrub` refuses to
run rather than reporting files it never examined.

```bash
python -m metascrub doctor      # reports which engines can actually run
```

---

## Use

```bash
python -m metascrub scrub photo.jpg report.pdf clip.mp4
python -m metascrub scrub ./album -r --report scrub-report.json
python -m metascrub inspect photo.jpg          # show metadata, change nothing
python -m metascrub restore photo.jpg          # undo from photo.jpg.backup
python -m metascrub formats                    # what is handled, and how well
python -m metascrub selftest                   # prove the whole chain works here
python -m metascrub gui                        # desktop window
```

Useful flags: `--no-backup`, `--reset-times` (also normalise filesystem
timestamps), `--remove-field TAG` / `--sanitize-field TAG` (exiftool formats
only).

Exit status is non-zero if any file failed **or** if any file still holds
metadata after sanitizing, so it drops into a pipeline without parsing the
report.

```python
from metascrub import MetadataScrubber

with MetadataScrubber(backup=True) as scrubber:
    result = scrubber.sanitize_file("report.pdf", remove_all=True)
    assert result["verification"]["clean"]
```

---

## Desktop window

```bash
metascrub-gui                        # after pip install; no console window
metascrub gui ./album                # pre-queue a folder
python -m metascrub gui              # without installing, from the project folder
```

Tkinter, from the standard library. Queue files with Add Files / Add Folder, or
drag them onto the list.

Drag and drop comes from `tkinterdnd2`, which `requirements.txt` installs. It is
declared as the `gui` extra rather than a core dependency, because the CLI does
not need it and a headless machine cannot use it:

```bash
pip install -r requirements.txt     # includes drag and drop
pip install -e ".[gui]"             # same, via the extra
pip install -e .                    # CLI only, window still works without DnD
```

Two things about it are worth knowing, both measured rather than assumed:

- **Having it installed is not sufficient.** Its Tcl commands exist only when
  the root window was created as `TkinterDnD.Tk()`, which `main()` does. A
  window built on a plain `tk.Tk()` root, as a test harness or an embedding
  application would, raises `invalid command name "tkdnd::drop_target"`. The
  window catches that and turns drag and drop off rather than failing to open.
- **A dropped path is a Tcl list, not a string.** A path containing spaces
  arrives brace-wrapped (`{C:/two words/a.pdf} C:/b.pdf`), so splitting on
  whitespace would turn one real path into several nonexistent ones and the
  drop would silently add nothing.

It is a view over the same `MetadataScrubber` the CLI drives, not a second
implementation. That is deliberate: `gui/sanitizer.py` in the the parent project repo is a
stale v1.0.0 fork of `cli/sanitizer.py`, and two copies growing format support
independently is the failure this project already exists to undo.

What the window will not do:

- **It will not scrub when metadata cannot be read.** Engine status is checked
  at startup, shown in the header, and re-checked before every run. With
  exiftool missing the Scrub button is disabled and the status bar carries the
  install command. A GUI that paints a green CLEAN row for a file nothing
  examined is worse than no GUI, because the reassurance is what the user takes
  away.
- **It will not leave a verification cell blank.** Every file reads either
  `verified clean`, `clean (partial format)`, `STILL LEAKING (n)`, or
  `not verified`. A blank cell reads as "fine", and DEFERRED and SKIPPED files
  are not fine, they are unexamined.
- **It will not let a leak pass quietly.** A still-leaking file raises a dialog
  rather than only colouring a row. A fully clean run raises nothing.
- **It will not queue a `.backup`.** That file is the only remaining copy of the
  original.

Scrubbing runs on a worker thread, so a large directory does not freeze the
window; results cross back over a queue and are applied on the main thread.
Double-click any row for the full result JSON.

---

## Checking an installation

`doctor` answers "can the engines start". `selftest` answers the larger
question a launcher on a new machine actually raises: did it resolve the right
project, are the dependencies really there, and does a real file go in dirty
and come out clean.

```bash
metascrub selftest
```

```
  PASS python       3.14.6
  PASS package      1.0.0 from <volume>\Projects\...\metascrub
  PASS exiftool     <volume>\Projects\.tools\exiftool\exiftool.EXE
  PASS ffmpeg       ...
  PASS tkinter      available; `metascrub gui` will run
  PASS engines      all 5 ready
  PASS round trip   a PDF went in carrying a value and came out without it
```

The round trip is the only check that proves anything: it builds a PDF carrying
a known value, scrubs it, and searches the **output bytes** for that value. It
never asks the tool whether it succeeded, because a self test that trusted the
tool's own verdict would pass on exactly the bug this project exists to catch.

`SKIP` is a real outcome and is never counted as success. A missing ffmpeg is
not a metascrub failure, but it is not evidence that audio and video work
either. A missing exiftool is a `FAIL`, not a `SKIP`, because nothing can be
scrubbed or verified without it. Exit status is non-zero if any check failed.

This is what to run on macOS or Linux the first time, since the `sh` launcher
was written on Windows and cannot be exercised from there.

---

## Look

The window uses ttk's `classic` theme with the Windows 95 system palette.
That is a deliberate choice rather than nostalgia: `classic` already draws the
beveled borders that look needs, so the styling works with the toolkit. Chasing
a flat contemporary look in Tk means either fighting the widget set or adding a
theme dependency, which would undo the reason Tkinter was chosen.

Two rules constrain the styling, and `tests/test_theme.py` enforces both rather
than trusting a comment:

- **Every status colour clears a 3:1 contrast ratio** against the background it
  is actually rendered on, which is the white list well, not the grey face.
  Checking against the wrong background would pass a colour that is unreadable
  in practice.
- **Colour is never the only channel.** DANGER and OK measure a contrast ratio
  of 1.07 against each other: they are nearly identical in luminance, so a
  red/green deficiency makes a FAILED row indistinguishable from a SANITIZED
  one. The Status column (`SANITIZED` vs `FAILED`) and the Verification column
  (`verified clean` vs `STILL LEAKING`) carry the distinction as text, and a
  test asserts no two statuses share a label.

The checkbox indicators are deliberately left unstyled. Restyling them to match
the palette set the selected colour to the same white as the unselected one, so
a ticked box rendered identically to an empty one. That is not cosmetic:
`Keep backup` decides whether the user's originals survive, and a toggle whose
position cannot be read is worse than an ugly one.

---

## Coverage

| Family | Extensions | Engine | Guarantee |
|---|---|---|---|
| Images | jpg jpeg jpe png gif webp tiff tif heic heif avif jp2 psd | exiftool | complete |
| Raw photo | dng cr2 nef arw orf rw2 pef srw erf mos iiq arq sr2 rwl nrw raw gpr | exiftool | **partial**, maker notes may retain private records |
| PDF | pdf | pikepdf full rewrite | complete |
| Vector | svg | XML text rewrite | **partial**, embedded base64 rasters and external local references survive and are reported |
| Office | docx docm xlsx xlsm pptx pptm | zip rebuild | complete |
| OpenDocument | odt ott ods ots odp otp odg otg | zip rebuild | complete |
| Legacy Office | doc xls ppt | olefile stream rewrite | **partial**, the applications keep second copies |
| Video | mp4 m4v mov qt mqv lrv mkv webm avi f4v m4b ts m2ts | ffmpeg remux | complete |
| Audio | mp3 m4a f4a flac wav ogg opus aiff aif | ffmpeg remux | complete |

`complete` and `partial` are separate states carried in every result, because
"we support this format" and "we can fully clean this format" are different
promises. A partial result always says so.

### Not handled, and why

- **`.qt`, `.mqv`, `.lrv`, `.f4a` are no longer deferred.** They shipped on
  2026-09-04 once the muxer selection learned to preserve the container
  flavour. The `DEFERRED` table in `metascrub/capabilities.py` is what the
  gates in `tests/test_coverage_gate.py` read, and those gates are exercised
  against synthetic tables so they keep their teeth even when it is empty.
  An earlier version of this file described four formats as deferred while
  that table held nothing, which is precisely how a deferral gets discharged
  by being forgotten.
- **`.fodt`, `.fods` and `.fodp`** (flat XML OpenDocument) are deferred, not
  refused. They are a single uncompressed XML file rather than a zip package, so
  the ODF engine's whole strategy does not apply to them. Measured 2026-09-04
  with exiftool 13.29: a `.fodt` reports `FileType: XML`, reads Title and
  Creator correctly, and refuses to write with *"[minor] Can't handle XMP
  attribute 'office:mimetype'"*. **Discharge condition:** a raw-XML engine that
  edits the `office:document` element in place, a fixture per extension, and a
  `tests/test_flat_odf.py` that searches the output bytes.
- **`.doc`, `.xls` and `.ppt`** were deferred until 2026-09-04; they now ship as
  PARTIAL, and *Legacy Office, and what it does not promise* below says what
  survives. `tests/test_coverage_gate.py` enforces the mechanism, so a deferral
  cannot be discharged by forgetting it.
- **`.svgz` is deferred**, with the discharge condition below. `Container.RAW`
  would hand the residual scan the compressed bytes, so every `.svgz` would
  verify clean unconditionally, which is a false CLEAN and the one direction
  this tool must never fail in.
- **`.bmp`** is absent because exiftool 13.29 answers *"Writing of BMP files is
  not yet supported"*. Listing a format the tool cannot write would be a promise
  it cannot keep.
- **`.cr3`, `.raf`, `.x3f`, `.crw`, `.mrw`, `.cs1`, `.psb`** are absent because
  no fixture can be built for them here. These containers are not TIFF-based,
  so exiftool reports a file carrying that extension as plain `TIFF` and
  refuses to write it as the target format. Adding them would mean claiming
  coverage through the `.tiff` proxy, which would be a false claim rather than
  a shortcut. They need genuine camera samples.
- **`.3fr` and `.fff`** are absent because exiftool identifies them correctly
  but will not write one that was synthesised rather than produced by a camera.
- **`.wmv` and `.wma`** are absent because the tool cannot actually clean them.
  The ffmpeg remux leaves a value behind and verification reports
  `residual_found`. That is the check doing its job, and shipping the format
  anyway would be exactly the kind of unkept promise this table exists to
  prevent.
- **`.aac` and `.ac3`** are raw bitstreams with no metadata container. ffmpeg
  accepts `-metadata` and stores nothing, so there is no fixture to build and
  nothing to remove.
- **`.qt`, `.mqv`, `.lrv` and `.f4a` now ship**, added 2026-09-04. ffmpeg binds
  no output muxer to these four extensions, so the remux failed with
  `Error opening output files: Invalid argument` before it started, and the av
  engine now names the output format explicitly. Which format is read out of
  the input file rather than looked up from the extension: all four name ISO
  base media files in two incompatible flavours, QuickTime and ISO/MP4, and the
  extension does not tell you which. Measured across all eight
  extension/flavour combinations, a lookup keyed on extension alone silently
  handed back four of them in the other flavour, so the engine matches the
  input's `ftyp` brand and the extension is only the fallback for a file that
  carries no `ftyp` box at all. See *What the remux does not preserve* below.
- **Tracked changes and comments** in Office documents are detected and
  **reported, not removed**. They are user-visible content, not metadata, and
  deleting them silently would destroy work.
- **`.svgz`** is deferred, not refused. It is gzipped SVG, and the engine would
  be a one-line `gzip` wrapper, but `Container.RAW` makes the residual scan
  search the compressed bytes, where it can see nothing at all. The format would
  verify clean unconditionally, which is the one direction this tool must never
  fail in. **Discharge condition:** a `Container` value whose
  `searchable_bytes()` branch decompresses first, of the same shape as the
  existing ZIP branch, plus a fixture whose sentinel is found before the scrub
  and absent after.

### What the remux does not preserve

The ffmpeg remux keeps the container's flavour: a QuickTime file comes back
QuickTime, an ISO/MP4 file comes back ISO/MP4. It does not keep the exact
`ftyp` major brand within the ISO family. ffmpeg's mp4 muxer writes `isom`
whatever it read, so an `mp41` input comes back as `isom`. Measured
2026-09-04; this is not new, it is what every MP4-family format here has always
done, and it is recorded because "the container is unchanged" would be a
stronger claim than the tool can make.

### OpenDocument, and the printer sitting in every Writer document

`.odt`, `.ods`, `.odp` and the template and drawing variants ship as of
2026-09-04. exiftool reads all of them and writes none of them (*"Writing of ODT
files is not yet supported"*, exit 1), so the package is rebuilt, the way
`.docx` is.

**The finding that justified doing it at all is in `settings.xml`, and nothing
else in this tool would ever have looked there.** Measured on a `.odt` produced
by LibreOffice on this machine:

```
PrinterName    string        "<printer name redacted>"          the workstation's default printer
PrinterSetup   base64Binary  11316 characters  ->   8487 bytes of Windows DEVMODE
```

Decoding the blob yields the printer name twice in ASCII and once in UTF-16LE,
plus the exact driver string `<printer model redacted>`. A document published after
"metadata removal" would still name the owner's printer and its driver version.
`settings.xml` also carries `Rsid` and `RsidRoot`, which are the LibreOffice
analogue of the `w:rsid` revision-save identifiers stripped from `.docx`.

**The residual scan cannot see this one, and that is written down rather than
left implicit.** exiftool does not report `settings.xml`, so the printer name
never becomes a needle and the post-sanitize scan never searches for it. The
engine removes it; the *tests* confirm the removal, because they know the
sentinel independently. It is the same asymmetry `w:rsid` already lives with.

Also removed: the whole of `meta.xml`, replaced with a valid empty skeleton
(title, subject, description, `dc:creator` which is the last person to save,
`meta:initial-creator` which is the original author, keywords, the exact
LibreOffice build and git commit, creation and modification dates, editing
cycles, editing duration, document statistics, user-defined properties, and the
template path); `Thumbnails/thumbnail.png` with its manifest entry; the package
`manifest.rdf`, replaced with the default skeleton; any in-content `.rdf`
member; and `officeooo:rsid` attributes in `content.xml` and `styles.xml`.

**Annotations and tracked changes are reported, not removed**, the same as
comments in a `.docx`. An annotation carries a `<dc:creator>` naming the
commenter, so the note is the only thing standing between a user and a surprise.

**`settings.xml` is edited with an allowlist of names to DELETE, never a
denylist of names to keep.** It also holds `PrinterIndependentLayout`,
`UseFormerLineSpacing` and roughly 120 other compatibility flags that decide how
the document lays out. Deleting those would silently reflow the user's document,
which is a worse outcome than leaving metadata behind, and
`tests/test_odf.py::test_layout_config_items_survive` is the guard.

**One trap that a naive rebuild falls into silently.** The `mimetype` member
must be the FIRST entry in the zip and must be STORED, not deflated. Measured on
a rebuild that deflated it: LibreOffice still opens the file, exiftool still
reports `FileType: ODT`, and `file` (libmagic 5.45) reports
`Zip data (MIME type "K,("?)` instead of `OpenDocument Text`. The two obvious
checks both pass while content sniffers, and therefore `xdg-mime` and upload
validators and mail gateways, stop recognising the document. Producing that from
a metadata tool is a real user-visible break, so there are two tests: one that
needs nothing but the stdlib, and one that runs `file` when it is available.

### Legacy Office, and what it does not promise

`.doc`, `.xls` and `.ppt` were deferred until 2026-09-04. The reason was never
the format; it was that no fixture could be built here, and an untested rewrite
path for documents a user cannot regenerate is worse than a refusal. LibreOffice
turned out to solve that: its MS Word 97 / MS Excel 97 / MS PowerPoint 97 export
filters produce genuine compound files (`d0cf11e0a1b11ae1`) carrying seeded
metadata, so `tests/fixtures/ole2/` builds a real one at test time and skips,
never fails, where LibreOffice is absent.

**The stream contents are replaced; the container is not rebuilt.** `olefile`
can only overwrite a stream at its existing length, which is what made this look
hard. It stops being a problem once you notice a property set does not have to
be full to be valid: MS-OLEPS allows a property set with zero properties, and
readers reach one through an offset table and never look past it. So the engine
writes a valid EMPTY property set and zero-fills the rest of the allocation.
Nothing but stream payload ever changes, and `tests/test_ole2.py` asserts that
rather than trusting it: the 512-byte header is compared byte for byte, the
stream inventory and every stream length must match, and every stream the
engine does not target must come out byte-identical.
Zero-filling the remainder is not tidiness: leaving the old property bytes in an
allocation nothing points at any more is the PDF incremental-update mistake in a
different container.

Removed: both property streams, matched by basename at any depth, so the copy
inside an embedded object's storage is cleaned too (no fixture here holds
one, so that is what the code does rather than something measured); the
`\001CompObj` user type, which names the producing application **and
its UI language** (LibreOffice writes "Microsoft Word-Dokument" on an English
document); PowerPoint's `Current User` stream, whose userName is the last person
to edit the deck; and Excel's `WRITEACCESS` record, which is the Excel user name
and is not in a property stream at all.

**Partial, and here is exactly what survives.** Measured with exiftool 13.59 on
a scrubbed `.doc`:

```
[MS-DOC] CreateDate, ModifyDate, LastPrinted, RevisionNumber,
         TotalEditTime, Words, Characters, Pages, Paragraphs, Lines
```

Word keeps a second copy of its timestamps and statistics in the DOP inside the
`WordDocument`/`Table` streams. The give-away that it is a second carrier and
not a leftover: the surviving `CreateDate` is shifted by the local UTC offset,
because it is now being read from a local-time field in the DOP instead of the
FILETIME in the property set. `SttbfAssoc` and `SttbSavedBy`, which duplicate
the author and title and record the last ten save paths, also survive on
documents written by Microsoft Word. They are deliberately not touched:
LibreOffice's filter does not write them (measured, zero sentinel hits in
`1Table`), so any code aimed at them would be untested surgery against a
structure found through a version-dependent offset table. Being able to test the
property streams did not make the rest testable.

**Needs `olefile`**, which `requirements.txt` installs. Without it the engine
reports itself unavailable and `metascrub doctor` says so, rather than the
format silently disappearing.

### SVG, and the hole you should know about

exiftool cannot write SVG at all (`exiftool -all=` answers *"ExifTool does not
yet support writing of SVG images"* and exits 1), so `svg_engine.py` edits the
XML text directly while exiftool still reads the baseline and the verification.

**Removed:** XML comments, the `<metadata>` RDF block with its `dc:title`,
`dc:creator`, `dc:rights` and `dc:description`, `<sodipodi:namedview>` (which
records the author's window position, size, zoom and current layer), every
`sodipodi:`, `inkscape:` and `ooo:` attribute, and the root `<title>` and
`<desc>`. That set includes `sodipodi:docname`, the file's name on the author's
disk; `inkscape:version`, the exact editor build; and
`inkscape:export-filename` and `sodipodi:absref`, which are absolute paths
through the author's home directory. The `xmlns:sodipodi`, `xmlns:inkscape` and
`xmlns:ooo` declarations go too, but only once the prefix is confirmed unused,
because a leftover declaration still announces which editor made the file.

**Kept, deliberately:** everything that draws. `xmlns`, `viewBox`, `width`,
`height`, `preserveAspectRatio`, `style`, geometry, `transform`, every `id`
(they are the targets of `<use>`, of CSS and of gradient links), `<text>`
content, and any `<title>` or `<desc>` nested inside a shape, which is that
shape's accessibility name and is announced by screen readers. Only the ROOT
`<title>` and `<desc>` are removed, and that line is measured rather than
chosen: exiftool reports only those two, so only those two can ever become
residual-scan needles.

**Partial, and this one deserves a sentence rather than a table cell.** An SVG
can embed a whole JPEG as a `data:image/jpeg;base64,` URI, and that JPEG keeps
its own EXIF, including the photographer's name and the camera's GPS
coordinates. Measured: exiftool reports nothing about it, so there is no needle
to search for, and the residual byte scan finds nothing because the value is
base64-wrapped. **The file reports `verified clean` while carrying a location.**
It is not removed because removing it deletes the picture. The same applies to
an `xlink:href` pointing at `file:///C:/Users/<name>/...`, which leaks a
username. A third survivor is milder and was found by running the engine against
a genuine Inkscape file rather than only the test fixture: a `style` attribute
can carry `-inkscape-font-specification:Droid Sans Mono`, which is a CSS
property rather than a namespaced attribute, so the attribute sweep does not see
it. On Inkscape 0.48.3.1 output, 15 of them survived a strip that removed
everything else. All three are reported as `NOTE:` lines in the result, and the
external reference is named in full so you can act on it. If a drawing came out
of a photo-tracing workflow, treat a scrubbed SVG as reduced, not clean.

`tests/test_svg.py` enforces that claim rather than describing it: it decodes
the data URI back out of the scrubbed output and asserts the EXIF sentinel is
still in the decoded bytes. If a later version starts removing it, that test
fails and the note above has to change with it.

---

## Notes worth knowing

- **Every format in the table was measured, not assumed.** A format is listed
  only when a fixture carrying a unique sentinel could be built, the full
  pipeline removed that sentinel from the output bytes, and the file still
  opened afterwards. `exiftool -listwf` reporting a format as writable is not
  sufficient evidence and was not treated as such: of 26 raw extensions
  probed, 11 passed and 7 were rejected for failing the fixture test.
- **TIFF cannot have its IFD0 dropped.** exiftool answers *"Can't delete IFD0
  from TIFF"* and leaves `Artist` and `Copyright` in place, because in a TIFF
  the EXIF IFD *is* the image structure. The exiftool engine follows `-all=`
  with a targeted sweep of identity tags, which does remove them.
- **That sweep is an allowlist, deliberately.** `exiftool -ImageWidth=` is
  accepted on a TIFF and produces a file Pillow can no longer open, so sweeping
  every surviving tag would corrupt images while reporting success.
- **A failed metadata read is an error, never a clean file.** These are two
  states that must never share one representation, and they used to: the read
  path caught every exception and returned an empty dict, so "we could not read
  it" and "it carries nothing" were the same value. The scrubber read that value
  as CLEAN and returned before any engine ran. Measured 2026-09-04 on a machine
  without exiftool on PATH: a PDF carrying its author string twice reported
  `CLEAN`, `verified clean 1`, exit 0, and still carried it twice afterwards. It
  affected every format, because every engine reads its baseline through
  exiftool. `ExifSession.read()` now returns a `MetadataRead` carrying `ok`, an
  unreadable baseline fails the file, and `tests/test_failopen.py` asserts over
  the whole format cross-product that nothing reports clean when metadata cannot
  be read.
- **A file exiftool could not PARSE is an error too, and it is a different
  state from both of those.** Found by audit 2026-09-04, one level up from the
  fail-open above and wearing the successful read's clothes. Measured with
  exiftool 13.29 on a truncated `.svg` carrying `sodipodi:docname`: exiftool
  reports `FileType: SVG`, exits zero, emits `XMP format error (no closing tag
  for svg)`, and surfaces no SVG tags at all. The read SUCCEEDS, so `ok` is
  true; everything it emits sits in the `File` and `ExifTool` pseudo-groups, so
  the scrubber saw an empty document and reported "no metadata carriers found"
  about a file carrying an editor's docname.
  `ExifSession.read()` now returns `ReadOutcome.PARSED`, `UNPARSED` or `FAILED`,
  and an unparseable baseline is refused for the same reason an unreadable one
  is: it gives the residual scan no needles, so nothing an engine did afterwards
  could be verified. No engine runs, no backup is written, no byte moves, and
  the run exits non-zero.
  The predicate is an allowlist of warnings measured to occur on files that are
  FINE, not a denylist of warnings that look alarming. Over the whole fixture
  corpus, 31 formats read before and after scrubbing, exactly one family
  appeared on a good file: `Unrecognized MIMEType
  application/vnd.oasis.opendocument.*-template` on `.ott`, `.ots`, `.otp` and
  `.otg`. Those four decide the design, because a scrubbed ODF template reports
  zero document tags and so lands in the exact shape of the defect while being a
  file that must stay CLEAN. What makes that family benign is not its wording:
  exiftool emits it on reads that also return the document's Title and Creator,
  and a warning raised while successfully reporting document tags cannot be
  evidence that the document was not parsed. `tests/test_unparseable.py` holds
  both populations across every shipped format.
- **Existing backups are never overwritten.** The original the parent project code wrote
  `<file>.backup` unconditionally, so sanitizing the same file twice replaced
  the pristine backup with the already-sanitized copy and destroyed the only
  remaining original.
- **`-overwrite_original` is always passed.** Without it exiftool leaves a
  `_original` sidecar, which is an unsanitized duplicate of the input sitting
  next to the output.
- **Non-ASCII paths work.** They need both `encoding="utf-8"` and
  `-charset filename=UTF8`; without them exiftool reports "No matching files",
  which looks like a skipped file rather than an encoding problem.
- Container rewrites are atomic (`os.replace` onto a temp file in the same
  directory), so an interrupted run cannot leave a truncated document. The OLE2
  engine edits a copy for this reason specifically: `olefile` writes in place,
  and an interrupted in-place edit of a compound file leaves a document that
  opens as garbage.
- **A stream's own name is not a leak.** An OLE2 directory stores stream names
  as UTF-16LE, so a metadata value that happens to equal one of them is found by
  a raw byte search of a file that is genuinely clean. Measured: LibreOffice
  writes `Current User` as the PowerPoint user name, which is also the name of
  the stream holding it, and a clean `.ppt` was reported as still leaking. The
  residual scan now blanks the 64-byte name field of each directory entry and
  nothing else, so payloads, the FAT and sector slack are all still searched;
  `tests/test_ole2.py` plants the same string back into a stream payload and
  requires the scan to find it, so the exclusion cannot quietly become a way to
  miss a real survivor.
- **Do not trust a converter's failure as evidence about your file.** Checking
  that a scrubbed `.xls` still opens by running `soffice --convert-to txt` fails
  with "no export filter ... found" on a pristine file, because Calc has no
  plain-text filter. Read as damage, it accuses the scrubber of corrupting a
  document it handled correctly. The validity tests convert the untouched
  fixture first as a control, so a conversion that cannot work here skips
  instead of failing.

---

## Tests

```bash
python -m pytest tests/ -q
```

The suite asserts over a cross-product of (format x remove_all x backup) rather
than one file per format, and every fixture embeds a unique sentinel that the
tests search for **in the output bytes**, never by asking an engine whether it
succeeded. `tests/test_boundaries.py` covers zero-byte files, files with no
metadata, extensions that lie about their container, read-only files, non-ASCII
paths, backup preservation and restore round-trips.

`tests/test_failopen.py` covers the case where metadata cannot be read at all,
including the detached-process import path where there is no stdout to fail on.
`tests/test_gui.py` drives the real widget tree and the real worker thread,
including a scrub asserted against the output bytes rather than against the
window's own verdict. `tests/test_theme.py` enforces the contrast floor and the
rule that colour is never the only channel carrying a verdict, and
`tests/test_selftest.py` checks that the self test can actually fail.

`tests/test_ole2.py` covers the legacy compound files: that the fixture really
is one, that the property streams come out empty AND zero-filled rather than
merely unreferenced, that the stream inventory and every stream length are
unchanged, that LibreOffice can still open the result, and that a file the
engine cannot safely handle is refused with the input left untouched.

`tests/test_odf.py` covers OpenDocument: that the fixture is a real package
carrying every seeded value, that the printer name is gone in its plain
spelling, in its base64 spelling, and inside every base64 config item that
remains, that the layout config-items survive so the allowlist cannot quietly
become a denylist, that `mimetype` comes back first and STORED, that libmagic
still names the document type, and that a naive all-deflated rebuild really is
detectable, constructed on purpose so the argument cannot outlive its evidence.
The core of it needs no external tool at all; the LibreOffice and libmagic
layers skip when those are absent.

492 passing and 4 skipped as of 2026-09-04, in 187 seconds on the dev machine (292 and
4 before the ODF engine landed). All four skips are deliberate:
`.doc`, `.xls` and `.ppt` skip the COMPLETE-formats check because they are
declared PARTIAL, and one test that exercises the missing-LibreOffice path skips
on a machine that has LibreOffice.

---

## Relationship to the parent project

The public API is intentionally shaped like the the parent project `ExifSanitizer`
(`sanitize_file`, `sanitize_directory`, `restore_backup`,
`generate_sanitization_report`, context manager, `backup` flag), and
`ExifSanitizer` is exported here as an alias. the parent project can adopt this engine
without rewriting its call sites.

That adoption is the intended end state. Two copies of this logic growing format
support independently is exactly the failure already visible in the the parent project repo,
where `gui/sanitizer.py` is a stale v1.0.0 fork of `cli/sanitizer.py`. The
adoption is tracked outside this repository.
