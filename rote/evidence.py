"""Evidence: every run leaves a trail a human can debug from.

Each run gets ``runs/<run_id>/`` containing:

* ``events.jsonl`` - structured log of everything the system observed,
  decided, and did, with the *why* attached. Every string is passed through
  the policy redactor before it touches disk.
* ``*.png`` - screenshots at key moments and always on failure.
* ``*.txt`` / ``*.json`` - snapshots, transcripts, and the final result.

The curated copies that ship in the repo's ``/evidence/`` directory are made
from these run directories.
"""

from __future__ import annotations

import json
import secrets
from datetime import datetime, timezone
from pathlib import Path
from typing import Callable


def _now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="milliseconds")


class RunLog:
    def __init__(self, runs_root: str | Path, kind: str, redact: Callable[[str], str]):
        stamp = datetime.now(timezone.utc).strftime("%Y%m%d-%H%M%S")
        self.run_id = f"{kind}-{stamp}-{secrets.token_hex(2)}"
        self.dir = Path(runs_root) / self.run_id
        self.dir.mkdir(parents=True, exist_ok=True)
        self._redact = redact
        self._events = self.dir / "events.jsonl"
        self._shot_counter = 0

    # -- structured log ------------------------------------------------------

    def event(self, event: str, **fields: object) -> None:
        record = {"ts": _now(), "event": event, **self._clean(fields)}
        with self._events.open("a") as f:
            f.write(json.dumps(record, ensure_ascii=False) + "\n")

    def _clean(self, value: object) -> object:
        if isinstance(value, str):
            return self._redact(value)
        if isinstance(value, dict):
            return {k: self._clean(v) for k, v in value.items()}
        if isinstance(value, (list, tuple)):
            return [self._clean(v) for v in value]
        return value

    # -- artifacts -----------------------------------------------------------

    def screenshot(self, surface, label: str) -> str:
        self._shot_counter += 1
        path = self.dir / f"{self._shot_counter:02d}-{label}.png"
        try:
            surface.screenshot(str(path))
        except Exception as exc:  # a dead browser must not mask the real failure
            self.event("screenshot_failed", label=label, error=str(exc))
            return ""
        self.event("screenshot", label=label, path=path.name)
        return str(path)

    def save_text(self, name: str, text: str) -> str:
        path = self.dir / name
        path.write_text(self._redact(text))
        return str(path)

    def save_json(self, name: str, data: object) -> str:
        if hasattr(data, "model_dump_json"):
            text = data.model_dump_json(indent=2)
        else:
            text = json.dumps(data, indent=2, ensure_ascii=False)
        return self.save_text(name, text + "\n")
