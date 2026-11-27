"""Discord's approval window and its timing-footer switch, both at Slack parity.

* The approval prompt waits ``agent.tool_approval_timeout_secs``, read at each wait
  through ``dashboard.turn_dispatch.tool_approval_timeout_secs`` as Slack does,
  unless a test pins ``_APPROVAL_TIMEOUT_S``. It still denies when the window
  closes, and a prompt that never went out (a muted conversation) still denies
  at once.
* ``discord.show_timing_footer`` off leaves the ``-# Finished in …`` line off the
  final message and changes nothing else about the reply, a split one included.
"""

from __future__ import annotations

import asyncio
import dataclasses
import threading
from types import SimpleNamespace
from typing import Any

import pytest
from discord_helpers import _dispatcher, _live_discord

from kiro_crew.config.loader import KiroCrewConfig
from kiro_crew.constants import (
    DENY_CAUSE_APPROVAL_TIMEOUT,
    DENY_CAUSE_APPROVAL_UNDELIVERABLE,
)
from kiro_crew.dashboard import turn_dispatch
from kiro_crew.discord import renderer_approval
from kiro_crew.discord.client import EDIT_OK
from kiro_crew.discord.renderer import DiscordRenderer
from kiro_crew.discord.renderer_approval import DiscordApprovalDecider
from kiro_crew.discord.transport import DISCORD_CAPABILITIES


def _event(request_id: str) -> SimpleNamespace:
    return SimpleNamespace(request_id=request_id, title="bash", options=[])


def _config(window: float | None) -> KiroCrewConfig:
    """A loaded config; ``None`` leaves ``agent.tool_approval_timeout_secs`` unset."""
    base = KiroCrewConfig()
    if window is None:
        return base
    return dataclasses.replace(
        base, agent=dataclasses.replace(base.agent, tool_approval_timeout_secs=window)
    )


def _recording_wait_for(seen: list[float], *, press: bool | None = None):
    """``wait_for`` without the sleep: record the window, then expire or press."""

    async def _wait_for(fut: "asyncio.Future[bool]", timeout: float) -> bool:
        seen.append(timeout)
        if press is None:
            fut.cancel()
            raise asyncio.TimeoutError
        fut.set_result(press)
        return await fut

    return _wait_for


@pytest.fixture
def _clean_registry():
    yield
    DiscordApprovalDecider._REGISTRY.clear()
    DiscordApprovalDecider._NONCES.clear()
    DiscordApprovalDecider._DETACHED.clear()
    DiscordApprovalDecider._UNDELIVERED.clear()


def test_the_decider_binds_the_dashboard_resolver() -> None:
    assert renderer_approval.tool_approval_timeout_secs is turn_dispatch.tool_approval_timeout_secs
    assert renderer_approval._APPROVAL_TIMEOUT_S is None


@pytest.mark.usefixtures("_clean_registry")
class TestApprovalWindow:
    @pytest.mark.asyncio
    @pytest.mark.parametrize(
        ("written", "window"),
        [(None, 600.0), (600, 600.0), (1200, 1200.0)],
        ids=["unset-default", "set-600", "set-1200"],
    )
    async def test_waits_the_configured_window_then_denies(
        self, monkeypatch: pytest.MonkeyPatch, written: int | None, window: float
    ) -> None:
        monkeypatch.setattr(
            turn_dispatch.KiroCrewConfig, "load", staticmethod(lambda: _config(written))
        )
        seen: list[float] = []
        monkeypatch.setattr(renderer_approval.asyncio, "wait_for", _recording_wait_for(seen))
        decider = DiscordApprovalDecider(session_key="discord:u1")
        decider.register_nonce(decider.key("discord:u1", "r1"))

        assert await decider(_event("r1")) is False
        assert seen == [window]
        assert decider.last_deny_cause == DENY_CAUSE_APPROVAL_TIMEOUT
        assert decider.key("discord:u1", "r1") not in DiscordApprovalDecider._NONCES

    @pytest.mark.asyncio
    async def test_the_config_read_runs_off_the_event_loop(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """The resolver loads config.json, so it must not block the gateway loop."""
        threads: list[int] = []

        def _load() -> KiroCrewConfig:
            threads.append(threading.get_ident())
            return _config(600)

        monkeypatch.setattr(turn_dispatch.KiroCrewConfig, "load", staticmethod(_load))
        seen: list[float] = []
        monkeypatch.setattr(renderer_approval.asyncio, "wait_for", _recording_wait_for(seen))
        decider = DiscordApprovalDecider(session_key="discord:u5")
        decider.register_nonce(decider.key("discord:u5", "r1"))

        assert await decider(_event("r1")) is False
        assert seen == [600.0]
        assert threads and threading.get_ident() not in threads

    @pytest.mark.asyncio
    async def test_a_press_inside_the_window_answers(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setattr(
            turn_dispatch.KiroCrewConfig, "load", staticmethod(lambda: _config(600))
        )
        seen: list[float] = []
        monkeypatch.setattr(
            renderer_approval.asyncio, "wait_for", _recording_wait_for(seen, press=True)
        )
        decider = DiscordApprovalDecider(session_key="discord:u2")
        decider.register_nonce(decider.key("discord:u2", "r1"))

        assert await decider(_event("r1")) is True
        assert seen == [600.0]
        assert decider.last_deny_cause == ""

    @pytest.mark.asyncio
    async def test_a_muted_prompt_still_denies_at_once(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """No reservation means no prompt went out: no window is read or waited."""
        reads: list[None] = []

        def _load() -> KiroCrewConfig:
            reads.append(None)
            return _config(600)

        monkeypatch.setattr(turn_dispatch.KiroCrewConfig, "load", staticmethod(_load))
        seen: list[float] = []
        monkeypatch.setattr(renderer_approval.asyncio, "wait_for", _recording_wait_for(seen))
        decider = DiscordApprovalDecider(session_key="discord:u3")

        assert await decider(_event("r1")) is False
        assert decider.last_deny_cause == DENY_CAUSE_APPROVAL_UNDELIVERABLE
        assert seen == [] and reads == []

    @pytest.mark.asyncio
    async def test_a_pinned_window_wins_over_the_setting(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        reads: list[None] = []

        def _load() -> KiroCrewConfig:
            reads.append(None)
            return _config(600)

        monkeypatch.setattr(turn_dispatch.KiroCrewConfig, "load", staticmethod(_load))
        monkeypatch.setattr(  # flake-ok: the stubbed wait_for never sleeps on it
            renderer_approval, "_APPROVAL_TIMEOUT_S", 0.25
        )
        seen: list[float] = []
        monkeypatch.setattr(renderer_approval.asyncio, "wait_for", _recording_wait_for(seen))
        decider = DiscordApprovalDecider(session_key="discord:u4")
        decider.register_nonce(decider.key("discord:u4", "r1"))

        assert await decider(_event("r1")) is False
        assert seen == [0.25] and reads == []


class _Client:
    """Records every payload and its components, in the order Discord gets them."""

    def __init__(self) -> None:
        self.calls: list[tuple[str, str, Any]] = []
        self._mid = 100

    async def send_typing(self, channel_id: str) -> None:
        return None

    async def send_message(self, channel_id: str, text: str, **kw: Any) -> str:
        self._mid += 1
        self.calls.append(("send", text, kw.get("components")))
        return str(self._mid)

    async def edit_message(self, channel_id: str, mid: str, text: str, **kw: Any) -> bool:
        self.calls.append(("edit", text, kw.get("components")))
        return True

    async def edit_message_with_files(self, channel_id, mid, text, files, **kw: Any) -> bool:
        self.calls.append(("seal", text, kw.get("components")))
        return True

    async def edit_message_with_files_outcome(self, channel_id, mid, text, files, **kw):
        await self.edit_message_with_files(channel_id, mid, text, files, **kw)
        return EDIT_OK

    async def send_message_with_files(self, channel_id, text, files, **kw: Any) -> str:
        self._mid += 1
        self.calls.append(("seal", text, kw.get("components")))
        return str(self._mid)


class _Clock:
    def __init__(self) -> None:
        self.t = 1000.0

    def __call__(self) -> float:
        return self.t


async def _run_turn(
    chunks: list[str], *, footer: bool, limit: int | None = None, ok: bool = True
) -> list[tuple[str, str, Any]]:
    """Stream *chunks* through one renderer turn and return what Discord got."""
    client = _Client()
    clock = _Clock()
    renderer = DiscordRenderer(
        client,  # type: ignore[arg-type]
        "chan1",
        DISCORD_CAPABILITIES,
        session_key="discord:u1",
        reactions_enabled=False,
        show_timing_footer=footer,
        now=clock,
    )
    if limit is not None:
        renderer._limit = lambda: limit  # type: ignore[method-assign]
    await renderer.on_turn_start()
    for chunk in chunks:
        clock.t += 5.0  # past the edit throttle, so each chunk is a live frame
        await renderer.on_text_chunk(chunk)
    clock.t += 7.0
    await renderer.on_done(stop_reason=None if ok else "error")
    await renderer.close()
    return client.calls


def _strip_footer(text: str) -> str:
    head, sep, _ = text.rpartition("\n\n-# Finished in ")
    return head if sep else text


class TestTimingFooterSwitch:
    def test_the_field_defaults_on_like_slack(self) -> None:
        assert KiroCrewConfig().discord.show_timing_footer is True
        assert KiroCrewConfig().slack.show_timing_footer is True

    def test_the_dispatcher_reads_it_per_turn_and_defaults_on(self) -> None:
        d, _cli, _sess = _dispatcher({"u1"})
        with _live_discord():
            assert d._render_config()[2] is True
        with _live_discord(show_timing_footer=False):
            assert d._render_config()[2] is False

    @pytest.mark.asyncio
    async def test_on_by_default_the_reply_ends_with_the_footer(self) -> None:
        calls = await _run_turn(["the answer"], footer=True)
        assert calls[-1][1] == "the answer\n\n-# Finished in 12s"

    @pytest.mark.asyncio
    async def test_off_posts_no_footer_and_changes_nothing_else(self) -> None:
        on = await _run_turn(["the answer [OPTIONS: Yes | No]"], footer=True)
        off = await _run_turn(["the answer [OPTIONS: Yes | No]"], footer=False)
        assert not any("Finished in" in text for _, text, _ in off)
        assert off[-1][2], "the choice buttons still ride the reply"
        assert off == [(kind, _strip_footer(text), comps) for kind, text, comps in on]

    @pytest.mark.asyncio
    async def test_off_a_split_reply_splits_at_the_same_places(self) -> None:
        chunks = ["word wordy\n" * 40] * 5
        on = await _run_turn(chunks, footer=True, limit=600)
        off = await _run_turn(chunks, footer=False, limit=600)
        assert sum(kind == "seal" for kind, _, _ in off) > 1, "the reply did split"
        assert any(kind == "edit" for kind, _, _ in off), "it split while streaming"
        assert not any("Finished in" in text for _, text, _ in off)
        assert off == [(kind, _strip_footer(text), comps) for kind, text, comps in on]

    @pytest.mark.asyncio
    async def test_off_the_error_placeholder_stands_alone(self) -> None:
        off = await _run_turn([], footer=False, ok=False)
        assert [text for _, text, _ in off] == ["\u26a0\ufe0f Error \u2014 please try again"]
