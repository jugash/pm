# Kubernetes: low-latency benchmark pods

The k8s scenarios answer: *how close does a correctly configured pod get to
bare metal?* For that to be a fair fight, the pod must receive the same
substrate — a VF on the same NIC, exclusive isolated cores, hugepages, and
the Onload userland.

## Prerequisites

- Multus CNI installed (secondary networks).
- sriov-network-operator (or sriov-cni + sriov-network-device-plugin
  configured by hand).
- kubelet on the benchmark nodes:
  `--cpu-manager-policy=static --reserved-cpus=0-1`
  (+ `--topology-manager-policy=single-numa-node` strongly recommended), and
  kernel-level isolation per [host-tuning](host-tuning.md) — kubelet static
  pinning assigns exclusive cores, but only kernel `isolcpus`/`nohz_full`
  keeps the rest of the OS off them.
- Onload kernel module + sfc driver installed on the node; the pod needs
  `/dev/onload` and `/dev/sfc_char` (the harness's pod renderer mounts them
  for bypass scenarios) and an Onload userland matching the module version
  (bake into the bench image or mount from the host).

## Deploy with the Helm chart

```bash
helm install perfbench deploy/helm/perfbench -n perfbench --create-namespace \
  --set serviceMonitor.labels.release=monitoring \
  --set sriov.enabled=true \
  --set sriov.nodePolicy.nicSelector.pfNames='{ens1f0,ens2f0}' \
  --set nad.enabled=true --set nad.name=sriov-trading
```

This installs:

- the **results exporter** (Deployment + Service + ServiceMonitor + optional
  PVC-backed state file) — Alloy discovers it via the ServiceMonitor labels;
- a **SriovNetworkNodePolicy** carving VFs from the Solarflare PFs
  (`deviceType: netdevice` — Onload needs kernel netdev VFs);
- a **NetworkAttachmentDefinition** (`sriov-trading`) tied to the SR-IOV
  resource, with whereabouts IPAM on the benchmark subnet;
- the **Grafana dashboards** ConfigMap (sidecar label `grafana_dashboard: "1"`).

## Benchmark pods

The harness renders pods itself (`perfbench.runner.k8s.render_benchmark_pod`):
Guaranteed QoS (requests == limits, integral CPUs sized from the scenario's
role cores → exclusive cores from the static CPU manager), hugepages,
`k8s.v1.cni.cncf.io/networks: sriov-trading` annotation, NET_RAW/NET_ADMIN/
IPC_LOCK/SYS_NICE capabilities, Onload device mounts for bypass scenarios,
and `nodeName` pinning so client and server land on the intended hosts.

Typical flow:

```bash
# 1. create the pods (long-lived `sleep infinity` shells)
python3 - <<'EOF'
from perfbench.config.loader import load_scenarios
from perfbench.runner.k8s import Kubectl, render_benchmark_pod
sc = [s for s in load_scenarios("scenarios/") if s.id == "k8s-onload-net"][0]
k = Kubectl(namespace="perfbench")
for role, node in (("client", "worker-a"), ("server", "worker-b")):
    k.apply(render_benchmark_pod(
        name=f"bench-{role}", scenario=sc, role=role,
        image="ghcr.io/jugash/perfbench-bench:0.1.0",
        node=node, networks=["sriov-trading"],
        sriov_resource="amd.com/sfc_vf"))
    k.wait_ready(f"bench-{role}")
print("server data-path IP:", k.pod_network_ip("bench-server", "sriov-trading"))
EOF

# 2. run the scenario against the pods (one shared measurement pipeline)
perfbench run scenarios/ -s k8s-onload-net \
    --transport k8s --namespace perfbench \
    --client-pod bench-client --server-pod bench-server \
    --server-address <data-path IP from step 1> \
    --push http://perfbench-exporter.perfbench:9109
```

Use the **secondary-network IP** as `--server-address`, never the cluster
pod IP — otherwise you benchmark the CNI overlay instead of the VF.

## Checklist for a fair comparison

- Same physical NIC port (or its VF) as the bare-metal scenario.
- Pod cores ⊂ node isolated cores, same NUMA node as the NIC; verify in the
  run's env snapshot (it is taken *inside* the pod, so `isolated_cpus`,
  governor etc. reflect what the workload actually sees).
- Same Onload version in-image as the node's kernel module (env snapshot
  shows both sides).
- irqbalance disabled on the node; preflight runs inside the pod and will
  catch most of this.
- `perfbench preflight --transport k8s --client-pod bench-client …` first.
