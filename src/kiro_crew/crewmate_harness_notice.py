"""The dashboard alarm that says Crew bots cannot follow the Settings harness.

A Crew bot with no pin of its own and no stored ``agent.member_acp_backend`` runs on
``agent.acp_backend`` (Settings -> Agent Harness). When that harness is one Crew bots
cannot run on, the loader runs them on KAS instead and logs why
(``config.loader_builders._note_member_fallback``, harness-parity H3). The log is for
the operator; this note is for the person, in the bell, and it opens the Agent
Harness panel, which shows the same fact beside the picker.

One Crew bot can also run on KAS on its own: its file sets something for its
workers the harness it inherits cannot carry (worker tool limits, ``hooks``,
``permissions``, ``toolsSettings``; ``crewmate_harness.worker_spec_forces_kas``). It gets its own note, which opens its
page on the Bots page.

Sent at gateway start and on every config change that touches either harness field,
once per fact per process: the bell is not refilled on each reload, and a fact that
stops holding and holds again is told again.

A person whose unpinned Bots left KAS because they now follow the Settings harness
(Claude Code, Codex) gets one more note, once per install (RFC
``rfc-crewmate-harness-follows-settings`` section 6): the first time a Bot of theirs
starts (:func:`deliver_moved`). It is claimed with an ``O_EXCL`` marker under the
data home before it goes out, as ``kiro_on_kas_notice`` does, so a restart or a
second gateway never repeats it.
"""

from __future__ import annotations

import asyncio
import logging
import os
import threading
import unicodedata
from typing import Any
from urllib.parse import quote

logger = logging.getLogger(__name__)

NOTE_KIND = "agent"
NOTE_CHANNEL = "system.agent"
NOTE_URL = "/settings/agent"
#: Where a Bot's own harness is set: the Agent harness row of its profile settings.
ROUTE = "Agent harness in its Profile → Settings on the Bots page."

#: The facts already announced in this process: ``settings:<harness>`` for every
#: Crew bot, ``mate:<crew>:<harness>`` for one.
_announced: set[str] = set()
_lock = threading.Lock()


def fallback_notes(config: Any) -> list[dict[str, str]]:
    """The notes owed now, each marked announced. Blocking.

    A fact that does not hold now is forgotten here, so it is told again when it
    holds again.
    """
    from kiro_crew.crewmate_harness import (
        SOURCE_FALLBACK,
        harness_label,
        inherited_falls_back,
        inherited_harness_source,
        refresh_worker_spec_verdicts,
        worker_spec_kas_detail,
    )

    # Off the loop: what each Crew bot's file needs is read into memory here, so the
    # event loop's readers find it from the first session on.
    refresh_worker_spec_verdicts(config)

    source = inherited_harness_source(config)
    owed: list[tuple[str, dict[str, str]]] = []
    holding: set[str] = set()
    if source.source == SOURCE_FALLBACK:
        chosen = harness_label(source.settings_harness)
        used = harness_label(source.harness)
        key = f"settings:{source.settings_harness}"
        holding.add(key)
        owed.append(
            (
                key,
                {
                    "title": f"Bots run on {used}, not {chosen}",
                    "body": (
                        f"Your Agent Harness is {chosen}, which bots cannot run on. Bot "
                        f"chats and the workers they open run on {used} instead. To choose "
                        "another harness for a bot, set " + ROUTE
                    ),
                    "url": NOTE_URL,
                },
            )
        )
    else:
        inherited = harness_label(source.harness)
        used = harness_label("kas")
        for crew in sorted(getattr(config, "agents", {}) or {}):
            if not getattr(config.agents[crew], "member_id", "") or not inherited_falls_back(
                config, crew
            ):
                continue
            kinds, items = worker_spec_kas_detail(config, crew)
            fields = ", ".join(kinds)
            fix = (
                f" To move it to {inherited}, turn these tools back on or remove these "
                f"entries in the bot's file or its Capabilities: {_items_line(items)}."
                if items
                else ""
            )
            key = f"mate:{crew}:{source.harness}"
            holding.add(key)
            owed.append(
                (
                    key,
                    {
                        "title": f"{crew} runs on {used}, not {inherited}",
                        "body": (
                            f"{crew} uses settings {inherited} cannot carry ({fields}), "
                            f"so its chat and workers run on {used}.{fix} To choose another "
                            "harness, set " + ROUTE
                        ),
                        "url": f"/members?member={quote(crew)}",
                    },
                )
            )
    owed.extend(_pin_fallback_notes(config, holding))
    with _lock:
        _announced.intersection_update(holding)
        notes = []
        for key, note in owed:
            if key in _announced:
                continue
            _announced.add(key)
            notes.append({**note, "key": key})
    return notes


#: How many exact entries the Bot note names before ``+N more``, and how long each may be.
MAX_ITEMS = 5
MAX_ITEM_CHARS = 60


def _code(name: str) -> str:
    """*name*, text from the Bot's own file, as a short inline code span.

    The bell's detail panel renders markdown, so control characters and backticks
    are dropped and the span is cut to :data:`MAX_ITEM_CHARS`: nothing in *name*
    can close the span or become a link or markup.
    """
    text = "".join(
        " " if ch.isspace() else ch
        for ch in name
        if ch.isspace() or (ch != "`" and not unicodedata.category(ch).startswith("C"))
    )
    text = " ".join(text.split()) or "?"
    if len(text) > MAX_ITEM_CHARS:
        text = text[: MAX_ITEM_CHARS - 1] + "…"
    return f"`{text}`"


def _items_line(items: tuple[tuple[str, str], ...]) -> str:
    """``turned off: `a`, `b`; hooks: `c` (+N more)`` for the first :data:`MAX_ITEMS`."""
    groups: dict[str, list[str]] = {}
    for group, name in items[:MAX_ITEMS]:
        groups.setdefault(group, []).append(_code(name))
    line = "; ".join(f"{group}: {', '.join(names)}" for group, names in groups.items())
    extra = len(items) - MAX_ITEMS
    return f"{line} (+{extra} more)" if extra > 0 else line


def _pin_fallback_notes(config: Any, holding: set[str]) -> list[tuple[str, dict[str, str]]]:
    """A note per Bot pinned to a harness Bots cannot run on; *holding* gains each key."""
    from kiro_crew.crewmate_harness import (
        CrewmateHarnessUnavailable,
        crewmate_pin_for,
        harness_label,
    )

    notes: list[tuple[str, dict[str, str]]] = []
    for crew in sorted(getattr(config, "agents", {}) or {}):
        pin = crewmate_pin_for(config, crew)
        if pin is None:
            continue
        try:
            checked = pin.checked()
        except CrewmateHarnessUnavailable:
            continue
        if not checked.harness_fallback:
            continue
        chosen = harness_label(pin.display_stored)
        used = harness_label(_stored(checked.backend))
        key = f"pin:{crew}:{pin.display_stored}:{_stored(checked.backend)}"
        holding.add(key)
        notes.append(
            (
                key,
                {
                    "title": f"{crew} runs on {used}, not {chosen}",
                    "body": (
                        f"{crew} is pinned to {chosen}, which bots cannot run on, so its "
                        f"chat and workers run on {used}. Pick another harness: set " + ROUTE
                    ),
                    "url": f"/members?member={quote(crew)}",
                    # An error: the person picked this harness, and it is not used.
                    "kind": "error",
                },
            )
        )
    return notes


def _stored(backend: str) -> str:
    from kiro_crew.acp_backends import backend_to_stored

    return backend_to_stored(backend)


def _forget(key: str) -> None:
    """Drop *key* from the announced set after a failed delivery."""
    with _lock:
        _announced.discard(key)


async def deliver(state: Any, config: Any | None = None) -> int:
    """Send every owed note to *state*'s bell. Returns how many went out.

    *config* is the config now in force; ``None`` loads it. Never raises: a missing
    alarm leaves the log line and the readouts, which say the same.
    """
    try:
        if config is None:
            from kiro_crew.config.loader import KiroCrewConfig

            config = await asyncio.to_thread(KiroCrewConfig.load)
        notes = await asyncio.to_thread(fallback_notes, config)
    except Exception:  # noqa: BLE001 - the alarm must not take a config apply down
        logger.warning("could not work out the Crew bot harness fallback notice", exc_info=True)
        return 0
    sent = 0
    for note in notes:
        try:
            state.notify(
                note.get("kind", NOTE_KIND),
                note["title"],
                note["body"],
                url=note["url"],
                channel=NOTE_CHANNEL,
            )
        except Exception:  # noqa: BLE001 - the alarm must not take a config apply down
            # Not delivered, so not announced: the next config change or start tries again.
            _forget(note["key"])
            logger.warning("could not deliver the Crew bot harness fallback notice", exc_info=True)
            continue
        sent += 1
    return sent


#: Claimed once per data home, right before the moved-Bots note is delivered.
MOVED_MARKER_FILENAME = "announced-bots-follow-settings-harness"


def moved_note_owed(config: Any, crew: str) -> bool:
    """Whether Bot *crew* left KAS only because it follows the Settings harness. Blocking.

    No pin, no stored ``agent.member_acp_backend``, and an inherited harness that
    is not KAS: the people whose Bots this change moved (RFC section 6).
    """
    from kiro_crew.acp_backends import ACP_BACKEND_KAS, backend_from_stored
    from kiro_crew.crewmate_harness import (
        SOURCE_SETTINGS,
        crewmate_inherited_source,
        crewmate_pin_for,
    )

    if crewmate_pin_for(config, crew) is not None:
        return False
    source = crewmate_inherited_source(config, crew)
    if backend_from_stored(source.harness) == ACP_BACKEND_KAS:
        # Still on KAS, where an unpinned Bot ran before: nothing moved.
        return False
    return source.source == SOURCE_SETTINGS


def _claim_moved() -> bool:
    """Create the moved-note marker exclusively. ``True`` only for the caller that made it."""
    from kiro_crew.config.loader import config_dir

    path = config_dir() / MOVED_MARKER_FILENAME
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    except FileExistsError:
        return False
    except OSError:
        logger.warning("could not claim the moved bots notice", exc_info=True)
        return False
    os.close(fd)
    return True


def moved_note(config: Any, crew: str) -> dict[str, str] | None:
    """The once-per-install moved-Bots note when owed and now claimed, else ``None``.

    Blocking; never raises.
    """
    from kiro_crew.crewmate_harness import crewmate_inherited_source, harness_label

    try:
        if not moved_note_owed(config, crew) or not _claim_moved():
            return None
        chosen = harness_label(crewmate_inherited_source(config, crew).harness)
    except Exception:  # noqa: BLE001 - no note is the safe direction
        logger.warning("could not work out the moved bots notice", exc_info=True)
        return None
    kas = harness_label("kas")
    return {
        "title": f"Bots now run on {chosen}",
        "body": (
            f"A bot with no harness of its own now follows Settings → Agent Harness "
            f"({chosen}), not {kas}. Its next turn starts a new conversation there. To "
            f"keep a bot on {kas}, set it as " + ROUTE
        ),
        "url": f"/members?member={quote(crew)}",
    }


async def deliver_moved(state: Any, config: Any, crew: str) -> bool:
    """Send the moved-Bots note to *state*'s bell when owed. Never raises."""
    note = await asyncio.to_thread(moved_note, config, crew)
    if note is None:
        return False
    try:
        state.notify(NOTE_KIND, note["title"], note["body"], url=note["url"], channel=NOTE_CHANNEL)
    except Exception:  # noqa: BLE001 - the claim is spent; the Settings line still says it
        logger.warning("could not deliver the moved bots notice", exc_info=True)
        return False
    return True


#: Strong reference to the boot delivery: the loop holds tasks weakly.
_TASK: "asyncio.Task[int] | None" = None


def schedule(state: Any) -> "asyncio.Task[int]":
    """Run :func:`deliver` once in the background, at gateway start. Call on the loop."""
    global _TASK
    _TASK = asyncio.create_task(deliver(state), name="crewmate-harness-fallback-notice")
    return _TASK
