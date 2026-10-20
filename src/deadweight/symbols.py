"""Pure symbol logic: name and arity normalization, and the ladders of a verdict.

The linker side speaks mangled names and object files, the AST side speaks USRs and
cursor kinds, and neither sees the other's identifiers. Everything here reduces both
to a `(unqualified name, parameter count)` key and turns the two evidence sets into a
verdict. Nothing here spawns a process or reads a file.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from .model import Candidate, GcFacts, ObjectSymbols


def split_signature(text: str, *, skip_named_parens: bool) -> tuple[str, str] | None:
    """Split a C++ function type or demangled name into ``(head, parameter text)``."""
    search_from = 0
    if skip_named_parens:
        named = text.find("operator()")
        if named >= 0:
            search_from = named + len("operator()")
    start = text.find("(", search_from)
    if start < 0:
        return None
    depth = 0
    for index in range(start, len(text)):
        char = text[index]
        if char == "(":
            depth += 1
        elif char == ")":
            depth -= 1
            if depth == 0:
                return text[:start].strip(), text[start + 1 : index].strip()
    return None


def parameter_arity(parameters: str) -> int:
    """Count top-level parameters; an empty list and `void` both mean none."""
    if not parameters or parameters == "void":
        return 0
    depth = 0
    count = 1
    for char in parameters:
        if char in "(<[{":
            depth += 1
        elif char in ")>]}":
            depth -= 1
        elif char == "," and depth == 0:
            count += 1
    return count


def strip_template_args(component: str) -> str:
    """Drop a trailing `<...>` function-template argument list."""
    if not component.endswith(">"):
        return component
    depth = 0
    for index in range(len(component) - 1, -1, -1):
        if component[index] == ">":
            depth += 1
        elif component[index] == "<":
            depth -= 1
            if depth == 0 and index > 0:
                return component[:index].strip()
    return component


def qualifier_parts(head: str) -> list[str]:
    """Split `A::B<C>::D` into its depth-0 `::` components, template-free."""
    parts: list[str] = []
    start = 0
    depth = 0
    index = 0
    while index < len(head):
        char = head[index]
        if char in "(<[{":
            depth += 1
        elif char in ")>]}":
            depth -= 1
        elif char == ":" and depth == 0 and head[index : index + 2] == "::":
            parts.append(strip_template_args(head[start:index].strip()))
            start = index + 2
            index += 1
        index += 1
    parts.append(strip_template_args(head[start:].strip()))
    return [part for part in parts if part]


def unqualified_name(head: str) -> str:
    """Drop namespace/class qualifiers and a trailing template-argument list."""
    parts = qualifier_parts(head)
    return parts[-1] if parts else ""


def symbol_key(qualified: str) -> tuple[str, int] | None:
    """The `(unqualified name, parameter count)` key a linked symbol demangles to."""
    parts = split_signature(qualified, skip_named_parens="operator()" in qualified)
    if parts is None:
        return None
    return unqualified_name(parts[0]), parameter_arity(parts[1])


def ast_symbol_key(name: str, type_text: str) -> tuple[str, int] | None:
    """The same key built from an AST declaration's name and type text."""
    parts = split_signature(type_text, skip_named_parens=False)
    if parts is None:
        return None
    return name, parameter_arity(parts[1])


def object_matches_source(obj: str, source: str | None) -> bool:
    """True when |obj| is the object compiled from |source| (`<source>.o`)."""
    if not source:
        return False
    return obj.endswith((source + ".o", source))


def symbol_matches_key(qualified: str, name: str, arity: int) -> bool:
    """True when a demangled symbol carries the key the AST side built."""
    return bool(qualified) and symbol_key(qualified) == (name, arity)


def symbol_matches_kind(qualified: str, name: str, kind: str) -> bool:
    """Reject symbols whose constructor/destructor shape contradicts the AST kind.

    Without this, a nested class constructor (`Config::Config()`) answers for an
    accessor method of the same name and arity, and the cross-check refutes a
    correct AST verdict.
    """
    parts = split_signature(qualified, skip_named_parens="operator()" in qualified)
    if parts is None:
        return False
    head = qualifier_parts(parts[0])
    if not head:
        return False
    is_ctor = len(head) > 1 and head[-1] == head[-2] and head[-1] == name
    is_dtor = head[-1].startswith("~")
    if kind == "constructor":
        return is_ctor
    if kind == "destructor":
        return is_dtor
    return not is_ctor and not is_dtor


def cross_check_verdict(
    defined_in: set[str],
    referenced_by: set[str],
    exempt: set[str],
) -> str:
    """Compare an AST verdict against the linker's view of a symbol.

    `exempt` holds the objects that define the symbol when a self-reference
    there is plausibly recursion rather than a production call site.
    """
    if not defined_in:
        return "absent"
    if referenced_by - exempt:
        return "refuted"
    if referenced_by & exempt:
        return "refuted-same-tu"
    return "ok"


def candidate_verdict(  # noqa: PLR0913, PLR0917 -- the ported ladder API
    name: str,
    kind: str,
    arity: int,
    def_file: str | None,
    direct_prod_refs: int,
    index: dict[tuple[str, int], list[str]],
    facts: ObjectSymbols,
    demangled: dict[str, str],
) -> tuple[str, str, str]:
    """Match one candidate against the linked symbols: ``(verdict, detail, qualified)``.

    The parameters are the candidate's identity, the evidence the object ladder holds
    and what the linker says about the symbol; grouping them would only hide which
    evidence each rung reads.
    """
    matches = [
        symbol
        for symbol in index.get((name, arity), [])
        if symbol_matches_kind(demangled.get(symbol, ""), name, kind)
    ]
    if not matches:
        detail = (
            "no linked symbol with this name and parameter count "
            "(header-only, or test-only instantiation)"
        )
        return "absent", detail, ""
    preferred = [
        symbol
        for symbol in matches
        if any(
            object_matches_source(obj, def_file)
            for obj in facts.defined.get(symbol, ())
        )
    ]
    chosen = preferred or matches
    if len(chosen) > 1:
        detail = f"{len(chosen)} linked symbols share this name and parameter count"
        return "inconclusive-symbol", detail, ""
    symbol = chosen[0]
    defined_in = facts.defined.get(symbol, set())
    referenced_by = facts.referenced.get(symbol, set())
    # A reference from the object that defines a source-level function is usually
    # recursion (or a same-TU call the AST already saw), so it is reported but not
    # treated as a contradiction. Header-inline functions get no exemption: "same
    # object" says nothing there.
    exempt = (
        set(defined_in)
        if def_file and def_file.endswith((".cpp", ".cc", ".cxx"))
        else set()
    )
    verdict = cross_check_verdict(defined_in, referenced_by, exempt)
    detail = symbol
    if verdict in {"refuted", "refuted-same-tu"}:
        detail = f"{symbol} referenced by " + ", ".join(sorted(referenced_by)[:3])
    elif verdict == "absent":
        detail = f"{symbol} is referenced but defined in no production object"
    if verdict == "refuted" and direct_prod_refs > 0:
        # The AST did see production call sites, so the only disagreement is
        # reachability: those callers are themselves test-only. That is the
        # cascade, not an AST miss, and it must not look like a contradiction.
        detail = f"{symbol} reachable only through test-only production callers"
        return "refuted-cascade", detail, demangled.get(symbol, "")
    return verdict, detail, demangled.get(symbol, "")


def _gc_matches(  # noqa: PLR0913, PLR0917 -- the candidate's identity plus the link
    name: str,
    kind: str,
    arity: int,
    def_file: str | None,
    facts: GcFacts,
    defined_objects: dict[str, set[str]],
    demangled: dict[str, str],
) -> list[str]:
    """The linked symbols the candidate could be, its own object's first."""
    universe = sorted(facts.symbols | set(defined_objects))
    matches = [
        symbol
        for symbol in universe
        if symbol_matches_key(demangled.get(symbol, ""), name, arity)
        and symbol_matches_kind(demangled.get(symbol, ""), name, kind)
    ]
    in_own_object = [
        symbol
        for symbol in matches
        if any(
            object_matches_source(obj, def_file)
            for obj in defined_objects.get(symbol, ())
        )
    ]
    in_prod = [symbol for symbol in matches if symbol in defined_objects]
    return in_own_object or in_prod or matches


def _gc_discarded(section: str, symbol: str, facts: GcFacts) -> tuple[str, str]:
    """The rungs that read a symbol the production link discarded."""
    if symbol in facts.tests:
        detail = (
            f"{section} discarded from the production link and from the supplied "
            "test links"
        )
        return "gc-dead", detail
    if facts.tests_checked:
        if symbol in facts.tests_kept:
            detail = (
                f"{section} discarded from the production link, present in a "
                "supplied test binary"
            )
            return "gc-test-only", detail
        detail = (
            f"{section} discarded from the production link and absent from every "
            "supplied test binary (no supplied test reaches it, or none of them "
            "links its object)"
        )
        return "gc-not-in-tests", detail
    if facts.tests:
        detail = (
            f"{section} discarded from the production link, kept by the supplied "
            "test links"
        )
        return "gc-test-only", detail
    return "gc-confirmed", f"{section} discarded from the production link"


def _gc_without_evidence(symbol: str, facts: GcFacts) -> str:
    """The detail for ``gc-absent``: why the link has nothing to say about a symbol."""
    if facts.binary_checked:
        return (
            f"{symbol} is neither discarded nor present in the linked production "
            "binary (its archive member was not pulled in, or the object is not part "
            "of that link)"
        )
    return (
        f"{symbol} was not discarded, and the linked binary was not available to "
        "confirm that it survived (pass --prod-binary)"
    )


def gc_verdict(  # noqa: PLR0913, PLR0917 -- the ported ladder API
    name: str,
    kind: str,
    arity: int,
    def_file: str | None,
    facts: GcFacts,
    defined_objects: dict[str, set[str]],
    demangled: dict[str, str],
) -> tuple[str, str, str]:
    """Judge a candidate by the linker's reachability: ``(verdict, detail, qualified)``.

    The GC view is primary and the object ladder only resolves a tie, because a
    discarded section is the linker's own verdict while a surviving symbol may be a
    namesake from a third-party object.
    """
    chosen = _gc_matches(name, kind, arity, def_file, facts, defined_objects, demangled)
    if not chosen:
        return "gc-absent", "no linked symbol matches this name and parameter count", ""
    if len(chosen) > 1:
        detail = f"{len(chosen)} linked symbols share this name and parameter count"
        return "gc-inconclusive", detail, demangled.get(chosen[0], "")
    symbol = chosen[0]
    qualified = demangled.get(symbol, "")
    section = facts.prod.get(symbol)
    if section is not None:
        status, detail = _gc_discarded(section, symbol, facts)
        return status, detail, qualified
    if symbol in facts.tests:
        detail = f"{facts.tests[symbol]} discarded from the supplied test links only"
        return "gc-dead", detail, qualified
    if symbol in facts.kept:
        detail = (
            f"{symbol} is in the linked production binary (reachable from its roots)"
        )
        return "gc-refuted", detail, qualified
    return "gc-absent", _gc_without_evidence(symbol, facts), qualified


def decide_candidate(
    candidate: Candidate,
    facts: ObjectSymbols,
    index: dict[tuple[str, int], list[str]],
    gc: GcFacts | None,
    demangled: dict[str, str],
) -> tuple[str, str, str, str, str]:
    """Combine both cross-checks into a five-tuple of verdict, detail and GC facts.

    The GC view is primary: a discarded section is the linker's own verdict, while a
    surviving symbol may be a namesake, so the object ladder only breaks the tie.
    """
    key = ast_symbol_key(candidate.name, candidate.type_text)
    if key is None:
        return "absent", "signature could not be parsed", "", "none", ""
    nm_verdict, nm_detail, nm_qualified = candidate_verdict(
        key[0],
        candidate.kind,
        key[1],
        candidate.file,
        candidate.direct_prod_refs,
        index,
        facts,
        demangled,
    )
    if gc is None:
        return nm_verdict, nm_detail, nm_qualified, "none", ""
    # The GC view is primary. Its universe already includes the symbols the
    # object ladder knows (they resolve an otherwise ambiguous name match), so
    # there is nothing left for the ladder to add once GC has spoken.
    gc_status, gc_detail, gc_qualified = gc_verdict(
        key[0],
        candidate.kind,
        key[1],
        candidate.file,
        gc,
        facts.defined,
        demangled,
    )
    if gc_status == "gc-absent":
        # The link holds no evidence either way (the symbol never entered it,
        # or the binary could not be read), so the object ladder decides.
        return nm_verdict, nm_detail, nm_qualified or gc_qualified, gc_status, gc_detail
    return gc_status, gc_detail, gc_qualified or nm_qualified, gc_status, gc_detail
