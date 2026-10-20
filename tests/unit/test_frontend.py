"""The compile database, the worker that parses one unit, and the pool around it.

Two properties here are the reason the module exists rather than a loop over
``LibClang.parse``. The first is the working directory: libclang resolves a relative
``-I`` against the process's current directory, not against the compilation entry's,
so a worker has to ``chdir`` into the entry's directory and put it back afterwards.
The second is the cache: a unit whose facts are already on disk is never handed to a
parser, and a unit that does not parse poisons the run as an ``error`` record rather
than disappearing from it.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from deadweight import FACTS_VERSION, cindex, discover, facts, frontend
from deadweight.frontend import TranslationUnit
from deadweight.model import STATUS_ERROR, STATUS_OK, Decl, Facts, Loc, Span

FIXTURES = Path(__file__).parent.parent / "fixtures"
RELINC = FIXTURES / "relinc"
REPO_ROOT = Path(__file__).resolve().parents[2]
"""The scan root the tests run from, which carries no ``include/`` directory."""


def try_load() -> cindex.LibClang | None:
    """The host's libclang, or ``None`` when there is none to test against."""
    try:
        return discover.load()
    except (discover.LibClangError, OSError):
        return None


LIB = try_load()

requires_libclang = pytest.mark.skipif(LIB is None, reason="no libclang found")


def library() -> cindex.LibClang:
    """The discovered libclang, for the tests that are skipped without one."""
    assert LIB is not None
    return LIB


def relinc_units() -> list[TranslationUnit]:
    """The fixture's single compile entry, loaded the way a scan loads it."""
    return frontend.load_compile_commands(RELINC / "compile_commands.json")


def broken_unit(tmp_path: Path) -> TranslationUnit:
    """A unit whose source cannot be parsed, to prove the failure is reported."""
    source = tmp_path / "broken.cpp"
    source.write_text("int broken( { return 1 }\n")
    return TranslationUnit(path=str(source), directory=str(tmp_path), arguments=[])


def cached_facts(unit: TranslationUnit) -> Facts:
    """Facts a previous run could have left behind, recognisable in the result."""
    start = Loc(file="src/rel.cpp", line=3, col=1)
    return Facts(
        tu=unit.path,
        status=STATUS_OK,
        compiler={"libclang": "22.1.8", "version": "clang version 22.1.8"},
        decls=[
            Decl(
                usr="c:@F@rel#I#",
                kind="function",
                name="from_the_cache",
                file="src/rel.cpp",
                loc=start,
                span=Span(start=start, end=Loc("src/rel.cpp", 3, 31)),
                markers=frozenset(),
                type_text="int (int)",
            ),
        ],
    )


@requires_libclang
def test_compile_commands_relative_include_resolves(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """``-Iinclude`` is relative to the entry's directory, so the worker must chdir.

    The fixture's header lives under ``include/`` and its source under ``src/``, so
    the flag only resolves after moving into the directory the entry names. Parsing
    from the scan root instead fails — which is the graph-corrupting mistake the
    worker's ``chdir`` exists to prevent.
    """
    units = relinc_units()
    assert len(units) == 1
    unit = units[0]
    assert Path(unit.directory) == RELINC.resolve()
    assert Path(unit.path) == (RELINC / "src" / "rel.cpp").resolve()
    assert unit.arguments == ["-std=c++17", "-Iinclude", "-c"]

    monkeypatch.chdir(REPO_ROOT)
    _, _, diagnostics, ok = library().parse(Path(unit.path), unit.arguments)
    assert not ok, "the include directory is only valid from the entry's directory"
    assert diagnostics

    monkeypatch.chdir(unit.directory)
    decls, _, diagnostics, ok = library().parse(Path(unit.path), unit.arguments)
    assert ok, diagnostics
    assert [decl.name for decl in decls if decl.kind == "function"] == ["rel"]


@requires_libclang
def test_worker_restores_its_cwd(monkeypatch: pytest.MonkeyPatch) -> None:
    """The worker parses from the entry's directory and leaves the caller's alone."""
    unit = relinc_units()[0]
    monkeypatch.chdir(REPO_ROOT)
    before = Path.cwd()
    parsed = frontend.parse_unit(unit, library().path)
    assert Path.cwd() == before
    assert parsed.status == STATUS_OK, parsed.diags
    assert parsed.tu == unit.path
    assert parsed.compiler["libclang"] == library().version


@requires_libclang
def test_worker_restores_its_cwd_when_the_parse_fails(tmp_path: Path) -> None:
    """A unit that does not parse still puts the working directory back."""
    unit = broken_unit(tmp_path)
    before = Path.cwd()
    parsed = frontend.parse_unit(unit, library().path)
    assert Path.cwd() == before
    assert parsed.status == STATUS_ERROR
    assert parsed.diags


@requires_libclang
def test_parse_failure_marks_the_facts_and_propagates(tmp_path: Path) -> None:
    """An unparsable unit comes back as ``error`` facts, so the run can exit 2."""
    unit = broken_unit(tmp_path)
    results = frontend.scan(
        [unit],
        jobs=1,
        cache_dir=None,
        libclang_path=library().path,
        use_cache=False,
        tree_stamp="",
    )
    assert len(results) == 1
    assert results[0].tu == unit.path
    assert results[0].status == STATUS_ERROR
    assert results[0].diags


@requires_libclang
def test_scan_parses_a_unit_in_a_worker() -> None:
    """The pool is the path a real scan takes, relative include flag and all."""
    unit = relinc_units()[0]
    results = frontend.scan(
        [unit],
        jobs=1,
        cache_dir=None,
        libclang_path=library().path,
        use_cache=False,
        tree_stamp="",
    )
    assert len(results) == 1
    assert results[0].status == STATUS_OK, results[0].diags
    assert [decl.name for decl in results[0].decls] == ["rel"]
    assert results[0].tu == unit.path


@requires_libclang
def test_cache_hit_skips_the_parser(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Facts already on disk are answered without a parse, and without a pool."""
    unit = relinc_units()[0]
    cache_dir = tmp_path / "cache"
    stamp = facts.source_tree_stamp(RELINC, ("src", "include"))
    key = facts.cache_key(unit, FACTS_VERSION, library().version, stamp)
    facts.write(cache_dir, key, cached_facts(unit))

    def refuse(*_args: object, **_kwargs: object) -> Facts:
        message = "the cache hit must not reach the parser"
        raise AssertionError(message)

    monkeypatch.setattr(frontend, "parse_unit", refuse)
    results = frontend.scan(
        [unit],
        jobs=1,
        cache_dir=cache_dir,
        libclang_path=library().path,
        use_cache=True,
        tree_stamp=stamp,
    )
    assert results == [cached_facts(unit)]


def test_classify_splits_production_from_test_units() -> None:
    """A unit belongs to the root its source lives under; the rest are not scanned."""
    prod = TranslationUnit("/repo/client/framer.cpp", "/repo/client", [])
    test = TranslationUnit("/repo/tests/framer_test.cpp", "/repo/tests", [])
    other = TranslationUnit("/repo/third_party/zlib.cpp", "/repo/third_party", [])
    classified = frontend.classify([prod, test, other], ("client",), ("tests",))
    assert classified == ([prod], [test])


def test_an_arguments_entry_keeps_its_flags_and_drops_the_rest(tmp_path: Path) -> None:
    """The compiler, the output operand and the source are not parser arguments."""
    database = tmp_path / "compile_commands.json"
    database.write_text(
        '[{"directory": "build", "file": "../src/rel.cpp",'
        ' "arguments": ["c++", "-std=c++17", "-Iinclude", "-c",'
        ' "../src/rel.cpp", "-o", "rel.o"]}]',
    )
    (tmp_path / "build").mkdir()
    units = frontend.load_compile_commands(database)
    assert units == [
        TranslationUnit(
            path=str(tmp_path / "src" / "rel.cpp"),
            directory=str(tmp_path / "build"),
            arguments=["-std=c++17", "-Iinclude", "-c"],
        ),
    ]


def test_an_entry_without_a_command_is_an_error(tmp_path: Path) -> None:
    """A compile database the tool cannot read is refused, never half-loaded."""
    database = tmp_path / "compile_commands.json"
    database.write_text('[{"directory": ".", "file": "rel.cpp"}]')
    with pytest.raises(frontend.CompileCommandsError):
        frontend.load_compile_commands(database)


def test_a_malformed_compile_database_is_an_error(tmp_path: Path) -> None:
    """Invalid JSON names the file it could not read."""
    database = tmp_path / "compile_commands.json"
    database.write_text("{not json")
    with pytest.raises(frontend.CompileCommandsError):
        frontend.load_compile_commands(database)
