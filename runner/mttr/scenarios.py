"""Scenario plug-ins: each scenarios/<name>/scenario.yaml declares when and where a
fault lands and how it is injected. The injector kinds are implemented here,
once; the runner alone decides when the system has recovered and times it.
"""
from __future__ import annotations

import pathlib
import re
import string
from dataclasses import dataclass

import yaml

from .config import ROOT

SCENARIO_DIR = ROOT / "scenarios"


def seconds(v) -> float:
    if isinstance(v, (int, float)):
        return float(v)
    m = re.fullmatch(r"(\d+(?:\.\d+)?)(ms|s|m|h)?", str(v).strip())
    if not m:
        raise ValueError(f"bad duration: {v!r}")
    n, unit = float(m.group(1)), m.group(2) or "s"
    return n * {"ms": 0.001, "s": 1, "m": 60, "h": 3600}[unit]


@dataclass
class Scenario:
    name: str
    spec: dict
    path: pathlib.Path

    @property
    def phase(self) -> str:
        return self.spec["trigger"]["phase"]

    @property
    def at(self) -> str:
        return self.spec["trigger"].get("at", "")

    @property
    def inject(self) -> dict:
        return self.spec["inject"]

    @property
    def target(self) -> dict:
        return self.spec.get("target") or {}

    @property
    def timeout(self) -> float:
        return seconds(self.spec.get("timeout", "600s"))

    @property
    def also(self) -> list[dict]:
        """Further chaos that lands at a later point of the trial (e.g. while the
        operator's revert is being fetched), each with an optional stop_after."""
        return self.inject.get("also") or []

    @property
    def setup(self) -> dict:
        """State created before the fault and kept on purpose (e.g. an earlier
        aborted release), instead of the clean history the reset leaves."""
        return self.spec.get("setup") or {}

    @property
    def release_is_healthy(self) -> bool:
        """The release under test carries no fault: a rollback is a false alarm."""
        return bool(self.spec.get("release_is_healthy"))

    @property
    def is_control(self) -> bool:
        return self.phase == "baseline" and self.target.get("selector") == "stable" \
            and self.spec.get("expected_recovery") == "kubernetes"

    def applies(self, strategy: str, policy: str) -> bool:
        a = self.spec.get("applies_to") or {}
        return strategy in a.get("strategies", [strategy]) and policy in a.get("sync_policies", [policy])

    def render(self, manifest: str, values: dict[str, str]) -> list[dict]:
        text = string.Template((self.path / manifest).read_text()).substitute(values)
        return [d for d in yaml.safe_load_all(text) if d]


def load(name: str) -> Scenario:
    path = SCENARIO_DIR / name
    spec = yaml.safe_load((path / "scenario.yaml").read_text())
    if spec["trigger"]["phase"] not in ("baseline", "during-rollout"):
        raise ValueError(f"{name}: unsupported trigger phase {spec['trigger']['phase']}")
    if spec["inject"]["kind"] not in ("git-commit", "chaos-mesh", "kubectl-patch"):
        raise ValueError(f"{name}: unsupported injector {spec['inject']['kind']}")
    for extra in spec["inject"].get("also") or []:
        if extra.get("at") != "operator-revert":
            raise ValueError(f"{name}: inject.also supports only at: operator-revert")
    if (spec.get("setup") or {}).get("kind") not in (None, "aborted-release"):
        raise ValueError(f"{name}: unsupported setup {spec['setup']['kind']}")
    return Scenario(spec["name"], spec, path)


def all_scenarios() -> list[Scenario]:
    return [load(p.parent.name) for p in sorted(SCENARIO_DIR.glob("*/scenario.yaml"))]
