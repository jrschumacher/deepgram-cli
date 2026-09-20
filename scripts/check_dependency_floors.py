#!/usr/bin/env python3
"""Check (or fix) intra-workspace dependency floors.

Three rules, learned from the 0.3.0 release (PRs #100/#102):

1. **Root floors equal workspace versions.** `dg update` runs
   `pip install --upgrade deepctl`, and pip's default `only-if-needed`
   strategy upgrades a sub-package only when the root floor forces it. Any
   root floor below the current version means that package's fixes are
   published but never delivered on upgrade — `dg --version` reports the new
   release while 13 of 17 packages stay stale, which is what happened before
   0.3.0. Root's dependency list is the delivery manifest, so each
   `deepctl-*` floor must equal that package's current workspace version.

2. **Sub-package floors stay satisfiable.** Sub-package floors are API
   contracts (e.g. deepctl-cmd-keys needs the deepctl-core that provides
   `get_status_console`), hand-raised when a package starts using a newer
   sibling API. They must never exceed the sibling's current version.
   Keeping them *accurate* is still on the developer: raise the floor in the
   same PR that starts importing the new API.

3. **Root's dependency list covers every published package.** Rule 1 only
   validates the floors that are already listed. A package that release-please
   versions and publishes but that nobody added to root's `dependencies` is
   never installed by `pip install --upgrade deepctl` at all — the same
   delivery gap as rule 1, through the door rule 1 leaves open. Anything
   deliberately not shipped as part of the CLI goes in NOT_SHIPPED, so that
   intent is stated in the diff rather than inferred from an omission.

Versions are compared with `packaging.version.Version`, not as strings, so a
pre-release anywhere in the workspace (0.4.0rc1, 1.0.0.dev1, 0.3.0.post1)
behaves like any other version instead of turning `--fix` into a loop the
following check can never satisfy.

Run with --fix to rewrite root floors in place (used by the release
workflow's sync job so rule 1 holds automatically on every release PR).
"""

from __future__ import annotations

import argparse
import json
import re
import sys
from dataclasses import dataclass
from pathlib import Path

if sys.version_info >= (3, 11):
    import tomllib
else:  # pragma: no cover - only taken on Python 3.10
    try:
        import tomli as tomllib
    except ModuleNotFoundError:
        print(
            "Python 3.11+ required (tomllib), or install tomli: pip install tomli",
            file=sys.stderr,
        )
        sys.exit(1)

from packaging.version import InvalidVersion, Version

REPO = Path(__file__).resolve().parent.parent
MANIFEST = REPO / ".github" / ".release-please-manifest.json"

# Published packages that are deliberately not part of what `dg` installs.
# Everything else in the manifest must be a root dependency (rule 3).
NOT_SHIPPED = {
    "deepctl",  # the root package itself
    "deepctl-plugin-example",  # sample plugin, installed on demand
}

# A PEP 508 requirement, narrowed to intra-workspace packages: the name, an
# optional extras group, then whatever the author wrote after it. Matching
# only bare `name>=version` used to make `deepctl-cmd-keys[extra]>=0.0.1` or
# `deepctl-cmd-keys==0.1.0` invisible to rules 1 and 3, so the guard reported
# a listed package as missing from root entirely.
_DEP_RE = re.compile(
    r"^\s*(?P<name>deepctl[A-Za-z0-9._-]*)"
    r"\s*(?:\[[^\]]*\])?"
    r"\s*(?P<rest>.*)$"
)
# The floor itself. The version runs to the first delimiter, so an upper
# bound (`,<2`), an environment marker (`; python_version < "3.12"`) and a
# PEP 440 suffix (`0.4.0rc1`) all survive intact.
_FLOOR_RE = re.compile(r"^(?P<spec>>=\s*(?P<version>[^\s,;]+))")


@dataclass(frozen=True)
class Dep:
    """One intra-workspace dependency as root or a package declares it."""

    name: str
    #: The pinned floor, or None when the spec is not a plain `>=`.
    floor: str | None
    #: Exact `>=…` text matched, so --fix can rewrite it in place.
    spec: str
    #: The whole dependency string, as written.
    raw: str


def workspace_versions() -> dict[str, str]:
    """Map package name -> current workspace version, per the manifest."""
    versions: dict[str, str] = {}
    for path in json.loads(MANIFEST.read_text(encoding="utf-8")):
        pyproject = REPO / (
            "pyproject.toml" if path == "." else f"{path}/pyproject.toml"
        )
        project = tomllib.loads(pyproject.read_text(encoding="utf-8"))["project"]
        versions[project["name"]] = project["version"]
    return versions


def floors(pyproject: Path) -> list[Dep]:
    """Return every intra-workspace dependency declared by `pyproject`."""
    deps = tomllib.loads(pyproject.read_text(encoding="utf-8"))["project"].get(
        "dependencies", []
    )
    out: list[Dep] = []
    for dep in deps:
        m = _DEP_RE.match(dep)
        if not m:
            continue
        floor = _FLOOR_RE.match(m.group("rest").strip())
        out.append(
            Dep(
                name=m.group("name"),
                floor=floor.group("version") if floor else None,
                spec=floor.group("spec") if floor else "",
                raw=dep,
            )
        )
    return out


def parse(version: str) -> Version | None:
    """PEP 440 version, or None when the string is not a valid version.

    Returning None rather than raising keeps one hand-typed oddity from
    turning `make floors-check` into a bare traceback naming no package; the
    caller reports it as an ordinary problem instead.
    """
    try:
        return Version(version)
    except InvalidVersion:
        return None


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--fix",
        action="store_true",
        help="rewrite root pyproject.toml floors to the workspace versions",
    )
    args = parser.parse_args()

    versions = workspace_versions()
    problems: list[str] = []

    # Rule 1: root floors == workspace versions.
    root = REPO / "pyproject.toml"
    root_text = root.read_text(encoding="utf-8")
    fixed = root_text
    root_floors = floors(root)
    for dep in root_floors:
        current = versions.get(dep.name)
        if current is None:
            problems.append(f"root depends on {dep.name}, which is not in the manifest")
            continue
        if dep.floor is None:
            problems.append(
                f"root dependency {dep.raw!r} does not pin a `>=` floor"
                f" (root is the delivery manifest: write"
                f" {dep.name}>={current} so pip upgrades deliver it)"
            )
            continue
        floor_version, current_version = parse(dep.floor), parse(current)
        if floor_version is None:
            problems.append(
                f"root floor {dep.name}>={dep.floor} is not a valid PEP 440 version"
            )
            continue
        if current_version is None:
            problems.append(
                f"{dep.name} workspace version {current} is not a valid PEP 440 version"
            )
            continue
        if floor_version == current_version:
            continue
        if args.fix:
            # Rewrite the version inside the spec we matched, so any upper
            # bound, extra, or environment marker survives. A whole-spec
            # literal replace silently no-ops on those, and an unfixable
            # floor has to fall through to `problems` -- reporting "OK"
            # after failing to fix is worse than not fixing.
            new_raw = dep.raw.replace(dep.spec, f">={current}", 1)
            if new_raw != dep.raw and f'"{dep.raw}"' in fixed:
                fixed = fixed.replace(f'"{dep.raw}"', f'"{new_raw}"', 1)
                continue
        problems.append(
            f"root floor {dep.name}>={dep.floor} != workspace version {current}"
            " (published fixes will not be delivered by pip upgrades)"
        )
    if args.fix and fixed != root_text:
        root.write_text(fixed, encoding="utf-8")
        print(f"fixed: root floors pinned to workspace versions in {root}")

    # Rule 2: every sub-package floor must be satisfiable at co-release.
    for pkg_dir in sorted((REPO / "packages").iterdir()):
        pyproject = pkg_dir / "pyproject.toml"
        if not pyproject.is_file():
            continue
        for dep in floors(pyproject):
            current = versions.get(dep.name)
            if current is None:
                problems.append(
                    f"{pkg_dir.name} depends on {dep.name},"
                    " which is not in the manifest"
                )
                continue
            if dep.floor is None:
                continue
            floor_version, current_version = parse(dep.floor), parse(current)
            if floor_version is None or current_version is None:
                problems.append(
                    f"{pkg_dir.name}: floor {dep.name}>={dep.floor} or workspace"
                    f" version {current} is not a valid PEP 440 version"
                )
            elif floor_version > current_version:
                problems.append(
                    f"{pkg_dir.name}: floor {dep.name}>={dep.floor} exceeds"
                    f" workspace version {current} (unsatisfiable)"
                )

    # Rule 3: every published package is a root dependency.
    listed = {dep.name for dep in root_floors}
    for name in sorted(set(versions) - listed - NOT_SHIPPED):
        problems.append(
            f"{name} is published but is not a root dependency"
            " (pip upgrades will never install it; add it to root"
            " pyproject.toml or to NOT_SHIPPED in this script)"
        )

    if problems:
        print("dependency floor check FAILED:", file=sys.stderr)
        for problem in problems:
            print(f"  - {problem}", file=sys.stderr)
        print(
            "\nRun `make floors-fix` to pin stale root floors. Sub-package"
            " floors and missing root dependencies are hand-maintained.",
            file=sys.stderr,
        )
        return 1

    print("dependency floors OK")
    return 0


if __name__ == "__main__":
    sys.exit(main())
