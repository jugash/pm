# Benchmark methodology

How PerfBench is meant to be used so its numbers can be trusted, and the
reasoning behind the defaults.

## Percentiles, never averages

Trading systems live and die at the tail. A path with a 2.4 µs mean and a
quiet p99.9 beats one with a 2.1 µs mean and 40 µs excursions. PerfBench
therefore records full quantile ladders (`quantile` label, `0`…`1`) and
derives `latency_jitter_ns` = p99.9 − p50 per series — the "how much worse is
the tail than typical" number. Means are kept (`latency_mean_ns`) but should
never be the comparison basis.

## Idle RTT lies: measure under load

Ping-pong benchmarks (sfnt-pingpong, netperf RR) measure the unloaded
round-trip — useful as a floor and for A/B-ing the stack, but production
latency happens while messages queue behind each other. That's why the
default scenarios pair sfnt-pingpong with `sockperf under-load --mps …`:
percentiles at a controlled message rate. Sweep `mps` upward across scenarios
to find where the tail starts to fold.

## Repetitions and stability

Every scenario runs `repetitions` times (default 3–5, separate `RunRecord`
per rep). Compare medians across reps; use `stats.Aggregate.spread_pct`
(worst−best as % of median) as the stability signal. A spread above ~5% on
p99 means the environment isn't controlled — investigate before believing
either number. Typical causes: leftover irqbalance, SMT siblings, thermal
throttling, a noisy neighbour VM on the same switch.

## Duration and warmup

Defaults are 10 s ping-pong per message size and 30 s under-load — long
enough to accumulate meaningful p99.9 sample counts at 100k msg/s, short
enough to keep matrices tractable. The first seconds include Onload stack
setup, ARP, route cache warming and CPU frequency settling; sockperf and
sfnt-pingpong handle warmup internally, but if you shorten durations below
~5 s, expect tail pollution.

For p99.999 you need ≥ a few million observations: at 100k msg/s that's ≥
60 s. Scale `duration_s` with the percentile you care about.

## One variable at a time

The matrix mechanism makes it cheap to vary several axes at once; resist
doing so in one matrix. The shipped scenario files isolate axes deliberately:

- `baremetal-kernel-vs-onload.yaml` — network path only, then Onload
  spin-vs-interrupt only;
- `bonding-matrix.yaml` — bond layout only, on a fixed Onload config;
- `k8s-vs-baremetal.yaml` — platform only, identical NIC/CPU/Onload settings;
- `multicast-matrix.yaml` — one multicast dimension per matrix (path, rate,
  group count, receiver fan-out, bond), everything else held fixed.

If two scenarios differ in more than the axis under test, the comparison is
invalid — and the embedded env snapshots will show it.

## Environment capture and preflight (non-negotiable)

Every record embeds both hosts' kernel cmdline, isolated/nohz CPU lists,
governor, SMT, THP, tuned profile, NIC driver/firmware (`ethtool -i`), ring
and coalescing settings, Onload version and busy-poll sysctls. Preflight
*refuses to run* when irqbalance is active, the benchmark cores aren't in
`isolcpus`, or Onload is missing for a bypass scenario. Soft misconfigurations
(governor ≠ performance, SMT on, THP on) warn but proceed — they're recorded
in the run's preflight section either way.

Override with `--skip-preflight` only for smoke tests; never for numbers you
intend to keep.

## Comparing bare metal and Kubernetes

The k8s scenarios are valid comparisons only when the pod gets the same
substrate as the bare-metal run: SR-IOV VF on the same physical port, static
CPU Manager with exclusive cores from the same isolated set and NUMA node,
hugepages, and the same Onload userland as the host kernel module. See
docs/kubernetes.md. The interesting result is the *delta* between platforms,
which should be near zero when containerization is configured correctly; a
gap is a configuration finding, not a container tax.

## Bonding caveats

Onload accelerates bonded interfaces with mode-dependent constraints
(active-backup vs 802.3ad behave differently, and acceleration can silently
fall back to the kernel path if the bond configuration isn't supported —
check `onload_stackdump` during a run to confirm acceleration). Steady-state
bond numbers are what the matrix measures; **failover latency** (pull a
cable, fail the active port) is a separate, manual measurement in cut one:
run `sockperf under-load` for 120 s, fail the link at t=60, and read the
excursion off the raw output kept in `results/raw/`.

## Things this harness deliberately does not do (cut one)

- Application-level benchmarks (order gateways, market data handlers).
- Switch/fabric measurement — host-to-host numbers include exactly one
  switch hop unless your cabling says otherwise; record the topology in the
  scenario `description`.
- Hardware timestamping / packet capture cross-validation (solar_capture,
  hardware timestamping NICs). Worth adding when absolute (not comparative)
  numbers matter.
- Automated failover orchestration.
