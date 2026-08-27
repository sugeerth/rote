"""Unit tests for the deterministic replay engine's error taxonomy (rote/replay.py).

Driven entirely through a FakeSurface (tests/fakes.py) with a scripted state
model; the real Policy and a real RunLog (into tmp_path) are used throughout.
"""

from __future__ import annotations

import pytest

from rote.evidence import RunLog
from rote.replay import ReplayOptions, replay
from rote.schema import Contract, Detector, Recovery, SafetyMeta
from rote.surface import TargetNotFound

import fakes
from fakes import ORIGIN, FakeSurface, ckpt, click_step, cond, make_capability, nav_step, react, target

LOGIN_URL = f"{ORIGIN}/login"


def run(cap, surface, tmp_path, args=None, options=None, hub=None):
    log = RunLog(tmp_path / "runs", "replay", surface.policy.redact)
    result = replay(
        cap,
        {"member_id": "12345"} if args is None else args,
        surface,
        log,
        hub=hub,
        options=options,
    )
    return result, log


def events_text(log: RunLog) -> str:
    return (log.dir / "events.jsonl").read_text()


# -- detector builders -------------------------------------------------------


def business_not_found():
    return Detector(
        code="member_not_found",
        kind="business",
        description="No member matches the id",
        condition=cond("No member matches"),
    )


def recoverable_click(max_attempts=1):
    return Detector(
        code="session_reminder",
        kind="recoverable",
        description="Session timeout reminder interstitial",
        condition=cond("Session Reminder"),
        recovery=Recovery(
            kind="click",
            target=target("the OK button", name="OK"),
            max_attempts=max_attempts,
            backoff_ms=0,
        ),
    )


def recoverable_retry(max_attempts=1):
    return Detector(
        code="rate_limited",
        kind="recoverable",
        description="Please-wait throttle screen",
        condition=cond("Please wait"),
        recovery=Recovery(kind="retry_step", max_attempts=max_attempts, backoff_ms=0),
    )


def recoverable_resume():
    return Detector(
        code="session_expired",
        kind="recoverable",
        description="The session expired; log in again",
        condition=cond("Session expired"),
        recovery=Recovery(kind="resume_from_step", from_step="s1", max_attempts=1, backoff_ms=0),
    )


def fatal_error_page():
    return Detector(
        code="app_crash",
        kind="fatal",
        description="The application returned an error page",
        condition=cond("Server Error"),
    )


# ---------------------------------------------------------------------------
# 1. happy path
# ---------------------------------------------------------------------------


def test_happy_path_success_with_outputs(tmp_path):
    cap = make_capability()
    surface = FakeSurface(
        fakes.make_policy(),
        reactions={"the Search button": react(add=["Member Detail"])},
        reads={"the balance cell": 4982.17},
    )
    result, _log = run(cap, surface, tmp_path)

    assert result.status == "success"
    assert result.outcome_code is None
    assert result.failure is None
    assert result.outputs == {"balance": 4982.17}
    assert [(r.step_id, r.status) for r in result.steps] == [("s1", "ok"), ("s2", "ok"), ("s3", "ok")]


# ---------------------------------------------------------------------------
# 2. business outcome stops the run
# ---------------------------------------------------------------------------


def test_business_outcome_terminates_run(tmp_path):
    steps = fakes.search_steps() + [click_step("s4", "the Detail link", name="Detail", checkpoint_value=None)]
    cap = make_capability(steps=steps, detectors=[business_not_found()])
    surface = FakeSurface(
        fakes.make_policy(),
        reactions={"the Search button": react(add=["No member matches"])},
    )
    result, _log = run(cap, surface, tmp_path)

    assert result.status == "business_outcome"
    assert result.outcome_code == "member_not_found"
    assert result.outcome_message == "No member matches the id"
    assert result.failure is None
    assert [(r.step_id, r.status) for r in result.steps] == [
        ("s1", "ok"),
        ("s2", "ok"),
        ("s3", "business_outcome"),
    ]
    # the run stopped: s4 was never acted on
    assert surface.acted_on("the Detail link") == []


# ---------------------------------------------------------------------------
# 3. recoverable interstitial dismissed by a declared click
# ---------------------------------------------------------------------------


def test_recoverable_click_recovery_leads_to_success(tmp_path):
    cap = make_capability(detectors=[recoverable_click()])
    surface = FakeSurface(
        fakes.make_policy(),
        truths=["Session Reminder"],  # interstitial covering the screen
        reactions={"the OK button": react(add=["Member Detail"], remove=["Session Reminder"])},
        reads={"the balance cell": 10.0},
    )
    result, log = run(cap, surface, tmp_path)

    assert result.status == "success"
    assert result.outputs == {"balance": 10.0}
    # step succeeded after the declared recovery click
    assert [(r.step_id, r.status) for r in result.steps] == [("s1", "ok"), ("s2", "ok"), ("s3", "ok")]
    acted = [a for a in surface.actions if a[0] == "click"]
    assert acted == [("click", "the Search button", None), ("click", "the OK button", None)]
    assert '"recovery_applied"' in events_text(log)
    assert "session_reminder" in events_text(log)


# ---------------------------------------------------------------------------
# 4. recoverable condition recurring past its budget
# ---------------------------------------------------------------------------


def test_recovery_exhausted_when_condition_keeps_recurring(tmp_path):
    cap = make_capability(detectors=[recoverable_retry(max_attempts=1)])
    surface = FakeSurface(fakes.make_policy(), truths=["Please wait"])  # never clears
    result, _log = run(cap, surface, tmp_path)

    assert result.status == "hard_failure"
    assert result.failure is not None
    assert result.failure.kind == "recovery_exhausted"
    assert result.failure.step_id == "s3"
    assert "rate_limited" in result.failure.expected
    # one original attempt plus one declared retry
    assert len(surface.acted_on("the Search button")) == 2


# ---------------------------------------------------------------------------
# 5. resume_from_step
# ---------------------------------------------------------------------------


def test_resume_from_step_reexecutes_and_succeeds(tmp_path):
    cap = make_capability(detectors=[recoverable_resume()])
    clicks = []

    def search_reaction(s):
        clicks.append(1)
        if len(clicks) == 1:
            s.truths.add("Session expired")  # first attempt lands on the expiry screen
        else:
            s.truths.discard("Session expired")
            s.truths.add("Member Detail")

    surface = FakeSurface(
        fakes.make_policy(),
        reactions={"the Search button": search_reaction},
        reads={"the balance cell": 55.5},
    )
    result, log = run(cap, surface, tmp_path)

    assert result.status == "success"
    assert result.outputs == {"balance": 55.5}
    # the failed pass is reported as recovered, noting the resume target
    assert [(r.step_id, r.status) for r in result.steps] == [
        ("s1", "ok"),
        ("s2", "ok"),
        ("s3", "recovered"),
        ("s1", "ok"),
        ("s2", "ok"),
        ("s3", "ok"),
    ]
    recovered = result.steps[2]
    assert recovered.note == "resuming from s1"
    # replay actually jumped back: the login navigation ran twice
    assert len(surface.acted_on(LOGIN_URL)) == 2
    assert '"recovery_resume"' in events_text(log)


# ---------------------------------------------------------------------------
# 6. fatal detector
# ---------------------------------------------------------------------------


def test_fatal_detector_is_a_hard_failure_app_error(tmp_path):
    cap = make_capability(detectors=[fatal_error_page()])
    surface = FakeSurface(
        fakes.make_policy(),
        reactions={"the Search button": react(add=["Server Error"])},
    )
    result, _log = run(cap, surface, tmp_path)

    assert result.status == "hard_failure"
    assert result.failure.kind == "app_error"
    assert result.failure.step_id == "s3"
    assert "The application returned an error page" in result.failure.observed


# ---------------------------------------------------------------------------
# 7. unknown state: no detector claims, checkpoint fails
# ---------------------------------------------------------------------------


def test_unknown_state_is_checkpoint_failed_with_expected_and_observed(tmp_path):
    cap = make_capability()  # no detectors at all
    surface = FakeSurface(fakes.make_policy(), truths=["Welcome Screen"])
    result, _log = run(cap, surface, tmp_path)

    assert result.status == "hard_failure"
    assert result.failure.kind == "checkpoint_failed"
    assert result.failure.step_id == "s3"
    assert result.failure.expected == cap.steps[2].checkpoint.description
    assert "Welcome Screen" in result.failure.observed
    assert result.failure.observed.startswith("url=")
    assert result.failure.evidence.get("screenshot")


# ---------------------------------------------------------------------------
# 8 + 9. the irreversible gate
# ---------------------------------------------------------------------------


def posting_capability():
    steps = [
        nav_step("s1"),
        click_step("s2", "the Confirm Post button", name="Confirm Post", checkpoint_value=None, risk="irreversible"),
    ]
    return make_capability(
        steps=steps,
        contract=Contract(),
        success=ckpt("Posted"),
        safety=SafetyMeta(origins=[ORIGIN], irreversible_step_ids=["s2"]),
    )


def test_irreversible_gate_blocks_unattended_replay_before_acting(tmp_path):
    cap = posting_capability()
    surface = FakeSurface(
        fakes.make_policy(),
        reactions={"the Confirm Post button": react(add=["Posted"])},
    )
    result, _log = run(cap, surface, tmp_path, args={})  # hub=None, allow_irreversible=False

    assert result.status == "hard_failure"
    assert result.failure.kind == "policy_violation"
    assert result.failure.step_id == "s2"
    # blocked BEFORE the action executed: no click ever reached the surface
    assert surface.acted_on("the Confirm Post button") == []
    assert [a[0] for a in surface.actions] == ["navigate"]


def test_irreversible_step_proceeds_when_caller_authorized(tmp_path):
    cap = posting_capability()
    surface = FakeSurface(
        fakes.make_policy(),
        reactions={"the Confirm Post button": react(add=["Posted"])},
    )
    result, _log = run(cap, surface, tmp_path, args={}, options=ReplayOptions(allow_irreversible=True))

    assert result.status == "success"
    assert surface.acted_on("the Confirm Post button") == [("click", "the Confirm Post button", None)]


# ---------------------------------------------------------------------------
# 10. preflight failures: fail fast, before touching the UI
# ---------------------------------------------------------------------------


def test_preflight_missing_required_arg(tmp_path):
    cap = make_capability()
    surface = FakeSurface(fakes.make_policy())
    result, _log = run(cap, surface, tmp_path, args={})

    assert result.status == "hard_failure"
    assert result.failure.kind == "invalid_args"
    assert result.failure.step_id is None
    assert "missing required params" in result.failure.observed
    assert surface.actions == []


def test_preflight_capability_origin_outside_allowlist(tmp_path):
    cap = make_capability(safety=SafetyMeta(origins=["http://evil.example.com"]))
    surface = FakeSurface(fakes.make_policy())
    result, _log = run(cap, surface, tmp_path)

    assert result.status == "hard_failure"
    assert result.failure.kind == "policy_violation"
    assert result.failure.step_id is None
    assert "not on the allowlist" in result.failure.observed
    assert surface.actions == []


def test_preflight_required_secret_unresolvable(tmp_path, monkeypatch):
    monkeypatch.delenv("ROTE_TELLER_PASSWORD", raising=False)
    cap = make_capability(safety=SafetyMeta(origins=[ORIGIN], requires_secrets=["teller_password"]))
    surface = FakeSurface(fakes.make_policy())
    result, _log = run(cap, surface, tmp_path)

    assert result.status == "hard_failure"
    assert result.failure.kind == "policy_violation"
    assert result.failure.step_id is None
    assert "ROTE_TELLER_PASSWORD" in result.failure.observed
    assert surface.actions == []


# ---------------------------------------------------------------------------
# 11. every locator fails and no detector claims the screen
# ---------------------------------------------------------------------------


def test_target_not_found_with_no_detector_claiming(tmp_path):
    cap = make_capability()  # no detectors
    surface = FakeSurface(
        fakes.make_policy(),
        act_errors={"the Search button": [TargetNotFound("all locators failed")]},
    )
    result, _log = run(cap, surface, tmp_path)

    assert result.status == "hard_failure"
    assert result.failure.kind == "target_not_found"
    assert result.failure.step_id == "s3"
    assert "all locators failed" in result.failure.expected
    # the act never executed
    assert surface.acted_on("the Search button") == []
