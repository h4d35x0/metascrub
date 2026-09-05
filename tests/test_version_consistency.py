"""
The version is declared in exactly one place.

metascrub/__init__.py carries __version__, and pyproject.toml reads it from
there via setuptools' dynamic metadata. Until 2026-09-05 both files hardcoded
it, and the 1.0.1 bump changed one and not the other on the first attempt.

The first version of this file compared the two values. That catches drift after
it has happened. These tests assert the arrangement that makes drift impossible,
which is the same move as replacing the duplicated pseudo-group lists in
verify.py and scrubber.py with one shared object: fix the structure, not the
symptom.
"""
from __future__ import annotations

import os
import re

import pytest

import metascrub

_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
_PYPROJECT = os.path.join(_ROOT, "pyproject.toml")


def _pyproject_text() -> str:
    with open(_PYPROJECT, encoding="utf-8") as fh:
        return fh.read()


@pytest.mark.skipif(not os.path.isfile(_PYPROJECT), reason="running from an installed package")
def test_pyproject_does_not_hardcode_a_version():
    """
    The structural guarantee. A literal `version = "..."` under [project] is
    exactly the duplication this arrangement exists to prevent, so its absence
    is the thing worth asserting.
    """
    text = _pyproject_text()
    project = text.split("[project]", 1)[1].split("\n[", 1)[0]
    hardcoded = re.search(r'^\s*version\s*=\s*"', project, re.M)
    assert not hardcoded, (
        "pyproject.toml hardcodes a version again. It must stay dynamic and "
        "read metascrub.__version__, or the two will drift."
    )


@pytest.mark.skipif(not os.path.isfile(_PYPROJECT), reason="running from an installed package")
def test_pyproject_sources_the_version_from_the_package():
    text = _pyproject_text()
    assert 'dynamic = ["version"]' in text, "version is no longer declared dynamic"
    assert 'version = {attr = "metascrub.__version__"}' in text, (
        "the dynamic version no longer points at metascrub.__version__"
    )


def test_the_package_exposes_a_sane_version():
    """Whatever the single source says, it has to look like a version."""
    assert re.fullmatch(r"\d+\.\d+\.\d+([abrc.\-+\w]*)?", metascrub.__version__), (
        "metascrub.__version__ is not a version string: %r" % (metascrub.__version__,)
    )


def test_installed_distribution_agrees_with_the_package():
    """
    When the distribution is actually installed, its recorded version must match
    the constant it was built from. Skipped when running from a source tree with
    no install, which is a supported way to run this project.
    """
    from importlib import metadata

    try:
        dist = metadata.version("scrubproof")
    except metadata.PackageNotFoundError:
        pytest.skip("scrubproof is not installed; running from source")
    assert dist == metascrub.__version__
