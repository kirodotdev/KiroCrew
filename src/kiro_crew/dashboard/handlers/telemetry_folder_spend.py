"""Spend grouped by sidebar folder, for the Telemetry panel's Spend tab.

``cost_breakdown`` cannot see folders, so it hands back every session's spend
under ``slot_spend`` and :func:`_with_folder_spend` rolls it up by the folder
each session is filed in. A closed session's folder comes from the history
listing index (:meth:`ConversationLog.list_sessions_indexed`), which already
holds each transcript's ``folder_id`` and ``cron_run``.
"""

from __future__ import annotations

import asyncio
import logging
from typing import Any, Callable

from aiohttp import web

from kiro_crew.dashboard.chat_utils import dashboard_slot_key, usage_transcript_keys
from kiro_crew.dashboard.folder_repository import FolderRepository
from kiro_crew.dashboard.handlers.usage import (
    _COST_SLOT_SPEND_CAP,
    rank_spend,
    spend_key_for_slot,
)
from kiro_crew.history import ConversationLog, transcript_stems

logger = logging.getLogger(__name__)


def _cron_job_key(key: str) -> str:
    """``cron:<job>`` for a per-run cron key ``cron:<job>:<run|agent>``, else ``""``."""
    if not key.startswith("cron:") or ":" not in key[5:]:
        return ""
    return "cron:" + key[5:].split(":", 1)[0]


def _live_slot_for(
    get_slot: Any,
    key: str,
    aliases: dict[str, str] | None = None,
    *,
    job_fallback: bool = True,
) -> Any:
    """The open dashboard slot for a usage-row session key, or ``None``.

    Tried in order:

    * the key itself (a slot name);
    * the slot the live registry says holds that session
      (``DashboardState.spend_slot_by_session``, keyed by
      :func:`spend_key_for_slot`), and for a cron execution key also the slot
      holding its job's session ``cron:<job>``. This is what reaches a tab
      whose NAME is not derived from the key, such as a ``workflow-<run>``
      fallback tab linked to ``slack:<ts>``;
    * the tab that displays it by name (:func:`dashboard_slot_key`), which is
      how ``cron:<job>:<run>`` reaches ``cron-<job>`` and ``slack:<ts>``
      reaches ``slack_<ts>``.

    With *job_fallback* off, a per-run key ``cron:<job>:<run|agent>`` finds
    only a tab of its own, never the job's: a run that opened its own tab
    keeps its folder in its own transcript once that tab closes.
    """
    slot = get_slot(key)
    if slot is not None:
        return slot
    job_key = _cron_job_key(key) or (key if key.startswith("cron:") else "")
    skip_job = not job_fallback and job_key != key and bool(job_key)
    if aliases:
        session_keys = [spend_key_for_slot(key)]
        if job_key and not skip_job:
            session_keys.append(job_key)
        for session_key in session_keys:
            alias = aliases.get(session_key)
            if isinstance(alias, str) and alias and alias != key:
                slot = get_slot(alias)
                if slot is not None:
                    return slot
    tab = dashboard_slot_key(key)
    if skip_job and tab and tab == dashboard_slot_key(job_key):
        return None
    return get_slot(tab) if tab and tab != key else None


def _is_untabbed_cron_run(transcript_key: str, meta: Any) -> bool:
    """True for a per-run cron transcript whose run did not open a tab of its own.

    ``cron:<job>:<run|agent>`` names a run's own transcript. Only a
    ``new_chat_per_run`` run that opened its tab re-tags it ``cron_run: tab``
    (``cron_delivery``); any other run was shown in the job's tab. *meta* is
    the transcript's listing row, which carries ``cron_run``.
    """
    if not _cron_job_key(transcript_key):
        return False
    return not (isinstance(meta, dict) and meta.get("cron_run") == "tab")


def _folder_paths(folders: list[dict[str, Any]]) -> dict[str, str]:
    """Map each sidebar folder id to the breadcrumb the sidebar itself shows.

    :meth:`FolderRepository.breadcrumb` owns the walk (cycle-safe, `` › ``
    separator), so a path here reads exactly as it does in the sidebar.
    """
    ids = {str(f["id"]) for f in folders if isinstance(f, dict) and f.get("id")}
    return {fid: FolderRepository.breadcrumb(folders, fid) for fid in ids}


def _recorded_first(recorded: dict[str, str]) -> Callable[[str], tuple[str, ...]]:
    """:func:`usage_transcript_keys`, led by the transcript a key's rows recorded.

    A slot name cannot say which transcript it writes: a dashboard tab may carry a
    channel-shaped or cron-shaped name, and a task-review tab writes the
    task-runner key it is linked to. The recorded key is the writer's own answer.
    """

    def keys(slot_key: str) -> tuple[str, ...]:
        fallback = usage_transcript_keys(slot_key)
        own = recorded.get(slot_key)
        if not isinstance(own, str) or not own:
            return fallback
        return (own, *(k for k in fallback if k != own))

    return keys


async def _closed_folders(
    slot_spend: Any, conversation_log: Any, keys: list[str]
) -> tuple[dict[str, str], frozenset[str]]:
    """``folder_id`` from the transcript of each closed session in *keys*.

    Also the per-run cron keys whose own transcript answered: a run that opened
    a tab of its own, whose folder is its own and not its job's. Off the loop:
    the first listing of a process may build the index.
    """
    recorded = slot_spend.get("transcripts") if isinstance(slot_spend, dict) else None
    return await asyncio.to_thread(
        _indexed_folders,
        conversation_log,
        keys,
        _recorded_first(recorded if isinstance(recorded, dict) else {}),
    )


def _indexed_folders(
    conversation_log: Any, keys: list[str], transcripts: Callable[[str], tuple[str, ...]]
) -> tuple[dict[str, str], frozenset[str]]:
    """:func:`_closed_folders` from one listing-index snapshot. Blocking; call off-loop.

    Each key tries its *transcripts* candidates in order. The first candidate
    whose transcript EXISTS (has a listing row) is the answer, even when it
    names no folder: an unfiled dashboard tab named ``cron-foo`` has its own
    transcript, and falling through to ``cron:foo`` would read an unrelated
    job's. A missing transcript moves on to the next candidate, and so does a
    per-run cron transcript (``cron:<job>:<run|agent>``) not tagged
    ``cron_run: tab``, whatever folder it names: that run was shown in the
    job's tab, so the job's transcript answers for it.

    A row's ``key`` is its file stem, so a candidate is looked up by the stems
    ``get_metadata`` would open (:func:`transcript_stems`: the canonical one,
    then a Slack thread's legacy one), folded by
    :meth:`ConversationLog._canonical_key` as the listing folds stacked
    ``dashboard_`` copies. The folder id is bounded by ``_bounded_folder_id``,
    so a value longer than any real id reads as Unfiled.
    """
    # Imported here: ``session_revive`` loads ``chat_folders``, whose import chain
    # reaches ``handlers/__init__`` and so this module.
    from kiro_crew.dashboard.session_control import _bounded_folder_id

    try:
        listing = conversation_log.list_sessions_indexed()
    except Exception:
        logger.debug("no session listing for the folder roll-up", exc_info=True)
        return {}, frozenset()
    rows: dict[str, dict[str, Any]] = {}
    for row in listing:
        stem = row.get("key") if isinstance(row, dict) else None
        if isinstance(stem, str) and stem:
            rows.setdefault(ConversationLog._canonical_key(stem), row)

    def row_for(transcript_key: str) -> dict[str, Any] | None:
        for stem in transcript_stems(transcript_key):
            row = rows.get(ConversationLog._canonical_key(stem))
            if row is not None:
                return row
        return None

    found: dict[str, str] = {}
    own_runs: set[str] = set()
    for slot_key in keys:
        try:
            candidates = transcripts(slot_key)
        except Exception:  # pragma: no cover — a key shape no rule recognises
            continue
        for transcript_key in candidates:
            row = row_for(transcript_key)
            if row is None or _is_untabbed_cron_run(transcript_key, row):
                continue
            folder_id = _bounded_folder_id(row.get("folder_id"))
            if folder_id:
                found[slot_key] = folder_id
            if transcript_key == slot_key and row.get("cron_run") == "tab":
                own_runs.add(slot_key)
            break
    return found, frozenset(own_runs)


async def _with_folder_spend(request: web.Request, cost: dict[str, Any]) -> dict[str, Any]:
    """Replace the internal per-session spend with spend grouped by sidebar folder.

    ``cost_breakdown`` cannot see folders: they live on the dashboard state, not
    in the token rows. It hands back every session's current and prior credits
    under ``slot_spend``, and this rolls them up by the folder each session is
    filed in. A session's own live slot wins, so a move shows at once. A closed
    per-run cron tab uses its own persisted folder before the open job tab's
    folder. Other closed sessions use transcript metadata after live lookup. A
    session in no folder, or in one deleted since, or one whose folder cannot
    be read (a closed task-review tab, see :func:`usage_transcript_keys`), is
    counted under the empty-id row the frontend labels Unfiled, so the folder
    rows add up to the window total.
    Sessions past ``cost_breakdown``'s per-session cap, or with a key longer
    than its key bound, also land in Unfiled. The Unfiled row says how much of
    it came from each, credits and turns: ``capped_credits`` / ``capped_turns``
    (with ``capped_limit``, the cap) and ``long_key_credits`` /
    ``long_key_turns``, each pair present only when that cause has any.
    A live slot is found by its name, by the live session registry
    (``spend_slot_by_session``) or by the tab that displays the key. For a
    per-run cron key, these lookups first exclude the job tab. The run's own
    persisted transcript then decides whether the job tab is the live fallback.
    Both periods use the
    folder a session is in NOW: the store keeps no filing history, so a moved
    session carries its earlier spend with it.

    ``slot_spend`` is always dropped from the returned payload, which is a copy:
    the input is ``cost_breakdown``'s memoised object.
    """
    slot_spend = cost.get("slot_spend") or {}
    out = {k: v for k, v in cost.items() if k != "slot_spend"}
    out["by_folder"] = []
    try:
        state = request.app["state"]
    except KeyError:
        return out
    get_slot = getattr(state, "get_slot", None)
    if not callable(get_slot):
        return out
    current: dict[str, dict[str, Any]] = slot_spend.get("current") or {}
    prior: dict[str, float] = slot_spend.get("prior") or {}
    # The committed tree, read under the folder store's lock, so a folder
    # mutation whose write is still in flight (and may roll back) is not shown.
    read_folders = getattr(state, "read_folders", None)
    if callable(read_folders):
        paths = await read_folders(_folder_paths)
    else:
        paths = _folder_paths(list(getattr(state, "_folders", None) or []))

    folder_of: dict[str, str] = {}
    unresolved: list[str] = []
    # Live session identity -> slot key, read once on the loop (it walks live
    # slot objects). Validated here because a stub state may hand back anything.
    spend_slot_by_session = getattr(state, "spend_slot_by_session", None)
    aliases: dict[str, str] | None = None
    if callable(spend_slot_by_session):
        try:
            got = spend_slot_by_session()
        except Exception:
            logger.debug("no live session aliases for the folder roll-up", exc_info=True)
            got = None
        aliases = got if isinstance(got, dict) else None
    # A per-run cron key whose only live tab is its job's: the job tab's folder,
    # used unless the run's own transcript answers (a closed tab of its own).
    job_tab_folder: dict[str, str] = {}
    for slot_key in {*current, *prior}:
        if not slot_key:
            continue
        slot = _live_slot_for(get_slot, slot_key, aliases, job_fallback=False)
        if slot is None and _cron_job_key(slot_key):
            job_slot = _live_slot_for(get_slot, slot_key, aliases)
            if job_slot is not None:
                fid = getattr(job_slot, "folder_id", "")
                job_tab_folder[slot_key] = fid if isinstance(fid, str) else ""
        if slot is not None:
            fid = getattr(slot, "folder_id", "")
            folder_of[slot_key] = fid if isinstance(fid, str) else ""
        else:
            unresolved.append(slot_key)
    conversation_log = getattr(state, "conversation_log", None)
    own_runs: frozenset[str] = frozenset()
    if unresolved and conversation_log is not None:
        closed, own_runs = await _closed_folders(slot_spend, conversation_log, unresolved)
        folder_of.update(closed)
    for slot_key, fid in job_tab_folder.items():
        if slot_key not in own_runs:
            folder_of[slot_key] = fid

    def _folder(slot_key: str) -> str:
        fid = folder_of.get(slot_key, "")
        return fid if fid in paths else ""

    bucket: dict[str, dict[str, Any]] = {}
    for slot_key, e in current.items():
        fid = _folder(slot_key)
        row = bucket.setdefault(
            fid, {"name": paths.get(fid, ""), "folder_id": fid, "credits": 0.0, "turns": 0}
        )
        row["credits"] = float(row["credits"]) + float(e.get("credits") or 0.0)
        row["turns"] = int(row["turns"]) + int(e.get("turns") or 0)
    deltas: dict[str, float] = {}
    for slot_key, credits in prior.items():
        fid = _folder(slot_key)
        deltas[fid] = deltas.get(fid, 0.0) + float(credits or 0.0)
    # What Unfiled takes in from outside the kept set, by cause: sessions past
    # ``cost_breakdown``'s per-session cap (``overflow``) and sessions whose key
    # is too long to retain (``over_long``). Counted in Unfiled, not attributed.
    # One disclosure model for both: the Unfiled row carries each cause's exact
    # credits AND turns whenever either is non-zero, so a token- or cost-only
    # provider's zero-credit turns are named too, and the panel names each
    # cause only when it is present. No session count: the accumulator evicts
    # while it reads, and a session evicted and seen again would count twice.
    for source, prefix in (("overflow", "capped"), ("over_long", "long_key")):
        tail = slot_spend.get(source) or {}
        tail_credits = float(tail.get("credits") or 0.0)
        tail_turns = int(tail.get("turns") or 0)
        if tail_credits > 0 or tail_turns:
            row = bucket.setdefault("", {"name": "", "folder_id": "", "credits": 0.0, "turns": 0})
            row["credits"] = float(row["credits"]) + tail_credits
            row["turns"] = int(row["turns"]) + tail_turns
            row[f"{prefix}_credits"] = round(tail_credits, 1)
            row[f"{prefix}_turns"] = tail_turns
            if prefix == "capped":
                # The cap the tail was cut at, so the panel names the real
                # limit instead of a number baked into its copy.
                row["capped_limit"] = _COST_SLOT_SPEND_CAP
        if float(tail.get("prior_credits") or 0.0) > 0:
            deltas[""] = deltas.get("", 0.0) + float(tail["prior_credits"])
    total = sum(float(e["credits"]) for e in bucket.values())
    out["by_folder"] = rank_spend(bucket, deltas, total, places)
    return out


async def _with_folder_spend_per_view(request: web.Request, cost: dict[str, Any]) -> dict[str, Any]:
    """:func:`_with_folder_spend` on *cost* and on its nested ``usd`` view, if any.

    A window billed in both units carries its dollar figures as a nested ``usd``
    view with its own ``slot_spend``. Each view keeps its own sessions, so each
    groups its own by folder, and neither view's ``slot_spend`` leaves the handler.
    """
    cost = await _with_folder_spend(request, cost)
    if isinstance(cost.get("usd"), dict):
        cost = {**cost, "usd": await _with_folder_spend(request, cost["usd"])}
    return cost
