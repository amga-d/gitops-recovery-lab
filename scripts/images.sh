#!/usr/bin/env bash
# Keep every container image the lab needs on the host, so a rebuilt cluster
# never has to pull from a registry over the host's network link.
#   save   pull, on the host, every image the pods on the server and the first
#          agent run, into .state/images/{server,agent}.tar (run once on a working cluster)
#   load   import those archives into every node (start.sh runs this when they exist)
source "$(dirname "$0")/lib/common.sh"
load_config
require_cmd ssh docker jq

IMAGES_DIR="$STATE_DIR/images"
mkdir -p "$IMAGES_DIR"

# node_user <vm>
node_user() { [[ "$1" == "$SERVER_VM" ]] && echo "$SERVER_SSH_USER" || echo "$AGENT_SSH_USER"; }

# node_images <vm>: the images the pods on <vm> run, plus k3s's pause image
node_images() {
  local vm=$1 ip
  kc get pods -A --field-selector "spec.nodeName=$vm" \
    -o jsonpath='{range .items[*]}{range .spec.initContainers[*]}{.image}{"\n"}{end}{range .spec.containers[*]}{.image}{"\n"}{end}{end}'
  ip="$(vm_ip "$vm")"
  SSH_USER=$(node_user "$vm") vm_ssh "$ip" sudo k3s crictl images -o json \
    | jq -r '.images[].repoTags[]? | select(test("pause"))'
}

# save_node <vm> <archive>: k3s's containerd discards layer blobs once they are
# unpacked, so its images cannot be exported; pull them on the host instead.
save_node() {
  local vm=$1 archive=$2 img refs=()
  while read -r img; do
    [[ -n "$img" ]] || continue
    if ! docker image inspect "$img" >/dev/null 2>&1; then
      log "Pulling $img"
      retry 5 docker pull -q "$img" >/dev/null || die "could not pull $img"
    fi
    refs+=("$img")
  done < <(node_images "$vm" | sort -u)
  ((${#refs[@]})) || die "no images found for $vm"
  log "Saving ${#refs[@]} images for $vm to $archive"
  docker save -o "$archive.part" "${refs[@]}"
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
