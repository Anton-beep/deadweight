"""The pure ladders: name and arity normalization, and every verdict they can reach."""

from __future__ import annotations

from typing import TYPE_CHECKING

from deadweight.model import Candidate, GcFacts, ObjectSymbols
from deadweight.symbols import (
    ast_symbol_key,
    candidate_verdict,
    cross_check_verdict,
    decide_candidate,
    gc_verdict,
    object_matches_source,
    parameter_arity,
    symbol_key,
    symbol_matches_key,
    symbol_matches_kind,
    unqualified_name,
)

if TYPE_CHECKING:
    from collections.abc import Sequence

UNUSED = "_ZN4demo12UnusedByProdEi"
USED = "_ZN4demo11UsedByProdEi"


def gc_facts(
    prod: Sequence[str] = (),
    tests: Sequence[str] = (),
    kept: Sequence[str] = (),
    tests_kept: Sequence[str] = (),
    *,
    tests_checked: bool = False,
) -> GcFacts:
    """What one production link discarded, as the gc report and binaries told it."""
    return GcFacts(
        prod={symbol: f".text.{symbol}" for symbol in prod},
        tests={symbol: f".text.{symbol}" for symbol in tests},
        kept=set(kept),
        tests_kept=set(tests_kept),
        binary_checked=True,
        tests_checked=tests_checked or bool(tests_kept),
    )


def test_parameter_arity_counts_top_level_parameters() -> None:
    """Nested commas belong to one parameter, and ``void`` means none."""
    expected = {
        "": 0,
        "void": 0,
        "int": 1,
        "int, char": 2,
        "std::pair<int, char>": 1,
        "std::function<void (int, char)>, int": 2,
    }
    assert {text: parameter_arity(text) for text in expected} == expected


def test_symbol_key_unqualifies_names_and_counts_parameters() -> None:
    """A demangled symbol reduces to the key the AST side can build as well."""
    assert symbol_key("demo::UnusedByProd(int)") == ("UnusedByProd", 1)
    assert symbol_key("topi::core::Widget::Config() const") == ("Config", 0)
    assert symbol_key("topi::A::B<int>::Run(int, char)") == ("Run", 2)
    assert symbol_key("demo::operator()(int)") == ("operator()", 1)
    assert symbol_key("somethingWithoutParens") is None
    assert unqualified_name("topi::A<int>::Run") == "Run"


def test_ast_symbol_key_reads_the_type_text() -> None:
    """The AST side builds the same key from a cursor's type spelling."""
    assert ast_symbol_key("UnusedByProd", "int (int)") == ("UnusedByProd", 1)
    assert ast_symbol_key("Run", "void () const") == ("Run", 0)
    assert ast_symbol_key("Fn", "not a type") is None
    assert symbol_matches_key("demo::UnusedByProd(int)", "UnusedByProd", 1)
    assert not symbol_matches_key("demo::UnusedByProd(int)", "UnusedByProd", 2)
    assert not symbol_matches_key("", "UnusedByProd", 1)


def test_object_matches_source_follows_the_object_name() -> None:
    """An object file is attributed to the source it was compiled from."""
    assert object_matches_source("build/client/a.cpp.o", "client/a.cpp")
    assert not object_matches_source("build/client/b.cpp.o", "client/a.cpp")
    assert not object_matches_source("build/client/a.cpp.o", None)


def test_symbol_kind_rejects_a_nested_constructor_of_the_same_name() -> None:
    """``JoinStateMachine::Config()`` must not answer for ``Config::Config()``."""
    nested_ctor = "topi::telemost::JoinStateMachine::Config::Config()"
    accessor = "topi::telemost::JoinStateMachine::Config() const"
    assert not symbol_matches_kind(nested_ctor, "Config", "method")
    assert symbol_matches_kind(nested_ctor, "Config", "constructor")
    assert symbol_matches_kind(accessor, "Config", "method")
    assert not symbol_matches_kind(accessor, "Config", "constructor")
    assert symbol_matches_kind("topi::Config::~Config()", "~Config", "destructor")


def test_cross_check_verdicts() -> None:
    """The object ladder can refute a candidate, never confirm one."""
    assert (
        cross_check_verdict(defined_in={"a.o"}, referenced_by=set(), exempt=set())
        == "ok"
    )
    assert (
        cross_check_verdict(defined_in=set(), referenced_by=set(), exempt=set())
        == "absent"
    )
    assert (
        cross_check_verdict(defined_in={"a.o"}, referenced_by={"b.o"}, exempt={"a.o"})
        == "refuted"
    )
    assert (
        cross_check_verdict(defined_in={"a.o"}, referenced_by={"a.o"}, exempt={"a.o"})
        == "refuted-same-tu"
    )


def test_candidate_verdict_prefers_the_candidates_own_object() -> None:
    """A namesake in another object must not answer for this candidate."""
    facts = ObjectSymbols(
        defined={"_Z3Fooi": {"/b/x.dir/client/a.cpp.o"}},
        referenced={"_Z3Fooi": set()},
    )
    index = {("Foo", 1): ["_Z3Fooi"]}
    demangled = {"_Z3Fooi": "topi::Foo(int)"}
    verdict, detail, qualified = candidate_verdict(
        "Foo", "method", 1, "client/a.cpp", 0, index, facts, demangled,
    )
    assert (verdict, detail, qualified) == ("ok", "_Z3Fooi", "topi::Foo(int)")


def test_refuted_cascade_is_not_a_contradiction() -> None:
    """A production reference the cascade already explained is not an AST miss."""
    facts = ObjectSymbols(
        defined={"_Z3Fooi": {"/b/CMakeFiles/x.dir/client/a.cpp.o"}},
        referenced={"_Z3Fooi": {"/b/CMakeFiles/x.dir/client/other.cpp.o"}},
    )
    index = {("Foo", 1): ["_Z3Fooi"]}
    demangled = {"_Z3Fooi": "topi::Foo(int)"}
    verdict, detail, qualified = candidate_verdict(
        "Foo", "method", 1, "client/a.cpp", 0, index, facts, demangled,
    )
    assert verdict == "refuted"
    assert "other.cpp.o" in detail
    assert qualified == "topi::Foo(int)"
    cascaded, cascaded_detail, _ = candidate_verdict(
        "Foo", "method", 1, "client/a.cpp", 2, index, facts, demangled,
    )
    assert cascaded == "refuted-cascade"
    assert "test-only production callers" in cascaded_detail


def test_inconclusive_symbol_reports_ambiguity_instead_of_a_wrong_verdict() -> None:
    """Two linked symbols with one name and arity cannot be told apart."""
    facts = ObjectSymbols(defined={"_Z3Fooi": {"/b/x.o"}, "_Z3Fooj": {"/b/x.o"}})
    index = {("Foo", 1): ["_Z3Fooi", "_Z3Fooj"]}
    demangled = {"_Z3Fooi": "topi::A::Foo(int)", "_Z3Fooj": "topi::B::Foo(int)"}
    verdict, detail, _ = candidate_verdict(
        "Foo", "method", 1, None, 0, index, facts, demangled,
    )
    assert verdict == "inconclusive-symbol"
    assert "2 linked symbols" in detail


def test_gc_verdict_confirms_and_distinguishes_the_test_side() -> None:
    """Every rung between ``gc-confirmed`` and ``gc-dead`` is reachable."""
    demangled = {UNUSED: "demo::UnusedByProd(int)"}
    confirmed = gc_verdict(
        "UnusedByProd", "function", 1, None, gc_facts(prod=[UNUSED]), {}, demangled,
    )
    assert confirmed[0] == "gc-confirmed"
    assert f".text.{UNUSED}" in confirmed[1]
    test_only = gc_verdict(
        "UnusedByProd",
        "function",
        1,
        None,
        gc_facts(prod=[UNUSED], tests_kept=[UNUSED]),
        {},
        demangled,
    )
    assert test_only[0] == "gc-test-only"
    absent_from_tests = gc_verdict(
        "UnusedByProd",
        "function",
        1,
        None,
        gc_facts(prod=[UNUSED], tests_checked=True),
        {},
        demangled,
    )
    assert absent_from_tests[0] == "gc-not-in-tests"
    discarded_by_tests = gc_verdict(
        "UnusedByProd",
        "function",
        1,
        None,
        gc_facts(prod=[UNUSED], tests=[UNUSED], tests_checked=True),
        {},
        demangled,
    )
    assert discarded_by_tests[0] == "gc-dead"


def test_gc_verdict_ignores_a_third_party_namesake_that_survived() -> None:
    """Tens of thousands of symbols survive the link; a namesake is not the function."""
    namesake = "_ZN10disk_cache9EntryImpl10UserBuffer8TruncateEi"
    facts = gc_facts(prod=[UNUSED])
    facts.kept = {namesake}
    demangled = {
        UNUSED: "demo::UnusedByProd(int)",
        namesake: "disk_cache::EntryImpl::UserBuffer::Truncate(int)",
    }
    verdict, _, qualified = gc_verdict(
        "UnusedByProd", "function", 1, None, facts, {UNUSED: {"/b/x.o"}}, demangled,
    )
    assert verdict == "gc-confirmed"
    assert qualified == "demo::UnusedByProd(int)"


def test_gc_verdict_refutes_when_the_symbol_survived_into_the_binary() -> None:
    """A symbol still in the production binary contradicts the graph."""
    facts = gc_facts(prod=[UNUSED], kept=[USED])
    demangled = {USED: "demo::UsedByProd(int)"}
    verdict, detail, _ = gc_verdict(
        "UsedByProd", "function", 1, None, facts, {USED: {"/b/x.o"}}, demangled,
    )
    assert verdict == "gc-refuted"
    assert "in the linked production binary" in detail


def test_gc_verdict_is_absent_when_the_link_never_held_the_symbol() -> None:
    """A symbol whose archive member was never pulled in is not a refutation."""
    facts = gc_facts(prod=[UNUSED])
    demangled = {USED: "demo::UsedByProd(int)"}
    verdict, detail, _ = gc_verdict(
        "UsedByProd", "function", 1, None, facts, {USED: {"/b/x.o"}}, demangled,
    )
    assert verdict == "gc-absent"
    assert "neither discarded nor present" in detail


def test_gc_verdict_is_absent_without_a_matching_symbol() -> None:
    """No symbol with the name and arity leaves the link with nothing to say."""
    verdict, _, _ = gc_verdict("Nowhere", "function", 0, None, gc_facts(), {}, {})
    assert verdict == "gc-absent"


def test_gc_verdict_prefers_the_symbol_in_the_candidates_own_object() -> None:
    """Overloads from other objects must not answer for this candidate."""
    other = "_ZN5other12UnusedByProdEi"
    facts = gc_facts(prod=[UNUSED, other])
    demangled = {UNUSED: "demo::UnusedByProd(int)", other: "other::UnusedByProd(int)"}
    defined_objects = {
        UNUSED: {"/b/CMakeFiles/x.dir/client/a.cpp.o"},
        other: {"/b/CMakeFiles/x.dir/client/other.cpp.o"},
    }
    verdict, detail, _ = gc_verdict(
        "UnusedByProd",
        "function",
        1,
        "client/a.cpp",
        facts,
        defined_objects,
        demangled,
    )
    assert verdict == "gc-confirmed"
    assert UNUSED in detail


def test_gc_inconclusive_is_reported_when_nothing_prefers_one_symbol() -> None:
    """Two discarded sections can share a candidate's name and arity."""
    candidate = Candidate(
        usr="c:@N@demo@F@Foo#",
        name="Foo",
        kind="method",
        file="client/a.cpp",
        line=1,
        col=1,
        type_text="void ()",
    )
    gc = gc_facts(prod=["_ZN4demo3FooEv", "_ZN4other3FooEv"])
    demangled = {"_ZN4demo3FooEv": "demo::Foo()", "_ZN4other3FooEv": "other::Foo()"}
    cross_check, detail, _, gc_status, _ = decide_candidate(
        candidate, ObjectSymbols(), {}, gc, demangled,
    )
    assert gc_status == "gc-inconclusive"
    assert cross_check == "gc-inconclusive"
    assert "2 linked symbols" in detail


def test_decide_candidate_falls_back_to_the_object_ladder_when_gc_is_silent() -> None:
    """GC has no evidence, so the object symbols decide and GC reports why."""
    candidate = Candidate(
        usr="c:@N@demo@F@UnusedByProd#I#",
        name="UnusedByProd",
        kind="function",
        file="client/a.cpp",
        line=1,
        col=1,
        type_text="int (int)",
    )
    facts = ObjectSymbols(
        defined={UNUSED: {"/b/x.dir/client/a.cpp.o"}},
        referenced={UNUSED: set()},
    )
    demangled = {UNUSED: "demo::UnusedByProd(int)"}
    cross_check, _detail, qualified, gc_status, gc_detail = decide_candidate(
        candidate, facts, {("UnusedByProd", 1): [UNUSED]}, gc_facts(), demangled,
    )
    assert cross_check == "ok"
    assert qualified == "demo::UnusedByProd(int)"
    assert gc_status == "gc-absent"
    assert "neither discarded nor present" in gc_detail
