"""Library discovery: the ordered search, and the reason every rejected path lost.

Nothing here needs libclang itself. The probe is the seam these tests replace, so the
search order, the newest-version preference and the reason strings are exercised on a
machine with no clang installed at all. The one test that does load a real shared
library uses the C maths library, because it is the shortest way to show that a
library which is not libclang is a load error rather than a silent fallback.
"""

from __future__ import annotations

import ctypes.util
from typing import TYPE_CHECKING

import pytest

from deadweight import cindex, discover

if TYPE_CHECKING:
    from collections.abc import Callable

LDCONFIG_TEXT = """\
\tlibclang-19.so.1 (libc6,x86-64) => /usr/lib/llvm-19/lib/libclang-19.so.1
\tlibclang-cpp.so.22.1 (libc6,x86-64) => /usr/lib/libclang-cpp.so.22.1
\tlibclang.so.22.1 (libc6,x86-64) => /usr/lib/libclang.so.22.1
\tlibform.so.6 (libc6,x86-64) => /lib/x86_64-linux-gnu/libform.so.6
"""

EXPLICIT = "/opt/llvm-22/lib/libclang.so"

FOUND = "/usr/lib/llvm-19/lib/libclang-19.so.1"

LIBM = ctypes.util.find_library("m")

requires_libm = pytest.mark.skipif(LIBM is None, reason="no C maths library to load")


def fake_library(path: str, version: str = "19.1.7") -> cindex.LibClang:
    """A probe record for a library that exports everything the scanner needs."""
    return cindex.LibClang(
        path=path,
        version=version,
        banner=f"clang version {version}",
        api=dict.fromkeys(cindex.REQUIRED_SYMBOLS, True),
    )


def accepting(path: str) -> cindex.LibClang:
    """A probe that stands in for a libclang at every path it is handed."""
    return fake_library(path)


def refusing(reasons: dict[str, Exception]) -> Callable[[str], cindex.LibClang]:
    """A probe that raises the reason a test names for the paths it names."""

    def probe(path: str) -> cindex.LibClang:
        error = reasons.get(path)
        if error is None:
            return fake_library(path)
        raise error

    return probe


def no_text() -> str:
    """No ``ldconfig`` output."""
    return ""


def no_paths() -> list[str]:
    """No paths from the wheel."""
    return []


def no_prefixes(directory: str, pattern: str) -> list[str]:  # noqa: ARG001
    """A host that matches none of the install prefixes."""
    return []


def installed_under(paths: list[str]) -> Callable[[str, str], list[str]]:
    """A host that carries ``paths`` under the LLVM prefix and nothing else."""

    def expand(directory: str, pattern: str) -> list[str]:
        if (directory, pattern) == ("/usr/lib", "llvm-*/lib/libclang.so*"):
            return list(paths)
        return []

    return expand


def bare_host(monkeypatch: pytest.MonkeyPatch) -> None:
    """Discovery sees only what a test hands it: no host library, no wheel."""
    monkeypatch.setattr(discover, "_ldconfig_text", no_text)
    monkeypatch.setattr(discover, "_wheel_paths", no_paths)
    monkeypatch.setattr(discover, "_probe", accepting)
    monkeypatch.setattr(discover, "_expand", no_prefixes)


def test_ldconfig_output_yields_libclang_sonames() -> None:
    """Only the C API's sonames are candidates; the C++ one is not libclang-c."""
    assert discover.parse_ldconfig(LDCONFIG_TEXT) == [
        "/usr/lib/llvm-19/lib/libclang-19.so.1",
        "/usr/lib/libclang.so.22.1",
    ]


def test_ldconfig_output_that_is_not_a_cache_listing_is_ignored() -> None:
    """Malformed output is searched past, not trusted."""
    assert discover.parse_ldconfig("not a cache listing\n\n") == []


def test_the_explicit_path_is_searched_first(monkeypatch: pytest.MonkeyPatch) -> None:
    """``--libclang`` outranks every automatic tier."""
    bare_host(monkeypatch)
    monkeypatch.setattr(discover, "_expand", installed_under([FOUND]))
    found = discover.candidates(EXPLICIT, {})
    assert [(candidate.path, candidate.origin) for candidate in found] == [
        (EXPLICIT, "explicit"),
        (FOUND, "system-path"),
    ]
    assert found[0].accepted


def test_the_environment_variable_follows_the_explicit_path(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """``DEADWEIGHT_LIBCLANG`` outranks the automatic tiers and follows the flag."""
    bare_host(monkeypatch)
    found = discover.candidates(EXPLICIT, {discover.ENV_VAR: FOUND})
    assert [(candidate.path, candidate.origin) for candidate in found] == [
        (EXPLICIT, "explicit"),
        (FOUND, "environment"),
    ]


def test_ldconfig_output_finds_a_versioned_soname(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A versioned soname only ``ldconfig -p`` knows about is still found."""
    bare_host(monkeypatch)
    monkeypatch.setattr(discover, "_ldconfig_text", lambda: LDCONFIG_TEXT)
    found = discover.candidates(None, {})
    assert [candidate.path for candidate in found] == [
        "/usr/lib/libclang.so.22.1",
        "/usr/lib/llvm-19/lib/libclang-19.so.1",
    ]
    assert {candidate.origin for candidate in found} == {"ldconfig"}
    assert all(candidate.accepted for candidate in found)


def test_an_llvm_prefix_listing_finds_a_versioned_soname(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """``/usr/lib/llvm-*/lib`` is searched without ``ldconfig`` being usable."""
    bare_host(monkeypatch)
    monkeypatch.setattr(discover, "_expand", installed_under([FOUND]))
    found = discover.candidates(None, {})
    assert [candidate.path for candidate in found] == [FOUND]
    assert found[0].origin == "system-path"


def test_the_newest_installed_libclang_is_preferred(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Version numbers compared as numbers, so 19 outranks 9 and 10."""
    bare_host(monkeypatch)
    installed = [
        "/usr/lib/llvm-9/lib/libclang.so.9",
        "/usr/lib/llvm-10/lib/libclang.so.10",
        FOUND,
    ]
    monkeypatch.setattr(discover, "_expand", installed_under(installed))
    assert discover.candidates(None, {})[0].path == FOUND


def test_every_rejected_candidate_carries_a_reason(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A rejection is data the operator is shown, never a silent skip."""
    bare_host(monkeypatch)
    monkeypatch.setattr(
        discover,
        "_probe",
        refusing(
            {
                "/usr/lib/libclang.so": OSError("cannot open shared object file"),
                FOUND: cindex.MissingSymbolError(
                    FOUND,
                    ["clang_parseTranslationUnit2"],
                ),
            },
        ),
    )
    monkeypatch.setattr(
        discover,
        "_expand",
        installed_under(["/usr/lib/libclang.so", FOUND]),
    )
    found = {candidate.path: candidate for candidate in discover.candidates(None, {})}
    assert not found["/usr/lib/libclang.so"].accepted
    assert not found[FOUND].accepted
    assert "cannot open shared object file" in found["/usr/lib/libclang.so"].reason
    assert "clang_parseTranslationUnit2" in found[FOUND].reason


def test_a_named_library_that_cannot_be_used_is_fatal(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A named path is authoritative: no silent scan with a different library."""
    bare_host(monkeypatch)
    monkeypatch.setattr(discover, "_expand", installed_under([FOUND]))
    monkeypatch.setattr(
        discover,
        "_probe",
        refusing({EXPLICIT: OSError("cannot open shared object file")}),
    )
    with pytest.raises(discover.LibClangError, match="cannot open shared object file"):
        discover.load(EXPLICIT)


def test_load_returns_the_first_usable_candidate(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The winner is the first path in search order that the probe accepted."""
    bare_host(monkeypatch)
    monkeypatch.setattr(discover, "_expand", installed_under([FOUND]))
    assert discover.load().path == FOUND


def test_load_requires_the_requested_major_version(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """``--require-libclang`` mismatch is an error, not a quieter scan."""
    bare_host(monkeypatch)
    monkeypatch.setattr(discover, "_expand", installed_under([FOUND]))
    with pytest.raises(discover.LibClangError, match=r"19\.1\.7"):
        discover.load(required_major=22)


def test_load_without_a_candidate_names_the_ways_to_supply_one(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """An empty search explains the flag and the variable instead of guessing."""
    bare_host(monkeypatch)
    with pytest.raises(discover.LibClangError, match=discover.ENV_VAR):
        discover.load()


@requires_libm
def test_missing_required_symbol_is_a_load_error() -> None:
    """A shared library that is not libclang is rejected, and says what it lacks."""
    assert LIBM is not None
    with pytest.raises(discover.LibClangError, match="does not export"):
        discover.load(LIBM)
