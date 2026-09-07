"""
The routing of .heic, .heif and .avif from Engine.EXIFTOOL to Engine.ISOBMFF.

WHY THIS FILE EXISTS

exiftool cannot remove a HEIF ICC profile. In HEIF the ICC lives in
iprp/ipco/colr, which is an item PROPERTY rather than a metadata item, and
exiftool does not rewrite the property structure. Measured 2026-09-07 with
exiftool 13.29 through the full MetadataScrubber pipeline on real iPhone HEICs:
status=error, verdict=residual_found, and the Apple Display P3 strings
'Copyright Apple Inc., 2017' and 'Display P3' still in the output bytes.

Routing the family to the ISO base media engine fixes that. This is a change to
shipped behaviour for a whole format family, so the validation is the
deliverable and not the one-line table change. Every assertion here searches
bytes, structures or an independent oracle. None of them asks an engine whether
it succeeded.

WHAT EACH TEST IS FOR

  the table            the three rows route to Engine.ISOBMFF and say
                       COMPLETE, and the note names the vendor box.
  the mutation         a real Apple HEIC must come back sanitized and clean.
                       Reverting the routing to EXIFTOOL fails this and it does
                       not read the table to notice, so it catches a revert that
                       is disguised as anything at all.
  the picture          the file length and the coded picture bytes are unchanged.
  the oracle           ORACLE_ALLOW_HEIC below, plus the overcorrection test that
                       proves the allowlist has not blinded it.
  the COMPLETE claim   the Samsung `sefd` box, pinned REMOVED, plus the proof
                       that the residual scan still cannot see its carrier, so
                       the clean verdict is earned by the engine and never by
                       the scan's blindness.
  the inventory        every top-level and moov-level box type in the corpus is
                       one isobmff_engine.DECIDED_TYPES has an opinion about.
                       `sefd` was not a broken rule, it was a box nothing had a
                       rule for, and an undecided type is the same defect one
                       name over.
  fail closed          a truncated file is refused and left untouched.

REAL DEVICE MEDIA, AND HOW THIS FILE FINDS IT

The corpus lives OUTSIDE the repository and is not copied into it. Tests that
need it skip when it is absent, which is what happens on any other machine and
in CI. The synthetic AVIF tests run everywhere ffmpeg or Pillow can encode one,
so the routing is never completely untested.
"""

from __future__ import annotations

import os
import shutil
import subprocess

import pytest

from conftest import (
    HAVE_FFMPEG,
    assert_oracle_sees_nothing,
    exiftool_or_fail,
    oracle_tags,
    raw_contains,
    sentinel,
)
from metascrub import (
    STATUS_ERROR,
    STATUS_SANITIZED,
    Completeness,
    MetadataScrubber,
    spec_for,
)
from metascrub import isobmff
from metascrub.capabilities import CAPABILITIES, Container, Engine
from metascrub.engines import get_engine, isobmff_engine

ROUTED_EXTENSIONS = (".heic", ".heif", ".avif")


# --------------------------------------------------------------- device media
#
# Same shape as conftest.MOBILE_HEIC_SOURCES: an environment variable first so
# another machine can point at its own copy, then the path on this one. A
# missing corpus SKIPS. It never silently passes, because a test that passes on
# no input is the quietest possible way to assert nothing.

DEVICE_MEDIA_DIRS = (
    os.environ.get("METASCRUB_DEVICE_MEDIA", ""),
    # The device corpus is real phone media and lives OUTSIDE this repository:
    # it is never committed, and its path is a fleet path that must not be
    # published. Point METASCRUB_DEVICE_CORPUS at it to run these checks;
    # without it they skip, and the synthetic fixtures still run everywhere.
    os.environ.get("METASCRUB_DEVICE_CORPUS", ""),
)

# The three Apple files. Each carries the Apple Display P3 ICC profile in
# iprp/ipco/colr, which is the carrier exiftool cannot remove. Measured
# 2026-09-07: all three came back status=error under the shipped routing.
APPLE_HEICS = (
    "IMG_1034.heic",             # iPhone 8. Exif item, no XMP item.
    "Issue 263 dotnet.heic",     # iPhone XR. Exif item AND an XMP mime item.
    "exif-at-eof.heic",          # iPhone 11 Pro. Exif payload ends at the last
                                 # byte of the file, so an implementation that
                                 # truncates rather than zeroing breaks here and
                                 # passes on the other two.
)

# The non-Apple HEIC: Galaxy S10+, no colr at all, `meta` at the tail of the
# file, and the Samsung `sefd` box. That box shipped as a measured leak for one
# day and is now removed; see test_the_samsung_sefd_box_is_removed.
SAMSUNG_HEIC = "Issue 487.heic"

# A real AVIF carrying a real Exif item and a real XMP item. Its colr is `nclx`,
# four numbers rather than an ICC profile, which is the other colr case.
REAL_AVIF = "Issue 649.avif"

# The two ICC strings the shipped exiftool routing left in the output bytes.
# These are the defect, quoted exactly.
APPLE_ICC_STRINGS = ("Copyright Apple Inc., 2017", "Display P3")

# The raw Samsung carrier markers, as they are actually stored. NOT the strings
# exiftool prints: see test_the_residual_scan_is_blind_to_the_sefd_carrier.
# `SEFH` and `SEFT` are the Samsung Extended Format head and tail markers that
# sit in the same box, included so the assertion covers the whole box and not
# only the two fields exiftool happens to decode.
SEFD_MARKERS = (b"sefd", b"Image_UTC_Data", b"MCC_Data", b"SEFH", b"SEFT")

# The two tags exiftool read out of `sefd` before it was removed: a capture
# wall-clock time carrying the phone's local UTC offset, and a mobile country
# code. Named so the removal test asserts their ABSENCE by name rather than
# asserting that some unspecified set shrank.
SEFD_ORACLE_TAGS = ("MakerNotes:TimeStamp", "MakerNotes:MCCData")


def device_media_dir():
    for candidate in DEVICE_MEDIA_DIRS:
        if candidate and os.path.isdir(candidate):
            return candidate
    return None


def device_file(name, tmp_path):
    """A private COPY of a corpus file. The corpus itself is never written to."""
    directory = device_media_dir()
    if directory is None:
        pytest.skip(
            "no real device media directory; set METASCRUB_DEVICE_MEDIA to a "
            "directory holding the sourced corpus")
    source = os.path.join(directory, name)
    if not os.path.isfile(source):
        pytest.skip(f"{name} is not in the device media directory")
    target = str(tmp_path / name.replace(" ", "_"))
    shutil.copyfile(source, target)
    return target


def scrub(path):
    """The FULL pipeline, never an engine directly. Routing is what is on trial."""
    with MetadataScrubber(backup=False) as scrubber:
        return scrubber.sanitize_file(path, remove_all=True)


def read(path):
    with open(path, "rb") as handle:
        return handle.read()


def picture_bytes(path):
    """
    The coded picture, addressed through the file's OWN item table.

    A decoder is not a usable oracle for every real HEIC: measured 2026-09-07,
    ffmpeg 2024-12-11 cannot decode exif-at-eof.heic BEFORE any scrub, so
    framemd5 would compare one failure against another and call it a match. The
    item table is in the file, so this works on every file the engine accepts,
    and it is parsed from each copy independently rather than assuming offsets
    did not move.
    """
    from metascrub import isobmff

    data = read(path)
    metas = isobmff.item_metas(isobmff.parse(data))
    assert metas, f"{path} has no structural meta box"
    items, _ = isobmff.item_table(data, metas[0])
    picture_types = {b"hvc1", b"av01", b"grid", b"iovl", b"jpeg", b"avc1"}
    blob = bytearray()
    for item_id in sorted(items):
        item = items[item_id]
        if item.item_type not in picture_types:
            continue
        for offset, length in item.extents:
            blob += data[offset:offset + length]
    assert blob, f"{path} has no image items to compare"
    return bytes(blob)


# ------------------------------------------------------------ synthetic media
#
# Runs anywhere. Not a substitute for the real corpus, and it is not treated as
# one: the ICC profile, the maker notes and the Samsung box are all things only
# a real device writes. What this covers is that the routing works at all on a
# machine with no corpus, which is every machine except this one.


def synthetic_avif(tmp_path, name="synthetic.avif"):
    target = str(tmp_path / name)
    try:
        from PIL import Image

        Image.new("RGB", (48, 48), (10, 90, 150)).save(target, format="AVIF")
    except Exception:
        if not HAVE_FFMPEG:
            pytest.skip("no AVIF encoder: Pillow cannot write AVIF and ffmpeg "
                        "is absent")
        proc = subprocess.run(
            ["ffmpeg", "-y", "-loglevel", "error", "-f", "lavfi",
             "-i", "color=c=red:s=64x64:d=1", "-frames:v", "1", "-f", "avif",
             target], capture_output=True)
        if proc.returncode != 0 or not os.path.isfile(target):
            pytest.skip("this ffmpeg build cannot write AVIF")
    return target


# ------------------------------------------------------------------ the table


def test_the_family_routes_to_the_isobmff_engine():
    """
    The routing change itself.

    Kept separate from the behaviour tests on purpose. This one says what the
    table claims; the behaviour tests below say what the bytes do. If only this
    one fails, somebody edited the table. If only the behaviour tests fail, the
    engine regressed. Collapsing them would lose that distinction.
    """
    for ext in ROUTED_EXTENSIONS:
        spec = CAPABILITIES[ext]
        assert spec.engine is Engine.ISOBMFF, (
            f"{ext} routes to {spec.engine.value}. exiftool cannot remove a "
            "HEIF ICC profile: see the ISOBMFF block in capabilities.py.")


def test_the_rows_are_complete_and_the_note_names_the_vendor_box():
    """
    COMPLETE is the decision, and it is a claim about a measured diff.

    These rows shipped PARTIAL for one day, for one reason: the Samsung `sefd`
    box survived. It is removed now, and MEASURED 2026-09-07 across all five
    real-device stills, the set of tags that survive the ISOBMFF routing but
    not the exiftool routing it replaced is EMPTY on every file. Every other
    PARTIAL row in capabilities.py names a carrier measured to survive; this
    family names none, so PARTIAL would mean "we are nervous" rather than "this
    specific thing survives".

    COMPLETE is not free and that is the point of choosing it: scrubber.py
    downgrades a COMPLETE format to STATUS_ERROR when verification is not
    clean, so this row now fails loudly where PARTIAL would have reported
    sanitized. Given an engine that shipped a silent leak once, forfeiting an
    automatic guard is the wrong trade.

    The note must still name `sefd`. A reader who remembers the leak has to be
    able to see, from the row alone, that it is handled.
    """
    for ext in ROUTED_EXTENSIONS:
        spec = CAPABILITIES[ext]
        assert spec.completeness is Completeness.COMPLETE, (
            f"{ext} claims {spec.completeness.value}. Nothing known survives "
            "this engine on the measured corpus; see "
            "test_the_samsung_sefd_box_is_removed. If something does survive "
            "again, PARTIAL is right and this test is the place to say what.")
        assert "sefd" in spec.note, (
            f"{ext} does not name the Samsung sefd box. It was the one "
            "measured leak in this family and the row is where a reader finds "
            "out it is handled.")
        assert "surviv" not in spec.note, (
            f"{ext} is COMPLETE but its note still describes residue. A "
            "COMPLETE row that says something survives contradicts itself, and "
            "a caller reading the note will not know which half to believe.")


def test_the_rows_agree_with_the_engine_they_name():
    """A row that contradicts its engine misleads every caller that reads it."""
    for ext in ROUTED_EXTENSIONS:
        spec = CAPABILITIES[ext]
        assert spec.container is Container.RAW, (
            f"{ext} must be RAW: HEIF stores its metadata items uncompressed, "
            "and the ICC strings that exposed the defect were found by the raw "
            "residual scan.")
        assert spec.rewrites_container is True, (
            f"{ext} names a non-exiftool engine, which writes a temporary file "
            "and atomically replaces the original.")
        engine = get_engine(spec.engine)
        available, reason = engine.available()
        assert available, (
            f"{ext} routes to an engine that cannot run: {reason}. This engine "
            "is pure Python and has no external dependency, so this failing "
            "means the registry entry is wrong.")


# ------------------------------------------------- the defect, on real devices


@pytest.mark.parametrize("name", APPLE_HEICS)
def test_a_real_apple_heic_is_sanitized_and_verifies_clean(name, tmp_path):
    """
    THE MUTATION TEST.

    Measured 2026-09-07 under the shipped EXIFTOOL routing, all three files:
    status=error, verdict=residual_found, two residual values. Under
    Engine.ISOBMFF: status=sanitized, verdict=verified_clean.

    This does not read CAPABILITIES, so it catches a revert however it is
    spelled, and it fails on the thing a user would actually see rather than on
    a table entry.
    """
    path = device_file(name, tmp_path)
    result = scrub(path)

    assert result["status"] == STATUS_SANITIZED, (
        f"{name}: status={result['status']} error={result.get('error')!r}. "
        "exiftool cannot remove a HEIF ICC profile; this is what the ISOBMFF "
        "routing exists to fix.")
    assert result["verification"]["verdict"] == "verified_clean", (
        f"{name}: {result['verification']}")


@pytest.mark.parametrize("name", APPLE_HEICS)
def test_the_apple_icc_strings_are_gone_from_the_output_bytes(name, tmp_path):
    """
    The defect quoted exactly, searched for in the bytes.

    Not 'the verifier says clean' and not 'exiftool reports no ICC'. Both of
    those are an engine answering a question about itself. These two strings
    were measured present in the output of the shipped routing.
    """
    path = device_file(name, tmp_path)
    for needle in APPLE_ICC_STRINGS:
        assert raw_contains(path, needle), (
            f"fixture precondition failed: {name} does not carry {needle!r} "
            "before the scrub, so its absence afterwards would prove nothing")

    scrub(path)

    survivors = [needle for needle in APPLE_ICC_STRINGS
                 if raw_contains(path, needle)]
    assert not survivors, (
        f"{name} still leaks the Apple ICC profile: {survivors}")
    assert b"acsp" not in read(path), (
        f"{name} still carries an ICC profile: 'acsp' is the profile file "
        "signature at byte 36 of every ICC header, and it survives even when "
        "the descriptive strings do not")


@pytest.mark.parametrize("name", APPLE_HEICS + (SAMSUNG_HEIC, REAL_AVIF))
def test_the_length_and_the_coded_picture_are_unchanged(name, tmp_path):
    """
    The engine's central claim: it free-fills in place, so nothing moves.

    Sample tables and item tables hold ABSOLUTE offsets. An engine that removed
    bytes rather than zeroing them would shift every one of them, and the file
    would still look plausible. Length plus the picture payload is what catches
    that, and neither depends on a decoder being able to open the file.
    """
    path = device_file(name, tmp_path)
    before_size = os.path.getsize(path)
    before_picture = picture_bytes(path)

    scrub(path)

    assert os.path.getsize(path) == before_size, (
        f"{name}: the file length changed, so every absolute offset in it moved")
    assert picture_bytes(path) == before_picture, (
        f"{name}: the coded picture changed. The metadata removal is not "
        "supposed to touch a single byte of image data.")


def test_a_real_avif_is_sanitized_and_verifies_clean(tmp_path):
    """
    AVIF is routed by the same three-row block and is NOT the same case.

    Its colr is `nclx`, four numbers rather than an ICC profile, so the defect
    that forced this change does not appear here at all: measured 2026-09-07,
    Issue 649.avif verified clean under BOTH routings. It is here because the
    routing changed for it too, and a format whose behaviour changed with no
    test is a format nobody is watching.
    """
    path = device_file(REAL_AVIF, tmp_path)
    before = oracle_tags(path)
    assert len(before) > 100, (
        "fixture precondition failed: this AVIF should carry a large Canon EXIF "
        f"payload, and the oracle sees only {len(before)} tags")

    result = scrub(path)

    assert result["status"] == STATUS_SANITIZED, result.get("error")
    assert result["verification"]["verdict"] == "verified_clean"
    assert len(oracle_tags(path)) < 25, (
        "the AVIF still reports more tags than a scrubbed container should")


def test_a_synthetic_avif_loses_its_sentinel(tmp_path):
    """
    The always-on test. No device corpus, no ffmpeg on the critical path if
    Pillow can encode.

    Every other end-to-end test in this file skips on a machine without the
    sourced corpus, which is every machine except this one. This one runs
    wherever an AVIF encoder exists, so a revert of the routing cannot pass CI
    unnoticed merely because the real files are absent.
    """
    path = synthetic_avif(tmp_path)
    value = sentinel("heicrouting")
    exiftool_or_fail().execute(
        f"-Artist={value}", f"-Copyright={value}", f"-Software={value}",
        "-overwrite_original", path)
    assert raw_contains(path, value), (
        "fixture precondition failed: exiftool did not store the sentinel in "
        "the synthetic AVIF")

    assert spec_for(path).engine is Engine.ISOBMFF
    result = scrub(path)

    assert result["status"] == STATUS_SANITIZED, result.get("error")
    assert not raw_contains(path, value), "the sentinel survived the scrub"


# -------------------------------------------------------------- fail closed


def test_a_truncated_heic_is_refused_and_left_untouched(tmp_path):
    """
    The behaviour change users will actually notice, other than the fix.

    The ISO base media engine resolves the whole box tree and refuses anything
    it cannot fully account for, which the exiftool path did not do in the same
    way. Refusing is the safer direction, but a refusal must leave the file
    exactly as it found it: a half-written output is worse than no output,
    because the user has no way to tell which they got.

    Measured 2026-09-07: the shipped exiftool routing ALSO fails this file, with
    a different message. So this is not a newly failing file. It is the shape of
    the new failure, pinned.
    """
    whole = read(device_file(APPLE_HEICS[0], tmp_path))
    path = str(tmp_path / "truncated.heic")
    with open(path, "wb") as handle:
        handle.write(whole[:len(whole) * 6 // 10])
    before = read(path)

    result = scrub(path)

    assert result["status"] == STATUS_ERROR, (
        f"a truncated HEIC was not refused: {result['status']}")
    assert read(path) == before, (
        "the input was modified by a run that failed. A refusal must leave the "
        "directory exactly as it found it.")


# ---------------------------------------------------- the COMPLETE claim, pinned


def test_the_samsung_sefd_box_is_removed(tmp_path):
    """
    THE LEAK THIS FAMILY SHIPPED FOR A DAY, now pinned closed.

    Issue 487.heic is a real Galaxy S10+ HEIC carrying a top-level `sefd` box,
    Samsung Extended Format Data, 106 bytes. The shipped exiftool routing
    removed it; the first cut of the ISOBMFF engine did not, and exiftool still
    read a capture wall-clock time carrying the phone's local UTC offset and a
    mobile country code out of it afterwards, on a run that reported sanitized
    and verified_clean.

    Three independent assertions, on purpose, because each can fail alone:

      the BYTES     every marker of the box is gone from the output. This is
                    the assertion that does not ask any engine anything.
      the ORACLE    exiftool reports neither maker-note tag. A byte search that
                    passed while exiftool still decoded the values would mean
                    the box moved rather than went.
      the LENGTH    unchanged. This engine's whole strategy is length-preserving
                    surgery, and a `sefd` removal that shortened the file would
                    move every offset in the item table.

    The predecessor of this test asserted the box SURVIVED, deliberately, so
    that removing it could not happen without re-deciding the PARTIAL row. That
    is what happened: the row is COMPLETE now, and this is the test that keeps
    it honest in the other direction.
    """
    path = device_file(SAMSUNG_HEIC, tmp_path)
    before = read(path)
    for marker in SEFD_MARKERS:
        assert marker in before, (
            f"fixture precondition failed: {marker!r} is not in {SAMSUNG_HEIC}")
    before_tags = oracle_tags(path)
    for tag in SEFD_ORACLE_TAGS:
        assert tag in before_tags, (
            f"fixture precondition failed: exiftool does not report {tag} on "
            f"{SAMSUNG_HEIC}, so this test would prove nothing by its absence")

    result = scrub(path)
    assert result["status"] == STATUS_SANITIZED, result.get("error")
    assert result["verification"]["verdict"] == "verified_clean", (
        result["verification"])

    after = read(path)
    survivors = [marker for marker in SEFD_MARKERS if marker in after]
    assert survivors == [], (
        "the Samsung sefd box survives this engine again. It carries a capture "
        "wall-clock time with a local UTC offset and a mobile country code, "
        "and the residual scan CANNOT see either of them: see "
        "test_the_residual_scan_is_blind_to_the_sefd_carrier. A run over this "
        f"file will report verified_clean while leaking. Markers left: "
        f"{survivors}")

    leaked = sorted(tag for tag in oracle_tags(path)
                    if tag.startswith("MakerNotes:") or tag.startswith("Samsung:"))
    assert leaked == [], (
        "exiftool still reads maker notes out of the scrubbed Samsung file, so "
        f"a vendor carrier survived somewhere: {leaked}")

    assert len(after) == len(before), (
        "removing sefd changed the file length from %d to %d. This engine "
        "free-fills in place precisely so that no offset in the item table "
        "ever moves." % (len(before), len(after)))


def test_the_residual_scan_is_blind_to_the_sefd_carrier(tmp_path):
    """
    WHY NOTHING CAUGHT THE LEAK, kept after the fix because it is still true.

    The box is gone now, so this file no longer reports a false clean. What has
    NOT changed is the mechanism: if `sefd` came back tomorrow, the residual
    scan would still be blind to both of its values and the verdict would still
    read verified_clean. That is why the removal is pinned by a byte search in
    test_the_samsung_sefd_box_is_removed and not by the verdict, and it is why
    the row above can only claim COMPLETE on the strength of a measured diff.

    Trap 10, again.

    The residual scan searches the output bytes for the values exiftool reported
    BEFORE the scrub. That only works when the reported value is the stored
    value. Here it is not, and it fails in two different ways at once. Measured
    2026-09-07 on Issue 487.heic:

      MakerNotes:TimeStamp comes back as '2020:06:29 ...' but the box stores an
      ASCII Unix millisecond epoch after the literal 'Image_UTC_Data'. The
      rendered form never reaches the needle set anyway: strip ':', '-', '.'
      and ' ' from it and it is all digits, which verify.py:241 drops so that
      timestamps cannot collide with ordinary binary content.

      MakerNotes:MCCData comes back as the INTEGER 466. verify._flatten returns
      nothing at all for a non-string scalar, so it never becomes a needle
      either, and the box stores 'MCC_Data466'.

    Both carriers are therefore invisible to the scan. Only this file knows,
    because only a test can know a sentinel independently of the tool that is
    supposed to find it.
    """
    from metascrub import verify

    path = device_file(SAMSUNG_HEIC, tmp_path)
    before = exiftool_or_fail().read(path)
    assert before.parsed, before.error
    needles = verify.meaningful_values(before.metadata)

    timestamp = str(before.metadata["MakerNotes:TimeStamp"])
    country = before.metadata["MakerNotes:MCCData"]
    assert timestamp not in needles, (
        "the rendered timestamp IS a needle now, so this test no longer "
        "describes the mechanism; re-measure before trusting the note")
    assert not isinstance(country, str), (
        f"MCCData is now a {type(country).__name__}; if it became a string it "
        "may reach the needle set, which would change this analysis")
    assert not any(needle in timestamp for needle in needles), (
        "some needle overlaps the timestamp after all")

    result = scrub(path)

    assert result["verification"]["verdict"] == "verified_clean", (
        "the residual scan now sees something in this file, which it could not "
        "do through the sefd carrier; re-measure before trusting the note")
    assert b"Image_UTC_Data" not in read(path), (
        "the carrier is back, and the verdict above is therefore a FALSE "
        "clean: the scan cannot see either value, as this test just proved. "
        "See test_the_samsung_sefd_box_is_removed.")


# ------------------------------------------------------------- the mutation


def test_mutation_forgetting_the_samsung_vendor_box_is_caught(tmp_path, monkeypatch):
    """
    THE MUTATION: stop removing `sefd` and require that something fails.

    This is the shape the engine actually shipped in for one day, reproduced on
    purpose. It matters more than an ordinary mutation test because of what
    does NOT fail under it: the file still decodes, the length is still
    unchanged, the engine's own structural post-conditions still hold, and the
    residual scan still reports verified_clean. A suite that only asked the
    tool whether it succeeded would be green.

    Two independent guards are asserted to fire, not one:

      the BYTE SEARCH   the carrier is back in the output. Nothing is asked of
                        any engine here.
      the ORACLE        exiftool decodes the two maker-note tags again, and the
                        HEIC allowlist deliberately does not cover them, which
                        is what test_the_allowlist_never_covers_the_samsung_
                        maker_notes exists to keep true.

    MEASURED 2026-09-07 by making the same edit to the source rather than to a
    monkeypatched attribute: five tests fail, and they are
    test_the_samsung_sefd_box_is_removed,
    test_the_residual_scan_is_blind_to_the_sefd_carrier,
    test_the_decision_table_agrees_with_the_rules_that_run,
    test_no_surface_box_type_survives_that_the_table_calls_removed and
    test_the_oracle_sees_nothing_in_a_scrubbed_real_file for the Samsung file.
    """
    path = device_file(SAMSUNG_HEIC, tmp_path)
    monkeypatch.setattr(
        isobmff_engine, "REMOVED_TYPES",
        frozenset(isobmff_engine.REMOVED_TYPES - {b"sefd"}))

    result = scrub(path)
    assert result["status"] == STATUS_SANITIZED, result.get("error")
    assert result["verification"]["verdict"] == "verified_clean", (
        "the mutation was caught by the residual scan, which would mean the "
        "scan can see the sefd carrier after all. Re-measure: this test and "
        "the blindness test above both describe it as invisible.")

    after = read(path)
    assert b"Image_UTC_Data" in after and b"MCC_Data" in after, (
        "the mutation did not change the behaviour, so this test proves "
        "nothing about the assertions below")

    leaked = sorted(tag for tag in oracle_tags(path)
                    if tag in SEFD_ORACLE_TAGS)
    assert leaked == sorted(SEFD_ORACLE_TAGS), (
        f"exiftool no longer decodes the carrier that came back: {leaked}")

    with pytest.raises(AssertionError) as raised:
        assert_oracle_sees_nothing(
            path, allow=ORACLE_ALLOW_HEIC, gated=ORACLE_GATED_HEIC,
            note="sefd mutation, expected to fail")
    assert "MakerNotes:" in str(raised.value), (
        "the mutated file failed the oracle for some reason other than the "
        f"maker notes, so the oracle is not the guard here: {raised.value}")


# ------------------------------------------------------- the sibling inventory
#
# `sefd` was not a broken removal rule. It was a box type nothing in this engine
# had ever formed an opinion about, sitting at the TOP LEVEL of a mass-market
# phone's stills, and every check the tool has passed while it leaked: the file
# decoded, the structural post-conditions held, and the residual scan could not
# see either value it carried. So the durable fix is not the one name. It is
# refusing to have no opinion.
#
# isobmff_engine.DECIDED_TYPES names every box type met at the top level or
# directly under `moov`, with what happens to it. These two tests walk the real
# corpus and fail on a type that is not in it. Whether the right decision for a
# new type is "remove" or "keep" is a judgement; noticing that a judgement is
# owed is not, and that is what is automated here. The synthetic half of the
# same sweep is tests/test_isobmff_engine.py::test_every_box_type_in_the_
# fixtures_is_one_the_engine_has_decided.


def _isobmff_files_in_the_corpus():
    """Every corpus file this engine's parser can actually walk."""
    directory = device_media_dir()
    if directory is None:
        pytest.skip(
            "no real device media directory; set METASCRUB_DEVICE_MEDIA to a "
            "directory holding the sourced corpus")
    found = []
    for name in sorted(os.listdir(directory)):
        if os.path.splitext(name)[1].lower() not in (
                ".heic", ".heif", ".avif", ".mp4", ".mov", ".m4v", ".3gp"):
            continue
        path = os.path.join(directory, name)
        with open(path, "rb") as handle:
            data = handle.read()
        try:
            boxes = isobmff.parse(data)
        except isobmff.IsobmffError:
            # apple-livephoto-quicktime.mov has no ftyp box at all and this
            # parser refuses it, which is the fail-closed behaviour the engine
            # documents. A file the engine will not touch cannot leak through
            # it, so it is out of scope for an inventory of what the engine
            # keeps.
            continue
        found.append((name, boxes))
    assert found, "the corpus directory exists but holds no parseable container"
    return found


def surface_box_types(boxes):
    """Every box type at the top level or directly under `moov`."""
    types = {box.type for box in boxes}
    for box in boxes:
        if box.type == b"moov":
            types |= {child.type for child in box.children}
    return types


def test_every_box_type_in_the_real_corpus_is_one_the_engine_has_decided():
    """
    THE TEST THAT WOULD HAVE CAUGHT `sefd` ON THE DAY THE FILE WAS ADDED.

    Not by knowing what Samsung Extended Format Data is. By noticing that a box
    type had appeared at the top level of a real phone's output that no rule in
    this engine mentioned, and refusing to be quiet about it.

    A failure here is not "the engine is broken". It is "somebody owes a
    decision": look at what the box carries, add it to REMOVED_TYPES or to
    FREE_SPACE_TYPES or record it in DECIDED_TYPES as structural, and say in
    the comment what was measured. Do not add it to DECIDED_TYPES to make this
    green.
    """
    undecided = {}
    for name, boxes in _isobmff_files_in_the_corpus():
        for kind in sorted(surface_box_types(boxes)):
            if kind not in isobmff_engine.DECIDED_TYPES:
                undecided.setdefault(kind.decode("latin-1"), []).append(name)
    assert undecided == {}, (
        "box types this engine has no opinion about, at the top level or under "
        f"moov, in real device output: {undecided}. This is the shape the "
        "Samsung sefd leak had. Decide each one and say what you measured.")


def test_the_decision_table_agrees_with_the_rules_that_run():
    """
    DECIDED_TYPES is documentation and a test oracle; REMOVED_TYPES and
    FREE_SPACE_TYPES are what actually runs. Two structures describing one
    policy is how a policy silently forks, so the agreement is asserted rather
    than maintained by hand.
    """
    table = isobmff_engine.DECIDED_TYPES
    for kind in isobmff_engine.REMOVED_TYPES:
        assert table.get(kind) == isobmff_engine.DECISION_REMOVED, (
            f"{kind!r} is removed by the engine but the table says "
            f"{table.get(kind)!r}")
    for kind in isobmff_engine.FREE_SPACE_TYPES:
        assert table.get(kind) == isobmff_engine.DECISION_ZEROED, (
            f"{kind!r} is zeroed by the engine but the table says "
            f"{table.get(kind)!r}")
    for kind, decision in table.items():
        if decision == isobmff_engine.DECISION_REMOVED:
            assert kind in isobmff_engine.REMOVED_TYPES, (
                f"the table says {kind!r} is removed and nothing removes it")
        if decision == isobmff_engine.DECISION_ZEROED:
            assert kind in isobmff_engine.FREE_SPACE_TYPES, (
                f"the table says {kind!r} is zeroed and nothing zeroes it")
    assert b"sefd" in isobmff_engine.REMOVED_TYPES, (
        "sefd is not removed. It is a Samsung vendor box carrying a capture "
        "time and a country code that the residual scan cannot see.")


def test_no_surface_box_type_survives_that_the_table_calls_removed(tmp_path):
    """
    The table is only worth having if the file agrees with it afterwards.

    Reads the box tree of a REAL scrubbed still with the engine's own parser
    and requires that no type marked `removed` is still there. That is a
    circular check on its own, which is why it is one assertion among several
    and never the evidence: the byte searches above are the evidence.
    """
    removed = {kind for kind, decision in isobmff_engine.DECIDED_TYPES.items()
               if decision == isobmff_engine.DECISION_REMOVED}
    for name in APPLE_HEICS + (REAL_AVIF, SAMSUNG_HEIC):
        path = device_file(name, tmp_path)
        scrub(path)
        with open(path, "rb") as handle:
            boxes = isobmff.parse(handle.read())
        survivors = sorted(
            kind.decode("latin-1") for kind in surface_box_types(boxes) & removed)
        assert survivors == [], f"{name} still carries {survivors}"


# ---------------------------------------------------------------- the oracle
#
# THE ALLOWLIST, RE-MEASURED FOR THIS ENGINE. Not inherited.
#
# conftest.ORACLE_ALLOW_ISOBMFF_REMUX is 42 bare keys plus 9 gated predicates,
# measured against the ffmpeg REMUX engine on VIDEO, and it says in its own
# comment: "Phase 2 must re-measure and shrink this, and must not inherit it
# unexamined." This is that re-measurement, for still images under the in-place
# engine, and it is 34 bare keys plus 1 gated. It is a separate constant rather
# than a widening of that one, because a list built for one container silently
# widening to another is exactly trap 11.
#
# conftest also says, above the same block: "HEIC has NO allowlist here, on
# purpose ... Until an engine can clean it, there is nothing honest to allow."
# The reason it gives is that the ICC survived. That reason is now gone,
# measured, so the list exists. It lives here and not in conftest because
# conftest belongs to another owner in this change.
#
# HOW THE 34 WERE CHOSEN. They are the UNION of every tag exiftool 13.29 still
# reported, through scrubber._real_tags, over eleven scrubbed outputs measured
# 2026-09-07: five real device files, the same bytes under a .heif and an .avif
# name, DA-1p.heic, an exiftool-tagged copy of the Samsung file, and two
# synthetic AVIFs from ffmpeg and Pillow. MINUS the two Samsung maker-note tags,
# which were the leak and stay deliberately OFF the list even now that the box
# is removed. An allowlist entry is a name nobody checks again, so allowing them
# would mean a returning `sefd` passed the oracle in silence; excluded, the
# oracle is the second independent guard behind the byte search in
# test_the_samsung_sefd_box_is_removed. See
# test_the_allowlist_never_covers_the_samsung_maker_notes.
#
# Every name below is a number, a geometry, a byte offset, a box version or a
# four-character code out of a registered set. None of them has a free string
# field and none of them has a wall-clock time field, which is the bar
# conftest's own lists use.
#
# ONE HONEST QUALIFICATION, in the shape ORACLE_ALLOW_PNG_KEEPLIST uses. The
# brands are four arbitrary bytes each, so a crafted file could in principle put
# four characters of anything in MajorBrand and more in CompatibleBrands. What
# they cannot hold is a name, a date or a coordinate, and the engine preserves
# them on purpose because the brand is what tells a reader which flavour of ISO
# base media the file is. Structural assertion over the box inventory is what
# covers that gap, not this list, and tests/test_isobmff_engine.py is where it
# lives.
ORACLE_ALLOW_HEIC = frozenset({
    # ftyp
    "QuickTime:MajorBrand",
    "QuickTime:MinorVersion",
    "QuickTime:CompatibleBrands",
    # the item table's own shape
    "QuickTime:HandlerType",           # 'pict'
    "QuickTime:PrimaryItemReference",  # pitm, an item id
    "QuickTime:MediaDataOffset",       # where mdat starts
    "QuickTime:MediaDataSize",         # how long mdat is
    # geometry, from ispe, irot, pixi, clap and the grid derivation
    "QuickTime:ImageSpatialExtent",
    "QuickTime:ImagePixelDepth",
    "QuickTime:MetaImageSize",
    "QuickTime:CleanAperture",
    "QuickTime:Rotation",
    # hvcC, the HEVC decoder configuration record. Every field is a number or a
    # bit field the decoder needs; there is no string in the record.
    "QuickTime:HEVCConfigurationVersion",
    "QuickTime:GeneralProfileSpace",
    "QuickTime:GeneralTierFlag",
    "QuickTime:GeneralProfileIDC",
    "QuickTime:GenProfileCompatibilityFlags",
    "QuickTime:ConstraintIndicatorFlags",
    "QuickTime:GeneralLevelIDC",
    "QuickTime:MinSpatialSegmentationIDC",
    "QuickTime:ParallelismType",
    "QuickTime:ChromaFormat",
    "QuickTime:BitDepthLuma",
    "QuickTime:BitDepthChroma",
    "QuickTime:AverageFrameRate",
    "QuickTime:ConstantFrameRate",
    "QuickTime:NumTemporalLayers",
    "QuickTime:TemporalIDNested",
    # av1C, the AV1 configuration record, and the nclx colour description.
    "QuickTime:AV1ConfigurationVersion",
    "QuickTime:ChromaSamplePosition",
    "QuickTime:ColorPrimaries",
    "QuickTime:TransferCharacteristics",
    "QuickTime:MatrixCoefficients",
    "QuickTime:VideoFullRangeFlag",
})


def _is_nclx_only(value):
    """
    colr, gated rather than allowed, because its VALUE is the whole point.

    'nclx' is four numbers describing the colour space and carries no identity.
    'prof' and 'rICC' mean the box holds an ICC PROFILE, which is the carrier
    this entire routing change exists to remove: measured 2026-09-07, the Apple
    Display P3 profile in a `prof` colr names a device manufacturer, a device
    model, a profile creation date and a profile ID. Allowing the bare key would
    make the oracle blind to the original defect.
    """
    return str(value).strip() == "nclx"


ORACLE_GATED_HEIC = {
    "QuickTime:ColorProfiles": _is_nclx_only,
}


@pytest.mark.parametrize("name", APPLE_HEICS + (REAL_AVIF, SAMSUNG_HEIC))
def test_the_oracle_sees_nothing_in_a_scrubbed_real_file(name, tmp_path):
    """
    The independent check: exiftool, not this tool, says what is left.

    The Samsung file is in this list now. It was excluded while `sefd` leaked,
    because an allowlist wide enough to make every real file green is the false
    confidence the oracle exists to prevent; the honest move then was to let it
    fail here and pin the failure in its own test. The box is removed, so it
    passes on the same unwidened list.
    """
    path = device_file(name, tmp_path)
    scrub(path)
    assert_oracle_sees_nothing(
        path, allow=ORACLE_ALLOW_HEIC, gated=ORACLE_GATED_HEIC,
        note=f"{name} after the ISOBMFF routing")


def test_the_allowlist_never_covers_the_samsung_maker_notes():
    """
    The leak that was fixed must not be allowed away later.

    Making the Samsung file pass the oracle by removing its carrier is the
    correct fix. Making it pass by adding its tags to the list would look
    identical from a green suite and would hide a returning `sefd` forever.
    Trap 11 in its purest form: every name on an allowlist is a name nobody
    checks again, so the two names that mattered are asserted OFF it.
    """
    both = set(ORACLE_ALLOW_HEIC) | set(ORACLE_GATED_HEIC)
    for tag in SEFD_ORACLE_TAGS:
        assert tag not in both, (
            f"{tag} is on the HEIC oracle list. That tag comes out of the "
            "Samsung sefd box, which this engine removes; allowing it would "
            "make the oracle blind to the box coming back.")
    vendor = sorted(tag for tag in both
                    if tag.startswith("MakerNotes:") or tag.startswith("Samsung:"))
    assert vendor == [], (
        f"the HEIC oracle list allows vendor maker-note tags: {vendor}. No "
        "maker note is structural, so none of them belongs on this list.")


@pytest.mark.parametrize("name", APPLE_HEICS[:1] + (REAL_AVIF,))
def test_the_allowlist_has_not_blinded_the_oracle(name, tmp_path):
    """
    THE OVERCORRECTION TEST, landed in the same change as the allowlist.

    Trap 11: every name on an allowlist is a name nobody checks again. The
    ORACLE_ALLOW_PNG_KEEPLIST precedent is to re-inject real identity into a
    file that passes the list and require the assertion to still FAIL. If it
    passes, the list is wide enough to hide a leak and the list is the bug.
    """
    path = device_file(name, tmp_path)
    scrub(path)
    assert_oracle_sees_nothing(
        path, allow=ORACLE_ALLOW_HEIC, gated=ORACLE_GATED_HEIC,
        note="precondition: a scrubbed file passes the list")

    value = sentinel("heicoracle")
    exiftool_or_fail().execute(
        f"-Artist={value}", f"-Copyright={value}",
        "-DateTimeOriginal=2019:03:04 05:06:07",
        "-overwrite_original", path)

    with pytest.raises(AssertionError):
        assert_oracle_sees_nothing(
            path, allow=ORACLE_ALLOW_HEIC, gated=ORACLE_GATED_HEIC,
            note="identity re-injected; the oracle MUST still see it")


def test_the_gated_colr_predicate_rejects_an_icc_profile():
    """
    The one gated key, exercised on the value that matters.

    A predicate is only worth having if something proves it says no. 'prof' and
    'rICC' are the two colr subtypes that carry an ICC profile, which is the
    exact carrier this routing change exists to remove.
    """
    assert _is_nclx_only("nclx")
    assert not _is_nclx_only("prof")
    assert not _is_nclx_only("rICC")
    assert not _is_nclx_only("")


def test_the_allowlist_is_smaller_than_the_remux_list_it_replaces():
    """
    conftest's ISOBMFF remux list says of itself: "Phase 2 must re-measure and
    shrink this, and must not inherit it unexamined."

    That instruction is only worth anything if something checks it. This is not
    a numerology test: the in-place engine leaves a still image, which has no
    movie header, no track header and no media header, so the six wall-clock
    times and the three free strings that the remux list has to gate do not
    exist here at all. A list that had grown instead would mean this engine
    leaves MORE behind than the one it replaced.
    """
    from conftest import ORACLE_ALLOW_ISOBMFF_REMUX, ORACLE_GATED_ISOBMFF_REMUX

    assert len(ORACLE_ALLOW_HEIC) < len(ORACLE_ALLOW_ISOBMFF_REMUX)
    assert len(ORACLE_GATED_HEIC) < len(ORACLE_GATED_ISOBMFF_REMUX)
    assert not (set(ORACLE_GATED_HEIC) & set(ORACLE_ALLOW_HEIC)), (
        "a key that is both allowed and gated is allowed, and the predicate is "
        "dead code that reads like a control")
