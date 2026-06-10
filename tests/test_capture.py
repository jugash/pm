import unittest

from perfbench.capture.envsnapshot import collect_environment
from perfbench.capture.preflight import (
    SEV_FATAL,
    fatal_failures,
    parse_cpu_list,
    run_preflight,
)
from perfbench.runner.base import ExecResult, FakeExecutor
from tests.helpers import kernel_scenario, make_scenario


def _ok(stdout: str) -> ExecResult:
    return ExecResult(command="", exit_code=0, stdout=stdout)


def _fail(stderr: str = "", code: int = 1) -> ExecResult:
    return ExecResult(command="", exit_code=code, stderr=stderr)


GOOD_HOST = {
    "pgrep -x irqbalance": _fail(),  # not running -> good
    "/proc/1/comm": _ok("systemd"),  # real host, pid namespace visible
    "isolated": _ok("2,4,6"),
    "scaling_governor": _ok("performance"),
    "smt/control": _ok("off"),
    "transparent_hugepage": _ok("always madvise [never]"),
    "ethtool -i": _ok("driver: sfc\nversion: 5.3.16\nfirmware-version: 8.2.7\n"),
    "device/vendor": _ok("0x1924"),  # Solarflare PCI vendor id
    "readlink -f": _ok("0000:3b:00.0"),
    "onload --version": _ok("OpenOnload 8.1.2\n"),
}


class TestParseCpuList(unittest.TestCase):
    def test_forms(self):
        self.assertEqual(parse_cpu_list(""), set())
        self.assertEqual(parse_cpu_list("3"), {3})
        self.assertEqual(parse_cpu_list("2-5,8,10-11"), {2, 3, 4, 5, 8, 10, 11})
        self.assertEqual(parse_cpu_list(" 1, 2 \n"), {1, 2})


class TestPreflight(unittest.TestCase):
    def test_all_pass_on_good_host(self):
        executor = FakeExecutor(responses=dict(GOOD_HOST))
        results = run_preflight(executor, make_scenario(), role="client")
        self.assertEqual(fatal_failures(results), [])
        names = {r.name for r in results}
        self.assertEqual(
            names,
            {"irqbalance", "cpu_isolation", "cpu_governor", "smt",
             "transparent_hugepages", "nic_driver", "nic_identity", "onload"},
        )

    def test_irqbalance_running_is_fatal(self):
        responses = dict(GOOD_HOST)
        responses["pgrep -x irqbalance"] = _ok("1234")
        results = run_preflight(FakeExecutor(responses=responses), make_scenario())
        fatals = fatal_failures(results)
        self.assertEqual([f.name for f in fatals], ["irqbalance"])

    def test_irqbalance_unverifiable_in_container(self):
        responses = dict(GOOD_HOST)
        responses["/proc/1/comm"] = _ok("sleep")  # pod pid 1
        results = run_preflight(FakeExecutor(responses=responses), make_scenario())
        self.assertEqual(fatal_failures(results), [])  # warning, not fatal
        irq = [r for r in results if r.name == "irqbalance"][0]
        self.assertFalse(irq.passed)
        self.assertIn("cannot verify from container", irq.message)

    def test_missing_isolation_is_fatal(self):
        responses = dict(GOOD_HOST)
        responses["isolated"] = _ok("2")  # core 4 missing
        results = run_preflight(FakeExecutor(responses=responses), make_scenario())
        fatals = fatal_failures(results)
        self.assertEqual([f.name for f in fatals], ["cpu_isolation"])
        self.assertIn("[4]", fatals[0].message)

    def test_isolation_read_failure_is_fatal(self):
        responses = dict(GOOD_HOST)
        responses["isolated"] = _fail("no such file")
        fatals = fatal_failures(run_preflight(FakeExecutor(responses=responses), make_scenario()))
        self.assertEqual([f.name for f in fatals], ["cpu_isolation"])

    def test_isolation_check_disabled(self):
        scenario = make_scenario(
            cpu={"client_cores": [2], "server_cores": [3], "require_isolated": False}
        )
        responses = dict(GOOD_HOST)
        responses["isolated"] = _ok("")  # nothing isolated, but check disabled
        results = run_preflight(FakeExecutor(responses=responses), scenario)
        self.assertEqual(fatal_failures(results), [])

    def test_governor_smt_thp_are_warnings(self):
        responses = dict(GOOD_HOST)
        responses["scaling_governor"] = _ok("powersave\nperformance")
        responses["smt/control"] = _ok("on")
        responses["transparent_hugepage"] = _ok("[always] madvise never")
        results = run_preflight(FakeExecutor(responses=responses), make_scenario())
        self.assertEqual(fatal_failures(results), [])  # warnings only
        warned = {r.name for r in results if not r.passed}
        self.assertEqual(warned, {"cpu_governor", "smt", "transparent_hugepages"})

    def test_nic_driver_failure_is_fatal(self):
        responses = dict(GOOD_HOST)
        responses["ethtool -i"] = _fail("no such device")
        fatals = fatal_failures(run_preflight(FakeExecutor(responses=responses), make_scenario()))
        self.assertEqual([f.name for f in fatals], ["nic_driver"])

    def _identity(self, results):
        return [r for r in results if r.name == "nic_identity"][0]

    def test_nic_identity_verified_by_vendor(self):
        results = run_preflight(FakeExecutor(responses=dict(GOOD_HOST)), make_scenario())
        check = self._identity(results)
        self.assertTrue(check.passed, check.message)
        self.assertIn("ens1f0=0x1924", check.message)

    def test_nic_identity_wrong_vendor_fatal_for_bypass(self):
        responses = dict(GOOD_HOST)
        responses["device/vendor"] = _ok("0x8086")  # intel, not solarflare
        results = run_preflight(FakeExecutor(responses=responses), make_scenario())
        fatals = fatal_failures(results)
        self.assertIn("nic_identity", [f.name for f in fatals])
        self.assertIn("expected 0x1924 (Solarflare)", self._identity(results).message)

    def test_nic_identity_kernel_scenario_any_vendor_ok(self):
        responses = dict(GOOD_HOST)
        responses["device/vendor"] = _ok("0x8086")
        results = run_preflight(FakeExecutor(responses=responses), kernel_scenario())
        self.assertTrue(self._identity(results).passed)

    def test_nic_identity_explicit_vendor_enforced_on_kernel(self):
        scenario = kernel_scenario(
            nic={"ports": [{"name": "eth7"}], "vendor": "0x1924"}
        )
        responses = dict(GOOD_HOST)
        responses["device/vendor"] = _ok("0x8086")
        results = run_preflight(FakeExecutor(responses=responses), scenario)
        self.assertFalse(self._identity(results).passed)

    def test_nic_identity_pci_drift_fatal(self):
        scenario = make_scenario(
            nic={"ports": [{"name": "ens1f0", "pci": "0000:5e:00.0"}]}
        )
        results = run_preflight(FakeExecutor(responses=dict(GOOD_HOST)), scenario)
        check = self._identity(results)
        self.assertFalse(check.passed)
        self.assertIn("name drift", check.message)

    def test_nic_identity_pci_match_ok(self):
        scenario = make_scenario(
            nic={"ports": [{"name": "ens1f0", "pci": "0000:3B:00.0"}]}  # case-insensitive
        )
        results = run_preflight(FakeExecutor(responses=dict(GOOD_HOST)), scenario)
        self.assertTrue(self._identity(results).passed)

    def test_nic_identity_missing_interface_fatal(self):
        responses = dict(GOOD_HOST)
        responses["device/vendor"] = _fail("No such file or directory")
        results = run_preflight(FakeExecutor(responses=responses), make_scenario())
        check = self._identity(results)
        self.assertFalse(check.passed)
        self.assertIn("no such interface", check.message)

    def test_onload_missing_fatal_for_bypass_only(self):
        responses = dict(GOOD_HOST)
        responses["onload --version"] = _fail("not found", 127)
        fatals = fatal_failures(run_preflight(FakeExecutor(responses=responses), make_scenario()))
        self.assertEqual([f.name for f in fatals], ["onload"])
        # kernel path: onload not required
        results = run_preflight(FakeExecutor(responses=responses), kernel_scenario())
        self.assertEqual(fatal_failures(results), [])

    def test_check_result_to_dict(self):
        results = run_preflight(FakeExecutor(responses=dict(GOOD_HOST)), make_scenario())
        d = results[0].to_dict()
        self.assertEqual(set(d), {"name", "passed", "severity", "message"})
        self.assertIn(d["severity"], (SEV_FATAL, "warning"))

    def test_server_role_uses_server_cores(self):
        scenario = make_scenario(
            cpu={"client_cores": [2], "server_cores": [10], "require_isolated": True}
        )
        responses = dict(GOOD_HOST)
        responses["isolated"] = _ok("2")  # 10 not isolated
        fatals = fatal_failures(
            run_preflight(FakeExecutor(responses=responses), scenario, role="server")
        )
        self.assertEqual([f.name for f in fatals], ["cpu_isolation"])


class TestEnvSnapshot(unittest.TestCase):
    def test_collects_host_and_interface_data(self):
        executor = FakeExecutor(
            responses={
                "uname -r": _ok("5.15.0-rt"),
                "ethtool -i": _ok("driver: sfc"),
                "tuned-adm": _fail("not installed", 127),
            },
            name="snapshot",
        )
        snapshot = collect_environment(executor, interfaces=["ens1f0"])
        self.assertEqual(snapshot["kernel"], "5.15.0-rt")
        self.assertEqual(snapshot["target"], "fake:snapshot")
        self.assertTrue(snapshot["tuned_profile"].startswith("<error: rc=127"))
        self.assertIn("ens1f0", snapshot["interfaces"])
        self.assertEqual(snapshot["interfaces"]["ens1f0"]["driver_info"], "driver: sfc")
        self.assertIn("collected_at", snapshot)

    def test_executor_exception_is_captured(self):
        class Exploding(FakeExecutor):
            def run(self, command, timeout=None, env=None, input_data=None):
                raise RuntimeError("boom")

        snapshot = collect_environment(Exploding(), interfaces=[])
        self.assertTrue(snapshot["kernel"].startswith("<error: boom"))


if __name__ == "__main__":
    unittest.main()
