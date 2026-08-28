"""OpenCode and pi: described, withheld, ROUTED, adapted only.

OpenCode reaches the gate by seeding project ``permission: ask``. pi is
structural ``PERMISSION_REQUEST``. Neither inherits kiro runtime capabilities.
"""

from __future__ import annotations

import json
from pathlib import Path
from unittest.mock import patch

import pytest

from conftest import requires_symlinks
from kiro_crew.acp import backends, claude, opencode, tool_gate
from kiro_crew.acp.codex import Verdict
from kiro_crew.acp.tool_gate import ToolGateUnroutable
from kiro_crew.acp.types import (
    ACP_BACKEND_CLAUDE,
    ACP_BACKEND_OPENCODE,
    ACP_BACKEND_PI,
    ACP_BACKENDS_AUTO_MODEL,
    ACP_BACKENDS_INTERNAL_SANDBOX,
    ACP_BACKENDS_KIRO_CREDITS,
    ACP_BACKENDS_KNOWN,
    ACP_BACKENDS_SELECTABLE,
    ACP_BACKENDS_SESSION_SHARING,
    ACP_BACKENDS_STEER,
)


@pytest.mark.parametrize("backend", [ACP_BACKEND_OPENCODE, ACP_BACKEND_PI])
class TestOpenCodeAndPiAreAdaptedOnly:
    def test_known_but_withheld_from_the_initial_preview(self, backend: str) -> None:
        assert backend in ACP_BACKENDS_KNOWN
        assert backend not in ACP_BACKENDS_SELECTABLE

    def test_spec_dialect(self, backend: str) -> None:
        assert backends.descriptor_for(backend).dialect is backends.Dialect.SPEC

    def test_no_kiro_runtime_capabilities(self, backend: str) -> None:
        assert backend not in ACP_BACKENDS_INTERNAL_SANDBOX
        assert backend not in ACP_BACKENDS_STEER
        assert backend not in ACP_BACKENDS_SESSION_SHARING
        assert backend not in ACP_BACKENDS_AUTO_MODEL
        assert backend not in ACP_BACKENDS_KIRO_CREDITS


class TestPiStaysPermissionRequest:
    def test_routing_is_permission_request(self) -> None:
        assert (
            backends.descriptor_for(ACP_BACKEND_PI).routing is backends.Routing.PERMISSION_REQUEST
        )

    def test_enforce_succeeds_without_the_opt_out(self, tmp_path: Path) -> None:
        tool_gate.enforce(ACP_BACKEND_PI, tmp_path, allow_ungated=False)

    def test_verdict_is_routed(self, tmp_path: Path) -> None:
        verdict, reason = tool_gate.resolve_verdict(ACP_BACKEND_PI, tmp_path)
        assert verdict is Verdict.ROUTED
        assert "session/request_permission" in reason or "asks per privileged tool" in reason


class TestOpenCodeSeededSettings:
    def test_routing_is_seeded_settings(self) -> None:
        assert (
            backends.descriptor_for(ACP_BACKEND_OPENCODE).routing
            is backends.Routing.SEEDED_SETTINGS
        )

    def test_unseeded_is_made_routed_by_writing_ask(self, tmp_path: Path) -> None:
        tool_gate.enforce(ACP_BACKEND_OPENCODE, tmp_path, allow_ungated=False)
        path = opencode.project_config_path(tmp_path)
        written = json.loads(path.read_text())
        assert written["permission"] == opencode.PERMISSION_ASK
        verdict, reason = opencode.routing_verdict(tmp_path)
        assert verdict is Verdict.ROUTED
        assert "ask" in reason

    def test_explicit_allow_is_not_overwritten(self, tmp_path: Path) -> None:
        path = tmp_path / "opencode.json"
        path.write_text(json.dumps({"permission": "allow", "keep": 1}))

        with pytest.raises(ToolGateUnroutable):
            tool_gate.enforce(ACP_BACKEND_OPENCODE, tmp_path, allow_ungated=False)

        after = json.loads(path.read_text())
        assert after["permission"] == "allow"
        assert after["keep"] == 1

    def test_object_wildcard_ask_is_routed_and_left_alone(self, tmp_path: Path) -> None:
        path = tmp_path / "opencode.json"
        path.write_text(json.dumps({"permission": {"*": "ask"}, "keep": True}))
        tool_gate.enforce(ACP_BACKEND_OPENCODE, tmp_path, allow_ungated=False)
        after = json.loads(path.read_text())
        assert after["permission"] == {"*": "ask"}
        assert after["keep"] is True

    @pytest.mark.parametrize("raw", ["{not json", "[]", ""])
    def test_invalid_existing_config_is_preserved(self, tmp_path: Path, raw: str) -> None:
        path = tmp_path / "opencode.json"
        path.write_text(raw)

        with pytest.raises(ToolGateUnroutable):
            tool_gate.enforce(ACP_BACKEND_OPENCODE, tmp_path, allow_ungated=False)

        assert path.read_text() == raw

    def test_unreadable_existing_config_is_not_replaced(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        path = tmp_path / "opencode.json"
        path.write_text("operator-owned")
        original_read = Path.read_text

        def unreadable(candidate: Path, *args: object, **kwargs: object) -> str:
            if candidate == path:
                raise OSError("denied")
            return original_read(candidate, *args, **kwargs)

        with monkeypatch.context() as patcher:
            patcher.setattr(Path, "read_text", unreadable)
            with pytest.raises(ToolGateUnroutable):
                tool_gate.enforce(ACP_BACKEND_OPENCODE, tmp_path, allow_ungated=False)
        assert path.read_text() == "operator-owned"

    @requires_symlinks
    def test_linked_config_is_neither_read_nor_replaced(self, tmp_path: Path) -> None:
        external = tmp_path / "external.json"
        original = json.dumps({"operator": True})
        external.write_text(original)
        project = tmp_path / "project"
        project.mkdir()
        path = project / "opencode.json"
        path.symlink_to(external)

        with pytest.raises(ToolGateUnroutable):
            tool_gate.enforce(ACP_BACKEND_OPENCODE, project, allow_ungated=False)

        assert external.read_text() == original
        assert path.is_symlink()

    @requires_symlinks
    def test_linked_nested_config_directory_is_not_followed(self, tmp_path: Path) -> None:
        external = tmp_path / "external"
        external.mkdir()
        project = tmp_path / "project"
        project.mkdir()
        (project / ".opencode").symlink_to(external, target_is_directory=True)
        (external / "opencode.json").write_text(json.dumps({"model": "outside"}))

        with pytest.raises(ToolGateUnroutable):
            tool_gate.enforce(ACP_BACKEND_OPENCODE, project, allow_ungated=False)

        assert json.loads((external / "opencode.json").read_text()) == {"model": "outside"}

    def test_nested_config_is_preferred_when_it_already_exists(self, tmp_path: Path) -> None:
        nested = tmp_path / ".opencode" / "opencode.json"
        nested.parent.mkdir()
        nested.write_text(json.dumps({"model": "already-here"}))
        tool_gate.enforce(ACP_BACKEND_OPENCODE, tmp_path, allow_ungated=False)
        assert not (tmp_path / "opencode.json").exists()
        after = json.loads(nested.read_text())
        assert after["permission"] == opencode.PERMISSION_ASK
        assert after["model"] == "already-here"

    def test_never_writes_the_operator_global_config(self, tmp_path: Path) -> None:
        home = tmp_path / "home"
        global_cfg = home / ".config" / "opencode" / "opencode.json"
        global_cfg.parent.mkdir(parents=True)
        global_cfg.write_text(json.dumps({"permission": "allow"}))
        work = tmp_path / "work"
        work.mkdir()
        tool_gate.enforce(ACP_BACKEND_OPENCODE, work, allow_ungated=False)
        assert json.loads(global_cfg.read_text())["permission"] == "allow"
        assert json.loads((work / "opencode.json").read_text())["permission"] == "ask"

    def test_dispatch_does_not_call_claude_seed(self, tmp_path: Path) -> None:
        with patch.object(claude, "ensure_routed_settings") as claude_seed:
            tool_gate.enforce(ACP_BACKEND_OPENCODE, tmp_path, allow_ungated=False)
        claude_seed.assert_not_called()
        assert (tmp_path / "opencode.json").is_file()

    def test_claude_dispatch_does_not_call_opencode_seed(self, tmp_path: Path) -> None:
        with patch.object(opencode, "ensure_routed_settings") as opencode_seed:
            tool_gate.enforce(ACP_BACKEND_CLAUDE, tmp_path, allow_ungated=False)
        opencode_seed.assert_not_called()
