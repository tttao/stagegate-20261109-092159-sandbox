"""``stagegate features``: detect platform features instead of assuming them (D11, R66).

Detection reads ``GET /meta``. GitHub Enterprise Server is recognised by ``installed_version``,
which github.com does not send. Anything not known to work on a GHES version is reported as
unsupported, so a workflow that consumes these flags takes the conservative path.
"""

from __future__ import annotations

import argparse
import json
import re
import sys
from dataclasses import asdict, dataclass

from stagegate import cli
from stagegate.github import GitHub, GitHubError

DOTCOM_ISSUER = "https://token.actions.githubusercontent.com"

# Minimum GHES version per feature flag, as (major, minor, patch). A flag with no entry is
# reported false on GHES (unknown means unsupported); on github.com every flag except
# ``private_pages`` is true. To add an entry, verify the feature on that GHES release (run the
# e2e scenarios per e2e/ghes/README.md, or read the release notes for that exact version),
# add e.g. ``"queue_max": (3, 15, 0)``, and record the verification in the matrix in that
# README. Never add a version you have not seen working.
GHES_FEATURE_MIN_VERSIONS: dict[str, tuple[int, int, int]] = {}

_VERSION = re.compile(r"^(\d+)\.(\d+)(?:\.(\d+))?")


@dataclass(frozen=True)
class Features:
    platform: str  # "dotcom" | "ghes" | "ghec-dr"
    ghes_version: str | None
    queue_max: bool
    deployment_false: bool
    lockfiles: bool
    private_pages: bool
    oidc_issuer: str


def parse_version(raw: str) -> tuple[int, int, int] | None:
    """``3.14.2`` -> (3, 14, 2); ``3.9`` -> (3, 9, 0); a pre-release suffix is ignored."""
    m = _VERSION.match(raw.strip())
    if not m:
        return None
    return int(m.group(1)), int(m.group(2)), int(m.group(3) or 0)


def _supported(flag: str, version: str) -> bool:
    minimum = GHES_FEATURE_MIN_VERSIONS.get(flag)
    parsed = parse_version(version)
    return minimum is not None and parsed is not None and parsed >= minimum


_GHE_COM = re.compile(r"^api\.([a-z0-9][a-z0-9-]*)\.ghe\.com$")


def detect(gh: GitHub, private_pages: bool | None = None) -> Features:
    """Detect the platform (R67: from the API host, then ``/meta``) and its features.

    ``api.github.com`` is github.com; ``api.<sub>.ghe.com`` is GitHub Enterprise Cloud with data
    residency (every flag false until verified); a host whose ``/meta`` has ``installed_version``
    is GHES; anything else raises ``GitHubError``. A failing ``/meta`` raises too."""
    meta = gh.meta()
    pages = bool(private_pages)
    host = gh.api_host
    version = meta.get("installed_version")
    if version is None:
        if host == "api.github.com":
            return Features("dotcom", None, True, True, True, pages, DOTCOM_ISSUER)
        m = _GHE_COM.match(host)
        if m:
            return Features(
                "ghec-dr",
                None,
                False,
                False,
                False,
                pages,
                f"https://token.actions.{m.group(1)}.ghe.com",
            )
        raise GitHubError(0, f"unknown platform: {host} (no installed_version in /meta)")
    if not isinstance(version, str) or not version:
        raise GitHubError(0, "unexpected installed_version in meta response")
    return Features(
        platform="ghes",
        ghes_version=version,
        queue_max=_supported("queue_max", version),
        deployment_false=_supported("deployment_false", version),
        lockfiles=False,
        private_pages=pages,
        oidc_issuer=f"{gh.web_url}/_services/token",
    )


def _bool(value: str) -> bool:
    if value not in ("true", "false"):
        raise argparse.ArgumentTypeError("expected true or false")
    return value == "true"


def _run(args: argparse.Namespace) -> int:
    gh: GitHub | None = None
    try:
        gh = GitHub.from_env()
        found = detect(gh, args.private_pages)
    except GitHubError as exc:
        print(f"features: error: {exc}", file=sys.stderr)
        return 1
    finally:
        if gh is not None:
            gh.close()
    print(json.dumps(asdict(found), indent=2))
    return 0


def _setup(parser: argparse.ArgumentParser) -> None:
    parser.add_argument("--json", action="store_true", help="print JSON (the only format)")
    parser.add_argument(
        "--private-pages",
        type=_bool,
        default=None,
        metavar="true|false",
        help="Pages visibility cannot be detected; state it here (default: false)",
    )
    parser.set_defaults(handler=_run)


cli.register("features", _setup)
