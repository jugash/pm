"""Scenario file loading and matrix expansion.

A scenario file may contain literal ``scenarios`` and/or ``matrices``. A
matrix has a ``base`` scenario document, an ``axes`` mapping of dotted
document paths to value lists, and an ``id_template`` rendered per
combination — the cartesian product becomes one scenario per combination:

.. code-block:: yaml

    matrices:
      - id_template: "bm-{network_path}-{nic.bond_mode}"
        base: { ... scenario without id ... }
        axes:
          network_path: [kernel, onload]
          nic.bond_mode: [none, active_backup]
"""

from __future__ import annotations

import copy
import itertools
import re
from pathlib import Path
from typing import Any, Iterable

import yaml

from perfbench.config.schema import Scenario
from perfbench.errors import SchemaError

_TOKEN_RE = re.compile(r"\{([^}]+)\}")


def _slug(value: Any) -> str:
    out = re.sub(r"[^a-z0-9]+", "-", str(value).lower()).strip("-")
    return out or "x"


def set_path(doc: dict, path: str, value: Any) -> None:
    """Set ``value`` at dotted ``path``, creating intermediate mappings.

    Integer segments index into lists: ``nic.ports.0.name``.
    """
    parts = path.split(".")
    node: Any = doc
    for i, part in enumerate(parts[:-1]):
        nxt = parts[i + 1]
        if isinstance(node, list):
            node = node[int(part)]
        else:
            if part not in node or node[part] is None:
                node[part] = [] if nxt.isdigit() else {}
            node = node[part]
    last = parts[-1]
    if isinstance(node, list):
        node[int(last)] = value
    else:
        node[last] = value


def get_path(doc: Any, path: str) -> Any:
    node = doc
    for part in path.split("."):
        if isinstance(node, list):
            node = node[int(part)]
        else:
            node = node[part]
    return node


def _render_id(template: str, doc: dict, mpath: str) -> str:
    def repl(match: re.Match) -> str:
        token = match.group(1)
        try:
            return _slug(get_path(doc, token))
        except (KeyError, IndexError, TypeError):
            raise SchemaError(
                f"{mpath}.id_template", f"unknown path {token!r} in id_template"
            ) from None

    return _TOKEN_RE.sub(repl, template)


def expand_matrix(spec: Any, mpath: str = "matrices[0]") -> list[Scenario]:
    """Expand one matrix specification into concrete scenarios."""
    if not isinstance(spec, dict):
        raise SchemaError(mpath, "matrix must be a mapping")
    unknown = set(spec) - {"base", "axes", "id_template"}
    if unknown:
        raise SchemaError(mpath, f"unknown keys: {sorted(unknown)}")
    base = spec.get("base")
    if not isinstance(base, dict):
        raise SchemaError(f"{mpath}.base", "matrix base must be a mapping")
    template = spec.get("id_template")
    if not isinstance(template, str) or not template:
        raise SchemaError(f"{mpath}.id_template", "id_template is required")
    axes = spec.get("axes", {})
    if not isinstance(axes, dict) or not axes:
        raise SchemaError(f"{mpath}.axes", "at least one axis is required")
    for axis, values in axes.items():
        if not isinstance(values, list) or not values:
            raise SchemaError(f"{mpath}.axes.{axis}", "axis values must be a non-empty list")

    scenarios = []
    paths = list(axes.keys())
    for combo in itertools.product(*(axes[p] for p in paths)):
        doc = copy.deepcopy(base)
        for path, value in zip(paths, combo):
            set_path(doc, path, copy.deepcopy(value))
        doc["id"] = _render_id(template, doc, mpath)
        scenarios.append(Scenario.from_dict(doc, path=f"{mpath}<{doc['id']}>"))
    return scenarios


def _iter_documents(path: Path) -> Iterable[tuple[Path, dict]]:
    if path.is_dir():
        files = sorted(p for p in path.glob("*.yaml")) + sorted(path.glob("*.yml"))
        if not files:
            raise SchemaError(str(path), "no .yaml files found in directory")
        for f in files:
            yield f, _load_yaml(f)
    else:
        yield path, _load_yaml(path)


def _load_yaml(path: Path) -> dict:
    try:
        doc = yaml.safe_load(path.read_text())
    except yaml.YAMLError as exc:
        raise SchemaError(str(path), f"invalid YAML: {exc}") from exc
    if doc is None:
        doc = {}
    if not isinstance(doc, dict):
        raise SchemaError(str(path), "top level must be a mapping")
    unknown = set(doc) - {"scenarios", "matrices"}
    if unknown:
        raise SchemaError(str(path), f"unknown top-level keys: {sorted(unknown)}")
    return doc


def load_scenarios(path: str | Path) -> list[Scenario]:
    """Load scenarios from a YAML file or a directory of YAML files."""
    path = Path(path)
    if not path.exists():
        raise SchemaError(str(path), "file or directory not found")

    scenarios: list[Scenario] = []
    for src, doc in _iter_documents(path):
        for i, raw in enumerate(doc.get("scenarios", []) or []):
            scenarios.append(Scenario.from_dict(raw, path=f"{src}:scenarios[{i}]"))
        for i, matrix in enumerate(doc.get("matrices", []) or []):
            scenarios.extend(expand_matrix(matrix, mpath=f"{src}:matrices[{i}]"))

    seen: dict[str, str] = {}
    for sc in scenarios:
        if sc.id in seen:
            raise SchemaError(sc.id, "duplicate scenario id")
        seen[sc.id] = sc.id
    return scenarios
