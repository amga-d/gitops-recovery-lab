#!/usr/bin/env bash
# Print VM state, cluster nodes, platform pods, and access URLs/credentials.
source "$(dirname "$0")/lib/common.sh"
load_config

log "VMs"
for vm in "$SERVER_VM" "$AGENT_VM"; do
  if vm_exists "$vm"; then
    printf '  %-12s %-10s %s\n' "$vm" "$(vm_state "$vm")" "$(vm_ip "$vm")"
  else
    printf '  %-12s %s\n' "$vm" "not created"
  fi
done

kc get nodes >/dev/null 2>&1 || { warn "context '$KUBE_CONTEXT' unreachable"; exit 0; }

log "Nodes"
kc get nodes -o wide
log "Platform pods"
kc get pods -n gitea 2>/dev/null || true
kc get pods -n argocd 2>/dev/null || true

NODE_IP="$(vm_ip "$SERVER_VM")"
argocd_pw="$(kc -n argocd get secret argocd-initial-admin-secret \
  -o jsonpath='{.data.password}' 2>/dev/null | base64 -d || true)"

log "Access"
cat <<EOF
  Gitea   http://${NODE_IP}:${GITEA_NODEPORT}   user: ${GITEA_ADMIN_USER}  password: see GITEA_ADMIN_PASSWORD in .env
  Argo CD http://${NODE_IP}:${ARGOCD_NODEPORT}   user: admin       password: ${argocd_pw:-<not available>}
  kubectl --context ${KUBE_CONTEXT} get pods -A
EOF
