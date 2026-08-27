"""Deterministic replay: the production execution path.

Given a capability artifact and typed invocation args, execute the recorded
flow with **no LLM in the decision loop**. Determinism comes from:

* ordered locator stacks with bounded, polled resolution (no unbounded waits),
* explicit checkpoints - we assert we reached the state, never assume,
* declarative detectors that classify every off-happy-path state into
  business outcome / recoverable / fatal, with bounded declared recoveries,
* a closed-world default: a state nothing claims is a *hard failure* with
  step, expected, observed, and evidence attached. Replay never guesses.

Classification precedence: business first (a legitimate answer must win),
then fatal (a declared-broken state must not be "recovered" into), then
recoverable.
"""

from __future__ import annotations

import time
from dataclasses import dataclass
from datetime import datetime, timezone

from .evidence import RunLog
from .policy import PolicyViolation
from .results import FailureDetail, RunResult, StepReport
from .schema import Capability, Detector, Step
from .surface import PolicyViolationNavigation, TargetNotFound, WebSurface

_KIND_ORDER = {"business": 0, "fatal": 1, "recoverable": 2}
MAX_STEP_ATTEMPTS = 3


@dataclass
class ReplayOptions:
    allow_irreversible: bool = False
    locator_budget_ms: int = 5000
    escalate_on_failure: bool = True  # only takes effect when a hub is attached


class _Outcome(Exception):
    def __init__(self, detector: Detector):
        self.detector = detector


class _Hard(Exception):
    def __init__(self, detail: FailureDetail, escalated: bool = False):
        self.detail = detail
        self.escalated = escalated  # an intervention was already raised and ended unresolved


def replay(
    capability: Capability,
    args: dict[str, object],
    surface: WebSurface,
    log: RunLog,
    hub=None,
    options: ReplayOptions | None = None,
) -> RunResult:
    options = options or ReplayOptions()
    started = datetime.now(timezone.utc).isoformat(timespec="seconds")
    reports: list[StepReport] = []
    outputs: dict[str, str | int | float | bool] = {}

    def finish(status: str, *, failure=None, outcome: Detector | None = None) -> RunResult:
        log.event("replay_finished", status=status,
                  outcome=outcome.code if outcome else None)
        return RunResult(
            run_id=log.run_id,
            kind="replay",
            capability_id=capability.id,
            status=status,
            outcome_code=outcome.code if outcome else None,
            outcome_message=outcome.description if outcome else None,
            outputs=outputs,
            failure=failure,
            steps=reports,
            interventions=hub.interventions if hub else [],
            evidence_dir=str(log.dir),
            started_at=started,
            finished_at=datetime.now(timezone.utc).isoformat(timespec="seconds"),
        )

    # -- preflight: fail fast, before touching the UI -----------------------
    try:
        params = capability.contract.validate_args(args)
    except ValueError as exc:
        return finish("hard_failure", failure=FailureDetail(
            kind="invalid_args", step_id=None,
            expected="invocation args matching the capability contract", observed=str(exc),
        ))
    surface.bind(params)
    log.event("replay_started", capability=capability.id, version=capability.version,
              params=params, review_status=capability.review.status)

    for origin in capability.safety.origins:
        decision = surface.policy.check_url(origin + "/")
        if not decision.allowed:
            return finish("hard_failure", failure=FailureDetail(
                kind="policy_violation", step_id=None,
                expected=f"capability origin {origin} inside the policy allowlist",
                observed=decision.reason,
            ))
    try:
        for secret in capability.safety.requires_secrets:
            surface.policy.resolve_secret(secret)
    except PolicyViolation as exc:
        return finish("hard_failure", failure=FailureDetail(
            kind="policy_violation", step_id=None,
            expected="all required secrets resolvable", observed=str(exc),
        ))

    detectors = sorted(capability.detectors, key=lambda d: _KIND_ORDER[d.kind])
    recovery_spent: dict[str, int] = {}
    step_index = {s.id: i for i, s in enumerate(capability.steps)}
    escalated_steps: set[str] = set()

    # -- the run ------------------------------------------------------------
    i = 0
    human_fixed = False  # set when an operator intervened and handed control back
    while i < len(capability.steps):
        step = capability.steps[i]
        t0 = time.monotonic()
        attempts = 0
        resume_to: str | None = None
        locator_used: str | None = None
        try:
            while True:
                attempts += 1
                if human_fixed and step.checkpoint is not None and surface.condition_holds(step.checkpoint.condition):
                    # The operator completed this step's effect manually.
                    log.event("step_completed_by_human", step=step.id)
                    human_fixed = False
                    break
                try:
                    _gate_irreversible(step, capability, options, hub, log, escalated_steps)
                    locator_used = _execute(step, surface, capability, detectors, recovery_spent, options, log)
                    _verify(step, surface, capability, detectors, recovery_spent, log)
                    break
                except _Retry as r:
                    log.event("step_retry", step=step.id, reason=r.reason, attempt=attempts)
                    if attempts >= MAX_STEP_ATTEMPTS:
                        raise _Hard(FailureDetail(
                            kind=r.kind, step_id=step.id,
                            expected=f"step to succeed within {MAX_STEP_ATTEMPTS} attempts",
                            observed=r.reason,
                            evidence={"screenshot": log.screenshot(surface, f"{step.id}-exhausted")},
                        ))
                    time.sleep(r.backoff_ms / 1000)
                except _Resume as r:
                    log.event("recovery_resume", step=step.id, from_step=r.from_step)
                    resume_to = r.from_step
                    break
            human_fixed = False
            if resume_to is not None:
                reports.append(StepReport(step_id=step.id, status="recovered", attempts=attempts,
                                          note=f"resuming from {resume_to}",
                                          duration_ms=int((time.monotonic() - t0) * 1000)))
                i = step_index[resume_to]
                continue
            for ex in step.extract:
                try:
                    outputs[ex.output] = surface.read_target(ex.target, ex.parse, options.locator_budget_ms)
                    log.event("extracted", step=step.id, output=ex.output, value=str(outputs[ex.output]))
                except TargetNotFound as exc:
                    raise _Hard(FailureDetail(
                        kind="target_not_found", step_id=step.id,
                        expected=f"extraction target for output '{ex.output}'", observed=str(exc),
                        evidence={"screenshot": log.screenshot(surface, f"{step.id}-extract-missing")},
                    ))
            reports.append(StepReport(step_id=step.id, status="ok", attempts=attempts,
                                      locator_used=locator_used,
                                      duration_ms=int((time.monotonic() - t0) * 1000)))
        except _Outcome as o:
            reports.append(StepReport(step_id=step.id, status="business_outcome", attempts=attempts,
                                      note=o.detector.code,
                                      duration_ms=int((time.monotonic() - t0) * 1000)))
            log.screenshot(surface, f"{step.id}-outcome-{o.detector.code}")
            log.event("business_outcome", step=step.id, code=o.detector.code)
            return finish("business_outcome", outcome=o.detector)
        except _Hard as h:
            log.event("hard_failure", step=step.id, kind=h.detail.kind,
                      expected=h.detail.expected, observed=h.detail.observed)
            if h.escalated:
                # An intervention was already raised for this failure and the
                # operator abandoned (or it timed out): report it as escalated.
                reports.append(StepReport(step_id=step.id, status="escalated", attempts=attempts,
                                          note=h.detail.kind))
                return finish("escalated", failure=h.detail)
            if hub and options.escalate_on_failure and step.id not in escalated_steps:
                escalated_steps.add(step.id)
                resolution = hub.intervene(
                    reason=f"{h.detail.kind}: expected {h.detail.expected!r}, observed {h.detail.observed!r}",
                    step_id=step.id,
                    goal=capability.description,
                )
                reports.append(StepReport(step_id=step.id, status="escalated", attempts=attempts,
                                          note=h.detail.kind))
                if resolution.resumed:
                    log.event("resumed_after_intervention", step=step.id)
                    human_fixed = True
                    continue  # re-verify (the human may have completed the step) or redo
                return finish("escalated", failure=h.detail)
            reports.append(StepReport(step_id=step.id, status="failed", attempts=attempts,
                                      note=h.detail.kind))
            return finish("hard_failure", failure=h.detail)
        i += 1

    # -- final success condition -------------------------------------------
    if not surface.condition_holds(capability.success.condition):
        claimed = _classify(surface, detectors)
        if claimed and claimed.kind == "business":
            return finish("business_outcome", outcome=claimed)
        return finish("hard_failure", failure=FailureDetail(
            kind="checkpoint_failed", step_id=None,
            expected=capability.success.description,
            observed=_observed(surface),
            evidence={"screenshot": log.screenshot(surface, "success-failed")},
        ))
    log.screenshot(surface, "success")
    return finish("success")


# ---------------------------------------------------------------------------
# step execution internals
# ---------------------------------------------------------------------------


class _Retry(Exception):
    def __init__(self, reason: str, backoff_ms: int = 800, kind: str = "recovery_exhausted"):
        self.reason = reason
        self.backoff_ms = backoff_ms
        self.kind = kind  # HardFailureKind to report if attempts run out


class _Resume(Exception):
    def __init__(self, from_step: str):
        self.from_step = from_step


def _gate_irreversible(
    step: Step, capability: Capability, options: ReplayOptions, hub, log: RunLog,
    escalated_steps: set[str],
) -> None:
    if step.risk != "irreversible" or options.allow_irreversible:
        return
    mode = getattr(hub.surface.policy, "unattended_irreversible", "escalate") if hub else "escalate"
    log.event("irreversible_gate", step=step.id, review=capability.review.status, mode=mode)
    if hub is None or mode == "block":
        raise _Hard(FailureDetail(
            kind="policy_violation", step_id=step.id,
            expected="caller authorization (allow_irreversible) for an irreversible step",
            observed="unattended replay reached an unauthorized irreversible step"
            + ("" if hub else " with no escalation hub attached"),
        ))
    escalated_steps.add(step.id)  # one intervention per step: a refusal must not re-escalate
    resolution = hub.intervene(
        reason=f"Irreversible step {step.id} ({step.intent}) requires human approval",
        step_id=step.id,
        goal=capability.description,
    )
    if not resolution.resumed:
        raise _Hard(FailureDetail(
            kind="policy_violation", step_id=step.id,
            expected="human approval for the irreversible step",
            observed=f"intervention ended without approval ({resolution.outcome})",
        ), escalated=True)
    options.allow_irreversible = True  # approval covers this invocation
    log.event("irreversible_approved", step=step.id)


def _execute(
    step: Step,
    surface: WebSurface,
    capability: Capability,
    detectors: list[Detector],
    recovery_spent: dict[str, int],
    options: ReplayOptions,
    log: RunLog,
) -> str | None:
    """Perform the step's action; classify what can go wrong doing it.

    Returns the locator rung that resolved (e.g. "role#0") so the step report
    carries the drift signal, or None for actions without a target.
    """
    try:
        if step.action == "navigate":
            surface.navigate(step.url or "", actor="replay")
            log.event("navigated", step=step.id, url=step.url)
            return None
        if step.action == "read":
            return None
        decision, used = surface.act_target(step.action, step.target, step.value, options.locator_budget_ms)
        log.event("acted", step=step.id, action=step.action, locator_used=used, risk=decision.risk)
        if used and not used.endswith("#0"):
            log.event("drift_signal", step=step.id, locator_used=used,
                      note="resolved below the top of the locator stack")
        return used
    except (PolicyViolation, PolicyViolationNavigation) as exc:
        raise _Hard(FailureDetail(
            kind="policy_violation", step_id=step.id,
            expected="action within the guardrail policy", observed=str(exc),
        ))
    except TargetNotFound as exc:
        # Maybe a known state (an interstitial, an expired session) is covering
        # the screen - let the detectors classify before calling it hard.
        _classify_and_raise(step, surface, capability, detectors, recovery_spent, log,
                            expected=f"target resolvable: {exc}", default_kind="target_not_found",
                            action_ran=False)
    except Exception as exc:
        raise _Retry(f"surface error during {step.action}: {exc}", kind="surface_error")
    return None


def _verify(
    step: Step,
    surface: WebSurface,
    capability: Capability,
    detectors: list[Detector],
    recovery_spent: dict[str, int],
    log: RunLog,
) -> None:
    """Wait/checkpoint the step, classifying any deviation."""
    failure_note = None
    try:
        ok = True
        if step.wait_after is not None:
            ok = surface.condition_holds(step.wait_after)
            failure_note = f"wait condition {step.wait_after.kind} {step.wait_after.value!r}"
        if ok and step.checkpoint is not None:
            ok = surface.condition_holds(step.checkpoint.condition)
            failure_note = step.checkpoint.description
        if ok:
            return
    except Exception as exc:
        raise _Retry(f"verification error: {exc}")
    _classify_and_raise(step, surface, capability, detectors, recovery_spent, log,
                        expected=failure_note or "checkpoint")


def _classify_and_raise(step, surface, capability, detectors, recovery_spent, log,
                        expected: str, default_kind: str = "checkpoint_failed",
                        action_ran: bool = True):
    claimed = _classify(surface, detectors)
    if claimed is None:
        raise _Hard(FailureDetail(
            kind=default_kind, step_id=step.id,
            expected=expected, observed=_observed(surface),
            evidence={"screenshot": log.screenshot(surface, f"{step.id}-unexpected")},
        ))
    log.event("detector_fired", step=step.id, code=claimed.code, kind=claimed.kind)
    if claimed.kind == "business":
        raise _Outcome(claimed)
    if claimed.kind == "fatal":
        raise _Hard(FailureDetail(
            kind="app_error", step_id=step.id,
            expected=expected, observed=f"{claimed.description}: {_observed(surface)}",
            evidence={"screenshot": log.screenshot(surface, f"{step.id}-{claimed.code}")},
        ))
    # recoverable
    spent = recovery_spent.get(claimed.code, 0)
    recovery = claimed.recovery
    assert recovery is not None
    if spent >= recovery.max_attempts:
        raise _Hard(FailureDetail(
            kind="recovery_exhausted", step_id=step.id,
            expected=f"'{claimed.code}' to occur at most {recovery.max_attempts} time(s)",
            observed=f"occurred {spent + 1} times",
            evidence={"screenshot": log.screenshot(surface, f"{step.id}-{claimed.code}")},
        ))
    recovery_spent[claimed.code] = spent + 1
    log.event("recovery_applied", step=step.id, code=claimed.code, kind=recovery.kind,
              attempt=spent + 1)
    if recovery.kind == "click":
        try:
            surface.act_target("click", recovery.target, None, 5000)
        except TargetNotFound as exc:
            raise _Hard(FailureDetail(
                kind="recovery_exhausted", step_id=step.id,
                expected=f"recovery control for '{claimed.code}'", observed=str(exc),
            ))
        except (PolicyViolation, PolicyViolationNavigation) as exc:
            raise _Hard(FailureDetail(
                kind="policy_violation", step_id=step.id,
                expected=f"recovery for '{claimed.code}' within the guardrail policy", observed=str(exc),
            ))
        # If the step's action already ran and its checkpoint now holds, the
        # interstitial merely delayed it. If the action never ran (the target
        # was covered), the step must be redone - a passing checkpoint alone
        # cannot vouch for a fill/click that never happened.
        if action_ran and (step.checkpoint is None or surface.condition_holds(step.checkpoint.condition)):
            return
        raise _Retry(f"redo after dismissing '{claimed.code}'", backoff_ms=recovery.backoff_ms)
    if recovery.kind == "retry_step":
        raise _Retry(f"declared retry for '{claimed.code}'", backoff_ms=recovery.backoff_ms)
    raise _Resume(recovery.from_step or "s1")


def _classify(surface: WebSurface, detectors: list[Detector]) -> Detector | None:
    for det in detectors:  # already sorted: business, fatal, recoverable
        if surface.condition_holds(det.condition):
            return det
    return None


def _observed(surface: WebSurface) -> str:
    try:
        snap = surface.snapshot()
        first_lines = [ln.strip() for ln in snap.visible_text().splitlines() if ln.strip()][:3]
        return f"url={snap.url} showing: " + " | ".join(first_lines)
    except Exception as exc:
        return f"(could not observe surface: {exc})"
