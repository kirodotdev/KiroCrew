"""DM-thread bindings of the Bots an instance import installed, rebuilt from config.

A binding attributes a member-thread transcript to one Bot. Every input it holds is
derivable from the destination config (the Bot's name, slug, ``member_id`` and the
slot generation of its memory store), so an import never copies one from the archive:
it regenerates the binding through :func:`members_parts.dm_bindings.write_dm_binding`
for a Bot whose transcript this import installed, and only when the identity is
unambiguous on the destination. A binding file the archive carries is untrusted
input: it is read, bounded and without following a link, only so that one which
disagrees with the derived values can VETO the rebuild. It can never authorise one.

:func:`orphan_transcripts` lists the archive's member-thread transcripts that match
no imported Bot's slot, which the import withholds instead of installing where no
Bot could open them.

The orchestration (``portability_bots``) calls every function here; each answers
per Bot and never raises for one Bot's failure.
"""

from __future__ import annotations

import json
import logging
import os
import stat
from collections.abc import Collection, Iterable, Mapping
from pathlib import Path, PurePath
from types import ModuleType
from typing import Any

from kiro_crew import members as members_mod
from kiro_crew.atomic_write import read_bytes_with_retry
from kiro_crew.history import SESSIONS_DIR_NAME
from kiro_crew.history_files import transcript_stem
from kiro_crew.member_records import MEMBER_RECORDS_LEAF
from kiro_crew.portability_bot_types import (
    LOAD_CONFIG,
    BoundedReadError,
    ImportedBot,
    PartOutcome,
    read_fd_at_most,
)

logger = logging.getLogger(__name__)

#: Largest archive binding file read as a claim; a bigger one reads as ``None``.
MAX_CLAIM_BYTES = 4 * 1024

#: Most archive binding files examined; the rest are not read at all. A claim only
#: vetoes, so one left unread can never make a rebuild happen that would not.
MAX_CLAIM_FILES = 1000

_PART = "binding"
_CLAIM_SUFFIX = ".json"
_CLAIM_KEYS = ("member_id", "member", "slug", "slot_key", "memory_store")


def _pf() -> ModuleType:
    """The portability facade, read at call time: its owners are reached through it.

    Its extraction listing, pinned open and stem rules are reused here. Imported on
    use: that module drives the import and imports this one.
    """
    from kiro_crew import portability

    return portability


def member_stem(slot_key: str) -> str:
    """The transcript stem of the member slot *slot_key*: ``dashboard_<slot key>``.

    The spelling the history layer writes for the slot's canonical session alias
    (``dashboard:<slot key>``), and the one ``portability_archive._chat_stem``
    reads back from an archive entry name.
    """
    return transcript_stem(f"dashboard:{slot_key}")


def is_member_stem(stem: str) -> bool:
    """Whether the dashboard transcript stem *stem* is a member thread's."""
    return stem.startswith(_pf()._DASHBOARD_STEM_PREFIX + members_mod.DM_SLOT_KEY_PREFIX)


def _read_claim(snap: Path, name: str) -> dict | None:
    """One archive binding file as a JSON object, or ``None`` when it is not usable.

    Opened through the import's pinned extraction open (every component below the
    private extraction directory pinned, the leaf no-follow and required to be a
    regular file with one link), then read through that descriptor and no further
    than one byte past :data:`MAX_CLAIM_BYTES`.
    """
    rel = PurePath(snap.name, MEMBER_RECORDS_LEAF, members_mod.DM_BINDINGS_DIR_NAME, name)
    with _pf()._open_import_session_file(snap.parent, rel) as fd:
        if fd is None:
            return None
        try:
            st = os.fstat(fd)
            if not stat.S_ISREG(st.st_mode) or st.st_size > MAX_CLAIM_BYTES:
                return None
            raw = read_fd_at_most(fd, MAX_CLAIM_BYTES)
        except (OSError, BoundedReadError):
            return None
    try:
        data = json.loads(raw.decode("utf-8"))
    except (UnicodeError, ValueError):
        return None
    return data if isinstance(data, dict) else None


def read_archive_binding_claims(snap: Path) -> dict[str, dict | None]:
    """The binding files the archive carries, by slug: the parsed object, or ``None``.

    *snap* is the archive's top directory inside the import's private extraction
    directory. Only ``member-records/member-bindings/<slug>.json`` entries whose
    stem is a valid slug are claims; at most :data:`MAX_CLAIM_FILES` of them are
    examined, in name order. A link, a non-regular file, a file over
    :data:`MAX_CLAIM_BYTES`, bytes that are not UTF-8 JSON, or JSON that is not an
    object reads as ``None``: present, so it vetoes, but never read as content.
    Nothing here raises for an archive's contents.
    """
    fs = _pf()
    if not fs._real_dir(fs._listing(snap).get(MEMBER_RECORDS_LEAF)):
        return {}
    records = snap / MEMBER_RECORDS_LEAF
    if not fs._real_dir(fs._listing(records).get(members_mod.DM_BINDINGS_DIR_NAME)):
        return {}
    claims: dict[str, dict | None] = {}
    examined = 0
    for name in sorted(fs._listing(records / members_mod.DM_BINDINGS_DIR_NAME)):
        if not name.endswith(_CLAIM_SUFFIX):
            continue
        slug = name[: -len(_CLAIM_SUFFIX)]
        try:
            members_mod.validate_slug(slug)
        except members_mod.MemberSlugError:
            continue
        if examined >= MAX_CLAIM_FILES:
            break
        examined += 1
        try:
            claims[slug] = _read_claim(snap, name)
        except Exception:  # an archive's bytes must never fail the import
            logger.debug("archive binding claim %s unreadable", name, exc_info=True)
            claims[slug] = None
    return claims


def _destination_transcript_exists(mc: Path, stem: str) -> bool:
    try:
        return os.path.lexists(mc / SESSIONS_DIR_NAME / f"{stem}.jsonl")
    except (OSError, ValueError):
        return True


def _names_for_slug(config: Any, slug: str) -> list[str]:
    from kiro_crew.dashboard.handlers.members_access import _member_names_for_slug

    return _member_names_for_slug(config, slug)


def _derived_slot(config: Any, bot: ImportedBot) -> tuple[str, str]:
    """``(slot key, generation)`` the thread endpoint derives for *bot* from *config*."""
    from kiro_crew.dashboard.handlers.members_thread import _member_thread_slot

    return _member_thread_slot(config, bot.name, bot.slug)


def _remove_if_unchanged(path: Path, written: dict) -> None:
    """Remove the binding this rebuild wrote when the file still holds exactly it."""
    expected = json.dumps(written, ensure_ascii=False).encode("utf-8")
    try:
        if read_bytes_with_retry(path, max_bytes=len(expected) + 1) == expected:
            path.unlink()
    except OSError:
        logger.debug("could not remove unverified binding %s", path, exc_info=True)


def identity_refusal(config: Any, bot: ImportedBot) -> str:
    """Why *config* gives *bot* no unambiguous identity for a binding; ``""`` when it does.

    (a) its ``member_id`` must be non-empty (``no_member_id``), equal its slug
    (``slug_collision``) and resolve uniquely to it through
    ``execution_context.member_config_for_id`` (``member_id_ambiguous``); (b) its slug
    must be no other addressable name's (``slug_collision``).
    """
    if not bot.member_id:
        return "no_member_id"
    if bot.member_id != bot.slug:
        return "slug_collision"
    from kiro_crew.execution_context import member_config_for_id

    try:
        alias, _ = member_config_for_id(config, bot.member_id)
    except ValueError:
        return "member_id_ambiguous"
    if alias != bot.name:
        return "member_id_ambiguous"
    if _names_for_slug(config, bot.slug) != [bot.name]:
        return "slug_collision"
    return ""


def sessions_listing(snap: Path) -> dict[str, os.DirEntry]:
    """The archive's ``sessions/`` entries by name; empty when it has no real ``sessions/`` tree.

    Taken once per import (``portability_bots.PreImport.sessions``) and shared by every
    step that reads the archive's chats, so they all judge the same entries.
    """
    fs = _pf()
    if not fs._real_dir(fs._listing(snap).get(SESSIONS_DIR_NAME)):
        return {}
    return dict(fs._listing(snap / SESSIONS_DIR_NAME))


def archive_member_stems(
    snap: Path, *, listing: dict[str, os.DirEntry] | None = None
) -> frozenset[str]:
    """The member-thread transcript stems the archive's ``sessions/`` tree carries.

    *listing* is :func:`sessions_listing` of *snap*, taken here when ``None``.
    """
    fs = _pf()
    entries = sessions_listing(snap) if listing is None else listing
    stems: set[str] = set()
    for name, entry in entries.items():
        stem = fs._chat_stem(name)
        if stem is not None and is_member_stem(stem) and fs._real_file(entry):
            stems.add(stem)
    return frozenset(stems)


#: The verdict status that means the ladder passed: :func:`rebuild_bindings` writes.
REBUILD = "rebuilt"


def _ladder(
    mc: Path,
    bot: ImportedBot,
    claims: dict[str, dict | None] | None,
    installed_stems: Collection[str] | None,
    config: Any,
    owned_slugs: Collection[str] | None,
) -> tuple[PartOutcome, str]:
    """The binding verdict for *bot* and the generation a write would record.

    *installed_stems* ``None`` asks the question before the chat merge: the Bot's
    archive thread counts as installed unless the destination already holds it.
    """

    def outcome(status: str, reason: str = "") -> tuple[PartOutcome, str]:
        return PartOutcome(bot.name, _PART, status, reason), ""

    # (1) a binding the destination already holds decides first, whatever the
    # transcript's origin: one naming this Bot at this slot is kept, any other is
    # left byte-for-byte as it is. One the destination held for another Bot of
    # that slug is never adopted, whatever it names.
    try:
        present = os.path.lexists(members_mod.dm_binding_path(bot.slug))
    except (OSError, ValueError, RuntimeError):
        return outcome("not_rebuilt", "write_failed")
    if present and owned_slugs is not None and bot.slug not in owned_slugs:
        return outcome("not_rebuilt", "held_by_other_bot")
    if present:
        return _existing_binding(bot), ""
    stem = member_stem(bot.slot_key)
    held = _destination_transcript_exists(mc, stem)
    installed = (not held) if installed_stems is None else stem in installed_stems
    if not installed:
        if not held:
            return outcome("not_needed")
        # A transcript at this Bot's own slot, on a slug this install had, is its
        # own thread whose binding was lost: the checks below decide the rebuild.
        if owned_slugs is not None and bot.slug not in owned_slugs:
            return outcome("not_rebuilt", "transcript_predates_import")
    if config is None:
        return outcome("not_rebuilt", "unreadable_record")
    refusal = identity_refusal(config, bot)
    if refusal:
        return outcome("not_rebuilt", refusal)
    try:
        slot_key, generation = _derived_slot(config, bot)
    except ValueError:
        return outcome("not_rebuilt", "unreadable_record")
    if slot_key != bot.slot_key:
        return outcome("not_rebuilt", "unreadable_record")
    # (c) an archive claim may only veto: it must agree on every derived value, and
    # a claim set that could not be read at all vetoes every rebuild.
    if claims is None:
        return outcome("not_rebuilt", "unreadable_record")
    if bot.slug in claims:
        claim = claims[bot.slug]
        derived = {
            "member_id": bot.member_id,
            "member": bot.name,
            "slug": bot.slug,
            "slot_key": slot_key,
            "memory_store": generation,
        }
        if not isinstance(claim, dict) or any(
            claim.get(key, "" if key == "memory_store" else None) != derived[key]
            for key in _CLAIM_KEYS
        ):
            return outcome("not_rebuilt", "archive_binding_disagrees")
    return PartOutcome(bot.name, _PART, REBUILD), generation


def binding_ladder(
    mc: Path,
    bot: ImportedBot,
    claims: dict[str, dict | None] | None,
    config: Any,
    *,
    owned_slugs: Collection[str] | None = None,
) -> tuple[PartOutcome, str]:
    """The binding verdict for *bot*, judged before the chat merge, and the generation a write records.

    The one ladder both the rebuild and the chat merge's member-thread predicate use.
    Status :data:`REBUILD` means every check passed and the rebuild writes the binding;
    ``kept``, ``not_needed`` and ``not_rebuilt`` (with its reason) are final. Judged as
    if the import installs the Bot's archive thread (unless the destination already
    holds that stem). The import judges each Bot whose thread the archive carries this
    way once (``portability_bots.prepare_bots``) and hands the result to
    :func:`rebuild_bindings` (``verdicts``). Raises only what a read raises; writes
    nothing.
    """
    return _ladder(mc, bot, claims, None, config, owned_slugs)


def _binding_since(bot: ImportedBot) -> PartOutcome | None:
    """The outcome for a binding file now at *bot*'s slug, or ``None`` when there is none.

    ``kept`` when it names this Bot at this slot, ``binding_exists_unusable``
    otherwise; ``write_failed`` when whether one is there cannot be told.
    """
    try:
        if not os.path.lexists(members_mod.dm_binding_path(bot.slug)):
            return None
    except (OSError, ValueError, RuntimeError):
        return PartOutcome(bot.name, _PART, "not_rebuilt", "write_failed")
    return _existing_binding(bot)


def _existing_binding(bot: ImportedBot) -> PartOutcome:
    """``kept`` when the binding file at *bot*'s slug names it at its slot, else unusable."""
    # Only a binding of exactly this slot key reads back here.
    existing = members_mod.read_dm_binding_for_slot(bot.slot_key)
    if existing is not None and existing.get("member") == bot.name:
        return PartOutcome(bot.name, _PART, "kept")
    return PartOutcome(bot.name, _PART, "not_rebuilt", "binding_exists_unusable")


def _thread_not_here(mc: Path, bot: ImportedBot, installed_stems: Collection[str]) -> bool:
    """Whether *bot*'s member thread is neither installed by this import nor held here."""
    stem = member_stem(bot.slot_key)
    return stem not in installed_stems and not _destination_transcript_exists(mc, stem)


def _rebuild_one(
    mc: Path,
    bot: ImportedBot,
    claims: dict[str, dict | None] | None,
    installed_stems: Collection[str],
    config: Any,
    owned_slugs: Collection[str] | None,
    judged: tuple[PartOutcome, str] | None = None,
) -> PartOutcome:
    if judged is not None and _thread_not_here(mc, bot, installed_stems):
        # Judged before the chat merge as if the merge installs this Bot's thread; it
        # did not, and the destination holds none, so whatever that verdict was rests
        # on a thread that is not here: the ladder judges the Bot again as it stands
        # (a binding file here still decides first; else there is nothing to bind).
        judged = None
    verdict, generation = judged or _ladder(mc, bot, claims, installed_stems, config, owned_slugs)
    if verdict.status != REBUILD:
        return verdict
    # A verdict judged before the chat merge saw no binding file; one that appeared
    # since is left as it is, never written over.
    present = _binding_since(bot)
    if present is not None:
        return present
    try:
        written = members_mod.write_dm_binding(
            bot.slug, member=bot.name, slot_key=bot.slot_key, memory_store=generation
        )
    except (OSError, ValueError):
        logger.warning("binding for Bot %r could not be written", bot.name, exc_info=True)
        return PartOutcome(bot.name, _PART, "not_rebuilt", "write_failed")
    resolved = members_mod.read_dm_binding_for_slot(bot.slot_key)
    if resolved is None or resolved.get("member") != bot.name:
        _remove_if_unchanged(members_mod.dm_binding_path(bot.slug), written)
        return PartOutcome(bot.name, _PART, "not_rebuilt", "unreadable_record")
    return PartOutcome(bot.name, _PART, REBUILD)


def rebuild_bindings(
    mc: Path,
    bots: Iterable[ImportedBot],
    claims: dict[str, dict | None] | None,
    installed_stems: Collection[str],
    *,
    owned_slugs: Collection[str] | None = None,
    config: Any = LOAD_CONFIG,
    verdicts: Mapping[str, tuple[PartOutcome, str]] | None = None,
) -> list[PartOutcome]:
    """One ``binding`` outcome per Bot in *bots*, decided by the ladder of :func:`binding_ladder`.

    *verdicts* maps a Bot name to the verdict :func:`binding_ladder` already judged for
    it with these same inputs; such a Bot is acted on by that verdict, not judged again,
    except when the Bot's member thread is not in *installed_stems* after the chat merge
    and the destination holds no such transcript: whatever that verdict was, the Bot is
    judged again after the merge, so it is ``not_needed`` unless a binding file at its
    slug decides first (step 1).

    1. The destination already has a binding file for the Bot's slug: when the slug
       is not in *owned_slugs* (``None``: every slug is), the binding belongs to the
       other Bot that had that slug before the import, ``held_by_other_bot``;
       otherwise ``kept`` when it names this Bot with this slot key (whatever the
       transcript's origin, so a restore onto the install that made the archive
       keeps the Bot whole), else ``binding_exists_unusable``. Either way the file
       is left byte-for-byte as it is. The same holds for a file that appears after a
       reused *verdicts* entry was judged: it is checked again just before the write.
    2. No transcript at the slot (stem :func:`member_stem` of its slot key) at all:
       ``not_needed``.
    3. A transcript that was on the destination before the import (not in
       *installed_stems*, the stems this import installed), for a slug not in
       *owned_slugs* (``None``: every slug is): ``transcript_predates_import``. On an
       owned slug it is the Bot's own thread at its own slot, and step 4 decides.
    4. Otherwise the binding is written only when all hold: (a) its ``member_id`` is
       non-empty, equals its slug and resolves uniquely to it
       (``execution_context.member_config_for_id``); (b) its slug is no other
       addressable name's; (c) the derived slot equals the Bot's; (d) *claims* could
       be read (``None``: ``unreadable_record``) and a claim for the slug agrees on
       ``member_id``, ``member``, ``slug``, ``slot_key`` and ``memory_store``
       (:func:`identity_refusal` decides (a) and (b)). The written binding must then
       resolve to the Bot through ``read_dm_binding_for_slot``, else it is removed
       again.

    *config* is the destination's config, which the import loads once after its mode
    branch; ``None`` (unreadable) rebuilds none, and the default loads it here.
    """
    from kiro_crew.config.loader import KiroCrewConfig

    if config is LOAD_CONFIG:
        try:
            config = KiroCrewConfig.load()
        except Exception:
            logger.warning("config unreadable; no Bot binding rebuilt", exc_info=True)
            config = None
    outcomes: list[PartOutcome] = []
    for bot in bots:
        try:
            judged = (verdicts or {}).get(bot.name)
            outcomes.append(
                _rebuild_one(mc, bot, claims, installed_stems, config, owned_slugs, judged)
            )
        except Exception:
            logger.warning("binding rebuild for Bot %r failed", bot.name, exc_info=True)
            outcomes.append(PartOutcome(bot.name, _PART, "not_rebuilt", "write_failed"))
    return outcomes


def _split_slot(slot: str) -> tuple[str, str]:
    """``(slug, generation)`` a member slot key spells, unvalidated."""
    slug = members_mod.slug_from_dm_slot_key(slot) or ""
    _, sep, generation = slot.partition(members_mod.MEMORY_STORE_SLOT_SUFFIX)
    return slug, generation if sep else ""


def stem_slug(stem: str) -> str:
    """The member slug a member-thread transcript stem spells (unvalidated), or ``""``."""
    if not is_member_stem(stem):
        return ""
    return _split_slot(stem[len(_pf()._DASHBOARD_STEM_PREFIX) :])[0]


def orphan_transcripts(
    snap: Path,
    bots: Iterable[ImportedBot],
    config: Any,
    *,
    mc: Path | None = None,
    listing: dict[str, os.DirEntry] | None = None,
    passthrough_slugs: Collection[str] = (),
) -> list[dict]:
    """The archive's member-thread transcripts that match no imported Bot's slot.

    One row per such ``sessions/`` transcript, in name order, with its ``slug``
    and ``generation`` (empty for a V1 slot) as the stem spells them, clipped, and
    ``candidates``: the destination Bot names in *config* whose slug it carries.
    These are the transcripts the import withholds; one the data home *mc* already
    holds under the same stem lost nothing and is not listed. A transcript whose slug
    is in *passthrough_slugs* (a reserved Bot's, judged by the chat merge like any
    chat) is never listed. *listing* is :func:`sessions_listing` of *snap*, taken
    here when ``None``.
    """
    allowed = {member_stem(bot.slot_key) for bot in bots}
    fs = _pf()
    entries = sessions_listing(snap) if listing is None else listing
    rows: list[dict] = []
    for name, entry in sorted(entries.items()):
        stem = _pf()._chat_stem(name)
        if stem is None or not is_member_stem(stem) or stem in allowed:
            continue
        if stem_slug(stem) in passthrough_slugs:
            continue
        if not fs._real_file(entry):
            continue
        if mc is not None and _destination_transcript_exists(mc, stem):
            continue
        slug, generation = _split_slot(stem[len(_pf()._DASHBOARD_STEM_PREFIX) :])
        candidates: list[str] = []
        if config is not None:
            try:
                candidates = _names_for_slug(config, members_mod.validate_slug(slug))
            except Exception:
                candidates = []
        rows.append(
            {
                "slug": _pf()._clip(slug),
                "generation": _pf()._clip(generation),
                "candidates": [_pf()._clip(c) for c in candidates],
            }
        )
    return rows
