"""Rendering: the machine-readable report, the operator's markdown and the TSV."""

from __future__ import annotations

import json
from typing import TYPE_CHECKING

from .model import SCHEMA, Analysis, Candidate

if TYPE_CHECKING:
    from .model import RunMeta


def _ordered(candidates: list[Candidate]) -> list[Candidate]:
    """Report order: by source location, then by name.

    The analysis already fills each bucket in this order; sorting again here is what
    makes two renders of the same analysis byte-identical whatever order the caller
    handed the bucket over in.
    """
    return sorted(candidates, key=lambda candidate: candidate.sort_key)


def candidate_payload(candidate: Candidate) -> dict[str, object]:
    """One function as a machine consumer sees it, evidence included."""
    test_refs = [{"file": file, "line": line} for file, line in candidate.test_refs]
    return {
        "name": candidate.name,
        "usr": candidate.usr,
        "qualified_name": candidate.qualified_name,
        "kind": candidate.kind,
        "file": candidate.file,
        "line": candidate.line,
        "col": candidate.col,
        "type": candidate.type_text,
        "test_refs": test_refs,
        "reached_via": list(candidate.reached_via),
        "flags": list(candidate.flags),
        "reason": candidate.reason,
        "cross_check": candidate.cross_check,
        "cross_check_detail": candidate.cross_check_detail,
        "gc": candidate.gc,
        "gc_detail": candidate.gc_detail,
        "other_refs": candidate.other_refs,
        "direct_prod_refs": candidate.direct_prod_refs,
        "prod_mentions": list(candidate.prod_mentions),
    }


def render_json(analysis: Analysis, meta: RunMeta) -> str:
    """Render the machine-readable report: stable ordering, no timestamps."""
    inputs: dict[str, object] = {
        "scan_root": meta.scan_root,
        "compile_commands": meta.compile_commands,
        "prod_roots": list(meta.prod_roots),
        "test_roots": list(meta.test_roots),
        "gc_reports": list(meta.gc_reports),
        "gc_report_tests": list(meta.gc_report_tests),
        "prod_binary": meta.prod_binary,
        "test_binaries_dir": meta.test_binaries_dir,
        "jobs": meta.jobs,
        "cascade": meta.cascade,
        "cross_check": meta.cross_check,
    }
    payload: dict[str, object] = {
        "schema": SCHEMA,
        "tool": {"name": meta.tool, "version": meta.version},
        "inputs": inputs,
        "tu_counts": analysis.tu_counts,
        "failures": analysis.failures,
        "notes": analysis.notes,
        "unresolved_targets": analysis.unresolved_targets,
        "candidates": [
            candidate_payload(item) for item in _ordered(analysis.candidates)
        ],
        "keepers": [candidate_payload(item) for item in _ordered(analysis.keepers)],
        "special_members": [
            candidate_payload(item) for item in _ordered(analysis.special_members)
        ],
        "dead_anywhere": [
            candidate_payload(item) for item in _ordered(analysis.dead_anywhere)
        ],
        "entry_points": [
            candidate_payload(item) for item in _ordered(analysis.entry_points)
        ],
        "ambiguous": [candidate_payload(item) for item in _ordered(analysis.ambiguous)],
    }
    return json.dumps(payload, indent=2, sort_keys=True) + "\n"


def refs_text(candidate: Candidate, limit: int = 6) -> str:
    """The test sites that reach a function, truncated to stay one table row long."""
    refs = [f"{file}:{line}" for file, line in candidate.test_refs]
    if not refs:
        return "-"
    if len(refs) > limit:
        return ", ".join(refs[:limit]) + f" ... (+{len(refs) - limit} more)"
    return ", ".join(refs)


def markdown_table(
    candidates: list[Candidate],
    refs_header: str = "test uses",
) -> list[str]:
    """The markdown table every candidate bucket shares."""
    header = (
        f"| symbol | kind | defined | {refs_header} "
        "| reached via (prod) | flags | cross-check |"
    )
    lines = [header, "| --- | --- | --- | --- | --- | --- | --- |"]
    for candidate in _ordered(candidates):
        symbol = candidate.qualified_name or candidate.name
        definition = f"{candidate.file}:{candidate.line}"
        reached = ", ".join(candidate.reached_via) or "-"
        flags = ", ".join(candidate.flags) or "-"
        cross = candidate.cross_check
        if candidate.cross_check_detail:
            cross = f"{cross} ({candidate.cross_check_detail})"
        lines.append(
            f"| `{symbol}` | {candidate.kind} | {definition} | {refs_text(candidate)} "
            f"| {reached} | {flags} | {cross} |",
        )
    return lines


def _summary_lines(analysis: Analysis) -> list[str]:
    """The report's opening: what was scanned, and what a candidate row means."""
    counts = analysis.tu_counts
    counts_line = (
        f"- Translation units: {counts.get('prod', 0)} prod, "
        f"{counts.get('test', 0)} test, {counts.get('other', 0)} other, "
        f"{counts.get('failed', 0)} failed."
    )
    unresolved_line = (
        "- Reference targets that resolved to no scanned declaration: "
        f"{analysis.unresolved_targets} (lambdas and compiler-generated members are "
        "deliberately outside the graph)."
    )
    lines = [
        "# Functions no production translation unit reaches",
        "",
        counts_line,
        unresolved_line,
        "",
        (
            "A row in `candidates` is defined in production code, is unreachable from "
            "every production call site, and is reached by at least one test. Rows in "
            "the other sections exist so the verdict is auditable: nothing here is "
            "safe to delete without reading its section."
        ),
        "",
    ]
    if any(
        candidate.cross_check == "unavailable"
        for candidate in [*analysis.candidates, *analysis.dead_anywhere]
    ):
        lines += [
            (
                "The linker cross-check is unavailable for this run, so every "
                "verdict below rests on the AST alone."
            ),
            "",
        ]
    return lines


def _candidate_sections(analysis: Analysis) -> list[str]:
    """The two sections that propose a removal, test-only first."""
    lines = ["## Candidates (test-only)", ""]
    if analysis.candidates:
        lines += markdown_table(analysis.candidates)
        lines += [""]
        if any(item.reached_via for item in analysis.candidates):
            lines += [
                (
                    "Rows with a non-empty `reached via (prod)` column are test-only "
                    "only through other test-only production functions (the "
                    "transitive cascade); removing the caller makes them dead as well."
                ),
                "",
            ]
    else:
        lines += ["None.", ""]
    lines += ["## Candidates (referenced nowhere at all)", ""]
    if analysis.dead_anywhere:
        lines += markdown_table(analysis.dead_anywhere)
        lines += [""]
    else:
        lines += ["None.", ""]
    return lines


def _keeper_sections(analysis: Analysis) -> list[str]:
    """Every bucket that is reported so the verdict can be audited, not acted on."""
    lines = ["## Keepers - reported, not proposed for removal", ""]
    for bucket, title in (
        (analysis.keepers, "keep"),
        (analysis.special_members, "special member"),
        (analysis.ambiguous, "ambiguous"),
        (analysis.entry_points, "entry point"),
    ):
        if not bucket:
            continue
        lines += [f"### {title}", ""]
        lines += [
            f"- `{candidate.name}` ({candidate.kind}) at "
            f"{candidate.file}:{candidate.line} - "
            f"{candidate.reason}; test uses: {refs_text(candidate)}"
            for candidate in _ordered(bucket)
        ]
        lines += [""]
    return lines


def render_markdown(analysis: Analysis) -> str:
    """Render the human-readable report: candidates first, then every keeper."""
    lines = _summary_lines(analysis)
    lines += _candidate_sections(analysis)
    lines += _keeper_sections(analysis)
    if analysis.failures:
        lines += ["## Failed translation units", ""]
        lines += [f"- `{item['file']}`: {item['reason']}" for item in analysis.failures]
        lines += [""]
    if analysis.notes:
        lines += ["## Notes", ""]
        lines += [f"- {note}" for note in analysis.notes]
        lines += [""]
    return "\n".join(lines).rstrip() + "\n"


def render_tsv(analysis: Analysis) -> str:
    """One row per reported function, for spreadsheet and pipeline triage."""
    rows = [
        "bucket\tname\tkind\tdefined\ttest_uses\treached_via\tflags\tcross_check\tgc\treason",
    ]
    for bucket, items in (
        ("candidate", analysis.candidates),
        ("dead-anywhere", analysis.dead_anywhere),
        ("keeper", analysis.keepers),
        ("special-member", analysis.special_members),
        ("ambiguous", analysis.ambiguous),
        ("entry-point", analysis.entry_points),
    ):
        rows.extend(_tsv_row(bucket, candidate) for candidate in _ordered(items))
    return "\n".join(rows) + "\n"


def _tsv_row(bucket: str, candidate: Candidate) -> str:
    """One tab-separated row; every row carries the same ten columns as the header."""
    return "\t".join(
        [
            bucket,
            candidate.qualified_name or candidate.name,
            candidate.kind,
            f"{candidate.file}:{candidate.line}",
            refs_text(candidate, limit=99).replace(", ", "; "),
            "; ".join(candidate.reached_via),
            ", ".join(candidate.flags),
            candidate.cross_check,
            candidate.gc,
            candidate.reason,
        ],
    )
