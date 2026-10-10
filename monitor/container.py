from __future__ import annotations

import json
import subprocess
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from .sli import append_jsonl


def capture_container_metrics(metrics_dir: Path | str, *, project: str = "ditto") -> list[dict[str, Any]]:
    """Capture one local Docker resource snapshot without changing containers."""
    command = [
        "docker", "stats", "--no-stream",
        "--format", "{{json .}}",
    ]
    result = subprocess.run(command, capture_output=True, text=True, check=False)
    if result.returncode != 0:
        raise RuntimeError(f"Docker stats failed: {result.stderr.strip() or 'unknown error'}")
    captured_at = datetime.now(timezone.utc).isoformat(timespec="milliseconds").replace("+00:00", "Z")
    records: list[dict[str, Any]] = []
    for line in result.stdout.splitlines():
        try:
            raw = json.loads(line)
        except json.JSONDecodeError:
            continue
        name = str(raw.get("Name") or "")
        if not name or not name.startswith(f"{project}-"):
            continue
        record = {
            "kind": "container",
            "source": "live",
            "timestamp": captured_at,
            "container": name,
            "cpu_percent": raw.get("CPUPerc"),
            "memory_usage": raw.get("MemUsage"),
            "memory_percent": raw.get("MemPerc"),
            "net_io": raw.get("NetIO"),
            "block_io": raw.get("BlockIO"),
        }
        append_jsonl(Path(metrics_dir) / "container.jsonl", record)
        records.append(record)
    return records
