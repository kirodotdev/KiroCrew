"""Code-channel workspace: one git worktree per channel, its diff and context bar.

Lifecycle, all driven by the core's hooks (:func:`register`):

* **bind / turn start** — :meth:`WorkspaceService.ensure`. When the binding's
  project (else the project its session was created with) is in a git
  repository, cut a worktree on a channel-derived branch from the repository's
  default branch (``origin/HEAD``, else ``HEAD``), record the base sha, and
  point the bound dashboard session at it. A project that is not a repository,
  or no project, gets no worktree and no error. Then publish the context bar.
  Idempotent: a second call reuses the record.
* **turn end** — :meth:`WorkspaceService.refresh`. Republish the context bar
  (branch, PR, CI) and the Code tab's diff, each only when it changed.

Removing a channel's worktree when the channel is archived is not done here; it
belongs to the code-channel wrap-up, which owns the archive path.

Nothing here raises into the turn: every failure degrades (no worktree, no
view, no PR item) and is logged. A Slack error that means this channel cannot
take properties or views (``not_agent_channel``...) switches publishing off for
that channel; the core's client hands one that means the install cannot use
code channels to the feature's process latch.
"""

from __future__ import annotations

import asyncio
import contextlib
import contextvars
import logging
import os
import subprocess
import time
from dataclasses import replace
from types import SimpleNamespace
from typing import TYPE_CHECKING, Any, AsyncIterator, Awaitable, Callable

from kiro_crew.platform.context import redact_via_context
from kiro_crew.security import is_sensitive_path
from kiro_crew.sel import sel
from kiro_crew.slack.code_channels.models import (  # noqa: F401 - publishing.call reads them
    CodeChannelApiError,
    CodeChannelError,
)
from kiro_crew.slack.code_channels.workspace import folders, git_ops, publishing
from kiro_crew.slack.code_channels.workspace.board_cache import BoardCache
from kiro_crew.slack.code_channels.workspace.context_bar import BarState
from kiro_crew.slack.code_channels.workspace.diff import (
    CLEAN,
    MASK_LINES,
    MAX_VIEW_BYTES,
    OMITTED,
    WITHHOLD,
    DiffGroup,
    assemble,
    assemble_groups,
    render_untracked,
    screen_tracked,
)
from kiro_crew.slack.code_channels.workspace.pr import (
    PrLookup,
    find_pull_request,
    pull_request_by_number,
)
from kiro_crew.slack.code_channels.workspace.publishing import (  # noqa: F401 - re-exported
    _FENCED,
    BarExtras,
    _fence_admits,
    _fork_point,
    _view_gate,
    _write_refusal,
    resource_for,
)
from kiro_crew.slack.code_channels.workspace.remote import (  # noqa: F401 - publishing reads it
    RepoRef,
    parse_remote,
)
from kiro_crew.slack.code_channels.workspace.store import (
    REPAIRED,
    WorkspaceRecord,
    WorkspaceStore,
    record_key,
)
from kiro_crew.slack.code_channels.workspace.worker_prs import WorkerPrs

if TYPE_CHECKING:
    from kiro_crew.slack.code_channels.board.model import BoardSnapshot

logger = logging.getLogger(__name__)

#: Most untracked files rendered into one diff, and the bytes of their listing.
_MAX_UNTRACKED = 500
_UNTRACKED_LIST_CAP = 256 * 1024
#: How much whole-file-context diff is read and screened per refresh. Files past
#: it are left out (with the truncation note), never shown unscreened.
_SCAN_BYTES = 8 * MAX_VIEW_BYTES

#: How often the folder watcher reads which sessions in a channel's folder are
#: running (an in-memory read of the dashboard's slots; no git, no Slack call).
_WATCH_SECS = 15.0
#: While a board row is working or waiting, the longest the board goes without a
#: refresh: the watcher's backstop for whatever the event triggers missed (and
#: what keeps the board's "Updated" stamp and elapsed times current).
BOARD_BACKSTOP_SECS = 60.0

#: Slack answers that mean "this channel cannot take views or properties": the
#: workspace stops publishing for the channel instead of retrying every turn.
#: The install-wide answers (``feature_disabled`` / ``missing_scope`` /
#: ``not_allowed_token_type``) are not here: the core's client hands every
#: refusal to the feature's process latch, which a reconnect re-probe lifts, and
#: persisting them per channel would keep a channel dark after the scope is
#: granted.
#: ``is_archived`` is not here either: an unarchive undoes it, so it is retried
#: like a transient error, and nothing is published while the binding is
#: stamped archived (:func:`_archived`).
_CHANNEL_DISABLING_ERRORS = frozenset(
    {
        "not_agent_channel",
        "not_authorized",
        "channel_not_found",
        "enterprise_is_restricted",
        "restricted_action",
        "user_not_enabled",
        "app_not_installed_on_team",
    }
)

#: ``worktree_skipped`` reasons that describe the project or host, so a later
#: bind would decide the same. Any other reason is a git error and is retried,
#: at most :data:`_MAX_BIND_ATTEMPTS` times in all.
_SETTLED_SKIPS = frozenset(
    {"", "no_project", "project_missing", "not_a_repo", "sensitive_path", "sandbox_unavailable"}
)
_MAX_BIND_ATTEMPTS = 3
#: What a refresh publishes and so writes back; every other field is the bind's.
_REFRESH_FIELDS = (
    "pr_number",
    "pr_url",
    "pr_state",
    "pr_ci",
    "pr_title",
    "resource_hash",
    "bar_hash",
    "diff_hash",
    "view_id",
)
#: What a rebind that re-adopts the same worktree keeps from the stored record.
_ADOPTED_FIELDS = ("folder_id", "pointed_session", "view_id", "diff_hash")
#: What every replacement keeps, whatever tree it binds: the channel's diff tab.
#: The tab belongs to the channel, so a rebind to another repository (or to a
#: folder with no worktree) must still be able to remove the tab the old tree's
#: diff opened.
_CHANNEL_FIELDS = ("view_id",)

#: Longest the archive's cleanup waits for a bind still cutting the worktree.
_QUIET_WAIT_SECS = 120.0

#: Points a dashboard session at a directory; returns whether it was applied.
ProjectSetter = Callable[[str, str], Awaitable[bool]]
#: Reads the project a dashboard session currently has ("" when unknown).
ProjectReader = Callable[[str], Awaitable[str]]
PrFinder = Callable[..., Awaitable[PrLookup]]
#: Reads the channel conductor's work board (``None``: no board for this channel).
BoardReader = Callable[[Any], Awaitable["BoardSnapshot | None"]]
#: Publishes the board message; gets the binding and the PR-filled snapshot.
BoardPublisher = Callable[[Any, "BoardSnapshot"], Awaitable[Any]]

#: How long a worker's PR reading is reused before ``gh`` is asked again: a live
#: PR's CI moves, a merged or closed one does not.
_PR_TTL_LIVE = 60.0
_PR_TTL_SETTLED = 900.0
#: Debounce for refreshes the board's triggers arm (turn starts, approvals,
#: ledger writes), so a burst of reports costs one publish.
BOARD_DEBOUNCE_SECS = 3.0


def _audit(operation: str, outcome: str, resources: str, error: str = "") -> None:
    try:
        sel().log_api_access(
            caller="slack",
            operation=operation,
            outcome=outcome,
            source="slack",
            resources=resources,
            error=error,
        )
    except Exception:  # pragma: no cover - the audit must never break a turn
        logger.debug("code channel SEL write failed", exc_info=True)


def _git_failed(record: WorkspaceRecord, exc: BaseException, resources: str) -> WorkspaceRecord:
    """A git call at bind that timed out or hit an OS error, as a counted failure.

    Not a settled reason: the attempt is stored and counted like any git error,
    so the bind cap stops a repo whose git always times out.
    """
    timed_out = isinstance(exc, subprocess.TimeoutExpired)
    record.worktree_skipped = "worktree_timeout" if timed_out else "worktree_error"
    _audit("code_channel_worktree", "error", resources, record.worktree_skipped)
    return record


def _turn_ended(before: tuple[bool, int] | None, now: tuple[bool, int], observed: bool) -> bool:
    """Whether a session's turn finished since the last watch pass.

    Running then idle is the plain case. A turn short enough to start and end
    between two passes shows only as a new generation; a session that appeared
    and already ran one does too, once the channel has been watched before (the
    first pass after a restart only records what it sees).
    """
    running, generation = now
    if running:
        return False
    if before is None:
        return observed and generation > 0
    return before[0] or before[1] != generation


def _turn_started(before: tuple[bool, int] | None, now: tuple[bool, int], observed: bool) -> bool:
    """Whether a session is running a turn it was not running at the last watch pass.

    Idle then running is the plain case; a new generation while still running is
    a later turn that started after the last one ended between two passes. Like
    :func:`_turn_ended`, the first pass after a restart only records what it sees.
    """
    running, generation = now
    if not running:
        return False
    if before is None:
        return observed
    return not before[0] or before[1] != generation


def _redact_bytes(data: bytes) -> bytes:
    """``data`` through the outbound redaction the active platform installs, as UTF-8."""
    if not data:
        return data
    return redact_via_context(data.decode("utf-8", "replace")).encode("utf-8")


def _classify_version(text: str) -> str:
    """One complete file version against the outbound credential policy.

    Line by line catches everything a shown line holds whole; anything only the
    whole text reveals (a PEM key's body) means the file is never shown.
    """
    redacted = redact_via_context(text)
    if redacted == text:
        return CLEAN
    if redacted == "\n".join(redact_via_context(line) for line in text.split("\n")):
        return MASK_LINES
    return WITHHOLD


def _stamped_archived(channel_id: str) -> bool:
    """Whether the STORED binding is stamped archived, for refusing a new worktree.

    Unlike :func:`_archived` (which decides whether to publish, and lets the
    caller's copy stand in), only the store counts here, and an unreadable store
    counts as archived: cutting a worktree is the action to refuse, since the one
    an Archive removed would be cut again and orphaned. Blocking.
    """
    from kiro_crew.slack.code_channels.store import store as channel_store

    try:
        stored = channel_store().get_binding(channel_id)
    except Exception:
        logger.debug("code channel %s: binding unreadable", channel_id, exc_info=True)
        return True
    return stored is not None and bool(stored.archived_at)


def _gate_open() -> bool:
    """The core's one gate: toggle on AND the install can run code channels."""
    from kiro_crew.slack.code_channels.binding import code_channels_on, live_config

    try:
        return code_channels_on(live_config())
    except Exception:
        logger.debug("code-channel gate failed; treating as off", exc_info=True)
        return False


def _archived(binding: Any) -> bool:
    """Whether the channel is archived, as its stored binding says now.

    *binding* may be a snapshot from before an archive or an unarchive, so the
    channel store's current binding decides; *binding* only stands in when the
    store cannot be read. Reads the store: call it off the event loop.
    """
    try:
        from kiro_crew.slack.code_channels.store import store as channel_store

        current = channel_store().get_binding(str(binding.channel_id or ""))
    except Exception:
        current = None
    return bool(getattr(current if current is not None else binding, "archived_at", 0))


#: Refused status hand-backs in a row after which the watch stops re-sending one.
_STATUS_RETRIES = 4


def _activation_admits(binding: Any) -> bool:
    """The channel and its origin admit the bot (Off or Review refuse); fails closed."""
    from kiro_crew.slack.code_channels import surface
    from kiro_crew.slack.code_channels.binding import live_config

    try:
        return bool(surface.channel_admits(live_config(), binding))
    except Exception:
        return False


def _clear_aggregate(binding: Any) -> None:
    """Clear the conductor slot's ``work_aggregate`` (its board is not shown). Never raises."""
    try:
        from kiro_crew.slack.code_channels import board as board_pkg
        from kiro_crew.slack.code_channels import surface

        board_pkg.clear_aggregate(
            surface.dashboard_state(), str(getattr(binding, "session_key", "") or "")
        )
    except Exception:
        logger.debug("code channel: work aggregate not cleared", exc_info=True)


def _sharing_unknown(channel_id: str) -> bool:
    """Whether the channel's "is it shared?" reading is missing or stale (being re-read)."""
    from kiro_crew.slack.code_channels import audience

    try:
        return bool(channel_id) and audience.channel_shared(channel_id) is None
    except Exception:
        return False


def _board_refused_permanently(channel_id: str) -> bool:
    """Whether Slack's last answer to the channel's board was one no retry cures."""
    from kiro_crew.slack.code_channels.board import publisher

    try:
        return publisher.refused_permanently(channel_id)
    except Exception:
        return False


async def _set_status(channel_id: str, status: str) -> bool:
    """The channel's Slack session status, through the core's status writer."""
    from kiro_crew.slack.code_channels import status as channel_status

    try:
        return await channel_status.set_status(channel_id, status)
    except Exception:
        logger.debug("code channel status %s not set", status, exc_info=True)
        return False


def _trusted_worktree(record: WorkspaceRecord) -> bool:
    """Blocking: whether the record's worktree is really this channel's.

    The record is a file on disk. Before the gateway reads files under the
    worktree outside any sandbox (the diff's new-file sections), git must
    confirm the record (:func:`git_ops.is_channel_worktree`) and neither path
    may be a sensitive one. A record that fails is logged and SEL-recorded, and
    the read does not happen.
    """
    ok = (
        bool(record.worktree and record.repo_root)
        and not is_sensitive_path(record.worktree)
        and not is_sensitive_path(record.repo_root)
        and git_ops.is_channel_worktree(
            record.repo_root, record.worktree, record.branch, record.team_id, record.channel_id
        )
    )
    if not ok:
        logger.warning(
            "code channel %s: workspace record does not match git; ignored", record.channel_id
        )
        _audit(
            "code_channel_worktree",
            "denied",
            f"channel={record.channel_id} path={record.worktree} branch={record.branch}",
            "record does not name this channel's registered worktree",
        )
    return ok


def _remembered_root(channel: str, top: str, branch: str, team: str) -> str:
    """Blocking: the checkout to cut the channel's worktree beside, for top level ``top``.

    ``top`` unless the channel's binding remembers the checkout its worktree
    was first cut from AND git confirms ``top`` as this channel's worktree of
    that checkout (:func:`git_ops.is_channel_worktree`).
    """
    from kiro_crew.slack.code_channels.store import store as channel_store

    binding = channel_store().get_binding(channel)
    remembered = str(getattr(binding, "repo_root", "") or "")
    if not remembered or os.path.realpath(remembered) == os.path.realpath(top):
        return top
    if is_sensitive_path(remembered) or not os.path.isdir(remembered):
        return top
    if git_ops.is_channel_worktree(remembered, top, branch, team, channel):
        return remembered
    return top


def _removed_channel_tree(channel: str, project: str, branch: str) -> tuple[str, str] | None:
    """Blocking: ``(checkout, subdir)`` when the missing ``project`` is (a directory
    in) the channel's own worktree destination beside its remembered checkout.

    Only that exact destination counts: a missing project anywhere else is not
    this channel's tree, and is reported missing as before.
    """
    from kiro_crew.slack.code_channels.store import store as channel_store

    binding = channel_store().get_binding(channel)
    remembered = str(getattr(binding, "repo_root", "") or "")
    if not remembered or is_sensitive_path(remembered) or not os.path.isdir(remembered):
        return None
    dest = os.path.realpath(git_ops.channel_destination(remembered, branch))
    if project != dest and not project.startswith(dest + os.sep):
        return None
    return os.path.realpath(remembered), os.path.relpath(project, dest)


def _remember_root(channel: str, root: str, worktree: str, branch: str, team: str) -> None:
    """Blocking: record ``root`` on the channel's binding. Never raises.

    A root that IS the worktree (a rebuild that could not recover the checkout)
    is never remembered: it would make the wrong answer permanent. Nor is a
    remembered root replaced while git still confirms the tree as this
    channel's tree of it (a bind from some other top level that adopted the
    same tree by branch).
    """
    if os.path.realpath(root) == os.path.realpath(worktree):
        return
    from kiro_crew.slack.code_channels.store import StoreWriteError
    from kiro_crew.slack.code_channels.store import store as channel_store

    try:
        binding = channel_store().get_binding(channel)
        if binding is None or binding.repo_root == root:
            return
        kept = binding.repo_root
        if kept and os.path.isdir(kept) and not is_sensitive_path(kept):
            if git_ops.is_channel_worktree(kept, worktree, branch, team, channel):
                return
        channel_store().update_binding(channel, repo_root=root)
    except StoreWriteError:
        logger.warning("code channel %s: workspace root not remembered", channel)
    except Exception:
        # A hint only: losing it must not cost the record that was just built.
        logger.warning("code channel %s: workspace root not remembered", channel, exc_info=True)


def _worker_header(index: int, total: int, row: Any) -> str:
    from kiro_crew.slack.code_channels.board import model
    from kiro_crew.slack.code_channels.board.render import pr_text

    parts = [f"Worker {index} of {total}: {row.title}", f"branch {row.branch}"]
    pr = pr_text(row)
    if pr:
        parts.append(pr)
    parts.append(model.row_label(row))
    return redact_via_context(" · ".join(parts))


def _worker_groups(cwd: str, board: Any, channel_branch: str) -> list[DiffGroup]:
    """Blocking: one diff group per worker branch with commits of its own.

    Each worker's change is ``merge-base(channel HEAD, worker) .. worker``, read
    from git's object database in the channel's checkout (worktrees of one
    repository share it), so nothing in the worker's own worktree is opened. The
    merge base is taken against the channel's HEAD: a worker branch cut from the
    channel branch shows only its own commits, and one already merged into the
    channel shows nothing (its change is in the channel's group). The branch name
    is worker-reported, so only a valid name under ``refs/heads/`` or
    ``refs/remotes/origin/`` is looked up (:func:`git_ops.branch_commit`).
    """
    rows = list(getattr(board, "rows", ()))
    # The workers the board's plan shows (all of them up to Slack's task cap);
    # the raw-scan budget below bounds the reads, not a row cap.
    # Settled workers beyond the board's own view cost git calls for changes
    # that are usually merged already: they are left out (and said so).
    view = {id(r) for r in getattr(board, "shown", rows)}
    shown = {
        id(r)
        for r in getattr(board, "plan_rows", rows)
        if id(r) in view or not getattr(r, "settled", False)
    }
    groups: list[DiffGroup] = []
    spent = 0  # screened bytes, the view's share
    scanned = 0  # raw whole-file bytes read, the refresh's work
    lost = False
    # Numbered by board position, so "Worker 2 of 5" is the board's second row.
    for index, row in enumerate(rows, start=1):
        if not row.branch or row.branch == channel_branch:
            continue
        if id(row) not in shown:
            # A worker the plan leaves out ("+N more"): its change is not read,
            # and the view says something was left out rather than hiding it.
            lost = True
            continue
        if spent > MAX_VIEW_BYTES or scanned >= _SCAN_BYTES:
            # Either budget spent: the view would be cut anyway, and every
            # refresh would repeat the reads. The rest are left out, and said so.
            lost = True
            break
        head = git_ops.branch_commit(cwd, row.branch)
        if not head:
            continue
        fork = git_ops.merge_base(cwd, "HEAD", head)
        if not fork or fork == head:
            continue
        got = git_ops.commit_range_diff(cwd, fork, head, cap=_SCAN_BYTES - scanned)
        if got is None:
            lost = True  # git failed on a branch with commits of its own
            continue
        scanned += len(got[0])
        # The same whole-file screen as the channel's own diff: a hunk from the
        # middle of a key has neither marker, so line-level redaction alone
        # would miss it. A withheld file or an unreadable section shows as cut.
        body, cut = screen_tracked(
            got[0],
            truncated=got[1],
            classify=_classify_version,
            mask=redact_via_context,
            sensitive=lambda rel: is_sensitive_path(os.path.join(cwd, rel)),
        )
        spent += len(body)
        groups.append(
            DiffGroup(
                header=_worker_header(index, len(rows), row),
                tracked=_redact_bytes(body),
                tracked_truncated=cut,
            )
        )
    if lost:
        # An empty group that was cut: the assembly drops its header and adds
        # the truncation note, so a left-out worker is never silently absent.
        groups.append(DiffGroup(header="", tracked=b"", tracked_truncated=True))
    return groups


def _bindings_by_channel() -> dict[str, Any]:
    """Every stored code-channel binding by channel id (blocking: run off the loop).

    An unreadable store reads as no bindings, so the watch skips every channel
    rather than refreshing one from a stand-in.
    """
    try:
        from kiro_crew.slack.code_channels.store import store

        return {b.channel_id: b for b in store().bindings()}
    except Exception:
        logger.debug("code channel watch: bindings unreadable", exc_info=True)
        return {}


async def _stored_binding(binding: Any) -> Any:
    """The channel store's current binding for *binding*'s channel and team, else *binding*."""
    from kiro_crew.slack.code_channels.store import store

    try:
        stored = await asyncio.to_thread(store().get_binding, str(binding.channel_id or ""))
    except Exception:
        return binding
    if stored is None or stored.team_id != binding.team_id:
        return binding
    return stored


def _find_slot(state: Any, session_key: str) -> Any | None:
    """The dashboard slot for a slot key or a linked/effective session key."""
    if state is None or not session_key:
        return None
    slots = getattr(state, "_slots", {}) or {}
    slot = slots.get(session_key)
    if slot is None and hasattr(state, "get_linked_slot"):
        slot = state.get_linked_slot(session_key)
    if slot is None:
        from kiro_crew.dashboard.chat_utils import effective_session_key

        slot = next(
            (s for s in list(slots.values()) if effective_session_key(s) == session_key),
            None,
        )
    return slot


def dashboard_project_reader(state_getter: Callable[[], Any]) -> ProjectReader:
    """A :data:`ProjectReader`: the project a dashboard session currently has."""

    async def _read(session_key: str) -> str:
        slot = _find_slot(state_getter(), session_key)
        return str(getattr(slot, "project", "") or "") if slot is not None else ""

    return _read


def dashboard_project_setter(state_getter: Callable[[], Any]) -> ProjectSetter:
    """A :data:`ProjectSetter` that applies the dashboard's own ``set_project`` path.

    The same function the ``set_project`` MCP directive runs: sensitive-path and
    data-home-overlap refusals, recent-projects bookkeeping, and the deferred
    session reset that makes the next turn cold-start in the new directory.
    ``session_key`` may be a slot key or a linked/effective session key.
    """

    async def _set(session_key: str, path: str) -> bool:
        state = state_getter()
        slot = _find_slot(state, session_key)
        if slot is None:
            return False
        current = str(getattr(slot, "project", "") or "")
        same = await asyncio.to_thread(lambda: os.path.realpath(current) == os.path.realpath(path))
        if same:
            return True
        from kiro_crew.dashboard.session_directive_apply import _set_project

        where = f"session={session_key} project={path}"
        try:
            result = await _set_project(state, slot, {"project": path})
        except Exception as exc:  # _DirectiveDenied: a refused path
            logger.warning("code channel: session project not moved to worktree: %s", exc)
            _audit("code_channel_session_project", "denied", where, str(exc)[:200])
            return False
        if str(result).startswith("Error"):
            _audit("code_channel_session_project", "denied", where, str(result)[:200])
            return False
        _audit("code_channel_session_project", "allowed", where)
        return True

    return _set


class WorkspaceService(BoardCache, WorkerPrs):
    """Owns every code channel's workspace. One instance per gateway."""

    def __init__(
        self,
        client: Callable[[], Any],
        *,
        store: WorkspaceStore | None = None,
        set_project: ProjectSetter | None = None,
        project_of: ProjectReader | None = None,
        find_pr: PrFinder = find_pull_request,
        create_folder: folders.FolderCreator = folders.create_channel_folder,
        find_pr_number: PrFinder = pull_request_by_number,
        read_board: BoardReader | None = None,
        publish_board: BoardPublisher | None = None,
        clock: Callable[[], float] | None = None,
        extras: BarExtras | None = None,
    ) -> None:
        #: Returns the core's ``CodeChannelsClient`` (``None`` before Slack connects).
        self._client = client
        #: The bar's channel actions and dashboard link (``None``: repo/branch/PR only).
        self._extras = extras
        self._create_folder = create_folder
        self._watch: asyncio.Future[None] | None = None
        #: Per channel: ``slot key -> (turn running, turn generation)`` at the last
        #: watch pass.
        self._seen: dict[str, dict[str, tuple[bool, int]]] = {}
        #: Per channel: the status the watcher last set for its workers.
        self._working: dict[str, str] = {}
        #: Per channel: until when (monotonic) the watcher leaves that status alone,
        #: because its board was dropped only while the sharing reading was re-read.
        self._status_hold: dict[str, float] = {}
        #: Per channel: the conductor slot the watch last followed (cleared on forget).
        self._conductor_of: dict[str, str] = {}
        #: Per channel: status hand-backs Slack refused in a row (see _status_gave_up).
        self._status_failures: dict[str, int] = {}
        #: Per channel: the conductor slot's turn generation at the last watch pass.
        self._conductor_gen: dict[str, int] = {}
        #: Per channel: the keys of the workspace's own bar items last built.
        self._bar_base_keys: dict[str, list[str]] = {}
        self._crowded: set[str] = set()
        self._store = store or WorkspaceStore()
        self._set_project = set_project
        self._project_of = project_of
        self._find_pr = find_pr
        self._find_pr_number = find_pr_number
        self._read_board = read_board
        self._publish_board = publish_board
        self._clock = clock or time.monotonic
        self._locks: dict[str, asyncio.Lock] = {}
        self._tasks: dict[str, asyncio.Future[None]] = {}
        self._binds: dict[str, asyncio.Future[WorkspaceRecord | None]] = {}
        self._again: set[str] = set()
        self._again_delay: dict[str, float] = {}
        #: The newest binding a trigger handed :meth:`schedule_refresh`, per channel:
        #: each pass refreshes it, not the first trigger's (a rebind meanwhile).
        self._latest_binding: dict[str, Any] = {}
        #: Channels with a conductor-turn refresh pending (as opposed to board-only).
        self._full: set[str] = set()
        #: When each channel's own PR was last read (monotonic), for :data:`_PR_TTL_LIVE`.
        self._channel_pr_read: dict[str, float] = {}
        #: The last board each channel published, PR-filled (the bar and the diff
        #: read it between refreshes).
        self._boards: dict[str, BoardSnapshot] = {}
        #: The conductor session each cached board was read for (see _cached_board).
        self._board_owner: dict[str, str] = {}
        #: When each channel's board was last read and published (monotonic).
        self._board_refreshed: dict[str, float] = {}
        #: ``worker project folder -> (read at, repository)`` (see _foreign_workers).
        self._project_repos: dict[str, tuple[float, RepoRef | None]] = {}
        #: The keys of the bar each channel last built (what the agent's tool reports).
        #: Channels whose bound session has a turn running (the bar's Stop).
        self._turning: set[str] = set()
        #: The bar state each channel last published from, so a turn start can
        #: add Stop without a git read.
        self._bar_states: dict[str, BarState] = {}
        #: The digest of the bar each channel's last successful send carried: what
        #: Slack shows, whichever publisher (refresh or turn start) sent it.
        self._sent_bar: dict[str, str] = {}
        #: One bar send per channel at a time (publishing.send_bar). Never dropped
        #: (as ``_locks``): a send still holding it would race a new lock's send.
        self._bar_send_locks: dict[str, asyncio.Lock] = {}
        #: ``(host, repo slug, branch, number) -> (read at, lookup)`` for worker PRs.
        self._worker_prs: dict[tuple[str, str, str, int], tuple[float, PrLookup]] = {}

    def _lock(self, key: str) -> asyncio.Lock:
        lock = self._locks.get(key)
        if lock is None:
            lock = self._locks[key] = asyncio.Lock()
        return lock

    # ── bind ────────────────────────────────────────────────────────────────

    async def ensure(self, binding: Any) -> WorkspaceRecord | None:
        """Make (or reuse) the channel's workspace and publish its context bar.

        The bind itself (worktree creation, which can take a while on a large
        repository) runs as ONE task per channel, shielded from the caller: a
        caller that times out or is cancelled leaves it running, and the next
        caller joins it instead of starting a second ``git worktree add``.
        """
        team, channel = str(binding.team_id or ""), str(binding.channel_id or "")
        if not channel or not _gate_open():
            return None
        key = f"{team}:{channel}"
        try:
            async with self._lock(key):
                if await asyncio.to_thread(_stamped_archived, channel):
                    # Every path to a workspace comes through here (a bind, a
                    # message, a turn's start or end, a resume): an archived
                    # channel gets no tree back, or the one its Archive removed
                    # would be cut again and orphaned (no cleanup runs after).
                    return None
                record = await asyncio.to_thread(self._store.get, team, channel)
                fresh = record is None or not await asyncio.to_thread(
                    self._record_matches, record, binding
                )
                task = self._binds.get(key) if fresh else None
                if fresh and task is None:
                    task = asyncio.ensure_future(self._bind_and_store(binding, key))
                    self._binds[key] = task
                    task.add_done_callback(self._bind_done(key))
            if task is not None:
                record = await asyncio.shield(task)
            if record is None:
                return None
            if (
                not fresh
                and record.worktree
                and record.pointed_session != str(getattr(binding, "session_key", "") or "")
            ):
                await self._point_once(binding, key)  # a replacement session
            # The bar is set at bind; turn ends keep it current. A turn start on
            # a known channel costs no git call (its Stop: on_turn_start).
            if (fresh or not record.bar_hash) and not await asyncio.to_thread(_archived, binding):
                async with self._lock(key):
                    current = await asyncio.to_thread(self._store.get, team, channel)
                    if current is None:
                        return None
                    before = (current.bar_hash, current.slack_off)
                    self._drop_board_unless_admitted(binding, key)
                    await self._publish_bar(current, binding)
                    if (current.bar_hash, current.slack_off) != before:
                        await asyncio.to_thread(self._store.put, current)
                    record = current
            return record
        except Exception:
            logger.warning("code channel %s: workspace setup failed", channel, exc_info=True)
            return None

    def _bind_done(self, key: str) -> Callable[[asyncio.Future[Any]], None]:
        def _done(_task: asyncio.Future[Any]) -> None:
            self._binds.pop(key, None)

        return _done

    async def _bind_and_store(self, binding: Any, key: str) -> WorkspaceRecord | None:
        """The one bind for a channel, stored under the channel's lock."""
        record = await self._bind(binding)
        async with self._lock(key):
            current = await asyncio.to_thread(self._store.get, record.team_id, record.channel_id)
            if not record.worktree and record.worktree_skipped not in _SETTLED_SKIPS:
                record.bind_attempts = (current.bind_attempts if current else 0) + 1
            if current is not None and record.worktree and current.worktree == record.worktree:
                # The same tree re-adopted (a repaired or re-pointed record): its
                # folder, the session already pointed and the open tab carry over.
                for name in _ADOPTED_FIELDS:
                    setattr(record, name, getattr(current, name))
            elif current is not None:
                # Another tree: its diff is published afresh (diff_hash stays
                # empty), but the tab it replaces or removes is the channel's.
                for name in _CHANNEL_FIELDS:
                    setattr(record, name, getattr(current, name))
            await asyncio.to_thread(self._store.put, record)
        # Point the session ONCE, from the bind that made the worktree: a later
        # turn start never moves it again, so a project the user picks afterwards
        # holds and no mid-conversation reset is ever armed by this package.
        if record.worktree:
            await self._point_once(binding, key)
        return record

    async def _point_once(self, binding: Any, key: str) -> None:
        """Point ``binding``'s session at the worktree unless that session already was.

        Keyed by session: the bind's own session is pointed once, and a
        replacement session that later takes over the channel is pointed once
        too, while a project either session's user picks afterwards holds.
        """
        session = str(getattr(binding, "session_key", "") or "")
        team, channel = str(binding.team_id or ""), str(binding.channel_id)
        record = await asyncio.to_thread(self._store.get, team, channel)
        if record is None or not record.worktree or not session:
            return
        if record.pointed_session == session:
            return
        if not await self._point_session(binding, record):
            return
        async with self._lock(key):
            current = await asyncio.to_thread(self._store.get, team, channel)
            if current is not None:
                current.pointed_session = session
                current.session_key = session
                await asyncio.to_thread(self._store.put, current)

    @staticmethod
    def _record_matches(record: WorkspaceRecord, binding: Any) -> bool:
        """Whether ``record`` still describes ``binding``'s project.

        A binding that reports the session's CURRENT project names the worktree
        (or a directory in it) once the session was pointed there; that is the
        same workspace, not a new project to cut another worktree from.
        """
        project = str(getattr(binding, "project_dir", None) or "")
        if record.worktree_skipped == REPAIRED:
            return False  # its stored base was malformed: rebind to re-derive it
        if record.worktree and not os.path.isdir(record.worktree):
            return False
        if (
            not record.worktree
            and record.worktree_skipped not in _SETTLED_SKIPS
            and record.bind_attempts < _MAX_BIND_ATTEMPTS
        ):
            return False  # a git error, not a property of the project: try again
        if record.project_dir == project:
            return True
        if not (record.worktree and project):
            return False
        tree = os.path.realpath(record.worktree)
        real = os.path.realpath(project)
        return real == tree or real.startswith(tree.rstrip(os.sep) + os.sep)

    async def _session_project(self, binding: Any) -> str:
        """The project the bound session was created with ("" when unknown).

        A binding with no project of its own still runs its session SOMEWHERE
        (the dashboard's default project, else the workspace directory); when
        that is a git repository, two channels would otherwise share its checkout.
        """
        if self._project_of is None:
            return ""
        key = str(getattr(binding, "session_key", "") or "")
        if not key:
            return ""
        try:
            return str(await self._project_of(key) or "")
        except Exception:
            logger.debug("code channel: session project unreadable", exc_info=True)
            return ""

    async def _bind(self, binding: Any) -> WorkspaceRecord:
        team, channel = str(binding.team_id or ""), str(binding.channel_id)
        raw_project = str(getattr(binding, "project_dir", None) or "")
        project = raw_project or await self._session_project(binding)
        record = WorkspaceRecord(
            team_id=team,
            channel_id=channel,
            session_key=str(getattr(binding, "session_key", "") or ""),
            project_dir=raw_project,
            resolved_project=project,
        )
        if not project:
            record.worktree_skipped = "no_project"
            return record
        title = str(getattr(binding, "title", "") or getattr(binding, "name", "") or "")
        branch = git_ops.channel_branch(title, team, channel)
        recovered: tuple[str, str] | None = None
        try:
            project_real = await asyncio.to_thread(os.path.realpath, project)
            if not await asyncio.to_thread(os.path.isdir, project_real):
                # The session's project is gone. When it was this channel's own
                # worktree (removed by hand, its branch kept), the checkout the
                # tree was cut from is the project, and the tree is cut again.
                recovered = await asyncio.to_thread(
                    _removed_channel_tree, channel, project_real, branch
                )
                if recovered is None:
                    record.worktree_skipped = "project_missing"
                    return record
                project_real = recovered[0]
            root = await asyncio.to_thread(git_ops.toplevel, project_real)
        except git_ops.SandboxUnavailable as exc:
            record.worktree_skipped = "sandbox_unavailable"
            _audit("code_channel_worktree", "denied", f"channel={channel}", str(exc)[:200])
            return record
        except (subprocess.TimeoutExpired, OSError) as exc:
            return _git_failed(record, exc, f"channel={channel} project={project}")
        if not root:
            record.worktree_skipped = "not_a_repo"
            return record
        rel = recovered[1] if recovered is not None else os.path.relpath(project_real, root)
        record.subdir = "" if rel == "." else rel
        # A record rebuilt while the session already runs in the channel's
        # worktree sees that worktree as the top level. The checkout it was cut
        # from is remembered on the binding (the gateway's own write, in the
        # fenced store), never inferred from git's layout, and is used only
        # when the gate confirms the top level as this channel's tree of it.
        top = root
        try:
            root = await asyncio.to_thread(_remembered_root, channel, root, branch, team)
        except git_ops.SandboxUnavailable as exc:
            record.worktree_skipped = "sandbox_unavailable"
            _audit("code_channel_worktree", "denied", f"channel={channel}", str(exc)[:200])
            return record
        except (subprocess.TimeoutExpired, OSError) as exc:
            return _git_failed(record, exc, f"channel={channel} project={project}")
        record.repo_root = root
        if await asyncio.to_thread(is_sensitive_path, root):
            record.worktree_skipped = "sensitive_path"
            _audit(
                "code_channel_worktree",
                "denied",
                f"channel={channel} root={root}",
                "sensitive path",
            )
            return record
        made: git_ops.CreatedWorktree | str
        try:
            if root != top:
                # The remembered root confirmed ``top`` as this channel's tree:
                # adopt it as it stands (its HEAD may be off the channel branch).
                made = await asyncio.to_thread(git_ops.adopt_confirmed_worktree, root, top, branch)
            else:
                made = await asyncio.to_thread(git_ops.create_or_adopt_worktree, root, branch)
        except git_ops.SandboxUnavailable as exc:
            record.worktree_skipped = "sandbox_unavailable"
            _audit(
                "code_channel_worktree", "denied", f"channel={channel} root={root}", str(exc)[:200]
            )
            return record
        except (subprocess.TimeoutExpired, OSError) as exc:
            return _git_failed(record, exc, f"channel={channel} root={root} branch={branch}")
        if isinstance(made, str):
            record.worktree_skipped = made
            _audit(
                "code_channel_worktree",
                "error",
                f"channel={channel} root={root} branch={branch}",
                made,
            )
            return record
        record.worktree, record.branch = made.path, made.branch
        record.base_sha, record.base_ref = made.base_sha, made.base_ref
        await asyncio.to_thread(_remember_root, channel, root, made.path, branch, team)
        _audit(
            "code_channel_worktree",
            "allowed",
            f"channel={channel} root={root} branch={branch} path={made.path} adopted={made.adopted}",
        )
        return record

    def _session_dir(self, record: WorkspaceRecord) -> str:
        if not record.worktree:
            return ""
        path = os.path.join(record.worktree, record.subdir) if record.subdir else record.worktree
        return path if os.path.isdir(path) else record.worktree

    async def _point_session(self, binding: Any, record: WorkspaceRecord) -> bool:
        """Point the bound session at the worktree; True when it now runs there."""
        target = await asyncio.to_thread(self._session_dir, record)
        if not target or self._set_project is None:
            return False
        key = str(getattr(binding, "session_key", "") or record.session_key)
        if not key:
            return False
        applied = await self._set_project(key, target)
        if not applied:
            logger.info("code channel %s: session %s kept its project", record.channel_id, key)
        return applied

    # ── the channel's folder and the sessions working in it ─────────────────

    async def plan_session(
        self,
        state: Any,
        *,
        channel_id: str,
        team_id: str,
        title: str,
        channel_name: str,
        project_dir: str,
        parent_folder_id: str,
    ) -> tuple[str, str] | None:
        """Before the channel's session exists: its worktree, and a folder for it.

        Returns ``(folder_id, session_project)`` — the channel's own folder,
        nested under ``parent_folder_id`` with the worktree as its project, and
        the directory the session should start in — or ``None`` when the
        channel gets no worktree (no git project, gate shut, a refusal). The
        session created in that folder starts in the worktree, and so does every
        session later filed under it, a conductor's workers included.
        """
        pending = SimpleNamespace(
            team_id=team_id,
            channel_id=channel_id,
            session_key="",
            project_dir=project_dir,
            title=title,
        )
        record = await self.ensure(pending)
        if record is None or not record.worktree:
            return None
        session_dir = await asyncio.to_thread(self._session_dir, record)
        name = folders.folder_name(channel_name, title, channel_id)
        folder_id = await self._create_folder(state, parent_folder_id, name, session_dir)
        if not folder_id:
            return None
        key = f"{team_id}:{channel_id}"
        async with self._lock(key):
            current = await asyncio.to_thread(self._store.get, team_id, channel_id)
            if current is not None:
                current.folder_id = folder_id
                await asyncio.to_thread(self._store.put, current)
        return folder_id, session_dir

    def start_watch(self, state_getter: Callable[[], Any]) -> None:
        """Watch the sessions filed in every channel's folder (idempotent).

        A conductor's workers run in their own sessions, so the bound session's
        turn hooks never see their turns end. Every :data:`_WATCH_SECS` the
        watcher reads, in memory, which sessions are filed under each channel's
        folder and whether a turn is running in them; a turn that ended
        schedules the same refresh a bound turn does, a turn that started does
        too (debounced) for a channel with a work board, and while only workers
        are running the channel shows Slack's working status.
        """
        if self._watch is not None and not self._watch.done():
            return

        async def _loop() -> None:
            while True:
                await asyncio.sleep(_WATCH_SECS)
                try:
                    await self.watch_tick(state_getter())
                except Exception:
                    logger.warning("code channel folder watch failed", exc_info=True)

        # A fresh context: the loop outlives the turn that started it, so it must
        # not inherit that turn's write fence (the status driver would tie its
        # writes to a turn that has ended).
        self._watch = asyncio.get_running_loop().create_task(_loop(), context=contextvars.Context())

    async def watch_tick(self, state: Any) -> None:
        """One pass of :meth:`start_watch` over every bound channel.

        Two kinds of session are watched per channel. The sessions filed in its
        folder (the channel's worktree is theirs, so their turns change the diff).
        And the conductor's WORKERS, by lineage, wherever they are filed and
        whatever worktree they run in: the code-channel store's worker records
        for the bound session plus every session the channel's last board names
        (one level only: a worker's own sessions are in neither). A worker that
        started fresh from main in its own worktree is still on the board and in
        the channel's status.
        """
        if state is None:
            return
        if not _gate_open():
            # Code channels were switched off: nothing is watched any more, so
            # nothing the watch set may outlive it (status, sidebar aggregate).
            for key in sorted(self._known_channels()):
                await self._forget_channel(key, state)
            return
        bound = await asyncio.to_thread(_bindings_by_channel)
        live: set[str] = set()
        for record in await asyncio.to_thread(self._store.all):
            # The channel's stored binding, not a stand-in: the board's scope
            # check reads its archive stamp and anchor. An unbound or archived
            # channel is not watched (and is forgotten below).
            binding = bound.get(record.channel_id)
            if binding is None or binding.archived_at:
                continue
            if await asyncio.to_thread(_archived, record):
                continue  # Slack refuses every write there until it is unarchived
            if not _activation_admits(binding):
                # Off or Review: the watch writes nothing there. It is forgotten
                # below, which only hands back the one status the watch itself set.
                continue
            # A ``slack_off`` record (Slack refused its bar or Code tab) is still
            # watched: only those two writes check it, while its board keeps
            # publishing, so its workers' turns must still refresh it.
            if binding.session_key and binding.session_key != record.session_key:
                # The record learns a replacement session only when it points
                # a worktree at it; the binding names the current conductor.
                record = replace(record, session_key=binding.session_key)
            key = record.key
            live.add(key)
            if record.session_key:
                self._conductor_of[key] = record.session_key
            # The cached board decides the status and the workers watched: a
            # channel that turned shared or stopped since loses it here, within
            # one pass, not only at its next refresh, which is armed now so the
            # board comes back as soon as the scope admits it again.
            if self._drop_board_unless_admitted(binding, key):
                self.schedule_refresh(binding, delay=BOARD_DEBOUNCE_SECS)
            if record.worktree and not record.folder_id:
                # A record rebuilt after it was lost has no folder: find it again
                # (the base's recovery). A channel with none is still watched by
                # its workers' lineage below.
                await self._recover_folder(state, record)
            folder = (
                folders.sessions_in(state, folders.subtree_ids(state, record.folder_id))
                if record.folder_id and record.worktree
                else {}
            )
            workers = self._lineage_of(state, record)
            seen = {**workers, **folder}
            observed = key in self._seen
            before = self._seen.get(key, {})
            self._seen[key] = seen
            now = {k: run for k, (run, _gen) in seen.items()}
            ended = [k for k, entry in seen.items() if _turn_ended(before.get(k), entry, observed)]
            # A session that vanished since the last pass, running or idle: an
            # idle worker the owner closed must leave the board too (an idle
            # board has no backstop to notice it).
            ended += [k for k in before if k not in seen]
            started = [
                k for k, entry in seen.items() if _turn_started(before.get(k), entry, observed)
            ]
            channel = binding
            if ended:
                self.schedule_refresh(channel)
            elif started and (self._cached_board(key) is not None or workers):
                # A worker picked work up: the channel's work board shows it.
                self.schedule_refresh(channel, delay=BOARD_DEBOUNCE_SECS)
            elif not observed and await self._has_board(record):
                # The first pass since the gateway started: a board it posted
                # before the restart is read again, which re-arms the backstop.
                self.schedule_refresh(channel, delay=BOARD_DEBOUNCE_SECS)
            elif self._board_due(key, workers):
                self.schedule_refresh(channel, delay=BOARD_DEBOUNCE_SECS)
            running = sorted(k for k, (run, _gen) in folder.items() if run)
            if len(running) > 1 and key not in self._crowded:
                self._crowded.add(key)
                logger.warning(
                    "code channel %s: %d sessions running in one worktree at once (%s); "
                    "its workers should take turns",
                    record.channel_id,
                    len(running),
                    ", ".join(running),
                )
            elif len(running) <= 1:
                self._crowded.discard(key)
            # A folderless channel's watch holds only workers: the conductor's
            # own running turn still owns the status (it shows Slack's Stop).
            conductor = _find_slot(state, record.session_key)
            if record.session_key:
                now.setdefault(record.session_key, bool(getattr(conductor, "turn_running", False)))
            self._note_conductor_turns(key, conductor)
            if not self._board_still_admitted(binding):
                # The board's scope refuses this channel (shared, relinked ...):
                # the conductor's workers, known by lineage, drive no status
                # there. What the watch set for them is handed back by
                # _worker_status, never over the conductor's own running turn.
                # Lineage decides, not location: a worker the board filed into the
                # channel's folder is still the conductor's worker.
                now = {k: run for k, run in now.items() if k not in workers}
                # The cached board's waiting rows must not set ``suspended`` either
                # (the scope can refuse here after admitting it earlier this pass).
                self._forget_board(key, binding.channel_id)
                if not _sharing_unknown(binding.channel_id):
                    self._status_hold.pop(key, None)  # a confirmed refusal holds nothing
            await self._worker_status(record, now)
        # Archived, unbound or Slack-refused channels: whatever the watch set for
        # them (status, caches, timers, the sidebar aggregate) is let go.
        for key in sorted(self._known_channels() - live):
            await self._forget_channel(key, state)

    def _known_channels(self) -> set[str]:
        """Every channel the watch holds state for."""
        return (
            set(self._seen)
            | set(self._boards)
            | set(self._working)
            | set(self._status_hold)
            | set(self._conductor_of)
            | set(self._channel_pr_read)
            | set(self._conductor_gen)
        )

    async def _forget_channel(self, key: str, state: Any) -> None:
        """Let go of everything the watch holds for a channel it no longer watches.

        A status the watch set (``processing`` / ``suspended``) is handed back as
        ``active`` once, the conductor slot's ``work_aggregate`` is cleared, and
        the board package's per-channel triggers, timers and publish state go too.
        Never raises.
        """
        channel = key.split(":", 1)[1] if ":" in key else key
        for table in (
            self._seen,
            self._boards,
            self._board_owner,
            self._board_refreshed,
            self._status_hold,
            self._channel_pr_read,
            self._conductor_gen,
            self._bar_base_keys,
            self._bar_states,
            self._sent_bar,
        ):
            table.pop(key, None)
        self._crowded.discard(key)
        conductor = self._conductor_of.get(key, "")
        if key in self._working:
            slot = _find_slot(state, conductor) if conductor else None
            if bool(getattr(slot, "turn_running", False)):
                # The conductor's own live turn owns the status now: leave it.
                self._working.pop(key, None)
                self._status_failures.pop(key, None)
            elif await _set_status(channel, "active"):
                self._working.pop(key, None)
                self._status_failures.pop(key, None)
            elif self._status_gave_up(key):
                self._working.pop(key, None)
            # Otherwise kept (with its conductor): the next pass hands it back again.
        if key not in self._working:
            self._conductor_of.pop(key, None)
            self._status_failures.pop(key, None)
        try:
            from kiro_crew.slack.code_channels import board as board_pkg

            board_pkg.clear_aggregate(state, conductor)
            board_pkg.forget_channel(channel)
        except Exception:
            logger.debug("code channel %s: board state not forgotten", channel, exc_info=True)

    async def _recover_folder(self, state: Any, record: WorkspaceRecord) -> bool:
        """A record rebuilt after it was lost has no ``folder_id`` (only
        :meth:`plan_session` sets it): find the channel's folder again, the one
        whose project is the channel's tree, and store it. True when found."""
        session_dir = await asyncio.to_thread(self._session_dir, record)
        if not session_dir:
            return False
        snapshot = list(getattr(state, "_folders", None) or ())
        folder_id = await asyncio.to_thread(folders.folder_for_project, snapshot, session_dir)
        if not folder_id:
            return False
        async with self._lock(record.key):
            current = await asyncio.to_thread(self._store.get, record.team_id, record.channel_id)
            if current is None or current.worktree != record.worktree:
                return False
            if not current.folder_id:
                current.folder_id = folder_id
                try:
                    await asyncio.to_thread(self._store.put, current)
                except OSError:
                    logger.warning(
                        "code channel %s: folder not stored", record.channel_id, exc_info=True
                    )
                    return False
            record.folder_id = current.folder_id
        return True

    async def _worker_status(self, record: WorkspaceRecord, now: dict[str, bool]) -> None:
        """The channel's Slack status while only its workers are moving.

        The bound session's own turns drive the status (the core's hooks); between
        them it follows the workers: ``suspended`` while one waits on you (or the
        conductor's last turn left it waiting), ``processing`` while one runs, and
        back to what the conductor's turn end showed once none does.
        """
        from kiro_crew.slack.code_channels import status as channel_status
        from kiro_crew.slack.code_channels.board.model import WAITING_ON_YOU

        bound_running = now.get(record.session_key, False)
        key = record.key
        if bound_running:
            # The bound session's own turn drives the status (the core's hooks).
            self._working.pop(key, None)
            self._status_hold.pop(key, None)
            self._status_failures.pop(key, None)
            return
        hold = self._status_hold.get(key)
        if hold is not None:
            if self._cached_board(key) is None and self._clock() < hold:
                # The board was dropped only while the channel's sharing reading
                # is re-read: the status it set stays until the board is back
                # (or the hold runs out), instead of flapping off and on.
                return
            self._status_hold.pop(key, None)
        workers = any(run for k, run in now.items() if k != record.session_key)
        board = self._cached_board(key)
        waiting = board is not None and any(r.state == WAITING_ON_YOU for r in board.rows)
        conductor_waits = channel_status.after_turn(record.channel_id) == "suspended"
        if waiting or (conductor_waits and (workers or key in self._working)):
            want = "suspended"
        elif workers:
            want = "processing"
        else:
            want = ""
        sent = self._working.get(key, "")
        if want == "processing" and sent == want:
            # Slack ends ``processing`` after an hour and a code channel's driver
            # arms no keepalive, so a worker running longer would lose the loader.
            # The driver is asked again each pass; its own refresh window
            # (PROCESSING_REFRESH_SECS) decides when a write actually goes out.
            await _set_status(record.channel_id, want)
        elif want and want != sent:
            if await _set_status(record.channel_id, want):
                self._working[key] = want
        elif not want and sent:
            # Hand back what the conductor's turn end left (suspended or active).
            # Forgotten only once Slack took it: a failed hand-back is sent again
            # on the next pass, never left showing the workers' status.
            if await _set_status(record.channel_id, "suspended" if conductor_waits else "active"):
                self._working.pop(key, None)
                self._status_failures.pop(key, None)
            elif self._status_gave_up(key):
                self._working.pop(key, None)

    def _note_conductor_turns(self, key: str, conductor: Any) -> None:
        """Forget the status the watch set once a conductor turn ran since the last pass.

        The conductor's turn hooks set the channel status themselves (``processing``,
        then what its end leaves) without telling the watch. A turn that started
        and ended between two passes is seen only as a new turn generation; the
        watch's own record is then stale, so it is dropped and the workers' status
        (``suspended`` for one still waiting on you) is sent again.
        """
        generation = getattr(conductor, "_turn_generation", None) if conductor else None
        if not isinstance(generation, int):
            return
        before = self._conductor_gen.get(key)
        self._conductor_gen[key] = generation
        if before is not None and before != generation:
            self._working.pop(key, None)
            self._status_failures.pop(key, None)
            self._status_hold.pop(key, None)  # the status it held was overwritten too

    def _status_gave_up(self, key: str) -> bool:
        """Count one refused hand-back; True once :data:`_STATUS_RETRIES` were refused.

        Bounded, so a refusal Slack keeps giving (the app left the channel) is
        not re-sent every pass forever; the conductor's next turn sets it again.
        """
        count = self._status_failures.get(key, 0) + 1
        if count < _STATUS_RETRIES:
            self._status_failures[key] = count
            return False
        self._status_failures.pop(key, None)
        logger.info(
            "code channel %s: status hand-back refused %d times; left to the next turn",
            key,
            count,
        )
        return True

    # ── turn end ────────────────────────────────────────────────────────────

    def _forget_worker_status(self, binding: Any) -> None:
        """The bound session's turn drives the channel status itself, so the
        watcher's record of the status it wrote (``processing`` / ``suspended``),
        its refused hand-backs and any hold are stale after it."""
        key = record_key(binding.team_id or "", binding.channel_id or "")
        self._working.pop(key, None)
        self._status_failures.pop(key, None)
        self._status_hold.pop(key, None)

    @property
    def store(self) -> WorkspaceStore:
        """The service's record store (one lock for every writer of it)."""
        return self._store

    async def republish_bar(self, binding: Any) -> str | None:
        return await publishing.republish_bar(self, binding)

    def _drop_board_unless_admitted(self, binding: Any, key: str) -> bool:
        """Forget the cached board when the channel may no longer be shown it.

        The context bar lists the cached board's workers; a bar sent outside a
        refresh (which re-reads the board under the scope check) checks the
        same scope first, so a channel that turned shared or archived since is
        never re-sent worker titles or PR links from the cache. True when a
        cached board was dropped.
        """
        self._note_conductor(binding)
        if self._cached_board(key) is None:
            return False
        from kiro_crew.slack.code_channels import surface
        from kiro_crew.slack.code_channels.board import source

        if source.scope_admits(surface.dashboard_state(), binding):
            return False
        return self._forget_board(key, str(getattr(binding, "channel_id", "") or ""))

    def _write_fence(self, binding: Any, *, board: bool) -> Callable[[], bool] | None:
        return publishing.write_fence(self, binding, board=board)

    def _board_still_admitted(self, binding: Any) -> bool:
        """The scope check again, after a refresh's gh and git work (it takes seconds)."""
        from kiro_crew.slack.code_channels import surface
        from kiro_crew.slack.code_channels.board import source

        return source.scope_admits(surface.dashboard_state(), binding)

    def _forget_board(self, key: str, channel_id: str, *, hold: bool = True) -> bool:
        """Drop the channel's cached board; True when there was one.

        When the only doubt is a sharing reading gone stale (it is re-read on the
        refusal), the status the watch set for the board's workers is held for
        up to :data:`BOARD_BACKSTOP_SECS` rather than dropped and set again.
        ``hold=False`` (the conductor was replaced: not a sharing doubt) arms none.
        """
        had = self._boards.pop(key, None) is not None
        self._board_owner.pop(key, None)
        self._board_refreshed.pop(key, None)
        if hold and had and key in self._working and _sharing_unknown(channel_id):
            self._status_hold.setdefault(key, self._clock() + BOARD_BACKSTOP_SECS)
        return had

    @contextlib.asynccontextmanager
    async def channel_quiet(self, team_id: str, channel_id: str) -> AsyncIterator[None]:
        """Hold the channel's lock with no bind in flight (the archive's cleanup).

        A bind still cutting the worktree is waited for first (bounded), so the
        cleanup sees what it made; a refresh in flight finishes before the lock
        is granted.
        """
        key = f"{team_id}:{channel_id}"
        for _ in range(2):
            bind = self._binds.get(key)
            if bind is None:
                break
            await asyncio.wait({bind}, timeout=_QUIET_WAIT_SECS)
        async with self._lock(key):
            yield

    async def on_turn_start(self, binding: Any, _event: Any = None) -> None:
        """Turn-start hook: make sure the workspace exists before the model runs.

        The bar gains Stop while the turn runs, rebuilt from its last state (no
        git read); a fresh channel's first bar already carries it.
        """
        self._forget_worker_status(binding)
        key = f"{binding.team_id or ''}:{binding.channel_id or ''}"
        self._turning.add(key)
        await self.ensure(binding)
        try:
            await self._turn_bar(binding)
        except Exception:
            logger.debug("code channel %s: Stop not shown", binding.channel_id, exc_info=True)

    async def on_turn_end(self, binding: Any, _event: Any = None) -> None:
        """Turn-end hook: schedule :meth:`refresh` and return at once.

        A diff of a large tree and two ``gh`` reads can outlast the core's hook
        timeout, and the turn must not wait on Slack views. One refresh runs per
        channel at a time; a turn ending while one runs queues exactly one rerun,
        so the last turn's state is always what gets published (without Stop).
        A turn Slack's own Stop ended publishes nothing: Slack's lifecycle allows no
        ``agents.conversations.*`` call for the channel until a new turn starts,
        whose end publishes everything again.
        """
        from kiro_crew.slack.code_channels.status import is_quiet

        self._forget_worker_status(binding)
        self._turning.discard(f"{binding.team_id or ''}:{binding.channel_id or ''}")
        if is_quiet(str(binding.channel_id or "")):
            return
        self.schedule_refresh(binding)

    def schedule_refresh(self, binding: Any, *, delay: float = 0.0) -> None:
        """Run :meth:`refresh` for the channel soon; coalesces with one in flight.

        *delay* is a debounce: the first run waits that long, so the triggers
        that arrive together (several workers reporting at once) share it.
        """
        key = f"{binding.team_id or ''}:{binding.channel_id or ''}"
        self._latest_binding[key] = binding
        if delay <= 0:
            self._full.add(key)  # a turn end: re-read the channel's own PR
        if key in self._tasks:
            self._again.add(key)
            # A rerun keeps the longest debounce asked of it, so a burst of
            # starts, approvals and ledger moves mid-refresh costs one more pass.
            self._again_delay[key] = max(self._again_delay.get(key, 0.0), delay)
            return

        async def _run() -> None:
            try:
                wait = delay
                while True:
                    if wait > 0:
                        await asyncio.sleep(wait)
                    self._again.discard(key)
                    # This pass absorbs every trigger so far, their debounce too.
                    self._again_delay.pop(key, None)
                    full = key in self._full
                    self._full.discard(key)
                    # The channel's binding as the store holds it now, else the
                    # newest trigger's: the scope check refuses a stale one, so a
                    # rebind mid-loop (or a late trigger carrying the old copy)
                    # must not drop every rerun.
                    current = await _stored_binding(self._latest_binding.get(key, binding))
                    if full:
                        await self.refresh(current)
                    else:
                        await self.refresh(current, recheck_pr=False)
                    if key not in self._again:
                        return
                    wait = self._again_delay.pop(key, 0.0)
            finally:
                self._tasks.pop(key, None)
                self._again_delay.pop(key, None)
                self._latest_binding.pop(key, None)

        self._tasks[key] = asyncio.ensure_future(_run())

    async def drain(self) -> None:
        """Wait for every scheduled refresh (shutdown, tests)."""
        while self._tasks:
            await asyncio.gather(*list(self._tasks.values()), return_exceptions=True)

    def bar_base_keys(self, team_id: str, channel_id: str) -> list[str] | None:
        """The keys the workspace's own bar items took at the last bar build, or ``None``."""
        keys = self._bar_base_keys.get(f"{team_id}:{channel_id}")
        return list(keys) if keys is not None else None

    async def _board_for(self, binding: Any) -> BoardSnapshot | None:
        if self._read_board is None:
            return None
        try:
            return await self._read_board(binding)
        except Exception:
            logger.warning("code channel %s: board read failed", binding.channel_id, exc_info=True)
            return None

    async def refresh(self, binding: Any, *, recheck_pr: bool = True) -> None:
        """Republish the context bar, the diff and the work board, each only on change.

        A refresh only the board triggered (``recheck_pr=False``: a worker's turn
        start, a tool approval, a ledger write) re-reads the channel's own PR at
        most once per :data:`_PR_TTL_LIVE`, so a busy board does not cost two
        ``gh`` calls a report; any turn END in the channel (the conductor's or a
        worker's) always re-reads it.

        Callers go through :meth:`schedule_refresh`: it is what keeps refreshes
        of one channel serial, which the board's post-and-pin needs.
        """
        team, channel = str(binding.team_id or ""), str(binding.channel_id or "")
        if not channel or not _gate_open():
            return
        if await asyncio.to_thread(_archived, binding):
            # Slack refuses every write to an archived channel; an unarchive
            # (``lifecycle``) clears the stamp and the next turn publishes again.
            return
        if not _activation_admits(binding):
            # The operator set the channel or its origin to Off or Review: worker
            # activity refreshes nothing there (no bar, no diff tab, no board),
            # the same rule the message path and the agent's own writes obey.
            self._forget_board(f"{team}:{channel}", channel)
            self._status_hold.pop(f"{team}:{channel}", None)
            _clear_aggregate(binding)
            return
        from kiro_crew.slack.code_channels.status import is_quiet

        if is_quiet(channel):
            return  # Slack's own Stop: nothing is published until a new turn
        if await asyncio.to_thread(self._store.get, team, channel) is None:
            await self.ensure(binding)
        # The gh, git and Slack work -- the board's ledger read, the worker PR
        # lookups and the board message included -- runs on a snapshot WITHOUT
        # the channel lock, which a turn start's ensure() takes: a reply right
        # after a turn must not wait on the last turn's refresh. Refreshes of one
        # channel are already serial (schedule_refresh), and the store has its
        # own thread lock; the channel lock is held only to merge the result back.
        record = await asyncio.to_thread(self._store.get, team, channel)
        if record is None:
            return
        key = f"{team}:{channel}"
        self._note_conductor(binding)
        start = (record.bar_hash, record.diff_hash, record.view_id)
        board = read = await self._board_for(binding)
        if board is None:
            # Refused or gone: forget it now, so a failure below never leaves
            # the last admitted board cached for the bar to re-send.
            self._forget_board(key, channel)
        try:
            now = self._clock()
            last = self._channel_pr_read.get(key)
            if recheck_pr or last is None or now - last >= _PR_TTL_LIVE:
                await self._refresh_pr(record)
                self._channel_pr_read[key] = now
            if board is not None:
                board = await self._fill_worker_prs(record, board)
                if not self._board_still_admitted(binding):
                    # The channel turned shared (or its slot was relinked) during
                    # the gh work: neither the bar nor the board shows it.
                    self._forget_board(key, channel)
                    board = None
            if board is not None:
                self._boards[key] = board
                self._board_owner[key] = str(binding.session_key or "")
                self._board_refreshed[key] = self._clock()
                self._status_hold.pop(key, None)
            if board is not None and self._publish_board is not None:
                # First, so the bar below links the board message this pass
                # posted. The board's pointers live on the channel's surface
                # record, not this one, so nothing here merges back for it. Its
                # own failure never costs the bar, the diff or the resource. It
                # asks the scope again before each of its own writes.
                try:
                    await self._publish_board(binding, board)
                except Exception:
                    logger.warning("code channel %s: board publish failed", channel, exc_info=True)
                # The board publish takes seconds: the bar checks the scope again.
                self._drop_board_unless_admitted(binding, key)
            await self._publish_bar(record, binding)
            await self._publish_diff(record, binding)
            if board is not None and key not in self._boards:
                # The diff's re-check refused the channel after the bar went out
                # with the Workers item on it: the bar is sent again without it.
                await self._publish_bar(record, binding)
            if read is not None and key not in self._boards:
                # The board read was admitted but a later re-check refused it: the
                # sidebar summary (published with the board) is cleared instead.
                _clear_aggregate(binding)
            if _activation_admits(binding):
                # Asked again: the bar and diff work above takes seconds.
                await self._publish_resource(record, binding)
        except Exception:
            logger.warning("code channel %s: refresh failed", channel, exc_info=True)
            if read is not None:
                # The board this pass read was never published: the sidebar
                # summary of the pass before is not left standing for it.
                _clear_aggregate(binding)
        async with self._lock(key):
            current = await asyncio.to_thread(self._store.get, team, channel)
            if current is None:
                return
            if (current.worktree, current.branch) != (record.worktree, record.branch):
                # Rebound meanwhile: what this pass sent described the old tree,
                # so the new tree's bar and diff are sent again, and a tab this
                # pass opened stays removable. The PR read was the old branch's,
                # so the rerun reads the new one's whatever its trigger.
                current.bar_hash, current.diff_hash = "", ""
                # The tab is the channel's, one per channel: when this pass
                # opened or removed it and nothing has touched it since, this
                # pass's id is what Slack shows; otherwise the stored one is.
                if record.view_id != start[2] and current.view_id == start[2]:
                    current.view_id = record.view_id
                self._channel_pr_read.pop(key, None)
                self._sent_bar.pop(key, None)
                raced = True
            else:
                # Someone else published while this pass ran (a turn start's
                # first bar): Slack shows whichever landed last, so the stored
                # digest is not trusted and the bar is sent again.
                raced = (current.bar_hash, current.diff_hash, current.view_id) != start
                for name in _REFRESH_FIELDS:
                    setattr(current, name, getattr(record, name))
                # A turn start may have sent the bar while this pass ran: the
                # stored digest is the last send that landed, not this pass's.
                current.bar_hash = self._sent_bar.get(key, current.bar_hash)
                if raced:
                    current.bar_hash = ""
                    self._sent_bar.pop(key, None)
            current.slack_off = current.slack_off or record.slack_off
            try:
                await asyncio.to_thread(self._store.put, current)
            except OSError:
                # An unreadable store abandons the write (never rewritten
                # empty); the next refresh tries again.
                logger.warning("code channel %s: refresh not stored", channel, exc_info=True)
                return
        if raced:
            self._again.add(key)  # schedule_refresh's loop runs one more pass

    def _checkout_dir(self, record: WorkspaceRecord) -> str:
        """The tree the session works in: the worktree, else the original repo."""
        return record.worktree or record.repo_root

    async def _refresh_pr(self, record: WorkspaceRecord) -> None:
        return await publishing.refresh_pr(self, record)

    async def _bar_state(self, record: WorkspaceRecord) -> BarState:
        return await publishing.bar_state(self, record)

    async def _bar_extras(self, record: WorkspaceRecord, *, has_pr: bool) -> dict[str, Any]:
        return await publishing.bar_extras(self, record, has_pr=has_pr)

    async def _publish_bar(self, record: WorkspaceRecord, binding: Any = None) -> bool:
        return await publishing.publish_bar(self, record, binding)

    async def _send_bar(
        self, record: WorkspaceRecord, state: BarState, binding: Any = None
    ) -> bool:
        return await publishing.send_bar(self, record, state, binding)

    async def _turn_bar(self, binding: Any) -> None:
        await publishing.turn_bar(self, binding)

    async def _publish_resource(self, record: WorkspaceRecord, binding: Any = None) -> None:
        await publishing.publish_resource(self, record, binding)

    async def _publish_diff(self, record: WorkspaceRecord, binding: Any = None) -> None:
        await publishing.publish_diff(self, record, binding)

    async def _clear_diff(self, record: WorkspaceRecord) -> None:
        await publishing.clear_diff(self, record)

    def _build_diff(
        self, record: WorkspaceRecord, board: BoardSnapshot | None = None
    ) -> tuple[str, str, str] | None:
        """Blocking: the worktree's diff against its base, capped (see :mod:`.diff`).

        With a work board whose workers have commits on their own branches, the
        tab shows the COMBINED change: the channel's own diff first, then each
        worker's committed changes under a header naming the worker, its branch
        and its PR (:func:`_worker_groups`).
        """
        cwd = record.worktree
        if not os.path.isdir(cwd):
            return None
        try:
            refusal = git_ops.checkout_filter(cwd)
            if refusal:
                logger.warning("code channel %s: diff skipped, %s", record.channel_id, refusal)
                return None
            if not _trusted_worktree(record):
                return None
            base = _fork_point(cwd, record)
            tracked = git_ops.tracked_diff(cwd, base, cap=_SCAN_BYTES)
            if tracked is None:
                return None
            body, body_cut = screen_tracked(
                tracked[0],
                truncated=tracked[1],
                classify=_classify_version,
                mask=redact_via_context,
                sensitive=lambda rel: is_sensitive_path(os.path.join(cwd, rel)),
            )
            listing = git_ops.untracked_files(cwd, cap=_UNTRACKED_LIST_CAP)
            names, names_cut = listing if listing is not None else ([], False)
            chunks: list[bytes] = []
            remaining = MAX_VIEW_BYTES - len(body)
            for rel in names[:_MAX_UNTRACKED]:
                if remaining <= 0:
                    names_cut = True
                    break
                if is_sensitive_path(os.path.join(cwd, rel)):
                    names_cut = True  # never read a credential-shaped path
                    continue
                section = render_untracked(cwd, rel, budget=remaining)
                if section is None:
                    continue
                if section == OMITTED:
                    names_cut = True  # a new file the tab cannot show: say so
                    continue
                # The same whole-file screen as tracked files: a rendered new
                # file is one hunk holding the whole file, and line-level
                # redaction of its "+"-prefixed lines misses a key's body.
                section, withheld = screen_tracked(
                    section,
                    truncated=False,
                    classify=_classify_version,
                    mask=redact_via_context,
                    sensitive=lambda rel: is_sensitive_path(os.path.join(cwd, rel)),
                )
                if withheld or not section:
                    names_cut = True
                    continue
                chunks.append(section)
                remaining -= len(section)
            names_cut = names_cut or len(names) > _MAX_UNTRACKED
            branch = git_ops.current_branch(cwd) or record.branch
            workers = _worker_groups(cwd, board, branch) if board is not None else []
        except git_ops.SandboxUnavailable:
            return None
        # Every byte bound for Slack goes through the gateway's outbound
        # redaction (credentials + exfiltration URLs) BEFORE it is sized, cut
        # and hashed: a key the agent wrote into a file must never reach the
        # channel, and the size cap must hold for what is actually sent.
        if workers:
            channel_group = DiffGroup(
                header=redact_via_context(f"Channel branch {branch} (conductor)"),
                tracked=_redact_bytes(body),
                tracked_truncated=body_cut,
                untracked=tuple(_redact_bytes(chunk) for chunk in chunks),
                untracked_truncated=names_cut,
            )
            built = assemble_groups([channel_group, *workers])
        else:
            built = assemble(
                _redact_bytes(body),
                tracked_truncated=body_cut,
                untracked=[_redact_bytes(chunk) for chunk in chunks],
                untracked_truncated=names_cut,
            )
        base_label = (git_ops.default_branch_name(cwd) if record.base_ref else "") or base[:12]
        return built.content, redact_via_context(base_label), redact_via_context(branch)

    async def _call(
        self,
        method: str,
        record: WorkspaceRecord,
        *args: Any,
        room_for: str = "",
        fence: Callable[[], bool] | None = None,
        **kwargs: Any,
    ) -> Any:
        return await publishing.call(
            self, method, record, *args, room_for=room_for, fence=fence, **kwargs
        )


def register(registry: Any, service: WorkspaceService) -> None:
    """Attach the workspace to the core's turn hooks (the only entry point).

    The hooks fire only while the core's gate is open; the service checks the
    same gate itself (``binding.code_channels_on``).
    """
    registry.on_turn_start(service.on_turn_start)
    registry.on_turn_end(service.on_turn_end)
