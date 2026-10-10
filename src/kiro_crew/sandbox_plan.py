"""What one sandboxed spawn masks, seals and re-opens, decided once for both backends.

:func:`plan_confinement` turns a :class:`SandboxRequest` (the tier and the caller's
extra paths) and a :class:`PlanHost` (where those things live on this host) into a
:class:`ConfinementPlan`. Two renderers read the plan: the Linux namespace launcher
(``sandbox_launcher.render_namespace_launcher``) and the macOS Seatbelt profile
(``sandbox_seatbelt.render_seatbelt_profile``). Every mask rule lives here, so both
programs follow one decision. What a renderer cannot express is declared once in
:data:`CAPABILITIES` and read by the planning both backends share; the rest of each
backend's shape -- one flat mask list for the launcher, a rule set per masked tree for
Seatbelt -- is its own planner, which :func:`plan_confinement` picks by backend.

The planner is pure. It reads nothing but its arguments: no filesystem call, no
environment, no clock and no logging, so the same ``(request, host)`` pair always gives
the same plan. Everything that needs the host -- the tier lists the platform policy
supplies, the relocated data-home spellings, the voice-runtime paths, the identity a
pre-spawn pass observed, ``realpath`` of a carve-out candidate -- arrives in the
:class:`PlanHost`, which ``kiro_crew.sandbox`` gathers before it plans. A refusal the
plan makes (a private window that would lift a mask, a carve-out that would re-open a
seal) is returned in :attr:`ConfinementPlan.refusals` for the caller to log.

Stdlib only: the namespace launcher program cannot import ``kiro_crew``, and nothing
here needs it.
"""

from __future__ import annotations

import os
from collections.abc import Callable, Iterable, Mapping
from dataclasses import dataclass, field
from typing import Any

#: The two renderers a plan is made for.
BACKEND_NAMESPACE = "namespace"
BACKEND_SEATBELT = "seatbelt"

#: The leaf name of the governance cache directory: a cache an ``extra_visible_dirs``
#: entry lifts stays read-only, because its metadata records the source the next boot
#: trusts.
POLICY_CACHE_LEAF = "policy_cache"

#: The gateway-only voice-runtime subtree, below the data home's ``run``.
VOICE_RUNTIME_LEAF = os.path.join("run", "voice-runtime")


@dataclass(frozen=True)
class RendererCapabilities:
    """What a renderer can express, read by the planner instead of a backend name.

    Each field is one place where the Linux namespace launcher and the macOS Seatbelt
    profile reach a different answer for the same request, on purpose, and each changes
    the plan of either backend when it changes. Naming it here keeps the difference a
    decision someone can read, rather than two builders that happen to disagree.
    """

    #: Tier directories the cc tier leaves VISIBLE, because the renderer cannot mask a
    #: directory while re-exposing one file inside it. The Linux launcher restores a
    #: read-only copy of ``~/.aws/config`` into the empty mask; Seatbelt rules are path
    #: rules with no such copy, and ``credential_process`` and the SSO caches live
    #: under ``~/.aws``, so Seatbelt keeps the whole directory readable.
    cc_unmaskable_dirs: frozenset[str]


#: The declared capabilities of each renderer.
CAPABILITIES: Mapping[str, RendererCapabilities] = {
    BACKEND_NAMESPACE: RendererCapabilities(
        cc_unmaskable_dirs=frozenset(),
    ),
    BACKEND_SEATBELT: RendererCapabilities(
        cc_unmaskable_dirs=frozenset({".aws"}),
    ),
}


@dataclass(frozen=True)
class SandboxRequest:
    """What one spawn asks of the sandbox: its tier, its backend and its extra paths.

    Built by ``kiro_crew.sandbox`` from the keyword arguments its spawn entry points
    already take. The identity fields (``*_ids``, ``fail_closed_file_masks``,
    ``required_mask_targets``, ``mask_occupants``, ``crew_home_aliases``) are what the
    pre-spawn passes observed, carried so the launcher can refuse a substituted object;
    only the namespace backend reads them.
    """

    tier: str
    backend: str = BACKEND_NAMESPACE
    strip_python_env: bool = False
    forward_ssh_auth_sock: bool = False
    extra_hidden_dirs: tuple[str, ...] = ()
    extra_hidden_dir_ids: tuple[tuple[str, int, int], ...] = ()
    extra_alias_credential_ids: tuple[tuple[int, int], ...] = ()
    extra_visible_dirs: tuple[str, ...] = ()
    extra_private_dirs: tuple[str, ...] = ()
    extra_private_dir_ids: tuple[tuple[str, int, int], ...] = ()
    extra_writable_dirs: tuple[str, ...] = ()
    extra_expose_files: tuple[str, ...] = ()
    fail_closed_file_masks: tuple[tuple[str, int, int], ...] = ()
    required_mask_targets: tuple[str, ...] = ()
    #: Name -> the identity a pre-spawn pass recorded for it: ``(dev, ino, is_link)``,
    #: optionally followed by the kind the name reached and the referent's dev and ino.
    mask_occupants: tuple[tuple[str, tuple[int, ...]], ...] = ()
    crew_home_aliases: tuple[tuple[str, str, int, int], ...] = ()


@dataclass(frozen=True)
class CarveoutProbe:
    """What the host said about one ``extra_writable_dirs`` candidate.

    ``lexical`` is the normalized absolute spelling, ``canonical`` its ``realpath``,
    and ``is_dir`` whether the canonical spelling is an existing directory.
    """

    raw: str
    lexical: str
    canonical: str
    is_dir: bool


@dataclass(frozen=True)
class PlanHost:
    """Where the things a plan names live on this host, gathered before planning.

    Two adapters fill it: ``kiro_crew.sandbox`` reads the live host (the platform
    policy, the data home, the voice-runtime cache, the pod environment), and a test
    builds one from literals. Every field is data, so a plan never reaches past it.
    """

    #: The gateway's ``$HOME``; every tier entry is relative to it.
    home: str
    #: The working directory a relative caller path is resolved against.
    cwd: str = "/"
    #: The tier's directory list, ``$HOME``-relative, as the platform policy supplies it.
    tier_dirs: tuple[str, ...] = ()
    #: ``$HOME``-relative single files the cc and strict tiers mask.
    cc_files: tuple[str, ...] = ()
    #: ``$HOME``-relative files the cc tier re-exposes read-only inside a masked tree.
    cc_expose_files: tuple[str, ...] = ()
    #: ``$HOME``-relative crew-home ceilings, sealed read-only.
    crew_readonly_targets: tuple[str, ...] = ()
    #: ``$HOME``-relative crew-home leaves the sandbox hides.
    crew_hidden_dirs: tuple[str, ...] = ()
    #: Crew-home leaves masked with an unreadable stand-in rather than an empty one.
    unreadable_mask_leaves: tuple[str, ...] = ()
    #: Environment prefixes scrubbed in every tier.
    sensitive_env_prefixes: tuple[str, ...] = ()
    #: Environment names scrubbed from agent spawns in the cc and strict tiers.
    agent_denied_env_keys: tuple[str, ...] = ()
    #: Interpreter environment scrubbed from a foreign Python subprocess.
    python_env_prefixes: tuple[str, ...] = ()
    #: A pod child's remapped home (``KIROCREW_OS_HOME``), or ``None`` outside a pod.
    pod_os_home: str | None = None
    #: Tier leaves NOT re-anchored under the pod home: the pod's own grant store.
    pod_grant_store_leaves: frozenset[str] = frozenset()
    #: Leaves re-anchored under the pod home in addition to the tier, while the grant
    #: store is carved out.
    pod_masked_subleaves: tuple[str, ...] = ()
    #: The governance cache's resolved path, when the data home is not under ``$HOME``.
    relocated_policy_cache_dirs: tuple[str, ...] = ()
    #: The resolved crew-home hidden leaves that differ from their ``$HOME`` spelling.
    relocated_crew_hidden: tuple[str, ...] = ()
    #: The resolved crew-home ceilings that differ from their ``$HOME`` spelling.
    relocated_crew_readonly: tuple[str, ...] = ()
    #: md-notebook state directories that must be masked whole: a planted link in their
    #: chain stopped the sweep from deleting an orphan through it.
    md_notebook_degraded_dirs: tuple[str, ...] = ()
    #: The voice-runtime roots (lexical and canonical), hidden in every tier.
    voice_runtime_roots: tuple[str, ...] = ()
    #: Their ``run`` parents, readable but never writable.
    voice_runtime_parents: tuple[str, ...] = ()
    #: Every rename-sensitive ancestor of those parents.
    voice_runtime_ancestor_guards: tuple[str, ...] = ()
    #: The resolved kiro agents tree, sealed read-only.
    kiro_agents_targets: tuple[str, ...] = ()
    #: The running gateway's packaged install tree (both spellings), sealed read-only.
    #: Empty for an editable or from-source checkout.
    install_root_targets: tuple[str, ...] = ()
    #: Every rename-sensitive ancestor of those targets (Seatbelt only): a path-based
    #: deny on the package directory does not hold if a parent can be renamed away.
    install_root_ancestor_guards: tuple[str, ...] = ()
    #: The probes for each absolute ``extra_writable_dirs`` candidate.
    carveout_probes: tuple[CarveoutProbe, ...] = ()
    #: The real uid and gid the namespace launcher maps.
    uid: int = 0
    gid: int = 0
    #: Whether the host's ssh accepts ``StrictHostKeyChecking=accept-new``.
    ssh_accept_new: bool = False


@dataclass(frozen=True)
class Mask:
    """One masked tree and everything the plan decided about it.

    ``origin`` is ``"tier"`` for an entry the tier or the host derived and
    ``"caller"`` for an ``extra_hidden_dirs`` entry. ``windows`` are the private windows
    re-opened inside it, ``window_ancestors`` the directories ``realpath`` must stat to
    reach them, and ``exposed`` the files carved out of its read deny.
    """

    path: str
    origin: str
    windows: tuple[str, ...] = ()
    window_ancestors: tuple[str, ...] = ()
    exposed: tuple[str, ...] = ()
    #: An ``extra_visible_dirs`` entry at or below this path lifted the mask.
    cancelled: bool = False
    #: A lifted governance cache stays read-only: its write and hardlink denies hold.
    read_only_when_cancelled: bool = False
    #: Seatbelt only: the tree also takes a write deny (the governance cache, the voice
    #: runtime, a crew-home secret). A bind mask blocks both directions in one rule.
    write_sealed: bool = False
    #: Seatbelt only: the tree may be a plain file, so it also takes a literal write deny.
    literal_write_sealed: bool = False


@dataclass(frozen=True)
class Cancellation:
    """A mask an ``extra_visible_dirs`` entry lifted, and what still holds on it."""

    path: str
    #: The visible entries at or below the mask that lifted it.
    by: tuple[str, ...]
    #: Whether the lifted tree is still sealed read-only (the governance cache).
    read_only: bool


@dataclass(frozen=True)
class Refusal:
    """A window or carve-out the plan refused, as the log record the caller emits."""

    kind: str
    message: str
    args: tuple[Any, ...] = ()


@dataclass(frozen=True)
class NamespaceIdentities:
    """What the pre-spawn passes observed, as the namespace launcher carries it.

    Serialized and never re-derived: the plan is made on the gateway's event loop, where
    ``test_the_builder_does_not_stat_the_hidden_paths`` and
    ``test_the_planner_reads_nothing_but_its_arguments`` forbid any filesystem probe,
    because one stat per path per async spawn blocks every session on a stalled network
    home. Each field is empty for a caller that passes nothing, so that caller's launcher
    behaves exactly as before.
    """

    #: Identities for MASK ROOTS, taken by the producer in the act that chose the name. A
    #: mask root is carried by name, and the child masks whatever answers to that name: a
    #: real directory renamed onto it is masked in the original's place while the original
    #: stays readable at its new name, which the name alone cannot detect. An entry binds
    #: the mask to one directory identity, and the child refuses the spawn when it cannot
    #: mask that identity -- a skipped mask leaves the tree exposed. A root with no entry
    #: keeps the plain name behaviour.
    hidden_dir_ids: Mapping[str, tuple[int, int]] = field(default_factory=dict)
    #: Identities the PRODUCER took when it approved each window. A window with no entry
    #: is one whose producer supplied none, and the child treats it exactly as before; only
    #: a producer that vouches for an inode gets the stricter check.
    private_dir_ids: Mapping[str, tuple[int, int]] = field(default_factory=dict)
    #: INODES, not roots: the per-app credentials that already carry a second hard link,
    #: as the parent read them. The child's own scan builds its set from the masked
    #: directories at DEPTH 1 -- a measured choice, because deepening it walks every masked
    #: tree on every spawn -- so ``apps/<app>/.app_secret``, one level below a mask root,
    #: never enters it. The child cannot read those paths itself: it masks that tree in the
    #: same process, so a stat there reports ENOENT and the walk arms on nothing. Pairs
    #: computed before any mask existed are the only form that survives the crossing.
    alias_credential_ids: tuple[tuple[int, int], ...] = ()
    #: The masked files whose ABSENCE at mask time is a fault rather than "nothing to
    #: hide". Every other entry is skipped when absent on purpose, but these were
    #: DISCOVERED to exist moments ago, as the second name of a credential leaf, so a path
    #: that is gone now was renamed between the discovery and the mount and the bytes are
    #: readable under whatever it is called instead. The link count is required too: a
    #: single-linked file at the same path is something else that appeared there.
    fail_closed_file_masks: tuple[tuple[str, int, int], ...] = ()
    #: Established targets whose absence at mask time is a fault, minus every target
    #: nested under a directory the launcher masks first.
    required_mask_targets: tuple[str, ...] = ()
    mask_occupants: Mapping[str, tuple[int, ...]] = field(default_factory=dict)
    crew_home_aliases: tuple[tuple[str, str, int, int], ...] = ()


@dataclass(frozen=True)
class ConfinementPlan:
    """The decided confinement of one spawn, for one renderer.

    ``masks`` hold every masked tree in rule order. The namespace launcher bind-masks
    each uncancelled one (:attr:`sensitive_dirs`); Seatbelt renders each as deny rules.
    ``readonly`` are sealed read-only, ``writable`` the approved write carve-outs inside
    the sealed runtime parents, ``files`` the single masked files and ``expose`` the
    ``(source, name)`` files re-exposed read-only inside a masked tree.
    """

    backend: str
    tier: str
    home: str
    masks: tuple[Mask, ...]
    readonly: tuple[str, ...]
    runtime_parents: tuple[str, ...]
    runtime_ancestor_guards: tuple[str, ...]
    files: tuple[str, ...]
    expose: tuple[tuple[str, str], ...]
    windows: tuple[str, ...]
    writable: tuple[str, ...]
    hide_ssh: bool
    ssh_dir: str
    ssh_known_hosts: str
    env_scrub_prefixes: tuple[str, ...]
    unreadable_mask_leaves: tuple[str, ...]
    ssh_accept_new: bool
    uid: int
    gid: int
    identities: NamespaceIdentities
    cancellations: tuple[Cancellation, ...]
    refusals: tuple[Refusal, ...]

    @property
    def sensitive_dirs(self) -> tuple[str, ...]:
        """The masked trees that stay masked, each spelling once."""
        return tuple(dict.fromkeys(m.path for m in self.masks if not m.cancelled))

    @property
    def sensitive_files(self) -> tuple[str, ...]:
        """Every path the launcher's file loop is offered.

        The single masked files plus every masked tree: a caller's hidden path may be a
        FILE, and the launcher's two loops hide each kind differently, so every path
        goes in both lists and the child classifies it with its own ``isdir`` /
        ``isfile``. Classifying here would put a stat per entry in front of every spawn.
        """
        return tuple(dict.fromkeys([*self.files, *self.sensitive_dirs]))


# --------------------------------------------------------------------------- #
# Path rules, shared with ``kiro_crew.sandbox`` for the callers outside the plan.
# --------------------------------------------------------------------------- #


def absolute(path: str, cwd: str | None = None) -> str:
    """*path* as an absolute, normalized spelling, resolving a relative one against *cwd*.

    ``os.path.abspath`` reads the process working directory for a relative path, so the
    plan resolves against the host's recorded ``cwd`` instead; with ``cwd`` omitted this
    is exactly ``os.path.abspath``.
    """
    if cwd is None or os.path.isabs(path):
        return os.path.abspath(path)
    return os.path.normpath(os.path.join(cwd, path))


def hidden_path_contains_visible_path(
    hidden_path: str, visible_paths: tuple[str, ...], cwd: str | None = None
) -> bool:
    """Return whether hiding *hidden_path* would also hide a required path.

    A visible path EQUAL to or BELOW the hidden path cancels it; a visible ancestor does
    not. Lexical.
    """
    hidden = absolute(hidden_path, cwd)
    for item in visible_paths:
        visible = absolute(item, cwd)
        try:
            if os.path.commonpath((hidden, visible)) == hidden:
                return True
        except ValueError:
            continue
    return False


def is_policy_cache_dir(path: str) -> bool:
    """Whether *path* is a governance-cache directory, by leaf name.

    Matched on the leaf rather than against a resolved path so it holds for every
    spelling the dir lists carry -- the ``$HOME``-relative default, the legacy
    ``~/.kirocrew`` entry that the deny lists must keep covering, and the relocated
    form -- without a filesystem call on the spawn path.
    """
    return os.path.basename(path.rstrip("/" + os.sep)) == POLICY_CACHE_LEAF


def is_voice_runtime_dir(path: str) -> bool:
    """Whether *path* is the gateway-only voice runtime subtree."""
    normalized = os.path.normpath(path)
    return normalized.endswith(os.sep + VOICE_RUNTIME_LEAF) or normalized.endswith(
        "/" + VOICE_RUNTIME_LEAF.replace(os.sep, "/")
    )


def fold_crew_home_alias(path: str, aliases: tuple[tuple[str, str, int, int], ...]) -> str:
    """*path* respelled under the canonical root when it sits under an alias root.

    The first alias in order wins. Pure string work: the identity decision was made once
    per root by the pass that recorded the alias.
    """
    for alias, canonical, _dev, _ino in aliases:
        if path == alias:
            return canonical
        if path.startswith(alias.rstrip("/") + "/"):
            return canonical.rstrip("/") + path[len(alias.rstrip("/")) :]
    return path


def window_is_a_hidden_target(path: str, hidden_dirs: Iterable[str]) -> bool:
    """Whether *path* as a private window IS one of the directories that stay hidden.

    A mask lift written as a window. Refused on every backend and at every producer,
    because nothing downstream can make it safe: the window is the masked tree.
    """
    probe = path.rstrip(os.sep)
    return any(probe == hidden.rstrip(os.sep) for hidden in hidden_dirs)


def window_contains_a_hidden_target(path: str, hidden_dirs: Iterable[str]) -> bool:
    """Whether *path* as a private window would hold a directory that stays hidden.

    Unlike the EQUALS case this one is an ORDERING problem rather than a contradiction:
    the window is re-bound read-write over the tree, so a nested mask applied BEFORE
    that bind lands on the path the window then shadows, and the leaf comes back with
    it. A backend that can re-apply the nested mask AFTER binding the window -- the
    Linux launcher does, from a descriptor it already holds -- keeps the leaf hidden and
    the rest of the window live. A backend that expresses masks as path rules with no
    ordering it controls cannot, so there it stays refused.

    This is why the question is asked of the whole mask set rather than of the one
    parent a window matched: an entry can be a proper descendant of one hidden tree
    while being an ancestor of another hidden leaf inside it -- ``apps/meetings/data``
    under a masked ``apps`` tree holds the masked ``apps/meetings/data/edits`` -- and a
    per-parent test accepts it on the strength of the first relationship.
    """
    probe = path.rstrip(os.sep)
    return any(hidden.rstrip(os.sep).startswith(probe + os.sep) for hidden in hidden_dirs)


#: The log record a refused private window becomes. NEITHER path is named: the mask
#: set's entries name credential and authorization stores, so logging them records the
#: layout of exactly what the set exists to hide. The refusal is deterministic and
#: reproducible from the caller's own arguments.
WINDOW_REFUSAL = (
    "SECURITY: not opening a private window that is or contains a masked "
    "directory -- a window is re-bound read-write over the mask, so that "
    "path stays masked for this spawn. Every other window and the spawn "
    "itself are unaffected."
)


def private_windows(
    extra_private_dirs: tuple[str, ...],
    hidden_dirs: list[str],
    *,
    remasks_contained_targets: bool = False,
    cwd: str | None = None,
) -> tuple[list[str], list[Refusal]]:
    """The ``extra_private_dirs`` entries that name a PROPER descendant of a hidden tree.

    A private window is the one directory a spawn keeps inside a masked tree -- its own
    scratch under the masked scratch root. Unlike ``extra_visible_dirs`` it never lifts
    the parent's mask: siblings stay hidden, only the window is re-exposed (read-write,
    it is the process's own). An entry that is not inside a hidden tree needs no window
    and is dropped. Lexical, like every other path rule here.

    An entry that EQUALS a hidden target is always refused: the window IS the masked
    tree. An entry that CONTAINS one is refused UNLESS the caller re-applies the nested
    mask after binding the window (*remasks_contained_targets*). Refusing is the
    fail-closed direction: the window is withheld and the parent's mask keeps covering
    the path. Returns the deduplicated windows and one refusal per refused entry.
    """
    windows: list[str] = []
    refusals: list[Refusal] = []
    for raw in extra_private_dirs:
        path = absolute(raw, cwd)
        refused = window_is_a_hidden_target(path, hidden_dirs) or (
            not remasks_contained_targets and window_contains_a_hidden_target(path, hidden_dirs)
        )
        if refused:
            refusals.append(Refusal("private-window", WINDOW_REFUSAL))
            continue
        for parent in hidden_dirs:
            if path.startswith(parent.rstrip(os.sep) + os.sep):
                windows.append(path)
                break
    return list(dict.fromkeys(windows)), refusals


def window_ancestors(target: str, windows: list[str]) -> list[str]:
    """Every directory from masked *target* down to each window's parent.

    These are the path components ``realpath`` must ``lstat`` to reach a window, all of
    them inside the mask. Lexical.
    """
    root = target.rstrip("/")
    ancestors: list[str] = []
    for window in windows:
        parent = os.path.dirname(window.rstrip("/"))
        while parent.startswith(root + "/"):
            ancestors.append(parent)
            parent = os.path.dirname(parent)
        ancestors.append(root)
    return list(dict.fromkeys(ancestors))


def path_within(path: str, parent: str) -> bool:
    """Whether *path* equals *parent* or lies inside it (lexical, normalized)."""
    parent_normalized = os.path.normpath(parent)
    if path == parent_normalized:
        return True
    prefix = parent_normalized.rstrip(os.sep) + os.sep
    return path.startswith(prefix)


#: The log records a refused write carve-out becomes.
CARVEOUT_NOT_ABSOLUTE = "SECURITY: refusing sandbox write carve-out %r: not an absolute path"
CARVEOUT_NOT_A_DIRECTORY = (
    "SECURITY: refusing sandbox write carve-out %r: not an existing " "real directory"
)
CARVEOUT_REFUSED = "SECURITY: refusing sandbox write carve-out %r: %s"


def writable_carveouts(
    extra_writable_dirs: tuple[str, ...],
    probes: Mapping[str, CarveoutProbe],
    *,
    subtree_guards: list[str],
    literal_guards: list[str],
    carveable_parents: list[str],
) -> tuple[list[str], list[Refusal]]:
    """Validate write carve-outs against the sandbox's own seals.

    ``extra_writable_dirs`` exists for exactly one purpose: a caller that hands a child
    its private scratch directory INSIDE the sealed runtime parent (``<data home>/run``)
    needs that one directory writable -- the MCP probe's ``TMPDIR`` lives at
    ``run/mcp-tmp/<probe>``. Everything else stays sealed.

    Both renderers apply a carve-out with override semantics (Seatbelt is
    last-match-wins; the launcher remounts a fresh bind read-write), so an unvalidated
    path would re-open whatever it covers. Each candidate is checked in BOTH its lexical
    and canonical spelling against every seal the plan emits:

    * it must lie inside a ``carveable_parents`` entry -- the runtime parent's write
      seal is the ONLY seal this parameter may punch through;
    * no guard (``subtree_guards`` -- read-hidden trees, read-only ceilings, the voice
      runtime -- or ``literal_guards`` -- sealed single files, rename guards) may equal
      the carve-out or live inside it, since the override would re-open that guard;
    * it may not sit inside a non-carveable subtree guard, so a read-hidden tree can
      never grow a writable window.

    A candidate that fails any check is SKIPPED with a refusal rather than raising:
    callers treat temp containment as fail-open hygiene, and a refused carve-out
    degrades to the sealed behavior instead of blocking the spawn. Comparisons are
    LEXICAL and case-sensitive, so this is a backstop for self-derived paths, not a
    boundary for hostile input. *probes* hold the host's ``realpath`` / ``isdir`` answer
    for each absolute candidate. Returns the deduplicated approved spellings.
    """
    carveable = [os.path.normpath(path) for path in carveable_parents]
    subtree = [os.path.normpath(path) for path in subtree_guards]
    literals = [os.path.normpath(path) for path in literal_guards]
    carveable_set = set(carveable)
    approved: list[str] = []
    refusals: list[Refusal] = []
    for raw in extra_writable_dirs:
        if not raw or not os.path.isabs(raw):
            refusals.append(Refusal("carve-out", CARVEOUT_NOT_ABSOLUTE, (raw,)))
            continue
        probe = probes[raw]
        spellings = list(dict.fromkeys((probe.lexical, probe.canonical)))
        if not probe.is_dir:
            refusals.append(Refusal("carve-out", CARVEOUT_NOT_A_DIRECTORY, (raw,)))
            continue
        refusal: str | None = None
        for spelling in spellings:
            if not any(path_within(spelling, parent) for parent in carveable):
                refusal = f"{spelling!r} is outside every carveable runtime parent"
                break
            for guard in subtree + literals:
                if path_within(guard, spelling):
                    refusal = f"sealed path {guard!r} would be re-opened by it"
                    break
            if refusal:
                break
            for guard in subtree:
                if guard not in carveable_set and path_within(spelling, guard):
                    refusal = f"it lies inside sealed subtree {guard!r}"
                    break
            if refusal:
                break
        if refusal:
            refusals.append(Refusal("carve-out", CARVEOUT_REFUSED, (raw, refusal)))
            continue
        approved.extend(spellings)
    return list(dict.fromkeys(approved)), refusals


def scrub_prefixes(base: list[str], forward_ssh_auth_sock: bool) -> list[str]:
    """Filter the ``SSH_AUTH_SOCK`` prefix out of *base* when the forward is on.

    The forward decision arrives already resolved, threaded from the agent spawn path
    only, so a generic launcher defaults it off and still scrubs the socket. It filters
    the exact literal ``"SSH_AUTH_SOCK"`` and nothing else, so the opt-in can never widen
    into a general environment passthrough. *base* is returned unchanged, never mutated,
    when the forward is off.
    """
    if not forward_ssh_auth_sock:
        return base
    return [p for p in base if p != "SSH_AUTH_SOCK"]


def pod_home_targets(
    dirs: tuple[str, ...],
    home: str,
    os_home: str | None,
    grant_store_leaves: frozenset[str],
    masked_subleaves: tuple[str, ...],
) -> list[str]:
    """The tier's directory list re-anchored under a pod child's REMAPPED home.

    A pod's kiro-cli child gets a pod-owned ``HOME`` (``KIROCREW_OS_HOME``) so its OAuth
    grants die with the pod, and every tier entry is ``$HOME``-relative against the
    GATEWAY's home -- so none of them named the remapped tree. Computing it here makes
    the mask correct for every caller regardless of when the remap runs.

    One leaf is deliberately NOT re-anchored: the pod's own grant store
    (*grant_store_leaves*, ``.aws``). The pod's child writes its OWN MCP OAuth grants
    there, and bind-masking it empty would discard every grant the pod mints. So that
    carving out the grant store does not also expose the file-credential leg,
    *masked_subleaves* (``.aws/config``, ``.aws/credentials``, ``.aws/cli``) are
    re-anchored in addition, and only for a tier that masks the grant store at all.

    Returns only paths that DIFFER from the ``$HOME``-relative spelling, so the default
    layout gains no duplicate rule; ``[]`` outside a pod. ``normpath``, never
    ``realpath``. What the carve-out leaves readable, and why it is accepted, is recorded
    on ``sandbox._pod_os_home_targets``, which reads the pod environment for this rule.
    """
    if not os_home:
        return []
    out: list[str] = []
    carved = grant_store_leaves.intersection(dirs)
    leaves = (*dirs, *(masked_subleaves if carved else ()))
    for leaf in leaves:
        if leaf in carved:
            continue
        relocated = os.path.normpath(os.path.join(os_home, leaf))
        default = os.path.normpath(os.path.join(home, leaf))
        if relocated != default:
            out.append(relocated)
    return out


# --------------------------------------------------------------------------- #
# The planner.
# --------------------------------------------------------------------------- #


def _tier_dirs(request: SandboxRequest, host: PlanHost, caps: RendererCapabilities) -> list[str]:
    """The tier's directory list, minus what this renderer must leave visible in cc."""
    if request.tier == "cc" and caps.cc_unmaskable_dirs:
        return [d for d in host.tier_dirs if d not in caps.cc_unmaskable_dirs]
    return list(host.tier_dirs)


def _mask_sources(dirs: list[str], host: PlanHost) -> list[str]:
    """Every tier- and host-derived masked tree, in rule order.

    The tier list under ``$HOME``, then the same list re-anchored under a pod's
    remapped home, the relocated governance cache and crew-home leaves, the md-notebook
    state directories a planted link degraded, and the voice runtime.
    """
    return (
        [os.path.join(host.home, d) for d in dirs]
        + pod_home_targets(
            tuple(dirs),
            host.home,
            host.pod_os_home,
            host.pod_grant_store_leaves,
            host.pod_masked_subleaves,
        )
        + list(host.relocated_policy_cache_dirs)
        + list(host.relocated_crew_hidden)
        + list(host.md_notebook_degraded_dirs)
        + list(host.voice_runtime_roots)
    )


def _env_scrub_prefixes(request: SandboxRequest, host: PlanHost) -> list[str]:
    """The environment prefixes the launcher scrubs before it execs the agent."""
    prefixes = list(host.sensitive_env_prefixes)
    if request.tier in ("cc", "strict"):
        # Agent subprocesses must not read channel credentials through os.environ: the
        # file mask of the data home's .env hides them on disk, and the config loader
        # seeds them into os.environ for trusted children only.
        prefixes = prefixes + list(host.agent_denied_env_keys)
    if request.strip_python_env:
        # A foreign Python subprocess must not inherit this runtime's interpreter env.
        prefixes = prefixes + list(host.python_env_prefixes)
    return scrub_prefixes(prefixes, request.forward_ssh_auth_sock)


def _visible_lifts(path: str, visible: tuple[str, ...], cwd: str) -> tuple[str, ...]:
    """The *visible* entries at or below *path*, which lift its mask."""
    return tuple(v for v in visible if hidden_path_contains_visible_path(path, (v,), cwd))


def carveout_candidates(request: SandboxRequest) -> tuple[str, ...]:
    """The ``extra_writable_dirs`` spellings the plan validates, for the host to probe.

    The namespace plan folds crew-home alias spellings and validates the folded
    spelling, so the host probes that one; the Seatbelt plan validates them as given.
    """
    if request.backend == BACKEND_NAMESPACE:
        aliases = request.crew_home_aliases
        return tuple(fold_crew_home_alias(path, aliases) for path in request.extra_writable_dirs)
    return tuple(request.extra_writable_dirs)


def plan_confinement(request: SandboxRequest, host: PlanHost) -> ConfinementPlan:
    """Decide what *request* masks, seals and re-opens on *host*.

    Pure and deterministic: the same pair gives the same plan, and nothing outside the
    arguments is read. Two properties hold whatever the caller passes in
    ``extra_visible_dirs``: the governance cache and the crew-home ceilings stay
    read-only, and every mask that list lifts is recorded in
    :attr:`ConfinementPlan.cancellations`. Raises ``KeyError`` for an unknown backend.
    """
    caps = CAPABILITIES[request.backend]
    return _PLANNERS[request.backend](request, host, caps)


def _namespace_plan(
    request: SandboxRequest, host: PlanHost, caps: RendererCapabilities
) -> ConfinementPlan:
    """The plan the Linux namespace launcher renders: one mask list, bind-masked.

    The caller's ``extra_hidden_dirs`` join the tier's masks as one list, which the
    launcher bind-masks alike. Every ``$HOME`` spelling of the data home reached through
    a link is folded onto the resolved spelling, so the launcher is handed one name per
    directory.
    """
    home = host.home
    tier = request.tier
    aliases = request.crew_home_aliases

    def fold(path: str) -> str:
        return fold_crew_home_alias(path, aliases)

    dirs = _tier_dirs(request, host, caps)
    files = list(host.cc_files) if tier in ("cc", "strict") else []
    expose_files = list(host.cc_expose_files) if tier == "cc" else []
    env_prefixes = _env_scrub_prefixes(request, host)
    hide_ssh = tier == "strict"
    sources = _mask_sources(dirs, host)
    hidden = sources + [absolute(p, host.cwd) for p in request.extra_hidden_dirs]
    origins = ["tier"] * len(sources) + ["caller"] * len(request.extra_hidden_dirs)
    # One name per directory: every path under a ``$HOME`` spelling of the data home
    # reached through a link is respelled under the resolved root, which is how the
    # pre-spawn passes already spell the leaves they record. The carve-out and window
    # spellings go through the same fold, so a caller naming the alias spelling still
    # lifts or re-opens what it asked for.
    hidden = [fold(path) for path in hidden]
    visible = tuple(fold(absolute(path, host.cwd)) for path in request.extra_visible_dirs)
    private = tuple(fold(path) for path in request.extra_private_dirs)
    private_ids = tuple((fold(p), dev, ino) for p, dev, ino in request.extra_private_dir_ids)
    hidden_ids = tuple((fold(p), dev, ino) for p, dev, ino in request.extra_hidden_dir_ids)
    writable_in = tuple(fold(path) for path in request.extra_writable_dirs)
    required_in = tuple(fold(path) for path in request.required_mask_targets)
    unhidden = [
        path for path in hidden if hidden_path_contains_visible_path(path, visible, host.cwd)
    ]
    kept = [path for path in hidden if path not in unhidden]
    masks = tuple(
        Mask(
            path=path,
            origin=origin,
            cancelled=path in unhidden,
            read_only_when_cancelled=path in unhidden and is_policy_cache_dir(path),
        )
        for path, origin in zip(hidden, origins)
    )
    cancellations = tuple(
        Cancellation(
            path=path,
            by=_visible_lifts(path, visible, host.cwd),
            read_only=is_policy_cache_dir(path),
        )
        for path in dict.fromkeys(unhidden)
    )
    # The governance cache is READ-ONLY whenever it is exposed at all, a property of the
    # directory rather than of the caller's request: a lift otherwise cancels the whole
    # rule set, and the one caller that legitimately reads the ceiling would get WRITE
    # with it. The metadata records the source the next boot trusts, so a process that
    # can rewrite it picks the ceiling for every later boot.
    readonly = [path for path in unhidden if is_policy_cache_dir(path)]
    # ``run`` stays readable because it holds the launcher, and sealing both of its
    # spellings stops an agent renaming the hidden voice runtime out from under the
    # path-based rule.
    readonly.extend(host.voice_runtime_parents)
    # The crew data home's ceilings: read-only rather than hidden, because in-sandbox
    # code resolves them and an absent ceiling reads as the permissive standalone
    # default -- masking one would REMOVE it.
    readonly.extend(
        fold(os.path.join(home, target))
        for target in host.crew_readonly_targets
        if fold(os.path.join(home, target)) not in kept
    )
    # A relocated data home escapes every ``$HOME``-relative rule above.
    readonly.extend(path for path in host.relocated_crew_readonly if path not in kept)
    # Same relocation hole for the kiro agents tree (fork governance's specs).
    readonly.extend(path for path in host.kiro_agents_targets if path not in kept)
    # The gateway's own packaged install tree: what it executes on its next start. Not
    # liftable by ``extra_visible_dirs`` (that list lifts masks, never seals), and the
    # updater that legitimately writes it runs in the gateway process, unwrapped.
    readonly.extend(path for path in host.install_root_targets if path not in kept)
    # The launcher re-applies a mask nested inside a private window after binding the
    # window, from a descriptor it already holds, so such a window is admitted here.
    windows, window_refusals = private_windows(
        private, kept, remasks_contained_targets=True, cwd=host.cwd
    )
    runtime_parents = list(host.voice_runtime_parents)
    # ``kept + unhidden`` reconstitutes the full mask set before the visibility lift:
    # dropping ``unhidden`` would let a tree a caller re-exposed read-only grow a
    # writable window through this parameter.
    writable, carveout_refusals = writable_carveouts(
        writable_in,
        {probe.raw: probe for probe in host.carveout_probes},
        subtree_guards=kept
        + unhidden
        + [path for path in readonly if path not in set(runtime_parents)]
        + ([os.path.join(home, ".ssh")] if hide_ssh else []),
        literal_guards=[fold(os.path.join(home, f)) for f in files],
        carveable_parents=runtime_parents,
    )
    expose = [(fold(os.path.join(home, f)), f.split("/")[-1]) for f in expose_files]
    # Caller re-exposures are a COPY, never the inode: strictly weaker than lifting the
    # tree with ``extra_visible_dirs``.
    expose += [(absolute(p, host.cwd), os.path.basename(p)) for p in request.extra_expose_files]
    # Deduplicated: the restore loop opens each destination for WRITE after the first
    # pass made it read-only, so a repeated entry kills the spawn.
    expose = list(dict.fromkeys(expose))
    # A target nested under a directory the launcher masks EARLIER is legitimately
    # absent by the time it is pinned, because its parent's empty mask covers it; only a
    # name that moved is a race. Subtracted by string, never by asking the filesystem.
    masked_ancestors = [d.rstrip("/") + "/" for d in dict.fromkeys(kept)]
    required = sorted(
        {
            target
            for target in dict.fromkeys(required_in)
            if not any(target.startswith(parent) for parent in masked_ancestors)
        }
    )
    occupants = {
        name: tuple(ident)
        for name, ident in sorted((fold(n), i) for n, i in request.mask_occupants)
    }
    identities = NamespaceIdentities(
        hidden_dir_ids={path: (dev, ino) for path, dev, ino in hidden_ids},
        private_dir_ids={path: (dev, ino) for path, dev, ino in private_ids},
        alias_credential_ids=tuple(dict.fromkeys(request.extra_alias_credential_ids)),
        fail_closed_file_masks=tuple(
            (fold(path), dev, ino)
            for path, dev, ino in dict.fromkeys(request.fail_closed_file_masks)
        ),
        required_mask_targets=tuple(required),
        mask_occupants=occupants,
        crew_home_aliases=tuple(request.crew_home_aliases),
    )
    return ConfinementPlan(
        backend=request.backend,
        tier=tier,
        home=home,
        masks=masks,
        readonly=tuple(dict.fromkeys(readonly)),
        runtime_parents=tuple(runtime_parents),
        runtime_ancestor_guards=(),
        files=tuple(fold(os.path.join(home, f)) for f in files),
        expose=tuple(expose),
        windows=tuple(windows),
        writable=tuple(writable),
        hide_ssh=hide_ssh,
        ssh_dir=os.path.join(home, ".ssh"),
        ssh_known_hosts=os.path.join(home, ".ssh", "known_hosts"),
        env_scrub_prefixes=tuple(env_prefixes),
        unreadable_mask_leaves=tuple(sorted(host.unreadable_mask_leaves)),
        ssh_accept_new=host.ssh_accept_new,
        uid=host.uid,
        gid=host.gid,
        identities=identities,
        cancellations=cancellations,
        refusals=tuple(window_refusals + carveout_refusals),
    )


def _seatbelt_plan(
    request: SandboxRequest, host: PlanHost, caps: RendererCapabilities
) -> ConfinementPlan:
    """The plan the Seatbelt profile renders: the tier's masks, then the caller's.

    Seatbelt rules are path rules, so both spellings of an aliased data home are kept,
    and the caller's masks are their own rule sets after the tier's, denying every
    direction. A private window inside a masked tree survives a visibility lift of that
    tree (the narrower answer wins), and a visibility lift never reaches the voice
    runtime.
    """
    home = host.home
    tier = request.tier
    sep = os.sep
    dirs = _tier_dirs(request, host, caps)
    files = list(host.cc_files) if tier in ("cc", "strict") else []
    expose_files = list(host.cc_expose_files) if tier == "cc" else []
    # One set for the tier's and the caller's exposed files: under ``strict`` the tier
    # already denies ``.aws``, and a narrower deny emitted later cannot cancel an
    # earlier one -- Seatbelt is deny-wins across deny rules.
    expose_abs = {os.path.join(home, f) for f in expose_files}
    extra_expose_abs = {absolute(p, host.cwd) for p in request.extra_expose_files}
    expose_abs |= extra_expose_abs
    crew_hidden = {os.path.join(home, rel) for rel in host.crew_hidden_dirs}
    crew_hidden.update(host.relocated_crew_hidden)
    visible = request.extra_visible_dirs
    masked_targets = _mask_sources(dirs, host)
    # Seatbelt cannot re-apply a mask nested inside a private window -- its rules cannot
    # be ordered that way -- so a window holding a masked leaf is refused.
    windows_for, window_refusals = private_windows(
        request.extra_private_dirs,
        masked_targets,
        remasks_contained_targets=False,
        cwd=host.cwd,
    )
    masks: list[Mask] = []
    cancellations: list[Cancellation] = []
    for target in masked_targets:
        windows = [w for w in windows_for if w.startswith(target.rstrip("/") + "/")]
        if windows:
            masks.append(
                Mask(
                    path=target,
                    origin="tier",
                    windows=tuple(windows),
                    window_ancestors=tuple(window_ancestors(target, windows)),
                )
            )
            continue
        lifts_voice = not is_voice_runtime_dir(target)
        if hidden_path_contains_visible_path(target, visible, host.cwd) and lifts_voice:
            sealed = is_policy_cache_dir(target)
            masks.append(
                Mask(path=target, origin="tier", cancelled=True, read_only_when_cancelled=sealed)
            )
            cancellations.append(
                Cancellation(target, _visible_lifts(target, visible, host.cwd), sealed)
            )
            continue
        is_crew_secret = target in crew_hidden
        masks.append(
            Mask(
                path=target,
                origin="tier",
                exposed=tuple(f for f in expose_abs if f.startswith(target + "/")),
                write_sealed=is_policy_cache_dir(target)
                or is_voice_runtime_dir(target)
                or is_crew_secret,
                literal_write_sealed=is_crew_secret,
            )
        )
    readonly = (
        [os.path.join(home, rel) for rel in host.crew_readonly_targets]
        + list(host.relocated_crew_readonly)
        + list(host.kiro_agents_targets)
        + list(host.install_root_targets)
    )
    caller_targets = list(
        dict.fromkeys(absolute(path, host.cwd) for path in request.extra_hidden_dirs)
    )
    # Windows inside a CALLER's own mask, computed against the caller's targets as well,
    # so a window there survives the blanket denies of that tree. Equality is refused by
    # the window gate itself, so every entry is a PROPER descendant.
    caller_windows, caller_window_refusals = private_windows(
        request.extra_private_dirs,
        caller_targets,
        remasks_contained_targets=False,
        cwd=host.cwd,
    )
    for target in caller_targets:
        windows = [w for w in caller_windows if w.startswith(target.rstrip("/") + "/")]
        carved = tuple(sorted(f for f in extra_expose_abs if f.startswith(target + sep)))
        if windows:
            masks.append(
                Mask(
                    path=target,
                    origin="caller",
                    windows=tuple(windows),
                    window_ancestors=tuple(window_ancestors(target, windows)),
                    exposed=carved,
                )
            )
            continue
        if hidden_path_contains_visible_path(target, visible, host.cwd):
            masks.append(Mask(path=target, origin="caller", cancelled=True))
            cancellations.append(
                Cancellation(target, _visible_lifts(target, visible, host.cwd), False)
            )
            continue
        masks.append(Mask(path=target, origin="caller", exposed=carved))
    hide_ssh = tier == "strict"
    ssh_dir = os.path.join(home, ".ssh")
    runtime_parents = list(host.voice_runtime_parents)
    ancestor_guards = list(
        dict.fromkeys((*host.voice_runtime_ancestor_guards, *host.install_root_ancestor_guards))
    )
    home_files = [os.path.join(home, f) for f in files]
    # Validated against every seal the profile emits, and emitted last: Seatbelt is
    # last-match-wins, so this allow overrides only the runtime parent's write seal.
    writable, carveout_refusals = writable_carveouts(
        request.extra_writable_dirs,
        {probe.raw: probe for probe in host.carveout_probes},
        subtree_guards=masked_targets + readonly + caller_targets + ([ssh_dir] if hide_ssh else []),
        literal_guards=ancestor_guards + home_files,
        carveable_parents=runtime_parents,
    )
    return ConfinementPlan(
        backend=request.backend,
        tier=tier,
        home=home,
        masks=tuple(masks),
        readonly=tuple(readonly),
        runtime_parents=tuple(runtime_parents),
        runtime_ancestor_guards=tuple(ancestor_guards),
        files=tuple(home_files),
        expose=tuple((path, os.path.basename(path)) for path in sorted(expose_abs)),
        windows=tuple(dict.fromkeys([*windows_for, *caller_windows])),
        writable=tuple(writable),
        hide_ssh=hide_ssh,
        ssh_dir=ssh_dir,
        ssh_known_hosts=os.path.join(ssh_dir, "known_hosts"),
        env_scrub_prefixes=(),
        unreadable_mask_leaves=(),
        ssh_accept_new=False,
        uid=host.uid,
        gid=host.gid,
        identities=NamespaceIdentities(),
        cancellations=tuple(cancellations),
        refusals=tuple(window_refusals + caller_window_refusals + carveout_refusals),
    )


#: The planner each backend renders from.
_PLANNERS: Mapping[
    str, Callable[[SandboxRequest, PlanHost, RendererCapabilities], ConfinementPlan]
] = {
    BACKEND_NAMESPACE: _namespace_plan,
    BACKEND_SEATBELT: _seatbelt_plan,
}


def namespace_payload(plan: ConfinementPlan) -> dict[str, Any]:
    """The data the namespace launcher program reads, as JSON-safe values.

    This is the ONE substitution the launcher renderer makes. It is embedded in the
    program as a Python literal, so it carries no ``true``/``false``/``null``: every flag
    is an ``int``. Each value is spelled as the launcher has always received it, so a
    path appears in the program exactly as ``json.dumps`` writes it.
    """
    ids = plan.identities
    return {
        "real_uid": plan.uid,
        "real_gid": plan.gid,
        "sensitive_dirs": list(plan.sensitive_dirs),
        "sensitive_dir_ids": {path: list(pair) for path, pair in ids.hidden_dir_ids.items()},
        "private_dirs": list(plan.windows),
        "private_dir_ids": {path: list(pair) for path, pair in ids.private_dir_ids.items()},
        "readonly_dirs": list(plan.readonly),
        "writable_dirs": list(plan.writable),
        "sensitive_files": list(plan.sensitive_files),
        "fail_closed_file_masks": [list(entry) for entry in ids.fail_closed_file_masks],
        "alias_credential_ids": [list(pair) for pair in ids.alias_credential_ids],
        "required_mask_targets": list(ids.required_mask_targets),
        # The link flag is an INT, not a bool: this data is embedded in the launcher as
        # PYTHON SOURCE, and ``json.dumps`` spells a bool ``true``/``false``, which
        # Python does not define -- the child would die with ``NameError`` before it
        # mounts anything, on every spawn. The fourth element (the KIND the name reached)
        # and the fifth and sixth (the referent's device and inode) travel as recorded;
        # an identity recorded without them is emitted at three, read as no kind.
        "mask_occupants": {
            name: [ident[0], ident[1], int(bool(ident[2]))] + [int(k) for k in ident[3:6]]
            for name, ident in ids.mask_occupants.items()
        },
        "crew_home_aliases": [list(entry) for entry in ids.crew_home_aliases],
        "expose_files": [list(pair) for pair in plan.expose],
        "env_prefixes": list(plan.env_scrub_prefixes),
        "ssh_dir": plan.ssh_dir,
        "ssh_known_hosts": plan.ssh_known_hosts,
        "hide_ssh": int(plan.hide_ssh),
        "sandbox_level": plan.tier,
        "unreadable_masks": list(plan.unreadable_mask_leaves),
        "strict_host_key_opt": (
            " -o StrictHostKeyChecking=accept-new" if plan.ssh_accept_new else ""
        ),
        "stand_in_roots": [f"/run/user/{plan.uid}", "/dev/shm"],
    }
