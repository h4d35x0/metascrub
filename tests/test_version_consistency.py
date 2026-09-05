"""
The version is declared twice. These must agree.

metascrub/__init__.py carries __version__, and pyproject.toml carries the
distribution version. Nothing enforced that they matched, and on 2026-09-05 the
1.0.1 bump changed one and not the other on the first attempt.

This is the same shape as the two defects that cost the most time on this
project: verify.py and scrubber.py each keeping their own pseudo-group list, and
the README claiming a Python floor its dependencies did not support. Two
declarations of one fact drift, and the drift is silent until a user hits it.
"""
from __future__ import annotations

import os
import re

import metascrub

_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


def _pyproject_version() -> str:
    path = os.path.join(_ROOT, "pyproject.toml")
    with open(path, encoding="utf-8") as fh:
        text = fh.read()
    # Match the [project] version, not a dependency pin.
    m = re.search(r'^version\s*=\s*"([^"]+)"', text, re.M)
    assert m, "no version found in pyproject.toml"
    return m.group(1)


def test_package_version_matches_the_distribution_version():
    assert metascrub.__version__ == _pyproject_version(), (
        "metascrub.__version__ is %r but pyproject.toml declares %r"
        % (metascrub.__version__, _pyproject_version())
    )
