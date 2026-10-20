"""The package-level contract every later task builds on."""

from __future__ import annotations

import json
import subprocess
import sys

import deadweight

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
    """The package reports its own release version."""
    assert deadweight.__version__ == "0.1.0"


def test_report_schema_versions_independently_of_the_package() -> None:
    """The report schema identifier is pinned and moves on its own schedule."""
    assert deadweight.REPORT_SCHEMA == "deadweight.report/1"


def test_facts_version_versions_independently_of_the_package() -> None:
    """The facts record version is pinned and moves on its own schedule."""
    assert deadweight.FACTS_VERSION == 1


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
