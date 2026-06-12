"""sockperf adapter.

sockperf adds the *under-load* dimension: latency percentiles at a
controlled message rate (``--mps``), which is the regime trading systems
actually operate in. One invocation tests one message size.

Multicast: when the scenario carries a ``multicast`` spec the target ``-i``
becomes the group address (the server joins it, the client sends to it) and
the protocol must be UDP. ``group_count`` > 1 generates a sockperf feed file
(``U:group:port`` per line) on both sides; ``receivers`` > 1 starts that
many server processes joined to the same group — every receiver gets each
message, the client measures latency to the first reflected reply and
reports the rest as duplicates (``messages_duplicated_count``).

``mc_rx_if`` / ``mc_tx_if`` params pass through to ``--mc-rx-if`` /
``--mc-tx-if`` and take the *IP address* of the interface (sockperf does not
accept interface names); set them when the host has multiple routes to the
group.

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

from perfbench.config.schema import MulticastSpec, Protocol, Scenario
from perfbench.errors import ParseError, SchemaError
from perfbench.results.models import Measurement
from perfbench.tools.base import ToolAdapter, quantile_label, register, us_to_ns

_PCT_RE = re.compile(r"percentile\s+([\d.]+)\s*=\s*([\d.]+)")
_MINMAX_RE = re.compile(r"<(MIN|MAX)>\s+observation\s*=\s*([\d.]+)")
_SUMMARY_RE = re.compile(r"Summary:\s+Latency is\s+([\d.]+)\s+usec")
_DROPPED_RE = re.compile(r"#\s*dropped messages\s*=\s*(\d+)")
_DUPLICATED_RE = re.compile(r"#\s*duplicated messages\s*=\s*(\d+)")


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
        "mc_rx_if": None,  # interface IP for --mc-rx-if
        "mc_tx_if": None,  # interface IP for --mc-tx-if
        "feed_file": "/tmp/perfbench-sockperf.feed",
    }

    def _proto_flag(self) -> str:
        return " --tcp" if Protocol(self.params["protocol"]) is Protocol.TCP else ""

    # -- multicast helpers ---------------------------------------------------

    def _mcast(self, scenario: Scenario) -> Optional[MulticastSpec]:
        mc = scenario.multicast
        if mc is not None and Protocol(self.params["protocol"]) is Protocol.TCP:
            raise SchemaError(
                "tools.sockperf.params.protocol",
                "multicast requires udp; set params.protocol: udp",
            )
        return mc

    def _feed_prefix(self, mc: MulticastSpec) -> str:
        """Generate the feed file in-line before the sockperf invocation."""
        lines = "".join(f"U:{g}:{mc.port}\\n" for g in mc.groups())
        return f"printf '{lines}' > {self.params['feed_file']} && "

    def _target_args(self, mc: Optional[MulticastSpec], address: str) -> str:
        if mc is None:
            return f"-i {address} -p {self.params['port']}"
        if mc.group_count > 1:
            return f"-f {self.params['feed_file']}"
        return f"-i {mc.group} -p {mc.port}"

    def _mc_if_flag(self, key: str, flag: str) -> str:
        value = self.params.get(key)
        return f" --{flag} {value}" if value else ""

    # -- commands --------------------------------------------------------------

    def _server_body(self, scenario: Scenario) -> tuple[str, str]:
        mc = self._mcast(scenario)
        prefix = self._feed_prefix(mc) if mc and mc.group_count > 1 else ""
        target = self._target_args(mc, "") if mc else f"-p {self.params['port']}"
        command = (
            f"{self.params['binary']} server {target}{self._proto_flag()}"
            f"{self._mc_if_flag('mc_rx_if', 'mc-rx-if')}"
        )
        return prefix, command

    def server_command(self, scenario: Scenario) -> Optional[str]:
        prefix, command = self._server_body(scenario)
        return prefix + self.wrap(command, scenario, role="server")

    def server_commands(self, scenario: Scenario) -> list[Optional[str]]:
        mc = self._mcast(scenario)
        if mc is None or mc.receivers <= 1:
            return [self.server_command(scenario)]
        prefix, command = self._server_body(scenario)
        cores = scenario.cpu.server_cores
        return [
            prefix + self.wrap_cores(command, scenario, (cores[i % len(cores)],))
            for i in range(mc.receivers)
        ]

    def client_command(self, scenario: Scenario, server_address: str) -> str:
        mode = str(self.params["mode"])
        if mode not in ("ping-pong", "under-load"):
            raise SchemaError("tools.sockperf.params.mode", f"unknown mode {mode!r}")
        mc = self._mcast(scenario)
        rate = f" --mps {self.params['mps']}" if mode == "under-load" else ""
        mcast = ""
        prefix = ""
        if mc is not None:
            mcast = f" --mc-ttl {mc.ttl}{self._mc_if_flag('mc_tx_if', 'mc-tx-if')}"
            if mc.group_count > 1:
                prefix = self._feed_prefix(mc)
        command = (
            f"{self.params['binary']} {mode} {self._target_args(mc, server_address)} "
            f"-m {self.params['msg_size']} -t {self.params['duration_s']}"
            f"{rate}{self._proto_flag()}{mcast}"
        )
        return prefix + self.wrap(command, scenario, role="client")

    # -- parsing ---------------------------------------------------------------

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
        for regex, metric in (
            (_DROPPED_RE, "messages_dropped_count"),
            (_DUPLICATED_RE, "messages_duplicated_count"),
        ):
            match = regex.search(output)
            if match:
                measurements.append(
                    self.measurement(
                        metric, float(match.group(1)), "count", msg_size=msg, mode=mode
                    )
                )
        if not measurements:
            raise ParseError("sockperf: no latency lines found in output")
        return measurements
