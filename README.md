# rote

**Discover a UI task once with an LLM. Replay it deterministically forever.**

`rote` is a computer-use automation system for the long tail of legacy
back-office applications that have no API. An LLM drives the live UI to
accomplish a natural-language goal *once* (discovery); what it learned is
distilled into a typed, versioned, reviewable **capability artifact**; and
from then on an AI agent invokes that capability through **deterministic
replay** — no model in the decision loop, bounded waits, explicit
checkpoints, and an error taxonomy that distinguishes business outcomes from
recoverable conditions from hard failures. When the system can't safely
proceed, it escalates to a human operator who takes control of the *same*
live session and hands it back.

The repo ships its own proxy target: a deliberately hostile "2003-era
credit-union teller system" (table-soup markup, an iframe workarea, no ids,
no test attributes, fault injection) so every claim above is demonstrable
locally. See [`REPORT.md`](REPORT.md) for design decisions and
[`evidence/`](evidence/) for real runs.

```
goal ──LLM──▶ discovery run ──distill──▶ capability.json ──replay──▶ outputs
                   │                        (typed contract)             │
                   └────────────── /evidence/ ◀──────────────────────────┘
```

## Layout

| Path | What it is |
|---|---|
| `rote/schema.py` | The capability artifact schema (the contract; start here) |
| `rote/surface.py` | The Surface seam: perception + action + policy choke point (Playwright impl) |
| `rote/discover.py` | LLM observe → decide → act loop |
| `rote/distill.py` | Mechanical trace → artifact distillation (no LLM) |
| `rote/replay.py` | Deterministic replay engine + error classification |
| `rote/policy.py` | Allowlist, action permissions, risk classes, redaction |
| `rote/handoff.py` / `rote/console.py` | Escalation hub (control lease) + operator console |
| `rote/evidence.py` / `rote/results.py` | Run logs & screenshots / the result contract |
| `rote/llm.py` | LLM provider seam (Anthropic API, `claude` CLI shim, scripted fake) |
| `target_app/` | The hostile legacy demo app (Flask) |
| `config/` | Guardrail policy + per-app detector profiles |
| `capabilities/` | Saved capability artifacts |
| `evidence/` | Curated evidence from real runs (discovery + replays + escalation) |

## Setup

Python 3.10+ (developed on 3.12).

```bash
python3 -m venv .venv && source .venv/bin/activate
pip install -e ".[dev]"
python3 -m playwright install chromium

# The demo app's teller credential (fake, hardcoded in the demo target).
# The automation system itself only ever sees it as {{secret:teller_password}}.
export ROTE_TELLER_PASSWORD=spring2026-demo

# For discovery (the only phase that needs a model):
export ANTHROPIC_API_KEY=sk-ant-...   # preferred
# ...or, if unset, discovery falls back to the `claude` CLI (subscription auth).
```

**Running without live services:** everything is local — the target app is
part of the repo — and the full test suite (115 tests, including a complete
end-to-end pipeline run) uses a deterministic scripted provider, so **tests
need no model access at all**: `python3 -m pytest`. Only a fresh discovery
run needs a model, by design: the brief requires the discovery run to be real.

## Demo path

Terminal 1 — the legacy target app:

```bash
python3 -m rote serve-target        # http://127.0.0.1:7710
```

Terminal 2 — discover, then replay:

```bash
# 1. LLM-driven discovery -> saves capabilities/lookup-member-savings.json
python3 -m rote discover \
  --goal "Look up member 12345 and read their current savings balance" \
  --id lookup-member-savings \
  --name "Look up member savings balance" \
  --description "Given a member number, return the member's current Regular Savings balance" \
  --param member_id=12345 \
  --output savings_balance:money

# 2. Review + approve the artifact for unattended use
python3 -m rote approve --capability capabilities/lookup-member-savings.json

# 3. Deterministic replay, DIFFERENT member (no LLM in the loop)
python3 -m rote replay --capability capabilities/lookup-member-savings.json \
  --param member_id=12346 --json          # exit 0, outputs.savings_balance = 310.09

# 4. Business outcome is a result, not a crash
python3 -m rote replay --capability capabilities/lookup-member-savings.json \
  --param member_id=99999                 # exit 2, outcome member_not_found

# 5. Injected runtime error -> declared recovery (re-login and resume)
python3 -m rote fault timeout_next
python3 -m rote replay --capability capabilities/lookup-member-savings.json \
  --param member_id=12345                 # exit 0; events show session_expired -> resume

# 6. Escalation & handoff: inject a server error, watch replay hand the live
#    session to a human, fix it in the console, resume to success
python3 -m rote fault error500
python3 -m rote replay --capability capabilities/lookup-member-savings.json \
  --param member_id=12345                 # blocks, prints the run dir
# Terminal 3:  python3 -m rote console --run runs/<that run id>
#              open http://127.0.0.1:7720 -> act on the live session -> Resume

# 7. The agent-facing catalog (capabilities as callable tools)
python3 -m rote catalog
```

Replay exit codes are part of the caller contract: `0` success · `1` hard
failure · `2` business outcome · `3` escalated-and-abandoned. The full typed
result (outputs, outcome code, failure detail, step reports, interventions)
is in each run's `result.json`, or on stdout with `--json`.

## Tests

```bash
python3 -m pytest        # 115 tests: schema, policy, replay taxonomy, distiller,
                         # target app, and a full end-to-end pipeline (no model needed)
```
