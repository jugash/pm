"""Prometheus text exposition format (version 0.0.4), zero-dependency.

Implements exactly the subset PerfBench needs: gauge families with
per-sample label sets, correct label-value escaping, and deterministic
ordering (so output is diff-able and trivially testable).
"""

from __future__ import annotations

import math
import re
from dataclasses import dataclass, field
from typing import Mapping

_NAME_RE = re.compile(r"[^a-zA-Z0-9_:]")
_LABEL_NAME_RE = re.compile(r"[^a-zA-Z0-9_]")

CONTENT_TYPE = "text/plain; version=0.0.4; charset=utf-8"


def sanitize_metric_name(name: str) -> str:
    name = _NAME_RE.sub("_", name)
    if name and name[0].isdigit():
        name = "_" + name
    return name


def sanitize_label_name(name: str) -> str:
    name = _LABEL_NAME_RE.sub("_", name)
    if name and name[0].isdigit():
        name = "_" + name
    return name


def escape_label_value(value: str) -> str:
    return value.replace("\\", "\\\\").replace("\n", "\\n").replace('"', '\\"')


def format_value(value: float) -> str:
    if math.isnan(value):
        return "NaN"
    if math.isinf(value):
        return "+Inf" if value > 0 else "-Inf"
    if float(value).is_integer() and abs(value) < 1e15:
        return str(int(value))
    return repr(float(value))


@dataclass
class GaugeFamily:
    name: str
    help: str
    # key: sorted label tuple -> value (last write wins)
    samples: dict[tuple, float] = field(default_factory=dict)

    def set(self, labels: Mapping[str, str], value: float) -> None:
        key = tuple(
            sorted((sanitize_label_name(k), str(v)) for k, v in labels.items() if v != "")
        )
        self.samples[key] = float(value)

    def render(self) -> str:
        lines = [
            f"# HELP {self.name} {self.help}",
            f"# TYPE {self.name} gauge",
        ]
        for key in sorted(self.samples):
            value = self.samples[key]
            if key:
                labels = ",".join(f'{k}="{escape_label_value(v)}"' for k, v in key)
                lines.append(f"{self.name}{{{labels}}} {format_value(value)}")
            else:
                lines.append(f"{self.name} {format_value(value)}")
        return "\n".join(lines)


def render_families(families: Mapping[str, GaugeFamily]) -> str:
    parts = [families[name].render() for name in sorted(families)]
    return "\n".join(parts) + "\n" if parts else "\n"
