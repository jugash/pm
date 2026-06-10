"""Push finished run records to the exporter's ingest API."""

from __future__ import annotations

import json
import urllib.error
import urllib.request
from typing import Any

from perfbench.errors import ExecutionError


def push_run(url: str, record: dict[str, Any], timeout: float = 10.0) -> dict[str, Any]:
    """POST one run record to ``{url}/api/v1/ingest``."""
    endpoint = url.rstrip("/") + "/api/v1/ingest"
    data = json.dumps(record).encode()
    request = urllib.request.Request(
        endpoint, data=data, headers={"Content-Type": "application/json"}, method="POST"
    )
    try:
        with urllib.request.urlopen(request, timeout=timeout) as response:
            return json.loads(response.read().decode())
    except urllib.error.HTTPError as exc:
        body = exc.read().decode(errors="replace")[:300]
        raise ExecutionError(f"ingest failed: HTTP {exc.code}: {body}") from exc
    except urllib.error.URLError as exc:
        raise ExecutionError(f"cannot reach exporter at {endpoint}: {exc.reason}") from exc
