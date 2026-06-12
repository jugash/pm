# Tool adapters

Every adapter builds pinned (and, for network tools on `network_path:
onload`, Onload-wrapped) command lines and parses stdout into normalized
measurements. Time is always nanoseconds; latency distributions use the
`quantile` label (`0`=min, `0.5`=median, …, `1`=max).

Install sources: `iperf3`, `netperf`, `sockperf`, `rt-tests` (cyclictest)
from distro packages; `sfnt-pingpong` and `sfnt-stream` from
[cns-sfnettest](https://github.com/Xilinx-CNS/cns-sfnettest); `sysjitter`
from [Xilinx-CNS/cns-sysjitter](https://github.com/Xilinx-CNS/cns-sysjitter);
`eflatency` ships with OpenOnload. `deploy/docker/Dockerfile.bench` builds
all of them.

## sfnt-pingpong — kernel vs Onload socket RTT

The canonical A/B tool: the identical binary runs over the kernel stack and
under `onload`, sweeping message sizes in one invocation.

| param | default | meaning |
|---|---|---|
| `protocol` | tcp | tcp / udp |
| `msg_sizes` | [32,64,256,1024] | swept via `--sizes` |
| `duration_s` | 10 | per size (`--maxms`) |
| `spin` | true | `--spin` busy-wait (always pair with pinned, isolated cores) |

Emits per `msg_size`: `latency_ns` (q 0/0.5/0.99/1), `latency_mean_ns`,
`latency_stddev_ns`, `iterations_count`. The `%ile` column is sfnt's default
99th percentile.

## sockperf — percentiles under load

| param | default | meaning |
|---|---|---|
| `mode` | ping-pong | or `under-load` |
| `mps` | 100000 | messages/s in under-load mode |
| `msg_size` | 64 | one size per invocation |
| `duration_s` | 10 | |
| `port` | 11111 | |

Emits `latency_ns` for the full percentile ladder sockperf prints
(p25…p99.999 plus min/max), `latency_mean_ns`, `messages_dropped_count`,
`messages_duplicated_count`. Labels: `msg_size`, `mode`. Run several
scenarios at increasing `mps` to map the latency/throughput knee.

**Multicast:** when the scenario carries a `multicast:` block, `-i` becomes
the group address on both sides and `params.protocol` must be `udp` (the
adapter rejects tcp). `multicast.group_count > 1` generates a sockperf feed
file (`U:group:port` per line, path via `feed_file` param) instead of
`-i/-p`. `multicast.receivers > 1` starts that many `sockperf server`
processes joined to the same group, each pinned round-robin to one core
from `cpu.server_cores`; the client measures RTT to the *first* reflected
reply and counts the rest in `messages_duplicated_count` — expect roughly
`(receivers - 1) × messages` duplicates when fan-out delivery is healthy.
`mc_rx_if` / `mc_tx_if` params (interface **IP**, not name) pass through to
`--mc-rx-if` / `--mc-tx-if` for hosts with multiple routes to the group;
the client always sends `--mc-ttl <multicast.ttl>`.

## sfnt-stream — multicast streaming latency vs rate

The canonical Onload multicast benchmark: one-way streaming latency swept
across message rates in a single invocation. With a `multicast:` block the
client adds `--mcast=<group> --mcastintf=<interface>`; without one it runs
unicast UDP.

| param | default | meaning |
|---|---|---|
| `protocol` | udp | must be udp with multicast |
| `msg_size` | 64 | one size per invocation |
| `rates` | [10000, 1000000, 100000] | `[min, max, step]` mps → `--rates=min-max+step` |
| `duration_s` | 10 | per rate step (`--maxms`) |
| `spin` | true | `--spin` busy-wait |

Emits per achieved rate (label `mps`): `latency_ns` (q 0/0.5/0.99/1),
`latency_mean_ns`, `latency_stddev_ns`, and `messages_dropped_count`
(send − recv) when both columns are present.

**Caveat:** like eflatency, sfnt-stream's output has varied across
sfnettest releases. The parser is header-driven (reads the `#` column-name
comment, needs at least `mean` and `median`); run it once by hand and check
`results/raw/<run>-sfnt-stream.out` parses for your version.

## netperf — TCP_RR/UDP_RR cross-check

Invoked with omni output selectors so parsing is unambiguous CSV
(`MIN/MEAN/P50/P90/P99/MAX_LATENCY`, `TRANSACTION_RATE`). Use it to sanity-
check sfnt/sockperf agreement; if the three disagree materially, suspect the
environment before the tools.

## iperf3 — throughput sanity

JSON output (`-J`). TCP: sent/received bps, retransmits, host/remote CPU.
UDP (`protocol: udp`): bps, `udp_jitter_ns`, `udp_loss_percent`. Not a
latency tool — it answers "is the pipe healthy and is bonding actually
spreading load".

## sysjitter — OS jitter on isolated cores (client-only)

Spins a thread per core and records every interruption above
`threshold_ns` (default 200). The ground truth for "is my isolation
working". Emits per `core`: `interruption_ns` (q 0/0.5/0.9/0.99/0.999/
0.9999/1), `interruption_mean_ns`, `interruptions_count`,
`interruptions_per_sec`, `interrupted_percent`. On a properly isolated
core expect single-digit interruptions/s and p99.9 well under 10 µs.

## cyclictest — scheduler wakeup latency (client-only, optional)

`-q -m --priority=99`, one thread per scenario client core, `-a` affinity.
Emits per `thread`: `wakeup_latency_ns` (q 0/1) and `wakeup_latency_mean_ns`.
Values are µs in cyclictest output, normalized to ns.

**Optional:** rt-tests needs `numactl-devel` and git.kernel.org (or an
entitled `dnf install rt-tests`), which airgapped environments often can't
reach. The bench image builds it best-effort and the shipped scenarios
don't include it; sysjitter (single C file, no dependencies) is the
primary OS-jitter gate. Add cyclictest back to scenarios where the binary
exists — it cross-checks sysjitter from the scheduler's point of view.

## eflatency — raw ef_vi floor (efvi scenarios only)

Measures layer-2 RTT below the sockets API: the absolute floor for the NIC +
PCIe + core combination, typically ~1 µs below Onload sockets. Server side
runs `eflatency pong <intf>`, client `eflatency ping <intf>`.

**Caveat:** eflatency's output format has varied across Onload releases; the
parser accepts `key: value ns` and `key=value` forms for
min/mean/median/max/99%/99.9%. Run it once by hand and check
`results/raw/<run>-eflatency.out` parses as expected for your version.

## Adding a tool

1. New file in `src/perfbench/tools/`, subclass `ToolAdapter`, decorate with
   `@register`, implement `server_command` (return `None` for client-only
   tools), `client_command`, `parse`.
2. Set `uses_network = False` for CPU-only tools (skips Onload wrapping).
3. Import it from `tools/__init__.py`.
4. Add canned output to `tests/helpers.py` and a test class mirroring
   `tests/test_tools.py`. Parsers must raise `ParseError` on unrecognized
   output — silence is how wrong numbers get published.
