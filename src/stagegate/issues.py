"""``stagegate issue stop-the-line``: open (or extend) the issue that blocks later stages."""

from __future__ import annotations

import argparse
import json
import re
import sys
from pathlib import Path

from stagegate import cli
from stagegate.github import GitHub, GitHubError
from stagegate.labels import APPLY_FAILED_LABEL, POST_APPROVAL_LABEL, STOP_LABEL

# The one incident reference format (also enforced by sg_check_mode and iac-breakglass.yml).
INCIDENT_RE = re.compile(r"[A-Z]+-[0-9]+|INC[0-9]+")
_LOGIN_RE = re.compile(r"[A-Za-z0-9][A-Za-z0-9-]*(\[bot\])?")
_COUNT_FACTS = (
    ("critical", "critical"),
    ("high", "high"),
    ("medium", "medium"),
    ("total", "total"),
)
_SHA_RE = re.compile(r"[0-9a-f]{40}")
_URL_RE = re.compile(r"https://[^\s`<>]+")

CHECKLIST = (
    "Change reviewed by an independent approver",
    "Incident record linked",
    "Follow-up PR merged to main",
    "Drift check clean",
)


def _run(args: argparse.Namespace) -> int:
    gh: GitHub | None = None
    body = f"Apply failed for commit `{args.sha[:12]}`.\n\nRun: {args.run_url}\n"
    try:
        gh = GitHub.from_env()
        number = gh.open_or_comment_issue(
            args.repo,
            f"stop-the-line: {args.env} apply failed",
            body,
            [STOP_LABEL, APPLY_FAILED_LABEL, f"env:{args.env}"],
        )
    except GitHubError as exc:
        print(f"issue: error: {exc}", file=sys.stderr)
        return 1
    finally:
        if gh is not None:
            gh.close()
    print(f"issue: stop-the-line #{number}")
    return 0


def post_approval_body(
    incident: str,
    sha: str,
    run_url: str,
    approvers: list[str],
    counts: dict | None = None,
    rollback_from: str | None = None,
) -> str:
    """Issue body for a break-glass use: identifiers, links and counts only, never values."""
    boxes = "\n".join(f"- [ ] {item}" for item in CHECKLIST)
    lines = [
        f"- Incident: `{incident}`",
        f"- Commit: `{sha[:12]}`",
        f"- Run: {run_url}",
        f"- Approvers of the break-glass run: {', '.join(approvers)}",
    ]
    if rollback_from:
        lines.append(f"- Rollback from `{rollback_from[:12]}`")
    if counts is not None:
        lines.append(
            "- Changes applied: "
            + ", ".join(f"{label} {int(counts.get(key, 0))}" for label, key in _COUNT_FACTS)
        )
    return (
        "Break-glass was used to change prod outside the normal approval flow. "
        "It needs approval after the fact "
        "(DORA RTS 2024/1774 Art. 17(1)(g)).\n\n"
        + "\n".join(lines)
        + f"\n\n## Approval after the fact\n\n{boxes}\n"
    )


def _counts_from_changeset(path: str) -> dict:
    cs = json.loads(Path(path).read_text())
    return {**cs["risk"], "total": len(cs["items"])}


def _post_approval(args: argparse.Namespace) -> int:
    """Open a new post-approval issue (one per break-glass use, never merged into another)."""
    approvers = [a for a in args.approvers.split(",") if a]
    problem = None
    if not INCIDENT_RE.fullmatch(args.incident):
        problem = "incident must look like ABC-123 or INC123"
    elif not _SHA_RE.fullmatch(args.sha):
        problem = "sha must be a full 40-character commit SHA"
    elif args.rollback_from is not None and not _SHA_RE.fullmatch(args.rollback_from):
        problem = "rollback-from must be a full 40-character commit SHA"
    elif not _URL_RE.fullmatch(args.run_url):
        problem = "run-url must be an https URL"
    elif not approvers or not all(_LOGIN_RE.fullmatch(a) for a in approvers):
        problem = "approvers must be a comma-separated list of logins"
    if problem:
        print(f"issue: error: {problem}", file=sys.stderr)
        return 2
    counts = None
    if args.changeset:
        try:
            counts = _counts_from_changeset(args.changeset)
        except (OSError, ValueError, KeyError, TypeError) as exc:
            print(f"issue: error: cannot read changeset: {type(exc).__name__}", file=sys.stderr)
            return 2
    gh: GitHub | None = None
    try:
        gh = GitHub.from_env()
        number = gh.create_issue(
            args.repo,
            f"post-approval: break-glass {args.incident}",
            post_approval_body(
                args.incident, args.sha, args.run_url, approvers, counts, args.rollback_from
            ),
            [POST_APPROVAL_LABEL, "env:prod"],
        )
    except GitHubError as exc:
        print(f"issue: error: {exc}", file=sys.stderr)
        return 1
    finally:
        if gh is not None:
            gh.close()
    print(f"issue: post-approval #{number}")
    return 0


def _open_count(args: argparse.Namespace) -> int:
    """Print how many open issues carry all the given labels (a skip condition for workflows)."""
    gh: GitHub | None = None
    try:
        gh = GitHub.from_env()
        count = len(gh.list_issues(args.repo, args.label))
    except GitHubError as exc:
        print(f"issue: error: {exc}", file=sys.stderr)
        return 1
    finally:
        if gh is not None:
            gh.close()
    print(count)
    return 0


def _setup(parser: argparse.ArgumentParser) -> None:
    sub = parser.add_subparsers(dest="issue_command", metavar="<action>", required=True)
    p = sub.add_parser("stop-the-line", help="open or update the stop-the-line issue for a stage")
    p.add_argument("--repo", required=True, help="owner/name")
    p.add_argument("--env", required=True)
    p.add_argument("--sha", required=True)
    p.add_argument("--run-url", required=True)
    p.set_defaults(handler=_run)
    p = sub.add_parser(
        "post-approval", help="open the after-the-fact approval issue for a break-glass use"
    )
    p.add_argument("--repo", required=True, help="owner/name")
    p.add_argument("--incident", required=True)
    p.add_argument("--sha", required=True)
    p.add_argument("--run-url", required=True)
    p.add_argument("--approvers", required=True, help="comma-separated logins")
    p.add_argument("--changeset", help="changeset JSON; only its counts go into the issue")
    p.add_argument("--rollback-from", help="live prod SHA that this run rolls back from")
    p.set_defaults(handler=_post_approval)
    p = sub.add_parser("open-count", help="print the number of open issues with all given labels")
    p.add_argument("--repo", required=True, help="owner/name")
    p.add_argument("--label", action="append", required=True)
    p.set_defaults(handler=_open_count)


cli.register("issue", _setup)
