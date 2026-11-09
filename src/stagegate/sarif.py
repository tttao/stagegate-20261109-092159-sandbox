"""``stagegate sarif-scrub``: make a SARIF file safe to upload (D10: SARIF never contains values).

Scanners quote source and resource values in many places (snippets, messages, fixes, code flows,
notifications, properties, artifact contents ...). Removing the known ones is a deny-list that
breaks with the next scanner release, so the output is rebuilt from an allow-list: only the
fields below are copied, everything else is dropped.

kept: version, $schema; per run the tool driver and extensions (name, version, and rules with
id, name, shortDescription, defaultConfiguration.level), originalUriBaseIds (uri only),
and results with ruleId, ruleIndex, rule, level, kind, partialFingerprints, locations reduced to
artifactLocation{uri, uriBaseId} + region{startLine, startColumn, endLine, endColumn}, and
message.text, which is always the rule's short description (the rule id when there is none).
"""

from __future__ import annotations

import argparse
import hashlib
import json
import sys
from pathlib import Path
from typing import Any

from stagegate import cli

SCHEMA = "https://json.schemastore.org/sarif-2.1.0.json"
_REGION = ("startLine", "startColumn", "endLine", "endColumn")


def _dict(value: Any) -> dict[str, Any]:
    return value if isinstance(value, dict) else {}


def _str(value: Any) -> str | None:
    return value if isinstance(value, str) else None


def _pick(src: dict[str, Any], keys: tuple[str, ...], kind: type) -> dict[str, Any]:
    return {
        k: src[k] for k in keys if isinstance(src.get(k), kind) and not isinstance(src[k], bool)
    }


def _text(src: Any, keys: tuple[str, ...] = ("text",)) -> dict[str, str] | None:
    picked = {k: v for k in keys if isinstance(v := _dict(src).get(k), str)}
    return picked or None


def _rule(raw: Any) -> dict[str, Any]:
    raw = _dict(raw)
    out: dict[str, Any] = {}
    for key in ("id", "name"):
        if (v := _str(raw.get(key))) is not None:
            out[key] = v
    if (short := _text(raw.get("shortDescription"))) is not None:
        out["shortDescription"] = short
    if (level := _str(_dict(raw.get("defaultConfiguration")).get("level"))) is not None:
        out["defaultConfiguration"] = {"level": level}
    return out


def _component(raw: Any) -> dict[str, Any]:
    raw = _dict(raw)
    out: dict[str, Any] = {"name": _str(raw.get("name")) or "unknown"}
    if (version := _str(raw.get("version"))) is not None:
        out["version"] = version
    rules = raw.get("rules")
    if isinstance(rules, list):
        out["rules"] = [_rule(r) for r in rules]  # one per entry: ruleIndex stays valid
    return out


def _location(raw: Any) -> dict[str, Any] | None:
    phys = _dict(_dict(raw).get("physicalLocation"))
    art = _pick(_dict(phys.get("artifactLocation")), ("uri", "uriBaseId"), str)
    if not art:
        return None  # logical-only location
    out: dict[str, Any] = {"artifactLocation": art}
    if region := _pick(_dict(phys.get("region")), _REGION, int):
        out["region"] = region
    return {"physicalLocation": out}


def _lookup(comp: dict[str, Any], rule_id: str | None, index: Any) -> str:
    rules = comp.get("rules") or []
    rule = None
    if isinstance(index, int) and not isinstance(index, bool) and 0 <= index < len(rules):
        rule = rules[index]
    elif rule_id is not None:
        rule = next((r for r in rules if r.get("id") == rule_id), None)
    return _dict(_dict(rule).get("shortDescription")).get("text", "") if rule else ""


def _component_for(ref: Any, driver: dict[str, Any], extensions: list[dict[str, Any]]):
    ref = _dict(ref)
    index, name = ref.get("index"), ref.get("name")
    if isinstance(index, int) and not isinstance(index, bool) and 0 <= index < len(extensions):
        return extensions[index]
    if isinstance(name, str):
        for comp in extensions:
            if comp.get("name") == name:
                return comp
    return driver


def _result(raw: Any, driver: dict[str, Any], extensions: list[dict[str, Any]]) -> dict[str, Any]:
    raw = _dict(raw)
    out: dict[str, Any] = {}
    rule_id = _str(raw.get("ruleId"))
    ref = _dict(raw.get("rule"))
    if rule_id is None:
        rule_id = _str(ref.get("id"))
    if rule_id is not None:
        out["ruleId"] = rule_id
    if isinstance(raw.get("ruleIndex"), int) and not isinstance(raw["ruleIndex"], bool):
        out["ruleIndex"] = raw["ruleIndex"]
    if ref:
        kept = _pick(ref, ("id",), str) | _pick(ref, ("index",), int)
        if tc := _pick(_dict(ref.get("toolComponent")), ("name",), str) | _pick(
            _dict(ref.get("toolComponent")), ("index",), int
        ):
            kept["toolComponent"] = tc
        if kept:
            out["rule"] = kept
    out |= _pick(raw, ("level", "kind"), str)
    comp = _component_for(ref.get("toolComponent"), driver, extensions)
    index = raw.get("ruleIndex") if ref.get("toolComponent") is None else ref.get("index")
    text = _lookup(comp, rule_id, index) or _lookup(driver, rule_id, None)
    out["message"] = {"text": text or rule_id or "finding"}
    fingerprints = {
        k: v for k, v in _dict(raw.get("partialFingerprints")).items() if isinstance(v, str)
    }
    locations = [loc for r in raw.get("locations") or [] if (loc := _location(r))]
    if not fingerprints and rule_id is not None and locations:
        # upload-sarif has no checkout to compute these from: derive a stable one from
        # rule + file + line. Never from code text.
        phys = locations[0]["physicalLocation"]
        uri = phys["artifactLocation"].get("uri", "")
        line = phys.get("region", {}).get("startLine", "")
        key = f"{rule_id}{uri}{line}"
        fingerprints = {"primaryLocationLineHash": hashlib.sha256(key.encode()).hexdigest()[:32]}
    if fingerprints:
        out["partialFingerprints"] = fingerprints
    if locations:
        out["locations"] = locations
    return out


def _run(raw: Any) -> dict[str, Any]:
    raw = _dict(raw)
    tool = _dict(raw.get("tool"))
    driver = _component(tool.get("driver"))
    extensions = [_component(e) for e in tool.get("extensions") or []]
    out: dict[str, Any] = {"tool": {"driver": driver}}
    if extensions:
        out["tool"]["extensions"] = extensions
    bases = {
        k: {"uri": v["uri"]}
        for k, v in _dict(raw.get("originalUriBaseIds")).items()
        if isinstance(v, dict) and isinstance(v.get("uri"), str)
    }
    if bases:
        out["originalUriBaseIds"] = bases
    out["results"] = [_result(r, driver, extensions) for r in raw.get("results") or []]
    return out


def scrub(doc: dict[str, Any]) -> dict[str, Any]:
    """A new SARIF document holding only the allow-listed fields of ``doc``."""
    return {
        "version": _str(doc.get("version")) or "2.1.0",
        "$schema": _str(doc.get("$schema")) or SCHEMA,
        "runs": [_run(r) for r in doc.get("runs") or []],
    }


def _main(args: argparse.Namespace) -> int:
    try:
        doc = json.loads(Path(args.infile).read_text())
        if not isinstance(doc, dict):
            raise ValueError("not a SARIF object")
        Path(args.outfile).write_text(json.dumps(scrub(doc), indent=2) + "\n")
    except (OSError, ValueError) as exc:
        print(f"sarif-scrub: error: {type(exc).__name__}", file=sys.stderr)
        return 1
    return 0


def _setup(parser: argparse.ArgumentParser) -> None:
    parser.add_argument("--in", dest="infile", required=True)
    parser.add_argument("--out", dest="outfile", required=True)
    parser.set_defaults(handler=_main)


cli.register("sarif-scrub", _setup)
