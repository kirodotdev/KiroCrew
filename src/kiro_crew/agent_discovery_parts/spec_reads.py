"""The user-level agents directory, and the fenced read of one agent spec in it.

The sensitive-path and Windows UNC gates every reader asks before it opens a spec, the
read pinned to the descriptor it opens, the denial audit row, the bounded head probes
(plain markdown, AppleDouble sidecars), the lookups by declared name and by stem, and the
warning a scan logs when every spec it read failed.

:mod:`kiro_crew.agent_discovery` stays the import path and the patch surface; see
:mod:`kiro_crew.agent_discovery_parts`.
"""

from __future__ import annotations

import errno
import logging
import os
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable, Iterator, Mapping, Sequence

from kiro_crew import hooks, platform_compat
from kiro_crew.agent_spec_format import (
    is_markdown_spec,
    iter_agent_spec_files,
    markdown_head_is_fenceless,
    parse_agent_spec_bytes,
)
from kiro_crew.atomic_write import retry_read_sharing_window
from kiro_crew.config.paths import kiro_agents_dir
from kiro_crew.hooks import FileTooLargeError, is_unc_shape, unc_probe_allowed
from kiro_crew.pinned_fs import open_fenced_for_read
from kiro_crew.platform import redact_log_via_context
from kiro_crew.security import is_sensitive_canonical_path
from kiro_crew.sel import sel as _sel
from kiro_crew.user_json import strict_json_equal

# Logs under the facade's name, which operators and tests filter on.
logger = logging.getLogger("kiro_crew.agent_discovery")


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


class _SpecReadRefused(OSError):
    """The pinned open refused the spec's inode (link, hardlink, non-regular, fence)."""


# Named by the path callers import it from: a traceback or a log line renders the
# raised class as ``module.qualname``, so the owner module must not leak into it.
_SpecReadRefused.__module__ = "kiro_crew.agent_discovery"


def _read_spec_bytes(real: Path, *, fence: Callable[[Path], bool] | None = None) -> bytes:
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

    A consumer *fence* judges *real* before the open and the opened inode's
    kernel path alongside :func:`_fence_refuses`; a refusal raises
    :class:`_SpecReadRefused`.
    """
    if fence is not None and fence(real):
        raise _SpecReadRefused(f"consumer fence refused {str(real)!r}")

    def refused(fd_real: str) -> bool:
        opened = Path(fd_real)
        return _fence_refuses(opened) or (fence is not None and fence(opened))

    cap = hooks.MAX_FILE_BYTES

    def read_once() -> bytes:
        # Local import: a module-level name would be served by this module's facade.
        from kiro_crew.bounded_read import bounded_read

        fd = open_fenced_for_read(real, fence=refused, refusal=_SpecReadRefused)
        with os.fdopen(fd, "rb") as fh:
            return bounded_read(fh, cap + 1)

    # On Windows a read that lands while a writer replaces the spec (the
    # dashboard's MCP enable, disable and remove paths rewrite kirocrew.json)
    # fails with a sharing violation. That spec is present and valid, so the
    # pinned open and read are retried with read_bytes_with_retry's budget,
    # off the event loop only; POSIX keeps the single attempt.
    data = retry_read_sharing_window(read_once, real)
    if len(data) > cap:
        raise FileTooLargeError(f"File exceeds {cap // (1024 * 1024)} MB safety cap")
    return data


# Resolved per call, never captured at import: an import-time binding freezes
# the data home and defeats pod isolation, the lazy legacy-home migration and
# test isolation. The name below is an opt-in override (None = live home) so
# existing monkeypatch call sites keep working. See config.md "Data Home";
# dashboard/handlers/usage_store.py is the reference implementation.
_KIRO_AGENTS_DIR: Path | None = None


def _kiro_agents_dir() -> Path:
    """The kiro-cli agents directory, resolved against the live data home."""
    return _KIRO_AGENTS_DIR if _KIRO_AGENTS_DIR is not None else kiro_agents_dir()


def _audit_denied(*, operation: str, source: str, resources: str, error: str) -> None:
    """Emit a denial audit row for a refused path, never raising.

    EVERY denial path in agent discovery promises not to raise -- ``_read_agent_spec``
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
    server) and no denial site in agent discovery is one -- the denial-side rule in
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
    fence: Callable[[Path], bool] | None = None,
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

    *fence* is an optional consumer policy judged on the resolved target and on
    the opened inode's kernel path. A refusal is audited as ``consumer fence
    rejected`` and the spec reads as unusable.
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
    to_resolve = path
    if _WINDOWS:
        # A local spelling can still be a link (or sit under one) whose target
        # names a share, and ``resolve`` would follow it into the SMB probe the
        # check above exists to prevent. The hooks screen reads each link's own
        # target without traversing it, so a chain reaching an untrusted share
        # is refused before anything follows it. The screened target is what is
        # resolved: re-following the original links would let a link retargeted
        # after the screen reach the share anyway. *path* still names the spec
        # for parsing and for the audit trail, and the pinned open below judges
        # the inode actually read.
        screened = hooks._screen_windows_links(str(path))
        if screened is None:
            _audit_denied(
                operation=operation,
                source=source,
                resources=str(path),
                error="untrusted link target rejected",
            )
            return None
        if screened != str(path):
            # A path with no link screens to itself; anything else had a
            # link rewritten to its vetted target.
            to_resolve = Path(screened)
    try:
        real = to_resolve.resolve(strict=True)
    except (OSError, RuntimeError):
        # OSError: broken link / permission; RuntimeError: pathlib's signal for
        # a symlink LOOP (a self-referential agent symlink is one `ln -s` away
        # in a user-writable dir). Both mean "not a readable spec", and an
        # uncaught loop here crashes whichever surface asked — e.g. Slack's
        # `!agent` handler exits without replying.
        return None
    if _unc_refused(str(real)):
        # A share the link screen admitted, or a resolve that still landed on
        # one: the read must not load the remote document.
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

    def audited_fence(candidate: Path) -> bool:
        refused = fence is not None and fence(candidate)
        if refused:
            _audit_denied(
                operation=operation,
                source=source,
                resources=str(candidate),
                error="consumer fence rejected",
            )
        return refused

    try:
        raw = (
            _read_spec_bytes(real) if fence is None else _read_spec_bytes(real, fence=audited_fence)
        )
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
    except PermissionError:
        if platform_compat.IS_WINDOWS:
            # The spec is present but stayed locked past the retry budget: a
            # reader of this None would show its MCP servers as never
            # configured, so the miss is logged where an operator sees it.
            logger.warning("Skipping agent config %r: still locked by another process", path)
        else:
            logger.debug("Skipping unreadable agent config: %r", path)
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


# Reports the path callers import it from, as it did before it moved: an import
# table that pins each name's defining module reads this.
_read_agent_spec.__module__ = "kiro_crew.agent_discovery"


class SensitiveAgentSpecPathError(ValueError):
    """A spec path resolved to a target the sensitive-path fence refuses.

    A ``ValueError`` like the other deterministic refusals, so a caller that
    only needs "not a spec" catches it with them; distinct so a caller that
    keeps a different answer for a refused target than for a document that does
    not parse -- the Slack name resolver treats a broken JSON spec as still
    occupying its name, a refused target as no agent at all -- can tell them
    apart without inspecting the message.
    """


# Named by the path callers import it from: a traceback or a log line renders the
# raised class as ``module.qualname``, so the owner module must not leak into it.
SensitiveAgentSpecPathError.__module__ = "kiro_crew.agent_discovery"


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


# The fence probe's head: a UTF-8 BOM (3 bytes) plus the longest opening fence
# line (``---\r\n``, 5 bytes) is 8, so 64 leaves the probe nothing to judge but
# the fence -- which is the point. Never the file's length.
_FENCE_PROBE_BYTES = 64


def _read_head(fd: int, limit: int) -> tuple[bytes, bool]:
    """At most *limit* bytes from *fd*, and whether the file continues past them.

    Reads ``limit + 1`` bytes (looping over short reads) so the caller can tell
    a file that ends exactly at the bound from one that runs past it: a
    multibyte sequence broken at the cut is incomplete, one broken at
    end-of-file is the parser's own decode failure.
    """
    data = b""
    while len(data) <= limit:
        chunk = os.read(fd, limit + 1 - len(data))
        if not chunk:
            break
        data += chunk
    return data[:limit], len(data) > limit


def plain_markdown_document(path: Path) -> bool:
    """Whether the markdown file at *path* has no OPENING frontmatter fence.

    The one answer to "is this ``.md`` in an agents directory a spec at all":
    a plain markdown file dropped there -- a README, a shared prompt fragment
    -- declares nothing and hides nothing, so it is not a spec. Callers ask it
    only AFTER :func:`read_agent_spec_strict` refused the file, to decide
    whether to skip the file rather than raise. A FENCED document that fails
    to parse is not plain: it announced itself as a spec and may be a
    truncated real one, so the caller keeps raising. The byte test is
    :func:`kiro_crew.agent_spec_format.markdown_head_is_fenceless`, the same
    opening-fence rule :func:`~kiro_crew.agent_spec_format.split_markdown_spec`
    applies; this function only reads the head it judges.

    The read path is the strict reader's own gates, never
    :func:`kiro_crew.hooks.validate_file_path`: that gate re-resolves on the
    two-worker ``mc-pathres`` pool and fails closed when the pool misses its
    budget, and a policy request probes here on every call after the strict
    reader refused -- the saturation the strict reader was moved off the pool
    to survive. Refused, exactly as the strict reader refuses: a spelling or a
    resolved target that is a UNC path outside the trusted roots on Windows
    (:func:`_unc_refused`, before and after the resolve); a spelling
    ``Path.resolve(strict=True)`` cannot canonicalise (absent, broken or
    looping link, permission) -- a link is otherwise FOLLOWED and its target
    judged; a resolved target :func:`_fence_refuses` fences; and whatever
    :func:`kiro_crew.pinned_fs.open_fenced_for_read` refuses at the open. No
    SEL row is written: the strict reader already audited any denial.

    The read is BOUNDED at ``_FENCE_PROBE_BYTES`` through :func:`_read_head`.
    The strict reader refuses an oversize file AT the cap precisely so it is
    never slurped into memory, and an unbounded re-read here would hand the
    caller an attacker-sized allocation whose ``MemoryError`` escapes every
    fail-closed arm. An over-cap plain document is still skipped: the probe
    judges its opening, not its length.

    Every failure is ``False``: a file that cannot even be probed is unknown,
    not ignorable, so the caller keeps raising.
    """
    probed = _probe_spec_head(path)
    if probed is None:
        return False
    head, truncated = probed
    return markdown_head_is_fenceless(head, complete=not truncated)


#: The magic numbers that open an AppleDouble and an AppleSingle file (RFC 1740).
_APPLE_DOUBLE_MAGICS = (b"\x00\x05\x16\x07", b"\x00\x05\x16\x00")


def apple_double_sidecar(path: Path) -> bool:
    """Whether *path* is a macOS AppleDouble sidecar, judged by its name AND its bytes.

    A macOS archive leaves a ``._<name>`` sidecar beside every file it carries.
    The name alone does not settle what the file is: kiro-cli applies no
    filename filter, so a ``._x.json`` that holds a spec is loaded under the
    name it declares. A file that opens with the AppleDouble or AppleSingle
    magic starts with a NUL byte, which neither spec form can start with, so
    no spec reader loads it. The head is read exactly as
    :func:`plain_markdown_document` reads it, through the strict reader's gates
    and bounded at ``_FENCE_PROBE_BYTES``. Every failure is ``False``.
    """
    if not path.name.startswith("._"):
        return False
    probed = _probe_spec_head(path)
    return probed is not None and probed[0].startswith(_APPLE_DOUBLE_MAGICS)


def _probe_spec_head(path: Path) -> tuple[bytes, bool] | None:
    """The head of *path* and whether the file runs past it, or ``None`` when refused.

    The gates and the bound :func:`plain_markdown_document` documents.
    """
    if _unc_refused(str(path)):
        return None
    try:
        real = path.resolve(strict=True)
    except (OSError, RuntimeError):
        # RuntimeError: pathlib's signal for a symlink loop on the Pythons
        # that raise it as such.
        return None
    if _unc_refused(str(real)) or _fence_refuses(real):
        return None
    try:
        fd = open_fenced_for_read(
            real,
            fence=lambda fd_real: _fence_refuses(Path(fd_real)),
            refusal=_SpecReadRefused,
        )
    except OSError:
        return None
    try:
        return _read_head(fd, _FENCE_PROBE_BYTES)
    except OSError:
        return None
    finally:
        os.close(fd)


class AmbiguousAgentSpecError(ValueError):
    """More than one spec in one directory declares the same ``name``, and they
    differ.

    Which of them is live is undefined, so a resolver that picked one would
    hand the caller an agent the operator did not name, with that agent's tools
    and prompt. Every declared-name resolver refuses instead, unless
    ``agent.spec_pins`` pins the name to one of them; the message names each file
    and the ``kirocrew agent pin`` command that chooses it. A reader of parsed
    content (:func:`spec_by_declared_name`) raises this only when the
    claimants' content differs: identical files answer the same question
    whichever is live, so it hands back that one answer instead. A writer
    (:func:`kiro_crew.agent.agent_spec_path`) must pick a file to write and
    refuses the pair whatever its content.

    ``paths`` carries the same files as data, for a caller that must name them
    WITHOUT their directory -- the tool-policy endpoint's 409 ``reason``
    crosses the wire into a model-visible refusal, and the message's full
    paths disclose the account name and on-disk layout there. Empty when the
    raiser did not supply them; the message is then the only record.

    ``agent`` is the declared name the files share, and ``packages`` maps each
    path to the package that installed it (``agent_spec_pins.spec_package``), or
    ``None``: the dashboard's chooser shows both. ``summaries`` maps each path to
    the fields of its parse the chooser compares (``agent_spec_pins.SHOWN_FIELDS``),
    so the chooser never reopens the files. ``pin`` is the pinned file name when a
    pin is in force and broken (:class:`AgentSpecPinError`), else ``None``.
    """

    pin: str | None = None

    def __init__(
        self,
        message: str,
        *,
        paths: Sequence[Path] = (),
        agent: str = "",
        packages: Mapping[Path, str | None] | None = None,
        summaries: Mapping[Path, dict[str, Any]] | None = None,
    ) -> None:
        super().__init__(message)
        self.paths: tuple[Path, ...] = tuple(paths)
        self.agent = agent
        self.packages: dict[Path, str | None] = dict(packages or {})
        self.summaries: dict[Path, dict[str, Any]] = dict(summaries or {})


# Named by the path callers import it from: a traceback or a log line renders the
# raised class as ``module.qualname``, so the owner module must not leak into it.
AmbiguousAgentSpecError.__module__ = "kiro_crew.agent_discovery"


class AgentSpecPinError(AmbiguousAgentSpecError):
    """``agent.spec_pins`` pins a name to a file that none of its claimants is.

    The pinned file is missing, unreadable, or declares another name, while two or
    more specs declare the name. Every resolver refuses: falling back to another
    claimant would run a file the operator did not choose. A subclass, so every
    caller that refuses an ambiguous name refuses this too.
    """

    def __init__(
        self,
        message: str,
        *,
        pin: str,
        paths: Sequence[Path] = (),
        agent: str = "",
        packages: Mapping[Path, str | None] | None = None,
        summaries: Mapping[Path, dict[str, Any]] | None = None,
    ) -> None:
        super().__init__(message, paths=paths, agent=agent, packages=packages, summaries=summaries)
        self.pin = pin


AgentSpecPinError.__module__ = "kiro_crew.agent_discovery"


@dataclass(frozen=True)
class DeclaredSpecClaim:
    """What :func:`declared_spec_claim` resolved a declared name to.

    ``path`` and ``spec`` are the file that runs and its parse; ``claimants`` every
    file that declares the name, in walk order; ``divergent`` whether their parses
    differ; ``pinned`` whether ``agent.spec_pins`` chose ``path`` among several.
    """

    path: Path
    spec: dict[str, Any]
    claimants: tuple[Path, ...]
    divergent: bool
    pinned: bool


def spec_by_declared_name(
    agents_dir: Path,
    agent_id: str,
    *,
    operation: str,
    source: str,
    pins: Mapping[str, str] | None = None,
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
    file an id means. Every reader of the file a session runs follows the same
    rule, ``agent.spec_pins`` included (:func:`declared_spec_claim`).

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

    Raises :class:`AmbiguousAgentSpecError` when two specs declare *agent_id*
    with DIFFERENT content, the refusal :func:`kiro_crew.agent.agent_spec_path`
    makes on the same names. Claimants whose parsed content is equal are not
    refused: every caller of this scan reads the parse and never writes the
    file, and an identical pair gives the same parse whichever of them the
    backend runs, so the question the caller asks (this agent's policy, this
    agent's projection) has one answer and that answer is returned, with a
    warning naming each file so the operator can tidy the directory. A package
    installer that flattens dependencies writes such twins. Equality is of the
    whole parse: a pair that agrees on ``managedToolPolicy`` but differs
    elsewhere is still two agents, and stays refused. Propagates ``OSError``
    from the directory walk itself; the per-file reads never raise.

    Only the first match's parse is held, so peak memory is a few capped parses,
    not one per same-name file; later matches keep only their paths.

    A fresh walk rather than a filter over :func:`parsed_agent_specs`: that
    cache's SEL labels are first-reader-wins, so a denial here would be
    attributed to whichever listing warmed it. This also returns a parse the
    caller owns, not a shared read-only row.
    """
    entry = declared_spec_entry(agents_dir, agent_id, operation=operation, source=source, pins=pins)
    return None if entry is None else entry[1]


def declared_spec_entry(
    agents_dir: Path,
    agent_id: str,
    *,
    operation: str,
    source: str,
    fence: Callable[[Path], bool] | None = None,
    pins: Mapping[str, str] | None = None,
) -> tuple[Path, dict[str, Any]] | None:
    """:func:`spec_by_declared_name` with the listed path of the first claimant.

    *fence* is handed to :func:`_read_agent_spec` for every scanned entry, so a
    caller can hold followed links to its own policy. The path names the entry
    as listed and is never reopened by this scan. *pins* is a snapshot of
    ``agent.spec_pins``, for a caller that keys a memo by the same snapshot.
    """
    claim = declared_spec_claim(
        agents_dir, agent_id, operation=operation, source=source, fence=fence, pins=pins
    )
    return None if claim is None else (claim.path, claim.spec)


def declared_spec_claim(
    agents_dir: Path,
    agent_id: str,
    *,
    operation: str,
    source: str,
    fence: Callable[[Path], bool] | None = None,
    pins: Mapping[str, str] | None = None,
    parsed_rows: Sequence[tuple[dict[str, Any], Path]] | None = None,
) -> DeclaredSpecClaim | None:
    """The one declared-name resolver, with ``agent.spec_pins`` applied.

    Walks *agents_dir* through :func:`_read_agent_spec` and collects every spec
    that declares *agent_id*. With one claimant, or several identical ones and no
    pin, the first is the answer, as :func:`spec_by_declared_name` documents.
    When two or more declare it, the pin for *agent_id* (:mod:`kiro_crew.agent_spec_pins`,
    read at call time unless *pins* is given) decides:

    * the pinned file is a claimant: it is the answer, ``pinned`` is set;
    * a pin naming none of them: :class:`AgentSpecPinError`, never another file;
    * no pin and differing content: :class:`AmbiguousAgentSpecError`, whose
      message lists each file and the ``kirocrew agent pin`` command for it.

    A pin naming the only claimant is ignored; a pin naming none of the claimants
    is refused however many are left. Only the first
    match and the pinned match are held; every other parse is dropped as the walk
    moves on. Snapshot readers may pass *parsed_rows* from the hardened
    :func:`parsed_agent_specs` cache to select without reopening unchanged files.
    They retain the snapshot's freshness and attribution contract. A consumer
    *fence* requires a fresh read and cannot be combined with cached rows.
    """
    if parsed_rows is not None and fence is not None:
        raise TypeError("parsed_rows and fence cannot be combined")
    # Call-time: the pin store is not part of this family, so the facade serves no
    # binding of it.
    from kiro_crew import agent_spec_pins

    match: dict[str, Any] | None = None
    pin = agent_spec_pins.spec_pin_for(agent_id, pins)
    pinned_spec: dict[str, Any] | None = None
    pinned_path: Path | None = None
    match_paths: list[Path] = []
    packages: dict[Path, str | None] = {}
    summaries: dict[Path, dict[str, Any]] = {}
    divergent = False
    rows = (
        parsed_rows
        if parsed_rows is not None
        else (
            (_read_agent_spec(path, operation=operation, source=source, fence=fence), path)
            for path in iter_agent_spec_files(agents_dir)
        )
    )
    for spec, path in rows:
        if isinstance(spec, dict) and spec.get("name") == agent_id:
            if match is None:
                match = spec
            elif not strict_json_equal(spec, match):
                divergent = True
            if pin is not None and path.name == pin and pinned_spec is None:
                pinned_spec, pinned_path = spec, path
            match_paths.append(path)
            packages[path] = agent_spec_pins.spec_package(spec)
            summaries[path] = {key: spec.get(key) for key in agent_spec_pins.SHOWN_FIELDS}
    if match is None:
        if pin is not None:
            # Pinned, but no file declares the name: not "no spec", which would let
            # a filename fallback run some other file. The pin is broken.
            raise AgentSpecPinError(
                agent_spec_pins.broken_pin_message(agent_id, pin, []),
                pin=pin,
                agent=agent_id,
            )
        return None
    decision, _pin = agent_spec_pins.pin_decision(
        agent_id, [path.name for path in match_paths], {} if pin is None else {agent_id: pin}
    )
    file_names = [path.name for path in match_paths]
    if decision == "broken":
        assert pin is not None
        raise AgentSpecPinError(
            agent_spec_pins.broken_pin_message(agent_id, pin, file_names),
            pin=pin,
            paths=match_paths,
            agent=agent_id,
            packages=packages,
            summaries=summaries,
        )
    if decision == "use" and pinned_spec is not None and pinned_path is not None:
        return DeclaredSpecClaim(pinned_path, pinned_spec, tuple(match_paths), divergent, True)
    if len(match_paths) > 1:
        # Paths are repr'd: a filename in this user-writable, tool-shared
        # directory is untrusted input, and both texts reach a terminal.
        if divergent:
            # The message is what a refused session start shows the user, so it
            # names every file and the command that picks one of them.
            raise AmbiguousAgentSpecError(
                agent_spec_pins.ambiguous_message(
                    agent_id, [str(path) for path in match_paths], file_names
                ),
                paths=match_paths,
                agent=agent_id,
                packages=packages,
                summaries=summaries,
            )
        names = ", ".join(repr(str(path)) for path in match_paths)
        # The name and the filenames are untrusted input from a user-writable
        # directory and this line persists in gateway.log: the same log-egress
        # redaction every other operational line carrying foreign text applies
        # (``redact_log_via_context``, the non-raising spelling for a log site),
        # so a credential-shaped name is scrubbed before it is written.
        logger.warning(
            "%d identical specs declare the name %s: %s. Whichever is live, the spec "
            "is the same, so it is used; remove or rename one to tidy the directory.",
            len(match_paths),
            redact_log_via_context(repr(agent_id)),
            redact_log_via_context(names),
        )
    return DeclaredSpecClaim(match_paths[0], match, tuple(match_paths), divergent, False)


def iter_declared_specs(
    agents_dir: Path, *, operation: str, source: str
) -> Iterator[tuple[str, dict[str, Any]]]:
    """Yield ``(declared name, parsed spec)`` for every spec in *agents_dir* that declares one.

    The walk :func:`spec_by_declared_name` makes, through the same reader and
    under the caller's SEL labels, without choosing a name: a caller that needs
    the answer for every declared name reads the directory once instead of once
    per name. A spec that is refused, is not an object, or declares no string
    ``name`` is skipped, as that scan skips it. Each parse is yielded and then
    dropped, so the caller decides what to keep. Propagates ``OSError`` from
    the directory walk itself.
    """
    for path in iter_agent_spec_files(agents_dir):
        spec = _read_agent_spec(path, operation=operation, source=source)
        if isinstance(spec, dict):
            name = spec.get("name")
            if isinstance(name, str):
                yield name, spec


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
