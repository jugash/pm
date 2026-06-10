"""Statistics helpers for aggregating repeated measurements."""

from __future__ import annotations

import math
from collections import defaultdict
from dataclasses import dataclass, field
from typing import Iterable, Sequence

from perfbench.results.models import Measurement


def percentile(values: Sequence[float], q: float) -> float:
    """Linear-interpolated percentile; ``q`` in [0, 100]."""
    if not values:
        raise ValueError("percentile of empty sequence")
    if not 0.0 <= q <= 100.0:
        raise ValueError(f"q must be in [0, 100], got {q}")
    data = sorted(values)
    if len(data) == 1:
        return float(data[0])
    pos = (len(data) - 1) * (q / 100.0)
    lo = math.floor(pos)
    hi = math.ceil(pos)
    if lo == hi:
        return float(data[lo])
    frac = pos - lo
    return float(data[lo] * (1.0 - frac) + data[hi] * frac)


def summarize(values: Sequence[float]) -> dict[str, float]:
    """min/mean/median/p90/p99/p99.9/max/stddev/count for raw samples."""
    if not values:
        raise ValueError("summarize of empty sequence")
    n = len(values)
    mean = sum(values) / n
    var = sum((v - mean) ** 2 for v in values) / n
    return {
        "count": float(n),
        "min": float(min(values)),
        "mean": mean,
        "median": percentile(values, 50),
        "p90": percentile(values, 90),
        "p99": percentile(values, 99),
        "p999": percentile(values, 99.9),
        "max": float(max(values)),
        "stddev": math.sqrt(var),
    }


@dataclass
class Aggregate:
    """One metric series aggregated across repetitions."""

    tool: str
    metric: str
    unit: str
    labels: dict[str, str]
    values: list[float] = field(default_factory=list)

    @property
    def median(self) -> float:
        return percentile(self.values, 50)

    @property
    def best(self) -> float:
        return min(self.values)

    @property
    def worst(self) -> float:
        return max(self.values)

    @property
    def spread_pct(self) -> float:
        """Run-to-run spread as a percentage of the median (stability signal)."""
        med = self.median
        if med == 0:
            return 0.0
        return 100.0 * (self.worst - self.best) / med


def aggregate(measurements: Iterable[Measurement]) -> list[Aggregate]:
    """Group identical metric series across repetitions.

    Two measurements belong to the same series when tool, metric, unit and
    full label set all match.
    """
    groups: dict[tuple, Aggregate] = {}
    order: list[tuple] = []
    for m in measurements:
        key = (m.tool, m.metric, m.unit, tuple(sorted(m.labels.items())))
        if key not in groups:
            groups[key] = Aggregate(
                tool=m.tool, metric=m.metric, unit=m.unit, labels=dict(m.labels)
            )
            order.append(key)
        groups[key].values.append(m.value)
    return [groups[k] for k in order]


def derive_jitter(measurements: list[Measurement]) -> list[Measurement]:
    """Derive tail-jitter metrics from latency quantile measurements.

    For every (tool, metric=*_ns, label-group) that has both a p50 and a
    p99.9 (falling back to p99) quantile, emit ``<metric base>_jitter_ns`` =
    high quantile − median. This is the "how much worse is the tail than
    typical" number trading desks actually watch.
    """
    groups: dict[tuple, dict[str, Measurement]] = defaultdict(dict)
    for m in measurements:
        quantile = m.labels.get("quantile")
        if quantile is None or not m.metric.endswith("_ns"):
            continue
        key_labels = tuple(
            sorted((k, v) for k, v in m.labels.items() if k != "quantile")
        )
        groups[(m.tool, m.metric, m.unit, key_labels)][quantile] = m

    derived: list[Measurement] = []
    for (tool, metric, unit, key_labels), quantiles in groups.items():
        median = quantiles.get("0.5")
        tail = quantiles.get("0.999") or quantiles.get("0.99")
        if median is None or tail is None:
            continue
        labels = dict(key_labels)
        labels["tail_quantile"] = tail.labels["quantile"]
        base = metric[: -len("_ns")]
        derived.append(
            Measurement(
                tool=tool,
                metric=f"{base}_jitter_ns",
                value=max(0.0, tail.value - median.value),
                unit=unit,
                labels=labels,
            )
        )
    return derived


def by_metric(aggregates: Iterable[Aggregate]) -> dict[str, list[Aggregate]]:
    out: dict[str, list[Aggregate]] = defaultdict(list)
    for agg in aggregates:
        out[agg.metric].append(agg)
    return dict(out)
