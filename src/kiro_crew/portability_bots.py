"""The instance import's per-Bot restore: the steps `portability.apply_import_zip` runs.

An instance import keeps every Bot whole or says what is missing. The work is split
by store into the ``portability_bot_*`` units, and this module runs them in order:

1. :func:`before_import`, before the mode branch, reads the archive's binding files as
   claims that can only veto a rebuilt binding (the Replace strip would remove them),
   and records which Bots this install had, so a later step can tell a Bot's own
   existing entry from one another Bot of that name or slug left here.
2. Replace passes :data:`INSTANCE_REPLACE_COMPONENTS`, so the destination's artifact
   library and uploads are never cleared by an instance import.
3. :func:`merge_roster_step`, in a Merge right after the named-store merge, keeps the
   archive Bots that are already here (an install merging its own export) and adds the
   ones that are free, on a destination that kept its own config.
4. :func:`prepare_bots` loads the destination config once, derives each imported
   Bot's DM-thread slot from it, and judges each Bot's binding with the rebuild's
   own ladder (``portability_bot_bindings.binding_ladder``);
   :func:`allowed_member_stems` lets the chat merge install a Bot's thread only when
   that verdict keeps or rebuilds its binding, or the Bot was here before.
5. :func:`restore_bots` installs the Bot stores, writes the minimal crewmate files,
   rebuilds the bindings, lists the orphaned threads and writes ``summary["bots"]``.

Every step after the mode branch reports a failure in the summary and never raises:
the import has already written the data home by then, and a raised error would show a
failed import over one that landed. A memory-only import runs none of this.

"""

from __future__ import annotations

import logging
import os
from collections.abc import Callable, Iterable, Mapping
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from kiro_crew import (
    portability_bot_bindings,
    portability_bot_crewmates,
    portability_bot_export,
    portability_bot_roster,
    portability_bot_stores,
)
from kiro_crew.portability_bot_bindings import REBUILD
from kiro_crew.portability_bot_types import (
    LOAD_CONFIG,
    NOT_WHOLE_STATUSES,
    NOTICES,
    PARTS,
    RESERVED_DEFAULT_MEMBER,
    ImportedBot,
    PartOutcome,
    is_reserved_bot_name,
    raw_entry_identity,
    raw_entry_slug,
)
from kiro_crew.snapshot import COMPONENTS

logger = logging.getLogger("kiro_crew.portability")

#: The snapshot components an instance Replace restores: every one except the artifact
#: library and the uploads. An instance export carries only the artifacts the roster's
#: dashboards pin, and a whole-tree component the archive carries is removed and
#: refilled, so passing either would erase every other artifact or upload here.
INSTANCE_REPLACE_COMPONENTS: tuple[str, ...] = tuple(
    name for name in COMPONENTS if name not in ("artifacts", "uploads")
)

#: Most Bot rows a summary lists; ``bots_more`` counts the rest.
MAX_BOT_ROWS = 200
#: Most orphaned member threads a summary lists; ``bot_transcripts_orphaned_more``
#: counts the rest.
MAX_ORPHAN_ROWS = 200
#: Most destination Bot names one orphan row lists.
MAX_ORPHAN_CANDIDATES = 20
#: Most names the import reads from each of the manifest's Bot lists. The export
#: writes at most 500 and counts the rest in a ``_more`` key.
_MAX_MANIFEST_NAMES = 500

#: The manifest's record kinds and the notice each one raises.
_RECORD_NOTICES: tuple[tuple[str, str], ...] = (
    ("member-rules", "rules_not_carried"),
    ("member-goals", "goals_not_carried"),
)


def _clip(text: object) -> str:
    from kiro_crew import portability

    return str(portability._clip(str(text)))


@dataclass
class RosterStep:
    """What the roster step decided: the Bot names to import, and the Bots it skipped.

    ``names`` is ``None`` only when this import installed the whole config (Replace,
    or a Merge into an install that had none): every agent of that config is
    imported. Otherwise the Merge decided the roster Bot by Bot (:attr:`added`).
    ``kept`` names the archive Bots the Merge found already here (also in
    ``names``); their roster part is ``kept``. ``slugs`` holds the archive slug of
    each skipped Bot, for its summary row.
    """

    names: list[str] | None = None
    skipped: list[PartOutcome] = field(default_factory=list)
    slugs: dict[str, str] = field(default_factory=dict)
    kept: list[str] = field(default_factory=list)

    @property
    def added(self) -> bool:
        """Whether the Merge added the Bots to a config it kept (``names`` is set)."""
        return self.names is not None


@dataclass
class PreImport:
    """What the import read before its mode branch changed anything.

    ``claims`` is :func:`collect_claims`. ``destination`` holds each Bot this install
    had before the import, name -> ``(member_id, memory_store)``, from its own
    ``config.json`` document. ``archive_config`` is the vetted archive ``config.json``.
    ``had_config`` says the destination had a ``config.json`` entry (unread is had).
    ``sessions`` is the archive's ``sessions/`` listing
    (``portability_bot_bindings.sessions_listing``), taken once here and shared by the
    chat merge and the orphan list (``None``: not taken, each lists it itself);
    ``member_stems`` holds the member-thread transcript stems it carries.
    ``destination_slugs`` holds the slug of every Bot this install had, and
    ``reserved_slugs`` the slug this install's own config gave each reserved name
    (``portability_bot_types.is_reserved_bot_name``; :func:`_destination_reserved_slugs`),
    read here so a config the import installs never decides one.
    """

    claims: dict[str, Any] | None = None
    destination: dict[str, tuple[str, str]] = field(default_factory=dict)
    destination_slugs: frozenset[str] = frozenset()
    reserved_slugs: frozenset[str] = frozenset()
    archive_config: object = None
    had_config: bool = True
    member_stems: frozenset[str] = frozenset()
    sessions: dict[str, os.DirEntry] | None = None


@dataclass
class PreparedBots:
    """The imported Bots with their derived slots, and the roster outcomes so far.

    ``owned_slugs`` holds the slug of each imported Bot that this install already had
    (:func:`was_here`): an entry at one of its paths is its own. ``pre`` is the
    :class:`PreImport` this import read before its mode branch; ``claims``,
    ``sessions``, ``fresh_home`` (this install had no ``config.json`` before the
    import) and ``archive_bots`` (the archive config names a Bot) read it. ``config``
    is the destination config loaded once after the mode branch (``None`` when it
    could not be read), which every later step reads. ``verdicts`` holds, by Bot name,
    the binding verdict and generation (``portability_bot_bindings.binding_ladder``)
    judged for each Bot whose thread the archive carries; ``rebuild_bindings`` acts on
    it instead of judging again. ``in_roster`` names the Bots of the import the loaded
    installed config really holds (:func:`_in_roster`: an imported Bot, or one it could
    not set up, only when that config has an agent of its name); empty when that
    config cannot be read, so no row claims a Bot is there.
    ``withheld`` maps each imported
    Bot this install did not have, whose member thread the archive carries and whose
    binding verdict (``portability_bot_bindings.binding_ladder``) neither keeps nor
    rebuilds it, to that verdict's reason: its transcript is not installed, so its
    thread opens fresh instead of on a binding that is not its own.
    ``reserved_slugs`` holds the slugs this install's own config gave the reserved
    names before the import (``PreImport.reserved_slugs``), less every slug a Bot uses
    (an imported one, one of the installed config, or one this install had): their
    member threads are out of the Bot processing and judged by the chat merge like any
    chat.
    """

    bots: list[ImportedBot] = field(default_factory=list)
    outcomes: list[PartOutcome] = field(default_factory=list)
    slugs: dict[str, str] = field(default_factory=dict)
    roster_status: str = "imported"
    owned_slugs: frozenset[str] = frozenset()
    pre: PreImport = field(default_factory=PreImport)
    config: Any = None
    withheld: dict[str, str] = field(default_factory=dict)
    reserved_slugs: frozenset[str] = frozenset()
    verdicts: dict[str, tuple[PartOutcome, str]] = field(default_factory=dict)
    in_roster: frozenset[str] = frozenset()

    @property
    def claims(self) -> dict[str, Any] | None:
        return self.pre.claims

    @property
    def sessions(self) -> dict[str, os.DirEntry] | None:
        return self.pre.sessions

    @property
    def fresh_home(self) -> bool:
        return not self.pre.had_config

    @property
    def archive_bots(self) -> bool:
        return bool(_archive_agents(self.pre.archive_config))


def collect_claims(snap: Path) -> dict[str, Any] | None:
    """The archive's binding files by slug, read before anything can remove them.

    ``None`` when they could not be read at all: a claim can only veto a rebuilt
    binding, so an unread claim set rebuilds none (fail closed, `restore_bots`).
    """
    try:
        claims = portability_bot_bindings.read_archive_binding_claims(snap)
    except Exception:  # noqa: BLE001 - any failure only withholds the rebuild
        logger.warning("Could not read the archive's Bot binding claims", exc_info=True)
        return None
    return claims if isinstance(claims, dict) else None


def _destination_document(mc: Path) -> Mapping[str, Any] | None:
    try:
        return portability_bot_export.read_config_document(str(mc))
    except Exception:  # noqa: BLE001 - an unread roster owns nothing
        logger.warning("Could not read this install's Bot roster", exc_info=True)
        return None


def destination_bots(mc: Path, doc: Any = LOAD_CONFIG) -> dict[str, tuple[str, str]]:
    """This install's Bots as its ``config.json`` names them: name -> (member_id, store).

    Read as a document (``portability_bot_export.read_config_document``), never through
    the loader; *doc* is that document when the caller read it already. Each entry's
    identity is ``portability_bot_types.raw_entry_identity``, so a version-1 Bot is
    listed with an empty ``member_id``; an entry with no identity (not an object, or a
    value that is not a string) is not listed: it can be the same Bot as no archive
    Bot. Empty when there is no readable config.
    """
    if doc is LOAD_CONFIG:
        doc = _destination_document(mc)
    agents = doc.get("agents") if isinstance(doc, Mapping) else None
    found: dict[str, tuple[str, str]] = {}
    for name, entry in agents.items() if isinstance(agents, Mapping) else ():
        if not isinstance(name, str) or is_reserved_bot_name(name):
            continue
        identity = raw_entry_identity(entry)
        if identity is not None:
            found[name] = identity
    return found


def _sessions_listing(snap: Path) -> dict[str, os.DirEntry] | None:
    try:
        return portability_bot_bindings.sessions_listing(snap)
    except Exception:  # noqa: BLE001 - not taken: each step lists it itself
        logger.warning("Could not list the archive's chats", exc_info=True)
        return None


def _archive_member_stems(snap: Path, listing: dict[str, os.DirEntry] | None) -> frozenset[str]:
    try:
        return frozenset(portability_bot_bindings.archive_member_stems(snap, listing=listing))
    except Exception:  # noqa: BLE001 - unlisted stems withhold nothing early
        logger.warning("Could not list the archive's member threads", exc_info=True)
        return frozenset()


def _destination_slugs(doc: Any) -> frozenset[str]:
    """The slug of every Bot (no reserved name) config document *doc* holds."""
    agents = doc.get("agents") if isinstance(doc, Mapping) else None
    if not isinstance(agents, Mapping):
        return frozenset()
    slugs = {_archive_slug(str(n), agents) for n in agents if not is_reserved_bot_name(n)}
    slugs.discard("")
    return frozenset(slugs)


def _destination_reserved_slugs(doc: Any) -> frozenset[str]:
    """The slug config document *doc* gives each reserved name it holds, and ``default``'s.

    The ``default`` agent's (``RESERVED_DEFAULT_MEMBER``) slug is always listed: from
    its entry when *doc* has one, else the slug its name derives. The loader gives an
    install a ``default`` agent whatever the document holds (an empty or degraded
    ``agents`` table, or no readable config at all), and the import may install a
    config that has one, so a ``default`` thread is never judged as a Bot's. A slug a
    Bot uses is taken out again (:func:`_reserved_slugs`).
    """
    agents = doc.get("agents") if isinstance(doc, Mapping) else None
    if not isinstance(agents, Mapping):
        agents = {}
    slugs = {_archive_slug(str(n), agents) for n in agents if is_reserved_bot_name(n)}
    if RESERVED_DEFAULT_MEMBER not in agents:
        slugs.add(_archive_slug(RESERVED_DEFAULT_MEMBER, {}))
    slugs.discard("")
    return frozenset(slugs)


def before_import(snap: Path, mc: Path, vetted_settings: Mapping[str, object]) -> PreImport:
    """Read, before the mode branch, what the later Bot steps need from before it. Never raises."""
    sessions = _sessions_listing(snap)
    doc = _destination_document(mc)
    return PreImport(
        claims=collect_claims(snap),
        destination=destination_bots(mc, doc),
        destination_slugs=_destination_slugs(doc),
        reserved_slugs=_destination_reserved_slugs(doc),
        archive_config=vetted_settings.get("config.json"),
        had_config=os.path.lexists(mc / "config.json"),
        member_stems=_archive_member_stems(snap, sessions),
        sessions=sessions,
    )


def was_here(bot: ImportedBot, destination: Mapping[str, tuple[str, str]]) -> bool:
    """Whether this install had *bot* itself before the import (the same-Bot rule).

    The same name, the same ``member_id`` (empty on both sides for a version-1 Bot)
    and the same memory store; any other Bot that had the name or slug is a
    different Bot.
    """
    return destination.get(bot.name) == (bot.member_id, bot.memory_store)


def _archive_slug(name: str, agents: Mapping[str, Any]) -> str:
    """The slug the archive config gives Bot *name*, or ``""`` when it names none valid."""
    try:
        return raw_entry_slug(name, agents.get(name))
    except Exception:  # noqa: BLE001 - archive-authored; an unusable one shows no slug
        return ""


def _archive_agents(archive_config: object) -> dict[str, Any]:
    """The archive config's Bots by name: its ``agents`` table without ``default``."""
    agents = archive_config.get("agents") if isinstance(archive_config, dict) else None
    if not isinstance(agents, dict):
        return {}
    return {str(k): v for k, v in agents.items() if not is_reserved_bot_name(k)}


def merge_roster_step(
    mc: Path,
    vetted_settings: Mapping[str, object],
    summary: dict,
    *,
    stores_added: Iterable[str],
    config_installed: bool,
    teams_installed: bool = False,
) -> RosterStep:
    """Decide the roster of a Merge, after its settings and named-store merges ran.

    When the destination kept its own ``config.json`` (`_merge_settings` named it in
    ``settings_kept``), ``portability_bot_roster.merge_roster`` keeps the archive Bots
    that are already here and adds the ones that are free, and only those are
    imported. When *config_installed* (this Merge installed the archive's config
    into an install that had none), that config is the roster and all of it is
    imported. Otherwise no Bot is imported: when the archive carried a config
    naming Bots that could not be installed, each of them gets a roster row
    ``skipped`` with reason ``config_rejected``. *teams_installed* says this Merge
    installed the archive's ``crew-teams`` document, whose memberships are then the
    imported Bots' own (``merge_roster`` releases no added name).
    """
    archive_config = vetted_settings.get("config.json")
    agents = _archive_agents(archive_config)
    if "config.json" not in summary.get("settings_kept", ()):
        return RosterStep() if config_installed else _rejected_step(agents)
    if not agents or not isinstance(archive_config, Mapping):
        return RosterStep(names=[])
    try:
        merged = portability_bot_roster.merge_roster(
            archive_config,
            mc,
            stores_added=frozenset(stores_added),
            teams_installed=teams_installed,
        )
    except Exception:  # noqa: BLE001 - a failed roster merge imports no archive Bot
        logger.warning("Could not merge the archive's Bot roster", exc_info=True)
        return _rejected_step(agents)
    slugs = {o.bot: _archive_slug(o.bot, agents) for o in merged.skipped}
    return RosterStep(
        names=merged.added + merged.kept,
        skipped=list(merged.skipped),
        slugs=slugs,
        kept=list(merged.kept),
    )


def _rejected_step(agents: Mapping[str, Any]) -> RosterStep:
    """A step that imports no Bot and skips each of *agents* with ``config_rejected``."""
    skipped = [PartOutcome(name, "roster", "skipped", "config_rejected") for name in agents]
    slugs = {o.bot: _archive_slug(o.bot, agents) for o in skipped}
    return RosterStep(names=[], skipped=skipped, slugs=slugs)


def _installed_config() -> Any:
    """The destination config as the import left it (the cache is dropped first)."""
    from kiro_crew.config.loader import KiroCrewConfig, _invalidate_config_cache

    _invalidate_config_cache()
    return KiroCrewConfig.load()


def prepare_bots(mc: Path, step: RosterStep | None, pre: PreImport | None = None) -> PreparedBots:
    """The imported Bots and their DM-thread slots, through ``imported_bots``.

    *step* is the Merge's roster step, or ``None`` for a Replace: then every agent of
    the installed config is imported. The installed config is loaded here, once, and
    kept on the result for every later step. When it cannot be read, the names are
    the step's, or for a Replace the archive config's (*pre*), so every Bot still gets
    a row. Never raises: a failure becomes a skipped roster row with reason
    ``unreadable_record`` for each Bot it covers.
    """
    step = step if step is not None else RosterStep()
    pre = pre if pre is not None else PreImport()
    archive_agents = _archive_agents(pre.archive_config)
    prepared = PreparedBots(
        outcomes=list(step.skipped),
        slugs=dict(step.slugs),
        roster_status="added" if step.added else "imported",
        pre=pre,
    )
    config: Any = None
    try:
        config = prepared.config = _installed_config()
        # The reserved ``default`` agent is not a Bot: no member thread, crewmate file or
        # dashboard belongs to it, so a Replace lists only the roster's Bots.
        installed = [n for n in config.agents if not is_reserved_bot_name(n)]
    except Exception:  # noqa: BLE001 - name the Bots another way; their rows say why
        logger.warning("Could not read the installed config", exc_info=True)
        installed = list(archive_agents)
    names = installed if step.names is None else list(step.names)
    try:
        prepared.bots, outcomes = portability_bot_roster.imported_bots(mc, names, config=config)
        prepared.outcomes.extend(outcomes)
        prepared.outcomes.extend(
            PartOutcome(b.name, "roster", "kept") for b in prepared.bots if b.name in step.kept
        )
    except Exception:  # noqa: BLE001 - the data home is written; report, never raise
        logger.warning("Could not read the imported Bots", exc_info=True)
        prepared.bots = []
        prepared.outcomes.extend(
            PartOutcome(name, "roster", "skipped", "unreadable_record") for name in names
        )
    for outcome in prepared.outcomes:
        if outcome.bot not in prepared.slugs:
            prepared.slugs[outcome.bot] = _installed_slug(outcome.bot, config, archive_agents)
    for bot in prepared.bots:
        prepared.slugs[bot.name] = bot.slug
    prepared.in_roster = _in_roster(config, names)
    prepared.owned_slugs = frozenset(
        bot.slug for bot in prepared.bots if was_here(bot, pre.destination)
    )
    prepared.withheld, prepared.verdicts = _withheld_threads(mc, prepared, pre.member_stems)
    prepared.reserved_slugs = _reserved_slugs(config, prepared)
    return prepared


def _in_roster(config: Any, names: list[str]) -> frozenset[str]:
    """The Bots of *names* the loaded installed *config* holds; empty when it was not read.

    *names* are the Bots this import set out to import (the installed config's own, or
    the Merge roster step's added and kept ones): each counts only when that config
    really has an agent of that name, never because the step named it. Unknown when
    the config cannot be read, so no row then claims the Bot is there.
    """
    if config is None:
        return frozenset()
    try:
        agents = config.agents
        return frozenset(n for n in names if isinstance(n, str) and n in agents)
    except Exception:  # noqa: BLE001 - unknown: no row claims the Bot is there
        logger.warning("Could not read the installed roster's names", exc_info=True)
        return frozenset()


def _reserved_slugs(config: Any, prepared: PreparedBots) -> frozenset[str]:
    """The reserved names' slugs this install had (``PreImport.reserved_slugs``), less every Bot's.

    They come from the destination's own config as it was before the import, never
    from a config the import installed. A slug an imported Bot, a Bot the roster step
    skipped, any archive entry that is not a reserved name, a Bot of the installed
    config *config* or a Bot this install had uses is never one: its member threads
    are a Bot's, withheld or judged as one, never passed through as any chat.
    """
    from kiro_crew import members

    archive_agents = _archive_agents(prepared.pre.archive_config)
    used: set[str] = {bot.slug for bot in prepared.bots} | set(prepared.pre.destination_slugs)
    used.update(prepared.slugs.values())
    used.update(_archive_slug(name, archive_agents) for name in archive_agents)
    for name in getattr(config, "agents", None) or ():
        if is_reserved_bot_name(name):
            continue
        try:
            used.add(str(members.member_slug(name, config)))
        except Exception:  # noqa: BLE001 - no slug, nothing it could take from another
            logger.debug("No slug for agent %r", name, exc_info=True)
    return frozenset(prepared.pre.reserved_slugs - used - {""})


def _withheld_threads(
    mc: Path, prepared: PreparedBots, member_stems: frozenset[str]
) -> tuple[dict[str, str], dict[str, tuple[PartOutcome, str]]]:
    """``(withheld, verdicts)``, judged before the chat merge.

    *verdicts* holds the binding verdict (``portability_bot_bindings.binding_ladder``,
    the ladder the rebuild runs) of every Bot whose thread the archive carries.
    *withheld* maps Bot name -> why its archive thread is withheld. A Bot whose slug
    is in ``owned_slugs`` is never listed: a self-restore puts back the transcript it
    lost, as the install held it. Any other whose thread the archive carries is listed
    unless its verdict is ``kept`` or rebuilt; one whose verdict cannot be computed is
    listed with ``unreadable_record``, so no thread opens on a binding nobody checked.
    """
    owned = prepared.owned_slugs
    withheld: dict[str, str] = {}
    verdicts: dict[str, tuple[PartOutcome, str]] = {}
    for bot in prepared.bots:
        try:
            if portability_bot_bindings.member_stem(bot.slot_key) not in member_stems:
                continue
            verdict = verdicts[bot.name] = portability_bot_bindings.binding_ladder(
                mc, bot, prepared.claims, prepared.config, owned_slugs=owned
            )
        except Exception:  # noqa: BLE001 - an unjudged binding opens no thread
            logger.warning("Could not judge the binding of Bot %r", bot.name, exc_info=True)
            if bot.slug not in owned:
                withheld[bot.name] = "unreadable_record"
            continue
        if bot.slug not in owned and verdict[0].status not in ("kept", REBUILD):
            withheld[bot.name] = verdict[0].reason or "unreadable_record"
    return withheld, verdicts


def _installed_slug(name: str, config: Any, archive_agents: Mapping[str, Any]) -> str:
    """The slug *config* gives Bot *name*; the archive's when *config* is unread."""
    from kiro_crew import members

    if config is None:
        return _archive_slug(name, archive_agents)
    try:
        return str(members.member_slug(name, config))
    except Exception:  # noqa: BLE001 - a row without a slug still names the Bot
        return ""


def member_thread_withheld(allowed: Callable[[str], bool] | None, stem: str) -> bool:
    """Whether `_merge_sessions` withholds the archive transcript *stem*.

    It does when *stem* is a member thread (``portability_bot_bindings.is_member_stem``)
    that *allowed* does not accept. ``False`` when *allowed* is ``None``: the stem is
    judged like any other chat. The caller counts a withheld stem this install
    already holds as "already here" (nothing was lost), any other in
    ``sessions_member_withheld``.
    """
    if allowed is None or not portability_bot_bindings.is_member_stem(stem):
        return False
    return not allowed(stem)


def allowed_member_stems(
    bots: PreparedBots | Iterable[ImportedBot] | None,
) -> Callable[[str], bool] | None:
    """The `_merge_sessions` predicate: a member-thread stem of an imported Bot slot.

    A Bot in ``PreparedBots.withheld`` is left out: its binding will not be rebuilt,
    so its transcript is withheld (and listed as orphaned) and the thread opens fresh.
    A stem of a reserved Bot's slug (``PreparedBots.reserved_slugs``) is accepted: the
    chat merge installs it as any chat, when its file name is free.
    ``None`` (a memory-only import, which prepares no Bots) answers ``None``, which
    keeps the chat merge's judgement of a member thread as it is for any chat.
    """
    if bots is None:
        return None
    reserved: frozenset[str] = frozenset()
    if isinstance(bots, PreparedBots):
        reserved = bots.reserved_slugs
        bots = _thread_bots(bots)
    allowed: set[str] = set()
    for bot in bots:
        try:
            allowed.add(portability_bot_bindings.member_stem(bot.slot_key))
        except Exception:  # noqa: BLE001 - a slot with no stem installs no thread
            logger.warning("No transcript stem for Bot %r", bot.name, exc_info=True)
    if not reserved:
        return allowed.__contains__

    def accepted(stem: str) -> bool:
        return stem in allowed or portability_bot_bindings.stem_slug(stem) in reserved

    return accepted


def _thread_bots(bots: PreparedBots) -> list[ImportedBot]:
    """The imported Bots whose member thread the import installs (none withheld)."""
    return [bot for bot in bots.bots if bot.name not in bots.withheld]


def _read_manifest(snap: Path) -> dict:
    from kiro_crew import portability

    try:
        return portability._read_archive_object(snap / "MANIFEST.json")
    except Exception:  # noqa: BLE001 - an unreadable manifest declares nothing
        return {}


def _manifest_names(manifest: Mapping[str, Any], key: str) -> frozenset[str]:
    value = manifest.get(key)
    if not isinstance(value, list):
        return frozenset()
    return frozenset(v for v in value[:_MAX_MANIFEST_NAMES] if isinstance(v, str))


def _manifest_records(manifest: Mapping[str, Any]) -> dict[str, frozenset[str]]:
    value = manifest.get("member_records_not_carried")
    if not isinstance(value, dict):
        return {}
    records: dict[str, frozenset[str]] = {}
    for slug, kinds in list(value.items())[:_MAX_MANIFEST_NAMES]:
        if isinstance(slug, str) and isinstance(kinds, list):
            records[slug] = frozenset(k for k in kinds if isinstance(k, str))
    return records


def _more_count(manifest: Mapping[str, Any], key: str) -> int:
    value = manifest.get(key)
    return value if isinstance(value, int) and not isinstance(value, bool) and value > 0 else 0


def _per_bot(
    bots: list[ImportedBot],
    parts: Mapping[str, str],
    call: Callable[[list[ImportedBot]], Iterable[PartOutcome]],
) -> list[PartOutcome]:
    """Run *call* once with every Bot; *parts* maps each part the unit owns to its failed status.

    When that call raises it is not retried: what it wrote before raising is not
    known, so every Bot gets, for EVERY part in *parts*, that status with reason
    ``write_failed``.
    """
    if not bots:
        return []
    try:
        return list(call(list(bots)))
    except Exception:  # noqa: BLE001 - the data home is written; report every Bot's parts
        logger.warning("%s restore failed for every Bot", "/".join(parts), exc_info=True)
    return [
        PartOutcome(bot.name, part, status, "write_failed")
        for bot in bots
        for part, status in parts.items()
    ]


def _part_outcome(outcomes: list[PartOutcome], part: str) -> PartOutcome | None:
    """The one outcome a row reports for *part*: its first not-whole one, else its first."""
    mine = [o for o in outcomes if o.part == part]
    for outcome in mine:
        if outcome.status in NOT_WHOLE_STATUSES:
            return outcome
    return mine[0] if mine else None


def _part_status(outcomes: list[PartOutcome], part: str, default: str) -> str:
    """The status of :func:`_part_outcome` for *part*; *default* when no unit reported it."""
    chosen = _part_outcome(outcomes, part)
    return chosen.status if chosen is not None else default


def _part_reason(outcomes: list[PartOutcome], part: str) -> str:
    """The reason of :func:`_part_outcome` for *part*; ``""`` for none."""
    chosen = _part_outcome(outcomes, part)
    return chosen.reason if chosen is not None else ""


def _row(
    name: str,
    slug: str,
    outcomes: list[PartOutcome],
    notices: list[str],
    roster: str,
    *,
    in_roster: bool,
) -> dict[str, Any]:
    """One ``summary["bots"]`` row.

    Every part's status, ``part_reasons`` (part -> the reason of that status, ``""``
    when none), the merged ``reasons`` list (kept for older readers), the notices and
    ``in_roster`` (the installed config holds the Bot after the import; ``False`` also
    when that config could not be read, so it is known only when ``True``).
    """
    row: dict[str, Any] = {"bot": _clip(name), "slug": _clip(slug)}
    # Every part, so a Bot that is not whole shows which: ``none`` where no unit reported.
    for part in PARTS:
        row[part] = _part_status(outcomes, part, roster if part == "roster" else "none")
    row["part_reasons"] = {part: _part_reason(outcomes, part) for part in PARTS}
    reasons: list[str] = []
    for outcome in outcomes:
        if outcome.reason and outcome.reason not in reasons:
            reasons.append(outcome.reason)
    row["reasons"] = reasons
    row["notices"] = notices
    row["in_roster"] = in_roster
    return row


def _missing_text(outcomes: list[PartOutcome]) -> str:
    missing = []
    for part in PARTS:
        for o in outcomes:
            if o.part == part and not o.whole:
                text = f"{part.replace('_', ' ')} {o.status.replace('_', ' ')}"
                missing.append(f"{text}: {o.reason}" if o.reason else text)
    return "; ".join(missing)


def _destination_has_record(kind: str, slug: str) -> bool:
    """Whether this install holds a *kind* record for *slug*; ``False`` when unknown."""
    from kiro_crew.member_records import records_dir

    try:
        return os.path.lexists(records_dir(kind) / f"{slug}.json")
    except Exception:  # noqa: BLE001 - unknown reads as lost, so the notice is shown
        return False


def _notices_for(
    name: str,
    slug: str,
    abilities: Mapping[str, list[str]],
    records: Mapping[str, frozenset[str]],
) -> list[str]:
    """What Bot *name* arrived without: a notice only for something not here after the import."""
    notices = [n for n in abilities.get(name, []) if n in NOTICES]
    kinds = records.get(slug, frozenset())
    notices.extend(
        notice
        for kind, notice in _RECORD_NOTICES
        if kind in kinds and not _destination_has_record(kind, slug)
    )
    return notices


def _orphan_rows(rows: object) -> list[dict[str, Any]]:
    clipped: list[dict[str, Any]] = []
    for row in rows if isinstance(rows, list) else ():
        if not isinstance(row, dict):
            continue
        candidates = row.get("candidates")
        names = list(candidates) if isinstance(candidates, (list, tuple)) else []
        clipped.append(
            {
                "slug": _clip(row.get("slug", "")),
                "generation": _clip(row.get("generation", "")),
                "candidates": [_clip(n) for n in names[:MAX_ORPHAN_CANDIDATES]],
            }
        )
    return clipped


def restore_bots(
    snap: Path,
    mc: Path,
    summary: dict,
    *,
    bots: PreparedBots,
    installed_stems: Iterable[str],
    allow_unpinned: bool,
    manifest: Mapping[str, Any] | None = None,
) -> None:
    """Restore each imported Bot's stores, crewmate file and binding, and report.

    Runs ``install_bot_stores``, ``restore_crewmate_files``, ``rebuild_bindings``
    (each once with every Bot, see :func:`_per_bot`; a Bot in
    ``PreparedBots.withheld`` is not rebuilt and its binding is ``not_rebuilt`` with
    that reason) and then ``orphan_transcripts``, both on ``PreparedBots.config``,
    and writes the summary keys
    ``bots`` (``bots_more``), ``bot_transcripts_orphaned`` and the items lines, each
    also listed in ``report_items`` (:func:`report_lines`).
    ``bot_dashboards_not_carried`` is reported only when the archive config names at
    least one Bot. Never raises: a failure of the whole step sets ``bots_failed`` to
    the exception's class name, with an items line.
    """
    summary.setdefault("report_items", [])
    try:
        _restore_bots(
            snap,
            mc,
            summary,
            bots=bots,
            installed_stems=frozenset(installed_stems),
            allow_unpinned=allow_unpinned,
            manifest=_read_manifest(snap) if manifest is None else manifest,
        )
    except Exception as exc:  # noqa: BLE001 - the data home is written; report, never raise
        logger.warning("Bot restore failed", exc_info=True)
        failed = _clip(type(exc).__name__)
        summary["bots_failed"] = failed
        report_lines(summary, [f"bots (failed: {failed})"])
        _flag_refused(summary)


def report_lines(summary: dict, lines: Iterable[str]) -> None:
    """Append the Bot report *lines* to ``summary["items"]`` and list them in ``report_items``.

    ``items`` is what the CLI prints. ``report_items`` holds exactly these strings,
    so the Portability tab can leave them out of its count of restored items
    without matching their text. Set (empty) whenever the Bot step ran.
    """
    lines = list(lines)
    summary["items"].extend(lines)
    summary.setdefault("report_items", []).extend(lines)


def _flag_refused(summary: dict) -> None:
    refused = summary.setdefault("refused_merges", [])
    if "bots" not in refused:
        refused.append("bots")


def _restore_bots(
    snap: Path,
    mc: Path,
    summary: dict,
    *,
    bots: PreparedBots,
    installed_stems: frozenset[str],
    allow_unpinned: bool,
    manifest: Mapping[str, Any],
) -> None:
    customised = _manifest_names(manifest, "crewmate_files_customised")
    records = _manifest_records(manifest)
    imported = list(bots.bots)
    outcomes = list(bots.outcomes)
    owned = bots.owned_slugs
    claims = bots.claims

    # Each unit's call runs inside the capture, so one that raises costs that part
    # of every Bot, reported, and never the steps after it.
    outcomes += _per_bot(
        imported,
        {"dashboard": "not_restored", "briefing": "not_restored"},
        lambda some: portability_bot_stores.install_bot_stores(
            snap, mc, some, allow_unpinned=allow_unpinned, owned_slugs=owned
        ),
    )
    minimal_kept: set[str] = set()
    crewmates = _per_bot(
        imported,
        {"crewmate_file": "not_written"},
        lambda some: portability_bot_crewmates.restore_crewmate_files(
            some, owned_slugs=owned, fresh_home=bots.fresh_home, minimal=minimal_kept
        ),
    )
    outcomes += crewmates
    # A Bot whose thread was withheld has the binding verdict that withheld it.
    outcomes += [
        PartOutcome(name, "binding", "not_rebuilt", reason)
        for name, reason in bots.withheld.items()
    ]
    threads = _thread_bots(bots)
    outcomes += _per_bot(
        threads,
        {"binding": "not_rebuilt"},
        lambda some: portability_bot_bindings.rebuild_bindings(
            mc,
            some,
            claims,
            installed_stems,
            owned_slugs=owned,
            config=bots.config,
            verdicts=bots.verdicts,
        ),
    )
    try:
        orphans = _orphan_rows(
            portability_bot_bindings.orphan_transcripts(
                snap,
                threads,
                bots.config,
                mc=mc,
                listing=bots.sessions,
                passthrough_slugs=bots.reserved_slugs,
            )
        )
    except Exception:  # noqa: BLE001 - the orphan list is a report; lose it, not the import
        logger.warning("Could not list the orphaned member threads", exc_info=True)
        orphans = []
    written = _minimal_file_bots(crewmates, minimal_kept)
    _write_summary(
        summary,
        bots,
        outcomes,
        orphans,
        _ability_notices(imported, customised, written),
        records,
        manifest,
        archive_bots=bots.archive_bots,
    )


def _minimal_file_bots(
    crewmates: Iterable[PartOutcome], minimal_kept: Iterable[str]
) -> frozenset[str]:
    """The Bots that run on the minimal crewmate file after the import, with no added abilities.

    Each Bot whose minimal file this import wrote, and each Bot this install did not
    have whose kept file the crewmate step itself judged the minimal file
    (*minimal_kept*, filled by ``portability_bot_crewmates.restore_crewmate_files``),
    when its outcome is still ``kept``.
    """
    minimal = frozenset(minimal_kept)
    return frozenset(
        o.bot
        for o in crewmates
        if o.status == "written" or (o.status == "kept" and o.bot in minimal)
    )


def _ability_notices(
    imported: list[ImportedBot], customised: frozenset[str], written: frozenset[str]
) -> dict[str, list[str]]:
    """``crewmate_file_notices``: Bot name -> its crewmate-file notices.

    *written* names the Bots whose minimal crewmate file this import wrote: only they
    arrived without the abilities their exported file had.
    """
    try:
        return portability_bot_crewmates.crewmate_file_notices(
            imported, customised, written=written
        )
    except Exception:  # noqa: BLE001 - the notice is the archive's own list; say it anyway
        logger.warning("Could not read the crewmate-file notices", exc_info=True)
        return {
            b.name: ["starting_abilities_only"]
            for b in imported
            if b.name in customised and b.name in written
        }


def _write_summary(
    summary: dict,
    bots: PreparedBots,
    outcomes: list[PartOutcome],
    orphans: list[dict[str, Any]],
    abilities: Mapping[str, list[str]],
    records: Mapping[str, frozenset[str]],
    manifest: Mapping[str, Any],
    *,
    archive_bots: bool,
) -> None:
    by_bot: dict[str, list[PartOutcome]] = {}
    for outcome in outcomes:
        by_bot.setdefault(outcome.bot, []).append(outcome)
    for bot in bots.bots:
        by_bot.setdefault(bot.name, [])
    imported = {bot.name for bot in bots.bots}
    rows: list[dict[str, Any]] = []
    lines: list[str] = []
    any_not_whole = False
    more = 0
    for name in sorted(by_bot, key=lambda n: (n.casefold(), n)):
        parts = by_bot[name]
        whole = all(o.whole for o in parts)
        any_not_whole = any_not_whole or not whole
        if len(rows) >= MAX_BOT_ROWS:
            # Past the cap a Bot is only counted: no row, no notice read, no line.
            more += 1
            continue
        slug = bots.slugs.get(name, "")
        # A notice says what an imported Bot arrived without; a skipped Bot has its row.
        notices = _notices_for(name, slug, abilities, records) if name in imported else []
        rows.append(
            _row(name, slug, parts, notices, bots.roster_status, in_roster=name in bots.in_roster)
        )
        if not whole:
            lines.append(f"bot {_clip(name)} (not whole: {_missing_text(parts)})")
        for notice in notices:
            lines.append(f"bot {_clip(name)} (notice: {notice})")
    summary["bots"] = rows
    if more > 0:
        summary["bots_more"] = more
        lines.append(f"bots ({more} more not listed)")
    summary["bot_transcripts_orphaned"] = orphans[:MAX_ORPHAN_ROWS]
    if len(orphans) > MAX_ORPHAN_ROWS:
        summary["bot_transcripts_orphaned_more"] = len(orphans) - MAX_ORPHAN_ROWS
    # An archive with no Bot carried no dashboard to lose, whatever its age.
    if archive_bots and "bot_stores" not in manifest:
        summary["bot_dashboards_not_carried"] = True
        lines.append("bot dashboards (not carried: the archive was made before Bot stores)")
    records_more = _more_count(manifest, "member_records_not_carried_more")
    if records_more:
        summary["member_records_not_carried_more"] = records_more
        lines.append(f"bots ({records_more} more had rules or goals that were not carried)")
    customised_more = _more_count(manifest, "crewmate_files_customised_more")
    if customised_more:
        summary["crewmate_files_customised_more"] = customised_more
        lines.append(f"bots ({customised_more} more had added abilities that were not carried)")
    report_lines(summary, lines)
    if any_not_whole:
        _flag_refused(summary)
