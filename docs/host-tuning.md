# Host tuning for valid numbers

PerfBench's preflight enforces the fatal subset of this list and warns on
the rest; this page is the reference for setting hosts up. Apply identically
to both hosts of a pair.

## Kernel command line

```
isolcpus=nohz,domain,managed_irq,2-7 nohz_full=2-7 rcu_nocbs=2-7
processor.max_cstate=1 intel_idle.max_cstate=0 idle=poll
default_hugepagesz=2M hugepagesz=2M hugepages=1024
```

- `isolcpus` + `nohz_full` + `rcu_nocbs` on the benchmark cores
  (client/server/irq cores in your scenarios must come from this set —
  preflight checks `/sys/devices/system/cpu/isolated`).
- C-state limiting (or `idle=poll` for the most aggressive setup) removes
  wake-from-idle excursions; `cpupower idle-info` to verify.
- AMD hosts: the equivalent is `amd_pstate=passive` plus
  `cpupower idle-set -D 0`.

## CPU

```
cpupower frequency-set -g performance      # all cores, preflight warns otherwise
echo off > /sys/devices/system/cpu/smt/control    # or BIOS; preflight warns
```

Decide turbo policy once and keep it: turbo on gives better numbers with
worse repeatability. For comparisons, lock frequency (`cpupower frequency-set
-d X -u X`) and record it — it's in the env snapshot via the governor field.

Pick cores on the NIC's NUMA node (`cat /sys/class/net/ens1f0/device/numa_node`).
Cross-node PCIe adds ~100–300 ns and more variance.

## IRQs

```
systemctl stop irqbalance && systemctl disable irqbalance   # preflight: fatal
# steer NIC IRQs to the scenario's irq_cores, e.g. cores 2:
grep ens1f0 /proc/interrupts | cut -d: -f1 | while read i; do
  echo 4 > /proc/irq/$i/smp_affinity   # mask: core 2
done
```

For spinning Onload configs (EF_POLL_USEC high), IRQs barely fire on the
fast path, but management traffic still lands somewhere — keep it off the
benchmark cores.

## Memory

```
echo never > /sys/kernel/mm/transparent_hugepage/enabled   # preflight warns
```

Explicit hugepages (kernel cmdline above) for Onload; THP's defrag stalls
are a classic tail-latency source.

## Solarflare / Onload

- Install the matching sfc driver + Onload release on both hosts; versions
  land in the env snapshot (`onload --version`, `ethtool -i`).
- Interrupt coalescing: for latency runs,
  `ethtool -C ens1f0 rx-usecs 0 adaptive-rx off` (snapshot captures `-c`).
- The shipped scenarios drive Onload via env axes; useful knobs:
  `EF_POLL_USEC=100000` (spin), `EF_INT_DRIVEN=0/1`,
  `EF_TCP_FASTSTART_INIT=0`, `EF_PIO=1`, profile `latency`.
  Sweep them as matrix axes rather than hand-editing hosts.
- Confirm acceleration during a run with `onload_stackdump lots | head`.
  Zero stacks = you're benchmarking the kernel by accident.

## tuned (alternative to hand-tuning)

Two relevant profiles:

- `tuned-adm profile network-latency` — covers governor/THP/busy-poll in
  one step; combine with explicit `isolcpus=` kernel args.
- `tuned-adm profile cpu-partitioning` — full isolation managed by tuned.
  Configure housekeeping cores in `/etc/tuned/cpu-partitioning-variables.conf`
  (`isolated_cores=2-7`); tuned then boots with
  `tuned.non_isolcpus=<hexmask>` (a mask of the *housekeeping* CPUs) instead
  of `isolcpus=`, so `/sys/devices/system/cpu/isolated` stays empty.
  **Preflight understands this**: when the sysfs isolated list doesn't cover
  the scenario cores, it parses `tuned.non_isolcpus` from `/proc/cmdline`
  and treats every present CPU outside the housekeeping mask as isolated
  (the check message reports which source satisfied it).

Either way, still verify irqbalance yourself; the active profile is captured
in every run record.

## Verifying the setup

```
perfbench preflight scenarios/ -s bm-onload-pp --transport ssh \
    --client-host trader-a --ssh-user bench
perfbench run scenarios/ -s baremetal-cpu-isolation ...   # sysjitter
```

On a well-tuned host, sysjitter p99.9 < 10 µs with < 10 interruptions/s per
isolated core (plus cyclictest max < 30 µs where it's installed — it's
optional, see docs/tools.md). Fix the substrate before
measuring the network — network tail latency on a jittery host measures the
host, not the network.
