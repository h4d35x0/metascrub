# Lessons

Corrections and the patterns that prevent repeating them. Newest first.

---

## 2026-09-05 - A test written to justify a change found the leak the change would have hidden

Three platforms disagreed about a zip format, and the proposed explanation was
that the ZIP tag group is container structure and should be ignored. Before
making that change, a test was written for the fact the change depended on:
that every zip entry timestamp is normalised, so the one ZIP field capable of
carrying information is a constant afterwards.

The test failed immediately, on the current shipped code.

`ooxml_engine` normalised the timestamp of every member it COPIED and none of
the members it REPLACED. Writing an entry by name stamps the current clock, so
every scrubbed document recorded the minute it was sanitized. Reusing the source
`ZipInfo` preserves the original, so two members kept the editing session's
timestamp. The engine's own comment said these leak wall-clock time and must be
normalised; two code paths went around it.

So the justification for the change was false, and the change would have stopped
anything from noticing.

**Pattern:** when an exclusion rests on a claim about the code, test the claim
before writing the exclusion, not after. The test is worth writing even if the
change is later abandoned, because it is the claim that matters, not the change.
This one is still here and the change is not.

---

## 2026-09-05 - Three wrong causes in one afternoon, every one a probe nobody checked

A cross-platform failure was diagnosed three times before the diagnosis was
right, and all three wrong answers came from the same place.

1. **"It is the older exiftool."** Plausible: one platform shipped 12.76, the
   others 13.x. Pinning 13.29 changed the failure count from 106 to 106. A
   number that does not move when the suspected variable moves is the variable
   being eliminated, and it should have been the end of that theory rather than
   a detail to work around.
2. **"It is the ZIP tag group."** The exclusion was written, and an existing
   test caught that it introduced a FALSE CLEAN on packages missing a mimetype
   member: with the group ignored, a file the reader could only see as a generic
   archive looked empty and was returned untouched.
3. **The actual cause**, one line inside a dump in a log: the reader could not
   load the Perl module it needs to look inside a zip. It was reporting only
   container tags and saying so in a warning, and the tool was correctly
   refusing to claim it had verified anything.

Later the same day, a release was reported to the user as still broken. It was
not. The check installed the previous version, because the index had not
propagated, and then two separate version readings both said the new one because
the shell's working directory was the project root, so `sys.path[0]` is `''` and
Python read the source tree and its egg-info instead of the installed package.
Two independent contaminations agreeing, which is what made it convincing. That
exact trap is written four entries below this one.

**Pattern:** a cause you reasoned to is a hypothesis, and the tell that you are
treating one as measured is that you start designing the fix. Before that, ask
what observation would rule it OUT, and go get that observation. And a probe
that has not been validated is not evidence: check what the harness actually
did, not what it was meant to do.

---

## 2026-09-05 - The claims a project makes about its environment are also untested code

Two promises in this repository were wrong, and neither could fail a test
because neither was ever executed.

`requires-python` said 3.9. Two dependencies require 3.10, so a 3.9 install
could only fail to resolve or silently pin versions nothing here has been tested
against. Nobody had run it, because the machine it was written on had 3.11.

The launcher promised to work on POSIX systems, and had only ever run under Git
Bash on Windows. The Windows launcher, meanwhile, preferred an interpreter
selector that deliberately ignores virtual environments and picks the newest
Python on the machine. In an environment with a venv active it ran a different
interpreter, found none of the dependencies, and reported them all missing,
which reads as a broken install rather than the wrong interpreter.

All three were found within two runs of the first cross-platform CI, and none
was reachable from a single developer machine.

**Pattern:** a version floor, a supported platform and a launcher are claims of
the same kind as "this format is handled". This project already refuses to list
a format it has not measured. The same standard applies to the environment, and
the only way to measure it is to run somewhere that is not your own machine.

---

## 2026-09-04 - A round trip that passes can still hand back a different file

The AV work was specified as an extension-to-muxer map: `-f mov` for `.qt` and
`.mqv`, `-f mp4` for `.lrv` and `.f4a`. Built exactly that, and all four
extensions passed everything the project asks for. The sentinel left the bytes,
ffprobe parsed the output, the verifier agreed.

Then the cross product. All four of these extensions name ISO base media files
in two incompatible flavours, QuickTime (`ftyp` brand `qt  `) and ISO/MP4, and
the extension does not decide which one a given file is. Feeding the engine
both flavours of each extension, eight cases, **four came back in the other
flavour**. A `.lrv` that was QuickTime going in was ISO/MP4 coming out. Every
other assertion still passed, because a converted file is still clean, still
parses, and still has the right name.

The fixtures could not see it. They were built by the same map the engine used,
so the engine was only ever handed the flavour it already expected. A test
built from the implementation's own assumption confirms the assumption.

The fix was to read the flavour out of the input's `ftyp` brand and match it,
leaving the extension map as the fallback for a file with no `ftyp` box. All
eight then round trip as themselves.

**Pattern:** when a function's behaviour is keyed on a NAME, ask what happens
when the name and the content disagree, and build the disagreeing case. And
when a fixture and the code under test both consult the same table, the test
cannot fail for the reason the table is wrong. Give the test its own statement
of the truth, then assert the two agree, rather than deriving one from the
other.

Second, smaller one from the same change: the flag had to be added to two
command lines, `strip_all` and `_retry_without_bitexact`. The retry path only
runs when a muxer rejects `-bitexact`, so a miss there would surface as a
corrupt-looking failure on a rare subset of files. Capturing argv and asserting
on both was cheap; noticing that path months later would not have been.

---

## 2026-09-04 - `pathlib.write_text` rewrote three files to CRLF, invisibly

Editing `capabilities.py`, `engines/__init__.py` and `odf_engine.py` through a
short `Path.write_text()` script converted every `\n` in those files to `\r\n`,
because Python's text mode does newline translation on Windows. Nothing in the
diff showed it. `git add` printed *"CRLF will be replaced by LF the next time
Git touches it"* three times, which is the only reason it was caught, and only
because that warning was read rather than skimmed past as noise.

This repo pins `* text=auto eol=lf` in `.gitattributes` precisely because the
fleet has already lost 552 files in the the parent project repo to the same class of
problem. The commit was correct in the index; the working tree was not.

**Pattern:** this is the `"\bin"` lesson below wearing different clothes. Any
file this project writes through Python gets `newline=""` or `write_bytes`, and
gets a byte scan afterwards. And a `git add` warning is output, not decoration:
read it.

---

## 2026-09-04 - A plan's "measured contents, in full" is still one machine's output

The engine plan gave the contents of `manifest.rdf`
"in full" for the `.odt` and `.ods` fixtures, and says it is the default
skeleton carrying nothing identifying. Measured here on 2026-09-04: true for
`.odt`, false for `.ods`. The LibreOffice on this machine writes a 899-byte
`manifest.rdf` for a spreadsheet, with `ContentFile`, `StylesFile` and two
`pkg#hasPart` triples in it.

Nothing was harmed, because the difference was noticed before the engine was
written. It changed the design though: the plan's "replace it and emit a NOTE
when the original differed" would have fired a scary warning about custom RDF on
every single `.ods`, which is a false alarm on structural triples. The engine
classifies instead, and only warns when something outside the structural set is
present.

**Pattern:** a plan measured on one machine, one LibreOffice build and one
document is a strong hypothesis, not a fact about the format. Re-measure the
specific claims an implementation is about to depend on, especially the ones
phrased as "in full" or "always". The cost was ten minutes; the cost of trusting
it would have been a warning users learn to ignore.

---

## 2026-09-04 - A plan that measured the problem can still propose too wide a fix

The engine plan measured a real defect precisely: an SVG
with every trace of metadata removed verified as `residual_found` on the value
of `SVG:Xmlns`, which is the namespace declaration without which the file is not
an SVG. The measurement was right and the diagnosis was right.

The proposed fix was to add `"xmlns"` and `"preserveaspectratio"` to
`_STRUCTURAL_TAGS`. That set is matched at `verify.py` on the BARE TAG NAME,
globally, for every format. `xmlns` is not a name only SVG can carry, so the fix
would have stopped the residual scan searching for any value stored under a tag
of that name in any file type, forever. The plan's own risk section called the
blast radius "nil" on the grounds that no other handled format reports those
tags today, which is true and is not the same claim.

The narrower fix was available and cost about the same: `_STRUCTURAL_VALUES`,
keyed on (exiftool group, tag name) with a predicate over the VALUE. It
suppresses only the exact structural string, in the one group that reports it.

**Pattern:** a plan's measurement and a plan's remedy are two separate claims
and they need separate scrutiny. The measurement is the expensive part and it
earns trust; the remedy is a design decision that arrives wearing the
measurement's credibility. Before implementing an exclusion a plan proposes, ask
what ELSE it excludes, on inputs the plan never measured. The tell here was that
the fix was expressed in a wider vocabulary than the problem: the problem was
about one group and one exact value, the fix was about a name.

Also, the second half of the older exclusion lesson turned out to be the part
that did the work. Writing the overcorrection test first is what forced the key
to be (group, tag, value) rather than tag: there is no way to write "a secret
smuggled into the xmlns value is still found" against a tag-name exclusion,
because the test cannot pass. A test you cannot write is a design telling you
something.

---

## 2026-09-04 - The deferral mechanism was built, and then not used

`.qt`, `.mqv`, `.lrv` and `.f4a` were described as deferred in the docs,
in `README.md`, and in a comment in `capabilities.py`. `DEFERRED` in
`capabilities.py` is `{}`.

Three gates in `tests/test_coverage_gate.py` exist to collect deferrals, and all
three iterate `DEFERRED`. With it empty they run zero assertions and report
green. The project built the exact mechanism that stops a deferral being
discharged by forgetting it, wrote the deferral in prose in three places, and
never put it in the one table the mechanism reads.

The OLE2 deferral is the control that proves the mechanism can work. The moment
`.doc`, `.xls` and `.ppt` moved into `CAPABILITIES`,
`test_ole2_cannot_ship_without_its_fixtures_and_tests` stopped skipping and
began demanding the fixtures. That gate names its extensions literally instead
of reading `DEFERRED`, which is the only reason it had teeth.

**Pattern:** an enforcement mechanism is engaged only for the entries actually
in its table, and a loop over an empty container is the quietest possible pass.
When you defer something, the last step is to open the thing that is supposed to
collect it and confirm your entry is in the collection. Prose in three files is
still memory.

---

## 2026-09-04 - A recorded pass count is an environment fact, not a gate

The handoff said "expect 292 passed, 4 skipped". This machine gives 291 passed,
5 skipped. Nothing regressed: `tkinterdnd2` is not installed here, so the one
drag and drop test skips instead of passing. Both runs total 296 tests and both
have zero failures.

Read as a gate, that delta looks like a broken project and invites a hunt
through the wrong code. The pass count moved for a reason that has nothing to do
with the product.

**Pattern:** record a pass count together with the environment that produced it,
and compare totals and failures before comparing passes. Then read every skip
reason with `-rs` before calling a suite green. Skips are the load-bearing
number, because a skip is exactly where a test has quietly stopped asserting.

---

## 2026-09-04 - The blocker was a missing fixture, and the fixture was one command away

`.doc`, `.xls` and `.ppt` were deferred with the reason "there is no way on this
machine to produce a genuine legacy Office document". That claim was written
without being tested. LibreOffice was already installed, and

```
soffice --headless --convert-to doc --outdir <dir> seed.docx
```

produces a file starting `d0cf11e0a1b11ae1` carrying the seeded metadata.

The second half of the deferral, that `olefile` cannot resize a stream, was true
and was never the real obstacle. A property set does not have to be full to be
valid: MS-OLEPS allows zero properties and readers reach the set through an
offset table, so an empty property set plus zero padding fits inside any existing
allocation. The constraint that looked fatal turned out to make the
implementation SAFER than a rebuild, because nothing but stream payload ever
changes.

**Pattern:** a deferral's stated reason is a claim, and it ages. Re-test the
blocker before re-deferring. And when a library constraint looks fatal, check
whether the FORMAT allows a smaller valid answer before concluding you have to
rewrite the container.

---

## 2026-09-04 - A tool that will not convert your file is not evidence your file is broken

The OLE2 validity test ran `soffice --convert-to txt` on a scrubbed `.xls` and
failed with "LibreOffice could not re-open the scrubbed .xls; the document is
damaged". The document was not damaged. Calc has no plain-text export filter, so
the same command fails identically on a pristine file straight out of the
converter.

The first instinct was to bisect the engine's three edits looking for the one
that corrupted the workbook. The bisect is what showed the unscrubbed control
failing too.

**Pattern:** any check of the form "an external tool can still read this" needs
the untouched input as a control in the same run. Without it, every
environmental failure of the checker is reported as damage caused by the code
under test, and the debugging goes straight to the wrong place.

---

## 2026-09-04 - LibreOffice serialises on its user profile, and calls it a timeout

Adding three LibreOffice-built fixtures took the suite from 54 seconds to 615,
and made two conversions return nothing at all. Not slow conversions: a second
`soffice --convert-to` attaches to the instance the first one left resident and
either does nothing or blocks until the 180 second timeout.

Giving the test session its own profile fixed both at once:

```
soffice -env:UserInstallation=file:///<temp dir> --headless --convert-to ...
```

70 seconds, no lost conversions, and the run can no longer disturb a LibreOffice
window the developer has open.

**Pattern:** when a subprocess is a desktop application, assume it has global
per-user state and give the test run its own. And read "returned nothing" as a
possible contention symptom, not only as a failure of the input.

---

## 2026-09-04 - A residual scan can find a value in the container's own directory

A scrubbed `.ppt` reported `residual_found` for the value `Current User`. The
value really was in the bytes, once, in UTF-16LE. It was the OLE2 directory
entry that NAMES the `Current User` stream. The engine had correctly blanked the
userName field inside the stream; the collision was that LibreOffice writes a
user name identical to the stream's own name.

The fix had to be positional rather than textual. Deleting every occurrence of
the string would also delete a real survivor sitting in a payload, which is a
false CLEAN, the one direction this project must never fail in. The scan now
blanks the 64-byte name field of each directory entry and nothing else, so
payloads, the FAT and sector slack are all still searched, and a test plants the
same string back into a payload and requires the scan to find it.

**Pattern:** when excluding something from a residual scan, exclude a REGION,
not a STRING. And write the test that proves the exclusion did not overcorrect
in the same change, because that test is the only thing standing between a
narrow exclusion and a silent blind spot.

---

## 2026-09-04 - Do not let convenience silently make a structural decision

**Correction from the maintainer:** "Why did you put it in .tools and not in
<volume>\Projects\Utilities-Tools\Metadata-Scrubber"

The launchers were put in the shared `.tools\bin\` for one reason: that
directory was already on PATH, so a bare `metascrub` worked immediately. That
convenience was allowed to decide the structure, and the trade-off against
project self-containment was never weighed or surfaced.

It was the wrong call. Launchers are project artifacts. Putting them in a shared
bucket meant the project directory alone was not runnable, and the shared
directory would accumulate a launcher per project.

**Pattern:** when one option is easier to wire up, that is not a reason, it is a
pull. Say out loud which structural property is being traded away, and offer the
choice rather than absorbing it. The fix here was a split the user should have
been offered up front: real launchers in the project, thin forwarders in the
PATH directory, the shared third-party binary left shared.

---

## 2026-09-04 - A green suite is not evidence when the failure lives off the tested path

138 tests passed while `metascrub scrub` reported every file CLEAN, verified
clean, exit 0, and changed nothing. The defect only opened when exiftool was
absent, and the suite ran on a machine that had it.

The smoke test that found it was one command on a real file, run before writing
any code, because the question asked was "how do I run this".

**Pattern:** run the thing once, as a user would, on a machine that is not
already set up. Ask what happens when a dependency is missing rather than
present. A test suite proves the paths it exercises and nothing about the ones
that only open on a different machine.

---

## 2026-09-04 - Two states that must never be confused must never share a type

The root cause of the above: `ExifSession.read()` caught every exception and
returned `{}`, so "could not read this file" and "this file carries nothing"
were the same value. The caller guessed, and guessed wrong.

Its own docstring claimed the caller distinguished them via the verification
verdict. It did not. The CLEAN branch returned before verification ran.

**Pattern:** fix the type, not the call site. Patching the one function observed
to be wrong leaves the next reader free to reintroduce it. And a comment
asserting an invariant is not the invariant; if it is load-bearing, a test has
to hold it.

---

## 2026-09-04 - The same conflation came back one level up, wearing the successful read's clothes

`MetadataRead` split "could not read" from "carries nothing" and the lesson
above says the pattern is to fix the type. It was fixed, and the identical bug
was still live three lines later, because there were never two states. There
were three:

    read failed          ok=False, empty
    read succeeded, document parsed, nothing there
    read succeeded, document NOT parsed, nothing reported   <-- this one

The third looked exactly like the second to every caller. exiftool identifies a
truncated `.svg`, exits zero, and says in a Warning that it could not parse it;
`_real_tags()` is empty either way, and the scrubber reported CLEAN over a live
`sodipodi:docname`.

**Pattern:** splitting a type into two states is not evidence that two is the
right number. After separating "failed" from "empty", ask what else can produce
an empty result, and keep asking until the answers stop. The tell is that the
new type's docstring can be read as a claim ("a successful read of an empty file
means the file is empty") that nothing verifies.

---

## 2026-09-04 - The obvious fix was wrong, and only the corpus could say so

The naive predicate here was "if the read carries an ExifTool warning, do not
report CLEAN". It reads as conservative and safe. Measured against the fixture
corpus it fails on four shipped formats: a perfectly good `.ott` carries
`Unrecognized MIMEType application/vnd.oasis.opendocument.text-template` AND
reports zero document tags once scrubbed, so it lands in the exact shape of the
defect. Shipping the obvious fix would have made the tool refuse four formats it
cleans correctly.

Nothing about the warning's wording says it is benign. What says so is that
exiftool emits it on reads that ALSO return the document's Title and Creator. A
warning raised while successfully reporting document tags cannot be evidence
that the document was not parsed. That rule is enforced against a real file, not
written in a comment.

**Pattern:** when a predicate has to separate two classes, do not design it from
the class you are hunting. Collect the class you must NOT break first, from real
inputs, and find the one that sits closest to the boundary. That example is the
design, and it is almost never the one that motivated the work.

Corollary, on direction: this predicate is an ALLOWLIST of benign warnings, not
a denylist of alarming ones. An unrecognised warning costs a refusal on a file
that may have been fine; the other direction costs a false CLEAN on a file that
is not. When a classifier must be wrong sometimes, choose which way before
choosing how.

---

## 2026-09-04 - Three times, a bad test was mistaken for a bad product

Each of these looked like a defect and was not:

1. `pythonw` was reported as working because the diagnostic redirected its
   output, which gave it a real `sys.stdout` and hid the bug. The redirection
   was the flaw in the test.
2. A launcher was reported as importing the wrong copy of the package. The test
   ran with cwd inside the other project, and `sys.path[0]` is `''`.
3. The `sh` launcher was reported as failing through a symlink. Git Bash had
   silently made a copy, not a symlink; `test -L` said so.

**Pattern:** when the product looks broken, prove the harness first. Check what
the test actually did, not what it was meant to do. Two of these three were
caught only because the output was read carefully rather than skimmed for a
pass.

---

## 2026-09-04 - Adding a dependency can subtract a feature

Installing `tkinterdnd2` broke the window entirely. Its Tcl commands exist only
on a `TkinterDnD.Tk()` root, so `drop_target_register` raised on a plain root
and killed construction. All 14 GUI tests and any embedding application would
have gone with it.

**Pattern:** after adding a dependency, run the suite before believing the
feature works. And treat an optional enhancement as optional at runtime, not
just at import: wrap the activation, degrade, and say which state you are in.

---

## 2026-09-04 - Decoration ate the product, and only a screenshot caught it

Styling the checkbox indicators to match the Windows 95 palette set the selected
colour to the same `#FFFFFF` as the unselected one. Every ticked box rendered as
empty. The variables were correct; the window lied about them. "Keep backup"
decides whether the user's originals survive.

No test caught it. Looking at the screenshot did.

**Pattern:** for anything visual, look at it. And when restyling a control that
carries state, the assertion is that the states remain distinguishable, not that
the colours are pretty.

---

## 2026-09-04 - Writing docs through Python string literals corrupts paths

`"\bin"` is a backspace followed by `in`. This silently turned
`.tools\bin` into `.toolsin` in the README twice and in a memory index once,
and was caught the third time only by scanning the output for control
characters.

**Pattern:** use raw strings or `chr(92)` for Windows paths, and verify written
files by byte scan rather than by reading them back on screen. The corruption is
invisible in a terminal.

---

## 2026-09-06 - Keeping a structure by name is an exclusion too

CLAUDE.md trap 11 says an exclusion added to `_STRUCTURAL_TAGS` needs its
overcorrection test in the same commit, because a name on that list is a name
never searched again. The JPEG engine showed the same hazard pointing the other
way, and the trap as written does not cover it.

The keep-list said "keep DNL". Implemented as "keep whatever arrives under
marker 0xDC", that is a 60 KB hiding place inside a segment nothing inspects
again. DNL's real payload is two bytes. The same held for EXP (one byte), DHP
(`6 + 3*Nf`) and DAC (whole 2-byte entries): all four are fixed or computable by
T.81, and all four were being kept at whatever length arrived on disk. Keying
the rule on the marker NUMBER alone would have created five carriers in the act
of protecting five structures that had none.

The fix was to enforce the shape the specification fixes, and to refuse anything
that does not match. `DQT`, `DHT`, `SOF` and `DRI` are deliberately not bounded
yet and that gap is named in the code rather than left implicit.

**Pattern:** an entry on a keep-list is an assertion that the structure cannot
carry a payload, and it needs the same evidence as an entry on a remove-list.
Ask what the maximum payload of the thing you are keeping actually is. If the
answer is "whatever the length field says", it is a carrier, not a structure.

Corollary measured the same day: `ORACLE_ALLOW_PNG_KEEPLIST` documents itself as
holding only fields that "cannot carry identity at all", and `cHRM`'s eight
chromaticity fields do not fully clear that bar. They are 32 bytes of
fixed-point an attacker can write anything into. They cannot hold a string or a
wall-clock time, and a wrong value visibly changes rendering, which is a weaker
guarantee than the seven IHDR fields have. The qualification was recorded rather
than the claim being asserted. `pHYs` sits on the same list while
`png_engine.py` documents `PixelsPerUnitX 5669` as a producer signature on its
own; the structural chunk inventory covers that gap, the allowlist does not.

---

## 2026-09-07 - Driving an app headlessly proves the engine, not the product

The Android app was verified by `adb shell am start` plus reading a
`METASCRUB_RESULT` line out of logcat, on an emulator and then on a real phone.
Both passed. The scrubbed output was byte-identical to the desktop engine's, so
the arm64 port was genuinely correct.

The app was still unusable. `targetSdk 35` makes an activity edge-to-edge by
default, the app applied no window insets anywhere, and the "Send the cleaned
copy" button was laid out at the bottom of a LinearLayout with no bottom
padding. On a Pixel 9 Pro XL with THREE-BUTTON navigation the ~126 px bar
covered essentially the whole button. The file was scrubbed correctly and the
user could not act on it. The owner found it in thirty seconds by looking at
his phone.

Two compounding errors, and the second is the one worth remembering:

1. Nothing ever took a screenshot. `lessons.md` already carried
   "for anything visual, look at it" from the GUI work, and it was not applied.
2. The emulator would probably not have caught it even WITH a screenshot,
   because it defaults to GESTURE navigation, whose inset is a few px. The bug
   only appears at a three-button inset. So "tested on a device" was not the
   bar; "tested in the user's configuration" was.

**Pattern:** a headless drive answers "did the code run". It cannot answer "can
a person complete the task". For anything with a UI, the acceptance evidence is
a screenshot plus the interactive element's measured bounds, checked against the
system bars, in the navigation mode the user actually has. `uiautomator dump`
gives the bounds; `settings get secure navigation_mode` gives the mode.

Corollary paid for the same hour: `cmd overlay enable
com.android.internal.systemui.navbar.threebutton`, used to reproduce the user's
navigation mode on the emulator, took down system_server. `activity` and
`package` services disappeared and the AVD needed a reboot. Change a system
setting with `settings put` where one exists, and do not reconfigure a machine
that is running someone else's work.
