"""Markdown renderer for the cockpit snapshot, and the history index for the site.

``render`` serves places where Pages cannot be private (GHES, a pinned issue, a job summary):
repos are rows, environments are columns (the R59 ordered merge, shared with the site through
``schema/fixtures/env-order.json``) and each cell reads ``<status> <short sha> (+N)``. Status
precedence is the site's (``schema/fixtures/status-precedence.json``). Only counts and https links
are emitted (D8); every name is escaped so a repo or environment name cannot inject markup.

``history-index`` lists the dated snapshot copies for the site: see :func:`build_history_index`.
"""

from __future__ import annotations

import argparse
import json
import os
import re
import sys
from collections.abc import Callable, Sequence
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any
from urllib.parse import quote, urlparse

from stagegate.github import GitHub, GitHubError

SCHEMA = "stagegate.snapshot/v1"
MARKER = "<!-- stagegate:cockpit -->"
ISSUE_BODY_LIMIT = 65000  # GitHub rejects issue bodies above 65536 characters
_REPO_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]*/[A-Za-z0-9][A-Za-z0-9._-]*$")

STATUS_LABELS = {
    "held": "Held",
    "failed": "Failed",
    "drift": "Drift",
    "waiting": "Waiting",
    "pending": "Pending",
    "synced": "Synced",
    "unknown": "Unknown",
}

Env = dict[str, Any]


def _get(d: Any, *path: str) -> Any:
    for key in path:
        if not isinstance(d, dict):
            return None
        d = d.get(key)
    return d


# First match wins; same order and conditions as cockpit-site/src/status.ts PRECEDENCE.
PRECEDENCE: list[tuple[str, Callable[[Env], bool]]] = [
    ("held", lambda e: _get(e, "health", "stop_the_line") is True),
    ("failed", lambda e: _get(e, "health", "last_result") == "failed"),
    ("drift", lambda e: _get(e, "drift", "open") is True),
    ("waiting", lambda e: _get(e, "gate", "waiting") is True),
    ("pending", lambda e: (e.get("pending_commits") or 0) > 0),
    (
        "synced",
        lambda e: (
            _get(e, "health", "last_result") in ("applied", "no_changes")
            and bool(e.get("live_sha"))
        ),
    ),
]
FALLBACK = "unknown"


def status_for(env: Env) -> str:
    for name, test in PRECEDENCE:
        if test(env):
            return name
    return FALLBACK


def merge_env_order(sequences: Sequence[Sequence[str]]) -> list[str]:
    """R59: insert an unseen env after its predecessor (first if none); the first repo wins."""
    out: list[str] = []
    for seq in sequences:
        prev: str | None = None
        for env in seq:
            if env not in out:
                out.insert(0 if prev is None else out.index(prev) + 1, env)
            prev = env
    return out


_ESCAPES = {"&": "&amp;", "<": "&lt;", ">": "&gt;"}


def escape_md(text: object) -> str:
    """Make ``text`` inert in Markdown (including inside a table cell) and in HTML."""
    out: list[str] = []
    for ch in str(text):
        if ch in "\r\n\t":
            out.append(" ")
        elif ch in _ESCAPES:
            out.append(_ESCAPES[ch])
        elif ch in "\\|`*_[]":
            out.append("\\" + ch)
        else:
            out.append(ch)
    return "".join(out)


def _https_url(url: object) -> str | None:
    if not isinstance(url, str):
        return None
    parsed = urlparse(url.strip())
    if parsed.scheme != "https" or not parsed.netloc:
        return None
    return quote(url.strip(), safe="/:?#[]@!$&'*+,;=%~-._")


def _link(label: str, url: object) -> str:
    href = _https_url(url)
    return f"[{label}]({href})" if href else label


def _count(n: object) -> str:
    return str(n) if isinstance(n, int) and not isinstance(n, bool) else "n/a"


def _cell(env: Env) -> str:
    text = f"{STATUS_LABELS[status_for(env)]} {escape_md((env.get('live_sha') or '-')[:7])}"
    pending = env.get("pending_commits") or 0
    if pending > 0:
        return _link(f"{text} (+{pending})", env.get("pending_compare_url"))
    return text


def render(snapshot: dict[str, Any]) -> str:
    repos = [r for r in snapshot.get("repos", []) if isinstance(r, dict)]
    columns = merge_env_order(
        [[e.get("env", "") for e in r.get("envs", []) if isinstance(e, dict)] for r in repos]
    )
    lines = [
        f"**IaC cockpit** - generated {escape_md(snapshot.get('generated_at', 'unknown'))}",
        "",
    ]
    lines.append("| " + " | ".join(["Repository", *map(escape_md, columns)]) + " |")
    lines.append("| " + " | ".join(["---"] * (len(columns) + 1)) + " |")
    for r in repos:
        cells: list[str] = []
        by_env = {e.get("env"): e for e in r.get("envs", []) if isinstance(e, dict)}
        for name in columns:
            if r.get("error"):
                cells.append("Error")
            elif name in by_env:
                cells.append(_cell(by_env[name]))
            else:
                cells.append("—")
        lines.append("| " + " | ".join([escape_md(r.get("repo", "?")), *cells]) + " |")
    lines += ["", "Findings (code scanning, per repository):", ""]
    for r in repos:
        f = r.get("findings") if isinstance(r.get("findings"), dict) else {}
        counts = f"critical {_count(f.get('critical'))}, high {_count(f.get('high'))}"
        text = f"- {escape_md(r.get('repo', '?'))}: {counts}"
        href = _https_url(f.get("url"))
        if href:
            text += f" ({_link('code scanning', href)})"
        lines.append(text)
    errors = [r for r in repos if r.get("error")]
    if errors:
        lines += ["", "Errors:", ""]
        lines += [f"- {escape_md(r.get('repo', '?'))}: {escape_md(r['error'])}" for r in errors]
    return "\n".join(lines) + "\n"


# ---- history index ---------------------------------------------------------------------------

_HIST_RE = re.compile(r"^(\d{4})/(\d{2})/(\d{2})/(\d{2})(\d{2})\.json$")


def build_history_index(directory: Path, days: int, now: datetime | None = None) -> list[str]:
    """Snapshot copies ``DIR/YYYY/MM/DD/HHMM.json`` newer than ``days`` days (UTC, from the path).

    Returns a sorted (oldest first, so newest last) list of paths relative to the PARENT of DIR,
    i.e. ``<DIR name>/YYYY/MM/DD/HHMM.json``. The site serves DIR's parent as its data root and
    fetches ``<root>/history/index.json`` then ``<root>/<path>``.
    """
    cutoff = (now or datetime.now(UTC)) - timedelta(days=days)
    found: list[tuple[datetime, str]] = []
    for path in directory.rglob("*.json"):
        rel = path.relative_to(directory).as_posix()
        m = _HIST_RE.match(rel)
        if not m:
            continue
        try:
            at = datetime(*map(int, m.groups()), tzinfo=UTC)
        except ValueError:
            continue
        if at >= cutoff:
            found.append((at, f"{directory.resolve().name}/{rel}"))
    return [p for _, p in sorted(found)]


# ---- CLI -------------------------------------------------------------------------------------


def _fail(msg: str, code: int = 2) -> int:
    print(f"stagegate: cockpit: {msg}", file=sys.stderr)
    return code


def _run_render(args: argparse.Namespace) -> int:
    try:
        snapshot = json.loads(Path(args.snapshot).read_text())
    except (OSError, ValueError) as exc:
        return _fail(f"cannot read --snapshot: {type(exc).__name__}")
    if not isinstance(snapshot, dict) or snapshot.get("schema") != SCHEMA:
        return _fail(f"--snapshot is not a {SCHEMA} document")
    md = render(snapshot)
    if args.issue_number is not None:
        return _update_issue(args, md)
    if args.summary:
        try:
            with open(args.summary, "a", encoding="utf-8") as fh:
                fh.write(md)
        except OSError as exc:
            return _fail(f"cannot write --summary ({exc.strerror})", 1)
        return 0
    sys.stdout.write(md)
    return 0


def _update_issue(args: argparse.Namespace, md: str) -> int:
    repo = args.repo or os.environ.get("STAGEGATE_DATA_REPOSITORY", "")
    if not _REPO_RE.match(repo):
        return _fail("issue mode needs --repo OWNER/NAME (or STAGEGATE_DATA_REPOSITORY)")
    body = f"{MARKER}\n{md}"
    if len(body) > ISSUE_BODY_LIMIT:
        return _fail("rendered table is too large for an issue body", 1)
    gh = None
    try:
        gh = GitHub.from_env()
        existing = gh.get_issue(repo, args.issue_number).get("body") or ""
        if existing.strip() and not existing.lstrip().startswith(MARKER):
            return _fail(
                f"issue #{args.issue_number} has a body without the {MARKER} marker; "
                "refusing to overwrite a human issue"
            )
        gh.update_issue(repo, args.issue_number, body=body)
    except GitHubError as exc:
        return _fail(f"issue update failed: {exc}")
    finally:
        if gh is not None:
            gh.close()
    return 0


def _run_index(args: argparse.Namespace) -> int:
    directory = Path(args.dir)
    if not directory.is_dir():
        return _fail("--dir is not a directory")
    if args.days < 0:
        return _fail("--days must be >= 0")
    paths = build_history_index(directory, args.days)
    out = Path(args.out)
    try:
        tmp = out.with_name(out.name + ".tmp")
        tmp.write_text(json.dumps(paths, indent=2) + "\n")
        tmp.replace(out)
    except OSError as exc:
        return _fail(f"cannot write --out ({exc.strerror})", 1)
    return 0


def add_subcommands(sub: Any) -> None:
    p = sub.add_parser("render-md", help="render snapshot.json as a Markdown table")
    p.add_argument("--snapshot", required=True, help="snapshot.json to render")
    p.add_argument("--summary", metavar="PATH", help="append the table to PATH (job summary)")
    p.add_argument("--issue-number", type=int, help="update the body of this issue")
    p.add_argument("--repo", help="OWNER/NAME of the issue (default: $STAGEGATE_DATA_REPOSITORY)")
    p.set_defaults(handler=_run_render)
    p = sub.add_parser("history-index", help="list recent history snapshots for the site")
    p.add_argument("--dir", required=True, help="history directory (DIR/YYYY/MM/DD/HHMM.json)")
    p.add_argument("--days", type=int, required=True, help="keep snapshots from the last N days")
    p.add_argument("--out", required=True, help="index file to write (JSON array)")
    p.set_defaults(handler=_run_index)
