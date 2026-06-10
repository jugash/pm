import unittest

from perfbench.config.schema import (
    BondMode,
    CpuLayout,
    NetworkPath,
    NicLayout,
    OnloadTuning,
    Platform,
    Scenario,
    ToolSpec,
)
from perfbench.errors import SchemaError
from tests.helpers import BASE_SCENARIO, kernel_scenario, make_scenario


class TestScenario(unittest.TestCase):
    def test_valid_scenario(self):
        sc = make_scenario()
        self.assertEqual(sc.id, "bm-onload-test")
        self.assertIs(sc.platform, Platform.BAREMETAL)
        self.assertIs(sc.network_path, NetworkPath.ONLOAD)
        self.assertEqual(sc.cpu.client_cores, (2, 4))
        self.assertEqual(sc.onload.env["EF_POLL_USEC"], "100000")
        self.assertEqual(sc.tools[0].name, "sockperf")
        self.assertEqual(sc.tags, ("fixture",))

    def test_labels(self):
        labels = make_scenario().labels()
        self.assertEqual(
            labels,
            {
                "scenario": "bm-onload-test",
                "platform": "baremetal",
                "network_path": "onload",
                "bond": "none",
            },
        )

    def test_invalid_id(self):
        with self.assertRaises(SchemaError):
            make_scenario(id="Bad Id!")

    def test_missing_id(self):
        doc = {**BASE_SCENARIO}
        doc.pop("id")
        with self.assertRaises(SchemaError):
            Scenario.from_dict(doc)

    def test_unknown_keys_rejected(self):
        with self.assertRaises(SchemaError) as ctx:
            Scenario.from_dict({**BASE_SCENARIO, "bogus": 1})
        self.assertIn("bogus", str(ctx.exception))

    def test_not_a_mapping(self):
        with self.assertRaises(SchemaError):
            Scenario.from_dict([1, 2])

    def test_invalid_platform_enum(self):
        with self.assertRaises(SchemaError) as ctx:
            make_scenario(platform="vmware")
        self.assertIn("allowed", str(ctx.exception))

    def test_kernel_with_onload_rejected(self):
        doc = {**BASE_SCENARIO, "network_path": "kernel"}
        with self.assertRaises(SchemaError):
            Scenario.from_dict(doc)

    def test_onload_default_tuning(self):
        doc = {**BASE_SCENARIO}
        doc.pop("onload")
        sc = Scenario.from_dict(doc)
        self.assertIsInstance(sc.onload, OnloadTuning)
        self.assertEqual(sc.onload.profile, "latency")

    def test_kernel_scenario_has_no_onload(self):
        self.assertIsNone(kernel_scenario().onload)

    def test_repetitions_minimum(self):
        with self.assertRaises(SchemaError):
            make_scenario(repetitions=0)
        with self.assertRaises(SchemaError):
            make_scenario(repetitions="three")

    def test_tools_required(self):
        with self.assertRaises(SchemaError):
            make_scenario(tools=[])

    def test_tool_shorthand_string(self):
        sc = make_scenario(tools=["iperf3"])
        self.assertEqual(sc.tools[0], ToolSpec(name="iperf3", params={}))


class TestNicLayout(unittest.TestCase):
    def test_single_port(self):
        nic = NicLayout.from_dict({"ports": [{"name": "ens1f0"}]})
        self.assertIs(nic.bond_mode, BondMode.NONE)
        self.assertEqual(nic.interface, "ens1f0")
        self.assertEqual(nic.label(), "none:ens1f0")

    def test_bond_requires_two_ports(self):
        with self.assertRaises(SchemaError):
            NicLayout.from_dict(
                {"ports": [{"name": "ens1f0"}], "bond_mode": "active_backup",
                 "bond_name": "bond0"}
            )

    def test_bond_requires_name(self):
        with self.assertRaises(SchemaError):
            NicLayout.from_dict(
                {"ports": [{"name": "a"}, {"name": "b"}], "bond_mode": "lacp_8023ad"}
            )

    def test_bonded_layout(self):
        nic = NicLayout.from_dict(
            {
                "ports": [{"name": "ens1f0", "pci": "0000:3b:00.0"},
                          {"name": "ens2f0", "numa_node": 1}],
                "bond_mode": "active_backup",
                "bond_name": "bond0",
            }
        )
        self.assertEqual(nic.interface, "bond0")
        self.assertEqual(nic.label(), "active_backup:bond0(2)")
        self.assertEqual(nic.ports[1].numa_node, 1)

    def test_empty_ports(self):
        with self.assertRaises(SchemaError):
            NicLayout.from_dict({"ports": []})

    def test_port_requires_name(self):
        with self.assertRaises(SchemaError):
            NicLayout.from_dict({"ports": [{"card": "x2"}]})

    def test_negative_numa(self):
        with self.assertRaises(SchemaError):
            NicLayout.from_dict({"ports": [{"name": "e0", "numa_node": -1}]})

    def test_vendor_normalized_and_validated(self):
        nic = NicLayout.from_dict({"ports": [{"name": "e0"}], "vendor": "0X1924"})
        self.assertEqual(nic.vendor, "0x1924")
        self.assertIsNone(NicLayout.from_dict({"ports": [{"name": "e0"}]}).vendor)
        with self.assertRaises(SchemaError):
            NicLayout.from_dict({"ports": [{"name": "e0"}], "vendor": "solarflare"})
        with self.assertRaises(SchemaError):
            NicLayout.from_dict({"ports": [{"name": "e0"}], "vendor": "0x19245"})


class TestCpuLayout(unittest.TestCase):
    def test_roles(self):
        cpu = CpuLayout.from_dict({"client_cores": [1], "server_cores": [3]})
        self.assertEqual(cpu.cores_for_role("client"), (1,))
        self.assertEqual(cpu.cores_for_role("server"), (3,))
        with self.assertRaises(ValueError):
            cpu.cores_for_role("referee")

    def test_empty_cores_rejected(self):
        with self.assertRaises(SchemaError):
            CpuLayout.from_dict({"client_cores": [], "server_cores": [1]})
        with self.assertRaises(SchemaError):
            CpuLayout.from_dict({"client_cores": [1], "server_cores": []})

    def test_irq_overlap_rejected(self):
        with self.assertRaises(SchemaError):
            CpuLayout.from_dict(
                {"client_cores": [1, 2], "server_cores": [3], "irq_cores": [2]}
            )

    def test_negative_core(self):
        with self.assertRaises(SchemaError):
            CpuLayout.from_dict({"client_cores": [-1], "server_cores": [1]})

    def test_bool_is_not_int(self):
        with self.assertRaises(SchemaError):
            CpuLayout.from_dict({"client_cores": [True], "server_cores": [1]})

    def test_require_isolated_must_be_bool(self):
        with self.assertRaises(SchemaError):
            CpuLayout.from_dict(
                {"client_cores": [1], "server_cores": [2], "require_isolated": "yes"}
            )


class TestOnloadTuning(unittest.TestCase):
    def test_defaults(self):
        t = OnloadTuning.from_dict({})
        self.assertEqual(t.profile, "latency")
        self.assertEqual(dict(t.env), {})

    def test_env_key_validation(self):
        with self.assertRaises(SchemaError):
            OnloadTuning.from_dict({"env": {"not-an-env-var": "1"}})

    def test_profile_none_allowed(self):
        self.assertIsNone(OnloadTuning.from_dict({"profile": None}).profile)

    def test_profile_type(self):
        with self.assertRaises(SchemaError):
            OnloadTuning.from_dict({"profile": 7})

    def test_env_values_stringified(self):
        t = OnloadTuning.from_dict({"env": {"EF_POLL_USEC": 100000}})
        self.assertEqual(t.env["EF_POLL_USEC"], "100000")


class TestToolSpec(unittest.TestCase):
    def test_invalid_name(self):
        with self.assertRaises(SchemaError):
            ToolSpec.from_dict({"name": "UPPER CASE"})

    def test_params_must_be_mapping(self):
        with self.assertRaises(SchemaError):
            ToolSpec.from_dict({"name": "sockperf", "params": [1]})


if __name__ == "__main__":
    unittest.main()
