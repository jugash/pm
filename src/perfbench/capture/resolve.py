"""Per-host NIC name resolution.

Scenario ports may omit the interface name and declare identity instead:
an exact PCI address, or nothing beyond the layout's vendor (+ optional
``numa_node``). Before a run, the orchestrator resolves the real interface
name **independently on each host** — so the same scenario works when the
client machine calls the port ens1f0 and the server calls it enp59s0f0.

Resolution rules, per port:

1. ``name`` set — kept as-is (preflight still verifies its PCI identity).
2. ``pci`` set — the interface whose sysfs device symlink matches that
   address. Exact; fails if absent.
3. otherwise — the first unused interface (sorted by name, deterministic)
   whose PCI vendor matches ``nic.vendor`` (default Solarflare 0x1924 for
   bypass scenarios), filtered by ``numa_node`` when declared.

Resolved ports are enriched with the discovered pci/numa facts so run
records show exactly which silicon was measured.
"""

from __future__ import annotations

from dataclasses import replace

from perfbench.capture.discover import SOLARFLARE_VENDOR, discover_nics
from perfbench.config.schema import NetworkPath, NicPort, Scenario
from perfbench.errors import ExecutionError
from perfbench.runner.base import Executor


def _expected_vendor(scenario: Scenario) -> str | None:
    if scenario.nic.vendor:
        return scenario.nic.vendor
    if scenario.network_path is not NetworkPath.KERNEL:
        return SOLARFLARE_VENDOR
    return None


def resolve_nic(executor: Executor, scenario: Scenario) -> Scenario:
    """Return a scenario copy with every port name resolved on ``executor``.

    No-op (and no remote calls) when all ports already have names.
    """
    if scenario.nic.resolved:
        return scenario

    nics = discover_nics(executor)
    target = executor.describe()
    vendor = _expected_vendor(scenario)
    used = {p.name for p in scenario.nic.ports if p.name}
    new_ports: list[NicPort] = []

    for i, port in enumerate(scenario.nic.ports):
        if port.name:
            new_ports.append(port)
            continue

        if port.pci:
            matches = [n for n in nics if n["pci"].lower() == port.pci.lower()]
            if not matches:
                available = [(n["name"], n["pci"]) for n in nics]
                raise ExecutionError(
                    f"{target}: no interface at PCI {port.pci} "
                    f"(ports[{i}]); available: {available}"
                )
            chosen = matches[0]
        else:
            candidates = sorted(
                (
                    n for n in nics
                    if n["vendor"] == vendor
                    and n["name"] not in used
                    and (port.numa_node is None or n["numa_node"] == str(port.numa_node))
                ),
                key=lambda n: n["name"],
            )
            if not candidates:
                available = [(n["name"], n["vendor"], n["numa_node"]) for n in nics]
                raise ExecutionError(
                    f"{target}: no unused interface with vendor {vendor}"
                    + (f" on NUMA {port.numa_node}" if port.numa_node is not None else "")
                    + f" (ports[{i}]); available: {available}"
                )
            chosen = candidates[0]

        if chosen["name"] in used:
            raise ExecutionError(
                f"{target}: interface {chosen['name']} matched twice (ports[{i}])"
            )
        used.add(chosen["name"])
        numa = (
            int(chosen["numa_node"]) if str(chosen["numa_node"]).isdigit()
            else port.numa_node
        )
        new_ports.append(
            replace(port, name=chosen["name"], pci=chosen["pci"], numa_node=numa)
        )

    return replace(scenario, nic=replace(scenario.nic, ports=tuple(new_ports)))
