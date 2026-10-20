"""The only module that knows libclang: the ctypes ABI and one parse call.

The C API is the durability seam. Everything here is declared against
``clang-c/Index.h`` and nothing depends on a clang release beyond the symbols the probe
reports, so an upgrade that keeps the ABI keeps the tool. Two recording rules carry the
analysis:

* A declaration is recorded when clang calls it a definition, and also when it carries
  a ``pure``, ``= default`` or ``= delete`` marker: those members have no body to
  record, and the keeper ladder still has to see them. A ``virtual`` declaration on its
  own is not recorded, because every override in a class body carries that marker and
  recording it would quote a declaration where the report promises the body.
* A reference is recorded wherever a cursor resolves to a function, which covers
  calls, constructor calls and address-taken functions, and excludes the type and
  variable references the analysis has no use for. The same site reached through
  several expression nodes is recorded once, because the analysis counts sites.

Paths come back exactly as libclang spelled them: absolute for a translation unit
named absolutely, and relative to the parse-time working directory otherwise. The
facts layer relativises them against the scan root.
"""

from __future__ import annotations

import ctypes
import os
import re
from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING

from .model import Decl, Loc, Ref, Span

if TYPE_CHECKING:
    from collections.abc import Sequence


class _CXString(ctypes.Structure):
    """The C API's string handle: a data pointer and a private flags word."""

    _fields_ = (("data", ctypes.c_void_p), ("private_flags", ctypes.c_uint))


class _CXCursor(ctypes.Structure):
    """The C API's cursor: a kind, a slot of its own, and three opaque pointers."""

    _fields_ = (
        ("kind", ctypes.c_int),
        ("xdata", ctypes.c_int),
        ("data", ctypes.c_void_p * 3),
    )


class _CXSourceLocation(ctypes.Structure):
    """A source location: two opaque pointers and one integer of the API's own."""

    _fields_ = (("ptr_data", ctypes.c_void_p * 2), ("int_data", ctypes.c_uint))


class _CXSourceRange(ctypes.Structure):
    """A half-open source range, as the two locations' integer halves."""

    _fields_ = (
        ("ptr_data", ctypes.c_void_p * 2),
        ("begin_int_data", ctypes.c_uint),
        ("end_int_data", ctypes.c_uint),
    )


class _CXType(ctypes.Structure):
    """A type as the C API hands it out: a kind and two opaque pointers."""

    _fields_ = (("kind", ctypes.c_int), ("data", ctypes.c_void_p * 2))


_VISITOR = ctypes.CFUNCTYPE(ctypes.c_int, _CXCursor, _CXCursor, ctypes.c_void_p)
"""``enum CXChildVisitResult (*)(CXCursor, CXCursor, CXClientData)``."""

_CXERROR_SUCCESS = 0
"""``CXError_Success``: the only error code that means the unit was built."""

_CHILD_VISIT_RECURSE = 2
"""``CXChildVisit_Recurse``: let libclang descend, so the walk costs no Python stack."""

_SEVERITY_ERROR = 3
"""``CXDiagnostic_Error``; ``CXDiagnostic_Fatal`` is 4 and also fails the unit."""

_CURSOR_FUNCTION = 8
_CURSOR_METHOD = 21
_CURSOR_CONSTRUCTOR = 24
_CURSOR_DESTRUCTOR = 25
_CURSOR_CONVERSION = 26
_CURSOR_FUNCTION_TEMPLATE = 30

_KIND_BY_CURSOR: dict[int, str] = {
    _CURSOR_FUNCTION: "function",
    _CURSOR_CONSTRUCTOR: "constructor",
    _CURSOR_DESTRUCTOR: "destructor",
    _CURSOR_CONVERSION: "conversion",
    _CURSOR_FUNCTION_TEMPLATE: "function",
}
"""The cursor kinds the graph records, and the ``Decl.kind`` each one carries.

A function template is a function here, because the analysis needs its span to
attribute the calls its body holds.
"""

_FUNCTION_CURSORS = frozenset({*_KIND_BY_CURSOR, _CURSOR_METHOD})
"""The cursor kinds a reference has to resolve to before the walk records it."""

_MARKERS = (
    ("virtual", "clang_CXXMethod_isVirtual"),
    ("pure", "clang_CXXMethod_isPureVirtual"),
    ("defaulted", "clang_CXXMethod_isDefaulted"),
    ("deleted", "clang_CXXMethod_isDeleted"),
)
"""Every marker the keeper ladder reads, with the entry point that reports it."""

_BODYLESS_MARKERS = frozenset({"pure", "defaulted", "deleted"})
"""The markers that make a bodyless declaration recordable.

``virtual`` is deliberately absent: an override declaration in a class body carries it
as well as the body defined in another translation unit does, so recording it would put
a declaration where the report promises a body. A virtual without a body is either pure
or defined elsewhere, and both of those are recorded on their own terms.
"""

_DELETE_MARKERS = ("= delete", "=delete")

OPTIONAL_SYMBOLS = ("clang_CXXMethod_isDeleted",)
"""Entry points that libclang 16 added and older libraries do not export.

A missing optional symbol is recorded in :attr:`LibClang.api`, and its marker is read
from the declaration's source line instead, so an older library still yields a usable
scan.
"""

_SIGNATURES: dict[str, tuple[tuple[object, ...], type[object] | None]] = {
    "clang_createIndex": ((ctypes.c_int, ctypes.c_int), ctypes.c_void_p),
    "clang_disposeIndex": ((ctypes.c_void_p,), None),
    "clang_parseTranslationUnit2": (
        (
            ctypes.c_void_p,
            ctypes.c_char_p,
            ctypes.POINTER(ctypes.c_char_p),
            ctypes.c_int,
            ctypes.c_void_p,
            ctypes.c_uint,
            ctypes.c_uint,
            ctypes.POINTER(ctypes.c_void_p),
        ),
        ctypes.c_int,
    ),
    "clang_disposeTranslationUnit": ((ctypes.c_void_p,), None),
    "clang_getTranslationUnitCursor": ((ctypes.c_void_p,), _CXCursor),
    "clang_getNumDiagnostics": ((ctypes.c_void_p,), ctypes.c_uint),
    "clang_getDiagnostic": ((ctypes.c_void_p, ctypes.c_uint), ctypes.c_void_p),
    "clang_getDiagnosticSeverity": ((ctypes.c_void_p,), ctypes.c_int),
    "clang_formatDiagnostic": ((ctypes.c_void_p, ctypes.c_uint), _CXString),
    "clang_defaultDiagnosticDisplayOptions": ((), ctypes.c_uint),
    "clang_disposeDiagnostic": ((ctypes.c_void_p,), None),
    "clang_getCString": ((_CXString,), ctypes.c_char_p),
    "clang_disposeString": ((_CXString,), None),
    "clang_getClangVersion": ((), _CXString),
    "clang_visitChildren": ((_CXCursor, _VISITOR, ctypes.c_void_p), ctypes.c_uint),
    "clang_getCursorKind": ((_CXCursor,), ctypes.c_int),
    "clang_isDeclaration": ((ctypes.c_int,), ctypes.c_uint),
    "clang_getCursorUSR": ((_CXCursor,), _CXString),
    "clang_getCursorSpelling": ((_CXCursor,), _CXString),
    "clang_getCursorReferenced": ((_CXCursor,), _CXCursor),
    "clang_Cursor_isNull": ((_CXCursor,), ctypes.c_int),
    "clang_getCursorLocation": ((_CXCursor,), _CXSourceLocation),
    "clang_getCursorExtent": ((_CXCursor,), _CXSourceRange),
    "clang_getRangeStart": ((_CXSourceRange,), _CXSourceLocation),
    "clang_getRangeEnd": ((_CXSourceRange,), _CXSourceLocation),
    "clang_getSpellingLocation": (
        (
            _CXSourceLocation,
            ctypes.POINTER(ctypes.c_void_p),
            ctypes.POINTER(ctypes.c_uint),
            ctypes.POINTER(ctypes.c_uint),
            ctypes.POINTER(ctypes.c_uint),
        ),
        None,
    ),
    "clang_getFileName": ((ctypes.c_void_p,), _CXString),
    "clang_getCursorType": ((_CXCursor,), _CXType),
    "clang_getTypeSpelling": ((_CXType,), _CXString),
    "clang_isCursorDefinition": ((_CXCursor,), ctypes.c_uint),
    "clang_CXXMethod_isVirtual": ((_CXCursor,), ctypes.c_uint),
    "clang_CXXMethod_isPureVirtual": ((_CXCursor,), ctypes.c_uint),
    "clang_CXXMethod_isDefaulted": ((_CXCursor,), ctypes.c_uint),
    "clang_CXXMethod_isDeleted": ((_CXCursor,), ctypes.c_uint),
}
"""Every declaration this module applies: ``name -> (argtypes, restype)``."""

REQUIRED_SYMBOLS = tuple(
    name for name in _SIGNATURES if name not in OPTIONAL_SYMBOLS
)
"""The entry points the scanner cannot do without; a missing one is a load error."""

_VERSION_RE = re.compile(r"\d+(?:\.\d+)+")


class CindexError(Exception):
    """A shared library that cannot serve as the scanner's libclang."""


class MissingSymbolError(CindexError):
    """A shared library that does not export a symbol the scanner requires."""

    def __init__(self, path: str, missing: Sequence[str]) -> None:
        """Name the library and every entry point it lacks."""
        self.path = path
        self.missing = list(missing)
        names = ", ".join(self.missing)
        super().__init__(f"{path} does not export {names}")


@dataclass(frozen=True)
class LibClang:
    """A discovered libclang: where it is, how new it is, and what it exports."""

    path: str
    version: str
    banner: str
    api: dict[str, bool]

    @property
    def major(self) -> int:
        """The major version number, or ``0`` when the banner named none."""
        head = self.version.split(".", maxsplit=1)[0]
        return int(head) if head.isdigit() else 0

    def parse(
        self,
        filename: Path,
        args: list[str],
    ) -> tuple[list[Decl], list[Ref], list[str], bool]:
        """Parse one translation unit into declarations, references and diagnostics.

        ``args`` is the compile entry's argument list without the source file, which
        the C API takes separately. The fourth element says whether the unit parsed:
        a unit with an error diagnostic did not, whatever libclang's error code says,
        because clang recovers from a syntax error and still hands back an AST. A
        caller that sees ``False`` has to treat the graph as incomplete.
        """
        return _Api(self.path).parse(filename, args)


def probe(path: str) -> LibClang:
    """Load ``path`` and record its version and the symbols it exports.

    Raises ``OSError`` when the file is not a loadable shared library, and
    ``MissingSymbolError`` when it lacks an entry point the scanner requires.
    """
    api = _Api(path)
    banner = api.version_banner()
    return LibClang(
        path=path,
        version=_version_number(banner),
        banner=banner,
        api=dict(api.present),
    )


def deleted_in_source(path: str, line: int) -> bool:
    """True when the source line at ``path`` carries ``= delete``.

    libclang 15 and older have no ``clang_CXXMethod_isDeleted``, so the marker comes
    from the declaration's own line instead. The file is read on every call, which is
    the pre-16 path only, and an unreadable file is a missing marker rather than an
    error: the scan degrades to something it cannot prove, never to something it
    guesses.
    """
    if not path or line <= 0:
        return False
    try:
        text = Path(path).read_text(encoding="utf-8", errors="replace")
    except OSError:
        return False
    lines = text.splitlines()
    if line > len(lines):
        return False
    return any(marker in lines[line - 1] for marker in _DELETE_MARKERS)


def _version_number(banner: str) -> str:
    """The version a clang banner carries, e.g. ``22.1.8``, or ``unknown``."""
    match = _VERSION_RE.search(banner)
    return match.group(0) if match else "unknown"


def _decl_kind(cursor_kind: int, spelling: str) -> str | None:
    """Map a cursor kind to a ``Decl.kind``, or ``None`` for anything else."""
    if cursor_kind == _CURSOR_METHOD:
        return "assignment" if spelling == "operator=" else "method"
    return _KIND_BY_CURSOR.get(cursor_kind)


class _Api:
    """One loaded libclang with its declarations applied and its symbols probed."""

    def __init__(self, path: str) -> None:
        """Load ``path`` and apply the declarations of every symbol it exports."""
        self.path = path
        self.lib = ctypes.CDLL(path)
        self.present: dict[str, bool] = {}
        for name, (argtypes, restype) in _SIGNATURES.items():
            try:
                function = getattr(self.lib, name)
            except AttributeError:
                self.present[name] = False
                continue
            function.argtypes = list(argtypes)
            function.restype = restype
            self.present[name] = True
        missing = sorted(
            name
            for name, present in self.present.items()
            if not present and name in REQUIRED_SYMBOLS
        )
        if missing:
            raise MissingSymbolError(path, missing)

    def parse(
        self,
        filename: Path,
        args: list[str],
    ) -> tuple[list[Decl], list[Ref], list[str], bool]:
        """Walk one translation unit and dispose of it, whatever the walk does."""
        index = self.lib.clang_createIndex(0, 0)
        try:
            unit, diagnostics = self._unit(index, filename, args)
            if unit is None:
                return [], [], diagnostics, False
            try:
                walker = _Walker(self)
                callback = _VISITOR(walker.on_cursor)
                root = self.lib.clang_getTranslationUnitCursor(unit)
                self.lib.clang_visitChildren(root, callback, None)
                diagnostics, failed = self._diagnostics(unit)
                return [*walker.decls], [*walker.refs], diagnostics, not failed
            finally:
                self.lib.clang_disposeTranslationUnit(unit)
        finally:
            self.lib.clang_disposeIndex(index)

    def text(self, value: _CXString) -> str:
        """Take a ``CXString`` as Python text, and dispose of it."""
        raw = self.lib.clang_getCString(value)
        text = raw.decode("utf-8", "replace") if raw else ""
        self.lib.clang_disposeString(value)
        return text

    def version_banner(self) -> str:
        """The banner ``clang_getClangVersion`` prints for this library."""
        return self.text(self.lib.clang_getClangVersion()).strip()

    def location(self, location: _CXSourceLocation) -> Loc:
        """Resolve a source location to its file, line and column."""
        handle = ctypes.c_void_p()
        line = ctypes.c_uint()
        column = ctypes.c_uint()
        offset = ctypes.c_uint()
        self.lib.clang_getSpellingLocation(
            location,
            ctypes.byref(handle),
            ctypes.byref(line),
            ctypes.byref(column),
            ctypes.byref(offset),
        )
        return Loc(
            file=self.text(self.lib.clang_getFileName(handle)),
            line=line.value or 0,
            col=column.value or 0,
        )

    def extent(self, cursor: _CXCursor) -> Span:
        """The source range a cursor spans, as its start and end locations."""
        extent = self.lib.clang_getCursorExtent(cursor)
        return Span(
            start=self.location(self.lib.clang_getRangeStart(extent)),
            end=self.location(self.lib.clang_getRangeEnd(extent)),
        )

    def cursor_usr(self, cursor: _CXCursor) -> str:
        """Clang's canonical identity for the declaration behind a cursor."""
        return self.text(self.lib.clang_getCursorUSR(cursor))

    def cursor_spelling(self, cursor: _CXCursor) -> str:
        """The declaration's name, unqualified."""
        return self.text(self.lib.clang_getCursorSpelling(cursor))

    def cursor_type_text(self, cursor: _CXCursor) -> str:
        """The declaration's function type, for the report's ``type`` field."""
        cursor_type = self.lib.clang_getCursorType(cursor)
        return self.text(self.lib.clang_getTypeSpelling(cursor_type))

    def is_definition(self, cursor: _CXCursor) -> bool:
        """True when the cursor is where the function's body is written."""
        return bool(self.lib.clang_isCursorDefinition(cursor))

    def flag(self, symbol: str, cursor: _CXCursor) -> bool:
        """What a marker probe answers, and ``False`` when it is not exported."""
        if not self.present[symbol]:
            return False
        return bool(getattr(self.lib, symbol)(cursor))

    def has(self, symbol: str) -> bool:
        """True when this library exports ``symbol``."""
        return self.present[symbol]

    def _unit(
        self,
        index: ctypes.c_void_p,
        filename: Path,
        args: list[str],
    ) -> tuple[ctypes.c_void_p | None, list[str]]:
        """Ask libclang for a translation unit, and say why when it refuses."""
        encoded = [os.fsencode(arg) for arg in args]
        argv = (ctypes.c_char_p * len(encoded))(*encoded) if encoded else None
        unit = ctypes.c_void_p()
        code = self.lib.clang_parseTranslationUnit2(
            index,
            os.fsencode(str(filename)),
            argv,
            len(encoded),
            None,
            0,
            0,
            ctypes.byref(unit),
        )
        if code == _CXERROR_SUCCESS and unit.value:
            return unit, []
        failure = f"libclang could not parse {filename}: CXErrorCode {code}"
        return None, [failure]

    def _diagnostics(self, unit: ctypes.c_void_p) -> tuple[list[str], bool]:
        """Every diagnostic, formatted, and whether any of them fails the unit."""
        options = self.lib.clang_defaultDiagnosticDisplayOptions()
        texts: list[str] = []
        failed = False
        for position in range(self.lib.clang_getNumDiagnostics(unit)):
            diagnostic = self.lib.clang_getDiagnostic(unit, position)
            try:
                texts.append(
                    self.text(self.lib.clang_formatDiagnostic(diagnostic, options)),
                )
                if self.lib.clang_getDiagnosticSeverity(diagnostic) >= _SEVERITY_ERROR:
                    failed = True
            finally:
                self.lib.clang_disposeDiagnostic(diagnostic)
        return texts, failed


class _Walker:
    """The visitor that records what one translation unit's AST holds."""

    def __init__(self, api: _Api) -> None:
        """Record into ``decls`` and ``refs`` as the walk goes."""
        self._api = api
        self._decls: dict[str, tuple[bool, Decl]] = {}
        self._refs: dict[tuple[str, str, int, int], Ref] = {}

    @property
    def decls(self) -> list[Decl]:
        """One declaration per USR, the definition where the unit holds one."""
        return [decl for _, decl in self._decls.values()]

    @property
    def refs(self) -> list[Ref]:
        """One record per reference site, in the order the walk met them."""
        return list(self._refs.values())

    def on_cursor(
        self,
        cursor: _CXCursor,
        _parent: _CXCursor,
        _client_data: int | None,
    ) -> int:
        """Record this node, then hand the recursion back to libclang."""
        self._record_reference(cursor)
        self._record_declaration(cursor)
        return _CHILD_VISIT_RECURSE

    def _record_reference(self, cursor: _CXCursor) -> None:
        """Record a reference when the node resolves to a function."""
        if self._api.lib.clang_isDeclaration(self._api.lib.clang_getCursorKind(cursor)):
            return
        referenced = self._api.lib.clang_getCursorReferenced(cursor)
        if self._api.lib.clang_Cursor_isNull(referenced):
            return
        if self._api.lib.clang_getCursorKind(referenced) not in _FUNCTION_CURSORS:
            return
        usr = self._api.cursor_usr(referenced)
        if not usr:
            return
        location = self._api.location(self._api.lib.clang_getCursorLocation(cursor))
        if not location.file:
            return
        key = (usr, location.file, location.line, location.col)
        self._refs.setdefault(key, Ref(usr=usr, file=location.file, loc=location))

    def _record_declaration(self, cursor: _CXCursor) -> None:
        """Record a definition, or a member whose marker clang does not define."""
        spelling = self._api.cursor_spelling(cursor)
        kind = _decl_kind(self._api.lib.clang_getCursorKind(cursor), spelling)
        if kind is None:
            return
        location = self._api.location(self._api.lib.clang_getCursorLocation(cursor))
        markers = self._markers(cursor, location)
        definition = self._api.is_definition(cursor)
        if not definition and not markers & _BODYLESS_MARKERS:
            return
        usr = self._api.cursor_usr(cursor)
        if not usr or not location.file:
            return
        decl = Decl(
            usr=usr,
            kind=kind,
            name=spelling,
            file=location.file,
            loc=location,
            span=self._api.extent(cursor),
            markers=markers,
            type_text=self._api.cursor_type_text(cursor),
        )
        current = self._decls.get(usr)
        if current is None or (definition and not current[0]):
            self._decls[usr] = (definition, decl)

    def _markers(self, cursor: _CXCursor, location: Loc) -> frozenset[str]:
        """The keeper ladder's markers, as this libclang can report them."""
        found = {name for name, symbol in _MARKERS if self._api.flag(symbol, cursor)}
        if not self._api.has("clang_CXXMethod_isDeleted") and deleted_in_source(
            location.file,
            location.line,
        ):
            found.add("deleted")
        return frozenset(found)
