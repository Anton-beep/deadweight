"""The libclang binding: one parse call, and the decls, refs and diagnostics it yields.

These tests run against whatever libclang the host has, and are skipped when the
search finds none, so the unit suite still runs on a machine with no clang at all.
The fixture tree under ``tests/fixtures/tiny`` is the smallest one that exercises
every cursor kind the walker records.
"""

from __future__ import annotations

from pathlib import Path
from typing import TYPE_CHECKING

from deadweight import cindex, discover

if TYPE_CHECKING:
    from deadweight.model import Decl, Ref

FIXTURES = Path(__file__).parent.parent / "fixtures" / "tiny"

ARGS = ["-std=c++17", "-I", str(FIXTURES)]
"""What a compile entry for the fixture would carry: the dialect and the include."""

FREE_USR = "c:@F@free_function#I#"
"""A free function's USR, which is canonical across every translation unit."""

READ_USR = "c:@S@Reader@F@read#1"
"""A pure virtual's USR, shared by its declaration and its out-of-line body."""


def parse(libclang: cindex.LibClang) -> tuple[list[Decl], list[Ref]]:
    """The fixture unit, asserting on the way that it parsed cleanly."""
    decls, refs, diagnostics, ok = libclang.parse(FIXTURES / "tiny.cpp", ARGS)
    assert ok, diagnostics
    return decls, refs


def one(decls: list[Decl], usr: str) -> Decl:
    """The single declaration the unit recorded under ``usr``."""
    matches = [decl for decl in decls if decl.usr == usr]
    assert len(matches) == 1
    return matches[0]


def test_parses_a_free_function_with_its_usr(libclang: cindex.LibClang) -> None:
    """A definition is recorded once, under clang's canonical identity."""
    decls, _ = parse(libclang)
    decl = one(decls, FREE_USR)
    assert decl.kind == "function"
    assert decl.name == "free_function"
    assert decl.file.endswith("tiny.cpp")
    assert decl.span.contains(decl.loc)
    assert decl.span.end > decl.span.start
    assert "int" in decl.type_text


def test_a_definition_outranks_the_declaration_that_precedes_it(
    libclang: cindex.LibClang,
) -> None:
    """A pure declaration in the header and its out-of-line body are one record.

    Only ``pure``, ``= default`` and ``= delete`` make a bodyless declaration
    recordable, so a pure virtual defined outside its class is the pair that still
    yields two records under one USR and leaves the quoted body to precedence.
    """
    decls, _ = parse(libclang)
    method = one(decls, READ_USR)
    assert method.kind == "method"
    assert method.file.endswith("tiny.cpp")


def test_virtual_method_is_marked_virtual(libclang: cindex.LibClang) -> None:
    """Both an explicit ``virtual`` and an ``override`` carry the marker."""
    decls, _ = parse(libclang)
    assert "virtual" in one(decls, "c:@S@Widget@F@draw#1").markers
    assert "virtual" in one(decls, "c:@S@Panel@F@update#1").markers


def test_pure_virtual_method_is_marked_pure(libclang: cindex.LibClang) -> None:
    """A bodyless ``= 0`` declaration is recorded for the marker, not the body."""
    decls, _ = parse(libclang)
    markers = one(decls, "c:@S@Widget@F@update#1").markers
    assert {"virtual", "pure"} <= markers


def test_defaulted_and_deleted_members_are_marked(libclang: cindex.LibClang) -> None:
    """Members clang calls declarations are recorded when they carry a marker.

    ``= default`` and ``= delete`` are not definitions by clang's reckoning, but the
    keeper ladder has to see them, so the marker is what makes them recordable.
    """
    decls, _ = parse(libclang)
    assert "defaulted" in one(decls, "c:@S@Widget@F@Widget#").markers
    assert "deleted" in one(decls, "c:@S@Widget@F@forbid#").markers


def test_special_member_kinds_are_reported(libclang: cindex.LibClang) -> None:
    """The four kinds the analysis never proposes, each with its own name."""
    decls, _ = parse(libclang)
    assert one(decls, "c:@S@Widget@F@Widget#").kind == "constructor"
    assert one(decls, "c:@S@Widget@F@~Widget#").kind == "destructor"
    assert one(decls, "c:@S@Widget@F@operator bool#1").kind == "conversion"
    assert one(decls, "c:@S@Widget@F@operator=#&1$@S@Widget#").kind == "assignment"


def test_call_expression_yields_a_reference_to_the_callee_usr(
    libclang: cindex.LibClang,
) -> None:
    """One call is one reference site, at the caller's line, keyed by the callee."""
    decls, refs = parse(libclang)
    caller = one(decls, "c:@F@caller#")
    sites = [ref for ref in refs if ref.usr == FREE_USR and ref.file.endswith(".cpp")]
    assert len(sites) == 1
    assert sites[0].loc.line == caller.loc.line
    assert caller.span.contains(sites[0].loc)


def test_references_to_anything_but_functions_are_not_recorded(
    libclang: cindex.LibClang,
) -> None:
    """Parameter and class references are the analysis's noise, not its edges."""
    _, refs = parse(libclang)
    assert {ref.usr for ref in refs} == {
        FREE_USR,
        "c:tiny.cpp@aN@F@file_local#I#",
    }


def test_a_clean_unit_reports_no_diagnostics(libclang: cindex.LibClang) -> None:
    """The fixture compiles without a diagnostic, so the run is not poisoned."""
    _, _, diagnostics, ok = libclang.parse(FIXTURES / "tiny.cpp", ARGS)
    assert ok
    assert diagnostics == []


def test_unparsable_unit_reports_diagnostics_and_not_ok(
    libclang: cindex.LibClang,
    tmp_path: Path,
) -> None:
    """A unit that does not parse says so, which is what makes the run exit 2."""
    broken = tmp_path / "broken.cpp"
    broken.write_text("int broken( { return 1 }\n")
    _, _, diagnostics, ok = libclang.parse(broken, ARGS)
    assert not ok
    assert any("error" in diagnostic for diagnostic in diagnostics)


def test_the_deleted_marker_falls_back_to_the_source_line(tmp_path: Path) -> None:
    """Before libclang 16 the marker is read from the definition's line."""
    source = tmp_path / "deleted.cpp"
    source.write_text("struct S {\n  void gone() = delete;\n  void stays();\n};\n")
    assert cindex.deleted_in_source(str(source), 2)
    assert not cindex.deleted_in_source(str(source), 3)


def test_the_deleted_fallback_survives_an_unreadable_file(tmp_path: Path) -> None:
    """A path that cannot be read is a missing marker, never an exception."""
    assert not cindex.deleted_in_source(str(tmp_path / "gone.cpp"), 1)


def test_a_macro_expanded_declaration_is_anchored_where_it_was_written(
    tmp_path: Path,
    libclang: cindex.LibClang,
) -> None:
    """The anchor is the invocation site, not the point inside the macro body.

    The spelling location of a macro-expanded declaration points into the macro, and
    LLVM 16 and 18 report it differently from 20 and 22 — which is how two functions
    generated by one macro stopped looking like twins on the older libraries and were
    proposed for deletion.
    """
    source = tmp_path / "macro.cpp"
    source.write_text(
        "#define DEFINE(name) int name() { return 0; }\n"
        "DEFINE(made_one)\n"
        "#define DEFINE_TWINS(a, b) int a() { return 1; } int b() { return 2; }\n"
        "DEFINE_TWINS(twin_alpha, twin_beta)\n",
        encoding="utf-8",
    )
    resource = discover.resource_dir(libclang)
    args = [*ARGS[:1], *(("-resource-dir", resource) if resource else ())]

    decls, _refs, diagnostics, ok = libclang.parse(source, args)

    assert ok, diagnostics
    by_name = {decl.name: decl for decl in decls}
    single_invocation = 2
    twin_invocation = 4
    assert by_name["made_one"].anchor.line == single_invocation
    twins = (by_name["twin_alpha"], by_name["twin_beta"])
    assert twins[0].anchor == twins[1].anchor
    assert twins[0].anchor.line == twin_invocation
