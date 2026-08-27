# REPORT

## Architecture

Single process, five seams, no premature infrastructure. The through-line —
*the model discovers, the artifact is the capability, deterministic replay is
production* — maps one-to-one onto modules:

```
discover.py ──trace──▶ distill.py ──▶ schema.py (Capability) ──▶ replay.py
      │                                                             │
      └──────────────── surface.py (perceive/act + policy gate) ────┘
                                    │
                     policy.py      │      handoff.py (control lease)
                     evidence.py ◀──┴────▶ console.py (operator)
```

Key decisions:

- **Python + Playwright; Claude as the discovery model.** Python because the
  artifact schema is the focal point and pydantic makes it typed, validated,
  and reviewable; Playwright for first-class frames, role-based locators, and
  bounded waits. The model sits behind a three-method provider seam
  (`llm.py`): the Anthropic API in production, a `claude`-CLI shim for
  keyless dev machines, a scripted fake for CI.
- **One policy choke point.** Every action — model-driven, replayed, or
  human-via-console — goes through the Surface, and the Surface consults the
  guardrail policy before acting. There is no second path to the UI, so
  "stay within guardrails throughout" is structural, not disciplinary.
- **Distillation is mechanical, not model-driven.** The artifact is derived
  entirely from what verifiably happened (the recorded trace, the final
  screen) plus reviewed configuration. There is no step where a model could
  hallucinate a locator or checkpoint into the artifact, which keeps
  artifacts auditable — important, since they run unattended later.
- **Runtime-error knowledge is app-level, not flow-level.** A happy-path
  trace cannot teach the system what "session expired" looks like. Detectors
  live in a reviewed per-app profile (`config/apps/*.yaml`) and are merged
  into every capability recorded against that app.
- **Provider seam for the LLM.** Discovery speaks one small interface
  (system, messages, tools → one tool call). Production uses the Anthropic
  API; a `claude`-CLI shim exists for keyless dev environments (the evidence
  log records which transport produced every run); a scripted fake makes the
  entire pipeline testable in CI with zero model cost.
- Trade-off: synchronous, single-session execution. Queues, pools, and
  fleet-scale plumbing are deliberately absent (see Cuts) — the brief rewards
  abstractions that could scale, not scaffolding that pretends to.

## Artifact schema

A `Capability` (see `rote/schema.py`, `capabilities/*.json`) is a *contract*,
not a step dump. The parts a calling agent sees:

- **`contract`** — typed `params` (with validation patterns; a bad
  `member_id` is rejected before the browser ever opens), typed `outputs`
  (`money`, `string`, ...), and — critically — declared **`outcomes`**: the
  non-success results a caller must handle (`member_not_found`,
  `validation_rejected`, `permission_denied`). "No such member" is part of
  the capability's type, not an exception.
- **`steps`** — each with an operator-language `intent`, an action, a
  **locator stack** (below), an explicit `checkpoint` asserting the screen
  actually changed as expected, optional typed extractions, and a `risk`
  marking (`safe` / `irreversible`).
- **`detectors`** — declarative classifiers for off-happy-path states, each
  `business`, `recoverable` (with a declared, bounded recovery), or `fatal`.
- **`success`** — the final goal-state checkpoint.
- **`safety`** — origins touched, irreversible step ids, secrets *required*
  (never their values); **`review`** — draft → approved gate; **`provenance`**
  — which model, which run, where the evidence lives.

Every UI target is an ordered **locator stack with recorded rationale**:
accessible role+name first (survives markup churn; maps to AX APIs on
desktop), then label-proximity (the text an operator reads next to a field —
the workhorse on markup with no ids and no `<label>`), then legacy `name=`
attributes (which outlive redesigns because server code depends on them),
then exact visible text, then a structural path strictly as a last resort.
Replay records *which* rung resolved; resolving below the top is logged as a
drift signal even when the run succeeds. Table data uses a semantic
`table_cell` address (row label × column header). Extractions carry **no
structural fallback** deliberately: on a page with a different row set a
recorded path can resolve to the *wrong cell* and silently return a wrong
number — for regulated data, an honest `target_not_found` beats a plausible
wrong answer. (The e2e test that found this is in the suite.)

Schema is versioned (`schema_version`) and capabilities carry their own
semver. Cross-field validators enforce coherence (irreversible markings in
sync, no undeclared params/secrets/outputs, business detector codes present
in the contract, values that look like raw credentials rejected).

## Determinism & error handling

Replay never calls a model and never guesses. Determinism comes from bounded,
polled locator resolution (each polling round tries every rung, so a stubborn
strategy cannot starve its fallbacks); explicit checkpoints after
state-changing steps; template-rendered params (`{{param:member_id}}`) in
values, URLs, and conditions; and a closed-world default.

When a wait or checkpoint fails, detectors classify the live screen with a
fixed precedence — **business** (a legitimate answer must win), then
**fatal** (a declared-broken state must not be "recovered" into), then
**recoverable**. Recoveries are data, not code: `click` (dismiss a known
interstitial, then re-verify — the interstitial may have swallowed the step),
`retry_step` (transient), `resume_from_step` (e.g. session expiry → re-run
from login), each with bounded attempts. A state *nothing* claims is a hard
failure that reports step id, expected, observed, and a screenshot — the
result contract (`results.py`) separates `success` / `business_outcome` /
`hard_failure` (+ `escalated`), and the CLI exposes them as distinct exit
codes. Transient slowness is absorbed by the bounded waits themselves; UI
drift (secondary here, per the brief) surfaces as drift signals in the log
and, at the limit, as honest `target_not_found` failures rather than wrong
actions.

The evidence layer gives every run a directory: `events.jsonl` (every
observation, decision, action, verdict — with the *why*), screenshots at key
moments and always on failure, and the final typed result.

## Heterogeneity & multi-tenant

**Surface seam.** Everything above `surface.py` speaks snapshots, elements,
targets, and conditions; only the implementation speaks Playwright.
Perception is deliberately accessibility-shaped (role, accessible name, label
proximity) rather than DOM-shaped — the same vocabulary an OS accessibility
API exposes — so a `DesktopSurface` (AX/UIA) implements the same interface,
and the locator-strategy *vocabulary* already anticipates it: `role` maps to
AX roles, `label_near` to spatial labeling, `dom_path` is the only
web-specific rung and is already the last resort. The artifact schema does
not change; a capability binds to a surface type via `app.surface`. A
screenshot+coordinates surface would slot into the same seam for the truly
hostile cases (recording anchor crops rather than paths). Frames — the
legacy-web reality — are first-class in targets today (`frame`, with `"*"`
for states that can appear at either level).

**Multi-tenant reuse.** The unit of reuse is the *vendor app*, not the
tenant: capabilities and detector profiles bind to `app_id`
(`cornerstone-teller`), and a tenant is app + configuration. The app profile
is already the override seam — a tenant profile would extend the base with
tenant-specific detectors, entry URLs, and locator overrides for
tenant-customized steps, so one recording serves the fleet with per-tenant
deltas instead of per-tenant re-recording. Drift detection is telemetry the
replay engine already emits: locator-stack rung slippage, checkpoint
failures, and recovery frequency per tenant/version pinpoint *which* variant
diverged and *where*; a capability that starts resolving on fallback rungs
for one tenant is flagged for re-validation there. Version pinning lives in
provenance + the review gate.

## Escalation & handoff

The control-transfer model is a **lease**: exactly one party controls the
live session at any moment, and that fact is written down (`control.json`).
Three triggers raise an intervention: the discovery model reports `stuck`; a
replay hard-fails (unknown state, exhausted recovery, app error); or replay
reaches an **irreversible step without caller authorization** — conservative
handling of the risky class *is* the escalation path.

An intervention carries its context: capability/goal, step, why it stopped,
a redacted screen render, the actable elements, and a screenshot. The run
process owns the browser and is its single writer; the operator console
(separate process, deliberately plain) reads that state and enqueues
commands on a filesystem bus. While the human holds the lease, the run
process executes *operator* commands — click / fill / select / navigate — on
the **same live session**, through the **same policy-checked surface**, and
records every human action into the result. `Resume` returns the lease;
the engine then *re-verifies* the current step's checkpoint (the human may
have completed the step manually) before deciding to redo it, and continues.
The evidence includes a real run: injected server error → intervention →
human navigates the session out of the error page → resume → success, with
the human's actions in `result.json`.

The mock line is drawn around the *UI only*: a production console would be a
real-time co-browse (CDP screencast) with auth and audit, behind exactly
this lease and command seam.

## Safety

- **Allowlist**: configurable origins + denied path prefixes + permitted
  action types (`config/policy.yaml`), enforced three ways: in the Surface
  before every action, at the network layer (a Playwright route rule aborts
  any request to a non-allowlisted origin, so even a hostile link's page
  load is blocked), and re-checked after actions that navigate. The demo app
  contains an admin "Purge Member Records" page precisely so the denylist
  has something real to deny.
- **Risky/irreversible actions**: classified by control-name policy at act
  time and marked per-step in the artifact at distillation. Unattended
  replay refuses them unless the caller passes `--allow-irreversible`;
  otherwise it escalates for human approval (my chosen conservative posture:
  block-by-default, human-approve on demand — logged either way). Artifacts
  ship `draft` and carry an explicit approval gate for unattended use.
- **Secrets**: the model never sees credential values. It types
  `{{secret:name}}` placeholders; the surface resolves them from the
  environment at act-time, registers the value with the redactor, and every
  log line, snapshot, and artifact field is scrubbed before touching disk. A
  schema validator additionally rejects step values that *look* like raw
  credentials. The artifact JSON of the shipped capability contains the
  placeholder, not the password — asserted by a test.
- **PII**: the demo profile page deliberately displays a (fake) SSN; the
  redaction patterns scrub it from all persisted text evidence, and
  *screenshots get the same treatment* - the rules are applied to on-screen
  text in every frame before capture and restored after
  (`WebSurface.screenshot`). Limits are honest: redaction is pattern-based,
  not semantic, and the model itself must see screen data to operate on it.

## Cuts

Deliberate, with the seam left clean:

- **Operator console UI** is minimal (auto-refreshing HTML over a file bus);
  production co-browse (CDP screencast), operator auth, and audit trails are
  designed for at the lease/command seam but not built.
- **Semantic screenshot masking** — pixel redaction covers on-screen *text*
  matching the redaction rules; PII rendered as images would need
  region-masking derived from detector/extraction geometry.
- **Desktop / screenshot surfaces** — designed (see Heterogeneity), not
  implemented; one concrete surface was the brief's scope.
- **Session reuse across invocations** — every replay logs in fresh; a
  session pool per app/tenant is the obvious next win and slots under the
  Surface without schema changes.
- **Tenant override profiles** — the base app profile *is* the mechanism;
  the overlay/merge logic is not written.
- **Assisted fallback** (bounded single-step LLM recovery on replay failure)
  — attractive stretch goal, skipped to keep replay's "no model in the loop"
  claim absolute; the escalation path covers the gap with a human instead.
- **Multi-run stability scoring** — the per-run drift signals exist; the
  aggregation job does not.

What I'd build next, in order: session reuse, tenant overlay profiles, then
a second Surface implementation to prove the seam with code rather than
argument.
