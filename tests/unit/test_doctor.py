"""The doctor's rendering: what it found, what it probed, and what it cannot do.

The report is built from a probe record, not from a loaded library, so these tests
run without libclang and pin the text a user pastes into an issue.
"""

from __future__ import annotations

from dataclasses import replace

from deadweight import cindex, discover, doctor

LIB = cindex.LibClang(
    path="/opt/llvm/lib/libclang.so",
    version="22.1.8",
    banner="clang version 22.1.8",
    api={
        "clang_createIndex": True,
        "clang_parseTranslationUnit2": True,
        "clang_CXXMethod_isDeleted": True,
    },
)
"""A modern libclang that exports every probed symbol."""

OLD = replace(
    LIB,
    path="/opt/llvm-15/lib/libclang.so",
    version="15.0.7",
    banner="clang version 15.0.7",
    api={**LIB.api, "clang_CXXMethod_isDeleted": False},
)
"""A libclang from before the floor, which has no ``isDeleted`` to call."""


def test_the_report_names_the_library_and_its_version() -> None:
    """The two facts every issue report needs are in the first lines."""
    text = doctor.report(LIB, cross_check_available=True)
    assert LIB.path in text
    assert LIB.version in text
    assert LIB.banner in text


def test_the_report_lists_every_probed_symbol() -> None:
    """The API probe is printed in full, so a missing entry point is visible."""
    text = doctor.report(LIB, cross_check_available=True)
    for name in LIB.api:
        assert name in text


def test_the_report_marks_a_missing_optional_symbol() -> None:
    """An optional symbol that is absent is named rather than silently dropped."""
    text = doctor.report(OLD, cross_check_available=True)
    assert "clang_CXXMethod_isDeleted" in text
    assert "missing" in text


def test_the_report_explains_the_source_fallback() -> None:
    """The pre-16 fallback is documented behaviour, so the doctor states it."""
    assert "source line" in doctor.report(OLD, cross_check_available=True)
    assert "source line" not in doctor.report(LIB, cross_check_available=True)


def test_the_report_warns_below_the_supported_floor() -> None:
    """A libclang older than 16 is called out, not quietly accepted."""
    assert "older than the supported floor" in doctor.report(
        OLD,
        cross_check_available=True,
    )
    assert "older than the supported floor" not in doctor.report(
        LIB,
        cross_check_available=True,
    )


def test_the_report_states_whether_the_linker_tier_is_reachable() -> None:
    """The linker tier is the evidence that confirms a candidate, not decoration."""
    assert "linker tier: available" in doctor.report(LIB, cross_check_available=True)
    assert "linker tier: unavailable" in doctor.report(
        LIB,
        cross_check_available=False,
    )


def test_the_report_prints_the_search_trail_with_each_reason() -> None:
    """A rejected path is named with why it lost, as the contract promises."""
    trail = [
        discover.Candidate(path=LIB.path, origin="explicit", lib=LIB),
        discover.Candidate(
            path="/opt/llvm-15/lib/libclang.so",
            origin="system-path",
            reason="libclang 15 is older than the supported floor",
        ),
    ]

    text = doctor.report(LIB, cross_check_available=True, candidates=trail)

    assert "## search trail" in text
    assert "`llvm-15/lib/libclang.so` (system-path): libclang 15 is older" in text
    assert "/opt/llvm-15" not in text


def test_a_loadable_path_the_search_did_not_choose_says_why_it_lost() -> None:
    """An unchosen but usable library lost to an earlier tier, and the trail says so."""
    trail = [
        discover.Candidate(path=LIB.path, origin="explicit", lib=LIB),
        discover.Candidate(path="/usr/lib/libclang.so.1", origin="ldconfig", lib=LIB),
    ]

    text = doctor.report(LIB, cross_check_available=True, candidates=trail)

    assert "usable, but an earlier candidate won the search" in text
    assert "/usr/lib" not in text


def test_the_search_trail_tells_two_libraries_of_one_name_apart() -> None:
    """Same-named libraries in two ``llvm-*`` prefixes stay distinguishable."""
    trail = [
        discover.Candidate(path=LIB.path, origin="explicit", lib=LIB),
        discover.Candidate(
            path="/usr/lib/llvm-16/lib/libclang.so.1",
            origin="system-path",
            lib=LIB,
        ),
        discover.Candidate(
            path="/usr/lib/llvm-18/lib/libclang.so.1",
            origin="system-path",
            lib=LIB,
        ),
    ]

    text = doctor.report(LIB, cross_check_available=True, candidates=trail)

    assert "`llvm-16/lib/libclang.so.1`" in text
    assert "`llvm-18/lib/libclang.so.1`" in text
    assert "/usr/lib/llvm-16" not in text


def test_the_search_trail_omits_the_chosen_library_and_empty_trails() -> None:
    """The chosen library is already printed verbatim, and no loss means no section."""
    chosen = [discover.Candidate(path=LIB.path, origin="explicit", lib=LIB)]

    assert "## search trail" not in doctor.report(
        LIB,
        cross_check_available=True,
        candidates=chosen,
    )
    assert "## search trail" not in doctor.report(LIB, cross_check_available=True)
