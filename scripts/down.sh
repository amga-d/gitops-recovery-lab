#!/usr/bin/env bash

source "$(dirname "$0")/lib/common.sh"
load_config

for vm in "${AGENTS[@]}" $SERVER_VM; do 
    if vm_exists "$vm"; then
        if [[ "$(vm_state "$vm")" == running ]]; then
        log "Shutting down $vm"
        "${VIRSH[@]}" shutdown "$vm" || true
        else
        log "$vm is not running"
        fi
    else
        log "$vm does not exist"
    fi
done
