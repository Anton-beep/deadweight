# Design: the front end and the facts seam

`deadweight` answers exactly one question: which first-party C and C++ functions do production translation units never reach while the tests do. The valuable half of the tool — the two-root model, the transitive cascade with caller attribution, the keeper ladder, the symbol matching, the linker tiers, the report contract, the fail-closed exits — is compiler-agnostic Python. Only the front end that turns source into a reference graph is coupled to a compiler, so the design is one decision about that front end and one decision about the data it hands over.

## Decision: libclang through `ctypes`

The front end loads the user's libclang shared library through `ctypes` and declares the thirty-odd C entry points the analysis needs. The chosen library and its version go into the report and the cache key, so a result is always attributable to the compiler that produced it.

The C ABI is the strongest durability seam available. It is source- and binary-stable across the LLVM 16–22 window the tool supports, exported by every distribution's libclang, and free of the debug-text and index assumptions that make the alternatives fragile. `clang_getCursorUSR` also gives the analysis something a text front end has to reconstruct: a canonical identity for a function. Declaration/definition merging becomes a dictionary keyed by USR instead of a three-way merge by source location and a `prev` redeclaration link, and an overload set is distinguished by the USR's signature rather than by name matching.

Three consequences make the port cheaper than it looks. One parse per translation unit replaces two full AST dumps per unit, so a cold run is faster and its peak memory is one translation unit's AST per worker rather than a project-wide text dump. The markers arrive one call each (`clang_CXXMethod_isVirtual`, `isPureVirtual`, `isDefaulted`, `isDeleted`), and virtual is a strict superset of the old override rule — an override is virtual, and over-keeping is the documented-safe direction. And the only code that knows libclang is `cindex.py`: everything above it consumes records, tests as records, and does not know whether they came from a C library, a wheel or a fixture.

## Rejected alternatives

| Alternative | Why it lost |
| --- | --- |
| A `clang-query` text parser, pinned by a container | Zero porting work and exact parity, but it freezes the tool at one clang and moves the version problem into a Docker tag. The text it reads is the AST dumper's debug format, which carries no stability promise. |
| `clangd` over LSP | A stable protocol, no extra library, but the analysis needs a warm background index (a cold index under-reports, so the tool would have to refuse), costs a `callHierarchy/incomingCalls` round trip per function, loses USR-based canonical identity, and forces override detection down to a source-text heuristic. A CI gate that takes minutes and fails when an index is cold is not a gate. |
| SCIP ingest | The most durable format and the best CI story, but `scip-clang` is beta against a specific clang, which reintroduces the coupling one level out, and SCIP carries no virtual/defaulted/deleted markers, so the keeper ladder needs source-text scanning anyway. Kept as a documented extension point: the facts seam is modelled so a SCIP adapter can emit the same records. |
| libTooling (the C++ API) | The most capable front end, and the wrong artifact to publish: it means shipping a compiled C++ binary per platform and per LLVM version, rebuilding it on every clang release, and asking a Python tool's users to trust a prebuilt executable. The C ABI buys nearly all of the same information for none of that. |
| A linker-only tool | Cannot distinguish test-only from dead-anywhere without a second link, has no source locations or caller attribution, and misses header-inline functions entirely: at `-O2` an inlined test-only function never appears in either GC report. The linker is the explanation's second opinion, not its replacement. |

## The facts seam

The front end emits one record per translation unit and knows nothing about roots, tests or verdicts. The record is the integration point a future SCIP or clangd adapter would target, it is what the cache stores, and it is what the unit suite feeds the analysis.

```json
{
  "facts_version": 1,
  "tu": "src/core/framer.cpp",
  "status": "ok",
  "compiler": {"libclang": "22.1.8", "version": "clang version 22.1.8"},
  "decls": [
    {
      "usr": "c:@N@example@F@Fnv1a64#*1c#",
      "kind": "function",
      "name": "Fnv1a64",
      "file": "src/utils/fnv1a.cpp",
      "loc": {"file": "src/utils/fnv1a.cpp", "line": 12, "col": 1},
      "span": {
        "start": {"file": "src/utils/fnv1a.cpp", "line": 12, "col": 1},
        "end": {"file": "src/utils/fnv1a.cpp", "line": 18, "col": 2}
      },
      "markers": [],
      "type_text": "unsigned long (char *)"
    }
  ],
  "refs": [
    {"usr": "c:@N@example@F@Fnv1a64#*1c#", "file": "src/core/framer.cpp", "loc": {"file": "src/core/framer.cpp", "line": 140, "col": 17}}
  ],
  "diags": []
}
```

`kind` is one of `function`, `method`, `constructor`, `destructor`, `conversion` or `assignment`. `markers` carries `virtual`, `pure`, `defaulted` and `deleted` exactly as the API reports them, and it is the only thing the keeper ladder needs. `status` is `ok` or `error`; an `error` record carries the diagnostics and, per the fail-closed rule, poisons the run so no report is written. `diags` is otherwise informational.

The analysis layer computes everything else: the USR grouping, the innermost enclosing production definition for each reference (from spans), the reference graph, the roots, the cascade fixpoint, the buckets and the verdict ladders. A record is versioned by `facts_version`, independently of the report schema and of the package version, because a cached record and a report consumer each have to detect a layout change on their own.

## The worker model

One process per translation unit, from a `multiprocessing` pool with the `spawn` context — never `fork`, because forking after libclang has initialised can deadlock inside the AST library. A worker parses its unit, walks the AST once with `clang_visitChildren`, disposes the translation unit, and returns the record. A libclang crash therefore costs one translation unit and lands as `status: "error"`, which exits 2; it can never silently shrink the graph.

Each worker `chdir`s to the compilation entry's directory before parsing and restores the previous directory in a `finally` block. This is not tidiness: libclang resolves relative include paths against the process's current directory rather than against the entry's `directory` field, so skipping the `chdir` produces a graph that looks plausible and is wrong on any build tree that uses a relative `-I`.

The cache is a directory of records under the user's cache root, keyed by `sha256(unit path, directory, arguments, facts_version, libclang version, tree digest)`, where the tree digest covers the first-party sources including headers. A cache is an optimisation and never a source of truth: a missing file, another layout or a truncated write reads as a miss. `--no-cache` bypasses it.

## What the loss costs

libclang exposes fewer expression forms than clang-query's matchers, so constructor-call and conversion edges are recovered best-effort. That loss cannot produce an unsafe proposal: constructors, destructors, conversion operators and assignment operators are `special_members` and are never proposed for removal. It can only move a special member from "test-only evidence" to "unknown", which is the conservative direction the tool already promises everywhere else — under-reporting beats deleting a function a mock or the runtime dispatch path needs.
