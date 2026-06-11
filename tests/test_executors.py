import time
import unittest

from perfbench.errors import ExecutionError
from perfbench.runner.base import ExecResult, FakeExecutor
from perfbench.runner.local import TIMEOUT_EXIT_CODE, LocalExecutor
from perfbench.runner.ssh import SSHExecutor, _shquote, _wrap_env


class TestLocalExecutor(unittest.TestCase):
    def setUp(self):
        self.executor = LocalExecutor()

    def test_describe(self):
        self.assertEqual(self.executor.describe(), "local")

    def test_run_success(self):
        result = self.executor.run("echo hello")
        self.assertTrue(result.ok)
        self.assertEqual(result.stdout.strip(), "hello")
        self.assertGreater(result.duration_s, 0)

    def test_run_failure(self):
        result = self.executor.run("echo oops >&2; exit 3")
        self.assertFalse(result.ok)
        self.assertEqual(result.exit_code, 3)
        self.assertIn("oops", result.stderr)

    def test_run_env(self):
        result = self.executor.run("echo $PERFBENCH_TEST", env={"PERFBENCH_TEST": "42"})
        self.assertEqual(result.stdout.strip(), "42")

    def test_run_input(self):
        result = self.executor.run("cat", input_data="stdin-data")
        self.assertEqual(result.stdout, "stdin-data")

    def test_timeout(self):
        result = self.executor.run("sleep 5", timeout=0.2)
        self.assertEqual(result.exit_code, TIMEOUT_EXIT_CODE)
        self.assertIn("timeout", result.stderr)

    def test_timeout_kills_whole_process_group(self):
        # a wedged child holding the pipe must not hang the harness
        started = time.monotonic()
        result = self.executor.run("sleep 30 & echo started; sleep 30", timeout=0.3)
        self.assertLess(time.monotonic() - started, 8)
        self.assertEqual(result.exit_code, TIMEOUT_EXIT_CODE)
        self.assertIn("started", result.stdout)

    def test_background(self):
        bg = self.executor.start("echo started; sleep 30")
        time.sleep(0.2)
        self.assertTrue(bg.running())
        result = bg.stop()
        self.assertIn("started", result.stdout)
        self.assertFalse(bg.running())

    def test_background_already_exited(self):
        bg = self.executor.start("echo done")
        time.sleep(0.3)
        self.assertFalse(bg.running())
        result = bg.stop()
        self.assertEqual(result.stdout.strip(), "done")
        self.assertTrue(result.ok)


class TestFakeExecutor(unittest.TestCase):
    def test_scripted_responses(self):
        fake = FakeExecutor(
            responses={"pgrep": ExecResult(command="", exit_code=1)}
        )
        self.assertEqual(fake.run("pgrep -x irqbalance").exit_code, 1)
        self.assertTrue(fake.run("anything else").ok)
        bg = fake.start("server")
        self.assertTrue(bg.running())
        bg.stop()
        self.assertFalse(bg.running())
        self.assertEqual(fake.calls, ["pgrep -x irqbalance", "anything else", "START server"])


class _FakeChannel:
    def __init__(self, exit_code=0, exited=True):
        self._exit_code = exit_code
        self._exited = exited
        self.closed = False

    def exit_status_ready(self):
        return self._exited

    def recv_exit_status(self):
        return self._exit_code

    def close(self):
        self.closed = True
        self._exited = True


class _FakeStream:
    def __init__(self, data, channel):
        self._data = data
        self.channel = channel

    def read(self):
        return self._data


class _FakeSSHClient:
    def __init__(self, exit_code=0, stdout=b"out", stderr=b"err", exited=True):
        self.exit_code = exit_code
        self.stdout_data = stdout
        self.stderr_data = stderr
        self.exited = exited
        self.connected_to = None
        self.commands = []
        self.closed = False

    def connect(self, host, port=22, username=None, key_filename=None, timeout=None):
        self.connected_to = (host, port, username)

    def exec_command(self, command, timeout=None, environment=None):
        self.commands.append(command)
        channel = _FakeChannel(self.exit_code, self.exited)
        return (
            None,
            _FakeStream(self.stdout_data, channel),
            _FakeStream(self.stderr_data, channel),
        )

    def close(self):
        self.closed = True


class TestSSHExecutor(unittest.TestCase):
    def _executor(self, client):
        return SSHExecutor("host-a", user="bench", client_factory=lambda: client)

    def test_describe(self):
        self.assertEqual(self._executor(_FakeSSHClient()).describe(), "ssh:bench@host-a")
        self.assertEqual(SSHExecutor("h", client_factory=_FakeSSHClient).describe(), "ssh:h")

    def test_run(self):
        client = _FakeSSHClient(stdout=b"hello", stderr=b"")
        executor = self._executor(client)
        result = executor.run("echo hello")
        self.assertTrue(result.ok)
        self.assertEqual(result.stdout, "hello")
        self.assertEqual(client.connected_to, ("host-a", 22, "bench"))
        # connection reused
        executor.run("again")
        self.assertEqual(len(client.commands), 2)

    def test_run_env_wrapped(self):
        client = _FakeSSHClient()
        self._executor(client).run("cmd", env={"EF_POLL_USEC": "100000", "X": "a b"})
        self.assertEqual(client.commands[0], "env EF_POLL_USEC=100000 X='a b' cmd")

    def test_run_rejects_stdin(self):
        with self.assertRaises(ExecutionError):
            self._executor(_FakeSSHClient()).run("cmd", input_data="x")

    def test_run_nonzero(self):
        result = self._executor(_FakeSSHClient(exit_code=2)).run("bad")
        self.assertEqual(result.exit_code, 2)

    def test_run_timeout_deadline_enforced(self):
        from perfbench.runner.ssh import TIMEOUT_EXIT_CODE

        # remote never exits: recv_exit_status would block forever without
        # the deadline loop
        client = _FakeSSHClient(exited=False, stdout=b"partial")
        started = time.monotonic()
        result = self._executor(client).run("wedged-tool", timeout=0.2)
        self.assertLess(time.monotonic() - started, 5)
        self.assertEqual(result.exit_code, TIMEOUT_EXIT_CODE)
        self.assertEqual(result.stdout, "partial")
        self.assertIn("timeout", result.stderr)

    def test_background_stop_while_running(self):
        client = _FakeSSHClient(exited=False, stdout=b"partial")
        bg = self._executor(client).start("server")
        self.assertTrue(bg.running())
        result = bg.stop()
        self.assertEqual(result.exit_code, -1)  # killed before exit
        self.assertEqual(result.stdout, "partial")

    def test_background_stop_after_exit(self):
        client = _FakeSSHClient(exited=True, exit_code=0)
        bg = self._executor(client).start("server")
        self.assertFalse(bg.running())
        self.assertEqual(bg.stop().exit_code, 0)

    def test_close(self):
        client = _FakeSSHClient()
        executor = self._executor(client)
        executor.run("x")
        executor.close()
        self.assertTrue(client.closed)
        executor.close()  # idempotent

    def test_helpers(self):
        self.assertEqual(_shquote("simple-1.0"), "simple-1.0")
        self.assertEqual(_shquote("a b"), "'a b'")
        self.assertEqual(_shquote("it's"), "'it'\\''s'")
        self.assertEqual(_wrap_env("cmd", None), "cmd")


if __name__ == "__main__":
    unittest.main()
