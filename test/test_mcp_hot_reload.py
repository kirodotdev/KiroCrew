"""The gate that lets MCP config writes skip the all-sessions reset.

Every path here fails CLOSED: the reset is the safe answer, so an unknown
backend, a handshake that reported no version, or a version below the floor
must all yield False. The gate reads what each LIVE provider reported at
``initialize`` — never a binary on disk — so no test spawns anything.
"""

from __future__ import annotations

from types import SimpleNamespace
from unittest.mock import MagicMock

import pytest

from kiro_crew.acp._dispatch import agent_version_from_init
from kiro_crew.acp_backends import (
    ACP_BACKEND_CLAUDE,
    ACP_BACKEND_KAS,
    ACP_BACKEND_KIRO,
    ACP_BACKENDS_KNOWN,
    ACP_BACKENDS_MCP_CONFIG_HOT_RELOAD,
)
from kiro_crew.mcp_hot_reload import (
    MCP_HOT_RELOAD_MIN_KIRO_CLI_VERSION,
    live_sessions_hot_reload,
    mcp_hot_reload_supported,
    parse_kiro_cli_version,
    provider_hot_reloads,
)


def _provider(hot: object) -> SimpleNamespace:
    """A provider carrying the ``LLMProvider.mcp_config_hot_reload`` declaration."""
    return SimpleNamespace(mcp_config_hot_reload=hot)


def _acp_shape(backend: str | None, version: object) -> SimpleNamespace:
    """The two attributes ``AcpProvider.mcp_config_hot_reload`` reads."""
    client = SimpleNamespace(backend=backend) if backend is not None else SimpleNamespace()
    return SimpleNamespace(_client=client, agent_version=version)


def _acp_hot_reload(shape: SimpleNamespace) -> bool:
    from kiro_crew.providers.acp import AcpProvider

    fget = AcpProvider.mcp_config_hot_reload.fget
    assert fget is not None
    return fget(shape)


class TestParse:
    @pytest.mark.parametrize(
        ("text", "expected"),
        [
            ("2.21.0", (2, 21, 0)),
            ("kiro-cli 2.21.0", (2, 21, 0)),
            ("kiro-cli 2.10.0\n", (2, 10, 0)),
            ("kiro-cli-chat 3.0", (3, 0, 0)),
            ("2.21.0-rc.1", (2, 21, 0)),
            ("banner line\nkiro-cli 2.12.3", (2, 12, 3)),
        ],
    )
    def test_parses_the_last_token(self, text: str, expected: tuple[int, int, int]) -> None:
        assert parse_kiro_cli_version(text) == expected

    @pytest.mark.parametrize("text", ["", "   ", "kiro-cli", "kiro-cli unknown", "v"])
    def test_non_version_is_none(self, text: str) -> None:
        assert parse_kiro_cli_version(text) is None


class TestAgentVersionFromInit:
    def test_reads_the_kiro_cli_shape(self) -> None:
        resp = {
            "protocolVersion": 1,
            "agentInfo": {"name": "Kiro CLI Agent", "title": "Kiro CLI Agent", "version": "2.21.0"},
        }
        assert agent_version_from_init(resp) == "2.21.0"

    @pytest.mark.parametrize(
        "resp",
        [
            {},
            {"agentInfo": None},
            {"agentInfo": "2.21.0"},
            {"agentInfo": {"name": "x"}},
            {"agentInfo": {"version": 2}},
            "not a dict",
        ],
    )
    def test_anything_else_is_unknown(self, resp: object) -> None:
        assert agent_version_from_init(resp) == ""  # type: ignore[arg-type]


class TestPureGate:
    def test_kiro_at_the_floor_is_supported(self) -> None:
        assert mcp_hot_reload_supported(ACP_BACKEND_KIRO, MCP_HOT_RELOAD_MIN_KIRO_CLI_VERSION)

    def test_kiro_above_the_floor_is_supported(self) -> None:
        assert mcp_hot_reload_supported(ACP_BACKEND_KIRO, (2, 21, 1))
        assert mcp_hot_reload_supported(ACP_BACKEND_KIRO, (3, 0, 0))

    def test_kiro_below_the_floor_is_not(self) -> None:
        major, minor, _patch = MCP_HOT_RELOAD_MIN_KIRO_CLI_VERSION
        assert not mcp_hot_reload_supported(ACP_BACKEND_KIRO, (major, minor - 1, 99))
        assert not mcp_hot_reload_supported(ACP_BACKEND_KIRO, (1, 99, 0))

    def test_the_floor_is_the_probed_release_not_the_watchers_debut(self) -> None:
        """kiro-cli's watcher shipped in 2.10.0, but the skip is granted only from
        the release whose reconcile semantics were observed. A release in between
        keeps the always-correct reset until it is verified."""
        assert MCP_HOT_RELOAD_MIN_KIRO_CLI_VERSION == (2, 21, 0)
        assert not mcp_hot_reload_supported(ACP_BACKEND_KIRO, (2, 10, 0))
        assert not mcp_hot_reload_supported(ACP_BACKEND_KIRO, (2, 20, 9))

    def test_unknown_version_fails_closed(self) -> None:
        assert not mcp_hot_reload_supported(ACP_BACKEND_KIRO, None)

    @pytest.mark.parametrize(
        "backend", sorted(ACP_BACKENDS_KNOWN - ACP_BACKENDS_MCP_CONFIG_HOT_RELOAD)
    )
    def test_non_member_is_not_supported_at_any_version(self, backend: str) -> None:
        assert not mcp_hot_reload_supported(backend, (99, 0, 0))

    def test_membership_is_kiro_only(self) -> None:
        """Opt-in membership (harness-parity H6): KAS injects its servers on
        session/new and claude reads no agent file, so neither has a file for a
        watcher to reconcile against."""
        assert ACP_BACKENDS_MCP_CONFIG_HOT_RELOAD == frozenset({ACP_BACKEND_KIRO})
        assert ACP_BACKEND_KAS not in ACP_BACKENDS_MCP_CONFIG_HOT_RELOAD
        assert ACP_BACKEND_CLAUDE not in ACP_BACKENDS_MCP_CONFIG_HOT_RELOAD


class TestAcpProviderDeclaration:
    """``AcpProvider.mcp_config_hot_reload`` — membership plus the version its
    own process reported at ``initialize``."""

    def test_kiro_at_or_above_the_floor(self) -> None:
        assert _acp_hot_reload(_acp_shape(ACP_BACKEND_KIRO, "2.21.0"))
        assert _acp_hot_reload(_acp_shape(ACP_BACKEND_KIRO, "2.22.3"))

    def test_kiro_below_the_floor(self) -> None:
        assert not _acp_hot_reload(_acp_shape(ACP_BACKEND_KIRO, "2.20.9"))
        assert not _acp_hot_reload(_acp_shape(ACP_BACKEND_KIRO, "2.9.4"))

    def test_before_the_handshake_is_false(self) -> None:
        """The placeholder client reports no version until ``initialize``; a
        session still starting is reset like one that cannot reconcile."""
        assert not _acp_hot_reload(_acp_shape(ACP_BACKEND_KIRO, ""))

    def test_non_member_backend_at_any_version(self) -> None:
        assert not _acp_hot_reload(_acp_shape(ACP_BACKEND_KAS, "9.0.0"))
        assert not _acp_hot_reload(_acp_shape(ACP_BACKEND_CLAUDE, "9.0.0"))


class TestProviderGate:
    def test_reads_the_declared_capability(self) -> None:
        assert provider_hot_reloads(_provider(True))
        assert not provider_hot_reloads(_provider(False))

    def test_only_a_literal_true_counts(self) -> None:
        """A mocked provider's truthy attribute must never read as a skip."""
        assert not provider_hot_reloads(_provider(MagicMock()))
        assert not provider_hot_reloads(_provider(1))
        assert not provider_hot_reloads(_provider("yes"))

    def test_the_abc_default_is_false(self) -> None:
        """A harness that has not declared the capability does not inherit the
        skip (harness-parity H6/H14)."""
        from kiro_crew.providers.base import LLMProvider

        fget = LLMProvider.mcp_config_hot_reload.fget
        assert fget is not None
        assert fget(SimpleNamespace()) is False


class TestLiveSessionsGate:
    def test_no_live_process_means_nothing_to_reset(self) -> None:
        assert live_sessions_hot_reload([])

    def test_all_reconciling_sessions_skip_the_reset(self) -> None:
        assert live_sessions_hot_reload([_provider(True), _provider(True)])

    def test_one_non_reconciling_process_forces_the_reset(self) -> None:
        """The version-skew case: a process spawned before an in-place upgrade
        declares False on its own handshake version, whatever the file on disk
        now says."""
        assert not live_sessions_hot_reload([_provider(True), _provider(False)])

    def test_one_undeclared_process_forces_the_reset(self) -> None:
        assert not live_sessions_hot_reload([_provider(True), _provider(MagicMock())])
