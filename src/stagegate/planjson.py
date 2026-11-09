"""Parse OpenTofu/Terraform ``show -json`` plans into normalized resource changes."""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from enum import StrEnum
from pathlib import Path
from typing import Any


class PlanFormatError(ValueError):
    """The plan file is not a supported ``show -json`` document."""


class Action(StrEnum):
    NOOP = "no-op"
    CREATE = "create"
    UPDATE = "update"
    DELETE = "delete"
    REPLACE = "replace"
    READ = "read"
    IMPORT = "import"
    MOVE = "move"
    FORGET = "forget"


@dataclass(frozen=True)
class ResourceChange:
    address: str
    type: str
    provider: str
    mode: str
    actions: tuple[Action, ...]
    before: dict | None = field(repr=False)
    after: dict | None = field(repr=False)
    after_unknown: dict = field(repr=False)
    before_sensitive: dict | bool
    after_sensitive: dict | bool
    previous_address: str | None


@dataclass(frozen=True)
class OutputChange:
    """A root-module output change. Only the name and the actions are kept: output values
    (sensitive or not) are never read into this object, so nothing downstream can leak them."""

    name: str
    actions: tuple[Action, ...]


_RAW = {
    "no-op": Action.NOOP,
    "create": Action.CREATE,
    "update": Action.UPDATE,
    "delete": Action.DELETE,
    "read": Action.READ,
    "forget": Action.FORGET,
}


def normalize(
    raw_actions: list[str], importing: bool, previous_address: str | None
) -> tuple[Action, ...]:
    """Collapse raw plan actions plus import/move markers into one action tuple.

    Delete+create (either order) is REPLACE. Import and move come first, and a
    plain no-op is dropped when either marker is present.
    """
    if set(raw_actions) == {"create", "delete"} and len(raw_actions) == 2:
        core: tuple[Action, ...] = (Action.REPLACE,)
    else:
        try:
            core = tuple(_RAW[a] for a in raw_actions)
        except KeyError as exc:
            raise PlanFormatError(f"unknown plan action: {exc.args[0]!r}") from None
    lead: list[Action] = []
    if importing:
        lead.append(Action.IMPORT)
    if previous_address is not None:
        lead.append(Action.MOVE)
    if lead:
        core = tuple(a for a in core if a is not Action.NOOP)
    return (*lead, *core)


def _check_version(doc: Any) -> None:
    if not isinstance(doc, dict):
        raise PlanFormatError("plan JSON must be an object")
    version = doc.get("format_version")
    if not isinstance(version, str):
        raise PlanFormatError("plan JSON has no format_version")
    if version.split(".")[0] != "1":
        raise PlanFormatError(f"unsupported plan format_version {version!r} (need 1.x)")


def _sensitive(change: dict, key: str) -> dict | bool:
    """Sensitivity marker; missing or null means fully sensitive (fail closed)."""
    marker = change.get(key)
    return True if marker is None else marker


def _parse(rc: dict) -> ResourceChange:
    try:
        change = rc["change"]
        return ResourceChange(
            address=rc["address"],
            type=rc["type"],
            provider=rc["provider_name"],
            mode=rc["mode"],
            actions=normalize(
                change["actions"],
                change.get("importing") is not None,
                rc.get("previous_address"),
            ),
            before=change.get("before"),
            after=change.get("after"),
            after_unknown=change.get("after_unknown") or {},
            before_sensitive=_sensitive(change, "before_sensitive"),
            after_sensitive=_sensitive(change, "after_sensitive"),
            previous_address=rc.get("previous_address"),
        )
    except (KeyError, TypeError) as exc:
        raise PlanFormatError(f"malformed resource_changes entry: {exc!r}") from None


def _load_doc(path: Path) -> dict:
    try:
        doc = json.loads(Path(path).read_text())
    except json.JSONDecodeError as exc:
        raise PlanFormatError(f"{path}: not valid JSON: {exc}") from None
    _check_version(doc)
    return doc


def load_plan(path: Path) -> list[ResourceChange]:
    return [_parse(rc) for rc in _load_doc(path).get("resource_changes", [])]


def parse_output_changes(doc: dict) -> list[OutputChange]:
    """``output_changes`` of a plan document, sorted by name. Values are never read."""
    raw = doc.get("output_changes")
    if raw is None:
        return []
    if not isinstance(raw, dict):
        raise PlanFormatError("output_changes must be an object")
    out = []
    for name in sorted(raw):
        change = raw[name]
        actions = change.get("actions") if isinstance(change, dict) else None
        if not isinstance(name, str) or not isinstance(actions, list):
            raise PlanFormatError(f"malformed output_changes entry: {name!r}")
        out.append(OutputChange(name, normalize(actions, False, None)))
    return out


def load_output_changes(path: Path) -> list[OutputChange]:
    """The output changes of a plan file (a separate list from its resource changes)."""
    return parse_output_changes(_load_doc(path))
