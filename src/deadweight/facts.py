"""The facts cache: what one translation unit's parse is stored as, and its key.

A facts record is the seam between the front end and the analysis, and the cache is
one JSON file per record. The key covers everything the parse depended on: the unit,
its flags, the record layout, the libclang that produced it, and a stamp over the
first-party sources — headers included, because a header edit changes what a
translation unit means while leaving the entry's own file untouched. The stamp is the
caller's to compute with :func:`source_tree_stamp`, since only the caller knows which
roots are first-party.

A cache is an optimisation and never a source of truth: anything unusable — a missing
file, another record layout, a truncated write — reads as a miss, and the unit is
parsed again.
"""

from __future__ import annotations

import hashlib
import json
import os
from typing import TYPE_CHECKING

from . import FACTS_VERSION
from .model import Decl, Facts, Loc, Ref, Span

if TYPE_CHECKING:
    from pathlib import Path

    from .frontend import TranslationUnit

SUFFIX = ".json"
"""The file suffix of one cached facts record."""

SOURCE_SUFFIXES = frozenset(
    {
        ".c",
        ".cc",
        ".cpp",
        ".cxx",
        ".h",
        ".hh",
        ".hpp",
        ".hxx",
        ".inc",
        ".inl",
        ".ipp",
        ".tpp",
    },
)
"""The suffixes the tree stamp covers: every first-party source and header."""

_UNREADABLE = b"<unreadable>"
"""What an unreadable file contributes to the stamp, so the state is still covered."""


class _MalformedRecordError(ValueError):
    """A cached record whose shape is not the one this reader understands."""


def source_tree_stamp(root: Path, roots: tuple[str, ...]) -> str:
    """Digest the first-party sources under ``roots``, headers included.

    Paths are hashed relative to ``root``, so moving the tree does not invalidate the
    cache, and only source files are read, so a README edit does not either. A root
    that does not exist contributes nothing: a scan that missed a root would then keep
    stale facts, which the caller's configuration is responsible for not causing.
    """
    digest = hashlib.sha256()
    for path in _sources(root, roots):
        digest.update(path.relative_to(root).as_posix().encode("utf-8"))
        digest.update(b"\0")
        digest.update(_content(path))
    return digest.hexdigest()


def cache_key(
    unit: TranslationUnit,
    facts_version: int,
    libclang_version: str,
    tree_stamp: str,
) -> str:
    """Name one unit's facts for one set of inputs.

    The flags are in the key because a flag changes the parse, the directory is in it
    because a relative flag is resolved against that directory, and both versions are
    in it because a new record layout or a new libclang makes an old record useless.
    """
    payload = json.dumps(
        {
            "path": unit.path,
            "directory": unit.directory,
            "arguments": list(unit.arguments),
            "facts_version": facts_version,
            "libclang": libclang_version,
            "tree": tree_stamp,
        },
        separators=(",", ":"),
        sort_keys=True,
    )
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()


def read(cache_dir: Path, key: str) -> Facts | None:
    """The facts cached under ``key``, or ``None`` when there are none to trust."""
    try:
        text = (cache_dir / f"{key}{SUFFIX}").read_text(encoding="utf-8")
        payload = _mapping(json.loads(text))
        if _number(payload.get("facts_version")) != FACTS_VERSION:
            return None
        return Facts(
            tu=_text(payload.get("tu")),
            status=_text(payload.get("status")),
            compiler={
                _text(name): _text(value)
                for name, value in _mapping(payload.get("compiler")).items()
            },
            decls=[_decl(item) for item in _sequence(payload.get("decls"))],
            refs=[_ref(item) for item in _sequence(payload.get("refs"))],
            diags=[_text(item) for item in _sequence(payload.get("diags"))],
        )
    except (OSError, ValueError, TypeError):
        return None


def write(cache_dir: Path, key: str, facts: Facts) -> None:
    """Store one unit's facts as JSON, atomically and without a timestamp."""
    cache_dir.mkdir(parents=True, exist_ok=True)
    target = cache_dir / f"{key}{SUFFIX}"
    temporary = target.with_name(f"{target.name}.{os.getpid()}.tmp")
    temporary.write_text(_render(facts), encoding="utf-8")
    temporary.replace(target)


def _sources(root: Path, roots: tuple[str, ...]) -> list[Path]:
    """The source files under ``roots``, ordered by their path relative to ``root``."""
    found: set[Path] = set()
    for name in roots:
        base = root / name
        if not base.is_dir():
            continue
        found.update(
            path
            for path in base.rglob("*")
            if path.is_file() and path.suffix in SOURCE_SUFFIXES
        )
    return sorted(found, key=lambda path: path.relative_to(root).as_posix())


def _content(path: Path) -> bytes:
    """A file's bytes, or a marker when it cannot be read at all."""
    try:
        return path.read_bytes()
    except OSError:
        return _UNREADABLE


def _render(facts: Facts) -> str:
    """The record as deterministic JSON, for a diffable cache and stable tests."""
    return json.dumps(_payload(facts), indent=2, sort_keys=True) + "\n"


def _payload(facts: Facts) -> dict[str, object]:
    """One record's JSON form, with every version it was written under."""
    return {
        "facts_version": FACTS_VERSION,
        "tu": facts.tu,
        "status": facts.status,
        "compiler": dict(facts.compiler),
        "decls": [_decl_payload(decl) for decl in facts.decls],
        "refs": [_ref_payload(ref) for ref in facts.refs],
        "diags": list(facts.diags),
    }


def _decl_payload(decl: Decl) -> dict[str, object]:
    """One declaration's JSON form; markers are sorted so the file is stable."""
    return {
        "usr": decl.usr,
        "kind": decl.kind,
        "name": decl.name,
        "file": decl.file,
        "loc": _loc_payload(decl.loc),
        "span": {
            "start": _loc_payload(decl.span.start),
            "end": _loc_payload(decl.span.end),
        },
        "markers": sorted(decl.markers),
        "type_text": decl.type_text,
    }


def _ref_payload(ref: Ref) -> dict[str, object]:
    """One reference site's JSON form."""
    return {"usr": ref.usr, "file": ref.file, "loc": _loc_payload(ref.loc)}


def _loc_payload(loc: Loc) -> dict[str, object]:
    """One source location's JSON form."""
    return {"file": loc.file, "line": loc.line, "col": loc.col}


def _decl(value: object) -> Decl:
    """One declaration from its JSON form."""
    payload = _mapping(value)
    return Decl(
        usr=_text(payload.get("usr")),
        kind=_text(payload.get("kind")),
        name=_text(payload.get("name")),
        file=_text(payload.get("file")),
        loc=_loc(payload.get("loc")),
        span=_span(payload.get("span")),
        markers=frozenset(
            _text(marker) for marker in _sequence(payload.get("markers"))
        ),
        type_text=_text(payload.get("type_text")),
    )


def _ref(value: object) -> Ref:
    """One reference site from its JSON form."""
    payload = _mapping(value)
    return Ref(
        usr=_text(payload.get("usr")),
        file=_text(payload.get("file")),
        loc=_loc(payload.get("loc")),
    )


def _loc(value: object) -> Loc:
    """One source location from its JSON form."""
    payload = _mapping(value)
    return Loc(
        file=_text(payload.get("file")),
        line=_number(payload.get("line")),
        col=_number(payload.get("col")),
    )


def _span(value: object) -> Span:
    """One source range from its JSON form."""
    payload = _mapping(value)
    return Span(start=_loc(payload.get("start")), end=_loc(payload.get("end")))


def _mapping(value: object) -> dict[str, object]:
    """One JSON object, or a refusal to read the rest of the record."""
    if not isinstance(value, dict):
        message = f"expected a JSON object, found {type(value).__name__}"
        raise _MalformedRecordError(message)
    return {str(name): item for name, item in value.items()}


def _sequence(value: object) -> list[object]:
    """One JSON array, or a refusal to read the rest of the record."""
    if not isinstance(value, list):
        message = f"expected a JSON array, found {type(value).__name__}"
        raise _MalformedRecordError(message)
    return list(value)


def _text(value: object) -> str:
    """One JSON string, or a refusal to read the rest of the record."""
    if not isinstance(value, str):
        message = f"expected a JSON string, found {type(value).__name__}"
        raise _MalformedRecordError(message)
    return value


def _number(value: object) -> int:
    """One JSON integer, or a refusal to read the rest of the record."""
    if not isinstance(value, int) or isinstance(value, bool):
        message = f"expected a JSON integer, found {type(value).__name__}"
        raise _MalformedRecordError(message)
    return value
