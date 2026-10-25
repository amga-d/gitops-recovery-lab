#!/usr/bin/env bash
# Keep every container image the lab needs on the host, so a rebuilt cluster
# never has to pull from a registry over the host's network link.
#   save   export all images from the server's and the first agent's containerd
#          into .state/images/{server,agent}.tar (run once on a working cluster)
#   load   import those archives into every node (start.sh runs this when they exist)
source "$(dirname "$0")/lib/common.sh"
load_config
require_cmd ssh

IMAGES_DIR="$STATE_DIR/images"
mkdir -p "$IMAGES_DIR"

# node_user <vm>
node_user() { [[ "$1" == "$SERVER_VM" ]] && echo "$SERVER_SSH_USER" || echo "$AGENT_SSH_USER"; }

# save_node <vm> <archive>
save_node() {
  local vm=$1 archive=$2 ip refs
  ip="$(vm_ip "$vm")"
  [[ -n "$ip" ]] || die "$vm has no IP; is it running?"
  # Named references only; the sha256: aliases point at the same content.
  refs="$(SSH_USER=$(node_user "$vm") vm_ssh "$ip" sudo k3s ctr -n k8s.io images ls -q | grep -v '^sha256:' | tr '\n' ' ')"
  [[ -n "$refs" ]] || die "no images found on $vm"
  log "Exporting $(wc -w <<<"$refs") images from $vm to $archive"
  # shellcheck disable=SC2086  # refs is a word list on purpose
  SSH_USER=$(node_user "$vm") vm_ssh "$ip" sudo k3s ctr -n k8s.io images export --platform linux/amd64 - $refs >"$archive.part"
  mv "$archive.part" "$archive"
}

# load_node <vm> <archive>
load_node() {
  local vm=$1 archive=$2 ip
  ip="$(vm_ip "$vm")"
  [[ -n "$ip" ]] || die "$vm has no IP; is it running?"
  log "Importing $(basename "$archive") into $vm"
  SSH_USER=$(node_user "$vm") vm_ssh "$ip" sudo k3s ctr -n k8s.io images import --platform linux/amd64 - <"$archive" >/dev/null
}

case "${1:-}" in
  save)
    save_node "$SERVER_VM" "$IMAGES_DIR/server.tar"
    save_node "${AGENTS[0]}" "$IMAGES_DIR/agent.tar"
    ls -lh "$IMAGES_DIR"/*.tar
    ;;
  load)
    [[ -f "$IMAGES_DIR/server.tar" && -f "$IMAGES_DIR/agent.tar" ]] \
      || { warn "no saved images in $IMAGES_DIR; nodes will pull from registries (run 'make images' once)"; exit 0; }
    load_node "$SERVER_VM" "$IMAGES_DIR/server.tar"
    for agent in "${AGENTS[@]}"; do
      load_node "$agent" "$IMAGES_DIR/agent.tar"
    done
    ;;
  *) sed -n '2,6p' "$0"; exit 1 ;;
esac
