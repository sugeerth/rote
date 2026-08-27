"""Command-line entry points. `python3 -m rote <command>`.

The demo path (see README.md):

    python3 -m rote serve-target                 # terminal 1: the legacy app
    python3 -m rote discover --goal "..." ...    # terminal 2: LLM discovery -> artifact
    python3 -m rote replay --capability ...      # deterministic replay, no LLM
    python3 -m rote console --run runs/<id>      # operator console during escalation
"""

from __future__ import annotations

import argparse
import json
import sys
import urllib.request
from pathlib import Path

from .distill import AppProfile, DistillationError, distill
from .discover import discover
from .evidence import RunLog
from .handoff import InterventionHub
from .llm import AnthropicLLM, ClaudeCLILLM, pick_llm
from .policy import Policy
from .replay import ReplayOptions, replay
from .schema import Capability
from .surface import WebSurface

EXIT = {"success": 0, "hard_failure": 1, "business_outcome": 2, "escalated": 3}


def _parse_kv(pairs: list[str]) -> dict[str, str]:
    out = {}
    for pair in pairs:
        key, sep, value = pair.partition("=")
        if not sep:
            raise SystemExit(f"--param expects name=value, got {pair!r}")
        out[key] = value
    return out


def _parse_outputs(pairs: list[str]) -> list[tuple[str, str]]:
    out = []
    for pair in pairs:
        name, sep, otype = pair.partition(":")
        out.append((name, otype if sep else "string"))
    return out


def cmd_discover(args: argparse.Namespace) -> int:
    policy = Policy.from_file(args.policy)
    profile = AppProfile.load(args.app)
    params = _parse_kv(args.param)
    outputs = _parse_outputs(args.output)
    llm = {"api": AnthropicLLM, "cli": ClaudeCLILLM}.get(args.provider, pick_llm)()
    log = RunLog(args.runs_dir, "discovery", redact=policy.redact)
    surface = WebSurface(policy, headed=args.headed)
    surface.start()
    surface.bind(params)
    hub = InterventionHub(surface, log, timeout_s=args.escalation_timeout)
    print(f"run: {log.run_id}  provider: {llm.name}\nevidence: {log.dir}")
    try:
        result, trace = discover(
            goal=args.goal,
            entry_url=profile.entry_url,
            surface=surface,
            llm=llm,
            log=log,
            output_names=[name for name, _ in outputs],
            secrets=profile.secrets,
            hub=hub,
            max_steps=args.max_steps,
            timeout_s=args.timeout,
            context=profile.context,
        )
        if result.status == "success":
            try:
                capability = distill(
                    trace, surface, profile,
                    capability_id=args.id, name=args.name,
                    description=args.description or args.goal,
                    params=params, outputs=outputs,
                )
            except DistillationError as exc:
                result.status = "hard_failure"
                print(f"distillation failed: {exc}", file=sys.stderr)
        log.save_json("result.json", result)
        print(result.summary())
        if result.status != "success":
            return EXIT[result.status]
    finally:
        surface.close()

    # A capability only counts once a verification replay passes: a trace
    # with detours (a failed attempt, a human-completed gap) can distill into
    # an artifact that cannot actually replay. Verify before saving.
    vlog = RunLog(args.runs_dir, "verify", redact=policy.redact)
    vsurface = WebSurface(policy, headed=args.headed)
    vsurface.start()
    try:
        verification = replay(capability, dict(params), vsurface, vlog,
                              options=ReplayOptions(allow_irreversible=False))
    finally:
        vsurface.close()
    vlog.save_json("result.json", verification)
    if verification.status != "success":
        print(f"verification replay FAILED ({verification.summary()}); artifact not saved.\n"
              f"Fix the cause (see {vlog.dir}) and re-run discovery.", file=sys.stderr)
        return 1
    path = capability.save(Path(args.capabilities_dir) / f"{args.id}.json")
    log.save_json("capability.json", capability)
    print(f"verification replay passed (outputs: {verification.outputs})")
    print(f"capability saved: {path}")
    return 0


def cmd_replay(args: argparse.Namespace) -> int:
    policy = Policy.from_file(args.policy)
    capability = Capability.load(args.capability)
    log = RunLog(args.runs_dir, "replay", redact=policy.redact)
    surface = WebSurface(policy, headed=args.headed)
    surface.start()
    hub = None if args.no_escalate else InterventionHub(surface, log, timeout_s=args.escalation_timeout)
    print(f"run: {log.run_id}  capability: {capability.id} v{capability.version}\nevidence: {log.dir}")
    if capability.review.status != "approved":
        print("note: capability is a draft (not yet approved); review before unattended use")
    try:
        result = replay(
            capability,
            _parse_kv(args.param),
            surface,
            log,
            hub=hub,
            options=ReplayOptions(allow_irreversible=args.allow_irreversible),
        )
        log.save_json("result.json", result)
        print(json.dumps(json.loads(result.model_dump_json()), indent=2) if args.json else result.summary())
        return EXIT[result.status]
    finally:
        surface.close()


def cmd_console(args: argparse.Namespace) -> int:
    from .console import main as console_main

    console_main(args.run, port=args.port)
    return 0


def cmd_serve_target(args: argparse.Namespace) -> int:
    from target_app.app import main as target_main

    target_main()
    return 0


def cmd_approve(args: argparse.Namespace) -> int:
    capability = Capability.load(args.capability)
    capability.review.status = "approved"
    capability.review.notes = args.notes or capability.review.notes
    capability.save(args.capability)
    print(f"{capability.id} v{capability.version}: approved")
    return 0


def cmd_catalog(args: argparse.Namespace) -> int:
    """Agent-facing capability interface: saved artifacts as a tool catalog."""
    catalog = []
    for path in sorted(Path(args.capabilities_dir).glob("*.json")):
        cap = Capability.load(path)
        catalog.append({
            "name": cap.id,
            "description": cap.description,
            "review_status": cap.review.status,
            "input_schema": {
                "type": "object",
                "properties": {
                    p.name: {"type": p.type, "description": p.description,
                             **({"pattern": p.pattern} if p.pattern else {})}
                    for p in cap.contract.params
                },
                "required": [p.name for p in cap.contract.params if p.required],
            },
            "outputs": {o.name: o.type for o in cap.contract.outputs},
            "possible_outcomes": [o.code for o in cap.contract.outcomes],
            "invoke": f"python3 -m rote replay --capability {path} " +
                      " ".join(f"--param {p.name}=<{p.type}>" for p in cap.contract.params),
        })
    print(json.dumps(catalog, indent=2))
    return 0


def cmd_fault(args: argparse.Namespace) -> int:
    """Demo helper: arm a one-shot fault in the local target app (test harness,
    not an agent action - the agent itself is denied /debug/* by policy)."""
    data = f"fault={args.kind}".encode()
    with urllib.request.urlopen(f"{args.target}/debug/fault", data=data) as resp:
        print(f"fault armed: {args.kind} ({resp.status})")
    return 0


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="rote")
    sub = parser.add_subparsers(dest="command", required=True)

    d = sub.add_parser("discover", help="LLM-driven discovery run; saves a capability artifact on success")
    d.add_argument("--goal", required=True)
    d.add_argument("--app", default="config/apps/cornerstone-teller.yaml")
    d.add_argument("--id", required=True, help="capability id (slug)")
    d.add_argument("--name", required=True)
    d.add_argument("--description", default="")
    d.add_argument("--param", action="append", default=[], metavar="name=value")
    d.add_argument("--output", action="append", default=[], metavar="name:type")
    d.add_argument("--policy", default="config/policy.yaml")
    d.add_argument("--provider", choices=["auto", "api", "cli"], default="auto")
    d.add_argument("--headed", action="store_true")
    d.add_argument("--max-steps", type=int, default=25)
    d.add_argument("--timeout", type=int, default=600)
    d.add_argument("--escalation-timeout", type=int, default=600)
    d.add_argument("--runs-dir", default="runs")
    d.add_argument("--capabilities-dir", default="capabilities")
    d.set_defaults(func=cmd_discover)

    r = sub.add_parser("replay", help="deterministic replay of a capability (no LLM)")
    r.add_argument("--capability", required=True)
    r.add_argument("--param", action="append", default=[], metavar="name=value")
    r.add_argument("--policy", default="config/policy.yaml")
    r.add_argument("--headed", action="store_true")
    r.add_argument("--allow-irreversible", action="store_true",
                   help="caller authorizes irreversible steps for this invocation")
    r.add_argument("--no-escalate", action="store_true",
                   help="fail instead of raising an intervention on hard failures")
    r.add_argument("--escalation-timeout", type=int, default=600)
    r.add_argument("--runs-dir", default="runs")
    r.add_argument("--json", action="store_true", help="print the full result contract")
    r.set_defaults(func=cmd_replay)

    c = sub.add_parser("console", help="operator console for a run's escalations")
    c.add_argument("--run", required=True, help="run directory, e.g. runs/replay-...")
    c.add_argument("--port", type=int, default=7720)
    c.set_defaults(func=cmd_console)

    t = sub.add_parser("serve-target", help="run the demo legacy credit-union app")
    t.set_defaults(func=cmd_serve_target)

    a = sub.add_parser("approve", help="mark a capability approved for unattended replay")
    a.add_argument("--capability", required=True)
    a.add_argument("--notes", default="")
    a.set_defaults(func=cmd_approve)

    g = sub.add_parser("catalog", help="list saved capabilities as an agent tool catalog")
    g.add_argument("--capabilities-dir", default="capabilities")
    g.set_defaults(func=cmd_catalog)

    f = sub.add_parser("fault", help="arm a one-shot fault in the local target app (demo)")
    f.add_argument("kind", choices=["none", "slow", "error500", "timeout_next", "notice_next"])
    f.add_argument("--target", default="http://127.0.0.1:7710")
    f.set_defaults(func=cmd_fault)

    args = parser.parse_args(argv)
    return args.func(args)


if __name__ == "__main__":
    raise SystemExit(main())
