"""Tests for mapping skills to agent templates via ``skill://`` resources.

Three layers, matching the three holes this feature closes:

* READ — ``agent_discovery`` derives an agent's skills from its ``skill://``
  resources.
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

from conftest import requires_symlinks
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
    agent_skill_views,
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
def _owner_caller(monkeypatch):
    """Run as the dashboard owner: these tests exercise handler behavior PAST
    the owner boundary on the agents module's mutating endpoints, which has
    its own enumerate-the-invariant coverage in
    test_agents_endpoints_owner_auth.py."""
    monkeypatch.setattr(
        "kiro_crew.dashboard.handlers.source_providers.is_owner_dashboard_request",
        lambda request: True,
    )


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
    monkeypatch.setattr("kiro_crew.agent_discovery._KIRO_AGENTS_DIR", tmp_path / ".kiro" / "agents")
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
        assert expand_skill_uri("skill://~/.kiro/skills/foo/SKILL.md", fake_home / "a.json") == str(
            fake_home / ".kiro/skills/foo/SKILL.md"
        )

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

    @requires_symlinks
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
        assert agent_skill_views(data, agent, state)[0] == ["kiro-user/one", "kiro-user/two"]

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

    def test_a_write_omitting_stale_keys_keeps_their_mappings(self, fake_home):
        """Omitting a mapping the catalog cannot resolve preserves it instead of deleting it.

        This is the property the editor depends on to stay usable: submitting a list that
        still contains an unresolvable key is refused WHOLE, so every edit on the agent is
        blocked until it is left out -- and leaving it out must not be a silent delete.
        """
        _make_skill(fake_home / ".kiro" / "skills", "one")
        state = _State()
        agent = _agents_dir(fake_home) / "a.json"
        stale_a = "skill://~/.kiro/skills/stale-a/SKILL.md"
        stale_b = "skill://~/.kiro/skills/stale-b/SKILL.md"
        data = {"resources": [stale_a, stale_b, "skill://~/.kiro/skills/one/SKILL.md"]}

        applied, unknown = apply_skill_mapping(data, agent, state, ["kiro-user/one"])

        assert unknown == [], "a submission carrying only resolvable keys must be accepted"
        assert applied == ["kiro-user/one"]
        assert stale_a in data["resources"], "an omitted unresolvable mapping was deleted"
        assert stale_b in data["resources"], "an omitted unresolvable mapping was deleted"
        # Non-vacuity: these are genuinely unresolvable, not merely absent from the catalog
        # walk, so the assertions above are about the preserve path and not a no-op write.
        assert agent_skill_views(data, agent, state)[0] == ["kiro-user/one"]

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

        applied, _ = apply_skill_mapping(data, agent, state, ["kiro-user/one", "kiro-user/one"])

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


class _PatchState(_State):
    """``_State`` plus the refresh hook only a SUCCEEDING write reaches.

    Every other PATCH test in this file is refused before the write, so the bare stub does
    not need it; a test asserting the success response is the one that gets there.
    """

    def push_refresh(self, *_a, **_k) -> None:
        return None


class TestSkillsPatchResponseCarriesBothViews:
    def test_a_preserved_unmanaged_uri_comes_back_on_the_patch_response(
        self, fake_home, monkeypatch
    ):
        """A removal must not report success over a mapping the write KEPT.

        ``apply_skill_mapping`` preserves a ``skill://`` URI it cannot key — a hand-authored
        glob is that case — so the mapping survives the PATCH while being absent from the
        applied list. A response carrying only the applied list therefore tells the caller
        the removal happened, and the editor renders no locked chip until an unrelated
        refetch. Both views are recomputed from one catalog walk so they cannot disagree.
        """
        import asyncio

        from kiro_crew.dashboard.handlers import agents as agents_handlers

        d = _agents_dir(fake_home)
        glob_uri = "skill://~/.kiro/skills/*/SKILL.md"
        (d / "keeper.json").write_text(
            json.dumps({"name": "keeper", "resources": [glob_uri]}), encoding="utf-8"
        )
        monkeypatch.setattr("kiro_crew.agent.KIRO_AGENTS_DIR", d, raising=False)

        request = _FakeRequest("PATCH", {"name": "keeper"}, {"skills": []}, _PatchState())
        resp = asyncio.run(agents_handlers.api_agent_detail(request))

        assert resp.status == 200, resp.body
        body = json.loads(resp.body)
        assert "unmanaged_skills" in body, "the PATCH response omits the surviving-URI view"
        assert glob_uri in body["unmanaged_skills"], body
        assert "skills" in body, "the applied view must ship alongside it, from the same walk"


class _FakeRequest:
    """Minimal aiohttp Request stand-in for api_agent_detail."""

    def __init__(self, method: str, match_info: dict, body: dict, state: object):
        self.method = method
        self.match_info = match_info
        self._body = body
        self.app = {"state": state}
        self.query: dict[str, str] = {}
        # api_agent_detail reads X-Session-Key via _read_session_key(request)
        # to scope the skill catalog to the requesting slot.
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
        """A custom agent with a mapping gets exactly the mapped set on the CC
        backend (which does not read agent ``resources``)."""
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


def test_a_named_removal_survives_a_write_that_also_maps_a_key(tmp_path, monkeypatch):
    """Naming a URI deletes it even while the same write maps a resolvable key.

    Removal and mapping travel in ONE request, so a removal must not be lost when the write
    also has keys to apply -- that would leave the ✕ looking effective and changing nothing.
    """
    from kiro_crew.dashboard.handlers import _shared as _sh

    gone = "skill://uninstalled-package/vanished"
    monkeypatch.setattr(_sh, "enumerate_skill_catalog", lambda *_a, **_k: {})
    monkeypatch.setattr(_sh, "skill_key_for_uri", lambda *_a, **_k: None)

    data = {"resources": [gone]}
    _sh.apply_skill_mapping(data, tmp_path / "a.json", None, [], "", [gone])
    assert gone not in (data.get("resources") or []), "a named removal was preserved anyway"


def test_a_write_that_omits_an_unmanaged_uri_leaves_it_alone(tmp_path, monkeypatch):
    """A writer holding stale state must not delete what it has never seen.

    Two owners edit one agent: the second adds an unmanaged URI, then the first writes from
    a view predating it. Inferring removal from absence makes that write destroy the new
    mapping silently, with manual re-add the only recovery -- so only a NAMED URI is deleted.
    """
    from kiro_crew.dashboard.handlers import _shared as _sh

    mine = "skill://uninstalled-package/mine"
    theirs = "skill://uninstalled-package/added-by-a-co-owner"

    monkeypatch.setattr(_sh, "enumerate_skill_catalog", lambda *_a, **_k: {})
    monkeypatch.setattr(_sh, "skill_key_for_uri", lambda *_a, **_k: None)

    stale = {"resources": [mine, theirs]}
    _sh.apply_skill_mapping(stale, tmp_path / "a.json", None, [], "", [])
    assert theirs in (
        stale.get("resources") or []
    ), "a stale writer deleted a co-owner's URI it never named"
    assert mine in (stale.get("resources") or [])

    named = {"resources": [mine, theirs]}
    _sh.apply_skill_mapping(named, tmp_path / "a.json", None, [], "", [mine])
    assert named.get("resources") == [theirs], "a named removal did not take effect"


def test_a_malformed_removal_list_is_refused_rather_than_coerced():
    """A coerced element names no URI, so the request becomes a no-op read as success."""
    from kiro_crew.dashboard.handlers.agents import (
        _LIST_ARG_INVALID,
        _string_list_arg,
    )

    assert _string_list_arg({}, "removed_unmanaged_skills") is None
    assert _string_list_arg(
        {"removed_unmanaged_skills": ["skill://a"]}, "removed_unmanaged_skills"
    ) == ["skill://a"]
    for bad in ([123], ["skill://a", None], [{"uri": "skill://a"}], "skill://a", {}):
        assert (
            _string_list_arg({"removed_unmanaged_skills": bad}, "removed_unmanaged_skills")
            is _LIST_ARG_INVALID
        ), f"malformed removal list was accepted: {bad!r}"


def test_a_uri_reclassified_between_load_and_write_is_not_deleted(tmp_path, monkeypatch):
    """Becoming catalog-resolvable is an install, not a removal.

    A hand-authored ``skill://`` the editor listed as unmanaged can be recognised by the
    catalogue before the write lands -- installing the package it already names does that.
    Reading the new classification as a deletion loses the binding with nothing having named
    it, and no automatic path restores it.
    """
    from kiro_crew.dashboard.handlers import _shared as _sh

    reclassified = "skill://~/.kiro/skills/newly-installed/SKILL.md"

    monkeypatch.setattr(_sh, "enumerate_skill_catalog", lambda *_a, **_k: {})
    # Resolvable NOW, which is exactly the mid-flight install this guards.
    monkeypatch.setattr(_sh, "skill_key_for_uri", lambda *_a, **_k: "newly-installed")

    data = {"resources": [reclassified]}
    _sh.apply_skill_mapping(data, tmp_path / "a.json", None, [], "", None, [reclassified])
    assert reclassified in (
        data.get("resources") or []
    ), "a URI reclassified between load and write was deleted without being named"

    # Managed all along, chip removed: the submitted keys still decide, so it goes.
    managed = {"resources": [reclassified]}
    _sh.apply_skill_mapping(managed, tmp_path / "a.json", None, [], "", None, [])
    assert reclassified not in (
        managed.get("resources") or []
    ), "removing a managed mapping by omitting its key stopped working"


def test_a_named_removal_outranks_a_mid_flight_reclassification(tmp_path, monkeypatch):
    """Naming a URI for removal must delete it even if it just became resolvable.

    Reclassification protects a URI the caller never asked to remove. Letting it also protect
    one the caller DID name turns an explicit delete into a success that deleted nothing, with
    no error surface to tell the user their removal was ignored.
    """
    from kiro_crew.dashboard.handlers import _shared as _sh

    named = "skill://~/.kiro/skills/newly-installed/SKILL.md"

    monkeypatch.setattr(_sh, "enumerate_skill_catalog", lambda *_a, **_k: {})
    # Resolvable NOW -- the reclassification the other guard exists for.
    monkeypatch.setattr(_sh, "skill_key_for_uri", lambda *_a, **_k: "newly-installed")

    data = {"resources": [named]}
    _sh.apply_skill_mapping(data, tmp_path / "a.json", None, [], "", [named], [named])
    assert named not in (
        data.get("resources") or []
    ), "a named removal was ignored because the URI had become resolvable"

    # And the protection still holds for a URI the caller did NOT name.
    kept = {"resources": [named]}
    _sh.apply_skill_mapping(kept, tmp_path / "a.json", None, [], "", [], [named])
    assert named in (kept.get("resources") or []), "an unnamed reclassified URI lost its protection"


def test_a_removal_only_write_keeps_a_mapping_a_concurrent_session_added(tmp_path, monkeypatch):
    """A write that states no managed set must not delete one.

    Removing a hand-authored URI names only what it removes. Were the client's managed keys
    resubmitted, a co-owner could map B between that client reading them and the write landing:
    the spec re-read under lock carries B, the submission does not, and the overwrite drops it.
    """
    from kiro_crew.dashboard.handlers import _shared as sh

    spec = tmp_path / "agent.json"
    spec.write_text("{}", encoding="utf-8")
    managed_a = "skill://managed-a"
    managed_b = "skill://managed-b"
    hand_authored = "skill://local/hand/notes"

    # As the spec reads on disk under the lock: A and B mapped, plus the hand-authored URI.
    data = {"resources": [managed_a, managed_b, hand_authored, "file://steering/*.md"]}

    # Both managed URIs resolve; the hand-authored one does not.
    monkeypatch.setattr(
        sh,
        "skill_key_for_uri",
        lambda uri, *a, **k: "key" if uri in (managed_a, managed_b) else None,
    )
    monkeypatch.setattr(sh, "enumerate_skill_catalog", lambda *a, **k: {})

    sh.apply_skill_mapping(
        data,
        spec,
        object(),
        None,  # states no managed set: this write only removes
        "",
        [hand_authored],
        [hand_authored],
    )

    resources = data.get("resources") or []
    assert hand_authored not in resources, "the named removal did not happen"
    assert (
        managed_b in resources
    ), f"a mapping the client never saw was deleted by a removal-only write: {resources}"
    assert managed_a in resources, f"an existing mapping was dropped: {resources}"
    assert "file://steering/*.md" in resources, f"a non-skill resource was touched: {resources}"


def test_a_removal_only_write_keeps_a_mapping_added_after_its_own_reread(tmp_path, monkeypatch):
    """A write may only move the URIs it named, even under the spec lock.

    The managed set is read before the lock and the spec is re-read inside it, so a mapping
    a co-owner adds in between is present in the fresh document and absent from this
    writer's copy. Assigning that copy whole deletes it, and the response is recomputed from
    what was written -- so nothing surfaces the loss.
    """
    from kiro_crew.dashboard.handlers import agents as _agents

    concurrent = "skill://concurrently-added"
    stale = {"resources": ["skill://managed-a", "skill://hand/authored"]}
    fresh = {"resources": ["skill://managed-a", "skill://hand/authored", concurrent]}

    # What the removal produced from the PRE-LOCK snapshot: the hand-authored URI gone.
    after = {"resources": ["skill://managed-a"]}

    _agents._merge_resources_delta(fresh, stale, after)

    assert (
        concurrent in fresh["resources"]
    ), f"a mapping added after this writer's reread was deleted: {fresh['resources']}"
    assert "skill://hand/authored" not in fresh["resources"], "the named removal did not happen"
    assert "skill://managed-a" in fresh["resources"], "an untouched mapping was dropped"
