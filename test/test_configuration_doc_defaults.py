"""Every default ``configuration.md`` prints is pinned to the dataclass it describes.

``scripts/docs_lint.py`` gates a doc's SYMBOLS and PATHS, so it already catches a
config key that is absent from the code. What it cannot see is a key that is
present while the VALUE beside it has moved: ``stt.language_code`` sat in this page
as ``"en-US"`` after the field default became ``STT_LANGUAGE_AUTO``, with every
symbol resolving and the lint green. The page's whole job is to be the reference
for "what does this default to", so that failure mode is the one worth a gate.

Two shapes state a default here and both are covered, because the drift landed in
both at once:

* the ``| `section.key` | description | `default` |`` rows of the per-section tables;
* the ``Key Settings`` JSON example, whose values a reader reasonably reads as
  defaults.

The JSON half is scoped to that ONE section rather than to every fenced ``json``
block on the page. Elsewhere a block is illustrative -- "set this to enable X" shows
a non-default on purpose -- so pinning every block to a default would red this gate
on a legitimate example and teach the next author to delete the test rather than the
example. ``Key Settings`` is the block that claims to be defaults, so it is the block
held to them.

The assertion is against ``dataclasses.fields`` rather than a transcribed list, so a
field renamed or re-defaulted in ``config/sections.py`` fails here instead of being
mirrored into a second copy that can drift on its own. A row naming a key the
dataclass does not have fails too — that is the same defect one step earlier.

Rows are DISCOVERED, never enumerated: a new section added to the page is covered
the day it lands, and a section whose dataclass this module does not know about is
reported rather than skipped silently, so the map cannot quietly stop covering the
page.
"""

from __future__ import annotations

import dataclasses
import json
import re
from pathlib import Path

import pytest

from kiro_crew.config import sections as S

DOC = Path(__file__).parent.parent / "src" / "kiro_crew" / "docs" / "configuration.md"

#: Doc section prefix -> the dataclass that owns those keys. Every prefix the page
#: uses in a default-bearing row must be here; ``test_every_documented_section_is_mapped``
#: is what makes an unmapped one a failure rather than an untested row.
SECTION_CLASS: dict[str, str] = {
    "agent": "AgentConfig",
    "session": "SessionConfig",
    "task_runner": "TaskRunnerConfig",
    "orchestrator": "OrchestratorConfig",
    "messaging": "MessagingConfig",
    "cron_history": "CronHistoryConfig",
    "memory": "MemoryConfig",
    "knowledge": "KnowledgeConfig",
    "slack": "SlackConfig",
    "publish": "PublishConfig",
    "tailscale": "TailscaleConfig",
    "dashboard": "DashboardConfig",
    "workspace": "WorkspaceConfig",
    "skills": "SkillsConfig",
    "session_summary": "SessionSummaryConfig",
    "telemetry": "TelemetryConfig",
    "stt": "SttConfig",
    "computer_use": "ComputerUseConfig",
    "mcp_gateway": "McpGatewayConfig",
    "mcp": "McpConfig",
    "instances": "InstancesConfig",
    "decisions": "DecisionsConfig",
}

#: ``| `sec.key` | … | `default` |`` — the shape every per-section table row uses.
_ROW = re.compile(r"^\|\s*`([a-z_]+)\.([a-z0-9_]+)`\s*\|(?P<desc>.*)\|\s*(?P<default>.*?)\s*\|\s*$")

#: The leading `backticked` literal of a default cell. The cell may carry a trailing
#: gloss -- ``` `3600` (1h) ``` -- which is prose about the value, not the value.
_CELL_LITERAL = re.compile(r"^`([^`]*)`")


@pytest.fixture(scope="module")
def doc_text() -> str:
    return DOC.read_text(encoding="utf-8")


def _field_defaults(cls_name: str) -> dict[str, object]:
    """``{field name: default}`` for one config dataclass, factories resolved."""
    cls = getattr(S, cls_name)
    out: dict[str, object] = {}
    for field in dataclasses.fields(cls):
        if field.default is not dataclasses.MISSING:
            out[field.name] = field.default
        elif field.default_factory is not dataclasses.MISSING:  # type: ignore[misc]
            out[field.name] = field.default_factory()  # type: ignore[misc]
    return out


def _rendered(value: object) -> str:
    """How the page spells *value* in a backticked cell."""
    if isinstance(value, bool):
        return "true" if value else "false"
    if isinstance(value, str):
        return f'"{value}"'
    return str(value)


def _agrees(shown: str, actual: object) -> bool:
    """Whether a doc cell's literal states *actual*.

    Three spellings are all correct and must all pass:

    * exact -- a string default is quoted verbatim in the cell;
    * numeric -- ``10.0`` and ``10`` agree, since the page picks whichever reads
      better and neither is wrong;
    * JSON -- a list or dict default is written the way a reader would put it in
      ``config.json`` (``["markdown", "text"]``), not the way Python repr()s it
      (``['markdown', 'text']``). Comparing those as text would fail on quote style
      alone, so containers are compared structurally.
    """
    want = _rendered(actual)
    if shown == want:
        return True
    if isinstance(actual, (list, dict)):
        try:
            return json.loads(shown) == actual
        except json.JSONDecodeError:
            return False
    try:
        return float(shown) == float(want)
    except ValueError:
        return False


def _table_rows(text: str) -> list[tuple[int, str, str, str]]:
    """``(line number, section, key, default literal)`` for every default-bearing row."""
    rows: list[tuple[int, str, str, str]] = []
    for lineno, line in enumerate(text.splitlines(), 1):
        match = _ROW.match(line)
        if not match:
            continue
        section, key = match.group(1), match.group(2)
        literal = _CELL_LITERAL.match(match.group("default").strip())
        if literal is None:
            # A cell with no backticked value states no default to pin.
            continue
        rows.append((lineno, section, key, literal.group(1)))
    return rows


def test_the_page_still_has_default_tables(doc_text: str) -> None:
    """A parser that silently matches nothing would make every test below vacuous."""
    rows = _table_rows(doc_text)
    assert len(rows) > 80, f"only {len(rows)} default rows parsed; the row shape likely changed"


def test_every_documented_section_is_mapped(doc_text: str) -> None:
    """A section this module cannot resolve is reported, not skipped."""
    unmapped = sorted({section for _, section, _, _ in _table_rows(doc_text)} - set(SECTION_CLASS))
    assert not unmapped, (
        f"configuration.md documents section(s) {unmapped} that this test cannot "
        "resolve to a config dataclass -- add them to SECTION_CLASS so their "
        "defaults are pinned too"
    )


def test_every_documented_key_exists_on_its_dataclass(doc_text: str) -> None:
    """A row naming a field the dataclass lacks is drift one step before the value."""
    missing: list[str] = []
    for lineno, section, key, _shown in _table_rows(doc_text):
        cls_name = SECTION_CLASS.get(section)
        if cls_name is None:
            continue  # reported by test_every_documented_section_is_mapped
        if key not in _field_defaults(cls_name):
            missing.append(f"configuration.md:{lineno} `{section}.{key}` is not a {cls_name} field")
    assert not missing, "\n".join(missing)


def test_every_documented_default_matches_the_dataclass(doc_text: str) -> None:
    """The regression this module exists for: a value that moved in code only.

    ``stt.language_code`` is the worked example -- the field became
    ``STT_LANGUAGE_AUTO`` while the page still read ``"en-US"``.
    """
    wrong: list[str] = []
    for lineno, section, key, shown in _table_rows(doc_text):
        cls_name = SECTION_CLASS.get(section)
        if cls_name is None:
            continue
        defaults = _field_defaults(cls_name)
        if key not in defaults:
            continue  # reported by test_every_documented_key_exists_on_its_dataclass
        actual = defaults[key]
        if not _agrees(shown, actual):
            wrong.append(
                f"configuration.md:{lineno} `{section}.{key}` documents "
                f"`{shown}` but {cls_name} defaults to `{_rendered(actual)}`"
            )
    assert not wrong, "\n".join(wrong)


def _key_settings_blocks(text: str) -> list[str]:
    """The fenced ``json`` blocks inside the ``Key Settings`` section only.

    Scoped deliberately. A block under another heading is illustrative -- it shows a
    non-default to demonstrate an option -- and holding those to defaults would fail
    this gate on a correct example.
    """
    start = text.find("\n## Key Settings")
    if start == -1:
        return []
    rest = text[start + 1 :]
    end = rest.find("\n## ", 1)
    section = rest if end == -1 else rest[:end]
    return re.findall(r"```json\n(.*?)```", section, re.DOTALL)


def test_the_key_settings_example_agrees_with_the_dataclasses(doc_text: str) -> None:
    """A reader takes the ``Key Settings`` block for defaults, so it is held to them.

    Only parseable objects are read: a fenced block that is a fragment states no
    complete claim. Keys whose section this module does not map are left to
    ``test_every_documented_section_is_mapped``.
    """
    blocks = _key_settings_blocks(doc_text)
    assert blocks, "no json block found under '## Key Settings'; the heading likely moved"
    wrong: list[str] = []
    examined = 0
    for block in blocks:
        try:
            data = json.loads(block)
        except json.JSONDecodeError:
            continue
        if not isinstance(data, dict):
            continue
        for section, body in data.items():
            cls_name = SECTION_CLASS.get(section)
            if cls_name is None or not isinstance(body, dict):
                continue
            defaults = _field_defaults(cls_name)
            for key, shown in body.items():
                if key not in defaults:
                    wrong.append(f"`{section}.{key}` in a JSON example is not a {cls_name} field")
                    continue
                examined += 1
                actual = defaults[key]
                if isinstance(shown, (int, float)) and isinstance(actual, (int, float)):
                    if float(shown) == float(actual):
                        continue
                elif shown == actual:
                    continue
                wrong.append(
                    f"`{section}.{key}` in a JSON example shows {shown!r} "
                    f"but {cls_name} defaults to {actual!r}"
                )
    assert (
        examined > 30
    ), f"only {examined} Key Settings values checked; the block shape likely changed"
    assert not wrong, "\n".join(wrong)
