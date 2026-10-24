#!/usr/bin/env bash
# Build the sample app image on the host and import it into every node's
# containerd. Re-running replaces the image in place.
source "$(dirname "$0")/lib/common.sh"
load_config
require_cmd go docker ssh

log "Building $APP_IMAGE"
(cd "$ROOT/app" && CGO_ENABLED=0 go build -trimpath -ldflags='-s -w' -o mttr-app .)
docker build -q -t "$APP_IMAGE" "$ROOT/app" >/dev/null
archive="$STATE_DIR/mttr-app.tar"
docker save "$APP_IMAGE" -o "$archive"
log "Image $(docker image inspect --format '{{.Id}}' "$APP_IMAGE")"

for vm in "${ALL_VMS[@]}"; do
  user=$AGENT_SSH_USER
  [[ "$vm" == "$SERVER_VM" ]] && user=$SERVER_SSH_USER
  ip="$(vm_ip "$vm")"
  [[ -n "$ip" ]] || die "$vm has no IP; is it running?"
  log "Importing into $vm ($ip)"
  SSH_USER=$user vm_ssh "$ip" sudo k3s ctr -n k8s.io images import - <"$archive" >/dev/null
  SSH_USER=$user vm_ssh "$ip" sudo k3s crictl images | grep -q "${APP_IMAGE%:*}" \
    || die "$APP_IMAGE missing on $vm after import"
done
