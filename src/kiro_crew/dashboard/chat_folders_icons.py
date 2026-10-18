"""Chat folder icons and colours.

The emoji check, the colour palette, the background LLM emoji generator (which
the artifact library shares) and the icon tasks a folder rename spawns, with the
task and epoch tables that let a delete cancel them. New icon or colour rules go
here.
"""

from __future__ import annotations

import asyncio
import logging
import unicodedata
from typing import Any

from kiro_crew.dashboard.state import DashboardState
from kiro_crew.llm_helpers import run_bg_oneliner
from kiro_crew.loop_lock import LoopBoundLock
from kiro_crew.security import redact_credentials, redact_exfiltration_urls

logger = logging.getLogger("kiro_crew.dashboard.chat_folders")


_folder_icon_lock = LoopBoundLock()


def _is_single_emoji(s: str) -> bool:
    """True if `s` is exactly one emoji grapheme (no letters/digits/text).

    Accepts simple emoji, variation-selector / skin-tone modified emoji, ZWJ
    sequences (families, professions), and two-codepoint flag pairs. Rejects
    empty strings, plain text, and multiple emoji.
    """
    if not s or len(s) > 16:
        return False
    modifiers = {0xFE0F, 0x200D}  # variation selector-16, zero-width joiner

    def _emoji_char(c: str) -> bool:
        o = ord(c)
        return (
            unicodedata.category(c).startswith("So")  # symbol, other
            or o > 0x1F000  # supplementary emoji planes
            or o in modifiers
            or 0x1F3FB <= o <= 0x1F3FF  # skin-tone modifiers
            or 0x1F1E6 <= o <= 0x1F1FF  # regional indicators (flags)
        )

    if not all(_emoji_char(c) for c in s):
        return False
    # Count grapheme clusters; must be exactly one.
    cps = [ord(c) for c in s]
    n = len(cps)
    clusters = 0
    i = 0
    while i < n:
        if 0x1F1E6 <= cps[i] <= 0x1F1FF:  # flag = pair of regional indicators
            clusters += 1
            i += 2 if (i + 1 < n and 0x1F1E6 <= cps[i + 1] <= 0x1F1FF) else 1
        else:
            clusters += 1  # base emoji, then absorb modifiers / ZWJ-joined emoji
            i += 1
            while i < n and (cps[i] == 0xFE0F or 0x1F3FB <= cps[i] <= 0x1F3FF):
                i += 1
            while i < n and cps[i] == 0x200D:  # ZWJ joins the following emoji
                i += 2 if i + 1 < n else 1
                while i < n and (cps[i] == 0xFE0F or 0x1F3FB <= cps[i] <= 0x1F3FF):
                    i += 1
        if clusters > 1:
            return False
    return clusters == 1


# "auto" = inherit the session's governed default (run_bg_oneliner skips the
# override for auto). A hardcoded model id 400s on accounts/partitions that do
# not serve it.
_FOLDER_ICON_MODEL = "auto"


# Folder color palette — the identity mark a user picks for a folder in the
# config modal. The frontend source of truth is FOLDER_COLOR_PALETTE in
# website/src/components/folderColorCatalog.tsx (shared by the chat-folder
# modal and the Artifacts page's folder swatches); this allowlist must match
# it, and test_folder_color_palette_matches_frontend_catalog pins the two.
_FOLDER_COLOR_PALETTE = frozenset(
    {
        "#ef4444",
        "#f97316",
        "#f59e0b",
        "#84cc16",
        "#22c55e",
        "#14b8a6",
        "#06b6d4",
        "#3b82f6",
        "#6366f1",
        "#8b5cf6",
        "#ec4899",
        "#94a3b8",
    }
)


def _is_valid_folder_color(s: str) -> bool:
    """True for a palette color value (lowercase hex, allowlisted)."""
    return s in _FOLDER_COLOR_PALETTE


async def generate_emoji_for_name(state: DashboardState, name: str) -> str:
    """Ask the background session for ONE emoji representing a folder ``name``.

    Shared by chat folders and artifact-library folders. Serialized via a
    module-level lock so concurrent folder creations don't interleave streams
    on the shared BACKGROUND_KEY session. Returns ``""`` on any failure or
    when the reply isn't exactly one emoji grapheme.
    """

    prompt = (
        f'Reply with exactly ONE emoji that best represents a project folder named "{name}". '
        "No text, no explanation, just the single emoji character."
    )

    # Folder icon is a trivial single-emoji task — run on the session's governed
    # default model (``_FOLDER_ICON_MODEL`` is "auto") via the shared background
    # one-liner helper (best-effort, 30s bound, denials
    # SEL-logged). The lock serializes icon generation across folders.
    async with _folder_icon_lock:
        try:
            text = await run_bg_oneliner(
                state.sessions,
                prompt,
                model=_FOLDER_ICON_MODEL,
                sel_source="chat_folders",
                timeout=30,
            )
        except Exception:  # noqa: BLE001 — best-effort background task
            text = ""
    icon = text.strip()
    icon, _ = redact_exfiltration_urls(icon)
    icon, _ = redact_credentials(icon)
    # Validate: must be exactly one emoji (guard against stray LLM text).
    return icon if _is_single_emoji(icon) else ""


# Strong refs so in-flight icon tasks aren't garbage-collected mid-run — the
# same pattern as the artifact library's _ARTIFACT_FOLDER_ICON_TASKS.
_CHAT_FOLDER_ICON_TASKS: set[asyncio.Task[None]] = set()

#: Per-folder coalescing index over ``_CHAT_FOLDER_ICON_TASKS``: at most ONE
#: live generation task per folder id. Spawning for a folder that already has
#: a pending task CANCELS the pending one and takes its slot — the latest
#: request's name wins, matching the epoch rule for write-backs. Without this,
#: an authenticated caller looping ``regenerate_icon`` PATCHes could enqueue
#: tasks faster than the serialized generator drains them (one bounded model
#: call at a time behind ``_folder_icon_lock``), growing both the pending-task
#: set and the paid-call backlog without bound. Entries remove themselves via
#: the done callback (identity-checked, so a superseded task's callback never
#: evicts its successor).
_CHAT_FOLDER_PENDING_ICON_TASKS: dict[str, asyncio.Task[None]] = {}

#: Per-folder icon epoch, bumped under the folder-store lock by every
#: user-visible mutation the generated icon must not outlive: a manual icon
#: set, an icon clear, and a rename. An in-flight generation task captures the
#: epoch at scheduling time and its write-back is dropped unless the epoch is
#: unchanged. One invariant closes all three races — the previous
#: ``expected_icon`` value-pin passed whenever the icon VALUE happened to be
#: unchanged, so a clear (``None`` -> ``None``) or a rename (icon untouched)
#: let a stale emoji land after the user's action. Deliberately per-folder,
#: not the store-wide ``folders_generation()`` counter: that bumps on every
#: folder mutation anywhere, so pinning to it would cancel a legitimate icon
#: delivery whenever an unrelated folder changed mid-generation. In-memory on
#: purpose — in-flight tasks die with the process, so the epoch has nothing
#: to survive a restart for. Entries are dropped on folder delete.
_CHAT_FOLDER_ICON_EPOCHS: dict[str, int] = {}


def _bump_icon_epoch(folder_id: str) -> None:
    """Invalidate any in-flight icon generation for this folder.

    Must be called under the folder-store lock — from a ``mutate_folders``
    post-commit hook or a mutation callback — which is what orders the bump
    against the write-back's check. The post-commit hook is the right home
    for a bump tied to a persisted change: it never runs for a rolled-back
    or no-op transaction, so the epoch always mirrors committed state.
    """
    _CHAT_FOLDER_ICON_EPOCHS[folder_id] = _CHAT_FOLDER_ICON_EPOCHS.get(folder_id, 0) + 1


def _spawn_chat_folder_icon_task(
    state: DashboardState,
    folder_id: str,
    name: str,
    *,
    expected_epoch: int,
) -> None:
    """Fire-and-forget: derive a single-emoji icon for a chat folder and store it.

    Spawned only by the explicit Auto-generate action (PATCH regenerate_icon),
    so every model call is user-initiated. The PATCH response returns
    immediately; the icon lands later via
    the slots push (the WS frame triggers the client's folder refetch). The
    write-back re-finds the folder by id under the store lock, so a folder
    deleted while generation was in flight is never resurrected, and only
    applies while the folder's icon epoch still equals ``expected_epoch`` (its
    value when this task was scheduled) — a manual icon set, an icon clear, or
    a rename that lands while generation is pending wins over the stale
    result. Best-effort — any failure leaves the folder's current icon
    unchanged. Coalesced per folder: a spawn for a folder with a generation
    already pending cancels the pending task and replaces it, so concurrent
    regenerate requests can never accumulate more than one live task — and
    one model call — per folder. Folder delete cancels and unregisters the
    folder's pending task, so a task never outlives its folder and the live
    set stays bounded by the extant-folder cap. A superseded task that has
    already begun its store write finishes that write under the lock before
    unwinding, so persisted snapshots stay strictly serialized.
    """

    async def _run() -> None:
        try:
            icon = await generate_emoji_for_name(state, name)
            if not icon:
                return

            def _write(folders: list[dict[str, Any]]) -> tuple[bool, bool]:
                target = next((f for f in folders if f["id"] == folder_id), None)
                if target is None:
                    return False, False  # deleted mid-generation; drop the icon
                if _CHAT_FOLDER_ICON_EPOCHS.get(folder_id, 0) != expected_epoch:
                    # The icon or name changed while generation was in flight
                    # (manual set, clear, or rename) — drop the stale result.
                    return False, False
                target["icon"] = icon
                return True, True

            # Shield the write-back from coalescing cancellation. Cancelling a
            # task awaiting asyncio.to_thread cannot stop the executor thread:
            # the CancelledError would release the store lock while the thread
            # finishes writing its whole-list snapshot, letting a stale
            # snapshot land after a superseding transaction and silently
            # revert interim committed folder mutations on reload. A started
            # transaction therefore always runs to completion under the lock;
            # cancellation still stops the throttle-wait and model phases.
            persist = asyncio.ensure_future(state.mutate_folders(_write))
            try:
                written = await asyncio.shield(persist)
            except asyncio.CancelledError:
                await persist
                raise
            if written:
                state.push_slots_update()
        except Exception:  # noqa: BLE001 — best-effort background task
            logger.debug("chat folder icon generation failed for %s", folder_id, exc_info=True)

    task = asyncio.ensure_future(_run())
    _CHAT_FOLDER_ICON_TASKS.add(task)
    # Coalesce per folder: cancel any pending generation for this folder and
    # take its slot, so a burst of regenerate requests holds at most one live
    # task (and at most one paid model call) per folder at a time.
    prior = _CHAT_FOLDER_PENDING_ICON_TASKS.get(folder_id)
    if prior is not None and not prior.done():
        prior.cancel()
    _CHAT_FOLDER_PENDING_ICON_TASKS[folder_id] = task

    def _cleanup(done: asyncio.Task[None]) -> None:
        _CHAT_FOLDER_ICON_TASKS.discard(done)
        if _CHAT_FOLDER_PENDING_ICON_TASKS.get(folder_id) is done:
            del _CHAT_FOLDER_PENDING_ICON_TASKS[folder_id]

    task.add_done_callback(_cleanup)
