"""Local subprocess executor."""

from __future__ import annotations

import logging
import os
import signal
import subprocess
import time
from typing import Mapping, Optional

from perfbench.runner.base import BackgroundProcess, ExecResult, Executor

TIMEOUT_EXIT_CODE = 124  # matches coreutils `timeout`

# DEBUG logs the literal command line executed (the full `kubectl exec …` for
# pod targets). Enable with `perfbench … --verbose`.
log = logging.getLogger(__name__)


def _signal_group(proc: subprocess.Popen, sig: int) -> None:
    """Signal the whole process group so children (the actual benchmark
    binary under sh/taskset/onload) die too and release the pipes."""
    try:
        os.killpg(os.getpgid(proc.pid), sig)
    except (ProcessLookupError, PermissionError):  # already gone
        pass


class LocalBackground(BackgroundProcess):
    def __init__(self, command: str, proc: subprocess.Popen):
        self.command = command
        self._proc = proc
        self._started = time.monotonic()

    def running(self) -> bool:
        return self._proc.poll() is None

    def stop(self) -> ExecResult:
        if self._proc.poll() is None:
            _signal_group(self._proc, signal.SIGTERM)
            try:
                self._proc.wait(timeout=5)
            except subprocess.TimeoutExpired:  # pragma: no cover - defensive
                _signal_group(self._proc, signal.SIGKILL)
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
        # Popen + process group (not subprocess.run): on timeout the whole
        # group must die, otherwise orphaned children of /bin/sh keep the
        # output pipes open and the final communicate() blocks until the
        # orphan exits — a wedged benchmark would hang the harness.
        log.debug("run: %s", command)
        started = time.monotonic()
        proc = subprocess.Popen(
            ["/bin/sh", "-c", command],
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            stdin=subprocess.PIPE if input_data is not None else None,
            text=True,
            env=self._merged_env(env),
            start_new_session=True,
        )
        try:
            stdout, stderr = proc.communicate(input=input_data, timeout=timeout)
            exit_code = proc.returncode
        except subprocess.TimeoutExpired:
            _signal_group(proc, signal.SIGTERM)
            try:
                stdout, stderr = proc.communicate(timeout=5)
            except subprocess.TimeoutExpired:  # pragma: no cover - defensive
                _signal_group(proc, signal.SIGKILL)
                stdout, stderr = proc.communicate()
            return ExecResult(
                command=command,
                exit_code=TIMEOUT_EXIT_CODE,
                stdout=stdout or "",
                stderr=f"timeout after {timeout}s",
                duration_s=time.monotonic() - started,
            )
        return ExecResult(
            command=command,
            exit_code=exit_code,
            stdout=stdout,
            stderr=stderr,
            duration_s=time.monotonic() - started,
        )

    def start(self, command, env=None) -> BackgroundProcess:
        log.debug("start: %s", command)
        proc = subprocess.Popen(
            ["/bin/sh", "-c", command],
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            env=self._merged_env(env),
            start_new_session=True,  # own process group; see _signal_group
        )
        return LocalBackground(command, proc)
