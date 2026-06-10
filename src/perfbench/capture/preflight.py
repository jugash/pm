"""Preflight validation.

A latency benchmark on a misconfigured host produces numbers that look
plausible and are worthless. Preflight refuses to run scenarios when fatal
conditions are detected (irqbalance running, benchmark cores not isolated,
Onload missing for bypass scenarios) and warns on softer ones (governor,
SMT, THP).
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Any, Callable, Iterable, Optional

from perfbench.config.schema import NetworkPath, Scenario
from perfbench.runner.base import Executor

SEV_FATAL = "fatal"
SEV_WARNING = "warning"


@dataclass
class CheckResult:
    name: str
    passed: bool
    severity: str
    message: str

    def to_dict(self) -> dict[str, Any]:
        return {
            "name": self.name,
            "passed": self.passed,
            "severity": self.severity,
            "message": self.message,
        }


def parse_cpu_mask(mask: str) -> set[int]:
    """Parse a hex CPU mask, optionally comma-grouped: 'f', '00000000,00000003'."""
    mask = mask.strip().replace(",", "")
    if not mask:
        return set()
    value = int(mask, 16)
    cpus: set[int] = set()
    bit = 0
    while value:
        if value & 1:
            cpus.add(bit)
        value >>= 1
        bit += 1
    return cpus


def parse_cpu_list(text: str) -> set[int]:
    """Parse kernel cpu-list syntax: '' | '3' | '2-5,8,10-11'."""
    cpus: set[int] = set()
    text = text.strip()
    if not text:
        return cpus
    for chunk in text.split(","):
        chunk = chunk.strip()
        if not chunk:
            continue
        if "-" in chunk:
            lo, hi = chunk.split("-", 1)
            cpus.update(range(int(lo), int(hi) + 1))
        else:
            cpus.add(int(chunk))
    return cpus


def check_irqbalance(executor: Executor, scenario: Scenario, role: str) -> CheckResult:
    result = executor.run("pgrep -x irqbalance", timeout=10)
    if result.exit_code == 0:
        return CheckResult(
            name="irqbalance",
            passed=False,
            severity=SEV_FATAL,
            message="irqbalance is running and will migrate IRQs onto benchmark cores",
        )
    # Not found — but inside a container the PID namespace hides host
    # processes, so "not found" proves nothing. Detect that and downgrade to
    # an honest warning instead of a vacuous pass.
    pid1 = executor.run("cat /proc/1/comm", timeout=10).stdout.strip()
    if pid1 and pid1 not in ("systemd", "init"):
        return CheckResult(
            name="irqbalance",
            passed=False,
            severity=SEV_WARNING,
            message=f"cannot verify from container (pid1={pid1}); "
            "check irqbalance on the node itself",
        )
    return CheckResult(
        name="irqbalance", passed=True, severity=SEV_FATAL, message="irqbalance not running"
    )


def _tuned_isolated_cpus(executor: Executor) -> Optional[set[int]]:
    """Isolated CPUs implied by tuned cpu-partitioning, if configured.

    The cpu-partitioning profile does not use ``isolcpus=`` (so
    /sys/devices/system/cpu/isolated is empty); instead the kernel cmdline
    carries ``tuned.non_isolcpus=<hexmask>`` naming the *housekeeping* CPUs
    — every other present CPU is isolated (via systemd CPUAffinity + tuned).
    """
    cmdline = executor.run("cat /proc/cmdline", timeout=10)
    if not cmdline.ok:
        return None
    match = re.search(r"\btuned\.non_isolcpus=([0-9a-fA-F,]+)", cmdline.stdout)
    if not match:
        return None
    present = executor.run("cat /sys/devices/system/cpu/present", timeout=10)
    if not present.ok:
        return None
    housekeeping = parse_cpu_mask(match.group(1))
    return parse_cpu_list(present.stdout) - housekeeping


def check_isolation(executor: Executor, scenario: Scenario, role: str) -> CheckResult:
    needed = set(scenario.cpu.cores_for_role(role))
    if not scenario.cpu.require_isolated:
        return CheckResult("cpu_isolation", True, SEV_WARNING, "isolation check disabled")
    result = executor.run("cat /sys/devices/system/cpu/isolated", timeout=10)
    if not result.ok:
        return CheckResult(
            "cpu_isolation", False, SEV_FATAL, "cannot read isolated cpu list"
        )
    isolated = parse_cpu_list(result.stdout)
    source = "isolcpus"
    missing = needed - isolated

    if missing:
        # fall back to tuned cpu-partitioning (tuned.non_isolcpus=<mask>)
        tuned_isolated = _tuned_isolated_cpus(executor)
        if tuned_isolated is not None:
            isolated |= tuned_isolated
            missing = needed - isolated
            source = "isolcpus + tuned.non_isolcpus"

    missing_sorted = sorted(missing)
    return CheckResult(
        name="cpu_isolation",
        passed=not missing_sorted,
        severity=SEV_FATAL,
        message=(
            f"benchmark cores not isolated: {missing_sorted} "
            f"(isolated={sorted(isolated)}, source={source})"
        )
        if missing_sorted
        else f"cores {sorted(needed)} are isolated (source: {source})",
    )


def check_governor(executor: Executor, scenario: Scenario, role: str) -> CheckResult:
    result = executor.run(
        "cat /sys/devices/system/cpu/cpu*/cpufreq/scaling_governor 2>/dev/null | sort -u",
        timeout=10,
    )
    governors = {g for g in result.stdout.split() if g}
    ok = governors == {"performance"}
    return CheckResult(
        name="cpu_governor",
        passed=ok,
        severity=SEV_WARNING,
        message=f"governors in use: {sorted(governors) or ['unknown']}",
    )


def check_smt(executor: Executor, scenario: Scenario, role: str) -> CheckResult:
    result = executor.run("cat /sys/devices/system/cpu/smt/control 2>/dev/null", timeout=10)
    state = result.stdout.strip() or "unknown"
    ok = state in ("off", "forceoff", "notsupported")
    return CheckResult(
        name="smt",
        passed=ok,
        severity=SEV_WARNING,
        message=f"SMT control: {state}",
    )


def check_thp(executor: Executor, scenario: Scenario, role: str) -> CheckResult:
    result = executor.run("cat /sys/kernel/mm/transparent_hugepage/enabled", timeout=10)
    ok = "[never]" in result.stdout
    return CheckResult(
        name="transparent_hugepages",
        passed=ok,
        severity=SEV_WARNING,
        message=f"THP: {result.stdout.strip() or 'unknown'}",
    )


def check_nic_driver(executor: Executor, scenario: Scenario, role: str) -> CheckResult:
    iface = scenario.nic.ports[0].name
    if not iface:
        return CheckResult(
            "nic_driver", False, SEV_FATAL,
            "port has no interface name — NIC resolution did not run "
            "(resolve_nic must be applied before preflight)",
        )
    result = executor.run(f"ethtool -i {iface}", timeout=10)
    if not result.ok:
        return CheckResult(
            "nic_driver", False, SEV_FATAL, f"ethtool -i {iface} failed: {result.stderr.strip()}"
        )
    driver = ""
    for line in result.stdout.splitlines():
        if line.startswith("driver:"):
            driver = line.split(":", 1)[1].strip()
    return CheckResult(
        name="nic_driver",
        passed=bool(driver),
        severity=SEV_FATAL,
        message=f"{iface} driver: {driver or 'unknown'}",
    )


SOLARFLARE_VENDOR = "0x1924"


def check_nic_identity(executor: Executor, scenario: Scenario, role: str) -> CheckResult:
    """Verify the NIC by PCI identity, not interface name.

    Interface names differ between machines (ens1f0 vs enp59s0f0) and can
    drift across BIOS/firmware changes; this check confirms that the named
    interface is actually the silicon the scenario thinks it is:

    * PCI vendor id from ``/sys/class/net/<iface>/device/vendor`` must match
      ``nic.vendor`` (defaults to Solarflare 0x1924 for bypass scenarios —
      Onload/ef_vi require Solarflare silicon);
    * when ``ports[].pci`` is declared, the interface's actual PCI address
      (sysfs device symlink) must match — catching name drift where ens1f0
      now points at a different slot.
    """
    expected_vendor = scenario.nic.vendor
    if expected_vendor is None and scenario.network_path is not NetworkPath.KERNEL:
        expected_vendor = SOLARFLARE_VENDOR

    problems: list[str] = []
    seen: list[str] = []
    for i, port in enumerate(scenario.nic.ports):
        if not port.name:
            problems.append(f"ports[{i}]: no interface name (NIC resolution did not run)")
            continue
        vendor_result = executor.run(
            f"cat /sys/class/net/{port.name}/device/vendor", timeout=10
        )
        if not vendor_result.ok:
            problems.append(f"{port.name}: cannot read PCI vendor (no such interface?)")
            continue
        vendor = vendor_result.stdout.strip().lower()
        seen.append(f"{port.name}={vendor}")
        if expected_vendor and vendor != expected_vendor:
            problems.append(
                f"{port.name}: PCI vendor {vendor}, expected {expected_vendor}"
                + (" (Solarflare)" if expected_vendor == SOLARFLARE_VENDOR else "")
            )
        if port.pci:
            pci_result = executor.run(
                f"basename $(readlink -f /sys/class/net/{port.name}/device)", timeout=10
            )
            actual = pci_result.stdout.strip().lower()
            if pci_result.ok and actual and actual != port.pci.lower():
                problems.append(
                    f"{port.name}: at PCI {actual}, scenario declares {port.pci} "
                    "(interface name drift?)"
                )
    if problems:
        return CheckResult("nic_identity", False, SEV_FATAL, "; ".join(problems)[:400])
    return CheckResult(
        name="nic_identity",
        passed=True,
        severity=SEV_FATAL,
        message="PCI identity verified: " + ", ".join(seen),
    )


def check_onload(executor: Executor, scenario: Scenario, role: str) -> CheckResult:
    if scenario.network_path is NetworkPath.KERNEL:
        return CheckResult("onload", True, SEV_WARNING, "not required (kernel path)")
    result = executor.run("onload --version", timeout=10)
    return CheckResult(
        name="onload",
        passed=result.ok,
        severity=SEV_FATAL,
        message=result.stdout.splitlines()[0].strip()
        if result.ok and result.stdout
        else "onload not available on target",
    )


ALL_CHECKS: tuple[Callable[..., CheckResult], ...] = (
    check_irqbalance,
    check_isolation,
    check_governor,
    check_smt,
    check_thp,
    check_nic_driver,
    check_nic_identity,
    check_onload,
)


def run_preflight(
    executor: Executor, scenario: Scenario, role: str = "client"
) -> list[CheckResult]:
    return [check(executor, scenario, role) for check in ALL_CHECKS]


def fatal_failures(results: Iterable[CheckResult]) -> list[CheckResult]:
    return [r for r in results if not r.passed and r.severity == SEV_FATAL]
