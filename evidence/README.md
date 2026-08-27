# Evidence

Curated copies of real run directories (see `rote/evidence.py` for the
format: `events.jsonl` is the structured what-and-why log; numbered PNGs are
screenshots at key moments and on failures; `result.json` is the typed
result contract; `handoff/` is the escalation state the operator console
renders). Each curated directory is a verbatim copy of `runs/<run_id>/` —
the original id is the `run_id` in its `result.json`; `runs/` itself is
gitignored. All evidence passes the redaction layer, screenshots included
(the rules are applied to on-screen text before capture): the demo profile
page displays a fake SSN, which appears here only as `***-**-****`, and the
teller secret appears only as its `{{secret:teller_password}}` placeholder.

Model transport note: this machine had no `ANTHROPIC_API_KEY`, so the
discovery runs used the `claude` CLI shim (subscription auth) — the
`provider` field in every event and in the artifact's provenance records
exactly that (`claude-cli:sonnet`). The decisions are a real LLM's; only the
transport differs from the production `AnthropicLLM` path.

| Directory | What it shows |
|---|---|
| `discovery-run/` | The genuine LLM-driven discovery run: goal → observe/decide/act (with per-step "why") → success reading `$4,982.17`. Includes `capability.json` as distilled. |
| `verification-replay/` | The automatic replay that gates artifact saving: same params, no LLM, typed output `4982.17`. |
| `discovery-with-intervention/` | An earlier genuine run where the model, not knowing the teller User ID, tried `teller`, hit "Invalid credentials.", and reported **stuck**. A human operator took the control lease on the same live session (`handoff/`), fixed the User ID, signed in, and resumed; the model then completed the goal. Two lessons fed back into the system: app profiles now carry non-secret operator context, and — because this run's trace distilled into a non-replayable artifact — capabilities are now saved only after a verification replay passes. (This run predates capture-time screenshot masking; its two profile screenshots were masked during curation and are labeled as such.) |
| `capability-lookup-member-savings.json` | The saved capability artifact (typed params/outputs, declared outcomes, locator stacks with rationale, checkpoints, detectors, safety metadata, provenance). |
| `replay-success-parameterized/` | Deterministic replay with a **different** member (12346): no LLM, returns `310.09`. |
| `replay-business-outcome-not-found/` | Replay with member 99999: the app's "No matching member records were found." is classified as the declared business outcome `member_not_found` (exit code 2) — a result, not a crash. |
| `replay-recovery-session-expiry/` | Replay with an injected one-shot session expiry: the detector fires, the declared recovery re-runs from `s1` (re-login), and the run still succeeds. `events.jsonl` shows `detector_fired` → `recovery_resume`. |
| `replay-escalation-handoff/` | Replay with an injected server error: hard failure at `s4` with expected/observed/screenshot → intervention raised → the operator, through the real console UI (screenshots included), navigates the **same live session** out of the error page → resume → the engine re-verifies the checkpoint, records the step as completed by the human, and finishes with `success`. The human's actions are in `result.json` under `interventions`. |
