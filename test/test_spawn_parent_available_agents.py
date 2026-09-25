"""The parent agent's ``toolsSettings.subagent.availableAgents`` gates ``spawn_run``.

kiro-cli defines that key for its own built-in ``subagent`` tool: a glob list
of the agents THIS agent may spawn, and omitting it allows all. Kiro Crew's
sub-agents come through ``spawn_run`` / ``spawn_sub_agents`` instead, whose
admission only ever asked "does the target exist" plus Kiro Crew's own
governance -- so an operator who declared the allowlist on their agent spec
saw it silently ignored and the spawn tool advertised every installed agent as
valid.

Fixture (the reproduction from the triage): an ``orchestrator`` spec that
declares ``availableAgents: [agent1, agent2, agent3]`` (and, to prove the two
keys are not confused, the same names under ``trustedAgents``), the three named
agents, and a ``rogue`` agent that is installed but not in the list.

Contract pinned here:

* ``rogue`` is REFUSED with ``AGENT_NOT_AVAILABLE_CODE`` when the parent is
  ``orchestrator``; ``agent1`` is admitted.
* A parent whose spec OMITS ``availableAgents`` admits everything it did
  before -- the control that proves no undeclared user's behaviour moves.
* ``trustedAgents`` alone is NOT an allowlist (upstream semantics: it means
  "run without approval prompts").
* The gate is an intersection with governance, never a replacement for it.
"""

from __future__ import annotations

import json
import os
from pathlib import Path
from typing import Any
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from kiro_crew import agent_discovery
from kiro_crew import subagent as sa
from kiro_crew.execution_context import ExecutionContext, MemoryStoreRef
from kiro_crew.mcp_tools import spawn as spawn_tools


def _write_spec(agents_dir: Path, name: str, **extra: Any) -> None:
    spec: dict[str, Any] = {"name": name, "description": f"{name} test agent", "tools": ["fs_read"]}
    spec.update(extra)
    (agents_dir / f"{name}.json").write_text(json.dumps(spec), encoding="utf-8")


@pytest.fixture
def agents_dir(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    d = tmp_path / "agents"
    d.mkdir()
    monkeypatch.setattr(agent_discovery, "_KIRO_AGENTS_DIR", d)
    agent_discovery.clear_list_agents_cache()
    yield d
    agent_discovery.clear_list_agents_cache()


@pytest.fixture
def triage_fixture(agents_dir: Path) -> Path:
    """§3c of the triage report: orchestrator + agent1..3 + an out-of-list agent."""
    names = ["agent1", "agent2", "agent3"]
    _write_spec(
        agents_dir,
        "orchestrator",
        toolsSettings={"subagent": {"availableAgents": names, "trustedAgents": names}},
    )
    for n in names:
        _write_spec(agents_dir, n)
    _write_spec(agents_dir, "rogue")
    # A parent that declares NOTHING about sub-agents: the pre-existing shape.
    _write_spec(agents_dir, "plain")
    # A parent that only wrote trustedAgents -- the misreading the report came in
    # with. It must NOT act as an allowlist.
    _write_spec(agents_dir, "trust-only", toolsSettings={"subagent": {"trustedAgents": names}})
    return agents_dir


class TestSpecReading:
    def test_omitted_key_is_none_not_empty(self) -> None:
        assert sa.spawn_allowlist({}) is None
        assert sa.spawn_allowlist({"toolsSettings": {}}) is None
        assert sa.spawn_allowlist({"toolsSettings": {"subagent": {}}}) is None
        # trustedAgents is a trust grant, not an allowlist.
        assert sa.spawn_allowlist({"toolsSettings": {"subagent": {"trustedAgents": ["a"]}}}) is None

    def test_declared_list_is_returned_verbatim(self) -> None:
        spec = {"toolsSettings": {"subagent": {"availableAgents": ["reviewer", "docs-*"]}}}
        assert sa.spawn_allowlist(spec) == ("reviewer", "docs-*")

    def test_malformed_declaration_fails_closed(self) -> None:
        """Declared but not a list of strings: the operator meant to restrict, so
        the answer is "nothing allowed", never "allow all"."""
        assert sa.spawn_allowlist({"toolsSettings": {"subagent": {"availableAgents": "x"}}}) == ()
        assert sa.spawn_allowlist(
            {"toolsSettings": {"subagent": {"availableAgents": [1, "a"]}}}
        ) == ("a",)

    def test_retained_globs_are_bounded_and_overflow_fails_closed(self) -> None:
        """The list is matched on the event loop on every spawn and the reader's
        only other ceiling is the file size cap, so what is retained is bounded:
        patterns past the count cap and any over-long pattern are dropped --
        never matched, so the overflow can only narrow what is allowed."""
        many = [f"agent-{i}" for i in range(sa._MAX_AVAILABLE_AGENTS_GLOBS + 50)]
        kept = sa.spawn_allowlist({"toolsSettings": {"subagent": {"availableAgents": many}}})
        assert kept == tuple(many[: sa._MAX_AVAILABLE_AGENTS_GLOBS])
        assert not sa.agent_matches_allowlist(many[-1], kept)
        assert sa.agent_matches_allowlist(many[0], kept)
        long_glob = "x" * (sa._MAX_AVAILABLE_AGENTS_GLOB_CHARS + 1)
        assert sa.spawn_allowlist(
            {"toolsSettings": {"subagent": {"availableAgents": [long_glob, "a"]}}}
        ) == ("a",)
        # A declaration made ONLY of over-long patterns is a declared, empty list.
        assert (
            sa.spawn_allowlist({"toolsSettings": {"subagent": {"availableAgents": [long_glob]}}})
            == ()
        )

    def test_glob_semantics_match_kiro_cli(self) -> None:
        assert sa.agent_matches_allowlist("docs-writer", ("docs-*",))
        assert sa.agent_matches_allowlist("reviewer", ("reviewer",))
        assert not sa.agent_matches_allowlist("reviewer2", ("reviewer",))
        assert not sa.agent_matches_allowlist("anything", ())

    def test_app_namespaced_agent_matches_its_bare_name_only_for_the_verified_app(self) -> None:
        """An app's materialized ``<app>--<agent>`` is what the gateway sees, while
        the app's spec lists the bare name kiro-cli's own gate matches against.
        The alias needs the VERIFIED app identity: a ``--`` inside any other
        installed name is just a name, so ``rogue--reviewer`` cannot satisfy a
        ``reviewer``-only list."""
        composer = "pptx-maker--pptx-maker-composer"
        assert sa.agent_matches_allowlist(composer, ("pptx-maker-composer",), app="pptx-maker")
        assert not sa.agent_matches_allowlist(composer, ("pptx-maker-composer",))
        assert not sa.agent_matches_allowlist("rogue--reviewer", ("reviewer",))
        assert not sa.agent_matches_allowlist("rogue--reviewer", ("reviewer",), app="other-app")


class TestParentAllowlistResolution:
    def test_declared_parent_resolves_to_its_list(self, triage_fixture: Path) -> None:
        assert sa.parent_spawn_allowlists("orchestrator") == (("agent1", "agent2", "agent3"),)

    def test_undeclared_parent_resolves_to_nothing(self, triage_fixture: Path) -> None:
        assert sa.parent_spawn_allowlists("plain") == ()
        assert sa.parent_spawn_allowlists("trust-only") == ()

    def test_unreadable_parent_record_is_unknown_not_parentless(self, triage_fixture: Path) -> None:
        """A record that exists but cannot be read is UNKNOWN (refuse); a caller
        with no session at all is parentless (allow all). The pump's re-check
        runs through this resolver, so the two must never collapse."""
        with patch.object(sa, "read_session_execution", side_effect=OSError("disk")):
            assert sa.parent_spawn_policy("chat-parent") == ("", None)
        denial = sa._vet_parent_available_agents(("", None), "agent1")
        assert denial is not None and "execution record could not be read" in denial
        with patch.object(sa, "read_session_execution", return_value=None):
            assert sa.parent_spawn_policy("chat-parent") == ("", ())
        assert sa.parent_spawn_policy("") == ("", ())

    def test_an_unscannable_agents_directory_is_unknown(
        self, triage_fixture: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """The snapshot reader folds a walk failure into "no specs"; for this gate
        that is UNKNOWN (refuse), not "no declaration"."""
        real_scandir = os.scandir

        def denied(path: Any = ".", *args: Any, **kwargs: Any) -> Any:
            if isinstance(path, (str, Path)) and Path(path) == triage_fixture:
                raise PermissionError(13, "denied", str(path))
            return real_scandir(path, *args, **kwargs)

        monkeypatch.setattr(sa.os, "scandir", denied)
        assert sa.parent_spawn_allowlists("orchestrator") is None
        assert _vet("orchestrator", "agent1") is not None

    def test_an_absent_agents_directory_declares_nothing(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setattr(agent_discovery, "_KIRO_AGENTS_DIR", tmp_path / "missing")
        agent_discovery.clear_list_agents_cache()
        assert sa.parent_spawn_allowlists("orchestrator") == ()

    def test_a_file_where_the_agents_directory_should_be_is_unknown(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        (tmp_path / "agents").write_text("not a directory", encoding="utf-8")
        monkeypatch.setattr(agent_discovery, "_KIRO_AGENTS_DIR", tmp_path / "agents")
        agent_discovery.clear_list_agents_cache()
        assert sa.parent_spawn_allowlists("orchestrator") is None

    def test_unknown_parent_resolves_to_nothing(self, triage_fixture: Path) -> None:
        # No spec for the parent means no declaration to honour: unchanged behaviour.
        assert sa.parent_spawn_allowlists("no-such-agent") == ()
        assert sa.parent_spawn_allowlists("") == ()

    def test_unreadable_parent_spec_is_unknown_not_allow_all(self, triage_fixture: Path) -> None:
        """The hardened reader folds an unparseable file into "no spec"; for the
        parent's OWN candidate that must read as unknown (refuse), not as an
        omitted key."""
        (triage_fixture / "orchestrator.json").write_text("{not json", encoding="utf-8")
        agent_discovery.clear_list_agents_cache()
        assert sa.parent_spawn_allowlists("orchestrator") is None
        assert _vet("orchestrator", "agent1") is not None
        assert _vet("orchestrator", "rogue") is not None

    def test_a_resembling_filename_is_not_the_parents_candidate(self, triage_fixture: Path) -> None:
        """A parent whose own declaration is readable is never refused by an
        unrelated broken file: a broken ``sprint-planner.json`` beside a healthy
        ``planner.json`` must not refuse ``planner``'s spawns."""
        _write_spec(triage_fixture, "planner")
        (triage_fixture / "sprint-planner.json").write_text("{not json", encoding="utf-8")
        agent_discovery.clear_list_agents_cache()
        assert sa.parent_spawn_allowlists("planner") == ()

    def test_a_declaring_spec_under_another_filename_wins_over_a_broken_candidate(
        self, triage_fixture: Path
    ) -> None:
        """A readable spec that DECLARES the name is the one kiro-cli resolves,
        so a broken ``<template>.json`` beside it does not make the answer unknown."""
        (triage_fixture / "pkg-orchestrator3.json").write_text(
            json.dumps({"name": "orchestrator3", "description": "d", "tools": ["fs_read"]}),
            encoding="utf-8",
        )
        (triage_fixture / "orchestrator3.json").write_text("{not json", encoding="utf-8")
        agent_discovery.clear_list_agents_cache()
        assert sa.parent_spawn_allowlists("orchestrator3") == ()

    def test_unrelated_unreadable_spec_does_not_refuse_a_readable_parent(
        self, triage_fixture: Path
    ) -> None:
        """A broken spec for some other agent must not refuse a session whose own
        declaration is readable, so one bad file in the directory leaves every
        parent with a parseable spec exactly where it was."""
        (triage_fixture / "broken-other.json").write_text("{not json", encoding="utf-8")
        agent_discovery.clear_list_agents_cache()
        assert sa.parent_spawn_allowlists("plain") == ()
        assert sa.parent_spawn_allowlists("orchestrator") == (("agent1", "agent2", "agent3"),)
        assert _vet("plain", "rogue") is None

    def test_a_declaring_spec_under_another_filename_that_breaks_is_unknown(
        self, triage_fixture: Path
    ) -> None:
        """kiro-cli resolves a name by its DECLARED ``name`` under any filename.
        When the only spec declaring the parent becomes unreadable, no readable
        spec declares the name and no direct-filename candidate exists -- the
        shape a direct-filename-only probe reads as ``()`` (allow all). It is
        UNKNOWN: the declaration may be inside the very file that did not parse."""
        pkg = triage_fixture / "pkg-orchestrator4.json"
        pkg.write_text(
            json.dumps(
                {
                    "name": "orchestrator4",
                    "description": "d",
                    "tools": ["fs_read"],
                    "toolsSettings": {"subagent": {"availableAgents": ["agent1"]}},
                }
            ),
            encoding="utf-8",
        )
        agent_discovery.clear_list_agents_cache()
        assert sa.parent_spawn_allowlists("orchestrator4") == (("agent1",),)
        assert _vet("orchestrator4", "rogue") is not None
        pkg.write_text("{not json", encoding="utf-8")
        agent_discovery.clear_list_agents_cache()
        assert sa.parent_spawn_allowlists("orchestrator4") is None
        denial = _vet("orchestrator4", "rogue")
        assert denial is not None and "could not be read" in denial
        # The listed child is refused too: unknown is not "allow the old list".
        assert _vet("orchestrator4", "agent1") is not None

    def test_a_parent_with_no_spec_is_unknown_while_any_spec_is_unreadable(
        self, triage_fixture: Path
    ) -> None:
        """The accepted cost of the rule above: a parent no readable spec declares
        cannot be told apart from one whose declaring spec broke, so it is
        refused while any spec file is unreadable, and admitted again the moment
        the file is repaired or removed. Sidecars do not count."""
        broken = triage_fixture / "broken-other.json"
        broken.write_text("{not json", encoding="utf-8")
        agent_discovery.clear_list_agents_cache()
        assert sa.parent_spawn_allowlists("no-such-agent") is None
        broken.unlink()
        (triage_fixture / "._orchestrator.json").write_text("{not json", encoding="utf-8")
        agent_discovery.clear_list_agents_cache()
        assert sa.parent_spawn_allowlists("no-such-agent") == ()

    def test_an_mtime_preserving_restrictive_rewrite_is_honoured(
        self, triage_fixture: Path
    ) -> None:
        """A ``cp -p`` / ``rsync -t`` restore rewrites a spec without moving its
        mtime, and no in-process writer clears the caches. The catalog snapshot
        revalidates on names and mtime alone and would keep serving the
        PERMISSIVE answer it read first; the gate's read is pinned to the
        stronger directory revision (ctime, size, racy window) and re-reads."""
        spec = triage_fixture / "plain.json"
        stat = spec.stat()
        assert sa.parent_spawn_allowlists("plain") == ()
        assert _vet("plain", "rogue") is None
        # Tighten the declaration and put the original timestamps back, with no
        # clear_list_agents_cache() between: only the bytes changed.
        _write_spec(
            triage_fixture,
            "plain",
            toolsSettings={"subagent": {"availableAgents": ["agent1"]}},
        )
        os.utime(spec, ns=(stat.st_atime_ns, stat.st_mtime_ns))
        assert spec.stat().st_mtime_ns == stat.st_mtime_ns
        assert sa.parent_spawn_allowlists("plain") == (("agent1",),)
        assert _vet("plain", "rogue") is not None
        assert _vet("plain", "agent1") is None


def _vet(parent: str, child: str, **kw: Any) -> str | None:
    policy = (parent, sa.parent_spawn_allowlists(parent) if parent else ())
    return sa._vet_parent_available_agents(policy, child, **kw)


class TestVetAgainstParentSpec:
    def test_rogue_is_refused_by_a_declaring_parent(self, triage_fixture: Path) -> None:
        denial = _vet("orchestrator", "rogue")
        assert denial is not None
        assert "rogue" in denial and "availableAgents" in denial
        # The allowlist travels with the refusal so the caller can self-correct.
        assert "agent1" in denial

    def test_listed_agent_is_admitted(self, triage_fixture: Path) -> None:
        for name in ("agent1", "agent2", "agent3"):
            assert _vet("orchestrator", name) is None

    def test_undeclared_parent_admits_everything(self, triage_fixture: Path) -> None:
        """The no-regression control: every pre-existing spawn from a parent that
        never wrote ``availableAgents`` still goes through, rogue included."""
        for parent in ("plain", "trust-only", "no-such-agent", ""):
            for child in ("agent1", "rogue", "orchestrator", "kirocrew"):
                assert _vet(parent, child) is None, (parent, child)

    def test_empty_child_is_not_vetted_here(self, triage_fixture: Path) -> None:
        # The gate resolves the EFFECTIVE template before calling; an empty name
        # never reaches the glob match, so it cannot be refused by accident.
        assert _vet("orchestrator", "") is None


def _mock_sessions() -> MagicMock:
    sessions = MagicMock()
    sessions.get_pid = MagicMock(return_value=None)
    sessions.get_agent = MagicMock(return_value="")
    sessions.get_agent_selection = MagicMock(return_value=("template", ""))
    sessions.get_approval_policy = MagicMock(return_value="")
    return sessions


def _mock_ctx_builder() -> MagicMock:
    ctx = MagicMock()
    ctx.build_message = MagicMock(return_value=("built_message", None))
    ctx.hooks.on_tool_call = MagicMock()
    ctx.hooks.auto_approve_subagent_spawn = True
    return ctx


def _parent_execution(template: str) -> ExecutionContext:
    return ExecutionContext(
        None, MemoryStoreRef("default"), "template", template, "persistent", "", template
    )


@pytest.mark.usefixtures("healthy_host_memory")
class TestGateWiring:
    """The check runs at the admission gate, next to governance, before any row
    is persisted, and reports its own code on the refused ``SubagentInfo``."""

    async def _spawn(self, parent_template: str, agent: str) -> Any:
        from kiro_crew.subagent import SubagentManager

        manager = SubagentManager(sessions=_mock_sessions(), ctx_builder=_mock_ctx_builder())
        await manager.wait_taskq_ready()
        sel_mock = MagicMock()
        parent = _parent_execution(parent_template)
        with (
            patch("kiro_crew.subagent.Stats"),
            patch("kiro_crew.subagent.sel", return_value=sel_mock),
            patch("kiro_crew.execution_context.read_session_execution", return_value=parent),
            patch("kiro_crew.subagent.read_session_execution", return_value=parent),
        ):
            info = manager.spawn("t", parent_session_key="chat-parent", agent=agent)
        return manager, info, sel_mock

    @pytest.mark.asyncio
    async def test_gate_refuses_out_of_list_agent(self, triage_fixture: Path) -> None:
        manager, info, sel_mock = await self._spawn("orchestrator", "rogue")
        assert info is not None and info.done is True
        assert info.error_code == sa.AGENT_NOT_AVAILABLE_CODE
        assert "rogue" in info.error and "availableAgents" in info.error
        # A policy refusal: no slot taken, no row persisted, audited as denied.
        assert manager._running_count == 0
        assert (
            manager._admission.taskq_store() is None
            or not manager._admission.taskq_store().list_rows()
        )
        denied = [
            c
            for c in sel_mock.log_tool_invocation.call_args_list
            if c.kwargs.get("outcome") == "denied"
        ]
        assert len(denied) == 1 and "availableAgents" in denied[0].kwargs.get("error", "")

    @pytest.mark.asyncio
    async def test_gate_admits_listed_agent(self, triage_fixture: Path) -> None:
        _manager, info, _sel = await self._spawn("orchestrator", "agent1")
        assert info is not None
        assert info.error_code != sa.AGENT_NOT_AVAILABLE_CODE
        assert "availableAgents" not in (info.error or "")

    @pytest.mark.asyncio
    async def test_gate_leaves_an_undeclared_parent_alone(self, triage_fixture: Path) -> None:
        """No-regression control at the gate itself: the same out-of-list name
        from a parent that never declared the key is not refused on this ground."""
        for parent in ("plain", "trust-only"):
            _manager, info, _sel = await self._spawn(parent, "rogue")
            assert info is not None
            assert info.error_code != sa.AGENT_NOT_AVAILABLE_CODE
            assert "availableAgents" not in (info.error or "")

    @pytest.mark.asyncio
    async def test_inherited_template_is_vetted_too(self, triage_fixture: Path) -> None:
        """Omitting ``agent`` inherits the parent's own template, which is not in
        the parent's list here -- the effective template is what is checked, so
        the list cannot be routed around by not naming an agent."""
        _manager, info, _sel = await self._spawn("orchestrator", "")
        assert info is not None and info.error_code == sa.AGENT_NOT_AVAILABLE_CODE

    @pytest.mark.asyncio
    async def test_gate_refuses_when_the_parent_spec_is_unreadable(
        self, triage_fixture: Path
    ) -> None:
        (triage_fixture / "orchestrator.json").write_text("{not json", encoding="utf-8")
        agent_discovery.clear_list_agents_cache()
        _manager, info, _sel = await self._spawn("orchestrator", "agent1")
        assert info is not None and info.error_code == sa.AGENT_NOT_AVAILABLE_CODE
        assert "could not be read" in info.error

    @pytest.mark.asyncio
    async def test_a_precomputed_policy_is_consumed_without_an_inline_scan(
        self, triage_fixture: Path
    ) -> None:
        """The event-loop entry points hand the gate the parent's declaration
        they read off-loop; with it present the gate must not scan inline."""
        from kiro_crew.subagent import SubagentManager

        manager = SubagentManager(sessions=_mock_sessions(), ctx_builder=_mock_ctx_builder())
        await manager.wait_taskq_ready()
        parent = _parent_execution("orchestrator")
        with (
            patch("kiro_crew.subagent.Stats"),
            patch("kiro_crew.subagent.sel"),
            patch("kiro_crew.execution_context.read_session_execution", return_value=parent),
            patch("kiro_crew.subagent.read_session_execution", return_value=parent),
            patch.object(
                sa, "parent_spawn_policy", side_effect=AssertionError("scanned on the loop")
            ),
        ):
            info = manager.spawn(
                "t",
                parent_session_key="chat-parent",
                agent="rogue",
                _parent_spawn_policy=("orchestrator", (("agent1", "agent2", "agent3"),)),
            )
        assert info is not None and info.error_code == sa.AGENT_NOT_AVAILABLE_CODE

    @pytest.mark.asyncio
    async def test_spawn_async_resolves_the_policy_off_the_loop(self, triage_fixture: Path) -> None:
        import threading

        from kiro_crew.subagent import SubagentManager

        manager = SubagentManager(sessions=_mock_sessions(), ctx_builder=_mock_ctx_builder())
        await manager.wait_taskq_ready()
        parent = _parent_execution("orchestrator")
        loop_thread = threading.get_ident()
        seen: list[int] = []

        def resolver(parent_template: str) -> Any:
            seen.append(threading.get_ident())
            assert parent_template == "orchestrator"
            return (("agent1", "agent2", "agent3"),)

        with (
            patch("kiro_crew.subagent.Stats"),
            patch("kiro_crew.subagent.sel"),
            patch("kiro_crew.execution_context.read_session_execution", return_value=parent),
            patch("kiro_crew.subagent.read_session_execution", return_value=parent),
            patch.object(sa, "parent_spawn_allowlists", side_effect=resolver),
            patch.object(sa, "parent_spawn_policy", side_effect=AssertionError("re-read parent")),
        ):
            info = await manager.spawn_async("t", parent_session_key="chat-parent", agent="rogue")
        assert info is not None and info.error_code == sa.AGENT_NOT_AVAILABLE_CODE
        assert seen and all(tid != loop_thread for tid in seen), "policy resolved on the loop"

    @pytest.mark.asyncio
    async def test_queued_params_carry_the_admitted_policy(self, triage_fixture: Path) -> None:
        """The in-memory queue's synchronous drain re-enters ``spawn`` from the
        stored params, so the policy admitted with the row travels with it and
        the drain scans nothing on the loop; the durable row never carries it."""
        from kiro_crew.subagent import SubagentManager

        manager = SubagentManager(
            sessions=_mock_sessions(), ctx_builder=_mock_ctx_builder(), max_concurrent=1
        )
        await manager.wait_taskq_ready()
        parent = _parent_execution("plain")
        policy = ("plain", ())
        with (
            patch("kiro_crew.subagent.Stats"),
            patch("kiro_crew.subagent.sel"),
            patch("kiro_crew.execution_context.read_session_execution", return_value=parent),
            patch("kiro_crew.subagent.read_session_execution", return_value=parent),
            patch.object(SubagentManager, "_run", new=AsyncMock()),
        ):
            manager.spawn("occupy", parent_session_key="chat-parent", _parent_spawn_policy=policy)
            queued = manager.spawn(
                "waiting", parent_session_key="chat-parent", _parent_spawn_policy=policy
            )
        assert queued is not None and queued.queued
        params = manager._queue[0]
        assert params["_parent_spawn_policy"] == policy
        record = manager._admission.taskq_build_record(
            "x",
            params,
            parent_session_key="chat-parent",
            memory_store="",
            app="",
            model=None,
            allowed_tools=None,
            approval_mode=None,
        )
        assert "_parent_spawn_policy" not in record.params

    def test_error_code_is_distinct_from_not_found(self) -> None:
        assert sa.AGENT_NOT_AVAILABLE_CODE != sa.AGENT_NOT_FOUND_CODE
        assert sa.AGENT_NOT_AVAILABLE_CODE == "agent_not_available"

    def test_wave_short_circuit_recognises_the_code(self) -> None:
        assert spawn_tools._is_unknown_agent_refusal({"code": sa.AGENT_NOT_AVAILABLE_CODE}, "rogue")
        assert not spawn_tools._is_unknown_agent_refusal({"code": sa.AGENT_NOT_AVAILABLE_CODE}, "")


class TestToolDescriptionRoster:
    """``spawn_run``'s "Valid names right now" lists only what the parent may spawn."""

    def test_roster_is_filtered_by_the_parent_allowlist(
        self, triage_fixture: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setattr(spawn_tools, "_parent_template_for_roster", lambda: "orchestrator")
        hint = spawn_tools._agent_roster_hint()
        assert "agent1" in hint and "agent2" in hint and "agent3" in hint
        assert "rogue" not in hint
        assert "availableAgents" in hint

    def test_roster_is_unfiltered_for_an_undeclared_parent(
        self, triage_fixture: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setattr(spawn_tools, "_parent_template_for_roster", lambda: "plain")
        hint = spawn_tools._agent_roster_hint()
        assert "rogue" in hint and "agent1" in hint
        assert "availableAgents" not in hint

    def test_roster_is_unfiltered_when_the_parent_is_unknown(
        self, triage_fixture: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        # The pre-existing shape: no session identity -> the whole installed roster.
        monkeypatch.setattr(spawn_tools, "_parent_template_for_roster", lambda: "")
        hint = spawn_tools._agent_roster_hint()
        assert "rogue" in hint and "agent1" in hint

    def test_empty_allowlist_still_announces_the_restriction(
        self, triage_fixture: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        _write_spec(triage_fixture, "locked", toolsSettings={"subagent": {"availableAgents": []}})
        agent_discovery.clear_list_agents_cache()
        monkeypatch.setattr(spawn_tools, "_parent_template_for_roster", lambda: "locked")
        hint = spawn_tools._agent_roster_hint()
        assert "availableAgents" in hint and "none of the installed agents" in hint
        assert "rogue" not in hint and "agent1" not in hint
        with patch.object(spawn_tools.mcp_core, "_get", return_value={"agents": []}):
            listing = spawn_tools.spawn_list("spawn_list", {})
        assert "availableAgents" in listing and "every installed agent is refused" in listing
        assert "Available agents:" not in listing

    def test_unreadable_parent_leaves_the_roster_alone(
        self, triage_fixture: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """The gate refuses on its own; the advisory roster does not pretend to
        know the list it could not read."""
        (triage_fixture / "orchestrator.json").write_text("{not json", encoding="utf-8")
        agent_discovery.clear_list_agents_cache()
        monkeypatch.setattr(spawn_tools, "_parent_template_for_roster", lambda: "orchestrator")
        hint = spawn_tools._agent_roster_hint()
        assert "rogue" in hint and "availableAgents" not in hint
