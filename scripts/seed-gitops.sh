#!/usr/bin/env bash
# Put the experiment's desired state in place: the Git repository in Gitea
# (from gitops/apps), the push webhook to Argo CD, the shared analysis
# templates and recording rules, and one Argo CD Application per app.
# Idempotent: re-running brings everything back to the baseline release.
source "$(dirname "$0")/lib/common.sh"
load_config
require_cmd kubectl git curl jq

kc get nodes >/dev/null || die "cannot reach context '$KUBE_CONTEXT'; run 'make up' first"
SERVER_IP="$(vm_ip "$SERVER_VM")"
GITEA="http://${SERVER_IP}:${GITEA_NODEPORT}"
OWNER=$GITEA_ADMIN_USER
# What Argo CD and the webhook payload both call the repository.
REPO_URL="http://gitea-http.gitea.svc.cluster.local:3000/${OWNER}/${GITOPS_REPO}.git"

gitea() { curl -sf -u "${GITEA_ADMIN_USER}:${GITEA_ADMIN_PASSWORD}" -H 'Content-Type: application/json' "$@"; }

wait_until 300 "Gitea API" gitea "$GITEA/api/v1/version"
if ! gitea "$GITEA/api/v1/repos/$OWNER/$GITOPS_REPO" >/dev/null; then
  log "Creating Gitea repository $OWNER/$GITOPS_REPO"
  gitea -X POST "$GITEA/api/v1/user/repos" \
    -d "{\"name\":\"$GITOPS_REPO\",\"private\":false,\"default_branch\":\"main\"}" >/dev/null
fi

# Sync the baseline manifests into the repository; commit only if they differ.
work="$STATE_DIR/gitops-seed"
rm -rf "$work"
push_url="http://${GITEA_ADMIN_USER}:${GITEA_ADMIN_PASSWORD}@${SERVER_IP}:${GITEA_NODEPORT}/${OWNER}/${GITOPS_REPO}.git"
git clone -q "$push_url" "$work" 2>/dev/null || { mkdir -p "$work" && git -C "$work" init -q -b main && git -C "$work" remote add origin "$push_url"; }
find "$work" -mindepth 1 -maxdepth 1 ! -name .git -exec rm -rf {} +
cp -r "$ROOT/gitops/apps/." "$work/"
git -C "$work" add -A
if ! git -C "$work" diff --cached --quiet || ! git -C "$work" rev-parse -q --verify HEAD >/dev/null; then
  log "Committing the baseline manifests"
  git -C "$work" -c user.name=operator -c user.email=operator@mttr.lab commit -q -m "baseline: release v1"
  git -C "$work" push -q origin main
else
  log "Repository already holds the baseline manifests"
fi
rm -rf "$work"

hook_url="http://argocd-server.argocd.svc.cluster.local/api/webhook"
if ! gitea "$GITEA/api/v1/repos/$OWNER/$GITOPS_REPO/hooks" | jq -e --arg u "$hook_url" 'any(.[]; .config.url == $u)' >/dev/null; then
  log "Adding the push webhook to Argo CD"
  gitea -X POST "$GITEA/api/v1/repos/$OWNER/$GITOPS_REPO/hooks" -d "$(jq -n --arg u "$hook_url" --arg s "$ARGOCD_WEBHOOK_SECRET" \
    '{type:"gitea", active:true, events:["push"], config:{url:$u, content_type:"json", secret:$s}}')" >/dev/null
fi

log "Applying the analysis templates and recording rules"
kc apply -f "$ROOT/gitops/platform/" >/dev/null

# application <name> <path> <namespace>: auto-sync is the default; the runner
# switches the policy per trial.
application() {
  cat <<EOF
apiVersion: argoproj.io/v1alpha1
kind: Application
metadata:
  name: $1
  namespace: argocd
spec:
  project: default
  source:
    repoURL: $REPO_URL
    targetRevision: main
    path: $2
  destination:
    server: https://kubernetes.default.svc
    namespace: $3
  syncPolicy:
    automated:
      prune: true
      selfHeal: true
    syncOptions:
      - CreateNamespace=true
      - RespectIgnoreDifferences=true
  ignoreDifferences:
    - group: traefik.io
      kind: TraefikService
      jsonPointers:
        - /spec/weighted/services
---
EOF
}
log "Applying the Argo CD Applications"
{
  application mttr-backend backend mttr-backend
  for s in canary bluegreen rolling; do application "mttr-$s" "$s" "mttr-$s"; done
} | kc apply -f - >/dev/null

for app in mttr-backend mttr-canary mttr-bluegreen mttr-rolling; do
  log "Waiting for $app to be Synced and Healthy"
  wait_until 600 "$app Synced+Healthy" bash -c \
    "[[ \$(kubectl --kubeconfig '$KUBECONFIG_FILE' --context '$KUBE_CONTEXT' -n argocd get application $app \
      -o jsonpath='{.status.sync.status}/{.status.health.status}') == Synced/Healthy ]]"
done
log "GitOps baseline in place"
