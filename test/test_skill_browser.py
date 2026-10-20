"""Tests for the skill directory browser API.

Covers:
- ``list_kiro_skills`` discovery of ``~/.kiro/skills/`` and workspace ``.kiro/skills/``
- ``_resolve_loaded_by_agents`` glob-matching against installed agent JSONs
- ``list_skill_tree`` / ``read_skill_file`` size + sensitive-path + escape guards
- ``_resolve_skill_root`` cross-source resolution (kirocrew / kiro-user / aim)
- ``GET /api/skills/<name>/tree`` and ``GET /api/skills/<name>/file`` end-to-end

Tests use a tmp_path fake $HOME so we never touch the real filesystem.

The package-skill detail read through the descriptor gate, and the containment of every
read inside the admitted skill root (symlinked leaves, the pinned descriptor, a redefined
root), are pinned in ``test_skill_browser_containment.py``; the shared helpers live in
``skill_browser_helpers.py``.
"""
from __future__ import annotations

import json
from pathlib import Path
from unittest.mock import MagicMock

import pytest
from aiohttp import web
from aiohttp.test_utils import TestClient, TestServer
from skill_browser_helpers import (  # noqa: F401 -- the fixture is requested by name
    _make_app,
    _write_skill,
    fake_home_fixture,
)

from conftest import make_dir_link
from kiro_crew.dashboard.handlers._shared import (
    SKILL_FILE_MAX_BYTES,
    SKILL_TREE_MAX_ENTRIES,
    _agent_loads_skill,
    _expand_agent_globs,
    _expand_resource_uri,
    _load_parsed_agents,
    _one_row_per_skill_file,
    _parse_skill_description,
    _resolve_loaded_by_agents,
    _resolve_skill_root,
    annotate_skills_with_agents,
    collect_skills_blocking,
    enumerate_skill_catalog,
    list_kiro_skills,
    list_skill_tree,
    read_skill_file,
    skill_rows_for_globs,
)

# ── _parse_skill_description ──


class TestParseSkillDescription:
    def test_extracts_description_and_always(self, tmp_path):
        f = tmp_path / "SKILL.md"
        f.write_text("---\nname: x\ndescription: hello world\nalways: true\n---\nbody\n")
        desc, always = _parse_skill_description(f)
        assert desc == "hello world"
        assert always is True

    def test_no_frontmatter_returns_empty(self, tmp_path):
        f = tmp_path / "SKILL.md"
        f.write_text("# No frontmatter here\n")
        assert _parse_skill_description(f) == ("", False)

    def test_truncated_frontmatter_returns_empty(self, tmp_path):
        f = tmp_path / "SKILL.md"
        # Open frontmatter, never closed.
        f.write_text("---\nname: x\n" + ("padding\n" * 100))
        # Cap on read means we may see partial frontmatter; either way the
        # closing ``---`` is missing so parser returns empty.
        desc, always = _parse_skill_description(f)
        assert desc == ""
        assert always is False

    def test_nested_description_does_not_replace_top_level(self, tmp_path):
        f = tmp_path / "SKILL.md"
        f.write_text(
            "---\nname: x\ndescription: the skill\ninputs:\n"
            "  - name: a\n    description: one input\n\tdescription: tabbed\n---\n"
        )
        assert _parse_skill_description(f) == ("the skill", False)

    def test_strips_quotes_from_description(self, tmp_path):
        f = tmp_path / "SKILL.md"
        f.write_text('---\ndescription: "quoted desc"\n---\n')
        desc, _ = _parse_skill_description(f)
        assert desc == "quoted desc"

    def test_symlink_to_sensitive_file_returns_empty(self, fake_home):
        """Security: a SKILL.md that is a symlink to a sensitive credential
        file must not be read, even though it sits under a trusted skills
        root.  The resolved target is what's gated, not the link location."""
        creds = fake_home / ".aws" / "credentials"
        creds.parent.mkdir(parents=True)
        creds.write_text("---\ndescription: SECRET\n---\n")
        skill_dir = fake_home / ".kiro" / "skills" / "evil"
        skill_dir.mkdir(parents=True)
        link = skill_dir / "SKILL.md"
        link.symlink_to(creds)
        # The credential content must never surface as a description.
        assert _parse_skill_description(link) == ("", False)


# ── list_kiro_skills ──


class TestListKiroSkills:
    def test_lists_global_kiro_skills(self, fake_home):
        kiro = fake_home / ".kiro" / "skills"
        _write_skill(kiro, "alpha", description="alpha desc")
        _write_skill(kiro, "beta", description="beta desc")
        out = list_kiro_skills(project_dir=None)
        names = [s["name"] for s in out]
        assert "alpha" in names and "beta" in names
        for s in out:
            assert s["source"] == "kiro-user"
            assert s["key"].startswith("kiro-user/")

    def test_lists_workspace_skills_too(self, fake_home, tmp_path):
        proj = tmp_path / "proj"
        ws = proj / ".kiro" / "skills"
        _write_skill(ws, "ws-skill", description="workspace one")
        out = list_kiro_skills(project_dir=proj)
        keys = [s["key"] for s in out]
        assert "kiro-workspace/ws-skill" in keys

    def test_skips_directories_without_skill_md(self, fake_home):
        kiro = fake_home / ".kiro" / "skills"
        kiro.mkdir(parents=True)
        (kiro / "dangling").mkdir()  # no SKILL.md inside
        assert list_kiro_skills(None) == []

    def test_skips_dotfile_dirs(self, fake_home):
        kiro = fake_home / ".kiro" / "skills"
        _write_skill(kiro, ".hidden", description="should skip")
        out = list_kiro_skills(None)
        assert all(s["name"] != ".hidden" for s in out)

    def test_returns_empty_when_no_kiro_dir(self, fake_home):
        # ~/.kiro/skills/ does not exist at all
        assert list_kiro_skills(None) == []


# ── _expand_resource_uri / _agent_loads_skill / _resolve_loaded_by_agents ──


class TestResourceUriExpansion:
    def test_skill_uri_with_tilde_expands_to_home(self, fake_home, tmp_path):
        agent_path = tmp_path / "agent.json"
        out = _expand_resource_uri("skill://~/.kiro/skills/*/SKILL.md", agent_path)
        assert out == str(fake_home / ".kiro" / "skills" / "*" / "SKILL.md")

    def test_non_skill_uri_returns_none(self, tmp_path):
        agent_path = tmp_path / "agent.json"
        assert _expand_resource_uri("file://foo", agent_path) is None
        assert _expand_resource_uri("other://x", agent_path) is None

    def test_workspace_relative_resolves_against_project_root(self, tmp_path):
        # ``<project>/.kiro/agents/foo.json`` — a workspace-relative
        # ``.kiro/skills/...`` URI must resolve to ``<project>/.kiro/skills``
        # NOT ``<project>/.kiro/.kiro/skills`` (the doubled-segment bug).
        proj = tmp_path / "proj"
        agents_dir = proj / ".kiro" / "agents"
        agents_dir.mkdir(parents=True)
        agent_path = agents_dir / "foo.json"
        out = _expand_resource_uri("skill://.kiro/skills/*/SKILL.md", agent_path)
        expected = str(proj / ".kiro" / "skills" / "*" / "SKILL.md")
        assert out == expected
        assert ".kiro/.kiro" not in out

    def test_absolute_skill_uri_passthrough(self, tmp_path):
        agent_path = tmp_path / "agent.json"
        assert _expand_resource_uri("skill:///abs/path", agent_path) == "/abs/path"


class TestAgentLoadsSkill:
    def test_glob_matches_skill_md(self, fake_home, tmp_path):
        skill_md = fake_home / ".kiro" / "skills" / "linear" / "SKILL.md"
        skill_md.parent.mkdir(parents=True)
        skill_md.write_text("---\n---\n")
        agent_json = {
            "name": "my-agent",
            "resources": ["skill://~/.kiro/skills/*/SKILL.md"],
        }
        assert _agent_loads_skill(agent_json, tmp_path / "a.json", skill_md) is True

    def test_no_match_when_glob_excludes(self, fake_home, tmp_path):
        skill_md = fake_home / ".kiro" / "skills" / "linear" / "SKILL.md"
        skill_md.parent.mkdir(parents=True)
        skill_md.write_text("---\n---\n")
        agent_json = {
            "name": "my-agent",
            "resources": ["skill://~/.kiro/skills/specific-only/SKILL.md"],
        }
        assert _agent_loads_skill(agent_json, tmp_path / "a.json", skill_md) is False

    def test_handles_non_string_resources(self, fake_home, tmp_path):
        skill_md = fake_home / ".kiro" / "skills" / "linear" / "SKILL.md"
        skill_md.parent.mkdir(parents=True)
        skill_md.write_text("")
        valid = "skill://~/.kiro/skills/*/SKILL.md"
        garbage: list = [{"oops": "object"}, None, 7]
        # Garbage entries are skipped, and the valid glob beside them still matches.
        agent_json = {"name": "x", "resources": [*garbage, valid]}
        assert _agent_loads_skill(agent_json, tmp_path / "a.json", skill_md) is True
        only_garbage = {"name": "x", "resources": garbage}
        assert _agent_loads_skill(only_garbage, tmp_path / "a.json", skill_md) is False

    def test_resources_can_be_missing_or_non_list(self, tmp_path):
        skill_md = tmp_path / "SKILL.md"
        skill_md.write_text("")
        assert _agent_loads_skill({}, tmp_path / "a.json", skill_md) is False
        assert _agent_loads_skill({"resources": "not a list"}, tmp_path / "a.json", skill_md) is False


class TestResolveLoadedByAgents:
    def test_finds_agents_that_load_skill(self, fake_home):
        agents_dir = fake_home / ".kiro" / "agents"
        agents_dir.mkdir(parents=True)
        skill_md = fake_home / ".kiro" / "skills" / "x" / "SKILL.md"
        skill_md.parent.mkdir(parents=True)
        skill_md.write_text("---\n---\n")

        (agents_dir / "loader.json").write_text(json.dumps({
            "name": "loader",
            "resources": ["skill://~/.kiro/skills/*/SKILL.md"],
        }))
        (agents_dir / "non-loader.json").write_text(json.dumps({
            "name": "non-loader",
            "resources": ["file://something-else"],
        }))
        out = _resolve_loaded_by_agents(skill_md)
        assert out == ["loader"]

    def test_skips_unparseable_agent_json(self, fake_home):
        agents_dir = fake_home / ".kiro" / "agents"
        agents_dir.mkdir(parents=True)
        skill_md = fake_home / ".kiro" / "skills" / "x" / "SKILL.md"
        skill_md.parent.mkdir(parents=True)
        skill_md.write_text("---\n---\n")

        (agents_dir / "broken.json").write_text("{ this is not json")
        # Does not raise, returns empty.
        assert _resolve_loaded_by_agents(skill_md) == []

    def test_survives_non_utf8_agent_json(self, fake_home):
        """A non-UTF-8 file matching ``*.json`` (e.g. a macOS AppleDouble
        ``._*.json`` sidecar dragged in by a manual tar) must be skipped, not
        raise UnicodeDecodeError and 500 the /api/skills endpoint."""
        agents_dir = fake_home / ".kiro" / "agents"
        agents_dir.mkdir(parents=True)
        skill_md = fake_home / ".kiro" / "skills" / "x" / "SKILL.md"
        skill_md.parent.mkdir(parents=True)
        skill_md.write_text("---\n---\n")

        # A valid loader that must still be discovered despite the bad sibling.
        (agents_dir / "loader.json").write_text(json.dumps({
            "name": "loader",
            "resources": ["skill://~/.kiro/skills/*/SKILL.md"],
        }))
        # AppleDouble sidecar: starts with "._" and is non-UTF-8 binary.
        (agents_dir / "._loader.json").write_bytes(
            b"\x02\x00\x00\x00\xa3\x80\x81\x82 not utf-8"
        )
        # Arbitrary non-UTF-8 *.json that is not an AppleDouble name either.
        (agents_dir / "binary.json").write_bytes(b"\xff\xfe\x00\x01\xa3")

        out = _resolve_loaded_by_agents(skill_md)
        assert out == ["loader"]

    def test_returns_empty_when_no_agents_dir(self, fake_home):
        skill_md = fake_home / "elsewhere.md"
        skill_md.write_text("")
        assert _resolve_loaded_by_agents(skill_md) == []

    def test_skips_symlink_to_sensitive_file(self, fake_home):
        """Security: a ``*.json`` symlink under ~/.kiro/agents/ that points at
        a sensitive credential file must NOT be read.  Otherwise an attacker
        could exfiltrate ~/.aws/credentials via the loaded_by_agents scan."""
        agents_dir = fake_home / ".kiro" / "agents"
        agents_dir.mkdir(parents=True)
        skill_md = fake_home / ".kiro" / "skills" / "x" / "SKILL.md"
        skill_md.parent.mkdir(parents=True)
        skill_md.write_text("---\n---\n")

        # Plant a credential file and symlink it in as a fake agent config.
        creds = fake_home / ".aws" / "credentials"
        creds.parent.mkdir(parents=True)
        creds.write_text('{"name": "evil", "resources": ["skill://~/.kiro/skills/*/SKILL.md"]}')
        (agents_dir / "evil.json").symlink_to(creds)

        # Even though the file *would* match, the sensitive-path guard skips
        # it — the credential file is never read and "evil" never returned.
        out = _resolve_loaded_by_agents(skill_md)
        assert "evil" not in out
        assert out == []


class TestLoadParsedAgents:
    """``_load_parsed_agents`` reads every agent JSON once into
    ``(name, data, path)`` tuples, skipping junk and sensitive symlinks."""

    def test_parses_each_agent_once(self, fake_home):
        agents_dir = fake_home / ".kiro" / "agents"
        agents_dir.mkdir(parents=True)
        (agents_dir / "a.json").write_text(json.dumps({"name": "alpha", "resources": []}))
        (agents_dir / "b.json").write_text(json.dumps({"name": "beta", "resources": []}))

        parsed = _load_parsed_agents()
        names = sorted(name for name, _data, _path in parsed)
        assert names == ["alpha", "beta"]
        # Each tuple carries the parsed dict and the source path.
        for name, data, path in parsed:
            assert isinstance(data, dict)
            assert path.suffix == ".json"
            assert data["name"] == name

    def test_name_falls_back_to_stem(self, fake_home):
        agents_dir = fake_home / ".kiro" / "agents"
        agents_dir.mkdir(parents=True)
        # No "name" key → the file stem is used.
        (agents_dir / "stem-name.json").write_text(json.dumps({"resources": []}))

        parsed = _load_parsed_agents()
        assert [name for name, _d, _p in parsed] == ["stem-name"]

    def test_skips_appledouble_invalid_and_non_dict(self, fake_home):
        agents_dir = fake_home / ".kiro" / "agents"
        agents_dir.mkdir(parents=True)
        (agents_dir / "good.json").write_text(json.dumps({"name": "good", "resources": []}))
        (agents_dir / "._good.json").write_bytes(b"\x00\xa3 not utf-8")  # AppleDouble sidecar
        (agents_dir / "broken.json").write_text("{ not json")
        (agents_dir / "binary.json").write_bytes(b"\xff\xfe\x00\x01")  # non-UTF-8
        (agents_dir / "list.json").write_text(json.dumps(["not", "a", "dict"]))

        parsed = _load_parsed_agents()
        assert [name for name, _d, _p in parsed] == ["good"]

    def test_skips_sensitive_symlink(self, fake_home):
        agents_dir = fake_home / ".kiro" / "agents"
        agents_dir.mkdir(parents=True)
        creds = fake_home / ".aws" / "credentials"
        creds.parent.mkdir(parents=True)
        creds.write_text(json.dumps({"name": "evil", "resources": []}))
        (agents_dir / "evil.json").symlink_to(creds)

        assert _load_parsed_agents() == []

    def test_empty_when_no_agents_dir(self, fake_home):
        assert _load_parsed_agents() == []


class TestAnnotateSkillsWithAgents:
    """``annotate_skills_with_agents`` parses agents ONCE and annotates every
    skill in-place — the batch path behind ``GET /api/skills``."""

    def _setup_agents(self, fake_home):
        agents_dir = fake_home / ".kiro" / "agents"
        agents_dir.mkdir(parents=True)
        (agents_dir / "loader.json").write_text(json.dumps({
            "name": "loader",
            "resources": ["skill://~/.kiro/skills/*/SKILL.md"],
        }))
        (agents_dir / "non-loader.json").write_text(json.dumps({
            "name": "non-loader",
            "resources": ["file://something-else"],
        }))

    def test_annotates_each_skill_in_place(self, fake_home):
        self._setup_agents(fake_home)
        skills_root = fake_home / ".kiro" / "skills"
        a = _write_skill(skills_root, "alpha")
        b = _write_skill(skills_root, "beta")
        skills = [
            {"name": "alpha", "path": str(a / "SKILL.md")},
            {"name": "beta", "path": str(b / "SKILL.md")},
        ]
        annotate_skills_with_agents(skills)
        # Both skills are loaded by the wildcard loader, by neither non-loader.
        assert skills[0]["loaded_by_agents"] == ["loader"]
        assert skills[1]["loaded_by_agents"] == ["loader"]

    def test_skill_without_path_gets_empty_list(self, fake_home):
        self._setup_agents(fake_home)
        skills = [{"name": "no-path"}, {"name": "blank", "path": ""}]
        annotate_skills_with_agents(skills)
        assert skills[0]["loaded_by_agents"] == []
        assert skills[1]["loaded_by_agents"] == []

    def test_matches_per_skill_resolver(self, fake_home):
        """Batch annotation must produce the same result as calling the
        single-skill ``_resolve_loaded_by_agents`` for each skill."""
        self._setup_agents(fake_home)
        skills_root = fake_home / ".kiro" / "skills"
        paths = [_write_skill(skills_root, n) / "SKILL.md" for n in ("x", "y", "z")]
        skills = [{"name": p.parent.name, "path": str(p)} for p in paths]

        annotate_skills_with_agents(skills)
        for p, s in zip(paths, skills):
            assert s["loaded_by_agents"] == _resolve_loaded_by_agents(p)

    def test_parses_agents_once_regardless_of_skill_count(self, fake_home, monkeypatch):
        """The core fix: agent JSONs are parsed ONCE per request, not once
        per skill (the old O(skills × agents) blowup that wedged the loop)."""
        self._setup_agents(fake_home)
        skills_root = fake_home / ".kiro" / "skills"
        skills = [
            {"name": n, "path": str(_write_skill(skills_root, n) / "SKILL.md")}
            for n in ("s1", "s2", "s3", "s4", "s5")
        ]

        import kiro_crew.dashboard.handlers._shared as shared

        calls = {"n": 0}
        real = shared._load_parsed_agents

        def _counting() -> list:
            calls["n"] += 1
            return real()

        monkeypatch.setattr(shared, "_load_parsed_agents", _counting)
        shared.annotate_skills_with_agents(skills)

        assert calls["n"] == 1  # ONE parse for all 5 skills, not 5
        assert all(s["loaded_by_agents"] == ["loader"] for s in skills)

    def test_reusing_parsed_agents_avoids_rereading_disk(self, fake_home):
        """Passing a pre-parsed agent list to ``_resolve_loaded_by_agents``
        reuses it instead of re-globbing/parsing ~/.kiro/agents."""
        self._setup_agents(fake_home)
        skills_root = fake_home / ".kiro" / "skills"
        skill_md = _write_skill(skills_root, "alpha") / "SKILL.md"

        parsed = _load_parsed_agents()
        # Delete the agents dir AFTER parsing: a re-read would now find nothing,
        # so a correct reuse must still resolve "loader" from the passed list.
        for f in (fake_home / ".kiro" / "agents").glob("*.json"):
            f.unlink()
        assert _resolve_loaded_by_agents(skill_md, parsed) == ["loader"]
        # Sanity: without the pre-parsed list it re-reads disk and finds none.
        assert _resolve_loaded_by_agents(skill_md) == []


# ── collect_skills_blocking ──


class TestCollectSkillsBlocking:
    """``collect_skills_blocking`` is the synchronous core behind
    ``GET /api/skills`` — it gathers every source AND annotates, so the
    whole thing can be offloaded to a thread in one job."""

    def _loader_with(self, *entries):
        """A stand-in SkillsLoader whose list_skills() returns *entries*."""
        loader = MagicMock()
        loader.list_skills.return_value = [dict(e) for e in entries]
        return loader

    def test_merges_all_sources_and_annotates(self, fake_home):
        # Agent that loads every kiro skill via wildcard.
        agents_dir = fake_home / ".kiro" / "agents"
        agents_dir.mkdir(parents=True)
        (agents_dir / "loader.json").write_text(json.dumps({
            "name": "loader",
            "resources": ["skill://~/.kiro/skills/*/SKILL.md"],
        }))
        kiro_root = fake_home / ".kiro" / "skills"
        kiro_md = _write_skill(kiro_root, "kiro-one") / "SKILL.md"

        loader = self._loader_with({"key": "mc", "name": "mc", "path": ""})
        # Package skills arrive pre-structured from CapabilityManager.list_skills()
        # (the manager owns parsing — no core text grammar).
        package_skills = [{"key": "package/aim-one", "name": "aim-one", "source": "package", "path": ""}]

        result = collect_skills_blocking(loader, package_skills, project_dir=None)

        by_key = {s["key"]: s for s in result}
        # kirocrew source defaulted, package rows merged, kiro discovered.
        assert by_key["mc"]["source"] == "kirocrew"
        assert "package/aim-one" in by_key
        assert "kiro-user/kiro-one" in by_key
        # Every entry carries loaded_by_agents; the kiro skill matches loader.
        assert all("loaded_by_agents" in s for s in result)
        assert by_key["kiro-user/kiro-one"]["loaded_by_agents"] == ["loader"]
        assert str(kiro_md)  # path was materialized

    def test_empty_package_skills_skips_package(self, fake_home):
        loader = self._loader_with({"key": "mc", "name": "mc", "path": ""})
        result = collect_skills_blocking(loader, [], project_dir=None)
        assert [s["key"] for s in result] == ["mc"]
        assert result[0]["loaded_by_agents"] == []

    def test_empty_everything(self, fake_home):
        loader = self._loader_with()
        assert collect_skills_blocking(loader, [], project_dir=None) == []


class TestOneRowPerSkillFile:
    """Two sources reaching ONE SKILL.md list it once, and only a row from the
    user-writable skills folder stays editable."""

    def test_extra_paths_over_kiro_user_lists_once(self, fake_home, close_skills_loaders):
        """Real loader: ``skills.extra_paths`` naming ``~/.kiro/skills`` reaches
        the file as a Kiro Crew row AND as a ``kiro-user`` row; one is listed."""
        from kiro_crew.config import KiroCrewConfig
        from kiro_crew.skills import SkillsLoader

        user_root = fake_home / ".kiro" / "skills"
        skill_dir = user_root / "tracker"
        skill_dir.mkdir(parents=True)
        (skill_dir / "SKILL.md").write_text(
            "---\nname: tracker\ndescription: top level\n"
            "inputs:\n  - name: x\n    description: nested input\n---\nbody\n"
        )
        cfg = KiroCrewConfig()
        cfg.skills.extra_paths = [str(user_root)]
        loader = SkillsLoader(
            skills_path=fake_home / ".kiro" / "crew" / "skills",
            install_builtins=False,
            config=cfg,
        )

        rows = [r for r in collect_skills_blocking(loader, [], project_dir=None)
                if r["name"] == "tracker"]

        assert len(rows) == 1
        # Neither row is editable, so the row keyed by the file's own folder
        # stays: its key is the one an agent-skill mapping saves.
        assert rows[0]["key"] == "kiro-user/tracker"
        assert rows[0]["description"] == "top level"
        assert rows[0]["aliases"] == ["tracker"]

    def test_editable_row_wins_and_agents_merge(self, tmp_path):
        md = tmp_path / "s" / "SKILL.md"
        md.parent.mkdir()
        md.write_text("---\nname: s\n---\n")
        alias = tmp_path / "alias"
        alias.symlink_to(md.parent)  # a second spelling of the same file
        rows = [
            {"key": "s", "source": "kirocrew", "owned": False,
             "path": str(alias / "SKILL.md"), "loaded_by_agents": ["a"]},
            {"key": "s2", "source": "kirocrew", "owned": True,
             "path": str(md), "loaded_by_agents": ["b"]},
            {"key": "kiro-user/s", "source": "kiro-user",
             "path": str(md), "loaded_by_agents": ["a", "c"]},
        ]

        out = _one_row_per_skill_file(rows)

        assert [r["key"] for r in out] == ["s2"]
        assert out[0]["loaded_by_agents"] == ["b", "a", "c"]
        # The dropped keys stay resolvable for key lookups.
        assert out[0]["aliases"] == ["s", "kiro-user/s"]

    def test_inode_zero_never_merges(self, tmp_path, monkeypatch):
        """A filesystem that reports inode 0 for every file gives no identity."""
        import os as _os

        a = tmp_path / "a" / "SKILL.md"
        b = tmp_path / "b" / "SKILL.md"
        for p in (a, b):
            p.parent.mkdir()
            p.write_text("---\nname: s\n---\n")
        real_stat = _os.stat

        def zero_ino(path, *args, **kwargs):
            st = real_stat(path, *args, **kwargs)
            return _os.stat_result((st.st_mode, 0, st.st_dev, *st[3:]))

        monkeypatch.setattr(
            "kiro_crew.dashboard.handlers._shared.os.stat", zero_ino
        )
        rows = [
            {"key": "a", "source": "kirocrew", "path": str(a)},
            {"key": "kiro-user/b", "source": "kiro-user", "path": str(b)},
        ]
        assert [r["key"] for r in _one_row_per_skill_file(rows)] == ["a", "kiro-user/b"]

    def test_kept_row_carries_the_dropped_rows_usage(self, tmp_path):
        md = tmp_path / "s" / "SKILL.md"
        md.parent.mkdir()
        md.write_text("---\nname: s\n---\n")
        rows = [
            {"key": "s", "source": "kirocrew", "owned": False, "path": str(md),
             "deliveries": 7, "last_used_at": 1700000000.0,
             "created_at": 1690000000.0,
             "size_bytes": 42, "inject_on_trigger": False},
            {"key": "kiro-user/s", "source": "kiro-user", "path": str(md)},
        ]
        out = _one_row_per_skill_file(rows)
        assert [r["key"] for r in out] == ["kiro-user/s"]
        assert out[0]["deliveries"] == 7
        assert out[0]["last_used_at"] == 1700000000.0
        assert out[0]["created_at"] == 1690000000.0
        assert out[0]["size_bytes"] == 42
        # A pointer-only skill stays pointer-only on the kept row.
        assert out[0]["inject_on_trigger"] is False

    def test_unowned_crew_row_yields_to_the_folder_owner(self, tmp_path):
        """An edition root is walked by the loader too; its ``package/`` row stays."""
        md = tmp_path / "s" / "SKILL.md"
        md.parent.mkdir()
        md.write_text("---\nname: s\n---\n")
        rows = [
            {"key": "s", "source": "kirocrew", "owned": False, "path": str(md)},
            {"key": "package/s", "source": "package", "path": str(md)},
        ]
        out = _one_row_per_skill_file(rows)
        assert [r["key"] for r in out] == ["package/s"]
        assert out[0]["aliases"] == ["s"]

    def test_directory_path_compares_by_its_skill_md(self, tmp_path):
        md = tmp_path / "s" / "SKILL.md"
        md.parent.mkdir()
        md.write_text("---\nname: s\n---\n")
        rows = [
            {"key": "package/s", "source": "package", "path": str(md.parent)},
            {"key": "kiro-user/s", "source": "kiro-user", "path": str(md)},
        ]
        assert [r["key"] for r in _one_row_per_skill_file(rows)] == ["package/s"]

    def test_distinct_files_and_workspace_rows_are_kept(self, tmp_path):
        a = tmp_path / "a" / "SKILL.md"
        b = tmp_path / "b" / "SKILL.md"
        for p in (a, b):
            p.parent.mkdir()
            p.write_text("---\nname: s\n---\n")
        rows = [
            {"key": "s", "source": "kirocrew", "path": str(a)},
            {"key": "kiro-user/s", "source": "kiro-user", "path": str(b)},
            # A workspace row is consent-gated, so it never merges, even
            # when it names a file another row lists.
            {"key": "kiro-workspace/s", "source": "kiro-workspace", "path": str(a)},
            {"key": "no-path", "source": "kirocrew", "path": ""},
            {"key": "gone", "source": "kirocrew", "path": str(tmp_path / "missing")},
        ]
        assert [r["key"] for r in _one_row_per_skill_file(rows)] == [
            "s", "kiro-user/s", "kiro-workspace/s", "no-path", "gone",
        ]


# ── _expand_agent_globs (annotation O(n²) reduction) ──


class TestExpandAgentGlobs:
    """Agent globs are expanded ONCE up front, not per skill."""

    def test_expands_and_drops_resourceless_agents(self, fake_home):
        parsed = [
            ("loader", {"resources": ["skill://~/.kiro/skills/*/SKILL.md"]}, fake_home / "a.json"),
            ("noload", {"resources": ["file://x"]}, fake_home / "b.json"),
            ("empty", {"resources": []}, fake_home / "c.json"),
            ("badtype", {"resources": "not a list"}, fake_home / "d.json"),
        ]
        out = dict(_expand_agent_globs(parsed))
        # Only the agent with a skill:// resource survives, with an expanded glob.
        assert set(out) == {"loader"}
        assert out["loader"] == [str(fake_home / ".kiro" / "skills" / "*" / "SKILL.md")]

    def test_annotation_matches_per_skill_resolver_after_optimization(self, fake_home):
        """The optimized batch path must still equal the single-skill resolver."""
        agents_dir = fake_home / ".kiro" / "agents"
        agents_dir.mkdir(parents=True)
        (agents_dir / "loader.json").write_text(json.dumps({
            "name": "loader",
            "resources": ["skill://~/.kiro/skills/*/SKILL.md"],
        }))
        skills_root = fake_home / ".kiro" / "skills"
        paths = [_write_skill(skills_root, n) / "SKILL.md" for n in ("a", "b")]
        skills = [{"name": p.parent.name, "path": str(p)} for p in paths]
        annotate_skills_with_agents(skills)
        for p, s in zip(paths, skills):
            assert s["loaded_by_agents"] == _resolve_loaded_by_agents(p)


class TestProjectRelativeGlobAnchorsOnProjectDir:
    """The picker's agent filter must resolve a workspace-relative
    ``skill://.kiro/skills/*`` glob against the requesting slot's project,
    the way ``session_skill_globs`` does on the runtime path. Without the
    project, a user-dir agent's relative glob collapses onto ``~`` and never
    matches a ``kiro-workspace`` row, so the agent-scoped listing drops every
    project skill — the reported symptom.
    """

    def test_expand_resource_uri_prefers_project_dir(self, fake_home, tmp_path):
        # A user-dir spec: with no project the relative glob falls back three
        # levels above the spec file (onto ``~``); with a project it anchors there.
        agent_path = fake_home / ".kiro" / "agents" / "generalist.json"
        proj = tmp_path / "proj"
        without = _expand_resource_uri("skill://.kiro/skills/*/SKILL.md", agent_path)
        withp = _expand_resource_uri(
            "skill://.kiro/skills/*/SKILL.md", agent_path, project_dir=proj
        )
        assert withp == str(proj / ".kiro" / "skills" / "*" / "SKILL.md")
        assert without != withp

    def test_expand_agent_globs_threads_project_dir(self, fake_home, tmp_path):
        proj = tmp_path / "proj"
        parsed = [(
            "generalist",
            {"resources": ["skill://.kiro/skills/*/SKILL.md"]},
            fake_home / ".kiro" / "agents" / "generalist.json",
        )]
        out = dict(_expand_agent_globs(parsed, project_dir=proj))
        assert out["generalist"] == [str(proj / ".kiro" / "skills" / "*" / "SKILL.md")]

    def test_annotation_anchors_workspace_row_only_with_project(self, fake_home, tmp_path):
        # A custom agent in the USER dir mapping only the relative glob.
        agents_dir = fake_home / ".kiro" / "agents"
        agents_dir.mkdir(parents=True)
        (agents_dir / "generalist.json").write_text(json.dumps({
            "name": "generalist",
            "resources": ["skill://.kiro/skills/*/SKILL.md"],
        }))
        proj = tmp_path / "proj"
        ws_md = _write_skill(proj / ".kiro" / "skills", "ws-one") / "SKILL.md"
        row = {"key": "kiro-workspace/ws-one", "name": "ws-one", "path": str(ws_md)}

        skills_without = [dict(row)]
        annotate_skills_with_agents(skills_without)
        assert skills_without[0]["loaded_by_agents"] == []

        skills_with = [dict(row)]
        annotate_skills_with_agents(skills_with, project_dir=proj)
        assert skills_with[0]["loaded_by_agents"] == ["generalist"]

    def test_rows_for_globs_matches_project_anchored_glob(self, fake_home, tmp_path):
        """``skill_rows_for_globs`` keeps the rows whose path matches the
        owner-resolved globs, the way the ``?agent=`` filter scopes the list."""
        proj = tmp_path / "proj"
        ws_md = _write_skill(proj / ".kiro" / "skills", "ws-one") / "SKILL.md"
        other = _write_skill(fake_home / ".kiro" / "skills", "user-one") / "SKILL.md"
        rows = [
            {"key": "kiro-workspace/ws-one", "path": str(ws_md)},
            {"key": "kiro-user/user-one", "path": str(other)},
        ]
        globs = [str(proj / ".kiro" / "skills" / "*" / "SKILL.md")]
        kept = skill_rows_for_globs(rows, globs)
        assert [r["key"] for r in kept] == ["kiro-workspace/ws-one"]

    def test_rows_for_globs_matches_through_resolved_symlink(self, fake_home, tmp_path):
        """A row whose listed path is a symlink still matches a glob written
        against the resolved target, via ``_matches_any``'s resolved-path retry
        — the link-vs-resolved-path disagreement a bare fnmatch would miss."""
        real_dir = tmp_path / "real-skills" / "linked-one"
        real_dir.mkdir(parents=True)
        (real_dir / "SKILL.md").write_text("---\nname: linked-one\n---\n")
        link_root = tmp_path / "proj" / ".kiro" / "skills"
        link_root.mkdir(parents=True)
        link = link_root / "linked-one"
        # make_dir_link falls back to a junction on Windows (no privilege
        # needed), so the resolved-path retry stays exercised on every host
        # rather than skipped (a-ratchet-may-only-tighten).
        make_dir_link(link, real_dir)
        rows = [{"key": "kiro-workspace/linked-one", "path": str(link / "SKILL.md")}]
        globs = [str(real_dir.parent / "*" / "SKILL.md")]
        kept = skill_rows_for_globs(rows, globs)
        assert [r["key"] for r in kept] == ["kiro-workspace/linked-one"]

    def test_rows_for_globs_matches_a_collapsed_duplicate_spelling(self, tmp_path):
        """A skill listed twice -- a crew skill also symlinked into
        ``~/.kiro/skills`` -- collapses to the crew row, whose path is the
        real file. An agent glob naming only the ``~/.kiro/skills`` spelling
        must still keep it: the dedup carries the dropped row's path in
        ``alias_paths`` and the filter matches every spelling, as the
        ``loaded_by_agents`` union it replaced did."""
        crew_dir = tmp_path / "crew" / "skills" / "dup-one"
        crew_dir.mkdir(parents=True)
        (crew_dir / "SKILL.md").write_text("---\nname: dup-one\n---\n")
        user_root = tmp_path / "home" / ".kiro" / "skills"
        user_root.mkdir(parents=True)
        link = user_root / "dup-one"
        make_dir_link(link, crew_dir)
        rows = _one_row_per_skill_file([
            {"key": "dup-one", "source": "kirocrew", "owned": True,
             "path": str(crew_dir / "SKILL.md")},
            {"key": "kiro-user/dup-one", "source": "kiro-user",
             "path": str(link / "SKILL.md")},
        ])
        assert [r["key"] for r in rows] == ["dup-one"]
        assert rows[0]["alias_paths"] == [str(link / "SKILL.md")]

        globs = [str(user_root / "*" / "SKILL.md")]
        kept = skill_rows_for_globs(rows, globs)
        assert [r["key"] for r in kept] == ["dup-one"]

    def test_project_spec_shadows_user_spec_via_owner(self, fake_home, tmp_path):
        """A project agent spec shadowing a same-named user spec decides the
        filter's scope, because the filter resolves ``agent_skill_globs(
        project_dir=…)`` — whose winner ``list_agents`` picks — and matches rows
        against it. The user spec maps nothing; the project spec maps the
        project skill, so the project spec must win and the row is kept.
        """
        from kiro_crew.agent_discovery import agent_skill_globs

        user_agents = fake_home / ".kiro" / "agents"
        user_agents.mkdir(parents=True)
        (user_agents / "generalist.json").write_text(json.dumps({
            "name": "generalist",
            "resources": ["skill://~/.kiro/skills/none/SKILL.md"],
        }))
        proj = tmp_path / "proj"
        proj_agents = proj / ".kiro" / "agents"
        proj_agents.mkdir(parents=True)
        (proj_agents / "generalist.json").write_text(json.dumps({
            "name": "generalist",
            "resources": ["skill://.kiro/skills/*/SKILL.md"],
        }))
        ws_md = _write_skill(proj / ".kiro" / "skills", "ws-one") / "SKILL.md"
        rows = [{"key": "kiro-workspace/ws-one", "path": str(ws_md)}]

        globs = agent_skill_globs("generalist", project_dir=str(proj))
        kept = skill_rows_for_globs(rows, globs)
        assert [r["key"] for r in kept] == ["kiro-workspace/ws-one"]

    def test_collect_blocking_anchors_project_skill_for_badge(self, fake_home, tmp_path):
        """The ``loaded_by_agents`` badge counts a project row for a user-dir
        agent's relative glob once the catalog is assembled with the project."""
        agents_dir = fake_home / ".kiro" / "agents"
        agents_dir.mkdir(parents=True)
        (agents_dir / "generalist.json").write_text(json.dumps({
            "name": "generalist",
            "resources": ["skill://.kiro/skills/*/SKILL.md"],
        }))
        proj = tmp_path / "proj"
        ws_md = _write_skill(proj / ".kiro" / "skills", "ws-one") / "SKILL.md"

        loader = MagicMock()
        loader.list_skills.return_value = []
        loader.catalog_project_skills.return_value = [
            {"key": "ws-one", "name": "ws-one", "path": str(ws_md)}
        ]

        result = collect_skills_blocking(loader, [], project_dir=proj)
        by_key = {s["key"]: s for s in result}
        assert "kiro-workspace/ws-one" in by_key
        assert by_key["kiro-workspace/ws-one"]["loaded_by_agents"] == ["generalist"]


# ── list_skill_tree ──


class TestListSkillTree:
    def test_returns_files_and_dirs(self, fake_home):
        skill = fake_home / ".kiro" / "crew" / "skills" / "demo"
        skill.mkdir(parents=True)
        (skill / "SKILL.md").write_text("---\n---\n")
        (skill / "helper.sh").write_text("echo hi\n")
        (skill / "references").mkdir()
        (skill / "references" / "doc.md").write_text("# doc\n")
        out = list_skill_tree(skill)
        kinds = {(e["path"], e["type"]) for e in out}
        assert ("SKILL.md", "file") in kinds
        assert ("helper.sh", "file") in kinds
        assert ("references", "dir") in kinds
        assert ("references/doc.md", "file") in kinds

    def test_caps_at_max_entries(self, fake_home):
        skill = fake_home / ".kiro" / "crew" / "skills" / "huge"
        skill.mkdir(parents=True)
        for i in range(SKILL_TREE_MAX_ENTRIES + 50):
            (skill / f"f{i:04d}.txt").write_text("x")
        out = list_skill_tree(skill)
        assert len(out) == SKILL_TREE_MAX_ENTRIES

    def test_empty_skill_dir_returns_empty(self, fake_home):
        skill = fake_home / ".kiro" / "crew" / "skills" / "empty"
        skill.mkdir(parents=True)
        assert list_skill_tree(skill) == []


# ── read_skill_file ──


class TestReadSkillFile:
    def test_reads_file_inside_skill(self, fake_home):
        skill = fake_home / ".kiro" / "crew" / "skills" / "demo"
        skill.mkdir(parents=True)
        (skill / "SKILL.md").write_text("hello\n")
        content, err = read_skill_file(skill, "SKILL.md")
        assert err is None
        assert content == "hello\n"

    def test_crlf_and_cr_are_served_as_newlines(self, fake_home):
        """The read is binary; the contract is text.

        A skill file written on Windows carries CRLF, and the viewer must get
        the same content it gets for the same file on any other host.
        """
        skill = fake_home / ".kiro" / "crew" / "skills" / "demo"
        skill.mkdir(parents=True)
        (skill / "SKILL.md").write_bytes(b"first\r\nsecond\rthird\n")
        content, err = read_skill_file(skill, "SKILL.md")
        assert err is None
        assert content == "first\nsecond\nthird\n"

    def test_rejects_path_traversal(self, fake_home):
        skill = fake_home / ".kiro" / "crew" / "skills" / "demo"
        skill.mkdir(parents=True)
        (skill / "SKILL.md").write_text("x")
        outside = fake_home / "secret.txt"
        outside.write_text("PASSWORD")
        _, err = read_skill_file(skill, "../../secret.txt")
        assert err == "invalid path"

    def test_rejects_absolute_path(self, fake_home):
        skill = fake_home / ".kiro" / "crew" / "skills" / "demo"
        skill.mkdir(parents=True)
        _, err = read_skill_file(skill, "/etc/passwd")
        assert err == "invalid path"

    def test_rejects_oversized_file(self, fake_home, monkeypatch):
        skill = fake_home / ".kiro" / "crew" / "skills" / "demo"
        skill.mkdir(parents=True)
        (skill / "big.txt").write_bytes(b"x" * (SKILL_FILE_MAX_BYTES + 1))
        _, err = read_skill_file(skill, "big.txt")
        assert err and err.startswith("file too large")

    def test_missing_file_returns_not_found(self, fake_home):
        skill = fake_home / ".kiro" / "crew" / "skills" / "demo"
        skill.mkdir(parents=True)
        _, err = read_skill_file(skill, "no-such.txt")
        assert err == "not found"

    def test_directory_target_rejected(self, fake_home):
        skill = fake_home / ".kiro" / "crew" / "skills" / "demo"
        skill.mkdir(parents=True)
        (skill / "subdir").mkdir()
        _, err = read_skill_file(skill, "subdir")
        assert err == "not a file"


# ── _resolve_skill_root ──


class TestResolveSkillRoot:
    def test_kirocrew_skill(self, fake_home):
        skill_dir = _write_skill(fake_home / ".kiro" / "crew" / "skills", "foo")
        state = MagicMock(_slots={})
        out = _resolve_skill_root("foo", state)
        assert out == skill_dir.resolve()

    def test_kiro_user_prefix(self, fake_home):
        skill_dir = _write_skill(fake_home / ".kiro" / "skills", "bar")
        state = MagicMock(_slots={})
        out = _resolve_skill_root("kiro-user/bar", state)
        assert out == skill_dir.resolve()

    def test_path_traversal_rejected(self, fake_home):
        _write_skill(fake_home / ".kiro" / "crew" / "skills", "ok")
        state = MagicMock(_slots={})
        assert _resolve_skill_root("../etc", state) is None
        assert _resolve_skill_root("/abs/path", state) is None

    def test_missing_skill_returns_none(self, fake_home):
        state = MagicMock(_slots={})
        assert _resolve_skill_root("does-not-exist", state) is None

    def test_symlinked_kiro_skill_resolves(self, fake_home, tmp_path):
        """AIM ``--local`` installs and similar manual setups symlink
        ``~/.kiro/skills/<name>`` to a directory elsewhere (commonly
        ``~/.agents/skills/<name>``).  Resolver must accept these even
        though the resolved target sits outside the kiro skills root."""
        # Real skill directory off in some other tree.
        target_dir = tmp_path / "agents-tree" / "skills" / "linked"
        target_dir.mkdir(parents=True)
        (target_dir / "SKILL.md").write_text("---\nname: linked\n---\nbody")

        # ``~/.kiro/skills/`` exists with a symlink pointing at the target.
        kiro_skills = fake_home / ".kiro" / "skills"
        kiro_skills.mkdir(parents=True)
        (kiro_skills / "linked").symlink_to(target_dir)

        state = MagicMock(_slots={})
        out = _resolve_skill_root("kiro-user/linked", state)
        assert out == target_dir.resolve()

    def test_nested_kirocrew_skill_resolves(self, fake_home):
        """Regression: category-keyed skills (``utils/multi-badger``,
        ``code/builder-toolbox``) live one level below the skills root.
        An over-strict symlink guard that required the candidate's parent
        to *be* the root 404'd every nested skill even though the GET
        ``/api/skills`` listing (via SkillsLoader) surfaced them fine."""
        skill_dir = _write_skill(fake_home / ".kiro" / "crew" / "skills", "utils/multi-badger")
        state = MagicMock(_slots={})
        out = _resolve_skill_root("utils/multi-badger", state)
        assert out == skill_dir.resolve()

    def test_nested_kiro_user_skill_resolves(self, fake_home):
        """Nesting must work for the kiro-user source too."""
        skill_dir = _write_skill(fake_home / ".kiro" / "skills", "cat/nested-one")
        state = MagicMock(_slots={})
        out = _resolve_skill_root("kiro-user/cat/nested-one", state)
        assert out == skill_dir.resolve()

    def test_kirocrew_skill_honors_kirocrew_home(self, tmp_path, monkeypatch):
        """``_resolve_skill_root`` must resolve kirocrew skills under the
        active config home (``skills_dir()``), not a hardcoded
        ``~/.kiro/crew``.  An isolated dev gateway sets KIROCREW_HOME to a
        separate directory; the tree/file endpoints must follow it."""
        home_dir = tmp_path / "real-home"
        home_dir.mkdir()
        monkeypatch.setenv("HOME", str(home_dir))
        monkeypatch.setattr(Path, "home", lambda: home_dir)

        # Isolated config home elsewhere, selected via KIROCREW_HOME.
        mc_home = tmp_path / "dev-home"
        monkeypatch.setenv("KIROCREW_HOME", str(mc_home))
        skill_dir = _write_skill(mc_home / "skills", "isolated-skill")

        state = MagicMock(_slots={})
        out = _resolve_skill_root("isolated-skill", state)
        assert out == skill_dir.resolve()
        # And nothing was created under the real ~/.kiro/crew.
        assert not (home_dir / ".kiro" / "crew" / "skills" / "isolated-skill").exists()

    def test_symlinked_intermediate_dir_escape_rejected(self, fake_home, tmp_path):
        """Security: a leaf skill symlink is allowed (AIM installs), but a
        symlinked *intermediate* directory that points outside the root
        must NOT let ``evil/skill`` escape the skills tree."""
        # Secret tree outside any skills root.
        outside = tmp_path / "outside" / "skill"
        outside.mkdir(parents=True)
        (outside / "SKILL.md").write_text("---\nname: x\n---\nsecret")

        skills_root = fake_home / ".kiro" / "crew" / "skills"
        skills_root.mkdir(parents=True)
        # ``evil`` is a symlinked intermediate dir → points at ../../outside.
        (skills_root / "evil").symlink_to(tmp_path / "outside")

        state = MagicMock(_slots={})
        # ``evil/skill`` resolves to outside/skill, whose parent (outside)
        # is not at/under the root → rejected.
        assert _resolve_skill_root("evil/skill", state) is None

    def test_aim_skill_symlink_to_sensitive_rejected(self, fake_home):
        """Security: the aim/ branch must re-check the *resolved* target, not
        just the unresolved candidate.  An AIM skill dir symlinked to a
        sensitive location must be rejected."""
        # Sensitive target with a SKILL.md inside.
        creds_dir = fake_home / ".aws"
        creds_dir.mkdir(parents=True)
        (creds_dir / "SKILL.md").write_text("---\nname: x\n---\nsecret")

        # AIM layout: ~/.aim/skills/<pkg>/<name>/SKILL.md, where <name> dir is
        # a symlink pointing into the sensitive ~/.aws directory.
        pkg_dir = fake_home / ".aim" / "skills" / "pkg"
        pkg_dir.mkdir(parents=True)
        (pkg_dir / "evil").symlink_to(creds_dir)

        state = MagicMock(_slots={})
        # candidate.parent (the symlinked ``evil`` dir) resolves to ~/.aws,
        # which is sensitive → must return None, never the credentials dir.
        assert _resolve_skill_root("package/evil", state) is None


# ── GET endpoints (integration) ──


class TestEndpoints:
    @pytest.mark.asyncio
    async def test_tree_endpoint_returns_entries(self, fake_home):
        skill_dir = _write_skill(fake_home / ".kiro" / "skills", "demo")
        (skill_dir / "helper.sh").write_text("#!/bin/sh\n")

        state = MagicMock(_slots={}, context_builder=None)
        # SkillsLoader will use ~/.kiro/crew/skills (empty here) — fine.
        async with TestClient(TestServer(_make_app(state))) as client:
            resp = await client.get("/api/skills/kiro-user/demo/-/tree")
            assert resp.status == 200
            data = await resp.json()
            paths = [e["path"] for e in data["entries"]]
            assert "SKILL.md" in paths
            assert "helper.sh" in paths
            # The absolute home path must be redacted to ``~`` — never leak
            # the server's real filesystem layout to the client.
            assert data["root"].startswith("~")
            assert str(fake_home) not in data["root"]

    @pytest.mark.asyncio
    async def test_tree_endpoint_404_for_unknown(self, fake_home):
        state = MagicMock(_slots={}, context_builder=None)
        async with TestClient(TestServer(_make_app(state))) as client:
            resp = await client.get("/api/skills/kiro-user/nope/-/tree")
            assert resp.status == 404

    @pytest.mark.asyncio
    async def test_file_endpoint_returns_content(self, fake_home):
        skill_dir = _write_skill(fake_home / ".kiro" / "skills", "demo")
        (skill_dir / "helper.sh").write_text("#!/bin/sh\necho ok\n")

        state = MagicMock(_slots={}, context_builder=None)
        async with TestClient(TestServer(_make_app(state))) as client:
            resp = await client.get("/api/skills/kiro-user/demo/-/file?path=helper.sh")
            assert resp.status == 200
            data = await resp.json()
            assert "echo ok" in data["content"]

    @pytest.mark.asyncio
    async def test_file_endpoint_400_without_path(self, fake_home):
        _write_skill(fake_home / ".kiro" / "skills", "demo")
        state = MagicMock(_slots={}, context_builder=None)
        async with TestClient(TestServer(_make_app(state))) as client:
            resp = await client.get("/api/skills/kiro-user/demo/-/file")
            assert resp.status == 400

    @pytest.mark.asyncio
    async def test_file_endpoint_400_on_traversal(self, fake_home):
        _write_skill(fake_home / ".kiro" / "skills", "demo")
        (fake_home / "secret.txt").write_text("PASSWORD")
        state = MagicMock(_slots={}, context_builder=None)
        async with TestClient(TestServer(_make_app(state))) as client:
            resp = await client.get("/api/skills/kiro-user/demo/-/file?path=../../secret.txt")
            assert resp.status == 400

    @pytest.mark.asyncio
    async def test_file_endpoint_413_for_oversized(self, fake_home):
        skill_dir = _write_skill(fake_home / ".kiro" / "skills", "demo")
        (skill_dir / "big.txt").write_bytes(b"x" * (SKILL_FILE_MAX_BYTES + 1))
        state = MagicMock(_slots={}, context_builder=None)
        async with TestClient(TestServer(_make_app(state))) as client:
            resp = await client.get("/api/skills/kiro-user/demo/-/file?path=big.txt")
            assert resp.status == 413

    @pytest.mark.asyncio
    async def test_endpoints_emit_sel_audit_events(self, fake_home, monkeypatch):
        """Tree/file access — including failed access — must emit SEL audit
        events.  Failed access (traversal/sensitive-path) is a probing signal."""
        _write_skill(fake_home / ".kiro" / "skills", "demo")
        sel_mock = MagicMock()
        monkeypatch.setattr("kiro_crew.dashboard.handlers.sel", lambda: sel_mock)

        state = MagicMock(_slots={}, context_builder=None)
        async with TestClient(TestServer(_make_app(state))) as client:
            await client.get("/api/skills/kiro-user/demo/-/tree")
            await client.get("/api/skills/kiro-user/demo/-/file?path=SKILL.md")
            await client.get("/api/skills/kiro-user/demo/-/file?path=../../secret.txt")

        # Every access logged a tool invocation.
        tools = [c.kwargs.get("tool_name") for c in sel_mock.log_tool_invocation.call_args_list]
        outcomes = [c.kwargs.get("outcome") for c in sel_mock.log_tool_invocation.call_args_list]
        assert "api_skill_tree" in tools
        assert tools.count("api_skill_file") == 2
        assert "ok" in outcomes        # successful tree + file
        assert "blocked" in outcomes   # traversal attempt audited as blocked

    @pytest.mark.asyncio
    async def test_skill_named_tree_hits_detail_not_browser(self, fake_home):
        """Route collision regression: a nested skill whose last path segment
        is literally ``tree`` (``utils/tree``) must reach the detail endpoint,
        not the tree browser.  The ``/-/`` separator keeps them distinct."""
        from kiro_crew.dashboard.handlers import (
            api_skill_detail,
            api_skill_file,
            api_skill_tree,
        )
        from kiro_crew.skills import SkillsLoader

        # A real skill literally named ``utils/tree`` under the kirocrew root.
        _write_skill(fake_home / ".kiro" / "crew" / "skills", "utils/tree", description="edge")

        app = web.Application()
        # Seed a *real* SkillsLoader so api_skill_detail can load the skill —
        # a bare MagicMock state would make ``_get_skills`` return a mock whose
        # load_skill() yields an unserializable MagicMock (500).
        state = MagicMock(_slots={}, context_builder=None)
        state._standalone_skills = SkillsLoader(install_builtins=False)
        app["state"] = state
        # Same registration order as server.py: browser routes (with /-/)
        # before the catch-all detail route.
        app.router.add_get("/api/skills/{name:.+}/-/tree", api_skill_tree)
        app.router.add_get("/api/skills/{name:.+}/-/file", api_skill_file)
        app.router.add_get("/api/skills/{name:.+}", api_skill_detail)

        async with TestClient(TestServer(app)) as client:
            # The detail endpoint for the skill named ``utils/tree``.
            resp = await client.get("/api/skills/utils/tree")
            assert resp.status == 200
            data = await resp.json()
            assert data["name"] == "utils/tree"
            assert "content" in data  # detail payload, not a tree listing

            # Its actual file browser lives under the /-/ separator.
            resp2 = await client.get("/api/skills/utils/tree/-/tree")
            assert resp2.status == 200
            assert "entries" in (await resp2.json())


class TestApiSkillsAgentScoping:
    """GET /api/skills?agent=<name> scopes the listing to that
    agent's own skill:// mapping, instead of the chat `$` picker always
    showing the unfiltered global catalog regardless of the active agent
    template."""

    @pytest.fixture(autouse=True)
    def _redirect_agents_dir(self, fake_home, monkeypatch):
        """Point agent discovery at the fixture home's agents dir.

        ``_KIRO_AGENTS_DIR`` is computed at import time from the real home, so
        this module's ``fake_home`` (which patches only ``HOME`` and
        ``Path.home``) does not redirect the default-argument lookup that
        ``agent_skill_globs`` performs — leaving it to read the operator's real
        ``~/.kiro/agents``, return ``[]``, and skip the filter entirely. Mirrors
        the same override in ``test_agent_template_skills.py``'s fixture.
        """
        monkeypatch.setattr(
            "kiro_crew.agent_discovery._KIRO_AGENTS_DIR",
            fake_home / ".kiro" / "agents",
        )

    @staticmethod
    def _state() -> MagicMock:
        from kiro_crew.skills import SkillsLoader

        # A real SkillsLoader, not a bare MagicMock: `_get_skills` treats
        # `hasattr(state, "_standalone_skills")` as "already built", but a
        # MagicMock auto-vivifies ANY attribute access as truthy, so an
        # unseeded MagicMock state silently returns a mock in place of the
        # loader — collect_skills_blocking then serializes that mock into
        # the response and 500s. Matches the pattern already used above for
        # api_skill_detail's fake state.
        state = MagicMock(_slots={}, context_builder=None)
        state._standalone_skills = SkillsLoader(install_builtins=False)
        return state

    @pytest.mark.asyncio
    async def test_agent_with_an_explicit_mapping_sees_only_its_own_skills(self, fake_home):
        _write_skill(fake_home / ".kiro" / "skills", "alpha")
        _write_skill(fake_home / ".kiro" / "skills", "beta")
        agents_dir = fake_home / ".kiro" / "agents"
        agents_dir.mkdir(parents=True)
        (agents_dir / "custom.json").write_text(json.dumps({
            "name": "custom",
            "resources": ["skill://~/.kiro/skills/alpha/SKILL.md"],
        }))

        async with TestClient(TestServer(_make_app(self._state()))) as client:
            resp = await client.get("/api/skills", params={"agent": "custom"})
            assert resp.status == 200
            payload = await resp.json()
        # an applied agent filter answers with the scoped envelope —
        # the arrays alone are byte-identical to the legacy shape, so this
        # flag is the ONLY way the picker can cue that filtering happened.
        assert payload["agent_scoped"] is True
        assert payload["agent"] == "custom"
        assert {s["name"] for s in payload["skills"]} == {"alpha"}

    @pytest.mark.asyncio
    async def test_scoped_envelope_is_kept_when_the_mapping_matches_nothing(self, fake_home):
        """An agent whose skill:// mapping resolves to zero listed
        skills still gets the envelope (``skills: []``, ``agent_scoped``
        true). This is the empty state the picker must attribute to the
        MAPPING ("no skills mapped to this agent"), not to the catalog
        ("no skills exist") — without the flag both are a bare ``[]``."""
        _write_skill(fake_home / ".kiro" / "skills", "alpha")
        agents_dir = fake_home / ".kiro" / "agents"
        agents_dir.mkdir(parents=True)
        (agents_dir / "custom.json").write_text(json.dumps({
            "name": "custom",
            "resources": ["skill://~/.kiro/skills/gamma/SKILL.md"],
        }))

        async with TestClient(TestServer(_make_app(self._state()))) as client:
            resp = await client.get("/api/skills", params={"agent": "custom"})
            assert resp.status == 200
            payload = await resp.json()
        assert payload["agent_scoped"] is True
        assert payload["agent"] == "custom"
        assert payload["skills"] == []

    @pytest.mark.asyncio
    async def test_agent_without_an_explicit_mapping_sees_everything(self, fake_home):
        """An agent with NO skill:// resources of its own (empty
        ``agent_skill_globs``) must keep the unfiltered, legacy
        all-or-nothing listing — the majority of agents that never
        customized their skill set must not lose access just because a
        DIFFERENT, customized agent exists on the same install."""
        _write_skill(fake_home / ".kiro" / "skills", "alpha")
        _write_skill(fake_home / ".kiro" / "skills", "beta")
        agents_dir = fake_home / ".kiro" / "agents"
        agents_dir.mkdir(parents=True)
        (agents_dir / "plain.json").write_text(json.dumps({"name": "plain"}))
        (agents_dir / "custom.json").write_text(json.dumps({
            "name": "custom",
            "resources": ["skill://~/.kiro/skills/alpha/SKILL.md"],
        }))

        async with TestClient(TestServer(_make_app(self._state()))) as client:
            resp = await client.get("/api/skills", params={"agent": "plain"})
            assert resp.status == 200
            payload = await resp.json()
        # No filter applied → the legacy bare-array shape, no envelope: the
        # picker must render this with zero scope cues.
        assert isinstance(payload, list)
        assert {s["name"] for s in payload} == {"alpha", "beta"}

    @pytest.mark.asyncio
    async def test_unknown_agent_name_sees_everything(self, fake_home):
        _write_skill(fake_home / ".kiro" / "skills", "alpha")
        async with TestClient(TestServer(_make_app(self._state()))) as client:
            resp = await client.get("/api/skills", params={"agent": "does-not-exist"})
            assert resp.status == 200
            payload = await resp.json()
        assert isinstance(payload, list)
        assert {s["name"] for s in payload} == {"alpha"}

    @pytest.mark.asyncio
    async def test_no_agent_param_is_unfiltered_as_before(self, fake_home):
        _write_skill(fake_home / ".kiro" / "skills", "alpha")
        _write_skill(fake_home / ".kiro" / "skills", "beta")
        agents_dir = fake_home / ".kiro" / "agents"
        agents_dir.mkdir(parents=True)
        (agents_dir / "custom.json").write_text(json.dumps({
            "name": "custom",
            "resources": ["skill://~/.kiro/skills/alpha/SKILL.md"],
        }))

        async with TestClient(TestServer(_make_app(self._state()))) as client:
            resp = await client.get("/api/skills")
            assert resp.status == 200
            payload = await resp.json()
        assert isinstance(payload, list)
        assert {s["name"] for s in payload} == {"alpha", "beta"}


class TestSessionScopedSkillResolution:
    """kiro-workspace/ resolution is scoped to the requesting chat slot.

    With two chats on DIFFERENT projects, the keyless shared-project fallback
    fails closed and workspace skills silently vanished. A session key now
    selects the requesting slot's project; keyless behavior is unchanged.
    """

    def _two_project_state(self, tmp_path: Path):
        proj_a = tmp_path / "proj-a"
        proj_b = tmp_path / "proj-b"
        _write_skill(proj_a / ".kiro" / "skills", "alpha-skill")
        state = MagicMock(
            _slots={
                "slot-a": MagicMock(project=str(proj_a)),
                "slot-b": MagicMock(project=str(proj_b)),
            }
        )
        return state, proj_a, proj_b

    def test_issue_2457_repro_two_projects_resolve_via_session_key(self, fake_home, tmp_path):
        """The exact repro from the issue, then the fix: keyed resolution works."""
        state, proj_a, _ = self._two_project_state(tmp_path)
        # Pre-existing fail-closed contract without a key: ambiguous -> None.
        assert _resolve_skill_root("kiro-workspace/alpha-skill", state) is None
        # The fix: the requesting slot's key resolves its own project's skill.
        out = _resolve_skill_root("kiro-workspace/alpha-skill", state, "slot-a")
        assert out == (proj_a / ".kiro" / "skills" / "alpha-skill").resolve()

    def test_other_slots_key_does_not_see_foreign_project_skill(self, fake_home, tmp_path):
        """Scoping is per-slot: slot B must not resolve slot A's skill."""
        state, _, _ = self._two_project_state(tmp_path)
        assert _resolve_skill_root("kiro-workspace/alpha-skill", state, "slot-b") is None

    def test_catalog_agrees_with_resolver_per_session(self, fake_home, tmp_path):
        """enumerate_skill_catalog and _resolve_skill_root must agree per key —
        an enumerated key that the resolver cannot resolve would be a phantom
        entry in the editor."""
        state, _, _ = self._two_project_state(tmp_path)
        keyed = enumerate_skill_catalog(state, "slot-a")
        assert "kiro-workspace/alpha-skill" in keyed
        other = enumerate_skill_catalog(state, "slot-b")
        assert "kiro-workspace/alpha-skill" not in other
        keyless = enumerate_skill_catalog(state)
        assert "kiro-workspace/alpha-skill" not in keyless  # ambiguous -> omitted

    def test_keyless_single_project_fallback_unchanged(self, fake_home, tmp_path):
        """The shared-project fallback (step 2) still serves keyless callers."""
        proj = tmp_path / "solo"
        _write_skill(proj / ".kiro" / "skills", "solo-skill")
        state = MagicMock(_slots={"only": MagicMock(project=str(proj))})
        out = _resolve_skill_root("kiro-workspace/solo-skill", state)
        assert out == (proj / ".kiro" / "skills" / "solo-skill").resolve()
        assert "kiro-workspace/solo-skill" in enumerate_skill_catalog(state)

    def test_session_key_does_not_widen_the_allowlist(self, fake_home, tmp_path):
        """The enumeration security boundary is untouched: with a valid key,
        traversal names still miss, and the catalog still contains only
        enumerated paths."""
        state, proj_a, _ = self._two_project_state(tmp_path)
        assert _resolve_skill_root("kiro-workspace/../secrets", state, "slot-a") is None
        assert _resolve_skill_root("kiro-workspace//abs", state, "slot-a") is None
        catalog = enumerate_skill_catalog(state, "slot-a")
        for key, path in catalog.items():
            assert ".." not in key
            assert path.name == "SKILL.md"

    def test_unknown_session_key_falls_back_not_crashes(self, fake_home, tmp_path):
        """A stale/unknown key behaves like no key (fallback chain), never raises."""
        state, _, _ = self._two_project_state(tmp_path)
        assert _resolve_skill_root("kiro-workspace/alpha-skill", state, "gone-slot") is None

    @pytest.mark.asyncio
    async def test_skill_file_endpoint_honors_x_session_key(self, fake_home, tmp_path):
        """End-to-end through the HTTP surface: the header scopes resolution."""
        state, proj_a, _ = self._two_project_state(tmp_path)
        state.context_builder = None
        async with TestClient(TestServer(_make_app(state))) as client:
            path = "/api/skills/kiro-workspace/alpha-skill/-/file?path=SKILL.md"
            keyed = await client.get(path, headers={"X-Session-Key": "slot-a"})
            assert keyed.status == 200
            keyless = await client.get(path)
            assert keyless.status == 404  # two projects, no key -> fail closed

    @pytest.mark.asyncio
    async def test_app_skill_catalog_requires_positive_slot_ownership(
        self, fake_home, tmp_path, monkeypatch
    ):
        """An app permission is not authority to select another app's project.

        The session header chooses which project contributes workspace skill
        metadata. An app caller must therefore own that exact slot; a foreign,
        unscoped, missing, or absent slot key is indistinguishable from missing.
        """
        from kiro_crew.dashboard.handlers import prompts

        project = tmp_path / "foreign-project"
        _write_skill(
            project / ".kiro" / "skills",
            "foreign-skill",
            description="foreign metadata",
        )
        state = MagicMock(
            _slots={
                "foreign": MagicMock(project=str(project), _app="app-B"),
                "unscoped": MagicMock(project=str(project), _app=""),
                "owned": MagicMock(project=str(project), _app="app-A"),
            },
            context_builder=None,
        )
        audit = MagicMock()
        monkeypatch.setattr(prompts, "_sel", lambda: audit)
        monkeypatch.setattr(
            prompts,
            "collect_skills_blocking",
            lambda _skills, _package, project_dir: [
                {"key": "kiro-workspace/foreign-skill", "project": str(project_dir)}
            ],
        )
        app = _make_app(state)

        @web.middleware
        async def inject_app(request, handler):
            request["app"] = "app-A"
            return await handler(request)

        app.middlewares.insert(0, inject_app)
        async with TestClient(TestServer(app)) as client:
            for session_key in ("foreign", "unscoped", "missing", ""):
                headers = {"X-Session-Key": session_key} if session_key else {}
                response = await client.get("/api/skills", headers=headers)
                assert response.status == 404, session_key
                assert (await response.json())["code"] == "slot_not_found"

            response = await client.get("/api/skills", headers={"X-Session-Key": "owned"})
            assert response.status == 200
            assert "kiro-workspace/foreign-skill" in {row["key"] for row in await response.json()}

        denied = [
            call
            for call in audit.log_api_access.call_args_list
            if call.kwargs.get("outcome") == "denied"
        ]
        assert len(denied) == 4
        assert all(call.kwargs["source"] == "app_isolation" for call in denied)
        allowed = [
            call
            for call in audit.log_api_access.call_args_list
            if call.kwargs.get("outcome") == "allowed"
        ]
        assert len(allowed) == 1
        assert allowed[0].kwargs == {
            "caller": "app-A",
            "operation": "skills_list",
            "outcome": "allowed",
            "source": "app_isolation",
            "resources": "slot=owned",
        }

    @pytest.mark.asyncio
    async def test_app_projectless_slot_cannot_fall_back_to_foreign_project(
        self, fake_home, tmp_path, monkeypatch
    ):
        """An owned slot without a project must not inherit another slot's project."""
        from kiro_crew.dashboard.handlers import api_skill_detail, prompts
        from kiro_crew.skills import SkillsLoader

        project = tmp_path / "foreign-project"
        _write_skill(project / ".kiro" / "skills", "foreign-skill")
        state = MagicMock(
            _slots={
                "foreign": MagicMock(project=str(project), _app="app-B"),
                "owned-projectless": MagicMock(
                    project="", project_dir="", _app="app-A"
                ),
            },
            context_builder=None,
        )
        state._standalone_skills = SkillsLoader(install_builtins=False)
        audit = MagicMock()
        monkeypatch.setattr(prompts, "_sel", lambda: audit)
        app = _make_app(state)
        app.router.add_get("/api/skills/{name:.+}", api_skill_detail)

        @web.middleware
        async def inject_app(request, handler):
            request["app"] = "app-A"
            return await handler(request)

        app.middlewares.insert(0, inject_app)
        headers = {"X-Session-Key": "owned-projectless"}
        paths = (
            "/api/skills",
            "/api/skills/kiro-workspace/foreign-skill/-/tree",
            "/api/skills/kiro-workspace/foreign-skill/-/file?path=SKILL.md",
            "/api/skills/kiro-workspace/foreign-skill",
        )
        async with TestClient(TestServer(app)) as client:
            for path in paths:
                response = await client.get(path, headers=headers)
                assert response.status == 404, path
                assert (await response.json())["code"] == "slot_not_found"

        denied = [
            call
            for call in audit.log_api_access.call_args_list
            if call.kwargs.get("outcome") == "denied"
        ]
        assert len(denied) == len(paths)
        assert all(call.kwargs["source"] == "app_isolation" for call in denied)
