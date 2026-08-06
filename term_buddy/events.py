from __future__ import annotations

import fcntl
import json
import os
import time
import uuid
from pathlib import Path
from typing import Any


def append_event(path: Path, kind: str, payload: dict[str, Any]) -> dict[str, Any]:
    event = {
        "id": uuid.uuid4().hex,
        "time": time.time(),
        "kind": kind,
        **payload,
    }
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("a", encoding="utf-8") as handle:
        fcntl.flock(handle, fcntl.LOCK_EX)
        handle.write(json.dumps(event, ensure_ascii=False) + "\n")
        handle.flush()
        os.fsync(handle.fileno())
        fcntl.flock(handle, fcntl.LOCK_UN)
    return event


def read_events(path: Path, offset: int = 0) -> tuple[list[dict[str, Any]], int]:
    if not path.exists():
        return [], offset
    events: list[dict[str, Any]] = []
    with path.open("r", encoding="utf-8", errors="replace") as handle:
        handle.seek(offset)
        for line in handle:
            try:
                events.append(json.loads(line))
            except json.JSONDecodeError:
                continue
        return events, handle.tell()

