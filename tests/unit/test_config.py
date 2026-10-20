"""The configuration file: discovery, the schema, and the refusals.

A typo in ``.deadweight.toml`` that silently disables a root turns a clean run into a
false clean run, so every unknown key is a refusal rather than a warning, and every
``[[ignore]]`` entry has to say why it exists. Paths are kept exactly as the file
spells them; resolving them against the scan root is the caller's job, so this layer
stays a pure reading of the file and can be tested without a tree.
"""

from __future__ import annotations

from datetime import date
from pathlib import Path

import pytest

from deadweight import config


def write_config(directory: Path, text: str) -> Path:
    """Write a configuration file where a scan run would look for one."""
    path = directory / config.CONFIG_NAME
    path.write_text(text, encoding="utf-8")
    return path


def test_config_discovery_walks_upward(tmp_path: Path) -> None:
    """A scan run in a subdirectory finds the repository's file above it."""
    root = tmp_path / "repo"
    root.mkdir()
    write_config(root, '[scan]\nprod_roots = ["client"]\ntest_roots = ["tests"]\n')
    nested = root / "client" / "core"
    nested.mkdir(parents=True)

    loaded = config.load_config(nested, None)

    assert loaded.prod_roots == ("client",)
    assert loaded.test_roots == ("tests",)
    assert config.find_config(nested, None) == root / config.CONFIG_NAME


def test_a_tree_without_a_file_loads_the_defaults(tmp_path: Path) -> None:
    """No configuration file is not an error; the flags have to supply the roots."""
    loaded = config.load_config(tmp_path, None)

    assert loaded.prod_roots == ()
    assert loaded.test_roots == ()
    assert loaded.fail_on == "none"
    assert loaded.format == config.FORMATS
    assert loaded.compile_commands is None


def test_an_explicit_config_must_exist(tmp_path: Path) -> None:
    """``--config`` names a file, so a missing one is a usage error, not a fallback."""
    with pytest.raises(config.ConfigError, match="--config"):
        config.load_config(tmp_path, tmp_path / "absent.toml")


def test_unknown_key_is_refused(tmp_path: Path) -> None:
    """``prod_root`` for ``prod_roots`` must not silently scan nothing."""
    write_config(tmp_path, '[scan]\nprod_root = ["client"]\n')

    with pytest.raises(config.ConfigError, match="prod_root"):
        config.load_config(tmp_path, None)


def test_an_unknown_table_is_refused(tmp_path: Path) -> None:
    """A misspelled table would otherwise be ignored as thoroughly as a key."""
    write_config(tmp_path, '[linkers]\nprod_binary = "build/app"\n')

    with pytest.raises(config.ConfigError, match="linkers"):
        config.load_config(tmp_path, None)


def test_ignore_requires_a_reason(tmp_path: Path) -> None:
    """An allowlist entry without a reason is how an allowlist rots."""
    write_config(tmp_path, '[[ignore]]\nsymbol = "topi::LegacyShim"\n')

    with pytest.raises(config.ConfigError, match="reason"):
        config.load_config(tmp_path, None)


def test_a_blank_reason_is_no_reason(tmp_path: Path) -> None:
    """Whitespace is not an explanation either."""
    write_config(
        tmp_path,
        '[[ignore]]\nsymbol = "topi::LegacyShim"\nreason = "  "\n',
    )

    with pytest.raises(config.ConfigError, match="reason"):
        config.load_config(tmp_path, None)


def test_an_ignore_entry_is_typed(tmp_path: Path) -> None:
    """The symbol, the reason and the expiry are kept as the file spells them."""
    write_config(
        tmp_path,
        '[[ignore]]\nsymbol = "topi::LegacyShim"\n'
        'reason = "kept for the termux build"\nuntil = "2027-01-01"\n',
    )

    loaded = config.load_config(tmp_path, None)

    assert loaded.ignores == (
        config.Ignore(
            symbol="topi::LegacyShim",
            reason="kept for the termux build",
            until=date(2027, 1, 1),
        ),
    )


@pytest.mark.parametrize("value", ["sometimes", "Candidates", ""])
def test_fail_on_is_validated(tmp_path: Path, value: str) -> None:
    """A gate that cannot trip because its value is unknown is worse than no gate."""
    write_config(tmp_path, f'[gate]\nfail_on = "{value}"\n')

    with pytest.raises(config.ConfigError, match="fail_on"):
        config.load_config(tmp_path, None)


def test_format_is_validated(tmp_path: Path) -> None:
    """``--format`` names renderers that exist, so the file must too."""
    write_config(tmp_path, '[scan]\nformat = ["md", "pdf"]\n')

    with pytest.raises(config.ConfigError, match="pdf"):
        config.load_config(tmp_path, None)


def test_a_value_of_the_wrong_type_is_refused(tmp_path: Path) -> None:
    """A bare string where a list belongs is a typo, not a one-item list."""
    write_config(tmp_path, '[scan]\nprod_roots = "client"\n')

    with pytest.raises(config.ConfigError, match="prod_roots"):
        config.load_config(tmp_path, None)


def test_a_malformed_file_is_a_config_error(tmp_path: Path) -> None:
    """Invalid TOML is a usage error with the file named, not a traceback."""
    write_config(tmp_path, "[scan\nprod_roots = [")

    with pytest.raises(config.ConfigError, match=config.CONFIG_NAME):
        config.load_config(tmp_path, None)


def test_paths_are_kept_as_written(tmp_path: Path) -> None:
    """The file's relative paths are the caller's to resolve against the scan root."""
    write_config(
        tmp_path,
        '[scan]\ncompile_commands = "build/compile_commands.json"\nout = "runs/x"\n'
        '[linker]\nprod_binary = "build/app"\ngc_reports = ["build/gc.txt"]\n',
    )

    loaded = config.load_config(tmp_path, None)

    assert loaded.compile_commands == Path("build/compile_commands.json")
    assert loaded.out == Path("runs/x")
    assert loaded.prod_binary == Path("build/app")
    assert loaded.gc_reports == (Path("build/gc.txt"),)
