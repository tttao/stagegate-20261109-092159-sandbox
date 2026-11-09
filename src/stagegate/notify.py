"""Teams notifications: Adaptive Cards carrying counts and links only (D8).

``build_card`` accepts a closed set of scalar fields, so plan values and finding details
cannot enter a card. Notifications never break a run: ``post`` and the CLI fail open.
"""

from __future__ import annotations

import argparse
import json
import os
import sys
from enum import StrEnum
from pathlib import Path

import httpx

from stagegate import cli

WEBHOOK_ENV = "STAGEGATE_TEAMS_WEBHOOK"
_SCHEMA = "http://adaptivecards.io/schemas/adaptive-card.json"


class Event(StrEnum):
    PROD_WAITING = "prod_waiting"
    PROD_DEPLOYED = "prod_deployed"
    PROD_REJECTED = "prod_rejected"
    PROD_HELD = "prod_held"
    PROD_FAILED = "prod_failed"
    STAGE_FAILED = "stage_failed"
    DRIFT_FOUND = "drift_found"
    DRIFT_CLEARED = "drift_cleared"
    NEW_FINDINGS = "new_findings"
    BREAKGLASS_USED = "breakglass_used"


# event -> (title prefix, status line)
_TEXT: dict[Event, tuple[str, str]] = {
    Event.PROD_WAITING: ("Prod waiting for approval", "Waiting for approval"),
    Event.PROD_DEPLOYED: ("Prod deployed", "Deployed"),
    Event.PROD_REJECTED: ("Prod rejected", "Rejected"),
    Event.PROD_HELD: ("Prod held", "Held"),
    Event.PROD_FAILED: ("Prod failed", "Failed"),
    Event.STAGE_FAILED: ("Stage failed", "Failed"),
    Event.DRIFT_FOUND: ("Drift found", "Drift detected"),
    Event.DRIFT_CLEARED: ("Drift cleared", "No drift"),
    Event.NEW_FINDINGS: ("New findings", "New findings reported"),
    Event.BREAKGLASS_USED: ("Break-glass used", "Break-glass used: post-approval required"),
}

# Fixed status lines for --detail: the CLI takes a key, never free text (D8).
DETAILS = {"replan_needed": "Plan refused: re-plan and approve again"}

_COUNT_FACTS = (
    ("Critical", "critical"),
    ("High", "high"),
    ("Medium", "medium"),
    ("Changes", "total"),
)


def build_card(
    event: Event,
    *,
    repo: str,
    env: str,
    sha: str,
    run_url: str,
    counts: dict | None = None,
    approver: str | None = None,
    detail: str | None = None,
) -> dict:
    """Adaptive Card 1.5 in the Teams Workflows message envelope."""
    title, status = _TEXT[Event(event)]
    facts = [
        {"title": "Repository", "value": repo},
        {"title": "Environment", "value": env},
        {"title": "Commit", "value": sha[:12]},
    ]
    if counts is not None:
        facts += [
            {"title": label, "value": str(int(counts.get(key, 0)))} for label, key in _COUNT_FACTS
        ]
    if approver:
        facts.append({"title": "Approver", "value": approver})
    card = {
        "type": "AdaptiveCard",
        "$schema": _SCHEMA,
        "version": "1.5",
        "body": [
            {"type": "TextBlock", "text": f"{title} · {repo}", "weight": "Bolder", "wrap": True},
            {"type": "TextBlock", "text": status, "wrap": True},
            *([{"type": "TextBlock", "text": DETAILS[detail], "wrap": True}] if detail else []),
            {"type": "FactSet", "facts": facts},
        ],
        "actions": [{"type": "Action.OpenUrl", "title": "Open run", "url": run_url}],
    }
    return {
        "type": "message",
        "attachments": [
            {
                "contentType": "application/vnd.microsoft.card.adaptive",
                "contentUrl": None,
                "content": card,
            }
        ],
    }


def _warn(msg: str) -> None:
    print(f"::warning::stagegate notify: {msg}")


def post(webhook_url: str, card: dict, timeout: float = 10.0) -> bool:
    """POST ``card`` to a Teams webhook. Never raises; never prints the URL (a secret)."""
    try:
        if httpx.URL(webhook_url).scheme != "https":
            _warn("webhook URL is not https; not posting")
            return False
        resp = httpx.post(webhook_url, json=card, timeout=timeout)
        resp.raise_for_status()
    except httpx.HTTPStatusError as exc:
        _warn(f"webhook returned HTTP {exc.response.status_code}")
        return False
    except Exception as exc:  # noqa: BLE001 - notifications must never break a run
        _warn(f"post failed ({type(exc).__name__})")
        return False
    return True


def _counts_from_changeset(path: str) -> dict:
    cs = json.loads(Path(path).read_text())
    return {**cs["risk"], "total": len(cs["items"])}


def _run(args: argparse.Namespace) -> int:
    counts = None
    if args.changeset:
        try:
            counts = _counts_from_changeset(args.changeset)
        except (OSError, ValueError, KeyError, TypeError) as exc:
            print(
                f"stagegate: notify: cannot read changeset: {type(exc).__name__}", file=sys.stderr
            )
            return 2
    webhook = os.environ.get(WEBHOOK_ENV)
    if not webhook:
        print(f"stagegate: notify: {WEBHOOK_ENV} is not set; skipping notification")
        return 0
    card = build_card(
        Event(args.event),
        repo=args.repo,
        env=args.env,
        sha=args.sha,
        run_url=args.run_url,
        counts=counts,
        approver=args.approver,
        detail=args.detail,
    )
    post(webhook, card)
    return 0


def _setup(parser: argparse.ArgumentParser) -> None:
    parser.add_argument("--event", required=True, choices=[e.value for e in Event])
    parser.add_argument("--repo", required=True)
    parser.add_argument("--env", required=True)
    parser.add_argument("--sha", required=True)
    parser.add_argument("--run-url", required=True)
    parser.add_argument("--changeset", help="changeset JSON to take risk counts from")
    parser.add_argument("--approver")
    parser.add_argument("--detail", choices=sorted(DETAILS), help="fixed extra status line")
    parser.set_defaults(handler=_run)


cli.register("notify", _setup)
