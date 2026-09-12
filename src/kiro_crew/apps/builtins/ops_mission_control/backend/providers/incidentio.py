"""incident.io adapter — signals, rotation, and actions.

One class implements three Protocols because incident.io answers all three questions
with the same credential: what is firing (alerts), who is on shift (schedules), and
how to respond (resolve an alert / attach a note).

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
import logging
from datetime import datetime, timedelta, timezone
from typing import Any

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

#: Window used to ask "who is on call at this instant". The schedule-entries endpoint
#: answers for a range, and a zero-length range risks excluding a shift that starts
#: exactly now, so the question is asked as the shortest usable interval.
_SHIFT_WINDOW = timedelta(minutes=1)


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
        alerts: list[dict[str, Any]] = []
        cursor = ""
        truncated = False
        while True:
            params: dict[str, Any] = {
                "status[one_of]": _STATUS_FIRING,
                "page_size": _ALERTS_PAGE_SIZE,
            }
            if cursor:
                params["after"] = cursor
            data = request_json(f"{_API_BASE}/v2/alerts", headers=_headers(), params=params)
            page = data.get("alerts", []) if isinstance(data, dict) else []
            alerts.extend(item for item in page if isinstance(item, dict))

            meta = data.get("pagination_meta") if isinstance(data, dict) else None
            cursor = str(meta.get("after", "") or "") if isinstance(meta, dict) else ""
            if len(alerts) > DEFAULT_POLL_LIMIT:
                # THE CAP IS CHECKED BEFORE EITHER TERMINAL CONDITION, because a page can
                # carry us past it AND be the last one. Checking `not cursor` first let a
                # terminal page of 10 land on 110, break with `truncated` still False, and
                # then lose 10 still-firing alerts to the slice below while reporting a
                # complete estate.
                #
                # STRICTLY GREATER: `base.py` states the invariant — "Requesting exactly
                # the cap makes 'full' and 'capped' indistinguishable; the extra item is
                # the difference" — so `>=` would wrap a whole estate of exactly the cap as
                # truncated, which is the same non-authoritative-poll failure this walk
                # exists to remove. cloudwatch, datadog and github_issues all use `>`.
                truncated = True
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

        # THE VERDICT AND THE SLICE ARE DECIDED BY THE SAME FACT. Three separate findings
        # in this loop were all one bug wearing different clothes: a branch ended the walk
        # and the verdict disagreed with what the slice then discarded. Deriving it here
        # makes "dropped an alert but called the poll complete" unrepresentable, whichever
        # branch broke — which is the property that matters, since absence from an
        # authoritative poll is read as recovery.
        if len(alerts) > DEFAULT_POLL_LIMIT:
            truncated = True
        del alerts[DEFAULT_POLL_LIMIT:]

        # Source filtering happens HERE rather than in the query. The endpoint documents an
        # `alert_source[one_of]` filter but not how to encode several values into it, and
        # guessing an encoding risks a filter that silently matches nothing — which presents
        # as a quiet estate, the one failure mode this app must never manufacture. Filtering
        # after the fetch can drop the count below the cap while the provider still had more,
        # which is exactly why the truncation verdict above is computed from the walk itself
        # and preserved regardless of how many survive.
        wanted_sources = set(config_list(PROVIDER_ID, "alert_source_ids"))

        signals: list[Signal] = []
        for alert in alerts:
            if not isinstance(alert, dict):
                continue
            alert_id = str(alert.get("id", ""))
            if not alert_id:
                continue
            source_id = str(alert.get("alert_source_id", ""))
            if wanted_sources and source_id not in wanted_sources:
                continue

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
