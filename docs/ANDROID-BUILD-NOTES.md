# Android build notes: Phase 4, the app shell

Everything in this file was measured on 2026-09-07 on the Windows dev machine
against emulator `emulator-5554`. Nothing in it is inferred. Where a number
appears, the command that produced it is quoted alongside it.

Phase 4 in `docs/ANDROID-MEDIA-BUILD.md` Part 5 is: "Chaquopy shell, and a
share-target activity so the flow is gallery, Share, metascrub, on to the
destination app. Write a new scrubbed file rather than editing the original in
MediaStore."

**Status: built, installed, driven, and verified. A real file was scrubbed on
device and the removal was proved from the desktop against the pulled bytes.**

**The app has since outgrown that description, 2026-09-07.** A share target
alone made the launcher icon a dead end: it opened a screen that said "share a
photo to this app" and offered no way to do anything. There are now two entry
points, an in-app picker using `ACTION_OPEN_DOCUMENT` with multi-select and the
share target handling both `ACTION_SEND` and `ACTION_SEND_MULTIPLE`, and the
cleaned copy is saved where the user chooses rather than only handed to another
app. Still no permissions: the pickers grant per-file access. See "In-app picker
and save" below for the measurements.

---

## Answers to the four questions, up front

| Question | Answer |
|---|---|
| Did the APK build? | Yes. `BUILD SUCCESSFUL in 3m 32s`, 36053785 bytes. |
| Did it install? | Yes. `adb install -r` returned `Success` on `emulator-5554`. |
| Did the share flow run? | Yes, for JPEG, PNG and MP4, driven by `am start` with an `ACTION_SEND` intent, and the onward hand-off was completed through the real system chooser into a third-party app. |
| Was a real file verifiably scrubbed on device? | Yes. Eleven planted sentinels across three containers, all present in the inputs, none present in the outputs pulled back off the device. |
| Blocker? | One, and it is in the TEST HARNESS rather than the app: `adb shell am start --grant-read-uri-permission` cannot mint a MediaStore URI grant for a third app, so the intent had to carry a `file://` URI into the app's own storage instead of a gallery's `content://media/...` URI. Details and evidence in "What did not work" below. |

---

## What was built

Everything lives under `android/`, plus this file. No file outside those two
paths was modified.

```
android/
  settings.gradle                 plugin and dependency repositories
  build.gradle                    plugin versions, applied in :app
  gradle.properties               no machine paths, see "Choosing the JDK"
  gradlew, gradlew.bat            wrapper, generated from Gradle 8.14
  gradle/wrapper/                 wrapper jar and properties
  .gitignore                      build outputs, .gradle, local.properties, keys
  app/
    build.gradle                  AGP + Chaquopy config and copyMetascrubPackage
    proguard-rules.pro            empty on purpose; nothing is minified
    src/main/AndroidManifest.xml  the SEND filter, the launcher, the FileProvider
    src/main/java/io/github/h4d35x0/metascrub/ShareActivity.java
    src/main/python/metascrub_android.py
    src/main/res/values/strings.xml
    src/main/res/xml/file_paths.xml
```

`android/local.properties` is generated locally and is gitignored. It holds
`sdk.dir` and nothing else. No key, keystore or signing config exists anywhere
in the tree; `assembleRelease` would produce an unsigned APK.

### The metascrub package is not copied into the app tree

`app/build.gradle` registers a `Sync` task, `copyMetascrubPackage`, that copies
`<repo>/metascrub` into `app/build/generated/python/metascrub` at build time and
adds that directory as a Chaquopy Python source root. There is exactly one copy
of the package in the repository and the app cannot drift from it.

The task asserts, in a `doLast`, that seven specific files landed. A `Sync` from
a missing or wrong directory otherwise produces an empty package and a build
that succeeds while shipping nothing, which is the same fail-open shape as trap
2 in `CLAUDE.md`.

Verified by reading the APK rather than by trusting the task:

```
$ python -c "... zipfile over app-debug.apk, then over assets/chaquopy/app.imy"
app.imy entries: 29
   metascrub/__init__.pyc  metascrub/capabilities.pyc  metascrub/cli.pyc
   metascrub/engines/{av,base,exiftool,isobmff,jpeg,odf,ole2,ooxml,pdf,png,svg,webp}_engine.pyc
   metascrub/exif_io.pyc  metascrub/gps_verify.pyc  metascrub/gui.pyc
   metascrub/isobmff.pyc  metascrub/scrubber.pyc  metascrub/selftest.pyc
   metascrub/structure.pyc  metascrub/theme.pyc  metascrub/verify.pyc
   metascrub_android.pyc
```

The whole package ships, unmodified, including the modules the phone cannot use.
That is deliberate: shipping a curated subset would mean maintaining a second
opinion about which modules matter, and the cost of the extra `.pyc` files is
negligible against a 36 MB APK whose bulk is the CPython runtime.

### Two files in `android/` are third-party artifacts, kept verbatim

`gradle/wrapper/gradle-wrapper.jar` is binary, and `gradlew` is the shell
launcher Gradle generates. `gradlew` contains five non-ASCII lines: the Apache
copyright line at line 4, and a comment block at lines 37 to 40 that quotes
shell constructs inside guillemets. They are Gradle's bytes, not authored prose,
and editing a generated launcher to satisfy a punctuation convention would be a
worse trade than leaving it identical to what every other Gradle project ships.

`gradlew.bat` was converted from Gradle's CRLF to LF to match the repository's
`.gitattributes` rule of `* text=auto eol=lf`. That was measured rather than
assumed, because a cmd.exe batch file with LF endings is a known failure mode:

```
$ python -c "convert gradlew.bat CRLF -> LF"
$ ./gradlew.bat -Dorg.gradle.java.home="..." --version
Gradle 8.14
$ ./gradlew.bat -Dorg.gradle.java.home="..." assembleDebug
BUILD SUCCESSFUL in 55s
```

Every other file under `android/` is ASCII with LF endings.

---

## The version matrix that works

All four of these were resolved and used by a build that succeeded. They are
readings, not recommendations copied from a compatibility page.

| Component | Version | Why |
|---|---|---|
| Chaquopy Gradle plugin | 17.0.0 | Latest on Maven Central (`com.chaquo.python:gradle`, `<release>17.0.0</release>`, lastUpdated 20251130210915). Its docs state AGP 7.3 - 9.2, Python 3.10 - 3.14, minimum API 24. |
| Android Gradle Plugin | 8.10.1 | Inside Chaquopy's stated range. Requires Gradle 8.11.1 or newer. |
| Gradle | 8.14 | Already present in `~/.gradle/wrapper/dists/gradle-8.14-all`, so the wrapper resolves it without a download. |
| JDK for the build | Temurin 17.0.13+11 | See below. `java` on PATH is OpenJDK 23.0.1, which was NOT used. |
| Target Python | 3.11 (pinned) | The build machine has 3.11.3 at `C:\Python311`; Chaquopy needs a local interpreter of the same major.minor. On device this resolves to CPython 3.11.14. |
| compileSdk / targetSdk | 35 | Matches the emulator. |
| minSdk | 26 | Chaquopy 17 needs 24; 26 avoids separate handling for pre-scoped-storage behaviour for no benefit. |
| ABIs | `arm64-v8a`, `x86_64` | x86_64 is the emulator, arm64-v8a is every real phone. Two, not "all", so the APK does not carry runtimes nobody executes. |

### Choosing the JDK

`java -version` on this machine reports `openjdk version "23.0.1"`. That is newer
than AGP 8.10 targets and there was no reason to find out the hard way, because
a second JDK is already installed:

```
$ ls "/c/Program Files/Java"
jdk-17.0.13.11-hotspot/
jdk-23/
```

`org.gradle.java.home` is deliberately NOT written into `gradle.properties`,
because that file is committed and the JDK path is a machine path. Pass it per
invocation instead. Every build in this document used:

```
cd android
./gradlew.bat -Dorg.gradle.java.home="C:/Program Files/Java/jdk-17.0.13.11-hotspot" assembleDebug
```

Confirmed rather than assumed, because `gradlew --version` reports only the
launcher JVM (which is the 23.0.1 on PATH) and says nothing about the JVM that
actually runs the build:

```
$ ./gradlew.bat -Dorg.gradle.java.home="C:/Program Files/Java/jdk-17.0.13.11-hotspot" \
    -I <script printing System.getProperty("java.version")> -q help --no-daemon
DAEMON JVM: 17.0.13 at C:\Program Files\Java\jdk-17.0.13.11-hotspot
```

The Android Studio bundled JBR at
`C:\Program Files\Android\Android Studio\jbr\bin` is NOT usable: it contains
`java.dll` but no `java` executable. Measured, because it was the obvious first
choice.

### Generating the wrapper without Gradle on PATH

`gradle` is not on PATH. The wrapper was generated in a scratch directory from
the cached distribution and then copied into `android/`, so that no plugin
resolution had to succeed before the wrapper existed:

```
$ cd <scratch>/wrapgen && echo "rootProject.name='wrapgen'" > settings.gradle
$ JAVA_HOME="C:/Program Files/Java/jdk-17.0.13.11-hotspot" \
  "$HOME/.gradle/wrapper/dists/gradle-8.14-all/c2qonpi39x1mddn7hk5gh9iqj/gradle-8.14/bin/gradle.bat" \
  wrapper --gradle-version 8.14 --distribution-type all --no-daemon
BUILD SUCCESSFUL in 1m 22s
```

---

## Does the Python import survive without the desktop dependencies?

This was the specific risk named in the task, so it was measured twice, on both
sides.

**On the desktop, with a meta-path blocker that was itself proved to work first**
(a probe that finds nothing has two explanations, and the boring one is that the
probe is broken):

```
blocker ok: pikepdf -> blocked (android simulation): pikepdf
blocker ok: olefile -> blocked (android simulation): olefile
blocker ok: exiftool -> blocked (android simulation): exiftool
----------------------------------------
import metascrub OK, version 1.0.2
OK metascrub.engines.jpeg_engine
OK metascrub.engines.png_engine
OK metascrub.engines.webp_engine
OK metascrub.engines.isobmff_engine
OK metascrub.structure
OK metascrub.engines
```

**On the device**, read out of the launcher screen's own diagnostics call and
logged as a single line to logcat:

```json
{
  "python": "3.11.14 (main, Nov 20 2025, 18:07:04) [Clang 18.0.4 ...]",
  "platform": "Linux-6.6.30-android15-8-gdd9c02ccfe27-ab11987101-x86_64-with-libc",
  "engines": {"jpeg": "ready", "png": "ready", "webp": "ready", "isobmff": "ready"},
  "structure_walkers": [".gif", ".png", ".webp"],
  "optional_desktop_dependencies": {
    "exiftool": "absent (ModuleNotFoundError)",
    "pikepdf":  "absent (ModuleNotFoundError)",
    "olefile":  "absent (ModuleNotFoundError)",
    "PIL":      "absent (ModuleNotFoundError)"
  },
  "metascrub_version": "1.0.2"
}
```

**Nothing breaks, and nothing had to be hacked around.** `import metascrub`
succeeds on the phone with all four desktop dependencies absent, because every
one of them is already behind a `try: import ... except ImportError` in
`pdf_engine.py`, `ole2_engine.py` and `exif_io.py`. `gui.py` imports tkinter at
module scope but is never imported by `metascrub/__init__.py`, so it is compiled
into the APK and never loaded. That is the answer to "report exactly what
breaks": nothing does.

What that reading also shows is the thing worth keeping: the four engines report
`ready` on a device where exiftool, pikepdf, olefile and Pillow are all
`ModuleNotFoundError`. The pure-Python engines are demonstrably the ones doing
the work.

---

## The app

### The share target

`ShareActivity` has two intent filters: `ACTION_SEND` for `image/*` and
`video/*`, and `MAIN`/`LAUNCHER`. The launcher entry shows the diagnostics above
rather than a file picker, because the app has no business browsing storage.

The manifest declares **no permissions at all**. It reads exactly one file,
through the URI grant that arrives with the share, and writes only inside its own
storage. There is no `INTERNET` permission, so nothing it reads can leave the
device.

### It writes a new file, and cannot do otherwise

The incoming URI is only ever opened for reading. There is no code path that
opens it for writing, so "never destroy the original" is structural here rather
than a policy the code has to remember. This is what Part 5 means by the backup
policy not needing a redesign: **the desktop tool's `<path>.backup` policy has no
job on this platform**, because the original is never the thing being modified.

Measured, on all three test files, comparing the on-device original before and
after the scrub:

```
$ adb shell run-as io.github.h4d35x0.metascrub md5sum <APPDIR>/files/input/fixture.jpg
39822072ab18b73764e746d14529753a   (desktop md5 of the fixture: 39822072ab18b73764e746d14529753a)
$ adb shell run-as io.github.h4d35x0.metascrub md5sum <APPDIR>/files/input/fixture.png
8017a4027c8bfe8b5a176608a0cdd9cb   (desktop: 8017a4027c8bfe8b5a176608a0cdd9cb)
$ adb shell run-as io.github.h4d35x0.metascrub md5sum <APPDIR>/files/input/fixture.mp4
0e82b0ee7df5e1764b99e66f5a5c611f   (desktop: 0e82b0ee7df5e1764b99e66f5a5c611f)
```

### The output name is neutral, by default and without a toggle

Section 1.2 of the design document: `PXL_20260906_143022891.jpg` carries the
capture time to the millisecond and names the device family, and no byte scan of
the file will ever catch it because it is not in the file.

The outputs written on device were `clean_e127f3ab.jpg`, `clean_ff62da0f.png`
and `clean_c9b463f2.mp4`. The extension comes from the container's magic bytes,
never from the input name, and the random stem carries nothing.

Note the divergence from the desktop, which is intentional and is already
argued in `scrubber.py`: on the desktop `neutral_names` is off by default because
renaming a user's file in place is destructive. Here the output is a new file
that never had a name, so there is nothing to preserve and nothing to toggle.

### Container detection is by magic bytes

`metascrub_android.detect()` reads the first 32 bytes and dispatches on
`FF D8 FF`, the PNG signature, `RIFF....WEBP`, and `ftyp` at offset 4. For ISO
base media it reads the brand and picks the output extension from it, so a `.qt`
input that is really ISO/MP4 gets `.mp4` and a `.mp4` that is really QuickTime
gets `.mov`. That is trap 8 in `CLAUDE.md` applied here, and on Android it is not
merely prudent: a `content://` URI frequently has no usable filename at all, so
extension dispatch is often impossible rather than just risky.

### What the app refuses to claim

The desktop tool proves a scrub by capturing metadata values BEFORE the write
and searching the output bytes for those exact values afterwards. That baseline
read is an exiftool read. exiftool cannot run here, so **the residual-value scan
is not available on device and the app does not print the word "verified"
anywhere.**

What it does show is what the engine reported targeting, plus the structural scan
from `metascrub.structure`, which asks a question that needs no baseline: is
there any region of the output the container's own structure does not account
for? Where no walker exists the screen says so in words:

```
Structural scan: no walker for this container, so
this check has no opinion. It was not run.
```

rather than leaving a blank that reads as a clean. `applicable: false` and
"clean" must never share a representation.

### MetadataScrubber is not used, and that is correct

`metascrub.scrubber.MetadataScrubber` reads a baseline through exiftool before
touching a file and refuses to proceed when that read is unavailable. On Android
that read is unavailable for every file, so `MetadataScrubber` would correctly
refuse every file. The app therefore drives the four engines directly. This is
not a workaround for a bug; the refusal is the fix for the fail-open defect
recorded in `CLAUDE.md` trap 2 and it must not be weakened. See "Python-side
changes that would help" below for the shape of a proper answer.

---

## A defect found and fixed during verification

The first working build handed the cleaned file on with `EXTRA_STREAM` and
`FLAG_GRANT_READ_URI_PERMISSION` only. Driving the button revealed that the
system chooser's own preview loader is not covered by that grant:

```
D ImagePreviewImageLoader: failed to load
  content://io.github.h4d35x0.metascrub.fileprovider/scrubbed/clean_e02bdf55.jpg preview
D ImagePreviewImageLoader: java.io.IOException: java.lang.SecurityException:
  Permission Denial: opening provider androidx.core.content.FileProvider from
  ProcessRecord{... com.android.intentresolver/u0a102} (pid=7783, uid=10102)
  that is not exported from UID 10209
```

The URI in an intent's `ClipData` is what the framework walks when it propagates
grants. Adding

```java
send.setClipData(ClipData.newUri(getContentResolver(), "cleaned copy", shareUri));
```

fixes it, and also covers receivers that read `getClipData()` rather than the
extra. After the fix, on a clean rerun:

```
$ adb logcat -d | grep -c "Permission Denial: opening provider androidx.core.content.FileProvider"
0
```

This is worth recording because a Gradle build proves nothing about it, a unit
test would not have found it, and the app would have looked like it worked.

---

## The verification chain, with real commands and real output

### The fixtures

Three files, eleven sentinels, each in a different carrier, every sentinel
**proved present in the input bytes before anything was run**. A verification
that starts from a needle the file never contained proves nothing.

JPEG, `PXL_20260907_143022891.jpg`, 36436 bytes:

```
  planted artist    MSCRUB-SENTINEL-ARTIST-7f3a91c2       at offset 258
  planted comment   MSCRUB-SENTINEL-USERCOMMENT-4e8d02ba  at offset 416
  planted software  MSCRUB-SENTINEL-SOFTWARE-b6041da9     at offset 224
  planted thumb     MSCRUB-SENTINEL-THUMBCOMMENT-cc25e730 at offset 666
  planted trailer   MSCRUB-SENTINEL-MOTIONVIDEO-8a1b46ef  at offset 36329
  planted xmp       MSCRUB-SENTINEL-XMPTITLE-19c7fe55     at offset 2121
```

The `thumb` sentinel is a COM segment inside the 990-byte EXIF thumbnail, so it
tests wholesale APP1 removal rather than tag-by-tag editing. The `trailer`
sentinel is the title of a real 4086-byte x264 MP4 appended past the JPEG's EOI,
which is the motion photo shape the design document calls the highest-severity
carrier.

PNG, `Screenshot_2026-09-07-14-30-22_signal.png`, 2089 bytes: sentinels in
`tEXt`, in an `iTXt` XMP packet, in an `eXIf` chunk with GPS, in a **private
`prVt` ancillary chunk exiftool does not report**, and in data appended past
`IEND`. The last two are exactly the carriers a residual-value scan is blind to.

MP4, `VID_20260907_143022.mp4`, 11693 bytes: sentinels in `ilst` title, comment
and artist, plus the x264 SEI signature and the Lavc/Lavf muxer strings that
ffmpeg writes on its own.

### Getting the fixture onto the device

```
$ adb push <scratch>/fixture.b64 /data/local/tmp/fixture.b64
$ adb shell "cat /data/local/tmp/fixture.b64 | run-as io.github.h4d35x0.metascrub \
    sh -c 'base64 -d > /data/user/0/io.github.h4d35x0.metascrub/files/input/fixture.jpg'"
$ adb shell run-as io.github.h4d35x0.metascrub md5sum \
    /data/user/0/io.github.h4d35x0.metascrub/files/input/fixture.jpg
39822072ab18b73764e746d14529753a
$ md5sum <scratch>/PXL_20260907_143022891.jpg
39822072ab18b73764e746d14529753a
```

Base64 rather than a direct push: see "What did not work".

### Driving the share

```
$ adb shell am force-stop io.github.h4d35x0.metascrub
$ adb logcat -c
$ adb shell am start -a android.intent.action.SEND -t image/jpeg \
    --eu android.intent.extra.STREAM \
    file:///data/user/0/io.github.h4d35x0.metascrub/files/input/fixture.jpg \
    -n io.github.h4d35x0.metascrub/io.github.h4d35x0.metascrub.ShareActivity
Starting: Intent { act=android.intent.action.SEND typ=image/jpeg cmp=io.github.h4d35x0.metascrub/.ShareActivity (has extras) }
$ adb logcat -d -s metascrub:V
I metascrub: staged 36436 bytes from file uri into the cache directory
I metascrub: METASCRUB_RESULT {"ok": true, "container": "jpeg", "engine": "jpeg",
  "input_bytes": 36436, "output_bytes": 27691,
  "output_path": "/storage/emulated/0/Android/data/io.github.h4d35x0.metascrub/files/scrubbed/clean_e127f3ab.jpg",
  "removed": [
    "APP1 'Exif', carrying EXIF, including GPS and the embedded thumbnail (1630 bytes)",
    "APP1 'http://ns.adobe.com/xap/1.0/', carrying XMP (3029 bytes)",
    "trailing data after EOI (4086 bytes); a motion photo trailer is a complete MP4 carrying its own GPS"],
  "structure": {"applicable": false, "unaccounted": [], "error": null}, "error": null}
```

PNG, same commands with `-t image/png`:

```
I metascrub: METASCRUB_RESULT {"ok": true, "container": "png", "engine": "png",
  "input_bytes": 2089, "output_bytes": 1088,
  "output_path": ".../scrubbed/clean_ff62da0f.png",
  "removed": ["tEXt chunk, uncompressed text chunk (37 bytes)",
    "tEXt chunk, uncompressed text chunk (38 bytes)",
    "iTXt chunk, international text chunk (XMP lives here) (369 bytes)",
    "tEXt chunk, uncompressed text chunk (33 bytes)",
    "eXIf chunk, EXIF block (196 bytes)",
    "prVt chunk, unknown ancillary chunk (32 bytes)",
    "trailing data after IEND (224 bytes)"],
  "structure": {"applicable": true, "unaccounted": [], "error": null}, "error": null}
```

MP4, same commands with `-t video/mp4`:

```
I metascrub: METASCRUB_RESULT {"ok": true, "container": "isobmff", "engine": "isobmff",
  "input_bytes": 11693, "output_bytes": 11693,
  "output_path": ".../scrubbed/clean_c9b463f2.mp4",
  "removed": ["udta box at offset 11411, 282 bytes",
    "compressorname '\\x15Lavc61.26.100 libx264' in the avc1 sample entry at offset 10747",
    "an SEI user-data NAL carrying 'x264 - core 164 r3198 da14df5 - H.264/MPEG-4 AVC codec - Cop' (686 bytes at offset 52, in the sample at offset 48), overwritten with SEI filler",
    "hdlr name 'VideoHandler' at offset 10646"],
  "structure": {"applicable": false, "unaccounted": [], "error": null}, "error": null}
```

Note the MP4 is 11693 bytes in and 11693 bytes out. The in-place zero-fill and
SEI-filler strategy from section 2.5 keeps every offset valid, which is why no
`stco` fixup is needed.

### Pulling the outputs back

```
$ adb pull /sdcard/Android/data/io.github.h4d35x0.metascrub/files/scrubbed/clean_e127f3ab.jpg <scratch>/pulled_clean.jpg
1 file pulled, 0 skipped. 2.1 MB/s (27691 bytes in 0.012s)
$ adb shell md5sum /sdcard/Android/data/.../clean_e127f3ab.jpg
0890d84b0b6dd47e1680ca2cf515bc2f
$ md5sum <scratch>/pulled_clean.jpg
0890d84b0b6dd47e1680ca2cf515bc2f
```

The pull is faithful, so anything measured on the desktop copy is a measurement
of the bytes the phone wrote.

### exiftool on the pulled JPEG

Before, the fixture reported `Make: SentinelCorp`, `Model: SentinelPhone X1`,
`Software`, `Artist`, `Copyright`, `UserComment`, `DateTimeOriginal`, the whole
`[GPS]` group, `[IFD1] ThumbnailImage (Binary data 990 bytes)`, and
`[XMP-dc] Title`/`Creator`.

After, `exiftool -a -G1 -s` on the pulled file reports, in full, apart from the
`[System]` and `[ExifTool]` groups which describe the desktop file rather than
its contents:

```
[File] FileType: JPEG   FileTypeExtension: jpg   MIMEType: image/jpeg
[File] ImageWidth: 800  ImageHeight: 600  EncodingProcess: Baseline DCT, Huffman coding
[File] BitsPerSample: 8  ColorComponents: 3  YCbCrSubSampling: YCbCr4:2:0 (2 2)
[JFIF] JFIFVersion: 1.01  ResolutionUnit: None  XResolution: 1  YResolution: 1
[Composite] ImageSize: 800x600  Megapixels: 0.480
```

No IFD0, no ExifIFD, no GPS, no IFD1, no XMP.

### The byte search, which is the measurement that counts

exiftool reporting nothing is not evidence. This is:

```
sentinel                             in input  in output
artist (EXIF Artist + Copyright)     yes       no
comment (EXIF UserComment)           yes       no
software (EXIF Software)             yes       no
thumb (COM inside EXIF thumbnail)    yes       no
trailer (MP4 title past EOI)         yes       no
xmp (XMP-dc:Title)                   yes       no

literal 'Exif\x00\x00'         input=True  output=absent
XMP namespace URI              input=True  output=absent
MP4 'ftyp' brand box           input=True  output=absent
x264 encoder signature         input=True  output=absent
camera make                    input=True  output=absent
camera model                   input=True  output=absent
capture timestamp              input=True  output=absent

RESULT: CLEAN
```

PNG and MP4, same method:

```
PNG   2089 bytes in, 1088 bytes out
  tEXt Author                input=yes  output=gone
  iTXt XMP title             input=yes  output=gone
  eXIf Artist                input=yes  output=gone
  private prVt chunk         input=yes  output=gone
  data appended past IEND    input=yes  output=gone
  Software/CreationTime      input=yes  output=gone
MP4   11693 bytes in, 11693 bytes out
  ilst title                 input=yes  output=gone
  ilst comment               input=yes  output=gone
  ilst artist                input=yes  output=gone
  x264 encoder signature     input=yes  output=gone
  Lavc muxer signature       input=yes  output=gone
  Lavf muxer signature       input=yes  output=gone
  VideoHandler               input=yes  output=gone
RESULT: CLEAN
```

Eleven planted sentinels plus thirteen incidental needles, all present in the
inputs, none in the outputs.

### The outputs are still the files they were

A scrub that corrupts the image is not a success.

```
JPEG: PIL opens the pulled file at (800, 600) RGB;
      max pixel difference against the pre-metadata original, over all channels: 0
PNG:  PIL opens the pulled file at (400, 300) RGB, info text keys: []
MP4:  ffprobe -> codec_name=h264 width=320 height=240 nb_frames=30 duration=2.000000
      framemd5 over the 30 DECODED frames, original vs scrubbed:
        75955b1ec3266b52e59468706c725ca6
        75955b1ec3266b52e59468706c725ca6
      DECODED FRAMES: bit-identical
```

A note on how that MP4 check has to be run, because getting it wrong looks like a
failure: `ffmpeg -f framemd5 -c copy` hashes the *packet* bytes, and the SEI NAL
that was overwritten with filler lives inside a packet, so `-c copy` reports a
difference that is the fix working rather than damage. Dropping `-c copy` hashes
decoded frames, which is the claim the design document actually makes.

### The same bytes as the desktop engine

The strongest single check available, and it is cheap. The desktop `JpegEngine`
was run on the same fixture and the outputs compared:

```
desktop output md5: 0890d84b0b6dd47e1680ca2cf515bc2f  27691 bytes
phone   output md5: 0890d84b0b6dd47e1680ca2cf515bc2f  27691 bytes
BYTE-IDENTICAL: True
```

The device produced byte-for-byte the same file as the desktop engine, and
reported the same three removals in the same words. The phone is running the
repository's code, not a lookalike.

### The onward hand-off

The button was tapped through `adb shell input tap`, coordinates read from a
`uiautomator dump` rather than guessed:

```
$ adb shell uiautomator dump /sdcard/u1.xml
$ adb shell cat /sdcard/u1.xml | grep 'text="SEND THE CLEANED COPY"'
  ... bounds="[42,2232][1038,2358]"
$ adb shell input tap 540 2295
$ adb shell dumpsys activity activities | grep topResumedActivity
  topResumedActivity=ActivityRecord{... com.android.intentresolver/.ChooserActivityLauncher t15}
$ adb shell cat /sdcard/u2.xml | grep -o 'text="[^"]*"' | sort -u
  text="Add to Maps"  text="Drive"  text="Maps"  text="Print"
  text="Quick Share"  text="Scrub metadata"  text="Sharing image"  text="metascrub"
$ adb shell input tap 324 2246          # the "Print" target
$ adb shell dumpsys activity activities | grep topResumedActivity
  topResumedActivity=ActivityRecord{... com.android.printspooler/.ui.PrintActivity t16}
$ adb shell uiautomator dump /sdcard/u3.xml && adb shell cat /sdcard/u3.xml | grep -o 'text="[^"]*"'
  text="1/1"  text="Copies:"  text="Index Card 4x6"  text="Paper size:"  text="Select a printer"
$ adb logcat -d | grep -ci "Permission Denial.*fileprovider|SecurityException.*fileprovider"
0
```

A third-party app, the system print spooler, resolved
`content://io.github.h4d35x0.metascrub.fileprovider/scrubbed/clean_*.jpg`, read it,
and rendered it as a one-page document. Zero permission denials. The full loop
runs: share in, scrub, new file, share out.

Also visible in that UI dump, which is worth quoting because it is the app's
own honesty about what it measured:

```
Structural scan: no walker for this container, so
this check has no opinion. It was not run.

exiftool cannot run on Android, so the desktop tool's
residual-value scan was not performed. What is above
is what was measured, and nothing more.
```

---

## What did not work, and exactly why

Recorded so nobody re-derives these. Each is a real measurement, not a guess.

**1. `am start --grant-read-uri-permission` cannot hand a MediaStore URI to a
third app.** This is the one genuine gap in the verification, and it is in the
harness rather than the app.

The fixture was pushed to `/sdcard/Pictures/`, scanned into MediaStore
(`_id=1000000033`), and sent with the grant flag set (`flg=0x1` is visible in the
`am` output). The app still got:

```
E metascrub: java.lang.SecurityException: io.github.h4d35x0.metascrub has no access
  to content://media/external/images/media/1000000033
  at android.content.ContentResolver.openInputStream(ContentResolver.java:1528)
  at io.github.h4d35x0.metascrub.ShareActivity.stage(ShareActivity.java:207)
```

Shell can read the URI itself (`content read --uri ... | wc -c` returned
`36436`), so this is not shell lacking access. Shell's access to MediaProvider
comes from its group membership and an adb allowlist rather than from a URI
permission it holds, and a grant can only be delegated by a caller that holds
one. `adb root` is not an escape on this image:

```
$ adb root
adbd cannot run as root in production builds
```

The workaround was a `file://` URI into the app's own internal storage, which
exercises every line of the app except the grant itself. **What remains
unverified is precisely one thing: that a real gallery's `content://media/...`
grant reaches this activity.** That is standard Android behaviour for an exported
`ACTION_SEND` receiver and the code uses `IntentCompat.getParcelableExtra` plus
`ContentResolver.openInputStream`, but it was not measured here and is not
claimed. To close it, share from a real gallery by hand, or use a rooted AVD such
as the `frida_root_api35` image also present on this machine.

**2. Pushing into `/sdcard/Android/data/<pkg>/files/` and reading it as the app
fails with EACCES.**

```
E metascrub: java.io.FileNotFoundException:
  /sdcard/Android/data/io.github.h4d35x0.metascrub/files/input/PXL_...jpg:
  open failed: EACCES (Permission denied)
```

Cause, from `ls -l`: a directory created there by `adb shell mkdir` is
`drwxrws--- shell ext_data_rw`. The app's uid is not in `ext_data_rw`, so it
cannot traverse the directory even though the file inside is mode 0666. The same
listing shows the directory the app created itself is `drwxrws--- u0_a209
ext_data_rw`, which the app can traverse. Create app-owned directories with
`run-as`, or stay in internal storage.

**3. `adb shell "run-as pkg sh -c 'cat > file'" < binary` silently truncates.**
A 36436-byte JPEG arrived as 65 bytes with a different md5. The transfer is going
through a PTY. Base64 the file, push the text with `adb push`, and decode
entirely on device with `cat ... | run-as ... sh -c 'base64 -d > ...'`. That
round-trips with a matching md5.

**4. Git Bash rewrites device paths.** `adb push x /sdcard/Pictures/x` failed
with `remote secure_mkdirs() failed`, because MSYS turned `/sdcard/Pictures/x`
into `C:/Program Files/Git/sdcard/Pictures/x`. Every adb command in this document
was run with `MSYS_NO_PATHCONV=1 MSYS2_ARG_CONV_EXCL='*'` exported.

**5. `sdk.dir` in `local.properties` must not contain raw backslashes.** A
Windows path written as `C:\Users\...` produced:

```
Could not determine the dependencies of task ':app:compileDebugJavaWithJavac'.
> java.io.IOException: The filename, directory name, or volume label syntax is incorrect
    at com.android.build.gradle.internal.SdkLocator$SdkLocationSource.validateSdkPath
```

A `.properties` file is Java-escaped, so `\U` is consumed as an escape. Write
`sdk.dir=C\:/Users/<you>/AppData/Local/Android/Sdk`.

**6. The bundled Android Studio JBR is not a usable JDK here.**
`C:\Program Files\Android\Android Studio\jbr\bin` contains `java.dll` and no
`java` executable.

**Not a problem, though it was expected to be:** Java 23 versus AGP never came
up, because a Temurin 17 was already installed and was used from the first
invocation. AGP 8.10.1 on JDK 23 is therefore untested here, neither working nor
broken.

**Also not a problem:** `stripDebugDebugSymbols` prints
`Unable to strip the following libraries, packaging them as they are:
libchaquopy_java.so, libpython3.11.so, ...`. That is a warning about Chaquopy's
prebuilt runtime, not an error, and the APK works.

---

## Python-side changes that would help, written down and NOT made

Per the task's constraint, none of these were implemented. They are ordered by
how much they would improve the app.

**1. A content-sniffing dispatcher inside the package.** `metascrub_android.py`
carries its own magic-byte table and its own ISO brand-to-extension map. That is
a second opinion about container identity living outside `metascrub`, and the
project's own history (trap 8) is about exactly that kind of table drifting from
reality. A `metascrub/sniff.py` returning `(Container, Engine, extension)` from
bytes, used by both the app and the desktop, would collapse it to one table with
one set of tests. The brand map in the app module is the piece most likely to rot.

**2. Nothing in `CAPABILITIES` routes to `Engine.JPEG`, `PNG`, `WEBP` or
`ISOBMFF`.** `metascrub/engines/__init__.py` says so in a comment and it is
accurate. The consequence for Phase 4 is that `MetadataScrubber` can never reach
the four engines the phone depends on, so the app has to instantiate them
directly and reimplement dispatch. This is presumably deliberate sequencing and
belongs to Phase 1/2 rather than to me, but it is the reason the app talks to
engines rather than to the orchestrator.

**3. `MetadataScrubber` has no mode that can run without an oracle.** The refusal
is correct and must not be weakened. What is missing is a documented second mode
that performs the removal and returns a verdict that is explicitly NOT the
desktop's verified-clean, so the app could use the orchestrator's backup policy,
status model and reporting instead of bypassing all of it. The critical
constraint is that the two verdicts must never share a representation, which is
the same rule `ReadOutcome` already enforces one level down. Doing this by adding
a boolean flag to `sanitize_file` would be the wrong shape.

**4. `structure.py` has walkers for `.png`, `.webp` and `.gif` only.** JPEG and
ISO base media, the two formats that matter most on a phone, have none. On device
the structural scan is the ONLY proof available, so those two containers
currently get a scrub with no on-device evidence at all beyond the engine's own
say-so. The app reports that honestly rather than hiding it, but it is the single
biggest gap in what an Android build can claim. A JPEG walker in particular looks
tractable: `jpeg_engine._walk` already consumes every segment by its declared
length and finds the outer EOI, which is most of what a walker needs.

**5. Minor, no action needed.** `metascrub/gui.py` imports tkinter at module
scope and is compiled into the APK. It is never imported, so it costs a few KB
and nothing else. `engines/base.py::atomic_replace` preserves the destination's
mtime; on Android the destination is always a fresh copy so this is benign, but
it would matter if a future caller ever passed a user file directly.

---

## In-app picker and save: driven end to end on device, 2026-09-07

Everything below was driven over adb on a Pixel 9 Pro XL, Android 17 (SDK 37),
three-button navigation, against the build whose installed APK hashes equal to
the local `app-debug.apk`. Each step was read back from `uiautomator dump`
rather than assumed, and every output was verified by searching the SAVED bytes
for a sentinel planted before the file was pushed.

### Single JPEG, pick -> clean -> save

Input built with a sentinel in Artist, Copyright, Model and ImageDescription
plus GPS, then pushed and scanned into MediaStore.

    picked via ACTION_OPEN_DOCUMENT   "1 file selected: metascrub-devicetest.jpg"
    cleaned                            18108 -> 17730 bytes
                                       removed: APP1 'Exif', 378 bytes
                                       structural scan: every byte accounted for
    saved via ACTION_CREATE_DOCUMENT   suggested name metascrub-01.jpg, editable

    sentinel in the saved bytes        0   (4 in the input)
    exiftool on the saved copy         JFIFVersion, ResolutionUnit, X/YResolution
                                       and nothing else
    original on the device afterwards  byte-identical, same sha256

17730 is exactly the size of that JPEG before the EXIF was written into it.

### Three JPEGs, multi-select -> batch clean -> folder save

    picked                             "3 selected" in the system picker,
                                       app showed "3 files selected"
    cleaned                            9649->9293, 8729->8373, 9817->9461
                                       each removed one APP1 'Exif', 356 bytes
    saved via OPEN_DOCUMENT_TREE       metascrub-01.jpg .. metascrub-03.jpg

    sentinels in the saved bytes       0, 0, 0   (4 each in the inputs)
    identifying tags per exiftool      0, 0, 0
    originals afterwards               all three byte-identical
    cross-contamination check          output 1 contains neither sentinel 2 nor 3

The SEND button was correctly absent for a batch: a chooser takes one stream.

### MP4, the in-place ISO BMFF path

This is the interesting one, because ffmpeg cannot run here and the device
diagnostics report it absent. The pure-Python isobmff engine did the work.

    cleaned                            16349 -> 16349 bytes, LENGTH PRESERVED
    removed                            udta box at 16100 (249 bytes)
                                       compressorname in the avc1 sample entry
                                       hdlr name 'VideoHandler'
                                       an x264 SEI user-data NAL, 687 bytes,
                                       overwritten with SEI filler
    structural scan                    every byte accounted for

    sentinel in the saved bytes        0   (3 in the input)
    'x264 - core' banner remaining     0
    Lavf/Lavc encoder strings          0
    'VideoHandler' remaining           0
    exiftool on the saved copy         HandlerType 'Video Track' and
                                       CompressorID 'avc1', both structural
    still decodable                    h264 640x480, 30 frames, 2.000s,
                                       `ffmpeg -f null -` reports no errors
    original afterwards                byte-identical

Length preservation is the point of the in-place design: `stco`/`co64` sample
offsets never move, so the file stays valid without a remux.

### What this does NOT cover

WebP and HEIC were still not pushed through the app. The engines report ready in
the device diagnostics, but no claim is made beyond that.

## Open work on the Android side

- The `content://media/...` grant path from a real gallery share is still
  unverified from a HARNESS: `am start --grant-read-uri-permission` cannot
  delegate a MediaStore grant it does not own, and the exact failure is
  `SecurityException: ... has no access to content://media/external/images/media/N`,
  measured 2026-09-07. The in-app picker path IS verified end to end, and a
  human sharing from the gallery has confirmed the app works, so what remains
  unverified is the automated harness route, not the feature.
- Audio removal is off, matching the desktop default. Section 1.2 argues it
  should be a prominent toggle, which is UI work this shell does not have. The
  honest state today is the documented default, not a silently different one.
- The cleaned copy is left in `getExternalFilesDir("scrubbed")` after the onward
  share. It is app-private and removed on uninstall, and it is what makes the
  adb verification chain possible without root, but a shipping build should offer
  to delete it once the hand-off completes.
- Only debug builds have been produced. There is no signing config anywhere in
  the tree, by design.
- WebP was not exercised end to end on device. The engine reports `ready` in the
  device diagnostics and the dispatcher recognises `RIFF....WEBP`, but no WebP
  file was pushed and pulled, so no claim is made about it beyond that.
