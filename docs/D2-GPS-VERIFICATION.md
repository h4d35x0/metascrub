# D2: the residual byte scan cannot see a GPS coordinate

Status: OPEN. Design document, not an implementation. No code changed.

Target: `metascrub` 1.0.2 (`metascrub/__init__.py` reports `1.0.2`).

Everything below labelled "measured" was run on 2026-09-06 on the dev machine:
Windows 11, Python 3.11.3, exiftool 13.29 at `C:\Tools\exiftool-13.29_64`
(on PATH), pyexiftool through `metascrub.exif_io.session()`, ffmpeg
`2024-12-11-git-a518b5540d`, Pillow, pikepdf. Anything not measured is labelled
INFERENCE and is listed again at the end.

---

## 1. The measured defect

### 1.1 The reproduction

A 64x48 JPEG was written by Pillow, then given an Artist sentinel and a full
EXIF GPS fix by exiftool:

```
exiftool -Artist=SENTINEL-ARTIST-D2-AAAA \
  -GPSLatitude=43.653226 -GPSLatitudeRef=N \
  -GPSLongitude=-79.383184 -GPSLongitudeRef=W \
  -GPSAltitude=76.5 -GPSAltitudeRef=0 gps.jpg
```

Read through `metascrub.exif_io.session().read()` (which supplies
`-G -n -charset filename=UTF8`), the read came back `ReadOutcome.PARSED` with
41 keys, of which these are the GPS carriers:

```
Composite:GPSAltitude    76.5                     float
Composite:GPSLatitude    43.653226                float
Composite:GPSLongitude   -79.383184               float
Composite:GPSPosition    '43.653226 -79.383184'   str
EXIF:Artist              'SENTINEL-ARTIST-D2-AAAA' str
EXIF:GPSAltitude         76.5                     float
EXIF:GPSAltitudeRef      0                        int
EXIF:GPSLatitude         43.653226                float
EXIF:GPSLatitudeRef      'N'                      str
EXIF:GPSLongitude        79.383184                float
EXIF:GPSLongitudeRef     'W'                      str
EXIF:GPSVersionID        '2 3 0 0'                str
```

`verify.meaningful_values()` on that dict returned exactly ONE needle:
`'SENTINEL-ARTIST-D2-AAAA'`. The defect reproduces. Confirmed.

### 1.2 The premise names the wrong filter. Three corrections.

The brief says the GPS values "are discarded by the pure-digits-and-separators
filter in verify.py". Measured, per value, by replaying the filter chain by
hand:

| tag | what actually drops it |
|---|---|
| `Composite:GPSAltitude` | `_PSEUDO_GROUPS` contains `Composite` |
| `Composite:GPSLatitude` | `_PSEUDO_GROUPS` |
| `Composite:GPSLongitude` | `_PSEUDO_GROUPS` |
| `Composite:GPSPosition` | `_PSEUDO_GROUPS` |
| `EXIF:GPSAltitude` | `_flatten()` returns `[]` for a `float` |
| `EXIF:GPSAltitudeRef` | `_flatten()` returns `[]` for an `int` |
| `EXIF:GPSLatitude` | `_flatten()` returns `[]` for a `float` |
| `EXIF:GPSLongitude` | `_flatten()` returns `[]` for a `float` |
| `EXIF:GPSLatitudeRef` | `_MIN_NEEDLE`, `'N'` is 1 char |
| `EXIF:GPSLongitudeRef` | `_MIN_NEEDLE`, `'W'` is 1 char |
| `EXIF:GPSVersionID` | `_MIN_NEEDLE`, `'2 3 0 0'` is 7 chars |

**The pure-digits-and-separators filter drops none of them.** On this file it
fires exactly once, on `EXIF:DateTimeOriginal` when that tag is present, and
never on a GPS tag.

Three separate corrections follow:

1. **`Composite:GPSPosition` is dropped by `_PSEUDO_GROUPS`, not by the digits
   filter.** `verify.py:50` lists `Composite` as a pseudo-group and `Composite`
   is checked before any value-level filter runs. The value would ALSO be
   caught by the digits filter if it ever got there (measured:
   `'43.653226 -79.383184'.replace(...).isdigit()` is `True`), so the
   conclusion "it is discarded" holds. The mechanism does not. This matters
   because the fix implied by the brief, loosening the digits filter, does not
   touch `_PSEUDO_GROUPS` and would leave `GPSPosition` exactly where it is.

2. **The `EXIF:GPS*` numeric tags never reach any filter at all.** With `-n` in
   `common_args`, pyexiftool returns them as Python `float` and `int`.
   `verify._flatten()` returns `[]` for anything that is not `str`, `list` or
   `tuple`. This is a THIRD mechanism, it is not GPS-specific, and it is much
   larger than the GPS defect: across the eight-file corpus built for this
   work, **218 of 334 non-pseudo tag values are discarded by `_flatten()`
   because they are not strings**, of which 16 are GPS tags. Every numeric
   metadata value in the tool is invisible to the residual scan, not only the
   coordinates.

3. **The length figure in `docs/ANDROID-MEDIA-BUILD.md` line 447 and in the
   1.0.2 CHANGELOG is coordinate-dependent, not a constant.** That text says
   `Composite:GPSPosition` is "a 22-character string". Measured for
   `43.653226 -79.383184`: `len()` is **20**. The claim it supports (clears
   `_MIN_NEEDLE = 8`) is true for any real coordinate, so nothing downstream
   changes, but the number is not a property of the tag.

### 1.3 The engines do remove GPS. Measured, not assumed.

Eight GPS-bearing files were scrubbed through `MetadataScrubber(backup=False)`
and the output re-read. All eight: status `sanitized`, verdict
`verified_clean`, **zero** tags whose name contains `GPS` reported by exiftool
in the output, and zero of the stored coordinate string forms present in the
output bytes.

| file | engine | GPS tags in output | stored GPS strings in output bytes |
|---|---|---|---|
| `photo.jpg` (EXIF GPS) | exiftool | none | none |
| `shot.png` (EXIF GPS) | exiftool | none | none |
| `pic.webp` (EXIF GPS) | exiftool | none | none |
| `scan.tif` (EXIF GPS) | exiftool | none | none |
| `xmp.png` (XMP GPS) | exiftool | none | none |
| `clip.mp4` (XMP GPS) | av | none | none |
| `xmp.mp4` (XMP GPS in uuid) | av | none | none |
| `udta.mp4` (ISO6709 `(c)xyz`) | av | none | none |

So D2 is confirmed as a gap in what the tool can PROVE, not a live leak, on
these eight files with these two engines.

### 1.4 What is actually proving GPS removal today: nothing

For each of the same eight outputs, `structure.scan()` was called directly:

| file | `applicable` | `unaccounted` |
|---|---|---|
| `photo.jpg` | False | [] |
| `scan.tif` | False | [] |
| `clip.mp4`, `xmp.mp4`, `udta.mp4` | False | [] |
| `shot.png`, `xmp.png` | True | [] |
| `pic.webp` | True | [] |

`structure.py` registers walkers for `.png`, `.webp` and `.gif` only, so it has
no opinion at all on five of the eight. On the three where it does apply it
reports zero unaccounted regions, which is correct and is also silent about
GPS by design: its own docstring says "It also does not flag known metadata
chunks that survived."

Net: for a scrubbed geotagged photo, the `verified_clean` verdict currently
rests on the engine read-back alone for the GPS question. That is the exact
inference `verify.py`'s module docstring exists to refuse.

---

## 2. Why the obvious fix does not work

### 2.1 Is the coordinate findable in the output bytes at all?

For each container, the file was searched for eleven candidate string forms of
the same coordinate, in UTF-8, UTF-16LE and Latin-1, in a file that plainly
carries the coordinate.

| container / carrier | ASCII coordinate present in bytes? |
|---|---|
| JPEG, EXIF GPS IFD | **NO** |
| PNG, EXIF GPS IFD | **NO** |
| WebP, EXIF GPS IFD | **NO** |
| TIFF, EXIF GPS IFD | **NO** |
| PDF | no GPS carrier written |
| MP4, XMP in `uuid` box | YES, as `43,39.19356N` and `79,22.99104W` |
| PNG, XMP in `iTXt` | YES, as `43,39.19356N` and `79,22.99104W` |
| MP4, QuickTime `(c)xyz` atom | YES, as `+43.653226-079.383184+76.500/` |

EXIF stores GPS as three `rational64u` pairs. `exiftool -v3` on the JPEG:

```
GPSLatitude = 43 39 11.6136 (43/1 39/1 14517/1250)
  - Tag 0x0002 (24 bytes, rational64u[3]):
      00f6: 00 00 00 2b 00 00 00 01 00 00 00 27 00 00 00 01
```

So for the entire TIFF/EXIF family the decimal string is not in the file and no
search for it can ever succeed. That half of the brief's hypothesis is
confirmed: **a needle built from exiftool's printed decimal is impossible in
principle for EXIF GPS.**

### 2.2 exiftool never hands us the string that IS in the bytes

For the two carriers that DO store ASCII, the value exiftool returns still does
not match. Measured on the same files with `-G -n` and with `-G` (print
conversion on):

| tag | `-n` value | print-conv value | either present in bytes? |
|---|---|---|---|
| `XMP:GPSLatitude` | `43.653226` (float) | `43 deg 39' 11.61" N` | NO |
| `EXIF:GPSLatitude` | `43.653226` (float) | `43 deg 39' 11.61"` | NO |
| `Composite:GPSPosition` | `'43.653226 -79.383184'` | `43 deg 39' 11.61" N, ...` | NO |
| `QuickTime:GPSCoordinates` | `'43.653226 -79.383184 76.5'` | `43 deg 39' ...` | NO |

The literal `43,39.19356N` is only obtainable from the raw packet
(`exiftool -xmp -b`), which returns `<exif:GPSLatitude>43,39.19356N</...>`.
Neither read mode the tool uses, nor the other one available, produces it.

### 2.3 The positive control: does loosening the filter fix anything?

Three needle policies were run against eight files that plainly carry GPS, with
the file used as its own haystack. If a policy cannot find the coordinate in a
file that has it, it can never find a survivor.

- A: shipped `meaningful_values()`
- B: identical, with the digits filter removed
- C: identical, with the digits filter removed AND whitespace-splitting of
  multi-value strings

| file | A finds GPS | B finds GPS | C finds GPS |
|---|---|---|---|
| `photo.jpg` | no | no | no |
| `shot.png` | no | no | no |
| `pic.webp` | no | no | no |
| `scan.tif` | no | no | no |
| `xmp.png` | no | no | no |
| `clip.mp4` | no | no | no |
| `xmp.mp4` | no | no | no |
| `udta.mp4` | no | no | **yes, latitude only** |

**Removing the digits filter fixes GPS detection on 0 of 8 files.** The maximal
naive variant, removing the filter and splitting, fixes 1 of 8, and on that one
it recovers the latitude only: the needle it forms for longitude is
`-79.383184` while the file stores `-079.383184`, so the sign-plus-zero-padding
difference makes it miss. Half a coordinate is not a verification.

### 2.4 What the digits filter is actually worth, both directions

Cost, measured: on the eight-file corpus it blocks 0 GPS detections that would
otherwise work.

Benefit, measured: across the same corpus scrubbed, the values it currently
drops (`EXIF:DateTimeOriginal` on the JPEG, six `0000:00:00 00:00:00` QuickTime
date tags on the MP4) survive in **0** output files, so on this corpus it
prevented 0 false positives as well.

Collision surface, measured. Runs of `[0-9][0-9.:\- ]{7,}` that are entirely
digits and separators, per file:

| file | bytes | such runs |
|---|---|---|
| `photo.jpg` | 1132 | 1 |
| `shot.png` | 859 | 0 |
| `pic.webp` | 302 | 0 |
| `scan.tif` | 9516 | 0 |
| `doc.pdf` | 5149 | 15 |
| `scrubbed.pdf` | 1025 | 10 |
| `clip.mp4` | 5890 | 6 |
| `udta.mp4` | 2900 | 5 |
| `exiftool.exe` | 58368 | 0 |
| `python.exe` | 103192 | 20 |
| total | 207789 | 67, or 338 per MB |

The density is dominated by PDF cross-reference tables (`0000000015 00000 `,
`0000000480 00000 `) and by version strings in executables. So the filter is
defending a real surface, it is simply not the surface that has anything to do
with GPS. The honest summary is that on this corpus the digits filter is close
to a no-op in both directions and it is a red herring for D2. Loosening it is
not a fix, and tightening it is not the problem.

---

## 3. Candidate designs

Four options. Each was exercised against a deliberately constructed leak: an
output scrubbed with `exiftool -all=` and then given ONLY its GPS back, which
is exactly what an engine that misses GPS would produce. Each was also run
against the genuinely clean output of `MetadataScrubber` for the same input, to
check for false positives.

| design | JPEG EXIF leak | MP4 `(c)xyz` leak | PNG XMP leak | fires on clean output |
|---|---|---|---|---|
| 1 shipped needle scan | MISS | MISS | MISS | no |
| 2 needle scan, digits filter off + split | MISS | catches latitude | MISS | no |
| 3 byte slice lifted from input GPS IFD | CATCH | n/a | n/a | no |
| 4 structural GPS assertion on the output | CATCH | CATCH | CATCH | no |

### Option 1: do nothing, keep documenting it

Cost: zero. Risk: the README and the CHANGELOG say `verified clean` while the
single most important value in a phone photo is checked by nothing but an
engine read-back. Silent failure mode: none new, but the existing one persists,
and it is the exact shape the project exists to refuse.

### Option 2: loosen `meaningful_values()`

Remove or narrow the digits filter, optionally split multi-value strings, and
teach `_flatten()` to stringify numbers.

Cost: small, one function. Risk: measured above, it fixes 0 of 8 and at best 1
of 8. Making `_flatten()` stringify floats would produce the needle
`'43.653226'` for EXIF GPS, and that string is measurably NOT in an EXIF file,
so the scan would search for it, find nothing, and report a clean file clean
for the wrong reason. **Silent failure mode: this is the worst option in the
set, because it produces a needle count that goes UP and a detection rate that
stays at zero.** `checked_values` would rise from 4 to 7 on `photo.jpg` and the
operator would reasonably read that as more coverage. It is not.

A second silent failure hides here: stringifying a float goes through Python's
`repr`, so `76.5` becomes `'76.5'` and `43.653226` becomes `'43.653226'`, but a
value exiftool returns as `1.0` becomes `'1.0'` where the file may hold `1`.
The needle would then be wrong in a way nothing tests.

### Option 3: a needle built from the stored rational representation

Locate the GPS IFD in the INPUT, lift the raw value bytes of each GPS tag as a
byte slice, and search the output for those slices. Do not reconstruct the
bytes, slice them, so byte order and denominators come from the file.

Measured: on the JPEG this lifts two 24-byte slices (tags `0x0002` and
`0x0004`), finds both in the leaky output and neither in the clean output.

Cost: a small TIFF/IFD walker (about 40 lines in the prototype), plus a way to
carry a bytes needle through a pipeline whose needles are currently `str` and
whose `_encodings()` helper assumes text. `residual_scan` and `Verification`
both take strings today.

Risk, both directions, and both are measured:

- **False negative on re-encoding.** The slice is only found if the output
  preserves the exact rationals and byte order. Measured: the same coordinate
  written by the same exiftool into a Pillow-authored TIFF is stored
  little-endian (`II`) while the JPEG is big-endian (`MM`), so the JPEG's
  24-byte slice is absent from the TIFF that carries the identical coordinate.
  Any engine that decodes and re-encodes GPS defeats this needle while leaving
  the coordinate perfectly readable.
- **False positive on low-entropy coordinates.** The slice is only specific if
  the numbers are. Measured against 107,429,025 bytes of noisy H.264 MP4 and
  3,423,232 bytes of `perl532.dll`:

  | slice | length | hits in noise.mp4 | hits in perl532.dll |
  |---|---|---|---|
  | `43/1 39/1 14517/1250` (the real latitude) | 24 | 0 | 0 |
  | `43/1 39/1` | 16 | 0 | 0 |
  | `0/1 0/1 0/1` (latitude 0, Null Island) | 24 | 0 | **1** |
  | `1/1` | 8 | 2 | **257** |
  | `0/1` | 8 | 12 | **173** |

  A photo geotagged at an integer coordinate, or at 0,0, produces a slice that
  occurs in ordinary binary. **Silent failure mode: the false positive is
  data-dependent, so it will pass every test written with an interesting
  coordinate and fire on a user's file.** Any implementation needs a minimum
  slice length AND a minimum-entropy rule, and the test corpus needs a boring
  coordinate in it deliberately.
- Scope: EXIF only. It says nothing about XMP or `(c)xyz`, which is 4 of the 8
  measured carriers.

### Option 4: a structural assertion that the output carries no GPS structure

Ask of the OUTPUT: does it still contain an EXIF GPS IFD with values, an XMP
`exif:GPS*` property, or a QuickTime `(c)xyz` / `GPSCoordinates` atom? This is
`docs/ANDROID-MEDIA-BUILD.md` Part 3.2 applied to one specific carrier class.

Measured: catches all three constructed leaks, fires on none of the three clean
outputs.

Cost: highest of the four. It needs a real parser per container family, not a
grep, and the project has no ISO BMFF box walker in `metascrub/` today (there
is a prototype at `<scratchpad>/isobmff_walk.py` from the phase 0 work, which is
not shipped code). It also needs a home: `structure.py` is the natural-looking
place and is the WRONG place as written, because its contract is "regions the
format does not account for" and a surviving GPS IFD is fully accounted for.
Putting GPS into `StructureReport.unaccounted` would change the meaning of the
`STRUCTURE_UNACCOUNTED` verdict for every existing file. It belongs in a
sibling with its own report type, or in a new field on `StructureReport` that
`verify.py` maps to a different verdict.

Risk, and here the measurements matter most:

- **A grep is not good enough, measured.** On the 102.45 MB noise MP4 the exact
  four bytes `\xa9xyz` occurred 0 times, but the SHAPE `\xa9` followed by three
  ASCII letters occurred 3620 times, which is 35.3 per MB. One in 17,576 such
  sequences is `xyz` by chance, so the expected number of false `(c)xyz` hits is
  about 0.21 per 100 MB. INFERENCE from those two measured numbers, not a
  measured false positive. The bare string `GPS` occurred 7 times in the same
  file, which has no GPS metadata at all. So the assertion must walk the box
  tree and the IFD, never scan for magic strings.
- **Selective removal is a measured false-positive path.** Running
  `python -m metascrub scrub --remove-field Artist --no-backup sel.jpg`
  removed the Artist, deliberately KEPT `Composite:GPSLatitude 43.653226`, and
  correctly reported `SANITIZED ... verified clean 1`. An unconditional "no GPS
  structure in the output" assertion would turn that correct run into a
  failure. The assertion has to be scoped by `only_fields` in the same way
  `scoped_values()` already is. **Silent failure mode: forget this and the tool
  reports a leak on a file the user explicitly asked to keep its GPS, which is
  the expensive kind of false positive `verify.py` already warns about in the
  `_PSEUDO_GROUPS` comment.**
- Second silent failure mode: an unparseable output. If the box walk or the IFD
  walk throws, "we could not check" must not collapse into "no GPS found".
  `StructureReport.error` already models this correctly and the new check must
  copy it exactly. This is trap 2 and trap 12 in a new place.

### Option 5: a dedicated field saying what was and was not checked

Add to `Verification` an explicit record of which carrier classes were
interrogated, so `verified_clean` stops implying GPS coverage it does not have.

Cost: low in `verify.py`, non-trivial at the edges. `Verification.as_dict()`
feeds `metascrub/cli.py` and `metascrub/gui.py`, and per trap 6 the GUI must
carry any new distinction as TEXT and not colour. Phase 0 section 8 of
`<scratchpad>/phase0-findings-harness.md` also warns that
`scrubber.py` downgrades a COMPLETE format with `spec.completeness is
Completeness.COMPLETE and not verification.clean`, an identity test that a new
enum member would fall straight through. A new FIELD is safe there; a new
`Completeness` member is not.

Risk: on its own it changes no verdict, so it cannot catch anything. Silent
failure mode: it is the option most likely to be shipped alone, declared as the
fix, and leave the detection gap exactly where it is while making the docs read
as though it were closed.

---

## 4. Recommendation

**Do option 4, scoped, with option 5 as its reporting surface, and do neither
option 2 nor option 3.**

Reasoning, from the measurements rather than from taste:

- Option 2 is disqualified by section 2.3: it fixes 0 of 8, and its failure
  mode raises `checked_values` while detection stays at zero, which is worse
  than the honest gap because it manufactures false confidence. Note this does
  NOT mean the digits filter is fine; it means the digits filter is unrelated
  to D2. If it is ever changed, it should be changed for its own reasons and
  with its own measurements.
- Option 3 works, and only for EXIF, which is 4 of the 8 carriers measured. It
  is defeated by any re-encode and it false-positives on boring coordinates. It
  is a second instrument, not a first one.
- Option 4 is the only design that caught all three constructed leaks, and it
  is the only one whose answer does not depend on the baseline read having
  produced a needle. That is the same argument that produced `structure.py` in
  1.0.2, applied to a carrier rather than to a region, and Part 3.2 of
  `docs/ANDROID-MEDIA-BUILD.md` already reached this conclusion for the mobile
  build: structural assertion is the gate, the byte search is the belt.
- Option 5 is required alongside it, not instead of it, because option 4 will
  ship covering some containers and not others (see section 5) and a verdict
  that does not say which is the same over-claim in a new place.

Concretely, the shape I would build:

1. A new module, sibling to `structure.py`, that answers one question about an
   OUTPUT file: which GPS carriers does it still contain? It returns a report
   with three states per container, matching `StructureReport`: not applicable
   (no walker for this format), checked and clean, checked and carrier found,
   plus an `error` that is never conflated with clean.
2. Real walkers, never greps: a TIFF/IFD walk for tag `0x8825`, an ISO BMFF box
   walk for `moov/udta/(c)xyz` and `moov/udta/XMP_` and `uuid`, and an XMP
   packet check for `exif:GPS*`. Registered by extension the way `_WALKERS` is,
   so an unregistered format reports "not applicable" and changes no verdict.
3. `verify.verify()` consults it after the residual scan and before the
   `after_readable` check, and reports a distinct verdict rather than folding
   into `RESIDUAL_FOUND`. A surviving GPS carrier is not "a value we captured
   survived"; it is "a carrier we can prove is still there", which is a
   different fact and a different remediation.
4. It is skipped entirely when `only_fields` is set and no GPS field is in
   scope, mirroring `scoped_values()`.
5. `Verification` gains a field naming the carrier classes actually
   interrogated for this file, surfaced as TEXT in the CLI and the GUI.

---

## 5. Which formats this spans

Measured this session: 5 container families, 8 files. JPEG EXIF, PNG EXIF, PNG
XMP `iTXt`, WebP EXIF, TIFF EXIF, MP4 XMP-in-`uuid`, MP4 QuickTime `(c)xyz`.

The capability table has 71 extensions. By engine: 30 on the exiftool engine
(`.jpg .jpeg .jpe .tif .tiff .png .webp .gif .psd .jp2 .heic .heif .avif` plus
17 raw formats), 22 on the av engine, and the rest are document formats.

INFERENCE, not measured: GPS can appear in EXIF for every TIFF-based extension
on that list including all 17 raws, in XMP for anything that can carry an XMP
packet including `.gif` and `.svg` and `.pdf`, in the ISO BMFF `udta`/`uuid`
carriers for `.mp4 .mov .m4v .qt .mqv .lrv .f4v .3gp` and for the ISO BMFF
still formats `.heic .heif .avif`, and in Matroska tags for `.mkv` and `.webm`.

**A fix that covers only JPEG covers 1 extension out of 71.** Saying that out
loud is the point of this section. The realistic first increment is: TIFF/IFD
walk (covers the JPEG, TIFF, raw and PNG-eXIf/WebP-EXIF cases in one walker,
since they all embed the same TIFF header), plus an XMP packet check (covers
PNG, MP4, GIF, SVG, PDF wherever XMP is reachable), plus an ISO BMFF box walk.
Anything not covered must report "not checked", never "clean".

---

## 6. The overcorrection tests, per trap 11

Trap 11's rule is that the test lands in the SAME commit as the change. These
are the tests I would require. They are described, not written, because this
document does not touch `tests/`.

**For any change to `meaningful_values()` (options 2 or 3):**

1. A three-axis test in the shape `tests/test_verify_structural.py` already
   uses. Plant a real secret in each position the new exclusion or new needle
   source touches, and require the scan to still find it: same tag name in a
   different group, same tag in the same group with a different value, and a
   different tag in the same group.
2. A "needle count did not buy coverage" test. Assert that any increase in
   `checked_values` on a geotagged JPEG is accompanied by at least one needle
   that is ACTUALLY PRESENT in a file carrying GPS. This is the direct guard
   against the option 2 failure mode: it fails a change that adds needles which
   can never match.

**For option 3, the byte-slice needle:**

3. A boring-coordinate fixture. A file geotagged at exactly `0.0, 0.0` and one
   at exactly `43.0, -79.0`, asserting the scan does not report RESIDUAL_FOUND
   on a correctly scrubbed output. Measured above: the 24-byte `0/1 0/1 0/1`
   slice occurs in a 3.4 MB DLL, so this is a real case, not a hypothetical.
4. A re-encode fixture. The same coordinate stored little-endian and
   big-endian, asserting the slice is lifted from the file under test and not
   reconstructed.

**For option 4, the structural assertion:**

5. The false-positive gate, and it is the important one: a selective run
   (`--remove-field Artist`) on a geotagged photo must still verify clean.
   Measured this session that the shipped tool does exactly this, so the test
   asserts a behaviour that exists today and would be broken by a careless
   implementation.
6. A no-grep test. A synthetic file containing the literal bytes `\xa9xyz` in a
   position that is NOT inside a `udta` box (for instance inside a `mdat`
   payload) must NOT be reported as carrying a location atom. Justified by the
   measured 35.3 `\xa9`-plus-three-letters sequences per MB in ordinary video.
   Same test in the other direction for the string `GPS`, measured 7 times in a
   102 MB file with no GPS metadata.
7. An unparseable-output test. Truncate the output and assert the verdict is
   "could not check", never clean. This mirrors `tests/test_unparseable.py` and
   is trap 12 in a new place.
8. A not-applicable test. A format with no registered GPS walker must report
   "not checked" and must not change the verdict, mirroring
   `StructureReport(applicable=False)`.

**For option 5, the reporting field:**

9. A serialisation test that `Verification.as_dict()` carries the new field, and
   a theme test in the shape of `tests/test_theme.py` asserting the CLI and GUI
   render the distinction as TEXT, per trap 6.
10. A gate test that a COMPLETE format whose GPS check did not run cannot report
    a bare `verified clean`. Otherwise option 5 is decoration.

---

## 7. Interaction with 1.0.2's `structure.py`

Yes, and it is a real constraint rather than a convenience.

- `structure.py` already proves things about an output with no baseline read,
  which is the property D2 needs. Its dispatch shape (`_WALKERS` keyed by
  extension, `applicable=False` for anything unregistered) is the right shape to
  copy, and copying it means adding GPS coverage cannot regress any format that
  does not have a walker.
- Its `StructureReport` already models the three-state answer correctly, error
  included. Reuse the pattern.
- **Do not put GPS into `unaccounted`.** The module's docstring is explicit that
  it does not flag known metadata chunks that survived, and `verify.py` maps a
  non-empty `unaccounted` to `STRUCTURE_UNACCOUNTED`, whose meaning is "we
  cannot say what these bytes are". A surviving GPS IFD is the opposite: we know
  exactly what it is. Folding them together would change the meaning of an
  existing verdict for existing files and would make the CHANGELOG entry for
  1.0.2 wrong retroactively.
- The two checks want different walkers anyway. `_walk_png` enumerates chunk
  types and cares about unknown ones; a GPS check needs to go INSIDE a known
  `eXIf` or `iTXt` chunk, which `_walk_png` deliberately does not do.
- `verify.verify()` currently runs the structure scan only after the residual
  scan finds no survivors. A GPS check placed in the same position inherits that
  ordering, which is correct: an actual residual value is still the more
  specific finding.

---

## 8. What I did NOT measure

Stated so nothing here is read as broader than it is.

1. **HEIC, HEIF and AVIF.** No fixture was built. Their GPS carriers are the
   ISO BMFF ones and I did not confirm that, nor that the exiftool engine
   removes GPS from them.
2. **Any raw format.** All 17 are on the exiftool engine and PARTIAL, and I
   built none of them. Whether maker-note GPS survives a PARTIAL scrub is
   unmeasured and is a plausible live leak rather than only a proof gap.
3. **Matroska and WebM GPS tags.** Not built, not read, not scrubbed.
4. **GIF, SVG and PDF XMP GPS.** I confirmed the PDF I built carried no GPS
   carrier; I did not try to write one into any of the three.
5. **A real phone photo.** Every fixture was synthetic and every GPS value was
   written by exiftool 13.29. A Samsung or Pixel JPEG may store GPS with
   different denominators, an additional `GPSProcessingMethod`, a maker-note
   copy, or an ISO6709 string at a different precision. The measured
   `+43.653226-079.383184+76.500/` is exiftool's formatting, not a phone's.
6. **A compressed XMP carrier.** The PNG `iTXt` produced here was uncompressed,
   so the XMP GPS string was findable in the bytes. A `zTXt` or a compressed
   `iTXt` would not be, per the second known limit in the 1.0.2 CHANGELOG. I
   did not build one.
7. **A natural false positive from the digits filter.** I measured the
   collision surface (338 digit/separator runs per MB) and I measured that the
   values the filter drops survived in 0 of 6 scrubbed outputs. I did not
   observe the filter actually preventing a false positive, and I did not
   construct one.
8. **The false-positive rate of a `(c)xyz` grep.** I measured 0 exact hits and
   3620 same-shape sequences in 102.45 MB. The 0.21-per-100-MB figure in
   section 3 is arithmetic on those two numbers, not an observed false positive.
9. **The full test suite.** I ran no `pytest` run this session, so I cannot say
   what the current pass and skip totals are, and none of the numbers above
   came from the suite. Two other teams are editing `tests/conftest.py` and
   `tests/test_motion_photo.py` concurrently.
10. **Any implementation.** No code was written, changed, staged or committed.
    The prototypes for options 3 and 4 live only in
    `<scratchpad>/d2/repro10.py` and are measurement instruments, not proposed
    code.

---

## 9. One paragraph, and the biggest risk

The premise is right that the residual scan cannot see GPS and wrong about why:
the digits filter drops none of the GPS tags on a real geotagged JPEG, and
removing it fixes detection on 0 of the 8 GPS-bearing files measured. The
coordinate is not in an EXIF file's bytes in any form exiftool prints, so no
needle derived from a printed value can ever work there, and the maximal naive
needle fix recovers half of one coordinate on one of eight files. The design
that works is the one Part 3.2 already argued for: assert on the OUTPUT's
structure that no GPS carrier remains, with real walkers rather than greps,
scoped so a selective run that deliberately keeps GPS still verifies clean, and
reported through an explicit field so a container with no walker says "not
checked" instead of riding a bare `verified clean`. **The single biggest risk is
that this ships covering JPEG and TIFF, which is the easy walker, and the
verdict string does not change, so `verified clean` on a `.heic` or a `.mkv`
means exactly what it means today while the CHANGELOG says GPS verification was
added.** That is the same over-claim D2 is, moved one level up and made harder
to find, and the only thing that prevents it is that the "not checked" state is
built in the same commit as the first walker and is impossible for a caller to
confuse with clean.
