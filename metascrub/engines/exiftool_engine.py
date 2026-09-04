"""
exiftool engine: images and other containers exiftool edits safely in place.

This is the engine inherited more or less directly from the the parent project sanitizer.
The behaviour that changed: `-overwrite_original` is now passed explicitly.
Without it exiftool leaves a `_original` sidecar next to every file it touches,
which is a copy of the unsanitized input. For a tool whose entire purpose is
removing sensitive data, silently dropping a pristine copy of the original next
to the output is the worst possible default.
"""

from __future__ import annotations

import logging
from typing import List, Sequence, Tuple

from .. import exif_io
from .base import BaseEngine, EngineError

logger = logging.getLogger("metascrub.engines.exiftool")
logger.addHandler(logging.NullHandler())

# Tags swept individually when `-all=` leaves them behind.
#
# This exists because of TIFF. exiftool answers "Can't delete IFD0 from TIFF"
# and leaves Artist and Copyright in place, because in a TIFF the EXIF IFD is
# the image structure rather than an attachment to it. Deleting those tags one
# at a time does work.
#
# It is an ALLOWLIST, and that is a safety decision rather than a stylistic one.
# Measured 2026-09-04: `exiftool -ImageWidth= file.tiff` is accepted and
# produces a file Pillow can no longer open. exiftool refuses some structural
# tags ("Sorry, StripOffsets is not writable") but not all of them, so sweeping
# every remaining tag would corrupt images while reporting success. Only tags
# that carry identity or provenance are listed, and none of them is load-bearing
# for decoding the file.
_IDENTITY_TAGS = frozenset({
    # attribution and ownership
    "artist", "copyright", "creator", "author", "by-line", "byline",
    "rights", "owner", "ownername", "cameraownername", "credit", "contact",
    # free text that routinely carries names, paths and notes
    "comment", "usercomment", "xpcomment", "xpauthor", "xpkeywords",
    "xpsubject", "xptitle", "imagedescription", "description", "caption",
    "caption-abstract", "title", "subject", "keywords", "headline",
    "instructions", "source", "documentname", "pagename", "imagehistory",
    "history", "usercomment", "note",
    # hardware and software provenance
    "software", "processingsoftware", "hostcomputer", "make", "model",
    "serialnumber", "cameraserialnumber", "internalserialnumber",
    "lensserialnumber", "bodyserialnumber", "lensmake", "lensmodel",
    "creatortool", "encoder", "writer-editor",
    # timestamps that place a person somewhere at a time
    "datetimeoriginal", "createdate", "modifydate", "datecreated",
    "digitalcreationdate", "digitalcreationtime", "subsectimeoriginal",
    "offsettime", "offsettimeoriginal", "timezoneoffset",
    # location
    "gpslatitude", "gpslongitude", "gpsaltitude", "gpsposition",
    "gpsdatestamp", "gpstimestamp", "gpsdatetime", "gpsprocessingmethod",
    "gpsareainformation", "location", "sublocation", "city", "province-state",
    "country", "country-primarylocationname",
})

# Group-level deletions that are safe on every container exiftool can write.
# These clear whole sidecar blocks that `-all=` sometimes leaves on TIFF.
_SAFE_GROUP_DELETES = ("-gps:all=", "-xmp:all=", "-iptc:all=", "-photoshop:all=")


class ExifToolEngine(BaseEngine):
    name = "exiftool"
    supports_selective = True

    def available(self) -> Tuple[bool, str]:
        if not exif_io.EXIFTOOL_AVAILABLE:
            return False, "pyexiftool is not installed"
        try:
            exif_io.session().helper
        except Exception as exc:
            return False, f"exiftool binary not usable: {exc}"
        return True, ""

    def strip_all(self, path: str) -> List[str]:
        self.require()
        session = exif_io.session()
        try:
            # -all= drops every writable tag group.
            # -overwrite_original suppresses the _original sidecar copy, which
            # would otherwise leave a pristine unsanitized duplicate on disk.
            session.execute("-all=", *_SAFE_GROUP_DELETES, "-overwrite_original", path)
        except Exception as exc:
            raise EngineError(f"exiftool failed to remove metadata: {exc}") from exc

        targeted = ["all metadata"]
        swept = self._sweep_survivors(path)
        if swept:
            targeted.extend(swept)
        return targeted

    def _sweep_survivors(self, path: str) -> List[str]:
        """
        Delete identity tags that survived `-all=`, one at a time.

        Only tags in _IDENTITY_TAGS are touched. A tag that survives and is not
        on that list is left alone and will be reported by verification rather
        than blindly deleted, because deleting an unknown tag can destroy the
        file (see the note on _IDENTITY_TAGS).
        """
        session = exif_io.session()
        read = session.read(path)
        if read.failed:
            # Cannot enumerate survivors without a read. Sweeping nothing is
            # correct here: verification is what decides whether the file is
            # acceptable, and it fails closed on an unreadable file.
            logger.warning("cannot read %s to sweep survivors: %s", path, read.error)
            return []
        remaining = read.metadata

        survivors = []
        for key in remaining:
            group, _, bare = key.partition(":")
            if not bare:
                continue
            if group in ("File", "System", "Composite", "ExifTool", "ZIP"):
                continue
            if bare.lower() in _IDENTITY_TAGS:
                survivors.append(key)

        if not survivors:
            return []

        args = [f"-{key}=" for key in sorted(survivors)]
        try:
            session.execute(*args, "-overwrite_original", path)
        except Exception as exc:
            # A failed sweep is not fatal on its own: verification decides
            # whether the file is acceptable, and it will fail this file if the
            # values are still there.
            logger.warning("targeted sweep failed on %s: %s", path, exc)
            return []
        return [f"swept {len(survivors)} residual identity tag(s)"]

    def strip_fields(
        self,
        path: str,
        fields_to_remove: Sequence[str],
        fields_to_sanitize: Sequence[str],
    ) -> Tuple[List[str], List[str]]:
        self.require()
        session = exif_io.session()
        read = session.read(path)
        if read.failed:
            raise EngineError(
                f"cannot read {path} to resolve the requested fields: {read.error}"
            )
        present = read.metadata

        # Match tags with or without their group prefix, so a caller can ask for
        # "Artist" and have it hit "EXIF:Artist". Without this, selective removal
        # silently does nothing whenever the caller omits the group, and reports
        # success, which is the same class of bug as an unverified write.
        def matches(field: str) -> List[str]:
            field_l = field.lower()
            return [
                key for key in present
                if key.lower() == field_l or key.lower().endswith(":" + field_l)
            ]

        removed: List[str] = []
        for field in fields_to_remove:
            if not matches(field):
                continue
            try:
                session.execute(f"-{field}=", "-overwrite_original", path)
                removed.append(field)
            except Exception as exc:
                logger.error("failed to remove field %s: %s", field, exc)

        sanitized: List[str] = []
        for field in fields_to_sanitize:
            if not matches(field):
                continue
            try:
                if "date" in field.lower() or "time" in field.lower():
                    # Keep the field shape but neutralise the value, so software
                    # that requires the tag to exist does not break.
                    session.execute(
                        f"-{field}=1970:01:01 00:00:00", "-overwrite_original", path
                    )
                else:
                    session.execute(f"-{field}=", "-overwrite_original", path)
                sanitized.append(field)
            except Exception as exc:
                logger.error("failed to sanitize field %s: %s", field, exc)

        return removed, sanitized
