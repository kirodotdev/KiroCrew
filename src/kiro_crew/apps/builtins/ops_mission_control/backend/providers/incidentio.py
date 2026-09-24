"""incident.io adapter — signals, rotation, and actions.

One class implements three Protocols because incident.io answers all three questions
with the same credential: what is firing (alerts), who is on shift (schedules), and
how to respond (resolve an alert / attach a note). ``roster()`` reuses the schedule read
to show the board every shift on the configured schedules over the next two weeks,
this operator's included — display only.

The adapter is built on **alerts**, not incidents, and that choice runs through every
method. An incident.io alert carries a two-value status — ``firing`` or ``resolved`` —
which is the same shape as a signal's own lifecycle, so absence from a poll means the
alert cleared. A declared incident is a human artefact with an eight-category status
whose transitions run a post-incident flow; treating one as a firing signal would put
work on the board that is already owned by a responder.

The API key can resolve real alerts, so it lives in the keystone-protected secret store
(``secrets.py``), never in the app config, and is never returned by a read endpoint.
"""

from __future__ import annotations

import asyncio
import heapq
import logging
import threading
import time
import urllib.parse
from datetime import datetime, timedelta, timezone
from typing import Any, Iterator

from kiro_crew.apps.builtins.ops_mission_control.backend import policy_store
from kiro_crew.apps.builtins.ops_mission_control.backend.models import (
    ACTION_COMMENT,
    ACTION_RESOLVE,
    SEVERITY_WARNING,
    STATE_FIRING,
    Signal,
)
from kiro_crew.apps.builtins.ops_mission_control.backend.providers import (
    config_list,
    provider_enabled,
)
from kiro_crew.apps.builtins.ops_mission_control.backend.providers.base import (
    DEFAULT_POLL_LIMIT,
    ActionResult,
    ShiftStatus,
    TruncatedSignals,
)
from kiro_crew.apps.builtins.ops_mission_control.backend.providers.http import (
    HttpError,
    request_json,
)
from kiro_crew.apps.builtins.ops_mission_control.backend.secrets import (
    get_secret,
    has_secrets,
)

logger = logging.getLogger(__name__)

PROVIDER_ID = "incidentio"

_API_BASE = "https://api.incident.io"
_SECRET_TOKEN = "api_key"
_REQUIRED_SECRETS: tuple[str, ...] = (_SECRET_TOKEN,)

#: The only alert status that constitutes open work. The field is a strict two-value
#: enum, so there is no acknowledged-but-unresolved middle state to include.
_STATUS_FIRING = "firing"

#: Page ceiling the alerts endpoint enforces. Lower than the registry's own poll cap, so
#: this is what actually bounds a cycle — and why truncation is detected from the
#: response cursor rather than by asking for one item past the cap.
_ALERTS_PAGE_SIZE = 50

#: Page ceiling for the alerts walk. Paging exists so an estate larger than one page is
#: not reported as a complete snapshot; this bounds it so a provider that keeps handing
#: back a cursor cannot spin. 20 pages x 50 records is 1000 alerts — far past
#: ``DEFAULT_POLL_LIMIT``, so a real install always breaks out on the cap first and only
#: a misbehaving API reaches this. cloudwatch's ``_MAX_ALARM_PAGES`` is the model.
_MAX_ALERT_PAGES = 20

#: Window for the on-call-at-this-instant query. The schedule-entries endpoint
#: answers for a range, and a zero-length range risks excluding a shift that starts
#: exactly now, so the question is asked as the shortest usable interval.
_SHIFT_WINDOW = timedelta(minutes=1)

#: How far ahead the roster reads. Two weeks spans the next handover of a weekly
#: rotation with a full rotation to spare, which is what "when am I next on?" needs.
_ROSTER_HORIZON = timedelta(days=14)

#: The roster rides on the board's POLLED `/state`, which would otherwise cost one
#: schedule-entries walk per configured schedule on every poll. A shift boundary does not
#: move on a seconds scale, so five minutes of staleness is invisible in the display.
_ROSTER_TTL_SECS = 300.0

#: A failed read is cached too, but briefly: long enough that a down API is not asked
#: again on every poll, short enough that a fixed one shows up without a restart.
_ROSTER_ERROR_TTL_SECS = 60.0

#: Wall-clock budget for one roster read, across every schedule and page. Each request is
#: already bounded by the HTTP timeout, but a slow-but-answering vendor would otherwise
#: hold a cache-miss `/state` poll for schedules x pages x that timeout. Checked between
#: requests, so a read ends within this plus one request.
_ROSTER_WALK_DEADLINE_SECS = 20.0

#: Page ceiling for one schedule's entries walk. The endpoint pages by handing a cursor
#: back through `entry_window_start`; a two-week window is a page or two, so this only
#: bounds a provider that keeps returning a cursor.
_MAX_ENTRY_PAGES = 10

#: How many shifts the cached roster keeps, and so bounds its members too (a member exists
#: only for a kept shift). Far past a real two-week rotation — 14 days of hourly handoffs
#: across a dozen schedules is ~4000, but a team's own is tens — so hitting it is a
#: misconfiguration worth the warning it logs, not a rotation this display must show whole.
_MAX_ROSTER_WINDOWS = 500

#: Longest id or display name the roster keeps. Both are vendor-controlled strings on a
#: cached, polled payload.
_MAX_ROSTER_TEXT = 128

#: How many configured schedules one roster read walks. `schedule_ids` is agent-writable,
#: so the walk itself is bounded, not just what it keeps; a real team configures a few.
_MAX_ROSTER_SCHEDULES = 20

#: Why a roster carries no shifts, as a code the board translates. The English `error`
#: beside it is for logs and older clients; a card that exists to explain a broken setup
#: must not explain it in English to everyone.
ROSTER_ERROR_NO_SCHEDULES = "no_schedule_ids"
ROSTER_ERROR_UNREACHABLE = "unreachable"

_roster_lock = threading.Lock()
#: ``(expires_at_monotonic, cache_key, roster)``. Keyed on the identity and the schedule
#: list, so an operator changing either sees the new answer on the next poll rather than
#: after the TTL.
_roster_cache: tuple[float, tuple[str, tuple[str, ...]], dict[str, Any]] | None = None


def _headers() -> dict[str, str]:
    return {"Authorization": f"Bearer {get_secret(PROVIDER_ID, _SECRET_TOKEN)}"}


def _iso(moment: datetime) -> str:
    return moment.astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def _parse(value: Any) -> datetime | None:
    """An incident.io timestamp as an aware datetime, or None if it is not one."""
    if not isinstance(value, str) or not value:
        return None
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return None
    return parsed if parsed.tzinfo is not None else parsed.replace(tzinfo=timezone.utc)


def _covers(entry: dict[str, Any], moment: datetime) -> bool:
    """True only when ``entry`` is the shift in force AT ``moment``.

    ``entry_window_start/end`` selects entries that OVERLAP the window, not ones that
    contain it, so the endpoint also returns a shift beginning up to ``_SHIFT_WINDOW``
    from now. Matching that reported on_shift while the outgoing engineer still held the
    page — an authorization granted before handoff. Checking containment here is what
    stops the window's width from being load-bearing: widening it later changes how far
    ahead we LOOK, never who is judged on call now.

    A missing or unparseable bound does not grant the shift. This answer feeds an
    authorization gate, so an entry it cannot evaluate fails closed.
    """
    start = _parse(entry.get("start_at"))
    end = _parse(entry.get("end_at"))
    if start is None or end is None:
        return False
    return start <= moment < end


class IncidentIoAdapter:
    """SignalSource + RotationSource + ActionSink over the incident.io REST API."""

    id = PROVIDER_ID
    display_name = "incident.io"
    detail = (
        "Firing alerts as signals, on-call schedules as rotation, resolve and note as "
        "actions. There is no acknowledge or snooze in the API, so neither is offered."
    )
    #: `user_id` is deliberately ABSENT, for the same reason PagerDuty's is: it identifies
    #: this operator on the rotation, so it is an input to the off-shift refusal. A field in
    #: `config_fields` is writable through `PUT /provider/<id>/config`, which would let the
    #: constrained party name itself as the on-call engineer and authorize a write it does not
    #: own. It lives on the keystone (`policy_store.INCIDENTIO_USER_KEY`), written only by the
    #: authenticated `PUT /settings`.
    config_fields: tuple[str, ...] = ("enabled", "alert_source_ids", "schedule_ids")
    secret_fields: tuple[str, ...] = _REQUIRED_SECRETS

    def configured(self) -> bool:
        return provider_enabled(PROVIDER_ID) and has_secrets(PROVIDER_ID, _REQUIRED_SECRETS)

    # -- SignalSource ------------------------------------------------------

    async def poll(self) -> list[Signal]:
        if not self.configured():
            return []
        return await asyncio.to_thread(self._poll_sync)

    def _poll_sync(self) -> list[Signal]:
        # PAGE UNTIL THE ESTATE IS EXHAUSTED, not just once. The endpoint caps a page at
        # 50, well under the registry's own signal cap, so stopping after one page made a
        # fleet with more than 50 firing alerts report a TRUNCATED poll on EVERY cycle.
        # A permanently non-authoritative poll is not cosmetic: absence from it is never
        # read as recovery, so `reconcile` could never resolve one of this source's
        # signals, and the operator was told the reason was push delivery into a drained
        # spool — which describes the webhook source, not this one.
        # Source filtering happens CLIENT-SIDE, inside the walk, rather than in the query.
        # The endpoint documents an `alert_source[one_of]` filter but not how to encode
        # several values into it, and guessing an encoding risks a filter that silently
        # matches nothing — which presents as a quiet estate, the one failure mode this app
        # must never manufacture. And it runs BEFORE the cap, not after the walk: the cap
        # bounds what a poll may carry to the board, and the board only ever sees matching
        # alerts, so capping the unfiltered stream lets a storm of alerts from unselected
        # sources evict a selected firing alert from the poll — a silent drop that
        # absence-as-recovery then turns into a false resolve.
        wanted_sources = set(config_list(PROVIDER_ID, "alert_source_ids"))

        alerts: list[dict[str, Any]] = []
        cursor = ""
        truncated = False
        for page_num in range(_MAX_ALERT_PAGES):
            params: dict[str, Any] = {
                "status[one_of]": _STATUS_FIRING,
                "page_size": _ALERTS_PAGE_SIZE,
            }
            if cursor:
                params["after"] = cursor
            data = request_json(f"{_API_BASE}/v2/alerts", headers=_headers(), params=params)
            page = data.get("alerts", []) if isinstance(data, dict) else []
            alerts.extend(
                item
                for item in page
                if isinstance(item, dict)
                and (not wanted_sources or str(item.get("alert_source_id", "")) in wanted_sources)
            )

            meta = data.get("pagination_meta") if isinstance(data, dict) else None
            cursor = str(meta.get("after", "") or "") if isinstance(meta, dict) else ""
            if len(alerts) > DEFAULT_POLL_LIMIT:
                # THE CAP IS CHECKED BEFORE EITHER TERMINAL CONDITION, so the walk stops
                # fetching the moment the estate is known to exceed the cap. The verdict
                # itself is not set here: the post-loop derivation below decides it from
                # this same fact, whichever branch ends the walk.
                #
                # STRICTLY GREATER: `base.py` states the invariant — "Requesting exactly
                # the cap makes 'full' and 'capped' indistinguishable; the extra item is
                # the difference" — so `>=` would wrap a whole estate of exactly the cap as
                # truncated, which is the same non-authoritative-poll failure this walk
                # exists to remove. cloudwatch, datadog and github_issues all use `>`.
                break
            if not page:
                # An empty page cannot advance the walk, and looping on one would spin
                # forever. A cursor arriving BESIDE it is the ambiguous case: the provider
                # says more exists while handing back nothing, so this is truncation, not a
                # complete estate.
                truncated = bool(cursor)
                break
            if not cursor:
                break
            if page_num + 1 >= _MAX_ALERT_PAGES:
                # Bounded out with a cursor still pending and NOT over the cap, so nothing
                # downstream would notice the shortfall. Raise instead: an under-reported
                # estate that looks complete is the bug this walk exists to close, and the
                # registry turns the raise into "incidentio did not answer". cloudwatch's
                # `_MAX_ALARM_PAGES` bound is the model.
                raise RuntimeError(
                    f"incident.io returned more than {_MAX_ALERT_PAGES} pages of alerts "
                    "without reaching the poll cap; refusing to report a partial estate "
                    "as a complete snapshot"
                )

        # THE VERDICT AND THE SLICE ARE DECIDED BY THE SAME FACT. Three separate findings
        # in this loop were all one bug wearing different clothes: a branch ended the walk
        # and the verdict disagreed with what the slice then discarded. Deriving it here
        # makes "dropped an alert but called the poll complete" unrepresentable, whichever
        # branch broke — which is the property that matters, since absence from an
        # authoritative poll is read as recovery. Both operate on alerts that already
        # passed the source filter, so neither the cap nor the verdict can be spent on
        # alerts the board would never see.
        if len(alerts) > DEFAULT_POLL_LIMIT:
            truncated = True
        del alerts[DEFAULT_POLL_LIMIT:]

        signals: list[Signal] = []
        for alert in alerts:
            alert_id = str(alert.get("id", ""))
            if not alert_id:
                continue
            source_id = str(alert.get("alert_source_id", ""))

            # The deduplication key is the upstream system's own notion of "this same
            # failure", so it identifies the recurring condition rather than one occurrence
            # — a better exact-match key than the alert id, which is minted per firing.
            dedup = str(alert.get("deduplication_key", ""))
            signals.append(
                Signal.create(
                    source=PROVIDER_ID,
                    native_id=f"alert/{alert_id}",
                    title=str(alert.get("title", "") or f"alert {alert_id}"),
                    # Every alert lands at `warning`. The alert object carries no severity or
                    # priority field: severity is expressed through account-configured
                    # `attributes`, whose names differ per install, so reading one here would
                    # be this app asserting a schema the operator owns. A uniform, honest
                    # default beats a guessed ranking that silently mis-sorts the board.
                    severity=SEVERITY_WARNING,
                    state=STATE_FIRING,
                    fired_at=str(alert.get("created_at", "")),
                    resource=str(alert.get("description", ""))[:200],
                    # The upstream system's link, not an incident.io one — the alert object
                    # has no dashboard permalink (an incident does). It still points a human
                    # at where the condition is visible, which is what the field is for.
                    url=str(alert.get("source_url", "")),
                    provider_key=f"alert/{dedup}" if dedup else f"alert/{alert_id}",
                    labels={
                        "incidentio_alert_id": alert_id,
                        "alert_source_id": source_id,
                        "status": str(alert.get("status", "")),
                    },
                )
            )
        return TruncatedSignals(signals) if truncated else signals

    # -- RotationSource ----------------------------------------------------

    async def on_shift(self) -> ShiftStatus:
        if not self.configured():
            return ShiftStatus(on_shift=True, unknown=True)
        return await asyncio.to_thread(self._on_shift_sync)

    def _on_shift_sync(self) -> ShiftStatus:
        schedule_ids = config_list(PROVIDER_ID, "schedule_ids")
        user_id = str(policy_store.get(policy_store.INCIDENTIO_USER_KEY) or "").strip()

        if not schedule_ids:
            # THE FENCED IDENTITY DECIDES WHICH KIND OF ABSENCE THIS IS. `schedule_ids` is
            # agent-writable config while `user_id` is operator-only, so an empty schedule
            # list is ambiguous where an empty identity is not: it can mean "never
            # configured" or "just cleared". Reporting `unknown` for both would let the
            # constrained party MANUFACTURE the abstention that the tier gate counts as a
            # non-vote, switching the off-shift refusal off with one config write.
            if user_id:
                logger.warning(
                    "ops-mission-control: incidentio has an operator-set user id but NO "
                    "schedule_ids; treating this instance as off shift rather than "
                    "abstaining, because that list is agent-writable"
                )
                return ShiftStatus(on_shift=False)
            # No identity and no schedules is the normal state of a solo install. Report
            # unknown so the tier gate fails OPEN: a missing config must not silently
            # disable a team's incident response.
            return ShiftStatus(on_shift=True, unknown=True)

        if not user_id:
            # WITHOUT AN IDENTITY THIS SOURCE CANNOT ANSWER "AM *I* ON CALL?". Matching any
            # returned shift would read a colleague's rotation as this instance's own and
            # permit a write off shift. `unknown` rather than `on_shift=False` because the
            # vote treats False as a real off-shift ballot, so an operator who set schedules
            # and forgot the identity would find every action refused with nothing
            # explaining why; abstaining leaves any other rotation source deciding.
            return ShiftStatus(on_shift=True, unknown=True)

        now = datetime.now(timezone.utc)
        for schedule_id in schedule_ids:
            params: dict[str, Any] = {
                "schedule_id": schedule_id,
                "entry_window_start": _iso(now),
                "entry_window_end": _iso(now + _SHIFT_WINDOW),
            }
            data = request_json(
                f"{_API_BASE}/v2/schedule_entries", headers=_headers(), params=params
            )
            entries = data.get("schedule_entries") if isinstance(data, dict) else None
            # `final`, never `scheduled`: the rotation rules alone ignore overrides, so a
            # colleague covering this shift would still read as ours (and our own override
            # would not read as ours at all). `final` is the merged, effective answer.
            shifts = entries.get("final", []) if isinstance(entries, dict) else []
            for entry in shifts:
                if not isinstance(entry, dict):
                    continue
                user = entry.get("user") or {}
                if not isinstance(user, dict) or str(user.get("id", "")) != user_id:
                    continue
                if not _covers(entry, now):
                    continue
                return ShiftStatus(
                    on_shift=True,
                    who=str(user.get("name", "")),
                    until=str(entry.get("end_at", "") or ""),
                )
        return ShiftStatus(on_shift=False)

    # -- ActionSink --------------------------------------------------------

    def supported_actions(self) -> frozenset[str]:
        # NO `ack` AND NO `silence`, because the API has neither. An alert's status is a
        # two-value enum and the only lifecycle write is resolve; there is no snooze, mute
        # or suppress for a single alert (a maintenance window is account-level config, not
        # a per-alert call). Advertising a verb the provider cannot perform would fail at
        # execute time, after the autonomy gate had already granted it.
        return frozenset({ACTION_RESOLVE, ACTION_COMMENT})

    async def execute(self, signal: Signal, action: str, payload: dict[str, Any]) -> ActionResult:
        if not self.configured():
            return ActionResult(ok=False, action=action, error="incidentio is not configured")
        if action not in self.supported_actions():
            return ActionResult(
                ok=False,
                action=action,
                error=f"action {action!r} is not available on incident.io",
            )
        alert_id = signal.labels.get("incidentio_alert_id", "")
        if not alert_id:
            return ActionResult(ok=False, action=action, error="signal carries no incident.io id")
        return await asyncio.to_thread(self._execute_sync, alert_id, action, payload)

    def _execute_sync(self, alert_id: str, action: str, payload: dict[str, Any]) -> ActionResult:
        try:
            if action == ACTION_COMMENT:
                request_json(
                    f"{_API_BASE}/v1/alert_notes",
                    method="POST",
                    headers=_headers(),
                    body={"alert_id": alert_id, "content": str(payload.get("note", ""))[:1000]},
                )
            else:
                request_json(
                    f"{_API_BASE}/v2/alerts/{alert_id}/actions/resolve",
                    method="POST",
                    headers=_headers(),
                    body={},
                )
        except HttpError as exc:
            return ActionResult(ok=False, action=action, error=str(exc))
        return ActionResult(ok=True, action=action, detail=f"incidentio {action} {alert_id}")


def user_exists(user_id: str) -> bool | None:
    """Whether incident.io knows ``user_id``: True, False, or None when it cannot say.

    Backs the save-time check on the fenced identity. An id that matches nobody is not a
    harmless typo here: every schedule comparison misses, the rotation reads this operator
    as off shift, and nothing says why — the state a display name pasted into the field
    produced on a real install. Only a definite 404 is ``False``. With no API key yet, or
    on any other failure, the answer is ``None`` and the save goes ahead: refusing an
    identity because the vendor was briefly unreachable would lock the operator out of a
    field only they can write.
    """
    if not (provider_enabled(PROVIDER_ID) and has_secrets(PROVIDER_ID, _REQUIRED_SECRETS)):
        return None
    try:
        request_json(
            f"{_API_BASE}/v2/users/{urllib.parse.quote(user_id, safe='')}", headers=_headers()
        )
    except HttpError as exc:
        return False if exc.status == 404 else None
    return True


# -- Roster (display only) ---------------------------------------------------


def reset_roster_cache() -> None:
    """Forget the cached roster. For tests, and for nothing else."""
    global _roster_cache
    with _roster_lock:
        _roster_cache = None


def roster(now: datetime | None = None) -> dict[str, Any]:
    """Every shift on the configured schedules over the next ``_ROSTER_HORIZON``, or ``{}``.

    The same shape as ``schedule_file.roster()``, so the board's on-call card renders it
    unchanged: members in first-shift order, every shift window, and ``me`` /
    ``me_on_roster`` so "you are not on these schedules" reads differently from "you are,
    just not now". ``login`` and ``me`` carry the incident.io USER ID, because that is what
    the fenced identity stores and a display name is not unique; ``name`` is what to show.

    **Display only, never an authorization input.** ``_on_shift_sync`` alone answers the
    off-shift vote; this may be up to ``_ROSTER_TTL_SECS`` stale and partial on a failed
    read, which is fine for "when am I next on?" and would not be for a gate.

    ``{}`` when there is nothing to show: the provider is off, or no operator identity is
    set (without one there is no "you" to build the roster around). An identity with NO
    ``schedule_ids`` is not ``{}`` — it is the state that makes this source vote off shift
    on every check, so it comes back as an ``error`` the board can surface. Never raises:
    ``rotation.describe`` backs the board's main poll.
    """
    global _roster_cache
    if not (provider_enabled(PROVIDER_ID) and has_secrets(PROVIDER_ID, _REQUIRED_SECRETS)):
        return {}
    user_id = str(policy_store.get(policy_store.INCIDENTIO_USER_KEY) or "").strip()
    if not user_id:
        return {}
    # Capped and clamped BEFORE it becomes the cache key, so the key is bounded like
    # everything else the cache holds: only the schedules a read walks can change its answer.
    configured = config_list(PROVIDER_ID, "schedule_ids")
    schedule_ids = tuple(str(s)[:_MAX_ROSTER_TEXT] for s in configured[:_MAX_ROSTER_SCHEDULES])
    key = (user_id, schedule_ids)

    with _roster_lock:
        cached = _roster_cache
    if cached is not None and cached[1] == key and time.monotonic() < cached[0]:
        return cached[2]

    try:
        result = _build_roster(
            user_id, schedule_ids, now or datetime.now(timezone.utc), configured=len(configured)
        )
    except Exception as exc:  # noqa: BLE001 — a display extra must never break the board
        # The board gets a fixed phrase plus the status, the log gets the detail. A raw
        # vendor error body in the card read as the app breaking; the status alone is
        # what an operator acts on (401: the key, 429: wait, 5xx: incident.io).
        logger.warning("ops-mission-control: incidentio roster unavailable: %s", exc)
        status = exc.status if isinstance(exc, HttpError) else 0
        result = _roster_shell(user_id, ROSTER_ERROR_UNREACHABLE, status)

    ttl = _ROSTER_ERROR_TTL_SECS if result["error"] else _ROSTER_TTL_SECS
    with _roster_lock:
        _roster_cache = (time.monotonic() + ttl, key, result)
    return result


def _roster_shell(user_id: str, error_code: str = "", status: int = 0) -> dict[str, Any]:
    if error_code == ROSTER_ERROR_NO_SCHEDULES:
        error = (
            "an incident.io user id is set but no schedule_ids are configured, so this "
            "instance counts as off shift on every check; add the schedules you are on"
        )
    elif error_code == ROSTER_ERROR_UNREACHABLE:
        error = (
            f"incident.io did not answer (HTTP {status})"
            if status
            else "incident.io did not answer"
        )
    else:
        error = ""
    return {
        "source": PROVIDER_ID,
        "members": [],
        "windows": [],
        # Entries are absolute UTC instants and the board renders them in the viewer's own
        # time, so there is no single schedule timezone to report (each incident.io
        # schedule carries its own).
        "timezone": "UTC",
        "me": user_id,
        "me_on_roster": False,
        # Both are schedule-file concepts. Present so the payload keeps one shape; the
        # values are the ones the card already renders as "nothing to say".
        "strict_gating": False,
        "leader": "",
        "error": error,
        "error_code": error_code,
        "error_status": status,
    }


def _build_roster(
    user_id: str, schedule_ids: tuple[str, ...], now: datetime, *, configured: int
) -> dict[str, Any]:
    """The roster for ``schedule_ids``, already capped at ``_MAX_ROSTER_SCHEDULES``.

    ``configured`` is how many the operator listed, so the log can say how many were not
    walked.
    """
    if not schedule_ids:
        return _roster_shell(user_id, ROSTER_ERROR_NO_SCHEDULES)
    if now.tzinfo is None:
        now = now.replace(tzinfo=timezone.utc)

    # BOUNDED WHILE WALKING, not after: the roster rides on the polled `/state` and
    # `schedule_ids` is agent-writable, so neither what the walk holds nor what the cache
    # keeps may grow with vendor volume. The walk covers at most `_MAX_ROSTER_SCHEDULES`
    # schedules (capped by the caller), pages stream through one at a time, and a heap
    # keeps only the soonest `_MAX_ROSTER_WINDOWS` shifts — the ones "when am I next on?"
    # is about. A member is created only for a kept shift, so a refused shift leaves no
    # row anywhere.
    walked = schedule_ids
    skipped_schedules = max(0, configured - len(walked))
    # Max-heap on start time via the negated timestamp: the root is the LATEST kept shift,
    # the one an earlier arrival evicts. `seq` breaks ties so tuples never compare datetimes.
    heap: list[tuple[float, int, datetime, datetime, str, str]] = []
    left_out = 0
    seq = 0
    deadline = time.monotonic() + _ROSTER_WALK_DEADLINE_SECS
    for schedule_id in walked:
        for entry in _schedule_entries(schedule_id, now, now + _ROSTER_HORIZON, deadline):
            user = entry.get("user")
            if not isinstance(user, dict) or not str(user.get("id", "")):
                continue
            start = _parse(entry.get("start_at"))
            end = _parse(entry.get("end_at"))
            if start is None or end is None or end <= start:
                # Same skip the schedule-file roster applies: counting a malformed entry
                # would inflate a shift count and imply coverage that does not exist.
                continue
            uid = str(user["id"])[:_MAX_ROSTER_TEXT]
            name = str(user.get("name", "") or uid)[:_MAX_ROSTER_TEXT]
            seq += 1
            item = (-start.timestamp(), seq, start, end, uid, name)
            if len(heap) < _MAX_ROSTER_WINDOWS:
                heapq.heappush(heap, item)
            elif item[0] > heap[0][0]:
                heapq.heapreplace(heap, item)
                left_out += 1
            else:
                left_out += 1

    if left_out or skipped_schedules:
        # Said once per read (one read per cache miss), so a capped roster is not mistaken
        # for a rotation that simply had no more shifts.
        logger.warning(
            "ops-mission-control: incidentio roster kept the soonest %d shifts and left out "
            "%d; walked %d of %d configured schedules",
            len(heap),
            left_out,
            len(walked),
            configured,
        )

    # First-appearance order from the sorted windows, so the card does not reshuffle
    # between polls — the schedule-file roster's rule.
    members: dict[str, dict[str, Any]] = {}
    windows: list[dict[str, Any]] = []
    for _neg, _seq, start, end, uid, name in sorted(heap, key=lambda c: (c[2], c[1])):
        current = start <= now < end
        windows.append(
            {"from": start.isoformat(), "to": end.isoformat(), "who": [uid], "current": current}
        )
        slot = members.setdefault(
            uid, {"login": uid, "name": name, "shifts": 0, "on_call_now": False}
        )
        slot["shifts"] += 1
        if current:
            slot["on_call_now"] = True

    result = _roster_shell(user_id)
    result.update(members=list(members.values()), windows=windows, me_on_roster=user_id in members)
    return result


def _schedule_entries(
    schedule_id: str, start: datetime, end: datetime, deadline: float
) -> Iterator[dict[str, Any]]:
    """Every effective (``final``) entry of one schedule overlapping ``[start, end)``.

    A generator, one page at a time, so the caller bounds what it keeps without this walk
    first holding a schedule's whole window.

    ``final``, never ``scheduled``, for the reason ``_on_shift_sync`` gives: overrides are
    how a swapped shift is expressed. The endpoint pages by returning a cursor in
    ``pagination_meta.after`` that goes back in ``entry_window_start`` with the end left
    unchanged, which is why the walk replaces the start and nothing else.
    """
    window_start = _iso(start)
    for _ in range(_MAX_ENTRY_PAGES):
        if time.monotonic() >= deadline:
            # Raised, like the page bound below: a partial roster that looked whole would
            # hide shifts, while the error card says the read did not finish.
            raise RuntimeError(
                f"incident.io roster read ran past {_ROSTER_WALK_DEADLINE_SECS:.0f}s"
            )
        params: dict[str, Any] = {
            "schedule_id": schedule_id,
            "entry_window_start": window_start,
            "entry_window_end": _iso(end),
        }
        data = request_json(f"{_API_BASE}/v2/schedule_entries", headers=_headers(), params=params)
        payload = data.get("schedule_entries") if isinstance(data, dict) else None
        page = payload.get("final", []) if isinstance(payload, dict) else []
        yield from (item for item in page if isinstance(item, dict))
        meta = data.get("pagination_meta") if isinstance(data, dict) else None
        cursor = str(meta.get("after", "") or "") if isinstance(meta, dict) else ""
        if not cursor or not page:
            return
        window_start = cursor
    raise RuntimeError(
        f"incident.io returned more than {_MAX_ENTRY_PAGES} pages of entries for schedule "
        f"{schedule_id}"
    )
