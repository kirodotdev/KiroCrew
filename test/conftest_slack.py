"""The suite's in-memory Slack client and the per-test resets of Slack
module state.

Part of ``test/conftest.py``, which imports and re-binds these names so pytest registers
each fixture as the suite conftest's own and ``from conftest import ...`` keeps working;
see the import block there. Plain module, never collected.
"""

from __future__ import annotations

import pytest

from kiro_crew.slack.client import SlackClientOps
from kiro_crew.slack.handler import _PHASE_EMOJIS, _build_phase_emojis


@pytest.fixture(autouse=True)
def _reset_options_control_state():
    """Clear the per-message OPTIONS registries between tests.

    ``kiro_crew.slack.outbound`` holds two process-global maps keyed by
    ``(channel, ts)``: the per-message edit lock, and the once-only answer claim
    that stops a second Send click dispatching a duplicate turn. Both are
    correct as process state in the gateway, where a control's ts is unique and
    lives as long as the message does.

    Tests are the opposite: fixtures reuse a fixed pair like ``("CH1", "msg1")``
    across unrelated cases, so without this the first test to submit claims the
    control and every later test's click is silently dropped as a duplicate.
    Reset per test rather than making production defensive about it.
    """
    from kiro_crew.slack import outbound

    outbound._ANSWERED.clear()
    outbound._EDIT_LOCKS.clear()
    outbound._LOCK_USERS.clear()
    yield
    outbound._ANSWERED.clear()
    outbound._EDIT_LOCKS.clear()
    outbound._LOCK_USERS.clear()


@pytest.fixture(autouse=True)
def _enterprise_bypass(_floor_monkeypatch) -> None:
    """Set a default validated team_id so _route_message doesn't reject messages."""
    _floor_monkeypatch.setattr("kiro_crew.slack.enterprise._validated_team_id", "TTEST")
    _floor_monkeypatch.setattr("kiro_crew.slack.enterprise._validated_enterprise_id", "ETEST")
    _floor_monkeypatch.setattr("kiro_crew.slack.enterprise._allowed_team_ids", {"TTEST"})


@pytest.fixture(autouse=True)
def _clean_emojis():
    """Reset _PHASE_EMOJIS to defaults before each test (suppresses local config)."""
    original = dict(_PHASE_EMOJIS)
    _PHASE_EMOJIS.clear()
    _PHASE_EMOJIS.update(_build_phase_emojis({})[0])
    yield
    _PHASE_EMOJIS.clear()
    _PHASE_EMOJIS.update(original)


@pytest.fixture(autouse=True)
def _clean_slack_thread_state():
    """Reset the ``handler`` module-global thread-state maps between tests.

    ``handler`` keeps process-global maps for per-thread privacy and routing
    state: ``_thread_temporary`` / ``_thread_incognito`` (drive
    ``_is_slack_restricted`` — the memory-write gate consulted by ``!title``,
    consolidation, etc.), ``_titled_threads`` (auto-title claim), and
    ``_thread_agents`` (per-thread agent override). Nothing clears these
    globally, so a test that marks a thread restricted — including one that
    drives the real ``handle_message_transport`` drain path against a
    ``MagicMock`` session map, whose ``_hydrate_conv_flags`` reads truthy mock
    flags and calls ``_mark_incognito`` / ``_mark_temporary`` — leaves e.g.
    ``"thread1"`` in ``_thread_incognito`` forever. Under ``pytest -n auto``
    (``--dist load`` interleaves tests across files on each worker) a later
    ``test_title_updates_conversation_log`` then sees
    ``_is_slack_restricted("thread1") is True`` and skips ``set_title``,
    failing with no production-code change — a classic order-dependent flake.
    Clearing before and after every test makes each hermetic regardless of
    scheduling. Idempotent with per-file fixtures that already clear a subset.
    """
    from kiro_crew.slack import handler as _h
    from kiro_crew.slack import thread_override_slots as _slots

    _maps = (
        _h._thread_temporary,
        _h._thread_incognito,
        _h._titled_threads,
        _h._thread_agents,
        _slots._order,
        _slots._pinned,
    )
    for _m in _maps:
        _m.clear()
    yield
    for _m in _maps:
        _m.clear()


class MockSlackClient(SlackClientOps):
    """In-memory mock for testing."""

    def __init__(self):
        self.actions: list[tuple[str, dict]] = []
        self._next_ts = 1000000
        self._fetch_message_result: str | None = None
        self._fetch_thread_replies_result: list[dict] = []

    async def post_message(self, channel, text, thread_ts=None):
        ts = f"{self._next_ts}.000000"
        self._next_ts += 1
        self.actions.append(
            ("post", {"channel": channel, "text": text, "thread_ts": thread_ts, "ts": ts})
        )
        return ts

    async def post_blocks(self, channel, blocks, text, thread_ts=None):
        ts = f"{self._next_ts}.000000"
        self._next_ts += 1
        self.actions.append(
            (
                "blocks",
                {
                    "channel": channel,
                    "blocks": blocks,
                    "text": text,
                    "thread_ts": thread_ts,
                    "ts": ts,
                },
            )
        )
        return ts

    async def update_message(self, channel, ts, text):
        self.actions.append(("update", {"channel": channel, "ts": ts, "text": text}))

    async def delete_message(self, channel, ts):
        self.actions.append(("delete", {"channel": channel, "ts": ts}))

    async def add_reaction(self, channel, ts, emoji, raise_on_error=False):
        self.actions.append(("react", {"channel": channel, "ts": ts, "emoji": emoji}))

    async def remove_reaction(self, channel, ts, emoji, raise_on_error=False):
        self.actions.append(("unreact", {"channel": channel, "ts": ts, "emoji": emoji}))

    async def open_dm(self, user_id):
        self.actions.append(("open_dm", {"user_id": user_id}))
        return f"D{user_id}"

    async def post_ephemeral(self, channel, user_id, text, blocks=None, thread_ts=None):
        self.actions.append(
            (
                "ephemeral",
                {
                    "channel": channel,
                    "user_id": user_id,
                    "text": text,
                    "blocks": blocks,
                    "thread_ts": thread_ts,
                },
            )
        )

    async def views_publish(self, user_id, view):
        self.actions.append(("views_publish", {"user_id": user_id, "view": view}))

    async def views_open(self, trigger_id, view):
        self.actions.append(("views_open", {"trigger_id": trigger_id, "view": view}))

    async def views_update(self, view_id, view):
        self.actions.append(("views_update", {"view_id": view_id, "view": view}))

    async def upload_file(self, channel, thread_ts, file, filename, title):
        self.actions.append(
            (
                "upload_file",
                {
                    "channel": channel,
                    "thread_ts": thread_ts,
                    "file": file,
                    "filename": filename,
                    "title": title,
                },
            )
        )

    async def start_stream(self, channel, thread_ts, initial_text=None, team_id=None, user_id=None):
        if not getattr(self, "_stream_enabled", False) or getattr(
            self, "_start_stream_fails", False
        ):
            return None
        ts = f"{self._next_ts}.000000"
        self._next_ts += 1
        self.actions.append(
            (
                "start_stream",
                {
                    "channel": channel,
                    "thread_ts": thread_ts,
                    "text": initial_text,
                    "ts": ts,
                },
            )
        )
        return ts

    async def append_stream(self, channel, ts, text):
        self.actions.append(("append_stream", {"channel": channel, "ts": ts, "text": text}))
        return True

    async def append_task(self, channel, ts, task_id, title, status, details="", output=""):
        self.actions.append(
            (
                "append_task",
                {
                    "channel": channel,
                    "ts": ts,
                    "task_id": task_id,
                    "title": title,
                    "status": status,
                    "details": details,
                },
            )
        )
        return True

    async def stop_stream(self, channel, ts, final_text=None):
        self.actions.append(("stop_stream", {"channel": channel, "ts": ts, "text": final_text}))
        return True

    async def set_thread_title(self, channel, thread_ts, title):
        self.actions.append(
            ("set_thread_title", {"channel": channel, "thread_ts": thread_ts, "title": title})
        )

    async def set_thread_status(self, channel, thread_ts, status):
        self.actions.append(
            ("set_thread_status", {"channel": channel, "thread_ts": thread_ts, "status": status})
        )

    async def fetch_message(self, channel: str, ts: str) -> str | None:
        self.actions.append(("fetch_message", {"channel": channel, "ts": ts}))
        return self._fetch_message_result

    async def fetch_thread_replies(
        self, channel: str, thread_ts: str, limit: int = 200, warn_on_pagination: bool = True
    ) -> list[dict]:
        self.actions.append(
            (
                "fetch_thread_replies",
                {
                    "channel": channel,
                    "thread_ts": thread_ts,
                    "limit": limit,
                    "warn_on_pagination": warn_on_pagination,
                },
            )
        )
        return self._fetch_thread_replies_result
