# Benchmarking over an external NAD + in-pod VLAN

This shows how to run the bench pods against a **pre-existing** (externally
managed) secondary network and layer a **VLAN on top of it inside the pod**,
so the pod sees **both** interfaces:

- the *secondary network* delivers the low-latency Solarflare interface into
  the pod as `net1` (PF-IOV: a partitioned PF moved in by `host-device`), and
- a *VLAN* (`vlan0`) is created at run time on top of `net1`, with a static IP.

Because the VLAN is created in the pod, the tag rides the exact interface
Multus moved in — no `vlan` CNI / host master is involved. PerfBench does not
create the NAD; leave `nad.enabled=false` and reference the existing one.

## 1. The external secondary NAD (managed outside PerfBench)

Created by your network team / GitOps, in any namespace. Reference it later as
`namespace/name`. The `"capabilities": { "deviceID": true }` flag lets the PF
be selected at run time (Multus / the device plugin injects its PCI), so the
NAD does not hardcode an interface name:

```yaml
apiVersion: k8s.cni.cncf.io/v1
kind: NetworkAttachmentDefinition
metadata:
  name: sfc-lowlat
  namespace: netops
spec:
  config: |-
    {
      "cniVersion": "0.3.1",
      "type": "host-device",
      "name": "sfc-lowlat",
      "capabilities": { "deviceID": true }
    }
```

(To target a fixed interface instead, drop `capabilities` and set
`"device": "ens2f0"`.) No VLAN NAD is needed — the VLAN is made in the pod.

## 2. What the pod gets

PerfBench renders the Multus annotation referencing the external NAD and
**pins the base interface name** so the VLAN can be created on it
deterministically:

```yaml
metadata:
  annotations:
    k8s.v1.cni.cncf.io/networks: |-
      [ { "name": "sfc-lowlat", "namespace": "netops", "interface": "net1" } ]
```

Once the pod is Ready, the harness execs into it and builds the VLAN (the pod
carries `NET_ADMIN`, so no extra privilege is required):

```bash
ip link set net1 up
ip link add link net1 name vlan0 type vlan id 100
ip addr add 192.168.110.2/24 dev vlan0      # the role's static VLAN IP
ip link set vlan0 up
```

Inside the pod:

```text
$ ip -br addr
lo               UNKNOWN  127.0.0.1/8
eth0             UP       10.244.1.7/24          # cluster network (Pod IP)
net1             UP                              # base secondary PF (host-device)
vlan0@net1       UP       192.168.110.2/24       # VLAN created in-pod, static IP
```

## 3. Run it

Point PerfBench at the external secondary NAD by name, give the VLAN id and
each role's static VLAN IP. The server's VLAN IP becomes the data-path address
the client targets, and the socket tools bind to `vlan0`.

CLI:

```bash
perfbench k8s-run scenarios/ -s k8s-onload-net \
  --namespace perfbench --image ghcr.io/jugash/perfbench-bench:0.1.0 \
  --client-node worker-a --server-node worker-b \
  --network netops/sfc-lowlat \
  --vlan-id 100 --base-interface net1 --vlan-interface vlan0 \
  --client-vlan-ip 192.168.110.1/24 \
  --server-vlan-ip 192.168.110.2/24 \
  --push http://perfbench-exporter.perfbench:9109
```

Helm runner (NAD not created by the chart):

```bash
helm upgrade perfbench deploy/helm/perfbench -n perfbench --reuse-values \
  --set runner.enabled=true \
  --set runner.clientNode=worker-a --set runner.serverNode=worker-b \
  --set 'runner.networks={netops/sfc-lowlat}' \
  --set runner.vlanId=100 \
  --set runner.baseInterface=net1 --set runner.vlanInterface=vlan0 \
  --set runner.clientVlanIp=192.168.110.1/24 \
  --set runner.serverVlanIp=192.168.110.2/24 \
  --set-file runner.scenarios=scenarios/k8s-vs-baremetal.yaml
```

Notes:

- Use `namespace/name` when the NAD lives in another namespace (above:
  `netops/sfc-lowlat`); a bare name resolves in the pod's own namespace.
- `--base-interface` must match the in-pod name of the secondary interface
  (pinned in the annotation; `net1` by default). The VLAN is built on it.
- Static VLAN IPs are required: they are assigned in-pod with `ip addr`, so
  there is no IPAM to fall back on.
