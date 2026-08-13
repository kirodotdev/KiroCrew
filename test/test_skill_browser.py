"""Tests for the skill directory browser API.

Covers:
- ``list_kiro_skills`` discovery of ``~/.kiro/skills/`` and workspace ``.kiro/skills/``
- ``_resolve_loaded_by_agents`` glob-matching against installed agent JSONs
- ``list_skill_tree`` / ``read_skill_file`` size + sensitive-path + escape guards
- ``_resolve_skill_root`` cross-source resolution (kirocrew / kiro-user / aim)
- ``GET /api/skills/<name>/tree`` and ``GET /api/skills/<name>/file`` end-to-end

Tests use a tmp_path fake $HOME so we never touch the real filesystem.
"""
from __future__ import annotations

import json
from pathlib import Path
from unittest.mock import MagicMock

import pytest
from aiohttp import web
from aiohttp.test_utils import TestClient, TestServer

from kiro_crew.dashboard.handlers._shared import (
    SKILL_FILE_MAX_BYTES,
    SKILL_TREE_MAX_ENTRIES,
    _agent_loads_skill,
    _expand_agent_globs,
    _expand_resource_uri,
    _load_parsed_agents,
    _parse_skill_description,
    _resolve_loaded_by_agents,
    _resolve_skill_root,
    annotate_skills_with_agents,
    collect_skills_blocking,
    enumerate_skill_catalog,
    list_kiro_skills,
    list_skill_tree,
    read_skill_file,
)

# ── Fixtures ──


@pytest.fixture
def fake_home(tmp_path, monkeypatch):
    """Pin $HOME to tmp_path so Path.home() returns a writable sandbox.

    Also clears KIROCREW_HOME so ``skills_dir()`` resolves to
    ``<tmp>/.kiro/crew/skills`` rather than any value leaked from the
    surrounding build environment.
    """
    monkeypatch.setenv("HOME", str(tmp_path))
    monkeypatch.delenv("KIROCREW_HOME", raising=False)
    monkeypatch.setattr(Path, "home", lambda: tmp_path)
    return tmp_path


def _write_skill(root: Path, name: str, *, description: str = "", body: str = "body") -> Path:
    """Materialize a SKILL.md under root/<name>/SKILL.md."""
    skill_dir = root / name
    skill_dir.mkdir(parents=True, exist_ok=True)
    fm = ["---", f"name: {name}"]
    if description:
        fm.append(f"description: {description}")
    fm.append("---")
    skill_dir.joinpath("SKILL.md").write_text("\n".join(fm) + f"\n{body}\n")
    return skill_dir


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

    def test_handles_non_string_resources(self, tmp_path):
        skill_md = tmp_path / "SKILL.md"
        agent_json = {"name": "x", "resources": [{"oops": "object"}, None, "skill://*/SKILL.md"]}
        # Should not crash on garbage; should still match the valid glob
        # (matches anything ending in /SKILL.md so depending on path).
        skill_md.write_text("")
        # Returns True/False — just checks no exception.
        _agent_loads_skill(agent_json, tmp_path / "a.json", skill_md)

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


def _make_app(state):
    from kiro_crew.dashboard.handlers import api_skill_file, api_skill_tree, api_skills

    app = web.Application()
    app["state"] = state
    app.router.add_get("/api/skills", api_skills)
    # Route order matters — specific routes must come before {name:.+}.
    # ``/-/`` separator avoids collision with skills named ``.../tree``.
    app.router.add_get("/api/skills/{name:.+}/-/tree", api_skill_tree)
    app.router.add_get("/api/skills/{name:.+}/-/file", api_skill_file)
    return app


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


class TestSessionScopedSkillResolution:
    """#2457: kiro-workspace/ resolution is scoped to the requesting chat slot.

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
