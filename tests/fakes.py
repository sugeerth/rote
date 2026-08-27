"""Shared builders and fakes for the rote unit tests.

``FakeSurface`` implements exactly the slice of the WebSurface interface that
``replay()`` touches. It is driven by a scripted state model: a set of
"truth" strings that ``condition_holds()`` consults, mutated by per-target
``reactions`` that run whenever an action actually executes. The real
``Policy`` is used for every enforcement decision.
"""

from __future__ import annotations

import copy
from pathlib import Path

from rote.policy import Policy, PolicyDecision
from rote.schema import (
    AppRef,
    Capability,
    Checkpoint,
    Condition,
    Contract,
    Extraction,
    Locator,
    OutcomeSpec,
    OutputSpec,
    ParamSpec,
    Provenance,
    SafetyMeta,
    Step,
    TargetRef,
)
from rote.surface import TargetNotFound

ORIGIN = "http://127.0.0.1:7710"

# Mirrors config/policy.yaml.
POLICY_CONFIG = {
    "allowlist": {
        "origins": [ORIGIN],
        "denied_paths": ["/admin/", "/debug/"],
    },
    "actions": {"allowed": ["navigate", "click", "fill", "select", "press", "read"]},
    "risk": {
        "irreversible_name_pattern": r"(?i)\b(confirm|post|delete|purge|transfer|approve)\b",
        "unattended_irreversible": "escalate",
    },
    "secrets": {"teller_password": {"env": "ROTE_TELLER_PASSWORD"}},
    "redaction": {
        "patterns": [
            {"name": "ssn", "regex": r"\b\d{3}-\d{2}-\d{4}\b", "replacement": "***-**-****"},
            {
                "name": "password_kv",
                "regex": r"(?i)(password[\"'=:\s]+)[^\s\"'&]+",
                "replacement": r"\1[REDACTED]",
            },
        ]
    },
}


def make_policy(config: dict | None = None) -> Policy:
    return Policy(copy.deepcopy(config if config is not None else POLICY_CONFIG))


# ---------------------------------------------------------------------------
# schema builders
# ---------------------------------------------------------------------------


def cond(value: str, kind: str = "text_visible", frame: str | None = None, within_ms: int = 0) -> Condition:
    return Condition(kind=kind, value=value, frame=frame, within_ms=within_ms)


def ckpt(value: str, description: str | None = None) -> Checkpoint:
    return Checkpoint(description=description or f"Screen shows {value!r}", condition=cond(value))


def target(described_as: str, name: str = "Search", role: str = "button") -> TargetRef:
    return TargetRef(
        described_as=described_as,
        locators=[
            Locator(
                strategy="role",
                value={"role": role, "name": name},
                confidence=0.9,
                rationale="test locator",
            )
        ],
    )


def nav_step(sid: str = "s1", url: str = f"{ORIGIN}/login") -> Step:
    return Step(id=sid, intent="Open the application entry point", action="navigate", url=url)


def fill_step(
    sid: str = "s2",
    described_as: str = "the Member ID field",
    value: str = "{{param:member_id}}",
) -> Step:
    return Step(
        id=sid,
        intent="Enter the member id",
        action="fill",
        target=target(described_as, name="Member ID", role="textbox"),
        value=value,
    )


def click_step(
    sid: str = "s3",
    described_as: str = "the Search button",
    name: str = "Search",
    checkpoint_value: str | None = "Member Detail",
    risk: str = "safe",
    extract: list[Extraction] | None = None,
) -> Step:
    return Step(
        id=sid,
        intent=f"Click {described_as}",
        action="click",
        target=target(described_as, name=name),
        checkpoint=ckpt(checkpoint_value) if checkpoint_value else None,
        risk=risk,
        extract=extract or [],
    )


def balance_extraction() -> Extraction:
    return Extraction(output="balance", target=target("the balance cell", name="Balance"), parse="money")


def search_steps(checkpoint_value: str = "Member Detail") -> list[Step]:
    """navigate -> fill member id -> click Search (checkpoint + extraction)."""
    return [
        nav_step("s1"),
        fill_step("s2"),
        click_step("s3", checkpoint_value=checkpoint_value, extract=[balance_extraction()]),
    ]


def make_contract() -> Contract:
    return Contract(
        params=[ParamSpec(name="member_id", type="string", description="Member number", pattern=r"\d+")],
        outputs=[OutputSpec(name="balance", type="money", description="Share balance")],
        outcomes=[OutcomeSpec(code="member_not_found", description="No member matches the id")],
    )


def make_capability(**overrides) -> Capability:
    data: dict = dict(
        id="member-balance",
        name="Member balance lookup",
        version="1.0.0",
        description="Look up a member's share balance",
        app=AppRef(app_id="cornerstone-teller", surface="web", entry_url=f"{ORIGIN}/"),
        contract=make_contract(),
        steps=search_steps(),
        detectors=[],
        success=ckpt("Member Detail", "The member detail screen is shown"),
        safety=SafetyMeta(origins=[ORIGIN]),
        provenance=Provenance(
            discovery_run_id="disc-1",
            discovered_by="test-model",
            recorded_at="2026-08-27T00:00:00+00:00",
            evidence="runs/disc-1",
        ),
    )
    data.update(overrides)
    return Capability(**data)


# ---------------------------------------------------------------------------
# the fake surface
# ---------------------------------------------------------------------------


def react(add: tuple[str, ...] | list[str] = (), remove: tuple[str, ...] | list[str] = ()):
    """A reaction that adds/removes truths when its target is acted on."""

    def _apply(surface: "FakeSurface") -> None:
        surface.truths.difference_update(remove)
        surface.truths.update(add)

    return _apply


class FakeSnapshot:
    def __init__(self, url: str, text: str):
        self.url = url
        self._text = text

    def visible_text(self) -> str:
        return self._text


class FakeSurface:
    """Scripted stand-in for WebSurface, covering exactly what replay() uses.

    * ``truths`` - set of strings currently "shown"; text_visible holds when
      the condition value is in the set, text_absent when it is not.
    * ``reactions`` - {target.described_as (or url for navigate): callable}
      run after an act executes; use ``react(add=..., remove=...)`` or any
      callable taking the surface.
    * ``reads`` - {target.described_as: value} for read_target.
    * ``act_errors`` - {target.described_as: [exceptions]} raised (in order,
      one per act) *instead of* executing; the queue empties out.
    * ``actions`` - every action that actually executed: (kind, key, value).
    """

    def __init__(
        self,
        policy: Policy,
        url: str = f"{ORIGIN}/",
        truths: tuple[str, ...] | list[str] = (),
        reactions: dict | None = None,
        reads: dict | None = None,
        act_errors: dict | None = None,
    ):
        self.policy = policy
        self.url = url
        self.truths: set[str] = set(truths)
        self.reactions = dict(reactions or {})
        self.reads = dict(reads or {})
        self.act_errors = {k: list(v) for k, v in (act_errors or {}).items()}
        self.actions: list[tuple[str, str, str | None]] = []
        self.params: dict[str, str] = {}
        self.screenshots: list[str] = []

    # -- what replay() calls -------------------------------------------------

    def bind(self, params: dict[str, str]) -> None:
        self.params = dict(params)

    def navigate(self, url: str, actor: str = "replay") -> PolicyDecision:
        decision = self.policy.enforce("navigate", url)
        self.url = url
        self.actions.append(("navigate", url, None))
        self._react(url)
        return decision

    def act_target(self, kind, target, value=None, budget_ms=5000):
        key = target.described_as
        queue = self.act_errors.get(key)
        if queue:
            raise queue.pop(0)
        decision = self.policy.enforce(kind, self.url, key)
        self.actions.append((kind, key, value))
        self._react(key)
        return decision, "role#0"

    def condition_holds(self, cond) -> bool:
        if cond.kind == "text_absent":
            return cond.value not in self.truths
        if cond.kind == "url_contains":
            return cond.value in self.url
        return cond.value in self.truths  # text_visible / element_visible

    def read_target(self, target, parse="text", budget_ms=5000):
        key = target.described_as
        if key not in self.reads:
            raise TargetNotFound(f"no scripted read for {key!r}")
        return self.reads[key]

    def screenshot(self, path: str) -> str:
        Path(path).write_bytes(b"fake-png")
        self.screenshots.append(path)
        return path

    def snapshot(self) -> FakeSnapshot:
        return FakeSnapshot(self.url, "\n".join(sorted(self.truths)) or "(blank screen)")

    def current_url(self) -> str:
        return self.url

    # -- scripting -----------------------------------------------------------

    def _react(self, key: str) -> None:
        fn = self.reactions.get(key)
        if fn:
            fn(self)

    def acted_on(self, key: str) -> list[tuple[str, str, str | None]]:
        return [a for a in self.actions if a[1] == key]
