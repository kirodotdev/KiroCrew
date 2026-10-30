"""A session whose MCP tools a settings file withholds says so in its transcript.

When ``_write_claude_local_settings`` cannot govern ``settings.local.json`` --
another open Crew session holds a seed with a different model or permission
mode, or the project's own file holds the path -- the session runs without
Crew's MCP tools. Before this, only a gateway log line said why. These tests pin:

1. ``AcpClient.settings_withheld_cause`` reads the live governed state (the
   predicate that withholds the array) and names whose file holds the path.
2. ``AcpProvider.settings_withheld_cause`` forwards it for the claude shape.
3. ``chat_runner._notice_seed_declined`` appends one tagged ``notice`` row with
   the text for that cause, and nothing when the tools are not withheld.
"""

from __future__ import annotations

import asyncio
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest
from test_acp_seed_provenance import (  # noqa: F401 -- autouse fixture
    _OWNER,
    isolated_records,
)

from kiro_crew import model_registry as mr
from kiro_crew.acp.client import AcpClient
from kiro_crew.acp.types import ACP_BACKEND_CLAUDE
from kiro_crew.dashboard import chat_runner
from kiro_crew.kiro_cli import SPEC_PERMISSIONS_MIN_VERSION
from kiro_crew.providers.acp import AcpProvider

# Two distinct advertised model lists the tests toggle between.
_MODEL_A = "global.anthropic.claude-opus-5[1m]"
_MODEL_B = "global.anthropic.claude-opus-4-8[1m]"
_SERVED_A = [_MODEL_A]
_SERVED_B = [_MODEL_A, _MODEL_B]


@pytest.fixture(autouse=True)
def _pinned_kiro_cli_version(_floor_monkeypatch: pytest.MonkeyPatch) -> None:
    """Pin the kiro-cli version so the spec permissions gate does not shell out."""
    _floor_monkeypatch.setattr(
        "kiro_crew.kiro_cli.installed_kiro_cli_version",
        lambda: SPEC_PERMISSIONS_MIN_VERSION,
    )


def _client(tmp_path: Path, **kw: object) -> AcpClient:
    return AcpClient(work_dir=tmp_path, acp_backend=ACP_BACKEND_CLAUDE, **kw)


# ── part 1: the client's live answer ─────────────────────────────────────────


class TestSettingsWithheldCause:
    """``settings_withheld_cause`` is ``""`` exactly when the array is delivered."""

    def test_backend_that_seeds_nothing_is_never_withheld(self, tmp_path: Path) -> None:
        assert AcpClient(work_dir=tmp_path).settings_withheld_cause() == ""

    def test_no_file_yet_names_no_holder(self, tmp_path: Path) -> None:
        assert _client(tmp_path).settings_withheld_cause() == ""

    def test_own_seed_is_governed(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setattr(mr, "_ADVERTISED_MODELS", {"claude_code": _SERVED_A})
        client = _client(tmp_path)
        client._write_claude_local_settings()
        assert client._permission_surface_governed
        assert client.settings_withheld_cause() == ""

    def test_identical_sibling_seed_is_governed(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setattr(mr, "_ADVERTISED_MODELS", {"claude_code": _SERVED_A})
        _client(tmp_path)._write_claude_local_settings()
        sibling = _client(tmp_path)
        sibling._write_claude_local_settings()
        assert sibling.settings_withheld_cause() == ""

    def test_sibling_seed_with_another_model_names_the_session(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        # The owner's availableModels lists only model A; the sibling needs B.
        # Another settings source defines availableModels, so the owner's seed
        # carries a list at all.
        monkeypatch.setattr(
            "kiro_crew.acp.client_claude_seed.other_sources_define_available_models",
            lambda *a, **k: True,
        )
        monkeypatch.setattr(mr, "_ADVERTISED_MODELS", {"claude_code": _SERVED_A})
        _client(tmp_path, model=_MODEL_A)._write_claude_local_settings()
        monkeypatch.setattr(mr, "_ADVERTISED_MODELS", {"claude_code": _SERVED_B})
        sibling = _client(tmp_path, model=_MODEL_B)
        sibling._write_claude_local_settings()
        assert not sibling._permission_surface_governed
        assert sibling.settings_withheld_cause() == "session"

    def test_sibling_seed_with_another_permission_mode_names_the_session(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setattr(mr, "_ADVERTISED_MODELS", {"claude_code": _SERVED_A})
        _client(tmp_path, permission_mode="default")._write_claude_local_settings()
        sibling = _client(tmp_path, permission_mode="acceptEdits")
        sibling._write_claude_local_settings()
        assert sibling.settings_withheld_cause() == "session"

    def test_project_file_names_the_project(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        # A session that asked for a permission mode cannot leave the project's
        # own file out, so the file stays in charge and the array is withheld.
        monkeypatch.setattr(mr, "_ADVERTISED_MODELS", {"claude_code": _SERVED_A})
        (tmp_path / ".claude").mkdir()
        (tmp_path / ".claude" / "settings.local.json").write_text("{}\n", encoding="utf-8")
        client = _client(tmp_path, permission_mode="acceptEdits")
        client._write_claude_local_settings()
        assert not client._permission_surface_governed
        assert client.settings_withheld_cause() == "project"

    def test_answer_follows_the_live_state(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        # A later re-earn of the share must stop the answer, with no flag to clear.
        monkeypatch.setattr(mr, "_ADVERTISED_MODELS", {"claude_code": _SERVED_A})
        _client(tmp_path, permission_mode="default")._write_claude_local_settings()
        sibling = _client(tmp_path, permission_mode="acceptEdits")
        sibling._write_claude_local_settings()
        assert sibling.settings_withheld_cause() == "session"
        sibling._permission_surface_share_validated = True
        assert sibling.settings_withheld_cause() == ""


# ── part 2: the provider forwards it ─────────────────────────────────────────


class TestProviderForwards:
    def _read(self, client: Any) -> str:
        return AcpProvider.settings_withheld_cause(SimpleNamespace(_client=client))  # type: ignore[arg-type]

    def test_forwards_the_client_answer(self) -> None:
        assert self._read(SimpleNamespace(settings_withheld_cause=lambda: "session")) == "session"

    def test_client_shape_without_the_read_is_empty(self) -> None:
        assert self._read(SimpleNamespace()) == ""

    def test_non_string_answer_is_empty(self) -> None:
        assert self._read(SimpleNamespace(settings_withheld_cause=lambda: True)) == ""


# ── part 3: the notice row ───────────────────────────────────────────────────


@pytest.fixture
def notices(monkeypatch: pytest.MonkeyPatch) -> list[tuple[str, str, dict]]:
    """Collect ``(role, text, meta)`` for every row appended via ``append_and_surface``."""
    seen: list[tuple[str, str, dict]] = []
    monkeypatch.setattr(
        chat_runner,
        "append_and_surface",
        lambda state, slot, role, text, cls, **kw: seen.append((role, text, kw.get("meta") or {})),
    )
    return seen


def _run_notice(client: object) -> None:
    asyncio.run(chat_runner._notice_seed_declined(SimpleNamespace(), SimpleNamespace(), client))


def _answering(value: object) -> SimpleNamespace:
    return SimpleNamespace(settings_withheld_cause=lambda: value)


class TestNoticeEmitted:
    def test_session_cause_names_the_other_session_and_the_fix(self, notices: list) -> None:
        _run_notice(_answering("session"))
        [(role, text, meta)] = notices
        assert role == "notice"
        assert meta == {"kind": chat_runner.SEED_DECLINED_NOTICE_KIND}
        assert "tools are off" in text
        assert "Another open Crew session" in text
        assert "close that session, then start a new session here" in text

    def test_project_cause_names_the_project_file(self, notices: list) -> None:
        _run_notice(_answering("project"))
        [(_, text, meta)] = notices
        assert meta == {"kind": "seed_declined"}
        assert "This folder's own .claude/settings.local.json" in text
        assert "Another open Crew session" not in text

    @pytest.mark.parametrize("value", ["", "other", None, True])
    def test_no_row_when_not_withheld_or_unknown(self, notices: list, value: object) -> None:
        _run_notice(_answering(value))
        assert notices == []

    def test_no_row_for_a_client_without_the_read(self, notices: list) -> None:
        _run_notice(SimpleNamespace())
        assert notices == []

    def test_a_failing_read_never_fails_the_turn(self, notices: list) -> None:
        def boom() -> str:
            raise OSError("unreadable")

        _run_notice(SimpleNamespace(settings_withheld_cause=boom))
        assert notices == []

    def test_real_withheld_client_gets_one_row(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch, notices: list
    ) -> None:
        monkeypatch.setattr(mr, "_ADVERTISED_MODELS", {"claude_code": _SERVED_A})
        _client(tmp_path, permission_mode="default")._write_claude_local_settings()
        sibling = _client(tmp_path, permission_mode="acceptEdits")
        sibling._write_claude_local_settings()
        _run_notice(sibling)
        assert len(notices) == 1
