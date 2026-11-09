"""``stagegate findings new``: count the high/critical code-scanning alerts raised recently."""

from __future__ import annotations

import argparse
import sys
from datetime import UTC, datetime, timedelta

from stagegate import cli
from stagegate.github import GitHub, GitHubError

SEVERITIES = ("high", "critical")


def count_new(gh, repo: str, since_hours: float, now: datetime | None = None) -> int:
    """Open high/critical alerts created in the last ``since_hours`` (counts only, no details)."""
    cutoff = (now or datetime.now(UTC)) - timedelta(hours=since_hours)
    seen: set[int] = set()
    for severity in SEVERITIES:
        for alert in gh.list_code_scanning_alerts(repo, state="open", severity=severity):
            created = datetime.fromisoformat(
                str(alert.get("created_at", "")).replace("Z", "+00:00")
            )
            if created >= cutoff and isinstance(alert.get("number"), int):
                seen.add(alert["number"])
    return len(seen)


def _run(args: argparse.Namespace) -> int:
    gh: GitHub | None = None
    try:
        gh = GitHub.from_env()
        count = count_new(gh, args.repo, args.since_hours)
    except (GitHubError, ValueError) as exc:
        print(f"findings: error: {exc}", file=sys.stderr)
        return 1
    finally:
        if gh is not None:
            gh.close()
    print(count)
    return 0


def _setup(parser: argparse.ArgumentParser) -> None:
    sub = parser.add_subparsers(dest="findings_command", metavar="<action>", required=True)
    p = sub.add_parser("new", help="count open high/critical alerts created in a time window")
    p.add_argument("--repo", required=True, help="owner/name")
    p.add_argument("--since-hours", type=float, default=26.0)
    p.set_defaults(handler=_run)


cli.register("findings", _setup)
