"""The trial plan and its execution: randomised blocks over every applicable
condition, resumable, one CSV per condition plus one for all trials."""
from __future__ import annotations

import csv
import json
import pathlib
import random
import time
import traceback

from . import obs
from .config import STRATEGIES, SYNC_POLICIES, Config
from .extract import extract
from .scenarios import all_scenarios, load
from .trial import Runner, Trial, log


def conditions(names: list[str] | None = None) -> list[tuple[str, str, str]]:
    scs = [load(n) for n in names] if names else all_scenarios()
    return [(s, sc.name, p) for sc in scs for s in STRATEGIES for p in SYNC_POLICIES if sc.applies(s, p)]


def make_plan(reps: int, seed: int, names: list[str] | None) -> list[dict]:
    """Each replicate is one block holding every condition once, in a fresh
    random order, so drift over the session spreads evenly over conditions."""
    rng = random.Random(seed)
    conds = conditions(names)
    plan = []
    for rep in range(1, reps + 1):
        block = conds[:]
        rng.shuffle(block)
        for s, sc, p in block:
            plan.append({"trial_id": f"T{len(plan) + 1:03d}", "rep": rep, "strategy": s, "scenario": sc,
                         "sync_policy": p})
    return plan


def append_csv(path: pathlib.Path, row: dict) -> None:
    exists = path.exists()
    fields = list(row)
    if exists:
        with open(path) as f:
            header = next(csv.reader(f), [])
        fields = header + [k for k in row if k not in header]
        if fields != header:  # new columns: rewrite with the wider header
            with open(path) as f:
                rows = list(csv.DictReader(f))
            with open(path, "w", newline="") as f:
                w = csv.DictWriter(f, fields)
                w.writeheader()
                w.writerows(rows)
    with open(path, "a", newline="") as f:
        w = csv.DictWriter(f, fields)
        if not exists:
            w.writeheader()
        w.writerow(row)


def run_plan(cfg: Config, outdir: pathlib.Path, plan: list[dict], retries: int = 1) -> None:
    runner = Runner(cfg)
    prom = obs.Prometheus(cfg)
    (outdir / "conditions").mkdir(parents=True, exist_ok=True)
    queue = [dict(item, attempt=1) for item in plan]
    while queue:
        item = queue.pop(0)
        tid = item["trial_id"] + ("" if item["attempt"] == 1 else f"r{item['attempt'] - 1}")
        d = outdir / "trials" / tid
        if (d / "row.json").exists():
            continue
        if d.exists() and not (d / "row.json").exists():
            d.rename(d.with_name(f"{tid}.aborted-{int(time.time())}"))
        trial = Trial(runner, item["strategy"], load(item["scenario"]), item["sync_policy"], tid, d)
        trial.meta |= {"order": plan.index(next(p for p in plan if p["trial_id"] == item["trial_id"])) + 1,
                       "rep": item["rep"], "attempt": item["attempt"]}
        try:
            trial.run()
            row = extract(d, cfg, prom)
        except Exception as e:
            log(f"trial {tid} failed: {e}")
            (d / "error.txt").write_text(traceback.format_exc()) if d.exists() else None
            if item["attempt"] <= retries:
                queue.append(dict(item, attempt=item["attempt"] + 1))
            continue
        row["rep"], row["attempt"] = item["rep"], item["attempt"]
        # A trial that started unhealthy or whose fault never took effect is
        # not a measurement of the condition; it is kept on file and re-run.
        row["valid"] = bool(row["baseline_success"] >= 0.995 and row["confirmed"] is not False)
        (d / "row.json").write_text(json.dumps(row, indent=1, default=str))
        append_csv(outdir / "all-trials.csv", row)
        if row["valid"]:
            append_csv(outdir / "conditions" / f"{item['strategy']}__{item['scenario']}__{item['sync_policy']}.csv", row)
        elif item["attempt"] <= retries:
            queue.append(dict(item, attempt=item["attempt"] + 1))
        log(f"trial {tid}: MTTR_full={row['mttr_full_s']:.1f}s MTTR_service={row['mttr_service_s']:.1f}s "
            f"trigger={row['trigger_cause']} latency={row['rollback_trigger_latency_s']:.1f}s valid={row['valid']}")
