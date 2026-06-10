"""Plain-text comparison reports from the result store.

Grafana is the primary consumption path; this gives a quick terminal view
(`perfbench report`) without any infrastructure.
"""

from __future__ import annotations

from typing import Any, Optional, Sequence

from perfbench.results.store import ResultStore


def comparison_rows(
    store: ResultStore,
    metric: str = "latency_ns",
    quantile: Optional[str] = "0.99",
    latest_only: bool = True,
) -> list[dict[str, Any]]:
    """One row per (scenario, tool, msg_size[, extra]) for ``metric``.

    With ``latest_only`` the latest run per scenario is used; otherwise all
    runs contribute (run_id included in the row).
    """
    latest = store.latest_run_ids() if latest_only else {}
    rows: list[dict[str, Any]] = []
    for m in store.measurements(metric=metric):
        if latest_only and latest.get(m["scenario_id"]) != m["run_id"]:
            continue
        labels = m["labels"]
        if quantile is not None and labels.get("quantile") != quantile:
            continue
        rows.append(
            {
                "scenario": m["scenario_id"],
                "tool": m["tool"],
                "msg_size": labels.get("msg_size", "-"),
                "quantile": labels.get("quantile", "-"),
                "value": m["value"],
                "unit": m["unit"],
                "run_id": m["run_id"],
            }
        )
    rows.sort(key=lambda r: (r["scenario"], r["tool"], _msg_key(r["msg_size"])))
    return rows


def _msg_key(value: str) -> tuple:
    try:
        return (0, int(value))
    except ValueError:
        return (1, value)


def format_table(rows: Sequence[dict[str, Any]], columns: Sequence[str]) -> str:
    """Fixed-width table for terminal output."""
    if not rows:
        return "(no results)"
    headers = list(columns)
    cells = [[_fmt(row.get(col, "")) for col in headers] for row in rows]
    widths = [
        max(len(headers[i]), *(len(row[i]) for row in cells)) for i in range(len(headers))
    ]
    lines = [
        "  ".join(h.ljust(widths[i]) for i, h in enumerate(headers)),
        "  ".join("-" * widths[i] for i in range(len(headers))),
    ]
    for row in cells:
        lines.append("  ".join(row[i].ljust(widths[i]) for i in range(len(headers))))
    return "\n".join(lines)


def _fmt(value: Any) -> str:
    if isinstance(value, float):
        return f"{value:,.0f}" if value >= 100 else f"{value:.2f}"
    return str(value)
