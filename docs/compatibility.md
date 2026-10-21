# The libclang compatibility contract

`deadweight` talks to the C ABI of the libclang that is already installed on the machine it runs on. There is no `clang-query`, no `clangd`, no `libclang` Python package and no compiled extension in the wheel, so the only thing a future LLVM release can break is the small set of C entry points declared in `src/deadweight/cindex.py`. This document states which of them the tool needs, how it finds the library, what CI actually runs, and what `deadweight doctor` reports when something is missing.

## The supported floor: libclang 16

The oldest supported libclang is **16**, the first release that exports `clang_CXXMethod_isDeleted`. A library that does not export it still scans: the `= delete` marker is read from the declaration's source line instead, and `doctor` says so. Every other entry point the tool calls has been in the C API since before 16.

A library older than 16 is not refused, because the scan is still usable, but it is not a version CI tests and `doctor` prints a warning naming the floor. `--require-libclang MAJOR` is the hard form of the same statement: a run that pins a major version fails with exit 1 when the library it found is a different one, so a CI job cannot silently scan with another clang.

## The probed entry points

The tool declares and probes these thirty-four functions, and nothing else. `clang_CXXMethod_isDeleted` is the only optional one; a missing required entry point is a load error naming the library and every symbol it lacks, never a silently weaker scan.

```
clang_createIndex                      clang_getCursorUSR
clang_disposeIndex                     clang_getCursorSpelling
clang_parseTranslationUnit2            clang_getCursorReferenced
clang_disposeTranslationUnit           clang_Cursor_isNull
clang_getTranslationUnitCursor         clang_getCursorLocation
clang_getNumDiagnostics                clang_getCursorExtent
clang_getDiagnostic                    clang_getRangeStart
clang_getDiagnosticSeverity            clang_getRangeEnd
clang_formatDiagnostic                 clang_getSpellingLocation
clang_defaultDiagnosticDisplayOptions  clang_getFileName
clang_disposeDiagnostic                clang_getCursorType
clang_getCString                       clang_getTypeSpelling
clang_disposeString                    clang_isCursorDefinition
clang_getClangVersion                  clang_CXXMethod_isVirtual
clang_visitChildren                    clang_CXXMethod_isPureVirtual
clang_getCursorKind                    clang_CXXMethod_isDefaulted
clang_isDeclaration                    clang_CXXMethod_isDeleted (optional)
```

The set is deliberately small: it is the whole of what the analysis needs to know — canonical identity (USR), source spans, reference targets, the four markers and the diagnostics. Adding an entry point is a compatibility event and belongs in a changelog entry, because it raises the floor for every user.

## How the library is found

The search is ordered from the most explicit answer to the most implicit, and the first usable library wins:

1. `--libclang PATH`.
2. `DEADWEIGHT_LIBCLANG`, which is also how CI points the suite at the matrix's library.
3. The known install prefixes and the `ldconfig -p` cache, newest version first: `/usr/lib` and `/usr/lib64` (including `llvm-*/lib`), the Homebrew prefixes on macOS, and the versioned names beside them.
4. The `libclang` wheel's bundled library, when that distribution happens to be installed.

A path the user named is authoritative. When it cannot be loaded the run fails with exit 1 and prints every reason it has, instead of quietly scanning with a different library, because a scan done with the wrong clang is worse than no scan at all. `doctor` prints the whole trail: each path the search considered and why it lost.

`libclang-cpp` is never a candidate. It is the C++ API and does not export a single symbol the tool calls.

## deadweight doctor

`deadweight doctor` is the runtime counterpart of this contract, and its output is what an issue should quote:

- the library path the search chose, its version number and its `clang_getClangVersion` banner;
- the supported floor and, when the library is older, the warning that says the scan works but is untested there;
- whether the linker tier is available on this machine, which is `nm`, `readelf` and `c++filt` on `PATH`;
- one line per probed symbol, sorted, saying `available` or `missing`;
- a note when the deleted marker is read from the source line instead of the API.

Pass `--compile-commands PATH` to also have `doctor` report how many translation units the database holds, which distinguishes "the library is fine" from "the database is empty". `doctor` exits 0 whenever it could produce its report; use `--require-libclang` to make a version mismatch exit 1.

## What CI runs

The matrix is the contract, and the workflow in `.github/workflows/ci.yml` is the same suite on every entry:

| Job | libclang | Runs |
| --- | --- | --- |
| `checks` | 16, 18, 20, 22 from apt.llvm.org on `ubuntu-24.04` | ruff, `mypy src`, the full `pytest` suite including the conformance corpus |
| `canary` | the current LLVM snapshot, nightly and `workflow_dispatch` | the full `pytest` suite, non-blocking (`continue-on-error`) |

The conformance corpus copies checked-in small C++ trees — each with a checked-in `compile_commands.json` — and runs `deadweight scan` in process over the copy, asserting the verdict sets, so a change in libclang's behaviour shows up as a corpus failure rather than as a quietly different verdict on a user's tree. Nothing is compiled: the trees are sources and a database, and libclang does the parsing. The facts records are expected to be identical across the four versions except for the `compiler` block; a difference is a durability bug, not a tolerance.

The nightly canary runs against LLVM `main` so that a renamed or removed entry point is visible before it is a user's bug. It never blocks a merge: a snapshot that moved under us is news, not a broken pull request. The workflow's `DEADWEIGHT_LIBCLANG` environment variable is also the documented way for anyone to run the suite against a specific library.

## The linker tier

The AST verdict is portable; the linker tier is not. GC-report parsing and the object-symbol ladder are ELF-only, and need `nm`, `readelf` and `c++filt`. Elsewhere, or without binutils, every candidate reports `cross_check: "unavailable"` and `gc: "unavailable"`, and the report says in the same sentence that the verdict rests on the AST alone, so nobody reads a Linux-shaped conclusion off a macOS run. A report that carries no per-function sections is treated as unusable rather than trusted, and an LTO link must not be supplied at all, because the optimizer removes unreachable functions before the linker's GC sees them; `docs/verdicts.md` gives the recipes and the reason for each.

## Versioning and the unit of compatibility

The package version, the report schema identifier and the facts record version move independently: the version follows semantic versioning, `deadweight.report/1` names the JSON layout, and `facts_version = 1` names the cached per-translation-unit record. A breaking change to either layout is a major bump of that identifier, not of the package alone, because a report consumer and a stale cache each need to detect it on their own.

The libclang contract versions differently again: it is a floor plus a tested matrix, not a range of releases. Raising the floor is a decision recorded in `CHANGELOG.md`, and the machinery that makes it visible — `doctor`'s symbol list, `--require-libclang`, the corpus, the canary — exists so raising it is a deliberate act rather than an accident of a user's distribution.


## Debian and Ubuntu: install the common package too

`libclang-N-dev` and `libclang1-N` are not enough on a Debian derivative. The builtin headers — `<prefix>/lib/clang/N/include/stddef.h` and its neighbours — ship in `libclang-common-N-dev`, and without them libclang refuses every translation unit that includes a standard header:

```
fatal error: 'stddef.h' file not found
```

The tool turns that into a refused run rather than a silent one, and it will not borrow another major version's directory to paper over it: a 19-era resource directory handed to libclang 16 produces `ASTReadError` on every unit, which is worse than a clear failure. Install the package that matches the library:

```bash
sudo apt-get install -y libclang1-18 libclang-18-dev libclang-common-18-dev
```

`deadweight doctor` prints the library it chose and the version it reports, which is usually enough to see that the wrong package set is installed.
