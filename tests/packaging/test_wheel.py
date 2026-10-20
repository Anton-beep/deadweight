"""The wheel is the release lane: what ships, and what the metadata promises.

The spec makes the built distribution a precondition of the CLI contract, so these
tests inspect the real archive rather than the source tree: a file that only the
repository needs must not ship, the console script must exist, and the two metadata
fields a resolver and a licence scanner read must be present.
"""

from __future__ import annotations

import shutil
import subprocess
import zipfile
from pathlib import Path

import pytest

_ROOT = Path(__file__).resolve().parents[2]
"""The repository root, where ``uv build`` runs."""

pytestmark = pytest.mark.slow


@pytest.fixture(scope="module")
def archive(tmp_path_factory: pytest.TempPathFactory) -> zipfile.ZipFile:
    """Build the wheel once and hand every test the same archive."""
    uv = shutil.which("uv")
    if uv is None:
        pytest.skip("uv is not installed, so the wheel cannot be built")
    out = tmp_path_factory.mktemp("wheel")
    subprocess.run(  # noqa: S603 - the arguments are literals, not user input
        [uv, "build", "--wheel", "--out-dir", str(out)],
        cwd=_ROOT,
        check=True,
        capture_output=True,
        text=True,
    )
    wheels = list(out.glob("*.whl"))
    assert len(wheels) == 1, f"expected one wheel, built {wheels}"
    return zipfile.ZipFile(wheels[0])


def _member(archive: zipfile.ZipFile, suffix: str) -> str:
    """Read the one archive member whose name ends with ``suffix``."""
    matches = [name for name in archive.namelist() if name.endswith(suffix)]
    assert len(matches) == 1, f"expected one {suffix}, found {matches}"
    return archive.read(matches[0]).decode("utf-8")


def test_the_wheel_ships_no_repository_only_files(archive: zipfile.ZipFile) -> None:
    """The archive holds the package and its metadata, not the test or CI tree."""
    names = archive.namelist()

    assert not [name for name in names if name.startswith(("tests/", "tools/"))]
    assert not [name for name in names if name.startswith(".github/")]
    assert any(name.startswith("deadweight/") for name in names)


def test_the_wheel_declares_the_console_script(archive: zipfile.ZipFile) -> None:
    """``deadweight`` on ``PATH`` is the entry point the README documents."""
    entry_points = _member(archive, "entry_points.txt")

    assert "[console_scripts]" in entry_points
    assert "deadweight = deadweight.cli:main" in entry_points


def test_the_wheel_metadata_carries_the_licence_and_python_floor(
    archive: zipfile.ZipFile,
) -> None:
    """A resolver and a licence scanner read these two fields, not the README."""
    metadata = _member(archive, "METADATA")

    assert "License-Expression: Apache-2.0" in metadata
    assert "Requires-Python: >=3.11" in metadata
