import unittest

from perfbench.report.summary import comparison_rows, format_table
from perfbench.results.store import ResultStore
from tests.test_models_store_stats import _record


class TestComparisonRows(unittest.TestCase):
    def _store(self):
        store = ResultStore()
        store.save_run(_record(run_id="r1", scenario_id="s1", value=100,
                               started="2026-01-01T00:00:00"))
        store.save_run(_record(run_id="r2", scenario_id="s1", value=110,
                               started="2026-01-02T00:00:00"))
        store.save_run(_record(run_id="r3", scenario_id="s2", value=300,
                               started="2026-01-01T00:00:00"))
        return store

    def test_latest_only(self):
        rows = comparison_rows(self._store(), metric="latency_ns", quantile="0.99")
        self.assertEqual([(r["scenario"], r["value"]) for r in rows],
                         [("s1", 110.0), ("s2", 300.0)])

    def test_all_runs(self):
        rows = comparison_rows(
            self._store(), metric="latency_ns", quantile="0.99", latest_only=False
        )
        self.assertEqual(len(rows), 3)
        self.assertTrue(all("run_id" in r for r in rows))

    def test_quantile_none_includes_all(self):
        rows = comparison_rows(self._store(), metric="latency_ns", quantile=None)
        self.assertEqual(len(rows), 2)

    def test_no_matches(self):
        rows = comparison_rows(self._store(), metric="nonexistent_metric")
        self.assertEqual(rows, [])


class TestFormatTable(unittest.TestCase):
    def test_empty(self):
        self.assertEqual(format_table([], ["a"]), "(no results)")

    def test_alignment_and_formatting(self):
        rows = [
            {"scenario": "s1", "value": 2461.0, "unit": "ns"},
            {"scenario": "long-scenario-name", "value": 0.5, "unit": "ns"},
        ]
        text = format_table(rows, ["scenario", "value", "unit"])
        lines = text.splitlines()
        self.assertEqual(len(lines), 4)  # header, rule, 2 rows
        self.assertIn("2,461", text)
        self.assertIn("0.50", text)
        # columns aligned
        self.assertEqual(lines[0].index("value"), lines[2].index("2,461"))

    def test_msg_size_sort(self):
        from perfbench.report.summary import _msg_key

        self.assertLess(_msg_key("64"), _msg_key("1024"))
        self.assertLess(_msg_key("1024"), _msg_key("-"))


if __name__ == "__main__":
    unittest.main()
