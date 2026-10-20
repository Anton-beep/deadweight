"""The linked program's view: gc reports, binary symbols and the cross-check.

Every fixture here follows the real output of the tool it names - binutils ld, gold
and lld `--print-gc-sections`, `nm --defined-only` and `readelf -rW` - with the
linker's own argv[0] abbreviated to its tool name and its column padding collapsed to
one space, which are the only parts the parser never reads.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

from deadweight.linker import (
    collect_test_binaries,
    gc_section_symbol,
    load_gc_facts,
    parse_gc_report,
    parse_nm_defined,
    parse_readelf_symbols,
    production_objects,
    run_cross_check,
)
from deadweight.model import Analysis, Candidate, ObjectSymbols

if TYPE_CHECKING:
    from pathlib import Path

    import pytest

BARE_SECTIONS = 3
"""The `.text` sections in the fixture reports that name no symbol."""

GC_LINES = 13
"""Every `unused section` line the three fixture reports hold."""

UNUSED = "_Z9dead_codei"
USED = "_Z8live_onei"

NM_DEFINED_TEXT = """\
0000000000000000 T main
0000000000000000 T _Z8live_onei
0000000000000000 T _Z9dead_codei
0000000000000010 b .bss.demo
                 U _Z8live_onei
"""

READELF_TEXT = """\
Relocation section '.rela.text.main' at offset 0x1d0 contains 1 entry:
0000000000000015 0000000600000004 R_X86_64_PLT32 0000000000000000 _Z8live_onei - 4
Relocation section '.rela.eh_frame' at offset 0x1e8 contains 1 entry:
0000000000000020 0000000200000002 R_X86_64_PC32 0000000000000000 .text._Z9dead_codei + 0
"""

# binutils ld: the section and the file are single-quoted.
GNU_LD_GC_REPORT = """\
ld: removing unused section '.data' in file 'Scrt1.o'
ld: removing unused section '.text' in file 'crti.o'
ld: removing unused section '.rodata.cst4' in file 'Scrt1.o'
ld: removing unused section '.text._Z9dead_codei' in file 'a.o'
ld: removing unused section '.text._Z8live_onei.part.0' in file 'a.o'
"""

# gold says "removing unused section from '<section>'", bfd says "of".
GOLD_GC_REPORT = """\
ld.gold: removing unused section from '.sframe' in file 'Scrt1.o'
ld.gold: removing unused section from '.text' in file 'crti.o'
ld.gold: removing unused section from '.text._Z9dead_codei' in file 'a.o'
ld.gold: removing unused section from '.text._Z8live_onei.part.0' in file 'a.o'
"""

# lld names the object and parenthesizes the section, with no quoting at all.
LLD_GC_REPORT = """\
removing unused section Scrt1.o:(.data)
removing unused section crti.o:(.text)
removing unused section a.o:(.text._Z9dead_codei)
removing unused section a.o:(.text.unlikely._Z8live_onei)
"""


def make_candidate(
    name: str,
    *,
    file: str = "client/a.cpp",
    kind: str = "function",
    type_text: str = "int (int)",
) -> Candidate:
    """One candidate, as the analysis would have built it."""
    return Candidate(
        usr=f"c:@F@{name}#i",
        name=name,
        kind=kind,
        file=file,
        line=10,
        col=1,
        type_text=type_text,
    )


def make_analysis(*candidates: Candidate) -> Analysis:
    """An analysis whose candidates are the only bucket the cross-check reads."""
    return Analysis(
        components={},
        candidates=list(candidates),
        keepers=[],
        special_members=[],
        dead_anywhere=[],
        entry_points=[],
        ambiguous=[],
        unresolved_targets=0,
    )


def demangled_name(symbol: str) -> str:
    """The two fixture symbols, as ``c++filt`` would print them."""
    names = {UNUSED: "dead_code(int)", USED: "live_one(int)"}
    return names.get(symbol, symbol)


def test_parse_nm_defined_keeps_defined_symbols() -> None:
    """An undefined or unnamed symbol is not a definition the ladder can use."""
    assert parse_nm_defined(NM_DEFINED_TEXT) == {"main", USED, UNUSED}


def test_parse_readelf_symbols_keeps_named_symbols_only() -> None:
    """A relocation against a section (``.text.foo``) resolves to no function."""
    assert parse_readelf_symbols(READELF_TEXT) == {USED}


def test_gc_section_symbol_strips_prefixes_and_suffixes() -> None:
    """The linker's section name has to become the symbol whose code it holds."""
    assert gc_section_symbol(".text._Z3foov") == "_Z3foov"
    assert gc_section_symbol(".text.startup._Z3foov") == "_Z3foov"
    assert gc_section_symbol(".text.unlikely._Z3foov") == "_Z3foov"
    assert gc_section_symbol(".text._Z3foov.part.0") == "_Z3foov"
    assert gc_section_symbol(".text._Z3foov.constprop.2") == "_Z3foov"
    assert gc_section_symbol(".text._Z3foov.isra.1") == "_Z3foov"
    assert gc_section_symbol(".text._Z3foov.llvm.1a2b3c") == "_Z3foov"
    assert gc_section_symbol(".text") is None
    assert gc_section_symbol(".data._Z3foov") is None
    assert gc_section_symbol(".rodata.str1.1") is None


def test_parse_gc_report_handles_gnu_ld_gold_and_lld_formats() -> None:
    """Three linkers, three spellings of one fact: this section was discarded."""
    text = GNU_LD_GC_REPORT + GOLD_GC_REPORT + LLD_GC_REPORT
    report = parse_gc_report(text)
    assert set(report.symbols) == {UNUSED, USED}
    assert report.symbols[UNUSED].endswith(UNUSED)
    # One bare `.text` per linker; every `unlikely`/`part` section still names a
    # function, so only the bare ones are counted as sections without a symbol.
    assert report.bare_sections == BARE_SECTIONS
    assert report.lines == GC_LINES


def test_stale_objects_are_dropped_from_the_cross_check(tmp_path: Path) -> None:
    """A rename leaves the old object behind; it must not refute a verdict."""
    build = tmp_path / "CMakeFiles" / "x.dir" / "client"
    build.mkdir(parents=True)
    (build / "a.cpp.o").write_text("", encoding="utf-8")
    (build / "renamed.cpp.o").write_text("", encoding="utf-8")
    (tmp_path / "loose.o").write_text("", encoding="utf-8")
    assert production_objects(tmp_path, {"client/a.cpp"}) == [build / "a.cpp.o"]


def test_collect_test_binaries_keeps_only_executables(tmp_path: Path) -> None:
    """A test binary is an executable named ``test_*``, not any file beside one."""
    binaries = tmp_path / "bin"
    binaries.mkdir()
    executable = binaries / "test_alpha"
    executable.write_text("", encoding="utf-8")
    executable.chmod(0o755)
    (binaries / "test_alpha.txt").write_text("", encoding="utf-8")
    helper = binaries / "helper"
    helper.write_text("", encoding="utf-8")
    helper.chmod(0o755)
    assert collect_test_binaries(binaries) == [executable]
    assert collect_test_binaries(None) == []


def test_load_gc_facts_refuses_a_report_without_function_sections(
    tmp_path: Path,
) -> None:
    """A link without ``-ffunction-sections`` cannot support a verdict."""
    report = tmp_path / "gc.txt"
    report.write_text(
        "ld: removing unused section '.text' in file 'a.o'\n",
        encoding="utf-8",
    )
    notes: list[str] = []
    assert load_gc_facts([report], [], None, None, notes) is None
    assert any("-ffunction-sections" in note for note in notes)


def test_cross_check_returns_the_candidates_the_link_refutes(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The refuted candidates are the exit-3 evidence, so the check returns them."""
    gc_report = tmp_path / "gc.txt"
    gc_report.write_text(
        f"ld: removing unused section '.text.{UNUSED}' in file 'a.o'\n",
        encoding="utf-8",
    )
    binary = tmp_path / "app"
    binary.write_text("", encoding="utf-8")
    build = tmp_path / "build"
    build.mkdir()
    monkeypatch.setattr(
        "deadweight.linker.collect_binary_symbols",
        lambda _binary: {USED},
    )
    monkeypatch.setattr(
        "deadweight.linker.collect_object_symbols",
        lambda _objects, _jobs: ObjectSymbols(defined={USED: {"/b/client/a.cpp.o"}}),
    )
    monkeypatch.setattr(
        "deadweight.linker.demangle_symbols",
        lambda symbols: {symbol: demangled_name(symbol) for symbol in symbols},
    )
    candidates = [make_candidate("dead_code"), make_candidate("live_one")]
    analysis = make_analysis(*candidates)
    refuted = run_cross_check(
        analysis,
        build,
        {"client/a.cpp"},
        jobs=1,
        gc_reports=[gc_report],
        gc_test_reports=[],
        prod_binary=binary,
        test_binaries_dir=None,
    )
    assert [item.name for item in refuted] == ["live_one"]
    assert candidates[0].cross_check == "gc-confirmed"
    assert candidates[1].cross_check == "gc-refuted"


def test_cross_check_is_unavailable_without_a_linker_view(tmp_path: Path) -> None:
    """No objects and no gc report means no verdict, which the report has to say."""
    candidate = make_candidate("dead_code")
    analysis = make_analysis(candidate)
    refuted = run_cross_check(
        analysis,
        tmp_path / "build",
        set(),
        jobs=1,
        gc_reports=[],
        gc_test_reports=[],
        prod_binary=tmp_path / "app",
        test_binaries_dir=None,
    )
    assert refuted == ()
    assert candidate.cross_check == "unavailable"
    assert candidate.gc == "unavailable"
