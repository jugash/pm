"""SSH executor (paramiko).

paramiko is imported lazily so the rest of the framework works without the
``ssh`` extra installed. A ``client_factory`` hook allows full unit testing
without network access.
"""

from __future__ import annotations

import time
from typing import Callable, Mapping, Optional

from perfbench.errors import ExecutionError
from perfbench.runner.base import BackgroundProcess, ExecResult, Executor

TIMEOUT_EXIT_CODE = 124  # matches coreutils `timeout` and LocalExecutor


def _default_client_factory():  # pragma: no cover - requires paramiko + network
    try:
        import paramiko
    except ImportError as exc:
        raise ExecutionError(
            "paramiko is required for SSH execution; install perfbench[ssh]"
        ) from exc
    client = paramiko.SSHClient()
    client.set_missing_host_key_policy(paramiko.AutoAddPolicy())
    return client


def _wrap_env(command: str, env: Optional[Mapping[str, str]]) -> str:
    """Prefix command with env assignments (SSH servers often refuse setenv)."""
    if not env:
        return command
    assigns = " ".join(f"{k}={_shquote(str(v))}" for k, v in sorted(env.items()))
    return f"env {assigns} {command}"


def _shquote(value: str) -> str:
    if value and all(c.isalnum() or c in "._-=/:," for c in value):
        return value
    return "'" + value.replace("'", "'\\''") + "'"


class SSHBackground(BackgroundProcess):
    def __init__(self, command: str, stdout, stderr):
        self.command = command
        self._stdout = stdout
        self._stderr = stderr
        self._channel = stdout.channel
        self._started = time.monotonic()

    def running(self) -> bool:
        return not self._channel.exit_status_ready()

    def stop(self) -> ExecResult:
        if not self._channel.exit_status_ready():
            self._channel.close()
            exit_code = -1
        else:
            exit_code = self._channel.recv_exit_status()
        out = self._read(self._stdout)
        err = self._read(self._stderr)
        return ExecResult(
            command=self.command,
            exit_code=exit_code,
            stdout=out,
            stderr=err,
            duration_s=time.monotonic() - self._started,
        )

    @staticmethod
    def _read(stream) -> str:
        try:
            data = stream.read()
        except Exception:  # noqa: BLE001 - channel may already be closed
            return ""
        return data.decode() if isinstance(data, bytes) else str(data)


class SSHExecutor(Executor):
    def __init__(
        self,
        host: str,
        user: Optional[str] = None,
        port: int = 22,
        key_filename: Optional[str] = None,
        connect_timeout: float = 10.0,
        client_factory: Optional[Callable] = None,
    ):
        self.host = host
        self.user = user
        self.port = port
        self.key_filename = key_filename
        self.connect_timeout = connect_timeout
        self._client_factory = client_factory or _default_client_factory
        self._client = None

    def describe(self) -> str:
        prefix = f"{self.user}@" if self.user else ""
        return f"ssh:{prefix}{self.host}"

    def _connection(self):
        if self._client is None:
            client = self._client_factory()
            client.connect(
                self.host,
                port=self.port,
                username=self.user,
                key_filename=self.key_filename,
                timeout=self.connect_timeout,
            )
            self._client = client
        return self._client

    def run(self, command, timeout=None, env=None, input_data=None) -> ExecResult:
        if input_data is not None:
            raise ExecutionError("SSHExecutor does not support stdin input")
        full = _wrap_env(command, env)
        started = time.monotonic()
        _stdin, stdout, stderr = self._connection().exec_command(full, timeout=timeout)
        channel = stdout.channel

        # paramiko's exec_command timeout only bounds reads; recv_exit_status()
        # blocks forever if the remote command wedges. Enforce a real deadline.
        if timeout is not None:
            deadline = started + timeout
            while not channel.exit_status_ready():
                if time.monotonic() >= deadline:
                    channel.close()
                    return ExecResult(
                        command=full,
                        exit_code=TIMEOUT_EXIT_CODE,
                        stdout=SSHBackground._read(stdout),
                        stderr=f"timeout after {timeout}s",
                        duration_s=time.monotonic() - started,
                    )
                time.sleep(0.05)
        exit_code = channel.recv_exit_status()
        return ExecResult(
            command=full,
            exit_code=exit_code,
            stdout=SSHBackground._read(stdout),
            stderr=SSHBackground._read(stderr),
            duration_s=time.monotonic() - started,
        )

    def start(self, command, env=None) -> BackgroundProcess:
        full = _wrap_env(command, env)
        _stdin, stdout, stderr = self._connection().exec_command(full)
        return SSHBackground(full, stdout, stderr)

    def close(self) -> None:
        if self._client is not None:
            self._client.close()
            self._client = None
