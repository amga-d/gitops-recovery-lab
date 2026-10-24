#!/usr/bin/env bash
# Install (or upgrade in place) the platform at pinned chart versions: Gitea,
# kube-prometheus-stack, Argo CD, Argo Rollouts and Chaos Mesh.
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

# Network fetches here run over a possibly flaky uplink, so they are retried.
add_repo() {
  helm repo list 2>/dev/null | awk 'NR > 1 {print $1}' | grep -qx "$1" && return
  retry 3 helm repo add "$1" "$2" >/dev/null
}

# An interrupted helm run (host reboot, Ctrl-C) leaves its release "pending-*",
# and helm then refuses every later operation on it. Nothing else runs helm
# against this lab, so a pending release is always a dead one: roll it back,
# or uninstall it if it never finished its first install.
recover_release() {
  local release=$1 ns=$2 last
  last="$(hlm -n "$ns" history "$release" --max 1 -o json 2>/dev/null)" || return 0
  case "$last" in
    *'"status":"pending-install"'*)
      warn "$release: first install was interrupted; uninstalling it to start over"
      hlm -n "$ns" uninstall "$release" --wait --timeout 10m ;;
    *'"status":"pending-'*)
      warn "$release: last operation was interrupted; rolling back to the previous revision"
      hlm -n "$ns" rollback "$release" --wait --timeout 10m ;;
  esac
}

# fetch_chart <repo/chart> <version>: download the pinned chart once, with
# retries, and print the local archive. Installs then never depend on the
# uplink, and the exact chart that was installed stays on disk.
fetch_chart() {
  local chart=$1 version=$2 dir="$STATE_DIR/charts"
  local tgz="$dir/${chart#*/}-$version.tgz"
  if [[ ! -s "$tgz" ]]; then
    mkdir -p "$dir"
    retry 5 helm pull "$chart" --version "$version" -d "$dir" >&2 || die "cannot download $chart $version"
  fi
  echo "$tgz"
}

# install_chart <release> <namespace> <repo/chart> <version> <values-file> [helm args...]
install_chart() {
  local release=$1 ns=$2 chart=$3 version=$4 values=$5 tgz
  shift 5
  log "Installing $chart $version"
  tgz="$(fetch_chart "$chart" "$version")"
  recover_release "$release" "$ns"
  hlm upgrade --install "$release" "$tgz" -n "$ns" --create-namespace \
    -f "$values" --wait --timeout 15m "$@"
}

log "Adding Helm repos"
add_repo gitea-charts https://dl.gitea.io/charts/
add_repo argo https://argoproj.github.io/argo-helm
add_repo prometheus-community https://prometheus-community.github.io/helm-charts
add_repo chaos-mesh https://charts.chaos-mesh.org
retry 3 helm repo update gitea-charts argo prometheus-community chaos-mesh >/dev/null

install_chart gitea gitea gitea-charts/gitea "$GITEA_CHART_VERSION" \
  "$ROOT/platform/gitea/values.yaml" \
  --set-string gitea.admin.username="$GITEA_ADMIN_USER" \
  --set-string gitea.admin.password="$GITEA_ADMIN_PASSWORD" \
  --set service.http.nodePort="$GITEA_NODEPORT"
kc -n gitea rollout status deploy/gitea --timeout=5m

# Before the other charts: it installs the ServiceMonitor CRD they rely on.
install_chart kube-prometheus-stack monitoring prometheus-community/kube-prometheus-stack \
  "$KUBE_PROMETHEUS_STACK_CHART_VERSION" "$ROOT/platform/kube-prometheus-stack/values.yaml" \
  --set prometheus.service.nodePort="$PROMETHEUS_NODEPORT" \
  --set grafana.service.nodePort="$GRAFANA_NODEPORT" \
  --set-string grafana.adminPassword="$GRAFANA_ADMIN_PASSWORD"
# The operator creates the Prometheus StatefulSet after helm returns.
wait_until 300 "Prometheus StatefulSet" kc -n monitoring get statefulset prometheus-kube-prometheus-stack-prometheus
kc -n monitoring rollout status statefulset/prometheus-kube-prometheus-stack-prometheus --timeout=10m

install_chart argocd argocd argo/argo-cd "$ARGOCD_CHART_VERSION" \
  "$ROOT/platform/argocd/values.yaml" \
  --set server.service.nodePortHttp="$ARGOCD_NODEPORT" \
  --set-string configs.secret.githubSecret="$ARGOCD_WEBHOOK_SECRET" \
  --set-string configs.secret.gogsSecret="$ARGOCD_WEBHOOK_SECRET"
kc -n argocd rollout status deploy/argocd-server --timeout=5m

install_chart argo-rollouts argo-rollouts argo/argo-rollouts "$ARGO_ROLLOUTS_CHART_VERSION" \
  "$ROOT/platform/argo-rollouts/values.yaml"

install_chart chaos-mesh chaos-mesh chaos-mesh/chaos-mesh "$CHAOS_MESH_CHART_VERSION" \
  "$ROOT/platform/chaos-mesh/values.yaml"
kc -n chaos-mesh rollout status daemonset/chaos-daemon --timeout=10m

log "Platform installed"
