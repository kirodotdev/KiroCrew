"""Shared Slack access: the Slack users besides the owner who may drive this Crew.

Off by default. The owner turns ``slack.shared_access.enabled`` on and lists Slack
member ids in ``slack.shared_access.user_ids``. A listed person then works with the
bot as the owner does in DMs, mentions and threads, under the owner's identity:
the owner's files, git and cloud credentials and connected tools. The owner-only
controls stay with :func:`kiro_crew.slack.handler.is_owner`.

``slack.allowed_users``, ``!allowlist`` and ``/kirocrew @user`` stay inert: this
list is the only one that admits anybody, so turning the switch on never admits a
stale allowlist entry.

A listed id counts only once Slack has confirmed it (:func:`check`): a person
(not a bot), not deactivated, in the bot's own workspace (``auth.test``) and not
the owner. The list and the switch are read live on every call, so a removal or a
switch-off applies to the next event. A confirmation is kept for
:data:`CONFIRMED_TTL_SECS` and then asked again; a refusal for
:data:`REFUSED_TTL_SECS`.
"""

from __future__ import annotations

import logging
import re
import time
from dataclasses import dataclass
from typing import Any, Callable

from kiro_crew.config.sections_slack import is_slack_user_id

logger = logging.getLogger(__name__)

CONFIRMED_TTL_SECS = 600.0
REFUSED_TTL_SECS = 60.0

#: Why an id is not admitted. Closed set: these strings reach the audit log and the
#: settings page.
REASON_OK = "ok"
REASON_MALFORMED = "malformed_id"
REASON_OWNER = "owner"
REASON_NOT_FOUND = "user_not_found"
REASON_BOT = "bot_user"
REASON_DEACTIVATED = "deactivated"
REASON_OTHER_TEAM = "other_workspace"
REASON_UNVERIFIABLE = "unverifiable"


@dataclass(frozen=True)
class Verdict:
    """Slack's answer for one listed id."""

    user_id: str
    ok: bool
    reason: str
    name: str = ""
    checked_at: float = 0.0


#: The gateway's bot web client (``slack_sdk`` ``AsyncWebClient``), installed at
#: Slack start. ``None`` until then, and in a process without Slack.
_client: Any = None
_verdicts: dict[str, Verdict] = {}
#: Bumped by :func:`install`: an answer to a lookup begun before a reinstall (a
#: reconnect, maybe to another workspace) is never cached.
_generation = 0
#: Every id admitted as a shared user in this process. Kept after a removal or a
#: switch-off, so a message admitted just before either still meets the owner-only
#: refusals (:func:`was_shared`). One entry per person ever shared.
_ever_shared: set[str] = set()

#: Seam the tests replace.
clock: Callable[[], float] = time.monotonic


def install(client: Any) -> None:
    """Remember the bot web client the gateway checks listed ids with."""
    global _client, _generation
    _client = client
    _generation += 1
    _verdicts.clear()


def reset() -> None:
    """Forget the client and every answer (tests, gateway stop)."""
    install(None)
    _ever_shared.clear()


def _settings(cfg: Any) -> tuple[bool, list[str]]:
    shared = getattr(getattr(cfg, "slack", None), "shared_access", None)
    enabled = getattr(shared, "enabled", False) is True
    ids = getattr(shared, "user_ids", None)
    return enabled, list(ids) if isinstance(ids, list) else []


def listed(cfg: Any, user_id: str) -> bool:
    """Whether the switch is on and *user_id* is on the list (not yet confirmed)."""
    enabled, ids = _settings(cfg)
    return enabled and bool(user_id) and user_id in ids


def listed_ids(cfg: Any) -> list[str]:
    """The listed ids while the switch is on, none while it is off (not yet confirmed)."""
    enabled, ids = _settings(cfg)
    return ids if enabled else []


def is_shared_user(cfg: Any, user_id: str, *, owner: bool) -> bool:
    """Whether *user_id* may drive this Crew from Slack as a shared user.

    *owner* is the caller's own owner test for *user_id*: the owner is never a
    shared user, so an owner id on the list changes nothing. True only while the
    switch is on, the id is listed and Slack confirmed it.
    """
    if owner or not listed(cfg, user_id):
        return False
    verdict = _verdicts.get(user_id)
    if verdict is None or not verdict.ok or _stale(verdict):
        return False
    _ever_shared.add(user_id)
    return True


def was_shared(user_id: str) -> bool:
    """Whether *user_id* was admitted as a shared user earlier in this process."""
    return user_id in _ever_shared


def forget(user_id: str) -> None:
    """Drop the answer for *user_id*, so a re-listed id is checked with Slack again."""
    _verdicts.pop(user_id, None)


def _stale(verdict: Verdict | None) -> bool:
    if verdict is None:
        return True
    ttl = CONFIRMED_TTL_SECS if verdict.ok else REFUSED_TTL_SECS
    return clock() - verdict.checked_at >= ttl


async def check(client: Any, user_id: str, *, owner_id: str) -> Verdict:
    """Ask Slack whether *user_id* may be a shared user. Never raises.

    ``users.info`` names the person and their workspace; ``auth.test`` names the
    bot's. A failed call is :data:`REASON_UNVERIFIABLE`, never a pass.
    """
    now = clock()

    def _no(reason: str, name: str = "") -> Verdict:
        return Verdict(user_id, False, reason, name, now)

    if not is_slack_user_id(user_id):
        return _no(REASON_MALFORMED)
    # The owner, also under the other prefix (W/U, as ``is_owner`` matches it).
    if owner_id and owner_id in (
        user_id,
        user_id.replace("W", "U", 1),
        user_id.replace("U", "W", 1),
    ):
        return _no(REASON_OWNER)
    if client is None:
        return _no(REASON_UNVERIFIABLE)
    try:
        auth = await client.auth_test()
        info = await client.users_info(user=user_id)
    except Exception as exc:
        error = ""
        response = getattr(exc, "response", None)
        if response is not None:
            try:
                error = str(response.get("error", "") or "")
            except Exception:
                error = ""
        if error == "user_not_found":
            return _no(REASON_NOT_FOUND)
        logger.warning("shared access: could not check a Slack user (%s)", type(exc).__name__)
        return _no(REASON_UNVERIFIABLE)
    user = info.get("user") if hasattr(info, "get") else None
    if not isinstance(user, dict):
        return _no(REASON_NOT_FOUND)
    raw_profile = user.get("profile")
    profile: dict[str, Any] = raw_profile if isinstance(raw_profile, dict) else {}
    label = profile.get("display_name") or profile.get("real_name") or user.get("name") or ""
    name = str(label)[:80]
    bot_team = str(auth.get("team_id") or "") if hasattr(auth, "get") else ""
    if user.get("is_bot") or user.get("is_app_user") or user_id == "USLACKBOT":
        return _no(REASON_BOT, name)
    if user.get("deleted"):
        return _no(REASON_DEACTIVATED, name)
    if not bot_team or str(user.get("team_id") or "") != bot_team:
        return _no(REASON_OTHER_TEAM, name)
    return Verdict(user_id, True, REASON_OK, name, now)


#: The reply a shared user gets for an owner-only command or button.
SHARED_OWNER_ONLY_TEXT = "⛔ Only the bot owner can do that."

#: The buttons a shared user may press: tool approvals (the handler admits only
#: the requester's own prompt and never their Trust), the review-mode draft of
#: their own turn (the handler admits only the requester or the owner), question
#: cards (an answer is a new message, as if typed; refused in a code channel) and
#: Stop. Everything else on a card (settings, channels, sessions, resume, link,
#: allowlist) is the owner's.
_SHARED_ACTIONS = frozenset(
    {
        "approve_tool",
        "reject_tool",
        "trust_tool",
        "mc_stop_confirm",
        "mc_stop_cancel",
        "stop_kill_now",
        "mc_review_approve",
        "mc_review_edit",
        "mc_review_revise",
        "mc_review_cancel",
    }
)
_SHARED_ACTION_PREFIXES = (
    "mc_inline_stop_",
    "mc_tool_approve_",
    "mc_tool_deny_",
    "mc_tool_trust_",
    "options_choice_",
    "options_several",
)


_FORWARDED_BODY = re.compile(
    r"--- UNTRUSTED FORWARDED CONTENT BEGIN \[(\w+)\] ---.*?"
    r"--- UNTRUSTED FORWARDED CONTENT END \[\1\] ---",
    re.DOTALL,
)


def shared_command_refused(user_id: str, text: str, *, modifiers: bool = True) -> bool:
    """Whether *text* from a shared user is a command they may not send.

    A leading ``/`` (after any bot mention) reaches the agent harness's own command
    parser or the dashboard chat's slash commands (``/agent``, ``/model``, ``/goal``,
    ``/tools trust-all``), and a ``!temporary`` / ``!incognito`` modifier keeps the
    owner's own record from holding the turn. Both are the owner's. False for the
    owner and for anyone who is not a shared user.

    The modifier scan reads only the person's own words: a forwarded message's
    fenced body is someone else's, and with *modifiers* False (a card's
    model-authored label) only the leading ``/`` is judged.
    """
    # circular import: slack.handler composes the access module that imports this one.
    from kiro_crew.messaging import privacy_mode
    from kiro_crew.slack import handler

    if not handler.is_shared_listed(user_id):
        return False
    stripped = text.lstrip()
    if stripped.startswith("<@") and ">" in stripped:
        stripped = stripped[stripped.index(">") + 1 :].lstrip()
    if stripped.startswith("/"):
        return True
    return modifiers and bool(privacy_mode.strip_tokens(_FORWARDED_BODY.sub("", text))[1])


def shared_action_permitted(action_id: str) -> bool:
    """Whether a shared user may press the button *action_id* at all.

    Trust is let through to its handler only so the refusal is answered there,
    where the pending prompt is known; a shared user never grants Trust.
    """
    return action_id in _SHARED_ACTIONS or action_id.startswith(_SHARED_ACTION_PREFIXES)


async def prime_shared_user(user_id: str, orch: Any = None) -> None:
    """:func:`prime` with the gateway's live config (read through *orch*). Never raises."""
    # circular import: slack.handler composes the access module that imports this one.
    from kiro_crew.slack import handler

    if not user_id or handler.is_owner(user_id):
        return
    try:
        await prime(handler.slack_cfg(orch), user_id, owner_id="")
    except Exception:
        logger.warning("shared access: check of a listed user failed", exc_info=True)


async def lookup(client: Any, user_id: str, *, owner_id: str, refresh: bool = False) -> Verdict:
    """Slack's answer for *user_id*, from the cache while it is fresh.

    The settings page reads it to show each person's name; an add passes
    ``refresh=True`` so the id is checked now. The answer is kept for the gate too.
    """
    cached = _verdicts.get(user_id)
    if not refresh and not _stale(cached) and cached is not None:
        return cached
    generation = _generation
    verdict = await check(client, user_id, owner_id=owner_id)
    # Kept for the gate only when *client* is the bot the gateway runs on: an answer
    # from other credentials (a token saved but not yet live) names another
    # workspace, so the gateway checks the id with its own client instead.
    if _same_bot(client) and generation == _generation:
        _verdicts[user_id] = verdict
    return verdict


def _same_bot(client: Any) -> bool:
    """Whether *client* speaks for the bot the gateway's Slack connection runs on."""
    if _client is None or client is None:
        return False
    if client is _client:
        return True
    token = getattr(client, "token", None)
    return bool(token) and token == getattr(_client, "token", None)


async def prime(cfg: Any, user_id: str, *, owner_id: str) -> None:
    """Check a listed *user_id* with Slack when its last answer is missing or old.

    Called on the event path before the synchronous gates read
    :func:`is_shared_user`, so a newly listed person is admitted on their first
    message. A no-op for the owner, an unlisted id and a fresh answer.
    """
    if not user_id or user_id == owner_id or not listed(cfg, user_id):
        return
    if not _stale(_verdicts.get(user_id)):
        return
    generation = _generation
    verdict = await check(_client, user_id, owner_id=owner_id)
    # Re-read: the list may have changed while Slack answered, and a reinstall
    # (reconnect) since the lookup began voids its answer.
    if listed(cfg, user_id) and generation == _generation:
        _verdicts[user_id] = verdict
