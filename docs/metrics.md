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

Two domain dashboards ship in the chart (sidecar-discovered ConfigMap) and in
`deploy/helm/perfbench/dashboards/` for manual import. They are split by
domain on purpose: each derives its template variables from its **own** metric
family, so the `scenario`/`msg_size` and `scenario`/`core` pickers only ever
list values that actually exist for that domain (network scenarios don't emit
sysjitter series, and vice-versa).

- **PerfBench — Network** (`perfbench-network`): variables from
  `perfbench_latency_ns` (scenario / tool / msg_size). Comparison table
  (p50/p99/p99.9 pivoted per scenario), p99 + tail-jitter bar gauges, quantile
  trends over time, full percentile ladder, latency-vs-size, mean±stddev,
  throughput (iperf3), transaction rate (netperf), drops/retransmits/loss/UDP
  jitter, per-rep success timeline, environment table.
- **PerfBench — CPU** (`perfbench-cpu`): variables from
  `perfbench_interruption_ns` + `perfbench_wakeup_latency_ns` (scenario /
  core). Per-core jitter p99.9 bar gauge (isolation thresholds), interrupts/s
  and worst-max **bar charts** by core, the classic per-core interruption
  ladder as a **colour-scaled table** (core × quantile), jitter-over-time,
  time-interrupted %, cyclictest wakeup min/avg/max, failure/staleness stats,
  environment table.

Visualization choices: categorical comparisons (per scenario / message size /
core / thread / quantile) use **bar gauges**; the scenario×quantile and
core×quantile matrices use **colour-background tables** (a heatmap over a
categorical grid, cells scaled green→red by magnitude); trends over runs use
**time series**. Bar gauges and tables render reliably from Prometheus instant
multi-series; the dedicated **Bar chart** panel is intentionally avoided here
because it requires a single pre-joined "wide" frame and renders empty with
plain instant queries. A true latency *density* heatmap (X=time, Y=latency
bucket, colour=count) is likewise not used: the exporter emits fixed quantiles
as gauges, not histogram buckets, so there is no distribution to bin. If you
want real density heatmaps (and a true grouped Bar chart panel), the exporter
and tool adapters would need to emit Prometheus histogram (`_bucket`) series —
ask and that can be added.
