"""Bounded, complete V2 persona and project documents; no memory search."""

from __future__ import annotations

import contextlib
import contextvars
import fnmatch
import logging
import os
from collections.abc import Callable, Iterator
from pathlib import Path
from typing import Any

from kiro_crew import agent as agent_mod
from kiro_crew import agent_spec_pins
from kiro_crew.agent_discovery import (
    AgentSpecPinError,
    _read_agent_spec,
    declared_spec_claim,
    project_agent_files,
)
from kiro_crew.agent_sdk.drivers import acp as acp_driver
from kiro_crew.config import KiroCrewConfig, config_dir
from kiro_crew.config.loader import workspace_dir_for
from kiro_crew.config.paths import project_agents_dir
from kiro_crew.frontmatter import STEERING_LOADER, split_frontmatter
from kiro_crew.hooks import FileTooLargeError, safe_read_file_bytes_nolink, validate_file_path
from kiro_crew.platform_compat import first_linked_ancestor, is_link_or_junction
from kiro_crew.validation import is_registered_agent_name

logger = logging.getLogger(__name__)

ESSENTIAL_MAX_CHARS = 64_000
_MAX_SOURCE_BYTES = ESSENTIAL_MAX_CHARS * 4
#: Source label of the in-band notice that names guides left out of the envelope.
ESSENTIAL_OMISSION_SOURCE = "essential-context#omitted"
#: Source-label prefix of the in-band notice that names wildcard entries pruned as
#: managed state; the full label appends ``:<template>``.
ESSENTIAL_MANAGED_SKIP_SOURCE = "essential-context#managed-skipped"
#: Source label of the in-band notice that names linked entries the implicit
#: steering scan left out instead of refusing the whole turn.
ESSENTIAL_LINKED_SKIP_SOURCE = "essential-context#linked-skipped"
#: Source-label prefix of the in-band notice that names declared literal
#: resources skipped because they do not exist; the full label appends ``:<template>``.
ESSENTIAL_MISSING_SKIP_SOURCE = "essential-context#missing-skipped"
#: Source-label prefix of the in-band notice that names paths an ABSOLUTE
#: declaration reaches through a link whose target is outside the admitted root
#: (home, reached through a linked folder such as ``~/workplace``), left out unread
#: instead of refusing the whole turn; the full label appends ``:<template>``.
ESSENTIAL_OUTSIDE_SKIP_SOURCE = "essential-context#outside-skipped"
#: Source-label prefix of the in-band notice that names steering documents the
#: no-follow descriptor read refused (a hard link, a reparse point that is not a
#: link) and that were left out instead of refusing the whole turn; the full
#: label appends ``:<template>``.
ESSENTIAL_UNREADABLE_SKIP_SOURCE = "essential-context#unreadable-skipped"
#: Source-label prefix of the in-band notice that names steering documents over
#: the per-file read cap, left out instead of refusing the whole turn; the full
#: label appends ``:<template>``.
ESSENTIAL_OVERSIZED_SKIP_SOURCE = "essential-context#oversized-skipped"
#: Source-label prefix of the in-band notice that names steering documents left
#: out because the snapshot already holds ``_MAX_DOCUMENTS`` documents, instead
#: of refusing the whole turn; the full label appends ``:<template>``.
ESSENTIAL_OVER_CAP_SKIP_SOURCE = "essential-context#over-cap-skipped"
_MAX_SKIPPED_LISTED = 10
_MAX_DIRECTORY_ENTRIES = 2048
_MAX_DOCUMENTS = 64
_CONDITIONAL_INCLUSIONS = frozenset({"manual", "filematch", "auto"})


def _steering_inclusion(fields: dict[str, str], path: Path) -> str:
    """A steering file's ``inclusion`` mode; an absent or unrecognised value reads as ``always``."""
    inclusion = fields.get("inclusion", "always").strip().casefold()
    if inclusion == "always" or inclusion in _CONDITIONAL_INCLUSIONS:
        return inclusion
    logger.warning("Steering %s: unknown inclusion %r, read as always", path, inclusion)
    return "always"


# kiro-cli re-reads its declared resources for every request and drops whole
# files once they total more than three bytes per token of the model's context
# window: about 492,000 bytes for deepseek-3.2's 164,000 tokens, the smallest
# window kiro-cli 2.27.0 lists. The model can change mid-session and a later
# kiro-cli may list a smaller window, so the launch record is kept only while the
# declared files total at most about a quarter of that, checked at launch and
# again on every read of the record; otherwise the folder sends every guide
# rather than skip one kiro-cli may have dropped.
_LAUNCH_RECORD_MAX_BYTES = 128_000


# A resources entry is a URI string this reader may open, or an object whose
# keys are kiro-cli's schema. The alias records the shape, not those keys.
ResourceDeclaration = str | dict[str, object]


class MemberEssentialContextError(ValueError):
    """A declared essential source cannot be included completely and safely."""


def _declared_document_count(resources: list[ResourceDeclaration]) -> int:
    """How many declared resources can become essential documents.

    Only ``file://`` declarations are ever read; ``skill://`` strings and
    object-form ``knowledgeBase`` declarations stay on demand and never enter
    the essentials snapshot, so they must not consume the document budget
    either. An agent that declares seventy skills and no files loads zero
    documents.
    """
    return sum(1 for r in resources if isinstance(r, str) and r.startswith("file://"))


class _ManagedEssentialSourceError(MemberEssentialContextError):
    """A managed source is excluded from wildcard discovery, never readable.

    *workspace_entry* is the top-level workspace entry name that matched the
    managed-name prefix, or ``None`` when the source sits under an admin root.
    """

    def __init__(self, message: str, *, workspace_entry: str | None = None) -> None:
        super().__init__(message)
        self.workspace_entry = workspace_entry


class _UnreadableEssentialSourceError(MemberEssentialContextError):
    """An admitted, contained source the no-follow descriptor read refused, or not UTF-8.

    Raised only after the path screen, the admitted-root containment check and
    the managed-state isolation have all passed, when
    :func:`safe_read_file_bytes_nolink` still returns nothing (the file is a hard
    link, carries a reparse point the read does not open, changed shape between
    the walk and the open, or is otherwise not a regular file it may read), or
    when the bytes it returns are not UTF-8 text. A steering document refused
    this way is left out and named; every other source keeps failing closed.
    """


class _OversizedEssentialSourceError(_UnreadableEssentialSourceError):
    """An admitted source larger than the per-file read cap, ``_MAX_SOURCE_BYTES``.

    Nothing past the cap is read. A steering document refused this way is left
    out and named with its *size*, like any other unreadable steering, because
    it could never fit the essentials envelope whole and a truncated prefix would
    present partial rules as complete. Every other source keeps failing closed,
    and the message names the file, its size and the cap.
    """

    def __init__(self, message: str, *, size: int) -> None:
        super().__init__(message)
        self.size = size


class _OutsideRootEssentialSourceError(MemberEssentialContextError):
    """A source whose real path is not inside its admitted root; nothing was read.

    Raised by :func:`_read` only for the containment failure, after the path
    screen admitted both the source and the root. A resource the template
    declares by absolute path and that fails this way is left out unread and
    named (see :func:`_outside_declared_note`); every other source keeps
    failing closed.
    """


#: Top-level workspace names the product itself writes as managed state. An entry
#: the prefix prune catches under one of these names is the store working as
#: intended; any other name is a prefix collision with ordinary project content.
_MANAGED_EXACT_NAMES = frozenset({"memory", "lessons", ".lessons"})
#: Managed files whose name may carry a suffix (SQLite ``-wal``/``-shm`` sidecars).
_MANAGED_NAME_STEMS = ("memory.db", "memory_index.db", "lessons.jsonl")


def _is_prefix_collision(name: str) -> bool:
    """Whether a pruned top-level *name* is ordinary content, not managed state."""
    folded = name.casefold()
    return folded not in _MANAGED_EXACT_NAMES and not folded.startswith(_MANAGED_NAME_STEMS)


_RootTable = tuple[list[Path], list[Path], dict[Path, Path]]
#: The root table of the one :func:`documents_for_member` read running on this
#: context, or ``None`` outside one.
_managed_roots: contextvars.ContextVar[_RootTable | None] = contextvars.ContextVar(
    "kirocrew_member_managed_roots", default=None
)


def _managed_root_table(*, refuse_unverifiable: bool = False) -> _RootTable:
    """The admin roots, the workspace roots, and their compared spellings, read now.

    With *refuse_unverifiable*, a root the path screen cannot admit raises
    :class:`_ManagedEssentialSourceError` instead of keeping its lexical spelling.
    """
    cfg = KiroCrewConfig.load()
    roots = [config_dir(), Path.home() / ".kiro/crew", Path.home() / ".kirocrew"]
    workspaces = [config_dir() / "workspace"]
    workspaces.extend(workspace_dir_for(name) for name in cfg.workspaces)
    resolved_roots = {
        root: (
            _comparable_root(root, refuse_unverifiable=True)
            if refuse_unverifiable
            else _comparable_root(root)
        )
        for root in dict.fromkeys([*roots, *workspaces])
    }
    return roots, workspaces, resolved_roots


@contextlib.contextmanager
def _managed_roots_scope() -> Iterator[None]:
    """Read the root table once for one document read; nested scopes reuse it.

    One member turn checks ~37 paths, and each fresh table costs a config load per
    workspace plus a guarded resolve per root (~16 ms). The table lives only for
    this read: the next turn reads configuration and link targets again.
    """
    if _managed_roots.get() is not None:
        yield
        return
    token = _managed_roots.set(_managed_root_table())
    try:
        yield
    finally:
        _managed_roots.reset(token)


def _refuse_managed_source(path: Path) -> None:
    """Resources cannot reopen Global V1 or a peer's managed member state.

    The ROOTS are compared in their realpath spelling; the CANDIDATE is only
    ``abspath``-normalized. The asymmetry is deliberate on both sides.

    Roots must resolve because ``abspath`` follows no link: a candidate that
    arrived resolved -- as an expanded glob match does -- matches no root whose
    spelling still carries a symlink, which skips this isolation entirely on a
    host whose home is reached through one. A workspace root is configuration,
    but configuration a dashboard caller can write (an absolute ``dir``), so it
    is resolved through the same ``validate_file_path`` screen as everything
    else here (:func:`_comparable_root`): a UNC-shaped root is compared
    lexically instead of being resolved, because ``realpath`` on it would be
    the outbound SMB probe.

    The candidate must NOT resolve, because it can be an unvalidated caller path
    and ``realpath`` on one is itself an outbound probe for a UNC target on
    Windows -- the same reason ``validate_file_path`` screens UNC shapes BEFORE
    resolving anything. Callers that hold a validated path pass it already
    resolved, so those comparisons are exact; ``_read`` additionally re-checks the
    validated path, so the pre-validation call never has to be the deciding one.
    """
    _refuse_managed_candidate(path, _managed_roots.get() or _managed_root_table())


def _refuse_managed_candidate(path: Path, table: _RootTable) -> None:
    """:func:`_refuse_managed_source` against one already-read root *table*."""
    roots, workspaces, resolved_roots = table
    candidate = Path(os.path.abspath(path))
    in_workspace = False
    for workspace in workspaces:
        workspace = resolved_roots[workspace]
        admin_overlap = False
        for admin in roots:
            admin = resolved_roots[admin]
            if admin.is_relative_to(workspace):
                admin_overlap = True
            elif workspace.is_relative_to(admin):
                top = workspace.relative_to(admin).parts[0].casefold()
                if top in {
                    "members",
                    "member-rules",
                    "member-records",
                    "backups",
                    "trust",
                } or top.startswith(("memory", "lessons")):
                    admin_overlap = True
        if admin_overlap:
            continue
        if candidate.is_relative_to(workspace):
            parts = candidate.relative_to(workspace).parts
            if parts and parts[0].casefold().startswith(("memory", "lessons", ".lessons")):
                raise _ManagedEssentialSourceError(
                    f"Essential source {path}: managed memory/member state cannot be a project resource",
                    workspace_entry=parts[0],
                )
            in_workspace = True
    if not in_workspace and any(candidate.is_relative_to(resolved_roots[root]) for root in roots):
        raise _ManagedEssentialSourceError(
            f"Essential source {path}: managed memory/member state cannot be a project resource"
        )


def member_context_identity(member: str, *, member_is_id: bool = True) -> tuple[str, str]:
    """Resolve an explicit ID or configured name without touching learned memory."""
    if not member:
        return "", ""
    from kiro_crew.execution_context import member_config_for_id

    cfg = KiroCrewConfig.load()
    member_id = member
    if not member_is_id:
        configured = cfg.agents.get(member)
        if configured is not None and not configured.member_id:
            return "", ""
        member_id = configured.member_id if configured else ""
    _, configured_member = member_config_for_id(cfg, member_id)
    return member_id, configured_member.kiro_agent or "kirocrew"


def _comparable_root(root: Path, *, refuse_unverifiable: bool = False) -> Path:
    """The spelling a root is compared against in :func:`_refuse_managed_source`.

    Resolved through ``validate_file_path`` when that screen admits the root, so
    a symlinked spelling matches resolved candidates. A root the screen refuses
    (a UNC share not on the trusted list, a sensitive path) is never handed to
    ``realpath`` -- on Windows that resolution is itself the network probe --
    and keeps the lexical ``abspath`` comparison this check always had, unless
    *refuse_unverifiable* asks for :class:`_ManagedEssentialSourceError` instead.
    """
    admitted = _admitted_root(root)
    if admitted is None and refuse_unverifiable:
        raise _ManagedEssentialSourceError(
            f"Essential source {root}: managed root cannot be verified"
        )
    return admitted if admitted is not None else Path(os.path.abspath(root))


def _admitted_root(root: Path) -> Path | None:
    """Normalize a declared root to the spelling admitted paths are compared against.

    ``validate_file_path`` returns a fully RESOLVED path, so a root that still
    carries a symlink in its own spelling matches no document at all: on a host
    whose ``$HOME`` is ``/home/<user>`` linking to ``/local/home/<user>``,
    ``Path.home()`` IS the link, every admitted path resolves past it, and the
    containment check below is false for every source. The ``project`` root is
    already stored resolved by ``documents_for_member``; this gives a root taken
    from ``Path.home()`` the same treatment instead of leaving the caller to
    remember it.

    Containment stays exact -- a document's real path must still sit inside the
    real root -- and this says nothing about paths BELOW the root, which the
    walk still refuses when they are, or sit under, a link. Only the declared
    root's own spelling is normalized, and that root comes from configuration
    rather than from scanned content.
    """
    admitted = validate_file_path(str(root))
    return None if admitted is None else Path(admitted)


def _read(path: Path, root: Path) -> str:
    try:
        _refuse_managed_source(path)
        admitted = validate_file_path(str(path))
        admitted_root = _admitted_root(root)
        if admitted is None or admitted_root is None:
            raise ValueError("outside the admitted document root")
        # Before the containment check: an outside source is skippable, managed
        # state reached through a link is not.
        _refuse_managed_source(Path(admitted))
        if not Path(admitted).is_relative_to(admitted_root):
            raise _OutsideRootEssentialSourceError(
                f"Essential source {path}: outside the admitted document root"
            )
        # Pin this admitted parent, not the whole home. A racing ancestor
        # redirect cannot reach managed data allowed by the generic V1 gate.
        try:
            data = safe_read_file_bytes_nolink(
                str(path),
                within_root=str(Path(admitted).parent),
                max_bytes=_MAX_SOURCE_BYTES,
                allow_truncate=False,
            )
        except FileTooLargeError as exc:
            # Not an OSError or ValueError, so without this it escaped with no path.
            # The size is the one the read measured on the descriptor it opened;
            # an error without a measured size fails closed instead of skipping,
            # and the ValueError handler below names the path.
            if exc.size is None:
                raise MemberEssentialContextError(str(exc)) from exc
            raise _OversizedEssentialSourceError(
                f"Essential source {path}: {exc}", size=exc.size
            ) from exc
        if data is None:
            raise _UnreadableEssentialSourceError(
                f"Essential source {path}: missing, unreadable, or outside the admitted "
                "document root"
            )
        # utf-8-sig drops one leading byte-order mark ("UTF-8 with BOM" editors).
        # Left in, the mark sits in front of the frontmatter fence, the column-0
        # grammar finds no block, and a manual or fileMatch file reads as always.
        # A file without the mark decodes exactly as with plain utf-8.
        try:
            text = data.decode("utf-8-sig")
        except UnicodeDecodeError as exc:
            # UTF-16 or an ANSI code page (Windows PowerShell 5.1's defaults).
            raise _UnreadableEssentialSourceError(
                f"Essential source {path}: not UTF-8 text ({exc})"
            ) from exc
        return text.replace("\r\n", "\n").replace("\r", "\n")
    except (_UnreadableEssentialSourceError, _OutsideRootEssentialSourceError):
        raise
    except (OSError, ValueError) as exc:
        raise MemberEssentialContextError(f"Essential source {path}: {exc}") from exc


def _read_implicit_guide(path: Path, root: Path) -> str | None:
    """Read a project-root guide the template did not declare, or ``None`` if refused.

    ``AGENTS.md`` and ``SOUL.md`` are picked up because they exist, not because
    the template names them, so a refused one must not refuse the whole turn:
    a guide symlinked to a repository outside the project, or a link whose
    target has gone, would otherwise abort every session start of the agent. The read is
    the same :func:`_read` every declared source goes through -- containment,
    managed-state isolation, the no-follow descriptor read -- so nothing it
    refuses is read here either; only the refusal's consequence differs. A
    declared source keeps failing closed, and an oversized guide still raises,
    because that is a guide the user can shorten rather than one this reader
    may not open.
    """
    try:
        return _read(path, root)
    except _OversizedEssentialSourceError:
        raise
    except MemberEssentialContextError as exc:
        reason = str(exc.__cause__ or exc)
        logger.warning("Project guide %s not loaded: %s", path, reason)
        return None


def _omitted_guide(path: Path, root: Path) -> tuple[str, str]:
    """The in-band note that stands in for a guide :func:`_read_implicit_guide` refused."""
    return (
        f"{path}#omitted",
        f"PROJECT GUIDE NOT LOADED. {path.name} in {root} could not be read safely: it is "
        "a link to a file outside this project, a link whose target is missing, a managed "
        "memory file, a hard link, or otherwise unreadable. Do not assume its contents. "
        f"If it should apply, ask the user to make {path.name} a regular file inside {root}.",
    )


def _skipped_managed_note(skipped: list[Path], template: str) -> tuple[str, str] | None:
    """The in-band note naming wildcard entries pruned as managed state, or ``None``.

    The prune is a name-prefix heuristic and stays fail-safe: an entry whose
    top-level name starts with ``memory``/``lessons`` in a managed workspace is
    never read. An ordinary project entry can share that prefix, so -- like the
    ``#omitted`` note for a refused guide -- the agent is told which entries were
    left out instead of the snapshot dropping them without a trace.

    The source is keyed by *template*: a turn that merges the owner template's
    documents with a different execution template's must see two distinct
    sources, since each template's globs can skip different entries.
    """
    unique = list(dict.fromkeys(skipped))
    if not unique:
        return None
    listed = ", ".join(str(path) for path in unique[:_MAX_SKIPPED_LISTED])
    more = len(unique) - _MAX_SKIPPED_LISTED
    if more > 0:
        listed += f", and {more} more"
    return (
        f"{ESSENTIAL_MANAGED_SKIP_SOURCE}:{template}",
        f"PROJECT ENTRIES NOT LOADED. {len(unique)} entr{'y' if len(unique) == 1 else 'ies'} "
        f"matched a declared resource pattern but were skipped because the name matches "
        f"managed memory/lessons state: {listed}. Do not assume their contents. If one is "
        "project content, ask the user to rename it so its name does not start with "
        "'memory' or 'lessons'.",
    )


def _skipped_linked_note(linked: list[Path]) -> tuple[str, str] | None:
    """The in-band note naming steering links the implicit scan left out, or ``None``.

    Global and project steering are read because they exist, not because a
    template names them -- the same footing as the project-root ``AGENTS.md`` in
    :func:`_read_implicit_guide`. A link there therefore must not refuse every
    session start of the member; it is left out, still unread, and named here so
    the agent does not assume its contents. kiro-cli itself follows the link and
    may load the file natively.
    """
    unique = list(dict.fromkeys(linked))
    if not unique:
        return None
    listed = ", ".join(str(path) for path in unique[:_MAX_SKIPPED_LISTED])
    more = len(unique) - _MAX_SKIPPED_LISTED
    if more > 0:
        listed += f", and {more} more"
    return (
        ESSENTIAL_LINKED_SKIP_SOURCE,
        f"LINKED STEERING NOT IN THIS SNAPSHOT. {len(unique)} "
        f"entr{'y' if len(unique) == 1 else 'ies'} on the steering scan path "
        f"{'is a link' if len(unique) == 1 else 'are links'}, which this snapshot does "
        f"not read: {listed}. The host may still load linked steering natively, so its "
        "text can appear elsewhere in your context; this note only says the snapshot "
        "does not carry it.",
    )


def _skipped_unreadable_note(unreadable: list[Path], template: str) -> tuple[str, str] | None:
    """The in-band note naming steering documents the descriptor read refused, or ``None``.

    An installer that places steering by hard link (or a file that carries a
    reparse point that is not a link) passes the path screen and containment,
    then is refused by :func:`safe_read_file_bytes_nolink`. The file is still
    not read; only the consequence differs: it is left out and named here, so
    the member starts and the agent does not assume its contents. kiro-cli
    reads the file natively.
    """
    unique = list(dict.fromkeys(unreadable))
    if not unique:
        return None
    listed = ", ".join(str(path) for path in unique[:_MAX_SKIPPED_LISTED])
    more = len(unique) - _MAX_SKIPPED_LISTED
    if more > 0:
        listed += f", and {more} more"
    return (
        f"{ESSENTIAL_UNREADABLE_SKIP_SOURCE}:{template}",
        f"STEERING NOT IN THIS SNAPSHOT. {len(unique)} steering "
        f"file{'' if len(unique) == 1 else 's'} could not be read safely (a hard link, "
        f"a reparse point that is not a link, or otherwise unreadable) and "
        f"{'was' if len(unique) == 1 else 'were'} left out: {listed}. The host may "
        "still load them natively, so their text can appear elsewhere in your context; "
        "this note only says the snapshot does not carry them.",
    )


def _skipped_oversized_note(
    oversized: list[tuple[Path, int]], template: str
) -> tuple[str, str] | None:
    """The in-band note naming steering documents over the read cap, or ``None``.

    Each entry is ``(path, measured size in bytes)``. The file is not read past
    the cap and is left out whole, so the member starts and the agent does not assume
    its contents; the note gives the size and the cap so the user knows what to
    trim. kiro-cli applies its own limits when it loads the file natively.
    """
    unique = list(dict.fromkeys(oversized))
    if not unique:
        return None
    listed = ", ".join(f"{path} ({size:,} bytes)" for path, size in unique[:_MAX_SKIPPED_LISTED])
    more = len(unique) - _MAX_SKIPPED_LISTED
    if more > 0:
        listed += f", and {more} more"
    return (
        f"{ESSENTIAL_OVERSIZED_SKIP_SOURCE}:{template}",
        f"STEERING NOT IN THIS SNAPSHOT. {len(unique)} steering "
        f"file{'' if len(unique) == 1 else 's'} {'is' if len(unique) == 1 else 'are'} "
        f"over the {_MAX_SOURCE_BYTES:,}-byte per-file limit and "
        f"{'was' if len(unique) == 1 else 'were'} left out: {listed}. The host may "
        "still load them natively, so their text can appear elsewhere in your context; "
        "this note only says the snapshot does not carry them. To load one here, "
        "ask the user to shorten it or split it into smaller files.",
    )


def _skipped_over_cap_note(over_cap: list[Path], template: str) -> tuple[str, str] | None:
    """The in-band note naming steering left out past the document cap, or ``None``.

    Global and project steering are read because they exist, so a steering folder
    with more than ``_MAX_DOCUMENTS`` files must not stop every member turn. The
    first files in the scan's sorted order are kept, the rest are left out unread,
    and this note says how many and names the first few. A member's own sources
    (persona, ``SOUL.md``, declared non-steering resources) take a slot from the
    last kept steering file before they would be refused.
    """
    # Sorted, so the first files named are the ones right after the last kept one.
    unique = sorted(dict.fromkeys(over_cap), key=str)
    if not unique:
        return None
    listed = ", ".join(str(path) for path in unique[:_MAX_SKIPPED_LISTED])
    more = len(unique) - _MAX_SKIPPED_LISTED
    if more > 0:
        listed += f", and {more} more"
    return (
        f"{ESSENTIAL_OVER_CAP_SKIP_SOURCE}:{template}",
        f"STEERING NOT IN THIS SNAPSHOT. {len(unique)} steering "
        f"file{'' if len(unique) == 1 else 's'} {'was' if len(unique) == 1 else 'were'} "
        f"left out because this snapshot holds at most {_MAX_DOCUMENTS} documents: "
        f"{listed}. The host may still load them natively, so their text can appear "
        "elsewhere in your context; this note only says the snapshot does not carry "
        "them. To load them here, ask the user to merge small steering files or move "
        "ones this agent does not need out of the steering folder.",
    )


def _is_absent(path: Path, admitted_root: Path) -> bool:
    """Whether a declared literal *path* below *admitted_root* simply does not exist.

    True only when some component of the path is missing and no component the
    probe reached on the way is a link: every ``lstat`` runs root-first and only
    after the components above it are known not to be links, so the probe never
    traverses one (the same order :func:`first_linked_ancestor` relies on). A
    link anywhere on the path -- dangling or not -- a present but unreadable
    file, and a managed-state path all answer False, so :func:`_read` refuses
    them exactly as it would have.
    """
    try:
        _refuse_managed_source(path)
    except _ManagedEssentialSourceError:
        return False
    if not path.is_relative_to(admitted_root):
        return False
    current = admitted_root
    for part in path.relative_to(admitted_root).parts:
        current = current / part
        if is_link_or_junction(current):
            return False
        try:
            os.lstat(current)
        except (FileNotFoundError, NotADirectoryError):
            return True
        except (OSError, ValueError):
            # Present but not probeable (an unsearchable parent): not absent.
            return False
    return False


def _missing_declared_note(missing: list[Path], template: str) -> tuple[str, str] | None:
    """The in-band note naming declared resources that do not exist, or ``None``.

    kiro-cli skips a declared ``file://`` resource that is absent, so a template
    shipping ``file://AGENTS.md`` (or a subagent starting in an empty folder)
    must not refuse the session here either. Like the ``#omitted`` note for a
    refused guide, the agent is told which declared files were not there.
    """
    unique = list(dict.fromkeys(missing))
    if not unique:
        return None
    listed = ", ".join(str(path) for path in unique[:_MAX_SKIPPED_LISTED])
    more = len(unique) - _MAX_SKIPPED_LISTED
    if more > 0:
        listed += f", and {more} more"
    return (
        f"{ESSENTIAL_MISSING_SKIP_SOURCE}:{template}",
        f"DECLARED RESOURCES NOT FOUND. {len(unique)} "
        f"file{'' if len(unique) == 1 else 's'} the agent template declares "
        f"{'does' if len(unique) == 1 else 'do'} not exist and "
        f"{'was' if len(unique) == 1 else 'were'} skipped: {listed}. Do not assume "
        "their contents.",
    )


def _outside_declared_note(outside: list[Path], template: str) -> tuple[str, str] | None:
    """The in-band note naming absolute declarations left out as outside home, or ``None``.

    On a cloud desktop a folder under home (such as ``~/workplace``) is often
    a link to another volume. kiro-cli follows the link and loads what the
    template declares there; the snapshot does not read outside its root, so the
    link is left out unread and named here, and the member still starts.
    """
    unique = list(dict.fromkeys(outside))
    if not unique:
        return None
    listed = ", ".join(str(path) for path in unique[:_MAX_SKIPPED_LISTED])
    more = len(unique) - _MAX_SKIPPED_LISTED
    if more > 0:
        listed += f", and {more} more"
    return (
        f"{ESSENTIAL_OUTSIDE_SKIP_SOURCE}:{template}",
        f"DECLARED RESOURCES NOT IN THIS SNAPSHOT. {len(unique)} "
        f"path{'' if len(unique) == 1 else 's'} the agent template declares by absolute "
        f"path {'resolves' if len(unique) == 1 else 'resolve'} outside the home folder "
        f"(for example through a linked folder) and {'was' if len(unique) == 1 else 'were'} "
        f"left out unread: {listed}. The host may still load them natively, so their text "
        "can appear elsewhere in your context; this note only says the snapshot does not "
        "carry them. To load one here, ask the user to keep a regular copy of it inside "
        "the home folder and declare that path.",
    )


def _matches(
    root: Path,
    pattern: str,
    skipped: list[Path] | None = None,
    linked: list[Path] | None = None,
    missing: list[Path] | None = None,
    *,
    confined: bool = False,
    outside: list[Path] | None = None,
    over_cap: list[Path] | None = None,
) -> list[Path]:
    """Expand a declared glob with bounded directory work and no link traversal.

    A glob matching more than ``_MAX_DOCUMENTS`` documents refuses, unless
    *over_cap* is given: then the first ``_MAX_DOCUMENTS`` in sorted order are
    returned and the rest are appended there, unread. The steering scans pass it,
    so a large steering folder is loaded up to the cap instead of failing the
    turn. The walk itself stays bounded by ``_MAX_DIRECTORY_ENTRIES``.

    A link the walk meets below *root* is refused, unless *linked* is given: then
    it is appended there and left out, unread and not descended into. The implicit
    steering scan passes *linked* alone, so it leaves a link out wherever it
    points. A declared steering glob also passes *confined*: only a link whose
    target the path screen resolves inside *root* goes to *linked*, and any other
    link refuses unless *outside* takes it.

    An ABSOLUTE declaration passes *outside*, and a relative steering glob passes
    its *linked* list as *outside*: a link the path screen resolves
    to a target outside *root* (``~/workplace`` linked to another volume) is appended
    there and left out, unread and not descended into, instead of refusing. A
    target that is managed memory state still refuses, and so does a link the
    screen refuses or that stays inside *root* under a glob that does not leave it
    out.

    A glob-free *pattern* naming a path that does not exist is appended to
    *missing*, when given, and matches nothing -- kiro-cli skips an absent
    declared resource the same way. Only plain absence counts (see
    :func:`_is_absent`); a link, an unreadable file or an out-of-root path is
    still returned and refused by :func:`_read`.

    An entry the glob would have used but the managed-source check prunes for a
    prefix collision (a name like ``memory-notes`` that is not one of the store's
    own names) is logged as a warning and, when *skipped* is given, appended to
    it, so the caller can tell the agent which entries were left out. Pruning the
    store's real ``memory``/``lessons`` entries is logged at debug level only.
    """
    pieces = Path(pattern).parts
    if Path(pattern).is_absolute() or ".." in pieces:
        raise MemberEssentialContextError(
            f"Essential source {root / pattern}: outside admitted root"
        )
    # Walk the root in its admitted spelling: an unresolved root whose own path
    # contains a symlink would otherwise be refused as a linked directory on its
    # very first visit, before any document is considered.
    admitted_root = _admitted_root(root)
    if admitted_root is None:
        raise MemberEssentialContextError(f"Essential source {root}: outside admitted root")

    def _escapes(link: Path) -> bool:
        """Whether *confined* forbids leaving *link* out: its target is not inside *root*."""
        if not confined:
            return False
        target = validate_file_path(str(link))
        return target is None or not Path(target).is_relative_to(admitted_root)

    def _left_out_outside(link: Path) -> bool:
        """Whether *outside* leaves *link* out; it is then appended there, unread.

        Nothing under the target is opened or listed. A managed-state target
        raises: a link must not turn another member's store into a quiet skip.
        """
        if outside is None:
            return False
        target = validate_file_path(str(link))
        if target is None or Path(target).is_relative_to(admitted_root):
            return False
        _refuse_managed_source(Path(target))
        logger.warning("Essential source %s left out: it links outside %s", link, root)
        outside.append(link)
        return True

    if not any(c in pattern for c in "*?["):
        path = admitted_root / pattern
        if missing is not None and _is_absent(path, admitted_root):
            missing.append(path)
            return []
        return [path]
    pending = [(admitted_root, 0)]
    result: set[Path] = set()
    pruned: set[Path] = set()
    scanned = 0
    visited: set[tuple[Path, int]] = set()
    while pending:
        directory, offset = pending.pop()
        if (directory, offset) in visited:
            continue
        visited.add((directory, offset))
        admitted = validate_file_path(str(directory))
        if admitted is None or not Path(admitted).is_relative_to(admitted_root):
            raise MemberEssentialContextError(
                f"Essential source {directory}: outside admitted root"
            )
        _refuse_managed_source(Path(admitted))
        if first_linked_ancestor(directory) or is_link_or_junction(directory):
            raise MemberEssentialContextError(f"Essential source {directory}: linked directory")
        component = pieces[offset]
        if not any(c in component for c in "*?["):
            # Literal prefixes need no directory listing. A large unrelated
            # project root must not exhaust a steering subtree's scan budget.
            path = directory / component
            if is_link_or_junction(path):
                if linked is not None and not _escapes(path):
                    linked.append(path)
                    continue
                if _left_out_outside(path):
                    continue
                raise MemberEssentialContextError(
                    f"Essential source {path}: linked document or directory"
                )
            if offset + 1 < len(pieces):
                pending.append((path, offset + 1))
            elif path.is_file():
                result.add(path)
            continue
        if component == "**" and offset + 1 < len(pieces):
            pending.append((directory, offset + 1))
        try:
            with os.scandir(directory) as entries:
                for entry in entries:
                    scanned += 1
                    if scanned > _MAX_DIRECTORY_ENTRIES:
                        raise MemberEssentialContextError(
                            f"Essential source {root / pattern}: too many directory entries"
                        )
                    path = Path(entry.path)
                    matches = component == "**" or fnmatch.fnmatchcase(entry.name, component)
                    if not matches:
                        continue
                    # Wildcards discover project guides, not managed state. Prune
                    # before descent; literal prefixes and reads still refuse it.
                    try:
                        _refuse_managed_source(path)
                    except _ManagedEssentialSourceError as exc:
                        # The prune keeps the name-prefix heuristic fail-safe, so
                        # an ordinary project entry can be caught by it: say so
                        # instead of dropping it silently. Only an entry the glob
                        # would otherwise have descended into or returned counts,
                        # and only a prefix COLLISION is reported -- the real
                        # managed store pruned by a broad glob is routine.
                        is_dir = entry.is_dir(follow_symlinks=False)
                        wanted = (is_dir and (component == "**" or offset + 1 < len(pieces))) or (
                            not is_dir and offset == len(pieces) - 1
                        )
                        if wanted and path not in pruned:
                            pruned.add(path)
                            name = exc.workspace_entry
                            if name is not None and _is_prefix_collision(name):
                                logger.warning(
                                    "Essential source %s skipped by %s: its name matches "
                                    "managed memory/lessons state",
                                    path,
                                    pattern,
                                )
                                if skipped is not None:
                                    skipped.append(path)
                            else:
                                logger.debug("Managed state %s pruned from %s", path, pattern)
                        continue
                    if is_link_or_junction(path):
                        if linked is not None and not _escapes(path):
                            # Name only a link the glob could have used: a document
                            # matching the final component, or a directory to descend.
                            # The directory probe follows the link, so the target is
                            # screened first (a UNC target is never probed); a refused
                            # target is named without probing.
                            if (
                                fnmatch.fnmatchcase(entry.name, pieces[-1])
                                or validate_file_path(str(path)) is None
                                or entry.is_dir()
                            ):
                                linked.append(path)
                            continue
                        if _left_out_outside(path):
                            continue
                        raise MemberEssentialContextError(
                            f"Essential source {path}: linked document or directory"
                        )
                    if entry.is_dir(follow_symlinks=False):
                        if component == "**" or offset + 1 < len(pieces):
                            pending.append((path, offset if component == "**" else offset + 1))
                    elif offset == len(pieces) - 1:
                        result.add(path)
                        if over_cap is None and len(result) > _MAX_DOCUMENTS:
                            raise MemberEssentialContextError(
                                f"Essential source {root / pattern}: too many documents"
                            )
        except FileNotFoundError:
            continue  # A declared glob matching no existing source is valid.
        except OSError as exc:
            raise MemberEssentialContextError(f"Essential source {directory}: {exc}") from exc
    return sorted(result)


def resolve_template_path(template: str, project: str | None = None) -> Path | None:
    """Resolve one template, with a project override ahead of the global copy."""
    spec_path: Path | None = None
    if project:
        admitted = validate_file_path(project)
        if admitted is None:
            raise MemberEssentialContextError(f"Essential project {project}: cannot be read safely")
        for path in project_agent_files(
            Path(admitted), operation="member_essentials", source="context"
        ):
            spec = _read_agent_spec(path, operation="member_essentials", source="context")
            if spec is None and path.stem == template:
                raise MemberEssentialContextError(
                    f"Essential template {path}: cannot be read safely"
                )
            if spec is not None and spec.get("name", path.stem) == template:
                if spec_path is not None:
                    raise MemberEssentialContextError(f"Ambiguous essential template {template!r}")
                spec_path = path
    user_refusal: ValueError | None = None
    if spec_path is None:
        try:
            spec_path = agent_mod.session_agent_spec_path(template)
        except AgentSpecPinError as exc:
            if exc.paths:
                raise MemberEssentialContextError(
                    f"Essential template {template!r}: {exc}"
                ) from exc
            # The pin names a user-level file, and no user-level file declares the
            # name: the template may be a crewmate's own, so the mate scope answers
            # below, and this refusal stands if it does not.
            user_refusal = exc
        except ValueError as exc:
            raise MemberEssentialContextError(f"Essential template {template!r}: {exc}") from exc
    if spec_path is None:
        # A crewmate's own config lives in the mate store, not the user-level
        # agents dir the picker reads. Mirror the resolution order the KAS and
        # loader seams use: user-level first, then the mate scope, before giving
        # up. The mate file is a full, trusted custom-agent spec, so it is read
        # the same way as a user-level one.
        from kiro_crew.config.paths import mate_agents_dir

        try:
            spec_path = agent_mod.session_agent_spec_path(template, agents_dir=mate_agents_dir())
        except AgentSpecPinError as exc:
            if exc.paths:
                raise MemberEssentialContextError(
                    f"Essential template {template!r}: {exc}"
                ) from exc
            # No crewmate file declares the name, so the mate scope has no answer:
            # a user-level pin on a linked file is read by the linked reader below
            # (``read_template_spec``), and a user-level refusal still stands.
        except ValueError as exc:
            raise MemberEssentialContextError(f"Essential template {template!r}: {exc}") from exc
    if spec_path is None and user_refusal is not None:
        raise MemberEssentialContextError(
            f"Essential template {template!r}: {user_refusal}"
        ) from user_refusal
    return spec_path


def read_template_spec(
    template: str, project: str | None, *, operation: str, source: str
) -> tuple[Path, dict[str, Any] | None] | None:
    """Resolve and parse one template; the spec is ``None`` when it is unusable.

    :func:`resolve_template_path` answers first. A template it does not find is
    looked up by declared name in the global agents directory through
    :func:`linked_global_template_spec`, so a linked global spec is read once,
    behind the managed-state fence.
    """
    spec_path = resolve_template_path(template, project)
    if spec_path is not None:
        return spec_path, _read_agent_spec(spec_path, operation=operation, source=source)
    try:
        return linked_global_template_spec(template, operation=operation, source=source)
    except ValueError as exc:
        raise MemberEssentialContextError(f"Essential template {template!r}: {exc}") from exc


def linked_global_template_spec(
    template: str, *, operation: str, source: str
) -> tuple[Path, dict[str, Any]] | None:
    """The global spec declaring *template*, read through links in the agents directory.

    For a template :func:`kiro_crew.agent.session_agent_spec_path` does not find:
    that path resolver shares the writer's no-link fence. Each entry is read by the
    hardened reader under :func:`_managed_template_target_refused`, so a target
    outside the agents directory and inside managed memory or member private
    state reads as unusable, and a managed root that cannot be verified refuses
    every such target. Claimants with differing content raise
    :class:`~kiro_crew.agent_discovery.AmbiguousAgentSpecError`.
    """
    if not is_registered_agent_name(template):
        return None
    agents_dir = agent_mod.kiro_agents_dir_path()
    if not agents_dir.is_dir():
        return None
    claim = declared_spec_claim(
        agents_dir,
        template,
        operation=operation,
        source=source,
        fence=_managed_template_target_refused(agents_dir),
    )
    pin = agent_spec_pins.spec_pin_for(template)
    if pin is not None and (claim is None or claim.path.name != pin):
        # The fence may have set the pinned claimant aside, leaving another file
        # as the only one it admits. The session runs the pinned file, so a
        # reader that cannot read it refuses rather than read a different one.
        unfenced = declared_spec_claim(agents_dir, template, operation=operation, source=source)
        if unfenced is not None and unfenced.pinned:
            raise ValueError(
                f"the pinned spec {pin!r} for {template!r} resolves into managed state"
            )
    return None if claim is None else (claim.path, claim.spec)


def _managed_template_target_refused(agents_dir: Path) -> Callable[[Path], bool]:
    """A fence that refuses a spec target inside managed memory or member state.

    The fence judges the resolved target and the opened file, so a target inside
    *agents_dir*, resolved through ``validate_file_path``, is admitted on what is
    actually read. That covers every regular entry and a link between entries,
    including when the agents directory itself sits under a managed root, as
    ``KIRO_HOME`` does under ``KIROCREW_HOME`` in a pod. An agents directory the
    path screen refuses admits nothing this way.
    """
    admitted = validate_file_path(str(agents_dir))
    agents_root = None if admitted is None else Path(admitted)
    # Read once for this lookup. A later lookup must observe new configuration
    # and link targets through the same guarded resolver, never a cached grant.
    try:
        table: _RootTable | None = _managed_root_table(refuse_unverifiable=True)
    except _ManagedEssentialSourceError:
        table = None

    def refused(path: Path) -> bool:
        if agents_root is not None and Path(os.path.abspath(path)).is_relative_to(agents_root):
            return False
        if table is None:
            return True
        try:
            _refuse_managed_candidate(path, table)
        except _ManagedEssentialSourceError:
            return True
        return False

    return refused


def resolve_relative_prompt_path(
    source: Path, spec_path: Path, project: str | None
) -> tuple[Path, Path] | None:
    """Return a canonical relative prompt and the root that supplied its template.

    A relative ``file://`` source is anchored to the agent config file's own
    directory (``spec_path.parent``), the base the Kiro agent configuration
    reference documents ("Resolved relative to the agent configuration file's
    directory"). ``spec_path`` selects which admitted root applies: home for a
    global agent, the project root for an agent under ``<project>/.kiro/agents``.

    The admitted root (home, or that project root) is the containment boundary:
    the resolved path must land inside it, so a ``..`` source that climbs above
    the root is refused. That root is returned alongside the path so the readers
    fence their read to it (``within_root``).
    """
    try:
        root = _admitted_root(Path.home())
        if project:
            project_root = _admitted_root(Path(project))
            if project_root is None:
                raise ValueError("project root is not admitted")
            if spec_path.parent == project_agents_dir(project_root):
                root = project_root
        if root is None:
            raise ValueError("template root is not admitted")
        admitted = validate_file_path(str(spec_path.parent / source))
        if admitted is None or not Path(admitted).is_relative_to(root):
            raise ValueError("prompt is outside its template root")
        return Path(admitted), root
    except (OSError, ValueError):
        logger.debug("Skipping relative agent prompt outside its admitted root")
        return None


def _admitted_project_root(project: str | None) -> Path | None:
    """The member's project as the essential readers may open it, or ``None``."""
    if not project:
        return None
    admitted = validate_file_path(project)
    if admitted is None:
        raise MemberEssentialContextError(f"Essential project {project}: cannot be read safely")
    project_root = Path(admitted)
    _refuse_managed_source(project_root)
    return project_root


def member_inherits_default_resources(project: str | None) -> bool:
    """Whether kiro-cli hands a member in its admitted *project* the default resources.

    Global and workspace steering plus ``AGENTS.md``. Settings that cannot be
    read keep inheritance; the caller decides whether kiro-cli serves the
    session at all, since a kiro-cli setting changes nothing on another harness.
    Computed once per member turn by the caller that knows the provider and
    handed to :func:`documents_for_member` and the folder-steering dedup, so a
    document under a ``.kiro/steering`` root is delivered by exactly one of them
    whichever way the workspace decides.
    """
    return acp_driver.inherits_default_resources(_admitted_project_root(project))


@_managed_roots_scope()
def documents_for_member(
    template: str,
    project: str | None,
    *,
    include_project: bool = True,
    native_only: bool = False,
    conditional_index: bool = False,
    context_settings: bool = False,
    trigger_text: str = "",
    inherits_default_resources: bool = True,
    core_sources_out: set[str] | None = None,
    template_dir: str | None = None,
) -> list[tuple[str, str]]:
    """Read actual project instructions and the owner's declared template sources.

    Native project steering defaults to always; manual, auto and fileMatch
    documents are deliberately left to their native trigger. Generic product
    prompts keep their existing provider/session-start path.

    *inherits_default_resources* is the caller's verdict on whether the session's
    harness hands the member kiro-cli's default resources (global and workspace
    steering, ``AGENTS.md``). It defaults to inheriting because only a session
    kiro-cli serves can opt out, and only that caller knows which harness it has.

    *core_sources_out*, when given, receives the source label of every returned
    document that belongs to the member's CORE -- the persona prompt, ``SOUL.md``,
    the template context settings and any guide-not-loaded note -- so a caller
    fitting an over-budget envelope knows which documents it must never leave
    out. Every other returned document (global and project steering, the
    conditional-guide index, ``AGENTS.md`` and the declared ``file://``
    resources) is a guide the caller may drop whole.

    *template_dir*, used only when *project* is ``None``, is searched for the
    template under its ``.kiro/agents`` (and anchors that template's relative
    prompt). No guide, steering or resource is read from it.
    """
    documents: list[tuple[str, str]] = []
    seen: set[Path] = set()
    skipped: list[Path] = []
    linked: list[Path] = []
    outside: list[Path] = []
    missing: list[Path] = []
    unreadable: list[Path] = []
    oversized: list[tuple[Path, int]] = []
    over_cap: list[Path] = []
    # Source labels of steering documents in *documents*, in the order added: the
    # last one gives its slot to a member's own source when the cap is reached.
    steering_sources: list[str] = []
    project_root = _admitted_project_root(project)

    def _mark_core(source: str) -> None:
        # A core source is never evicted: a steering file reused as the persona
        # leaves the evictable list. It is matched the way ``add`` deduplicates
        # (``abspath`` and ``normcase``, as ``Path`` equality does), so any
        # spelling of the persona finds the document that carries it, and that
        # document's label is the one marked core.
        target = os.path.normcase(os.path.abspath(source))
        for label in list(steering_sources):
            if os.path.normcase(os.path.abspath(label)) == target:
                steering_sources.remove(label)
                if core_sources_out is not None:
                    core_sources_out.add(label)
        if core_sources_out is not None:
            core_sources_out.add(source)

    def _make_room(path: Path | str, *, refuse: bool) -> None:
        """Free one slot for a non-steering source once the document cap is reached.

        Steering is read first, so it can fill every slot before the persona,
        ``SOUL.md`` or a declared resource is reached. The last steering document
        added gives its slot back and is named as left out. With no steering left
        to give, a *refuse* source keeps refusing as it always has.
        """
        if len(documents) < _MAX_DOCUMENTS:
            return
        while steering_sources:
            label = steering_sources.pop()
            index = next(
                (i for i in range(len(documents) - 1, -1, -1) if documents[i][0] == label),
                None,
            )
            if index is None:
                continue
            documents.pop(index)
            given = Path(label.removesuffix("#selection"))
            # Out of the snapshot, so a later source naming it loads it again.
            seen.discard(Path(os.path.abspath(given)))
            logger.warning(
                "Steering %s left out: the %d-document cap is reached", given, _MAX_DOCUMENTS
            )
            over_cap.append(given)
            return
        if refuse:
            raise MemberEssentialContextError(f"Essential source {path}: too many documents")

    def _finish() -> list[tuple[str, str]]:
        # A native-only read lists what the host itself loads; the note is not
        # a host-native source, so it rides only in the essentials envelope.
        # Normalized the way ``add`` deduplicates (``abspath`` and ``normcase``),
        # so a source delivered under another spelling counts as delivered.
        loaded = {
            os.path.normcase(os.path.abspath(source.removesuffix("#selection")))
            for source, _ in documents
        }
        notes = (
            ()
            if native_only
            else (
                _skipped_linked_note(linked),
                _outside_declared_note(outside, template),
                _skipped_unreadable_note(unreadable, template),
                _skipped_oversized_note(oversized, template),
                _skipped_over_cap_note(
                    # A path another scan delivered is not left out.
                    [
                        path
                        for path in over_cap
                        if os.path.normcase(os.path.abspath(path)) not in loaded
                    ],
                    template,
                ),
                _skipped_managed_note(skipped, template),
                _missing_declared_note(
                    [path for path in missing if path.suffix.lower() == ".md"], template
                ),
            )
        )
        for note in notes:
            if note is not None:
                documents.append(note)
                _mark_core(note[0])
        return documents

    def add(
        path: Path,
        root: Path,
        *,
        steering: bool = False,
        body: str | None = None,
        outside_ok: bool = False,
    ) -> None:
        if Path(os.path.abspath(path)) in seen:
            return

        def _full() -> bool:
            """Whether a steering document that would take a slot finds none.

            Asked once inclusion is known, so a conditional file that takes no
            slot never pushes an ``always`` file out. Steering past the cap is
            left out and named, so a large steering folder never fails the turn.
            """
            if len(documents) < _MAX_DOCUMENTS:
                return False
            logger.warning(
                "Steering %s left out: the %d-document cap is reached", path, _MAX_DOCUMENTS
            )
            over_cap.append(path)
            return True

        if body is None:
            try:
                body = _read(path, root)
            except _OutsideRootEssentialSourceError as exc:
                if not outside_ok:
                    raise
                # Declared by absolute path, but its real path leaves home (a
                # linked ~/workplace on a cloud desktop). Left out unread and named.
                logger.warning("Declared resource %s not loaded: %s", path, exc)
                outside.append(path)
                return
            except _UnreadableEssentialSourceError as exc:
                if not steering:
                    raise
                # Contained and admitted, but the descriptor read refused it (a
                # hard link an installer placed, a non-link reparse point), it is
                # over the read cap, or it is not UTF-8. Left out and named, as a
                # linked steering entry is. The read precedes the inclusion check, so
                # this holds for every inclusion mode.
                logger.warning("Steering %s not loaded: %s", path, exc)
                if isinstance(exc, _OversizedEssentialSourceError):
                    oversized.append((path, exc.size))
                else:
                    unreadable.append(path)
                return
        if steering:
            fields, _ = split_frontmatter(body, STEERING_LOADER)
            inclusion = _steering_inclusion(fields, path)
            if inclusion in _CONDITIONAL_INCLUSIONS:
                if conditional_index:
                    import hashlib
                    import re

                    named = bool(
                        re.search(
                            r"(?<![\w-])#" + re.escape(path.stem) + r"(?![\w-])", trigger_text
                        )
                    )
                    pattern = fields.get("fileMatchPattern", "").strip()
                    file_selected = (
                        inclusion == "filematch"
                        and bool(pattern)
                        and any(
                            fnmatch.fnmatchcase(token, pattern)
                            or (
                                pattern.startswith("**/")
                                and fnmatch.fnmatchcase(token, pattern[3:])
                            )
                            for token in re.findall(r"[\w./\\-]+", trigger_text)
                        )
                    )
                    if named or file_selected:
                        if _full():
                            return
                        seen.add(Path(os.path.abspath(path)))
                        documents.append((str(path), body))
                        steering_sources.append(str(path))
                        return

                    condition = {
                        "manual": f"Only when the user explicitly requests #{path.stem} or this guide.",
                        "filematch": "Only before working on a file matching fileMatchPattern; an empty pattern never matches.",
                        "auto": "Only when the description is relevant to the current task; an empty description requires an explicit request.",
                    }[inclusion]
                    if _full():
                        return
                    documents.append(
                        (
                            f"{path}#selection",
                            "CONDITIONAL GUIDE, NOT ACTIVE INSTRUCTIONS. "
                            + condition
                            + f"\nRead {path} with the file tool when that condition holds, then apply its full current contents."
                            + f"\nDescription: {fields.get('description', '')}"
                            + f"\nfileMatchPattern: {fields.get('fileMatchPattern', '')}"
                            + f"\nContent version: {hashlib.sha256(body.encode('utf-8')).hexdigest()}",
                        )
                    )
                    steering_sources.append(f"{path}#selection")
                    seen.add(Path(os.path.abspath(path)))
                return
        if not steering:
            _make_room(path, refuse=True)
        elif _full():
            return
        seen.add(Path(os.path.abspath(path)))
        documents.append((str(path), body))
        if steering:
            steering_sources.append(str(path))

    # kiro-cli appends its default resources (global and workspace steering,
    # AGENTS.md) to a custom agent only while the workspace inherits them. A
    # member whose workspace opts out loads just what its template declares, so
    # the snapshot must not re-add the operator's global steering behind it.
    inherits = include_project and not native_only and inherits_default_resources
    if inherits:
        for path in _matches(
            Path.home(), ".kiro/steering/**/*.md", skipped, linked, over_cap=over_cap
        ):
            add(path, Path.home(), steering=True)

    if project_root is not None and include_project and not native_only:
        # SOUL.md is Crew's own member file, not a kiro-cli default resource, so
        # the opt-out leaves it in place.
        for name in ("AGENTS.md", "SOUL.md") if inherits else ("SOUL.md",):
            path = project_root / name
            if path.exists() or path.is_symlink():
                body = _read_implicit_guide(path, project_root)
                if body is None:
                    _make_room(path, refuse=False)
                    documents.append(_omitted_guide(path, project_root))
                    _mark_core(documents[-1][0])
                else:
                    add(path, project_root, body=body)
                    if name == "SOUL.md":
                        _mark_core(str(path))
        if inherits:
            for path in _matches(
                project_root, ".kiro/steering/**/*.md", skipped, linked, over_cap=over_cap
            ):
                add(path, project_root, steering=True)

    template_root = project or template_dir
    template_spec = read_template_spec(
        template, template_root, operation="member_essentials", source="context"
    )
    if template_spec is None:
        if template != "kirocrew":
            raise MemberEssentialContextError(f"Essential template {template!r}: not found")
        return _finish()
    spec_path, spec = template_spec
    if spec is None:
        raise MemberEssentialContextError(f"Essential template {spec_path}: cannot be read safely")
    # Native file resources are relative to the project cwd or user home.
    absolute_root = (
        project_root
        if project_root is not None and spec_path.is_relative_to(project_root)
        else Path.home()
    )
    source_root = project_root or Path.home()
    prompt = spec.get("prompt", "")
    if not isinstance(prompt, str):
        raise MemberEssentialContextError(f"Essential template {spec_path}: prompt must be text")
    # A fork inherits the managed contract; essentials omit it because the
    # session-start injection delivers it once, regardless of template name or
    # install directory (see is_managed_prompt).
    if prompt and not agent_mod.is_managed_prompt(prompt):
        if prompt.startswith("file://"):
            path = Path(prompt[7:]).expanduser()
            if path.is_absolute():
                # An absolute persona is also contained by the template_dir
                # that stood in for a missing project. Declared resources keep
                # absolute_root, so nothing else is read from that directory.
                template_root_path = (
                    None if project_root is not None else _admitted_project_root(template_dir)
                )
                persona_root = (
                    template_root_path
                    if template_root_path is not None
                    and spec_path.is_relative_to(template_root_path)
                    else absolute_root
                )
                add(path, persona_root)
                _mark_core(str(path))
            else:
                resolved = resolve_relative_prompt_path(path, spec_path, template_root)
                if resolved is not None:
                    add(*resolved)
                    _mark_core(str(resolved[0]))
        else:
            _make_room(spec_path, refuse=False)
            documents.append((f"{spec_path}#prompt", prompt))
            _mark_core(documents[-1][0])
    if context_settings and not native_only:
        import json

        _make_room(spec_path, refuse=False)
        documents.append(
            (
                f"{spec_path}#context-settings",
                "Template context settings (descriptive, not authorization):\n"
                + json.dumps(
                    {
                        key: spec[key]
                        for key in (
                            "name",
                            "description",
                            "model",
                            "includeCrewContext",
                            "resources",
                        )
                        if key in spec
                    },
                    ensure_ascii=False,
                    sort_keys=True,
                ),
            )
        )
        _mark_core(documents[-1][0])
    resources = spec.get("resources", [])
    if include_project and (
        not isinstance(resources, list) or any(not isinstance(r, (str, dict)) for r in resources)
    ):
        raise MemberEssentialContextError(
            f"Essential template {spec_path}: resources must be a list of declarations"
        )
    if include_project and isinstance(resources, list):
        if _declared_document_count(resources) > _MAX_DOCUMENTS:
            raise MemberEssentialContextError(f"Essential template {spec_path}: too many resources")
        absolute: set[Path] = set()
        for match, root in _resource_paths(
            resources,
            source_root,
            absolute_root,
            skipped,
            missing,
            linked=linked,
            outside=outside,
            absolute_out=absolute,
            over_cap=over_cap,
        ):
            add(
                match,
                root,
                steering="steering" in match.parts,
                outside_ok=match in absolute,
            )
    return _finish()


def _resource_pattern(path: Path, root: Path) -> str:
    """The root-relative glob for an absolute declaration, in either root spelling.

    A declaration and its root can name the SAME directory in two spellings. An
    installer records an installed resource in its realpath spelling
    (``file:///local/home/<user>/.aim/...``) while ``Path.home()`` stays the link
    (``/home/<user>``) on a host whose home is reached through one, so the lexical
    ``relative_to`` below reports a resource genuinely inside home as outside it
    and refuses every absolute essential source on that host.

    The lexical comparison is tried FIRST, so nothing already admitted changes.
    The fallback compares against the same admitted spelling :func:`_matches` and
    :func:`_read` already anchor on, which is why it widens no root: the pattern
    it returns is still expanded under that one admitted root, and both reads
    re-screen the result. It only lets a caller name the root it is already
    confined to by its other spelling.

    The DECLARATION itself is never resolved -- ``realpath`` on an unvalidated
    caller path is itself the outbound probe a UNC target wants, the same
    asymmetry :func:`_refuse_managed_source` documents.
    """
    try:
        return str(path.relative_to(root))
    except ValueError:
        pass
    admitted_root = _admitted_root(root)
    if admitted_root is None:
        raise MemberEssentialContextError(f"Essential source {path}: outside {root}")
    try:
        return str(path.relative_to(admitted_root))
    except ValueError:
        pass
    # The reverse layout: the declaration carries the link spelling while the
    # root is already resolved (a project root is stored resolved). Only the
    # declaration's glob-free ANCESTORS are screened, never its tail, so a link
    # below the root is still refused: by the walk in :func:`_matches` for a
    # glob, and by the containment check in :func:`_read` for a literal path.
    for ancestor in reversed(path.parents):
        if any(c in ancestor.name for c in "*?["):
            break
        admitted = validate_file_path(str(ancestor))
        if admitted is not None and Path(admitted) == admitted_root:
            return str(path.relative_to(ancestor))
    raise MemberEssentialContextError(f"Essential source {path}: outside {root}")


def _resource_paths(
    resources: list[ResourceDeclaration],
    source_root: Path,
    absolute_root: Path,
    skipped: list[Path] | None = None,
    missing: list[Path] | None = None,
    *,
    linked: list[Path] | None = None,
    markdown_only: bool = True,
    outside: list[Path] | None = None,
    absolute_out: set[Path] | None = None,
    over_cap: list[Path] | None = None,
) -> list[tuple[Path, Path]]:
    """Declared ``file://`` matches; an absent literal one goes to *missing* instead.

    A caller that passes *over_cap* gets every steering match and caps them
    itself once their inclusion is known (see :func:`_matches`); only the other
    matches count against ``_MAX_DOCUMENTS`` here.

    When *absolute_out* is given, every match of an ABSOLUTE declaration is also
    added to it, so a reader can tell those apart from project-relative ones.

    A caller that passes *linked* has a steering glob (a ``steering`` component in
    its pattern, the test :func:`documents_for_member` applies to a match) leave a
    link that stays inside its root out and name it there, as the implicit steering
    scan does. A relative steering glob names a link that leaves its root there too,
    unless the screen refuses its target or the target is managed state (see
    *outside* in :func:`_matches`). A link under any other relative glob still
    refuses, as it does for a caller that passes none.

    A caller that passes *outside* has an ABSOLUTE declaration leave out a link
    whose target resolves outside its root and name it there (see
    :func:`_matches`). A relative declaration never uses *outside*.
    """
    paths: list[tuple[Path, Path]] = []
    absent: list[Path] = [] if missing is None else missing
    if _declared_document_count(resources) > _MAX_DOCUMENTS:
        raise MemberEssentialContextError(
            "Essential resource declaration exceeds the document limit"
        )
    counted = 0
    for resource in resources:
        if not isinstance(resource, str) or not resource.startswith("file://"):
            continue
        path = Path(resource[7:]).expanduser()
        root = absolute_root if path.is_absolute() else source_root
        if path.is_absolute():
            pattern = _resource_pattern(path, root)
        else:
            pattern = str(path)
        steering_glob = "steering" in Path(pattern).parts
        left_out = linked if steering_glob else None
        escaping = outside if path.is_absolute() else left_out
        for match in _matches(
            root,
            pattern,
            skipped,
            left_out,
            absent,
            confined=True,
            outside=escaping,
            over_cap=over_cap if steering_glob else None,
        ):
            wanted = not markdown_only or match.suffix.lower() == ".md"
            if wanted and (match, root) not in paths:
                paths.append((match, root))
                if absolute_out is not None and path.is_absolute():
                    absolute_out.add(match)
                # With *over_cap*, steering matches are capped by the reader once
                # their inclusion is known; only the other matches count here.
                if over_cap is None or "steering" not in match.parts:
                    counted += 1
                if markdown_only and counted > _MAX_DOCUMENTS:
                    raise MemberEssentialContextError(
                        "Essential resources exceed the document limit"
                    )
    return paths


def _declared_file_size(path: Path) -> int:
    """On-disk size of one declared file, through the same path screen as a read."""
    admitted = validate_file_path(str(path))
    if admitted is None:
        raise MemberEssentialContextError(f"Essential source {path}: outside admitted path screen")
    try:
        return os.stat(admitted, follow_symlinks=False).st_size
    except OSError as exc:
        raise MemberEssentialContextError(f"Essential source {path}: {exc}") from exc


def _projected_resource_paths(
    definition: dict,
    cwd: str,
    *,
    markdown_only: bool = True,
    linked: list[Path] | None = None,
    outside: list[Path] | None = None,
    absolute_out: set[Path] | None = None,
    over_cap: list[Path] | None = None,
) -> list[tuple[Path, Path]]:
    resources = definition.get("resources", [])
    if not isinstance(resources, list) or any(not isinstance(r, (str, dict)) for r in resources):
        raise MemberEssentialContextError("Projected resources must be a list of declarations")
    return _resource_paths(
        resources,
        Path(cwd),
        Path.home(),
        linked=linked,
        markdown_only=markdown_only,
        outside=outside,
        absolute_out=absolute_out,
        over_cap=over_cap,
    )


def _declared_file_bytes(definition: dict, cwd: str) -> int:
    """On-disk size of every file the definition's ``file://`` resources match.

    kiro-cli budgets what it loads by these raw sizes, so every match counts,
    whatever its extension or ``inclusion`` mode. A declared file that does not
    exist adds nothing, since kiro-cli cannot load it either.
    """
    total = 0
    for path, _root in _projected_resource_paths(definition, cwd, markdown_only=False):
        try:
            total += _declared_file_size(path)
        except MemberEssentialContextError as exc:
            if not isinstance(exc.__cause__, FileNotFoundError):
                raise
    return total


def _declared_files_fit(definition: dict, cwd: str) -> bool:
    """Whether the declared files fit ``_LAUNCH_RECORD_MAX_BYTES`` today.

    kiro-cli re-reads them for every request, so the launch check is repeated
    on every read of the launch record. A declared file that cannot be measured
    counts as too large.
    """
    try:
        return _declared_file_bytes(definition, cwd) <= _LAUNCH_RECORD_MAX_BYTES
    except (MemberEssentialContextError, OSError, RuntimeError, RecursionError):
        return False


def projected_launch_paths(definition: dict, cwd: str) -> list[str]:
    """Paths of the markdown documents the definition's ``file://`` resources match.

    These are the guides kiro-cli loads for an agent launched with this
    definition. kiro-cli re-reads them for every request, so a folder that also
    lists one skips it by path and an edited guide reaches the model through
    kiro-cli's own re-read. No body is read here. Nothing is returned when the
    declared files total more than ``_LAUNCH_RECORD_MAX_BYTES`` on disk
    (:func:`_declared_file_bytes`), since kiro-cli may then drop any of them from
    its context.
    """
    if _declared_file_bytes(definition, cwd) > _LAUNCH_RECORD_MAX_BYTES:
        return []
    paths = (str(path) for path, _root in _projected_resource_paths(definition, cwd))
    return list(dict.fromkeys(paths))


def projected_resource_documents(definition: dict, cwd: str) -> dict[str, str]:
    """Snapshot only file resources present in the actual native wire definition.

    No implicit project scan and no template reread: project overrides cannot
    substitute their resources for the global definition KAS actually registers.
    Conditional inclusion stays with the native selector; skill URI resources
    and object-form ``knowledgeBase`` declarations keep their on-demand
    behavior and are never treated as full text. A link a steering glob reaches
    is left out unread, as in the snapshot, which names it. So is
    a steering document the no-follow descriptor read refuses (a hard link, a
    non-link reparse point).
    """
    documents: dict[str, str] = {}
    absolute: set[Path] = set()
    # Steering past the document cap is left out, as in the snapshot, which names
    # it: only always-included steering takes one of the slots the other
    # declared documents leave.
    paths = _projected_resource_paths(
        definition, cwd, linked=[], outside=[], absolute_out=absolute, over_cap=[]
    )
    room = _MAX_DOCUMENTS - sum(1 for path, _ in paths if "steering" not in path.parts)
    for path, root in paths:
        if str(path) in documents:
            continue
        try:
            body = _read(path, root)
        except _OutsideRootEssentialSourceError as exc:
            if path not in absolute:
                raise
            logger.warning("Declared resource %s not loaded: %s", path, exc)
            continue
        except _UnreadableEssentialSourceError as exc:
            if "steering" not in path.parts:
                raise
            logger.warning("Steering %s not loaded: %s", path, exc)
            continue
        if "steering" in path.parts:
            fields, _ = split_frontmatter(body, STEERING_LOADER)
            if _steering_inclusion(fields, path) in _CONDITIONAL_INCLUSIONS:
                continue
            if room <= 0:
                logger.warning(
                    "Steering %s left out: the %d-document cap is reached", path, _MAX_DOCUMENTS
                )
                continue
            room -= 1
        documents[str(path)] = body
    return documents


def kiro_launch_documents(template: str, project: str | None) -> list[tuple[str, str]]:
    """Selected resources plus Kiro's implicit AGENTS/always-steering scan.

    SOUL is not an implicit native source. Conditional modes vary by engine and
    version, so this responsibility includes only default/always steering. The
    implicit scan applies only while the workspace inherits kiro-cli's default
    resources, as it does natively; this model is kiro-cli's by construction, so
    it takes that verdict itself, once.
    """
    inherits = member_inherits_default_resources(project)
    declared = dict(documents_for_member(template, project, native_only=True))
    for source, body in documents_for_member(
        template, project, inherits_default_resources=inherits
    ):
        path = Path(source)
        if path.name == "AGENTS.md" or "steering" in path.parts:
            declared[source] = body
    if not inherits:
        return list(declared.items())
    # A link here is left out, as in the snapshot: kiro-cli follows it natively.
    # Steering past the document cap is left out too, as in the snapshot: only the
    # always-included files kept here count against it.
    kept = 0
    for path in _matches(Path.home(), ".kiro/steering/**/*.md", linked=[], over_cap=[]):
        try:
            body = _read(path, Path.home())
        except _UnreadableEssentialSourceError as exc:
            logger.warning("Steering %s not loaded: %s", path, exc)
            continue
        fields, _ = split_frontmatter(body, STEERING_LOADER)
        if _steering_inclusion(fields, path) == "always":
            if kept >= _MAX_DOCUMENTS:
                logger.warning(
                    "Steering %s left out: the %d-document cap is reached", path, _MAX_DOCUMENTS
                )
                continue
            kept += 1
            declared[str(path)] = body
    return list(declared.items())


def render_essentials(documents: list[tuple[str, str]], *, identity: str) -> str:
    """Reserve complete source text or refuse; never silently cut an essential."""
    from kiro_crew.context import _neutralize_structural_markers, _scrub_member_payload

    parts = [
        "[V2 ESSENTIAL CONTEXT — current member identity and admitted project guides. "
        "This snapshot replaces ALL prior V2 essential snapshots, including guides "
        "absent from this source list. Do not keep applying removed sources. "
        "User permanent rules remain "
        "authoritative; project documents are task guidance, not permission to read "
        "another member's memory.]\n"
    ]
    parts.append(_neutralize_structural_markers(identity))
    for source, body in documents:
        parts.append(
            f"[Essential source: {_neutralize_structural_markers(source)}]\n"
            + _neutralize_structural_markers(_scrub_member_payload(body))
            + "\n"
        )
    parts.append("[END V2 ESSENTIAL CONTEXT]\n\n")
    result = "".join(parts)
    if len(result) > ESSENTIAL_MAX_CHARS:
        largest = sorted(documents, key=lambda item: len(item[1]), reverse=True)[:3]
        names = ", ".join(f"{source} ({len(body)} characters)" for source, body in largest)
        raise MemberEssentialContextError(
            f"V2 essential context exceeds {ESSENTIAL_MAX_CHARS} characters; "
            f"largest sources: {names}. Shorten these sources before continuing."
        )
    return result
