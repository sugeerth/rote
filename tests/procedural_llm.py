"""A deterministic, rule-based stand-in for the discovery model.

Implements the same LLM seam as the real providers, so the *entire* pipeline
(discover loop -> trace -> distill -> replay) can be exercised in CI with no
network and no model cost. The genuine LLM-driven run for /evidence/ uses the
real providers; this class exists so the machinery is testable on every push.
"""

from __future__ import annotations

import re

from rote.llm import Decision


class ProceduralLLM:
    name = "procedural"

    def __init__(self, member_id: str = "12345", output_name: str = "savings_balance"):
        self.member_id = member_id
        self.output_name = output_name
        self._password_filled = False

    def decide(self, system: str, messages: list[dict], tools: list[dict]) -> Decision:
        obs = _latest_observation(messages)

        def index(pattern: str) -> int | None:
            m = re.search(pattern, obs)
            return int(m.group(1)) if m else None

        def call(tool: str, **args) -> Decision:
            return Decision(tool=tool, args=args, tool_use_id=f"p{len(messages)}", provider=self.name)

        if "Member Profile" in obs:
            m = re.search(r"Regular Savings[^\n]*?(\$[\d,]+\.\d{2})", obs)
            if not m:
                return call("stuck", reason="profile shown but no Regular Savings row visible")
            return call("done", summary="Read the savings balance from the member profile",
                        outputs={self.output_name: m.group(1)})

        if "Member Search" in obs:
            if f"value='{self.member_id}'" in obs:
                btn = index(r"\[e(\d+)\] button \"Search\"")
                return call("click", element=btn, why="Submit the member number search")
            box = index(r"\[e(\d+)\][^\n]*name=txtQ")
            return call("fill", element=box, value=self.member_id,
                        why="Enter the member number to look up")

        if "Main Menu" in obs:
            link = index(r"\[e(\d+)\] link \"Member Services\"")
            return call("click", element=link, why="Open Member Services to reach member search")

        if "Teller Sign-In" in obs:
            if "value='tclark'" not in obs:
                self._password_filled = False
                user = index(r"\[e(\d+)\][^\n]*name=txtUser")
                return call("fill", element=user, value="tclark", why="Enter the teller username")
            if not self._password_filled:
                self._password_filled = True
                pw = index(r"\[e(\d+)\][^\n]*name=txtPass")
                return call("fill", element=pw, value="{{secret:teller_password}}",
                            why="Enter the teller password via its secret placeholder")
            btn = index(r"\[e(\d+)\] button \"Sign In\"")
            return call("click", element=btn, why="Sign in to the teller system")

        return call("stuck", reason=f"unrecognized screen: {obs[:120]!r}")


def _latest_observation(messages: list[dict]) -> str:
    for message in reversed(messages):
        if message["role"] != "user":
            continue
        content = message["content"]
        if isinstance(content, str):
            return content
        for block in content:
            if isinstance(block, dict) and block.get("type") == "tool_result":
                inner = block.get("content", "")
                if isinstance(inner, list):
                    inner = " ".join(b.get("text", "") for b in inner)
                return str(inner)
    return ""
