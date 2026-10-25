"""Lab and trial configuration, read from the same config.env and .env as the scripts."""
from __future__ import annotations

import os
import pathlib
import subprocess
from dataclasses import dataclass, field

ROOT = pathlib.Path(__file__).resolve().parents[2]
STRATEGIES = ("canary", "bluegreen", "rolling")
SYNC_POLICIES = ("auto", "manual")


def _read_env(path: pathlib.Path) -> dict[str, str]:
    out: dict[str, str] = {}
    if not path.exists():
        return out
    for raw in path.read_text().splitlines():
        line = raw.split(" #", 1)[0].strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, value = line.split("=", 1)
        out[key.strip()] = value.strip().strip('"')
    return out


def vm_ip(vm: str) -> str:
    out = subprocess.run(["virsh", "-c", "qemu:///system", "domifaddr", vm],
                         capture_output=True, text=True, check=True).stdout
    for line in out.splitlines():
        if "ipv4" in line:
            return line.split()[3].split("/")[0]
    raise RuntimeError(f"{vm} has no IP; is it running?")


@dataclass
class Config:
    env: dict[str, str]
    server_ip: str = ""
    results_dir: pathlib.Path = field(default_factory=lambda: ROOT / ".state" / "results")

    def __getitem__(self, key: str) -> str:
        return self.env[key]

    def num(self, key: str) -> float:
        return float(self.env[key])

    @property
    def kubeconfig(self) -> str:
        return os.path.expanduser(self.env["KUBECONFIG_FILE"])

    @property
    def context(self) -> str:
        return self.env["KUBE_CONTEXT"]

    @property
    def gitea_url(self) -> str:
        return f"http://{self.server_ip}:{self.env['GITEA_NODEPORT']}"

    @property
    def prometheus_url(self) -> str:
        return f"http://{self.server_ip}:{self.env['PROMETHEUS_NODEPORT']}"

    @property
    def grafana_url(self) -> str:
        return f"http://{self.server_ip}:{self.env['GRAFANA_NODEPORT']}"

    @property
    def nodes(self) -> list[tuple[str, str]]:
        """(vm, ssh user) for every node."""
        out = [(self.env["SERVER_VM"], self.env["SERVER_SSH_USER"])]
        out += [(a, self.env["AGENT_SSH_USER"]) for a in self.env["AGENT_VMS"].split()]
        return out


def load() -> Config:
    env = _read_env(ROOT / "config.env") | _read_env(ROOT / ".env")
    cfg = Config(env=env)
    # Offline work (re-extraction, plan dry runs) needs no running VM; anything
    # that talks to the lab fails clearly on the empty address instead.
    try:
        cfg.server_ip = vm_ip(env["SERVER_VM"])
    except (subprocess.CalledProcessError, RuntimeError):
        cfg.server_ip = ""
    # Results usually live outside the repo; the path goes in the environment or .env.
    results = os.environ.get("RESULTS_DIR") or env.get("RESULTS_DIR")
    if results:
        cfg.results_dir = pathlib.Path(results).expanduser()
    return cfg
