"""The Git and Argo CD side of a trial: commits to Gitea, sync policy, manual sync."""
from __future__ import annotations

import base64
import copy
import time

import requests
import yaml

from .cluster import APPLICATION, Cluster
from .config import ROOT, Config

AUTHOR = {"name": "operator", "email": "operator@mttr.lab"}


class Gitea:
    def __init__(self, cfg: Config):
        self.base = f"{cfg.gitea_url}/api/v1/repos/{cfg['GITEA_ADMIN_USER']}/{cfg['GITOPS_REPO']}"
        self.s = requests.Session()
        self.s.auth = (cfg["GITEA_ADMIN_USER"], cfg["GITEA_ADMIN_PASSWORD"])

    def read(self, path: str) -> tuple[str, str]:
        r = self.s.get(f"{self.base}/contents/{path}", params={"ref": "main"}, timeout=10)
        r.raise_for_status()
        j = r.json()
        return base64.b64decode(j["content"]).decode(), j["sha"]

    def write(self, path: str, content: str, message: str) -> str:
        """Commit `content` to `path` on main; returns the commit SHA."""
        _, sha = self.read(path)
        body = {"content": base64.b64encode(content.encode()).decode(), "sha": sha, "message": message,
                "branch": "main", "author": AUTHOR, "committer": AUTHOR}
        r = self.s.put(f"{self.base}/contents/{path}", json=body, timeout=15)
        r.raise_for_status()
        return r.json()["commit"]["sha"]

    def head(self) -> str:
        r = self.s.get(f"{self.base}/branches/main", timeout=10)
        r.raise_for_status()
        return r.json()["commit"]["id"]


def baseline_rollout(strategy: str) -> str:
    return (ROOT / "gitops" / "apps" / strategy / "rollout.yaml").read_text()


def release_rollout(strategy: str, release: str, patch: dict | None) -> str:
    """The baseline Rollout with a new release id and an optional fault patch
    (env vars and/or image) applied to the app container."""
    doc = yaml.safe_load(baseline_rollout(strategy))
    c = doc["spec"]["template"]["spec"]["containers"][0]
    env = [e for e in c["env"] if e["name"] != "RELEASE"]
    env.insert(0, {"name": "RELEASE", "value": release})
    for k, v in ((patch or {}).get("env") or {}).items():
        env.append({"name": k, "value": str(v)})
    c["env"] = env
    if (patch or {}).get("image"):
        c["image"] = patch["image"]
    return yaml.safe_dump(doc, sort_keys=False)


class ArgoCD:
    def __init__(self, cluster: Cluster):
        self.c = cluster

    def get(self, app: str) -> dict:
        return self.c.get(APPLICATION, "argocd", app)

    def set_policy(self, app: str, policy: str) -> None:
        automated = {"prune": True, "selfHeal": True} if policy == "auto" else None
        self.c.patch(APPLICATION, "argocd", app, {"spec": {"syncPolicy": {"automated": automated}}})

    def sync(self, app: str) -> float:
        """What an operator's "Sync" does: start a sync operation to the branch head."""
        body = {"operation": {"initiatedBy": {"username": "operator"},
                              "sync": {"revision": "main", "prune": True,
                                       "syncOptions": ["CreateNamespace=true", "RespectIgnoreDifferences=true"]}}}
        self.c.patch(APPLICATION, "argocd", app, body)
        return time.time()

    def refresh(self, app: str) -> None:
        self.c.patch(APPLICATION, "argocd", app,
                     {"metadata": {"annotations": {"argocd.argoproj.io/refresh": "normal"}}})
