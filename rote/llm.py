"""LLM provider seam for the discovery loop.

The loop speaks one small interface: given a system prompt, a message
history, and a tool catalog, return the model's next tool call. Three
implementations:

* ``AnthropicLLM`` - the production path (Anthropic Messages API, native
  tool use). Needs ``ANTHROPIC_API_KEY``.
* ``ClaudeCLILLM`` - a development shim that shells out to the ``claude``
  CLI (subscription auth) when no API key is configured. Same decisions,
  different transport; clearly logged in evidence when used.
* ``ScriptedLLM`` - deterministic canned decisions for tests.
"""

from __future__ import annotations

import json
import os
import re
import subprocess
import uuid
from dataclasses import dataclass, field
from typing import Protocol

DEFAULT_MODEL = os.environ.get("ROTE_MODEL", "claude-sonnet-5")


@dataclass
class Decision:
    tool: str
    args: dict
    tool_use_id: str
    provider: str
    # Assistant content blocks to append to the transcript (API providers).
    raw_content: list = field(default_factory=list)
    text: str | None = None  # any prose the model emitted alongside the call


class LLM(Protocol):
    name: str

    def decide(self, system: str, messages: list[dict], tools: list[dict]) -> Decision: ...


class AnthropicLLM:
    def __init__(self, model: str = DEFAULT_MODEL):
        import anthropic

        self.name = f"anthropic:{model}"
        self.model = model
        self._client = anthropic.Anthropic()

    def decide(self, system: str, messages: list[dict], tools: list[dict]) -> Decision:
        response = self._client.messages.create(
            model=self.model,
            max_tokens=1500,
            temperature=0,
            system=system,
            messages=messages,
            tools=tools,
            tool_choice={"type": "any"},
        )
        raw = [block.model_dump() for block in response.content]
        text = " ".join(b["text"] for b in raw if b.get("type") == "text") or None
        for block in raw:
            if block["type"] == "tool_use":
                return Decision(
                    tool=block["name"],
                    args=block["input"],
                    tool_use_id=block["id"],
                    provider=self.name,
                    raw_content=raw,
                    text=text,
                )
        raise RuntimeError("model returned no tool call despite tool_choice=any")


class ClaudeCLILLM:
    """Development transport: the same decisions via the `claude` CLI.

    Stateless per call: the transcript is flattened into one prompt and the
    model must answer with a single JSON object naming a tool. Used only when
    no API key is available; the provider name lands in the evidence log so
    a reviewer can see exactly which transport produced a run.
    """

    def __init__(self, model: str = "sonnet"):
        self.name = f"claude-cli:{model}"
        self.model = model

    def decide(self, system: str, messages: list[dict], tools: list[dict]) -> Decision:
        tool_lines = "\n".join(
            f"- {t['name']}: {t['description']} args schema: {json.dumps(t['input_schema'])}"
            for t in tools
        )
        history = "\n\n".join(_flatten_message(m) for m in messages)
        prompt = (
            f"{system}\n\n== Available tools ==\n{tool_lines}\n\n== Transcript ==\n{history}\n\n"
            "Decide the single next tool call. Respond with ONLY a JSON object, no prose, "
            'no code fences: {"tool": "<name>", "args": {...}}'
        )
        last_error: Exception | None = None
        for attempt in range(2):  # nothing enforces tool choice over this transport
            out = subprocess.run(
                ["claude", "-p", prompt, "--output-format", "json", "--model", self.model],
                capture_output=True,
                text=True,
                timeout=240,
            )
            if out.returncode != 0:
                raise RuntimeError(f"claude CLI failed: {out.stderr[:500]}")
            payload = json.loads(out.stdout)
            if isinstance(payload, list):  # some CLI versions emit the event array
                payload = next((item for item in payload if item.get("type") == "result"), {})
            if payload.get("is_error"):
                raise RuntimeError(f"claude CLI returned an error result: {str(payload)[:300]}")
            result_text = str(payload.get("result", "")).strip()
            result_text = re.sub(r"^```(json)?|```$", "", result_text, flags=re.M).strip()
            try:
                start, end = result_text.find("{"), result_text.rfind("}")
                call = json.loads(result_text[start : end + 1])
                return Decision(
                    tool=call["tool"],
                    args=call.get("args", {}),
                    tool_use_id=f"cli-{uuid.uuid4().hex[:8]}",
                    provider=self.name,
                )
            except (ValueError, KeyError) as exc:
                last_error = exc
                prompt += "\n\nREMINDER: respond with ONLY the JSON object, nothing else."
        raise RuntimeError(f"claude CLI answer was not a tool call: {last_error}")


class ScriptedLLM:
    """Canned decisions for tests: no network, fully deterministic."""

    def __init__(self, script: list[tuple[str, dict]]):
        self.name = "scripted"
        self._script = list(script)

    def decide(self, system: str, messages: list[dict], tools: list[dict]) -> Decision:
        if not self._script:
            raise RuntimeError("scripted LLM ran out of decisions")
        tool, args = self._script.pop(0)
        return Decision(tool=tool, args=args, tool_use_id=f"scripted-{len(self._script)}", provider=self.name)


def _flatten_message(message: dict) -> str:
    role = message["role"].upper()
    content = message["content"]
    if isinstance(content, str):
        return f"{role}: {content}"
    parts = []
    for block in content:
        kind = block.get("type")
        if kind == "text":
            parts.append(block["text"])
        elif kind == "tool_use":
            parts.append(f"(called {block['name']} with {json.dumps(block['input'])})")
        elif kind == "tool_result":
            inner = block.get("content", "")
            if isinstance(inner, list):
                inner = " ".join(b.get("text", "") for b in inner)
            parts.append(str(inner))
    return f"{role}: " + "\n".join(parts)


def pick_llm() -> LLM:
    """Production API when configured; CLI shim otherwise."""
    if os.environ.get("ANTHROPIC_API_KEY"):
        return AnthropicLLM()
    return ClaudeCLILLM()
