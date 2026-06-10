import unittest

import yaml

from perfbench.errors import ExecutionError
from perfbench.runner.base import ExecResult, FakeExecutor
from perfbench.runner.k8s import Kubectl, PodExecutor, render_benchmark_pod
from tests.helpers import kernel_scenario, make_scenario


def _kubectl(responses=None, **kwargs):
    fake = FakeExecutor(responses=responses or {})
    return Kubectl(executor=fake, **kwargs), fake


class TestKubectl(unittest.TestCase):
    def test_base_command(self):
        k, _ = _kubectl(namespace="bench", context="lab", kubeconfig="/kc")
        self.assertEqual(
            k.base_command(), "kubectl -n bench --context lab --kubeconfig /kc"
        )

    def test_apply_pipes_manifest(self):
        k, fake = _kubectl()
        k.apply("kind: Pod")
        self.assertEqual(fake.calls, ["kubectl -n perfbench apply -f -"])

    def test_failure_raises(self):
        k, _ = _kubectl(
            responses={"delete pod": ExecResult("", 1, stderr="forbidden")}
        )
        with self.assertRaises(ExecutionError) as ctx:
            k.delete_pod("x")
        self.assertIn("forbidden", str(ctx.exception))

    def test_wait_and_logs(self):
        k, fake = _kubectl(responses={"logs": ExecResult("", 0, stdout="hello")})
        k.wait_ready("pod-a", timeout_s=30)
        self.assertIn("--for=condition=Ready pod/pod-a", fake.calls[0])
        self.assertEqual(k.logs("pod-a"), "hello")

    def test_pod_ip(self):
        k, _ = _kubectl(responses={"get pod": ExecResult("", 0, stdout="10.1.2.3")})
        self.assertEqual(k.pod_ip("p"), "10.1.2.3")
        k2, _ = _kubectl(responses={"get pod": ExecResult("", 0, stdout="")})
        with self.assertRaises(ExecutionError):
            k2.pod_ip("p")

    def test_pod_network_ip(self):
        status = (
            '[{"name": "default/cbr0", "ips": ["10.1.0.5"]},'
            ' {"name": "perfbench/sriov-trading", "ips": ["192.168.10.5"]}]'
        )
        k, _ = _kubectl(responses={"network-status": ExecResult("", 0, stdout=status)})
        self.assertEqual(k.pod_network_ip("p", "sriov-trading"), "192.168.10.5")
        with self.assertRaises(ExecutionError):
            k.pod_network_ip("p", "missing-net")
        k2, _ = _kubectl(responses={"network-status": ExecResult("", 0, stdout="not json")})
        with self.assertRaises(ExecutionError):
            k2.pod_network_ip("p", "x")


class TestPodExecutor(unittest.TestCase):
    def test_run_wraps_kubectl_exec(self):
        k, fake = _kubectl()
        executor = PodExecutor(k, "bench-client", container="bench")
        executor.run("sockperf ping-pong -i 1.2.3.4", env={"EF_POLL_USEC": "100000"})
        self.assertEqual(len(fake.calls), 1)
        call = fake.calls[0]
        self.assertIn("exec bench-client -c bench --", call)
        self.assertIn("env EF_POLL_USEC=100000 sockperf", call)
        self.assertEqual(executor.describe(), "k8s:perfbench/bench-client")

    def test_start_background(self):
        k, fake = _kubectl()
        PodExecutor(k, "bench-server").start("sockperf server")
        self.assertTrue(fake.calls[0].startswith("START kubectl -n perfbench exec"))

    def test_stdin_rejected(self):
        k, _ = _kubectl()
        with self.assertRaises(ExecutionError):
            PodExecutor(k, "p").run("cat", input_data="x")


class TestRenderBenchmarkPod(unittest.TestCase):
    def test_onload_pod(self):
        text = render_benchmark_pod(
            name="bench-client",
            scenario=make_scenario(),
            role="client",
            image="perfbench/bench:1",
            node="worker-1",
            networks=["sriov-trading"],
            sriov_resource="intel.com/sriov_sfc",
        )
        pod = yaml.safe_load(text)
        self.assertEqual(pod["kind"], "Pod")
        self.assertEqual(pod["metadata"]["name"], "bench-client")
        self.assertEqual(
            pod["metadata"]["annotations"]["k8s.v1.cni.cncf.io/networks"], "sriov-trading"
        )
        container = pod["spec"]["containers"][0]
        # Guaranteed QoS: requests == limits, integral cpu = len(client_cores)
        self.assertEqual(container["resources"]["requests"]["cpu"], "2")
        self.assertEqual(container["resources"]["requests"], container["resources"]["limits"])
        self.assertEqual(container["resources"]["limits"]["intel.com/sriov_sfc"], "1")
        mounts = {m["mountPath"] for m in container["volumeMounts"]}
        self.assertIn("/dev/onload", mounts)
        self.assertEqual(pod["spec"]["nodeName"], "worker-1")
        caps = container["securityContext"]["capabilities"]["add"]
        self.assertIn("NET_RAW", caps)

    def test_kernel_pod_has_no_onload_devices(self):
        text = render_benchmark_pod(
            name="b", scenario=kernel_scenario(), role="server", image="img"
        )
        pod = yaml.safe_load(text)
        mounts = {m["mountPath"] for m in pod["spec"]["containers"][0]["volumeMounts"]}
        self.assertNotIn("/dev/onload", mounts)
        self.assertNotIn("annotations", pod["metadata"])
        self.assertNotIn("nodeName", pod["spec"])


if __name__ == "__main__":
    unittest.main()
