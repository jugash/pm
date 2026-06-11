"""SQLite-backed result store.

Full-fidelity run records are stored as JSON; measurements are additionally
flattened into a queryable table for reporting.
"""

from __future__ import annotations

import json
import sqlite3
from pathlib import Path
from typing import Any, Optional

from perfbench.results.models import RunRecord

_SCHEMA = """
CREATE TABLE IF NOT EXISTS runs (
    run_id      TEXT PRIMARY KEY,
    scenario_id TEXT NOT NULL,
    rep         INTEGER NOT NULL,
    started_at  TEXT NOT NULL,
    finished_at TEXT NOT NULL,
    record      TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS measurements (
    run_id      TEXT NOT NULL REFERENCES runs(run_id) ON DELETE CASCADE,
    scenario_id TEXT NOT NULL,
    tool        TEXT NOT NULL,
    metric      TEXT NOT NULL,
    value       REAL NOT NULL,
    unit        TEXT NOT NULL,
    labels      TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_meas_metric ON measurements(metric, scenario_id);
CREATE INDEX IF NOT EXISTS idx_runs_scenario ON runs(scenario_id, started_at);
"""


class ResultStore:
    """Persists :class:`RunRecord` objects to SQLite."""

    def __init__(self, path: str | Path = ":memory:"):
        self.path = str(path)
        self._conn = sqlite3.connect(self.path)
        self._conn.execute("PRAGMA foreign_keys = ON")
        self._conn.executescript(_SCHEMA)

    # -- write ------------------------------------------------------------

    def save_run(self, record: RunRecord) -> None:
        with self._conn:
            self._conn.execute("DELETE FROM measurements WHERE run_id = ?", (record.run_id,))
            self._conn.execute(
                "INSERT OR REPLACE INTO runs VALUES (?, ?, ?, ?, ?, ?)",
                (
                    record.run_id,
                    record.scenario_id,
                    record.rep,
                    record.started_at,
                    record.finished_at,
                    record.to_json(),
                ),
            )
            rows = [
                (
                    record.run_id,
                    record.scenario_id,
                    m.tool,
                    m.metric,
                    m.value,
                    m.unit,
                    json.dumps(m.labels, sort_keys=True),
                )
                for m in record.all_measurements()
            ]
            self._conn.executemany(
                "INSERT INTO measurements VALUES (?, ?, ?, ?, ?, ?, ?)", rows
            )

    # -- read -------------------------------------------------------------

    def get_run(self, run_id: str) -> Optional[RunRecord]:
        row = self._conn.execute(
            "SELECT record FROM runs WHERE run_id = ?", (run_id,)
        ).fetchone()
        return RunRecord.from_json(row[0]) if row else None

    def list_runs(self, scenario_id: Optional[str] = None) -> list[dict[str, Any]]:
        sql = "SELECT run_id, scenario_id, rep, started_at, finished_at FROM runs"
        args: tuple = ()
        if scenario_id:
            sql += " WHERE scenario_id = ?"
            args = (scenario_id,)
        sql += " ORDER BY started_at, run_id"
        cols = ("run_id", "scenario_id", "rep", "started_at", "finished_at")
        return [dict(zip(cols, r)) for r in self._conn.execute(sql, args)]

    def latest_run_ids(self) -> dict[str, str]:
        """Latest run id per scenario (by started_at, then run_id).

        Uses a window function: a tuple-IN against independent MAX()
        aggregates would silently drop scenarios whenever the newest run
        doesn't also carry the lexically-largest run id (clock skew).
        """
        rows = self._conn.execute(
            """
            SELECT scenario_id, run_id FROM (
                SELECT scenario_id, run_id,
                       ROW_NUMBER() OVER (
                           PARTITION BY scenario_id
                           ORDER BY started_at DESC, run_id DESC
                       ) AS rn
                FROM runs
            ) WHERE rn = 1
            """
        ).fetchall()
        return {scenario: run_id for scenario, run_id in rows}

    def measurements(
        self,
        metric: Optional[str] = None,
        scenario_id: Optional[str] = None,
        run_id: Optional[str] = None,
    ) -> list[dict[str, Any]]:
        sql = "SELECT run_id, scenario_id, tool, metric, value, unit, labels FROM measurements"
        clauses, args = [], []
        for clause, value in (
            ("metric = ?", metric),
            ("scenario_id = ?", scenario_id),
            ("run_id = ?", run_id),
        ):
            if value is not None:
                clauses.append(clause)
                args.append(value)
        if clauses:
            sql += " WHERE " + " AND ".join(clauses)
        out = []
        for row in self._conn.execute(sql, args):
            out.append(
                {
                    "run_id": row[0],
                    "scenario_id": row[1],
                    "tool": row[2],
                    "metric": row[3],
                    "value": row[4],
                    "unit": row[5],
                    "labels": json.loads(row[6]),
                }
            )
        return out

    def export_json(self, run_id: str, path: str | Path) -> Path:
        record = self.get_run(run_id)
        if record is None:
            raise KeyError(f"unknown run_id: {run_id}")
        out = Path(path)
        out.write_text(record.to_json())
        return out

    def close(self) -> None:
        self._conn.close()

    def __enter__(self) -> "ResultStore":
        return self

    def __exit__(self, *exc) -> None:
        self.close()
