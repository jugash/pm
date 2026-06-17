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

    @staticmethod
    def _pod_json(annotations):
        import json as _json

        return ExecResult(
            "", 0, stdout=_json.dumps({"metadata": {"annotations": annotations}})
        )

    def test_pod_network_ip(self):
        status = (
            '[{"name": "default/cbr0", "ips": ["10.1.0.5"]},'
            ' {"name": "perfbench/sriov-trading", "ips": ["192.168.10.5"]}]'
        )
        k, fake = _kubectl(responses={
            "--output json": self._pod_json({"k8s.v1.cni.cncf.io/network-status": status})
        })
        self.assertEqual(k.pod_network_ip("p", "sriov-trading"), "192.168.10.5")
        # no jsonpath: dotted annotation keys don't survive shell quoting
        self.assertNotIn("jsonpath", fake.calls[-1])
        with self.assertRaises(ExecutionError):
            k.pod_network_ip("p", "missing-net")
        k2, _ = _kubectl(responses={
            "--output json": self._pod_json({"k8s.v1.cni.cncf.io/network-status": "not json"})
        })
        with self.assertRaises(ExecutionError):
            k2.pod_network_ip("p", "x")

    def test_pod_network_ip_old_multus_annotation_fallback(self):
        # older Multus writes networks-status (plural)
        status = '[{"name": "perfbench/sriov-trading", "ips": ["192.168.10.5"]}]'
        k, _ = _kubectl(responses={
            "--output json": self._pod_json({"k8s.v1.cni.cncf.io/networks-status": status})
        })
        self.assertEqual(k.pod_network_ip("p", "sriov-trading"), "192.168.10.5")

    def test_pod_network_ip_missing_annotation_is_diagnostic(self):
        # no status annotation -> error explains what to check, including
        # what (if anything) the pod requested
        k, _ = _kubectl(responses={
            "--output json": self._pod_json({"k8s.v1.cni.cncf.io/networks": "sriov-trading"})
        })
        with self.assertRaises(ExecutionError) as ctx:
            k.pod_network_ip("p", "sriov-trading")
        msg = str(ctx.exception)
        self.assertIn("no Multus network-status annotation", msg)
        self.assertIn("networks requested: sriov-trading", msg)
        self.assertIn("net-attach-def", msg)

    def test_pod_network_ip_namespaced_request(self):
        # config may carry namespace/name; status may carry either form
        status = '[{"name": "perfbench/sriov-trading", "ips": ["192.168.10.5"]}]'
        k, _ = _kubectl(responses={
            "--output json": self._pod_json({"k8s.v1.cni.cncf.io/network-status": status})
        })
        self.assertEqual(
            k.pod_network_ip("p", "perfbench/sriov-trading"), "192.168.10.5"
        )
        # same name in a different namespace must NOT match
        with self.assertRaises(ExecutionError):
            k.pod_network_ip("p", "other-ns/sriov-trading")
        # bare names must match exactly, not by substring
        with self.assertRaises(ExecutionError):
            k.pod_network_ip("p", "trading")

    def test_pod_network_ip_attached_but_no_ipam(self):
        status = '[{"name": "perfbench/sriov-trading", "interface": "net1"}]'
        k, _ = _kubectl(responses={
            "--output json": self._pod_json({"k8s.v1.cni.cncf.io/network-status": status})
        })
        with self.assertRaises(ExecutionError) as ctx:
            k.pod_network_ip("p", "sriov-trading")
        msg = str(ctx.exception)
        self.assertIn("attached", msg)
        self.assertIn("IPAM", msg)

    def test_pod_network_ip_wrong_network_lists_attached(self):
        status = '[{"name": "perfbench/other-net", "ips": ["192.168.10.5"]}]'
        k, _ = _kubectl(responses={
            "--output json": self._pod_json({"k8s.v1.cni.cncf.io/network-status": status})
        })
        with self.assertRaises(ExecutionError) as ctx:
            k.pod_network_ip("p", "sriov-trading")
        self.assertIn("perfbench/other-net", str(ctx.exception))


class TestStaticIps(unittest.TestCase):
    def _render(self, **kwargs):
        from perfbench.runner.k8s import render_benchmark_pod

        return yaml.safe_load(render_benchmark_pod(
            name="pb-x-client", scenario=make_scenario(), role="client",
            image="bench:1", **kwargs,
        ))

    def test_annotation_plain_without_static_ip(self):
        manifest = self._render(networks=("sriov-trading", "other"))
        annotation = manifest["metadata"]["annotations"]["k8s.v1.cni.cncf.io/networks"]
        self.assertEqual(annotation, "sriov-trading, other")

    def test_annotation_json_with_static_ip(self):
        import json as _json

        manifest = self._render(
            networks=("perfbench/sriov-trading", "other"),
            static_ip="192.168.100.1/24",
        )
        annotation = manifest["metadata"]["annotations"]["k8s.v1.cni.cncf.io/networks"]
        entries = _json.loads(annotation)
        self.assertEqual(entries[0]["name"], "sriov-trading")
        self.assertEqual(entries[0]["namespace"], "perfbench")
        self.assertEqual(entries[0]["ips"], ["192.168.100.1/24"])
        # only the data-path (first) network gets the static IP
        self.assertEqual(entries[1], {"name": "other"})

    def test_session_uses_static_server_ip_directly(self):
        from perfbench.runner.k8s import K8sBenchSession

        kubectl, fake = _kubectl()  # no network-status response needed
        session = K8sBenchSession(
            kubectl, image="bench:1", networks=("sriov-trading",),
            client_ip="192.168.100.1/24", server_ip="192.168.100.2/24",
        )
        deployment = session.provision(make_scenario())
        self.assertEqual(deployment.server_address, "192.168.100.2")
        # static path never queries the pod for its annotations
        self.assertFalse(any("--output json" in c for c in fake.calls))
        # rendered pods carry per-role ips in the applied manifests
        applied = "\n".join(c for c in fake.calls if "apply" in c)
        self.assertNotIn("--output json", applied)


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


class TestK8sBenchSession(unittest.TestCase):
    def _session(self, networks=("sriov-trading",), responses=None):
        from perfbench.runner.k8s import K8sBenchSession

        base = {
            "--output json": TestKubectl._pod_json({
                "k8s.v1.cni.cncf.io/network-status":
                    '[{"name": "perfbench/sriov-trading", "ips": ["192.168.100.7"]}]',
            }),
        }
        base.update(responses or {})
        kubectl, fake = _kubectl(responses=base)
        session = K8sBenchSession(
            kubectl,
            image="bench:1",
            networks=networks,
            sriov_resource="amd.com/sfc_vf",
            client_node="worker-a",
            server_node="worker-b",
        )
        return session, fake

    def test_pod_name_sanitized(self):
        session, _ = self._session()
        sc = make_scenario(id="bm.onload_x2")
        self.assertEqual(session.pod_name(sc, "client"), "pb-bm-onload-x2-client")

    def test_pod_name_long_id_keeps_role_distinct(self):
        session, _ = self._session()
        sc = make_scenario(id="x" * 70)
        client = session.pod_name(sc, "client")
        server = session.pod_name(sc, "server")
        self.assertNotEqual(client, server)
        self.assertLessEqual(len(client), 63)
        self.assertTrue(client.endswith("-client"))
        self.assertTrue(server.endswith("-server"))

    def test_provision_applies_waits_and_resolves_address(self):
        session, fake = self._session()
        deployment = session.provision(make_scenario())
        self.assertEqual(deployment.client_pod, "pb-bm-onload-test-client")
        self.assertEqual(deployment.server_pod, "pb-bm-onload-test-server")
        self.assertEqual(deployment.server_address, "192.168.100.7")
        applies = [c for c in fake.calls if "apply -f -" in c]
        waits = [c for c in fake.calls if "--for=condition=Ready" in c]
        self.assertEqual(len(applies), 2)
        self.assertEqual(len(waits), 2)

    def test_provision_without_networks_uses_pod_ip(self):
        session, _ = self._session(
            networks=(), responses={"get pod": ExecResult("", 0, stdout="10.2.3.4")}
        )
        deployment = session.provision(make_scenario())
        self.assertEqual(deployment.server_address, "10.2.3.4")

    def test_node_preflight_with_pinned_nodes(self):
        responses = {
            "pgrep -x irqbalance": ExecResult("", 1),
            "tuned-adm active": ExecResult("", 0, stdout="network-latency"),
            "/proc/interrupts": ExecResult("", 0, stdout="141"),
            "smp_affinity_list": ExecResult("", 0, stdout="6"),
        }
        session, fake = self._session(responses=responses)
        results = session.node_preflight(make_scenario())
        # debug pods created and removed for both nodes
        applies = [c for c in fake.calls if "apply -f -" in c]
        deletes = [c for c in fake.calls if "delete pod pb-nodecheck-" in c]
        self.assertEqual(len(applies), 2)
        self.assertEqual(len(deletes), 2)
        targets = {r["target"] for r in results}
        self.assertEqual(targets, {"node:worker-a", "node:worker-b"})
        self.assertTrue(all(r["passed"] for r in results))

    def test_node_preflight_unpinned_reports_skip(self):
        from perfbench.runner.k8s import K8sBenchSession

        kubectl, _ = _kubectl()
        session = K8sBenchSession(kubectl, image="img")
        results = session.node_preflight(make_scenario())
        self.assertEqual(len(results), 2)
        self.assertTrue(all("skipped" in r["message"] for r in results))
        self.assertTrue(all(r["severity"] == "warning" for r in results))

    def test_node_preflight_deletes_pod_on_check_failure(self):
        session, fake = self._session(
            responses={"--for=condition=Ready": ExecResult("", 1, stderr="timeout")}
        )
        from perfbench.errors import ExecutionError

        with self.assertRaises(ExecutionError):
            session.node_preflight(make_scenario())
        self.assertTrue(any("delete pod pb-nodecheck-" in c for c in fake.calls))

    def test_executors_and_teardown(self):
        session, fake = self._session()
        deployment = session.provision(make_scenario())
        client, server = session.executors(deployment)
        self.assertIn("pb-bm-onload-test-client", client.describe())
        self.assertIn("pb-bm-onload-test-server", server.describe())
        session.teardown(deployment)
        deletes = [c for c in fake.calls if "delete pod" in c]
        self.assertEqual(len(deletes), 2)


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
        requests = container["resources"]["requests"]
        # device-plugin model: isolated CPUs requested as a resource (count =
        # len(client_cores)=2), no integral cpu request, onload device plugin
        self.assertEqual(requests["perfbench.io/isolated-cpus"], "2")
        self.assertNotIn("cpu", requests)
        self.assertEqual(requests, container["resources"]["limits"])
        self.assertEqual(requests["intel.com/sriov_sfc"], "1")
        self.assertEqual(requests["perfbench.io/onload"], "1")
        # devices come from the plugin now, not hostPath mounts/volumes
        mounts = {m["mountPath"] for m in container["volumeMounts"]}
        self.assertNotIn("/dev/onload", mounts)
        self.assertNotIn("hostPath", yaml.safe_dump(pod["spec"]["volumes"]))
        self.assertEqual(pod["spec"]["nodeName"], "worker-1")
        caps = container["securityContext"]["capabilities"]["add"]
        self.assertIn("NET_RAW", caps)

    def test_resource_names_are_configurable(self):
        text = render_benchmark_pod(
            name="c", scenario=make_scenario(), role="server", image="img",
            isolated_cpus_resource="amd.com/isolated_cpus",
            onload_resource="amd.com/onload",
        )
        requests = yaml.safe_load(text)["spec"]["containers"][0]["resources"]["requests"]
        self.assertIn("amd.com/isolated_cpus", requests)
        self.assertEqual(requests["amd.com/onload"], "1")

    def test_count_sizes_the_isolated_cpus_request(self):
        # cpu.count drives the request when no explicit cores are named
        sc = make_scenario(cpu={"count": 4, "require_isolated": True})
        text = render_benchmark_pod(name="c", scenario=sc, role="client", image="img")
        requests = yaml.safe_load(text)["spec"]["containers"][0]["resources"]["requests"]
        self.assertEqual(requests["perfbench.io/isolated-cpus"], "4")

    def test_kernel_pod_has_no_onload_resource(self):
        text = render_benchmark_pod(
            name="b", scenario=kernel_scenario(), role="server", image="img"
        )
        pod = yaml.safe_load(text)
        requests = pod["spec"]["containers"][0]["resources"]["requests"]
        self.assertNotIn("perfbench.io/onload", requests)
        self.assertIn("perfbench.io/isolated-cpus", requests)
        self.assertNotIn("annotations", pod["metadata"])
        self.assertNotIn("nodeName", pod["spec"])


if __name__ == "__main__":
    unittest.main()
