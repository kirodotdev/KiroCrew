"""This-crew AgentCore identity — Settings → Security on THIS gateway.

Each crew's own dashboard shows and (when the home policy is writable)
configures ``capabilities.agentcore``. This is not a Remote Crew / launch
control: a hub launching another box is a different crew.

GET is display-only. When no ceiling is loaded it falls back to a
validated ``KIROCREW_AGENTCORE_POSTURE`` so a live env-attached
identity is not shown as Off. Launch-env ``workload_name`` /
``gateway_url`` fill the snapshot only when ``source`` is ``env``;
a policy row that omitted those fields stays empty so Save cannot
write process env into the keystone. Save → Off keeps any non-empty
submitted name/URL on the disabled row so an env-only identity can
re-enable without losing the displayed values. One ``_file_row()`` peek supplies
posture, name, and URL so a concurrent Save cannot mix generations.
PUT is the operator's out-of-band write of the
standalone ``security_policy.json`` home file (same trust model as
computer-use Settings: dashboard cookie, no app token). The agent tool
gate still cannot touch that path. A fleet env override, a signed document, or a
required-signature fleet is refused rather than rewritten.

Owner-dashboard PUT hot-applies the home file onto the running
ceiling and AWS adapter. Cache-only policy distribution
(``KIROCREW_POLICY_CACHE_ONLY``) is refused the same way as a
fleet URL. The home-file write takes ``security_policy.json.lock``.
``restart_required`` stays true when that apply cannot attach the
extra, or when the agent-config rebuild fails. A failed rebuild is
HTTP 503 ``agent_rebuild_failed`` and does not drop live sessions
onto a stale generated spec. The same 503 is used when session
revoke raises after persist (provider-factory reload), so PUT
cannot 500 on a write that already committed. A failed apply (Off, Login, or Workload) still
revokes live sessions and the proxy when apply returns False
(policy reload failed) and answers 503 ``runtime_apply_failed`` —
a 200 must not mean a persisted identity while Gateway sessions remain.
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
import time
from pathlib import Path
from typing import Any

from aiohttp import web

from kiro_crew import platform_compat
from kiro_crew.atomic_write import (
    _REPLACE_BACKOFF_SECONDS,
    _REPLACE_MAX_ATTEMPTS,
    on_event_loop,
    replace_with_retry,
    unlink_with_retry,
)
from kiro_crew.platform.agentcore_schema import (
    normalize_agentcore_gateway_url,
    normalize_agentcore_workload_name,
)
from kiro_crew.platform.agentcore_sigv4 import is_agentcore_gateway_url
from kiro_crew.platform.context import PROFILE_STANDALONE, current_context
from kiro_crew.platform.governance import (
    PlatformCompositionError,
    _policy_home_path,
    _policy_signature_required,
    agentcore_posture,
    parse_policy,
)

logger = logging.getLogger(__name__)

OP_GET = "agentcore.identity.get"
OP_SAVE = "agentcore.identity.save"
_ENV_WORKLOAD = "KIROCREW_AGENTCORE_WORKLOAD_NAME"
_ENV_GATEWAY_URL = "KIROCREW_AGENTCORE_GATEWAY_URL"
_ENV_POSTURE = "KIROCREW_AGENTCORE_POSTURE"


def apply_agentcore_runtime() -> bool:
    """Lazy so dashboard route import does not load the AWS extra."""
    from kiro_crew.platform.agentcore_aws import apply_agentcore_runtime as impl

    return impl()


def ensure_extra() -> str:
    from kiro_crew.platform.agentcore_aws import ensure_extra as impl

    return impl()


def extra_snapshot(*, last_code: str | None = None) -> dict[str, Any]:
    from kiro_crew.platform.agentcore_aws import extra_snapshot as impl

    return impl(last_code=last_code)


_POSTURES = frozenset({"none", "workload", "login"})
_MINIMAL_BOOT = {
    "require_sandbox": True,
    "allow_terminal": False,
    "fail_closed": True,
}


class _PolicyNotWritable(Exception):
    """Home policy cannot be rewritten (signed, distributed, or fleet)."""

    def __init__(self, reason: str) -> None:
        super().__init__(reason)
        self.reason = reason


def _audit(
    request: web.Request,
    *,
    operation: str,
    outcome: str,
    resources: str = "",
    error: str = "",
) -> None:
    try:
        import kiro_crew.dashboard.handlers as pkg

        pkg.sel().log_api_access(
            caller=request.get("user", "dashboard"),
            operation=operation,
            outcome=outcome,
            source="dashboard",
            resources=resources,
            error=error,
        )
    except Exception:
        logger.warning("SEL logging failed for %s", operation, exc_info=True)


def _file_workload_name(row: dict[str, Any] | None = None) -> str:
    if row is None:
        row = _file_row()
    if row is None:
        return ""
    raw = row.get("workload_name")
    if not isinstance(raw, str) or not raw.strip():
        return ""
    try:
        return normalize_agentcore_workload_name(raw)
    except ValueError:
        return ""


def _workload_name(*, allow_env: bool = False, row: dict[str, Any] | None = None) -> str:
    """Policy name. Launch env only when the snapshot source is env."""
    name = _file_workload_name(row)
    if name:
        return name
    if allow_env:
        return os.environ.get(_ENV_WORKLOAD, "").strip()
    return ""


def _file_row() -> dict[str, Any] | None:
    """``capabilities.agentcore`` object from the home file, if any.

    Peek only — do not parse_policy. GET must still render when the
    running ceiling is stale (boot-frozen) or the file is not yet loaded.
    Disabled rows are returned so Save → Off can keep a retained
    Gateway URL in the snapshot; :func:`_file_posture` decides whether
    identity is on.
    """
    path = _policy_home_path()
    if not path.is_file():
        return None
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return None
    if not isinstance(data, dict):
        return None
    caps = data.get("capabilities")
    if not isinstance(caps, dict):
        return None
    row = caps.get("agentcore")
    if not isinstance(row, dict):
        return None
    return row


def _file_posture(row: dict[str, Any] | None = None) -> str | None:
    if row is None:
        row = _file_row()
    if row is None or not row.get("enabled"):
        return None
    posture = str(row.get("posture") or "").strip().lower()
    return posture if posture in {"workload", "login"} else None


def _file_gateway_url(row: dict[str, Any] | None = None) -> str:
    if row is None:
        row = _file_row()
    if row is None:
        return ""
    raw = row.get("gateway_url")
    if not isinstance(raw, str) or not raw.strip():
        return ""
    try:
        return normalize_agentcore_gateway_url(raw)
    except ValueError:
        return ""


def _env_gateway_url() -> str:
    raw = os.environ.get(_ENV_GATEWAY_URL, "").strip()
    if not raw:
        return ""
    try:
        return normalize_agentcore_gateway_url(raw)
    except ValueError:
        return ""


def _env_posture() -> str | None:
    """Launch-env posture when no ceiling exists. Unknown values stay unset."""
    raw = os.environ.get(_ENV_POSTURE, "").strip().lower()
    return raw if raw in {"workload", "login"} else None


def _refuse_non_owner(request: web.Request, operation: str) -> web.Response | None:
    """Refuse app tokens and non-owner dashboard sessions."""
    from kiro_crew.dashboard.handlers.source_providers import (
        is_owner_dashboard_request,
        stale_owner_session_response,
    )

    if request.get("app"):
        _audit(
            request,
            operation=operation,
            outcome="denied",
            error="app tokens may not read or write AgentCore identity",
        )
        return web.json_response(
            {"error": "dashboard user required", "code": "dashboard_user_required"},
            status=403,
        )
    if not is_owner_dashboard_request(request):
        _audit(request, operation=operation, outcome="denied", error="non_owner")
        stale = stale_owner_session_response(request)
        if stale is not None:
            return stale
        return web.json_response(
            {"error": "dashboard owner required", "code": "dashboard_owner_required"},
            status=403,
        )
    return None


async def _audit_async(
    request: web.Request,
    *,
    operation: str,
    outcome: str,
    resources: str = "",
    error: str = "",
) -> None:
    """SEL first-use can mkdir; keep that off the event loop."""
    await asyncio.to_thread(
        _audit,
        request,
        operation=operation,
        outcome=outcome,
        resources=resources,
        error=error,
    )


async def _owner_gate(request: web.Request, operation: str) -> web.Response | None:
    return await asyncio.to_thread(_refuse_non_owner, request, operation)


def _document_write_reason(data: dict[str, Any]) -> str:
    """Writability of the document that will actually be rewritten."""
    identity = data.get("identity")
    if isinstance(identity, dict) and str(identity.get("signature") or "").strip():
        return "signed"
    distribution = data.get("distribution")
    if isinstance(distribution, dict) and str(distribution.get("source") or "").strip():
        return "distribution"
    return ""


def _write_reason() -> str:
    profile = getattr(current_context(), "profile", PROFILE_STANDALONE)
    if profile != PROFILE_STANDALONE:
        return "companion"
    if os.environ.get("KIROCREW_SECURITY_POLICY", "").strip():
        return "fleet_override"
    if os.environ.get("KIROCREW_POLICY_URL", "").strip():
        return "distribution"
    from kiro_crew.platform.policy_distribution import cache_only

    if cache_only():
        return "distribution"
    # A mandated-signature fleet cannot install an unsigned home
    # file and hot-apply it. The dashboard does not hold a trust key.
    if _policy_signature_required():
        return "signature_required"
    path = _policy_home_path()
    if not path.is_file():
        return ""
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return "unreadable"
    if not isinstance(data, dict):
        return "unreadable"
    return _document_write_reason(data)


def _rebuild_agent_after_apply() -> bool:
    """Rebuild so the next session/new sees the new Gateway spec.

    Returns False when the generated config is stale. The PUT must not
    answer 200 and then drop live sessions onto that leftover spec.
    """
    try:
        from kiro_crew.agent import rebuild_agent_config

        rebuild_agent_config()
    except Exception:
        logger.warning("AgentCore apply: agent config rebuild failed", exc_info=True)
        return False
    return True


async def _reset_workload_proxy() -> None:
    """Stop the process-wide SigV4 listener, if any."""
    try:
        from kiro_crew.platform.agentcore_sigv4 import reset_workload_proxy

        await asyncio.to_thread(reset_workload_proxy)
    except Exception:
        logger.warning("AgentCore apply: workload proxy reset failed", exc_info=True)


async def _drop_live_identity(request: web.Request, *, reset_proxy: bool = True) -> None:
    """Drop live ACP sessions after apply; optionally stop the SigV4 proxy.

    Rebuild only affects the next ``session/new``. Existing sessions keep the
    previously injected Gateway URL and proxy/bearer headers, so Save → Off
    awaits provider shutdown (not the fire-and-forget dashboard restart)
    before returning.

    A workload apply starts a *new* proxy during rebuild. Callers reset the
    old listener first and pass ``reset_proxy=False`` here so cleanup does
    not kill the listener the next ``session/new`` just received.
    """
    from kiro_crew.dashboard.handlers.sessions import _reset_all_sessions

    try:
        await _reset_all_sessions(request, await_shutdown=True)
    finally:
        # Session cleanup must not skip proxy revocation when asked —
        # a raise here used to leave the SigV4 listener serving the
        # old bearer after Save → Off.
        if reset_proxy:
            await _reset_workload_proxy()


def _snapshot(
    *,
    last_extra_code: str | None = None,
    runtime_applied: bool = False,
    rebuild_failed: bool = False,
) -> dict[str, Any]:
    """Display the authored posture; flag when the running ceiling is stale.

    Settings configures THIS crew's home policy. PUT hot-applies the file
    onto the running ceiling; ``runtime_applied`` means that reload
    succeeded. GET never pips. ``last_extra_code`` is a just-ran
    ``ensure_extra`` result (PUT).
    """
    ceiling = getattr(current_context(), "governance", None)
    running = agentcore_posture(ceiling)
    reason = _write_reason()
    name_env = os.environ.get(_ENV_WORKLOAD, "").strip()
    # One home-file peek: concurrent Save must not let GET stitch
    # posture from generation N onto name/URL from generation N+1.
    row = _file_row()
    if reason == "fleet_override":
        displayed = running
        source = "policy" if running else ("env" if name_env else "unset")
        restart = False
    else:
        authored = _file_posture(row)
        env_posture = _env_posture() if ceiling is None else None
        if authored is not None:
            displayed = authored
        elif running is not None:
            displayed = running
        else:
            displayed = env_posture
        if authored is not None or running is not None:
            source = "policy"
        elif displayed is not None or name_env:
            source = "env"
        else:
            source = "unset"
        restart = authored is not None and authored != running
        if runtime_applied:
            restart = False
        if rebuild_failed:
            # Ceiling matches the file, but kirocrew.json was not rebuilt.
            restart = True
    file_url = _file_gateway_url(row)
    # Launch env fills the form only when the snapshot itself is env-sourced.
    # A policy row that omitted those fields must stay empty so Save cannot
    # write stale process env into the keystone.
    if source == "env":
        name = _workload_name(allow_env=True, row=row)
        gateway_url = file_url or _env_gateway_url()
    else:
        name = _workload_name(allow_env=False, row=row)
        gateway_url = file_url
    payload: dict[str, Any] = {
        "configured": displayed is not None,
        "posture": displayed,
        "workload_name": name,
        "gateway_url": gateway_url,
        "source": source,
        "writable": reason == "",
        "write_blocked": reason or None,
        "restart_required": restart,
    }
    payload.update(extra_snapshot(last_code=last_extra_code))
    return payload


def _read_home_document() -> dict[str, Any]:
    path = _policy_home_path()
    if not path.is_file():
        return {"version": 1, "boot": dict(_MINIMAL_BOOT), "capabilities": {}}
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise PlatformCompositionError(f"security policy is unreadable: {exc}") from exc
    if not isinstance(data, dict):
        raise PlatformCompositionError("security policy top level is not an object")
    return data


def _fsync_parent(path: Path) -> None:
    """Fsync *path*'s parent so a replace/unlink survives power loss.

    No-op where a directory fd cannot be fsynced (Windows). The rename
    plus file ``fsync`` already landed; this commits the directory entry.
    """
    try:
        dir_fd = os.open(str(path.parent), os.O_RDONLY)
    except OSError:
        return
    try:
        os.fsync(dir_fd)
    except OSError:
        pass
    finally:
        os.close(dir_fd)


def _write_home_document(data: dict[str, Any]) -> None:
    parse_policy(data)
    path = _policy_home_path()
    payload = json.dumps(data, indent=2, sort_keys=False) + "\n"
    # Stage on the same sensitive leaf as the keystone. ``atomic_write``
    # uses a random sibling in the writable data-home root, which is
    # outside the floor; a crash mid-write on the live file would
    # truncate the fail-closed trust root.
    tmp = path.with_name(path.name + ".tmp")
    # Exclusive no-follow create. write_text follows a pre-planted
    # symlink and replace would then install that link as the
    # keystone. A leftover regular .tmp is an abandoned previous
    # Save (the exclusive flock is already held); reclaim it so a
    # failed replace cannot brick every later Save. Refuse a link
    # or any other pre-existing entry.
    if platform_compat.is_link_or_junction(tmp):
        raise FileExistsError(f"security policy staging path is a link: {tmp}")
    if tmp.exists():
        if tmp.is_file():
            tmp.unlink()
        else:
            raise FileExistsError(f"security policy staging path already exists: {tmp}")
    flags = (
        os.O_WRONLY
        | os.O_CREAT
        | os.O_EXCL
        | getattr(os, "O_NOFOLLOW", 0)
        | getattr(os, "O_BINARY", 0)
    )
    fd: int | None = None
    created = False
    try:
        fd = os.open(tmp, flags, 0o600)
        created = True
        # Lock down while empty so content never exists under the
        # inherited DACL. Windows 0o600 is a no-op.
        platform_compat.restrict_to_owner(tmp)
        encoded = payload.encode("utf-8")
        view = memoryview(encoded)
        while view:
            written = os.write(fd, view)
            if written == 0:
                raise OSError(
                    f"short write persisting {tmp}: os.write reported 0 bytes "
                    f"with {len(view)} of {len(encoded)} still pending"
                )
            view = view[written:]
        os.fsync(fd)
    except OSError:
        # Close before unlink: Windows refuses to delete an open handle,
        # which would leave the exclusive .tmp sitting on the floor and
        # block every later Save.
        if fd is not None:
            os.close(fd)
            fd = None
        if created:
            try:
                tmp.unlink()
            except FileNotFoundError:
                pass
        raise
    finally:
        if fd is not None:
            os.close(fd)
    previous: bytes | None = None
    if path.is_file() and not platform_compat.is_link_or_junction(path):
        try:
            previous = path.read_bytes()
        except FileNotFoundError:
            # Dest vanished between the exists check and the read.
            previous = None
        except OSError:
            # An existing keystone that cannot be snapshotted must not
            # publish: rollback would treat previous=None as a first
            # write and unlink dest.
            try:
                tmp.unlink()
            except FileNotFoundError:
                pass
            raise
    try:
        # Windows AV/indexer can hold the fresh tmp; a bare os.replace
        # then leaves the exclusive leaf and every later Save is 500.
        replace_with_retry(tmp, path)
        _fsync_parent(path)
    except OSError:
        try:
            tmp.unlink()
        except FileNotFoundError:
            pass
        raise
    # Dest inherits the tmp leaf's lockdown on POSIX; Windows replace
    # does not keep the DACL, so re-assert. atomic_write cannot be used:
    # its random sibling lands outside the sensitive-path floor.
    try:
        platform_compat.restrict_to_owner(path)
    except OSError:
        # Replace already committed. Roll back so write_failed cannot
        # leave a policy that the next restart would load.
        _rollback_replaced_home_document(path, previous)
        raise


def _rollback_replaced_home_document(path: Path, previous: bytes | None) -> None:
    """Undo a committed replace whose dest lockdown failed.

    Restore snapshotted bytes through exclusive tmp+replace — dest
    restrict is skipped: it just failed on this path. A first write
    has no previous bytes: unlink dest so a failed Save cannot leave
    the requested policy active. Unlink and in-place restore retry
    the Windows sharing-violation window and verify dest is gone or
    matches the snapshot before ``write_failed`` returns; otherwise
    a restart would load the rejected policy. If tmp+replace cannot
    land the snapshot, overwrite dest in place. Unlinking an
    existing keystone lets a restart load ungoverned defaults. The
    Save still fails ``write_failed``. Staging ``.tmp`` is unlinked
    so it cannot brick a later Save.
    """
    if previous is None:
        unlink_with_retry(path, missing_ok=True)
        if path.exists():
            raise OSError(f"rollback unlink left dest in place: {path}")
        _fsync_parent(path)
        return
    # Forward replace already consumed ``.tmp``. Reuse that same
    # floor leaf — ``.restore`` is outside ``_CREW_SECRET_LEAVES``.
    restore = path.with_name(path.name + ".tmp")
    try:
        _write_exclusive_tmp(restore, previous)
        replace_with_retry(restore, path)
        _fsync_parent(path)
        if path.read_bytes() != previous:
            raise OSError(f"rollback restore did not match snapshot: {path}")
    except OSError:
        try:
            unlink_with_retry(restore, missing_ok=True)
        except OSError:
            pass
        _restore_previous_verified(path, previous)


def _restore_previous_verified(path: Path, previous: bytes) -> None:
    """Overwrite dest with *previous* and confirm the snapshot landed."""
    last_error: OSError | None = None
    for attempt in range(_REPLACE_MAX_ATTEMPTS):
        try:
            _restore_previous_in_place(path, previous)
            if path.read_bytes() == previous:
                return
            last_error = OSError(f"rollback restore did not match snapshot: {path}")
        except OSError as exc:
            last_error = exc
            if not platform_compat.IS_WINDOWS or on_event_loop():
                raise
        if attempt + 1 >= _REPLACE_MAX_ATTEMPTS:
            break
        time.sleep(_REPLACE_BACKOFF_SECONDS)
    if last_error is not None:
        raise last_error
    raise OSError(f"rollback restore did not match snapshot: {path}")


def _restore_previous_in_place(path: Path, previous: bytes) -> None:
    """Last-resort overwrite of dest after tmp+replace restore failed."""
    flags = os.O_WRONLY | os.O_TRUNC | getattr(os, "O_NOFOLLOW", 0) | getattr(os, "O_BINARY", 0)
    fd = os.open(str(path), flags)
    try:
        view = memoryview(previous)
        while view:
            written = os.write(fd, view)
            if written == 0:
                raise OSError(
                    f"short write restoring {path}: os.write reported 0 bytes "
                    f"with {len(view)} of {len(previous)} still pending"
                )
            view = view[written:]
        os.fsync(fd)
    finally:
        os.close(fd)
    _fsync_parent(path)


def _write_exclusive_tmp(tmp: Path, encoded: bytes) -> None:
    """Owner-only exclusive create, write, fsync. Unlinks *tmp* on failure."""
    if platform_compat.is_link_or_junction(tmp):
        raise FileExistsError(f"security policy staging path is a link: {tmp}")
    if tmp.exists():
        if tmp.is_file():
            tmp.unlink()
        else:
            raise FileExistsError(f"security policy staging path already exists: {tmp}")
    flags = (
        os.O_WRONLY
        | os.O_CREAT
        | os.O_EXCL
        | getattr(os, "O_NOFOLLOW", 0)
        | getattr(os, "O_BINARY", 0)
    )
    fd: int | None = None
    created = False
    try:
        fd = os.open(tmp, flags, 0o600)
        created = True
        platform_compat.restrict_to_owner(tmp)
        view = memoryview(encoded)
        while view:
            written = os.write(fd, view)
            if written == 0:
                raise OSError(
                    f"short write persisting {tmp}: os.write reported 0 bytes "
                    f"with {len(view)} of {len(encoded)} still pending"
                )
            view = view[written:]
        os.fsync(fd)
    except OSError:
        if fd is not None:
            os.close(fd)
            fd = None
        if created:
            try:
                tmp.unlink()
            except FileNotFoundError:
                pass
        raise
    finally:
        if fd is not None:
            os.close(fd)


def _apply_posture(
    data: dict[str, Any],
    posture: str,
    *,
    gateway_url: str | None = None,
    workload_name: str | None = None,
) -> dict[str, Any]:
    caps = data.get("capabilities")
    if not isinstance(caps, dict):
        caps = {}
        data["capabilities"] = caps
    previous = caps.get("agentcore")
    if posture == "none":
        if isinstance(previous, dict):
            kept = {
                key: value for key, value in previous.items() if key not in {"enabled", "posture"}
            }
            kept["enabled"] = False
            if workload_name:
                kept["workload_name"] = workload_name
            if gateway_url:
                kept["gateway_url"] = gateway_url
            caps["agentcore"] = kept
        else:
            # Env-only identity has no home row; persist a disabled row so
            # Save → Off still revokes rather than leaving the env posture.
            # Keep operator-supplied name/URL so a later re-enable does not
            # lose the values Settings displayed from launch env.
            disabled_row: dict[str, Any] = {"enabled": False}
            if workload_name:
                disabled_row["workload_name"] = workload_name
            if gateway_url:
                disabled_row["gateway_url"] = gateway_url
            caps["agentcore"] = disabled_row
        if "boot" not in data or not isinstance(data.get("boot"), dict):
            data["boot"] = dict(_MINIMAL_BOOT)
        return data
    existing_url = ""
    existing_name = ""
    row: dict[str, Any] = {}
    if isinstance(previous, dict):
        row = {
            key: value
            for key, value in previous.items()
            if key not in {"enabled", "posture", "gateway_url", "workload_name"}
        }
        if isinstance(previous.get("gateway_url"), str):
            existing_url = previous["gateway_url"].strip()
        if isinstance(previous.get("workload_name"), str):
            existing_name = previous["workload_name"].strip()
    row["enabled"] = True
    row["posture"] = posture
    chosen_url = existing_url if gateway_url is None else gateway_url
    if chosen_url:
        row["gateway_url"] = chosen_url
    chosen_name = existing_name if workload_name is None else workload_name
    if chosen_name:
        row["workload_name"] = chosen_name
    caps["agentcore"] = row
    if "boot" not in data or not isinstance(data.get("boot"), dict):
        data["boot"] = dict(_MINIMAL_BOOT)
    return data


def _open_policy_lock(lock_path: Path) -> int:
    """Open the home-file lock without following a planted link."""
    if platform_compat.is_link_or_junction(lock_path):
        raise OSError(f"security policy lock {lock_path} is a link; refusing to lock it")
    flags = (
        os.O_RDWR
        | os.O_CREAT
        | getattr(os, "O_NOFOLLOW", 0)
        | getattr(os, "O_CLOEXEC", 0)
        | getattr(os, "O_BINARY", 0)
    )
    return os.open(lock_path, flags, 0o600)


def _persist_identity_write(
    posture: str,
    *,
    gateway_url: str | None,
    workload_name: str | None,
) -> dict[str, Any] | None:
    """Write the home file under an exclusive, non-followable lock."""
    path = _policy_home_path()
    lock_path = path.with_name(path.name + ".lock")
    lock_path.parent.mkdir(parents=True, exist_ok=True)
    fd = _open_policy_lock(lock_path)
    try:
        with platform_compat.file_lock(fd, exclusive=True, required=True):
            data = _read_home_document()
            parse_policy(data)
            if _policy_signature_required():
                raise _PolicyNotWritable("signature_required")
            blocked = _document_write_reason(data)
            if blocked:
                raise _PolicyNotWritable(blocked)
            _apply_posture(data, posture, gateway_url=gateway_url, workload_name=workload_name)
            _write_home_document(data)
    finally:
        os.close(fd)
    return None


def _unavailable_snapshot() -> dict[str, Any]:
    fallback: dict[str, Any] = {
        "configured": False,
        "posture": None,
        "workload_name": _workload_name(),
        "gateway_url": "",
        "source": "unset",
        "writable": False,
        "write_blocked": "unavailable",
        "restart_required": False,
    }
    fallback.update(extra_snapshot())
    return fallback


async def api_agentcore_identity_get(request: web.Request) -> web.Response:
    """GET /api/agentcore/identity — this crew's AgentCore identity (read)."""
    refused = await _owner_gate(request, OP_GET)
    if refused is not None:
        return refused
    try:
        payload = await asyncio.to_thread(_snapshot)
    except Exception:
        logger.warning("agentcore identity snapshot failed", exc_info=True)
        await _audit_async(request, operation=OP_GET, outcome="error", error="snapshot_failed")
        fallback = await asyncio.to_thread(_unavailable_snapshot)
        return web.json_response(fallback)
    await _audit_async(request, operation=OP_GET, outcome="success")
    return web.json_response(payload)


async def api_agentcore_identity_save(request: web.Request) -> web.Response:
    """PUT /api/agentcore/identity — set this crew's AgentCore posture.

    Owner dashboard cookie only — same gate as consent GET. App tokens and
    allow-listed messaging users are refused before the body is read: this
    writes the keystone the agent cannot touch.
    """
    refused = await _owner_gate(request, OP_SAVE)
    if refused is not None:
        return refused
    try:
        body = await request.json()
    except Exception:
        await _audit_async(request, operation=OP_SAVE, outcome="denied", resources="invalid_json")
        return web.json_response({"error": "invalid JSON", "code": "invalid_json"}, status=400)
    if not isinstance(body, dict):
        await _audit_async(
            request, operation=OP_SAVE, outcome="denied", resources="body_not_object"
        )
        return web.json_response(
            {"error": "request body must be a JSON object", "code": "invalid_json"},
            status=400,
        )
    raw = body.get("posture")
    if not isinstance(raw, str) or raw.strip().lower() not in _POSTURES:
        await _audit_async(request, operation=OP_SAVE, outcome="denied", resources="bad_posture")
        return web.json_response(
            {
                "error": "posture must be none, workload, or login",
                "code": "invalid_agentcore_posture",
            },
            status=400,
        )
    posture = raw.strip().lower()
    workload_name: str | None = None
    if "workload_name" in body:
        raw_name = body.get("workload_name")
        if raw_name is None:
            workload_name = ""
        elif not isinstance(raw_name, str):
            await _audit_async(
                request, operation=OP_SAVE, outcome="denied", resources="bad_workload_name"
            )
            return web.json_response(
                {
                    "error": "workload_name must be a workload identity name or empty",
                    "code": "invalid_agentcore_workload_name",
                },
                status=400,
            )
        else:
            try:
                workload_name = normalize_agentcore_workload_name(raw_name)
            except ValueError:
                await _audit_async(
                    request, operation=OP_SAVE, outcome="denied", resources="bad_workload_name"
                )
                return web.json_response(
                    {
                        "error": "workload_name must be 3–255 letters, digits, _ . or -",
                        "code": "invalid_agentcore_workload_name",
                    },
                    status=400,
                )
    gateway_url: str | None = None
    if "gateway_url" in body:
        raw_url = body.get("gateway_url")
        if raw_url is None:
            gateway_url = ""
        elif not isinstance(raw_url, str):
            await _audit_async(
                request, operation=OP_SAVE, outcome="denied", resources="bad_gateway_url"
            )
            return web.json_response(
                {
                    "error": "gateway_url must be an https URL or empty",
                    "code": "invalid_agentcore_gateway_url",
                },
                status=400,
            )
        else:
            try:
                gateway_url = normalize_agentcore_gateway_url(raw_url)
            except ValueError:
                await _audit_async(
                    request, operation=OP_SAVE, outcome="denied", resources="bad_gateway_url"
                )
                return web.json_response(
                    {
                        "error": "gateway_url must be an https URL without credentials",
                        "code": "invalid_agentcore_gateway_url",
                    },
                    status=400,
                )
            if gateway_url and not is_agentcore_gateway_url(gateway_url):
                await _audit_async(
                    request, operation=OP_SAVE, outcome="denied", resources="bad_gateway_url"
                )
                return web.json_response(
                    {
                        "error": "gateway_url must be an AgentCore Gateway MCP URL or empty",
                        "code": "invalid_agentcore_gateway_url",
                    },
                    status=400,
                )
    blocked = await asyncio.to_thread(_write_reason)
    if blocked:
        await _audit_async(
            request,
            operation=OP_SAVE,
            outcome="denied",
            resources=blocked,
            error="policy not writable from this gateway",
        )
        return web.json_response(
            {
                "error": "this crew's security policy cannot be edited here",
                "code": "policy_not_writable",
                "write_blocked": blocked,
            },
            status=409,
        )
    if posture in {"workload", "login"}:
        existing_name = ""
        row = await asyncio.to_thread(_file_row)
        if row is not None and isinstance(row.get("workload_name"), str):
            existing_name = str(row.get("workload_name") or "").strip()
        chosen_name = existing_name if workload_name is None else workload_name
        if not chosen_name:
            await _audit_async(
                request, operation=OP_SAVE, outcome="denied", resources="workload_name_required"
            )
            return web.json_response(
                {
                    "error": "workload_name is required when identity is on",
                    "code": "workload_name_required",
                },
                status=400,
            )
    from kiro_crew.dashboard.handlers.agents import _get_config_lock

    extra_code = None
    applied = False
    rebuilt = False
    # Failed apply, Login, and Off all revoke IAM Gateway access.
    # Successful Workload apply keeps the listener rebuild just started.
    revoking_workload = posture != "workload"
    # Persist + apply are one critical section. The home-file lock only
    # serializes the rewrite; without this, overlapping Saves can stop
    # the workload proxy the later write just started.
    async with _get_config_lock():
        try:
            noop = await asyncio.to_thread(
                _persist_identity_write,
                posture,
                gateway_url=gateway_url,
                workload_name=workload_name,
            )
            if noop is not None:
                await _audit_async(request, operation=OP_SAVE, outcome="success", resources="none")
                return web.json_response(noop)
        except _PolicyNotWritable as exc:
            await _audit_async(
                request,
                operation=OP_SAVE,
                outcome="denied",
                resources=exc.reason,
                error="policy not writable from this gateway",
            )
            return web.json_response(
                {
                    "error": "this crew's security policy cannot be edited here",
                    "code": "policy_not_writable",
                    "write_blocked": exc.reason,
                },
                status=409,
            )
        except PlatformCompositionError as exc:
            await _audit_async(request, operation=OP_SAVE, outcome="denied", error=str(exc))
            return web.json_response({"error": str(exc), "code": "invalid_policy"}, status=400)
        except OSError as exc:
            logger.warning("agentcore identity write failed", exc_info=True)
            await _audit_async(request, operation=OP_SAVE, outcome="error", error=str(exc))
            return web.json_response(
                {"error": "could not write security policy", "code": "write_failed"},
                status=500,
            )
        if posture in {"workload", "login"}:
            extra_code = await asyncio.to_thread(ensure_extra)
        applied = await asyncio.to_thread(apply_agentcore_runtime)
        # Persist already committed. Always revoke the previous
        # identity: a failed Workload apply must not leave the old
        # SigV4 proxy and sessions serving the prior bearer.
        await _reset_workload_proxy()
        rebuilt = bool(await asyncio.to_thread(_rebuild_agent_after_apply))
        # Successful workload apply starts a new listener during
        # rebuild; keep it. Failed apply, Off, and Login stop it.
        # A provider-factory reload error must not 500 after persist:
        # treat it as the existing rebuild-failed 503.
        try:
            await _drop_live_identity(
                request,
                reset_proxy=(not applied) or revoking_workload,
            )
        except Exception:
            logger.warning(
                "AgentCore apply: live session revoke failed after persist",
                exc_info=True,
            )
            rebuilt = False
        payload = await asyncio.to_thread(
            _snapshot,
            last_extra_code=extra_code,
            runtime_applied=applied and rebuilt,
            rebuild_failed=not (applied and rebuilt),
        )
    if not applied:
        await _audit_async(
            request,
            operation=OP_SAVE,
            outcome="error",
            resources=posture,
            error="runtime_apply_failed",
        )
        return web.json_response(
            {
                "error": "identity runtime apply failed; restart the gateway",
                "code": "runtime_apply_failed",
                "configured": payload.get("configured"),
                "posture": payload.get("posture"),
                "workload_name": payload.get("workload_name"),
                "gateway_url": payload.get("gateway_url"),
                "source": payload.get("source"),
                "writable": payload.get("writable"),
                "write_blocked": payload.get("write_blocked"),
                "restart_required": True,
                "extra_installed": payload.get("extra_installed"),
                "extra_code": payload.get("extra_code"),
            },
            status=503,
        )
    if applied and not rebuilt:
        await _audit_async(
            request,
            operation=OP_SAVE,
            outcome="error",
            resources=posture,
            error="agent_rebuild_failed",
        )
        return web.json_response(
            {
                "error": "agent config rebuild failed; restart the gateway",
                "code": "agent_rebuild_failed",
                "configured": payload.get("configured"),
                "posture": payload.get("posture"),
                "workload_name": payload.get("workload_name"),
                "gateway_url": payload.get("gateway_url"),
                "source": payload.get("source"),
                "writable": payload.get("writable"),
                "write_blocked": payload.get("write_blocked"),
                "restart_required": True,
                "extra_installed": payload.get("extra_installed"),
                "extra_code": payload.get("extra_code"),
            },
            status=503,
        )
    await _audit_async(request, operation=OP_SAVE, outcome="success", resources=posture)
    return web.json_response(payload)
