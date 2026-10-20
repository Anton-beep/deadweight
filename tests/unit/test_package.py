"""The package-level contract every later task builds on."""

from __future__ import annotations

import sys

# The snapshot has to be taken before the package is imported, which is why this
# assignment sits above an import.
_MODULES_BEFORE_DEADWEIGHT = frozenset(sys.modules)

import deadweight  # noqa: E402

_STDLIB_AND_SELF = frozenset(sys.stdlib_module_names) | {"deadweight"}


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
    """The package imports the standard library and nothing else."""
    imported = {
        name.partition(".")[0]
        for name in set(sys.modules) - _MODULES_BEFORE_DEADWEIGHT
    }
    third_party = sorted(imported - _STDLIB_AND_SELF)
    assert third_party == []
