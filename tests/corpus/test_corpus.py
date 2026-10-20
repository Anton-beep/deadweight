"""The conformance corpus: one small C++ tree per verdict shape, with expected buckets.

Each case is parsed by the host's libclang, scanned in process through ``deadweight
scan``, and compared against an ``expected.json`` written from the rules in the design
rather than from the tool's current output: a disagreement is a bug in the tool, not a
tolerance to widen. What each case pins:

* ``header_inline_test_only`` — a body in a header reached only from a test is a
  candidate, and the unused function beside it is dead anywhere.
* ``virtual_override_chain`` — an override reached only through an interface is a
  keeper, the private helper its body calls is not proposed, and a defaulted member
  called only from a dead body is a keeper rather than a candidate.
* ``macro_twins`` — two functions expanded from one macro body share a definition
  anchor and are ``ambiguous``, never candidates.
* ``template_body_call`` — a call made from a template body is invisible to the graph,
  so the verdict quotes the production file that still names the function.
* ``ctor_only_in_test`` — a constructor constructed only in a test is a special member,
  and the accessor it serves is a candidate.
* ``recursion`` — a function whose only caller is itself never becomes a root.
* ``address_taken`` — an address taken at namespace scope is a use, and the handler the
  table does not hold is dead anywhere.
* ``overloaded_names`` — two overloads of one name in two files are told apart by USR.
* ``cross_tu_dispatch`` — a call site in another production unit keeps its callee, and
  ``main`` is an entry point.
* ``unparsable_unit`` — a unit that does not parse exits 2 and writes no report at all.
"""

from __future__ import annotations

import json
from collections.abc import Callable
from pathlib import Path
from typing import cast

import pytest

from deadweight.cli import ExitCode

ScanTree = Callable[[Path], tuple[int, dict[str, object] | None]]
"""What the harness answers: the exit code and the report, when one was written."""

CASES_DIR = Path(__file__).parent
"""The directory holding one subdirectory per case."""

BUCKETS = (
    "candidates",
    "keepers",
    "special_members",
    "dead_anywhere",
    "entry_points",
    "ambiguous",
)
"""The six buckets the report writes, in the order it writes them."""

CASES = (
    "address_taken",
    "cross_tu_dispatch",
    "ctor_only_in_test",
    "header_inline_test_only",
    "macro_twins",
    "overloaded_names",
    "recursion",
    "template_body_call",
    "unparsable_unit",
    "virtual_override_chain",
)
"""Every case the corpus is required to hold."""


def case_directories() -> list[Path]:
    """Every case on disk, in a stable order."""
    return sorted(
        path for path in CASES_DIR.iterdir() if (path / "expected.json").is_file()
    )


def expectation(case: Path) -> dict[str, object]:
    """One case's expected verdicts, as its ``expected.json`` spells them."""
    return json.loads((case / "expected.json").read_text(encoding="utf-8"))


def bucket_keys(report: dict[str, object] | None) -> dict[str, list[str]]:
    """Every bucket as the ``file:name`` keys an expectation lists.

    A run that wrote no report answers with empty buckets, because it judged nothing: an
    expectation for such a run may only claim emptiness.
    """
    payload = report or {}
    return {
        bucket: [
            f"{item['file']}:{item['name']}"
            for item in cast("list[dict[str, str]]", payload.get(bucket, []))
        ]
        for bucket in BUCKETS
    }


def test_the_corpus_holds_every_case() -> None:
    """The cases on disk are exactly the ones the verdict model is held to."""
    assert sorted(path.name for path in case_directories()) == list(CASES)


@pytest.mark.parametrize("case", case_directories(), ids=lambda case: case.name)
def test_a_case_answers_its_expected_verdicts(case: Path, scan_tree: ScanTree) -> None:
    """One case's scan answers exactly the verdicts and the exit code it expects."""
    expected = expectation(case)
    exit_code, report = scan_tree(case)
    assert exit_code == expected["exit"]
    found = {bucket: expected.get(bucket, []) for bucket in BUCKETS}
    assert bucket_keys(report) == found
    if expected["exit"] == ExitCode.INCOMPLETE:
        assert report is None, "an incomplete graph must write no report"


def test_the_template_body_case_quotes_the_production_mention(
    scan_tree: ScanTree,
) -> None:
    """A call from a template body is invisible, and the mention is what answers for it.

    ``transform`` is called only from the body of ``doubled``, which the front end does
    not walk, so its bucket says dead anywhere. What keeps that verdict auditable is the
    production file that still names the function, which the report quotes beside it.
    """
    _, report = scan_tree(CASES_DIR / "template_body_call")
    assert report is not None
    mentions = {
        item["name"]: item["prod_mentions"]
        for item in cast("list[dict[str, object]]", report["dead_anywhere"])
    }
    assert mentions == {
        "doubled": ["tree/src/pipeline.cpp"],
        "transform": ["tree/include/pipeline.hpp"],
    }
