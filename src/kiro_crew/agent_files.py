"""Canonical filenames of the agent configs KiroCrew generates.

Single source of truth for the on-disk names KiroCrew writes into
``~/.kiro/agents/`` (kiro specs). This is a **leaf module** (no intra-package
imports) so both
``agent.py`` (which writes these files) and ``browser/setup.py`` (whose
Playwright convergence sweep must touch only KiroCrew-owned files) can import it
without an import cycle — ``agent.py`` imports ``converge_playwright_servers``
from ``browser/setup.py``, so ``browser/setup.py`` cannot import ``agent.py``.

Keeping the names here — rather than duplicating them as a literal in each
consumer — means adding a new managed agent spec is a one-line change in ONE
place: add its filename to ``OWNED_KIRO_AGENT_FILES`` and every consumer
(including the boot-time self-heal sweep) picks it up.
"""

from __future__ import annotations

# The primary KiroCrew agent spec.
AGENT_FILENAME = "kirocrew.json"

# Background/auxiliary managed agent specs KiroCrew writes under ~/.kiro/agents/.
LITE_AGENT_FILENAME = "kirocrew-lite.json"
CONDUCTOR_AGENT_FILENAME = "kirocrew-conductor.json"
PIPELINE_CONDUCTOR_AGENT_FILENAME = "kirocrew-pipeline-conductor.json"
# The goal conductor's work-ledger variant, and a SEPARATE spec rather than a flag
# on ``kirocrew-conductor``. The ledger flow inverts that agent's dispatch order
# (bind before seed) and replaces its patrol cycle (a ledger read instead of a
# transcript read), so mounting it on the shipped conductor would move every
# existing conductor user onto a different procedure without their asking. Same
# installer and the same no-file-write properties; what differs is the
# ``kirocrew-work`` mount and the prompt that drives it.
LEDGER_CONDUCTOR_AGENT_FILENAME = "kirocrew-ledger-conductor.json"
SECURITY_CONDUCTOR_AGENT_FILENAME = "kirocrew-security-conductor.json"
WORKER_AGENT_FILENAME = "kirocrew-worker.json"
KNOWLEDGE_AGENT_FILENAME = "kirocrew-knowledge.json"
RESEARCH_AGENT_FILENAME = "kirocrew-research.json"
HEARTBEAT_AGENT_FILENAME = "kirocrew-heartbeat.json"

# Collective allowlists — the EXACT filenames KiroCrew owns in each dir. Used by
# the Playwright convergence sweep (browser/setup.py) so it rewrites only files
# KiroCrew generates, never a user's own agent config that happens to share a
# prefix (e.g. a hand-authored ``kirocrew-custom.json``).
OWNED_KIRO_AGENT_FILES = (
    AGENT_FILENAME,
    LITE_AGENT_FILENAME,
    CONDUCTOR_AGENT_FILENAME,
    PIPELINE_CONDUCTOR_AGENT_FILENAME,
    LEDGER_CONDUCTOR_AGENT_FILENAME,
    SECURITY_CONDUCTOR_AGENT_FILENAME,
    WORKER_AGENT_FILENAME,
    KNOWLEDGE_AGENT_FILENAME,
    RESEARCH_AGENT_FILENAME,
    HEARTBEAT_AGENT_FILENAME,
)

# The specs that MUST exist for the product to work at all. kiro-cli resolves an
# agent by reading ``<agents dir>/<name>.json``; with the file absent it answers
# every ``session/set_mode`` with "Mode '<name>' not found", so a missing entry
# here fails EVERY turn rather than degrading one feature:
#   * ``kirocrew.json``      — the agent behind user-facing chat.
#   * ``kirocrew-lite.json`` — the cheap background agent (auto-titles,
#     compaction, heartbeat), reached via ``SessionManager.get_bg_session``.
# The remaining OWNED_KIRO_AGENT_FILES entries are deliberately excluded: their
# installers in ``agent.py`` already degrade to ``logger.debug`` on failure
# because each one only disables its own feature (goal conducting, Knowledge
# extraction, Research Lab, unattended heartbeat polling).
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
# when choosing a stem, and the KAS projection refuses the id with the remedy
# instead of letting the misleading "spec is likely missing" refusal fire, or
# letting a built-in silently run in the crewmate's place.
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
