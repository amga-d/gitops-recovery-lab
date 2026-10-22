# shellcheck shell=bash
# Shared helpers for the lab scripts. Source this, don't execute it.

set -euo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd)"
STATE_DIR="$ROOT/.state"
VIRSH=(virsh -c qemu:///system)

log()  { printf '\033[1;34m==>\033[0m %s\n' "$*"; }
warn() { printf '\033[1;33mWARN:\033[0m %s\n' "$*" >&2; }
die()  { printf '\033[1;31mERROR:\033[0m %s\n' "$*" >&2; exit 1; }

require_cmd() {
  local missing=()
  for c in "$@"; do command -v "$c" >/dev/null 2>&1 || missing+=("$c"); done
  ((${#missing[@]} == 0)) || die "missing required commands: ${missing[*]}"
}

SECRET_VARS=(K3S_TOKEN GITEA_ADMIN_PASSWORD GRAFANA_ADMIN_PASSWORD)

# Load committed defaults, then local secrets. Any secret missing or empty in
# .env is generated and appended, so secrets stay stable across re-runs without
# ever being committed, even if .env is absent, empty, or partially filled.
load_config() {
  # shellcheck source=../../config.env
  source "$ROOT/config.env"
  (umask 077; touch "$ROOT/.env")
  set -a
  # shellcheck disable=SC1091
  source "$ROOT/.env"
  set +a
  local var
  for var in "${SECRET_VARS[@]}"; do
    [[ -n "${!var:-}" ]] && continue
    require_cmd openssl
    log "Generating $var in $ROOT/.env"
    sed -i "/^${var}=/d" "$ROOT/.env"
    printf '%s=%s\n' "$var" "$(openssl rand -hex 16)" >>"$ROOT/.env"
    set -a
    # shellcheck disable=SC1091
    source "$ROOT/.env"
    set +a
  done
  for var in "${SECRET_VARS[@]}"; do
    [[ -n "${!var:-}" ]] || die "$var is empty after loading $ROOT/.env"
  done
  read -ra AGENTS <<<"$AGENT_VMS"
  ((${#AGENTS[@]} > 0)) || die "AGENT_VMS in config.env lists no agents"
  # shellcheck disable=SC2034  # used by the scripts that source this file
  ALL_VMS=("$SERVER_VM" "${AGENTS[@]}")
  SSH_KEY="${SSH_KEY/#\~/$HOME}"
  KUBECONFIG_FILE="${KUBECONFIG_FILE/#\~/$HOME}"
  mkdir -p "$STATE_DIR"
}

# wait_until <timeout-seconds> <description> <command...>
wait_until() {
  local timeout=$1 desc=$2; shift 2
  local deadline=$((SECONDS + timeout))
  until "$@" >/dev/null 2>&1; do
    ((SECONDS < deadline)) || die "timed out after ${timeout}s waiting for: $desc"
    sleep 5
  done
}

vm_exists() { "${VIRSH[@]}" dominfo "$1" >/dev/null 2>&1; }
vm_state()  { "${VIRSH[@]}" domstate "$1" 2>/dev/null | head -n1; }

vm_ip() {
  "${VIRSH[@]}" domifaddr "$1" 2>/dev/null | awk '/ipv4/ {split($4, a, "/"); print a[1]; exit}'
}

# Blocks until the VM has a DHCP lease, then prints its IP.
wait_vm_ip() {
  local vm=$1 ip=""
  local deadline=$((SECONDS + ${2:-300}))
  until ip=$(vm_ip "$vm") && [[ -n "$ip" ]]; do
    ((SECONDS < deadline)) || die "timed out waiting for an IP on $vm"
    sleep 5
  done
  echo "$ip"
}

# vm_ssh <ip> <command...>   (set SSH_USER to log in as someone other than the server user)
vm_ssh() {
  local ip=$1; shift
  ssh -i "$SSH_KEY" -o BatchMode=yes -o ConnectTimeout=5 \
    -o StrictHostKeyChecking=no -o UserKnownHostsFile=/dev/null -o LogLevel=ERROR \
    "${SSH_USER:-$SERVER_SSH_USER}@${ip}" "$@"
}

# Always pin the context so nothing lands on another cluster in ~/.kube/config.
kc()    { kubectl --kubeconfig "$KUBECONFIG_FILE" --context "$KUBE_CONTEXT" "$@"; }
hlm()   { helm --kubeconfig "$KUBECONFIG_FILE" --kube-context "$KUBE_CONTEXT" "$@"; }
