"""Distillation: successful trace -> capability artifact.

Deliberately *mechanical* - no LLM is involved. Every field of the artifact
is derived from something that verifiably happened (the trace, the final
screen) or from reviewed configuration (the app profile). That keeps the
artifact auditable: there is no step in the pipeline where a model could
hallucinate a locator or a checkpoint.

Two ideas carry the design:

* **Locator stacks from observed facets.** During discovery the surface
  records every identifying facet of each control it touched (role +
  accessible name, nearby label, legacy name= attribute, structural path).
  The distiller ranks those facets by expected robustness into an ordered
  fallback stack, with the reasoning recorded per locator.

* **Runtime-error knowledge is app-level, not flow-level.** A happy-path
  trace cannot teach you what "session expired" looks like. Detectors for
  an app's error states live in a reviewed *app profile* and are merged
  into every capability recorded against that app - the same mechanism a
  tenant override would use at scale.
"""

from __future__ import annotations

import re
from datetime import datetime, timezone
from pathlib import Path
from urllib.parse import urlparse

import yaml
from pydantic import BaseModel, Field

from .discover import DiscoveryTrace, TraceStep
from .schema import (
    AppRef,
    Capability,
    Checkpoint,
    Condition,
    Contract,
    Detector,
    Extraction,
    Locator,
    OutcomeSpec,
    OutputSpec,
    ParamSpec,
    Provenance,
    Review,
    SafetyMeta,
    Step,
    TargetRef,
    _referenced_secrets,
)
from .surface import MAIN_FRAME, Element, WebSurface


class DistillationError(Exception):
    """The trace cannot be turned into a trustworthy artifact."""


class AppProfile(BaseModel):
    """Reviewed, app-level knowledge shared by every capability on this app."""

    app_id: str
    entry_url: str
    secrets: list[str] = Field(default_factory=list)
    context: list[str] = Field(
        default_factory=list,
        description="Non-secret operator knowledge the goal won't carry (e.g. the sign-in User ID); "
        "injected into the discovery prompt",
    )
    outcomes: list[OutcomeSpec] = Field(default_factory=list)
    detectors: list[Detector] = Field(default_factory=list)

    @classmethod
    def load(cls, path: str | Path) -> "AppProfile":
        return cls.model_validate(yaml.safe_load(Path(path).read_text()))


def distill(
    trace: DiscoveryTrace,
    surface: WebSurface,
    profile: AppProfile,
    capability_id: str,
    name: str,
    description: str,
    params: dict[str, str],
    outputs: list[tuple[str, str]],
) -> Capability:
    """Build the artifact. Call while the discovery run's final page is still live."""

    if not trace.steps:
        raise DistillationError("empty trace: nothing to distill")

    origin = _origin(profile.entry_url)
    for pname, literal in params.items():
        if literal and re.search(rf"(?<![0-9A-Za-z]){re.escape(literal)}(?![0-9A-Za-z])", origin):
            raise DistillationError(
                f"param '{pname}' value {literal!r} collides with the app origin {origin!r}; "
                "record with a non-colliding example value"
            )

    templatize = _make_templatizer(params)
    steps: list[Step] = [
        Step(
            id="s1",
            intent="Open the application entry point",
            action="navigate",
            url=templatize(profile.entry_url),
        )
    ]

    prev_text: dict[str, str] = {}
    for ts in trace.steps:
        sid = f"s{len(steps) + 1}"
        checkpoint = _checkpoint_after(ts, prev_text, templatize)
        if ts.tool == "navigate":
            steps.append(Step(id=sid, intent=ts.why, action="navigate",
                              url=templatize(ts.url or ts.url_after), checkpoint=checkpoint))
        else:
            assert ts.element is not None
            steps.append(Step(
                id=sid,
                intent=ts.why,
                action=ts.tool,  # click / fill / select / press
                target=_target_from_element(ts.element),
                value=templatize(ts.value) if ts.value is not None else None,
                checkpoint=checkpoint if ts.tool == "click" else None,
                risk=ts.risk if ts.risk in ("safe", "irreversible") else "safe",
            ))
        prev_text = ts.frame_text_after

    _attach_extractions(steps[-1], trace, surface, outputs)

    detectors = [d for d in profile.detectors if _resume_target_exists(d, steps)]
    dropped = len(profile.detectors) - len(detectors)
    if dropped:
        raise DistillationError(
            f"{dropped} profile detector(s) resume from a step this capability does not contain"
        )

    contract = Contract(
        params=[
            ParamSpec(
                name=pname,
                type="string",
                description=f"Invocation input; recorded against the value {literal!r}",
                pattern=r"\d+" if literal.isdigit() else None,
                example=literal,
            )
            for pname, literal in params.items()
        ],
        outputs=[
            OutputSpec(name=oname, type=otype, description=f"Read from the final screen (saw {trace.outputs_seen.get(oname, '?')!r})")  # noqa: E501
            for oname, otype in outputs
        ],
        outcomes=list(profile.outcomes),
    )

    final_heading = _heading(_changed_frame_text(trace.steps[-1], prev_text={}))
    success = Checkpoint(
        description=f"Goal state reached for: {trace.goal}",
        condition=Condition(kind="text_visible", value=templatize(final_heading), within_ms=8000),
    )

    origins = sorted(
        {_origin(u) for ts in trace.steps for u in (ts.url_before, ts.url_after)}
        | {_origin(profile.entry_url)}
    )

    capability = Capability(
        id=capability_id,
        name=name,
        version="1.0.0",
        description=description,
        app=AppRef(app_id=profile.app_id, surface="web", entry_url=profile.entry_url),
        contract=contract,
        preconditions=[
            f"Secret '{s}' resolvable from the environment" for s in profile.secrets
        ],
        steps=steps,
        detectors=detectors,
        success=success,
        safety=SafetyMeta(
            origins=origins,
            irreversible_step_ids=[s.id for s in steps if s.risk == "irreversible"],
            requires_secrets=sorted(_referenced_secrets(steps)),
        ),
        review=Review(status="draft", notes="Auto-distilled; review before approving for unattended replay"),
        provenance=Provenance(
            discovery_run_id=trace.run_id,
            discovered_by=trace.provider,
            recorded_at=datetime.now(timezone.utc).isoformat(timespec="seconds"),
            evidence=trace.evidence_dir,
        ),
    )
    return capability


# ---------------------------------------------------------------------------
# helpers
# ---------------------------------------------------------------------------


def _make_templatizer(params: dict[str, str]):
    """Replace param literals with placeholders - on token boundaries only.

    A naive substring replace corrupts artifacts when a literal collides with
    surrounding text (member_id=1 would rewrite '127.0.0.1'). Boundary-anchored
    substitution only fires where the literal stands alone as a token.
    """
    ordered = sorted(params.items(), key=lambda kv: -len(kv[1]))
    subs = [
        (re.compile(rf"(?<![0-9A-Za-z]){re.escape(literal)}(?![0-9A-Za-z])"), f"{{{{param:{pname}}}}}")
        for pname, literal in ordered
        if literal
    ]

    def templatize(text: str | None) -> str:
        if text is None:
            return ""
        for pattern, placeholder in subs:
            text = pattern.sub(placeholder, text)
        return text

    return templatize


def _target_from_element(el: Element) -> TargetRef:
    """Rank the element's observed facets into a fallback stack, most robust first."""
    locators: list[Locator] = []
    if el.role in ("button", "link") and el.name:
        locators.append(Locator(
            strategy="role", value={"role": el.role, "name": el.name}, confidence=0.9,
            rationale="Accessible role + name; survives markup churn and maps directly to AX APIs on desktop surfaces",
        ))
    if el.label:
        locators.append(Locator(
            strategy="label_near", value={"text": el.label, "control": el.role}, confidence=0.8,
            rationale="The label an operator reads next to the field; stable because retraining staff is expensive",
        ))
    if el.input_name:
        locators.append(Locator(
            strategy="attr", value={"selector": f"{el.tag}[name='{el.input_name}']"}, confidence=0.7,
            rationale="Legacy name= attributes tend to outlive redesigns (server-side code depends on them)",
        ))
    if el.role == "link" and el.name:
        locators.append(Locator(
            strategy="text", value={"text": el.name}, confidence=0.6,
            rationale="Exact visible link text; wording is part of operator training and changes rarely",
        ))
    locators.append(Locator(
        strategy="dom_path", value={"selector": el.dom_path}, confidence=0.3,
        rationale="Structural path recorded at discovery; brittle, kept strictly as a last resort",
    ))
    return TargetRef(
        described_as=f"the {el.role} \"{el.name or el.label or el.input_name}\"",
        frame=None if el.frame == MAIN_FRAME else el.frame,
        locators=locators,
    )


def _changed_frame_text(ts: TraceStep, prev_text: dict[str, str]) -> str:
    """Text of the frame this action actually changed (the page the flow is on)."""
    changed = [f for f, t in ts.frame_text_after.items() if prev_text.get(f) != t]
    preferred = [f for f in changed if f != MAIN_FRAME] or changed
    frame = preferred[0] if preferred else (ts.element.frame if ts.element else MAIN_FRAME)
    return ts.frame_text_after.get(frame, "")


def _changed_frame_name(ts: TraceStep, prev_text: dict[str, str]) -> str | None:
    changed = [f for f, t in ts.frame_text_after.items() if prev_text.get(f) != t]
    preferred = [f for f in changed if f != MAIN_FRAME] or changed
    frame = preferred[0] if preferred else (ts.element.frame if ts.element else MAIN_FRAME)
    return None if frame == MAIN_FRAME else frame


def _heading(text: str) -> str:
    for line in text.splitlines():
        if line.strip():
            return line.strip()
    return ""


def _checkpoint_after(ts: TraceStep, prev_text: dict[str, str], templatize) -> Checkpoint | None:
    """A click that changed the screen asserts the new screen's heading."""
    if ts.tool not in ("click", "navigate"):
        return None
    heading = _heading(_changed_frame_text(ts, prev_text))
    if not heading:
        return None
    return Checkpoint(
        description=f"Screen shows {heading!r} after: {ts.why}",
        condition=Condition(
            kind="text_visible",
            value=templatize(heading),
            frame=_changed_frame_name(ts, prev_text),
            within_ms=8000,
        ),
    )


def _attach_extractions(
    final_step: Step, trace: DiscoveryTrace, surface: WebSurface, outputs: list[tuple[str, str]]
) -> None:
    parse_for = {"money": "money", "integer": "integer"}
    for oname, otype in outputs:
        seen = trace.outputs_seen.get(oname)
        if not seen:
            raise DistillationError(f"discovery did not report output '{oname}'")
        ctx = surface.find_value_context(seen)
        if ctx["kind"] == "table":
            row_label = _pick_row_label(ctx["row_cells"], seen)
            col_index = ctx["col_index"]
            if row_label and col_index < len(ctx["headers"]):
                # Extractions deliberately get NO structural fallback: a
                # dom_path can resolve to the *wrong* cell on a page with a
                # different row set and silently return a wrong value. For
                # data reads, an honest target_not_found beats a plausible
                # wrong answer - especially with regulated financial data.
                target = TargetRef(
                    described_as=f"table cell: row {row_label!r} x column {ctx['headers'][col_index]!r}",
                    frame=None if ctx["frame"] == MAIN_FRAME else ctx["frame"],
                    locators=[
                        Locator(
                            strategy="table_cell",
                            value={"row_contains": row_label, "col_header": ctx["headers"][col_index]},
                            confidence=0.85,
                            rationale="Semantic table address (row label x column header); survives cosmetic layout churn",
                        ),
                    ],
                )
                final_step.extract.append(
                    Extraction(output=oname, target=target, parse=parse_for.get(otype, "text"))
                )
                continue
        if ctx["kind"] in ("table", "text"):
            path = ctx.get("cell_path") or ctx.get("path")
            target = TargetRef(
                described_as=f"the element showing {oname}",
                frame=None if ctx["frame"] == MAIN_FRAME else ctx["frame"],
                locators=[Locator(
                    strategy="dom_path", value={"selector": path}, confidence=0.4,
                    rationale="Value found outside a headed table; structural path is the only stable handle observed",
                )],
            )
            final_step.extract.append(
                Extraction(output=oname, target=target, parse=parse_for.get(otype, "text"))
            )
            continue
        raise DistillationError(
            f"output '{oname}' (value {seen!r}) not found on the final screen; refusing to guess"
        )


def _pick_row_label(row_cells: list[str], value: str) -> str | None:
    """Prefer a digit-free cell (a semantic label like 'Regular Savings')."""
    for cell in row_cells:
        if cell and cell != value and not any(ch.isdigit() for ch in cell):
            return cell
    for cell in row_cells:
        if cell and cell != value:
            return cell
    return None


def _resume_target_exists(detector: Detector, steps: list[Step]) -> bool:
    rec = detector.recovery
    if rec and rec.kind == "resume_from_step":
        return rec.from_step in {s.id for s in steps}
    return True


def _origin(url: str) -> str:
    parsed = urlparse(url)
    return f"{parsed.scheme}://{parsed.netloc}"
