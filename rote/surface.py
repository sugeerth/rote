"""The Surface: how the system perceives and acts on an application.

This module is the seam described in REPORT.md. Everything above it - the
discovery loop, the replay engine, the operator handoff - speaks in terms of
snapshots, elements, targets, and conditions. Everything below it is
Playwright driving a browser. A legacy-web or desktop surface implements the
same interface with a different perceiver/actor underneath.

Perception is deliberately accessibility-shaped rather than DOM-shaped: the
snapshot lists controls by role, accessible name, and *label proximity* (the
text a human operator reads next to a field), because that is what survives
on hostile markup with no ids and no test attributes - and it is the same
vocabulary an OS accessibility API exposes for desktop apps.

Every action - model-driven, replayed, or human-via-console - passes through
the guardrail policy here. There is no other way to touch the UI.
"""

from __future__ import annotations

import re
import time
from dataclasses import dataclass, field

from playwright.sync_api import Frame, Locator, sync_playwright

from .policy import Policy, PolicyDecision
from .schema import Condition, TargetRef, TEMPLATE_RE

MAIN_FRAME = "(main)"


class TargetNotFound(Exception):
    """No locator in the stack resolved within the budget."""


@dataclass(frozen=True)
class Element:
    """One interactive control as perceived on the current screen."""

    index: int
    frame: str
    tag: str
    role: str
    name: str  # accessible name: link/button text, or label for a field
    input_name: str  # legacy name= attribute, often stable for decades
    label: str  # nearby label text (table-cell proximity)
    value_hint: str  # current value; never populated for password fields
    href: str
    type: str
    disabled: bool
    dom_path: str
    options: tuple[str, ...] = ()

    def brief(self) -> str:
        bits = [f"[e{self.index}] {self.role} \"{self.name}\""]
        if self.input_name:
            bits.append(f"name={self.input_name}")
        if self.value_hint:
            bits.append(f"value={self.value_hint!r}")
        if self.options:
            bits.append(f"options={list(self.options)}")
        if self.disabled:
            bits.append("disabled")
        if self.frame != MAIN_FRAME:
            bits.append(f"frame={self.frame}")
        return " ".join(bits)


@dataclass
class Snapshot:
    url: str
    title: str
    frame_text: dict[str, str] = field(default_factory=dict)
    elements: list[Element] = field(default_factory=list)

    def render(self, max_text: int = 3000) -> str:
        """Compact, model-facing view of the screen."""
        parts = [f"URL: {self.url}", f"TITLE: {self.title}"]
        for frame, text in self.frame_text.items():
            excerpt = re.sub(r"\n{2,}", "\n", text).strip()[:max_text]
            parts.append(f"--- visible text ({frame}) ---\n{excerpt or '(empty)'}")
        parts.append("--- interactive elements ---")
        parts.extend(el.brief() for el in self.elements)
        return "\n".join(parts)

    def element(self, index: int) -> Element:
        for el in self.elements:
            if el.index == index:
                return el
        raise KeyError(f"no element e{index} on the current screen")

    def visible_text(self) -> str:
        return "\n".join(self.frame_text.values())


# JS run in every frame to enumerate controls the way an operator sees them.
_COLLECT_JS = """
() => {
  const out = [];
  const path = (el) => {
    const parts = [];
    for (let n = el; n && n.nodeType === 1 && n.tagName !== 'HTML'; n = n.parentElement) {
      let idx = 1, sib = n;
      while ((sib = sib.previousElementSibling)) if (sib.tagName === n.tagName) idx++;
      parts.unshift(n.tagName.toLowerCase() + ':nth-of-type(' + idx + ')');
    }
    return parts.join(' > ');
  };
  const cellLabel = (el) => {
    const td = el.closest('td');
    if (!td) return '';
    const prev = td.previousElementSibling;
    if (prev && prev.textContent.trim()) return prev.textContent.trim();
    return '';
  };
  for (const el of document.querySelectorAll('a, button, input, select, textarea')) {
    if (el.type === 'hidden') continue;
    const style = window.getComputedStyle(el);
    if (style.display === 'none' || style.visibility === 'hidden') continue;
    const tag = el.tagName.toLowerCase();
    let role = 'generic', name = '';
    if (tag === 'a') { role = 'link'; name = el.textContent.trim(); }
    else if (tag === 'button') { role = 'button'; name = el.textContent.trim(); }
    else if (tag === 'input' && (el.type === 'submit' || el.type === 'button')) { role = 'button'; name = el.value || ''; }
    else if (tag === 'select') { role = 'combobox'; name = el.getAttribute('aria-label') || cellLabel(el) || el.name || ''; }
    else { role = 'textbox'; name = el.getAttribute('aria-label') || cellLabel(el) || el.name || ''; }
    out.push({
      tag, role, name,
      input_name: el.name || '',
      label: ['input', 'select', 'textarea'].includes(tag) ? cellLabel(el) : '',
      value_hint: el.type === 'password' ? '' :
        (tag === 'select' ? (el.selectedIndex >= 0 ? el.options[el.selectedIndex].text : '')
         : (['input', 'textarea'].includes(tag) ? (el.value || '') : '')),
      href: tag === 'a' ? (el.getAttribute('href') || '') : '',
      type: el.type || '',
      disabled: !!el.disabled,
      dom_path: path(el),
      options: tag === 'select' ? Array.from(el.options).map(o => o.text) : [],
    });
    if (out.length >= 80) break;
  }
  return out;
}
"""


# Resolve a semantic table address to the concrete cell's CSS path.
_TABLE_CELL_JS = """
(args) => {
  const norm = (t) => t.replace(/\\s+/g, ' ').trim();
  const path = (el) => {
    const parts = [];
    for (let n = el; n && n.nodeType === 1 && n.tagName !== 'HTML'; n = n.parentElement) {
      let idx = 1, sib = n;
      while ((sib = sib.previousElementSibling)) if (sib.tagName === n.tagName) idx++;
      parts.unshift(n.tagName.toLowerCase() + ':nth-of-type(' + idx + ')');
    }
    return parts.join(' > ');
  };
  for (const table of document.querySelectorAll('table')) {
    const rows = Array.from(table.rows || []);
    if (rows.length < 2) continue;
    const headers = Array.from(rows[0].cells).map((c) => norm(c.textContent));
    const col = headers.indexOf(args.col_header);
    if (col < 0) continue;
    for (const row of rows.slice(1)) {
      const cells = Array.from(row.cells);
      if (cells.some((c) => norm(c.textContent) === args.row_contains) && cells[col]) {
        return path(cells[col]);
      }
    }
  }
  return null;
}
"""

# Find where a value lives on screen and describe its context for extraction.
_FIND_VALUE_JS = """
(value) => {
  const norm = (t) => t.replace(/\\s+/g, ' ').trim();
  const path = (el) => {
    const parts = [];
    for (let n = el; n && n.nodeType === 1 && n.tagName !== 'HTML'; n = n.parentElement) {
      let idx = 1, sib = n;
      while ((sib = sib.previousElementSibling)) if (sib.tagName === n.tagName) idx++;
      parts.unshift(n.tagName.toLowerCase() + ':nth-of-type(' + idx + ')');
    }
    return parts.join(' > ');
  };
  let best = null;
  for (const el of document.body.querySelectorAll('td, th, b, font, span, div, p')) {
    if (norm(el.textContent) === value) best = el;  // last match = innermost in doc order
  }
  if (!best) return { kind: 'none' };
  const cell = best.closest('td');
  if (cell) {
    const row = cell.parentElement;
    const table = cell.closest('table');
    const rows = table ? Array.from(table.rows) : [];
    if (rows.length >= 2 && row !== rows[0]) {
      const headers = Array.from(rows[0].cells).map((c) => norm(c.textContent));
      return {
        kind: 'table',
        headers,
        col_index: cell.cellIndex,
        row_cells: Array.from(row.cells).map((c) => norm(c.textContent)),
        cell_path: path(cell),
      };
    }
  }
  return { kind: 'text', path: path(best) };
}
"""


# Mask/unmask on-screen text for screenshot redaction (see WebSurface.screenshot).
_MASK_JS = """
(rules) => {
  const swaps = [];
  const it = document.createNodeIterator(document.body, NodeFilter.SHOW_TEXT);
  let node;
  while ((node = it.nextNode())) {
    const original = node.nodeValue;
    let text = original;
    for (const [src, flags, repl] of rules) {
      try { text = text.replace(new RegExp(src, flags), repl); } catch (e) {}
    }
    if (text !== original) { swaps.push([node, original]); node.nodeValue = text; }
  }
  window.__rote_mask_swaps = swaps;
  return swaps.length;
}
"""

_UNMASK_JS = """
() => {
  for (const [node, original] of (window.__rote_mask_swaps || [])) {
    try { node.nodeValue = original; } catch (e) {}
  }
  window.__rote_mask_swaps = [];
}
"""


class WebSurface:
    """Browser implementation of the surface seam (Playwright/Chromium)."""

    def __init__(self, policy: Policy, headed: bool = False):
        self.policy = policy
        self.headed = headed
        self._params: dict[str, str] = {}
        self._pw = None
        self._page = None

    # -- lifecycle ----------------------------------------------------------

    def start(self) -> None:
        self._pw = sync_playwright().start()
        browser = self._pw.chromium.launch(headless=not self.headed)
        context = browser.new_context(viewport={"width": 1180, "height": 860})
        # Network-level deny-by-default: even a hostile link's page load is
        # blocked at the request layer, not just detected after the fact.
        context.route("**/*", self._route_gate)
        self._page = context.new_page()

    def _route_gate(self, route) -> None:
        if self.policy.check_url(route.request.url).allowed:
            route.continue_()
        else:
            route.abort()

    def close(self) -> None:
        if self._pw:
            self._pw.stop()
            self._pw = self._page = None

    def bind(self, params: dict[str, str]) -> None:
        """Set the per-invocation values that '{{param:...}}' templates resolve to."""
        self._params = dict(params)

    # -- perception ---------------------------------------------------------

    def snapshot(self) -> Snapshot:
        page = self._require_page()
        snap = Snapshot(url=page.url, title=page.title())
        index = 0
        for frame in page.frames:
            fname = self._frame_name(frame)
            try:
                snap.frame_text[fname] = frame.evaluate(
                    "() => document.body ? document.body.innerText : ''"
                )
                raw = frame.evaluate(_COLLECT_JS)
            except Exception:  # frame detached mid-read
                continue
            for item in raw:
                snap.elements.append(
                    Element(
                        index=index,
                        frame=fname,
                        tag=item["tag"],
                        role=item["role"],
                        name=item["name"],
                        input_name=item["input_name"],
                        label=item["label"],
                        value_hint=item["value_hint"],
                        href=item["href"],
                        type=item["type"],
                        disabled=item["disabled"],
                        dom_path=item["dom_path"],
                        options=tuple(item["options"]),
                    )
                )
                index += 1
        return snap

    def current_url(self) -> str:
        return self._require_page().url

    def screenshot(self, path: str) -> str:
        """Capture the page with the redaction rules applied to the pixels.

        On-screen text matching the policy's redaction patterns (and any
        resolved secret values) is masked in every frame before capture and
        restored afterwards, so image evidence gets the same treatment as
        text evidence.
        """
        page = self._require_page()
        rules = self.policy.redactor.js_mask_rules()
        masked = []
        for frame in page.frames:
            try:
                frame.evaluate(_MASK_JS, rules)
                masked.append(frame)
            except Exception:
                pass
        try:
            page.screenshot(path=path, full_page=True)
        finally:
            for frame in masked:
                try:
                    frame.evaluate(_UNMASK_JS)
                except Exception:
                    pass
        return path

    # -- action (the policy choke point) ------------------------------------

    def navigate(self, url: str, actor: str = "model") -> PolicyDecision:
        url = self._render_value(url)[0]
        decision = self.policy.enforce("navigate", url)
        self._require_page().goto(url, wait_until="domcontentloaded")
        self._post_act_check()
        return decision

    def act_element(
        self, kind: str, element: Element, value: str | None = None, actor: str = "model"
    ) -> PolicyDecision:
        """Act on an element from the *current* snapshot (discovery / operator)."""
        frame = self._frame(element.frame)
        decision = self.policy.enforce(kind, frame.url, element.name or element.label)
        self._apply(kind, frame.locator(element.dom_path).first, value)
        self._post_act_check()
        return decision

    def act_target(
        self, kind: str, target: TargetRef, value: str | None = None, budget_ms: int = 5000
    ) -> tuple[PolicyDecision, str]:
        """Act on a recorded TargetRef (deterministic replay)."""
        locator, used, frame = self.resolve_target(target, budget_ms)
        decision = self.policy.enforce(kind, frame.url, target.described_as)
        self._apply(kind, locator, value)
        self._post_act_check()
        return decision, used

    def _apply(self, kind: str, locator: Locator, value: str | None) -> None:
        if kind == "click":
            locator.click(timeout=5000)
        elif kind == "fill":
            rendered, _safe = self._render_value(value or "")
            locator.fill(rendered, timeout=5000)
        elif kind == "select":
            rendered, _safe = self._render_value(value or "")
            locator.select_option(label=rendered, timeout=5000)
        elif kind == "press":
            locator.press(value or "Enter", timeout=5000)
        else:
            raise ValueError(f"unsupported action kind: {kind}")
        # Let same-document updates and frame navigations settle.
        page = self._require_page()
        page.wait_for_timeout(150)
        for frame in page.frames:
            try:
                frame.wait_for_load_state("domcontentloaded", timeout=4000)
            except Exception:
                pass

    def _post_act_check(self) -> None:
        """An action may cause navigation; verify we are still inside the allowlist."""
        for frame in self._require_page().frames:
            if frame.url and not frame.url.startswith("about:"):
                decision = self.policy.check_url(frame.url)
                if not decision.allowed:
                    raise PolicyViolationNavigation(decision.reason)

    # -- deterministic target resolution ------------------------------------

    def resolve_target(self, target: TargetRef, budget_ms: int = 5000) -> tuple[Locator, str, Frame]:
        """Try the locator stack in order, in polling rounds, until the budget expires.

        Earlier locators are preferred every round, but one stubborn strategy
        cannot starve its fallbacks: each round gives every strategy a chance.
        A target frame of "*" searches every frame - used by app-profile
        recovery targets, since an interstitial may replace either the whole
        page or just the frame the flow was in.
        """
        deadline = time.monotonic() + budget_ms / 1000
        attempts: list[str] = []
        while True:
            for frame in self._candidate_frames(target.frame):
                for i, loc in enumerate(target.locators):
                    candidate = self._try_locator(frame, loc.strategy, loc.value)
                    if candidate is not None:
                        return candidate, f"{loc.strategy}#{i}", frame
                    attempts.append(f"{loc.strategy}#{i}")
            if not attempts:
                attempts.append(f"frame '{target.frame}' missing")
            if time.monotonic() >= deadline:
                raise TargetNotFound(
                    f"could not resolve {target.described_as!r} "
                    f"(frame={target.frame or MAIN_FRAME}); tried {sorted(set(attempts))}"
                )
            time.sleep(0.2)

    def _candidate_frames(self, name: str | None) -> list[Frame]:
        if name == "*":
            return list(self._require_page().frames)
        frame = self._frame(name or MAIN_FRAME, required=False)
        return [frame] if frame is not None else []

    def _try_locator(self, frame: Frame, strategy: str, value: dict) -> Locator | None:
        try:
            if strategy == "role":
                loc = frame.get_by_role(value["role"], name=value["name"], exact=True)
            elif strategy == "text":
                loc = frame.get_by_text(str(value["text"]), exact=True)
            elif strategy == "label_near":
                loc = self._by_label(frame, str(value["text"]), str(value.get("control", "")))
                if loc is None:
                    return None
            elif strategy == "table_cell":
                path = frame.evaluate(
                    _TABLE_CELL_JS,
                    {"row_contains": str(value["row_contains"]), "col_header": str(value["col_header"])},
                )
                if not path:
                    return None
                loc = frame.locator(path)
            elif strategy in ("attr", "dom_path"):
                loc = frame.locator(str(value["selector"]))
            else:
                return None
            count = loc.count()
            if count == 0:
                return None
            if count > 1 and strategy in ("role", "text", "label_near"):
                return None  # ambiguity is failure; fall through to the next strategy
            first = loc.first
            return first if first.is_visible() else None
        except Exception:
            return None

    def _by_label(self, frame: Frame, label: str, control: str) -> Locator | None:
        """Legacy-markup labeling: the cell text next to a field IS its label."""
        for item in frame.evaluate(_COLLECT_JS):
            if item["label"] == label and (not control or item["role"] == control):
                return frame.locator(item["dom_path"])
        return None

    # -- reading & conditions ------------------------------------------------

    def read_target(self, target: TargetRef, parse: str = "text", budget_ms: int = 5000) -> str | int | float:
        locator, _used, _frame = self.resolve_target(target, budget_ms)
        tag = locator.evaluate("el => el.tagName.toLowerCase()")
        raw = locator.input_value() if tag in ("input", "select", "textarea") else locator.inner_text()
        raw = raw.strip()
        if parse == "money":
            return float(re.sub(r"[^0-9.\-]", "", raw))
        if parse == "integer":
            return int(re.sub(r"[^0-9\-]", "", raw))
        return raw

    def condition_holds(self, cond: Condition) -> bool:
        """Evaluate a Condition, waiting up to its bounded budget.

        Condition values may carry ``{{param:...}}`` templates; they are
        rendered against the bound invocation params before evaluation.
        """
        value = self._render_value(cond.value)[0]
        deadline = time.monotonic() + cond.within_ms / 1000
        while True:
            if self._condition_now(cond, value):
                return True
            if time.monotonic() >= deadline:
                return False
            time.sleep(0.2)

    def _condition_now(self, cond: Condition, value: str) -> bool:
        page = self._require_page()
        try:
            if cond.kind == "url_contains":
                urls = [page.url] + [f.url for f in page.frames]
                return any(value in u for u in urls)
            if cond.kind == "element_visible":
                frame = self._frame(cond.frame or MAIN_FRAME, required=False)
                if frame is None:
                    return False
                loc = frame.locator(value)
                return loc.count() > 0 and loc.first.is_visible()
            # text_visible / text_absent
            frames = (
                [self._frame(cond.frame, required=False)]
                if cond.frame
                else list(page.frames)
            )
            text = "\n".join(
                f.evaluate("() => document.body ? document.body.innerText : ''")
                for f in frames
                if f is not None
            )
            present = value in text
            return present if cond.kind == "text_visible" else not present
        except Exception:
            return False

    def find_value_context(self, value: str) -> dict:
        """Locate a value on screen and describe how to re-find it robustly.

        Used by the distiller to turn 'the model read $4,982.17' into a
        semantic extraction target. Prefers table addressing (row label x
        column header) because that is what survives cosmetic churn in
        legacy table-soup layouts.
        """
        for frame in self._require_page().frames:
            try:
                found = frame.evaluate(_FIND_VALUE_JS, value)
            except Exception:
                continue
            if found and found.get("kind") != "none":
                found["frame"] = self._frame_name(frame)
                return found
        return {"kind": "none"}

    # -- internals -----------------------------------------------------------

    def _render_value(self, template: str) -> tuple[str, str]:
        """Resolve '{{param:x}}' / '{{secret:y}}' templates.

        Returns (real value, loggable value): the loggable form keeps secret
        placeholders intact so a secret can never reach a transcript.
        """
        real, safe = template, template

        def _sub(m: re.Match[str]) -> str:
            kind, name = m.group(1), m.group(2)
            if kind == "param":
                if name not in self._params:
                    raise KeyError(f"no value bound for param '{name}'")
                return self._params[name]
            return self.policy.resolve_secret(name)

        real = TEMPLATE_RE.sub(_sub, real)
        safe = TEMPLATE_RE.sub(
            lambda m: self._params.get(m.group(2), m.group(0)) if m.group(1) == "param" else m.group(0),
            safe,
        )
        return real, safe

    def _frame_name(self, frame: Frame) -> str:
        return frame.name if frame.name else MAIN_FRAME

    def _frame(self, name: str, required: bool = True) -> Frame | None:
        page = self._require_page()
        if name in (MAIN_FRAME, None):
            return page.main_frame
        for frame in page.frames:
            if frame.name == name:
                return frame
        if required:
            raise TargetNotFound(f"frame '{name}' not present")
        return None

    def _require_page(self):
        if self._page is None:
            raise RuntimeError("surface not started; call start() first")
        return self._page


class PolicyViolationNavigation(Exception):
    """An action carried the session outside the allowlist; the run must stop."""
