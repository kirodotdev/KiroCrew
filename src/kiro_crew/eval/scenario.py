"""Scenario model for multi-session evaluation.

A scenario defines a sequence of sessions, each with turns (user messages)
and assertions on agent responses. Optionally seeds a user profile before
the first session.
"""

from __future__ import annotations

import json
import re
from collections.abc import Sequence
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
    TOOL_CALLED = "tool_called"
    TOOL_NOT_CALLED = "tool_not_called"
    TOOL_ORDER = "tool_order"


#: The assertions that read the calls a turn made rather than its reply text.
TOOL_ASSERTION_TYPES = frozenset(
    {AssertionType.TOOL_CALLED, AssertionType.TOOL_NOT_CALLED, AssertionType.TOOL_ORDER}
)


@dataclass
class ToolCall:
    """One tool call a turn made: its name, its MCP server (empty for a built-in) and arguments."""

    name: str
    server: str = ""
    args: dict[str, Any] = field(default_factory=dict)

    def is_named(self, expected: str, case_sensitive: bool) -> bool:
        """Whether *expected* is this call's tool name, bare or as ``server/tool`` or ``@server/tool``."""
        labels = [self.name]
        if self.server:
            labels += [f"{self.server}/{self.name}", f"@{self.server}/{self.name}"]
        if case_sensitive:
            return expected in labels
        return expected.casefold() in {label.casefold() for label in labels}

    def has_args(self, expected: dict[str, Any], case_sensitive: bool) -> bool:
        """Whether every key of *expected* is an argument of this call with a matching value.

        A string matches as a fragment of a string argument (as ``contains`` does for a
        reply); any other value must equal the argument.
        """
        for key, want in expected.items():
            if key not in self.args:
                return False
            have = self.args[key]
            if isinstance(want, str):
                if not isinstance(have, str):
                    return False
                if (want in have) if case_sensitive else (want.lower() in have.lower()):
                    continue
                return False
            if have != want:
                return False
        return True


@dataclass
class Assertion:
    """A single assertion on an agent response, or on the tools a turn called."""

    type: AssertionType
    value: str
    case_sensitive: bool = False
    #: ``tool_called`` / ``tool_not_called``: arguments the call must carry.
    args: dict[str, Any] = field(default_factory=dict)
    #: ``tool_order``: the tool names that must be called, in this order.
    tools: tuple[str, ...] = ()
    #: ``tool_order``: no other call may appear between or around them.
    exact: bool = False

    @property
    def label(self) -> str:
        """What a report prints beside the type."""
        return " > ".join(self.tools) if self.type == AssertionType.TOOL_ORDER else self.value

    def _check_tools(self, calls: Sequence[ToolCall]) -> bool:
        cs = self.case_sensitive
        if self.type == AssertionType.TOOL_ORDER:
            if self.exact:
                return len(calls) == len(self.tools) and all(
                    c.is_named(t, cs) for c, t in zip(calls, self.tools)
                )
            remaining = iter(calls)
            # ``any`` stops at the first match, so each step resumes after the previous one.
            return all(any(c.is_named(t, cs) for c in remaining) for t in self.tools)
        found = any(c.is_named(self.value, cs) and c.has_args(self.args, cs) for c in calls)
        return found if self.type == AssertionType.TOOL_CALLED else not found

    def check(self, response: str, tool_calls: Sequence[ToolCall] = ()) -> bool:
        if self.type in TOOL_ASSERTION_TYPES:
            return self._check_tools(tool_calls)
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


#: The file suffixes a scenario is read from.
SCENARIO_SUFFIXES = (".yaml", ".yml", ".json")


def parse_scenario_text(text: str, suffix: str) -> Scenario:
    """A scenario from the text of a YAML or JSON file whose name ends in *suffix*."""
    data = _load_yaml(text) if suffix in (".yaml", ".yml") else json.loads(text)
    return _parse_scenario(data)


def load_scenario(path: str | Path) -> Scenario:
    """Load a scenario from a YAML or JSON file."""
    p = Path(path)
    return parse_scenario_text(p.read_text(encoding="utf-8"), p.suffix)


def scenario_files(path: str | Path) -> list[Path]:
    """*path* itself when it is a file, else the scenario files directly in it, sorted."""
    p = Path(path)
    if p.is_file():
        return [p]
    return [f for f in sorted(p.iterdir()) if f.suffix in SCENARIO_SUFFIXES]


def load_scenarios(path: str | Path) -> list[Scenario]:
    """Load all scenarios from a directory or single file."""
    return [load_scenario(f) for f in scenario_files(path)]


def _parse_assertion(a: dict[str, Any]) -> Assertion:
    """One assertion from its scenario-file mapping.

    A tool assertion that could not be checked (no tool name, a malformed ``args`` or
    ``tools``) is refused by name rather than loaded as one that passes or fails by
    accident. The extra keys mean nothing on any other type.
    """
    kind = AssertionType(a["type"])
    assertion = Assertion(
        type=kind,
        value=a.get("value", ""),
        case_sensitive=a.get("case_sensitive", False),
    )
    if kind not in TOOL_ASSERTION_TYPES:
        return assertion
    if kind == AssertionType.TOOL_ORDER:
        tools = a.get("tools")
        if (
            not isinstance(tools, list)
            or not tools
            or not all(isinstance(t, str) and t for t in tools)
        ):
            raise ValueError(f"{kind.value} needs 'tools': a list of tool names")
        exact = a.get("exact", False)
        if not isinstance(exact, bool):
            raise ValueError(f"{kind.value} 'exact' must be true or false")
        assertion.tools = tuple(tools)
        assertion.exact = exact
        return assertion
    if not isinstance(assertion.value, str) or not assertion.value:
        raise ValueError(f"{kind.value} needs the tool's name as 'value'")
    args = a.get("args")
    if args is None:
        args = {}
    if not isinstance(args, dict):
        raise ValueError(f"{kind.value} 'args' must be a mapping of argument names to values")
    assertion.args = args
    return assertion


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
            assertions = [_parse_assertion(a) for a in td.get("assertions", [])]
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
