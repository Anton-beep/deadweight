## [Unreleased]

## [0.1.1] - 2026-10-21

### Fixed

- Macro-generated declarations carry an anchor taken from the macro's invocation site, and the `ambiguous` bucket groups on it. Grouping on the definition span split the twins on LLVM 16 and 18 and proposed them for deletion, while LLVM 20 and 22 called the same pair ambiguous. The facts record version moves to 2.
- A resource directory from another LLVM major is no longer handed to libclang; its builtin headers made every unit fail with `ASTReadError`.
- The CI's libclang pin no longer leaks into the discovery tests, and the jammy leg installs `libclang-common-N-dev` so the builtin headers exist at all.

## [0.1.0] - 2026-10-20

### Added

- The `deadweight` package: `deadweight scan` and `deadweight doctor`, the `deadweight.report/1` report schema and `facts_version = 1`, each versioned independently of the others.
- `docs/verdicts.md`, the operator manual: the question the scan answers, every flag, the six buckets, the linker GC and object-symbol ladders, and the known limits.
- `docs/compatibility.md`, the libclang contract: the version 16 floor, the probed C entry points, how the library is found, the 16/18/20/22 CI matrix, `deadweight doctor` and the non-blocking LLVM nightly canary.
- `docs/design.md`, why the front end is libclang through `ctypes` rather than clangd, SCIP or libTooling, and the per-translation-unit facts seam an alternative front end would target.
- `tests/unit/test_docs.py`, which reads the real `deadweight scan --help` output and the README's exit-code table, so a flag or an exit code cannot ship undocumented.
- Packaging and licence (Apache-2.0): Python 3.11 or newer with no runtime dependencies, `uv.lock` committed, and CI across libclang 16, 18, 20 and 22.
