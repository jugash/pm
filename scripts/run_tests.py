#!/usr/bin/env python3
"""Zero-dependency test + line-coverage runner.

Runs the unittest suite under a ``sys.settrace`` line tracer and compares
executed lines against executable lines derived from compiled code objects
(``co_lines``), honoring ``# pragma: no cover`` block exclusions via ast.

This exists because PerfBench must be verifiable on minimal hosts without
pytest/coverage installed; with the dev extras installed, ``make pytest``
runs the same suite under pytest-cov.

Exit codes: 0 ok, 2 test failures, 3 coverage below threshold.
"""

from __future__ import annotations

import argparse
import ast
import sys
import threading
import unittest
from collections import defaultdict
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
SRC = ROOT / "src"
PKG = SRC / "perfbench"

sys.path.insert(0, str(SRC))
sys.path.insert(0, str(ROOT))

_BLOCK_NODES = (
    ast.FunctionDef,
    ast.AsyncFunctionDef,
    ast.ClassDef,
    ast.If,
    ast.For,
    ast.While,
    ast.Try,
    ast.With,
)


def executable_lines(path: Path) -> set[int]:
    source = path.read_text()
    code = compile(source, str(path), "exec")
    lines: set[int] = set()
    stack = [code]
    while stack:
        obj = stack.pop()
        for _start, _end, lineno in obj.co_lines():
            if lineno is not None:
                lines.add(lineno)
        for const in obj.co_consts:
            if type(const).__name__ == "code":
                stack.append(const)

    # pragma exclusions: a pragma on a block-opening line excludes the block
    src_lines = source.splitlines()
    pragma_linenos = {
        i + 1 for i, line in enumerate(src_lines) if "pragma: no cover" in line
    }
    excluded: set[int] = set(pragma_linenos)
    if pragma_linenos:
        tree = ast.parse(source)
        for node in ast.walk(tree):
            if (
                isinstance(node, _BLOCK_NODES)
                and getattr(node, "lineno", None) in pragma_linenos
            ):
                excluded.update(range(node.lineno, node.end_lineno + 1))
    return lines - excluded


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--fail-under", type=float, default=90.0)
    parser.add_argument("--pattern", default="test_*.py")
    args = parser.parse_args()

    hits: dict[str, set[int]] = defaultdict(set)
    src_prefix = str(PKG)

    def tracer(frame, event, arg):
        if event == "line":
            filename = frame.f_code.co_filename
            if filename.startswith(src_prefix):
                hits[filename].add(frame.f_lineno)
        return tracer

    threading.settrace(tracer)
    sys.settrace(tracer)
    try:
        loader = unittest.TestLoader()
        suite = loader.discover(str(ROOT / "tests"), pattern=args.pattern,
                                top_level_dir=str(ROOT))
        runner = unittest.TextTestRunner(verbosity=1, buffer=True)
        result = runner.run(suite)
    finally:
        sys.settrace(None)
        threading.settrace(None)  # type: ignore[arg-type]

    if not result.wasSuccessful():
        return 2

    total_exec = 0
    total_hit = 0
    report_rows = []
    for path in sorted(PKG.rglob("*.py")):
        exec_lines = executable_lines(path)
        hit_lines = hits.get(str(path), set()) & exec_lines
        missed = sorted(exec_lines - hit_lines)
        total_exec += len(exec_lines)
        total_hit += len(hit_lines)
        pct = 100.0 * len(hit_lines) / len(exec_lines) if exec_lines else 100.0
        report_rows.append((str(path.relative_to(ROOT)), len(exec_lines), pct, missed))

    print(f"\n{'file':56} {'lines':>6} {'cover':>7}  missing")
    print("-" * 100)
    for name, count, pct, missed in report_rows:
        missing = _compact(missed) if pct < 100 else ""
        print(f"{name:56} {count:6d} {pct:6.1f}%  {missing[:60]}")
    total_pct = 100.0 * total_hit / total_exec if total_exec else 100.0
    print("-" * 100)
    print(f"{'TOTAL':56} {total_exec:6d} {total_pct:6.1f}%")

    if total_pct < args.fail_under:
        print(f"\nFAIL: coverage {total_pct:.1f}% is below threshold {args.fail_under}%")
        return 3
    print(f"\nOK: coverage {total_pct:.1f}% >= {args.fail_under}%")
    return 0


def _compact(lines: list[int]) -> str:
    """Compress [1,2,3,7] -> '1-3,7'."""
    if not lines:
        return ""
    out = []
    start = prev = lines[0]
    for n in lines[1:]:
        if n == prev + 1:
            prev = n
            continue
        out.append(f"{start}-{prev}" if start != prev else str(start))
        start = prev = n
    out.append(f"{start}-{prev}" if start != prev else str(start))
    return ",".join(out)


if __name__ == "__main__":
    sys.exit(main())
