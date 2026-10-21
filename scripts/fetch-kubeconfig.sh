#!/usr/bin/env bash
# Copy the kubeconfig from the k3s server and merge it into the local kubeconfig
# as cluster/user/context "$KUBE_CONTEXT". Other contexts are left untouched.
source "$(dirname "$0")/lib/common.sh"
load_config
require_cmd kubectl ssh

SERVER_IP="$(vm_ip "$SERVER_VM")"
[[ -n "$SERVER_IP" ]] || die "$SERVER_VM has no IP; is it running?"

tmp="$(mktemp)"
merged="$(mktemp)"
trap 'rm -f "$tmp" "$merged"' EXIT

log "Fetching kubeconfig from $SERVER_VM ($SERVER_IP)"
wait_until 600 "/etc/rancher/k3s/k3s.yaml on $SERVER_VM" vm_ssh "$SERVER_IP" test -s /etc/rancher/k3s/k3s.yaml
vm_ssh "$SERVER_IP" cat /etc/rancher/k3s/k3s.yaml >"$tmp"

# k3s names everything "default" and points at localhost; rename and repoint.
sed -i \
  -e "s#https://127.0.0.1:6443#https://${SERVER_IP}:6443#" \
  -e "s#^\(\s*\(- \)\?\(name\|cluster\|user\|current-context\):\) default\$#\1 ${KUBE_CONTEXT}#" \
  "$tmp"

mkdir -p "$(dirname "$KUBECONFIG_FILE")"
if [[ -f "$KUBECONFIG_FILE" ]]; then
  backup="$STATE_DIR/kubeconfig.bak.$(date +%Y%m%d%H%M%S)"
  cp -p "$KUBECONFIG_FILE" "$backup"
  log "Backed up $KUBECONFIG_FILE to $backup"
  # Drop stale entries: a reprovisioned cluster has new certificates.
  kubectl --kubeconfig "$KUBECONFIG_FILE" config delete-context "$KUBE_CONTEXT" >/dev/null 2>&1 || true
  kubectl --kubeconfig "$KUBECONFIG_FILE" config delete-cluster "$KUBE_CONTEXT" >/dev/null 2>&1 || true
  kubectl --kubeconfig "$KUBECONFIG_FILE" config delete-user "$KUBE_CONTEXT" >/dev/null 2>&1 || true
  KUBECONFIG="$KUBECONFIG_FILE:$tmp" kubectl config view --flatten >"$merged"
else
  cp "$tmp" "$merged"
fi

install -m 600 "$merged" "$KUBECONFIG_FILE"
kubectl --kubeconfig "$KUBECONFIG_FILE" config use-context "$KUBE_CONTEXT" >/dev/null
log "Context '$KUBE_CONTEXT' merged into $KUBECONFIG_FILE and set as current"
