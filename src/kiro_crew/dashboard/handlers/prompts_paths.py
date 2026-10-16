"""Where prompts live on disk, and how a prompt directory is resolved and pinned.

The user and project prompt roots, a linked root's refusal, the directory a write
is pinned to, the platform capabilities the pinned create relies on, and the root
a read must stay inside.

Served through :mod:`kiro_crew.dashboard.handlers.prompts`, which callers and tests import and patch; this module
never imports it.
"""

from __future__ import annotations

import errno
import os
from pathlib import Path
from typing import Any

from kiro_crew import pinned_fs
from kiro_crew.platform_compat import is_link_or_junction


def _prompt_read_root(entry: dict[str, Any], project_dir: Path | None) -> Path | None:
    """The PINNED prompt root an *entry*'s read must be confined to, or ``None``.

    Handed to ``safe_read_file_bytes_nolink`` as ``within_root``, which requires
    the OPENED descriptor's real path to resolve inside it. That closes what
    ``O_NOFOLLOW`` cannot: the flag guards only the final component, so an
    ANCESTOR directory swapped for a link would redirect the read out of the
    prompt tree with the leaf still looking ordinary.

    Answered only when the entry POSITIVELY names one of the two user scopes, and
    then by re-running that scope's OWN root gate rather than by assembling
    ``<project>/.kiro/prompts`` here. What that buys is the window from the MINT to
    the read: the entry was approved against a root the mint validated, and without
    this the read would confine itself to a root nothing had checked since — so a
    root swapped for a link any time after the listing or the exact-name lookup
    would have the gate ``realpath`` its way into the link's destination on both
    sides of the containment comparison, and an outside file carrying the matched
    prompt's own name would be what the ``@mention`` injects or the unscoped read
    returns in full.

    ``None`` therefore means REFUSE, not "unconstrained": for a user-scope entry it
    is the answer when the root is not serveable at all, and a read that
    fell back to the canonical path's parent there would be pinned inside exactly
    the directory the swap named. :func:`_prompt_read_within_root` is what turns
    that into a refusal, and is what every reader calls; a package SOP's fallback
    lives there too, so the two answers cannot drift apart.

    KNOWN RESIDUAL, stated rather than implied: ``within_root`` is a PATH, and
    ``safe_read_file_bytes_nolink`` ``realpath``s it at read time, so a root
    swapped between this derivation and that ``realpath`` is still followed. Two
    adjacent syscalls rather than a whole scan, but not closed. Closing it needs
    the read to happen RELATIVE to a held directory descriptor
    (``pinned_fs.open_dir_pinned``, which the write verbs already use), which is a
    ``safe_read_file_bytes_nolink`` contract change shared with its other ~40
    consumers — including the scoped read and the package-SOP description read on
    this same surface, which carry the identical residual — plus a decision about
    the name-based fallback on Windows, where ``_DIR_FD_SUPPORTED`` is False. It
    belongs in one change that moves every reader, not in three call sites.

    Shared by every reader of a prompt entry — the chat ``@mention`` expansion and
    both HTTP detail branches — so no two of them can disagree about which root
    pins a given entry.
    """
    if entry.get("package"):
        return None
    source = entry.get("source")
    if source == "local":
        roots = _local_prompt_scan_root(project_dir)
        return roots[1] if roots is not None else None
    if source == "global":
        # Resolved, like the local root, but NOT gated: ``~/.kiro/prompts`` is a
        # location the operator chose rather than one a checkout can name, and the
        # listing walks a symlinked one on purpose (the documented asymmetry), so
        # the resolution follows that link and its destination is the root.
        try:
            return (Path.home() / ".kiro" / "prompts").resolve()
        except (OSError, RuntimeError):
            return None
    return None


def _prompt_read_within_root(
    entry: dict[str, Any], project_dir: Path | None, canonical: str
) -> str | None:
    """``within_root`` for a read of *entry*, or ``None`` to REFUSE the read.

    One derivation for every reader, because a root derived per reader is how two
    readers of one directory drift apart. Three cases, and the middle one is why
    this is a function rather than an expression at each call site:

    * an entry naming the ``local`` or ``global`` scope is pinned inside the
      resolved root :func:`_prompt_read_root` answers;
    * the same entry with NO serveable root is refused. Falling back to
      *canonical*'s own parent would pin the read inside whatever directory a
      swapped root now names, which is the leak the pin exists to close;
    * anything else — a package SOP, or an unfamiliar producer's entry shape — gets
      *canonical*'s own parent. Not a weaker authorization but the only one
      available: those roots are plural and come from the platform seam, so no
      single authorizing directory exists to name the way ``?scope=`` names one.
      It is still passed, because it is what carries the guarantee onto Windows,
      where ``O_NOFOLLOW`` does not exist at all and the gate's ``getattr`` for it
      yields 0 — there the open FOLLOWS a leaf swapped for a link after
      canonicalization, and only the fd-real-path check
      (``GetFinalPathNameByHandleW``) still sees that the inode opened is not the
      one resolved. A link the entry legitimately points at is already followed by
      the canonicalization, so its target's own directory IS that root and only a
      substitution landing after it escapes.
    """
    if entry.get("package") or entry.get("source") not in ("local", "global"):
        return os.path.dirname(canonical)
    root = _prompt_read_root(entry, project_dir)
    return str(root) if root is not None else None


#: Windows errors that name a too-long path: ``ERROR_FILENAME_EXCED_RANGE`` (206),
#: which CPython files under ``errno.ENOENT``, and ``ERROR_BUFFER_OVERFLOW`` (111),
#: which it files under ``EINVAL`` -- so ``errno`` alone names neither.
_WINERROR_FILENAME_EXCED_RANGE = 206
_WINERROR_BUFFER_OVERFLOW = 111
_WINERRORS_TOO_LONG = frozenset({_WINERROR_FILENAME_EXCED_RANGE, _WINERROR_BUFFER_OVERFLOW})
#: Module-level so a test can take the Windows by-name branch on any host.
_IS_WINDOWS = os.name == "nt"


def _is_path_too_long_error(exc: BaseException) -> bool:
    """True when *exc* is the filesystem refusing a path for its LENGTH.

    ``MAX_PROMPT_NAME_BYTES`` bounds the NAME, but the create is handed the name
    joined onto its root, and whether that whole string is accepted is a property
    of the HOST, not of the string: a Windows host without long-path support caps
    it at ``MAX_PATH`` while a host with ``LongPathsEnabled`` takes the same path
    without complaint. No pre-flight length can be right on both, so neither
    create guesses; each performs the write and maps the filesystem's own refusal
    onto the coded 400 the name bound already returns. The prompts create and the
    skills create classify through this one predicate so the two cannot drift.
    Every other ``OSError`` keeps its current handling -- including a code that
    MIGHT mean the length but names something else too, which is left to say what
    it says rather than re-read.

    POSIX reports ``ENAMETOOLONG``; Windows reports one of the codes above.
    """
    if not isinstance(exc, OSError):
        return False
    if exc.errno == errno.ENAMETOOLONG:
        return True
    return getattr(exc, "winerror", None) in _WINERRORS_TOO_LONG


def _linked_prompt_root(d: Path) -> bool:
    """True when the prompt directory itself is a link, so writes would land
    outside the tree the caller named.

    Confinement compares the target against the RESOLVED scope directory, which
    is the right test for an entry inside that directory but says nothing about
    the directory itself: if ``~/.kiro/prompts`` is a symlink, both sides resolve
    into the link's destination and every path looks confined.

    Delegates to ``platform_compat.is_link_or_junction`` rather than testing
    ``is_symlink()`` here: ``islink`` is FALSE for a Windows directory junction,
    and that helper already carries the reparse-tag fallback for interpreters
    without ``os.path.isjunction``. A local probe would have to re-derive that,
    and ``stat.IO_REPARSE_TAG_MOUNT_POINT`` is not exported off Windows, so a
    hand-rolled version fails open on exactly the platform it is meant to cover.

    Deliberately only the leaf: an ANCESTOR symlink is normal (``/home/<user>``
    is itself a link on many hosts) and redirects nothing the user did not
    already choose, so testing ``resolve() != absolute()`` would refuse every
    write on those machines.
    """
    try:
        return is_link_or_junction(d)
    except OSError:
        return True  # unreadable root: refuse rather than guess


#: True when this platform can perform an operation RELATIVE to an open
#: directory descriptor. POSIX has ``openat``/``unlinkat`` behind these; Windows
#: has neither ``O_DIRECTORY`` nor ``dir_fd`` support, so there the by-name
#: operation plus the junction check on the root is all that is available.
#: ``supports_pinned_walk`` covers the openat capability itself; unlink/mkdir
#: are probed separately because delete removes and create mkdirs relative to
#: the pinned descriptor, and the Windows-simulation tests clear capabilities
#: selectively.
_DIR_FD_SUPPORTED = pinned_fs.supports_pinned_walk() and {os.unlink, os.mkdir}.issubset(
    os.supports_dir_fd
)

#: True when this interpreter can build a file with NO NAME at all -- Linux's
#: ``O_TMPFILE`` -- and hand it a name with ``linkat``. That is what lets create
#: publish a finished prompt without the body ever being reachable under a name,
#: and without the published inode passing through a two-link state.
#:
#: Deliberately only ATTRIBUTE lookups: this module is imported on the gateway
#: boot path, where every statement is paid on every launch before the socket
#: accepts requests. The remaining half of the capability -- whether
#: ``/proc/self/fd`` is actually mounted, which ``linkat``'s unprivileged form
#: needs -- is a filesystem question, so it is asked inside the write job in the
#: executor rather than here. A mount that refuses ``O_TMPFILE`` (NFS, some
#: overlayfs) cannot be probed at all: the capability is per-filesystem, so the
#: open is attempted and its refusal handled where it happens.
_UNNAMED_CREATE_SUPPORTED = bool(getattr(os, "O_TMPFILE", 0)) and os.link in os.supports_dir_fd


def _pin_prompt_dir(d: Path, *, create: bool = False) -> int:
    """Return a descriptor pinning the prompt directory *d*, via ``pinned_fs``.

    This is what closes the check-to-use window the by-name paths leave open.
    ``_resolve_prompt_dir`` validates a PATH; an operation that then names
    ``<dir>/<stem>.md`` re-resolves every component afresh, so replacing a
    directory in between makes it land somewhere the check never saw. Operations
    performed relative to this descriptor reach the inode the walk pinned, no
    matter what the names mean by then.

    The mechanism — the openat-per-component walk, the ``ELOOP``/``ENOTDIR``
    translation, the capability probe — lives in :mod:`kiro_crew.pinned_fs`,
    whose charter (two closed PRs' worth of call-site rewrites) is that callers
    stay thin consumers of one set of invariants. This wrapper only supplies
    this handler's policy:

    * Which links are REFUSED deliberately matches ``_linked_prompt_root``
      rather than being stricter. An already-existing ancestor link is a
      location the user chose — ``/home/<user>`` is a link on many hosts and
      dotfile managers routinely symlink ``~/.kiro`` — and ``pinned_fs``
      tolerates it the documented way: the parent chain is realpathed ONCE
      before the walk, so a pre-existing link is followed by that resolution
      and only a component swapped after it is refused. The leaf refuses a
      link outright (``O_NOFOLLOW``), which is ``_linked_prompt_root``'s rule.
    * With *create*, the parents are ensured BY NAME first — the module's
      contract ("callers create their own tree roots"), and the same policy as
      the read path for pre-existing links — and only the leaf is created
      through its pinned parent.

    Raises :class:`pinned_fs.PinnedPathRefusal` for a linked or non-directory
    component (callers map it to ``linked_prompt_root``), and plain ``OSError``
    for operational failures, which reach the generic write-failure path.
    """
    if create:
        d.parent.mkdir(parents=True, exist_ok=True)
        return pinned_fs.create_and_open_dir_pinned(d, what="prompt directory")
    return pinned_fs.open_dir_pinned(d, what="prompt directory")


def _resolve_prompt_dir(scope: str, project_dir: Path | None) -> tuple[Path | None, str | None]:
    """Resolve and validate the prompt directory for *scope*, OFF the loop.

    Returns ``(dir, None)`` or ``(None, error_code)``.

    ``project_dir`` is the caller's local project (or ``None``), resolved on the
    event loop by ``_prompt_local_project`` and passed in — this executor job
    does not resolve it itself, which is what keeps create and list agreeing on
    where "local" is for one request. A ``None`` project for a "local" scope
    yields ``no_active_project``, the same code a "local" scope carries whenever
    no project can be named.

    The remaining half still touches the filesystem: the link check ``lstat``s
    the directory. On a network-mounted home or project that is a multi-second
    stall, so every caller runs this inside its own executor job rather than on
    the event loop — the gateway serves other requests and the heartbeat keeps
    ticking meanwhile.
    """
    d = _user_prompt_dir(scope, project_dir)
    if d is None:
        return None, "no_active_project"
    if _linked_prompt_root(d):
        return None, "linked_prompt_root"
    if scope == "local" and _local_prompt_dir_in_project(d, project_dir) is None:
        return None, "linked_prompt_root"
    return d, None


def _local_prompt_scan_root(project_dir: Path | None) -> tuple[Path, Path] | None:
    """``(as_addressed, pinned)`` for the local prompt root, or ``None`` to refuse it.

    ``_resolve_prompt_dir`` answers WHETHER a root may be served; this answers
    which INODE that permission was granted for, and the two are different
    questions. Re-resolving the caller-addressed root at every containment decision
    downstream — ``_prompt_dir_entry``'s parent comparison and the ``within_root``
    its description read is pinned inside — would let a root swapped for a link
    after validation resolve into the link's destination on BOTH sides of every
    later comparison, so every file under the directory the swap named would look
    confined. Resolving once here and comparing against that fixed value refuses
    them instead:

    * a swap landing BEFORE this resolve makes the pinned value escape the
      project, which the containment gate below catches;
    * a swap landing AFTER it leaves every entry's resolved parent unequal to the
      pinned root, so the scan and the exact-name lookup both mint nothing.

    So the local library goes EMPTY under an active swap rather than publishing a
    foreign directory — the same outcome a statically redirected root already
    produces, and the same one every serving verb answers ``linked_prompt_root``
    for. Pinning a descriptor instead would additionally keep the honest entries
    listed through the swap, but ``dir_fd`` enumeration does not exist on Windows
    (``_DIR_FD_SUPPORTED``), so it would buy availability on one platform only
    while the refusal above is what the security property needs.

    The as-addressed root is returned alongside because enumeration must still
    walk the name the caller addressed: ``_prompt_dir_entry`` reports that
    spelling, which is what lets ``api_prompts`` fold ``$HOME`` to ``~`` and what
    the write verbs address the same file by.
    """
    d, err = _resolve_prompt_dir("local", project_dir)
    if d is None or err is not None:
        return None
    pinned = _local_prompt_dir_in_project(d, project_dir)
    if pinned is None:
        return None
    return d, pinned


def _local_prompt_dir_in_project(d: Path, project_dir: Path | None) -> Path | None:
    """The local prompt dir RESOLVED, when it resolves inside the resolved project.

    Returns the resolved directory so the caller can PIN it, or ``None`` when the
    chain leaves the project. Answering with the resolved value rather than a
    bool is what lets :func:`_local_prompt_scan_root` hand every downstream
    containment check the very inode this gate approved instead of re-deriving it
    from a name that may since mean something else.

    The leaf-only rule in ``_linked_prompt_root`` tolerates ancestor links
    because a link under the user's own tree is a location the user chose. A
    project ``.kiro`` is authored by the REPOSITORY, not the user: a checkout
    shipping ``.kiro -> ~/.kiro`` would silently redirect local-scope writes
    and deletes into the global prompt tree. Comparing resolved-to-resolved
    keeps legitimately-linked project roots working while refusing any chain
    that leaves the project.

    ``project_dir`` is the caller's local project (or ``None``), supplied by the
    caller rather than read from a process-wide resolver — so the containment
    check is made against the very root the write was addressed to.

    ``RuntimeError`` is caught alongside ``OSError`` because ``Path.resolve()``
    signals a symlink LOOP that way and ``RuntimeError`` is not an ``OSError``.
    An ANCESTOR loop reaches here undetected: ``_linked_prompt_root``'s
    ``is_link_or_junction`` is ``os.path.islink``, which swallows the ``ELOOP``
    and answers False, so a checkout shipping ``.kiro -> .kiro`` arrives at this
    resolve. Both enumerating callers of this gate — the listing scan and the
    exact-name lookup — run it outside any broad handler catch, so letting the
    loop escape takes ``GET /api/prompts`` and the unscoped detail lookup down
    with a 500 instead of costing one local library.
    A loop names no directory inside the project, so it is refused like any other
    escaping chain.
    """
    proj = project_dir
    if not proj:
        return None
    try:
        resolved = d.resolve()
        return resolved if resolved.is_relative_to(Path(proj).resolve()) else None
    except (OSError, RuntimeError):
        return None


def _user_prompt_dir(scope: str, project_dir: Path | None) -> Path | None:
    """Resolve the user prompt directory for *scope*.

    "local" resolves against the project supplied by the caller
    (``project_dir``), which ``_prompt_local_project`` resolved on the event loop
    from the request's own slot context — not from the process-wide
    ``KIROCREW_PROJECT_DIR``, which names the Kiro Crew tree on a source install
    and nothing at all on a wheel. ``_list_aim_prompts`` is handed the SAME
    resolved project, so create and list agree on where "local" is: a created
    local prompt appears in the listing that same request gets, and never in a
    listing for a different project. ``None`` resolves "local" to ``None``, which
    flows into the existing ``no_active_project`` contract.
    """
    if scope == "global":
        return Path.home() / ".kiro" / "prompts"
    proj = project_dir
    return proj / ".kiro" / "prompts" if proj else None


def _plain_stem_ok(stem: str) -> bool:
    """True when *stem* is a plain path component that cannot leave its
    directory — no traversal, no separator, no dotfile.

    Shared by the pending-skill slug check and the prompt-name check: both need
    exactly this predicate, and two spellings of it would drift.
    """
    return (
        bool(stem)
        and stem not in (".", "..")
        and not stem.startswith(".")
        and "/" not in stem
        and "\\" not in stem
        and ".." not in stem
    )
