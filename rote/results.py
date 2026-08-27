"""Result contracts: what a caller gets back from discovery and replay.

The load-bearing distinction (see REPORT.md): a run can end three ways that
are *not* the same thing -

* ``success`` - the capability did its job; declared outputs are populated.
* ``business_outcome`` - the application gave a legitimate answer the caller
  needs ("no such member"). This is a result, not an error.
* ``hard_failure`` - the run could not safely continue; the failure detail
  says which step, what was expected, what was observed, and where the
  evidence lives.

``escalated`` is a fourth, transitional state: a human was brought into the
loop. After the human acts, the run resolves into one of the three above and
the intervention is recorded on the result.
"""

from __future__ import annotations

from typing import Literal

from pydantic import BaseModel, Field

RunStatus = Literal["success", "business_outcome", "hard_failure", "escalated"]

HardFailureKind = Literal[
    "invalid_args",  # the invocation violated the capability's param contract
    "target_not_found",  # every locator in the stack failed to resolve
    "checkpoint_failed",  # acted, but did not reach the expected state (and no detector claimed it)
    "app_error",  # the application itself errored (e.g. HTTP 500 page)
    "policy_violation",  # an action was blocked by the guardrail policy
    "timeout",  # a bounded wait expired
    "recovery_exhausted",  # a recoverable condition kept recurring past its budget
    "stopping_condition",  # discovery hit max steps / dead-end
    "surface_error",  # the automation surface itself failed (browser died)
]


class FailureDetail(BaseModel):
    kind: HardFailureKind
    step_id: str | None
    expected: str = Field(description="What the run needed to observe or do")
    observed: str = Field(description="What the surface actually showed (redacted)")
    evidence: dict[str, str] = Field(
        default_factory=dict, description="Named evidence refs, e.g. screenshot/snapshot paths"
    )


class StepReport(BaseModel):
    step_id: str
    status: Literal["ok", "recovered", "business_outcome", "failed", "escalated", "not_reached"]
    attempts: int = 1
    locator_used: str | None = Field(
        default=None,
        description="strategy#index that resolved; index > 0 on a success is a drift signal",
    )
    duration_ms: int = 0
    note: str | None = None


class HumanAction(BaseModel):
    """One thing the operator did while in control (captured, redacted)."""

    at: str
    kind: str
    detail: str


class Intervention(BaseModel):
    id: str
    reason: str
    step_id: str | None
    raised_at: str
    context: dict[str, str] = Field(
        default_factory=dict, description="What the operator was shown: goal, state, screenshot path"
    )
    resolved_at: str | None = None
    resolution: Literal["resumed", "completed_by_human", "abandoned"] | None = None
    human_actions: list[HumanAction] = Field(default_factory=list)


class RunResult(BaseModel):
    """The single result contract for both discovery and replay runs."""

    run_id: str
    kind: Literal["discovery", "replay"]
    capability_id: str | None = None
    goal: str | None = None
    status: RunStatus
    outcome_code: str | None = Field(
        default=None, description="Set when status == business_outcome (e.g. member_not_found)"
    )
    outcome_message: str | None = None
    outputs: dict[str, str | int | float | bool] = Field(default_factory=dict)
    failure: FailureDetail | None = None
    steps: list[StepReport] = Field(default_factory=list)
    interventions: list[Intervention] = Field(default_factory=list)
    evidence_dir: str
    started_at: str
    finished_at: str

    def summary(self) -> str:
        head = f"[{self.kind}] {self.status}"
        if self.outcome_code:
            head += f" ({self.outcome_code})"
        if self.failure:
            head += f" at {self.failure.step_id}: {self.failure.kind} - expected {self.failure.expected!r}, observed {self.failure.observed!r}"
        if self.outputs:
            head += f" outputs={self.outputs}"
        return head
