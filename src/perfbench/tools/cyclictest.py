"""cyclictest adapter (rt-tests): scheduler wakeup latency.

Client-only CPU tool. Parsed from the ``-q`` summary lines::

    T: 0 ( 2613) P:99 I:1000 C: 100000 Min:      2 Act:    3 Avg:    3 Max:      27

Values are microseconds; normalized to ns.
"""

from __future__ import annotations

import re
from typing import Optional

from perfbench.config.schema import Scenario
from perfbench.errors import ParseError
from perfbench.results.models import Measurement
from perfbench.tools.base import ToolAdapter, cores_arg, register, us_to_ns

_LINE_RE = re.compile(
    r"T:\s*(\d+).*?Min:\s*(\d+).*?Avg:\s*(\d+).*?Max:\s*(\d+)"
)


@register
class Cyclictest(ToolAdapter):
    name = "cyclictest"
    needs_server = False
    uses_network = False
    DEFAULTS = {
        "duration_s": 10,
        "priority": 99,
        "interval_us": 1000,
        "binary": "cyclictest",
    }

    def server_command(self, scenario: Scenario) -> Optional[str]:
        return None

    def client_command(self, scenario: Scenario, server_address: str) -> str:
        cores = scenario.cpu.client_cores
        return (
            f"{self.params['binary']} -q -m --priority={self.params['priority']} "
            f"--interval={self.params['interval_us']} -t {len(cores)} "
            f"-a {cores_arg(cores)} -D {self.params['duration_s']}"
        )

    def parse(self, output: str) -> list[Measurement]:
        measurements: list[Measurement] = []
        for match in _LINE_RE.finditer(output):
            thread, mn, avg, mx = match.groups()
            measurements.extend(
                [
                    self.measurement(
                        "wakeup_latency_ns", us_to_ns(float(mn)), "ns",
                        thread=thread, quantile="0",
                    ),
                    self.measurement(
                        "wakeup_latency_mean_ns", us_to_ns(float(avg)), "ns", thread=thread
                    ),
                    self.measurement(
                        "wakeup_latency_ns", us_to_ns(float(mx)), "ns",
                        thread=thread, quantile="1",
                    ),
                ]
            )
        if not measurements:
            raise ParseError("cyclictest: no thread summary lines found in output")
        return measurements
