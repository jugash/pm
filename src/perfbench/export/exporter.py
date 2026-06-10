"""Prometheus exporter with an HTTP ingest API.

Designed for the pull-model pipeline: benchmark runs are batch jobs, so the
runner POSTs finished run records to this long-lived exporter
(``POST /api/v1/ingest``), which serves them on ``GET /metrics`` for Grafana
Alloy / Prometheus to scrape (Service + ServiceMonitor manifests are in
``deploy/k8s``).

Behavior:

* keeps the latest run per (scenario, rep); historical values live in your
  TSDB once scraped — the exporter is not a database,
* optional JSONL state file so a restart does not blank the metrics,
* exposes fixed quantiles as gauges (``quantile`` label) rather than native
  histograms to keep series cardinality predictable.
"""

from __future__ import annotations

import json
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any, Optional

from perfbench import __version__
from perfbench.export.promtext import (
    CONTENT_TYPE,
    GaugeFamily,
    render_families,
    sanitize_metric_name,
)

_HELP = {
    "perfbench_latency_ns": "Round-trip latency distribution (quantile label; 0=min, 1=max).",
    "perfbench_latency_mean_ns": "Mean round-trip latency.",
    "perfbench_run_timestamp_seconds": "Unix time the run finished.",
    "perfbench_run_info": "Run environment info (value is always 1).",
    "perfbench_tool_ok": "1 if the tool run succeeded, 0 otherwise.",
}

_INFO_ENV_KEYS = ("kernel", "onload_version", "tuned_profile", "cmdline")


def _epoch(iso_text: str) -> float:
    from datetime import datetime

    try:
        return datetime.fromisoformat(iso_text).timestamp()
    except (ValueError, TypeError):
        return 0.0


class MetricsStore:
    """Thread-safe store of the latest run records, rendered on demand."""

    def __init__(self, state_file: Optional[str | Path] = None):
        self._lock = threading.Lock()
        self._runs: dict[tuple[str, int], dict[str, Any]] = {}
        self._state_file = Path(state_file) if state_file else None
        if self._state_file and self._state_file.exists():
            self._load_state()

    def _load_state(self) -> None:
        for line in self._state_file.read_text().splitlines():
            line = line.strip()
            if not line:
                continue
            try:
                self._ingest(json.loads(line), persist=False)
            except (json.JSONDecodeError, KeyError):
                continue  # tolerate a corrupt tail line

    def ingest(self, record: dict[str, Any]) -> None:
        self._ingest(record, persist=True)

    def _ingest(self, record: dict[str, Any], persist: bool) -> None:
        scenario = record["scenario_id"]  # KeyError -> 400 at the HTTP layer
        rep = int(record.get("rep", 0))
        with self._lock:
            key = (scenario, rep)
            existing = self._runs.get(key)
            if existing and existing.get("started_at", "") > record.get("started_at", ""):
                return  # ignore stale replays
            self._runs[key] = record
            if persist and self._state_file:
                with self._state_file.open("a") as fh:
                    fh.write(json.dumps(record) + "\n")

    def run_count(self) -> int:
        with self._lock:
            return len(self._runs)

    def render(self) -> str:
        families: dict[str, GaugeFamily] = {}

        def family(name: str) -> GaugeFamily:
            if name not in families:
                families[name] = GaugeFamily(name, _HELP.get(name, f"PerfBench metric {name}."))
            return families[name]

        with self._lock:
            runs = list(self._runs.values())

        for record in runs:
            base = dict(record.get("labels", {}))
            base["rep"] = str(record.get("rep", 0))

            family("perfbench_run_timestamp_seconds").set(
                base, _epoch(record.get("finished_at", ""))
            )

            info_labels = dict(base)
            client_env = record.get("env", {}).get("client", {})
            for key in _INFO_ENV_KEYS:
                value = str(client_env.get(key, ""))[:120]
                if value and not value.startswith("<error"):
                    info_labels[key] = value
            family("perfbench_run_info").set(info_labels, 1)

            for tool_run in record.get("tool_runs", []):
                tool_labels = dict(base)
                tool_labels["tool"] = tool_run.get("tool", "")
                family("perfbench_tool_ok").set(
                    tool_labels,
                    1 if tool_run.get("exit_code", 1) == 0 and not tool_run.get("error") else 0,
                )
                for m in tool_run.get("measurements", []):
                    name = sanitize_metric_name(f"perfbench_{m['metric']}")
                    labels = dict(tool_labels)
                    labels.update({k: str(v) for k, v in m.get("labels", {}).items()})
                    family(name).set(labels, float(m["value"]))

        return render_families(families)


class _Handler(BaseHTTPRequestHandler):
    server_version = f"perfbench-exporter/{__version__}"

    @property
    def metrics(self) -> MetricsStore:
        return self.server.metrics  # type: ignore[attr-defined]

    def _respond(self, code: int, body: str, content_type: str = "application/json") -> None:
        data = body.encode()
        self.send_response(code)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)

    def do_GET(self) -> None:  # noqa: N802
        if self.path == "/metrics":
            self._respond(200, self.metrics.render(), CONTENT_TYPE)
        elif self.path == "/healthz":
            self._respond(200, json.dumps({"status": "ok", "runs": self.metrics.run_count()}))
        else:
            self._respond(404, json.dumps({"error": "not found"}))

    def do_POST(self) -> None:  # noqa: N802
        if self.path != "/api/v1/ingest":
            self._respond(404, json.dumps({"error": "not found"}))
            return
        try:
            length = int(self.headers.get("Content-Length", 0))
            record = json.loads(self.rfile.read(length))
            self.metrics.ingest(record)
        except (json.JSONDecodeError, KeyError, ValueError) as exc:
            self._respond(400, json.dumps({"error": str(exc)}))
            return
        self._respond(200, json.dumps({"status": "ok"}))

    def log_message(self, fmt, *args) -> None:  # silence per-request stderr noise
        return


class ExporterServer(ThreadingHTTPServer):
    daemon_threads = True

    def __init__(self, address, metrics: MetricsStore):
        super().__init__(address, _Handler)
        self.metrics = metrics


def make_server(host: str, port: int, metrics: MetricsStore) -> ExporterServer:
    return ExporterServer((host, port), metrics)


def serve(host: str, port: int, state_file: Optional[str] = None) -> None:  # pragma: no cover
    server = make_server(host, port, MetricsStore(state_file))
    print(f"perfbench exporter listening on {host}:{port}")
    server.serve_forever()
