"""One trial: reset to the baseline release, trigger, inject, let the recovery
stack and the scripted operator act, and record everything.

Measurement belongs to the runner alone. Scenarios only say when and where a
fault lands; whether and when the system recovered is decided here, the same
way for every scenario.
"""
from __future__ import annotations

import json
import pathlib
import random
import subprocess
import sys
import time
from collections import defaultdict

import yaml

from . import obs
from .cluster import CHAOS, ROLLOUT, Cluster
from .config import ROOT, Config
from .gitops import ArgoCD, Gitea, baseline_rollout, release_rollout
from .scenarios import Scenario, seconds
from .watcher import Watcher

REPLICAS = 4


def log(msg: str) -> None:
    print(time.strftime("%H:%M:%S"), msg, flush=True)


class LiveSLI:
    """Follows the load generator's log and judges the main stream per bin."""

    def __init__(self, path: pathlib.Path, cfg: Config):
        self.path, self.pos = path, 0
        self.bin = cfg.num("SLI_BIN_S")
        self.slo = cfg.num("SLI_SUCCESS")
        self.settle = cfg.num("REQUEST_TIMEOUT_S") + 0.5  # bins younger than this are incomplete
        self.bins: dict[int, list[int]] = defaultdict(lambda: [0, 0])

    def poll(self) -> None:
        if not self.path.exists():
            return
        with open(self.path) as f:
            f.seek(self.pos)
            chunk = f.read()
        cut = chunk.rfind("\n") + 1
        self.pos += cut
        for line in chunk[:cut].splitlines():
            parts = line.split(",")
            if parts[0] == "t" or len(parts) < 3 or parts[1] != "main":
                continue
            b = int(float(parts[0]) // self.bin)
            self.bins[b][1] += 1
            self.bins[b][0] += 200 <= int(parts[2]) < 300

    def last_bad(self, since: float, now: float) -> float | None:
        """End time of the last completed bad bin in [since, now - settle]."""
        hi = int((now - self.settle) // self.bin)
        for b in range(hi, int(since // self.bin) - 1, -1):
            ok, total = self.bins.get(b, (0, 0))
            if total and ok / total < self.slo:
                return (b + 1) * self.bin
        return None

    def success(self, start: float, end: float) -> float:
        ok = total = 0
        for b in range(int(start // self.bin), int(end // self.bin)):
            o, t = self.bins.get(b, (0, 0))
            ok, total = ok + o, total + t
        return ok / total if total else 1.0


class Runner:
    """Shared clients for a session of trials."""

    def __init__(self, cfg: Config):
        self.cfg = cfg
        self.cluster = Cluster(cfg)
        self.gitea = Gitea(cfg)
        self.argocd = ArgoCD(self.cluster)
        self.grafana = obs.Grafana(cfg)

    # --- per-trial reset -------------------------------------------------
    def reset(self, strategy: str, timeout: float = 900) -> str:
        """Bring the strategy's app back to the baseline release: no chaos, Git
        and live state at v1, all replicas Ready with no restarts. Returns the
        baseline pod-template hash."""
        ns = app = f"mttr-{strategy}"
        for kind, k in CHAOS.items():
            for o in self.cluster.list(k, ns):
                self.cluster.delete(k, ns, o["metadata"]["name"])
        stuck = [f"{p.metadata.namespace}/{p.metadata.name}"
                 for p in self.cluster.core.list_pod_for_all_namespaces().items
                 if p.metadata.namespace != ns and any(
                     (c.state.waiting and c.state.waiting.reason in ("ErrImagePull", "ImagePullBackOff"))
                     for c in p.status.container_statuses or [])]
        if stuck:  # a trial must not depend on a registry pull
            raise RuntimeError(f"pods waiting on an image pull: {', '.join(stuck)}")
        if not self.gitea.webhooks_active():  # a poll trial that ended early
            log("reset: re-enabling the Git webhooks")
            self.gitea.set_webhooks(True)
        path = f"{strategy}/rollout.yaml"
        content, _ = self.gitea.read(path)
        if yaml.safe_load(content) != yaml.safe_load(baseline_rollout(strategy)):
            log(f"reset: committing the baseline release to {path}")
            self.gitea.write(path, baseline_rollout(strategy), "reset: baseline release v1")
        self.argocd.set_policy(app, "auto")
        deadline = time.time() + timeout
        head = self.gitea.head()
        last_refresh = 0.0
        while time.time() < deadline:
            a = self.argocd.get(app)
            st = a.get("status") or {}
            r = self.cluster.get(ROLLOUT, ns, "mttr-app")
            rst = r.get("status") or {}
            pods = [p for p in self.cluster.core.list_namespaced_pod(ns).items if not p.metadata.deletion_timestamp]
            rss = self.cluster.apps.list_namespaced_replica_set(ns).items
            stable = rst.get("stableRS")
            restarted = [p for p in pods if any((c.restart_count or 0) > 0 for c in p.status.container_statuses or [])]
            for p in restarted:  # a restarted pod would carry the last trial into this one
                log(f"reset: replacing restarted pod {p.metadata.name}")
                self.cluster.core.delete_namespaced_pod(p.metadata.name, ns)
            # Old releases left behind by an earlier trial must go too: Argo
            # Rollouts skips the progress-deadline check while any other
            # ReplicaSet still carries a scale-down-deadline annotation, even
            # one long expired, so the history would change the next result.
            if stable and rst.get("phase") == "Healthy":
                for rs in rss:
                    h = rs.metadata.labels.get("rollouts-pod-template-hash")
                    if h != stable and (rs.spec.replicas or 0) == 0 and (rs.status.replicas or 0) == 0:
                        log(f"reset: deleting old ReplicaSet {rs.metadata.name}")
                        self.cluster.apps.delete_namespaced_replica_set(rs.metadata.name, ns)
            ok = (
                (st.get("sync") or {}).get("status") == "Synced"
                and (st.get("sync") or {}).get("revision") == head
                and (st.get("health") or {}).get("status") == "Healthy"
                and not a.get("operation")
                and rst.get("phase") == "Healthy" and not rst.get("abort")
                and stable and rst.get("currentPodHash") == stable
                and len(pods) == REPLICAS and not restarted
                and all(p.metadata.labels.get("rollouts-pod-template-hash") == stable for p in pods)
                and all(any(c.type == "Ready" and c.status == "True" for c in p.status.conditions or []) for p in pods)
                and all((rs.status.replicas or 0) == 0 for rs in rss
                        if rs.metadata.labels.get("rollouts-pod-template-hash") != stable)
            )
            if ok:
                return stable
            if time.time() - last_refresh > 30:
                self.argocd.refresh(app)
                last_refresh = time.time()
            time.sleep(2)
        raise RuntimeError(f"{app} did not return to the baseline within {timeout:.0f}s")


class Trial:
    def __init__(self, runner: Runner, strategy: str, scenario: Scenario, policy: str,
                 trial_id: str, outdir: pathlib.Path, revert_path: str = "webhook"):
        self.r, self.cfg = runner, runner.cfg
        self.strategy, self.scenario, self.policy = strategy, scenario, policy
        if revert_path not in ("webhook", "poll"):
            raise ValueError(f"unknown revert path {revert_path!r}")
        self.revert_path = revert_path
        self.id, self.out = trial_id, outdir
        self.ns = self.app = f"mttr-{strategy}"
        self.path = f"{strategy}/rollout.yaml"
        self.actions: list[dict] = []
        self.meta: dict = {"trial_id": trial_id, "strategy": strategy, "scenario": scenario.name,
                           "sync_policy": policy, "revert_path": revert_path,
                           "inject_kind": scenario.inject["kind"],
                           "release_is_healthy": scenario.release_is_healthy}
        self.delay = self.cfg.num("OPERATOR_DELAY_S")

    def act(self, action: str, **details) -> float:
        t = time.time()
        self.actions.append({"t": t, "action": action, **details})
        self.r.grafana.annotate(t, f"{self.id}: {action}", [self.id, action])
        log(f"  {action} {json.dumps(details) if details else ''}")
        return t

    # --- helpers on the watcher's live state ------------------------------
    def rollout(self) -> dict:
        return self.w.snapshot("Rollout").get("mttr-app", {})

    def application(self) -> dict:
        return self.w.snapshot("Application").get(self.app, {})

    def rs_ready(self, h: str) -> int:
        return sum(s["ready"] for s in self.w.snapshot("ReplicaSet").values() if s["hash"] == h)

    def wait(self, what: str, cond, timeout: float) -> bool:
        deadline = time.time() + timeout
        while time.time() < deadline:
            if cond():
                return True
            time.sleep(0.2)
        log(f"  timed out after {timeout:.0f}s waiting for {what}")
        return False

    def converged(self) -> bool:
        """Git, Argo CD and the cluster all at the intended release: the
        baseline, or a healthy release under test that was promoted and never
        reverted (nothing was wrong with it)."""
        a, r = self.application(), self.rollout()
        pods = [p for p in self.w.snapshot("Pod").values() if not p["deleted"]]
        want = self.v2 if self.scenario.release_is_healthy and "git_revert" not in self.meta and self.v2 else self.v1
        return (a.get("sync") == "Synced" and a.get("health") == "Healthy" and a.get("revision") == self.git_head
                and r.get("phase") == "Healthy" and not r.get("abort")
                and r.get("currentPodHash") == want and r.get("stableRS") == want
                and len(pods) == REPLICAS and all(p["ready"] and p["hash"] == want for p in pods))

    # --- the trial ----------------------------------------------------------
    def run(self) -> dict:
        cfg, sc = self.cfg, self.scenario
        self.out.mkdir(parents=True, exist_ok=True)
        log(f"trial {self.id}: {self.strategy} / {sc.name} / {self.policy}")
        self.v1 = self.r.reset(self.strategy)
        self.r.argocd.set_policy(self.app, self.policy)
        self.git_head = self.r.gitea.head()
        self.meta |= {"v1_hash": self.v1, "git_baseline": self.git_head, "operator_delay_s": self.delay,
                      "covariates": {"clock_skew": obs.clock_skew(cfg), "host": obs.host_memory(),
                                     "nodes": obs.node_usage(self.r.cluster)}}
        self.w = Watcher(self.r.cluster, self.ns, self.app).start()
        time.sleep(2)
        if self.scenario.setup:
            self.setup_history()
        self.meta["covariates"]["baseline_pods"] = {n: s["node"] for n, s in self.w.snapshot("Pod").items()}

        log_path = self.out / "requests.csv"
        streams = ["--stream", f"main={self.strategy}.mttr.lab:{cfg['LOAD_RPS']}"]
        if self.strategy == "bluegreen":
            streams += ["--stream", f"preview=preview.bluegreen.mttr.lab:{cfg['PREVIEW_RPS']}"]
        lg = subprocess.Popen([sys.executable, "-m", "mttr.loadgen", "--target", f"http://{cfg.server_ip}",
                               "--timeout", cfg["REQUEST_TIMEOUT_S"], "--out", str(log_path), *streams],
                              cwd=ROOT / "runner")
        sli = LiveSLI(log_path, cfg)
        try:
            self.meta["t_load_start"] = time.time()
            time.sleep(cfg.num("BASELINE_S"))
            sli.poll()
            self.meta["baseline_success"] = round(sli.success(self.meta["t_load_start"] + 1, time.time() - 3), 4)
            self.act("trial-start")
            self.inject()
            self.recover(sli)
        finally:
            for kind, k in CHAOS.items():
                for o in self.r.cluster.list(k, self.ns):
                    self.r.cluster.delete(k, self.ns, o["metadata"]["name"])
            if self.revert_path == "poll":
                self.r.gitea.set_webhooks(True)
            lg.terminate()
            lg.wait(timeout=10)
            self.w.stop()
            self.meta["t_end"] = time.time()
            self.meta["covariates"]["host_end"] = obs.host_memory()
            self.w.dump(self.out / "events.jsonl")
            (self.out / "actions.json").write_text(json.dumps(self.actions, indent=1))
            (self.out / "meta.json").write_text(json.dumps(self.meta, indent=1))
        return self.meta

    def setup_history(self) -> None:
        """Leave an aborted release behind, as an earlier failed deploy would:
        commit a faulty release, let Argo Rollouts abort it, revert, and wait
        until it is scaled down. Not timed; the fault comes afterwards."""
        setup = self.scenario.setup
        self.act("setup-release")
        self.r.gitea.write(self.path, release_rollout(self.strategy, f"{self.id}-setup", setup.get("patch")),
                           f"setup release {self.id}")
        if self.policy == "manual":
            self.wait("setup release OutOfSync", lambda: self.application().get("sync") == "OutOfSync", 60)
            self.r.argocd.sync(self.app)
        if not self.wait("setup release applied",
                         lambda: self.rollout().get("currentPodHash") not in (None, self.v1), 120):
            raise RuntimeError("setup release never reached the cluster")
        old = self.rollout()["currentPodHash"]
        if not self.wait("setup release aborted", lambda: self.rollout().get("abort"), 300):
            raise RuntimeError("setup release was never aborted")
        self.act("setup-revert")
        self.git_head = self.r.gitea.write(self.path, baseline_rollout(self.strategy), f"setup revert {self.id}")
        if self.policy == "manual":
            self.wait("setup revert OutOfSync", lambda: self.application().get("sync") == "OutOfSync", 60)
            self.r.argocd.sync(self.app)
        back = lambda: (self.rollout().get("phase") == "Healthy" and not self.rollout().get("abort")
                        and self.rollout().get("stableRS") == self.v1 and self.rs_ready(self.v1) >= REPLICAS)
        if not self.wait("baseline after setup", back, 300):
            raise RuntimeError("baseline did not return after the setup release")
        self.wait("setup release scaled down", lambda: not any(
            s["hash"] == old and s["replicas"] for s in self.w.snapshot("ReplicaSet").values()), 180)
        rs = [r for r in self.r.cluster.apps.list_namespaced_replica_set(self.ns).items
              if r.metadata.labels.get("rollouts-pod-template-hash") == old]
        kept = bool(rs) and "scale-down-deadline" in (rs[0].metadata.annotations or {})
        self.meta |= {"setup_hash": old, "history_precondition": kept}
        if not kept:  # the history this condition needs is absent: not a measurement of it
            self.meta["confirmed"] = False
        self.act("setup-done", hash=old, annotation_kept=kept)

    def inject(self) -> None:
        sc, inj = self.scenario, self.scenario.inject
        if sc.phase == "during-rollout":
            patch = inj.get("patch") if inj["kind"] in ("git-commit", "chaos-mesh") else None
            content = release_rollout(self.strategy, self.id, patch)
            self.act("commit-release", release=self.id)
            sha = self.r.gitea.write(self.path, content, f"release {self.id}")
            self.git_head = sha
            self.meta["git_release"] = sha
            if self.policy == "manual":  # the operator deploys the release; not part of recovery
                self.wait("release OutOfSync", lambda: self.application().get("sync") == "OutOfSync", 60)
                self.r.argocd.sync(self.app)
                self.act("deploy-sync")
            if not self.wait("release applied",
                             lambda: self.rollout().get("currentPodHash") not in (None, self.v1), 120):
                raise RuntimeError("release never reached the cluster")
            self.v2 = self.rollout()["currentPodHash"]
            self.meta["v2_hash"] = self.v2
            if inj["kind"] == "git-commit":
                self.meta["t_inject_action"] = self.act("release-applied", hash=self.v2)
                return
            # Runtime faults land once the release under test is serving.
            def live():
                r = self.rollout()
                if self.strategy == "bluegreen":
                    return self.rs_ready(self.v2) >= REPLICAS
                return (r.get("currentStepIndex") or 0) >= 1 and self.rs_ready(self.v2) >= 1
            if not self.wait("release live", live, 180):
                raise RuntimeError("release under test never became live")
            target = {"TARGET_HASH": self.v2}
        else:
            self.v2 = None
            target = {"TARGET_HASH": self.v1}
        if sc.target.get("pick") == "one":
            pods = sorted(n for n, p in self.w.snapshot("Pod").items() if p["hash"] == self.v1 and not p["deleted"])
            target["TARGET_POD"] = random.choice(pods)
            self.meta["target_pod"] = target["TARGET_POD"]
        if inj["kind"] == "chaos-mesh":
            self.create_chaos(inj["manifest"], target)
            self.meta["t_inject_action"] = self.act("chaos-created", **target)
        elif inj["kind"] == "kubectl-patch":
            self.r.cluster.json_patch(inj["resource"], self.ns, "mttr-app", inj["patch"])
            self.meta["t_inject_action"] = self.act("live-patched")
        self.meta["v2_hash"] = self.v2

    def create_chaos(self, manifest: str, target: dict) -> list[tuple[str, str]]:
        """Create a scenario's Chaos Mesh resources; returns (kind, name) pairs."""
        values = {"TRIAL_ID": self.id.lower(), "NAMESPACE": self.ns, **target}
        made = []
        for doc in self.scenario.render(manifest, values):
            doc.setdefault("metadata", {}).setdefault("labels", {})["mttr.lab/trial"] = self.id.lower()
            self.r.cluster.create(CHAOS[doc["kind"]], self.ns, doc)
            made.append((doc["kind"], doc["metadata"]["name"]))
        return made

    def recover(self, sli: LiveSLI) -> None:
        """Watch the recovery stack act; play the scripted operator; stop once
        the system has stayed recovered for RESTORE_HOLD_S."""
        cfg, sc = self.cfg, self.scenario
        t0 = self.meta["t_inject_action"]
        hold = cfg.num("RESTORE_HOLD_S")
        stop_after = seconds(sc.inject["stop_after"]) if sc.inject.get("stop_after") else None
        git_is_baseline = sc.phase == "baseline"
        t_failure = t_oos = None
        confirmed = None if sc.spec.get("confirm", {}).get("kind", "none") == "none" else False
        stopped = False
        good_since = None
        late_chaos: list[tuple[float | None, list[tuple[str, str]]]] = []
        while True:
            now = time.time()
            sli.poll()
            r, a = self.rollout(), self.application()
            if stop_after and not stopped and now >= t0 + stop_after:
                for kind, k in CHAOS.items():
                    for o in self.r.cluster.list(k, self.ns):
                        self.r.cluster.delete(k, self.ns, o["metadata"]["name"])
                stopped = True
                self.act("chaos-stopped")
            if confirmed is False:
                confirmed = self.confirm()
                if confirmed:
                    self.act("fault-confirmed")
                elif now > t0 + seconds(sc.spec["confirm"].get("within", "60s")):
                    confirmed = None
                    self.act("fault-not-confirmed")
                    self.meta["confirmed"] = False

            # Scripted operator. It knows nothing about the scenario: it reacts
            # to what an on-call engineer would see, after a fixed delay.
            if not git_is_baseline:
                promoted_bad = (self.v2 and r.get("phase") == "Healthy" and r.get("stableRS") == self.v2
                                and sli.success(now - 30, now - sli.settle) < sli.slo)
                if t_failure is None and (r.get("abort") or promoted_bad):
                    t_failure = now
                    self.act("operator-sees-failure", reason="rollout aborted" if r.get("abort") else "SLO alert")
                if t_failure and now >= t_failure + self.delay:
                    for extra in sc.also:  # faults that hit the revert's way back to the cluster
                        names = self.create_chaos(extra["manifest"], {"TARGET_HASH": self.v2 or self.v1})
                        until = now + seconds(extra["stop_after"]) if extra.get("stop_after") else None
                        late_chaos.append((until, names))
                        self.act("revert-chaos-created", manifests=names)
                    if self.revert_path == "poll":
                        self.r.gitea.set_webhooks(False)
                        self.act("webhooks-disabled")
                    self.act("operator-revert-commit")
                    self.git_head = self.r.gitea.write(self.path, baseline_rollout(self.strategy),
                                                       f"revert release {self.id}")
                    self.meta["git_revert"] = self.git_head
                    git_is_baseline = True
            for i, (until, names) in enumerate(late_chaos):
                if until and now >= until:
                    for kind, name in names:
                        self.r.cluster.delete(CHAOS[kind], self.ns, name)
                    late_chaos[i] = (None, [])
                    self.act("revert-chaos-stopped")
            if self.policy == "manual":
                if a.get("sync") == "OutOfSync" and not a.get("operation") and a.get("opPhase") != "Running":
                    if t_oos is None:
                        t_oos = now
                        self.act("operator-sees-outofsync")
                    elif now >= t_oos + self.delay:
                        self.r.argocd.sync(self.app)
                        self.act("operator-sync")
                        t_oos = None
                        time.sleep(1)
                elif a.get("sync") == "Synced":
                    t_oos = None

            # Recovered: converged for `hold` seconds, and no SLO breach in the
            # last `hold` seconds of settled request bins.
            late_done = all(until is None for until, _ in late_chaos)
            if self.converged() and (not stop_after or stopped) and late_done:
                good_since = good_since or now
            else:
                good_since = None
            last_bad = sli.last_bad(t0, now) or t0
            if good_since and now - good_since >= hold and now - sli.settle - last_bad >= hold:
                self.act("trial-end")
                self.meta["censored"] = False
                return
            if now - t0 > sc.timeout:
                self.act("trial-timeout")
                self.meta["censored"] = True
                return
            time.sleep(0.25)

    def confirm(self) -> bool:
        c = self.scenario.spec["confirm"]
        target = self.v2 or self.v1
        pods = [p for p in self.w.snapshot("Pod").values() if p["hash"] == target]
        if c["kind"] == "pod-restarts":
            return any(p["restarts"] > 0 for p in pods)
        if c["kind"] == "pod-waiting":
            return any(p["waiting"] in c["reasons"] for p in pods)
        if c["kind"] == "chaos-injected":
            for kind in CHAOS:
                for s in self.w.snapshot(kind).values():
                    if any(x["type"] == "AllInjected" and x["status"] == "True" for x in s["conditions"]):
                        return True
        return False
