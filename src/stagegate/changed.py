"""``stagegate changed-units``: which units of one environment a pull request changes.

Prints a comma-separated list of unit names (a unit's name is its path below
``<units-root>/<env>`` with ``/`` written ``__``), an empty line when nothing changed, or
``ALL`` when the answer cannot be narrowed safely: a changed file outside
``<units-root>/<env>/`` (modules, root.hcl, other environments, .stagegate.yaml), a changed
file that belongs to no unit (a removed unit, a shared file), or a diff that cannot be computed.
Documentation-only files (``*.md``, ``docs/**``, ``LICENSE``) are ignored (R38): a PR that
changes nothing else gives the empty result, and in a mixed PR they do not widen it.
"""

from __future__ import annotations

import argparse
import os
import re
import subprocess
from collections.abc import Callable
from pathlib import PurePosixPath

from stagegate import cli

IGNORABLE_DIRS = ("docs",)  # R38: documentation never changes infrastructure
IGNORABLE_SUFFIXES = (".md",)
IGNORABLE_NAMES = ("LICENSE",)
ALL = None  # classify()/changed_units() return this when every unit must be planned
_REV = re.compile(r"^[0-9a-fA-F]{7,64}$")
UNIT_FILE = "terragrunt.hcl"


def _ignorable(f: str) -> bool:
    path = PurePosixPath(f)
    return (
        path.suffix in IGNORABLE_SUFFIXES
        or path.name in IGNORABLE_NAMES
        or (path.parts[0] in IGNORABLE_DIRS and len(path.parts) > 1)
    )


def classify(
    files: list[str],
    units_root: str,
    env: str,
    is_unit_dir: Callable[[str], bool],
) -> list[str] | None:
    """Unit names touched by ``files``; ``ALL`` (None) when any file cannot be tied to a unit.

    ``is_unit_dir(rel)`` says whether the repository-relative directory ``rel`` is a unit.
    """
    prefix = PurePosixPath(units_root) / env
    names: set[str] = set()
    for f in files:
        path = PurePosixPath(f)
        if _ignorable(f):
            continue
        if not path.is_relative_to(prefix) or path == prefix:
            return ALL
        unit = next(
            (
                d
                for d in path.parents
                if d != prefix and d.is_relative_to(prefix) and is_unit_dir(str(d))
            ),
            None,
        )
        if unit is None:
            return ALL
        names.add(str(unit.relative_to(prefix)).replace("/", "__"))
    return sorted(names)


def _diff_files(base: str, head: str) -> list[str] | None:
    if not (_REV.match(base) and _REV.match(head)):
        return None
    try:
        proc = subprocess.run(
            ["git", "diff", "--name-only", "-z", "--no-renames", f"{base}...{head}", "--"],
            capture_output=True,
            env={**os.environ, "LC_ALL": "C"},
            check=False,
        )
    except OSError:
        return None
    if proc.returncode != 0:
        return None
    return [f.decode("utf-8", "surrogateescape") for f in proc.stdout.split(b"\0") if f]


def changed_units(base: str, head: str, units_root: str, env: str) -> list[str] | None:
    """Changed units of ``env`` between ``base`` and ``head`` (run in the checked-out head)."""
    files = _diff_files(base, head)
    if files is None:
        return ALL
    return classify(
        files, units_root, env, lambda rel: os.path.isfile(os.path.join(rel, UNIT_FILE))
    )


def _run(args: argparse.Namespace) -> int:
    units = changed_units(args.base, args.head, args.units_root.strip("/"), args.env)
    print("ALL" if units is ALL else ",".join(units))
    return 0


def _setup(parser: argparse.ArgumentParser) -> None:
    parser.add_argument("--base", required=True, help="base commit SHA")
    parser.add_argument("--head", required=True, help="head commit SHA")
    parser.add_argument("--units-root", required=True)
    parser.add_argument("--env", required=True)
    parser.set_defaults(handler=_run)


cli.register("changed-units", _setup)
