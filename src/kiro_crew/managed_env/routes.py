"""Dashboard routes for the managed-environment lane (Settings -> Remote Crew).

The launch itself goes through the core's ``POST /api/cloud/launch`` with
``provider_id=kiro_managed_env``. These routes cover what that seam does not:

=====================================================  ===================================
``GET  /api/managed-environments/list``                the service's list merged with the
                                                       registry rows
``GET  /api/managed-environments/row-lifecycle``       the lane switch and the token-free
                                                       sign-in status: no service call
``GET  /api/managed-environments/signed-in-user``      who the lane is signed in as
``POST /api/managed-environments/{id}/{action}``       ``stop``, ``resume`` or ``delete``
``POST /api/managed-environments/{id}/ssh-session``    one StartSshSession, for the ssh
                                                       ``ProxyCommand`` (:mod:`.mint_and_attach`)
``POST /api/managed-environments/{id}/idle-timeout``   ``{"minutes": n}``: one UpdateSandbox
``POST /api/managed-environments/{id}/rename``         ``{"name": crew}``: one UpdateSandbox
``POST /api/managed-environments/{id}/start-crew``     one run of the box's boot hook
``POST /api/managed-environments/{id}/set-up-crew``    the one read on a box with no record
``GET  /api/managed-environments/{id}/connect-phase``  how far a connect has got, from memory
``GET|POST /api/managed-environments/network-step/{op}``  the network setting step
=====================================================  ===================================

``{id}`` is the environment's ``execEnvironmentId``. Every call goes through the client's
guard, which accepts only ids it knows (this client's ledger, or the current sign-in's
listed Crew launches), so a foreign id (or a made-up one) is refused before any service
call. The list also shows environments another client made (``own: false``); the panel
draws those with a label and no actions.

Exposure: the dashboard listens on loopback only, every handler runs the core's owner check
first (its answer is the route's answer), and with the ``crew_client`` preview off every
route then answers 404, as if it were not mounted. No response carries a bearer. The one
exception is ``ssh-session``: its answer is the single-use ssh session, owner-only and over
loopback, read by the ssh ``ProxyCommand`` that asked; it is never logged, audited or
stored.

Stop, resume and delete take seconds to minutes, so they run on a worker thread and the
list reports their progress.

``idle-timeout`` takes a whole number of minutes, 15 to 10080 (else a 400 and no call), one
action per environment as the others. A stopped environment keeps the value for its next
resume. A running one restarts: its status reads RUNNING until the restart begins, so the
worker polls ``GetSandbox`` until it leaves RUNNING (180 s at most; past that it ends with a
note, not an error), then waits for RUNNING. The service's answers become plain texts
(:data:`IDLE_TEXT`).
"""

from __future__ import annotations

import asyncio
import functools
import logging
import threading
import time
from typing import Any, Awaitable, Callable, Optional

from aiohttp import web

from kiro_crew.managed_env import PROVISIONER_ID, core_signin, netcfg_step
from kiro_crew.managed_env import settings as lane_settings
from kiro_crew.managed_env.client import (
    POLL_INTERVAL_S,
    SandboxError,
    SandboxStateError,
    is_resume_not_ready,
)
from kiro_crew.managed_env.engine import (
    IDLE_SWITCH_MARK,
    RENAME_TEXT,
    SET_UP_CREW_TEXT,
    START_CREW_TEXT,
    SandboxEngineError,
    SandboxLaunchEngine,
    crew_display_name,
    idle_switch_off_at,
    sandbox_id_from_host,
    ssh_host_for,
    validate_crew_name,
)
from kiro_crew.managed_env.guard import (
    POLICY_KIND_WORD,
    POLICY_TEXT,
    RENAME_OFF_TEXT,
    SERVICE_TEXT,
    ClientStopped,
    GuardRefused,
    crew_name_of,
    crew_tag_of,
    is_crew_marker,
    is_policy_refusal,
    is_rename_switch_unreadable,
    stopped_line,
)
from kiro_crew.managed_env.netcfg_step import NetcfgRefused, NetConfig
from kiro_crew.managed_env.settings import SettingsMissing

logger = logging.getLogger(__name__)

_ACTIONS = ("stop", "resume", "delete")
_ERROR_MAX = 300
#: a Resume whose not-ready retry ran out (the box still stopping or restarting), in plain
#: words. The List that next shows the box RUNNING clears it.
RESUME_BUSY_TEXT = "This sandbox is still stopping or restarting. Try Resume again in a minute."
# The list item's crew_marker is guard.is_crew_marker, the Crew launch's own ``kc-<6 hex>``
# displayName. The marker alone grants nothing: the accepted set is
# filled only from a full List under the current sign-in (guard.accept_listed).

#: a not-found read is shown again, with no call, for this long in one RUNNING period.
SET_UP_CACHE_S = 600.0

IDLE_ACTION = "idle-timeout"
IDLE_MIN, IDLE_MAX = 15, 10080
IDLE_RESTART_BEGIN_S = 180.0  # the longest wait for a running sandbox's restart to begin
IDLE_OFF_REMEMBER_S = 600.0  # how long the idle switch's answer greys the control
# the answers, matched by text pinned from recorded ones: the switch's 400 (on
# CreateSandbox; UpdateSandbox runs the same check first), the busy 409 (
# "Sandbox <id> is changing state (...). Try again later."), and the running sandbox that can't be
# restarted ("... Stop the sandbox, then update it."). A route the service doesn't
# have answers an UnknownOperation kind (its exact spelling unrecorded: matched by prefix).
IDLE_BUSY_MARK = "is changing state"
IDLE_STOP_FIRST_MARK = "Stop the sandbox, then update it"
IDLE_NO_ROUTE_KIND = "UnknownOperation"
IDLE_TEXT = {
    "range": "The idle timeout is a whole number of minutes, 15 minutes to 7 days.",
    "off": (
        "Idle stop is off for your account, so this sandbox doesn't stop on its own."
        " Stop it when you're done."
    ),
    "busy": "The sandbox is busy (starting, stopping or restarting). Try again in a moment.",
    "stop_first": "Stop the sandbox, then change its idle timeout.",
    "no_route": "This Kiro service doesn't offer the idle timeout change yet.",
    "not_begun": "Saved. The restart hasn't begun yet; chats stop when it does.",
}


def _lane_enabled() -> bool:
    return lane_settings.lane_enabled()


def _row_by_id(reg: Any, iid: str) -> Any:
    """The registry row *iid*, or None."""
    for row in reg.list():
        if getattr(row, "id", "") == iid:
            return row
    return None


def _row_lifecycle() -> bool:
    """Whether Stop / Resume / Idle timeout / Rename / Delete sit on each crew's row: the lane on."""
    return _lane_enabled()


class _ActionTracker:
    """In-flight and last-finished sandbox actions, one per sandbox."""

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._state: dict[str, dict] = {}

    def begin(self, key: str, action: str) -> bool:
        with self._lock:
            current = self._state.get(key)
            if current and current.get("running"):
                return False
            self._state[key] = {"action": action, "running": True, "error": "", "at": time.time()}
            return True

    def finish(self, key: str, error: str = "", *, note: str = "") -> None:
        """``note`` is a line for an action that ended without an error but isn't over (the
        idle timeout's restart that hasn't begun); it is in the entry only when set."""
        with self._lock:
            entry = self._state.setdefault(key, {})
            entry.update(running=False, error=error, at=time.time())
            entry.pop("note", None)
            if note:
                entry["note"] = note

    def get(self, key: str) -> dict:
        with self._lock:
            return dict(self._state.get(key) or {})

    def clear_error(self, key: str, action: str) -> None:
        """Drop a finished *action*'s error line (the List showed the box RUNNING)."""
        with self._lock:
            entry = self._state.get(key)
            if entry and not entry.get("running") and entry.get("action") == action:
                entry["error"] = ""


CONNECT_WINDOW_S = 60.0
CONNECT_PHASES = ("starting", "tunnel", "signing_in")


class _ConnectPhases:
    """How far each sandbox's connect has got: a start time and a count, never a value.

    A connect window opens at a tuple ask while none is open, or once two tuples were served
    (that connect has both its ssh's, so a new ask is a new connect), and lasts
    ``CONNECT_WINDOW_S``; a failed ask closes it. Inside it the tuples served so far give the
    phase: none yet is ``starting`` (the session is being started), one is ``tunnel`` (the
    tunnel's ssh has its tuple), two is ``signing_in`` (the token mint's ssh has its tuple). A
    user's own ssh inside an open window counts too, so the label is best effort.
    """

    def __init__(self, now: Callable[[], float]) -> None:
        self._now = now
        self._lock = threading.Lock()
        self._open: dict[str, tuple[float, int]] = {}  # sandbox id -> (opened at, tuples served)

    def _current(self, sid: str) -> Optional[tuple[float, int]]:
        cur = self._open.get(sid)
        if cur is not None and self._now() - cur[0] >= CONNECT_WINDOW_S:
            del self._open[sid]
            return None
        return cur

    def asked(self, sid: str) -> None:
        with self._lock:
            cur = self._current(sid)
            if cur is None or cur[1] >= len(CONNECT_PHASES) - 1:
                self._open[sid] = (self._now(), 0)

    def served(self, sid: str) -> None:
        with self._lock:
            cur = self._current(sid)
            if cur is not None:
                self._open[sid] = (cur[0], cur[1] + 1)

    def failed(self, sid: str) -> None:
        with self._lock:
            self._open.pop(sid, None)

    def phase(self, sid: str) -> str:
        with self._lock:
            cur = self._current(sid)
        return "" if cur is None else CONNECT_PHASES[min(cur[1], len(CONNECT_PHASES) - 1)]


FORWARD_HOLD_STATES = ("STOPPING", "STOPPED")


class _ForwardHold:
    """The lane crews whose forward this dashboard paused while their sandbox read STOPPING or
    STOPPED, so the core's tunnel self-heal stops asking a stopped box for ssh tuples.

    A pause is the core manager's public ``disconnect(keep_intent=True)``: the forward goes,
    a recovery the self-heal had parked is left stale by the teardown, and the user's
    "connected" intent (``was_connected``) stays. When the sandbox reads RUNNING again, a crew
    paused here whose intent still stands gets one ``connect``. A user's own disconnect clears
    that intent, so it is never undone. The status comes only from reads the lane already makes
    (the list answer, the Stop request): no service call is added. In memory only.

    It also holds the sandbox itself (by sandbox id): while held, the ``ssh-session`` route
    answers 409 ``not_ready`` from memory with no StartSshSession. A pause alone doesn't stop
    every try, because the page's own auto-connect asks the core for a connect on each crew list
    load (the core website's ``useAutoConnectInstances``). The hold clears when the list reads
    the sandbox RUNNING, before the one connect.
    """

    def __init__(self) -> None:
        self._paused: set[str] = set()
        self._held: set[str] = set()
        self._tasks: set[asyncio.Task] = set()

    def is_paused(self, instance_id: str) -> bool:
        return instance_id in self._paused

    def hold(self, sid: str) -> None:
        self._held.add(sid)

    def release(self, sid: str) -> None:
        self._held.discard(sid)

    def holds(self, sid: str) -> bool:
        return sid in self._held

    def mark_paused(self, instance_id: str) -> None:
        """A crew row the lane turned off before a restart reads as paused again."""
        self._paused.add(instance_id)

    async def pause(
        self,
        mgr: Any,
        instance_id: str,
        turn_off: Optional[Callable[[], Awaitable[bool]]] = None,
    ) -> None:
        """``turn_off``: turn the crew's row off instead, True when it did."""
        if instance_id in self._paused:
            return
        self._paused.add(instance_id)
        try:
            if turn_off is not None:
                try:
                    if await turn_off():
                        return
                except Exception:  # noqa: BLE001 - the plain pause still runs
                    logger.warning("could not turn crew %s's row off", instance_id, exc_info=True)
            await mgr.disconnect(instance_id, keep_intent=True)
        except Exception:  # noqa: BLE001 - a failed pause leaves today's behaviour
            logger.warning("could not pause crew %s's forward", instance_id, exc_info=True)

    async def resume(
        self,
        mgr: Any,
        instance_id: str,
        wanted: Callable[[str], bool],
        turn_on: Optional[Callable[[], Awaitable[None]]] = None,
    ) -> None:
        """``turn_on``: turn on a row the lane turned off, before the one connect."""
        if instance_id not in self._paused:
            return
        self._paused.discard(instance_id)
        try:
            if turn_on is not None:
                await turn_on()
            if await asyncio.to_thread(wanted, instance_id):
                await mgr.connect(instance_id)
        except Exception:  # noqa: BLE001 - the user's own Connect still works
            logger.warning("could not reconnect crew %s", instance_id, exc_info=True)

    def spawn(self, coro: Any) -> None:
        """Run a pause or resume beside the request, so a list answer never waits on a connect."""
        task = asyncio.ensure_future(coro)
        self._tasks.add(task)
        task.add_done_callback(self._tasks.discard)

    async def idle(self) -> None:
        """Wait for every pause and resume started so far (tests)."""
        while True:
            pending = [t for t in self._tasks if not t.done()]
            if not pending:
                return
            await asyncio.gather(*pending, return_exceptions=True)


class SandboxRoutes:
    def __init__(
        self,
        engine_factory: Callable[[], SandboxLaunchEngine] = SandboxLaunchEngine,
        *,
        run_in_thread: bool = True,
        sleep: Callable[[float], None] = time.sleep,
        now: Callable[[], float] = time.time,
        crews_watch_s: Optional[float] = None,
    ) -> None:
        self._crews_watch_s = crews_watch_s  # None (tests) runs no watch
        self._engine_factory = engine_factory
        self._rows_lock = threading.Lock()
        self._landed_fp = ""  # the sign-in whose landing List already ran
        self._starting: set[str] = set()  # Start Crew in flight, by sandbox id
        self._last_status: dict[str, str] = {}  # each sandbox's status in the last List
        # when this process first read each box RUNNING (the once-per-RUNNING key), the one
        # read's last result per box for that period, and the boxes being read now.
        self._running_since: dict[str, float] = {}
        self._checks: dict[str, dict] = {}
        self._checking: set[str] = set()
        self._display_names: dict[str, str] = {}  # each listed box's name, for an adopted row
        self._renaming: set[str] = set()  # a Rename in flight, by sandbox id
        # the rows this lane turned off before a restart, read once at the first List
        self._turned_off_boot: set[str] = set()
        self._turned_off_read = False
        self._engine: Optional[SandboxLaunchEngine] = None
        self._run_in_thread = run_in_thread
        self._sleep = sleep
        self._now = now
        self.actions = _ActionTracker()
        self._idle_off_at: Optional[float] = None  # when the idle switch's 400 last came
        # the List outage's backoff: failures in a row, and
        # the time before which no List call is made
        self._list_backoff_n = 0
        self._list_retry_at = 0.0
        self.phases = _ConnectPhases(now)
        self.forwards = _ForwardHold()

    @property
    def engine(self) -> SandboxLaunchEngine:
        if self._engine is None:
            self._engine = self._engine_factory()
        return self._engine

    # ── gate ─────────────────────────────────────────────────────────────────
    @staticmethod
    def _audit(caller: str, operation: str, outcome: str, resource: str) -> None:
        try:
            import kiro_crew.dashboard.handlers as _pkg

            _pkg.sel().log_api_access(
                caller=caller,
                operation=operation,
                outcome=outcome,
                source="dashboard",
                resources=resource,
            )
        except Exception:
            logger.debug("security event log unavailable", exc_info=True)

    async def _gate(self, request: web.Request, operation: str) -> Optional[web.Response]:
        """The core's owner check first (its answer is the route's answer); then flag off = 404."""
        from kiro_crew.dashboard.handlers._shared import require_owner_dashboard_request

        refused = await require_owner_dashboard_request(request, operation)
        if refused is not None:
            return refused
        if not _lane_enabled():
            return web.json_response({"error": "not found"}, status=404)
        return None

    # ── list ─────────────────────────────────────────────────────────────────
    @staticmethod
    def _lane_rows(engine: SandboxLaunchEngine) -> Optional[list]:
        """This lane's Remote Crew rows, or None when the registry can't be read."""
        try:
            return [
                r
                for r in engine._registry().list()
                if getattr(r, "provisioner_id", "") == PROVISIONER_ID
                and sandbox_id_from_host(r.ssh_host)
            ]
        except Exception:
            logger.warning("could not read the Remote Crew rows", exc_info=True)
            return None

    def list_sync(self) -> dict:
        """The service list merged with this lane's registry rows (``ssh_host=crew-sbx-<id>``)."""
        engine = self.engine
        read = self._lane_rows(engine)
        rows = read or []
        error = ""
        entries: list[dict] = []
        mine: set[str] = set()
        listed = False
        guard = engine.guard
        # A List outage is retried on a capped backoff; a List asked for inside the wait makes
        # no call and keeps the retrying line
        waiting = self._list_backoff_n > 0 and self._now() < self._list_retry_at
        if waiting:
            error = SERVICE_TEXT["retrying"]
        else:
            try:
                entries = engine.client.list_sandboxes()
                mine = guard.own_ids()
                listed = True
                self._list_backoff_n = 0  # the next 200 clears the line
            except (SandboxError, GuardRefused, SandboxEngineError) as exc:
                entries = []
                logger.debug("the sandbox List failed: %s", exc)
                if guard.stopped:
                    # the answer that stopped the client reads as plainly as every List after
                    error = _stopped_line(guard.stopped_by_service)
                elif _list_outage(exc):
                    self._list_backoff_n += 1
                    wait = LIST_BACKOFF_S[min(self._list_backoff_n, len(LIST_BACKOFF_S)) - 1]
                    self._list_retry_at = self._now() + wait
                    error = SERVICE_TEXT["retrying"]
                else:
                    error = _short(exc)
        # Start Crew reads this: each box's status in the last List, none on a failed one.
        self._last_status = {
            str(e.get("execEnvironmentId") or ""): str(e.get("status") or "") for e in entries
        }
        if listed:
            for e in entries:
                # a listed box's policy hold ends (after its floor); its line stays
                guard.end_policy_hold(str(e.get("execEnvironmentId") or ""))
                if str(e.get("status") or "") == "RUNNING":
                    # the box is up, so a finished Resume's line has nothing left to say
                    self.actions.clear_error(str(e.get("execEnvironmentId") or ""), "resume")
            self._follow_running(entries)
            self._display_names = {
                str(e.get("execEnvironmentId") or ""): str(e.get("displayName") or "")
                for e in entries
            }
        # read once: crew_marker is sent only while it reads on (off: the answer is as before)
        row_on = _row_lifecycle()
        if listed and not self._turned_off_read:
            # the rows turned off here before a restart, so RUNNING turns them on again.
            # whatever row_lifecycle reads now, so a rollback (the switch off,
            # then a restart while the box is STOPPED) still turns them on at RUNNING
            self._turned_off_read = True
            self._turned_off_boot = engine.guard.turned_off_ids()
        # the user's List decides, only after a full List filled the accepted set
        # under a sign-in (the lane on).
        from_list = listed and row_on and bool(engine.guard.accepted_fingerprint)
        added = False
        if from_list and read is not None:
            added = self._register_from_list(engine, entries, mine, rows)
            if added:
                rows = self._lane_rows(engine) or rows
        by_host = {r.ssh_host: r for r in rows}
        out = []
        seen: set[str] = set()
        for e in entries:
            sid = str(e.get("execEnvironmentId") or "")
            seen.add(sid)
            row = by_host.get(ssh_host_for(sid))
            # guard.is_actionable's rule: the ledger read once for this list, or the accepted set
            own = bool(sid) and (sid in mine or (from_list and engine.guard.is_accepted(sid)))
            deletable = (sid in mine) if from_list else None
            out.append(
                self._item(
                    sid,
                    str(e.get("status") or ""),
                    e,
                    row,
                    own=own,
                    marker=row_on,
                    deletable=deletable,
                )
            )
        for host, row in by_host.items():
            sid = sandbox_id_from_host(host)
            if sid not in seen:
                # Only on a full answer: a failed list must not read as "all gone".
                # A row of this lane is one this Crew made: own.
                deletable = (sid in mine) if from_list else None
                out.append(
                    self._item(
                        sid,
                        "GONE" if listed else "",
                        {},
                        row,
                        own=True,
                        marker=row_on,
                        deletable=deletable,
                    )
                )
        answer: dict = {
            "sandboxes": out,
            "error": error,
            "row_lifecycle": row_on,
            "idle_off": self._idle_off(),
        }
        if engine.guard.stopped:
            # the panel says a crew can't reconnect until the dashboard restarts
            answer["client_stopped"] = True
        if from_list:
            answer["crews_from_list"] = True  # the panel draws Start Crew only under it
            for item in out:  # the one read's line on a box with no record
                line = "" if item["own"] else self._setup_line(item["sandbox_id"])
                if line:
                    item["setup_line"] = line
        if added:
            answer["rows_added"] = True
        return answer

    def _register_from_list(
        self, engine: SandboxLaunchEngine, entries: list[dict], mine: set[str], rows: list
    ) -> bool:
        """A Remote Crew row for each accepted box not in this home's ledger, with no
        row here and not removed by the user, by ``_write_row``'s same-host path only:
        no ssh, no StartSshSession. One lock across the check and the add. True if added.

        No ssh config is written: a List-made row reaches its box through the same
        ``Host crew-sbx-*`` block a launch-made row does (:mod:`.ssh_config`, written once by
        ``python -m kiro_crew.managed_env.ssh_config``), never by a List."""
        with self._rows_lock:
            hosts = {r.ssh_host for r in rows}
            registered, removed = engine.guard.list_rows()
            # A row this lane wrote from the List that is gone now, with no Delete in flight on it,
            # was removed by the user. The lane's own unregister follows only a Delete.
            by_hand = {
                sid
                for sid in registered
                if ssh_host_for(sid) not in hosts and not self._deleting(sid)
            }
            new: list[str] = []
            for e in entries:
                sid = str(e.get("execEnvironmentId") or "")
                host = ssh_host_for(sid)
                if (
                    not engine.guard.is_accepted(sid)
                    or sid in mine
                    or sid in removed
                    or sid in by_hand
                    or host in hosts
                ):
                    continue
                try:
                    # the crew name the launch put in the displayName; an older
                    # launch's bare tag stays the row's name, as before.
                    dn = str(e.get("displayName") or "")
                    engine._write_row(crew_name_of(dn) or dn, host, match_name=False)
                except (SandboxEngineError, ValueError):
                    logger.warning("could not write the Remote Crew row for %s", sid, exc_info=True)
                    continue
                hosts.add(host)
                new.append(sid)
            names, renamed = self._sync_row_names(engine, entries, mine, hosts, new)
            if new or by_hand or names:
                engine.guard.note_list_rows(registered=new, removed=by_hand, names=names)
            return bool(new) or renamed

    @staticmethod
    def _sync_row_names(
        engine: SandboxLaunchEngine, entries: list[dict], mine: set[str], hosts: set, new: list
    ) -> tuple[dict[str, str], bool]:
        """A Crew box's row here (this home's, or accepted) takes the crew name in its
        listed displayName when that name differs from the one this home last took, so a Rename
        on any dashboard shows on every one. A name given here alone stays until the shared name
        changes. Same host only (``rename_row``). Returns (the names to note, any row renamed)."""
        taken = engine.guard.row_names()
        names: dict[str, str] = {}
        renamed = False
        for e in entries:
            sid = str(e.get("execEnvironmentId") or "")
            crew = crew_name_of(str(e.get("displayName") or ""))
            if not crew or taken.get(sid) == crew or ssh_host_for(sid) not in hosts:
                continue
            if sid not in mine and not engine.guard.is_accepted(sid):
                continue
            if sid in new:
                names[sid] = crew  # written with that name just now
                continue
            if sid in mine and sid not in taken:
                # A row this home launched keeps the name it was given here: the first crew
                # name seen is its baseline, and only a later change renames it.
                names[sid] = crew
                continue
            try:
                changed, line = engine.rename_row(sid, crew)
            except Exception:  # a registry write: the next List tries again
                logger.warning("could not rename the Remote Crew row for %s", sid, exc_info=True)
                continue
            if line:
                logger.info("row of %s keeps its name: %s", sid, line)
                continue
            names[sid] = crew
            renamed = renamed or changed
        return names, renamed

    # ── the one read's period and result ─────────────────────────────────
    def _follow_running(self, entries: list[dict]) -> None:
        """A full List: keep when each box was first read RUNNING; a box that left RUNNING (or the
        List) drops its key and its read's result with it."""
        now = self._now()
        since: dict[str, float] = {}
        for e in entries:
            sid = str(e.get("execEnvironmentId") or "")
            if sid and str(e.get("status") or "") == "RUNNING":
                since[sid] = self._running_since.get(sid, now)
        self._running_since = since
        self._checks = {sid: c for sid, c in self._checks.items() if c.get("key") == since.get(sid)}

    def _setup_line(self, sid: str, *, cached_only: bool = False) -> str:
        c = self._checks.get(sid)
        if not c or c.get("key") != self._running_since.get(sid):
            return ""
        if c["result"] == "not_found":
            if cached_only and self._now() - c["at"] >= SET_UP_CACHE_S:
                return ""
            at = time.strftime("%H:%M", time.localtime(c["at"]))
            return f"{SET_UP_CREW_TEXT['not_found']} Checked at {at}."
        return "" if cached_only else SET_UP_CREW_TEXT.get(c["result"], "")

    def _read_and_adopt(self, sid: str) -> str:
        """The one read on a user-asked box (a worker thread), then adoption when found. Returns
        the result; not found is kept for the RUNNING period, a failure only as the last line."""
        engine = self.engine
        key = self._running_since.setdefault(sid, self._now())
        result = engine.crew_alive(sid)
        if result == "found":
            try:
                engine.adopt(sid, self._display_names.get(sid, ""))
            except Exception:  # a refusal, a bad name or a failed write: no adopted box
                logger.warning("could not adopt sandbox %s", sid, exc_info=True)
                result = "failed"
        self._checks[sid] = {"key": key, "at": self._now(), "result": result}
        return result

    def _deleting(self, sid: str) -> bool:
        a = self.actions.get(sid) or {}
        return a.get("action") == "delete"

    def _idle_off(self) -> bool:
        """True for 10 minutes after the idle switch's 400: the control is greyed meanwhile."""
        # a Create's idle switch 400 counts too (the launch runs in the core's job)
        marks = [t for t in (self._idle_off_at, idle_switch_off_at()) if t is not None]
        return bool(marks) and self._now() - max(marks) < IDLE_OFF_REMEMBER_S

    def _item(
        self,
        sid: str,
        status: str,
        entry: dict,
        row: Any,
        *,
        own: bool,
        marker: bool,
        deletable: Optional[bool] = None,
    ) -> dict:
        display_name = str(entry.get("displayName") or "")
        item = {
            "sandbox_id": sid,
            "status": status.upper(),
            "display_name": display_name,
            "created_at": entry.get("createdAt") or 0,
            "instance_id": getattr(row, "id", "") if row is not None else "",
            "name": getattr(row, "name", "") if row is not None else "",
            "ssh_host": ssh_host_for(sid),
            "action": self.actions.get(sid),
            "own": own,
            "idle_timeout_minutes": _minutes(entry.get("idleTimeoutMinutes")),
        }
        if marker:
            item["crew_marker"] = is_crew_marker(display_name)
        if deletable is not None:
            # Delete stays on this home's ledger boxes only
            item["deletable"] = deletable
        policy = self.engine.guard.policy_line(sid) if sid else ""
        if policy:
            # this box's connect was refused by the organization's policy (the others run on)
            item["policy_line"] = policy
        return item

    async def row_lifecycle_handler(self, request: web.Request) -> web.Response:
        """The flag, read from the config, and the lane's token-free sign-in status
        (``core_signin.sign_in_status``): no engine, no client, no service call, no token."""
        denied = await self._gate(request, "sandboxes_row_lifecycle")
        if denied is not None:
            return denied
        sign_in = await asyncio.to_thread(core_signin.sign_in_status)
        return web.json_response({"row_lifecycle": _row_lifecycle(), "sign_in": sign_in})

    async def signed_in_user_handler(self, request: web.Request) -> web.Response:
        """Who the lane is signed in as (``signed_in_user.read``): one ``GetUserInfo`` per
        sign-in, cached; owner-only, like every lane route. The answer goes to the owner's own
        dashboard only, and is never logged."""
        denied = await self._gate(request, "sandboxes_signed_in_user")
        if denied is not None:
            return denied
        from kiro_crew.managed_env import signed_in_user

        return web.json_response(
            await asyncio.to_thread(signed_in_user.read, lambda: self.engine.client)
        )

    async def list_handler(self, request: web.Request) -> web.Response:
        denied = await self._gate(request, "sandboxes_list")
        if denied is not None:
            return denied
        answer = await asyncio.to_thread(self.list_sync)
        self._follow_status(request, answer)
        return web.json_response(answer)

    # ── a crew's forward follows its sandbox (_ForwardHold) ──────────────────
    @staticmethod
    def _instances_manager(request: web.Request) -> Any:
        """The core's tunnel manager (the one ``/api/instances/{id}/connect`` uses), or None."""
        state = request.app.get("state")
        return getattr(state, "instances_manager", None)

    def _crew_wanted(self, instance_id: str) -> bool:
        """The user's intent for this crew: the core registry's ``was_connected``."""
        for row in self.engine._registry().list():
            if getattr(row, "id", "") == instance_id:
                return bool(getattr(row, "was_connected", False))
        return False

    def _crew_of(self, sid: str) -> str:
        """The id of this lane's crew row for sandbox *sid*, or ""."""
        host = ssh_host_for(sid)
        for row in self.engine._registry().list():
            if getattr(row, "provisioner_id", "") == PROVISIONER_ID and row.ssh_host == host:
                return getattr(row, "id", "")
        return ""

    def _follow_status(self, request: web.Request, answer: dict) -> None:
        self._follow_answer(self._instances_manager(request), answer)

    def _follow_answer(self, mgr: Any, answer: dict) -> None:
        """Hold each sandbox the list reads STOPPING or STOPPED, and pause its lane crew; release
        it, then connect a crew paused here, once it reads RUNNING again."""
        for item in answer.get("sandboxes") or []:
            status = item.get("status") or ""
            sid = item.get("sandbox_id") or ""
            # The hold is by sandbox id, for every listed sandbox, crew row or not: an ssh to a
            # sandbox whose crew row is gone must not reach a stopped box either.
            if status in FORWARD_HOLD_STATES:
                self.forwards.hold(sid)
            elif status == "RUNNING":
                self.forwards.release(sid)
            iid = item.get("instance_id") or ""
            if not iid or mgr is None:
                continue
            if iid in self._turned_off_boot:
                # turned off by this lane before a restart: RUNNING turns it on again
                self._turned_off_boot.discard(iid)
                self.forwards.mark_paused(iid)
            paused = self.forwards.is_paused(iid)
            if status in FORWARD_HOLD_STATES and not paused:
                self.forwards.spawn(self.forwards.pause(mgr, iid, self._turn_off(mgr, iid)))
            elif status == "RUNNING" and paused:
                self.forwards.spawn(
                    self.forwards.resume(mgr, iid, self._crew_wanted, self._turn_on(iid))
                )

    # ── a stopped crew reads off, not a red Error ───────────────────
    def _turn_off(self, mgr: Any, iid: str) -> Optional[Callable[[], Awaitable[bool]]]:
        """While ``row_lifecycle`` reads on: turn the crew's row off for a stopped sandbox, by
        the core's own locked path (``mgr.reconfigure``, as the core's PATCH ``disabled`` does:
        the tunnel goes, the connect intent stays). The core page's auto-connect skips only a
        turned-off row, so it stops trying a stopped box, and the core draws no status badge
        (no red Error, no ssh text); the edition's row reads "Sandbox stopped" with Resume. A row
        the user turned off is left alone. Remembered (the guard's state dir) for RUNNING."""
        if not (_row_lifecycle() and hasattr(mgr, "reconfigure")):
            return None

        async def turn_off() -> bool:
            reg = self.engine._registry()
            row = await asyncio.to_thread(_row_by_id, reg, iid)
            if row is None or getattr(row, "disabled", False):
                return False  # gone, or the user's own off: the plain pause runs
            guard = self.engine.guard
            # noted first, so a crash after the write still turns it on at RUNNING
            await asyncio.to_thread(functools.partial(guard.note_turned_off, add=(iid,)))
            try:
                await mgr.reconfigure(
                    iid, functools.partial(reg.update, iid, disabled=True), needs_teardown=None
                )
            except Exception:
                # only a failed write drops the note. A cancel (a Stop request
                # whose client hung up, a shutdown) may land after the core's shielded write set
                # the row off, so the note stays and RUNNING turns the row on again (turn_on reads
                # the row first, and leaves a row that is still on as it is)
                await asyncio.to_thread(functools.partial(guard.note_turned_off, drop=(iid,)))
                raise
            return True

        return turn_off

    def _turn_on(self, iid: str) -> Callable[[], Awaitable[None]]:
        async def turn_on() -> None:
            guard = self.engine.guard
            if iid not in await asyncio.to_thread(guard.turned_off_ids):
                return  # the user's own off, or never turned off here
            reg = self.engine._registry()
            row = await asyncio.to_thread(_row_by_id, reg, iid)
            if row is not None and getattr(row, "disabled", False):
                await asyncio.to_thread(functools.partial(reg.update, iid, disabled=False))
            await asyncio.to_thread(functools.partial(guard.note_turned_off, drop=(iid,)))

        return turn_on

    # ── stop / resume / delete ───────────────────────────────────────────────
    def _run_action(self, sid: str, action: str, then_read: bool = False) -> None:
        engine = self.engine
        error = ""
        try:
            if action == "stop":
                engine.client.stop_sandbox(sid)
            elif action == "resume":
                got = engine.client.resume_sandbox(sid)
                engine.client.wait_running(sid, start_shown=str(got.get("status")) != "STOPPED")
                if then_read:
                    # the follower's own rule, on the Get that read RUNNING: release the
                    # hold for this id, then the one read.
                    self.forwards.release(sid)
                    self._read_and_adopt(sid)
            elif action == "delete":
                if not engine.destroy(sid):
                    error = f"Could not confirm sandbox {sid} was deleted."
        except (SandboxError, SandboxStateError, GuardRefused, SandboxEngineError) as exc:
            error = _short(exc)
        except Exception as exc:  # the worker must always report back
            logger.exception("sandbox %s on %s failed", action, sid)
            error = type(exc).__name__
        if action == "stop" and error:
            # The StopSandbox failed, so the box isn't stopping: its ssh must not be refused.
            self.forwards.release(sid)
        self.actions.finish(sid, error)

    async def act(self, request: web.Request) -> web.Response:
        action = request.match_info.get("action", "")
        sid = request.match_info.get("sandbox_id", "")
        denied = await self._gate(request, f"sandboxes_{action}")
        if denied is not None:
            return denied
        if action not in _ACTIONS:
            return web.json_response({"error": f"unknown action {action!r}"}, status=400)
        guard = self.engine.guard
        # Delete stays on this home's ledger; Stop and Resume take the accepted set too
        allowed = guard.is_own(sid) if action == "delete" else guard.is_actionable(sid)
        then_read = False
        if action == "resume" and not allowed and sandbox_id_from_host(ssh_host_for(sid)):
            # the user's Resume on a STOPPED box with no record, then the one read
            then_read = allowed = await self._ask(sid, "STOPPED")
        if not sandbox_id_from_host(ssh_host_for(sid)) or not allowed:
            return web.json_response({"error": "not a sandbox of this crew client"}, status=404)
        if not self.actions.begin(sid, action):
            return web.json_response(
                {"error": "another action is still running on this sandbox"}, status=409
            )
        self._audit(request.get("user") or "unknown", f"sandboxes_{action}", "success", sid)
        if action == "stop":
            self.forwards.hold(sid)
        mgr = self._instances_manager(request) if action == "stop" else None
        if mgr is not None:
            # The crew's forward goes before the StopSandbox, so no try reaches a stopping box.
            iid = await asyncio.to_thread(self._crew_of, sid)
            if iid:
                await self.forwards.pause(mgr, iid, self._turn_off(mgr, iid))
        self._start_action(sid, action, then_read)
        return web.json_response({"ok": True, "action": action}, status=202)

    def _start_action(self, sid: str, action: str, then_read: bool = False) -> None:
        if self._run_in_thread:
            threading.Thread(
                target=self._run_action,
                args=(sid, action, then_read),
                name=f"sandbox-{action}",
                daemon=True,
            ).start()
        else:
            self._run_action(sid, action, then_read)

    async def _ask(self, sid: str, status: str) -> bool:
        """A box with no record, listed *status* by the last List, the switch on:
        the user's click makes it user-asked (the fingerprint read off the loop)."""
        from kiro_crew.managed_env.engine import (
            crews_from_list_applies,
            crews_from_list_fingerprint,
        )

        guard = self.engine.guard
        if not crews_from_list_applies():
            return False
        if self._last_status.get(sid) != status:
            return False
        fp = await asyncio.to_thread(crews_from_list_fingerprint)
        return guard.ask(sid, fp)

    # ── the idle timeout (UpdateSandbox) ─────────────────────────────────────
    def _wait_restart_begins(self, sid: str) -> bool:
        """Poll GetSandbox every 10 s until the status leaves RUNNING; False after 180 s."""
        client = self.engine.client
        waited = 0.0
        while waited < IDLE_RESTART_BEGIN_S:
            self._sleep(POLL_INTERVAL_S)
            waited += POLL_INTERVAL_S
            if str(client.get_sandbox(sid).get("status")) != "RUNNING":
                return True
        return False

    def _idle_error(self, exc: SandboxError) -> str:
        message = exc.message or ""
        if exc.status == 400 and IDLE_SWITCH_MARK in message:
            self._idle_off_at = self._now()  # a 400: the guard does not stop the client for it
            return IDLE_TEXT["off"]
        if exc.status == 409 and IDLE_BUSY_MARK in message:
            return IDLE_TEXT["busy"]
        if exc.status == 409 and IDLE_STOP_FIRST_MARK in message:
            return IDLE_TEXT["stop_first"]
        if exc.kind.startswith(IDLE_NO_ROUTE_KIND):
            return IDLE_TEXT["no_route"]
        if exc.status in (400, 409) and message:
            return message[
                :_ERROR_MAX
            ]  # a range 400, or a 409 not told apart: the service's own words
        return _short(exc)  # a 404 as today's actions; a 403 / 5XX after the guard's stop

    def _run_idle_timeout(self, sid: str, minutes: int) -> None:
        client = self.engine.client
        error = note = ""
        try:
            got = client.update_sandbox(sid, idle_timeout_minutes=minutes)
            if str(got.get("status")) == "RUNNING":
                # RUNNING "until its restart begins" (the model), so wait for the leave first.
                if self._wait_restart_begins(sid):
                    client.wait_running(sid, start_shown=True)
                else:
                    note = IDLE_TEXT["not_begun"]
        except SandboxError as exc:
            error = self._idle_error(exc)
        except (SandboxStateError, GuardRefused, SandboxEngineError) as exc:
            error = _short(exc)
        except Exception as exc:  # the worker must always report back
            logger.exception("sandbox idle timeout on %s failed", sid)
            error = type(exc).__name__
        self.actions.finish(sid, error, note=note)

    async def idle_timeout(self, request: web.Request) -> web.Response:
        sid = request.match_info.get("sandbox_id", "")
        denied = await self._gate(request, "sandboxes_idle_timeout")
        if denied is not None:
            return denied
        # The flag off: absent, as an unmounted route, once the owner check has passed.
        if not _row_lifecycle():
            return web.json_response({"error": "not found"}, status=404)
        if not sandbox_id_from_host(ssh_host_for(sid)) or not self.engine.guard.is_actionable(sid):
            return web.json_response({"error": "not a sandbox of this crew client"}, status=404)
        try:
            body = await request.json()
        except Exception:  # noqa: BLE001 - no JSON is no value
            body = None
        minutes = body.get("minutes") if isinstance(body, dict) else None
        # A JSON integer only: not a bool, a string or a float, even 60.0.
        if type(minutes) is not int or not IDLE_MIN <= minutes <= IDLE_MAX:
            return web.json_response({"error": IDLE_TEXT["range"]}, status=400)
        if not self.actions.begin(sid, IDLE_ACTION):
            return web.json_response(
                {"error": "another action is still running on this sandbox"}, status=409
            )
        self._audit(request.get("user") or "unknown", "sandboxes_idle_timeout", "success", sid)
        if self._run_in_thread:
            threading.Thread(
                target=self._run_idle_timeout, args=(sid, minutes), name="sandbox-idle", daemon=True
            ).start()
        else:
            self._run_idle_timeout(sid, minutes)
        return web.json_response({"ok": True, "action": IDLE_ACTION}, status=202)

    # ── the COMMON_DEPENDENCIES setup step: status, apply (explicit agree), restore ──────────
    def _step_state_dir(self) -> Any:
        from kiro_crew.managed_env.engine import _state_dir

        return _state_dir()

    def _step_sync(self, op: str, body: dict) -> dict:
        client = self.engine.client
        if op == "status":
            try:
                cfg = netcfg_step._read(client)
            except (SandboxError, GuardRefused, NetcfgRefused) as exc:
                return netcfg_step._read_failed(exc).as_dict()
            return {
                "code": "status",
                "ok": True,
                "networkAccess": cfg.access,
                "customDomains": list(cfg.domains),
                "needsAgreement": cfg.access == netcfg_step.MODE_FROM,
                "canRestore": netcfg_step.prior_path(self._step_state_dir()).exists(),
                "dialog": netcfg_step.DIALOG_TEXT,
                "appliesNote": netcfg_step.APPLIES_NOTE,
            }
        if op == "restore":
            return netcfg_step.restore_network_setting(client, self._step_state_dir()).as_dict()
        seen = None
        shown = body.get("seen")
        if (
            isinstance(shown, dict)
            and isinstance(shown.get("networkAccess"), str)
            and isinstance(shown.get("customDomains"), list)
            and all(isinstance(d, str) for d in shown["customDomains"])
        ):
            seen = NetConfig(shown["networkAccess"], tuple(shown["customDomains"]), True)
        agreed = body.get("agree") is True  # only the JSON value true; absent, "true", 1 are all no
        return netcfg_step.ensure_common_dependencies(
            client,
            self._step_state_dir(),
            (lambda _text: agreed) if seen is not None else None,
            seen=seen,
        ).as_dict()

    async def netcfg_step_handler(self, request: web.Request) -> web.Response:
        op = request.match_info.get("op", "")
        denied = await self._gate(request, f"netcfg_step_{op}")
        if denied is not None:
            return denied
        if op not in ("status", "apply", "restore"):
            return web.json_response({"error": f"unknown step op {op!r}"}, status=400)
        if (op == "status") != (request.method == "GET"):  # a write is never a GET
            return web.json_response({"error": "wrong method"}, status=405)
        body: dict = {}
        if op == "apply":
            try:
                body = await request.json()
            except Exception:  # noqa: BLE001
                body = {}
            if not isinstance(body, dict):
                body = {}
        self._audit(request.get("user") or "unknown", f"netcfg_step_{op}", "attempt", "")
        try:
            result = await asyncio.to_thread(self._step_sync, op, body)
        except (SandboxError, GuardRefused, SandboxEngineError) as exc:
            return web.json_response(
                {"error": _short(exc), "kind": getattr(exc, "kind", "")}, status=424
            )
        return web.json_response(result)

    async def shutdown(self, app: Any = None) -> None:
        """aiohttp on_shutdown: stop the sign-in watch."""
        task = getattr(self, "_watch_task", None)
        if task is not None:
            task.cancel()

    # ── the start List and each sign-in landing's List ──────────────
    async def start_watch(self, app: web.Application) -> None:
        """aiohttp on_startup (production only): the first pass is the start List."""
        self._watch_task = asyncio.get_running_loop().create_task(self._crews_watch(app))

    async def _crews_watch(self, app: web.Application) -> None:
        while True:
            try:
                await self.landing_once(app)
            except asyncio.CancelledError:
                raise
            except Exception:
                logger.warning("the sandbox lane's sign-in watch failed once", exc_info=True)
            await asyncio.sleep(self._crews_watch_s or CREWS_WATCH_S)

    async def landing_once(self, app: Any) -> bool:
        """One List of the current user, at start and at each new sign-in (a new vault
        fingerprint), signed in only, under the rule only. Its answer goes
        through the same hold as the list handler's, so a STOPPED registered row is held from the
        start. The fingerprint is read off the loop. True if it listed."""
        fp = await asyncio.to_thread(self._landing_fingerprint)
        if not fp:
            # Signed out, or the rule off: the previous sign-in's ids go now.
            if self._landed_fp:
                self._landed_fp = ""
                await asyncio.to_thread(self.engine.guard.clear_accepted)
            return False
        if fp == self._landed_fp:
            return False
        self._landed_fp = fp
        answer = await asyncio.to_thread(self._landing_list)
        self._follow_answer(getattr(app.get("state"), "instances_manager", None), answer)
        return True

    def _landing_fingerprint(self) -> str:
        from kiro_crew.managed_env.engine import (
            crews_from_list_applies,
            crews_from_list_fingerprint,
        )

        # The switch first: a switched-off home reads nothing in the background.
        if not crews_from_list_applies():
            return ""
        if core_signin.sign_in_status().get("state") != "signed_in":
            return ""
        return crews_from_list_fingerprint()

    def _landing_list(self) -> dict:
        # A new sign-in: the previous one's accepted ids go first, in memory.
        self.engine.guard.clear_accepted()
        self.engine.guard.clear_policy()  # the new sign-in's boxes are asked afresh
        return self.list_sync()

    # ── Rename on a crew's row ───────────────────────────────────────
    async def rename(self, request: web.Request) -> web.Response:
        """The user's Rename on a crew's row, ``{"name": <crew name>}``. A box the List shows
        with a Crew launch's tag gets one UpdateSandbox ``displayName`` = ``<name>-<tag>`` (no
        retry), so every dashboard of the user shows it; the service's answer decides. Its rename
        switch off (the 400) fails the rename here too: nothing is renamed, one line says
        so; an unreadable switch (the 503) changes nothing. A box with no tag has no shared
        name, so it gets no rename and one line, with no call: there is no dashboard-only
        rename. Owner-only, the ledger or the accepted set only.
        Never a power, network or timeout change."""
        sid = request.match_info.get("sandbox_id", "")
        denied = await self._gate(request, "sandboxes_rename")
        if denied is not None:
            return denied
        from kiro_crew.managed_env.engine import crews_from_list_applies

        if not crews_from_list_applies():
            return web.json_response({"error": "not found"}, status=404)
        guard = self.engine.guard
        if not sandbox_id_from_host(ssh_host_for(sid)) or not guard.is_actionable(sid):
            return web.json_response({"error": "not a sandbox of this crew client"}, status=404)
        try:
            body = await request.json()
        except Exception:
            body = None
        name = body.get("name") if isinstance(body, dict) else None
        try:
            validate_crew_name(name if isinstance(name, str) else "")
        except SandboxEngineError as exc:
            return web.json_response({"error": str(exc).split(": ", 1)[-1]}, status=400)
        if sid not in self._display_names:
            return web.json_response(
                {
                    "error": "The sandbox list hasn't been read yet. Try again in a moment.",
                    "kind": "not_listed",
                },
                status=409,
            )
        if sid in self._renaming:
            return web.json_response({"error": "A rename is already running here."}, status=409)
        self._renaming.add(sid)
        self._audit(request.get("user") or "unknown", "sandboxes_rename", "success", sid)
        try:
            if self._run_in_thread:
                result = await asyncio.to_thread(self._rename, sid, str(name))
            else:
                result = self._rename(sid, str(name))
        finally:
            self._renaming.discard(sid)
        return web.json_response(result)

    def _rename(self, sid: str, name: str) -> dict:
        engine = self.engine
        listed = self._display_names.get(sid, "")
        tag = crew_tag_of(listed)
        if not tag:
            # Not a Crew launch's name (a box Set up Crew adopted): no shared name to carry, so
            # no rename at all (no dashboard-only rename anywhere). No call.
            return {"renamed": "no", "line": RENAME_TEXT["no_shared"]}
        if crew_name_of(listed) == name:
            # The shared name already: no same-value update is ever sent.
            _, line = engine.rename_row(sid, name)
            return {"renamed": "unchanged" if not line else "no", "name": name, "line": line}
        try:
            # the row's own checks before the shared rename, so a taken
            # name or a missing row never renames the box everywhere but not here. No call.
            refused = engine.rename_refusal(sid, name)
        except Exception:
            logger.warning("could not read the Remote Crew rows for %s", sid, exc_info=True)
            refused = RENAME_TEXT["no_row"]
        if refused:
            return {"renamed": "no", "line": refused}
        shared = crew_display_name(name, tag)
        try:
            engine.client.rename_sandbox(sid, shared)
        except SandboxError as exc:
            if exc.status == 400 and exc.message == RENAME_OFF_TEXT:
                # the shared rename is off, so the rename fails on this
                # dashboard too. Nothing is renamed; the row keeps its name.
                return {"renamed": "no", "line": RENAME_TEXT["off"]}
            if is_rename_switch_unreadable(exc.op, exc.status, exc.kind, exc.message):
                return {"renamed": "no", "line": RENAME_TEXT["try_later"]}
            return {"renamed": "no", "line": _short(exc)}
        except (GuardRefused, ValueError) as exc:
            return {"renamed": "no", "line": _short(exc)}
        self._display_names[sid] = shared
        try:
            _, line = engine.rename_row(sid, name)
            engine.guard.note_list_rows(names={sid: name})
        except Exception:
            logger.warning("renamed %s, but its row here could not be renamed", sid, exc_info=True)
            line = RENAME_TEXT["no_row"]
        return {"renamed": "everywhere", "name": name, "line": line}

    # ── Start Crew ─────────────────────────────────────────────────────
    async def start_crew(self, request: web.Request) -> web.Response:
        """One ``hook-run`` on a recorded, RUNNING sandbox (case 1c), on the user's click: no
        retry and no port read. The page makes the one Connect after a start.
        No exit starts a sign-in, a device code or an install."""
        sid = request.match_info.get("sandbox_id", "")
        denied = await self._gate(request, "sandboxes_start_crew")
        if denied is not None:
            return denied
        from kiro_crew.managed_env.engine import crews_from_list_applies

        # Behind the switch: with it off the route is absent, as today.
        if not crews_from_list_applies():
            return web.json_response({"error": "not found"}, status=404)
        guard = self.engine.guard
        if not sandbox_id_from_host(ssh_host_for(sid)) or not guard.is_actionable(sid):
            return web.json_response({"error": "not a sandbox of this crew client"}, status=404)
        # Only on a box the last List read RUNNING: not PENDING, STOPPING or STOPPED.
        if self.forwards.holds(sid) or self._last_status.get(sid) != "RUNNING":
            return web.json_response(
                {"error": "sandbox is not RUNNING", "kind": "not_ready"}, status=409
            )
        if sid in self._starting:
            return web.json_response({"error": "Start Crew is already running here."}, status=409)
        self._starting.add(sid)
        self._audit(request.get("user") or "unknown", "sandboxes_start_crew", "success", sid)
        try:
            if self._run_in_thread:
                started, line = await asyncio.to_thread(self.engine.start_crew, sid)
            else:
                started, line = self.engine.start_crew(sid)
        except Exception:
            logger.warning("Start Crew on %s failed", sid, exc_info=True)
            started, line = False, START_CREW_TEXT["other"].format(rc="?")
        finally:
            self._starting.discard(sid)
        return web.json_response({"started": started, "line": line})

    # ── Set up Crew ──────────────────────────────────────────────────────
    async def set_up_crew(self, request: web.Request) -> web.Response:
        """The user's Set up Crew on a listed box with no record: on a RUNNING box the one read
        (``crew-alive``) now, on a STOPPED one a Resume and then the read at RUNNING. Found
        adopts the box (it moves to Your crews); not found is shown again with no call for
        :data:`SET_UP_CACHE_S` in one RUNNING period. No hook, no install, no write on the box."""
        sid = request.match_info.get("sandbox_id", "")
        denied = await self._gate(request, "sandboxes_set_up_crew")
        if denied is not None:
            return denied
        from kiro_crew.managed_env.engine import crews_from_list_applies

        if not crews_from_list_applies():
            return web.json_response({"error": "not found"}, status=404)
        status = self._last_status.get(sid, "")
        if status not in ("RUNNING", "STOPPED"):
            return web.json_response(
                {"error": "sandbox is not RUNNING or STOPPED", "kind": "not_ready"}, status=409
            )
        if not sandbox_id_from_host(ssh_host_for(sid)) or not await self._ask(sid, status):
            return web.json_response({"error": "not a sandbox of this crew client"}, status=404)
        if status == "RUNNING":
            cached = self._setup_line(sid, cached_only=True)
            if cached:
                return web.json_response({"result": "not_found", "line": cached})
        kind = "resume" if status == "STOPPED" else "check"
        if sid in self._checking or not self.actions.begin(sid, kind):
            return web.json_response(
                {"error": "another action is still running on this sandbox"}, status=409
            )
        self._audit(request.get("user") or "unknown", "sandboxes_set_up_crew", "success", sid)
        if status == "STOPPED":
            self._start_action(sid, "resume", then_read=True)
            return web.json_response({"ok": True, "action": "resume"}, status=202)
        self._checking.add(sid)
        try:
            if self._run_in_thread:
                result = await asyncio.to_thread(self._read_and_adopt, sid)
            else:
                result = self._read_and_adopt(sid)
        finally:
            self._checking.discard(sid)
            self.actions.finish(sid, "")
        line = self._setup_line(sid) if result == "not_found" else SET_UP_CREW_TEXT[result]
        return web.json_response({"result": result, "line": line})

    async def ssh_session(self, request: web.Request) -> web.Response:
        """One StartSshSession for an own, RUNNING sandbox; never resumes. A user-asked id
        only with the one read's armed ticket, which this takes (one use)."""
        denied = await self._gate(request, "sandboxes_ssh_session")
        if denied is not None:
            return denied
        sid = request.match_info.get("sandbox_id", "")
        guard = self.engine.guard
        ticket = False
        if sandbox_id_from_host(ssh_host_for(sid)) and not guard.is_actionable(sid):
            ticket = guard.is_asked(sid) and guard.take_ticket(sid)
        if not sandbox_id_from_host(ssh_host_for(sid)) or not (ticket or guard.is_actionable(sid)):
            return web.json_response({"error": "not a sandbox of this crew client"}, status=404)
        if self.forwards.holds(sid):
            # The list or the Stop read it STOPPING or STOPPED: refused from memory, no service call.
            self._audit(request.get("user") or "unknown", "sandboxes_ssh_session", "held", sid)
            return web.json_response(
                {"error": "sandbox is not RUNNING", "kind": "not_ready"}, status=409
            )
        try:
            region = lane_settings.read_settings().require_ssh()
        except SettingsMissing as exc:
            return web.json_response({"error": str(exc), "kind": "not_configured"}, status=409)
        self._audit(request.get("user") or "unknown", "sandboxes_ssh_session", "success", sid)
        self.phases.asked(sid)
        served = False
        try:
            tup = await asyncio.to_thread(
                functools.partial(self.engine.client.start_ssh_session, sid, read_ticket=ticket)
            )
            served = True
        except SandboxError as exc:
            if exc.status == 409:
                return web.json_response(
                    {"error": "sandbox is not RUNNING", "kind": "not_ready"}, status=409
                )
            if is_policy_refusal(exc.op, exc.status, exc.kind):
                # a stable kind, so the mint prints the plain line as it is
                answer = {"error": POLICY_TEXT, "kind": POLICY_KIND_WORD}
                return web.json_response(answer, status=424)
            return web.json_response({"error": _short(exc), "kind": exc.kind}, status=424)
        except GuardRefused as exc:
            if exc.reason == POLICY_KIND_WORD:  # inside the box's hold, no call was made
                answer = {"error": POLICY_TEXT, "kind": POLICY_KIND_WORD}
                return web.json_response(answer, status=422)
            return web.json_response({"error": _short(exc), "kind": "refused"}, status=422)
        except SandboxEngineError as exc:
            # No sign-in yet (the startup auto-connect runs before it): a plain refusal, not a 500.
            if exc.code == "not_signed_in":
                return web.json_response(
                    {"error": "not signed in", "kind": "not_signed_in"}, status=409
                )
            return web.json_response({"error": _short(exc), "kind": exc.code}, status=424)
        finally:
            (self.phases.served if served else self.phases.failed)(sid)
        return web.json_response(
            {
                "ssmSessionId": tup.ssm_session_id,
                "streamUrl": tup.stream_url,
                "tokenValue": tup.token_value,
                "region": region,
            }
        )

    async def connect_phase(self, request: web.Request) -> web.Response:
        """How far this sandbox's connect has got (:class:`_ConnectPhases`), from this process's
        memory: no engine, no client, no service call. A sandbox with no open window reads ``""``,
        and so does one this client never asked a tuple for."""
        sid = request.match_info.get("sandbox_id", "")
        denied = await self._gate(request, "sandboxes_connect_phase")
        if denied is not None:
            return denied
        # The flag off: absent, as an unmounted route, once the owner check has passed.
        if not _row_lifecycle():
            return web.json_response({"error": "not found"}, status=404)
        if not sandbox_id_from_host(ssh_host_for(sid)):
            return web.json_response({"error": "not a sandbox of this crew client"}, status=404)
        return web.json_response({"phase": self.phases.phase(sid)})


def _stopped_line(by_service: bool) -> str:
    return stopped_line(by_service)


#: the List's retry waits, in seconds, after 1, 2, 3 and 4+ outages in a row.
LIST_BACKOFF_S = (15.0, 30.0, 60.0, 120.0)


def _list_outage(exc: Exception) -> bool:
    """A List the service couldn't answer (a 5XX, or no answer at all): retried on the
    backoff, never a stop."""
    return (
        isinstance(exc, SandboxError)
        and exc.op == "ListSandboxes"
        and ((exc.status or 0) >= 500 or exc.kind == "unreachable")
    )


def _short(exc: Exception) -> str:
    if isinstance(exc, ClientStopped):
        return stopped_line(exc.by_service)
    if isinstance(exc, SandboxError) and is_policy_refusal(exc.op, exc.status, exc.kind):
        return POLICY_TEXT  # the organization's policy refused this box's action
    if isinstance(exc, SandboxError) and exc.op == "ResumeSandbox" and is_resume_not_ready(exc):
        return RESUME_BUSY_TEXT  # the retry ran out; plain words, no op name or code
    if isinstance(exc, SandboxEngineError) and exc.code == "not_signed_in" and exc.message:
        return exc.message[:_ERROR_MAX]  # the plain line, without its code
    return str(exc)[:_ERROR_MAX]


def _minutes(value: Any) -> Optional[int]:
    """The list's ``idleTimeoutMinutes``: an int, or None when absent or not an int."""
    return value if type(value) is int else None


#: How often the watch reads the vault fingerprint (a file read, no service call) for a new
#: sign-in. The first pass, at start, is the start List.
CREWS_WATCH_S = 15.0


def register_sandbox_routes(
    app: web.Application, routes: Optional[SandboxRoutes] = None
) -> SandboxRoutes:
    handlers = routes or SandboxRoutes(crews_watch_s=CREWS_WATCH_S)
    app.router.add_get("/api/managed-environments/list", handlers.list_handler)
    app.router.add_get("/api/managed-environments/row-lifecycle", handlers.row_lifecycle_handler)
    app.router.add_get("/api/managed-environments/signed-in-user", handlers.signed_in_user_handler)
    app.router.add_get("/api/managed-environments/network-step/{op}", handlers.netcfg_step_handler)
    app.router.add_post("/api/managed-environments/network-step/{op}", handlers.netcfg_step_handler)
    app.on_shutdown.append(handlers.shutdown)
    if handlers._crews_watch_s:
        app.on_startup.append(handlers.start_watch)
    app.router.add_post("/api/managed-environments/{sandbox_id}/ssh-session", handlers.ssh_session)
    app.router.add_get(
        "/api/managed-environments/{sandbox_id}/connect-phase", handlers.connect_phase
    )
    # before the {action} route, which would take it as an unknown action
    app.router.add_post(
        "/api/managed-environments/{sandbox_id}/idle-timeout", handlers.idle_timeout
    )
    app.router.add_post("/api/managed-environments/{sandbox_id}/start-crew", handlers.start_crew)
    app.router.add_post("/api/managed-environments/{sandbox_id}/rename", handlers.rename)
    app.router.add_post("/api/managed-environments/{sandbox_id}/set-up-crew", handlers.set_up_crew)
    app.router.add_post("/api/managed-environments/{sandbox_id}/{action}", handlers.act)
    return handlers
