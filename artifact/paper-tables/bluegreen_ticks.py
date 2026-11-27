#!/usr/bin/env python3
"""Write data/bluegreen-tick-delay.csv from the recorded trials.

For each analysis-detected blue-green trial it reports the observed abort and
the analysis tick at which the error-rate metric would have failed for the
(failureLimit + 1)-th time if two scrapes of *any* preview pods after the
first faulty response were enough. The query's rate() works per pod, so when
the four preview pods are scraped at different moments the observed abort can
come one 15 s tick later than that.

Inputs (all shipped in this artifact):
  ../data/all-trials.csv     rows of the trials (scrape timestamps in scrapes_app/scrapes_ksm)
  ../data/trials/<id>/       unpacked from ../data/trials.tar.gz (AnalysisRun events)
The tick computation reuses runner/mttr/model.py from this repository.

Usage:  python bluegreen_ticks.py      (run from artifact/paper-tables)
"""
import os
import pathlib
import sys

import numpy as np
import pandas as pd

HERE = pathlib.Path(__file__).resolve().parent
DATA = pathlib.Path(os.environ.get("RESULTS_DIR", HERE.parent / "data"))
sys.path.insert(0, str(HERE.parent.parent / "runner"))
from mttr import model  # noqa: E402

TICK_S = 15.0

df = pd.read_csv(DATA / "all-trials.csv")
df = df[(df.valid.astype(str) == "True") & (df.strategy == "bluegreen")
        & (df.trigger_cause.astype(str) == "analysis")]
rows = []
for _, r in df.sort_values("trial_id").iterrows():
    meas = model.measurements(DATA / "trials" / r.trial_id, r.v2_hash, r.t_inject)
    tick = model.predict(meas, r.first_fault_response_s, r.first_release_restart_s,
                         model._floats(r.scrapes_app), model._floats(r.scrapes_ksm))
    tick = tick if np.isfinite(tick) else np.nan
    late = r.rollback_trigger_latency_s - tick
    rows.append({"trial_id": r.trial_id, "scenario": r.scenario, "sync": r.sync_policy,
                 "revert": r.revert_path, "abort_s": r.rollback_trigger_latency_s,
                 "tick_two_release_scrapes_s": tick,
                 "delayed_one_tick": bool(abs(late) > model.HIT_S and abs(late - TICK_S) < 1.0)})
out = pd.DataFrame(rows)
out.to_csv(DATA / "bluegreen-tick-delay.csv", index=False)
print("wrote %d rows, %d delayed by one tick" % (len(out), int(out.delayed_one_tick.sum())), file=sys.stderr)
