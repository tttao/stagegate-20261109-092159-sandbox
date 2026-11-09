"""``stagegate approvals list``: who approved a run's deployment to an environment."""

from __future__ import annotations

import argparse
import sys

from stagegate import cli
from stagegate.github import GitHub, GitHubError


def approvers(approvals: list[dict], env: str) -> list[str]:
    """Sorted distinct logins with an ``approved`` entry whose environments include ``env``.

    The API has no attempt field, so approvals from earlier attempts of the same run may be
    included: the result is a superset. Rejected entries are never listed.
    """
    logins: set[str] = set()
    for a in approvals:
        if a.get("state") != "approved":
            continue
        names = {e.get("name") for e in a.get("environments") or [] if isinstance(e, dict)}
        login = (a.get("user") or {}).get("login")
        if env in names and isinstance(login, str) and login:
            logins.add(login)
    return sorted(logins)


def rejected(approvals: list[dict], env: str) -> bool:
    """True when some review of this run rejected ``env``."""
    for a in approvals:
        names = {e.get("name") for e in a.get("environments") or [] if isinstance(e, dict)}
        if a.get("state") == "rejected" and env in names:
            return True
    return False


def job_never_ran(jobs: list[dict], name: str) -> bool | None:
    """Did the job called ``name`` (or ``<caller> / name``) execute no step at all? None when the
    job is not in the list. A job that a reviewer rejected has no steps, or only skipped ones."""
    for j in jobs:
        full = str(j.get("name", ""))
        if full == name or full.endswith(f" / {name}"):
            steps = j.get("steps") or []
            return all(
                s.get("conclusion") == "skipped" or s.get("status") == "skipped" for s in steps
            )
    return None


def _rejected(args: argparse.Namespace) -> int:
    gh: GitHub | None = None
    try:
        gh = GitHub.from_env()
        found = rejected(gh.run_approvals(args.repo, args.run_id), args.env)
        if found and args.job:
            # The approvals API has no attempt field: a rejection from an earlier attempt would
            # still be listed after a re-run whose apply then failed. Only a job of the current
            # attempt that executed nothing was stopped at the gate.
            never_ran = job_never_ran(
                gh.list_run_jobs(args.repo, args.run_id, latest=True), args.job
            )
            if never_ran is None:
                print(f"approvals: error: no job {args.job} in the latest attempt", file=sys.stderr)
                return 2
            found = never_ran
    except GitHubError as exc:
        print(f"approvals: error: {exc}", file=sys.stderr)
        return 2  # an API failure is neither "rejected" nor "not rejected"
    except Exception as exc:  # unexpected data: never report "not rejected" (exit 1) by mistake
        print(f"approvals: error: unexpected {type(exc).__name__}", file=sys.stderr)
        return 2
    finally:
        if gh is not None:
            gh.close()
    return 0 if found else 1


def _run(args: argparse.Namespace) -> int:
    gh: GitHub | None = None
    try:
        gh = GitHub.from_env()
        logins = approvers(gh.run_approvals(args.repo, args.run_id), args.env)
    except GitHubError as exc:
        print(f"approvals: error: {exc}", file=sys.stderr)
        return 1
    finally:
        if gh is not None:
            gh.close()
    if not logins:
        print(f"approvals: no approval for {args.env} on run {args.run_id}", file=sys.stderr)
        return 1
    print(",".join(logins))
    return 0


def _setup(parser: argparse.ArgumentParser) -> None:
    sub = parser.add_subparsers(dest="approvals_command", metavar="<action>", required=True)
    p = sub.add_parser("list", help="print the comma-joined logins that approved an environment")
    p.add_argument("--repo", required=True, help="owner/name")
    p.add_argument("--run-id", required=True, type=int)
    p.add_argument("--env", required=True)
    p.set_defaults(handler=_run)
    p = sub.add_parser(
        "rejected", help="exit 0 if a review rejected the environment on this run, 1 if not"
    )
    p.add_argument("--repo", required=True, help="owner/name")
    p.add_argument("--run-id", required=True, type=int)
    p.add_argument("--env", required=True)
    p.add_argument(
        "--job",
        help="also require that this job of the latest attempt ran no step (stopped at the gate)",
    )
    p.set_defaults(handler=_rejected)


cli.register("approvals", _setup)
