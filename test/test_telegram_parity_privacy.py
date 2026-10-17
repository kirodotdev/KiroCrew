"""``/temporary`` and ``/incognito`` on Telegram, and every write their promise covers.

The upload gate reads the persisted mode and a live dashboard slot; a restricted
session persists no transcript, saves no title and uploads nothing; a modifier
typed mid-turn survives the queue and a steer; the mode lands on the key the turn
runs under, survives a restart and gates memory reads; and every command whose
effect outlives the turn resolves through rotation, in exactly one place. The
doubles come from ``test_telegram`` and the autouse snapshot fixture from
``telegram_parity_helpers.py``.
"""

from __future__ import annotations

import time
from contextlib import nullcontext
from types import SimpleNamespace
from typing import Any
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from telegram_parity_helpers import (  # noqa: F401 -- the autouse fixture registers by import
    _drop_live_config_snapshot,
)
from test_telegram import _dispatcher, _dm, _origin, _prime_live


class TestUploadGate:
    @pytest.mark.asyncio
    async def test_a_channel_native_key_is_allowed(self) -> None:
        dispatcher, _, _ = _dispatcher({1})
        assert await dispatcher._uploads_restricted("telegram:kirocrew:direct:1") is False

    @pytest.mark.asyncio
    async def test_a_persisted_mode_survives_an_empty_tracker(self, tmp_path) -> None:
        # The restart case. The privacy trackers are process-local and only an
        # INBOUND channel message populates them, so a turn no inbound message
        # drove — a cron, a webhook resume, a monitor/auto-nudge re-injection, an
        # explicit file_send — reaches this gate with empty trackers even though
        # the user's !incognito is on disk. Without the durable restore the gate
        # reads "unrestricted" and the bytes leave the session that forbade them.
        from kiro_crew.messaging import privacy_mode
        from kiro_crew.messaging.upload_gate import uploads_restricted
        from kiro_crew.session_map import SessionMap

        key = "telegram:kirocrew:direct:1"
        with patch("kiro_crew.session_map.config_dir", return_value=tmp_path):
            sm = SessionMap()
        # ``set_flag`` -> ``_save`` schedules a debounced ``_flush_async`` task on
        # THIS test's loop, so the retirement has to be in a ``finally``: loop
        # teardown would otherwise destroy it mid-write ("Task was destroyed but
        # it is pending"), leaking a failure into whichever test runs next.
        try:
            sm.set_flag(key, privacy_mode.MODE_INCOGNITO, True)
            privacy_mode.reset()  # the empty process-local view a restart leaves
            state = SimpleNamespace(sessions=SimpleNamespace(_session_map=sm))

            assert (
                await uploads_restricted(
                    state,
                    key,
                    channel_type="telegram",
                    persisted_probe=lambda _slot: (False, None),
                )
                is True
            )
        finally:
            await sm.aclose()

    @pytest.mark.asyncio
    async def test_an_unflagged_channel_key_stays_allowed_after_the_restore(self, tmp_path) -> None:
        # The restore must not turn the common case into a refusal: a conversation
        # with no durable flag is still permitted.
        from kiro_crew.messaging import privacy_mode
        from kiro_crew.messaging.upload_gate import uploads_restricted
        from kiro_crew.session_map import SessionMap

        privacy_mode.reset()
        with patch("kiro_crew.session_map.config_dir", return_value=tmp_path):
            state = SimpleNamespace(sessions=SimpleNamespace(_session_map=SessionMap()))

        assert (
            await uploads_restricted(
                state,
                "telegram:kirocrew:direct:1",
                channel_type="telegram",
                persisted_probe=lambda _slot: (False, None),
            )
            is False
        )

    @pytest.mark.parametrize("restricted", [True, False])
    @pytest.mark.asyncio
    async def test_a_live_dashboard_slot_decides(self, restricted: bool) -> None:
        dispatcher, _, _ = _dispatcher({1})
        dispatcher.dashboard_state = SimpleNamespace(
            get_slot=lambda _name: SimpleNamespace(is_restricted=restricted)
        )
        assert await dispatcher._uploads_restricted("dashboard:abc") is restricted

    @pytest.mark.asyncio
    async def test_no_live_slot_falls_through_to_the_persisted_mode(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        from kiro_crew.messaging import upload_gate as ug

        dispatcher, _, _ = _dispatcher({1})
        dispatcher.dashboard_state = SimpleNamespace(get_slot=lambda _name: None)
        # The persisted probe is a PARAMETER rather than an import, so `messaging`
        # keeps its one-way dependency; the third argument is the unknown-mode
        # posture, which the upload ceiling leaves fail-closed.
        monkeypatch.setattr(
            ug, "_persisted_mode_is_restricted", lambda key, probe, unknown_denies=True: True
        )
        assert await dispatcher._uploads_restricted("dashboard:ghost") is True

    @pytest.mark.asyncio
    async def test_the_probe_is_injected_not_imported(self) -> None:
        # The gate must consult the probe its CALLER supplied. A gate that reached
        # for `dashboard` itself would answer without ever calling this one, and
        # would reintroduce the import `messaging` may not have.
        from kiro_crew.messaging.upload_gate import uploads_restricted

        calls: list[str] = []

        def probe(slot: str) -> tuple[bool, str | None]:
            calls.append(slot)
            return True, "incognito"

        restricted = await uploads_restricted(
            SimpleNamespace(get_slot=lambda _name: None),
            "dashboard:ghost",
            channel_type="telegram",
            persisted_probe=probe,
        )
        assert restricted is True
        assert calls == ["ghost"], "the injected probe was not the one consulted"

    @pytest.mark.asyncio
    async def test_a_probe_that_raises_denies(self) -> None:
        # Fail closed: a caller cannot open the gate by handing in something broken.
        from kiro_crew.messaging.upload_gate import uploads_restricted

        def boom(_slot: str) -> tuple[bool, str | None]:
            raise RuntimeError("transcript unreadable")

        assert (
            await uploads_restricted(
                SimpleNamespace(get_slot=lambda _name: None),
                "dashboard:ghost",
                channel_type="telegram",
                persisted_probe=boom,
            )
            is True
        )

    @pytest.mark.asyncio
    async def test_the_denial_is_audited_so_the_ceiling_is_observable(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        from kiro_crew.messaging import upload_gate as ug

        seen: list[dict] = []
        monkeypatch.setattr(
            ug, "sel", lambda: SimpleNamespace(log_api_access=lambda **kw: seen.append(kw))
        )
        dispatcher, _, _ = _dispatcher({1})
        dispatcher.dashboard_state = SimpleNamespace(
            get_slot=lambda _name: SimpleNamespace(is_restricted=True)
        )
        assert await dispatcher._uploads_restricted("dashboard:abc") is True
        assert seen and seen[0]["source"] == "telegram"
        assert seen[0]["error"] == "restricted_session"


class TestPrivacyModes:
    """`/temporary` and `/incognito` reach Telegram, on the shared mechanism.

    Slack spells them as inline ``!temporary`` / ``!incognito`` tokens because it
    has no command grammar to put them in. Telegram does, so they are ordinary
    commands here and the shared module's inline-token stripper is not used — the
    modes, the guarantees, the audit and the notice text are all the same.
    """

    def setup_method(self) -> None:
        from kiro_crew.messaging import privacy_mode

        privacy_mode.reset()

    @pytest.mark.asyncio
    async def test_a_bare_temporary_command_marks_and_answers_nothing(self) -> None:
        from kiro_crew.messaging import privacy_mode

        d, client, sessions = _dispatcher({7})
        await d.handle_message(_dm("/temporary"))
        key = d._session_key(("direct", "7"))
        assert privacy_mode.is_temporary(key)
        assert privacy_mode.is_restricted(key)
        assert any("Temporary" in t for t, _ in client.sent)
        # The fake echoes "Answer: <prompt prefix>", so an answered turn is visible
        # in the outbound. A bare modifier must produce the notice and nothing else.
        assert not any(
            t.startswith("Answer:") for t, _ in client.sent
        ), "a bare modifier must not spend a turn"

    @pytest.mark.asyncio
    async def test_incognito_with_a_question_marks_and_still_answers(self) -> None:
        # Slack's `!incognito summarise this` both marks the thread and answers, so
        # dropping the question here would be a silent behaviour difference.
        from kiro_crew.messaging import privacy_mode

        d, client, _ = _dispatcher({7})
        await d.handle_message(_dm("/incognito summarise this"))
        key = d._session_key(("direct", "7"))
        assert privacy_mode.is_incognito(key)
        # The fake echoes "Answer: " + the prompt's first characters, so the reply
        # is the observable for what reached the model.
        answers = [t for t, _ in client.sent if t.startswith("Answer:")]
        assert answers, "the question after the modifier must still run"
        assert "summarise" in answers[-1]
        assert (
            "/incognito" not in answers[-1]
        ), "the modifier is an instruction to the gateway, not to the model"

    @pytest.mark.asyncio
    async def test_repeating_the_command_still_confirms(self) -> None:
        # apply_mode is idempotent and deliberately says nothing the second time.
        # A command that answers with silence reads as having failed.
        d, client, _ = _dispatcher({7})
        await d.handle_message(_dm("/temporary"))
        before = len(client.sent)
        await d.handle_message(_dm("/temporary"))
        assert len(client.sent) > before
        assert "Temporary" in client.sent[-1][0]

    def test_a_restricted_session_persists_no_transcript(self) -> None:
        from kiro_crew.messaging import privacy_mode

        logged: list = []
        d, _, _ = _dispatcher({7})
        d.conv_log = SimpleNamespace(  # type: ignore[assignment]
            append=lambda *a, **k: logged.append(a),
            set_title=lambda *a, **k: None,
            atomic_appends=lambda _key: nullcontext(),
        )
        key = "telegram:kirocrew:direct:7"
        d._persist_turn(key, "hello", "hi there", True, "kirocrew")
        assert logged, "an unrestricted session must still persist"
        logged.clear()
        privacy_mode.mark_incognito(key)
        d._persist_turn(key, "hello", "hi there", True, "kirocrew")
        assert not logged, "an incognito session must write no transcript"


class TestARestrictedSessionWritesNothingAtAll:
    """A privacy mode's promise covers EVERY write, not just the transcript.

    ``/title`` reaches ``ConversationLog.set_title`` -> ``update_metadata``, which
    CREATES the session file. So on a `/temporary` or `/incognito` conversation that
    one command persisted user-authored content for a mode that had just promised not
    to. ``_persist_turn`` gates the same way and so does Slack's own ``/title``; this
    is the third write on one promise, not a new rule.
    """

    def setup_method(self) -> None:
        from kiro_crew.messaging import privacy_mode

        privacy_mode.reset()

    @pytest.mark.asyncio
    @pytest.mark.parametrize("modifier", ["/temporary", "/incognito"])
    async def test_title_writes_nothing_for_a_restricted_session(self, modifier: str) -> None:
        titled: list = []
        d, client, _ = _dispatcher({7})
        d.conv_log = SimpleNamespace(  # type: ignore[assignment]
            append=lambda *a, **k: None, set_title=lambda *a, **k: titled.append(a)
        )

        await d.handle_message(_dm(modifier))
        await d.handle_message(_dm("/title Secret project"))

        assert not titled, "a restricted session must not persist a title"
        # And the user is TOLD, because a rename that silently does nothing reads as
        # the command having failed.
        assert "private" in client.sent[-1][0]

    @pytest.mark.asyncio
    async def test_an_ordinary_session_still_renames(self) -> None:
        """Non-vacuity: the gate refuses the MODE, not the command."""
        titled: list = []
        d, client, _ = _dispatcher({7})
        d.conv_log = SimpleNamespace(  # type: ignore[assignment]
            append=lambda *a, **k: None, set_title=lambda *a, **k: titled.append(a)
        )

        await d.handle_message(_dm("/title Ordinary project"))

        assert titled and titled[-1][1] == "Ordinary project"
        assert "Renamed" in client.sent[-1][0]

    @staticmethod
    def _title_writes_by_channel() -> dict[str, list[str]]:
        """``channel -> [functions that call set_title]``, for dispatchers that offer
        the privacy modes at all.

        Scoped to those channels ON PURPOSE. The modifiers are a Slack and Telegram
        feature: eight other dispatchers never touch ``privacy_mode``, so no session
        of theirs can BE restricted and a gate there would guard nothing while
        reading as eight open defects. The scope is derived from whether the module
        reaches ``privacy_mode``, not from a channel list, so a channel that adopts
        the modes is covered the moment it does.
        """
        import ast
        from pathlib import Path

        import kiro_crew as kiro_crew_pkg

        root = Path(kiro_crew_pkg.__file__).parent
        found: dict[str, list[str]] = {}
        for path in sorted(root.glob("*/transport_dispatch.py")):
            source = "\n".join(
                module.read_text(encoding="utf-8")
                for module in TestARestrictedSessionWritesNothingAtAll._dispatch_sources(path)
            )
            if "privacy_mode" not in source:
                continue
            for module in TestARestrictedSessionWritesNothingAtAll._dispatch_sources(path):
                tree = ast.parse(module.read_text(encoding="utf-8"))
                for node in ast.walk(tree):
                    if not isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
                        continue
                    if "'set_title'" in ast.dump(node):
                        found.setdefault(path.parent.name, []).append(node.name)
        return found

    @staticmethod
    def _dispatch_sources(facade: Any) -> list[Any]:
        """A channel's dispatcher files: its facade and the owners composed into it.

        A title write in an owner module is one the dispatcher makes, so it is read
        with the facade rather than left outside the scan.
        """
        return [
            facade,
            *sorted((facade.parent / "dispatch").glob("*.py")),
            *sorted(facade.parent.glob("transport_dispatch_*.py")),
        ]

    def test_the_scan_finds_the_writes_it_is_meant_to_guard(self) -> None:
        """A scan that matched nothing would make the next test vacuous."""
        found = self._title_writes_by_channel()
        assert "telegram" in found, f"saw {found}"
        # Both of Telegram's: the turn's own title and the interactive rename.
        assert set(found["telegram"]) >= {"_persist_turn", "_handle_title"}, found["telegram"]

    def test_every_title_write_on_a_privacy_channel_is_gated(self) -> None:
        """The class, by enumeration: a title write must sit behind the predicate.

        ``set_title`` creates the transcript, so any dispatcher that offers the modes
        and gains a rename gains this hazard. Checked as SOURCE, because the point is
        that a NEW call site cannot appear ungated.
        """
        import ast
        from pathlib import Path

        import kiro_crew as kiro_crew_pkg

        root = Path(kiro_crew_pkg.__file__).parent
        ungated: list[str] = []
        for channel, functions in sorted(self._title_writes_by_channel().items()):
            for module in self._dispatch_sources(root / channel / "transport_dispatch.py"):
                tree = ast.parse(module.read_text("utf-8"))
                for node in ast.walk(tree):
                    if not isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
                        continue
                    if node.name not in functions:
                        continue
                    # Both shapes count: an early return (``_persist_turn``) and a
                    # guarded branch that answers the user (``_handle_title``). The
                    # dashboard-aware helper is named ``_session_restricted`` rather
                    # than ``privacy_mode.is_restricted``; both are explicit gates.
                    dump = ast.dump(node)
                    if "is_restricted" not in dump and "session_restricted" not in dump:
                        ungated.append(f"{channel}.{node.name}")
        assert not ungated, (
            "these write a durable title without checking the privacy mode, so a "
            f"restricted conversation would persist one: {ungated}"
        )


class TestARestrictedSessionUploadsNothing:
    """The fourth write on the same promise, and the one with the widest audience.

    A restricted conversation refuses to write a transcript, read memory or save a
    title — and still shipped local file bytes into a DM or a supergroup-readable
    Topic. The upload gate answered only the dashboard-slot question and returned a
    flat False for every channel-native key, which was correct exactly while no
    channel-native conversation had a privacy mode.
    """

    def setup_method(self) -> None:
        from kiro_crew.messaging import privacy_mode

        privacy_mode.reset()

    @staticmethod
    async def _restricted(key: str) -> bool:
        from kiro_crew.messaging.upload_gate import uploads_restricted

        return await uploads_restricted(
            None,
            key,
            channel_type="telegram",
            persisted_probe=lambda _slot: (False, None),
        )

    @pytest.mark.asyncio
    @pytest.mark.parametrize("mark", ["mark_temporary", "mark_incognito"])
    async def test_a_marked_channel_session_may_not_upload(self, mark: str) -> None:
        from kiro_crew.messaging import privacy_mode

        key = "telegram:kirocrew:direct:7"
        getattr(privacy_mode, mark)(key)
        assert await self._restricted(key) is True

    @pytest.mark.asyncio
    async def test_an_unmarked_channel_session_still_may(self) -> None:
        # The common case, and the reason this rung is not a blanket fail-closed:
        # denying here would disable uploads for every ordinary conversation.
        assert await self._restricted("telegram:kirocrew:direct:7") is False

    @pytest.mark.asyncio
    async def test_a_forum_topic_key_is_covered_too(self) -> None:
        # The audience that makes this worst: a Topic is readable by the whole
        # supergroup, and its key is shaped differently from a DM's.
        from kiro_crew.messaging import privacy_mode

        key = "telegram:kirocrew:forum:-100999:4"
        privacy_mode.mark_temporary(key)
        assert await self._restricted(key) is True

    @pytest.mark.asyncio
    async def test_a_channel_without_privacy_modes_is_unaffected(self) -> None:
        """Discord has no `/temporary`, so nothing can mark its keys.

        This rung must not answer False unconditionally: the change
        must be invisible to a channel that offers no modes, or it would read as a
        behaviour change to every other channel's uploads.
        """
        from kiro_crew.messaging.upload_gate import uploads_restricted

        assert (
            await uploads_restricted(
                None,
                "discord:kirocrew:direct:42",
                channel_type="discord",
                persisted_probe=lambda _slot: (False, None),
            )
            is False
        )

    @pytest.mark.asyncio
    async def test_the_denial_is_audited(self) -> None:
        # The ceiling has to be observable, like every other transport denial.
        from kiro_crew.messaging import privacy_mode

        key = "telegram:kirocrew:direct:7"
        privacy_mode.mark_incognito(key)
        with patch("kiro_crew.messaging.upload_gate.sel") as mock_sel:
            assert await self._restricted(key) is True
        kw = mock_sel.return_value.log_api_access.call_args.kwargs
        assert kw["outcome"] == "denied" and kw["error"] == "restricted_session"

    @pytest.mark.asyncio
    async def test_the_dispatcher_hands_the_renderer_the_gate_s_answer(self) -> None:
        """End to end: the turn must build a renderer with uploads OFF.

        The gate answering correctly buys nothing if the dispatcher does not consult
        it, so this asserts the value that actually reaches the renderer.
        """
        from kiro_crew.messaging import privacy_mode

        d, _, _ = _dispatcher({7})
        route = ("direct", "7")
        key = d._rotated_session_key(route)
        privacy_mode.mark_temporary(key)
        assert await d._uploads_restricted(key) is True


class TestAMidTurnModifierSurvivesTheQueue:
    """A modifier typed while a turn is running must still protect its own turn.

    The command ladder strips the modifier off the text and DEFERS applying it until
    after rotation, so the mark lands on the key the turn really runs under. The
    busy path returns before that, and the drain re-enters with
    ``interpret_commands=False`` on the already-stripped text -- so the request has
    to travel WITH the message rather than being re-derived later.
    """

    def setup_method(self) -> None:
        from kiro_crew.messaging import privacy_mode

        privacy_mode.reset()

    @staticmethod
    def _busy(dispatcher) -> None:
        """Make the session read busy without a real turn in flight."""
        dispatcher.sessions._busy = True

    @pytest.mark.asyncio
    async def test_a_queued_modifier_rides_along_and_applies_on_drain(self) -> None:
        d, client, sessions = _dispatcher({7})
        d.cfg.messaging.queue_mode = "queue"
        _prime_live(d.cfg)
        self._busy(d)

        await d.handle_message(_dm("/temporary summarise this"))

        # It queued rather than answering, and the QUEUED ENTRY carries the request:
        # this is the hand-off the drain depends on.
        queued = sessions.queued
        assert queued, "a mid-turn message must queue"
        assert queued[-1][2].get("privacy_request") == "temporary"
        assert "summarise this" in queued[-1][1]
        assert "/temporary" not in queued[-1][1], "the modifier is stripped from the text"

    @pytest.mark.asyncio
    async def test_the_drained_turn_is_restricted_before_it_persists(self) -> None:
        from kiro_crew.messaging import privacy_mode

        d, client, sessions = _dispatcher({7})
        key = d._session_key(("direct", "7"))
        logged: list = []
        d.conv_log = SimpleNamespace(  # type: ignore[assignment]
            append=lambda *a, **k: logged.append(a),
            set_title=lambda *a, **k: None,
            atomic_appends=lambda _key: nullcontext(),
        )
        sessions.enqueue(
            key, "1", "summarise this", force=True, privacy_request="temporary", **_origin()
        )

        await d._drain_queue(key)

        # The whole point: the drained turn ran AND its session is restricted, so
        # `_persist_turn` writes nothing for it.
        assert any(t.startswith("Answer:") for t, _ in client.sent), "the drained turn must run"
        assert privacy_mode.is_temporary(key)
        logged.clear()
        d._persist_turn(key, "summarise this", "done", True, "kirocrew")
        assert not logged

    @pytest.mark.asyncio
    async def test_the_strictest_of_a_collapsed_burst_wins(self) -> None:
        from kiro_crew.messaging import privacy_mode

        d, _, sessions = _dispatcher({7})
        key = d._session_key(("direct", "7"))
        # Incognito FIRST, so honouring the first request would leave the stricter
        # `/temporary` behind it silently downgraded. ONE sender, so they collapse.
        sessions.enqueue(key, "1", "one", force=True, privacy_request="incognito", **_origin())
        sessions.enqueue(key, "2", "two", force=True, privacy_request="temporary", **_origin())

        await d._drain_queue(key)

        assert privacy_mode.is_temporary(key), "the collapsed turn takes the strictest mode"

    @pytest.mark.asyncio
    async def test_a_message_deferred_past_the_collapse_cap_keeps_its_protection(self) -> None:
        from kiro_crew.messaging.queue_receipt import MAX_COLLAPSE

        d, _, sessions = _dispatcher({7})
        key = d._session_key(("direct", "7"))
        # Fill the collapse budget with unprotected messages, then one protected
        # message BEHIND the cap. The drain re-enqueues the surplus for a later
        # iteration, and that copy is the only carrier its request has left.
        for i in range(MAX_COLLAPSE):
            sessions.enqueue(key, str(i), f"msg{i}", force=True, **_origin())
        sessions.enqueue(
            key, "last", "the private one", force=True, privacy_request="temporary", **_origin()
        )
        d.handle_message = AsyncMock()  # type: ignore[method-assign]
        # Observed as the re-enqueue CALL: the pump loops, so the copy left in the
        # queue after the first iteration is consumed by the second one.
        requeued: list[tuple[str, dict]] = []
        real_enqueue = sessions.enqueue

        def _spy(k: str, ts: str, text: str, **kw: Any) -> bool:
            requeued.append((text, kw))
            return real_enqueue(k, ts, text, **kw)

        sessions.enqueue = _spy  # type: ignore[method-assign]

        await d._drain_queue(key)

        deferred = [kw for text, kw in requeued if text == "the private one"]
        assert deferred, "the surplus message must be re-enqueued, not dropped"
        assert deferred[-1].get("privacy_request") == "temporary"

    @pytest.mark.asyncio
    async def test_a_steered_modifier_marks_the_running_turn(self) -> None:
        from kiro_crew.messaging import privacy_mode

        d, _, sessions = _dispatcher({7})
        d.cfg.messaging.queue_mode = "steer"
        _prime_live(d.cfg)
        key = d._session_key(("direct", "7"))
        self._busy(d)
        steered: list[str] = []
        sessions._gp = SimpleNamespace(
            supports_steer=True,
            has_active_turn=lambda: True,
            steer=AsyncMock(side_effect=lambda t: steered.append(t) or True),
        )

        await d.handle_message(_dm("/incognito and also this"))

        # A steer folds into the turn ALREADY running on this key, which writes its
        # transcript when it finishes -- so the mark belongs on that key now, not on
        # whatever a later drain would resolve.
        assert steered, "the message must have steered"
        assert privacy_mode.is_incognito(key)

    @pytest.mark.asyncio
    async def test_a_refused_steer_does_not_leave_the_session_marked(self) -> None:
        from kiro_crew.messaging import privacy_mode

        d, _, sessions = _dispatcher({7})
        d.cfg.messaging.queue_mode = "steer"
        _prime_live(d.cfg)
        key = d._session_key(("direct", "7"))
        self._busy(d)
        sessions._gp = SimpleNamespace(
            supports_steer=True,
            has_active_turn=lambda: True,
            steer=AsyncMock(return_value=False),
        )

        await d.handle_message(_dm("/incognito and also this"))

        # The steer was refused, so the message fell through to the queue path and
        # its protection travels with it there. Marking this key anyway would restrict
        # a turn the user never asked to protect.
        assert not privacy_mode.is_incognito(key)
        queued = sessions.queued
        assert queued and queued[-1][2].get("privacy_request") == "incognito"

    @staticmethod
    def _real_map(sessions, tmp_path, monkeypatch):
        """Give the sessions double a real ``SessionMap`` so the row is a real record."""
        from kiro_crew.session_map import SessionMap

        monkeypatch.setattr("kiro_crew.session_map.config_dir", lambda: tmp_path)
        monkeypatch.setattr("kiro_crew.session_map._KIRO_SESSIONS_DIR", tmp_path / "kiro")
        sessions._session_map = SessionMap()
        return sessions._session_map

    @staticmethod
    def _audits(monkeypatch) -> list[dict]:
        from unittest.mock import MagicMock

        from kiro_crew.messaging import privacy_mode

        events: list[dict] = []
        fake = MagicMock()
        fake.log_api_access = lambda **kw: events.append(kw)
        monkeypatch.setattr(privacy_mode, "sel", lambda: fake)
        return events

    @pytest.mark.asyncio
    async def test_no_steer_without_a_persisted_row(self, tmp_path, monkeypatch) -> None:
        """The row is reserved BEFORE the steer, strictly: a map that cannot write it
        refuses the message -- the steer never runs, the user is told, one denial.
        Mutation: check the row read-only, steer, then apply best-effort -- the steer
        runs and the mark holds with no row behind it."""
        from kiro_crew.messaging import privacy_mode

        d, client, sessions = _dispatcher({7})
        d.cfg.messaging.queue_mode = "steer"
        _prime_live(d.cfg)
        key = d._session_key(("direct", "7"))
        self._busy(d)
        sm = self._real_map(sessions, tmp_path, monkeypatch)
        monkeypatch.setattr(sm, "set_flag", MagicMock(side_effect=OSError("disk full")))
        events = self._audits(monkeypatch)
        steered: list[bool] = []

        async def _steer(text: str) -> bool:
            steered.append(sm.get_flag(key, "incognito"))  # was the row there when we steered?
            return True

        sessions._gp = SimpleNamespace(
            supports_steer=True, has_active_turn=lambda: True, steer=_steer
        )
        privacy_mode.reset()
        try:
            await d.handle_message(_dm("/incognito and also this"))
        finally:
            privacy_mode.reset()
        assert steered == [], f"the message steered without a persisted row: {steered}"
        assert not privacy_mode.is_incognito(key)
        assert sessions.queued == [], "a refused message must not be queued either"
        landed = "".join(text for text, _ in client.sent)
        assert "NOT processed" in landed and "Incognito mode ON" not in landed
        assert [e["outcome"] for e in events] == ["denied"]
        assert events[0]["resources"] == f"private_session_refused:persist_failed:{key}"

    @pytest.mark.asyncio
    async def test_no_steer_when_the_row_cannot_be_made_durable(
        self, tmp_path, monkeypatch
    ) -> None:
        """The row reached the map's memory but its write failed: the reservation is
        durable-or-refused, so the steer never runs, nothing is marked or published,
        the user is told plainly, one denial -- and nothing else in the trail, since
        the mark (and its ``allowed`` record) now follows the write instead of
        preceding it. Mutation: return before the map's flush -- the steer runs on
        a row that only exists in memory (red: the steer was called and the mark
        holds)."""
        from kiro_crew.messaging import privacy_mode

        d, client, sessions = _dispatcher({7})
        d.cfg.messaging.queue_mode = "steer"
        _prime_live(d.cfg)
        key = d._session_key(("direct", "7"))
        self._busy(d)
        sm = self._real_map(sessions, tmp_path, monkeypatch)
        monkeypatch.setattr(sm, "aflush", AsyncMock(side_effect=OSError("disk full")))
        events = self._audits(monkeypatch)
        steer = AsyncMock(return_value=True)
        sessions._gp = SimpleNamespace(
            supports_steer=True, has_active_turn=lambda: True, steer=steer
        )
        privacy_mode.reset()
        try:
            await d.handle_message(_dm("/incognito and also this"))
        finally:
            privacy_mode.reset()
        steer.assert_not_awaited()
        assert not privacy_mode.is_incognito(key)
        assert sm.get_flag(key, "incognito") is False
        assert sessions.queued == []
        landed = "".join(text for text, _ in client.sent)
        assert "could not be written" in landed and "NOT processed" in landed
        assert [e["outcome"] for e in events] == ["denied"]
        assert events[-1]["resources"] == f"private_session_refused:persist_failed:{key}"

    @pytest.mark.asyncio
    async def test_a_declined_steer_whose_release_cannot_reach_disk_retains_the_mode(
        self, tmp_path, monkeypatch
    ) -> None:
        """The reservation landed durably, the provider declined the steer, and the
        release's clear cannot be written: the mode is RETAINED on this key -- mark
        on, flag on, header stamped -- with a ``retained`` record, and the message
        still takes the queue path with its request. Mutation: drop the mark before
        the clear is durable -- the key reads persistent with its row still on disk."""
        from kiro_crew.messaging import privacy_mode

        d, client, sessions = _dispatcher({7})
        d.cfg.messaging.queue_mode = "steer"
        _prime_live(d.cfg)
        key = d._session_key(("direct", "7"))
        self._busy(d)
        sm = self._real_map(sessions, tmp_path, monkeypatch)
        events = self._audits(monkeypatch)
        real_aflush = sm.aflush
        flushes: list[str] = []

        async def _aflush_fails_after_the_reservation() -> None:
            flushes.append("flush")
            if len(flushes) > 1:  # the first flush lands the reservation; the release's fails
                raise OSError("disk full")
            await real_aflush()

        monkeypatch.setattr(sm, "aflush", _aflush_fails_after_the_reservation)
        sessions._gp = SimpleNamespace(
            supports_steer=True, has_active_turn=lambda: True, steer=AsyncMock(return_value=False)
        )
        privacy_mode.reset()
        try:
            await d.handle_message(_dm("/incognito and also this"))
            assert privacy_mode.is_incognito(key), "the mark was dropped with the clear still owed"
            assert sm.get_flag(key, "incognito") is True
            assert [e["outcome"] for e in events] == ["allowed", "retained"]
            queued = sessions.queued
            assert queued and queued[-1][2].get("privacy_request") == "incognito"
        finally:
            privacy_mode.reset()

    @pytest.mark.asyncio
    async def test_a_full_map_refuses_before_the_steer(self, tmp_path, monkeypatch) -> None:
        import kiro_crew.session_map as session_map_mod
        from kiro_crew.messaging import privacy_mode

        d, client, sessions = _dispatcher({7})
        d.cfg.messaging.queue_mode = "steer"
        _prime_live(d.cfg)
        key = d._session_key(("direct", "7"))
        self._busy(d)
        sm = self._real_map(sessions, tmp_path, monkeypatch)
        monkeypatch.setattr(session_map_mod, "PRIVACY_ROW_CAP", 1)
        sm.set_flag("telegram:kirocrew:direct:other", "incognito", True)
        events = self._audits(monkeypatch)
        steer = AsyncMock(return_value=True)
        sessions._gp = SimpleNamespace(
            supports_steer=True, has_active_turn=lambda: True, steer=steer
        )
        privacy_mode.reset()
        try:
            await d.handle_message(_dm("/incognito and also this"))
        finally:
            privacy_mode.reset()
        steer.assert_not_awaited()
        assert sm.get_flag(key, "incognito") is False
        assert [e["outcome"] for e in events] == ["denied"]
        assert "NOT processed" in "".join(text for text, _ in client.sent)

    @pytest.mark.asyncio
    async def test_a_declined_steer_releases_the_reservation(self, tmp_path, monkeypatch) -> None:
        """The row, the mark and the header exist AT the steer; when the provider
        declines it, all three are taken back (the cap count goes back down) and the
        message takes the queue path with its request. Mutation: reserve after the
        steer -- the row is absent when the steer runs (red: ``steered == [False]``);
        never release -- the row stays (red: ``privacy_flagged_entries``)."""
        from kiro_crew import history as history_mod
        from kiro_crew.history import ConversationLog
        from kiro_crew.messaging import privacy_mode

        d, client, sessions = _dispatcher({7})
        d.cfg.messaging.queue_mode = "steer"
        _prime_live(d.cfg)
        key = d._session_key(("direct", "7"))
        self._busy(d)
        sm = self._real_map(sessions, tmp_path, monkeypatch)
        events = self._audits(monkeypatch)
        log = ConversationLog()
        log.init()
        with history_mod.allow_on_loop_persist():
            log.append(key, "user", "a persistent-era turn")
        steered: list[bool] = []

        async def _steer(text: str) -> bool:
            steered.append(sm.get_flag(key, "incognito"))
            return False  # the provider declines the steer

        sessions._gp = SimpleNamespace(
            supports_steer=True, has_active_turn=lambda: True, steer=_steer
        )
        privacy_mode.reset()
        try:
            await d.handle_message(_dm("/incognito and also this"))
            assert steered == [True], "the row must exist when the steer runs"
            # Released: nothing of the reservation remains, the count is back down.
            assert not privacy_mode.is_incognito(key)
            assert sm.get_flag(key, "incognito") is False
            assert sm.privacy_flagged_entries() == {}
            assert log.get_metadata(key).get("memory_mode") in (None, "persistent")
            assert [e["outcome"] for e in events] == ["allowed", "released"]
            queued = sessions.queued
            assert queued and queued[-1][2].get("privacy_request") == "incognito"
        finally:
            privacy_mode.reset()

    @pytest.mark.asyncio
    async def test_a_steer_that_raises_retains_the_reservation(self, tmp_path, monkeypatch) -> None:
        """A steer that RAISES is ambiguous: its bytes reach the backend before
        the awaited flush that fails, so the message may be in the turn and on
        its way into a transcript. Fail-closed: the mode stands -- row, mark and
        header exactly as a landed steer leaves them, one ``allowed`` record --
        and only an explicit decline (the steer returning False) releases.
        Mutation: release on the raise (the r36 shape) -- red: the row is
        cleared while the backend may be recording the message."""
        from kiro_crew.messaging import privacy_mode

        d, client, sessions = _dispatcher({7})
        d.cfg.messaging.queue_mode = "steer"
        _prime_live(d.cfg)
        key = d._session_key(("direct", "7"))
        self._busy(d)
        sm = self._real_map(sessions, tmp_path, monkeypatch)
        events = self._audits(monkeypatch)
        sessions._gp = SimpleNamespace(
            supports_steer=True,
            has_active_turn=lambda: True,
            steer=AsyncMock(side_effect=RuntimeError("transport dropped")),
        )
        privacy_mode.reset()
        try:
            with pytest.raises(RuntimeError):
                await d.handle_message(_dm("/incognito and also this"))
            assert privacy_mode.is_incognito(
                key
            ), "a raised steer released the mode over a message the backend may be recording"
            assert sm.get_flag(key, "incognito") is True
            assert [e["outcome"] for e in events] == ["allowed"]
        finally:
            privacy_mode.reset()


class TestPrivacyModeEnforcement:
    """The three halves of a privacy mode that marking alone does not deliver.

    Marking a session records the intent. Enforcing it means the turn must run
    under the key that was marked, must not READ memory in temporary mode, and must
    still be restricted after a restart. Each is separately reachable, so each has
    its own test: a mode that reports success and then reads yesterday's memories
    into the prompt is worse than one that refused outright.
    """

    def setup_method(self) -> None:
        from kiro_crew.messaging import privacy_mode

        privacy_mode.reset()

    @pytest.mark.asyncio
    async def test_temporary_blocks_memory_reads_and_incognito_does_not(self) -> None:
        # This is the documented difference between the two modes, and the half the
        # transcript gate cannot cover: refusing to WRITE still leaves yesterday's
        # memories and lessons in today's prompt.
        from kiro_crew.messaging import privacy_mode

        d, _, _ = _dispatcher({7})
        await d.handle_message(_dm("/temporary summarise this"))
        assert d.ctx_builder.build_calls[-1]["blocks_reads"] is True

        privacy_mode.reset()
        # A second conversation: the first one's transcript header records
        # temporary, and the inbound restore reads it, so an incognito there
        # still blocks reads.
        d2, _, _ = _dispatcher({8})
        await d2.handle_message(_dm("/incognito summarise this", uid="8"))
        assert d2.ctx_builder.build_calls[-1]["blocks_reads"] is False

    @pytest.mark.asyncio
    async def test_an_unmarked_conversation_reads_normally(self) -> None:
        d, _, _ = _dispatcher({7})
        await d.handle_message(_dm("just a question"))
        assert d.ctx_builder.build_calls[-1]["blocks_reads"] is False

    @pytest.mark.asyncio
    async def test_the_mode_lands_on_the_key_the_turn_actually_runs_under(self) -> None:
        # The session key early in the command ladder is the PRE-rotation one, and
        # the idle/daily rotation can mint a different key for the very turn the
        # user is asking to protect. Marking the old key would leave the turn
        # unrestricted while reporting success.
        from kiro_crew.messaging import privacy_mode

        d, _, _ = _dispatcher({7})
        route = ("direct", "7")
        # Force a rotation between the command branch and the turn by advancing the
        # generation the way an idle reset does.
        original = d._session_key

        def _rotating(r: Any) -> str:
            # First call (the command ladder) sees generation 0; the turn sees 1.
            key = original(r)
            if not getattr(_rotating, "bumped", False):
                _rotating.bumped = True  # type: ignore[attr-defined]
                d._conv.bump_gen(route)
            return key

        d._session_key = _rotating  # type: ignore[assignment]
        await d.handle_message(_dm("/incognito summarise this"))
        d._session_key = original  # type: ignore[assignment]
        final_key = d._session_key(route)
        assert privacy_mode.is_incognito(final_key), (
            f"the mode must be on the key the turn ran under, not a stale one; "
            f"final={final_key}"
        )

    @pytest.mark.asyncio
    async def test_a_restart_restores_a_persisted_restriction(self) -> None:
        # The in-memory trackers are empty on a cold process, so without hydration a
        # session the operator marked incognito yesterday reads as unrestricted
        # today and this turn's transcript is written.
        from kiro_crew.messaging import privacy_mode

        d, _, _ = _dispatcher({7})
        key = d._session_key(("direct", "7"))
        hydrated: list[str] = []

        def _fake_hydrate(sessions: Any, session_key: str) -> None:
            hydrated.append(session_key)
            privacy_mode.mark_incognito(session_key)

        with pytest.MonkeyPatch.context() as mp:
            mp.setattr(privacy_mode, "hydrate", _fake_hydrate)
            await d.handle_message(_dm("a question after a restart"))
        assert key in hydrated, "the turn must consult the durable flag"
        assert privacy_mode.is_restricted(key)

    @pytest.mark.asyncio
    async def test_hydration_uses_the_post_rotation_key(self) -> None:
        # Hydrating the pre-rotation key would restore the restriction onto a key
        # nothing runs under, which reads as working and enforces nothing.
        from kiro_crew.messaging import privacy_mode

        d, _, _ = _dispatcher({7})
        seen: list[str] = []
        with pytest.MonkeyPatch.context() as mp:
            mp.setattr(privacy_mode, "hydrate", lambda s, k: seen.append(k))
            await d.handle_message(_dm("hello"))
        # Asserted as a SET: what matters is which key is restored and that the
        # pre-rotation one never is. The restore is idempotent and every gate that
        # reads the process-local trackers runs it, so pinning a call count here
        # would fail on a second gate joining the turn rather than on the key
        # being wrong.
        assert seen, "the inbound path must restore the durable flags"
        assert set(seen) == {d._session_key(("direct", "7"))}


class TestRotationChokepoint:
    """Rotation is settled in exactly ONE place, so no path keys on a stale one.

    The generation a route resolves to changes when the idle or daily window
    elapses. A key read BEFORE that is settled can be one the very next message
    abandons, so anything written against it protects a session that is already
    dead — and reports success while doing it. The bare privacy command was the
    second path to get this wrong, which is why the fix is a shared resolver rather
    than another corrected call site.
    """

    def setup_method(self) -> None:
        from kiro_crew.messaging import privacy_mode

        privacy_mode.reset()

    @pytest.mark.asyncio
    async def test_a_bare_modifier_resolves_through_the_rotation_helper(self) -> None:
        # Asserted on WHICH resolver the bare path uses, because the outcome cannot
        # tell them apart reliably: whether the two keys differ depends on when the
        # idle window last moved, so an outcome assertion here passes for the wrong
        # reason about as often as the right one. The invariant is that a key a side
        # effect attaches to comes from the rotating resolver, so that is the
        # assertion. The user-visible consequence is covered by the test below.
        d, _, _ = _dispatcher({7})
        used: list[str] = []
        original = d._rotated_session_key
        d._rotated_session_key = lambda r: used.append("rotated") or original(r)  # type: ignore[assignment]
        await d.handle_message(_dm("/temporary"))
        assert used == ["rotated"], "the bare modifier must key on the rotated session"

    @pytest.mark.asyncio
    async def test_the_next_message_runs_under_the_protected_key(self) -> None:
        # The end-to-end version: mark, then send, and the transcript must not be
        # written. This is the consequence the key mismatch would produce.
        from kiro_crew.messaging import privacy_mode

        d, _, _ = _dispatcher({7})
        d.cfg.messaging.idle_reset_minutes = 1
        _prime_live(d.cfg)
        route = ("direct", "7")
        d._conv.maybe_rotate(route, time.time() - 3600, idle_minutes=1, daily_reset_hour=-1)
        logged: list[Any] = []
        d.conv_log = SimpleNamespace(  # type: ignore[assignment]
            append=lambda *a, **k: logged.append(a),
            set_title=lambda *a, **k: None,
            atomic_appends=lambda _key: nullcontext(),
        )
        await d.handle_message(_dm("/temporary"))
        assert privacy_mode.is_temporary(d._session_key(route))
        await d.handle_message(_dm("now answer this"))
        assert not logged, "a protected conversation must persist nothing"

    def test_rotation_is_resolved_in_exactly_one_place(self) -> None:
        # The chokepoint claim, held structurally: a second maybe_rotate call site
        # is how a third path gets to key on a stale generation. The busy check is
        # the one deliberate reader of the pre-rotation key, and it does not rotate.
        import inspect
        from pathlib import Path

        from kiro_crew.telegram import transport_dispatch as td

        src = inspect.getsource(td)
        assert (
            src.count("self._conv.maybe_rotate(") == 1
        ), "rotation must be settled by _rotated_session_key alone"
        # The dispatcher's owners run on the same state, so one of them settling
        # rotation would be the second site this pins against.
        owners = sorted(Path(td.__file__).with_name("dispatch").glob("*.py"))
        assert len(owners) >= 8, owners
        assert [p.name for p in owners if "maybe_rotate(" in p.read_text("utf-8")] == []

    def test_resolving_twice_in_one_message_is_a_no_op(self) -> None:
        # The helper is called on both the bare-modifier and the turn path, so it
        # has to be safe to call more than once for one inbound message.
        d, _, _ = _dispatcher({7})
        d.cfg.messaging.idle_reset_minutes = 1
        _prime_live(d.cfg)
        route = ("direct", "7")
        first = d._rotated_session_key(route)
        assert d._rotated_session_key(route) == first


class TestDurableWritesUseTheRotatedKey:
    """Every command whose effect OUTLIVES the turn resolves through rotation.

    Extracting `_rotated_session_key` last round fixed the two callers the review
    had named and left five more reading the pre-rotation key. That is the failure
    mode a chokepoint is supposed to end, so this pins the whole set rather than the
    line a reviewer happened to notice: each of these writes something a LATER
    message or a LATER event reads back, so landing it on a generation the idle
    window has just retired means the write is silently addressed to a conversation
    nobody will look at again.

    The complement matters just as much. A command that acts on the turn ALREADY
    RUNNING must NOT rotate: minting a new key would miss the live session, so
    `/stop` would fail to stop anything and an approval press would resolve against
    a prompt that was never asked.
    """

    #: `(method, rotates)` — every session-key consumer in the dispatcher, and which
    #: side of the invariant it is on. A new command has to be added here, which is
    #: the point: the classification is the thing that keeps being got wrong.
    _CLASSIFIED = {
        # Durable: read back by a later message or a later event.
        "_handle_title": True,
        "_handle_spawn": True,
        "_handle_task": True,
        "_handle_link": True,
        "_handle_unlink": True,
        # Live: acts on the turn already running.
        "_handle_stop": False,
        "_handle_compact": False,
        "_handle_model": False,
        "_apply_model": False,
        "_apply_agent": False,
        "_callback_session_key": False,
    }

    def test_every_classified_method_matches_its_side(self) -> None:
        import inspect

        from kiro_crew.telegram.transport_dispatch import TelegramDispatcher

        wrong: list[str] = []
        for name, rotates in self._CLASSIFIED.items():
            src = inspect.getsource(getattr(TelegramDispatcher, name))
            uses_rotated = "_rotated_session_key(route)" in src
            uses_plain = "_session_key(route)" in src and not uses_rotated
            if rotates and not uses_rotated:
                wrong.append(f"{name} writes durably but resolves the PRE-rotation key")
            if not rotates and uses_rotated:
                wrong.append(f"{name} acts on the live turn but rotates, so it will miss it")
            if not rotates and not uses_plain and not uses_rotated:
                wrong.append(f"{name} no longer resolves a session key — reclassify or remove")
        assert not wrong, "\n".join(wrong)

    def test_the_classification_covers_every_consumer(self) -> None:
        # A method that resolves a session key and is absent from the table is
        # exactly the gap this class exists to close.
        import inspect

        from kiro_crew.telegram.transport_dispatch import TelegramDispatcher

        consumers = self._consumers(inspect.getsource(TelegramDispatcher))
        # The methods ``telegram/dispatch`` owns are bound on the class too, so their
        # sources are read with the same rule; a scan of the class body alone would
        # stop seeing every consumer that moved.
        owned = set()
        for owner in self._owner_modules():
            owned |= self._consumers(inspect.getsource(owner))
        assert owned >= {
            "_handle_model",
            "_apply_model",
            "_apply_agent",
            "_handle_compact",
            "_handle_spawn",
            "_handle_task",
            "_handle_link",
            "_handle_unlink",
        }, sorted(owned)
        consumers |= owned
        # These resolve a key for reasons the durable/live split does not govern.
        exempt = {
            "_rotated_session_key",
            "handle_message",
            "_notify_mode",
            "_callback_session_key",
            "_refused_turn_restricted",
        }
        missing = consumers - set(self._CLASSIFIED) - exempt
        assert not missing, f"unclassified session-key consumers: {sorted(missing)}"

    @staticmethod
    def _consumers(src: str) -> set[str]:
        """Every ``def`` in *src* whose own lines resolve a key from ``route``."""
        import re

        consumers = set()
        current = ""
        for line in src.split("\n"):
            m = re.match(r"\s*(?:async\s+)?def\s+(\w+)", line)
            if m:
                current = m.group(1)
            elif "_session_key(route)" in line and current:
                consumers.add(current)
        return consumers

    @staticmethod
    def _owner_modules() -> list[Any]:
        import importlib
        from pathlib import Path

        from kiro_crew.telegram import transport_dispatch as td

        return [
            importlib.import_module(f"kiro_crew.telegram.dispatch.{p.stem}")
            for p in sorted(Path(td.__file__).with_name("dispatch").glob("*.py"))
            if p.stem != "__init__"
        ]

    def test_the_consumer_scan_reads_an_owner_the_same_way(self) -> None:
        planted = "def _handle_new_thing(self, route):\n    return self._session_key(route)\n"
        assert self._consumers(planted) == {"_handle_new_thing"}

    @pytest.mark.asyncio
    async def test_title_renames_the_session_the_next_message_will_use(self) -> None:
        d, _, _ = _dispatcher({7})
        d.cfg.messaging.idle_reset_minutes = 1
        _prime_live(d.cfg)
        route = ("direct", "7")
        d._conv.maybe_rotate(route, time.time() - 3600, idle_minutes=1, daily_reset_hour=-1)
        titled: list[tuple[str, str]] = []
        d.conv_log = SimpleNamespace(  # type: ignore[assignment]
            append=lambda *a, **k: None,
            set_title=lambda key, title: titled.append((key, title)),
        )
        await d._handle_title(route, 7, "a better name")
        assert titled, "the rename must reach the log"
        assert titled[0][0] == d._rotated_session_key(route)
