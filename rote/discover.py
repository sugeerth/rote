"""Discovery: an LLM drives the live surface until the goal is met.

The loop is observe -> decide -> act. The model sees a compact,
accessibility-shaped snapshot of the screen and must answer with exactly one
tool call. Every decision, action, and policy verdict is logged; the
successful trace is the raw material the distiller turns into a capability
artifact (see distill.py). The model transcript itself never becomes the
artifact.

Two properties worth noticing:

* Policy violations are surfaced *to the model* as tool results ("BLOCKED:
  ..."), so discovery can route around a fence rather than crash into it -
  but the fence itself lives in the surface, not in the prompt.
* The model never sees secret values. It fills credential fields with
  ``{{secret:name}}`` placeholders that the surface resolves at act-time.
"""

from __future__ import annotations

import json
import time
from dataclasses import dataclass, field
from datetime import datetime, timezone

from .evidence import RunLog
from .llm import LLM, Decision
from .policy import PolicyViolation
from .results import FailureDetail, RunResult, StepReport
from .surface import Element, Snapshot, WebSurface

ACT_TOOLS = {"navigate", "click", "fill", "select", "press"}


@dataclass
class TraceStep:
    """One successful action, with everything the distiller needs."""

    tool: str
    why: str
    element: Element | None
    value: str | None  # safe form: params literal, secrets as placeholders
    url: str | None
    url_before: str
    url_after: str
    frame_text_after: dict[str, str]
    risk: str


@dataclass
class DiscoveryTrace:
    goal: str
    provider: str
    steps: list[TraceStep] = field(default_factory=list)
    outputs_seen: dict[str, str] = field(default_factory=dict)
    final_snapshot: Snapshot | None = None
    run_id: str = ""
    evidence_dir: str = ""


def _tools(output_names: list[str]) -> list[dict]:
    el = {"type": "integer", "description": "element index from the current screen, e.g. 3 for [e3]"}
    why = {"type": "string", "description": "one sentence: what this accomplishes toward the goal"}
    return [
        {
            "name": "navigate",
            "description": "Go directly to a URL (allowlisted origins only).",
            "input_schema": {"type": "object", "properties": {"url": {"type": "string"}, "why": why}, "required": ["url", "why"]},
        },
        {
            "name": "click",
            "description": "Click a link or button.",
            "input_schema": {"type": "object", "properties": {"element": el, "why": why}, "required": ["element", "why"]},
        },
        {
            "name": "fill",
            "description": "Type into a text field. For credential fields use the {{secret:name}} placeholder you were given; never invent credentials.",
            "input_schema": {"type": "object", "properties": {"element": el, "value": {"type": "string"}, "why": why}, "required": ["element", "value", "why"]},
        },
        {
            "name": "select",
            "description": "Choose an option (by its visible text) in a dropdown.",
            "input_schema": {"type": "object", "properties": {"element": el, "value": {"type": "string"}, "why": why}, "required": ["element", "value", "why"]},
        },
        {
            "name": "press",
            "description": "Press a key (e.g. Enter) with a field focused.",
            "input_schema": {"type": "object", "properties": {"element": el, "key": {"type": "string"}, "why": why}, "required": ["element", "key", "why"]},
        },
        {
            "name": "done",
            "description": "The goal is fully achieved and its results are visible on screen.",
            "input_schema": {
                "type": "object",
                "properties": {
                    "summary": {"type": "string"},
                    "outputs": {
                        "type": "object",
                        "description": f"The requested outputs, read exactly as shown on screen. Required keys: {output_names}",
                    },
                },
                "required": ["summary", "outputs"],
            },
        },
        {
            "name": "stuck",
            "description": "You cannot safely make progress (dead end, unexpected state, missing permission). A human operator will be asked to help.",
            "input_schema": {"type": "object", "properties": {"reason": {"type": "string"}}, "required": ["reason"]},
        },
    ]


def _system_prompt(
    goal: str, origins: list[str], secrets: list[str], output_names: list[str],
    context: list[str] | None = None,
) -> str:
    secret_note = (
        "Credential placeholders available (type them verbatim into the matching field): "
        + ", ".join(f"{{{{secret:{s}}}}}" for s in secrets)
        if secrets
        else "No credentials are available; if a login is required, report stuck."
    )
    context_note = ("\nApp notes:\n" + "\n".join(f"- {c}" for c in context)) if context else ""
    return f"""You are operating a legacy back-office application through its UI to accomplish one task.

GOAL: {goal}
{context_note}

Rules:
- You may only operate within these origins: {', '.join(origins)}. Links elsewhere are off-limits.
- One tool call per turn. Read the screen carefully before acting; prefer the obvious operator path.
- {secret_note}
- The screen lists interactive elements as [eN]. Refer to them by that number.
- Watch for error text on the screen (red messages, warnings) and adapt.
- When the goal is achieved, call done with these outputs read exactly from the screen: {output_names}.
- If you are blocked, in a state you don't understand, or asked to do something irreversible that the goal does not clearly require, call stuck."""


def discover(
    goal: str,
    entry_url: str,
    surface: WebSurface,
    llm: LLM,
    log: RunLog,
    output_names: list[str],
    secrets: list[str],
    hub=None,
    max_steps: int = 25,
    timeout_s: int = 600,
    context: list[str] | None = None,
) -> tuple[RunResult, DiscoveryTrace]:
    started = datetime.now(timezone.utc).isoformat(timespec="seconds")
    trace = DiscoveryTrace(goal=goal, provider=llm.name, run_id=log.run_id, evidence_dir=str(log.dir))
    origins = surface.policy.origins
    system = _system_prompt(goal, origins, secrets, output_names, context)
    messages: list[dict] = []
    reports: list[StepReport] = []
    deadline = time.monotonic() + timeout_s
    blocked_streak = 0

    def finish(status, failure=None, outputs=None) -> RunResult:
        return RunResult(
            run_id=log.run_id,
            kind="discovery",
            goal=goal,
            status=status,
            outputs=outputs or {},
            failure=failure,
            steps=reports,
            interventions=hub.interventions if hub else [],
            evidence_dir=str(log.dir),
            started_at=started,
            finished_at=datetime.now(timezone.utc).isoformat(timespec="seconds"),
        )

    log.event("discovery_started", goal=goal, entry_url=entry_url, provider=llm.name)
    surface.navigate(entry_url, actor="model")
    log.screenshot(surface, "start")

    for step_no in range(1, max_steps + 1):
        if time.monotonic() > deadline:
            log.event("stopping_condition", kind="timeout")
            shot = log.screenshot(surface, "timeout")
            return finish("hard_failure", FailureDetail(
                kind="stopping_condition", step_id=None,
                expected="goal reached within time budget", observed=f"timeout after {timeout_s}s",
                evidence={"screenshot": shot},
            )), trace

        snap = surface.snapshot()
        observation = snap.render()
        log.save_text(f"snapshot-{step_no:02d}.txt", observation)
        note = "" if step_no == 1 else "Action performed. "
        obs_text = f"{note}Current screen:\n{observation}"
        if messages and messages[-1]["role"] == "assistant":
            # pair the observation with the pending tool call when the provider speaks tool-use
            last_raw = messages[-1]["content"]
            tool_id = next((b["id"] for b in last_raw if isinstance(b, dict) and b.get("type") == "tool_use"), None) if isinstance(last_raw, list) else None
            if tool_id:
                messages.append({"role": "user", "content": [{"type": "tool_result", "tool_use_id": tool_id, "content": obs_text}]})
            else:
                messages.append({"role": "user", "content": obs_text})
        else:
            messages.append({"role": "user", "content": obs_text})

        decision: Decision = llm.decide(system, messages, _tools(output_names))
        log.event("decision", step=step_no, tool=decision.tool, args=decision.args, provider=decision.provider)
        messages.append({
            "role": "assistant",
            "content": decision.raw_content or f"(called {decision.tool} with {json.dumps(decision.args)})",
        })

        if decision.tool == "done":
            outputs = {str(k): str(v) for k, v in (decision.args.get("outputs") or {}).items()}
            missing = [n for n in output_names if n not in outputs]
            if missing:
                messages.append({"role": "user", "content": f"done rejected: missing outputs {missing}. Read them from the screen and call done again."})
                continue
            trace.outputs_seen = outputs
            trace.final_snapshot = snap
            log.screenshot(surface, "final")
            log.event("discovery_succeeded", summary=decision.args.get("summary", ""), outputs=outputs)
            return finish("success", outputs=outputs), trace

        if decision.tool == "stuck":
            reason = decision.args.get("reason", "model reported stuck")
            log.event("model_stuck", reason=reason)
            if hub is None:
                return finish("hard_failure", FailureDetail(
                    kind="stopping_condition", step_id=None,
                    expected="goal reachable by the model", observed=reason,
                )), trace
            resolution = hub.intervene(reason=reason, step_id=None, goal=goal)
            if resolution.resumed:
                messages.append({"role": "user", "content": "A human operator intervened on the live session and performed some actions. Re-observe the screen and continue toward the goal."})
                continue
            return finish("escalated", FailureDetail(
                kind="stopping_condition", step_id=None,
                expected="operator to unblock the run", observed=f"intervention ended: {resolution.outcome}",
            )), trace

        if decision.tool not in ACT_TOOLS:
            messages.append({"role": "user", "content": f"Unknown tool '{decision.tool}'. Use one of the provided tools."})
            continue

        # -- act ------------------------------------------------------------
        url_before = surface.current_url()
        try:
            if decision.tool == "navigate":
                surface.navigate(decision.args["url"], actor="model")
                element, value = None, None
                risk = "safe"
            else:
                element = snap.element(int(decision.args["element"]))
                value = decision.args.get("value") or decision.args.get("key")
                verdict = surface.act_element(decision.tool, element, value, actor="model")
                risk = verdict.risk
                if risk == "irreversible":
                    log.event("irreversible_action", step=step_no, control=element.name,
                              note="attended discovery: permitted, flagged, recorded")
            blocked_streak = 0
        except (PolicyViolation, Exception) as exc:
            blocked = isinstance(exc, PolicyViolation)
            log.event("action_blocked" if blocked else "action_failed", step=step_no, error=str(exc))
            blocked_streak += 1
            if blocked_streak >= 3:
                shot = log.screenshot(surface, "dead-end")
                # A dead end is the one stopping condition a human can genuinely
                # unblock (unlike an exhausted step/time budget), so it escalates.
                if hub is not None:
                    resolution = hub.intervene(
                        reason=f"dead end: 3 consecutive blocked/failed actions; last: {exc}",
                        step_id=None, goal=goal,
                    )
                    if resolution.resumed:
                        blocked_streak = 0
                        messages.append({"role": "user", "content": "A human operator intervened on the live session. Re-observe the screen and continue toward the goal."})
                        continue
                    return finish("escalated", FailureDetail(
                        kind="stopping_condition", step_id=None,
                        expected="operator to unblock the dead end",
                        observed=f"intervention ended: {resolution.outcome}",
                        evidence={"screenshot": shot},
                    )), trace
                return finish("hard_failure", FailureDetail(
                    kind="stopping_condition", step_id=None,
                    expected="a permitted, working action", observed=f"3 consecutive failures; last: {exc}",
                    evidence={"screenshot": shot},
                )), trace
            messages.append({"role": "user", "content": f"BLOCKED: {exc}. Choose a different action."})
            continue

        reports.append(StepReport(step_id=f"d{step_no}", status="ok", note=f"{decision.tool}: {decision.args.get('why', '')}"))
        after = surface.snapshot()
        trace.steps.append(TraceStep(
            tool=decision.tool,
            why=str(decision.args.get("why", "")),
            element=element,
            value=value,
            url=decision.args.get("url"),
            url_before=url_before,
            url_after=surface.current_url(),
            frame_text_after={k: v[:1500] for k, v in after.frame_text.items()},
            risk=risk,
        ))
        log.screenshot(surface, f"after-{decision.tool}")

    log.event("stopping_condition", kind="max_steps")
    shot = log.screenshot(surface, "max-steps")
    return finish("hard_failure", FailureDetail(
        kind="stopping_condition", step_id=None,
        expected=f"goal reached within {max_steps} steps", observed="max steps exhausted",
        evidence={"screenshot": shot},
    )), trace
