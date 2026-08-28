"""Codex model-id translation and the spec-adapter agent-profile guard."""

from __future__ import annotations

import json
from pathlib import Path
from unittest.mock import AsyncMock

import pytest

from kiro_crew.acp import codex, spec_agent_guard
from kiro_crew.acp.client import AcpClient
from kiro_crew.acp.spec_agent_guard import SpecAdapterAgentRefused
from kiro_crew.acp.types import ACP_BACKEND_CODEX


class TestModelIdTranslation:
    """Codex spells effort into the model id, so a pick must be split."""

    @pytest.mark.parametrize(
        ("advertised", "base", "effort"),
        [
            ("gpt-5.2[high]", "gpt-5.2", "high"),
            ("gpt-5.2[xhigh]", "gpt-5.2", "xhigh"),
            ("gpt-5.2", "gpt-5.2", ""),
            ("  gpt-5.2[low]  ", "gpt-5.2", "low"),
        ],
    )
    def test_split(self, advertised: str, base: str, effort: str) -> None:
        assert codex.split_model_id(advertised) == (base, effort)

    def test_malformed_id_is_returned_unchanged(self) -> None:
        """Guessing at a repair would send something never advertised."""
        assert codex.split_model_id("gpt[5.2[high]") == ("gpt[5.2[high]", "")

    def test_default_wires_as_empty_meaning_reset(self) -> None:
        """Codex has no id meaning "your own default".

        So there is nothing to push that would restore it on a live session, and
        the caller must reset instead. Sending a guess would pin a model the
        operator did not choose.
        """
        assert codex.wire_model_id("gpt-5.2[high]", is_default=True) == ""
        assert codex.wire_model_id("", is_default=False) == ""

    def test_a_pick_wires_as_the_base_id(self) -> None:
        """The composite form is answered -32602.

        Which surfaces as "model unavailable" rather than "that is not a model
        id", so leaving the suffix on sends the operator chasing entitlements.
        """
        assert codex.wire_model_id("gpt-5.2[high]", is_default=False) == "gpt-5.2"

    def test_effort_is_recoverable_for_the_effort_control(self) -> None:
        assert codex.advertised_effort("gpt-5.2[xhigh]") == "xhigh"
        assert codex.advertised_effort("gpt-5.2") == ""

    def test_composite_detection_is_for_diagnosis_only(self) -> None:
        """Documents why callers must key on the descriptor, not this shape.

        The reference implementation identified codex BY this advertisement
        shape, so any future backend advertising the same shape silently
        inherited codex's entitlement comparison.
        """
        assert codex.is_composite_advertisement(["gpt-5.2[high]", "gpt-5.2[low]"])
        assert not codex.is_composite_advertisement(["claude-opus-4.8"])


class TestCompositeModelStartup:
    """A persisted Codex picker row must survive a cold session start."""

    @pytest.mark.asyncio
    async def test_startup_sends_only_the_base_model_and_keeps_the_selection(
        self, tmp_path: Path
    ) -> None:
        client = AcpClient(
            work_dir=tmp_path,
            model="gpt-5.2[high]",
            acp_backend=ACP_BACKEND_CODEX,
        )
        client._session_id = "session-1"
        client.set_config_option = AsyncMock()

        await client._apply_startup_model()

        client.set_config_option.assert_awaited_once_with("model", "gpt-5.2")
        assert client._model == "gpt-5.2[high]"
        assert client._resolved_model_id == "gpt-5.2"


class TestSpecAdapterAgentGuard:
    """A spec adapter cannot enforce a narrowed tool set, so refuse the combo."""

    @pytest.fixture()
    def agents_dir(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
        agents = tmp_path / "agents"
        agents.mkdir()
        monkeypatch.setattr("kiro_crew.config.paths.kiro_agents_dir", lambda: agents, raising=True)
        return agents

    def _write(self, agents_dir: Path, name: str, spec: dict) -> Path:
        path = agents_dir / f"{name}.json"
        path.write_text(json.dumps(spec))
        return path

    def test_the_default_agent_is_never_restricted(self, tmp_path: Path) -> None:
        assert spec_agent_guard.shell_restriction("kirocrew", tmp_path) == ""

    def test_global_owned_default_agent_file_is_exempt(
        self, agents_dir: Path, tmp_path: Path
    ) -> None:
        self._write(agents_dir, "kirocrew", {"name": "kirocrew", "tools": []})
        assert spec_agent_guard.shell_restriction("kirocrew", tmp_path) == ""

    def test_project_default_agent_shadow_is_not_exempt(
        self, agents_dir: Path, tmp_path: Path
    ) -> None:
        """A project cannot borrow Crew's default name to widen its tools."""
        from kiro_crew.config.paths import project_agents_dir

        project_agents = project_agents_dir(tmp_path)
        project_agents.mkdir(parents=True)
        (project_agents / "kirocrew.json").write_text(
            json.dumps({"name": "kirocrew", "tools": []}),
            encoding="utf-8",
        )

        with pytest.raises(SpecAdapterAgentRefused):
            spec_agent_guard.assert_agent_permitted("kirocrew", "Claude Code", tmp_path)

    def test_an_absent_spec_is_not_a_finding(self, agents_dir: Path, tmp_path: Path) -> None:
        """A host with no third-party agents must never be blocked by this."""
        assert spec_agent_guard.shell_restriction("nonexistent", tmp_path) == ""

    def test_a_spec_granting_shell_is_permitted(self, agents_dir: Path, tmp_path: Path) -> None:
        self._write(agents_dir, "helper", {"tools": ["execute_bash", "fs_read"]})
        assert spec_agent_guard.shell_restriction("helper", tmp_path) == ""

    def test_a_wildcard_tools_list_grants_shell(self, agents_dir: Path, tmp_path: Path) -> None:
        self._write(agents_dir, "helper", {"tools": ["*"]})
        assert spec_agent_guard.shell_restriction("helper", tmp_path) == ""

    def test_a_missing_tools_key_does_not_restrict(self, agents_dir: Path, tmp_path: Path) -> None:
        """kiro-cli's own reading: absence is not a restriction."""
        self._write(agents_dir, "helper", {"prompt": "hi"})
        assert spec_agent_guard.shell_restriction("helper", tmp_path) == ""

    def test_a_shell_withholding_spec_is_refused(self, agents_dir: Path, tmp_path: Path) -> None:
        self._write(agents_dir, "restricted", {"tools": ["fs_read"]})
        with pytest.raises(SpecAdapterAgentRefused, match="withholds shell access"):
            spec_agent_guard.assert_agent_permitted("restricted", "OpenAI Codex", tmp_path)

    def test_declared_name_resolves_even_when_filename_differs(
        self, agents_dir: Path, tmp_path: Path
    ) -> None:
        self._write(agents_dir, "alias", {"name": "restricted", "tools": []})
        with pytest.raises(SpecAdapterAgentRefused):
            spec_agent_guard.assert_agent_permitted("restricted", "OpenAI Codex", tmp_path)

    def test_project_file_reusing_owned_basename_is_not_exempt(
        self, agents_dir: Path, tmp_path: Path
    ) -> None:
        from kiro_crew.agent_files import KNOWLEDGE_AGENT_FILENAME
        from kiro_crew.config.paths import project_agents_dir

        project_agents = project_agents_dir(tmp_path)
        project_agents.mkdir(parents=True)
        (project_agents / KNOWLEDGE_AGENT_FILENAME).write_text(
            json.dumps({"name": "restricted", "tools": []}),
            encoding="utf-8",
        )

        with pytest.raises(SpecAdapterAgentRefused):
            spec_agent_guard.assert_agent_permitted("restricted", "OpenAI Codex", tmp_path)

    def test_an_empty_tools_list_is_refused(self, agents_dir: Path, tmp_path: Path) -> None:
        """The shipped auto-improvement PR author has exactly this shape."""
        self._write(agents_dir, "noshell", {"tools": []})
        with pytest.raises(SpecAdapterAgentRefused):
            spec_agent_guard.assert_agent_permitted("noshell", "OpenAI Codex", tmp_path)

    def test_refusal_names_the_way_out(self, agents_dir: Path, tmp_path: Path) -> None:
        self._write(agents_dir, "restricted", {"tools": ["fs_read"]})
        with pytest.raises(SpecAdapterAgentRefused) as caught:
            spec_agent_guard.assert_agent_permitted("restricted", "OpenAI Codex", tmp_path)
        message = str(caught.value)
        assert "acp_backend" in message
        assert "set_mode" in message

    def test_an_unreadable_spec_is_unverifiable_not_permitted(
        self, agents_dir: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Treating unreadable as permissive is a bypass.

        An attacker who can make the spec unreadable would otherwise turn a
        refusal into an approval.
        """
        self._write(agents_dir, "opaque", {"tools": ["fs_read"]})
        monkeypatch.setattr(
            "kiro_crew.agent_discovery._read_agent_spec", lambda path: None, raising=True
        )
        restriction = spec_agent_guard.shell_restriction("opaque", tmp_path)
        assert "could not be read" in restriction

    def test_unreadable_project_shadow_blocks_same_name_global_spec(
        self, agents_dir: Path, tmp_path: Path
    ) -> None:
        """An omitted project shadow must not expose the permissive global spec."""
        from kiro_crew.agent_discovery import clear_list_agents_cache
        from kiro_crew.config.paths import project_agents_dir

        self._write(
            agents_dir,
            "helper",
            {"name": "helper", "tools": ["execute_bash"]},
        )
        project_agents = project_agents_dir(tmp_path)
        project_agents.mkdir(parents=True)
        (project_agents / "helper.json").write_text("{not-json", encoding="utf-8")
        clear_list_agents_cache()

        restriction = spec_agent_guard.shell_restriction("helper", tmp_path)

        assert "could not be read" in restriction
        assert str(project_agents / "helper.json") in restriction

    def test_kiro_crew_own_agents_are_exempt(self, agents_dir: Path, tmp_path: Path) -> None:
        """Their narrow tool sets are Kiro Crew's own scope choice.

        Refusing them bricked the background session on the reference
        implementation's first live deploy.
        """
        from kiro_crew.agent_files import OWNED_KIRO_AGENT_FILES

        owned = OWNED_KIRO_AGENT_FILES[1]  # a non-default owned spec
        name = owned[: -len(".json")]
        self._write(agents_dir, name, {"tools": []})
        assert spec_agent_guard.shell_restriction(name, tmp_path) == ""
