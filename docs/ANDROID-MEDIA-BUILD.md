# Android media build: scope, architecture, and what it can honestly claim

Status: design, not started. Written 2026-09-06.
Changes nothing in the desktop tool.

## Why this document exists

metascrub on the desktop covers 71 formats and proves every removal by
searching the output bytes for values an exiftool baseline read captured
first. Porting that to Android means porting exiftool, which is a Perl
program with no library form, plus ffmpeg. That is a toolchain project
measured in weeks and it produces a large APK.

This document scopes a different build: the formats a phone actually
produces and receives, scrubbed by container surgery in pure Python, with
no exiftool and no ffmpeg on the device.

The driving use case: strip photos and videos on the device before posting
them, without handing the file to a third-party service that may retain it.
That is the case desktop metascrub cannot serve, and it is the only reason
this document exists.

## The scope decision

A phone does not produce `.cr2`, `.nef`, `.flac` or `.odt`. It produces and
receives a small set:

| Container family | Extensions | Parser cost |
|---|---|---|
| JPEG marker stream | `.jpg` `.jpeg` | small |
| PNG chunk stream | `.png` (screenshots) | small |
| RIFF | `.webp` | small |
| ISO base media box tree | `.heic` `.heif` `.avif` `.mp4` `.mov` `.3gp` | the real work |

Four parsers, not 52 format handlers. None of them needs exiftool or
ffmpeg, because in all four families every metadata carrier is a removable
container structure sitting alongside the compressed data rather than
inside it. Nothing is re-encoded. There is no quality loss and no
transcode, which also means it is fast enough to run inside the Android
share sheet.

`metascrub/engines/av_engine.py` already contains `ftyp_brand()`, which
reads the ISO base media file type box. That is the seed of the box walker.

Out of scope for this build: WebM/Matroska, and every desktop format. They
are not removed from the project; they are simply not in the Android build.

---

## Part 1: what gets removed

### 1.1 The tiers

Not every identifier in a photo is the same kind of thing, and the tool
must not present them as if they were.

| Tier | Identifier | Removable? | Cost | Provable by this tool? |
|---|---|---|---|---|
| 1 | Container metadata (EXIF, XMP, IPTC, maker notes, `udta`, `ilst`) | Yes, fully | none | Yes, structurally |
| 1 | Embedded thumbnails and preview images | Yes, fully | none | Yes, structurally |
| 1 | C2PA / JUMBF content credentials | Yes, fully | none | Yes, structurally |
| 1 | Motion photo / Live Photo video trailers | Yes, fully | none | Yes, structurally |
| 2 | Filename and output file timestamps | Yes, fully | none | Yes, trivially |
| 2 | Audio track in a video | Yes, fully | loses audio | Yes, structurally |
| 3 | Video encoder identification (SEI user data) | Yes, losslessly | none | Yes, structurally |
| 4 | JPEG quantization table fingerprint | Only by re-encoding | generation loss | Partially |
| 5 | Sensor pattern noise (PRNU) | Only degraded, never proven removed | visible quality loss | **No** |
| 6 | Visible content: faces, reflections, signs, screens | No | n/a | **No** |

Tiers 1 through 3 are this build. Tiers 4 and 5 are a separate, opt-in,
differently-labelled feature. Tier 6 is not this tool's problem and the UI
should say so once rather than imply otherwise by silence.

### 1.2 The things people forget, which are the ones that leak

**The filename.** `PXL_20260906_143022891.jpg` carries the capture
timestamp to the millisecond and identifies the device family.
`Screenshot_2026-09-06-14-30-22_instagram.png` does the same and also names
the app. This is not in the file, so no byte scan of the file will ever
catch it, and it is the cheapest fix in this document: write the output
under a neutral name. Do it by default.

**Embedded thumbnails show the pre-edit image.** An EXIF thumbnail is
generated at capture. If someone crops a face or a document out of a photo
in an editor that does not regenerate the thumbnail, the original framing
survives inside APP1. This is handled for free by removing APP1 wholesale,
and it is the reason removal must be wholesale rather than tag-by-tag.

**Motion photos hide an entire MP4 with its own GPS.** Detailed in 2.1.
This is the highest-severity trap in the build.

**Audio in a video geolocates it.** Traffic noise, accents, a television in
the background, and mains hum. Electrical Network Frequency analysis can
place and time a recording from the power grid signature in the audio track
alone. In a container-surgery model, dropping the audio track is nearly
free. It should be a prominent toggle, not a buried setting.

**Video encoders sign their work.** x264 and x265 write a user-data
unregistered SEI NAL containing the encoder version and the full settings
string. Hardware phone encoders often do not, but anything that passed
through a desktop editor may. These NALs are droppable without re-encoding.

### 1.3 What cannot honestly be claimed

**PRNU.** Photo Response Non-Uniformity is a multiplicative noise pattern
arising from manufacturing variation between individual sensor photosites.
It is unique to a specific physical camera, it lives in the pixel values
themselves, and it survives metadata stripping and moderate recompression.
It is the established technique for tying a photo to a device.

It can be *degraded*: cropping a margin off each edge breaks the pixel
alignment that correlation-based matching depends on, and downscaling plus
recompression suppresses the high-frequency residual it lives in. It cannot
be *removed* by this tool in any provable sense, because proving removal
would require estimating this camera's fingerprint from a set of reference
images the tool does not have.

So PRNU mitigation, if built at all, must never share a verdict word with
tier 1 removals. See Part 4.

**Visible content.** The tool cannot see. A photo with every byte of
metadata gone still shows the view from the window.

---

## Part 2: per-container removal tables

Every table is a **whitelist**. Keep the named structures, remove
everything else including unknown structures. A blocklist is wrong here: it
silently passes the carrier nobody has thought of yet, and new carriers do
appear (C2PA did).

### 2.1 JPEG

Keep only these markers:

| Marker | Why kept |
|---|---|
| `SOI` `EOI` | frame the file |
| `APP0` **only when the identifier is exactly `JFIF\0`** | structural, expected by older decoders |
| `APP14` (Adobe) | declares the colour transform, but see the correction below |
| `DQT` `DHT` `SOF*` `SOS` `DRI` `RST*` | required to decode |

**Corrected 2026-09-06 by measurement. Keying the keep-list on the marker
number alone leaks.**

`APP0` is not only JFIF. An `APP0` segment with the identifier `JFXX`
carries a complete embedded JPEG thumbnail: measured at 675 bytes with an
intact sentinel, reported by exiftool as `[JFIF] ThumbnailImage`. So the
rule is the identifier string, not the marker number. (The shipped desktop
tool does remove this correctly; this is a trap for the planned in-house
walker only.)

`APP1` is not one segment either. An oversized XMP payload produced
**three** `APP1` segments carrying three different identifiers: `Exif\0\0`,
`http://ns.adobe.com/xap/1.0/\0`, and `http://ns.adobe.com/xmp/extension/\0`
with the last repeated. A walker that finds "the" APP1 and stops will leave
the rest in place.

`APP14` matters less than stated. Measured: it changes decoding only for
YCCK, or for a 3-component image with no JFIF present, because libjpeg lets
JFIF win when both are there. It is a no-op for `transform=0` CMYK, and
ffmpeg ignores it entirely.

Remove every other marker, specifically:

| Marker | Carries |
|---|---|
| `APP1` | EXIF (including GPS and the embedded thumbnail), XMP, ExtendedXMP |
| `APP2` | ICC profile, MPF (Multi-Picture Format, used by burst and motion photos) |
| `APP3` `APP4` `APP5` | Meta/Kodak and vendor blocks |
| `APP11` | JUMBF, which is where C2PA content credentials live |
| `APP12` | Ducky, Picture Info |
| `APP13` | Photoshop image resource blocks, which is where IPTC lives |
| `COM` | free text comment |

**ICC is a deliberate judgement call.** Removing it makes a Display P3 photo
render with wrong colour on a naive viewer. Keeping an arbitrary ICC
profile risks keeping a custom, identifying one. The proposed rule was: keep
`APP2`/ICC only when the profile bytes hash to a known standard profile
(sRGB, Display P3, Adobe RGB); otherwise remove it.

**That rule as written does not work, measured 2026-09-06.** Two sRGB
profiles generated 1.2 seconds apart differ at exactly one byte, offset 35,
which is the seconds field in the ICC header. A raw byte hash therefore
rejects a profile it should accept. If this rule is kept, the header must be
normalised (zero the creation timestamp and the profile ID) before hashing.
The simpler alternative, and the current recommendation, is to remove ICC
unconditionally and accept sRGB rendering, because a colour shift is visible
and recoverable while a custom profile is neither.

**Everything after `EOI` is removed.** This is the motion photo trailer.

Google Motion Photos and Samsung Motion Photos append a complete MP4 after
the JPEG end-of-image marker. That MP4 carries its own GPS location atom.
The file still opens correctly in every viewer, and the photo ships with its
location intact inside a video nobody looked at. The XMP in `APP1` carries a
`GContainer` directory pointing at the offset, so removing `APP1` without
removing the trailer also leaves an orphaned payload.

**Two corrections, measured 2026-09-06 against synthetic Google-style and
Samsung-style fixtures.**

First, the wording above was wrong about which walker leaks. A walker that
genuinely "parses to `EOI` and stops" is the *safe* variant: it drops the
trailer as a side effect. The leaking shape is the common one, which parses
the headers and then copies the remainder of the file from `SOS` onward.
That is the implementation to guard against.

Second, and worse: **exiftool never reports the trailer video's GPS at any
verbosity.** Zero mentions across `-a -G1 -s`, `-ee3` and `-ee3 -U`, on both
fixtures. It names the blob (`MotionPhotoVideo`, `EmbeddedVideoFile`) and
stops. So the residual scan can never make a needle from it, and neither can
the exiftool oracle in Part 3.3. **The test must be a structural assertion:
zero bytes after the first top-level `EOI`.** The sentinel search is the
belt, not the gate.

An asymmetry that cuts against intuition: removing `APP1` orphans the Google
trailer, but leaves the Samsung one fully self-describing, because a Samsung
SEF trailer is discovered backward from the last six bytes of the file
rather than from a pointer in the XMP.

The shipped desktop tool is not affected: it scrubs JPEG through exiftool,
and `exiftool -all=` removes all post-EOI data including an unrecognised
trailer (measured: three fixtures all reduced to 7153 bytes with zero bytes
after `EOI`). This fixture is a regression guard for the in-house walker.

This is the same class of failure as trap 8 in CLAUDE.md, where the
extension and the content disagreed and the sentinel search, ffprobe and
the verifier all still passed. It gets a dedicated fixture and a
permanently-on test.

### 2.2 PNG

Keep: `IHDR` `PLTE` `IDAT` `IEND` `tRNS` `sRGB` `gAMA` `cHRM` `sBIT`
`bKGD` `pHYs` `acTL` `fcTL` `fdAT`.

Remove everything else, including any unknown ancillary chunk, specifically
`tEXt` `zTXt` `iTXt` (XMP lives here) `eXIf` `tIME` `dSIG` `caBX` (C2PA).

**`iCCP` was missing from both lists above and must be decided explicitly.**
It is the PNG ICC profile chunk and it is the direct analogue of the JPEG
`APP2` case. Same recommendation: remove unconditionally.

**`zTXt` is zlib-compressed, and that defeats the residual byte scan by
construction.** Measured 2026-09-06: a PNG carrying a sentinel inside a
`zTXt` chunk does not contain that sentinel anywhere in its bytes, so
searching for it returns nothing even before any scrubbing happens. The
shipped tool removes the chunk correctly, so this is a blind spot rather
than a leak, but it means **no compressed carrier in any format can ever be
verified by needle search.** Structural assertion is the only instrument
that reaches it. `iTXt` has a compressed mode with the same property.

**Trailing data after `IEND` needs its own rule**, as the JPEG post-`EOI`
case does. Measured: exiftool does emit a `[minor]` warning here, unlike the
JPEG and WebP cases, and Pillow does not care.

### 2.3 WebP (RIFF)

Keep: `VP8 ` `VP8L` `ALPH` `ANIM` `ANMF`.
Remove: `EXIF` `XMP ` `ICCP`, **any unknown chunk**, and `VP8X` itself where
possible. See below: all three claims in the original version of this
section were wrong.

**Correction 1: the VP8X trap is pointed the wrong way.** The original text
warned that removing a chunk while leaving its `VP8X` flag bit set would
confuse decoders. Measured 2026-09-06 across four variants: it breaks
nothing. Pillow/libwebp, exiftool 13.29 and ffmpeg all produced
byte-identical pixels with no warning.

The dangerous direction is the opposite one, and it is a fail-open. With the
`VP8X` flag byte cleared to `0x00` but the `ICCP`/`EXIF`/`XMP` chunks still
present, **Pillow reports no `exif`, no `xmp` and no `icc_profile` while
exiftool reads all three out of the same bytes.** Independently reproduced:
Pillow's `info` keys were `['background', 'loop']` while exiftool returned a
full ICC profile. Any verifier that trusts a flag byte over the actual chunk
inventory is reading a field an attacker controls.

**Correction 2: the proposed assertion produces false positives.** Part 3.2
said to assert that the `VP8X` flags agree with the chunks present. That
fails on a valid Pillow-written animated WebP, where the alpha bit is set
but alpha lives inside `ANMF` with no top-level `ALPH` chunk.

**Correction 3, and this is the better rule: a WebP with no `VP8X` cannot
carry metadata at all.** Dropping `VP8X` outright is strictly stronger than
reconciling its flags, and it removes the whole class of problem rather than
policing it. Prefer it wherever the file does not need `VP8X` for animation
or alpha.

**The unknown-chunk clause is load-bearing, and it was unimplemented until
1.0.2.** Measured against 1.0.1: a WebP carrying an 8 KB MP4 in an unknown
`MPVD` chunk inside the declared RIFF size was reported
`SANITIZED ... verified clean` while the video and its `ftyp` survived
intact. Fixed by the structural scan in `metascrub/structure.py`; see the
1.0.2 entry in `CHANGELOG.md`.

**Trailing data past the declared RIFF size needs its own rule**, as the
JPEG and PNG cases do. Measured: silent on a normal exiftool read, generic
corruption under `-validate`. The shipped tool fails closed here, which is
correct.

### 2.4 ISO base media (MP4, MOV, HEIC, HEIF, AVIF, 3GP)

Remove, wherever they appear in the box tree:

| Box | Carries |
|---|---|
| `udta` | the GPS location atom (`0xA9` followed by `xyz`, confirmed byte-for-byte as `a9 78 79 7a`, payload ISO 6709 e.g. `+44.5588-072.5778+315.000/`), `0xA9 mak`, `0xA9 mod`, Apple and Samsung proprietary boxes, **and `moov/udta/XMP_`** |
| `meta` and `ilst` **in MP4/MOV only** | iTunes-style tag dictionaries |
| `uuid` | XMP, under the UUID `be7acfcb-97a9-42e8-9c71-999491e3afac` (confirmed); also vendor blocks |
| `iprp/ipco/colr` **only when its colour_type is `prof` or `rICC`** | an embedded ICC profile, which carries `DeviceManufacturer` and a profile id |
| `hdlr` name field | a device writes its product name here; ffmpeg writes `VideoHandler`/`SoundHandler` |
| `CompressorName` and `VendorID` | fixed-offset fields inside the VisualSampleEntry (`avc1`/`hvc1`). Measured surviving in-place surgery: `Lavc61.26.100 libx264` and `FFMP`. The ffmpeg remux engine hid these by rewriting the container; surgery does not, so they must be handled explicitly |

**`colr` correction, measured 2026-09-06.** An earlier version of this table said
to remove `iprp/ipco/colr` unconditionally. That is wrong. A `colr` box whose
colour_type is `nclx` holds colour primaries, transfer characteristics, matrix
coefficients and a range flag: enumerations that cannot hold a string, carry no
identity, and are needed to render the image correctly. Removing it changes the
picture for no privacy gain. Key the decision on the colour_type: `prof` and
`rICC` are embedded ICC profiles and go; `nclx` stays.

**Corrected 2026-09-06. The original two-row table leaks, three ways.**

1. **XMP in QuickTime is `moov/udta/XMP_`, not a top-level `uuid` box.** An
   implementation that only knows the `uuid` form misses it entirely.
2. **exiftool's default GPS write on an MP4 goes to XMP in the `uuid` box and
   writes no `0xA9 xyz` atom at all.** So implementing row 1 without row 3
   leaks the coordinates for any file exiftool has touched. The two carriers
   are alternatives, not a primary and a fallback, and both must be handled.
3. **`colr` in HEIF/AVIF was missing and survives everything the original
   table describes.** It is the ISO BMFF analogue of the JPEG `APP2` and PNG
   `iCCP` cases and gets the same treatment.

**`meta` has two HEADER shapes, not just two meanings, and both occur in one
file.** `moov/udta/meta` is a FullBox, carrying a version and flags word;
`moov/meta` in the Apple Keys layout is not. A walker that assumes either
shape misparses the other, in one measured case into a child box of size
1751411826. This is trap 8 in a new place: behaviour keyed on a name where
the name does not determine the layout. No amount of keep-list discipline
catches it, only reading the header shape.

Zero, do not remove, because they are fixed-position fields inside required
headers: `creation_time` and `modification_time` in `mvhd`, `tkhd` and
`mdhd`. Zero in this format means 1904-01-01, which reads as obviously
scrubbed rather than as a plausible false time. That is the honest choice
and it should stay.

Zero the *contents* of `free` and `skip` boxes rather than removing them.
They can hold orphaned data from a previous edit, and see 2.5 for why they
stay in place.

**The top-level `meta` box in HEIF and AVIF is the file structure, not
metadata.** It holds `iinf`, `iloc`, `iref` and the item table describing
where the actual image data lives. Removing it produces a file with no image
in it. That much stands. In MP4 and MOV, by contrast, `moov/meta` is a
metadata dictionary and is safe to remove.

**But the conclusion drawn from it was wrong, and HEIC is not the hard part.**
The original text said item removal means editing `iinf`, `iloc` and `iref`
consistently, and sequenced HEIC last for that reason. You edit none of the
three, and **HEIC can be sequenced alongside MP4 rather than last.**

**Corrected twice, and the second correction matters.** An intermediate version
of this section said to zero the item payload BYTES that `iloc` points at, and
cited "150 exiftool tags reduced to 68" on "a real iPhone 11 HEIC". Both were
wrong. That file (`DA-1p.heic`) is a genuine HEIC container but NOT device
output: measured, it is 596x842 and carries ZERO EXIF and ZERO XMP tags, where
an iPhone 11 shoots 4032x3024. The figure belonged to someone else's file.

More importantly, **zeroing the payload bytes produces a file this project's own
oracle refuses to certify.** Measured 2026-09-06: exiftool then warns
`Invalid XMP`, which `exif_io` classes as `UNPARSED`, and trap 12 is explicit
that an unparseable read is not evidence of cleanliness. Allowlisting that
warning was proposed and is the wrong fix: it would silence the one signal that
distinguishes a cleaned file from an unreadable one.

**Zero the `iloc` extent LENGTH field instead.** It is length-preserving, it
matches the shape of exiftool's own `-all=` output, and the warning does not
occur. Measured: all eleven Phase 2 outputs read `PARSED` with an empty error,
and nothing was added to any allowlist. Note the consequence for a verifier: a
surviving `Exif` item in `iinf` is therefore NOT evidence of a leak, because a
correctly scrubbed HEIC still has the item with a zero-length extent.

Honour `construction_method`. Method 0 is file-offset and method 1 is
`idat`-relative; a walker that reads the field and discards it will zero the
wrong bytes for method 1. Method 2 (item-relative) is refused.

### 2.5 The offset strategy, which is the difference between working and corrupting

Sample tables (`stco`, and `co64` for large files) hold **absolute file
offsets** into `mdat`. Removing any box that sits before `mdat` shifts
every one of those offsets and silently breaks playback.

**For v1, never change the file length.** Overwrite each removed box in
place with a `free` box of byte-identical size, zero-filled. No offset
moves, no sample table fixups, no corruption class to debug. The output is
the same length as the input and differs only in the regions the engine
declared it would zero.

**Measured and confirmed 2026-09-06. This strategy works.** Tested on five
containers: an MP4 with `moov` after `mdat`, an MP4 with `moov` before
`mdat`, a `+faststart` MP4, a fragmented MP4, a QuickTime `.mov` (brand
`qt  ` preserved), and a 3GP. In every case the length was unchanged, the
decode was clean, the decoded video `framemd5` was **bit identical** to the
original, exiftool reported zero metadata groups, and zero sentinel bytes
survived.

Two conditions attach. `udta` and `uuid` must be zeroed in one operation rather
than separately; an earlier version of this sentence listed `XMP_` as a third,
which reads as a separate top-level carrier and is not: `XMP_` is always a child
of `udta` and goes with it. And every one of these results comes from
ffmpeg, exiftool and libheif on a single desktop machine: a `free` box placed
inside `moov` needs one confirmation on the actual Android decoder before
shipping, which belongs in Phase 4.

The counter-experiment also confirms the premise: removing a pre-`mdat` box
without fixing offsets genuinely destroys playback (ffmpeg exit 69). `co64`
specifically is **NOT MEASURED**, since forcing a greater-than-4 GiB `mdat`
was not practical here. That is acceptable only because v1 moves no offsets
at all; it becomes a blocker the moment compaction is attempted.

**`ffprobe` exiting 0 is not evidence, and the test suite must not treat it
as such.** Measured: ffprobe returned success, with plausible stream and
frame counts, on a file whose every frame was garbage. exiftool likewise
returned exit 0 and `FileType: AVIF` on an AVIF with no `meta` box at all.
Acceptance for any AV fixture is a full decode plus a `framemd5` comparison
against the input, never an exit status.

This also gives the verifier an exceptionally cheap invariant: **output
length equals input length**, and every differing byte falls inside a
declared region.

True compaction, which reclaims the bytes and requires `stco`/`co64`
rewriting, is deferred. See Part 6.

---

## Part 3: verification, and how the circularity problem is solved

### 3.1 The problem

The desktop tool reads a baseline with exiftool and writes with a different
engine, so the search for residual values is genuinely independent. Drop
exiftool and the reader and the writer become the same codebase, which
means the tool would only ever look for what it already knows how to
remove. That is precisely the failure traps 2 and 12 exist to prevent.

### 3.2 Structural assertion, which is stronger than needle search

For these four containers the tool does not have to rely on needle search
alone. It can assert over the output **structure**:

- JPEG: no marker outside the keep-list is present, each kept marker's
  **identifier string** is checked and not just its number, and there are
  zero bytes after the first top-level `EOI`.
- PNG: the set of chunk types present is a subset of the keep-list, and
  there are zero bytes after `IEND`.
- WebP: the set of chunk types present is a subset of the keep-list, there
  are zero bytes past the declared RIFF size, and **no unknown chunk is
  present inside it**. Assert over the actual chunk inventory, never over
  the `VP8X` flag bits, which are an attacker-controlled field (see 2.3).
- ISO base media: no `udta`, no `uuid`, no `ilst` anywhere in the tree; every
  `free` and `skip` payload is all zero; `mvhd`/`tkhd`/`mdhd` times are zero;
  output length equals input length.

  **Corrected 2026-09-06.** This bullet used to say "no `meta` outside
  HEIF/AVIF". That is a routing rule keyed on FILE TYPE, which is precisely
  what trap 8 forbids: the extension does not determine the layout. State it as
  a CONTENT rule instead. A `meta` box that holds both `iinf` and `iloc` is
  structural and must survive, at any depth, in any file; a `meta` box that does
  not is a metadata dictionary and goes.

This does not depend on having read the value first. It is a proof about
what the file can no longer contain, rather than a search for one thing it
used to contain.

**Corrected 2026-09-06: this is not belt-and-braces. For several carriers it
is the only instrument that works at all.** The original text called the
byte search a co-equal check. Three measurements say otherwise:

1. **GPS coordinates can never be needles, and no needle could work.**
   Corrected 2026-09-06 after measurement: an earlier version of this line
   blamed the pure-digits-and-separators filter and called
   `Composite:GPSPosition` a 22-character string. Both were wrong. The string
   is 20 characters, and the digits filter drops zero GPS tags. The real
   mechanisms are `_PSEUDO_GROUPS`, which excludes the whole `Composite`
   group, and `_flatten()`, which returns an empty list for any value that is
   not a string or list: reading exiftool with `-n` returns floats, so
   `EXIF:GPSLatitude` never reaches a filter at all.

   More fundamentally, EXIF stores coordinates as rationals
   (`43/1 39/1 14517/1250`), so the decimal form is not in the bytes and a
   byte search for it cannot succeed regardless of filtering. For a photo
   scrubber this is the single most important value in the file. See
   `docs/D2-GPS-VERIFICATION.md` for the measured analysis and the plan.
2. **Compressed carriers are invisible to a byte search by construction.** A
   sentinel inside a PNG `zTXt` is not present in the file's bytes at all.
3. **Trailers and unknown chunks are invisible to exiftool**, so no needle is
   ever generated for them. See 2.1 and 2.3.

So the ordering is: structural assertion is the gate, and the byte search is
the belt. Keep the byte search, seeded from whatever the pure-Python reader
found, because it is nearly free and it catches value-level leaks the
structural check is not looking for. Do not rely on it alone for anything.

### 3.3 exiftool stays, in CI, as an independent oracle

**exiftool is not shipped to the phone. It is still required to develop and
test this.**

The test suite runs on desktop machines and in CI, where exiftool is
present. Every fixture is scrubbed by the pure-Python engine and then
interrogated by exiftool, which reports what a mature, independent,
25-year-old implementation can still see.

**Two corrections, measured 2026-09-06, and the first one limits what this
oracle is worth.**

**The oracle is blind to the CONTENTS of the carrier this document calls
highest-severity.** A JPEG stripped to the 2.1 keep-list but with the motion
photo trailer deliberately retained still carries a complete MP4 and its
sentinel. The same holds for a WebP with an MP4 past its declared RIFF size.

**Corrected 2026-09-06. An earlier version of this paragraph said exiftool
reports "zero tags and zero warnings" on such a file. That is true of the
Google flavour and false of the Samsung one.** Measured on both fixtures with
the trailer deliberately retained:

| Flavour | `ftyp` still present | What exiftool reports |
|---|---|---|
| Google | yes | nothing beyond the keep-list groups |
| Samsung | yes | `EmbeddedVideoType`, `EmbeddedVideoFile`, `TimeStamp` |

Samsung is visible because a SEF trailer is discovered backward from the last
six bytes of the file and needs no APPn segment to point at it, so removing
`APP1` does not orphan it.

**The half that holds for both is the load-bearing half:** exiftool NAMES the
blob and never says what is inside it. `EmbeddedVideoFile` reports
`(Binary data 8054 bytes)`. The trailer video's own GPS therefore appears in no
reported value, no needle can be made from it, and the oracle cannot tell a
retained trailer from a removed one by content. That is why the gate is the
structural assertion (zero bytes after the first top-level `EOI`) and the
oracle is only the belt.

So the oracle is necessary and not sufficient. **The gate is exiftool plus
the structural assertions from 3.2, never exiftool alone.** An oracle that
is silent on the worst case would otherwise manufacture exactly the false
confidence this project exists to refuse.

**The oracle cannot assert "zero tags".** A correctly scrubbed PNG still
reports `Gamma`, `WhitePointX`, `SignificantBits`, `PixelsPerUnitX` and
`SRGBRendering`, all of which come from chunks the 2.2 keep-list
deliberately keeps. A naive zero-tags rule therefore fails on correct
output. The oracle needs an explicit allowlist of benign residual tags,
structurally the same as the warning allowlist in `exif_io.py` described in
trap 12, and it carries the same danger: every name added to it is a name
never checked again. Each addition needs its own justification and its own
overcorrection test, per trap 11.

That restores the independence the desktop tool gets at runtime, moves it
to build time, and costs the phone nothing. It is the load-bearing idea in
this design: **the phone ships a weaker reader, but no code reaches the
phone that has not been checked against the stronger one.**

CI must therefore fail, not skip, when exiftool is absent.

### 3.4 Fixtures

Fixtures are the first phase of work, not an afterthought, and the desktop
suite almost certainly contains none of these:

- a Google Motion Photo with GPS in the trailer MP4
- a Samsung Motion Photo, whose trailer convention differs
- a HEIC from an iPhone with both Exif and XMP items
- an MP4 from a Pixel with the `udta` GPS atom populated
- a `.mov` from an iPhone, for the QuickTime brand path
- an Android screenshot PNG, with app name and timestamp in the filename
- a WebP with `VP8X` flags set and EXIF present
- an AVIF
- a JPEG carrying a C2PA manifest in `APP11`
- a JPEG whose EXIF thumbnail differs from the full image, proving the
  pre-edit leak

Each embeds a unique sentinel, per the project rule. Real device output,
not synthesised, for at least one of each family.

---

## Part 4: the verdict model

A tool that says CLEAN must mean one thing. This build introduces a class
of operation that cannot be proven, so the word has to fork before the
feature ships, not after.

Proposed, alongside the existing `Completeness`:

- `Assurance.PROVEN`: structural assertion and byte search both pass. Tiers
  1 through 3. The only state that may render as "verified".
- `Assurance.MITIGATED`: a degradation was applied whose effect the tool
  cannot measure. Tiers 4 and 5, meaning PRNU and quantization work. Must
  render with different words, never a different colour alone, per trap 6.
- `Assurance.REFUSED`: the file could not be parsed, per trap 12. An
  unparseable input is never "cleaned".

The rule this encodes is the one from trap 2, applied to a new pair of
states: *two states that must never be confused must never share a
representation.* A mobile "CLEAN" that quietly means less than a desktop
"CLEAN" would be exactly the fail-open bug this project was created after.

**If the assurance split is not built, the PRNU feature does not ship.**

---

## Part 5: sequencing

**Phase 0. Fixtures and the oracle harness.** Collect real device media,
embed sentinels, wire the exiftool cross-check so it fails rather than
skips. Nothing else starts until an engine can be judged.

**Phase 1. JPEG, PNG, WebP.** Pure Python, desktop-first, in this repo,
alongside the existing engines and using the existing `MetadataScrubber`
orchestration. Includes the motion photo trailer and the ICC hash rule.
Deliverable: three new engines and their structural verifiers, running on
the desktop, green against the exiftool oracle.

**Phase 2. ISO base media, MP4 and MOV first.** Box walker built out from
`ftyp_brand()`. In-place zero-fill strategy, no length change. HEIC, HEIF
and AVIF come after, because of the `meta` box distinction in 2.4.

**Phase 3. The other identifiers.** Neutral output filenames, output
timestamp control, optional audio track drop, SEI user-data NAL removal.

**Phase 4. The Android app.** Chaquopy shell, and a share-target activity
so the flow is gallery, Share, metascrub, on to the destination app. Write
a new scrubbed file rather than editing the original in MediaStore: it
sidesteps the write-consent dialog and honours "never destroy the original"
for free, which also means the `<path>.backup` policy is simply not needed
on this platform rather than needing a redesign.

**Phase 5, conditional. Tier 4 and 5 mitigation.** Only after the
`Assurance` split from Part 4 exists.

Phases 1 through 3 are desktop work in this repo, testable with the
existing suite, and useful on their own even if the app is never built.
Phase 4 is the only phase that requires Android tooling. That ordering is
deliberate: it puts every hard correctness problem before the first line of
Kotlin.

---

## Part 6: deferrals

Per the project rule, a deferral names the event that discharges it, and
the discharge is enforced by a test rather than by memory. These are
proposed entries for `DEFERRED` in `capabilities.py`, to be added **in the
commit that begins Phase 1**, not before, since `DEFERRED` is currently
empty and that is a measured state worth keeping true.

**Schema conflict, found 2026-09-06 and not yet resolved.** `DEFERRED` is
`Dict[extension, reason]`. All three gate functions and the README prose
scraper assume extension keys. Four of the five rows below are not
extensions: they are capabilities within a format that is otherwise
supported. Resolve this explicitly before writing any of them, and do not
resolve it by quietly widening the key type, since the gates read it. The
open question is whether a capability-level deferral belongs in `DEFERRED`
at all or needs its own table with its own gate.

| Item | Discharged when |
|---|---|
| HEIC/HEIF/AVIF item-level removal | a HEIC fixture with Exif and XMP items round-trips, opens in the Android decoder, and shows zero tags to the exiftool oracle |
| True ISO base media compaction with `stco`/`co64` fixup | a compacted MP4 fixture plays correctly and its sample table offsets resolve |
| SEI user-data NAL removal | an x264-encoded fixture loses its version string with no re-encode and still decodes |
| WebM / Matroska | a phone in the fleet actually produces one |
| PRNU and quantization mitigation | `Assurance.MITIGATED` exists and the GUI renders it distinctly in text |

---

## Part 7: honest caveats to carry into the README

- **Major social platforms already strip EXIF on upload.** That does not
  make this pointless, and it is worth stating precisely why: the platform
  still *receives* the original, with GPS in it, before stripping it for
  other viewers. Local stripping is what prevents the disclosure. The
  threat model is the platform, not only the audience.
- **Stripping metadata does not anonymise a photo.** PRNU identifies the
  camera and content identifies everything else. Say so plainly rather than
  letting a green CLEAN imply more than it means.
- **HEIC arrives from other people even if this phone shoots JPEG.**
- **Do not use `androidx.ExifInterface` as the writer.** It has no
  strip-everything operation, and using a platform API as both the writer
  and the check reintroduces exactly the circularity Part 3 exists to
  avoid. It is fine as a third independent cross-check in tests.

---

## Part 8: what this build does not do

It does not replace the desktop tool, it does not cover the 52 desktop-only
formats, and it does not put exiftool or ffmpeg on a phone. If full
desktop-parity coverage on Android is ever wanted, that is a separate
toolchain project: cross-compiled Perl and ffmpeg packaged as `lib*.so`
under the Android W^X rules, which is a different document and a much
larger one.

iOS is not addressed here. `fork`/`exec` is unavailable to sandboxed apps,
so the subprocess-based desktop architecture cannot run there in any form.
Most of *this* design would port, because it is pure Python container
surgery with no subprocess anywhere. That is a consequence, not a
commitment, and it should not be claimed until someone has built it.
