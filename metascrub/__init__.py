"""
metascrub: remove metadata from documents, images, audio and video, and prove
the removal actually happened.

Built on the ExifSanitizer from the parent project (cli/sanitizer.py), extended
from eight image extensions to five format families across four engines, with
verification that does not trust the engine that performed the write.
"""

from .capabilities import (
    CAPABILITIES, DEFERRED, Completeness, Container, Engine, FormatSpec,
    deferral_for, is_supported, spec_for, supported_extensions,
)
from .scrubber import (
    ExifSanitizer, MetadataScrubber,
    STATUS_CLEAN, STATUS_DEFERRED, STATUS_ERROR, STATUS_SANITIZED, STATUS_UNSUPPORTED,
)
from .verify import Verdict, Verification

__version__ = "1.0.2"

__all__ = [
    "MetadataScrubber", "ExifSanitizer",
    "CAPABILITIES", "DEFERRED", "Completeness", "Container", "Engine", "FormatSpec",
    "spec_for", "is_supported", "deferral_for", "supported_extensions",
    "Verdict", "Verification",
    "STATUS_SANITIZED", "STATUS_CLEAN", "STATUS_UNSUPPORTED",
    "STATUS_DEFERRED", "STATUS_ERROR",
    "__version__",
]
