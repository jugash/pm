"""Preflight validation.

A latency benchmark on a misconfigured host produces numbers that look
plausible and are worthless. Preflight refuses to run scenarios when fatal
conditions are detected (irqbalance running, benchmark cores not isolated,
Onload missing for bypass scenarios) and warns on softer ones (governor,
SMT, THP).
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Callable, Iterable

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
    running = result.exit_code == 0
    return CheckResult(
        name="irqbalance",
        passed=not running,
        severity=SEV_FATAL,
        message="irqbalance is running and will migrate IRQs onto benchmark cores"
        if running
        else "irqbalance not running",
    )


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
    missing = sorted(needed - isolated)
    return CheckResult(
        name="cpu_isolation",
        passed=not missing,
        severity=SEV_FATAL,
        message=f"benchmark cores not isolated: {missing} (isolated={sorted(isolated)})"
        if missing
        else f"cores {sorted(needed)} are isolated",
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
    check_onload,
)


def run_preflight(
    executor: Executor, scenario: Scenario, role: str = "client"
) -> list[CheckResult]:
    return [check(executor, scenario, role) for check in ALL_CHECKS]


def fatal_failures(results: Iterable[CheckResult]) -> list[CheckResult]:
    return [r for r in results if not r.passed and r.severity == SEV_FATAL]
