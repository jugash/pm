# OpenShift / Kubernetes: in-cluster benchmarking

The k8s scenarios answer: *how close does a correctly configured pod get to
bare metal?* For that to be a fair fight, the pod must receive the same
substrate — an SR-IOV VF on the same NIC, exclusive isolated cores,
hugepages, and the Onload userland. This page targets OpenShift Container
Platform (OCP); plain-Kubernetes differences are noted inline.

## Node prerequisites (OCP)

- **PerformanceProfile** (Node Tuning Operator) on the benchmark
  MachineConfigPool — this is the OCP-native way to get everything
  [host-tuning.md](host-tuning.md) requires: `isolated`/`reserved` CPU sets
  (becomes `isolcpus`/`nohz_full`/`rcu_nocbs`), hugepages, static CPU
  manager, `single-numa-node` topology policy, and the low-latency tuned
  profile, all in one CR:

  ```yaml
  apiVersion: performance.openshift.io/v2
  kind: PerformanceProfile
  metadata: { name: trading-latency }
  spec:
    cpu: { isolated: "2-15", reserved: "0-1" }
    hugepages:
      defaultHugepagesSize: 2M
      pages: [{ size: 2M, count: 1024 }]
    numa: { topologyPolicy: single-numa-node }
    realTimeKernel: { enabled: false }
    nodeSelector: { node-role.kubernetes.io/worker-latency: "" }
  ```

  (Plain k8s: set kubelet `--cpu-manager-policy=static
  --topology-manager-policy=single-numa-node` and kernel args by hand.)
- **SR-IOV Network Operator** (supported OCP operator; Multus is built into
  OCP) — the chart's `SriovNetworkNodePolicy` carves VFs from the Solarflare
  PFs with `deviceType: netdevice` (Onload needs kernel netdev VFs).
- **Onload kernel module + sfc driver** on the nodes (KMM / kmods-via-
  containers on OCP), and an Onload userland in the bench image matching the
  module version.

## Install the chart

```bash
helm install perfbench deploy/helm/perfbench -n perfbench --create-namespace \
  --set serviceMonitor.labels.release=monitoring \
  --set sriov.enabled=true \
  --set sriov.nodePolicy.nicSelector.pfNames='{ens1f0,ens2f0}' \
  --set nad.enabled=true --set nad.name=sriov-trading
```

Installs:

- **results exporter** — Deployment + Service + ServiceMonitor + PVC state;
- **SecurityContextConstraints** (`openshift.enabled=true`, default) —
  grants the benchmark pods NET_RAW/NET_ADMIN/IPC_LOCK/SYS_NICE and the
  hostPath mounts for `/dev/onload`/`/dev/sfc_char`. Cluster-admin required;
  on plain k8s set `openshift.enabled=false` (PSS: the namespace needs the
  `privileged` pod-security level instead);
- **RBAC** — a namespace Role letting the runner create/exec/delete pods;
- **SriovNetworkNodePolicy + NetworkAttachmentDefinition** (when enabled);
- **Grafana dashboards** ConfigMap.

All images are UBI 9 based (`deploy/docker/`): `Dockerfile.exporter`,
`Dockerfile.runner` (harness + `oc`/`kubectl`), `Dockerfile.bench` (the
benchmark tools; see in-file notes about entitled vs source builds).

## Running benchmarks in-cluster (runner Job)

Enable the runner and give it scenarios:

```bash
helm upgrade perfbench deploy/helm/perfbench -n perfbench --reuse-values \
  --set runner.enabled=true \
  --set runner.clientNode=worker-a --set runner.serverNode=worker-b \
  --set runner.scenarioIds='{k8s-onload-net}' \
  --set-file runner.scenarios=scenarios/k8s-vs-baremetal.yaml

oc -n perfbench logs -f job/perfbench-runner-2
```

Each upgrade with `runner.enabled=true` creates a fresh revision-suffixed
Job. The Job runs `perfbench k8s-run`, which is fully self-contained:

1. renders Guaranteed-QoS client/server pods (exclusive CPUs sized from the
   scenario's role cores, hugepages, Multus annotation, SR-IOV resource,
   Onload device mounts for bypass scenarios), pinned to
   `runner.clientNode`/`serverNode`;
2. applies them, waits for Ready, resolves the **data-path address** from
   the Multus `network-status` annotation (never the cluster pod IP — that
   would benchmark the CNI overlay);
3. runs preflight + env capture + tools through the same orchestrator as
   bare metal, pushing each rep to the exporter;
4. deletes the pods (cleanup is by-name and runs even after partial
   provisioning; `runner.keepPods=true` to debug).

The same command works from a laptop/bastion with `oc` configured:

```bash
perfbench k8s-run scenarios/ -s k8s-onload-net \
  --namespace perfbench --image ghcr.io/jugash/perfbench-bench:0.1.0 \
  --client-node worker-a --server-node worker-b \
  --network sriov-trading --sriov-resource amd.com/sfc_vf \
  --push http://perfbench-exporter.perfbench:9109
```

## How preflight works inside pods

Preflight runs **inside the benchmark pods** via `oc exec`, which has an
important property: sysfs is the *node's* sysfs, mounted read-only into the
pod. So these checks genuinely verify the node the pod landed on:

| check | what it sees from a pod |
|---|---|
| `cpu_isolation` | node truth (`/sys/devices/system/cpu/isolated`) — verifies the PerformanceProfile actually isolated your scenario cores |
| `cpu_governor`, `smt`, `transparent_hugepages` | node truth (host sysfs) |
| `nic_driver` | the VF inside the pod (`ethtool -i net1`) — verifies you got a real sfc VF, not a veth |
| `onload` | the pod image — verifies the userland is present |
| `irqbalance` | **cannot see host processes** (PID namespace) |

The irqbalance blind spot is handled at two levels:

1. **Honest in-pod check** — the check detects it's inside a container
   (pid 1 isn't systemd/init) and reports a *warning* — "cannot verify from
   container, check the node" — instead of a vacuous pass.
2. **Host-level node checks** — when nodes are pinned
   (`--client-node`/`--server-node`, or `runner.clientNode/serverNode`),
   `k8s-run` first launches a short-lived privileged `hostPID` debug pod on
   each benchmark node (host root read-only at `/host`, like `oc debug
   node/…`) and verifies, on the actual node:
   - `node_irqbalance` — a real pgrep against host PIDs (fatal if running);
   - `nic_irq_affinity` — the scenario NIC's IRQs from `/proc/interrupts`
     must not overlap the benchmark cores (fatal) and should sit on the
     declared `irq_cores` (warning otherwise);
   - `node_tuned` — active tuned/PerformanceProfile (warning if it isn't a
     latency/performance profile).

   Fatal node failures abort the scenario *before* any benchmark pod is
   created; all node-check results are recorded in each run's preflight
   section. The debug pod is allowed by the chart's SCC (privileged is
   required for hostPID); it is deleted immediately after the checks.

Note also: the scenario's `cpu.client_cores` are used for `taskset` inside
the pod and for the isolation check; with the static CPU manager the pod is
*allocated* exclusive cores by kubelet, so set the scenario cores to match
the cpuset the pod actually receives (`oc exec … -- cat
/sys/fs/cgroup/cpuset.cpus.effective`) or run with `require_isolated: false`
and rely on the env snapshot.

## Checklist for a fair bare-metal comparison

- Same physical NIC port (VF) as the bare-metal scenario, same NUMA node.
- Pod cores within the node's isolated set; verified by preflight/env
  snapshot taken inside the pod.
- Same Onload version in-image as the node's module (both appear in the env
  snapshot).
- `--network`/data-path address used, not the cluster IP.
- `perfbench preflight --transport k8s --client-pod …` (or just read the
  preflight section of the run record) before trusting numbers.
