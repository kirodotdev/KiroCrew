"""Read a chat's folder-inherited steering directories into prompt documents.

A sidebar folder may declare extra steering roots (``steering_dirs``) that every
chat under it inherits. This module is the ONE reader for them: the
Context_Builder calls it on the non-member session-start path and on the member
essentials path, so a single admissibility check, a single inclusion rule, a
single dedup and a single double-load skip serve both. A second reader would be
a second set of those rules, which is how one of them silently diverges.

The module is pure with respect to the process -- no dashboard state, no clock,
and the ONE config read is :func:`memory_silo_roots`, which asks the config
where the memory workspaces live so the fence follows the same answer the
memory layer does -- which is what lets the same call serve both paths and be
property-tested over real temporary trees.
"""

from __future__ import annotations

import logging
import os
from collections.abc import Callable, Collection, Iterator, Sequence
from dataclasses import dataclass, field
from pathlib import Path

from kiro_crew import pinned_fs
from kiro_crew.config.loader import KiroCrewConfig, config_dir, workspace_dir_from_entry
from kiro_crew.config.resolution import DEGRADED_WHOLE_CONFIG, DEGRADED_WORKSPACES
from kiro_crew.frontmatter import STEERING_LOADER, split_frontmatter
from kiro_crew.hooks import safe_read_file_bytes_nolink, validate_file_path
from kiro_crew.member_essential_context import _MAX_DOCUMENTS, _MAX_SOURCE_BYTES
from kiro_crew.memory import WORKSPACE_DIR_NAME
from kiro_crew.memory_stores import MEMORY_STORES_DIR_NAME

logger = logging.getLogger(__name__)

#: Running ceiling on a single collection pass. A folder's steering roots are
#: operator-declared and validated, but an operator can still point one at a
#: very large ``.md`` tree; without a running bound the whole tree's paths and
#: bodies would be materialized before any per-document truncation, spiking a
#: worker. Each document body is already capped at ``_MAX_SOURCE_BYTES`` on read,
#: so a document-count ceiling bounds the aggregate (count x per-doc cap) and the
#: traversal together. Collection stops once the ceiling is reached. Mirrors the
#: essential-context document cap.
_MAX_FOLDER_STEERING_DOCUMENTS = _MAX_DOCUMENTS

#: Ceiling on directory entries examined per steering root during one walk.
#: The document ceiling above bounds what is READ; this bounds what is
#: ENUMERATED, so a root whose tree is huge (or mostly non-Markdown) still costs
#: a bounded amount of work before collection gives up on it. Generous relative
#: to any real standards repository; the walk stops, it does not fail.
_MAX_FOLDER_STEERING_ENTRIES = 4096

#: Longest canonical document path retained as a source label. 4096 is Linux
#: ``PATH_MAX``; a longer path cannot name a real file, and the label rides into
#: the prompt and the essentials envelope, so it carries an explicit bound like
#: every other retained string here.
_MAX_FOLDER_STEERING_SOURCE_LEN = 4096


def _source_label(path: str) -> str:
    """The prompt-safe spelling of a document's source path.

    A filename is agent-nameable on the host, and the label rides into the
    prompt on its own line (``# <path>`` here, ``[Essential source: <path>]``
    in a member envelope). A name containing a newline would end that line
    early and start another, letting the rest of the filename pose as a fresh
    marker line with whatever authority that dialect grants. Every C0/C1
    control character and the line/paragraph separators are folded to ``U+FFFD``
    so the label is exactly one line; nothing else about the path is changed,
    because the label must still name the file on disk.
    """
    return "".join(
        "\ufffd" if (ord(ch) < 0x20 or 0x7F <= ord(ch) <= 0x9F or ch in "\u2028\u2029") else ch
        for ch in path
    )


#: Header of the rendered prompt section. Names the provenance ("this chat's
#: folder") because the bodies are operator-authored standards the model has no
#: other way to place: without it, a checklist from an org-standards repo reads
#: as if it came from the project in front of it.
FOLDER_STEERING_HEADER = (
    "[FOLDER STEERING — standards inherited from this chat's folder. "
    "Follow these as you would project steering.]"
)
FOLDER_STEERING_FOOTER = "[END FOLDER STEERING]"

#: Source label of the ONE synthetic essentials document a member envelope
#: carries when folder steering had to be cut (see
#: ``context._fit_folder_steering_into_envelope``). A URI-shaped label, not a
#: path: it must never collide with a real steering file's realpath key.
FOLDER_STEERING_OMISSION_SOURCE = "folder-steering://omitted"

#: ``inclusion`` values that mean "not every session". Compared case-folded, so
#: the ``fileMatch`` spelling the IDE writes is recognised as ``filematch``.
#: Anything else -- including an absent key -- is treated as ``always``, which
#: matches how the steering loader reads a document with no frontmatter at all.
_SKIPPED_INCLUSIONS = frozenset({"manual", "auto", "filematch"})


@dataclass(frozen=True)
class SteeringOmission:
    """One ceiling that fired during a collection pass, said out loud.

    A ceiling that drops the tail silently makes the delivered section read
    exactly like a tree that never held those documents, so the model applies
    an incomplete standard as if it were the whole one and nobody can tell from
    the prompt. Each ceiling therefore records WHICH bound fired (``kind``),
    WHERE (``root``, the declared steering root as the operator spelled it) and
    HOW MANY items fell past it (``count``), and the renderer says so once per
    section. ``kind`` is one of ``"documents"`` (the collection-wide document
    ceiling; ``count`` is the number of ``*.md`` candidates enumerated under
    ``root`` after the ceiling was reached, none of them opened -- so whether
    each was always-load steering, a manual document or a duplicate is unknown,
    and the notice says "not examined", not "missing"), ``"fence"`` (the
    memory-store fence was incomplete because the configuration's workspace
    table could not be read; ``count`` is the number of declared roots, none of
    them read, and ``root`` is empty), ``"files"`` (Markdown files the entry
    ceiling's directory had already listed when the ceiling fired; ``count`` is
    how many were discarded unread -- reported as files, not folded into the
    directory count) or ``"entries"`` (a LOWER BOUND on directories under ``root`` the walk never listed:
    ``count`` covers the one mid-listing when the enumeration ceiling fired,
    every sibling still queued at that moment, and any child that could not be
    opened at all -- their files are unknown, and so is anything beneath them,
    which is why the count is a floor and the notice says so).
    """

    kind: str
    root: str
    count: int


@dataclass
class SteeringCollection:
    """Result of one :func:`collect_folder_steering` pass.

    ``documents`` holds the ``(source_path, body)`` pairs; ``omissions`` holds
    every ceiling that fired, in the order it fired. The dataclass is a
    list-like stand-in for the many call sites and tests that only want the
    documents (``len``, iteration, indexing, equality with a plain list), so
    the count reaches the renderer without every consumer changing shape.
    """

    documents: list[tuple[str, str]] = field(default_factory=list)
    omissions: list[SteeringOmission] = field(default_factory=list)

    def __iter__(self) -> Iterator[tuple[str, str]]:
        return iter(self.documents)

    def __len__(self) -> int:
        return len(self.documents)

    def __getitem__(self, index):  # type: ignore[no-untyped-def]
        return self.documents[index]

    def __bool__(self) -> bool:
        return bool(self.documents) or bool(self.omissions)

    def __eq__(self, other: object) -> bool:
        if isinstance(other, SteeringCollection):
            return self.documents == other.documents and self.omissions == other.omissions
        if isinstance(other, list):
            return self.documents == other
        return NotImplemented


def _is_within(path: Path, parent: Path) -> bool:
    """Is *path* strictly inside *parent*?

    A separator-terminated prefix test, the same shape
    :func:`kiro_crew.context.steering_target_admissible` uses, so ``/a/bc``
    never counts as being under ``/a/b``. Both sides are expected to be
    already-resolved paths; comparing an unresolved spelling here would let a
    symlinked home defeat the test.
    """
    return str(path).startswith(str(parent) + os.sep)


def _double_load_roots(project: str | None, home: Path | None) -> tuple[Path, ...]:
    """Steering roots a provider with its own steering path ALREADY delivers.

    On such a provider a document under the project's ``.kiro/steering`` or under
    ``~/.kiro/steering`` reaches the model through the existing project/global
    steering path, so re-sending it here would double its tokens; the caller
    decides per provider whether that path exists (``skip_delivered_roots``). An
    unresolvable root simply contributes no skip rule: it cannot contain a file
    we are about to emit either.
    """
    roots: list[Path] = []
    candidates: list[Path] = []
    if project:
        candidates.append(Path(project))
    candidates.append(home if home is not None else Path.home())
    for candidate in candidates:
        try:
            # RuntimeError, not only OSError: ``Path.resolve()`` raises it on a
            # symlink loop, which a stale operator path can easily be.
            resolved = candidate.expanduser().resolve()
        except (OSError, RuntimeError):
            continue
        roots.append(resolved / ".kiro" / "steering")
    return tuple(roots)


@dataclass(frozen=True)
class MemorySiloFence:
    """The memory-store fence as this process can currently know it.

    ``roots`` are the directories no steering root may point at, into, or
    over. ``degraded`` is empty when the fence is complete and otherwise names
    why it is NOT: the ``workspaces`` table (which can place a Global V1
    workspace at an absolute directory anywhere) could not be read, so a
    workspace the operator declared may be missing from ``roots``. A consumer
    that admits or reads a steering root must treat a degraded fence as "the
    fence is unknown" and refuse -- the default roots alone are NOT the fence,
    and a root that merely contains a later-declared external workspace would
    carry that person's memory into a member's prompt with nothing going red.
    """

    roots: tuple[Path, ...]
    degraded: str = ""

    @property
    def complete(self) -> bool:
        return not self.degraded


def memory_silo_fence() -> MemorySiloFence:
    """Build the memory-store fence, saying so when it is incomplete.

    Three shapes leave the ``workspaces`` table unknown, and all three are
    reported rather than narrowed to the defaults: the config load raising, a
    whole config file this load could not read (``DEGRADED_WHOLE_CONFIG`` --
    the loader hands back defaults for an unparseable ``config.json``, exactly
    the case where an operator's external workspace is invisible), and a
    ``workspaces`` value that is not an object (``DEGRADED_WORKSPACES``).
    """
    base = config_dir()
    candidates: list[Path] = [
        base / WORKSPACE_DIR_NAME,
        base / MEMORY_STORES_DIR_NAME,
        # The base workspace directory (what an unmapped name resolves to),
        # from the placement rule alone -- no config read.
        workspace_dir_from_entry(None),
    ]
    degraded = ""
    try:
        cfg = KiroCrewConfig.load()
    except Exception as exc:
        degraded = f"the configuration could not be loaded ({type(exc).__name__})"
    else:
        sections = cfg.degraded_sections
        if DEGRADED_WHOLE_CONFIG in sections:
            degraded = "the configuration file could not be read"
        elif DEGRADED_WORKSPACES in sections:
            degraded = "the 'workspaces' table in the configuration could not be read"
        # ONE load, ONE snapshot: every workspace directory is resolved from the
        # ``cfg`` just loaded, never through ``workspace_dir_for(name)``, which
        # re-loads the config per name. A second load can observe a different
        # document (a concurrent config write, a transient read failure between
        # the two reads) and silently place a workspace at the base directory
        # that THIS snapshot placed elsewhere -- leaving that external
        # directory out of the fence while ``degraded`` (computed from this
        # snapshot) still reads complete. The default workspace's entry is
        # covered by the same loop.
        candidates.extend(
            workspace_dir_from_entry(cfg.workspaces[name]) for name in sorted(cfg.workspaces)
        )
    roots: list[Path] = []
    for candidate in candidates:
        try:
            resolved = candidate.resolve()
        except (OSError, RuntimeError):
            resolved = candidate
        if resolved not in roots:
            roots.append(resolved)
    return MemorySiloFence(roots=tuple(roots), degraded=degraded)


def memory_silo_roots() -> tuple[Path, ...]:
    """The directories no steering root may point at, into, or over.

    A named memory store is a SILO: every Global V1 workspace (the default
    ``config_dir()/workspace`` AND each entry of the ``workspaces`` table, which
    may name an absolute directory anywhere -- ``config.loader.workspace_dir_for``
    returns such a value verbatim -- holding ``preferences.md``, ``projects.md``,
    ``history/``) and every named store under ``config_dir()/memory_stores/``
    are separate trees on purpose, and content never crosses between them
    except by an owner-selected copy. Folder steering reads Markdown from an
    operator-pointed directory and hands it to every chat in the folder,
    including a V2 member's; a root inside any of those workspaces (or a root
    that CONTAINS one, such as the crew data home itself) would carry the
    person's memory into that member's prompt with nothing going red. The
    workspace directories are resolved by the same placement rule the memory
    layer's ``workspace_dir_for`` applies (``workspace_dir_from_entry``), from
    ONE config snapshot, so the fence and the stores cannot name two different
    trees and no per-name reload can diverge from the snapshot ``degraded``
    was judged on.

    This is the ROOTS ONLY. When the ``workspaces`` table could not be read the
    roots are the defaults and are NOT the whole fence; a gate must consult
    :func:`memory_silo_fence` and refuse on ``degraded`` rather than fence
    against this tuple alone.
    """
    return memory_silo_fence().roots


def crosses_memory_silo(root_resolved: Path, silos: tuple[Path, ...] | None = None) -> bool:
    """Whether *root_resolved* is a memory silo, lies inside one, or contains one.

    With *silos* omitted the fence is built here, and a DEGRADED fence answers
    ``True`` for every root: an unknown fence cannot clear anything. Callers
    that already hold a :class:`MemorySiloFence` pass ``fence.roots`` after
    refusing on ``fence.degraded`` themselves, so they can say why.

    Containment runs BOTH ways: a steering root at ``~/.kiro/crew`` contains
    the Global workspace just as surely as a root at ``~/.kiro/crew/workspace``
    is it, and the walk would descend into it.

    Compared CASEFOLDED on both sides, the rule the sensitive-path gate applies
    for the same reason: on a case-insensitive filesystem (macOS APFS/HFS+ by
    default, Windows) ``.../Workspace`` and ``.../workspace`` are the SAME
    directory, and ``Path.resolve()`` does not canonicalize case, so a byte-exact
    comparison would let an alternate-case spelling of the workspace walk
    straight past the fence. Folding is strictly more protective: on
    case-sensitive Linux it can only over-match an alternate-case sibling of a
    silo, which is refused rather than read -- the safe side.
    """
    if silos is None:
        fence = memory_silo_fence()
        if not fence.complete:
            return True
        silos = fence.roots
    root_cf = str(root_resolved).casefold()
    for silo in silos:
        silo_cf = str(silo).casefold()
        if (
            root_cf == silo_cf
            or root_cf.startswith(silo_cf + os.sep)
            or silo_cf.startswith(root_cf + os.sep)
        ):
            return True
    return False


def _admissible(resolved: Path, root_resolved: Path) -> bool:
    """Apply the existing steering gate with the declared directory as base.

    Imported inside the function on purpose: ``context`` imports this module,
    so a module-level import would close a cycle. The trust base is the
    directory the operator declared, never ``$HOME`` -- a symlink inside a
    steering root can then never read outside the root it sits in.
    """
    from kiro_crew.context import steering_target_admissible

    return steering_target_admissible(resolved, base=root_resolved)


def _walk_markdown(root: Path, omissions: list[SteeringOmission] | None = None) -> Iterator[Path]:
    """Yield ``*.md`` files under *root* lazily, bounded, without following links.

    Deliberately not ``root.glob("**/*.md")``: that materializes the whole
    path list before the caller's first document check, and on Python < 3.13
    the ``**`` walk follows directory symlinks, so one link back up the tree or
    into a huge directory turns a small steering root into an unbounded
    enumeration. Deliberately not ``os.walk`` either: it builds one directory's
    complete name lists before yielding, so a single sufficiently wide
    directory is materialized whole before any ceiling can apply. This walker
    reads directory entries one at a time through ``os.scandir`` and counts
    each as it is read, so the ``_MAX_FOLDER_STEERING_ENTRIES`` ceiling bounds
    memory as well as work however wide or deep the tree behind the root is.
    It never descends a linked directory.

    The root is opened THROUGH A PINNED HANDLE (:func:`open_dir_pinned`: the
    parent chain resolved once and pinned component by component, the root's
    own name opened relative to it with ``O_DIRECTORY | O_NOFOLLOW``) and every
    subdirectory relative to its parent's descriptor. Validation screened the
    root's spelling, but ``is_dir()``/``scandir(path)`` would re-open that name
    and follow whatever sits there now -- on Windows a swap to a link at a
    share is itself the outbound SMB probe. Nothing in the subtree is addressed
    by path again; only the *logical* path is carried for the reader, which
    pins its own descriptor inside the root.

    Where a directory cannot be opened relative to a descriptor (Windows), the
    walk REFUSES rather than falling back to names: a by-name walk is not a
    weaker mode of this one, it is the mechanism whose check-then-open window
    the product's own modeled adversary -- an agent running as this user --
    can win by swapping an ancestor for a junction (see :mod:`kiro_crew.pinned_fs`
    and the same rule in :mod:`kiro_crew.snapshot`). Folder steering is
    task guidance, so the refusal costs the turn nothing but that guidance,
    logged once at warning so an operator on such a host knows why.

    Order is deterministic: files of a directory (sorted), then its
    subdirectories (sorted, depth-first). Sorting needs a directory's names in
    hand, so a directory that cannot be listed within the remaining budget
    contributes nothing and ends the walk -- failing closed rather than
    emitting an order-dependent partial listing. Errors on a directory are
    skipped, not raised, so one unreadable subtree does not break a turn.

    When the entry ceiling ends the walk, the caller's *omissions* (if given)
    receives one :class:`SteeringOmission` of kind ``"entries"`` counting the
    directories still queued -- the part of the tree nobody listed -- so the
    rendered section can say the standard is incomplete instead of reading
    like a tree that stopped there.
    """
    if not pinned_fs.supports_pinned_tree_walk():
        logger.warning(
            "folder steering skipped for %s: this platform cannot open a directory "
            "relative to a descriptor, and a by-name walk could follow a swapped link",
            root,
        )
        return
    yield from _walk_markdown_pinned(root, omissions)


def _classify_entries(
    entries: Iterator[os.DirEntry[str]], *, root: Path, examined: int
) -> tuple[list[str], list[str], int, bool]:
    """Split one directory listing into (files, subdirs) under the entry ceiling.

    Counts each entry as it is READ so a wide directory cannot be materialized
    past ``_MAX_FOLDER_STEERING_ENTRIES``. The fourth element says whether the
    ceiling fired: when it did, reading STOPPED there (fail closed, see
    :func:`_walk_markdown`) and the returned ``files`` / ``subdirs`` are the
    entries seen BEFORE it -- none of them will be read or entered, and the
    caller counts them into the omission notice so the section names what was
    discarded rather than a placeholder. Entries never read are, by
    construction, uncounted; the notice says the count is a floor.
    """
    files: list[str] = []
    subdirs: list[str] = []
    overflowed = False
    for entry in entries:
        examined += 1
        if examined > _MAX_FOLDER_STEERING_ENTRIES:
            logger.debug(
                "folder steering walk hit its %d-entry ceiling under %s; "
                "remaining entries skipped",
                _MAX_FOLDER_STEERING_ENTRIES,
                root,
            )
            overflowed = True
            break
        try:
            is_dir = entry.is_dir(follow_symlinks=False)
        except OSError:
            continue
        if is_dir:
            subdirs.append(entry.name)
        elif entry.name.endswith(".md"):
            # A link named ``*.md`` is listed and then refused by the
            # no-link reader, exactly as ``os.walk`` reported it.
            files.append(entry.name)
    return files, subdirs, examined, overflowed


def _walk_markdown_pinned(
    root: Path, omissions: list[SteeringOmission] | None = None
) -> Iterator[Path]:
    """Descriptor-relative walk; see :func:`_walk_markdown`.

    Descriptor discipline: at any moment the walk holds exactly ONE open
    descriptor per level of the active ancestry (root -> current directory),
    never one per sibling. A directory's children are opened one at a time,
    each AFTER the previous sibling subtree is finished and closed, so a root
    with thousands of sibling directories costs a handful of descriptors, not
    thousands -- on a low-``RLIMIT_NOFILE`` host the alternative runs the
    process out of descriptors and later subtrees vanish with no trace. Every
    open is relative to the parent's descriptor (``dir_fd``) with
    ``O_NOFOLLOW``, so a name swapped for a link after the listing is refused.
    Depth is bounded by the entry ceiling (each level costs at least one entry)
    and the descriptor count with it.

    A child that cannot be opened (``OSError``: gone, unreadable, EMFILE) is
    COUNTED into *omissions* as an unlisted directory rather than skipped in
    silence: the section must never read as complete when part of the tree
    was never entered.
    """
    try:
        root_fd = pinned_fs.open_dir_pinned(root, what="folder steering root")
    except (pinned_fs.PinnedPathRefusal, OSError) as exc:
        logger.debug("folder steering directory is not a directory: %s (%s)", root, exc)
        return
    examined = 0
    unopened = 0
    # Tallies of what the ceiling discarded in the directory it fired in: the
    # Markdown files and the subdirectories listed before it that will never
    # be read or entered. They feed the omission notice, so a 4096-file root
    # is reported as thousands of unread files, not as one unlisted directory.
    discarded_files = 0
    discarded_dirs = 0
    # The active ancestry: one frame per open level, holding its descriptor,
    # its logical path and the sibling names still to visit (smallest last, so
    # ``pop()`` yields them in sorted order).
    stack: list[tuple[int, Path, list[str]]] = []
    pending_files: list[Path] = []

    def _release(fd: int) -> None:
        try:
            os.close(fd)
        except OSError:
            pass

    def _enter(fd: int, directory: Path) -> bool:
        """List *directory* (open as *fd*), queue its files, push its frame.

        Returns ``False`` when the entry ceiling fired, which ends the walk,
        after tallying the files and subdirectories the listing had already
        produced (they are discarded, and the notice counts them). A directory
        that cannot be listed is released and counted as unopened.
        """
        nonlocal examined, unopened, discarded_files, discarded_dirs
        try:
            with os.scandir(fd) as entries:
                files, subdirs, examined, overflowed = _classify_entries(
                    entries, root=root, examined=examined
                )
        except OSError:
            _release(fd)
            unopened += 1
            return True
        if overflowed:
            _release(fd)
            discarded_files += len(files)
            discarded_dirs += len(subdirs)
            return False
        stack.append((fd, directory, sorted(subdirs, reverse=True)))
        pending_files.extend(directory / name for name in sorted(files))
        return True

    def _ceiling_omissions(queued: int) -> None:
        """Say what the ceiling left out: the directory it fired in, the
        subdirectories it had listed but will never enter, every sibling still
        queued at every open level, any child that could not be opened -- and,
        as its own line, the Markdown files it had listed but will never read."""
        if omissions is None:
            return
        omissions.append(
            SteeringOmission(
                kind="entries", root=str(root), count=1 + discarded_dirs + queued + unopened
            )
        )
        if discarded_files:
            omissions.append(SteeringOmission(kind="files", root=str(root), count=discarded_files))

    try:
        if not _enter(root_fd, root):
            _ceiling_omissions(queued=0)
            return
        while stack:
            yield from pending_files
            pending_files.clear()
            fd, directory, remaining = stack[-1]
            if not remaining:
                stack.pop()
                _release(fd)
                continue
            name = remaining.pop()
            try:
                child_fd = os.open(name, pinned_fs.dir_flags(), dir_fd=fd)
            except OSError:
                unopened += 1
                continue
            if not _enter(child_fd, directory / name):
                # The ceiling fired inside this child: it, what it had listed,
                # plus every sibling still queued at every open level, is the
                # unlisted part.
                _ceiling_omissions(queued=sum(len(frame[2]) for frame in stack))
                return
        yield from pending_files
        if unopened and omissions is not None:
            omissions.append(SteeringOmission(kind="entries", root=str(root), count=unopened))
    finally:
        for fd, _directory, _remaining in stack:
            _release(fd)


def collect_folder_steering(
    steering_dirs: Sequence[str],
    *,
    project: str | None,
    home: Path | None = None,
    skip_delivered_roots: bool = True,
    delivered_sources: Collection[str] = (),
) -> SteeringCollection:
    """``(source_path, body)`` for every always-inclusion ``*.md`` under *steering_dirs*.

    Roots are read in the order given (the resolver hands them over root-first)
    and each document is emitted at most once across all of them, keyed by
    realpath. A missing root and an unreadable document are debug-logged and
    skipped so one stale path never breaks a turn.

    Two ceilings bound the pass -- ``_MAX_FOLDER_STEERING_DOCUMENTS`` on what is
    READ and ``_MAX_FOLDER_STEERING_ENTRIES`` on what is ENUMERATED per root --
    and each one that fires is COUNTED into ``omissions`` rather than dropped
    silently: past the document ceiling the remaining candidates are still
    enumerated (never opened, never read; the entry ceiling still bounds that
    enumeration) so their number is known per root. The renderer says the
    counts out loud once, so a capped tree never reads like a complete one.

    *skip_delivered_roots* says whether documents under the project's and the
    operator's ``.kiro/steering`` trees are ALREADY reaching the model by the
    provider's own steering path (kiro-cli loads them natively while the
    workspace inherits kiro-cli's default resources, the Claude Code seam loads
    them explicitly, KAS reports ``native_steering``) and so must not be re-sent
    here. A provider with no such path -- Codex, and the other
    harnesses that neither load ``.kiro/steering`` nor receive the explicit
    load -- passes ``False`` and receives them through the folder like any other
    document; skipping there would drop the rules with nothing delivered in
    their place. The default keeps the dedup for callers that know their
    provider delivers.

    *delivered_sources* names canonical document paths the caller already
    delivers. Matching candidates are skipped before they are read. The paths
    are compared as strings and are never resolved or otherwise opened here.

    *home* exists for the tests and for a caller that knows the operator home
    without paying ``Path.home()``; it defaults to ``Path.home()``.
    """
    result = SteeringCollection()
    documents = result.documents
    omissions = result.omissions
    if not steering_dirs:
        return result
    delivered_source_keys = {os.path.normcase(source) for source in delivered_sources}
    if not pinned_fs.supports_pinned_tree_walk():
        # Refuse BEFORE resolving any root: the walker's own refusal fires only
        # after ``validate_file_path`` has canonicalized the name, and on a host
        # without descriptor-relative opens that canonicalization is itself a
        # by-name touch of whatever the stored path points at now. The
        # validator refuses to store a list on such a host; a value that
        # predates that refusal is skipped here for the same reason.
        logger.warning(
            "folder steering skipped: this platform cannot open a directory relative "
            "to a descriptor, and a by-name walk could follow a swapped link"
        )
        return result
    skip_roots = _double_load_roots(project, home) if skip_delivered_roots else ()
    fence = memory_silo_fence()
    if not fence.complete:
        # Fail CLOSED, not narrowed: the default directories are not the fence
        # when the operator's table is unreadable, and a declared root that
        # contains an external workspace this process cannot see would carry
        # that person's memory Markdown into the prompt -- a V2 member's
        # included -- with nothing going red. Nothing is read; the section
        # says so in-band so the omission is visible in the prompt, and the
        # warning names the repair.
        logger.warning(
            "folder steering skipped: %s, so the memory-store fence is incomplete; "
            "%d declared steering root(s) were not read (repair config.json to restore)",
            fence.degraded,
            len(steering_dirs),
        )
        omissions.append(SteeringOmission(kind="fence", root="", count=len(steering_dirs)))
        return result
    silos = fence.roots
    seen: set[str] = set()
    for raw in steering_dirs:
        # ``validate_file_path`` is the hardened canonicalizer, not a bare
        # ``resolve()``: it refuses an untrusted UNC shape and screens Windows
        # link targets BEFORE anything is resolved (on Windows ``realpath`` on a
        # share is itself the outbound SMB probe), then fences sensitive paths.
        # The stored value was admitted the same way, but the directory it
        # names can be swapped for a link between admission and this read.
        root_str = validate_file_path(str(Path(raw).expanduser()))
        if root_str is None:
            logger.debug("folder steering directory refused or unresolvable: %r", raw)
            continue
        root_resolved = Path(root_str)
        if crosses_memory_silo(root_resolved, silos):
            # The validator refuses this at write time; a stored value that
            # predates it, or that was written through another path, must
            # not carry one store's memory into another store's prompt.
            logger.warning(
                "folder steering root refused: %s is, contains or lies inside a memory store",
                raw,
            )
            continue
        # No ``is_dir()`` here: that re-opens the validated name by path. The
        # walker opens the root through a pinned descriptor and refuses (with
        # the same debug line) anything that is not a plain directory by then.
        past_ceiling = 0
        for candidate in _walk_markdown(root_resolved, omissions):
            if len(documents) >= _MAX_FOLDER_STEERING_DOCUMENTS:
                # A running ceiling, not a post-collection trim: nothing past it
                # is opened or read, so a very large operator-pointed tree is
                # never materialized (each body is already per-doc capped, so
                # the count ceiling bounds the aggregate too). The candidates
                # ARE still counted -- the walk is lazy and itself bounded by
                # the entry ceiling -- so the omission below can name how many
                # documents the model is not seeing.
                past_ceiling += 1
                continue
            # Same screen as the root: a candidate can be a link whose target is
            # a share, and resolving it bare would be the probe.
            resolved_str = validate_file_path(str(candidate))
            if resolved_str is None:
                logger.debug("folder steering document refused or unresolvable: %s", candidate)
                continue
            resolved = Path(resolved_str)
            key = resolved_str
            if len(key) > _MAX_FOLDER_STEERING_SOURCE_LEN:
                logger.debug("folder steering document path too long: %d chars", len(key))
                continue
            if key in seen:
                continue
            if os.path.normcase(key) in delivered_source_keys:
                continue
            if any(_is_within(resolved, skip_root) for skip_root in skip_roots):
                continue
            if not _admissible(resolved, root_resolved):
                continue
            # Bounded read: the reader stops at ``_MAX_SOURCE_BYTES`` instead
            # of materializing an oversized file and slicing afterwards, and it
            # pins the opened descriptor inside this steering root, so a nested
            # directory swapped for a link between the walk and the open cannot
            # escape the tree. It returns ``None`` for a sensitive target, a
            # hardlink, a non-regular file, a file that vanished between the
            # glob and the read, or one that escaped the root -- all the same
            # skip. ``root_resolved`` is the canonical root admitted above;
            # passing it as canonical keeps the reader from re-resolving it, so
            # swapping the root itself for a link after admission cannot move
            # the boundary. Truncation can cut a multibyte sequence, so a
            # truncated body decodes leniently; a full body must still be valid
            # UTF-8.
            data = safe_read_file_bytes_nolink(
                key,
                within_root=str(root_resolved),
                within_root_is_canonical=True,
                max_bytes=_MAX_SOURCE_BYTES,
                allow_truncate=True,
            )
            if data is None:
                logger.debug("folder steering document unreadable: %s", key)
                continue
            try:
                body = data.decode(
                    "utf-8", errors="ignore" if len(data) >= _MAX_SOURCE_BYTES else "strict"
                )
            except UnicodeDecodeError:
                logger.debug("folder steering document is not UTF-8: %s", key)
                continue
            body = body.replace("\r\n", "\n").replace("\r", "\n")
            truncated = len(data) >= _MAX_SOURCE_BYTES
            fields, stripped = split_frontmatter(body, STEERING_LOADER)
            if truncated and not fields and body.lstrip().startswith("---"):
                # The frontmatter fence opened but its close fell past the read
                # cap, so the parser saw no fields at all -- including a possible
                # ``inclusion: manual``. Unknown inclusion is not ``always``;
                # skip rather than inject a document the author may have opted
                # out of the automatic load.
                logger.debug("folder steering document frontmatter truncated: %s", key)
                continue
            if fields.get("inclusion", "").strip().casefold() in _SKIPPED_INCLUSIONS:
                continue
            seen.add(key)
            # Retain the prompt-safe label, not the raw path: the collection is
            # the ONE boundary both consumers (the section renderer and the
            # member envelope) read from, so the fold happens once, here.
            documents.append((_source_label(key), stripped))
        if past_ceiling:
            logger.debug(
                "folder steering collection hit its %d-document ceiling; "
                "%d candidate(s) under %s not read",
                _MAX_FOLDER_STEERING_DOCUMENTS,
                past_ceiling,
                raw,
            )
            omissions.append(SteeringOmission(kind="documents", root=str(raw), count=past_ceiling))
    return result


#: Longest root path an omission notice prints. A root can be up to
#: ``MAX_FOLDER_STEERING_DIR_LEN`` (4096) characters, and the notices are the
#: part of a bounded section that is never cut, so their own size must be
#: bounded or a long path could push the reserved floor past ``max_chars``. The
#: TAIL is kept because the innermost names are what identifies a root.
_MAX_NOTICE_PATH_LEN = 200


def _notice_path(root: str) -> str:
    """The prompt-safe, bounded spelling of an omission notice's root.

    Same one-line rule as :func:`_source_label`: a directory name is
    operator-typed but host-nameable, and a newline inside it would end the
    notice line early and let the remainder pose as a fresh marker line. Fold
    first, then bound, so the bound applies to what is actually rendered.
    """
    root = _source_label(root)
    if len(root) <= _MAX_NOTICE_PATH_LEN:
        return root
    return "..." + root[-(_MAX_NOTICE_PATH_LEN - 3) :]


def render_omission_notice(omission: SteeringOmission) -> str:
    """One line naming what a fired ceiling left out of the section.

    Kept in the section's own bracket dialect (no Markdown heading, so it can
    never be mistaken for a document), with the root path bounded to
    ``_MAX_NOTICE_PATH_LEN`` characters so the line's length is bounded too.
    """
    if omission.kind == "fence":
        return (
            f"[FOLDER STEERING OMISSION: {omission.count} declared steering root(s) were "
            f"not read -- the configuration's workspace table could not be read, so the "
            f"memory-store fence is incomplete and no folder steering is loaded until "
            f"config.json is repaired.]"
        )
    root = _notice_path(omission.root)
    if omission.kind == "files":
        return (
            f"[FOLDER STEERING OMISSION: {omission.count} Markdown file(s) under {root} "
            f"were listed but not read -- the {_MAX_FOLDER_STEERING_ENTRIES}-entry ceiling "
            f"was reached while listing their directory. The standards above are incomplete.]"
        )
    if omission.kind == "documents":
        return (
            f"[FOLDER STEERING OMISSION: {omission.count} more Markdown file(s) under "
            f"{root} were not examined -- the {_MAX_FOLDER_STEERING_DOCUMENTS}-"
            f"document ceiling was reached, so whether they hold steering is unknown. "
            f"The standards above may be incomplete.]"
        )
    return (
        f"[FOLDER STEERING OMISSION: at least {omission.count} directory(ies) under "
        f"{root} were not listed -- the {_MAX_FOLDER_STEERING_ENTRIES}-entry "
        f"ceiling was reached or they could not be opened. The count is a lower bound: "
        f"an unlisted directory's own subtree was never seen. Documents inside them "
        f"are unknown; the standards above are incomplete.]"
    )


def render_folder_steering(
    collection: SteeringCollection | list[tuple[str, str]],
    *,
    max_chars: int | None = None,
    scrub: Callable[[str], str] | None = None,
) -> str:
    """Render *collection* as one prompt section; ``""`` when there is nothing to say.

    Returning ``""`` rather than a bare header matters: the caller appends the
    result to the context parts unconditionally, and an empty-but-present
    section would tell the model a folder declared standards it then cannot
    see.

    Every ceiling that fired during collection is said out loud once, after the
    documents and before the footer, with its count: a truncated tail otherwise
    reads exactly like a tree that never held those documents, and the model
    would apply a partial standard as if it were the whole one. A section can
    consist of omission notices alone (a root whose enumeration ceiling fired
    before any ``*.md`` was reached) -- that is still worth saying.

    With *max_chars* the section is BOUNDED without losing its shape: the
    header, every omission notice and the footer are reserved first, whole
    documents are admitted in order while they fit, at most ONE document -- the
    one straddling the budget -- is cut short with an inline marker naming the
    characters removed, and the documents left out are counted into one more
    omission line. A bare ``[:max_chars]`` slice would instead drop the very
    lines that say the section is incomplete, and the footer with them.

    *scrub*, when given, is applied to every source label and body BEFORE the
    section is assembled and costed, so the header and footer this function
    mints are the only genuine frame: a body that itself carries
    ``[FOLDER STEERING -- ...]`` or ``[END FOLDER STEERING]`` cannot close the
    real section early and open a forged one. The caller passes its prompt
    boundary scrub; this module stays free of the context machinery.
    """
    if isinstance(collection, SteeringCollection):
        documents = collection.documents
        omissions = collection.omissions
    else:
        documents = collection
        omissions = []
    if scrub is not None:
        documents = [(scrub(path), scrub(body)) for path, body in documents]
        # The notices name a root path too -- operator-typed, but a path is
        # host-nameable text and the notice line is rendered inside the
        # genuine frame with no later scrub, so it gets the same treatment.
        omissions = [
            SteeringOmission(kind=o.kind, root=scrub(o.root), count=o.count) for o in omissions
        ]
    if not documents and not omissions:
        return ""
    notices = [render_omission_notice(omission) for omission in omissions]
    if max_chars is None:
        return _assemble_section(
            [_document_block(path, body) for path, body in documents] + notices
        )
    return _render_bounded(documents, notices, max_chars)


def _document_block(path: str, body: str) -> str:
    return f"# {path}\n{body.strip()}"


def _assemble_section(blocks: list[str]) -> str:
    return f"{FOLDER_STEERING_HEADER}\n" + "\n\n".join(blocks) + f"\n{FOLDER_STEERING_FOOTER}"


def _budget_notice(dropped: int, cut: int, max_chars: int) -> str:
    parts: list[str] = []
    if cut:
        parts.append(f"{cut} document cut short")
    if dropped:
        parts.append(f"{dropped} more document(s) not loaded")
    return (
        f"[FOLDER STEERING OMISSION: {' and '.join(parts)} -- the folder steering "
        f"section is capped at {max_chars} characters. The standards above are incomplete.]"
    )


def _cut_marker(omitted: int) -> str:
    return f"\n...[document cut short: {omitted} characters omitted]"


def _collapsed_notice(count: int, max_chars: int) -> str:
    return (
        f"[FOLDER STEERING OMISSION: {count} notice(s) about incomplete steering were "
        f"collapsed into this line -- the folder steering section is capped at {max_chars} "
        "characters. The standards above are incomplete.]"
    )


def _fit_notices(notices: list[str], max_chars: int) -> list[str] | None:
    """The notice lines a *max_chars* section can carry, or ``None`` if not even the frame fits.

    The notices are the section's honesty and are reserved before any body, but
    they are text too and the bound has to bound them: sixteen roots with
    200-character paths and two fired ceilings each is more notice than a small
    cap allows. In order: keep them all if they fit; else collapse them into ONE
    line that says how many were collapsed; else cut that one line to the room
    left (it is this module's own text, so a cut cannot forge anything); else
    the cap cannot hold even the header and footer and there is no honest
    section to return.
    """
    if len(_assemble_section(notices)) <= max_chars:
        return notices
    collapsed = _collapsed_notice(len(notices), max_chars)
    if len(_assemble_section([collapsed])) <= max_chars:
        return [collapsed]
    room = max_chars - len(_assemble_section([""]))
    if room >= len("[FOLDER STEERING OMISSION: ...]"):
        return [collapsed[: room - 4] + "...]"]
    return None


def _render_bounded(documents: list[tuple[str, str]], notices: list[str], max_chars: int) -> str:
    """The bounded arm of :func:`render_folder_steering`; see its docstring.

    Invariant: every returned section is at most *max_chars* long and carries
    the header, the collection notices (whole when they fit, collapsed into one
    line when they do not -- see :func:`_fit_notices`), one budget notice when
    anything was left out, and the footer. Those lines are the section's
    honesty and are reserved before any document body spends a character. A
    cap too small to hold even the frame yields ``""`` -- there is no honest
    section that fits, and an over-cap one would be trimmed downstream at a
    place that says nothing about being incomplete.
    """
    fitted = _fit_notices(notices, max_chars)
    if fitted is None:
        logger.warning(
            "folder steering section cap %d cannot hold the section frame; nothing rendered",
            max_chars,
        )
        return ""
    notices = fitted
    # The budget notice is one more line the frame must have room for when
    # anything is left out; if it does not fit, the collapsed notice already
    # says the section is incomplete, and no document can be admitted anyway.
    kept: list[str] = []
    for path, body in documents:
        block = _document_block(path, body)
        if len(_assemble_section([*kept, block, *notices])) > max_chars:
            break
        kept.append(block)
    if len(kept) == len(documents):
        return _assemble_section([*kept, *notices])
    while True:
        remaining = len(documents) - len(kept)
        # Try to keep the head of the straddling document under a cut marker,
        # with the budget notice already counting it as cut.
        path, body = documents[len(kept)]
        stripped = body.strip()
        head = f"# {path}\n"
        notice = _budget_notice(remaining - 1, 1, max_chars)
        room = max_chars - len(
            _assemble_section([*kept, head + _cut_marker(len(stripped)), *notices, notice])
        )
        if room > 0:
            keep = stripped[:room]
            block = head + keep + _cut_marker(len(stripped) - len(keep))
            return _assemble_section([*kept, block, *notices, notice])
        # Not even a useful head fits: drop the straddler whole and say so.
        section = _assemble_section([*kept, *notices, _budget_notice(remaining, 0, max_chars)])
        if len(section) <= max_chars:
            return section
        if not kept:
            # No document admitted and the budget notice itself does not fit.
            # The fitted notices alone are within the cap by construction;
            # make sure they say the section is incomplete before returning.
            if notices and "incomplete" in notices[-1]:
                return _assemble_section(notices)
            squeezed = _fit_notices([*notices, _budget_notice(remaining, 0, max_chars)], max_chars)
            return _assemble_section(squeezed) if squeezed else ""
        kept.pop()
