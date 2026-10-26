"""Grafana annotations, Prometheus queries and the per-trial covariates."""
from __future__ import annotations

import pathlib
import subprocess
import time

import requests

from .cluster import Cluster
from .config import Config, vm_ip


class Grafana:
    def __init__(self, cfg: Config):
        self.url = cfg.grafana_url + "/api/annotations"
        self.auth = ("admin", cfg["GRAFANA_ADMIN_PASSWORD"])

    def annotate(self, t: float, text: str, tags: list[str]) -> None:
        try:
            requests.post(self.url, auth=self.auth, timeout=3,
                          json={"time": int(t * 1000), "tags": ["mttr", *tags], "text": text})
        except requests.RequestException:
            pass  # annotations are a visual aid; never fail a trial over one


class Prometheus:
    def __init__(self, cfg: Config):
        self.url = cfg.prometheus_url + "/api/v1"

    def range(self, query: str, start: float, end: float, step: float = 15) -> list[dict]:
        r = requests.get(f"{self.url}/query_range", timeout=20,
                         params={"query": query, "start": start, "end": end, "step": step})
        r.raise_for_status()
        return r.json()["data"]["result"]

    def instant(self, query: str, t: float | None = None) -> list[dict]:
        params = {"query": query} | ({"time": t} if t else {})
        r = requests.get(f"{self.url}/query", params=params, timeout=20)
        r.raise_for_status()
        return r.json()["data"]["result"]


def _chrony_offset_ms(text: str) -> float | None:
    """System clock offset from NTP, from `chronyc tracking` (positive = fast)."""
    for line in text.splitlines():
        if line.startswith("System time"):
            parts = line.split(":", 1)[1].split()
            value = float(parts[0]) * 1000
            return round(value if parts[2] == "fast" else -value, 3)
    return None


def clock_skew(cfg: Config) -> dict[str, dict]:
    """Each node's clock offset from the host's, as the difference of the two
    chrony offsets from NTP. (An SSH round trip is far too slow and asymmetric
    to measure millisecond offsets directly.)"""
    host = subprocess.run(["chronyc", "tracking"], capture_output=True, text=True).stdout
    host_off = _chrony_offset_ms(host)
    key = str(pathlib.Path(cfg["SSH_KEY"]).expanduser())
    out = {"host": {"ntp_offset_ms": host_off}}
    for vm, user in cfg.nodes:
        r = subprocess.run(["ssh", "-i", key, "-o", "BatchMode=yes", "-o", "StrictHostKeyChecking=no",
                            "-o", "UserKnownHostsFile=/dev/null", "-o", "LogLevel=ERROR", "-o", "ConnectTimeout=5",
                            f"{user}@{vm_ip(vm)}", "chronyc tracking"], capture_output=True, text=True, timeout=20)
        node_off = _chrony_offset_ms(r.stdout)
        out[vm] = {"ntp_offset_ms": node_off,
                   "offset_ms": None if node_off is None or host_off is None else round(node_off - host_off, 3)}
    return out


def host_memory() -> dict:
    info = {}
    for line in open("/proc/meminfo"):
        k, v = line.split(":", 1)
        info[k] = int(v.split()[0]) // 1024  # MiB
    vm = dict(line.split() for line in open("/proc/vmstat"))
    return {"host_mem_available_mib": info["MemAvailable"],
            "host_pswpin": int(vm["pswpin"]), "host_pswpout": int(vm["pswpout"]),
            "host_swap_used_mib": info["SwapTotal"] - info["SwapFree"],
            "host_load1": round(__import__("os").getloadavg()[0], 2)}


CPU_MILLI = {"n": 1e-6, "u": 1e-3, "m": 1.0}
MEM_MIB = {"Ki": 1 / 1024, "Mi": 1.0, "Gi": 1024.0}


def cpu_millicores(q: str) -> float:
    # metrics-server picks the suffix per value (n, u or m), so any of them can appear.
    if q[-1:] in CPU_MILLI:
        return float(q[:-1]) * CPU_MILLI[q[-1]]
    return float(q) * 1000


def mem_mib(q: str) -> float:
    if q[-2:] in MEM_MIB:
        return float(q[:-2]) * MEM_MIB[q[-2:]]
    return float(q) / 2**20


def node_usage(cluster: Cluster) -> dict[str, dict]:
    try:
        items = cluster.custom.list_cluster_custom_object("metrics.k8s.io", "v1beta1", "nodes")["items"]
    except Exception:
        return {}
    out = {}
    for n in items:
        # A covariate must never stop a trial, so an unreadable value is recorded as missing.
        try:
            usage = {"cpu_m": round(cpu_millicores(n["usage"]["cpu"])),
                     "mem_mib": round(mem_mib(n["usage"]["memory"]))}
        except (KeyError, ValueError):
            usage = {"cpu_m": None, "mem_mib": None}
        out[n["metadata"]["name"]] = usage
    return out
