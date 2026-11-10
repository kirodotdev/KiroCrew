"""A cron or channel claim re-projects a stale KAS batch.

``spec_hooks.reproject_unattended_claim`` runs after the turn's Stop snapshot,
opens the replay gap before its first reset, and leaves it for the caller to
close after its release. A batch still stale after two resets keeps the
stale-batch fallback instead of refusing the turn. The shared channel pipeline is
driven end to end through the channel-turn fakes.
"""

from __future__ import annotations

import asyncio
from typing import Any

import pytest
from channel_turn_fakes import FakeSessions, RecordingRenderer, ScriptedProvider, answer
from channel_turns_helpers import _answer, _turns

from kiro_crew.acp.types import STOP_REASON_CANCELLED
from kiro_crew.agent_sdk import spec_hooks
from kiro_crew.messaging.dispatch import Verdict


def _stale_for(*providers: Any):
    """A ``hook_projection_stale`` that answers True for *providers* only."""

    async def _stale(provider: Any, _agent: str) -> bool:
        return any(provider is p for p in providers)

    return _stale


class _Sessions:
    """The parts of the session manager the helper touches, as one ledger."""

    def __init__(self, *, on_reset: Any = None, reset_raises: bool = False) -> None:
        self.ledger: list[str] = []
        self.gap_open = False
        self.claims = 0
        self.on_reset = on_reset
        self.reset_raises = reset_raises

    def open_replay_gap(self, key: str) -> None:
        self.gap_open = True
        self.ledger.append("open_replay_gap")

    def close_replay_gap(self, key: str) -> None:
        self.gap_open = False
        self.ledger.append("close_replay_gap")

    async def reset(self, key: str) -> None:
        self.ledger.append("reset")
        if self.reset_raises:
            raise RuntimeError("reset failed")
        if self.on_reset is not None:
            self.on_reset(self)

    def adopt_turn(self, key: str) -> None:
        self.ledger.append("adopt_turn")


class _FenceSessions:
    """A session manager whose ``reset`` honours ``expect_session`` and whose
    live entry is readable through ``_sessions``/``_fold_key``, so the fence that
    refuses to reset a swapped-in successor can be exercised."""

    def __init__(self, entries: dict[str, Any]) -> None:
        self._sessions = dict(entries)
        self.ledger: list[str] = []
        self.gap_open = False

    def _fold_key(self, key: str) -> str:
        return key

    def open_replay_gap(self, key: str) -> None:
        self.gap_open = True
        self.ledger.append("open")

    def close_replay_gap(self, key: str) -> None:
        self.gap_open = False
        self.ledger.append("close")

    async def reset(self, key: str, *, expect_session: Any = None) -> bool:
        self.ledger.append("reset")
        current = self._sessions.get(key)
        if expect_session is not None and current is not expect_session:
            return False
        self._sessions.pop(key, None)
        return True

    def adopt_turn(self, key: str) -> None:
        self.ledger.append("adopt")


def _run(sessions: Any, first: Any, *successors: Any) -> spec_hooks.UnattendedClaim:
    queue = list(successors)

    async def _claim() -> tuple[tuple[Any, bool, bool], Any]:
        sessions.claims += 1
        sessions.ledger.append("claim")
        got = queue.pop(0) if len(queue) > 1 else queue[0]
        return (got, True, False), spec_hooks.live_session_entry(sessions, "k")

    token = spec_hooks.make_claim_token(sessions, "k")
    return asyncio.run(
        spec_hooks.reproject_unattended_claim(
            sessions, "k", "agent", (first, False, False), _claim, token
        )
    )


class TestTheHelper:
    def test_a_fresh_batch_is_left_alone(self, monkeypatch) -> None:
        old = object()
        monkeypatch.setattr(spec_hooks, "hook_projection_stale", _stale_for())
        sessions = _Sessions()

        got = _run(sessions, old, object())

        assert got == spec_hooks.UnattendedClaim((old, False, False), False, False)
        assert sessions.ledger == []

    def test_a_stale_batch_is_reset_and_claimed_again_under_the_gap(self, monkeypatch) -> None:
        old, new = object(), object()
        monkeypatch.setattr(spec_hooks, "hook_projection_stale", _stale_for(old))
        sessions = _Sessions()

        got = _run(sessions, old, new)

        assert got.claimed == (new, True, False)
        assert got.reprojected is True and got.still_stale is False
        # Opened before the reset; left open for the caller to close after release.
        assert sessions.ledger == ["open_replay_gap", "reset", "claim", "adopt_turn"]
        assert sessions.gap_open is True

    def test_still_stale_after_two_resets_falls_back_without_refusing(self, monkeypatch) -> None:
        old, new = object(), object()
        monkeypatch.setattr(spec_hooks, "hook_projection_stale", _stale_for(old, new))
        sessions = _Sessions()

        got = _run(sessions, old, new)

        assert got.claimed[0] is new and got.reprojected is True and got.still_stale is True
        assert sessions.ledger.count("reset") == spec_hooks._MAX_REPROJECTIONS
        assert sessions.ledger.count("open_replay_gap") == 1

    def test_a_hard_stop_that_swaps_the_session_in_the_probe_is_not_reset(
        self, monkeypatch
    ) -> None:
        # The entry the turn claimed, and the successor a hard Stop + a newer
        # message leave under the key while the staleness probe awaits.
        old, successor = object(), object()
        sessions = _FenceSessions({"k": old})

        async def _probe(_provider: object, _agent: str) -> bool:
            # The hard Stop pops the claimed session and a successor is claimed,
            # all while this probe (the first await) runs.
            sessions._sessions["k"] = successor
            return True

        monkeypatch.setattr(spec_hooks, "hook_projection_stale", _probe)

        async def _claim() -> tuple[tuple[Any, bool, bool], Any]:
            raise AssertionError("must not re-claim after the session was superseded")

        token = spec_hooks.make_claim_token(sessions, "k")
        got = asyncio.run(
            spec_hooks.reproject_unattended_claim(
                sessions, "k", "agent", (old, False, False), _claim, token
            )
        )

        assert got.superseded is True and got.claimed == (old, False, False)
        # The successor was refused, not reset, and still holds the key.
        assert sessions._sessions["k"] is successor
        assert sessions.ledger.count("reset") == 1 and "adopt" not in sessions.ledger
        # The gap opened before the reset is left for the caller to close.
        assert sessions.gap_open is True

    def test_a_fenced_reset_of_the_turns_own_session_re_projects(self, monkeypatch) -> None:
        old, new = object(), object()
        sessions = _FenceSessions({"k": old})
        stale = iter([True, False])

        async def _probe(_provider: object, _agent: str) -> bool:
            return next(stale)

        monkeypatch.setattr(spec_hooks, "hook_projection_stale", _probe)

        async def _claim() -> tuple[tuple[Any, bool, bool], Any]:
            sessions._sessions["k"] = new
            return (new, True, False), new

        token = spec_hooks.make_claim_token(sessions, "k")
        got = asyncio.run(
            spec_hooks.reproject_unattended_claim(
                sessions, "k", "agent", (old, False, False), _claim, token
            )
        )

        assert got.superseded is False and got.reprojected is True
        assert got.claimed == (new, True, False)
        assert sessions.ledger == ["open", "reset", "adopt"]

    def test_a_failed_reset_closes_the_gap_it_opened(self, monkeypatch) -> None:
        old = object()
        monkeypatch.setattr(spec_hooks, "hook_projection_stale", _stale_for(old))
        sessions = _Sessions(reset_raises=True)

        with pytest.raises(RuntimeError):
            _run(sessions, old, object())

        assert sessions.ledger == ["open_replay_gap", "reset", "close_replay_gap"]
        assert sessions.gap_open is False

    def test_the_dashboard_claim_still_refuses_a_batch_it_cannot_cure(self, monkeypatch) -> None:
        old, new = object(), object()
        monkeypatch.setattr(spec_hooks, "hook_projection_stale", _stale_for(old, new))
        sessions = _Sessions()

        async def _claim() -> tuple[Any, bool, bool]:
            return new, True, False

        with pytest.raises(spec_hooks.StaleProjectionError):
            asyncio.run(
                spec_hooks.reproject_claimed_session(
                    sessions, "k", "agent", (old, False, False), _claim
                )
            )
        assert "open_replay_gap" not in sessions.ledger


_SRC = __import__("pathlib").Path(spec_hooks.__file__).resolve().parents[1]

# Every surface that re-projects an unattended claim, and the call-bearing file.
_CALLER_FILES = [
    "messaging/dispatch.py",
    "slack/transport_dispatch.py",
    "telegram/dispatch/turn_engine.py",
    "slack/gateway_runtime/cron_turn.py",
]


class TestEveryCallerThreadsTheClaimToken:
    """The root-cause guard: every caller captures its claim token at the
    acquisition and passes it into the helper, so the helper never reads the live
    session and a swapped-in successor cannot be reset. Parametrized over the
    caller kinds (the shared pipeline carries both inbound and monitor wake; cron
    carries both the single and the sequence site)."""

    @pytest.mark.parametrize("rel", _CALLER_FILES)
    def test_every_reproject_call_passes_a_token(self, rel: str) -> None:
        import ast

        tree = ast.parse((_SRC / rel).read_text(encoding="utf-8"))
        calls = [
            node
            for node in ast.walk(tree)
            if isinstance(node, ast.Call)
            and (
                (isinstance(node.func, ast.Name) and node.func.id == "reproject_unattended_claim")
                or (
                    isinstance(node.func, ast.Attribute)
                    and node.func.attr == "reproject_unattended_claim"
                )
            )
        ]
        assert calls, f"{rel} no longer calls reproject_unattended_claim"
        for call in calls:
            # sessions, session_key, agent, claimed, claim, token.
            assert len(call.args) == 6, f"{rel}:{call.lineno} must pass the claim token"

    def test_the_monitor_captures_its_token_before_renderer_setup(self) -> None:
        # The race GPT flagged: the wake claims, then awaits its renderer config.
        # The acquire mints the token before the renderer setup, so a Stop +
        # successor claim in the window cannot slip past the fence or baseline.
        text = (_SRC / "messaging/dispatch.py").read_text(encoding="utf-8")
        acquire = text.index("await acquire_with_token(")
        renderer_setup = text.index("renderer = await source(muted)")
        assert acquire < renderer_setup

    def test_cron_acquire_mints_the_token_before_the_rebind_await(self) -> None:
        # The race GPT flagged on cron: _acquire_with_model_fallback awaits
        # _rebind_prepared_execution after get_or_create. The token must be minted
        # between the claim and that await, in every arm, so a Stop + successor
        # during the rebind cannot make the token name the successor.
        import re

        text = (_SRC / "slack/gateway_runtime/cron_fire.py").read_text(encoding="utf-8")
        rebinds = [m.start() for m in re.finditer(r"await _rebind_prepared_execution", text)]
        assert rebinds, "cron acquire no longer rebinds"
        for pos in rebinds:
            head = text[:pos]
            claim = head.rfind("await self.sessions.get_or_create(")
            mint = head.rfind("_claim_token(self.sessions, key)")
            assert claim != -1, "a rebind await with no preceding claim"
            assert mint > claim, "the token must be minted after the claim, before the rebind await"


class TestTheSharedChannelPipeline:
    def test_a_stale_claim_runs_on_the_re_projected_session(self, monkeypatch) -> None:
        old = ScriptedProvider(answer("must not run"), name="old")
        new = ScriptedProvider(answer("the reply"), name="new")
        monkeypatch.setattr(spec_hooks, "hook_projection_stale", _stale_for(old))
        sessions = FakeSessions(old, new)

        outcome = _answer(_turns(sessions))

        assert outcome.verdict is Verdict.ANSWERED
        assert old.prompts == [] and new.prompts == ["hi"]
        names = sessions.names()
        assert names.count("get_or_create") == 2 and names.count("release") == 1
        assert names.index("open_replay_gap") < names.index("reset")
        assert names.index("release") < names.index("close_replay_gap")
        assert sessions.gap_open is False

    def test_a_stop_inside_the_re_projection_ends_the_turn_unprompted(self, monkeypatch) -> None:
        old = ScriptedProvider(answer("must not run"), name="old")
        new = ScriptedProvider(answer("must not run"), name="new")
        monkeypatch.setattr(spec_hooks, "hook_projection_stale", _stale_for(old))

        def _stop_in_gap(sessions: FakeSessions) -> None:
            # Recorded only because the gap is open while the key has no session.
            if sessions.gap_open:
                sessions.stop_gen += 1

        sessions = FakeSessions(old, new, on_reset=_stop_in_gap)
        renderer = RecordingRenderer()

        outcome = _answer(_turns(sessions), renderer=renderer)

        assert outcome.verdict is Verdict.ABANDONED
        assert old.prompts == [] and new.prompts == []
        assert [e.stop_reason for e in renderer.done] == [STOP_REASON_CANCELLED]
        assert sessions.count("release") == 1 and sessions.gap_open is False

    def test_a_fresh_claim_opens_no_gap(self, monkeypatch) -> None:
        monkeypatch.setattr(spec_hooks, "hook_projection_stale", _stale_for())
        sessions = FakeSessions(ScriptedProvider(answer()))

        _answer(_turns(sessions))

        assert sessions.count("reset") == 0 and sessions.count("get_or_create") == 1
        assert "open_replay_gap" not in sessions.names()
