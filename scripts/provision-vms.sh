#!/usr/bin/env bash
# Create the k3s server and agent VMs if they don't exist, start them if stopped.
# Safe to re-run: existing domains are never recreated.
source "$(dirname "$0")/lib/common.sh"
load_config
require_cmd virsh virt-install qemu-img envsubst curl

BASE_IMAGE="$ROOT/$BASE_IMAGE"
RENDER_DIR="$STATE_DIR/cloud-init"
DISK_DIR="$STATE_DIR/disks"
SSH_PUBKEY_FILE="${SSH_PUBKEY_FILE/#\~/$HOME}"
[[ -f "$SSH_PUBKEY_FILE" ]] || die "SSH public key not found: $SSH_PUBKEY_FILE"
SSH_PUBKEY="$(<"$SSH_PUBKEY_FILE")"

# Only these variables are substituted; anything else in the templates is left alone.
# VM_NAME is the VM being rendered, so one agent template serves every agent.
TEMPLATE_VARS='${SERVER_VM} ${VM_NAME} ${SERVER_SSH_USER} ${AGENT_SSH_USER} ${SSH_PUBKEY}
${K3S_VERSION} ${K3S_TOKEN} ${CLUSTER_CIDR} ${CALICO_VERSION} ${SERVER_IP} ${SERVER_NODE_TAINT}'
export SERVER_VM SERVER_SSH_USER AGENT_SSH_USER SSH_PUBKEY \
  K3S_VERSION K3S_TOKEN CLUSTER_CIDR CALICO_VERSION SERVER_NODE_TAINT

ensure_base_image() {
  [[ -f "$BASE_IMAGE" ]] && return
  log "Downloading base image to $BASE_IMAGE"
  mkdir -p "$(dirname "$BASE_IMAGE")"
  curl -fL --progress-bar -o "$BASE_IMAGE.part" "$BASE_IMAGE_URL"
  mv "$BASE_IMAGE.part" "$BASE_IMAGE"
}

# ensure_vm <name> <role: server|agent> <memory-mb> <vcpus> <disk-size>
ensure_vm() {
  local vm=$1 role=$2 memory=$3 vcpus=$4 disk_size=$5
  if vm_exists "$vm"; then
    if [[ "$(vm_state "$vm")" == running ]]; then
      log "$vm already running"
    else
      log "Starting existing VM $vm"
      "${VIRSH[@]}" start "$vm" >/dev/null
    fi
    return
  fi

  ensure_base_image
  log "Creating VM $vm"
  # Rendered per VM, not per role, so agents don't overwrite each other's files.
  local out="$RENDER_DIR/$vm"
  mkdir -p "$out" "$DISK_DIR"
  local disk="$DISK_DIR/$vm.qcow2"
  local tmpl="$ROOT/infra/cloud-init/$role"
  VM_NAME=$vm envsubst "$TEMPLATE_VARS" <"$tmpl/user-data.yaml.tmpl" >"$out/user-data.yaml"
  VM_NAME=$vm envsubst "$TEMPLATE_VARS" <"$tmpl/meta-data.yaml.tmpl" >"$out/meta-data.yaml"

  # Absolute backing path so the overlay works from any directory.
  qemu-img create -q -f qcow2 -F qcow2 -b "$BASE_IMAGE" "$disk" "$disk_size"

  virt-install --connect qemu:///system \
    --name "$vm" \
    --memory "$memory" --vcpus "$vcpus" \
    --disk "path=$disk,format=qcow2" \
    --cloud-init "user-data=$out/user-data.yaml,meta-data=$out/meta-data.yaml" \
    --os-variant "$VM_OS_VARIANT" \
    --network "network=$VM_NETWORK" \
    --import --graphics none --noautoconsole
}

ensure_vm "$SERVER_VM" server "$SERVER_MEMORY_MB" "$SERVER_VCPUS" "$SERVER_DISK_SIZE"

log "Waiting for $SERVER_VM to get an IP"
SERVER_IP="$(wait_vm_ip "$SERVER_VM")"
export SERVER_IP
log "$SERVER_VM IP: $SERVER_IP"

# The agents' cloud-init needs the server IP, so they are created after it.
for agent in "${AGENTS[@]}"; do
  ensure_vm "$agent" agent "$AGENT_MEMORY_MB" "$AGENT_VCPUS" "$AGENT_DISK_SIZE"
done
