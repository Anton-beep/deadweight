"""The vocabulary: locations, facts records, components, candidates and verdicts.

Every path in this module is relative to the scan root, which is what lets two runs
of the same tree produce the same report. Two versioned identifiers are involved and
neither is the package version: the report schema (:data:`SCHEMA`) and the facts
record version (:data:`deadweight.FACTS_VERSION`).
"""

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path

from . import REPORT_SCHEMA

SCHEMA = REPORT_SCHEMA
"""The report schema identifier, versioned independently of the package version."""

STATUS_OK = "ok"
"""A translation unit that parsed, so its facts can be trusted."""

STATUS_ERROR = "error"
"""A translation unit that did not parse; the run it belongs to is incomplete."""


@dataclass(frozen=True, order=True)
class Loc:
    """A source location; ``file`` is relative to the scan root."""

    file: str
    line: int
    col: int


@dataclass(frozen=True)
class Span:
    """A source range: ``start`` inclusive, ``end`` exclusive, within one file."""

    start: Loc
    end: Loc

    def contains(self, loc: Loc) -> bool:
        """True when ``loc`` falls inside this single-file range."""
        if loc.file != self.start.file or self.end.file != self.start.file:
            return False
        return self.start <= loc < self.end


@dataclass(frozen=True)
class Decl:
    """One function as a translation unit recorded it.

    ``usr`` is clang's canonical identity, so the same function recorded in another
    translation unit lands in the same component with no merge pass. ``kind`` is one
    of ``function``, ``method``, ``constructor``, ``destructor``, ``conversion`` or
    ``assignment``; ``markers`` carries ``virtual``, ``pure``, ``defaulted`` and
    ``deleted`` as clang reports them. The front end records only definitions, so a
    component's declarations are the function's body wherever it was seen.

    ``anchor`` is where the declaration was *written*: the macro's invocation site when
    a macro produced it, and the declaration itself otherwise. ``loc`` is the spelling
    location, which for a macro-expanded declaration points inside the macro body and
    moves between LLVM releases, so identity collisions are keyed on the anchor.
    """

    usr: str
    kind: str
    name: str
    file: str
    loc: Loc
    anchor: Loc
    span: Span
    markers: frozenset[str]
    type_text: str


@dataclass(frozen=True)
class Ref:
    """One reference site: the translation unit that saw it and where it is."""

    usr: str
    file: str
    loc: Loc


@dataclass
class Facts:
    """One translation unit's contribution to the graph, as the front end saw it."""

    tu: str
    status: str
    compiler: dict[str, str] = field(default_factory=dict)
    decls: list[Decl] = field(default_factory=list)
    refs: list[Ref] = field(default_factory=list)
    diags: list[str] = field(default_factory=list)


@dataclass
class Component:
    """One function, merged across every translation unit that recorded it."""

    usr: str
    names: set[str]
    decls: list[Decl]
    markers: set[str]


@dataclass(frozen=True)
class DefinitionSpan:
    """A production function body, used to attribute a reference to its caller."""

    usr: str
    name: str
    span: Span


@dataclass
class Candidate:
    """A function the report asks a human to judge, with the evidence for it."""

    usr: str
    name: str
    kind: str
    file: str
    line: int
    col: int
    type_text: str = ""
    test_refs: list[tuple[str, int]] = field(default_factory=list)
    reached_via: list[str] = field(default_factory=list)
    flags: list[str] = field(default_factory=list)
    reason: str = ""
    qualified_name: str = ""
    cross_check: str = "skipped"
    cross_check_detail: str = ""
    gc: str = "none"
    gc_detail: str = ""
    other_refs: int = 0
    direct_prod_refs: int = 0
    prod_mentions: list[str] = field(default_factory=list)

    @property
    def sort_key(self) -> tuple[str, int, int, str]:
        """Report order: by source location, then by name."""
        return (self.file, self.line, self.col, self.name)


@dataclass
class Analysis:
    """Everything the scan learned, in the buckets the report prints."""

    components: dict[str, Component]
    candidates: list[Candidate]
    keepers: list[Candidate]
    special_members: list[Candidate]
    dead_anywhere: list[Candidate]
    entry_points: list[Candidate]
    ambiguous: list[Candidate]
    unresolved_targets: int
    tu_counts: dict[str, int] = field(
        default_factory=lambda: {"prod": 0, "test": 0, "other": 0, "failed": 0},
    )
    failures: list[dict[str, str]] = field(default_factory=list)
    notes: list[str] = field(default_factory=list)

    @property
    def all_candidates(self) -> list[Candidate]:
        """Both buckets that propose a removal: test-only and unreachable alike."""
        return [*self.candidates, *self.dead_anywhere]


@dataclass
class RunMeta:
    """The inputs of one scan, printed into the report header."""

    tool: str = "deadweight"
    version: str = ""
    scan_root: str = ""
    compile_commands: str = ""
    prod_roots: tuple[str, ...] = ()
    test_roots: tuple[str, ...] = ()
    gc_reports: tuple[str, ...] = ()
    gc_report_tests: tuple[str, ...] = ()
    prod_binary: str = ""
    test_binaries_dir: str = ""
    jobs: int = 1
    cascade: bool = True
    cross_check: bool = True


@dataclass
class ObjectSymbols:
    """The linker's view of the production objects: what defines and uses what."""

    defined: dict[str, set[str]] = field(default_factory=dict)
    referenced: dict[str, set[str]] = field(default_factory=dict)


@dataclass
class GcFacts:
    """What the production link discarded, and what survived into the binary."""

    prod: dict[str, str] = field(default_factory=dict)
    tests: dict[str, str] = field(default_factory=dict)
    kept: set[str] = field(default_factory=set)
    tests_kept: set[str] = field(default_factory=set)
    bare_sections: int = 0
    binary_checked: bool = False
    tests_checked: bool = False

    @property
    def symbols(self) -> set[str]:
        """The symbols this link can speak about.

        ``kept`` and ``tests_kept`` are deliberately absent: they hold every symbol
        in the binaries, most of them third-party, and a namesake must never answer
        for a first-party candidate. They are consulted only after a symbol has been
        chosen from the smaller universe.
        """
        return set(self.prod) | set(self.tests)


def path_under(path: str, root: str) -> bool:
    """True when ``root`` appears as a whole component prefix of ``path``.

    A relative path that starts with ``..`` has left the scan root, so no root can
    claim it. Without this, a third-party header the facts spell as
    ``../heavy/stage-webrtc/include/logging/event.h`` matches the production root
    ``logging`` and is reported as first-party — a scan of topi attributed 41 libwebrtc
    headers to its own ``logging/`` tree before this rule existed.
    """
    parts = Path(path).parts
    root_parts = Path(root).parts
    escaped = not Path(path).is_absolute() and parts[:1] == ("..",)
    if escaped and root_parts[:1] != ("..",):
        # The path left the scan root, and this root does not itself start outside it.
        return False
    if not root_parts:
        return False
    return any(
        parts[start : start + len(root_parts)] == root_parts
        for start in range(len(parts) - len(root_parts) + 1)
    )


def under_any(path: str, roots: tuple[str, ...]) -> bool:
    """True when ``path`` lives under any of ``roots``."""
    return any(path_under(path, root) for root in roots)
