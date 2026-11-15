"""The crewmate file an instance import writes for each imported Bot.

An archive never carries a crewmate file: one can hold grant keys
(``allowedTools``, ``autoApprove``, ``permissions``) and MCP server entries that
launch commands, which an import must not install without a person acting. The
import instead writes the minimal file (:func:`kiro_crew.mate_config.write_mate_config`)
for each imported Bot that runs as its own agent, and only where no file is at
the path: an existing file, this instance's or another's, is left byte-for-byte
unchanged. A file whose crewmate owner record names another data home is that
instance's: the part is ``not_written`` with ``foreign_owner``. Any other existing
file counts as the Bot's own (``kept``) only when the destination had the same Bot
before the import, when the file is the minimal file (no grants either way), or when
the owner record names this data home and the destination had no ``config.json``
before the import (:func:`_own_claim`); otherwise it belongs to another
Bot that had the name, and the part is ``not_written`` with ``held_by_other_bot``.
No archive byte is read here.

A Bot the exporting host listed in ``crewmate_files_customised`` had abilities
added to its file that this import does not carry. When this import wrote the
minimal file for it, its summary row carries the notice ``starting_abilities_only``
(:func:`crewmate_file_notices`); a file already here keeps what it has.
"""

from __future__ import annotations

import logging
import os
from collections.abc import Collection, Iterable, Sequence

from kiro_crew import mate_config
from kiro_crew.config import paths as paths_mod
from kiro_crew.portability_bot_types import (
    ImportedBot,
    PartOutcome,
    crewmate_file_state,
    safe_path_component,
)

logger = logging.getLogger(__name__)

PART = "crewmate_file"

#: The notice a Bot whose exported crewmate file had added abilities gets.
NOTICE_STARTING_ABILITIES_ONLY = "starting_abilities_only"

#: What makes an existing crewmate file a Bot's own (:func:`_own_claim`).
_OWNED, _MINIMAL, _FRESH_OWN = "owned", "minimal", "fresh_own"


def safe_crewmate_name(name: str) -> bool:
    """Whether *name* may name a crewmate file: one plain leaf inside ``mate_agents_dir()``.

    Refuses a name ``portability_bot_types.safe_path_component`` refuses (empty, a
    slash, backslash or NUL, a leading dot), and any name whose file path does not
    resolve directly inside the crewmate folder.
    """
    if not safe_path_component(name):
        return False
    try:
        folder = os.path.realpath(paths_mod.mate_agents_dir())
        target = os.path.realpath(mate_config.mate_config_path(name))
    except (OSError, ValueError):
        return False
    return os.path.dirname(target) == folder


def _restore_one(
    bot: ImportedBot,
    owned_slugs: Collection[str] | None,
    fresh_home: bool,
    minimal: set[str] | None = None,
) -> PartOutcome:
    def outcome(status: str, reason: str = "") -> PartOutcome:
        return PartOutcome(bot=bot.name, part=PART, status=status, reason=reason)

    if bot.kiro_agent != bot.name:
        return outcome("not_needed")
    if not safe_crewmate_name(bot.name):
        return outcome("not_written", "unsafe_name")
    if mate_config.user_agent_named(bot.name):
        return outcome("not_written", "user_agent_named")
    try:
        path = mate_config.write_mate_config(bot.name, only_if_absent=True)
    except mate_config.ForeignMateFileError:
        return outcome("not_written", "foreign_owner")
    except mate_config.MateConfigError as exc:
        logger.warning("import could not write the crewmate file of Bot %r: %s", bot.name, exc)
        return outcome("not_written", "write_failed")
    if path is not None:
        return outcome("written")
    if _owner_of(bot.name) == "foreign":
        return outcome("not_written", "foreign_owner")
    claim = _own_claim(bot, owned_slugs, fresh_home=fresh_home)
    if not claim:
        return outcome("not_written", "held_by_other_bot")
    if claim == _MINIMAL and minimal is not None:
        minimal.add(bot.name)
    return outcome("kept")


def _own_claim(bot: ImportedBot, owned_slugs: Collection[str] | None, *, fresh_home: bool) -> str:
    """Which rule makes an existing crewmate file at *bot*'s name *bot*'s own; ``""`` for none.

    Never when the crewmate owner record names ANOTHER data home
    (:func:`_owner_of`): that file is another instance's, whatever it holds.
    Otherwise :data:`_OWNED` when the destination had the same Bot before the import
    (its slug is in *owned_slugs*; ``None``: every slug); :data:`_MINIMAL` when the
    file is the minimal file :func:`mate_config.write_mate_config` writes for that name
    (``portability_bot_types.crewmate_file_state``: it grants nothing, whoever wrote
    it); :data:`_FRESH_OWN` when the destination had no ``config.json`` before the
    import (*fresh_home*) and the owner record names this data home: a backup restored
    into a fresh data home at the same path, on a host whose Kiro home kept the file.
    A file with additions that this data home wrote for a Bot its roster does not hold
    is not the imported Bot's: it carries that other Bot's grants.
    """
    owner = _owner_of(bot.name)
    if owner == "foreign":
        return ""
    if owned_slugs is None or bot.slug in owned_slugs:
        return _OWNED
    if crewmate_file_state(bot.name) == "minimal":
        return _MINIMAL
    return _FRESH_OWN if fresh_home and owner == "own" else ""


def _owner_of(name: str) -> str:
    """``own``, ``foreign`` or ``none`` by the crewmate owner record; ``unreadable``
    when the record exists but cannot be read."""
    try:
        if mate_config.crewmate_file_foreign(name):
            return "foreign"
        return "own" if mate_config.crewmate_file_on_record(name) else "none"
    except mate_config.MateConfigError:
        return "unreadable"


def crewmate_name_held(name: str, *, fresh_home: bool = False) -> str:
    """Why an added Bot named *name* would resolve to a file this import did not write.

    ``user_agent_named`` when a user-level agent declares *name*, else
    :func:`crewmate_file_held`. ``""`` when neither holds the name. The caller has
    established that no Bot of *name* is in the roster. A Merge adds a Bot as running
    as its own agent (``kiro_agent`` is its name), so with either one here it would
    run with grants the import never wrote.
    """
    if mate_config.user_agent_named(name):
        return "user_agent_named"
    return crewmate_file_held(name, fresh_home=fresh_home)


def crewmate_file_held(name: str, *, fresh_home: bool = False) -> str:
    """Why the crewmate file at *name*'s path (a link included) is not an added Bot's.

    ``foreign_owner`` when the owner record names another data home (that file is
    never this Bot's and never free); ``held_by_other_bot`` when it is not the added
    Bot's by the rule of :func:`_own_claim` for a Bot the destination did
    not have (it is not the minimal file, and not *fresh_home* with the record naming
    this data home), or when the owner record cannot be read, so whose file it is is
    unknown. ``""`` when no entry is at the path or it is free.
    """
    if not os.path.lexists(mate_config.mate_config_path(name)):
        return ""
    owner = _owner_of(name)
    if owner == "foreign":
        return "foreign_owner"
    if owner == "unreadable":
        return "held_by_other_bot"
    if crewmate_file_state(name) == "minimal" or (fresh_home and owner == "own"):
        return ""
    return "held_by_other_bot"


def restore_crewmate_files(
    bots: Sequence[ImportedBot],
    *,
    owned_slugs: Collection[str] | None = None,
    fresh_home: bool = False,
    minimal: set[str] | None = None,
) -> list[PartOutcome]:
    """Write the minimal crewmate file of each imported Bot that has none; one outcome per Bot.

    ``not_needed`` for a Bot whose ``kiro_agent`` is not its own name; ``written``
    when this call created the file; ``kept`` when a file was already there and it
    is the Bot's own (:func:`_own_claim`; *fresh_home* says the destination
    had no ``config.json`` before the import), ``not_written`` with
    ``held_by_other_bot`` when one was there and it is not; ``not_written`` with
    ``unsafe_name``, ``user_agent_named``, ``foreign_owner`` or
    ``write_failed`` otherwise. Never raises for one Bot: an
    unexpected error is that Bot's ``write_failed``. What the archive says about a
    customised file changes nothing written here; :func:`crewmate_file_notices`
    reports it. *minimal*, when given, receives the name of each Bot the destination
    did not have whose ``kept`` file this step judged to be the minimal file, so the
    caller reads that classification instead of the file again.
    """
    outcomes: list[PartOutcome] = []
    for bot in bots:
        try:
            outcomes.append(_restore_one(bot, owned_slugs, fresh_home, minimal))
        except Exception:  # noqa: BLE001 - one Bot's failure is reported, never raised
            logger.exception("import crewmate file step failed for Bot %r", bot.name)
            outcomes.append(
                PartOutcome(bot=bot.name, part=PART, status="not_written", reason="write_failed")
            )
    return outcomes


def crewmate_file_notices(
    bots: Sequence[ImportedBot], customised: Iterable[str], *, written: Collection[str]
) -> dict[str, list[str]]:
    """The crewmate-file notices per imported Bot name: ``starting_abilities_only``
    for each Bot the archive lists in ``crewmate_files_customised`` whose minimal file
    this import wrote (its name is in *written*).

    A Bot whose file was already here lost nothing to the import, so it gets no
    notice. Archive names that match no imported Bot are ignored, and a Bot with no
    notice is absent from the map.
    """
    listed = {name for name in customised if isinstance(name, str)}
    return {
        bot.name: [NOTICE_STARTING_ABILITIES_ONLY]
        for bot in bots
        if bot.name in listed and bot.name in written
    }
