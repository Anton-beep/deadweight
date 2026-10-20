"""The documentation-truth gate: what the tool does and what the docs say are one thing.

The README is what a stranger reads before running anything and ``docs/verdicts.md`` is
the operator manual; both are prose, so nothing but a test stops a flag from reaching
``--help`` and nowhere else. The check reads the real parser output rather than a copy,
because a copy is the thing that drifts.
"""

from __future__ import annotations

import re
from pathlib import Path

import pytest

from deadweight import cli

_ROOT = Path(__file__).resolve().parents[2]
README = _ROOT / "README.md"
VERDICTS = _ROOT / "docs" / "verdicts.md"

_FLAG = re.compile(r"(?<![\w-])--[a-z][a-z0-9-]*")
"""A long flag as the help output spells it, without its metavar."""

_README_ROW = re.compile(r"^\|\s*(\d+)\s*\|\s*([^|]+?)\s*\|\s*$", re.MULTILINE)
"""One row of the README's exit-code table."""

_DOC_LINK = re.compile(r"\((docs/[a-z-]+\.md)\)")
"""A relative link from the README to one of the three operator documents."""


def _scan_help(capsys: pytest.CaptureFixture[str]) -> str:
    """The real ``deadweight scan --help`` output, off the parser that ships."""
    with pytest.raises(SystemExit) as exit_info:
        cli.main(["scan", "--help"])
    assert exit_info.value.code == 0
    return capsys.readouterr().out


def test_every_scan_flag_appears_in_the_readme_or_the_operator_manual(
    capsys: pytest.CaptureFixture[str],
) -> None:
    """A flag an operator can type is a flag the documentation has to name.

    The README carries the first-run path and ``docs/verdicts.md`` the reference, so
    either is a legitimate home for a flag; appearing in neither means the only way to
    discover it is the help output.
    """
    flags = set(_FLAG.findall(_scan_help(capsys))) - {"--help"}
    assert flags, "the parser printed no flags, so this test would pass vacuously"
    documented = "\n".join(
        path.read_text(encoding="utf-8") for path in (README, VERDICTS)
    )
    assert sorted(flag for flag in flags if flag not in documented) == []


def test_the_readme_exit_codes_are_the_ones_the_cli_defines() -> None:
    """The README's table is the published exit contract, so it must be the whole one.

    The meanings are prose and are compared by a human; the codes themselves are the
    machine-checked half, because a code that exists in ``cli.py`` and not in the
    README is one an operator's script cannot know about.
    """
    readme = README.read_text(encoding="utf-8")
    section = readme.partition("## Exit codes")[2].partition("## ")[0]
    documented = [int(code) for code, _ in _README_ROW.findall(section)]
    assert documented, "the README has no exit-code table"
    assert len(set(documented)) == len(documented), "an exit code is documented twice"
    assert sorted(documented) == sorted(int(code) for code in cli.ExitCode)


def test_the_readme_links_the_three_operator_documents() -> None:
    """The manual, the compatibility contract and the design note are reachable."""
    linked = set(_DOC_LINK.findall(README.read_text(encoding="utf-8")))
    assert sorted(linked) == sorted(
        ["docs/compatibility.md", "docs/design.md", "docs/verdicts.md"],
    )
    assert [link for link in sorted(linked) if not (_ROOT / link).is_file()] == []
