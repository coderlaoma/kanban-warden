"""Secret and connection-string scanner used by the Kanban Warden plugin."""

from __future__ import annotations

import re
from collections.abc import Iterable, Sequence
from dataclasses import dataclass
from functools import lru_cache
from importlib import resources
from typing import Any

import yaml


@dataclass(frozen=True)
class Rule:
    """Compiled detection rule."""

    id: str
    severity: str
    description: str
    pattern: re.Pattern[str]


@dataclass(frozen=True)
class ScanFinding:
    """A redacted finding produced by :class:`SecretScanner`."""

    rule_id: str
    severity: str
    description: str
    line: int
    column: int
    snippet: str


class SecretScanner:
    """Detect likely secrets in free-form text without returning raw secret values."""

    def __init__(self, rules: Sequence[Rule], allowlist: Iterable[str] = ()) -> None:
        self._rules = tuple(rules)
        self._allowlist = tuple(item.lower() for item in allowlist if item)

    def scan(self, text: str, *, max_findings: int = 20) -> list[ScanFinding]:
        if not text:
            return []
        findings: list[ScanFinding] = []
        for rule in self._rules:
            for match in rule.pattern.finditer(text):
                raw = match.group(0)
                if self._is_allowed(raw):
                    continue
                line, column = _line_column(text, match.start())
                findings.append(
                    ScanFinding(
                        rule_id=rule.id,
                        severity=rule.severity,
                        description=rule.description,
                        line=line,
                        column=column,
                        snippet=_redact(raw),
                    )
                )
                if len(findings) >= max_findings:
                    return findings
        return _dedupe_findings(findings)

    def _is_allowed(self, value: str) -> bool:
        lowered = value.lower()
        return any(item in lowered for item in self._allowlist)


def load_scanner_from_yaml(text: str) -> SecretScanner:
    """Build a scanner from a rules YAML document."""
    data = yaml.safe_load(text) or {}
    rules: list[Rule] = []
    for raw_rule in data.get("rules", []):
        pattern_text = str(raw_rule["pattern"])
        rules.append(
            Rule(
                id=str(raw_rule["id"]),
                severity=str(raw_rule.get("severity", "medium")),
                description=str(raw_rule.get("description", raw_rule["id"])),
                pattern=re.compile(pattern_text),
            )
        )
    return SecretScanner(rules, allowlist=data.get("allowlist", []))


@lru_cache(maxsize=1)
def default_scanner() -> SecretScanner:
    """Return the packaged scanner instance."""
    rules_text = resources.files(__package__).joinpath("rules.yaml").read_text(encoding="utf-8")
    return load_scanner_from_yaml(rules_text)


def build_warning_text(
    findings: Sequence[ScanFinding], *, task_id: str | None = None, tool_name: str | None = None
) -> str:
    """Render a concise warning safe to append to a tool result."""
    if not findings:
        return ""
    lines = [
        "[kanban-warden] WARNING: possible secret or unsafe connection detail detected ",
        f"in {tool_name or 'kanban tool'} output for task {task_id or 'unknown'}.",
        "Do not preserve raw credentials in durable Kanban comments/results. "
        "Replace with [REDACTED].",
        "Findings:",
    ]
    for finding in findings:
        lines.append(
            f"- {finding.severity.upper()} {finding.rule_id} at "
            f"line {finding.line}, column {finding.column}: {finding.snippet}"
        )
    return "\n".join(lines)


def _line_column(text: str, index: int) -> tuple[int, int]:
    line = text.count("\n", 0, index) + 1
    last_newline = text.rfind("\n", 0, index)
    column = index + 1 if last_newline == -1 else index - last_newline
    return line, column


def _redact(value: str) -> str:
    compact = " ".join(value.strip().split())
    if not compact:
        return "[REDACTED]"
    if len(compact) <= 12:
        return "[REDACTED]"
    return f"{compact[:4]}…[REDACTED]…{compact[-4:]}"


def _dedupe_findings(findings: Sequence[ScanFinding]) -> list[ScanFinding]:
    seen: set[tuple[Any, ...]] = set()
    deduped: list[ScanFinding] = []
    for finding in findings:
        key = (finding.line, finding.column, finding.snippet)
        if key in seen:
            continue
        seen.add(key)
        deduped.append(finding)
    return deduped
