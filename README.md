# deadweight

Find C and C++ functions that only your tests reach.

A function defined in production code that nothing in production calls is dead weight: it costs compile time, review attention and binary size, and it is invisible to every unused-code warning because the one test that exercises it looks like a legitimate caller. `deadweight` answers that question directly — which production functions are reached only by tests — and refuses to answer when the evidence is incomplete.

## What it gives you

- **A test-only verdict, not a generic list of unused functions.** Six buckets: `candidates` (the removals), `keepers` (virtuals and compiler-owned members), `special_members` (constructors and destructors, your call), `dead_anywhere`, `entry_points`, `ambiguous`.
- **Transitive.** A helper reached only through a function that is itself test-only is reported too, with the caller named.
- **Conservative by construction.** Virtual and overriding methods, `= default` and `= delete` members are never proposed; constructors, destructors and conversion operators are listed for a human decision.
- **A second opinion from the linker.** Give it `--print-gc-sections` output and the verdicts become the linker's, including the paths a static graph cannot see; a contradiction exits non-zero instead of guessing.
- **Deterministic.** `report.json` has a versioned schema, sorted rows and no timestamps, so it diffs cleanly between runs and works as a CI gate.
- **Nothing to install at run time.** Python 3.11+, stdlib only, talks to the libclang you already have.

## Supported codebases

- **Languages:** C and C++, any standard your compiler accepts.
- **Input:** a `compile_commands.json` from CMake, Meson, Bazel or any compilation database.
- **Layout:** one or more production roots and one or more test roots in the same tree, with tests that call production code.
- **Linker tier:** Linux and other ELF targets built with `-ffunction-sections -Wl,--gc-sections`; elsewhere the AST verdict stands alone and the report says so.
- **Compiler:** any libclang ≥ 16. CI runs 16, 18, 20 and 22.
- **Not supported:** LTO builds for the linker tier, Windows/PE, runtime coverage.

## Install

```bash
uv tool install deadweight     # or: pipx install deadweight
```

You need libclang (from your LLVM install) and, for the linker tier, `nm`, `readelf` and `c++filt`. `deadweight doctor` tells you what it found.

## Use

```bash
deadweight scan --compile-commands build/compile_commands.json --prod client,src --tests tests
```

or, with `.deadweight.toml` committed:

```bash
deadweight scan
```

## Exit codes

| Code | Meaning |
| --- | --- |
| 0 | no candidates, or the gate did not trip |
| 1 | usage or environment error — a flag, the configuration file, the compilation database, a missing `--gc-report`/`--gc-report-tests` file, or libclang |
| 2 | the graph is incomplete — a translation unit failed to parse, so no verdict is trustworthy |
| 3 | the linker or the object symbols contradict the graph |
| 4 | `--fail-on` tripped |

## What it does not do

It reports functions and nothing else — no unused types, files, globals or targets. It does not run your tests and is not a coverage tool. It cannot see virtual dispatch or function-pointer identity, which is why those functions are keepers rather than candidates. Deleting code is always your call.

## Documentation

- [`docs/verdicts.md`](docs/verdicts.md) — the operator manual: every flag, the six buckets, the linker GC and object-symbol ladders, and the limits.
- [`docs/compatibility.md`](docs/compatibility.md) — the libclang contract: the version 16 floor, the tested matrix, `deadweight doctor` and the nightly canary.
- [`docs/design.md`](docs/design.md) — why the front end is libclang and not clangd, SCIP or libTooling, and the per-translation-unit facts seam.

## Licence

Apache-2.0.
