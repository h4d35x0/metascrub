# CLAUDE.md - Metadata-Scrubber

**Project:** metascrub
**Description:** Removes metadata from documents, images, audio and video, and proves the removal happened by searching the output bytes.
**Stack:** Python 3.9+, exiftool, pikepdf, ffmpeg, Tkinter
**Owner:** h4d35x0

This file exists because `Utilities-Tools/CLAUDE.md` one level up is still an
unfilled template, so a session opened here would otherwise load placeholders.
Read `README.md` for the user-facing picture; this file is the things a session
needs that the README does not say.

---

## The one idea this project rests on

Success is **measured, never inferred**. A tool that re-reads a file with the
same engine that wrote it is not verifying anything.

Measured 2026-09-04, and re-confirmed on exiftool 13.59: `exiftool -all=
-overwrite_original` on a PDF makes the file GROW by 327 bytes, reports no
Author when read back, and leaves both copies of the author string in the
bytes, because exiftool edits a PDF by appending an incremental update and the
old objects stay. exiftool warns about this itself.

So: capture metadata values BEFORE touching a file, then search the OUTPUT
BYTES for those exact values afterwards. Any change that weakens that, in the
code or in a test, is a change to the point of the project.

**Never write a test that asks an engine whether it succeeded.** Every fixture
embeds a unique sentinel and the assertion searches the bytes for it.

---

## Traps already paid for. Do not re-learn these.

1. **exiftool is required for EVERY format, not just images.** All five engines
   read their baseline through it, even though pdf writes with pikepdf, ooxml
   with zipfile, ole2 with olefile and av with ffmpeg. Without it the tool must
   refuse, and does.

2. **"Could not read" and "carries nothing" must never share a representation.**
   `ExifSession.read()` used to swallow every exception into an empty dict, so
   with exiftool absent every file of every format reported CLEAN, verified
   clean, exit 0, untouched. It now returns `MetadataRead(metadata, ok, error)`.
   The same rule applies anywhere else a sentinel means "unset".

3. **Tk is not thread safe, and it deadlocks rather than raising.** Reading a Tk
   variable (`backup_var.get()`) from the worker thread hung every run with no
   error. Read Tk variables on the main thread; hand the worker plain values.

4. **A detached `pythonw` has `sys.stdout` AND `sys.stderr` set to `None`.**
   A module-level `sys.stdout.isatty()` therefore killed `pythonw -m metascrub
   gui` with no window and no traceback. Any module-level probe of the standard
   streams is a landmine for GUI processes.

5. **One Tk interpreter per test session, shared via `conftest.tk_root`.** A
   second live interpreter in one process fails outright, and per-test roots
   intermittently failed and SKIPPED, hiding GUI tests behind a green suite.

6. **A GUI must never render a verdict as colour alone.** DANGER and OK measure
   a 1.07 contrast ratio against each other. The Status and Verification
   columns carry the distinction as text; `tests/test_theme.py` enforces it.

7. **PDF is never scrubbed with exiftool.** Use the pikepdf full-rewrite engine.
   See trap 0 above for why.

8. **An ODF package rebuilt the OOXML way passes every obvious check and is
   still broken.** The `mimetype` member must be FIRST and STORED. Measured
   2026-09-04 on a rebuild that deflated it: LibreOffice opens it, exiftool
   reports `FileType: ODT`, and `file` (libmagic 5.45) reports
   `Zip data (MIME type "K,("?)` instead of `OpenDocument Text`. Only a content
   sniffer notices, so `test_mimetype_is_first_and_stored` is the always-on
   guard and `test_libmagic_still_identifies_the_package` is the belt. This is
   why `odf_engine.py` is a separate engine and not a flag on `ooxml_engine.py`.

9. **`settings.xml` in an ODF file carries the workstation's printer name and
   its Windows DEVMODE blob, and the residual scan cannot see either.** exiftool
   does not report `settings.xml` at all, so `verify.meaningful_values()` never
   makes a needle out of it. The engine removes it; only the TESTS confirm the
   removal, because they know the sentinel independently. Same asymmetry as
   `w:rsid`. Never assume the residual scan is covering a carrier exiftool does
   not read.

---

## Layout

| Path | Purpose |
|---|---|
| `metascrub/scrubber.py` | orchestrator, statuses, backup policy |
| `metascrub/verify.py` | the residual byte scan; the heart of the project |
| `metascrub/exif_io.py` | shared exiftool session, `MetadataRead` |
| `metascrub/capabilities.py` | format table: engine + completeness per extension |
| `metascrub/engines/` | exiftool, pdf (pikepdf), ooxml (zip), odf (zip), ole2 (olefile), av (ffmpeg) |
| `metascrub/gui.py` | Tkinter window; a view over MetadataScrubber, never a fork |
| `metascrub/theme.py` | Windows 95 palette and its contrast floor |
| `metascrub/selftest.py` | end-to-end proof for a new machine |
| `bin/` | real launchers; the project is runnable without installing |

`<volume>\Projects\.tools\bin\` holds forwarders to `bin/` and no logic.
`<volume>\Projects\.tools\exiftool\` is a Windows exiftool that travels.

---

## Commands

```bash
python -m pytest tests/ -q          # 492 passed, 4 skipped, 187s, 2026-09-04
                                    # (292 / 4 before the ODF engine landed)
python -m metascrub selftest        # end-to-end, including a real round trip
python -m metascrub doctor          # can the engines start
python -m metascrub gui
```

The 4 skips are intentional: .doc, .xls and .ppt are PARTIAL so they skip the
COMPLETE-formats check, and one test of the missing-LibreOffice path skips where
LibreOffice is present.

**exiftool on this machine** lives at
`C:\Users\user\AppData\Local\Programs\ExifTool\` and may not be on
the PATH of a shell started before it was installed.

---

## Conventions specific to this project

- **`Completeness.PARTIAL` is a real state and must stay separate from
  COMPLETE.** "We support this format" and "we can fully clean it" are
  different promises. Raw formats are PARTIAL because maker notes survive.
- **A deferral must be enforced by a test, not by memory.** OLE2 was deferred
  and `tests/test_coverage_gate.py` collected it on 2026-09-04. DEFERRED is
  empty now; the mechanism stays for the next one.
- **Never overwrite an existing `<file>.backup`.** It is the only remaining copy
  of the pre-sanitize original.
- **The GUI is a view over `MetadataScrubber`, never a second implementation.**
  The whole reason this project exists is that `the parent project/gui/sanitizer.py`
  is a stale fork of `cli/sanitizer.py`.
- No em dashes, ASCII punctuation only. Writing docs through Python string
  literals has silently turned `\bin` into a backspace three times; use raw
  strings and scan the result for control characters.

---

## Open work

See `tasks/todo.md`. The short version: no git remote yet, the parent project adoption is the
stated end state, and the `sh` launcher has never run on real macOS or Linux.
OLE2 shipped on 2026-09-04 as PARTIAL; the Word SttbfAssoc and SttbSavedBy
carriers are still untouched because no fixture here writes them.
ODF shipped on 2026-09-04 as COMPLETE for all eight extensions. Flat XML
OpenDocument (`.fodt` `.fods` `.fodp`) is in `DEFERRED` with a discharge
condition, which is what made `DEFERRED` non-empty again; the four av
extensions are still enforced by prose only and that is the AV team's item.
