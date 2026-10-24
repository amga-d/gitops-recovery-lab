"""Statistics and report for a set of trials.

- descriptive statistics per condition (mean, median, SD, CV, P95);
- the pilot's sample-size estimate: the smallest N whose expected 95% CI
  half-width of mean MTTR meets the precision threshold, per condition;
- running-mean stability;
- Kruskal-Wallis across strategies within each scenario x sync cell, Holm
  across cells, Dunn post-hoc with Bonferroni, epsilon-squared and Cliff's delta;
- aligned-rank-transform ANOVA for strategy x sync per scenario;
- rollback success rate with a Wilson 95% CI;
- Kaplan-Meier per strategy with censoring, and a log-rank test.
"""
from __future__ import annotations

import itertools
import math
import pathlib

import matplotlib

matplotlib.use("Agg")
import matplotlib.pyplot as plt
import numpy as np
import pandas as pd
from scipy import stats

from .config import load

PRIMARY = "mttr_full_s"
COND = ["scenario", "strategy", "sync_policy"]


def precision(mean: float) -> float:
    """Allowed 95% CI half-width: 10% of the mean, but never tighter than 10 s."""
    return max(0.10 * mean, 10.0)


def n_required(sd: float, mean: float, n_max: int = 200) -> int:
    if not np.isfinite(sd) or sd == 0:
        return 2
    e = precision(mean)
    for n in range(2, n_max + 1):
        if stats.t.ppf(0.975, n - 1) * sd / math.sqrt(n) <= e:
            return n
    return n_max


def wilson(k: int, n: int) -> tuple[float, float]:
    if n == 0:
        return (np.nan, np.nan)
    z = 1.959964
    p = k / n
    c = (p + z * z / (2 * n)) / (1 + z * z / n)
    h = z * math.sqrt(p * (1 - p) / n + z * z / (4 * n * n)) / (1 + z * z / n)
    return (c - h, c + h)


def describe(x: pd.Series) -> dict:
    x = x.dropna()
    if not len(x):
        return {"n": 0}
    sd = x.std(ddof=1) if len(x) > 1 else np.nan
    return {"n": len(x), "mean": x.mean(), "median": x.median(), "sd": sd,
            "cv": sd / x.mean() if x.mean() else np.nan, "p95": x.quantile(0.95), "min": x.min(), "max": x.max()}


def cliffs_delta(a, b) -> float:
    a, b = np.asarray(a), np.asarray(b)
    if not len(a) or not len(b):
        return np.nan
    gt = sum((x > b).sum() for x in a)
    lt = sum((x < b).sum() for x in a)
    return (gt - lt) / (len(a) * len(b))


def dunn(groups: dict[str, np.ndarray]) -> list[dict]:
    names = list(groups)
    allv = np.concatenate([groups[g] for g in names])
    ranks = stats.rankdata(allv)
    n = len(allv)
    _, counts = np.unique(allv, return_counts=True)
    tie = (counts ** 3 - counts).sum() / (12 * (n - 1)) if n > 1 else 0
    mean_rank, i = {}, 0
    for g in names:
        k = len(groups[g])
        mean_rank[g] = ranks[i:i + k].mean()
        i += k
    out = []
    m = len(names) * (len(names) - 1) / 2
    for a, b in itertools.combinations(names, 2):
        se = math.sqrt((n * (n + 1) / 12 - tie) * (1 / len(groups[a]) + 1 / len(groups[b])))
        z = (mean_rank[a] - mean_rank[b]) / se if se else 0
        p = 2 * stats.norm.sf(abs(z))
        out.append({"a": a, "b": b, "z": z, "p": p, "p_bonf": min(1, p * m),
                    "cliffs_delta": cliffs_delta(groups[a], groups[b])})
    return out


def holm(pvals: list[float]) -> list[float]:
    order = np.argsort(pvals)
    m = len(pvals)
    adj = np.empty(m)
    running = 0.0
    for rank, idx in enumerate(order):
        running = max(running, min(1, (m - rank) * pvals[idx]))
        adj[idx] = running
    return list(adj)


def art_anova(df: pd.DataFrame, y: str, a: str, b: str) -> dict:
    """Aligned-rank-transform two-way ANOVA (Wobbrock et al. 2011): align for
    each effect, rank, and test that effect with a full-factorial ANOVA."""
    d = df[[y, a, b]].dropna()
    if d[a].nunique() < 2 or d[b].nunique() < 2:
        return {}
    grand = d[y].mean()
    cell = d.groupby([a, b])[y].transform("mean")
    ma = d.groupby(a)[y].transform("mean")
    mb = d.groupby(b)[y].transform("mean")
    resid = d[y] - cell
    aligned = {a: resid + ma - grand, b: resid + mb - grand,
               f"{a}:{b}": resid + cell - ma - mb + grand}
    out = {}
    for effect, yy in aligned.items():
        r = pd.Series(stats.rankdata(yy), index=d.index)
        out[effect] = _anova_effect(d.assign(_r=r), "_r", a, b, effect)
    return out


def _anova_effect(d, y, a, b, effect):
    grand = d[y].mean()
    cells = d.groupby([a, b])[y]
    ss_err = ((d[y] - cells.transform("mean")) ** 2).sum()
    df_err = len(d) - cells.ngroups
    la, lb = d[a].nunique(), d[b].nunique()
    if effect == a:
        ss = (d.groupby(a)[y].transform("mean") - grand).pow(2).sum(); dfe = la - 1
    elif effect == b:
        ss = (d.groupby(b)[y].transform("mean") - grand).pow(2).sum(); dfe = lb - 1
    else:
        inter = cells.transform("mean") - d.groupby(a)[y].transform("mean") - d.groupby(b)[y].transform("mean") + grand
        ss = inter.pow(2).sum(); dfe = (la - 1) * (lb - 1)
    if df_err <= 0 or ss_err == 0:
        return {"F": np.nan, "df": dfe, "df_err": df_err, "p": np.nan}
    f = (ss / dfe) / (ss_err / df_err)
    return {"F": f, "df": dfe, "df_err": df_err, "p": stats.f.sf(f, dfe, df_err)}


def kaplan_meier(t: np.ndarray, event: np.ndarray):
    order = np.argsort(t)
    t, event = t[order], event[order]
    times, surv, s = [0.0], [1.0], 1.0
    for u in np.unique(t[event]):
        at_risk = (t >= u).sum()
        d = ((t == u) & event).sum()
        s *= 1 - d / at_risk
        times.append(u); surv.append(s)
    return np.array(times), np.array(surv)


def logrank(groups: dict[str, tuple[np.ndarray, np.ndarray]]) -> dict:
    names = list(groups)
    all_t = np.unique(np.concatenate([g[0][g[1]] for g in groups.values()]))
    k = len(names)
    o_e = np.zeros(k); var = np.zeros((k, k))
    for u in all_t:
        n_j = np.array([(groups[g][0] >= u).sum() for g in names], float)
        d_j = np.array([((groups[g][0] == u) & groups[g][1]).sum() for g in names], float)
        n, d = n_j.sum(), d_j.sum()
        if n < 2:
            continue
        o_e += d_j - d * n_j / n
        for i in range(k):
            for j in range(k):
                var[i, j] += d * (n - d) / (n - 1) * (n_j[i] / n * ((i == j) - n_j[j] / n))
    try:
        chi2 = float(o_e[:-1] @ np.linalg.inv(var[:-1, :-1]) @ o_e[:-1])
    except np.linalg.LinAlgError:
        return {"chi2": np.nan, "df": k - 1, "p": np.nan}
    return {"chi2": chi2, "df": k - 1, "p": stats.chi2.sf(chi2, k - 1)}


def fmt(x, nd=1):
    if x is None or (isinstance(x, float) and not np.isfinite(x)):
        return "–"
    return f"{x:.{nd}f}" if isinstance(x, (float, np.floating)) else str(x)


def md_table(df: pd.DataFrame, nd=1) -> str:
    cols = list(df.columns)
    lines = ["| " + " | ".join(cols) + " |", "|" + "---|" * len(cols)]
    for _, r in df.iterrows():
        lines.append("| " + " | ".join(fmt(r[c], max(nd, 2) if "CV" in c else nd) for c in cols) + " |")
    return "\n".join(lines)


def analyze(out: pathlib.Path) -> None:
    df = pd.read_csv(out / "all-trials.csv")
    df = df[df["valid"].astype(str) == "True"].copy()
    # A retried trial replaces its failed attempt; keep the last valid one per plan slot.
    df["slot"] = df.trial_id.str.extract(r"^(T\d+)")[0]
    df = df.sort_values("attempt").groupby("slot").tail(1)
    figs = out / "figures"
    figs.mkdir(exist_ok=True)
    rep = ["# Pilot results", "", f"Valid trials: {len(df)}", ""]

    # --- descriptive statistics and the N estimate per condition ----------------
    rows = []
    for key, g in df.groupby(COND):
        full, svc, lat = describe(g[PRIMARY]), describe(g.mttr_service_s), describe(g.rollback_trigger_latency_s)
        trig = g.rollback_triggered.astype(str) == "True"
        succ = g.rollback_success.astype(str) == "True"
        lo, hi = wilson(int(succ.sum()), int(trig.sum()) if trig.any() else 0)
        rows.append({"scenario": key[0], "strategy": key[1], "sync": key[2], "n": full.get("n", 0),
                     "censored": int((g.censored.astype(str) == "True").sum()),
                     "MTTR_full mean": full.get("mean"), "median": full.get("median"), "SD": full.get("sd"),
                     "CV": full.get("cv"), "P95": full.get("p95"),
                     "MTTR_full strict mean": g.mttr_full_strict_s.mean() if "mttr_full_strict_s" in g else np.nan,
                     "MTTR_service mean": svc.get("mean"), "svc SD": svc.get("sd"),
                     "trigger mean": lat.get("mean"), "trigger CV": lat.get("cv"),
                     "cause": "/".join(sorted(set(g.trigger_cause.astype(str)))),
                     "rollback ok": f"{int(succ.sum())}/{int(trig.sum())}",
                     "success CI": f"[{fmt(lo, 2)}, {fmt(hi, 2)}]" if trig.any() else "–",
                     "N required": n_required(full.get("sd", np.nan), full.get("mean", np.nan)) if full.get("n", 0) > 1 else np.nan})
    desc = pd.DataFrame(rows)
    desc.to_csv(out / "summary-by-condition.csv", index=False)
    rep += ["## Per condition (seconds)", "",
            "Precision threshold for N: 95% CI half-width of mean MTTR_full ≤ max(10% of the mean, 10 s).", "",
            md_table(desc), ""]
    n_star = int(np.nanmax(desc["N required"])) if desc["N required"].notna().any() else None
    rep += [f"**N for the main runs (largest per-condition requirement): {n_star}**", ""]

    # --- success criteria per condition --------------------------------------
    # Trigger-latency consistency and rollback success only mean something
    # where the analysis, not a configured deadline, detects the fault.
    crit = []
    for key, g in df.groupby(COND):
        causes = set(g.trigger_cause.astype(str))
        trig = g.rollback_triggered.astype(str) == "True"
        if not trig.any():
            continue
        lat = describe(g.rollback_trigger_latency_s)
        succ = (g.rollback_success.astype(str) == "True").sum() / trig.sum()
        crit.append({"scenario": key[0], "strategy": key[1], "sync": key[2], "detected by": "/".join(sorted(causes)),
                     "trigger CV": lat.get("cv"), "CV < 0.30": bool(lat.get("cv", np.nan) < 0.30),
                     "success rate": succ, "success ≥ 0.80": bool(succ >= 0.80)})
    if crit:
        rep += ["## Latency consistency and rollback success per condition", "",
                "Only conditions where a rollback was triggered. Deadline-detected conditions pass the CV test by "
                "construction, so read them separately.", "", md_table(pd.DataFrame(crit), 2), ""]

    # --- hand-off between the layers -------------------------------------------
    if "argocd_state_before_revert" in df:
        h = df[df.rollback_triggered.astype(str) == "True"]
        ho = h.groupby(["argocd_state_before_revert"]).agg(
            trials=("trial_id", "size"), syncs_before_revert=("argocd_syncs_between_abort_and_revert", "sum")).reset_index()
        rep += ["## Argo CD between the rollback and the corrective commit", "", md_table(ho, 0), ""]

    # --- MTTR components -----------------------------------------------------
    comps = ["comp_detection_s", "comp_rollback_s", "comp_operator_wait_s", "comp_reconcile_s", "mttr_service_s",
             "outofsync_lag_s", "sync_duration_s", "inject_lag_s"]
    cm = df.groupby(COND)[comps].mean().reset_index()
    cm.to_csv(out / "components-by-condition.csv", index=False)
    rep += ["## Mean MTTR components (seconds)", "", md_table(cm), ""]

    # --- strategy comparison within each scenario x sync cell -----------------
    tests = []
    for (scn, sync), g in df.groupby(["scenario", "sync_policy"]):
        groups = {s: gg[PRIMARY].dropna().values for s, gg in g.groupby("strategy")}
        groups = {k: v for k, v in groups.items() if len(v)}
        if len(groups) < 2 or sum(len(v) for v in groups.values()) <= len(groups):
            continue
        allv = np.concatenate(list(groups.values()))
        if np.ptp(allv) == 0:
            h, p = 0.0, 1.0
        else:
            h, p = stats.kruskal(*groups.values())
        n, k = len(allv), len(groups)
        tests.append({"scenario": scn, "sync": sync, "H": h, "p": p, "eps2": (h - k + 1) / (n - k) if n > k else np.nan,
                      "n": n, "dunn": dunn(groups)})
    if tests:
        for t, adj in zip(tests, holm([t["p"] for t in tests])):
            t["p_holm"] = adj
        kw = pd.DataFrame([{k: v for k, v in t.items() if k != "dunn"} for t in tests])
        kw.to_csv(out / "kruskal-wallis.csv", index=False)
        rep += ["## Kruskal-Wallis across strategies (MTTR_full), Holm across cells", "", md_table(kw, 3), ""]
        dd = pd.DataFrame([{"scenario": t["scenario"], "sync": t["sync"], **d} for t in tests for d in t["dunn"]])
        dd.to_csv(out / "dunn.csv", index=False)
        rep += ["## Dunn post-hoc (Bonferroni) with Cliff's δ", "", md_table(dd, 3), ""]

    # --- interaction: aligned rank transform ANOVA ------------------------------
    arts = []
    for scn, g in df.groupby("scenario"):
        for eff, r in art_anova(g, PRIMARY, "strategy", "sync_policy").items():
            arts.append({"scenario": scn, "effect": eff, **r})
    if arts:
        art = pd.DataFrame(arts)
        art.to_csv(out / "art-anova.csv", index=False)
        rep += ["## Aligned-rank-transform ANOVA (MTTR_full)", "", md_table(art, 3), ""]

    # --- censoring: Kaplan-Meier per strategy and log-rank --------------------
    lr = []
    for scn, g in df.groupby("scenario"):
        fig, ax = plt.subplots(figsize=(5, 3.2))
        groups = {}
        for s, gg in g.groupby("strategy"):
            cens = gg.censored.astype(str) == "True"
            t = np.where(cens, pd.to_numeric(gg.get("t_end", np.nan), errors="coerce") - gg.t_inject, gg[PRIMARY])
            t = np.nan_to_num(t.astype(float), nan=600.0)
            ev = ~cens.values
            groups[s] = (t, ev)
            x, y = kaplan_meier(t, ev)
            ax.step(x, y, where="post", label=s)
        ax.set_xlabel("seconds since injection"); ax.set_ylabel("not yet recovered"); ax.set_title(scn)
        ax.legend(); fig.tight_layout(); fig.savefig(figs / f"km-{scn}.png", dpi=120); plt.close(fig)
        if len(groups) > 1:
            lr.append({"scenario": scn, **logrank(groups)})
    if lr:
        rep += ["## Log-rank across strategies (MTTR_full, censored at timeout)", "", md_table(pd.DataFrame(lr), 3), ""]

    # --- running-mean stability ------------------------------------------------
    fig, axes = plt.subplots(1, 2, figsize=(10, 3.5))
    for ax, metric in zip(axes, [PRIMARY, "rollback_trigger_latency_s"]):
        for key, g in df.sort_values("order").groupby(COND):
            v = g[metric].dropna().values
            if len(v):
                ax.plot(range(1, len(v) + 1), np.cumsum(v) / np.arange(1, len(v) + 1), alpha=0.5)
        ax.set_xlabel("trial within condition"); ax.set_ylabel(f"running mean {metric}")
    fig.tight_layout(); fig.savefig(figs / "running-mean.png", dpi=120); plt.close(fig)

    # --- figures: MTTR per condition and stacked components -------------------
    for metric in (PRIMARY, "mttr_service_s", "rollback_trigger_latency_s"):
        scns = sorted(df.scenario.unique())
        fig, axes = plt.subplots(1, len(scns), figsize=(3.2 * len(scns), 3.6), sharey=False)
        for ax, scn in zip(np.atleast_1d(axes), scns):
            g = df[df.scenario == scn]
            labels, data = [], []
            for (s, p), gg in g.groupby(["strategy", "sync_policy"]):
                labels.append(f"{s[:4]}/{p[:3]}"); data.append(gg[metric].dropna().values)
            ax.boxplot(data, tick_labels=labels, showmeans=True)
            ax.set_title(scn, fontsize=9); ax.tick_params(axis="x", rotation=60, labelsize=7)
        np.atleast_1d(axes)[0].set_ylabel(metric)
        fig.tight_layout(); fig.savefig(figs / f"box-{metric}.png", dpi=120); plt.close(fig)
    rep += ["## Figures", "", *[f"- figures/{p.name}" for p in sorted(figs.glob("*.png"))], ""]
    (out / "report.md").write_text("\n".join(rep))
    print(f"wrote {out / 'report.md'}")


if __name__ == "__main__":
    analyze(load().results_dir)
