#!/usr/bin/env bash
# Install (or upgrade in place) Gitea and Argo CD at pinned chart versions.
source "$(dirname "$0")/lib/common.sh"
load_config
require_cmd kubectl helm

kc get nodes >/dev/null || die "cannot reach context '$KUBE_CONTEXT'; run 'make kubeconfig' first"

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
