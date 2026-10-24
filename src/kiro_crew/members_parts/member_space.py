"""The member space's layout, member names and crew ids, slugs and member paths.

Every file and directory name a member's space holds, the display-name and crew-id rules
(validation, allocation, the label a crew may show), slugs and the containment-checked
paths under ``members/``, and the first-welcome marker a just-created member's directory
carries.

:mod:`kiro_crew.members` stays the import path and the patch surface; see
:mod:`kiro_crew.members_parts`.
"""

from __future__ import annotations

import json
import logging
import re
from collections.abc import Container, Mapping
from dataclasses import dataclass
from pathlib import Path

from kiro_crew import platform_compat
from kiro_crew.artifacts import slugify
from kiro_crew.atomic_write import atomic_write, read_bytes_with_retry
from kiro_crew.config.paths import data_home
from kiro_crew.external_text import external_text_requires_redaction
from kiro_crew.slugs import slug_hash_fallback
from kiro_crew.validation import (
    _AGENT_NAME_RE,
    MAX_SHORT_STRING,
    normalize_unicode,
    sanitize_string,
)

# Logs under the facade's name, which operators and tests filter on.
logger = logging.getLogger("kiro_crew.members")


#: Directory under the data home holding one subdirectory per crew member.
MEMBERS_DIR_NAME = "members"

#: Append-only pointer log inside a member's directory.
ACTIVITY_FILE_NAME = "activity.jsonl"

# LEGACY ONLY. Nothing writes this file: participation events go to the
# per-member append-only event log, and :func:`read_activity` reads that log
# rather than this path. It survives as a migration SOURCE, folded in once per
# member and then retired by rename (see the event-log service). Bounding it by
# size therefore has nothing to bound, since the writer that grew it is gone;
# the fold streams it under its own byte budget instead, because the file stays
# agent-writable whatever its size.

#: Crew-slug -> DM-thread binding inside a member's directory.
DM_FILE_NAME = "dm.json"

#: Per-member PERMANENT RULES (the user-owned layer of the member system
#: prompt: approval boundaries, forbidden actions, evidence requirements).
#: Lives under the gateway-only ``member-records/`` leaf
#: (:mod:`kiro_crew.member_records`) for the same reason the DM binding does:
#: these rules are precisely what the member must not be able to rewrite for
#: itself, so they sit where the OS sandbox masks every name and the agent's
#: file tools are refused. Only the gateway (via the dashboard rules endpoint —
#: a human action) writes here.
RULES_DIR_NAME = "member-rules"

#: Hard cap on a member's permanent-rules text. Enforced on WRITE (the write
#: is a human dashboard action, so a too-long payload is refused loudly)
#: rather than truncated on read — silently dropping the tail of a rules
#: document would drop rules.
MEMBER_RULES_MAX_CHARS = 4000

#: Per-member self-maintained briefing (the member-owned layer of the member
#: system prompt: current priorities, pointers to its own scripts and notes).
#: Lives in the member's OWN directory — deliberately agent-writable, since
#: the whole point is that the member curates what its future self wakes up
#: knowing. Precedence is fixed by injection order, not trust: rules outrank
#: the briefing because they are injected above it and named as user-owned.
BRIEFING_FILE_NAME = "briefing.md"

#: Cap on how much of the briefing is INJECTED per turn (working memory, not
#: an archive). Enforced on read with a visible truncation marker so the
#: member learns its briefing overflowed instead of silently losing the tail.
MEMBER_BRIEFING_MAX_CHARS = 4000
# Bindings live under the gateway-only ``member-records/`` leaf, NOT inside the
# member's own directory. The binding IS the thread's identity authority (the
# resume/send/thread-open guards all defer to it precisely because transcript
# metadata is operator-editable), so nothing an agent runs may write it: a
# prompt-injected write that re-points ``member`` at a colliding crew would
# hand that crew the thread's entire transcript at the next restore. The leaf
# is masked by the OS sandbox in every mode and refused by the sensitive-path
# floor, and the gateway opens paths there directly
# (:mod:`kiro_crew.member_records`).
DM_BINDINGS_DIR_NAME = "member-bindings"

#: Slot ``mode`` tag for member DM threads. The frontend's single
#: chat-ownership predicate (``isChatPageSurface``) does not admit it, so a
#: slot born with this mode is excluded from the ordinary Sessions list on
#: every consumer with no filtering code of its own.
DM_SLOT_MODE = "member"

#: Slot-key prefix for member DM threads (``member-<slug>``), following the
#: existing ``<kind>-<id>`` key convention (``chat-<N>-<ts>``, ``cron-<id>``).
DM_SLOT_KEY_PREFIX = "member-"

#: Appended to a member's slot key when that member opts into a private memory
#: store (``member-<slug>.memory-<store>``). It belongs to the SLOT, not to the
#: slug, so every reader of a slot key drops it before treating the tail as a
#: slug -- ``validate_slug`` rejects the ``.`` otherwise.
MEMORY_STORE_SLOT_SUFFIX = ".memory-"


# Same shape the artifact store enforces for its slugs: lowercase letters,
# digits and hyphens, 1-80 chars, no leading or trailing hyphen. Kept here as a
# local constant rather than imported because it is a private name there; the
# artifact store remains the source of truth for the spelling.
_SLUG_RE = re.compile(r"^[a-z0-9](?:[a-z0-9-]{0,78}[a-z0-9])?\Z")


MEMBER_NAME_MAX_CHARS = MAX_SHORT_STRING


class MemberNameError(ValueError):
    """Raised when a Crew Member display name is unsafe or unusable."""


# Named by the path callers import it from: a traceback or a log line renders the
# raised class as ``module.qualname``, so the owner module must not leak into it.
MemberNameError.__module__ = "kiro_crew.members"


class MemberSlugError(ValueError):
    """Raised when a member slug is unusable or cannot be allocated."""


# Named by the path callers import it from: a traceback or a log line renders the
# raised class as ``module.qualname``, so the owner module must not leak into it.
MemberSlugError.__module__ = "kiro_crew.members"


def validate_member_name(name: object) -> str:
    """Return an exact Crew Member display name, else raise MemberNameError."""
    if not isinstance(name, str) or not name:
        raise MemberNameError("name must be a non-empty string")
    if len(name) > MEMBER_NAME_MAX_CHARS:
        raise MemberNameError(f"name must be at most {MEMBER_NAME_MAX_CHARS} characters")
    if name != name.strip():
        raise MemberNameError("name must not start or end with whitespace")
    try:
        name.encode("utf-8")
    except UnicodeEncodeError as exc:
        raise MemberNameError("name must be valid UTF-8 text") from exc
    if len(name.splitlines()) != 1 or "\t" in name:
        raise MemberNameError("name must not contain line breaks or tabs")
    if sanitize_string(name) != name:
        raise MemberNameError("name must not contain hidden or non-canonical characters")
    # ``.`` and ``..`` are URL path segments every client normalizes away before
    # the request leaves the browser, so a crew so named could be created but
    # never addressed at ``/api/agents/{name}`` for edit or delete.
    if not name.strip("."):
        raise MemberNameError("name must not consist only of periods")
    return name


#: Longest base a derived crew id keeps, leaving room for a ``-<n>`` suffix
#: inside ``_AGENT_NAME_RE``'s 64-character cap.
_CREW_ID_BASE_MAX_CHARS = 56

#: Suffixes tried before a derived crew id falls back to the name's hash.
_CREW_ID_SUFFIX_ATTEMPTS = 99


def is_crew_id(value: object) -> bool:
    """Return whether *value* is shaped like a new crew's config key.

    A crew id is the identifier grammar crews carried before free-form names
    (``validation._AGENT_NAME_RE``). New crews are keyed by one; a crew already
    stored under a free-form name stays readable and is never renamed.
    """
    return isinstance(value, str) and bool(_AGENT_NAME_RE.match(value))


def crew_id_for_display_name(name: str, taken: Container[str]) -> str:
    """Derive an unused crew id for a crew the user named *name*.

    The id is a slug of the name, suffixed ``-2``, ``-3``... past any id in
    *taken*. A name with no slug-safe characters (CJK, emoji) derives from its
    hash, so two such names never share an id. Deterministic for a given
    *name* and *taken*.
    """
    base = slugify(name)[:_CREW_ID_BASE_MAX_CHARS].strip("-")
    if not base or base == slug_hash_fallback(name, "artifact"):
        base = slug_hash_fallback(name, "crew")
    for n in range(1, _CREW_ID_SUFFIX_ATTEMPTS + 1):
        candidate = base if n == 1 else f"{base}-{n}"
        if candidate not in taken and is_crew_id(candidate):
            return candidate
    return slug_hash_fallback(name, "crew")


@dataclass(frozen=True)
class NewCrewKey:
    """How a crew being created is keyed and labelled.

    ``taken`` is the name the user would see twice when the create must be
    refused (``409 agent_exists``), else ``""``.
    """

    key: str
    display_name: str
    taken: str


def key_new_crew(name: str, display_name: str, agents: Mapping[str, object]) -> NewCrewKey:
    """Decide the config key and label for a crew created as *name*.

    An id-shaped *name* is the key as sent. A free-form one is kept as the
    label of an id :func:`crew_id_for_display_name` derives. An explicit
    *display_name* is the label either way. The create is refused when the
    key is taken, or when the name the crew would show (its label, else its
    key) is exactly what another crew already shows. The one decision both
    create surfaces (``POST /api/agents``, ``kirocrew agent create``) share.
    """
    if is_crew_id(name):
        key, label = name, display_name
        if key in agents:
            return NewCrewKey(key, label, name)
    else:
        key, label = crew_id_for_display_name(name, agents), display_name or name
    shown = label or key
    if crew_showing(shown, agents):
        return NewCrewKey(key, label, shown)
    return NewCrewKey(key, label, "")


def crew_showing(name: str, agents: Mapping[str, object], *, exclude: str = "") -> str:
    """The key of a crew other than *exclude* that shows *name*, else ``""``.

    A crew shows its ``display_name`` when it has one, else its key. Exact
    comparison, the same one :func:`key_new_crew` refuses a create on.
    """
    for key, agent in agents.items():
        if key != exclude and (getattr(agent, "display_name", "") or key) == name:
            return key
    return ""


class MemberNameTaken(MemberNameError):
    """Raised when another crew already shows the name a relabel would show."""

    def __init__(self, other: str) -> None:
        super().__init__(f"agent '{other}' already shows that name")
        self.other = other


# Named by the path callers import it from: a traceback or a log line renders the
# raised class as ``module.qualname``, so the owner module must not leak into it.
MemberNameTaken.__module__ = "kiro_crew.members"


def check_crew_label(key: str, label: str, agents: Mapping[str, object]) -> str:
    """Return *label* when crew *key* may show it, else raise :class:`MemberNameError`.

    ``""`` is a real value: the crew then shows its key. A non-empty label must
    pass :func:`validate_member_name` and must not be credential-shaped, since
    every roster surface and the crewmate's own prompt render it. Either way the
    name the crew would show must not be one another crew already shows
    (:class:`MemberNameTaken`), the rule a create is held to. Only the label is
    judged: the key, slug, thread and memory of the crew never move.
    """
    if label:
        validate_member_name(label)
        if external_text_requires_redaction(label):
            raise MemberNameError("name must not be credential-shaped text")
    other = crew_showing(label or key, agents, exclude=key)
    if other:
        raise MemberNameTaken(other)
    return label


def is_valid_member_name(value: object) -> bool:
    """Return whether *value* is a valid Crew Member display name."""
    try:
        validate_member_name(value)
    except MemberNameError:
        return False
    return True


def is_readable_member_name(value: object) -> bool:
    """Return whether an exact stored Crew Member name may identify local data."""
    if not isinstance(value, str) or len(value) > MEMBER_NAME_MAX_CHARS:
        return False
    if is_valid_member_name(value):
        return True
    nfc = normalize_unicode(value)
    return sanitize_string(value) == nfc and is_valid_member_name(nfc)


def is_dispatchable_member_name(value: object) -> bool:
    """Return whether a stored Crew Member name can safely reach a model.

    Legacy NFD spellings pass only when their NFC form is valid and both forms
    are safe to expose.
    """
    if not is_readable_member_name(value):
        return False
    assert isinstance(value, str)
    nfc = normalize_unicode(value)
    return not external_text_requires_redaction(value) and (
        nfc == value or not external_text_requires_redaction(nfc)
    )


# Reports the path callers import it from, as it did before it moved: an import
# table that pins each name's defining module reads this.
is_dispatchable_member_name.__module__ = "kiro_crew.members"


def is_configured_dispatchable_member(name: object, config=None) -> bool:
    """Return whether *name* is a stored Crew Member that can safely reach a model.

    The agent-choice guards (chat send, slot agent switch, OpenAI-compatible
    ``model``) admit a bare template or legacy name by the registered-agent
    grammar (``validation.is_registered_agent_name``: the slot grammar or a
    published dotted template). A free-form display name -- ``dr. eggbot`` --
    fails that grammar, so those guards admit it ONLY when it is a configured
    member: the roster lists exactly these names, and a string that is not a
    member key never reaches the template lookup as if it were a template. Loads
    the config only when called, so callers check the grammar first and pay this
    read on the free-form path alone.
    """
    if not isinstance(name, str) or not name:
        return False
    if config is None:
        # Function-local like ``member_slug`` below: ``config.loader`` imports
        # ``select_provider_backend`` from ``kiro_crew.members``, so a module-scope import
        # here would close the cycle.
        from kiro_crew.config.loader import KiroCrewConfig

        config = KiroCrewConfig.load()
    return name in config.agents and is_dispatchable_member_name(name)


def member_pin_matches(mode: object, current_agent: object, requested_agent: object) -> bool:
    """Return whether a request preserves a dispatchable Crew Member pin."""
    return (
        mode == DM_SLOT_MODE
        and is_dispatchable_member_name(current_agent)
        and isinstance(requested_agent, str)
        and requested_agent == current_agent
    )


def members_root() -> Path:
    """Root directory for member spaces.

    Uses :func:`data_home` rather than :func:`config_dir`: this is reached from
    request and chat paths, and ``config_dir`` re-runs start-of-process
    maintenance (including a destructive leftover sweep) on every call.
    """
    return data_home() / MEMBERS_DIR_NAME


def validate_slug(slug: str) -> str:
    """Return *slug* unchanged when it is well-formed, else raise.

    The pattern admits no ``/``, ``.`` or whitespace, so a validated slug cannot
    traverse out of :func:`members_root` on its own. :func:`member_dir` still
    re-checks containment, because validation and use are separated by a call
    boundary a future caller could bypass.
    """
    if not isinstance(slug, str) or not _SLUG_RE.match(slug):
        raise MemberSlugError(f"invalid member slug {slug!r}: must match {_SLUG_RE.pattern}")
    return slug


def slug_for_name(name: str) -> str:
    """Derive a candidate slug from a free-form member name.

    Not guaranteed unique: slugification is lossy, so two distinct member names
    can map to one slug. :func:`record_activity` stores the exact name in each
    entry so attribution survives that. Falls back to
    ``"member"`` when the name has no slug-safe characters, so a name written
    entirely in punctuation still yields something addressable.
    """
    base = slugify(name)
    # slugify hash-falls-back under its own module's noun; a member's stored
    # activity, rules, and DM bindings are addressed by this slug, so keep the
    # documented "member" fallback rather than adopting the artifact-prefixed
    # hash (which would also strand data recorded under "member").
    if base == slug_hash_fallback(name, "artifact"):
        base = "member"
    return validate_slug(base)


def canonical_member_key(name: str, config=None) -> str:
    """The ROSTER's own spelling of *name*, or *name* unchanged when it has none.

    The roster is keyed by the display name somebody typed, so its keys carry case:
    ``Atlas``. Every per-member path is then derived from the key's ``member_id``
    through :func:`member_slug` -- and a caller holding ``atlas`` misses the key, gets
    no ``member_id``, and falls through to ``slug_for_name("atlas")``. That is a
    DIFFERENT slug from the one the member's own session writes under, so a write and
    a read of one crewmate land in two directories, both succeeding and neither
    seeing the other.

    An exact hit wins, so a roster that really holds two keys differing only in case
    keeps answering each of them as itself. A unique case-insensitive hit is then
    taken as the same member, because that is what it is. An AMBIGUOUS fold -- two
    keys, neither matching exactly -- returns *name* unchanged rather than picking
    one: those two already collide at the slug level, and the surfaces that care
    refuse that slug outright (``dashboard_slug_ambiguous``). Guessing here would
    turn a refusal into a write on whichever key sorted first.
    """
    if not isinstance(name, str) or not name:
        return name
    if config is None:
        from kiro_crew.config.loader import KiroCrewConfig

        config = KiroCrewConfig.load()
    agents = getattr(config, "agents", None) or {}
    if name in agents:
        return name
    folded = name.casefold()
    hits = [key for key in agents if isinstance(key, str) and key.casefold() == folded]
    return hits[0] if len(hits) == 1 else name


def member_slug(name: str, config=None) -> str:
    """Use persisted member identity; legacy members retain their existing slug."""
    if config is None:
        from kiro_crew.config.loader import KiroCrewConfig

        config = KiroCrewConfig.load()
    # Through the roster's own spelling first. A caller that holds `atlas` for the key
    # `Atlas` otherwise reads no `member_id` and derives a second slug for one member.
    agent = config.agents.get(canonical_member_key(name, config))
    member_id = getattr(agent, "member_id", "") if agent else ""
    return validate_slug(member_id) if member_id else slug_for_name(name)


def _stable_member_slug(slug: str, name: str) -> bool:
    from kiro_crew.config.loader import KiroCrewConfig

    cfg = KiroCrewConfig.load()
    agent = cfg.agents.get(canonical_member_key(name, cfg))
    return bool(agent and getattr(agent, "member_id", "") == slug)


def member_dir(slug: str) -> Path:
    """Absolute path to one member's directory, containment-checked.

    Does NOT create the directory; :func:`record_activity` creates it on demand.
    """
    validate_slug(slug)
    root = members_root().resolve()
    target = (root / slug).resolve()
    # Defence in depth behind validate_slug: a symlinked root, or a future
    # caller that skipped validation, must not land outside the members root.
    if target != root and root not in target.parents:
        raise MemberSlugError(f"member slug {slug!r} escapes {root}")
    return target


#: Written into a member's directory when the member owes its user a first
#: welcome: Mate, created by the first-crewmate step, and a crewmate the
#: Crewmates page's New crewmate card creates. A member that arrived any other
#: way (discovery, an import, an app, an API create without ``first_greeting``)
#: has no such file and never greets through it.
WELCOME_OWED_FILENAME = "welcome_owed.json"

#: The owed record's ``kind`` for Mate's welcome (no ``kind`` key at all).
WELCOME_KIND_MATE = ""

#: The owed record's ``kind`` for a created crewmate: its first message asks
#: the user what it should work on.
WELCOME_KIND_GOAL = "goal"


def mark_welcome_owed(name: str, *, config=None, kind: str = WELCOME_KIND_MATE) -> bool:
    """Record that the just-created member *name* owes a first welcome.

    Called after the member is published: by the first-crewmate step for Mate,
    and by ``POST /api/agents`` with ``first_greeting`` (*kind*
    :data:`WELCOME_KIND_GOAL`). Best effort: a failure is logged and the member
    simply never greets, which is the safe direction. The record names the
    member, so a directory a different member later reaches under the same slug
    is not mistaken for this one's. Blocking IO.
    """
    record: dict[str, str] = {"member": name}
    if kind:
        record["kind"] = kind
    try:
        path = member_dir(member_slug(name, config)) / WELCOME_OWED_FILENAME
        path.parent.mkdir(parents=True, exist_ok=True)
        # Written at boot, before the gateway's own owner-only sweep runs, so the
        # chain this creates is tightened here (members/ and members/<slug>/).
        for _dir in (path.parent, path.parent.parent):
            platform_compat.restrict_dir_to_owner(_dir)
        atomic_write(path, json.dumps(record) + "\n")
    except Exception:
        logger.warning("Could not record the first welcome owed by %r", name, exc_info=True)
        return False
    return True


def owed_welcome_kind(slug: str, member: str) -> str | None:
    """The kind of first welcome *member* (at *slug*) owes, ``None`` for none.

    Only a record written for this exact member counts; a missing, unreadable or
    foreign record owes nothing. A record whose ``kind`` is not one this
    gateway knows owes nothing either. Blocking IO.
    """
    try:
        raw = read_bytes_with_retry(member_dir(slug) / WELCOME_OWED_FILENAME, max_bytes=4096)
        record = json.loads(raw)
    except (OSError, ValueError, MemberSlugError):
        return None
    if not isinstance(record, dict) or record.get("member") != member:
        return None
    kind = record.get("kind", WELCOME_KIND_MATE)
    return kind if kind in (WELCOME_KIND_MATE, WELCOME_KIND_GOAL) else None


def welcome_owed(slug: str, member: str) -> bool:
    """Whether *member* (at *slug*) owes a first welcome of any kind. Blocking IO."""
    return owed_welcome_kind(slug, member) is not None
