"""``agent.harness_sandbox``: an opt-in floor that runs one enforced harness sandboxed.

With ``agent.sandbox: "off"`` an enforced harness is refused at launch because its
credential mask would never be applied. A floor for that harness raises ITS sessions'
tier, so the mask is applied, while every other harness keeps ``agent.sandbox``. The
floor only raises, is read for enforced harnesses only, and changes nothing without
an entry.
"""

from __future__ import annotations

import json
import logging
import os
from pathlib import Path
from unittest.mock import patch

import pytest

from kiro_crew import platform_compat
from kiro_crew.acp import launch as launch_mod
from kiro_crew.acp.transport_errors import AcpToolGateUnroutable
from kiro_crew.agent_sdk import tool_gate as gate
from kiro_crew.agent_sdk.backends import ACP_BACKEND_KIRO, ACP_BACKEND_OPENCODE
from kiro_crew.config.loader import KiroCrewConfig

_FLOOR = {ACP_BACKEND_OPENCODE: "auto"}


def _pin_host(monkeypatch: pytest.MonkeyPatch, *, backend: str, windows: bool = False) -> None:
    """Pin the probes ``credential_mask_applies`` reads; never the operator config."""
    from kiro_crew import sandbox

    monkeypatch.setattr(platform_compat, "IS_WINDOWS", windows)
    monkeypatch.setattr(sandbox, "_governance_sandbox_floor", lambda: None)
    monkeypatch.setattr(sandbox, "detect_backend", lambda **_: backend)
    monkeypatch.setattr(sandbox, "_inside_kirocrew_sandbox", lambda: False)
    monkeypatch.setattr(sandbox, "_allow_unsandboxed_exec", lambda: False)


@pytest.mark.parametrize(
    "backend,mode,floors,expected",
    [
        (ACP_BACKEND_OPENCODE, "off", {ACP_BACKEND_OPENCODE: "auto"}, "standard"),
        (ACP_BACKEND_OPENCODE, "off", {ACP_BACKEND_OPENCODE: "strict"}, "strict"),
        (ACP_BACKEND_OPENCODE, "auto", {ACP_BACKEND_OPENCODE: "strict"}, "strict"),
        # Never lowers a tier.
        (ACP_BACKEND_OPENCODE, "strict", {ACP_BACKEND_OPENCODE: "auto"}, "strict"),
        (ACP_BACKEND_OPENCODE, "auto", {ACP_BACKEND_OPENCODE: "auto"}, "auto"),
        # No entry, or an "off" entry, is no floor.
        (ACP_BACKEND_OPENCODE, "off", {}, "off"),
        (ACP_BACKEND_OPENCODE, "off", {ACP_BACKEND_OPENCODE: "off"}, "off"),
        # A harness this core does not enforce keeps agent.sandbox.
        (ACP_BACKEND_KIRO, "off", {ACP_BACKEND_KIRO: "auto", "kiro": "auto"}, "off"),
    ],
)
def test_the_floor_only_raises_and_only_for_an_enforced_harness(
    backend: str, mode: str, floors: dict, expected: str
) -> None:
    assert gate.harness_sandbox_mode(backend, mode, floors) == expected


def test_an_unknown_floor_value_sets_no_floor_and_says_so_once(
    caplog: pytest.LogCaptureFixture,
) -> None:
    with caplog.at_level(logging.WARNING, logger=gate.__name__):
        for _ in range(2):
            mode = gate.harness_sandbox_mode(ACP_BACKEND_OPENCODE, "off", {"opencode": "standard"})
    assert mode == "off"
    assert caplog.text.count("agent.harness_sandbox.opencode") == 1


def test_sandbox_off_with_a_floor_starts_the_harness_with_its_credential_mask(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _pin_host(monkeypatch, backend="namespace")
    mode = gate.harness_sandbox_mode(ACP_BACKEND_OPENCODE, "off", _FLOOR)

    hidden = launch_mod._sandbox_preflight(ACP_BACKEND_OPENCODE, mode)

    assert os.path.join(os.path.expanduser("~"), ".aws") in hidden


def test_sandbox_off_without_a_floor_is_still_refused_with_todays_message(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _pin_host(monkeypatch, backend="namespace")
    mode = gate.harness_sandbox_mode(ACP_BACKEND_OPENCODE, "off", {})

    with pytest.raises(AcpToolGateUnroutable) as refused:
        launch_mod._sandbox_preflight(ACP_BACKEND_OPENCODE, mode)

    assert str(refused.value) == gate.sandbox_floor_refusal(ACP_BACKEND_OPENCODE, "off")
    assert "Set agent.sandbox to 'auto' or 'strict'" in str(refused.value)


def test_native_windows_with_a_floor_is_still_refused_with_the_host_message(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _pin_host(monkeypatch, backend="none", windows=True)
    mode = gate.harness_sandbox_mode(ACP_BACKEND_OPENCODE, "off", _FLOOR)

    with pytest.raises(AcpToolGateUnroutable, match="native Windows"):
        launch_mod._sandbox_preflight(ACP_BACKEND_OPENCODE, mode)


@pytest.mark.parametrize("floors,expected", [(_FLOOR, "standard"), ({}, "off")])
def test_the_provider_factory_starts_the_session_at_the_floored_tier(
    floors: dict, expected: str
) -> None:
    """Through the REAL factory, so the one tier the preflight and the wrap read is pinned."""
    cfg = KiroCrewConfig()
    cfg.agent.acp_backend = ACP_BACKEND_OPENCODE
    cfg.agent.sandbox = "off"
    cfg.agent.harness_sandbox = dict(floors)

    provider = cfg.create_provider_factory()(session_key="test:floor", agent="")

    assert provider.client.backend == ACP_BACKEND_OPENCODE
    assert provider.client._sandbox_mode == expected


def test_the_floor_is_read_from_config_json(tmp_path: Path) -> None:
    cfg_file = tmp_path / "config.json"
    cfg_file.write_text(
        json.dumps({"agent": {"sandbox": "off", "harness_sandbox": {"opencode": "strict"}}}),
        encoding="utf-8",
    )
    with patch("kiro_crew.config.loader.config_path", return_value=cfg_file):
        loaded = KiroCrewConfig.load()

    assert loaded.agent.harness_sandbox == {"opencode": "strict"}


@pytest.mark.parametrize("floors,refused", [(_FLOOR, False), ({}, True)])
def test_the_harness_row_and_the_saves_read_the_floor(
    monkeypatch: pytest.MonkeyPatch, floors: dict, refused: bool
) -> None:
    """The Agent Harness row, the ``agent.acp_backend`` PATCH and a crewmate pin judge the floored tier."""
    from kiro_crew import crewmate_harness
    from kiro_crew.dashboard.handlers import acp_backend_status, core_config_patch

    _pin_host(monkeypatch, backend="namespace")
    cfg = _off_config(monkeypatch, floors)
    monkeypatch.setattr(crewmate_harness, "crewmate_harnesses", lambda config: ["opencode"])

    assert acp_backend_status._sandbox_refusal(ACP_BACKEND_OPENCODE) == (
        "setting" if refused else ""
    )
    assert bool(core_config_patch._acp_backend_sandbox_refusal("opencode")) is refused
    assert (crewmate_harness.harness_pin_rejected("opencode", cfg) is not None) is refused


@pytest.mark.parametrize(
    "floors,default,override,hidden",
    [
        # The floored harness starts sandboxed, so an earlier run's scratch tree is masked.
        (_FLOOR, ACP_BACKEND_OPENCODE, None, True),
        # A Kiro CLI session keeps agent.sandbox "off" under an OpenCode floor: kept.
        (_FLOOR, ACP_BACKEND_KIRO, None, False),
        (_FLOOR, ACP_BACKEND_OPENCODE, ACP_BACKEND_KIRO, False),
        ({}, ACP_BACKEND_OPENCODE, None, False),
    ],
)
def test_a_scratch_cwd_is_judged_by_the_floor_of_the_sessions_own_harness(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    floors: dict,
    default: str,
    override: str | None,
    hidden: bool,
) -> None:
    """The cwd check reads the floor of the backend the factory selects for the session."""
    from kiro_crew.session_allocation_parts.get_or_create import _stored_cwd_is_hidden_scratch

    _off_config(monkeypatch, floors).agent.acp_backend = default
    root = tmp_path / "scratch"
    monkeypatch.setattr(platform_compat, "IS_WINDOWS", False)
    with (
        patch("kiro_crew.agent_scratch.scratch_root", return_value=root),
        patch("kiro_crew.sandbox_tiers._clamp_sandbox_mode", side_effect=lambda mode: mode),
    ):
        # A cwd that is not a directory is always hidden, so the tree must exist.
        (root / "runtime-old" / "x").mkdir(parents=True)
        cwd = str(root / "runtime-old" / "x")
        assert _stored_cwd_is_hidden_scratch(cwd, None, "dashboard_floor", override) is hidden


@pytest.mark.asyncio
@pytest.mark.parametrize("floors,expected", [(_FLOOR, "standard"), ({}, "off")])
async def test_a_knowledge_worker_on_a_floored_harness_starts_at_the_floored_tier(
    monkeypatch: pytest.MonkeyPatch, floors: dict, expected: str
) -> None:
    from kiro_crew.knowledge import llm_pool

    built: list[dict] = []

    class _Worker:
        def __init__(self, **kwargs: object) -> None:
            built.append(kwargs)

        async def start(self) -> None:
            return None

    monkeypatch.setattr(
        llm_pool, "_read_config", lambda: {"agent": {"sandbox": "off", "harness_sandbox": floors}}
    )
    monkeypatch.setattr(
        llm_pool, "_get_acp_backend", lambda config=None, *, sessions=None: ACP_BACKEND_OPENCODE
    )
    monkeypatch.setattr(llm_pool, "AcpWorker", _Worker)

    await llm_pool.LLMPool(pool_size=1)._create_worker()

    assert built and built[0]["sandbox_mode"] == expected


def _off_config(monkeypatch: pytest.MonkeyPatch, floors: dict) -> KiroCrewConfig:
    """Serve a config with ``agent.sandbox: "off"`` and *floors* to every ``load()``."""
    cfg = KiroCrewConfig()
    cfg.agent.sandbox = "off"
    cfg.agent.harness_sandbox = dict(floors)
    monkeypatch.setattr(KiroCrewConfig, "load", classmethod(lambda cls: cfg))
    return cfg
