"""The Discord REST request ladder: one call through Discord's own rate-limit accounting.

``_api_request`` runs every outbound call: it waits out a bucket with nothing left and
any GLOBAL hold, sends, learns the route's bucket from the response headers, backs off a
429 or a 5xx within that failure class's own retry budget, stops itself with the
invalid-request breaker far short of Discord's IP block, and answers a
:class:`DiscordApiResult` saying whether a failure is worth another attempt. Also here:
the route key a bucket is charged to, the bounded most-recently-used store the maps
share, the outcome and edit classifications and the shared aiohttp session. Each wait
the ladder serves is paired with the destination re-check in
:mod:`kiro_crew.discord.client_recheck`. Each function here whose first parameter is
``self`` is bound as the ``DiscordClient`` attribute of the same name in
:mod:`kiro_crew.discord.client`, which keeps the client's state, so ``self.<name>(...)``
calls and instance and class patches reach it as they reach a method defined in the
class body. They read ``_breaker_until``, ``_global_ready_at``, ``_proxy``,
``_session``, ``_session_lock`` and ``_token``, write ``_breaker_until``,
``_global_ready_at`` and ``_session``, and update ``_holds``, ``_invalid_hits`` and
``_route_buckets`` in place. New ladder work goes here; the REST verbs, the bodies they
send and the ``api_json`` / ``api_files`` entry points stay on
:class:`~kiro_crew.discord.client.DiscordClient`.
"""

from __future__ import annotations

import asyncio
import logging
import math
import re
import time
from collections.abc import Mapping
from dataclasses import dataclass
from typing import TYPE_CHECKING, Any, Callable, TypeVar

import aiohttp

if TYPE_CHECKING:
    from kiro_crew.discord.client import DiscordClient

logger = logging.getLogger("kiro_crew.discord.client")


_T = TypeVar("_T")


# Sniffed MIME determines the canonical inline-rendering extension.
_API_BASE = "https://discord.com/api/v10"


# ── REST rate-limit accounting ─────────────────────────────────────────────
#
# Discord answers every REST call with the state of the bucket it charged, and
# punishes an app that ignores it: 10,000 responses of status 401, 403 or 429
# within 10 minutes gets the IP temporarily blocked, which costs every channel
# on the host rather than the one route that misbehaved. So the ladder spends
# the accounting it is handed (wait out a bucket that has nothing left, hold
# every route while a GLOBAL limit is in force) and stops itself far short of
# that ceiling instead of discovering it.

#: Bucket state, present on every response. ``Reset-After`` is FRACTIONAL
#: seconds; ``Bucket`` is the opaque id of the limit the route was charged to,
#: and several routes share one, so it is what state is keyed by.
_HDR_BUCKET = "X-RateLimit-Bucket"
_HDR_REMAINING = "X-RateLimit-Remaining"
_HDR_RESET_AFTER = "X-RateLimit-Reset-After"
#: 429 only: ``user`` | ``global`` | ``shared``.
_HDR_SCOPE = "X-RateLimit-Scope"
#: Whole seconds, which is why the 429 body's fractional ``retry_after`` wins
#: when both are present.
_HDR_RETRY_AFTER = "Retry-After"
_SCOPE_GLOBAL = "global"
#: A 429 on a limit Discord shares between apps is not attributed to us and is
#: excluded from the invalid-request ban count, so it must not trip the breaker.
_SCOPE_SHARED = "shared"

#: Back-off floor, so a ``retry_after`` of 0 cannot become a hot loop.
_MIN_HOLD_SECS = 0.5
#: Ceiling on the back-off served INSIDE a call after a route-scoped 429. A
#: route's allowance resets in seconds; past this, returning a transient
#: failure the caller can re-drive beats parking the turn in the transport.
_MAX_RETRY_AFTER_SECS = 5.0
#: Ceiling on a wait taken BEFORE issuing a call, which may run longer than the
#: one above because waiting is strictly cheaper than the 429 it prevents: the
#: 429 spends ban budget shared with every other route, the wait spends nothing.
_MAX_PREEMPT_SECS = 10.0
#: Ceiling on a GLOBAL hold. Highest of the three because the app's whole
#: 50 requests/second allowance is what ran out: continuing to send through it
#: is precisely what earns the IP block, so waiting is the cheaper failure.
_MAX_GLOBAL_HOLD_SECS = 30.0
#: Back-off used when a 429 names no usable delay.
_DEFAULT_RETRY_AFTER_SECS = 1.0

#: Retries per failure class, deliberately different. A 429 already told us how
#: long to wait, so one honoured retry either lands or the route is genuinely
#: saturated, and every further 429 spends ban budget. A 5xx or a connector
#: blip spends none and is far likelier to clear, so it gets the same
#: three-attempt budget as the Slack DM path in ``slack/retry.py``.
_RATE_LIMIT_RETRIES = 1
_TRANSIENT_RETRIES = 2
#: Linear back-off step for the transient class (1s, then 2s), matching that
#: same path so the two cannot drift for opposite reasons.
_TRANSIENT_BACKOFF_SECS = 1.0

#: Statuses that spend Discord's invalid-request budget.
_INVALID_STATUSES = frozenset({401, 403, 429})
#: Discord's own ceiling, named so the breaker's margin is legible.
_DISCORD_INVALID_BAN_LIMIT = 10_000
#: Discord counts invalid responses over 10 minutes, so the breaker uses the
#: same window and trips at 5% of the ceiling: a runaway loop is stopped with
#: the budget almost untouched, and no realistic legitimate burst reaches it.
_INVALID_WINDOW_SECS = 600.0
_INVALID_LIMIT = 500
#: How long the breaker refuses to issue anything once tripped. Long enough
#: that the window drains meaningfully, short enough that a channel recovers
#: without an operator.
_BREAKER_COOLOFF_SECS = 120.0

#: Upper bound on tracked routes and buckets. The route space is small but open
#: (one entry per verb per channel), so the maps are capped least-recently-used.
_MAX_TRACKED_ROUTES = 256


#: Path segments whose FOLLOWING id is one of Discord's "major parameters":
#: those buckets are per-id, everything else shares one bucket across ids, so
#: the route key keeps the majors and collapses the rest.
_MAJOR_PARAM_PARENTS = frozenset({"channels", "guilds", "webhooks"})
_ID_SEGMENT_RE = re.compile(r"^\d{5,}$")
#: Parents whose next path segment is a CREDENTIAL, not an id: an interaction
#: token and a webhook token both authorize the call that carries them. They are
#: collapsed structurally rather than by the length rule below, because the route
#: key is what gets logged and a length threshold is the wrong thing to rest a
#: credential on: a short or changed token would print verbatim into the log ring
#: and into `kirocrew logs`.
_TOKEN_SEGMENT_PARENTS = frozenset({"interactions", "webhooks"})
#: An interaction token rides in the path and is unique per interaction, so a
#: verbatim route key would grow one entry per button press. No literal segment
#: in Discord's API is anywhere near this long.
_MAX_LITERAL_SEGMENT = 24

#: ``DiscordApiResult.outcome``: the request was delivered.
DISCORD_OK = "ok"
#: A 4xx other than 429. The same request will fail identically forever (a
#: missing permission, a deleted message, a malformed payload), so retrying it
#: only spends ban budget. This is ``slack/retry.py``'s classification rule,
#: kept identical here so the two channels cannot drift.
DISCORD_PERMANENT = "permanent"
#: A 429, a 5xx, a timeout or a connector error whose retry budget is spent.
#: The same request may well succeed later.
DISCORD_TRANSIENT = "transient"
#: Refused locally by the invalid-request breaker, without touching the
#: network. Transient in nature, but distinct because nothing was sent.
DISCORD_BLOCKED = "blocked"

#: Classified outcomes of an in-place message edit, for the seam grader.
EDIT_OK = "ok"
#: The message is gone (a moderator/automod deletion -> 404). A fresh
#: POST replacing it lands below the message that was ABOVE the deleted bubble.
EDIT_GONE = "gone"
#: A transient edit failure (429/5xx/timeout); the bubble is still on screen, so
#: a fresh POST lands below THAT frame.
EDIT_FAILED = "failed"


def _rate_limit_headers(resp: Any) -> Mapping[str, str]:
    """Response headers as a mapping, empty when there are none to read.

    Discord sends the bucket headers on every REST response, but an
    intercepting proxy's error page need not: a response without them simply
    carries no accounting information, and must not turn a delivered request
    into a failed one.
    """
    headers = getattr(resp, "headers", None)
    return headers if isinstance(headers, Mapping) else {}


def _coerce_float(raw: Any) -> float | None:
    """A numeric field as a FINITE float, or None when it is absent or not one.

    Every rate-limit number Discord sends, in a header or a 429 body, reaches a
    sleep duration through here, so the finiteness check belongs at this one
    chokepoint rather than at each clamp. ``float("nan")`` and ``float("inf")``
    both parse, and NaN then survives every guard downstream: ``nan <= 0`` is
    False so the "no hold needed" branch does not take it, and
    ``min(max(nan, floor), ceiling)`` is still NaN, so a single malformed or
    hostile header would become ``asyncio.sleep(nan)`` and wedge the client for
    the life of the process. Treating a non-finite value as absent falls back to
    the documented default instead.
    """
    if isinstance(raw, bool) or raw is None:
        return None
    try:
        value = float(raw)
    except (TypeError, ValueError):
        return None
    return value if math.isfinite(value) else None


def _header_float(headers: Mapping[str, str], name: str) -> float | None:
    return _coerce_float(headers.get(name))


def _header_int(headers: Mapping[str, str], name: str) -> int | None:
    value = _coerce_float(headers.get(name))
    return None if value is None else int(value)


def _remember(
    store: dict[str, _T], key: str, value: _T, cap: int = _MAX_TRACKED_ROUTES
) -> list[str]:
    """Insert into a bounded most-recently-used map. Returns the keys it dropped.

    Dropping the least recently touched entry costs only the pre-emption it
    carried: the next call on that route earns a 429 and re-learns its bucket,
    which is exactly the behaviour with no accounting at all. An unbounded map
    in a process that runs for weeks is the worse failure.

    ``cap`` lets a caller retaining something other than routes name its own
    bound, so every store that grows has one. The dropped keys are returned
    because for some stores the eviction changes what a later refusal MEANS, and
    a caller that must say so cannot see it otherwise; a caller that does not
    care ignores the value, as the route store does.
    """
    store.pop(key, None)
    store[key] = value
    dropped: list[str] = []
    while len(store) > cap:
        oldest = next(iter(store))
        del store[oldest]
        dropped.append(oldest)
    return dropped


#: URL path separator for a Discord REST route. Fixed by the URL grammar, so
#: it is deliberately NOT a filesystem path join.
_ROUTE_SEP = "/"


def _route_key(method: str, path: str) -> str:
    """Collapse a concrete path onto the rate-limit route it belongs to.

    Discord buckets a route per "major parameter" (the channel, guild or
    webhook id) and shares one bucket across every other id, so
    ``/channels/A/messages/1`` and ``/channels/A/messages/2`` are one route
    while channel ``B`` is another.

    ``path`` is a REST route, not a filesystem path, so the separator is fixed by
    the URL grammar and named here rather than spelled inline: `os.path.join` /
    `pathlib` would emit a backslash on Windows and silently stop matching any
    bucket. The portability gate cannot tell the two apart from a line regex.
    """
    segments = path.split(_ROUTE_SEP)
    out: list[str] = []
    for index, segment in enumerate(segments):
        parent = segments[index - 1] if index else ""
        if _ID_SEGMENT_RE.match(segment):
            out.append(segment if parent in _MAJOR_PARAM_PARENTS else "{id}")
        elif index >= 2 and segments[index - 2] in _TOKEN_SEGMENT_PARENTS:
            # `/interactions/{id}/{token}/callback` and
            # `/webhooks/{id}/{token}` put the credential two segments after the
            # parent, so the grandparent is what identifies it.
            out.append("{token}")
        elif len(segment) > _MAX_LITERAL_SEGMENT:
            out.append("{token}")
        else:
            out.append(segment)
    return f"{method} {_ROUTE_SEP.join(out)}"


def _is_global_limit_exempt(path: str) -> bool:
    """True for the routes Discord exempts from the app's global allowance.

    Interaction callbacks and their followups are exempt, and they answer a
    ~3 second deadline: holding one behind a global limit it does not consume
    would leave the user's client spinning for nothing.
    """
    return path.startswith("/interactions/") or path.startswith("/webhooks/")


@dataclass(frozen=True)
class DiscordApiResult:
    """One REST call's outcome.

    A bare ``None`` could only say "something went wrong", which is how a turn
    whose entire output failed to send gets recorded as a delivered turn. This
    record says WHICH kind of wrong, so a caller can distinguish a refusal that
    will repeat forever (``DISCORD_PERMANENT``: report it, do not re-send) from
    one worth another attempt later (``DISCORD_TRANSIENT`` / ``DISCORD_BLOCKED``).

    ``data`` is the parsed JSON body (``{}`` for a 204 or an undecodable 2xx)
    and is ``None`` on every failure, so both historical body tests
    (``if body:`` and ``body is not None``) keep meaning "did it land".
    """

    outcome: str
    data: Any = None
    #: HTTP status, ``0`` when no response was received (timeout, connector
    #: error, or a request the breaker refused to issue).
    status: int = 0
    #: Discord's own JSON error code, ``0`` when the body carried none.
    code: int = 0
    #: Short operator-facing reason. Never carries the bot token.
    detail: str = ""

    def __bool__(self) -> bool:
        """True only for a delivered request."""
        return self.outcome == DISCORD_OK

    @property
    def retryable(self) -> bool:
        """True when the identical request could succeed on a later attempt."""
        return self.outcome in (DISCORD_TRANSIENT, DISCORD_BLOCKED)

    @property
    def message_id(self) -> str:
        """The created/edited message's snowflake, empty when none was returned."""
        return str(self.data.get("id", "")) if isinstance(self.data, dict) else ""


def _failed(outcome: str, *, status: int = 0, code: int = 0, detail: str = "") -> DiscordApiResult:
    """A failure result. ``data`` stays None so body checks read as "no send"."""
    return DiscordApiResult(outcome=outcome, status=status, code=code, detail=detail)


def _classify_edit(result: DiscordApiResult) -> str:
    """``EDIT_OK`` / ``EDIT_GONE`` / ``EDIT_FAILED`` for an in-place edit result.

    GONE is a 404 -- Discord maps a deleted message to a non-retryable permanent
    result (``retry cannot undelete a message``), so the bubble is off screen and
    a replacement send lands below the message ABOVE it. Every other failure is
    treated as still-on-screen (a transient 429/5xx, a local breaker block, a
    timeout): fail-safe, because grading a fresh send against the still-visible
    bubble's own frame is the same no-op the successful-edit path already relies
    on, whereas mistaking a transient failure for GONE would drop a real seam.
    """
    if bool(result):
        return EDIT_OK
    if result.status == 404:
        return EDIT_GONE
    return EDIT_FAILED


async def _ensure_session(self: DiscordClient) -> aiohttp.ClientSession:
    """Return the shared ClientSession, creating it once on demand
    (double-checked under a lock — REST calls run concurrently with the
    gateway loop)."""
    if self._session is None or self._session.closed:
        async with self._session_lock:
            if self._session is None or self._session.closed:
                self._session = aiohttp.ClientSession()
    return self._session


def _hold_key(self: DiscordClient, route: str) -> str:
    """State key for ``route``: Discord's bucket id once a response has
    named one, so routes SHARING a bucket pre-empt each other, and the
    route itself until then."""
    return self._route_buckets.get(route) or route


def _set_hold(self: DiscordClient, key: str, delay: float) -> None:
    """Hold ``key`` for at least ``delay`` seconds, never shortening an
    existing hold: two responses can disagree, and the later deadline is
    the one that keeps us out of a 429."""
    deadline = time.monotonic() + delay
    _remember(self._holds, key, max(self._holds.get(key, 0.0), deadline))


def _note_headers(self: DiscordClient, route: str, headers: Mapping[str, str]) -> None:
    """Learn the route's bucket, and pre-empt when the bucket is spent.

    A response saying ``Remaining: 0`` is Discord telling us the next call
    on this bucket will be a 429. Recording the reset here is what turns
    that into a wait instead of a refusal.
    """
    bucket = str(headers.get(_HDR_BUCKET, "") or "")
    if bucket:
        _remember(self._route_buckets, route, bucket)
    remaining = _header_int(headers, _HDR_REMAINING)
    reset_after = _header_float(headers, _HDR_RESET_AFTER)
    if remaining is None or remaining > 0 or reset_after is None or reset_after <= 0:
        return
    self._set_hold(bucket or route, min(reset_after, _MAX_PREEMPT_SECS))


async def _await_capacity(self: DiscordClient, route: str, *, exempt: bool) -> float:
    """Wait out a known-spent bucket, and any global hold, before sending.

    Returns the seconds actually slept, ``0.0`` when nothing was due, so the
    caller can tell a suspended attempt from an immediate one and re-read
    authorization only for the former.

    ``exempt`` routes skip the GLOBAL hold only: Discord does not charge
    interaction callbacks to the app's 50 requests/second allowance, so
    holding them there would blow their ~3 second deadline for a limit they
    never consumed. Their own bucket still applies.
    """
    deadlines = [self._holds.get(self._hold_key(route), 0.0)]
    if not exempt:
        deadlines.append(self._global_ready_at)
    wait = max(deadlines) - time.monotonic()
    if wait > 0:
        await asyncio.sleep(wait)
        return wait
    return 0.0


def _note_rate_limit(
    self: DiscordClient, route: str, data: Any, headers: Mapping[str, str]
) -> float:
    """Record a 429 and return the back-off this attempt must serve.

    The body's ``retry_after`` is fractional seconds while the
    ``Retry-After`` header is rounded to whole seconds, so the body wins
    when both are present. A GLOBAL 429 is the app's entire allowance, not
    this route's, so it holds EVERY non-exempt route: letting the other
    routes keep firing is how one rate limit becomes an IP block.
    """
    body = data if isinstance(data, dict) else {}
    retry_after = _coerce_float(body.get("retry_after"))
    if retry_after is None:
        retry_after = _header_float(headers, _HDR_RETRY_AFTER)
    if retry_after is None:
        retry_after = _DEFAULT_RETRY_AFTER_SECS
    if bool(body.get("global")) or headers.get(_HDR_SCOPE, "") == _SCOPE_GLOBAL:
        hold = min(max(retry_after, _MIN_HOLD_SECS), _MAX_GLOBAL_HOLD_SECS)
        self._global_ready_at = max(self._global_ready_at, time.monotonic() + hold)
        logger.warning("Discord REST: GLOBAL rate limit, holding every route for %.2fs", hold)
        return hold
    delay = min(max(retry_after, _MIN_HOLD_SECS), _MAX_RETRY_AFTER_SECS)
    self._set_hold(self._hold_key(route), delay)
    return delay


def _breaker_allows(self: DiscordClient) -> bool:
    """False while the invalid-request breaker is OPEN.

    Closes lazily when the cool-off has elapsed, clearing the window so the
    next runaway sequence starts from a clean slate.
    """
    if not self._breaker_until:
        return True
    if time.monotonic() < self._breaker_until:
        return False
    self._breaker_until = 0.0
    self._invalid_hits.clear()
    logger.info("Discord REST: invalid-request breaker closed, resuming requests")
    return True


def _note_invalid(self: DiscordClient, status: int, headers: Mapping[str, str]) -> None:
    """Count a response that spent Discord's invalid-request budget, and
    trip the breaker before that budget can run out.

    The breaker fails toward NOT sending on purpose: a turn that goes
    undelivered costs one conversation, while the IP block it is avoiding
    costs every channel on the host for ten minutes.
    """
    if status not in _INVALID_STATUSES:
        return
    if status == 429 and headers.get(_HDR_SCOPE, "") == _SCOPE_SHARED:
        # A shared-scope 429 is another app's traffic on a limit we merely
        # share, and Discord excludes it from the ban count. Counting it
        # would stop this channel sending over something it did not cause.
        return
    now = time.monotonic()
    cutoff = now - _INVALID_WINDOW_SECS
    while self._invalid_hits and self._invalid_hits[0] < cutoff:
        self._invalid_hits.popleft()
    self._invalid_hits.append(now)
    if self._breaker_until or len(self._invalid_hits) < _INVALID_LIMIT:
        return
    self._breaker_until = now + _BREAKER_COOLOFF_SECS
    logger.warning(
        "Discord REST: %d invalid responses (401/403/429) within %.0fs: pausing all "
        "outbound requests for %.0fs to stay clear of Discord's %d-per-10min IP block. "
        "Check the bot token, its channel permissions, and any send loop.",
        len(self._invalid_hits),
        _INVALID_WINDOW_SECS,
        _BREAKER_COOLOFF_SECS,
        _DISCORD_INVALID_BAN_LIMIT,
    )


async def _api_request(
    self: DiscordClient,
    method: str,
    path: str,
    *,
    timeout: int,
    build: Callable[[], dict[str, Any]],
    destination: str = "",
) -> DiscordApiResult:
    """Shared REST ladder. ``build`` supplies the per-ATTEMPT body kwargs.

    One pass: wait for capacity, send, account for what came back, then
    either answer or retry within the failure class's own budget. Every
    wait is an ``await`` so the event loop keeps running the turn's other
    work while a bucket refills.

    Every one of those waits is also a window in which the destination can be
    withdrawn, so each is paired with a re-read of authorization before the
    attempt that follows it (:meth:`_destination_still_permitted`). An attempt
    that waits nothing is unaffected: the caller's own pre-send check is still
    the most recent reading when no time has passed.
    """
    session = await self._ensure_session()
    url = _API_BASE + path
    auth = {"Authorization": f"Bot {self._token}"}
    route = _route_key(method, path)
    exempt = _is_global_limit_exempt(path)
    rate_limited = 0
    transient = 0
    # The attempt that follows a 429 has already served that back-off, so it
    # sends immediately; re-checking the hold it just waited out would
    # charge it twice.
    served_backoff = False
    while True:
        if not self._breaker_allows():
            return _failed(DISCORD_BLOCKED, detail="invalid-request breaker open")
        if not served_backoff:
            if await self._await_capacity(route, exempt=exempt) > 0:
                permission = await self._destination_still_permitted(path, destination)
                if not permission.permitted:
                    return _failed(DISCORD_BLOCKED, detail=permission.detail)
        served_backoff = False
        try:
            async with session.request(
                method,
                url,
                headers=auth,
                proxy=self._proxy,
                timeout=aiohttp.ClientTimeout(total=timeout),
                **build(),
            ) as resp:
                headers = _rate_limit_headers(resp)
                self._note_headers(route, headers)
                if resp.status == 204:
                    return DiscordApiResult(DISCORD_OK, data={}, status=204)
                # A proxy or Discord error page can return non-JSON; a
                # decode failure must degrade to an error result, never
                # propagate into the renderer/dispatcher.
                try:
                    data = await resp.json(content_type=None)
                except Exception:
                    data = None
                if 200 <= resp.status < 300:
                    # Malformed 2xx body: succeed with an empty result so
                    # callers treat the operation as done (it was).
                    return DiscordApiResult(
                        DISCORD_OK,
                        data=data if data is not None else {},
                        status=resp.status,
                    )
                self._note_invalid(resp.status, headers)
                body = data if isinstance(data, dict) else {}
                if resp.status == 429:
                    delay = self._note_rate_limit(route, data, headers)
                    rate_limited += 1
                    if rate_limited > _RATE_LIMIT_RETRIES:
                        logger.warning(
                            "Discord API %s rate limited again (retry_after %.2fs), "
                            "giving up; the route is held for the next caller",
                            route,
                            delay,
                        )
                        return _failed(DISCORD_TRANSIENT, status=429, detail="rate limited")
                    permission = await self._wait_then_revalidate(path, delay, destination)
                    if not permission.permitted:
                        return _failed(DISCORD_BLOCKED, detail=permission.detail)
                    served_backoff = True
                    continue
                if resp.status >= 500:
                    transient += 1
                    if transient > _TRANSIENT_RETRIES:
                        logger.warning(
                            "Discord API %s failed: status=%s (retries exhausted)",
                            route,
                            resp.status,
                        )
                        return _failed(
                            DISCORD_TRANSIENT,
                            status=resp.status,
                            detail="server error",
                        )
                    backoff = _TRANSIENT_BACKOFF_SECS * transient
                    logger.warning(
                        "Discord API %s failed: status=%s, retrying in %.1fs",
                        route,
                        resp.status,
                        backoff,
                    )
                    permission = await self._wait_then_revalidate(path, backoff, destination)
                    if not permission.permitted:
                        return _failed(DISCORD_BLOCKED, detail=permission.detail)
                    continue
                # Anything else below 500 will fail identically forever: a
                # retry cannot grant a permission or undelete a message, it
                # only spends invalid-request budget.
                logger.warning(
                    "Discord API %s failed: status=%s code=%s message=%s",
                    route,
                    resp.status,
                    body.get("code"),
                    body.get("message"),
                )
                code = _coerce_float(body.get("code"))
                return _failed(
                    DISCORD_PERMANENT,
                    status=resp.status,
                    code=int(code) if code is not None else 0,
                    detail=str(body.get("message", "") or "")[:200],
                )
        except (aiohttp.ClientError, asyncio.TimeoutError) as exc:
            transient += 1
            # Log only the exception type — never anything token-adjacent.
            logger.warning(
                "Discord API %s transport error: %s",
                route,
                type(exc).__name__,
            )
            if transient > _TRANSIENT_RETRIES:
                return _failed(DISCORD_TRANSIENT, detail=type(exc).__name__)
            permission = await self._wait_then_revalidate(
                path, _TRANSIENT_BACKOFF_SECS * transient, destination
            )
            if not permission.permitted:
                return _failed(DISCORD_BLOCKED, detail=permission.detail)
