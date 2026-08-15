"""The turn ceiling is configurable above 2h, and the transport follows it.

Before this change ``agent.chat_turn_timeout_secs`` was clamped to 7200s
because the ACP transport's own per-prompt wait was a fixed 7200s constant
underneath it — a larger configured ceiling would advertise a limit the
transport did not honour. Long unattended turns (a full test-suite run inside
one babysit turn) hit that wall routinely.

Now ``resolve_prompt_timeout`` makes the transport wait FOLLOW the configured
ceiling (plus a margin so the dashboard's visible turn-limit card always fires
before the transport cut), and the loader bound is raised to 24h. These tests
pin the contract:

* the transport wait never shrinks below its 2h default (non-dashboard callers
  share it), and follows a raised ceiling with the margin on top;
* an explicit caller timeout always wins over config;
* the loader accepts values up to ``CHAT_TURN_TIMEOUT_MAX`` (86400) and still
  clamps above it;
* end-to-end, a raised ceiling reaches ``chat_turn_timeout_secs`` unclamped —
  the honesty clamp stays but no longer fires in normal operation;
* the ceiling card names day-scale limits correctly.
"""

from __future__ import annotations

import logging
from types import SimpleNamespace

import pytest

from kiro_crew.acp import client as acp_client
from kiro_crew.acp.client import (
    _DEFAULT_PROMPT_TIMEOUT,
    _PROMPT_TIMEOUT_MARGIN_SECS,
    _effective_prompt_timeout,
    resolve_prompt_timeout,
)
from kiro_crew.config.loader import (
    CHAT_TURN_TIMEOUT_MAX,
    CHAT_TURN_TIMEOUT_MIN,
    KiroCrewConfig,
    _clamp_security_bounds,
    _safe_int,
)
from kiro_crew.dashboard import turn_dispatch as td


def _patch_loaded_ceiling(monkeypatch, value: object) -> None:
    """Patch ``KiroCrewConfig.load`` on the CLASS.

    The resolver imports the class lazily inside the function and
    ``turn_dispatch`` holds a module-scope reference — both resolve to the same
    class object, so one patch covers the whole path end-to-end. ``value=None``
    simulates config being unavailable entirely.
    """
    if value is None:

        def _raise(cls):  # noqa: ANN001
            raise RuntimeError("no config here")

        monkeypatch.setattr(KiroCrewConfig, "load", classmethod(_raise))
        return
    cfg = SimpleNamespace(agent=SimpleNamespace(chat_turn_timeout_secs=value))
    monkeypatch.setattr(KiroCrewConfig, "load", classmethod(lambda cls: cfg))


class TestResolvePromptTimeout:
    def test_config_unavailable_keeps_the_default(self, monkeypatch) -> None:
        """Tests and early bootstrap behave exactly like a default config."""
        _patch_loaded_ceiling(monkeypatch, None)
        assert resolve_prompt_timeout() == _DEFAULT_PROMPT_TIMEOUT

    def test_never_shrinks_below_the_default(self, monkeypatch) -> None:
        """A LOWERED ceiling is the dashboard deadline's job; shrinking the
        transport wait with it would also cut non-dashboard callers (subagents,
        review runs) that share this default."""
        _patch_loaded_ceiling(monkeypatch, 1800)
        assert resolve_prompt_timeout() == _DEFAULT_PROMPT_TIMEOUT

    def test_exactly_default_stays_byte_identical(self, monkeypatch) -> None:
        """No margin at the default: every existing install (and everything
        derived from the transport wait, like the watchdog window budget) keeps
        today's numbers unless the operator raises the ceiling."""
        _patch_loaded_ceiling(monkeypatch, 7200)
        assert resolve_prompt_timeout() == _DEFAULT_PROMPT_TIMEOUT

    def test_follows_a_raised_ceiling_with_margin(self, monkeypatch) -> None:
        """The margin keeps the dashboard's card firing before the transport
        cut — equal deadlines would race and report a raw timeout instead."""
        _patch_loaded_ceiling(monkeypatch, 86400)
        assert resolve_prompt_timeout() == 86400.0 + _PROMPT_TIMEOUT_MARGIN_SECS

    def test_non_positive_value_keeps_the_default(self, monkeypatch) -> None:
        """Defense in depth: the loader clamps first, but the resolver must not
        turn a corrupt value into an instantly-dead prompt."""
        _patch_loaded_ceiling(monkeypatch, 0)
        assert resolve_prompt_timeout() == _DEFAULT_PROMPT_TIMEOUT


class TestEffectivePromptTimeout:
    def test_explicit_caller_timeout_wins(self, monkeypatch) -> None:
        _patch_loaded_ceiling(monkeypatch, 86400)
        assert _effective_prompt_timeout(300.0) == 300.0

    def test_none_resolves_from_config(self, monkeypatch) -> None:
        _patch_loaded_ceiling(monkeypatch, 86400)
        assert _effective_prompt_timeout(None) == resolve_prompt_timeout()


class TestEffectivePromptTimeoutAsync:
    """The async twin must never run the config disk read on the event loop."""

    @pytest.mark.asyncio
    async def test_explicit_caller_timeout_skips_config_read(self, monkeypatch) -> None:
        def _boom() -> float:  # pragma: no cover - must not be reached
            raise AssertionError("config resolver called despite explicit timeout")

        monkeypatch.setattr(acp_client, "resolve_prompt_timeout", _boom)
        assert await acp_client._effective_prompt_timeout_async(300.0) == 300.0

    @pytest.mark.asyncio
    async def test_none_resolves_off_the_event_loop(self, monkeypatch) -> None:
        import asyncio
        import threading

        loop_thread = threading.current_thread()
        seen: list[threading.Thread] = []

        def _record() -> float:
            seen.append(threading.current_thread())
            return 1234.0

        monkeypatch.setattr(acp_client, "resolve_prompt_timeout", _record)
        assert await acp_client._effective_prompt_timeout_async(None) == 1234.0
        assert seen and seen[0] is not loop_thread, (
            "resolve_prompt_timeout must run via asyncio.to_thread, not inline "
            "on the event loop"
        )
        assert isinstance(asyncio.get_running_loop(), asyncio.AbstractEventLoop)


class TestLoaderBounds:
    def test_day_scale_value_survives_coercion(self) -> None:
        assert (
            _safe_int(86400, 7200, CHAT_TURN_TIMEOUT_MIN, CHAT_TURN_TIMEOUT_MAX)
            == 86400
        )

    def test_above_the_new_max_still_clamps(self) -> None:
        assert (
            _safe_int(999999, 7200, CHAT_TURN_TIMEOUT_MIN, CHAT_TURN_TIMEOUT_MAX)
            == CHAT_TURN_TIMEOUT_MAX
        )

    def test_security_bounds_accept_a_day_and_clamp_above(self) -> None:
        data = {"agent": {"chat_turn_timeout_secs": 86400}}
        _clamp_security_bounds(data)
        assert data["agent"]["chat_turn_timeout_secs"] == 86400

        data = {"agent": {"chat_turn_timeout_secs": 999999}}
        _clamp_security_bounds(data)
        assert data["agent"]["chat_turn_timeout_secs"] == CHAT_TURN_TIMEOUT_MAX


class TestRaisedCeilingEndToEnd:
    def test_raised_ceiling_reaches_the_dispatch_unclamped(
        self, monkeypatch, caplog
    ) -> None:
        """The full path: config → resolver → transport ceiling → dispatch.

        With the transport following the configured value, the honesty clamp in
        ``chat_turn_timeout_secs`` must not fire — the turn really can run this
        long, so the resolved ceiling is the configured one.
        """
        _patch_loaded_ceiling(monkeypatch, 86400)
        with caplog.at_level(logging.WARNING, logger=td.logger.name):
            assert td.chat_turn_timeout_secs() == 86400.0
        assert "clamping" not in caplog.text

    def test_clamp_remains_as_the_fail_safe(self, monkeypatch, caplog) -> None:
        """If the transport could not follow (resolver fell back to default),
        the dispatch must still refuse to advertise a limit the transport does
        not honour."""
        _patch_loaded_ceiling(monkeypatch, 86400)
        monkeypatch.setattr(td, "resolve_prompt_timeout", lambda: 7200.0)
        with caplog.at_level(logging.WARNING, logger=td.logger.name):
            assert td.chat_turn_timeout_secs() == 7200.0
        assert "clamping" in caplog.text


class TestCardWording:
    @pytest.mark.parametrize(
        ("secs", "label"),
        [
            (7200.0, "2-hour"),
            (86400.0, "24-hour"),
            (10800.0, "3-hour"),
        ],
    )
    def test_hour_scale_labels(self, secs: float, label: str) -> None:
        assert label in td.format_turn_timeout_card(secs)


class TestTransportEntryPointsResolveNone:
    """Every public prompt entry point defaults to config resolution.

    Pinned structurally (signature defaults) rather than by driving a live
    transport: the contract is exactly 'None means resolve from config', and
    ``_effective_prompt_timeout`` is tested above.
    """

    def test_client_entry_points_default_to_none(self) -> None:
        import inspect

        for name in ("send_message", "send_message_stream", "stream_events", "stream_command"):
            sig = inspect.signature(getattr(acp_client.AcpClient, name))
            assert sig.parameters["timeout"].default is None, name

    def test_session_handle_prompt_defaults_to_none(self) -> None:
        import inspect

        from kiro_crew.acp.session_handle import AcpSessionHandle

        sig = inspect.signature(AcpSessionHandle.prompt)
        assert sig.parameters["timeout"].default is None
