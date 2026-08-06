"""Folder suggestion — offer an existing folder for a freshly-titled session.

A session started from the plain "New" button lands at the top level: there is
no ambient folder context to inherit, so filing it stays a separate deliberate
act that most sessions never get. This closes that gap without asking anything
at creation time — once the session has a title, the shared background session
picks the folder it belongs in (or none), and the dashboard offers a one-click
move above the composer.

Two properties are deliberate:

* **One shot per slot, never retried.** A suggestion is a convenience; a prompt
  that returns every turn is worse than no prompt at all. The claim is taken
  before the model call so a titling retry cannot fire a second card.
* **The model returns an INDEX, never text.** It picks a number from the list
  the prompt showed it, so nothing generated reaches the card — the rendered
  name and breadcrumb are the user's own stored folder data. An out-of-range
  number, prose, or the explicit ``NONE`` escape all mean "stay silent", which
  is the right default: a wrong folder costs the user more than an absent card.
"""

from __future__ import annotations

import asyncio
import logging
import os
import re
import time
from typing import Any

from kiro_crew.config.loader import KiroCrewConfig
from kiro_crew.dashboard.state import DashboardState, _ChatSlot
from kiro_crew.executors import subprocess_executor
from kiro_crew.history import INCOGNITO_MEMORY_MODES
from kiro_crew.llm_helpers import run_bg_oneliner

logger = logging.getLogger(__name__)

# No model override. Picking one folder from a short list is a trivial
# classification, and the shared background session this runs on is already the
# cheap one: the ``kirocrew-lite`` spec's model is ``_background_agent_model()``,
# which resolves ``agent.role_models['background']`` -> ``agent.model`` ->
# ``"auto"`` (AGENTS.md → Model selection). Pinning a concrete id here would both
# duplicate that resolution and break accounts not entitled to the pinned model,
# so an operator who wants this on a specific cheap model sets the background
# role instead.

# Serialized the way generate_emoji_for_name is: several tabs taking their first
# turn at once must not interleave streams on the shared background session.
_suggest_lock = asyncio.Lock()

# Prompt bounds. Every list below scales with the user's workspace, so each is
# capped rather than trusted — a workspace with 300 folders must not grow the
# prompt without limit.
_MAX_FOLDERS = 40
_MAX_SAMPLES_PER_FOLDER = 4
_MAX_SAMPLE_CHARS = 60
_MAX_TITLE_CHARS = 120
_MAX_MESSAGE_CHARS = 400
_SUGGEST_TIMEOUT_SECS = 30

_PROMPT_TEMPLATE = (
    "You are filing ONE new chat session into an existing folder.\n\n"
    "SESSION\n"
    "title: {title}\n"
    "first message: {message}\n\n"
    "FOLDERS\n"
    "{folders}\n\n"
    "Reply with ONLY the number of the folder this session belongs in.\n"
    "Reply with exactly NONE if no folder is a good fit — NONE is the correct "
    "answer whenever you are unsure. Do not explain.\n"
)


def _eligible_folders(state: DashboardState) -> list[dict[str, Any]]:
    """Folders a new session could be filed into, in sidebar order.

    ``hidden`` folders are excluded: the user tucked those away deliberately, so
    proactively surfacing one works against that. (A move still un-hides its
    target — see ``_unhide_folder`` — which is exactly why an unprompted
    suggestion should not resurrect one.) Entries lacking an ``id`` or a name are
    skipped because ``load_folders`` does no validation, so a hand-edited or
    legacy ``folders.json`` can contain either.
    """
    out = [
        f
        for f in state._folders
        if isinstance(f, dict)
        and f.get("id")
        and str(f.get("name") or "").strip()
        and not f.get("hidden")
    ]
    out.sort(key=lambda f: (int(f.get("order") or 0), str(f.get("name") or "")))
    return out[:_MAX_FOLDERS]


def _folder_sample_titles(state: DashboardState) -> dict[str, list[str]]:
    """Newest session titles per folder id — the only real topical grounding.

    A chat folder carries no description: ``name`` is its single descriptive
    field (see the create literal in ``chat_folders.py``), which is far too thin
    for a model to file "Fix the render gate flake" under "Kiro Crew › i18n".
    The titles already filed in a folder ARE its description, and
    ``list_sessions()`` yields title + folder_id newest-first in the one scan the
    folder-list endpoint already performs.

    Synchronous filesystem scan that grows with the archived-session count —
    call it through an executor, never on the loop thread.
    """
    samples: dict[str, list[str]] = {}
    if not state.conversation_log:
        return samples
    for session in state.conversation_log.list_sessions():
        # Private sessions are excluded BEFORE their folder or title is read.
        # INCOGNITO_MEMORY_MODES is documented as "never
        # searchable/listable/summarizable", and sampling a title to ground a
        # prompt is exactly that: the title would be shipped to a remote model.
        # A temporary session CAN be filed (api_chat_slot_folder does not gate on
        # memory_mode, and the folder id is persisted to its metadata line), so
        # the folder filter below is not enough on its own.
        if str(session.get("memory_mode") or "persistent") in INCOGNITO_MEMORY_MODES:
            continue
        fid = str(session.get("folder_id") or "")
        if not fid:
            continue
        bucket = samples.setdefault(fid, [])
        if len(bucket) >= _MAX_SAMPLES_PER_FOLDER:
            continue
        title = " ".join(str(session.get("title") or "").split())[:_MAX_SAMPLE_CHARS]
        if title:
            bucket.append(title)
    return samples


def _live_slot_titles(state: DashboardState, exclude_key: str) -> dict[str, list[str]]:
    """Titles of currently-open slots per folder id.

    Read from memory ahead of the disk scan because an open session is the most
    current statement of what a folder is for, and a session that has never been
    archived yet appears nowhere in ``list_sessions()``.
    """
    titles: dict[str, list[str]] = {}
    for slot in state._slots.values():
        if slot.key == exclude_key or not slot.folder_id:
            continue
        # Same privacy rule as the archived scan: a LIVE temporary/incognito slot
        # can be filed too, and its title must not reach the prompt either.
        if slot.memory_mode in INCOGNITO_MEMORY_MODES:
            continue
        title = " ".join(str(slot.title or "").split())[:_MAX_SAMPLE_CHARS]
        if not title:
            continue
        bucket = titles.setdefault(slot.folder_id, [])
        if len(bucket) < _MAX_SAMPLES_PER_FOLDER:
            bucket.append(title)
    return titles


def _merge_samples(
    live: dict[str, list[str]], archived: dict[str, list[str]]
) -> dict[str, list[str]]:
    """Live titles first, topped up from the archive, de-duplicated per folder."""
    merged: dict[str, list[str]] = {}
    for fid in set(live) | set(archived):
        seen: set[str] = set()
        bucket: list[str] = []
        for title in [*live.get(fid, []), *archived.get(fid, [])]:
            low = title.lower()
            if low in seen:
                continue
            seen.add(low)
            bucket.append(title)
            if len(bucket) >= _MAX_SAMPLES_PER_FOLDER:
                break
        merged[fid] = bucket
    return merged


def _build_prompt(
    *,
    title: str,
    message: str,
    labels: list[str],
    samples: list[list[str]],
) -> str:
    """Render the numbered folder list into the pick prompt.

    ``labels`` are breadcrumbs (``Kiro Crew › feature``) so a nested folder is
    distinguishable from a same-named sibling elsewhere in the tree.
    """
    lines = []
    for index, (label, folder_samples) in enumerate(zip(labels, samples), start=1):
        if folder_samples:
            contains = "; ".join(f'"{s}"' for s in folder_samples)
            lines.append(f"{index}. {label} — contains: {contains}")
        else:
            lines.append(f"{index}. {label} — (no sessions yet)")
    return _PROMPT_TEMPLATE.format(
        title=title or "(none)",
        message=message or "(none)",
        folders="\n".join(lines),
    )


def _parse_choice(text: str, folder_count: int) -> int | None:
    """Parse a reply into a 0-based folder index, or ``None`` for no suggestion.

    Tolerates a leading ``#`` and a trailing explanation the prompt asked the
    model not to give. Everything else — ``NONE``, an out-of-range number, an
    empty reply, prose — resolves to ``None``. The card is only worth showing
    when the pick is unambiguous, so every ambiguous shape stays silent.
    """
    head = text.strip().splitlines()[0].strip() if text.strip() else ""
    if not head or head.upper().startswith("NONE"):
        return None
    match = re.match(r"^#?(\d{1,3})\b", head)
    if not match:
        return None
    choice = int(match.group(1))
    if not 1 <= choice <= folder_count:
        return None
    return choice - 1


def _normalize_dir(raw: str) -> str:
    """Resolve a directory for comparison; "" when unset or unresolvable."""
    if not raw:
        return ""
    try:
        return os.path.realpath(os.path.expanduser(raw))
    except OSError:
        return ""


def _match_by_project_dir(
    folders: list[dict[str, Any]], project: str
) -> dict[str, Any] | None:
    """Pick a folder whose linked project directory is the slot's, or ``None``.

    A folder bound to a repo is a far stronger signal than any wording in the
    transcript, so this runs first and skips the model entirely when it hits.
    Requires a UNIQUE match: two folders on the same repo means the directory
    cannot decide between them, and the model should.
    """
    target = _normalize_dir(project)
    if not target:
        return None
    matches = [f for f in folders if _normalize_dir(str(f.get("project_dir") or "")) == target]
    return matches[0] if len(matches) == 1 else None


async def _match_by_project_dir_off_loop(
    folders: list[dict[str, Any]], project: str
) -> dict[str, Any] | None:
    """Run the directory match in an executor, never on the loop thread.

    ``realpath`` is a blocking syscall per folder, and a folder (or the slot's own
    project) can live on a network mount. One unresponsive mount would otherwise
    stall the gateway's event loop — every chat, WS push and heartbeat behind it —
    until the filesystem timed out, because this runs inside the titling task the
    loop is driving. Failures degrade to "no directory match", which just hands
    the decision to the model.
    """
    loop = asyncio.get_running_loop()
    try:
        return await loop.run_in_executor(
            subprocess_executor(), _match_by_project_dir, folders, project
        )
    except Exception:  # noqa: BLE001 — the model can still decide
        logger.debug("Folder suggestion: project_dir match failed", exc_info=True)
        return None


async def _pick_via_llm(
    state: DashboardState,
    slot: _ChatSlot,
    folders: list[dict[str, Any]],
) -> dict[str, Any] | None:
    """Ask the background session which folder fits. ``None`` on doubt or error."""
    loop = asyncio.get_running_loop()
    try:
        archived = await loop.run_in_executor(
            subprocess_executor(), _folder_sample_titles, state
        )
    except Exception:  # noqa: BLE001 — grounding is a bonus, not a requirement
        logger.debug("Folder suggestion: sample scan failed", exc_info=True)
        archived = {}
    samples = _merge_samples(_live_slot_titles(state, slot.key), archived)

    labels = [
        state.folder_breadcrumb(str(f["id"])) or str(f.get("name") or "") for f in folders
    ]
    first_message = next(
        (
            " ".join(str(m.get("content") or "").split())[:_MAX_MESSAGE_CHARS]
            for m in slot.messages
            if m.get("role") == "user" and str(m.get("content") or "").strip()
        ),
        "",
    )
    prompt = _build_prompt(
        title=" ".join(str(slot.title or "").split())[:_MAX_TITLE_CHARS],
        message=first_message,
        labels=labels,
        samples=[samples.get(str(f["id"]), []) for f in folders],
    )

    async with _suggest_lock:
        try:
            text = await run_bg_oneliner(
                state.sessions,
                prompt,
                sel_source="chat_folder_suggest",
                timeout=_SUGGEST_TIMEOUT_SECS,
            )
        except Exception:  # noqa: BLE001 — best-effort background task
            logger.debug("Folder suggestion: model call failed", exc_info=True)
            return None

    index = _parse_choice(text, len(folders))
    if index is None:
        # Log a SHAPE, never the reply itself. The dashboard log ring is streamed
        # over /api/ws, which an App Kit credential can subscribe to, so echoing
        # model output here would hand a prompt-injected string a path out. A
        # length and a bool still separate the cases worth telling apart —
        # "model said NONE" vs "model returned prose" vs "empty reply".
        stripped = text.strip()
        logger.debug(
            "Folder suggestion: no confident pick (reply len=%d, numeric_head=%s, none=%s)",
            len(stripped),
            bool(re.match(r"^#?\d", stripped)),
            stripped[:4].upper() == "NONE",
        )
        return None
    return folders[index]


async def maybe_suggest_folder(state: DashboardState, slot: _ChatSlot) -> None:
    """Offer one folder for an unfiled slot. Best-effort, at most once per slot.

    Fired from ``_maybe_auto_title`` once the slot has a title, because the title
    is the single best input to the decision — a distilled topic beats the raw
    transcript, and it is already computed by then. Every failure path is silent:
    the session simply stays where it is.
    """
    # Cheapest guards first; the disk scan and model call are behind all of them.
    if slot.folder_id or slot._folder_suggested:
        return
    # A temporary session is a blank slate the user means to discard — filing it
    # is not worth a prompt, and the grounding below reads other sessions' titles.
    if slot.blocks_reads:
        return
    folders = _eligible_folders(state)
    if not folders:
        return

    loop = asyncio.get_running_loop()
    try:
        cfg = await loop.run_in_executor(None, KiroCrewConfig.load)
    except Exception:  # noqa: BLE001 — no config, no suggestion
        logger.debug("Folder suggestion: config load failed", exc_info=True)
        return
    if not cfg.dashboard.folder_suggestions_enabled:
        return

    # Claim the one shot BEFORE the model call: the end-of-turn titling retry can
    # re-enter while this is still awaiting, and two cards for one slot is the
    # exact nagging this feature must not do. Not released on failure either —
    # a suggestion that could not be computed is simply not offered.
    slot._folder_suggested = True

    chosen = await _match_by_project_dir_off_loop(folders, slot.project)
    if chosen is None:
        chosen = await _pick_via_llm(state, slot, folders)
    if chosen is None:
        return
    # The slot may have been filed by hand, or reset, while the model was running.
    if slot.folder_id:
        return

    folder_id = str(chosen["id"])
    payload = {
        "slot": slot.key,
        "folder_id": folder_id,
        "folder_name": str(chosen.get("name") or ""),
        "breadcrumb": state.folder_breadcrumb(folder_id),
        "ts": time.time(),
    }
    # The folder's own ``icon`` emoji is deliberately NOT sent: the card renders a
    # lucide glyph instead, so an emoji here would be an unused field that reads
    # as a rendering option the UI does not actually have.
    # No redaction pass: unlike the title and folder-icon paths, nothing here is
    # model-generated. The model contributed an index; every string is stored
    # folder data that GET /api/chat/folders already returns verbatim to this
    # same client, so redacting here would diverge from it without adding cover.
    #
    # Owner sockets only — folder names are the user's own data and an App Kit
    # credential can open /api/ws (see deliver_ws_owners).
    try:
        delivered = await state.deliver_ws_owners("slot_folder_suggestion", payload)
    except Exception:  # noqa: BLE001 — a dropped card is not worth an error
        logger.debug("Folder suggestion: WS delivery failed", exc_info=True)
        return
    logger.info(
        "Folder suggestion for slot %s: %s (delivered to %d client(s))",
        slot.key,
        folder_id,
        delivered,
    )
