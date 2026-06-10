"""Normalized result data model.

Conventions
-----------
* Latency distributions use metric ``latency_ns`` with a ``quantile`` label
  ("0" = min, "0.5" = median, "0.99", "0.999", ... , "1" = max). Means and
  stddevs are separate metrics (``latency_mean_ns``...), matching Prometheus
  summary conventions and keeping PromQL slicing trivial.
* All time values are normalized to nanoseconds at parse time.
"""

from __future__ import annotations

import json
import secrets
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any, Optional


def new_run_id() -> str:
    """Sortable, collision-resistant run identifier."""
    stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%S")
    return f"{stamp}-{secrets.token_hex(4)}"


def utc_now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


@dataclass
class Measurement:
    """A single scalar produced by a benchmark tool."""

    tool: str
    metric: str  # e.g. latency_ns, throughput_bps, interruptions_per_sec
    value: float
    unit: str  # ns | bps | count | 1/s | percent
    labels: dict[str, str] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        return {
            "tool": self.tool,
            "metric": self.metric,
            "value": self.value,
            "unit": self.unit,
            "labels": dict(self.labels),
        }

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> "Measurement":
        return cls(
            tool=data["tool"],
            metric=data["metric"],
            value=float(data["value"]),
            unit=data["unit"],
            labels=dict(data.get("labels", {})),
        )


@dataclass
class ToolRun:
    """One tool invocation inside a run (commands, status, measurements)."""

    tool: str
    client_command: str
    server_command: Optional[str] = None
    exit_code: int = 0
    duration_s: float = 0.0
    error: str = ""
    measurements: list[Measurement] = field(default_factory=list)

    @property
    def ok(self) -> bool:
        return self.exit_code == 0 and not self.error

    def to_dict(self) -> dict[str, Any]:
        return {
            "tool": self.tool,
            "client_command": self.client_command,
            "server_command": self.server_command,
            "exit_code": self.exit_code,
            "duration_s": self.duration_s,
            "error": self.error,
            "measurements": [m.to_dict() for m in self.measurements],
        }

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> "ToolRun":
        return cls(
            tool=data["tool"],
            client_command=data["client_command"],
            server_command=data.get("server_command"),
            exit_code=int(data.get("exit_code", 0)),
            duration_s=float(data.get("duration_s", 0.0)),
            error=data.get("error", ""),
            measurements=[Measurement.from_dict(m) for m in data.get("measurements", [])],
        )


@dataclass
class RunRecord:
    """One repetition of one scenario, with full environment context."""

    run_id: str
    scenario_id: str
    rep: int
    labels: dict[str, str] = field(default_factory=dict)
    started_at: str = field(default_factory=utc_now_iso)
    finished_at: str = ""
    env: dict[str, Any] = field(default_factory=dict)
    preflight: list[dict[str, Any]] = field(default_factory=list)
    tool_runs: list[ToolRun] = field(default_factory=list)

    def all_measurements(self) -> list[Measurement]:
        out: list[Measurement] = []
        for tr in self.tool_runs:
            out.extend(tr.measurements)
        return out

    def to_dict(self) -> dict[str, Any]:
        return {
            "run_id": self.run_id,
            "scenario_id": self.scenario_id,
            "rep": self.rep,
            "labels": dict(self.labels),
            "started_at": self.started_at,
            "finished_at": self.finished_at,
            "env": self.env,
            "preflight": self.preflight,
            "tool_runs": [t.to_dict() for t in self.tool_runs],
        }

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> "RunRecord":
        return cls(
            run_id=data["run_id"],
            scenario_id=data["scenario_id"],
            rep=int(data.get("rep", 0)),
            labels=dict(data.get("labels", {})),
            started_at=data.get("started_at", ""),
            finished_at=data.get("finished_at", ""),
            env=dict(data.get("env", {})),
            preflight=list(data.get("preflight", [])),
            tool_runs=[ToolRun.from_dict(t) for t in data.get("tool_runs", [])],
        )

    def to_json(self) -> str:
        return json.dumps(self.to_dict(), indent=2, sort_keys=True)

    @classmethod
    def from_json(cls, text: str) -> "RunRecord":
        return cls.from_dict(json.loads(text))
