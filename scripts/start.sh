#!/usr/bin/env bash
# Idempotent full bring-up: VMs -> k3s ready -> kubeconfig -> Gitea + Argo CD.
#   --skip-platform   stop after the cluster is ready and the kubeconfig is merged
source "$(dirname "$0")/lib/common.sh"
load_config

SKIP_PLATFORM=0
for arg in "$@"; do
  case "$arg" in
    --skip-platform) SKIP_PLATFORM=1 ;;
    -h|--help) sed -n '2,3p' "$0"; exit 0 ;;
    *) die "unknown option: $arg" ;;
  esac
done

require_cmd virsh virt-install qemu-img envsubst curl ssh kubectl helm openssl
[[ -f "$SSH_KEY" ]] || die "SSH key not found: $SSH_KEY"

"$ROOT/scripts/provision-vms.sh"

# wait_cloud_init <vm> <ssh-user>: fail fast if first-boot provisioning broke,
# instead of hanging later while waiting for nodes that will never join.
wait_cloud_init() {
  local vm=$1 ip
  ip="$(wait_vm_ip "$vm")"
  log "Waiting for SSH on $vm ($ip)"
  SSH_USER=$2 wait_until 300 "SSH on $vm" vm_ssh "$ip" true
  log "Waiting for cloud-init on $vm (first boot installs k3s)"
  if ! SSH_USER=$2 vm_ssh "$ip" cloud-init status --wait >/dev/null; then
    SSH_USER=$2 vm_ssh "$ip" sudo tail -n 15 /var/log/cloud-init-output.log >&2 || true
    die "cloud-init failed on $vm; full log: ssh -i $SSH_KEY $2@$ip sudo less /var/log/cloud-init-output.log"
  fi
}

wait_cloud_init "$SERVER_VM" "$SERVER_SSH_USER"
wait_cloud_init "$AGENT_VM" "$AGENT_SSH_USER"

"$ROOT/scripts/fetch-kubeconfig.sh"

log "Waiting for both nodes to register and become Ready"
wait_until 600 "2 nodes registered" bash -c \
  "[[ \$(kubectl --kubeconfig '$KUBECONFIG_FILE' --context '$KUBE_CONTEXT' get nodes --no-headers 2>/dev/null | wc -l) -ge 2 ]]"
kc wait node --all --for=condition=Ready --timeout=600s

((SKIP_PLATFORM)) || "$ROOT/scripts/install-platform.sh"
"$ROOT/scripts/status.sh"
