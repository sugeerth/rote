"""Unit tests for the discovery loop's transcript handling (rote/discover.py).

Regression focus: on the Messages-API transport a pending tool_use must be
answered by a tool_result in the next user turn, and user turns must never
end up consecutive - feedback ("BLOCKED", "done rejected") once broke both,
which crashed every non-happy-path discovery on the production provider.
"""

from __future__ import annotations

from rote.discover import _feedback


def _assistant_tool_use(tool_id: str = "tu_1") -> dict:
    return {"role": "assistant", "content": [
        {"type": "tool_use", "id": tool_id, "name": "click", "input": {"element": 1}},
    ]}


def test_feedback_answers_a_pending_tool_use_as_tool_result():
    messages = [_assistant_tool_use("tu_9")]
    _feedback(messages, "BLOCKED: denied path. Choose a different action.")

    assert messages[-1]["role"] == "user"
    assert messages[-1]["content"][0] == {
        "type": "tool_result", "tool_use_id": "tu_9",
        "content": "BLOCKED: denied path. Choose a different action.",
    }


def test_feedback_merges_rather_than_stacking_user_turns():
    messages = [_assistant_tool_use()]
    _feedback(messages, "BLOCKED: denied.")
    _feedback(messages, "Action performed. Current screen:\n(blank)")

    assert [m["role"] for m in messages] == ["assistant", "user"]
    assert [b.get("type") for b in messages[-1]["content"]] == ["tool_result", "text"]


def test_feedback_plain_append_and_merge_without_tool_use():
    messages = [{"role": "assistant", "content": "(called click with {})"}]
    _feedback(messages, "next")
    assert messages[-1] == {"role": "user", "content": "next"}

    _feedback(messages, "more")
    assert [m["role"] for m in messages] == ["assistant", "user"]
    assert messages[-1]["content"] == "next\n\nmore"
