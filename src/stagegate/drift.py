"""Nightly drift: evaluate ``plan -detailed-exitcode`` results and keep one issue per env.

Only addresses and attribute *paths* are ever used; plan values never reach an issue, a log
or the result file (D8).
"""

from __future__ import annotations

import argparse
import json
import os
import sys
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from stagegate import cli
from stagegate.config import DEFAULT_PATH, ConfigError, load_config
from stagegate.github import GitHub, GitHubError
from stagegate.labels import DRIFT_LABEL, STOP_LABEL
from stagegate.planjson import (
    Action,
    OutputChange,
    PlanFormatError,
    ResourceChange,
    load_output_changes,
    load_plan,
)

_QUIET = {Action.NOOP, Action.READ}


class DriftError(Exception):
    """The plan failed or its result cannot be trusted; this is never "no drift"."""


@dataclass(frozen=True)
class DriftResult:
    drifted: bool
    addresses: tuple[str, ...]


def _paths(before: Any, after: Any, prefix: str = "") -> set[str]:
    """Dotted paths of every leaf that differs; list indexes are numbers."""
    if isinstance(before, dict) and isinstance(after, dict):
        pairs = [(k, before.get(k), after.get(k)) for k in before.keys() | after.keys()]
    elif isinstance(before, list) and isinstance(after, list):
        size = max(len(before), len(after))
        pairs = [
            (str(i), before[i] if i < len(before) else None, after[i] if i < len(after) else None)
            for i in range(size)
        ]
    else:
        return {prefix} if before != after and prefix else set()
    out: set[str] = set()
    for key, b, a in pairs:
        out |= _paths(b, a, f"{prefix}.{key}" if prefix else str(key))
    return out


def _unknown_paths(marker: Any, prefix: str = "") -> set[str]:
    if marker is True:
        return {prefix} if prefix else set()
    if isinstance(marker, dict):
        items = marker.items()
    elif isinstance(marker, list):
        items = ((str(i), v) for i, v in enumerate(marker))
    else:
        return set()
    out: set[str] = set()
    for key, value in items:
        out |= _unknown_paths(value, f"{prefix}.{key}" if prefix else str(key))
    return out


def _under(path: str, noise: str) -> bool:
    return path == noise or path.startswith(noise + ".")


def _is_noise(change: ResourceChange, noise: dict[str, list[str]]) -> bool:
    """An update whose every changed attribute path is under a noise path of its type."""
    if change.actions != (Action.UPDATE,):
        return False
    allowed = noise.get(change.type, [])
    if not allowed or change.before is None or change.after is None:
        return False
    changed = _paths(change.before, change.after) | _unknown_paths(change.after_unknown)
    if not changed:
        return False  # cannot prove it is noise: fail closed
    return all(any(_under(p, n) for n in allowed) for p in changed)


def evaluate(
    env: str,
    exit_code: int,
    changes: list[ResourceChange],
    noise: dict[str, list[str]],
    outputs: list[OutputChange] | None = None,
) -> DriftResult:
    """Drifted addresses. A changed root output counts as drift too (R72), as ``output.<name>``:
    the live state no longer gives what the code at the live SHA declares."""
    if exit_code == 0:
        return DriftResult(False, ())
    if exit_code != 2:
        raise DriftError(f"{env}: plan failed with exit code {exit_code}; not treated as no drift")
    drifted = sorted(
        c.address
        for c in changes
        if c.mode == "managed" and not set(c.actions) <= _QUIET and not _is_noise(c, noise)
    )
    drifted += [f"output.{o.name}" for o in outputs or [] if not set(o.actions) <= _QUIET]
    return DriftResult(bool(drifted), tuple(drifted))


DRIFT_BULLET_PREFIX = "- `"  # the cockpit counts body lines that start with this


def drift_bullet(address: str) -> str:
    """One drifted resource line of the drift issue body."""
    return f"{DRIFT_BULLET_PREFIX}{address}`\n"


def _labels(env: str) -> list[str]:
    return [DRIFT_LABEL, f"env:{env}"]


def _number(issue: dict) -> int:
    number = issue.get("number")
    if isinstance(number, bool) or not isinstance(number, int):
        raise GitHubError(0, "issue without a number")
    return number


def _sync(gh, repo: str, env: str, result: DriftResult, run_url: str, sha: str):
    """Returns (issue number or None, change) with change in opened, updated, cleared, none."""
    existing = gh.list_issues(repo, _labels(env))
    if result.drifted:
        body = (
            f"Drift detected in `{env}`"
            + (f" at `{sha[:12]}`" if sha else "")
            + ".\n\nDrifted resources:\n"
            + "".join(drift_bullet(a) for a in result.addresses)
            + f"\nRun: {run_url}\n"
        )
        if existing:
            number = _number(existing[0])
            gh.create_issue_comment(repo, number, body)
            if env == "prod":
                have = [
                    str(x.get("name") if isinstance(x, dict) else x)
                    for x in existing[0].get("labels", [])
                ]
                if STOP_LABEL not in have:
                    gh.update_issue(repo, number, labels=[*have, STOP_LABEL])
            return number, "updated"
        labels = [*_labels(env), *([STOP_LABEL] if env == "prod" else [])]
        return gh.create_issue(repo, f"drift: {env}", body, labels), "opened"
    for issue in existing:
        number = _number(issue)
        gh.create_issue_comment(repo, number, f"drift cleared at {sha[:12]}")
        gh.update_issue(repo, number, state="closed")
    return None, ("cleared" if existing else "none")


def sync_issue(
    gh, repo: str, env: str, result: DriftResult, run_url: str, sha: str = ""
) -> int | None:
    """Open or comment on the env's drift issue; close it when the drift is gone."""
    return _sync(gh, repo, env, result, run_url, sha)[0]


def _read_json(path: Path, what: str) -> Any:
    try:
        return json.loads(Path(path).read_text())
    except (OSError, ValueError) as exc:
        raise DriftError(f"cannot read {what}: {type(exc).__name__}") from None


def _evaluate_cmd(args: argparse.Namespace) -> int:
    try:
        noise = load_config(Path(args.config)).drift_noise
        codes = _read_json(Path(args.exit_codes), "exit codes")
        if not isinstance(codes, dict):
            raise DriftError("exit codes file must map unit to exit code")
        files = sorted(Path(args.plan_json).glob("*.json"))
        if not files:
            raise DriftError("no plan JSON found")
        if {f.stem for f in files} != set(codes):
            raise DriftError("exit codes and plan JSON do not cover the same units")
        addresses: list[str] = []
        for f in files:
            code = codes[f.stem]
            if isinstance(code, bool) or not isinstance(code, int):
                raise DriftError(f"{f.stem}: exit code is not a number")
            changes = load_plan(f) if code == 2 else []
            outputs = load_output_changes(f) if code == 2 else []
            res = evaluate(args.env, code, changes, noise, outputs)
            addresses += [f"{f.stem}: {a}" for a in res.addresses]
    except (DriftError, ConfigError, PlanFormatError) as exc:
        print(f"drift: error: {exc}", file=sys.stderr)
        return 1
    out = {"env": args.env, "drifted": bool(addresses), "addresses": sorted(addresses)}
    Path(args.out).write_text(json.dumps(out, indent=2) + "\n")
    print(f"drift: {args.env}: {'drifted' if addresses else 'in sync'} ({len(addresses)})")
    return 0


def _sync_cmd(args: argparse.Namespace) -> int:
    gh: GitHub | None = None
    try:
        doc = _read_json(Path(args.result), "result")
        if not isinstance(doc, dict) or doc.get("env") != args.env:
            raise DriftError("result does not belong to this environment")
        drifted, addrs = doc.get("drifted"), doc.get("addresses")
        if not isinstance(drifted, bool):
            raise DriftError("result: drifted must be true or false")
        if not isinstance(addrs, list) or not all(isinstance(a, str) for a in addrs):
            raise DriftError("result: addresses must be a list of strings")
        result = DriftResult(drifted, tuple(addrs))
        gh = GitHub.from_env()
        number, change = _sync(gh, args.repo, args.env, result, args.run_url, args.sha)
    except (DriftError, GitHubError) as exc:
        print(f"drift: error: {exc}", file=sys.stderr)
        return 1
    finally:
        if gh is not None:
            gh.close()
    if os.environ.get("GITHUB_OUTPUT"):
        with open(os.environ["GITHUB_OUTPUT"], "a") as fh:
            fh.write(f"change={change}\n")
    print(f"drift: {args.env}: {change}" + (f" #{number}" if number else ""))
    return 0


def _setup(parser: argparse.ArgumentParser) -> None:
    sub = parser.add_subparsers(dest="drift_command", metavar="<action>", required=True)
    p = sub.add_parser("evaluate", help="decide drift from plan JSON and plan exit codes")
    p.add_argument("--plan-json", required=True)
    p.add_argument("--exit-codes", required=True, help="JSON mapping unit to plan exit code")
    p.add_argument("--env", required=True)
    p.add_argument("--config", default=str(DEFAULT_PATH))
    p.add_argument("--out", required=True)
    p.set_defaults(handler=_evaluate_cmd)
    p = sub.add_parser("sync", help="open, update or close the env's drift issue")
    p.add_argument("--repo", required=True, help="owner/name")
    p.add_argument("--env", required=True)
    p.add_argument("--result", required=True)
    p.add_argument("--sha", required=True)
    p.add_argument("--run-url", required=True)
    p.set_defaults(handler=_sync_cmd)


cli.register("drift", _setup)
