"""Configuration-derived detection-latency model, checked per trial.

The rule has no fitted parameter. Each analysis measurement of the release
either errors (the metrics path is down), or fails if the fault had become
visible in Prometheus by the time the query ran:

- a counter-rate metric (error-rate) fails once at least two scrapes of the
  release's pods fall after its first faulty response, because a rate needs
  two samples;
- a gauge metric (restarts) fails once one kube-state-metrics scrape falls
  after the release's first restart.

Errored measurements count as neither. The rollout aborts at the
(failureLimit + 1)-th failing measurement of any metric. A prediction is a hit
when it lands within one second of the observed abort.
"""
from __future__ import annotations

import json
import math
import pathlib

import numpy as np
import pandas as pd

FAILURE_LIMIT = {"error-rate": 1, "restarts": 0}
INGEST_S = 1.0
HIT_S = 1.0


def measurements(trial_dir: pathlib.Path, v2: str, t_inject: float) -> dict[str, list[tuple[float, str]]]:
    """Each metric's measurements of the release as (seconds since injection, outcome)."""
    last: dict[str, tuple[int, int, int]] = {}
    out: dict[str, list[tuple[float, str]]] = {}
    for line in (trial_dir / "events.jsonl").read_text().splitlines():
        e = json.loads(line)
        if e["kind"] != "AnalysisRun" or e["s"].get("hash") != v2:
            continue
        for m in e["s"].get("metrics") or []:
            cnt, fail, err = m.get("count") or 0, m.get("failed") or 0, m.get("error") or 0
            pc, pf, pe = last.get(m["name"], (0, 0, 0))
            if cnt > pc:
                outcome = "error" if err > pe else "fail" if fail > pf else "pass"
                out.setdefault(m["name"], []).append((round(e["t"] - t_inject, 2), outcome))
            last[m["name"]] = (cnt, fail, err)
    return out


def predict(meas: dict[str, list[tuple[float, str]]], t_fault: float, t_restart: float,
            scrapes_app: list[float], scrapes_ksm: list[float], ingest: float = INGEST_S) -> float:
    best = math.inf
    for name, ms in meas.items():
        if name == "error-rate":
            onset, scrapes, need = t_fault, scrapes_app, 2
        elif name == "restarts":
            onset, scrapes, need = t_restart, scrapes_ksm, 1
        else:
            continue
        if not np.isfinite(onset):
            continue
        fails = 0
        for t, outcome in ms:
            if outcome == "error":
                continue
            if sum(onset < s <= t - ingest for s in scrapes) >= need:
                fails += 1
                if fails == FAILURE_LIMIT[name] + 1:
                    best = min(best, t)
                    break
    return best


def _floats(v) -> list[float]:
    try:
        return [float(x) for x in json.loads(v)]
    except (TypeError, ValueError):
        return []


def check(out: pathlib.Path, df: pd.DataFrame, ingest: float = INGEST_S) -> pd.DataFrame:
    """Predicted vs observed abort for every analysis-detected trial."""
    rows = []
    for _, r in df[df.trigger_cause.astype(str) == "analysis"].iterrows():
        d = out / "trials" / r.trial_id
        meas = measurements(d, r.v2_hash, r.t_inject)
        pred = predict(meas, r.first_fault_response_s, r.first_release_restart_s,
                       _floats(r.scrapes_app), _floats(r.scrapes_ksm), ingest)
        obs = r.rollback_trigger_latency_s
        rows.append({"trial_id": r.trial_id, "scenario": r.scenario, "strategy": r.strategy,
                     "sync": r.sync_policy, "revert": r.revert_path, "observed_s": obs,
                     "predicted_s": pred if np.isfinite(pred) else np.nan,
                     "errored_measurements": sum(o == "error" for ms in meas.values() for _, o in ms)})
    res = pd.DataFrame(rows)
    if len(res):
        res["bias_s"] = res.observed_s - res.predicted_s
        res["hit"] = res.bias_s.abs() <= HIT_S
    return res
