"""Multicast: schema, sockperf/sfnt-stream commands, orchestrator fan-out."""

import unittest

from perfbench.config.schema import MulticastSpec, Scenario, ToolSpec
from perfbench.errors import ParseError, SchemaError
from perfbench.runner.base import FakeExecutor
from perfbench.runner.orchestrator import Orchestrator
from perfbench.tools import create_tool
from tests.helpers import (
    BASE_SCENARIO,
    SFNT_STREAM_OUTPUT,
    SOCKPERF_OUTPUT,
    mcast_scenario,
)
from tests.test_capture import GOOD_HOST, _ok


def _tool(name, **params):
    return create_tool(ToolSpec(name=name, params=params))


def _values(measurements, metric, **labels):
    return [
        m.value
        for m in measurements
        if m.metric == metric
        and all(m.labels.get(k) == v for k, v in labels.items())
    ]


class TestMulticastSpec(unittest.TestCase):
    def test_defaults(self):
        spec = MulticastSpec.from_dict({})
        self.assertEqual(spec.group, "239.100.1.1")
        self.assertEqual(spec.port, 12000)
        self.assertEqual(spec.ttl, 1)
        self.assertEqual(spec.group_count, 1)
        self.assertEqual(spec.receivers, 1)

    def test_groups_consecutive(self):
        spec = MulticastSpec.from_dict({"group": "239.100.2.254", "group_count": 3})
        self.assertEqual(spec.groups(), ("239.100.2.254", "239.100.2.255", "239.100.3.0"))

    def test_non_multicast_group_rejected(self):
        with self.assertRaises(SchemaError):
            MulticastSpec.from_dict({"group": "10.0.0.1"})

    def test_invalid_address_rejected(self):
        with self.assertRaises(SchemaError):
            MulticastSpec.from_dict({"group": "not-an-ip"})

    def test_group_count_overflow_rejected(self):
        with self.assertRaises(SchemaError):
            MulticastSpec.from_dict({"group": "239.255.255.250", "group_count": 16})

    def test_bounds(self):
        for bad in ({"port": 0}, {"port": 70000}, {"ttl": 0}, {"ttl": 256},
                    {"group_count": 0}, {"receivers": 0}, {"unknown": 1}):
            with self.assertRaises(SchemaError, msg=bad):
                MulticastSpec.from_dict(bad)

    def test_scenario_integration(self):
        sc = mcast_scenario()
        self.assertIsNotNone(sc.multicast)
        self.assertEqual(sc.multicast.group, "239.100.1.1")
        # absent by default
        self.assertIsNone(Scenario.from_dict(dict(BASE_SCENARIO)).multicast)


class TestSockperfMulticast(unittest.TestCase):
    def test_tcp_rejected(self):
        with self.assertRaises(SchemaError):
            _tool("sockperf", protocol="tcp").client_command(mcast_scenario(), "10.0.0.2")

    def test_client_targets_group_not_server(self):
        cmd = _tool("sockperf", protocol="udp").client_command(mcast_scenario(), "10.0.0.2")
        self.assertIn("-i 239.100.1.1 -p 12000", cmd)
        self.assertNotIn("10.0.0.2", cmd)
        self.assertIn("--mc-ttl 1", cmd)
        self.assertIn("onload", cmd)  # still wrapped

    def test_server_joins_group(self):
        cmd = _tool("sockperf", protocol="udp").server_command(mcast_scenario())
        self.assertIn("sockperf server -i 239.100.1.1 -p 12000", cmd)

    def test_mc_interface_flags(self):
        tool = _tool("sockperf", protocol="udp", mc_rx_if="10.0.0.2", mc_tx_if="10.0.0.1")
        self.assertIn("--mc-rx-if 10.0.0.2", tool.server_command(mcast_scenario()))
        self.assertIn("--mc-tx-if 10.0.0.1", tool.client_command(mcast_scenario(), "x"))

    def test_feed_file_for_multiple_groups(self):
        sc = mcast_scenario(
            multicast={"group": "239.100.2.0", "port": 12000, "group_count": 3}
        )
        tool = _tool("sockperf", protocol="udp")
        for cmd in (tool.server_command(sc), tool.client_command(sc, "x")):
            self.assertIn("U:239.100.2.0:12000", cmd)
            self.assertIn("U:239.100.2.2:12000", cmd)
            self.assertIn("-f /tmp/perfbench-sockperf.feed", cmd)
            self.assertNotIn("-i 239", cmd)

    def test_fanout_server_commands_pinned_round_robin(self):
        sc = mcast_scenario(
            multicast={"group": "239.100.3.1", "receivers": 3},
            cpu={"client_cores": [4], "server_cores": [10, 11], "irq_cores": [2]},
        )
        cmds = _tool("sockperf", protocol="udp").server_commands(sc)
        self.assertEqual(len(cmds), 3)
        self.assertIn("taskset -c 10 ", cmds[0])
        self.assertIn("taskset -c 11 ", cmds[1])
        self.assertIn("taskset -c 10 ", cmds[2])
        for cmd in cmds:
            self.assertIn("-i 239.100.3.1", cmd)

    def test_single_receiver_one_server_command(self):
        cmds = _tool("sockperf", protocol="udp").server_commands(mcast_scenario())
        self.assertEqual(len(cmds), 1)

    def test_duplicates_parsed(self):
        out = SOCKPERF_OUTPUT.replace("# duplicated messages = 0",
                                      "# duplicated messages = 42")
        m = _tool("sockperf", protocol="udp").parse(out)
        self.assertEqual(_values(m, "messages_duplicated_count"), [42.0])


class TestSfntStream(unittest.TestCase):
    def test_commands(self):
        tool = _tool("sfnt-stream", rates=[10000, 50000, 10000], msg_size=128)
        sc = mcast_scenario()
        server = tool.server_command(sc)
        client = tool.client_command(sc, "10.0.0.2")
        self.assertIn("sfnt-stream", server)
        self.assertIn("--msgsize=128", client)
        self.assertIn("--rates=10000-50000+10000", client)
        self.assertIn("--mcast=239.100.1.1", client)
        self.assertIn("--mcastintf=ens1f0", client)
        self.assertIn("udp 10.0.0.2", client)
        self.assertIn("onload", client)

    def test_unicast_no_mcast_flags(self):
        from tests.helpers import make_scenario
        client = _tool("sfnt-stream").client_command(make_scenario(), "10.0.0.2")
        self.assertNotIn("--mcast", client)

    def test_tcp_with_multicast_rejected(self):
        with self.assertRaises(SchemaError):
            _tool("sfnt-stream", protocol="tcp").client_command(mcast_scenario(), "x")

    def test_bad_rates_rejected(self):
        with self.assertRaises(SchemaError):
            _tool("sfnt-stream", rates=[1, 2]).client_command(mcast_scenario(), "x")

    def test_parse_per_rate_rows(self):
        m = _tool("sfnt-stream", msg_size=64).parse(SFNT_STREAM_OUTPUT)
        self.assertEqual(_values(m, "latency_ns", mps="10000", quantile="0.5"), [2480.0])
        self.assertEqual(_values(m, "latency_ns", mps="100000", quantile="0.99"), [3100.0])
        self.assertEqual(_values(m, "latency_mean_ns", mps="100000"), [2600.0])
        self.assertEqual(_values(m, "messages_dropped_count", mps="100000"), [2.0])
        self.assertEqual(_values(m, "messages_dropped_count", mps="10000"), [0.0])
        for meas in m:
            self.assertEqual(meas.labels["msg_size"], "64")

    def test_parse_garbage_raises(self):
        with self.assertRaises(ParseError):
            _tool("sfnt-stream").parse("no rows here")

    def test_timeout_covers_sweep(self):
        tool = _tool("sfnt-stream", rates=[10000, 50000, 10000], duration_s=10)
        self.assertEqual(tool.timeout_s(), 10.0 * 5 + 60.0)


class TestOrchestratorFanout(unittest.TestCase):
    def _run(self, scenario):
        responses = dict(GOOD_HOST)
        responses["sockperf ping-pong"] = _ok(SOCKPERF_OUTPUT)
        client = FakeExecutor(name="client", responses=dict(responses))
        server = FakeExecutor(name="server", responses=dict(responses))
        orch = Orchestrator(
            client=client, server=server, server_address="10.0.0.2",
            sleep=lambda s: None,
        )
        return orch, client, server, orch.run_scenario(scenario)

    def test_fanout_starts_n_servers(self):
        sc = mcast_scenario(
            multicast={"group": "239.100.3.1", "receivers": 4},
            cpu={"client_cores": [4], "server_cores": [2, 4], "irq_cores": [6]},
        )
        _, _, server, records = self._run(sc)
        starts = [c for c in server.calls if c.startswith("START")]
        self.assertEqual(len(starts), 4)
        tool_run = records[0].tool_runs[0]
        self.assertTrue(tool_run.ok)
        self.assertEqual(len(tool_run.server_command.splitlines()), 4)
        # mcast context labels stamped
        labels = tool_run.measurements[0].labels
        self.assertEqual(labels["mcast_group"], "239.100.3.1")
        self.assertEqual(labels["mcast_receivers"], "4")
        self.assertEqual(labels["mcast_groups"], "1")

    def test_single_receiver_unchanged(self):
        _, _, server, records = self._run(mcast_scenario())
        starts = [c for c in server.calls if c.startswith("START")]
        self.assertEqual(len(starts), 1)
        self.assertTrue(records[0].tool_runs[0].ok)


if __name__ == "__main__":
    unittest.main()
