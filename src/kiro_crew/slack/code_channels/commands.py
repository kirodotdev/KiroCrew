"""A code channel's runtime slash commands: ``/summarize``, ``/wrap-up``, ``/create-pr``.

Slack lets the channel's agent register its own commands at runtime
(``agents.conversations.setCommands``, a full replacement of THIS app's set) and
delivers an invocation like a manifest slash command; over Socket Mode it arrives
as a ``slash_commands`` envelope, so ``slack/events.py`` hands it to
:func:`handle_slash` before its own ``/kirocrew`` routing.

**What is registered** (:func:`desired_commands`): ``/summarize`` and
``/wrap-up`` always; ``/create-pr`` only while the CHANNEL can open a pull
request (:func:`pr_capability`: the channel has a git worktree, its remote is
a GitHub repository this install may reach, and ``gh`` is signed in for that
host). The gate is deliberately not the bound agent's own tool set: a conductor
that never edits files is offered ``/create-pr`` too, and its prompt lets it
dispatch a worker on the channel's own worktree and branch. The set is
re-checked at every turn start, off the turn's path, and sent only when it
changed.

**What an invocation does.** Each command becomes a prompt for the bound
session, delivered the way a message typed in the channel is: the sender is
authorized against the channel's ORIGIN conversation first (allowed user, origin
still admits the bot), and every decision is SEL-audited. ``/wrap-up`` also
arms the wrap-up fallback (:mod:`.wrapup`). Slack may deliver one invocation
twice; a repeat (same ``trigger_id``) is dropped with no side effect.
"""

from __future__ import annotations

import asyncio
import hashlib
import json
import logging
import os
import time
from collections.abc import Awaitable, Callable
from typing import Any

from kiro_crew.slack.code_channels import surface
from kiro_crew.slack.code_channels.binding import code_channels_on, live_config
from kiro_crew.slack.code_channels.models import UNAVAILABLE_ERRORS, CodeChannelApiError
from kiro_crew.slack.code_channels.store import Binding, store
from kiro_crew.slack.code_channels.surface_store import get_async as surface_get
from kiro_crew.slack.code_channels.surface_store import update_async as surface_update

logger = logging.getLogger(__name__)

SUMMARIZE = "summarize"
WRAP_UP = "wrap-up"
CREATE_PR = "create-pr"

_COMMANDS: dict[str, dict[str, str]] = {
    SUMMARIZE: {
        "name": SUMMARIZE,
        "description": "Summarize this session: what changed and what is still open",
    },
    WRAP_UP: {
        "name": WRAP_UP,
        "description": "Finish up and post a summary with an Archive button",
        "argument_hint": "[anything to add]",
    },
    CREATE_PR: {
        "name": CREATE_PR,
        "description": "Open a pull request for this channel's branch",
        "argument_hint": "[title]",
    },
}

#: Answers that mean this channel cannot take commands at all: stop asking.
_DISABLING = UNAVAILABLE_ERRORS | frozenset(
    {"not_agent_channel", "channel_not_found", "is_archived", "not_authorized"}
)

#: How long a ``gh`` sign-in answer is trusted, by outcome.
_GH_OK_TTL = 600.0
_GH_FAIL_TTL = 120.0
_gh_cache: dict[str, tuple[bool, float]] = {}

_locks: dict[str, asyncio.Lock] = {}
_tasks: set[asyncio.Future[Any]] = set()

_seen: Any = None

NOT_A_CODE_CHANNEL_TEXT = "That command works only in a Kiro Crew code channel."
NOT_AUTHORIZED_TEXT = "⛔ You are not authorized to use this command here."
ARCHIVED_TEXT = "This channel is archived."
NO_PR_TEXT = {
    "not_a_repo": "This session's project is not a git repository, so there is no branch to open a pull request from.",
    "no_github_remote": "This repository has no GitHub remote, so I can't open a pull request for it.",
    "host_not_allowed": "This repository's GitHub host isn't one this Kiro Crew may reach.",
    "gh_not_signed_in": "The GitHub CLI isn't signed in on this machine (`gh auth login`), so I can't open a pull request.",
}
ACK_TEXT = "Running `/{name}`…"
#: How the prompt names a command the context bar ran.
ACTION_LABELS = {CREATE_PR: "Create PR"}


def _lock(channel_id: str) -> asyncio.Lock:
    lock = _locks.get(channel_id)
    if lock is None:
        lock = _locks[channel_id] = asyncio.Lock()
    return lock


def _spawn(coro: Awaitable[Any]) -> None:
    task = asyncio.ensure_future(coro)
    _tasks.add(task)
    task.add_done_callback(_tasks.discard)


async def drain() -> None:
    """Wait for background command syncs (tests, shutdown)."""
    while _tasks:
        await asyncio.gather(*list(_tasks), return_exceptions=True)


# ── Capability ────────────────────────────────────────────────────────────


async def _gh_signed_in(host: str) -> bool:
    cached = _gh_cache.get(host)
    now = time.monotonic()
    if cached is not None and cached[1] > now:
        return cached[0]
    ok = False
    try:
        from kiro_crew.dashboard.source_providers import runner

        user = await runner._run_json("gh", "api", "user", host=host)
        ok = isinstance(user, dict) and bool(user.get("login"))
    except Exception as exc:
        logger.info("code channel: gh sign-in check for %s failed: %s", host, exc)
    _gh_cache[host] = (ok, now + (_GH_OK_TTL if ok else _GH_FAIL_TTL))
    return ok


async def pr_capability(binding: Binding) -> tuple[bool, str]:
    """``(can_open, reason)``: whether the bound session can open a pull request."""
    from kiro_crew.slack.code_channels.workspace import git_ops
    from kiro_crew.slack.code_channels.workspace.pr import _github_host_allowed
    from kiro_crew.slack.code_channels.workspace.remote import parse_remote
    from kiro_crew.slack.code_channels.workspace.store import WorkspaceStore

    try:
        record = await asyncio.to_thread(WorkspaceStore().get, binding.team_id, binding.channel_id)
        tree = record.worktree if record is not None else ""
        if not tree or not await asyncio.to_thread(os.path.isdir, tree):
            return False, "not_a_repo"
        repo = parse_remote(await asyncio.to_thread(git_ops.remote_url, tree))
    except Exception:
        logger.debug("code channel PR capability unreadable", exc_info=True)
        return False, "not_a_repo"
    if repo is None:
        return False, "no_github_remote"
    if not _github_host_allowed(repo.host):
        return False, "host_not_allowed"
    if not await _gh_signed_in(repo.host):
        return False, "gh_not_signed_in"
    return True, ""


def desired_commands(can_open_pr: bool) -> list[dict[str, str]]:
    names = [SUMMARIZE, WRAP_UP] + ([CREATE_PR] if can_open_pr else [])
    return [dict(_COMMANDS[n]) for n in names]


def _digest(commands: list[dict[str, str]]) -> str:
    body = json.dumps(commands, sort_keys=True)
    return hashlib.sha256(body.encode()).hexdigest()


# ── Registration ──────────────────────────────────────────────────────────


async def sync_commands(binding: Binding) -> None:
    """Register the channel's command set when it changed (never raises)."""
    channel = binding.channel_id
    try:
        if not code_channels_on(live_config()) or await surface.is_archived(binding):
            return
        if not await surface.outbound_permitted():
            return
        async with _lock(channel):
            record = await surface_get(channel)
            if record is not None and record.commands_off:
                return
            can_pr, _ = await pr_capability(binding)
            commands = desired_commands(can_pr)
            digest = _digest(commands)
            if record is not None and record.commands_digest == digest:
                return
            client = surface.client()
            if client is None:
                return
            try:
                await client.set_commands(channel, commands)
            except CodeChannelApiError as exc:
                if exc.code in _DISABLING:
                    await surface_update(channel, commands_off=exc.code)
                    logger.info("code channel %s: commands off (%s)", channel, exc.code)
                    from kiro_crew.slack.code_channels.notices import notify_refusal

                    await notify_refusal(channel, exc.code, session_key=binding.session_key)
                else:
                    logger.info("code channel %s: commands not set: %s", channel, exc.code)
                return
            await surface_update(channel, commands_digest=digest)
    except Exception:
        logger.warning("code channel %s: command sync failed", channel, exc_info=True)


async def on_turn_start(binding: Binding, _event: Any) -> None:
    """Turn-start hook: re-check the command set without holding up the turn."""
    _spawn(sync_commands(binding))


# ── Invocation ────────────────────────────────────────────────────────────


def _first_delivery(payload: dict[str, Any]) -> bool:
    """False for a repeat of an invocation already handled."""
    key = str(payload.get("trigger_id") or "")
    if not key:
        parts = [str(payload.get(k) or "") for k in ("channel_id", "user_id", "command", "text")]
        key = "nt:" + hashlib.sha256("\0".join(parts).encode()).hexdigest()
    global _seen
    if _seen is None:
        from kiro_crew.slack.events import SeenCache

        _seen = SeenCache()
    return not _seen.check_and_add("code_channel_command:" + key)


async def _pr_target(binding: Binding) -> tuple[str, str]:
    """``(worktree, branch)`` the channel's pull request is opened from ("" unknown)."""
    from kiro_crew.slack.code_channels.workspace.store import WorkspaceStore

    try:
        record = await asyncio.to_thread(WorkspaceStore().get, binding.team_id, binding.channel_id)
    except Exception:
        logger.debug("code channel PR target unreadable", exc_info=True)
        return "", ""
    if record is None:
        return "", ""
    return record.worktree, record.branch


def _prompt(
    name: str, user: str, text: str, *, worktree: str = "", branch: str = "", via: str = "command"
) -> str:
    if via == "action":
        asked = f"<@{user}> pressed {ACTION_LABELS.get(name, name)} on this Slack code channel's context bar."
    else:
        asked = f"<@{user}> ran /{name} in this Slack code channel."
    extra = f"\nThey added: {text}" if text else ""
    if name == SUMMARIZE:
        body = (
            "Summarize this session for the people in the channel: what was asked, what "
            "you did, what changed (files, branch, pull request, CI) and what is still "
            "open. Keep it short and reply here."
        )
    elif name == CREATE_PR:
        title = f' Title it "{text}".' if text else ""
        where = (
            f" The channel's branch is `{branch}`, checked out in the worktree `{worktree}`."
            if worktree and branch
            else ""
        )
        # The gate is the channel's capability (worktree + GitHub remote + gh), not
        # the bound agent's tools: a conductor that never edits or runs anything
        # itself still gets the command, and hands the work to a worker. The worker
        # must stay on the channel's own branch, or the context bar never sees the PR.
        body = (
            "Open a pull request for this channel's branch now: commit the finished work, "
            "push the branch and create the pull request with `gh pr create` (ready for "
            f"review unless asked otherwise).{where} If you do not edit files or run "
            "commands yourself (for example you orchestrate workers), dispatch one worker "
            "to do it in that same worktree on that same branch, not a new worktree or "
            "branch, so the channel's context bar picks the pull request up."
            f"{title} Reply here with its link once it is open."
        )
        extra = ""
    else:
        body = (
            "Wrap up this session: finish or set aside what is in flight, then call the "
            "code_channel_wrap_up tool with a short summary of what changed, the branch, "
            "the pull request and its CI, and anything left open. Do not archive the "
            "channel yourself: the people here archive it with the button the summary "
            "carries."
        )
    return f"{asked}\n{body}{extra}"


async def handle_slash(
    orch: Any, payload: dict[str, Any], respond: Callable[[str], Awaitable[None]]
) -> bool:
    """Handle a code-channel command; False when it is not one (normal routing runs)."""
    command = str(payload.get("command") or "").strip().lower().lstrip("/")
    if command not in _COMMANDS or not code_channels_on(live_config(orch)):
        return False
    channel = str(payload.get("channel_id") or "")
    user = str(payload.get("user_id") or "")
    text = str(payload.get("text") or "").strip()
    if not _first_delivery(payload):
        logger.info("code channel command /%s redelivered in %s; ignored", command, channel)
        return True
    await run_command(
        orch,
        command=command,
        channel=channel,
        user=user,
        team=str(payload.get("team_id") or ""),
        text=text,
        respond=respond,
    )
    return True


async def run_command(
    orch: Any,
    *,
    command: str,
    channel: str,
    user: str,
    team: str,
    text: str,
    respond: Callable[[str], Awaitable[None]],
    via: str = "command",
) -> None:
    """Every gate and effect of one channel command, after its de-duplication.

    The context bar's Create PR action (:mod:`.actions`) runs this same function
    with ``via="action"``, so a click is authorized, audited and delivered exactly
    as ``/create-pr`` is.
    """
    operation = "slack.code_channel.command"
    resources = f"channel={channel} command=/{command}" + (
        "" if via == "command" else f" via={via}"
    )
    if not surface.workspace_admitted(team):
        surface.audit(
            user, operation, "denied", f"{resources} team_id={team}", "enterprise_origin_mismatch"
        )
        return
    from kiro_crew.messaging.identity import channel_inbound_permitted

    if not await channel_inbound_permitted("slack"):
        # The same ceiling the message path applies before any side effect.
        surface.audit(user, operation, "denied", resources, "channels governance policy")
        return
    binding = await asyncio.to_thread(store().get_binding, channel) if channel else None
    if binding is None:
        surface.audit(user, operation, "denied", resources, "not a bound code channel")
        await respond(NOT_A_CODE_CHANNEL_TEXT)
        return
    from kiro_crew.slack.code_channels.routing import (
        Gate,
        InboundResult,
        _origin_of,
        channel_and_origin_permit,
        deliver_directed,
    )
    from kiro_crew.slack.handler_runtime.access import is_allowed_user

    if not is_allowed_user(user):
        surface.audit(user, operation, "denied", resources, "unauthorized sender")
        await respond(NOT_AUTHORIZED_TEXT)
        return
    origin = _origin_of(channel, binding, None)
    # The code channel's own activation applies on top of its origin's, exactly
    # as on the message path (routing.inbound_gate).
    if not channel_and_origin_permit(live_config(orch), channel, origin):
        surface.audit(
            user,
            operation,
            "denied",
            f"{resources} origin={origin}",
            "channel or origin does not admit the bot",
        )
        await respond(NOT_AUTHORIZED_TEXT)
        return
    if await surface.is_archived(binding):
        surface.audit(user, operation, "denied", resources, "archived")
        await respond(ARCHIVED_TEXT)
        return
    target = ("", "")
    if command == CREATE_PR:
        can_pr, reason = await pr_capability(binding)
        if not can_pr:
            surface.audit(user, operation, "allowed", f"{resources} result={reason}")
            _spawn(sync_commands(binding))  # the set offered /create-pr: correct it
            await respond(NO_PR_TEXT.get(reason, NO_PR_TEXT["not_a_repo"]))
            return
        target = await _pr_target(binding)
    surface.audit(user, operation, "allowed", resources)
    from kiro_crew.slack.code_channels import wrapup

    if command == WRAP_UP:
        # Armed before delivery: the turn it starts may begin before the call returns.
        await wrapup.request_wrap_up(channel, user)
    if via == "action":
        await respond(f"Running {ACTION_LABELS.get(command, command)}…")
    else:
        await respond(ACK_TEXT.format(name=command))
    event = {"channel": channel, "team": binding.team_id}
    delivered = await deliver_directed(
        orch,
        InboundResult(Gate.DIRECTED, binding=binding),
        event,
        _prompt(command, user, text, worktree=target[0], branch=target[1], via=via),
        user,
    )
    if command == WRAP_UP and not delivered:
        await wrapup.cancel_wrap_up(channel)
