"""netperf TCP_RR/UDP_RR adapter (omni output selectors).

Used as a cross-check on sfnt-pingpong/sockperf numbers. The client is
invoked with ``-o`` omni selectors so the output is an unambiguous CSV::

    MIN_LATENCY,MEAN_LATENCY,P50_LATENCY,P90_LATENCY,P99_LATENCY,MAX_LATENCY,TRANSACTION_RATE
    5,7.25,7,8,12,150,137931.03

Latency values are microseconds.
"""

from __future__ import annotations

from typing import Optional

from perfbench.config.schema import Protocol, Scenario
from perfbench.errors import ParseError
from perfbench.results.models import Measurement
from perfbench.tools.base import ToolAdapter, register, us_to_ns

_SELECTORS = (
    "MIN_LATENCY,MEAN_LATENCY,P50_LATENCY,P90_LATENCY,"
    "P99_LATENCY,MAX_LATENCY,TRANSACTION_RATE"
)

_QUANTILES = {
    "MIN_LATENCY": "0",
    "P50_LATENCY": "0.5",
    "P90_LATENCY": "0.9",
    "P99_LATENCY": "0.99",
    "MAX_LATENCY": "1",
}


@register
class Netperf(ToolAdapter):
    name = "netperf"
    DEFAULTS = {
        "protocol": "tcp",
        "msg_size": 64,
        "duration_s": 10,
        "port": 12865,
        "binary": "netperf",
        "server_binary": "netserver",
    }

    def _test_name(self) -> str:
        return "TCP_RR" if Protocol(self.params["protocol"]) is Protocol.TCP else "UDP_RR"

    def server_command(self, scenario: Scenario) -> Optional[str]:
        command = f"{self.params['server_binary']} -D -p {self.params['port']}"
        return self.wrap(command, scenario, role="server")

    def client_command(self, scenario: Scenario, server_address: str) -> str:
        size = self.params["msg_size"]
        command = (
            f"{self.params['binary']} -H {server_address} -p {self.params['port']} "
            f"-t {self._test_name()} -l {self.params['duration_s']} -- "
            f"-r {size},{size} -o {_SELECTORS}"
        )
        return self.wrap(command, scenario, role="client")

    def parse(self, output: str) -> list[Measurement]:
        lines = [ln.strip() for ln in output.splitlines() if ln.strip()]
        header_idx = None
        for i, line in enumerate(lines):
            if line.upper().startswith("MIN_LATENCY"):
                header_idx = i
                break
        if header_idx is None or header_idx + 1 >= len(lines):
            raise ParseError("netperf: omni CSV header not found in output")
        header = [h.strip().upper() for h in lines[header_idx].split(",")]
        try:
            values = [float(v) for v in lines[header_idx + 1].split(",")]
        except ValueError as exc:
            raise ParseError(f"netperf: cannot parse CSV values: {exc}") from exc
        if len(values) != len(header):
            raise ParseError("netperf: CSV header/value count mismatch")

        msg = str(self.params["msg_size"])
        test = self._test_name()
        measurements: list[Measurement] = []
        for key, value in zip(header, values):
            if key in _QUANTILES:
                measurements.append(
                    self.measurement(
                        "latency_ns", us_to_ns(value), "ns",
                        msg_size=msg, test=test, quantile=_QUANTILES[key],
                    )
                )
            elif key == "MEAN_LATENCY":
                measurements.append(
                    self.measurement(
                        "latency_mean_ns", us_to_ns(value), "ns", msg_size=msg, test=test
                    )
                )
            elif key == "TRANSACTION_RATE":
                measurements.append(
                    self.measurement(
                        "transactions_per_sec", value, "1/s", msg_size=msg, test=test
                    )
                )
        return measurements
