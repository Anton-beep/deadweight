"""The package-level contract every later task builds on."""

from __future__ import annotations

import json
import subprocess
import sys
import tomllib
from pathlib import Path

import deadweight

_ROOT = Path(__file__).resolve().parents[2]

_PROBE = """
import json
import sys

before = set(sys.modules)
import deadweight
import deadweight.analysis
import deadweight.model
import deadweight.symbols

roots = {name.partition(".")[0] for name in set(sys.modules) - before}
print(json.dumps(sorted(roots)))
"""


def test_version_is_the_release_version() -> None:
    """The package reports the version its own metadata declares, not a copy of it."""
    declared = tomllib.loads(
        (_ROOT / "pyproject.toml").read_text(encoding="utf-8"),
    )["project"]["version"]
    assert deadweight.__version__ == declared


def test_report_schema_versions_independently_of_the_package() -> None:
    """The report schema identifier is pinned and moves on its own schedule."""
    assert deadweight.REPORT_SCHEMA == "deadweight.report/1"


def test_facts_version_versions_independently_of_the_package() -> None:
    """The facts record version is pinned and moves on its own schedule."""
    facts_record_version = 2
    assert facts_record_version == deadweight.FACTS_VERSION


def test_importing_the_package_pulls_in_no_third_party_distribution() -> None:
    """The package imports the standard library and nothing else.

    The probe runs in a fresh interpreter, because in-process every baseline is
    measured against modules pytest imported first, and the difference would say
    more about pytest's import order than about the package.
    """
    probe = subprocess.run(  # noqa: S603 - the arguments are literals, not user input
        [sys.executable, "-c", _PROBE],
        capture_output=True,
        text=True,
        check=True,
    )
    imported = set(json.loads(probe.stdout))
    assert sorted(imported - frozenset(sys.stdlib_module_names) - {"deadweight"}) == []
