"""Every dispatcher's ``finally`` must survive a renderer that fails to close.

``renderer.close()`` runs in the ``finally`` of a turn, ahead of the session
release. If it raises and the call is unguarded, the release never happens --
and because the semaphore is keyed by SESSION, that does not merely lose one
turn: every later message in that conversation blocks forever and its queue
never drains. The channel looks permanently busy until the gateway restarts.

The shared pipeline (``messaging/dispatch.py``) and Discord both guard this
already; Telegram and Slack did not, which is what these tests pin. Telegram is
the worst case of the three because two more steps sit after its ``close()`` --
the ``_active_renderers`` pop and the attachment temp-file cleanup -- so an
unguarded raise leaked three things, not one.

``TestRatchet`` is the part that makes this un-forgettable: it reads every
dispatcher's source and fails if a ``renderer.close()`` is not inside a ``try``.
A new channel that copies the old shape fails here rather than in production.
"""

from __future__ import annotations

import ast
import asyncio
import importlib
import sys
from pathlib import Path

import kiro_crew.messaging.dispatch as _pipeline
from kiro_crew.acp.types import EVENT_COMPLETE, EVENT_TEXT_CHUNK, STOP_REASON_END_TURN
from kiro_crew.slack import transport_dispatch as slack_dispatch

sys.path.insert(0, str(Path(__file__).parent))

_tg = importlib.import_module("test_telegram")
_golden = importlib.import_module("test_slack_golden_transcript")


class _CountingSessions(_golden.FakeSessions):
    """Slack's FakeSessions.release() records nothing; count it here."""

    def __init__(self, provider) -> None:
        super().__init__(provider)
        self.released: list = []

    def release(self, session_key):
        self.released.append(session_key)
        return None


class TestSlack:
    def test_release_survives_a_close_failure(self, monkeypatch) -> None:
        """Pre-fix this leaked the semaphore for the whole conversation."""
        monkeypatch.setattr(slack_dispatch, "_get_default_agent", lambda: "")
        monkeypatch.setattr(slack_dispatch, "_hydrate_thread_overrides", lambda *a, **k: None)
        monkeypatch.setattr(slack_dispatch, "_hydrate_conv_flags", lambda *a, **k: None)
        monkeypatch.setattr(slack_dispatch, "_thread_agents", {})

        real_renderer = slack_dispatch.SlackRenderer

        class _ExplodingRenderer(real_renderer):  # type: ignore[misc,valid-type]
            async def close(self):  # noqa: D102
                raise RuntimeError("slack renderer finalization failed")

        monkeypatch.setattr(slack_dispatch, "SlackRenderer", _ExplodingRenderer)

        provider = _golden.ScriptedProvider(
            [
                _golden.make_event(EVENT_TEXT_CHUNK, text="hi"),
                _golden.make_event(EVENT_COMPLETE, stop_reason=STOP_REASON_END_TURN),
            ]
        )
        sessions = _CountingSessions(provider)

        # Must not raise: a finalization failure is not a turn failure.
        asyncio.run(
            slack_dispatch.handle_message_transport(
                slack=_golden.RecordingSlackClient(),
                sessions=sessions,
                channel="C1",
                text="hello",
                thread_ts=None,
                msg_ts="1700000000.000100",
                user_id="U_OWNER",
                context_builder=None,
                conversation_log=None,
            )
        )

        assert sessions.released, (
            "Slack did not release the session after renderer.close() raised -- "
            "that conversation is now permanently busy"
        )


class TestTelegram:
    def test_release_and_cleanup_survive_a_close_failure(self, monkeypatch) -> None:
        """Three steps sit after telegram's close(); all three must still run."""
        d, cli, sess = _tg._dispatcher({7})

        real_renderer = _tg.TelegramRenderer

        class _ExplodingRenderer(real_renderer):  # type: ignore[misc,valid-type]
            async def close(self, failure_reason: str | None = None):  # noqa: D102
                raise RuntimeError("telegram renderer finalization failed")

        import kiro_crew.telegram.transport_dispatch as tg_dispatch

        monkeypatch.setattr(tg_dispatch, "TelegramRenderer", _ExplodingRenderer)

        cleaned: list = []
        monkeypatch.setattr(tg_dispatch, "cleanup_attachments", lambda paths: cleaned.append(paths))

        msg = _tg.InboundMessage(
            channel_type="telegram",
            user_id="7",
            conversation_id="7",
            text="hello",
        )

        # Must not raise.
        asyncio.run(d.handle_message(msg))

        assert sess.released, (
            "Telegram did not release the session after close() raised -- the "
            "conversation is permanently busy"
        )
        assert cleaned, (
            "Telegram skipped cleanup_attachments after close() raised -- "
            "attachment temp files leak on every failed finalization"
        )


def _close_calls_are_guarded(path: Path) -> list[int]:
    """Return the line numbers of ``renderer.close()`` calls NOT inside a try.

    Source-level rather than behavioural on purpose: the point is to catch a
    NEW channel that never got a test, and a parametrised behavioural test
    cannot be written for a dispatcher that does not exist yet.
    """
    tree = ast.parse(path.read_text(encoding="utf-8"))
    guarded: set[int] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Try):
            for stmt in node.body:
                for inner in ast.walk(stmt):
                    if isinstance(inner, ast.Call):
                        guarded.add(inner.lineno)

    unguarded: list[int] = []
    for node in ast.walk(tree):
        if not isinstance(node, ast.Call):
            continue
        func = node.func
        if not isinstance(func, ast.Attribute) or func.attr != "close":
            continue
        target = func.value
        if not (isinstance(target, ast.Name) and target.id == "renderer"):
            continue
        if node.lineno not in guarded:
            unguarded.append(node.lineno)
    return unguarded


class TestRatchet:
    def test_no_dispatcher_awaits_close_unguarded(self) -> None:
        pkg = Path(_pipeline.__file__).resolve().parent.parent
        targets = [pkg / "messaging" / "dispatch.py"] + sorted(
            pkg.glob("*/transport_dispatch.py")
        )
        assert len(targets) >= 5, f"expected the dispatcher set, found {targets}"

        offenders = {
            str(p.relative_to(pkg)): lines
            for p in targets
            if (lines := _close_calls_are_guarded(p))
        }
        assert not offenders, (
            "these dispatchers await renderer.close() outside a try, so a "
            "finalization failure skips the session release and wedges the "
            f"conversation: {offenders}"
        )
