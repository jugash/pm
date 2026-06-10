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

    def pod_network_ip(self, pod: str, network: str) -> str:
        """IP on a secondary (Multus) network, from network-status annotation."""
        result = self._run(
            f"get pod {pod} -o jsonpath={{.metadata.annotations.k8s\\.v1\\.cni\\.cncf\\.io/network-status}}"
        )
        try:
            statuses = json.loads(result.stdout)
        except json.JSONDecodeError as exc:
            raise ExecutionError(f"cannot parse network-status for pod {pod}") from exc
        for status in statuses:
            if network in status.get("name", "") and status.get("ips"):
                return status["ips"][0]
        raise ExecutionError(f"pod {pod} has no IP on network {network}")

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
