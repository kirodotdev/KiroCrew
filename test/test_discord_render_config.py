"""Discord renderer: status reactions, the turn footer, and the two toggles.

Covers what ``discord.reactions_enabled`` and ``discord.show_thinking`` change
about a turn, and the ``-#`` turn footer the renderer closes with. The phase
ladder's own machinery (debounce, stall watchdog, close) is pinned in
``test_status_reactions.py``; here it is the WIRING that matters: which phases
Discord reports, on which message, and what the toggles suppress.
"""

from __future__ import annotations

import asyncio
import dataclasses
import json
from pathlib import Path
from typing import Any

import pytest

from kiro_crew.config import loader as loader_mod
from kiro_crew.config.loader import DiscordConfig, KiroCrewConfig, SlackConfig
from kiro_crew.discord import renderer as renderer_mod
from kiro_crew.discord.client import DISCORD_OK, DISCORD_TRANSIENT, DiscordApiResult
from kiro_crew.discord.renderer import DiscordRenderer
from kiro_crew.discord.transport import DISCORD_CAPABILITIES
from kiro_crew.messaging.status_reactions import LadderTimings, PhaseReactionLadder

_CHANNEL = "chan1"
_MSG = "msg1"

# Wire forms of the ladder's marks: Discord takes the emoji itself as a path
# segment, so these are what a reaction actually costs on the network.
_ROUTE_QUEUED = f"/channels/{_CHANNEL}/messages/{_MSG}/reactions/%F0%9F%91%80/@me"
_ROUTE_THINKING = f"/channels/{_CHANNEL}/messages/{_MSG}/reactions/%F0%9F%A4%94/@me"
_ROUTE_TOOL = f"/channels/{_CHANNEL}/messages/{_MSG}/reactions/%F0%9F%94%A7/@me"
_ROUTE_DONE = f"/channels/{_CHANNEL}/messages/{_MSG}/reactions/%F0%9F%A6%9E/@me"
_ROUTE_ERROR = f"/channels/{_CHANNEL}/messages/{_MSG}/reactions/%F0%9F%98%B1/@me"


class FakeClient:
    """Captures the Discord REST calls the renderer makes."""

    def __init__(self, *, api_ok: bool = True) -> None:
        self.sent: list[tuple[str, Any]] = []
        self.edits: list[tuple[str, str, Any]] = []
        self.sealed: list[str] = []
        self.api_calls: list[tuple[str, str]] = []
        self.api_ok = api_ok
        self._mid = 100

    async def send_typing(self, channel_id: str) -> None:
        return None

    async def send_message(
        self,
        channel_id: str,
        text: str,
        *,
        components: Any = None,
        reply_to_message_id: Any = None,
    ) -> str:
        self._mid += 1
        self.sent.append((text, components))
        return str(self._mid)

    async def edit_message(
        self, channel_id: str, message_id: str, text: str, *, components: Any = None
    ) -> bool:
        self.edits.append((message_id, text, components))
        return True

    async def edit_message_with_files(
        self,
        channel_id: str,
        message_id: str,
        text: str,
        files: Any,
        *,
        components: Any = None,
    ) -> bool:
        self.sealed.append(text)
        return True

    async def send_message_with_files(
        self, channel_id: str, text: str, files: Any, *, components: Any = None
    ) -> str:
        self._mid += 1
        self.sealed.append(text)
        return str(self._mid)

    async def api_json(
        self, method: str, path: str, payload: Any, *, timeout: int = 30
    ) -> DiscordApiResult:
        self.api_calls.append((method, path))
        if self.api_ok:
            return DiscordApiResult(outcome=DISCORD_OK, data={})
        return DiscordApiResult(outcome=DISCORD_TRANSIENT, detail="rate limited")

    @property
    def texts(self) -> list[str]:
        """Every visible payload, in the order the user sees it."""
        return [t for t, _ in self.sent] + [t for _, t, _ in self.edits] + self.sealed


class StepClock:
    """Monotonic clock the test moves explicitly."""

    def __init__(self, start: float = 1000.0) -> None:
        self.t = start

    def __call__(self) -> float:
        return self.t


class FakeProvider:
    """Stands in for the session provider the footer reads usage from."""

    def __init__(self, pct: float = 0.0, *, raises: bool = False) -> None:
        self._pct = pct
        self._raises = raises

    def context_usage_pct(self) -> float:
        if self._raises:
            raise RuntimeError("no window information")
        return self._pct


async def _settle() -> None:
    """Let the ladder's spawned reaction tasks and 0s timers run."""
    for _ in range(8):
        await asyncio.sleep(0)


@pytest.fixture(autouse=True)
def _instant_ladder(monkeypatch: pytest.MonkeyPatch) -> None:
    """Zero the ladder's debounce and park its watchdog far away.

    A 0-second debounce still goes through ``call_later``, so the ordering under
    test is the real one, but it lands on the next loop iteration instead of
    costing the suite the production 0.7s. The stall windows are pushed out of
    reach: the watchdog has its own tests, and firing here would add marks these
    assertions do not describe.
    """
    fast = LadderTimings(debounce=0.0, stall_soft=3600.0, stall_hard=7200.0)

    def _build(sink: Any, **kw: Any) -> PhaseReactionLadder:
        kw["timings"] = fast
        return PhaseReactionLadder(sink, **kw)

    monkeypatch.setattr(renderer_mod, "PhaseReactionLadder", _build)


def _renderer(**kw: Any) -> tuple[DiscordRenderer, FakeClient, StepClock]:
    client = FakeClient(api_ok=kw.pop("api_ok", True))
    clock = StepClock()
    kw.setdefault("react_message_id", _MSG)
    renderer = DiscordRenderer(
        client,  # type: ignore[arg-type]
        _CHANNEL,
        kw.pop("capabilities", DISCORD_CAPABILITIES),
        session_key="discord:u1",
        now=clock,
        **kw,
    )
    return renderer, client, clock


class TestStatusReactionWiring:
    """Which phases Discord reports, and on which message."""

    @pytest.mark.asyncio
    async def test_turn_start_marks_the_users_message_queued(self) -> None:
        renderer, client, _ = _renderer()
        await renderer.on_turn_start()
        await _settle()
        assert client.api_calls == [("PUT", _ROUTE_QUEUED)]
        await renderer.close()

    @pytest.mark.asyncio
    async def test_text_swaps_queued_for_thinking(self) -> None:
        renderer, client, _ = _renderer()
        await renderer.on_turn_start()
        await _settle()
        client.api_calls.clear()

        await renderer.on_text_chunk("hello")
        await _settle()
        assert client.api_calls == [("DELETE", _ROUTE_QUEUED), ("PUT", _ROUTE_THINKING)]
        await renderer.close()

    @pytest.mark.asyncio
    async def test_tool_call_reports_the_tools_phase(self) -> None:
        renderer, client, _ = _renderer()
        await renderer.on_turn_start()
        await _settle()
        client.api_calls.clear()

        await renderer.on_tool_call("t1", "Running: ls -la", "other")
        await _settle()
        assert client.api_calls == [("DELETE", _ROUTE_QUEUED), ("PUT", _ROUTE_TOOL)]
        await renderer.close()

    @pytest.mark.asyncio
    async def test_done_marks_the_terminal_phase(self) -> None:
        renderer, client, _ = _renderer()
        await renderer.on_turn_start()
        await renderer.on_text_chunk("answer")
        await _settle()
        client.api_calls.clear()

        await renderer.on_done()
        await renderer.close()
        assert client.api_calls == [("DELETE", _ROUTE_THINKING), ("PUT", _ROUTE_DONE)]

    @pytest.mark.asyncio
    async def test_failed_turn_marks_the_error_phase(self) -> None:
        renderer, client, _ = _renderer()
        await renderer.on_turn_start()
        await _settle()
        client.api_calls.clear()

        await renderer.on_done(stop_reason="error")
        await renderer.close()
        assert client.api_calls == [("DELETE", _ROUTE_QUEUED), ("PUT", _ROUTE_ERROR)]

    @pytest.mark.asyncio
    async def test_a_refused_reaction_never_costs_the_turn(self) -> None:
        renderer, client, _ = _renderer(api_ok=False)
        await renderer.on_turn_start()
        await renderer.on_text_chunk("answer")
        await _settle()
        await renderer.on_done()
        await renderer.close()
        assert client.api_calls  # attempted
        assert any("answer" in text for text in client.texts)  # and the turn landed

    @pytest.mark.asyncio
    async def test_close_leaves_no_ladder_task_behind(self) -> None:
        baseline = asyncio.all_tasks()
        renderer, _, _ = _renderer()
        await renderer.on_turn_start()
        await renderer.on_text_chunk("answer")
        await renderer.on_done()
        await renderer.close()
        assert asyncio.all_tasks() - baseline == set()


class TestReactionsEnabledToggle:
    """``discord.reactions_enabled=false`` suppresses every phase reaction."""

    @pytest.mark.asyncio
    async def test_disabled_makes_no_reaction_call_all_turn(self) -> None:
        renderer, client, _ = _renderer(reactions_enabled=False)
        await renderer.on_turn_start()
        await renderer.on_thinking("thought")
        await renderer.on_text_chunk("answer")
        await renderer.on_tool_call("t1", "Bash", "bash")
        await _settle()
        await renderer.on_done()
        await renderer.close()
        assert client.api_calls == []
        assert any("answer" in text for text in client.texts)  # the answer still lands

    @pytest.mark.asyncio
    async def test_no_message_to_react_on_means_no_ladder(self) -> None:
        renderer, client, _ = _renderer(react_message_id="")
        await renderer.on_turn_start()
        await renderer.on_text_chunk("answer")
        await _settle()
        await renderer.on_done()
        await renderer.close()
        assert client.api_calls == []

    @pytest.mark.asyncio
    async def test_a_transport_without_reactions_is_respected(self) -> None:
        caps = dataclasses.replace(DISCORD_CAPABILITIES, reactions=False)
        renderer, client, _ = _renderer(capabilities=caps)
        await renderer.on_turn_start()
        await renderer.on_text_chunk("answer")
        await _settle()
        await renderer.on_done()
        await renderer.close()
        assert client.api_calls == []


class TestTurnFooter:
    """The one-line ``-#`` footer: elapsed plus context usage, exactly once."""

    @pytest.mark.asyncio
    async def test_footer_carries_elapsed_and_context_usage(self) -> None:
        renderer, client, clock = _renderer()
        renderer.bind_context_source(FakeProvider(18.4))  # type: ignore[arg-type]
        await renderer.on_turn_start()
        await renderer.on_text_chunk("the answer")
        clock.t += 64.2
        await renderer.on_done()
        await renderer.close()
        assert client.sealed[-1] == "the answer\n\n-# Finished in 1m 4s · 🟢 ctx 18%"

    @pytest.mark.asyncio
    async def test_footer_renders_once_across_repeated_done(self) -> None:
        renderer, client, clock = _renderer()
        await renderer.on_turn_start()
        await renderer.on_text_chunk("the answer")
        clock.t += 3.0
        await renderer.on_done()
        await renderer.on_done()
        await renderer.close()
        assert sum(text.count("Finished in") for text in client.texts) == 1

    @pytest.mark.asyncio
    async def test_footer_lands_only_on_the_last_chunk_of_a_rotated_turn(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        renderer, client, clock = _renderer()
        monkeypatch.setattr(renderer, "_limit", lambda: 600)
        await renderer.on_turn_start()
        await renderer.on_text_chunk("word wordy\n" * 200)
        clock.t += 2.0
        await renderer.on_done()
        await renderer.close()
        assert len(client.sealed) > 1  # the answer did rotate
        assert sum(text.count("Finished in") for text in client.texts) == 1
        assert "Finished in" in client.sealed[-1]

    @pytest.mark.asyncio
    async def test_unknown_context_usage_carries_no_chip(self) -> None:
        renderer, client, clock = _renderer()
        await renderer.on_turn_start()
        await renderer.on_text_chunk("answer")
        clock.t += 7.0
        await renderer.on_done()
        await renderer.close()
        assert client.sealed[-1] == "answer\n\n-# Finished in 7s"

    @pytest.mark.asyncio
    async def test_a_provider_that_cannot_report_usage_is_not_fatal(self) -> None:
        renderer, client, clock = _renderer()
        renderer.bind_context_source(FakeProvider(raises=True))  # type: ignore[arg-type]
        await renderer.on_turn_start()
        await renderer.on_text_chunk("answer")
        clock.t += 7.0
        await renderer.on_done()
        await renderer.close()
        assert client.sealed[-1] == "answer\n\n-# Finished in 7s"

    @pytest.mark.asyncio
    async def test_silent_turn_stays_silent(self) -> None:
        """A turn whose output already landed in earlier segments posts nothing,
        footer included."""
        renderer, client, _ = _renderer()
        await renderer.on_turn_start()
        renderer._seal_count = 1  # an earlier rotation carried the answer
        await renderer.on_done()
        await renderer.close()
        assert client.texts == []

    @pytest.mark.asyncio
    async def test_empty_turn_placeholder_carries_the_footer(self) -> None:
        renderer, client, clock = _renderer()
        await renderer.on_turn_start()
        clock.t += 4.0
        await renderer.on_done(stop_reason="error")
        await renderer.close()
        assert client.texts == ["⚠️ Error — please try again\n\n-# Finished in 4s"]

    @pytest.mark.asyncio
    async def test_footer_is_dropped_rather_than_overflow_the_platform_cap(self) -> None:
        """The budget backstop: a segment with no room keeps the answer whole."""
        renderer, _, _ = _renderer()
        body = "x" * (renderer_mod.DISCORD_MAX_TEXT - 5)
        assert renderer._with_turn_footer(body) == body


class TestShowThinkingToggle:
    """``discord.show_thinking`` gates reasoning in both directions."""

    @pytest.mark.asyncio
    async def test_off_by_default_surfaces_nothing(self) -> None:
        renderer, client, _ = _renderer()
        await renderer.on_turn_start()
        await renderer.on_thinking("the private chain of thought")
        await renderer.on_text_chunk("answer")
        await renderer.on_done()
        await renderer.close()
        assert not any("chain of thought" in text for text in client.texts)
        assert not any(text.startswith("-# 💭") for text in client.texts)

    @pytest.mark.asyncio
    async def test_on_posts_reasoning_as_subtext_above_the_answer(self) -> None:
        renderer, client, _ = _renderer(show_thinking=True)
        await renderer.on_turn_start()
        await renderer.on_thinking("weighing the options")
        await renderer.on_text_chunk("answer")
        await renderer.on_done()
        await renderer.close()
        assert client.sent[0][0] == "-# 💭 weighing the options"
        assert "answer" in client.texts[1]

    @pytest.mark.asyncio
    async def test_on_posts_once_even_as_more_reasoning_arrives(self) -> None:
        renderer, client, _ = _renderer(show_thinking=True)
        await renderer.on_turn_start()
        await renderer.on_thinking("first")
        await renderer.on_text_chunk("answer")
        await renderer.on_thinking("second")
        await renderer.on_text_chunk(" more")
        await renderer.on_done()
        await renderer.close()
        assert sum(text.startswith("-# 💭") for text in client.texts) == 1

    @pytest.mark.asyncio
    async def test_on_still_posts_reasoning_from_a_turn_with_no_answer(self) -> None:
        renderer, client, _ = _renderer(show_thinking=True)
        await renderer.on_turn_start()
        await renderer.on_thinking("thought with no answer")
        await renderer.on_done()
        await renderer.close()
        assert client.sent[0][0] == "-# 💭 thought with no answer"

    @pytest.mark.asyncio
    async def test_multi_line_reasoning_marks_every_line(self) -> None:
        renderer, client, _ = _renderer(show_thinking=True)
        await renderer.on_turn_start()
        await renderer.on_thinking("first line\n\nsecond line")
        await renderer.on_text_chunk("answer")
        await renderer.close()
        assert client.sent[0][0] == "-# 💭 first line\n-# second line"

    @pytest.mark.asyncio
    async def test_reasoning_is_redacted_before_it_is_posted(self) -> None:
        secret = "AKIAIOSFODNN7EXAMPLE"
        renderer, client, _ = _renderer(show_thinking=True)
        await renderer.on_turn_start()
        await renderer.on_thinking(f"the key is {secret}")
        await renderer.on_text_chunk("answer")
        await renderer.close()
        assert secret not in client.sent[0][0]

    @pytest.mark.asyncio
    async def test_long_reasoning_is_previewed_not_dumped(self) -> None:
        renderer, client, _ = _renderer(show_thinking=True)
        await renderer.on_turn_start()
        await renderer.on_thinking("reason " * 400)
        await renderer.on_text_chunk("answer")
        await renderer.close()
        note = client.sent[0][0]
        assert note.endswith("…")
        assert len(note) < renderer_mod._THINKING_PREVIEW_CHARS + 20

    @pytest.mark.asyncio
    async def test_reasoning_still_advances_the_ladder_when_hidden(self) -> None:
        """The reaction reports what the agent is doing; the text toggle is
        about what the user reads, not about the phase."""
        renderer, client, _ = _renderer()
        await renderer.on_turn_start()
        await _settle()
        client.api_calls.clear()

        await renderer.on_thinking("hidden")
        await _settle()
        assert client.api_calls == [("DELETE", _ROUTE_QUEUED), ("PUT", _ROUTE_THINKING)]
        await renderer.close()


class TestDiscordConfigToggles:
    """The two config fields, mirroring SlackConfig's shape and defaults."""

    @staticmethod
    def _load(tmp_path: Path, monkeypatch: pytest.MonkeyPatch, data: dict) -> DiscordConfig:
        cfg_file = tmp_path / "config.json"
        cfg_file.write_text(json.dumps({"discord": data}), encoding="utf-8")
        monkeypatch.setattr(loader_mod, "config_path", lambda: cfg_file)
        return KiroCrewConfig.load().discord

    def test_defaults_are_reactions_on_and_thinking_off(self) -> None:
        cfg = DiscordConfig()
        assert cfg.reactions_enabled is True
        assert cfg.show_thinking is False

    def test_values_are_read_from_the_config_file(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        cfg = self._load(tmp_path, monkeypatch, {"reactions_enabled": False, "show_thinking": True})
        assert cfg.reactions_enabled is False
        assert cfg.show_thinking is True

    def test_an_unconfigured_section_keeps_the_defaults(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        cfg = self._load(tmp_path, monkeypatch, {})
        assert cfg.reactions_enabled is True
        assert cfg.show_thinking is False

    def test_metadata_shape_and_tags_mirror_slack(self) -> None:
        discord_fields = {f.name: f for f in dataclasses.fields(DiscordConfig)}
        slack_fields = {f.name: f for f in dataclasses.fields(SlackConfig)}
        for name in ("reactions_enabled", "show_thinking"):
            mine, theirs = discord_fields[name].metadata, slack_fields[name].metadata
            assert set(mine) == set(theirs)
            assert mine["label"] == theirs["label"]
            assert mine["tags"] == ["discord"]
            assert theirs["tags"] == ["slack"]

    def test_both_toggles_reach_the_schema_registry(self) -> None:
        """The dashboard panel and config-baseline.json read the registry."""
        from kiro_crew.config.schema import SCHEMA_REGISTRY

        paths = {entry.path: entry for entry in SCHEMA_REGISTRY}
        assert paths["discord.reactions_enabled"].default_value is True
        assert paths["discord.show_thinking"].default_value is False
