"""The linked program's view: object symbols, gc reports and the cross-check."""

from __future__ import annotations

import concurrent.futures
import os
import re
import shutil
import subprocess
from dataclasses import dataclass, field
from pathlib import Path
from typing import TYPE_CHECKING

from .model import Analysis, Candidate, GcFacts, ObjectSymbols
from .symbols import decide_candidate, symbol_key

if TYPE_CHECKING:
    from collections.abc import Sequence

CONTRADICTIONS = frozenset({"refuted", "gc-refuted"})
"""The verdicts that say the graph and the linked program disagree; the run exits 3."""

_NM_ADDRESSED_TOKENS = 3
"""``nm`` prints an address, a kind and a name for a defined symbol."""

_NM_BARE_TOKENS = 2
"""Without an address ``nm`` prints only the kind and the name."""

_READELF_MIN_TOKENS = 5
"""The fields ``readelf -rW`` prints before a relocation's symbol name."""

_HEX_RE = re.compile(r"^[0-9a-f]+$", re.IGNORECASE)

_OBJECT_SUFFIX = ".o"

_BUILD_DIR_MARKER = ".dir/"


@dataclass
class GcReport:
    """One linker's ``--print-gc-sections`` output, reduced to discarded symbols."""

    symbols: dict[str, str] = field(default_factory=dict)
    bare_sections: int = 0
    lines: int = 0


def parse_nm_defined(text: str) -> set[str]:
    """The symbol names an ``nm --defined-only --no-demangle`` dump defines."""
    symbols: set[str] = set()
    for line in text.splitlines():
        tokens = line.split()
        if len(tokens) >= _NM_ADDRESSED_TOKENS and _HEX_RE.match(tokens[0]):
            kind, name = tokens[1], tokens[2]
        elif len(tokens) == _NM_BARE_TOKENS:
            kind, name = tokens[0], tokens[1]
        else:
            continue
        if kind == "U" or not name or name.startswith("."):
            continue
        symbols.add(name)
    return symbols


def parse_readelf_symbols(text: str) -> set[str]:
    """Return the symbol names a ``readelf -rW`` dump references."""
    symbols: set[str] = set()
    for line in text.splitlines():
        tokens = line.split()
        if len(tokens) < _READELF_MIN_TOKENS:
            continue
        if not _HEX_RE.match(tokens[0]) or not _HEX_RE.match(tokens[1]):
            continue
        if not tokens[2].startswith("R_"):
            continue
        symbol = tokens[4]
        if symbol and not symbol.startswith("."):
            symbols.add(symbol)
    return symbols


def _object_source(path: Path) -> str | None:
    """The scan-root-relative source a CMake ``.dir`` object was compiled from."""
    text = path.as_posix()
    if not text.endswith(_OBJECT_SUFFIX) or _BUILD_DIR_MARKER not in text:
        return None
    relative = text.rsplit(_BUILD_DIR_MARKER, 1)[1]
    return relative[: -len(_OBJECT_SUFFIX)]


def production_objects(build_dir: Path, sources: set[str]) -> list[Path]:
    """The objects whose source translation unit is still one of the scanned ones.

    A rename or removal leaves the previous object behind in the build tree, and a
    reference in that stale object would refute a correct AST verdict, so it is
    dropped. ``sources`` holds scan-root-relative paths, the form every path in the
    model takes, which is also the form CMake's ``.dir`` object names carry.
    """
    return [
        path
        for path in sorted(build_dir.rglob(f"*{_OBJECT_SUFFIX}"))
        if _object_source(path) in sources
    ]


def collect_object_symbols(objects: Sequence[Path], jobs: int = 1) -> ObjectSymbols:
    """Read every current production object's defined and referenced symbols."""
    nm_tool = shutil.which("nm")
    readelf_tool = shutil.which("readelf")
    if nm_tool is None or readelf_tool is None:
        return ObjectSymbols()

    def inspect(path: Path) -> tuple[str, set[str], set[str]]:
        # Both tools are resolved with shutil.which and the operand is a path inside
        # the caller's build tree, so no part of this call is untrusted input.
        defined = subprocess.run(  # noqa: S603
            [nm_tool, "--defined-only", "--no-demangle", str(path)],
            capture_output=True,
            text=True,
            check=False,
        )
        referenced = subprocess.run(  # noqa: S603
            [readelf_tool, "-rW", str(path)],
            capture_output=True,
            text=True,
            check=False,
        )
        readelf_symbols: set[str] = set()
        if referenced.returncode == 0:
            readelf_symbols = parse_readelf_symbols(referenced.stdout)
        return str(path), parse_nm_defined(defined.stdout), readelf_symbols

    symbols = ObjectSymbols()
    with concurrent.futures.ThreadPoolExecutor(max_workers=max(jobs, 1)) as pool:
        for path, defined, referenced in pool.map(inspect, objects):
            for symbol in defined:
                symbols.defined.setdefault(symbol, set()).add(path)
            for symbol in referenced:
                symbols.referenced.setdefault(symbol, set()).add(path)
    return symbols


def demangle_symbols(symbols: list[str]) -> dict[str, str]:
    """Bulk-demangle with c++filt; the cross-check matches on demangled names."""
    demangler = shutil.which("c++filt")
    if not symbols or demangler is None:
        return {}
    completed = subprocess.run(  # noqa: S603
        [demangler, "--no-strip-underscore"],
        input="\n".join(symbols),
        capture_output=True,
        text=True,
        check=False,
    )
    if completed.returncode != 0:
        return {}
    names = completed.stdout.splitlines()
    return dict(zip(symbols, names, strict=False)) if len(names) == len(symbols) else {}


_GC_LINE_RE = re.compile(r"unused section", re.IGNORECASE)

_GC_QUOTED_RE = re.compile(r"'([^']+)'|\"([^\"]+)\"")

_GC_LLD_RE = re.compile(r"\(([^()]+)\)\s*$")

_GC_BARE_RE = re.compile(r"(?<![\w.])(\.text[\w.$]*)")

_GC_TEXT_PREFIXES = (
    ".text.startup.",
    ".text.unlikely.",
    ".text.hot.",
    ".text.exit.",
    ".text.",
)

_GC_SUFFIX_RE = re.compile(
    r"\.(part|constprop|isra)\.\d+$|\.(cold|hot)$|\.llvm\.[0-9a-f]+$",
)


def gc_section_symbol(section: str) -> str | None:
    """Map a ``.text.<mangled>`` section name to the symbol whose code it holds."""
    symbol = ""
    for prefix in _GC_TEXT_PREFIXES:
        if section.startswith(prefix):
            symbol = section[len(prefix) :]
            break
    if not symbol:
        return None
    symbol = _GC_SUFFIX_RE.sub("", symbol)
    return symbol or None


def _gc_discarded_section(line: str) -> str | None:
    """The ``.text`` section name a ``--print-gc-sections`` line names, if any."""
    for quoted in _GC_QUOTED_RE.findall(line):
        # `findall` on a pattern with groups is typed `list[Any]`; each element is
        # the (single-quoted, double-quoted) pair this pattern matches.
        token: str = quoted[0] or quoted[1]
        if token.startswith(".text"):
            return token
    lld = _GC_LLD_RE.search(line)
    if lld is not None and lld.group(1).startswith(".text"):
        return lld.group(1)
    bare = _GC_BARE_RE.search(line)
    return bare.group(1) if bare is not None else None


def parse_gc_report(text: str) -> GcReport:
    """Parse GNU ld, gold and lld ``--print-gc-sections`` output.

    The three linkers disagree on the sentence and on the quoting, not on the fact:
    all of them name the discarded section, and in a ``-ffunction-sections`` build
    that section name is the symbol.
    """
    report = GcReport()
    for line in text.splitlines():
        if not _GC_LINE_RE.search(line):
            continue
        report.lines += 1
        section = _gc_discarded_section(line)
        if section is None:
            continue
        symbol = gc_section_symbol(section)
        if symbol is None:
            report.bare_sections += 1
            continue
        report.symbols[symbol] = section
    return report


def collect_binary_symbols(binary: Path) -> set[str]:
    """Every symbol the linked binary defines, from the symtab and .dynsym."""
    nm_tool = shutil.which("nm")
    if nm_tool is None or not binary.is_file():
        return set()
    symbols: set[str] = set()
    for extra in (
        ["--defined-only", "--no-demangle"],
        ["-D", "--defined-only", "--no-demangle"],
    ):
        completed = subprocess.run(  # noqa: S603
            [nm_tool, *extra, str(binary)],
            capture_output=True,
            text=True,
            check=False,
        )
        if completed.returncode == 0:
            symbols |= parse_nm_defined(completed.stdout)
    return symbols


def collect_test_binaries(directory: Path | None) -> list[Path]:
    """Every ``test_*`` executable in a tests build directory."""
    if directory is None or not directory.is_dir():
        return []
    return sorted(
        path
        for path in directory.iterdir()
        if path.is_file() and path.name.startswith("test_") and os.access(path, os.X_OK)
    )


def _spell(path: Path, scan_root: Path) -> str:
    """Spell one path the way the report does: relative to the scan root.

    ``os.path.relpath`` answers a ``../``-prefixed path when the input is outside the
    tree, which keeps the checkout's location out of the report without mangling a
    sibling that merely shares a prefix with the scan root.
    """
    return Path(os.path.relpath(str(path), scan_root)).as_posix()


def _record_production_binary(
    facts: GcFacts,
    prod_binary: Path | None,
    notes: list[str],
    scan_root: Path,
) -> None:
    """Note whether the linked production binary can confirm that a symbol survived."""
    if prod_binary is None or not prod_binary.is_file():
        notes.append(
            "gc-report: linked production binary not found (pass --prod-binary); "
            "gc-refuted cannot be told apart from gc-absent",
        )
        return
    facts.kept = collect_binary_symbols(prod_binary)
    facts.binary_checked = bool(facts.kept)
    spelled = _spell(prod_binary, scan_root)
    if facts.binary_checked:
        notes.append(
            f"gc-report: {len(facts.kept)} symbols survived the production link "
            f"({spelled})",
        )
    else:
        notes.append(
            f"gc-report: {spelled} has no readable symbol table (stripped?); "
            "gc-refuted cannot be told apart from gc-absent",
        )


def _record_test_binaries(
    facts: GcFacts,
    test_binaries_dir: Path | None,
    gc_test_reports: list[Path],
    notes: list[str],
) -> None:
    """Note which test-side rungs the supplied test links and binaries can reach."""
    test_binaries = collect_test_binaries(test_binaries_dir)
    for binary in test_binaries:
        facts.tests_kept |= collect_binary_symbols(binary)
    if test_binaries:
        facts.tests_checked = True
        notes.append(
            f"gc-report (tests): {len(facts.tests_kept)} symbols survived "
            f"{len(test_binaries)} test binaries",
        )
    if gc_test_reports and not facts.tests_checked:
        notes.append(
            "gc-report (tests) has no binary symbol tables behind it "
            "(pass --test-binaries-dir): a symbol no supplied test link "
            "discarded is reported as gc-test-only on the weaker evidence that it "
            "was not discarded",
        )
    if facts.tests_checked:
        notes.append(
            "gc-report (tests) speaks only for the test binaries it was produced from: "
            "gc-dead/gc-not-in-tests mean those binaries do not reach the symbol",
        )


def load_gc_facts(  # noqa: PLR0913 -- one argument per distinct input of the report
    gc_reports: list[Path],
    gc_test_reports: list[Path],
    prod_binary: Path | None,
    test_binaries_dir: Path | None,
    notes: list[str],
    *,
    scan_root: Path,
) -> GcFacts | None:
    """Read the linker reports; None when they cannot support a verdict."""
    if not gc_reports and not gc_test_reports:
        return None
    facts = GcFacts()
    for path in gc_reports:
        report = parse_gc_report(path.read_text(encoding="utf-8", errors="replace"))
        facts.prod.update(report.symbols)
        facts.bare_sections += report.bare_sections
        notes.append(
            f"gc-report {_spell(path, scan_root)}: {len(report.symbols)} discarded "
            f"function section(s) in {report.lines} removal line(s)",
        )
    for path in gc_test_reports:
        report = parse_gc_report(path.read_text(encoding="utf-8", errors="replace"))
        facts.tests.update(report.symbols)
        notes.append(
            f"gc-report (tests) {_spell(path, scan_root)}: {len(report.symbols)} "
            f"discarded function section(s) in {report.lines} removal line(s)",
        )
    if not facts.prod:
        notes.append(
            "gc-report unusable: no .text.<symbol> sections in the production "
            "report - build that tree with -ffunction-sections -fdata-sections and "
            "link with -Wl,--gc-sections -Wl,--print-gc-sections",
        )
        return None
    _record_production_binary(facts, prod_binary, notes, scan_root)
    if facts.bare_sections:
        notes.append(
            f"gc-report: {facts.bare_sections} bare .text section(s) skipped "
            "(that link had no per-function section names)",
        )
    _record_test_binaries(facts, test_binaries_dir, gc_test_reports, notes)
    return facts


def _mark_unavailable(candidates: list[Candidate], note: str, notes: list[str]) -> None:
    """Record that the linker tier cannot speak for these candidates, and why."""
    notes.append(note)
    for candidate in candidates:
        candidate.cross_check = "unavailable"
        candidate.gc = "unavailable"


def _judge_candidates(
    candidates: list[Candidate],
    facts: ObjectSymbols,
    gc: GcFacts | None,
    demangled: dict[str, str],
) -> tuple[Candidate, ...]:
    """Apply both ladders to every candidate and return the contradicted ones."""
    index: dict[tuple[str, int], list[str]] = {}
    for symbol, qualified in demangled.items():
        key = symbol_key(qualified)
        if key is not None:
            index.setdefault(key, []).append(symbol)
    refuted: list[Candidate] = []
    for candidate in candidates:
        cross_check, detail, qualified, gc_status, gc_detail = decide_candidate(
            candidate,
            facts,
            index,
            gc,
            demangled,
        )
        candidate.cross_check = cross_check
        candidate.cross_check_detail = detail
        candidate.gc = gc_status
        candidate.gc_detail = gc_detail
        if qualified:
            candidate.qualified_name = qualified
        if cross_check in CONTRADICTIONS:
            refuted.append(candidate)
    return tuple(sorted(refuted, key=lambda candidate: candidate.sort_key))


def run_cross_check(  # noqa: PLR0913 -- one argument per distinct input of the link
    analysis: Analysis,
    prod_build_dir: Path,
    prod_sources: set[str],
    *,
    scan_root: Path,
    jobs: int,
    gc_reports: list[Path],
    gc_test_reports: list[Path],
    prod_binary: Path | None,
    test_binaries_dir: Path | None,
) -> tuple[Candidate, ...]:
    """Judge every candidate against the linked program, returning the contradictions.

    Two independent views: the linker's own garbage collection (when a
    ``--print-gc-sections`` report is supplied) and the object-file ladder. The GC
    view is primary because it is whole-program reachability, including virtual
    dispatch and function-pointer tables; the object ladder is the fallback and the
    resolver for an ambiguous GC match. The returned candidates are the ones the
    linked program contradicts, which is what makes the run exit 3. A run whose
    linker tier cannot speak marks every candidate ``unavailable`` and returns none.
    """
    candidates = analysis.all_candidates
    gc = load_gc_facts(
        gc_reports,
        gc_test_reports,
        prod_binary,
        test_binaries_dir,
        analysis.notes,
        scan_root=scan_root,
    )
    facts = ObjectSymbols()
    if prod_build_dir.is_dir():
        objects = production_objects(prod_build_dir, prod_sources)
        facts = collect_object_symbols(objects, jobs)
        if facts.defined:
            defining = len({path for paths in facts.defined.values() for path in paths})
            analysis.notes.append(
                f"linker cross-check over {defining} production objects and "
                f"{len(facts.defined)} defined symbols (an object whose scanned source "
                "is gone is skipped as stale)",
            )
        else:
            analysis.notes.append(
                f"nm/readelf found no readable objects under "
                f"{_spell(prod_build_dir, scan_root)}: "
                "gc-refuted cannot be told apart from gc-absent",
            )
    else:
        analysis.notes.append(
            f"production build tree {_spell(prod_build_dir, scan_root)} not found "
            "(pass --prod-build-dir): the nm/readelf ladder is unavailable",
        )
    if not facts.defined and gc is None:
        _mark_unavailable(
            candidates,
            "no linker evidence: pass --prod-build-dir, --gc-report or --prod-binary "
            "to cross-check the graph",
            analysis.notes,
        )
        return ()
    symbols = set(facts.defined) | (gc.symbols if gc else set())
    demangled = demangle_symbols(sorted(symbols))
    if not demangled:
        _mark_unavailable(
            candidates,
            "c++filt is not on PATH: candidates cannot be matched to linked symbols",
            analysis.notes,
        )
        return ()
    return _judge_candidates(candidates, facts, gc, demangled)
