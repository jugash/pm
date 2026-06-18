"""sfnt-stream adapter (sfnettest, Solarflare/AMD).

The canonical Onload *multicast* benchmark: one-way streaming latency at a
swept message rate, over a multicast group when the scenario carries a
``multicast`` spec (``--mcast``/``--mcastintf``), unicast UDP otherwise.
Where sockperf under-load gives one rate per invocation, sfnt-stream sweeps
``--rates=min-max+step`` in one run and reports a latency row per rate.

Like eflatency, output has varied across sfnettest releases, so the parser
is header-driven: it reads the last ``#`` comment line naming columns
(expects at least ``mean`` and ``median``) and maps known columns; unknown
columns are ignored. Verify against your installed version (docs/tools.md).
Expected shape::

    #	mps	send	recv	mean	min	median	max	%ile	stddev
    	10000	10000	10000	2500	2300	2480	9000	2900	80

Latencies are reported in ns, one set per achieved rate (label ``mps``).
``send``/``recv`` columns, when both present, also yield a per-rate
``messages_dropped_count``.
"""

from __future__ import annotations

from typing import Optional

from perfbench.config.schema import Protocol, Scenario
from perfbench.errors import ParseError, SchemaError
from perfbench.results.models import Measurement
from perfbench.tools.base import ToolAdapter, register

_COLUMN_METRICS = {
    "min": ("latency_ns", {"quantile": "0"}),
    "median": ("latency_ns", {"quantile": "0.5"}),
    "%ile": ("latency_ns", {"quantile": "0.99"}),
    "max": ("latency_ns", {"quantile": "1"}),
    "mean": ("latency_mean_ns", {}),
    "stddev": ("latency_stddev_ns", {}),
}


@register
class SfntStream(ToolAdapter):
    name = "sfnt-stream"
    DEFAULTS = {
        "protocol": "udp",
        "msg_size": 64,
        "rates": [10000, 1000000, 100000],  # [min, max, step] mps
        "duration_s": 10,  # per rate (--maxms)
        "spin": True,
        "binary": "sfnt-stream",
    }

    def _check(self, scenario: Scenario) -> None:
        if scenario.multicast and Protocol(self.params["protocol"]) is Protocol.TCP:
            raise SchemaError(
                "tools.sfnt-stream.params.protocol",
                "multicast requires udp; set params.protocol: udp",
            )

    def _rates_flag(self) -> str:
        rates = list(self.params["rates"])
        if len(rates) != 3:
            raise SchemaError(
                "tools.sfnt-stream.params.rates",
                f"expected [min, max, step], got {rates!r}",
            )
        lo, hi, step = (int(r) for r in rates)
        return f"--rates={lo}-{hi}+{step}"

    def server_command(self, scenario: Scenario) -> Optional[str]:
        self._check(scenario)
        return self.wrap(str(self.params["binary"]), scenario, role="server")

    def client_command(self, scenario: Scenario, server_address: str) -> str:
        self._check(scenario)
        proto = Protocol(self.params["protocol"]).value
        maxms = int(self.params["duration_s"]) * 1000
        spin = "--spin " if self.params.get("spin") else ""
        mcast = ""
        if scenario.multicast:
            mcast = (
                f"--mcast={scenario.multicast.group} "
                f"--mcastintf={scenario.nic.data_path_interface()} "
            )
        command = (
            f"{self.params['binary']} --msgsize={self.params['msg_size']} "
            f"{self._rates_flag()} --maxms={maxms} "
            f"{spin}{mcast}{proto} {server_address}"
        )
        return self.wrap(command, scenario, role="client")

    def timeout_s(self) -> float:
        explicit = self.params.get("timeout_s")
        if explicit is not None:
            return float(explicit)
        lo, hi, step = (int(r) for r in self.params["rates"])
        steps = max(1, (hi - lo) // max(1, step) + 1)
        return float(self.params["duration_s"]) * steps + 60.0

    def parse(self, output: str) -> list[Measurement]:
        msg = str(self.params["msg_size"])
        columns: list[str] = []
        measurements: list[Measurement] = []
        for line in output.splitlines():
            line = line.strip()
            if not line:
                continue
            if line.startswith("#"):
                fields = line.lstrip("#").split()
                if "mean" in fields and "median" in fields:
                    columns = fields
                continue
            if not columns:
                continue
            values = line.split()
            if len(values) != len(columns):
                continue
            try:
                row = {c: float(v) for c, v in zip(columns, values)}
            except ValueError:
                continue
            rate = row.get("mps")
            labels = {"msg_size": msg}
            if rate is not None:
                labels["mps"] = str(int(rate))
            for column, (metric, extra) in _COLUMN_METRICS.items():
                if column in row:
                    measurements.append(
                        self.measurement(metric, row[column], "ns", **labels, **extra)
                    )
            if "send" in row and "recv" in row:
                measurements.append(
                    self.measurement(
                        "messages_dropped_count",
                        max(0.0, row["send"] - row["recv"]),
                        "count",
                        **labels,
                    )
                )
        if not measurements:
            raise ParseError("sfnt-stream: no result rows found in output")
        return measurements
