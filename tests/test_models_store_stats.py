import tempfile
import unittest
from pathlib import Path

from perfbench.results.models import Measurement, RunRecord, ToolRun, new_run_id
from perfbench.results.store import ResultStore
from perfbench.stats import (
    Aggregate,
    aggregate,
    by_metric,
    derive_jitter,
    percentile,
    summarize,
)


def _record(run_id="r1", scenario_id="s1", rep=0, value=100.0, started="2026-01-01T00:00:00"):
    return RunRecord(
        run_id=run_id,
        scenario_id=scenario_id,
        rep=rep,
        labels={"scenario": scenario_id, "platform": "baremetal"},
        started_at=started,
        finished_at=started.replace("00:00:00", "00:05:00"),
        env={"client": {"kernel": "5.15.0"}},
        preflight=[{"name": "irqbalance", "passed": True}],
        tool_runs=[
            ToolRun(
                tool="sockperf",
                client_command="sockperf ping-pong",
                server_command="sockperf server",
                measurements=[
                    Measurement("sockperf", "latency_ns", value, "ns",
                                {"quantile": "0.99", "msg_size": "64"}),
                    Measurement("sockperf", "latency_mean_ns", value / 2, "ns",
                                {"msg_size": "64"}),
                ],
            )
        ],
    )


class TestModels(unittest.TestCase):
    def test_run_id_unique_and_sortable(self):
        a, b = new_run_id(), new_run_id()
        self.assertNotEqual(a, b)
        self.assertRegex(a, r"^\d{8}T\d{6}-[0-9a-f]{8}$")

    def test_json_round_trip(self):
        record = _record()
        clone = RunRecord.from_json(record.to_json())
        self.assertEqual(clone.to_dict(), record.to_dict())
        self.assertEqual(len(clone.all_measurements()), 2)

    def test_tool_run_ok(self):
        self.assertTrue(ToolRun(tool="x", client_command="c").ok)
        self.assertFalse(ToolRun(tool="x", client_command="c", exit_code=1).ok)
        self.assertFalse(ToolRun(tool="x", client_command="c", error="parse").ok)


class TestStore(unittest.TestCase):
    def test_save_and_get(self):
        store = ResultStore()
        record = _record()
        store.save_run(record)
        loaded = store.get_run("r1")
        self.assertEqual(loaded.to_dict(), record.to_dict())
        self.assertIsNone(store.get_run("missing"))

    def test_save_is_idempotent(self):
        store = ResultStore()
        store.save_run(_record())
        store.save_run(_record(value=200.0))
        self.assertEqual(len(store.list_runs()), 1)
        meas = store.measurements(metric="latency_ns")
        self.assertEqual(len(meas), 1)
        self.assertEqual(meas[0]["value"], 200.0)

    def test_list_and_filter(self):
        store = ResultStore()
        store.save_run(_record(run_id="r1", scenario_id="s1"))
        store.save_run(_record(run_id="r2", scenario_id="s2"))
        self.assertEqual(len(store.list_runs()), 2)
        self.assertEqual(store.list_runs("s2")[0]["run_id"], "r2")
        rows = store.measurements(metric="latency_ns", scenario_id="s2", run_id="r2")
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]["labels"]["quantile"], "0.99")

    def test_latest_run_ids(self):
        store = ResultStore()
        store.save_run(_record(run_id="r1", scenario_id="s1", started="2026-01-01T00:00:00"))
        store.save_run(_record(run_id="r2", scenario_id="s1", started="2026-01-02T00:00:00"))
        self.assertEqual(store.latest_run_ids(), {"s1": "r2"})

    def test_latest_run_ids_with_clock_skew(self):
        # newest started_at carries a lexically SMALLER run_id: the naive
        # tuple-IN-of-MAXes query silently returned nothing here
        store = ResultStore()
        store.save_run(_record(run_id="zz-old", scenario_id="s1",
                               started="2026-01-01T00:00:00"))
        store.save_run(_record(run_id="aa-new", scenario_id="s1",
                               started="2026-01-02T00:00:00"))
        self.assertEqual(store.latest_run_ids(), {"s1": "aa-new"})

    def test_export_json_and_file_store(self):
        with tempfile.TemporaryDirectory() as tmp:
            db = Path(tmp) / "x.sqlite"
            with ResultStore(db) as store:
                store.save_run(_record())
                out = store.export_json("r1", Path(tmp) / "r1.json")
                self.assertIn('"run_id": "r1"', out.read_text())
                with self.assertRaises(KeyError):
                    store.export_json("nope", Path(tmp) / "no.json")
            # reopen: data persisted
            with ResultStore(db) as store:
                self.assertEqual(len(store.list_runs()), 1)


class TestStats(unittest.TestCase):
    def test_percentile(self):
        data = [1, 2, 3, 4, 5, 6, 7, 8, 9, 10]
        self.assertEqual(percentile(data, 0), 1)
        self.assertEqual(percentile(data, 100), 10)
        self.assertAlmostEqual(percentile(data, 50), 5.5)
        self.assertEqual(percentile([7], 99), 7)

    def test_percentile_errors(self):
        with self.assertRaises(ValueError):
            percentile([], 50)
        with self.assertRaises(ValueError):
            percentile([1], 101)

    def test_summarize(self):
        s = summarize([1.0, 2.0, 3.0, 4.0])
        self.assertEqual(s["count"], 4)
        self.assertEqual(s["min"], 1)
        self.assertEqual(s["max"], 4)
        self.assertAlmostEqual(s["mean"], 2.5)
        self.assertGreater(s["stddev"], 0)
        with self.assertRaises(ValueError):
            summarize([])

    def test_aggregate_groups_across_reps(self):
        m1 = Measurement("t", "latency_ns", 100, "ns", {"quantile": "0.99"})
        m2 = Measurement("t", "latency_ns", 120, "ns", {"quantile": "0.99"})
        m3 = Measurement("t", "latency_ns", 50, "ns", {"quantile": "0.5"})
        aggs = aggregate([m1, m2, m3])
        self.assertEqual(len(aggs), 2)
        first = aggs[0]
        self.assertEqual(first.values, [100, 120])
        self.assertEqual(first.median, 110)
        self.assertEqual(first.best, 100)
        self.assertEqual(first.worst, 120)
        self.assertAlmostEqual(first.spread_pct, 100 * 20 / 110)

    def test_spread_pct_zero_median(self):
        agg = Aggregate("t", "m", "ns", {}, values=[0.0, 0.0])
        self.assertEqual(agg.spread_pct, 0.0)

    def test_derive_jitter_prefers_p999(self):
        ms = [
            Measurement("t", "latency_ns", 2000, "ns", {"quantile": "0.5", "msg_size": "64"}),
            Measurement("t", "latency_ns", 5000, "ns", {"quantile": "0.99", "msg_size": "64"}),
            Measurement("t", "latency_ns", 8000, "ns", {"quantile": "0.999", "msg_size": "64"}),
        ]
        derived = derive_jitter(ms)
        self.assertEqual(len(derived), 1)
        j = derived[0]
        self.assertEqual(j.metric, "latency_jitter_ns")
        self.assertEqual(j.value, 6000.0)
        self.assertEqual(j.labels["tail_quantile"], "0.999")
        self.assertEqual(j.labels["msg_size"], "64")

    def test_derive_jitter_falls_back_to_p99(self):
        ms = [
            Measurement("t", "latency_ns", 2000, "ns", {"quantile": "0.5"}),
            Measurement("t", "latency_ns", 5000, "ns", {"quantile": "0.99"}),
        ]
        derived = derive_jitter(ms)
        self.assertEqual(derived[0].value, 3000.0)
        self.assertEqual(derived[0].labels["tail_quantile"], "0.99")

    def test_derive_jitter_groups_separately(self):
        ms = [
            Measurement("t", "latency_ns", 2000, "ns", {"quantile": "0.5", "msg_size": "64"}),
            Measurement("t", "latency_ns", 3000, "ns", {"quantile": "0.999", "msg_size": "64"}),
            Measurement("t", "latency_ns", 2500, "ns", {"quantile": "0.5", "msg_size": "256"}),
            Measurement("t", "latency_ns", 9000, "ns", {"quantile": "0.999", "msg_size": "256"}),
        ]
        derived = derive_jitter(ms)
        self.assertEqual(len(derived), 2)

    def test_derive_jitter_skips_incomplete_and_clamps(self):
        # no median -> nothing derived
        self.assertEqual(
            derive_jitter([Measurement("t", "latency_ns", 5, "ns", {"quantile": "0.99"})]), []
        )
        # non-ns metrics and unquantiled measurements ignored
        self.assertEqual(
            derive_jitter([Measurement("t", "throughput_bps", 5, "bps", {})]), []
        )
        # tail below median clamps to 0
        ms = [
            Measurement("t", "latency_ns", 100, "ns", {"quantile": "0.5"}),
            Measurement("t", "latency_ns", 90, "ns", {"quantile": "0.999"}),
        ]
        self.assertEqual(derive_jitter(ms)[0].value, 0.0)

    def test_by_metric(self):
        aggs = aggregate(
            [
                Measurement("t", "a", 1, "ns", {}),
                Measurement("t", "b", 2, "ns", {}),
                Measurement("t", "a", 3, "ns", {"x": "1"}),
            ]
        )
        grouped = by_metric(aggs)
        self.assertEqual(set(grouped), {"a", "b"})
        self.assertEqual(len(grouped["a"]), 2)


if __name__ == "__main__":
    unittest.main()
