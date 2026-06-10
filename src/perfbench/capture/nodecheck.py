"""Host-level preflight checks, run from a privileged hostPID debug pod.

In-pod preflight (``capture.preflight``) sees the node's sysfs but not its
processes or IRQ layout. These checks run inside a node-check pod rendered
by :func:`perfbench.runner.k8s.render_node_check_pod` (privileged,
``hostPID: true``, host root mounted at ``/host``), so they verify the
actual node:

* ``node_irqbalance`` — host PID namespace is visible: a real pgrep.
* ``nic_irq_affinity`` — the scenario NIC's IRQs must not be steered onto
  the benchmark cores (and should sit on ``irq_cores`` when specified).
* ``node_tuned`` — active tuned/PerformanceProfile on the node.
"""

from __future__ import annotations

from typing import Callable

from perfbench.capture.preflight import (
    SEV_FATAL,
    SEV_WARNING,
    CheckResult,
    parse_cpu_list,
)
from perfbench.config.schema import Scenario
from perfbench.runner.base import Executor


def check_node_irqbalance(executor: Executor, scenario: Scenario, role: str) -> CheckResult:
    result = executor.run("pgrep -x irqbalance", timeout=10)
    running = result.exit_code == 0
    return CheckResult(
        name="node_irqbalance",
        passed=not running,
        severity=SEV_FATAL,
        message="irqbalance is running on the node (hostPID-verified)"
        if running
        else "irqbalance not running on the node (hostPID-verified)",
    )


def check_node_tuned(executor: Executor, scenario: Scenario, role: str) -> CheckResult:
    result = executor.run("chroot /host tuned-adm active 2>/dev/null", timeout=15)
    profile = result.stdout.strip()
    if not result.ok or not profile:
        return CheckResult(
            name="node_tuned",
            passed=False,
            severity=SEV_WARNING,
            message="cannot read tuned profile on the node",
        )
    ok = "latency" in profile or "performance" in profile
    return CheckResult(
        name="node_tuned",
        passed=ok,
        severity=SEV_WARNING,
        message=profile.splitlines()[0],
    )


def check_nic_irq_affinity(executor: Executor, scenario: Scenario, role: str) -> CheckResult:
    bench_cores = set(scenario.cpu.cores_for_role(role))
    irq_cores = set(scenario.cpu.irq_cores)
    problems: list[str] = []
    found_any = False

    for port in scenario.nic.ports:
        result = executor.run(
            f"awk -F: '/{port.name}/ {{gsub(/ /, \"\", $1); print $1}}' /proc/interrupts",
            timeout=10,
        )
        irqs = [i for i in result.stdout.split() if i.isdigit()]
        if not irqs:
            problems.append(f"{port.name}: no IRQs found (VF naming differs on node?)")
            continue
        found_any = True
        for irq in irqs:
            aff = executor.run(f"cat /proc/irq/{irq}/smp_affinity_list", timeout=10)
            if not aff.ok:
                continue
            cpus = parse_cpu_list(aff.stdout)
            overlap = cpus & bench_cores
            if overlap:
                problems.append(
                    f"{port.name} irq {irq} affinity {sorted(cpus)} overlaps "
                    f"benchmark cores {sorted(overlap)}"
                )
            elif irq_cores and not cpus <= irq_cores:
                problems.append(
                    f"{port.name} irq {irq} affinity {sorted(cpus)} outside "
                    f"irq_cores {sorted(irq_cores)}"
                )

    overlapping = any("overlaps" in p for p in problems)
    if problems:
        return CheckResult(
            name="nic_irq_affinity",
            passed=False,
            # overlap with benchmark cores ruins the run -> fatal;
            # missing IRQs / loose steering -> warning
            severity=SEV_FATAL if overlapping else SEV_WARNING,
            message="; ".join(problems)[:400],
        )
    if not found_any:
        return CheckResult(
            name="nic_irq_affinity",
            passed=False,
            severity=SEV_WARNING,
            message="no NIC IRQs found for any scenario port",
        )
    return CheckResult(
        name="nic_irq_affinity",
        passed=True,
        severity=SEV_FATAL,
        message=f"NIC IRQs clear of benchmark cores {sorted(bench_cores)}",
    )


NODE_CHECKS: tuple[Callable[..., CheckResult], ...] = (
    check_node_irqbalance,
    check_nic_irq_affinity,
    check_node_tuned,
)


def run_node_checks(executor: Executor, scenario: Scenario, role: str) -> list[CheckResult]:
    return [check(executor, scenario, role) for check in NODE_CHECKS]
