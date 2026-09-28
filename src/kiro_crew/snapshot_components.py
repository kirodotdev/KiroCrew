"""What a snapshot bundle can carry, and where each component lives in the data home.

The component table is the single source of truth both directions read: staging copies
what a component declares, and restore applies what the bundle's manifest says rode. A
rule about which paths belong to a component, which paths never ship, and how each
component is restored therefore has one home here. Everything is data or a pure question
about it, plus the one filesystem check every component tree root goes through,
:func:`safe_tree_root`.
"""

from __future__ import annotations

import sys
from dataclasses import dataclass
from enum import Enum
from pathlib import Path, PurePosixPath
from types import ModuleType

from kiro_crew import platform_compat
from kiro_crew.memory_stores import (
    MEMORY_STORES_DIR_NAME,
    is_host_local_store_state,
    named_store_product_file,
)

# Files that must always have 0o600 permissions in snapshots and on restore.
SECURITY_SENSITIVE_FILES: frozenset = frozenset({"sel_hmac.key", "telemetry_salt"})


# Files that must NEVER ride a snapshot: sel_hmac.key is regenerated on restore
# so audit-log HMACs stay bound to the host that wrote them.
#
# This set is matched by BASENAME inside `_data_filter`, which runs over the
# ENTIRE tar — including the staged workspace/, plan_memory/ and skills/ trees.
# So any name added here also silently drops a USER file that happens to share
# it. Keep the set minimal for that reason.
#
# The beacon's per-install identity (beacon_install_id / beacon_last_sent) is
# deliberately NOT here: snapshot staging copies an explicit per-component file
# list (CORE_FILES) plus those three directories, and no component lists a beacon
# file, so a root beacon file is never staged in the first place. The
# id-cloning hazard is closed by that non-selection, not by a basename filter.
NEVER_SNAPSHOT_FILES: frozenset = frozenset({"sel_hmac.key"})


#: Data-home-relative paths, besides the host-local half of ``memory_stores/``, that no
#: bundle carries in either direction. ``crew-teams/.lock`` is the advisory lock file
#: `crew_teams.document_lock` opens around every write: empty, this host's runtime state,
#: and recreated by the first locked write on the restoring host. Matched by POSITION, not
#: by name -- ``.lock`` is a name an operator's workspace can easily hold.
_HOST_LOCAL_PATHS: frozenset[tuple[str, ...]] = frozenset({("crew-teams", ".lock")})


def _is_host_local(rel_parts: tuple[str, ...]) -> bool:
    """Is the data-home-relative path *rel_parts* this host's own runtime state?"""
    return tuple(rel_parts) in _HOST_LOCAL_PATHS or is_host_local_store_state(rel_parts)


def _never_ships(member_name: str) -> bool:
    """Is this archive member one no bundle carries in EITHER direction?

    Two rules, one by basename and one by path. `NEVER_SNAPSHOT_FILES` is the basename
    rule above. The path rule is the host-local state under ``memory_stores/`` --
    the member signing key, the execution logs and the local backup directories -- which
    is matched by its position rather than its name for the reason the basename set's own
    comment gives: a name filter over the whole tar drops an operator's file that merely
    shares the name, and ``backups`` is a name an operator's workspace can easily hold.

    Members are ``<bundle-root>/<data-home-relative path>``, so the root is dropped before
    the predicate that speaks data-home-relative paths is asked.
    """
    parts = PurePosixPath(member_name).parts
    if parts and parts[-1] in NEVER_SNAPSHOT_FILES:
        return True
    return _is_host_local(parts[1:])


def _tree_roots_replace_clears() -> frozenset[str]:
    """The top-level directories replace mode removes before refilling.

    Derived from the component table rather than listed: a tree added to a component
    is a tree replace clears, and a hand-kept copy would leave extraction's rejection
    filter (``snapshot_archive._rejection_recording_filter``) blind to exactly the tree
    that was added last.
    """
    return frozenset(PurePosixPath(t).parts[0] for s in COMPONENTS.values() for t in s.trees)


class Purpose(str, Enum):
    """Why a bundle exists. Decides which components may ride in it.

    A bundle's purpose is not cosmetic: ``BACKUP`` restores onto a replacement host
    the operator already controls, so it wants the credentials that make recovery
    turnkey. ``SHARE`` leaves the operator's control, so a component that carries
    credential material must not ride in one. Recording the purpose in the manifest
    is what lets a reader of a bundle know which of the two they are holding.
    """

    BACKUP = "backup"
    SHARE = "share"


class SecretPolicy(str, Enum):
    """A component's declaration about the credential material it carries.

    Every component must declare one. There is deliberately no default: a component
    added without a declaration is refused at staging (see :func:`resolve_components`)
    rather than inheriting whichever value happens to be permissive.

    ``UNRESOLVED`` means nobody has established that the component is safe to hand to
    another person. It rides a ``BACKUP`` bundle unchanged and is refused outright in
    a ``SHARE`` bundle.

    ``SHARE_SAFE`` means someone has, and **no component claims it today**. That is
    not an oversight. Whether a component is safe to share is a question about
    CONTENT, not structure: a workspace file, a skill, a cron's ``env`` map, a
    notification body or a pasted lesson can each contain a token, and staging cannot
    tell. Two components were flipped from a guessed-safe value to ``UNRESOLVED``
    during review of this change, one at a time, before the pattern was obvious. The
    value is kept so the seam has both sides and the gate stays exercised; the first
    genuinely certified component will arrive with the redaction work that earns it.
    """

    SHARE_SAFE = "share-safe"
    UNRESOLVED = "unresolved"


@dataclass(frozen=True)
class ComponentSpec:
    """What one component stages, and its credential declaration.

    ``files`` are data-home-relative files copied individually; ``trees`` are
    data-home-relative directories copied wholesale. A ``.db`` file in ``files`` is
    copied through the SQLite backup API rather than the filesystem, so a live
    gateway holding the database open still yields a consistent copy.
    """

    policy: SecretPolicy
    help: str
    files: tuple[str, ...] = ()
    trees: tuple[str, ...] = ()


# The single source of truth for what a bundle can contain. Both the staging path
# and the restore path read this, so a component cannot be stageable but
# unrestorable (or the reverse) without the mismatch being visible here.
COMPONENTS: dict[str, ComponentSpec] = {
    # Self-contained on purpose: lessons and semantic/episodic recall live in the two
    # databases, but the markdown half of memory lives under workspace/. Naming those
    # trees here means restoring memory does not require restoring the whole
    # workspace, which on a real install is two orders of magnitude larger.
    #
    # `memory_stores/` is the NAMED stores -- one silo per crew member, each holding its
    # own markdown tree, FTS index, vector file, lessons and ownership manifest. It is a
    # component tree like the other two, so a bundle that declares `memory` carries every
    # store and not only the default one. The tree's host-local half (the member signing
    # key, execution logs, local backup directories) never rides: `_never_ships` names
    # it, and the same predicate excludes it at staging and at extraction.
    "memory": ComponentSpec(
        # UNRESOLVED like every other component: a lesson or a note can contain a
        # token somebody pasted, and staging cannot tell. Memory is NOT redacted in a
        # backup -- that is the whole point of backing it up -- this declaration only
        # governs whether it may ride a bundle that leaves the operator's control.
        policy=SecretPolicy.UNRESOLVED,
        help=(
            "memory.db, memory_index.db (semantic, episodic, lessons), "
            "workspace/memory/ (preferences, projects, history), workspace/knowledge/ "
            "(files; the knowledge database is replaced, not row-merged), "
            "memory_stores/ (every named store's memory; a store's database is "
            "replaced, not row-merged)"
        ),
        files=("memory.db", "memory_index.db"),
        trees=("workspace/memory", "workspace/knowledge", MEMORY_STORES_DIR_NAME),
    ),
    "crons": ComponentSpec(
        # `CronJob.env` is a persisted dict of per-job environment variables
        # (cron.py), so a job passing an API token carries it in crons.json.
        policy=SecretPolicy.UNRESOLVED,
        help="crons.json (scheduled jobs)",
        files=("crons.json",),
    ),
    "config": ComponentSpec(
        policy=SecretPolicy.UNRESOLVED,
        help="config.json, session_map.json, hooks.json, project_dir, workspace_dir",
        files=("config.json", "session_map.json", "hooks.json", "project_dir", "workspace_dir"),
    ),
    "skills": ComponentSpec(
        policy=SecretPolicy.UNRESOLVED,
        help="skills/ directory",
        trees=("skills",),
    ),
    # The crewmate team list: `crew_teams.TEAMS_DIR_NAME` / `TEAMS_FILE_NAME`, one JSON
    # document naming crews that live in config.json. A tree rather than a flat file so the
    # restore paths that already exist for a whole tree carry it; the directory's lock
    # file is host-local and never rides (`_HOST_LOCAL_PATHS`). Team names are the
    # operator's, so UNRESOLVED like every other component.
    "crew-teams": ComponentSpec(
        policy=SecretPolicy.UNRESOLVED,
        help="crew-teams/ directory (teams.json, the crewmate team list)",
        trees=("crew-teams",),
    ),
    "workspace": ComponentSpec(
        policy=SecretPolicy.UNRESOLVED,
        help="workspace/, plan_memory/ directories",
        trees=("workspace", "plan_memory"),
    ),
    "notifications": ComponentSpec(
        policy=SecretPolicy.UNRESOLVED,
        help="notifications.jsonl (notification history)",
        files=("notifications.jsonl",),
    ),
    "security": ComponentSpec(
        policy=SecretPolicy.UNRESOLVED,
        help="telemetry_salt (sel_hmac.key excluded — regenerated on restore)",
        files=("telemetry_salt",),
    ),
    # The artifact library: every report, log, diff and generated file an agent or the
    # operator saved. `artifact_folders.json`, the index naming which folder each one sits
    # in, is NOT here: it is a record format whose consumers read fields off each entry,
    # so carrying it means answering what a restore does with a record those consumers
    # cannot use -- a question with its own answer and its own tests. The tree is the data
    # and is what a restored host is missing; an artifact whose folder the destination does
    # not have is shown at the root, which the folder store already does for any id it does
    # not know. The index is tracked as follow-up work rather than carried half-answered.
    "artifacts": ComponentSpec(
        # UNRESOLVED like every other component, and for the plainest reason in the
        # table: an artifact is whatever someone saved. A pasted log, a captured
        # response, a generated script -- staging cannot tell which of them holds a
        # token, so nobody has established that this is safe to hand to another person.
        policy=SecretPolicy.UNRESOLVED,
        help="artifacts/ directory (the artifact library; folder assignments not carried)",
        trees=("artifacts",),
    ),
    "uploads": ComponentSpec(
        # Files the operator handed to the product from their own disk. Same reasoning.
        policy=SecretPolicy.UNRESOLVED,
        help="uploads/ directory (files uploaded through the dashboard and apps)",
        trees=("uploads",),
    ),
}


class ComponentRefused(Exception):
    """A requested component cannot ride a bundle of the requested purpose."""


def resolve_components(requested: list[str] | None, purpose: Purpose) -> list[str]:
    """Return the component names to stage, or raise :class:`ComponentRefused`.

    ``None`` means every component. The two refusals are the seam's whole point:
    an unknown name never silently stages nothing, and an ``UNRESOLVED`` component
    never rides a ``SHARE`` bundle just because nobody wrote the policy down.

    Duplicates are collapsed, ORDER PRESERVED. Without that, ``--components config,config``
    reaches the staging pass twice for one component, and the second pass hits the exclusive
    create the pinned primitives make -- an uncaught ``FileExistsError`` traceback rather
    than a snapshot. A repeated name is a typo, not a request to stage anything twice, so
    the honest reading is to collapse it rather than to refuse the run.
    """
    names = list(COMPONENTS) if requested is None else list(dict.fromkeys(requested))
    unknown = [c for c in names if c not in COMPONENTS]
    if unknown:
        raise ComponentRefused(
            f"unknown component(s): {', '.join(sorted(unknown))} "
            f"(known: {', '.join(sorted(COMPONENTS))})"
        )
    if purpose is Purpose.SHARE:
        blocked = [c for c in names if COMPONENTS[c].policy is SecretPolicy.UNRESOLVED]
        if blocked:
            raise ComponentRefused(
                f"component(s) {', '.join(sorted(blocked))} have no share-safe policy, "
                f"so they cannot ride a '{Purpose.SHARE.value}' bundle. Whether a "
                f"component is safe to hand to someone else is a question about its "
                f"CONTENT — a workspace file, a skill, a cron's env map or a pasted "
                f"lesson can each hold a token — and no component is certified yet. "
                f"Use --purpose {Purpose.BACKUP.value} to back up onto a host you "
                f"control."
            )
    return names


# Derived views, kept because callers and tests read them as the component tables.
CORE_FILES: dict[str, tuple[str, ...]] = {
    name: spec.files for name, spec in COMPONENTS.items() if spec.files
}


# Every core-file name, flattened across components. DERIVED, never hand-listed: recovery
# uses it to tell "this run would have created a regular FILE here" from "something else's
# directory is standing at that name", and a hand-kept copy of the list would drift from the
# component specs exactly when a new component is added -- which is when the distinction
# matters most.
CORE_FILES_FLAT: frozenset[str] = frozenset(f for files in CORE_FILES.values() for f in files)


# Core files that are DERIVED: regenerable from the payload they index, and dropped from an
# off-host bundle by the redaction pass for exactly that reason. Replace has to answer for
# the consequence -- see `_drop_derived_indexes_absent_from_bundle`.
#
# Duplicated from `snapshot_redact._DERIVED_INDEXES` rather than imported: the snapshot
# facade loads that module LAZILY (through `snapshot._redactor`), and an eager import for
# one frozenset would pull it onto the gateway's boot path. A test asserts the two sets
# agree, so a future divergence fails loudly instead of silently restoring a stale index.
_DERIVED_INDEXES: frozenset[str] = frozenset({"memory_index.db"})


# Component files whose consumers read a JSON OBJECT and degrade silently when they do
# not get one. `crons.json` is the sharpest case: its loader wraps `json.loads` in a
# `try` and falls back to "no jobs", and even a well-formed JSON *array* takes the
# `isinstance(data, dict) else []` branch — so a corrupt file discards every scheduled
# job while the restore reports success.
#
# Listed rather than derived, because "ends in .json" is not the property that matters:
# what matters is that a consumer treats an unreadable file as empty instead of as an
# error. A component file added here is validated before it can be installed.
COMPONENT_JSON_OBJECTS: frozenset[str] = frozenset(
    {
        "crons.json",
        "config.json",
        "session_map.json",
        "hooks.json",
    }
)


# Keys inside those files whose value must be a LIST OF OBJECTS, because a reader iterates
# the list and reads fields off each entry. An object at the top level is necessary and not
# sufficient: `{"jobs": ["x"]}` is a valid object whose consumer reaches `.get` on a `str`
# and raises halfway through a merge, with live state already partly changed.
_JSON_OBJECT_LISTS: dict[str, tuple[str, ...]] = {
    "crons.json": ("jobs",),
}


#: The one document a component TREE carries whose reader REFUSES a damaged file rather
#: than reading it empty: `crew_teams.read_teams` raises on anything its parser rejects,
#: which fails every team route and refuses every crew create with no in-product repair.
#: So a restore validates it with THAT parser (`crew_teams.read_document`), not with the
#: flat files' shape check -- a document the shape check admits and the parser refuses
#: (a bad id, an over-cap name, a newer version) would otherwise install and report success.
_TREE_DOCUMENT_VALIDATORS: tuple[tuple[str, str, str], ...] = (
    ("crew-teams", "crew-teams/teams.json", "crew_teams"),
)


#: Component trees that are restored as ONE DOCUMENT under their owner's lock, never as a
#: directory swap. `crew-teams/` holds the team document AND the lock file every writer
#: of that document holds (`crew_teams.document_lock`); removing and recreating the
#: directory would hand a writer already inside the lock a file nobody else can see, and
#: its next commit would land over the restored document. So replace, merge and rollback
#: all go through `crew_teams.install_document` / `remove_document`, which take the same
#: lock and touch only `teams.json`. Keyed by tree name; the value is the document's
#: bundle-relative path, which is also the rollback target's granularity.
_LOCKED_DOCUMENT_TREES: dict[str, str] = {"crew-teams": "crew-teams/teams.json"}


# The tree counterpart of CORE_FILES. Derived from the same specs so a component that
# gains a tree is covered by everything keyed on this without a second edit.
COMPONENT_TREES: dict[str, tuple[str, ...]] = {
    name: spec.trees for name, spec in COMPONENTS.items() if spec.trees
}


# How a component is RESTORED, which is not derivable from what it declares. Both restore
# modes read these instead of naming components inline, so a component added to one of them
# needs no new branch on either path -- and a component in neither is visibly unrestorable
# rather than silently skipped.
#
#: Components whose declared FILES are moved aside and replaced one by one. `memory` is here
#: for its two databases; its TREES are handled separately because they are nested, overlap
#: `workspace` and carry their own clear-then-refill ordering.
_CORE_FILE_COMPONENTS: tuple[str, ...] = (
    "memory",
    "crons",
    "config",
    "notifications",
    "security",
)


#: Components restored as whole trees: replace removes the live tree and writes the
#: archive's, merge copies in without overwriting. Every member declares trees ONLY -- a
#: component with a flat file belongs above, where a file is moved aside and replaced.
_WHOLE_TREE_COMPONENTS: tuple[str, ...] = (
    "workspace",
    "skills",
    "crew-teams",
    "artifacts",
    "uploads",
)


#: Components a MERGE does not restore, so it says so rather than importing them by halves.
#: The reason is the DATA's shape, not unfinished work, and :func:`_do_merge` states it at
#: the refusal. Replace restores both completely, which is why this is a mode restriction
#: and not a gap in the component.
_REPLACE_ONLY_COMPONENTS: tuple[str, ...] = ("artifacts", "uploads")


# Databases this product owns that live INSIDE a component tree rather than at the top
# level. Paths are relative to a bundle root, POSIX-separated.
#
# They cannot be derived from `ComponentSpec.files`, which names only top-level files, so
# they are listed. The list is what separates "our database, broken bundle" from "a `.db`
# the operator happens to keep in their own folder": everything here is validated as
# strictly as `memory.db`, and everything else under a tree is only checked when it opens
# as a database at all. A product database added under a tree and left off this list is
# validated leniently, which is the failure this comment exists to prevent.
PRODUCT_TREE_DATABASES: frozenset[str] = frozenset(
    {
        "workspace/knowledge/knowledge.db",
    }
)


def is_product_tree_database(rel: str) -> bool:
    """Is the bundle-relative path *rel* a database this product owns inside a tree?

    The set above lists the FIXED paths. A named store's vector file and index sit at
    ``memory_stores/<name>/memory.db`` and ``.../memory_index.db``, where ``<name>`` is
    the operator's, so they cannot be listed and are recognised by shape instead --
    through the layout owner, so this module holds no second spelling of a store path.
    Both answers carry the same consequence: strict validation on the way in and on the
    way out, exactly as for ``memory.db`` at the root.
    """
    return rel in PRODUCT_TREE_DATABASES or bool(named_store_product_file(PurePosixPath(rel).parts))


COMPONENT_HELP = {name: spec.help for name, spec in COMPONENTS.items()}


VALID_COMPONENTS: tuple[str, ...] = tuple(COMPONENTS)


def _facade() -> ModuleType:
    """``kiro_crew.snapshot``, read from ``sys.modules`` when called: the owners' patch target.

    The owner modules call the helpers and read the limits a test replaces on the facade --
    ``_copytree_safe``, ``_do_replace_mutations``, ``sqlite3``, ``_MAX_ARCHIVE_MEMBERS`` and
    the like -- through this, so a patch of one of those names on ``kiro_crew.snapshot``
    reaches the owners' call sites as it reaches the facade's own code. Every other name an
    owner uses resolves in that owner's own globals. The facade is looked up, never
    imported: it imports every owner and is the only way into them, so it is loaded before
    any owner function runs, and no owner depends on it by import. A stored reference would
    go stale when a test purges and re-imports the facade. A function that reads the facade
    in a loop resolves it once, before the loop.
    """
    return sys.modules["kiro_crew.snapshot"]


def _mc_dir() -> Path:
    # Use the shared resolver so snapshot/restore honor the documented
    # KIROCREW_HOME override (and the same ~/.kiro/crew default) as every other
    # module — not an undocumented KIROCREW_DIR, which would make snapshots
    # silently target the real home even when state was relocated.
    from kiro_crew.config.loader import config_dir

    return config_dir()


def safe_tree_root(root: Path, *, what: str, home: Path | None = None) -> Path | None:
    """Return *root* if it is the declared tree AND staying inside the data home.

    THE chokepoint for component tree roots. Three separate sites touch them — the
    staging walk, the replace pass and the merge pass — and each was found to
    dereference a link independently, so the check lives here once.

    Two INDEPENDENT properties are required, and neither implies the other:

    **Containment** — the fully resolved path is a strict descendant of the resolved
    home. This answers "can a read or write through this root land outside the
    directory we are allowed to touch". Checking whether the node itself is a link
    does not answer it: a link nested under the root, or an ancestor of it, escapes
    while every individual node looks ordinary. ``Path.resolve()`` follows every link
    in the path, so the comparison covers roots, ancestors, descendants and Windows
    junctions at once. Equality with the home is refused, not allowed: a link like
    ``workspace/memory -> ..`` resolves to the home itself, which would make the
    "component tree" the whole home and sweep ``.env`` and ``sel_hmac.key`` into an
    archive meant to carry memory. No declared component tree is ever the home.

    **Identity** — no path segment from the home down to the root is a link. This
    answers a different question: "is this the tree the component declared". A link
    that redirects to another subtree INSIDE the home satisfies containment perfectly
    — ``workspace/memory -> ../apps`` resolves to a strict descendant — while
    silently changing WHICH data is archived. Because these bundles are uploaded, a
    redirect is an exfiltration primitive, not a mix-up: the archive would carry
    whatever the link points at under the name of the component that was asked for.
    Containment cannot see this, because nothing left the home.
    """
    base = (home or _facade()._mc_dir()).resolve()
    try:
        resolved = root.resolve()
    except OSError as e:  # broken link, ELOOP, permission on an ancestor
        print(f"⚠️  Skipping unresolvable {what} ({e}): {root}")
        return None
    if base not in resolved.parents:
        print(f"⚠️  Skipping {what} that resolves outside {base}: {root} -> {resolved}")
        return None
    # Identity. Walk the segments BELOW the home only: the home itself is allowed to
    # sit behind a link (a real one often does), and resolving it once already accounted
    # for that. Climbing stops as soon as a parent resolves to the home, so a link above
    # the home is never mistaken for a redirect within it.
    probe = root.absolute()
    while True:
        if platform_compat.is_link_or_junction(probe):
            print(
                f"⚠️  Skipping {what} that is reached through a link: {probe}. "
                "A component tree must be the declared directory, not a redirect to "
                "another one — the archive is uploaded, so a redirect would ship "
                "whatever the link points at."
            )
            return None
        parent = probe.parent
        if parent == probe:
            break
        try:
            if parent.resolve() == base:
                break
        except OSError:
            break
        probe = parent
    return root


def _want(components: list[str] | None, name: str) -> bool:
    return components is None or name in components


class UnsafeComponentRoot(Exception):
    """A selected component's tree root does not resolve inside the data home.

    Raised rather than skipped: a bundle whose manifest claims a component it could not
    read is a backup that lies about its contents, which is worse than a refusal.
    """
