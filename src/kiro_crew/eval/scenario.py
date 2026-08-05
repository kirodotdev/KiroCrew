"""Scenario model for multi-session evaluation.

A scenario defines a sequence of sessions, each with turns (user messages)
and assertions on agent responses. Optionally seeds a user profile before
the first session.
"""

from __future__ import annotations

import json
import re
from dataclasses import dataclass, field
from enum import Enum
from pathlib import Path
from typing import Any


class AssertionType(Enum):
    CONTAINS = "contains"
    NOT_CONTAINS = "not_contains"
    REGEX = "regex"
    EQUALS = "equals"
    JUDGE = "judge"


@dataclass
class Assertion:
    """A single assertion on an agent response."""

    type: AssertionType
    value: str
    case_sensitive: bool = False

    def check(self, response: str) -> bool:
        if self.type == AssertionType.JUDGE:
            return True  # handled separately by LLMJudge
        target = response if self.case_sensitive else response.lower()
        value = self.value if self.case_sensitive else self.value.lower()
        if self.type == AssertionType.CONTAINS:
            return value in target
        if self.type == AssertionType.NOT_CONTAINS:
            return value not in target
        if self.type == AssertionType.REGEX:
            flags = 0 if self.case_sensitive else re.IGNORECASE
            return bool(re.search(self.value, response, flags))
        if self.type == AssertionType.EQUALS:
            return target.strip() == value.strip()
        return False


@dataclass
class Turn:
    """A single user→agent exchange."""

    user: str
    assertions: list[Assertion] = field(default_factory=list)


@dataclass
class Session:
    """A session within a scenario — a sequence of turns."""

    name: str
    turns: list[Turn] = field(default_factory=list)


@dataclass
class SeedProfile:
    """Initial user profile to seed before session 1."""

    preferences: str = ""
    projects: str = ""
    lessons: list[str] = field(default_factory=list)


@dataclass
class Scenario:
    """A complete multi-session evaluation scenario."""

    name: str
    description: str = ""
    dimensions: list[str] = field(default_factory=list)
    seed: SeedProfile | None = None
    sessions: list[Session] = field(default_factory=list)
    judge_criteria: str = ""


def _load_yaml(text: str) -> Any:
    try:
        import yaml as _yaml  # type: ignore[import-untyped]
    except ImportError as exc:
        raise ImportError(
            "PyYAML is required for YAML scenarios: pip install PyYAML"
        ) from exc
    return _yaml.safe_load(text)


def load_scenario(path: str | Path) -> Scenario:
    """Load a scenario from a YAML or JSON file."""
    p = Path(path)
    text = p.read_text(encoding="utf-8")
    if p.suffix in (".yaml", ".yml"):
        data = _load_yaml(text)
    else:
        data = json.loads(text)
    return _parse_scenario(data)


def load_scenarios(path: str | Path) -> list[Scenario]:
    """Load all scenarios from a directory or single file."""
    p = Path(path)
    if p.is_file():
        return [load_scenario(p)]
    return [
        load_scenario(f)
        for f in sorted(p.iterdir())
        if f.suffix in (".yaml", ".yml", ".json")
    ]


def _parse_scenario(data: dict[str, Any]) -> Scenario:
    # Parse seed profile
    seed = None
    if "seed" in data:
        sd = data["seed"]
        seed = SeedProfile(
            preferences=sd.get("preferences", ""),
            projects=sd.get("projects", ""),
            lessons=sd.get("lessons", []),
        )

    # Parse sessions
    sessions = []
    for sess_data in data.get("sessions", []):
        turns = []
        for td in sess_data.get("turns", []):
            assertions = [
                Assertion(
                    type=AssertionType(a["type"]),
                    value=a.get("value", ""),
                    case_sensitive=a.get("case_sensitive", False),
                )
                for a in td.get("assertions", [])
            ]
            turns.append(Turn(user=td["user"], assertions=assertions))
        sessions.append(Session(name=sess_data["name"], turns=turns))

    return Scenario(
        name=data["name"],
        description=data.get("description", ""),
        dimensions=data.get("dimensions", []),
        seed=seed,
        sessions=sessions,
        judge_criteria=data.get("judge_criteria", ""),
    )
