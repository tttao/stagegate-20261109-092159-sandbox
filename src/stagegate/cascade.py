"""``stagegate cascade check``: refuse a saved plan whose dependents were planned against stale
upstream outputs (R52).

A unit that reads another unit's outputs (a Terragrunt ``dependency`` block) is planned with the
outputs the upstream has *before* its own apply. If both units change, the saved plan of the
dependent is wrong the moment the upstream is applied. same-job and breakglass modes avoid this by
re-planning each unit right before its apply; encrypted_plan and replan cannot, so they refuse.
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

from stagegate import cli

_QUIET = {("no-op",), ("read",)}


def changed_units(plan_json_dir: Path) -> set[str]:
    """Units whose plan JSON has at least one resource or output change other than no-op or
    read (R72): a changed output is what a dependent reads, so it changes the dependent's inputs.
    """
    out: set[str] = set()
    for path in sorted(plan_json_dir.glob("*.json")):
        doc = json.loads(path.read_text())
        changes = [rc.get("change", {}) for rc in doc.get("resource_changes") or []]
        changes += list((doc.get("output_changes") or {}).values())
        if any(tuple(c.get("actions") or ()) not in _QUIET for c in changes):
            out.add(path.stem)
    return out


def cascades(edges: dict[str, list[str]], changed: set[str]) -> list[tuple[str, str]]:
    """(dependent, upstream) pairs: a changed unit and a changed unit it depends on, directly or
    through other units. ``edges`` maps a unit to the units whose outputs it reads."""
    found: list[tuple[str, str]] = []
    for unit in sorted(changed):
        seen: set[str] = set()
        stack = list(edges.get(unit, []))
        while stack:
            up = stack.pop()
            if up in seen:
                continue
            seen.add(up)
            stack.extend(edges.get(up, []))
        found.extend((unit, up) for up in sorted((seen & changed) - {unit}))
    return found


def _run(args: argparse.Namespace) -> int:
    try:
        edges = json.loads(Path(args.edges).read_text())
        changed = changed_units(Path(args.plan_json))
    except (OSError, ValueError, AttributeError) as exc:
        print(f"cascade: error: {type(exc).__name__}", file=sys.stderr)
        return 1
    if not isinstance(edges, dict):
        print("cascade: error: edges must be a JSON object", file=sys.stderr)
        return 1
    pairs = cascades(edges, changed)
    for dependent, upstream in pairs:
        print(
            f"cascading change: {dependent} depends on {upstream}, which also changes; "
            f"land {upstream} first",
            file=sys.stderr,
        )
    return 1 if pairs else 0


def _setup(parser: argparse.ArgumentParser) -> None:
    sub = parser.add_subparsers(dest="cascade_command", metavar="<action>", required=True)
    p = sub.add_parser("check", help="exit 1 when a changed unit depends on another changed unit")
    p.add_argument("--edges", required=True, help='JSON file {"unit": ["upstream", ...]}')
    p.add_argument("--plan-json", required=True, help="directory of <unit>.json plans")
    p.set_defaults(handler=_run)


cli.register("cascade", _setup)
