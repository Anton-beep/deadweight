"""The command line: the two subcommands, the merged settings, and the exit contract.

The interesting part of this module is not argument parsing but the order of its
decisions. A configuration file is read before anything is parsed, a missing
compilation database refuses the run before libclang is loaded, and an incomplete
graph writes no report at all: a report that silently omitted a translation unit would
read exactly like a clean one. Everything else — which bucket goes to stdout, which
three files ``--out`` writes, and which of ``0``/``1``/``2``/``3``/``4`` comes back —
follows from those rules.
"""

from __future__ import annotations

import argparse
import fnmatch
import hashlib
import os
import shutil
import sys
import tempfile
from dataclasses import dataclass, replace
from datetime import date
from enum import IntEnum
from pathlib import Path
from typing import TYPE_CHECKING, TypeVar

from . import (
    __version__,
    analysis,
    cindex,
    config,
    discover,
    doctor,
    facts,
    frontend,
    linker,
    report,
)
from .model import Analysis, Facts, Loc, RunMeta, Span, under_any

if TYPE_CHECKING:
    from collections.abc import Sequence
    from typing import NoReturn

    from .frontend import TranslationUnit
    from .model import Candidate

_T = TypeVar("_T")
"""The type a flag and its configuration fallback share."""

_DEFAULT_DATABASES = ("compile_commands.json", "build/compile_commands.json")
"""Where a run looks for a compilation database when nothing names one."""

_CACHE_DIRECTORY = "deadweight"
"""The directory under the user's cache root that holds every scan's facts."""


class ExitCode(IntEnum):
    """The published exit codes, in precedence order (2, then 3, then 4, then 0)."""

    CLEAN = 0
    USAGE = 1
    INCOMPLETE = 2
    CONTRADICTION = 3
    GATE = 4


class UsageError(Exception):
    """A command line or configuration the run refuses to act on."""


class _Parser(argparse.ArgumentParser):
    """An argument parser whose errors are usage errors rather than ``SystemExit(2)``.

    argparse's own exit code for a mistyped flag is 2, which here means "incomplete
    graph"; an operator would have no way to tell a typo from a failed parse.
    """

    def error(self, message: str) -> NoReturn:
        raise UsageError(message)


@dataclass(frozen=True)
class _Settings:
    """One run's inputs, after the flags have been merged over the configuration."""

    scan_root: Path
    prod_roots: tuple[str, ...]
    test_roots: tuple[str, ...]
    exclude: tuple[str, ...]
    compile_commands: Path
    out: Path | None
    formats: tuple[str, ...]
    jobs: int
    gc_reports: tuple[Path, ...]
    gc_report_tests: tuple[Path, ...]
    prod_binary: Path | None
    test_binaries_dir: Path | None
    prod_build_dir: Path
    fail_on: str
    ignores: tuple[config.Ignore, ...]
    libclang: str | None
    require_libclang: int | None
    cascade: bool
    cross_check: bool
    use_cache: bool
    strict_ignores: bool


def main(argv: list[str] | None = None) -> int:
    """Run one command and answer with the process exit code."""
    try:
        args = parse_args(argv)
        if args.command == "doctor":
            return int(_doctor(args))
        return int(_scan(args))
    except (
        UsageError,
        config.ConfigError,
        frontend.CompileCommandsError,
        discover.LibClangError,
        cindex.CindexError,
        OSError,
    ) as error:
        return int(_fail(error))


def parse_args(argv: Sequence[str] | None) -> argparse.Namespace:
    """Parse the command line into the namespace the two commands read."""
    parser = _Parser(
        prog="deadweight",
        description="Find C and C++ functions that only your tests reach.",
    )
    parser.add_argument(
        "--version",
        action="version",
        version=f"%(prog)s {__version__}",
    )
    commands = parser.add_subparsers(dest="command", required=True)
    _scan_flags(commands.add_parser("scan", help="analyse one tree"))
    _doctor_flags(commands.add_parser("doctor", help="report the environment"))
    return parser.parse_args(argv)


def _scan_flags(parser: argparse.ArgumentParser) -> None:
    """Declare ``deadweight scan``'s flag set."""
    parser.add_argument(
        "--compile-commands",
        default=None,
        metavar="PATH",
        help="the compilation database to read",
    )
    parser.add_argument(
        "--config",
        default=None,
        metavar="PATH",
        help="the configuration file to read instead of discovering "
        f"{config.CONFIG_NAME}",
    )
    parser.add_argument(
        "--prod",
        type=_roots,
        default=None,
        metavar="A,B",
        help="comma-separated production roots",
    )
    parser.add_argument(
        "--tests",
        type=_roots,
        default=None,
        metavar="C,D",
        help="comma-separated test roots",
    )
    parser.add_argument(
        "--exclude",
        action="append",
        default=None,
        metavar="GLOB",
        help="a glob of translation units never to scan; repeatable",
    )
    parser.add_argument(
        "--jobs",
        type=int,
        default=None,
        metavar="N",
        help="parser processes; defaults to the CPU count",
    )
    parser.add_argument(
        "--out",
        default=None,
        metavar="DIR",
        help="write report.md, report.json and report.tsv here",
    )
    parser.add_argument(
        "--format",
        type=_formats,
        default=None,
        metavar="md,json,tsv",
        help="the renderings --out writes",
    )
    parser.add_argument(
        "--gc-report",
        action="append",
        default=None,
        metavar="FILE",
        help="a production --print-gc-sections report; repeatable",
    )
    parser.add_argument(
        "--gc-report-tests",
        action="append",
        default=None,
        metavar="FILE",
        help="a test link's --print-gc-sections report; repeatable",
    )
    parser.add_argument(
        "--prod-binary",
        default=None,
        metavar="FILE",
        help="the linked production binary, to read surviving symbols from",
    )
    parser.add_argument(
        "--test-binaries-dir",
        default=None,
        metavar="DIR",
        help="a directory of linked test binaries",
    )
    parser.add_argument(
        "--prod-build-dir",
        default=None,
        metavar="DIR",
        help="the build tree the object-symbol ladder reads; defaults to the "
        "compilation database's directory",
    )
    parser.add_argument(
        "--libclang",
        default=None,
        metavar="PATH",
        help="the libclang shared library to load",
    )
    parser.add_argument(
        "--require-libclang",
        type=int,
        default=None,
        metavar="MAJOR",
        help="refuse to run against any other libclang major version",
    )
    parser.add_argument(
        "--no-cascade",
        action="store_true",
        help="report direct references only, not what they transitively reach",
    )
    parser.add_argument(
        "--no-cross-check",
        action="store_true",
        help="skip the linker and object-symbol tiers",
    )
    parser.add_argument(
        "--no-cache",
        action="store_true",
        help="parse every unit instead of reusing cached facts",
    )
    parser.add_argument(
        "--fail-on",
        choices=config.FAIL_ON_VALUES,
        default=None,
        help="the gate: exit 4 when it trips",
    )
    parser.add_argument(
        "--strict-ignores",
        action="store_true",
        help="exit 1 when an [[ignore]] entry is stale or expired",
    )


def _doctor_flags(parser: argparse.ArgumentParser) -> None:
    """Declare ``deadweight doctor``'s flag set."""
    parser.add_argument(
        "--compile-commands",
        default=None,
        metavar="PATH",
        help="read this compilation database and report what it holds",
    )
    parser.add_argument(
        "--libclang",
        default=None,
        metavar="PATH",
        help="the libclang shared library to load",
    )
    parser.add_argument(
        "--require-libclang",
        type=int,
        default=None,
        metavar="MAJOR",
        help="refuse to run against any other libclang major version",
    )


def _roots(text: str) -> tuple[str, ...]:
    """Split a comma-separated root list, refusing an empty one."""
    parts = tuple(part.strip() for part in text.split(",") if part.strip())
    if not parts:
        message = "expected at least one root"
        raise argparse.ArgumentTypeError(message)
    return parts


def _formats(text: str) -> tuple[str, ...]:
    """Split a comma-separated rendering list, refusing one that does not exist."""
    parts = _roots(text)
    for name in parts:
        if name not in config.FORMATS:
            known = ", ".join(config.FORMATS)
            message = f"unknown format {name!r}, expected one of {known}"
            raise argparse.ArgumentTypeError(message)
    return parts


def _scan(args: argparse.Namespace) -> ExitCode:
    """Run one scan: parse, analyse, cross-check, report, and answer the exit code."""
    settings = _settings(args)
    _require_library(settings)
    units = _units(settings)
    parsed = frontend.scan(
        units,
        jobs=settings.jobs,
        cache_dir=_cache_dir(settings.scan_root) if settings.use_cache else None,
        libclang_path=settings.libclang,
        use_cache=settings.use_cache,
        tree_stamp=facts.source_tree_stamp(
            settings.scan_root,
            settings.prod_roots + settings.test_roots,
        ),
    )
    findings = [
        _relative_facts(unit, record, settings.scan_root)
        for unit, record in zip(units, parsed, strict=True)
    ]
    result = analysis.analyze(
        findings,
        settings.prod_roots,
        settings.test_roots,
        cascade=settings.cascade,
    )
    analysis.annotate_prod_mentions(result, settings.scan_root, settings.prod_roots)
    incomplete = _incomplete(result, settings)
    if incomplete is not None:
        return incomplete
    contradicted = _cross_check(settings, result, findings)
    _without_checkout_paths(result, settings.scan_root)
    _apply_ignores(result, settings)
    code = _exit_code(settings, result, contradicted)
    _emit(result, settings)
    return code


def _settings(args: argparse.Namespace) -> _Settings:
    """Merge the flags over the configuration file, and resolve every path.

    A relative path from the command line is resolved against the scan root rather
    than against the working directory, so a run from a subdirectory names the same
    file as a run at the top of the tree.
    """
    start = Path.cwd()
    explicit = _given(args.config)
    # find_config answers where the scan root is; load_config reads the same file and
    # keeps its paths as written, so both calls see one discovery rule.
    found = config.find_config(start, explicit)
    loaded = config.load_config(start, explicit)
    scan_root = found.parent.resolve() if found is not None else start
    prod_roots = _pick(args.prod, loaded.prod_roots)
    if not prod_roots:
        message = "no production roots: pass --prod A,B or set [scan] prod_roots"
        raise UsageError(message)
    database = _database(
        scan_root,
        _pick(args.compile_commands, loaded.compile_commands),
    )
    jobs = args.jobs if args.jobs is not None else _default_jobs()
    if jobs < 1:
        message = f"--jobs must be at least 1, got {jobs}"
        raise UsageError(message)
    build_dir = (
        Path(args.prod_build_dir)
        if args.prod_build_dir is not None
        else database.parent
    )
    return _Settings(
        scan_root=scan_root,
        prod_roots=prod_roots,
        test_roots=_pick(args.tests, loaded.test_roots),
        exclude=tuple(_pick(args.exclude, loaded.exclude)),
        compile_commands=database,
        out=_resolve_optional(scan_root, _pick(args.out, loaded.out)),
        formats=_pick(args.format, loaded.format),
        jobs=jobs,
        gc_reports=_resolve_many(scan_root, _pick(args.gc_report, loaded.gc_reports)),
        gc_report_tests=_resolve_many(
            scan_root,
            _pick(args.gc_report_tests, loaded.gc_report_tests),
        ),
        prod_binary=_resolve_optional(
            scan_root,
            _pick(args.prod_binary, loaded.prod_binary),
        ),
        test_binaries_dir=_resolve_optional(
            scan_root,
            _pick(args.test_binaries_dir, loaded.test_binaries_dir),
        ),
        prod_build_dir=_resolve(scan_root, build_dir),
        fail_on=_pick(args.fail_on, loaded.fail_on),
        ignores=loaded.ignores,
        libclang=args.libclang,
        require_libclang=args.require_libclang,
        cascade=not args.no_cascade,
        cross_check=not args.no_cross_check,
        use_cache=not args.no_cache,
        strict_ignores=args.strict_ignores,
    )


def _pick(flag: _T | None, fallback: _T) -> _T:
    """Answer with the flag's value when it was given, and the file's otherwise."""
    return fallback if flag is None else flag


def _given(value: str | None) -> Path | None:
    """One optional path from the command line."""
    return None if value is None else Path(value)


def _database(scan_root: Path, value: Path | None) -> Path:
    """Find the compilation database, and refuse the run when there is none."""
    if value is None:
        for name in _DEFAULT_DATABASES:
            candidate = scan_root / name
            if candidate.is_file():
                return candidate.resolve()
        message = (
            "no compilation database: pass --compile-commands PATH or set [scan] "
            f"compile_commands (looked for {', '.join(_DEFAULT_DATABASES)})"
        )
        raise UsageError(message)
    database = _resolve(scan_root, value)
    if not database.is_file():
        message = f"the compilation database {database} does not exist"
        raise UsageError(message)
    return database


def _resolve(base: Path, value: str | Path) -> Path:
    """Make one path absolute against the scan root."""
    path = Path(value)
    return path if path.is_absolute() else base / path


def _resolve_optional(base: Path, value: str | Path | None) -> Path | None:
    """Make an optional path absolute against the scan root."""
    return None if value is None else _resolve(base, value)


def _resolve_many(base: Path, values: Sequence[str | Path]) -> tuple[Path, ...]:
    """Make every path in a list absolute against the scan root."""
    return tuple(_resolve(base, Path(value)) for value in values)


def _default_jobs() -> int:
    """Answer how many parser processes a run uses when ``--jobs`` is absent."""
    return max(os.cpu_count() or 1, 1)


def _require_library(settings: _Settings) -> None:
    """Refuse a libclang that is not the major version this run pinned."""
    if settings.require_libclang is not None:
        discover.load(settings.libclang, required_major=settings.require_libclang)


def _units(settings: _Settings) -> list[TranslationUnit]:
    """Return the units to parse: production first, then test, minus exclusions."""
    loaded = frontend.load_compile_commands(settings.compile_commands)
    kept = [
        unit
        for unit in loaded
        if not _excluded(_relative(unit.path, settings.scan_root), settings.exclude)
    ]
    prod, test = frontend.classify(kept, settings.prod_roots, settings.test_roots)
    return [*prod, *test]


def _excluded(path: str, patterns: tuple[str, ...]) -> bool:
    """Answer whether a scan-root-relative path matches any exclude pattern.

    The patterns are shell globs over the whole relative path, so ``third_party/**``
    drops a subtree and ``**/generated/**`` drops one wherever it appears.
    """
    return any(fnmatch.fnmatch(path, pattern) for pattern in patterns)


def _relative(path: Path | str, scan_root: Path) -> str:
    """Spell one path the way every path in the model is spelled."""
    return Path(os.path.relpath(str(path), scan_root)).as_posix()


def _relative_facts(unit: TranslationUnit, record: Facts, scan_root: Path) -> Facts:
    """Rewrite one facts record so every path is relative to the scan root.

    libclang spells a path the way the compile entry's directory spelled it, so that
    directory is what makes the path absolute before it is made relative. Two units
    that saw the same header then agree on its name, which is what lets the analysis
    group them by USR and what keeps an absolute path out of the report.
    """

    def where(path: str) -> str:
        """One file path, from the unit's spelling to the report's."""
        if not path:
            return ""
        return _relative(Path(unit.directory, path), scan_root)

    def loc(value: Loc) -> Loc:
        """One location, rewritten to the report's spelling."""
        return Loc(file=where(value.file), line=value.line, col=value.col)

    return replace(
        record,
        tu=where(record.tu),
        decls=[
            replace(
                decl,
                file=where(decl.file),
                loc=loc(decl.loc),
                span=Span(start=loc(decl.span.start), end=loc(decl.span.end)),
            )
            for decl in record.decls
        ],
        refs=[
            replace(ref, file=where(ref.file), loc=loc(ref.loc)) for ref in record.refs
        ],
    )


def _incomplete(result: Analysis, settings: _Settings) -> ExitCode | None:
    """Report every input the run could not read, and answer exit 2.

    This is the only failure path that writes nothing. A report built on a graph with
    a unit missing is worse than no report, because it reads as a clean one.
    """
    missing = [
        path
        for path in (*settings.gc_reports, *settings.gc_report_tests)
        if not path.is_file()
    ]
    if not result.failures and not missing:
        return None
    for failure in result.failures:
        print(
            f"deadweight: {failure['file']} did not parse: {failure['reason']}",
            file=sys.stderr,
        )
    for path in missing:
        print(f"deadweight: gc-report {path} does not exist", file=sys.stderr)
    print(
        "deadweight: the graph is incomplete, so no report was written",
        file=sys.stderr,
    )
    return ExitCode.INCOMPLETE


def _cross_check(
    settings: _Settings,
    result: Analysis,
    findings: list[Facts],
) -> tuple[Candidate, ...]:
    """Judge the candidates against the linked program, and return the contradicted.

    The production sources are the relativised paths the report already uses, which
    is the form the object-symbol ladder matches an object file against.
    """
    if not settings.cross_check:
        return ()
    prod_sources = {
        record.tu for record in findings if under_any(record.tu, settings.prod_roots)
    }
    return linker.run_cross_check(
        result,
        settings.prod_build_dir,
        prod_sources,
        jobs=settings.jobs,
        gc_reports=list(settings.gc_reports),
        gc_test_reports=list(settings.gc_report_tests),
        prod_binary=settings.prod_binary,
        test_binaries_dir=settings.test_binaries_dir,
    )


def _without_checkout_paths(result: Analysis, scan_root: Path) -> None:
    """Rewrite any absolute path a note quotes into its scan-root-relative form.

    The report must not carry the checkout's location, or two runs of the same tree
    would not produce the same bytes. The linker tier quotes the build tree it was
    handed and does not know where the tree starts, so the quoting is unwound here.
    """
    prefix = scan_root.as_posix()
    if not prefix or prefix == "/":
        return
    result.notes[:] = [
        note.replace(f"{prefix}/", "").replace(prefix, ".") for note in result.notes
    ]


def _apply_ignores(result: Analysis, settings: _Settings) -> None:
    """Suppress the allowlisted symbols, and refuse an allowlist that has rotted.

    ``--strict-ignores`` turns two kinds of rot into a usage error: an entry that
    matches nothing, and an entry whose ``until`` has passed. Both mean the allowlist
    no longer describes the tree it was written for.
    """
    if not settings.ignores:
        return
    stale: list[str] = []
    for ignore in settings.ignores:
        suppressed = _suppress(result, ignore.symbol)
        if suppressed:
            result.notes.append(
                f"ignore: {ignore.symbol!r} suppressed {suppressed} candidate(s), "
                f"reason: {ignore.reason}",
            )
        if not settings.strict_ignores:
            continue
        if not suppressed:
            stale.append(f"{ignore.symbol!r} matches no candidate")
        elif ignore.until is not None and ignore.until < date.today():  # noqa: DTZ011 -- an expiry is a calendar date, not an instant
            stale.append(
                f"{ignore.symbol!r} passed its until date {ignore.until.isoformat()}",
            )
    if stale:
        message = "stale [[ignore]] entries: " + "; ".join(stale)
        raise UsageError(message)


def _suppress(result: Analysis, symbol: str) -> int:
    """Remove every candidate the ``[[ignore]]`` symbol names."""
    suppressed = 0
    for bucket in (result.candidates, result.dead_anywhere):
        kept = [candidate for candidate in bucket if not _named(candidate, symbol)]
        suppressed += len(bucket) - len(kept)
        bucket[:] = kept
    return suppressed


def _named(candidate: Candidate, symbol: str) -> bool:
    """Answer whether an ``[[ignore]]`` symbol addresses this candidate.

    The match is exact. Matching the unqualified tail would let an entry written for
    ``topi::shim`` quiet an unrelated ``other::shim``, and that over-suppression is
    silent: the operator sees a candidate leave the report instead of an allowlist
    entry that no longer applies. ``qualified_name`` is empty in today's analysis, so
    a qualified spelling names nothing yet; ``--strict-ignores`` reports it as stale,
    which is the alarm the loose match would have no equivalent of.
    """
    return symbol in (candidate.name, candidate.qualified_name)


def _exit_code(
    settings: _Settings,
    result: Analysis,
    contradicted: tuple[Candidate, ...],
) -> ExitCode:
    """Decide the exit code, in the precedence order the contract states."""
    if contradicted:
        return ExitCode.CONTRADICTION
    if settings.fail_on == config.FAIL_ON_CANDIDATES and result.candidates:
        return ExitCode.GATE
    return ExitCode.CLEAN


def _emit(result: Analysis, settings: _Settings) -> None:
    """Print the markdown summary, or write the renderings ``--out`` asked for."""
    meta = _meta(settings)
    if settings.out is None:
        sys.stdout.write(report.render_markdown(result))
        return
    settings.out.mkdir(parents=True, exist_ok=True)
    written = []
    for name in settings.formats:
        filename, text = _render(name, result, meta)
        (settings.out / filename).write_text(text, encoding="utf-8")
        written.append(filename)
    print(
        f"deadweight: {len(result.candidates)} candidate(s), "
        f"{len(result.dead_anywhere)} dead-anywhere, "
        f"{len(result.keepers)} keeper(s); wrote {', '.join(written)}",
    )


def _render(
    name: str,
    result: Analysis,
    meta: RunMeta,
) -> tuple[str, str]:
    """Render one format under the filename it is written to."""
    if name == "json":
        return "report.json", report.render_json(result, meta)
    if name == "tsv":
        return "report.tsv", report.render_tsv(result)
    return "report.md", report.render_markdown(result)


def _meta(settings: _Settings) -> RunMeta:
    """Build the run metadata the report header prints.

    Every path is written the way the report writes one, relative to the scan root,
    so two runs of the same tree in different checkouts produce the same bytes.
    """
    return RunMeta(
        version=__version__,
        scan_root=".",
        compile_commands=_relative(settings.compile_commands, settings.scan_root),
        prod_roots=settings.prod_roots,
        test_roots=settings.test_roots,
        gc_reports=tuple(
            _relative(path, settings.scan_root) for path in settings.gc_reports
        ),
        gc_report_tests=tuple(
            _relative(path, settings.scan_root) for path in settings.gc_report_tests
        ),
        prod_binary=_optional_relative(settings.prod_binary, settings.scan_root),
        test_binaries_dir=_optional_relative(
            settings.test_binaries_dir,
            settings.scan_root,
        ),
        jobs=settings.jobs,
        cascade=settings.cascade,
        cross_check=settings.cross_check,
    )


def _optional_relative(path: Path | None, scan_root: Path) -> str:
    """Spell an optional path the report's way, or the empty string."""
    return "" if path is None else _relative(path, scan_root)


def _cache_dir(scan_root: Path) -> Path:
    """Name the directory this scan root's facts cache lives in.

    The cache lives under the user's cache directory rather than in the analysed tree:
    a scan must not add files to the repository it is judging. The digest keeps two
    trees apart without putting either path into the cache's own layout.
    """
    digest = hashlib.sha256(scan_root.as_posix().encode("utf-8")).hexdigest()
    return _cache_base() / _CACHE_DIRECTORY / digest[:16]


def _cache_base() -> Path:
    """Name the user's cache directory, falling back to the temporary directory."""
    xdg = os.environ.get("XDG_CACHE_HOME")
    if xdg:
        return Path(xdg)
    home = os.environ.get("HOME")
    if home:
        return Path(home) / ".cache"
    return Path(tempfile.gettempdir())


def _doctor(args: argparse.Namespace) -> ExitCode:
    """Report what the environment offers, and exit 0 when it is usable."""
    library = discover.load(args.libclang, required_major=args.require_libclang)
    if args.compile_commands is not None:
        database = Path(args.compile_commands)
        units = frontend.load_compile_commands(database)
        print(f"compilation database: {len(units)} translation unit(s)")
    sys.stdout.write(
        doctor.report(library, cross_check_available=_linker_tools()),
    )
    return ExitCode.CLEAN


def _linker_tools() -> bool:
    """Answer whether the ELF tier's tools are on PATH, so the tier can speak."""
    return all(shutil.which(tool) is not None for tool in ("nm", "readelf", "c++filt"))


def _fail(error: Exception) -> ExitCode:
    """Print one refusal on stderr and answer with the usage exit code."""
    print(f"deadweight: error: {error}", file=sys.stderr)
    return ExitCode.USAGE
