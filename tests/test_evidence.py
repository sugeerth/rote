"""Unit tests for the evidence layer (rote/evidence.py).

Regression focus: JSON evidence must stay valid JSON after redaction. The
redactor once ran over the *serialized* text, where a match could consume
structural tokens (a string ending in "password" swallowed the closing
brace and newline); save_json now redacts value-by-value.
"""

from __future__ import annotations

import json

from rote.evidence import RunLog

from fakes import make_policy


def test_save_json_stays_valid_when_a_string_ends_with_password(tmp_path):
    log = RunLog(tmp_path, "test", redact=make_policy().redact)
    data = {
        "note": "Submit the sign-in form with the entered User ID and password",
        "steps": [{"why": "Enter the teller password to complete sign-in"}],
    }
    log.save_json("result.json", data)

    loaded = json.loads((log.dir / "result.json").read_text())  # must parse
    assert loaded["note"] == data["note"]  # prose survives intact
    assert loaded["steps"][0]["why"] == data["steps"][0]["why"]


def test_save_json_still_redacts_secrets_and_credential_pairs(tmp_path, monkeypatch):
    monkeypatch.setenv("ROTE_TELLER_PASSWORD", "spring2026-demo")
    policy = make_policy()
    policy.resolve_secret("teller_password")  # registers the value with the redactor
    log = RunLog(tmp_path, "test", redact=policy.redact)

    log.save_json("result.json", {
        "observed": "typed spring2026-demo into the field",
        "kv": "password=hunter2 rest",
    })

    text = (log.dir / "result.json").read_text()
    assert "spring2026-demo" not in text
    assert "hunter2" not in text
    assert json.loads(text)["kv"].endswith("rest")  # neighbors survive
