"""sockperf adapter.

sockperf adds the *under-load* dimension: latency percentiles at a
controlled message rate (``--mps``), which is the regime trading systems
actually operate in. One invocation tests one message size.

Parsed lines::

    sockperf: Summary: Latency is 2.341 usec
    sockperf: ---> <MAX> observation =   45.123
    sockperf: ---> percentile 99.999 =   12.345
    sockperf: ---> percentile 50.000 =    2.341
    sockperf: ---> <MIN> observation =    1.987
    sockperf: # dropped messages = 0, # duplicated messages = 0, ...
"""

from __future__ import annotations

import re
from typing import Optional

from perfbench.config.schema import Protocol, Scenario
from perfbench.errors import ParseError
from perfbench.results.models import Measurement
from perfbench.tools.base import ToolAdapter, quantile_label, register, us_to_ns

_PCT_RE = re.compile(r"percentile\s+([\d.]+)\s*=\s*([\d.]+)")
_MINMAX_RE = re.compile(r"<(MIN|MAX)>\s+observation\s*=\s*([\d.]+)")
_SUMMARY_RE = re.compile(r"Summary:\s+Latency is\s+([\d.]+)\s+usec")
_DROPPED_RE = re.compile(r"#\s*dropped messages\s*=\s*(\d+)")


@register
class Sockperf(ToolAdapter):
    name = "sockperf"
    DEFAULTS = {
        "mode": "ping-pong",  # or "under-load"
        "protocol": "tcp",
        "msg_size": 64,
        "duration_s": 10,
        "mps": 100000,  # only used by under-load
        "port": 11111,
        "binary": "sockperf",
    }

    def _proto_flag(self) -> str:
        return " --tcp" if Protocol(self.params["protocol"]) is Protocol.TCP else ""

    def server_command(self, scenario: Scenario) -> Optional[str]:
        command = f"{self.params['binary']} server -p {self.params['port']}{self._proto_flag()}"
        return self.wrap(command, scenario, role="server")

    def client_command(self, scenario: Scenario, server_address: str) -> str:
        mode = str(self.params["mode"])
        if mode not in ("ping-pong", "under-load"):
            raise ParseError(f"sockperf: unknown mode {mode!r}")
        rate = f" --mps {self.params['mps']}" if mode == "under-load" else ""
        command = (
            f"{self.params['binary']} {mode} -i {server_address} -p {self.params['port']} "
            f"-m {self.params['msg_size']} -t {self.params['duration_s']}"
            f"{rate}{self._proto_flag()}"
        )
        return self.wrap(command, scenario, role="client")

    def parse(self, output: str) -> list[Measurement]:
        msg = str(self.params["msg_size"])
        mode = str(self.params["mode"])
        measurements: list[Measurement] = []
        for percent, value in _PCT_RE.findall(output):
            measurements.append(
                self.measurement(
                    "latency_ns",
                    us_to_ns(float(value)),
                    "ns",
                    msg_size=msg,
                    mode=mode,
                    quantile=quantile_label(float(percent)),
                )
            )
        for kind, value in _MINMAX_RE.findall(output):
            measurements.append(
                self.measurement(
                    "latency_ns",
                    us_to_ns(float(value)),
                    "ns",
                    msg_size=msg,
                    mode=mode,
                    quantile="0" if kind == "MIN" else "1",
                )
            )
        summary = _SUMMARY_RE.search(output)
        if summary:
            measurements.append(
                self.measurement(
                    "latency_mean_ns",
                    us_to_ns(float(summary.group(1))),
                    "ns",
                    msg_size=msg,
                    mode=mode,
                )
            )
        dropped = _DROPPED_RE.search(output)
        if dropped:
            measurements.append(
                self.measurement(
                    "messages_dropped_count",
                    float(dropped.group(1)),
                    "count",
                    msg_size=msg,
                    mode=mode,
                )
            )
        if not measurements:
            raise ParseError("sockperf: no latency lines found in output")
        return measurements
