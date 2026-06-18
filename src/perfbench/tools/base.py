"""Tool adapter base class, registry and command-wrapping helpers.

An adapter knows how to (a) build the server/client command lines for a
scenario — including core pinning (taskset) and Onload wrapping — and (b)
parse the tool's stdout into normalized :class:`Measurement` objects.
"""

from __future__ import annotations

import abc
from typing import Any, ClassVar, Mapping, Optional, Sequence

from perfbench.config.schema import NetworkPath, OnloadTuning, Platform, Scenario, ToolSpec
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


# Environment variables injected into the pod by the device plugins (k8s).
# The isolated-cpus plugin sets ISOLATED_CPUS to the comma-separated ids it
# actually allocated; the onload plugin mounts the .so and sets ONLOAD_LIB to
# its path. In device-plugin scenarios commands reference these at *runtime*
# inside the pod rather than baking core ids / an onload wrapper at build time.
ISOLATED_CPUS_ENV = "ISOLATED_CPUS"
ONLOAD_LIB_ENV = "ONLOAD_LIB"


def device_plugin(scenario: Scenario) -> bool:
    """True when the scenario gets its CPUs/Onload from device plugins.

    This is the k8s path: kubelet (via the plugins + Topology Manager) decides
    which isolated cores and which Onload library the pod receives, so the
    harness must read them from the pod's environment instead of from the
    scenario file. Bare metal keeps using the explicit, statically-isolated
    cores named in the scenario.
    """
    return scenario.platform is Platform.K8S


def data_path_ip(scenario: Scenario) -> str:
    """Shell expression for the local IPv4 on the data-path interface.

    On k8s the pod has both the cluster network (eth0) and the low-latency
    Multus secondary interface (the Solarflare PF moved in by host-device, or
    an SR-IOV VF — e.g. ``net1``). Socket tools must bind to that interface's
    address so traffic actually traverses the accelerated NIC rather than eth0.
    We read the address *at runtime* inside the pod from the resolved interface
    name — symmetric on both sides and independent of the (dynamic) IPAM.

    When a VLAN network is layered on the NIC the data-path interface is the
    VLAN sub-interface (e.g. ``vlan0``), so tools bind there rather than the
    bare port.

    Returns ``""`` for non-k8s scenarios or when the interface name has not
    been resolved yet (e.g. ``perfbench plan`` previews), so callers simply
    skip binding and behave exactly as before.
    """
    iface = scenario.nic.data_path_interface()
    if not device_plugin(scenario) or iface == "<resolved-at-runtime>":
        return ""
    return (
        f"$(ip -o -4 addr show dev {iface} scope global "
        f"| awk '{{print $4}}' | cut -d/ -f1 | head -n1)"
    )


def cores_arg(cores: Sequence[int]) -> str:
    return ",".join(str(c) for c in cores)


def cores_spec(cores: Sequence[int], scenario: Optional[Scenario] = None) -> str:
    """The cpu-list a tool should pin to, as a shell token.

    Device-plugin scenarios resolve to the injected ``"$ISOLATED_CPUS"`` (kept
    quoted so the value expands inside the pod, not on the harness host);
    otherwise the explicit comma list."""
    if scenario is not None and device_plugin(scenario):
        return f'"${ISOLATED_CPUS_ENV}"'
    return cores_arg(cores)


def taskset_prefix(cores: Sequence[int], scenario: Optional[Scenario] = None) -> str:
    if scenario is not None and device_plugin(scenario):
        return f'taskset -c "${ISOLATED_CPUS_ENV}" '
    return f"taskset -c {cores_arg(cores)} " if cores else ""


def taskset_nth_prefix(
    index: int, count: int, scenario: Scenario, cores: Sequence[int]
) -> str:
    """Pin a fan-out worker (receiver ``index``) to a single core.

    Bare metal cycles through the explicit ``cores``. Device-plugin scenarios
    pick the ``index``-th id out of the injected ``ISOLATED_CPUS`` list at
    runtime (``count`` is how many the pod requested, used to wrap the index)."""
    if device_plugin(scenario):
        field = (index % count) + 1 if count else 1
        return f'taskset -c "$(echo "${ISOLATED_CPUS_ENV}" | cut -d, -f{field})" '
    return taskset_prefix((cores[index % len(cores)],)) if cores else ""


def onload_prefix(scenario: Scenario) -> str:
    """Onload acceleration prefix for bypass scenarios.

    Bare metal uses the ``onload --profile=X`` wrapper from the image.
    Device-plugin (k8s) scenarios instead ``LD_PRELOAD`` the exact library the
    onload plugin injected (``"$ONLOAD_LIB"``); the EF_* tuning still applies."""
    if scenario.network_path is not NetworkPath.ONLOAD:
        return ""
    tuning = scenario.onload or OnloadTuning()
    parts = []
    if tuning.env:
        assigns = " ".join(f"{k}={v}" for k, v in sorted(tuning.env.items()))
        parts.append(f"env {assigns}")
    if device_plugin(scenario):
        parts.append(f'LD_PRELOAD="${ONLOAD_LIB_ENV}"')
        return " ".join(parts) + " "
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
        prefix = taskset_prefix(cores, scenario)
        if self.uses_network:
            prefix += onload_prefix(scenario)
        return prefix + command

    def wrap_fanout(self, command: str, scenario: Scenario, index: int, role: str) -> str:
        """Pin one fan-out worker (e.g. multicast receiver ``index``) to a
        single core, then apply Onload wrapping as usual."""
        prefix = taskset_nth_prefix(
            index, scenario.cpu.request_count(role), scenario,
            scenario.cpu.cores_for_role(role),
        )
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
