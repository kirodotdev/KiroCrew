"""What a crewmate may change on its own record, and how the change is applied.

The self-editable fields and their bounds, the cleaning of a display name, a description
and an avatar, the update with its undo, and the rename.

:mod:`kiro_crew.members` stays the import path and the patch surface; see
:mod:`kiro_crew.members_parts`.
"""

from __future__ import annotations

import logging
from collections.abc import Mapping

from kiro_crew.external_text import external_text_requires_redaction
from kiro_crew.members_parts.member_space import (
    MemberNameError,
    MemberNameTaken,
    check_crew_label,
    member_slug,
    validate_member_name,
)

# Logs under the facade's name, which operators and tests filter on.
logger = logging.getLogger("kiro_crew.members")


#: Fields of its own record a crewmate may change directly from its own chat
#: (``update_self``): the presentation it owns. Everything else on the record is
#: either the user's to grant through a change card (goal, model, approval mode,
#: tools) or never changes at all (memory store, member id, key, template).
SELF_EDITABLE_FIELDS = frozenset({"display_name", "description", "avatar"})

#: Cap on a crewmate's description, the same bound the crew editor's text
#: fields and the change-card text parameters are held to.
MEMBER_DESCRIPTION_MAX_CHARS = 4000

#: The plain Kiro mark, trait by trait: what a face a crewmate pins for itself
#: starts from when it had no pinned traits before (a name-derived face). A trait
#: the crewmate leaves out keeps this value, so a partial pick never draws a
#: face with an axis missing (the renderer draws an unknown or empty option as
#: nothing at all). ``tile`` is the brand purple the renderer falls back to.
DEFAULT_GHOST_TRAITS: Mapping[str, object] = {
    "eyes": "canon",
    "brows": "none",
    "mouth": "none",
    "accessory": "none",
    "prop": "none",
    "blush": False,
    "flip": False,
    "tile": "#9046ff",
}


class SelfUpdateError(ValueError):
    """Why :func:`update_member_self` refused: ``code`` is machine-readable."""

    def __init__(self, code: str, message: str) -> None:
        super().__init__(message)
        self.code = code


# Named by the path callers import it from: a traceback or a log line renders the
# raised class as ``module.qualname``, so the owner module must not leak into it.
SelfUpdateError.__module__ = "kiro_crew.members"


#: Kept as a name for the rename case: every rename refusal is a self-update
#: refusal, and the codes (``not_a_crewmate``, ``name_taken``, ``invalid_name``,
#: ``empty_name``) are unchanged.
SelfRenameError = SelfUpdateError


def _strip_controls(raw: str, *, keep_newlines: bool) -> str:
    """*raw* with control and format characters removed.

    Whitespace controls separate words (a tab, a line break); with
    *keep_newlines* a line break survives as ``\\n`` so a multi-line description
    keeps its paragraphs. Every other control or format character is dropped
    outright, so a bidi override or a zero-width joiner never reaches a label.
    """
    import unicodedata

    out: list[str] = []
    for ch in raw:
        if ch == "\n" and keep_newlines:
            out.append("\n")
        elif ch.isspace():
            out.append(" ")
        elif not unicodedata.category(ch).startswith("C"):
            out.append(ch)
    return "".join(out)


def clean_display_name(raw: object) -> str:
    """*raw* with control and format characters removed and spaces collapsed.

    Raises :class:`SelfUpdateError` when nothing usable is left or the result
    is not a valid member display name (:func:`validate_member_name`, whose
    length cap is :data:`MEMBER_NAME_MAX_CHARS`).
    """
    if not isinstance(raw, str):
        raise SelfUpdateError("invalid_name", "name must be a string")
    name = " ".join(_strip_controls(raw, keep_newlines=False).split())
    if not name:
        raise SelfUpdateError("empty_name", "name is empty")
    try:
        return validate_member_name(name)
    except MemberNameError as exc:
        raise SelfUpdateError("invalid_name", str(exc)) from None


def clean_description(raw: object) -> str:
    """*raw* as a crewmate description: controls dropped, blank runs collapsed.

    ``""`` is a real value (no description). Raises :class:`SelfUpdateError`
    for a non-string, a text over :data:`MEMBER_DESCRIPTION_MAX_CHARS`, or a
    credential-shaped text: the description is rendered on every roster surface
    and in the crewmate's own prompt.
    """
    if not isinstance(raw, str):
        raise SelfUpdateError("invalid_description", "description must be a string")
    lines = [
        " ".join(line.split()) for line in _strip_controls(raw, keep_newlines=True).split("\n")
    ]
    text = "\n".join(lines).strip()
    while "\n\n\n" in text:
        text = text.replace("\n\n\n", "\n\n")
    if len(text) > MEMBER_DESCRIPTION_MAX_CHARS:
        raise SelfUpdateError(
            "invalid_description",
            f"description must be at most {MEMBER_DESCRIPTION_MAX_CHARS} characters",
        )
    if text and external_text_requires_redaction(text):
        raise SelfUpdateError("invalid_description", "description must not be credential-shaped")
    return text


def clean_self_avatar(raw: object, current: Mapping[str, object]) -> dict:
    """The ghost face a crewmate asked for, merged onto its current record.

    *raw* is ``{"traits": {...}}`` (``kind`` may be given and must be
    ``"ghost"``), naming one or more axes. The axes it leaves out keep the
    face's current pinned value, or :data:`DEFAULT_GHOST_TRAITS` when the
    crewmate wore the name-derived face until now, so a partial pick is always
    a whole face. The result passes the same validator the crew editor's save
    does (:func:`kiro_crew.config.sections._safe_avatar`), so a junk value is
    dropped there and a face with no usable trait is refused here rather than
    stored empty. The reactions the current ghost record carries (``motions``,
    ``sounds``, ``expressions``) ride along unchanged: a crewmate changing its
    face does not lose the cues the user chose for it.

    A crewmate wearing an uploaded picture or an appearance pack is refused
    (``avatar_not_ghost``): those faces have files and library entries behind
    them that only the crew editor manages.
    """
    from kiro_crew.config.sections import _safe_avatar

    if not isinstance(raw, Mapping):
        raise SelfUpdateError("invalid_avatar", "avatar must be an object with traits")
    kind = raw.get("kind", "ghost")
    if kind != "ghost":
        raise SelfUpdateError("invalid_avatar", "only a ghost face can be chosen here")
    unknown = sorted(set(raw) - {"kind", "traits"})
    if unknown:
        raise SelfUpdateError("invalid_avatar", f"unknown avatar field '{unknown[0][:32]}'")
    traits = raw.get("traits")
    if not isinstance(traits, Mapping) or not traits:
        raise SelfUpdateError("invalid_avatar", "avatar needs a traits object")
    current_kind = current.get("kind") if isinstance(current, Mapping) else None
    if current_kind in ("image", "pack"):
        raise SelfUpdateError(
            "avatar_not_ghost",
            "this mate wears a picture or an appearance pack; that face is changed in "
            "the crew editor, not from the chat",
        )
    unknown_axes = sorted(set(traits) - set(DEFAULT_GHOST_TRAITS))
    if unknown_axes:
        raise SelfUpdateError("invalid_avatar", f"unknown trait '{str(unknown_axes[0])[:32]}'")
    pinned = current.get("traits") if isinstance(current, Mapping) else None
    base = dict(pinned) if isinstance(pinned, Mapping) and pinned else dict(DEFAULT_GHOST_TRAITS)
    validated = _safe_avatar({"kind": "ghost", "traits": {**base, **dict(traits)}})
    if validated.get("kind") != "ghost" or not validated.get("traits"):
        raise SelfUpdateError("invalid_avatar", "no usable trait in the avatar")
    merged: dict = {"kind": "ghost", "traits": validated["traits"]}
    for key in ("motions", "sounds", "expressions"):
        kept = current.get(key) if isinstance(current, Mapping) else None
        if isinstance(kept, Mapping) and kept:
            merged[key] = dict(kept)
    return merged


def _self_target(member: str, expected_store: str):
    """The config and the crewmate record a self-update may write, or raise.

    The reserved ``default`` member and the configured default crew are refused,
    as is a member that is not a configured crewmate. *expected_store*, when
    given, is the memory store the caller's thread was opened on: a row under
    the same key with any other store (a crewmate deleted and created again
    while the old thread still ran) is refused.
    """
    from kiro_crew.config.loader import KiroCrewConfig, _invalidate_config_cache

    _invalidate_config_cache()
    cfg = KiroCrewConfig.load()
    agent = cfg.agents.get(member)
    default_crew = getattr(cfg, "default_agent", "") or ""
    if agent is None or member == "default" or member == default_crew:
        raise SelfUpdateError("not_a_crewmate", "only a mate can change its own settings")
    if expected_store and agent.memory_store != expected_store:
        raise SelfUpdateError("not_a_crewmate", "this chat belongs to a mate that was removed")
    return cfg, agent


def _write_self_fields(
    cfg,
    member: str,
    agent,
    values: dict,
    *,
    expected_store: str,
    expected_values: Mapping[str, object] | None = None,
) -> list[str]:
    """Set *values* on *agent*, publish the changed ones, log them. Returns the changed keys.

    *expected_values* is handed to the persist as its compare-and-set: the
    write is refused inside the config lock when any named field does not still
    hold that value on disk (:class:`memory_stores.MemberFieldConflict`).
    """
    from kiro_crew.memory_stores import persist_member_config

    changed = [k for k, v in values.items() if getattr(agent, k) != v]
    if not changed:
        return []
    for key in changed:
        setattr(agent, key, values[key])
    persist_member_config(
        cfg,
        member,
        expected_store=expected_store or agent.memory_store,
        changed_fields=set(changed),
        expected_values=expected_values,
    )
    try:
        from kiro_crew import eventlog_hooks
        from kiro_crew.eventlog.types import MEMBER_CONFIG

        eventlog_hooks.emit(
            member_slug(member, cfg),
            member,
            MEMBER_CONFIG,
            {**{k: values[k] for k in changed}, "changed": changed},
        )
    except Exception:
        logger.debug("member/config event-log hook failed", exc_info=True)
    return changed


def update_member_self(
    member: str, fields: Mapping[str, object], *, expected_store: str = ""
) -> dict:
    """Apply a crewmate's own change to its record. Blocking IO.

    *fields* names one or more of :data:`SELF_EDITABLE_FIELDS` with the raw
    value the crewmate passed; each is cleaned by its own rule
    (:func:`clean_display_name`, :func:`clean_description`,
    :func:`clean_self_avatar`) and refused with a :class:`SelfUpdateError`
    whose ``code`` the tool can say. Only the label, text and face change: the
    key, slug, thread and memory stay. The change is recorded in the member's
    event log (``member/config``), whose roster projection is what the dashboard
    folds into the open chat, the roster and the switcher.

    Returns ``{"member", "name", "changed": [...], "before": {...}, "after": {...}}``.
    ``name`` is the display name the crewmate goes by after the change (its key
    when it has none). ``after`` holds every field that was asked for as it is now stored;
    ``before`` holds ONLY the fields in ``changed``, as they were -- the shape
    :func:`undo_member_self` takes back, so an undo never touches a field this
    change left as it found it (a later rename survives undoing an earlier
    description change).
    """
    if not isinstance(fields, Mapping) or not fields:
        raise SelfUpdateError("no_fields", "nothing to change")
    unknown = sorted(set(fields) - SELF_EDITABLE_FIELDS)
    if unknown:
        raise SelfUpdateError(
            "unknown_field", f"'{str(unknown[0])[:32]}' is not something a mate changes itself"
        )
    cfg, agent = _self_target(member, expected_store)
    values: dict = {}
    if "display_name" in fields:
        name = clean_display_name(fields["display_name"])
        try:
            check_crew_label(member, name, cfg.agents)
        except MemberNameTaken:
            raise SelfUpdateError("name_taken", "another mate already goes by that name") from None
        except MemberNameError as exc:
            raise SelfUpdateError("invalid_name", str(exc)) from None
        values["display_name"] = name
    if "description" in fields:
        values["description"] = clean_description(fields["description"])
    if "avatar" in fields:
        values["avatar"] = clean_self_avatar(fields["avatar"], agent.avatar or {})
    was = {k: _copy_value(getattr(agent, k)) for k in values}
    from kiro_crew.memory_stores import MemberFieldConflict

    try:
        # ``was`` is what the undo will put back, so it must be what was on disk
        # at the moment of the write: the persist compares the fields inside the
        # config lock and refuses when a crew-editor or CLI save landed between
        # this snapshot and the write. The crewmate is told to try again.
        changed = _write_self_fields(
            cfg, member, agent, values, expected_store=expected_store, expected_values=was
        )
    except MemberFieldConflict as exc:
        raise SelfUpdateError(
            "changed_meanwhile",
            f"the mate's {exc.field} was changed by someone else just now; try again",
        ) from None
    after = {k: _copy_value(getattr(agent, k)) for k in values}
    before = {k: was[k] for k in changed}
    return {
        "member": member,
        "name": agent.display_name or member,
        "changed": changed,
        "before": before,
        "after": after,
    }


def undo_member_self(
    member: str,
    before: Mapping[str, object],
    *,
    after: Mapping[str, object],
    expected_store: str = "",
) -> dict:
    """Put the fields in *before* back as they were. Blocking IO.

    *before* and *after* are the maps a :func:`update_member_self` result
    carried, held by the gateway (never read back from the transcript). An undo
    takes back THAT change and nothing newer: a field whose stored value differs
    from the recorded ``after`` was edited again since (by the crewmate, the
    crew editor or the CLI), and the whole undo is refused (``stale_undo``) so an
    old row's button cannot overwrite the newer edit. The check is made twice: on
    the loaded snapshot, to answer quickly, and again inside the config lock by
    the persist (``expected_values``), so a save that commits between the two is
    never overwritten either. The ``before`` values were the record's own, so
    they are written back as given, with only the uniqueness check a display
    name always gets: another crewmate may have taken the old name in between,
    and then the undo is refused (``name_taken``).
    """
    if not isinstance(before, Mapping) or not before:
        raise SelfUpdateError("no_fields", "nothing to undo")
    unknown = sorted(set(before) - SELF_EDITABLE_FIELDS)
    if unknown:
        raise SelfUpdateError("unknown_field", f"'{str(unknown[0])[:32]}' cannot be undone")
    cfg, agent = _self_target(member, expected_store)
    for key in before:
        if _copy_value(getattr(agent, key)) != after.get(key):
            raise SelfUpdateError(
                "stale_undo", f"the mate's {key} was changed again since; nothing was undone"
            )
    values: dict = {}
    for key, value in before.items():
        if key == "display_name":
            if not isinstance(value, str):
                raise SelfUpdateError("invalid_name", "name must be a string")
            try:
                check_crew_label(member, value, cfg.agents)
            except MemberNameTaken:
                raise SelfUpdateError(
                    "name_taken", "another mate now goes by the old name"
                ) from None
            except MemberNameError as exc:
                raise SelfUpdateError("invalid_name", str(exc)) from None
            values[key] = value
        elif key == "description":
            if not isinstance(value, str):
                raise SelfUpdateError("invalid_description", "description must be a string")
            values[key] = value
        else:
            values[key] = dict(value) if isinstance(value, Mapping) else {}
    from kiro_crew.memory_stores import MemberFieldConflict

    try:
        changed = _write_self_fields(
            cfg,
            member,
            agent,
            values,
            expected_store=expected_store,
            expected_values={k: after.get(k) for k in before},
        )
    except MemberFieldConflict as exc:
        raise SelfUpdateError(
            "stale_undo", f"the mate's {exc.field} was changed again since; nothing was undone"
        ) from None
    return {
        "member": member,
        "changed": changed,
        "after": {k: _copy_value(getattr(agent, k)) for k in values},
    }


def _copy_value(value: object) -> object:
    return dict(value) if isinstance(value, Mapping) else value


def rename_member_display(member: str, raw_name: object, *, expected_store: str = "") -> str:
    """Set crewmate *member*'s display name to *raw_name*, cleaned. Blocking IO.

    The rename case of :func:`update_member_self`: same rules, same record,
    same event. Returns the name that was saved.
    """
    result = update_member_self(member, {"display_name": raw_name}, expected_store=expected_store)
    return str(result["after"]["display_name"])
