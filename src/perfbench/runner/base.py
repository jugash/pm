"""Executor abstraction.

The orchestrator is transport-agnostic: it drives ``Executor`` objects which
may run commands locally, over SSH (bare metal hosts), or inside Kubernetes
pods (``kubectl exec``). This is what makes bare-metal vs container
comparisons share one code path.
"""

from __future__ import annotations

import abc
from dataclasses import dataclass, field
from typing import Mapping, Optional


@dataclass
class ExecResult:
    """Outcome of one command execution."""

    command: str
    exit_code: int
    stdout: str = ""
    stderr: str = ""
    duration_s: float = 0.0

    @property
    def ok(self) -> bool:
        return self.exit_code == 0


class BackgroundProcess(abc.ABC):
    """A long-running command (e.g. a benchmark server) started by an executor."""

    @abc.abstractmethod
    def stop(self) -> ExecResult:
        """Terminate the process and return its collected output."""

    @abc.abstractmethod
    def running(self) -> bool:
        """True while the process has not exited."""


class Executor(abc.ABC):
    """Runs shell commands on one target (host or pod)."""

    @abc.abstractmethod
    def describe(self) -> str:
        """Human-readable target description, e.g. ``ssh:host-a``."""

    @abc.abstractmethod
    def run(
        self,
        command: str,
        timeout: Optional[float] = None,
        env: Optional[Mapping[str, str]] = None,
        input_data: Optional[str] = None,
    ) -> ExecResult:
        """Run ``command`` to completion and capture output."""

    @abc.abstractmethod
    def start(
        self, command: str, env: Optional[Mapping[str, str]] = None
    ) -> BackgroundProcess:
        """Start ``command`` without waiting for completion."""


@dataclass
class FakeBackground(BackgroundProcess):
    """Deterministic background process used by tests and dry runs."""

    result: ExecResult
    _running: bool = True

    def stop(self) -> ExecResult:
        self._running = False
        return self.result

    def running(self) -> bool:
        return self._running


@dataclass
class FakeExecutor(Executor):
    """Scripted executor for tests and dry runs.

    ``responses`` maps a substring of the command to a canned ExecResult;
    the first match wins. Unmatched commands succeed with empty output.
    Every call is recorded in ``calls``.
    """

    name: str = "fake"
    responses: dict[str, ExecResult] = field(default_factory=dict)
    calls: list[str] = field(default_factory=list)

    def describe(self) -> str:
        return f"fake:{self.name}"

    def _lookup(self, command: str) -> ExecResult:
        for needle, result in self.responses.items():
            if needle in command:
                return ExecResult(
                    command=command,
                    exit_code=result.exit_code,
                    stdout=result.stdout,
                    stderr=result.stderr,
                    duration_s=result.duration_s,
                )
        return ExecResult(command=command, exit_code=0)

    def run(self, command, timeout=None, env=None, input_data=None) -> ExecResult:
        self.calls.append(command)
        return self._lookup(command)

    def start(self, command, env=None) -> BackgroundProcess:
        self.calls.append(f"START {command}")
        return FakeBackground(result=self._lookup(command))
