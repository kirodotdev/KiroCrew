"""The work board's per-worker PR and CI, read from the channel's own repository.

A mixin of :class:`~.service.WorkspaceService`, kept in its own module so the
refresh owner stays under the file cap. Its state (``_worker_prs``,
``_project_repos``), its clock and its forge readers are the service's own, set
in the service's ``__init__``; nothing here holds state of its own.
"""

from __future__ import annotations

import asyncio
import logging
from dataclasses import replace
from typing import TYPE_CHECKING, Any, Callable

from kiro_crew.slack.code_channels.workspace import git_ops
from kiro_crew.slack.code_channels.workspace.pr import PrLookup
from kiro_crew.slack.code_channels.workspace.remote import RepoRef, parse_remote
from kiro_crew.slack.code_channels.workspace.store import WorkspaceRecord, WorkspaceStore

if TYPE_CHECKING:
    from kiro_crew.slack.code_channels.board.model import BoardSnapshot
    from kiro_crew.slack.code_channels.workspace.service import PrFinder

logger = logging.getLogger(__name__)


class WorkerPrs:
    """Fill each shown worker row with its PR and CI (see :meth:`_fill_worker_prs`)."""

    if TYPE_CHECKING:
        _store: WorkspaceStore
        _clock: Callable[[], float]
        _find_pr: PrFinder
        _find_pr_number: PrFinder
        _project_repos: dict[str, tuple[float, RepoRef | None]]
        _worker_prs: dict[tuple[str, str, str, int], tuple[float, PrLookup]]

        def _checkout_dir(self, record: WorkspaceRecord) -> str: ...

    async def _fill_worker_prs(
        self, record: WorkspaceRecord, board: BoardSnapshot
    ) -> BoardSnapshot:
        """Each SHOWN worker row with its PR and CI, read from the channel's own repository.

        The lookups run concurrently (each is cached, see :meth:`_worker_pr`); rows
        the board does not show are counted but never looked up.
        """
        cwd = self._checkout_dir(record)
        repo = parse_remote(await asyncio.to_thread(git_ops.remote_url, cwd)) if cwd else None
        if repo is None:
            return board
        # A worker working in ANOTHER repository (its session moved there) reports
        # that repository's branch and PR: nothing of it is looked up in this
        # channel's repository, and its branch, PR and CI are left off the row,
        # so the board, the bar, the Code tab and the wrap-up never show another
        # repository's PR or commits as this worker's.
        foreign = await self._foreign_workers(repo, board.rows)
        if foreign:
            board = replace(
                board,
                rows=tuple(
                    (
                        replace(
                            r,
                            branch="",
                            pr_number=0,
                            pr_url="",
                            pr_state="",
                            pr_ci="",
                            pr_checks=None,
                        )
                        if r.item_id in foreign
                        else r
                    )
                    for r in board.rows
                ),
            )
        # Every row either look can show: the plan's (up to 49) and the rows' (20).
        visible = {r.item_id for r in board.plan_rows} | {r.item_id for r in board.shown}
        shown = [r for r in board.rows if r.item_id in visible]
        lookups = await asyncio.gather(
            *(self._worker_pr(repo, r.branch, r.pr_number) for r in shown)
        )
        filled = dict(zip((r.item_id for r in shown), lookups))
        rows = []
        for row in board.rows:
            lookup = filled.get(row.item_id)
            if lookup is not None and lookup.status == "found" and lookup.pr is not None:
                pr = lookup.pr
                row = row.with_pr(pr.number, pr.url, pr.state, pr.ci, pr.checks)
            elif row.pr_number:
                # A number the worker reported that the forge could not confirm:
                # shown, but without a link or CI that would claim more.
                row = row.with_pr(row.pr_number, "", "", "")
            rows.append(row)
        return replace(board, rows=tuple(rows))

    async def _foreign_workers(self, repo: RepoRef, rows: Any) -> set[str]:
        """Item ids of rows whose worker works in a repository other than *repo*.

        A worker's repository is its session's project folder's ``origin``. One
        with no folder, or whose folder names no repository git can read, is not
        called foreign: it was dispatched from this channel, and nothing says it
        left. Folder reads are cached for ``service._PR_TTL_LIVE``.
        """
        from kiro_crew.slack.code_channels import surface
        from kiro_crew.slack.code_channels.workspace.service import _find_slot

        state = surface.dashboard_state()
        out: set[str] = set()
        for row in rows:
            slot = _find_slot(state, row.session_key) if row.session_key else None
            project = str(getattr(slot, "project", "") or "") if slot is not None else ""
            if not project:
                continue
            theirs = await self._repo_of(project)
            if theirs is not None and (theirs.host, theirs.slug) != (repo.host, repo.slug):
                out.add(row.item_id)
        return out

    async def _repo_of(self, project: str) -> RepoRef | None:
        """The repository *project*'s ``origin`` names (cached), or ``None``."""
        from kiro_crew.slack.code_channels.workspace.service import _PR_TTL_LIVE

        now = self._clock()
        cached = self._project_repos.get(project)
        if cached is not None and now - cached[0] < _PR_TTL_LIVE:
            return cached[1]
        try:
            url = await asyncio.to_thread(git_ops.remote_url, project)
        except Exception:
            url = ""
        found = parse_remote(url) if url else None
        if len(self._project_repos) > 512:
            self._project_repos.clear()
        self._project_repos[project] = (now, found)
        return found

    async def filled(self, binding: Any, snapshot: BoardSnapshot) -> BoardSnapshot:
        """*snapshot* with its workers' PRs read from the channel's repository.

        For a reader that has a fresh board but no refresh (the wrap-up after a
        restart): the same fill a refresh does, so nothing it shows comes from
        another repository. With no workspace record, the snapshot as is.
        """
        team, channel = str(binding.team_id or ""), str(binding.channel_id or "")
        record = await asyncio.to_thread(self._store.get, team, channel)
        if record is None:
            return snapshot
        return await self._fill_worker_prs(record, snapshot)

    async def _worker_pr(self, repo: RepoRef, branch: str, number: int) -> PrLookup | None:
        from kiro_crew.slack.code_channels.workspace.service import (
            _PR_TTL_LIVE,
            _PR_TTL_SETTLED,
        )

        if not branch and not number:
            return None
        cache_key = (repo.host, repo.slug, branch, number)
        now = self._clock()
        cached = self._worker_prs.get(cache_key)
        if cached is not None:
            read_at, lookup = cached
            settled = lookup.pr is not None and lookup.pr.state in {"merged", "closed"}
            if now - read_at < (_PR_TTL_SETTLED if settled else _PR_TTL_LIVE):
                return lookup
        try:
            if number:
                # The worker said which PR is its own: that one is read, never a
                # PR found by branch, which can be a later PR on a reused branch.
                lookup = await self._find_pr_number(repo, number)
            else:
                lookup = await self._find_pr(repo, branch)
        except Exception:
            logger.debug("code channel: worker PR lookup failed", exc_info=True)
            return cached[1] if cached else None
        if lookup.status == "unavailable" and cached is not None:
            return cached[1]  # gh down: keep what was last seen
        if len(self._worker_prs) > 512:
            self._worker_prs.clear()
        self._worker_prs[cache_key] = (now, lookup)
        return lookup
