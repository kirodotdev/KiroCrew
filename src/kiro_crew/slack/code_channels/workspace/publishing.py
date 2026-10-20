"""The workspace's own Slack writes: the context bar, the PR resource and the Code tab.

Split out of :mod:`.service` (the file-line cap): each function here is one of
:class:`~.service.WorkspaceService`'s publishing steps, taking the service and
called through its same-named method, so the service's state, locks and write
path (:meth:`~.service.WorkspaceService._call`, its write gate and fences) are
the ones it always used. Nothing here keeps state of its own.
"""

from __future__ import annotations

import asyncio
import hashlib
import json
import logging
import os
from dataclasses import dataclass, replace
from types import SimpleNamespace
from typing import TYPE_CHECKING, Any, Awaitable, Callable

from kiro_crew.slack.code_channels.workspace import git_ops
from kiro_crew.slack.code_channels.workspace.context_bar import BarState, build_items
from kiro_crew.slack.code_channels.workspace.pr import PullRequest
from kiro_crew.slack.code_channels.workspace.remote import parse_remote
from kiro_crew.slack.code_channels.workspace.store import WorkspaceRecord

if TYPE_CHECKING:
    from kiro_crew.slack.code_channels.workspace.service import WorkspaceService

logger = logging.getLogger(__name__)


@dataclass(frozen=True)
class BarExtras:
    """Where the context bar's channel actions and dashboard link come from.

    ``can_open_pr`` is the ``/create-pr`` capability (worktree + GitHub remote +
    ``gh``); ``dashboard_link`` builds the bound session's dashboard link with no
    credential in it (BLOCKING).
    """

    can_open_pr: Callable[[WorkspaceRecord], Awaitable[bool]]
    dashboard_link: Callable[[str], str]

    @classmethod
    def live(cls) -> BarExtras:
        async def _can_open_pr(record: WorkspaceRecord) -> bool:
            from kiro_crew.slack.code_channels.commands import pr_capability

            target = SimpleNamespace(team_id=record.team_id, channel_id=record.channel_id)
            ok, _reason = await pr_capability(target)  # type: ignore[arg-type]
            return ok

        def _dashboard_link(session_key: str) -> str:
            from kiro_crew.slack.code_channels.board.links import link_builder

            return link_builder()(session_key) if session_key else ""

        return cls(can_open_pr=_can_open_pr, dashboard_link=_dashboard_link)


def _digest(*parts: object) -> str:
    return hashlib.sha256(json.dumps(parts, sort_keys=True, default=str).encode()).hexdigest()


def _fork_point(cwd: str, record: WorkspaceRecord) -> str:
    """The commit the channel's work is measured from (BLOCKING): where HEAD forks
    from the ref the worktree was cut from, else the commit it was cut at. The
    Code tab's diff and the bar's Create PR read the same one."""
    base = record.base_sha
    if record.base_ref:
        moved = git_ops.resolve_commit(cwd, record.base_ref)
        base = (git_ops.merge_base(cwd, "HEAD", moved) if moved else "") or base
    return base


def _branch_has_commits(cwd: str, record: WorkspaceRecord) -> bool:
    """Whether the checkout has commits of its own past :func:`_fork_point`."""
    return git_ops.has_commits_since(cwd, _fork_point(cwd, record))


async def _board_link(channel_id: str) -> str:
    """The pinned board message's cached permalink, ``""`` until the publisher read one."""
    from kiro_crew.slack.code_channels.surface_store import get_async

    try:
        record = await get_async(channel_id)
    except Exception:
        return ""
    if record is None or not record.board_ts or record.board_link_ts != record.board_ts:
        return ""
    return record.board_link


def resource_for(record: WorkspaceRecord) -> dict[str, str] | None:
    """The ``agent_resource`` the channel's PR gives it (None without a PR)."""
    from kiro_crew.slack.code_channels.workspace.context_bar import pr_resource

    return pr_resource(record.pr_number, record.pr_url, record.pr_title, record.pr_state)


#: What :meth:`WorkspaceService._call` answers when its fence refused the write.
_FENCED = object()


def _agent_items(channel_id: str) -> list[dict[str, str]]:
    """Blocking: the agent's own bar items (none when they cannot be read)."""
    from kiro_crew.slack.code_channels.agent_bar import agent_items

    try:
        return list(agent_items(channel_id))
    except Exception:
        # The agent's items are decoration; the workspace bar publishes regardless.
        return []


async def _write_refusal(channel_id: str) -> str:
    """The package's write gate for one channel (fails closed: unknown refuses)."""
    try:
        from kiro_crew.slack.code_channels import surface

        return await surface.write_refusal(channel_id)
    except Exception:
        logger.debug("code channel %s: write gate failed", channel_id, exc_info=True)
        return "error"


def _view_gate(channel_id: str, fence: Callable[[], bool] | None) -> Callable[[], Awaitable[str]]:
    """The gate :func:`..views.make_room` asks right before each removal."""

    async def gate() -> str:
        refused = await _write_refusal(channel_id)
        if refused:
            return refused
        return "" if fence is None or _fence_admits(fence) else "out_of_scope"

    return gate


def _fence_admits(fence: Callable[[], bool]) -> bool:
    """A write fence's answer (fails closed)."""
    try:
        return bool(fence())
    except Exception:
        return False


async def bar_state(svc: WorkspaceService, record: WorkspaceRecord) -> BarState:
    cwd = svc._checkout_dir(record)
    if not cwd:
        return BarState(folder=record.project_dir, **(await svc._bar_extras(record, has_pr=False)))
    branch = await asyncio.to_thread(git_ops.current_branch, cwd)
    if not branch:
        head = await asyncio.to_thread(git_ops.short_head, cwd)
        branch = f"detached at {head}" if head else ""
    repo = parse_remote(await asyncio.to_thread(git_ops.remote_url, cwd))
    pushed = bool(repo) and await asyncio.to_thread(git_ops.has_remote_branch, cwd, branch)
    pr = (
        PullRequest(
            record.pr_number,
            record.pr_url,
            record.pr_state,
            record.pr_ci,
            title=record.pr_title,
        )
        if record.pr_number
        else None
    )
    extras = await svc._bar_extras(record, has_pr=pr is not None)
    if extras.get("can_open_pr"):
        extras["branch_has_commits"] = await asyncio.to_thread(_branch_has_commits, cwd, record)
    board = svc._cached_board(record.key)
    return BarState(
        repo=repo,
        repo_name=os.path.basename(record.repo_root.rstrip(os.sep)),
        branch=branch,
        branch_pushed=pushed,
        pr=pr,
        board=board,
        board_url=await _board_link(record.channel_id) if board is not None else "",
        **extras,
    )


async def bar_extras(
    svc: WorkspaceService, record: WorkspaceRecord, *, has_pr: bool
) -> dict[str, Any]:
    """The bar's channel actions and dashboard link (none without :class:`BarExtras`)."""
    extras = svc._extras
    if extras is None:
        return {}
    try:
        can_open = (not has_pr) and bool(record.worktree) and await extras.can_open_pr(record)
    except Exception:
        logger.debug("code channel %s: PR capability unread", record.channel_id)
        can_open = False
    try:
        link = await asyncio.to_thread(extras.dashboard_link, record.session_key)
    except Exception:
        link = ""
    return {
        "can_open_pr": can_open,
        "stop": bool(record.session_key) and record.key in svc._turning,
        "dashboard_url": link,
    }


async def publish_bar(svc: WorkspaceService, record: WorkspaceRecord, binding: Any = None) -> bool:
    """Publish the bar when it changed; False when a needed write failed.

    With *binding*, a bar that lists the board's workers is judged by the
    scope check again right before it is sent (the git reads before it take
    a moment): a refused channel gets the bar without them.
    """
    if binding is not None:
        svc._note_conductor(binding)  # every bar path: refresh, ensure, republish
    if record.slack_off:
        return False
    try:
        state = await svc._bar_state(record)
    except git_ops.SandboxUnavailable:
        state = BarState(folder=record.project_dir, **(await svc._bar_extras(record, has_pr=False)))
    if state.board is not None and binding is not None and not svc._board_still_admitted(binding):
        svc._forget_board(record.key, record.channel_id)
        state = replace(state, board=None, board_url="")
    return await svc._send_bar(record, state, binding)


async def send_bar(
    svc: WorkspaceService, record: WorkspaceRecord, state: BarState, binding: Any = None
) -> bool:
    """Send the bar *state* builds, unless it is exactly what Slack already has.

    Stop is decided HERE, from whether the bound turn runs right now, so a
    refresh that read the state before a turn started (or ended) never sends
    a stale Stop; and "already has" is the last send that landed, whichever
    publisher made it, so a turn start's bar is never mistaken for current.

    One per channel at a time (refresh, turn start, the agent's republish):
    the Stop decision, the "already has" check and the send run under the
    channel's bar lock, so a turn start never matches its bar against a send
    an older refresh has not landed yet, and that send never lands after it.
    """
    lock = svc._bar_send_locks.setdefault(record.key, asyncio.Lock())
    async with lock:
        return await _send_bar_locked(svc, record, state, binding)


async def _send_bar_locked(
    svc: WorkspaceService, record: WorkspaceRecord, state: BarState, binding: Any
) -> bool:
    from kiro_crew.slack.code_channels.agent_bar import merge
    from kiro_crew.slack.code_channels.workspace.context_bar import with_dashboard

    key = record.key
    # The agent's own items (a file read) are the one await. Everything the
    # bar's truth depends on -- Stop (is the bound turn running NOW?) and the
    # board's scope -- is decided after it, with no await before the send, so
    # neither a turn start nor a sharing change can slip in between.
    extra = await asyncio.to_thread(_agent_items, record.channel_id)
    state = replace(state, stop=bool(record.session_key) and key in svc._turning)
    if state.board is not None and binding is not None and not svc._board_still_admitted(binding):
        # A channel that turned shared, Off or Review gets no Workers item.
        svc._forget_board(key, record.channel_id)
        state = replace(state, board=None, board_url="")
    svc._bar_states[key] = state
    # The agent's items ride after these; the dashboard link takes a slot
    # only after them.
    base = build_items(replace(state, dashboard_url=""))
    # The keys the workspace's own items took (workers included), so the
    # agent's set_context_bar reply names exactly which of its items fit.
    svc._bar_base_keys[key] = [str(i.get("key") or "") for i in base]
    merged = merge(base, extra)[0]
    items = with_dashboard(merged, state.dashboard_url)
    digest = _digest(items)
    last = svc._sent_bar.get(key, record.bar_hash)
    if digest == last or (not items and not last):
        record.bar_hash = digest
        return True
    answer = await svc._call(
        "set_context_bar",
        record,
        items,
        fence=svc._write_fence(binding, board=state.board is not None),
    )
    if answer is _FENCED:
        if state.board is None:
            return False  # Off or Review now: nothing goes out
        # The channel left the board's scope at the last moment: the bar
        # goes out once more without the Workers item, or not at all.
        svc._forget_board(key, record.channel_id)
        # Under the same lock (an asyncio lock is not reentrant).
        return await _send_bar_locked(
            svc, record, replace(state, board=None, board_url=""), binding
        )
    if answer is not None:
        record.bar_hash = digest
        svc._sent_bar[key] = digest
        return True
    return False


async def turn_bar(svc: WorkspaceService, binding: Any) -> None:
    """Show Stop for a turn start, from the bar's last state (no git read).

    Costs one ``setProperties``, and only when the item set changed. The
    board link is re-read (a store read) so a reposted board is not linked
    by its old message. With no state yet in this process (a restart) the
    bar is built once in full.
    """
    team, channel = str(binding.team_id or ""), str(binding.channel_id or "")
    key = f"{team}:{channel}"
    async with svc._lock(key):
        record = await asyncio.to_thread(svc._store.get, team, channel)
        if record is None or record.slack_off:
            return
        before = (record.bar_hash, record.slack_off)
        state = svc._bar_states.get(key)
        if state is None:
            await svc._publish_bar(record, binding)
        else:
            if state.board is not None:
                # A bar sent outside a refresh: the cached board shows only
                # while the channel's scope still admits it, as republish_bar.
                svc._drop_board_unless_admitted(binding, key)
                if svc._cached_board(key) is not None:
                    state = replace(state, board_url=await _board_link(channel))
                else:
                    state = replace(state, board=None, board_url="")
            await svc._send_bar(record, state, binding)
        if (record.bar_hash, record.slack_off) != before:
            await asyncio.to_thread(svc._store.put, record)


async def publish_resource(
    svc: WorkspaceService, record: WorkspaceRecord, binding: Any = None
) -> None:
    """Set the channel's ``agent_resource`` to its pull request, when that changed.

    Sent once a PR exists and again whenever what it shows moves: a new PR
    number, a renamed PR, or the PR merging or closing. A channel whose PR
    goes away keeps the last one (Slack documents no way to clear the
    property; the next PR replaces it).
    """
    if record.slack_off:
        return
    resource = resource_for(record)
    if resource is None:
        return
    digest = _digest(resource)
    if digest == record.resource_hash:
        return
    answer = await svc._call(
        "set_agent_resource",
        record,
        fence=svc._write_fence(binding, board=False),
        **resource,
    )
    if answer is not None and answer is not _FENCED:
        record.resource_hash = digest


async def publish_diff(svc: WorkspaceService, record: WorkspaceRecord, binding: Any = None) -> None:
    if binding is not None:
        svc._note_conductor(binding)
    if record.slack_off:
        return
    if not record.worktree:
        # No tree to diff (rebound to a folder that is not a repository): a
        # tab an earlier tree opened would describe code this channel no
        # longer works in, so it goes.
        await svc._clear_diff(record)
        return
    if not record.base_sha:
        return
    board = svc._cached_board(record.key)
    workers_tab = board is not None  # what is up now may list workers
    built = await asyncio.to_thread(svc._build_diff, record, board)
    if board is not None and binding is not None and not svc._board_still_admitted(binding):
        # The build took seconds of git work: a channel that turned shared, or
        # was set to Off or Review, meanwhile gets no worker titles, branches
        # or committed code. Only the channel's own diff is rebuilt and sent.
        svc._forget_board(record.key, record.channel_id)
        board = None
        built = await asyncio.to_thread(svc._build_diff, record, None)
        if built is None:
            # No channel-only diff to send in its place: the tab published
            # earlier may list workers, so it is taken down, not left up.
            await svc._clear_diff(record)
            return
    if built is None:
        return
    content, base_label, head_label = built
    if not content:
        await svc._clear_diff(record)
        return
    digest = _digest(content, base_label, head_label)
    if digest == record.diff_hash:
        return
    view_id = await svc._call(
        "set_diff_view",
        record,
        content,
        base_branch=base_label,
        head_branch=head_label,
        room_for="diff",
        fence=svc._write_fence(binding, board=board is not None),
    )
    if view_id is _FENCED and board is not None:
        # Left the board's scope at the last moment: only the channel's own
        # diff may go out, under the activation fence.
        svc._forget_board(record.key, record.channel_id)
        own = await asyncio.to_thread(svc._build_diff, record, None)
        if own is None or not own[0]:
            await svc._clear_diff(record)
            return
        content, base_label, head_label = own
        digest = _digest(content, base_label, head_label)
        view_id = await svc._call(
            "set_diff_view",
            record,
            content,
            base_branch=base_label,
            head_branch=head_label,
            room_for="diff",
            fence=svc._write_fence(binding, board=False),
        )
    if view_id is _FENCED:
        if workers_tab:
            # Off or Review too: the tab up may list workers, so it is taken
            # down (the safe direction) rather than left showing them.
            await svc._clear_diff(record)
        return
    if view_id is not None:
        record.diff_hash = digest
        if isinstance(view_id, str) and view_id:
            record.view_id = view_id


async def clear_diff(svc: WorkspaceService, record: WorkspaceRecord) -> None:
    """No changes any more (reverted, or merged into the base): drop the diff tab.

    ``setView`` refuses an empty diff, so a tab that was published is removed
    instead, whenever its view id is known: the digest alone may have been
    reset for a republish (a rebind, a raced refresh) while the tab stands.
    Never published at all, there is simply no tab yet.
    """
    if not record.view_id:
        return
    if await svc._call("remove_view", record, record.view_id) is not None:
        record.diff_hash, record.view_id = "", ""


def write_fence(svc: WorkspaceService, binding: Any, *, board: bool) -> Callable[[], bool] | None:
    """The scope a write must still pass right before it is sent.

    With the board's worker content in it: THE board scope check (sharing,
    archive, stop and activation). Without: the channel's activation (Off or
    Review refuse). None without a binding to judge (no fence).
    """
    from kiro_crew.slack.code_channels.workspace.service import (  # read when run
        _activation_admits,
    )

    if binding is None:
        return None
    if board:
        return lambda: svc._board_still_admitted(binding)
    return lambda: _activation_admits(binding)


async def republish_bar(svc: WorkspaceService, binding: Any) -> str | None:
    """Publish the context bar now (the agent changed its own items).

    Returns ``""`` when the bar is current, a Slack error code (or
    ``publish_failed``) when it is not, and ``None`` when there is no
    workspace record to publish from (the caller publishes the agent's items).
    """
    from kiro_crew.slack.code_channels.workspace.service import (  # read when run
        _activation_admits,
        _gate_open,
        _write_refusal,
    )

    team, channel = str(binding.team_id or ""), str(binding.channel_id or "")
    if not channel or not _gate_open():
        return None
    async with svc._lock(f"{team}:{channel}"):
        record = await asyncio.to_thread(svc._store.get, team, channel)
        if record is None:
            return None
        svc._drop_board_unless_admitted(binding, f"{team}:{channel}")
        published = await svc._publish_bar(record, binding)
        await asyncio.to_thread(svc._store.put, record)
        if published:
            return ""
        refused = "" if record.slack_off else await _write_refusal(channel)
        if refused:
            return refused  # policy, archive or Slack's Stop while it was built
        if not record.slack_off and not _activation_admits(binding):
            return "channel_off"  # set to Off or Review while the bar was built
        return record.slack_off or "publish_failed"


async def refresh_pr(svc: WorkspaceService, record: WorkspaceRecord) -> None:
    from kiro_crew.slack.code_channels.workspace.service import (  # read when run
        git_ops,
        parse_remote,
    )

    cwd = svc._checkout_dir(record)
    if not cwd:
        return
    branch = await asyncio.to_thread(git_ops.current_branch, cwd)
    repo = parse_remote(await asyncio.to_thread(git_ops.remote_url, cwd))
    if not branch or repo is None:
        return
    lookup = await svc._find_pr(repo, branch)
    if lookup.status == "found" and lookup.pr is not None:
        pr = lookup.pr
        same = pr.number == record.pr_number
        record.pr_number, record.pr_url = pr.number, pr.url
        record.pr_state, record.pr_ci = pr.state, pr.ci
        record.pr_title = pr.title or (record.pr_title if same else "")
    elif lookup.status == "none":
        record.pr_number, record.pr_url, record.pr_state, record.pr_ci = 0, "", "", ""
        record.pr_title = ""
    else:
        # gh missing or signed out: keep the PR last seen, drop its CI (stale).
        record.pr_ci = ""


async def call(
    svc: WorkspaceService,
    method: str,
    record: WorkspaceRecord,
    *args: Any,
    room_for: str = "",
    fence: Callable[[], bool] | None = None,
    **kwargs: Any,
) -> Any:
    """One write through the core's client. Returns its answer, or None when it failed.

    The core's client maps Slack's answer to :class:`CodeChannelApiError` and
    hands every refusal to the feature's process latch itself. Here a
    channel-specific refusal (:data:`_CHANNEL_DISABLING_ERRORS`) turns
    publishing off for this channel, is SEL-recorded and is told to the
    dashboard once (:mod:`..notices`); anything else is logged and retried on
    the next turn, because no digest is advanced. A view write (*room_for* set)
    refused at Slack's five-view cap removes the stalest view of
    ours and is sent once more (:func:`..views.make_room`).

    *fence*, when given, is asked right before the write goes out (and again
    before the resend after making room): the awaits before it can outlast a
    change of the channel's sharing or activation. A refused fence sends
    nothing and answers :data:`_FENCED`.
    """
    from kiro_crew.slack.code_channels.workspace.service import (  # read when run
        _CHANNEL_DISABLING_ERRORS,
        _FENCED,
        CodeChannelApiError,
        CodeChannelError,
        _audit,
        _fence_admits,
        _view_gate,
        _write_refusal,
    )

    client = svc._client()
    if client is None:
        return None  # Slack not connected yet: the next turn retries
    # THE write gate, its one await first: the channels policy, an archive
    # this process fenced, Slack's own Stop (no agents.* call until the
    # channel's next turn), then the fence (activation, or the board's scope)
    # -- all read with no await before the write. A refusal advances
    # nothing, so the next pass sends it.
    if await _write_refusal(record.channel_id):
        return None
    if fence is not None and not _fence_admits(fence):
        return _FENCED
    try:
        answer = await getattr(client, method)(record.channel_id, *args, **kwargs)
    except CodeChannelApiError as exc:
        code = exc.code
        if code == "too_many_views" and room_for:
            from kiro_crew.slack.code_channels.views import make_room

            if await make_room(
                client,
                record.channel_id,
                # The Code tab is not in the view record: every recorded
                # tab of ours may make room for it (none is kept by name).
                for_key="",
                admits=_view_gate(record.channel_id, fence),
            ):
                return await svc._call(method, record, *args, fence=fence, **kwargs)
    except CodeChannelError as exc:  # refused locally, before it was sent
        logger.warning("code channel %s: %s not sent: %s", record.channel_id, method, exc)
        return None
    else:
        return answer if answer is not None else True
    if code in _CHANNEL_DISABLING_ERRORS:
        record.slack_off = code
        _audit(
            "code_channel_publish",
            "denied",
            f"channel={record.channel_id} method={method}",
            code,
        )
        logger.info(
            "code channel %s: %s refused (%s); publishing off", record.channel_id, method, code
        )
        from kiro_crew.slack.code_channels.notices import notify_refusal

        await notify_refusal(record.channel_id, code, session_key=record.session_key)
    else:
        logger.warning("code channel %s: %s failed: %s", record.channel_id, method, code or "error")
    return None
