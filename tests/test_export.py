import http.client
import json
import tempfile
import threading
import unittest
from pathlib import Path

from perfbench.errors import ExecutionError
from perfbench.export.exporter import MetricsStore, make_server
from perfbench.export.promtext import (
    GaugeFamily,
    escape_label_value,
    format_value,
    render_families,
    sanitize_label_name,
    sanitize_metric_name,
)
from perfbench.export.push import push_run
from tests.test_models_store_stats import _record


class TestPromText(unittest.TestCase):
    def test_sanitize(self):
        self.assertEqual(sanitize_metric_name("latency p99(ns)"), "latency_p99_ns_")
        self.assertEqual(sanitize_metric_name("9lives"), "_9lives")
        self.assertEqual(sanitize_label_name("msg-size"), "msg_size")
        self.assertEqual(sanitize_label_name("0core"), "_0core")

    def test_escape(self):
        self.assertEqual(escape_label_value('a"b\\c\nd'), 'a\\"b\\\\c\\nd')

    def test_format_value(self):
        self.assertEqual(format_value(2461.0), "2461")
        self.assertEqual(format_value(0.999), "0.999")
        self.assertEqual(format_value(float("nan")), "NaN")
        self.assertEqual(format_value(float("inf")), "+Inf")
        self.assertEqual(format_value(float("-inf")), "-Inf")

    def test_gauge_family_render(self):
        fam = GaugeFamily("perfbench_latency_ns", "Latency.")
        fam.set({"scenario": "a", "quantile": "0.99"}, 2461)
        fam.set({"scenario": "a", "quantile": "0.5"}, 2100)
        fam.set({}, 1.5)
        text = fam.render()
        self.assertIn("# TYPE perfbench_latency_ns gauge", text)
        self.assertIn('perfbench_latency_ns{quantile="0.5",scenario="a"} 2100', text)
        self.assertIn("perfbench_latency_ns 1.5", text)

    def test_last_write_wins(self):
        fam = GaugeFamily("g", "h")
        fam.set({"a": "1"}, 10)
        fam.set({"a": "1"}, 20)
        self.assertEqual(len(fam.samples), 1)
        self.assertIn("20", fam.render())

    def test_empty_label_values_dropped(self):
        fam = GaugeFamily("g", "h")
        fam.set({"a": "1", "b": ""}, 5)
        self.assertIn('g{a="1"} 5', fam.render())

    def test_render_families_sorted(self):
        fams = {
            "b_metric": GaugeFamily("b_metric", "B."),
            "a_metric": GaugeFamily("a_metric", "A."),
        }
        fams["a_metric"].set({}, 1)
        fams["b_metric"].set({}, 2)
        text = render_families(fams)
        self.assertLess(text.index("a_metric"), text.index("b_metric"))
        self.assertEqual(render_families({}), "\n")


class TestMetricsStore(unittest.TestCase):
    def test_ingest_and_render(self):
        store = MetricsStore()
        store.ingest(_record().to_dict())
        text = store.render()
        self.assertIn("perfbench_latency_ns{", text)
        self.assertIn('quantile="0.99"', text)
        self.assertIn('scenario="s1"', text)
        self.assertIn('tool="sockperf"', text)
        self.assertIn("perfbench_run_timestamp_seconds{", text)
        self.assertIn('perfbench_run_info{', text)
        self.assertIn('kernel="5.15.0"', text)
        self.assertIn("perfbench_tool_ok{", text)
        self.assertEqual(store.run_count(), 1)

    def test_latest_per_scenario_rep(self):
        store = MetricsStore()
        store.ingest(_record(run_id="r1", value=100, started="2026-01-02T00:00:00").to_dict())
        store.ingest(_record(run_id="r0", value=999, started="2026-01-01T00:00:00").to_dict())
        text = store.render()
        self.assertIn(" 100", text)
        self.assertNotIn(" 999", text)
        self.assertEqual(store.run_count(), 1)

    def test_reps_kept_separately(self):
        store = MetricsStore()
        store.ingest(_record(run_id="r1", rep=0).to_dict())
        store.ingest(_record(run_id="r2", rep=1).to_dict())
        self.assertEqual(store.run_count(), 2)
        self.assertIn('rep="1"', store.render())

    def test_state_file_persistence(self):
        with tempfile.TemporaryDirectory() as tmp:
            state = Path(tmp) / "state.jsonl"
            store = MetricsStore(state_file=state)
            store.ingest(_record().to_dict())
            # corrupt tail line must be tolerated
            with state.open("a") as fh:
                fh.write("{not json\n")
            reloaded = MetricsStore(state_file=state)
            self.assertEqual(reloaded.run_count(), 1)
            self.assertIn('scenario="s1"', reloaded.render())

    def test_failed_tool_run_metric(self):
        record = _record().to_dict()
        record["tool_runs"][0]["exit_code"] = 1
        store = MetricsStore()
        store.ingest(record)
        self.assertIn("} 0", [l for l in store.render().splitlines()
                              if l.startswith("perfbench_tool_ok")][0])


class TestExporterHTTP(unittest.TestCase):
    def setUp(self):
        self.server = make_server("127.0.0.1", 0, MetricsStore())
        self.port = self.server.server_address[1]
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        self.thread.start()

    def tearDown(self):
        self.server.shutdown()
        self.server.server_close()

    def _request(self, method, path, body=None):
        conn = http.client.HTTPConnection("127.0.0.1", self.port, timeout=5)
        headers = {"Content-Type": "application/json"} if body else {}
        conn.request(method, path, body=body, headers=headers)
        response = conn.getresponse()
        data = response.read().decode()
        conn.close()
        return response.status, data

    def test_healthz(self):
        status, body = self._request("GET", "/healthz")
        self.assertEqual(status, 200)
        self.assertEqual(json.loads(body)["status"], "ok")

    def test_ingest_then_metrics(self):
        status, _ = self._request(
            "POST", "/api/v1/ingest", json.dumps(_record().to_dict())
        )
        self.assertEqual(status, 200)
        status, body = self._request("GET", "/metrics")
        self.assertEqual(status, 200)
        self.assertIn("perfbench_latency_ns{", body)
        self.assertIn('scenario="s1"', body)

    def test_ingest_invalid_json(self):
        status, body = self._request("POST", "/api/v1/ingest", "{broken")
        self.assertEqual(status, 400)

    def test_ingest_missing_fields(self):
        status, _ = self._request("POST", "/api/v1/ingest", json.dumps({"rep": 1}))
        self.assertEqual(status, 400)

    def test_not_found(self):
        self.assertEqual(self._request("GET", "/nope")[0], 404)
        self.assertEqual(self._request("POST", "/nope")[0], 404)

    def test_push_client(self):
        result = push_run(f"http://127.0.0.1:{self.port}", _record().to_dict())
        self.assertEqual(result["status"], "ok")

    def test_push_client_http_error(self):
        with self.assertRaises(ExecutionError) as ctx:
            push_run(f"http://127.0.0.1:{self.port}", {"rep": "bad"})
        self.assertIn("HTTP 400", str(ctx.exception))

    def test_push_client_unreachable(self):
        with self.assertRaises(ExecutionError) as ctx:
            push_run("http://127.0.0.1:1", _record().to_dict(), timeout=0.5)
        self.assertIn("cannot reach", str(ctx.exception))


if __name__ == "__main__":
    unittest.main()
