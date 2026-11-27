"""Three ``deniedCommands`` entries sent to KAS as kiro-cli sends them.

kiro-cli's own upgrade of a legacy agent file turns each ``deniedCommands`` regex
into shell globs. For three entries its globs are not what the regex names, so
the KAS translation did not copy it: ``rm \\| .*`` travelled as ``rm | *``, and
``rm [|] .*`` and the ``shred +x`` branch of ``rm .*|shred +x`` had no rule and
were named at session start. With the ``kas_subagents`` preview on, each of those
entries sends the globs kiro-cli 3.5.0 wrote for it (:data:`CLI_WRITTEN_GLOBS`),
which refuse commands the entry does not name; the first refuses every command.

Only an entry whose text is one of the three is sent this way: kiro-cli's
conversion is not published, so no other entry is guessed at. The key stays named
as not carried exactly, and the session-start notice says what each entry refuses
(:func:`sent_as_cli_sentence`). Kiro Crew's own gate reads none of this: it counts
such an entry as not carried, as before, so it never refuses less.

The preview is read each time a spec is translated, through the seam every other
reader uses; a config that cannot be read keeps the translation of before.
"""

from __future__ import annotations

import json
import logging
from typing import Any

logger = logging.getLogger(__name__)

#: The entry as written (outer spaces off) -> the shell deny globs kiro-cli 3.5.0
#: wrote into the agent file it upgraded.
CLI_WRITTEN_GLOBS: dict[str, tuple[str, ...]] = {
    "rm \\| .*": ("*",),
    "rm [|] .*": ("rm * *",),
    "rm .*|shred +x": ("rm *", "shred *x"),
}


def _preview_on() -> bool:
    """Whether the ``kas_subagents`` preview is on. Off when the config cannot be read."""
    try:
        # Call-time: both sit above the permissions translation that imports this.
        from kiro_crew.acp.kas_engine_subagents import kas_subagents_preview_on
        from kiro_crew.config.loader import KiroCrewConfig

        return kas_subagents_preview_on(KiroCrewConfig.load())
    except Exception:  # noqa: BLE001 - an unreadable config keeps the translation of before
        logger.debug("the kas_subagents preview unreadable for a deny entry", exc_info=True)
        return False


def cli_written_globs(entry: object) -> tuple[str, ...] | None:
    """The globs kiro-cli wrote for the ``deniedCommands`` *entry*, or ``None``.

    ``None`` for every entry but the three, and for those while the preview is off
    or never set. The config is read only for one of the three.
    """
    globs = CLI_WRITTEN_GLOBS.get(entry.strip()) if isinstance(entry, str) else None
    return globs if globs is not None and _preview_on() else None


def take_cli_written(entries: list[str]) -> tuple[list[str], list[str]]:
    """*entries* split into the globs kiro-cli wrote for those it is followed on, and the rest."""
    written: list[str] = []
    rest: list[str] = []
    for entry in entries:
        globs = cli_written_globs(entry)
        if globs is None:
            rest.append(entry)
        else:
            written.extend(globs)
    return written, rest


#: The field a session-start deny record carries the written globs under
#: (``kas_agents.spec_keys_without_carrier``).
SENT_AS_FIELD = "sentAs"

_RECORD_HEAD = '{"deniedCommand":'


def with_sent_as(item: dict[str, Any]) -> dict[str, Any]:
    """The deny record *item* also carrying the globs its entry is sent as, when it is."""
    globs = cli_written_globs(item.get("entry"))
    return {**item, SENT_AS_FIELD: list(globs)} if globs else item


def sent_as_cli_sentence(
    agent: str, key: str, entry: str, globs: tuple[str, ...], worker: bool = False
) -> str:
    """The session-start sentence for one entry sent as kiro-cli sends it.

    For a Bot's *worker* it also says what Kiro Crew's gate still does: the entry
    is one nobody can check, so the worker's shell is held as before.
    """
    if "*" in globs:
        # A deny on every command: kiro-cli 3.5.0's engine drops the shell tool too.
        refused = "so every shell command is refused and the agent may have no shell tool"
    else:
        refused = "so every shell command matching " + " or ".join(globs) + " is refused"
    held = (
        " Kiro Crew cannot check the entry itself, so this Bot's worker also refuses "
        "every shell command that is not read-only."
        if worker
        else ""
    )
    return (
        f"Agent {agent} sets {key}: {entry}, which this backend receives as kiro-cli "
        f"sends it, {refused}. That is more than the entry names.{held}"
    )


def split_sent_as_cli(agent: str, keys: list[str], worker: bool) -> tuple[list[str], list[str]]:
    """The sentences for the deny records of *keys* sent as kiro-cli sends them, and the other keys."""
    sentences: list[str] = []
    rest: list[str] = []
    for key in keys:
        item = json.loads(key)["deniedCommand"] if key.startswith(_RECORD_HEAD) else {}
        globs = item.get(SENT_AS_FIELD)
        if globs:
            sentences.append(
                sent_as_cli_sentence(agent, item["key"], item["entry"], tuple(globs), worker)
            )
        else:
            rest.append(key)
    return sentences, rest


__all__ = [
    "CLI_WRITTEN_GLOBS",
    "SENT_AS_FIELD",
    "cli_written_globs",
    "sent_as_cli_sentence",
    "split_sent_as_cli",
    "take_cli_written",
    "with_sent_as",
]
