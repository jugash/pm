# Architecture

## Design goals

PerfBench exists to answer one question repeatedly and trustworthily: *for a
given host/NIC/kernel configuration, what latency and jitter does the network
and CPU substrate deliver?* Everything follows from three principles:

1. **Use established tools, orchestrate rigorously.** sfnt-pingpong, sockperf,
   netperf, iperf3, sysjitter, cyclictest and eflatency are battle-tested;
   reimplementing them would only add doubt. The harness adds what they lack:
   configuration management, environment capture, normalization, comparison.
2. **A number without its environment is noise.** Every run record embeds a
   full snapshot (kernel cmdline, isolation, governor, NIC driver/firmware,
   Onload version, IRQ layout) and the preflight verdicts, so any two results
   can be compared honestly — or rejected as incomparable.
3. **One code path for all platforms.** Bare metal vs container comparisons
   are only valid if the measurement pipeline is identical. The orchestrator
   drives abstract `Executor`s; whether commands travel over SSH or
   `kubectl exec` is invisible to it.

## Components

```
scenarios/*.yaml ──> config.loader ──> [Scenario]            (matrix expansion)
                                          │
                                          ▼
                  runner.orchestrator.Orchestrator
                  │   1. capture.preflight  (abort on fatal misconfig)
                  │   2. capture.envsnapshot (client + server)
                  │   3. per rep, per tool:
                  │        tools.<adapter>.server_command ──> server Executor.start()
                  │        tools.<adapter>.client_command ──> client Executor.run()
                  │        tools.<adapter>.parse ──> [Measurement]
                  │        stats.derive_jitter ──> +latency_jitter_ns
                  ▼
                  results.RunRecord ──> results.ResultStore (SQLite, full JSON)
                                   └──> export.push ──> exporter /api/v1/ingest
                                                            │
                                        Alloy scrape <── /metrics (prom text)
```

### Executors (`runner/`)

`Executor` is the transport abstraction: `run()` for foreground commands,
`start()` for long-lived servers (returns a `BackgroundProcess` with
`stop()`).

- `LocalExecutor` — subprocess; background commands run in their own process
  group so stopping a server kills the whole `sh → taskset → onload → tool`
  chain.
- `SSHExecutor` — paramiko, lazily imported (`perfbench[ssh]`); env vars are
  prefixed as `env K=V cmd` because sshd usually refuses `setenv`.
- `PodExecutor` — wraps `kubectl exec` against a running pod via `Kubectl`,
  which itself drives an injected executor (testable, and `kubectl` can run
  anywhere, including over SSH on a bastion).
- `FakeExecutor` — scripted responses; the unit-test workhorse.

### Tool adapters (`tools/`)

Each adapter declares defaults, builds the wrapped command lines, and parses
stdout into `Measurement`s. Wrapping is centralized in `ToolAdapter.wrap`:
core pinning (`taskset -c`), then for network tools the Onload prefix
(`env EF_…=… onload --profile=…`). CPU-only tools (`sysjitter`,
`cyclictest`) opt out via `uses_network = False`; `eflatency` refuses to run
outside `network_path: efvi` scenarios.

Adapters are registered with `@register`; `create_tool(spec)` resolves by
name, so adding a tool = one file + tests, no orchestrator changes.

### Measurement model (`results/`)

`Measurement(tool, metric, value, unit, labels)`, normalized at parse time:

- nanoseconds everywhere (`*_ns`), bits/s for throughput;
- latency distributions: metric `latency_ns` + `quantile` label
  (`0`, `0.5`, `0.99`, `0.999`, `1`…) — mirrors Prometheus summaries and
  makes PromQL slicing trivial;
- context labels: `msg_size`, `mode`, `core`, `thread`, `direction`,
  `interface` (stamped by the orchestrator).

`RunRecord` = one repetition of one scenario: labels, env snapshots,
preflight verdicts, every tool's commands/exit codes/measurements. Stored as
full-fidelity JSON in SQLite plus a flattened, indexed `measurements` table.

### Exporter (`export/`)

Benchmarks are batch jobs; Prometheus is pull-based. The bridge is a small,
long-lived exporter: `POST /api/v1/ingest` accepts run records (the runner's
`--push`, or `perfbench push` for backfill), `GET /metrics` serves the latest
run per (scenario, rep) in Prometheus text format. An optional JSONL state
file survives restarts. Historical values live in your TSDB once scraped —
the exporter deliberately is not a database.

The text exposition is implemented in-tree (`promtext.py`, ~50 lines) to
keep the runtime dependency-free.

### Failure philosophy

- **Preflight failures are fatal** (irqbalance running, cores not isolated,
  Onload missing): a run that would lie refuses to start.
- **Tool failures are recorded, not raised**: a flaky tool mustn't destroy a
  2-hour matrix. The failure is visible in the run record, the CLI exit code,
  and the `perfbench_tool_ok` metric.
- **Snapshot failures are embedded** (`<error: …>`): missing `tuned-adm` on a
  host shouldn't abort a benchmark, but the gap stays visible.

## Testing strategy

~190 unit tests, ~99% line coverage, enforced ≥90%. All transports are tested
against fakes (scripted `FakeExecutor`, fake paramiko client, fake kubectl);
parsers against realistic canned outputs; the exporter over real HTTP on an
ephemeral port; the CLI end-to-end with `--transport local` and a fixture
tool. `scripts/run_tests.py` provides line coverage with zero dependencies
(sys.settrace + `co_lines()`), so the gate runs on minimal hosts; `make
pytest` runs the same suite under pytest-cov.
