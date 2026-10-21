"""Fixtures shared by the unit suite, chiefly the host's libclang.

Loading the library is the one thing every libclang test needs and the one thing a
host without clang cannot do, so the lookup, the loaded handle and the skip live here
rather than being spelled again in each module that parses a fixture.
"""

from __future__ import annotations

import pytest

from deadweight import cindex, discover


@pytest.fixture(scope="session")
def libclang() -> cindex.LibClang:
    """The host's libclang; a test that asks for one is skipped without it."""
    try:
        return discover.load()
    except (discover.LibClangError, OSError) as error:
        pytest.skip(f"no libclang found: {error}")


@pytest.fixture(autouse=True)
def _forget_the_designated_library(monkeypatch: pytest.MonkeyPatch) -> None:
    """Give every test an unpinned environment, whatever the runner exported.

    ``DEADWEIGHT_LIBCLANG`` narrows discovery to one path, which is the right contract
    for a scan and the wrong one for a test that exercises the search: with a pinned
    library exported (as CI does), a test that expects the second candidate to win
    never sees it, and the failure looks like the tool rejecting a usable library.
    Tests that mean to exercise the pin set it themselves.
    """
    monkeypatch.delenv(discover.ENV_VAR, raising=False)
