"""Slack as an output channel — the pin board.

This is the half of the workflow that otherwise exists only on paper.
Their ops channel WAS the dashboard: one message per incident, its emoji tracking
state, so anyone could read the room's health without opening a tool. That is what
this reproduces.

**No new credential.** This deliberately does NOT add a bot token to the app's
secret store. Kiro Crew already holds one for its Slack gateway, and the live
``SlackClientOps`` is reachable in-process off gateway state — so this reuses it and
introduces zero new secret material, no second rotation obligation, and no second
copy to leak. Governance guidance on credential storage puts "prefer no secret to
rotate" first and permits a stored third-party token only where no such path
exists; here one does. The consequence is a real constraint, not a shortcut: if the
operator has not configured Slack for Kiro Crew itself, this channel is simply
unavailable, and ``configured()`` says so rather than prompting for a token.

**One message per incident, edited in place.** The pin board is only readable if an
incident occupies ONE line that changes, not a stream of updates. So the first post
records ``slack_thread_ts`` on the incident and every later state change is a
``chat_update`` of that same message; detail (diagnosis, resolution) goes into the
thread beneath it so the top line stays scannable. If the ts is lost, we post fresh
rather than going silent — a duplicate line is a cosmetic problem, a missing alarm
is not.

**Failure is never fatal to a cycle.** Slack being down must not stop the agent from
investigating, so every send is wrapped: failures are logged and reported, and the
dispatch cycle proceeds. Notifying is not the work.

**Everything outbound is redacted, through BOTH passes.** Incident titles and diagnoses are
model- and provider-derived text heading to a channel with a different (usually wider)
audience than the dashboard. A credential that reached a provider's alarm description must
not be republished into Slack by us.

``_safe()`` is the single chokepoint and it applies ``security.redact`` **and**
``secrets.redact_tokens``. The two cover different things and neither is a superset:
``redact`` knows AWS keys and exfiltration URLs, while ``redact_tokens`` knows the
PROVIDER-specific token shapes this app handles (PagerDuty, Datadog, …). Measured — a
Datadog app-key shape and a PagerDuty ``u+`` token both pass through ``redact`` completely
unchanged and are masked only by ``redact_tokens``, so an alarm title carrying one reached
the channel verbatim. Found in review; ``redact_tokens``'s own docstring already claimed
Slack as one of its sinks, and it was the one sink not wired to it.

One function rather than three call sites on purpose: the next field added here inherits the
floor instead of depending on whoever adds it remembering both passes.

See ``docs/system-specs/modules/ops-mission-control.md`` § Slack output.
"""

from __future__ import annotations

import asyncio
import logging
from typing import Any

from kiro_crew.apps.builtins.ops_mission_control.backend import policy_store, store
from kiro_crew.apps.builtins.ops_mission_control.backend.models import (
    STATUS_DISPATCHED,
    STATUS_ESCALATED,
    STATUS_INVESTIGATING,
    STATUS_NEEDS_HUMAN,
    STATUS_RESOLVED,
    STATUS_STALE,
    Incident,
)
from kiro_crew.apps.builtins.ops_mission_control.backend.secrets import redact_tokens

# `redact_via_context`, not `security.redact` directly. The core function is what the shim
# calls in the public edition, so behaviour here is unchanged — but on a host that loads a
# companion, the companion's declared credential patterns apply too, and an enterprise host
# that FAILS to compose its companion fails CLOSED rather than silently falling back to public
# patterns. `registry.gather_evidence` and `dispatch.investigation_brief` already went through
# the shim; this sink, `notify_out` and `store` were still calling the core directly, so a
# companion-only credential shape was redacted on two egress paths and not the other three.
# Found in review. Aliased to `redact` so the call sites and their prose read unchanged.
from kiro_crew.platform.context import redact_via_context as redact

logger = logging.getLogger(__name__)

#: Slot-key prefix for an incident's investigation chat. MUST stay in lockstep with
#: ``IncidentChat.tsx`` and the dispatch SOP: the dashboard panel polls this exact key,
#: and the Slack reply link is registered against it.
APP_NAME = "ops-mission-control"

#: Config keys. Non-secret (a channel id is not a credential), so these live in
#: the plain app config rather than the keystone secret store.
_ENABLED_KEY = "slack_enabled"
_CHANNEL_KEY = "slack_channel"

#: State glyph per status. Emoji is correct HERE and only here: Slack messages are
#: not the dashboard, and `slack/blocks.py` already uses them. The repo's
#: no-emoji rule governs rendered dashboard UI, where Lucide icons are required.
_STATUS_EMOJI: dict[str, str] = {
    STATUS_DISPATCHED: "⏳",
    STATUS_INVESTIGATING: "🔍",
    STATUS_NEEDS_HUMAN: "🧑",
    STATUS_RESOLVED: "✅",
    STATUS_ESCALATED: "🚨",
    STATUS_STALE: "💤",
}
_DEFAULT_EMOJI = "•"

#: Slack hard-limits a text block to 3000 chars; stay well under so a long
#: diagnosis is truncated by us with an ellipsis rather than rejected by the API.
_MAX_DETAIL_CHARS = 2000

#: Cap on the incident title in the one-line summary, so the status and resource
#: stay visible on a narrow client.
_MAX_TITLE_CHARS = 160


def configured() -> bool:
    """True when the operator enabled this channel AND named a destination.

    Does not check that Kiro Crew's own Slack client exists — that is a runtime
    condition (it depends on gateway boot), reported by ``status()``.
    """
    return bool(policy_store.get(_ENABLED_KEY)) and bool(channel())


def channel() -> str:
    """The mirror destination — from the FENCED store, not agent-writable config.

    Every incident title, diagnosis and resource name is posted here. An agent that could
    rewrite it would redirect the live incident stream to a channel it chose. Same class as the
    ledger remote and the autonomy ceiling; see `policy_store.OPERATOR_ONLY_KEYS`.
    """
    return str(policy_store.get(_CHANNEL_KEY, "") or "").strip()


def set_settings(*, enabled: bool | None = None, channel_id: str | None = None) -> None:
    """Persist the operator's choice. Non-secret, so plain app config."""
    if enabled is not None:
        policy_store.put(_ENABLED_KEY, bool(enabled))
    if channel_id is not None:
        policy_store.put(_CHANNEL_KEY, channel_id.strip())


def client_from_state(state: Any | None) -> Any | None:
    """Pull the live Slack client off gateway state, tolerating its absence.

    The client is passed in from the route layer (``request.app["state"]``) rather
    than fetched from a module global, because Kiro Crew has no global state
    accessor — state is per-application. That makes the dependency explicit and
    lets every send be tested without a gateway.
    """
    return getattr(state, "slack_client", None) if state is not None else None


def status(client: Any | None = None) -> dict[str, Any]:
    """Why this channel is or is not usable — surfaced in Settings.

    Distinguishes the three failure modes, because they need three different
    fixes: not enabled (flip the toggle), no channel (name one), and no Slack on
    Kiro Crew itself (configure the gateway's Slack integration — this app cannot
    fix that for you, by design, since it holds no token of its own).
    """
    enabled = bool(policy_store.get(_ENABLED_KEY))
    chan = channel()
    has_client = client is not None
    if not enabled:
        detail = "Off. Turn on to mirror incidents to a Slack channel."
    elif not chan:
        detail = "No channel set — enter a channel ID (e.g. C0123456789)."
    elif not has_client:
        detail = (
            "Kiro Crew's own Slack integration is not connected, so there is "
            "nothing to post with. This app deliberately stores no Slack token "
            "of its own — configure Slack in Settings and it will work here."
        )
    else:
        detail = f"Mirroring incidents to {chan}."
    return {
        "enabled": enabled,
        "channel": chan,
        "slack_available": has_client,
        "ready": enabled and bool(chan) and has_client,
        "detail": detail,
    }


def _clip(text: str, limit: int) -> str:
    text = text.strip()
    return text if len(text) <= limit else text[: limit - 1].rstrip() + "…"


def _safe(text: str) -> str:
    """The redaction AND escaping floor for anything Slack-bound. See the module docstring.

    BOTH redaction passes, because neither is a superset of the other: ``redact`` covers AWS
    keys and exfiltration URLs, ``redact_tokens`` covers this app's provider token shapes.
    Order between them is irrelevant — each only ever replaces a match with a placeholder —
    but both are required.

    **Then mrkdwn-escaped, which was missing.** Every string that reaches here is provider
    text — an alarm name, a GitHub issue title, an HMAC-signed webhook body — i.e. content
    this app does not control, rendered into a Slack message as mrkdwn. A title of
    ``<https://attacker.example|runbook>`` therefore painted an attacker-chosen hyperlink into
    the team's incident channel, labelled however the attacker liked. Redaction does not help:
    the payload contains no credential. Found in review.

    Exactly the three characters Slack's own escaping rules name (``&``, ``<``, ``>``), and
    ``&`` first so the ampersands introduced by the other two are not re-escaped. Deliberately
    NOT escaping ``*``/``_``/``` ` ```: those only affect emphasis within the operator's own
    text, cost readability on every ordinary title containing an underscore, and cannot
    fabricate a link or mention — the actual harm here.
    """
    redacted = redact_tokens(redact(text))
    return redacted.replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")


def _safe_link_target(url: str) -> str:
    """A provider URL for the TARGET half of ``<target|label>``. Empty if unusable.

    Separate from ``_safe`` because the two positions need opposite things. In message TEXT,
    ``<`` must become ``&lt;`` or provider content can forge a link. In a link TARGET, that
    escaping would break the URL — but the characters that end the target (``|``, ``>``) and a
    hostile scheme are exactly what must not get through, or a signal titled with
    ``https://x|label> <https://attacker.example`` breaks out of our own link and appends
    another.

    So: redact (a console link or signed webhook URL can carry a token in its query string),
    require ``http``/``https``, reject anything holding a mrkdwn delimiter or whitespace
    rather than trying to repair it, and return "" so the caller simply omits the link. The
    same http(s)-only rule the dashboard applies via ``lib/safeUrl.safeHttpUrl``, which is why
    a ``javascript:`` signal URL is not rendered there either.
    """
    cleaned = redact_tokens(redact(url)).strip()
    if not cleaned or not cleaned.lower().startswith(("http://", "https://")):
        return ""
    if any(ch in cleaned for ch in ("<", ">", "|")) or any(ch.isspace() for ch in cleaned):
        return ""
    return cleaned


def summary_line(incident: Incident) -> str:
    """The one line the pin board shows for this incident.

    Redacted: the title comes from a provider payload and may carry anything.
    """
    emoji = _STATUS_EMOJI.get(incident.status, _DEFAULT_EMOJI)
    title = _clip(_safe(incident.signal.title), _MAX_TITLE_CHARS)
    parts = [f"{emoji} *{incident.incident_id}* {title}"]

    # The blocked reason, when present, is the actionable half — "Needs human"
    # alone does not tell the reader whether they must click approve or think.
    state = incident.blocked_reason or incident.status
    parts.append(f"_{state.replace('_', ' ')}_")

    if incident.signal.resource:
        parts.append(f"`{_clip(_safe(incident.signal.resource), 120)}`")
    return "  ·  ".join(parts)


def _blocks(incident: Incident) -> list[dict]:
    blocks: list[dict] = [
        {"type": "section", "text": {"type": "mrkdwn", "text": summary_line(incident)}}
    ]
    context: list[dict] = [
        {"type": "mrkdwn", "text": f"{incident.signal.source} · {incident.signal.severity}"}
    ]
    # `_safe_link_target`, NOT `_safe`: this is the TARGET half of `<target|label>`, where
    # `_safe`'s `<`/`>` escaping would corrupt the URL while doing nothing about the
    # characters that actually matter here (`|`, `>`, a hostile scheme). Returns "" for
    # anything unusable, and the link is then omitted rather than rendered broken.
    #
    # A URL is not exempt from redaction: a signed webhook or console link can carry a token
    # in its query string. That was missed when `_safe` was introduced for title/resource/
    # detail, because the guard test asserted no bare `redact(` remained but not that every
    # FIELD went through it. Both rounds found in review.
    link_target = _safe_link_target(incident.signal.url)
    if link_target:
        context.append({"type": "mrkdwn", "text": f"<{link_target}|open in provider>"})
    if incident.ledger_matches:
        context.append(
            {"type": "mrkdwn", "text": f"{len(incident.ledger_matches)} known pattern(s)"}
        )
    blocks.append({"type": "context", "elements": context})
    return blocks


async def publish(incident: Incident, client: Any | None) -> bool:
    """Create or update this incident's line on the pin board.

    Returns True when Slack was actually written. Never raises: a Slack outage
    must not fail the dispatch cycle that called it.
    """
    if client is None or not configured():
        return False

    chan = channel()
    blocks = _blocks(incident)
    fallback = summary_line(incident)

    # Edit in place when we already own a message — that is what makes this a
    # board rather than a feed.
    if incident.slack_thread_ts:
        try:
            await client.update_message(
                chan, incident.slack_thread_ts, text=fallback, blocks=blocks
            )
            return True
        except Exception as exc:
            # Fall through to a fresh post: the old ts may be gone (message
            # deleted, channel changed). Silence would be the worse outcome.
            logger.warning(
                "ops-mission-control: Slack update failed for %s (%s) — reposting",
                incident.incident_id,
                exc,
            )

    try:
        ts = await client.post_blocks(chan, blocks, fallback)
    except Exception as exc:
        logger.warning(
            "ops-mission-control: Slack post failed for %s: %s", incident.incident_id, exc
        )
        return False

    if ts:
        try:
            # Off-loop: `update_fields` is a full read-modify-write of the incident index,
            # and `publish` is awaited by `dispatch.run_cycle` on the gateway loop (through
            # `publish_all`), so on a busy install this stalled every other task — the chat
            # turn and the liveness heartbeat included. Same class as the other index calls
            # already wrapped on this path; this one is reached only on a SUCCESSFUL Slack
            # post, which is why it survived the earlier sweep. Found in review.
            await asyncio.to_thread(
                store.update_fields, incident.incident_id, slack_thread_ts=str(ts)
            )
        except Exception as exc:  # pragma: no cover - index write already logged
            # We posted but could not record the ts, so the NEXT update will post
            # a duplicate instead of editing. Cosmetic, and worth logging.
            logger.warning(
                "ops-mission-control: posted to Slack but could not record ts for %s: %s",
                incident.incident_id,
                exc,
            )
    return True


def link_thread_to_investigation(incident: Incident, state: Any | None) -> bool:
    """Register the board message's ts with the host so a REPLY reaches the agent.

    Without this the board is write-only. The app records ``slack_thread_ts`` on its own
    incident record, but inbound Slack routing looks the thread up in the HOST's
    session map (``DashboardState.link_slack`` → ``sessions.set_slack_link``), which
    nothing here ever populated. So a reply into the thread resolved to no session and
    was dropped **silently** — no error, no ephemeral — while the app store listing
    advertised "replyable Slack threads". An operator who answered a question believed
    they had answered it.

    In-process through ``DashboardState`` for the same reasons ``_slot_state`` is: an
    HTTP call to our own gateway would need an auth token and can deadlock the loop.

    Returns whether the link was made. Never raises, and a host without the method (or
    without the slot yet) is a no-op — the slot is created by the dispatch SOP after the
    claim, so early calls are expected to miss and later ones succeed.
    """
    if state is None or not incident.slack_thread_ts or not configured():
        return False
    slot_key = incident.slot_key or f"{APP_NAME}-{incident.incident_id}"
    linker = getattr(state, "link_slack", None)
    if linker is None:
        return False
    # Only link a slot that exists: link_slack silently returns on an unknown slot, and
    # we want to report honestly whether the thread is actually answerable.
    getter = getattr(state, "get_slot", None)
    try:
        if getter is not None and getter(slot_key) is None:
            return False
        linker(slot_key, incident.slack_thread_ts, channel())
    except Exception as exc:  # noqa: BLE001 — never fatal to a dispatch cycle
        logger.warning(
            "ops-mission-control: could not link Slack thread for %s: %s",
            incident.incident_id,
            exc,
        )
        return False
    logger.info(
        "ops-mission-control: linked Slack thread %s to slot %r — replies now reach the "
        "investigation",
        incident.slack_thread_ts,
        slot_key,
    )
    return True


async def post_detail(incident: Incident, text: str, client: Any | None) -> bool:
    """Add detail in the incident's thread, keeping the top line scannable.

    Used for a diagnosis or resolution: it belongs with the incident but must not
    push the board's one-line summary out of view.
    """
    if client is None or not configured() or not incident.slack_thread_ts:
        return False
    body = _clip(_safe(text), _MAX_DETAIL_CHARS)
    if not body:
        return False
    try:
        await client.post_message(channel(), body, thread_ts=incident.slack_thread_ts)
        return True
    except Exception as exc:
        logger.warning(
            "ops-mission-control: Slack thread post failed for %s: %s",
            incident.incident_id,
            exc,
        )
        return False


async def publish_all(incidents: list[Incident], client: Any | None) -> int:
    """Refresh the board for several incidents. Returns how many were written."""
    if client is None:
        return 0
    written = 0
    for incident in incidents:
        if await publish(incident, client):
            written += 1
    return written
