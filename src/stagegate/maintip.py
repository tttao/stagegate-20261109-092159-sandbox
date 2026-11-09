"""``stagegate main-tip``: the commit at the tip of ``main`` (R70 supersession check).

The staged run refuses with ``superseded`` when the commit it was started for is no longer the
tip of ``main``: a newer push carries the change and runs the stages itself."""

from __future__ import annotations

import argparse
import re
import sys

from stagegate import cli
from stagegate.github import GitHub, GitHubError

_REPO = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]*/[A-Za-z0-9][A-Za-z0-9._-]*$")


def _repo(value: str) -> str:
    if not _REPO.match(value):
        raise argparse.ArgumentTypeError("expected owner/name")
    return value


def _run(args: argparse.Namespace) -> int:
    gh: GitHub | None = None
    try:
        gh = GitHub.from_env()
        print(gh.main_sha(args.repo))
    except GitHubError as exc:
        print(f"main-tip: error: {exc}", file=sys.stderr)
        return 1
    finally:
        if gh is not None:
            gh.close()
    return 0


def _setup(parser: argparse.ArgumentParser) -> None:
    parser.add_argument("--repo", required=True, type=_repo, help="owner/name")
    parser.set_defaults(handler=_run)


cli.register("main-tip", _setup)
