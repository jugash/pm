"""Environment snapshot.

Every run record embeds a snapshot of the host environment, because a
latency number without its kernel cmdline, NIC firmware and tuning context
is not comparable to anything. Failures to collect an item are recorded
in-band (``<error: ...>``) rather than aborting the run.
"""

from __future__ import annotations

from datetime import datetime, timezone
from typing import Any, Sequence

from perfbench.runner.base import Executor

HOST_COMMANDS: dict[str, str] = {
    "hostname": "hostname",
    "kernel": "uname -r",
    "cmdline": "cat /proc/cmdline",
    "cpu_model": "grep -m1 'model name' /proc/cpuinfo | cut -d: -f2",
    "governor": "cat /sys/devices/system/cpu/cpu*/cpufreq/scaling_governor 2>/dev/null | sort -u",
    "smt": "cat /sys/devices/system/cpu/smt/control 2>/dev/null",
    "isolated_cpus": "cat /sys/devices/system/cpu/isolated",
    "nohz_full": "cat /sys/devices/system/cpu/nohz_full 2>/dev/null",
    "thp": "cat /sys/kernel/mm/transparent_hugepage/enabled",
    "tuned_profile": "tuned-adm active 2>/dev/null",
    "irqbalance": "pgrep -x irqbalance >/dev/null && echo running || echo not-running",
    "onload_version": "onload --version 2>&1 | head -n1",
    "busy_poll": "sysctl -n net.core.busy_poll 2>/dev/null",
    "busy_read": "sysctl -n net.core.busy_read 2>/dev/null",
}

INTERFACE_COMMANDS: dict[str, str] = {
    "driver_info": "ethtool -i {iface}",
    "ring": "ethtool -g {iface} 2>/dev/null",
    "coalesce": "ethtool -c {iface} 2>/dev/null",
    "link": "ip -o link show {iface}",
}


def _capture(executor: Executor, command: str) -> str:
    try:
        result = executor.run(command, timeout=15)
    except Exception as exc:  # noqa: BLE001 - snapshots must never abort runs
        return f"<error: {exc}>"
    if not result.ok:
        return f"<error: rc={result.exit_code} {result.stderr.strip()}>"
    return result.stdout.strip()


def collect_environment(
    executor: Executor, interfaces: Sequence[str] = ()
) -> dict[str, Any]:
    """Collect a tuning-relevant snapshot from one target."""
    snapshot: dict[str, Any] = {
        "collected_at": datetime.now(timezone.utc).isoformat(),
        "target": executor.describe(),
    }
    for key, command in HOST_COMMANDS.items():
        snapshot[key] = _capture(executor, command)

    nics: dict[str, dict[str, str]] = {}
    for iface in interfaces:
        nics[iface] = {
            key: _capture(executor, command.format(iface=iface))
            for key, command in INTERFACE_COMMANDS.items()
        }
    snapshot["interfaces"] = nics
    return snapshot
