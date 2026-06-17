"""Typed scenario schema with strict validation.

Implemented with dataclasses + explicit validation (no third-party schema
library) so the framework runs on minimal hosts. Every ``from_dict`` raises
:class:`perfbench.errors.SchemaError` carrying a dotted document path on
invalid input.
"""

from __future__ import annotations

import ipaddress
import re
from dataclasses import dataclass, field
from enum import Enum
from typing import Any, Mapping, Optional, Sequence

from perfbench.errors import SchemaError

_ID_RE = re.compile(r"^[a-z0-9][a-z0-9_.-]*$")


class Platform(str, Enum):
    BAREMETAL = "baremetal"
    K8S = "k8s"


class NetworkPath(str, Enum):
    KERNEL = "kernel"
    ONLOAD = "onload"
    EFVI = "efvi"


class BondMode(str, Enum):
    NONE = "none"
    ACTIVE_BACKUP = "active_backup"
    LACP = "lacp_8023ad"


class Protocol(str, Enum):
    TCP = "tcp"
    UDP = "udp"


# ---------------------------------------------------------------------------
# validation helpers
# ---------------------------------------------------------------------------

def _expect_mapping(value: Any, path: str) -> Mapping[str, Any]:
    if not isinstance(value, Mapping):
        raise SchemaError(path, f"expected a mapping, got {type(value).__name__}")
    return value


def _expect_list(value: Any, path: str) -> Sequence[Any]:
    if not isinstance(value, (list, tuple)):
        raise SchemaError(path, f"expected a list, got {type(value).__name__}")
    return value


def _expect_str(value: Any, path: str) -> str:
    if not isinstance(value, str) or not value:
        raise SchemaError(path, f"expected a non-empty string, got {value!r}")
    return value


def _expect_int(value: Any, path: str, minimum: Optional[int] = None) -> int:
    if isinstance(value, bool) or not isinstance(value, int):
        raise SchemaError(path, f"expected an integer, got {value!r}")
    if minimum is not None and value < minimum:
        raise SchemaError(path, f"expected >= {minimum}, got {value}")
    return value


def _coerce_enum(enum_cls, value: Any, path: str):
    try:
        return enum_cls(value)
    except ValueError:
        allowed = ", ".join(e.value for e in enum_cls)
        raise SchemaError(path, f"invalid value {value!r}; allowed: {allowed}") from None


def _int_tuple(value: Any, path: str) -> tuple[int, ...]:
    items = _expect_list(value, path)
    return tuple(_expect_int(v, f"{path}[{i}]", minimum=0) for i, v in enumerate(items))


def _reject_unknown(data: Mapping[str, Any], known: set[str], path: str) -> None:
    unknown = set(data) - known
    if unknown:
        raise SchemaError(path, f"unknown keys: {sorted(unknown)}")


# ---------------------------------------------------------------------------
# model objects
# ---------------------------------------------------------------------------

@dataclass(frozen=True)
class NicPort:
    """One physical NIC port participating in a scenario.

    ``name`` is optional: interface names differ per machine (ens1f0 vs
    enp59s0f0), so ports may instead be declared by PCI address (exact slot)
    or rely on vendor+NUMA matching — the harness resolves the real name on
    each host at run time (see ``perfbench.capture.resolve``).
    """

    name: Optional[str] = None  # OS interface name; resolved at runtime if unset
    card: str = ""  # human label, e.g. "x2522-a"
    pci: str = ""  # PCI address, e.g. 0000:3b:00.0
    numa_node: Optional[int] = None

    @classmethod
    def from_dict(cls, data: Any, path: str = "nic.ports") -> "NicPort":
        data = _expect_mapping(data, path)
        _reject_unknown(data, {"name", "card", "pci", "numa_node"}, path)
        numa = data.get("numa_node")
        if numa is not None:
            numa = _expect_int(numa, f"{path}.numa_node", minimum=0)
        name = data.get("name")
        if name is not None:
            name = _expect_str(name, f"{path}.name")
        return cls(
            name=name,
            card=str(data.get("card", "")),
            pci=str(data.get("pci", "")),
            numa_node=numa,
        )


@dataclass(frozen=True)
class NicLayout:
    """NIC/bonding layout under test.

    ``vendor`` is the expected PCI vendor id (e.g. "0x1924" = Solarflare).
    Identity is verified by preflight against sysfs, not the interface name —
    names like ens1f0 vary between machines and can drift across firmware
    changes. For bypass scenarios the check defaults to Solarflare even when
    unset.
    """

    ports: tuple[NicPort, ...]
    bond_mode: BondMode = BondMode.NONE
    bond_name: Optional[str] = None
    vendor: Optional[str] = None

    @classmethod
    def from_dict(cls, data: Any, path: str = "nic") -> "NicLayout":
        data = _expect_mapping(data, path)
        _reject_unknown(data, {"ports", "bond_mode", "bond_name", "vendor"}, path)
        vendor = data.get("vendor")
        if vendor is not None:
            vendor = str(vendor).lower()
            if not re.match(r"^0x[0-9a-f]{4}$", vendor):
                raise SchemaError(
                    f"{path}.vendor",
                    f"expected a PCI vendor id like '0x1924', got {vendor!r}",
                )
        ports_raw = _expect_list(data.get("ports", []), f"{path}.ports")
        if not ports_raw:
            raise SchemaError(f"{path}.ports", "at least one port is required")
        ports = tuple(
            NicPort.from_dict(p, f"{path}.ports[{i}]") for i, p in enumerate(ports_raw)
        )
        bond_mode = _coerce_enum(BondMode, data.get("bond_mode", "none"), f"{path}.bond_mode")
        bond_name = data.get("bond_name")
        if bond_mode is not BondMode.NONE:
            if len(ports) < 2:
                raise SchemaError(f"{path}.ports", "bonded layouts need >= 2 ports")
            if not bond_name:
                raise SchemaError(f"{path}.bond_name", "required when bond_mode != none")
        return cls(ports=ports, bond_mode=bond_mode, bond_name=bond_name, vendor=vendor)

    @property
    def interface(self) -> str:
        """The interface traffic actually flows over (bond device or port).

        Returns a placeholder for unresolved ports; the orchestrator always
        resolves names per host before building commands.
        """
        if self.bond_name:
            return self.bond_name
        return self.ports[0].name or "<resolved-at-runtime>"

    @property
    def resolved(self) -> bool:
        return all(p.name for p in self.ports)

    def label(self) -> str:
        first = self.ports[0].name or self.ports[0].pci or "auto"
        if self.bond_mode is BondMode.NONE:
            return f"none:{first}"
        return f"{self.bond_mode.value}:{self.bond_name}({len(self.ports)})"


@dataclass(frozen=True)
class CpuLayout:
    """CPU pinning/isolation layout under test.

    Two ways to say how many exclusive cores a role needs:

    * explicit IDs (``client_cores``/``server_cores``) — required for the
      ``baremetal`` platform, where the harness ``taskset``\\s onto those exact
      isolated cores on the host;
    * a ``count`` — the device-plugin path (k8s): the pod *requests* that many
      isolated CPUs and the plugin injects the actual ids as ``ISOLATED_CPUS``
      at runtime, so the scenario must not (and cannot) name them ahead of
      time. When ``count`` is set the per-role core lists are optional.
    """

    client_cores: tuple[int, ...]
    server_cores: tuple[int, ...]
    irq_cores: tuple[int, ...] = ()
    numa_node: Optional[int] = None
    require_isolated: bool = True
    count: Optional[int] = None

    @classmethod
    def from_dict(cls, data: Any, path: str = "cpu") -> "CpuLayout":
        data = _expect_mapping(data, path)
        _reject_unknown(
            data,
            {"client_cores", "server_cores", "irq_cores", "numa_node",
             "require_isolated", "count"},
            path,
        )
        client = _int_tuple(data.get("client_cores", []), f"{path}.client_cores")
        server = _int_tuple(data.get("server_cores", []), f"{path}.server_cores")
        irq = _int_tuple(data.get("irq_cores", []), f"{path}.irq_cores")
        count = data.get("count")
        if count is not None:
            count = _expect_int(count, f"{path}.count", minimum=1)
        # Either name the cores explicitly, or request a count (device plugin).
        if count is None:
            if not client:
                raise SchemaError(
                    f"{path}.client_cores",
                    "at least one core is required (or set cpu.count to request "
                    "isolated CPUs from a device plugin)",
                )
            if not server:
                raise SchemaError(
                    f"{path}.server_cores",
                    "at least one core is required (or set cpu.count to request "
                    "isolated CPUs from a device plugin)",
                )
        overlap = set(client) & set(irq)
        if overlap:
            raise SchemaError(
                f"{path}.irq_cores",
                f"irq_cores overlap client_cores: {sorted(overlap)}",
            )
        numa = data.get("numa_node")
        if numa is not None:
            numa = _expect_int(numa, f"{path}.numa_node", minimum=0)
        require_isolated = data.get("require_isolated", True)
        if not isinstance(require_isolated, bool):
            raise SchemaError(f"{path}.require_isolated", "expected a boolean")
        return cls(
            client_cores=client,
            server_cores=server,
            irq_cores=irq,
            numa_node=numa,
            require_isolated=require_isolated,
            count=count,
        )

    def cores_for_role(self, role: str) -> tuple[int, ...]:
        if role == "client":
            return self.client_cores
        if role == "server":
            return self.server_cores
        raise ValueError(f"unknown role: {role}")

    def request_count(self, role: str) -> int:
        """How many exclusive/isolated CPUs this role needs.

        ``count`` wins when set (device-plugin request); otherwise it is the
        number of explicitly named cores. This is what sizes the pod's
        isolated-cpus resource request and the thread counts of CPU tools.
        """
        if self.count is not None:
            return self.count
        return len(self.cores_for_role(role))


@dataclass(frozen=True)
class OnloadTuning:
    """Onload acceleration settings (profile + EF_* environment)."""

    profile: Optional[str] = "latency"
    env: Mapping[str, str] = field(default_factory=dict)

    @classmethod
    def from_dict(cls, data: Any, path: str = "onload") -> "OnloadTuning":
        data = _expect_mapping(data, path)
        _reject_unknown(data, {"profile", "env"}, path)
        profile = data.get("profile", "latency")
        if profile is not None and not isinstance(profile, str):
            raise SchemaError(f"{path}.profile", "expected a string or null")
        env_raw = data.get("env", {})
        env_map = _expect_mapping(env_raw, f"{path}.env") if env_raw else {}
        env = {str(k): str(v) for k, v in env_map.items()}
        for key in env:
            if not re.match(r"^[A-Z][A-Z0-9_]*$", key):
                raise SchemaError(f"{path}.env.{key}", "expected an env-var style key")
        return cls(profile=profile, env=env)


@dataclass(frozen=True)
class MulticastSpec:
    """Multicast traffic shape for a scenario.

    ``group`` is the base IPv4 group; ``group_count`` > 1 subscribes to that
    many *consecutive* groups (fan-in: filter-table and demux cost).
    ``receivers`` > 1 starts that many receiver processes on the server host
    all joined to the same group (fan-out: per-socket delivery cost).
    """

    group: str = "239.100.1.1"
    port: int = 12000
    ttl: int = 1
    group_count: int = 1
    receivers: int = 1

    @classmethod
    def from_dict(cls, data: Any, path: str = "multicast") -> "MulticastSpec":
        data = _expect_mapping(data, path)
        _reject_unknown(data, {"group", "port", "ttl", "group_count", "receivers"}, path)
        group = str(data.get("group", cls.group))
        try:
            addr = ipaddress.IPv4Address(group)
        except ipaddress.AddressValueError:
            raise SchemaError(f"{path}.group", f"invalid IPv4 address {group!r}") from None
        if not addr.is_multicast:
            raise SchemaError(
                f"{path}.group",
                f"{group} is not a multicast address (224.0.0.0/4)",
            )
        port = _expect_int(data.get("port", cls.port), f"{path}.port", minimum=1)
        if port > 65535:
            raise SchemaError(f"{path}.port", f"expected <= 65535, got {port}")
        ttl = _expect_int(data.get("ttl", cls.ttl), f"{path}.ttl", minimum=1)
        if ttl > 255:
            raise SchemaError(f"{path}.ttl", f"expected <= 255, got {ttl}")
        group_count = _expect_int(
            data.get("group_count", cls.group_count), f"{path}.group_count", minimum=1
        )
        last = ipaddress.IPv4Address(int(addr) + group_count - 1)
        if not last.is_multicast:
            raise SchemaError(
                f"{path}.group_count",
                f"{group} + {group_count} groups runs past the multicast range",
            )
        receivers = _expect_int(
            data.get("receivers", cls.receivers), f"{path}.receivers", minimum=1
        )
        return cls(
            group=group, port=port, ttl=ttl, group_count=group_count, receivers=receivers
        )

    def groups(self) -> tuple[str, ...]:
        """The consecutive group addresses subscribed to."""
        base = int(ipaddress.IPv4Address(self.group))
        return tuple(str(ipaddress.IPv4Address(base + i)) for i in range(self.group_count))


@dataclass(frozen=True)
class ToolSpec:
    """A benchmark tool invocation with tool-specific parameters."""

    name: str
    params: Mapping[str, Any] = field(default_factory=dict)

    @classmethod
    def from_dict(cls, data: Any, path: str = "tools") -> "ToolSpec":
        if isinstance(data, str):
            data = {"name": data}
        data = _expect_mapping(data, path)
        _reject_unknown(data, {"name", "params"}, path)
        name = _expect_str(data.get("name"), f"{path}.name")
        if not _ID_RE.match(name):
            raise SchemaError(f"{path}.name", f"invalid tool name {name!r}")
        params = data.get("params", {})
        params = dict(_expect_mapping(params, f"{path}.params")) if params else {}
        return cls(name=name, params=params)


@dataclass(frozen=True)
class Scenario:
    """One fully-specified benchmark configuration."""

    id: str
    platform: Platform
    network_path: NetworkPath
    nic: NicLayout
    cpu: CpuLayout
    tools: tuple[ToolSpec, ...]
    onload: Optional[OnloadTuning] = None
    multicast: Optional[MulticastSpec] = None
    description: str = ""
    repetitions: int = 3
    tags: tuple[str, ...] = ()

    KNOWN_KEYS = {
        "id", "platform", "network_path", "nic", "cpu", "tools",
        "onload", "multicast", "description", "repetitions", "tags",
    }

    @classmethod
    def from_dict(cls, data: Any, path: str = "scenario") -> "Scenario":
        data = _expect_mapping(data, path)
        _reject_unknown(data, cls.KNOWN_KEYS, path)

        sid = _expect_str(data.get("id"), f"{path}.id")
        if not _ID_RE.match(sid):
            raise SchemaError(f"{path}.id", f"invalid scenario id {sid!r}")

        platform = _coerce_enum(Platform, data.get("platform"), f"{path}.platform")
        network_path = _coerce_enum(
            NetworkPath, data.get("network_path"), f"{path}.network_path"
        )
        nic = NicLayout.from_dict(data.get("nic"), f"{path}.nic")
        cpu = CpuLayout.from_dict(data.get("cpu"), f"{path}.cpu")

        tools_raw = _expect_list(data.get("tools", []), f"{path}.tools")
        if not tools_raw:
            raise SchemaError(f"{path}.tools", "at least one tool is required")
        tools = tuple(
            ToolSpec.from_dict(t, f"{path}.tools[{i}]") for i, t in enumerate(tools_raw)
        )

        onload = None
        if data.get("onload") is not None:
            onload = OnloadTuning.from_dict(data["onload"], f"{path}.onload")
        if network_path is NetworkPath.KERNEL and onload is not None:
            raise SchemaError(
                f"{path}.onload", "onload tuning is meaningless with network_path=kernel"
            )
        if network_path is NetworkPath.ONLOAD and onload is None:
            onload = OnloadTuning()

        multicast = None
        if data.get("multicast") is not None:
            multicast = MulticastSpec.from_dict(data["multicast"], f"{path}.multicast")

        # name-free ports need something to resolve by: an exact PCI address,
        # or a vendor to match (explicit, or the Solarflare default implied
        # by a kernel-bypass network path)
        effective_vendor = nic.vendor or (
            "0x1924" if network_path is not NetworkPath.KERNEL else None
        )
        for i, port in enumerate(nic.ports):
            if not port.name and not port.pci and not effective_vendor:
                raise SchemaError(
                    f"{path}.nic.ports[{i}]",
                    "port needs a name, a pci address, or nic.vendor to resolve by",
                )

        repetitions = _expect_int(data.get("repetitions", 3), f"{path}.repetitions", minimum=1)
        tags_raw = data.get("tags", [])
        tags = tuple(str(t) for t in _expect_list(tags_raw, f"{path}.tags")) if tags_raw else ()

        return cls(
            id=sid,
            platform=platform,
            network_path=network_path,
            nic=nic,
            cpu=cpu,
            tools=tools,
            onload=onload,
            multicast=multicast,
            description=str(data.get("description", "")),
            repetitions=repetitions,
            tags=tags,
        )

    def labels(self) -> dict[str, str]:
        """Canonical label set used in metrics and result records."""
        return {
            "scenario": self.id,
            "platform": self.platform.value,
            "network_path": self.network_path.value,
            "bond": self.nic.bond_mode.value,
        }
