"""Extension points for what a deployment installs, bundles and stores.

External-service admission, the app registry and the bundled-apps loader, memory file
access, knowledge connectors, the reserved package manager and the reserved feature-app
item -- the "install / structural" extension points of the Composed Platform Providers
contract.

:mod:`kiro_crew.platform.interfaces` re-exports every name here and stays the one import path
callers use; this module never imports it. A new extension point of this kind goes here.
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING, Any, Dict, List, Optional, Protocol

if TYPE_CHECKING:
    from kiro_crew.config.loader import KiroCrewConfig


# ── install / structural extension points ──


class ExternalAccessPolicy(Protocol):
    """Which external services this deployment may reach, beyond the deny floor.

    The core offers three things unconditionally that a managed deployment may
    have to withhold, and none of them had a composition point:

    * **Installable-content registries.** Skill discovery (``skill_providers/``,
      skills.sh) and MCP server discovery (``mcp_providers/``, the official MCP
      registry) fetch from the public internet and then offer to INSTALL what
      they return. Both hardcoded their public provider at registration time.
    * **Cloud deployment.** ``kiro_crew/deploy/`` provisions real infrastructure
      (S3, CloudFront, IAM roles, a reaper Lambda) in the operator's own cloud
      account and hands back a public URL. It carries no capability gate at all,
      so ``capabilities.publish`` — which bounds publish-provider destinations —
      does not reach it.

    One policy object rather than three, because they answer the same question
    ("may this deployment use an external service the core offers by default?")
    and because a single object gives ONE audited admission chokepoint and one
    thing for an operator to implement, instead of a decision that is logged in
    some places and not others.

    Both decisions take the concrete target as well as a label. A name is
    something a provider chooses for itself; the URL or target is what determines
    where bytes go. An allowlist pinned to the target stops admitting a provider
    that later repoints at a different host, rather than letting it inherit trust
    from its name.

    The public default admits everything, so open-source behaviour is unchanged.
    """

    def admits_registry(self, kind: str, name: str, api_base: str) -> bool:
        """Whether the *kind* registry *name* serving ``api_base`` may be queried.

        *kind* is the catalog the provider belongs to — ``"skill"`` or ``"mcp"``.
        Returning ``False`` means the provider is never registered.
        """
        ...

    def admits_cloud_deployment(self, target: str) -> bool:
        """Whether this deployment may provision infrastructure in a cloud account.

        *target* names the deployment backend (``"aws"`` today). Returning
        ``False`` makes the deploy surface report itself disabled and refuse every
        mutating request.
        """
        ...


class AppRegistryPolicy(Protocol):
    """Trusted git hosts + clone-sandbox-mode decision for the app registry.

    Public default = the open-source public-forge host set.  The companion adds
    internal git hosts as trusted.
    """

    def public_git_hosts(self) -> "frozenset[str]": ...

    def clone_sandbox_mode(self, git_url: str, trusted_hosts: "frozenset[str] | None") -> str: ...


class AppsLoader(Protocol):
    """Discovery of bundled (builtin) apps + manifest sources.

    Public default = the open-source ``apps/builtins/`` set (``auto_research``,
    ``file_explorer``).  The companion bundles the internal feature apps.
    """

    def bundled_app_names(self) -> List[str]: ...

    def manifest_sources(self) -> List[Path]: ...

    def registry_rows(self) -> List[Dict[str, Any]]:
        """Extra App-Store registry rows the edition bundles (ADD-only merge).

        WIRED: ``apps/registry_pipeline/sources.py::_load_registry_file`` appends these to the
        rows parsed from the bundled ``app-registry.json``, de-duplicated by the
        row ``name`` (a bundled core row wins over a same-named edition row, so a
        companion can only ADD catalog entries, never silently repoint a core
        one). The public Default returns ``[]`` (catalog unchanged). A companion
        returns its internal App-Store rows (e.g. dev-fleet, pipeline-health)
        pointing at internal git hosts — which its ``AppRegistryPolicy`` already
        trusts. Each row is the same dict shape as an ``app-registry.json`` entry.
        v1 method addition (no ``CONTRACT_VERSION`` bump).
        """
        ...

    def default_registries(self) -> List[Dict[str, Any]]:
        """External app registries the edition ships as defaults (ADD-only merge).

        WIRED: ``apps/registry_pipeline/sources.py::_effective_registries`` merges these with the
        operator's ``config.registries`` for EVERY consumer of the registry list —
        index fetch/refresh, the trusted-host allowlist, row lookup, install, and
        the blob-proxy allowlist. Merging at the consumption sites rather than
        inside ``KiroCrewConfig`` is deliberate: an edition default must never be
        written back into the operator's ``config.json`` by a config save, and must
        never be shadowed by a stale copy that a past save persisted.

        Each row is the field shape of ``config.loader.ExternalRegistryConfig``
        (``{"name", "repo", "branch", "label", "review", "trust"}``); dicts, not
        dataclass instances, so a companion need not import the config module.
        Missing keys take the dataclass default.

        ``label`` and ``review`` are DISPLAY metadata the dashboard reads, and
        neither changes any security posture — ``trust`` alone selects the
        credential posture for cloning. ``label`` is a human name shown instead of
        the ``name`` id (empty shows the id); it never replaces the id, because
        cache paths and every installed app's ``_registry`` tag are keyed by it, so
        renaming would orphan installed apps. ``review`` is one of ``""`` /
        ``"curated"`` / ``"community"`` and says how thoroughly the registry's
        listings were reviewed before publication: ``"curated"`` renders a
        "Team reviewed" badge, ``"community"`` a "Not vetted" badge, and ``""``
        renders exactly as it did before the field existed. An unrecognised value
        DEGRADES to ``""`` (no claim) and is logged at error level; it never drops
        the row, because this list also feeds index fetch, the trusted-host
        allowlist and install, so a typo in a display field must not be able to
        take a registry offline.

        An edition default WINS on a ``name`` collision with an operator entry.
        That direction is the fail-closed one: a registry the edition pins carries
        a ``trust`` tier, and letting a same-named operator entry replace it would
        let a hand-edited ``config.json`` inherit that tier while repointing
        ``repo`` somewhere else. An operator can still add registries freely — just
        not silently repoint one the edition pinned.

        A row may also carry ``"allowDefaultInstall": true``. Only then may that
        registry's index mark apps ``defaultInstall``, which installs and trusts
        them once on first boot (``apps/default_installs.py``). It is honoured only
        for a row in force at the ``owner`` tier, and read only from this build
        row, never from ``config.json``. Absent means ``false``.

        The public Default returns ``[]``, so standalone behaviour is byte-identical
        to reading ``config.registries`` alone. v1 method addition (no
        ``CONTRACT_VERSION`` bump).
        """
        ...


@dataclass(frozen=True)
class MemoryRoots:
    """Which store's files a :class:`MemoryFiles` is being asked to serve.

    The extension point is a FACTORY keyed on this rather than a single ready-made
    object, because there is not one memory tree: the default workspace has one,
    and every named/member store has its own under its own directory. A context
    field holding one ``MemoryFiles`` would have to be either wrong for all but
    one store, or internally re-keyed by path -- which just moves this dataclass
    somewhere less visible.

    It also gives an edition the information it needs to decide PER STORE. An
    implementation that maps some stores elsewhere and leaves the rest on local
    disk can only do that if it is told which store it is answering for.
    """

    workspace: Path
    memory_dir: Path
    history_dir: Path
    # ``""`` for the DEFAULT store, a store name for a named/member one -- the
    # spelling ``memory_stores.named_store_of_db`` already uses. Not ``Optional``:
    # an empty string is a real, meaningful value here and ``None`` would add a
    # second way to say "default" that callers would have to handle.
    store_name: str = ""
    memory_version: int = 1


class MemoryFilesProvider(Protocol):
    """Chooses the :class:`MemoryFiles` implementation for one store's tree."""

    def files_for(self, roots: "MemoryRoots") -> "MemoryFiles":
        """File access for *roots*.

        Called once per ``MemoryStore`` and cached by it, so this may do real work
        (resolving a mount table, opening a client). It MUST NOT silently answer
        with local-disk access when it meant to answer with something else and
        could not: returning a degraded implementation here is indistinguishable
        to the caller from success, and for memory that means serving a stale
        document and then letting the next write publish it over the live one.
        Raise instead -- the read surface already degrades a failed read to an
        empty entry with a visible notice.
        """
        ...


@dataclass(frozen=True)
class MemoryEntry:
    """One markdown memory file as data: its path, last change, and text.

    The shape ``MemoryStore._guarded_entry`` has always returned, promoted to a
    declared type because it is now the value an extension point hands back. A
    refused, missing, oversized or undecodable file is the SAME shape with
    ``content=""`` and ``updated_at=None`` -- never an exception and never partial
    text -- so a caller cannot tell those cases apart by accident, which is the
    property the read surface's existing callers already depend on.

    ``updated_at`` is an ISO-8601 UTC string rather than a number because the two
    implementations derive it from different clocks (a filesystem mtime, a remote
    document's revision timestamp) and only ever display or compare it for
    equality; a float would invite arithmetic that means something on one
    implementation and nothing on the other.
    """

    path: str
    updated_at: Optional[str] = None
    content: str = ""


class MemoryFiles(Protocol):
    """File access for the markdown memory layer -- the one place it touches storage.

    Every read and write of memory's SOURCE text (preferences, projects, daily
    history, and the lessons/semantic documents the vector store ingests) goes
    through this interface instead of calling ``pathlib.Path`` methods directly.
    The public default (``DefaultMemoryFiles``) is today's local-disk code moved
    behind the protocol unchanged, so standalone behaviour is byte-identical and
    this seam adds no new dependency.

    Memory's DERIVED state is deliberately NOT here: the FTS5 index, the SQLite
    stores and the FAISS vectors are rebuilt from the source text and stay plain
    local files. Routing them through this interface would put a cache on the far
    side of a possibly-remote boundary and make a rebuild cost a network round
    trip per row, for state that is reconstructible by definition.

    ## Why the verbs are compound rather than POSIX-shaped

    The obvious protocol -- ``read_text``/``write_text`` mirroring ``Path`` -- was
    rejected twice over.

    First, the READ path is not a read: ``_guarded_entry`` is an admission gate
    (``O_NOFOLLOW`` open, non-regular-file and hardlink rejection, a size cap, and
    a double-stat retry so reported metadata always describes the bytes returned)
    over a directory an agent can write to. Those are local-inode concepts. An
    implementation backed by something other than a local filesystem cannot honour
    them and must not be asked to pretend; and a caller left to assemble the gate
    itself would reproduce it slightly differently at every site. So the protocol
    hands back a finished :class:`MemoryEntry` and each implementation owes the
    guarantees it can actually make.

    Second, the WRITE path is a compare-and-set, not a store. Memory's writers are
    read-modify-write: the consolidator reads a file, spends minutes in a model
    call, then writes a whole-file result, and a concurrent dashboard Save landing
    in that window must not be silently reverted. Today that is a lock plus an
    exact-baseline comparison; :meth:`replace_if` is the same contract named. It
    stays ONE call because a remote implementation can only be atomic if it is
    told the base it is replacing -- split into read-then-write, the base is
    inferred and an overlapping edit is lost with no error. A refused write returns
    ``False``; it never retries against a newer base, because "write anyway" is
    precisely the silent overwrite the baseline exists to prevent.

    :meth:`lock` remains separate because some writers mutate more than one thing
    under one tenure (today's history file and its index entry publish together).
    An implementation whose :meth:`replace_if` is already atomic may return a
    no-op context manager.
    """

    def read_entry(
        self, path: Path, *, require_readable: bool = False, missing_ok: bool = True
    ) -> MemoryEntry:
        """*path* as a :class:`MemoryEntry`, with the read guarantees applied.

        A file that cannot be safely or wholly read is an EMPTY entry, except when
        ``require_readable`` is set (index rebuilds and member anchors, where an
        unreadable source must not silently become an empty one) or the file is
        missing and ``missing_ok`` is false -- then it raises ``OSError``.
        """
        ...

    def read_text(self, path: Path) -> str:
        """*path*'s whole text, or ``""`` when it does not exist.

        The read behind ``read_preferences``/``read_projects`` and behind
        :meth:`replace_if`'s baseline comparison, whose historical contract is
        "absent reads as empty" rather than an exception.

        The decode is STRICT: a file that exists but is not valid UTF-8 RAISES
        here, where :meth:`read_entry` would answer an empty entry. The asymmetry
        is deliberate and load-bearing -- this value is what read-modify-write
        callers compute their next whole-file write from, so answering ``""`` for
        an undecodable file would let that write persist over the original bytes.
        An exception leaves the file intact and recoverable.
        """
        ...

    def read_text_for_rewrite(self, path: Path) -> str:
        """*path*'s text, hardened for a caller that is about to REWRITE it.

        Separate from :meth:`read_text` because the two have different threat
        models, not different implementations of one. A plain read that follows a
        symlink leaks the target's content to the reader; a read-modify-write that
        follows one REPUBLISHES the target's content into memory, where it is then
        served in context, shown in the dashboard and included in exports. So this
        variant additionally refuses a leaf that reading would follow or share --
        a symlink, a junction, a hardlinked or special inode -- and raises
        ``OSError`` rather than degrading, because the caller's next action is a
        write and a write must not proceed from bytes that failed admission.

        Absent reads as ``""`` (a fresh day's history file is the normal case).
        """
        ...

    def write(self, path: Path, content: str, *, newline: Optional[str] = None) -> None:
        """Publish *content* to *path* UNCONDITIONALLY, creating or replacing it.

        For writes whose intent is direct rather than merged -- seeding a default
        document, committing an already-validated edit. A reader must never observe
        a partial or empty intermediate state, so an implementation publishes
        whole versions only (locally: a staged temp file and an atomic rename).

        Writers that must not clobber a concurrent edit use :meth:`replace_if`
        instead; reach for this one only when losing a concurrent edit is the
        caller's explicit intent.
        """
        ...

    def replace_if(
        self,
        path: Path,
        content: str,
        *,
        base: Optional[str],
        newline: Optional[str] = None,
    ) -> bool:
        """Publish *content* to *path* iff its current text is exactly *base*.

        ``base=None`` writes unconditionally (direct user intent, which wins by
        design). Returns ``True`` when the write happened and ``False`` when
        *base* fails to match the current text -- byte-for-byte, whitespace
        included, because a merge computed from different bytes is stale
        whatever the difference was.
        Raises ``OSError`` when the write is REFUSED (an unsafe target), which is
        distinct from being skipped: a refusal must never look like success.
        """
        ...

    def exists(self, path: Path) -> bool:
        """Whether *path* exists."""
        ...

    def is_dir(self, path: Path) -> bool:
        """Whether *path* is a directory."""
        ...

    def glob(self, directory: Path, pattern: str) -> List[Path]:
        """Paths in *directory* matching *pattern* (one level, no recursion)."""
        ...

    def mkdir(self, path: Path) -> None:
        """Create *path* and any missing parents; existing is not an error."""
        ...

    def remove(self, path: Path) -> None:
        """Remove the file at *path*; missing is not an error.

        An implementation with a trash SHOULD use it: this is how history pruning
        deletes, and a memory file must not be unrecoverable by ordinary upkeep.
        """
        ...

    def lock(self, path: Path) -> "Any":
        """A context manager holding an exclusive write tenure for *path*.

        Typed ``Any`` because the public implementation yields an OS file lock and
        another may yield nothing at all; callers only ever use it as a ``with``.
        """
        ...


class KnowledgeProvider(Protocol):
    """Extra knowledge-base connectors the edition contributes.

    The knowledge sync engine keys connectors by ``source_type`` (the public
    core ships ``local_folder`` / ``obsidian_vault``).  Public default = none;
    the companion contributes internal connectors (e.g. an internal document
    connector) that
    the sync scheduler merges into its connector map.
    """

    def extra_connectors(self, cfg: "KiroCrewConfig") -> Dict[str, Any]:
        """Return ``{source_type: BaseConnector}`` to merge into the sync map.

        The values are ``kiro_crew.knowledge.connectors.base.BaseConnector``
        instances (typed ``Any`` here to avoid importing the knowledge package
        into the platform contract).  Returning ``{}`` (the Default) leaves the
        public connector set unchanged.
        """
        ...


class PackageManager(Protocol):
    """**RESERVED extension point — not consumed by the core.**

    Composing a ``PackageManager`` into ``PlatformContext.package_manager`` has
    NO effect: the external-tool install hints (ffmpeg, faiss, the ``voice-aws``
    extra) are inline brew/winget/pip text in ``doctor_checks/features.py``, not a
    single plan-resolution point this seam could own. Both methods are inert.

    For registry-backed installation of MCP servers / skills / agent packages,
    use ``CapabilityManager`` — the live, operations-based seam.

    Declared in ``context.RESERVED_SLOTS``: a non-default value logs a loud
    warning at boot, and ``test_platform_cpp_seam_coverage.py`` fails if a
    consumption site is added without the reservation being removed.
    """

    def install_plan(self, tool: str) -> List[str]: ...

    def which(self, tool: str) -> Optional[str]: ...


# ── feature apps ──


class FeatureApp(Protocol):
    """**RESERVED item type — not consumed by the core.**

    Populating ``PlatformContext.feature_apps`` has NO effect: bundled apps are
    discovered through ``AppsLoader.manifest_sources()`` / ``bundled_app_names()``
    and registered by ``apps/manager_builtins.py``. Neither ``manifest_path`` nor
    ``register`` is ever called; the tuple is a provenance record only.

    **Bundle apps through ``AppsLoader`` instead** — the live seam.

    Unlike every other Protocol here this one ships NO ``Default*`` adapter, and
    deliberately so: ``FeatureApp`` describes ONE app (an item), not a policy the
    core asks a question of, so there is no behavior for a default to reproduce.
    The default for the *slot* is the empty tuple ``()`` that
    ``build_default_context`` already composes — a ``DefaultFeatureApp`` would
    have to invent a fictional app to be instantiable, which is worse than
    nothing. Declared in ``context.RESERVED_SLOTS``; a non-empty tuple logs a
    loud warning at boot, and ``test_platform_cpp_seam_coverage.py`` fails if a
    consumption site appears without the reservation being removed.
    """

    @property
    def name(self) -> str: ...

    def manifest_path(self) -> Path: ...

    def register(self, ctx: Any) -> None: ...
