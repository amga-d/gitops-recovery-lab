"""Command line: python -m mttr {trial,pilot,extract,analyze,conditions} ...

Results go to $RESULTS_DIR (default .state/results)."""
from __future__ import annotations

import argparse
import json
import time

from . import config, obs
from .extract import extract
from .pilot import conditions, make_plan, run_plan


def main() -> None:
    p = argparse.ArgumentParser(prog="mttr")
    sub = p.add_subparsers(dest="cmd", required=True)
    t = sub.add_parser("trial", help="run one trial")
    t.add_argument("--strategy", required=True, choices=config.STRATEGIES)
    t.add_argument("--scenario", required=True)
    t.add_argument("--sync", default="auto", choices=config.SYNC_POLICIES)
    t.add_argument("--id", default=None)
    pl = sub.add_parser("pilot", help="run (or resume) a randomised plan of trials")
    pl.add_argument("--reps", type=int, default=3)
    pl.add_argument("--seed", type=int, default=None)
    pl.add_argument("--scenarios", nargs="*", default=None)
    ex = sub.add_parser("extract", help="re-extract every trial directory into a fresh CSV")
    ex.add_argument("--no-prometheus", action="store_true")
    sub.add_parser("analyze", help="statistics and report from all-trials.csv")
    sub.add_parser("conditions", help="list the conditions the scenarios apply to")
    a = p.parse_args()

    if a.cmd == "conditions":
        for c in conditions():
            print(*c)
        return
    cfg = config.load()
    out = cfg.results_dir
    out.mkdir(parents=True, exist_ok=True)
    if a.cmd == "trial":
        tid = a.id or time.strftime("X%m%d%H%M%S")
        plan = [{"trial_id": tid, "rep": 0, "strategy": a.strategy, "scenario": a.scenario, "sync_policy": a.sync}]
        run_plan(cfg, out / "adhoc", plan, retries=0)
    elif a.cmd == "pilot":
        plan_file = out / "plan.json"
        if plan_file.exists():
            plan = json.loads(plan_file.read_text())["trials"]
            print(f"resuming the plan in {plan_file} ({len(plan)} trials)")
        else:
            seed = a.seed if a.seed is not None else int(time.time())
            plan = make_plan(a.reps, seed, a.scenarios)
            plan_file.write_text(json.dumps({"seed": seed, "reps": a.reps, "created": time.time(),
                                             "trials": plan}, indent=1))
        run_plan(cfg, out, plan)
    elif a.cmd == "extract":
        import pandas as pd
        prom = None if a.no_prometheus else obs.Prometheus(cfg)
        rows = []
        for d in sorted((out / "trials").glob("T*")):
            if (d / "row.json").exists():
                old = json.loads((d / "row.json").read_text())
                row = extract(d, cfg, prom) | {k: old[k] for k in ("rep", "attempt", "valid") if k in old}
                (d / "row.json").write_text(json.dumps(row, indent=1, default=str))
                rows.append(row)
        pd.DataFrame(rows).to_csv(out / "all-trials.csv", index=False)
        print(f"re-extracted {len(rows)} trials")
    elif a.cmd == "analyze":
        from .analyze import analyze
        analyze(out)


if __name__ == "__main__":
    main()
