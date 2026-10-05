#!/usr/bin/env bash
set -euo pipefail

cd "$(dirname "$0")"
BASE_IMG="$PWD/noble-base.img"

SERVER_DOMAIN_NAME=k3s-server
AGENT_DOMAIN_NAME=k3s-agent

export K3S_TOKEN=my-lab-secret

# Server disk (absolute backing path so the overlay works from any directory)
sudo qemu-img create -f qcow2 -F qcow2 -b "$BASE_IMG" k3s-server/k3s-server.qcow2 10G

sudo virt-install \
  --name ${SERVER_DOMAIN_NAME} \
  --memory 2048 --vcpus 2 \
  --disk path=k3s-server/k3s-server.qcow2,format=qcow2 \
  --cloud-init user-data=k3s-server/server-user-data.yaml,meta-data=k3s-server/server-meta-data.yaml \
  --os-variant ubuntu24.04 \
  --network network=default \
  --import --graphics none --noautoconsole

# Wait for the server to get a DHCP lease
SERVER_IP=""
until [ -n "$SERVER_IP" ]; do
  sleep 5
  SERVER_IP=$(virsh -c qemu:///system domifaddr ${SERVER_DOMAIN_NAME} | awk '/ipv4/ {split($4, a, "/"); print a[1]}')
done
export SERVER_IP
echo "Server IP: $SERVER_IP"

# k3s-agent disk
sudo qemu-img create -f qcow2 -F qcow2 -b "$BASE_IMG" k3s-agent/k3s-agent.qcow2 10G

envsubst '${SERVER_IP} ${K3S_TOKEN}' <k3s-agent/agent-user-data.yaml.tmpl >k3s-agent/agent-user-data.yaml

sudo virt-install \
  --name ${AGENT_DOMAIN_NAME} \
  --memory 2048 --vcpus 2 \
  --disk path=k3s-agent/k3s-agent.qcow2,format=qcow2 \
  --cloud-init user-data=k3s-agent/agent-user-data.yaml,meta-data=k3s-agent/agent-meta-data.yaml \
  --os-variant ubuntu24.04 \
  --network network=default \
  --import --graphics none --noautoconsole
