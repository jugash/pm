# PerfBench

Network & CPU latency benchmark harness for low-latency trading platforms.

PerfBench drives established open-source benchmark tools across a matrix of
configurations — Solarflare/Onload kernel bypass vs the kernel stack, NIC
bonding layouts, Onload tuning, CPU pinning/isolation, and bare metal vs
Kubernetes (Multus + SR-IOV) — and turns their output into normalized,
comparable, Grafana-visualized measurements.

It deliberately runs **no application benchmarks** and writes **no benchmark
code of its own**: the value is in rigorous orchestration, environment
capture, and comparable results.

## What it measures

| Dimension | Tool | What you get |
|---|---|---|
| Socket RTT, kernel vs Onload | `sfnt-pingpong` (sfnettest) | mean/min/median/p99/max per message size, identical binary both paths |
| Latency under load | `sockperf` | full percentile ladder (p50…p99.999) at a controlled message rate |
| RTT cross-check | `netperf` TCP_RR/UDP_RR | omni-selector percentiles + transaction rate |
| Raw ef_vi floor | `eflatency` (Onload) | layer-2 RTT below the sockets API |
| Throughput sanity | `iperf3` | bps, retransmits, CPU |
| OS jitter on isolated cores | `sysjitter` | per-core interruption counts/durations (p99.9, max) |
| Scheduler wakeup latency | `cyclictest` (rt-tests) | min/avg/max wakeup latency per thread |
| Derived | harness | `latency_jitter_ns` = p99.9 − p50 tail spread per series |

All time values are normalized to nanoseconds; latency distributions use a
`quantile` label (`0`=min … `1`=max).

## Quick start

```bash
pip install -e .            # PyYAML + click only
pip install -e ".[ssh,dev]" # + paramiko, pytest

# validate and inspect scenarios
perfbench validate scenarios/
perfbench scenarios scenarios/
perfbench plan scenarios/ -s bm-onload-pp --server-address 192.168.100.2

# check a host is fit to benchmark (irqbalance, isolation, governor, onload…)
perfbench preflight scenarios/ -s bm-onload-pp \
    --transport ssh --client-host trader-a --ssh-user bench

# run: client on trader-a, server on trader-b, results into SQLite + raw logs
perfbench run scenarios/ -s bm-kernel-pp -s bm-onload-pp \
    --transport ssh --client-host trader-a --server-host trader-b \
    --ssh-user bench --server-address 192.168.100.2 \
    --push http://perfbench-exporter:9109

# quick terminal comparison
perfbench report --metric latency_ns --quantile 0.99
```

## Results pipeline

```
perfbench run ──> SQLite (full fidelity) ──push──> exporter /api/v1/ingest
                                                        │
                       Grafana Alloy ──scrape──> /metrics (ServiceMonitor)
                                                        │
                                              Grafana dashboards
```

Deploy the exporter + ServiceMonitor + SCC + dashboards with the Helm chart
(OpenShift-focused; UBI 9 images), and run benchmarks **in-cluster** via the
runner Job:

```bash
helm install perfbench deploy/helm/perfbench -n perfbench --create-namespace \
    --set serviceMonitor.labels.release=monitoring \
    --set sriov.enabled=true --set nad.enabled=true

# launch a benchmark Job (provisions pods, runs, pushes, cleans up)
helm upgrade perfbench deploy/helm/perfbench -n perfbench --reuse-values \
    --set runner.enabled=true \
    --set runner.clientNode=worker-a --set runner.serverNode=worker-b \
    --set-file runner.scenarios=scenarios/k8s-vs-baremetal.yaml

# or directly from a bastion with oc configured:
perfbench k8s-run scenarios/ -s k8s-onload-net --namespace perfbench \
    --image ghcr.io/jugash/perfbench-bench:0.1.0 \
    --client-node worker-a --server-node worker-b --network sriov-trading
```

## Testing

The framework itself is tested to **>90% line coverage** (currently ~99%),
enforced in CI-style by a zero-dependency runner:

```bash
make test     # unittest + built-in coverage tracer, fails under 90%
make pytest   # same suite under pytest-cov (dev extras)
```

## Documentation

- [docs/architecture.md](docs/architecture.md) — components and data flow
- [docs/methodology.md](docs/methodology.md) — how to benchmark honestly
- [docs/scenarios.md](docs/scenarios.md) — scenario/matrix schema reference
- [docs/tools.md](docs/tools.md) — tool adapters, parsed output, caveats
- [docs/host-tuning.md](docs/host-tuning.md) — host prep for valid numbers
- [docs/kubernetes.md](docs/kubernetes.md) — OpenShift/Multus/SR-IOV setup, in-cluster runner, preflight-in-pods
- [docs/metrics.md](docs/metrics.md) — exporter, metric names, PromQL recipes
- [docs/runbook.md](docs/runbook.md) — step-by-step campaign runbook

## Repository layout

```
src/perfbench/      framework (config, runner, tools, capture, export, report)
scenarios/          example scenario matrices (edit for your lab)
deploy/helm/        Helm chart: exporter, ServiceMonitor, SR-IOV/Multus, dashboards
deploy/docker/      exporter + benchmark-pod images
scripts/run_tests.py  zero-dependency test + coverage runner
tests/              unit tests (190+, ~99% line coverage)
docs/               documentation
```
