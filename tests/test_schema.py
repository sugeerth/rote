"""Unit tests for the capability artifact contract (rote/schema.py)."""

from __future__ import annotations

import json

import pytest
from pydantic import ValidationError

from rote.schema import (
    Capability,
    Contract,
    Detector,
    Extraction,
    OutputSpec,
    ParamSpec,
    Recovery,
    SafetyMeta,
    Step,
)

from fakes import (
    ORIGIN,
    ckpt,
    click_step,
    cond,
    fill_step,
    make_capability,
    nav_step,
    target,
)


# ---------------------------------------------------------------------------
# serialization round-trip
# ---------------------------------------------------------------------------


def test_minimal_valid_capability_round_trips(tmp_path):
    cap = make_capability()
    path = cap.save(tmp_path / "caps" / "member-balance.json")

    assert path.exists()
    raw = json.loads(path.read_text())
    assert raw["schema_version"] == "1.0"
    assert raw["id"] == "member-balance"

    loaded = Capability.load(path)
    assert loaded == cap


# ---------------------------------------------------------------------------
# cross-reference validators
# ---------------------------------------------------------------------------


def test_duplicate_step_ids_rejected():
    with pytest.raises(ValidationError, match="duplicate step ids"):
        make_capability(steps=[nav_step("s1"), nav_step("s1", url=f"{ORIGIN}/other")])


def test_extraction_must_reference_declared_output():
    ghost = Extraction(output="ghost", target=target("the ghost cell"), parse="text")
    steps = [nav_step("s1"), click_step("s2", extract=[ghost])]
    with pytest.raises(ValidationError, match="undeclared output 'ghost'"):
        make_capability(steps=steps)


def test_business_detector_code_must_be_a_declared_outcome():
    det = Detector(
        code="mystery_outcome",
        kind="business",
        description="not declared in contract.outcomes",
        condition=cond("Nope"),
    )
    with pytest.raises(ValidationError, match="missing from contract.outcomes"):
        make_capability(detectors=[det])


def test_recoverable_detector_requires_a_recovery():
    with pytest.raises(ValidationError, match="must declare a recovery"):
        Detector(
            code="session_expired",
            kind="recoverable",
            description="session timed out",
            condition=cond("Session expired"),
        )


@pytest.mark.parametrize("kind", ["business", "fatal"])
def test_business_and_fatal_detectors_must_not_carry_a_recovery(kind):
    recovery = Recovery(kind="retry_step", max_attempts=1)
    with pytest.raises(ValidationError, match="must not declare a recovery"):
        Detector(
            code="member_not_found",
            kind=kind,
            description="a terminal state",
            condition=cond("No member"),
            recovery=recovery,
        )


def test_resume_from_step_must_point_at_a_known_step():
    det = Detector(
        code="session_expired",
        kind="recoverable",
        description="session timed out",
        condition=cond("Session expired"),
        recovery=Recovery(kind="resume_from_step", from_step="s99"),
    )
    with pytest.raises(ValidationError, match="resumes from unknown step s99"):
        make_capability(detectors=[det])


def test_irreversible_step_ids_must_match_step_risk_markings():
    steps = [nav_step("s1"), click_step("s2", "the Confirm button", "Confirm", risk="irreversible")]
    # marked on the step but not declared in safety
    with pytest.raises(ValidationError, match="out of sync"):
        make_capability(steps=steps, safety=SafetyMeta(origins=[ORIGIN]))
    # declared in safety but no step is marked
    with pytest.raises(ValidationError, match="out of sync"):
        make_capability(safety=SafetyMeta(origins=[ORIGIN], irreversible_step_ids=["s3"]))
    # in sync: constructs fine
    cap = make_capability(steps=steps, safety=SafetyMeta(origins=[ORIGIN], irreversible_step_ids=["s2"]))
    assert cap.safety.irreversible_step_ids == ["s2"]


def test_step_value_referencing_undeclared_param_rejected():
    steps = [nav_step("s1"), fill_step("s2", value="{{param:ghost}}")]
    with pytest.raises(ValidationError, match="undeclared param 'ghost'"):
        make_capability(steps=steps)


def test_step_value_referencing_undeclared_secret_rejected():
    steps = [nav_step("s1"), fill_step("s2", value="{{secret:mystery}}")]
    # safety.requires_secrets does not list 'mystery'
    with pytest.raises(ValidationError, match="undeclared secret 'mystery'"):
        make_capability(steps=steps)


def test_raw_credential_value_rejected():
    steps = [nav_step("s1"), fill_step("s2", value="hunter2password")]
    with pytest.raises(ValidationError, match="raw credential"):
        make_capability(steps=steps)


def test_secret_placeholder_value_is_allowed():
    steps = [nav_step("s1"), fill_step("s2", value="{{secret:teller_password}}")]
    cap = make_capability(
        steps=steps,
        safety=SafetyMeta(origins=[ORIGIN], requires_secrets=["teller_password"]),
    )
    assert cap.safety.requires_secrets == ["teller_password"]


def test_fill_step_requires_a_target():
    with pytest.raises(ValidationError, match="fill requires a target"):
        Step(id="s2", intent="type something", action="fill", value="x")


def test_navigate_step_requires_a_url():
    with pytest.raises(ValidationError, match="navigate requires url"):
        Step(id="s1", intent="open the app", action="navigate")


# ---------------------------------------------------------------------------
# Contract.validate_args
# ---------------------------------------------------------------------------


def _contract() -> Contract:
    return Contract(
        params=[
            ParamSpec(name="member_id", type="string", description="member number", pattern=r"\d+"),
            ParamSpec(name="limit", type="integer", description="row limit", required=False),
        ],
        outputs=[OutputSpec(name="balance", type="money", description="share balance")],
    )


def test_validate_args_rejects_unknown_param():
    with pytest.raises(ValueError, match="unknown params"):
        _contract().validate_args({"member_id": "12345", "bogus": "x"})


def test_validate_args_rejects_missing_required():
    with pytest.raises(ValueError, match="missing required params"):
        _contract().validate_args({})


def test_validate_args_rejects_pattern_mismatch():
    with pytest.raises(ValueError, match="does not match"):
        _contract().validate_args({"member_id": "abc"})


def test_validate_args_rejects_wrong_type():
    with pytest.raises(ValueError, match="expects integer"):
        _contract().validate_args({"member_id": "12345", "limit": "10"})
    # bool is not an acceptable integer even though bool subclasses int
    with pytest.raises(ValueError, match="expects integer"):
        _contract().validate_args({"member_id": "12345", "limit": True})


def test_validate_args_happy_path_returns_normalized_strings():
    normalized = _contract().validate_args({"member_id": "12345", "limit": 10})
    assert normalized == {"member_id": "12345", "limit": "10"}
    assert all(isinstance(v, str) for v in normalized.values())
    # optional param may be omitted entirely
    assert _contract().validate_args({"member_id": "7"}) == {"member_id": "7"}
