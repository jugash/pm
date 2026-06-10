import tempfile
import unittest
from pathlib import Path

from perfbench.errors import PreflightError
from perfbench.results.store import ResultStore
from perfbench.runner.base import ExecResult, FakeExecutor
from perfbench.runner.orchestrator import Orchestrator
from tests.helpers import SOCKPERF_OUTPUT, make_scenario
from tests.test_capture import GOOD_HOST, _fail, _ok


def _executors():
    responses = dict(GOOD_HOST)
    responses["sockperf ping-pong"] = _ok(SOCKPERF_OUTPUT)
    client = FakeExecutor(name="client", responses=dict(responses))
    server = FakeExecutor(name="server", responses=dict(responses))
    return client, server


def _orchestrator(client, server, **kwargs):
    kwargs.setdefault("sleep", lambda s: None)
    return Orchestrator(client=client, server=server, server_address="10.0.0.2", **kwargs)


class TestPlan(unittest.TestCase):
    def test_plan_resolves_commands(self):
        client, server = _executors()
        plans = _orchestrator(client, server).plan(make_scenario())
        self.assertEqual(len(plans), 1)
        self.assertIn("sockperf server", plans[0].server_command)
        self.assertIn("-i 10.0.0.2", plans[0].client_command)
        self.assertGreater(plans[0].timeout_s, 0)


class TestRunScenario(unittest.TestCase):
    def test_full_run_persists_records(self):
        client, server = _executors()
        events = []
        store = ResultStore()
        with tempfile.TemporaryDirectory() as tmp:
            orch = _orchestrator(
                client, server, store=store, output_dir=Path(tmp), on_event=events.append
            )
            records = orch.run_scenario(make_scenario(repetitions=2))

            self.assertEqual(len(records), 2)
            for record in records:
                self.assertEqual(record.scenario_id, "bm-onload-test")
                self.assertEqual(len(record.tool_runs), 1)
                self.assertTrue(record.tool_runs[0].ok)
                self.assertGreater(len(record.tool_runs[0].measurements), 5)
                # derived jitter metric present (p99.9 - p50 from sockperf output)
                jitter = [m for m in record.tool_runs[0].measurements
                          if m.metric == "latency_jitter_ns"]
                self.assertEqual(len(jitter), 1)
                self.assertAlmostEqual(jitter[0].value, 7654.0 - 2341.0)
                self.assertTrue(record.finished_at)
                # context label stamped
                self.assertEqual(
                    record.tool_runs[0].measurements[0].labels["interface"], "ens1f0"
                )
                # environment captured from both sides
                self.assertIn("client", record.env)
                self.assertIn("server", record.env)
                # preflight recorded for both targets
                targets = {p["target"] for p in record.preflight}
                self.assertEqual(targets, {"client", "server"})
            # persisted
            self.assertEqual(len(store.list_runs()), 2)
            # raw output saved
            outs = list(Path(tmp).glob("*-sockperf.out"))
            self.assertEqual(len(outs), 2)
            self.assertIn(SOCKPERF_OUTPUT.splitlines()[1], outs[0].read_text())
        # server started then stopped
        self.assertTrue(any(c.startswith("START") for c in server.calls))
        self.assertTrue(any("preflight" in e for e in events))

    def test_preflight_failure_aborts(self):
        client, server = _executors()
        client.responses["pgrep -x irqbalance"] = _ok("999")  # running -> fatal
        with self.assertRaises(PreflightError) as ctx:
            _orchestrator(client, server).run_scenario(make_scenario())
        self.assertIn("irqbalance", str(ctx.exception))

    def test_skip_preflight(self):
        client, server = _executors()
        client.responses["pgrep -x irqbalance"] = _ok("999")
        records = _orchestrator(client, server).run_scenario(
            make_scenario(), skip_preflight=True
        )
        self.assertEqual(records[0].preflight, [])
        self.assertTrue(records[0].tool_runs[0].ok)

    def test_tool_failure_recorded_not_raised(self):
        client, server = _executors()
        client.responses["sockperf ping-pong"] = _fail("connection refused", 1)
        records = _orchestrator(client, server).run_scenario(make_scenario())
        tool_run = records[0].tool_runs[0]
        self.assertFalse(tool_run.ok)
        self.assertEqual(tool_run.exit_code, 1)
        self.assertIn("rc=1", tool_run.error)
        self.assertEqual(tool_run.measurements, [])

    def test_parse_error_recorded_not_raised(self):
        client, server = _executors()
        client.responses["sockperf ping-pong"] = _ok("garbage output")
        records = _orchestrator(client, server).run_scenario(make_scenario())
        tool_run = records[0].tool_runs[0]
        self.assertFalse(tool_run.ok)
        self.assertIn("parse error", tool_run.error)

    def test_client_only_tool_skips_server(self):
        scenario = make_scenario(
            tools=[{"name": "cyclictest", "params": {"duration_s": 1}}]
        )
        responses = dict(GOOD_HOST)
        responses["cyclictest"] = _ok(
            "T: 0 ( 1) P:99 I:1000 C: 1000 Min: 2 Act: 3 Avg: 3 Max: 27"
        )
        client = FakeExecutor(name="client", responses=responses)
        server = FakeExecutor(name="server", responses=dict(GOOD_HOST))
        records = _orchestrator(client, server).run_scenario(scenario)
        self.assertTrue(records[0].tool_runs[0].ok)
        self.assertIsNone(records[0].tool_runs[0].server_command)
        self.assertFalse(any(c.startswith("START") for c in server.calls))

    def test_bond_interfaces_in_env_capture(self):
        scenario = make_scenario(
            nic={
                "ports": [{"name": "ens1f0"}, {"name": "ens2f0"}],
                "bond_mode": "active_backup",
                "bond_name": "bond0",
            }
        )
        client, server = _executors()
        records = _orchestrator(client, server).run_scenario(scenario)
        captured = records[0].env["client"]["interfaces"]
        self.assertEqual(set(captured), {"ens1f0", "ens2f0", "bond0"})


if __name__ == "__main__":
    unittest.main()
