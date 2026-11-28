# SPDX-License-Identifier: Apache-2.0
"""Every built-in skill's ``SKILL.md`` core comes back whole in one read.

KAS's ``read_file`` stops a read at 30,000 characters and 2,000 lines and ends it
with "hit the 30000-character limit. To continue, use offset N"; any tool result
of 30,000 characters or more is saved to a file and the model sees 1,000 of them.
A conductor that read a longer ``SKILL.md`` once stopped before its stop
conditions. So a core stays under 25,000 characters, detail lives in files under
the skill's ``references/``, and the core names each one with when to read it.

The four ways a core reaches a model, each checked here:

* Read File on the installed path: the size bound below.
* ``load_resource`` / ``skill::<name>`` on KAS: KAS loads the ``SKILL.md`` in the
  skills home, which the built-in sync fills with each packaged skill tree whole.
* ``skill_search`` ``action=read``: one page, under 30,000 characters, on KAS.
* After a compaction: the re-injection sends the conductor back to the skill,
  through one of the three paths above.
"""

from __future__ import annotations

import json
import re
import shutil
from pathlib import Path
from types import SimpleNamespace
from urllib.parse import urlencode

import pytest
from skill_script_helpers import load_skill_script

import kiro_crew
from kiro_crew.skills import SkillsLoader

ROOT = Path(__file__).resolve().parents[1]
BUILTIN = Path(kiro_crew.__file__).resolve().parent / "builtin_skills"
CORES = sorted(BUILTIN.rglob("SKILL.md"))
CORE_IDS = [p.parent.relative_to(BUILTIN).as_posix() for p in CORES]

#: The bound a core is held to, in characters: a margin under KAS's read cut.
CORE_CHAR_LIMIT = 25_000
#: Anthropic's guidance for a SKILL.md; a core past it is split further.
CORE_LINE_LIMIT = 500
#: KAS's ``read_file``: characters per read, and lines a read returns by default.
KAS_READ_CHARS = 30_000
KAS_READ_LINES = 2_000
#: A ``references/...`` path as a core names it.
_REF = re.compile(r"references/[A-Za-z0-9_./-]+\.md")


def _text(path: Path) -> str:
    return path.read_text(encoding="utf-8")


@pytest.mark.parametrize("core", CORES, ids=CORE_IDS)
def test_a_core_is_under_the_size_bound(core: Path) -> None:
    text = _text(core)
    assert len(text) < CORE_CHAR_LIMIT, (
        f"{core.relative_to(BUILTIN)} is {len(text):,} characters; move detail to "
        f"references/ and point at it from the core (limit {CORE_CHAR_LIMIT:,})"
    )
    assert text.count("\n") < CORE_LINE_LIMIT, f"{core.relative_to(BUILTIN)} is over 500 lines"


@pytest.mark.parametrize("core", CORES, ids=CORE_IDS)
def test_read_file_returns_a_core_in_one_read(core: Path) -> None:
    """The Read File path: one KAS read holds the whole file, so no offset notice."""
    text = _text(core)
    assert len(text) < KAS_READ_CHARS and text.count("\n") < KAS_READ_LINES


def _named_refs(core: Path) -> set[str]:
    return {m.rstrip(".") for m in _REF.findall(_text(core))}


@pytest.mark.parametrize("core", CORES, ids=CORE_IDS)
def test_every_reference_a_core_names_exists(core: Path) -> None:
    missing = sorted(r for r in _named_refs(core) if not (core.parent / r).is_file())
    assert not missing, f"{core.relative_to(BUILTIN)} names missing files: {missing}"


#: A reference a core must not name, and what names it instead.
_NAMED_ELSEWHERE = {
    # Remote dispatch is a preview: the crew tools' own descriptions name this file
    # only while it is on, so the core stays silent about it when it is off.
    "goal-conductor/references/remote-dispatch.md": "mcp_session_crew / mcp_crew_list",
}


@pytest.mark.parametrize("core", CORES, ids=CORE_IDS)
def test_every_reference_file_is_named_by_its_core(core: Path) -> None:
    refs_dir = core.parent / "references"
    if not refs_dir.is_dir():
        return
    named = _named_refs(core)
    present = {
        p.relative_to(core.parent).as_posix()
        for p in refs_dir.rglob("*.md")
        if p.relative_to(BUILTIN).as_posix() not in _NAMED_ELSEWHERE
    }
    orphans = sorted(present - named)
    assert not orphans, (
        f"{core.relative_to(BUILTIN)} never names {orphans}: a reference the core does "
        "not point at is never read"
    )


def test_load_resource_reads_the_installed_core_and_its_references(tmp_path: Path) -> None:
    """The skill:: path: the skills home KAS loads from holds each tree whole."""
    from kiro_crew.skills_builtin_sync import _ensure_builtin_skills

    _ensure_builtin_skills(tmp_path)
    for core in CORES:
        rel = core.parent.relative_to(BUILTIN)
        installed = tmp_path / rel
        assert (installed / "SKILL.md").read_bytes() == core.read_bytes(), rel
        for ref in (core.parent / "references").rglob("*.md"):
            copy = installed / ref.relative_to(core.parent)
            assert copy.read_bytes() == ref.read_bytes(), copy


@pytest.mark.asyncio
@pytest.mark.parametrize("provider", ["kas", "acp"])
async def test_skill_search_reads_a_core_whole_in_one_response(
    monkeypatch: pytest.MonkeyPatch, opened, provider: str, tmp_path: Path
) -> None:
    """The skill_search path: no page, and on KAS the response stays under its cut."""
    from aiohttp import web
    from aiohttp.test_utils import make_mocked_request
    from body_stream_helpers import BodyStreamPayload

    from kiro_crew.dashboard.handlers import prompts
    from kiro_crew.mcp_tools import skills as skill_tool

    # A loader writes its index and usage counts beside its skills root, so it
    # reads a copy, never the package tree.
    root = tmp_path / "skills"
    shutil.copytree(BUILTIN, root)
    loader = opened(SkillsLoader(skills_path=root, install_builtins=False))
    monkeypatch.setattr(prompts, "_read_session_key", lambda request: "dashboard:scoped")
    monkeypatch.setattr(prompts, "_deny_foreign_app_skill_slot", lambda *args, **kwargs: None)
    monkeypatch.setattr(prompts, "_get_skills", lambda state: loader)
    monkeypatch.setattr(prompts, "requesting_slot_project", lambda *args: None)
    monkeypatch.setattr(prompts, "_named_slot", lambda *args: SimpleNamespace(agent="custom"))
    monkeypatch.setattr(prompts, "session_skill_globs", lambda *args, **kw: None)
    monkeypatch.setattr(prompts, "served_provider_label", lambda served: provider)
    app = web.Application()
    app["state"] = SimpleNamespace(
        sessions=SimpleNamespace(get_agent=lambda key: None, get_provider=lambda key: object())
    )
    for core, key in zip(CORES, CORE_IDS):
        body = {
            "scope": "installed",
            "action": "read",
            "key": key,
            "capacity": skill_tool._read_capacity(key, 0),
        }
        payload = json.dumps(body).encode()
        request = make_mocked_request(
            "POST",
            "/api/skills/-/discover?" + urlencode({}),
            headers={"Content-Type": "application/json", "Content-Length": str(len(payload))},
            payload=BodyStreamPayload(payload),
            app=app,
        )
        result = json.loads((await prompts.api_skills(request)).body)
        assert result.get("matches"), (key, result.get("refusal"))
        match = result["matches"][0]
        assert match["content"] == _text(core), key
        rendered = skill_tool._render_read(key, match, 0)
        assert len(rendered) < KAS_READ_CHARS, (key, len(rendered))


def test_the_split_check_maps_moved_lines_and_reports_lost_ones() -> None:
    check = load_skill_script(
        "check_skill_split_under_test", ROOT / "scripts" / "check_skill_split.py"
    )
    base = "# T\n\nkeep me\nmove me\n  - [x](a.md) indented\nlose me\n"
    core = "# T\n\nkeep me\nsee references/r.md\n"
    refs = {"references/r.md": "move me\n- [x](../a.md) indented\n"}
    cov = check.classify(base, core, refs)
    assert (cov.total, cov.in_core, cov.in_refs) == (5, 2, 2)
    assert cov.missing == ["lose me"]
    # A rule the base states twice needs both copies: one dropped copy is lost.
    twice = check.classify("same rule\nsame rule\n", "same rule\n", {})
    assert twice.missing == ["same rule"]


def test_a_scope_that_maps_living_ui_reads_its_blocks_too() -> None:
    """A saved mapping written before the split names ``living-ui`` alone."""
    from kiro_crew.agent_discovery_parts.skill_scope import with_companion_skills

    home = "/h/.kiro/crew/skills"
    scope = with_companion_skills([f"{home}/living-ui/SKILL.md", f"{home}/widgets/SKILL.md"])
    assert f"{home}/living-ui-blocks/SKILL.md" in scope
    assert with_companion_skills([f"{home}/widgets/SKILL.md"]) == [f"{home}/widgets/SKILL.md"]
    assert with_companion_skills(scope) == scope
    # A home-relative mapping expands with the native separator on Windows.
    native = with_companion_skills(["C:\\Users\\u\\.kiro\\crew\\skills\\living-ui\\SKILL.md"])
    assert "C:\\Users\\u\\.kiro\\crew\\skills\\living-ui-blocks\\SKILL.md" in native


def test_a_skill_a_widget_skill_sends_the_agent_to_is_mapped_with_it() -> None:
    """An agent whose skills are mapped reads only those keys: a core that says
    ``skill_search`` ``key=<k>`` needs ``<k>`` mapped beside it."""
    from kiro_crew.agent_materialization.skill_resources import WIDGET_POINTER_SKILLS

    named = {
        key
        for skill in WIDGET_POINTER_SKILLS
        for key in re.findall(r"key=`([^`]+)`", _text(BUILTIN / skill / "SKILL.md"))
    }
    assert named <= set(WIDGET_POINTER_SKILLS), named - set(WIDGET_POINTER_SKILLS)
