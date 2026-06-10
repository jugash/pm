"""NIC discovery: enumerate interfaces by PCI identity on a target.

Used by ``perfbench nics`` so scenario files can be written from facts
(per-machine interface name, PCI address, NUMA node) instead of guessed
names. Solarflare ports are tagged via PCI vendor id 0x1924.
"""

from __future__ import annotations

from typing import Any

from perfbench.runner.base import Executor

SOLARFLARE_VENDOR = "0x1924"

# One line per physical (PCI-backed) interface:
#   name|vendor|device|pci_address|numa_node
_LIST_COMMAND = (
    "for d in /sys/class/net/*/device; do "
    '[ -e "$d" ] || continue; '
    'i=$(basename $(dirname "$d")); '
    'v=$(cat "$d/vendor" 2>/dev/null); '
    'dev=$(cat "$d/device" 2>/dev/null); '
    'p=$(basename $(readlink -f "$d")); '
    'n=$(cat "$d/numa_node" 2>/dev/null); '
    'echo "$i|$v|$dev|$p|$n"; '
    "done"
)


def discover_nics(executor: Executor) -> list[dict[str, Any]]:
    """Enumerate PCI-backed network interfaces with identity details."""
    result = executor.run(_LIST_COMMAND, timeout=20)
    nics: list[dict[str, Any]] = []
    for line in result.stdout.splitlines():
        parts = line.strip().split("|")
        if len(parts) != 5 or not parts[0]:
            continue
        name, vendor, device, pci, numa = (p.strip() for p in parts)
        driver_result = executor.run(
            f"ethtool -i {name} 2>/dev/null | awk '/^driver:/ {{print $2}}'", timeout=10
        )
        nics.append(
            {
                "name": name,
                "driver": driver_result.stdout.strip() or "?",
                "vendor": vendor.lower() or "?",
                "device": device.lower() or "?",
                "pci": pci,
                "numa_node": numa if numa not in ("", "-1") else "?",
                "solarflare": vendor.lower() == SOLARFLARE_VENDOR,
            }
        )
    return nics
