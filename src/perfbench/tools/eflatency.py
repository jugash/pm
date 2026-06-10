"""eflatency adapter (ships with OpenOnload).

Measures raw ef_vi layer-2 round-trip latency — the floor below
sockets-over-Onload. Run ``eflatency pong <intf>`` on the server and
``eflatency ping <intf>`` on the client.

The parser is deliberately tolerant: it accepts both ``key: value ns`` and
``key=value`` forms for mean/min/median/max/percentile fields, since the
output format has varied across Onload releases. Verify against your
installed version (see docs/tools.md).
"""

from __future__ import annotations

import re
from typing import Optional

from perfbench.config.schema import NetworkPath, Scenario
from perfbench.errors import ParseError
from perfbench.results.models import Measurement
from perfbench.tools.base import ToolAdapter, register, taskset_prefix

_FIELD_RE = re.compile(
    r"\b(mean|min|median|max|99%(?:ile)?|99\.9%(?:ile)?)\s*[:=]\s*([\d.]+)\s*(ns|us)?",
    re.IGNORECASE,
)

_METRIC_MAP = {
    "min": ("latency_ns", {"quantile": "0"}),
    "median": ("latency_ns", {"quantile": "0.5"}),
    "99%": ("latency_ns", {"quantile": "0.99"}),
    "99%ile": ("latency_ns", {"quantile": "0.99"}),
    "99.9%": ("latency_ns", {"quantile": "0.999"}),
    "99.9%ile": ("latency_ns", {"quantile": "0.999"}),
    "max": ("latency_ns", {"quantile": "1"}),
    "mean": ("latency_mean_ns", {}),
}


@register
class Eflatency(ToolAdapter):
    name = "eflatency"
    DEFAULTS = {
        "iterations": 100000,
        "binary": "eflatency",
    }

    def _check_path(self, scenario: Scenario) -> None:
        if scenario.network_path is not NetworkPath.EFVI:
            raise ParseError(
                "eflatency measures the raw ef_vi layer; use it only in "
                "scenarios with network_path: efvi"
            )

    def server_command(self, scenario: Scenario) -> Optional[str]:
        self._check_path(scenario)
        return (
            taskset_prefix(scenario.cpu.server_cores)
            + f"{self.params['binary']} pong {scenario.nic.interface}"
        )

    def client_command(self, scenario: Scenario, server_address: str) -> str:
        self._check_path(scenario)
        return (
            taskset_prefix(scenario.cpu.client_cores)
            + f"{self.params['binary']} -n {self.params['iterations']} "
            + f"ping {scenario.nic.interface}"
        )

    def parse(self, output: str) -> list[Measurement]:
        measurements: list[Measurement] = []
        for key, value, unit in _FIELD_RE.findall(output):
            key = key.lower()
            if key not in _METRIC_MAP:
                continue
            metric, labels = _METRIC_MAP[key]
            scale = 1000.0 if (unit or "ns").lower() == "us" else 1.0
            measurements.append(
                self.measurement(metric, float(value) * scale, "ns", **labels)
            )
        if not measurements:
            raise ParseError("eflatency: no latency fields found in output")
        return measurements
