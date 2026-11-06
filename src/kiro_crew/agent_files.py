"""Canonical filenames of the agent configs KiroCrew generates.

Single source of truth for the names Kiro Crew writes into ``~/.kiro/agents/``.
A **leaf module** (no intra-package imports): ``agent.py`` imports
``converge_playwright_servers`` from ``browser/setup.py``, so ``browser/setup.py``
cannot import ``agent.py`` and reads the names from here instead.

A new managed spec is added to ``OWNED_KIRO_AGENT_FILES`` and every consumer
(including the boot-time self-heal sweep) picks it up.
"""

from __future__ import annotations

# The primary KiroCrew agent spec.
AGENT_FILENAME = "kirocrew.json"

# Background/auxiliary managed agent specs KiroCrew writes under ~/.kiro/agents/.
LITE_AGENT_FILENAME = "kirocrew-lite.json"
# The tool-less agent a non-operator channel sender talks to. Its own file so the
# background helper (lite) may grow a tool one day without handing it to a guest.
GUEST_AGENT_FILENAME = "kirocrew-guest.json"
CONDUCTOR_AGENT_FILENAME = "kirocrew-conductor.json"
PIPELINE_CONDUCTOR_AGENT_FILENAME = "kirocrew-pipeline-conductor.json"
# The goal conductor's work-ledger variant: a SEPARATE spec, not a flag on
# ``kirocrew-conductor``, because the ledger flow inverts its dispatch order (bind
# before seed) and patrols the ledger instead of transcripts; a flag would move
# every existing conductor user onto a different procedure. It differs only in
# the ``kirocrew-work`` mount and its prompt.
LEDGER_CONDUCTOR_AGENT_FILENAME = "kirocrew-ledger-conductor.json"
SECURITY_CONDUCTOR_AGENT_FILENAME = "kirocrew-security-conductor.json"
WORKER_AGENT_FILENAME = "kirocrew-worker.json"
# The mate conductor: the agent a crewmate's own chat runs on. Derived from the
# default template (same prompt, tools and mcpServers), so it behaves like
# `kirocrew.json` under its own name. It does not read or write
# `~/.kiro/mates/agents/` (docs/request-for-change/rfc-mate-owned-agent-config.md).
MATE_CONDUCTOR_AGENT_FILENAME = "kirocrew-mate-conductor.json"
# The mate worker: the agent a crewmate's worker sessions run on (the session a
# crewmate's own chat opens with ``session_create``). Source-defined like the
# conductor; a crewmate's own file under ``~/.kiro/mates/agents/`` adds to it at
# projection time (``kiro_crew.mate_worker``), never on disk here.
MATE_WORKER_AGENT_FILENAME = "kirocrew-mate-worker.json"
#: The two crewmate specs Crew generates. They live in ``~/.kiro/mates/agents/``
#: beside the crewmates' own files, NOT in ``~/.kiro/agents/``: kiro-cli lists
#: every spec in its agents folder, and these two are never picked by hand. The
#: KAS projection resolves that folder after the user level, and reads these two
#: names there first (``acp/harness/kas.resolve_projected_spec``). Being in that
#: folder, their stems are reserved: no crewmate may own a file by either name
#: (``mate_config.write_mate_spec``).
MATE_SERVICE_AGENT_FILENAMES = (MATE_CONDUCTOR_AGENT_FILENAME, MATE_WORKER_AGENT_FILENAME)
MATE_SERVICE_AGENT_NAMES = frozenset(f[: -len(".json")] for f in MATE_SERVICE_AGENT_FILENAMES)


def is_mate_service_agent(name: object) -> bool:
    """Whether *name* is the mate conductor or the mate worker (case-folded)."""
    return isinstance(name, str) and name.casefold() in MATE_SERVICE_AGENT_NAMES


# The dashboard-author crewmate: authors ONE dashboard template and lands it as a
# pull request. Its own file because its tool surface fits neither the worker nor a
# conductor: it writes files and drives git, but mounts neither the work-ledger set
# nor the publication surface. Prompt and charter come from the shipped
# ``dashboard-template/agent-spec.md``; the installer is in ``worker_agent.py``.
DASHBOARD_AUTHOR_AGENT_FILENAME = "kirocrew-dashboard-author.json"
KNOWLEDGE_AGENT_FILENAME = "kirocrew-knowledge.json"
RESEARCH_AGENT_FILENAME = "kirocrew-research.json"
# The team lead: owns a goal and does the focused work on it itself. A DEFAULT
# agent like research: same ceiling, same install sequence, overwritten on every
# boot, so a customized copy needs a different name.
TEAM_LEAD_AGENT_FILENAME = "kirocrew-team-lead.json"
HEARTBEAT_AGENT_FILENAME = "kirocrew-heartbeat.json"
DASHBOARD_MANAGER_AGENT_FILENAME = "kirocrew-dashboard-manager.json"

# The dashboard manager's agent NAME, derived from its filename: kiro-cli resolves
# ``<agents dir>/<name>.json``, and the member base prompt routes page work to this
# name, so a drifted name would fail the turn. It lives here because
# ``context_assembly.member`` needs it and importing ``agent.py`` would be a cycle.
DASHBOARD_MANAGER_AGENT_NAME = DASHBOARD_MANAGER_AGENT_FILENAME[: -len(".json")]
# The config key of the first crewmate every install creates once (shown as
# Mate). An ordinary crewmate on the ordinary crewmate template: renameable,
# deletable and never re-created. ``display_name`` is the label the user sees and
# may change.
ASSISTANT_MEMBER_NAME = "mate"
#: The label the first crewmate shows while its ``display_name`` is empty.
ASSISTANT_DEFAULT_DISPLAY_NAME = "Bot"


#: Agent names a chat session treats as "the kirocrew default" for context
#: assembly. ``context_assembly`` guards workspace identity, the docs pointer and
#: steering re-injection behind an ``is_custom`` check ("did a user replace the
#: default agent"), keyed on NAME, not content. The mate conductor and mate
#: worker are Crew-owned copies of the default template, so they are in the set;
#: otherwise a crewmate's sessions would lose those blocks just for having their
#: own filename. ``kirocrew-research``/``kirocrew-worker``/``kirocrew-team-lead``
#: are backend loops with their own context rules and stay out.
KIROCREW_DEFAULT_AGENT_NAMES = frozenset(
    {
        AGENT_FILENAME[: -len(".json")],
        "kirocrew-mate-conductor",
        MATE_WORKER_AGENT_FILENAME[: -len(".json")],
    }
)


def is_kirocrew_default_agent(agent: object) -> bool:
    """Whether *agent* is one of :data:`KIROCREW_DEFAULT_AGENT_NAMES`.

    Every ``is_custom`` check in ``context.py`` and ``context_assembly/`` reads
    this instead of repeating the name literal.
    """
    return agent in KIROCREW_DEFAULT_AGENT_NAMES


def is_assistant_member(name: object, entry: object) -> bool:
    """True when *name* is the first crewmate's key. *entry* is not consulted.

    Kept as a predicate over ``(name, entry)`` so callers read the same way they
    read any member test; the first crewmate is identified by its key alone,
    whatever template it is bound to.
    """
    del entry
    return name == ASSISTANT_MEMBER_NAME


# Collective allowlists — the EXACT filenames KiroCrew owns in each dir. Used by
# the Playwright convergence sweep (browser/setup.py) so it rewrites only files
# KiroCrew generates, never a user's own agent config that happens to share a
# prefix (e.g. a hand-authored ``kirocrew-custom.json``).
OWNED_KIRO_AGENT_FILES = (
    AGENT_FILENAME,
    LITE_AGENT_FILENAME,
    GUEST_AGENT_FILENAME,
    CONDUCTOR_AGENT_FILENAME,
    PIPELINE_CONDUCTOR_AGENT_FILENAME,
    LEDGER_CONDUCTOR_AGENT_FILENAME,
    SECURITY_CONDUCTOR_AGENT_FILENAME,
    WORKER_AGENT_FILENAME,
    MATE_CONDUCTOR_AGENT_FILENAME,
    MATE_WORKER_AGENT_FILENAME,
    DASHBOARD_AUTHOR_AGENT_FILENAME,
    KNOWLEDGE_AGENT_FILENAME,
    RESEARCH_AGENT_FILENAME,
    TEAM_LEAD_AGENT_FILENAME,
    HEARTBEAT_AGENT_FILENAME,
    DASHBOARD_MANAGER_AGENT_FILENAME,
)

# The specs that MUST exist for the product to work at all. kiro-cli resolves an
# agent by reading ``<agents dir>/<name>.json``; with the file absent it answers
# every ``session/set_mode`` with "Mode '<name>' not found", so a missing entry
# here fails EVERY turn rather than degrading one feature:
#   * ``kirocrew.json``      — the agent behind user-facing chat.
#   * ``kirocrew-lite.json`` — the cheap background agent (auto-titles,
#     compaction, heartbeat), reached via ``SessionManager.get_bg_session``.
# The remaining OWNED_KIRO_AGENT_FILES entries are deliberately excluded because
# each one only disables its own feature (guest replies, goal conducting,
# Knowledge extraction, Research Lab, unattended heartbeat polling). Their
# installer calls in ``agent_parts/spec_rebuild.py`` degrade to ``logger.debug`` on failure, except
# ``kirocrew-guest.json``, which ``_install_aim_capabilities`` writes unguarded
# after the lite fallback.
REQUIRED_KIRO_AGENT_FILES = (
    AGENT_FILENAME,
    LITE_AGENT_FILENAME,
)

# Agent ids the KAS engine (``kiro-cli acp --agent-engine v3``) will not run
# from the wire. A ``_meta.kiro.customAgents`` entry under one of these ids is
# accepted without an error and then either never appears in the session's
# ``availableModes`` or is shadowed by the engine's own built-in agent of that
# id, so a ``session/set_mode`` for it is refused, or activates the built-in
# instead of the definition Crew sent. Measured against kiro-cli 2.23.0 by
# injecting every id below in ONE ``session/new``, each with a distinctive
# ``description``, and reading the advertised mode back:
#
#   injected id                       advertised mode         definition that runs
#   ------------------------------    --------------------    --------------------
#   default                           none (dropped)          none: set_mode refused
#   vibe / spec / quick-spec /        the built-in            the built-in; the
#     bug-fix / plan / autonomous     (origin: bundled)       client entry discarded
#   Default / DEFAULT / Vibe / Spec   the client entry        the client entry
#     / PLAN / kiro_default / kiro    (origin: client)
#     / kiro-default / default_agent
#   kirocrew-conductor /              the client entry        the client entry: the
#     semantic_reviewer (also on      (origin: client)        wire definition replaces
#     disk, advertised from there)                            the on-disk one
#
# The match is therefore exact and case-sensitive: the built-in ids are all
# lower-case, and every case variant registers as an ordinary client agent.
#
# Why this lives with the FILENAMES: Kiro Crew names a crewmate's private
# template copy after the crewmate, and the seeded first crewmate is called
# ``default``, so without this set its first template edit writes
# ``~/.kiro/agents/default.json`` and binds the crewmate to an id that can never
# be activated on KAS. The template writers (fork, publish, create) consult it
# when choosing a stem. A spec that already carries one of these ids -- written
# before the writers did, or a kiro-cli setup carried over to KAS -- is sent to
# the engine under :func:`kas_wire_agent_id` instead, so it runs unchanged.
KAS_RESERVED_AGENT_IDS = frozenset(
    {
        "default",
        "vibe",
        "spec",
        "quick-spec",
        "bug-fix",
        "plan",
        "autonomous",
    }
)

#: Appended to a reserved id to make the id the KAS engine sees. A hyphenated
#: lower-case id is the shape measured above to register as a client agent
#: (``kiro-default``).
KAS_WIRE_ID_SUFFIX = "-crewmate"


def kas_wire_agent_id(agent_id: str) -> str:
    """The id *agent_id* is registered and activated under on the KAS wire.

    Itself for every id but :data:`KAS_RESERVED_AGENT_IDS`, which the engine
    drops or shadows; those get :data:`KAS_WIRE_ID_SUFFIX`. Applied only where
    the id crosses the wire (the ``customAgents`` batch, ``set_mode``, the
    advertised modes read back), so the rest of Kiro Crew keeps the crewmate's
    own id and kiro-cli, which reserves none of these, never sees the alias.

    A session registers exactly one agent, so the alias cannot meet a second
    entry of the same id in its batch; an on-disk spec that happens to carry it
    is replaced by the wire definition (the ``kirocrew-conductor`` row above).
    """
    if agent_id in KAS_RESERVED_AGENT_IDS:
        return f"{agent_id}{KAS_WIRE_ID_SUFFIX}"
    return agent_id
