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
