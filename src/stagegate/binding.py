"""Plan binding manifest (D1): prod applies only the exact approved plan files.

``make`` records SHA-256 hashes of each unit's plan file and provider lock file
plus the commit SHA and engine version. ``verify`` re-checks them before apply.
``verify-replan`` is the plain-Terraform fallback (D1(b)): a fresh plan must
match on ``(address, actions)`` and on a hash of the non-sensitive ``after``
values. Known limit: a change that only touches sensitive or unknown paths is
not detected by the values hash.

Nothing here ever prints plan values or file contents.
"""

from __future__ import annotations

import argparse
import functools
import hashlib
import json
import sys
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Literal

from stagegate import cli
from stagegate.planjson import Action, PlanFormatError, ResourceChange, load_plan

SCHEMA = "stagegate.manifest/v1"

Reason = Literal["sha", "engine", "plan_hash", "lock_hash", "unit_set", "values"]


class BindingError(Exception):
    """The approved plan binding does not match what is about to be applied."""

    def __init__(self, reason: Reason, unit: str | None = None) -> None:
        self.reason = reason
        self.unit = unit
        super().__init__(f"binding: {reason}" + (f" unit={unit}" if unit else ""))


@dataclass(frozen=True)
class UnitBinding:
    plan_sha256: str
    lock_sha256: str
    values_sha256: str | None = None


@dataclass(frozen=True)
class Manifest:
    sha: str
    env: str
    engine: str
    engine_version: str
    units: dict[str, UnitBinding]

    def to_json(self) -> str:
        doc = {
            "schema": SCHEMA,
            "sha": self.sha,
            "env": self.env,
            "engine": self.engine,
            "engine_version": self.engine_version,
            "units": {
                name: {
                    "plan_sha256": u.plan_sha256,
                    "lock_sha256": u.lock_sha256,
                    "values_sha256": u.values_sha256,
                }
                for name, u in sorted(self.units.items())
            },
        }
        return json.dumps(doc, indent=2) + "\n"

    @classmethod
    def from_json(cls, text: str) -> Manifest:
        try:
            doc = json.loads(text)
        except json.JSONDecodeError:
            raise ValueError("manifest is not valid JSON") from None
        if not isinstance(doc, dict):
            raise ValueError("manifest must be a JSON object")
        if doc.get("schema") != SCHEMA:
            raise ValueError(f"unsupported manifest schema (need {SCHEMA!r})")
        try:
            units = {
                name: UnitBinding(u["plan_sha256"], u["lock_sha256"], u.get("values_sha256"))
                for name, u in doc["units"].items()
            }
            m = cls(doc["sha"], doc["env"], doc["engine"], doc["engine_version"], units)
        except (KeyError, TypeError, AttributeError):
            raise ValueError("malformed manifest") from None
        strings = [m.sha, m.env, m.engine, m.engine_version]
        for u in units.values():
            strings += [u.plan_sha256, u.lock_sha256]
            if u.values_sha256 is not None:
                strings.append(u.values_sha256)
        if not all(isinstance(s, str) for s in strings):
            raise ValueError("malformed manifest")
        return m


def _file_sha(path: Path) -> str:
    return hashlib.sha256(Path(path).read_bytes()).hexdigest()


def _canonical(obj: Any) -> str:
    return json.dumps(obj, sort_keys=True, separators=(",", ":"))


_Redacted = tuple[Any, Any]  # (value with redacted nodes as None, redaction mask)


def _redact(value: Any, sens: Any, unk: Any) -> _Redacted:
    """Replace every sensitive/unknown node by ``None`` in place, keeping the shape.

    The mask is the union of both markers as booleans: ``True`` for a redacted
    subtree, a dict/list for partial redaction, ``False`` when nothing is marked.
    Structure is preserved so reorders, insertions and removals stay visible.
    """
    if sens is True or unk is True:
        return None, True
    if isinstance(value, dict) and (isinstance(sens, dict) or isinstance(unk, dict)):
        s_map = sens if isinstance(sens, dict) else {}
        u_map = unk if isinstance(unk, dict) else {}
        out: dict[str, Any] = {}
        mask: dict[str, Any] = {}
        for k in sorted(set(value) | set(s_map) | set(u_map)):
            v, m = _redact(value.get(k), s_map.get(k), u_map.get(k))
            if k in value:
                out[k] = v
            if m:
                mask[k] = m
        return out, mask or False
    if isinstance(value, list) and (isinstance(sens, list) or isinstance(unk, list)):
        s_list = sens if isinstance(sens, list) else []
        u_list = unk if isinstance(unk, list) else []
        n = max(len(value), len(s_list), len(u_list))
        pairs = [
            _redact(
                value[i] if i < len(value) else None,
                s_list[i] if i < len(s_list) else None,
                u_list[i] if i < len(u_list) else None,
            )
            for i in range(n)
        ]
        masks = [m for _, m in pairs]
        return [v for v, _ in pairs[: len(value)]], (masks if any(masks) else False)
    return value, False


def sanitized_after(rc: ResourceChange) -> _Redacted:
    """``(after with redacted nodes nulled, redaction mask)`` for one change."""
    return _redact(rc.after, rc.after_sensitive, rc.after_unknown)


def _values_sha(changes: list[ResourceChange]) -> str:
    rows = []
    for rc in changes:
        if rc.mode == "managed" and not set(rc.actions) <= {Action.NOOP, Action.READ}:
            after, mask = sanitized_after(rc)
            rows.append([rc.address, [a.value for a in rc.actions], after, mask])
    rows.sort(key=_canonical)
    return hashlib.sha256(_canonical(rows).encode()).hexdigest()


def make_manifest(
    sha: str,
    env: str,
    engine: str,
    engine_version: str,
    plan_files: dict[str, Path],
    lock_files: dict[str, Path],
    plan_json: dict[str, Path] | None = None,
) -> Manifest:
    if not plan_files:
        raise ValueError("no units to bind")
    if set(plan_files) != set(lock_files):
        raise ValueError("plan and lock files cover different units")
    if plan_json is not None and set(plan_json) != set(plan_files):
        raise ValueError("plan JSON files cover different units than the plans")
    units = {
        name: UnitBinding(
            _file_sha(plan_files[name]),
            _file_sha(lock_files[name]),
            _values_sha(load_plan(plan_json[name])) if plan_json is not None else None,
        )
        for name in sorted(plan_files)
    }
    return Manifest(sha, env, engine, engine_version, units)


def verify(
    manifest: Manifest,
    *,
    sha: str,
    engine_version: str,
    plan_files: dict[str, Path],
    lock_files: dict[str, Path],
) -> None:
    if not manifest.units or not plan_files or not lock_files:
        raise ValueError("no units to verify")
    if manifest.sha != sha:
        raise BindingError("sha")
    if manifest.engine_version != engine_version:
        raise BindingError("engine")
    if set(manifest.units) != set(plan_files) or set(manifest.units) != set(lock_files):
        raise BindingError("unit_set")
    for name in sorted(manifest.units):
        bound = manifest.units[name]
        if _file_sha(plan_files[name]) != bound.plan_sha256:
            raise BindingError("plan_hash", name)
        if _file_sha(lock_files[name]) != bound.lock_sha256:
            raise BindingError("lock_hash", name)


def verify_replan(
    manifest: Manifest, replan_json: dict[str, Path], *, sha: str, engine_version: str
) -> None:
    """D1(b): the fresh plan of ``sha`` with ``engine_version`` must match the manifest.

    Same order as :func:`verify`: sha, engine, unit set, then the values hash per unit."""
    if not manifest.units or not replan_json:
        raise ValueError("no units to verify")
    if manifest.sha != sha:
        raise BindingError("sha")
    if manifest.engine_version != engine_version:
        raise BindingError("engine")
    if set(manifest.units) != set(replan_json):
        raise BindingError("unit_set")
    for name in sorted(manifest.units):
        expected = manifest.units[name].values_sha256
        if expected is None or _values_sha(load_plan(replan_json[name])) != expected:
            raise BindingError("values", name)


# --- CLI -----------------------------------------------------------------


class _InputError(Exception):
    pass


def _dir_files(directory: str, suffix: str) -> dict[str, Path]:
    d = Path(directory)
    files = sorted(d.glob(f"*{suffix}")) if d.is_dir() else []
    if not files:
        raise _InputError(f"no *{suffix} files found in {d}")
    return {f.name[: -len(suffix)]: f for f in files}


def _load_manifest(path: str) -> Manifest:
    return Manifest.from_json(Path(path).read_text())


def _make(args: argparse.Namespace) -> int:
    plans = _dir_files(args.plans, ".tfplan")
    locks = _dir_files(args.locks, ".lock.hcl")
    pjson = _dir_files(args.plan_json, ".json") if args.plan_json else None
    m = make_manifest(args.sha, args.env, args.engine, args.engine_version, plans, locks, pjson)
    Path(args.out).write_text(m.to_json())
    return 0


def _verify(args: argparse.Namespace) -> int:
    verify(
        _load_manifest(args.manifest),
        sha=args.sha,
        engine_version=args.engine_version,
        plan_files=_dir_files(args.plans, ".tfplan"),
        lock_files=_dir_files(args.locks, ".lock.hcl"),
    )
    return 0


def _verify_replan(args: argparse.Namespace) -> int:
    verify_replan(
        _load_manifest(args.manifest),
        _dir_files(args.plan_json, ".json"),
        sha=args.sha,
        engine_version=args.engine_version,
    )
    return 0


def _guarded(fn: Callable[[argparse.Namespace], int]) -> Callable[[argparse.Namespace], int]:
    @functools.wraps(fn)
    def run(args: argparse.Namespace) -> int:
        try:
            return fn(args)
        except BindingError as exc:
            print(str(exc), file=sys.stderr)
            return 3
        except (_InputError, ValueError, PlanFormatError, OSError) as exc:
            # Messages come from our own checks; OSError text names a path, not contents.
            print(f"stagegate: binding failed: {exc}", file=sys.stderr)
            return 1
        except Exception as exc:  # fail closed; never echo the message (may hold values)
            print(f"stagegate: binding failed: unexpected {type(exc).__name__}", file=sys.stderr)
            return 1

    return run


def _setup(parser: argparse.ArgumentParser) -> None:
    sub = parser.add_subparsers(dest="binding_command", required=True, metavar="<action>")

    p = sub.add_parser("make", help="write a manifest for the approved plans")
    p.add_argument("--sha", required=True)
    p.add_argument("--env", required=True)
    p.add_argument("--engine", required=True, choices=["opentofu", "terraform"])
    p.add_argument("--engine-version", required=True)
    p.add_argument("--plans", required=True, help="directory of <unit>.tfplan")
    p.add_argument("--locks", required=True, help="directory of <unit>.lock.hcl")
    p.add_argument("--plan-json", help="directory of <unit>.json (D1(b) values hash)")
    p.add_argument("--out", required=True)
    p.set_defaults(handler=_guarded(_make))

    p = sub.add_parser("verify", help="check plans, locks, SHA and engine against a manifest")
    p.add_argument("--manifest", required=True)
    p.add_argument("--sha", required=True)
    p.add_argument("--engine-version", required=True)
    p.add_argument("--plans", required=True)
    p.add_argument("--locks", required=True)
    p.set_defaults(handler=_guarded(_verify))

    p = sub.add_parser("verify-replan", help="check a fresh plan JSON against a manifest (D1(b))")
    p.add_argument("--manifest", required=True)
    p.add_argument("--sha", required=True)
    p.add_argument("--engine-version", required=True)
    p.add_argument("--plan-json", required=True)
    p.set_defaults(handler=_guarded(_verify_replan))


cli.register("binding", _setup)
