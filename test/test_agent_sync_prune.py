"""Tests for agent sync prune logic in dashboard/handlers/agents.py."""

from __future__ import annotations

import dataclasses
import json
import tempfile
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest
from member_memory_helpers import PRIVATE_EXECUTION_GATE

from kiro_crew.agent_discovery import AgentInfo
from kiro_crew.config.loader import KiroCrewAgentConfig, KiroCrewConfig
from kiro_crew.config.sections import MemoryConfig
from kiro_crew.memory_stores import (
    UnknownMemoryStore,
    archive_member_memory_store,
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


async def _run_sync(
    cfg: KiroCrewConfig,
    aim_agents_list: list[AgentInfo],
    *,
    apps_unreadable: bool = False,
    lifecycle_in_progress: bool = False,
    file_back_at_write: str | None = None,
    apps_declaring: dict[str, set[str]] | None = None,
) -> dict:
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

    # The document the locked write reads is the config as it stood BEFORE the
    # sync's snapshot edits (the real one is re-read from disk under the lock).
    on_disk = {
        "agents": {n: dataclasses.asdict(a) for n, a in cfg.agents.items()},
        "memory_stores": {n: dataclasses.asdict(m) for n, m in cfg.memory_stores.items()},
    }

    def _fake_update_config_locked(*args, **kwargs):
        doc: dict = json.loads(json.dumps(on_disk))
        if file_back_at_write is not None:
            # The app finished registering its file between the snapshot and
            # this locked write: the MATERIALIZED file (``<app>--<name>.json``,
            # declaring the row's binding as its ``name``) is on disk again when
            # the mutate runs.
            (Path(agents_tmp) / f"oncall-pack--{file_back_at_write}.json").write_text(
                json.dumps({"name": file_back_at_write})
            )
        result = kwargs["mutate"](doc)
        cfg.save()
        cfg.written_doc = result
        return result

    with (
        tempfile.TemporaryDirectory() as agents_tmp,
        patch("kiro_crew.dashboard.handlers.agents.KiroCrewConfig.load", return_value=cfg),
        patch("kiro_crew.dashboard.handlers.agents.list_agents", return_value=aim_agents_list),
        patch(
            "kiro_crew.dashboard.handlers.agents.installed_app_names",
            return_value=None if apps_unreadable else frozenset({"oncall-pack"}),
        ),
        patch(
            "kiro_crew.dashboard.handlers.agents.app_lifecycle_in_progress",
            return_value=lifecycle_in_progress,
        ),
        # What the installed manifests DECLARE (file-independent); the real
        # walk reads the apps directory, which these cases do not build.
        patch(
            "kiro_crew.dashboard.handlers.agents._apps_declaring_agents",
            return_value=dict(apps_declaring or {}),
        ),
        patch(
            "kiro_crew.dashboard.handlers.agents.kiro_agents_dir_path",
            side_effect=lambda: Path(agents_tmp),
        ),
        patch(
            "kiro_crew.dashboard.handlers.agents.update_config_locked",
            new=_fake_update_config_locked,
        ),
        patch("kiro_crew.dashboard.handlers.agents._sel", return_value=sel_mock),
        patch(PRIVATE_EXECUTION_GATE, return_value=True),
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
    async def test_prune_skips_kirocrew_owned_agents(self):
        """Agents with source='kirocrew' are never pruned."""
        agents = {
            "kirocrew": KiroCrewAgentConfig(kiro_agent="kirocrew", source="kirocrew"),
            "stale-aim": KiroCrewAgentConfig(kiro_agent="stale-aim", source="aim"),
        }
        cfg = _make_config(agents)
        aim_list = [_make_aim_agent("gpu-dev")]

        body = await _run_sync(cfg, aim_list)

        assert "stale-aim" in body["pruned"]
        assert "kirocrew" not in body["pruned"]
        assert "kirocrew" in cfg.agents

    @pytest.mark.asyncio
    async def test_an_apps_agent_whose_file_is_gone_is_pruned_like_a_packages(self):
        """The sync registers an installed app's agents as ``source="app"``
        (the discovery names the app that ships the file). Disabling or
        removing the app deletes the materialized file, so a row left behind
        would dispatch to a definition that is gone: it is pruned exactly as a
        package's is, while the user's own rows (``local``) never are."""
        agents = {
            "oncall-pack--triage": KiroCrewAgentConfig(
                kiro_agent="oncall-pack--triage", source="app"
            ),
            "mine": KiroCrewAgentConfig(kiro_agent="mine", source="local"),
        }
        cfg = _make_config(agents)
        aim_list = [_make_aim_agent("gpu-dev")]

        body = await _run_sync(cfg, aim_list)

        assert body["pruned"] == ["oncall-pack--triage"]
        assert "oncall-pack--triage" not in cfg.agents
        assert "mine" in cfg.agents

    @pytest.mark.asyncio
    async def test_an_app_row_is_kept_only_by_an_apps_own_file_of_that_name(self):
        """An ``app`` row survives the prune only when the discovery that
        answers to its name is ITSELF an app's file. A local or package agent
        that happens to share the name must not keep the row alive after the
        app is disabled: the row would then dispatch that unrelated definition
        in the app agent's name."""
        agents = {
            "triage": KiroCrewAgentConfig(kiro_agent="triage", source="app"),
            "scribe": KiroCrewAgentConfig(kiro_agent="scribe", source="app"),
        }
        cfg = _make_config(agents)
        discovered = [
            # A user's own file that shares the disabled app agent's name.
            AgentInfo(
                name="triage", filename="triage.json", description="", model="", source="local"
            ),
            # The app's own file: this row stays.
            AgentInfo(
                name="scribe",
                filename="oncall-pack--scribe.json",
                description="",
                model="",
                source="app",
                package="oncall-pack",
            ),
        ]

        body = await _run_sync(cfg, discovered)

        assert body["pruned"] == ["triage"]
        assert "triage" not in cfg.agents
        assert "scribe" in cfg.agents

    @pytest.mark.asyncio
    async def test_a_name_an_app_and_a_package_both_ship_keeps_the_app_row(self):
        """The listing dedups by name and can hand the name to an earlier-sorting
        PACKAGE file over the app's own; the app's file is then not in the
        listing at all. That is an ambiguous name, not evidence the app stopped
        shipping the agent: the dropped file leaves its provenance on the kept
        entry, and the app row is kept -- pruning it would archive a live
        member's memory over a filename collision."""
        agents = {"triage": KiroCrewAgentConfig(kiro_agent="triage", source="app")}
        cfg = _make_config(agents)
        discovered = [
            AgentInfo(
                name="triage",
                filename="acme-triage.json",
                description="",
                model="",
                source="package",
                package="acme",
                shadowed_sources=("app",),
            ),
        ]

        body = await _run_sync(cfg, discovered)

        assert body["pruned"] == []
        assert "triage" in cfg.agents

    @pytest.mark.asyncio
    async def test_an_app_row_is_kept_only_by_its_own_apps_file(self):
        """Two installed apps may both declare ``triage``. A row the sync
        registered from one of them (``source_app``) is kept only by THAT app's
        file: when its app is disabled, the other app's same-named file must not
        keep the row -- and its private memory -- alive under the other app's
        definition."""
        agents = {
            "triage": KiroCrewAgentConfig(
                kiro_agent="triage", source="app", source_app="oncall-pack"
            ),
        }
        cfg = _make_config(agents)
        discovered = [
            AgentInfo(
                name="triage",
                filename="acme-desk--triage.json",
                description="",
                model="",
                source="app",
                package="acme-desk",
            ),
        ]
        body = await _run_sync(cfg, discovered)
        assert body["pruned"] == ["triage"]
        # The row went with its app; acme-desk's same-named agent is not
        # registered in its place this pass (the name was taken when the
        # listing was read), so no row -- and no memory -- changes hands.
        assert "triage" not in cfg.written_doc["agents"]
        assert body["synced"] == []

    @pytest.mark.asyncio
    async def test_a_freshly_registered_app_row_records_its_app(self):
        cfg = _make_config({})
        discovered = [
            AgentInfo(
                name="scribe",
                filename="oncall-pack--scribe.json",
                description="",
                model="",
                source="app",
                package="oncall-pack",
            ),
        ]
        body = await _run_sync(cfg, discovered)
        assert body["synced"] == ["scribe"]
        assert cfg.written_doc["agents"]["scribe"]["source_app"] == "oncall-pack"

    @pytest.mark.asyncio
    async def test_a_legacy_app_row_is_attributed_once_when_one_app_ships_the_name(self):
        """A row from before ``source_app`` existed names no app. When exactly
        one installed app ships the name, the sync records that app on the row
        (in the locked write, only while the entry is unchanged); when two do,
        the row is kept unattributed and the gap is logged -- never guessed."""
        agents = {"triage": KiroCrewAgentConfig(kiro_agent="triage", source="app")}
        cfg = _make_config(agents)
        one = [
            AgentInfo(
                name="triage",
                filename="oncall-pack--triage.json",
                description="",
                model="",
                source="app",
                package="oncall-pack",
            ),
        ]
        body = await _run_sync(cfg, one)
        assert body["pruned"] == []
        assert cfg.written_doc["agents"]["triage"]["source_app"] == "oncall-pack"

        agents = {"triage": KiroCrewAgentConfig(kiro_agent="triage", source="app")}
        cfg = _make_config(agents)
        two = [
            AgentInfo(
                name="triage",
                filename="acme-desk--triage.json",
                description="",
                model="",
                source="app",
                package="acme-desk",
                shadowed_apps=("oncall-pack",),
            ),
        ]
        body = await _run_sync(cfg, two)
        assert body["pruned"] == []
        assert "triage" in cfg.agents
        # Nothing was written: no attribution, no prune.
        written = cfg.__dict__.get("written_doc")
        assert written is None or written["agents"]["triage"].get("source_app", "") == ""

    @pytest.mark.asyncio
    async def test_the_write_time_recheck_looks_only_at_the_rows_own_app(self):
        """The in-lock "file is back" re-check is scoped the same way: another
        app's ``<app>--triage.json`` declaring the name does not keep a row that
        records ``oncall-pack``."""
        agents = {
            "triage": KiroCrewAgentConfig(
                kiro_agent="triage", source="app", source_app="oncall-pack"
            ),
        }
        cfg = _make_config(agents)
        from kiro_crew.dashboard.handlers import agents as handlers

        with tempfile.TemporaryDirectory() as d:
            (Path(d) / "acme-desk--triage.json").write_text(json.dumps({"name": "triage"}))
            with patch.object(handlers, "kiro_agents_dir_path", lambda: Path(d)):
                assert handlers._app_agent_file_present("triage", "oncall-pack") is False
                assert handlers._app_agent_file_present("triage", "acme-desk") is True
                # A legacy row (no app recorded) is kept by any app's file, as before.
                assert handlers._app_agent_file_present("triage", "") is True
        body = await _run_sync(cfg, [_make_aim_agent("gpu-dev")])
        assert body["pruned"] == ["triage"]

    @pytest.mark.asyncio
    async def test_a_pre_existing_package_row_for_an_apps_file_is_restamped_as_the_apps(self):
        """A row an older sync registered as ``package`` for what discovery now
        classifies as an app's ``<app>--<agent>.json`` gets every app-row
        protection only if it IS an app row: the sync re-stamps it (source
        ``app`` + the app) in the locked write, and treats it as one in the same
        pass -- a lifecycle hold keeps it, and its own app's file keeps it."""
        agents = {"triage": KiroCrewAgentConfig(kiro_agent="triage", source="package")}
        cfg = _make_config(agents)
        app_file = [
            AgentInfo(
                name="triage",
                filename="oncall-pack--triage.json",
                description="",
                model="",
                source="app",
                package="oncall-pack",
            ),
        ]
        body = await _run_sync(cfg, app_file)
        assert body["pruned"] == []
        row = cfg.written_doc["agents"]["triage"]
        assert (row["source"], row["source_app"]) == ("app", "oncall-pack")
        # Treated as an app row in the SAME pass: with the app in transit, nothing
        # is decided about it -- where a package row's absent file would prune.
        agents = {"triage": KiroCrewAgentConfig(kiro_agent="triage", source="package")}
        cfg = _make_config(agents)
        body = await _run_sync(cfg, app_file, lifecycle_in_progress=True)
        assert body["pruned"] == []
        # A real package file of that name keeps the package spelling: not an app's.
        agents = {"triage": KiroCrewAgentConfig(kiro_agent="triage", source="package")}
        cfg = _make_config(agents)
        both = app_file + [
            AgentInfo(
                name="triage",
                filename="acme-triage.json",
                description="",
                model="",
                source="package",
                package="acme",
            ),
        ]
        body = await _run_sync(cfg, both)
        assert body["pruned"] == []
        written = cfg.__dict__.get("written_doc")
        assert written is None or written["agents"]["triage"]["source"] == "package"

    @pytest.mark.asyncio
    async def test_a_legacy_package_row_whose_app_file_is_in_transit_is_recognised_from_the_manifest(
        self,
    ):
        """The legacy row's only file-based evidence of being an app's is the
        ``<app>--<name>`` file -- which an app update removes and rewrites
        under its lock. Caught mid-update, the file is absent, ``apps_shipping``
        knows nothing, and the row would prune as a gone package agent with its
        memory archived. The installed manifests answer independently of the
        files: exactly one app declares the name, so the row is re-stamped as
        that app's and, with the lifecycle in progress, left for the next sync.
        A name no manifest declares stays a package row and prunes as before."""
        # The scan saw SOMETHING (an empty scan decides nothing) -- just not
        # triage; the something is a row already present, so nothing is added.
        others = [_make_aim_agent("gpu-dev")]

        def _agents() -> dict:
            return {
                "triage": KiroCrewAgentConfig(kiro_agent="triage", source="package"),
                "gpu-dev": KiroCrewAgentConfig(kiro_agent="gpu-dev", source="aim"),
            }

        cfg = _make_config(_agents())
        body = await _run_sync(
            cfg, others, lifecycle_in_progress=True, apps_declaring={"triage": {"oncall-pack"}}
        )
        assert body["pruned"] == []
        row = cfg.written_doc["agents"]["triage"]
        assert (row["source"], row["source_app"]) == ("app", "oncall-pack")
        # Two apps declaring the name: ambiguous, the row is left as it is (and,
        # as a package row with no file, pruned as before -- nothing is guessed).
        cfg = _make_config(_agents())
        body = await _run_sync(
            cfg, others, lifecycle_in_progress=True, apps_declaring={"triage": {"a", "b"}}
        )
        assert body["pruned"] == ["triage"], body
        # No manifest declares it: a plain gone package agent, pruned.
        cfg = _make_config(_agents())
        body = await _run_sync(cfg, others, lifecycle_in_progress=True)
        assert body["pruned"] == ["triage"], body

    def test_the_listing_keeps_the_provenance_of_a_same_name_file_it_dropped(self, tmp_path):
        """``list_agents`` records the sources of the same-name files its dedup
        dropped, so the sync can tell an ambiguous name from an absent one."""
        from kiro_crew import agent_discovery

        d = tmp_path / "agents"
        d.mkdir()
        (d / "acme-triage.json").write_text(json.dumps({"name": "triage", "prompt": "p"}))
        (d / "oncall-pack--triage.json").write_text(json.dumps({"name": "triage", "prompt": "a"}))
        with patch.object(
            agent_discovery, "installed_app_names", lambda: frozenset({"oncall-pack"})
        ):
            rows = {a.name: a for a in agent_discovery.list_agents(agents_dir=d)}
        kept = rows["triage"]
        # One file dispatches; the other's provenance survives on it -- the
        # kind AND the app, so the sync can attribute the row to its own app.
        assert {kept.source, *kept.shadowed_sources} == {"package", "app"}
        assert kept.shadowed_apps == ("oncall-pack",)

    @pytest.mark.asyncio
    async def test_an_unreadable_apps_directory_creates_and_restamps_no_app_row(self):
        """The retention-safe classification ("every ``--`` file is an app's while
        the apps directory cannot be read") protects rows that EXIST; it is no
        basis for creating one. A user's own ``foo--bar.json`` would otherwise be
        registered as an app row with a private store and pruned -- memory
        archived -- by the next readable sync. Nothing is created or re-stamped on
        provenance that could not be read; the file waits for a readable sync."""
        agents = {
            "legacy": KiroCrewAgentConfig(kiro_agent="legacy", source="package"),
            "gpu-dev": KiroCrewAgentConfig(kiro_agent="gpu-dev", source="aim"),
        }
        cfg = _make_config(agents)
        discovered = [
            AgentInfo(
                name="bar",
                filename="foo--bar.json",
                description="",
                model="",
                source="app",
                package="foo",
            ),
            AgentInfo(
                name="legacy",
                filename="foo--legacy.json",
                description="",
                model="",
                source="app",
                package="foo",
            ),
            _make_aim_agent("gpu-dev"),
        ]
        body = await _run_sync(cfg, discovered, apps_unreadable=True)
        assert body["synced"] == [] and "bar" not in cfg.agents
        # The legacy package row is neither pruned (its name is in the listing)
        # nor re-stamped as ``foo``'s on the unreadable reading.
        assert body["pruned"] == []
        assert cfg.agents["legacy"].source == "package" and not cfg.agents["legacy"].source_app
        # Once the directory reads again, the same listing registers the file.
        cfg = _make_config(dict(agents))
        body = await _run_sync(cfg, discovered)
        assert "bar" in body["synced"]

    @pytest.mark.asyncio
    async def test_an_unreadable_apps_directory_prunes_no_app_row(self):
        """A failed read of the apps directory is not an empty apps directory:
        with the failure every app row would read as "its app is gone" and be
        pruned with its memory archived. App rows are kept until the directory
        reads again; package rows are still decided as usual."""
        agents = {
            "triage": KiroCrewAgentConfig(kiro_agent="oncall-pack--triage", source="app"),
            "stale-aim": KiroCrewAgentConfig(kiro_agent="stale-aim", source="aim"),
        }
        cfg = _make_config(agents)
        # During the failure the listing classifies the app-shaped file as an
        # app's (retention-safe) -- and even a listing that did NOT would leave
        # the row alone, because the decision is keyed on the failure, not on
        # what the listing says about the file.
        discovered = [
            AgentInfo(
                name="triage",
                filename="oncall-pack--triage.json",
                description="",
                model="",
                source="local",
            ),
            _make_aim_agent("gpu-dev"),
        ]

        body = await _run_sync(cfg, discovered, apps_unreadable=True)

        assert body["pruned"] == ["stale-aim"]
        assert "triage" in cfg.agents

    @pytest.mark.asyncio
    async def test_an_app_lifecycle_in_progress_prunes_no_app_row(self):
        """An update deregisters an app's materialized agents and registers
        them again under the app's lifecycle lock; a scan alongside it sees the
        files absent. The sync cannot take that lock (it holds the config lock,
        which the lifecycle routes take inside theirs), so while any lifecycle
        lock is held every app row is left for the next sync -- a member whose
        file is merely in transit is not pruned and its memory not archived.
        Package rows are decided as usual."""
        agents = {
            "triage": KiroCrewAgentConfig(kiro_agent="oncall-pack--triage", source="app"),
            "stale-aim": KiroCrewAgentConfig(kiro_agent="stale-aim", source="aim"),
        }
        cfg = _make_config(agents)
        body = await _run_sync(cfg, [_make_aim_agent("gpu-dev")], lifecycle_in_progress=True)
        assert body["pruned"] == ["stale-aim"]
        assert "triage" in cfg.agents
        # (With no operation in flight the same absence prunes:
        # test_an_apps_agent_whose_file_is_gone_is_pruned_like_a_packages.)

    @pytest.mark.asyncio
    async def test_an_app_file_back_on_disk_at_write_time_keeps_the_row(self):
        """The narrower window: no lifecycle operation was in flight when the
        sync looked, but one began and finished registering the file before
        the locked write. The write re-checks the file for an app row -- under
        the name the bridge actually writes, ``<app>--<name>.json`` declaring the
        row's binding, since an app row's ``kiro_agent`` is the DECLARED name and
        a bare ``<name>.json`` check would call every app agent missing -- and
        keeps the row; the answer does not report it pruned."""
        agents = {
            "triage": KiroCrewAgentConfig(kiro_agent="triage", source="app"),
            "stale-aim": KiroCrewAgentConfig(kiro_agent="stale-aim", source="aim"),
        }
        cfg = _make_config(agents)
        body = await _run_sync(cfg, [_make_aim_agent("gpu-dev")], file_back_at_write="triage")
        assert body["pruned"] == ["stale-aim"]
        assert "triage" in cfg.written_doc["agents"]
        assert "stale-aim" not in cfg.written_doc["agents"]

    @pytest.mark.asyncio
    async def test_prune_skips_user_created_agents(self):
        """Agents with source='builtin' (user-created) are never pruned."""
        agents = {
            "my-custom": KiroCrewAgentConfig(kiro_agent="my-custom", source="builtin"),
            "stale-aim": KiroCrewAgentConfig(kiro_agent="stale-aim", source="aim"),
        }
        cfg = _make_config(agents)
        aim_list = [_make_aim_agent("gpu-dev")]

        body = await _run_sync(cfg, aim_list)

        assert "stale-aim" in body["pruned"]
        assert "my-custom" not in body["pruned"]
        assert "my-custom" in cfg.agents

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
    async def test_package_prune_archives_private_generation(self):
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
        with pytest.raises(UnknownMemoryStore, match="archived"):
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
        assert cfg.written_doc["memory_stores"][store] == {
            "owner_member": "oncall-triage",
            "memory_version": 2,
            "description": "",
            "embedding_provider": "",
        }

    @pytest.mark.asyncio
    async def test_reinstalled_package_member_gets_fresh_memory(self):
        """A retired store is retained but never inherited by a same-name reinstall."""
        cfg = _make_config({"oncall": KiroCrewAgentConfig(kiro_agent="oncall", source="package")})
        retired_store = provision_member_memory(cfg, "oncall")
        assert archive_member_memory_store(retired_store, "oncall")
        del cfg.agents["oncall"]

        body = await _run_sync(cfg, [_make_aim_agent("oncall")])

        assert body["synced"] == ["oncall"]
        fresh = cfg.agents["oncall"].memory_store
        assert fresh != retired_store
        assert retired_store in cfg.memory_stores
        assert cfg.written_doc["memory_stores"][fresh]["owner_member"] == "oncall"


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
            patch(PRIVATE_EXECUTION_GATE, return_value=True),
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
