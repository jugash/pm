import unittest

from perfbench.config.schema import ToolSpec
from perfbench.errors import ParseError, SchemaError
from perfbench.tools import available_tools, create_tool
from perfbench.tools.base import onload_prefix, quantile_label, taskset_prefix
from tests.helpers import (
    CYCLICTEST_OUTPUT,
    EFLATENCY_OUTPUT,
    IPERF3_TCP_OUTPUT,
    IPERF3_UDP_OUTPUT,
    NETPERF_OUTPUT,
    SFNT_OUTPUT,
    SOCKPERF_OUTPUT,
    SYSJITTER_OUTPUT,
    kernel_scenario,
    make_scenario,
)


def _tool(name, **params):
    return create_tool(ToolSpec(name=name, params=params))


def _values(measurements, metric, **labels):
    out = []
    for m in measurements:
        if m.metric != metric:
            continue
        if all(m.labels.get(k) == v for k, v in labels.items()):
            out.append(m.value)
    return out


class TestRegistry(unittest.TestCase):
    def test_available(self):
        # subset check: other test modules may register fixture tools
        builtin = {"cyclictest", "eflatency", "iperf3", "netperf",
                   "sfnt-pingpong", "sockperf", "sysjitter"}
        self.assertTrue(builtin.issubset(set(available_tools())))

    def test_unknown_tool(self):
        with self.assertRaises(SchemaError):
            create_tool(ToolSpec(name="warpspeed"))

    def test_quantile_label(self):
        self.assertEqual(quantile_label(50.0), "0.5")
        self.assertEqual(quantile_label(99.0), "0.99")
        self.assertEqual(quantile_label(99.9), "0.999")
        self.assertEqual(quantile_label(99.999), "0.99999")
        self.assertEqual(quantile_label(0.0), "0")


class TestWrapping(unittest.TestCase):
    def test_taskset(self):
        self.assertEqual(taskset_prefix((2, 4)), "taskset -c 2,4 ")
        self.assertEqual(taskset_prefix(()), "")

    def test_onload_prefix_for_onload_scenario(self):
        prefix = onload_prefix(make_scenario())
        self.assertEqual(prefix, "env EF_POLL_USEC=100000 onload --profile=latency ")

    def test_onload_prefix_empty_for_kernel(self):
        self.assertEqual(onload_prefix(kernel_scenario()), "")

    def test_client_command_fully_wrapped(self):
        cmd = _tool("sockperf").client_command(make_scenario(), "10.0.0.2")
        self.assertEqual(
            cmd,
            "taskset -c 2,4 env EF_POLL_USEC=100000 onload --profile=latency "
            "sockperf ping-pong -i 10.0.0.2 -p 11111 -m 64 -t 10 --tcp",
        )

    def test_cpu_tools_not_onload_wrapped(self):
        cmd = _tool("cyclictest").client_command(make_scenario(), "10.0.0.2")
        self.assertNotIn("onload", cmd)

    def test_timeout_default_and_explicit(self):
        self.assertEqual(_tool("sockperf", duration_s=10).timeout_s(), 70.0)
        self.assertEqual(_tool("sockperf", timeout_s=5).timeout_s(), 5.0)
        # sfnt sweeps sizes: timeout scales
        t = _tool("sfnt-pingpong", duration_s=10, msg_sizes=[32, 64, 128])
        self.assertEqual(t.timeout_s(), 90.0)


class TestSfntPingpong(unittest.TestCase):
    def test_commands(self):
        tool = _tool("sfnt-pingpong", msg_sizes=[32, 64], duration_s=5, spin=True)
        scenario = make_scenario()
        self.assertIn("sfnt-pingpong", tool.server_command(scenario))
        client = tool.client_command(scenario, "10.0.0.2")
        self.assertIn("--sizes=32,64", client)
        self.assertIn("--maxms=5000", client)
        self.assertIn("--spin", client)
        self.assertTrue(client.endswith("tcp 10.0.0.2"))

    def test_parse(self):
        m = _tool("sfnt-pingpong").parse(SFNT_OUTPUT)
        self.assertEqual(_values(m, "latency_ns", msg_size="32", quantile="0.5"), [2461])
        self.assertEqual(_values(m, "latency_ns", msg_size="64", quantile="0.99"), [2853])
        self.assertEqual(_values(m, "latency_ns", msg_size="64", quantile="1"), [11876])
        self.assertEqual(_values(m, "latency_mean_ns", msg_size="32"), [2467])
        self.assertEqual(_values(m, "iterations_count", msg_size="32"), [1000000])

    def test_parse_empty_raises(self):
        with self.assertRaises(ParseError):
            _tool("sfnt-pingpong").parse("# only comments\n")


class TestSockperf(unittest.TestCase):
    def test_under_load_command(self):
        tool = _tool("sockperf", mode="under-load", mps=250000, protocol="udp")
        cmd = tool.client_command(make_scenario(), "10.0.0.2")
        self.assertIn("under-load", cmd)
        self.assertIn("--mps 250000", cmd)
        self.assertNotIn("--tcp", cmd)

    def test_bad_mode(self):
        with self.assertRaises(ParseError):
            _tool("sockperf", mode="warp").client_command(make_scenario(), "h")

    def test_parse(self):
        m = _tool("sockperf").parse(SOCKPERF_OUTPUT)
        self.assertEqual(_values(m, "latency_ns", quantile="0.999"), [7654.0])
        self.assertEqual(_values(m, "latency_ns", quantile="0.99999"), [12345.0])
        self.assertEqual(_values(m, "latency_ns", quantile="0"), [1987.0])
        self.assertEqual(_values(m, "latency_ns", quantile="1"), [45123.0])
        self.assertEqual(_values(m, "latency_mean_ns"), [2341.0])
        self.assertEqual(_values(m, "messages_dropped_count"), [0.0])
        self.assertTrue(all(x.labels["msg_size"] == "64" for x in m))

    def test_parse_garbage_raises(self):
        with self.assertRaises(ParseError):
            _tool("sockperf").parse("no latency here")


class TestNetperf(unittest.TestCase):
    def test_commands(self):
        tool = _tool("netperf", protocol="udp", msg_size=128)
        scenario = make_scenario()
        self.assertIn("netserver -D", tool.server_command(scenario))
        client = tool.client_command(scenario, "10.0.0.2")
        self.assertIn("-t UDP_RR", client)
        self.assertIn("-r 128,128", client)

    def test_parse(self):
        m = _tool("netperf").parse(NETPERF_OUTPUT)
        self.assertEqual(_values(m, "latency_ns", quantile="0"), [5000.0])
        self.assertEqual(_values(m, "latency_ns", quantile="0.99"), [12000.0])
        self.assertEqual(_values(m, "latency_mean_ns"), [7250.0])
        self.assertEqual(_values(m, "transactions_per_sec"), [137931.03])

    def test_parse_errors(self):
        tool = _tool("netperf")
        with self.assertRaises(ParseError):
            tool.parse("no header")
        with self.assertRaises(ParseError):
            tool.parse("MIN_LATENCY,MEAN_LATENCY\nnot,numbers")
        with self.assertRaises(ParseError):
            tool.parse("MIN_LATENCY,MEAN_LATENCY\n1,2,3")


class TestIperf3(unittest.TestCase):
    def test_commands(self):
        tool = _tool("iperf3", protocol="udp")
        cmd = tool.client_command(make_scenario(), "10.0.0.2")
        self.assertIn("-u -b 0", cmd)
        self.assertIn("-J", cmd)
        self.assertIn("iperf3 -s -1", tool.server_command(make_scenario()))

    def test_parse_tcp(self):
        m = _tool("iperf3").parse(IPERF3_TCP_OUTPUT)
        self.assertEqual(_values(m, "throughput_bps", direction="sent"), [9.41e9])
        self.assertEqual(_values(m, "tcp_retransmits_count"), [12.0])
        self.assertEqual(_values(m, "cpu_utilization_percent", side="host"), [35.5])

    def test_parse_udp(self):
        m = _tool("iperf3").parse(IPERF3_UDP_OUTPUT)
        self.assertEqual(_values(m, "throughput_bps"), [4.2e9])
        self.assertEqual(_values(m, "udp_jitter_ns"), [12000.0])
        self.assertEqual(_values(m, "udp_loss_percent"), [0.001])

    def test_parse_errors(self):
        with self.assertRaises(ParseError):
            _tool("iperf3").parse("not json")
        with self.assertRaises(ParseError):
            _tool("iperf3").parse("{}")


class TestSysjitter(unittest.TestCase):
    def test_command_and_no_server(self):
        tool = _tool("sysjitter", runtime_s=20, threshold_ns=100)
        scenario = make_scenario()
        self.assertIsNone(tool.server_command(scenario))
        cmd = tool.client_command(scenario, "ignored")
        self.assertEqual(cmd, "sysjitter --runtime 20 --cores 2,4 100")
        self.assertEqual(tool.timeout_s(), 80.0)

    def test_parse_per_core(self):
        m = _tool("sysjitter").parse(SYSJITTER_OUTPUT)
        self.assertEqual(_values(m, "interruption_ns", core="2", quantile="0.999"), [1200.0])
        self.assertEqual(_values(m, "interruption_ns", core="4", quantile="1"), [900.0])
        self.assertEqual(_values(m, "interruptions_per_sec", core="2"), [1.2])
        self.assertEqual(_values(m, "interrupted_percent", core="4"), [0.001])
        self.assertEqual(_values(m, "interruption_mean_ns", core="2"), [350.0])

    def test_parse_empty(self):
        with self.assertRaises(ParseError):
            _tool("sysjitter").parse("nothing useful")


class TestCyclictest(unittest.TestCase):
    def test_command(self):
        cmd = _tool("cyclictest").client_command(make_scenario(), "ignored")
        self.assertIn("-t 2", cmd)
        self.assertIn("-a 2,4", cmd)
        self.assertIn("--priority=99", cmd)
        self.assertIsNone(_tool("cyclictest").server_command(make_scenario()))

    def test_parse(self):
        m = _tool("cyclictest").parse(CYCLICTEST_OUTPUT)
        self.assertEqual(_values(m, "wakeup_latency_ns", thread="0", quantile="1"), [27000.0])
        self.assertEqual(_values(m, "wakeup_latency_ns", thread="1", quantile="0"), [2000.0])
        self.assertEqual(_values(m, "wakeup_latency_mean_ns", thread="1"), [3000.0])

    def test_parse_empty(self):
        with self.assertRaises(ParseError):
            _tool("cyclictest").parse("")


class TestEflatency(unittest.TestCase):
    def _efvi(self):
        return make_scenario(
            id="efvi-test", network_path="efvi", onload=None,
            tools=[{"name": "eflatency"}],
        )

    def test_requires_efvi_path(self):
        with self.assertRaises(ParseError):
            _tool("eflatency").client_command(make_scenario(), "h")

    def test_commands(self):
        tool = _tool("eflatency", iterations=50000)
        scenario = self._efvi()
        self.assertEqual(
            tool.server_command(scenario), "taskset -c 2,4 eflatency pong ens1f0"
        )
        self.assertEqual(
            tool.client_command(scenario, "ignored"),
            "taskset -c 2,4 eflatency -n 50000 ping ens1f0",
        )

    def test_parse(self):
        m = _tool("eflatency").parse(EFLATENCY_OUTPUT)
        self.assertEqual(_values(m, "latency_mean_ns"), [4123.0])
        self.assertEqual(_values(m, "latency_ns", quantile="0.999"), [5200.0])
        self.assertEqual(_values(m, "latency_ns", quantile="1"), [9000.0])

    def test_parse_us_units(self):
        m = _tool("eflatency").parse("mean: 4.1 us\nmax: 9.0 us\n")
        self.assertEqual(_values(m, "latency_mean_ns"), [4100.0])

    def test_parse_empty(self):
        with self.assertRaises(ParseError):
            _tool("eflatency").parse("eflatency: something went wrong")


if __name__ == "__main__":
    unittest.main()
