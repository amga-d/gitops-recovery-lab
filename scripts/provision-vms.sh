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
TEMPLATE_VARS='${SERVER_VM} ${AGENT_VM} ${SERVER_SSH_USER} ${AGENT_SSH_USER} ${SSH_PUBKEY}
${K3S_VERSION} ${K3S_TOKEN} ${CLUSTER_CIDR} ${CALICO_VERSION} ${SERVER_IP}'
export SERVER_VM AGENT_VM SERVER_SSH_USER AGENT_SSH_USER SSH_PUBKEY \
  K3S_VERSION K3S_TOKEN CLUSTER_CIDR CALICO_VERSION

ensure_base_image() {
  [[ -f "$BASE_IMAGE" ]] && return
  log "Downloading base image to $BASE_IMAGE"
  mkdir -p "$(dirname "$BASE_IMAGE")"
  curl -fL --progress-bar -o "$BASE_IMAGE.part" "$BASE_IMAGE_URL"
  mv "$BASE_IMAGE.part" "$BASE_IMAGE"
}

# ensure_vm <name> <role: server|agent>
ensure_vm() {
  local vm=$1 role=$2
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
  mkdir -p "$RENDER_DIR/$role" "$DISK_DIR"
  local disk="$DISK_DIR/$vm.qcow2"
  local tmpl="$ROOT/infra/cloud-init/$role"
  envsubst "$TEMPLATE_VARS" <"$tmpl/user-data.yaml.tmpl" >"$RENDER_DIR/$role/user-data.yaml"
  envsubst "$TEMPLATE_VARS" <"$tmpl/meta-data.yaml.tmpl" >"$RENDER_DIR/$role/meta-data.yaml"

  # Absolute backing path so the overlay works from any directory.
  sudo qemu-img create -q -f qcow2 -F qcow2 -b "$BASE_IMAGE" "$disk" "$VM_DISK_SIZE"

  sudo virt-install --connect qemu:///system \
    --name "$vm" \
    --memory "$VM_MEMORY_MB" --vcpus "$VM_VCPUS" \
    --disk "path=$disk,format=qcow2" \
    --cloud-init "user-data=$RENDER_DIR/$role/user-data.yaml,meta-data=$RENDER_DIR/$role/meta-data.yaml" \
    --os-variant "$VM_OS_VARIANT" \
    --network "network=$VM_NETWORK" \
    --import --graphics none --noautoconsole
}

ensure_vm "$SERVER_VM" server

log "Waiting for $SERVER_VM to get an IP"
SERVER_IP="$(wait_vm_ip "$SERVER_VM")"
export SERVER_IP
log "$SERVER_VM IP: $SERVER_IP"

# The agent's cloud-init needs the server IP, so it is created second.
ensure_vm "$AGENT_VM" agent
