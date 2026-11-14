"""The session replay under a rotation compaction method (``context.build_session_replay``).

A ``soft`` rotation restarts the session and its successor is re-seeded by this
replay. The rotation is judged on a projection that charges the replay budget
for the tail, so while a rotation method is configured (``rotation_configured``)
the replay clips an oversized newest row to that budget and leaves out the
notices a rotation posted. Without it the replay is what it always was; the
first tests pin that.
"""

from __future__ import annotations

import pytest

from kiro_crew import context as ctx
from kiro_crew.context_assembly.replay import _TRUNCATED_MARK, replay_budget_chars
from kiro_crew.session_compaction_methods import ROTATION_NOTICE_META_KEY


class _Log:
    def __init__(self, rows: list[dict]) -> None:
        self._rows = rows

    def read_messages_chained(self, key: str) -> list[dict]:
        return list(self._rows)


class _SessionSectionRaises:
    """A loaded config whose ``session`` section cannot be read, like a narrow test double."""

    def __init__(self, real: object) -> None:
        self._real = real

    def __getattr__(self, name: str) -> object:
        if name == "session":
            raise AttributeError(name)
        return getattr(self._real, name)


def _ts(second: int) -> str:
    return f"2026-09-15T12:00:{second:02d}+00:00"


def _row(role: str, content: str, second: int, **meta) -> dict:
    row = {"role": role, "content": content, "ts": _ts(second)}
    if meta:
        row["meta"] = meta
    return row


def _replay(rows: list[dict], **kw) -> str:
    out = ctx.build_session_replay(_Log(rows), "k", **kw)
    assert out is not None
    return out


class TestWithoutARotationNothingChanges:
    def test_conversation_and_inject_rows_render_as_before(self):
        rows = [
            _row("user", "hello", 1),
            _row("tool", "noise", 2),
            _row("assistant", "hi", 3),
            _row("inject", "[Cron notification] ran", 4),
        ]
        assert _replay(rows) == "User: hello\n\nAssistant: hi\n\nInject: [Cron notification] ran"

    def test_replay_rows_keep_the_two_key_shape(self):
        rows = [_row("user", "a", 1), _row("assistant", "b", 2)]
        assert ctx._replay_rows(_Log(rows), "k") == [
            {"role": "user", "content": "a"},
            {"role": "assistant", "content": "b"},
        ]


class TestTheNewestRowClip:
    def test_an_oversized_newest_row_replays_whole_unless_a_rotation_is_configured(self):
        # main's replay carries the newest row whole whatever its size; the
        # default path here is that replay. Only a caller with a rotation
        # method configured passes ``rotation_configured``: a rotation is judged
        # on a projection that charges the replay budget for the tail, so the
        # carried tail may not exceed it.
        budget = replay_budget_chars(200_000)
        huge = "x" * (budget * 2)
        rows = [_row("user", "older", 1), _row("assistant", huge, 2)]
        plain = _replay(rows, model_window=200_000)
        assert plain == "Assistant: " + huge
        clipped = _replay(rows, model_window=200_000, rotation_configured=True)
        # The replay clips to the budget with the "…" mark; its final translate
        # table then spells that ellipsis as three ASCII dots.
        room = budget - len(_TRUNCATED_MARK)
        assert clipped == ("Assistant: " + huge)[:room] + "...[truncated]"

    def test_a_newest_row_that_fits_is_never_clipped(self):
        line = "Assistant: fits"
        rows = [_row("assistant", "fits", 1)]
        assert _replay(rows, rotation_configured=True) == line


@pytest.mark.parametrize("role", ["tool", "system", "compacting", "done"])
def test_other_non_conversation_roles_stay_out_of_the_replay(role):
    rows = [_row("user", "a", 1), _row(role, "noise", 2), _row("assistant", "b", 3)]
    assert _replay(rows) == "User: a\n\nAssistant: b"


def _notice(second: int, *, method: str | None) -> dict:
    """A dashboard compaction notice; *method* set is how the dashboard marks a rotation's."""
    meta: dict = {"kind": "compaction"}
    if method is not None:
        meta[ROTATION_NOTICE_META_KEY] = method
    return {
        "role": "assistant",
        "content": "\U0001f504 Auto-compacted at 72%. Session rotated (soft). The conversation "
        "above is still here; the agent remembers only the recent turns, earlier ones are "
        "gone from its memory.",
        "ts": _ts(second),
        "meta": meta,
    }


class TestRotationNotices:
    """Every notice a rotation posts is status for the person, not conversation.

    Counted by the successor's replay it would be the newest row and spend the
    tail budget the rotation's projection charged, so while a rotation method is
    configured the replay leaves the marked rows out.
    """

    def test_a_soft_rotation_at_a_small_window_carries_the_last_reply(self):
        # A 200K window replays a 16K-char tail. Counted, the notice was the
        # newest row and a 17K-char last reply broke the walk after it: the
        # successor would have replayed the notice alone.
        rows = [_row("user", "earlier question", 1), _row("assistant", "y" * 17_000, 2)]
        rows.append(_notice(3, method="soft"))
        replay = _replay(rows, model_window=200_000, rotation_configured=True)
        assert replay.startswith("Assistant: yyy"), "the reply is carried, clipped to the budget"
        assert "Session rotated" not in replay

    def test_a_rotation_notice_is_replayed_as_before_without_a_rotation_configured(self):
        rows = [_row("user", "a", 1), _notice(2, method="soft"), _row("assistant", "b", 3)]
        assert "Session rotated" in _replay(rows)

    def test_a_native_notice_is_replayed_as_before(self):
        rows = [_row("user", "a", 1), _notice(2, method=None), _row("assistant", "b", 3)]
        assert "Auto-compacted at 72%" in _replay(rows, rotation_configured=True)

    def test_a_sent_row_carrying_the_key_alone_is_still_replayed(self):
        # A client sets the ``meta`` of the row it sends; only the full shape the
        # dashboard writes for a rotation's notice is left out of the replay.
        forged = _row("user", "key-only row", 1, **{ROTATION_NOTICE_META_KEY: "soft"})
        kinded = _row(
            "user", "kind-and-key row", 2, kind="compaction", **{ROTATION_NOTICE_META_KEY: "soft"}
        )
        replay = _replay([forged, kinded, _row("assistant", "b", 3)], rotation_configured=True)
        assert "key-only row" in replay
        assert "kind-and-key row" in replay


class TestTheRunnerPassesTheRotationOnlyWhenConfigured:
    """The one caller that knows the configuration decides the clip and the notice skip.

    Driven through the real ``_run_chat`` cold-start path (the provider has no
    history, so the replay is built) with ``KiroCrewConfig.load`` pinned: with
    the default ``native`` the replay is main's and carries the newest row
    whole; with a rotation method configured the replay is told so, since the
    rotation's projection charged the replay budget for the tail. A config
    whose method cannot be read keeps main's replay, and the turn still runs.
    """

    @pytest.mark.asyncio
    @pytest.mark.parametrize("path", ["builder", "legacy"])
    @pytest.mark.parametrize(
        ("method", "expected"),
        [("native", False), ("soft", True), ("unreadable", False)],
    )
    async def test_the_flag_follows_the_configured_method(
        self, tmp_path, monkeypatch, path: str, method: str, expected: bool
    ) -> None:
        from dataclasses import replace
        from unittest.mock import AsyncMock, Mock

        from test_chat_runner_coverage import _complete, _drive, _runner_state, _set_stream, _slot

        from kiro_crew.config.loader import KiroCrewConfig
        from kiro_crew.dashboard import chat_runner

        real = KiroCrewConfig.load()
        pinned: object
        if method == "unreadable":
            pinned = _SessionSectionRaises(real)
        else:
            pinned = replace(real, session=replace(real.session, compaction_method=method))
        monkeypatch.setattr(chat_runner.KiroCrewConfig, "load", staticmethod(lambda: pinned))

        # Both replay sites: the context-builder path (``build_session_replay``
        # called directly) and the legacy no-builder path
        # (``_build_history_prefix``, which forwards the flag).
        context_builder = None
        if path == "builder":
            from kiro_crew.context import ContextBuilder
            from kiro_crew.history import ConversationLog
            from kiro_crew.learn import LessonStore
            from kiro_crew.memory import MemoryStore
            from kiro_crew.skills import SkillsLoader

            context_builder = ContextBuilder(
                memory=MemoryStore(workspace=tmp_path / "memory"),
                skills=SkillsLoader(skills_path=tmp_path / "skills", install_builtins=False),
                lessons=LessonStore(base_dir=tmp_path / "lessons"),
                conversation_log=ConversationLog(base_dir=tmp_path / "history"),
            )
        state, client = _runner_state(tmp_path, context_builder=context_builder)
        client.mcp_session_report = Mock(return_value=None)
        client.client = Mock(pop_pending_oauth_requests=Mock(return_value=[]))
        state.sessions._sessions = {}
        # (client, is_new=True, resumed=False): a cold start with no provider
        # history, the one branch that builds the replay.
        state.sessions.get_or_create = AsyncMock(return_value=(client, True, False))
        slot = _slot()
        state._slots[slot.key] = slot
        slot.append("user", "earlier", meta={"mid": "older"})

        observed: list[bool | None] = []
        replay = ctx.build_session_replay

        def observe(*args, **kwargs):
            observed.append(kwargs.get("rotation_configured"))
            return replay(*args, **kwargs)

        monkeypatch.setattr(ctx, "build_session_replay", observe)
        _set_stream(client, [_complete()])
        await _drive(state, slot, "now")
        assert observed == [expected]

    @pytest.mark.asyncio
    @pytest.mark.parametrize(
        ("session", "expected"),
        [("tab", True), ("claude", False), ("cron_linked", False)],
    )
    async def test_a_session_that_never_rotates_replays_as_native(
        self, tmp_path, monkeypatch, session: str, expected: bool
    ) -> None:
        """``soft`` is configured, but the coordinator runs a Claude backend in
        place and hands a key the seed writer refuses (a cron-linked tab) to
        ``native``. Such a session never rotates, so its replay is main's; an
        ordinary dashboard tab keeps the flag."""
        from dataclasses import replace
        from unittest.mock import AsyncMock, Mock

        from test_chat_runner_coverage import _complete, _drive, _runner_state, _set_stream, _slot

        from kiro_crew import session as session_module
        from kiro_crew.config.loader import KiroCrewConfig
        from kiro_crew.dashboard import chat_runner

        real = KiroCrewConfig.load()
        pinned = replace(real, session=replace(real.session, compaction_method="soft"))
        monkeypatch.setattr(chat_runner.KiroCrewConfig, "load", staticmethod(lambda: pinned))
        state, client = _runner_state(tmp_path)
        client.mcp_session_report = Mock(return_value=None)
        client.client = Mock(pop_pending_oauth_requests=Mock(return_value=[]))
        state.sessions._sessions = {}
        state.sessions.get_or_create = AsyncMock(return_value=(client, True, False))
        if session == "claude":
            monkeypatch.setattr(
                session_module, "_is_claude_backend", lambda provider: provider is client
            )
        slot = _slot()
        if session == "cron_linked":
            slot.linked_session_key = "cron:job-7"
        state._slots[slot.key] = slot
        slot.append("user", "earlier", meta={"mid": "older"})

        observed: list[bool | None] = []
        replay = ctx.build_session_replay

        def observe(*args, **kwargs):
            observed.append(kwargs.get("rotation_configured"))
            return replay(*args, **kwargs)

        monkeypatch.setattr(ctx, "build_session_replay", observe)
        _set_stream(client, [_complete()])
        await _drive(state, slot, "now")
        assert observed == [expected]
