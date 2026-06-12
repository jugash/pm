"""Tool adapter base class, registry and command-wrapping helpers.

An adapter knows how to (a) build the server/client command lines for a
scenario — including core pinning (taskset) and Onload wrapping — and (b)
parse the tool's stdout into normalized :class:`Measurement` objects.
"""

from __future__ import annotations

import abc
from typing import Any, ClassVar, Mapping, Optional, Sequence

from perfbench.config.schema import NetworkPath, OnloadTuning, Scenario, ToolSpec
from perfbench.errors import SchemaError
from perfbench.results.models import Measurement

_REGISTRY: dict[str, type["ToolAdapter"]] = {}


def register(cls: type["ToolAdapter"]) -> type["ToolAdapter"]:
    _REGISTRY[cls.name] = cls
    return cls


def available_tools() -> list[str]:
    return sorted(_REGISTRY)


def create_tool(spec: ToolSpec) -> "ToolAdapter":
    try:
        cls = _REGISTRY[spec.name]
    except KeyError:
        raise SchemaError(
            f"tools.{spec.name}",
            f"unknown tool {spec.name!r}; available: {available_tools()}",
        ) from None
    return cls(spec.params)


def cores_arg(cores: Sequence[int]) -> str:
    return ",".join(str(c) for c in cores)


def taskset_prefix(cores: Sequence[int]) -> str:
    return f"taskset -c {cores_arg(cores)} " if cores else ""


def onload_prefix(scenario: Scenario) -> str:
    """``env EF_...=... onload --profile=X`` prefix for bypass scenarios."""
    if scenario.network_path is not NetworkPath.ONLOAD:
        return ""
    tuning = scenario.onload or OnloadTuning()
    parts = []
    if tuning.env:
        assigns = " ".join(f"{k}={v}" for k, v in sorted(tuning.env.items()))
        parts.append(f"env {assigns}")
    onload = "onload"
    if tuning.profile:
        onload += f" --profile={tuning.profile}"
    parts.append(onload)
    return " ".join(parts) + " "


def us_to_ns(value: float) -> float:
    return float(value) * 1_000.0


def ms_to_ns(value: float) -> float:
    return float(value) * 1_000_000.0


class ToolAdapter(abc.ABC):
    """Base class for benchmark tool adapters."""

    name: ClassVar[str]
    needs_server: ClassVar[bool] = True
    uses_network: ClassVar[bool] = True  # CPU-only tools skip onload wrapping
    DEFAULTS: ClassVar[Mapping[str, Any]] = {}

    def __init__(self, params: Optional[Mapping[str, Any]] = None):
        self.params: dict[str, Any] = {**self.DEFAULTS, **(params or {})}

    # -- command construction ----------------------------------------------

    @abc.abstractmethod
    def server_command(self, scenario: Scenario) -> Optional[str]:
        """Server-side command, or None for client-only tools."""

    def server_commands(self, scenario: Scenario) -> list[Optional[str]]:
        """All server-side commands for the scenario.

        One entry by default. Multicast-capable adapters override this to
        start ``scenario.multicast.receivers`` receiver processes (fan-out),
        each pinned to its own core.
        """
        return [self.server_command(scenario)]

    @abc.abstractmethod
    def client_command(self, scenario: Scenario, server_address: str) -> str:
        """Client-side command line."""

    @abc.abstractmethod
    def parse(self, output: str) -> list[Measurement]:
        """Parse client stdout into normalized measurements."""

    # -- helpers -------------------------------------------------------------

    def timeout_s(self) -> float:
        """Client command timeout; generous margin over configured duration."""
        explicit = self.params.get("timeout_s")
        if explicit is not None:
            return float(explicit)
        duration = float(self.params.get("duration_s", 30))
        sizes = self.params.get("msg_sizes") or [0]
        return duration * max(1, len(sizes)) + 60.0

    def wrap(self, command: str, scenario: Scenario, role: str) -> str:
        """Apply core pinning and (for network tools) Onload wrapping."""
        return self.wrap_cores(command, scenario, scenario.cpu.cores_for_role(role))

    def wrap_cores(
        self, command: str, scenario: Scenario, cores: Sequence[int]
    ) -> str:
        """Like :meth:`wrap` but pinning an explicit core set (fan-out
        receivers get one core each rather than the whole role set)."""
        prefix = taskset_prefix(cores)
        if self.uses_network:
            prefix += onload_prefix(scenario)
        return prefix + command

    def measurement(
        self,
        metric: str,
        value: float,
        unit: str,
        **labels: str,
    ) -> Measurement:
        return Measurement(
            tool=self.name,
            metric=metric,
            value=float(value),
            unit=unit,
            labels={k: str(v) for k, v in labels.items() if v is not None},
        )


def quantile_label(percent: float) -> str:
    """Format a percentage (99.9) as a quantile label ('0.999')."""
    q = percent / 100.0
    text = f"{q:.6f}".rstrip("0").rstrip(".")
    return text if text else "0"
