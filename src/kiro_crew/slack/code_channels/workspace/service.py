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
import hashlib
import json
import logging
import os
import subprocess
from types import SimpleNamespace
from typing import Any, AsyncIterator, Awaitable, Callable

from kiro_crew.platform.context import redact_via_context
from kiro_crew.security import is_sensitive_path
from kiro_crew.sel import sel
from kiro_crew.slack.code_channels.models import CodeChannelApiError, CodeChannelError
from kiro_crew.slack.code_channels.workspace import folders, git_ops
from kiro_crew.slack.code_channels.workspace.context_bar import BarState, build_items
from kiro_crew.slack.code_channels.workspace.diff import (
    CLEAN,
    MASK_LINES,
    MAX_VIEW_BYTES,
    OMITTED,
    WITHHOLD,
    assemble,
    render_untracked,
    screen_tracked,
)
from kiro_crew.slack.code_channels.workspace.pr import PrLookup, PullRequest, find_pull_request
from kiro_crew.slack.code_channels.workspace.remote import parse_remote
from kiro_crew.slack.code_channels.workspace.store import (
    REPAIRED,
    WorkspaceRecord,
    WorkspaceStore,
)

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

#: Slack answers that mean "this channel cannot take views or properties": the
#: workspace stops publishing for the channel instead of retrying every turn.
#: The install-wide answers (``feature_disabled`` / ``missing_scope`` /
#: ``not_allowed_token_type``) are not here: the core's client hands every
#: refusal to the feature's process latch, which a reconnect re-probe lifts, and
#: persisting them per channel would keep a channel dark after the scope is
#: granted.
_CHANNEL_DISABLING_ERRORS = frozenset(
    {
        "not_agent_channel",
        "not_authorized",
        "channel_not_found",
        "is_archived",
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
    "bar_hash",
    "diff_hash",
    "view_id",
)
#: What a rebind that re-adopts the same worktree keeps from the stored record.
_ADOPTED_FIELDS = ("folder_id", "pointed_session", "view_id", "diff_hash")
#: Longest the archive's cleanup waits for a bind still cutting the worktree.
_QUIET_WAIT_SECS = 120.0

#: Points a dashboard session at a directory; returns whether it was applied.
ProjectSetter = Callable[[str, str], Awaitable[bool]]
#: Reads the project a dashboard session currently has ("" when unknown).
ProjectReader = Callable[[str], Awaitable[str]]
PrFinder = Callable[..., Awaitable[PrLookup]]


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


def _digest(*parts: object) -> str:
    return hashlib.sha256(json.dumps(parts, sort_keys=True, default=str).encode()).hexdigest()


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


def _gate_open() -> bool:
    """The core's one gate: toggle on AND the install can run code channels."""
    from kiro_crew.slack.code_channels.binding import code_channels_on, live_config

    try:
        return code_channels_on(live_config())
    except Exception:
        logger.debug("code-channel gate failed; treating as off", exc_info=True)
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


class WorkspaceService:
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
    ) -> None:
        #: Returns the core's ``CodeChannelsClient`` (``None`` before Slack connects).
        self._client = client
        self._create_folder = create_folder
        self._watch: asyncio.Future[None] | None = None
        #: Per channel: ``slot key -> (turn running, turn generation)`` at the last
        #: watch pass.
        self._seen: dict[str, dict[str, tuple[bool, int]]] = {}
        self._working: set[str] = set()
        self._crowded: set[str] = set()
        self._store = store or WorkspaceStore()
        self._set_project = set_project
        self._project_of = project_of
        self._find_pr = find_pr
        self._locks: dict[str, asyncio.Lock] = {}
        self._tasks: dict[str, asyncio.Future[None]] = {}
        self._binds: dict[str, asyncio.Future[WorkspaceRecord | None]] = {}
        self._again: set[str] = set()

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
            # a known channel costs no git or Slack call.
            if fresh or not record.bar_hash:
                async with self._lock(key):
                    current = await asyncio.to_thread(self._store.get, team, channel)
                    if current is None:
                        return None
                    before = (current.bar_hash, current.slack_off)
                    await self._publish_bar(current)
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
        try:
            project_real = await asyncio.to_thread(os.path.realpath, project)
            if not await asyncio.to_thread(os.path.isdir, project_real):
                record.worktree_skipped = "project_missing"
                return record
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
        record.repo_root = root
        rel = os.path.relpath(project_real, root)
        record.subdir = "" if rel == "." else rel
        if await asyncio.to_thread(is_sensitive_path, root):
            record.worktree_skipped = "sensitive_path"
            _audit(
                "code_channel_worktree",
                "denied",
                f"channel={channel} root={root}",
                "sensitive path",
            )
            return record
        title = str(getattr(binding, "title", "") or getattr(binding, "name", "") or "")
        branch = git_ops.channel_branch(title, team, channel)
        try:
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
        schedules the same refresh a bound turn does, and while only workers
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

        self._watch = asyncio.ensure_future(_loop())

    async def watch_tick(self, state: Any) -> None:
        """One pass of :meth:`start_watch` over every channel with a folder."""
        if state is None or not _gate_open():
            return
        for record in await asyncio.to_thread(self._store.all):
            if not record.folder_id or not record.worktree or record.slack_off:
                continue
            key = record.key
            seen = folders.sessions_in(state, folders.subtree_ids(state, record.folder_id))
            observed = key in self._seen
            before = self._seen.get(key, {})
            self._seen[key] = seen
            now = {k: run for k, (run, _gen) in seen.items()}
            ended = [k for k, entry in seen.items() if _turn_ended(before.get(k), entry, observed)]
            ended += [k for k, (run, _gen) in before.items() if run and k not in seen]
            if ended:
                self.schedule_refresh(
                    SimpleNamespace(
                        team_id=record.team_id,
                        channel_id=record.channel_id,
                        session_key=record.session_key,
                        project_dir=record.project_dir,
                    )
                )
            running = sorted(k for k, run in now.items() if run)
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
            await self._worker_status(record, now)

    async def _worker_status(self, record: WorkspaceRecord, now: dict[str, bool]) -> None:
        """Slack's working status while only the channel's workers are running.

        The bound session's own turns already drive the status (the core's
        hooks); this covers the rest, and hands the status back the moment the
        bound session runs again or nothing runs.
        """
        bound_running = now.get(record.session_key, False)
        workers = any(run for k, run in now.items() if k != record.session_key)
        key = record.key
        want = "processing" if workers and not bound_running else ""
        if want and key not in self._working:
            if await _set_status(record.channel_id, "processing"):
                self._working.add(key)
        elif not want and key in self._working:
            self._working.discard(key)
            if not bound_running:
                await _set_status(record.channel_id, "active")

    # ── turn end ────────────────────────────────────────────────────────────

    @property
    def store(self) -> WorkspaceStore:
        """The service's record store (one lock for every writer of it)."""
        return self._store

    async def republish_bar(self, binding: Any) -> str | None:
        """Publish the context bar now (the agent changed its own items).

        Returns ``""`` when the bar is current, a Slack error code (or
        ``publish_failed``) when it is not, and ``None`` when there is no
        workspace record to publish from (the caller publishes the agent's items).
        """
        team, channel = str(binding.team_id or ""), str(binding.channel_id or "")
        if not channel or not _gate_open():
            return None
        async with self._lock(f"{team}:{channel}"):
            record = await asyncio.to_thread(self._store.get, team, channel)
            if record is None:
                return None
            published = await self._publish_bar(record)
            await asyncio.to_thread(self._store.put, record)
            return "" if published else (record.slack_off or "publish_failed")

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
        """Turn-start hook: make sure the workspace exists before the model runs."""
        await self.ensure(binding)

    async def on_turn_end(self, binding: Any, _event: Any = None) -> None:
        """Turn-end hook: schedule :meth:`refresh` and return at once.

        A diff of a large tree and two ``gh`` reads can outlast the core's hook
        timeout, and the turn must not wait on Slack views. One refresh runs per
        channel at a time; a turn ending while one runs queues exactly one rerun,
        so the last turn's state is always what gets published.
        """
        self.schedule_refresh(binding)

    def schedule_refresh(self, binding: Any) -> None:
        key = f"{binding.team_id or ''}:{binding.channel_id or ''}"
        if key in self._tasks:
            self._again.add(key)
            return

        async def _run() -> None:
            try:
                while True:
                    self._again.discard(key)
                    await self.refresh(binding)
                    if key not in self._again:
                        return
            finally:
                self._tasks.pop(key, None)

        self._tasks[key] = asyncio.ensure_future(_run())

    async def drain(self) -> None:
        """Wait for every scheduled refresh (shutdown, tests)."""
        while self._tasks:
            await asyncio.gather(*list(self._tasks.values()), return_exceptions=True)

    async def refresh(self, binding: Any) -> None:
        """Republish the context bar and the diff after a turn, each only on change."""
        team, channel = str(binding.team_id or ""), str(binding.channel_id or "")
        if not channel or not _gate_open():
            return
        if await asyncio.to_thread(self._store.get, team, channel) is None:
            await self.ensure(binding)
        # The gh, git and Slack work runs on a snapshot WITHOUT the channel lock,
        # which a turn start's ensure() takes: a reply right after a turn must not
        # wait on the last turn's refresh. Refreshes of one channel are already
        # serial (schedule_refresh), and the store has its own thread lock; the
        # channel lock is held only to merge the result back.
        record = await asyncio.to_thread(self._store.get, team, channel)
        if record is None:
            return
        start = (record.bar_hash, record.diff_hash, record.view_id)
        try:
            await self._refresh_pr(record)
            await self._publish_bar(record)
            await self._publish_diff(record)
        except Exception:
            logger.warning("code channel %s: refresh failed", channel, exc_info=True)
        key = f"{team}:{channel}"
        async with self._lock(key):
            current = await asyncio.to_thread(self._store.get, team, channel)
            if current is None:
                return
            if (current.worktree, current.branch) != (record.worktree, record.branch):
                # Rebound meanwhile: what this pass sent described the old tree,
                # so the new tree's bar and diff are sent again, and a tab this
                # pass opened stays removable.
                current.bar_hash, current.diff_hash = "", ""
                current.view_id = current.view_id or record.view_id
                raced = True
            else:
                # Someone else published while this pass ran (a turn start's
                # first bar): Slack shows whichever landed last, so the stored
                # digest is not trusted and the bar is sent again.
                raced = (current.bar_hash, current.diff_hash, current.view_id) != start
                for name in _REFRESH_FIELDS:
                    setattr(current, name, getattr(record, name))
                if raced:
                    current.bar_hash = ""
            current.slack_off = current.slack_off or record.slack_off
            await asyncio.to_thread(self._store.put, current)
        if raced:
            self._again.add(key)  # schedule_refresh's loop runs one more pass

    def _checkout_dir(self, record: WorkspaceRecord) -> str:
        """The tree the session works in: the worktree, else the original repo."""
        return record.worktree or record.repo_root

    async def _refresh_pr(self, record: WorkspaceRecord) -> None:
        cwd = self._checkout_dir(record)
        if not cwd:
            return
        branch = await asyncio.to_thread(git_ops.current_branch, cwd)
        repo = parse_remote(await asyncio.to_thread(git_ops.remote_url, cwd))
        if not branch or repo is None:
            return
        lookup = await self._find_pr(repo, branch)
        if lookup.status == "found" and lookup.pr is not None:
            pr = lookup.pr
            record.pr_number, record.pr_url = pr.number, pr.url
            record.pr_state, record.pr_ci = pr.state, pr.ci
        elif lookup.status == "none":
            record.pr_number, record.pr_url, record.pr_state, record.pr_ci = 0, "", "", ""
        else:
            # gh missing or signed out: keep the PR last seen, drop its CI (stale).
            record.pr_ci = ""

    async def _bar_state(self, record: WorkspaceRecord) -> BarState:
        cwd = self._checkout_dir(record)
        if not cwd:
            return BarState(folder=record.project_dir)
        branch = await asyncio.to_thread(git_ops.current_branch, cwd)
        if not branch:
            head = await asyncio.to_thread(git_ops.short_head, cwd)
            branch = f"detached at {head}" if head else ""
        repo = parse_remote(await asyncio.to_thread(git_ops.remote_url, cwd))
        pushed = bool(repo) and await asyncio.to_thread(git_ops.has_remote_branch, cwd, branch)
        pr = (
            PullRequest(record.pr_number, record.pr_url, record.pr_state, record.pr_ci)
            if record.pr_number
            else None
        )
        return BarState(
            repo=repo,
            repo_name=os.path.basename(record.repo_root.rstrip(os.sep)),
            branch=branch,
            branch_pushed=pushed,
            pr=pr,
        )

    async def _publish_bar(self, record: WorkspaceRecord) -> bool:
        """Publish the bar when it changed; False when a needed write failed."""
        if record.slack_off:
            return False
        try:
            state = await self._bar_state(record)
        except git_ops.SandboxUnavailable:
            state = BarState(folder=record.project_dir)
        from kiro_crew.slack.code_channels.agent_bar import merge_for_channel

        # The agent's own items (code_channel_set_context_bar) ride after these.
        items = await asyncio.to_thread(merge_for_channel, build_items(state), record.channel_id)
        digest = _digest(items)
        if digest == record.bar_hash or (not items and not record.bar_hash):
            return True
        if await self._call("set_context_bar", record, items) is not None:
            record.bar_hash = digest
            return True
        return False

    async def _publish_diff(self, record: WorkspaceRecord) -> None:
        if record.slack_off or not record.worktree or not record.base_sha:
            return
        built = await asyncio.to_thread(self._build_diff, record)
        if built is None:
            return
        content, base_label, head_label = built
        if not content:
            await self._clear_diff(record)
            return
        digest = _digest(content, base_label, head_label)
        if digest == record.diff_hash:
            return
        view_id = await self._call(
            "set_diff_view", record, content, base_branch=base_label, head_branch=head_label
        )
        if view_id is not None:
            record.diff_hash = digest
            if isinstance(view_id, str) and view_id:
                record.view_id = view_id

    async def _clear_diff(self, record: WorkspaceRecord) -> None:
        """No changes any more (reverted, or merged into the base): drop the diff tab.

        ``setView`` refuses an empty diff, so a tab that was published is removed
        instead, when its view id is known. Never published at all, there is
        simply no tab yet.
        """
        if not record.diff_hash or not record.view_id:
            return
        if await self._call("remove_view", record, record.view_id) is not None:
            record.diff_hash, record.view_id = "", ""

    def _build_diff(self, record: WorkspaceRecord) -> tuple[str, str, str] | None:
        """Blocking: the worktree's diff against its base, capped (see :mod:`.diff`)."""
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
            base = record.base_sha
            if record.base_ref:
                moved = git_ops.resolve_commit(cwd, record.base_ref)
                base = (git_ops.merge_base(cwd, "HEAD", moved) if moved else "") or base
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
        except git_ops.SandboxUnavailable:
            return None
        # Every byte bound for Slack goes through the gateway's outbound
        # redaction (credentials + exfiltration URLs) BEFORE it is sized, cut
        # and hashed: a key the agent wrote into a file must never reach the
        # channel, and the size cap must hold for what is actually sent.
        built = assemble(
            _redact_bytes(body),
            tracked_truncated=body_cut,
            untracked=[_redact_bytes(chunk) for chunk in chunks],
            untracked_truncated=names_cut,
        )
        base_label = (git_ops.default_branch_name(cwd) if record.base_ref else "") or base[:12]
        return built.content, redact_via_context(base_label), redact_via_context(branch)

    async def _call(self, method: str, record: WorkspaceRecord, *args: Any, **kwargs: Any) -> Any:
        """One write through the core's client. Returns its answer, or None when it failed.

        The core's client maps Slack's answer to :class:`CodeChannelApiError` and
        hands every refusal to the feature's process latch itself. Here a
        channel-specific refusal (:data:`_CHANNEL_DISABLING_ERRORS`) turns
        publishing off for this channel and is SEL-recorded; anything else is
        logged and retried on the next turn, because no digest is advanced.
        """
        client = self._client()
        if client is None:
            return None  # Slack not connected yet: the next turn retries
        try:
            answer = await getattr(client, method)(record.channel_id, *args, **kwargs)
        except CodeChannelApiError as exc:
            code = exc.code
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
        else:
            logger.warning(
                "code channel %s: %s failed: %s", record.channel_id, method, code or "error"
            )
        return None


def register(registry: Any, service: WorkspaceService) -> None:
    """Attach the workspace to the core's turn hooks (the only entry point).

    The hooks fire only while the core's gate is open; the service checks the
    same gate itself (``binding.code_channels_on``).
    """
    registry.on_turn_start(service.on_turn_start)
    registry.on_turn_end(service.on_turn_end)
