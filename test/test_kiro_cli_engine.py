"""``agent.kiro_cli_engine``: the opt-in V2 engine for a Kiro CLI harness choice.

``"auto"`` (the default) must leave a stored Kiro CLI choice exactly where the
recorded decision puts it; ``"v2"`` keeps it on the Kiro CLI harness and pins
``--agent-engine v2`` on both argv builders while the pinned kiro-cli lists v2.
"""

from __future__ import annotations

import json
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from aiohttp import web
from aiohttp.test_utils import TestClient, TestServer

from kiro_crew import agent as agent_mod
from kiro_crew import kiro_cli, kiro_cli_engine
from kiro_crew import sandbox as sandbox_mod
from kiro_crew.acp import client as client_mod
from kiro_crew.acp.client import AcpClient
from kiro_crew.acp.harness import harness_for
from kiro_crew.acp.harness.base import SpawnContext
from kiro_crew.acp_backends import ACP_BACKEND_KAS, ACP_BACKEND_KIRO
from kiro_crew.acp_backends import STORED_BACKEND_KIRO as KIRO_STORED
from kiro_crew.config.loader import KiroCrewConfig
from kiro_crew.config.loader_locations import config_path

#: kiro-cli 3.0.0's ``acp --help`` engine block, verbatim in its parts that matter.
ACP_HELP_3_0 = """Options:
      --agent <AGENT>
          Name of the agent to use when starting the first session

      --agent-engine <ENGINE>
          Agent engine to use: "v1", "v2", or "v3". Overrides the acp.agentEngine setting; v2 when
          neither is set

          [possible values: v2, v1, v3]

      --auth-method <METHOD>
          Authentication owner for the v3 engine.
"""
ALL_ENGINES = ("v2", "v1", "v3")
PINNED = "/pinned/kiro-cli"


def _write_agent_config(agent: dict) -> None:
    path = config_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps({"agent": agent}), encoding="utf-8")


def _load(agent: dict) -> KiroCrewConfig:
    _write_agent_config(agent)
    return KiroCrewConfig.load()


@pytest.fixture
def registry(monkeypatch):
    """The process-local evidence flags, reset for each test."""
    from kiro_crew.agent_sdk import backends

    monkeypatch.setattr(backends, "_selectable", set(backends._selectable))
    monkeypatch.setattr(backends, "_host_unservable", {})
    monkeypatch.setattr(backends, "_kiro_cli_offers_kas", False)
    monkeypatch.setattr(backends, "_kiro_cli_retired_v2", False)
    return backends


@pytest.fixture
def pinned_engines(monkeypatch):
    """Make the pinned-binary engine probe answer *engines*, recording each probe."""
    probes: list[str | None] = []

    def _set(engines: tuple[str, ...] | None) -> list[str | None]:
        def _probe(kiro_bin: str | None = None) -> tuple[str, ...] | None:
            probes.append(kiro_bin)
            return engines

        monkeypatch.setattr(kiro_cli_engine, "pinned_kiro_cli_engines", _probe)
        return probes

    return _set


@pytest.fixture(autouse=True)
def _fresh_process(_floor_monkeypatch):
    """Each test is a fresh process: no fallback logged, the engine not yet read."""
    _floor_monkeypatch.setattr(kiro_cli_engine, "_warned", set())
    _floor_monkeypatch.setattr(kiro_cli_engine, "_process_engine", None)


def _restart(monkeypatch) -> None:
    """The process is replaced: the next use reads the saved engine again."""
    monkeypatch.setattr(kiro_cli_engine, "_process_engine", None)


# ── The argv helper ──


class TestEngineArgv:
    def test_unset_is_auto_and_adds_nothing(self, pinned_engines):
        probes = pinned_engines(ALL_ENGINES)
        assert kiro_cli_engine.stored_kiro_cli_engine() == "auto"
        assert kiro_cli_engine.kiro_cli_engine_argv(PINNED) == []
        assert probes == [], "auto must not spend a probe"

    def test_auto_adds_nothing(self, pinned_engines):
        _write_agent_config({"kiro_cli_engine": "auto"})
        pinned_engines(ALL_ENGINES)
        assert kiro_cli_engine.kiro_cli_engine_argv(PINNED) == []

    def test_v2_pins_the_engine_when_kiro_cli_lists_it(self, pinned_engines):
        _write_agent_config({"kiro_cli_engine": "v2"})
        probes = pinned_engines(ALL_ENGINES)
        assert kiro_cli_engine.kiro_cli_engine_argv(PINNED) == ["--agent-engine", "v2"]
        assert probes == [PINNED]

    def test_an_old_kiro_cli_without_the_selector_keeps_todays_argv(self, pinned_engines, caplog):
        pinned_engines(())
        assert kiro_cli_engine.kiro_cli_engine_argv(PINNED, "v2") == []
        assert "no --agent-engine selector" in caplog.text

    def test_a_kiro_cli_that_retired_v2_keeps_todays_argv(self, pinned_engines, caplog):
        pinned_engines(("v3",))
        assert kiro_cli_engine.kiro_cli_engine_argv(PINNED, "v2") == []
        assert "no longer offers v2" in caplog.text

    def test_a_retired_v2_seen_at_spawn_moves_the_next_load(self, registry, pinned_engines):
        """kiro-cli updated in place without v2: the next load follows ``auto``."""
        registry.confirm_kiro_cli_offers_kas()
        _write_agent_config({"acp_backend": KIRO_STORED, "kiro_cli_engine": "v2"})
        assert KiroCrewConfig.load().agent.acp_backend == ACP_BACKEND_KIRO
        pinned_engines(("v3",))
        assert kiro_cli_engine.kiro_cli_engine_argv(PINNED) == []
        assert KiroCrewConfig.load().agent.acp_backend == ACP_BACKEND_KAS

    def test_an_unknown_probe_keeps_todays_argv(self, pinned_engines, caplog):
        pinned_engines(None)
        assert kiro_cli_engine.kiro_cli_engine_argv(PINNED, "v2") == []
        assert "could not be read" in caplog.text

    def test_the_fallback_is_logged_once_per_binary(self, pinned_engines, caplog):
        pinned_engines(())
        kiro_cli_engine.kiro_cli_engine_argv(PINNED, "v2")
        kiro_cli_engine.kiro_cli_engine_argv(PINNED, "v2")
        assert caplog.text.count("no --agent-engine selector") == 1

    @pytest.mark.parametrize("stored", ["v3", "V2", "v1", 2, True, None, ["v2"]])
    def test_anything_but_v2_reads_as_auto(self, stored):
        _write_agent_config({"kiro_cli_engine": stored})
        assert kiro_cli_engine.stored_kiro_cli_engine() == "auto"


class TestPinnedProbe:
    def test_a_binary_that_is_not_the_pinned_one_is_never_run(self, monkeypatch):
        """The probe runs outside the sandbox, so a ``PATH`` binary is unknown."""
        monkeypatch.setattr(kiro_cli, "pin_kiro_cli", lambda: (PINNED, False))
        ran: list[str] = []
        monkeypatch.setattr(
            kiro_cli, "acp_agent_engines_at", lambda b: ran.append(b) or ALL_ENGINES
        )
        assert kiro_cli_engine.pinned_kiro_cli_engines("/agent/writable/kiro-cli") is None
        assert ran == []

    def test_no_pin_is_unknown(self, monkeypatch):
        monkeypatch.setattr(kiro_cli, "pin_kiro_cli", lambda: (None, False))
        assert kiro_cli_engine.pinned_kiro_cli_engines() is None

    def test_acp_help_is_parsed_and_cached(self, monkeypatch, tmp_path):
        binary = tmp_path / "kiro-cli"
        binary.write_text("", encoding="utf-8")
        calls: list[list[str]] = []

        class _Done:
            returncode = 0
            stdout = ACP_HELP_3_0
            stderr = ""

        def _run(argv, **kwargs):
            calls.append(argv)
            return _Done()

        monkeypatch.setattr(kiro_cli, "_acp_engines_cache", {})
        monkeypatch.setattr(kiro_cli.subprocess, "run", _run)
        assert kiro_cli.acp_agent_engines_at(str(binary)) == ALL_ENGINES
        assert kiro_cli.acp_agent_engines_at(str(binary)) == ALL_ENGINES
        assert calls == [[str(binary), "acp", "--help"]]

    def test_the_floor_probe_answers_the_engine_reader_without_a_second_spawn(
        self, monkeypatch, tmp_path
    ):
        binary = tmp_path / "kiro-cli"
        binary.write_text("", encoding="utf-8")
        calls: list[list[str]] = []

        class _Done:
            returncode = 0
            stdout = ACP_HELP_3_0
            stderr = ""

        def _run(argv, **kwargs):
            calls.append(argv)
            return _Done()

        monkeypatch.setattr(kiro_cli, "_acp_engines_cache", {})
        monkeypatch.setattr(kiro_cli.subprocess, "run", _run)
        assert kiro_cli.kas_relay_help(str(binary)) is not None
        assert kiro_cli.acp_agent_engines_at(str(binary)) == ALL_ENGINES
        assert len(calls) == 1

    def test_a_failed_probe_is_unknown(self, monkeypatch, tmp_path):
        binary = tmp_path / "kiro-cli"
        binary.write_text("", encoding="utf-8")

        def _boom(argv, **kwargs):
            raise OSError("cannot exec")

        monkeypatch.setattr(kiro_cli, "_acp_engines_cache", {})
        monkeypatch.setattr(kiro_cli.subprocess, "run", _boom)
        assert kiro_cli.acp_agent_engines_at(str(binary)) is None


# ── Spawn argv on both builders ──


def _ctx(tmp_path) -> SpawnContext:
    return SpawnContext(
        agent="a", work_dir=str(tmp_path), model=None, environ={}, home=Path(tmp_path)
    )


@pytest.fixture
def kiro_spawn_ready(monkeypatch):
    async def _bin(*, environ, home):
        return PINNED

    monkeypatch.setattr(client_mod, "_resolve_kiro_bin_for_spawn", _bin)
    monkeypatch.setattr(agent_mod, "ensure_agent_materialized", lambda agent: None)
    monkeypatch.setattr(agent_mod, "require_fork_governance", lambda agent, work_dir: None)
    monkeypatch.setattr(
        sandbox_mod, "delegated_workspace_exposes_sealed_target", lambda work_dir: ""
    )


class TestKiroHarnessArgv:
    @pytest.mark.asyncio
    @pytest.mark.parametrize(
        ("stored", "tail"),
        [
            (None, []),
            ("auto", []),
            ("v2", ["--agent-engine", "v2"]),
        ],
    )
    async def test_the_engine_flag_follows_the_setting(
        self, stored, tail, kiro_spawn_ready, pinned_engines, tmp_path
    ):
        if stored is not None:
            _write_agent_config({"kiro_cli_engine": stored})
        pinned_engines(ALL_ENGINES)
        plan = await harness_for(ACP_BACKEND_KIRO).resolve_spawn(_ctx(tmp_path))
        assert plan.argv == [PINNED, "acp", "--agent", "a", *tail]

    @pytest.mark.asyncio
    async def test_v2_on_an_old_kiro_cli_keeps_the_argv(
        self, kiro_spawn_ready, pinned_engines, tmp_path
    ):
        _write_agent_config({"kiro_cli_engine": "v2"})
        pinned_engines(())
        plan = await harness_for(ACP_BACKEND_KIRO).resolve_spawn(_ctx(tmp_path))
        assert plan.argv == [PINNED, "acp", "--agent", "a"]

    @pytest.mark.asyncio
    async def test_the_kas_harness_is_untouched(self, pinned_engines):
        """KAS keeps its own ``--agent-engine v3`` launch; the setting is not read there."""
        from kiro_crew.acp import kas_transport

        assert kas_transport.KAS_RELAY_ENGINE == "v3"


class TestAcpClientKiroArm:
    """The client's own Kiro CLI argv (used by knowledge/llm_pool and the placeholder client)."""

    @pytest.mark.asyncio
    @pytest.mark.parametrize(("stored", "tail"), [(None, []), ("v2", ["--agent-engine", "v2"])])
    async def test_the_engine_flag_follows_the_setting(
        self, stored, tail, pinned_engines, tmp_path
    ):
        if stored is not None:
            _write_agent_config({"kiro_cli_engine": stored})
        pinned_engines(ALL_ENGINES)
        fake = tmp_path / "kiro-cli"
        fake.write_bytes(b"#!/bin/sh\n")
        fake.chmod(0o755)
        wrapped: dict[str, object] = {}

        def capture_wrap(argv, mode, **kwargs):
            wrapped["argv"] = list(argv)
            return list(argv), None

        with (
            patch.object(client_mod, "_resolve_kiro_bin", return_value=str(fake)),
            patch.object(client_mod, "wrap_argv", side_effect=capture_wrap),
            patch.object(client_mod, "assert_voice_runtime_outside_agent_workspace"),
            patch.object(client_mod, "cgroup_scope_argv", side_effect=lambda argv: list(argv)),
            patch("asyncio.create_subprocess_exec", AsyncMock(side_effect=RuntimeError("stop"))),
        ):
            client = AcpClient(work_dir=tmp_path / "workspace")
            with pytest.raises(RuntimeError, match="stop"):
                await client._spawn()
        argv = wrapped["argv"]
        # The ``--agent`` value may be the per-spawn skill-view alias, so it is not pinned.
        assert argv[:3] == [str(fake), "acp", "--agent"]
        assert argv[4:] == tail


# ── The pick: auto unchanged, v2 stays on Kiro CLI ──


class TestThePick:
    def test_auto_without_evidence_is_unchanged(self, registry):
        assert _load({"acp_backend": KIRO_STORED}).agent.acp_backend == ACP_BACKEND_KIRO

    @pytest.mark.parametrize("engine", [None, "auto"])
    def test_auto_with_confirmed_v3_still_runs_on_kas(self, engine, registry):
        """``auto`` is main's behaviour exactly: the recorded decision's move onto KAS."""
        registry.confirm_kiro_cli_offers_kas()
        agent = {"acp_backend": KIRO_STORED}
        if engine is not None:
            agent["kiro_cli_engine"] = engine
        cfg = _load(agent)
        assert cfg.agent.acp_backend == ACP_BACKEND_KAS
        assert cfg.agent.kiro_cli_engine == "auto"

    def test_v2_keeps_the_pick_on_kiro_cli_despite_v3(self, registry):
        registry.confirm_kiro_cli_offers_kas()
        cfg = _load({"acp_backend": KIRO_STORED, "kiro_cli_engine": "v2"})
        assert cfg.agent.acp_backend == ACP_BACKEND_KIRO
        assert cfg.agent.kiro_cli_engine == "v2"
        provider = cfg.create_provider_factory()(session_key="test:v2-pick", agent="")
        assert provider.is_kiro_backend is True

    def test_v2_on_a_kiro_cli_that_retired_v2_follows_auto(self, registry, pinned_engines):
        registry.confirm_kiro_cli_offers_kas()
        _write_agent_config({"acp_backend": KIRO_STORED, "kiro_cli_engine": "v2"})
        pinned_engines(("v3",))
        registry.record_kiro_cli_v2_evidence()
        assert KiroCrewConfig.load().agent.acp_backend == ACP_BACKEND_KAS

    def test_an_unknown_v2_probe_records_nothing(self, registry, pinned_engines):
        _write_agent_config({"acp_backend": KIRO_STORED, "kiro_cli_engine": "v2"})
        pinned_engines(None)
        registry.record_kiro_cli_v2_evidence()
        assert registry._kiro_cli_retired_v2 is False

    def test_auto_never_spends_the_v2_probe(self, registry, pinned_engines):
        _write_agent_config({"acp_backend": KIRO_STORED})
        probes = pinned_engines(("v3",))
        registry.record_kiro_cli_v2_evidence()
        assert probes == []

    def test_v2_does_not_move_a_kas_pick(self, registry):
        """The setting is about a Kiro CLI choice; a KAS choice stays KAS."""
        registry.confirm_kiro_cli_offers_kas()
        cfg = _load({"acp_backend": "kas", "kiro_cli_engine": "v2"})
        assert cfg.agent.acp_backend == ACP_BACKEND_KAS

    def test_the_crewmate_pin_follows_v2(self, registry, monkeypatch):
        """H17: a crewmate's stored ``kiro`` pin reads the same rule."""
        from kiro_crew.crewmate_harness import CrewBackendPin

        registry.confirm_kiro_cli_offers_kas()
        assert CrewBackendPin("c", KIRO_STORED, kas_bundle=False).resolve() == ACP_BACKEND_KAS
        _write_agent_config({"kiro_cli_engine": "v2"})
        _restart(monkeypatch)
        assert CrewBackendPin("c", KIRO_STORED, kas_bundle=False).resolve() == ACP_BACKEND_KIRO

    def test_the_per_process_verdict_keeps_v2_on_kiro_cli(
        self, registry, monkeypatch, pinned_engines
    ):
        """A process outside the gateway (``kirocrew chat``) reaches the same answer."""
        from kiro_crew import agent_sdk

        monkeypatch.setattr(registry, "_kiro_cli_kas_probed", False)
        monkeypatch.setattr(agent_sdk, "kas_engine_verdict", lambda: (True, None))
        pinned_engines(ALL_ENGINES)
        _write_agent_config({"acp_backend": KIRO_STORED, "kiro_cli_engine": "v2"})
        registry.ensure_kiro_cli_kas_verdict()
        assert registry.kiro_cli_offers_kas() is True
        assert KiroCrewConfig.load().agent.acp_backend == ACP_BACKEND_KIRO


class TestRestartOnly:
    """``agent.kiro_cli_engine`` is a restart field: a live save moves neither half."""

    def test_a_save_while_running_keeps_the_pick_and_the_argv(
        self, registry, pinned_engines, monkeypatch
    ):
        registry.confirm_kiro_cli_offers_kas()
        pinned_engines(ALL_ENGINES)
        _write_agent_config({"acp_backend": KIRO_STORED, "kiro_cli_engine": "v2"})
        assert KiroCrewConfig.load().agent.acp_backend == ACP_BACKEND_KIRO
        assert kiro_cli_engine.kiro_cli_engine_argv(PINNED) == ["--agent-engine", "v2"]
        # The user saves "auto" without restarting: this process keeps v2 on both halves.
        _write_agent_config({"acp_backend": KIRO_STORED, "kiro_cli_engine": "auto"})
        assert KiroCrewConfig.load().agent.acp_backend == ACP_BACKEND_KIRO
        assert kiro_cli_engine.kiro_cli_engine_argv(PINNED) == ["--agent-engine", "v2"]
        _restart(monkeypatch)
        assert KiroCrewConfig.load().agent.acp_backend == ACP_BACKEND_KAS
        assert kiro_cli_engine.kiro_cli_engine_argv(PINNED) == []


class TestDoctor:
    def test_a_kiro_pick_resolved_to_kas_still_gets_the_kas_rows(self, monkeypatch, capsys):
        """A policy floor moved the pick onto KAS: the v2 row must not hide KAS diagnostics."""
        from kiro_crew import cli_doctor

        ran: list[str] = []
        monkeypatch.setattr(cli_doctor, "_kiro_cli_selected", lambda: True)
        monkeypatch.setattr(cli_doctor, "_report_kas_backend", lambda issues: ran.append("kas"))
        monkeypatch.setattr(cli_doctor, "_report_kiro_cli_engine_v2", lambda: True)
        _write_agent_config({"acp_backend": "kas", "kiro_cli_engine": "v2"})
        cli_doctor._doctor_kas([])
        assert ran == ["kas"]

    def test_a_kiro_pick_on_v2_skips_the_kas_rows(self, monkeypatch):
        from kiro_crew import cli_doctor

        ran: list[str] = []
        monkeypatch.setattr(cli_doctor, "_kiro_cli_selected", lambda: True)
        monkeypatch.setattr(cli_doctor, "_report_kas_backend", lambda issues: ran.append("kas"))
        monkeypatch.setattr(cli_doctor, "_report_kiro_cli_engine_v2", lambda: True)
        _write_agent_config({"acp_backend": KIRO_STORED, "kiro_cli_engine": "v2"})
        cli_doctor._doctor_kas([])
        assert ran == []


class TestNoNag:
    def test_the_save_kas_offer_is_silent_for_v2(self, registry, monkeypatch):
        from kiro_crew.kiro_on_kas_notice import kiro_selection_runs_on_kas

        registry.confirm_kiro_cli_offers_kas()
        _write_agent_config({"acp_backend": KIRO_STORED})
        assert kiro_selection_runs_on_kas() is True
        _write_agent_config({"acp_backend": KIRO_STORED, "kiro_cli_engine": "v2"})
        _restart(monkeypatch)
        assert kiro_selection_runs_on_kas() is False


# ── The config surface ──


@web.middleware
async def _owner_identity(request, handler):
    request["user"] = "local-app"
    request["app"] = ""
    state = request.app.get("state")
    if state is not None:
        state.owner_id = ""
    return await handler(request)


async def _patch_engine(value: object) -> tuple[int, dict]:
    from kiro_crew.dashboard.handlers import api_kirocrew_config_patch

    app = web.Application(middlewares=[_owner_identity])
    app["state"] = MagicMock()
    app.router.add_patch("/api/config/kirocrew", api_kirocrew_config_patch)
    async with TestClient(TestServer(app)) as client:
        resp = await client.patch(
            "/api/config/kirocrew", json={"path": "agent.kiro_cli_engine", "value": value}
        )
        return resp.status, await resp.json()


class TestSchema:
    def test_the_patch_schema_lists_auto_and_v2_only(self):
        from kiro_crew.dashboard.handlers.core_config_schema import _EDITABLE_CONFIG

        assert _EDITABLE_CONFIG["agent.kiro_cli_engine"] == {
            "type": "enum",
            "values": ["auto", "v2"],
        }

    @pytest.mark.asyncio
    @pytest.mark.parametrize("bad", ["v3", "v1", "V2", "", 2, None])
    async def test_a_patch_with_another_value_is_refused(self, bad):
        _write_agent_config({"approval_mode": "auto"})
        status, _ = await _patch_engine(bad)
        assert status == 400
        stored = json.loads(config_path().read_text(encoding="utf-8"))
        assert "kiro_cli_engine" not in stored["agent"]

    @pytest.mark.asyncio
    @pytest.mark.parametrize("good", ["auto", "v2"])
    async def test_a_patch_with_auto_or_v2_is_stored(self, good):
        _write_agent_config({"approval_mode": "auto"})
        status, _ = await _patch_engine(good)
        assert status == 200
        stored = json.loads(config_path().read_text(encoding="utf-8"))
        assert stored["agent"]["kiro_cli_engine"] == good

    def test_the_dataclass_default_is_auto_and_needs_a_restart(self):
        from dataclasses import fields

        from kiro_crew.config.sections_agent import AgentConfig

        field = next(f for f in fields(AgentConfig) if f.name == "kiro_cli_engine")
        assert field.default == "auto"
        assert field.metadata["enum"] == ["auto", "v2"]
        assert field.metadata["restart"] is True

    def test_a_saved_value_round_trips(self):
        cfg = _load({"kiro_cli_engine": "v2"})
        assert cfg.to_dict()["agent"]["kiro_cli_engine"] == "v2"
