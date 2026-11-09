"""Stage guard: predecessor converged at this SHA, not superseded, no stop-the-line issue.

Runs at the start of every stage job and again at apply time in prod. It is the defence
against an older approved run applying a stale SHA (D4).
"""

from __future__ import annotations

import argparse
import sys
from dataclasses import dataclass
from pathlib import Path
from typing import Protocol

from stagegate import cli
from stagegate.github import GitHub, GitHubError
from stagegate.labels import STOP_LABEL
from stagegate.ledger import LedgerError, LedgerStore, open_store

# Stages that write no ledger record; skipped when looking for a predecessor (R21).
NO_RECORD_STAGES = frozenset({"prod-plan"})
# Stages subject to the stop-the-line check (R5).
_STOP_STAGES = frozenset({"prod-plan", "prod"})
DEFAULT_ORDER = "dev,prp,prod-plan,prod"


@dataclass(frozen=True)
class GuardResult:
    ok: bool
    reasons: tuple[str, ...]


class IssueQuery(Protocol):
    def open_stop_the_line(self, repo: str, env: str) -> list[int]: ...


class GitHubIssueQuery:
    """:class:`IssueQuery` over issues labelled ``stagegate:stop-the-line`` and ``env:<env>``."""

    def __init__(self, gh: GitHub) -> None:
        self._gh = gh

    def open_stop_the_line(self, repo: str, env: str) -> list[int]:
        issues = self._gh.list_issues(repo, [STOP_LABEL, f"env:{env}"])
        numbers = []
        for issue in issues:
            number = issue.get("number")
            if isinstance(number, bool) or not isinstance(number, int):
                raise GitHubError(0, "issue without an integer number in response")
            numbers.append(number)
        return numbers


def _normalize_order(order: list[str]) -> list[str]:
    names = [n.strip() for n in order]
    if not names or any(not n for n in names):
        raise ValueError("order must be a non-empty list of non-empty stage names")
    if len(set(names)) != len(names):
        raise ValueError("order contains duplicate stages")
    return names


def _predecessor(stage: str, order: list[str]) -> str | None:
    if stage not in order:
        raise ValueError(f"stage {stage!r} is not in order {order!r}")
    for name in reversed(order[: order.index(stage)]):
        if name not in NO_RECORD_STAGES:
            return name
    return None


def needs_issues(stage: str) -> bool:
    return stage in _STOP_STAGES


def check(
    repo: str,
    stage: str,
    sha: str,
    order: list[str],
    store: LedgerStore,
    issues: IssueQuery,
) -> GuardResult:
    order = _normalize_order(order)
    pred = _predecessor(stage, order)
    reasons: list[str] = []
    if pred is None and needs_issues(stage):
        # R22: prod-plan and prod always need a recordable predecessor; never fail open.
        reasons.append("predecessor_missing")
    if pred is not None:
        cur = store.current(repo, pred)
        if cur is None:
            reasons.append("predecessor_missing")
        elif cur.sha != sha:
            reasons.append("superseded" if cur.converged else "predecessor_missing")
        elif not cur.converged:
            reasons.append("predecessor_not_converged")
    if needs_issues(stage):
        upto = order[: order.index("prod") + 1] if "prod" in order else order
        envs = [e for e in upto if e not in NO_RECORD_STAGES]
        if any(issues.open_stop_the_line(repo, env) for env in envs):
            reasons.append("stop_the_line")
    return GuardResult(not reasons, tuple(reasons))


def _run(args: argparse.Namespace) -> int:
    order = args.order.split(",")
    gh: GitHub | None = None
    try:
        store = open_store(args.store)
        if needs_issues(args.stage):
            gh = GitHub.from_env()
            query: IssueQuery = GitHubIssueQuery(gh)
        else:
            query = _NoIssues()
        result = check(args.repo, args.stage, args.sha, order, store, query)
    except Exception as exc:  # fail closed, never a traceback
        print(f"guard: error: {_describe(exc)}", file=sys.stderr)
        return 1
    finally:
        if gh is not None:
            gh.close()
    if result.ok:
        return 0
    reasons = ",".join(result.reasons)
    print(f"guard: rejected {args.stage}@{args.sha[:12]}: {reasons}", file=sys.stderr)
    if args.summary:
        try:
            lines = "".join(f"- `{r}`\n" for r in result.reasons)
            with Path(args.summary).open("a") as fh:
                fh.write(f"### Guard rejected `{args.stage}` at `{args.sha[:12]}`\n\n{lines}\n")
        except OSError as exc:
            print(f"guard: error: cannot write summary: {type(exc).__name__}", file=sys.stderr)
    return 1


class _NoIssues:
    def open_stop_the_line(self, repo: str, env: str) -> list[int]:
        raise AssertionError("issue query not expected for this stage")


def _describe(exc: Exception) -> str:
    # ValueError (unknown stage), LedgerError and GitHubError messages are ours and token-free.
    if isinstance(exc, ValueError | LedgerError | GitHubError | OSError):
        return str(exc)
    return f"unexpected {type(exc).__name__}"


def _setup(parser: argparse.ArgumentParser) -> None:
    parser.add_argument("--store", required=True, help="ledger store URL")
    parser.add_argument("--repo", required=True, help="owner/name")
    parser.add_argument("--stage", required=True)
    parser.add_argument("--sha", required=True)
    parser.add_argument("--order", default=DEFAULT_ORDER, help="comma-separated stage order")
    parser.add_argument("--summary", help="append a Markdown rejection block to this file")
    parser.set_defaults(handler=_run)


cli.register("guard", _setup)
