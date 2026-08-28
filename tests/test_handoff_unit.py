"""Unit tests for intervention command binding (rote/handoff.py).

Regression focus: a command left over from before an intervention - or
stamped for a different one - must never resolve it. The worst case was a
stale Resume counting as human approval of an irreversible step.
"""

from __future__ import annotations

import json
import threading
import time

from rote.evidence import RunLog
from rote.handoff import InterventionHub, send_command

from fakes import FakeSurface, make_policy


def _hub(tmp_path, timeout_s: float):
    surface = FakeSurface(make_policy())
    log = RunLog(tmp_path / "runs", "replay", surface.policy.redact)
    return InterventionHub(surface, log, timeout_s=timeout_s, poll_s=0.05), log


def test_leftover_resume_does_not_resolve_the_next_intervention(tmp_path):
    hub, log = _hub(tmp_path, timeout_s=0.5)
    send_command(log.dir, {"kind": "resume"})  # written before any intervention exists

    resolution = hub.intervene(reason="test", step_id="s1")

    assert not resolution.resumed and resolution.outcome == "timeout"
    assert "stale_command_discarded" in (log.dir / "events.jsonl").read_text()


def test_command_stamped_for_another_intervention_is_ignored(tmp_path):
    hub, log = _hub(tmp_path, timeout_s=5)
    context = log.dir / "handoff" / "context.json"

    def operator() -> None:
        for _ in range(100):
            try:
                iid = json.loads(context.read_text())["intervention"]
                break
            except (OSError, json.JSONDecodeError, KeyError):
                time.sleep(0.03)
        send_command(log.dir, {"kind": "resume"}, intervention="iv999")  # someone else's
        time.sleep(0.2)
        send_command(log.dir, {"kind": "resume"}, intervention=iid)

    threading.Thread(target=operator, daemon=True).start()
    resolution = hub.intervene(reason="test", step_id="s1")

    assert resolution.resumed
    assert "stale_command_ignored" in (log.dir / "events.jsonl").read_text()
