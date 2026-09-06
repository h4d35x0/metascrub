# Changelog

Notable changes, newest first. Dates are the release date.

This file exists for people working from a clone. A clone does not update
itself, and the GitHub release page is invisible from inside one, so without
this there is no way to tell from the source tree what changed or whether it
matters to you.

Versions follow [semantic versioning](https://semver.org/). The PyPI package is
`scrubproof`; the import and the command are both `metascrub`.

---

## [1.0.2] - 2026-09-06

**If you scrubbed a `.png`, `.webp` or `.gif` with any earlier version and you
relied on the "verified clean" verdict, re-scrub it with this one.** Those
files may have carried data the tool could not see and did not report.

### Fixed

- **A `.webp`, `.png` or `.gif` could carry an arbitrary hidden payload past a
  "verified clean" verdict.** Measured on 1.0.1: a WebP holding an 8 KB MP4
  inside an unknown `MPVD` RIFF chunk was reported `SANITIZED ... verified
  clean` while the video and its `ftyp` header survived intact in the output.
  The same shape was then measured for an unknown PNG ancillary chunk and for
  data appended after a GIF trailer. Three formats, three false CLEAN verdicts.

  The engine was not at fault; exiftool removed everything it could see. The
  fault was in verification. `verify.py` searches the output for values that a
  baseline read captured beforehand, so a carrier exiftool never parses yields
  no values, yields no needles, and the scan passes having searched for
  nothing. The verdict's confidence exceeded the question that was asked.

  A new structural scan (`metascrub/structure.py`) asks a question that does
  not depend on the baseline read: after scrubbing, is there any region of the
  file that the format's own structure does not account for? PNG, WebP and GIF
  are walked chunk by chunk and block by block, and any unknown structure or
  trailing region is reported as the new verdict `structure_unaccounted`.

  **This release reports the problem and fails closed; it does not remove
  these carriers.** A file with an unexplained region is one this tool cannot
  currently clean, and saying so is the honest outcome. Removal needs a
  container rewriter per format and is deliberately not being rushed into a
  patch release.

  Formats other than PNG, WebP and GIF are unchanged: a format with no
  registered walker reports "not applicable", which is held distinct from
  "clean" for the same reason `ReadOutcome` distinguishes them in `exif_io.py`.

  Measured as NOT affected, and left alone: an unknown JPEG `APP9` segment, and
  trailing data appended to a JPEG, PNG or TIFF, are all removed correctly.
  Data past a WebP's declared RIFF size and a malformed GIF extension already
  failed closed before this change.

### Known limits, stated rather than left implicit

- The residual byte scan cannot see GPS coordinates, and no needle-based scan
  ever could. EXIF stores a coordinate as rationals (`43/1 39/1 14517/1250`),
  so the decimal string exiftool prints is not present in the file at all:
  searching the output bytes for `43.653226` finds nothing even in a file that
  plainly carries that coordinate. The engines do remove GPS, so this is a gap
  in what the tool can PROVE rather than a leak.

  Two mechanisms discard the values before any search happens, and neither is
  the digits filter (an earlier draft of this entry said it was, wrongly):
  `Composite:GPSPosition` is excluded by `_PSEUDO_GROUPS`, and
  `EXIF:GPSLatitude` and friends never reach a filter at all because exiftool
  is read with `-n`, which returns floats, and `_flatten()` returns an empty
  list for any value that is not a string or list. That second mechanism is
  much broader than GPS: measured on one geotagged JPEG, 9 of 14 non-pseudo
  tag values are discarded for being numeric.

  The fix is structural rather than needle-based. See
  `docs/D2-GPS-VERIFICATION.md`.
- The residual byte scan cannot see any compressed carrier. A value inside a
  PNG `zTXt` is not present in the file's bytes at all, so searching for it
  finds nothing whether or not the chunk survived.

---

## [1.0.1] - 2026-09-05

**If you scrubbed an Office document with 1.0.0, upgrade and scrub it again.**
The file records when you scrubbed it.

### Fixed

- **Scrubbed `.docx`, `.xlsx` and `.pptx` recorded the time they were
  sanitized.** Zip entries carry their own modification timestamps, and those
  are metadata. The engine normalised the timestamp of every member it copied
  and none of the members it replaced: writing an entry by name stamps the
  current clock, so four members carried the sanitizing time, and reusing the
  source entry preserves the original, so `[Content_Types].xml` and
  `_rels/.rels` kept the original editing session's timestamp. Every member is
  now pinned to the zip epoch, asserted across OOXML and all eight OpenDocument
  extensions.

- **`requires-python` said 3.9, which was never true.** pikepdf and Pillow both
  require 3.10, so a 3.9 install could only fail to resolve or silently pin
  versions this project has never been tested against. Corrected to `>=3.10`.
  A PyPI version cannot be re-uploaded, which is why this needed a release
  rather than an edit.

- **The Windows launcher ran the wrong Python.** `bin\metascrub.cmd` preferred
  the `py` launcher, which deliberately ignores virtual environments and selects
  the newest interpreter on the machine. With a venv active it ran a different
  Python, found none of the dependencies, and reported them all missing, which
  reads as a broken install rather than the wrong interpreter. It now prefers
  `python` from PATH, so an activated environment wins.

### Added

- **Continuous integration on Linux, macOS and Windows**, Python 3.10 through
  3.13, on every push. It verifies the POSIX launcher, which despite the README
  telling macOS and Linux users to run it had only ever run under Git Bash on
  Windows. All three fixes above were found by CI in its first two runs and none
  was reachable from a single developer machine.

- **The `Archive::Zip` requirement is documented.** On Linux, exiftool needs
  that Perl module to read inside a zip-backed document. Without it it reports
  only generic `ZIP:*` container tags and warns, and metascrub then correctly
  refuses to claim it verified the file, so every Office and OpenDocument format
  fails in a way that looks like a bug. Install `libarchive-zip-perl` on Debian
  and Ubuntu, or `perl-Archive-Zip` on Fedora and Arch. The Windows and macOS
  builds of exiftool bundle it.

### Changed

- The version is declared in one place, `metascrub/__init__.py`, and
  `pyproject.toml` reads it from there. It was declared in both, and they
  drifted.

---

## [1.0.0] - 2026-09-05

First public release. Removes metadata from documents, images, audio and video,
and proves it by searching the output bytes for the exact values the file
carried before it was touched. It never asks the tool that just wrote the file
whether the tool did its job.

### Added

- **71 extensions across 7 engines**: exiftool for images and raw photo, pikepdf
  for PDF, a zip rebuild for OOXML, a separate zip rebuild for OpenDocument,
  olefile for legacy Office, an XML engine for SVG, and ffmpeg for audio and
  video. Every format was measured end to end before it shipped: a fixture
  carrying a unique sentinel goes in, the sentinel must be absent from the
  output bytes, and the file must still open. Formats that failed that test are
  listed in the README as unsupported with the measured reason.

- **OpenDocument support** for `.odt .ott .ods .ots .odp .otp .odg .otg`,
  including the printer name and the Windows printer driver blob that a common
  office suite writes into `settings.xml` of every document. No metadata reader
  reports that file, so nothing else finds it.

- **SVG support**, PARTIAL, with what survives named rather than hidden: EXIF
  inside a base64-embedded raster, external `file:///` references, and
  `-inkscape-*` CSS properties inside style attributes.

- **Four more audio and video containers**, `.qt .mqv .lrv .f4a`, on a muxer
  choice that reads the input's `ftyp` brand instead of trusting the extension.
  A static extension-to-muxer map silently returned four of eight
  extension/flavour combinations in the other container while every check
  passed.

### Fixed

- **A file exiftool cannot parse is no longer reported CLEAN.** It identifies
  the file and does not fail, it simply surfaces nothing, and the tool then
  short-circuited to "no metadata carriers found" about a file that carried
  them. A false clean is the one direction this tool must never fail in.

- **A PDF with bookmarks is no longer reported as still leaking.** `/PageMode`
  and `/PageLayout` are closed enumerations describing how a reader should open
  the document, not who made it. The tool cleaned such files correctly and then
  told the user it had failed.

- **A correctly cleaned SVG is no longer reported as leaking** on its own
  namespace declaration.

[1.0.1]: https://github.com/h4d35x0/metascrub/releases/tag/v1.0.1
[1.0.0]: https://github.com/h4d35x0/metascrub/releases/tag/v1.0.0
