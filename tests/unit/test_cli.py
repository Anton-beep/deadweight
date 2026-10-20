"""The command line: the merged configuration, the pipeline, and the exit contract.

Every test here drives ``main`` rather than the pieces underneath it, because the
contract being pinned is the one an operator sees: which flags override the file,
which failures write a report, and which integer comes back out. The compiler and the
linker are replaced by records, so the suite needs no libclang and no linked program;
the real pipeline is exercised by ``tests/integration/test_end_to_end.py``.
"""

from __future__ import annotations

import json
from typing import TYPE_CHECKING

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
    from pathlib import Path

    import pytest

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


def test_a_namespaced_ignore_symbol_suppresses_the_candidate(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The allowlist may name a symbol the way the manual spells it, qualified."""
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
    assert payload["candidates"] == []


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

    code = cli.main(["doctor"])

    assert code == cli.ExitCode.CLEAN
    printed = capsys.readouterr().out
    assert "# deadweight doctor" in printed
    assert library.path in printed


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

    code = cli.main(["doctor", "--compile-commands", "absent.json"])

    assert code == cli.ExitCode.USAGE
    assert "absent.json" in capsys.readouterr().err
