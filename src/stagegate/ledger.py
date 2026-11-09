"""Ledger (D5): the backend's record of what is live per repo x environment.

Every store uses the same layout (``{prefix}`` is always ``stagegate-ledger`` unless an
Azure URL overrides it)::

    {prefix}/{owner}/{name}/{env}/history/{YYYYMMDDTHHMMSSffffffZ}-{sha[:12]}.json
    {prefix}/{owner}/{name}/{env}/current.json

Writes append the history entry first, then overwrite ``current.json``. History file names
sort lexicographically, so ``history()`` lists newest first. Records carry unit names,
statuses and booleans only: never plan values.

Stores are chosen by URL (:func:`open_store`)::

    file:///abs/path
    git-branch:///abs/repo?branch=stagegate-ledger&remote=origin
    azure-blob://<account>.blob.core.windows.net/<container>?prefix=stagegate-ledger
    github-branch://<owner>/<repo>?branch=stagegate-ledger     (read-only, contents API)
"""

from __future__ import annotations

import argparse
import functools
import json
import os
import re
import subprocess
import sys
import tempfile
from collections.abc import Callable
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, Literal, Protocol, get_args
from urllib.parse import parse_qs, unquote, urlparse

from stagegate import cli
from stagegate.github import GitHub, GitHubError

DEFAULT_PREFIX = "stagegate-ledger"
DEFAULT_BRANCH = "stagegate-ledger"
PUSH_ATTEMPTS = 5
AUTHOR_NAME = "stagegate"
AUTHOR_EMAIL = "stagegate@users.noreply.github.com"

Status = Literal["applied", "no_changes", "failed", "skipped"]

_SEGMENT = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]*$")
_SHA = re.compile(r"^[0-9a-fA-F]{12,64}$")
_URL_CREDS = re.compile(r"(\w+://)[^/@\s]+@")
_TIMESTAMP = re.compile(r"^(\d{4})-(\d\d)-(\d\d)T(\d\d):(\d\d):(\d\d)\.(\d{6})Z$")


class LedgerError(Exception):
    """A ledger operation failed (bad input, bad store, backend error)."""


@dataclass(frozen=True)
class UnitResult:
    status: Status
    converged: bool


@dataclass(frozen=True)
class LedgerRecord:
    repo: str
    env: str
    sha: str
    run_id: int
    run_attempt: int
    units: dict[str, UnitResult]
    converged: bool
    approver: str | None
    breakglass_incident: str | None
    recorded_at: str  # RFC 3339 UTC, microsecond precision, e.g. 2026-11-06T12:00:00.000001Z

    def to_dict(self) -> dict[str, Any]:
        return {
            "repo": self.repo,
            "env": self.env,
            "sha": self.sha,
            "run_id": self.run_id,
            "run_attempt": self.run_attempt,
            "units": {
                n: {"status": u.status, "converged": u.converged}
                for n, u in sorted(self.units.items())
            },
            "converged": self.converged,
            "approver": self.approver,
            "breakglass_incident": self.breakglass_incident,
            "recorded_at": self.recorded_at,
        }

    @classmethod
    def from_dict(cls, d: Any) -> LedgerRecord:
        if not isinstance(d, dict):
            raise LedgerError("malformed ledger record (not an object)")
        try:
            return cls(
                repo=_str(d["repo"]),
                env=_str(d["env"]),
                sha=_str(d["sha"]),
                run_id=_int(d["run_id"]),
                run_attempt=_int(d["run_attempt"]),
                units=parse_units(d["units"]),
                converged=_bool(d["converged"]),
                approver=_opt_str(d["approver"]),
                breakglass_incident=_opt_str(d["breakglass_incident"]),
                recorded_at=_str(d["recorded_at"]),
            )
        except (KeyError, TypeError, ValueError) as exc:
            raise LedgerError(f"malformed ledger record ({type(exc).__name__})") from None


def _str(v: Any) -> str:
    if not isinstance(v, str):
        raise TypeError("expected string")
    return v


def _opt_str(v: Any) -> str | None:
    return None if v is None else _str(v)


def _int(v: Any) -> int:
    if isinstance(v, bool) or not isinstance(v, int):
        raise TypeError("expected int")
    return v


def _bool(v: Any) -> bool:
    if not isinstance(v, bool):
        raise TypeError("expected bool")
    return v


def parse_units(raw: Any) -> dict[str, UnitResult]:
    """Validate ``{unit: {"status": <Status>, "converged": bool}}`` strictly."""
    if not isinstance(raw, dict):
        raise TypeError("units must be an object")
    out: dict[str, UnitResult] = {}
    for name, val in raw.items():
        if not isinstance(val, dict) or set(val) != {"status", "converged"}:
            raise LedgerError(f"unit {name!r}: expected exactly status and converged")
        if val["status"] not in get_args(Status):
            raise LedgerError(f"unit {name!r}: invalid status {val['status']!r}")
        if not isinstance(val["converged"], bool):
            raise LedgerError(f"unit {name!r}: converged must be a boolean")
        out[name] = UnitResult(val["status"], val["converged"])
    return out


def now_rfc3339() -> str:
    return datetime.now(UTC).strftime("%Y-%m-%dT%H:%M:%S.%fZ")


def record_from_run(
    *,
    repo: str,
    env: str,
    sha: str,
    run_id: int,
    run_attempt: int,
    units: dict[str, UnitResult],
    approver: str | None = None,
    breakglass_incident: str | None = None,
    now: str | None = None,
) -> LedgerRecord:
    """Build a record. ``converged`` is true iff every unit converged (so an empty plan,
    all ``no_changes``, records the environment as in sync at ``sha``). ``units`` must not be
    empty: an empty plan is recorded as units with status ``no_changes``."""
    if not units:
        raise ValueError("units must not be empty (record no_changes units for an empty plan)")
    return LedgerRecord(
        repo=repo,
        env=env,
        sha=sha,
        run_id=run_id,
        run_attempt=run_attempt,
        units=dict(units),
        converged=all(u.converged for u in units.values()),
        approver=approver,
        breakglass_incident=breakglass_incident,
        recorded_at=now or now_rfc3339(),
    )


class LedgerStore(Protocol):
    def current(self, repo: str, env: str) -> LedgerRecord | None: ...
    def append(self, rec: LedgerRecord) -> None: ...
    def history(self, repo: str, env: str, limit: int = 50) -> list[LedgerRecord]: ...


# ---- layout ---------------------------------------------------------------------------


def _check_names(repo: str, env: str) -> None:
    parts = repo.split("/")
    if len(parts) != 2 or not all(_SEGMENT.match(p) for p in parts) or ".." in repo:
        raise LedgerError(f"invalid repo {repo!r}: expected owner/name")
    if not _SEGMENT.match(env) or ".." in env:
        raise LedgerError(f"invalid env {env!r}")


def _env_dir(prefix: str, repo: str, env: str) -> str:
    _check_names(repo, env)
    return f"{prefix}/{repo}/{env}"


def _history_name(rec: LedgerRecord) -> str:
    m = _TIMESTAMP.match(rec.recorded_at)
    if not m:
        raise LedgerError(f"invalid recorded_at {rec.recorded_at!r}")
    if not _SHA.match(rec.sha):
        raise LedgerError("invalid sha")
    y, mo, d, h, mi, s, us = m.groups()
    return f"{y}{mo}{d}T{h}{mi}{s}{us}Z-{rec.sha[:12]}.json"


def _newest(a: LedgerRecord | None, b: LedgerRecord | None) -> LedgerRecord | None:
    """The record with the later ``recorded_at`` (fixed-width, so strings compare in time order)."""
    if a is None or b is None:
        return a or b
    return b if b.recorded_at > a.recorded_at else a


def _supersedes(rec: LedgerRecord, existing: LedgerRecord | None) -> bool:
    """Should ``rec`` become current? Not if the existing current is strictly newer."""
    return existing is None or existing.recorded_at <= rec.recorded_at


def _redact(text: str) -> str:
    return _URL_CREDS.sub(r"\1***@", text)


def _encode(rec: LedgerRecord) -> bytes:
    return (json.dumps(rec.to_dict(), indent=2, sort_keys=True) + "\n").encode()


def _decode(data: bytes) -> LedgerRecord:
    try:
        return LedgerRecord.from_dict(json.loads(data))
    except ValueError:
        raise LedgerError("malformed ledger record (not JSON)") from None


def _paths(prefix: str, rec: LedgerRecord) -> tuple[str, str]:
    base = _env_dir(prefix, rec.repo, rec.env)
    return f"{base}/history/{_history_name(rec)}", f"{base}/current.json"


# ---- file store -----------------------------------------------------------------------


class FileStore:
    def __init__(self, root: Path) -> None:
        self.root = Path(root)

    def _write(self, path: Path, data: bytes) -> None:
        path.parent.mkdir(parents=True, exist_ok=True)
        tmp = path.with_name(path.name + ".tmp")
        tmp.write_bytes(data)
        os.replace(tmp, path)

    def append(self, rec: LedgerRecord) -> None:
        hist, cur = _paths(DEFAULT_PREFIX, rec)
        data = _encode(rec)
        self._write(self.root / hist, data)
        if _supersedes(rec, self._current_file(rec.repo, rec.env)):
            self._write(self.root / cur, data)

    def _current_file(self, repo: str, env: str) -> LedgerRecord | None:
        path = self.root / _env_dir(DEFAULT_PREFIX, repo, env) / "current.json"
        return _decode(path.read_bytes()) if path.is_file() else None

    def current(self, repo: str, env: str) -> LedgerRecord | None:
        # Newest of current.json and history[0]: heals a crash between the two writes.
        hist = self.history(repo, env, 1)
        return _newest(self._current_file(repo, env), hist[0] if hist else None)

    def history(self, repo: str, env: str, limit: int = 50) -> list[LedgerRecord]:
        d = self.root / _env_dir(DEFAULT_PREFIX, repo, env) / "history"
        if not d.is_dir():
            return []
        names = sorted((p.name for p in d.glob("*.json")), reverse=True)[:limit]
        return [_decode((d / n).read_bytes()) for n in names]


# ---- git branch store -----------------------------------------------------------------


class GitBranchStore:
    """Ledger on an orphan branch, written with git plumbing and a temporary index, so the
    caller's working tree, index and HEAD are never touched. With ``remote`` set, reads
    fetch first and ``append`` does fetch, commit, push (up to 5 tries on a race).

    Side effects on the caller's repo are limited to refs: it creates ``refs/heads/<branch>``
    and ``refs/stagegate/remote/*`` (the fetched remote tip), plus loose objects. It refuses to
    run if ``<branch>`` is checked out in ``repo_dir``."""

    def __init__(
        self, repo_dir: Path, branch: str = DEFAULT_BRANCH, remote: str | None = None
    ) -> None:
        self.repo_dir = Path(repo_dir)
        self.branch = branch
        self.remote = remote

    # plumbing helpers
    @property
    def _local_ref(self) -> str:
        return f"refs/heads/{self.branch}"

    @property
    def _remote_ref(self) -> str:
        return f"refs/stagegate/remote/{self.branch}"

    def _git(
        self,
        *args: str,
        env: dict[str, str] | None = None,
        input: bytes | None = None,
        ok: tuple[int, ...] = (0,),
    ) -> subprocess.CompletedProcess[bytes]:
        # LC_ALL/LANG=C: we match on git's English messages (push rejection, CAS mismatch).
        full_env = {
            **os.environ,
            "GIT_TERMINAL_PROMPT": "0",
            "LC_ALL": "C",
            "LANG": "C",
            **(env or {}),
        }
        try:
            proc = subprocess.run(
                ["git", "-C", str(self.repo_dir), *args],
                input=input,
                capture_output=True,
                env=full_env,
                check=False,
            )
        except OSError as exc:
            raise LedgerError(f"git unavailable: {type(exc).__name__}") from None
        if proc.returncode not in ok:
            err = proc.stderr.decode(errors="replace").strip().splitlines()
            msg = _redact(err[-1] if err else "no output")[-200:]
            raise LedgerError(f"git {args[0]} failed: {msg}")
        return proc

    def _resolve(self, ref: str) -> str | None:
        p = self._git("rev-parse", "--verify", "--quiet", ref, ok=(0, 1))
        return p.stdout.decode().strip() if p.returncode == 0 else None

    def _check_not_checked_out(self) -> None:
        head = self._git("symbolic-ref", "-q", "HEAD", ok=(0, 1)).stdout.decode().strip()
        if head == self._local_ref:
            raise LedgerError(f"ledger branch {self.branch!r} is checked out in {self.repo_dir}")

    def _fetch(self) -> None:
        self._check_not_checked_out()
        if not self.remote:
            return
        listed = self._git("ls-remote", "--heads", self.remote, self._local_ref).stdout
        if not listed.strip():
            if self._resolve(self._remote_ref):
                self._git("update-ref", "-d", self._remote_ref)
            return
        self._git("fetch", "--quiet", self.remote, f"+{self._local_ref}:{self._remote_ref}")

    def _zero_oid(self) -> str:
        fmt = self._git("rev-parse", "--show-object-format").stdout.decode().strip()
        return "0" * (64 if fmt == "sha256" else 40)

    def _tip(self) -> str | None:
        return (self._resolve(self._remote_ref) if self.remote else None) or self._resolve(
            self._local_ref
        )

    def _blob(self, tip: str | None, path: str) -> bytes | None:
        if tip is None:
            return None
        if self._resolve(f"{tip}:{path}") is None:
            return None
        return self._git("cat-file", "blob", f"{tip}:{path}").stdout

    # reads
    def _history_at(self, tip: str | None, d: str, limit: int) -> list[LedgerRecord]:
        if tip is None or self._resolve(f"{tip}:{d}") is None:
            return []
        names = self._git("ls-tree", "--name-only", f"{tip}:{d}").stdout.decode().splitlines()
        out = []
        for n in sorted(names, reverse=True)[:limit]:
            data = self._blob(tip, f"{d}/{n}")
            if data is not None:
                out.append(_decode(data))
        return out

    def _current_at(self, tip: str | None, base: str) -> LedgerRecord | None:
        data = self._blob(tip, f"{base}/current.json")
        return None if data is None else _decode(data)

    def current(self, repo: str, env: str) -> LedgerRecord | None:
        base = _env_dir(DEFAULT_PREFIX, repo, env)
        self._fetch()
        tip = self._tip()
        hist = self._history_at(tip, f"{base}/history", 1)
        return _newest(self._current_at(tip, base), hist[0] if hist else None)

    def history(self, repo: str, env: str, limit: int = 50) -> list[LedgerRecord]:
        d = f"{_env_dir(DEFAULT_PREFIX, repo, env)}/history"
        self._fetch()
        return self._history_at(self._tip(), d, limit)

    # write
    def _commit(self, base: str | None, rec: LedgerRecord) -> str:
        hist, cur = _paths(DEFAULT_PREFIX, rec)
        data = _encode(rec)
        with tempfile.TemporaryDirectory(prefix="stagegate-idx-") as tmp:
            env = {"GIT_INDEX_FILE": str(Path(tmp) / "index")}
            if base:
                self._git("read-tree", base, env=env)
            # Keep current monotonic: only replace it if no newer record is already there.
            existing = self._current_at(base, _env_dir(DEFAULT_PREFIX, rec.repo, rec.env))
            paths = (hist, cur) if _supersedes(rec, existing) else (hist,)
            for path in paths:
                oid = self._git("hash-object", "-w", "--stdin", input=data).stdout.decode().strip()
                self._git("update-index", "--add", "--cacheinfo", f"100644,{oid},{path}", env=env)
            tree = self._git("write-tree", env=env).stdout.decode().strip()
        ident = {
            "GIT_AUTHOR_NAME": AUTHOR_NAME,
            "GIT_AUTHOR_EMAIL": AUTHOR_EMAIL,
            "GIT_COMMITTER_NAME": AUTHOR_NAME,
            "GIT_COMMITTER_EMAIL": AUTHOR_EMAIL,
        }
        msg = f"ledger: {rec.repo} {rec.env} {rec.sha[:12]}"
        parents = ["-p", base] if base else []
        return (
            self._git("commit-tree", tree, *parents, "-m", msg, env=ident).stdout.decode().strip()
        )

    def append(self, rec: LedgerRecord) -> None:
        _paths(DEFAULT_PREFIX, rec)  # validate before touching git
        last = "no attempt"
        for _ in range(PUSH_ATTEMPTS):
            self._fetch()
            base = self._tip()
            new = self._commit(base, rec)
            if self.remote:
                push = self._git(
                    "push",
                    "--quiet",
                    "--no-verify",
                    self.remote,
                    f"{new}:{self._local_ref}",
                    ok=(0, 1),
                )
                if push.returncode != 0:
                    err = push.stderr.decode(errors="replace")
                    if not re.search(r"rejected|non-fast-forward|fetch first", err):
                        raise LedgerError("git push failed: " + _redact(err.strip())[-200:])
                    last = "non-fast-forward"
                    continue
                self._git("update-ref", self._local_ref, new)
                self._git("update-ref", self._remote_ref, new)
                return
            local_old = base or self._zero_oid()  # CAS against what we built on, not a re-read
            cas = self._git("update-ref", self._local_ref, new, local_old, ok=(0, 128))
            if cas.returncode == 0:
                return
            if b"but expected" not in cas.stderr and b"already exists" not in cas.stderr:
                raise LedgerError("git update-ref failed: " + _redact(cas.stderr.decode()[-200:]))
            last = "concurrent local update"
        raise LedgerError(f"ledger append failed after {PUSH_ATTEMPTS} attempts ({last})")


# ---- azure blob store -----------------------------------------------------------------


class AzureBlobStore:
    """Ledger in a blob container. Needs the ``stagegate[azure]`` extra; auth is
    ``DefaultAzureCredential`` (OIDC in CI). Both blobs carry index tag ``stagegate-sha``."""

    def __init__(self, account_url: str, container: str, prefix: str = DEFAULT_PREFIX) -> None:
        self.account_url = account_url
        self.container = container
        self.prefix = prefix
        self._client: Any = None
        self._not_found: type[Exception] | None = None

    def _cc(self) -> Any:
        if self._client is None:
            try:
                from azure.core.exceptions import ResourceNotFoundError
                from azure.identity import DefaultAzureCredential
                from azure.storage.blob import BlobServiceClient
            except ImportError:
                raise LedgerError(
                    "the Azure Blob ledger store needs the optional extra: "
                    "pip install 'stagegate[azure]'"
                ) from None
            svc = BlobServiceClient(self.account_url, credential=DefaultAzureCredential())
            self._client = svc.get_container_client(self.container)
            self._not_found = ResourceNotFoundError
        return self._client

    def _read(self, name: str) -> bytes | None:
        cc = self._cc()
        assert self._not_found is not None
        try:
            return cc.download_blob(name).readall()
        except self._not_found:
            return None

    def append(self, rec: LedgerRecord) -> None:
        hist, cur = _paths(self.prefix, rec)
        cc = self._cc()
        data = _encode(rec)
        tags = {"stagegate-sha": rec.sha}
        existing = self._read(hist)
        if existing is None:
            cc.upload_blob(hist, data, overwrite=False, tags=tags)
        elif existing != data:  # a retry re-sends identical bytes; anything else is a conflict
            raise LedgerError("history entry already exists with different content")
        if _supersedes(rec, self._current_blob(rec.repo, rec.env)):
            cc.upload_blob(cur, data, overwrite=True, tags=tags)

    def _current_blob(self, repo: str, env: str) -> LedgerRecord | None:
        data = self._read(f"{_env_dir(self.prefix, repo, env)}/current.json")
        return None if data is None else _decode(data)

    def current(self, repo: str, env: str) -> LedgerRecord | None:
        # Newest of current.json and history[0]: heals a crash between the two writes.
        hist = self.history(repo, env, 1)
        return _newest(self._current_blob(repo, env), hist[0] if hist else None)

    def history(self, repo: str, env: str, limit: int = 50) -> list[LedgerRecord]:
        d = f"{_env_dir(self.prefix, repo, env)}/history/"
        names = sorted((b.name for b in self._cc().list_blobs(name_starts_with=d)), reverse=True)
        out = []
        for n in names[:limit]:
            data = self._read(n)
            if data is not None:
                out.append(_decode(data))
        return out


# ---- github branch store (read-only) ----------------------------------------------------


class GitHubBranchStore:
    """Read-only view of a ledger branch through the contents API (no clone), for the cockpit.
    ``append`` always raises: writers use the git-branch store."""

    def __init__(self, gh: GitHub, repo: str, branch: str = DEFAULT_BRANCH) -> None:
        self.gh = gh
        self.repo = repo
        self.branch = branch

    def _file(self, path: str) -> LedgerRecord | None:
        try:
            text = self.gh.get_file(self.repo, path, self.branch)
        except GitHubError as exc:
            raise LedgerError(f"ledger read failed: {exc}") from None
        try:
            return None if text is None else _decode(text.encode())
        except ValueError:
            raise LedgerError("malformed ledger record") from None

    def current(self, repo: str, env: str) -> LedgerRecord | None:
        base = _env_dir(DEFAULT_PREFIX, repo, env)
        hist = self.history(repo, env, 1)
        return _newest(self._file(f"{base}/current.json"), hist[0] if hist else None)

    def history(self, repo: str, env: str, limit: int = 50) -> list[LedgerRecord]:
        d = f"{_env_dir(DEFAULT_PREFIX, repo, env)}/history"
        try:
            names = self.gh.list_dir(self.repo, d, self.branch)
        except GitHubError as exc:
            raise LedgerError(f"ledger read failed: {exc}") from None
        out = []
        for n in sorted((n for n in names if n.endswith(".json")), reverse=True)[:limit]:
            rec = self._file(f"{d}/{n}")
            if rec is not None:
                out.append(rec)
        return out

    def append(self, rec: LedgerRecord) -> None:
        raise LedgerError("read-only store: github-branch cannot be written")


# ---- store selection ------------------------------------------------------------------


def open_store(url: str, gh: GitHub | None = None) -> LedgerStore:
    """Open the store for ``url``. ``gh`` is only used by github-branch (default: from env)."""
    u = urlparse(url)
    q = {k: v[-1] for k, v in parse_qs(u.query).items()}
    if u.scheme == "file":
        if u.netloc or not u.path.startswith("/"):
            raise LedgerError("file store URL must be file:///abs/path")
        return FileStore(Path(unquote(u.path)))
    if u.scheme == "git-branch":
        if u.netloc or not u.path.startswith("/"):
            raise LedgerError("git-branch store URL must be git-branch:///abs/repo/path")
        return GitBranchStore(
            Path(unquote(u.path)), branch=q.get("branch") or DEFAULT_BRANCH, remote=q.get("remote")
        )
    if u.scheme == "azure-blob":
        container = u.path.strip("/")
        if not u.netloc or not container or "/" in container:
            raise LedgerError(
                "azure-blob URL must be azure-blob://<account>.blob.core.windows.net/<container>"
            )
        return AzureBlobStore(
            f"https://{u.netloc}", container, prefix=q.get("prefix") or DEFAULT_PREFIX
        )
    if u.scheme == "github-branch":
        repo = f"{u.netloc}/{u.path.strip('/')}"
        if not u.netloc or u.path.strip("/").count("/") or not u.path.strip("/"):
            raise LedgerError("github-branch URL must be github-branch://owner/repo")
        try:
            client = gh or GitHub.from_env()
        except GitHubError as exc:
            raise LedgerError(str(exc)) from None
        return GitHubBranchStore(client, repo, branch=q.get("branch") or DEFAULT_BRANCH)
    raise LedgerError(
        "unknown ledger store URL scheme (file, git-branch, azure-blob, github-branch)"
    )


# ---- CLI ------------------------------------------------------------------------------


def _load_units(path: str) -> dict[str, UnitResult]:
    try:
        units = parse_units(json.loads(Path(path).read_text()))
        if not units:
            raise LedgerError("--units-json is empty (an empty plan is recorded as no_changes)")
        return units
    except TypeError as exc:
        raise LedgerError(f"--units-json: {exc}") from None
    except ValueError:
        raise LedgerError("--units-json is not valid JSON") from None


def _write(args: argparse.Namespace) -> int:
    if args.mirror_deployments and not args.allow_ref:
        if os.environ.get("GITHUB_REF") != "refs/heads/main":
            raise LedgerError("--mirror-deployments is only allowed from refs/heads/main (D5)")
    rec = record_from_run(
        repo=args.repo,
        env=args.env,
        sha=args.sha,
        run_id=args.run_id,
        run_attempt=args.run_attempt,
        units=_load_units(args.units_json),
        approver=args.approver,
        breakglass_incident=args.breakglass_incident,
    )
    open_store(args.store).append(rec)
    if args.mirror_deployments:
        server = os.environ.get("GITHUB_SERVER_URL") or "https://github.com"
        gh = None
        try:
            gh = GitHub.from_env()
            # ref = the commit sha (not the branch tip); payload.branch = "main". See github.py.
            dep = gh.create_deployment(rec.repo, rec.sha, rec.sha, rec.env, rec.env == "prod")
            gh.create_deployment_status(
                rec.repo,
                dep,
                "success" if rec.converged else "failure",
                f"{server}/{rec.repo}/actions/runs/{rec.run_id}",
            )
        except GitHubError as exc:
            raise LedgerError(f"ledger written, but deployment mirror failed: {exc}") from None
        finally:
            if gh is not None:
                gh.close()
    return 0


def _show(args: argparse.Namespace) -> int:
    store = open_store(args.store)
    if args.history is not None:
        out: Any = [r.to_dict() for r in store.history(args.repo, args.env, args.history)]
    else:
        rec = store.current(args.repo, args.env)
        out = None if rec is None else rec.to_dict()
    print(json.dumps(out, indent=2, sort_keys=True))
    return 0


def _guarded(fn: Callable[[argparse.Namespace], int]) -> Callable[[argparse.Namespace], int]:
    @functools.wraps(fn)
    def run(args: argparse.Namespace) -> int:
        try:
            return fn(args)
        except (LedgerError, GitHubError, OSError) as exc:
            print(f"stagegate: ledger failed: {exc}", file=sys.stderr)
            return 1
        except Exception as exc:  # fail closed; never echo an arbitrary message
            print(f"stagegate: ledger failed: unexpected {type(exc).__name__}", file=sys.stderr)
            return 1

    return run


def _positive(text: str) -> int:
    n = int(text)
    if n < 1:
        raise argparse.ArgumentTypeError("must be >= 1")
    return n


def _setup(parser: argparse.ArgumentParser) -> None:
    sub = parser.add_subparsers(dest="ledger_command", required=True, metavar="<action>")

    p = sub.add_parser("write", help="record a run in the ledger")
    p.add_argument(
        "--store", required=True, help="store URL (file://, git-branch://, azure-blob://)"
    )
    p.add_argument("--repo", required=True, help="owner/name")
    p.add_argument("--env", required=True)
    p.add_argument("--sha", required=True)
    p.add_argument("--run-id", required=True, type=int)
    p.add_argument("--run-attempt", required=True, type=int)
    p.add_argument(
        "--units-json", required=True, help='{"unit": {"status": ..., "converged": bool}}'
    )
    p.add_argument("--approver")
    p.add_argument("--breakglass-incident")
    p.add_argument(
        "--mirror-deployments", action="store_true", help="also post a GitHub deployment"
    )
    p.add_argument(
        "--allow-ref", action="store_true", help="skip the refs/heads/main check (tests)"
    )
    p.set_defaults(handler=_guarded(_write))

    p = sub.add_parser("show", help="print the current record, or recent history, as JSON")
    p.add_argument("--store", required=True)
    p.add_argument("--repo", required=True)
    p.add_argument("--env", required=True)
    p.add_argument("--history", type=_positive, metavar="N")
    p.set_defaults(handler=_guarded(_show))


cli.register("ledger", _setup)
