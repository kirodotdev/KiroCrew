# SPDX-License-Identifier: Apache-2.0
"""A session's skill scope pinned to one agent spec file (``kiro_crew.skill_scope_pin``).

The pin replaces the name lookup for its session, so what it answers when the
pinned file is unreadable or declares no skills decides what the agent sees.
``None`` means "nothing pinned, resolve by name"; a list, even an empty one, is
the pinned file's own answer and must never fall back to the name lookup.
"""

from __future__ import annotations

import json
import uuid
from pathlib import Path

import pytest

from kiro_crew import agent_discovery
from kiro_crew.agent_discovery import SkillScopeResolutionError
from kiro_crew.skill_scope_pin import (
    pin_session_skill_spec,
    pinned_session_skill_spec,
    pinned_skill_globs,
)

# A spec that may take its inherited skill trees: an explicit tool list without
# "*", and a kirocrew-core entry that keeps skill_search.
_INHERITING_SPEC = {
    "name": "pinned",
    "tools": ["@kirocrew-core/skill_search"],
    "mcpServers": {"kirocrew-core": {}},
}


@pytest.fixture
def session_key():
    """A session key no other test pins, unpinned again afterwards."""
    key = f"pin-test-{uuid.uuid4().hex}"
    yield key
    pin_session_skill_spec(key, None)


def _write_spec(path: Path, spec: dict) -> Path:
    path.write_text(json.dumps(spec), encoding="utf-8")
    return path


class TestThePinItself:
    def test_an_empty_session_key_is_never_pinned(self, tmp_path):
        pin_session_skill_spec("", tmp_path / "spec.json")
        assert pinned_session_skill_spec("") is None
        assert pinned_session_skill_spec(None) is None
        assert pinned_skill_globs("") is None

    def test_none_removes_the_pin_and_the_name_lookup_resumes(self, session_key, tmp_path):
        spec = _write_spec(tmp_path / "spec.json", _INHERITING_SPEC)
        pin_session_skill_spec(session_key, spec)
        assert pinned_session_skill_spec(session_key) == spec
        pin_session_skill_spec(session_key, None)
        assert pinned_session_skill_spec(session_key) is None
        assert pinned_skill_globs(session_key) is None


class TestAnUnreadablePinnedSpec:
    def test_it_scopes_the_session_to_no_skills_not_the_name_lookup(self, session_key, tmp_path):
        pin_session_skill_spec(session_key, tmp_path / "missing.json")
        # [] is the pinned file's answer; None would hand the session back to
        # the name lookup, which is what the pin exists to prevent.
        assert pinned_skill_globs(session_key) == []

    def test_a_strict_caller_is_told_it_cannot_be_read(self, session_key, tmp_path):
        bad = tmp_path / "broken.json"
        bad.write_text("{not json", encoding="utf-8")
        pin_session_skill_spec(session_key, bad)
        with pytest.raises(SkillScopeResolutionError, match="broken.json"):
            pinned_skill_globs(session_key, strict=True)


class TestThePinnedSpecsOwnSkills:
    def test_its_skill_resources_are_the_scope(self, session_key, tmp_path):
        glob = (tmp_path / "skills" / "*" / "SKILL.md").as_posix()
        spec = _write_spec(
            tmp_path / "spec.json",
            {**_INHERITING_SPEC, "resources": [f"skill://{glob}", "file://README.md"]},
        )
        pin_session_skill_spec(session_key, spec)
        assert pinned_skill_globs(session_key, inherit_defaults=True) == [glob]

    def test_no_skill_resources_take_the_inherited_trees_only_when_asked(
        self, session_key, tmp_path, monkeypatch
    ):
        inherited = [str(tmp_path / "home" / "skills" / "*" / "SKILL.md")]
        seen_projects = []

        def fake_inherited(project_dir):
            seen_projects.append(project_dir)
            return inherited

        monkeypatch.setattr(agent_discovery, "inherited_skill_globs", fake_inherited)
        spec = _write_spec(tmp_path / "spec.json", _INHERITING_SPEC)
        pin_session_skill_spec(session_key, spec)
        assert pinned_skill_globs(session_key) == []
        assert seen_projects == []
        project = tmp_path / "project"
        assert (
            pinned_skill_globs(session_key, project_dir=project, inherit_defaults=True) == inherited
        )
        assert seen_projects == [project]

    def test_a_spec_that_may_not_inherit_gets_no_inherited_trees(
        self, session_key, tmp_path, monkeypatch
    ):
        monkeypatch.setattr(
            agent_discovery,
            "inherited_skill_globs",
            lambda _project_dir: pytest.fail("a zero-tool spec must not inherit skills"),
        )
        spec = _write_spec(tmp_path / "spec.json", {"name": "pinned", "tools": []})
        pin_session_skill_spec(session_key, spec)
        assert pinned_skill_globs(session_key, inherit_defaults=True) == []
