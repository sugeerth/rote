"""Guardrail policy: allowlist, action permissions, risk classes, redaction.

One object, one choke point. The Surface consults the policy before *every*
action - during LLM discovery and during deterministic replay alike - so
there is no code path where the agent can act outside the allowlist.

Secrets are resolved from the environment at act-time and simultaneously
registered with the redactor, so even an accidental echo of a secret value
in a snapshot or log is scrubbed before it reaches disk.
"""

from __future__ import annotations

import os
import re
from dataclasses import dataclass, field
from pathlib import Path
from urllib.parse import urlparse

import yaml


class PolicyViolation(Exception):
    """Raised when an action would leave the allowlist. Always fatal."""


@dataclass(frozen=True)
class PolicyDecision:
    allowed: bool
    risk: str  # "safe" | "irreversible"
    reason: str


@dataclass
class Redactor:
    """Scrubs regulated data from anything headed for disk or a transcript."""

    patterns: list[tuple[str, re.Pattern[str], str]] = field(default_factory=list)
    _secret_values: set[str] = field(default_factory=set)

    def register_secret(self, value: str) -> None:
        if value:
            self._secret_values.add(value)

    def redact(self, text: str) -> str:
        for _name, pattern, replacement in self.patterns:
            text = pattern.sub(replacement, text)
        for value in self._secret_values:
            text = text.replace(value, "[REDACTED]")
        return text

    def js_mask_rules(self) -> list[list[str]]:
        """The same redaction rules as [source, flags, replacement] triples a
        browser can apply to on-screen text before a screenshot is captured,
        so pixels get the same treatment as persisted text."""
        rules: list[list[str]] = []
        for _name, pattern, replacement in self.patterns:
            src, flags = pattern.pattern, "g"
            if src.startswith("(?i)"):
                src, flags = src[4:], "gi"
            rules.append([src, flags, replacement.replace("\\1", "$1")])
        for value in self._secret_values:
            rules.append([re.escape(value), "g", "[REDACTED]"])
        return rules


class Policy:
    def __init__(self, config: dict):
        allow = config.get("allowlist", {})
        self.origins: list[str] = [o.rstrip("/") for o in allow.get("origins", [])]
        self.denied_paths: list[str] = allow.get("denied_paths", [])
        self.allowed_actions: set[str] = set(config.get("actions", {}).get("allowed", []))
        risk = config.get("risk", {})
        self.irreversible_name = re.compile(risk.get("irreversible_name_pattern", r"$^"))
        self.unattended_irreversible: str = risk.get("unattended_irreversible", "escalate")
        self.secrets: dict[str, str] = {
            name: spec["env"] for name, spec in (config.get("secrets") or {}).items()
        }
        self.redactor = Redactor(
            patterns=[
                (p["name"], re.compile(p["regex"]), p.get("replacement", "[REDACTED]"))
                for p in (config.get("redaction", {}).get("patterns") or [])
            ]
        )

    @classmethod
    def from_file(cls, path: str | Path) -> "Policy":
        return cls(yaml.safe_load(Path(path).read_text()))

    # -- allowlist ----------------------------------------------------------

    def check_url(self, url: str) -> PolicyDecision:
        parsed = urlparse(url)
        origin = f"{parsed.scheme}://{parsed.netloc}"
        if origin not in self.origins:
            return PolicyDecision(False, "safe", f"origin {origin} is not on the allowlist")
        for prefix in self.denied_paths:
            if parsed.path.startswith(prefix):
                return PolicyDecision(False, "safe", f"path {parsed.path} matches denied prefix {prefix}")
        return PolicyDecision(True, "safe", "url allowed")

    def check_action(self, kind: str, current_url: str, control_name: str | None = None) -> PolicyDecision:
        if kind not in self.allowed_actions:
            return PolicyDecision(False, "safe", f"action type '{kind}' is not permitted")
        url_decision = self.check_url(current_url)
        if not url_decision.allowed:
            return PolicyDecision(False, "safe", url_decision.reason)
        risk = "safe"
        # press (Enter submits forms) and select can commit state just like a
        # click; classify all three against the control-name policy.
        if kind in ("click", "press", "select") and control_name and self.irreversible_name.search(control_name):
            risk = "irreversible"
        return PolicyDecision(True, risk, "allowed")

    def enforce(self, kind: str, current_url: str, control_name: str | None = None) -> PolicyDecision:
        decision = self.check_action(kind, current_url, control_name)
        if not decision.allowed:
            raise PolicyViolation(decision.reason)
        return decision

    # -- secrets ------------------------------------------------------------

    def resolve_secret(self, name: str) -> str:
        env_var = self.secrets.get(name)
        if not env_var:
            raise PolicyViolation(f"secret '{name}' is not declared in policy")
        value = os.environ.get(env_var)
        if not value:
            raise PolicyViolation(f"secret '{name}' not available: set {env_var}")
        self.redactor.register_secret(value)
        return value

    def redact(self, text: str) -> str:
        return self.redactor.redact(text)
