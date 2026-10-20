"""Reading ``.deadweight.toml``: discovery, the schema, and the refusals.

The file is the only place a repository states its own roots, so a key this module
does not recognise is a usage error rather than a warning: a typo that silently
disables a production root turns an incomplete scan into a confident clean one. The
same reasoning makes ``reason`` mandatory on every ``[[ignore]]`` entry — an allowlist
nobody has to justify is an allowlist that never shrinks.

Paths are kept exactly as the file spells them. Resolving them against the scan root
is the caller's job, which keeps this layer a pure reading of one file.
"""

from __future__ import annotations

import tomllib
from dataclasses import dataclass
from datetime import date
from pathlib import Path

CONFIG_NAME = ".deadweight.toml"
"""The file a run looks for, from its working directory upward."""

FAIL_ON_NONE = "none"
"""The default gate: report the candidates and exit 0."""

FAIL_ON_CANDIDATES = "candidates"
"""Exit 4 when any candidate is found, which is what CI wants."""

FAIL_ON_VALUES = (FAIL_ON_NONE, FAIL_ON_CANDIDATES)
"""Every value ``--fail-on`` and ``[gate] fail_on`` accept."""

FORMATS = ("md", "json", "tsv")
"""Every rendering ``--format`` and ``[scan] format`` accept."""

_SCAN_KEYS = frozenset(
    {"prod_roots", "test_roots", "exclude", "compile_commands", "out", "format"},
)
_LINKER_KEYS = frozenset(
    {"gc_reports", "gc_report_tests", "prod_binary", "test_binaries_dir"},
)
_GATE_KEYS = frozenset({"fail_on"})
_IGNORE_KEYS = frozenset({"symbol", "reason", "until"})
_TOP_KEYS = frozenset({"scan", "linker", "gate", "ignore"})


class ConfigError(Exception):
    """A configuration file the run refuses to interpret."""


@dataclass(frozen=True)
class Ignore:
    """One allowlisted symbol, with the reason it is still there and its expiry."""

    symbol: str
    reason: str
    until: date | None = None


@dataclass(frozen=True)
class Config:
    """Everything the file said, in the vocabulary the command line also uses.

    An unset path is ``None`` rather than a guess, so the caller can tell "the file
    said nothing" from "the file said the default".
    """

    prod_roots: tuple[str, ...] = ()
    test_roots: tuple[str, ...] = ()
    exclude: tuple[str, ...] = ()
    gc_reports: tuple[Path, ...] = ()
    gc_report_tests: tuple[Path, ...] = ()
    prod_binary: Path | None = None
    test_binaries_dir: Path | None = None
    fail_on: str = FAIL_ON_NONE
    ignores: tuple[Ignore, ...] = ()
    compile_commands: Path | None = None
    out: Path | None = None
    format: tuple[str, ...] = FORMATS
    """The renderings a run with ``--out`` writes, in the order it writes them."""


def find_config(start: Path, explicit: Path | None) -> Path | None:
    """The configuration file a run started at ``start`` should read.

    ``explicit`` is ``--config``: it names one file, so a path that is not there is an
    error rather than a reason to keep walking. Otherwise the search walks upward and
    stops at the first ``.deadweight.toml``, which is what lets a run anywhere under a
    repository pick up that repository's file.
    """
    if explicit is not None:
        if not explicit.is_file():
            message = f"--config {explicit} does not exist"
            raise ConfigError(message)
        return explicit
    for directory in (start, *start.parents):
        candidate = directory / CONFIG_NAME
        if candidate.is_file():
            return candidate
    return None


def load_config(start: Path, explicit: Path | None) -> Config:
    """Read the configuration a run at ``start`` inherits, or the defaults."""
    path = find_config(start, explicit)
    if path is None:
        return Config()
    try:
        text = path.read_text(encoding="utf-8")
    except OSError as error:
        message = f"cannot read {path}: {error}"
        raise ConfigError(message) from error
    try:
        payload = tomllib.loads(text)
    except tomllib.TOMLDecodeError as error:
        message = f"{path} is not valid TOML: {error}"
        raise ConfigError(message) from error
    return _config(payload, path)


def _config(payload: dict[str, object], path: Path) -> Config:
    """Validate one parsed file and turn it into a :class:`Config`."""
    _refuse_unknown(payload, _TOP_KEYS, path, table="")
    scan = _table(payload, "scan", path)
    linker = _table(payload, "linker", path)
    gate = _table(payload, "gate", path)
    _refuse_unknown(scan, _SCAN_KEYS, path, table="scan")
    _refuse_unknown(linker, _LINKER_KEYS, path, table="linker")
    _refuse_unknown(gate, _GATE_KEYS, path, table="gate")
    fail_on = _text(gate.get("fail_on", FAIL_ON_NONE), "gate.fail_on", path)
    if fail_on not in FAIL_ON_VALUES:
        message = (
            f"{path}: gate.fail_on is {fail_on!r}, expected one of "
            f"{', '.join(FAIL_ON_VALUES)}"
        )
        raise ConfigError(message)
    formats = _texts(scan.get("format", list(FORMATS)), "scan.format", path)
    for name in formats:
        if name not in FORMATS:
            message = (
                f"{path}: scan.format names {name!r}, expected one of "
                f"{', '.join(FORMATS)}"
            )
            raise ConfigError(message)
    return Config(
        prod_roots=_texts(scan.get("prod_roots", []), "scan.prod_roots", path),
        test_roots=_texts(scan.get("test_roots", []), "scan.test_roots", path),
        exclude=_texts(scan.get("exclude", []), "scan.exclude", path),
        gc_reports=_paths(linker.get("gc_reports", []), "linker.gc_reports", path),
        gc_report_tests=_paths(
            linker.get("gc_report_tests", []),
            "linker.gc_report_tests",
            path,
        ),
        prod_binary=_path(linker.get("prod_binary"), "linker.prod_binary", path),
        test_binaries_dir=_path(
            linker.get("test_binaries_dir"),
            "linker.test_binaries_dir",
            path,
        ),
        fail_on=fail_on,
        ignores=_ignores(payload.get("ignore", []), path),
        compile_commands=_path(
            scan.get("compile_commands"),
            "scan.compile_commands",
            path,
        ),
        out=_path(scan.get("out"), "scan.out", path),
        format=formats,
    )


def _ignores(value: object, path: Path) -> tuple[Ignore, ...]:
    """Read every ``[[ignore]]`` entry, refusing one that cannot be justified."""
    entries: list[Ignore] = []
    for entry in _sequence(value, "ignore", path):
        table = _mapping(entry, "ignore", path)
        _refuse_unknown(table, _IGNORE_KEYS, path, table="ignore")
        symbol = _text(table.get("symbol", ""), "ignore.symbol", path).strip()
        if not symbol:
            message = f"{path}: every [[ignore]] entry needs a non-empty 'symbol'"
            raise ConfigError(message)
        reason = _text(table.get("reason", ""), "ignore.reason", path).strip()
        if not reason:
            message = (
                f"{path}: [[ignore]] entry {symbol!r} needs a non-empty 'reason'; an "
                "allowlist entry nobody had to justify is one that never goes away"
            )
            raise ConfigError(message)
        entries.append(
            Ignore(symbol=symbol, reason=reason, until=_date(table.get("until"), path)),
        )
    return tuple(entries)


def _date(value: object, path: Path) -> date | None:
    """An optional expiry date, in the only spelling the tool accepts."""
    if value is None:
        return None
    text = _text(value, "ignore.until", path)
    try:
        return date.fromisoformat(text)
    except ValueError as error:
        message = f"{path}: ignore.until {text!r} is not an ISO date (YYYY-MM-DD)"
        raise ConfigError(message) from error


def _table(payload: dict[str, object], name: str, path: Path) -> dict[str, object]:
    """One optional table of a parsed file."""
    return _mapping(payload.get(name, {}), name, path)


def _mapping(value: object, name: str, path: Path) -> dict[str, object]:
    """A parsed value that has to be a table."""
    if not isinstance(value, dict):
        message = f"{path}: {name} must be a table, found {type(value).__name__}"
        raise ConfigError(message)
    return value


def _sequence(value: object, name: str, path: Path) -> list[object]:
    """A parsed value that has to be a list of tables."""
    if not isinstance(value, list):
        found = type(value).__name__
        message = f"{path}: {name} must be a list of tables, found {found}"
        raise ConfigError(message)
    return value


def _text(value: object, name: str, path: Path) -> str:
    """A parsed value that has to be text."""
    if not isinstance(value, str):
        message = f"{path}: {name} must be text, found {type(value).__name__}"
        raise ConfigError(message)
    return value


def _texts(value: object, name: str, path: Path) -> tuple[str, ...]:
    """A parsed value that has to be a list of text."""
    if not isinstance(value, list):
        message = f"{path}: {name} must be a list of text, found {type(value).__name__}"
        raise ConfigError(message)
    return tuple(_text(item, name, path) for item in value)


def _path(value: object, name: str, path: Path) -> Path | None:
    """An optional path, kept as written."""
    if value is None:
        return None
    text = _text(value, name, path)
    if not text:
        message = f"{path}: {name} is empty"
        raise ConfigError(message)
    return Path(text)


def _paths(value: object, name: str, path: Path) -> tuple[Path, ...]:
    """A list of paths, kept as written."""
    return tuple(Path(item) for item in _texts(value, name, path))


def _refuse_unknown(
    payload: dict[str, object],
    known: frozenset[str],
    path: Path,
    *,
    table: str,
) -> None:
    """Refuse a key this module does not read, naming it and where it was found."""
    unknown = sorted(set(payload) - known)
    if not unknown:
        return
    where = table or "the top level"
    names = ", ".join(repr(name) for name in unknown)
    message = (
        f"{path}: unknown key {names} in {where}; a key the tool ignores would "
        "silently change what gets scanned"
    )
    raise ConfigError(message)
