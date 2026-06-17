# Campaign runbook

A repeatable sequence for a benchmark campaign (e.g. "qualify new Onload
release", "decide bond layout", "validate the k8s platform").

## 0. One-time setup

1. Tune both hosts per [host-tuning.md](host-tuning.md); reboot.
2. Install tools on both hosts (or use the bench container image):
   `iperf3 netperf sockperf` + sfnt-pingpong/sfnt-stream + sysjitter
   (+ Onload). `rt-tests` (cyclictest) is optional — skip it where
   git.kernel.org / numactl-devel are unreachable.
3. Deploy the exporter: `helm install perfbench deploy/helm/perfbench …`
   (see [kubernetes.md](kubernetes.md)); confirm Alloy is scraping:
   `curl http://exporter:9109/healthz`, then check
   `perfbench_run_timestamp_seconds` exists in Grafana Explore after the
   first push.
4. Discover each host's NIC facts and write them into the scenarios —
   interface names differ per machine, so don't guess:

   ```bash
   perfbench nics --transport ssh --client-host trader-a --ssh-user bench
   ```

   Copy `name`, `pci`, `numa_node` per host into `scenarios/*.yaml` (and set
   `nic.vendor: "0x1924"` so preflight pins the ports to Solarflare
   silicon). Then `perfbench validate scenarios/`.

## 1. Qualify the substrate (per host pair, per reboot)

```bash
perfbench preflight scenarios/ -s bm-onload-pp \
  --transport ssh --client-host trader-a --ssh-user bench
perfbench preflight scenarios/ -s bm-onload-pp --role server \
  --transport ssh --server-host trader-b --ssh-user bench

perfbench run scenarios/ -s baremetal-cpu-isolation \
  --transport ssh --client-host trader-a --server-host trader-b \
  --ssh-user bench --server-address 192.168.100.2
perfbench report --metric interruption_ns --quantile 0.999
```

Gate: sysjitter p99.9 < 10 µs per isolated core (plus cyclictest max
< 30 µs where installed — it's optional). Don't proceed past a failing
gate — fix the host.

## 2. Run the matrix

```bash
perfbench run scenarios/ \
  -s bm-kernel-pp -s bm-onload-pp -s bm-onload-0 -s bm-onload-1 \
  --transport ssh --client-host trader-a --server-host trader-b \
  --ssh-user bench --server-address 192.168.100.2 \
  --db results/perfbench.sqlite --output-dir results/raw \
  --push http://exporter:9109
```

Notes:

- `--server-address` is the data-path IP (the benchmark NIC/bond/PF), not
  the management address.
- Each scenario runs its repetitions back-to-back; the matrix above is ~30
  min with the shipped durations.
- A failed tool doesn't stop the campaign; it exits non-zero at the end and
  shows up red in `perfbench_tool_ok`. Raw stdout/stderr for every tool run
  is in `results/raw/<run_id>-<tool>.{out,err}` — keep this directory.

## 3. Read the results

1. Grafana → *PerfBench — Overview*: p99 and tail-jitter per scenario.
2. Check stability first: rep-to-rep spread on p99 > ~5% ⇒ rerun after
   investigating (irqbalance creep, thermal, switch neighbours); the env
   table on the dashboard shows kernel/onload/tuned per scenario.
3. Then read the comparisons (PromQL recipes in
   [metrics.md](metrics.md)). Terminal alternative:
   `perfbench report --metric latency_ns --quantile 0.99`.
4. Archive: `results/perfbench.sqlite` + `results/raw/` are the full-fidelity
   record; `perfbench push` can replay them into a fresh exporter at any
   time. Commit the scenario files used alongside the results.

## 4. Bonding failover (manual, cut one)

1. Start a long under-load run against the bond scenario
   (`duration_s: 120`).
2. At t≈60 s, fail the active path (switch port disable, or
   `ip link set ens1f0 down` on the server).
3. The excursion is in the max/percentile gap of that rep vs the others, and
   visible in the raw sockperf output. Record which member was active
   (`cat /proc/net/bonding/bond0` before/after) alongside.

## Troubleshooting

| symptom | likely cause |
|---|---|
| preflight `cpu_isolation` fails | scenario cores not in `isolcpus`; check `/sys/devices/system/cpu/isolated` |
| Onload numbers ≈ kernel numbers | acceleration silently bypassed: unsupported bond mode, missing `onload` prefix in plan (check `perfbench plan`), or stack creation failed — `onload_stackdump` |
| huge p99.999 on otherwise clean runs | run length too short for the percentile; C-states not limited; SMT sibling active |
| `sockperf` drops > 0 under load | `mps` beyond capacity for the size, or server core overloaded — sweep rates downward |
| k8s much worse than bare metal | pod on wrong NUMA node, CNI overlay address used as `--server-address`, CPU manager not static, Onload userland/module mismatch |
| exporter shows nothing | push URL wrong (`run --push`), or Alloy label selector ≠ `serviceMonitor.labels` |
