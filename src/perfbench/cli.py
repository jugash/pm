"""PerfBench command-line interface.

Commands:

* ``validate``   — validate scenario files (including matrix expansion)
* ``scenarios``  — list expanded scenarios
* ``plan``       — show resolved server/client commands without running
* ``run``        — execute scenarios against local/SSH/k8s targets
* ``preflight``  — run only the preflight checks against a target
* ``report``     — terminal comparison table from the result DB
* ``push``       — push stored runs to the exporter ingest API
* ``exporter``   — serve the Prometheus exporter
"""

from __future__ import annotations

import sys
from pathlib import Path

import click

from perfbench import __version__
from perfbench.capture.preflight import fatal_failures, run_preflight
from perfbench.config.loader import load_scenarios
from perfbench.errors import PerfBenchError
from perfbench.export.push import push_run
from perfbench.report.summary import comparison_rows, format_table
from perfbench.results.store import ResultStore
from perfbench.runner.base import Executor
from perfbench.runner.local import LocalExecutor
from perfbench.runner.orchestrator import Orchestrator
import perfbench.tools  # noqa: F401  (registers adapters)


def _build_executor(transport: str, host: str | None, user: str | None,
                    ssh_key: str | None, namespace: str, pod: str | None) -> Executor:
    if transport == "local":
        return LocalExecutor()
    if transport == "ssh":
        if not host:
            raise click.UsageError("--client-host/--server-host required for ssh transport")
        from perfbench.runner.ssh import SSHExecutor

        return SSHExecutor(host, user=user, key_filename=ssh_key)
    if transport == "k8s":
        if not pod:
            raise click.UsageError("--client-pod/--server-pod required for k8s transport")
        from perfbench.runner.k8s import Kubectl, PodExecutor

        return PodExecutor(Kubectl(namespace=namespace), pod)
    raise click.UsageError(f"unknown transport {transport}")  # pragma: no cover


def _select(scenarios, ids):
    if not ids:
        return scenarios
    by_id = {s.id: s for s in scenarios}
    missing = [i for i in ids if i not in by_id]
    if missing:
        raise click.UsageError(f"unknown scenario id(s): {missing}")
    return [by_id[i] for i in ids]


@click.group()
@click.version_option(version=__version__, prog_name="perfbench")
def main() -> None:
    """Network & CPU latency benchmark harness."""


@main.command()
@click.argument("path", type=click.Path(exists=True))
def validate(path: str) -> None:
    """Validate scenario file(s); exits non-zero on schema errors."""
    scenarios = load_scenarios(path)
    click.echo(f"OK: {len(scenarios)} scenario(s) valid")


@main.command()
@click.argument("path", type=click.Path(exists=True))
def scenarios(path: str) -> None:
    """List expanded scenarios."""
    rows = [
        {
            "id": s.id,
            "platform": s.platform.value,
            "network": s.network_path.value,
            "nic": s.nic.label(),
            "tools": ",".join(t.name for t in s.tools),
            "reps": s.repetitions,
        }
        for s in load_scenarios(path)
    ]
    click.echo(format_table(rows, ["id", "platform", "network", "nic", "tools", "reps"]))


@main.command()
@click.argument("path", type=click.Path(exists=True))
@click.option("--scenario", "-s", "ids", multiple=True, help="Scenario id(s); default all.")
@click.option("--server-address", default="<server>", show_default=True)
def plan(path: str, ids, server_address: str) -> None:
    """Print resolved commands without executing anything."""
    orch = Orchestrator(
        client=LocalExecutor(), server=LocalExecutor(), server_address=server_address
    )
    for sc in _select(load_scenarios(path), ids):
        click.echo(f"# scenario: {sc.id}  [{sc.platform.value}/{sc.network_path.value}"
                   f"/{sc.nic.label()}] reps={sc.repetitions}")
        for p in orch.plan(sc):
            if p.server_command:
                click.echo(f"  [{p.adapter.name}] server: {p.server_command}")
            click.echo(f"  [{p.adapter.name}] client: {p.client_command}")


_transport_options = [
    click.option("--transport", type=click.Choice(["local", "ssh", "k8s"]), default="local",
                 show_default=True),
    click.option("--client-host", default=None, help="SSH host for the client role."),
    click.option("--server-host", default=None, help="SSH host for the server role."),
    click.option("--ssh-user", default=None),
    click.option("--ssh-key", default=None, type=click.Path()),
    click.option("--namespace", default="perfbench", show_default=True),
    click.option("--client-pod", default=None, help="Pod name for the client role."),
    click.option("--server-pod", default=None, help="Pod name for the server role."),
]


def _with_transport(fn):
    for opt in reversed(_transport_options):
        fn = opt(fn)
    return fn


@main.command()
@click.argument("path", type=click.Path(exists=True))
@click.option("--scenario", "-s", "ids", multiple=True)
@click.option("--server-address", required=True,
              help="Address the client targets (server data-path IP).")
@click.option("--db", default="results/perfbench.sqlite", show_default=True)
@click.option("--output-dir", default="results/raw", show_default=True)
@click.option("--skip-preflight", is_flag=True)
@click.option("--settle", default=1.0, show_default=True, help="Server settle seconds.")
@click.option("--push", "push_url", default=None, help="Exporter base URL to push results to.")
@_with_transport
def run(path, ids, server_address, db, output_dir, skip_preflight, settle, push_url,
        transport, client_host, server_host, ssh_user, ssh_key, namespace,
        client_pod, server_pod):
    """Run scenario(s) end to end and persist results."""
    selected = _select(load_scenarios(path), ids)
    Path(db).parent.mkdir(parents=True, exist_ok=True)
    store = ResultStore(db)
    client = _build_executor(transport, client_host, ssh_user, ssh_key, namespace, client_pod)
    server = _build_executor(transport, server_host, ssh_user, ssh_key, namespace, server_pod)
    orch = Orchestrator(
        client=client,
        server=server,
        server_address=server_address,
        store=store,
        output_dir=Path(output_dir),
        settle_s=settle,
        on_event=click.echo,
    )
    failed = 0
    for sc in selected:
        try:
            records = orch.run_scenario(sc, skip_preflight=skip_preflight)
        except PerfBenchError as exc:
            click.echo(f"ERROR [{sc.id}]: {exc}", err=True)
            failed += 1
            continue
        failed += _report_records(records, sc.id, push_url)
    store.close()
    if failed:
        sys.exit(1)


@main.command()
@click.argument("path", type=click.Path(exists=True))
@click.option("--scenario", "-s", "ids", multiple=True, required=True)
@click.option("--role", type=click.Choice(["client", "server"]), default="client",
              show_default=True)
@_with_transport
def preflight(path, ids, role, transport, client_host, server_host, ssh_user, ssh_key,
              namespace, client_pod, server_pod):
    """Run preflight checks only; exits non-zero on fatal failures."""
    host = client_host if role == "client" else server_host
    pod = client_pod if role == "client" else server_pod
    executor = _build_executor(transport, host, ssh_user, ssh_key, namespace, pod)
    any_fatal = False
    for sc in _select(load_scenarios(path), ids):
        results = run_preflight(executor, sc, role=role)
        click.echo(f"# {sc.id} ({role} on {executor.describe()})")
        for r in results:
            mark = "PASS" if r.passed else ("FATAL" if r.severity == "fatal" else "WARN")
            click.echo(f"  [{mark:5}] {r.name}: {r.message}")
        any_fatal = any_fatal or bool(fatal_failures(results))
    if any_fatal:
        sys.exit(1)


@main.command()
@click.option("--db", default="results/perfbench.sqlite", show_default=True)
@click.option("--metric", default="latency_ns", show_default=True)
@click.option("--quantile", default="0.99", show_default=True,
              help="Quantile label to filter on; use 'all' to disable.")
@click.option("--all-runs", is_flag=True, help="Include all runs, not just latest.")
def report(db, metric, quantile, all_runs):
    """Terminal comparison table across scenarios."""
    store = ResultStore(db)
    q = None if quantile == "all" else quantile
    rows = comparison_rows(store, metric=metric, quantile=q, latest_only=not all_runs)
    columns = ["scenario", "tool", "msg_size", "quantile", "value", "unit"]
    if all_runs:
        columns.append("run_id")
    click.echo(format_table(rows, columns))
    store.close()


@main.command(name="push")
@click.option("--db", default="results/perfbench.sqlite", show_default=True)
@click.option("--url", required=True, help="Exporter base URL, e.g. http://exporter:9109")
@click.option("--run-id", default=None, help="Push one run; default: all runs.")
def push_cmd(db, url, run_id):
    """Push stored run records to the exporter ingest API."""
    store = ResultStore(db)
    if run_id:
        record = store.get_run(run_id)
        if record is None:
            raise click.UsageError(f"unknown run id {run_id}")
        runs = [record]
    else:
        runs = [store.get_run(r["run_id"]) for r in store.list_runs()]
    for record in runs:
        push_run(url, record.to_dict())
    click.echo(f"pushed {len(runs)} run(s) to {url}")
    store.close()


def _make_kubectl(namespace, context, kubeconfig):
    """Factory hook (patched in tests)."""
    from perfbench.runner.k8s import Kubectl

    return Kubectl(namespace=namespace, context=context, kubeconfig=kubeconfig)


def _report_records(records, scenario_id, push_url):
    failed = 0
    for record in records:
        bad = [t.tool for t in record.tool_runs if not t.ok]
        status = f"FAILED tools: {bad}" if bad else "ok"
        click.echo(f"[{scenario_id}] run {record.run_id} rep {record.rep}: {status}")
        if bad:
            failed += 1
        if push_url:
            push_run(push_url, record.to_dict())
    return failed


@main.command(name="k8s-run")
@click.argument("path", type=click.Path(exists=True))
@click.option("--scenario", "-s", "ids", multiple=True)
@click.option("--namespace", default="perfbench", show_default=True)
@click.option("--context", default=None)
@click.option("--kubeconfig", default=None, type=click.Path())
@click.option("--image", required=True, help="Benchmark pod image.")
@click.option("--client-node", default=None, help="Pin the client pod to a node.")
@click.option("--server-node", default=None, help="Pin the server pod to a node.")
@click.option("--network", "networks", multiple=True,
              help="Multus network(s); the first provides the data-path address.")
@click.option("--sriov-resource", default=None, help="e.g. amd.com/sfc_vf")
@click.option("--db", default="results/perfbench.sqlite", show_default=True)
@click.option("--output-dir", default="results/raw", show_default=True)
@click.option("--skip-preflight", is_flag=True)
@click.option("--settle", default=1.0, show_default=True)
@click.option("--push", "push_url", default=None)
@click.option("--keep-pods", is_flag=True, help="Leave pods running for debugging.")
def k8s_run(path, ids, namespace, context, kubeconfig, image, client_node,
            server_node, networks, sriov_resource, db, output_dir,
            skip_preflight, settle, push_url, keep_pods):
    """Provision benchmark pods, run k8s scenarios end-to-end, tear down.

    Fully self-contained: renders Guaranteed-QoS pods (SR-IOV VF, hugepages,
    Onload devices for bypass scenarios), waits for readiness, resolves the
    data-path address from the Multus network-status annotation, runs the
    scenario through the same orchestrator as bare metal, then deletes the
    pods. This is the command the Helm runner Job executes in-cluster.
    """
    from perfbench.config.schema import Platform
    from perfbench.runner.k8s import K8sBenchSession

    selected = _select(load_scenarios(path), ids)
    skipped = [s.id for s in selected if s.platform is not Platform.K8S]
    scenarios_k8s = [s for s in selected if s.platform is Platform.K8S]
    if skipped:
        click.echo(f"skipping non-k8s scenario(s): {skipped}")
    if not scenarios_k8s:
        raise click.UsageError("no scenarios with platform: k8s selected")

    kubectl = _make_kubectl(namespace, context, kubeconfig)
    session = K8sBenchSession(
        kubectl,
        image=image,
        networks=networks,
        sriov_resource=sriov_resource,
        client_node=client_node,
        server_node=server_node,
    )
    Path(db).parent.mkdir(parents=True, exist_ok=True)
    store = ResultStore(db)
    failed = 0
    for sc in scenarios_k8s:
        try:
            node_results: list[dict] = []
            if not skip_preflight:
                click.echo(f"[{sc.id}] running host-level node checks")
                node_results = session.node_preflight(sc)
                for r in node_results:
                    mark = "PASS" if r["passed"] else (
                        "FATAL" if r["severity"] == "fatal" else "WARN")
                    click.echo(f"  [{mark:5}] {r['target']} {r['name']}: {r['message']}")
                node_fatals = [r for r in node_results
                               if not r["passed"] and r["severity"] == "fatal"]
                if node_fatals:
                    click.echo(f"ERROR [{sc.id}]: fatal node-level preflight failures",
                               err=True)
                    failed += 1
                    continue
            click.echo(f"[{sc.id}] provisioning pods in namespace {namespace}")
            deployment = session.provision(sc)
            click.echo(
                f"[{sc.id}] client={deployment.client_pod} "
                f"server={deployment.server_pod} address={deployment.server_address}"
            )
            client, server = session.executors(deployment)
            orch = Orchestrator(
                client=client,
                server=server,
                server_address=deployment.server_address,
                store=store,
                output_dir=Path(output_dir),
                settle_s=settle,
                on_event=click.echo,
            )
            records = orch.run_scenario(
                sc, skip_preflight=skip_preflight, extra_preflight=node_results
            )
            failed += _report_records(records, sc.id, push_url)
        except PerfBenchError as exc:
            click.echo(f"ERROR [{sc.id}]: {exc}", err=True)
            failed += 1
        finally:
            if not keep_pods:
                try:
                    session.teardown_scenario(sc)  # by-name; safe after partial provision
                except PerfBenchError as exc:  # cleanup is best-effort
                    click.echo(f"WARNING [{sc.id}]: pod cleanup failed: {exc}", err=True)
    store.close()
    if failed:
        sys.exit(1)


@main.command()
@click.option("--host", default="0.0.0.0", show_default=True)
@click.option("--port", default=9109, show_default=True)
@click.option("--state-file", default=None, type=click.Path(),
              help="JSONL state file to survive restarts.")
def exporter(host, port, state_file):  # pragma: no cover - blocking server loop
    """Serve the Prometheus exporter (scrape /metrics, POST /api/v1/ingest)."""
    from perfbench.export.exporter import serve

    serve(host, port, state_file)


if __name__ == "__main__":  # pragma: no cover
    main()
