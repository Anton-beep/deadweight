"""The command line: the merged configuration, the pipeline, and the exit contract.

Every test here drives ``main`` rather than the pieces underneath it, because the
contract being pinned is the one an operator sees: which flags override the file,
which failures write a report, and which integer comes back out. The compiler and the
linker are replaced by records, so the suite needs no libclang and no linked program;
the real pipeline is exercised by ``tests/integration/test_end_to_end.py``.
"""

from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path
from typing import TYPE_CHECKING

import pytest

from deadweight import cindex, cli, config, discover, frontend, linker
from deadweight.frontend import TranslationUnit
from deadweight.model import (
    STATUS_ERROR,
    STATUS_OK,
    Candidate,
    Decl,
    Facts,
    Loc,
    Ref,
    Span,
)

if TYPE_CHECKING:
    from collections.abc import Callable

PROD = "src/core.cpp"
BROKEN = "src/broken.cpp"
TEST = "tests/core_test.cpp"
"""The three units a scan sees: a production file, one that does not parse, a test."""


def project(root: Path) -> list[TranslationUnit]:
    """Write the sources and hand back the units the loader would return.

    The database itself is written too, because the run refuses to start without the
    file it was pointed at even though the loader is replaced below.
    """
    for name in (PROD, BROKEN, TEST):
        path = root / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text("int nothing(int value) { return value; }\n", encoding="utf-8")
    (root / "compile_commands.json").write_text("[]\n", encoding="utf-8")
    return [
        TranslationUnit(
            path=str(root / name),
            directory=str(root),
            arguments=["-std=c++17"],
        )
        for name in (PROD, BROKEN, TEST)
    ]


def declaration(usr: str, name: str, line: int) -> Decl:
    """One production definition; the file is spelled as the front end spells it."""
    start = Loc(file=PROD, line=line, col=1)
    return Decl(
        usr=usr,
        kind="function",
        name=name,
        file=PROD,
        loc=start,
        span=Span(start=start, end=Loc(file=PROD, line=line, col=20)),
        markers=frozenset(),
        type_text="int (int)",
    )


def parsed(root: Path, *, broken: bool = False) -> list[Facts]:
    """The facts one run of the front end would produce for :func:`project`."""
    production = Facts(
        tu=str(root / PROD),
        status=STATUS_OK,
        decls=[
            declaration("usr@only", "only_in_tests", 1),
            declaration("usr@never", "never_called", 3),
        ],
    )
    test = Facts(
        tu=str(root / TEST),
        status=STATUS_OK,
        refs=[Ref(usr="usr@only", file=TEST, loc=Loc(file=TEST, line=3, col=10))],
    )
    third = Facts(
        tu=str(root / BROKEN),
        status=STATUS_ERROR if broken else STATUS_OK,
        diags=["expected ';' after return statement"] if broken else [],
    )
    return [production, test, third]


def install(
    monkeypatch: pytest.MonkeyPatch,
    root: Path,
    facts: list[Facts],
    *,
    contradicted: tuple[Candidate, ...] = (),
    cross_check: Callable[..., tuple[Candidate, ...]] | None = None,
) -> None:
    """Replace the compiler and the linker with records the tests control.

    ``cross_check`` is for the one test that wants the real tier to run, so that the
    notes it writes are the notes the report has to scrub.
    """
    units = project(root)
    monkeypatch.setattr(
        frontend,
        "load_compile_commands",
        lambda *args, **kwargs: units,
    )
    monkeypatch.setattr(frontend, "scan", lambda *args, **kwargs: facts)
    monkeypatch.setattr(
        linker,
        "run_cross_check",
        lambda *args, **kwargs: (
            cross_check(*args, **kwargs) if cross_check is not None else contradicted
        ),
    )


def contradiction() -> Candidate:
    """A candidate the linked program is supposed to have refuted."""
    return Candidate(
        usr="usr@only",
        name="only_in_tests",
        kind="function",
        file=PROD,
        line=1,
        col=1,
        cross_check="gc-refuted",
    )


def write_config(root: Path, text: str) -> Path:
    """Put a configuration file where the run discovers it."""
    path = root / config.CONFIG_NAME
    path.write_text(text, encoding="utf-8")
    return path


def test_flags_override_the_file(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """``--out`` and ``--format`` beat the file.

    That is the only merge order that lets a one-off run differ from the committed
    configuration, which is what makes the file safe to write for CI.
    """
    monkeypatch.chdir(tmp_path)
    install(monkeypatch, tmp_path, parsed(tmp_path))
    write_config(
        tmp_path,
        '[scan]\nprod_roots = ["src"]\ntest_roots = ["tests"]\n'
        'out = "from-the-file"\nformat = ["tsv"]\n',
    )

    code = cli.main(["scan", "--no-cache", "--out", "flagged", "--format", "md"])

    assert code == cli.ExitCode.CLEAN
    assert (tmp_path / "flagged" / "report.md").is_file()
    assert not (tmp_path / "from-the-file").exists()


def test_unknown_key_is_a_usage_error(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    """A typo in the file refuses the run instead of scanning the wrong roots."""
    monkeypatch.chdir(tmp_path)
    install(monkeypatch, tmp_path, parsed(tmp_path))
    write_config(tmp_path, '[scan]\nprod_root = ["src"]\n')

    code = cli.main(["scan"])

    assert code == cli.ExitCode.USAGE
    assert "prod_root" in capsys.readouterr().err


def test_ignore_requires_a_reason(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    """The refusal reaches the operator as a usage error, not a traceback."""
    monkeypatch.chdir(tmp_path)
    install(monkeypatch, tmp_path, parsed(tmp_path))
    write_config(tmp_path, '[[ignore]]\nsymbol = "only_in_tests"\n')

    code = cli.main(["scan", "--prod", "src", "--tests", "tests"])

    assert code == cli.ExitCode.USAGE
    assert "reason" in capsys.readouterr().err


def test_an_ignore_suppresses_a_candidate(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """An allowlisted symbol leaves the bucket, and the report says it was allowed."""
    monkeypatch.chdir(tmp_path)
    install(monkeypatch, tmp_path, parsed(tmp_path))
    write_config(
        tmp_path,
        '[scan]\nprod_roots = ["src"]\ntest_roots = ["tests"]\nout = "reports"\n'
        '[[ignore]]\nsymbol = "only_in_tests"\nreason = "kept for the termux build"\n',
    )

    code = cli.main(["scan", "--no-cache"])

    assert code == cli.ExitCode.CLEAN
    payload = json.loads((tmp_path / "reports" / "report.json").read_text("utf-8"))
    assert payload["candidates"] == []
    assert any("ignore" in note for note in payload["notes"])


def test_a_qualified_ignore_symbol_does_not_suppress_a_bare_name(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """An entry written for one namespace must not quiet another's function.

    ``qualified_name`` is empty in today's analysis, so ``topi::only_in_tests`` names
    nothing; matching its tail would hide a real candidate instead of leaving an
    allowlist entry the operator can see is idle.
    """
    monkeypatch.chdir(tmp_path)
    install(monkeypatch, tmp_path, parsed(tmp_path))
    write_config(
        tmp_path,
        '[scan]\nprod_roots = ["src"]\ntest_roots = ["tests"]\nout = "reports"\n'
        '[[ignore]]\nsymbol = "topi::only_in_tests"\n'
        'reason = "kept for the termux build"\n',
    )

    code = cli.main(["scan", "--no-cache"])

    assert code == cli.ExitCode.CLEAN
    payload = json.loads((tmp_path / "reports" / "report.json").read_text("utf-8"))
    assert [item["name"] for item in payload["candidates"]] == ["only_in_tests"]
    assert not any(note.startswith("ignore:") for note in payload["notes"])


def test_a_qualified_ignore_symbol_that_names_nothing_is_stale(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    """``--strict-ignores`` is the alarm an exact match relies on."""
    monkeypatch.chdir(tmp_path)
    install(monkeypatch, tmp_path, parsed(tmp_path))
    write_config(
        tmp_path,
        '[scan]\nprod_roots = ["src"]\ntest_roots = ["tests"]\n'
        '[[ignore]]\nsymbol = "topi::only_in_tests"\n'
        'reason = "kept for the termux build"\n',
    )

    code = cli.main(["scan", "--no-cache", "--strict-ignores"])

    assert code == cli.ExitCode.USAGE
    assert "topi::only_in_tests" in capsys.readouterr().err


def test_strict_ignores_flags_a_stale_entry(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    """An entry that matches nothing is a hole in the allowlist, so it fails."""
    monkeypatch.chdir(tmp_path)
    install(monkeypatch, tmp_path, parsed(tmp_path))
    write_config(
        tmp_path,
        '[scan]\nprod_roots = ["src"]\ntest_roots = ["tests"]\n'
        '[[ignore]]\nsymbol = "removed_last_year"\nreason = "kept for a branch"\n',
    )

    code = cli.main(["scan", "--no-cache", "--strict-ignores"])

    assert code == cli.ExitCode.USAGE
    assert "removed_last_year" in capsys.readouterr().err


def test_strict_ignores_flags_an_expired_entry(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    """An entry past its ``until`` is stale even when the symbol still matches."""
    monkeypatch.chdir(tmp_path)
    install(monkeypatch, tmp_path, parsed(tmp_path))
    write_config(
        tmp_path,
        '[scan]\nprod_roots = ["src"]\ntest_roots = ["tests"]\n'
        '[[ignore]]\nsymbol = "only_in_tests"\nreason = "kept for a branch"\n'
        'until = "2000-01-01"\n',
    )

    code = cli.main(["scan", "--no-cache", "--strict-ignores"])

    assert code == cli.ExitCode.USAGE
    assert "2000-01-01" in capsys.readouterr().err


def test_missing_compile_commands_exits_1(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    """A named database that is not there is a usage error, before any parsing."""
    monkeypatch.chdir(tmp_path)

    code = cli.main(
        [
            "scan",
            "--prod",
            "src",
            "--tests",
            "tests",
            "--compile-commands",
            "absent.json",
        ],
    )

    assert code == cli.ExitCode.USAGE
    assert "absent.json" in capsys.readouterr().err


def test_missing_gc_report_exits_1_before_the_scan_starts(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    """A named gc report that is not there is a bad argument, not a bad graph.

    The graph has not been built yet, so exit 2 would name a failure that did not
    happen; the run refuses before it loads a library, parses a unit or writes a
    report, and names the missing file.
    """
    monkeypatch.chdir(tmp_path)
    install(monkeypatch, tmp_path, parsed(tmp_path))

    def forbidden(*_args: object, **_kwargs: object) -> list[Facts]:
        """A scan that must never be reached."""
        pytest.fail("the scan started before the gc report was checked")

    monkeypatch.setattr(frontend, "scan", forbidden)

    code = cli.main(
        [
            "scan",
            "--prod",
            "src",
            "--tests",
            "tests",
            "--no-cache",
            "--gc-report",
            "absent.txt",
            "--out",
            "reports",
        ],
    )

    assert code == cli.ExitCode.USAGE
    assert not (tmp_path / "reports").exists()
    assert "absent.txt" in capsys.readouterr().err


def test_unparsable_unit_exits_2_and_writes_no_report(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    """An incomplete graph writes nothing that could be read as a clean report."""
    monkeypatch.chdir(tmp_path)
    install(monkeypatch, tmp_path, parsed(tmp_path, broken=True))

    code = cli.main(
        [
            "scan",
            "--prod",
            "src",
            "--tests",
            "tests",
            "--no-cache",
            "--out",
            "reports",
        ],
    )

    assert code == cli.ExitCode.INCOMPLETE
    assert not (tmp_path / "reports").exists()
    assert "broken.cpp" in capsys.readouterr().err


def test_contradiction_exits_3(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A candidate the linked program contradicts outranks a clean run."""
    monkeypatch.chdir(tmp_path)
    install(
        monkeypatch,
        tmp_path,
        parsed(tmp_path),
        contradicted=(contradiction(),),
    )

    code = cli.main(
        ["scan", "--prod", "src", "--tests", "tests", "--no-cache", "--out", "reports"],
    )

    assert code == cli.ExitCode.CONTRADICTION
    assert (tmp_path / "reports" / "report.json").is_file()


def test_fail_on_candidates_exits_4(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The CI gate trips on a candidate, and still leaves the report behind."""
    monkeypatch.chdir(tmp_path)
    install(monkeypatch, tmp_path, parsed(tmp_path))

    code = cli.main(
        [
            "scan",
            "--prod",
            "src",
            "--tests",
            "tests",
            "--no-cache",
            "--fail-on",
            "candidates",
            "--out",
            "reports",
        ],
    )

    assert code == cli.ExitCode.GATE
    assert (tmp_path / "reports" / "report.json").is_file()


def test_exit_precedence_incomplete_beats_gate(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """No verdict is trustworthy on an incomplete graph, so exit 2 outranks exit 4."""
    monkeypatch.chdir(tmp_path)
    install(monkeypatch, tmp_path, parsed(tmp_path, broken=True))

    code = cli.main(
        [
            "scan",
            "--prod",
            "src",
            "--tests",
            "tests",
            "--no-cache",
            "--fail-on",
            "candidates",
        ],
    )

    assert code == cli.ExitCode.INCOMPLETE


def test_scan_prints_markdown_to_stdout_without_out(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    """A first run with no ``--out`` writes nothing and prints the report."""
    monkeypatch.chdir(tmp_path)
    install(monkeypatch, tmp_path, parsed(tmp_path))

    code = cli.main(["scan", "--prod", "src", "--tests", "tests", "--no-cache"])

    assert code == cli.ExitCode.CLEAN
    printed = capsys.readouterr().out
    assert "## Candidates (test-only)" in printed
    assert "only_in_tests" in printed
    assert not (tmp_path / "report.md").exists()


def test_scan_writes_three_reports_with_out(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """``--out`` writes the three documented renderings of one analysis."""
    monkeypatch.chdir(tmp_path)
    install(monkeypatch, tmp_path, parsed(tmp_path))

    code = cli.main(
        ["scan", "--prod", "src", "--tests", "tests", "--no-cache", "--out", "reports"],
    )

    assert code == cli.ExitCode.CLEAN
    reports = tmp_path / "reports"
    assert [path.name for path in sorted(reports.iterdir())] == [
        "report.json",
        "report.md",
        "report.tsv",
    ]
    payload = json.loads((reports / "report.json").read_text("utf-8"))
    assert payload["schema"] == "deadweight.report/1"
    assert [item["name"] for item in payload["candidates"]] == ["only_in_tests"]
    assert [item["name"] for item in payload["dead_anywhere"]] == ["never_called"]
    assert str(tmp_path) not in json.dumps(payload)


def test_the_report_quotes_no_absolute_path(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A note from the linker tier is rewritten relative, not copied verbatim.

    The tier says which build tree it could not read, and that path is absolute
    because the tier was handed one; a report that kept it would differ between two
    checkouts of the same tree.
    """
    monkeypatch.chdir(tmp_path)
    install(
        monkeypatch,
        tmp_path,
        parsed(tmp_path),
        cross_check=linker.run_cross_check,
    )

    code = cli.main(
        ["scan", "--prod", "src", "--tests", "tests", "--no-cache", "--out", "reports"],
    )

    assert code == cli.ExitCode.CLEAN
    text = (tmp_path / "reports" / "report.json").read_text(encoding="utf-8")
    payload = json.loads(text)
    assert any("no linker evidence" in note for note in payload["notes"])
    assert str(tmp_path) not in text


def test_a_build_tree_outside_the_scan_root_is_relative(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A build tree beside the checkout is quoted relative, never absolute.

    The stripping pass this replaced only knew the scan-root prefix, so a path that
    did not start with it survived into ``report.json`` exactly as the tier spelt it.
    """
    root = tmp_path / "tree"
    elsewhere = tmp_path / "elsewhere" / "build"
    root.mkdir()
    monkeypatch.chdir(root)
    install(
        monkeypatch,
        root,
        parsed(root),
        cross_check=linker.run_cross_check,
    )

    code = cli.main(
        [
            "scan",
            "--prod",
            "src",
            "--tests",
            "tests",
            "--no-cache",
            "--prod-build-dir",
            str(elsewhere),
            "--out",
            "reports",
        ],
    )

    assert code == cli.ExitCode.CLEAN
    text = (root / "reports" / "report.json").read_text(encoding="utf-8")
    payload = json.loads(text)
    assert any("../elsewhere/build" in note for note in payload["notes"])
    assert str(tmp_path) not in text


def test_a_sibling_sharing_the_scan_root_prefix_survives(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Stripping the scan-root string mangled a ``/a/bc`` sibling into ``.c``."""
    root = tmp_path / "a" / "b"
    sibling = tmp_path / "a" / "bc" / "build"
    root.mkdir(parents=True)
    monkeypatch.chdir(root)
    install(
        monkeypatch,
        root,
        parsed(root),
        cross_check=linker.run_cross_check,
    )

    code = cli.main(
        [
            "scan",
            "--prod",
            "src",
            "--tests",
            "tests",
            "--no-cache",
            "--prod-build-dir",
            str(sibling),
            "--out",
            "reports",
        ],
    )

    assert code == cli.ExitCode.CLEAN
    text = (root / "reports" / "report.json").read_text(encoding="utf-8")
    payload = json.loads(text)
    assert any("../bc/build" in note for note in payload["notes"])
    assert ".c/build" not in text
    assert str(tmp_path) not in text


def test_format_limits_the_reports_written(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """``--format json`` is a CI job that wants only the machine-readable file."""
    monkeypatch.chdir(tmp_path)
    install(monkeypatch, tmp_path, parsed(tmp_path))

    code = cli.main(
        [
            "scan",
            "--prod",
            "src",
            "--tests",
            "tests",
            "--no-cache",
            "--out",
            "reports",
            "--format",
            "json",
        ],
    )

    assert code == cli.ExitCode.CLEAN
    assert [path.name for path in (tmp_path / "reports").iterdir()] == ["report.json"]


def test_an_invalid_flag_value_is_a_usage_error(
    capsys: pytest.CaptureFixture[str],
) -> None:
    """A refused flag value exits 1 with one line, not argparse's ``SystemExit(2)``.

    Exit 2 already means "the graph is incomplete", so argparse's own code would be
    indistinguishable from a failed parse; ``_Parser`` turns both of these into the
    usage error a mistyped flag produces.
    """
    format_code = cli.main(["scan", "--format", "pdf"])
    gate_code = cli.main(["scan", "--fail-on", "bogus"])

    assert format_code == cli.ExitCode.USAGE
    assert gate_code == cli.ExitCode.USAGE
    err = capsys.readouterr().err
    assert "--format" in err
    assert "--fail-on" in err
    assert "Traceback" not in err


def test_a_bare_invocation_is_a_usage_error(
    capsys: pytest.CaptureFixture[str],
) -> None:
    """``deadweight`` with no subcommand names the missing command and exits 1."""
    code = cli.main([])

    assert code == cli.ExitCode.USAGE
    captured = capsys.readouterr()
    assert "command" in captured.err
    assert "Traceback" not in captured.err


def test_a_production_root_is_required(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    """A scan that names no production root would answer about nothing."""
    monkeypatch.chdir(tmp_path)

    code = cli.main(["scan", "--tests", "tests"])

    assert code == cli.ExitCode.USAGE
    assert "--prod" in capsys.readouterr().err


def test_jobs_below_one_is_a_usage_error(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    """A run with no worker cannot answer, so the flag is refused, not crashed on."""
    monkeypatch.chdir(tmp_path)
    install(monkeypatch, tmp_path, parsed(tmp_path))

    code = cli.main(["scan", "--prod", "src", "--tests", "tests", "--jobs", "0"])

    assert code == cli.ExitCode.USAGE
    assert "--jobs" in capsys.readouterr().err


def test_a_required_libclang_version_is_a_usage_error(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    """``--require-libclang`` turns a version mismatch into a refusal."""
    monkeypatch.chdir(tmp_path)
    install(monkeypatch, tmp_path, parsed(tmp_path))

    def refuse(path: str, *, required_major: int | None = None) -> object:
        """A discovery that finds only the wrong major version."""
        message = f"{path} is not libclang {required_major}"
        raise discover.LibClangError(message)

    monkeypatch.setattr(discover, "load", refuse)

    code = cli.main(
        ["scan", "--prod", "src", "--tests", "tests", "--require-libclang", "16"],
    )

    assert code == cli.ExitCode.USAGE
    assert "16" in capsys.readouterr().err


def test_doctor_reports_the_library_it_found(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    """``doctor`` names the library and exits 0 when the environment is usable."""
    monkeypatch.chdir(tmp_path)
    library = cindex.LibClang(
        path="/opt/llvm/lib/libclang.so",
        version="22.1.8",
        banner="clang version 22.1.8",
        api={"clang_createIndex": True},
    )
    monkeypatch.setattr(discover, "load", lambda *args, **kwargs: library)
    monkeypatch.setattr(discover, "candidates", lambda *args, **kwargs: [])

    code = cli.main(["doctor"])

    assert code == cli.ExitCode.CLEAN
    printed = capsys.readouterr().out
    assert "# deadweight doctor" in printed
    assert library.path in printed


def test_doctor_prints_the_rejected_candidate_trail(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    """Every path the search considered is printed with why it lost.

    ``docs/compatibility.md`` promises the trail, and the report is what a user
    pastes into an issue, so each rejection has to survive with its reason - but
    only the chosen library may keep an absolute path.
    """
    monkeypatch.chdir(tmp_path)
    library = cindex.LibClang(
        path="/opt/llvm/lib/libclang.so",
        version="22.1.8",
        banner="clang version 22.1.8",
        api={"clang_createIndex": True},
    )
    rejected = discover.Candidate(
        path="/opt/llvm-15/lib/libclang.so",
        origin="system-path",
        reason="libclang 15 is older than the supported floor",
    )
    monkeypatch.setattr(discover, "load", lambda *args, **kwargs: library)
    monkeypatch.setattr(
        discover,
        "candidates",
        lambda *args, **kwargs: [
            discover.Candidate(path=library.path, origin="system-path", lib=library),
            rejected,
        ],
    )

    code = cli.main(["doctor"])

    assert code == cli.ExitCode.CLEAN
    printed = capsys.readouterr().out
    assert "## search trail" in printed
    assert "`llvm-15/lib/libclang.so` (system-path)" in printed
    assert rejected.reason in printed
    assert "/opt/llvm-15" not in printed


def test_doctor_searches_the_host_once(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    """The trail and the winner come from one search, so no path is probed twice."""
    monkeypatch.chdir(tmp_path)
    library = cindex.LibClang(
        path="/opt/llvm/lib/libclang.so",
        version="22.1.8",
        banner="clang version 22.1.8",
        api={"clang_createIndex": True},
    )
    searches: list[int] = []

    def search(
        *_args: object,
        **_kwargs: object,
    ) -> list[discover.Candidate]:
        """Count the searches the doctor runs, and answer with one library."""
        searches.append(len(searches))
        return [
            discover.Candidate(path=library.path, origin="system-path", lib=library),
        ]

    monkeypatch.setattr(discover, "candidates", search)

    code = cli.main(["doctor"])

    assert code == cli.ExitCode.CLEAN
    assert library.path in capsys.readouterr().out
    assert len(searches) == 1


def test_doctor_with_a_missing_compile_database_exits_1(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    """``doctor --compile-commands`` checks the file it was pointed at."""
    monkeypatch.chdir(tmp_path)
    library = cindex.LibClang(
        path="/opt/llvm/lib/libclang.so",
        version="22.1.8",
        banner="clang version 22.1.8",
        api={"clang_createIndex": True},
    )
    monkeypatch.setattr(discover, "load", lambda *args, **kwargs: library)
    monkeypatch.setattr(discover, "candidates", lambda *args, **kwargs: [])

    code = cli.main(["doctor", "--compile-commands", "absent.json"])

    assert code == cli.ExitCode.USAGE
    assert "absent.json" in capsys.readouterr().err


_SETTING_CASES: list[tuple[str, str | None, str, object]] = [
    ("--exclude", "third_party/**", "exclude", ("third_party/**",)),
    ("--no-cascade", None, "cascade", False),
    ("--prod-build-dir", "build-gc", "prod_build_dir", Path("build-gc")),
    ("--prod-binary", "build-gc/app", "prod_binary", Path("build-gc/app")),
    (
        "--test-binaries-dir",
        "build-gc-tests",
        "test_binaries_dir",
        Path("build-gc-tests"),
    ),
    ("--gc-report", "gc-prod.txt", "gc_reports", (Path("gc-prod.txt"),)),
    ("--jobs", "3", "jobs", 3),
    ("--out", "runs", "out", Path("runs")),
    ("--format", "json", "formats", ("json",)),
]
"""One flag per case, with the ``_Settings`` field it has to reach and its value."""


def _resolve_expected(root: Path, expected: object) -> object:
    """Resolve the path-shaped expectations against the scan root, as the run does."""
    if isinstance(expected, Path):
        return root / expected
    if isinstance(expected, tuple) and all(isinstance(item, Path) for item in expected):
        return tuple(root / item for item in expected)
    return expected


@pytest.mark.parametrize(("flag", "value", "attribute", "expected"), _SETTING_CASES)
def test_each_flag_reaches_the_settings(  # noqa: PLR0913, PLR0917 -- parametrized case plus two fixtures
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    flag: str,
    value: str | None,
    attribute: str,
    expected: object,
) -> None:
    """The parsed flag lands on the ``_Settings`` field the scan reads.

    The pipeline is exercised through ``main`` everywhere else, which cannot tell a
    flag that stops being wired from a flag the pipeline ignores. This reads the
    merged settings directly, so renaming or dropping one of these flags fails here.
    """
    monkeypatch.chdir(tmp_path)
    (tmp_path / "compile_commands.json").write_text("[]\n", encoding="utf-8")
    argv = ["scan", "--prod", "src", flag]
    if value is not None:
        argv.append(value)

    settings = cli._settings(cli.parse_args(argv))  # noqa: SLF001 -- the merged settings are the seam under test

    assert getattr(settings, attribute) == _resolve_expected(tmp_path, expected)


def test_python_m_deadweight_runs_the_scan_path(tmp_path: Path) -> None:
    """The module entry point reaches ``scan`` and answers the documented code.

    A tree with no compilation database is refused before any parsing, which is the
    one run of the real pipeline that needs neither libclang nor a linker.
    """
    completed = subprocess.run(
        [sys.executable, "-m", "deadweight", "scan", "--prod", "src"],
        cwd=tmp_path,
        capture_output=True,
        text=True,
        check=False,
    )

    assert completed.returncode == int(cli.ExitCode.USAGE)
    assert "compile_commands" in completed.stderr
    assert "Traceback" not in completed.stderr
