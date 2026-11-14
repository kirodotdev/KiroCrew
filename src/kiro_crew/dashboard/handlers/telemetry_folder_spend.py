"""Spend grouped by sidebar folder, for the Telemetry panel's Spend tab.

``cost_breakdown`` cannot see folders, so it hands back every session's spend
under ``slot_spend`` and :func:`_with_folder_spend` rolls it up by the folder
each session is filed in. Also holds the transcript-metadata read
(:func:`_persisted_field`) that the closed-conversation title lookup in
:mod:`kiro_crew.dashboard.handlers.telemetry` shares. Callers import these
names through that module.
"""

from __future__ import annotations

import asyncio
import logging
from typing import Any, Callable

from aiohttp import web

from kiro_crew.dashboard.chat_utils import dashboard_slot_key, usage_transcript_keys
from kiro_crew.dashboard.folder_repository import FolderRepository
from kiro_crew.dashboard.handlers.usage import rank_spend, spend_key_for_slot

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
    (``cron_delivery``); any other run was shown in the job's tab.
    """
    if not _cron_job_key(transcript_key):
        return False
    return not (isinstance(meta, dict) and meta.get("cron_run") == "tab")


def _persisted_field(
    conversation_log: Any,
    slot_keys: list[str],
    field: str,
    transcripts: Callable[[str], tuple[str, ...]] = usage_transcript_keys,
    tabbed_runs: set[str] | None = None,
) -> dict[str, str]:
    """Read one string *field* from each slot's transcript metadata. Blocking; call off-loop.

    *tabbed_runs*, when given, receives keys answered by their own transcript
    with ``cron_run: tab`` metadata.

    The one transcript-metadata read this module does, shared by the title and
    folder lookups for closed conversations. Only a non-empty string counts.

    ``get_metadata`` rather than ``list_sessions``: the latter falls back to the
    first user message and then to the session key when the metadata line names
    no title, which would turn a ranking label into prompt text and leave no way
    to tell a named conversation from an unnamed one.

    Keyed by the caller's key on the way out, so the caller never has to know
    how a key maps onto a transcript (*transcripts*, by default
    :func:`usage_transcript_keys`, which can name more than one, tried in
    order). The first candidate whose transcript
    EXISTS is the answer, even when it leaves *field* empty: an unfiled
    dashboard tab named ``cron-foo`` has its own transcript, and falling
    through to ``cron:foo`` would read an unrelated job's. Only a missing
    transcript moves on to the next candidate. When *field* is ``folder_id``,
    so does a per-run cron transcript (``cron:<job>:<run|agent>``) not tagged
    ``cron_run: tab``, whatever folder it names: that run was shown in the
    job's tab, so the job's transcript answers for it. Distinct
    keys can share one transcript (a channel-born slot's conversation IS the
    channel's, every run of a cron job writes the job's), so the read is
    deduplicated by transcript key rather than by key.
    """
    # transcript key -> (field value, whether the transcript exists, tabbed run)
    by_transcript: dict[str, tuple[str, bool, bool]] = {}
    out: dict[str, str] = {}
    for slot_key in slot_keys:
        try:
            transcript_keys = transcripts(slot_key)
        except Exception:  # pragma: no cover — a key shape no rule recognises
            continue
        for transcript_key in transcript_keys:
            if transcript_key not in by_transcript:
                try:
                    meta = conversation_log.get_metadata(transcript_key) or {}
                except Exception:
                    logger.debug("no persisted %s for %s", field, transcript_key, exc_info=True)
                    meta = {}
                tabbed_run = isinstance(meta, dict) and meta.get("cron_run") == "tab"
                untabbed_run = field == "folder_id" and _is_untabbed_cron_run(transcript_key, meta)
                raw = meta.get(field) if isinstance(meta, dict) else None
                value = raw if isinstance(raw, str) and not untabbed_run else ""
                exists = bool(value)
                if not exists and not untabbed_run:
                    try:
                        exists = bool(conversation_log.has_log(transcript_key))
                    except Exception:
                        logger.debug("no transcript check for %s", transcript_key, exc_info=True)
                by_transcript[transcript_key] = (value, exists, tabbed_run)
            value, exists, tabbed_run = by_transcript[transcript_key]
            if value:
                out[slot_key] = value
            if exists:
                if tabbed_runs is not None and transcript_key == slot_key and tabbed_run:
                    tabbed_runs.add(slot_key)
                break
    return out


def _folder_paths(folders: list[dict[str, Any]]) -> dict[str, str]:
    """Map each sidebar folder id to the breadcrumb the sidebar itself shows.

    :meth:`FolderRepository.breadcrumb` owns the walk (cycle-safe, `` › ``
    separator), so a path here reads exactly as it does in the sidebar.
    """
    ids = {str(f["id"]) for f in folders if isinstance(f, dict) and f.get("id")}
    return {fid: FolderRepository.breadcrumb(folders, fid) for fid in ids}


#: Single-entry memo of the closed-transcript folder reads. Keyed by the
#: ``slot_spend`` OBJECT (identity, not value): ``cost_breakdown`` hands back the
#: same memoised object until its cache refreshes, so every poll inside that
#: window reuses one round of transcript reads instead of repeating them. The
#: conversation log and the exact set of keys to resolve are part of the key, so
#: a slot that closes mid-window is still read.
_CLOSED_FOLDER_MEMO: tuple[Any, Any, frozenset[str], dict[str, str], frozenset[str]] | None = None

#: Longest persisted ``folder_id`` the memo keeps. Sidebar folder ids are 12 hex
#: characters (``uuid4().hex[:12]``), so a longer value cannot name a real folder
#: and is dropped before it is retained: the session lands in Unfiled, which is
#: where an unknown id would group it anyway, and the memo holds at most this
#: many characters per key however large the transcript metadata line is.
_FOLDER_ID_MAX_CHARS = 64


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
    """``folder_id`` from the transcript of each closed session in *keys*, memoised.

    Also the per-run cron keys whose own transcript answered: a run that opened
    a tab of its own, whose folder is its own and not its job's.
    """
    global _CLOSED_FOLDER_MEMO
    want = frozenset(keys)
    memo = _CLOSED_FOLDER_MEMO
    if (
        memo is not None
        and memo[0] is slot_spend
        and memo[1] is conversation_log
        and memo[2] == want
    ):
        return memo[3], memo[4]
    tabbed_runs: set[str] = set()
    recorded = slot_spend.get("transcripts") if isinstance(slot_spend, dict) else None
    read = await asyncio.to_thread(
        _persisted_field,
        conversation_log,
        keys,
        "folder_id",
        _recorded_first(recorded if isinstance(recorded, dict) else {}),
        tabbed_runs,
    )
    found = {k: v for k, v in read.items() if len(v) <= _FOLDER_ID_MAX_CHARS}
    own_runs = frozenset(tabbed_runs)
    _CLOSED_FOLDER_MEMO = (slot_spend, conversation_log, want, found, own_runs)
    return found, own_runs


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
    it came from those, as ``unsorted_credits`` / ``unsorted_turns``, present
    only when there is any.
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
    # What Unfiled takes in from outside the kept set (``unsorted``): sessions
    # past ``cost_breakdown``'s per-session cap, or whose key is too long to
    # retain. Counted in Unfiled, not attributed. The Unfiled row carries its
    # exact credits AND turns whenever either is non-zero, so a token- or
    # cost-only provider's zero-credit turns are named too.
    tail = slot_spend.get("unsorted") or {}
    tail_credits = float(tail.get("credits") or 0.0)
    tail_turns = int(tail.get("turns") or 0)
    # Amounts round as ``cost_breakdown`` rounds its own: credits to one
    # decimal, dollars (``unit: usd``) to four.
    places = 4 if cost.get("unit") == "usd" else 1
    if tail_credits > 0 or tail_turns:
        row = bucket.setdefault("", {"name": "", "folder_id": "", "credits": 0.0, "turns": 0})
        row["credits"] = float(row["credits"]) + tail_credits
        row["turns"] = int(row["turns"]) + tail_turns
        row["unsorted_credits"] = round(tail_credits, places)
        row["unsorted_turns"] = tail_turns
    if float(tail.get("prior_credits") or 0.0) > 0:
        deltas[""] = deltas.get("", 0.0) + float(tail["prior_credits"])
    total = sum(float(e["credits"]) for e in bucket.values())
    out["by_folder"] = rank_spend(bucket, deltas, total, places)
    return out
