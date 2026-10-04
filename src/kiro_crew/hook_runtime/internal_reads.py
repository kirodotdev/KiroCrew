"""The audited internal-read carve-outs: the edition registration seam, the
fixed-path sensitive read and the two SEL audit entry points.

Composed onto ``kiro_crew.hooks``; see :mod:`kiro_crew.hook_runtime`.
"""

from __future__ import annotations

import os
from pathlib import Path
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from kiro_crew.hooks import (
        _AUDIT_ONLY_READ_IDS,
        _INTERNAL_READ_ALLOWLIST,
        MAX_FILE_BYTES,
        is_sensitive_path,
        logger,
        platform_compat,
    )


def register_internal_read_path(read_id: str, rel_path: str) -> None:
    """Register an edition-contributed fixed-path internal-read carve-out.

    The composition-time seam an edition companion uses to add its own trusted
    fixed-path reads (e.g. an SSO cookie jar for the usage-upload path) to
    ``_INTERNAL_READ_ALLOWLIST`` — the exact structural twin of the boot-time
    ``register_acp_backends`` / ``register_publish_providers`` seams.  This is
    NOT an agent-reachable API: it is called once, from the companion's boot
    composition, with HARDCODED constant arguments.  It never widens what
    ``safe_read_file_internal`` will read at call time — that function still
    re-verifies the resolved path is sensitive, opens O_NOFOLLOW, and SEL-audits
    every outcome — this only lets an edition contribute an entry to the same
    guarded table the core ships.

    Guards (fail-closed, so a mis-registration cannot open a hole):

    * ``read_id`` must be a non-empty string; re-registering an existing key with
      a DIFFERENT path raises (a companion cannot silently repoint a core entry
      such as ``kiro_usage_api.sso_token_cli`` at an attacker file).  Re-
      registering the same key with the same path is idempotent.
    * ``rel_path`` must be a relative path with no ``..`` component and no
      absolute/anchor part, so the resolved target can only ever live under
      ``~`` (the read still resolves under ``Path.home()`` at call time).
    * the resolved ``~/<rel_path>`` must already be classified sensitive by
      :func:`kiro_crew.security.is_sensitive_path` — the carve-out is only valid
      for a path the shared file gate otherwise blocks; registering a
      non-sensitive path is a configuration error and raises.
    """
    if not isinstance(read_id, str) or not read_id:
        raise ValueError("register_internal_read_path: read_id must be a non-empty string")
    existing = _INTERNAL_READ_ALLOWLIST.get(read_id)
    if existing is not None and existing != rel_path:
        raise ValueError(
            f"register_internal_read_path: {read_id!r} already registered to a "
            f"different path {existing!r}; refusing to repoint",
        )
    p = Path(rel_path)
    if p.is_absolute() or p.anchor or ".." in p.parts:
        raise ValueError(
            f"register_internal_read_path: rel_path must be relative with no '..' "
            f"(got {rel_path!r})",
        )
    resolved = str((Path.home() / p).expanduser())
    if not is_sensitive_path(resolved):
        raise ValueError(
            f"register_internal_read_path: {rel_path!r} resolves to a non-sensitive "
            f"path; the carve-out is only valid for a sensitive path",
        )
    _INTERNAL_READ_ALLOWLIST[read_id] = rel_path


def safe_read_file_internal(read_id: str) -> bytes | None:
    """Read a sensitive path on behalf of an authorized internal caller.

    The ``read_id`` must be a key in ``_INTERNAL_READ_ALLOWLIST``. The
    function resolves the allowlisted path under ``~``, verifies it is in fact
    sensitive (defense in depth), reads the bytes (subject to
    ``MAX_FILE_BYTES``), emits an SEL audit event on every outcome, and returns
    the bytes -- or ``None`` if missing / unreadable / oversized.

    Raises ``PermissionError`` if ``read_id`` is not allowlisted -- callers must
    never construct ``read_id`` from untrusted input.

    Fail-closed audit: if the SEL audit for the ``success`` outcome cannot be
    recorded (backend unavailable, or the emit raised), the function returns
    ``None`` instead of the bytes -- a ``logger.warning`` is not itself an SEL
    audit event, and the carve-out's validity depends on every successful read
    producing a real audit. Callers already handle ``None`` (degrade to the
    text scrape).
    """
    if read_id not in _INTERNAL_READ_ALLOWLIST:
        _emit_internal_read_audit(read_id, "not_allowlisted")
        raise PermissionError(
            f"safe_read_file_internal denied: {read_id!r} not in allowlist",
        )

    rel_path = _INTERNAL_READ_ALLOWLIST[read_id]
    abs_path = Path.home() / rel_path
    resolved = str(abs_path.expanduser())

    # Defense in depth: the allowlist is only a meaningful carve-out if the
    # underlying path is in fact sensitive. If it has stopped being sensitive,
    # the carve-out has nothing to protect against and the configuration has
    # drifted; refuse rather than silently widen access.
    if not is_sensitive_path(resolved):
        _emit_internal_read_audit(read_id, "not_sensitive")
        raise PermissionError(
            f"safe_read_file_internal denied: {read_id!r} resolves to a "
            f"non-sensitive path; allowlist is only valid for sensitive paths",
        )

    # Open so a link at the final path component (e.g. a planted
    # ~/.aws/sso/cache/kiro-auth-token-cli.json -> attacker file) is refused,
    # binding the read to the real allowlisted file rather than a redirected
    # target. platform_compat.open_file_no_reparse carries that refusal on Windows
    # as well, where O_NOFOLLOW does not exist and a plain os.open would resolve a
    # junction planted at the name. Check + read share ONE descriptor
    # (TOCTOU-safe), and fstat confirms a regular file before reading.
    import stat

    try:
        fd = platform_compat.open_file_no_reparse(resolved, nonblocking=True)
    except FileNotFoundError:
        _emit_internal_read_audit(read_id, "missing")
        return None
    except OSError:
        # ELOOP (final component is a link) and any other open error —
        # fail closed, never following the link.
        _emit_internal_read_audit(read_id, "unreadable")
        return None

    data = b""
    try:
        st = os.fstat(fd)
        if not stat.S_ISREG(st.st_mode):
            _emit_internal_read_audit(read_id, "not_regular")
            return None
        with os.fdopen(fd, "rb", closefd=True) as fh:
            fd = -1  # ownership transferred to fh; do not double-close
            data = fh.read(MAX_FILE_BYTES + 1)
    except OSError:
        _emit_internal_read_audit(read_id, "unreadable")
        return None
    finally:
        if fd != -1:
            try:
                os.close(fd)
            except OSError:
                pass

    if len(data) > MAX_FILE_BYTES:
        _emit_internal_read_audit(read_id, "too_large")
        return None

    if not _emit_internal_read_audit(read_id, "success"):
        logger.error(
            "Denying sensitive read %s: SEL audit unavailable; the carve-out "
            "requires an audit trail and the caller will see None instead of "
            "the file bytes.",
            read_id,
        )
        return None
    return data


def _emit_internal_read_audit(read_id: str, outcome: str) -> bool:
    """Emit an SEL audit event for an internal sensitive/credential read.

    Returns ``True`` iff an SEL event was recorded, ``False`` otherwise (SEL
    backend unavailable or the emit raised). ``safe_read_file_internal`` /
    ``emit_internal_read_audit`` gate the return of sensitive bytes on this
    result for ``success`` outcomes: a ``logger.warning`` is NOT itself an SEL
    audit event, so a read whose audit could not be recorded must be denied.
    """
    try:
        from kiro_crew.sel import sel
    except ImportError:  # pragma: no cover - sel optional in some test envs
        logger.warning(
            "SEL backend unavailable; internal-read audit dropped " "for read_id=%s outcome=%s",
            read_id,
            outcome,
        )
        return False
    try:
        sel().log_tool_invocation(
            session_key="hooks:safe_read_file_internal",
            tool_name=f"internal_read.{read_id}",
            outcome=outcome,
            source="hooks",
            # audit-or-deny: a "success" gates the return of live credential
            # bytes, so it must be written SYNCHRONOUSLY (critical=True drains the
            # queue and re-raises on a filesystem failure). In async SEL mode a
            # non-critical log() only ENQUEUES — a later writer-thread failure is
            # swallowed and this would wrongly return True for an audit that
            # never landed. Non-success outcomes already return None / raise, so
            # a dropped event there still leaves an observable log line.
            critical=(outcome == "success"),
        )
    except Exception:  # noqa: BLE001 - audit must never break the caller
        logger.warning(
            "SEL audit emission failed for internal read read_id=%s",
            read_id,
            exc_info=True,
        )
        return False
    return True


def emit_internal_read_audit(read_id: str, outcome: str) -> bool:
    """Emit an SEL audit event for a credential read that cannot route through
    :func:`safe_read_file_internal`.

    ``safe_read_file_internal`` covers reads of *sensitive paths*. Some
    credential material lives at a path that is NOT classified sensitive yet
    still holds a live secret -- e.g. the kiro-cli auth store at
    ``~/.local/share/kiro-cli/data.sqlite3``. Such a reader still owes the same
    audit trail, so it calls this wrapper with its own ``read_id`` and outcome.
    A presence-only access under a classified directory at a computed name lands
    here for the mirror-image reason: there is no content to gate and no fixed
    path to register, but the contact with the credential store is real. See
    :data:`_AUDIT_ONLY_READ_IDS` for both classes.

    The ``read_id`` MUST be registered in ``_AUDIT_ONLY_READ_IDS`` -- this entry
    point enforces its own allowlist, mirroring the ``_INTERNAL_READ_ALLOWLIST``
    gate, so it cannot be used as an unscoped bypass of the SEL-audit surface.
    An unregistered ``read_id`` returns ``False`` without emitting, which
    callers treat as "audit unavailable" and fail closed on.
    """
    if read_id not in _AUDIT_ONLY_READ_IDS:
        logger.warning("emit_internal_read_audit: unregistered read_id %r rejected", read_id)
        return False
    return _emit_internal_read_audit(read_id, outcome)
