"""Count repeated app token exchange refusals instead of writing a row for each.

``POST /api/apps/<name>/token`` refuses a missing or wrong ``X-App-Secret``. An app
whose secret went stale keeps retrying as often as it polls, and one audit row per
refusal let a single app rotate the security event log every few hours, pushing every
other event out of the retained segments. A streak of identical refusals (same app,
same reason, same ``forwarded`` verdict) is written as:

* its first refusal, a row as before, answered 403;
* every later refusal counted instead of written, and answered 429 ``auth_throttled``
  with ``Retry-After``, which is how the gateway's other self-authenticating routes
  answer repeated failed auth. The request audit writes no row for it either;
* one summary row at the end of each :data:`WINDOW_SECS` window that counted any,
  carrying the count and the first and last counted times. A whole window with no
  refusal ends the streak, so the next refusal is a first row again.

The name comes from the URL, so a caller can send a new one with every request. A name
with no owner grant (``apps.host_exec_grants``) is therefore counted under one key,
:data:`NOT_ENABLED_KEY`, and new names write no first row each. Which status a refusal
gets is still decided by its own name, whether granted or not, so the answer does not
tell a caller which names the owner enabled.

A grant writes the app's pending summaries before its own row. Every planned end of
the process writes whatever is pending and stops counting (:func:`close`, wired by
:func:`install`): the dashboard's ``on_cleanup`` on a normal shutdown, and the
pre-exec seam on the in-place restart and on an exit whose shutdown ran out of time,
which both skip that hook and then drain the SEL queue. The owner's
``/kirocrew restart`` calls :func:`close` before its own drain. The routes still
serve until the process ends, so a refusal after that write is written and answered
403 like a first one, and no count is left in memory for the exit to drop. Only a
crash or the second-signal force exit loses counts, at most one window of them.

The secret is checked on every request before this module is asked, so a corrected
secret is granted at once, streak or not, and a caller sending wrong secrets cannot
lock the real app out. Like the webhook failed-auth throttle this is in memory and
per process: it bounds audit volume and is not a security boundary. It is mutated
only on the event loop.
"""

from __future__ import annotations

import asyncio
import logging
import time
from dataclasses import dataclass
from datetime import datetime, timezone

from aiohttp import web

from kiro_crew.apps.manifest_names import APP_NAME_MAX_LENGTH
from kiro_crew.dashboard.origin import mark_audit_counted
from kiro_crew.pre_exec_stops import register_pre_exec_stop

logger = logging.getLogger(__name__)

#: This module's name on the pre-exec seam (``kiro_crew.pre_exec_stops``).
PRE_EXEC_STOP_NAME = "App token refusal summaries"

#: One counting window. A streak writes at most one summary row per window.
WINDOW_SECS = 300.0
#: ``Retry-After`` on a repeated refusal, so an app that honours it tries once a minute.
RETRY_AFTER_SECS = 60
#: Entries in each table. The app name comes from the URL, so a caller can mint keys of
#: :data:`_answered`; at its bound that table starts empty, which writes nothing and
#: only answers each name's next refusal 403 again. :data:`_streaks` holds granted
#: names and :data:`NOT_ENABLED_KEY` only; at its bound every pending count is written
#: and it starts empty. Each key keeps at most ``APP_NAME_MAX_LENGTH`` characters of the
#: name, which is as long as an installed app's name can be, and a reason from the
#: handler's fixed set.
_MAX_STREAKS = 1024

#: The name every refusal of a name with no owner grant is counted under. An app name
#: is kebab-case (``manifest_names.KEBAB_RE``), so no granted app has this name.
NOT_ENABLED_KEY = "(app not enabled)"

#: (app name, refusal reason, ``metadata.forwarded`` of the refused request).
_Key = tuple[str, str, "bool | None"]


@dataclass
class _Streak:
    window_start: float
    last_seen: float
    count: int = 0
    first_at: str = ""
    last_at: str = ""


#: Counting streaks: a granted app's under its own name, every other name's under
#: :data:`NOT_ENABLED_KEY`. A streak decides whether a refusal is written or counted.
_streaks: dict[_Key, _Streak] = {}
#: Monotonic time of each name's last refusal, granted or not. Decides 403 or 429.
_answered: dict[_Key, float] = {}
_flush_handle: asyncio.TimerHandle | None = None
_flush_loop: asyncio.AbstractEventLoop | None = None
#: Set by :func:`close`. From then on no refusal is counted.
_closed = False


def refuse(
    request: web.Request,
    app_name: str,
    reason: str,
    code: str,
    hint: str | None = None,
    *,
    enabled: bool,
) -> web.Response:
    """Answer one refused exchange: a 403 or a 429, written as a row or counted.

    *reason* is the row's ``error``, the body's ``error`` and part of both keys.
    *enabled* is whether *app_name* holds the owner's grant; a name without one is
    counted under :data:`NOT_ENABLED_KEY`. *code* is the 403's ``code``; the 429 says
    ``auth_throttled`` and keeps *hint*. The 429 answers a name refused again inside a
    window. A counted refusal marks *request* so the request audit writes no row of its
    own for it (``origin.AUDIT_COUNTED_KEY``): the window's summary row records it.
    After :func:`close` every refusal is a row and a 403.
    """
    repeat = counted = False
    if not _closed:
        # circular import: sel_request_provenance -> sel -> dashboard
        from kiro_crew.sel_request_provenance import current_request_forwarded

        forwarded = current_request_forwarded()
        name = app_name[:APP_NAME_MAX_LENGTH]
        repeat = _answered_again((name, reason, forwarded))
        counted = _is_repeat((name if enabled else NOT_ENABLED_KEY, reason, forwarded))
    if counted:
        mark_audit_counted(request)
    else:
        # Late-bound so a test's patch of ``kiro_crew.sel.sel`` reaches it.
        from kiro_crew.sel import sel

        sel().log_api_access(
            caller=app_name,
            operation="app_token_exchange",
            outcome="denied",
            source="app_auth",
            error=reason,
        )
    if repeat:
        return web.json_response(
            {
                "error": reason,
                "code": "auth_throttled",
                "hint": hint,
                "retryAfter": RETRY_AFTER_SECS,
            },
            status=429,
            headers={"Retry-After": str(RETRY_AFTER_SECS)},
        )
    return web.json_response({"error": reason, "code": code, "hint": hint}, status=403)


def release(app_name: str) -> None:
    """Write *app_name*'s pending summaries and end its streaks. Called on a grant."""
    name = app_name[:APP_NAME_MAX_LENGTH]
    for key in [k for k in _streaks if k[0] == name]:
        _end(key)
    for key in [k for k in _answered if k[0] == name]:
        del _answered[key]


def write_pending() -> None:
    """Write every pending summary and forget every streak."""
    global _flush_handle
    for key in list(_streaks):
        _end(key)
    if _flush_handle is not None:
        _flush_handle.cancel()
        _flush_handle = None


def close() -> None:
    """Write every pending summary and count no refusal after it. For a planned exit."""
    global _closed
    _closed = True
    write_pending()
    _answered.clear()


async def write_pending_at_exit(_app: web.Application | None = None) -> None:
    """``on_cleanup`` hook and pre-exec stop: :func:`close`.

    Safe to repeat. With nothing pending it writes nothing.
    """
    close()


def install(app: web.Application) -> None:
    """Write the pending summaries on every planned end of the process.

    ``on_cleanup`` runs on a normal shutdown. The in-place restart's exec and the exit
    after a shutdown that ran out of time skip it, and both run the pre-exec seam
    before they drain the SEL queue, so the stop stays registered for the process.
    """
    app.on_cleanup.append(write_pending_at_exit)
    register_pre_exec_stop(PRE_EXEC_STOP_NAME, write_pending_at_exit)


def flush_due() -> None:
    """Write a summary for every window that has ended. The timer calls this."""
    global _flush_handle
    _flush_handle = None
    now = time.monotonic()
    for key, streak in list(_streaks.items()):
        if now - streak.window_start < WINDOW_SECS:
            continue
        if streak.count:
            _close_window(key, streak, now)
        elif now - streak.last_seen >= WINDOW_SECS:
            del _streaks[key]
    for key, last in list(_answered.items()):
        if now - last >= WINDOW_SECS:
            del _answered[key]
    pending = [s.window_start + WINDOW_SECS - now for s in _streaks.values() if s.count]
    if pending:
        _arm_flush(min(pending))


def _answered_again(key: _Key) -> bool:
    """Record one refusal of a name; ``True`` when it refused within a window."""
    now = time.monotonic()
    last = _answered.get(key)
    if last is None and len(_answered) >= _MAX_STREAKS:
        # Statuses only: no count is lost, and every name's next refusal is a 403.
        _answered.clear()
    _answered[key] = now
    return last is not None and now - last < WINDOW_SECS


def _is_repeat(key: _Key) -> bool:
    """Record one refusal in its streak; ``True`` when it joined one and is not written."""
    now = time.monotonic()
    streak = _streaks.get(key)
    if streak is not None and now - streak.last_seen >= WINDOW_SECS:
        _end(key)
        streak = None
    if streak is None:
        if len(_streaks) >= _MAX_STREAKS:
            write_pending()
        _streaks[key] = _Streak(window_start=now, last_seen=now)
        return False
    if now - streak.window_start >= WINDOW_SECS:
        _close_window(key, streak, now)
    stamp = datetime.now(tz=timezone.utc).isoformat()
    if not streak.count:
        streak.first_at = stamp
    streak.count += 1
    streak.last_at = stamp
    streak.last_seen = now
    _arm_flush(streak.window_start + WINDOW_SECS - now)
    return True


def _end(key: _Key) -> None:
    streak = _streaks.pop(key, None)
    if streak is not None and streak.count:
        _write_summary(key, streak)


def _close_window(key: _Key, streak: _Streak, now: float) -> None:
    if streak.count:
        _write_summary(key, streak)
    streak.window_start = now
    streak.count = 0
    streak.first_at = streak.last_at = ""


def _write_summary(key: _Key, streak: _Streak) -> None:
    from kiro_crew.sel import sel

    app_name, reason, forwarded = key
    try:
        sel().log_api_access(
            caller=app_name,
            operation="app_token_exchange",
            outcome="denied",
            source="app_auth",
            resources=f"repeated {streak.count} times",
            error=reason,
            forwarded=forwarded,
            metadata={
                "repeated": streak.count,
                "first_at": streak.first_at,
                "last_at": streak.last_at,
            },
        )
    except Exception:
        # Best-effort like every api_access row; the other streaks still get theirs.
        logger.warning("could not write a repeated app token refusal summary", exc_info=True)


def _arm_flush(delay: float) -> None:
    global _flush_handle, _flush_loop
    try:
        loop = asyncio.get_running_loop()
    except RuntimeError:
        # No loop to time it: the streak's next refusal or grant closes the window.
        return
    delay = max(delay, 0.0)
    handle = _flush_handle
    if handle is not None:
        if _flush_loop is loop and not handle.cancelled() and handle.when() <= loop.time() + delay:
            return
        handle.cancel()
    _flush_handle = loop.call_later(delay, flush_due)
    _flush_loop = loop


def _reset() -> None:
    """Forget every streak without writing it, and count again. Test-only."""
    global _flush_handle, _flush_loop, _closed
    if _flush_handle is not None:
        _flush_handle.cancel()
    _flush_handle = None
    _flush_loop = None
    _closed = False
    _streaks.clear()
    _answered.clear()
