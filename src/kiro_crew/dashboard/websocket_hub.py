"""WebSocket client registry, authorization, serialization, and fan-out."""

from __future__ import annotations

import asyncio
import json
import logging
import time
from collections.abc import Callable, Mapping
from typing import Any, Protocol

from aiohttp import web

from kiro_crew.write_buffer_level import level_is_progress_signal

#: Capability a dashboard tab declares in the ``caps`` query parameter of
#: ``/api/ws`` when its bundle applies ``slot_patch`` frames.
SLOT_PATCH_CAPABILITY = "slot_patch"

#: Socket attribute recording that declaration. Only a dashboard-user socket
#: carries it; every other socket keeps receiving the full ``slots`` list.
SLOT_PATCH_WS_FLAG = "_slot_patch"

#: Capability a dashboard tab declares in ``?caps=`` when its bundle sends
#: ``fold_watch`` and can live with slot-fold frames for only the boards it watches.
FOLD_DELTA_CAPABILITY = "fold_delta.1"

#: Socket attribute recording that declaration (set through
#: :meth:`WebSocketHub.set_fold_delta_capable`). A socket without it is LEGACY: it
#: receives every board's ``slot_projection`` frames, exactly as before.
FOLD_DELTA_WS_FLAG = "_fold_delta"

#: Socket attribute holding the boards a capable tab watches, a ``frozenset``.
FOLD_WATCH_WS_KEY = "_fold_watch"

#: Capability a tab declares in ``?caps=``, BESIDE :data:`FOLD_DELTA_CAPABILITY`, when
#: its bundle applies ``slot_fold`` delta frames. A separate token, because a bundle
#: that shipped ``fold_delta.1`` reads only ``slot_projection``: it watches boards but
#: cannot apply a delta, and must keep receiving whole values.
FOLD_APPLY_CAPABILITY = "fold_delta.2"

#: Socket attribute recording that declaration. Only ever set on a socket that also
#: carries :data:`FOLD_DELTA_WS_FLAG`.
FOLD_APPLY_WS_FLAG = "_fold_apply"

#: The frame a delta-applying socket gets for a ``work`` advance instead of that
#: board's ``slot_projection`` frame. Same string as
#: ``dashboard.handlers.crew_log.DELTA_FRAME``.
SLOT_DELTA_FRAME = "slot_fold"

#: The frame type whose fan-out the watch sets route. Same string as
#: ``dashboard.handlers.crew_log.SLOT_FRAME``, spelled here so the hub imports no
#: handler module.
SLOT_FOLD_FRAME = "slot_projection"

#: The frame a capable socket is answered with right after connect, so the tab knows
#: the gateway routes by watch set. Sent by the socket handler, not by the hub.
FOLD_HELLO_FRAME = "fold_hello"

#: The most boards one socket may watch, and the longest key accepted. A tab shows a
#: handful; the caps bound what a misbehaving client can make the hub hold.
MAX_FOLD_WATCH_SLOTS = 64
MAX_FOLD_WATCH_KEY_LEN = 256

#: How many creator hops an ancestor walk takes before it stops, so a creation
#: cycle in the tree cannot loop.
_MAX_ANCESTOR_DEPTH = 32


def fold_hello_payload(ws: Any = None) -> dict[str, Any]:
    """The ``data`` of :data:`FOLD_HELLO_FRAME`: schema version and routed folds.

    ``delta`` lists the folds this socket will get as ``slot_fold`` deltas, and is
    present only for a socket that declared :data:`FOLD_APPLY_CAPABILITY`. A tab reads
    its absence as "this gateway sends whole values", which is also what a gateway
    from before the key answers.
    """
    payload: dict[str, Any] = {"v": 1, "folds": ["work"]}
    if ws is not None and ws.get(FOLD_APPLY_WS_FLAG, False):
        payload["delta"] = ["work"]
    return payload


def board_key(slot: str) -> str:
    """*slot* in the form a dashboard tab names its board by.

    A fold names its board by session key (``dashboard:chat-7``,
    ``discord:agent:direct:123``) while a tab names it by slot key (``chat-7``,
    ``discord_agent_direct_123``), the fold the browser's ``slotKey`` applies. Both
    sides of a watch match go through this so the two spellings meet. The same rule
    the artifact store uses to reconcile the two (``_strip_session_scope``).
    """
    try:
        from kiro_crew.artifact_store.rules import _strip_session_scope

        return _strip_session_scope(slot)
    except Exception:
        return slot


def _default_slot_creator(slot: str) -> str:
    """The slot that created *slot*, from the crew log's session tree. No I/O."""
    try:
        from kiro_crew.crew_log import session_tree_projection

        return str(session_tree_projection.creator_of(slot) or "")
    except Exception:
        return ""


#: Unsent bytes above which the hub checks whether a tab still reads. A fan-out
#: send writes its whole frame into the transport before it waits on drain, and
#: each send is its own task, so a burst can take a tab that still reads past
#: this. A tab that stays connected but stops reading passes it after about 70
#: frames of 64 KiB.
WS_MAX_BUFFERED_BYTES = 4 * 1024 * 1024

#: Seconds a tab may stay over ``WS_MAX_BUFFERED_BYTES`` with its buffer not
#: shrinking before the hub drops it. A tab that reads drains between sends; one
#: that stopped reading never does. Dropping it makes the dashboard reconnect and
#: resync from a fresh snapshot. Applied only where the level shows reading
#: (``level_is_progress_signal``), so not on the proactor loop Windows runs.
WS_STALL_SECONDS = 5.0

#: Unsent bytes past which a tab is dropped at once, whatever its window, so the
#: memory one tab can hold stays bounded.
WS_HARD_MAX_BUFFERED_BYTES = 4 * WS_MAX_BUFFERED_BYTES

#: Key on a tab's ``WebSocketResponse``: (when its window started, its buffer size
#: at the last check), kept while the buffer is over ``WS_MAX_BUFFERED_BYTES``.
_OVER_LIMIT_KEY = "_ws_over_limit"

#: Seconds an awaited owner send (``deliver_ws_owners``) may wait while its tab's
#: buffer does not shrink. The send returns once that buffer drains, so how long it
#: takes is the tab's backlog over its link speed: a tab on a slow link that still
#: reads keeps shrinking its buffer, and one that stopped reading never does. The
#: same window as ``WS_STALL_SECONDS``, and like it applied only where the level
#: shows reading (``level_is_progress_signal``).
WS_OWNER_SEND_TIMEOUT_S = WS_STALL_SECONDS

#: Seconds one awaited owner send may take in all, however its tab's buffer moves,
#: so a tab that reads too slowly to finish cannot hold its caller past it. Where
#: the level shows no reading (the proactor loop, or a transport that reports no
#: level), this is the only limit on the send.
WS_OWNER_SEND_MAX_S = 30.0

#: Seconds between two reads of the buffer level of a tab an owner send waits on.
_OWNER_SEND_POLL_S = 0.25


def _write_buffer_size(ws: web.WebSocketResponse) -> int | None:
    """Bytes the tab's transport still holds, or None when that is unknown."""
    transport = getattr(getattr(ws, "_req", None), "transport", None)
    size_of = getattr(transport, "get_write_buffer_size", None)
    size = size_of() if callable(size_of) else None
    return size if isinstance(size, int) else None


class WebSocketHubOwner(Protocol):
    """The mutable facade-owned state the hub operates on.

    These collections intentionally remain owned by ``DashboardState``. Existing
    handlers and tests inspect or replace them directly, so copying them into the
    hub would create two registries and make disconnect cleanup depend on which
    reference a caller happened to mutate.
    """

    _ws_clients: list[web.WebSocketResponse]
    _owner_ws_clients: set[web.WebSocketResponse]
    _ws_log_subscribers: set[web.WebSocketResponse]
    _ws_subagent_subscribers: set[web.WebSocketResponse]
    _background_tasks: set[asyncio.Task[Any]]
    _flush_task: asyncio.Task[Any] | None


#: The ``data`` of a fan-out frame. A mapping, not ``object``: an unawaited async
#: payload builder hands the fan-out a coroutine ``json.dumps`` cannot serialize,
#: and with ``object`` that mistake type-checked at every call site.
WsPayload = Mapping[str, Any]


class WebSocketHub:
    """Coordinate WebSocket clients without owning dashboard domain state.

    The owner and providers are injected so this module never imports the state
    facade. Providers are resolved at call time, preserving monkeypatch seams and
    the serving-loop value bound after construction.
    """

    def __init__(
        self,
        owner: WebSocketHubOwner,
        *,
        serving_loop_provider: Callable[[], asyncio.AbstractEventLoop | None],
        logger_provider: Callable[[], logging.Logger],
        scope_state_provider: Callable[[], Any] | None = None,
        running_loop_provider: Callable[[], asyncio.AbstractEventLoop | None] | None = None,
        slot_creator_provider: Callable[[str], str] | None = None,
    ) -> None:
        self._owner = owner
        self._serving_loop_provider = serving_loop_provider
        self._logger_provider = logger_provider
        self._scope_state_provider: Callable[[], Any] = (
            scope_state_provider if scope_state_provider is not None else lambda: owner
        )
        self._running_loop_provider = running_loop_provider or self._running_loop
        self._slot_creator: Callable[[str], str] = slot_creator_provider or _default_slot_creator
        # Called with no argument whenever which owner sockets are capable, or what
        # they watch, may have changed. The crew-log exporter re-derives its bus
        # subscriptions from :meth:`fold_watch_routing` there.
        self._fold_watch_listeners: list[Callable[[], None]] = []

    @property
    def _log(self) -> logging.Logger:
        return self._logger_provider()

    def _owner_method(self, name: str, fallback: Callable[..., Any]) -> Callable[..., Any]:
        """Resolve a facade seam at call time, falling back for standalone use.

        Dashboard tests and integrations replace several of these methods on an
        individual ``DashboardState`` instance. Looking them up for each fan-out
        keeps those seams live after the implementation moves behind this hub.
        The normal facade wrappers delegate back to the matching hub method, so
        the lookup does not transfer ownership of any collection.
        """
        method = getattr(self._owner, name, None)
        return method if callable(method) else fallback

    @staticmethod
    def _running_loop() -> asyncio.AbstractEventLoop | None:
        """Return the running loop, or None when called off the event loop."""
        try:
            return asyncio.get_running_loop()
        except RuntimeError:
            return None

    def _spawn_ws_send(self, ws: web.WebSocketResponse, msg: str) -> None:
        """Fire-and-forget a WS send while retaining a strong task reference.

        A fan-out may originate on a worker thread. In that case the send hops to
        the dashboard serving loop and creates its coroutine there. Only a
        synchronous refusal from ``send_str`` escapes; scheduling failures are a
        process condition and must not unregister an otherwise healthy peer.
        """
        loop = self._running_loop_provider()
        if loop is None:
            target = self._serving_loop_provider()
            if target is not None and not target.is_closed():
                try:
                    spawn = self._owner_method("_spawn_ws_send", self._spawn_ws_send)
                    target.call_soon_threadsafe(spawn, ws, msg)
                    return
                except RuntimeError:
                    self._log.debug("WS send: serving loop is shutting down")
            # Still call send_str so a synchronous peer refusal reaches the
            # fan-out. Close a returned coroutine because there is no loop on
            # which it can run.
            coro = ws.send_str(msg)
            close = getattr(coro, "close", None)
            if callable(close):
                close()
            self._log.debug("WS send dropped: no serving loop to run it on")
            return

        # Resolve the provider on-loop as well. DashboardState's provider latches
        # this loop only when startup has not already bound an authoritative one.
        self._serving_loop_provider()
        if self._drop_if_stalled(ws):
            return
        task = asyncio.ensure_future(ws.send_str(msg))
        self._owner._background_tasks.add(task)
        done = self._owner_method("_on_ws_send_done", self._on_ws_send_done)
        task.add_done_callback(done)

    def _drop_if_stalled(self, ws: web.WebSocketResponse) -> bool:
        """Drop a tab that stopped reading.

        Runs on the serving loop, before a send. A tab that stays connected but
        stops reading keeps every frame in its transport's write buffer, and that
        buffer never shrinks. A tab that reads can pass ``WS_MAX_BUFFERED_BYTES``
        after a burst, but its buffer shrinks as it reads. So a tab is dropped
        when its buffer is over the limit and has not shrunk for
        ``WS_STALL_SECONDS``, and at once when it passes
        ``WS_HARD_MAX_BUFFERED_BYTES``. A shrink, or a buffer at or under the
        limit, restarts the window.

        The window needs a level that falls as the tab reads
        (:func:`kiro_crew.write_buffer_level.level_is_progress_signal`). The
        proactor loop Windows runs gives none: its level is the overlapped send in
        flight plus the bytes queued behind it, flat while a tab that reads takes
        one large send. There only the ceiling and the owner-send cap
        (``WS_OWNER_SEND_MAX_S``) drop a tab.
        """
        size = _write_buffer_size(ws)
        if size is None:
            return False
        if size <= WS_MAX_BUFFERED_BYTES:
            ws.pop(_OVER_LIMIT_KEY, None)
            return False
        if size <= WS_HARD_MAX_BUFFERED_BYTES:
            if not level_is_progress_signal():
                return False
            now = time.monotonic()
            mark = ws.get(_OVER_LIMIT_KEY)
            if not isinstance(mark, tuple) or size < mark[1]:
                ws[_OVER_LIMIT_KEY] = (now, size)
                return False
            ws[_OVER_LIMIT_KEY] = (mark[0], size)
            if now - mark[0] < WS_STALL_SECONDS:
                return False
        self._log.warning(
            "WS client stopped reading: %d bytes buffered (limit %d, hard limit %d); dropping it",
            size,
            WS_MAX_BUFFERED_BYTES,
            WS_HARD_MAX_BUFFERED_BYTES,
        )
        self._drop_ws(ws)
        return True

    def _drop_ws(self, ws: web.WebSocketResponse) -> None:
        """Unregister a tab and abort its connection.

        ``abort()`` discards the bytes buffered for the peer and fires
        ``connection_lost``, which ends every send waiting on the buffer to
        drain. A ``close()`` would wait for a peer that is not reading.
        """
        remove = self._owner_method("_remove_ws", self._remove_ws)
        remove(ws)
        transport = getattr(getattr(ws, "_req", None), "transport", None)
        abort = getattr(transport, "abort", None)
        if callable(abort):
            abort()

    def _on_ws_send_done(self, task: asyncio.Task[Any]) -> None:
        """Release a completed send task and surface asynchronous failures."""
        self._owner._background_tasks.discard(task)
        if task.cancelled():
            return
        exc = task.exception()
        if exc is not None:
            self._log.debug("WS send failed (client likely disconnected): %s", exc)

    def _ws_client_allowed(
        self,
        ws: web.WebSocketResponse,
        msg_type: str,
        data: object,
    ) -> bool:
        """Apply the deny-by-default event-scope gate for one client."""
        # Checked BEFORE the dashboard-user short-circuit, and gated on a plain
        # dict lookup because this predicate runs for every frame on every socket:
        # the mark is absent for all but a socket still awaiting its
        # members_subscribed baseline, so the common path costs one `get`.
        #
        # A projection delivered before that baseline is worse than a projection
        # delayed: the baseline's lastSeqs is computed before the append, so the
        # client's prune rule reads the newer row as stale and deletes it, and
        # nothing corrects it until that slug next changes. Hold it back here and
        # record the slug; send_members_subscribed replays the current value once
        # the baseline is out.
        pending = ws.get("_members_baseline_pending")
        if pending is not None:
            from kiro_crew.eventlog import types as eventlog_types

            if msg_type == eventlog_types.WS_MEMBER_PROJECTION:
                slug = data.get("slug") if isinstance(data, dict) else None
                if isinstance(slug, str) and slug:
                    pending.add(slug)
                return False
        if ws.get("_is_dashboard_user", False):
            # The per-member event log is the owner's view of the crew, the
            # boundary the ``/api/members`` reads apply. A non-owner dashboard
            # session (a Telegram, Teams or Webex allowlist link) is a dashboard
            # user too, so the owner set decides: ``register_ws`` fills it from
            # ``is_owner_dashboard_request``.
            from kiro_crew.dashboard.ws_event_scope import MEMBER_LOG_EVENTS

            if msg_type in MEMBER_LOG_EVENTS and ws not in (
                getattr(self._owner, "_owner_ws_clients", None) or set()
            ):
                try:
                    from kiro_crew.dashboard.ws_event_scope import (
                        DASHBOARD_USER_AUDITEE,
                        _audit_deny,
                    )

                    _audit_deny(DASHBOARD_USER_AUDITEE, msg_type, "owner_only")
                except Exception:
                    self._log.debug(
                        "state: SEL audit for non-owner deny %s failed",
                        msg_type,
                        exc_info=True,
                    )
                return False
            # Granted, and the grant is a permission decision like any other:
            # ``AUTOSDE.yaml`` wants an SEL record for it, not only for the
            # refusals below. Recorded HERE, at the chokepoint every dashboard
            # frame passes (live broadcast via ``_send_ws_all``, the log
            # fan-out's per-send recheck, and the subagent reconnect replay in
            # ``ws.py`` all funnel through this predicate), rather than at each
            # sender, so a send path added later is covered by construction --
            # the same reasoning ``ws_event_allowed`` gives for app tokens.
            # Under a reserved auditee rather than the empty app claim, so the
            # operator can tell their own socket's grants from an unnamed
            # app's. Never raises: a failing audit sink must not withhold a
            # frame the owner is entitled to.
            try:
                from kiro_crew.dashboard.ws_event_scope import (
                    DASHBOARD_USER_AUDITEE,
                    _audit_allow,
                )

                _audit_allow(DASHBOARD_USER_AUDITEE, msg_type)
            except Exception:
                self._log.debug(
                    "state: SEL audit for dashboard-user grant %s failed",
                    msg_type,
                    exc_info=True,
                )
            return True
        ws_app: str = ws.get("_app", "")
        snapshot: frozenset[str] = ws.get("_allowed_events", frozenset())
        data_dict: dict[Any, Any] = data if isinstance(data, dict) else {}
        try:
            from kiro_crew.dashboard.ws_event_scope import (
                _audit_deny,
                effective_allowed_events,
                ws_event_allowed,
            )

            allowed = effective_allowed_events(ws_app, snapshot)
            return ws_event_allowed(
                msg_type,
                data_dict,
                app=ws_app,
                allowed_events=allowed,
                state=self._scope_state_provider(),
            )
        except Exception:
            try:
                _audit_deny(ws_app or "<unknown>", msg_type, "scope_check_exception")
            except Exception as inner_exc:
                self._log.debug(
                    "state: audit for scope_check_exception failed for %s/%s: %s",
                    ws_app,
                    msg_type,
                    inner_exc,
                )
            return False

    def _serialize_for_client(
        self,
        ws: web.WebSocketResponse,
        msg_type: str,
        data: object,
        default_msg: str,
    ) -> str:
        """Return a payload filtered for one dashboard or app client."""
        if ws.get("_is_dashboard_user", False):
            return default_msg
        if msg_type in ("subagent_batch_update", "subagent_batch_chunks"):
            serialize_batch = self._owner_method(
                "_serialize_subagent_batch", self._serialize_subagent_batch
            )
            return serialize_batch(ws, msg_type, data, default_msg)
        if msg_type != "slots":
            return default_msg
        if not isinstance(data, dict) or "slots" not in data:
            return default_msg

        snapshot: frozenset[str] = ws.get("_allowed_events", frozenset())
        ws_app: str = ws.get("_app", "")
        try:
            from kiro_crew.dashboard.ws_event_scope import (
                effective_allowed_events,
                filter_slots_for_app,
                slots_envelope_extras,
            )

            allowed = effective_allowed_events(ws_app, snapshot)
            scope_state = self._scope_state_provider()
            filtered = filter_slots_for_app(data["slots"], ws_app, allowed, scope_state)
            extras = slots_envelope_extras(allowed, yolo=bool(data.get("yolo", False)))
        except Exception:
            # The safe fallback is an empty slot list with no global posture
            # fields; defaulting those fields to false still reveals state.
            filtered = []
            extras = {}
        return json.dumps({"type": "slots", "data": filtered, **extras})

    def _serialize_subagent_batch(
        self,
        ws: web.WebSocketResponse,
        msg_type: str,
        data: object,
        default_msg: str,
    ) -> str:
        """Filter every item in a coalesced subagent frame for one app."""
        from kiro_crew.dashboard.ws_event_scope import (
            _SUBAGENT_BATCH_ITEM_KEY,
            filter_subagent_batch_for_app,
        )

        key = _SUBAGENT_BATCH_ITEM_KEY.get(msg_type, "")
        if not key or not isinstance(data, dict) or not isinstance(data.get(key), list):
            return json.dumps({"type": msg_type, "data": {key or "items": []}})
        snapshot: frozenset[str] = ws.get("_allowed_events", frozenset())
        ws_app: str = ws.get("_app", "")
        try:
            from kiro_crew.dashboard.ws_event_scope import effective_allowed_events

            allowed = effective_allowed_events(ws_app, snapshot)
            items = filter_subagent_batch_for_app(
                data[key],
                ws_app,
                allowed,
                self._scope_state_provider(),
                msg_type=msg_type,
            )
        except Exception:
            # Preserve the facade's historical ``_log`` seam when a harness or
            # embedding supplies it; ordinary DashboardState instances fall
            # back to the module logger provider used by every other hub path.
            batch_log = getattr(self._owner, "_log", self._log)
            batch_log.warning("subagent batch filter failed; dropping items", exc_info=True)
            items = []
        return json.dumps({"type": msg_type, "data": {key: items}})

    def _send_ws_all(self, msg_type: str, data: object, msg: str) -> None:
        """Send one typed frame through the per-client authorization chokepoint."""
        dead: list[web.WebSocketResponse] = []
        skip_owners = msg_type == "slots"
        # A legacy-only slots frame repeats a change every patch-capable socket
        # already applied from its ``slot_patch`` frame.
        skip_patch_clients = (
            skip_owners and isinstance(data, dict) and bool(data.get("_legacy_only", False))
        )
        owners = getattr(self._owner, "_owner_ws_clients", None) or set()
        client_allowed = self._owner_method("_ws_client_allowed", self._ws_client_allowed)
        serialize = self._owner_method("_serialize_for_client", self._serialize_for_client)
        spawn = self._owner_method("_spawn_ws_send", self._spawn_ws_send)
        remove = self._owner_method("_remove_ws", self._remove_ws)
        for ws in list(self._owner._ws_clients):
            if ws.closed:
                dead.append(ws)
                continue
            if skip_owners and ws in owners:
                continue
            if skip_patch_clients and ws.get(SLOT_PATCH_WS_FLAG, False):
                continue
            if not client_allowed(ws, msg_type, data):
                continue
            try:
                payload = serialize(ws, msg_type, data, msg)
            except Exception:
                # Payload shaping is our failure, not evidence that the peer is
                # dead. Keep the registration so later frames can recover.
                self._log.warning(
                    "WS payload shaping failed for %s; keeping the client registered",
                    msg_type,
                    exc_info=True,
                )
                continue
            try:
                spawn(ws, payload)
            except Exception:
                # Only a synchronous send_str refusal reaches here.
                dead.append(ws)
        for ws in dead:
            remove(ws)

    def _send_ws_owners(self, msg: str, *, skip_slot_patch_clients: bool = False) -> None:
        """Send a pre-serialized message only to owner-authenticated clients."""
        dead: list[web.WebSocketResponse] = []
        spawn = self._owner_method("_spawn_ws_send", self._spawn_ws_send)
        remove = self._owner_method("_remove_ws", self._remove_ws)
        for ws in list(self._owner._owner_ws_clients):
            if ws.closed:
                dead.append(ws)
                continue
            if skip_slot_patch_clients and ws.get(SLOT_PATCH_WS_FLAG, False):
                continue
            try:
                spawn(ws, msg)
            except Exception:
                dead.append(ws)
        for ws in dead:
            remove(ws)

    def send_ws_slot_patch(self, msg: str) -> int:
        """Send a pre-serialized ``slot_patch`` frame to patch-capable sockets.

        Returns how many sockets it was handed to. Only dashboard-user sockets
        are ever flagged (see ``api_ws``), so no scope gate applies here: the
        frame carries fields of the dashboard-user slot projection, which those
        sockets already receive in full.
        """
        dead: list[web.WebSocketResponse] = []
        sent = 0
        spawn = self._owner_method("_spawn_ws_send", self._spawn_ws_send)
        remove = self._owner_method("_remove_ws", self._remove_ws)
        for ws in list(self._owner._ws_clients):
            if ws.closed:
                dead.append(ws)
                continue
            if not ws.get(SLOT_PATCH_WS_FLAG, False):
                continue
            try:
                spawn(ws, msg)
                sent += 1
            except Exception:
                dead.append(ws)
        for ws in dead:
            remove(ws)
        return sent

    def broadcast_ws(self, msg_type: str, data: WsPayload) -> None:
        """Send a typed message to every authorized WS client."""
        if not self._owner._ws_clients:
            return
        msg = json.dumps({"type": msg_type, "data": data})
        send_all = self._owner_method("_send_ws_all", self._send_ws_all)
        send_all(msg_type, data, msg)

    async def _await_owner_send(self, ws: web.WebSocketResponse, msg: str) -> None:
        """Send ``msg`` to one owner tab and wait for as long as the tab reads.

        The send returns once the tab's buffer drains, so how long it takes is the
        tab's backlog over its link speed, not a sign that the tab stopped reading.
        While it waits, the buffer level is read every ``_OWNER_SEND_POLL_S``. The
        level rises only as frames are written and falls only as the tab reads, so
        a fall between two reads is the tab reading, whenever this frame itself
        reached the buffer. The wait raises ``TimeoutError`` once the level has not
        fallen for ``WS_OWNER_SEND_TIMEOUT_S``, the signal ``_drop_if_stalled``
        reads, or once the send has taken ``WS_OWNER_SEND_MAX_S`` in all. Where the
        level shows no reading (``level_is_progress_signal``, or a transport that
        reports no level), only ``WS_OWNER_SEND_MAX_S`` ends it.
        """
        send = asyncio.ensure_future(ws.send_str(msg))
        try:
            loop = asyncio.get_running_loop()
            watch = level_is_progress_signal()
            started = moved = loop.time()
            last = _write_buffer_size(ws) if watch else None
            while True:
                done, _pending = await asyncio.wait({send}, timeout=_OWNER_SEND_POLL_S)
                if done:
                    send.result()
                    return
                now = loop.time()
                size = _write_buffer_size(ws) if watch else None
                if size is not None and last is not None and size < last:
                    moved = now
                last = size
                if size is not None and now - moved >= WS_OWNER_SEND_TIMEOUT_S:
                    raise TimeoutError(
                        f"its buffer did not shrink for {WS_OWNER_SEND_TIMEOUT_S:g} s"
                    )
                if now - started >= WS_OWNER_SEND_MAX_S:
                    raise TimeoutError(f"it did not complete in {WS_OWNER_SEND_MAX_S:g} s")
        finally:
            if not send.done():
                send.cancel()

    async def deliver_ws_owners(self, msg_type: str, data: WsPayload) -> int:
        """Await owner-only sends and return the number that completed.

        Each send waits under its own bound (``_await_owner_send``): a tab that
        stays connected but stopped reading never completes a send that waits for
        its buffer to drain. Such a tab counts as not delivered and is dropped, so
        the call returns for every other tab. A tab that still reads keeps its
        send however slow its link, up to ``WS_OWNER_SEND_MAX_S``.
        """
        targets = [
            ws
            for ws in list(self._owner._owner_ws_clients)
            if not ws.closed and not self._drop_if_stalled(ws)
        ]
        if not targets:
            return 0
        msg = json.dumps({"type": msg_type, "data": data})
        results = await asyncio.gather(
            *(self._await_owner_send(ws, msg) for ws in targets),
            return_exceptions=True,
        )
        delivered = 0
        remove = self._owner_method("_remove_ws", self._remove_ws)
        for ws, result in zip(targets, results):
            if isinstance(result, TimeoutError):
                self._log.warning("Owner WS send ended: %s; dropping the client", result)
                self._drop_ws(ws)
            elif isinstance(result, BaseException):
                self._log.debug("Owner WS send failed (client likely disconnected): %s", result)
                remove(ws)
            else:
                delivered += 1
        for ws in list(self._owner._owner_ws_clients):
            if ws.closed:
                remove(ws)
        return delivered

    def broadcast_ws_owners(self, msg_type: str, data: WsPayload) -> None:
        """Send a typed message only to owner-authorized clients."""
        if not getattr(self._owner, "_owner_ws_clients", None):
            return
        msg = json.dumps({"type": msg_type, "data": data})
        if msg_type == SLOT_FOLD_FRAME and self._any_fold_capable():
            slot = data.get("slot")
            if isinstance(slot, str) and slot:
                self._send_slot_fold(msg, slot)
                return
        send_owners = self._owner_method("_send_ws_owners", self._send_ws_owners)
        send_owners(msg)

    # -- per-tab fold watch ------------------------------------------------- #

    def _any_fold_capable(self) -> bool:
        """Whether any open owner socket declared :data:`FOLD_DELTA_CAPABILITY`."""
        return any(
            not ws.closed and ws.get(FOLD_DELTA_WS_FLAG, False)
            for ws in list(self._owner._owner_ws_clients)
        )

    def broadcast_slot_fold(self, slot: str, legacy: WsPayload, delta: "WsPayload | None") -> None:
        """Send one board's ``work`` advance in the form each owner socket reads.

        *legacy* is the ``slot_projection`` data every socket has always read; *delta*
        is the ``slot_fold`` data, sent INSTEAD of it to each socket that declared
        :data:`FOLD_APPLY_CAPABILITY` and watches the board. ``None`` (or no such socket
        open) sends exactly what :meth:`broadcast_ws_owners` sends. Each frame is
        serialized at most once.
        """
        if not getattr(self._owner, "_owner_ws_clients", None):
            return
        apply_open = delta is not None and any(
            not ws.closed and ws.get(FOLD_APPLY_WS_FLAG, False)
            for ws in list(self._owner._owner_ws_clients)
        )
        if not apply_open:
            self.broadcast_ws_owners(SLOT_FOLD_FRAME, legacy)
            return
        msg = json.dumps({"type": SLOT_FOLD_FRAME, "data": legacy})
        apply_msg = json.dumps({"type": SLOT_DELTA_FRAME, "data": delta})
        self._send_slot_fold(msg, slot, apply_msg)

    def set_fold_apply_capable(self, ws: web.WebSocketResponse, capable: bool) -> bool:
        """Record whether *ws* declared :data:`FOLD_APPLY_CAPABILITY`; returns the flag.

        Only a socket already carrying :data:`FOLD_DELTA_WS_FLAG` can: the delta frame
        goes only to the boards a socket watches, and only a watching socket has a set.
        """
        flag = bool(capable) and bool(ws.get(FOLD_DELTA_WS_FLAG, False))
        ws[FOLD_APPLY_WS_FLAG] = flag
        return flag

    def _send_slot_fold(self, msg: str, slot: str, apply_msg: "str | None" = None) -> None:
        """Send one board's slot frame: every legacy owner socket, and each capable
        one that watches *slot* or one of its ancestors. A watching socket that applies
        deltas gets *apply_msg* instead of *msg* when one is given."""
        dead: list[web.WebSocketResponse] = []
        spawn = self._owner_method("_spawn_ws_send", self._spawn_ws_send)
        remove = self._owner_method("_remove_ws", self._remove_ws)
        chain: frozenset[str] | None = None
        for ws in list(self._owner._owner_ws_clients):
            if ws.closed:
                dead.append(ws)
                continue
            if ws.get(FOLD_DELTA_WS_FLAG, False):
                watched = ws.get(FOLD_WATCH_WS_KEY) or frozenset()
                if not watched:
                    continue
                if chain is None:
                    chain = self.board_chain(slot)
                if watched.isdisjoint(chain):
                    continue
            try:
                if apply_msg is not None and ws.get(FOLD_APPLY_WS_FLAG, False):
                    spawn(ws, apply_msg)
                else:
                    spawn(ws, msg)
            except Exception:
                dead.append(ws)
        for ws in dead:
            remove(ws)

    def board_chain(self, slot: str) -> frozenset[str]:
        """:meth:`slot_ancestry` of *slot*, each in its :func:`board_key` form.

        Both spellings of every slot, so a creator recorded under either one still
        matches a watch set that holds board keys.
        """
        chain: set[str] = set()
        for name in self.slot_ancestry(slot):
            chain.add(name)
            chain.add(board_key(name))
        return frozenset(chain)

    def slot_ancestry(self, slot: str) -> list[str]:
        """*slot* followed by each slot that created the one before it, root last."""
        chain: list[str] = []
        current = slot
        while current and current not in chain and len(chain) <= _MAX_ANCESTOR_DEPTH:
            chain.append(current)
            try:
                current = self._slot_creator(current)
            except Exception:
                break
        return chain

    def add_fold_watch_listener(self, listener: Callable[[], None]) -> Callable[[], None]:
        """Call *listener* whenever fold routing may have changed; returns a remover."""
        self._fold_watch_listeners.append(listener)

        def remove() -> None:
            try:
                self._fold_watch_listeners.remove(listener)
            except ValueError:
                pass

        return remove

    def _fold_watch_changed(self) -> None:
        for listener in list(self._fold_watch_listeners):
            try:
                listener()
            except Exception:
                self._log.debug("fold watch listener failed", exc_info=True)

    def set_fold_delta_capable(self, ws: web.WebSocketResponse, capable: bool) -> bool:
        """Record whether *ws* declared :data:`FOLD_DELTA_CAPABILITY`; returns the flag.

        Only a dashboard-user socket can carry it, like ``slot_patch``. A socket that
        turns capable starts with an empty watch set, so it receives no slot frame
        until its tab sends ``fold_watch``.
        """
        flag = bool(capable) and bool(ws.get("_is_dashboard_user", False))
        ws[FOLD_DELTA_WS_FLAG] = flag
        ws[FOLD_WATCH_WS_KEY] = frozenset()
        self._fold_watch_changed()
        return flag

    def set_fold_watch(self, ws: web.WebSocketResponse, slots: object) -> frozenset[str]:
        """REPLACE *ws*'s watch set with *slots*; returns what was kept.

        Entries that are not a non-empty string of at most
        :data:`MAX_FOLD_WATCH_KEY_LEN` characters are ignored, as is everything past
        :data:`MAX_FOLD_WATCH_SLOTS`. A slot nobody publishes is harmless: it matches
        no frame. A socket without the capability keeps no watch set at all, since it
        receives every board anyway.
        """
        if not ws.get(FOLD_DELTA_WS_FLAG, False):
            return frozenset()
        kept: list[str] = []
        if isinstance(slots, (list, tuple)):
            for slot in slots:
                if len(kept) >= MAX_FOLD_WATCH_SLOTS:
                    break
                if (
                    isinstance(slot, str)
                    and slot
                    and len(slot) <= MAX_FOLD_WATCH_KEY_LEN
                    and slot.isprintable()
                    and board_key(slot) not in kept
                ):
                    kept.append(board_key(slot))
        watched = frozenset(kept)
        if ws.get(FOLD_WATCH_WS_KEY) != watched:
            ws[FOLD_WATCH_WS_KEY] = watched
            self._fold_watch_changed()
        return watched

    def fold_watchers(self, slot: str) -> list[web.WebSocketResponse]:
        """The capable owner sockets that watch *slot* or one of its ancestors."""
        chain = self.board_chain(slot)
        return [
            ws
            for ws in list(self._owner._owner_ws_clients)
            if not ws.closed
            and ws.get(FOLD_DELTA_WS_FLAG, False)
            and not (ws.get(FOLD_WATCH_WS_KEY) or frozenset()).isdisjoint(chain)
        ]

    def fold_watch_routing(self) -> tuple[bool, bool, frozenset[str]]:
        """``(legacy_open, capable_open, watched)`` over the open owner sockets.

        *watched* is the union of every capable socket's watch set. The exporter
        keeps its wildcard bus subscription unless a capable socket is open and no
        legacy one is.
        """
        legacy = capable = False
        watched: set[str] = set()
        for ws in list(self._owner._owner_ws_clients):
            if ws.closed:
                continue
            if ws.get(FOLD_DELTA_WS_FLAG, False):
                capable = True
                watched.update(ws.get(FOLD_WATCH_WS_KEY) or ())
            else:
                legacy = True
        return legacy, capable, frozenset(watched)

    def fold_apply_watch(self) -> frozenset[str]:
        """The union of the watch sets of the open sockets that apply ``slot_fold`` deltas.

        Those are the only sockets a ``work`` delta is sent to, so a board outside this
        set (and outside every board created under one in it) needs no delta base.
        """
        watched: set[str] = set()
        for ws in list(self._owner._owner_ws_clients):
            if not ws.closed and ws.get(FOLD_APPLY_WS_FLAG, False):
                watched.update(ws.get(FOLD_WATCH_WS_KEY) or ())
        return frozenset(watched)

    def ws_client_count(self) -> int:
        return len(self._owner._ws_clients)

    def dashboard_user_ws_count(self) -> int:
        """Count open sockets belonging to a dashboard USER, not an app token.

        ``ws_client_count`` counts every ``/api/ws`` registration, and an app
        token is one of them (``_is_dashboard_user`` False, set from the auth
        middleware in ``dashboard/ws.py``). Such a socket does not receive an
        owner-surface frame unless its manifest declared that event -- the same
        ``_is_dashboard_user`` gate ``_ws_client_allowed`` applies -- so a caller
        asking "is a human watching?" must not count it.

        Closed-but-not-yet-pruned sockets are skipped: the registry prunes
        lazily, on the next broadcast.
        """
        return sum(
            1
            for ws in list(self._owner._ws_clients)
            if not ws.closed and ws.get("_is_dashboard_user", False)
        )

    def broadcast_browser_event(self, event_type: str, data: dict[str, Any]) -> None:
        """Redact and broadcast a browser activity event."""
        safe_data: dict[str, Any] = {}
        for key, value in data.items():
            if isinstance(value, str):
                from kiro_crew.security import redact

                value = redact(value)
            safe_data[key] = value
        payload: dict[str, Any] = {
            "type": "browser_event",
            "event": event_type,
            "ts": time.time(),
        }
        for key, value in safe_data.items():
            if key not in ("type", "event", "ts"):
                payload[key] = value
        broadcast = self._owner_method("broadcast_ws", self.broadcast_ws)
        broadcast("browser_event", payload)

    def register_ws(self, ws: web.WebSocketResponse, *, owner: bool = False) -> None:
        """Register a client and latch the serving loop before its first frame."""
        self._owner._ws_clients.append(ws)
        if owner:
            self._owner._owner_ws_clients.add(ws)
        self._serving_loop_provider()
        if owner and self._fold_watch_listeners:
            # A new owner socket is legacy until it is flagged capable.
            self._fold_watch_changed()

    async def send_members_subscribed(self, ws: web.WebSocketResponse) -> None:
        """Send the one-shot ``members_subscribed`` frame to a NEW owner socket.

        Carries ``{"lastSeqs": {slug: last_seq}}`` from the per-member event-log
        service, so the client can drop any held member_projection frame whose
        seq is newer than this baseline (a replay/stale-frame guard). Sent to
        THIS socket alone, right after the connect-time snapshot. The snapshot is
        the first frame a socket receives by contract, so the baseline cannot
        precede it: ``test_chat_send_echo_scope`` reads that first frame and treats
        its arrival as proof the socket is registered for echoes.

        THE WINDOW IS CLOSED BY SUPPRESSION, NOT BY ORDERING: the read below awaits
        a thread and the socket is already registered for owner broadcasts, so a
        ``member_projection`` append landing in that window would be delivered
        ahead of a baseline computed before it and the client would prune the newer
        row. So the socket is marked pending before that await, and
        ``_ws_client_allowed`` -- the predicate the fan-out already consults per
        socket -- holds ``member_projection`` back from a marked socket and records
        its slug. Once the baseline is out the mark is cleared and each recorded
        slug's CURRENT projection is replayed. Reordering instead is not available:
        the connect snapshot must stay the first frame a socket receives.

        Owner-only: the caller must gate on a dashboard-user connection and skip
        app-token connections (``member_projection`` / ``members_subscribed`` are
        classified owner-only in ``ws_event_scope``). Best-effort — a serialize
        or send fault is logged and swallowed so it never fails the connection.
        """
        # Marked BEFORE the await below, because the socket is already in the owner
        # broadcast set: any member_projection published while this coroutine waits
        # would otherwise reach the client ahead of a baseline computed before it,
        # and the client's prune rule deletes the newer row. _ws_client_allowed
        # holds those frames back and records their slugs here; the release at the
        # end of this method sends each one's CURRENT value instead.
        pending: set[str] = set()
        ws["_members_baseline_pending"] = pending
        try:
            from kiro_crew.eventlog.service import get_service

            # last_seqs() iterates and parses every uncached member log on first
            # dashboard connect -- synchronous file I/O that would stall the serving
            # loop. Offload it, matching the sibling _handle_eventlog_frame read.
            last_seqs = await asyncio.to_thread(get_service().last_seqs)
        except Exception:
            self._log.debug("members_subscribed: last_seqs read failed", exc_info=True)
            # Release on the failure path too. A socket left marked is suppressed
            # for the rest of its life, which turns a missed baseline into a
            # permanently blank Members page.
            ws.pop("_members_baseline_pending", None)
            return
        try:
            msg = json.dumps({"type": "members_subscribed", "data": {"lastSeqs": last_seqs}})
            await ws.send_str(msg)
        except Exception:
            self._log.debug("members_subscribed send failed", exc_info=True)
        finally:
            # Cleared before the replay, so the replayed frames are not themselves
            # suppressed, and so a fault in the replay cannot leave the socket
            # permanently held back.
            ws.pop("_members_baseline_pending", None)
        await self._replay_suppressed_projections(ws, pending)

    async def _replay_suppressed_projections(
        self, ws: web.WebSocketResponse, slugs: set[str]
    ) -> None:
        """Send each held-back slug's CURRENT projection to one socket.

        Whole-value frames plus the client's higher-seq-wins rule make the replay
        safe to run after the baseline: a value newer than what was suppressed is
        simply the one the client keeps.

        Routed through the service's ``redacted_snapshot`` rather than serializing
        ``snapshot`` directly, because the broadcast redacts every projection before
        it leaves the process and a second egress path that skipped that would put
        an unredacted view on the wire.
        """
        if not slugs:
            return
        try:
            from kiro_crew.dashboard.ws_event_scope import DASHBOARD_USER_AUDITEE, _audit_allow
            from kiro_crew.eventlog import types as eventlog_types
            from kiro_crew.eventlog.service import get_service

            # These frames bypass ``_ws_client_allowed``, which records the grant
            # for broadcast frames, so the replay records its own.
            try:
                _audit_allow(DASHBOARD_USER_AUDITEE, eventlog_types.WS_MEMBER_PROJECTION)
            except Exception:
                self._log.debug("members_subscribed: replay grant audit failed", exc_info=True)
            service = get_service()
            for slug in sorted(slugs):
                snap = await asyncio.to_thread(service.redacted_snapshot, slug)
                seq = snap.get("asOfSeq", -1)
                for key, view in (snap.get("values") or {}).items():
                    # Same type constant and same payload keys as the service's own
                    # broadcast, so a change to the frame moves both together.
                    frame = {
                        "type": eventlog_types.WS_MEMBER_PROJECTION,
                        "data": {"slug": slug, "key": key, "value": view, "seq": seq},
                    }
                    await ws.send_str(json.dumps(frame))
        except Exception:
            self._log.debug("members_subscribed: replay failed", exc_info=True)

    def unregister_ws(self, ws: web.WebSocketResponse) -> None:
        remove = self._owner_method("_remove_ws", self._remove_ws)
        remove(ws)

    def _remove_ws(self, ws: web.WebSocketResponse) -> None:
        """Remove a client from the registry and every subscriber subset."""
        try:
            self._owner._ws_clients.remove(ws)
        except ValueError:
            pass
        was_owner = ws in self._owner._owner_ws_clients
        self._owner._owner_ws_clients.discard(ws)
        self._owner._ws_log_subscribers.discard(ws)
        self._owner._ws_subagent_subscribers.discard(ws)
        if was_owner and self._fold_watch_listeners:
            self._fold_watch_changed()

    def subscribe_logs(self, ws: web.WebSocketResponse) -> None:
        self._owner._ws_log_subscribers.add(ws)

    def unsubscribe_logs(self, ws: web.WebSocketResponse) -> None:
        self._owner._ws_log_subscribers.discard(ws)

    def subscribe_subagents(self, ws: web.WebSocketResponse) -> None:
        self._owner._ws_subagent_subscribers.add(ws)

    def unsubscribe_subagents(self, ws: web.WebSocketResponse) -> None:
        self._owner._ws_subagent_subscribers.discard(ws)

    def broadcast_ws_subagent_subscribers(self, msg_type: str, data: WsPayload) -> None:
        """Fan out heavy subagent data only to subscribed, authorized clients."""
        if not self._owner._ws_subagent_subscribers:
            return
        msg = json.dumps({"type": msg_type, "data": data})
        dead: list[web.WebSocketResponse] = []
        client_allowed = self._owner_method("_ws_client_allowed", self._ws_client_allowed)
        serialize = self._owner_method("_serialize_for_client", self._serialize_for_client)
        spawn = self._owner_method("_spawn_ws_send", self._spawn_ws_send)
        remove = self._owner_method("_remove_ws", self._remove_ws)
        for ws in list(self._owner._ws_subagent_subscribers):
            if ws.closed:
                dead.append(ws)
                continue
            if not client_allowed(ws, msg_type, data):
                continue
            try:
                payload = serialize(ws, msg_type, data, msg)
            except Exception:
                self._log.warning(
                    "WS subagent payload shaping failed for %s; keeping the client registered",
                    msg_type,
                    exc_info=True,
                )
                continue
            try:
                spawn(ws, payload)
            except Exception:
                dead.append(ws)
        for ws in dead:
            remove(ws)

    async def close_all_ws(self) -> None:
        """Cancel the flush loop, close sockets in order, then clear registries."""
        if self._owner._flush_task:
            self._owner._flush_task.cancel()
            self._owner._flush_task = None
        for ws in list(self._owner._ws_clients):
            try:
                await ws.close()
            except Exception:
                pass
        self._owner._ws_clients.clear()
        self._owner._owner_ws_clients.clear()
        self._owner._ws_log_subscribers.clear()
        self._owner._ws_subagent_subscribers.clear()
        if self._fold_watch_listeners:
            self._fold_watch_changed()
