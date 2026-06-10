import unittest

from perfbench.capture.resolve import resolve_nic
from perfbench.errors import ExecutionError
from perfbench.runner.base import ExecResult, FakeExecutor
from tests.helpers import kernel_scenario, make_scenario


def _ok(stdout):
    return ExecResult(command="", exit_code=0, stdout=stdout)


LISTING = (
    "enp59s0f0|0x1924|0x0b03|0000:3b:00.0|0\n"
    "enp59s0f1|0x1924|0x0b03|0000:3b:00.1|0\n"
    "enp94s0f0|0x1924|0x0b03|0000:5e:00.0|1\n"
    "eno1|0x8086|0x37d2|0000:18:00.0|0\n"
)


def _host(listing=LISTING):
    return FakeExecutor(responses={"/sys/class/net/*/device": _ok(listing)})


def _auto_scenario(ports, **overrides):
    return make_scenario(nic={"vendor": "0x1924", "ports": ports}, **overrides)


class TestResolveNic(unittest.TestCase):
    def test_already_named_is_noop_without_remote_calls(self):
        executor = _host()
        scenario = make_scenario()  # ens1f0 named in fixture
        self.assertIs(resolve_nic(executor, scenario), scenario)
        self.assertEqual(executor.calls, [])

    def test_resolve_by_vendor_and_numa(self):
        scenario = _auto_scenario([{"card": "x2522-a", "numa_node": 0}])
        resolved = resolve_nic(_host(), scenario)
        port = resolved.nic.ports[0]
        self.assertEqual(port.name, "enp59s0f0")  # first solarflare on numa 0
        self.assertEqual(port.pci, "0000:3b:00.0")  # enriched
        self.assertEqual(port.card, "x2522-a")  # preserved
        self.assertEqual(resolved.nic.interface, "enp59s0f0")
        # original untouched
        self.assertIsNone(scenario.nic.ports[0].name)

    def test_resolve_two_ports_distinct(self):
        scenario = make_scenario(
            nic={
                "vendor": "0x1924",
                "bond_mode": "active_backup",
                "bond_name": "bond0",
                "ports": [{"numa_node": 0}, {"numa_node": 0}],
            },
        )
        resolved = resolve_nic(_host(), scenario)
        names = [p.name for p in resolved.nic.ports]
        self.assertEqual(names, ["enp59s0f0", "enp59s0f1"])
        self.assertEqual(resolved.nic.interface, "bond0")

    def test_resolve_by_exact_pci(self):
        scenario = _auto_scenario([{"pci": "0000:5E:00.0"}])  # case-insensitive
        resolved = resolve_nic(_host(), scenario)
        self.assertEqual(resolved.nic.ports[0].name, "enp94s0f0")
        self.assertEqual(resolved.nic.ports[0].numa_node, 1)

    def test_numa_filter(self):
        scenario = _auto_scenario([{"numa_node": 1}])
        resolved = resolve_nic(_host(), scenario)
        self.assertEqual(resolved.nic.ports[0].name, "enp94s0f0")

    def test_kernel_scenario_uses_explicit_vendor(self):
        scenario = kernel_scenario(
            nic={"vendor": "0x8086", "ports": [{"numa_node": 0}]}
        )
        resolved = resolve_nic(_host(), scenario)
        self.assertEqual(resolved.nic.ports[0].name, "eno1")

    def test_no_pci_match_raises_with_inventory(self):
        scenario = _auto_scenario([{"pci": "0000:ff:00.0"}])
        with self.assertRaises(ExecutionError) as ctx:
            resolve_nic(_host(), scenario)
        self.assertIn("0000:ff:00.0", str(ctx.exception))
        self.assertIn("available", str(ctx.exception))

    def test_no_vendor_match_raises(self):
        scenario = _auto_scenario([{"numa_node": 0}])
        with self.assertRaises(ExecutionError) as ctx:
            resolve_nic(_host("eno1|0x8086|0x37d2|0000:18:00.0|0\n"), scenario)
        self.assertIn("vendor 0x1924", str(ctx.exception))

    def test_more_ports_than_matches_raises(self):
        listing = "sf0|0x1924|0x0b03|0000:3b:00.0|0\n"
        scenario = make_scenario(
            nic={
                "vendor": "0x1924",
                "bond_mode": "active_backup",
                "bond_name": "bond0",
                "ports": [{"numa_node": 0}, {"numa_node": 0}],
            }
        )
        with self.assertRaises(ExecutionError):
            resolve_nic(_host(listing), scenario)

    def test_mixed_named_and_auto(self):
        scenario = make_scenario(
            nic={
                "vendor": "0x1924",
                "bond_mode": "active_backup",
                "bond_name": "bond0",
                "ports": [{"name": "enp59s0f0"}, {}],
            }
        )
        resolved = resolve_nic(_host(), scenario)
        names = [p.name for p in resolved.nic.ports]
        # auto port must not re-pick the explicitly named one
        self.assertEqual(names, ["enp59s0f0", "enp59s0f1"])


if __name__ == "__main__":
    unittest.main()
