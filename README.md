# GitOps MTTR lab

A reproducible three-node k3s cluster on libvirt VMs, with Gitea and Argo CD
installed, for measuring mean time to recovery (MTTR) under GitOps.

- k3s with embedded etcd and the Calico CNI
- Gitea as the in-cluster Git server
- Argo CD as the GitOps controller
- A pluggable failure-scenario layer (see `scenarios/`)

## Topology

| VM            | Role                                   | Size            | Runs                         |
|---------------|----------------------------------------|-----------------|------------------------------|
| `k3s-server`  | Control plane + etcd, tainted `CriticalAddonsOnly=true:NoExecute` | 4 vCPU / 4 GiB | k3s addons, Traefik, Gitea, Argo CD |
| `k3s-agent-1` | Worker                                 | 2 vCPU / 2 GiB  | Applications                 |
| `k3s-agent-2` | Worker                                 | 2 vCPU / 2 GiB  | Applications                 |

Platform components carry a toleration for `CriticalAddonsOnly` and a
`nodeSelector` for `node-role.kubernetes.io/control-plane: "true"`, so they
run only on the server. Traefik is pinned the same way with a `HelmChartConfig`
written by the server's cloud-init. Anything without the toleration (the
applications) is scheduled on the agents.

## Prerequisites

On the host: `libvirt`/`virsh`, `virt-install`, `qemu-img`, `envsubst` (gettext),
`curl`, `ssh`, `openssl`, `kubectl`, `helm`, `make`. You also need a working
libvirt `default` network and an SSH key pair at `~/.ssh/k3s_id_ed25519`
(path set in `config.env`).

## Quickstart

```bash
make up        # create/start VMs, wait for k3s, merge kubeconfig, install Gitea + Argo CD
make status    # VM IPs, nodes, pods, URLs and credentials
kubectl --context k3s get pods -A
```

Every step is idempotent, so run `make up` again after a reboot or a partial
failure. Run `make help` to list all targets.

| Target            | What it does                                                      |
|-------------------|-------------------------------------------------------------------|
| `make up`         | Full bring-up (`scripts/start.sh`; `--skip-platform` available)   |
| `make vms`        | Create the VMs if missing, start them if stopped                  |
| `make kubeconfig` | Copy `/etc/rancher/k3s/k3s.yaml` from the server and merge it as context, cluster and user `k3s` |
| `make platform`   | `helm upgrade --install` Gitea and Argo CD at pinned versions     |
| `make down`       | Gracefully shut down the VMs                                      |
| `make destroy`    | Delete the VMs, disks and the `k3s` kube entries (`FORCE=1` skips the prompt) |

## Configuration

- `config.env` (committed) holds the VM names (`SERVER_VM`, `AGENT_VMS`),
  per-role sizes, the server taint, and the pinned versions of k3s, Calico and
  both Helm charts.
- `.env` (git-ignored) holds `K3S_TOKEN` and `GITEA_ADMIN_PASSWORD`. It is
  generated with random values on first run; see `.env.example`.

Before merging, `make kubeconfig` backs up `~/.kube/config` to
`.state/kubeconfig.bak.<timestamp>`. Only the `k3s` entries are replaced.

## Layout

```
├── Makefile              entry point
├── config.env            pinned versions and VM settings
├── scripts/              start, provision-vms, fetch-kubeconfig, install-platform, status, destroy
│   └── lib/common.sh     shared helpers (config loading, virsh, kubectl/helm pinned to the k3s context)
├── infra/cloud-init/     server and agent cloud-init templates
├── platform/             Helm values for gitea and argocd
├── scenarios/            pluggable failure scenarios
├── docs/                 manual install and provisioning notes
└── .state/               generated (git-ignored): base image, disks, rendered cloud-init, backups
```

## Access

| Service | URL                       | User         |
|---------|---------------------------|--------------|
| Gitea   | `http://<server-ip>:30300` | `researcher` |
| Argo CD | `http://<server-ip>:30080` | `admin`      |

`make status` prints the server IP and the Argo CD initial admin password.
