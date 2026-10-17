"""PagerDuty adapter — signals, rotation, and actions.

One class implements three Protocols because PagerDuty genuinely answers all three
questions with the same credential: what is paging (incidents), who is on shift
(on-call schedules), and how to respond (acknowledge / resolve / note). Splitting
it into three classes would triple the config surface for no gain.

The API token is a live credential against the user's production paging system —
it can acknowledge and resolve real pages. It is therefore stored in the
keystone-protected secret store (see ``secrets.py``), never in the app config, and
never returned by any read endpoint.
"""

from __future__ import annotations

import asyncio
import logging
from typing import Any

from kiro_crew.apps.builtins.ops_mission_control.backend import policy_store
from kiro_crew.apps.builtins.ops_mission_control.backend.models import (
    ACTION_ACK,
    ACTION_COMMENT,
    ACTION_RESOLVE,
    ACTION_SILENCE,
    SEVERITY_CRITICAL,
    SEVERITY_WARNING,
    STATE_FIRING,
    Signal,
    resolve_silence_secs,
)
from kiro_crew.apps.builtins.ops_mission_control.backend.providers import (
    config_list,
    config_value,
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

PROVIDER_ID = "pagerduty"

_API_BASE = "https://api.pagerduty.com"
_SECRET_TOKEN = "api_token"
_REQUIRED_SECRETS: tuple[str, ...] = (_SECRET_TOKEN,)

#: Incident statuses that constitute open work. ``acknowledged`` is included:
#: an acknowledged page is still unresolved, and the whole point is to be working
#: it rather than to stop at the ack.
_OPEN_STATUSES: tuple[str, ...] = ("triggered", "acknowledged")

#: PagerDuty urgency -> our normalized severity.
_URGENCY_SEVERITY: dict[str, str] = {"high": SEVERITY_CRITICAL, "low": SEVERITY_WARNING}

#: PagerDuty requires a From header carrying a valid user email for write
#: operations. Without it, writes 400 — so a missing email disables writes rather
#: than failing at execute time.
_CONFIG_FROM_EMAIL = "from_email"


def _headers(*, for_write: bool = False) -> dict[str, str]:
    token = get_secret(PROVIDER_ID, _SECRET_TOKEN)
    headers = {
        "Authorization": f"Token token={token}",
        "Accept": "application/vnd.pagerduty+json;version=2",
    }
    if for_write:
        email = config_value(PROVIDER_ID, _CONFIG_FROM_EMAIL)
        if email:
            headers["From"] = email
    return headers


class PagerDutyAdapter:
    """SignalSource + RotationSource + ActionSink over the PagerDuty REST API."""

    id = PROVIDER_ID
    display_name = "PagerDuty"
    detail = "Incidents as signals, on-call schedules as rotation, ack/resolve/note as actions."
    #: `user_id` is deliberately ABSENT: it identifies this operator on the rotation, so it is
    #: an input to the off-shift refusal (`_definitely_off_shift` -> `_on_shift_sync`). Writing
    #: the current responder's id would make this instance report on-shift and authorize a
    #: provider write it does not own. `PUT /provider/<id>/config` writes the agent-writable
    #: `config.json`, so declaring it here would keep that forgery reachable even with the read
    #: fenced. It lives on the keystone (`policy_store.PAGERDUTY_USER_KEY`), written only by the
    #: authenticated `PUT /settings` — exactly like `schedule-file.github_login`.
    config_fields: tuple[str, ...] = (
        "enabled",
        "service_ids",
        "schedule_ids",
        _CONFIG_FROM_EMAIL,
    )
    secret_fields: tuple[str, ...] = _REQUIRED_SECRETS

    def configured(self) -> bool:
        return provider_enabled(PROVIDER_ID) and has_secrets(PROVIDER_ID, _REQUIRED_SECRETS)

    # -- SignalSource ------------------------------------------------------

    async def poll(self) -> list[Signal]:
        if not self.configured():
            return []
        return await asyncio.to_thread(self._poll_sync)

    def _poll_sync(self) -> list[Signal]:
        params: dict[str, Any] = {
            "statuses[]": list(_OPEN_STATUSES),
            "limit": DEFAULT_POLL_LIMIT,
            "sort_by": "created_at:desc",
        }
        service_ids = config_list(PROVIDER_ID, "service_ids")
        if service_ids:
            params["service_ids[]"] = service_ids

        data = request_json(f"{_API_BASE}/incidents", headers=_headers(), params=params)
        incidents = data.get("incidents", []) if isinstance(data, dict) else []
        # PagerDuty's `more` flag is the truncation detector, NOT the `limit+1` trick used
        # elsewhere: 100 is this endpoint's maximum `limit`, so a 101 request would be clamped
        # and read back as a full page. `more: true` means additional open incidents exist that
        # this poll cannot see, and reporting a complete snapshot would let `reconcile`
        # terminally resolve them. See `providers.base.TruncatedSignals`.
        truncated = bool(data.get("more")) if isinstance(data, dict) else False

        signals: list[Signal] = []
        for incident in incidents:
            if not isinstance(incident, dict):
                continue
            incident_id = str(incident.get("id", ""))
            if not incident_id:
                continue
            service = incident.get("service") or {}
            signals.append(
                Signal.create(
                    source=PROVIDER_ID,
                    native_id=f"incident/{incident_id}",
                    title=str(incident.get("title", "") or f"incident {incident_id}"),
                    severity=_URGENCY_SEVERITY.get(
                        str(incident.get("urgency", "")).lower(), SEVERITY_WARNING
                    ),
                    state=STATE_FIRING,
                    fired_at=str(incident.get("created_at", "")),
                    resource=str(service.get("summary", "")) if isinstance(service, dict) else "",
                    url=str(incident.get("html_url", "")),
                    # PagerDuty mints a NEW incident id each time an alert re-triggers, so
                    # unlike a CloudWatch alarm name this identifies the occurrence, not
                    # the recurring failure. It is still the right exact key — matching
                    # "this very incident" is correct when work is re-claimed after a
                    # sweep — it just will not generalize across occurrences, and the
                    # fingerprint remains the path that does.
                    provider_key=f"incident/{incident_id}" if incident_id else "",
                    labels={
                        "incident_number": str(incident.get("incident_number", "")),
                        "status": str(incident.get("status", "")),
                        "pd_incident_id": incident_id,
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
        user_id = str(policy_store.get(policy_store.PAGERDUTY_USER_KEY) or "").strip()
        if not schedule_ids:
            # THE FENCED IDENTITY DECIDES WHICH KIND OF ABSENCE THIS IS.
            #
            # `schedule_ids` lives in `config_fields`, i.e. in agent-writable `config.json`,
            # while `user_id` is operator-only on the keystone. So an empty schedule list is
            # ambiguous in a way an empty `user_id` is not: it can mean "never configured", or
            # it can mean "an agent just cleared it". Reporting `unknown` for both let the
            # constrained party MANUFACTURE the abstention — `_definitely_off_shift` counts
            # `unknown` as a non-vote, so one config write made this source stop answering and
            # the off-shift refusal stopped firing. Reproduced end to end: with schedules set
            # and PagerDuty reporting off shift the write was refused; clearing `schedule_ids`
            # — changing nothing about who is on call — returned "granted by rule on
            # cloudwatch" for the same signal. Found in review (GPT 5.6).
            #
            # A stored `user_id` is the operator's own statement that they use this rotation,
            # and it is the ONE input here the agent cannot forge. With it present, an empty
            # schedule list is a source that should be able to answer and cannot, which is the
            # same shape as a raise: an off-shift VOTE, not a shrug.
            if user_id:
                logger.warning(
                    "ops-mission-control: pagerduty has an operator-set user id but NO "
                    "schedule_ids; treating this instance as off shift rather than "
                    "abstaining, because that list is agent-writable"
                )
                return ShiftStatus(on_shift=False)
            # Genuinely unconfigured — no identity, no schedules. Report unknown so the tier
            # gate fails OPEN: a missing config must not silently disable a team's incident
            # response, and on a solo install this is the normal state.
            return ShiftStatus(on_shift=True, unknown=True)

        if not user_id:
            # WITHOUT AN IDENTITY THIS SOURCE CANNOT ANSWER "AM *I* ON CALL?".
            #
            # Both the query filter below and the per-entry check further down are
            # conditional on `user_id`, so a blank one made this return `on_shift=True` for
            # ANY teammate's shift — the off-shift refusal then read a colleague's rotation as
            # this instance's own and permitted a production write. Found in review.
            #
            # `unknown`, NOT `on_shift=False`: the review suggested False, and that is the
            # wrong direction here. `_definitely_off_shift` treats a False as a real
            # off-shift VOTE, so an operator who set `schedule_ids` and never set a user id
            # would find every manual action refused with no way to see why — a
            # configuration omission silently disabling the app, which is the failure mode
            # the `unknown` branch three lines up exists to avoid. `unknown` is a non-vote:
            # this source abstains and any OTHER configured rotation still decides.
            #
            # The id is operator-only (`policy_store.PAGERDUTY_USER_KEY`) precisely because it
            # is an authorization input, so "unset" is a state only the operator can create.
            return ShiftStatus(on_shift=True, unknown=True)

        params: dict[str, Any] = {"schedule_ids[]": schedule_ids, "earliest": "true"}
        params["user_ids[]"] = [user_id]
        data = request_json(f"{_API_BASE}/oncalls", headers=_headers(), params=params)
        oncalls = data.get("oncalls", []) if isinstance(data, dict) else []

        for entry in oncalls:
            if not isinstance(entry, dict):
                continue
            user = entry.get("user") or {}
            who = str(user.get("summary", "")) if isinstance(user, dict) else ""
            # No `if user_id` guard: it is non-empty by the early return above. Keeping the
            # guard here would suggest a blank id still reaches this loop, which is exactly the
            # reading that let the hole survive.
            if not isinstance(user, dict) or str(user.get("id", "")) != user_id:
                continue
            return ShiftStatus(on_shift=True, who=who, until=str(entry.get("end", "") or ""))
        return ShiftStatus(on_shift=False)

    # -- ActionSink --------------------------------------------------------

    def supported_actions(self) -> frozenset[str]:
        # Writes need the From header; without it PagerDuty rejects them, so we
        # advertise no actions rather than failing at execute time.
        if not config_value(PROVIDER_ID, _CONFIG_FROM_EMAIL):
            return frozenset()
        # ``silence`` maps onto PagerDuty's own snooze, which takes a REQUIRED duration —
        # so it is a genuine time-boxed suppression rather than an ack dressed up as one.
        # An ack, by contrast, has no expiry: it says "I am on it", and if the responder
        # then vanishes the incident stays acknowledged and un-paged indefinitely. Those
        # are different promises and the vocabulary now keeps them apart.
        return frozenset({ACTION_ACK, ACTION_RESOLVE, ACTION_COMMENT, ACTION_SILENCE})

    async def execute(self, signal: Signal, action: str, payload: dict[str, Any]) -> ActionResult:
        if not self.configured():
            return ActionResult(ok=False, action=action, error="pagerduty is not configured")
        if action not in self.supported_actions():
            return ActionResult(
                ok=False,
                action=action,
                error=(
                    f"action {action!r} unavailable — set "
                    f"{_CONFIG_FROM_EMAIL} in the PagerDuty config to enable writes"
                ),
            )
        incident_id = signal.labels.get("pd_incident_id", "")
        if not incident_id:
            return ActionResult(ok=False, action=action, error="signal carries no PagerDuty id")
        return await asyncio.to_thread(self._execute_sync, incident_id, action, payload)

    def _execute_sync(self, incident_id: str, action: str, payload: dict[str, Any]) -> ActionResult:
        try:
            if action == ACTION_COMMENT:
                request_json(
                    f"{_API_BASE}/incidents/{incident_id}/notes",
                    method="POST",
                    headers=_headers(for_write=True),
                    body={"note": {"content": str(payload.get("note", ""))[:1000]}},
                )
            elif action == ACTION_SILENCE:
                # Snooze re-pages automatically when the window elapses, which is the
                # self-healing property that makes this the safe verb to grant.
                duration = resolve_silence_secs(payload.get("duration_secs"))
                request_json(
                    f"{_API_BASE}/incidents/{incident_id}/snooze",
                    method="POST",
                    headers=_headers(for_write=True),
                    body={"duration": duration},
                )
                return ActionResult(
                    ok=True,
                    action=action,
                    detail=(
                        f"pagerduty incident {incident_id} snoozed for "
                        f"{duration // 60}m — it re-pages when the window elapses"
                    ),
                )
            else:
                status = "acknowledged" if action == ACTION_ACK else "resolved"
                request_json(
                    f"{_API_BASE}/incidents/{incident_id}",
                    method="PUT",
                    headers=_headers(for_write=True),
                    body={"incident": {"type": "incident_reference", "status": status}},
                )
        except HttpError as exc:
            return ActionResult(ok=False, action=action, error=str(exc))
        return ActionResult(ok=True, action=action, detail=f"pagerduty {action} {incident_id}")
