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
import os
import time
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
    agent_skill_keys,
    agent_unmanaged_skill_uris,
    apply_skill_mapping,
    enumerate_skill_catalog,
    skill_key_for_uri,
    skill_uri_for_key,
    walk_skill_catalog,
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


def _settle(root: Path, age_s: float = 10.0) -> None:
    """Backdate every directory at or under *root* past the catalog's settle window.

    ``SkillCatalogSnapshot.changed`` treats a directory modified within that window
    of the walk as changed on purpose -- an equal mtime cannot prove nothing landed
    in the same clock tick -- so a test that asserts "nothing changed" must first
    put the tree at rest, as a real skill tree is between installs.
    """
    stamp = time.time() - age_s
    for d in [root, *(p for p in root.rglob("*") if p.is_dir())]:
        os.utime(d, (stamp, stamp))


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
    def test_global_spec_relative_resource_uses_the_session_working_directory(self, tmp_path):
        path = tmp_path / "home" / ".kiro" / "agents" / "custom.json"
        project = tmp_path / "project"
        assert expand_skill_uri(
            "skill://.kiro/skills/*/SKILL.md", path, project_dir=project
        ) == str(project / ".kiro/skills/*/SKILL.md")

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
    def test_missing_custom_template_cannot_fall_back_to_global_skills(self, fake_home):
        with pytest.raises(ValueError, match="Cannot resolve skill scope"):
            agent_skill_globs("removed", agents_dir=_agents_dir(fake_home), strict=True)

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


class TestSkillCatalogSnapshot:
    """``changed()`` is the PATCH receipt's licence to skip the second walk."""

    def test_a_tree_at_rest_is_unchanged(self, fake_home):
        root = fake_home / ".kiro" / "skills"
        _make_skill(root, "one")
        _make_skill(root / "utils", "tiny-url")
        _settle(root)

        snapshot = walk_skill_catalog(_State())

        assert set(snapshot.entries) == {"kiro-user/one", "kiro-user/utils/tiny-url"}
        assert snapshot.changed() is False

    def test_a_skill_installed_inside_a_category_is_a_change(self, fake_home):
        """A root's own mtime does not move for a nested install; the category's does."""
        root = fake_home / ".kiro" / "skills"
        _make_skill(root / "utils", "tiny-url")
        _settle(root)
        snapshot = walk_skill_catalog(_State())
        assert snapshot.changed() is False

        _make_skill(root / "utils", "short-url")

        assert snapshot.changed() is True

    def test_a_root_that_appears_is_a_change(self, fake_home, tmp_path, monkeypatch):
        """A root absent at walk time is recorded as absent, so its creation is seen."""
        root = fake_home / ".kiro" / "skills"
        _make_skill(root, "one")
        _settle(root)
        project = tmp_path / "proj"
        project.mkdir()
        state = _State(project)
        snapshot = walk_skill_catalog(state)
        assert (project / ".kiro" / "skills") in snapshot.dir_mtimes
        assert snapshot.changed() is False

        _make_skill(project / ".kiro" / "skills", "local")

        assert snapshot.changed() is True

    def test_a_tree_touched_within_the_settle_window_is_not_trusted(self, fake_home):
        """Coarse mtimes: a change in the tick the walk recorded leaves the mtime equal.

        So a directory modified within ``_CATALOG_SETTLE_NS`` of the walk reads as
        changed even when its mtime has not moved -- the price is a second walk after
        a fresh install, never a receipt built on a snapshot a same-tick install may
        have slipped past.
        """
        root = fake_home / ".kiro" / "skills"
        _make_skill(root, "one")

        snapshot = walk_skill_catalog(_State())

        assert snapshot.changed() is True


class TestSkillCatalogWalkBench:
    """The measurement behind the re-check stays runnable against the code it measures.

    ``test/skill_catalog_walk_bench.py`` is what puts numbers on "a second walk is
    dearer than the re-check"; a script nothing runs breaks silently the day the walk's
    signature or the root resolver moves. Tiny tree, two runs, no duration asserted.
    """

    _PINNED = ("HOME", "USERPROFILE", "KIROCREW_HOME")

    def test_the_benchmark_builds_its_tree_and_prints_the_table(self, capsys):
        import skill_catalog_walk_bench as bench

        # The script pins HOME, USERPROFILE and KIROCREW_HOME at its temporary
        # tree for the run and must hand this process its own back afterwards.
        before = {name: os.environ.get(name) for name in self._PINNED}

        assert bench.main(["--skills", "5", "--runs", "2"]) == 0

        assert {name: os.environ.get(name) for name in self._PINNED} == before
        out = capsys.readouterr().out
        assert "tree: 5 skills over 3 roots (kiro-user/, kiro-workspace/, data-home)" in out
        rows = [line.split("|")[1].strip() for line in out.splitlines() if line.startswith("| ")]
        assert rows == [
            "Measured",
            "a: one full walk (`walk_skill_catalog`)",
            "b: `changed()` re-check, roots at rest",
            "a+b: receipt with the re-check (paired)",
        ], out
        assert "2a: receipt with an unconditional second walk (paired): median " in out

    def test_a_root_outside_the_temporary_tree_is_refused_before_any_write(
        self, tmp_path, monkeypatch, capsys
    ):
        """A home the pins did not move is refused, not built into.

        The Windows shape: ``Path.home()`` reads ``USERPROFILE`` there and never
        ``HOME``, so a script that pinned HOME alone resolved ``kiro-user/`` to the
        runner's real profile, wrote its skills into it, and then found the tree
        "not at rest" because nothing backdated them. Simulated on every platform
        by a ``Path.home()`` the environment does not move: the run must stop with
        the escaping root named and the profile untouched.
        """
        import skill_catalog_walk_bench as bench

        real_profile = tmp_path / "real-profile"
        real_profile.mkdir()
        monkeypatch.setattr(Path, "home", classmethod(lambda cls: real_profile))

        assert bench.main(["--skills", "5", "--runs", "2"]) == 2

        out = capsys.readouterr().out
        assert "refusing to build outside the temporary tree" in out
        assert str(real_profile / ".kiro" / "skills") in out
        assert not (real_profile / ".kiro").exists()


class TestApplySkillMapping:
    def test_writes_uris_and_preserves_file_resources(self, fake_home):
        _make_skill(fake_home / ".kiro" / "skills", "one")
        _make_skill(fake_home / ".kiro" / "skills", "two")
        state = _State()
        agent = _agents_dir(fake_home) / "a.json"
        data = {"name": "a", "resources": ["file://.kiro/steering/**/*.md"]}

        applied, unknown, uris, snapshot = apply_skill_mapping(
            data, agent, state, ["kiro-user/one", "kiro-user/two"]
        )

        assert unknown == []
        assert applied == ["kiro-user/one", "kiro-user/two"]
        assert uris == [
            "skill://~/.kiro/skills/one/SKILL.md",
            "skill://~/.kiro/skills/two/SKILL.md",
        ]
        assert data["resources"] == [
            "file://.kiro/steering/**/*.md",
            "skill://~/.kiro/skills/one/SKILL.md",
            "skill://~/.kiro/skills/two/SKILL.md",
        ]
        assert agent_skill_keys(data, agent, state) == ["kiro-user/one", "kiro-user/two"]
        # The returned walk resolves the written spec without another enumeration.
        assert snapshot.entries == enumerate_skill_catalog(state)
        assert agent_skill_keys(data, agent, state, catalog=snapshot.entries) == [
            "kiro-user/one",
            "kiro-user/two",
        ]

    def test_unknown_key_rejects_whole_request_without_mutating(self, fake_home):
        _make_skill(fake_home / ".kiro" / "skills", "one")
        state = _State()
        agent = _agents_dir(fake_home) / "a.json"
        data = {"resources": ["file://keep.md"]}

        applied, unknown, _uris, _snapshot = apply_skill_mapping(
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

        applied, _, _, _ = apply_skill_mapping(
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


class _RefreshingState(_State):
    """``_State`` plus the refresh hook a successful PATCH calls on its way out."""

    def push_refresh(self, kind: str) -> None:
        pass


class TestPatchReordersManagedSkills:
    """A ``skills`` PATCH that permutes the mapped skills must persist that order.

    ``apply_skill_mapping`` rebuilds ``resources`` as every non-managed entry first,
    then the managed ``skill://`` URIs in request order. So the list it hands the
    locked merge differs from the persisted one both when the caller reordered the
    skills AND when the author merely interleaved a ``file://`` glob (or a
    hand-written wildcard) between two skills. The merge has to tell those apart:
    honour the first, leave the second byte-for-byte alone.
    """

    ONE = "skill://~/.kiro/skills/one/SKILL.md"
    TWO = "skill://~/.kiro/skills/two/SKILL.md"
    THREE = "skill://~/.kiro/skills/three/SKILL.md"

    def _agent(self, fake_home: Path, monkeypatch, resources: list) -> Path:
        from kiro_crew.dashboard.handlers import agents as agents_handlers

        _make_skill(fake_home / ".kiro" / "skills", "one")
        _make_skill(fake_home / ".kiro" / "skills", "two")
        d = _agents_dir(fake_home)
        cfg = d / "victim.json"
        # The handler's own serialisation (``json.dump(..., indent=2)`` plus a newline),
        # so a byte comparison after the PATCH measures the resources merge alone.
        cfg.write_text(
            json.dumps({"name": "victim", "resources": resources}, indent=2) + "\n",
            encoding="utf-8",
        )
        monkeypatch.setattr(agents_handlers, "KIRO_AGENTS_DIR", d, raising=False)
        monkeypatch.setattr("kiro_crew.agent.KIRO_AGENTS_DIR", d, raising=False)
        return cfg

    @staticmethod
    def _patch(skills: list[str]):
        import asyncio

        from kiro_crew.dashboard.handlers import agents as agents_handlers

        request = _FakeRequest("PATCH", {"name": "victim"}, {"skills": skills}, _RefreshingState())
        resp = asyncio.run(agents_handlers.api_agent_detail(request))
        assert resp.status == 200, resp.text
        return json.loads(resp.text)

    def test_a_pure_reorder_persists_and_the_response_reports_the_persisted_order(
        self, fake_home, monkeypatch
    ):
        """Same members, new order: the spec must carry the new order, not just the reply.

        The membership delta of a permutation is empty, so a merge that moves only what
        was added or removed keeps the persisted order -- while the handler answers
        ``ok`` with the order the caller asked for, so nothing tells the caller the
        write was discarded.
        """
        cfg = self._agent(
            fake_home, monkeypatch, ["file://.kiro/steering/**/*.md", self.ONE, self.TWO]
        )

        body = self._patch(["kiro-user/two", "kiro-user/one"])

        landed = json.loads(cfg.read_text(encoding="utf-8"))["resources"]
        assert landed == [
            "file://.kiro/steering/**/*.md",
            self.TWO,
            self.ONE,
        ], f"the reorder was not persisted: {landed!r}"
        assert body["skills"] == ["kiro-user/two", "kiro-user/one"]

    def test_naming_the_current_skills_in_their_current_order_keeps_an_interleaved_layout(
        self, fake_home, monkeypatch
    ):
        """A no-op PATCH over an interleaved spec must stay a no-op, byte for byte.

        The mapping hoists the ``file://`` glob and the hand-written wildcard ahead of
        both skills, so the list it produces differs from the persisted one although the
        caller changed nothing. A merge that re-applied that whole list would rewrite
        the author's layout on every PATCH that so much as mentions the existing skills.
        """
        cfg = self._agent(
            fake_home,
            monkeypatch,
            [
                self.ONE,
                "file://.kiro/steering/**/*.md",
                "skill://~/.kiro/skills/*/SKILL.md",
                self.TWO,
            ],
        )
        before = cfg.read_bytes()

        body = self._patch(["kiro-user/one", "kiro-user/two"])

        assert cfg.read_bytes() == before, cfg.read_text(encoding="utf-8")
        assert body["skills"] == ["kiro-user/one", "kiro-user/two"]

    def test_a_reorder_never_resurrects_a_uri_a_concurrent_writer_removed(
        self, fake_home, monkeypatch
    ):
        """The requested order applies to the URIs the fresh read still carries, only.

        The order was computed against a snapshot taken before the spec lock. A URI a
        co-owner unmapped in between is gone from the locked read and must stay gone;
        the reply then reports what was persisted, so the caller can see the difference.

        The removed skill is named FIRST in a three-skill reorder on purpose: a merge
        that paired the requested order with the merged list's slots without dropping
        the absent URI would write it back into slot 0 and push the last carried URI
        off the end -- named last, the absent URI would fall off the pairing unseen.
        """
        from kiro_crew.dashboard.handlers import agents as agents_handlers

        cfg = self._agent(fake_home, monkeypatch, [self.ONE, self.TWO, self.THREE])
        _make_skill(fake_home / ".kiro" / "skills", "three")
        real_read = agents_handlers._read_agent_spec
        calls = {"n": 0}

        def racing_read(path, **kwargs):
            calls["n"] += 1
            result = real_read(path, **kwargs)
            # After the pre-lock re-read, a co-owner unmaps ``one``. The locked read
            # that follows sees the removal; this writer's snapshot never did.
            if calls["n"] == 2:
                cfg.write_text(json.dumps({"name": "victim", "resources": [self.TWO, self.THREE]}))
            return result

        monkeypatch.setattr(agents_handlers, "_read_agent_spec", racing_read)

        body = self._patch(["kiro-user/one", "kiro-user/three", "kiro-user/two"])

        landed = json.loads(cfg.read_text(encoding="utf-8"))["resources"]
        assert landed == [
            self.THREE,
            self.TWO,
        ], f"a concurrently removed URI came back or a carried one was lost: {landed!r}"
        assert body["skills"] == ["kiro-user/three", "kiro-user/two"], body

    def test_a_skill_a_concurrent_writer_added_is_kept_and_reported(self, fake_home, monkeypatch):
        """The reply lists every skill the WRITTEN spec maps, not only the ones requested.

        A co-owner maps a third skill between this writer's snapshot and the locked
        read. The merge keeps it -- it is not a URI this request removed -- so the reply
        has to carry it as well: the skills editor takes the reply as its next state,
        and a reply missing the addition would have the editor's next toggle unmap it.
        """
        from kiro_crew.dashboard.handlers import agents as agents_handlers

        cfg = self._agent(fake_home, monkeypatch, [self.ONE, self.TWO])
        _make_skill(fake_home / ".kiro" / "skills", "three")
        real_read = agents_handlers._read_agent_spec
        calls = {"n": 0}

        def racing_read(path, **kwargs):
            calls["n"] += 1
            result = real_read(path, **kwargs)
            # After the pre-lock re-read, a co-owner maps ``three``.
            if calls["n"] == 2:
                cfg.write_text(
                    json.dumps({"name": "victim", "resources": [self.ONE, self.TWO, self.THREE]})
                )
            return result

        monkeypatch.setattr(agents_handlers, "_read_agent_spec", racing_read)

        body = self._patch(["kiro-user/two", "kiro-user/one"])

        landed = json.loads(cfg.read_text(encoding="utf-8"))["resources"]
        assert landed == [self.TWO, self.ONE, self.THREE], landed
        assert body["skills"] == [
            "kiro-user/two",
            "kiro-user/one",
            "kiro-user/three",
        ], f"the reply dropped a skill the written spec maps: {body!r}"

    def test_a_skills_patch_walks_the_skill_roots_once(self, fake_home, monkeypatch):
        """One PATCH, one catalog walk: the reply reuses the walk that validated the keys.

        ``enumerate_skill_catalog`` caches nothing and walks every skill root. The
        mapping walks them to validate the keys; a reply resolved by a second walk over
        the written spec doubles the filesystem cost of every interactive chip toggle,
        and that walk is hundreds of milliseconds on a 200-skill tree against about one
        for the re-check (``test/skill_catalog_walk_bench.py``). With the roots at rest
        (see ``_settle``) the re-check after the write is a stat per directory and no
        walk. The reply is still the persisted order, so the single walk cannot be
        bought by echoing the request.
        """
        cfg = self._agent(fake_home, monkeypatch, [self.ONE, self.TWO])
        _settle(fake_home / ".kiro" / "skills")
        walks = self._count_walks(monkeypatch)

        body = self._patch(["kiro-user/two", "kiro-user/one"])

        assert walks["n"] == 1, f"a skills PATCH walked the skill roots {walks['n']} times"
        landed = json.loads(cfg.read_text(encoding="utf-8"))["resources"]
        assert landed == [self.TWO, self.ONE], landed
        assert body["skills"] == ["kiro-user/two", "kiro-user/one"], body

    def test_a_skill_installed_and_mapped_after_the_mapping_walk_is_reported(
        self, fake_home, monkeypatch
    ):
        """A skill that did not exist when the mapping walked must still reach the receipt.

        A co-owner installs ``three`` on disk AND maps it after the mapping validated the
        keys but before the locked read. The merge keeps the URI, and the mapping's
        catalog cannot name it -- so the reply must notice the roots moved and walk them
        again, or the skills editor's next toggle unmaps a skill the written spec carries.
        Two walks here, and only here.
        """
        from kiro_crew.dashboard.handlers import agents as agents_handlers

        cfg = self._agent(fake_home, monkeypatch, [self.ONE, self.TWO])
        _settle(fake_home / ".kiro" / "skills")
        real_read = agents_handlers._read_agent_spec
        calls = {"n": 0}

        def racing_read(path, **kwargs):
            calls["n"] += 1
            result = real_read(path, **kwargs)
            # Reads, in order: the candidate scan, the pre-lock re-read, the scan
            # repeated under the spec lock, the locked read the merge applies to.
            # The mapping's walk sits between the second and the third, so a skill
            # installed here is one that walk never saw.
            if calls["n"] == 3:
                _make_skill(fake_home / ".kiro" / "skills", "three")
                cfg.write_text(
                    json.dumps({"name": "victim", "resources": [self.ONE, self.TWO, self.THREE]})
                )
            return result

        monkeypatch.setattr(agents_handlers, "_read_agent_spec", racing_read)
        walks = self._count_walks(monkeypatch)

        body = self._patch(["kiro-user/two", "kiro-user/one"])

        assert calls["n"] == 4, calls
        landed = json.loads(cfg.read_text(encoding="utf-8"))["resources"]
        assert landed == [self.TWO, self.ONE, self.THREE], landed
        assert body["skills"] == [
            "kiro-user/two",
            "kiro-user/one",
            "kiro-user/three",
        ], f"the reply dropped a skill installed after the mapping's walk: {body!r}"
        assert walks["n"] == 2, f"the roots moved but were walked {walks['n']} times"

    @staticmethod
    def _count_walks(monkeypatch) -> dict[str, int]:
        """Count every walk of the skill roots, whichever caller asks for it."""
        from kiro_crew.dashboard.handlers import _shared

        real_walk = _shared.walk_skill_catalog
        walks = {"n": 0}

        def counting_walk(state, session_key=""):
            walks["n"] += 1
            return real_walk(state, session_key)

        monkeypatch.setattr(_shared, "walk_skill_catalog", counting_walk)
        return walks


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

    def test_mapped_agent_on_kiro_gets_scoped_discovery(self, fake_home):
        """Native startup uses the Crew directory and loads bodies on demand."""
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
        assert "skill_search" in ctx
        assert "alpha" in ctx
        assert "Body of alpha" not in ctx

    def test_unmapped_custom_agent_still_gets_nothing(self, fake_home):
        skills_root = fake_home / "skills"
        _make_skill(skills_root, "alpha")
        d = _agents_dir(fake_home)
        (d / "plain.json").write_text(json.dumps({"name": "plain"}), encoding="utf-8")

        ctx = self._builder(fake_home, skills_root).build_session_context(
            agent="plain", provider_type="claude_code"
        )
        assert "[Skills:]" not in ctx
        from kiro_crew.agent_discovery import session_skill_globs

        assert session_skill_globs("", "plain") == []
        assert session_skill_globs("", "kirocrew") is None

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

    def test_mapped_kirocrew_on_kiro_gets_scoped_discovery(self, fake_home):
        """A mapped default agent gets only its scoped discovery directory."""
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
        assert "skill_search" in ctx
        assert "alpha" in ctx and "beta" not in ctx
        assert "Body of alpha" not in ctx

    def test_unmapped_kirocrew_gets_short_discovery(self, fake_home):
        skills_root = fake_home / "skills"
        _make_skill(skills_root, "alpha")
        _make_skill(skills_root, "beta")
        _agents_dir(fake_home)

        ctx = self._builder(fake_home, skills_root).build_session_context(
            agent="kirocrew", provider_type="claude_code"
        )
        # The default entry is the bounded usage-ranked index; an unmapped agent
        # gets it rather than a full catalog dump.
        assert "## Available Skills" in ctx
        assert "alpha" in ctx and "beta" in ctx
