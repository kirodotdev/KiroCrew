"""Gateway-held, sandbox-sealed enablement grants for apps that run code OUTSIDE the sandbox.

An app that runs code outside the agent sandbox (Dev Fleet's "pod" execs worktree
code on the host) must not be enable-able from inside the jail. The
``apps/<name>/installed.json`` enablement record cannot carry that authorization: it
lives under the agent-writable, renamable ``apps/<name>`` directory, so a sandboxed
shell can rename the directory aside (the read-only bind moves with the renamed
directory on the namespace launcher, see ``sandbox_argv`` "a renamed parent carries
the read-only mount with it"), recreate the path, and write ``enabled: true`` into a
fresh, unmasked file. A gateway reading that record would start a pod outside the
sandbox -- the exact containment boundary the gate exists to hold.

This module keeps the authorization as a marker under a TOP-LEVEL crew-home directory
(``host-exec-grants/``) the sandbox seals read-only and pre-creates, which -- as a
direct crew-home child -- is the pattern the sandbox does not let an agent rename
around (``aws-control-staging``, ``quarantined-clones``), unlike a leaf nested under
``apps/``.

The marker is written ONLY from the owner-authenticated enable path (``enable_app``,
which requires an owner dashboard request and refuses app tokens), for whichever app
the owner enabled. It is NEVER derived from the enablement record, so re-writing a
forged ``installed.json`` (for example on the boot re-registration of a builtin)
cannot mint it. It is revoked on every write that disables the app and on uninstall,
so a disabled app can never keep one.

These functions are generic -- they act on whatever app name they are handed and know
no specific app id, so core (`sandbox_leaves`, `manager_records`, `manager_uninstall`)
stays app-agnostic. A marker for an app with no host-exec path is harmless: nothing
reads it. The only reader is the pod gate inside ``apps/builtins/dev_fleet/``.
"""

from __future__ import annotations

import logging

from kiro_crew.config.paths import config_dir

logger = logging.getLogger(__name__)

#: Top-level crew-home directory holding one empty marker file per app the owner has
#: enabled through the authenticated enable path. Sealed read-only in the sandbox and
#: pre-created so it exists (and is a mount point) before any spawn. Mirrored by name
#: in ``sandbox_leaves._CREW_READONLY_LEAVES`` / ``_CREW_CHILD_READABLE_LEAVES`` and
#: ``sandbox_precreate._CREW_PRECREATE_READONLY_DIR_LEAVES``.
GRANTS_LEAF = "host-exec-grants"


def _grant_path(name: str):
    return config_dir() / GRANTS_LEAF / name


def grant_host_exec(name: str) -> bool:
    """Record, from the unsandboxed gateway, that *name* is owner-enabled to run
    outside the sandbox. Call ONLY from the owner-authenticated enable path -- never
    derived from the enablement record. A write failure is logged, never raised, and
    returns False so the caller can report it; every reader fails CLOSED on a
    missing grant.
    """
    try:
        path = _grant_path(name)
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text("", encoding="utf-8")
    except OSError:
        logger.warning("could not record host-exec grant for %r", name, exc_info=True)
        return False
    return True


def revoke_host_exec(name: str) -> None:
    """Remove *name*'s grant. Safe to call for any app on any write that disables it
    and on uninstall: a no-op when no grant exists."""
    try:
        _grant_path(name).unlink()
    except FileNotFoundError:
        pass
    except OSError:
        logger.warning("could not revoke host-exec grant for %r", name, exc_info=True)


def host_exec_granted(name: str) -> bool:
    """Whether the gateway recorded an owner-authenticated grant for *name*.

    The pod gate's authority. Unforgeable from the sandbox: the grant directory is
    sealed read-only and cannot be renamed around, and the marker is written only on an
    owner-authenticated enable, never from the enablement record. A forged
    ``installed.json`` never produces one.
    """
    try:
        return _grant_path(name).is_file()
    except OSError:
        return False
