"""An unusable model pin must be caught where a human is looking at it.

A spec's ``model`` is read by kiro-cli when the child starts, so a pin it cannot
serve kills every session and subagent using that agent seconds after spawn --
before any of the entitlement guards, which all sit behind session init, can run.
"""

from __future__ import annotations

import json
from pathlib import Path
from types import SimpleNamespace

import pytest
from aiohttp import web
from aiohttp.test_utils import TestClient, TestServer

from kiro_crew.model_registry import acp_id_correction


@pytest.fixture(autouse=True)
def _owner_caller(monkeypatch):
    """Exercise the agent handlers past their independent owner-auth boundary."""
    monkeypatch.setattr(
        "kiro_crew.dashboard.handlers.agents.is_owner_dashboard_request",
        lambda request: True,
    )


class TestAcpIdCorrection:
    def test_corrects_a_bedrock_flavoured_spelling(self):
        """The registry lists each model's prefix-stripped Bedrock id as an alias.

        ``claude-opus-4-8`` therefore resolves to a canonical key while being
        nothing kiro-cli serves, and the registry already knows the id that is.
        """
        assert acp_id_correction("claude-opus-4-8") == "claude-opus-4.5"

    def test_corrects_a_registered_full_bedrock_id(self):
        """The id the alias was stripped FROM must correct too.

        ``_build_indices`` puts aliases into every provider's index but each
        provider's own id only into its own, so an ``acp``-only lookup silently
        passed these through — the likelier spelling for someone copying from
        Bedrock, and the one that actually reaches the child and kills it.
        """
        assert acp_id_correction("global.anthropic.claude-opus-4-8") == "claude-opus-4.5"

    def test_corrects_the_1m_bedrock_variant_to_its_own_kiro_id(self):
        """The 1M entry is a DISTINCT model, so it must not collapse onto the other."""
        assert acp_id_correction("global.anthropic.claude-opus-4-8[1m]") == "claude-opus-4.8"

    def test_every_registered_claude_code_id_is_correctable(self):
        """Class-level guard: no registered non-acp id may bypass the correction.

        Pins the rule rather than the three instances above, so a registry entry
        added later cannot reintroduce the bypass silently.
        """
        from kiro_crew.model_registry import available_models

        served = set(available_models("acp"))
        for pid in available_models("claude_code"):
            if pid in served:
                continue
            assert acp_id_correction(pid), f"{pid!r} bypassed the correction"

    @pytest.mark.parametrize("valid", ["claude-opus-4.8", "claude-sonnet-4", "claude-haiku-4.5"])
    def test_a_real_kiro_id_needs_no_correction(self, valid):
        assert acp_id_correction(valid) is None

    @pytest.mark.parametrize("inherit", ["", "auto"])
    def test_inherit_sentinels_pass(self, inherit):
        assert acp_id_correction(inherit) is None

    def test_an_unrecognized_id_is_never_second_guessed(self):
        """A regional profile or a model newer than this registry is legitimate."""
        assert acp_id_correction("us.anthropic.some-future-model-v9") is None
        assert acp_id_correction("not-a-model-at-all") is None
        # Registered nowhere: a partial prefix must not be guessed at either.
        assert acp_id_correction("anthropic.claude-opus-4-8") is None


class TestDoctorFlagsSpecPins:
    def _spec(self, agents_dir: Path, name: str, model: str) -> None:
        agents_dir.mkdir(parents=True, exist_ok=True)
        (agents_dir / f"{name}.json").write_text(
            json.dumps({"name": name, "description": "d", "model": model}),
            encoding="utf-8",
        )

    def test_reports_an_unusable_pin_with_the_right_id(self, tmp_path):
        from kiro_crew import cli_doctor

        agents_dir = tmp_path / "agents"
        self._spec(agents_dir, "code-reviewer", "claude-opus-4-8")

        problems = cli_doctor._agent_spec_model_problems(agents_dir=agents_dir)

        assert ("code-reviewer", "claude-opus-4-8", "claude-opus-4.5") in problems

    def test_silent_when_every_pin_is_usable(self, tmp_path):
        from kiro_crew import cli_doctor

        agents_dir = tmp_path / "agents"
        self._spec(agents_dir, "fine", "claude-opus-4.8")
        self._spec(agents_dir, "inherits", "auto")

        assert cli_doctor._agent_spec_model_problems(agents_dir=agents_dir) == []

    def test_a_project_scoped_spec_is_audited_too(self, tmp_path):
        """A project spec SHADOWS a same-named global one, so it must be scanned.

        Auditing only the global scope reports a clean bill of health for the
        very spec a session in that project actually runs.
        """
        from kiro_crew import cli_doctor

        global_dir = tmp_path / "agents"
        self._spec(global_dir, "fine", "claude-opus-4.8")
        project = tmp_path / "proj"
        self._spec(project / ".kiro" / "agents", "proj-reviewer", "claude-opus-4-8")

        problems = cli_doctor._agent_spec_model_problems(agents_dir=global_dir, project_dir=project)

        assert problems is not None, "the check must have run"
        assert any(
            name == "proj-reviewer" for name, _pin, _fix in problems
        ), f"project-scoped pin was not audited: {problems}"

    def test_unreadable_specs_report_unchecked_not_clean(self, tmp_path):
        """A diagnostic must not print green for a check it never performed."""
        from kiro_crew import cli_doctor

        agents_dir = tmp_path / "agents"
        agents_dir.mkdir()
        (agents_dir / "broken.json").write_text("{not json", encoding="utf-8")

        result = cli_doctor._agent_spec_model_problems(agents_dir=agents_dir)

        assert result is None, "an unrunnable check must be distinguishable from a clean one"

    def test_claude_code_provider_does_not_audit_its_valid_wire_id(self, tmp_path):
        from kiro_crew import cli_doctor

        agents_dir = tmp_path / "agents"
        self._spec(
            agents_dir,
            "cc-reviewer",
            "global.anthropic.claude-opus-4-8",
        )

        assert (
            cli_doctor._agent_spec_model_problems(agents_dir=agents_dir, provider="claude_code")
            == []
        )

    def test_appledouble_sidecar_is_not_an_unreadable_spec(self, tmp_path):
        from kiro_crew import cli_doctor

        agents_dir = tmp_path / "agents"
        agents_dir.mkdir()
        (agents_dir / "._finder-copy.json").write_bytes(b"\x00\x05garbage")

        assert cli_doctor._agent_spec_model_problems(agents_dir=agents_dir) == []

    def test_unreadable_project_enumeration_reports_unchecked(self, tmp_path, monkeypatch):
        from kiro_crew import cli_doctor
        from kiro_crew.config.paths import project_agents_dir

        project = tmp_path / "project"
        target = project_agents_dir(project)
        original_scan = cli_doctor._strict_agent_json_specs

        def refusing_scan(path):
            if path == target:
                raise PermissionError("project agents unreadable")
            return original_scan(path)

        monkeypatch.setattr(cli_doctor, "_strict_agent_json_specs", refusing_scan)

        assert (
            cli_doctor._agent_spec_model_problems(
                agents_dir=tmp_path / "global", project_dir=project
            )
            is None
        )

    def test_a_hostile_spec_name_cannot_inject_terminal_escapes(self):
        """Spec contents reach a terminal, so control characters must be escaped.

        A planted or packaged spec whose name carries cursor moves, a screen
        clear or an OSC sequence could otherwise rewrite or hide this report.
        """
        from kiro_crew.cli_doctor import _format_model_pin_problem

        hostile = "\x1b[2J\x1b[1Aevil\x1b]52;c;cGFzcw==\x07"
        lines = _format_model_pin_problem(hostile, "claude-opus-4-8", "claude-opus-4.5")
        rendered = "\n".join(lines)

        assert "\x1b" not in rendered, "a raw ESC byte would reach the terminal"
        assert "\x07" not in rendered, "a raw BEL would terminate an OSC sequence"
        assert "evil" in rendered, "the name must still be reported, only escaped"
        assert "claude-opus-4.5" in rendered

    def test_a_hostile_pin_and_correction_are_escaped_too(self):
        """All three fields come from spec contents, so none may pass through raw."""
        from kiro_crew.cli_doctor import _format_model_pin_problem

        rendered = "\n".join(_format_model_pin_problem("ok", "\x1b[2Jpin", "\x1b[2Jfix"))

        assert "\x1b" not in rendered


class TestNoRedundantConfigLoad:
    """The validation path must not re-read config to learn what it was told.

    ``KiroCrewConfig.load()`` deep-copies the validated dict even on a cache hit
    and reads/validates files on a miss. The agent handlers run it inside
    ``_get_config_lock()`` on the event loop, so an extra load there stalls the
    loop while the lock is held.
    """

    def _counting_load(self, monkeypatch):
        from kiro_crew.config.loader import KiroCrewConfig

        original = KiroCrewConfig.load
        calls: list[int] = []

        def counting(*args, **kwargs):
            calls.append(1)
            return original(*args, **kwargs)

        monkeypatch.setattr(KiroCrewConfig, "load", staticmethod(counting))
        return calls

    def test_supplied_provider_skips_the_load(self, monkeypatch):
        from kiro_crew.dashboard.chat_handlers import _model_rejected_reason

        calls = self._counting_load(monkeypatch)
        _model_rejected_reason("claude-opus-4-8", provider="acp")
        assert calls == []

    def test_omitted_provider_still_resolves_it(self, monkeypatch):
        """The default must preserve the original behaviour for existing callers."""
        from kiro_crew.dashboard.chat_handlers import _model_rejected_reason

        calls = self._counting_load(monkeypatch)
        _model_rejected_reason("claude-opus-4-8")
        assert len(calls) == 1

    def test_validator_forwards_the_provider(self, monkeypatch):
        from kiro_crew.dashboard.handlers.core import _validate_role_model

        calls = self._counting_load(monkeypatch)
        request = SimpleNamespace(app={})
        _validate_role_model("claude-opus-4-8", request, provider="acp")
        assert calls == []

    def test_live_entitlements_preserve_the_registry_correction(self, monkeypatch):
        """A live advertised set must not replace the actionable spelling hint."""
        from kiro_crew.dashboard.handlers import agents, core

        monkeypatch.setattr(core, "_active_advertised_ids", lambda request: ["claude-opus-4.8"])

        reason = agents._model_pin_rejected(
            "claude-opus-4-8", SimpleNamespace(app={}), provider="acp"
        )

        assert reason is not None
        assert "registry maps that spelling to 'claude-opus-4.5'" in reason

    def test_claude_code_provider_keeps_its_wire_id(self, monkeypatch):
        from kiro_crew.dashboard.handlers import agents, core

        monkeypatch.setattr(core, "_active_advertised_ids", lambda request: ["claude-opus-4-8"])

        reason = agents._model_pin_rejected(
            "global.anthropic.claude-opus-4-8",
            SimpleNamespace(app={}),
            provider="claude_code",
        )

        assert reason is None


def _crud_app() -> web.Application:
    from kiro_crew.dashboard.handlers import (
        api_kirocrew_agent_update,
        api_kirocrew_agents_create,
    )

    app = web.Application()
    app.router.add_post("/api/agents", api_kirocrew_agents_create)
    app.router.add_put("/api/agents/{name}", api_kirocrew_agent_update)
    return app


@pytest.fixture()
def seeded_agent():
    """One stored agent, written through the real config API.

    The config home is already redirected per test by the rootdir conftest, so
    this touches no real install.
    """
    from kiro_crew.config.loader import KiroCrewAgentConfig, KiroCrewConfig

    cfg = KiroCrewConfig.load()
    cfg.agents["existing"] = KiroCrewAgentConfig(
        kiro_agent="kirocrew", workspace="default", memory_store="default"
    )
    cfg.save()
    return "existing"


class TestSavePathRefusesAnUnusablePin:
    @pytest.mark.asyncio
    async def test_create_refuses_and_carries_an_error_code(self, seeded_agent):
        from kiro_crew.config.loader import KiroCrewConfig

        async with TestClient(TestServer(_crud_app())) as client:
            resp = await client.post(
                "/api/agents",
                json={
                    "name": "reviewer",
                    "kiro_agent": "kirocrew",
                    "model": "claude-opus-4-8",
                },
            )
            assert resp.status == 400
            body = await resp.json()
            assert body["code"] == "invalid_model"
            assert "claude-opus-4.5" in body["error"]
            # Reports the mapping and the served set; must NOT instruct a swap,
            # since the user's intent for a Bedrock spelling is ambiguous.
            assert "confirm that is the model you want" in body["error"]
            assert "claude-opus-4.8" in body["error"]

        assert "reviewer" not in KiroCrewConfig.load().agents

    @pytest.mark.asyncio
    async def test_update_refuses_and_leaves_the_stored_value_alone(self, seeded_agent):
        from kiro_crew.config.loader import KiroCrewConfig

        async with TestClient(TestServer(_crud_app())) as client:
            resp = await client.put(
                f"/api/agents/{seeded_agent}", json={"model": "claude-opus-4-8"}
            )
            assert resp.status == 400
            assert (await resp.json())["code"] == "invalid_model"

        assert KiroCrewConfig.load().agents[seeded_agent].model != "claude-opus-4-8"

    @pytest.mark.asyncio
    async def test_a_usable_pin_is_accepted(self, seeded_agent):
        async with TestClient(TestServer(_crud_app())) as client:
            resp = await client.post(
                "/api/agents",
                json={
                    "name": "good",
                    "kiro_agent": "kirocrew",
                    "model": "claude-opus-4.8",
                },
            )
            assert resp.status == 200

    @pytest.mark.asyncio
    async def test_auto_is_accepted(self, seeded_agent):
        async with TestClient(TestServer(_crud_app())) as client:
            resp = await client.post(
                "/api/agents",
                json={"name": "inherits", "kiro_agent": "kirocrew", "model": "auto"},
            )
            assert resp.status == 200
