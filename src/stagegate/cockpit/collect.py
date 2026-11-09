"""Cockpit collector (D8): one ``snapshot.json`` from the ledgers and GitHub, all repos.

The snapshot (``schema/snapshot.v1.json``) carries counts, addresses and links only: never
plan values, never finding details. A repo whose data cannot be fetched is still listed, with
``error`` set and its environments unknown; the run exits 1 only when every repo failed.
Degraded per environment instead: HTTP 403 on pending deployments gives ``gate.waiting``
null; 404/422 on compare gives ``pending_commits`` null.

Repos file: a YAML list of ``{repo, ledger, stages?, workflows?}``. ``workflows`` (default
``[".github/workflows/main.yml"]``, the caller of iac-staged) lists the workflow files whose
waiting runs count as gates.

Rate limit: per repo and run about 6 + 7 x envs requests (main, waiting runs, code scanning
x2, plus per env: ledger current.json + history listing + newest history entry, compare, drift
issues, stop-the-line issues, and one pending_deployments per waiting run). With
``--etag-cache FILE`` unchanged responses come back 304, which do not count against the
primary rate limit; restore and save FILE between runs (actions/cache). Without the flag no
cache is used. The file holds API JSON only (never credentials), at most 2000 entries and
about 20 MB, oldest evicted first.
"""

from __future__ import annotations

import argparse
import json
import re
import sys
from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path
from typing import Any
from urllib.parse import urlparse

import yaml

from stagegate import cli
from stagegate.cockpit.render_md import add_subcommands
from stagegate.drift import DRIFT_BULLET_PREFIX
from stagegate.github import GitHub, GitHubError
from stagegate.labels import DRIFT_LABEL, STOP_LABEL
from stagegate.ledger import LedgerError, LedgerRecord, LedgerStore, open_store

SCHEMA = "stagegate.snapshot/v1"
DEFAULT_STAGES = ["dev", "prp", "prod"]
DEFAULT_WORKFLOWS = [".github/workflows/main.yml"]
_REPO = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]*/[A-Za-z0-9][A-Za-z0-9._-]*$")


class ReposFileError(Exception):
    """The repos file is invalid."""


@dataclass(frozen=True)
class RepoEntry:
    repo: str
    ledger: str
    stages: list[str] = field(default_factory=lambda: list(DEFAULT_STAGES))
    workflows: list[str] = field(default_factory=lambda: list(DEFAULT_WORKFLOWS))


def load_repos(path: Path) -> list[RepoEntry]:
    try:
        doc = yaml.safe_load(Path(path).read_text())
    except (OSError, yaml.YAMLError) as exc:
        raise ReposFileError(f"cannot read repos file: {type(exc).__name__}") from None
    if not isinstance(doc, list) or not doc:
        raise ReposFileError("repos file: expected a non-empty list")
    out: list[RepoEntry] = []
    for i, item in enumerate(doc):
        where = f"repos[{i}]"
        if not isinstance(item, dict):
            raise ReposFileError(f"{where}: expected a mapping")
        unknown = sorted(str(k) for k in item if k not in {"repo", "ledger", "stages", "workflows"})
        if unknown:
            raise ReposFileError(f"{where}: unknown key(s): {', '.join(unknown)}")
        repo, ledger = item.get("repo"), item.get("ledger")
        if not isinstance(repo, str) or not _REPO.match(repo):
            raise ReposFileError(f"{where}.repo: expected owner/name")
        if not isinstance(ledger, str) or not ledger:
            raise ReposFileError(f"{where}.ledger: expected a store URL")
        stages = item.get("stages", DEFAULT_STAGES)
        if (
            not isinstance(stages, list)
            or not stages
            or not all(isinstance(s, str) and s and s != "prod-plan" for s in stages)
            or len(set(stages)) != len(stages)
        ):
            raise ReposFileError(f"{where}.stages: expected unique env names (not prod-plan)")
        workflows = item.get("workflows", DEFAULT_WORKFLOWS)
        if (
            not isinstance(workflows, list)
            or not workflows
            or not all(isinstance(w, str) and w for w in workflows)
        ):
            raise ReposFileError(f"{where}.workflows: expected workflow file paths")
        out.append(RepoEntry(repo, ledger, list(stages), list(workflows)))
    if len({e.repo for e in out}) != len(out):
        raise ReposFileError("repos file: duplicate repo")
    return out


def _env_unknown(env: str) -> dict[str, Any]:
    return {
        "env": env,
        "live_sha": None,
        "live_at": None,
        "converged": None,
        "pending_commits": None,
        "pending_compare_url": None,
        "gate": {"waiting": False, "since": None, "run_url": None},
        "drift": {"open": False, "since": None, "issue_url": None, "resources": 0},
        "health": {"last_result": "unknown", "stop_the_line": False},
    }


def _last_result(rec: LedgerRecord | None) -> str:
    if rec is None:
        return "unknown"
    statuses = {u.status for u in rec.units.values()}
    if "failed" in statuses:
        return "failed"
    if "applied" in statuses:
        return "applied"
    return "no_changes"


def _waiting_gates(gh: GitHub, repo: str, workflows: list[str]) -> dict[str, dict[str, str | None]]:
    """env -> oldest waiting run ``{since, run_url}``."""
    out: dict[str, dict[str, str | None]] = {}
    for run in gh.waiting_runs(repo, workflows):
        run_id = run.get("id")
        if isinstance(run_id, bool) or not isinstance(run_id, int):
            continue
        created = run.get("created_at")
        created = created if isinstance(created, str) else None
        names = {
            (d.get("environment") or {}).get("name") for d in gh.pending_deployments(repo, run_id)
        }
        for name in names:
            if not isinstance(name, str):
                continue
            cur = out.get(name)
            if cur is None or (created and (cur["since"] is None or created < cur["since"])):
                url = run.get("html_url")
                out[name] = {"since": created, "run_url": url if isinstance(url, str) else None}
    return out


def _drift(gh: GitHub, repo: str, env: str) -> dict[str, Any]:
    issues = gh.list_issues(repo, [DRIFT_LABEL, f"env:{env}"])
    if not issues:
        return {"open": False, "since": None, "issue_url": None, "resources": 0}
    issue = min(issues, key=lambda i: str(i.get("created_at") or ""))
    body = issue.get("body")
    url = issue.get("html_url")
    since = issue.get("created_at")
    return {
        "open": True,
        "since": since if isinstance(since, str) else None,
        "issue_url": url if isinstance(url, str) else None,
        "resources": sum(ln.startswith(DRIFT_BULLET_PREFIX) for ln in body.splitlines())
        if isinstance(body, str)
        else 0,
    }


def _collect_repo(entry: RepoEntry, gh: GitHub, store: LedgerStore) -> dict[str, Any]:
    repo = entry.repo
    main = gh.main_sha(repo)
    try:
        gates: dict[str, dict[str, str | None]] | None = _waiting_gates(gh, repo, entry.workflows)
    except GitHubError as exc:
        if exc.status != 403:  # no permission on deployments: gate state unknown, not an error
            raise
        gates = None
    counts = gh.code_scanning_counts(repo)
    findings = {
        "critical": counts["critical"] if counts else None,
        "high": counts["high"] if counts else None,
        "url": f"{gh.web_url}/{repo}/security/code-scanning" if counts else None,
    }
    envs: list[dict[str, Any]] = []
    prev_sha: str | None = main
    for name in entry.stages:
        e = _env_unknown(name)
        rec = store.current(repo, name)
        if rec is not None:
            e["live_sha"], e["live_at"], e["converged"] = rec.sha, rec.recorded_at, rec.converged
        live = e["live_sha"]
        if live is not None and prev_sha is not None:
            try:
                e["pending_commits"], e["pending_compare_url"] = gh.compare(repo, live, prev_sha)
            except GitHubError as exc:
                if exc.status not in (404, 422):  # stale or force-pushed live SHA: unknown
                    raise
        prev_sha = live
        if gates is None:
            e["gate"] = {"waiting": None, "since": None, "run_url": None}
        elif name in gates:
            e["gate"] = {"waiting": True, **gates[name]}
        e["drift"] = _drift(gh, repo, name)
        e["health"] = {
            "last_result": _last_result(rec),
            "stop_the_line": bool(gh.list_issues(repo, [STOP_LABEL, f"env:{name}"])),
        }
        envs.append(e)
    return {"repo": repo, "main_sha": main, "findings": findings, "envs": envs}


def _short(exc: Exception) -> str:
    return str(exc).replace("\n", " ")[:200] or type(exc).__name__


def collect(
    entries: list[RepoEntry],
    gh: GitHub,
    *,
    store_factory: Callable[..., LedgerStore] = open_store,
    now: datetime | None = None,
) -> dict[str, Any]:
    """Build the snapshot. Per-repo failures become ``error`` entries, never exceptions."""
    repos: list[dict[str, Any]] = []
    for entry in entries:
        try:
            repos.append(_collect_repo(entry, gh, store_factory(entry.ledger, gh=gh)))
        except (GitHubError, LedgerError) as exc:
            repos.append(
                {
                    "repo": entry.repo,
                    "main_sha": None,
                    "error": _short(exc),
                    "findings": {"critical": None, "high": None, "url": None},
                    "envs": [_env_unknown(s) for s in entry.stages],
                }
            )
    stamp = (now or datetime.now(UTC)).strftime("%Y-%m-%dT%H:%M:%SZ")
    return {"schema": SCHEMA, "generated_at": stamp, "repos": repos}


def _run(args: argparse.Namespace) -> int:
    gh = None
    cache = Path(args.etag_cache) if args.etag_cache else None
    try:
        entries = load_repos(Path(args.repos_file))
        gh = GitHub.from_env()
        if cache:
            gh.enable_etag_cache(cache)
        snapshot = collect(entries, gh)
        if cache:
            gh.save_etag_cache(cache)
    except (ReposFileError, GitHubError, OSError) as exc:
        print(f"stagegate: cockpit collect failed: {exc}", file=sys.stderr)
        return 2
    finally:
        if gh is not None:
            gh.close()
    try:
        Path(args.out).write_text(json.dumps(snapshot, indent=2, sort_keys=True) + "\n")
    except OSError as exc:
        print(
            f"stagegate: cockpit collect failed: cannot write --out ({exc.strerror})",
            file=sys.stderr,
        )
        return 1
    failed = [r["repo"] for r in snapshot["repos"] if "error" in r]
    for repo in failed:
        print(f"stagegate: cockpit: {repo}: data unavailable", file=sys.stderr)
    return 1 if len(failed) == len(snapshot["repos"]) else 0


MAX_ESTATE_REPOS = 500


def estate_repositories(entries: list[RepoEntry], owner: str) -> list[str]:
    """Names (without owner) of every repository the collector reads: ``repo:`` entries and the
    repositories of ``github-branch://`` ledgers. All must belong to ``owner``; at most 500.

    The cockpit-collect workflow reads ledgers only through the GitHub App token, so any other
    store URL (file://, git-branch://, azure-blob://) is refused here, before a token is minted."""
    full: set[str] = set()
    for e in entries:
        full.add(e.repo)
        u = urlparse(e.ledger)
        if u.scheme != "github-branch":
            raise ReposFileError(
                f"{e.repo}: ledger must be a github-branch:// URL; cockpit-collect reads no other "
                "ledger store (see docs/cockpit/setup.md)"
            )
        full.add(f"{u.netloc}/{u.path.strip('/')}")
    names: set[str] = set()
    for f in sorted(full):
        o, _, name = f.partition("/")
        if o.lower() != owner.lower() or not _REPO.match(f):
            raise ReposFileError(f"{f}: not a repository of {owner} (the estate shares one owner)")
        names.add(name)
    if len(names) > MAX_ESTATE_REPOS:
        raise ReposFileError(f"{len(names)} repositories: at most {MAX_ESTATE_REPOS}")
    return sorted(names)


def _repos(args: argparse.Namespace) -> int:
    try:
        names = estate_repositories(load_repos(Path(args.repos_file)), args.owner)
    except ReposFileError as exc:
        print(f"stagegate: cockpit repos failed: {exc}", file=sys.stderr)
        return 2
    print(",".join(names))
    return 0


def _setup(parser: argparse.ArgumentParser) -> None:
    sub = parser.add_subparsers(dest="cockpit_command", required=True, metavar="<action>")
    p = sub.add_parser("collect", help="build snapshot.json from ledgers and GitHub")
    p.add_argument("--repos-file", required=True, help="YAML list of {repo, ledger, stages?}")
    p.add_argument("--out", required=True, help="snapshot file to write")
    p.add_argument("--etag-cache", metavar="FILE", help="persist the ETag cache in FILE (JSON)")
    p.set_defaults(handler=_run)
    r = sub.add_parser("repos", help="list the repositories to mint the read token for")
    r.add_argument("--repos-file", required=True)
    r.add_argument("--owner", required=True, help="owner every repository must belong to")
    r.set_defaults(handler=_repos)
    add_subcommands(sub)


cli.register("cockpit", _setup)
