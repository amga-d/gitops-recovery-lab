#!/usr/bin/env bash
# Install (or upgrade in place) Gitea and Argo CD at pinned chart versions.
source "$(dirname "$0")/lib/common.sh"
load_config
require_cmd kubectl helm

kc get nodes >/dev/null || die "cannot reach context '$KUBE_CONTEXT'; run 'make kubeconfig' first"

# k3s's local-path provisioner creates and deletes each volume's directory with
# a helper pod bound to the volume's node. Its template has no tolerations, so on
# the tainted server, where the platform's volumes live, the helper is evicted
# and the PVC never binds. k3s may reset the ConfigMap on restart, so re-check
# on every run.
tolerate_local_path_helper() {
  local helper patch
  helper="$(kc -n kube-system get configmap local-path-config -o jsonpath='{.data.helperPod\.yaml}')"
  grep -q CriticalAddonsOnly <<<"$helper" && return
  log "Letting the local-path helper pod tolerate the server taint"
  helper="$(sed '/^spec:/a\  tolerations:\n  - key: CriticalAddonsOnly\n    operator: Exists' <<<"$helper")"
  patch="$(printf 'data:\n  helperPod.yaml: |\n%s\n' "$(sed 's/^/    /' <<<"$helper")")"
  kc -n kube-system patch configmap local-path-config --type merge -p "$patch" >/dev/null
  # Restart so the provisioner reads the new template now, not after the
  # kubelet's next ConfigMap sync. Generous timeout: the new pod may land on a
  # node that still has to pull the image.
  kc -n kube-system rollout restart deploy/local-path-provisioner >/dev/null
  kc -n kube-system rollout status deploy/local-path-provisioner --timeout=5m
}
tolerate_local_path_helper

log "Adding Helm repos"
helm repo add gitea-charts https://dl.gitea.io/charts/ --force-update >/dev/null
helm repo add argo https://argoproj.github.io/argo-helm --force-update >/dev/null
helm repo update gitea-charts argo >/dev/null

log "Installing Gitea chart $GITEA_CHART_VERSION"
hlm upgrade --install gitea gitea-charts/gitea \
  -n gitea --create-namespace \
  --version "$GITEA_CHART_VERSION" \
  -f "$ROOT/platform/gitea/values.yaml" \
  --set-string gitea.admin.username="$GITEA_ADMIN_USER" \
  --set-string gitea.admin.password="$GITEA_ADMIN_PASSWORD" \
  --set service.http.nodePort="$GITEA_NODEPORT" \
  --wait --timeout 10m
kc -n gitea rollout status deploy/gitea --timeout=5m

log "Installing Argo CD chart $ARGOCD_CHART_VERSION"
hlm upgrade --install argocd argo/argo-cd \
  -n argocd --create-namespace \
  --version "$ARGOCD_CHART_VERSION" \
  -f "$ROOT/platform/argocd/values.yaml" \
  --set server.service.nodePortHttp="$ARGOCD_NODEPORT" \
  --wait --timeout 10m
kc -n argocd rollout status deploy/argocd-server --timeout=5m

log "Platform installed"
