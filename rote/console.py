"""Operator console: the minimal-but-real human side of the handoff.

A deliberately plain Flask page (auto-refreshing, no JavaScript) that reads
the run's handoff directory and writes command files. It never touches the
browser: the run process is the only writer to the live session, and it
executes these commands only while the human holds the control lease.

This UI is the mocked component the brief allows; the handoff mechanism
behind it (lease, command bus, capture, resume) is real. See REPORT.md.
"""

from __future__ import annotations

import json
from pathlib import Path

from flask import Flask, redirect, request, send_file

from .handoff import send_command

_PAGE = """<!doctype html>
<html><head><meta http-equiv="refresh" content="3"><title>rote operator console</title>
<style>
 body {{ font-family: -apple-system, Segoe UI, sans-serif; margin: 2rem; max-width: 70rem; }}
 .lease {{ padding: .6rem 1rem; border-radius: .4rem; display: inline-block; margin-bottom: 1rem; }}
 .human {{ background: #fde8e8; border: 1px solid #c0392b; }}
 .automation {{ background: #e8f6ee; border: 1px solid #1e8449; }}
 pre {{ background: #f4f4f4; padding: 1rem; overflow-x: auto; max-height: 22rem; }}
 img {{ max-width: 100%; border: 1px solid #ccc; }}
 table {{ border-collapse: collapse; }} td, th {{ border: 1px solid #ddd; padding: .3rem .6rem; text-align: left; }}
 form.inline {{ display: inline; }}
 .actions {{ margin: 1rem 0; }}
</style></head><body>
<h1>rote operator console</h1>
<p>run: <code>{run_dir}</code></p>
<div class="lease {controller}">controller: <b>{controller}</b> - {note}</div>
{body}
</body></html>"""

_INTERVENTION = """
<h2>Intervention {iid}</h2>
<p><b>Why automation stopped:</b> {reason}</p>
<p><b>Capability / goal:</b> {goal} &nbsp; <b>step:</b> {step}</p>
<div class="actions">
<form class="inline" method="post" action="/command"><input type="hidden" name="kind" value="resume">
  <button>Resume automation</button></form>
<form class="inline" method="post" action="/command"><input type="hidden" name="kind" value="abandon">
  <button>Abandon run</button></form>
<form class="inline" method="post" action="/command"><input type="hidden" name="kind" value="observe">
  <button>Refresh screen state</button></form>
</div>
<h3>Act on the live session</h3>
<form method="post" action="/command">
 <input type="hidden" name="kind" value="act">
 action <select name="action"><option>click</option><option>fill</option><option>select</option><option>press</option><option>navigate</option></select>
 element # <input name="element" size="4"> (blank for navigate)
 value <input name="value" size="24">
 <button>Send</button>
</form>
<h3>Current screen</h3>
{screenshot}
<h3>Actable elements</h3>
<table><tr><th>#</th><th>control</th></tr>{elements}</table>
<h3>Screen text (redacted)</h3>
<pre>{screen}</pre>
"""


def create_app(run_dir: str) -> Flask:
    app = Flask(__name__)
    root = Path(run_dir) / "handoff"

    def read_json(name: str, default):
        try:
            return json.loads((root / name).read_text())
        except (OSError, json.JSONDecodeError):
            return default

    @app.get("/")
    def index():
        control = read_json("control.json", {"controller": "unknown", "note": "no control file yet"})
        if control.get("controller") != "human":
            body = "<p>No intervention pending. Automation holds the session lease.</p>"
        else:
            context = read_json("context.json", {})
            elements = read_json("elements.json", [])
            screen = ""
            try:
                screen = (root / "screen.txt").read_text()
            except OSError:
                pass
            shot = (
                '<img src="/screenshot.png" alt="live session screenshot">'
                if (root / "screenshot.png").exists()
                else "<p>(no screenshot)</p>"
            )
            body = _INTERVENTION.format(
                iid=context.get("intervention", "?"),
                reason=_esc(context.get("reason", "")),
                goal=_esc(context.get("goal", "")),
                step=_esc(str(context.get("step_id"))),
                screenshot=shot,
                elements="".join(
                    f"<tr><td>{e['index']}</td><td>{_esc(e['brief'])}</td></tr>" for e in elements
                ),
                screen=_esc(screen),
            )
        return _PAGE.format(
            run_dir=_esc(str(run_dir)),
            controller=_esc(control.get("controller", "unknown")),
            note=_esc(control.get("note", "")),
            body=body,
        )

    @app.get("/screenshot.png")
    def screenshot():
        return send_file(root / "screenshot.png", mimetype="image/png")

    @app.post("/command")
    def command():
        form = request.form
        cmd: dict = {"kind": form["kind"]}
        if cmd["kind"] == "act":
            cmd.update(action=form["action"], element=form["element"], value=form.get("value") or None)
        try:
            iid = json.loads((root / "context.json").read_text()).get("intervention")
        except (OSError, json.JSONDecodeError):
            iid = None
        send_command(run_dir, cmd, intervention=iid)
        return redirect("/")

    return app


def _esc(text: str) -> str:
    return (
        text.replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")
    )


def main(run_dir: str, port: int = 7720) -> None:
    create_app(run_dir).run(host="127.0.0.1", port=port)
