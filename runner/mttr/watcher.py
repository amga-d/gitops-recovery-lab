"""Records every state change of the objects a trial involves, stamped with the
host's wall clock in milliseconds when the API server delivers it.

These are the primary timestamps; Prometheus (15 s scrape) is only the
cross-check. Each event keeps a compact summary of the object, enough to replay
the trial's state afterwards.
"""
from __future__ import annotations

import json
import threading
import time
from typing import Callable

from kubernetes import client, watch

from .cluster import ANALYSISRUN, APPLICATION, CHAOS, ROLLOUT, Cluster

HASH = "rollouts-pod-template-hash"


def _conds(status: dict) -> list[dict]:
    return [{k: c.get(k) for k in ("type", "status", "reason", "lastTransitionTime", "message")}
            for c in (status.get("conditions") or [])]


def summarize_rollout(o: dict) -> dict:
    st = o.get("status") or {}
    spec = o.get("spec") or {}
    tmpl = spec.get("template", {}).get("spec", {}).get("containers", [{}])[0]
    return {
        "generation": o["metadata"].get("generation"),
        "observedGeneration": st.get("observedGeneration"),
        "phase": st.get("phase"), "message": st.get("message"), "abort": bool(st.get("abort")),
        "currentPodHash": st.get("currentPodHash"), "stableRS": st.get("stableRS"),
        "currentStepIndex": st.get("currentStepIndex"),
        "replicas": st.get("replicas"), "updatedReplicas": st.get("updatedReplicas"),
        "readyReplicas": st.get("readyReplicas"), "availableReplicas": st.get("availableReplicas"),
        "canaryWeight": (((st.get("canary") or {}).get("weights") or {}).get("canary") or {}).get("weight"),
        "activeSelector": (st.get("blueGreen") or {}).get("activeSelector"),
        "previewSelector": (st.get("blueGreen") or {}).get("previewSelector"),
        "pauseConditions": [p.get("reason") for p in st.get("pauseConditions") or []],
        "conditions": _conds(st),
        "image": tmpl.get("image"),
        "env": {e["name"]: e.get("value") for e in tmpl.get("env", []) if "value" in e},
    }


def summarize_analysisrun(o: dict) -> dict:
    st = o.get("status") or {}
    metrics = []
    for m in st.get("metricResults") or []:
        last = (m.get("measurements") or [{}])[-1]
        metrics.append({"name": m.get("name"), "phase": m.get("phase"), "count": m.get("count"),
                        "failed": m.get("failed"), "error": m.get("error"),
                        "lastValue": last.get("value"), "lastFinishedAt": last.get("finishedAt")})
    return {"phase": st.get("phase"), "message": st.get("message"), "startedAt": st.get("startedAt"),
            "hash": o["metadata"].get("labels", {}).get(HASH), "metrics": metrics}


def summarize_application(o: dict) -> dict:
    st = o.get("status") or {}
    op = st.get("operationState") or {}
    return {
        "sync": (st.get("sync") or {}).get("status"), "revision": (st.get("sync") or {}).get("revision"),
        "health": (st.get("health") or {}).get("status"), "reconciledAt": st.get("reconciledAt"),
        "operation": bool(o.get("operation")),
        "opPhase": op.get("phase"), "opStartedAt": op.get("startedAt"), "opFinishedAt": op.get("finishedAt"),
        "opRevision": (op.get("syncResult") or {}).get("revision"),
        "opInitiatedBy": (op.get("operation") or {}).get("initiatedBy"),
        "automated": bool(((o.get("spec") or {}).get("syncPolicy") or {}).get("automated")),
    }


def summarize_chaos(o: dict) -> dict:
    st = o.get("status") or {}
    records = []
    for r in ((st.get("experiment") or {}).get("containerRecords") or []):
        events = r.get("events") or []
        records.append({"id": r.get("id"), "phase": r.get("phase"), "injectedCount": r.get("injectedCount"),
                        "firstEvent": events[0] if events else None})
    return {"kind": o.get("kind"), "conditions": _conds(st), "records": records,
            "desiredPhase": (st.get("experiment") or {}).get("desiredPhase"),
            "labels": o["metadata"].get("labels", {}), "deleted": bool(o["metadata"].get("deletionTimestamp"))}


def summarize_pod(o: dict) -> dict:
    st = o.get("status") or {}
    ready = next((c for c in st.get("conditions") or [] if c.get("type") == "Ready"), {})
    cs = (st.get("containerStatuses") or [{}])[0]
    waiting = (cs.get("state") or {}).get("waiting") or {}
    return {"hash": o["metadata"].get("labels", {}).get(HASH), "node": (o.get("spec") or {}).get("nodeName"),
            "phase": st.get("phase"), "ready": ready.get("status") == "True",
            "readySince": ready.get("lastTransitionTime"), "restarts": cs.get("restartCount", 0),
            "waiting": waiting.get("reason"), "deleted": bool(o["metadata"].get("deletionTimestamp"))}


def summarize_rs(o: dict) -> dict:
    st = o.get("status") or {}
    return {"hash": o["metadata"].get("labels", {}).get(HASH), "spec": (o.get("spec") or {}).get("replicas"),
            "replicas": st.get("replicas", 0), "ready": st.get("readyReplicas", 0),
            "available": st.get("availableReplicas", 0)}


class Watcher:
    """Background watches on one app namespace and its Argo CD Application."""

    def __init__(self, cluster: Cluster, ns: str, app: str):
        self.cluster, self.ns, self.app = cluster, ns, app
        self.events: list[dict] = []
        self.state: dict[tuple[str, str], dict] = {}
        self._lock = threading.Lock()
        self._stop = threading.Event()
        self._threads: list[threading.Thread] = []
        self._watches: list[watch.Watch] = []
        c = cluster
        custom = lambda k, ns, **kw: lambda **kw2: c.custom.list_namespaced_custom_object(
            k.group, k.version, ns, k.plural, **kw, **kw2)
        self.sources: list[tuple[str, Callable, Callable]] = [
            ("Rollout", custom(ROLLOUT, ns), summarize_rollout),
            ("AnalysisRun", custom(ANALYSISRUN, ns), summarize_analysisrun),
            ("Application", custom(APPLICATION, "argocd", field_selector=f"metadata.name={app}"),
             summarize_application),
            ("Pod", lambda **kw: c.core.list_namespaced_pod(ns, **kw), summarize_pod),
            ("ReplicaSet", lambda **kw: c.apps.list_namespaced_replica_set(ns, **kw), summarize_rs),
        ] + [(k, custom(kind, ns), summarize_chaos) for k, kind in CHAOS.items()]

    def emit(self, kind: str, etype: str, obj: dict, summarize: Callable) -> None:
        t = time.time()
        name = obj["metadata"]["name"]
        summary = summarize(obj)
        with self._lock:
            key = (kind, name)
            if etype == "DELETED":
                self.state.pop(key, None)
            else:
                # Only record real changes: re-lists repeat unchanged objects.
                if self.state.get(key) == summary and etype != "ADDED":
                    return
                self.state[key] = summary
            self.events.append({"t": t, "kind": kind, "type": etype, "name": name, "s": summary})

    def _run(self, kind: str, list_fn: Callable, summarize: Callable) -> None:
        rv = None
        while not self._stop.is_set():
            try:
                if rv is None:
                    lst = self.cluster.to_dict(list_fn())
                    rv = lst["metadata"]["resourceVersion"]
                    for item in lst["items"]:
                        item.setdefault("kind", kind)
                        self.emit(kind, "LIST", item, summarize)
                w = watch.Watch()
                self._watches.append(w)
                for ev in w.stream(list_fn, resource_version=rv, timeout_seconds=20, allow_watch_bookmarks=True):
                    obj = self.cluster.to_dict(ev["object"])
                    rv = obj["metadata"].get("resourceVersion", rv)
                    if ev["type"] in ("BOOKMARK", "ERROR"):
                        if ev["type"] == "ERROR":
                            rv = None
                        continue
                    obj.setdefault("kind", kind)
                    self.emit(kind, ev["type"], obj, summarize)
                    if self._stop.is_set():
                        break
            except client.ApiException as e:
                if e.status == 410:
                    rv = None  # history expired: list again
                else:
                    time.sleep(1)
            except Exception:  # connection reset etc.: resume from the last version
                time.sleep(0.5)

    def start(self) -> "Watcher":
        for kind, fn, summ in self.sources:
            t = threading.Thread(target=self._run, args=(kind, fn, summ), daemon=True, name=f"watch-{kind}")
            t.start()
            self._threads.append(t)
        return self

    def stop(self) -> None:
        self._stop.set()
        for w in self._watches:
            w.stop()

    def snapshot(self, kind: str) -> dict[str, dict]:
        with self._lock:
            return {n: s for (k, n), s in self.state.items() if k == kind}

    def dump(self, path) -> None:
        with self._lock, open(path, "w") as f:
            for e in sorted(self.events, key=lambda e: e["t"]):
                f.write(json.dumps(e) + "\n")
