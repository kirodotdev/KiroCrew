"""A fork carrying a crewmate alias is repaired during contextual binding.

A fork of a carrier-less chat can record the parent's agent NAME where the
carrier holds a kiro-cli template name, so a chat on the ``default`` crewmate
lands as ``{"selection_kind": "template", "template_id": "default"}``.
``resolve_session_agent_bindings`` launches ``template_id`` verbatim, no agent
called ``default`` exists, and the child cannot run a turn. Such a child stays
frozen because ``read_session_execution`` answers from any carrier that
decodes, so nothing looks at it again.

An explicitly scoped read rebinds that shape to the crewmate the name resolves to, on
the same ``_resolve_agent_selection`` rule the fork path and the parent's own
turns apply, and persists it once. Nothing is guessed: a deliberate template
session, an ordinary template carrier, a name that is no alias, a carrier whose
two names disagree, a restricted chat and a V2 crewmate are all left exactly as
they are.
"""

from __future__ import annotations

import asyncio
import json
import os
import threading
from pathlib import Path

import pytest

from kiro_crew import agent_discovery, hooks, memory_stores
from kiro_crew.config import loader_snapshots
from kiro_crew.config.loader import (
    KiroCrewAgentConfig,
    KiroCrewConfig,
    resolve_agent_bindings,
)
from kiro_crew.execution_context import (
    EXECUTION_CONTEXT_KEY,
    execution_from_record,
    read_session_execution,
    resolve_member_execution,
)
from kiro_crew.history import ConversationLog
from kiro_crew.session_agent_selection import resolve_session_agent_bindings

KEY = "dashboard:chat-pre-fix-fork"
PARENT_KEY = "dashboard:chat-legacy-parent"


@pytest.fixture
def template_scopes(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> tuple[Path, Path]:
    monkeypatch.setenv("KIRO_HOME", str(tmp_path / "kiro"))
    # Discovery refreshes these globals; each test owns its copy until teardown.
    for name in (
        "_MATERIALIZED_AGENTS",
        "_MATERIALIZED_STEMS",
        "_MATERIALIZED_MATE_ONLY",
        "_MATERIALIZED_AGENTS_READY",
        "_MATERIALIZED_COMPLETE",
        "_MATERIALIZED_SEEN",
        "_MATERIALIZED_LAST_DROPPED",
        "_MATERIALIZED_REFRESH_ISSUED",
        "_MATERIALIZED_REFRESH_APPLIED",
        "_MATERIALIZED_AGENTS_GENERATION",
    ):
        value = getattr(loader_snapshots, name)
        monkeypatch.setattr(
            loader_snapshots, name, value.copy() if isinstance(value, dict) else value
        )
    monkeypatch.setattr(loader_snapshots, "_SCAN_STATE", threading.local())
    monkeypatch.setattr(agent_discovery, "_PROJECT_NAMES_CACHE", {})
    user = tmp_path / "kiro" / "agents"
    project = tmp_path / "project" / ".kiro" / "agents"
    user.mkdir(parents=True)
    project.mkdir(parents=True)
    (user / "kirocrew.json").write_text(json.dumps({"name": "kirocrew"}), encoding="utf-8")
    return user, project


def _crew() -> KiroCrewConfig:
    """A home whose ``default`` alias binds the ``kirocrew`` template."""
    cfg = KiroCrewConfig.load()
    cfg.agents["default"] = KiroCrewAgentConfig(kiro_agent="kirocrew")
    cfg.agents["builder"] = KiroCrewAgentConfig(kiro_agent="amzn-builder", model="sonnet")
    cfg.save()
    return KiroCrewConfig.load()


def _carrier(**over: object) -> dict:
    """The carrier the pre-fix fork wrote for a parent on the ``default`` alias."""
    carrier = {
        "member_id": None,
        "store": {"store_id": "default", "member_id": None},
        "selection_kind": "template",
        "template_id": "default",
        "memory_mode": "persistent",
        "app": "",
        "selection_name": "",
        "selection_revision": "",
    }
    carrier.update(over)
    return carrier


def _record(
    key: str = KEY,
    *,
    agent: str = "default",
    parent_fields: dict | None = None,
    **fields: object,
) -> dict:
    parent = {
        "agent": agent,
        "memory_mode": "persistent",
        "title": "old conversation",
    }
    parent.update(parent_fields or {})
    ConversationLog().update_metadata(PARENT_KEY, parent)
    record: dict = {
        "agent": agent,
        "memory_mode": "persistent",
        "title": "\u21b3 Fork of an old conversation",
        "forked_from": PARENT_KEY,
        EXECUTION_CONTEXT_KEY: _carrier(),
    }
    record.update(fields)
    ConversationLog().update_metadata(key, record)
    return _stored(key)


def _stored(key: str = KEY) -> dict:
    record, readable = ConversationLog().get_metadata_status(key)
    assert readable
    return record


class TestThePreFixForkIsRebound:
    def test_contextual_binding_repairs_the_fork_before_dispatch(self, template_scopes) -> None:
        _user, project = template_scopes
        cfg = _crew()
        active_project = str(project.parents[1])
        _record(project=active_project)

        bindings = resolve_session_agent_bindings(
            resolve_agent_bindings, cfg, KEY, "default", active_project
        )

        assert bindings.requested_resolved
        assert bindings.selection_kind == "member"
        assert bindings.execution_context == resolve_member_execution(cfg, "default")
        assert execution_from_record(_stored()) == bindings.execution_context

    def test_a_generic_read_does_not_rewrite_the_carrier(self, template_scopes) -> None:
        _crew()
        before = _record()

        assert read_session_execution(KEY) == execution_from_record(before)
        assert read_session_execution(KEY, required=True) == execution_from_record(before)
        assert _stored()[EXECUTION_CONTEXT_KEY] == before[EXECUTION_CONTEXT_KEY]

    def test_the_active_project_must_match_the_durable_scope(self, template_scopes) -> None:
        user, project = template_scopes
        cfg = _crew()
        cfg.agents["reporter"] = KiroCrewAgentConfig(kiro_agent="kirocrew")
        cfg.save()
        old_project = user.parent.parent / "old-project"
        old_project.mkdir()
        active_project = str(project.parents[1])
        (project / "reporter.json").write_text(json.dumps({"name": "reporter"}), encoding="utf-8")
        before = _record(
            agent="reporter",
            project=str(old_project),
            **{EXECUTION_CONTEXT_KEY: _carrier(template_id="reporter")},
        )

        unscoped = resolve_session_agent_bindings(resolve_agent_bindings, cfg, KEY, "reporter")
        assert unscoped.execution_context == execution_from_record(before)

        bindings = resolve_session_agent_bindings(
            resolve_agent_bindings, cfg, KEY, "reporter", active_project
        )

        assert bindings.requested_resolved
        assert bindings.execution_context == execution_from_record(before)
        assert bindings.selection_kind == "template"
        assert read_session_execution(KEY) == execution_from_record(before)
        assert _stored()[EXECUTION_CONTEXT_KEY] == before[EXECUTION_CONTEXT_KEY]
        ConversationLog().update_metadata(KEY, {"project": active_project})

        synchronized = resolve_session_agent_bindings(
            resolve_agent_bindings, cfg, KEY, "reporter", active_project
        )

        assert synchronized.requested_resolved
        assert synchronized.execution_context == execution_from_record(before)
        assert read_session_execution(KEY) == execution_from_record(before)
        assert _stored()[EXECUTION_CONTEXT_KEY] == before[EXECUTION_CONTEXT_KEY]

    @pytest.mark.parametrize("initial_project", [None, "", "recorded"])
    @pytest.mark.parametrize("change_project", [False, True])
    @pytest.mark.parametrize("privacy", [None, "incognito", "temporary"])
    def test_project_and_privacy_are_compared_at_commit(
        self,
        template_scopes,
        monkeypatch: pytest.MonkeyPatch,
        initial_project,
        change_project,
        privacy,
    ) -> None:
        user, project = template_scopes
        cfg = _crew()
        if initial_project == "recorded":
            initial_project = str(project.parents[1])
        before = _record(project=initial_project)
        replacement = user.parent.parent / "replacement"
        replacement_agents = replacement / ".kiro" / "agents"
        replacement_agents.mkdir(parents=True)
        (replacement_agents / "default.json").write_text(
            json.dumps({"name": "default"}), encoding="utf-8"
        )
        update = ConversationLog.update_metadata_if
        commits = []

        def update_after_project_change(log, key, fields, guard, **kwargs):
            if key == KEY and EXECUTION_CONTEXT_KEY in fields:
                commits.append(key)
                if change_project:
                    log.update_metadata(key, {"project": str(replacement)})
                if privacy:
                    log.update_metadata(key, {"memory_mode": privacy})
            return update(log, key, fields, guard, **kwargs)

        monkeypatch.setattr(ConversationLog, "update_metadata_if", update_after_project_change)

        selected = read_session_execution(KEY, repair_project=initial_project)

        assert commits == [KEY]
        if change_project or privacy:
            assert selected == execution_from_record(before).with_mode(privacy or "persistent")
            assert _stored()[EXECUTION_CONTEXT_KEY] == before[EXECUTION_CONTEXT_KEY]
            assert _stored()["project"] == (str(replacement) if change_project else initial_project)
            assert read_session_execution(KEY) == selected
        else:
            assert selected == resolve_member_execution(cfg, "default")
            assert execution_from_record(_stored()) == selected
            assert _stored()["project"] == initial_project

    def test_the_read_rebinds_the_alias_and_persists_it(self) -> None:
        cfg = _crew()
        _record()

        repaired = read_session_execution(KEY, repair_project=None)

        assert repaired == resolve_member_execution(cfg, "default")
        assert repaired is not None
        assert (repaired.selection_kind, repaired.template_id) == ("member", "kirocrew")
        assert repaired.selection_name == "default"
        assert repaired.store.store_id == "default"
        # One-shot: the record now carries the rebound carrier, so the next read
        # decodes it instead of repairing anything.
        stored = _stored()
        assert execution_from_record(stored) == repaired
        assert read_session_execution(KEY) == repaired
        assert read_session_execution(KEY, required=True) == repaired
        # Nothing but the carrier is rewritten.
        assert stored["agent"] == "default"
        assert stored["title"] == "\u21b3 Fork of an old conversation"
        assert stored["forked_from"] == PARENT_KEY

    def test_the_rebound_chat_can_run_a_turn_again(self) -> None:
        """The end the user feels: the chat resolves an agent instead of refusing.

        Before the repair the stored ``template_id`` is launched verbatim, so
        the turn asks for an agent called ``default`` that does not exist.
        """
        cfg = _crew()
        _record()

        bindings = resolve_session_agent_bindings(resolve_agent_bindings, cfg, KEY, "default")
        assert bindings.requested_resolved
        assert (bindings.kiro_agent, bindings.resolved_alias) == ("kirocrew", "default")
        assert execution_from_record(_stored()) == resolve_member_execution(cfg, "default")

    def test_a_pinned_crewmate_keeps_its_model(self) -> None:
        """The rebind is the crewmate, so its template AND its pin come back."""
        cfg = _crew()
        _record(agent="builder", **{EXECUTION_CONTEXT_KEY: _carrier(template_id="builder")})

        repaired = read_session_execution(KEY, repair_project=None)

        assert repaired == resolve_member_execution(cfg, "builder")
        assert repaired is not None and repaired.template_id == "amzn-builder"
        bindings = resolve_session_agent_bindings(resolve_agent_bindings, cfg, KEY, "builder")
        assert bindings.model == "sonnet"


class TestEveryOtherShapeIsLeftAlone:
    @pytest.mark.parametrize(
        "record_fields",
        [
            # A deliberate template pick: the slot was stamped, so the carrier
            # is the truth and the matching alias name is a coincidence.
            pytest.param(
                {"parent_fields": {"agent_kind": "template"}},
                id="template-stamped-parent",
            ),
            pytest.param(
                {"forked_from": "dashboard:chat-missing-parent"},
                id="parent-is-not-readable",
            ),
            pytest.param(
                {"parent_fields": {"agent": "builder"}},
                id="parent-and-child-disagree",
            ),
            pytest.param(
                {"parent_fields": {EXECUTION_CONTEXT_KEY: _carrier()}},
                id="parent-already-has-a-carrier",
            ),
            # An ordinary template carrier records the name in both fields.
            pytest.param(
                {EXECUTION_CONTEXT_KEY: _carrier(selection_name="default")},
                id="ordinary-template-carrier",
            ),
            # The fork copied one field into the other; two different names are
            # some other history and are never reconciled by guessing.
            pytest.param(
                {"agent": "builder", EXECUTION_CONTEXT_KEY: _carrier(template_id="default")},
                id="agent-and-template-disagree",
            ),
            # Not an alias at all: a template, possibly one only a project declares.
            pytest.param(
                {
                    "agent": "project-only-template",
                    EXECUTION_CONTEXT_KEY: _carrier(template_id="project-only-template"),
                },
                id="name-is-not-an-alias",
            ),
            # Deriving a member binding is what opens a store, so a restricted
            # chat is not rewritten by a read.
            pytest.param(
                {
                    "memory_mode": "incognito",
                    EXECUTION_CONTEXT_KEY: _carrier(memory_mode="incognito"),
                },
                id="restricted-chat",
            ),
            pytest.param(
                {EXECUTION_CONTEXT_KEY: _carrier(memory_mode="incognito")},
                id="restricted-carrier-incognito",
            ),
            pytest.param(
                {EXECUTION_CONTEXT_KEY: _carrier(memory_mode="temporary")},
                id="restricted-carrier-temporary",
            ),
            pytest.param({"agent": ""}, id="no-agent-recorded"),
        ],
    )
    def test_the_carrier_is_returned_as_stored(self, record_fields: dict) -> None:
        _crew()
        before = _record(**record_fields)

        kept = read_session_execution(KEY, repair_project=before.get("project"))

        assert kept == execution_from_record(before)
        assert kept is not None and kept.selection_kind == "template"
        assert _stored()[EXECUTION_CONTEXT_KEY] == before[EXECUTION_CONTEXT_KEY]

    def test_a_v2_crewmates_private_memory_is_not_opened_by_a_read(self) -> None:
        """A read that only INFERRED the identity never reaches private memory.

        This is the refusal the fork path takes for the same parent, not an
        accident of which store the carrier happens to name.
        """
        cfg = KiroCrewConfig.load()
        cfg.agents["writer"] = KiroCrewAgentConfig()
        store = memory_stores.provision_member_memory(cfg, "writer")
        cfg.save()
        assert KiroCrewConfig.load().memory_stores[store].memory_version == 2
        before = _record(agent="writer", **{EXECUTION_CONTEXT_KEY: _carrier(template_id="writer")})

        kept = read_session_execution(KEY, repair_project=None)

        assert kept == execution_from_record(before)
        assert kept is not None and kept.store.store_id == "default"
        assert kept.member_id is None
        assert _stored()[EXECUTION_CONTEXT_KEY] == before[EXECUTION_CONTEXT_KEY]


class TestAnAliasThatIsAlsoATemplateIsLeftAlone:
    @pytest.mark.parametrize("scope", ["user", "project"])
    @pytest.mark.parametrize(
        "filename,content,keep_template",
        [
            ("._reporter.json", b'{"name":"reporter"}', True),
            ("._reporter.json", b"\x00\x05\x16\x07sidecar", False),
            ("README.md", b"# Templates\nPlain documentation.\n", False),
            ("reporter.md", b"---\nname: reporter\n", True),
        ],
    )
    def test_only_proven_non_specs_are_ignored(
        self, template_scopes, scope, filename, content, keep_template
    ) -> None:
        user, project = template_scopes
        cfg = _crew()
        cfg.agents["reporter"] = KiroCrewAgentConfig(kiro_agent="kirocrew")
        cfg.save()
        loader_snapshots.refresh_materialized_agents()
        agents = user if scope == "user" else project
        (agents / filename).write_bytes(content)
        before = _record(
            agent="reporter",
            project=str(project.parents[1]),
            **{EXECUTION_CONTEXT_KEY: _carrier(template_id="reporter")},
        )

        selected = read_session_execution(KEY, repair_project=str(project.parents[1]))

        if keep_template:
            assert selected == execution_from_record(before)
            assert _stored()[EXECUTION_CONTEXT_KEY] == before[EXECUTION_CONTEXT_KEY]
        else:
            assert selected == resolve_member_execution(cfg, "reporter")
            assert execution_from_record(_stored()) == selected

    @pytest.mark.parametrize("scope", ["user", "project"])
    @pytest.mark.parametrize("failure", ["read", "listing"])
    @pytest.mark.parametrize("declared", ["reporter", "unrelated"])
    def test_inconclusive_discovery_defers_until_a_healthy_read(
        self, template_scopes, monkeypatch: pytest.MonkeyPatch, scope, failure, declared
    ) -> None:
        user, project = template_scopes
        cfg = _crew()
        cfg.agents["reporter"] = KiroCrewAgentConfig(kiro_agent="kirocrew")
        cfg.save()
        agents = user if scope == "user" else project
        spec = agents / "package-report.json"
        spec.write_text(json.dumps({"name": declared}), encoding="utf-8")
        before = _record(
            agent="reporter",
            project=str(project.parents[1]),
            **{EXECUTION_CONTEXT_KEY: _carrier(template_id="reporter")},
        )
        attempts = []
        safe_read = hooks.safe_read_file
        read_bytes = agent_discovery._read_spec_bytes
        scandir = os.scandir

        def read_text(path, *args, **kwargs):
            if Path(path) == spec:
                attempts.append("read")
                raise PermissionError("forced template read failure")
            return safe_read(path, *args, **kwargs)

        def read_spec_bytes(path, *args, **kwargs):
            if Path(path) == spec:
                attempts.append("read")
                raise PermissionError("forced template read failure")
            return read_bytes(path, *args, **kwargs)

        def list_directory(path, *args, **kwargs):
            if path == agents or path == str(agents):
                attempts.append("listing")
                raise PermissionError("forced template listing failure")
            return scandir(path, *args, **kwargs)

        with monkeypatch.context() as failing:
            if failure == "read":
                failing.setattr(hooks, "safe_read_file", read_text)
                failing.setattr(agent_discovery, "_read_spec_bytes", read_spec_bytes)
            else:
                failing.setattr(os, "scandir", list_directory)
            loader_snapshots.refresh_materialized_agents()

            assert read_session_execution(
                KEY, repair_project=str(project.parents[1])
            ) == execution_from_record(before)
            assert _stored()[EXECUTION_CONTEXT_KEY] == before[EXECUTION_CONTEXT_KEY]
            assert failure in attempts

        # A retry uses the now-readable directory, even with the failed snapshot
        # still marked ready. Unknown discovery never becomes a permanent bind.
        kept = read_session_execution(KEY, repair_project=str(project.parents[1]))
        if declared == "reporter":
            assert kept == execution_from_record(before)
            assert _stored()[EXECUTION_CONTEXT_KEY] == before[EXECUTION_CONTEXT_KEY]
        else:
            assert kept == resolve_member_execution(cfg, "reporter")
            assert execution_from_record(_stored()) == kept

    @pytest.mark.parametrize("suffix", ["json", "md"])
    def test_a_user_template_added_after_the_snapshot_is_not_rebound(
        self, template_scopes, suffix
    ) -> None:
        user, _project = template_scopes
        cfg = _crew()
        cfg.agents["reporter"] = KiroCrewAgentConfig(kiro_agent="kirocrew")
        cfg.save()
        loader_snapshots.refresh_materialized_agents()
        assert "reporter" not in loader_snapshots._MATERIALIZED_AGENTS
        content = (
            json.dumps({"name": "reporter"}) if suffix == "json" else "---\nname: reporter\n---\n"
        )
        (user / f"package-report.{suffix}").write_text(content, encoding="utf-8")
        before = _record(
            agent="reporter", **{EXECUTION_CONTEXT_KEY: _carrier(template_id="reporter")}
        )

        assert read_session_execution(KEY, repair_project=None) == execution_from_record(before)
        assert _stored()[EXECUTION_CONTEXT_KEY] == before[EXECUTION_CONTEXT_KEY]

    def test_a_cold_project_template_is_not_rebound_on_the_event_loop(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        cfg = KiroCrewConfig.load()
        cfg.agents["reporter"] = KiroCrewAgentConfig(kiro_agent="reporter")
        cfg.save()
        agents = tmp_path / ".kiro" / "agents"
        agents.mkdir(parents=True)
        (agents / "reporter.json").write_text(json.dumps({"name": "reporter"}), encoding="utf-8")
        before = _record(
            agent="reporter",
            project=str(tmp_path),
            **{EXECUTION_CONTEXT_KEY: _carrier(template_id="reporter")},
        )
        monkeypatch.setattr(loader_snapshots, "_MATERIALIZED_AGENTS", frozenset())
        monkeypatch.setattr(loader_snapshots, "_MATERIALIZED_AGENTS_READY", False)

        async def read_on_loop():
            return read_session_execution(KEY, repair_project=str(tmp_path))

        kept = asyncio.run(read_on_loop())

        assert kept == execution_from_record(before)
        assert _stored()[EXECUTION_CONTEXT_KEY] == before[EXECUTION_CONTEXT_KEY]
        # An off-loop reader can inspect the real project template and also
        # leaves it alone, rather than rewriting it as the same-named member.
        assert read_session_execution(KEY, repair_project=str(tmp_path)) == kept

    def test_a_name_that_still_names_a_materialized_template_is_not_rebound(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """A legitimate template fork writes the same shape as the broken one.

        When the recorded name resolves to a materialized kiro-cli template, a
        template fork of it produces an identical carrier and an identical
        unstamped parent. The alias lookup alone cannot prove the original
        selection was the member, so the carrier is left as stored and the
        template chat keeps running its template.
        """
        cfg = KiroCrewConfig.load()
        cfg.agents["reporter"] = KiroCrewAgentConfig(kiro_agent="reporter")
        cfg.save()
        monkeypatch.setattr(loader_snapshots, "_MATERIALIZED_AGENTS", frozenset({"reporter"}))
        monkeypatch.setattr(loader_snapshots, "_MATERIALIZED_AGENTS_READY", True)
        before = _record(
            agent="reporter", **{EXECUTION_CONTEXT_KEY: _carrier(template_id="reporter")}
        )

        kept = read_session_execution(KEY, repair_project=None)

        assert kept == execution_from_record(before)
        assert kept is not None and kept.selection_kind == "template"
        assert _stored()[EXECUTION_CONTEXT_KEY] == before[EXECUTION_CONTEXT_KEY]

    def test_default_is_still_rebound_because_no_template_shadows_it(self) -> None:
        """The main case is unaffected: no materialized template shadows it."""
        cfg = _crew()
        _record()

        repaired = read_session_execution(KEY, repair_project=None)

        assert repaired == resolve_member_execution(cfg, "default")
        assert repaired is not None
        assert (repaired.selection_kind, repaired.template_id) == ("member", "kirocrew")
