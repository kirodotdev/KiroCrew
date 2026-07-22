"""Shared test harness for the messaging-transport Slack tests.

Provides the recording Slack client, scripted provider, and fake session
manager reused by ``test_slack_transport_dispatch`` and
``test_slack_transport_integration`` (imported via ``importlib``).

NOTE: the upstream project also shipped a set of golden
native-``handle_message`` transcript baselines here (``_GOLDEN_*`` +
``run_native_turn``). Those baselines were captured against the internal beta
renderer (which posts a 💭 placeholder + deletes it) and would NOT match this
fork's native handler, so they are intentionally omitted. The abstraction
itself is covered by the messaging/driver/renderer/transport suites; this file
only carries the reusable fakes.
"""

from __future__ import annotations

from typing import Any

from kiro_crew.acp.types import AcpEvent
from kiro_crew.slack.client import SlackClientOps


def make_event(kind: str, **kw: Any) -> AcpEvent:
    """Build a scripted provider event."""
    return AcpEvent(kind=kind, **kw)


class RecordingSlackClient(SlackClientOps):
    """SlackClientOps that records every outbound call in order."""

    def __init__(self) -> None:
        self.transcript: list[tuple[str, dict]] = []
        self._ts = 0
        self.stream_disabled = False  # when True, start_stream returns None

    def _next_ts(self) -> str:
        self._ts += 1
        return f"ts-{self._ts}"

    def _rec(self, method: str, **kw: Any) -> None:
        self.transcript.append((method, kw))

    # -- abstract methods --
    async def post_message(self, channel, text, thread_ts=None, unfurl_links=None, unfurl_media=None) -> str:
        self._rec("post_message", channel=channel, text=text, thread_ts=thread_ts)
        return self._next_ts()

    async def post_blocks(self, channel, blocks, text, thread_ts=None, unfurl_links=None, unfurl_media=None) -> str:
        self._rec("post_blocks", channel=channel, text=text, thread_ts=thread_ts, n_blocks=len(blocks))
        return self._next_ts()

    async def update_message(self, channel, ts, text="", blocks=None) -> None:
        self._rec("update_message", channel=channel, ts=ts, text=text)

    async def delete_message(self, channel, ts) -> None:
        self._rec("delete_message", channel=channel, ts=ts)

    async def add_reaction(self, channel, ts, emoji, raise_on_error=False) -> None:
        self._rec("add_reaction", channel=channel, ts=ts, emoji=emoji)

    async def remove_reaction(self, channel, ts, emoji, raise_on_error=False) -> None:
        self._rec("remove_reaction", channel=channel, ts=ts, emoji=emoji)

    async def upload_file(self, channel, thread_ts, file, filename, title) -> None:
        self._rec("upload_file", channel=channel, filename=filename, title=title)

    async def open_dm(self, user_id) -> str:
        self._rec("open_dm", user_id=user_id)
        return "D-FAKE"

    async def post_ephemeral(self, channel, user_id, text, blocks=None, thread_ts=None) -> None:
        self._rec("post_ephemeral", channel=channel, user_id=user_id, text=text)

    async def views_publish(self, user_id, view) -> None:
        self._rec("views_publish", user_id=user_id)

    # -- streaming + assistant API (default impls in ABC; recorded here) --
    async def start_stream(self, channel, thread_ts, initial_text=None, team_id=None, user_id=None) -> str | None:
        self._rec("start_stream", channel=channel, thread_ts=thread_ts)
        if self.stream_disabled:
            return None
        return self._next_ts()

    async def append_stream(self, channel, ts, text) -> bool:
        self._rec("append_stream", channel=channel, ts=ts, text=text)
        return True

    async def stop_stream(self, channel, ts, final_text=None) -> bool:
        self._rec("stop_stream", channel=channel, ts=ts, final_text=final_text)
        return True

    async def append_task(self, channel, ts, task_id, title, status, details="", output="") -> bool:
        self._rec("append_task", channel=channel, ts=ts, title=title, status=status)
        return True

    async def set_thread_status(self, channel, thread_ts, status) -> None:
        self._rec("set_thread_status", channel=channel, thread_ts=thread_ts, status=status)

    async def set_thread_title(self, channel, thread_ts, title) -> None:
        self._rec("set_thread_title", channel=channel, thread_ts=thread_ts, title=title)

    async def set_suggested_prompts(self, channel, thread_ts, prompts) -> None:
        self._rec("set_suggested_prompts", channel=channel, thread_ts=thread_ts)

    async def fetch_message(self, channel, ts) -> str | None:
        self._rec("fetch_message", channel=channel, ts=ts)
        return None

    async def fetch_thread_replies(self, channel, thread_ts, limit=200, warn_on_pagination=True) -> list[dict]:
        self._rec("fetch_thread_replies", channel=channel, thread_ts=thread_ts)
        return []


class ScriptedProvider:
    """Provider stand-in whose stream() yields a fixed event list."""

    def __init__(self, events: list[AcpEvent]) -> None:
        self._events = events
        self.stream_calls = 0
        self.approved: list = []
        self.rejected: list = []

    async def stream(self, message: str):
        self.stream_calls += 1
        # First call = main turn; later calls (e.g. title gen) get an empty
        # stream so the transcript stays deterministic.
        events = self._events if self.stream_calls == 1 else []
        for ev in events:
            yield ev

    async def approve_tool(self, request_id, *, always=False):
        self.approved.append(request_id)

    async def reject_tool(self, request_id):
        self.rejected.append(request_id)

    async def stream_command(self, command: str):
        if False:  # pragma: no cover - async-gen shape only
            yield None


class FakeSessions:
    """Minimal SessionManager stand-in."""

    def __init__(self, provider: ScriptedProvider) -> None:
        self._provider = provider
        self._sessions: dict = {}
        self.links: dict = {}

    def get_session_for_thread(self, thread_ts: str):
        return None

    async def get_or_create(self, session_key, agent=None, channel_id=None):
        return self._provider, False, False  # (client, is_new, resumed)

    async def set_channel(self, session_key, channel):
        return None

    def set_slack_link(self, key, thread_ts, channel_id):
        self.links[key] = (thread_ts, channel_id)

    def get_pid(self, session_key):
        return None

    def is_cancelled(self, session_key, msg_ts):
        return False

    def get_provider(self, session_key):
        return "acp"

    def record_success(self, session_key):
        return None

    def check_context_usage(self, key, provider):
        return 0.0

    async def record_failure(self, session_key):
        return None

    def release(self, session_key):
        return None
