"""Tracked-channel history-readability probe.

Slack grants OAuth scopes only at install time: an install created before the
app manifest gained ``groups:history``/``message.groups`` keeps its old grant
set even after the manifest is fixed, so a tracked private channel delivers no
message events and the silence is indistinguishable from "nobody wrote
anything". This probe gives that dead state a trace: one cheap
``conversations.history(limit=1)`` capability check per tracked channel,
emitting a log warning (and a dashboard notification) for every channel the
bot token cannot read.

Callers dispatch :func:`warn_unreadable_tracked_channels` as a deferred task
(``asyncio.create_task``) — never on the gateway boot path — at gateway
startup after the Slack socket connects, and whenever a channel is added to
tracking dynamically. Best-effort by design: only definitive Slack error
codes are reported; transient network failures stay silent so a flaky link
never produces a false "reinstall your app" alarm.
"""

from __future__ import annotations

import asyncio
import logging
from dataclasses import dataclass
from typing import TYPE_CHECKING, Any, Callable, Iterable

if TYPE_CHECKING:
    from kiro_crew.slack.client import SlackClientOps

logger = logging.getLogger(__name__)

# Slack error codes that definitively mean the bot token cannot read the
# channel's history under its current OAuth grant:
# - ``missing_scope``: the install predates a required scope (for a private
#   channel that is ``groups:history`` on a pre-manifest-fix install).
# - ``channel_not_found``: the token cannot see the channel at all (a private
#   channel outside the grant's visibility looks nonexistent to the API).
UNREADABLE_ERRORS = frozenset({"missing_scope", "channel_not_found"})

_REINSTALL_HINT = (
    "the Slack app install likely predates the current OAuth scopes "
    "(groups:history / message.groups) — reinstall the Slack app from the "
    "bundled manifest to restore private-channel message delivery. A manifest "
    "from `kirocrew manifest` renames the slash command to /kirocrew-<alias>; "
    "run the `kirocrew config set slack.command` line it prints, then restart "
    "the gateway."
)


def log_probe_failure(task: asyncio.Task[dict[str, str]]) -> None:
    """Done-callback for a dispatched probe task: retrieve and log its failure.

    A callback that only discards the task leaves a raised exception
    unretrieved, and asyncio then reports it from the task's finalizer at
    whatever later moment the garbage collector runs.
    """
    if not task.cancelled() and task.exception() is not None:
        logger.warning("Tracked-channel scope probe failed", exc_info=task.exception())


async def warn_unreadable_tracked_channels(
    slack: "SlackClientOps",
    channel_ids: Iterable[str],
    notify: Callable[..., None] | None = None,
) -> dict[str, str]:
    """Probe each tracked channel's history readability; warn on failures.

    Returns ``{channel_id: error_code}`` for every channel the bot token
    cannot read. When *notify* (``DashboardState.notify``-compatible) is
    given and at least one channel is unreadable, one aggregated dashboard
    notification is pushed. Never raises — safe to run fire-and-forget.
    """
    unreadable: dict[str, str] = {}
    for cid in sorted({c for c in channel_ids if c}):
        err = await slack.probe_channel_history(cid)
        if err in UNREADABLE_ERRORS:
            unreadable[cid] = err
            # The rule keys on the word "token" in the format string; the call
            # logs only a channel ID, a Slack error code, and a static hint —
            # no credential is in scope here.
            logger.warning(  # nosemgrep: python-logger-credential-disclosure
                "Tracked Slack channel %s is not readable by the bot token (%s) "
                "— messages there will NOT be delivered. If this is a private "
                "channel, %s",
                cid,
                err,
                _REINSTALL_HINT,
            )
    if unreadable and notify is not None:
        channels = ", ".join(sorted(unreadable))
        try:
            notify(
                "agent",
                "Slack: tracked channel(s) unreadable",
                (
                    f"The Slack bot token cannot read tracked channel(s) "
                    f"{channels} — messages there will NOT be delivered. "
                    f"If these are private channels, {_REINSTALL_HINT}"
                ),
                meta={"channels": sorted(unreadable)},
            )
        except Exception:
            logger.debug("scope-probe notification failed", exc_info=True)
    return unreadable


# ── Manifest drift: does the installed app carry what the switches need? ──

#: The install carries every bot scope the rendered manifest asks for.
STATUS_UP_TO_DATE = "up_to_date"
#: At least one required bot scope is missing: update the app and reinstall.
STATUS_NEEDS_UPDATE = "needs_update"
#: Scopes are granted, but Slack answered ``feature_disabled``: the workspace
#: does not have that Slack Code feature turned on.
STATUS_FEATURE_DISABLED = "feature_disabled"
#: Slack did not say which scopes the install holds (no ``x-oauth-scopes``
#: header, a network failure, or an error other than a rejected token).
STATUS_UNKNOWN = "unknown"
#: No bot token is stored, so there is no install to compare against.
STATUS_NOT_CONFIGURED = "not_configured"
#: Slack rejected the stored bot token (``invalid_auth`` and friends).
STATUS_TOKEN_REJECTED = "token_rejected"

#: Errors that mean the stored bot token itself is no good.
_TOKEN_ERRORS = frozenset(
    {"invalid_auth", "not_authed", "account_inactive", "token_revoked", "token_expired"}
)


def _slack_error(exc: BaseException) -> str:
    """Slack's ``error`` code from a ``SlackApiError``-shaped exception, else ''."""
    response = getattr(exc, "response", None)
    try:
        return str(response.get("error", "") or "") if response is not None else ""
    except Exception:
        return ""


def _header(headers: Any, name: str) -> str | None:
    """Case-insensitive header lookup over a dict or multidict; None if absent."""
    if not headers:
        return None
    try:
        items = headers.items()
    except Exception:
        return None
    for key, value in items:
        if str(key).lower() == name:
            if isinstance(value, (list, tuple)):
                value = ",".join(str(v) for v in value)
            return str(value)
    return None


def parse_granted_scopes(headers: Any) -> frozenset[str] | None:
    """The bot scopes Slack reports in ``x-oauth-scopes``; None when unreadable."""
    raw = _header(headers, "x-oauth-scopes")
    if raw is None:
        return None
    return frozenset(s.strip() for s in raw.split(",") if s.strip())


@dataclass(frozen=True)
class ManifestStatus:
    """The installed app compared with the manifest the switches need."""

    status: str
    #: Required bot scopes the install does not hold.
    missing_scopes: tuple[str, ...] = ()
    #: Bot scopes the install holds that the current manifest does not ask for.
    extra_scopes: tuple[str, ...] = ()
    #: Slack features answered ``feature_disabled`` (probe or runtime).
    disabled_features: tuple[str, ...] = ()
    #: The Slack app id, when Slack named it (for the App Manifest deep link).
    app_id: str = ""
    team_id: str = ""
    #: Short machine reason for ``unknown`` / ``token_rejected``.
    reason: str = ""

    def as_dict(self) -> dict[str, Any]:
        return {
            "status": self.status,
            "missing_scopes": list(self.missing_scopes),
            "extra_scopes": list(self.extra_scopes),
            "disabled_features": list(self.disabled_features),
            "app_id": self.app_id,
            "team_id": self.team_id,
            "reason": self.reason,
        }


async def probe_manifest_status(
    client: Any,
    required_scopes: Iterable[str],
    *,
    known_scopes: Iterable[str] = (),
) -> ManifestStatus:
    """Compare the install behind *client* (a bot-token ``AsyncWebClient``) with
    the manifest's *required_scopes*.

    *known_scopes* is every scope any manifest variant can ask for: an extra
    scope is reported only when it is one of those (a scope the user added to the
    app themselves is theirs, not drift). Never raises.
    """
    required = frozenset(required_scopes)
    try:
        auth = await client.auth_test()
    except Exception as exc:
        err = _slack_error(exc)
        if err in _TOKEN_ERRORS:
            return ManifestStatus(STATUS_TOKEN_REJECTED, reason=err)
        return ManifestStatus(STATUS_UNKNOWN, reason=err or type(exc).__name__)

    def _field(key: str) -> str:
        try:
            return str(auth.get(key, "") or "")
        except Exception:
            return ""

    team_id = _field("team_id")
    app_id = await _app_id(client, _field("bot_id"))
    granted = parse_granted_scopes(getattr(auth, "headers", None))
    if granted is None:
        return ManifestStatus(
            STATUS_UNKNOWN, app_id=app_id, team_id=team_id, reason="scopes_unreadable"
        )
    # The same install the gateway runs on: a reinstall the user just checked
    # becomes the live capability at once, without waiting for a reconnect.
    from kiro_crew.slack import features

    features.record_granted_scopes(granted)
    missing = tuple(sorted(required - granted))
    extra = tuple(sorted((granted & frozenset(known_scopes)) - required))
    disabled = {f for f, e in features.latched().items() if e == "feature_disabled"}
    if missing:
        status = STATUS_NEEDS_UPDATE
    elif disabled:
        status = STATUS_FEATURE_DISABLED
    else:
        status = STATUS_UP_TO_DATE
    return ManifestStatus(
        status,
        missing_scopes=missing,
        extra_scopes=extra,
        disabled_features=tuple(sorted(disabled)),
        app_id=app_id,
        team_id=team_id,
    )


async def _app_id(client: Any, bot_id: str) -> str:
    """The app id behind *bot_id* via ``bots.info`` (needs ``users:read``); '' on failure."""
    if not bot_id:
        return ""
    try:
        info = await client.bots_info(bot=bot_id)
        bot = info.get("bot") or {}
        return str(bot.get("app_id") or "")
    except Exception:
        return ""


async def refresh_capabilities(client: Any) -> None:
    """Re-read what the install can use: the connect-time capability probe.

    Run on every Slack (re)connect. Clears the process latches, records the
    granted bot scopes from ``auth.test``'s ``x-oauth-scopes`` (unknown when the
    header is absent or the call fails). Logs one line; never raises.
    """
    from kiro_crew.slack import features

    try:
        features.clear_latch()
        try:
            auth = await client.auth_test()
        except Exception as exc:
            err = _slack_error(exc)
            features.record_granted_scopes(None)
            if err == "not_allowed_token_type":
                for feature in features.REQUIRED_SCOPE:
                    features.latch_unavailable(feature, err)
            logger.info("Slack capability probe: scopes unknown (%s)", err or type(exc).__name__)
            return
        granted = parse_granted_scopes(getattr(auth, "headers", None))
        features.record_granted_scopes(granted)
        summary = ", ".join(
            f"{f}={'available' if ok else reason}"
            for f in features.REQUIRED_SCOPE
            for ok, reason in [features.availability(f)]
        )
        logger.info("Slack capability probe: %s", summary)
    except Exception:
        logger.debug("Slack capability probe failed", exc_info=True)


def capability_notifier(notify: Callable[..., None] | None) -> Callable[[str, str], None] | None:
    """A ``features.set_capability_notifier`` callback posting one dashboard note."""
    if notify is None:
        return None
    labels = {"agent_sessions": "Agent sessions"}
    reasons = {
        "feature_disabled": "Slack has not enabled this feature for your workspace",
        "missing_scope": "your Slack app is missing a permission it needs; update the "
        "app's manifest from Settings → Slack and reinstall it",
        "not_allowed_token_type": "Slack refused the bot token for this feature",
    }

    def _notify(feature: str, error: str) -> None:
        notify(
            "agent",
            f"Slack: {labels.get(feature, feature)} unavailable",
            f"{labels.get(feature, feature)} is on in Settings, but "
            f"{reasons.get(error, error)}. Slack keeps working as before.",
            meta={"feature": feature, "error": error},
        )

    return _notify
