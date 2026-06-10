"""Exception hierarchy for PerfBench."""

from __future__ import annotations


class PerfBenchError(Exception):
    """Base class for all PerfBench errors."""


class SchemaError(PerfBenchError):
    """A scenario document failed validation.

    Carries the document path (dotted) where validation failed so users can
    locate the offending YAML node quickly.
    """

    def __init__(self, path: str, message: str):
        self.path = path
        self.message = message
        super().__init__(f"{path}: {message}" if path else message)


class ParseError(PerfBenchError):
    """A benchmark tool produced output the parser could not understand."""


class ExecutionError(PerfBenchError):
    """A command failed to execute on a target host."""


class PreflightError(PerfBenchError):
    """Fatal preflight validation failures; the run was aborted."""

    def __init__(self, failures):
        self.failures = list(failures)
        details = "; ".join(f"{f.name}: {f.message}" for f in self.failures)
        super().__init__(f"preflight failed: {details}")
