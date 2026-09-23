"""Agent discovery — scans ~/.kiro/agents/ for installed agents.

Provides ``list_agents()`` which returns metadata about all installed
agents, including KiroCrew's own agent and any agents shipped by
locally-installed skill packages (agent config files on disk). It only
reads on-disk agent config files and has no external-tool dependency.

Each agent is identified by its ``modeId`` — the value passed to
``session/set_mode`` in the ACP protocol to switch the backend's behavior.
"""

from __future__ import annotations

import asyncio
import contextlib
import errno
import functools
import itertools
import logging
import os
import threading
import time
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any, Callable, Generic, Iterable, Iterator, Sequence, TypeVar

from kiro_crew import agent_state, hooks
from kiro_crew.agent_files import (
    AGENT_FILENAME,
    LITE_AGENT_FILENAME,
    OWNED_KIRO_AGENT_FILES,
)
from kiro_crew.agent_spec_format import (
    NATIVE_SKILL_ALIAS_PREFIX,
    is_agent_spec_name,
    is_markdown_spec,
    is_native_skill_alias_name,
    iter_agent_spec_files,
    parse_agent_spec_bytes,
    shadowed_markdown_specs,
    spec_stem,
    split_listed_spec_paths,
)
from kiro_crew.config.paths import kiro_agents_dir, project_agents_dir, project_kiro_dir
from kiro_crew.executors import discovery_executor
from kiro_crew.hooks import FileTooLargeError, is_unc_shape, unc_probe_allowed
from kiro_crew.pinned_fs import (
    PinnedPathRefusal,
    fd_real_path,
    open_fenced_for_read,
    open_in_pinned_parent,
    supports_pinned_walk,
)
from kiro_crew.platform_compat import (
    is_link_or_junction,
    iter_linked_ancestors,
)
from kiro_crew.security import is_sensitive_canonical_path, is_sensitive_path
from kiro_crew.sel import sel as _sel

logger = logging.getLogger(__name__)


_WINDOWS = os.name == "nt"


def _fence_refuses(real: Path) -> bool:
    """The sensitive-path verdict for the ``Path.resolve(strict=True)`` result.

    Delegates to :func:`security.is_sensitive_canonical_path`, which picks the
    gate by thread: the pre-resolved gate off the event loop (no ``mc-pathres``
    submission, so a saturated pool cannot drop a healthy spec) and the bounded
    gate on it. Dashboard handlers call the readers from coroutines; the native
    skill projection reads every spec under ``asyncio.to_thread``.
    """
    return is_sensitive_canonical_path(str(real))


def _unc_refused(spelling: str) -> bool:
    """The Windows UNC trusted-root gate, as ``hooks.validate_file_path`` applies it.

    A UNC path names a HOST: on Windows, resolving or opening one is an outbound
    SMB connection, an NTLM credential probe the path's author controls. The
    readers ask this BEFORE ``Path.resolve`` on the spelling they were handed
    (so a UNC-shaped spec never reaches the probe) and again on the resolved
    spelling (so a local link into a share is refused before the open). Only
    the shares ``unc_probe_allowed`` names are admitted; every other platform
    answers ``False`` here, exactly as the hooks gate does.
    """
    return _WINDOWS and is_unc_shape(spelling) and not unc_probe_allowed(spelling)


_LINK_TARGET_MAX_HOPS = 32


def _stored_link_target_path(link: str, target: str) -> str:
    """Return *target* as a lexical path relative to *link*, without resolving it."""
    drive_absolute = len(target) >= 3 and target[1] == ":" and target[2] in ("/", "\\")
    if os.path.isabs(target) or drive_absolute:
        return os.path.normpath(target)
    return os.path.normpath(os.path.join(os.path.dirname(link), target))


def _not_a_link_error(exc: OSError) -> bool:
    """Whether ``readlink`` proved a local terminal rather than failing to inspect it."""
    return exc.errno == errno.EINVAL or getattr(exc, "winerror", None) == 4390


def _link_reaches_unc(spelling: str | os.PathLike[str], _seen: set[str] | None = None) -> bool:
    r"""Whether *spelling*'s stored-target chain must be refused before a probe.

    Every hop is inspected with ``os.readlink``, which reads local reparse-point
    metadata without contacting the target. Only after a hop's stored target is
    cleared as local does the walk inspect that target as the next possible link.
    A regular local terminal is allowed; an eventual disallowed UNC target, a
    cycle, an unreadable hop, or exhaustion of the bounded walk is refused.
    No resolving or following syscall runs on an uncleared hop, so an ordinary
    single-hop link into a local dotfiles directory remains supported without
    opening a path to an attacker-controlled SMB host.

    Hop 0's ancestors are the caller's responsibility (:func:`_linked_ancestor_refused`
    screens them before this function is ever entered). Every hop after that
    walks a REWRITTEN target this function invented, whose ancestors nothing
    outside has seen, so each one is screened root-first with
    :func:`iter_linked_ancestors` -- judging every yielded ancestor with a
    recursive call to this same function -- before that hop's own
    ``os.readlink``. A local-shaped target sitting beneath an untrusted UNC
    junction is refused at the junction, never traversed to find out what is
    beneath it. ``_seen`` carries the visited-key set INTO that recursive call
    so an ancestor chain that loops back into the same walk is caught by the
    one cycle guard rather than opening a second, unbounded one; the public
    single-argument call starts a fresh set, exactly as before.
    """
    current: str | os.PathLike[str] = spelling
    seen: set[str] = _seen if _seen is not None else set()
    for hop in range(_LINK_TARGET_MAX_HOPS):
        current_str = os.fspath(current)
        key = os.path.normcase(os.path.normpath(current_str))
        if key in seen:
            return True
        seen.add(key)
        if hop > 0:
            for ancestor in iter_linked_ancestors(current):
                if _link_reaches_unc(ancestor, seen):
                    return True
        try:
            target = os.fspath(os.readlink(current))
        except OSError as exc:
            if hop > 0 and _not_a_link_error(exc):
                return False
            return True
        if is_unc_shape(target):
            return not unc_probe_allowed(target)
        current = _stored_link_target_path(current_str, target)
    return True


def _linked_ancestor_refused(spelling: str | os.PathLike[str]) -> bool:
    r"""Whether resolving a Windows-local spelling would touch an SMB share.

    A lexical UNC gate cannot see a local-looking path whose LINK target is a
    share. :func:`iter_linked_ancestors` walks EVERY linked ancestor root-first
    -- not merely the first one, which is the SAFETY property here: a benign
    LOCAL junction sitting above a malicious one must not stop the walk before
    the deeper junction is examined. Each ancestor is judged by its OWN stored
    target (:func:`_link_reaches_unc`, a local metadata read) and the walk
    steps past a link only AFTER it is cleared, refusing at the FIRST one
    whose target is UNC-shaped and untrusted -- so nothing below an uncleared
    link is ever touched. The leaf itself is refused under the same rule:
    ordinary links into local directories are followed only after every stored
    link target in their chain is cleared; a cycle, unreadable hop, hop-budget
    exhaustion, or eventual untrusted UNC target is refused before a resolving
    syscall. A dotfiles-managed checkout therefore still contributes its specs.
    Callers use this before any resolving or following syscall.
    """
    if not _WINDOWS:
        return False
    for ancestor in iter_linked_ancestors(spelling):
        if _link_reaches_unc(ancestor):
            return True
    return is_link_or_junction(spelling) and _link_reaches_unc(spelling)


class _SpecReadRefused(OSError):
    """The pinned open refused the spec's inode (link, hardlink, non-regular, fence)."""


def _read_spec_bytes(real: Path) -> bytes:
    """Read a resolved, fence-judged spec path pinned to the descriptor it opens.

    :func:`pinned_fs.open_fenced_for_read` refuses a link at the final
    component, a non-regular or hardlinked inode, and an opened inode whose
    kernel path :func:`_fence_refuses` rejects, raising :class:`_SpecReadRefused`;
    a missing file raises ``FileNotFoundError``. The same ``hooks.MAX_FILE_BYTES``
    cap as :func:`kiro_crew.hooks.safe_read_file_bytes` applies (read at call
    time, so the two readers share one cap), raising :class:`FileTooLargeError`
    past it, so a multi-gigabyte "agent config" is still refused at the cap
    instead of being slurped into memory. Nothing here submits to the resolver
    pool off the event loop.
    """
    fd = open_fenced_for_read(
        real,
        fence=lambda fd_real: _fence_refuses(Path(fd_real)),
        refusal=_SpecReadRefused,
    )
    cap = hooks.MAX_FILE_BYTES
    with os.fdopen(fd, "rb") as fh:
        data = fh.read(cap + 1)
    if len(data) > cap:
        raise FileTooLargeError(f"File exceeds {cap // (1024 * 1024)} MB safety cap")
    return data


# Resolved per call, never captured at import: an import-time binding freezes
# the data home and defeats pod isolation, the lazy legacy-home migration and
# test isolation. The name below is an opt-in override (None = live home) so
# existing monkeypatch call sites keep working. See config.md "Data Home";
# dashboard/handlers/usage.py is the reference implementation.
_KIRO_AGENTS_DIR: Path | None = None


def _kiro_agents_dir() -> Path:
    """The kiro-cli agents directory, resolved against the live data home."""
    return _KIRO_AGENTS_DIR if _KIRO_AGENTS_DIR is not None else kiro_agents_dir()


# Discovery scopes. A ``project`` agent comes from the session's own checkout and
# SHADOWS a ``global`` agent of the same name, mirroring kiro-cli: it resolves
# ``--agent`` against ``$PWD/.kiro/agents`` before ``~/.kiro/agents``. Kiro Crew
# spawns kiro-cli with the session's project dir as cwd, so the shadowing is a
# property of the backend rather than a policy choice here — surfacing the losing
# entry as separately selectable would advertise an agent that cannot be reached.
SCOPE_GLOBAL = "global"
SCOPE_PROJECT = "project"

# ── list_agents() result cache ──
# list_agents() reads and JSON-parses every ~/.kiro/agents/*.json on each call.
# Hot callers (agent picker, per-turn agent resolution) call it repeatedly, so an
# uncached scan over 100+ AIM-installed agent files blocks the asyncio event loop.
# Cache the parsed result keyed by directory and reuse it while a cheap stat-only
# directory signature (file count + newest mtime) is unchanged — that signature
# detects adds, removals, and in-place edits.
#
# The key carries BOTH scopes because the project scope varies per session: two
# sessions on different checkouts must not serve each other's agents from one
# entry. The signature is the pair of per-directory signatures, so an edit in
# either scope invalidates.
_ListAgentsSig = tuple[tuple[str, int], ...]

# Counter behind `_sensitive_dir_sig`, so each refusal gets a signature no cache
# entry can already hold. `itertools.count` is atomic under CPython, which is what
# this needs across the executor threads discovery runs on.
_sensitive_dir_seq = itertools.count()


def _sensitive_dir_sig() -> _ListAgentsSig:
    """A signature for a REFUSED scan dir that never equals any earlier one.

    Two distinct jobs, and a stable sentinel only does the first. It must differ
    from the empty-dir signature ``()``, so a cache warmed on a legitimately
    empty ``.kiro/agents`` cannot be served after that dir becomes a symlink into
    a credential home. It must ALSO differ from its own previous value: the
    refusal is what makes :func:`project_agent_files` emit the SEL denial, and a
    stable sentinel matches the cached signature on the very next lookup, so the
    cached result is served and every repeat attempt goes unaudited -- the first
    probe is recorded and an attacker's subsequent ones are silent.

    Counting per call makes the sensitive case permanently uncacheable, which is
    the intent: a refused directory must be re-checked and re-audited every time
    it is asked for. The ``\\0`` prefix keeps it outside the space of real
    filenames as well.
    """
    return (("\0sensitive", next(_sensitive_dir_seq)),)


_LIST_AGENTS_KEY = tuple[str, str]
_LIST_AGENTS_CACHE: dict[_LIST_AGENTS_KEY, tuple[tuple[_ListAgentsSig, ...], list[AgentInfo]]] = {}

# Dispatchable project agent NAMES, keyed by project dir and revalidated by the same
# stat-only signature idea. Separate from the cache above because the per-turn
# resolver needs only the name set: building full AgentInfo rows (and scanning the
# user-level dir alongside) on every turn is the cost this index exists to avoid.
_PROJECT_NAMES_CACHE: dict[str, tuple[tuple[_ListAgentsSig, ...], frozenset[str]]] = {}

# Parsed agent SPECS (raw dict + original path), keyed by directory and
# revalidated by the same stat-only signature. This is the shared snapshot
# behind :func:`agent_skill_globs` and the dashboard's ``loaded_by_agents``
# annotation: both re-read every spec per call without it, and the parse is
# their dominant cost. Guarded by a lock — callers run on executor threads.
# Rows are shared, never copied: treat ``(data, path)`` tuples and the
# ``data`` dicts as read-only.
_PARSED_SPECS_LOCK = threading.Lock()
_PARSED_SPECS_CACHE: dict[str, tuple[_ListAgentsSig, list[tuple[dict[str, Any], Path]]]] = {}
_PARSED_SPECS_REFRESHING: set[str] = set()  # Guarded by _PARSED_SPECS_LOCK.
# Bumped by clear_list_agents_cache() under the lock. A parse snapshot records
# the generation it started under and is discarded instead of stored when a
# clear landed meanwhile — otherwise an in-flight parse could re-publish rows
# read BEFORE the write that the clear announced, inside one mtime tick where
# the signature cannot tell the difference.
_PARSED_SPECS_GEN = 0


def spec_cache_generation() -> int:
    """Return the generation of in-process agent-spec content."""
    with _PARSED_SPECS_LOCK:
        return _PARSED_SPECS_GEN


@dataclass
class AgentInfo:
    """Metadata for an installed kiro-cli agent."""

    name: str
    filename: str
    description: str
    model: str
    skills: list[str] = field(default_factory=list)
    mcp_servers: list[str] = field(default_factory=list)
    source: str = "builtin"  # "kirocrew" | "package" | "builtin"
    package: str = ""  # AIM package name (e.g. "Customer360GenAIContext")
    scope: str = SCOPE_GLOBAL  # "global" | "project"
    # Display-only provenance, deliberately NOT folded into ``source``: that field
    # also gates agent auto-creation and delete-blocking, so widening it to cover
    # the helper specs would silently change both.
    kirocrew_owned: bool = False
    # Fork lineage from the agent_state sidecar: set when this template is one
    # crew's private copy of another (blueprint semantics). ``private_to`` also
    # gates the sync loop's agent auto-creation — a private copy is not a
    # standalone template deserving its own agent.
    forked_from: str = ""
    private_to: str = ""

    def __post_init__(self) -> None:
        """Make the annotations above TRUE, at every construction site.

        ``~/.kiro/agents`` is a SHARED directory: ACP adapters and IDE plugins
        drop their own specs there and do not all spell every field as a plain
        string (observed: ``"model": {"id": "anthropic:claude-opus-4-8"}``, and
        bare ``null``). ``to_dict()`` is what ``/api/agents/installed``
        serialises, so any such value reached the dashboard verbatim; rendered as
        a JSX child it threw React error #31 ("Objects are not valid as a React
        child") and put the WHOLE Agent Templates tab into the error boundary —
        every other agent's row with it.

        The enforcement lives HERE rather than in per-field calls at each caller
        because the fields are rendered bare in several places (`{a.name}`,
        `{a.package}`, `<SourceBadge source={a.source}>`, `a.filename.startsWith`)
        and there are two construction sites, one of them an out-of-tree edition
        seam. A per-field fix at one caller only *looks* complete: the next
        foreign spelling, or the next field someone renders, reopens the same
        whole-tab crash. A constructor invariant cannot be forgotten.

        Fallbacks are per field because they are not interchangeable: ``model``
        defers to ``"auto"`` (see :func:`spec_model` — the same "non-string means
        no pin" rule the execution path applies), ``source`` to its
        ``"builtin"`` default, and the free-text fields to empty.
        """
        for name, fallback in (
            ("name", ""),
            ("filename", ""),
            ("description", ""),
            ("model", _DEFER_MODEL),
            ("source", "builtin"),
            ("package", ""),
            ("scope", SCOPE_GLOBAL),
            ("forked_from", ""),
            ("private_to", ""),
        ):
            if not isinstance(getattr(self, name), str):
                setattr(self, name, fallback)
        # ``list[str]`` is equally load-bearing: these elements are rendered as
        # skill/server chips. Drop the unusable ones rather than the whole list,
        # so a spec with one bad entry keeps the rest.
        self.skills = [s for s in self.skills if isinstance(s, str)]
        self.mcp_servers = [s for s in self.mcp_servers if isinstance(s, str)]
        # The out-of-tree edition seam passes this through from a raw row, and a
        # truthy non-bool would render as a provenance claim nothing verified.
        self.kirocrew_owned = self.kirocrew_owned is True

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


SKILL_URI_PREFIX = "skill://"

# Kiro Crew-only spec convention, predating ``.kiro/agents/`` and still used by
# projects driven from Slack. kiro-cli does NOT read this location, so a name
# declared only here is NOT dispatchable: offering it as an agent would hand
# kiro-cli a mode it cannot activate. It is therefore excluded from discovery by
# default and included only for Slack's own listing/resolution, which predates
# this scope — see :func:`project_agent_files`.
AGENT_SPEC_SUFFIX = ".agent-spec.json"


def _audit_denied(*, operation: str, source: str, resources: str, error: str) -> None:
    """Emit a denial audit row for a refused path, never raising.

    EVERY denial path in this module promises not to raise -- ``_read_agent_spec``
    by the contract :func:`_warn_on_systematic_scan_failure` documents and its
    callers read bare, :func:`project_agent_names` in its own docstring
    ("Never raises; an unreadable checkout yields an empty set"), and
    :func:`project_agent_files` by the contract its own callers read it on, since
    each treats an empty list as "this checkout declares no agents". Auditing the
    denial must not become the one way to break any of those promises: for some
    surfaces this is the process's FIRST SEL use, and constructing the singleton
    mkdirs its home (``sel.py``), so an unwritable or hostile SEL directory
    would abort whichever surface asked -- on exactly the hostile path the
    refusal exists to handle.

    The REFUSAL always stands, so nothing unaudited is ever read or scanned;
    what is lost is the audit ROW. That is best-effort by the SEL API's own
    design: ``log_api_access`` reserves fail-closed behaviour for its explicit
    ``critical=True`` callers (``apps/admission.py``, the auto-improvement
    server) and no denial site in this module is one -- the denial-side rule in
    ``docs/architecture/security-deep-dive.md`` says why a refusal must not be
    coupled to SEL health. WARNING, not debug, so an operator sees that the trail
    has a hole rather than finding out later.

    The fallback names only the ``operation`` -- a fixed internal label. The
    REFUSED PATH is deliberately not logged here: on this branch its resolved
    target is a sensitive location (that is why it was refused), so writing it at
    a default-visible level would leak the very thing the deny list protects.
    The path already appears in the neighbouring ``debug`` line for anyone
    debugging a specific file, and the SEL row -- the record designed to carry
    it, redacted and clipped -- is what is being lost.
    """
    try:
        _sel().log_api_access(
            caller="agent_discovery",
            operation=operation,
            outcome="denied",
            source=source,
            resources=resources,
            error=error,
        )
    except Exception:
        logger.warning(
            "SEL denial audit failed for a %s denial -- refusal stands, audit row lost",
            operation,
            exc_info=True,
        )


def _read_agent_spec(
    path: Path,
    *,
    operation: str = "list_agents",
    source: str = "list_agents",
) -> dict[str, Any] | None:
    """Parse an agent config file, or ``None`` when it is not usable.

    The one reader for both scopes and both forms (``<name>.json`` and the
    markdown ``<name>.md`` -- see :mod:`kiro_crew.agent_spec_format`), so every
    guard applies uniformly: AppleDouble sidecars, a symlink whose RESOLVED
    target is sensitive (``evil.json`` -> ``~/.aws/credentials``), non-UTF-8
    bytes, a document that is not an object, and oversized files are all
    rejected. A markdown file with no frontmatter fence is not a spec and is
    skipped like malformed JSON. The read itself goes through
    :func:`_read_spec_bytes`: a no-reparse open pinned to the descriptor it
    reads, the same ``MAX_FILE_BYTES`` cap as every other dashboard file read,
    and no resolver-pool submission off the event loop, so a multi-gigabyte
    "agent config" is refused at the size cap instead of being slurped into
    memory during a cache warm, and a saturated pool cannot drop a healthy spec.
    The agents directories are user-writable and shared with other tools, so
    none of
    these are hypothetical.

    *operation*/*source* label the SEL denial event emitted on a sensitive
    resolved target. Precisely BECAUSE this is the one reader for every surface,
    a fixed label would record a denial served for an unrelated request as an
    agent-listing cache warm: the calling surface names itself here so
    the security trail attributes the refusal to the request that triggered it.
    ``source`` is the interface channel (``SecurityEvent.source`` vocabulary:
    dashboard, cli, slack, cron, ...; ``"unknown"`` when the caller serves
    multiple channels) — every call site passes it explicitly, enforced by the
    call-site ratchet test. Both defaults exist ONLY so a bare call reproduces
    the established event byte-for-byte (a forgotten future call site degrades
    to exactly today's trail); they are not for new call sites.
    ``caller`` stays fixed at ``"agent_discovery"``: the reader genuinely is the
    caller into SEL, and a fixed value keeps the trail greppable by module.
    """
    if path.name.startswith("._"):
        return None
    if _unc_refused(str(path)):
        # Refused BEFORE resolve: on Windows the resolve of a UNC spelling is
        # itself the outbound SMB probe.
        _audit_denied(
            operation=operation,
            source=source,
            resources=str(path),
            error="untrusted UNC path rejected",
        )
        return None
    try:
        real = path.resolve(strict=True)
    except (OSError, RuntimeError):
        # OSError: broken link / permission; RuntimeError: pathlib's signal for
        # a symlink LOOP (a self-referential agent symlink is one `ln -s` away
        # in a user-writable dir). Both mean "not a readable spec", and an
        # uncaught loop here crashes whichever surface asked — e.g. Slack's
        # `!agent` handler exits without replying.
        return None
    if _unc_refused(str(real)):
        # A local link into a share: the resolve has already probed, and the
        # read must still not load the remote document.
        _audit_denied(
            operation=operation,
            source=source,
            resources=str(real),
            error="untrusted UNC path rejected",
        )
        return None
    if _fence_refuses(real):
        logger.debug("Skipping sensitive agent config: %r", path)
        _audit_denied(
            operation=operation,
            source=source,
            resources=str(real),
            error="sensitive path rejected",
        )
        return None
    try:
        raw = _read_spec_bytes(real)
    except FileTooLargeError:
        logger.debug("Skipping oversized agent config: %r", path)
        return None
    except _SpecReadRefused as exc:
        # A link or a hardlink planted at the spec's name, a non-regular inode,
        # or an opened inode the fence rejects. The agents directories are
        # shared with other tools (a hardlink-based dotfile layout produces the
        # nlink refusal legitimately), so the reason and the path are logged
        # where an operator sees them; a DEBUG line here is what turns a
        # refused spec into an unexplained "no prepared skill discovery view".
        # Both values come from an untrusted filename (the refusal message
        # quotes the spelling), so they are rendered with ``%r``: a newline in
        # the name is escaped instead of starting a forged log record.
        logger.warning("Skipping agent config %r: %r", path, exc)
        return None
    except OSError:
        # Absent or unreadable: "not a readable spec".
        logger.debug("Skipping unreadable agent config: %r", path)
        return None
    try:
        data = parse_agent_spec_bytes(raw, path)
    except (UnicodeDecodeError, ValueError):
        logger.debug("Skipping unreadable agent config: %r", path)
        return None
    if not isinstance(data, dict):
        logger.debug("Skipping non-object agent config: %r", path)
        return None
    return data


class SensitiveAgentSpecPathError(ValueError):
    """A spec path resolved to a target the sensitive-path fence refuses.

    A ``ValueError`` like the other deterministic refusals, so a caller that
    only needs "not a spec" catches it with them; distinct so a caller that
    keeps a different answer for a refused target than for a document that does
    not parse -- the Slack name resolver treats a broken JSON spec as still
    occupying its name, a refused target as no agent at all -- can tell them
    apart without inspecting the message.
    """


def read_agent_spec_strict(path: Path, *, operation: str, source: str) -> Any:
    """Read one spec through the same hardened gate, keeping the failure class.

    :func:`_read_agent_spec` folds every refusal into ``None`` because a
    listing only needs "usable or not". Two direct-filename readers need to
    know WHY: the KAS projection reports the reason to the client that named
    the agent, and the overlay rewriter keeps an agent's previous overlay for a
    transient read failure but caches a deterministic skip. Before this reader
    both did a bare ``Path.read_text``, so a symlink dropped into the
    user-writable agents directory was followed to wherever it pointed. The
    gates here are the listing reader's -- AppleDouble sidecars, the resolved
    target checked against the sensitive-path fence through :func:`_fence_refuses`
    (and the denial audited under *operation*/*source*), the size-capped
    descriptor-pinned open of :func:`_read_spec_bytes` -- but the outcome is
    raised, not swallowed:

    * ``OSError`` -- the file could not be read: absent, unreadable, a broken
      or looping symlink (pathlib's ``RuntimeError`` is mapped to ``ELOOP``),
      or an open the hardened gate refused. Retrying may succeed.
    * ``ValueError`` -- the content is not a spec, and re-reading will not
      change that: a sensitive resolved target or a UNC spelling outside the
      trusted roots (both the :class:`SensitiveAgentSpecPathError` subclass), a
      file over the size cap, bytes that are not UTF-8 (``UnicodeDecodeError``
      is a ``ValueError``), or a document that does not parse.

    Returns the parsed document; a non-object is the caller's to reject, as
    the two forms' parsers leave it.
    """
    if path.name.startswith("._"):
        raise ValueError(f"{path} is an AppleDouble sidecar, not an agent spec")
    if _unc_refused(str(path)):
        _audit_denied(
            operation=operation,
            source=source,
            resources=str(path),
            error="untrusted UNC path rejected",
        )
        raise SensitiveAgentSpecPathError(f"{path} names a share outside the trusted roots")
    try:
        real = path.resolve(strict=True)
    except RuntimeError as exc:
        raise OSError(errno.ELOOP, "symlink loop", str(path)) from exc
    if _unc_refused(str(real)):
        _audit_denied(
            operation=operation,
            source=source,
            resources=str(real),
            error="untrusted UNC path rejected",
        )
        raise SensitiveAgentSpecPathError(f"{path} resolves to a share outside the trusted roots")
    if _fence_refuses(real):
        _audit_denied(
            operation=operation,
            source=source,
            resources=str(real),
            error="sensitive path rejected",
        )
        raise SensitiveAgentSpecPathError(f"{path} resolves to a sensitive path")
    try:
        raw = _read_spec_bytes(real)
    except FileTooLargeError as exc:
        raise ValueError(f"{path}: {exc}") from exc
    except _SpecReadRefused as exc:
        raise OSError(errno.EACCES, "agent spec could not be read", str(path)) from exc
    return parse_agent_spec_bytes(raw, path)


class AmbiguousAgentSpecError(ValueError):
    """More than one spec in one directory declares the same ``name``.

    Which of them is live is undefined, so a resolver that picked one would
    hand the caller an agent the operator did not name, with that agent's tools
    and prompt. Every declared-name resolver refuses instead; the message names
    each file so the operator can remove or rename one.

    ``paths`` carries the same files as data, for a caller that must name them
    WITHOUT their directory -- the tool-policy endpoint's 409 ``reason``
    crosses the wire into a model-visible refusal, and the message's full
    paths disclose the account name and on-disk layout there. Empty when the
    raiser did not supply them; the message is then the only record.
    """

    def __init__(self, message: str, *, paths: Sequence[Path] = ()) -> None:
        super().__init__(message)
        self.paths: tuple[Path, ...] = tuple(paths)


def spec_by_declared_name(
    agents_dir: Path,
    agent_id: str,
    *,
    operation: str,
    source: str,
) -> dict[str, Any] | None:
    """Return the parsed spec in *agents_dir* whose declared ``name`` is *agent_id*.

    A spec's filename and its declared ``name`` are allowed to differ: a package
    manager that installs several agents namespaces them on disk as
    ``<package>-<name>.json`` while the declared ``name`` stays bare, and the
    config, the CLI and :func:`kiro_crew.agent.agent_spec_path` all address such
    an agent by the bare name. Two surfaces resolve through this scan when they
    find no ``<agent_id>.json``: the KAS projection that starts a session
    (:func:`kiro_crew.acp.kas_agents.load_agent_spec`) and the tool-policy read
    that session's managed MCP servers then make, so those two agree on which
    file an id means. Other surfaces still carry their own inline
    ``data.get("name") == agent`` scans; routing them here is tracked, not done.

    Reads go through :func:`_read_agent_spec`, the one hardened reader, so a
    scan of a user-writable directory applies the same guards as the listing
    path: size cap, AppleDouble sidecars, a symlink whose resolved target is
    sensitive, non-UTF-8 bytes, JSON that is not an object. *operation* and
    *source* label its SEL denial trail exactly as that reader documents, so a
    denial is attributed to the surface that asked rather than to a listing.

    The parsed spec is returned rather than its path, and the caller uses it
    as it stands. Handing back a path to reopen would put a second read outside
    the guards: between the reader resolving a symlink and the reopen, the link
    can be repointed at a sensitive file, which would then be read with no
    denial and no SEL audit entry.

    Raises :class:`AmbiguousAgentSpecError` when two specs declare *agent_id*,
    the refusal :func:`kiro_crew.agent.agent_spec_path` makes on the same
    ambiguity. Propagates ``OSError`` from the directory walk itself; the
    per-file reads never raise.

    Only the first matching spec is held; a later match keeps its path and its
    parse is dropped at once. The reader caps each file, so the parsed-spec
    memory this scan holds at its peak is one capped parse, not one per
    same-name file; the paths themselves, one per candidate, are what the
    refusal message needs and are all the scan keeps of the rest.

    This is a fresh walk rather than a filter over :func:`parsed_agent_specs`,
    and the difference is the labels, not the loop. That cache serves catalog
    readers of one directory, so its SEL labels are first-reader-wins for the
    life of a snapshot; a denial met here would then be attributed to whichever
    listing warmed the cache instead of to the projection or the policy read
    that asked, which is the attribution the call-site ratchet exists to keep.
    The cache also hands out its own rows to be treated as read-only, where
    this returns a parse the caller owns.
    """
    match: dict[str, Any] | None = None
    match_paths: list[Path] = []
    for path in iter_agent_spec_files(agents_dir):
        spec = _read_agent_spec(path, operation=operation, source=source)
        if isinstance(spec, dict) and spec.get("name") == agent_id:
            if match is None:
                match = spec
            match_paths.append(path)
    if len(match_paths) > 1:
        # Paths are repr'd: a filename in this user-writable, tool-shared
        # directory is untrusted input, and this message reaches a terminal.
        raise AmbiguousAgentSpecError(
            f"{len(match_paths)} specs declare the name {agent_id!r}: "
            f"{', '.join(repr(str(path)) for path in match_paths)}. Which one is live is "
            f"undefined -- remove or rename one.",
            paths=match_paths,
        )
    return match


def agent_spec_stems(agents_dir: Path, *, operation: str, source: str) -> list[str]:
    """Filename stems of the specs in *agents_dir*, sorted by filename, deduplicated.

    The cheap listing the Slack surfaces show: every ``*.json`` stem as before,
    unparseable ones included (a broken JSON spec still occupies its name), plus
    the stem of each ``*.md`` that PARSES as a spec. A markdown file is a spec
    only when it opens with a frontmatter fence, so a ``README.md`` dropped into
    the directory is not listed as an agent; deciding that takes a read, which
    goes through :func:`_read_agent_spec` under its guards. Propagates
    ``OSError`` from the directory walk like the glob it replaces.
    """
    stems: dict[str, None] = {}
    for path in iter_agent_spec_files(agents_dir):
        if is_markdown_spec(path) and (
            _read_agent_spec(path, operation=operation, source=source) is None
        ):
            continue
        stems.setdefault(path.stem)
    return list(stems)


def _warn_on_systematic_scan_failure(directory: Path, candidates: int, parsed: int) -> None:
    """Emit ONE warning when a scan rejected every candidate spec it saw.

    :func:`_read_agent_spec` deliberately degrades per file to ``None`` at debug
    level — callers depend on that contract. The cost is that a SYSTEMATIC
    failure (every spec in a scope unreadable for the same reason, e.g. the
    trusted-root gate refusing an entire home layout) is indistinguishable at
    default log levels from an empty agents directory: discovery lists nothing,
    model resolution silently falls back, and nothing says why. This scan-level
    check makes that case visible without touching the per-file contract: one
    warning per scan invocation (the rate limit), none when the directory is
    empty (N=0 is not failure) or when at least one spec parsed.
    """
    if candidates > 0 and parsed == 0:
        logger.warning(
            "agent discovery: read %d candidate spec file(s) under %s but parsed 0 — "
            "all were unreadable or rejected; enable debug logging for per-file reasons",
            candidates,
            directory,
        )


class ScanUnverifiable(Exception):
    """Raised when a scan scope could not be pinned and enumerated safely.

    Distinct from the ``None`` sentinel :func:`_pinned_scan_dir_fd` yields for a
    resolved-sensitive target: that is a POSITIVE verdict (this directory is
    protected, and the refusal is auditable). This exception means the verdict
    itself could not be reached at all -- the platform cannot pin
    (:func:`kiro_crew.pinned_fs.supports_pinned_walk` is False) or the pinned open
    failed for a reason other than absence. The distinction matters to a caller
    surfacing state to a human: an unverifiable scan must read as "could not be
    checked", never silently as "no agents here" -- collapsing the two is the
    defect several review rounds were about.
    """


_AGENT_DIRECTORY_MAX_ENTRIES = 4096
# A project spec's declared name feeds kiro-cli dispatch as an agent name, so
# the retained length must match kiro_crew.validation._AGENT_NAME_RE, the
# grammar a dispatchable agent name is checked against: one leading character,
# up to 62 continuation characters, one trailing character (or a single bare
# character), for a maximum of 64.
_AGENT_NAME_MAX_CHARS = 64


def _oversized_name_sig(count: int) -> _ListAgentsSig:
    """Mark a name-capped roster so it cannot match a complete scan signature."""
    return (("\0oversized-name", count),)


def _bounded_scan_entries(
    directory: Path | str | int,
    entries: Iterable[os.DirEntry[str]],
    *,
    refuse_partial: bool,
) -> tuple[list[os.DirEntry[str]], bool]:
    """Retain at most one agent directory's shared entry cap.

    One-item lookahead distinguishes a directory exactly at the cap from one
    whose roster would be partial. Strict request scans refuse that answer;
    pre-existing session scans consume the bounded prefix and report that
    imposed narrowing instead of refusing the whole roster.
    """
    sampled = list(itertools.islice(entries, _AGENT_DIRECTORY_MAX_ENTRIES + 1))
    overflow = len(sampled) > _AGENT_DIRECTORY_MAX_ENTRIES
    if overflow:
        logger.warning(
            (
                "agent directory scan for %s exceeds the %d-entry cap; refusing partial roster"
                if refuse_partial
                else "agent directory scan for %s exceeds the %d-entry cap; using bounded roster"
            ),
            directory,
            _AGENT_DIRECTORY_MAX_ENTRIES,
        )
    return sampled[:_AGENT_DIRECTORY_MAX_ENTRIES], overflow


def _raise_scan_overflow(directory: Path) -> None:
    raise ScanUnverifiable(
        f"agent directory {directory!s} exceeds the "
        f"{_AGENT_DIRECTORY_MAX_ENTRIES}-entry scan cap"
    )


@contextlib.contextmanager
def _pinned_scan_dir(
    d: Path, *, unsupported_ok: bool = False
) -> Iterator[Iterable[os.DirEntry[str]] | None]:
    """:func:`_pinned_scan_dir_fd` for a caller that needs only the entries.

    Most callers just enumerate. The descriptor exists for the one that must put
    a further question to the same directory, so it is not in this signature --
    a caller cannot hold a descriptor it never asked for past the ``with`` block.
    """
    with _pinned_scan_dir_fd(d, unsupported_ok=unsupported_ok) as (
        entries,
        _dir_fd,
        overflow,
    ):
        if overflow and not unsupported_ok:
            _raise_scan_overflow(d)
        yield entries


@contextlib.contextmanager
def _pinned_scan_dir_fd(
    d: Path, *, unsupported_ok: bool = False
) -> Iterator[tuple[Iterable[os.DirEntry[str]] | None, int | None, bool]]:
    """Yield *d*'s entries, descriptor, and overflow signal, or ``None`` entries.

    ``project_agent_files`` sensitivity-checks the project ROOT, but the
    directories it actually enumerates are the ``<project>/.kiro`` and
    ``<project>/.kiro/agents`` SUBDIRS. A checkout whose ``.kiro`` or
    ``.kiro/agents`` is a symlink into a credential home has a non-sensitive
    root yet a sensitive scan target, so the root check alone lets
    ``scandir``+``stat`` touch the protected directory (per-file reads are
    still blocked by :func:`_read_agent_spec`, but the probe itself should not
    happen). ``None`` means refuse-and-audit; an empty iterator means there is
    simply nothing to scan, which is the ordinary case for a checkout with no
    ``.kiro`` yet. :class:`ScanUnverifiable` means neither -- the scan could not
    be run at all, on this platform or for this target, and must not read as
    either verdict.

    Built on :mod:`kiro_crew.pinned_fs`, the module this repo already uses for
    every other descriptor-pinned filesystem access
    (:func:`kiro_crew.pinned_fs.open_fenced_for_read`, used a few lines below in
    this same file, is its read-a-file counterpart). Its standing rule --
    refuse on a platform that cannot pin rather than silently falling back to a
    by-name walk -- is exactly the rule the NEW request-supplied
    ``?project_path=`` scan needs, so it is asked for rather than
    re-implemented:

    * :func:`kiro_crew.pinned_fs.supports_pinned_walk` gates the scan, but ONLY
      when *unsupported_ok* is ``False``. False capability with *unsupported_ok*
      also ``False`` REFUSES outright (:class:`ScanUnverifiable`) rather than
      falling back to a by-name walk an ancestor swap could redirect -- the same
      fail-closed contract :mod:`kiro_crew.pinned_fs` states for
      :func:`kiro_crew.pinned_fs.remove_tree_pinned`. This is the ``?project_path=``
      caller's contract: an HTTP query names a directory the caller does not
      already trust, so the platform that cannot pin it must say so rather than
      silently walking it by name.

      *unsupported_ok* is the opt-out for every PRE-EXISTING caller (per-turn
      resolution, ``spawn_run`` validation, Slack, the config loader): none of
      those reads a query-supplied path, all of them read the session's
      already-established project directory, and `upstream/main` has always
      scanned that directory by plain ``Path.is_dir()`` + ``glob``/
      ``iter_agent_spec_files`` on every platform with no capability gate at
      all. Gating them here would be a Windows agent-discovery regression this
      PR does not otherwise touch -- Slack, spawn validation and per-turn
      dispatch would each silently lose Windows project agents, riding along
      inside a folder-picker change with no review of its own. So with
      *unsupported_ok* set, an unpinnable platform degrades to that exact
      by-name walk instead of raising, and the residual is real and stated
      rather than reasoned away: that walk re-resolves ``.kiro``/
      ``.kiro/agents`` BY NAME, so an ancestor swapped for a link between the
      ``is_dir()`` check and the read is followed, exactly as it always has been
      on `upstream/main`. Closing that gap on Windows is future work with its
      own PR and its own review, not a side effect of this one.
    * On a platform that DOES support pinning, both callers get the SAME pinned
      walk regardless of *unsupported_ok* -- the flag only chooses what happens
      when pinning is unavailable, never a weaker path when it is.
    * The parent chain is resolved ONCE (:func:`os.path.realpath`) and pinned
      component by component with :func:`kiro_crew.pinned_fs.open_in_pinned_parent`,
      exactly as :func:`kiro_crew.pinned_fs.open_dir_pinned` pins a directory's
      ancestors -- so an ancestor swapped for a link after the resolve is
      refused rather than traversed. The FINAL component is opened WITHOUT
      ``O_NOFOLLOW`` (plain ``O_RDONLY | O_DIRECTORY``): a link at the leaf
      itself -- ``.kiro`` or ``.kiro/agents`` symlinked into an ordinary
      directory, the dotfiles-managed-checkout case -- is followed rather than
      refused for being a link, because the descriptor is then judged by its
      RESOLVED target, not by whether it is a link. Refusing every leaf link
      outright would deny that ordinary checkout as if it were an attack.
    * The opened descriptor's real path (:func:`kiro_crew.pinned_fs.fd_real_path`,
      the kernel's own answer for the inode already held, never a name that
      could have been swapped since) is what :func:`security.is_sensitive_path`
      judges. A target the kernel cannot report a real path for fails closed as
      sensitive-denied, because there is nothing else honest to judge.
    * Enumeration then runs ``os.scandir(fd)`` on the SAME descriptor --
      descriptor-relative, so it reads the inode that was judged even if the
      NAME is swapped afterwards. A name-based ``glob`` would re-resolve and
      follow the swap.

    The caller MUST consume the entries inside the ``with`` block; nothing after
    it exits is protected.
    """
    if not supports_pinned_walk():
        if unsupported_ok:
            # The pre-existing, unpinned behaviour every EXISTING caller has
            # always had, byte for byte: `upstream/main` never gated this scan
            # on any capability, so this degrades to the same `is_dir()` +
            # `os.scandir` by-name walk rather than raising. Before that walk,
            # reject a Windows-local spelling with a linked ancestor: resolving
            # or statting it could traverse a junction into an attacker SMB
            # share, which a lexical UNC check cannot see.
            if _linked_ancestor_refused(d):
                yield None, None, False
                return
            # Sensitivity is judged against the RESOLVED name here, because
            # there is no held descriptor to judge instead -- after the linked-
            # ancestor screen, the remaining residual is the same check-to-use
            # window `upstream/main` has always had, not a new one.
            try:
                if not d.is_dir():
                    yield (), None, False
                    return
                resolved = os.path.realpath(d)
            except OSError:
                yield (), None, False
                return
            if is_sensitive_path(resolved):
                yield None, None, False
                return
            try:
                with os.scandir(d) as scan:
                    entries, overflow = _bounded_scan_entries(
                        d, scan, refuse_partial=not unsupported_ok
                    )
            except OSError:
                yield (), None, False
                return
            yield entries, None, overflow
            return
        raise ScanUnverifiable(
            "cannot pin a directory descriptor on this platform, so the scan "
            "would have to re-open every component by name and could be "
            "redirected by an ancestor swapped mid-walk"
        )
    d_str = os.fspath(d)
    as_given = Path(d_str)
    try:
        resolved_parent = os.path.realpath(as_given.parent or Path("."))
    except (OSError, ValueError) as exc:
        raise ScanUnverifiable(f"could not resolve the parent of {d_str!r}: {exc}") from exc
    # Judge the resolved parent before any metadata probe or pinned walk touches
    # it. A symlinked ``.kiro`` may resolve directly into a credential directory;
    # that is a positive sensitive-path verdict, not ordinary absence.
    if is_sensitive_path(resolved_parent):
        yield None, None, False
        return
    # A non-directory ANCESTOR (e.g. a plain file sitting where `.kiro` should
    # be a directory) is an ordinary malformed checkout, not a security event:
    # there is nothing behind a regular file to protect, and `pin_parent`
    # cannot tell "ancestor is a non-directory" apart from "ancestor is a
    # symlink" -- both surface as ENOTDIR/ELOOP and translate to the SAME
    # refusal. Checked here, by name, before any pin: a swap landing in the
    # gap between this check and the pin below still gets caught BY the pin
    # (an ancestor that becomes a link after this check fails `O_NOFOLLOW`
    # there), so this pre-check only narrows the "absent" case and cannot
    # widen what the pin still refuses.
    if not os.path.isdir(resolved_parent):
        yield (), None, False
        return
    try:
        fd = open_in_pinned_parent(
            resolved_parent,
            as_given.name,
            # No O_NOFOLLOW: the leaf is FOLLOWED if it is a link, so a link to an
            # ordinary directory is scanned rather than refused for being a link
            # -- see the docstring. Every ancestor above it is still pinned by
            # `open_in_pinned_parent` -> `pin_parent`, refusing a swapped one.
            flags=os.O_RDONLY | getattr(os, "O_DIRECTORY", 0),
            mode=0,
            what="agent scan directory",
            refusal=PinnedPathRefusal,
        )
    except FileNotFoundError:
        # Absent: the ordinary "nothing here" for a checkout with no `.kiro` yet.
        yield (), None, False
        return
    except NotADirectoryError:
        # A regular file occupies the scan scope -- a malformed checkout, not a
        # security event: there is nothing here to enumerate.
        yield (), None, False
        return
    except PinnedPathRefusal as exc:
        # An ancestor became a link after the resolve above (the check-to-use
        # swap `pin_parent` exists to catch). The target was never shown to be
        # sensitive, so this is unverifiable rather than an audited denial.
        raise ScanUnverifiable(f"could not pin {d_str!r} for scanning: {exc}") from exc
    except OSError as exc:
        raise ScanUnverifiable(f"could not open {d_str!r} for scanning: {exc}") from exc
    try:
        real = fd_real_path(fd)
        if real is None:
            # The kernel could not report the opened inode's own path. Nothing
            # honest is left to judge, so this fails closed as sensitive-denied
            # rather than scanning a target that was never verified.
            yield None, None, False
            return
        if is_sensitive_path(real):
            yield None, None, False
            return
        try:
            # Materialized BEFORE the yield, deliberately. A `@contextmanager`
            # generator may yield exactly once, and a lazy iterator lets an
            # `OSError` surface DURING the caller's iteration -- that exception
            # is thrown back in at the yield, and a second yield from the
            # handler raises `RuntimeError: generator didn't stop after
            # throw()`, which aborts the caller's whole command rather than
            # degrading to "no agents". Reading the bounded entries here also
            # guarantees the enumeration happens against the same fd that was
            # judged.
            with os.scandir(fd) as scan:
                entries, overflow = _bounded_scan_entries(
                    d, scan, refuse_partial=not unsupported_ok
                )
        except OSError:
            yield (), None, False
            return
        # The descriptor travels with the entries so a caller that must ask this
        # directory one more question -- does `<stem>.json` exist beside this
        # `<stem>.md` -- asks it relative to the inode already validated, rather
        # than by a name a swap could redirect. NOT closed here: the caller owns
        # it for the life of the `with` block and closes it via the `finally`
        # below.
        yield entries, fd, overflow
    finally:
        os.close(fd)


def project_agent_files(
    project_dir: str | Path | None,
    include_legacy: bool = False,
    *,
    operation: str = "project_agent_files",
    source: str = "project_agent_files",
    raise_unverifiable: bool = False,
) -> list[Path]:
    """Agent config files declared by a project checkout, sorted by stem.

    Returns the kiro-cli-native ``<project>/.kiro/agents/*.json`` and the markdown
    ``*.md`` form beside it — the only project location the backends resolve
    ``--agent`` against, and therefore the only one whose names are dispatchable.
    Dispatchable by the kiro-cli backend, which reads the checkout itself: the
    KAS projection (:func:`kiro_crew.acp.kas_agents.load_agent_spec`) reads the
    user-level directory only, for either form, so a project-only agent selected
    on a KAS session is refused at session start there. Whether a checkout's
    spec may be projected at all is a governance question (a checkout can
    shadow a managed agent), not a question of which form is scanned, and the
    two forms are treated alike here.

    *include_legacy* additionally returns ``<project>/.kiro/*.agent-spec.json``, Kiro
    Crew's own older convention. It defaults to ``False`` because every dispatch
    surface (the agent picker, ``spawn_run`` validation, per-turn resolution) must
    offer only agents the backend can actually activate; a legacy-only name would be
    accepted here and then fail at ``session/set_mode``. Slack passes ``True`` to
    keep its pre-existing listing and resolution behavior.

    Returns ``[]`` for a falsy or sensitive *project_dir*, and by default never
    raises: an unreadable checkout yields no agents rather than failing the
    caller's turn.

    *raise_unverifiable* switches BOTH what an unpinnable platform does and
    whether the result can raise, together, because the two describe one
    surface: caller-supplied vs. session-established.

    * ``False`` (every EXISTING caller -- per-turn resolution, ``spawn_run``
      validation, Slack, the config loader): none of them names a caller-supplied
      path, all of them read the session's already-established project
      directory, and this is the scan `upstream/main` has always run on every
      platform with no capability gate at all. So on a platform that cannot pin
      a directory descriptor (:func:`kiro_crew.pinned_fs.supports_pinned_walk`
      is False), the scan degrades to that SAME by-name ``is_dir()`` + ``glob``/
      ``iter_agent_spec_files`` walk rather than refusing -- Windows agent
      discovery keeps working exactly as it does today, with the SAME residual
      it has always had (a name-based check-to-use window on an unpinnable
      platform). Widening the fail-closed gate to this scope would be a
      Windows agent-discovery regression this PR does not otherwise touch.
    * ``True`` (the dashboard roster endpoint's explicit ``?project_path=``
      scan, the new surface this PR adds): an HTTP query names a directory the
      caller does not already trust, so an unpinnable platform REFUSES
      (:class:`ScanUnverifiable` propagates) rather than walking it by name --
      the endpoint answers 503 rather than an empty roster, so the picker does
      not read a scan it could not run as a project with none.

    The sensitive-path check is on the project root because that value arrives from
    a caller-supplied session field; the ``.kiro``/``.kiro/agents`` subdirs are
    additionally resolved and sensitivity-checked (:func:`_pinned_scan_dir`)
    so a symlinked scope is not even enumerated, and the per-file resolved-target
    check that catches a planted spec symlink stays with the reader
    (:func:`_read_agent_spec`).

    *operation*/*source* label the SEL denial event emitted on a sensitive
    project directory, exactly as on :func:`_read_agent_spec` and
    :func:`project_agent_names`: the calling surface names itself so the security
    trail attributes the refusal to the request that triggered it. ``source`` is
    the interface channel (``SecurityEvent.source`` vocabulary: dashboard, cli,
    slack, cron, ...; ``"unknown"`` when the caller serves multiple channels) --
    every call site passes it explicitly, enforced by the call-site ratchet test.
    Both defaults exist ONLY so a bare call still records the refusal under a
    label that names this function; they are not for new call sites.
    """
    if not project_dir:
        return []
    if is_sensitive_path(str(project_dir)):
        logger.debug("Skipping sensitive project dir for agent discovery: %s", project_dir)
        # Audited like every other deny in this module: the path arrives from a
        # caller-supplied session, spawn or channel field, so a scan of a
        # protected tree is a probe an operator must be able to see. Best-effort
        # by :func:`_audit_denied` -- the refusal below already stands, so a lost
        # row costs the trail, never the guard (see the denial-audit rule in
        # ``docs/architecture/security-deep-dive.md``).
        _audit_denied(
            operation=operation,
            source=source,
            resources=str(project_dir),
            error="sensitive project dir rejected",
        )
        return []
    unsupported_ok = not raise_unverifiable
    specs: list[Path] = []
    try:
        if include_legacy:
            kiro_dir = project_kiro_dir(project_dir)
            with _pinned_scan_dir(kiro_dir, unsupported_ok=unsupported_ok) as entries:
                if entries is None:
                    logger.debug("Skipping sensitive .kiro scan dir: %s", kiro_dir)
                    _audit_denied(
                        operation=operation,
                        source=source,
                        resources=str(kiro_dir),
                        error="sensitive scan dir rejected",
                    )
                else:
                    # Consumed inside the `with` block and, on POSIX,
                    # descriptor-relative: a swap of the NAME after the check
                    # cannot redirect what is read (see _pinned_scan_dir).
                    specs.extend(
                        kiro_dir / e.name for e in entries if e.name.endswith(AGENT_SPEC_SUFFIX)
                    )
        agents_dir = project_agents_dir(project_dir)
        with _pinned_scan_dir_fd(agents_dir, unsupported_ok=unsupported_ok) as (
            entries,
            dir_fd,
            overflow,
        ):
            if overflow and not unsupported_ok:
                _raise_scan_overflow(agents_dir)
            if entries is None:
                logger.debug("Skipping sensitive .kiro/agents scan dir: %s", agents_dir)
                _audit_denied(
                    operation=operation,
                    source=source,
                    resources=str(agents_dir),
                    error="sensitive scan dir rejected",
                )
            else:
                # Both spec forms, with a ``<stem>.md`` beside its ``<stem>.json``
                # twin dropped — the same rule the user-level scan applies through
                # ``iter_agent_spec_files``, reached here by the listed-paths entry
                # point so the pinned entries are used as-is. Re-globbing
                # ``agents_dir`` by name to get that rule would reopen the
                # check-to-use window the pin closes, and so would probing one
                # twin's existence by name — hence ``dir_fd``, which puts that
                # probe through the very descriptor the entries were read from.
                # ``dir_fd`` is ``None`` on the unpinned fallback branch, which
                # is fine: that branch is only taken when ``unsupported_ok`` is
                # True, and ``split_listed_spec_paths`` accepts ``dir_fd=None``
                # for exactly the by-name-fallback case (see its own doc).
                live, _shadowed = split_listed_spec_paths(
                    agents_dir, (agents_dir / e.name for e in entries), dir_fd=dir_fd
                )
                # The twin rule is all ``split_listed_spec_paths`` carries;
                # ``iter_agent_spec_files`` drops the projection's own
                # ``kirocrew-skill-view-*`` views on top of it, and this scan
                # needs that half too. Those stems are the MACHINE namespace the
                # native-skill projection writes (``acp/skill_projection.py``),
                # so a checkout that plants one must not have it listed as an
                # agent a person can pick: the projection reads this very roster
                # to decide what to project, and feeding its own output back in
                # makes an alias of an alias on each fire. Filtered HERE rather
                # than in the shared helper because the helper's other caller,
                # ``shadowed_markdown_specs``, consumes the unfiltered
                # ``shadowed`` half, and folding a namespace rule into a
                # twin-splitting primitive would make entries vanish from a
                # function whose name says it only partitions them.
                # NOT a security boundary and not load-bearing for one:
                # ``_require_unshadowed_templates`` walks the same directory
                # unfiltered, by design, because the native CLI can still load
                # these files by declared name -- so a spec hiding a protected
                # template behind an alias stem is still refused there.
                specs.extend(p for p in live if not p.stem.startswith(NATIVE_SKILL_ALIAS_PREFIX))
    except ScanUnverifiable:
        if raise_unverifiable:
            raise
        logger.debug("Agent scan for %s could not be verified; treating as no agents", project_dir)
        return []
    except OSError:
        return []
    return sorted(specs, key=lambda f: f.stem)


def _project_agent_fallback_name(spec: Path) -> str:
    """The filename-derived name for *spec*, with the spec suffixes stripped."""
    fallback = spec.name
    if fallback.endswith(AGENT_SPEC_SUFFIX):
        return fallback[: -len(AGENT_SPEC_SUFFIX)]
    return spec_stem(fallback)


def _declared_project_agent_name(spec: Path) -> str | None:
    """The dispatchable name *spec* declares, or ``None`` when it does not parse.

    ``None`` (malformed JSON, unreadable file, sensitive symlink target) means the
    file cannot become a kiro-cli mode, so its name must not enter any dispatch
    allowlist: offering the filename of a broken spec has the session accept the
    agent and then fail at ``session/set_mode``.
    """
    data = _read_agent_spec(spec, operation="resolve_project_agent_name", source="unknown")
    if data is None:
        return None
    return spec_str(data, "name", _project_agent_fallback_name(spec))


def project_agent_name(spec: Path) -> str:
    """The dispatchable name a project agent file declares.

    The declared ``name`` wins over the filename, matching what kiro-cli lists and
    accepts for ``--agent``. The stem is the fallback, with
    :data:`AGENT_SPEC_SUFFIX` stripped — a raw ``<name>.agent-spec`` stem is not a
    name anything downstream resolves.
    """
    return _declared_project_agent_name(spec) or _project_agent_fallback_name(spec)


def _project_signature(
    project_dir: str | Path, *, unsupported_ok: bool = True
) -> tuple[_ListAgentsSig, ...]:
    """Stat-only signature of a project's agent scopes.

    Covers both ``<project>/.kiro`` (legacy specs) and ``<project>/.kiro/agents``, so
    an add, removal, or in-place edit in either invalidates. Stats only — no file is
    opened — which is what makes revalidating a warm cache cheap. Each subdir whose
    RESOLVED target is sensitive contributes :func:`_sensitive_dir_sig` rather than
    a real ``_dir_signature`` — never ``scandir``+``stat``'d, matching
    :func:`project_agent_files` so a symlinked ``.kiro``/``.kiro/agents`` scope is
    never probed even for cache validation. The sentinel must differ from the
    empty-dir signature ``()`` so a transition from empty-and-cached to
    sensitive is a cache MISS, not a hit that skips the denial audit — see
    :func:`_sensitive_dir_sig`.

    *unsupported_ok* forwards to :func:`_pinned_scan_dir` and must agree with the
    same-named flag :func:`project_agent_files` uses for the scan this signature
    validates a cache entry for. With the default ``True`` (the pre-existing,
    ``raise_unverifiable=False`` caller shape), an unpinnable platform's scan
    degrades to the by-name walk exactly as :func:`_pinned_scan_dir` documents,
    so this signature never sees :class:`ScanUnverifiable` from THAT cause. But
    ``unsupported_ok=True`` does not close every raise: an ancestor swap
    (:class:`kiro_crew.pinned_fs.PinnedPathRefusal`) is still translated to
    :class:`ScanUnverifiable` by the pinned branch regardless of
    *unsupported_ok* -- that branch runs whenever pinning IS supported, which
    is the common case, and the flag only chooses the unpinnable-platform
    fallback. So this function itself must fold that raise into the SAME
    sensitive-dir sentinel :func:`project_agent_files` degrades to for its
    ``raise_unverifiable=False`` callers, or the ``unsupported_ok=True`` shape
    would raise out of here on every reachable platform, not just an
    unpinnable one -- exactly the caller-visible crash this fold exists to
    prevent. Passed ``False`` (the dashboard's ``raise_unverifiable=True``
    scan), the exception propagates instead: a caller asking for the sharper
    signal on the scan must get it on the cache-miss signature computation as
    well, or a project that could not be verified would still get a signature
    computed and cached as if it had been.

    A sensitive subdir's sentinel is unaffected by this flag either way: that
    is a real, reached verdict (this directory resolves somewhere protected),
    never a stand-in for a scan that could not run at all.
    """
    kiro_dir = project_kiro_dir(project_dir)
    agents_dir = project_agents_dir(project_dir)
    try:
        with _pinned_scan_dir_fd(kiro_dir, unsupported_ok=unsupported_ok) as (
            kiro_entries,
            kiro_dir_fd,
            kiro_overflow,
        ):
            if kiro_overflow and not unsupported_ok:
                _raise_scan_overflow(kiro_dir)
            # Computed from the entries the pinned scan yielded, inside the
            # `with` block -- the same protection the spec scan gets, rather
            # than a signature-only stat pass reopening the window by name
            # right after the check released its pin.
            kiro_sig = (
                _sensitive_dir_sig()
                if kiro_entries is None
                else _entries_signature(kiro_entries, dir_fd=kiro_dir_fd)
            )
        with _pinned_scan_dir_fd(agents_dir, unsupported_ok=unsupported_ok) as (
            agents_entries,
            agents_dir_fd,
            agents_overflow,
        ):
            if agents_overflow and not unsupported_ok:
                _raise_scan_overflow(agents_dir)
            agents_sig = (
                _sensitive_dir_sig()
                if agents_entries is None
                else _entries_signature(agents_entries, dir_fd=agents_dir_fd)
            )
    except ScanUnverifiable:
        if not unsupported_ok:
            raise
        # The pinned branch still raises on an ancestor swap regardless of
        # *unsupported_ok* -- that flag only chooses the unpinnable-platform
        # fallback, not a weaker outcome on a platform that CAN pin. A
        # degrading caller must not have that raise reach it as an uncaught
        # crash; fold it into the same sensitive-dir sentinel
        # `project_agent_files` uses for its own `raise_unverifiable=False`
        # degrade, so the signature stays a real, cacheable value rather than
        # one that could later read as a verified-empty hit.
        return (_sensitive_dir_sig(), _sensitive_dir_sig())
    return (kiro_sig, agents_sig)


def project_agent_names(
    project_dir: str | Path | None,
    *,
    operation: str = "project_agent_names",
    source: str = "project_agent_names",
    raise_unverifiable: bool = False,
) -> frozenset[str]:
    """Dispatchable agent names declared by a project, cached on a stat signature.

    Only ``<project>/.kiro/agents/`` (``*.json`` and ``*.md``) contributes, because
    only those names are ones the backend can activate (see :func:`project_agent_files`).

    Cached per project directory and revalidated by :func:`_project_signature`, so a
    repeat call on an unchanged checkout costs a pair of ``scandir`` walks rather than
    re-reading and re-parsing every spec. This matters because the per-turn agent
    resolver calls this on EVERY turn of a project-agent-bound session: bounding the
    file count is not enough on its own, since the cost that stalls a caller is the
    reads, not the count.

    *operation*/*source* label the SEL denial event emitted on a sensitive
    project directory, exactly as on :func:`_read_agent_spec`: the calling
    surface names itself so the security trail attributes the refusal to the
    request that triggered it. ``source`` is the interface
    channel (``SecurityEvent.source`` vocabulary: dashboard, cli, slack, cron,
    ...; ``"unknown"`` when the caller serves multiple channels) — every call
    site passes it explicitly, enforced by the call-site ratchet test. Both
    defaults exist ONLY so a bare call reproduces the established event
    byte-for-byte (a forgotten future call site degrades to exactly today's
    trail); they are not for new call sites.

    By default never raises: an unreadable checkout, and a scan the platform
    cannot pin and verify, both yield an empty set -- the SAME by-name scan
    `upstream/main` has always run on every platform, with no capability gate,
    for this pre-existing caller shape (per-turn resolution, ``spawn_run``
    validation, Slack, the config loader). *raise_unverifiable* switches to the
    dashboard roster endpoint's contract instead: an unpinnable platform
    REFUSES (:class:`ScanUnverifiable` propagates) rather than falling back to
    that by-name walk, because the caller supplying a value here is a request
    query rather than an established session project. This flag fences the
    cache rather than being bypassed by a warm entry: :func:`_project_signature`
    is computed with the SAME ``unsupported_ok`` and consulted as the cache
    key's validity check BEFORE the stored result is read, so under
    *raise_unverifiable* an unverifiable checkout's :class:`ScanUnverifiable`
    propagates from that signature computation ahead of the cache lookup and
    refuses a would-be hit exactly as it refuses a miss. Under the default that
    same computation folds the unverifiable case into a sensitive-dir sentinel,
    so a repeat call on an unchanged checkout revalidates to the cached answer.
    """
    if not project_dir:
        return frozenset()
    key = str(project_dir)
    # Sensitivity is decided BEFORE any filesystem access, signature stats
    # included: this path arrives from caller-supplied session/spawn fields, so
    # even a stat pair under ~/.aws etc. is probing a protected tree. Denied
    # loudly — the SEL record is what lets an operator see a spawn_run/cwd probe
    # at a protected path, matching every other deny in this module.
    if is_sensitive_path(key):
        logger.debug("Skipping sensitive project dir for agent discovery: %s", project_dir)
        _audit_denied(
            operation=operation,
            source=source,
            resources=key,
            error="sensitive project dir rejected",
        )
        return frozenset()
    signature = _project_signature(project_dir, unsupported_ok=not raise_unverifiable)
    cached = _PROJECT_NAMES_CACHE.get(key)
    if cached is not None and cached[0] == signature:
        return cached[1]
    candidates = 0
    oversized_names = 0
    declared: list[str] = []
    for f in project_agent_files(
        project_dir,
        operation=operation,
        source=source,
        raise_unverifiable=raise_unverifiable,
    ):
        # AppleDouble sidecars are rejected by design, not by failure — a
        # directory holding only sidecars is empty of specs, not broken.
        if not f.name.startswith("._"):
            candidates += 1
        # Only a spec that parses contributes: a malformed or unreadable file can
        # never become a kiro-cli mode, and admitting its filename fallback here
        # would have dispatch accept a name whose session/set_mode then fails.
        if (name := _declared_project_agent_name(f)) is not None:
            if len(name) > _AGENT_NAME_MAX_CHARS:
                oversized_names += 1
                continue
            declared.append(name)
    if oversized_names:
        # One bounded row per scan: never copy the attacker-controlled name
        # into logs, and never emit one warning for each refused spec. Mirror
        # _bounded_scan_entries: a strict request scan refuses the whole
        # answer, but a pre-existing session scan consumes the surviving
        # names and reports the imposed narrowing instead of refusing the
        # roster outright -- the same split the entry cap already draws.
        logger.warning(
            (
                "agent directory scan for %s found %d spec(s) above the "
                "%d-character agent-name cap; refusing partial roster"
                if raise_unverifiable
                else "agent directory scan for %s found %d spec(s) above the "
                "%d-character agent-name cap; using bounded roster"
            ),
            project_agents_dir(project_dir),
            oversized_names,
            _AGENT_NAME_MAX_CHARS,
        )
        if raise_unverifiable:
            raise ScanUnverifiable(
                f"agent directory {project_agents_dir(project_dir)!s} contains a name above the "
                f"{_AGENT_NAME_MAX_CHARS}-character agent-name cap"
            )
        # cached_project_agent_names() must see the narrowing after an
        # off-loop warm, but a later verified scan must not hit this as a
        # complete roster. Appending a synthetic row makes the next real
        # signature mismatch even though the cached names are non-empty.
        partial_signature = (*signature, _oversized_name_sig(oversized_names))
        names: frozenset[str] = frozenset(declared)
        _PROJECT_NAMES_CACHE[key] = (partial_signature, names)
        return names
    _warn_on_systematic_scan_failure(project_agents_dir(project_dir), candidates, len(declared))
    names = frozenset(declared)
    _PROJECT_NAMES_CACHE[key] = (signature, names)
    return names


def cached_project_agent_names(project_dir: str | Path | None) -> frozenset[str] | None:
    """Cached names for *project_dir*, or ``None`` when nothing is cached yet.

    Performs **no syscalls at all** — not even the stat pair
    :func:`project_agent_names` uses to revalidate. That is the point: this is the
    read the per-turn resolver makes while an event loop is running, where any
    filesystem access is a potential gateway stall. A caller that needs a fresh
    answer warms the cache off-loop first (see :func:`project_agent_names`); this
    then serves it from memory.

    Returning the possibly-stale cached value is deliberate. The alternative on the
    loop is not "a fresher answer", it is "no answer" — and one turn resolved
    against a snapshot taken moments earlier is strictly better than a stall or a
    wrong fallback to the default agent.
    """
    if not project_dir:
        return None
    cached = _PROJECT_NAMES_CACHE.get(str(project_dir))
    return None if cached is None else cached[1]


def clear_project_agent_cache() -> None:
    """Drop all cached :func:`project_agent_names` results.

    Invalidation is normally automatic via the stat signature; call this only to
    force an immediate refresh (tests, or right after writing a project spec).
    """
    _PROJECT_NAMES_CACHE.clear()


async def warm_project_agent_names(
    project_dir: str | Path | None,
    *,
    operation: str = "warm_project_agent_names",
    source: str = "unknown",
) -> None:
    """Populate the project name cache from the discovery pool, off the event loop.

    The counterpart to :func:`cached_project_agent_names`: an async caller runs this
    first so the synchronous, on-loop resolution that follows is a cache HIT rather
    than a fallback to the default agent.

    Only the scan is offloaded. Resolution itself stays inline on purpose — moving a
    synchronous resolver into an executor changes its exception semantics, and
    ``StopIteration`` in particular cannot be delivered through a ``Future``, which
    hangs the awaiting caller instead of surfacing the error.

    *operation*/*source* forward to :func:`project_agent_names` so a denial hit
    during the warm names the surface that requested it rather than echoing the
    helper's own name. The defaults name this hop truthfully: the warm
    itself is the operation, and the helper serves several channels (dashboard
    chat, spawn admission), so its channel is ``"unknown"`` unless the caller
    says otherwise.

    A no-op without a *project_dir*. Best-effort and never raises: failing to warm
    costs one turn's fallback, and must not break turn handling.
    """
    if not project_dir:
        return
    try:
        await asyncio.get_running_loop().run_in_executor(
            discovery_executor(),
            functools.partial(project_agent_names, project_dir, operation=operation, source=source),
        )
    except Exception:  # noqa: BLE001 — a warm failure only costs a fallback
        logger.debug("Failed to warm project agent names for %s", project_dir, exc_info=True)


# The "no pin, defer to the tier below" spelling. Mirrors
# ``config.loader.DEFAULT_MODEL``; duplicated as a literal rather than imported
# so this module keeps its leaf-level import graph (``config.paths`` only).
_DEFER_MODEL = "auto"


def spec_str(data: dict[str, Any], key: str, default: str = "") -> str:
    """Read a raw spec field as a ``str``, for callers that are NOT an AgentInfo.

    ``AgentInfo.__post_init__`` is what enforces the type contract for the
    dataclass; this is its counterpart for the two places that hand spec fields
    to the dashboard WITHOUT going through it:

    - ``api_agent_detail``, which returns ``{**data, ...}`` — the raw on-disk spec
      — so the detail panel receives whatever the file happened to contain.
    - ``list_agents``, for ``name``, where the useful fallback is the file's own
      stem rather than the generic empty string.

    ``~/.kiro/agents`` is a SHARED directory: other tools (ACP adapters, IDE
    plugins) drop their own specs there and do not all spell every field as a
    plain string. Observed in the wild: ``"model": {"id":
    "anthropic:claude-opus-4-8"}``, and a bare ``null``. Rendered as a React
    child, either throws error #31 and blanks the whole Agent Templates tab.
    """
    value = data.get(key, default)
    return value if isinstance(value, str) else default


def spec_model(data: dict[str, Any]) -> str:
    """The ``model`` of an agent spec, coerced to the ``str`` AgentInfo declares.

    A non-string is treated as "no pin" (``"auto"``), which is exactly the rule
    :func:`config.loader.normalize_agent_model` already applies to the same file
    on the EXECUTION path. Keeping both sides on that rule is deliberate: the
    resolver would collapse a structured model to "defer" regardless, so
    extracting ``id`` here would make the displayed chip disagree with the model
    actually used — and ``anthropic:claude-opus-4-8`` is a provider-prefixed id
    kiro-cli would reject anyway, turning a display bug into a spawn failure.

    It also leaked into ``subagent.py``'s spawn kwargs as a ``--model`` argument.
    """
    return spec_str(data, "model", _DEFER_MODEL)


def agent_model_map(
    agents_dir: Path | None = None,
    *,
    operation: str,
    source: str,
) -> dict[str, str]:
    """Build the full agent name/stem-to-model map for legacy history restore.

    Restore needs every entry, so this intentionally scans the complete scope.
    It keeps that surface on the hardened reader while preserving
    security-event attribution through the required *operation* and *source*
    arguments. A missing or non-string model stays ``""`` here so a restored
    legacy session continues to inherit its crew/global model; this deliberately
    differs from session's targeted runtime resolver, where no pin means
    ``"auto"``.

    A refused spec is skipped like an absent one.  If every candidate in a
    non-empty directory is refused, the scan-level warning used by discovery is
    emitted so a systematic gate failure is not mistaken for an empty install.
    """
    directory = agents_dir or _kiro_agents_dir()
    if not directory.is_dir():
        return {}
    try:
        files = iter_agent_spec_files(directory)
    except OSError:
        return {}

    candidates = 0
    parsed = 0
    result: dict[str, str] = {}
    for spec_file in files:
        if not spec_file.name.startswith("._"):
            candidates += 1
        data = _read_agent_spec(
            spec_file,
            operation=operation,
            source=source,
        )
        if data is None:
            continue
        model = spec_str(data, "model", "")
        declared_name = spec_str(data, "name")
        if declared_name:
            result[declared_name] = model
        result[spec_file.stem] = model
        parsed += 1
    _warn_on_systematic_scan_failure(directory, candidates, parsed)
    return result


def _builder_mcp_skills(data: dict[str, Any]) -> list[str]:
    """Extract skill names from builder-mcp args (--skill-name-filter)."""
    mcp = data.get("mcpServers") or {}
    if not isinstance(mcp, dict):
        return []
    bm = mcp.get("builder-mcp") or {}
    if not isinstance(bm, dict):
        return []
    args = bm.get("args") or []
    skills: list[str] = []
    for i, arg in enumerate(args):
        if arg == "--skill-name-filter" and i + 1 < len(args):
            skills.extend(s.strip() for s in args[i + 1].split(",") if s.strip())
    return skills


def skill_resource_uris(data: dict[str, Any]) -> list[str]:
    """Return the ``skill://`` entries of an agent spec's ``resources``, in order.

    These are the kiro-cli-native mapping of skills to an agent: kiro-cli loads
    every ``SKILL.md`` matched by these URIs when the agent is spawned with
    ``--agent``. Non-``skill://`` resources (``file://`` steering globs and
    friends) are user-owned and deliberately excluded.
    """
    resources = data.get("resources") or []
    if not isinstance(resources, list):
        return []
    return [r for r in resources if isinstance(r, str) and r.startswith(SKILL_URI_PREFIX)]


def _skills_from_resources(data: dict[str, Any]) -> list[str]:
    """Derive display names for the skills an agent maps via ``skill://``.

    Lexical only (no filesystem access) so :func:`list_agents` stays a
    stat+parse operation: a skill directory is named by the path segment
    directly above ``SKILL.md``, which is the skill's name in every supported
    layout. A wildcard segment (``skill://~/.kiro/skills/*/SKILL.md`` — "every
    skill in this root") has no single name, so the pattern is surfaced
    verbatim rather than silently dropped.
    """
    names: list[str] = []
    for uri in skill_resource_uris(data):
        parts = [p for p in uri[len(SKILL_URI_PREFIX) :].split("/") if p]
        if not parts:
            continue
        # Trailing SKILL.md (or any *.md) is the file, not the skill name.
        if parts[-1].lower().endswith(".md"):
            parts.pop()
        if parts:
            names.append(parts[-1])
    return names


def _extract_skills(data: dict[str, Any]) -> list[str]:
    """All skills mapped to an agent, de-duplicated, order-preserving.

    Two independent mapping mechanisms are unioned:

    1. ``skill://`` entries in ``resources`` — the kiro-cli-native mapping
       (what the dashboard's Agent Templates editor writes).
    2. ``builder-mcp --skill-name-filter`` args — an edition-specific
       convention that predates the ``resources`` support.
    """
    seen: set[str] = set()
    out: list[str] = []
    for name in (*_skills_from_resources(data), *_builder_mcp_skills(data)):
        if name in seen:
            continue
        seen.add(name)
        out.append(name)
    return out


def expand_skill_uri(
    uri: str, agent_path: Path, *, project_dir: str | Path | None = None
) -> str | None:
    """Expand a ``skill://`` resource URI into an fnmatch glob over real paths.

    kiro-cli accepts ``skill://~/.kiro/skills/*/SKILL.md`` (global),
    ``skill:///abs/path/SKILL.md`` (absolute), and
    ``skill://.kiro/skills/*/SKILL.md`` (workspace-relative to the cwd at
    session start).

    A workspace-relative URI resolves against *project_dir* when the caller
    supplies one — every path that resolves skills for a prompt passes the
    session's own project, which is the cwd kiro-cli is launched in. With no
    project supplied it falls back to the project root inferred from
    *agent_path*: for the ``<project>/.kiro/agents/foo.json`` layout that is three
    levels up (``foo.json`` -> ``agents`` -> ``.kiro`` -> ``<project>``), so
    appending the ``.kiro/``-prefixed glob yields ``<project>/.kiro/...`` without
    doubling the ``.kiro`` segment. Both are best-effort: the cwd kiro-cli
    actually uses may differ.

    Returns ``None`` for anything that is not a ``skill://`` URI.
    """
    if not uri.startswith(SKILL_URI_PREFIX):
        return None
    raw = uri[len(SKILL_URI_PREFIX) :]
    if raw.startswith("~/"):
        return str(Path.home() / raw[2:])
    if raw.startswith("/") or Path(raw).is_absolute():
        return raw
    base = Path(project_dir) if project_dir else agent_path.parent.parent.parent
    return str(base / raw)


def parsed_agent_specs(
    agents_dir: Path | None = None,
    *,
    operation: str,
    source: str,
) -> list[tuple[dict[str, Any], Path]]:
    """Return every parsed agent spec in *agents_dir* as ``(data, path)`` pairs.

    ``path`` is the ORIGINAL file path (not the resolved target), in sorted
    filename order, so ``path.stem`` matching and relative ``skill://`` glob
    anchoring behave exactly as a direct scan would. Reads go through
    :func:`_read_agent_spec`, so sidecars, symlink loops, sensitive targets,
    oversized files, and invalid JSON are skipped best-effort.

    Cached per directory and revalidated by :func:`_dir_signature`, so a warm
    call costs one ``scandir`` instead of parsing every spec. The cache is
    also dropped by :func:`clear_list_agents_cache` — the agent write paths
    already call it, which covers a write landing inside one mtime tick. The
    returned list is a fresh copy but its rows are the cached objects: treat
    them as read-only.

    *operation*/*source* label the SEL denial trail exactly as
    :func:`_read_agent_spec` documents; the cache makes the labels
    first-reader-wins for the lifetime of one snapshot, which is acceptable
    because every current caller reads the same user-level directory for the
    same catalog purpose.
    """
    d = agents_dir or _kiro_agents_dir()
    key = str(d)
    signature = _dir_signature(d)
    with _PARSED_SPECS_LOCK:
        cached = _PARSED_SPECS_CACHE.get(key)
        gen = _PARSED_SPECS_GEN
    if cached is not None and cached[0] == signature:
        return list(cached[1])
    # Parse OUTSIDE the lock: the lock guards only dict reads/writes, so an
    # event-loop caller of clear_list_agents_cache() can never block behind a
    # worker thread's disk scan. Two threads missing at once parse redundantly
    # and last-write-wins — the same rows, from the same signature-checked
    # directory state, so the duplicate work is bounded and harmless.
    try:
        candidates = iter_agent_spec_files(d)
    except OSError:
        candidates = []
    rows: list[tuple[dict[str, Any], Path]] = []
    for f in candidates:
        data = _read_agent_spec(f, operation=operation, source=source)
        if data is None:
            continue
        rows.append((data, f))
    with _PARSED_SPECS_LOCK:
        # A clear() that landed during the parse announced a write this scan
        # may predate; serve these rows to this caller but do not publish them.
        if _PARSED_SPECS_GEN == gen:
            _PARSED_SPECS_CACHE[key] = (signature, rows)
    return list(rows)


def cached_agent_specs(
    agents_dir: Path | None = None,
    *,
    operation: str,
    source: str,
) -> list[tuple[dict[str, Any], Path]]:
    """Return cached specs without filesystem calls on the caller's thread.

    The caller only reads the snapshot dict; every scandir, stat and parse runs
    on ``mc-discovery``. A cold, just-cleared or changed snapshot serves the
    previous rows (or none) until the worker refresh lands. A loop-thread model
    lookup therefore briefly degrades rather than blocking on filesystem I/O or
    queuing a full parse behind ``mc-pathres`` and triggering the watchdog exit.
    Off-loop callers needing current rows should use :func:`parsed_agent_specs`
    directly.

    Returned lists are copies; their rows remain read-only. Revalidations
    preserve the caller's *operation*/*source* audit labels, with at most one
    in-flight revalidation per directory, even across cache invalidation. A
    warm worker revalidation scans the signature without parsing specs again.
    """
    d = agents_dir or _kiro_agents_dir()
    key = str(d)
    with _PARSED_SPECS_LOCK:
        cached = _PARSED_SPECS_CACHE.get(key)
        rows = list(cached[1]) if cached is not None else []
        if key in _PARSED_SPECS_REFRESHING:
            return rows
        _PARSED_SPECS_REFRESHING.add(key)

    def refresh() -> None:
        try:
            parsed_agent_specs(d, operation=operation, source=source)
        except Exception:
            # The future is fire-and-forget, so nothing else surfaces this:
            # without the log a persistent parse failure degrades silently.
            logger.warning("agent spec snapshot refresh failed for %s", d, exc_info=True)
        finally:
            with _PARSED_SPECS_LOCK:
                _PARSED_SPECS_REFRESHING.discard(key)

    try:
        discovery_executor().submit(refresh)
    except RuntimeError:  # The executor is shutting down; leave the lookup degraded.
        with _PARSED_SPECS_LOCK:
            _PARSED_SPECS_REFRESHING.discard(key)
    return rows


class SkillScopeResolutionError(ValueError):
    """The bound agent has an unavailable skill scope."""


def agent_skill_globs(
    agent: str,
    agents_dir: Path | None = None,
    *,
    project_dir: str | Path | None = None,
    strict: bool = False,
) -> list[str]:
    """Return fnmatch globs for the skills mapped to *agent*, or ``[]``.

    An empty list means "this agent has no explicit skill mapping". Session
    discovery distinguishes the default catalog from an empty custom scope.
    Listing callers are best-effort: an unreadable, invalid, or sensitive-path
    agent file yields ``[]``. Session callers use ``strict=True`` so a missing
    custom template cannot silently widen its scope to the global catalog.
    Resolved from the :func:`parsed_agent_specs` snapshot when no project is supplied.
    """
    if not agent:
        return []
    if project_dir:
        rows = list_agents(agents_dir=agents_dir, project_dir=str(project_dir))
        winner = next((row for row in rows if row.name == agent), None)
        if winner is None or not winner.filename:
            if strict and agent != "kirocrew":
                raise SkillScopeResolutionError(f"Cannot resolve skill scope for agent {agent!r}")
            return []
        if winner.scope == SCOPE_PROJECT:
            directory = project_agents_dir(str(project_dir))
        elif agents_dir is not None:
            directory = agents_dir
        else:
            directory = _kiro_agents_dir()
        path = directory / winner.filename
        data = _read_agent_spec(path, operation="agent_skill_globs", source="unknown")
        if strict and data is None:
            raise SkillScopeResolutionError(f"Cannot read skill scope for agent {agent!r}")
        return [
            g
            for uri in skill_resource_uris(data or {})
            if (g := expand_skill_uri(uri, path, project_dir=project_dir))
        ]
    # ``f`` is the ORIGINAL path: ``f.stem`` and ``expand_skill_uri`` below
    # must see it so a symlinked spec's relative globs stay anchored where
    # the symlink lives.
    for data, f in parsed_agent_specs(agents_dir, operation="agent_skill_globs", source="unknown"):
        if data.get("name") != agent and f.stem != agent:
            continue
        return [g for uri in skill_resource_uris(data) if (g := expand_skill_uri(uri, f))]
    if strict and agent != "kirocrew":
        raise SkillScopeResolutionError(f"Cannot resolve skill scope for agent {agent!r}")
    return []


# This is called on the discovery/expansion worker, never on the event loop.
def session_skill_globs(
    session_key: str, fallback_agent: str, *, project_dir: str | Path | None = None
) -> list[str] | None:
    """Use a member's bound template, not its dashboard display alias."""
    from kiro_crew.execution_context import read_session_execution

    execution = read_session_execution(session_key) if session_key else None
    template = execution.template_id if execution and execution.template_id else fallback_agent
    mapped = agent_skill_globs(template, project_dir=project_dir, strict=True)
    return None if template == "kirocrew" and not mapped else mapped


#: Ceiling on a ``welcomeMessage`` rendered into a chat transcript. The field is
#: authored in a user-writable, tool-shared directory, so its length is not a
#: trusted quantity: an unbounded value would be persisted into the slot window
#: and re-broadcast to every open tab on each restore. Truncated rather than
#: refused — a long hint is still the author's intent, and dropping it silently
#: reproduces exactly the "accepted but invisible" behaviour this reader exists
#: to remove.
WELCOME_MESSAGE_MAX_CHARS = 2000


def spec_welcome_message(data: dict[str, Any]) -> str:
    """The display-ready ``welcomeMessage`` of a parsed agent spec, or ``""``.

    Coerced through :func:`spec_str` for the reason documented there: this key
    is read from ``~/.kiro/agents``, a directory other tools also write, so a
    structured or ``null`` value is "absent" rather than an error. Surrounding
    whitespace is stripped and a whitespace-only value collapses to ``""``, so
    a blank hint renders nothing instead of an empty bubble.

    Truncated at :data:`WELCOME_MESSAGE_MAX_CHARS` with an ellipsis, so the
    caller can append the result without re-checking its size.
    """
    text = spec_str(data, "welcomeMessage").strip()
    if len(text) > WELCOME_MESSAGE_MAX_CHARS:
        # The ellipsis is part of the budget, not an addition to it: the ceiling
        # is what callers are promised, so a result of cap+1 would break the one
        # guarantee this function makes.
        text = text[: WELCOME_MESSAGE_MAX_CHARS - 1].rstrip() + "\u2026"
    return text


def agent_welcome_message(
    agent: str,
    *,
    project: str | Path | None = None,
    agents_dir: Path | None = None,
) -> str:
    """*agent*'s ``welcomeMessage`` as display-ready text, or ``""``.

    The one reader of the field. Blocking (it scans agent directories), so an
    event-loop caller must offload it — the dashboard chat runner does.

    WHICH spec is live is answered by :func:`list_agents`, not re-decided here.
    That roster is what the agent picker shows and what the backend activates, so
    the hint has to come from the row it selected or the greeting describes an
    agent that is not running. Every rule that choice needs already lives there
    and nowhere else: project scope shadowing the user directory, a declared
    ``name`` outranking a matching filename, package-installed winning a
    duplicate name, and last-seen winning among duplicate project specs. Reading
    the winner instead of reproducing the rules is what keeps the two from
    drifting; a second copy of the precedence, however well tested, is a copy
    that can disagree.

    Only the winning file is then parsed, through the same hardened reader under
    this function's own *operation* label, so a denial is attributed to the hint
    rather than to a listing. That read applies the full guard set again (size
    cap, sidecars, sensitive symlink targets, non-object JSON), so reopening by
    name is not an unguarded second read.

    Best-effort and never raises: an unknown agent, an unreadable or oversized
    spec, or a roster row whose file is gone all yield ``""``. A missing hint and
    an unreadable one are deliberately the same answer — the field is decoration,
    and no chat turn should fail over it.
    """
    if not agent:
        return ""
    project_dir = str(project) if project else None
    try:
        rows = list_agents(agents_dir=agents_dir, project_dir=project_dir)
    except Exception:  # noqa: BLE001 - decoration must never fail a turn
        logger.debug("Agent roster unreadable for welcomeMessage %r", agent, exc_info=True)
        return ""
    winner = next((row for row in rows if row.name == agent), None)
    if winner is None or not winner.filename:
        return ""
    # The roster records a bare filename plus the scope it was found in, which is
    # what says which of the two directories to reopen it from.
    if winner.scope == SCOPE_PROJECT:
        if not project_dir:
            return ""
        directory = project_agents_dir(project_dir)
    elif agents_dir is not None:
        directory = agents_dir
    else:
        directory = _kiro_agents_dir()
    data = _read_agent_spec(
        directory / winner.filename,
        operation="agent_welcome_message",
        source="unknown",
    )
    if data is None:
        return ""
    return spec_welcome_message(data)


def _iter_spec_entries(d: Path) -> Iterator[os.DirEntry[str]]:
    """The one ``scandir`` walk behind the stat-only fingerprints of an agents dir.

    Yields every entry with a recognised spec suffix that is not a managed
    skill-view alias, and nothing else; the caller decides what to ``stat`` and
    how. Both rules are the roster's own (:func:`iter_agent_spec_files`); the one
    entry a fingerprint built here keeps and the roster drops is a Markdown spec
    shadowed by its JSON twin, so the fingerprint is a superset of the roster,
    never less.
    Case-insensitive on the suffix: a case-insensitive filesystem serves
    ``Foo.JSON`` to ``glob("*.json")`` consumers, so a case-sensitive suffix here
    would omit from a fingerprint a file the scans include -- its edits would
    never invalidate. Alias-free by name, before any ``stat``: the roster drops
    the aliases, so a fingerprint that counted them would move on writes the
    roster cannot see -- invalidating every cache it guards on each alias write
    -- and would cost one ``stat`` per alias per call on the event loop, which
    with thousands of aliases is the walk that stalls the loop. A directory that
    cannot be listed raises the ``OSError``; each caller decides what that means.
    """
    with os.scandir(d) as it:
        for entry in it:
            if is_agent_spec_name(entry.name) and not is_native_skill_alias_name(entry.name):
                yield entry


def _entries_signature(
    entries: Iterable[os.DirEntry[str]], *, dir_fd: int | None = None
) -> _ListAgentsSig:
    """Signature of an ALREADY-OPENED listing, so the caller can compute it
    without re-resolving the directory by name.

    Split out of :func:`_dir_signature` so a pinned scan can hand over the
    entries it already validated (see :func:`_pinned_scan_dir_fd`): re-opening the
    directory by path to stat it would reintroduce exactly the check-to-use
    window the pin exists to close. When *dir_fd* is available, link targets are
    read relative to that same validated directory; platforms without
    descriptor-relative ``readlink`` retain the by-name behaviour.
    """
    out: list[tuple[str, int]] = []
    try:
        for entry in entries:
            # Both spec forms, case-insensitively: a case-insensitive filesystem
            # serves ``Foo.JSON`` to ``*.json`` consumers, so a case-sensitive
            # suffix here would omit from the signature a file the scans include
            # — its edits would never invalidate. A markdown spec that went
            # unfingerprinted would likewise serve a stale roster forever, so the
            # predicate is the scans' own (:func:`is_agent_spec_name`), not a
            # second copy of it.
            if not is_agent_spec_name(entry.name):
                continue
            try:
                # follow_symlinks=False, i.e. lstat semantics: ``DirEntry.stat()``
                # follows by DEFAULT, and on Windows statting a name that is a
                # symlink to ``\\host\share`` IS the outbound SMB/NTLM
                # authentication -- so fingerprinting a planted link would
                # authenticate to a caller-chosen host before the reader's
                # resolved-target guard in ``_read_agent_spec`` ever runs. The
                # directory-level hold protects the scan DIRECTORY from being
                # swapped; it says nothing about a child entry inside it, and an
                # attacker who can write the scanned path is this feature's own
                # threat model. The LINK's own mtime detects a REPOINT of the
                # link, which a followed stat would miss.
                m = entry.stat(follow_symlinks=False).st_mtime_ns
            except OSError:
                m = 0
            out.append((entry.name, m))
            # The link mtime alone is NOT enough for a symlinked spec: it catches
            # a repoint but not an edit to the file the link resolves to (the
            # common ``~/.kiro/agents/mine.json`` -> a dotfiles copy). Without the
            # target's mtime folded in, such an edit leaves this signature
            # identical and both ``_LIST_AGENTS_CACHE`` and ``_PARSED_SPECS_CACHE``
            # serve the pre-edit model/tools/prompt until a write path clears the
            # cache or the gateway restarts -- the very staleness
            # :func:`agents_dir_revision` returns ``None`` for on a symlinked spec.
            # Recorded under a ``\0target`` key: a NUL cannot appear in a real
            # filename, so this synthetic entry can never collide with another
            # file's ``(name, mtime)`` pair, exactly as :func:`_sensitive_dir_sig`
            # keeps its sentinel outside the filename space.
            #
            # The FOLLOWED stat carries the SMB/NTLM cost the non-following stat
            # above exists to avoid, so it takes the SAME UNC gate the readers
            # apply to a raw spelling (:func:`_unc_refused`, on the link's stored
            # target read locally by ``os.readlink`` -- which connects to no
            # host). A link whose target is an untrusted UNC share is therefore
            # fingerprinted by its link mtime alone and never followed; a repoint
            # to such a share still changes the link mtime and invalidates.
            if entry.is_symlink():
                try:
                    if dir_fd is not None and os.readlink in os.supports_dir_fd:
                        _target = os.readlink(entry.name, dir_fd=dir_fd)
                    else:
                        _target = os.readlink(entry.path)
                except OSError:
                    # A failed descriptor-relative read must not retry by name:
                    # that would re-resolve a directory the caller pinned.
                    _target = None
                if _target is not None:
                    _target_path = (
                        _target
                        if os.path.isabs(_target)
                        else os.path.join(os.path.dirname(entry.path), _target)
                    )
                    target_safe_to_follow = not _unc_refused(
                        _target
                    ) and not _linked_ancestor_refused(_target_path)
                else:
                    target_safe_to_follow = False
                if target_safe_to_follow:
                    try:
                        fm = entry.stat(follow_symlinks=True).st_mtime_ns
                    except OSError:
                        fm = 0
                    out.append((entry.name + "\0target", fm))
    except OSError:
        pass
    return tuple(sorted(out))


def _dir_signature(d: Path) -> _ListAgentsSig:
    """Cheap stat-only signature of the agents dir.

    Captures each spec entry's name and mtime (both forms; a markdown edit
    that went unfingerprinted would serve a stale roster forever) — enough to detect adds,
    removals, renames, and any edit that changes a file's mtime, without
    reading or parsing any file. A skill-view alias is not a spec entry here
    (:func:`_iter_spec_entries`), so an alias write leaves the signature, and
    the caches it guards, untouched. An edit landing inside the same mtime tick
    is invisible here; :func:`clear_list_agents_cache` is the escape hatch
    the write paths use for exactly that case. Naming the files matters: a
    rename changes neither the file count nor any file's mtime, but does
    change the stem-derived agent name and the anchoring of relative
    ``skill://`` globs. Invalidates the :func:`list_agents`, project-names,
    and parsed-specs caches.

    Always a tuple, never ``None``: the catalog caches it revalidates stay on
    for a symlinked or freshly edited directory. :func:`agents_dir_revision`
    is the stricter fingerprint, for answers that must never be served stale.
    """
    try:
        return _entries_signature(_iter_spec_entries(d))
    except OSError:
        return ()


_SpecStatRevision = tuple[str, int, int, int, int, int, int]
AgentsDirRevision = tuple[int, tuple[_SpecStatRevision, ...], int]
# ``st_ctime_ns`` is creation time on Windows, so entry metadata cannot prove
# that an in-place rewrite did not happen; the revision is unavailable there.
AGENTS_DIR_MEMO_ENABLED = not _WINDOWS
# Above this many spec entries no revision is taken, and a read costs what it costs.
# Bound to the shared entry cap so the agents-dir revision and the project scans
# it fingerprints alongside cannot drift onto two different populations.
_AGENTS_DIR_REVISION_MAX_ENTRIES = _AGENT_DIRECTORY_MAX_ENTRIES
# Follow the racy-git precedent: metadata younger than this window is untrusted.
_AGENTS_DIR_RACY_WINDOW_NS = 2_000_000_000
# An answer set that reaches this many keys is cleared whole, so a churn of names cannot grow it.
_AGENTS_DIR_MEMO_MAX_KEYS = 256
_AGENTS_DIR_REVISION_LOCK = threading.Lock()
_AGENTS_DIR_REVISION_OVERFLOW_WARNED: set[str] = set()  # Guarded by the lock above.


def agents_dir_revision(agents_dir: Path) -> AgentsDirRevision | None:
    """Stat-only fingerprint of *agents_dir* strong enough to pin a read answer to.

    ``None`` means "cannot prove freshness; do not memoize". Where
    :func:`_dir_signature` answers every call with a tuple because the catalog
    caches it serves tolerate a same-tick edit, this refuses whenever entry
    metadata could miss a rewrite: no spec is opened or parsed either way.

    The directory's own mtime catches an entry added, removed, renamed or
    re-linked; each spec entry's name, timestamps, size, identity and mode catch
    ordinary in-place edits and metadata changes. The in-process spec
    generation (:func:`spec_cache_generation`) is part of the tuple, so
    :func:`clear_list_agents_cache` -- which the in-process spec writers call --
    moves every revision at once, closing the sub-tick window. An entry whose
    mtime or ctime is within the last two seconds gives ``None``, so a
    same-size rewrite that lands in the same filesystem timestamp tick as the
    previous one cannot be served stale (the racy-git rule).

    A symlinked spec gives ``None``: its entry metadata cannot see edits to its
    target. On Windows the answer is always ``None``: ``st_ctime_ns`` is
    creation time there, so entry metadata cannot prove an in-place rewrite did
    not happen. A directory past :data:`_AGENTS_DIR_REVISION_MAX_ENTRIES` gives
    ``None`` and logs one warning per directory.

    Only the entries :func:`_iter_spec_entries` yields -- a recognised spec
    suffix, not a skill-view alias -- are fingerprinted; that is a superset of
    what the spec scans parse (a Markdown spec shadowed by its JSON twin is still
    fingerprinted; an alias is left out by scan and fingerprint alike), so the
    revision can only be more sensitive than the scan, never less. Adding or
    removing a stray file -- an alias included -- still invalidates through the
    directory mtime, but the stray file itself is omitted from the entry tuples
    and from the entry cap. A ``stat`` that fails records zeros: the entry is
    still named, so its appearance and disappearance are revisions. An entry
    whose kind cannot be determined gives ``None``, and so does a directory that
    cannot be listed: an unlistable directory is not an empty one.
    """
    if not AGENTS_DIR_MEMO_ENABLED:
        return None
    try:
        dir_mtime = agents_dir.stat().st_mtime_ns
    except OSError:
        dir_mtime = 0
    entries: list[_SpecStatRevision] = []
    try:
        for entry in _iter_spec_entries(agents_dir):
            try:
                if entry.is_symlink():
                    return None
            except OSError:
                return None
            try:
                st = entry.stat(follow_symlinks=False)
                entries.append(
                    (
                        entry.name,
                        st.st_mtime_ns,
                        st.st_ctime_ns,
                        st.st_size,
                        st.st_ino,
                        st.st_dev,
                        st.st_mode,
                    )
                )
            except OSError:
                entries.append((entry.name, 0, 0, 0, 0, 0, 0))
            if len(entries) > _AGENTS_DIR_REVISION_MAX_ENTRIES:
                key = str(agents_dir)
                with _AGENTS_DIR_REVISION_LOCK:
                    should_warn = key not in _AGENTS_DIR_REVISION_OVERFLOW_WARNED
                    _AGENTS_DIR_REVISION_OVERFLOW_WARNED.add(key)
                if should_warn:
                    logger.warning(
                        "agents-dir memo disabled for %s: %d spec entries exceed %d",
                        agents_dir,
                        len(entries),
                        _AGENTS_DIR_REVISION_MAX_ENTRIES,
                    )
                return None
    except OSError:
        return None
    cutoff = time.time_ns() - _AGENTS_DIR_RACY_WINDOW_NS
    if dir_mtime > cutoff or any(entry[1] > cutoff or entry[2] > cutoff for entry in entries):
        return None
    return dir_mtime, tuple(sorted(entries)), spec_cache_generation()


T = TypeVar("T")


class AgentsDirMemo(Generic[T]):
    """Answers computed from one walk of an agents directory, pinned to its revision.

    The two ``spec_by_declared_name`` callers (the KAS projection and the
    tool-policy read) each parse every spec in the directory to resolve one
    name, and each keeps its own instance here so their SEL ``operation``
    labels never share an answer. The store and hit rules live once:

    - a revision is taken before ``compute`` and again after it, and the answer
      is stored only when both are equal and not ``None``, so a write landing
      during the read is never memoized under the revision that preceded it;
    - an exception from ``compute`` propagates and nothing is stored;
    - one answer set per directory, replaced whole on a new revision and
      cleared when it reaches :data:`_AGENTS_DIR_MEMO_MAX_KEYS`, so a churn of
      keys cannot grow it.

    An in-process spec write that calls :func:`clear_list_agents_cache` moves
    the spec generation, which is part of every revision, so the stored answers
    stop matching without the memo being told.

    The answer returned is the stored object itself, so a caller that hands it
    to code which may mutate it must copy (the KAS projection deep-copies; the
    tool-policy read's answer is serialized and never mutated). Two threads
    missing at once compute redundantly and last-write-wins, the same answer
    from the same revision. The lock guards the dict only; callers run on
    worker threads.
    """

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._answers: dict[str, tuple[AgentsDirRevision, dict[str, T]]] = {}

    def get(self, agents_dir: Path, key: str, compute: Callable[[], T]) -> T:
        """Return the memoized answer for *key* under *agents_dir*, or ``compute()``."""
        dir_key = str(agents_dir)
        revision = agents_dir_revision(agents_dir)
        if revision is None:
            return compute()
        with self._lock:
            cached = self._answers.get(dir_key)
            if cached is not None and cached[0] == revision and key in cached[1]:
                return cached[1][key]
        answer = compute()
        if agents_dir_revision(agents_dir) != revision:
            return answer
        with self._lock:
            cached = self._answers.get(dir_key)
            if cached is None or cached[0] != revision:
                cached = (revision, {})
                self._answers[dir_key] = cached
            answers = cached[1]
            if len(answers) >= _AGENTS_DIR_MEMO_MAX_KEYS:
                answers.clear()
            answers[key] = answer
        return answer


def clear_list_agents_cache() -> None:
    """Drop the :func:`list_agents` result cache and the :func:`parsed_agent_specs`
    snapshot (forces a fresh scan next call).

    One invalidation point for those two caches, so the agent write paths that
    already call this also cover a write landing inside one mtime tick. The
    project-names cache is untouched: it revalidates purely by directory
    signature and holds only name sets, never parsed spec content.
    Invalidation is normally automatic via the directory signature; call this
    only to force an immediate refresh (e.g. right after writing an agent
    file).

    The generation invalidates spec-derived caches in other modules.
    """
    _LIST_AGENTS_CACHE.clear()
    global _PARSED_SPECS_GEN
    with _PARSED_SPECS_LOCK:
        _PARSED_SPECS_CACHE.clear()
        _PARSED_SPECS_GEN += 1


def _with_edition_agents(disk_agents: list[AgentInfo]) -> list[AgentInfo]:
    """Merge edition-contributed agent-catalog rows onto the on-disk scan.

    Reads ``AgentCatalogProvider.builtin_agents()`` through the platform context
    (deferred import so this module never imports the platform package at load
    time; fails closed to no extra agents). ADD-only and de-duped by name — an
    on-disk agent of the same name wins. The public Default returns ``[]`` so
    this is a no-op for the standalone edition.
    """
    from kiro_crew.platform.context import current_context, safe_context_call

    rows: list[dict[str, Any]] = safe_context_call(
        lambda: list(current_context().agent_catalog.builtin_agents()),
        fallback_factory=list,
        log_message="builtin_agents lookup failed; using none",
    )
    if not rows:
        return list(disk_agents)
    by_name: dict[str, AgentInfo] = {a.name: a for a in disk_agents}
    for row in rows:
        if not isinstance(row, dict):
            continue
        name = row.get("name")
        # A row keyed by a non-string name has no usable identity: the name IS
        # the dedup key, the React list key, and the argument every mutation
        # (agentDetail / agentPatch / setDefaultAgent) is addressed by. Blanking
        # it via __post_init__ would yield an unselectable row that also collides
        # with any other nameless row, so such a row is skipped outright — unlike
        # the cosmetic fields, which degrade.
        if not isinstance(name, str) or not name or name in by_name:
            continue
        try:
            by_name[name] = AgentInfo(
                name=name,
                filename=row.get("filename", ""),
                # Every other field is passed through as-is: this seam is
                # out-of-tree, so ``__post_init__`` — not this call site — is what
                # guarantees the declared ``str`` / ``list[str]`` types hold.
                # ``model`` still goes through spec_model() because its "defer"
                # spelling is domain knowledge, not a generic type fallback.
                description=row.get("description", ""),
                model=spec_model(row),
                skills=list(row.get("skills") or []),
                mcp_servers=list(row.get("mcp_servers") or []),
                source=row.get("source", "builtin"),
                package=row.get("package", ""),
                kirocrew_owned=row.get("kirocrew_owned", False),
            )
        except Exception:
            logger.debug("Skipping malformed edition agent row: %r", row)
    return list(by_name.values())


def _global_agent_info(f: Path, data: dict[str, Any]) -> AgentInfo:
    """Build an :class:`AgentInfo` for a user-level (``~/.kiro/agents``) config."""
    # Coerced BEFORE the package-detection below, which does
    # ``stem.endswith(agent_name)``: a non-string name raised TypeError there, and
    # the broad ``except`` around the caller's loop turned that into a silently
    # DROPPED agent rather than a degraded one. Falling back to the filename stem
    # keeps the row selectable under the name its file already implies.
    agent_name = spec_str(data, "name", f.stem)
    stem = f.stem

    package = ""
    # Package-installed agents follow the "{package}-{name}.json" filename
    # convention (a generic package-manager convention, not tied to any specific
    # tool). A plain "{name}.json" is built-in.
    is_package_filename = agent_name and stem.endswith(agent_name) and stem != agent_name
    if is_package_filename:
        pkg_stem = f.stem
        if pkg_stem.startswith("local-"):
            pkg_stem = pkg_stem[len("local-") :]
        package = pkg_stem[: -(len(agent_name) + 1)]

    if f.name in (AGENT_FILENAME, LITE_AGENT_FILENAME):
        source = "kirocrew"
    elif is_package_filename:
        source = "package"
    else:
        source = "builtin"

    return AgentInfo(
        name=agent_name,
        filename=f.name,
        description=spec_str(data, "description"),
        model=spec_model(data),
        skills=_extract_skills(data),
        mcp_servers=_mcp_server_names(data),
        source=source,
        package=package,
        scope=SCOPE_GLOBAL,
        kirocrew_owned=f.name in OWNED_KIRO_AGENT_FILES,
    )


def _project_agent_info(f: Path, data: dict[str, Any]) -> AgentInfo:
    """Build an :class:`AgentInfo` for a project-level (``<project>/.kiro``) config.

    The ``{package}-{name}`` filename convention is deliberately NOT applied here:
    a project checkout has no package manager installing into it, so a hyphenated
    filename is just a filename and reading a package out of it would invent one.
    """
    return AgentInfo(
        name=project_agent_name(f),
        filename=f.name,
        description=spec_str(data, "description"),
        model=spec_model(data),
        skills=_extract_skills(data),
        mcp_servers=_mcp_server_names(data),
        source="builtin",
        package="",
        scope=SCOPE_PROJECT,
        kirocrew_owned=False,
    )


def _mcp_server_names(data: dict[str, Any]) -> list[str]:
    """The ``mcpServers`` keys of a spec, or ``[]`` when the field is not a mapping."""
    mcp = data.get("mcpServers") or {}
    return list(mcp.keys()) if isinstance(mcp, dict) else []


def list_agents(
    agents_dir: Path | None = None,
    project_dir: str | Path | None = None,
) -> list[AgentInfo]:
    """Scan the agent directories for all installed agents.

    Returns a list of ``AgentInfo`` objects. Each agent corresponds to a kiro-cli
    agent config file that can be selected via ``session/set_mode`` in the ACP
    protocol.

    Two scopes are searched when *project_dir* is given: the user-level directory
    (``~/.kiro/agents``, or *agents_dir*) and the project's own
    (``<project>/.kiro/agents`` plus ``<project>/.kiro/*.agent-spec.json``). A
    project agent SHADOWS a user-level agent of the same name and the shadowing is
    logged, mirroring kiro-cli — which resolves ``--agent`` against its cwd first,
    and is spawned by Kiro Crew with the session's project dir as that cwd. The
    losing entry is not returned: it is unreachable for this session, so listing it
    would offer an agent that cannot run.

    Omitting *project_dir* preserves the user-level-only behavior, which is what
    callers with no session context (and therefore no project) want.

    Results are cached per scope pair and reused while both directory signatures
    are unchanged, so repeated calls avoid re-reading and re-parsing every agent
    JSON on the event loop.
    """
    d = agents_dir or _kiro_agents_dir()
    project_files = project_agent_files(project_dir, operation="list_agents", source="unknown")
    cache_key = (str(d), str(project_dir or ""))
    signature: tuple[_ListAgentsSig, ...] = (
        _dir_signature(d),
        _dir_signature(project_kiro_dir(project_dir)) if project_dir else (),
        _dir_signature(project_agents_dir(project_dir)) if project_dir else (),
    )
    cached = _LIST_AGENTS_CACHE.get(cache_key)
    if cached is not None and cached[0] == signature:
        return _with_edition_agents(list(cached[1]))

    agents: list[AgentInfo] = []

    if d.is_dir():
        for hidden_md in shadowed_markdown_specs(d):
            # The one user-facing surface every author reads, so this is where
            # the JSON-wins rule is announced rather than silently applied.
            logger.warning(
                "agent %r: %s is shadowed by its JSON twin %s.json and is not read; "
                "delete one of the two files",
                hidden_md.stem,
                hidden_md.name,
                hidden_md.stem,
            )
        user_candidates = 0
        user_parsed = 0
        for f in iter_agent_spec_files(d):
            # AppleDouble sidecars are rejected by design, not by failure — a
            # directory holding only sidecars is empty of specs, not broken.
            if not f.name.startswith("._"):
                user_candidates += 1
            try:
                data = _read_agent_spec(f, operation="list_agents", source="unknown")
                if data is None:
                    continue
                agents.append(_global_agent_info(f, data))
                # Counted AFTER the append: a spec that parses but whose row
                # construction raises into the handler below still ends in
                # "discovery listed nothing", which is exactly what the
                # systematic-failure warning exists to surface.
                user_parsed += 1
            except Exception:
                logger.debug("Skipping invalid agent config: %s", f)
                continue
        _warn_on_systematic_scan_failure(d, user_candidates, user_parsed)
        # One sidecar read for the whole scan, not one per row. Global scope
        # only: forks are made from (and recorded against) user-level templates.
        # Lenient on purpose: a corrupt sidecar degrades the roster to "no fork
        # info" rather than failing the whole listing — the strict readers are
        # the mutators and the governance paths, where {} would be a hazard.
        try:
            forks = agent_state.all_fork_info()
        except (OSError, ValueError):
            logger.warning("fork sidecar unreadable; roster shows no fork info", exc_info=True)
            forks = {}
        if forks:
            for a in agents:
                fork_info = forks.get(a.name)
                if fork_info:
                    a.forked_from = fork_info["forked_from"]
                    a.private_to = fork_info["private_to"]

    # Deduplicate by name — prefer package-installed (has package) over fallback
    seen: dict[str, AgentInfo] = {}
    for a in agents:
        existing = seen.get(a.name)
        if existing is None:
            seen[a.name] = a
        elif a.package and not existing.package:
            seen[a.name] = a
        elif a.package and existing.package:
            if a.package == existing.package:
                # Package managers publish a locally-built package as BOTH
                # "{package}-{name}.json" AND "local-{package}-{name}.json";
                # stripping the "local-" prefix above makes the twin files
                # collide on the same (name, package). That is the EXPECTED
                # on-disk shape for every locally published package — warning
                # on it produced a self-contradictory "from packages 'X' and
                # 'X'" line per agent per scan (dozens per startup), drowning
                # real signals. Keep the first-seen file (unchanged first-wins
                # policy; sort order decides which twin that is) and note the
                # twin at debug.
                logger.debug(
                    "Agent '%s': keeping '%s' over same-package twin '%s'",
                    a.name,
                    existing.filename,
                    a.filename,
                )
            else:
                logger.warning(
                    "Duplicate agent name '%s' from packages '%s' and '%s'; keeping '%s'",
                    a.name,
                    existing.package,
                    a.package,
                    existing.package,
                )

    # Project scope LAST so it shadows: kiro-cli resolves --agent against its cwd
    # before the user-level dir, and Kiro Crew spawns it with the session's project
    # dir as cwd, so the project entry is what would actually run. The warning
    # mirrors kiro-cli's own conflict notice — shadowing is correct here, silent
    # shadowing is not, because the two configs can differ in tools and permissions.
    project_candidates = 0
    project_parsed = 0
    for pf in project_files:
        if not pf.name.startswith("._"):
            project_candidates += 1
        try:
            data = _read_agent_spec(pf, operation="list_agents", source="unknown")
            if data is None:
                continue
            info = _project_agent_info(pf, data)
            shadowed = seen.get(info.name)
            if shadowed is not None and shadowed.scope == SCOPE_GLOBAL:
                logger.warning(
                    "Project agent '%s' (%s) shadows the user-level agent in %s",
                    info.name,
                    pf,
                    shadowed.filename,
                )
            seen[info.name] = info
            project_parsed += 1
        except Exception:
            logger.debug("Skipping invalid project agent config: %s", pf)
            continue
    if project_dir:  # type narrowing only; without it candidates is 0 anyway
        _warn_on_systematic_scan_failure(
            project_agents_dir(project_dir), project_candidates, project_parsed
        )

    result = list(seen.values())
    _LIST_AGENTS_CACHE[cache_key] = (signature, result)
    return _with_edition_agents(result)
