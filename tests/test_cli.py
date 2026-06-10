import json
import tempfile
import threading
import unittest
from pathlib import Path

import yaml
from click.testing import CliRunner

from perfbench.cli import main
from perfbench.export.exporter import MetricsStore, make_server
from perfbench.results.store import ResultStore
from perfbench.tools.base import register, ToolAdapter
from tests.helpers import BASE_SCENARIO


@register
class _EchoTool(ToolAdapter):
    """Test-only tool: runs `echo`/`sleep` so `run --transport local` works."""

    name = "echo-tool"
    needs_server = True
    uses_network = False
    DEFAULTS = {"duration_s": 1}

    def server_command(self, scenario):
        return "sleep 5"

    def client_command(self, scenario, server_address):
        return f"echo latency=123 server={server_address}"

    def parse(self, output):
        value = float(output.split("latency=")[1].split()[0])
        return [self.measurement("latency_ns", value, "ns", quantile="0.5")]


def _scenario_doc(**overrides):
    doc = {
        **BASE_SCENARIO,
        "id": "cli-local",
        "network_path": "kernel",
        "platform": "baremetal",
        "tools": [{"name": "echo-tool"}],
        "cpu": {"client_cores": [0], "server_cores": [0], "require_isolated": False},
        "repetitions": 1,
    }
    doc.pop("onload", None)
    doc.update(overrides)
    return doc


class CliTestCase(unittest.TestCase):
    def setUp(self):
        self.runner = CliRunner()
        self.tmp = tempfile.TemporaryDirectory()
        self.dir = Path(self.tmp.name)
        self.scenario_file = self.dir / "scenarios.yaml"
        self.scenario_file.write_text(
            yaml.safe_dump({"scenarios": [_scenario_doc()]})
        )

    def tearDown(self):
        self.tmp.cleanup()


class TestValidateScenariosPlan(CliTestCase):
    def test_validate_ok(self):
        result = self.runner.invoke(main, ["validate", str(self.scenario_file)])
        self.assertEqual(result.exit_code, 0, result.output)
        self.assertIn("1 scenario(s) valid", result.output)

    def test_validate_bad_file(self):
        bad = self.dir / "bad.yaml"
        bad.write_text(yaml.safe_dump({"scenarios": [{"id": "x"}]}))
        result = self.runner.invoke(main, ["validate", str(bad)])
        self.assertNotEqual(result.exit_code, 0)

    def test_scenarios_table(self):
        result = self.runner.invoke(main, ["scenarios", str(self.scenario_file)])
        self.assertEqual(result.exit_code, 0, result.output)
        self.assertIn("cli-local", result.output)
        self.assertIn("baremetal", result.output)

    def test_plan(self):
        result = self.runner.invoke(
            main, ["plan", str(self.scenario_file), "--server-address", "10.9.9.9"]
        )
        self.assertEqual(result.exit_code, 0, result.output)
        self.assertIn("server: sleep 5", result.output)
        self.assertIn("client: echo latency=123 server=10.9.9.9", result.output)

    def test_plan_unknown_scenario(self):
        result = self.runner.invoke(
            main, ["plan", str(self.scenario_file), "-s", "nope"]
        )
        self.assertNotEqual(result.exit_code, 0)
        self.assertIn("unknown scenario", result.output)


class TestRunAndReport(CliTestCase):
    def _run(self, *extra):
        db = self.dir / "results.sqlite"
        out = self.dir / "raw"
        args = [
            "run", str(self.scenario_file),
            "--server-address", "127.0.0.1",
            "--db", str(db),
            "--output-dir", str(out),
            "--skip-preflight",
            "--settle", "0",
            *extra,
        ]
        return self.runner.invoke(main, args), db

    def test_run_local_and_report(self):
        result, db = self._run()
        self.assertEqual(result.exit_code, 0, result.output)
        self.assertIn("rep 0: ok", result.output)

        store = ResultStore(db)
        runs = store.list_runs()
        self.assertEqual(len(runs), 1)
        store.close()

        report = self.runner.invoke(
            main, ["report", "--db", str(db), "--quantile", "0.5"]
        )
        self.assertEqual(report.exit_code, 0, report.output)
        self.assertIn("cli-local", report.output)
        self.assertIn("123", report.output)

        report_all = self.runner.invoke(
            main, ["report", "--db", str(db), "--quantile", "all", "--all-runs"]
        )
        self.assertEqual(report_all.exit_code, 0)
        self.assertIn("run_id", report_all.output)

    def test_run_failure_exit_code(self):
        doc = _scenario_doc(id="cli-fail", tools=[
            {"name": "echo-tool", "params": {}}])
        # break the tool by making parse fail: echo without latency=
        f = self.dir / "fail.yaml"

        @register
        class _FailTool(_EchoTool):
            name = "fail-tool"

            def client_command(self, scenario, server_address):
                return "exit 7"

        doc["tools"] = [{"name": "fail-tool"}]
        f.write_text(yaml.safe_dump({"scenarios": [doc]}))
        result = self.runner.invoke(main, [
            "run", str(f), "--server-address", "x", "--db",
            str(self.dir / "f.sqlite"), "--output-dir", str(self.dir / "fraw"),
            "--skip-preflight", "--settle", "0",
        ])
        self.assertEqual(result.exit_code, 1)
        self.assertIn("FAILED tools", result.output)

    def test_run_preflight_error(self):
        # without --skip-preflight, local host will fail isolation/nic checks
        doc = _scenario_doc(id="cli-pf", cpu={
            "client_cores": [0], "server_cores": [0], "require_isolated": True})
        f = self.dir / "pf.yaml"
        f.write_text(yaml.safe_dump({"scenarios": [doc]}))
        result = self.runner.invoke(main, [
            "run", str(f), "--server-address", "x",
            "--db", str(self.dir / "pf.sqlite"),
            "--output-dir", str(self.dir / "pfraw"), "--settle", "0",
        ])
        self.assertEqual(result.exit_code, 1)
        self.assertIn("ERROR", result.output)

    def test_run_with_push(self):
        server = make_server("127.0.0.1", 0, MetricsStore())
        port = server.server_address[1]
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        try:
            result, db = self._run("--push", f"http://127.0.0.1:{port}")
            self.assertEqual(result.exit_code, 0, result.output)
            self.assertIn("perfbench_latency_ns", server.metrics.render())
        finally:
            server.shutdown()
            server.server_close()

    def test_ssh_transport_requires_host(self):
        result = self.runner.invoke(main, [
            "run", str(self.scenario_file), "--server-address", "x",
            "--transport", "ssh",
        ])
        self.assertNotEqual(result.exit_code, 0)

    def test_k8s_transport_requires_pod(self):
        result = self.runner.invoke(main, [
            "run", str(self.scenario_file), "--server-address", "x",
            "--transport", "k8s",
        ])
        self.assertNotEqual(result.exit_code, 0)


class TestK8sRunCommand(CliTestCase):
    def setUp(self):
        super().setUp()
        from perfbench.runner.base import ExecResult, FakeExecutor

        self.fake = FakeExecutor(
            responses={
                "network-status": ExecResult(
                    "", 0,
                    stdout='[{"name": "perfbench/sriov-net", "ips": ["192.168.100.9"]}]',
                ),
                "get pod": ExecResult("", 0, stdout="10.0.0.5"),
                "latency=": ExecResult("", 0, stdout="latency=123 server=x"),
            }
        )
        doc = _scenario_doc(id="k8s-echo", platform="k8s")
        self.k8s_file = self.dir / "k8s.yaml"
        self.k8s_file.write_text(yaml.safe_dump({"scenarios": [doc]}))

    def _invoke(self, *extra):
        import unittest.mock as mock
        from perfbench.runner.k8s import Kubectl

        def factory(namespace, context, kubeconfig):
            return Kubectl(namespace=namespace, context=context,
                           kubeconfig=kubeconfig, executor=self.fake)

        with mock.patch("perfbench.cli._make_kubectl", side_effect=factory):
            return self.runner.invoke(main, [
                "k8s-run", str(self.k8s_file),
                "--image", "bench:1",
                "--db", str(self.dir / "k.sqlite"),
                "--output-dir", str(self.dir / "kraw"),
                "--skip-preflight", "--settle", "0",
                *extra,
            ])

    def test_end_to_end_with_network(self):
        result = self._invoke("--network", "sriov-net",
                              "--client-node", "wa", "--server-node", "wb")
        self.assertEqual(result.exit_code, 0, result.output)
        self.assertIn("address=192.168.100.9", result.output)
        self.assertIn("rep 0: ok", result.output)
        # pods applied, exec'd, deleted
        self.assertTrue(any("apply -f -" in c for c in self.fake.calls))
        self.assertTrue(any("exec pb-k8s-echo-client" in c for c in self.fake.calls))
        self.assertEqual(len([c for c in self.fake.calls if "delete pod" in c]), 2)
        # results persisted
        store = ResultStore(self.dir / "k.sqlite")
        self.assertEqual(len(store.list_runs()), 1)
        store.close()

    def test_keep_pods(self):
        result = self._invoke("--keep-pods")
        self.assertEqual(result.exit_code, 0, result.output)
        self.assertFalse(any("delete pod" in c for c in self.fake.calls))

    def test_non_k8s_scenarios_rejected(self):
        import unittest.mock as mock

        with mock.patch("perfbench.cli._make_kubectl"):
            result = self.runner.invoke(main, [
                "k8s-run", str(self.scenario_file), "--image", "bench:1",
            ])
        self.assertNotEqual(result.exit_code, 0)
        self.assertIn("no scenarios with platform: k8s", result.output)

    def test_provision_failure_cleans_up_and_fails(self):
        from perfbench.runner.base import ExecResult

        self.fake.responses["--for=condition=Ready"] = ExecResult("", 1, stderr="timed out")
        result = self._invoke()
        self.assertEqual(result.exit_code, 1)
        self.assertIn("ERROR", result.output)

    def test_node_preflight_runs_when_not_skipped(self):
        from perfbench.runner.base import ExecResult

        # irqbalance "found" on the node -> fatal -> scenario aborts before pods
        self.fake.responses["pgrep -x irqbalance"] = ExecResult("", 0, stdout="812")
        self.fake.responses["tuned-adm active"] = ExecResult("", 0, stdout="balanced")
        self.fake.responses["/proc/interrupts"] = ExecResult("", 0, stdout="")
        import unittest.mock as mock
        from perfbench.runner.k8s import Kubectl

        def factory(namespace, context, kubeconfig):
            return Kubectl(namespace=namespace, executor=self.fake)

        with mock.patch("perfbench.cli._make_kubectl", side_effect=factory):
            result = self.runner.invoke(main, [
                "k8s-run", str(self.k8s_file), "--image", "bench:1",
                "--client-node", "wa", "--server-node", "wb",
                "--db", str(self.dir / "n.sqlite"),
                "--output-dir", str(self.dir / "nraw"), "--settle", "0",
            ])
        self.assertEqual(result.exit_code, 1, result.output)
        self.assertIn("node_irqbalance", result.output)
        self.assertIn("fatal node-level preflight", result.output)
        # benchmark pods never provisioned
        self.assertFalse(any("exec pb-k8s-echo-client" in c for c in self.fake.calls))


class TestPreflightCommand(CliTestCase):
    def test_preflight_local_fails_fatally(self):
        # local sandbox: cores not isolated, ethtool missing -> fatal -> exit 1
        doc = _scenario_doc(id="pf", cpu={
            "client_cores": [0], "server_cores": [0], "require_isolated": True})
        f = self.dir / "pf.yaml"
        f.write_text(yaml.safe_dump({"scenarios": [doc]}))
        result = self.runner.invoke(main, ["preflight", str(f), "-s", "pf"])
        self.assertEqual(result.exit_code, 1, result.output)
        self.assertIn("FATAL", result.output)
        self.assertIn("PASS", result.output)


class TestNicsCommand(CliTestCase):
    def test_local_discovery_runs(self):
        # sandbox may expose no PCI NICs; command must still succeed
        result = self.runner.invoke(main, ["nics", "--transport", "local"])
        self.assertEqual(result.exit_code, 0, result.output)
        self.assertIn("# local", result.output)

    def test_requires_host_for_ssh(self):
        result = self.runner.invoke(main, ["nics", "--transport", "ssh"])
        self.assertNotEqual(result.exit_code, 0)


class TestPushCommand(CliTestCase):
    def test_push_all_and_single(self):
        result, db = TestRunAndReport._run(self, )  # reuse run helper
        self.assertEqual(result.exit_code, 0)
        server = make_server("127.0.0.1", 0, MetricsStore())
        port = server.server_address[1]
        threading.Thread(target=server.serve_forever, daemon=True).start()
        try:
            url = f"http://127.0.0.1:{port}"
            pushed = self.runner.invoke(main, ["push", "--db", str(db), "--url", url])
            self.assertEqual(pushed.exit_code, 0, pushed.output)
            self.assertIn("pushed 1 run(s)", pushed.output)

            store = ResultStore(db)
            run_id = store.list_runs()[0]["run_id"]
            store.close()
            single = self.runner.invoke(
                main, ["push", "--db", str(db), "--url", url, "--run-id", run_id]
            )
            self.assertEqual(single.exit_code, 0)
            missing = self.runner.invoke(
                main, ["push", "--db", str(db), "--url", url, "--run-id", "nope"]
            )
            self.assertNotEqual(missing.exit_code, 0)
        finally:
            server.shutdown()
            server.server_close()


if __name__ == "__main__":
    unittest.main()
