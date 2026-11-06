"""Workload format: one JSON case per line."""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from pathlib import Path

from .ledger import canonical, sha256

GRADERS = {"exact", "contains", "regex", "number", "json_fields", "python_tests", "jury"}
DEFAULT_PARAMS = {"temperature": 0, "max_tokens": 1024}


@dataclass
class Case:
    id: str
    segment: str
    messages: list[dict]
    grader: dict
    params: dict = field(default_factory=dict)
    weight: float = 1.0
    recorded: dict | None = None

    @property
    def request_params(self) -> dict:
        return {**DEFAULT_PARAMS, **self.params}


def _parse(line: str, number: int) -> Case:
    raw = json.loads(line)
    case = Case(
        id=str(raw.get("id", f"case-{number}")),
        segment=str(raw.get("segment", "default")),
        messages=raw["messages"],
        grader=raw.get("grader") or {"type": "jury"},
        params=raw.get("params") or {},
        weight=float(raw.get("weight", 1.0)),
        recorded=raw.get("recorded"),
    )
    if case.grader.get("type") not in GRADERS:
        raise ValueError(f"line {number}: unknown grader {case.grader.get('type')!r}")
    if not case.messages:
        raise ValueError(f"line {number}: case has no messages")
    if case.weight <= 0:
        raise ValueError(f"line {number}: weight must be positive")
    return case


def load(path: Path) -> tuple[list[Case], str]:
    """Return the cases and a digest that pins exactly what was tested."""
    text = path.read_text(encoding="utf-8")
    cases = [
        _parse(line, number)
        for number, line in enumerate(text.splitlines(), start=1)
        if line.strip()
    ]
    if not cases:
        raise ValueError(f"{path} has no cases")
    ids = [case.id for case in cases]
    if len(set(ids)) != len(ids):
        raise ValueError(f"{path} has duplicate case ids")
    return cases, sha256(text)


def prompt_digest(messages: list[dict]) -> str:
    return sha256(canonical(messages))
