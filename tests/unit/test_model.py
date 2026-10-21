"""The vocabulary every other module shares: locating code and ordering a report."""

from __future__ import annotations

import deadweight
from deadweight.model import SCHEMA, Candidate, Loc, Span, under_any


def test_schema_is_the_package_report_schema() -> None:
    """The report schema has one spelling, and it is the package's."""
    assert SCHEMA == deadweight.REPORT_SCHEMA
    assert SCHEMA == "deadweight.report/1"


def test_loc_orders_by_file_then_position() -> None:
    """Locations sort the way a diff reads them, which is what report order needs."""
    assert Loc(file="a.cpp", line=1, col=2) < Loc(file="a.cpp", line=1, col=3)
    assert Loc(file="a.cpp", line=2, col=1) < Loc(file="b.cpp", line=1, col=1)
    assert Loc(file="a.cpp", line=1, col=1) < Loc(file="a.cpp", line=2, col=1)


def test_span_contains_is_half_open() -> None:
    """A span contains its start and not its end, and never crosses a file."""
    span = Span(
        start=Loc(file="a.cpp", line=2, col=1), end=Loc(file="a.cpp", line=4, col=1),
    )
    assert span.contains(Loc(file="a.cpp", line=2, col=1))
    assert span.contains(Loc(file="a.cpp", line=3, col=9))
    assert not span.contains(Loc(file="a.cpp", line=4, col=1))
    assert not span.contains(Loc(file="a.cpp", line=1, col=1))
    assert not span.contains(Loc(file="b.cpp", line=3, col=1))


def test_under_any_matches_whole_path_components() -> None:
    """A root names a directory, so an absolute path that holds it still matches."""
    assert under_any("/repo/client/core/a.cpp", ("client",))
    assert under_any("client/core/a.cpp", ("logging", "client"))
    assert not under_any("/repo/third_party/core/a.cpp", ("client",))
    # A path relativised out of the scan root cannot be claimed by a root that merely
    # shares a component name: this is the libwebrtc-header case parity found.
    assert not under_any("../heavy/include/logging/event.h", ("client", "logging"))
    assert under_any("../heavy/include/logging/event.h", ("../heavy",))
    assert not under_any("client2/a.cpp", ("client",))
    assert not under_any("client/a.cpp", ())


def test_candidate_sort_key_orders_by_location_then_name() -> None:
    """Two candidates from one file are reported in source order."""
    late = Candidate(
        usr="u2", name="Late", kind="function", file="client/a.cpp", line=9, col=1,
    )
    early = Candidate(
        usr="u1", name="Early", kind="function", file="client/a.cpp", line=2, col=1,
    )
    assert [
        item.name for item in sorted([late, early], key=lambda item: item.sort_key)
    ] == [
        "Early",
        "Late",
    ]
