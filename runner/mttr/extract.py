"""Turn one trial's raw records into one row of measurements.

Inputs (per trial directory): events.jsonl (watcher), requests.csv (load
generator), actions.json (runner and scripted operator), meta.json.

Primary timestamps come from the watcher and the request log; Prometheus is
queried afterwards for the same transitions as a cross-check (15 s resolution).
"""
from __future__ import annotations

import json
import math
import pathlib

import numpy as np
import pandas as pd

from . import obs
from .config import Config

NAN = float("nan")
REPLICAS = 4


def _first(events, pred, after=-math.inf):
    for e in events:
        if e["t"] >= after and pred(e):
            return e["t"]
    return NAN


def _num(x):
    return NAN if x is None else x


class Replay:
    """Steps through the event log, keeping the latest summary of every object."""

    def __init__(self, events):
        self.events = events

    def times_where(self, pred):
        """Yield (t, holds) after each event."""
        state: dict = {}
        for e in self.events:
            key = (e["kind"], e["name"])
            if e["type"] == "DELETED":
                state.pop(key, None)
            else:
                state[key] = e["s"]
            yield e["t"], pred(state)


def converged_pred(v1, app, git_head, strict=False):
    """Git, Argo CD and the Rollout back at the baseline, all baseline replicas
    Ready. With strict, also no pod of any other release left: that waits for
    the scale-down delay of the aborted release (configuration, not recovery),
    so it is reported separately as clean-up."""
    def pred(state):
        a = state.get(("Application", app), {})
        r = state.get(("Rollout", "mttr-app"), {})
        pods = [s for (k, _), s in state.items() if k == "Pod" and not s["deleted"]]
        base = [p for p in pods if p["hash"] == v1]
        ok = (a.get("sync") == "Synced" and a.get("health") == "Healthy" and a.get("revision") == git_head
              and r.get("phase") == "Healthy" and not r.get("abort")
              and r.get("currentPodHash") == v1 and r.get("stableRS") == v1
              and len(base) >= REPLICAS and all(p["ready"] for p in base))
        return ok and (not strict or len(pods) == REPLICAS)
    return pred


def last_rise(replay: Replay, pred, after, end):
    """Start of the final interval in which pred holds through `end`."""
    rise = NAN
    prev = False
    for t, ok in replay.times_where(pred):
        if t > end:
            break
        if ok and not prev and t >= after - 1e-6:
            rise = t
        elif ok and not prev:
            rise = after  # already true when the window opened
        if not ok:
            rise = NAN
        prev = ok
    return rise


def sli_bins(req: pd.DataFrame, cfg: Config) -> pd.DataFrame:
    main = req[req.stream == "main"].copy()
    main["bin"] = (main.t // cfg.num("SLI_BIN_S")).astype(int)
    main["ok"] = main.status.between(200, 299)
    g = main.groupby("bin").agg(ok=("ok", "sum"), total=("ok", "size"))
    g["ratio"] = g.ok / g.total
    return g


def extract(d: pathlib.Path, cfg: Config, prom: obs.Prometheus | None = None) -> dict:
    meta = json.loads((d / "meta.json").read_text())
    actions = json.loads((d / "actions.json").read_text())
    events = [json.loads(line) for line in (d / "events.jsonl").read_text().splitlines()]
    req = pd.read_csv(d / "requests.csv", dtype={"release": str, "pod": str}, keep_default_na=False)
    act = {}
    for a in actions:
        act.setdefault(a["action"], a["t"])
    app, v1, v2 = f"mttr-{meta['strategy']}", meta["v1_hash"], meta.get("v2_hash")
    kind = meta["inject_kind"]
    git_head = meta.get("git_revert") or meta.get("git_release") or meta["git_baseline"]
    t_end = meta["t_end"]
    row = {k: meta.get(k) for k in ("trial_id", "strategy", "scenario", "sync_policy", "inject_kind",
                                    "v1_hash", "v2_hash", "censored", "baseline_success")}
    row["block"] = meta.get("block")
    row["revert_path"] = meta.get("revert_path", "webhook")
    row["release_is_healthy"] = bool(meta.get("release_is_healthy"))
    row["history_precondition"] = meta.get("history_precondition")
    row["order"] = meta.get("order")
    row["confirmed"] = meta.get("confirmed", True)

    # --- injection ---------------------------------------------------------
    t_action = meta.get("t_inject_action", NAN)
    rollout_ev = [e for e in events if e["kind"] == "Rollout"]
    if kind == "git-commit":
        t_inject = _first(rollout_ev, lambda e: e["s"]["currentPodHash"] == v2, act.get("commit-release", 0))
    elif kind == "chaos-mesh":
        chaos = [e for e in events if e["kind"] in ("PodChaos", "NetworkChaos")]
        t_inject = _first(chaos, lambda e: any(r["phase"] == "Injected" or (r["injectedCount"] or 0) > 0
                                               for r in e["s"]["records"])
                          or any(c["type"] == "AllInjected" and c["status"] == "True" for c in e["s"]["conditions"]),
                          t_action - 1)
    else:
        t_inject = t_action
    if math.isnan(t_inject):
        t_inject = t_action
    row["t_inject"] = t_inject
    row["inject_lag_s"] = t_inject - t_action  # time from the runner's action to the fault taking effect
    if not v2:  # a fault on the live object can still start a new release (e.g. a pod-template edit)
        v2 = next((e["s"]["currentPodHash"] for e in rollout_ev
                   if e["t"] >= t_action and e["s"]["currentPodHash"] not in (None, v1)), None)
        row["v2_hash"] = v2

    # --- rollback trigger (DV2) ----------------------------------------------
    t_abort = _first(rollout_ev, lambda e: e["s"]["abort"], t_inject)
    ar = [e for e in events if e["kind"] == "AnalysisRun"]
    t_ar_failed = _first(ar, lambda e: e["s"]["phase"] == "Failed" and e["s"]["hash"] == v2, t_inject)
    # An analysis that cannot query its metrics ends in Error after
    # consecutiveErrorLimit measurement errors: a different detection path.
    t_ar_error = _first(ar, lambda e: e["s"]["phase"] == "Error" and e["s"]["hash"] == v2, t_inject)
    # With progressDeadlineAbort the deadline shows up as an abort whose message
    # says the ReplicaSet "has timed out progressing".
    t_deadline = _first(rollout_ev, lambda e: any(c["reason"] in ("ProgressDeadlineExceeded", "TimedOutReason")
                                                  for c in e["s"]["conditions"])
                        or "timed out progressing" in (e["s"]["message"] or ""), t_inject)
    row["t_abort"] = t_abort
    row["rollback_triggered"] = not math.isnan(t_abort)
    if not row["rollback_triggered"]:
        row["trigger_cause"] = "none"
    elif not math.isnan(t_ar_error) and t_ar_error <= t_abort + 1 and (
            math.isnan(t_ar_failed) or t_ar_error <= t_ar_failed):
        row["trigger_cause"] = "error-path"
        row["failed_metric"] = "+".join(sorted({m["name"] for e in ar if e["s"]["phase"] == "Error"
                                                and e["s"]["hash"] == v2 for m in e["s"]["metrics"]
                                                if m["phase"] == "Error"}))
    elif not math.isnan(t_ar_failed) and t_ar_failed <= t_abort + 1:
        row["trigger_cause"] = "analysis"
        failed_metrics = [m["name"] for e in ar if e["s"]["phase"] in ("Failed", "Error") and e["s"]["hash"] == v2
                          for m in e["s"]["metrics"] if m["phase"] in ("Failed", "Error")]
        row["failed_metric"] = "+".join(sorted(set(failed_metrics)))
    elif not math.isnan(t_deadline):
        row["trigger_cause"] = "deadline"
    else:
        row["trigger_cause"] = "other"
    row["rollback_trigger_latency_s"] = t_abort - t_inject

    # --- inputs of the detection-latency model --------------------------------
    # Measurement times of the release's analysis, and when its fault first
    # became observable, so the predicted abort tick can be checked per trial.
    rel_ar = [e for e in ar if e["s"]["hash"] == v2] if v2 else []
    t_a = _first(rel_ar, lambda e: e["s"]["phase"] == "Running")
    row["t_analysis_start"] = t_a
    row["analysis_start_offset_s"] = t_a - t_inject
    ticks: dict[str, list[float]] = {}
    first_fail: dict[str, float] = {}
    seen: dict[tuple[str, int], bool] = {}
    for e in rel_ar:
        for m in e["s"]["metrics"] or []:
            if m["count"] and (m["name"], m["count"]) not in seen:
                seen[(m["name"], m["count"])] = True
                ticks.setdefault(m["name"], []).append(round(e["t"] - t_inject, 2))
            if (m.get("failed") or 0) > 0 and m["name"] not in first_fail:
                first_fail[m["name"]] = e["t"]
    row["analysis_ticks"] = json.dumps(ticks) if ticks else ""
    row["first_failing_measurement_s"] = min(first_fail.values()) - t_inject if first_fail else NAN
    # --- rollback execution ----------------------------------------------------
    pod_hash = {}
    for e in events:
        if e["kind"] == "Pod":
            pod_hash[e["name"]] = e["s"]["hash"]
    req["hash"] = req.pod.map(pod_hash)
    main = req[req.stream == "main"]
    new_served = main[(main.hash.notna()) & (main.hash != v1) & (main.status > 0)]
    row["requests_served_by_release"] = int(len(new_served[new_served.t >= t_inject]))
    rs_ev = [e for e in events if e["kind"] == "ReplicaSet"]
    t_stable_full = _first(rs_ev, lambda e: e["s"]["hash"] == v1 and e["s"]["ready"] >= REPLICAS, t_abort) \
        if row["rollback_triggered"] else NAN
    # Stable may have stayed fully available throughout (blue-green, early canary).
    if row["rollback_triggered"] and math.isnan(t_stable_full):
        t_stable_full = t_abort
    last_new = new_served[new_served.t >= t_abort].t.max() if row["rollback_triggered"] else NAN
    t_rb_done = np.nanmax([t_abort, t_stable_full, _num(last_new)]) if row["rollback_triggered"] else NAN
    row["t_rollback_done"] = t_rb_done
    row["rollback_execution_s"] = t_rb_done - t_abort
    row["release_scaled_down_s"] = _first(rs_ev, lambda e: e["s"]["hash"] == v2 and e["s"]["replicas"] == 0, t_abort) - t_abort \
        if row["rollback_triggered"] and v2 else NAN

    # --- service level (MTTR_service) -----------------------------------------
    # A failure is attributed to the fault if the release or pod under fault
    # answered it, or if no pod answered at all (router error, timeout). A
    # failure answered by an untouched pod is background noise from the lab
    # (e.g. a host stall): reported, but it does not define recovery.
    targets = {h for h in (v2,) if h}
    failed = ~req.status.between(200, 299)
    attributable = req.hash.isin(targets) | (req.pod == "") | (req.pod == meta.get("target_pod"))
    req["fault_failed"] = failed & attributable
    req["background_failed"] = failed & ~attributable
    slo, w = cfg.num("SLI_SUCCESS"), cfg.num("SLI_BIN_S")
    lo, hi = t_inject - w, t_end - cfg.num("REQUEST_TIMEOUT_S")

    def restored(fail_col):
        m = req[(req.stream == "main") & (req.t >= lo) & (req.t <= hi)].copy()
        m["bin"] = (m.t // w).astype(int)
        g = m.groupby("bin").agg(bad=(fail_col, "sum"), total=(fail_col, "size"))
        bad = g[(g.total - g.bad) / g.total < slo]
        t = max((bad.index.max() + 1) * w, t_inject) if len(bad) else t_inject
        return t, len(bad)

    req["all_failed"] = failed
    t_svc, n_bad = restored("fault_failed")
    t_svc_all, _ = restored("all_failed")
    row["service_impact"] = n_bad > 0
    row["t_service_restored"] = t_svc
    row["mttr_service_s"] = t_svc - t_inject
    row["mttr_service_all_failures_s"] = t_svc_all - t_inject
    row["bad_bins"] = int(n_bad)
    main = req[req.stream == "main"]
    after = main[main.t >= t_inject]
    row["failed_requests"] = int(after.fault_failed.sum())
    rel_fail = after[after.fault_failed & after.hash.isin(targets)]
    row["t_first_fault_response"] = float(rel_fail.t.min()) if len(rel_fail) else NAN
    row["first_fault_response_s"] = row["t_first_fault_response"] - t_inject
    t_restart = _first([e for e in events if e["kind"] == "Pod"],
                       lambda e: e["s"]["hash"] == v2 and (e["s"].get("restarts") or 0) > 0, t_inject - 5) if v2 else NAN
    row["first_release_restart_s"] = t_restart - t_inject
    row["background_failed_requests"] = int(after.background_failed.sum())
    row["requests_in_outage"] = int(((main.t >= t_inject) & (main.t <= t_svc)).sum())
    base = main[(main.t >= meta.get("t_load_start", -math.inf)) & (main.t < t_inject)]
    row["p95_latency_ms_baseline"] = float(base.latency_ms.quantile(0.95)) if len(base) else NAN

    # --- GitOps reconciliation ---------------------------------------------
    app_ev = [e for e in events if e["kind"] == "Application"]
    t_revert = act.get("operator-revert-commit", NAN)
    row["t_operator_sees_failure"] = act.get("operator-sees-failure", NAN)
    row["t_revert_commit"] = t_revert
    ref = t_revert if not math.isnan(t_revert) else t_inject
    t_oos = _first(app_ev, lambda e: e["s"]["sync"] == "OutOfSync", ref)
    row["t_outofsync"] = t_oos
    row["outofsync_lag_s"] = t_oos - ref  # webhook (or watch, for drift) until Argo CD notices
    t_sync_req = next((a["t"] for a in actions if a["action"] == "operator-sync" and a["t"] >= ref), NAN)
    row["t_operator_sync"] = t_sync_req
    t_op_start = _first(app_ev, lambda e: e["s"]["opPhase"] == "Running", t_oos if not math.isnan(t_oos) else ref)
    row["t_sync_started"] = t_op_start
    # Only a sync that follows an OutOfSync counts as Argo CD acting.
    t_synced = NAN if math.isnan(t_oos) else _first(
        app_ev, lambda e: e["s"]["sync"] == "Synced" and e["s"]["revision"] == git_head
        and e["s"]["opPhase"] != "Running", t_oos)
    row["t_synced"] = t_synced
    row["sync_duration_s"] = t_synced - t_op_start

    # What Argo CD showed and did between the rollback and the corrective
    # commit: the hand-off between the recovery layers.
    if row["rollback_triggered"]:
        until = t_revert if not math.isnan(t_revert) else t_end
        before = [e for e in app_ev if e["t"] <= until - 0.5]
        last = before[-1]["s"] if before else {}
        row["argocd_state_before_revert"] = f"{last.get('sync')}/{last.get('health')}"
        row["argocd_syncs_between_abort_and_revert"] = len({
            e["s"]["opStartedAt"] for e in app_ev
            if t_abort <= e["t"] < until and e["s"]["opPhase"] == "Running"})
    # A healthy release that was promoted and never reverted is the intended
    # end state of its trial; everything else converges back to the baseline.
    want = v2 if meta.get("release_is_healthy") and not meta.get("git_revert") and v2 else v1
    t_conv = last_rise(Replay(events), converged_pred(want, app, git_head), t_inject, t_end)
    row["t_state_converged"] = t_conv
    t_clean = last_rise(Replay(events), converged_pred(want, app, git_head, strict=True), t_inject, t_end)
    row["t_cleanup_done"] = t_clean
    row["cleanup_after_converged_s"] = t_clean - t_conv
    row["mttr_full_strict_s"] = (np.nanmax([t_clean, t_svc]) - t_inject) if not (meta.get("censored") or math.isnan(t_clean)) else NAN
    t_full = np.nanmax([t_conv, t_svc]) if not math.isnan(t_conv) else NAN
    if meta.get("censored"):
        t_full = NAN
    row["t_full"] = t_full
    row["mttr_full_s"] = t_full - t_inject

    # Sequential decomposition: detection, rollback, reconciliation.
    row["comp_detection_s"] = row["rollback_trigger_latency_s"]
    row["comp_rollback_s"] = row["rollback_execution_s"]
    row["comp_operator_wait_s"] = (t_revert - row["t_operator_sees_failure"]) if not math.isnan(t_revert) else NAN
    row["comp_reconcile_s"] = (t_conv - t_revert) if not math.isnan(t_revert) else (t_conv - t_inject)
    row["comp_service_after_rollback_s"] = t_svc - t_rb_done if row["rollback_triggered"] else NAN

    # --- rollback success (DV3) and which layer recovered the service --------
    if row["rollback_triggered"]:
        row["rollback_success"] = bool(not meta.get("censored") and (math.isnan(t_revert) or t_svc <= t_revert + 1))
    else:
        row["rollback_success"] = None
    ops = [a for a in actions if a["action"] in ("operator-revert-commit", "operator-sync") and a["t"] <= np.nan_to_num(t_full, nan=t_end)]
    if meta.get("censored"):
        row["recovered_by"] = "none"
    elif row["rollback_success"]:
        row["recovered_by"] = "rollouts"
    elif not ops and not math.isnan(t_synced):
        row["recovered_by"] = "argocd"
    elif not ops and not row["rollback_triggered"]:
        row["recovered_by"] = "kubernetes"
    elif ops:
        row["recovered_by"] = "operator+argocd"
    else:
        row["recovered_by"] = "unknown"

    row["coverage_class"] = coverage_class(row, meta)

    # --- covariates --------------------------------------------------------------
    cov = meta.get("covariates", {})
    skews = [abs(v["offset_ms"]) for v in cov.get("clock_skew", {}).values() if v.get("offset_ms") is not None]
    row["max_clock_skew_ms"] = max(skews) if skews else NAN
    row |= {k: v for k, v in cov.get("host", {}).items() if not k.startswith("host_psw")}
    end = cov.get("host_end", {})
    for k in ("host_pswpin", "host_pswpout"):  # pages swapped during the trial
        row[k.replace("host_", "host_trial_")] = end.get(k, NAN) - cov.get("host", {}).get(k, NAN) if end else NAN
    for n, u in cov.get("nodes", {}).items():
        row[f"{n}_cpu_m"], row[f"{n}_mem_mib"] = u["cpu_m"], u["mem_mib"]
    nodes = {}
    for e in events:
        if e["kind"] == "Pod" and e["s"]["hash"] == v2 and e["s"]["node"]:
            nodes[e["name"]] = e["s"]["node"]
    row["release_pod_nodes"] = ";".join(sorted(f"{n.split('-')[-1]}" for n in nodes.values()))

    # --- Prometheus cross-check (15 s scrape) --------------------------------
    if prom is not None:
        row |= cross_check(prom, meta, row, t_end)
    return row


def coverage_class(row: dict, meta: dict) -> str:
    """One class per trial: how (or whether) the stack caught the fault."""
    if row["rollback_triggered"]:
        if meta.get("release_is_healthy"):
            return "false-rollback"
        return {"analysis": "analysis", "deadline": "deadline", "error-path": "error-path"}.get(
            row["trigger_cause"], "other")
    if meta.get("censored"):
        return "stalled"
    if meta.get("release_is_healthy"):
        return "tolerated"  # the healthy release went through despite the fault
    if row["recovered_by"] == "operator+argocd":
        return "operator-only"
    if row["recovered_by"] in ("argocd", "kubernetes"):
        return "self-heal"
    return "other"


def _prom_first(series, after):
    ts = sorted(float(t) for s in series for t, v in s["values"] if float(v) > 0 and float(t) >= after)
    return ts[0] if ts else NAN


def cross_check(prom: obs.Prometheus, meta: dict, row: dict, t_end: float) -> dict:
    ns, app = f"mttr-{meta['strategy']}", f"mttr-{meta['strategy']}"
    t0, start = row["t_inject"], row["t_inject"] - 60
    out = {}
    try:
        q = lambda expr: prom.range(expr, start, t_end + 30, 5)
        # The Rollouts controller's metrics carry the app's namespace as exported_namespace.
        out["prom_t_abort"] = _prom_first(q(f'rollout_phase{{exported_namespace="{ns}",name="mttr-app",phase="Abort"}}'), t0)
        if row.get("v2_hash"):
            out["prom_t_analysis_failed"] = _prom_first(
                q(f'analysis_run_phase{{exported_namespace="{ns}",name=~"mttr-app-{row["v2_hash"]}-.*",phase="Failed"}}'), t0)
            out["prom_t_release_restart"] = _prom_first(q(
                f'sum(kube_pod_container_status_restarts_total{{namespace="{ns}"}} * on (namespace, pod) group_left () '
                f'max by (namespace, pod) (kube_pod_labels{{namespace="{ns}",label_rollouts_pod_template_hash="{row["v2_hash"]}"}}))'), t0 - 30)
        if row.get("v2_hash"):
            out["prom_t_release_errors"] = _prom_first(
                q(f'mttr:app_error_ratio:rate1m{{namespace="{ns}",rollouts_pod_template_hash="{row["v2_hash"]}"}}'), t0 - 30)
        if row.get("v2_hash"):
            # When Prometheus actually sampled the release: the scrape phase the
            # latency model needs. timestamp() of the newest sample at each step.
            for key, expr in (("app", f'app_http_requests_total{{namespace="{ns}",pod=~"mttr-app-{row["v2_hash"]}-.*"}}'),
                              ("ksm", f'kube_pod_container_status_restarts_total{{namespace="{ns}",pod=~"mttr-app-{row["v2_hash"]}-.*"}}')):
                series = prom.range(f"timestamp({expr})", t0 - 30, t_end, 1)
                stamps = sorted({round(float(v), 3) for s in series for _, v in s["values"]})
                out[f"scrapes_{key}"] = json.dumps([round(s - t0, 2) for s in stamps[:12]])
        ref = row["t_revert_commit"] if not math.isnan(row["t_revert_commit"]) else t0
        out["prom_t_outofsync"] = _prom_first(q(f'argocd_app_info{{name="{app}",sync_status="OutOfSync"}}'), ref)
        out["prom_t_stable_ready"] = _prom_first(
            q(f'mttr:app_ready_pods{{namespace="{ns}",label_rollouts_pod_template_hash="{meta["v1_hash"]}"}} >= 4'),
            row["t_rollback_done"] if not math.isnan(row["t_rollback_done"]) else t0)
        for k in ("abort", "outofsync"):
            out[f"xcheck_{k}_delta_s"] = out[f"prom_t_{k}"] - row[f"t_{k}"]
    except Exception as e:  # cross-check only; never lose a trial over it
        out["prom_error"] = str(e)[:200]
    return out
