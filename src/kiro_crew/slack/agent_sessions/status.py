"""Slack agent-session status and title: ``agents.sessions.setStatus`` / ``rename``.

With ``slack.agent_sessions`` on, a Slack thread the bot answers in is an agent
session: Slack shows "Working..." and a native Stop button while it is
``processing``, a paused state while it is ``suspended`` (a tool approval or a
review draft waiting on a person), and the ready state when it is ``active``.

The existing call sites speak the older ``assistant.threads.setStatus`` dialect:
free status text, where an empty string clears the indicator. This driver keeps
them unchanged and translates (:func:`lifecycle_for_text`), so the transport
renderer, the native handler and the review path all drive the session without
learning a second vocabulary. Slack's loading UX takes no custom text, so the
text only chooses the lifecycle state.

What it adds on top of a plain translation:

* **No stray sessions.** ``setStatus`` creates a session when there is none, so
  ``active`` is only ever sent for a thread this process put into ``processing``
  or ``suspended``; an error path clearing a status it never set creates nothing.
* **Fewer calls.** A tool event re-announces "working" many times a turn; a repeat
  ``processing`` inside the refresh window is not re-sent (``setStatus`` is Tier 3).
* **Long turns stay "Working...".** Slack times ``processing`` out to ``active``
  after an hour, so a keepalive re-sends it every :data:`PROCESSING_REFRESH_SECS`
  while the turn is still running.
* **Degrades to today's behaviour.** A workspace without the agent feature, or an
  install whose grant predates it, answers ``feature_disabled`` / ``missing_scope``
  (``features.LATCH_ERRORS``). The first such answer latches agent sessions off
  through the shared capability latch in ``kiro_crew.slack.features`` (one log
  line, ONE dashboard notification, lifted when a reconnect finds the scope
  granted), and every call from then on -- that one included -- takes the
  ``assistant.threads.*`` path again. Any other failed call falls back to that
  path for that call only. Nothing is ever shown in Slack.

The driver is attached to the :class:`RealSlackClient` at boot
(:func:`configure`) and does nothing unless ``agent_sessions_enabled`` holds at
call time -- the Slack setting is on and the install can run agent sessions --
so with it off every call is the old one, and a toggle applied live needs no
reattach. Its state lives on the instance, never in module globals, so a second
boot in one process starts clean.
"""

from __future__ import annotations

import asyncio
import contextvars
import logging
import time
from collections import OrderedDict
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Any

from kiro_crew.slack.agent_sessions import fence
from kiro_crew.slack.channel_policy import SlackChannelRefused, require_channel

if TYPE_CHECKING:
    from kiro_crew.slack.client import RealSlackClient

logger = logging.getLogger(__name__)

SESSION_STATUS_ACTIVE = "active"
SESSION_STATUS_PROCESSING = "processing"
SESSION_STATUS_SUSPENDED = "suspended"
#: The lifecycle states this runtime sends. ``closed`` is Slack's fourth state and is
#: deliberately absent: nothing here ends a session, so nothing sends it.
SESSION_STATUSES = frozenset(
    {SESSION_STATUS_ACTIVE, SESSION_STATUS_PROCESSING, SESSION_STATUS_SUSPENDED}
)

#: Status texts the existing call sites set while the turn waits on a PERSON: the
#: native handler's tool-approval prompt and the review path's draft. Pinned against
#: those call sites by ``test_slack_agent_session_status.py``.
SUSPENDED_STATUS_TEXTS = frozenset({"Waiting for approval…", "Awaiting review…"})

#: Slack moves a ``processing`` session to ``active`` after an hour; re-sent well
#: inside that so a long build never loses its "Working..." and its Stop button.
PROCESSING_REFRESH_SECS = 50 * 60

#: ``setStatus`` warns with this while the app is not subscribed to
#: ``agent_session_stopped``: Slack then shows a loader with no Stop button.
MISSING_STOP_SUBSCRIPTION_WARNING = "missing_agent_session_stopped_event_subscription"

#: Bound on the per-thread records, so a long-lived gateway cannot grow them forever.
_MAX_TRACKED_SESSIONS = 2048

#: Slack's limit on a session title.
_TITLE_MAX = 200

#: Bound on a thread's unanswered renames, in case Slack never echoes one.
_MAX_PENDING_ECHOES = 4

#: How long a rename of ours waits for its echo; Slack sends it within seconds.
_ECHO_WINDOW_SECS = 60.0

_NO_STOP_TITLE = "Slack shows no Stop button"
_NO_STOP_BODY = (
    "The Slack app is not subscribed to the `agent_session_stopped` event, so threads "
    "show “Working…” without a Stop button. Update the app from the manifest in "
    "Settings → Slack and reinstall it to the workspace."
)


def lifecycle_for_text(text: str) -> str:
    """The agent-session state an ``assistant.threads``-style status text means."""
    if not text:
        return SESSION_STATUS_ACTIVE
    if text in SUSPENDED_STATUS_TEXTS:
        return SESSION_STATUS_SUSPENDED
    return SESSION_STATUS_PROCESSING


def thread_ts_of(key_or_ts: str) -> str | None:
    """The bare Slack thread ts in a session key or ts, or None when it holds none.

    The title call sites pass a session key (``slack:<ts>``); a single-session DM's
    key names a channel (``slack:D…``) and a dashboard-owned thread's key is not a
    Slack key at all, and neither is a thread Slack could address.
    """
    ts = key_or_ts.removeprefix("slack:")
    head, dot, tail = ts.partition(".")
    if dot and head.isdigit() and tail.isdigit():
        return ts
    return None


def _carries_content(method: str, params: dict[str, Any]) -> bool:
    """Whether an ``agents.sessions`` call puts anything new into the channel.

    A rename writes a title; a working or paused state is shown in the thread.
    Only moving a session back to ``active`` with no title clears what is there.
    """
    if method != "agents.sessions.setStatus":
        return True
    return params.get("status") != SESSION_STATUS_ACTIVE or bool(params.get("title"))


def _error_code(exc: BaseException) -> str:
    response = getattr(exc, "response", None)
    try:
        return str((response or {}).get("error", "") or "")
    except Exception:
        return ""


#: Statuses this process set that leave Slack showing a session as busy.
_OWED_CLEAR = frozenset({SESSION_STATUS_PROCESSING, SESSION_STATUS_SUSPENDED})

#: Slack errors after which an ``active`` can never reach that session: the token
#: is refused, or the session, thread or channel is gone. Only these release a
#: thread that is still owed its ``active``; a capability latch or a transient
#: failure does not.
_FINAL_REFUSALS = frozenset(
    {
        "invalid_auth",
        "not_authed",
        "token_revoked",
        "token_expired",
        "account_inactive",
        "channel_not_found",
        "thread_not_found",
        "session_not_found",
        "is_archived",
    }
)


@dataclass
class _Record:
    """What this process last told Slack about one session."""

    status: str = ""
    sent_at: float = 0.0
    title: str = ""
    #: Titles this process sent whose ``agent_session_title_changed`` has not come
    #: back yet, oldest first. Each is consumed once, so a person who later renames
    #: the thread back to one of them is still mirrored.
    pending_echoes: list[tuple[str, float]] = field(default_factory=list)
    #: The turn that last announced ``processing``; the keepalive runs while it does.
    turn: fence.TurnScope | None = field(default=None, repr=False)
    keepalive: asyncio.Task[None] | None = field(default=None, repr=False)
    #: A native call failed since the last clear, so the old path may show a status
    #: too: the next clear goes both ways. ``status`` keeps what Slack last accepted.
    legacy_owed: bool = False


class AgentSessionStatus:
    """Drives ``agents.sessions.*`` for one Slack client; see the module docstring."""

    def __init__(
        self,
        client: RealSlackClient,
        *,
        gate: Callable[[], bool] = lambda: True,
        notify: Callable[[], Callable[..., Any] | None] | None = None,
        clock: Callable[[], float] = time.monotonic,
        sleep: Callable[[float], Awaitable[None]] = asyncio.sleep,
    ) -> None:
        self._client = client
        #: Whether the Slack setting is on (and the install can run it), read on
        #: every call so a toggle applied live takes effect on the next status.
        self._gate = gate
        #: Resolves the dashboard's ``notify`` when it is needed: the dashboard state
        #: may be wired after the client is.
        self._notify = notify
        self._clock = clock
        self._sleep = sleep
        self._records: OrderedDict[tuple[str, str | None], _Record] = OrderedDict()
        self._warned_no_stop = False
        self._warned_over_bound = False
        #: The error code of the last native call that failed ("" after a success).
        self._last_error = ""

    @property
    def active(self) -> bool:
        try:
            from kiro_crew.slack import features

            if features.AGENT_SESSIONS in features.latched():
                return False
            return self._gate() is True
        except Exception:
            logger.debug("agent-session gate failed; treating the feature as off", exc_info=True)
            return False

    # ── the two call-site dialects ──

    async def set_text(self, channel: str, thread_ts: str, text: str) -> bool:
        """``assistant.threads.setStatus``-style entry. False means "use the old call"."""
        if not channel or not thread_ts:
            return False
        return await self.set_status(channel, thread_ts, lifecycle_for_text(text))

    def owes_clear(self, channel: str, thread_ts: str | None) -> bool:
        """Whether this process left *thread_ts* working or paused natively.

        Such a session must still be moved to ``active`` if the feature goes off
        mid-turn (the toggle switched off, or a reconnect that could not read the
        scopes): otherwise Slack shows "Working..." with a dead Stop until its
        own one-hour timeout.
        """
        record = self._records.get((channel, thread_ts or None))
        return record is not None and record.status in _OWED_CLEAR

    async def set_status(self, channel: str, thread_ts: str | None, status: str) -> bool:
        """Move one session to *status*; False when the old path should run instead.

        *thread_ts* ``None`` addresses a whole-channel session (none is opened
        yet), where Slack refuses a ``thread_ts``. Never raises.
        """
        if not channel or status not in SESSION_STATUSES:
            return False
        if not self.active:
            # Off now, but a session this process set working still gets its
            # clear; anything else goes the old way, as with the feature off.
            if status == SESSION_STATUS_ACTIVE and self.owes_clear(channel, thread_ts):
                cleared = await self._send_owed_clear(channel, thread_ts)
                return cleared and not self._take_legacy_owed((channel, thread_ts or None))
            if status != SESSION_STATUS_ACTIVE and self.owes_clear(channel, thread_ts):
                # The old call shows this one: the clear owed natively must clear
                # it there too.
                self._records[(channel, thread_ts or None)].legacy_owed = True
            return False
        key = (channel, thread_ts or None)
        record = self._records.get(key)
        now = self._clock()
        if status == SESSION_STATUS_ACTIVE:
            if record is None or record.status == "":
                # No native status was ever set here: setStatus would create a
                # session, and a working state, if any, came from the old call
                # (a native call that failed fell back to it), so the old call
                # clears it.
                return False
            if record.status == status:
                # Already ready natively; a status the old path took over after a
                # failed native call is still its to clear.
                return not self._take_legacy_owed(key)
        if (
            status == SESSION_STATUS_PROCESSING
            and record is not None
            and record.status == status
            and now - record.sent_at < PROCESSING_REFRESH_SECS
        ):
            # The keepalive follows whichever turn announced "working" last.
            record.turn = fence.current_scope() or record.turn
            self._arm_keepalive(key, record)
            return True
        if self._withdrawn("agents.sessions.setStatus", {"channel_id": channel, "status": status}):
            # Handled: nothing is shown, and the old call must not send it either.
            return True
        ok = await self._send_status(channel, thread_ts, status)
        # Only a failed call hands the status back to the old path. A call Slack
        # accepted is recorded even if the feature went off meanwhile, so the
        # clear that follows is still owed (and sent).
        if not ok:
            # Refused: this call goes the old way (and a latch, every later one).
            # Slack still shows what it last accepted, so that stays recorded and
            # the next clear still moves the session to ``active``: dropping it
            # would leave "Working..." and a dead Stop behind a clear that only
            # reaches the old path. The old path may now show a status of its own,
            # so that clear goes there too, never stopping at "already ready".
            stale = self._records.get(key)
            if stale is not None:
                stale.legacy_owed = True
                stale.turn = None
                self._disarm(stale)
                if status == SESSION_STATUS_ACTIVE:
                    self._active_refused(key)
            return False
        record = self._track(key)
        record.status = status
        record.sent_at = now
        if status == SESSION_STATUS_PROCESSING:
            record.turn = fence.current_scope()
            self._arm_keepalive(key, record)
        else:
            record.turn = None
            self._disarm(record)
        if status == SESSION_STATUS_ACTIVE and self._take_legacy_owed(key):
            return False  # cleared natively; the old path's status is cleared too
        return True

    def _take_legacy_owed(self, key: tuple[str, str | None]) -> bool:
        """Whether the old path must also be cleared here, and settle it."""
        record = self._records.get(key)
        if record is None or not record.legacy_owed:
            return False
        record.legacy_owed = False
        return True

    async def rename(self, channel: str, thread_ts: str | None, title: str) -> bool:
        """``agents.sessions.rename``; False means "use the old setTitle call"."""
        title = title.strip()[:_TITLE_MAX]
        if not self.active or not channel or not title:
            return False
        params: dict[str, Any] = {"channel_id": channel, "title": title}
        if thread_ts:
            params["thread_ts"] = thread_ts
        key = (channel, thread_ts or None)
        if self._withdrawn("agents.sessions.rename", params):
            return True  # handled: no title, no echo to wait for, no old call
        # Recorded before the call: Socket Mode can deliver the echo before the
        # HTTP reply returns.
        echo = (title, self._clock())
        record = self._track(key)
        record.pending_echoes = [*record.pending_echoes, echo][-_MAX_PENDING_ECHOES:]
        if not await self._call("agents.sessions.rename", params):
            current = self._records.get(key)
            if current is not None:
                current.pending_echoes = [e for e in current.pending_echoes if e is not echo]
            return False
        self._track(key).title = title
        return True

    def take_echo(self, channel: str, thread_ts: str | None, title: str) -> bool:
        """Whether a rename event is this process's own rename coming back.

        A match consumes that rename and every older one (Slack delivers them in
        order). A rename that matches none is a person's, and clears the rest: any
        echo of ours would have arrived before it. A rename Slack never echoed
        expires after ``_ECHO_WINDOW_SECS``, so it cannot swallow a person's later
        rename to the same title.
        """
        record = self._records.get((channel, thread_ts or None))
        if record is None:
            return False
        now = self._clock()
        pending = [e for e in record.pending_echoes if now - e[1] < _ECHO_WINDOW_SECS]
        titles = [t for t, _ in pending]
        if title in titles:
            record.pending_echoes = pending[titles.index(title) + 1 :]
            return True
        record.pending_echoes = []
        return False

    def owns(self, channel: str, thread_ts: str | None) -> bool:
        """Whether this process put that session into a lifecycle state."""
        record = self._records.get((channel, thread_ts or None))
        return record is not None and bool(record.status)

    def last_title(self, channel: str, thread_ts: str | None) -> str:
        record = self._records.get((channel, thread_ts or None))
        return record.title if record is not None else ""

    async def settle_after_stop(self, channel: str, thread_ts: str | None) -> None:
        """Move a stopped session off ``processing``, as Slack requires of the app.

        Slack does not change the status itself when Stop is pressed. Sent whatever
        was last recorded -- the click came from a loader, so Slack believes the
        session is still working, and after a restart nothing is recorded at all.
        The caller decides the session is this bot's before calling.
        """
        key = (channel, thread_ts or None)
        record = self._records.get(key)
        owed = record is not None and record.status in _OWED_CLEAR
        if record is not None:
            self._disarm(record)
        if not channel or (not self.active and not owed):
            return
        if await self._send_status(channel, thread_ts, SESSION_STATUS_ACTIVE):
            record = self._track(key)
            record.status = SESSION_STATUS_ACTIVE
            record.turn = None
            record.sent_at = self._clock()
            return
        self._active_refused(key)
        record = self._records.get(key)
        if record is not None and record.status == SESSION_STATUS_PROCESSING:
            # Not settled, and the stopped turn's own clear is muted: put the
            # keepalive back. Its turn has ended, so it sends ``active`` at the
            # next refresh deadline instead of re-sending ``processing``.
            self._arm_keepalive(key, record)

    async def close(self) -> None:
        for record in self._records.values():
            self._disarm(record)
        self._records.clear()

    # ── internals ──

    def _track(self, key: tuple[str, str | None]) -> _Record:
        record = self._records.get(key)
        if record is None:
            record = self._records[key] = _Record()
            while len(self._records) > _MAX_TRACKED_SESSIONS and self._evict_one(keep=key):
                pass
        else:
            self._records.move_to_end(key)
        return record

    def _evict_one(self, *, keep: tuple[str, str | None]) -> bool:
        """Drop the least recently used record that owes no ``active``; False when none.

        A thread left working or paused keeps its record until its ``active`` is
        sent or Slack refuses it for good (:meth:`_active_refused`), so it is never
        evicted.
        """
        victim = next(
            (k for k, r in self._records.items() if k != keep and r.status not in _OWED_CLEAR),
            None,
        )
        if victim is None:
            # Every record is a thread still owed its ``active``: keep them all and
            # let the bound give, rather than strand a "Working..." session.
            if not self._warned_over_bound:
                self._warned_over_bound = True
                logger.warning(
                    "agent sessions: more than %d threads are working at once",
                    _MAX_TRACKED_SESSIONS,
                )
            return False
        self._disarm(self._records.pop(victim))
        return True

    def _disarm(self, record: _Record) -> None:
        task, record.keepalive = record.keepalive, None
        if task is not None and not task.done() and task is not asyncio.current_task():
            task.cancel()

    def _arm_keepalive(self, key: tuple[str, str | None], record: _Record) -> None:
        if record.turn is None:
            # Not inside a Slack turn (a mirror, say): nothing tells
            # the keepalive when the work ends, so Slack's own timeout bounds it.
            return
        if record.keepalive is not None and not record.keepalive.done():
            return
        # A fresh context: the keepalive must not inherit the turn's write fence, and
        # it outlives the call that armed it.
        record.keepalive = asyncio.get_running_loop().create_task(
            self._keepalive(key, record), context=contextvars.Context()
        )

    async def _keepalive(self, key: tuple[str, str | None], record: _Record) -> None:
        """Re-send ``processing`` while the turn that set it is still running.

        Bound to the TURN, not to the status: a turn that ends without clearing
        its status (cancelled by another Stop path, or killed) would otherwise be
        kept "Working..." forever. Such a session is moved to ``active`` instead.
        """
        try:
            while record.status == SESSION_STATUS_PROCESSING:
                wait = record.sent_at + PROCESSING_REFRESH_SECS - self._clock()
                if wait > 0:
                    await self._sleep(wait)
                    continue
                if self._records.get(key) is not record:
                    return
                if not fence.is_running(record.turn):
                    record.turn = None
                    if await self._send_status(key[0], key[1], SESSION_STATUS_ACTIVE):
                        record.status = SESSION_STATUS_ACTIVE
                        record.sent_at = self._clock()
                    else:
                        self._active_refused(key)
                    return
                if not self.active:
                    # Off mid-turn: no refresh, but keep watching the turn so its
                    # session is still moved to ``active`` when it ends.
                    record.sent_at = self._clock()
                    continue
                refresh = {"channel_id": key[0], "status": SESSION_STATUS_PROCESSING}
                if self._withdrawn("agents.sessions.setStatus", refresh):
                    return  # the channel was withdrawn: stop refreshing; the end still clears
                if await self._send_status(key[0], key[1], SESSION_STATUS_PROCESSING):
                    record.sent_at = self._clock()
                else:
                    return
        finally:
            if record.keepalive is asyncio.current_task():
                record.keepalive = None

    def _active_refused(self, key: tuple[str, str | None]) -> None:
        """After a failed ``active``: let the thread go only when Slack refused it for
        good (the token, or the session or channel is gone), so it is owed nothing
        more and its next clear goes the old way. Any other failure keeps it owed,
        and the next clear or Stop tries again."""
        record = self._records.get(key)
        if record is not None and self._last_error in _FINAL_REFUSALS:
            record.status = ""
            record.turn = None
            self._disarm(record)

    async def _send_owed_clear(self, channel: str, thread_ts: str | None) -> bool:
        key = (channel, thread_ts or None)
        record = self._records.get(key)
        if record is not None:
            self._disarm(record)
        if not await self._send_status(channel, thread_ts, SESSION_STATUS_ACTIVE):
            self._active_refused(key)
            return False
        record = self._track(key)
        record.status = SESSION_STATUS_ACTIVE
        record.turn = None
        record.sent_at = self._clock()
        return True

    async def _send_status(self, channel: str, thread_ts: str | None, status: str) -> bool:
        params: dict[str, Any] = {"channel_id": channel, "status": status}
        if thread_ts:
            params["thread_ts"] = thread_ts
        return await self._call("agents.sessions.setStatus", params)

    @staticmethod
    def _withdrawn(method: str, params: dict[str, Any]) -> bool:
        """Whether the Slack channel policy refuses this content write.

        The same check as every content write in the client (``require_channel``):
        a channel withdrawn mid-turn gets no title and no working state. The
        refusal is audited there, once. A caller that sees True sends nothing and
        RECORDS nothing (no status, keepalive or rename echo), so a later clear
        cannot treat an undelivered status as one to move to ``active``. Moving a
        delivered session back to ``active`` adds no content and stays exempt.
        """
        if not _carries_content(method, params):
            return False
        try:
            require_channel(params["channel_id"])
        except SlackChannelRefused:
            return True
        return False

    async def _call(self, method: str, params: dict[str, Any]) -> bool:
        client = self._client
        client._inject_team(params["channel_id"], params)
        try:
            resp = await client._web.api_call(method, params=params)
        except Exception as exc:
            code = _error_code(exc)
            self._last_error = code
            if not self._latch(code):
                logger.debug("%s failed (%s)", method, code or type(exc).__name__, exc_info=True)
            return False
        self._last_error = ""
        warning = ""
        try:
            warning = str(resp.get("warning", "") or "")
        except Exception:
            warning = ""
        if MISSING_STOP_SUBSCRIPTION_WARNING in warning and not self._warned_no_stop:
            self._warned_no_stop = True
            logger.warning("Slack: %s — threads show no Stop button", warning)
            self._push(_NO_STOP_TITLE, _NO_STOP_BODY)
        return True

    def _latch(self, code: str) -> bool:
        """Latch agent sessions off on a capability refusal; False for any other code.

        The shared latch (``features.latch_unavailable``) logs one line, pushes the
        one dashboard notification and shows on the settings page.
        """
        from kiro_crew.slack import features

        if not features.latch_unavailable(features.AGENT_SESSIONS, code):
            return False
        # The latch blocks only NEW native working and title writes (``active``
        # reads False). Every record stays: a thread this process left working or
        # paused is still owed its ``active`` (the turn-end clear, a native Stop,
        # its keepalive watching the turn), and those reach it through the record.
        return True

    def _push(self, title: str, body: str) -> None:
        notify = self._notify() if self._notify is not None else None
        if notify is None:
            return
        try:
            notify("agent", title, body, meta={"source": "slack.agent_sessions"})
        except Exception:
            logger.debug("agent-session notification failed", exc_info=True)


def configure(orch: Any) -> AgentSessionStatus | None:
    """Attach the driver to the orchestrator's Slack client, gated on the setting.

    Called once at boot. The driver is always attached to a real client but does
    nothing unless ``agent_sessions_enabled`` holds for ``orch._cfg`` when a call
    is made (the Slack setting is on and the install can run agent sessions), so
    a toggle applied to the live config needs no reattach. Off, every call goes
    the old way.
    """
    from kiro_crew.slack.features import agent_sessions_enabled

    client = getattr(orch, "slack", None)
    if client is None or not hasattr(client, "_web"):
        return None

    def _gate() -> bool:
        return agent_sessions_enabled(getattr(orch, "_cfg", None))

    def _notify() -> Callable[..., Any] | None:
        state = getattr(orch, "dashboard_state", None)
        return getattr(state, "notify", None) if state is not None else None

    driver = AgentSessionStatus(client, gate=_gate, notify=_notify)
    client.agent_sessions = driver
    return driver


def clearing_driver(slack: Any, channel: str, thread_ts: str | None) -> AgentSessionStatus | None:
    """The driver when it is live, or when it owes *thread_ts* a clear while off.

    A Stop on a session this process left working must still settle it after the
    feature went off mid-turn; nothing else is done natively while it is off.
    """
    driver = getattr(slack, "agent_sessions", None)
    if not isinstance(driver, AgentSessionStatus):
        return None
    if driver.active or driver.owes_clear(channel, thread_ts):
        return driver
    return None


def driver_of(slack: Any) -> AgentSessionStatus | None:
    """The live driver on a Slack client, or None (feature off, disabled, or a double)."""
    driver = getattr(slack, "agent_sessions", None)
    if isinstance(driver, AgentSessionStatus) and driver.active:
        return driver
    return None


async def set_session_status(slack: Any, channel: str, thread_ts: str | None, status: str) -> bool:
    """Move a session to *status* when agent sessions are live; never raises.

    The renderer's helper for the approval pause. False when the
    feature is off, disabled for the process, or Slack refused the call.
    """
    driver = driver_of(slack)
    if driver is None or fence.turn_muted(channel, thread_ts):
        return False
    try:
        return await driver.set_status(channel, thread_ts, status)
    except Exception:
        logger.debug("agent session status update failed", exc_info=True)
        return False
