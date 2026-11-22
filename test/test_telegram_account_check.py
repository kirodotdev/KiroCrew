"""Telegram's answers when the Kiro account check retires or flags a session.

``SessionManager.try_acquire`` runs the account check before it takes the per-session
lease. A session that held another account is retired, so nothing is live by the time the
lease would be taken. An idle parent the check spared for its running sub-agents is only
flagged: refused, yet still registered. These tests pin what ``/compact`` and the
``/model`` picker answer in each case, for a route's own session and for a resumed one.
"""

from __future__ import annotations

import asyncio
from typing import Any

from telegram_helpers import _drop_live_config_snapshot  # noqa: F401
from telegram_helpers import _dispatcher, _dm, _press

from kiro_crew.messaging.commands import COMPACT_ACCOUNT_CHANGED_TEXT
from kiro_crew.messaging.transport import InboundMessage

_ROUTE = ("direct", "7")
_PICKED = "gpt-5.6-sol"
_PICK_DEFERRED = f"✅ Model set to {_PICKED} — it applies to your next message."
_RESUMED_UNCHANGED = (
    "⚠️ Couldn't switch this conversation to model-a — the resumed session was not changed."
)


def _picker_posted() -> tuple[Any, Any, Any]:
    """A dispatcher for user 7 that has posted the /model picker over two advertised models."""
    d, cli, sess = _dispatcher({7})
    sess._gp.advertised = [
        {"modelId": "claude-opus-5", "name": "Opus 5"},
        {"modelId": _PICKED, "name": "GPT 5.6 Sol"},
    ]
    asyncio.run(d.handle_message(_dm("/model")))
    return d, cli, sess


def _retire_then_refuse(sess: Any) -> Any:
    """A ``try_acquire`` whose account check retires the session: it held another account,
    so nothing is live by the time the lease would be taken."""

    async def _try_acquire(key: str) -> bool:
        sess._has = False
        return False

    return _try_acquire


def test_compact_on_a_flagged_session_says_the_account_changed() -> None:
    # Flagged, not retired: try_acquire refuses the session while has_session still
    # answers True, and no reply is running.
    d, cli, sess = _dispatcher({7})
    sess.retiring = True

    asyncio.run(
        d.handle_message(
            InboundMessage(
                channel_type="telegram", user_id="7", conversation_id="7", text="/compact"
            )
        )
    )
    assert cli.sent == [(COMPACT_ACCOUNT_CHANGED_TEXT, None)]
    assert sess.acquired == [] and sess.released == []


def test_a_retired_session_defers_the_pick_to_the_next_message() -> None:
    d, cli, sess = _picker_posted()
    sess.try_acquire = _retire_then_refuse(sess)
    asyncio.run(d.on_callback(_press("m:2")))
    assert cli.edits[-1][1] == _PICK_DEFERRED
    assert d._model_pref[_ROUTE] == _PICKED
    assert sess.released == []


def test_a_flagged_session_defers_to_the_next_message() -> None:
    d, cli, sess = _picker_posted()
    sess.retiring = True
    asyncio.run(d.on_callback(_press("m:2")))
    assert cli.edits[-1][1] == _PICK_DEFERRED
    assert d._model_pref[_ROUTE] == _PICKED
    assert sess.acquired == [] and sess.released == []


def test_a_resumed_session_the_account_check_retires_is_reported_unchanged() -> None:
    d, _cli, sess = _dispatcher({7})
    sess.try_acquire = _retire_then_refuse(sess)
    # A resumed session stores no route preference and restarts on its own model,
    # so the pick reaches nothing: the answer must not promise the next message.
    assert asyncio.run(d._apply_model(_ROUTE, "model-a", "dashboard:x")) == _RESUMED_UNCHANGED
    assert _ROUTE not in d._model_pref


def test_a_flagged_resumed_session_is_reported_unchanged() -> None:
    # Still registered but refused, and it restarts on its own model, so the pick
    # reaches nothing, the same as for a retired one.
    d, _cli, sess = _dispatcher({7})
    sess.retiring = True
    assert asyncio.run(d._apply_model(_ROUTE, "model-a", "dashboard:x")) == _RESUMED_UNCHANGED
    assert _ROUTE not in d._model_pref and sess.acquired == []
