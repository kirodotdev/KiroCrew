"""Tests for the agent sync prune logic, ``_do_agents_sync`` in dashboard/agent_admin/installed_agents.py."""

from __future__ import annotations

import json
from unittest.mock import MagicMock, patch

import pytest

from kiro_crew.agent_discovery import AgentInfo
from kiro_crew.config.loader import KiroCrewAgentConfig, KiroCrewConfig, PendingEditBlocksSave
from kiro_crew.config.sections import MemoryConfig
from kiro_crew.memory_stores import (
    UnknownMemoryStore,
    provision_member_memory,
    require_member_memory_store,
)


def _make_aim_agent(name: str) -> AgentInfo:
    return AgentInfo(
        name=name,
        filename=f"local-OmniAgents-{name}.json",
        description=f"{name} agent",
        model="auto",
        source="aim",
        package="OmniAgents",
    )


def _make_config(agents: dict[str, KiroCrewAgentConfig]) -> KiroCrewConfig:
    """Create a MagicMock standing in for KiroCrewConfig with the given agents dict."""
    cfg = MagicMock(spec=KiroCrewConfig)
    cfg.agents = agents
    cfg.memory_stores = {}
    cfg.memory = MemoryConfig()
    cfg.degraded_sections = frozenset()
    cfg.default_agent = "kirocrew"
    cfg.save = MagicMock()
    return cfg


async def _run_sync(cfg: KiroCrewConfig, aim_agents_list: list[AgentInfo]) -> dict:
    """Invoke the production _do_agents_sync with mocked dependencies and return parsed body.

    The sync persists via a delta mutate through ``update_config_locked``;
    the patch below records each call on ``cfg.save`` (so the
    existing called/not-called assertions keep their meaning) and stores the
    mutated document on ``cfg.written_doc``.
    """
    from kiro_crew.dashboard.handlers.agents import _do_agents_sync

    request = MagicMock()
    request.get.return_value = "dashboard"

    sel_mock = MagicMock()

    def _fake_update_config_locked(*args, **kwargs):
        doc: dict = {"agents": {}, "memory_stores": {}}
        result = kwargs["mutate"](doc)
        cfg.save()
        cfg.written_doc = result
        return result

    with (
        patch("kiro_crew.dashboard.handlers.agents.KiroCrewConfig.load", return_value=cfg),
        patch("kiro_crew.dashboard.handlers.agents.list_agents", return_value=aim_agents_list),
        patch(
            "kiro_crew.dashboard.handlers.agents.update_config_locked",
            new=_fake_update_config_locked,
        ),
        patch("kiro_crew.dashboard.handlers.agents._sel", return_value=sel_mock),
    ):
        response = await _do_agents_sync(request)

    assert response.body is not None
    return json.loads(response.body)


class TestAgentSyncPrune:
    """Tests for the prune step in _do_agents_sync (real production code path)."""

    @pytest.mark.asyncio
    async def test_prune_removes_stale_aim_agents(self):
        """Agents with source='aim' not in scan results get pruned."""
        agents = {
            "omni-reviewer": KiroCrewAgentConfig(kiro_agent="omni-reviewer", source="aim"),
            "omni-aws": KiroCrewAgentConfig(kiro_agent="omni-aws", source="aim"),
            "gpu-dev": KiroCrewAgentConfig(kiro_agent="gpu-dev", source="aim"),
        }
        cfg = _make_config(agents)
        aim_list = [_make_aim_agent("omni-aws"), _make_aim_agent("gpu-dev")]

        body = await _run_sync(cfg, aim_list)

        assert body["pruned"] == ["omni-reviewer"]
        assert "omni-reviewer" not in cfg.agents
        assert "omni-aws" in cfg.agents
        assert "gpu-dev" in cfg.agents
        cfg.save.assert_called_once()

    @pytest.mark.asyncio
    async def test_prune_removes_a_starred_package_agent_too(self):
        """A star does not keep a spec-less row alive: the row is pruned like
        any other and a reinstall comes back un-starred (one click restores it)."""
        agents = {
            "omni-reviewer": KiroCrewAgentConfig(
                kiro_agent="omni-reviewer", source="aim", starred=True
            ),
            "omni-aws": KiroCrewAgentConfig(kiro_agent="omni-aws", source="aim"),
        }
        cfg = _make_config(agents)
        body = await _run_sync(cfg, [_make_aim_agent("omni-aws")])
        assert body["pruned"] == ["omni-reviewer"]
        assert "omni-reviewer" not in cfg.agents
        body = await _run_sync(cfg, [_make_aim_agent("omni-aws"), _make_aim_agent("omni-reviewer")])
        assert body["synced"] == ["omni-reviewer"]
        assert cfg.agents["omni-reviewer"].starred is False

    @pytest.mark.asyncio
    @pytest.mark.parametrize(
        ("name", "source"),
        [
            pytest.param("kirocrew", "kirocrew", id="test_prune_skips_kirocrew_owned_agents"),
            # source='builtin' is a user-created agent.
            pytest.param("my-custom", "builtin", id="test_prune_skips_user_created_agents"),
        ],
    )
    async def test_prune_skips_agents_aim_does_not_own(self, name: str, source: str):
        """Only AIM-sourced agents are pruned; kirocrew-owned and user-created ones never are."""
        agents = {
            name: KiroCrewAgentConfig(kiro_agent=name, source=source),
            "stale-aim": KiroCrewAgentConfig(kiro_agent="stale-aim", source="aim"),
        }
        cfg = _make_config(agents)
        aim_list = [_make_aim_agent("gpu-dev")]

        body = await _run_sync(cfg, aim_list)

        assert "stale-aim" in body["pruned"]
        assert name not in body["pruned"]
        assert name in cfg.agents

    @pytest.mark.asyncio
    async def test_no_prune_when_scan_returns_empty(self):
        """Empty scan result (likely transient failure) should not prune anything."""
        agents = {
            "omni-aws": KiroCrewAgentConfig(kiro_agent="omni-aws", source="aim"),
            "gpu-dev": KiroCrewAgentConfig(kiro_agent="gpu-dev", source="aim"),
        }
        cfg = _make_config(agents)
        aim_list: list[AgentInfo] = []

        body = await _run_sync(cfg, aim_list)

        assert body["pruned"] == []
        assert body["synced"] == []
        assert "omni-aws" in cfg.agents
        assert "gpu-dev" in cfg.agents
        cfg.save.assert_not_called()

    @pytest.mark.asyncio
    async def test_add_and_prune_in_same_sync(self):
        """A single sync both adds new agents and prunes stale ones."""
        agents = {
            "old-agent": KiroCrewAgentConfig(kiro_agent="old-agent", source="aim"),
        }
        cfg = _make_config(agents)
        aim_list = [_make_aim_agent("new-agent")]

        body = await _run_sync(cfg, aim_list)

        assert body["synced"] == ["new-agent"]
        assert body["pruned"] == ["old-agent"]
        assert "new-agent" in cfg.agents
        assert "old-agent" not in cfg.agents
        cfg.save.assert_called_once()

    @pytest.mark.asyncio
    async def test_a_pending_config_json_is_audited_and_hands_the_refusal_on(self):
        """config.json has a change pending owner review: the sync's write is refused,
        its failure is audited, and the refusal reaches the dashboard chain, which
        answers 409, not the sync's own 500."""
        from kiro_crew.config import loosening_provenance as lp
        from kiro_crew.dashboard.handlers.agents import _do_agents_sync

        cfg = _make_config({})
        request = MagicMock()
        request.get.return_value = "dashboard"
        sel_mock = MagicMock()

        with (
            patch("kiro_crew.dashboard.handlers.agents.KiroCrewConfig.load", return_value=cfg),
            patch(
                "kiro_crew.dashboard.handlers.agents.list_agents",
                return_value=[_make_aim_agent("new-agent")],
            ),
            patch(
                "kiro_crew.dashboard.handlers.agents.update_config_locked",
                side_effect=PendingEditBlocksSave(lp.LEAF_CONFIG),
            ),
            patch("kiro_crew.dashboard.handlers.agents._sel", return_value=sel_mock),
            pytest.raises(PendingEditBlocksSave),
        ):
            await _do_agents_sync(request)

        assert sel_mock.log_api_access.call_args.kwargs["outcome"] == "failure"

    @pytest.mark.asyncio
    async def test_noop_when_nothing_changed(self):
        """No adds or prunes when config matches scan exactly."""
        agents = {
            "omni-aws": KiroCrewAgentConfig(kiro_agent="omni-aws", source="aim"),
        }
        cfg = _make_config(agents)
        aim_list = [_make_aim_agent("omni-aws")]

        body = await _run_sync(cfg, aim_list)

        assert body["synced"] == []
        assert body["pruned"] == []
        cfg.save.assert_not_called()

    @pytest.mark.asyncio
    async def test_package_prune_preserves_memory_without_rebinding_new_member(self):
        from kiro_crew.dashboard.handlers.agents import _do_agents_sync

        cfg = KiroCrewConfig.load()
        cfg.agents["stale-package"] = KiroCrewAgentConfig(
            kiro_agent="stale-package", source="package"
        )
        cfg.agents["live"] = KiroCrewAgentConfig(kiro_agent="live", source="package")
        store = provision_member_memory(cfg, "stale-package")
        cfg.save()
        request = MagicMock()
        request.get.return_value = "dashboard"
        request.app = {}
        with (
            patch(
                "kiro_crew.dashboard.handlers.agents.list_agents",
                return_value=[_make_aim_agent("live")],
            ),
            patch("kiro_crew.dashboard.handlers.agents._sel", return_value=MagicMock()),
        ):
            response = await _do_agents_sync(request)
        assert json.loads(response.body)["pruned"] == ["stale-package"]

        rebind = KiroCrewConfig.load()
        rebind.agents["stale-package"] = KiroCrewAgentConfig(
            kiro_agent="stale-package", source="package", memory_store=store
        )
        rebind.save()
        with pytest.raises(UnknownMemoryStore, match="member identity is missing or ambiguous"):
            require_member_memory_store(KiroCrewConfig.load(), "stale-package")


class TestSyncRefusesCredentialShapedNames:
    """The SECOND way a name reaches `cfg.agents`, which the create route cannot see.

    A discovered spec's name is package-controlled, not typed by the owner, so
    "the owner is reading a string the owner wrote" does not hold for it: a package
    could land a credential-shaped name that then reaches the roster. Refused at
    this source too.
    """

    PROBE = "AKIAIOSFODNN7EXAMPLE"

    @pytest.mark.asyncio
    async def test_a_credential_shaped_discovered_name_is_not_synced(self):
        cfg = _make_config({})
        body = await _run_sync(cfg, [_make_aim_agent(self.PROBE)])
        assert self.PROBE not in cfg.agents, "a credential-shaped package name was stored"
        assert self.PROBE not in json.dumps(body), "the name was echoed into the response"

    @pytest.mark.asyncio
    async def test_an_ordinary_discovered_name_still_syncs(self):
        """The direction that proves the refusal is narrow, not a blanket."""
        cfg = _make_config({})
        await _run_sync(cfg, [_make_aim_agent("oncall-triage")])
        assert "oncall-triage" in cfg.agents
        store = cfg.agents["oncall-triage"].memory_store
        assert cfg.written_doc["agents"]["oncall-triage"]["memory_store"] == store
        assert store == "default"
        assert cfg.written_doc["memory_stores"] == {}

    @pytest.mark.asyncio
    async def test_reinstalled_package_member_does_not_adopt_previous_memory(self):
        """An existing store is retained but never inherited by a same-name discovery."""
        cfg = _make_config({"oncall": KiroCrewAgentConfig(kiro_agent="oncall", source="package")})
        retired_store = provision_member_memory(cfg, "oncall")
        del cfg.agents["oncall"]

        body = await _run_sync(cfg, [_make_aim_agent("oncall")])

        assert body["synced"] == ["oncall"]
        fresh = cfg.agents["oncall"].memory_store
        assert fresh != retired_store
        assert retired_store in cfg.memory_stores
        assert fresh == "default"
        assert cfg.written_doc["memory_stores"] == {}
        from kiro_crew.memory_stores import memory_stores_root
        from kiro_crew.vector_memory import read_member_database_identity

        assert (
            read_member_database_identity(memory_stores_root() / retired_store / "memory.db")[1]
            == retired_store
        )


class TestAgentSyncFsCheckIsOffloaded:
    """The per-agent on-disk existence check (a stat + a namespaced glob) runs in
    a loop over discovered agents; on a populated agents directory it must be
    offloaded or the gateway loop and heartbeat stall."""

    def test_the_on_disk_check_is_awaited_off_loop(self) -> None:
        import inspect

        from kiro_crew.dashboard.handlers import agents

        src = inspect.getsource(agents._do_agents_sync)
        assert "await asyncio.to_thread(" in src
        assert "_namespaced_agent_file_exists(_dn)" in src, "the FS check must run off-loop"


class TestPruneOnlySnapshotMatchedEntries:
    """The locked prune only deletes entries that still equal this sync's own
    snapshot -- an agent (re)added by a NEWER sync between the discovery snapshot
    and the lock hold must survive a stale prune."""

    @pytest.mark.asyncio
    async def test_agent_added_or_changed_after_snapshot_survives_stale_prune(self):
        from kiro_crew.dashboard.handlers.agents import _do_agents_sync

        cfg = _make_config({"stale": KiroCrewAgentConfig(kiro_agent="stale-spec", source="aim")})
        request = MagicMock()
        request.get.return_value = "dashboard"

        # Discovery finds one unrelated agent, so "stale" (spec gone) is this
        # sync's prune candidate. The in-lock document simulates a NEWER sync
        # having landed between the snapshot and the lock hold: "stale" was
        # re-added with a DIFFERENT spec name, and "fresh" is brand new.
        # Neither equals this sync's snapshot entry, so neither is pruned.
        in_lock_doc = {
            "agents": {
                "stale": {"kiro_agent": "renewed-spec", "source": "aim"},
                "fresh": {"kiro_agent": "fresh-spec", "source": "aim"},
            }
        }
        written: dict = {}

        def _fake_update_config_locked(*args, **kwargs):
            result = kwargs["mutate"](in_lock_doc)
            written["doc"] = result if result is not None else in_lock_doc
            return result

        with (
            patch("kiro_crew.dashboard.handlers.agents.KiroCrewConfig.load", return_value=cfg),
            patch(
                "kiro_crew.dashboard.handlers.agents.list_agents",
                return_value=[_make_aim_agent("unrelated")],
            ),
            patch(
                "kiro_crew.dashboard.handlers.agents.update_config_locked",
                new=_fake_update_config_locked,
            ),
            patch("kiro_crew.dashboard.handlers.agents._sel", return_value=MagicMock()),
        ):
            await _do_agents_sync(request)

        agents_after = written["doc"]["agents"]
        assert "fresh" in agents_after, "an agent added after the snapshot was pruned"
        assert (
            agents_after["stale"]["kiro_agent"] == "renewed-spec"
        ), "a re-added (changed) entry was deleted on stale snapshot evidence"


class TestAgentSyncSkipsForks:
    """An orphaned fork (private_to set, owner crew gone) must NOT resurrect as a
    ghost agent. Normally the owner's binding puts the fork in mc_kiro_agents so
    the add branch never sees it; the guard fires only for the orphaned copy."""

    def _fork_agent(self, name: str, private_to: str) -> AgentInfo:
        return AgentInfo(
            name=name,
            filename=f"{name}.json",
            description="orphaned crew copy",
            model="auto",
            source="builtin",
            private_to=private_to,
        )

    @pytest.mark.asyncio
    async def test_orphaned_fork_is_not_auto_created(self):
        cfg = _make_config({})
        aim_list = [self._fork_agent("ex-crew-copy", private_to="ex-crew")]

        body = await _run_sync(cfg, aim_list)

        assert "ex-crew-copy" not in body["synced"]
        assert "ex-crew-copy" not in cfg.agents
        cfg.save.assert_not_called()

    @pytest.mark.asyncio
    async def test_same_agent_without_private_to_would_be_created(self):
        """Control: the ONLY thing keeping the fork out is private_to."""
        cfg = _make_config({})
        twin = self._fork_agent("would-be-agent", private_to="")

        body = await _run_sync(cfg, [twin])

        assert body["synced"] == ["would-be-agent"]
        assert "would-be-agent" in cfg.agents


class TestAgentSyncSkipsRuntimeSpecs:
    """Kiro Crew's own specs are runtime templates, not Bots to enrol.

    Discovery labels the managed helper specs (worker, conductors, knowledge, ...)
    ``source="builtin"`` with ``kirocrew_owned=True``, and a derived
    ``<agent>--readonly`` side-turn spec reads as an ordinary ``builtin`` row. The
    sync must leave both out of ``cfg.agents``: a row written there lands on the
    shared memory store and no prune ever removes it.
    """

    @staticmethod
    def _owned_spec(name: str) -> AgentInfo:
        return AgentInfo(
            name=name,
            filename=f"{name}.json",
            description=f"{name} runtime spec",
            model="auto",
            source="builtin",
            kirocrew_owned=True,
        )

    @staticmethod
    def _readonly_spec(base: str) -> AgentInfo:
        from kiro_crew.dashboard.side_readonly_spec import OWNER_MARKER, READONLY_SUFFIX

        name = f"{base}{READONLY_SUFFIX}"
        return AgentInfo(
            name=name,
            filename=f"{name}.json",
            description=f"{OWNER_MARKER} of {base!r}; generated on every side turn, do not edit.",
            model="auto",
            source="builtin",
        )

    @pytest.mark.asyncio
    @pytest.mark.parametrize(
        "row",
        [
            _owned_spec("kirocrew-worker"),
            _owned_spec("kirocrew-conductor"),
            _readonly_spec("oncall"),
        ],
        ids=["owned-worker", "owned-conductor", "derived-readonly"],
    )
    async def test_runtime_spec_is_not_enrolled(self, row: AgentInfo):
        cfg = _make_config({})

        body = await _run_sync(cfg, [row])

        assert row.name not in body["synced"]
        assert row.name not in cfg.agents
        cfg.save.assert_not_called()

    @pytest.mark.asyncio
    async def test_same_row_without_runtime_markers_is_enrolled(self):
        """Control: the owned flag and the owner marker are what keep the rows out."""
        cfg = _make_config({})
        plain = AgentInfo(
            name="kirocrew-worker",
            filename="kirocrew-worker.json",
            description="a hand-written spec",
            model="auto",
            source="builtin",
            kirocrew_owned=False,
        )

        body = await _run_sync(cfg, [plain])

        assert body["synced"] == ["kirocrew-worker"]
        assert "kirocrew-worker" in cfg.agents
