"""The report: its schema, its determinism and the three renderings of one analysis."""

from __future__ import annotations

import json
from typing import TYPE_CHECKING

from deadweight import REPORT_SCHEMA, __version__
from deadweight.model import Analysis, Candidate, RunMeta
from deadweight.report import (
    candidate_payload,
    markdown_table,
    refs_text,
    render_json,
    render_markdown,
    render_tsv,
)

if TYPE_CHECKING:
    from collections.abc import Sequence

CANDIDATE_LINE = 10
"""The line a fixture candidate is defined on."""

RUN_META = RunMeta(
    version=__version__,
    scan_root=".",
    compile_commands="build/compile_commands.json",
    prod_roots=("client",),
    test_roots=("tests",),
    jobs=2,
)


def make_candidate(  # noqa: PLR0913 -- a fixture builder: one argument per field
    name: str,
    *,
    file: str = "client/a.cpp",
    line: int = CANDIDATE_LINE,
    kind: str = "function",
    test_refs: Sequence[tuple[str, int]] = (),
    reached_via: Sequence[str] = (),
    flags: Sequence[str] = (),
    cross_check: str = "skipped",
    gc: str = "none",
) -> Candidate:
    """One function, as the analysis would have built it."""
    return Candidate(
        usr=f"c:@N@demo@F@{name}#",
        name=name,
        kind=kind,
        file=file,
        line=line,
        col=1,
        type_text="void ()",
        test_refs=list(test_refs),
        reached_via=list(reached_via),
        flags=list(flags),
        cross_check=cross_check,
        gc=gc,
    )


def make_analysis(
    candidates: Sequence[Candidate] = (),
    *,
    keepers: Sequence[Candidate] = (),
    dead_anywhere: Sequence[Candidate] = (),
) -> Analysis:
    """An analysis holding only the buckets a rendering test needs."""
    return Analysis(
        components={},
        candidates=list(candidates),
        keepers=list(keepers),
        special_members=[],
        dead_anywhere=list(dead_anywhere),
        entry_points=[],
        ambiguous=[],
        unresolved_targets=2,
    )


def test_report_schema_is_deadweight_report_1() -> None:
    """The schema is its own identifier, versioned apart from the package version."""
    analysis = make_analysis([make_candidate("TestOnly")])
    payload = json.loads(render_json(analysis, RUN_META))
    assert payload["schema"] == "deadweight.report/1"
    assert payload["schema"] == REPORT_SCHEMA
    assert payload["tool"] == {"name": "deadweight", "version": __version__}
    assert set(payload["inputs"]) == {
        "scan_root",
        "compile_commands",
        "prod_roots",
        "test_roots",
        "gc_reports",
        "gc_report_tests",
        "prod_binary",
        "test_binaries_dir",
        "jobs",
        "cascade",
        "cross_check",
    }
    assert set(payload) == {
        "schema",
        "tool",
        "inputs",
        "tu_counts",
        "failures",
        "notes",
        "unresolved_targets",
        "candidates",
        "keepers",
        "special_members",
        "dead_anywhere",
        "entry_points",
        "ambiguous",
    }


def test_report_is_deterministic_and_timestamp_free() -> None:
    """Same analysis, same bytes; rows follow source order, never a clock."""
    later = make_candidate("Second", line=40, test_refs=[("tests/unit/t.cpp", 5)])
    earlier = make_candidate("First", line=20, test_refs=[("tests/unit/t.cpp", 9)])
    analysis = make_analysis([later, earlier])
    json_text = render_json(analysis, RUN_META)
    assert render_json(analysis, RUN_META) == json_text
    assert "timestamp" not in json_text
    payload = json.loads(json_text)
    assert [item["name"] for item in payload["candidates"]] == ["First", "Second"]
    markdown = render_markdown(analysis)
    assert render_markdown(analysis) == markdown
    assert markdown.index("First") < markdown.index("Second")
    assert "tests/unit/t.cpp:5" in markdown
    tsv = render_tsv(analysis)
    assert render_tsv(analysis) == tsv
    assert tsv.splitlines()[1].startswith("candidate\tFirst\t")


def test_candidate_payload_carries_the_evidence_a_verdict_rests_on() -> None:
    """A machine consumer sees the same evidence a human reads in the table."""
    candidate = make_candidate(
        "TestOnly",
        test_refs=[("tests/a.cpp", 7)],
        reached_via=["Caller"],
        flags=["header-defined"],
        cross_check="gc-confirmed",
        gc="gc-confirmed",
    )
    payload = candidate_payload(candidate)
    assert payload["file"] == "client/a.cpp"
    assert payload["line"] == CANDIDATE_LINE
    assert payload["test_refs"] == [{"file": "tests/a.cpp", "line": 7}]
    assert payload["reached_via"] == ["Caller"]
    assert payload["flags"] == ["header-defined"]
    assert payload["cross_check"] == "gc-confirmed"
    assert payload["gc"] == "gc-confirmed"
    assert payload["prod_mentions"] == []


def test_refs_text_lists_locations_and_truncates_a_long_list() -> None:
    """The table stays readable when a function has dozens of test uses."""
    bare = make_candidate("Bare")
    assert refs_text(bare) == "-"
    refs = [(f"tests/t{index}.cpp", index) for index in range(8)]
    many = make_candidate("Many", test_refs=refs)
    assert refs_text(many) == (
        "tests/t0.cpp:0, tests/t1.cpp:1, tests/t2.cpp:2, tests/t3.cpp:3, "
        "tests/t4.cpp:4, tests/t5.cpp:5 ... (+2 more)"
    )
    assert refs_text(many, limit=1).endswith("(+7 more)")


def test_markdown_table_columns_follow_the_model() -> None:
    """One location per function now that the facts record only definitions."""
    table = markdown_table([make_candidate("TestOnly")], refs_header="test uses")
    assert table[0] == (
        "| symbol | kind | defined | test uses | reached via (prod) | flags "
        "| cross-check |"
    )
    assert table[2] == (
        "| `TestOnly` | function | client/a.cpp:10 | - | - | - | skipped |"
    )


def test_render_markdown_says_when_the_cross_check_is_unavailable() -> None:
    """A Linux-shaped conclusion must not be read off a run with no linker tier."""
    unavailable = make_analysis([make_candidate("TestOnly", cross_check="unavailable")])
    assert "linker cross-check is unavailable" in render_markdown(unavailable)
    checked = make_analysis([make_candidate("TestOnly", cross_check="gc-confirmed")])
    assert "linker cross-check is unavailable" not in render_markdown(checked)


def test_render_markdown_prints_keepers_failures_and_notes() -> None:
    """The report accounts for every bucket, every failure and every caveat."""
    analysis = make_analysis(
        [make_candidate("TestOnly")],
        keepers=[make_candidate("Virtual", line=30, kind="method")],
        dead_anywhere=[make_candidate("Dead", line=50)],
    )
    analysis.failures.append({"file": "client/broken.cpp", "reason": "expected ';'"})
    analysis.notes.append("gc-report: nothing discarded")
    markdown = render_markdown(analysis)
    assert "## Candidates (test-only)" in markdown
    assert "## Candidates (referenced nowhere at all)" in markdown
    assert "## Keepers - reported, not proposed for removal" in markdown
    assert "`Virtual` (method) at client/a.cpp:30" in markdown
    assert "`client/broken.cpp`: expected ';'" in markdown
    assert "gc-report: nothing discarded" in markdown


def test_tsv_header_matches_every_row() -> None:
    """The header the pipeline reads names the same columns the rows carry."""
    analysis = make_analysis(
        [make_candidate("TestOnly")],
        keepers=[make_candidate("Virtual", line=30, kind="method")],
        dead_anywhere=[make_candidate("Dead", line=50)],
    )
    rows = render_tsv(analysis).splitlines()
    widths = {len(row.split("\t")) for row in rows}
    assert len(widths) == 1
    assert rows[0].split("\t") == [
        "bucket",
        "name",
        "kind",
        "defined",
        "test_uses",
        "reached_via",
        "flags",
        "cross_check",
        "gc",
        "reason",
    ]
