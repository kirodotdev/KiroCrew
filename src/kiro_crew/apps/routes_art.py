"""Reading an app's declared art, and what the blob proxy shares with the registry routes.

``_declared_art_paths`` and ``_read_declared_art`` read the manifest-declared art the
installed-art route serves. The repository and ref validators (``_is_safe_repo_identifier``,
``_SAFE_REF_RE``), the fetch timeout and concurrency bound, and the blob-cache touch serve
the git blob proxy; the art route and the blob proxy themselves stay in
``apps/routes.py``.

Part of the ``kiro_crew.apps.routes`` family: callers and tests reach every name here
as ``kiro_crew.apps.routes.<name>``, and a patch there reaches this module too
(:mod:`kiro_crew._facade_mirror`). This module never imports the facade.
"""

from __future__ import annotations

import asyncio
import os
import re
import stat
from pathlib import Path
from typing import Any

from kiro_crew.apps.manager import apps_dir, get_app_manifest
from kiro_crew.apps.registry import _ART_MANIFEST_FIELDS, _ART_MANIFEST_LIST_FIELDS, _ART_MAX_BYTES
from kiro_crew.pinned_fs import (
    PinnedPathRefusal,
    is_reparse_point,
    open_in_pinned_parent,
    supports_pinned_walk,
)

#: Store-art field names, image extension allowlist and size ceiling are ONE set
#: for all three art readers -- the installed-app route and the blob proxy in apps/routes.py, and
#: the registry's owner-tier prewarm (``registry_pipeline.store_art``, which owns
#: them). The parity is load-bearing: a file one reader serves and another refuses
#: means the same app's art renders or 403s depending only on whether it happens to
#: be installed, or on which path its bytes arrived through.
#:
#: The field tuple is deliberately NOT a path filter rooted at the install
#: directory. That directory is the app's whole checkout and ``_ALLOWED_EXTENSIONS``
#: admits ``.json``, so a filter would also serve ``installed.json``, ``app.json``
#: and every other JSON in the tree -- a widening nobody asked for to display an icon.


def _declared_art_paths(name: str) -> set[str]:
    """The art paths *name*'s own installed manifest declares.

    Blocking (reads the manifest off disk) — callers must offload it.
    """
    manifest = get_app_manifest(name)
    if manifest is None:
        return set()
    extra = getattr(manifest, "extra", None)
    fields: dict[str, Any] = extra if isinstance(extra, dict) else {}
    declared: set[str] = set()
    for key in _ART_MANIFEST_FIELDS:
        value = fields.get(key)
        if isinstance(value, str) and value:
            declared.add(value[2:] if value.startswith("./") else value)
    for key in _ART_MANIFEST_LIST_FIELDS:
        values = fields.get(key)
        if not isinstance(values, list):
            continue
        for value in values:
            if isinstance(value, str) and value:
                declared.add(value[2:] if value.startswith("./") else value)
    return declared


def _read_declared_art(name: str, file_path: str) -> tuple[bytes, str] | None:
    """The BYTES of *file_path* and a validator for them, or None to refuse.

    Returning bytes rather than a path is the security-relevant part. Validating a
    path and then handing it to ``FileResponse`` opens it a SECOND time, so the app
    that owns this directory can swap a declared name for a symlink between the
    check and that open and have the gateway read the target instead — and the
    gateway is NOT sandboxed, so this would launder a read the app's own code can be
    refused. Checking a path and acting on a re-resolution of it is worse than no
    check, because it reports success.

    One open, validated as a DESCRIPTOR, is the only shape without that window.
    :func:`open_in_pinned_parent` does one ``openat`` per component carrying
    ``O_NOFOLLOW``, so a component swapped for a link after the parent was resolved
    fails instead of being followed, and the final name is refused if it is a link
    at all. Everything after that reads the descriptor, which cannot be re-pointed.

    One thread hop for the whole decision — manifest read, declaration check, open,
    stat and read — because every part is a blocking syscall and the gateway runs on
    one event loop (``no-blocking-call-on-event-loop``).
    """
    if file_path not in _declared_art_paths(name):
        return None
    root = apps_dir() / name
    target = root / file_path
    try:
        # Resolved ONCE, here, because `pin_parent` requires the CALLER to do it:
        # resolving inside would re-follow whatever an ancestor points at by then,
        # which is the mistake that makes a guard look defensible and do nothing.
        resolved_root = Path(os.path.realpath(root))
        resolved_parent = Path(os.path.realpath(target.parent))
        resolved_parent.relative_to(resolved_root)
    except (OSError, RuntimeError, ValueError):
        # `RuntimeError` because `Path`/`realpath` resolution raises THAT -- not an
        # OSError -- on a symlink loop, and the app that plants one is the app whose
        # art this serves. All three mean the path is not servable, which is the same
        # answer as undeclared and missing.
        return None

    # `O_NONBLOCK` is not an optimisation, it is what makes the descriptor checks
    # REACHABLE. Opening a FIFO blocks until a writer appears, and this runs inside
    # `asyncio.to_thread` -- so an app declaring a FIFO as its icon path parks a
    # thread-pool worker forever, and enough such requests starve every other
    # blocking call in the gateway. Measured: the open hangs indefinitely without
    # this flag, returns immediately with it, and then `S_ISREG` below refuses the
    # FIFO. On an ordinary file it changes nothing -- the read returns the same bytes.
    flags = (
        os.O_RDONLY
        | getattr(os, "O_NOFOLLOW", 0)
        | getattr(os, "O_NONBLOCK", 0)
        | getattr(os, "O_BINARY", 0)
    )
    if supports_pinned_walk():
        try:
            fd = open_in_pinned_parent(
                str(resolved_parent),
                Path(file_path).name,
                flags=flags,
                mode=0o600,
                what="app art file",
            )
        except (PinnedPathRefusal, OSError, ValueError):
            # `ValueError` is NOT redundant with `OSError`: `os.open` raises it --
            # never an OSError -- for a name the OS layer cannot even encode. Two
            # reachable classes, both measured: an embedded NUL raises `ValueError`
            # ("embedded null character in path"), and a lone surrogate raises
            # `UnicodeEncodeError`, which is a ValueError SUBCLASS. Screening NUL at
            # the door would therefore be INCOMPLETE -- the surrogates carry no NUL.
            #
            # Reachable because such a name survives every earlier check: the
            # extension allowlist reads the suffix AFTER the bad byte
            # (`bad\x00.png` -> `.png`), and containment resolves the PARENT, which
            # is clean when the bad byte sits in the final component. So the first
            # thing that touches it is this open, and uncaught it is a 500 on a
            # route whose every other refusal is a clean status.
            return None
    else:
        # Windows: no `O_NOFOLLOW` and no descriptor-relative open, so the pinned
        # walk is unavailable. An unprivileged process there cannot create a FILE
        # symlink at all (that needs elevation, which is why `symlink_or_junction`
        # exists), so the reachable swap is a junction on an ancestor -- refused by
        # the reparse-point probe below. The window is narrowed rather than closed,
        # and it is narrowed against what the platform actually permits.
        try:
            if any(
                is_reparse_point(p)
                for p in (target, *target.parents)
                if p == resolved_root or resolved_root in p.parents or p == target
            ):
                return None
            fd = os.open(target, flags)
        except (OSError, ValueError):
            # Same unencodable-name classes as the pinned branch above: this open
            # takes the whole path rather than a name under a descriptor, so it is
            # reachable the same way and needs the same tuple.
            return None

    try:
        st = os.fstat(fd)
        # `st_nlink != 1` is the third gate, and it is the only one that can see a
        # HARDLINK. An alias shares the target's inode, so every path-based guard is
        # blind to it: `is_symlink()` is False, `realpath` yields the alias's own name
        # (so containment passes), and `O_NOFOLLOW` has no link to refuse. Measured:
        # a declared `assets/icon.webp` hardlinked to a file outside the install
        # directory opens cleanly, reports S_ISREG, sits under the size cap, and its
        # bytes are served with a 200 -- laundering, through an unsandboxed gateway, a
        # read the app's own sandboxed code can be refused.
        #
        # Checked on the DESCRIPTOR, which is what makes it race-free: this fd already
        # refers to the inode being judged. Every other descriptor-validated read in
        # the tree applies the same gate (`hooks.py`, `memory.py`, `spec_builder`,
        # `onboarding_scan.py`, `pinned_fs.copy_file_pinned`), so this route was the
        # outlier rather than a new rule.
        #
        # Inline rather than `pinned_fs.refuse_hardlink_alias`, which is the same
        # check behind an exception: that helper CLOSES the fd before raising, and this
        # function closes in a `finally`, so routing through it would double-close --
        # and a reused fd number makes that a worse bug than the one being fixed. The
        # inline spelling is what the sibling sites above use for the same reason:
        # their refusal is a return value, not a raise.
        if not stat.S_ISREG(st.st_mode) or st.st_nlink != 1 or st.st_size > _ART_MAX_BYTES:
            return None
        with os.fdopen(fd, "rb", closefd=False) as fh:
            data = fh.read(_ART_MAX_BYTES + 1)
    except OSError:
        return None
    finally:
        os.close(fd)
    if len(data) > _ART_MAX_BYTES:
        return None
    # Validator derived from the DESCRIPTOR we actually read, not from a second stat
    # of the path. `no-cache` means the browser revalidates every load, and the rail
    # renders on every load, so without a validator each one would be a full 200.
    validator = f'"{st.st_ino:x}-{st.st_size:x}-{st.st_mtime_ns:x}"'
    return data, validator


# ---------------------------------------------------------------------------
# Git blob proxy — serve images from a registry app's git repo
# ---------------------------------------------------------------------------


_BLOB_FETCH_TIMEOUT = 30  # seconds — shallow clone of a single-branch repo
_BLOB_FETCH_SEMAPHORE = asyncio.Semaphore(3)  # max 3 concurrent git fetches
# Bare-name repo identifier (legacy registry entries) — no scheme, no path.
_SAFE_REPO_RE = re.compile(r"^[A-Za-z0-9_-]+$")
# https git URL: https://host[:port]/org/app[.git]. Host/path charset is
# restricted and shell metacharacters / traversal are rejected separately.
# Plaintext ``http://`` is deliberately NOT accepted: registry clones fetch an
# index + app manifests whose setup code later runs with gateway privileges
# (signatures are optional by default), so an unauthenticated transport would
# let a network (MITM) attacker swap in an attacker-controlled app. Require TLS
# for HTTP-style remotes; use an explicit ssh:// / scp form for private ones.
_SAFE_HTTPS_URL_RE = re.compile(r"^https://[A-Za-z0-9.\-]+(?::[0-9]+)?/[A-Za-z0-9._/\-]+$")
# scp-style ssh remote: user@host:org/app[.git]
_SAFE_SCP_URL_RE = re.compile(r"^[A-Za-z0-9._\-]+@[A-Za-z0-9.\-]+:[A-Za-z0-9._/\-]+$")
# ssh:// URL form: ssh://[user@]host[:port]/org/app[.git]
# Userinfo is optional — userless ssh URLs (e.g. ssh://git.example.com/pkg/X) are
# a standard git form where ~/.ssh/config supplies the user.
_SAFE_SSH_URL_RE = re.compile(
    r"^ssh://(?:[A-Za-z0-9._\-]+@)?[A-Za-z0-9.\-]+(?::[0-9]+)?/[A-Za-z0-9._/\-]+$"
)
# `\Z`, not `$`: Python's `$` also matches immediately BEFORE a trailing newline, so with
# the `.match` calls in the blob handler a value like "main\n" passes -- and this feeds
# git argv. Same defect class as the catalog-side coordinate patterns; this is the blob
# handler's ref instance. (The class is wider than this file: other `$`-anchored
# request-path patterns exist elsewhere, e.g. papyrus's GIT_URL_RE.) The `path` grammar
# is `_SAFE_PATH_RE`, imported from the registry facade: the owner-tier prewarm applies
# the same object to a declared path before caching it, so the two cannot drift.
_SAFE_REF_RE = re.compile(r"^[A-Za-z0-9._/-]+\Z")


def _is_safe_repo_identifier(repo: str) -> bool:
    """Validate the blob-proxy ``repo`` query parameter.

    Registry entries are now full git URLs (``https://github.com/org/app``,
    ``git@host:org/app.git``), but legacy entries may still use a bare name.
    Accept either a bare token OR a vetted git URL — never an arbitrary string.

    Git URLs are validated against a restricted scheme/host/path charset and
    rejected outright if they contain shell metacharacters or ``..`` path
    traversal, so the value is safe to pass to ``git clone`` argv.
    """
    if not repo:
        return False
    # Reject shell metacharacters and traversal regardless of form.
    if ".." in repo or any(c in repo for c in " \t\n\r;|&$`<>()*?!\\\"'"):
        return False
    if _SAFE_REPO_RE.match(repo):
        return True
    if _SAFE_HTTPS_URL_RE.match(repo):
        return True
    if _SAFE_SCP_URL_RE.match(repo):
        return True
    if _SAFE_SSH_URL_RE.match(repo):
        return True
    return False


def _touch_served_blob(cache_path: Path) -> None:
    """Bump a served blob's mtime to now without touching a link's target.

    ``os.utime(follow_symlinks=False)`` where the platform supports it; elsewhere
    (Windows) the name must lstat as a plain regular file before it is touched.
    """
    if os.utime in os.supports_follow_symlinks:
        os.utime(cache_path, None, follow_symlinks=False)
        return
    if not stat.S_ISREG(os.lstat(cache_path).st_mode):
        return
    os.utime(cache_path, None)
