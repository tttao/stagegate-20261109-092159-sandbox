"""Minimal GitHub REST client (R4). Later tasks extend this class; keep it small.

Errors raise :class:`GitHubError` carrying the HTTP status and GitHub's own
``message`` field. The token is never part of any message.
"""

from __future__ import annotations

import base64
import binascii
import json
import os
from pathlib import Path
from typing import Any
from urllib.parse import urlparse

import httpx

DEFAULT_API_URL = "https://api.github.com"
_API_VERSION = "2022-11-28"
MAX_PAGES = 50
ETAG_MAX_ENTRIES = 2000
ETAG_MAX_BYTES = 20 * 1024 * 1024


class GitHubError(Exception):
    """A GitHub API call failed. ``status`` is 0 for transport/config errors."""

    def __init__(self, status: int, message: str) -> None:
        self.status = status
        self.message = message
        super().__init__(f"github: HTTP {status}: {message}" if status else f"github: {message}")


class GitHub:
    def __init__(self, base_url: str, token: str, *, timeout: float = 30.0) -> None:
        url = urlparse(base_url)
        if url.scheme != "https" and not (
            url.scheme == "http" and url.hostname in ("localhost", "127.0.0.1")
        ):
            raise GitHubError(0, "base_url must be https (http only for localhost/127.0.0.1)")
        self._etags: dict[str, dict[str, Any]] | None = None
        self._client = httpx.Client(
            base_url=base_url.rstrip("/"),
            timeout=timeout,
            headers={
                "Authorization": f"Bearer {token}",
                "Accept": "application/vnd.github+json",
                "X-GitHub-Api-Version": _API_VERSION,
            },
        )

    @classmethod
    def from_env(cls) -> GitHub:
        token = os.environ.get("GITHUB_TOKEN")
        if not token:
            raise GitHubError(0, "GITHUB_TOKEN is not set")
        return cls(os.environ.get("GITHUB_API_URL") or DEFAULT_API_URL, token)

    def close(self) -> None:
        self._client.close()

    @property
    def web_url(self) -> str:
        """Browser origin matching the API URL (github.com, or the GHES host without /api/v3)."""
        base = self._client.base_url
        if base.host == "api.github.com":
            return "https://github.com"
        path = base.path.rstrip("/").removesuffix("/api/v3")
        return f"{base.scheme}://{base.netloc.decode()}{path}"

    @property
    def api_host(self) -> str:
        """Lower-case hostname of the API base URL."""
        return (self._client.base_url.host or "").lower()

    def meta(self) -> dict[str, Any]:
        """``GET /meta``: instance metadata (``installed_version`` is present on GHES only)."""
        data = self._get("/meta", "meta")
        if not isinstance(data, dict):
            raise GitHubError(0, "unexpected meta response")
        return data

    def enable_etag_cache(self, path: Path | None = None) -> None:
        """Send ``If-None-Match`` on GETs and reuse the cached body on 304. With ``path`` the
        cache is loaded from that JSON file (a missing or unreadable file is an empty cache).

        Only GET 200 JSON responses that carry an ETag are kept, as
        ``url -> {etag, status, headers: {link, content-type}, body}``; request headers
        (Authorization) are never stored. At most ``ETAG_MAX_ENTRIES`` entries and about
        ``ETAG_MAX_BYTES`` of bodies; the oldest are evicted first."""
        if self._etags is not None:
            return
        self._etags = {}
        if path is None:
            return
        try:
            raw = json.loads(Path(path).read_text())
        except (OSError, ValueError):
            return
        if not isinstance(raw, dict):
            return
        for url, e in raw.items():
            if (
                isinstance(url, str)
                and isinstance(e, dict)
                and isinstance(e.get("etag"), str)
                and isinstance(e.get("body"), str)
                and isinstance(e.get("headers"), dict)
                and e.get("status") == 200
            ):
                self._etags[url] = {k: e[k] for k in ("etag", "status", "headers", "body")}
        self._trim_etags()

    def save_etag_cache(self, path: Path) -> None:
        if self._etags is None:
            return
        self._trim_etags()
        tmp = Path(str(path) + ".tmp")
        tmp.write_text(json.dumps(self._etags))
        tmp.replace(path)

    def _trim_etags(self) -> None:
        cache = self._etags
        if cache is None:
            return
        size = sum(len(e["body"]) for e in cache.values())
        while cache and (len(cache) > ETAG_MAX_ENTRIES or size > ETAG_MAX_BYTES):
            oldest = next(iter(cache))
            size -= len(cache.pop(oldest)["body"])

    def _request(self, method: str, url: str, **kwargs: Any) -> httpx.Response:
        key: str | None = None
        if self._etags is not None and method == "GET" and not kwargs.get("follow_redirects"):
            key = str(self._client.build_request("GET", url, params=kwargs.get("params")).url)
            if key in self._etags:
                kwargs["headers"] = {
                    **kwargs.get("headers", {}),
                    "If-None-Match": self._etags[key]["etag"],
                }
        try:
            resp = self._client.request(method, url, **kwargs)
        except httpx.HTTPError as exc:
            raise GitHubError(0, f"request failed: {type(exc).__name__}") from None
        if key is not None and self._etags is not None:
            if resp.status_code == 304 and key in self._etags:
                entry = self._etags[key]
                self._etags[key] = self._etags.pop(key)  # recently used: evicted last
                return httpx.Response(
                    entry["status"],
                    headers=entry["headers"],
                    content=entry["body"].encode(),
                    request=resp.request,
                )
            etag = resp.headers.get("etag")
            ctype = resp.headers.get("content-type", "")
            if resp.status_code == 200 and etag and "json" in ctype:
                self._etags.pop(key, None)
                self._etags[key] = {
                    "etag": etag,
                    "status": 200,
                    "headers": {
                        h: resp.headers[h] for h in ("link", "content-type") if h in resp.headers
                    },
                    "body": resp.text,
                }
                self._trim_etags()
        if not 200 <= resp.status_code < 300:
            message = ""
            try:
                data = resp.json()
                if isinstance(data, dict):
                    message = str(data.get("message", ""))
            except ValueError:
                pass
            raise GitHubError(resp.status_code, message[:300] or resp.reason_phrase)
        return resp

    def _post(self, path: str, body: dict[str, Any]) -> Any:
        resp = self._request("POST", path, json=body)
        try:
            return resp.json()
        except ValueError:
            return {}

    def _paginate(
        self, path: str, params: dict[str, Any], what: str, key: str | None = None
    ) -> list[dict[str, Any]]:
        """All objects of a list endpoint; follows ``Link: rel=next`` on the same host only.
        With ``key`` the page is an object whose ``key`` member holds the list."""
        out: list[dict[str, Any]] = []
        url: str | None = path
        query: dict[str, Any] | None = params
        base = self._client.base_url
        for _ in range(MAX_PAGES):
            if not url:
                return out
            resp = self._request("GET", url, params=query)
            try:
                page = resp.json()
            except ValueError:
                raise GitHubError(resp.status_code, "response is not JSON") from None
            if key is not None and isinstance(page, dict):
                page = page.get(key)
            if not isinstance(page, list):
                raise GitHubError(resp.status_code, f"unexpected {what} response")
            out.extend(i for i in page if isinstance(i, dict))
            url = resp.links.get("next", {}).get("url")
            if url:
                nxt = httpx.URL(url)
                if (nxt.scheme, nxt.host, nxt.port) != (base.scheme, base.host, base.port):
                    raise GitHubError(0, "pagination link points to a different host")
            query = None  # the next URL already carries the query
        if url:
            raise GitHubError(0, f"more than {MAX_PAGES} pages of {what}")
        return out

    def list_issues(
        self, repo: str, labels: list[str], state: str = "open"
    ) -> list[dict[str, Any]]:
        """Issues carrying all ``labels`` (pull requests excluded); follows ``Link: rel=next``."""
        items = self._paginate(
            f"/repos/{repo}/issues",
            {"labels": ",".join(labels), "state": state, "per_page": 100},
            "issues",
        )
        return [i for i in items if "pull_request" not in i]

    def list_issue_comments(self, repo: str, number: int) -> list[dict[str, Any]]:
        """Every comment of an issue or pull request, oldest first."""
        return self._paginate(
            f"/repos/{repo}/issues/{number}/comments", {"per_page": 100}, "comments"
        )

    def create_issue_comment(self, repo: str, number: int, body: str) -> int:
        data = self._post(f"/repos/{repo}/issues/{number}/comments", {"body": body})
        comment_id = data.get("id") if isinstance(data, dict) else None
        if not isinstance(comment_id, int):
            raise GitHubError(0, "no comment created")
        return comment_id

    def update_issue_comment(self, repo: str, comment_id: int, body: str) -> None:
        self._request("PATCH", f"/repos/{repo}/issues/comments/{comment_id}", json={"body": body})

    def create_deployment(
        self, repo: str, ref: str, sha: str, environment: str, production: bool
    ) -> int:
        """Create a deployment record and return its id.

        Choice: the API deploys ``ref``; a branch name would resolve to the branch tip, which
        may be newer than the commit that was applied. So callers pass ``ref`` = the commit
        sha, and ``payload`` records the sha and ``branch = "main"`` (D5: ref=main only).
        ``ref`` is accepted separately so that choice stays visible at the call site.
        """
        data = self._post(
            f"/repos/{repo}/deployments",
            {
                "ref": ref,
                "environment": environment,
                "auto_merge": False,
                "required_contexts": [],
                "production_environment": production,
                "payload": {"sha": sha, "branch": "main"},
            },
        )
        dep_id = data.get("id") if isinstance(data, dict) else None
        if not isinstance(dep_id, int):
            detail = str(data.get("message", "no deployment created"))[:300]
            raise GitHubError(202, detail)
        return dep_id

    def create_deployment_status(
        self, repo: str, deployment_id: int, state: str, log_url: str
    ) -> None:
        self._post(
            f"/repos/{repo}/deployments/{deployment_id}/statuses",
            {"state": state, "log_url": log_url},
        )

    def run_approvals(self, repo: str, run_id: int) -> list[dict[str, Any]]:
        """Approval and rejection reviews on a workflow run's environment gates."""
        resp = self._request("GET", f"/repos/{repo}/actions/runs/{run_id}/approvals")
        try:
            data = resp.json()
        except ValueError:
            raise GitHubError(resp.status_code, "response is not JSON") from None
        if not isinstance(data, list):
            raise GitHubError(resp.status_code, "unexpected approvals response")
        return [a for a in data if isinstance(a, dict)]

    def create_issue(self, repo: str, title: str, body: str, labels: list[str]) -> int:
        data = self._post(f"/repos/{repo}/issues", {"title": title, "body": body, "labels": labels})
        number = data.get("number") if isinstance(data, dict) else None
        if not isinstance(number, int):
            raise GitHubError(0, "no issue created")
        return number

    def update_issue(
        self,
        repo: str,
        number: int,
        state: str | None = None,
        labels: list[str] | None = None,
        body: str | None = None,
    ) -> None:
        """PATCH an issue; only the fields given are sent (``state`` is open or closed)."""
        fields: dict[str, Any] = {}
        if body is not None:
            fields["body"] = body
        if state is not None:
            fields["state"] = state
        if labels is not None:
            fields["labels"] = labels
        self._request("PATCH", f"/repos/{repo}/issues/{number}", json=fields)

    def get_issue(self, repo: str, number: int) -> dict[str, Any]:
        resp = self._request("GET", f"/repos/{repo}/issues/{number}")
        try:
            data = resp.json()
        except ValueError:
            raise GitHubError(resp.status_code, "response is not JSON") from None
        if not isinstance(data, dict):
            raise GitHubError(resp.status_code, "unexpected issue response")
        return data

    def open_or_comment_issue(self, repo: str, title: str, body: str, labels: list[str]) -> int:
        """Comment on the open issue carrying all ``labels``, or create one. Returns its number."""
        existing = self.list_issues(repo, labels)
        if existing:
            number = existing[0].get("number")
            if not isinstance(number, int):
                raise GitHubError(0, "issue without a number")
            self._post(f"/repos/{repo}/issues/{number}/comments", {"body": body})
            return number
        return self.create_issue(repo, title, body, labels)

    def list_code_scanning_alerts(
        self, repo: str, state: str = "open", severity: str | None = None
    ) -> list[dict[str, Any]]:
        """Code-scanning alerts of the default branch; ``severity`` is a single value."""
        params: dict[str, Any] = {"state": state, "per_page": 100}
        if severity is not None:
            params["severity"] = severity
        return self._paginate(f"/repos/{repo}/code-scanning/alerts", params, "alerts")

    # ---- sandbox driver support (e2e): git data, pull requests, runs, deployment reviews -----

    def _get(self, path: str, what: str, **params: Any) -> Any:
        resp = self._request("GET", path, params=params or None)
        try:
            return resp.json()
        except ValueError:
            raise GitHubError(resp.status_code, f"{what}: response is not JSON") from None

    def current_login(self) -> str:
        """Login of the authenticated user (GitHub App installation tokens get HTTP 403)."""
        data = self._get("/user", "user")
        login = data.get("login") if isinstance(data, dict) else None
        if not isinstance(login, str) or not login:
            raise GitHubError(0, "unexpected user response")
        return login

    def get_ref_sha(self, repo: str, branch: str) -> str | None:
        """Commit SHA at the tip of ``branch``, or None when the branch does not exist."""
        try:
            data = self._get(f"/repos/{repo}/git/ref/heads/{branch}", "ref")
        except GitHubError as exc:
            if exc.status == 404:
                return None
            raise
        sha = data.get("object", {}).get("sha") if isinstance(data, dict) else None
        if not isinstance(sha, str):
            raise GitHubError(0, "unexpected ref response")
        return sha

    def commit_tree_sha(self, repo: str, commit_sha: str) -> str:
        data = self._get(f"/repos/{repo}/git/commits/{commit_sha}", "commit")
        sha = data.get("tree", {}).get("sha") if isinstance(data, dict) else None
        if not isinstance(sha, str):
            raise GitHubError(0, "unexpected commit response")
        return sha

    def create_tree(self, repo: str, base_tree: str | None, files: dict[str, str | None]) -> str:
        """New tree from text ``files`` (``None`` deletes the path) on top of ``base_tree``."""
        entries: list[dict[str, Any]] = [
            {"path": p, "mode": "100644", "type": "blob", "content": c}
            if c is not None
            else {"path": p, "mode": "100644", "type": "blob", "sha": None}
            for p, c in files.items()
        ]
        body: dict[str, Any] = {"tree": entries}
        if base_tree:
            body["base_tree"] = base_tree
        return self._sha_of(self._post(f"/repos/{repo}/git/trees", body), "tree")

    def create_commit(self, repo: str, message: str, tree: str, parents: list[str]) -> str:
        data = self._post(
            f"/repos/{repo}/git/commits", {"message": message, "tree": tree, "parents": parents}
        )
        return self._sha_of(data, "commit")

    @staticmethod
    def _sha_of(data: Any, what: str) -> str:
        sha = data.get("sha") if isinstance(data, dict) else None
        if not isinstance(sha, str):
            raise GitHubError(0, f"no {what} created")
        return sha

    def create_branch(self, repo: str, branch: str, sha: str) -> None:
        self._post(f"/repos/{repo}/git/refs", {"ref": f"refs/heads/{branch}", "sha": sha})

    def update_branch(self, repo: str, branch: str, sha: str, force: bool = False) -> None:
        self._request(
            "PATCH", f"/repos/{repo}/git/refs/heads/{branch}", json={"sha": sha, "force": force}
        )

    def create_pull_request(self, repo: str, head: str, base: str, title: str) -> int:
        data = self._post(
            f"/repos/{repo}/pulls", {"head": head, "base": base, "title": title, "body": ""}
        )
        number = data.get("number") if isinstance(data, dict) else None
        if not isinstance(number, int):
            raise GitHubError(0, "no pull request created")
        return number

    def close_pull_request(self, repo: str, number: int) -> None:
        self._request("PATCH", f"/repos/{repo}/pulls/{number}", json={"state": "closed"})

    def find_pull_request(self, repo: str, head_branch: str, state: str = "open") -> int | None:
        owner = repo.split("/")[0]
        items = self._paginate(
            f"/repos/{repo}/pulls",
            {"head": f"{owner}:{head_branch}", "state": state, "per_page": 100},
            "pull requests",
        )
        number = items[0].get("number") if items else None
        return number if isinstance(number, int) else None

    def merge_pull_request(self, repo: str, number: int, method: str = "squash") -> str:
        """Merge and return the merge commit SHA (the commit that lands on the base branch)."""
        resp = self._request(
            "PUT", f"/repos/{repo}/pulls/{number}/merge", json={"merge_method": method}
        )
        try:
            data = resp.json()
        except ValueError:
            data = None
        sha = data.get("sha") if isinstance(data, dict) else None
        if not isinstance(sha, str):
            raise GitHubError(resp.status_code, "merge returned no sha")
        return sha

    def get_file(self, repo: str, path: str, ref: str) -> str | None:
        """Text content of ``path`` at ``ref``; None when it does not exist."""
        try:
            data = self._get(f"/repos/{repo}/contents/{path}", "contents", ref=ref)
        except GitHubError as exc:
            if exc.status == 404:
                return None
            raise
        content = data.get("content") if isinstance(data, dict) else None
        if not isinstance(content, str) or data.get("encoding") != "base64":
            raise GitHubError(0, f"{path} is not a file")
        try:
            return base64.b64decode("".join(content.split()), validate=True).decode()
        except (binascii.Error, UnicodeDecodeError):
            raise GitHubError(0, f"{path} is not valid base64 text") from None

    def list_dir(self, repo: str, path: str, ref: str) -> list[str]:
        """Entry names of a directory at ``ref`` (empty when it does not exist)."""
        try:
            data = self._get(f"/repos/{repo}/contents/{path}", "contents", ref=ref)
        except GitHubError as exc:
            if exc.status == 404:
                return []
            raise
        if not isinstance(data, list):
            raise GitHubError(0, f"{path} is not a directory")
        return [str(e["name"]) for e in data if isinstance(e, dict) and "name" in e]

    def list_workflow_runs(
        self, repo: str, workflow: str, head_sha: str | None = None, event: str | None = None
    ) -> list[dict[str, Any]]:
        params: dict[str, Any] = {"per_page": 100}
        if head_sha:
            params["head_sha"] = head_sha
        if event:
            params["event"] = event
        data = self._get(f"/repos/{repo}/actions/workflows/{workflow}/runs", "runs", **params)
        runs = data.get("workflow_runs") if isinstance(data, dict) else None
        if not isinstance(runs, list):
            raise GitHubError(0, "unexpected workflow runs response")
        return [r for r in runs if isinstance(r, dict)]

    def get_run(self, repo: str, run_id: int) -> dict[str, Any]:
        data = self._get(f"/repos/{repo}/actions/runs/{run_id}", "run")
        if not isinstance(data, dict):
            raise GitHubError(0, "unexpected run response")
        return data

    def list_run_jobs(self, repo: str, run_id: int, latest: bool = False) -> list[dict[str, Any]]:
        """Jobs of a run; with ``latest`` only those of the newest attempt (``filter=latest``)."""
        params: dict[str, Any] = {"per_page": 100}
        if latest:
            params["filter"] = "latest"
        data = self._get(f"/repos/{repo}/actions/runs/{run_id}/jobs", "jobs", **params)
        jobs = data.get("jobs") if isinstance(data, dict) else None
        if not isinstance(jobs, list):
            raise GitHubError(0, "unexpected jobs response")
        return [j for j in jobs if isinstance(j, dict)]

    def job_log(self, repo: str, job_id: int) -> str:
        """Plain-text log of a job (GitHub answers 302 to a download URL; we follow it)."""
        resp = self._request(
            "GET", f"/repos/{repo}/actions/jobs/{job_id}/logs", follow_redirects=True
        )
        return resp.text

    def dispatch_workflow(
        self, repo: str, workflow: str, ref: str, inputs: dict[str, str] | None = None
    ) -> None:
        self._post(
            f"/repos/{repo}/actions/workflows/{workflow}/dispatches",
            {"ref": ref, "inputs": inputs or {}},
        )

    def pending_deployments(self, repo: str, run_id: int) -> list[dict[str, Any]]:
        data = self._get(f"/repos/{repo}/actions/runs/{run_id}/pending_deployments", "pending")
        if not isinstance(data, list):
            raise GitHubError(0, "unexpected pending deployments response")
        return [d for d in data if isinstance(d, dict)]

    def review_pending_deployments(
        self, repo: str, run_id: int, env_ids: list[int], approve: bool, comment: str
    ) -> None:
        self._post(
            f"/repos/{repo}/actions/runs/{run_id}/pending_deployments",
            {
                "environment_ids": env_ids,
                "state": "approved" if approve else "rejected",
                "comment": comment,
            },
        )

    # ---- cockpit collector (read-only) ----------------------------------------------------

    def main_sha(self, repo: str) -> str:
        """Commit SHA at the tip of ``main``."""
        data = self._get(f"/repos/{repo}/commits/main", "commit")
        sha = data.get("sha") if isinstance(data, dict) else None
        if not isinstance(sha, str) or not sha:
            raise GitHubError(0, "unexpected commit response")
        return sha

    def compare(self, repo: str, base: str, head: str) -> tuple[int, str]:
        """``(ahead_by, html_url)`` of ``head`` relative to ``base``."""
        data = self._get(f"/repos/{repo}/compare/{base}...{head}", "compare", per_page=1)
        ahead = data.get("ahead_by") if isinstance(data, dict) else None
        url = data.get("html_url") if isinstance(data, dict) else None
        if isinstance(ahead, bool) or not isinstance(ahead, int) or not isinstance(url, str):
            raise GitHubError(0, "unexpected compare response")
        return ahead, url

    def waiting_runs(self, repo: str, workflows: list[str] | None = None) -> list[dict[str, Any]]:
        """Workflow runs waiting on an environment gate (paginated, capped). With ``workflows``
        (workflow file paths) only runs of those files are kept: a run's ``path`` may carry a
        ``@ref`` suffix. A run without a ``path`` is kept: ``name`` is not a file, so it
        cannot be matched, and hiding a waiting gate is worse than showing an extra one."""
        runs = self._paginate(
            f"/repos/{repo}/actions/runs",
            {"status": "waiting", "per_page": 100},
            "runs",
            key="workflow_runs",
        )
        if workflows is None:
            return runs
        return [r for r in runs if "path" not in r or str(r["path"]).split("@")[0] in workflows]

    def code_scanning_counts(self, repo: str) -> dict[str, int] | None:
        """Open alert counts ``{"critical": n, "high": n}``; None when code scanning is not
        enabled or not readable (HTTP 403/404). Counts only: alert details are never kept."""
        try:
            return {
                sev: len(self.list_code_scanning_alerts(repo, "open", sev))
                for sev in ("critical", "high")
            }
        except GitHubError as exc:
            if exc.status in (403, 404):
                return None
            raise
