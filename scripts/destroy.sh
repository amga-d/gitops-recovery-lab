#!/usr/bin/env bash
# Tear down the lab: VMs, their disks, rendered cloud-init, and the kube context.
# Keeps the base image and .env so the next `make up` reuses them.
source "$(dirname "$0")/lib/common.sh"
load_config

if [[ "${FORCE:-0}" != 1 ]]; then
  read -r -p "Destroy VMs ${ALL_VMS[*]} and their disks? [y/N] " ans
  [[ "$ans" =~ ^[Yy]$ ]] || die "aborted"
fi

for vm in "${AGENTS[@]}" "$SERVER_VM"; do
  vm_exists "$vm" || continue
  log "Removing $vm"
  [[ "$(vm_state "$vm")" == running ]] && "${VIRSH[@]}" destroy "$vm" >/dev/null
  "${VIRSH[@]}" undefine "$vm" --remove-all-storage >/dev/null
done

rm -rf "$STATE_DIR/cloud-init" "$STATE_DIR/disks"

if [[ -f "$KUBECONFIG_FILE" ]]; then
  log "Removing '$KUBE_CONTEXT' from $KUBECONFIG_FILE"
  for kind in context cluster user; do
    kubectl --kubeconfig "$KUBECONFIG_FILE" config "delete-$kind" "$KUBE_CONTEXT" >/dev/null 2>&1 || true
  done
fi
log "Lab destroyed"
