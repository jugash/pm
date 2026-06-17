"""Shared test fixtures: scenario documents and canned tool outputs."""

from __future__ import annotations

from perfbench.config.schema import Scenario

BASE_SCENARIO: dict = {
    "id": "bm-onload-test",
    "description": "fixture",
    "platform": "baremetal",
    "network_path": "onload",
    "nic": {
        "ports": [{"name": "ens1f0", "card": "x2522-a", "numa_node": 0}],
    },
    "cpu": {
        "client_cores": [2, 4],
        "server_cores": [2, 4],
        "irq_cores": [6],
        "numa_node": 0,
        "require_isolated": True,
    },
    "onload": {"profile": "latency", "env": {"EF_POLL_USEC": "100000"}},
    "tools": [{"name": "sockperf", "params": {"msg_size": 64}}],
    "repetitions": 1,
    "tags": ["fixture"],
}


def make_scenario(**overrides) -> Scenario:
    doc = {**BASE_SCENARIO}
    doc.update(overrides)
    return Scenario.from_dict(doc)


def kernel_scenario(**overrides) -> Scenario:
    doc = {**BASE_SCENARIO, "network_path": "kernel", "id": "bm-kernel-test"}
    doc.pop("onload")
    doc.update(overrides)
    return Scenario.from_dict(doc)


def k8s_scenario(**overrides) -> Scenario:
    """A device-plugin (platform: k8s) onload scenario.

    CPUs/Onload come from device plugins, so the cpu block uses ``count``
    rather than naming cores; commands reference $ISOLATED_CPUS / $ONLOAD_LIB
    at runtime.
    """
    doc = {
        **BASE_SCENARIO,
        "id": "k8s-onload-test",
        "platform": "k8s",
        "cpu": {"count": 2, "require_isolated": True},
    }
    doc.update(overrides)
    return Scenario.from_dict(doc)


def mcast_scenario(**overrides) -> Scenario:
    doc = {
        **BASE_SCENARIO,
        "id": "mc-onload-test",
        "multicast": {"group": "239.100.1.1", "port": 12000, "ttl": 1},
        "tools": [{"name": "sockperf", "params": {"protocol": "udp", "msg_size": 64}}],
    }
    doc.update(overrides)
    return Scenario.from_dict(doc)


SFNT_OUTPUT = """\
# version: sfnettest-1.5.0
# server LD_PRELOAD=libonload.so
#\tsize\tmean\tmin\tmedian\tmax\t%ile\tstddev\titer
\t32\t2467\t2310\t2461\t10325\t2811\t57\t1000000
\t64\t2502\t2350\t2498\t11876\t2853\t61\t1000000
"""

SOCKPERF_OUTPUT = """\
sockperf: == version #3.10 ==
sockperf: Summary: Latency is 2.341 usec
sockperf: Total 851 observations; each percentile contains 8.51 observations
sockperf: ---> <MAX> observation =   45.123
sockperf: ---> percentile 99.999 =   12.345
sockperf: ---> percentile 99.990 =    9.876
sockperf: ---> percentile 99.900 =    7.654
sockperf: ---> percentile 99.000 =    5.432
sockperf: ---> percentile 90.000 =    3.210
sockperf: ---> percentile 50.000 =    2.341
sockperf: ---> <MIN> observation =    1.987
sockperf: # dropped messages = 0, # duplicated messages = 0, # out-of-order messages = 0
"""

SFNT_STREAM_OUTPUT = """\
# version: sfnettest-1.5.0
# server LD_PRELOAD=libonload.so
#\tmps\tsend\trecv\tmean\tmin\tmedian\tmax\t%ile\tstddev
\t10000\t10000\t10000\t2500\t2300\t2480\t9000\t2900\t80
\t100000\t100000\t99998\t2600\t2310\t2550\t12000\t3100\t120
"""

NETPERF_OUTPUT = """\
OMNI Send|Recv TEST from 0.0.0.0 () port 0 AF_INET to 10.0.0.2 () port 0 AF_INET
MIN_LATENCY,MEAN_LATENCY,P50_LATENCY,P90_LATENCY,P99_LATENCY,MAX_LATENCY,TRANSACTION_RATE
5,7.25,7,8,12,150,137931.03
"""

IPERF3_TCP_OUTPUT = """\
{
  "end": {
    "sum_sent": {"bits_per_second": 9.41e9, "retransmits": 12},
    "sum_received": {"bits_per_second": 9.40e9},
    "cpu_utilization_percent": {"host_total": 35.5, "remote_total": 28.1}
  }
}
"""

IPERF3_UDP_OUTPUT = """\
{
  "end": {
    "sum": {"bits_per_second": 4.2e9, "jitter_ms": 0.012, "lost_percent": 0.001},
    "cpu_utilization_percent": {"host_total": 22.0}
  }
}
"""

SYSJITTER_OUTPUT = """\
core_i:                       2        4
threshold(ns):              200      200
cpu_mhz:                   3000     3000
runtime(ns):         9999999999 9999999999
runtime(s):               10.00    10.00
int_n:                       12        3
int_n_per_sec:              1.2      0.3
int_min(ns):                210      250
int_median(ns):             300      310
int_mean(ns):               350      320
int_90(ns):                 400      390
int_99(ns):                 800      500
int_999(ns):               1200      600
int_9999(ns):              2000      700
int_max(ns):               5000      900
int_total(ns):            42000     9600
int_total(%):             0.004    0.001
"""

CYCLICTEST_OUTPUT = """\
# /dev/cpu_dma_latency set to 0us
T: 0 ( 2613) P:99 I:1000 C: 100000 Min:      2 Act:    3 Avg:    3 Max:      27
T: 1 ( 2614) P:99 I:1500 C:  66672 Min:      2 Act:    4 Avg:    3 Max:      41
"""

EFLATENCY_OUTPUT = """\
# eflatency
# iterations: 100000
mean: 4123 ns
min: 3900 ns
median: 4100 ns
99%: 4500 ns
99.9%: 5200 ns
max: 9000 ns
"""
