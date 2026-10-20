"""The whole tool over a real C++ tree: one candidate, and the exit contract.

The fixture carries a hand-written compilation database so the test needs no build
system, but nothing below the command line is faked: libclang parses both units, the
graph is built from that AST, and the assertions read the JSON report a user would
read. The tree is copied first, so a scan can write its reports beside the sources.
"""

from __future__ import annotations

import json
import shutil
from pathlib import Path

import pytest

from deadweight import cli, discover

FIXTURE = Path(__file__).parent.parent / "fixtures" / "project"


def copy_project(tmp_path: Path) -> Path:
    """A private copy of the fixture, so the run has somewhere to write."""
    target = tmp_path / "project"
    shutil.copytree(FIXTURE, target)
    return target


def test_the_fixture_project_yields_exactly_one_candidate(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The test-only helper is the candidate, the unused one is dead anywhere.

    This is the integration counterpart of the unit suite: the fixture holds exactly
    one function reached only from a test, one reached by nothing, and one virtual
    override that must never be proposed.
    """
    try:
        discover.load()
    except (discover.LibClangError, OSError) as error:
        pytest.skip(f"no libclang found: {error}")
    root = copy_project(tmp_path)
    monkeypatch.chdir(root)

    code = cli.main(
        [
            "scan",
            "--prod",
            "src",
            "--tests",
            "tests",
            "--no-cache",
            "--no-cross-check",
            "--out",
            "reports",
        ],
    )

    assert code == 0
    text = (root / "reports" / "report.json").read_text(encoding="utf-8")
    payload = json.loads(text)
    assert payload["schema"] == "deadweight.report/1"
    assert [item["name"] for item in payload["candidates"]] == ["test_only_helper"]
    assert [item["name"] for item in payload["dead_anywhere"]] == ["unused_everywhere"]
    assert "draw" in {item["name"] for item in payload["keepers"]}
    assert str(root) not in text
