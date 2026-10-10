"""The one rule for a user-named project directory.

A dashboard chat folder, a chat slot's project control and a cron job can
each name the repository their sessions run in. The check is the same for
all three -- absolute (or ``~``-relative) path, canonical path,
sensitive-location refusal with a SEL denial, existing directory -- and
copies of one rule drift, so this module is the single body every surface
calls; what differs per surface (the caller-facing noun in the message, the
SEL operation and caller, a length cap, the HTTP status a refusal maps to)
stays in the surface's own wrapper.

Two refusals run BEFORE anything can contact a remote host, because
canonicalising is what would: a UNC/network spelling (``\\\\host\\share``,
``//host/share``, ``\\\\?\\UNC\\...``) is refused outright on every
platform, and on Windows the path is not handed to ``realpath`` at all --
it is canonicalised through a HANDLE-PINNED walk (:func:`_pinned_windows_dir`)
that opens each prefix with ``FILE_FLAG_OPEN_REPARSE_POINT`` (a reparse point
at the name is opened AS ITSELF and refused, never followed) and holds every
ancestor open without ``FILE_SHARE_DELETE`` while the next component is
opened through it, so no component can be swapped for a junction between
check and use; the canonical spelling is then read from the leaf handle
(``GetFinalPathNameByHandleW``), the object actually opened. Windows resolves
a reparse point by opening its target, and a target on an attacker-named host
means an outbound SMB/NTLM authentication carrying the gateway's credentials
-- irreversible once emitted, and a junction needs no privilege to create, so
a check-then-resolve pair is a race the attacker can win by flipping the
component in a loop. Where the handle path cannot be read the walk fails
CLOSED. The leading-separator check alone does not cover a
``C:\\``-prefixed path whose component is such a point.

Every security-class refusal here -- a sensitive location, a UNC/network
spelling, a reparse-point component -- is recorded in the SEL as a denial
under the surface's *audit_operation* for its *audit_caller*, with the path
redacted through ``redact_log_via_context``; the plain input refusals
(relative, missing, not a directory) are not permission decisions and emit
nothing. The security, SEL and pinned-filesystem modules are imported at
module scope (no cycle: none of them imports this module or its callers) and
reached as attributes, so a test that patches
``kiro_crew.security.is_sensitive_canonical_path`` or ``kiro_crew.sel.sel``
still intercepts the call.
"""

from __future__ import annotations

import errno
import logging
import os
import re

from kiro_crew import pinned_fs, platform_compat, security, sel
from kiro_crew.platform.context import redact_log_via_context

logger = logging.getLogger(__name__)

#: Two leading separators in any mix name a UNC/network path on Windows
#: (including the ``\\?\UNC\`` and ``\\?\`` extended forms).
_UNC_PREFIX = re.compile(r"^[\\/]{2}")
#: Platform seam: the handle-pinned walk is a Windows concept (a POSIX symlink
#: cannot trigger outbound authentication, and POSIX ``realpath`` is fine). A
#: module attribute rather than an inline read so a test can drive the walk
#: off Windows against faked pin/final-path helpers.
_WINDOWS = os.name == "nt"


class ProjectDirRefused(ValueError):
    """The named directory cannot root a session; ``str(exc)`` is the caller-facing reason.

    ``sensitive`` is the one distinction a surface answers differently: the
    slot-project endpoint returns 403 for a protected location and 400 for
    every other refusal. The finer class of a security refusal (network
    spelling, reparse point, a revalidation that resolved elsewhere) is
    recorded in the SEL denial's ``error`` field, not on the exception -- no
    caller branches on it.
    """

    def __init__(self, message: str, *, sensitive: bool = False) -> None:
        super().__init__(message)
        self.sensitive = sensitive


def _deny(
    path: str,
    *,
    error: str,
    message: str,
    audit_operation: str,
    audit_caller: str,
    sensitive: bool = False,
) -> ProjectDirRefused:
    """Record a SECURITY refusal of *path* in the SEL and return the exception to raise.

    A sensitive location, a UNC/network spelling and a reparse-point
    component are permission decisions rather than input validation, so each
    is recorded as a denial under *audit_operation*, attributed to
    *audit_caller*, with the path redacted through ``redact_log_via_context``
    -- the companion-aware pass a gate-side audit line uses, so a host with a
    companion loaded is not scanned with the weaker OSS baseline. *error* is
    the short machine-readable reason (``"sensitive path"``, ``"network
    path"``, ``"reparse point"``); *message* is what the caller sees.
    Best-effort: an audit failure never turns the refusal into a traceback.
    Returned rather than raised so a call site reads ``raise _deny(...)``.
    """
    try:
        sel.sel().log_api_access(
            caller=audit_caller,
            operation=audit_operation,
            outcome="denied",
            resources=redact_log_via_context(path),
            error=error,
        )
    except Exception:
        logger.debug("SEL logging failed for %s denial", audit_operation, exc_info=True)
    return ProjectDirRefused(message, sensitive=sensitive)


def _refuse_if_sensitive(canonical: str, *, audit_operation: str, audit_caller: str) -> None:
    """Raise the audited sensitive-path refusal when *canonical* is protected."""
    if security.is_sensitive_canonical_path(canonical):
        raise _deny(
            canonical,
            error="sensitive path",
            message="project_dir refers to a sensitive path",
            sensitive=True,
            audit_operation=audit_operation,
            audit_caller=audit_caller,
        )


def _pinned_windows_dir(
    expanded: str, label: str, *, audit_operation: str, audit_caller: str
) -> str:
    r"""Canonicalise *expanded* on Windows without ever following a reparse point.

    The walk is :func:`kiro_crew.platform_compat.pin_directory_chain` -- the
    same one the agent spawn holds across process creation -- so the rule that
    admits a directory here and the rule that enters it later are one body.
    Every prefix is opened with ``FILE_FLAG_OPEN_REPARSE_POINT`` (a junction or
    symlink at the name is refused in the call that would have traversed it,
    its target never touched) and held until the walk ends, so no verified
    component can be swapped while the next is opened through it. The
    canonical spelling is read from the LEAF handle
    (:func:`kiro_crew.pinned_fs.fd_real_path`, ``GetFinalPathNameByHandleW``),
    so it names the directory actually opened, not a re-resolution of the
    string. Fails closed when that read is unavailable. The leaf is proven a
    real directory by the open, so the caller needs no ``isdir`` by name --
    which would re-resolve the path and reopen the window.

    The chain is released here: what this function establishes is the
    canonical NAME. Holding the name's components fixed until the directory is
    actually entered is the spawn's job (``AcpRuntime``/``ACPProvider`` pin the
    same chain across ``CreateProcess`` and for the child's lifetime), because
    only the spawn knows when that moment is.
    """
    reparse_message = f"{label} must not pass through a symlink, junction or other reparse point"

    def _reparse_denied(at: str) -> ProjectDirRefused:
        return _deny(
            at,
            error="reparse point",
            message=reparse_message,
            audit_operation=audit_operation,
            audit_caller=audit_caller,
        )

    fds: list[int] = []
    try:
        try:
            fds = platform_compat.pin_directory_chain(expanded)
        except platform_compat.NotALocalVolume as exc:
            # The letter has no local volume identity (a mapped share, or bound
            # to nothing): the chain refused before opening anything.
            raise _deny(
                expanded,
                error="network path",
                message=f"{label} must be a local path, not a UNC/network path or mapped drive",
                audit_operation=audit_operation,
                audit_caller=audit_caller,
            ) from exc
        except platform_compat.ReparsePointRefused as exc:
            # The pin proved the reparse bit on the handle it opened and says
            # so on the exception. Nothing is re-read by name here: the handle
            # is closed by now and the name is unpinned, so an ``lstat`` of it
            # could find the junction already gone and misfile a security
            # refusal as a plain missing directory -- with no denial audit.
            raise _reparse_denied(str(getattr(exc, "filename", "") or expanded)) from exc
        except NotADirectoryError as exc:
            # A plain file at the name (the pin reports a reparse point as the
            # distinct type above).
            raise ProjectDirRefused(f"{label} must be an existing directory") from exc
        except FileNotFoundError as exc:
            raise ProjectDirRefused(f"{label} must be an existing directory") from exc
        except OSError as exc:
            if exc.errno == errno.ELOOP:
                # A POSIX host driving this branch: ``O_NOFOLLOW`` reports a
                # link at the name as ELOOP rather than ENOTDIR.
                raise _reparse_denied(str(getattr(exc, "filename", "") or expanded)) from exc
            raise ProjectDirRefused(
                f"{label} could not be opened: {exc.strerror or exc.__class__.__name__}",
            ) from exc
        if not fds:
            # A bare drive: nothing was opened, so nothing was proven.
            raise ProjectDirRefused(f"{label} must be an existing directory")
        final = pinned_fs.fd_real_path(fds[-1])
        if not final:
            raise ProjectDirRefused(f"{label} could not be canonicalised through its handle")
        return final
    finally:
        platform_compat.release_directory_chain(fds)


def resolve_project_dir(
    raw: str,
    *,
    label: str,
    audit_operation: str,
    audit_caller: str,
    expect_canonical: str | None = None,
) -> str:
    """Return the REALPATH of *raw*, or ``""`` when *raw* is empty.

    Raises :class:`ProjectDirRefused` for a relative path, a path under a
    sensitive location, or a directory that does not exist. *label* is the
    noun the messages use for the field (``"project_dir"``, ``"Project
    directory"``) so each surface keeps the wording its callers and tests
    know.

    *expect_canonical* is for a REVALIDATION of a value this function already
    produced and a store persisted (the cron wake re-checking a job's
    ``project_dir``): the fresh resolution must spell the very same directory,
    compared as strings with no filesystem access (``normcase`` only -- a
    ``realpath`` of the expectation would follow the very swap this detects).
    Anything else is refused as ``"moved"`` and SEL-audited: the persisted
    name was judged (and, for an agent-authored job, confined to its allowed
    roots) at authoring, and a component of it replaced since by a link is a
    same-user redirection of where the session will run, not a stale input.

    The sensitive-path refusal (:func:`_refuse_if_sensitive`) is a SECURITY
    decision rather than input validation, so it is SEL-audited under
    *audit_operation* for *audit_caller*. The gate is
    ``is_sensitive_canonical_path`` on the canonical path computed here --
    re-submitting an already-canonical path to the pool-bounded
    ``is_sensitive_path`` would only add a fail-closed budget miss under
    concurrent callers. It runs before the existence check on every platform,
    so a protected location is refused as such whether or not it exists.
    """
    if not raw:
        return ""

    def _network_denied(spelling: str) -> ProjectDirRefused:
        return _deny(
            spelling,
            error="network path",
            message=f"{label} must be a local path, not a UNC/network path or mapped drive",
            audit_operation=audit_operation,
            audit_caller=audit_caller,
        )

    def _settled(resolved: str) -> str:
        """The canonical answer, refused when a revalidation resolved elsewhere.

        A plain string compare: the expectation is the canonical spelling
        this function produced before, so re-resolving it by name is exactly
        the follow-the-swapped-link the compare exists to catch. ``normcase``
        alone -- no ``normpath``, no filesystem -- so a case-insensitive host
        does not refuse its own spelling and nothing is opened.
        """
        if expect_canonical is not None and os.path.normcase(resolved) != os.path.normcase(
            expect_canonical
        ):
            raise _deny(
                resolved,
                error="moved",
                message=(
                    f"{label} no longer resolves to the directory that was validated "
                    "(a component was moved, replaced or relinked since); re-set it"
                ),
                audit_operation=audit_operation,
                audit_caller=audit_caller,
            )
        return resolved

    if _UNC_PREFIX.match(raw):
        raise _network_denied(raw)
    if not os.path.isabs(raw) and not raw.startswith("~"):
        raise ProjectDirRefused(f"{label} must be an absolute path")
    expanded = os.path.expanduser(raw)
    if _UNC_PREFIX.match(expanded):
        # ``~`` skipped the check above and expands from the profile
        # variables; a UNC-rooted profile would otherwise reach the walk.
        # Judged before the absolute demand: a network spelling is the
        # security denial whatever else is wrong with it.
        raise _network_denied(expanded)
    if not os.path.isabs(expanded):
        # ``~`` was admitted above on the promise that expansion makes it
        # absolute; an unknown ``~user`` (or a home-less process) leaves it
        # relative, and a relative path here would resolve against the
        # gateway's own cwd rather than anything the caller named.
        raise ProjectDirRefused(f"{label} must be an absolute path")
    if _WINDOWS:
        # Never ``realpath`` here: resolving through a reparse point is what
        # would open its target, and a share target means outbound
        # authentication. The pinned walk canonicalises AND proves the
        # directory in one operation per component. The sensitive gate is
        # asked FIRST on the lexical spelling (``normpath`` touches no
        # filesystem), so a protected location is refused as such whether or
        # not it exists -- the same precedence as the POSIX branch, where the
        # gate runs before the existence check -- and asked AGAIN on the
        # handle's answer, which is what the walk proved.
        _refuse_if_sensitive(
            os.path.normpath(expanded),
            audit_operation=audit_operation,
            audit_caller=audit_caller,
        )
        # A mapped drive (``Z:\repo`` bound to a share) spells like a local
        # path and passes the UNC guard, yet opening anything on it is the same
        # outbound authentication. The volume ROOT's drive type answers without
        # touching the share (``GetDriveTypeW``); anything but an explicit
        # "local" -- remote, unknown, or a failed query -- is refused, closed.
        # This is the cheap early answer; the binding one is inside the walk,
        # which opens under the local volume's own identity rather than the
        # letter, so a letter rebound between this check and the opens cannot
        # redirect them (``platform_compat.pin_directory_chain``).
        if platform_compat.path_volume_is_remote(expanded) is not False:
            raise _network_denied(expanded)
        resolved = _pinned_windows_dir(
            expanded, label, audit_operation=audit_operation, audit_caller=audit_caller
        )
        _refuse_if_sensitive(resolved, audit_operation=audit_operation, audit_caller=audit_caller)
        # The leaf handle already proved a real directory; an ``isdir`` by
        # name would re-resolve the string and reopen the window.
        return _settled(resolved)
    resolved = os.path.realpath(expanded)
    _refuse_if_sensitive(resolved, audit_operation=audit_operation, audit_caller=audit_caller)
    if not os.path.isdir(resolved):
        raise ProjectDirRefused(f"{label} must be an existing directory")
    return _settled(resolved)
