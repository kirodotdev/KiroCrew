"""The spawn path for codex and goose, and the cost figure Kiro Crew was discarding.

Before this, ``AcpClient._spawn`` branched on ``_is_claude`` and otherwise fell
through to kiro-cli, so selecting codex or goose LAUNCHED KIRO-CLI. That is worse
than a missing feature: it looks like it works. A turn ran, answered correctly, and
needed no adapter credential — because the adapter was never involved. These tests
pin that each backend resolves its OWN argv, and that the kiro arm is untouched.
"""

from __future__ import annotations

import stat
import sys
from pathlib import Path
from unittest.mock import AsyncMock, patch

import pytest

from kiro_crew.acp import backends, goose, opencode, pi
from kiro_crew.acp._dispatch import parse_usage_cost
from kiro_crew.acp.types import (
    ACP_BACKEND_CODEX,
    ACP_BACKEND_GOOSE,
    ACP_BACKEND_KIRO,
    ACP_BACKEND_OPENCODE,
    ACP_BACKEND_PI,
    ACP_CLIENT_CAPABILITIES,
    ACP_CLIENT_CAPABILITIES_SPEC_ADAPTER,
)


def _make_executable(path: Path) -> Path:
    """Create a runnable fixture using the host platform's executable shape."""
    if sys.platform == "win32":
        path = path.with_suffix(".cmd")
        path.write_text("@exit /b 0\n")
    else:
        path.write_text("#!/bin/sh\nexit 0\n")
        path.chmod(path.stat().st_mode | stat.S_IEXEC)
    return path


class TestGooseResolver:
    def test_argv_keeps_developer_tools_with_session_mcp_servers(self, tmp_path: Path) -> None:
        """Goose 1.47 replaces configured extensions when session MCP is present."""
        fake = _make_executable(tmp_path / "goose")
        with patch.dict("os.environ", {"GOOSE_BIN": str(fake)}):
            argv = goose.resolve_argv()
        assert argv == [str(fake), "acp", "--with-builtin", "developer"]

    def test_a_non_executable_hit_is_skipped(self, tmp_path: Path) -> None:
        """Unlike the Node adapters there is no interpreter to wrap it with."""
        fake = tmp_path / "goose"
        fake.write_text("not executable")
        fake.chmod(0o644)
        with patch.dict("os.environ", {"GOOSE_BIN": str(fake)}, clear=False):
            with patch("kiro_crew.acp.client._mise_which", return_value=None):
                with patch("kiro_crew.acp.client._ordered_path_matches", return_value=[]):
                    assert goose.resolve_argv() is None

    def test_a_stale_override_falls_through_rather_than_spawning_it(self, tmp_path: Path) -> None:
        """A GOOSE_BIN pointing at nothing must not become an ENOENT spawn."""
        with patch.dict("os.environ", {"GOOSE_BIN": str(tmp_path / "absent")}):
            with patch("kiro_crew.acp.client._mise_which", return_value=None):
                with patch("kiro_crew.acp.client._ordered_path_matches", return_value=[]):
                    assert goose.resolve_argv() is None

    def test_a_failed_resolution_is_not_cached(self) -> None:
        """An operator who installs goose mid-session should not need a restart."""
        goose._argv_cache = goose._UNRESOLVED
        with patch.object(goose, "resolve_argv", return_value=None):
            assert goose.resolve_argv_cached() is None
        assert goose._argv_cache is goose._UNRESOLVED

    def test_the_missing_message_does_not_name_an_npm_package(self) -> None:
        """There is no goose adapter package; telling someone to npm-install is wrong."""
        message = goose.missing_adapter_message()
        assert "npm install" not in message
        assert "goose acp" in message


class TestOpenCodeResolver:
    def test_argv_is_the_binary_plus_the_acp_subcommand(self, tmp_path: Path) -> None:
        fake = _make_executable(tmp_path / "opencode")
        with patch.dict("os.environ", {"OPENCODE_BIN": str(fake)}):
            argv = opencode.resolve_argv()
        assert argv == [str(fake), "acp"]

    def test_a_non_executable_hit_is_skipped(self, tmp_path: Path) -> None:
        fake = tmp_path / "opencode"
        fake.write_text("not executable")
        fake.chmod(0o644)
        with patch.dict("os.environ", {"OPENCODE_BIN": str(fake)}, clear=False):
            with patch("kiro_crew.acp.client._mise_which", return_value=None):
                with patch("kiro_crew.acp.client._ordered_path_matches", return_value=[]):
                    assert opencode.resolve_argv() is None

    def test_a_stale_override_falls_through_rather_than_spawning_it(self, tmp_path: Path) -> None:
        with patch.dict("os.environ", {"OPENCODE_BIN": str(tmp_path / "absent")}):
            with patch("kiro_crew.acp.client._mise_which", return_value=None):
                with patch("kiro_crew.acp.client._ordered_path_matches", return_value=[]):
                    assert opencode.resolve_argv() is None

    def test_a_failed_resolution_is_not_cached(self) -> None:
        opencode._argv_cache = opencode._UNRESOLVED
        with patch.object(opencode, "resolve_argv", return_value=None):
            assert opencode.resolve_argv_cached() is None
        assert opencode._argv_cache is opencode._UNRESOLVED

    def test_the_missing_message_names_opencode_acp_and_not_an_npm_package(self) -> None:
        message = opencode.missing_adapter_message()
        assert "npm install" not in message
        assert "opencode acp" in message


class TestPiResolver:
    def test_argv_is_the_binary_only_with_no_acp_subcommand(self, tmp_path: Path) -> None:
        """``pi acp`` is not an ACP server; the adapter is the ``pi-acp`` binary."""
        fake = _make_executable(tmp_path / "pi-acp")
        with patch.dict("os.environ", {"PI_ACP_BIN": str(fake)}):
            argv = pi.resolve_argv()
        assert argv == [str(fake)]
        assert "acp" not in argv

    def test_a_non_executable_hit_is_skipped(self, tmp_path: Path) -> None:
        fake = tmp_path / "pi-acp"
        fake.write_text("not executable")
        fake.chmod(0o644)
        with patch.dict("os.environ", {"PI_ACP_BIN": str(fake)}, clear=False):
            with patch("kiro_crew.acp.client._mise_which", return_value=None):
                with patch("kiro_crew.acp.client._ordered_path_matches", return_value=[]):
                    assert pi.resolve_argv() is None

    def test_a_stale_override_falls_through_rather_than_spawning_it(self, tmp_path: Path) -> None:
        with patch.dict("os.environ", {"PI_ACP_BIN": str(tmp_path / "absent")}):
            with patch("kiro_crew.acp.client._mise_which", return_value=None):
                with patch("kiro_crew.acp.client._ordered_path_matches", return_value=[]):
                    assert pi.resolve_argv() is None

    def test_a_failed_resolution_is_not_cached(self) -> None:
        pi._argv_cache = pi._UNRESOLVED
        with patch.object(pi, "resolve_argv", return_value=None):
            assert pi.resolve_argv_cached() is None
        assert pi._argv_cache is pi._UNRESOLVED

    def test_the_missing_message_names_pi_acp_and_its_npm_package(self) -> None:
        message = pi.missing_adapter_message()
        assert "pi-acp" in message
        assert "npm install" in message


class TestSpawnResolvesEachBackendsOwnArgv:
    """The bug this exists for: codex and goose silently launching kiro-cli."""

    @pytest.mark.parametrize(
        "backend,module,resolver",
        [
            (ACP_BACKEND_CODEX, "kiro_crew.acp.codex", "resolve_argv_cached"),
            (ACP_BACKEND_GOOSE, "kiro_crew.acp.goose", "resolve_argv_cached"),
            (ACP_BACKEND_OPENCODE, "kiro_crew.acp.opencode", "resolve_argv_cached"),
            (ACP_BACKEND_PI, "kiro_crew.acp.pi", "resolve_argv_cached"),
        ],
    )
    def test_each_adapter_has_a_resolver_the_spawn_can_call(
        self, backend: str, module: str, resolver: str
    ) -> None:
        """The resolver must exist and be callable, per backend.

        Asserted separately from the spawn itself because exercising _spawn runs
        the tool gate and execs a process; what regressed here was simply that
        nothing ever CALLED these.
        """
        import importlib

        assert callable(getattr(importlib.import_module(module), resolver))

    def test_spawn_dispatches_on_each_backend_rather_than_falling_through(self) -> None:
        """Pinned by source inspection — exercising _spawn would exec a process.

        The regression was structural: two branches, `_is_claude` and else. A
        backend absent from the chain is not refused, it is silently served
        kiro-cli, so the assertion is that each id appears in the dispatch.
        """
        import inspect

        from kiro_crew.acp.client import AcpClient

        source = inspect.getsource(AcpClient._spawn)
        assert "ACP_BACKEND_CODEX" in source
        assert "ACP_BACKEND_GOOSE" in source
        assert "ACP_BACKEND_OPENCODE" in source
        assert "ACP_BACKEND_PI" in source
        assert "resolve_argv_cached" in source

    @pytest.mark.asyncio
    async def test_unmapped_known_backend_refuses_before_kiro_resolution(
        self, tmp_path: Path
    ) -> None:
        """A future static id must not inherit the Kiro spawn arm by absence."""
        from kiro_crew.acp.client import AcpClient, AcpError

        client = AcpClient(work_dir=tmp_path, acp_backend="future-static-adapter")
        kiro_resolver = AsyncMock(side_effect=AssertionError("Kiro resolver reached"))

        with (
            patch.object(
                backends,
                "known_ids",
                return_value=backends.known_ids() | {"future-static-adapter"},
            ),
            patch("kiro_crew.acp.client._resolve_kiro_bin_for_spawn", kiro_resolver),
        ):
            with pytest.raises(AcpError, match="no spawn implementation"):
                await client._spawn()

        kiro_resolver.assert_not_awaited()


class TestSpecAdaptersAreNotToldWeSupportElicitation:
    """Declaring `elicitation` silently cancels every codex MCP tool call.

    codex-acp gates MCP approvals on that capability: declare it and approvals
    arrive as `elicitation/create`, which Kiro Crew answers -32601, which the
    adapter converts to `action: "cancel"`. No prompt, no visible error, and the
    call never reaches the PreToolUse gate. The constant existed for this and was
    referenced nowhere.
    """

    def test_the_spec_set_omits_elicitation(self) -> None:
        assert "elicitation" in ACP_CLIENT_CAPABILITIES
        assert "elicitation" not in ACP_CLIENT_CAPABILITIES_SPEC_ADAPTER

    def test_initialize_selects_the_set_by_dialect(self) -> None:
        """Keyed on the dialect so an added spec adapter inherits it."""
        from kiro_crew.acp.client import _wire_contract_for_backend

        _, kiro_capabilities = _wire_contract_for_backend(ACP_BACKEND_KIRO)
        _, spec_capabilities = _wire_contract_for_backend(ACP_BACKEND_CODEX)

        assert kiro_capabilities is ACP_CLIENT_CAPABILITIES
        assert spec_capabilities is ACP_CLIENT_CAPABILITIES_SPEC_ADAPTER

    @pytest.mark.asyncio
    async def test_unmapped_dialect_refuses_before_initialize_wire(self, tmp_path: Path) -> None:
        """A future dialect must opt in to an exact wire contract."""
        from kiro_crew.acp.client import AcpClient, AcpError

        client = AcpClient(work_dir=tmp_path, acp_backend=ACP_BACKEND_KIRO)
        client._send_request = AsyncMock(side_effect=AssertionError("initialize reached"))

        with patch.object(backends, "dialect_of", return_value=object()):
            with pytest.raises(AcpError, match="Unsupported ACP dialect"):
                await client._initialize_session()

        client._send_request.assert_not_awaited()


class TestUsageCostIsReadNotDiscarded:
    """claude-agent-acp ships a real USD figure; Crew was dropping it."""

    def test_a_cost_block_is_parsed(self) -> None:
        amount, currency = parse_usage_cost(
            {"used": 10, "size": 100, "cost": {"amount": 0.42, "currency": "USD"}}
        )
        assert amount == 0.42
        assert currency == "USD"

    def test_an_absent_cost_block_is_the_norm_not_an_error(self) -> None:
        """codex-acp sends no cost at all; that must not read as a failure."""
        assert parse_usage_cost({"used": 10, "size": 100}) == (None, "")

    def test_a_fractional_amount_survives(self) -> None:
        """The token validator clamps to int, which would silently zero a cent."""
        amount, _ = parse_usage_cost({"cost": {"amount": 0.004, "currency": "USD"}})
        assert amount == 0.004

    @pytest.mark.parametrize(
        "bad",
        ["1.0", None, True, float("nan"), float("inf"), -1.0, [], {}],
    )
    def test_a_malformed_amount_degrades_to_absent(self, bad: object) -> None:
        """A cost that is not a real non-negative number must not enter a total.

        ``True`` is in this list deliberately: bool is a subclass of int, so a
        naive isinstance check would record a cost of 1.
        """
        assert parse_usage_cost({"cost": {"amount": bad}}) == (None, "")

    def test_currency_is_never_inferred(self) -> None:
        """A bare number with an assumed currency is a wrong number."""
        amount, currency = parse_usage_cost({"cost": {"amount": 1.5}})
        assert amount == 1.5
        assert currency == ""


class TestCorrectedCapabilityLevels:
    """Three levels were wrong, all under-claiming, all now evidenced."""

    def test_goose_turn_usage_is_not_unavailable(self) -> None:
        """goose is the BEST instrumented spec adapter, not the worst.

        Its 1.46.0 binary carries goose::acp::server::build_usage_updates and the
        `usage_update` serde tag.
        """
        assert backends.level(ACP_BACKEND_GOOSE, backends.CAP_TURN_USAGE) is backends.Level.DEGRADED

    def test_codex_turn_usage_is_not_unavailable(self) -> None:
        """codex forwards `used`/`size` per turn; it forwards no BILLING.

        The old comment conflated the two.
        """
        assert backends.level(ACP_BACKEND_CODEX, backends.CAP_TURN_USAGE) is backends.Level.DEGRADED

    def test_claude_billing_is_not_unavailable(self) -> None:
        """claude ships cost.amount in USD; Crew was discarding it, not lacking it."""
        from kiro_crew.acp.types import ACP_BACKEND_CLAUDE

        assert backends.level(ACP_BACKEND_CLAUDE, backends.CAP_BILLING) is backends.Level.DEGRADED

    def test_a_claimed_level_is_backed_by_a_reader(self) -> None:
        """Claiming billing while discarding the field is what went wrong before.

        So the claim and the code that honours it are asserted together.
        """
        import inspect

        from kiro_crew.acp.client import AcpClient

        assert "parse_usage_cost" in inspect.getsource(AcpClient._track_usage_update)
