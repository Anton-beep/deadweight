"""The environment self-check: which libclang the run found, and what it can reach.

``deadweight doctor`` exists because a report is only as trustworthy as the compiler
that produced it. The output is what a user pastes into an issue, so it names the
library, its version, every API entry point the tool probed, whether the linker tier
is available on this machine, and the trail of every path the search considered and
why it lost. Each fact is one line of markdown, and the lists are sorted, so two runs
on the same machine differ only when the machine did. Only the chosen library keeps
its absolute path: every other path is spelt by its last three components, without the
root, because the trail is issue-report material that must not carry the checkout's
layout, yet a bare file name would print every ``llvm-*`` prefix's
``libclang.so.1`` alike.
"""

from __future__ import annotations

import re
from pathlib import Path
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from collections.abc import Sequence

    from .cindex import LibClang
    from .discover import Candidate

FLOOR = 16
"""The oldest libclang the C API contract covers."""

_DELETED_SYMBOL = "clang_CXXMethod_isDeleted"

_ABSOLUTE_PATH = re.compile(r"(?<![\w:/])/(?:[^\s/`]+/)*[^\s/`]+")
"""An absolute path as a rejection message spells one, so it can be reduced."""


def report(
    lib: LibClang,
    *,
    cross_check_available: bool,
    candidates: Sequence[Candidate] = (),
) -> str:
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
    lines.extend(_search_trail(lib.path, candidates))
    return "\n".join(lines) + "\n"


def _search_trail(chosen: str, candidates: Sequence[Candidate]) -> list[str]:
    """Render every path the search considered that is not the chosen library.

    A usable path that was not chosen lost to an earlier tier; a path that could not
    be loaded carries the reason the probe gave. Both are shown, because "which
    library did the search see and why did it pick this one" is the question the
    trail answers.
    """
    lost = [candidate for candidate in candidates if candidate.path != chosen]
    if not lost:
        return []
    lines = ["", "## search trail", "", "Every other path the search considered:"]
    lines.extend(
        f"- `{_without_absolute_paths(candidate.path)}` "
        f"({candidate.origin}): "
        f"{_without_absolute_paths(candidate.reason) or _LOST_TO_EARLIER_TIER}"
        for candidate in lost
    )
    return lines


_LOST_TO_EARLIER_TIER = "usable, but an earlier candidate won the search"
"""Why a loadable library that the search did not choose lost."""


def _without_absolute_paths(text: str) -> str:
    """Reduce every absolute path in ``text`` to the part that names its library.

    The chosen library is the one path the operator needs verbatim; the rest of the
    trail is diagnostic prose that goes into public issue reports.
    """
    return _ABSOLUTE_PATH.sub(lambda match: _trail_path(match.group()), text)


def _trail_path(path: str) -> str:
    """Spell one path the trail's way: its last three components, without the root.

    Three, because a file name alone cannot tell
    ``/usr/lib/llvm-16/lib/libclang.so.1`` from its ``llvm-18`` twin: the directory
    that distinguishes them is separated from the library by a generic ``lib``.
    """
    return "/".join(Path(path).parts[-3:])
