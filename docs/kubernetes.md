# OpenShift / Kubernetes: in-cluster benchmarking

The k8s scenarios answer: *how close does a correctly configured pod get to
bare metal?* For that to be a fair fight, the pod must receive the same
substrate — a dedicated Solarflare data-path interface, exclusive isolated
cores, hugepages, and the Onload userland. The data-path NIC is delivered by
**PF-IOV**: the card is partitioned into several physical functions, and a
Multus `host-device` CNI moves one PF netdev into the pod's network namespace
(an SR-IOV VF model is also supported; see `nad.type`). This page targets
OpenShift Container Platform (OCP); plain-Kubernetes differences are noted
inline.

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
- **PF-IOV partitioning + Multus host-device CNI** (Multus is built into OCP).
  Partition each Solarflare port into multiple physical functions with
  `sfboot` (e.g. `sfboot pf-count=8`) so there is a dedicated PF netdev per
  benchmark pod, then attach one to the pod via a `host-device` NAD
  (`nad.type=host-device`, `nad.device=<pf>`). host-device moves the real PF
  into the pod netns for the pod's lifetime and returns it on delete — the pod
  gets the actual silicon, not a veth, and **no device-plugin resource is
  requested for the NIC**. (Alternative: `nad.type=sriov` + the SR-IOV Network
  Operator, where `SriovNetworkNodePolicy` carves VFs and pods request a VF
  resource via `runner.nicResource`.)
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
  the allocated cores and the Onload devices on one NUMA node; pin the
  benchmark pods to nodes whose data-path PF lives on that same NUMA node.

## Install the chart

```bash
# PF-IOV (default): host-device moves a partitioned Solarflare PF into the pod
helm install perfbench deploy/helm/perfbench -n perfbench --create-namespace \
  --set serviceMonitor.labels.release=monitoring \
  --set nad.enabled=true --set nad.name=sfc-lowlat \
  --set nad.type=host-device --set nad.device=ens2f0
```

(SR-IOV instead: `--set nad.type=sriov --set sriov.enabled=true --set
sriov.nodePolicy.nicSelector.pfNames='{ens1f0,ens2f0}'`, and set
`runner.nicResource=amd.com/sfc_vf`.)

Installs:

- **results exporter** — Deployment + Service + ServiceMonitor + PVC state;
- **SecurityContextConstraints** (`openshift.enabled=true`, default) —
  grants the benchmark pods NET_RAW/NET_ADMIN/IPC_LOCK/SYS_NICE. The Onload
  device nodes now arrive via the onload device plugin, so the benchmark pods
  no longer need hostPath access (only the short-lived node-check debug pods
  do). Cluster-admin required; on plain k8s set `openshift.enabled=false`
  (PSS: the namespace needs the `privileged` pod-security level instead);
- **RBAC** — a namespace Role letting the runner create/exec/delete pods;
- **NetworkAttachmentDefinition** (`nad.enabled`) — host-device or sriov type;
  **SriovNetworkNodePolicy** only for the SR-IOV path (`sriov.enabled`);
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
   plus hugepages and the Multus annotation that attaches the data-path PF,
   pinned to `runner.clientNode`/`serverNode`. The pods read `ISOLATED_CPUS`
   (for `taskset`) and `ONLOAD_LIB` (`LD_PRELOAD`) at runtime, so no core ids
   or `/dev/onload` hostPaths are baked in;
2. applies them, waits for Ready, resolves the **data-path address** from
   the Multus `network-status` annotation (never the cluster pod IP — that
   would benchmark the CNI overlay). The socket benchmarks also **bind their
   local endpoint to the Multus secondary interface** (the relocated PF, e.g.
   `net1`): iperf3 (`-B`), netperf (`-L`) and sockperf (server `-i`, and
   multicast `--mc-rx-if`/`--mc-tx-if`) take that interface's address, read
   in-pod at runtime, so traffic traverses the low-latency NIC rather than
   eth0. (sfnt and eflatency already target it — via the data-path address and
   the resolved interface name respectively.);

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
  --network sfc-lowlat \
  --client-ip 192.168.100.1/24 --server-ip 192.168.100.2/24 \
  --push http://perfbench-exporter.perfbench:9109
```

(Drop `--client-ip/--server-ip` when the NAD does dynamic IPAM. PF-IOV needs
no `--nic-resource`; add it only for the SR-IOV model.)

## Running over a VLAN

To benchmark a tagged path, layer a VLAN on the data-path NIC. The VLAN is
created **inside the pod**, on top of the secondary interface Multus moved in
— so the tag rides the very same interface, and the pod ends up with **both**
the base port (e.g. `net1`) and the VLAN (`vlan0`):

```bash
helm upgrade perfbench deploy/helm/perfbench -n perfbench --reuse-values \
  --set runner.vlanId=100 \
  --set runner.baseInterface=net1 --set runner.vlanInterface=vlan0 \
  --set runner.clientVlanIp=192.168.110.1/24 \
  --set runner.serverVlanIp=192.168.110.2/24
```

(CLI equivalent: `--vlan-id 100 --client-vlan-ip … --server-vlan-ip …`,
optionally `--base-interface net1 --vlan-interface vlan0`.) What happens:

- the base secondary interface name is pinned in the Multus annotation so it
  is deterministic (`net1`);
- after the pods are Ready, the harness execs into each one and creates the
  VLAN on top of it — `ip link add link net1 name vlan0 type vlan id 100`,
  then assigns the role's static VLAN IP and brings it up (the pods carry
  `NET_ADMIN`, so no extra privilege is needed). No `vlan` NAD or host master
  is involved;
- the server's VLAN IP becomes the **data-path address** the client targets;
- the socket benchmarks bind their local endpoint to `vlan0` (iperf3 `-B`,
  netperf `-L`, sockperf `-i` / `--mc-rx-if` / `--mc-tx-if`), so traffic is
  tagged. The base port stays present (untagged) but unused by the tools.

`eflatency` is excluded — it measures the raw ef_vi layer, which sits below
VLAN tagging, so it always uses the physical port.

## Using pre-existing NADs

The chart does not have to own the networks. If your NADs are managed
elsewhere (a network team, a GitOps repo, a cluster operator), leave
`nad.enabled=false` / `vlanNad.enabled=false` and just reference them by name:

```bash
helm upgrade perfbench deploy/helm/perfbench -n perfbench --reuse-values \
  --set runner.enabled=true \
  --set 'runner.networks={trading-data}' \
  --set runner.vlanNetwork=netops/trading-vlan      # NAD in the netops namespace
```

- Names may be bare (`trading-data`, resolved in the pod's namespace) or
  namespaced (`netops/trading-vlan`) for a NAD that lives elsewhere.
- **IPAM is the NAD's choice.** Provide `runner.clientIp/serverIp` (and
  `clientVlanIp/serverVlanIp`) only when the NAD uses static IPAM. When it runs
  its own IPAM (whereabouts/host-local), omit them and the runner reads the
  assigned data-path / VLAN address from the pod's `network-status` annotation.
- For the VLAN, the runner still pins the in-pod interface name
  (`--vlan-interface`, default `vlan0`) so binding stays deterministic
  regardless of who created the NAD.

## How preflight works inside pods

Preflight runs **inside the benchmark pods** via `oc exec`, which has an
important property: sysfs is the *node's* sysfs, mounted read-only into the
pod. So these checks genuinely verify the node the pod landed on:

| check | what it sees from a pod |
|---|---|
| `cpu_isolation` | reads the pod's own `$ISOLATED_CPUS` (what the device plugin allocated) and checks every one is in the node's `/sys/devices/system/cpu/isolated` set — fatal if the var is empty (plugin didn't allocate) |
| `cpu_governor`, `smt`, `transparent_hugepages` | node truth (host sysfs) |
| `nic_driver` | the data-path interface inside the pod (`ethtool -i net1`) — verifies you got the real relocated `sfc` PF, not a veth |
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

- Same physical Solarflare port (a PF off the same card) as the bare-metal
  scenario, same NUMA node.
- Pod cores within the node's isolated set; verified by preflight/env
  snapshot taken inside the pod.
- Same Onload version in-image as the node's module (both appear in the env
  snapshot).
- `--network`/data-path address used, not the cluster IP.
- `perfbench preflight --transport k8s --client-pod …` (or just read the
  preflight section of the run record) before trusting numbers.
