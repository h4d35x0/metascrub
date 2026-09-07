# What the tool claims: five vocabularies, one verdict string

Status: OPEN. Design document, not an implementation. No code changed by this
document. It exists so one decision can be made: **what should `verified clean`
be allowed to mean.** That is a decision about the product's central promise,
so it is the owner's, not the author's.

Read in this order: the table in section 1 is the argument, section 2 is the
damage, sections 4 and 5 are the choice. Everything else is support.

## Provenance of every number below

Measured 2026-09-07 on the dev machine (Windows 11, Python 3.11.3), against:

| | |
|---|---|
| repo | `<repo>` |
| git HEAD | `85e20fa` (`feat: Android app shell, ...`), latest tag `v1.0.1` |
| `metascrub.__version__` | `1.0.2` |
| working tree | dirty in exactly one file: `metascrub/structure.py`, +362 lines, being edited in parallel by another team while this was written |
| exiftool | 13.29 (`C:\Tools\exiftool-13.29_64`, on PATH) |
| ffmpeg | 2024-12-11-git-a518b5540d |

**The dirty file matters and is called out rather than smoothed over.** At
06:01 EDT `structure.supported_extensions()` returned 3 entries
(`.gif .png .webp`). Minutes later, mid-session, it returned 13
(`.3gp .avif .gif .heic .heif .jpe .jpeg .jpg .m4v .mov .mp4 .png .webp`). Both
states were measured on real files. **The table in section 1 is measured against
the working tree as it stands now, with 13 walkers**, because that is what is
about to be true. Where the shipped state differs, the text says so.

Device media: `<device-corpus>\`, read in
place, never copied into this repo. Scrub outputs were written to a scratchpad
outside the repo. **No coordinate from that corpus appears anywhere in this
document.** Where a value had to be characterised, it is characterised by its
tag name, its length, or its group, never by its content.

Two rows are synthetic and are labelled SYNTHETIC. They were built here because
the device corpus contains no file in an extension that `gps_verify` has no
walker for, and that gap is precisely what needed measuring. Their coordinate
is a made-up number that was never on a device.

---

## 1. The measured table

One file, one moment, all five vocabularies. The `verify` verdict and the second
`gps_verify` / `structure` readings are taken after the scrub; the first
`gps_verify` reading and the `exif_io` outcome are taken before it, to establish
that there was something to remove and how well the baseline was understood.

| file | ext | (3) capabilities | (5) exif_io before | (2) gps before | (2) gps after | (4) structure after | (1) verify verdict | checked_values | what the user is shown |
|---|---|---|---|---|---|---|---|---|---|
| `Issue 263 dotnet.heic` (iPhone XR) | .heic | COMPLETE | PARSED, 133 real tags, 14 GPS | CARRIER_FOUND (GPS IFD, 14 entries, in EXIF item 50) | CLEAN | applicable=True, 0 unaccounted | **RESIDUAL_FOUND** (2) | 8 | `ERROR: verification failed` |
| `IMG_1034.heic` (iPhone 8) | .heic | COMPLETE | PARSED, 123 tags, 15 GPS | CARRIER_FOUND (15 entries) | CLEAN | applicable=True, 0 | **RESIDUAL_FOUND** (2) | 5 | `ERROR` |
| `exif-at-eof.heic` (iPhone 11 Pro) | .heic | COMPLETE | PARSED, 128 tags, 4 GPS | CARRIER_FOUND (4 entries) | CLEAN | applicable=True, 0 | **RESIDUAL_FOUND** (2) | 8 | `ERROR` |
| `Issue 487.heic` (Galaxy S10+) | .heic | COMPLETE | PARSED, 67 tags, 4 GPS | CARRIER_FOUND (4 entries) | CLEAN | applicable=True, 0 | **VERIFIED_CLEAN** | **3** | `SANITIZED`, `verified clean 1` |
| `Google Pixel 2.jpg` | .jpg | COMPLETE | **UNPARSED** (`[minor] Unrecognized MakerNotes`), 95 tags, 11 GPS | CARRIER_FOUND (11 entries) | CLEAN | applicable=True, 0 | **VERIFIED_CLEAN** | **5** | `SANITIZED`, `verified clean 1` |
| `test.MP.jpg` (Pixel 3a motion photo, 2.7 MB MP4 trailer) | .jpg | COMPLETE | PARSED, 112 tags, 10 GPS | CARRIER_FOUND (10 entries) | CLEAN | applicable=True, 0 | **VERIFIED_CLEAN** | 15 | `SANITIZED`, `verified clean 1` |
| `MVIMG_20180910_124410.jpg` (Nokia 7 plus) | .jpg | COMPLETE | **UNPARSED** (`Unrecognized MakerNotes`), 55 tags, 9 GPS | CARRIER_FOUND (9 entries) | CLEAN | applicable=True, 0 | **VERIFIED_CLEAN** | **4** | `SANITIZED`, `verified clean 1` |
| `Samsung SM-G950F (Galaxy S8).jpg` (18.4 MB SEF trailer) | .jpg | COMPLETE | PARSED, 42 tags, 0 GPS | **INCOMPLETE** (`18469861 bytes follow the JPEG EOI and were not interrogated`) | CLEAN | applicable=True, 0 | **VERIFIED_CLEAN** | **5** | `SANITIZED`, `verified clean 1` |
| `Nokia 6.1.mp4` (Android, real `(c)xyz`) | .mp4 | COMPLETE | PARSED, 58 tags, 1 GPS | CARRIER_FOUND (ISO 6709 atom in `/moov/udta`) | CLEAN | applicable=True, 0 | **VERIFIED_CLEAN** | **0** | `SANITIZED`, `verified clean 1` |
| `with-gps.mov` (iPhone 6, QuickTime) | .mov | COMPLETE | **UNPARSED** (`[minor] The ExtractEmbedded option may find more tags`), 71 tags, 1 GPS | CARRIER_FOUND (`com.apple.quicktime.location.ISO6709`) | CLEAN | applicable=True, 0 | **VERIFIED_CLEAN** | 2 | `SANITIZED`, `verified clean 1` |
| SYNTHETIC `geo.dng` (TIFF, GPS + Artist sentinel) | .dng | **PARTIAL** | PARSED, 20 tags, 7 GPS | **NOT_CHECKED** (`no GPS walker for .dng`) | **NOT_CHECKED** | **applicable=False** | **VERIFIED_CLEAN** | **3** | `SANITIZED`, `PARTIAL: raw container...`, `verified clean 1` |
| SYNTHETIC `gpsonly.dng` (GPS and nothing else) | .dng | **PARTIAL** | PARSED, 15 tags, 5 GPS | **NOT_CHECKED** | **NOT_CHECKED** | **applicable=False** | **VERIFIED_CLEAN** | **0** | `SANITIZED`, `PARTIAL:`, `verified clean 1` |
| SYNTHETIC `gpsonly.jpg` (GPS and nothing else) | .jpg | COMPLETE | PARSED, 13 tags, 5 GPS | CARRIER_FOUND (5 entries) | CLEAN | applicable=True, 0 | **VERIFIED_CLEAN** | **0** | `SANITIZED`, `verified clean 1` |

Column numbers refer to the five vocabularies as the brief numbered them:
(1) `verify.Verdict`, (2) `gps_verify.GpsStatus`, (3) `capabilities.Completeness`,
(4) `structure.StructureReport.applicable`, (5) `exif_io.ReadOutcome`.

### The three numbers the table is really about

**50 of 71.** Of the 71 extensions in `CAPABILITIES`, 50 have neither a
`gps_verify` walker nor a `structure` walker, measured against the working tree
as it stands, with the in-flight walkers already counted in its favour. For
those files both checks return "we did not look", and neither of those two
answers reaches the user in any form.

**29 of those 50 are declared COMPLETE.** `.pdf .docx .xlsx .pptx .odt .ods
.odp .mkv .webm .avi .ts .m2ts .mp3 .wav .flac .ogg .opus .aif .aiff .jp2 .psd`
plus the OOXML and ODF template extensions. On those, nothing in the output
hedges at all: no PARTIAL line, no note, just `verified clean`.

**20 of 71 have a GPS walker; 12 of the 71 have a structure walker** (13 are
registered, `.3gp` having no capability row). Before the in-flight change
landed, the structure figure was 3.

### The other measured facts the table compresses

- **`checked_values` reached 0 on a real device file.** `Nokia 6.1.mp4` is a
  genuine geotagged Android video. Its verdict is `VERIFIED_CLEAN` and the
  residual scan searched the output for **zero** strings. It reached 0 again on
  two synthetic GPS-only fixtures. `verify.py` treats an empty needle set as a
  pass by construction: `residual_scan()` returns `[]` immediately for an empty
  `needles`, and no branch downstream distinguishes "searched and found
  nothing" from "had nothing to search for".
- **The residual scan can never search for a coordinate, and that is now
  measured twice.** `docs/D2-GPS-VERIFICATION.md` section 1 established it on a
  built JPEG. It reproduced here on real device output: `Issue 263 dotnet.heic`
  has a 133-tag baseline and 14 GPS tags, and the 8 needles it produced come
  from `ICC_Profile:RedTRC`, `MakerNotes:ContentIdentifier`,
  `ICC_Profile:ProfileCopyright`, `XMP:ArtworkContentDescription`,
  `ICC_Profile:ProfileDescription`, `XMP:XMPToolkit`, `EXIF:Model` and
  `EXIF:LensModel`. Not one GPS tag among them.
- **The two HEIC residual failures are ICC profile strings, not private data.**
  On all three Apple HEICs the surviving needles came from
  `ICC_Profile:ProfileCopyright` (26 characters) and
  `ICC_Profile:ProfileDescription` (10 characters). The tool downgrades those
  files to `ERROR: verification failed`. That is a separate defect from the one
  this document is about; it is recorded because it is what the measurement
  returned, and it is not addressed below.
- **Three of the ten real device files had an UNPARSED baseline and still got a
  full-confidence verdict.** `ReadOutcome.UNPARSED` is consulted by
  `scrubber.py` only when `before_tags` is empty. When exiftool surfaces tags
  AND says it could not parse part of the file, the warning is discarded and
  never reaches the result dict, the report, or the screen.
- **The motion photo and SEF trailers were genuinely removed.** Ground truth by
  byte search of the outputs, not by asking an engine: after scrubbing
  `test.MP.jpg` and `MVIMG_20180910_124410.jpg`, the literals `ftyp`, `moov`,
  `GCamera`, `MotionPhoto`, `SEFH` and `SEFT` are each absent (`find` returned
  -1 for all six in both files), and both files end in `FF D9`.
  `Samsung SM-G950F.jpg` went from 22,965,268 to 4,494,711 bytes, against a
  first-EOI offset the manifest measured at 4,495,405. **No false clean was
  found on any real device file in this corpus. The problem measured here is
  what the tool CLAIMS, not what it removed.**

---

## 2. The misleading cases, by measurement

A misleading case here means a combination the tool actually produced, in which
a reasonable user would conclude something the tool did not establish.

### Case A. VERIFIED_CLEAN while two other checks say they did not look

Reproduced on `geo.dng` and `gpsonly.dng`. At one moment, on one file:

```
verify.Verdict                       VERIFIED_CLEAN
verify checked_values                3   (0 on the GPS-only variant)
gps_verify.GpsStatus                 NOT_CHECKED  ("no GPS walker for .dng")
structure.StructureReport.applicable False        ("we did not look")
capabilities.Completeness            PARTIAL
exif_io.ReadOutcome                  PARSED
```

and the console said, verbatim:

```
  SANITIZED  geo.dng  [exiftool]
      PARTIAL: raw container; maker notes may retain private records

Summary
  files processed   1
  sanitized         1
  already clean     0
  verified clean    1
```

Four of the five vocabularies are hedging. Exactly one of the four (`PARTIAL`)
reaches the user. The other three do not, so the user sees a hedge about maker
notes and a flat `verified clean`, and has no way to learn that the GPS question
was never asked and that no structural walk was performed.

The brief anticipated this case on a `.heic`. **Measured, that specific instance
is wrong in the tool's favour and should not be used as the argument:**
`gps_verify` already covers `.heic` (it is in `_ISOBMFF_EXTENSIONS`, and it
returned CARRIER_FOUND then CLEAN on all four HEICs above), and the in-flight
`structure.py` now covers it too. The extensions where Case A is real are the 50
listed in section 1, and the sharpest of them are the 17 raw formats: TIFF
based, routinely carrying a GPS IFD, declared PARTIAL, and with no walker of
either kind.

### Case B. VERIFIED_CLEAN on a residual scan that searched for nothing

Reproduced on `Nokia 6.1.mp4`, a real device file: `checked_values=0`,
`residual_values=[]`, verdict `VERIFIED_CLEAN`, status `sanitized`. Reproduced
again on two synthetic GPS-only files, one `.dng` and one `.jpg`.

This is the most direct contradiction of the project's founding sentence.
`verify.py` opens with "Success is measured, never inferred". Here the
measurement is over an empty set. The verdict is not wrong about anything it
checked; it checked nothing, and it prints the same word it prints after
checking fifteen values.

`checked_values` IS in the JSON report, so a script can recover the fact. It is
not printed by the CLI and not shown in the GUI, and there is no threshold
anywhere in the code: `checked_values=0` and `checked_values=15` produce
identical human output.

### Case C. An UNPARSED baseline producing a full-confidence verdict

Reproduced on `Google Pixel 2.jpg`, `MVIMG_20180910_124410.jpg` and
`with-gps.mov`. exiftool said `[minor] Unrecognized MakerNotes` on the two
JPEGs and `[minor] The ExtractEmbedded option may find more tags in the media
data` on the `.mov`. All three classify as `ReadOutcome.UNPARSED`. All three
reported `VERIFIED_CLEAN`.

The refusal in `scrubber.py` fires only on `not before_tags and
before.unparsed`, which is correct for the defect it was built for (trap 12) and
leaves this one open: a partially understood baseline yields a partial needle
set, which produces a verdict indistinguishable from one built on a complete
needle set. `Unrecognized MakerNotes` is exiftool stating, in as many words,
that there is a region of this file it did not interpret. That statement is
dropped before it reaches any output.

Note the asymmetry with trap 12's allowlist. `_BENIGN_WARNINGS` exists so a
benign warning cannot cause a refusal. There is no corresponding mechanism to
let a non-refusing warning still narrow the claim. The only two settings are
refuse and forget.

### Case D. `unaccounted_regions: []` means two different things in the JSON

Measured, at the same moment, in the same schema:

```
geo.dng      verification.unaccounted_regions = []   structure.applicable = False
gpsonly.jpg  verification.unaccounted_regions = []   structure.applicable = True
```

`structure.py`'s own docstring says: "We did not look" and "we looked and found
nothing" must never share a representation. `StructureReport` honours that with
its `applicable` flag. `Verification.as_dict()` then discards `applicable` and
emits only the list, so the two states share one representation **in the
interface people parse**. That is trap 2, live, inside the JSON report, today.

### Case E. gps_verify's own INCOMPLETE is computed and thrown away

On `Samsung SM-G950F (Galaxy S8).jpg` before scrubbing, `gps_verify` returned:

```
GPS carriers: PARTLY CHECKED [exif-gps-ifd, xmp-gps]; none found, but
18469861 bytes follow the JPEG EOI and were not interrogated for GPS carriers
```

That is exactly the sentence a user needs about that file. `verify.py` does not
import `gps_verify`, so it appeared nowhere. The module computes a correct,
specific, actionable limit and there is no wire for it to travel down.

### Case F. Six words for one idea, none of them shared

Not one file's verdict, but the reason the other five recur. "We did not look"
is currently spelled: `Verdict.UNVERIFIED`, `GpsStatus.NOT_CHECKED`,
`StructureReport.applicable=False`, `ReadOutcome.UNPARSED`,
`Completeness.PARTIAL`, and, proposed but unbuilt, `Assurance.REFUSED` in
`docs/ANDROID-MEDIA-BUILD.md` Part 4. Each is well designed in isolation, and
each was added because the previous one did not fit. Nothing reconciles them, so
a new check can be built correctly and still change nothing about what the tool
claims.

That is precisely the risk `docs/D2-GPS-VERIFICATION.md` section 9 names, and
`gps_verify` is now sitting in the tree as proof it was right: a complete,
careful, five-state, 1,204-line module covering 20 extensions, wired to nothing,
with a test that asserts it stays that way.

---

## 3. The JSON report is an interface

`--report FILE` is documented in `README.md` line 255. It is a public surface,
and any change to its shape is a **breaking change** and must be labelled that
in the CHANGELOG regardless of how small it looks.

The current shape, measured by running `--report` and dumping the keys:

Top level: `sanitize_date`, `total_files`, `sanitized_files`, `clean_files`,
`unsupported_files`, `deferred_files`, `error_files`, `verified_clean`,
`files_with_residual_metadata`, `filenames_with_leaks`,
`filenames_still_leaking`, `renamed_files`, `rename_failures`,
`total_fields_removed`, `total_fields_sanitized`, `sanitize_time`, `results`.

Per result: `file`, `status`, `sanitize_time`, `removed_fields`,
`sanitized_fields`, `engine`, `completeness`, `rewrites_container`,
`engine_note`, `tags_before`, `backup`, `verification`, plus conditionally
`error`, `name_leaks`, `renamed_to`, `rename_error`.

Per verification: `verdict`, `clean`, `residual_values`, `remaining_tags`,
`checked_values`, `detail`, `unaccounted_regions`.

Three grades of change, and they are not equal:

1. **Additive.** A new key inside `verification` or inside a result. A parser
   reading `verdict` or `clean` keeps working. The risk is not breakage, it is
   that the new key is ignored, which is the silent failure mode named against
   every option below.
2. **Semantic.** Keeping `verdict: "verified_clean"` while changing what it
   asserts. **Nothing breaks and nothing warns.** This is the most dangerous
   grade and the one a CHANGELOG is least likely to mark, because the diff
   looks like documentation.
3. **Structural.** New verdict values, a renamed field, a changed type. Every
   parser with an `if verdict == "verified_clean"` branch silently falls into
   its else. Breaking, and must be released as such.

There is a fourth consumer that is not the JSON: `gui.py::_verdict_text()` maps
four verdict strings by literal comparison and returns `"not verified"` for
anything unrecognised. A new verdict value therefore fails safe there. That is
the right direction and is worth preserving in whatever is chosen.

---

## 4. Options

### Option A. Leave the enums alone; document the limits

Add a section to `README.md` stating what `verified clean` covers per format,
and print `gps_verify.coverage()` from `python -m metascrub doctor` and
`formats`.

- **Changes:** `README.md`, `cli.py` (two new output blocks). No enum, no
  schema, no verdict.
- **JSON:** unchanged. Not a breaking change.
- **Cost:** near zero. `coverage()` already exists and already computes the
  covered / uncovered split at runtime from `CAPABILITIES` and the walker
  table, so it cannot go stale.
- **What breaks:** nothing.
- **How it silently fails:** completely, for the case that matters. The person
  at risk is the one who scrubs a photo before posting it, sees `verified
  clean`, and does not read the README. Documentation is not a control. It also
  leaves Case D, the ambiguous `unaccounted_regions: []`, live in the
  interface, which no amount of README fixes.
- **The honest case for it:** `README.md` line 104 already says "The runtime
  verdict is bounded by what exiftool can read, and `verified_clean` should be
  read as exactly that claim and no wider." If the answer to this whole document
  is "the claim is already correctly scoped and the scoping is written down",
  then Option A is finishing a job that is mostly done.

### Option B. One coverage statement alongside the verdict

Keep `Verdict` exactly as it is. Add one field, `coverage`, to `Verification`,
carrying which checks ran, which did not, and a one-line human string. Wire
`gps_verify` and `structure.applicable` into it. Print the line in the CLI
whenever a check did not run; append it to the GUI detail column.

- **Changes:** `verify.py` (populate it, and import `gps_verify`), `cli.py` (one
  print), `gui.py` (append to the detail cell), the JSON schema (one additive
  key), `README.md`. `scrubber.py` needs nothing: it already passes
  `verification.as_dict()` straight through.
- **JSON:** additive, grade 1. Existing parsers keep working. Still a minor
  version and a CHANGELOG entry.
- **Android app:** reads the same result dict. It needs a UI change to surface
  the line, and nothing breaks if it does not get one.
- **Cost:** moderate. The precedent already works: engines emit `NOTE:` strings
  that ride inside `removed_fields` and `cli.py` lines 132-134 print them. This is
  that pattern given a field of its own instead of a prefix convention.
- **What breaks:** `tests/test_gps_verify.py::test_verify_does_not_import_this_module_yet`
  fails by design and must be deleted in the same commit that wires it.
- **How it silently fails:** the verdict string does not change, so anyone
  reading `clean == true` and nothing else is exactly as misled as today. Grade
  1 change, grade 1 silent failure. Reducing that means making the coverage line
  impossible not to read, which the CLI and GUI can do and a JSON key cannot.

### Option C. Unify into one enum

Collapse `Verdict`, `GpsStatus` and `StructureReport.applicable` into a single
assurance enum, probably along the lines of the unbuilt
`Assurance.PROVEN / MITIGATED / REFUSED` in `docs/ANDROID-MEDIA-BUILD.md`
Part 4.

- **Changes:** `verify.py`, `gps_verify.py`, `structure.py`, `scrubber.py` (the
  `Completeness.COMPLETE and not verification.clean` downgrade), `cli.py`,
  `gui.py`, the JSON schema, `selftest.py`, the Android app, and a large
  fraction of the test suite.
- **JSON:** grade 3, breaking. Every consumer's `verdict` comparison changes.
- **Cost:** high, and the risk is not the size. Collapsing states is the
  operation this project's traps exist to forbid. `RESIDUAL_FOUND` (a value we
  captured survived) and `STRUCTURE_UNACCOUNTED` (we cannot say what these bytes
  are) are separate on purpose, and `structure.py`'s docstring says folding them
  would change what `STRUCTURE_UNACCOUNTED` means for every file that already
  reports it and make the 1.0.2 CHANGELOG retroactively wrong. A unification
  does that deliberately, five times over.
- **How it silently fails:** during the migration. Each collapse is individually
  defensible, and the aggregate loses distinctions nobody notices are gone,
  because the tests asserting them were rewritten in the same commit that
  removed them. `Assurance` is also scoped to a Phase 5 feature that is not
  being built, so adopting it now means adopting a vocabulary designed for a
  different problem.

### Option D. Make the verdict carry its own coverage

`VERIFIED_CLEAN` becomes reachable only when every registered check for that
format actually ran and passed. A format with no GPS walker, no structure
walker, an UNPARSED baseline, or `checked_values == 0` cannot reach it and lands
in a new, distinct verdict (`PARTIALLY_VERIFIED`, say) rendered in its own
words.

- **Changes:** `verify.py` (the decision), `cli.py`, `gui.py` (a fifth branch in
  `_verdict_text`, which currently falls back to `"not verified"` and would need
  the new word), `scrubber.py` (its COMPLETE downgrade reads
  `not verification.clean` and would start firing on files that are fine), the
  JSON schema, `README.md`, `CHANGELOG.md`, the Android app, and every test that
  asserts `VERIFIED_CLEAN`.
- **JSON:** grade 3, breaking, and loudly so. That is its main virtue: a parser
  with `if verdict == "verified_clean"` stops saying clean about the 50
  extensions immediately, rather than continuing to say it quietly.
- **Cost:** high, and mostly paid at once. Measured against the table: the rows
  that would move are `Google Pixel 2.jpg`, `MVIMG_20180910_124410.jpg` and
  `with-gps.mov` (UNPARSED baseline), `Nokia 6.1.mp4` and `gpsonly.jpg`
  (checked_values 0), and both `.dng` rows (no walker of either kind). That is 7
  of the 13 measured files, and every one of them was genuinely cleaned.
- **How it silently fails:** by being too loud, and then being tuned. Once
  `verified clean` stops appearing on ordinary correct output, the pressure is
  to relax the condition, and each relaxation is a small grade 2 semantic change
  that nothing detects. The failure is not in the commit that ships it; it is in
  the three that follow.
- **Also worth pricing:** this is the only option that makes
  `checked_values == 0` impossible to ship as clean, which is the single finding
  here that most directly contradicts the project's founding sentence.

---

## 5. Recommendation

**Option B, plus exactly one piece of Option D: `checked_values == 0` must not
be `VERIFIED_CLEAN`.**

The reasoning in one paragraph. Every real device file in this corpus was
actually cleaned; the removals are sound and the engines are not the problem, so
a breaking, tool-wide re-grading (Options C and D in full) buys correctness the
tool already has, and spends the one budget that is genuinely scarce here, which
is how many distinctions this codebase can hold before one of them is quietly
dropped. What is missing is not a better verdict, it is a wire: `gps_verify`
computed a correct and specific sentence about the Samsung file's 18.4 MB
trailer and no channel existed to carry it. Option B builds the channel, is
additive to the JSON, reuses the `NOTE:` precedent that already works, and lets
`gps_verify` be worth its 1,204 lines. The one carve-out from Option D is there
because Option B alone leaves `Nokia 6.1.mp4` saying `verified clean` on a
search of zero strings, and a document about what the tool claims cannot
recommend leaving that standing: an empty needle set is the one case where
"measured" and "inferred" are provably the same code path, which is the sentence
`verify.py` opens by rejecting.

**What it costs, plainly.** One new key in the JSON report plus a CHANGELOG
entry labelling it additive. A new verdict or status value for the zero-needle
case, which IS a breaking change for anyone matching on `verdict` and must be
released as one. The deletion of `test_verify_does_not_import_this_module_yet`.
CLI and GUI changes in two places each. The Android app needs one more rendered
line to benefit and breaks nothing if it does not get it. Roughly a day, plus
the test-suite churn for whichever verdict the zero-needle case lands in. Not
free, and much cheaper than C or D.

**Two things this document deliberately does not decide for you:**

1. Whether the zero-needle case becomes a new verdict value (breaking, visible)
   or a downgrade to the existing `UNVERIFIED` (also breaking for anyone who
   treats `UNVERIFIED` as an error, and it would turn 2 of the 13 measured files
   into an error status). `UNVERIFIED`'s docstring already says it is "never
   treated as success", which fits, but it currently means "verification could
   not run" and this is "verification ran over an empty set". Those may be the
   same thing, or they may be trap 2 again in a new place.
2. Whether `Verification.as_dict()` should also emit `structure_applicable`,
   which fixes Case D. It is one more additive key and it closes a live trap 2
   violation in the interface. I would do it in the same commit.

---

## 6. What I did not measure

Stated so nothing above is read as wider than it is.

1. **I did not find a false CLEAN on any real device file.** Every removal I
   checked worked. The `.dng` rows are synthetic TIFFs, and on them exiftool did
   remove the GPS: measured by byte search for the sentinel strings, and by
   exiftool reporting zero GPS tags afterwards. **I have not measured a real raw
   file where a maker-note GPS record survives**, which is the leak
   `Completeness.PARTIAL` warns about. No `.dng`, `.nef`, `.cr2` or `.arw` from
   a real camera was found on this machine. The Case A argument therefore rests
   on what the tool CLAIMS about raws, not on a demonstrated leak in one.
2. **I did not scrub `apple-livephoto-quicktime.mov`, `Issue 649.avif`, the
   three WebPs, or `with-gps.mp4`.** The manifest suggests at least two more
   interesting rows: the `.mov` with no `ftyp` box at all, and
   `Issue 473 (Java).webp` with a nonstandard `XMP\0` fourcc that the VP8X flag
   byte does not declare.
3. **I did not run the test suite.** `metascrub/structure.py` and
   `tests/test_structure.py` are being edited in parallel; a pass or fail count
   taken now would measure the other team's work in progress, not this tree.
4. **I did not measure the GUI.** Every claim about `gui.py` above is read from
   the source of `_verdict_text()` and the column definitions, not from a
   running window. No window was opened.
5. **I did not measure the Android app's behaviour.** The claim that it surfaces
   `applicable=false` comes from the brief, not from running it. What I did
   measure is that `android/app/build/` contains a generated copy of
   `gps_verify.py` carrying the same "NOT WIRED IN, ON PURPOSE" docstring, so
   the app ships the module in the same unwired state.
6. **I did not scrub any document format.** No PDF, OOXML, ODF, OLE2 or SVG file
   was run for this document. The "29 COMPLETE and blind" count is computed from
   `CAPABILITIES` and the two walker registries, which is a real measurement of
   the registries and not of those formats' behaviour.
7. **I did not verify that the in-flight `structure.py` is correct**, only that
   it changed `supported_extensions()` from 3 entries to 13 and that
   `applicable` became True for `.jpg` and `.heic` on real files. Whether its
   new walkers walk correctly is the other team's measurement to report.
8. **I did not re-run the HEIC ICC failure on a second exiftool version.** Three
   of three Apple HEICs failed identically on exiftool 13.29. Whether 13.59
   behaves the same is unmeasured.
9. **I did not measure whether any of these files leaks after the scrub through
   a carrier no tool here reads.** Nothing in this document is evidence about
   carriers outside exiftool, `gps_verify`'s three carrier classes, and the
   structure walkers.

---

## Appendix: the ten-second version

A `.dng` that has just been scrubbed produces, at one moment:
`Completeness.PARTIAL`, `ReadOutcome.PARSED`, `GpsStatus.NOT_CHECKED`,
`StructureReport.applicable=False`, and `Verdict.VERIFIED_CLEAN` with
`checked_values=0`.

Four of the five say some version of "we could not, or did not, look". The one
the user reads says `verified clean`.
