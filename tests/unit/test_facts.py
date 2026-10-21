"""The facts cache: one JSON record per key, and the key that names it.

The key is the whole contract. It has to change when anything the parse depended on
changes — the unit, its flags, the facts layout, the libclang that produced it, and
the content of the sources, headers included — because a stale record that is read as
fresh turns into a wrong verdict rather than a slow scan.
"""

from __future__ import annotations

import json
import shutil
from pathlib import Path

from deadweight import FACTS_VERSION, facts
from deadweight.frontend import TranslationUnit
from deadweight.model import STATUS_OK, Decl, Facts, Loc, Ref, Span

FIXTURES = Path(__file__).parent.parent / "fixtures"
RELINC = FIXTURES / "relinc"

LIBRARY_VERSION = "22.1.8"


def make_decl(usr: str = "c:@F@rel#I#", name: str = "rel") -> Decl:
    """A declaration with a span, so the round trip has something to lose."""
    start = Loc(file="src/rel.cpp", line=3, col=1)
    end = Loc(file="src/rel.cpp", line=3, col=31)
    return Decl(
        usr=usr,
        kind="function",
        name=name,
        file="src/rel.cpp",
        loc=start,
        anchor=start,
        span=Span(start=start, end=end),
        markers=frozenset({"virtual"}),
        type_text="int (int)",
    )


def make_facts(tu: str = "src/rel.cpp") -> Facts:
    """One unit's facts with a declaration, a reference and a diagnostic."""
    return Facts(
        tu=tu,
        status=STATUS_OK,
        compiler={"libclang": LIBRARY_VERSION, "version": "clang version 22.1.8"},
        decls=[make_decl()],
        refs=[
            Ref(
                usr="c:@F@rel#I#",
                file="src/main.cpp",
                loc=Loc("src/main.cpp", 7, 3),
            ),
        ],
        diags=["a note the front end kept"],
    )


def make_unit(
    path: str = "/repo/src/rel.cpp",
    directory: str = "/repo",
    arguments: list[str] | None = None,
) -> TranslationUnit:
    """A compile entry as the loader hands it to the pool."""
    return TranslationUnit(
        path=path,
        directory=directory,
        arguments=["-std=c++17", "-Iinclude"] if arguments is None else arguments,
    )


def key_for(
    unit: TranslationUnit,
    stamp: str = "stamp",
    *,
    library_version: str = LIBRARY_VERSION,
    facts_version: int = FACTS_VERSION,
) -> str:
    """The cache key for a unit under one set of conditions."""
    return facts.cache_key(unit, facts_version, library_version, stamp)


def test_cache_roundtrip(tmp_path: Path) -> None:
    """What a scan writes, the next run reads back whole."""
    original = make_facts()
    facts.write(tmp_path, "a" * 64, original)
    restored = facts.read(tmp_path, "a" * 64)
    assert restored == original
    assert restored is not None
    assert restored.decls[0].markers == frozenset({"virtual"})


def test_a_missing_key_reads_as_no_facts(tmp_path: Path) -> None:
    """An empty cache is a miss, not an error."""
    assert facts.read(tmp_path, "b" * 64) is None
    assert facts.read(tmp_path / "never-created", "b" * 64) is None


def test_a_corrupt_record_reads_as_no_facts(tmp_path: Path) -> None:
    """A truncated file from a killed scan is a miss, not a crash."""
    (tmp_path / ("c" * 64 + ".json")).write_text('{"facts_version": 1, "tu"')
    assert facts.read(tmp_path, "c" * 64) is None


def test_a_record_from_another_facts_version_reads_as_no_facts(
    tmp_path: Path,
) -> None:
    """The record layout versions independently of the package, so it is checked."""
    payload = {
        "facts_version": FACTS_VERSION + 1,
        "tu": "src/rel.cpp",
        "status": STATUS_OK,
        "compiler": {},
        "decls": [],
        "refs": [],
        "diags": [],
    }
    (tmp_path / ("d" * 64 + ".json")).write_text(json.dumps(payload))
    assert facts.read(tmp_path, "d" * 64) is None


def test_cache_key_follows_the_source_content(tmp_path: Path) -> None:
    """Editing a file under a scanned root invalidates every key in that tree."""
    source = tmp_path / "src" / "rel.cpp"
    source.parent.mkdir()
    source.write_text("int rel(int value) { return value + 1; }\n")
    unit = make_unit(path=str(source), directory=str(tmp_path))
    first = key_for(unit, facts.source_tree_stamp(tmp_path, ("src",)))
    source.write_text("int rel(int value) { return value + 2; }\n")
    second = key_for(unit, facts.source_tree_stamp(tmp_path, ("src",)))
    assert first != second


def test_cache_key_covers_flags_and_libclang_version() -> None:
    """The unit's flags and the library's identity are part of the key."""
    unit = make_unit()
    baseline = key_for(unit)
    assert key_for(make_unit(arguments=["-std=c++20", "-Iinclude"])) != baseline
    assert key_for(make_unit(arguments=["-Iinclude", "-std=c++17"])) != baseline
    assert key_for(make_unit(path="/repo/src/other.cpp")) != baseline
    assert key_for(make_unit(directory="/elsewhere")) != baseline
    assert key_for(unit, library_version="18.1.8") != baseline
    assert key_for(unit, facts_version=FACTS_VERSION + 1) != baseline
    assert key_for(unit, "another stamp") != baseline


def test_the_stamp_reaches_a_header_outside_the_unit_directories(
    tmp_path: Path,
) -> None:
    """The reason ``scan`` takes the stamp: only the caller knows the roots.

    The relinc fixture keeps ``rel.hpp`` under ``include/``, which no translation
    unit's directory contains. A stamp derived from the unit directories would miss
    it, so a header edit would keep reading stale facts; a stamp over the configured
    roots catches it.
    """
    tree = tmp_path / "relinc"
    shutil.copytree(RELINC, tree)
    unit = TranslationUnit(
        path=str(tree / "src" / "rel.cpp"),
        directory=str(tree),
        arguments=["-std=c++17", "-Iinclude"],
    )
    roots = ("src", "include")
    first = key_for(unit, facts.source_tree_stamp(tree, roots))
    (tree / "include" / "rel.hpp").write_text("int rel(int value);\nint rel2();\n")
    second = key_for(unit, facts.source_tree_stamp(tree, roots))
    assert first != second


def test_the_stamp_covers_sources_and_ignores_everything_else(
    tmp_path: Path,
) -> None:
    """A README edit is not a source edit, and a root that does not exist is empty."""
    (tmp_path / "src").mkdir()
    (tmp_path / "src" / "rel.cpp").write_text("int rel(int value) { return value; }\n")
    (tmp_path / "README.md").write_text("before\n")
    baseline = facts.source_tree_stamp(tmp_path, ("src",))
    (tmp_path / "README.md").write_text("after\n")
    assert facts.source_tree_stamp(tmp_path, ("src",)) == baseline
    assert facts.source_tree_stamp(tmp_path, ("does-not-exist",)) == (
        facts.source_tree_stamp(tmp_path, ())
    )
