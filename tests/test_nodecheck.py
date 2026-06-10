import unittest

import yaml

from perfbench.capture.nodecheck import run_node_checks
from perfbench.runner.base import ExecResult, FakeExecutor
from perfbench.runner.k8s import render_node_check_pod
from tests.helpers import make_scenario
from tests.test_capture import _fail, _ok

GOOD_NODE = {
    "pgrep -x irqbalance": _fail(),
    "tuned-adm active": _ok("Current active profile: network-latency"),
    "/proc/interrupts": _ok("141\n142"),
    "smp_affinity_list": _ok("6"),  # = scenario irq_cores
}


class TestNodeChecks(unittest.TestCase):
    def _run(self, overrides=None, scenario=None, role="client"):
        responses = dict(GOOD_NODE)
        responses.update(overrides or {})
        return run_node_checks(
            FakeExecutor(responses=responses), scenario or make_scenario(), role
        )

    def _by_name(self, results, name):
        return [r for r in results if r.name == name][0]

    def test_all_good(self):
        results = self._run()
        self.assertTrue(all(r.passed for r in results), [r.message for r in results])
        self.assertIn("hostPID-verified", self._by_name(results, "node_irqbalance").message)
        self.assertIn(
            "network-latency", self._by_name(results, "node_tuned").message
        )

    def test_irqbalance_running_fatal(self):
        results = self._run({"pgrep -x irqbalance": _ok("812")})
        check = self._by_name(results, "node_irqbalance")
        self.assertFalse(check.passed)
        self.assertEqual(check.severity, "fatal")

    def test_irq_affinity_overlap_is_fatal(self):
        # NIC irq steered onto benchmark core 4
        results = self._run({"smp_affinity_list": _ok("4-5")})
        check = self._by_name(results, "nic_irq_affinity")
        self.assertFalse(check.passed)
        self.assertEqual(check.severity, "fatal")
        self.assertIn("overlaps", check.message)

    def test_irq_outside_irq_cores_is_warning(self):
        # not on benchmark cores, but not on declared irq_cores (6) either
        results = self._run({"smp_affinity_list": _ok("0")})
        check = self._by_name(results, "nic_irq_affinity")
        self.assertFalse(check.passed)
        self.assertEqual(check.severity, "warning")
        self.assertIn("outside irq_cores", check.message)

    def test_no_irqs_found_is_warning(self):
        results = self._run({"/proc/interrupts": _ok("")})
        check = self._by_name(results, "nic_irq_affinity")
        self.assertFalse(check.passed)
        self.assertEqual(check.severity, "warning")
        self.assertIn("no IRQs found", check.message)

    def test_tuned_missing_is_warning(self):
        results = self._run({"tuned-adm active": _fail("not found", 127)})
        check = self._by_name(results, "node_tuned")
        self.assertFalse(check.passed)
        self.assertEqual(check.severity, "warning")

    def test_tuned_non_latency_profile_warns(self):
        results = self._run({"tuned-adm active": _ok("Current active profile: balanced")})
        check = self._by_name(results, "node_tuned")
        self.assertFalse(check.passed)
        self.assertIn("balanced", check.message)

    def test_server_role_uses_server_cores(self):
        scenario = make_scenario(
            cpu={"client_cores": [4], "server_cores": [10], "irq_cores": [6],
                 "require_isolated": True}
        )
        results = self._run({"smp_affinity_list": _ok("10")},
                            scenario=scenario, role="server")
        check = self._by_name(results, "nic_irq_affinity")
        self.assertEqual(check.severity, "fatal")


class TestRenderNodeCheckPod(unittest.TestCase):
    def test_manifest(self):
        pod = yaml.safe_load(
            render_node_check_pod("pb-nodecheck-w1", "worker-1", "img:1", "bench-ns")
        )
        self.assertEqual(pod["spec"]["nodeName"], "worker-1")
        self.assertTrue(pod["spec"]["hostPID"])
        container = pod["spec"]["containers"][0]
        self.assertTrue(container["securityContext"]["privileged"])
        mount = container["volumeMounts"][0]
        self.assertEqual(mount["mountPath"], "/host")
        self.assertTrue(mount["readOnly"])
        self.assertEqual(pod["spec"]["volumes"][0]["hostPath"]["path"], "/")
        self.assertEqual(pod["metadata"]["namespace"], "bench-ns")


if __name__ == "__main__":
    unittest.main()
