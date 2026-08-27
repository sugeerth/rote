"""The capability artifact schema.

A Capability is the contract between three parties:

* the **discovery agent** (an LLM) that produced it,
* the **replay engine** that executes it deterministically, and
* the **calling AI agent** that invokes it like a typed function.

Design intent, in order of importance:

1. It is a *contract*, not a transcript: typed params in, typed outputs out,
   and an enumeration of the business outcomes a caller must be prepared for.
2. Every UI target carries an ordered *locator stack* with per-locator
   rationale, so replay has fallbacks and a reviewer can judge robustness.
3. Runtime error handling is *data*, not code: detectors classify what the
   surface shows into business outcomes, recoverable conditions, and hard
   failures, and recoveries are declared per detector.
4. Nothing sensitive is ever stored: secrets appear only as ``{{secret:name}}``
   placeholders resolved from the environment at act-time.
"""

from __future__ import annotations

import json
import re
from pathlib import Path
from typing import Literal

from pydantic import BaseModel, Field, field_validator, model_validator

SCHEMA_VERSION = "1.0"

# ---------------------------------------------------------------------------
# Targeting: how replay finds a control without an LLM in the loop
# ---------------------------------------------------------------------------

LocatorStrategy = Literal[
    # Accessibility-first: role + accessible name. Survives markup churn and
    # is the strategy most portable to desktop surfaces (AX APIs).
    "role",
    # Legacy-markup workhorse: find the table cell / text label, act on the
    # nearest following control. Works when there are no ids, no <label for>.
    "label_near",
    # Visible text of a link/button. Stable in enterprise apps where wording
    # is part of operator training and changes rarely.
    "text",
    # Stable attribute selector (e.g. input[name='txtQ']). Legacy apps keep
    # field names for decades even when they never add ids.
    "attr",
    # Semantic table addressing: the cell where the row containing
    # `row_contains` meets the column headed `col_header`. Survives cosmetic
    # churn in table-soup layouts where structural paths do not.
    "table_cell",
    # Structural CSS path. Brittle; recorded only as a last-resort fallback.
    "dom_path",
]


class Locator(BaseModel):
    """One way of finding a control, with the reasoning recorded."""

    strategy: LocatorStrategy
    value: dict[str, str | bool] = Field(
        description="Strategy-specific payload, e.g. {'role': 'button', 'name': 'Search'}"
    )
    confidence: float = Field(ge=0.0, le=1.0, description="Distillation-time robustness estimate")
    rationale: str = Field(description="Why this locator was chosen / expected to be robust")


class TargetRef(BaseModel):
    """A UI control, identified by an ordered fallback stack of locators.

    Replay tries locators in order and records which one resolved; a resolve
    below the top of the stack is a drift signal even when the run succeeds.
    """

    described_as: str = Field(description="Human description, e.g. 'the Search button'")
    frame: str | None = Field(
        default=None,
        description="Name of the frame/iframe the control lives in; None = top document; "
        "'*' = search every frame (used by recovery targets, since an interstitial "
        "may replace either the whole page or just one frame)",
    )
    locators: list[Locator] = Field(min_length=1)


# ---------------------------------------------------------------------------
# Conditions: the predicate language shared by checkpoints and detectors
# ---------------------------------------------------------------------------


class Condition(BaseModel):
    """A predicate evaluated against the live surface, with a bounded wait."""

    kind: Literal["text_visible", "text_absent", "url_contains", "element_visible"]
    value: str = Field(description="Text to (not) find, URL fragment, or CSS selector")
    frame: str | None = Field(default=None, description="Frame to evaluate in; None = whole page")
    within_ms: int = Field(default=5000, ge=0, description="Max time to wait for the predicate")


class Checkpoint(BaseModel):
    """An assertion that we actually reached the state we expected."""

    description: str
    condition: Condition


# ---------------------------------------------------------------------------
# Runtime error handling: detectors + declared recoveries
# ---------------------------------------------------------------------------


class Recovery(BaseModel):
    """What replay may do, without an LLM, when a recoverable condition fires."""

    kind: Literal[
        "click",  # dismiss a known interstitial (target required)
        "retry_step",  # re-attempt the current step after a backoff
        "resume_from_step",  # re-run from an earlier step (e.g. re-login after expiry)
    ]
    target: TargetRef | None = None
    from_step: str | None = Field(default=None, description="Step id for resume_from_step")
    max_attempts: int = Field(default=1, ge=1, le=3)
    backoff_ms: int = Field(default=1000, ge=0)

    @model_validator(mode="after")
    def _kind_requirements(self) -> "Recovery":
        if self.kind == "click" and self.target is None:
            raise ValueError("click recovery requires a target")
        if self.kind == "resume_from_step" and not self.from_step:
            raise ValueError("resume_from_step recovery requires from_step")
        return self


class Detector(BaseModel):
    """Classifies a surface state the moment it appears.

    ``business`` detectors are legitimate results the caller needs
    (e.g. member_not_found) - they terminate the run as an outcome, not an
    error. ``recoverable`` detectors carry a declared recovery and bounded
    attempts. ``fatal`` detectors name states the app itself declares broken
    (an error page) - replay stops immediately with a debuggable failure.
    Anything *no* detector claims, and no checkpoint accepts, is likewise a
    hard failure: replay never blindly proceeds through an unknown state.
    """

    code: str = Field(pattern=r"^[a-z][a-z0-9_]*$")
    kind: Literal["business", "recoverable", "fatal"]
    description: str
    condition: Condition
    recovery: Recovery | None = None

    @model_validator(mode="after")
    def _recovery_matches_kind(self) -> "Detector":
        if self.kind == "recoverable" and self.recovery is None:
            raise ValueError(f"recoverable detector '{self.code}' must declare a recovery")
        if self.kind != "recoverable" and self.recovery is not None:
            raise ValueError(f"{self.kind} detector '{self.code}' must not declare a recovery")
        return self


# ---------------------------------------------------------------------------
# The agent-facing contract
# ---------------------------------------------------------------------------


class ParamSpec(BaseModel):
    name: str = Field(pattern=r"^[a-z][a-z0-9_]*$")
    type: Literal["string", "integer", "number", "boolean"]
    description: str
    required: bool = True
    pattern: str | None = Field(default=None, description="Validation regex for string params")
    example: str | int | float | bool | None = None

    def validate_value(self, value: object) -> str:
        """Validate and normalize one invocation argument to its string form."""
        expected: dict[str, type | tuple[type, ...]] = {
            "string": str,
            "integer": int,
            "number": (int, float),
            "boolean": bool,
        }
        if not isinstance(value, expected[self.type]) or isinstance(value, bool) != (self.type == "boolean"):
            raise ValueError(f"param '{self.name}' expects {self.type}, got {type(value).__name__}")
        text = str(value)
        if self.pattern and not re.fullmatch(self.pattern, text):
            raise ValueError(f"param '{self.name}' value {text!r} does not match {self.pattern!r}")
        return text


class OutputSpec(BaseModel):
    name: str = Field(pattern=r"^[a-z][a-z0-9_]*$")
    type: Literal["string", "integer", "number", "boolean", "money"]
    description: str


class OutcomeSpec(BaseModel):
    """A business outcome the caller must be prepared to receive."""

    code: str
    description: str


class Contract(BaseModel):
    params: list[ParamSpec] = Field(default_factory=list)
    outputs: list[OutputSpec] = Field(default_factory=list)
    outcomes: list[OutcomeSpec] = Field(
        default_factory=list,
        description="Non-success results that are legitimate answers, not errors",
    )

    def validate_args(self, args: dict[str, object]) -> dict[str, str]:
        """Check invocation args against the param specs; return normalized strings."""
        known = {p.name: p for p in self.params}
        unknown = set(args) - set(known)
        if unknown:
            raise ValueError(f"unknown params: {sorted(unknown)}")
        missing = [p.name for p in self.params if p.required and p.name not in args]
        if missing:
            raise ValueError(f"missing required params: {missing}")
        return {name: known[name].validate_value(value) for name, value in args.items()}


# ---------------------------------------------------------------------------
# Steps
# ---------------------------------------------------------------------------

ActionKind = Literal["navigate", "click", "fill", "select", "press", "read"]


class Extraction(BaseModel):
    """Data read from the surface into a declared output."""

    output: str = Field(description="Name of the OutputSpec this fills")
    target: TargetRef
    parse: Literal["text", "money", "integer"] = "text"


class Step(BaseModel):
    id: str = Field(pattern=r"^s\d+$")
    intent: str = Field(description="What this step accomplishes, in operator terms")
    action: ActionKind
    target: TargetRef | None = None
    value: str | None = Field(
        default=None,
        description="Literal, '{{param:name}}', or '{{secret:name}}' template for fill/select/press",
    )
    url: str | None = Field(default=None, description="Destination for navigate")
    wait_after: Condition | None = Field(
        default=None, description="Settle condition before the checkpoint is evaluated"
    )
    checkpoint: Checkpoint | None = None
    extract: list[Extraction] = Field(default_factory=list)
    risk: Literal["safe", "irreversible"] = "safe"

    @model_validator(mode="after")
    def _action_requirements(self) -> "Step":
        if self.action == "navigate" and not self.url:
            raise ValueError(f"step {self.id}: navigate requires url")
        if self.action in ("click", "fill", "select", "press") and self.target is None:
            raise ValueError(f"step {self.id}: {self.action} requires a target")
        if self.action in ("fill", "select", "press") and self.value is None:
            raise ValueError(f"step {self.id}: {self.action} requires a value")
        return self


# ---------------------------------------------------------------------------
# Capability metadata
# ---------------------------------------------------------------------------


class AppRef(BaseModel):
    app_id: str = Field(description="Logical application identity, e.g. 'cornerstone-teller'")
    surface: Literal["web"] = Field(
        description="Surface type this recording binds to; the seam for legacy-web/desktop"
    )
    entry_url: str


class SafetyMeta(BaseModel):
    origins: list[str] = Field(description="Origins this capability touches; must sit inside the policy allowlist")
    irreversible_step_ids: list[str] = Field(default_factory=list)
    requires_secrets: list[str] = Field(
        default_factory=list, description="Secret names needed at act-time; values are never stored"
    )


class Review(BaseModel):
    status: Literal["draft", "approved"] = "draft"
    notes: str | None = None


class Provenance(BaseModel):
    discovery_run_id: str
    discovered_by: str = Field(description="Model that drove the discovery run")
    recorded_at: str = Field(description="ISO-8601 timestamp")
    evidence: str = Field(description="Path to the discovery run's evidence directory")


class Capability(BaseModel):
    """A recorded, replayable, agent-invocable UI capability."""

    schema_version: str = SCHEMA_VERSION
    id: str = Field(pattern=r"^[a-z][a-z0-9-]*$")
    name: str
    version: str = Field(pattern=r"^\d+\.\d+\.\d+$")
    description: str
    app: AppRef
    contract: Contract
    preconditions: list[str] = Field(default_factory=list)
    steps: list[Step] = Field(min_length=1)
    detectors: list[Detector] = Field(default_factory=list)
    success: Checkpoint
    safety: SafetyMeta
    review: Review = Field(default_factory=Review)
    provenance: Provenance

    @model_validator(mode="after")
    def _cross_references(self) -> "Capability":
        step_ids = [s.id for s in self.steps]
        if len(step_ids) != len(set(step_ids)):
            raise ValueError("duplicate step ids")
        outputs = {o.name for o in self.contract.outputs}
        for step in self.steps:
            for ex in step.extract:
                if ex.output not in outputs:
                    raise ValueError(f"step {step.id} extracts undeclared output '{ex.output}'")
        for det in self.detectors:
            if det.kind == "business" and det.code not in {o.code for o in self.contract.outcomes}:
                raise ValueError(f"business detector '{det.code}' missing from contract.outcomes")
            rec = det.recovery
            if rec and rec.kind == "resume_from_step" and rec.from_step not in step_ids:
                raise ValueError(f"detector '{det.code}' resumes from unknown step {rec.from_step}")
        declared_irreversible = set(self.safety.irreversible_step_ids)
        actual_irreversible = {s.id for s in self.steps if s.risk == "irreversible"}
        if declared_irreversible != actual_irreversible:
            raise ValueError(
                f"safety.irreversible_step_ids {sorted(declared_irreversible)} out of sync "
                f"with step risk markings {sorted(actual_irreversible)}"
            )
        for param in _referenced_params(self.steps):
            if param not in {p.name for p in self.contract.params}:
                raise ValueError(f"steps reference undeclared param '{param}'")
        for secret in _referenced_secrets(self.steps):
            if secret not in self.safety.requires_secrets:
                raise ValueError(f"steps reference undeclared secret '{secret}'")
        return self

    @field_validator("steps")
    @classmethod
    def _no_raw_secrets(cls, steps: list[Step]) -> list[Step]:
        # Defense in depth: values must be templates or plain data, never
        # something that *looks* like a credential.
        suspicious = re.compile(r"(?i)password|passwd|secret|token|apikey|api_key")
        for step in steps:
            if step.value and suspicious.search(step.value) and not step.value.startswith("{{secret:"):
                raise ValueError(
                    f"step {step.id} value looks like a raw credential; use a {{{{secret:name}}}} placeholder"
                )
        return steps

    # -- serialization ------------------------------------------------------

    def save(self, path: str | Path) -> Path:
        path = Path(path)
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(self.model_dump_json(indent=2) + "\n")
        return path

    @classmethod
    def load(cls, path: str | Path) -> "Capability":
        return cls.model_validate(json.loads(Path(path).read_text()))


TEMPLATE_RE = re.compile(r"\{\{(param|secret):([a-z][a-z0-9_]*)\}\}")


def _referenced_params(steps: list[Step]) -> set[str]:
    return {
        m.group(2)
        for step in steps
        for m in TEMPLATE_RE.finditer(step.value or "")
        if m.group(1) == "param"
    } | {
        m.group(2)
        for step in steps
        if step.url
        for m in TEMPLATE_RE.finditer(step.url)
        if m.group(1) == "param"
    }


def _referenced_secrets(steps: list[Step]) -> set[str]:
    return {
        m.group(2)
        for step in steps
        for m in TEMPLATE_RE.finditer(step.value or "")
        if m.group(1) == "secret"
    }
