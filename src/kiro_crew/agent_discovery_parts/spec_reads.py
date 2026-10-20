"""The user-level agents directory, and the fenced read of one agent spec in it.

The sensitive-path and Windows UNC gates every reader asks before it opens a spec, the
read pinned to the descriptor it opens, the denial audit row, the bounded head probes
(plain markdown, AppleDouble sidecars), the lookups by declared name and by stem, and the
warning a scan logs when every spec it read failed.

:mod:`kiro_crew.agent_discovery` stays the import path and the patch surface; see
:mod:`kiro_crew.agent_discovery_parts`.
"""

from __future__ import annotations

import contextlib
import errno
import itertools
import logging
import os
from pathlib import Path
from typing import Any, Iterable, Iterator, Sequence

from kiro_crew import hooks
from kiro_crew.agent_spec_format import (
    is_markdown_spec,
    iter_agent_spec_files,
    markdown_head_is_fenceless,
    parse_agent_spec_bytes,
)
from kiro_crew.config.paths import kiro_agents_dir
from kiro_crew.hooks import (
    FileTooLargeError,
    _screen_windows_links,
    is_unc_shape,
    unc_probe_allowed,
)
from kiro_crew.pinned_fs import (
    PinnedPathRefusal,
    fd_real_path,
    open_fenced_for_read,
    open_fenced_for_read_at,
    open_in_pinned_parent,
    supports_pinned_walk,
)
from kiro_crew.platform import redact_log_via_context
from kiro_crew.security import is_sensitive_canonical_path, is_sensitive_path
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


def _read_spec_bytes(
    real: Path,
    *,
    dir_fd: int | None = None,
    operation: str = "list_agents",
    source: str = "list_agents",
) -> bytes:
    """Read a spec through either its resolved path or a held parent descriptor.

    A ``dir_fd`` is the descriptor that enumerated *real*'s parent. In that
    shape the leaf is opened relative to the held directory, so replacing the
    parent name after enumeration cannot redirect a project-agent parse. The
    kernel-reported opened path still passes the sensitive-path and UNC fences.
    ``None`` retains the hardened by-name reader for surfaces without a pinned
    directory descriptor.
    """
    if dir_fd is None:
        fd = open_fenced_for_read(
            real,
            fence=lambda fd_real: _fence_refuses(Path(fd_real)),
            refusal=_SpecReadRefused,
        )
    else:

        def _descriptor_fence(fd_real: str) -> bool:
            if _unc_refused(fd_real):
                _audit_denied(
                    operation=operation,
                    source=source,
                    resources=fd_real,
                    error="untrusted UNC path rejected",
                )
                return True
            if _fence_refuses(Path(fd_real)):
                _audit_denied(
                    operation=operation,
                    source=source,
                    resources=fd_real,
                    error="sensitive path rejected",
                )
                return True
            return False

        fd = open_fenced_for_read_at(
            dir_fd,
            real.name,
            fence=_descriptor_fence,
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
) -> dict[str, Any] | None:
    """Parse one spec through the hardened by-name reader contract."""
    return _read_agent_spec_from_descriptor(
        path,
        operation=operation,
        source=source,
        dir_fd=None,
    )


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
    and prompt. Every declared-name resolver refuses instead; the message names
    each file so the operator can remove or rename one. A reader of parsed
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
    """

    def __init__(self, message: str, *, paths: Sequence[Path] = ()) -> None:
        super().__init__(message)
        self.paths: tuple[Path, ...] = tuple(paths)


# Named by the path callers import it from: a traceback or a log line renders the
# raised class as ``module.qualname``, so the owner module must not leak into it.
AmbiguousAgentSpecError.__module__ = "kiro_crew.agent_discovery"


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

    Raises :class:`AmbiguousAgentSpecError` when two specs declare *agent_id*
    with DIFFERENT content, the refusal :func:`kiro_crew.agent.agent_spec_path`
    makes on the same names. Claimants whose parsed content is equal are not
    refused: every caller of this scan reads the parse and never writes the
    file, and an identical pair gives the same parse whichever of them the
    backend runs, so the question the caller asks (this agent's policy, this
    agent's projection) has one answer and that answer is returned, with a
    warning naming each file so the operator can tidy the directory. A package
    installer that flattens dependencies writes exactly such twins for an agent
    two packages vend; refusing them denied the agent its policy and its KAS
    session with no lasting repair. Equality is of the whole parse, not of the
    fields one caller reads: a pair that agrees on ``managedToolPolicy`` but
    differs elsewhere is still two agents, and stays refused. Propagates
    ``OSError`` from the directory walk itself; the per-file reads never raise.

    Only the first matching spec is held; a later match is compared with it,
    keeps its path, and its parse is dropped when the loop moves on. The reader
    caps each file, so the parsed-spec memory this scan holds at its peak is a
    constant few capped parses (the first match, the loop's last parse and the
    one being read), not one per same-name file; the paths themselves, one per
    candidate, are what the refusal message and the warning need and are all
    the scan keeps of the rest.

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
    divergent = False
    for path in iter_agent_spec_files(agents_dir):
        spec = _read_agent_spec(path, operation=operation, source=source)
        if isinstance(spec, dict) and spec.get("name") == agent_id:
            if match is None:
                match = spec
            elif not strict_json_equal(spec, match):
                divergent = True
            match_paths.append(path)
    if len(match_paths) > 1:
        # Paths are repr'd: a filename in this user-writable, tool-shared
        # directory is untrusted input, and both texts reach a terminal.
        names = ", ".join(repr(str(path)) for path in match_paths)
        if divergent:
            raise AmbiguousAgentSpecError(
                f"{len(match_paths)} specs declare the name {agent_id!r} with differing "
                f"content: {names}. Which one is live is undefined -- remove or rename one.",
                paths=match_paths,
            )
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


_DirIdentity = tuple[int, int]


_DIR_ID_DEV_ROW = "\0dir-dev"


_DIR_ID_INO_ROW = "\0dir-ino"


_AGENT_DIRECTORY_MAX_ENTRIES = 4096


class ScanUnverifiable(Exception):
    """Raised when a scan scope could not be pinned and enumerated safely.

    Distinct from the ``None`` sentinel :func:`_pinned_scan_dir_fd` yields for a
    resolved-sensitive target: that is a POSITIVE verdict (this directory is
    protected, and the refusal is auditable). This exception means the verdict
    itself could not be reached at all -- the platform cannot pin
    (:func:`kiro_crew.pinned_fs.supports_pinned_walk` is False) or the pinned open
    failed for a reason other than absence. The distinction matters to a caller
    surfacing state to a human: an unverifiable scan must read as "could not be
    checked", never silently as "no agents here" -- collapsing the two reports a
    directory nothing could read as one that holds no agents.
    """


# Named by the path callers import it from: a traceback or a log line renders the
# raised class as ``module.qualname``, so the owner module must not leak into it.
ScanUnverifiable.__module__ = "kiro_crew.agent_discovery"


def _link_chain_refused(spelling: str) -> bool:
    r"""Whether resolving a Windows-local *spelling* could touch an SMB share.

    A lexical UNC gate cannot see a local-looking path whose LINK target -- on
    an ancestor or on the leaf -- is a share. This asks the screen
    :func:`kiro_crew.hooks.validate_file_path` itself runs,
    ``hooks._screen_windows_links``: it reads each link's stored target locally
    (``os.readlink`` connects to no host), refuses an untrusted share or an
    ambiguous root- or drive-relative target, swaps a vetted target in and
    walks the rewritten path again, so a deeper link under a benign one is
    reached. It is called directly rather than through ``validate_file_path`` so
    a caller keeps its own sensitive-path refusal and audit reason. Off Windows
    it never refuses, exactly as :func:`_unc_refused`. Callers use this before
    any resolving or following syscall.
    """
    return _WINDOWS and _screen_windows_links(os.path.abspath(spelling)) is None


class PathPreflightOutcome:
    """Outcome of the shared path preflight sequence.

    Callers interpret these flags per their own return contract:

    * ``unc_denied`` — the raw or resolved spelling names an untrusted UNC share.
    * ``link_chain_denied`` — a local link chain into an untrusted share was found.
    * ``sensitive`` — the resolved path is a sensitive tree.
    * ``expand_failed`` — ``os.path.expanduser`` raised.
    * ``resolve_failed`` — ``os.path.realpath`` raised.
    * ``not_a_dir`` — the resolved path does not exist or is not a directory.
    * ``resolved`` — the final realpath'd spelling (empty when any refusal fired
      before resolution completed).
    * ``denied_path`` — the exact spelling a refusal fired on: the expanded one
      for a link-chain denial, the resolved one for a post-resolution UNC
      denial, the raw one for a raw UNC denial. A caller that audits a refusal
      must name the spelling that was actually refused, because a link chain or
      symlink is refused precisely for pointing somewhere other than what the
      user typed.
    """

    __slots__ = (
        "unc_denied",
        "link_chain_denied",
        "sensitive",
        "expand_failed",
        "resolve_failed",
        "not_a_dir",
        "resolved",
        "denied_path",
    )

    def __init__(
        self,
        *,
        unc_denied: bool = False,
        link_chain_denied: bool = False,
        sensitive: bool = False,
        expand_failed: bool = False,
        resolve_failed: bool = False,
        not_a_dir: bool = False,
        resolved: str = "",
        denied_path: str = "",
    ) -> None:
        self.unc_denied = unc_denied
        self.link_chain_denied = link_chain_denied
        self.sensitive = sensitive
        self.expand_failed = expand_failed
        self.resolve_failed = resolve_failed
        self.not_a_dir = not_a_dir
        self.resolved = resolved
        self.denied_path = denied_path


def _path_preflights(raw: str) -> PathPreflightOutcome:
    """Run the shared UNC / link-chain / sensitive / isdir sequence on *raw*.

    This is the single implementation of the validation sequence both
    ``chat_folders._validate_project_dir`` and
    ``dashboard/agent_admin/roster._resolve_roster_project_path`` need. Each
    caller interprets the outcome per its own return contract (error messages,
    SEL placement, exception wrapping).

    The sequence is:

    1. UNC gate on the raw spelling (before any syscall that could probe it).
    2. ``expanduser`` (``~`` → home).
    3. Link-chain gate on the expanded spelling.
    4. ``realpath`` (resolve symlinks).
    5. UNC gate on the resolved spelling.
    6. Sensitive-path gate.
    7. ``isdir`` existence check.

    Blocking (filesystem I/O), so callers on an async event loop must run it off
    the loop.
    """
    if _unc_refused(raw):
        return PathPreflightOutcome(unc_denied=True, denied_path=raw)
    try:
        expanded = os.path.expanduser(raw)
    except (OSError, ValueError):
        return PathPreflightOutcome(expand_failed=True)
    if _link_chain_refused(expanded):
        return PathPreflightOutcome(link_chain_denied=True, denied_path=expanded)
    try:
        resolved = os.path.realpath(expanded)
    except (OSError, ValueError):
        return PathPreflightOutcome(resolve_failed=True)
    if _unc_refused(resolved):
        return PathPreflightOutcome(unc_denied=True, denied_path=resolved)
    if is_sensitive_path(resolved):
        return PathPreflightOutcome(sensitive=True, resolved=resolved)
    if not os.path.isdir(resolved):
        return PathPreflightOutcome(not_a_dir=True, resolved=resolved)
    return PathPreflightOutcome(resolved=resolved)


def _opened_dir_identity(dir_fd: int | None) -> _DirIdentity | None:
    """Return the held directory's identity, or ``None`` without a usable hold."""
    if dir_fd is None:
        return None
    try:
        opened = os.fstat(dir_fd)
    except OSError:
        return None
    return (opened.st_dev, opened.st_ino)


def _raise_scan_overflow(directory: Path) -> None:
    raise ScanUnverifiable(
        f"agent directory {directory!s} exceeds the "
        f"{_AGENT_DIRECTORY_MAX_ENTRIES}-entry scan cap"
    )


def _bounded_scan_entries(
    directory: Path | str | int,
    entries: Iterable[os.DirEntry[str]],
) -> tuple[list[os.DirEntry[str]], bool]:
    """Retain at most one agent directory's entry cap, for the pinned walk only.

    One-item lookahead distinguishes a directory exactly at the cap from one
    whose roster would be partial. Every caller of the pinned walk refuses that
    partial answer (:func:`_raise_scan_overflow`), so a truncated listing never
    reads as proof that a name is absent. The by-name walk the session-established
    callers use is not capped, so it has no partial answer to give.
    """
    sampled = list(itertools.islice(entries, _AGENT_DIRECTORY_MAX_ENTRIES + 1))
    overflow = len(sampled) > _AGENT_DIRECTORY_MAX_ENTRIES
    if overflow:
        logger.warning(
            "agent directory scan for %s exceeds the %d-entry cap; refusing partial roster",
            directory,
            _AGENT_DIRECTORY_MAX_ENTRIES,
        )
    return sampled[:_AGENT_DIRECTORY_MAX_ENTRIES], overflow


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

    * *unsupported_ok* selects the walk. ``True`` is every PRE-EXISTING caller
      (per-turn resolution, ``spawn_run`` validation, Slack, the config loader):
      none of those reads a query-supplied path, all of them read the session's
      already-established project directory, and `upstream/main` scans that
      directory by plain ``Path.is_dir()`` + ``os.scandir`` on every platform.
      Those callers keep that by-name walk on EVERY platform, with no capability
      gate and no entry cap, plus a sensitivity verdict on the resolved
      directory name. The residual is real and stated rather than reasoned away:
      that walk re-resolves ``.kiro``/``.kiro/agents`` BY NAME, so an ancestor
      swapped for a link between the ``is_dir()`` check and the read is
      followed, exactly as on `upstream/main`.
    * ``False`` is the ``?project_path=`` caller's contract: an HTTP query names
      a directory the caller does not already trust, so it gets the pinned walk
      below. :func:`kiro_crew.pinned_fs.supports_pinned_walk` gates it, and a
      platform that cannot pin REFUSES outright (:class:`ScanUnverifiable`)
      rather than falling back to a by-name walk an ancestor swap could
      redirect -- the same fail-closed contract :mod:`kiro_crew.pinned_fs` states
      for :func:`kiro_crew.pinned_fs.remove_tree_pinned`. The pinned walk caps the
      listing at :data:`_AGENT_DIRECTORY_MAX_ENTRIES` and reports overflow, which
      every caller of this branch turns into :class:`ScanUnverifiable`.
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
    if unsupported_ok:
        # The by-name walk every session-established caller gets, on every
        # platform: `upstream/main` never gated this scan on any capability or
        # capped it, so this is an `is_dir()` + `os.scandir` walk that never
        # raises and never truncates. It adds no refusal beyond the sensitivity
        # check below; the linked-ancestor screen runs on the owner-supplied
        # folder path before any scan, not here. Sensitivity is judged against
        # the RESOLVED name, because there is no held descriptor to judge
        # instead -- the residual is the same check-to-use window
        # `upstream/main` has.
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
                listed = list(scan)
        except OSError:
            yield (), None, False
            return
        yield listed, None, False
        return
    if not supports_pinned_walk():
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
                entries, overflow = _bounded_scan_entries(d, scan)
        except OSError as exc:
            # Enumeration itself failed on the pinned descriptor (e.g. a
            # FUSE/NFS `readdir` EIO) -- distinct from every earlier absence
            # check above, all of which ran BEFORE anything was opened. Only the
            # owner-supplied scan reaches the pinned walk, so silently degrading
            # to an empty tuple would tell that caller "not in this project" when
            # the truth is "could not be read" -- exactly the
            # confident-wrong-answer :class:`ScanUnverifiable` exists to prevent.
            raise ScanUnverifiable(f"could not enumerate {d_str!r}: {exc}") from exc
        # The descriptor travels with the entries so a caller that must ask this
        # directory one more question -- does `<stem>.json` exist beside this
        # `<stem>.md` -- asks it relative to the inode already validated, rather
        # than by a name a swap could redirect. NOT closed here: the caller owns
        # it for the life of the `with` block and closes it via the `finally`
        # below.
        yield entries, fd, overflow
    finally:
        os.close(fd)


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
        if overflow:
            _raise_scan_overflow(d)
        yield entries


def _read_agent_spec_from_descriptor(
    path: Path,
    *,
    operation: str,
    source: str,
    dir_fd: int | None,
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
    if dir_fd is None:
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
    else:
        # This path is only a display and parser label. Opening it by name would
        # discard the directory descriptor that enumerated the entry and reopen
        # a replacement directory after a swap.
        real = path
    try:
        raw = _read_spec_bytes(real, dir_fd=dir_fd, operation=operation, source=source)
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
