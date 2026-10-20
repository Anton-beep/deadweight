"""The corpus harness: a private copy of a case tree, one in-process scan, its report.

A case is a directory holding a ``tree/`` of C++, a ``compile_commands.json`` over it
and an ``expected.json`` of the verdicts. The tree is copied before it is scanned
because a run writes its reports beside the sources and the corpus directory is checked
in, and the scan itself is the command line run in process, so a case asserts what a
user would see rather than an intermediate the tests share with the implementation.

Both live here rather than in the test module because the corpus is the one part of the
suite that cannot run without a libclang: the skip has to cover every case at once, and
a case-by-case skip would report ten reasons to look away instead of one.
"""

from __future__ import annotations

import json
import shutil
from typing import TYPE_CHECKING

import pytest

from deadweight import cli, discover

if TYPE_CHECKING:
    from collections.abc import Callable
    from pathlib import Path

PROD_ROOTS = "tree/src,tree/include"
"""Every corpus tree keeps its production code under these two roots."""

TEST_ROOTS = "tree/tests"
"""Every corpus tree keeps its tests under this root."""


@pytest.fixture(scope="session", autouse=True)
def _parsable_tree() -> None:
    """Skip the corpus on a host that has no libclang to parse it with."""
    try:
        discover.load()
    except (discover.LibClangError, OSError) as error:
        pytest.skip(f"no libclang found: {error}")


@pytest.fixture
def scan_tree(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> Callable[[Path], tuple[int, dict[str, object] | None]]:
    """Answer with a function that scans one case and reports what it found."""

    def scan(case: Path) -> tuple[int, dict[str, object] | None]:
        """Copy ``case`` aside, scan the copy, and read the report it wrote.

        The report is ``None`` when the run refused to write one, which is what an
        incomplete graph does: no report at all rather than a clean-looking one.
        """
        root = tmp_path / case.name
        shutil.copytree(case, root)
        monkeypatch.chdir(root)
        exit_code = cli.main(
            [
                "scan",
                "--compile-commands",
                "compile_commands.json",
                "--prod",
                PROD_ROOTS,
                "--tests",
                TEST_ROOTS,
                "--jobs",
                "1",
                "--no-cache",
                "--no-cross-check",
                "--out",
                "reports",
            ],
        )
        report = root / "reports" / "report.json"
        if not report.is_file():
            return exit_code, None
        return exit_code, json.loads(report.read_text(encoding="utf-8"))

    return scan
