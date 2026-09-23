"""The claude backend's ``permissions.defaultMode`` reaches a session, or nothing does.

``AcpProvider`` has carried a ``permission_mode`` kwarg and ``AcpClient`` has
written it into ``<work_dir>/.claude/settings.local.json`` all along -- with no
caller anywhere, so Claude Code's own permission classifier could never arm for a
Crew session. These tests pin the resolution that closes that chain, and pin
CLOSED the direction it must never open: with nothing asking for a mode, the seed
carries no ``defaultMode`` at all.

The widening direction is the mutation target. ``resolve_cc_permission_mode``
returning ``auto`` (or any truthy mode) when no opt-in is present must fail here.
"""

from __future__ import annotations

import json
import unittest.mock
from pathlib import Path

import pytest

from kiro_crew.acp.types import CC_PERMISSION_MODE_AUTO, CC_PERMISSION_MODE_BYPASS
from kiro_crew.agent_sdk.backends import (
    ACP_BACKEND_CLAUDE,
    ACP_BACKEND_KIRO,
    CC_PERMISSION_MODE_ENV,
    resolve_cc_permission_mode,
)
from kiro_crew.config.loader import KiroCrewConfig


def _cfg(backend: str, tmp_path: Path) -> KiroCrewConfig:
    """A config whose provider factory builds sessions on *backend*.

    Written inside the test's own ``tmp_path`` -- nothing this file creates may
    outlive the run (AUTOSDE ``no-test-side-effects``).
    """
    cfg_file = tmp_path / "kirocrew.json"
    cfg_file.write_text(
        json.dumps({"agent": {"provider": "acp", "acp_backend": backend}}), encoding="utf-8"
    )
    with unittest.mock.patch("kiro_crew.config.loader.config_path", return_value=cfg_file):
        return KiroCrewConfig.load()


@pytest.fixture(autouse=True)
def _no_ambient_lever(monkeypatch: pytest.MonkeyPatch) -> None:
    """The operator lever is an INPUT here, never inherited from the test host."""
    monkeypatch.delenv(CC_PERMISSION_MODE_ENV, raising=False)


class TestResolveCcPermissionMode:
    """One resolver, so the session lever and the env lever cannot disagree."""

    def test_session_intent_resolves(self) -> None:
        assert (
            resolve_cc_permission_mode(CC_PERMISSION_MODE_AUTO, ACP_BACKEND_CLAUDE)
            == CC_PERMISSION_MODE_AUTO
        )

    def test_operator_env_resolves(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setenv(CC_PERMISSION_MODE_ENV, CC_PERMISSION_MODE_AUTO)
        assert resolve_cc_permission_mode(None, ACP_BACKEND_CLAUDE) == CC_PERMISSION_MODE_AUTO

    def test_a_stray_space_does_not_resolve_but_is_reported(
        self, monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
    ) -> None:
        """Exact match, and the near-miss is named rather than silently dropped.

        ``" auto\n"`` from a plist is the shape that would otherwise coerce into a
        wider permission surface than the value spells. It resolves to nothing --
        and says so, because a lever that reads as set while doing nothing is the
        failure this path was reported for.
        """
        monkeypatch.setenv(CC_PERMISSION_MODE_ENV, " auto\n")
        with caplog.at_level("WARNING"):
            assert resolve_cc_permission_mode(None, ACP_BACKEND_CLAUDE) is None
        assert "not recognised" in caplog.text

    def test_nothing_asked_warns_about_nothing(self, caplog: pytest.LogCaptureFixture) -> None:
        """No opt-in is the normal case, not a misconfiguration."""
        with caplog.at_level("WARNING"):
            assert resolve_cc_permission_mode(None, ACP_BACKEND_CLAUDE) is None
        assert caplog.text == ""

    def test_nothing_asks_nothing_resolves(self) -> None:
        """THE mutation: no opt-in must never resolve to a mode."""
        assert resolve_cc_permission_mode(None, ACP_BACKEND_CLAUDE) is None
        assert resolve_cc_permission_mode("", ACP_BACKEND_CLAUDE) is None

    @pytest.mark.parametrize(
        "asked",
        ["Auto", "AUTO", "acceptEdits", "plan", "default", CC_PERMISSION_MODE_BYPASS, "yolo"],
    )
    def test_only_auto_resolves(self, asked: str) -> None:
        """Anything that is not exactly ``auto`` is declined, both levers alike.

        ``bypassPermissions`` is in here deliberately: it is the one value that
        would take a session out of Crew's host gate entirely, and an env var is
        the easiest place for it to arrive.
        """
        assert resolve_cc_permission_mode(asked, ACP_BACKEND_CLAUDE) is None

    def test_env_cannot_smuggle_bypass(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setenv(CC_PERMISSION_MODE_ENV, CC_PERMISSION_MODE_BYPASS)
        assert resolve_cc_permission_mode(None, ACP_BACKEND_CLAUDE) is None

    def test_a_backend_that_seeds_no_settings_file_gets_nothing(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """kiro-cli has no permission modes, so neither lever names anything there."""
        monkeypatch.setenv(CC_PERMISSION_MODE_ENV, CC_PERMISSION_MODE_AUTO)
        assert resolve_cc_permission_mode(CC_PERMISSION_MODE_AUTO, ACP_BACKEND_KIRO) is None
        assert resolve_cc_permission_mode(None, "no-such-backend") is None


class TestFactoryThreadsTheMode:
    """The factory must take the kwarg BY NAME -- ``**_kwargs`` would swallow it."""

    @staticmethod
    def _captured(backend: str, tmp_path: Path, **factory_kwargs: object) -> dict:
        import kiro_crew.providers.acp as acp_mod

        seen: list[dict] = []

        class _FakeProvider:
            def __init__(self, **kwargs: object) -> None:
                seen.append(kwargs)

        with unittest.mock.patch.object(acp_mod, "AcpProvider", _FakeProvider):
            _cfg(backend, tmp_path).create_provider_factory()("slot-1", **factory_kwargs)
        return seen[0]

    def test_session_intent_reaches_the_provider(self, tmp_path: Path) -> None:
        got = self._captured(ACP_BACKEND_CLAUDE, tmp_path, permission_mode=CC_PERMISSION_MODE_AUTO)
        assert got["permission_mode"] == CC_PERMISSION_MODE_AUTO

    def test_operator_env_reaches_the_provider(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setenv(CC_PERMISSION_MODE_ENV, CC_PERMISSION_MODE_AUTO)
        assert (
            self._captured(ACP_BACKEND_CLAUDE, tmp_path)["permission_mode"]
            == CC_PERMISSION_MODE_AUTO
        )

    def test_no_opt_in_reaches_the_provider_as_none(self, tmp_path: Path) -> None:
        assert self._captured(ACP_BACKEND_CLAUDE, tmp_path)["permission_mode"] is None

    def test_kiro_session_is_unaffected(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setenv(CC_PERMISSION_MODE_ENV, CC_PERMISSION_MODE_AUTO)
        assert self._captured(ACP_BACKEND_KIRO, tmp_path)["permission_mode"] is None


class TestSeedOnDisk:
    """End to end: what the factory resolved is what the settings file carries.

    The seed is written inside ``_spawn`` before the child is launched, so the
    file the adapter reads at startup is this one -- the whole point of resolving
    the mode at provider-construction time rather than after the session exists.
    """

    @staticmethod
    def _seed(backend: str, work_dir: Path, **factory_kwargs: object) -> dict:
        provider = _cfg(backend, work_dir).create_provider_factory()(
            "slot-1", cwd=str(work_dir), **factory_kwargs
        )
        provider._client._write_claude_local_settings()
        return json.loads((work_dir / ".claude" / "settings.local.json").read_text())

    def test_session_intent_seeds_auto(self, tmp_path: Path) -> None:
        data = self._seed(ACP_BACKEND_CLAUDE, tmp_path, permission_mode=CC_PERMISSION_MODE_AUTO)
        assert data["permissions"]["defaultMode"] == CC_PERMISSION_MODE_AUTO

    def test_operator_env_seeds_auto(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setenv(CC_PERMISSION_MODE_ENV, CC_PERMISSION_MODE_AUTO)
        data = self._seed(ACP_BACKEND_CLAUDE, tmp_path)
        assert data["permissions"]["defaultMode"] == CC_PERMISSION_MODE_AUTO

    def test_no_opt_in_seeds_no_mode_at_all(self, tmp_path: Path) -> None:
        """THE mutation, on disk: a default that widens permissions fails here."""
        data = self._seed(ACP_BACKEND_CLAUDE, tmp_path)
        assert "defaultMode" not in data.get("permissions", {})
