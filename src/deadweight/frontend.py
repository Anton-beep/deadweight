"""The front end: a compilation database, one worker per unit, and the pool around it.

This module is where the tool decides *what* to parse and *from where*. A compilation
database becomes a list of :class:`TranslationUnit`s, each parsed in its own spawned
process so one pathological unit costs one record rather than the run. Two rules make
the result trustworthy:

* **The working directory.** libclang resolves a relative include flag against the
  process's current directory, not against the compilation entry's ``directory``, so a
  worker ``chdir``s into the entry's directory before parsing and restores the previous
  directory in a ``finally``. Skipping this produces a graph that looks plausible and
  is wrong on any tree whose database uses ``-Iinclude``.
* **Failures are records.** A unit that does not parse, or a worker that dies, yields a
  facts record with ``status: "error"`` and the diagnostic that says why. Nothing is
  skipped quietly, because a shorter list of units reads as a cleaner graph.

The cache key needs a stamp over the first-party sources, which is why
:func:`scan` takes one: only the caller knows the configured roots, and a stamp
derived from the units' own directories would miss a header kept beside them.
"""

from __future__ import annotations

import contextlib
import json
import multiprocessing
import os
import shlex
from concurrent.futures import ProcessPoolExecutor, as_completed
from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING

from . import FACTS_VERSION, cindex, discover, facts
from .model import STATUS_ERROR, STATUS_OK, Facts, under_any

if TYPE_CHECKING:
    from collections.abc import Iterator

_LIBRARIES: dict[str, cindex.LibClang] = {}
"""The libraries this process has loaded, so a worker loads libclang once."""

_OUTPUT_FLAG = "-o"
"""The flag whose operand is a build artifact the scanner never wants."""


class CompileCommandsError(Exception):
    """A compilation database the loader cannot use as written."""


@dataclass(frozen=True)
class TranslationUnit:
    """One compile entry: what to parse, from where, and with which flags.

    ``path`` is absolute — resolved against ``directory`` when the database spells it
    relatively — so a worker can change directory before parsing and still name its
    own input.
    """

    path: str
    directory: str
    arguments: list[str]


def load_compile_commands(path: Path) -> list[TranslationUnit]:
    """Read a compilation database into the units a scan can parse.

    ``command`` is split as a shell would split it and ``arguments`` is used as given;
    either way everything before the first flag (the compiler, and any launcher before
    it), the ``-o`` operand and the source file are dropped, because the parser takes
    the file separately and an argument list that names it again asks libclang for two
    inputs.
    """
    try:
        entries = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError) as error:
        message = f"cannot read the compilation database {path}: {error}"
        raise CompileCommandsError(message) from error
    if not isinstance(entries, list):
        message = f"the compilation database {path} is not a JSON list of entries"
        raise CompileCommandsError(message)
    base = path.parent.resolve()
    return [_unit(entry, base) for entry in entries]


def classify(
    units: list[TranslationUnit],
    prod_roots: tuple[str, ...],
    test_roots: tuple[str, ...],
) -> tuple[list[TranslationUnit], list[TranslationUnit]]:
    """Split the units into production and test, dropping everything else.

    A unit under both roots is production: the question is what production reaches,
    and scanning a production unit as if it were a test would invent a test-only
    verdict for everything it calls.
    """
    prod = [unit for unit in units if under_any(unit.path, prod_roots)]
    test = [
        unit
        for unit in units
        if not under_any(unit.path, prod_roots) and under_any(unit.path, test_roots)
    ]
    return prod, test


def parse_unit(unit: TranslationUnit, libclang_path: str | None = None) -> Facts:
    """Parse one unit from its entry's directory, and restore the caller's.

    This is the worker's whole body, deliberately importable so a test can run it in
    process. The ``finally`` matters as much as the ``chdir``: a unit that fails must
    not move the working directory of every unit parsed after it.
    """
    library = _libclang(libclang_path)
    previous = Path.cwd()
    try:
        os.chdir(unit.directory)
        decls, refs, diagnostics, ok = library.parse(Path(unit.path), unit.arguments)
    except OSError as error:
        return _failed(unit, f"cannot parse {unit.path}: {error}")
    finally:
        os.chdir(previous)
    return Facts(
        tu=unit.path,
        status=STATUS_OK if ok else STATUS_ERROR,
        compiler={"libclang": library.version, "version": library.banner},
        decls=decls,
        refs=refs,
        diags=diagnostics,
    )


# The keyword set is the scan contract rather than a bag of options worth splitting
# into a request object, so the parameter count is deliberate.
def scan(  # noqa: PLR0913
    units: list[TranslationUnit],
    *,
    jobs: int,
    cache_dir: Path | None,
    libclang_path: str | None,
    use_cache: bool,
    tree_stamp: str,
) -> list[Facts]:
    """Parse every unit, reusing cached facts, and answer in the order asked.

    ``tree_stamp`` is unused when ``use_cache`` is false; it stays required so a
    caller never has to branch on whether the cache is on. A cache hit never reaches
    the pool, and a worker that dies yields an ``error`` record rather than a shorter
    list.
    """
    if jobs < 1:
        message = f"jobs must be at least 1, got {jobs}"
        raise ValueError(message)
    library = _libclang(libclang_path)
    results: dict[int, Facts] = {}
    pending: list[tuple[int, TranslationUnit, str]] = []
    for index, unit in enumerate(units):
        key = _key(unit, library, tree_stamp)
        cached = facts.read(cache_dir, key) if use_cache and cache_dir else None
        if cached is None:
            pending.append((index, unit, key))
        else:
            results[index] = cached
    for index, parsed, key in _parse_all(pending, jobs, library.path):
        results[index] = parsed
        if use_cache and cache_dir and parsed.status == STATUS_OK:
            # The cache is an optimisation, so an unwritable directory costs the
            # next run a parse; it must never fail this one.
            with contextlib.suppress(OSError):
                facts.write(cache_dir, key, parsed)
    return [results[index] for index in range(len(units))]


def _parse_all(
    pending: list[tuple[int, TranslationUnit, str]],
    jobs: int,
    libclang_path: str,
) -> Iterator[tuple[int, Facts, str]]:
    """Parse the cache misses in a spawned pool, one record per unit."""
    if not pending:
        return
    # ``spawn`` and never ``fork``: libclang is already initialised in this process,
    # and forking after that can deadlock inside the AST library.
    context = multiprocessing.get_context("spawn")
    with ProcessPoolExecutor(max_workers=jobs, mp_context=context) as pool:
        futures = {
            pool.submit(parse_unit, unit, libclang_path): (index, unit, key)
            for index, unit, key in pending
        }
        for future in as_completed(futures):
            index, unit, key = futures[future]
            try:
                parsed = future.result()
            except Exception as error:  # noqa: BLE001
                # A worker that crashed, was killed, or could not hand its result
                # back has to poison the run; the alternative is a graph that
                # silently lost a translation unit and a report that reads as clean.
                yield index, _failed(unit, f"the parser worker failed: {error}"), key
                continue
            yield index, parsed, key


def _unit(entry: object, base: Path) -> TranslationUnit:
    """One compilation database entry as a unit, resolved against ``base``."""
    if not isinstance(entry, dict):
        message = "a compilation database entry is not a JSON object"
        raise CompileCommandsError(message)
    directory = _directory(entry.get("directory"), base)
    source = Path(_text(entry.get("file")))
    resolved = source if source.is_absolute() else directory / source
    return TranslationUnit(
        path=str(Path(os.path.normpath(resolved))),
        directory=str(directory),
        arguments=_arguments(_tokens(entry), source),
    )


def _directory(value: object, base: Path) -> Path:
    """The entry's working directory, resolved against the database's own location."""
    if value is None or value == "":
        return base
    named = Path(_text(value))
    return named if named.is_absolute() else Path(os.path.normpath(base / named))


def _tokens(entry: dict[str, object]) -> list[str]:
    """The entry's argument vector, whichever form the database spelled it in."""
    arguments = entry.get("arguments")
    if isinstance(arguments, list):
        return _from_the_first_flag([_text(item) for item in arguments])
    command = entry.get("command")
    if isinstance(command, str):
        return _from_the_first_flag(shlex.split(command))
    message = "a compilation database entry has neither 'command' nor 'arguments'"
    raise CompileCommandsError(message)


def _from_the_first_flag(tokens: list[str]) -> list[str]:
    """Drop everything before the first flag, launcher words included.

    ``argv[0]`` is the compiler, but a database may also spell ``ccache c++ ...`` or
    ``cd build && c++ ...``. libclang reads a leading non-flag token as an input
    file, so every token before the first flag goes, not just the first one.
    """
    for index, token in enumerate(tokens):
        if token.startswith("-"):
            return tokens[index:]
    return []


def _arguments(tokens: list[str], source: Path) -> list[str]:
    """The parser's argument list: no compiler, no output operand, no source file."""
    arguments: list[str] = []
    index = 0
    while index < len(tokens):
        token = tokens[index]
        if token.startswith(_OUTPUT_FLAG):
            # ``-o out.o`` and ``-oout.o`` both name an artifact this tool never
            # writes, and libclang refuses an output it was not asked to produce.
            index += 1 if len(token) > len(_OUTPUT_FLAG) else 2
            continue
        if not token.startswith("-") and Path(token).name == source.name:
            index += 1
            continue
        arguments.append(token)
        index += 1
    return arguments


def _key(unit: TranslationUnit, library: cindex.LibClang, tree_stamp: str) -> str:
    """The cache key this unit's facts live under for this run."""
    return facts.cache_key(unit, FACTS_VERSION, library.version, tree_stamp)


def _libclang(path: str | None) -> cindex.LibClang:
    """This process's libclang, loaded once and reused by every task it runs."""
    name = path or ""
    library = _LIBRARIES.get(name)
    if library is None:
        library = discover.load(path)
        _LIBRARIES[name] = library
    return library


def _failed(unit: TranslationUnit, reason: str) -> Facts:
    """A record that poisons the run, so a lost unit can never read as clean."""
    return Facts(tu=unit.path, status=STATUS_ERROR, diags=[reason])


def _text(value: object) -> str:
    """One JSON value as text, or a refusal naming what was found instead."""
    if not isinstance(value, str):
        found = type(value).__name__
        message = f"expected text in the compilation database, found {found}"
        raise CompileCommandsError(message)
    return value
