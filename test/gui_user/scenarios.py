"""Scenario DSL: one YAML file per scenario under ``test/gui_user/scenarios/``.

.. code-block:: yaml

    name: settings-theme-toggle          # slug, also the artifact sub-directory
    tier: smoke                          # smoke (PR + nightly) | nightly (nightly only)
    summary: one line for the report table
    preconditions:
      seed: rich                         # KIROCREW_HOME fixture the target boots from
      members: []                        # crew member slugs boot.sh adds to config.agents
      start_url: /settings               # path the browser opens (token is appended)
    steps:                               # what a human tester would be told, in order
      - Open Settings and find the appearance / theme control.
    expectations:                        # what must be TRUE on screen at the end
      - The page background colour visibly changed.
    max_steps: 12                        # model actions before the scenario FAILS
    max_seconds: 300                     # wall clock before the scenario FAILS

The harness turns ``steps`` + ``expectations`` into the task prompt and asks the
model for a ``VERDICT`` block; the workflow only ever sees the resulting
``summary.json``. Everything here is data: the loader validates shape and
bounds, it never interprets the natural language.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Iterable

import yaml

TIERS: tuple[str, ...] = ("smoke", "nightly")
_SLUG_RE = re.compile(r"^[a-z0-9](?:[a-z0-9-]{0,62}[a-z0-9])?$")

#: Hard ceilings, independent of what a scenario asks for. A scenario that
#: wants more is a design smell (split it), and the ceiling is what bounds the
#: run's spend when a model loops.
MAX_STEPS_CEILING = 40
MAX_SECONDS_CEILING = 900


class ScenarioError(ValueError):
    """A scenario file is malformed."""


@dataclass(frozen=True)
class Scenario:
    name: str
    tier: str
    summary: str
    steps: tuple[str, ...]
    expectations: tuple[str, ...]
    max_steps: int
    max_seconds: int
    seed: str = "rich"
    members: tuple[str, ...] = ()
    start_url: str = "/"
    path: Path | None = field(default=None, compare=False)

    def task_prompt(self) -> str:
        """The user-turn text handed to the model, built only from the YAML."""
        lines = [
            f"TASK: {self.summary}",
            "",
            "Do these steps in order, like a person at the keyboard:",
        ]
        lines += [f"{i}. {s}" for i, s in enumerate(self.steps, 1)]
        lines += ["", "When you are done, ALL of these must be true and visible on screen:"]
        lines += [f"- {e}" for e in self.expectations]
        lines += [
            "",
            f"You have at most {self.max_steps} actions and about {self.max_seconds // 60} minutes.",
            "Verify each expectation against the LAST screenshot before you answer.",
        ]
        return "\n".join(lines)


def _str_list(value: Any, what: str, path: Path) -> tuple[str, ...]:
    if not isinstance(value, list) or not value:
        raise ScenarioError(f"{path}: '{what}' must be a non-empty list")
    out: list[str] = []
    for item in value:
        if not isinstance(item, str) or not item.strip():
            raise ScenarioError(f"{path}: every '{what}' entry must be a non-empty string")
        if len(item) > 400:
            raise ScenarioError(f"{path}: '{what}' entry longer than 400 chars")
        out.append(item.strip())
    return tuple(out)


def _bounded_int(value: Any, what: str, ceiling: int, path: Path) -> int:
    if not isinstance(value, int) or isinstance(value, bool):
        raise ScenarioError(f"{path}: '{what}' must be an integer")
    if value < 1 or value > ceiling:
        raise ScenarioError(f"{path}: '{what}' must be between 1 and {ceiling}")
    return value


def parse_scenario(doc: Any, path: Path) -> Scenario:
    if not isinstance(doc, dict):
        raise ScenarioError(f"{path}: top level must be a mapping")
    unknown = set(doc) - {
        "name",
        "tier",
        "summary",
        "preconditions",
        "steps",
        "expectations",
        "max_steps",
        "max_seconds",
    }
    if unknown:
        raise ScenarioError(f"{path}: unknown keys {sorted(unknown)}")

    name = doc.get("name")
    if not isinstance(name, str) or not _SLUG_RE.match(name):
        raise ScenarioError(f"{path}: 'name' must be a lowercase slug")
    if name != path.stem:
        raise ScenarioError(f"{path}: 'name' ({name}) must equal the file stem ({path.stem})")

    tier = doc.get("tier", "nightly")
    if tier not in TIERS:
        raise ScenarioError(f"{path}: 'tier' must be one of {TIERS}")

    summary = doc.get("summary")
    if not isinstance(summary, str) or not summary.strip() or len(summary) > 200:
        raise ScenarioError(f"{path}: 'summary' must be a short non-empty string")

    pre = doc.get("preconditions") or {}
    if not isinstance(pre, dict):
        raise ScenarioError(f"{path}: 'preconditions' must be a mapping")
    unknown_pre = set(pre) - {"seed", "members", "start_url"}
    if unknown_pre:
        raise ScenarioError(f"{path}: unknown preconditions {sorted(unknown_pre)}")
    seed = pre.get("seed", "rich")
    if not isinstance(seed, str) or not _SLUG_RE.match(seed):
        raise ScenarioError(f"{path}: preconditions.seed must be a fixture slug")
    members_raw = pre.get("members", [])
    if not isinstance(members_raw, list):
        raise ScenarioError(f"{path}: preconditions.members must be a list")
    members: list[str] = []
    for m in members_raw:
        if not isinstance(m, str) or not _SLUG_RE.match(m):
            raise ScenarioError(f"{path}: preconditions.members entries must be slugs")
        members.append(m)
    start_url = pre.get("start_url", "/")
    if not isinstance(start_url, str) or not start_url.startswith("/") or "?" in start_url:
        raise ScenarioError(
            f"{path}: preconditions.start_url must be an absolute path without a query"
        )

    return Scenario(
        name=name,
        tier=tier,
        summary=summary.strip(),
        steps=_str_list(doc.get("steps"), "steps", path),
        expectations=_str_list(doc.get("expectations"), "expectations", path),
        max_steps=_bounded_int(doc.get("max_steps", 15), "max_steps", MAX_STEPS_CEILING, path),
        max_seconds=_bounded_int(
            doc.get("max_seconds", 300), "max_seconds", MAX_SECONDS_CEILING, path
        ),
        seed=seed,
        members=tuple(members),
        start_url=start_url,
        path=path,
    )


def load_scenario(path: Path) -> Scenario:
    try:
        doc = yaml.safe_load(path.read_text(encoding="utf-8"))
    except yaml.YAMLError as exc:
        raise ScenarioError(f"{path}: invalid YAML: {exc}") from exc
    return parse_scenario(doc, path)


def load_all(directory: Path) -> list[Scenario]:
    files = sorted(directory.glob("*.yaml"))
    if not files:
        raise ScenarioError(f"no *.yaml scenarios under {directory}")
    return [load_scenario(p) for p in files]


def select(
    scenarios: Iterable[Scenario], *, tier: str = "all", names: Iterable[str] = ()
) -> list[Scenario]:
    """Filter by tier (``smoke`` | ``nightly`` | ``all``) and/or explicit names.

    ``nightly`` includes the smoke tier (nightly is the full run); ``smoke`` is
    the PR subset. An explicit name list bypasses the tier filter.
    """
    wanted = set(names)
    out: list[Scenario] = []
    for s in scenarios:
        if wanted:
            if s.name in wanted:
                out.append(s)
            continue
        if tier == "all" or tier == "nightly" or s.tier == tier:
            out.append(s)
    missing = wanted - {s.name for s in out}
    if missing:
        raise ScenarioError(f"unknown scenario name(s): {sorted(missing)}")
    return out
