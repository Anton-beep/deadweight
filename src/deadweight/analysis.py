"""The reference graph: USR-keyed components, caller attribution, the fixpoint."""

from __future__ import annotations

import re
from collections import deque
from dataclasses import dataclass, field
from typing import TYPE_CHECKING

from .model import (
    STATUS_OK,
    Analysis,
    Candidate,
    Component,
    Decl,
    DefinitionSpan,
    Facts,
    Loc,
    under_any,
)

if TYPE_CHECKING:
    from collections.abc import Callable
    from pathlib import Path

SPECIAL_KINDS = frozenset({"constructor", "destructor", "conversion", "assignment"})
"""Kinds whose removal changes construction or assignment, never a call site."""

COMPILER_OWNED = frozenset({"defaulted", "deleted"})
"""Markers of members the compiler generates: removing them changes class semantics."""

HEADER_SUFFIXES = (".hpp", ".hh", ".hxx", ".h", ".inc", ".inl")
"""Suffixes of a body that only exists in a header, which a removal has to respect."""

MENTION_SUFFIXES = frozenset({".cpp", ".hpp", ".cc", ".hh", ".cxx", ".hxx", ".h"})
"""Files whose text is searched for a candidate's name."""

MENTION_PATTERN = re.compile(r"[A-Za-z_][A-Za-z0-9_]*")


@dataclass
class _Evidence:
    """What the reference walk saw, before the fixpoint turns it into buckets."""

    test_sites: dict[str, set[tuple[str, int]]] = field(default_factory=dict)
    direct_prod: dict[str, int] = field(default_factory=dict)
    reached_via: dict[str, list[str]] = field(default_factory=dict)
    other_refs: dict[str, int] = field(default_factory=dict)
    edges: dict[str, set[str]] = field(default_factory=dict)
    roots: set[str] = field(default_factory=set)
    unresolved: int = 0


def build_components(
    decls: list[Decl],
    prod_roots: tuple[str, ...],
) -> dict[str, Component]:
    """Group declarations into functions keyed by USR, keeping the first-party ones.

    A USR is canonical, so a header declaration and its out-of-line definition arrive
    under one key with no merge pass. Only a function with a declaration under
    ``prod_roots`` is a component: a function that exists only in test code is not
    part of the production graph at all.
    """
    grouped: dict[str, list[Decl]] = {}
    for decl in decls:
        grouped.setdefault(decl.usr, []).append(decl)
    components: dict[str, Component] = {}
    for usr, group in grouped.items():
        if not any(under_any(decl.file, prod_roots) for decl in group):
            continue
        # The first declaration is the one the report quotes, so a production
        # redeclaration wins over a test-side one.
        group.sort(
            key=lambda decl: (
                not under_any(decl.file, prod_roots),
                decl.file,
                decl.loc,
                decl.name,
            ),
        )
        components[usr] = Component(
            usr=usr,
            names={decl.name for decl in group},
            decls=group,
            markers={marker for decl in group for marker in decl.markers},
        )
    return components


def definition_spans(
    components: dict[str, Component],
    prod_roots: tuple[str, ...],
) -> list[DefinitionSpan]:
    """The production bodies, for attributing a reference site to its caller.

    It takes the components :func:`analyze` already grouped rather than the raw
    declarations, so the grouping and its ordering happen once per run.
    """
    spans: list[DefinitionSpan] = []
    seen: set[tuple[str, int, int, int, int]] = set()
    for component in components.values():
        for decl in component.decls:
            if not under_any(decl.file, prod_roots):
                continue
            fingerprint = (
                decl.span.start.file,
                decl.span.start.line,
                decl.span.start.col,
                decl.span.end.line,
                decl.span.end.col,
            )
            if fingerprint in seen:
                continue
            seen.add(fingerprint)
            spans.append(
                DefinitionSpan(usr=component.usr, name=decl.name, span=decl.span),
            )
    return spans


def enclosing_function(spans: list[DefinitionSpan], loc: Loc) -> DefinitionSpan | None:
    """Return the innermost production function body containing ``loc``."""
    best: DefinitionSpan | None = None
    for span in spans:
        if not span.span.contains(loc):
            continue
        if best is None or span.span.start > best.span.start:
            best = span
    return best


def is_dispatch_root(component: Component) -> bool:
    """True when the function can be reached without a call site the graph sees."""
    return "virtual" in component.markers


def is_construction(component: Component) -> bool:
    """True when this is a constructor or a destructor.

    Construction and destruction happen through paths a static call graph cannot
    enumerate: a static duration object, a member of another class, a temporary in a
    factory, a container that reallocates. A body that only a constructor calls is
    production code, and topi's tree is full of it — ``RegisterRoutes`` is called from
    a constructor, and treating that edge as invisible reported the whole
    route-registration chain as test-only.
    """
    return any(decl.kind in {"constructor", "destructor"} for decl in component.decls)


def _walk(
    facts: list[Facts],
    components: dict[str, Component],
    spans: list[DefinitionSpan],
    klass: Callable[[str], str],
    *,
    cascade: bool,
) -> _Evidence:
    """Sort every reference into test evidence, a production call edge or a root."""
    evidence = _Evidence()
    for fact in facts:
        if fact.status != STATUS_OK:
            continue
        for ref in fact.refs:
            if ref.usr not in components:
                evidence.unresolved += 1
                continue
            # The reference carries the translation unit that saw it, which decides
            # whether it is a test use or a production call site.
            kind = klass(ref.file)
            if kind == "test":
                evidence.test_sites.setdefault(ref.usr, set()).add(
                    (ref.file, ref.loc.line),
                )
            elif kind == "other":
                evidence.other_refs[ref.usr] = evidence.other_refs.get(ref.usr, 0) + 1
            else:
                evidence.direct_prod[ref.usr] = evidence.direct_prod.get(ref.usr, 0) + 1
                caller = enclosing_function(spans, ref.loc)
                if caller is None or not cascade:
                    # A production reference from outside a function body
                    # (namespace-scope table, default argument, system-header
                    # instantiation) is a root; with the cascade disabled every
                    # production reference counts as one.
                    evidence.roots.add(ref.usr)
                else:
                    evidence.edges.setdefault(caller.usr, set()).add(ref.usr)
                    evidence.reached_via.setdefault(ref.usr, []).append(caller.name)
    return evidence


def _fixpoint(components: dict[str, Component], evidence: _Evidence) -> set[str]:
    """Add every entry point and dispatch root, then close over the call edges.

    Constructors and destructors propagate liveness without being live themselves.
    Their callees are production-reachable — a helper called from a constructor is
    reached whenever the object is constructed anywhere — but a constructor that only
    a test calls is still evidence worth reporting, so it keeps its own verdict and
    lands in ``special_members``. Seeding it as live would answer both questions with
    one answer and quietly drop a real test-only constructor from the report.
    """
    live = set(evidence.roots)
    for usr, component in components.items():
        if "main" in component.names or is_dispatch_root(component):
            live.add(usr)
    seeds = set(live)
    seeds.update(
        usr for usr, component in components.items() if is_construction(component)
    )
    queue = deque(sorted(seeds))
    while queue:
        source = queue.popleft()
        for callee in sorted(evidence.edges.get(source, ())):
            if callee not in live:
                live.add(callee)
                queue.append(callee)
    return live


def _shared_anchors(components: dict[str, Component]) -> set[str]:
    """The USRs whose anchors collide: what macro-generated twins look like."""
    anchored: dict[Loc, list[str]] = {}
    for usr, component in components.items():
        anchored.setdefault(component.decls[0].span.start, []).append(usr)
    return {usr for usrs in anchored.values() if len(usrs) > 1 for usr in usrs}


def _record_evidence(candidate: Candidate, usr: str, evidence: _Evidence) -> None:
    """Attach the reference evidence to a candidate, and the flags it implies."""
    candidate.test_refs = sorted(evidence.test_sites.get(usr, set()))
    candidate.reached_via = sorted(set(evidence.reached_via.get(usr, [])))
    candidate.other_refs = evidence.other_refs.get(usr, 0)
    candidate.direct_prod_refs = evidence.direct_prod.get(usr, 0)
    if candidate.file.endswith(HEADER_SUFFIXES):
        candidate.flags.append("header-defined")
    if any("mock" in path for path, _ in candidate.test_refs):
        candidate.flags.append("referenced-by-mock")


def _entry_point_or_keeper(
    component: Component,
    candidate: Candidate,
    live: set[str],
) -> tuple[str, str] | None:
    """The buckets a function enters without being dead weight, with its reason.

    A keeper is a virtual/override or a compiler-owned member that nothing keeps in
    the graph but the rule itself, and the report owes the operator that explanation.
    The two need different tests for that. Liveness cannot stand in for dispatch,
    because the fixpoint seeds every dispatch root as a root — asking whether a virtual
    is live answers yes because it is virtual, which would drop it from the report
    rather than explain it, so a virtual is a keeper when no production reference
    touches it. For a compiler-owned member the reference gate would be wrong: a
    ``= default`` that only a dead production body calls has a production reference
    yet is no more reached than that dead body, so the liveness test is what keeps it
    out of the candidates. A virtual a production body does call needs no explanation,
    and is reached like any other callee.
    """
    if "main" in component.names:
        return (
            "entry_points",
            "entry point: reached by the C runtime, not by a call site",
        )
    if is_dispatch_root(component):
        if candidate.direct_prod_refs or candidate.reached_via:
            return None
        return "keepers", (
            "virtual/override with no production call site: dispatch or a mock may "
            "call it, so it is never proposed"
        )
    if component.markers & COMPILER_OWNED and component.usr not in live:
        return "keepers", (
            "compiler-owned (= default / = delete): removing it changes class semantics"
        )
    return None


def _bucket(
    component: Component,
    candidate: Candidate,
    live: set[str],
    shared: set[str],
) -> tuple[str, str] | None:
    """Which bucket a function belongs in, and the sentence that explains it."""
    rooted = _entry_point_or_keeper(component, candidate, live)
    if rooted is not None:
        return rooted
    if component.usr in live:
        return None
    if candidate.kind in SPECIAL_KINDS:
        return "special_members", (
            "special member: verify before removing (construction/copy semantics)"
        )
    if component.usr in shared:
        return (
            "ambiguous",
            "ambiguous identity: several functions share this definition anchor",
        )
    if candidate.test_refs or candidate.reached_via:
        return "candidates", ""
    return "dead_anywhere", ""


def _candidate_from(component: Component) -> Candidate:
    decl = component.decls[0]
    return Candidate(
        usr=component.usr,
        name=decl.name,
        kind=decl.kind,
        file=decl.file,
        line=decl.loc.line,
        col=decl.loc.col,
        type_text=decl.type_text,
    )


def _failures(facts: list[Facts]) -> list[dict[str, str]]:
    """The translation units that did not parse, with the diagnostic that says why."""
    return [
        {
            "file": fact.tu,
            "reason": fact.diags[0] if fact.diags else "no diagnostic reported",
        }
        for fact in facts
        if fact.status != STATUS_OK
    ]


def _root_class(
    path: str,
    prod_roots: tuple[str, ...],
    test_roots: tuple[str, ...],
) -> str:
    """Which side of the scan a path belongs to."""
    if under_any(path, prod_roots):
        return "prod"
    if under_any(path, test_roots):
        return "test"
    return "other"


def _classify_tus(
    facts: list[Facts],
    prod_roots: tuple[str, ...],
    test_roots: tuple[str, ...],
) -> tuple[Callable[[str], str], dict[str, int]]:
    """Classify every translation unit by root, and count the four kinds.

    The returned classifier answers for any path, not only the translation units the
    facts hold: a reference names the unit that saw it, which is the same path but
    need not appear as a facts record of its own.
    """
    counts = {"prod": 0, "test": 0, "other": 0, "failed": 0}
    classes: dict[str, str] = {}

    def klass(path: str) -> str:
        if path not in classes:
            classes[path] = _root_class(path, prod_roots, test_roots)
        return classes[path]

    for fact in facts:
        if fact.status != STATUS_OK:
            counts["failed"] += 1
            continue
        counts[klass(fact.tu)] += 1
    return klass, counts


def analyze(
    facts: list[Facts],
    prod_roots: tuple[str, ...],
    test_roots: tuple[str, ...],
    *,
    cascade: bool = True,
) -> Analysis:
    """Classify every first-party function by which translation units reach it."""
    klass, tu_counts = _classify_tus(facts, prod_roots, test_roots)
    decls = [decl for fact in facts if fact.status == STATUS_OK for decl in fact.decls]
    components = build_components(decls, prod_roots)
    spans = definition_spans(components, prod_roots)
    evidence = _walk(facts, components, spans, klass, cascade=cascade)
    live = _fixpoint(components, evidence)
    shared = _shared_anchors(components)

    buckets: dict[str, list[Candidate]] = {
        "candidates": [],
        "keepers": [],
        "special_members": [],
        "dead_anywhere": [],
        "entry_points": [],
        "ambiguous": [],
    }
    for usr, component in components.items():
        candidate = _candidate_from(component)
        _record_evidence(candidate, usr, evidence)
        placed = _bucket(component, candidate, live, shared)
        if placed is None:
            continue
        bucket, reason = placed
        candidate.reason = reason
        buckets[bucket].append(candidate)
    for members in buckets.values():
        members.sort(key=lambda item: item.sort_key)
    return Analysis(
        components=components,
        candidates=buckets["candidates"],
        keepers=buckets["keepers"],
        special_members=buckets["special_members"],
        dead_anywhere=buckets["dead_anywhere"],
        entry_points=buckets["entry_points"],
        ambiguous=buckets["ambiguous"],
        unresolved_targets=evidence.unresolved,
        tu_counts=tu_counts,
        failures=_failures(facts),
    )


def annotate_prod_mentions(
    analysis: Analysis,
    repo_root: Path,
    prod_roots: tuple[str, ...],
) -> None:
    """Record production files that mention a candidate's name outside its own body.

    The graph cannot see a name used inside a template body that is only instantiated
    from production code, so the report says where the name still appears and leaves
    the judgement to a human. The recorded paths are relative to ``repo_root``, the
    same form as every other path in the report.
    """
    index: dict[str, set[str]] = {}
    for root in prod_roots:
        base = repo_root / root
        if not base.is_dir():
            continue
        for path in sorted(base.rglob("*")):
            if not path.is_file() or path.suffix not in MENTION_SUFFIXES:
                continue
            try:
                text = path.read_text(encoding="utf-8", errors="replace")
            except OSError:
                continue
            index[path.relative_to(repo_root).as_posix()] = set(
                MENTION_PATTERN.findall(text),
            )
    for candidate in analysis.all_candidates:
        candidate.prod_mentions = sorted(
            path
            for path, identifiers in index.items()
            if candidate.name in identifiers and path != candidate.file
        )
