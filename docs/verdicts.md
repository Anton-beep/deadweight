# Verdicts, ladders and limits (operator manual)

`deadweight` answers one review question mechanically: a function is defined in production code, and every production call site is gone — only the tests still call it. Deleting those is safe; keeping them is dead weight in the shipped binary. The scan is written for the tree where tests exercise production code directly, so it can tell "only the tests reach this" apart from "nothing reaches this".

## What you need

- Python 3.11 or newer, and a libclang shared library of version 16 or newer. The front end speaks the libclang C ABI through `ctypes`, so it needs no `clang-query`, no `clangd`, no clang development headers and no Python packages beyond the standard library. `docs/compatibility.md` states the contract and how to point the tool at a particular library.
- `nm`, `readelf` and `c++filt` for the linker and object-symbol tiers. Missing binutils turns the cross-check into `cross_check: "unavailable"` with a note in the report; the AST verdicts are unaffected.

## Setup

The scan reads a `compile_commands.json`, so the tree must be configured with the export on. With CMake that is a configure-time change only, nothing is rebuilt:

```bash
cmake -S . -B build -DCMAKE_EXPORT_COMPILE_COMMANDS=ON
```

The production objects are read for the cross-check (`--prod-build-dir`, by default the compilation database's directory); they are used as-is, so a stale build tree produces `absent` rows rather than wrong verdicts.

A repository can state its own roots once in `.deadweight.toml` at the top of the tree. The run discovers the file from the working directory upward, and any flag overrides the file. An unknown key is a usage error, because a typo that silently disables a production root turns an incomplete scan into a confident clean one:

```toml
[scan]
prod_roots = ["client", "logging"]
test_roots = ["tests"]
exclude = ["third_party/**", "**/generated/**"]
compile_commands = "build/compile_commands.json"
# out = "runs/deadweight"
# format = ["md", "json", "tsv"]

[linker]
gc_reports = ["build-gc/gc-prod.txt"]
gc_report_tests = ["build-gc/gc-tests.txt"]
prod_binary = "build-gc/app"
test_binaries_dir = "build-gc-tests"

[gate]
fail_on = "candidates"

[[ignore]]
symbol = "example::LegacyShim"
reason = "kept for the termux build until 2027"
until = "2027-01-01"
```

Every `[[ignore]]` entry needs a `reason`; `until` is optional and parsed as a date. An ignored symbol is suppressed from `candidates` and `dead_anywhere`, and `--strict-ignores` turns an entry that matches nothing, or one whose `until` has passed, into exit 1, so the allowlist cannot rot into a permanent hole.

## Run

```bash
deadweight scan --compile-commands build/compile_commands.json --prod client,src --tests tests
```

or, with `.deadweight.toml` committed:

```bash
deadweight scan
```

Without `--out` the tool prints the markdown summary to stdout and writes nothing, which is what a first-time run should do; with `--out DIR` it writes the renderings and prints a one-line summary.

| Flag | Meaning |
| --- | --- |
| `--compile-commands PATH` | the compilation database to read; defaults to `compile_commands.json` or `build/compile_commands.json` under the scan root |
| `--config PATH` | the configuration file to read instead of discovering `.deadweight.toml` |
| `--prod A,B` | comma-separated production roots; required, from the flag or `[scan] prod_roots` |
| `--tests C,D` | comma-separated test roots; without one, an unreachable production function is `dead_anywhere` rather than a candidate |
| `--exclude GLOB` | a glob over scan-root-relative paths of translation units never to scan; repeatable |
| `--jobs N` | parser processes; defaults to the CPU count, and each process holds one translation unit's AST |
| `--out DIR` | write `report.md`, `report.json` and `report.tsv` (or the `--format` subset) into this directory |
| `--format md,json,tsv` | the renderings `--out` writes; defaults to all three |
| `--gc-report FILE` | a production link's `--print-gc-sections` output; repeatable |
| `--gc-report-tests FILE` | a test link's `--print-gc-sections` output; repeatable |
| `--prod-binary FILE` | the linked production binary, read for surviving symbols |
| `--test-binaries-dir DIR` | a directory of linked test binaries, read for present symbols |
| `--prod-build-dir DIR` | the build tree the object-symbol ladder reads; defaults to the compilation database's directory |
| `--libclang PATH` | the libclang shared library to load, ahead of the ordered search |
| `--require-libclang MAJOR` | refuse to run against any other libclang major version, as a usage error |
| `--no-cascade` | report direct references only, not what they transitively reach |
| `--no-cross-check` | skip the linker and object-symbol tiers |
| `--no-cache` | parse every unit instead of reusing cached facts |
| `--fail-on none\|candidates` | the gate: exit 4 when it trips; defaults to `none` |
| `--strict-ignores` | exit 1 when an `[[ignore]]` entry is stale or expired |

The per-translation-unit facts cache lives under `$XDG_CACHE_HOME/deadweight/<scan-root digest>` (`~/.cache/deadweight/...` without `XDG_CACHE_HOME`), never in the analysed tree. Its key covers the unit, its flags and directory, the facts record version, the libclang version and a digest over the first-party sources including headers, so a header edit or a libclang upgrade misses rather than lies. A cache is an optimisation and never a source of truth: anything unusable reads as a miss and the unit is parsed again.

## Output contract

`report.md` is for humans, `report.json` (`schema: deadweight.report/1`) is for tooling, and `report.tsv` is one row per function for spreadsheet triage. The JSON has sorted rows, scan-root-relative paths, no timestamps and a versioned schema, so two runs over one tree produce the same bytes and a report diffs cleanly between runs. `facts_version` in the cached records is versioned independently of the package version and of the report schema.

The markdown report opens with the translation-unit counts, the number of reference targets that resolved to no scanned declaration, and the schema-level explanation of what a candidate row means. The JSON's `inputs` block records every input to the run, which is what an issue report should quote.

The buckets are deliberately separate so a verdict is auditable:

- `candidates` (in JSON; "Candidates (test-only)" in the markdown): defined in production, unreachable from every production call site, and reached by at least one test or by another candidate. These are the removal proposals.
- `keepers`: virtual/override methods with no production call site (a mock or the runtime dispatch path may call them — the static graph cannot see that) and compiler-owned `= default` / `= delete` members. Reported, never proposed for removal.
- `special_members`: constructors, destructors, conversion operators and assignment operators. Deleting one changes construction/copy semantics, so they are listed for a human decision instead of being proposed.
- `dead_anywhere`: defined in production, referenced by nobody at all.
- `entry_points`: `main`, reached by the C runtime rather than by a call site.
- `ambiguous`: several functions share one definition anchor (macro-generated code); the graph cannot tell them apart.

A candidate row also carries two hints that cost nothing to compute and catch what the graph cannot see: `header-defined` when the definition is in a header, and `referenced-by-mock` when a test path contains `mock`.

## Exit codes

| Code | Meaning |
| --- | --- |
| 0 | no candidates, or the gate did not trip |
| 1 | usage or environment error: a flag, the configuration file, the compilation database or libclang |
| 2 | the graph is incomplete — a translation unit failed to parse, or a `--gc-report` file is missing, so no verdict is trustworthy |
| 3 | the linker or the object symbols contradict the graph |
| 4 | `--fail-on` tripped |

Precedence is `2` before `3` before `4` before `0`: an incomplete graph outranks everything, because no verdict is trustworthy. An incomplete run writes no report at all, because a report with one translation unit silently missing reads exactly like a clean one.

## Linker GC cross-check (optional, strongest evidence)

The object ladder below reads symbols; the linker's own garbage collection reads reachability. Give the scan a `--print-gc-sections` report and its verdicts become the linker's: a candidate whose `.text.<symbol>` section the production link discarded is unreachable from every production root, including the paths a static graph cannot see (virtual dispatch, vtables, function-pointer tables). Produce the reports in throwaway analysis trees, because the flags change code generation:

```bash
cmake -S . -B build-gc -DCMAKE_EXPORT_COMPILE_COMMANDS=ON \
    -DCMAKE_CXX_FLAGS="-ffunction-sections -fdata-sections" \
    -DCMAKE_EXE_LINKER_FLAGS="-Wl,--gc-sections -Wl,--print-gc-sections"
cmake --build build-gc --target app 2>&1 | grep -i 'unused section' > gc-prod.txt

cmake -S . -B build-gc-tests -DCMAKE_EXPORT_COMPILE_COMMANDS=ON \
    -DCMAKE_CXX_FLAGS="-ffunction-sections -fdata-sections" \
    -DCMAKE_EXE_LINKER_FLAGS="-Wl,--gc-sections -Wl,--print-gc-sections"
cmake --build build-gc-tests --target test_a test_b 2>&1 | grep -i 'unused section' > gc-tests.txt

deadweight scan --gc-report gc-prod.txt --gc-report-tests gc-tests.txt \
    --prod-binary build-gc/app --test-binaries-dir build-gc-tests
```

Both trees are throwaway: neither binary is a parity subject, and disabling extra build lanes keeps them fast. The test recipe builds the test targets explicitly rather than `all`, because the default build also links the production binary and its discarded sections would masquerade as test-link evidence. `--test-binaries-dir` needs every test binary built, and its symbol tables are what turn "no test link discarded it" into "a test binary contains it".

GNU ld, gold and lld formats are all parsed (GNU ld quotes the section and the file; lld prints `file:(section)`). The verdicts:

- `gc-confirmed`: the production link discarded the candidate's section.
- `gc-test-only`: discarded from the production link and present in a supplied test binary (without `--test-binaries-dir`, the weaker "no supplied test link discarded it").
- `gc-dead`: discarded from the production link, discarded by a supplied test link, and absent from every supplied test binary — three independent signals that no supplied test reaches it.
- `gc-not-in-tests`: discarded from the production link and absent from every supplied test binary, but no test link discarded it — its object may simply not be linked into any supplied test binary, so this is not a dead verdict.
- `gc-refuted`: the symbol is in the linked production binary, so the linker disagrees with the AST graph. This is the fail-closed contradiction (exit code 3).
- `gc-absent`: the link holds no evidence either way — the symbol never entered it (its archive member was never pulled in, or the object is not part of that link), or the linked binary could not be read. The object ladder then decides the row, and `gc_detail` says which case it was.
- `gc-inconclusive`: several linked symbols share the name and parameter count; the row needs a human. Only symbols from the scanned production objects and the discarded sections are considered, so a third-party or system namesake that survived the link can never answer for a candidate.

What the reports cannot see: links built without `-ffunction-sections` (bare `.text` sections are counted and skipped, and a report with no per-function sections is rejected rather than trusted), LTO (the optimizer removes unreachable functions before the linker's GC sees them, so they look kept), and GC roots that keep dead code alive — `--export-dynamic`, `--whole-archive`, `-u`, `KEEP` linker scripts, `__attribute__((used))` and `retain`. "Not discarded" never means "kept" on its own: the archive member may simply never have been pulled into that link, which is why `--prod-binary` and `--test-binaries-dir` exist — without them the scan degrades to `gc-absent`/the weaker `gc-test-only` instead of inventing a refutation. The GC tree must compile the same translation units as the tree the graph came from, or symbols come out `gc-absent`.

## Object-symbol ladder

Without a GC report, the cross-check compares the AST verdict against `nm`/`readelf` over the production objects. It matches a candidate to a linked symbol by demangled unqualified name, parameter count and constructor/destructor shape, preferring symbols defined in the object that owns the candidate's file; several survivors mean `inconclusive-symbol` rather than a wrong verdict. A candidate whose symbol is defined in the production objects with zero relocations to it is `ok`; one with no symbol at all is `absent` (header-only, or instantiated only in tests). The check can only refute, never confirm — an inlined production call leaves no relocation, so silence is not evidence of absence. Refutations are graded: a reference from the object that defines the candidate is `refuted-same-tu` (usually recursion), a reference from production code the AST already saw but found test-only is `refuted-cascade`, and only a reference the AST never saw at all is a real contradiction that forces exit code 3. Objects whose source translation unit is not one of the scanned production TUs are skipped as stale, so a renamed file's leftover object cannot refute a correct verdict.

## How it works

One libclang parse per translation unit replaces the two text passes a `clang-query` front end needs. The front end records every first-party function definition with its USR (clang's canonical identity), its source span, its markers (virtual, pure, defaulted, deleted) and its type spelling, plus every reference with the declaration it resolved to. Declaration/definition merging is a USR grouping, so a test translation unit's reference to a header declaration and the `.cpp` definition become one function.

Every reference is classified by the translation unit that contains it; production references are attributed to their innermost enclosing production function body, which makes the transitive fixpoint possible: a helper reached only through a function that is itself test-only is reported as a candidate too, with that caller in the `reached via (prod)` column (`--no-cascade` turns this off and reports only direct production references).

Virtuals and overrides seed the reachability roots, because their call sites are dispatch, which a static graph cannot see, so their bodies count as production-reachable. Without that, every helper behind an interface implementation would look test-only. A virtual or override with no production call site is reported as a keeper instead of a candidate, and so is a compiler-owned `= default` / `= delete` member.

Uninstantiated template bodies and dependent-name calls are not in the graph: a call inside a template body only becomes a reference where the template is instantiated. Every candidate therefore carries a `prod_mentions` list — production files that still mention the name — which catches the cases the graph cannot see. Treat a non-empty list as "read the call sites before deleting".

## Known limits

- Lambdas, compiler-generated special members and implicit instantiations of system templates are deliberately outside the graph; references to them count as unresolved targets in the report header (a nonzero number there is expected).
- Virtual dispatch and function-pointer identity are invisible to a static call graph; that is why keepers exist instead of a silent pass.
- The scan reports source-level functions, not symbols: removing a header-inline function removes it from every translation unit at once, which is exactly what the candidate list means.
- Source edits after a scan invalidate its line numbers. Delete only from a clean tree, re-run the scan after any edit, and treat cached per-unit results as invalid whenever the compile flags change (the cache key covers the file, the flags, the record version, the libclang version and a digest over the first-party sources).
- Both cross-checks match by name, parameter count and constructor/destructor shape, not by an exact mangled symbol, because asking clang for the exact symbol per candidate is dominated by re-parsing whole translation units. Overload sets that collapse to one candidate symbol can therefore still be misread; the `inconclusive-symbol` and `gc-inconclusive` rows are where that risk surfaced. With a GC report the linker's discarded-symbol universe also contains third-party sections, which is what makes the ambiguity visible in the first place.
- A virtual function is a reachability root, so a helper called only from an override is never proposed for removal even if the override itself is only called by tests. That is deliberate: under-reporting beats deleting a function a mock or the runtime dispatch path needs.
- The scan reports functions and nothing else: no unused types, globals, files, headers or build targets. It does not run the tests and is not a coverage tool.
