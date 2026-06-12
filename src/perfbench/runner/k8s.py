"""Kubernetes execution support.

Built on ``kubectl`` driven through an injected executor (local by default),
so it works anywhere kubectl is configured and is fully unit-testable.

Two pieces:

* :class:`Kubectl` — manifest apply/delete, readiness waits, pod IP lookup.
* :class:`PodExecutor` — an :class:`Executor` that runs commands inside a
  running pod via ``kubectl exec``, letting the orchestrator treat pods
  exactly like SSH hosts.

``render_benchmark_pod`` produces a Guaranteed-QoS pod manifest with the
Multus network annotation, SR-IOV resource requests and the Onload device
mounts needed for kernel-bypass benchmarking inside a container.
"""

from __future__ import annotations

import json
import shlex
from dataclasses import dataclass
from typing import Mapping, Optional, Sequence

import yaml

from perfbench.config.schema import NetworkPath, Scenario
from perfbench.errors import ExecutionError
from perfbench.runner.base import BackgroundProcess, ExecResult, Executor
from perfbench.runner.local import LocalExecutor


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

    def _annotation(self, pod: str, key: str) -> str:
        escaped = key.replace(".", "\\.")
        result = self._run(
            f"get pod {pod} -o jsonpath={{.metadata.annotations.{escaped}}}"
        )
        return result.stdout.strip()

    def pod_network_ip(self, pod: str, network: str) -> str:
        """IP on a secondary (Multus) network, from the network-status
        annotation (``networks-status`` fallback for older Multus)."""
        raw = self._annotation(pod, "k8s.v1.cni.cncf.io/network-status")
        if not raw:
            raw = self._annotation(pod, "k8s.v1.cni.cncf.io/networks-status")
        if not raw:
            requested = self._annotation(pod, "k8s.v1.cni.cncf.io/networks")
            raise ExecutionError(
                f"pod {pod} has no Multus network-status annotation "
                f"(networks requested: {requested or 'NONE'}). Multus did not "
                f"attach the secondary network — check that the "
                f"NetworkAttachmentDefinition {network!r} exists in namespace "
                f"{self.namespace!r} (kubectl get net-attach-def -n "
                f"{self.namespace}) and that Multus/SR-IOV operator is running."
            )
        try:
            statuses = json.loads(raw)
        except json.JSONDecodeError as exc:
            raise ExecutionError(
                f"cannot parse network-status for pod {pod}: {raw[:200]!r}"
            ) from exc
        for status in statuses:
            if network in status.get("name", "") and status.get("ips"):
                return status["ips"][0]
        attached = [s.get("name", "?") for s in statuses]
        raise ExecutionError(
            f"pod {pod} has no IP on network {network!r}; attached networks: "
            f"{attached}"
        )

    def logs(self, pod: str) -> str:
        return self._run(f"logs {pod}").stdout


class PodExecutor(Executor):
    """Runs commands inside a pod via ``kubectl exec``."""

    def __init__(self, kubectl: Kubectl, pod: str, container: Optional[str] = None):
        self.kubectl = kubectl
        self.pod = pod
        self.container = container

    def describe(self) -> str:
        return f"k8s:{self.kubectl.namespace}/{self.pod}"

    def _exec_command(self, command: str, env: Optional[Mapping[str, str]]) -> str:
        if env:
            assigns = " ".join(f"{k}={shlex.quote(str(v))}" for k, v in sorted(env.items()))
            command = f"env {assigns} {command}"
        container = f" -c {self.container}" if self.container else ""
        return (
            f"{self.kubectl.base_command()} exec {self.pod}{container} -- "
            f"/bin/sh -c {shlex.quote(command)}"
        )

    def run(self, command, timeout=None, env=None, input_data=None) -> ExecResult:
        if input_data is not None:
            raise ExecutionError("PodExecutor does not support stdin input")
        return self.kubectl.executor.run(self._exec_command(command, env), timeout=timeout)

    def start(self, command, env=None) -> BackgroundProcess:
        return self.kubectl.executor.start(self._exec_command(command, env))


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
    """

    def __init__(
        self,
        kubectl: Kubectl,
        image: str,
        networks: Sequence[str] = (),
        sriov_resource: Optional[str] = None,
        client_node: Optional[str] = None,
        server_node: Optional[str] = None,
        hugepages_2mi: str = "1Gi",
        ready_timeout_s: int = 180,
    ):
        self.kubectl = kubectl
        self.image = image
        self.networks = tuple(networks)
        self.sriov_resource = sriov_resource
        self.nodes = {"client": client_node, "server": server_node}
        self.hugepages_2mi = hugepages_2mi
        self.ready_timeout_s = ready_timeout_s

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
                sriov_resource=self.sriov_resource,
                hugepages_2mi=self.hugepages_2mi,
                namespace=self.kubectl.namespace,
            )
            self.kubectl.apply(manifest)
        for role in ("server", "client"):
            self.kubectl.wait_ready(self.pod_name(scenario, role), self.ready_timeout_s)

        server_pod = self.pod_name(scenario, "server")
        if self.networks:
            address = self.kubectl.pod_network_ip(server_pod, self.networks[0])
        else:
            address = self.kubectl.pod_ip(server_pod)
        return BenchDeployment(
            client_pod=self.pod_name(scenario, "client"),
            server_pod=server_pod,
            server_address=address,
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
    sriov_resource: Optional[str] = None,
    hugepages_2mi: str = "1Gi",
    namespace: str = "perfbench",
) -> str:
    """Render a low-latency benchmark pod manifest.

    Guaranteed QoS (requests == limits, integral CPUs) so kubelet's static
    CPU manager grants exclusive cores; SR-IOV VF via Multus annotation;
    Onload device nodes mounted for kernel-bypass scenarios.
    """
    cores = scenario.cpu.cores_for_role(role)
    cpu_count = str(len(cores))
    resources: dict = {
        "cpu": cpu_count,
        "memory": "2Gi",
        "hugepages-2Mi": hugepages_2mi,
    }
    if sriov_resource:
        resources[sriov_resource] = "1"

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

    if scenario.network_path in (NetworkPath.ONLOAD, NetworkPath.EFVI):
        container["volumeMounts"].append({"name": "dev-onload", "mountPath": "/dev/onload"})
        container["volumeMounts"].append({"name": "dev-sfc", "mountPath": "/dev/sfc_char"})
        volumes.append({"name": "dev-onload", "hostPath": {"path": "/dev/onload"}})
        volumes.append({"name": "dev-sfc", "hostPath": {"path": "/dev/sfc_char"}})

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
        metadata["annotations"] = {"k8s.v1.cni.cncf.io/networks": ", ".join(networks)}

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
