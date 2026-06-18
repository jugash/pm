"""Kubernetes execution support.

Built on ``kubectl`` driven through an injected executor (local by default),
so it works anywhere kubectl is configured and is fully unit-testable.

Two pieces:

* :class:`Kubectl` — manifest apply/delete, readiness waits, pod IP lookup.
* :class:`PodExecutor` — an :class:`Executor` that runs commands inside a
  running pod via ``kubectl exec``, letting the orchestrator treat pods
  exactly like SSH hosts.

``render_benchmark_pod`` produces a benchmark pod manifest with the Multus
network annotation and — instead of statically isolated cores and hostPath
Onload device mounts — *device-plugin* resource requests: an isolated-cpus
resource (the plugin injects ``ISOLATED_CPUS``) and an onload resource (the
plugin mounts ``/dev/onload``/``/dev/onload_epoll``, injects the library and
sets ``ONLOAD_LIB``). The single-numa-node Topology Manager policy keeps the
cores and the devices on one NUMA node.

Data-path NIC: the low-latency interface is delivered by Multus. The default
model is **PF-IOV** — a Solarflare card partitioned into several physical
functions, one of which a ``host-device``-style CNI relocates into the pod's
network namespace; that path needs no pod resource request (the NAD does the
move). An optional ``nic_resource`` covers the alternative SR-IOV-device-plugin
model, where the pod must also request a VF resource.
"""

from __future__ import annotations

import json
import shlex
import uuid
from dataclasses import dataclass
from typing import Mapping, Optional, Sequence

import yaml

from perfbench.config.schema import NetworkPath, Scenario
from perfbench.errors import ExecutionError
from perfbench.runner.base import BackgroundProcess, ExecResult, Executor
from perfbench.runner.local import LocalExecutor

# Default device-plugin resource names. Both are configurable (CLI flags /
# Helm values) so they can match whatever the cluster's plugins advertise.
DEFAULT_ISOLATED_CPUS_RESOURCE = "perfbench.io/isolated-cpus"
DEFAULT_ONLOAD_RESOURCE = "perfbench.io/onload"


def _networks_annotation(
    networks: Sequence[str],
    static_ip: Optional[str],
    data_path_interface: Optional[str] = None,
) -> str:
    """Render the Multus ``networks`` annotation.

    Plain comma-joined names normally; the JSON form when a static IP is
    requested for the first (data-path) network — per-pod ``ips`` requires
    the NAD's IPAM to be ``{"type": "static"}``.

    ``data_path_interface`` pins the in-pod name of the first (data-path)
    network. This makes the base secondary interface deterministic so a VLAN
    can be created on top of it inside the pod (``ip link … type vlan``).
    """
    if not static_ip and not data_path_interface:
        return ", ".join(networks)
    entries = []
    for i, network in enumerate(networks):
        ns, _, name = network.rpartition("/")
        entry: dict = {"name": name}
        if ns:
            entry["namespace"] = ns
        if i == 0:
            if data_path_interface:
                entry["interface"] = data_path_interface
            if static_ip:
                entry["ips"] = [static_ip]
        entries.append(entry)
    return json.dumps(entries)


def _network_name_matches(requested: str, status_name: str) -> bool:
    """Match a configured network against a Multus network-status name.

    Status names are usually namespaced (``perfbench/sriov-trading``) while
    configs typically carry the bare NAD name (``sriov-trading``) — accept
    either form on either side. Namespaces are only compared when both
    sides carry one.
    """
    if not status_name:
        return False
    req_ns, _, req_name = requested.rpartition("/")
    st_ns, _, st_name = status_name.rpartition("/")
    if req_name != st_name:
        return False
    return not (req_ns and st_ns) or req_ns == st_ns


class Kubectl:
    def __init__(
        self,
        namespace: str = "perfbench",
        context: Optional[str] = None,
        kubeconfig: Optional[str] = None,
        executor: Optional[Executor] = None,
    ):
        self.namespace = namespace
        self.context = context
        self.kubeconfig = kubeconfig
        self.executor = executor or LocalExecutor()

    def base_command(self) -> str:
        parts = ["kubectl", "-n", self.namespace]
        if self.context:
            parts += ["--context", self.context]
        if self.kubeconfig:
            parts += ["--kubeconfig", self.kubeconfig]
        return " ".join(parts)

    def _run(self, args: str, input_data: Optional[str] = None, timeout: float = 60) -> ExecResult:
        result = self.executor.run(
            f"{self.base_command()} {args}", timeout=timeout, input_data=input_data
        )
        if not result.ok:
            raise ExecutionError(
                f"kubectl {args} failed (rc={result.exit_code}): {result.stderr.strip()}"
            )
        return result

    def apply(self, manifest_yaml: str) -> ExecResult:
        return self._run("apply -f -", input_data=manifest_yaml)

    def delete_pod(self, name: str) -> ExecResult:
        return self._run(f"delete pod {name} --ignore-not-found --wait=true", timeout=120)

    def wait_ready(self, pod: str, timeout_s: int = 180) -> ExecResult:
        return self._run(
            f"wait --for=condition=Ready pod/{pod} --timeout={timeout_s}s",
            timeout=timeout_s + 30,
        )

    def pod_ip(self, pod: str) -> str:
        result = self._run(f"get pod {pod} -o jsonpath={{.status.podIP}}")
        ip = result.stdout.strip()
        if not ip:
            raise ExecutionError(f"pod {pod} has no IP yet")
        return ip

    def pod_annotations(self, pod: str) -> dict:
        """All annotations of a pod.

        Fetched as ``-o json`` and parsed here rather than via jsonpath:
        annotation keys contain dots that need ``\\.`` escaping in jsonpath,
        and those backslashes don't survive the ``/bin/sh -c`` (or ssh)
        layers the executors run commands through.
        """
        result = self._run(f"get pod {pod} --output json")
        try:
            doc = json.loads(result.stdout)
        except json.JSONDecodeError as exc:
            raise ExecutionError(
                f"cannot parse `kubectl get pod {pod} --output json` output"
            ) from exc
        return doc.get("metadata", {}).get("annotations", {}) or {}

    def pod_network_ip(self, pod: str, network: str) -> str:
        """IP on a secondary (Multus) network, from the network-status
        annotation (``networks-status`` fallback for older Multus)."""
        annotations = self.pod_annotations(pod)
        raw = (
            annotations.get("k8s.v1.cni.cncf.io/network-status")
            or annotations.get("k8s.v1.cni.cncf.io/networks-status")
            or ""
        ).strip()
        if not raw:
            requested = annotations.get("k8s.v1.cni.cncf.io/networks", "")
            raise ExecutionError(
                f"pod {pod} has no Multus network-status annotation "
                f"(networks requested: {requested or 'NONE'}). Multus did not "
                f"attach the secondary network — check that the "
                f"NetworkAttachmentDefinition {network!r} exists in namespace "
                f"{self.namespace!r} (kubectl get net-attach-def -n "
                f"{self.namespace}) and that Multus (and the host-device/SR-IOV "
                f"CNI that attaches the NIC) is running."
            )
        try:
            statuses = json.loads(raw)
        except json.JSONDecodeError as exc:
            raise ExecutionError(
                f"cannot parse network-status for pod {pod}: {raw[:200]!r}"
            ) from exc
        matched = [
            s for s in statuses if _network_name_matches(network, s.get("name", ""))
        ]
        for status in matched:
            if status.get("ips"):
                return status["ips"][0]
        if matched:
            raise ExecutionError(
                f"pod {pod}: network {matched[0].get('name')!r} is attached "
                f"but reports no IPs — does the NetworkAttachmentDefinition "
                f"have IPAM configured (e.g. whereabouts/host-local)? The "
                f"harness needs an L3 address on the data-path network."
            )
        attached = [s.get("name", "?") for s in statuses]
        raise ExecutionError(
            f"pod {pod} has no IP on network {network!r}; attached networks: "
            f"{attached}"
        )

    def logs(self, pod: str) -> str:
        return self._run(f"logs {pod}").stdout

    def describe_events(self, pod: str) -> str:
        """Recent events for a pod (best-effort). The place CNI / device-plugin
        failures show up when a pod is stuck (e.g. a NAD that can't get its
        deviceID, so the sandbox network is never created)."""
        result = self.executor.run(
            f"{self.base_command()} get events --field-selector "
            f"involvedObject.name={pod} "
            "--sort-by=.lastTimestamp -o custom-columns=TYPE:.type,"
            "REASON:.reason,MESSAGE:.message --no-headers",
            timeout=30,
        )
        return result.stdout.strip()


class PodExecutor(Executor):
    """Runs commands inside a pod via ``kubectl exec``."""

    def __init__(self, kubectl: Kubectl, pod: str, container: Optional[str] = None):
        self.kubectl = kubectl
        self.pod = pod
        self.container = container

    def describe(self) -> str:
        return f"k8s:{self.kubectl.namespace}/{self.pod}"

    def _inpod(self, command: str, env: Optional[Mapping[str, str]]) -> str:
        if env:
            assigns = " ".join(f"{k}={shlex.quote(str(v))}" for k, v in sorted(env.items()))
            command = f"env {assigns} {command}"
        return command

    def _kubectl_exec(self, inpod_command: str) -> str:
        container = f" -c {self.container}" if self.container else ""
        return (
            f"{self.kubectl.base_command()} exec {self.pod}{container} -- "
            f"/bin/sh -c {shlex.quote(inpod_command)}"
        )

    def _exec_command(self, command: str, env: Optional[Mapping[str, str]]) -> str:
        return self._kubectl_exec(self._inpod(command, env))

    def run(self, command, timeout=None, env=None, input_data=None) -> ExecResult:
        if input_data is not None:
            raise ExecutionError("PodExecutor does not support stdin input")
        return self.kubectl.executor.run(self._exec_command(command, env), timeout=timeout)

    def start(self, command, env=None) -> BackgroundProcess:
        # `kubectl exec` does NOT forward signals into the container, so killing
        # the local client (the only thing the local BackgroundProcess controls)
        # would orphan the in-pod server — it keeps running and holds its port,
        # breaking the next repetition with EADDRINUSE. Run the server in its
        # own session (setsid) so the whole tree has a known process-group id,
        # record it, and `wait` so this exec stays attached for the server's
        # lifetime (running() keeps reflecting the server, not a detached shell).
        pidfile = f"/tmp/perfbench-srv-{uuid.uuid4().hex}.pid"
        inner = shlex.quote(self._inpod(command, env))
        wrapped = f"setsid /bin/sh -c {inner} & echo $! > {pidfile}; wait"
        background = self.kubectl.executor.start(self._kubectl_exec(wrapped))
        return PodBackground(background, self, pidfile)

    def _kill_remote(self, pidfile: str) -> None:
        """Best-effort kill of the in-pod server's whole process group, then
        remove the pidfile. The negative pid targets the setsid process group,
        so taskset/onload wrappers and the server all die together."""
        script = (
            f"pgid=$(cat {pidfile} 2>/dev/null); "
            f'[ -n "$pgid" ] && kill -TERM -"$pgid" 2>/dev/null; '
            f"sleep 1; "
            f'[ -n "$pgid" ] && kill -KILL -"$pgid" 2>/dev/null; '
            f"rm -f {pidfile}"
        )
        try:
            self.kubectl.executor.run(self._kubectl_exec(script), timeout=20)
        except ExecutionError:
            pass  # cleanup is best-effort; pod teardown is the backstop


class PodBackground(BackgroundProcess):
    """A pod server whose in-pod process tree is reliably terminated on stop.

    Wraps the local ``kubectl exec`` background process: ``stop`` first tears
    down the local client, then signals the server's process group inside the
    pod so the listening socket is released between repetitions."""

    def __init__(self, inner: BackgroundProcess, executor: "PodExecutor", pidfile: str):
        self._inner = inner
        self._executor = executor
        self._pidfile = pidfile

    def running(self) -> bool:
        return self._inner.running()

    def stop(self) -> ExecResult:
        result = self._inner.stop()
        self._executor._kill_remote(self._pidfile)
        return result


def render_node_check_pod(
    name: str,
    node: str,
    image: str,
    namespace: str = "perfbench",
) -> str:
    """Privileged hostPID debug pod for host-level preflight.

    hostPID exposes the node's processes (real irqbalance check); the host
    root is mounted read-only at /host for chroot-based checks (tuned).
    Equivalent in spirit to ``oc debug node/...`` but pinned, labelled and
    machine-managed.
    """
    manifest = {
        "apiVersion": "v1",
        "kind": "Pod",
        "metadata": {
            "name": name,
            "namespace": namespace,
            "labels": {"app": "perfbench", "perfbench/role": "node-check"},
        },
        "spec": {
            "restartPolicy": "Never",
            "terminationGracePeriodSeconds": 1,
            "nodeName": node,
            "hostPID": True,
            "containers": [
                {
                    "name": "check",
                    "image": image,
                    "command": ["sleep", "infinity"],
                    "securityContext": {"privileged": True},
                    "resources": {
                        "requests": {"cpu": "50m", "memory": "64Mi"},
                        "limits": {"cpu": "500m", "memory": "128Mi"},
                    },
                    "volumeMounts": [
                        {"name": "host", "mountPath": "/host", "readOnly": True}
                    ],
                }
            ],
            "volumes": [{"name": "host", "hostPath": {"path": "/"}}],
        },
    }
    return yaml.safe_dump(manifest, sort_keys=False)


@dataclass
class BenchDeployment:
    """A provisioned client/server pod pair for one scenario."""

    client_pod: str
    server_pod: str
    server_address: str


class K8sBenchSession:
    """Provisions benchmark pods for a scenario and exposes executors.

    This is what makes ``perfbench k8s-run`` (and the Helm runner Job)
    self-contained: render pods -> apply -> wait Ready -> resolve the
    data-path address (secondary Multus network if configured, else pod IP)
    -> run -> tear down.

    Networks may be chart-created or **pre-existing** NADs referenced by
    (optionally namespaced) name. Addresses are taken from the static ``ips``
    request when given, otherwise read from the pod's Multus ``network-status``
    — so NADs that run their own IPAM (whereabouts/host-local) work too, for
    both the base network and the VLAN.
    """

    def __init__(
        self,
        kubectl: Kubectl,
        image: str,
        networks: Sequence[str] = (),
        nic_resource: Optional[str] = None,
        client_node: Optional[str] = None,
        server_node: Optional[str] = None,
        hugepages_2mi: str = "1Gi",
        ready_timeout_s: int = 180,
        client_ip: Optional[str] = None,
        server_ip: Optional[str] = None,
        isolated_cpus_resource: str = DEFAULT_ISOLATED_CPUS_RESOURCE,
        onload_resource: str = DEFAULT_ONLOAD_RESOURCE,
        vlan_id: Optional[int] = None,
        vlan_interface: str = "vlan0",
        base_interface: str = "net1",
        client_vlan_ip: Optional[str] = None,
        server_vlan_ip: Optional[str] = None,
    ):
        self.kubectl = kubectl
        self.image = image
        self.networks = tuple(networks)
        self.nic_resource = nic_resource
        self.nodes = {"client": client_node, "server": server_node}
        self.hugepages_2mi = hugepages_2mi
        self.ready_timeout_s = ready_timeout_s
        self.isolated_cpus_resource = isolated_cpus_resource
        self.onload_resource = onload_resource
        # static IPs (CIDR form, e.g. 192.168.100.2/24) requested via the
        # Multus annotation on the first network; needs static-IPAM NAD
        self.static_ips = {"client": client_ip, "server": server_ip}
        # optional VLAN layered on the NIC *inside the pod*: after the pods are
        # Ready the harness creates ``vlan_interface`` on top of the base
        # secondary interface (``base_interface``) with the static role IP, so
        # the pod has both the base port and the VLAN, and benchmarks run over
        # the VLAN.
        self.vlan_id = vlan_id
        self.vlan_interface = vlan_interface
        self.base_interface = base_interface
        self.vlan_ips = {"client": client_vlan_ip, "server": server_vlan_ip}

    def pod_name(self, scenario: Scenario, role: str) -> str:
        # RFC 1123 label, 63 chars max. Truncate the scenario id, never the
        # role suffix — otherwise long ids would collapse client and server
        # to the same pod name.
        base = scenario.id.replace("_", "-").replace(".", "-")[:52].rstrip("-")
        return f"pb-{base}-{role}"

    def provision(self, scenario: Scenario) -> BenchDeployment:
        for role in ("server", "client"):
            manifest = render_benchmark_pod(
                name=self.pod_name(scenario, role),
                scenario=scenario,
                role=role,
                image=self.image,
                node=self.nodes[role],
                networks=self.networks,
                nic_resource=self.nic_resource,
                hugepages_2mi=self.hugepages_2mi,
                namespace=self.kubectl.namespace,
                static_ip=self.static_ips[role],
                isolated_cpus_resource=self.isolated_cpus_resource,
                onload_resource=self.onload_resource,
                # pin the base secondary iface so the in-pod VLAN can ride it
                data_path_interface=self.base_interface if self.vlan_id else None,
            )
            self.kubectl.apply(manifest)
        for role in ("server", "client"):
            self.kubectl.wait_ready(self.pod_name(scenario, role), self.ready_timeout_s)

        if self.vlan_id is not None:
            for role in ("server", "client"):
                self._setup_vlan(self.pod_name(scenario, role), role)

        server_pod = self.pod_name(scenario, "server")
        if self.vlan_id is not None:
            # benchmarks run over the VLAN: its static IP is the data-path addr
            address = self.vlan_ips["server"].split("/")[0]
        elif self.static_ips["server"]:
            # statically assigned via the networks annotation: known up front
            address = self.static_ips["server"].split("/")[0]
        elif self.networks:
            address = self.kubectl.pod_network_ip(server_pod, self.networks[0])
        else:
            address = self.kubectl.pod_ip(server_pod)
        return BenchDeployment(
            client_pod=self.pod_name(scenario, "client"),
            server_pod=server_pod,
            server_address=address,
        )

    def _setup_vlan(self, pod: str, role: str) -> None:
        """Create the VLAN interface on top of the base port, inside the pod.

        Runs ``ip link … type vlan`` in the pod (needs NET_ADMIN, which the
        bench pods carry) so the tag rides the very interface Multus moved in,
        and assigns the role's static VLAN IP. The base port stays present
        (untagged); benchmarks run over the VLAN."""
        ip = self.vlan_ips[role]
        if not ip:
            raise ExecutionError(
                f"vlan_id set but no {role} VLAN IP; pass --{role}-vlan-ip "
                "(CIDR) so the in-pod VLAN interface can be addressed"
            )
        executor = PodExecutor(self.kubectl, pod)
        command = (
            f"ip link set {self.base_interface} up && "
            f"ip link add link {self.base_interface} name {self.vlan_interface} "
            f"type vlan id {self.vlan_id} && "
            f"ip addr add {ip} dev {self.vlan_interface} && "
            f"ip link set {self.vlan_interface} up"
        )
        result = executor.run(command, timeout=30)
        if not result.ok:
            raise ExecutionError(
                f"failed to create VLAN {self.vlan_interface} (id {self.vlan_id}) "
                f"on {self.base_interface} in pod {pod}: {result.stderr.strip()}"
            )

    def executors(self, deployment: BenchDeployment) -> tuple[Executor, Executor]:
        """(client, server) executors for the orchestrator."""
        return (
            PodExecutor(self.kubectl, deployment.client_pod),
            PodExecutor(self.kubectl, deployment.server_pod),
        )

    def teardown(self, deployment: BenchDeployment) -> None:
        self.kubectl.delete_pod(deployment.client_pod)
        self.kubectl.delete_pod(deployment.server_pod)

    def node_preflight(self, scenario: Scenario) -> list[dict]:
        """Host-level checks on each pinned benchmark node via debug pods.

        Returns check dicts tagged with the node; nodes that aren't pinned
        are reported as skipped (the scheduler could place the pod anywhere,
        so there is no node to check ahead of time).
        """
        from perfbench.capture.nodecheck import run_node_checks

        results: list[dict] = []
        checked: set[str] = set()
        for role in ("client", "server"):
            node = self.nodes[role]
            if not node:
                results.append(
                    {
                        "target": f"node:{role}",
                        "name": "node_checks",
                        "passed": False,
                        "severity": "warning",
                        "message": f"no --{role}-node pinned; host-level checks skipped",
                    }
                )
                continue
            key = f"{node}:{role}"
            if key in checked:
                continue
            checked.add(key)
            pod = f"pb-nodecheck-{node}".replace(".", "-").replace("_", "-")[:63].rstrip("-")
            self.kubectl.apply(
                render_node_check_pod(pod, node, self.image, self.kubectl.namespace)
            )
            try:
                self.kubectl.wait_ready(pod, self.ready_timeout_s)
                executor = PodExecutor(self.kubectl, pod)
                checks = run_node_checks(executor, scenario, role)
            finally:
                self.kubectl.delete_pod(pod)
            results.extend({"target": f"node:{node}", **c.to_dict()} for c in checks)
        return results

    def teardown_scenario(self, scenario: Scenario) -> None:
        """Best-effort cleanup by name — safe even if provisioning failed
        halfway (delete uses --ignore-not-found)."""
        for role in ("client", "server"):
            self.kubectl.delete_pod(self.pod_name(scenario, role))


def render_benchmark_pod(
    name: str,
    scenario: Scenario,
    role: str,
    image: str,
    node: Optional[str] = None,
    networks: Sequence[str] = (),
    nic_resource: Optional[str] = None,
    hugepages_2mi: str = "1Gi",
    namespace: str = "perfbench",
    static_ip: Optional[str] = None,
    isolated_cpus_resource: str = DEFAULT_ISOLATED_CPUS_RESOURCE,
    onload_resource: str = DEFAULT_ONLOAD_RESOURCE,
    data_path_interface: Optional[str] = None,
) -> str:
    """Render a low-latency benchmark pod manifest (device-plugin model).

    Exclusive cores and the Onload userland come from *device plugins* rather
    than static config:

    * ``<isolated_cpus_resource>: <N>`` — the plugin pins N isolated cores and
      injects their ids as ``ISOLATED_CPUS`` (the harness ``taskset``\\s onto
      that at runtime). N is ``scenario.cpu.request_count(role)``.
    * ``<onload_resource>: 1`` (bypass scenarios) — the plugin mounts
      ``/dev/onload``/``/dev/onload_epoll``, injects the library and sets
      ``ONLOAD_LIB`` (the harness ``LD_PRELOAD``\\s it). No hostPath needed.

    The integral ``cpu`` request is intentionally dropped: the isolated cores
    are owned by the plugin (and ``isolcpus``), not kubelet's CPU manager, so
    requesting both would double-count. ``memory`` requests==limits and the
    single-numa-node Topology Manager keeps cores and devices NUMA-aligned.

    The data-path NIC is attached via the Multus annotation. ``nic_resource``
    is the device-plugin resource that delivers the NIC and is requested on
    *every* scenario (kernel included) — required when the NAD uses
    ``capabilities: {deviceID: true}``, since the deviceID is only injected if
    the pod requests that resource (otherwise the CNI cannot create the network
    and the pod never starts). With pure PF-IOV host-device (no deviceID) it
    can be left unset. ``onload_resource`` is requested only for bypass
    scenarios; set it empty when one resource bundles the NIC *and* Onload (the
    Onload ``LD_PRELOAD`` still applies, driven by ``network_path``). A static
    ``ips`` request on the data-path network is supported either way.

    ``data_path_interface`` pins the in-pod name of the base secondary
    interface (e.g. ``net1``) so a VLAN can later be layered on top of it
    inside the pod (see :meth:`K8sBenchSession._setup_vlan`).
    """
    isolated_count = str(scenario.cpu.request_count(role))
    resources: dict = {
        isolated_cpus_resource: isolated_count,
        "memory": "2Gi",
        "hugepages-2Mi": hugepages_2mi,
    }
    if nic_resource:
        resources[nic_resource] = "1"
    if onload_resource and scenario.network_path in (NetworkPath.ONLOAD, NetworkPath.EFVI):
        resources[onload_resource] = "1"

    container: dict = {
        "name": "bench",
        "image": image,
        "command": ["sleep", "infinity"],
        "securityContext": {
            "capabilities": {"add": ["NET_RAW", "NET_ADMIN", "IPC_LOCK", "SYS_NICE"]},
        },
        "resources": {"requests": dict(resources), "limits": dict(resources)},
        "volumeMounts": [
            {"name": "hugepages", "mountPath": "/dev/hugepages"},
        ],
    }

    volumes: list = [
        {"name": "hugepages", "emptyDir": {"medium": "HugePages"}},
    ]

    metadata: dict = {
        "name": name,
        "namespace": namespace,
        "labels": {
            "app": "perfbench",
            "perfbench/scenario": scenario.id,
            "perfbench/role": role,
        },
    }
    if networks:
        metadata["annotations"] = {
            "k8s.v1.cni.cncf.io/networks": _networks_annotation(
                networks, static_ip, data_path_interface=data_path_interface
            )
        }

    spec: dict = {
        "restartPolicy": "Never",
        "terminationGracePeriodSeconds": 2,
        "containers": [container],
        "volumes": volumes,
    }
    if node:
        spec["nodeName"] = node

    manifest = {"apiVersion": "v1", "kind": "Pod", "metadata": metadata, "spec": spec}
    return yaml.safe_dump(manifest, sort_keys=False)
