## [Unreleased]

## [0.1.0] - 2026-10-20

### Added

- The `deadweight` package: `deadweight scan` and `deadweight doctor`, the `deadweight.report/1` report schema and `facts_version = 1`, each versioned independently of the others.
- `docs/verdicts.md`, the operator manual: the question the scan answers, every flag, the six buckets, the linker GC and object-symbol ladders, and the known limits.
- `docs/compatibility.md`, the libclang contract: the version 16 floor, the probed C entry points, how the library is found, the 16/18/20/22 CI matrix, `deadweight doctor` and the non-blocking LLVM nightly canary.
- `docs/design.md`, why the front end is libclang through `ctypes` rather than clangd, SCIP or libTooling, and the per-translation-unit facts seam an alternative front end would target.
- `tests/unit/test_docs.py`, which reads the real `deadweight scan --help` output and the README's exit-code table, so a flag or an exit code cannot ship undocumented.
- Packaging and licence (Apache-2.0): Python 3.11 or newer with no runtime dependencies, `uv.lock` committed, and CI across libclang 16, 18, 20 and 22.
