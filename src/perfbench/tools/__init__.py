"""Benchmark tool adapters.

Importing this package registers all built-in adapters.
"""

from perfbench.tools.base import (  # noqa: F401
    ToolAdapter,
    available_tools,
    create_tool,
    register,
)

# Register built-in adapters (import side effects are intentional).
from perfbench.tools import (  # noqa: F401,E402
    cyclictest,
    eflatency,
    iperf3,
    netperf,
    sfnt_pingpong,
    sfnt_stream,
    sockperf,
    sysjitter,
)
