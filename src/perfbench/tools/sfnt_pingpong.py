"""sfnt-pingpong adapter (sfnettest, Solarflare/AMD).

The canonical kernel-vs-Onload latency tool: the identical binary runs over
the kernel stack and under ``onload``, sweeping message sizes in one
invocation. Output table columns (ns):

    #	size	mean	min	median	max	%ile	stddev	iter

The ``%ile`` column is the 99th percentile by default.
"""

from __future__ import annotations

from typing import Optional

from perfbench.config.schema import Protocol, Scenario
from perfbench.errors import ParseError
from perfbench.results.models import Measurement
from perfbench.tools.base import ToolAdapter, register


@register
class SfntPingpong(ToolAdapter):
    name = "sfnt-pingpong"
    DEFAULTS = {
        "protocol": "tcp",
        "msg_sizes": [32, 64, 256, 1024],
        "duration_s": 10,  # per message size (--maxms)
        "spin": True,
        "binary": "sfnt-pingpong",
    }

    def server_command(self, scenario: Scenario) -> Optional[str]:
        return self.wrap(str(self.params["binary"]), scenario, role="server")

    def client_command(self, scenario: Scenario, server_address: str) -> str:
        sizes = ",".join(str(s) for s in self.params["msg_sizes"])
        proto = Protocol(self.params["protocol"]).value
        maxms = int(self.params["duration_s"]) * 1000
        spin = "--spin " if self.params.get("spin") else ""
        command = (
            f"{self.params['binary']} --sizes={sizes} --maxms={maxms} "
            f"{spin}{proto} {server_address}"
        )
        return self.wrap(command, scenario, role="client")

    def parse(self, output: str) -> list[Measurement]:
        measurements: list[Measurement] = []
        for line in output.splitlines():
            line = line.strip()
            if not line or line.startswith("#"):
                continue
            fields = line.split()
            if len(fields) < 8:
                continue
            try:
                size = int(fields[0])
                mean, mn, median, mx, p99, stddev = (float(f) for f in fields[1:7])
                iterations = int(fields[7])
            except ValueError:
                continue
            msg = str(size)
            measurements.extend(
                [
                    self.measurement("latency_ns", mn, "ns", msg_size=msg, quantile="0"),
                    self.measurement("latency_ns", median, "ns", msg_size=msg, quantile="0.5"),
                    self.measurement("latency_ns", p99, "ns", msg_size=msg, quantile="0.99"),
                    self.measurement("latency_ns", mx, "ns", msg_size=msg, quantile="1"),
                    self.measurement("latency_mean_ns", mean, "ns", msg_size=msg),
                    self.measurement("latency_stddev_ns", stddev, "ns", msg_size=msg),
                    self.measurement("iterations_count", iterations, "count", msg_size=msg),
                ]
            )
        if not measurements:
            raise ParseError("sfnt-pingpong: no result rows found in output")
        return measurements
