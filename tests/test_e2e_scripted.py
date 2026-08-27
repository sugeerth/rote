"""End-to-end pipeline test: real browser, real target app, procedural model.

Exercises the full thread the take-home asks for - discovery -> artifact ->
deterministic replay -> error taxonomy -> escalation/handoff - with the LLM
seam filled by a deterministic stand-in, so it runs in CI with no model cost.
The genuine LLM-driven discovery run lives in /evidence/.
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
import threading
import time
import urllib.request
from pathlib import Path

import pytest

from rote.discover import discover
from rote.distill import AppProfile, distill
from rote.evidence import RunLog
from rote.handoff import InterventionHub, send_command
from rote.policy import Policy
from rote.replay import ReplayOptions, replay
from rote.schema import Capability
from rote.surface import WebSurface
from tests.procedural_llm import ProceduralLLM

BUILD = Path(__file__).resolve().parent.parent
PORT = 7719
BASE = f"http://127.0.0.1:{PORT}"


def _policy() -> Policy:
    return Policy({
        "allowlist": {"origins": [BASE], "denied_paths": ["/admin/", "/debug/"]},
        "actions": {"allowed": ["navigate", "click", "fill", "select", "press", "read"]},
        "risk": {
            "irreversible_name_pattern": r"(?i)\b(confirm|post|delete|purge|transfer|approve)\b",
            "unattended_irreversible": "escalate",
        },
        "secrets": {"teller_password": {"env": "ROTE_TELLER_PASSWORD"}},
        "redaction": {"patterns": [
            {"name": "ssn", "regex": r"\b\d{3}-\d{2}-\d{4}\b", "replacement": "***-**-****"},
        ]},
    })


@pytest.fixture(scope="module", autouse=True)
def secret_env():
    os.environ.setdefault("ROTE_TELLER_PASSWORD", "spring2026-demo")


@pytest.fixture(scope="module")
def target_server():
    env = {**os.environ, "ROTE_TARGET_PORT": str(PORT)}
    proc = subprocess.Popen(
        [sys.executable, "-m", "target_app.app"], cwd=BUILD, env=env,
        stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
    )
    try:
        _wait_healthy()
        yield
    finally:
        proc.terminate()
        proc.wait(timeout=10)


def _wait_healthy(timeout: float = 20) -> None:
    deadline = time.time() + timeout
    while time.time() < deadline:
        try:
            with urllib.request.urlopen(f"{BASE}/healthz", timeout=1) as resp:
                if resp.status == 200:
                    return
        except OSError:
            time.sleep(0.3)
    raise RuntimeError("target app did not come up")


def _arm_fault(kind: str) -> None:
    urllib.request.urlopen(f"{BASE}/debug/fault", data=f"fault={kind}".encode()).read()


@pytest.fixture(scope="module")
def recorded(target_server, tmp_path_factory):
    """One procedural discovery run + distillation, shared by the replay tests."""
    tmp = tmp_path_factory.mktemp("recorded")
    policy = _policy()
    profile = AppProfile.load(BUILD / "config/apps/cornerstone-teller.yaml")
    profile.entry_url = f"{BASE}/login"
    surface = WebSurface(policy)
    surface.start()
    try:
        log = RunLog(tmp / "runs", "discovery", redact=policy.redact)
        result, trace = discover(
            goal="Look up member 12345 and read their current savings balance",
            entry_url=profile.entry_url,
            surface=surface,
            llm=ProceduralLLM(),
            log=log,
            output_names=["savings_balance"],
            secrets=profile.secrets,
            hub=None,
            max_steps=15,
            timeout_s=120,
        )
        assert result.status == "success", result.summary()
        assert result.outputs == {"savings_balance": "$4,982.17"}
        capability = distill(
            trace, surface, profile,
            capability_id="lookup-member-savings",
            name="Look up member savings balance",
            description="Given a member number, return the member's current Regular Savings balance",
            params={"member_id": "12345"},
            outputs=[("savings_balance", "money")],
        )
        cap_path = capability.save(tmp / "capabilities" / "lookup-member-savings.json")
    finally:
        surface.close()
    return {"cap_path": cap_path, "tmp": tmp}


def _replay(recorded, params, hub_factory=None, **opts):
    policy = _policy()
    capability = Capability.load(recorded["cap_path"])
    log = RunLog(recorded["tmp"] / "runs", "replay", redact=policy.redact)
    surface = WebSurface(policy)
    surface.start()
    try:
        hub = hub_factory(surface, log) if hub_factory else None
        result = replay(capability, params, surface, log, hub=hub, options=ReplayOptions(**opts))
        return result, log
    finally:
        surface.close()


# ---------------------------------------------------------------------------


def test_artifact_shape(recorded):
    text = recorded["cap_path"].read_text()
    assert "{{param:member_id}}" in text
    assert "{{secret:teller_password}}" in text
    assert "spring2026-demo" not in text  # the secret value never reaches disk

    cap = Capability.load(recorded["cap_path"])
    assert cap.review.status == "draft"
    assert any(s.value == "{{param:member_id}}" for s in cap.steps if s.action == "fill")
    for step in cap.steps:
        if step.action == "click":
            strategies = [loc.strategy for loc in step.target.locators]
            assert strategies[-1] == "dom_path"
            assert strategies[0] in ("role", "label_near")
    last = cap.steps[-1]
    assert last.extract and last.extract[0].target.locators[0].strategy == "table_cell"
    assert {d.kind for d in cap.detectors} == {"business", "recoverable", "fatal"}


def test_replay_success(recorded):
    result, _ = _replay(recorded, {"member_id": "12345"})
    assert result.status == "success", result.summary()
    assert result.outputs["savings_balance"] == pytest.approx(4982.17)


def test_replay_business_outcome_not_found(recorded):
    result, _ = _replay(recorded, {"member_id": "99999"})
    assert result.status == "business_outcome"
    assert result.outcome_code == "member_not_found"


def test_replay_rejects_bad_param_before_touching_ui(recorded):
    result, _ = _replay(recorded, {"member_id": "abc"})
    assert result.status == "hard_failure"
    assert result.failure.kind == "invalid_args"
    assert "does not match" in result.failure.observed


def test_replay_recovers_from_interstitial(recorded, target_server):
    _arm_fault("notice_next")
    result, log = _replay(recorded, {"member_id": "12345"})
    assert result.status == "success", result.summary()
    events = (log.dir / "events.jsonl").read_text()
    assert '"recovery_applied"' in events and "daily_notice" in events


def test_replay_recovers_from_session_expiry(recorded, target_server):
    _arm_fault("timeout_next")
    result, log = _replay(recorded, {"member_id": "12345"})
    assert result.status == "success", result.summary()
    events = (log.dir / "events.jsonl").read_text()
    assert "session_expired" in events and '"recovery_resume"' in events
    assert any(s.status == "recovered" for s in result.steps)


def test_replay_fatal_app_error(recorded, target_server):
    _arm_fault("error500")
    result, _ = _replay(recorded, {"member_id": "12345"})
    assert result.status == "hard_failure"
    assert result.failure.kind == "app_error"
    assert result.failure.step_id is not None
    assert result.failure.evidence.get("screenshot")


def test_replay_honest_failure_when_member_has_no_savings(recorded):
    result, _ = _replay(recorded, {"member_id": "20001"})
    assert result.status == "hard_failure"
    assert result.failure.kind == "target_not_found"


def test_escalation_human_fixes_and_resumes(recorded, target_server):
    _arm_fault("error500")

    def hub_factory(surface, log):
        hub = InterventionHub(surface, log, timeout_s=60)

        def operator():
            control = log.dir / "handoff" / "control.json"
            deadline = time.time() + 30
            while time.time() < deadline:
                try:
                    if json.loads(control.read_text())["controller"] == "human":
                        break
                except (OSError, json.JSONDecodeError):
                    pass
                time.sleep(0.3)
            send_command(log.dir, {"kind": "act", "action": "navigate", "value": f"{BASE}/app"})
            send_command(log.dir, {"kind": "resume"})

        threading.Thread(target=operator, daemon=True).start()
        return hub

    result, log = _replay(recorded, {"member_id": "12345"}, hub_factory=hub_factory)
    assert result.status == "success", result.summary()
    assert len(result.interventions) == 1
    intervention = result.interventions[0]
    assert intervention.resolution == "resumed"
    assert any(a.kind == "navigate" for a in intervention.human_actions)
    events = (log.dir / "events.jsonl").read_text()
    assert '"escalation_raised"' in events
    assert '"step_completed_by_human"' in events
