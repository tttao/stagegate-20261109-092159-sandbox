"""``.stagegate.yaml``: strict, fail-closed configuration with compliant defaults (D2, D9)."""

from __future__ import annotations

import argparse
import json
import sys
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any

import yaml

from stagegate import cli

DEFAULT_PATH = Path(".stagegate.yaml")
NON_COMPLIANT = (
    "NON-COMPLIANT: solo_mode disables independent approval (DORA RTS 2024/1774 Art. 17(1)(b))"
)
ENGINES = ("opentofu", "terraform")
GATES = ("always", "risky")


class ConfigError(Exception):
    """The configuration file is invalid."""


@dataclass
class LedgerConfig:
    url: str | None = None


@dataclass
class NotifyConfig:
    teams: bool = True


@dataclass
class Config:
    stages: list[str] = field(default_factory=lambda: ["dev", "prp", "prod"])
    engine: str = "opentofu"
    terragrunt: bool = True
    units_root: str = "stages"
    ledger: LedgerConfig = field(default_factory=LedgerConfig)
    gate: str = "always"
    solo_mode: bool = False
    allow_cascade: bool = False
    extra_rules: list[str] = field(default_factory=list)
    drift_noise: dict[str, list[str]] = field(default_factory=dict)
    notify: NotifyConfig = field(default_factory=NotifyConfig)

    @property
    def binding_mode(self) -> str:
        """D1 for OpenTofu (encrypted plan), D1(b) for plain Terraform (re-plan)."""
        return "encrypted_plan" if self.engine == "opentofu" else "replan"


def _mapping(value: Any, where: str, allowed: set[str]) -> dict:
    if not isinstance(value, dict):
        raise ConfigError(f"{where}: expected a mapping, got {type(value).__name__}")
    unknown = sorted(str(k) for k in value if k not in allowed)
    if unknown:
        raise ConfigError(f"{where}: unknown key(s): {', '.join(unknown)}")
    return value


def _str(value: Any, where: str) -> str:
    if not isinstance(value, str):
        raise ConfigError(f"{where}: expected a string, got {type(value).__name__}")
    return value


def _bool(value: Any, where: str) -> bool:
    if not isinstance(value, bool):
        raise ConfigError(f"{where}: expected true or false, got {value!r}")
    return value


def _literal(value: Any, where: str, choices: tuple[str, ...]) -> str:
    if value not in choices:
        raise ConfigError(f"{where}: must be one of {', '.join(choices)}; got {value!r}")
    return value


def _str_list(value: Any, where: str) -> list[str]:
    if not isinstance(value, list):
        raise ConfigError(f"{where}: expected a list, got {type(value).__name__}")
    return [_str(v, f"{where}[{i}]") for i, v in enumerate(value)]


def _parse(raw: Any) -> Config:
    top = _mapping(raw, "config", set(Config.__dataclass_fields__))
    cfg = Config()
    if "stages" in top:
        stages = _str_list(top["stages"], "stages")
        if not stages or any(not s for s in stages):
            raise ConfigError("stages: must be a non-empty list of non-empty names")
        if len(set(stages)) != len(stages):
            raise ConfigError("stages: names must be unique")
        if "prod" not in stages:
            raise ConfigError("stages: must contain 'prod'")
        cfg.stages = stages
    if "engine" in top:
        cfg.engine = _literal(top["engine"], "engine", ENGINES)
    if "terragrunt" in top:
        cfg.terragrunt = _bool(top["terragrunt"], "terragrunt")
    if "units_root" in top:
        cfg.units_root = _str(top["units_root"], "units_root")
    if "ledger" in top:
        section = _mapping(top["ledger"], "ledger", {"url"})
        if section.get("url") is not None:
            cfg.ledger.url = _str(section["url"], "ledger.url")
    if "gate" in top:
        cfg.gate = _literal(top["gate"], "gate", GATES)
    if "solo_mode" in top:
        cfg.solo_mode = _bool(top["solo_mode"], "solo_mode")
    if "allow_cascade" in top:
        cfg.allow_cascade = _bool(top["allow_cascade"], "allow_cascade")
    if "extra_rules" in top:
        cfg.extra_rules = _str_list(top["extra_rules"], "extra_rules")
    if "drift_noise" in top:
        noise = top["drift_noise"]
        if not isinstance(noise, dict):
            raise ConfigError(f"drift_noise: expected a mapping, got {type(noise).__name__}")
        cfg.drift_noise = {
            _str(k, "drift_noise key"): _str_list(v, f"drift_noise.{k}") for k, v in noise.items()
        }
    if "notify" in top:
        section = _mapping(top["notify"], "notify", {"teams"})
        if "teams" in section:
            cfg.notify.teams = _bool(section["teams"], "notify.teams")
    return cfg


def load_config(path: Path = DEFAULT_PATH) -> Config:
    """Load ``path``; a missing file yields the defaults. Raises :class:`ConfigError`."""
    path = Path(path)
    if not path.exists():
        return Config()
    try:
        raw = yaml.safe_load(path.read_text())
    except (OSError, UnicodeDecodeError, yaml.YAMLError) as exc:
        raise ConfigError(f"{path}: cannot read: {type(exc).__name__}") from exc
    cfg = _parse({} if raw is None else raw)
    if cfg.solo_mode:
        print(NON_COMPLIANT, file=sys.stderr)
    return cfg


def _run(args: argparse.Namespace) -> int:
    try:
        cfg = load_config(Path(args.path))
    except ConfigError as exc:
        print(f"stagegate: config error: {exc}", file=sys.stderr)
        return 1
    print(json.dumps({**asdict(cfg), "binding_mode": cfg.binding_mode}, indent=2))
    return 0


def unsupported(cfg: Config) -> list[str]:
    """Settings the parser accepts but no workflow implements yet (R35, R73), one message each.

    The reusable workflows refuse such a configuration in their first job instead of silently
    running something else (for example the OpenTofu encrypted-plan path for ``engine: terraform``).
    """
    out = []
    if cfg.gate == "risky":
        out.append("gate: risky is not supported yet")
    if cfg.engine == "terraform":
        out.append("engine: not supported yet (engine: terraform; only opentofu runs today)")
    if not cfg.terragrunt:
        out.append("terragrunt: not supported yet (terragrunt: false; Terragrunt layouts only)")
    if cfg.extra_rules:
        out.append("extra_rules: not supported yet (no workflow reads extra rule files)")
    if not cfg.notify.teams:
        out.append(
            "notify.teams: not supported yet (notify.teams: false; leave the teams-webhook "
            "secret unset to disable Teams notifications)"
        )
    return out


def _check_supported(args: argparse.Namespace) -> int:
    try:
        cfg = load_config(Path(args.path))
    except ConfigError as exc:
        print(f"stagegate: config error: {exc}", file=sys.stderr)
        return 1
    problems = unsupported(cfg)
    for msg in problems:
        print(msg, file=sys.stderr)
    return 1 if problems else 0


def _setup(parser: argparse.ArgumentParser) -> None:
    sub = parser.add_subparsers(dest="config_command", metavar="<action>", required=True)
    show = sub.add_parser("show", help="print the resolved configuration as JSON")
    show.add_argument("--path", default=str(DEFAULT_PATH))
    show.set_defaults(handler=_run)
    check = sub.add_parser(
        "check-supported",
        help="exit 1, one line per setting, when the configuration uses an unsupported setting",
    )
    check.add_argument("--path", default=str(DEFAULT_PATH))
    check.set_defaults(handler=_check_supported)


cli.register("config", _setup)
