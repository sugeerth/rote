"""Human-in-the-loop escalation: pause, cede control, resume - same session.

The control-transfer model is a *lease*: at any moment exactly one party
controls the live session, and that fact is written down (control.json).
The browser is owned by the run process; the operator console is a separate
process. They meet on the filesystem:

    runs/<run_id>/handoff/
        control.json     who is (or should be) in control, and why
        context.json     the intervention request: goal, step, reason
        screen.txt       redacted render of the current screen
        elements.json    the actable controls, by index
        screenshot.png   what the operator sees
        commands/        console -> run process command queue (NNN.json)

While the human holds the lease, the run process executes *only* operator
commands - it is the single writer to the browser, so the human literally
operates the same live session, serialized through the same policy-checked
surface as the automation. Every human action is captured into evidence.

In production the console would be a real-time co-browse (CDP screencast)
behind the same lease; the mechanism here is the same seam with a plainer
transport. That is the mock line, and it is drawn around the UI - not
around the control-transfer model.
"""

from __future__ import annotations

import json
import time
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path

from .evidence import RunLog
from .policy import PolicyViolation
from .results import HumanAction, Intervention
from .surface import WebSurface


def _now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


@dataclass
class Resolution:
    resumed: bool
    outcome: str  # resumed | abandoned | timeout
    actions: list[HumanAction] = field(default_factory=list)


class InterventionHub:
    def __init__(self, surface: WebSurface, log: RunLog, timeout_s: int = 600, poll_s: float = 0.5):
        self.surface = surface
        self.log = log
        self.timeout_s = timeout_s
        self.poll_s = poll_s
        self.interventions: list[Intervention] = []
        self.root = log.dir / "handoff"
        self.commands = self.root / "commands"
        self.commands.mkdir(parents=True, exist_ok=True)
        self._write_control("automation", None, "run in progress")

    # -- the one entry point -------------------------------------------------

    def intervene(self, reason: str, step_id: str | None, goal: str | None = None) -> Resolution:
        iid = f"iv{len(self.interventions) + 1}"
        shot = self.log.screenshot(self.surface, f"intervention-{iid}")
        intervention = Intervention(
            id=iid,
            reason=reason,
            step_id=step_id,
            raised_at=_now(),
            context={"goal": goal or "", "screenshot": shot},
        )
        self.interventions.append(intervention)
        self._write_json("context.json", {
            "intervention": iid, "reason": reason, "step_id": step_id,
            "goal": goal, "raised_at": intervention.raised_at,
        })
        self._refresh_state()
        self._write_control("human", iid, reason)
        self.log.event("escalation_raised", intervention=iid, reason=reason, step=step_id,
                       console_hint="python3 -m rote console --run " + str(self.log.dir))

        deadline = time.monotonic() + self.timeout_s
        actions: list[HumanAction] = []
        while time.monotonic() < deadline:
            for command in self._pending_commands():
                outcome = self._apply(command, actions)
                if outcome is not None:
                    intervention.resolved_at = _now()
                    intervention.resolution = outcome
                    intervention.human_actions = actions
                    self._write_control("automation", iid, f"intervention {outcome}")
                    self.log.event("escalation_resolved", intervention=iid, outcome=outcome,
                                   human_actions=len(actions))
                    return Resolution(outcome == "resumed", outcome, actions)
            time.sleep(self.poll_s)

        intervention.resolved_at = _now()
        intervention.resolution = "abandoned"
        intervention.human_actions = actions
        self._write_control("automation", iid, "intervention timed out")
        self.log.event("escalation_timeout", intervention=iid)
        return Resolution(False, "timeout", actions)

    # -- command execution ----------------------------------------------------

    def _apply(self, command: dict, actions: list[HumanAction]) -> str | None:
        kind = command.get("kind")
        if kind == "resume":
            return "resumed"
        if kind == "abandon":
            return "abandoned"
        if kind == "observe":
            self._refresh_state()
            return None
        if kind == "act":
            try:
                redact = self.surface.policy.redact
                value = str(command.get("value") or "")
                if command["action"] == "navigate":
                    self.surface.navigate(value, actor="human")
                    detail = f"navigate to {value}"
                else:
                    snap = self.surface.snapshot()
                    element = snap.element(int(command["element"]))
                    self.surface.act_element(command["action"], element, command.get("value"), actor="human")
                    # Record what the human actually did - the typed value is
                    # part of the evidence, redacted like everything else.
                    typed = f" {redact(value)!r}" if value else ""
                    detail = f"{command['action']}{typed} on {element.brief()}"
                actions.append(HumanAction(at=_now(), kind=command["action"], detail=redact(detail)))
                self.log.event("human_action", action=command["action"], detail=detail)
            except PolicyViolation as exc:
                # The human is also inside the allowlist; refusals are recorded.
                self.log.event("human_action_blocked", error=str(exc))
            except Exception as exc:
                self.log.event("human_action_failed", error=str(exc))
            self._refresh_state()
            return None
        self.log.event("unknown_command", command=str(command))
        return None

    def _pending_commands(self) -> list[dict]:
        commands = []
        for path in sorted(self.commands.glob("*.json")):
            try:
                raw = path.read_text()
                commands.append(json.loads(raw))
            except json.JSONDecodeError:
                continue
            done = path.with_suffix(".done")
            path.rename(done)
            # The consumed command file stays as evidence, but redacted: an
            # operator may have typed a credential into the console.
            try:
                done.write_text(self.surface.policy.redact(raw))
            except OSError:
                pass
        return commands

    # -- state the console renders -------------------------------------------

    def _refresh_state(self) -> None:
        snap = self.surface.snapshot()
        (self.root / "screen.txt").write_text(self.surface.policy.redact(snap.render()))
        self._write_json("elements.json", [
            {"index": el.index, "brief": self.surface.policy.redact(el.brief())}
            for el in snap.elements
        ])
        try:
            self.surface.screenshot(str(self.root / "screenshot.png"))
        except Exception:
            pass

    def _write_control(self, controller: str, intervention: str | None, note: str) -> None:
        self._write_json("control.json", {
            "controller": controller, "intervention": intervention, "note": note, "at": _now(),
        })

    def _write_json(self, name: str, data: object) -> None:
        (self.root / name).write_text(json.dumps(data, indent=2) + "\n")


def send_command(run_dir: str | Path, command: dict) -> Path:
    """Console-side helper: enqueue one operator command for the run process."""
    commands = Path(run_dir) / "handoff" / "commands"
    commands.mkdir(parents=True, exist_ok=True)
    seq = len(list(commands.glob("*"))) + 1
    path = commands / f"{seq:03d}.json"
    path.write_text(json.dumps(command) + "\n")
    return path
