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
- **Device plugins** that hand the benchmark pods their isolated CPUs and the
  Onload userland, so scenarios never name cores or mount device nodes by
  hand:
  - an **isolated-cpus plugin** advertising a resource (default
    `perfbench.io/isolated-cpus`) that allocates cores from the node's
    `isolated` set and injects their ids as `ISOLATED_CPUS`;
  - an **onload plugin** advertising a resource (default
    `perfbench.io/onload`) that mounts `/dev/onload` + `/dev/onload_epoll`,
    injects the matching `libonload.so` and sets `ONLOAD_LIB`.

  Both resources are configurable (`runner.isolatedCpusResource` /
  `runner.onloadResource`, or `--isolated-cpus-resource` /
  `--onload-resource`). The `single-numa-node` Topology Manager policy aligns
  the allocated cores, the SR-IOV VF and the Onload devices on one NUMA node.

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
  grants the benchmark pods NET_RAW/NET_ADMIN/IPC_LOCK/SYS_NICE. The Onload
  device nodes now arrive via the onload device plugin, so the benchmark pods
  no longer need hostPath access (only the short-lived node-check debug pods
  do). Cluster-admin required; on plain k8s set `openshift.enabled=false`
  (PSS: the namespace needs the `privileged` pod-security level instead);
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

1. renders client/server pods that request their substrate from device
   plugins — `<isolatedCpusResource>: N` (N sized from the scenario's role
   cores or `cpu.count`) and, for bypass scenarios, `<onloadResource>: 1` —
   plus hugepages, the Multus annotation and the SR-IOV resource, pinned to
   `runner.clientNode`/`serverNode`. The pods read `ISOLATED_CPUS` (for
   `taskset`) and `ONLOAD_LIB` (`LD_PRELOAD`) at runtime, so no core ids or
   `/dev/onload` hostPaths are baked in;
2. applies them, waits for Ready, resolves the **data-path address** from
   the Multus `network-status` annotation (never the cluster pod IP — that
   would benchmark the CNI overlay);

   The NAD must have IPAM so the pods get an L3 address on the data path.
   Two options: dynamic (`nad.ipam` whereabouts/host-local, the chart's
   default) — the harness reads the assigned IP from `network-status`; or
   **static per-pod IPs** — set `runner.clientIp`/`serverIp` (CLI:
   `--client-ip/--server-ip`, CIDR form like `192.168.100.2/24`) and the
   harness requests them through the Multus annotation's `ips` field. The
   static path needs the NAD's IPAM to be `{"type": "static"}` and gives
   deterministic addresses run after run — useful when switch ACLs or
   multicast IGMP snooping config reference fixed source addresses;
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
  --client-ip 192.168.100.1/24 --server-ip 192.168.100.2/24 \
  --push http://perfbench-exporter.perfbench:9109
```

(Drop `--client-ip/--server-ip` when the NAD does dynamic IPAM.)

## How preflight works inside pods

Preflight runs **inside the benchmark pods** via `oc exec`, which has an
important property: sysfs is the *node's* sysfs, mounted read-only into the
pod. So these checks genuinely verify the node the pod landed on:

| check | what it sees from a pod |
|---|---|
| `cpu_isolation` | reads the pod's own `$ISOLATED_CPUS` (what the device plugin allocated) and checks every one is in the node's `/sys/devices/system/cpu/isolated` set — fatal if the var is empty (plugin didn't allocate) |
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

Note on CPU pinning: k8s scenarios no longer name their cores. Set
`cpu.count` (how many isolated CPUs the role needs) and leave
`client_cores`/`server_cores` out — the isolated-cpus plugin allocates that
many and injects the ids as `ISOLATED_CPUS`, which the harness uses verbatim
for `taskset` and the isolation check. (Explicit core lists are still required
for `platform: baremetal`, where the harness pins to the host's isolated
cores directly.) For example:

```yaml
cpu:
  count: 2          # request 2 isolated CPUs from the device plugin
  numa_node: 0
  require_isolated: true
```

## Checklist for a fair bare-metal comparison

- Same physical NIC port (VF) as the bare-metal scenario, same NUMA node.
- Pod cores within the node's isolated set; verified by preflight/env
  snapshot taken inside the pod.
- Same Onload version in-image as the node's module (both appear in the env
  snapshot).
- `--network`/data-path address used, not the cluster IP.
- `perfbench preflight --transport k8s --client-pod …` (or just read the
  preflight section of the run record) before trusting numbers.
