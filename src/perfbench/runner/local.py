"""Local subprocess executor."""

from __future__ import annotations

import os
import signal
import subprocess
import time
from typing import Mapping, Optional

from perfbench.runner.base import BackgroundProcess, ExecResult, Executor

TIMEOUT_EXIT_CODE = 124  # matches coreutils `timeout`


class LocalBackground(BackgroundProcess):
    def __init__(self, command: str, proc: subprocess.Popen):
        self.command = command
        self._proc = proc
        self._started = time.monotonic()

    def running(self) -> bool:
        return self._proc.poll() is None

    def _signal_group(self, sig: int) -> None:
        """Signal the whole process group so children (the actual benchmark
        binary under sh/taskset/onload) die too and release the pipes."""
        try:
            os.killpg(os.getpgid(self._proc.pid), sig)
        except (ProcessLookupError, PermissionError):  # already gone
            pass

    def stop(self) -> ExecResult:
        if self._proc.poll() is None:
            self._signal_group(signal.SIGTERM)
            try:
                self._proc.wait(timeout=5)
            except subprocess.TimeoutExpired:  # pragma: no cover - defensive
                self._signal_group(signal.SIGKILL)
        stdout, stderr = self._proc.communicate()
        return ExecResult(
            command=self.command,
            exit_code=self._proc.returncode or 0,
            stdout=stdout or "",
            stderr=stderr or "",
            duration_s=time.monotonic() - self._started,
        )


class LocalExecutor(Executor):
    """Runs commands via ``/bin/sh -c`` on the local machine."""

    def describe(self) -> str:
        return "local"

    @staticmethod
    def _merged_env(env: Optional[Mapping[str, str]]) -> Optional[dict]:
        if not env:
            return None
        merged = dict(os.environ)
        merged.update(env)
        return merged

    def run(self, command, timeout=None, env=None, input_data=None) -> ExecResult:
        started = time.monotonic()
        try:
            proc = subprocess.run(
                ["/bin/sh", "-c", command],
                capture_output=True,
                text=True,
                timeout=timeout,
                env=self._merged_env(env),
                input=input_data,
            )
            return ExecResult(
                command=command,
                exit_code=proc.returncode,
                stdout=proc.stdout,
                stderr=proc.stderr,
                duration_s=time.monotonic() - started,
            )
        except subprocess.TimeoutExpired as exc:
            return ExecResult(
                command=command,
                exit_code=TIMEOUT_EXIT_CODE,
                stdout=(exc.stdout or b"").decode() if isinstance(exc.stdout, bytes) else (exc.stdout or ""),
                stderr=f"timeout after {timeout}s",
                duration_s=time.monotonic() - started,
            )

    def start(self, command, env=None) -> BackgroundProcess:
        proc = subprocess.Popen(
            ["/bin/sh", "-c", command],
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            env=self._merged_env(env),
            start_new_session=True,  # own process group; see _signal_group
        )
        return LocalBackground(command, proc)
