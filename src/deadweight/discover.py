"""Where libclang comes from: the ordered search, the probe, and why a path lost.

The search order is the contract, from the most explicit answer to the most implicit:

1. ``--libclang PATH``, which reaches this module as ``explicit``;
2. ``DEADWEIGHT_LIBCLANG``;
3. the libraries the machine advertises, through the known install prefixes and
   ``ldconfig -p``, newest version first;
4. the ``libclang`` wheel's bundled library, when that distribution is installed.

A path the user named is authoritative. When it cannot be loaded, the run fails with
every reason it has instead of quietly scanning with a different library, because a
scan done with the wrong clang is worse than no scan at all. Every candidate the
search considered is kept, with the reason it lost, so ``deadweight doctor`` can show
the operator the whole trail.
"""

from __future__ import annotations

import importlib.util
import os
import re
import shutil
import subprocess
from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING

from . import cindex

if TYPE_CHECKING:
    from collections.abc import Iterator, Mapping, Sequence

ENV_VAR = "DEADWEIGHT_LIBCLANG"
"""The environment variable that names a libclang ahead of the automatic search."""

_SYSTEM_PREFIXES = (
    ("/usr/lib", "libclang.so*"),
    ("/usr/lib", "*/libclang-[0-9]*.so*"),
    ("/usr/lib", "llvm-*/lib/libclang.so*"),
    ("/usr/lib", "llvm-*/lib/libclang-[0-9]*.so*"),
    ("/usr/lib64", "llvm-*/lib/libclang.so*"),
    ("/opt/homebrew/opt/llvm/lib", "libclang*.dylib"),
    ("/usr/local/opt/llvm/lib", "libclang*.dylib"),
)
"""The directories and patterns distributions and Homebrew install libclang under.

The patterns name ``libclang`` and never ``libclang-cpp``: the latter is the C++ API
and does not export a single symbol this tool calls.
"""

_VERSION_IN_NAME = re.compile(r"\d+(?:\.\d+)*")


@dataclass(frozen=True)
class Candidate:
    """One library path the search considered, and what probing it found.

    ``lib`` is set exactly when the path is usable; ``reason`` says why it is not.
    A candidate is a report of the search, not a proposal about the code, so it is
    deliberately not the analysis's :class:`deadweight.model.Candidate`.
    """

    path: str
    origin: str
    lib: cindex.LibClang | None = None
    reason: str = ""

    @property
    def accepted(self) -> bool:
        """True when this path can be used."""
        return self.lib is not None

    @property
    def version(self) -> str:
        """The libclang version this path carries, or empty when it carries none."""
        return self.lib.version if self.lib is not None else ""


class LibClangError(RuntimeError):
    """No usable libclang: the search's conclusion, with every rejection in it."""

    def __init__(self, message: str, candidates: Sequence[Candidate] = ()) -> None:
        """Append one line per rejected candidate the search considered."""
        self.candidates = list(candidates)
        detail = "".join(
            f"\n  {candidate.path} ({candidate.origin}): {candidate.reason}"
            for candidate in self.candidates
            if candidate.reason
        )
        super().__init__(message + detail)


def parse_ldconfig(text: str) -> list[str]:
    """The libclang paths a ``ldconfig -p`` cache listing holds, in its order."""
    paths: list[str] = []
    for line in text.splitlines():
        soname, separator, path = line.strip().partition(" => ")
        if not separator or not soname.startswith("libclang"):
            continue
        name = soname.split()[0]
        if name.startswith("libclang-cpp") or ".so" not in name:
            continue
        paths.append(path.strip())
    return paths


def candidates(explicit: str | None, env: Mapping[str, str]) -> list[Candidate]:
    """Every library path the ordered search considered, with its verdict."""
    found: list[Candidate] = []
    seen: set[str] = set()
    for path, origin in _search(explicit, env):
        if path in seen:
            continue
        seen.add(path)
        found.append(_candidate(path, origin))
    return found


def load(
    explicit: str | None = None,
    *,
    required_major: int | None = None,
) -> cindex.LibClang:
    """The first usable libclang the search finds, or a :class:`LibClangError`.

    ``required_major`` is the ``--require-libclang`` contract: a version that does not
    match it is an error rather than a surprise, so a CI job that pins a major version
    cannot silently run against another one.
    """
    found = candidates(explicit, os.environ)
    designated = explicit or os.environ.get(ENV_VAR, "")
    considered = (
        [candidate for candidate in found if candidate.path == designated]
        if designated
        else found
    )
    for candidate in considered:
        library = candidate.lib
        if library is None:
            continue
        if required_major is not None and library.major != required_major:
            message = (
                f"{candidate.path} is libclang {library.version}, "
                f"but libclang {required_major} was required"
            )
            raise LibClangError(message, [candidate])
        return library
    raise LibClangError(_missing_message(designated), considered)


def _missing_message(designated: str) -> str:
    """Say what was searched, and how to name a library directly."""
    if designated:
        return f"libclang at {designated} cannot be used"
    return (
        "no usable libclang was found; pass --libclang PATH or set "
        f"{ENV_VAR}=PATH"
    )


def _search(explicit: str | None, env: Mapping[str, str]) -> Iterator[tuple[str, str]]:
    """Walk the tiers in order, naming the tier each path came from."""
    if explicit:
        yield explicit, "explicit"
    designated = env.get(ENV_VAR, "")
    if designated:
        yield designated, "environment"
    for path in _system_paths():
        yield path, "system-path"
    for path in _ldconfig_paths():
        yield path, "ldconfig"
    for path in _wheel_paths():
        yield path, "wheel"


def _candidate(path: str, origin: str) -> Candidate:
    """Probe one path and turn its failure into the reason the operator reads."""
    try:
        lib = _probe(path)
    except (OSError, cindex.CindexError) as error:
        return Candidate(path=path, origin=origin, reason=_reason(error))
    return Candidate(path=path, origin=origin, lib=lib)


def _reason(error: Exception) -> str:
    """The text of a rejection, which is empty for a bare exception."""
    return str(error).strip() or type(error).__name__


def _probe(path: str) -> cindex.LibClang:
    """Probe one path; the tests replace this seam to stand in for a library."""
    return cindex.probe(path)


def _system_paths() -> list[str]:
    """The libraries the known prefixes carry, newest version first."""
    paths: list[str] = []
    for directory, pattern in _SYSTEM_PREFIXES:
        paths.extend(_expand(directory, pattern))
    return sorted(paths, key=_version_key, reverse=True)


def _expand(directory: str, pattern: str) -> list[str]:
    """One prefix's matches; the tests replace this seam to stand in for a host."""
    return sorted(str(path) for path in Path(directory).glob(pattern))


def _version_key(path: str) -> tuple[tuple[int, ...], str]:
    """Order libraries by the version their file name carries, so 19 beats 9."""
    numbers = [
        tuple(int(part) for part in found.split("."))
        for found in _VERSION_IN_NAME.findall(Path(path).name)
    ]
    return (max(numbers, default=()), path)


def _ldconfig_paths() -> list[str]:
    """What ``ldconfig -p`` lists, newest version first; empty off Linux."""
    paths = parse_ldconfig(_ldconfig_text())
    return sorted(paths, key=_version_key, reverse=True)


def _ldconfig_text() -> str:
    """Run ``ldconfig -p``; empty when the tool is absent or the run fails."""
    tool = shutil.which("ldconfig")
    if tool is None:
        return ""
    # The tool is resolved through shutil.which and the only argument is a constant,
    # so there is no shell and nothing here that user input can influence.
    completed = subprocess.run(  # noqa: S603
        [tool, "-p"],
        capture_output=True,
        text=True,
        check=False,
    )
    return completed.stdout if completed.returncode == 0 else ""


def _wheel_paths() -> list[str]:
    """The ``libclang`` wheel's bundled library, when that distribution is present."""
    spec = importlib.util.find_spec("clang")
    if spec is None or not spec.submodule_search_locations:
        return []
    native = Path(next(iter(spec.submodule_search_locations))) / "native"
    return sorted(str(path) for path in native.glob("libclang*"))
