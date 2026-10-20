"""The environment self-check: which libclang the run found, and what it can reach.

``deadweight doctor`` exists because a report is only as trustworthy as the compiler
that produced it. The output is what a user pastes into an issue, so it names the
library, its version, every API entry point the tool probed, and whether the linker
tier is available on this machine. Each fact is one line of markdown, and the symbol
list is sorted, so two runs on the same machine differ only when the machine did.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from .cindex import LibClang

FLOOR = 16
"""The oldest libclang the C API contract covers."""

_DELETED_SYMBOL = "clang_CXXMethod_isDeleted"


def report(lib: LibClang, *, cross_check_available: bool) -> str:
    """Render what was found as markdown, one fact per line."""
    lines = [
        "# deadweight doctor",
        "",
        f"libclang: `{lib.path}`",
        f"version: {lib.version}",
        f"banner: {lib.banner}",
        f"supported floor: libclang {FLOOR}",
    ]
    if lib.major and lib.major < FLOOR:
        lines.append(
            f"warning: libclang {lib.version} is older than the supported floor of "
            f"{FLOOR}; the scan works, but it is not a version CI tests",
        )
    if cross_check_available:
        lines.append("linker tier: available")
    else:
        lines.append(
            "linker tier: unavailable; every candidate reports "
            "cross_check: unavailable and the AST verdict stands alone",
        )
    if not lib.api.get(_DELETED_SYMBOL, True):
        lines.append(
            f"note: `{_DELETED_SYMBOL}` is missing, so the deleted marker is read "
            "from the definition's source line",
        )
    lines.extend(["", "## probed symbols", ""])
    lines.extend(
        f"- `{name}`: {'available' if available else 'missing'}"
        for name, available in sorted(lib.api.items())
    )
    return "\n".join(lines) + "\n"
