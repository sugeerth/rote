"""Unit tests for the guardrail engine (rote/policy.py)."""

from __future__ import annotations

from pathlib import Path

import pytest

from rote.policy import Policy, PolicyViolation

from fakes import ORIGIN, make_policy

SHIPPED_CONFIG = Path(__file__).resolve().parent.parent / "config" / "policy.yaml"


# ---------------------------------------------------------------------------
# allowlist
# ---------------------------------------------------------------------------


def test_allowlisted_origin_ok():
    decision = make_policy().check_url(f"{ORIGIN}/member/search")
    assert decision.allowed
    assert decision.risk == "safe"


def test_foreign_origin_blocked():
    decision = make_policy().check_url("http://evil.example.com/member/search")
    assert not decision.allowed
    assert "not on the allowlist" in decision.reason


def test_denied_path_prefix_blocked_even_on_allowlisted_origin():
    decision = make_policy().check_url(f"{ORIGIN}/admin/panel")
    assert not decision.allowed
    assert "denied prefix" in decision.reason


def test_disallowed_action_kind_blocked():
    decision = make_policy().check_action("hover", f"{ORIGIN}/member")
    assert not decision.allowed
    assert "not permitted" in decision.reason


def test_action_on_foreign_origin_blocked():
    decision = make_policy().check_action("click", "http://evil.example.com/", "Search")
    assert not decision.allowed


# ---------------------------------------------------------------------------
# irreversible classification
# ---------------------------------------------------------------------------


def test_irreversible_name_pattern_classifies_confirm_controls():
    policy = make_policy()
    decision = policy.check_action("click", f"{ORIGIN}/teller", "Confirm Open")
    assert decision.allowed
    assert decision.risk == "irreversible"
    # matching is word-bounded and case-insensitive
    assert policy.check_action("click", f"{ORIGIN}/teller", "post transaction").risk == "irreversible"


def test_irreversible_pattern_does_not_fire_on_ordinary_controls():
    decision = make_policy().check_action("click", f"{ORIGIN}/teller", "Search")
    assert decision.allowed
    assert decision.risk == "safe"


# ---------------------------------------------------------------------------
# enforce
# ---------------------------------------------------------------------------


def test_enforce_raises_policy_violation_on_blocked_action():
    with pytest.raises(PolicyViolation, match="not on the allowlist"):
        make_policy().enforce("click", "http://evil.example.com/", "Search")


def test_enforce_returns_decision_when_allowed():
    decision = make_policy().enforce("click", f"{ORIGIN}/teller", "Search")
    assert decision.allowed


# ---------------------------------------------------------------------------
# secrets
# ---------------------------------------------------------------------------


def test_resolve_secret_undeclared_raises():
    with pytest.raises(PolicyViolation, match="not declared"):
        make_policy().resolve_secret("nonexistent_secret")


def test_resolve_secret_declared_but_env_unset_raises(monkeypatch):
    monkeypatch.delenv("ROTE_TELLER_PASSWORD", raising=False)
    with pytest.raises(PolicyViolation, match="ROTE_TELLER_PASSWORD"):
        make_policy().resolve_secret("teller_password")


def test_resolve_secret_returns_value_and_registers_it_with_redactor(monkeypatch):
    monkeypatch.setenv("ROTE_TELLER_PASSWORD", "s3cr3t-value-99")
    policy = make_policy()
    assert policy.resolve_secret("teller_password") == "s3cr3t-value-99"
    scrubbed = policy.redact("the field now contains s3cr3t-value-99, ok")
    assert "s3cr3t-value-99" not in scrubbed
    assert "[REDACTED]" in scrubbed


# ---------------------------------------------------------------------------
# redaction patterns from the shipped config
# ---------------------------------------------------------------------------


def test_shipped_config_ssn_pattern_redacts():
    policy = Policy.from_file(SHIPPED_CONFIG)
    scrubbed = policy.redact("member SSN 123-45-6789 on file")
    assert "123-45-6789" not in scrubbed
    assert "***-**-****" in scrubbed


def test_shipped_config_password_kv_pattern_redacts():
    policy = Policy.from_file(SHIPPED_CONFIG)
    scrubbed = policy.redact("password=hunter2 rest")
    assert "hunter2" not in scrubbed
    assert "[REDACTED]" in scrubbed
