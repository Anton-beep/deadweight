"""Find C and C++ functions that only your tests reach.

The package exposes the three identifiers that version independently of each
other: the release version, the report schema written by ``report.py`` and the
facts record version written by the front end.
"""

from __future__ import annotations

__all__ = ["FACTS_VERSION", "REPORT_SCHEMA", "__version__"]

__version__ = "0.1.0"

REPORT_SCHEMA = "deadweight.report/1"

FACTS_VERSION = 1
