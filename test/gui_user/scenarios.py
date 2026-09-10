"""Scenario DSL: one YAML file per scenario under ``test/gui_user/scenarios/``.

.. code-block:: yaml

    name: settings-theme-toggle          # slug, also the artifact sub-directory
    tier: smoke                          # smoke (PR + nightly) | nightly (nightly only)
    feature: settings                    # product area, a key of FEATURES (groups the report)
    user_story: >-                       # one user-readable sentence: what the user wants and why
      As a user, I want to switch the dashboard theme so that it matches my desk.
    docs_url: docs/build/gui-user-test.md   # optional: where the feature is documented
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

``feature`` and ``user_story`` are not read by the harness at all: they exist so
the report can be read as "which product areas are healthy" and so the scenario
directory doubles as a catalog of what the product does (``report.py --format
features``). A scenario without them is rejected, because a catalog with
unclassified entries is not a catalog.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Iterable

import yaml

TIERS: tuple[str, ...] = ("smoke", "nightly")
_SLUG_RE = re.compile(r"^[a-z0-9](?:[a-z0-9-]{0,62}[a-z0-9])?$")

#: Product areas a scenario may claim, slug -> human title. A closed list rather
#: than a free-form slug so a typo cannot split one feature into two report
#: groups; adding an area is a one-line data change here plus a row in
#: ``docs/build/gui-user-test.md``. Order is the order the report groups appear in.
FEATURES: dict[str, str] = {
    "chat": "Chat sessions",
    "sidebar": "Sessions sidebar & folders",
    "members": "Crew Members",
    "settings": "Settings",
    "apps": "Apps & App Store",
    "schedule": "Schedule (cron jobs)",
    "knowledge": "Knowledge library",
    "artifacts": "Artifacts",
    "files": "File viewer & project files",
    "browser-panel": "Browser panel",
    "voice": "Voice",
    "notifications": "Notifications",
    "onboarding": "Onboarding & login",
    "search": "Search everywhere & command palette",
    "developer": "Developer tools",
}

#: A user story is one sentence a product manager could read aloud; the bound is
#: generous enough for "As a …, I want …, so that …" and tight enough to refuse a
#: pasted paragraph.
USER_STORY_MAX = 300

#: An ``https://`` URL, or a repo path under ``docs/`` ending in ``.md`` (optional
#: anchor). Path segments are letters, digits, ``.``, ``_`` and ``-`` and never
#: start with ``.``, so ``docs/../x.md`` and hidden files are refused.
_DOCS_URL_RE = re.compile(
    r"^(https://[^\s]+"
    r"|docs/(?:[A-Za-z0-9_-][A-Za-z0-9._-]*/)*[A-Za-z0-9_-][A-Za-z0-9._-]*\.md"
    r"(#[A-Za-z0-9._-]+)?)$"
)

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
    feature: str
    user_story: str
    summary: str
    steps: tuple[str, ...]
    expectations: tuple[str, ...]
    max_steps: int
    max_seconds: int
    docs_url: str = ""
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
        "feature",
        "user_story",
        "docs_url",
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

    feature = doc.get("feature")
    if not isinstance(feature, str) or feature not in FEATURES:
        raise ScenarioError(
            f"{path}: 'feature' is required and must be one of {sorted(FEATURES)} "
            "(add a new product area to scenarios.FEATURES first)"
        )

    user_story = doc.get("user_story")
    if (
        not isinstance(user_story, str)
        or not user_story.strip()
        or len(user_story.strip()) > USER_STORY_MAX
    ):
        raise ScenarioError(
            f"{path}: 'user_story' is required: one sentence of at most {USER_STORY_MAX} chars "
            "('As a …, I want …, so that …' or a use case)"
        )

    docs_url = doc.get("docs_url", "")
    if not isinstance(docs_url, str) or (docs_url and not _DOCS_URL_RE.match(docs_url)):
        raise ScenarioError(
            f"{path}: 'docs_url' must be an https:// URL or a repo path under docs/ ending in .md"
        )

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
        feature=feature,
        user_story=user_story.strip(),
        summary=summary.strip(),
        steps=_str_list(doc.get("steps"), "steps", path),
        expectations=_str_list(doc.get("expectations"), "expectations", path),
        max_steps=_bounded_int(doc.get("max_steps", 15), "max_steps", MAX_STEPS_CEILING, path),
        max_seconds=_bounded_int(
            doc.get("max_seconds", 300), "max_seconds", MAX_SECONDS_CEILING, path
        ),
        docs_url=docs_url,
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


def by_feature(scenarios: Iterable[Scenario]) -> dict[str, list[Scenario]]:
    """Group scenarios by ``feature`` in :data:`FEATURES` order (empty groups omitted)."""
    groups: dict[str, list[Scenario]] = {slug: [] for slug in FEATURES}
    for s in scenarios:
        groups[s.feature].append(s)
    return {slug: group for slug, group in groups.items() if group}
