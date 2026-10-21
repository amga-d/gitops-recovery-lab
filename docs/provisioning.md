# Setting up 2 vm for k3s server and agent node using cloud image with cloud-locals

## Provisioning

### 1- starting with creating 10G disk 

1- Server disk
```bash
sudo qemu-img create -f qcow2 -F qcow2 -b noble-base.img k3s-server.qcow2 10G
```

2- Agent node disk
```bash
sudo qemu-img create -f qcow2 -F qcow2 -b noble-base.img k3s-agent.qcow2 10G
```

```bash
sudo virt-install \
  --name k3s-server \
  --memory 2048 --vcpus 2 \
  --disk path=k3s-server.qcow2,format=qcow2 \
  --cloud-init user-data=user-data.yaml,meta-data=meta-data.yaml \
  --os-variant ubuntu24.04 \
  --network network=default \
  --import --graphics none --noautoconsole
```
```
```
```
```
