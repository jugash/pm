# Metrics pipeline

```
perfbench run --push http://exporter:9109   (or: perfbench push --db ... --url ...)
        │  POST /api/v1/ingest  (RunRecord JSON)
        ▼
perfbench exporter  ──  GET /metrics  ──>  Grafana Alloy (ServiceMonitor)  ──>  TSDB
```

The exporter keeps the **latest run per (scenario, rep)** and serves gauges;
history accumulates in your TSDB through scraping. Restart-safe via
`--state-file` (the Helm chart provisions a PVC for it). Endpoints:
`GET /metrics`, `GET /healthz`, `POST /api/v1/ingest`.

## Metric names

| metric | unit | labels (beyond base) | source |
|---|---|---|---|
| `perfbench_latency_ns` | ns | `quantile`, `msg_size`, `mode`/`test` | sfnt-pingpong, sockperf, netperf, eflatency |
| `perfbench_latency_mean_ns` / `_stddev_ns` | ns | `msg_size` | same |
| `perfbench_latency_jitter_ns` | ns | `tail_quantile`, `msg_size` | derived: p99.9 (or p99) − p50 |
| `perfbench_interruption_ns` | ns | `core`, `quantile` | sysjitter |
| `perfbench_interruptions_per_sec`, `_count`, `perfbench_interrupted_percent` | | `core` | sysjitter |
| `perfbench_wakeup_latency_ns`, `_mean_ns` | ns | `thread`, `quantile` | cyclictest |
| `perfbench_throughput_bps` | bps | `direction` | iperf3 |
| `perfbench_tcp_retransmits_count`, `perfbench_udp_jitter_ns`, `perfbench_udp_loss_percent` | | | iperf3 |
| `perfbench_messages_dropped_count` | | `msg_size`, `mode` | sockperf |
| `perfbench_transactions_per_sec` | 1/s | `msg_size`, `test` | netperf |
| `perfbench_iterations_count` | | `msg_size` | sfnt-pingpong |
| `perfbench_tool_ok` | 0/1 | `tool` | harness |
| `perfbench_run_timestamp_seconds` | s | | harness |
| `perfbench_run_info` | 1 | `kernel`, `onload_version`, `tuned_profile`, `cmdline` | env snapshot |

**Base labels on everything:** `scenario`, `platform`, `network_path`,
`bond`, `rep`, plus `tool` and `interface`.

Quantiles are encoded as label values: `0` (min), `0.5`, `0.9`, `0.99`,
`0.999`, `0.9999`, `0.99999`, `1` (max).

## Cardinality

Fixed quantiles as gauges — not native histograms — keep series counts
predictable: roughly `scenarios × reps × tools × msg_sizes × ~8 quantiles`.
A 12-scenario matrix at 5 reps with 2 sizes ≈ a few thousand series. If you
grow the matrix 10×, drop `rep` granularity first (push only rep 0, or
aggregate before pushing).

## PromQL recipes

Onload vs kernel, p99 @64B:

```promql
max by (network_path)
  (perfbench_latency_ns{scenario=~"bm-.*-pp", msg_size="64", quantile="0.99"})
```

Bare metal vs k8s delta (the containerization tax, should be ≈0):

```promql
max(perfbench_latency_ns{scenario="k8s-onload-net", quantile="0.99", msg_size="64"})
- max(perfbench_latency_ns{scenario="baremetal-onload-net", quantile="0.99", msg_size="64"})
```

Bond layout comparison:

```promql
max by (scenario, bond)
  (perfbench_latency_ns{scenario=~"bond-.*", quantile="0.999", msg_size="64"})
```

Isolation health on every benchmark core:

```promql
max by (scenario, core) (perfbench_interruption_ns{quantile="0.999"}) > 10000
```

Alert on stale results or failing tools:

```promql
time() - max(perfbench_run_timestamp_seconds) > 86400
min(perfbench_tool_ok) == 0
```

## Dashboards

Two dashboards ship in the chart (sidecar-discovered ConfigMap) and in
`deploy/helm/perfbench/dashboards/` for manual import:

- **PerfBench — Overview** (`perfbench-overview`): p99/p50 and tail-jitter
  bar gauges across scenarios, worst per-core OS jitter, failure/staleness
  stats, environment table.
- **PerfBench — Scenario Drill-down** (`perfbench-drilldown`): quantile
  trends over time (run-over-run regression tracking), latency vs message
  size, sysjitter per core, cyclictest, drops/retransmits, per-rep tool
  success timeline.
