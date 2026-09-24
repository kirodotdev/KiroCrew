#!/usr/bin/env python3
"""Code Review Sage — app-local store + self-heal data layout.

This module is the deterministic, token-free backbone of the app. It owns the
on-disk layout under ``<config_dir>/apps/code-review-sage/data/`` (i.e.
``~/.kiro/crew/apps/code-review-sage/data/`` by default) and is safe to run on
every action (idempotent self-heal).

Layout:

    data/
      learnings/
        common/learned-patterns.md          # cross-repo (warm start)
        repos/<host>/<org>/<repo>/
            learned-patterns.md
            checkpoint.json
      results/<change-id>.json               # one result record per change
      reports/index.json                     # latest run pointer (UI reads)
      config.json                            # resolved paths, globs, caps, rule packs

Run ``python3 sage_lib/store.py --ensure`` to create/repair the layout and seed
``config.json`` without overwriting any user edits.
"""
from __future__ import annotations

import argparse
import errno
import json
import os
import re
import shutil
import stat
import tempfile
from pathlib import Path

# Canonical KiroCrew data-root accessor. Imported at module top but kept guarded
# so the store stays importable standalone (outside the KiroCrew runtime) — the
# fallback mirrors ``config_dir()``'s default of ``~/.kiro/crew``, honoring
# ``KIROCREW_HOME`` when it is set.
try:
    from kiro_crew.config.paths import config_dir as _config_dir
except ImportError:  # pragma: no cover - standalone fallback
    _config_dir = None  # type: ignore[assignment]

# Owner-only lockdown, same guard shape as ``config_dir`` above: a raw
# ``os.chmod`` cannot express one on Windows (see ``restrict_to_owner``).
try:
    from kiro_crew.platform_compat import restrict_to_owner as _runtime_restrict
except ImportError:  # pragma: no cover - standalone fallback
    _runtime_restrict = None  # type: ignore[assignment]

# The ancestor chain, in the two halves that need different mechanisms, both
# taken from the runtime rather than reimplemented here. Same guard shape as
# ``config_dir`` above, so the store stays importable standalone; standalone
# then has no confinement, exactly as it has no owner-only lockdown.
#
# ``refuse_linked_parent`` covers a link ALREADY sitting on the chain, which is
# the shape an attacker can set up at leisure. It carries the anchor policy
# (the runtime's own data roots, shallowest match, and a walk up to the first
# existing ancestor for a path outside them all), so a caller-supplied ``root``
# outside the data home is judged rather than silently exempted.
#
# ``pin_parent`` covers a component swapped mid-walk, with one ``openat`` per
# component and ``O_NOFOLLOW`` on each, which no single by-name open can do.
# Neither half subsumes the other: the pin is handed a path it opens by name, so
# it needs the chain it walks to be named lexically below the trust anchor
# (``anchored_parent``) rather than resolved, and the refusal is a stat and so
# loses a race with a planted link.
try:
    from kiro_crew.atomic_write import anchored_parent as _runtime_anchored_parent
    from kiro_crew.atomic_write import refuse_linked_parent as _runtime_refuse_linked
except ImportError:  # pragma: no cover - standalone fallback
    _runtime_anchored_parent = None  # type: ignore[assignment]
    _runtime_refuse_linked = None  # type: ignore[assignment]

try:
    from kiro_crew.pinned_fs import pin_parent as _runtime_pin_parent
    from kiro_crew.pinned_fs import supports_pinned_walk as _runtime_supports_pinned_walk
except ImportError:  # pragma: no cover - standalone fallback
    _runtime_pin_parent = None  # type: ignore[assignment]
    _runtime_supports_pinned_walk = None  # type: ignore[assignment]

APP_NAME = "code-review-sage"


def crew_home() -> Path:
    """Resolve the active KiroCrew data root.

    Delegates to ``kiro_crew.config.paths.config_dir()`` when the runtime is
    importable (so it follows the ``~/.kiro/crew`` root and honors
    ``KIROCREW_HOME`` uniformly, including the one-time legacy-home migration).
    Falls back to a standalone resolution when run outside the KiroCrew package.
    """
    if _config_dir is not None:
        return _config_dir()
    home = os.environ.get("KIROCREW_HOME")
    return Path(home) if home else Path.home() / ".kiro" / "crew"


# Sensitive-path globs feed the deterministic blast-radius extractor. Kept here
# so the single config is the tunable source of truth.
DEFAULT_SENSITIVE_GLOBS: list[str] = [
    "**/auth/**", "**/*auth*", "**/login*", "**/session*", "**/token*",
    "**/*cred*", "**/secret*", "**/*.pem", "**/*.key",
    "**/csp*", "**/cors*", "**/network/**", "**/*proxy*",
    "**/migrations/**", "**/*migration*", "**/schema*", "**/models/**",
    "**/infra/**", "**/*.tf", "**/cdk/**", "**/cloudformation/**",
    "**/gateway*", "**/server.py", "**/lifecycle*", "**/startup*",
]

# Rule-pack pointers: repo-identity -> rule pack skill name/path. Read-only reuse,
# composed at review time. Empty by default (OSS); users can map their own repos.
DEFAULT_RULE_PACKS: dict[str, str] = {}

# Hostnames accepted as GitHub-API-compatible, matched EXACTLY against the
# parsed URL hostname (see ``adapters.allowed_hosts``). GitHub Enterprise Server
# users add their instance here, mirroring ``gh auth login --hostname <host>``;
# ``gh`` must be authenticated for each listed host.
DEFAULT_GITHUB_HOSTS: list[str] = ["github.com"]

DEFAULT_CONFIG: dict[str, object] = {
    "schema": "code-review-sage-config",
    "version": 1,
    # Triage thresholds — tunable *guidance* the report AI weighs.
    "triage": {
        "critical_blast": "LARGE",
        "medium_blast": "MEDIUM",
        "yellow_min_yellow_findings": 2,
    },
    # Learning-store governance caps.
    "caps": {
        "common_max_patterns": 60,
        "repo_max_patterns": 120,
    },
    # Review settings — model, effort, and active namespaces.
    "review": {
        "model": None,         # None = inherit the system/agent default model
        "effort": "",          # "" = inherit the model/provider default effort
        "active_namespaces": ["default"],  # which namespaces to load during review
        "max_concurrent": 5,     # max reviews running at once on the shared runtime
                                 # (clamped to [1, 30]); "review all" can raise it.
        # Publish findings as a PENDING (draft) review on the PR itself.
        # OFF by default: a review is READ here, in the app, and writing to
        # someone else's pull request is a side effect the user has to ask for.
        # Turning it on restores the old behaviour (one pending review per PR,
        # bodies composed deterministically in Python and posted verbatim).
        "auto_post": False,
    },
    "sensitive_globs": DEFAULT_SENSITIVE_GLOBS,
    "rule_packs": DEFAULT_RULE_PACKS,
    # GitHub-compatible hosts (github.com + optional GitHub Enterprise Server).
    "github_hosts": DEFAULT_GITHUB_HOSTS,
    # Settled-change filtering defaults.
    "exclude_settled_by_default": True,
}


def app_root() -> Path:
    """Resolve the installed app root under the KiroCrew home dir.

    Derives from ``crew_home()`` (``config_dir()`` → ``~/.kiro/crew`` by
    default, honoring ``KIROCREW_HOME``); ``crew_home`` keeps a standalone
    fallback so the store stays importable outside the KiroCrew runtime."""
    return crew_home() / "apps" / APP_NAME


def restrict_to_owner(path: str | os.PathLike) -> None:
    """Lock a file this app stages down to its owner, before it takes its name.

    Every record, report and cache written here carries change content, so each
    one is restricted while it is still a private temp file. ``os.chmod(0o600)``
    expresses that only on POSIX: on Windows it toggles the read-only attribute,
    leaves the inherited DACL untouched, and *succeeds* — so the file stays
    readable by every other local account and nothing is raised to notice.
    Delegating to the runtime's helper applies a real owner-only DACL there,
    while the POSIX path stays the same chmod, including the fail-loud
    ``OSError`` the callers' temp-file cleanup relies on.

    The standalone fallback is that bare chmod, matching the guard on
    ``config_dir`` above: outside the Kiro Crew runtime there is no stdlib way to
    set a Windows DACL, and refusing the write would be worse than the POSIX
    behaviour this app has always had.
    """
    if _runtime_restrict is not None:
        _runtime_restrict(path)
        return
    os.chmod(path, 0o600)  # pragma: no cover - standalone fallback


def open_locked_temp(directory: str | os.PathLike) -> tuple[int, str]:
    """Create a temp file in ``directory`` that is ALREADY owner-only, and return
    ``(fd, path)`` with the file still open for writing.

    The lockdown has to happen before the caller writes a byte. ``mkstemp`` gives
    a POSIX file mode ``0600`` from creation, but on Windows access comes from the
    DACL, and a new file simply inherits the directory's -- so restricting only
    after the payload is written leaves a window in which the content is readable
    by everyone the parent grants. Nothing in this app tightens its data
    directories, so that window is real rather than theoretical.

    The returned fd is opened before the DACL changes, and Windows checks access
    at open time, so the caller's write still succeeds. If the lockdown itself
    fails the caller never receives either handle, so this closes the descriptor
    AND removes the temp file here -- the exception escapes before the caller's
    own ``try/finally`` is entered, so nothing downstream would clean either up.
    """
    fd, tmp = tempfile.mkstemp(dir=str(directory), suffix=".tmp")
    try:
        restrict_to_owner(tmp)
    except BaseException:
        os.close(fd)
        try:
            os.unlink(tmp)
        except OSError:  # pragma: no cover - best-effort cleanup
            pass
        raise
    return fd, tmp


#: True when this platform can WALK a chain by descriptor and open relative to
#: it. The runtime's ``supports_pinned_walk()`` is that definition, borrowed
#: rather than written again; the standalone fallback repeats it because the
#: runtime is not importable then.
_CAN_PIN_WALK = (
    _runtime_supports_pinned_walk()
    if _runtime_supports_pinned_walk is not None
    else (
        hasattr(os, "O_DIRECTORY")
        and hasattr(os, "O_NOFOLLOW")
        and os.open in os.supports_dir_fd
    )
)

#: True when this platform can pin a directory and PUBLISH relative to it. The
#: confinement in :func:`atomic_write_locked` needs ``open``, ``unlink`` and the
#: rename family to all accept a directory descriptor, and Windows has none of
#: them, so the capability is resolved once here rather than guessed per call.
#: Probed via ``os.rename``: CPython registers the rename family under that name,
#: so ``os.replace in os.supports_dir_fd`` is False even where the pinned
#: ``os.replace(..., src_dir_fd=, dst_dir_fd=)`` call works.
#:
#: Strictly more than :data:`_CAN_PIN_WALK`: only the two extra verbs a staged
#: replace needs are asked here, so a caller that merely opens relative to a
#: pinned parent -- :func:`open_append_nolink` -- is not degraded by a verb it
#: never calls.
_CAN_PIN_DIR = (
    _CAN_PIN_WALK and os.unlink in os.supports_dir_fd and os.rename in os.supports_dir_fd
)

#: Joined onto a directory so the planted-link refusal judges that DIRECTORY's
#: own chain rather than its parent's. The refusal deliberately exempts the
#: final component, because a rename replaces a link rather than writing through
#: it, so a directory handed in bare would have itself left unjudged. The name is
#: never created; only the components above it are inspected.
_CHAIN_PROBE = ".chain-probe"


def refuse_linked_parents(path: str | os.PathLike) -> None:
    """Refuse *path* when a link sits on the chain of directories above it.

    A no-op outside the Kiro Crew runtime, like the owner-only lockdown: the
    anchor policy lives in the runtime and there is nothing to anchor against
    without it.
    """
    if _runtime_refuse_linked is None:  # pragma: no cover - standalone fallback
        return
    _runtime_refuse_linked(Path(path))


def mkdir_refusing_links(directory: str | os.PathLike) -> Path:
    """Create *directory* and any missing parent, refusing a planted link first.

    ``mkdir(parents=True)`` resolves every component by name and creates THROUGH
    a link it meets, so the tree lands under whatever that link points at and the
    caller sees success. Refusing first is what removes that, and it has to
    happen before the ``mkdir`` rather than after: afterwards the directories
    already exist in the attacker's tree.

    The window between the refusal's ``lstat`` and this ``mkdir`` is still
    winnable by a link planted inside it, and closing it needs a chain-CREATING
    descent (``os.mkdir(..., dir_fd=)`` per component) that the runtime does not
    offer today. The refusal removes the shape that can be set up at leisure,
    which is the one the report is about; :func:`atomic_write_locked`, where the
    bytes actually land, additionally pins the chain and so does not depend on
    this window.
    """
    d = Path(directory)
    refuse_linked_parents(d / _CHAIN_PROBE)
    d.mkdir(parents=True, exist_ok=True)
    return d


class LinkedAncestorRefusal(OSError):
    """Raised when a directory above a record became a link before it was opened.

    An ``OSError`` carrying ``ELOOP``, which is what this condition already
    raised: a single ``os.open(parent, O_DIRECTORY | O_NOFOLLOW)`` reports
    ``ELOOP`` when the parent itself is a link, and a caller that distinguishes
    "cannot write here" from a programming error should not have to learn a
    second exception family now that the whole chain is checked instead of one
    component.
    """

    def __init__(self, message: str) -> None:
        super().__init__(errno.ELOOP, message)


def pin_record_dir(directory: str | os.PathLike) -> int:
    """Open *directory* as a descriptor, refusing a component swapped mid-walk.

    The returned descriptor is open and the caller closes it.

    One ``openat`` with ``O_NOFOLLOW`` per component, borrowed from the runtime
    rather than written again here. A single by-name open cannot do this: the
    flag guards the FINAL component, so every ancestor above it is resolved by
    name and a link there is followed silently and then pinned.

    The walk opens components by name, so which names it gets decides what it can
    refuse. ``resolve()`` is the wrong answer: it follows a link sitting on the
    chain and hands the walk the link's TARGET, which the walk then pins
    faithfully -- an attacker's directory, opened one careful ``O_NOFOLLOW`` at a
    time. ``anchored_parent`` gives the shape that works instead: the trust
    anchor resolved, because a link at or above it is the operator's own layout,
    and every name below it LEXICAL, because that is the stretch Kiro Crew
    creates itself and where a link is somebody's plant. A component swapped
    there fails its own open rather than being followed.

    Outside every owned root there is no anchor to start from and
    ``anchored_parent`` says so; a caller-supplied ``root`` and the tests' temp
    trees are that case. The walk then gets the resolved path, which keeps
    out-of-tree roots working at the cost of following a link already on their
    chain -- the same trade the refusal makes there, and for the same reason: a
    link outside Kiro Crew's own trees is indistinguishable from layout.

    Standalone, with no runtime to borrow the walk from, this is the one by-name
    open it replaces. ``O_CLOEXEC`` is absent from the runtime's flags and not
    needed: descriptors Python opens are non-inheritable already.
    """
    if _runtime_pin_parent is None:  # pragma: no cover - standalone fallback
        cloexec = getattr(os, "O_CLOEXEC", 0)
        return os.open(
            str(directory), os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW | cloexec
        )
    anchored = (
        _runtime_anchored_parent(Path(directory))
        if _runtime_anchored_parent is not None
        else None
    )
    return _runtime_pin_parent(
        anchored if anchored is not None else str(Path(directory).resolve()),
        what="sage record",
        refusal=LinkedAncestorRefusal,
    )


def _write_all(fd: int, data: bytes) -> None:
    """Write every byte of *data* to *fd*, or raise.

    ``os.write`` is the raw syscall: it is permitted to accept fewer bytes than
    it was given, and near a full disk it does. A one-shot call therefore
    publishes a TRUNCATED record, and one caller (``results.adopt_into_run``)
    deletes its source once the publish returns -- so a short write there loses
    the only valid copy. Looping until the buffer is drained is what makes the
    rename a publish of complete content; zero progress is treated as an error
    rather than spun on, since a descriptor that accepts nothing will not start.
    """
    view = memoryview(data)
    while view:
        written = os.write(fd, view)
        if written <= 0:  # pragma: no cover - kernel would have raised first
            raise OSError(errno.EIO, "short write while staging a record")
        view = view[written:]


def atomic_write_locked(path: str | os.PathLike, data: bytes) -> None:
    """Write *data* to *path* atomically, resolving the parent directory ONCE.

    Every writer in this app stages a private temp file beside its target and
    renames over the name, which stops a symlink planted AT that name from being
    followed. What it did not stop is a swapped ANCESTOR: ``mkstemp(dir=...)``
    resolves the parent by name, then ``os.replace(tmp, path)`` resolves it twice
    more, so a reviewer worker -- which runs prompt-injected model output and has
    a shell inside its own run tree -- could swap a directory for a symlink
    between those resolutions and redirect the write outside the sandbox. The
    content is gateway-derived, so the primitive is a fixed-content clobber
    rather than injection, but it lets the sandboxed side make the unsandboxed
    gateway write where the sandbox refused.

    Pinning collapses those three resolutions into one: the parent is opened as a
    descriptor and the create, the rename and any cleanup are all relative to
    that inode, so a swap AFTER the pin cannot redirect them -- it renames a
    directory the descriptor does not name. ``O_NOFOLLOW`` on the pin also
    refuses a parent that is itself a link.

    The ANCESTOR CHAIN is covered in two halves, because neither half covers the
    other. ``mkdir(parents=True)`` and a single ``os.open`` both resolve the full
    path by name and ``O_NOFOLLOW`` guards only the final component, so an
    INTERMEDIATE ancestor that is a link is followed. :func:`refuse_linked_parents`
    refuses a link that is ALREADY on the chain -- the shape that can be planted
    at leisure -- and :func:`pin_record_dir` then walks the chain one
    ``O_NOFOLLOW`` ``openat`` per component, so a component swapped AFTER that
    refusal is refused rather than followed. The pin cannot replace the refusal:
    it starts from the trust anchor and takes the anchor's own resolution on
    trust, which is where a link is the operator's layout and must keep working.
    The refusal cannot replace the pin, because an ``lstat`` loses a race with a
    link planted just after it.

    Inside the owned trees that race is closed here, and the closing is a matter
    of which NAMES the walk is handed: the names below the anchor stay lexical
    (see :func:`pin_record_dir`), so nothing between the refusal and the walk
    re-resolves them and a link planted in that window fails its own open. Two
    cases keep the older, weaker story. A swap at or ABOVE the anchor is followed
    by the anchor's resolution, which is deliberate. And a parent outside every
    owned root has no anchor, so its chain is resolved whole -- caller-supplied
    roots and temp trees, where a link cannot be told from layout anyway.
    Creating the chain is still the weaker step either way: see
    :func:`mkdir_refusing_links`, whose own window needs a chain-CREATING pinned
    descent the runtime does not offer yet.

    Owner-only comes from the ``0o600`` creation mode instead of a follow-up
    ``restrict_to_owner`` call, which would be another resolution by name. On the
    fallback path (Windows: :data:`_CAN_PIN_DIR` is False because none of the
    dir_fd variants exist there) the write itself is exactly today's
    :func:`open_locked_temp` + ``os.replace``, DACL lockdown included -- the
    platform that cannot pin is the platform that keeps the old shape. It is not
    left bare, though: the refusal above runs on every platform, and its ``lstat``
    walk is the only check that sees a Windows JUNCTION at all, which
    ``os.path.islink`` reports as False.
    Callers own creating the parent, and every one of the nine does -- either
    `mkdir(parents=True)` directly (`learning`, `report`, `followup`) or
    `ensure_layout` / `ensure_run_layout` (`discovery`, `results`). This function
    deliberately does NOT create it: an `exist_ok` mkdir here RESURRECTS a
    directory tree that a concurrent namespace deletion is removing, so the
    delete reports success while a stale record survives under a namespace that
    is supposed to be gone. Dropping it also removes the last by-name `mkdir`
    from this path, leaving the pin as the only place the parent is resolved.
    """
    target = Path(path)
    parent = target.parent

    # Before anything resolves the chain, including the pinned walk's own
    # ``resolve``. A link already sitting on it is followed by every resolution,
    # so refusing it is what the pin cannot do for itself. This runs on both
    # branches below, which is the whole of the guard where pinning is
    # unavailable.
    refuse_linked_parents(target)

    if not _CAN_PIN_DIR:  # pragma: no cover - exercised on Windows
        fd, tmp = open_locked_temp(parent)
        try:
            try:
                _write_all(fd, data)
            finally:
                os.close(fd)
            os.replace(tmp, target)
        finally:
            if os.path.exists(tmp):
                os.unlink(tmp)
        return

    cloexec = getattr(os, "O_CLOEXEC", 0)
    dir_fd = pin_record_dir(parent)
    try:
        # Unpredictable, like mkstemp's own name: the reviewer must not be able
        # to pre-plant the temp it is about to be handed. The randomness is the
        # whole of that property, so the name deliberately does NOT embed
        # ``target.name``: a change id built from a long GHE host, owner and repo
        # already approaches NAME_MAX, and adding the target plus a suffix on top
        # pushed the temp component past it -- ENAMETOOLONG, and no record
        # written. Fixed length, whatever the target is called.
        tmp_name = f".sage-{os.urandom(8).hex()}.tmp"
        fd = os.open(
            tmp_name,
            os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW | cloexec,
            0o600,
            dir_fd=dir_fd,
        )
        try:
            try:
                _write_all(fd, data)
            finally:
                os.close(fd)
            os.rename(tmp_name, target.name, src_dir_fd=dir_fd, dst_dir_fd=dir_fd)
            # The pin is what makes a mid-write swap harmless, and the same
            # property means a publish into a directory that has since been
            # renamed AWAY succeeds into the DETACHED inode -- correct bytes, in
            # a place nothing at the caller's path can reach.
            # ``results.adopt_into_run`` unlinks its source once this returns,
            # so reporting success there would delete the only reachable copy.
            # Compare the descriptor against the name after the rename and
            # refuse to claim success on a mismatch.
            #
            # DETECTION, not prevention: the name can change again immediately
            # after this stat. That is fine, because the guarantee being offered
            # is not "the name is stable" but "a caller that must not delete its
            # only copy gets an error rather than a false success". The security
            # property is untouched -- the bytes never went through the swapped
            # name, which is what the pin is for.
            try:
                named = os.stat(str(parent))
            except OSError as exc:
                raise OSError(
                    errno.ESTALE,
                    f"parent of {target.name} vanished mid-publish",
                ) from exc
            pinned = os.fstat(dir_fd)
            if (named.st_dev, named.st_ino) != (pinned.st_dev, pinned.st_ino):
                raise OSError(
                    errno.ESTALE,
                    f"parent of {target.name} was replaced mid-publish",
                )
        except BaseException:
            # Relative to the SAME descriptor, so the cleanup cannot be
            # redirected either.
            try:
                os.unlink(tmp_name, dir_fd=dir_fd)
            except OSError:  # pragma: no cover - best-effort cleanup
                pass
            raise
    finally:
        os.close(dir_fd)


def atomic_write_text(path: str | os.PathLike, text: str) -> None:
    """UTF-8 convenience wrapper over :func:`atomic_write_locked`."""
    atomic_write_locked(path, text.encode("utf-8"))


def _refuse_unsafe_leaf(target: Path) -> None:
    """Refuse an existing *target* that is a link or is not a regular file.

    ``follow_symlinks=False`` is available on every platform Python supports, so
    this leg holds where ``O_NOFOLLOW`` does not. It is what stops an
    attacker-PLANTED name from being written into, which needs no race at all.

    A NAME check only. What it cannot see is an ALIAS: a hardlink to another
    inode is itself a regular file and is not a link, so it satisfies both tests
    here and ``O_NOFOLLOW`` as well. :func:`_refuse_unsafe_fd` is the leg that
    catches that one, on the descriptor rather than the name.
    """
    try:
        existing = os.stat(str(target), follow_symlinks=False)
    except FileNotFoundError:
        return
    if stat.S_ISLNK(existing.st_mode):
        raise LinkedAncestorRefusal(f"refusing to append to {target}: it is a symbolic link")
    if not stat.S_ISREG(existing.st_mode):
        raise LinkedAncestorRefusal(f"refusing to append to {target}: it is not a regular file")


def _refuse_unsafe_fd(fd: int, target: Path) -> None:
    """Refuse an opened *fd* that is not a LONE regular file.

    The link count is the check a name cannot make. A hardlink to a sensitive
    inode is a regular file and is not a symbolic link, so it passes every
    name-based test and ``O_NOFOLLOW`` too, and an append then lands in the
    original file. ``st_nlink == 1`` is what says the name opened is the only one
    for those bytes.

    On the descriptor, so it cannot be raced: the bytes checked are the bytes
    written to, whatever the name was made to mean in between. It runs on every
    platform for the same reason -- it is the check that does not depend on a
    flag the platform may not have. The app's staging lock makes the same two
    tests on its own descriptor, so this is that rule applied to the one other
    path that opens a leaf by name.
    """
    st = os.fstat(fd)
    if not stat.S_ISREG(st.st_mode) or st.st_nlink != 1:
        raise LinkedAncestorRefusal(
            f"refusing to append to {target}: not a lone regular file"
        )


def open_append_nolink(path: str | os.PathLike) -> int:
    """Open *path* for APPENDING with the chain pinned and no name followed.

    For a log that is appended rather than republished. A staged replace would
    have to read the whole log back, add the line and rename a fresh file over
    it, and two appenders racing that sequence each publish a copy missing the
    other's entry, so ``O_APPEND`` keeps each record indivisible and the chain is
    made safe AROUND the append instead of by replacing it.

    Three legs, mirroring the runtime's own no-follow publish, and the middle one
    is the portable half:

    * the ancestor chain goes through :func:`refuse_linked_parents`, which is the
      shape an attacker can stage at leisure and the only check that sees a
      Windows junction;
    * the leaf is ``lstat``-ed by :func:`_refuse_unsafe_leaf` and refused when it
      is a link or not a regular file. The check runs on every platform, but it is
      only welded to the open where ``O_NOFOLLOW`` exists -- see the residual
      below;
    * where the dir_fd verbs exist the leaf is opened RELATIVE to the pinned
      parent, so no ancestor is re-resolved by name between the refusal and the
      open, and a component swapped in that window fails its own open.

    Then the OPENED descriptor is checked by :func:`_refuse_unsafe_fd`, which is
    the only leg that sees a hardlink -- an alias to another inode passes every
    name test above, and on a descriptor the check cannot be raced.

    RESIDUAL, Windows only. Where the dir_fd verbs are absent the open stays by
    name, and ``getattr(os, "O_NOFOLLOW", 0)`` is 0 there, so that open FOLLOWS a
    reparse point at the leaf. What degrades is therefore more than ancestor
    pinning: the leaf refusal above becomes a check-to-open window, and neither
    remaining leg closes it. ``_refuse_unsafe_fd`` cannot, because a followed
    reparse point yields a descriptor on the TARGET, and a regular target with one
    link passes both of its tests. So a reparse point planted in that window sends
    the append into the file it names. :func:`platform_compat.open_file_no_reparse`
    is the mechanism that settles a leaf and opens it in one operation, but it
    opens for READING; there is no write- or append-capable counterpart, and an
    append cannot be served by a read-only descriptor. The returned descriptor is
    open and the caller closes it.
    """
    target = Path(path)
    refuse_linked_parents(target)
    _refuse_unsafe_leaf(target)
    flags = os.O_WRONLY | os.O_CREAT | os.O_APPEND | getattr(os, "O_NOFOLLOW", 0)
    if not _CAN_PIN_WALK:  # pragma: no cover - exercised on Windows
        fd = os.open(str(target), flags, 0o600)
    else:
        dir_fd = pin_record_dir(target.parent)
        try:
            fd = os.open(target.name, flags, 0o600, dir_fd=dir_fd)
        finally:
            os.close(dir_fd)
    try:
        _refuse_unsafe_fd(fd, target)
    except BaseException:
        os.close(fd)
        raise
    return fd


# Optional Kiro Crew redaction. Lives here rather than in `pipeline` because readers
# outside the posting path need it too, and `pipeline` imports `discovery`, so a
# reader in `discovery` cannot import `pipeline` back.
try:                                   # pragma: no cover - import shape
    from kiro_crew.security import redact_credentials, redact_exfiltration_urls
except ImportError:                    # pragma: no cover - standalone fallback
    redact_credentials = redact_exfiltration_urls = None  # type: ignore


def redact_text(text: str) -> str:
    """Scrub credentials + exfiltration URLs from model-written text.

    Applied at every boundary where such text leaves this app -- the code-review
    system it posts to, and the dashboard it renders in. No-op when the Kiro Crew
    redaction lib is not importable (standalone use).
    """
    if redact_exfiltration_urls is None or redact_credentials is None:
        return text
    return redact_credentials(redact_exfiltration_urls(text)[0])[0]


try:                                  # pragma: no cover - import shape
    from kiro_crew import hooks
except Exception:                      # pragma: no cover - standalone fallback
    hooks = None  # type: ignore

# A record of this app's kind is a small JSON document; anything larger is not one.
JSON_MAX_BYTES = 4 * 1024 * 1024


def read_json_nolink(path: Path, within: Path) -> dict | None:
    """Read and parse a JSON object without following a link planted at `path`.

    Every file this app reads lives in a directory a review worker can reach, and
    runs are concurrent: a prompt-injected worker can replace a file with a symlink,
    and a plain `read_text` then dereferences it and hands the caller
    attacker-chosen JSON from anywhere the gateway can read.

    Returns None when the path is missing, is a plant, exceeds `JSON_MAX_BYTES`, or
    does not parse to an object. Schema validation is deliberately NOT done here --
    callers hold different schemas, so per-schema checks belong at the typed callers.
    """
    if hooks is None:  # pragma: no cover - standalone fallback
        try:
            raw: bytes | None = path.read_bytes()
        except OSError:
            return None
    else:
        raw = hooks.safe_read_file_bytes_nolink(
            str(path), str(within), max_bytes=JSON_MAX_BYTES)
    if raw is None:
        return None
    try:
        rec = json.loads(raw)
    except (json.JSONDecodeError, UnicodeDecodeError):
        return None
    return rec if isinstance(rec, dict) else None


def read_text_nolink(path: Path, within: Path, *, max_bytes: int = JSON_MAX_BYTES) -> str | None:
    """Read a text file without following a link planted at `path`.

    The sibling of `read_json_nolink` for the app's non-JSON stores -- the learning
    catalogs are markdown, and they live in the same worker-reachable tree, so they
    need the same guard: a prompt-injected worker can replace one with a symlink to
    `~/.aws/credentials` and a plain `read_text` would dereference it straight into
    review data.

    Returns None when the path is missing, is a plant, exceeds `max_bytes`, or is not
    decodable as UTF-8. Every one of those reads as "no content", which is the same
    thing a caller does with an empty file.
    """
    if hooks is None:  # pragma: no cover - standalone fallback
        try:
            raw: bytes | None = path.read_bytes()
        except OSError:
            return None
    else:
        raw = hooks.safe_read_file_bytes_nolink(
            str(path), str(within), max_bytes=max_bytes)
    if raw is None:
        return None
    try:
        return raw.decode("utf-8")
    except UnicodeDecodeError:
        return None


def data_dir(root: Path | None = None) -> Path:
    return (root or app_root()) / "data"


# --- Per-run scratch ---------------------------------------------------------
# Every review run owns a private subtree, ``data/runs/<run-id>/``, holding its
# result records and its report. Sharing one ``data/results`` dir and one
# ``data/reports`` index across runs forces the backend to serialize whole runs
# (an overlapping run clears the records the other is still writing) and leaves
# only the newest report readable. Per-run isolation is what lets several
# reviews run at once AND keeps each finished report retrievable by run id.
#
# What stays GLOBAL (deliberately, do not move under a run): ``config.json``,
# ``reviewed.json`` (durable cross-run dedup index), and ``learnings/`` (the
# whole point of the learning store is that it outlives any single run).

_UNSAFE_RUN_ID = re.compile(r"[^A-Za-z0-9._-]+")


def safe_run_id(run_id: str) -> str:
    """Sanitize a run id into a single filesystem-safe path segment.

    Run ids are minted server-side (``uuid4().hex[:12]``), but this is the
    boundary where an id becomes a filesystem path, and ids also arrive from URL
    path params on the read endpoints — so sanitize unconditionally rather than
    trusting the caller.

    Two rules, both required:

    * Anything outside ``[A-Za-z0-9._-]`` collapses to ``_``, which makes
      separators (and therefore ``a/b``, ``../../etc``) unrepresentable.
    * A segment made ENTIRELY of dots is rejected outright. ``.`` and ``..`` are
      built from otherwise-safe characters, so the character filter alone lets
      them through untouched — and ``run_dir("..")`` would then resolve to the
      shared ``data/`` tree one level ABOVE ``runs/``. The dot rule is what
      actually closes containment.
    """
    stem = _UNSAFE_RUN_ID.sub("_", str(run_id)).strip("_")
    if not stem or set(stem) == {"."}:
        return "unknown"
    return stem


def runs_root(root: Path | None = None) -> Path:
    return data_dir(root) / "runs"


def run_dir(run_id: str, root: Path | None = None) -> Path:
    """The private subtree for one run. Always inside ``data/runs/``."""
    return runs_root(root) / safe_run_id(run_id)


def ensure_run_layout(run_id: str, root: Path | None = None) -> dict[str, str]:
    """Create one run's private ``results/`` + ``report/`` dirs. Idempotent.

    Each directory is judged on its OWN chain rather than trusting the one above
    it: the refusal walks every component up to the trust anchor, so a link
    planted at ``report`` is caught even though ``runs`` was just cleared.
    """
    rd = run_dir(run_id, root)
    results = rd / "results"
    reports = rd / "report"
    for d in (rd, results, reports):
        mkdir_refusing_links(d)
    return {"runDir": str(rd), "resultsDir": str(results), "reportDir": str(reports)}


def remove_run_dir(run_id: str, root: Path | None = None) -> bool:
    """Delete one run's private subtree (called when a run is dismissed or ages
    out of the registry). Returns True when something was removed. Never raises
    — a run dir that is already gone, or unremovable, must not fail the caller."""
    rd = run_dir(run_id, root)
    # Containment assertion: rd is built from safe_run_id so it cannot escape,
    # but this is a recursive delete — verify the parent before removing.
    try:
        if rd.resolve().parent != runs_root(root).resolve():
            return False
    except OSError:  # pragma: no cover - defensive
        return False
    if not rd.exists():
        return False
    try:
        shutil.rmtree(rd)
        return True
    except OSError:  # pragma: no cover - defensive
        return False


def list_run_ids(root: Path | None = None) -> list[str]:
    """Run ids that currently have an on-disk subtree (the input for orphan reaping)."""
    rr = runs_root(root)
    if not rr.is_dir():
        return []
    return sorted(p.name for p in rr.iterdir() if p.is_dir())


def ensure_layout(root: Path | None = None) -> dict[str, str]:
    """Create the full data layout if missing. Idempotent — never clobbers.

    Returns a dict of the key resolved paths (consumed by config.json + the UI).
    """
    data = data_dir(root)
    learnings = data / "learnings"
    common = learnings / "common"   # learned-patterns.md (canonical) + .candidate.md (staging)
    repos = learnings / "repos"            # reserved for per-repo learning files
    namespaces = learnings / "namespaces"  # user-created namespaces
    results = data / "results"
    reports = data / "reports"
    runs = data / "runs"          # per-run scratch: runs/<run-id>/{results,report}
    # Scratch the review/consolidation WORKER writes into before handing a path to
    # a `learning.py` subcommand. It exists so the skills can name a temp path that
    # is real on every platform: a POSIX literal like `/tmp/pattern.json` resolves
    # on Windows to a `\tmp\` directory that normally does not exist, so the
    # worker's write raises FileNotFoundError and the staged learning is lost
    # silently. Inside the app root, so it is also covered by the app's own
    # owner-only lockdown rather than living in a world-writable directory.
    tmp = data / "tmp"

    for d in (data, learnings, common, repos, namespaces, results, reports, runs, tmp):
        mkdir_refusing_links(d)

    # Warm-start common layer (empty but present so brand-new repos inherit it).
    common_patterns = common / "learned-patterns.md"
    if not common_patterns.exists():
        atomic_write_text(
            common_patterns,
            "# Common learned patterns (cross-repo, warm start)\n\n"
            "<!-- Promoted from per-repo layers via human-approved generalization. -->\n",
        )

    # Reports pointer the UI polls.
    index = reports / "index.json"
    if not index.exists():
        atomic_write_text(
            index,
            json.dumps({"report_slug": None, "bands": {"red": 0, "yellow": 0, "green": 0},
                        "generated_at": None}, indent=2),
        )

    _seed_config(data)

    return {
        "dataDir": str(data),
        "learningsCommon": str(common_patterns),
        "reposDir": str(repos),
        "resultsDir": str(results),
        "reportsIndex": str(index),
        "tmpDir": str(tmp),
        "configPath": str(data / "config.json"),
    }


def _seed_config(data: Path) -> None:
    """Write config.json once, merging in any missing top-level keys on upgrade."""
    cfg_path = data / "config.json"
    if not cfg_path.exists():
        cfg = dict(DEFAULT_CONFIG)
        cfg["resolved_paths"] = {
            "results": str(data / "results"),
            "reports": str(data / "reports"),
            "learnings": str(data / "learnings"),
        }
        atomic_write_text(cfg_path, json.dumps(cfg, indent=2))
        return

    # Upgrade path: add any new default keys without overwriting user edits.
    #
    # Through `read_json_nolink`, for the same reason `read_config_quiet` reads it
    # that way: `config.json` sits in the worker-reachable data dir, so a plain
    # `read_text` dereferences a link a review worker plants there. That read is
    # the dangerous half of this function, because the merged document is
    # published back under this name -- the publish replaces the link, so a
    # dereferenced read would copy a foreign document's bytes into a real file
    # that `load_config` then serves as configuration. A refusal returns None and
    # leaves the file untouched, which is what an unparsable file already does.
    existing = read_json_nolink(cfg_path, data)
    if existing is None:
        return
    changed = False
    for key, val in DEFAULT_CONFIG.items():
        if key not in existing:
            existing[key] = val
            changed = True
    # Always ensure resolved_paths exists (not in DEFAULT_CONFIG but required by UI).
    if "resolved_paths" not in existing:
        existing["resolved_paths"] = {
            "results": str(data / "results"),
            "reports": str(data / "reports"),
            "learnings": str(data / "learnings"),
        }
        changed = True
    if changed:
        atomic_write_text(cfg_path, json.dumps(existing, indent=2))


def load_config(root: Path | None = None) -> dict:
    cfg_path = data_dir(root) / "config.json"
    if not cfg_path.exists():
        ensure_layout(root)
    return json.loads((data_dir(root) / "config.json").read_text(encoding="utf-8"))


def read_config_quiet(root: Path | None = None) -> dict:
    """Best-effort ``config.json`` read with NO side effects.

    Returns ``{}`` when the file is missing, unreadable, not a JSON object, or
    refused by the no-follow gate — unlike ``load_config`` it never creates the
    data layout, so pure parsers (e.g. the adapters' host allowlist) can
    consult configuration without writing to the user's data dir. The read
    goes through ``read_json_nolink`` because ``config.json`` sits in the
    worker-reachable data dir: a planted symlink must be refused, never
    dereferenced. Callers treat ``{}`` as "no configuration" and fall back to
    their defaults (``allowed_hosts`` -> ``DEFAULT_GITHUB_HOSTS``), so a
    refusal can only ever narrow behaviour to the github.com default."""
    return read_json_nolink(data_dir(root) / "config.json", data_dir(root)) or {}


def _main() -> int:
    ap = argparse.ArgumentParser(description="Code Review Sage store / self-heal")
    ap.add_argument("--ensure", action="store_true",
                    help="Create/repair the data layout and seed config.json")
    ap.add_argument("--print-config", action="store_true",
                    help="Print the resolved config.json")
    ap.add_argument("--root", default=None,
                    help="Override app root (testing); defaults to the installed path")
    args = ap.parse_args()

    root = Path(args.root) if args.root else None

    if args.ensure or not args.print_config:
        paths = ensure_layout(root)
        print(json.dumps({"ok": True, "paths": paths}, indent=2))
    if args.print_config:
        print(json.dumps(load_config(root), indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(_main())
