"""Kubernetes access pinned to the lab context, plus the object kinds the runner uses."""
from __future__ import annotations

import json
import subprocess
from dataclasses import dataclass

from kubernetes import client, config as kconfig

from .config import Config


@dataclass(frozen=True)
class Kind:
    group: str
    version: str
    plural: str


ROLLOUT = Kind("argoproj.io", "v1alpha1", "rollouts")
ANALYSISRUN = Kind("argoproj.io", "v1alpha1", "analysisruns")
APPLICATION = Kind("argoproj.io", "v1alpha1", "applications")
CHAOS = {
    "Schedule": Kind("chaos-mesh.org", "v1alpha1", "schedules"),
    "PodChaos": Kind("chaos-mesh.org", "v1alpha1", "podchaos"),
    "NetworkChaos": Kind("chaos-mesh.org", "v1alpha1", "networkchaos"),
}


class Cluster:
    def __init__(self, cfg: Config):
        self.cfg = cfg
        self.api_client = kconfig.new_client_from_config(config_file=cfg.kubeconfig, context=cfg.context)
        self.core = client.CoreV1Api(self.api_client)
        self.apps = client.AppsV1Api(self.api_client)
        self.custom = client.CustomObjectsApi(self.api_client)

    def to_dict(self, obj) -> dict:
        return obj if isinstance(obj, dict) else self.api_client.sanitize_for_serialization(obj)

    # --- custom objects -------------------------------------------------
    def get(self, kind: Kind, ns: str, name: str) -> dict:
        return self.custom.get_namespaced_custom_object(kind.group, kind.version, ns, kind.plural, name)

    def list(self, kind: Kind, ns: str, **kw) -> list[dict]:
        return self.custom.list_namespaced_custom_object(kind.group, kind.version, ns, kind.plural, **kw)["items"]

    def patch(self, kind: Kind, ns: str, name: str, body: dict) -> dict:
        return self.custom.patch_namespaced_custom_object(kind.group, kind.version, ns, kind.plural, name, body)

    def create(self, kind: Kind, ns: str, body: dict) -> dict:
        return self.custom.create_namespaced_custom_object(kind.group, kind.version, ns, kind.plural, body)

    def delete(self, kind: Kind, ns: str, name: str) -> None:
        try:
            self.custom.delete_namespaced_custom_object(kind.group, kind.version, ns, kind.plural, name)
        except client.ApiException as e:
            if e.status != 404:
                raise

    # --- kubectl, for patch types the Python client handles awkwardly ----
    def kubectl(self, *args: str, input: str | None = None) -> str:
        cmd = ["kubectl", "--kubeconfig", self.cfg.kubeconfig, "--context", self.cfg.context, *args]
        return subprocess.run(cmd, input=input, capture_output=True, text=True, check=True).stdout

    def json_patch(self, resource: str, ns: str, name: str, ops: list[dict]) -> None:
        self.kubectl("-n", ns, "patch", resource, name, "--type", "json", "-p", json.dumps(ops))
