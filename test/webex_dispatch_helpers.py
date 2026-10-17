"""Shared fakes and builders for the ``test_webex_dispatch*`` tests.

The provider, session, hook, context, client and conversation-log fakes, the config and
dispatcher builders, the inbound and queue-entry builders, and the polling wait.
"""

from __future__ import annotations

import asyncio
from contextlib import contextmanager
from types import SimpleNamespace

from _hot_reload_helpers import prime_live_sections

from kiro_crew.messaging.queue_drain import tag_entry
from kiro_crew.session_allocation import SessionClosingError
from kiro_crew.webex.client import WebexInbound
from kiro_crew.webex.transport_dispatch import WebexDispatcher

# ------------------------------------------------------------------
# Fakes
# ------------------------------------------------------------------


class FakeProvider:
    supports_steer = True

    def __init__(self, events: list) -> None:
        self._events = events
        self.compacted = False
        self.steered: list = []
        self.active_turn = True
        self.cancelled: list = []

    def has_active_turn(self) -> bool:
        return self.active_turn

    async def steer(self, text: str) -> bool:
        self.steered.append(text)
        return True

    async def stream(self, message: str):
        for ev in self._events:
            yield ev

    async def approve_tool(self, rid) -> None:
        pass

    async def reject_tool(self, rid) -> None:
        pass

    async def cancel(self, wait_ack_timeout: float = 0.0) -> None:
        self.cancelled.append(wait_ack_timeout)

    async def compact(self) -> None:
        self.compacted = True

    async def wait_for_compaction(self, timeout: float = 0.0) -> dict:
        return {"type": "completed", "summary": ""}


class FakeSessions:
    def __init__(
        self,
        provider,
        *,
        is_new=True,
        raise_on_get=None,
        ctx_pct=0.0,
        acquire=True,
        has_session=None,
    ) -> None:
        self._p = provider
        self._is_new = is_new
        self._raise = raise_on_get
        self._ctx_pct = ctx_pct
        self._acquire = acquire
        self._has_session = provider is not None if has_session is None else has_session
        self.acquired: list = []
        self.released: list = []
        self.successes: list = []
        self.failures: list = []
        self.channels: list = []
        self.last_agent = None
        self.queued: list = []
        self.cleared: list = []
        self.origin_links: dict = {}
        self.mirror_links: dict = {}
        self.opt_out: dict = {}
        self.batched = 0
        # `closing` mirrors SessionManager._closing so begin_turn refuses the
        # dispatch the way the real gate does after close_all.
        self.closing = False
        self.begin_turns = 0
        self.reserved_generations: set[str] = set()

    async def get_or_create(self, key, *, agent=None, channel_id=None, start_priority=None):
        self.last_agent = agent
        if self._raise is not None:
            raise self._raise
        return self._p, self._is_new, False

    def begin_turn(self, key):
        """The real manager's synchronous pre-dispatch closing gate."""
        self.begin_turns += 1
        if self.closing:
            raise SessionClosingError("SessionManager is closing")

    async def set_channel(self, key, cid) -> None:
        self.channels.append((key, cid))

    def release(self, key) -> None:
        self.released.append(key)

    def record_success(self, key) -> None:
        self.successes.append(key)

    async def record_failure(self, key) -> None:
        self.failures.append(key)

    def check_context_usage(self, key, provider) -> float:
        return self._ctx_pct

    def get_provider(self, key):
        return self._p

    async def try_acquire(self, key) -> bool:
        self.acquired.append(key)
        return self._acquire

    def has_session(self, key) -> bool:
        return self._has_session

    def is_busy(self, key) -> bool:
        return getattr(self, "_busy", False)

    def reserve_generation(self, session_key: str) -> None:
        self.reserved_generations.add(session_key)

    async def aflush(self) -> None:
        return None

    def max_generation(self, bucket: str) -> int:
        prefix = f"{bucket}:gen"
        return max(
            (
                int(key[len(prefix) :])
                for key in self.reserved_generations
                if key.startswith(prefix) and key[len(prefix) :].isdigit()
            ),
            default=-1,
        )

    # -- mid-turn queue (drive_turn's drain + /stop) --
    def enqueue(self, key, ts, text, *, force=False, **kw) -> bool:
        if not force and not self.is_busy(key):
            return False
        self.queued.append((str(ts), text, dict(kw)))
        return True

    def dequeue(self, key):
        return self.queued.pop(0) if self.queued else None

    def clear_queue(self, key, owned_by=None, *, only=None) -> None:
        self.cleared.append(key)
        self.queued.clear()

    def detach_queue(self, key):
        taken = tuple(self.queued)
        self.queued.clear()
        return taken

    def restore_queue(self, key, entries) -> None:
        self.queued[:0] = list(entries)

    # -- origin / mirror binding (drive_turn binds on every turn) --
    def set_origin_link(self, key, link) -> None:
        self.origin_links[key] = link

    def mirror_opt_out(self, key) -> bool:
        return self.opt_out.get(key, False)

    def set_mirror_opt_out(self, key, value) -> None:
        self.opt_out[key] = bool(value)

    def get_mirror_link(self, key):
        return self.mirror_links.get(key)

    def set_mirror_link(self, key, link, *, reason="") -> None:
        self.mirror_links[key] = link

    def clear_mirror_link(self, key, *, reason="") -> int:
        # Returns a COUNT, matching SessionMap: release_conversation_location
        # sums these to decide between "Unlinked" and "wasn't linked".
        return 1 if self.mirror_links.pop(key, None) is not None else 0

    def clear_mirror_links_at(self, location, *, reason="") -> list[str]:
        swept = [k for k, v in self.mirror_links.items() if v == location]
        for k in swept:
            self.mirror_links.pop(k, None)
        return swept

    @contextmanager
    def batched_save(self):
        self.batched += 1
        yield

    def compact_wait_budget_secs(self) -> float:
        """The real manager's resolved ``session.compact_wait_secs`` (unset: 300 s)."""
        return 300.0


class _GateResult:
    def __init__(self, action: str = "") -> None:
        self.action = action


class FakeHooks:
    auto_approve_subagent_spawn = False

    def on_tool_call(self, title, **kw):
        return _GateResult("")


class FakeCtx:
    def __init__(self) -> None:
        self.hooks = FakeHooks()

    # Names only the kwargs it asserts on and takes ``**kw`` for the rest: the
    # shared pipeline owns the call shape, so a fake that pins every kwarg
    # breaks on any field the pipeline later forwards, without that field
    # having anything to do with this channel. Mirrors the pipeline's own fake.
    def build_message(self, text, is_new, key, *, channel_id, agent, resumed, runtime_source, **kw):
        assert runtime_source == "webex"
        return (text, None)


class FakeClient:
    def __init__(self) -> None:
        self.sent: list[tuple[str, str]] = []
        self.edits: list[tuple[str, str, str]] = []
        self.deleted: list[str] = []
        self._next_id = 0

    async def send_message(self, conversation_id: str, markdown: str, **kw) -> str:
        self.sent.append((conversation_id, markdown))
        self._next_id += 1
        return f"MSG{self._next_id}"

    async def edit_message(self, message_id: str, room_id: str, markdown: str) -> bool:
        self.edits.append((message_id, room_id, markdown))
        return getattr(self, "edit_ok", True)

    async def delete_message(self, message_id: str) -> None:
        self.deleted.append(message_id)


class FakeConvLog:
    def __init__(self) -> None:
        self.appended: list[tuple[str, str, str]] = []
        self.titles: dict[str, str] = {}
        self.metadata: dict[str, dict] = {}

    def append(self, key, role, text, agent=None, mid=None) -> None:
        self.appended.append((key, role, text))

    def set_title(self, key, title) -> None:
        self.titles[key] = title

    def update_metadata_if(self, key: str, fields: dict, guard) -> bool:
        if not guard(self.metadata.get(key, {})):
            return False
        self.metadata.setdefault(key, {}).update(fields)
        rows = getattr(self, "_rows", None)
        if isinstance(rows, list):
            from kiro_crew.history import transcript_stem

            rows.insert(0, {"key": transcript_stem(key), **fields})
        return True


def _cfg(default_agent: str = "", approval_mode: str = "interactive"):
    return SimpleNamespace(
        agent=SimpleNamespace(default_agent=default_agent, approval_mode=approval_mode),
        webex=SimpleNamespace(
            hard_threshold_pct=95.0,
            soft_threshold_pct=80.0,
            allowed_emails=[_EMAIL],
            allow_group_rooms=False,
            allowed_room_ids=[],
            reply_in_thread=True,
            wdm_base="",
        ),
        messaging=SimpleNamespace(
            dm_scope="per-channel-peer",
            idle_reset_minutes=0,
            daily_reset_hour=-1,
            queue_mode="steer",
        ),
    )


def _prime_live(cfg) -> None:
    """Publish *cfg*'s ``webex`` and ``messaging`` fields as the live snapshot.

    The dispatcher reads those two sections at POINT OF USE from the config
    watcher rather than from the ``cfg=`` copy it was constructed with, so a
    test that varies one of them has to put the value where the turn actually
    looks for it. Call it again after mutating ``d.cfg`` mid-test -- the snapshot
    is a copy, not a view.
    """
    prime_live_sections(cfg, "webex", "messaging")


def _dispatcher(sessions, ctx, client, *, conv_log=None, agent=None, cfg=None):
    cfg = cfg or _cfg()
    _prime_live(cfg)
    d = WebexDispatcher(
        sessions=sessions,
        ctx_builder=ctx,
        cfg=cfg,
        agent=agent,
        conv_log=conv_log,
        approval_mode="interactive",
    )
    d.client = client
    return d


def _cfg_queue():
    cfg = _cfg()
    cfg.messaging.queue_mode = "queue"
    return cfg


async def _spin(predicate, timeout: float = 1.0) -> None:
    """Poll *predicate* until true. Polling, not sleeping — a fixed sleep long
    enough to be reliable on a loaded box also dominates the suite."""
    # flake-ok: pre-existing bounded poll, moved verbatim; the deadline only ends a wait the predicate never met
    deadline = asyncio.get_running_loop().time() + timeout
    while asyncio.get_running_loop().time() < deadline:
        if predicate():
            return
        # flake-ok: pre-existing yield between predicate polls, moved verbatim; not a barrier
        await asyncio.sleep(0)
    raise AssertionError("condition not reached")


_EMAIL = "kyle@example.com"


def _inbound(text: str = "hello", email: str = _EMAIL) -> WebexInbound:
    return WebexInbound(person_email=email, room_id="ROOM", text=text, room_type="direct")


def _entry(
    ts: str,
    text: str,
    *,
    room: str = "ROOM",
    parent: str = "",
    email: str = _EMAIL,
    room_type: str = "direct",
    **extra: object,
) -> tuple[str, str, dict]:
    """A queue entry recorded the way THIS channel's producer records one.

    Built through the real ``tag_entry`` rather than by spelling the neutral key, so a
    test cannot drift from the contract the drain reads. An entry missing the tag is a
    FOREIGN entry to every drain, which is the point of the field -- so a fixture that
    left it out would be testing the set-aside path while claiming to test the answer
    path.
    """
    kwargs: dict = {
        "webex_room_id": room,
        "webex_parent_id": parent,
        "webex_person_email": email,
        "webex_room_type": room_type,
        **extra,
    }
    return (ts, text, tag_entry(kwargs, "webex", ""))


def _space(text: str = "hello", email: str = _EMAIL, room_id: str = "SPACE1") -> WebexInbound:
    return WebexInbound(
        person_email=email,
        room_id=room_id,
        text=text,
        room_type="group",
        mentioned_people=("BOTID",),
    )


def _cfg_group():
    cfg = _cfg()
    cfg.webex.allow_group_rooms = True
    cfg.webex.allowed_room_ids = ["SPACE1"]
    return cfg
