# Metadata-Scrubber (`metascrub`)

Removes metadata from documents, images, audio and video, then **proves the
removal actually happened** instead of assuming it did.

Built on the `ExifSanitizer` from `the parent project` (`cli/sanitizer.py`), which
handled eight image extensions through one engine. This extends that to four
engines across 55 extensions, and replaces "no exception was raised" with
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
  PASS engines      all 4 ready
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
| Office | docx docm xlsx xlsm pptx pptm | zip rebuild | complete |
| Video | mp4 m4v mov mkv webm avi f4v m4b ts m2ts | ffmpeg remux | complete |
| Audio | mp3 m4a flac wav ogg opus aiff aif | ffmpeg remux | complete |

`complete` and `partial` are separate states carried in every result, because
"we support this format" and "we can fully clean this format" are different
promises. A partial result always says so.

### Not handled, and why

- **`.doc`, `.xls`, `.ppt`** (legacy OLE2) are **deferred, not shipped**. Their
  metadata lives in `\005SummaryInformation` streams, and `olefile` can only
  overwrite a stream in place at its existing length. It is not shipped because
  it could not be tested: there is no way on this machine to produce a genuine
  legacy Office document to build a fixture from, and an untested rewrite path
  for legacy documents can corrupt files a user cannot regenerate. A clear
  refusal beats a half-working rewrite.
  **Discharge condition:** these move into the capability table only when
  `tests/fixtures/ole2/` exists and `tests/test_ole2.py` passes against it.
  `tests/test_coverage_gate.py` fails if anyone ships them without that, so the
  deferral cannot be discharged by forgetting it.
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
- **`.qt`, `.mqv`, `.lrv`, `.f4a`** are deferred, not refused. ffmpeg muxes them
  when told the format explicitly, but the av engine names its temporary file
  with the target extension and lets ffmpeg infer the muxer, and ffmpeg binds no
  output muxer to those extensions (`Error opening output files: Invalid
  argument`). They need an explicit extension-to-muxer map in the engine.
  **Discharge condition:** they move into the table when `av_engine.py` selects
  the output format explicitly and a fixture for each passes the byte search.
- **Tracked changes and comments** in Office documents are detected and
  **reported, not removed**. They are user-visible content, not metadata, and
  deleting them silently would destroy work.

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
  affected every format, because all four engines read their baseline through
  exiftool. `ExifSession.read()` now returns a `MetadataRead` carrying `ok`, an
  unreadable baseline fails the file, and `tests/test_failopen.py` asserts over
  the whole format cross-product that nothing reports clean when metadata cannot
  be read.
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
  directory), so an interrupted run cannot leave a truncated document.

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

229 passing and 1 skipped as of 2026-09-04. The skip is deliberate:
`tests/test_coverage_gate.py` refuses to enforce OLE2 while it is deferred, and
fails if anyone ships it without fixtures.

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
