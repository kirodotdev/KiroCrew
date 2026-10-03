"""The iMessage settings API: ``GET /api/imessage/config`` and the body of its
``PUT``, whose route stays in the facade.

The only channel with NO credential to manage: the transport is the operator's
own signed-in Messages.app, reached through a local bridge process, so there is
nothing to mask, verify against a vendor, or write to .env. Everything lives in
config.json under the "imessage" key, and the whole section is read once at
gateway startup (only session_folder reloads live).
"""

from __future__ import annotations

import asyncio
import functools
from typing import TYPE_CHECKING

from aiohttp import web

if TYPE_CHECKING:
    from kiro_crew.dashboard.handlers.messaging import (
        IMESSAGE_SERVICES,
        IS_MACOS,
        DashboardState,
        KiroCrewConfig,
        _clean_id_list,
        _hot_apply_after_write,
        _is_valid_webex_email,
        _sel,
        channel_restart_required,
        clean_session_folder,
        config_path,
        ensure_channel_folder,
        is_direct_local_request,
        stored_folder_name,
    )


def _is_valid_imessage_handle(v: str) -> bool:
    """Accept an Apple ID email or a phone-shaped handle.

    Linear string ops, no regex: the same polynomial-backtracking concern that
    shaped ``_is_valid_webex_email`` applies to any pattern run over an
    operator-supplied list.

    A phone handle may carry the punctuation people actually type, spaces
    included -- ``normalize_handle`` strips it before any comparison, so
    rejecting ``+1 (555) 123-4567`` here would refuse a handle the transport
    treats as identical to the digits-only form.
    """
    if not v or len(v) > 254:
        return False
    if "@" in v:
        return _is_valid_webex_email(v)
    body = v[1:] if v.startswith("+") else v
    digits = [ch for ch in body if ch.isdigit()]
    # Anything outside digits and dialling punctuation is rejected, so a stray
    # identifier cannot be smuggled in as a "phone" and silently authorized.
    if any(not (ch.isdigit() or ch in "()-. ") for ch in body):
        return False
    return 4 <= len(digits) <= 18


def _clean_imessage_path(raw: object, label: str) -> str:
    """Validate an operator-supplied filesystem path for the bridge.

    The value becomes ``argv[0]`` (or a ``--db-path`` argument) of a spawned
    child. It is passed to ``create_subprocess_exec``, never a shell, so quoting
    is not the risk -- but a newline or NUL would corrupt the argv and is
    rejected rather than silently truncated.
    """
    if raw is None:
        return ""
    if not isinstance(raw, str):
        raise ValueError(f"{label} must be a string")
    value = raw.strip()
    if any(ch in value for ch in ("\n", "\r", "\x00")):
        raise ValueError(f"{label} must not contain line breaks")
    if len(value) > 4096:
        raise ValueError(f"{label} is too long")
    return value


async def api_imessage_config_get(request: web.Request) -> web.Response:
    """GET /api/imessage/config — read the iMessage channel config."""
    cfg = KiroCrewConfig.load()
    state: DashboardState = request.app["state"]
    return web.json_response(
        {
            # True only while the bridge's watch is actually live this session —
            # NOT merely "enabled in config". Kept truthful by the client's
            # on_state_change observer (see maybe_start_imessage).
            "connected": bool(getattr(state, "imessage_connected", False)),
            "connect_error": str(getattr(state, "imessage_connect_error", ""))[:120],
            "configured": bool(cfg.imessage.enabled and cfg.imessage.allowed_handles),
            # The UI explains the macOS-only requirement instead of leaving the
            # operator to infer it from a channel that silently never connects.
            "supported": bool(IS_MACOS),
            # Remote sessions get a read-only view: config edits (PUT) are
            # loopback-only, so the UI disables all inputs and hides Save.
            "read_only": not is_direct_local_request(request),
            "enabled": cfg.imessage.enabled,
            # No bridge path is exposed: the executable is resolved in code,
            # never from agent-writable config. See imessage.bridge_path.
            "db_path": cfg.imessage.db_path,
            "allowed_handles": list(cfg.imessage.allowed_handles),
            "service": cfg.imessage.service,
            "session_folder": cfg.imessage.session_folder,
        }
    )


async def _imessage_config_save(request: web.Request) -> web.Response:
    """Body of the iMessage save; runs to completion once started."""
    from kiro_crew.config.loader import (  # noqa: F811
        ConfigReadError,
        update_config_locked,
    )

    caller = request.get("user", "dashboard")

    def _audit_denial(msg: str) -> None:
        _sel().log_api_access(
            caller=caller,
            operation="imessage.config.update",
            outcome="denied",
            source="dashboard",
            error=msg,
        )

    def _deny(code: str, msg: str) -> web.Response:
        """Reject a bad request. ``code`` is the contract, ``msg`` is advisory.

        The status is a literal rather than a parameter so this lands in the
        error-code gate's CHECKED bucket: a computed status puts a response in
        the unverifiable ``dynamic_status`` escape hatch, which the gate caps
        precisely because hoisting a status out of view looks like refactoring.
        The dashboard renders ``error`` verbatim into a localized UI, so prose
        alone would be untranslatable by construction (RFC 9457 3.1.3).
        """
        _audit_denial(msg)
        return web.json_response({"error": msg, "code": code}, status=400)

    # Remote sessions are read-only (same gate as every other channel config
    # API): a remote or tunneled session cannot widen who may reach the agent.
    if not is_direct_local_request(request):
        message = "read-only from remote sessions (local machine only)"
        _audit_denial(message)
        return web.json_response({"error": message, "code": "remote_read_only"}, status=403)

    try:
        body = await request.json()
    except Exception:
        return _deny("invalid_json", "invalid JSON")
    if not isinstance(body, dict):
        return _deny("body_not_object", "body must be an object")

    # ── Phase 1: validate everything and stage changes (no partial writes).
    # The current config.json is NOT read here; the authoritative
    # read-modify-write happens entirely under the config lock in Phase 2.
    staged: dict[str, object] = {}

    if "enabled" in body:
        val = body.get("enabled")
        if not isinstance(val, bool):
            return _deny("enabled_not_bool", "enabled must be a boolean")
        staged["enabled"] = val

    if "allowed_handles" in body:
        try:
            staged["allowed_handles"] = _clean_id_list(
                body.get("allowed_handles"), _is_valid_imessage_handle, "handle"
            )
        except ValueError as exc:
            return _deny("invalid_handle", str(exc))

    if "service" in body:
        val = body.get("service")
        if not isinstance(val, str) or val.strip().lower() not in IMESSAGE_SERVICES:
            return _deny("invalid_service", "service must be one of: imessage, sms, auto")
        staged["service"] = val.strip().lower()

    for key in ("db_path",):
        if key in body:
            try:
                staged[key] = _clean_imessage_path(body.get(key), key)
            except ValueError as exc:
                return _deny("invalid_path", str(exc))

    if "session_folder" in body:
        try:
            staged["session_folder"] = clean_session_folder(body.get("session_folder"))
        except ValueError as exc:
            return _deny("invalid_session_folder", str(exc))

    # ── Phase 2: commit. The read-modify-write of config.json happens ENTIRELY
    # under the repo-wide config lock (read fresh, merge only the imessage
    # section, write atomic), so a concurrent save by another settings handler
    # is never overwritten by a stale snapshot taken before the lock.
    #
    # Through ``update_config_locked``, not ``_atomic_json_write``: it holds an
    # advisory lock on the sidecar ``<path>.lock`` for the entire read-modify-write,
    # so a concurrent ``kirocrew config set`` in ANOTHER PROCESS cannot land between
    # our read and our write. ``_get_config_lock()`` serializes writers inside this
    # process only, and loader.py names that combination the required path for a
    # config.json mutation.
    from kiro_crew.dashboard.handlers.agents import _get_config_lock  # noqa: F811

    applied: list[str] = []
    async with _get_config_lock():
        path = config_path()
        session_folder = ""

        def _apply_staged(fresh: dict) -> dict | None:
            """Merge the staged iMessage fields into the config read inside the lock.

            Returns ``None`` when nothing changed, which tells
            ``update_config_locked`` to skip the write -- preserving the previous
            behaviour of not touching config.json on a no-op save.
            """
            nonlocal applied, session_folder
            if not isinstance(fresh.get("imessage"), dict):
                fresh["imessage"] = {}
            imessage_cfg = fresh["imessage"]

            # Reduce staged fields to actual changes against the fresh read so
            # restart_required stays truthful on no-op saves.
            changes: dict[str, object] = {}
            if "enabled" in staged and staged["enabled"] != bool(
                imessage_cfg.get("enabled", False)
            ):
                changes["enabled"] = staged["enabled"]
            if "allowed_handles" in staged and staged["allowed_handles"] != imessage_cfg.get(
                "allowed_handles", []
            ):
                changes["allowed_handles"] = staged["allowed_handles"]
            for key, default in (("service", "imessage"), ("db_path", "")):
                if key in staged and staged[key] != str(imessage_cfg.get(key, default) or default):
                    changes[key] = staged[key]
            if "session_folder" in staged and staged["session_folder"] != str(
                imessage_cfg.get("session_folder", "") or ""
            ):
                changes["session_folder"] = staged["session_folder"]
            applied = list(changes.keys())

            imessage_cfg.update(changes)
            # Read AFTER the merge and inside the lock: the folder below must be
            # created for the value that was actually committed, not for a
            # pre-merge snapshot.
            session_folder = str(imessage_cfg.get("session_folder", "") or "")
            return fresh if changes else None

        # Shield + drain so a cancellation arriving mid-write cannot
        # release the config lock while the worker thread is still
        # replacing the file (interleaved-write race).
        _cfg_write_task_im: asyncio.Task[dict] = asyncio.ensure_future(
            asyncio.to_thread(functools.partial(update_config_locked, path, mutate=_apply_staged))
        )
        try:
            await asyncio.shield(_cfg_write_task_im)
        except asyncio.CancelledError:
            await asyncio.gather(_cfg_write_task_im, return_exceptions=True)
            raise
        except ConfigReadError:
            message = "config.json is corrupt"
            _audit_denial(message)
            return web.json_response({"error": message, "code": "config_corrupt"}, status=500)

        # Create the configured session folder now, on this user-initiated save,
        # so the reconcile path never has to write the folder store. Best-effort:
        # a failure leaves conversations unfiled until the next save.
        _folder_name = stored_folder_name(session_folder)
        if _folder_name:
            _state = request.app.get("state")
            if _state is not None:
                await ensure_channel_folder(
                    _state,
                    "imessage",
                    _folder_name,
                    relabel="session_folder" in applied,
                )

    _sel().log_api_access(
        caller=caller,
        operation="imessage.config.update",
        outcome="ok",
        source="dashboard",
        resources=",".join(applied),
    )
    # The entire iMessage channel config is read once at gateway startup.
    # Answer only once the watcher has applied the write: a narrowed allow-list
    # is in force before the caller sees "saved", not one poll interval later.
    await _hot_apply_after_write()
    return web.json_response(
        {
            "ok": True,
            "restart_required": channel_restart_required("imessage", applied),
            "verify_warning": "",
        }
    )
