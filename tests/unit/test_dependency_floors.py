"""Tests for scripts/check_dependency_floors.py.

The guard runs in CI (`make floors-check`) and, with `--fix`, twice in the
release workflow's sync job: fix, `uv lock`, check. Those two commands run
back to back, so anything that makes `--fix` disagree with the following
check wedges the release PR -- the exact wall the guard exists to remove.
The tests below drive the real script over throwaway workspaces.
"""

from __future__ import annotations

import ast
import json
import re
import shutil
import subprocess
import sys
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parents[2]
SCRIPT = REPO / "scripts" / "check_dependency_floors.py"


def make_workspace(
    tmp_path: Path,
    root_deps: list[str],
    packages: dict[str, str],
) -> Path:
    """Build a minimal workspace the guard can be pointed at.

    `packages` maps package name -> version; each becomes
    packages/<name>/pyproject.toml and a manifest entry.
    """
    (tmp_path / "scripts").mkdir()
    shutil.copy(SCRIPT, tmp_path / "scripts" / SCRIPT.name)

    deps = ", ".join(f'"{dep}"' for dep in root_deps)
    (tmp_path / "pyproject.toml").write_text(
        "[project]\n"
        'name = "deepctl"\n'
        'version = "1.0.0"\n'
        f"dependencies = [{deps}]\n",
        encoding="utf-8",
    )

    manifest: dict[str, str] = {".": "1.0.0"}
    for name, version in packages.items():
        pkg_dir = tmp_path / "packages" / name
        pkg_dir.mkdir(parents=True)
        (pkg_dir / "pyproject.toml").write_text(
            f'[project]\nname = "{name}"\nversion = "{version}"\n',
            encoding="utf-8",
        )
        manifest[f"packages/{name}"] = version

    github = tmp_path / ".github"
    github.mkdir()
    (github / ".release-please-manifest.json").write_text(
        json.dumps(manifest), encoding="utf-8"
    )
    return tmp_path


def run_guard(workspace: Path, *args: str) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        [sys.executable, str(workspace / "scripts" / SCRIPT.name), *args],
        capture_output=True,
        text=True,
    )


def root_dependencies(workspace: Path) -> list[str]:
    """Read back the dependency list this module wrote, after --fix.

    Parsed with ast rather than tomllib so the test itself runs on 3.10,
    where the guard uses its tomli fallback.
    """
    text = (workspace / "pyproject.toml").read_text(encoding="utf-8")
    match = re.search(r"^dependencies = (\[.*\])$", text, re.MULTILINE)
    assert match, text
    deps: list[str] = ast.literal_eval(match.group(1))
    return deps


@pytest.mark.parametrize(
    "version",
    ["0.4.0rc1", "1.0.0.dev1", "0.3.0.post1", "0.4.0b2"],
)
def test_fix_then_check_round_trips_on_pre_release_versions(
    tmp_path: Path, version: str
) -> None:
    """`--fix` must produce a tree the very next check accepts.

    The version regex used to stop at the first non-digit, so a workspace
    version of 0.4.0rc1 was read as a floor of "0.4.0": --fix wrote
    `deepctl-core>=0.4.0rc1` and reported success, and the check that runs
    immediately after it in the release sync job exited 1 on the floor it
    had just written. `make floors-check` could then never be made to pass.
    """
    workspace = make_workspace(
        tmp_path,
        root_deps=["deepctl-core>=0.1.0"],
        packages={"deepctl-core": version},
    )

    fix = run_guard(workspace, "--fix")
    assert fix.returncode == 0, fix.stderr
    assert root_dependencies(workspace) == [f"deepctl-core>={version}"]

    check = run_guard(workspace)
    assert check.returncode == 0, check.stderr
    assert "dependency floors OK" in check.stdout


def test_fix_is_idempotent_when_already_pinned(tmp_path: Path) -> None:
    """A second --fix on an already-correct tree changes nothing."""
    workspace = make_workspace(
        tmp_path,
        root_deps=["deepctl-core>=0.4.0rc1"],
        packages={"deepctl-core": "0.4.0rc1"},
    )
    before = (workspace / "pyproject.toml").read_text(encoding="utf-8")

    assert run_guard(workspace, "--fix").returncode == 0
    assert (workspace / "pyproject.toml").read_text(encoding="utf-8") == before


def test_fix_preserves_upper_bounds_and_extras(tmp_path: Path) -> None:
    """Only the floor is rewritten; the rest of the spec survives."""
    workspace = make_workspace(
        tmp_path,
        root_deps=["deepctl-core[speech]>=0.1.0,<2"],
        packages={"deepctl-core": "0.4.0rc1"},
    )

    assert run_guard(workspace, "--fix").returncode == 0
    assert root_dependencies(workspace) == ["deepctl-core[speech]>=0.4.0rc1,<2"]
    assert run_guard(workspace).returncode == 0


def test_extras_do_not_look_like_a_missing_root_dependency(tmp_path: Path) -> None:
    """A correctly pinned dep with extras is not reported as missing.

    Rule 3's "published but is not a root dependency" message is derived
    from what rule 1's parser saw. When that parser only matched bare
    `name>=version`, writing an extras group made the guard tell the
    maintainer to add a dependency that was already right there.
    """
    workspace = make_workspace(
        tmp_path,
        root_deps=["deepctl-core[speech]>=0.4.0"],
        packages={"deepctl-core": "0.4.0"},
    )

    result = run_guard(workspace)
    assert result.returncode == 0, result.stderr
    assert "is not a root dependency" not in result.stderr


def test_non_floor_specifier_is_named_for_what_it_is(tmp_path: Path) -> None:
    """`==` is reported as an unsupported specifier, not as an omission."""
    workspace = make_workspace(
        tmp_path,
        root_deps=["deepctl-core==0.4.0"],
        packages={"deepctl-core": "0.4.0"},
    )

    result = run_guard(workspace)
    assert result.returncode == 1
    assert "does not pin a `>=` floor" in result.stderr
    assert "is not a root dependency" not in result.stderr


def test_stale_root_floor_still_fails(tmp_path: Path) -> None:
    """Positive control: rule 1 keeps catching what it was written for."""
    workspace = make_workspace(
        tmp_path,
        root_deps=["deepctl-core>=0.3.0"],
        packages={"deepctl-core": "0.4.0"},
    )

    result = run_guard(workspace)
    assert result.returncode == 1
    assert "root floor deepctl-core>=0.3.0 != workspace version 0.4.0" in result.stderr


def test_missing_root_dependency_still_fails(tmp_path: Path) -> None:
    """Positive control: rule 3 keeps catching an unlisted package."""
    workspace = make_workspace(
        tmp_path,
        root_deps=["deepctl-core>=0.4.0"],
        packages={"deepctl-core": "0.4.0", "deepctl-cmd-keys": "0.1.0"},
    )

    result = run_guard(workspace)
    assert result.returncode == 1
    assert "deepctl-cmd-keys is published but is not a root dependency" in result.stderr


def test_unsatisfiable_sub_package_floor_still_fails(tmp_path: Path) -> None:
    """Positive control: rule 2 keeps catching a floor above the sibling."""
    workspace = make_workspace(
        tmp_path,
        root_deps=["deepctl-core>=0.4.0", "deepctl-cmd-keys>=0.1.0"],
        packages={"deepctl-core": "0.4.0", "deepctl-cmd-keys": "0.1.0"},
    )
    keys = workspace / "packages" / "deepctl-cmd-keys" / "pyproject.toml"
    keys.write_text(
        '[project]\nname = "deepctl-cmd-keys"\nversion = "0.1.0"\n'
        'dependencies = ["deepctl-core>=9.9.9"]\n',
        encoding="utf-8",
    )

    result = run_guard(workspace)
    assert result.returncode == 1
    assert "exceeds workspace version 0.4.0 (unsatisfiable)" in result.stderr


def test_final_release_floor_above_a_pre_release_is_unsatisfiable(
    tmp_path: Path,
) -> None:
    """0.4.0 is not satisfied by 0.4.0rc1, and the guard must say so.

    Comparing only the numeric segments made these two look identical.
    """
    workspace = make_workspace(
        tmp_path,
        root_deps=["deepctl-core>=0.4.0rc1", "deepctl-cmd-keys>=0.1.0"],
        packages={"deepctl-core": "0.4.0rc1", "deepctl-cmd-keys": "0.1.0"},
    )
    keys = workspace / "packages" / "deepctl-cmd-keys" / "pyproject.toml"
    keys.write_text(
        '[project]\nname = "deepctl-cmd-keys"\nversion = "0.1.0"\n'
        'dependencies = ["deepctl-core>=0.4.0"]\n',
        encoding="utf-8",
    )

    result = run_guard(workspace)
    assert result.returncode == 1
    assert "exceeds workspace version 0.4.0rc1 (unsatisfiable)" in result.stderr
