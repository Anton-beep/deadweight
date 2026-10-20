"""The libclang binding: one parse call, and the decls, refs and diagnostics it yields.

These tests run against whatever libclang the host has, and are skipped when the
search finds none, so the unit suite still runs on a machine with no clang at all.
The fixture tree under ``tests/fixtures/tiny`` is the smallest one that exercises
every cursor kind the walker records.
"""

from __future__ import annotations

from pathlib import Path
from typing import TYPE_CHECKING

import pytest

from deadweight import cindex, discover

if TYPE_CHECKING:
    from deadweight.model import Decl, Ref

FIXTURES = Path(__file__).parent.parent / "fixtures" / "tiny"

ARGS = ["-std=c++17", "-I", str(FIXTURES)]
"""What a compile entry for the fixture would carry: the dialect and the include."""

FREE_USR = "c:@F@free_function#I#"
"""A free function's USR, which is canonical across every translation unit."""


def try_load() -> cindex.LibClang | None:
    """The host's libclang, or ``None`` when there is none to test against."""
    try:
        return discover.load()
    except (discover.LibClangError, OSError):
        return None


LIB = try_load()

requires_libclang = pytest.mark.skipif(LIB is None, reason="no libclang found")


@pytest.fixture(scope="module")
def lib() -> cindex.LibClang:
    """The discovered libclang; every test that needs one is skipped without it."""
    assert LIB is not None
    return LIB


def parse(lib: cindex.LibClang) -> tuple[list[Decl], list[Ref]]:
    """The fixture unit, asserting on the way that it parsed cleanly."""
    decls, refs, diagnostics, ok = lib.parse(FIXTURES / "tiny.cpp", ARGS)
    assert ok, diagnostics
    return decls, refs


def one(decls: list[Decl], usr: str) -> Decl:
    """The single declaration the unit recorded under ``usr``."""
    matches = [decl for decl in decls if decl.usr == usr]
    assert len(matches) == 1
    return matches[0]


@requires_libclang
def test_parses_a_free_function_with_its_usr(lib: cindex.LibClang) -> None:
    """A definition is recorded once, under clang's canonical identity."""
    decls, _ = parse(lib)
    decl = one(decls, FREE_USR)
    assert decl.kind == "function"
    assert decl.name == "free_function"
    assert decl.file.endswith("tiny.cpp")
    assert decl.span.contains(decl.loc)
    assert decl.span.end > decl.span.start
    assert "int" in decl.type_text


@requires_libclang
def test_a_definition_outranks_the_declaration_that_precedes_it(
    lib: cindex.LibClang,
) -> None:
    """The header's declaration and the .cpp's definition are one record."""
    decls, _ = parse(lib)
    destructor = one(decls, "c:@S@Widget@F@~Widget#")
    assert destructor.file.endswith("tiny.cpp")
    assert "defaulted" in destructor.markers


@requires_libclang
def test_virtual_method_is_marked_virtual(lib: cindex.LibClang) -> None:
    """Both an explicit ``virtual`` and an ``override`` carry the marker."""
    decls, _ = parse(lib)
    assert "virtual" in one(decls, "c:@S@Widget@F@draw#1").markers
    assert "virtual" in one(decls, "c:@S@Panel@F@update#1").markers


@requires_libclang
def test_pure_virtual_method_is_marked_pure(lib: cindex.LibClang) -> None:
    """A bodyless ``= 0`` declaration is recorded for the marker, not the body."""
    decls, _ = parse(lib)
    markers = one(decls, "c:@S@Widget@F@update#1").markers
    assert {"virtual", "pure"} <= markers


@requires_libclang
def test_defaulted_and_deleted_members_are_marked(lib: cindex.LibClang) -> None:
    """Members clang calls declarations are recorded when they carry a marker.

    ``= default`` and ``= delete`` are not definitions by clang's reckoning, but the
    keeper ladder has to see them, so the marker is what makes them recordable.
    """
    decls, _ = parse(lib)
    assert "defaulted" in one(decls, "c:@S@Widget@F@Widget#").markers
    assert "deleted" in one(decls, "c:@S@Widget@F@forbid#").markers


@requires_libclang
def test_special_member_kinds_are_reported(lib: cindex.LibClang) -> None:
    """The four kinds the analysis never proposes, each with its own name."""
    decls, _ = parse(lib)
    assert one(decls, "c:@S@Widget@F@Widget#").kind == "constructor"
    assert one(decls, "c:@S@Widget@F@~Widget#").kind == "destructor"
    assert one(decls, "c:@S@Widget@F@operator bool#1").kind == "conversion"
    assert one(decls, "c:@S@Widget@F@operator=#&1$@S@Widget#").kind == "assignment"


@requires_libclang
def test_call_expression_yields_a_reference_to_the_callee_usr(
    lib: cindex.LibClang,
) -> None:
    """One call is one reference site, at the caller's line, keyed by the callee."""
    decls, refs = parse(lib)
    caller = one(decls, "c:@F@caller#")
    sites = [ref for ref in refs if ref.usr == FREE_USR and ref.file.endswith(".cpp")]
    assert len(sites) == 1
    assert sites[0].loc.line == caller.loc.line
    assert caller.span.contains(sites[0].loc)


@requires_libclang
def test_references_to_anything_but_functions_are_not_recorded(
    lib: cindex.LibClang,
) -> None:
    """Parameter and class references are the analysis's noise, not its edges."""
    _, refs = parse(lib)
    assert {ref.usr for ref in refs} == {
        FREE_USR,
        "c:tiny.cpp@aN@F@file_local#I#",
    }


@requires_libclang
def test_a_clean_unit_reports_no_diagnostics(lib: cindex.LibClang) -> None:
    """The fixture compiles without a diagnostic, so the run is not poisoned."""
    _, _, diagnostics, ok = lib.parse(FIXTURES / "tiny.cpp", ARGS)
    assert ok
    assert diagnostics == []


@requires_libclang
def test_unparsable_unit_reports_diagnostics_and_not_ok(
    lib: cindex.LibClang,
    tmp_path: Path,
) -> None:
    """A unit that does not parse says so, which is what makes the run exit 2."""
    broken = tmp_path / "broken.cpp"
    broken.write_text("int broken( { return 1 }\n")
    _, _, diagnostics, ok = lib.parse(broken, ARGS)
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
