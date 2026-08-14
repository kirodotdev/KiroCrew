"""Tests for mapping skills to agent templates via ``skill://`` resources.

Three layers, matching the three holes this feature closes:

* READ — ``agent_discovery`` derives an agent's skills from its ``skill://``
  resources (previously only ``builder-mcp --skill-name-filter`` was parsed, so
  every ordinary agent reported zero skills).
* WRITE — ``_shared.apply_skill_mapping`` turns catalog keys into ``skill://``
  resources without disturbing ``file://`` steering globs or hand-authored URIs.
* RUNTIME — ``SkillsLoader.get_context(only=…)`` and the ``build_session_context``
  gate narrow the injected skills block to the mapping.

Every test uses a tmp_path fake ``$HOME`` so the real filesystem is untouched.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from kiro_crew.agent_discovery import (
    _extract_skills,
    agent_skill_globs,
    clear_list_agents_cache,
    expand_skill_uri,
    list_agents,
    skill_resource_uris,
)
from kiro_crew.context import ContextBuilder
from kiro_crew.dashboard.handlers._shared import (
    agent_skill_keys,
    agent_unmanaged_skill_uris,
    apply_skill_mapping,
    enumerate_skill_catalog,
    skill_key_for_uri,
    skill_uri_for_key,
)
from kiro_crew.learn import LessonStore
from kiro_crew.memory import MemoryStore
from kiro_crew.skills import SkillsLoader


@pytest.fixture(autouse=True)
def _no_agent_cache():
    clear_list_agents_cache()
    yield
    clear_list_agents_cache()


@pytest.fixture
def fake_home(tmp_path, monkeypatch):
    monkeypatch.setenv("HOME", str(tmp_path))
    monkeypatch.setattr(Path, "home", lambda: tmp_path)
    # ``_KIRO_AGENTS_DIR`` is computed at import time from the real home, so the
    # Path.home patch alone does not redirect the default-argument lookups that
    # agent_skill_globs / list_agents use.
    monkeypatch.setattr(
        "kiro_crew.agent_discovery._KIRO_AGENTS_DIR", tmp_path / ".kiro" / "agents"
    )
    return tmp_path


def _agents_dir(home: Path) -> Path:
    d = home / ".kiro" / "agents"
    d.mkdir(parents=True, exist_ok=True)
    return d


def _make_skill(root: Path, name: str, *, always: bool = False, desc: str = "") -> Path:
    d = root / name
    d.mkdir(parents=True, exist_ok=True)
    md = d / "SKILL.md"
    front = f"---\nname: {name}\ndescription: {desc or name + ' skill'}\n"
    if always:
        front += "always: true\n"
    front += "---\n\nBody of " + name + "\n"
    md.write_text(front, encoding="utf-8")
    return md


class _Slot:
    def __init__(self, project: Path | None = None):
        self.project = str(project) if project else ""
        self.total_messages = 1
        self.workspace = "default"


class _State:
    """Minimal DashboardState stand-in for the skill-root resolvers."""

    def __init__(self, project: Path | None = None):
        self._slots = {"chat-1": _Slot(project)}


# ── READ: skill:// resources become the agent's skill list ──


class TestExtractSkills:
    def test_skill_uris_become_skill_names(self):
        data = {
            "resources": [
                "file://.kiro/steering/**/*.md",
                "skill://~/.kiro/skills/babysit/SKILL.md",
                "skill://~/.kiro/skills/prepare-pr/SKILL.md",
            ]
        }
        assert _extract_skills(data) == ["babysit", "prepare-pr"]
        assert skill_resource_uris(data) == [
            "skill://~/.kiro/skills/babysit/SKILL.md",
            "skill://~/.kiro/skills/prepare-pr/SKILL.md",
        ]

    def test_unions_builder_mcp_filter_without_duplicates(self):
        """Both mapping mechanisms are honored, and an overlap collapses."""
        data = {
            "resources": ["skill://~/.kiro/skills/babysit/SKILL.md"],
            "mcpServers": {
                "builder-mcp": {"args": ["--skill-name-filter", "babysit,other"]},
            },
        }
        assert _extract_skills(data) == ["babysit", "other"]

    def test_wildcard_pattern_is_surfaced_not_dropped(self):
        data = {"resources": ["skill://~/.kiro/skills/*/SKILL.md"]}
        assert _extract_skills(data) == ["*"]

    def test_no_resources_is_empty(self):
        assert _extract_skills({"name": "plain"}) == []
        assert _extract_skills({"resources": "not-a-list"}) == []

    def test_list_agents_reports_mapped_skills(self, fake_home):
        d = _agents_dir(fake_home)
        (d / "specialist.json").write_text(
            json.dumps(
                {
                    "name": "specialist",
                    "resources": ["skill://~/.kiro/skills/babysit/SKILL.md"],
                }
            ),
            encoding="utf-8",
        )
        agents = {a.name: a for a in list_agents(agents_dir=d)}
        assert agents["specialist"].skills == ["babysit"]


class TestExpandSkillUri:
    def test_home_relative(self, fake_home):
        assert expand_skill_uri(
            "skill://~/.kiro/skills/foo/SKILL.md", fake_home / "a.json"
        ) == str(fake_home / ".kiro/skills/foo/SKILL.md")

    def test_absolute(self, tmp_path):
        assert (
            expand_skill_uri("skill:///opt/skills/foo/SKILL.md", tmp_path / "a.json")
            == "/opt/skills/foo/SKILL.md"
        )

    def test_workspace_relative_resolves_to_project_root(self, tmp_path):
        agent_path = tmp_path / "proj" / ".kiro" / "agents" / "a.json"
        got = expand_skill_uri("skill://.kiro/skills/foo/SKILL.md", agent_path)
        assert got == str(tmp_path / "proj" / ".kiro" / "skills" / "foo" / "SKILL.md")

    def test_non_skill_uri_is_none(self, tmp_path):
        assert expand_skill_uri("file://x.md", tmp_path / "a.json") is None


class TestAgentSkillGlobs:
    def test_returns_expanded_globs_for_mapped_agent(self, fake_home):
        d = _agents_dir(fake_home)
        (d / "mapped.json").write_text(
            json.dumps(
                {
                    "name": "mapped",
                    "resources": [
                        "file://.kiro/steering/**/*.md",
                        "skill://~/.kiro/skills/foo/SKILL.md",
                    ],
                }
            ),
            encoding="utf-8",
        )
        assert agent_skill_globs("mapped", agents_dir=d) == [
            str(fake_home / ".kiro/skills/foo/SKILL.md")
        ]

    def test_unmapped_and_missing_agents_are_empty(self, fake_home):
        d = _agents_dir(fake_home)
        (d / "plain.json").write_text(json.dumps({"name": "plain"}), encoding="utf-8")
        assert agent_skill_globs("plain", agents_dir=d) == []
        assert agent_skill_globs("nope", agents_dir=d) == []
        assert agent_skill_globs("", agents_dir=d) == []


# ── WRITE: catalog keys <-> skill:// resources ──


class TestSkillKeyRoundTrip:
    def test_kiro_user_key_round_trips(self, fake_home):
        _make_skill(fake_home / ".kiro" / "skills", "babysit")
        state = _State()
        uri = skill_uri_for_key("kiro-user/babysit", state)
        assert uri == "skill://~/.kiro/skills/babysit/SKILL.md"
        assert skill_key_for_uri(uri, _agents_dir(fake_home) / "a.json", state) == (
            "kiro-user/babysit"
        )

    def test_unknown_key_is_none(self, fake_home):
        assert skill_uri_for_key("kiro-user/ghost", _State()) is None

    def test_traversal_key_is_rejected(self, fake_home):
        _make_skill(fake_home / ".kiro" / "skills", "babysit")
        assert skill_uri_for_key("kiro-user/../../.ssh", _State()) is None
        assert skill_uri_for_key("/etc/passwd", _State()) is None

    def test_wildcard_uri_has_no_key(self, fake_home):
        state = _State()
        agent = _agents_dir(fake_home) / "a.json"
        assert skill_key_for_uri("skill://~/.kiro/skills/*/SKILL.md", agent, state) is None

    def test_foreign_path_has_no_key(self, fake_home):
        state = _State()
        agent = _agents_dir(fake_home) / "a.json"
        assert skill_key_for_uri("skill:///opt/elsewhere/foo/SKILL.md", agent, state) is None

    def test_nested_category_key_round_trips(self, fake_home):
        """Skills may live under a category dir (``utils/tiny-url``); the
        enumeration walk must key them by their full relative path."""
        _make_skill(fake_home / ".kiro" / "skills", "utils/tiny-url")
        state = _State()
        uri = skill_uri_for_key("kiro-user/utils/tiny-url", state)
        assert uri == "skill://~/.kiro/skills/utils/tiny-url/SKILL.md"
        assert (
            skill_key_for_uri(uri, _agents_dir(fake_home) / "a.json", state)
            == "kiro-user/utils/tiny-url"
        )

    def test_symlinked_skill_dir_inverts_to_the_same_key(self, fake_home):
        """An AIM ``--local`` install symlinks ``~/.kiro/skills/<name>`` to a
        directory elsewhere. The written URI and its inversion must agree, or the
        mapping would show up as unmanaged the moment the agent is reopened."""
        real = fake_home / "elsewhere" / "linked-skill"
        _make_skill(fake_home / "elsewhere", "linked-skill")
        link_root = fake_home / ".kiro" / "skills"
        link_root.mkdir(parents=True, exist_ok=True)
        (link_root / "linked-skill").symlink_to(real, target_is_directory=True)

        state = _State()
        agent = _agents_dir(fake_home) / "a.json"
        uri = skill_uri_for_key("kiro-user/linked-skill", state)
        assert uri == "skill://~/.kiro/skills/linked-skill/SKILL.md"
        assert skill_key_for_uri(uri, agent, state) == "kiro-user/linked-skill"
        # A URI written against the symlink TARGET inverts to the same key.
        target_uri = f"skill://{(real / 'SKILL.md').as_posix()}"
        assert skill_key_for_uri(target_uri, agent, state) == "kiro-user/linked-skill"


class TestEnumerateSkillCatalog:
    """The catalog is an allowlist built by enumeration, never by joining a
    caller-supplied string onto a root."""

    def test_only_enumerated_paths_are_reachable(self, fake_home):
        _make_skill(fake_home / ".kiro" / "skills", "real")
        secret = fake_home / ".ssh"
        secret.mkdir(parents=True, exist_ok=True)
        (secret / "SKILL.md").write_text("---\nname: evil\n---\n", encoding="utf-8")

        catalog = enumerate_skill_catalog(_State())

        assert catalog["kiro-user/real"] == fake_home / ".kiro/skills/real/SKILL.md"
        # No key can name anything the walk did not discover — including via
        # traversal, an absolute path, or a ~ prefix.
        for hostile in (
            "kiro-user/../../.ssh",
            "../../.ssh",
            "/etc",
            "~/.ssh",
            "kiro-user/real/../../../.ssh",
        ):
            assert hostile not in catalog
            assert skill_uri_for_key(hostile, _State()) is None

    def test_skill_without_skill_md_is_not_a_key(self, fake_home):
        d = fake_home / ".kiro" / "skills" / "empty-dir"
        d.mkdir(parents=True)
        assert "kiro-user/empty-dir" not in enumerate_skill_catalog(_State())

    def test_sensitive_root_is_skipped(self, fake_home, monkeypatch):
        """A skill root that resolves into a credential tree contributes nothing,
        even if it holds a well-formed SKILL.md."""
        creds = fake_home / ".aws"
        _make_skill(creds, "looks-legit")
        monkeypatch.setattr(
            "kiro_crew.dashboard.handlers._shared._skill_key_roots",
            lambda state, session_key="": [("kiro-user/", creds)],
        )
        assert enumerate_skill_catalog(_State()) == {}


class TestApplySkillMapping:
    def test_writes_uris_and_preserves_file_resources(self, fake_home):
        _make_skill(fake_home / ".kiro" / "skills", "one")
        _make_skill(fake_home / ".kiro" / "skills", "two")
        state = _State()
        agent = _agents_dir(fake_home) / "a.json"
        data = {"name": "a", "resources": ["file://.kiro/steering/**/*.md"]}

        applied, unknown = apply_skill_mapping(
            data, agent, state, ["kiro-user/one", "kiro-user/two"]
        )

        assert unknown == []
        assert applied == ["kiro-user/one", "kiro-user/two"]
        assert data["resources"] == [
            "file://.kiro/steering/**/*.md",
            "skill://~/.kiro/skills/one/SKILL.md",
            "skill://~/.kiro/skills/two/SKILL.md",
        ]
        assert agent_skill_keys(data, agent, state) == ["kiro-user/one", "kiro-user/two"]

    def test_unknown_key_rejects_whole_request_without_mutating(self, fake_home):
        _make_skill(fake_home / ".kiro" / "skills", "one")
        state = _State()
        agent = _agents_dir(fake_home) / "a.json"
        data = {"resources": ["file://keep.md"]}

        applied, unknown = apply_skill_mapping(
            data, agent, state, ["kiro-user/one", "kiro-user/ghost"]
        )

        assert unknown == ["kiro-user/ghost"]
        assert applied == ["kiro-user/one"]
        # Nothing written: a typo must not partially apply.
        assert data["resources"] == ["file://keep.md"]

    def test_removal_replaces_the_managed_set(self, fake_home):
        _make_skill(fake_home / ".kiro" / "skills", "one")
        _make_skill(fake_home / ".kiro" / "skills", "two")
        state = _State()
        agent = _agents_dir(fake_home) / "a.json"
        data = {
            "resources": [
                "skill://~/.kiro/skills/one/SKILL.md",
                "skill://~/.kiro/skills/two/SKILL.md",
            ]
        }

        apply_skill_mapping(data, agent, state, ["kiro-user/two"])

        assert data["resources"] == ["skill://~/.kiro/skills/two/SKILL.md"]

    def test_clearing_all_skills_drops_the_key(self, fake_home):
        _make_skill(fake_home / ".kiro" / "skills", "one")
        state = _State()
        agent = _agents_dir(fake_home) / "a.json"
        data = {"resources": ["skill://~/.kiro/skills/one/SKILL.md"]}

        apply_skill_mapping(data, agent, state, [])

        # Absent (not []) so _refresh_dynamic_fields re-seeds the shipped
        # steering defaults instead of treating [] as a deliberate opt-out.
        assert "resources" not in data

    def test_unmanaged_uris_survive_an_edit(self, fake_home):
        _make_skill(fake_home / ".kiro" / "skills", "one")
        state = _State()
        agent = _agents_dir(fake_home) / "a.json"
        data = {
            "resources": [
                "skill://~/.kiro/skills/*/SKILL.md",
                "skill:///opt/elsewhere/x/SKILL.md",
            ]
        }
        assert agent_unmanaged_skill_uris(data, agent, state) == data["resources"]

        apply_skill_mapping(data, agent, state, ["kiro-user/one"])

        assert data["resources"] == [
            "skill://~/.kiro/skills/*/SKILL.md",
            "skill:///opt/elsewhere/x/SKILL.md",
            "skill://~/.kiro/skills/one/SKILL.md",
        ]

    def test_duplicate_keys_collapse(self, fake_home):
        _make_skill(fake_home / ".kiro" / "skills", "one")
        state = _State()
        agent = _agents_dir(fake_home) / "a.json"
        data: dict = {}

        applied, _ = apply_skill_mapping(
            data, agent, state, ["kiro-user/one", "kiro-user/one"]
        )

        assert applied == ["kiro-user/one"]
        assert data["resources"] == ["skill://~/.kiro/skills/one/SKILL.md"]


# ── HANDLER: a rejected PATCH must not mutate anything ──


class TestPatchRejectionLeavesStateIntact:
    """A combined ``{model, skills}`` PATCH with a bad skill key is rejected as a
    whole. Before the ordering fix the model branch ran first, so the sidecar was
    already written when the 400 returned — freezing an unchanged model against
    future shipped-default bumps."""

    def test_unknown_skill_does_not_freeze_the_model(self, fake_home, monkeypatch):
        import asyncio

        from kiro_crew import agent_state
        from kiro_crew.dashboard.handlers import agents as agents_handlers

        _make_skill(fake_home / ".kiro" / "skills", "one")
        d = _agents_dir(fake_home)
        spec = {"name": "victim", "model": "claude-opus-4.8"}
        (d / "victim.json").write_text(json.dumps(spec), encoding="utf-8")

        monkeypatch.setattr(agents_handlers, "KIRO_AGENTS_DIR", d, raising=False)
        monkeypatch.setattr("kiro_crew.agent.KIRO_AGENTS_DIR", d, raising=False)

        managed_calls: list[tuple[str, bool]] = []
        monkeypatch.setattr(
            agent_state,
            "set_model_managed",
            lambda n, v: managed_calls.append((n, v)),
        )

        state = _State()
        request = _FakeRequest(
            "PATCH",
            {"name": "victim"},
            {"model": "claude-sonnet-4.5", "skills": ["kiro-user/ghost"]},
            state,
        )
        resp = asyncio.run(agents_handlers.api_agent_detail(request))

        assert resp.status == 400
        # No sidecar write, and the spec on disk is untouched.
        assert managed_calls == []
        assert json.loads((d / "victim.json").read_text()) == spec

    def test_non_object_body_is_rejected_not_a_500(self, fake_home, monkeypatch):
        """A top-level JSON array makes ``"skills" in patch_body`` a LIST
        membership test — true for ``["skills"]`` — and the subscript that
        follows raised TypeError, surfacing as HTTP 500."""
        import asyncio

        from kiro_crew.dashboard.handlers import agents as agents_handlers

        d = _agents_dir(fake_home)
        (d / "victim.json").write_text(json.dumps({"name": "victim"}), encoding="utf-8")
        monkeypatch.setattr("kiro_crew.agent.KIRO_AGENTS_DIR", d, raising=False)

        for body in (["skills"], "skills", 42):
            request = _FakeRequest("PATCH", {"name": "victim"}, body, _State())
            resp = asyncio.run(agents_handlers.api_agent_detail(request))
            assert resp.status == 400, f"body {body!r} should be rejected, not 500"


class TestExtraSkillPathsAreAbsolute:
    def test_relative_extra_path_is_made_absolute(self, fake_home, monkeypatch):
        """A relative ``skills.extra_paths`` entry would key the catalog by a
        relative root, so the persisted ``skill://`` URI would resolve against
        whatever cwd the next session starts in."""
        from kiro_crew.dashboard.handlers._shared import _skill_key_roots

        class _Cfg:
            class skills:
                extra_paths = ["relative/skills"]

        monkeypatch.setattr(
            "kiro_crew.dashboard.handlers._shared.KiroCrewConfig",
            type("C", (), {"load": staticmethod(lambda: _Cfg)}),
        )
        roots = [root for _, root in _skill_key_roots(_State())]
        assert all(r.is_absolute() for r in roots), [str(r) for r in roots]


class _FakeRequest:
    """Minimal aiohttp Request stand-in for api_agent_detail."""

    def __init__(self, method: str, match_info: dict, body: dict, state: object):
        self.method = method
        self.match_info = match_info
        self._body = body
        self.app = {"state": state}
        self.query: dict[str, str] = {}
        # api_agent_detail reads X-Session-Key via _read_session_key(request)
        # to scope the skill catalog to the requesting slot (#2457).
        self.headers: dict[str, str] = {}

    async def json(self):
        return self._body


# ── RUNTIME: the injected block honors the mapping ──


class TestSkillsLoaderOnlyFilter:
    def _loader(self, tmp_path: Path) -> tuple[SkillsLoader, Path]:
        root = tmp_path / "skills"
        _make_skill(root, "alpha")
        _make_skill(root, "beta")
        return SkillsLoader(skills_path=root, install_builtins=False), root

    def test_only_narrows_the_block(self, tmp_path):
        loader, root = self._loader(tmp_path)
        ctx = loader.get_context(budget=8000, only=[str(root / "alpha" / "SKILL.md")])
        assert "alpha" in ctx
        assert "beta" not in ctx

    def test_only_matching_nothing_yields_empty(self, tmp_path):
        """A mapping pointing at a deleted skill must NOT fall back to the whole
        catalog — that would silently re-grant everything."""
        loader, _ = self._loader(tmp_path)
        assert loader.get_context(budget=8000, only=["/nowhere/*/SKILL.md"]) == ""

    def test_none_is_unchanged_full_catalog(self, tmp_path):
        loader, _ = self._loader(tmp_path)
        ctx = loader.get_context(budget=8000)
        assert "alpha" in ctx and "beta" in ctx

    def test_pinned_skill_outside_mapping_is_not_force_injected(self, tmp_path):
        """``always: true`` pins a skill for the default (unmapped) path; it must
        not override an explicit mapping, or the mapping would not bound the
        agent's skills."""
        root = tmp_path / "skills"
        _make_skill(root, "alpha")
        _make_skill(root, "pinned", always=True)
        loader = SkillsLoader(skills_path=root, install_builtins=False)

        # Legacy (budget=None) path — full content for always-skills.
        unrestricted = loader.get_context()
        assert "Body of pinned" in unrestricted

        restricted = loader.get_context(only=[str(root / "alpha" / "SKILL.md")])
        assert "alpha" in restricted
        assert "Body of pinned" not in restricted


class TestSessionContextGate:
    def _builder(self, tmp_path: Path, skills_root: Path) -> ContextBuilder:
        return ContextBuilder(
            memory=MemoryStore(workspace=tmp_path / "ws"),
            skills=SkillsLoader(skills_path=skills_root, install_builtins=False),
            lessons=LessonStore(base_dir=tmp_path),
        )

    def test_mapped_custom_agent_gets_its_skills_on_cc(self, fake_home):
        """Previously a custom agent got NO skills at all. With a mapping it now
        gets exactly the mapped set on the CC backend (which does not read agent
        ``resources``)."""
        skills_root = fake_home / "skills"
        _make_skill(skills_root, "alpha")
        _make_skill(skills_root, "beta")
        d = _agents_dir(fake_home)
        (d / "specialist.json").write_text(
            json.dumps(
                {
                    "name": "specialist",
                    "resources": [f"skill://{(skills_root / 'alpha' / 'SKILL.md').as_posix()}"],
                }
            ),
            encoding="utf-8",
        )

        ctx = self._builder(fake_home, skills_root).build_session_context(
            agent="specialist", provider_type="claude_code"
        )
        assert "alpha" in ctx
        assert "beta" not in ctx

    def test_mapped_agent_on_kiro_defers_to_native_resource_load(self, fake_home):
        """kiro-cli loads ``skill://`` resources itself when spawned with
        ``--agent``, so injecting them again would duplicate every SKILL.md."""
        skills_root = fake_home / "skills"
        _make_skill(skills_root, "alpha")
        d = _agents_dir(fake_home)
        (d / "specialist.json").write_text(
            json.dumps(
                {
                    "name": "specialist",
                    "resources": [f"skill://{(skills_root / 'alpha' / 'SKILL.md').as_posix()}"],
                }
            ),
            encoding="utf-8",
        )

        ctx = self._builder(fake_home, skills_root).build_session_context(
            agent="specialist", provider_type="acp"
        )
        assert "[Skills:]" not in ctx

    def test_unmapped_custom_agent_still_gets_nothing(self, fake_home):
        skills_root = fake_home / "skills"
        _make_skill(skills_root, "alpha")
        d = _agents_dir(fake_home)
        (d / "plain.json").write_text(json.dumps({"name": "plain"}), encoding="utf-8")

        ctx = self._builder(fake_home, skills_root).build_session_context(
            agent="plain", provider_type="claude_code"
        )
        assert "[Skills:]" not in ctx

    def test_mapped_kirocrew_is_scoped_not_full_catalog(self, fake_home):
        """The mapping bounds the kirocrew agent too: before this feature it
        always received the entire catalog."""
        skills_root = fake_home / "skills"
        _make_skill(skills_root, "alpha")
        _make_skill(skills_root, "beta")
        d = _agents_dir(fake_home)
        (d / "kirocrew.json").write_text(
            json.dumps(
                {
                    "name": "kirocrew",
                    "resources": [f"skill://{(skills_root / 'alpha' / 'SKILL.md').as_posix()}"],
                }
            ),
            encoding="utf-8",
        )

        ctx = self._builder(fake_home, skills_root).build_session_context(
            agent="kirocrew", provider_type="claude_code"
        )
        assert "alpha" in ctx
        assert "beta" not in ctx

    def test_mapped_kirocrew_on_kiro_defers_to_native_load(self, fake_home):
        """On the kiro backend the mapped SKILL.md files are loaded by kiro-cli
        from ``resources``, so KiroCrew must not inject them a second time."""
        skills_root = fake_home / "skills"
        _make_skill(skills_root, "alpha")
        _make_skill(skills_root, "beta")
        d = _agents_dir(fake_home)
        (d / "kirocrew.json").write_text(
            json.dumps(
                {
                    "name": "kirocrew",
                    "resources": [f"skill://{(skills_root / 'alpha' / 'SKILL.md').as_posix()}"],
                }
            ),
            encoding="utf-8",
        )

        ctx = self._builder(fake_home, skills_root).build_session_context(
            agent="kirocrew", provider_type="acp"
        )
        assert "[Skills:]" not in ctx

    def test_unmapped_kirocrew_still_gets_everything(self, fake_home):
        skills_root = fake_home / "skills"
        _make_skill(skills_root, "alpha")
        _make_skill(skills_root, "beta")
        _agents_dir(fake_home)

        ctx = self._builder(fake_home, skills_root).build_session_context(
            agent="kirocrew", provider_type="claude_code"
        )
        assert "alpha" in ctx and "beta" in ctx
