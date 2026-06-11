"""sysjitter adapter (Solarflare's OS-jitter measurement tool).

Client-only CPU tool: spins a thread per core and records every interruption
longer than a threshold. This is the ground truth for "is my core isolation
actually working". Output is a column-per-core table::

    core_i:                1        2
    threshold(ns):       200      200
    int_n:                12        3
    int_n_per_sec:       1.2      0.3
    int_median(ns):      300      310
    int_99(ns):          800      500
    int_999(ns):        1200      600
    int_max(ns):        5000      900
    int_total(%):      0.004    0.001
"""

from __future__ import annotations

from typing import Optional

from perfbench.config.schema import Scenario
from perfbench.errors import ParseError
from perfbench.results.models import Measurement
from perfbench.tools.base import ToolAdapter, cores_arg, register

# row key -> (metric, unit, extra labels)
_ROW_METRICS: dict[str, tuple[str, str, dict[str, str]]] = {
    "int_n": ("interruptions_count", "count", {}),
    "int_n_per_sec": ("interruptions_per_sec", "1/s", {}),
    "int_min(ns)": ("interruption_ns", "ns", {"quantile": "0"}),
    "int_median(ns)": ("interruption_ns", "ns", {"quantile": "0.5"}),
    "int_mean(ns)": ("interruption_mean_ns", "ns", {}),
    "int_90(ns)": ("interruption_ns", "ns", {"quantile": "0.9"}),
    "int_99(ns)": ("interruption_ns", "ns", {"quantile": "0.99"}),
    "int_999(ns)": ("interruption_ns", "ns", {"quantile": "0.999"}),
    "int_9999(ns)": ("interruption_ns", "ns", {"quantile": "0.9999"}),
    "int_max(ns)": ("interruption_ns", "ns", {"quantile": "1"}),
    "int_total(%)": ("interrupted_percent", "percent", {}),
}


@register
class Sysjitter(ToolAdapter):
    name = "sysjitter"
    needs_server = False
    uses_network = False
    DEFAULTS = {
        "runtime_s": 10,
        "threshold_ns": 200,
        "binary": "sysjitter",
    }

    def server_command(self, scenario: Scenario) -> Optional[str]:
        return None

    def client_command(self, scenario: Scenario, server_address: str) -> str:
        cores = scenario.cpu.client_cores
        return (
            f"{self.params['binary']} --runtime {self.params['runtime_s']} "
            f"--cores {cores_arg(cores)} {self.params['threshold_ns']}"
        )

    def timeout_s(self) -> float:
        return float(self.params.get("timeout_s") or float(self.params["runtime_s"]) + 60.0)

    def parse(self, output: str) -> list[Measurement]:
        cores: list[str] = []
        measurements: list[Measurement] = []
        for line in output.splitlines():
            line = line.strip()
            if not line or ":" not in line:
                continue
            key, _, rest = line.partition(":")
            key = key.strip()
            values = rest.split()
            if key == "core_i":
                cores = values
                continue
            if key not in _ROW_METRICS or not cores:
                continue
            metric, unit, extra = _ROW_METRICS[key]
            for core, raw in zip(cores, values):
                try:
                    value = float(raw)
                except ValueError:
                    continue
                measurements.append(
                    self.measurement(metric, value, unit, core=core, **extra)
                )
        if not measurements:
            raise ParseError("sysjitter: no per-core rows found in output")
        return measurements
