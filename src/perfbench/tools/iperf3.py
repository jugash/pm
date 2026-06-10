"""iperf3 adapter (throughput sanity check, JSON output)."""

from __future__ import annotations

import json
from typing import Optional

from perfbench.config.schema import Protocol, Scenario
from perfbench.errors import ParseError
from perfbench.results.models import Measurement
from perfbench.tools.base import ToolAdapter, ms_to_ns, register


@register
class Iperf3(ToolAdapter):
    name = "iperf3"
    DEFAULTS = {
        "protocol": "tcp",
        "duration_s": 10,
        "parallel": 1,
        "port": 5201,
        "binary": "iperf3",
    }

    def server_command(self, scenario: Scenario) -> Optional[str]:
        command = f"{self.params['binary']} -s -1 -p {self.params['port']}"
        return self.wrap(command, scenario, role="server")

    def client_command(self, scenario: Scenario, server_address: str) -> str:
        udp = " -u -b 0" if Protocol(self.params["protocol"]) is Protocol.UDP else ""
        command = (
            f"{self.params['binary']} -c {server_address} -p {self.params['port']} "
            f"-t {self.params['duration_s']} -P {self.params['parallel']} -J{udp}"
        )
        return self.wrap(command, scenario, role="client")

    def parse(self, output: str) -> list[Measurement]:
        try:
            doc = json.loads(output)
        except json.JSONDecodeError as exc:
            raise ParseError(f"iperf3: invalid JSON output: {exc}") from exc
        end = doc.get("end", {})
        measurements: list[Measurement] = []

        if "sum_sent" in end:  # TCP
            measurements.append(
                self.measurement(
                    "throughput_bps", end["sum_sent"]["bits_per_second"], "bps",
                    direction="sent",
                )
            )
            measurements.append(
                self.measurement(
                    "throughput_bps", end["sum_received"]["bits_per_second"], "bps",
                    direction="received",
                )
            )
            retr = end["sum_sent"].get("retransmits")
            if retr is not None:
                measurements.append(
                    self.measurement("tcp_retransmits_count", retr, "count")
                )
        elif "sum" in end:  # UDP
            s = end["sum"]
            measurements.append(
                self.measurement("throughput_bps", s["bits_per_second"], "bps")
            )
            if "jitter_ms" in s:
                measurements.append(
                    self.measurement("udp_jitter_ns", ms_to_ns(s["jitter_ms"]), "ns")
                )
            if "lost_percent" in s:
                measurements.append(
                    self.measurement("udp_loss_percent", s["lost_percent"], "percent")
                )

        cpu = doc.get("end", {}).get("cpu_utilization_percent", {})
        for side in ("host_total", "remote_total"):
            if side in cpu:
                measurements.append(
                    self.measurement(
                        "cpu_utilization_percent", cpu[side], "percent",
                        side=side.replace("_total", ""),
                    )
                )
        if not measurements:
            raise ParseError("iperf3: no summary section found in JSON")
        return measurements
