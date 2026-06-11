"""Scenario orchestration.

Drives one scenario end-to-end against a client executor and a server
executor (local, SSH or pod — the orchestrator does not care):

1. preflight validation on both targets (fatal failures abort),
2. environment snapshot of both targets,
3. per repetition, per tool: start server side, settle, run client side,
   stop server, parse output into normalized measurements,
4. persist a full-fidelity :class:`RunRecord` per repetition.

Tool failures are recorded in the run record (exit code + error) and do not
abort the remaining tools/repetitions — partial data with explicit failure
markers beats losing a 2-hour matrix to one flaky run.
"""

from __future__ import annotations

import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Callable, Optional, Sequence

from perfbench.capture.envsnapshot import collect_environment
from perfbench.capture.preflight import fatal_failures, run_preflight
from perfbench.capture.resolve import resolve_nic
from perfbench.config.schema import Scenario
from perfbench.errors import ParseError, PreflightError
from perfbench.results.models import Measurement, RunRecord, ToolRun, new_run_id, utc_now_iso
from perfbench.results.store import ResultStore
from perfbench.stats import derive_jitter
from perfbench.runner.base import Executor
from perfbench.tools.base import ToolAdapter, create_tool


@dataclass
class ToolPlan:
    """Resolved commands for one tool within a scenario."""

    adapter: ToolAdapter
    server_command: Optional[str]
    client_command: str
    timeout_s: float


@dataclass
class Orchestrator:
    client: Executor
    server: Executor
    server_address: str
    store: Optional[ResultStore] = None
    output_dir: Optional[Path] = None
    settle_s: float = 1.0
    sleep: Callable[[float], None] = time.sleep
    on_event: Callable[[str], None] = lambda msg: None
    extra_env_interfaces: Sequence[str] = field(default_factory=tuple)

    # -- planning ----------------------------------------------------------

    def plan(
        self, scenario: Scenario, server_scenario: Optional[Scenario] = None
    ) -> list[ToolPlan]:
        """Resolve commands; client and server may carry differently-resolved
        NIC names (interface names differ between machines)."""
        server_scenario = server_scenario or scenario
        plans = []
        for spec in scenario.tools:
            adapter = create_tool(spec)
            plans.append(
                ToolPlan(
                    adapter=adapter,
                    server_command=adapter.server_command(server_scenario),
                    client_command=adapter.client_command(scenario, self.server_address),
                    timeout_s=adapter.timeout_s(),
                )
            )
        return plans

    # -- execution ---------------------------------------------------------

    def run_scenario(
        self,
        scenario: Scenario,
        skip_preflight: bool = False,
        extra_preflight: Optional[list[dict]] = None,
    ) -> list[RunRecord]:
        """``extra_preflight``: pre-computed check dicts (e.g. node-level
        checks from a k8s debug pod) recorded alongside in-target checks."""
        # interface names are per-host facts: resolve them independently on
        # each side before anything that uses them (preflight, env capture,
        # command construction)
        if not scenario.nic.resolved:
            self.on_event(f"[{scenario.id}] resolving NIC names")
            client_sc = resolve_nic(self.client, scenario)
            server_sc = resolve_nic(self.server, scenario)
            self.on_event(
                f"[{scenario.id}] resolved client={[p.name for p in client_sc.nic.ports]} "
                f"server={[p.name for p in server_sc.nic.ports]}"
            )
        else:
            client_sc = server_sc = scenario

        preflight_results = list(extra_preflight or [])
        if skip_preflight:
            self.on_event(f"[{scenario.id}] preflight skipped")
        else:
            self.on_event(f"[{scenario.id}] running preflight")
            client_checks = run_preflight(self.client, client_sc, role="client")
            server_checks = run_preflight(self.server, server_sc, role="server")
            preflight_results += [
                {"target": "client", **c.to_dict()} for c in client_checks
            ] + [{"target": "server", **c.to_dict()} for c in server_checks]
            fatals = fatal_failures(client_checks) + fatal_failures(server_checks)
            if fatals:
                raise PreflightError(fatals)

        def _interfaces(sc: Scenario) -> list[str]:
            names = [p.name for p in sc.nic.ports if p.name]
            if sc.nic.bond_name:
                names.append(sc.nic.bond_name)
            names.extend(self.extra_env_interfaces)
            return names

        self.on_event(f"[{scenario.id}] capturing environment")
        env = {
            "client": collect_environment(self.client, _interfaces(client_sc)),
            "server": collect_environment(self.server, _interfaces(server_sc)),
        }

        plans = self.plan(client_sc, server_scenario=server_sc)
        records: list[RunRecord] = []
        for rep in range(scenario.repetitions):
            record = RunRecord(
                run_id=new_run_id(),
                scenario_id=scenario.id,
                rep=rep,
                labels=scenario.labels(),
                env=env,
                preflight=preflight_results,
            )
            for plan in plans:
                self.on_event(
                    f"[{scenario.id}] rep {rep + 1}/{scenario.repetitions} "
                    f"tool {plan.adapter.name}"
                )
                record.tool_runs.append(self._run_tool(client_sc, plan, record.run_id))
            record.finished_at = utc_now_iso()
            if self.store is not None:
                self.store.save_run(record)
            records.append(record)
        return records

    def _run_tool(self, scenario: Scenario, plan: ToolPlan, run_id: str) -> ToolRun:
        tool_run = ToolRun(
            tool=plan.adapter.name,
            client_command=plan.client_command,
            server_command=plan.server_command,
        )
        background = None
        if plan.server_command:
            background = self.server.start(plan.server_command)
            self.sleep(self.settle_s)
            if not background.running():
                # server died during settle (port in use, missing binary,
                # onload failure...) — fail with ITS error, not the client's
                # confusing "connection refused"
                server_result = background.stop()
                detail = (server_result.stderr or server_result.stdout).strip()[:300]
                tool_run.exit_code = server_result.exit_code or 1
                tool_run.error = (
                    f"server exited before client start "
                    f"(rc={server_result.exit_code}): {detail}"
                )
                self.on_event(f"[{scenario.id}] {plan.adapter.name} FAILED: {tool_run.error}")
                return tool_run

        result = self.client.run(plan.client_command, timeout=plan.timeout_s)
        tool_run.exit_code = result.exit_code
        tool_run.duration_s = result.duration_s

        if background is not None:
            server_result = background.stop()
            if not result.ok and server_result.stderr:
                tool_run.error = (
                    f"client rc={result.exit_code}; server stderr: "
                    f"{server_result.stderr.strip()[:500]}"
                )

        self._save_raw(run_id, plan.adapter.name, result.stdout, result.stderr)

        if not result.ok:
            tool_run.error = tool_run.error or (
                f"client rc={result.exit_code}: {result.stderr.strip()[:500]}"
            )
            self.on_event(f"[{scenario.id}] {plan.adapter.name} FAILED: {tool_run.error}")
            return tool_run

        try:
            measurements = plan.adapter.parse(result.stdout)
        except ParseError as exc:
            tool_run.error = f"parse error: {exc}"
            self.on_event(f"[{scenario.id}] {plan.adapter.name} PARSE ERROR: {exc}")
            return tool_run

        measurements.extend(derive_jitter(measurements))
        tool_run.measurements = self._stamp(measurements, scenario)
        return tool_run

    def _save_raw(self, run_id: str, tool: str, stdout: str, stderr: str) -> None:
        if self.output_dir is None:
            return
        self.output_dir.mkdir(parents=True, exist_ok=True)
        (self.output_dir / f"{run_id}-{tool}.out").write_text(stdout)
        if stderr:
            (self.output_dir / f"{run_id}-{tool}.err").write_text(stderr)

    @staticmethod
    def _stamp(measurements: list[Measurement], scenario: Scenario) -> list[Measurement]:
        """Attach scenario protocol-agnostic context labels where missing."""
        for m in measurements:
            m.labels.setdefault("interface", scenario.nic.interface)
        return measurements
