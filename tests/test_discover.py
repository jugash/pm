import unittest

from perfbench.capture.discover import discover_nics
from perfbench.runner.base import ExecResult, FakeExecutor


def _ok(stdout):
    return ExecResult(command="", exit_code=0, stdout=stdout)


class TestDiscoverNics(unittest.TestCase):
    def test_discovery_tags_solarflare(self):
        listing = (
            "ens1f0|0x1924|0x0b03|0000:3b:00.0|0\n"
            "ens1f1|0x1924|0x0b03|0000:3b:00.1|0\n"
            "eno1|0x8086|0x37d2|0000:18:00.0|0\n"
        )
        fake = FakeExecutor(
            responses={
                "/sys/class/net/*/device": _ok(listing),
                "ethtool -i ens1f0": _ok("sfc"),
                "ethtool -i ens1f1": _ok("sfc"),
                "ethtool -i eno1": _ok("i40e"),
            }
        )
        nics = discover_nics(fake)
        self.assertEqual(len(nics), 3)
        sf = [n for n in nics if n["solarflare"]]
        self.assertEqual({n["name"] for n in sf}, {"ens1f0", "ens1f1"})
        self.assertEqual(nics[0]["pci"], "0000:3b:00.0")
        self.assertEqual(nics[0]["driver"], "sfc")
        self.assertEqual(nics[2]["vendor"], "0x8086")
        self.assertFalse(nics[2]["solarflare"])

    def test_malformed_lines_skipped_and_unknown_numa(self):
        listing = "junk-line\nens1f0|0x1924|0x0b03|0000:3b:00.0|-1\n"
        fake = FakeExecutor(responses={"/sys/class/net/*/device": _ok(listing)})
        nics = discover_nics(fake)
        self.assertEqual(len(nics), 1)
        self.assertEqual(nics[0]["numa_node"], "?")
        self.assertEqual(nics[0]["driver"], "?")

    def test_empty_host(self):
        fake = FakeExecutor(responses={"/sys/class/net/*/device": _ok("")})
        self.assertEqual(discover_nics(fake), [])


if __name__ == "__main__":
    unittest.main()
