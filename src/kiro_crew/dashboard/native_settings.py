"""Owner-confirmed native Windows routing changes through the existing restart.

Nothing is staged as an executable or arbitrary document. The bounded, validated
MCP handler runs again after strict retirement, under an exclusive publication
permit, and the successor's ordinary rebuild/broker boot activates the result.
"""

from __future__ import annotations

import asyncio
import contextlib
import hashlib
import json
import os
import secrets
import time
from collections.abc import Awaitable, Callable
from typing import Any

from aiohttp import web

from kiro_crew import windows_settings
from kiro_crew.platform_compat_wintree import (
    block_windows_tree_starts,
    retry_pending_windows_process_trees,
    unblock_windows_tree_starts,
    windows_tree_admissions_pending,
)


async def _mutate(fn, /, *args, **kwargs):
    """Do not release a settings lease while its mutating worker still runs."""
    from kiro_crew.dashboard.chat_utils_awaits import run_to_completion

    return await run_to_completion(asyncio.to_thread(fn, *args, **kwargs))


def _digest(body: dict, operation: str = "") -> str:
    data = {
        key: value
        for key, value in body.items()
        if key not in {"restart_native_sessions", "native_settings_confirmation_token"}
    }
    return hashlib.sha256(
        json.dumps([operation, data], sort_keys=True, separators=(",", ":")).encode()
    ).hexdigest()


def _needed() -> bool:
    from kiro_crew.config import KiroCrewConfig

    return KiroCrewConfig.load().agent.acp_backend == "codex" or windows_settings.pins_present()


def _counts(state: Any) -> dict[str, int]:
    callback = getattr(state, "_native_settings_work_counts", None)
    try:
        if not callable(callback):
            return {"turns": 0, "background": 1}
        turns, background = callback()
        return {"turns": int(turns), "background": int(background)}
    except Exception:
        return {"turns": 0, "background": 1}


async def maybe_restart(
    request: web.Request,
    body: dict,
    operation: Callable[[], Awaitable[web.Response]],
) -> web.Response | None:
    """Called AFTER owner/body validation and BEFORE any approval/config mutation."""
    if os.name != "nt" or windows_settings.permitted():
        return None
    if not await _mutate(_needed):
        return None
    state = request.app["state"]
    task = getattr(state, "_native_settings_task", None)
    if task is not None and not task.done():
        return web.json_response(
            {
                "ok": False,
                "code": "native_settings_transition_in_progress",
                "status": "restarting",
                "saved": False,
                "active": False,
            },
            status=409,
        )
    digest = _digest(body, request.path)
    current = await asyncio.to_thread(windows_settings.snapshot)
    confirmations = getattr(state, "_native_settings_confirmations", {})
    confirmations = {
        token: row for token, row in confirmations.items() if row["expires"] > time.monotonic()
    }
    token = body.get("native_settings_confirmation_token")
    approved = confirmations.get(token) if isinstance(token, str) else None
    if body.get("restart_native_sessions") is not True or approved is None:
        token = secrets.token_urlsafe(24)
        # Bounded, process-local owner consent; no key, command or credential
        # document is persisted. Bind the exact change and starting versions.
        confirmations = dict(list(confirmations.items())[-15:])
        confirmations[token] = {
            "digest": digest,
            "base": current,
            "expires": time.monotonic() + 600,
        }
        setattr(state, "_native_settings_confirmations", confirmations)
        result = windows_settings.NativeSettingsRestartRequired().response()
        result.update({"confirmation_token": token, "work": _counts(state)})
        return web.json_response(result, status=409)
    if approved["digest"] != digest or approved["base"] != current:
        return web.json_response(
            {
                "ok": False,
                "code": "native_settings_confirmation_stale",
                "status": "restart_required",
                "saved": False,
                "active": False,
                "action": "refresh_and_review",
            },
            status=409,
        )
    # Snapshot I/O yielded. Claim/revalidate without another await so two
    # separately confirmed requests cannot both acquire the same fence owner.
    task = getattr(state, "_native_settings_task", None)
    if task is not None and not task.done():
        return web.json_response(
            {"ok": False, "code": "native_settings_transition_in_progress"}, status=409
        )
    if getattr(state, "_gateway_restart_in_progress", False):
        return web.json_response({"ok": False, "code": "gateway_restart_in_progress"}, status=409)
    restart_task = getattr(state, "_gateway_restart_task", None)
    if restart_task is not None and not restart_task.done():
        return web.json_response({"ok": False, "code": "gateway_restart_in_progress"}, status=409)
    confirmations.pop(token)
    setattr(state, "_native_settings_confirmations", confirmations)
    previous_fence = getattr(state, "_native_settings_fence", None)
    owner = previous_fence or object()
    if not block_windows_tree_starts(owner):
        return web.json_response(
            {"ok": False, "code": "native_settings_transition_in_progress"}, status=409
        )
    setattr(state, "_native_settings_fence", owner)
    setattr(state, "_native_settings_stop_started", False)
    setattr(state, "_native_settings_failure", None)
    transition_id = secrets.token_hex(16)
    task = asyncio.create_task(
        _apply(state, operation, current, digest, owner, transition_id, previous_fence is not None),
        name="native-routing-restart",
    )
    setattr(state, "_native_settings_task", task)
    state._background_tasks.add(task)
    task.add_done_callback(state._background_tasks.discard)
    return web.json_response(
        {
            "ok": True,
            "status": "restarting",
            "saved": False,
            "active": False,
            "restart_required": False,
            "transition_id": transition_id,
        }
    )


async def _apply(
    state: Any,
    operation: Callable[[], Awaitable[web.Response]],
    base: dict,
    digest: str,
    owner: object,
    transition_id: str,
    previous_fence: bool = False,
) -> None:
    from kiro_crew.config import KiroCrewConfig, live
    from kiro_crew.dashboard.handlers.updates import _restart_gateway

    phase = {
        "status": "stopping",
        "saved": False,
        "active": False,
        "starts_blocked": True,
        "request_digest": digest,
        "failed_step": None,
        "transition_id": transition_id,
    }
    step = "claim"
    operation_started = False
    claimed = False
    try:
        phase = await _mutate(windows_settings.claim_transition, phase)
        claimed = True
        step = "stop"
        # Flush the HTTP response before the existing restart can replace us.
        await asyncio.sleep(0.25)

        async def commit() -> None:
            nonlocal step, phase, operation_started
            # Manager teardown precedes this hook. The physical reservation
            # registry includes founding and already-draining trees, not just
            # roots whose providers still sit in the visible session registry.
            await wait_retirement()
            async with windows_settings.async_write_transaction():
                step = "validate_versions"
                if await asyncio.to_thread(windows_settings.snapshot) != base:
                    raise OSError("Settings changed while runtime retirement was pending")
                step = "save"
                from kiro_crew.dashboard.chat_utils_awaits import run_to_completion

                operation_started = True
                response = await run_to_completion(operation())
                after = await asyncio.to_thread(windows_settings.snapshot)
                phase["saved"] = after != base
                if response.status >= 400:
                    raise OSError("The validated routing mutation was refused")
                cfg = await _mutate(KiroCrewConfig.load)
                phase.update(
                    {
                        "status": "saved",
                        "saved": True,
                        "routing": sorted(cfg.mcp_gateway.stub_servers),
                        "sharing": cfg.mcp_gateway.enabled,
                        "documents": await asyncio.to_thread(windows_settings.snapshot),
                    }
                )
                await _mutate(
                    windows_settings.record_status, phase, expected_transition=transition_id
                )

        with live.hold():
            restarted = await _restart_gateway(state, before_exec=commit, strict_cleanup=True)
        if not restarted:
            raise OSError("Gateway restart was refused")
    except (Exception, asyncio.CancelledError):
        blocked = bool(
            previous_fence
            or phase.get("prior_admission_blocked")
            or getattr(state, "_native_settings_stop_started", False)
        )
        try:
            after = await asyncio.to_thread(windows_settings.snapshot)
            phase["saved"] = bool(phase["saved"] or operation_started and after != base)
            if phase["saved"]:
                cfg = await _mutate(KiroCrewConfig.load)
                phase.update(
                    {
                        "documents": after,
                        "routing": sorted(cfg.mcp_gateway.stub_servers),
                        "sharing": cfg.mcp_gateway.enabled,
                    }
                )
        except OSError:
            phase["saved"] = None  # No claim when the persisted state cannot be read.
        # No invented rollback: persisted settings remain saved, while native
        # admission stays shut until a successful rebuild/activation retry.
        phase.update(
            {
                "status": "activation_failed",
                "active": False,
                "starts_blocked": blocked,
                "failed_step": step,
                "action": "restart_and_retry" if blocked else "review_restart_target",
            }
        )
        setattr(state, "_native_settings_failure", dict(phase))
        try:
            from kiro_crew.dashboard.chat_utils_awaits import run_to_completion

            if claimed:
                await run_to_completion(
                    asyncio.to_thread(
                        windows_settings.record_status, phase, expected_transition=transition_id
                    )
                )
        except (OSError, asyncio.CancelledError):
            # The API also exposes the process-local terminal state if status
            # publication itself fails; never leave a silently polling task.
            pass
        finally:
            if not blocked:
                unblock_windows_tree_starts(owner)
                if getattr(state, "_native_settings_fence", None) is owner:
                    setattr(state, "_native_settings_fence", None)


async def wait_retirement(*, timeout: float = 30) -> None:
    deadline = asyncio.get_running_loop().time() + timeout
    while windows_tree_admissions_pending():
        if asyncio.get_running_loop().time() >= deadline:
            raise OSError("Windows runtime retirement is unconfirmed")
        # close_all stopped its maintenance task. Continue only the existing
        # owned receipts, including a prior pass that ended drain-pending.
        await asyncio.to_thread(retry_pending_windows_process_trees, 1)
        await asyncio.sleep(0.05)


@contextlib.asynccontextmanager
async def activation_boot(cfg: Any):
    """Fence the normal rebuild/factory/broker boot as one settings generation."""
    if os.name != "nt":
        yield
        return
    record = await asyncio.to_thread(windows_settings.status)
    if not record.get("starts_blocked"):
        yield
        return
    stack = contextlib.AsyncExitStack()
    acquired = False
    try:
        await stack.enter_async_context(windows_settings.async_write_transaction())
        acquired = True
        record = await asyncio.to_thread(windows_settings.with_owner_identity, record)
        await _mutate(
            windows_settings.record_status, record, expected_transition=record.get("transition_id")
        )
        await _validate_activation(cfg, record)
    except (Exception, asyncio.CancelledError):
        record.update(
            {
                "status": "activation_failed",
                "active": False,
                "starts_blocked": True,
                "failed_step": "validate_versions",
                "action": "restart_and_review",
            }
        )
        try:
            if acquired:
                await _mutate(
                    windows_settings.record_status,
                    record,
                    expected_transition=record.get("transition_id"),
                )
        finally:
            await stack.aclose()
        raise
    async with stack:
        try:
            yield
        finally:
            # Bootstrap's own rebuild can legitimately canonicalize model state.
            # Keep the next retry bound to that owned generation, including a
            # failed broker boot; unrelated writers remain excluded throughout.
            final = await asyncio.to_thread(windows_settings.status)
            if final.get("transition_id") == record.get("transition_id"):
                final["documents"] = await asyncio.to_thread(windows_settings.snapshot)
                with contextlib.suppress(windows_settings.NativeSettingsSuperseded):
                    await _mutate(
                        windows_settings.record_status,
                        final,
                        expected_transition=record.get("transition_id"),
                    )


async def _validate_activation(cfg: Any, record: dict) -> None:
    from kiro_crew.config import KiroCrewConfig

    if (
        record.get("documents") is not None
        and await asyncio.to_thread(windows_settings.snapshot) != record["documents"]
    ):
        raise OSError("Settings no longer match the confirmed generation")
    fresh = await _mutate(KiroCrewConfig.load)
    if fresh.to_dict() != cfg.to_dict():
        raise OSError("Loaded settings no longer match the current generation")


async def finish_activation(
    cfg: Any, manager: Any, served: frozenset[str], *, refused: frozenset[str] = frozenset()
) -> None:
    """Successor boot: report ACTIVE only after rebuild and verified broker boot."""
    if os.name != "nt":
        return
    record = await asyncio.to_thread(windows_settings.status)
    if not record.get("starts_blocked"):
        return
    from kiro_crew.agent import rebuild_agent_config

    boot_permit = windows_settings.permitted()
    async with windows_settings.async_write_transaction():
        try:
            # activation_boot already verified the pre-rebuild documents. A
            # direct/recovery caller must validate before acquiring authority.
            if record.get("saved") and (
                record.get("routing") != sorted(served)
                or record.get("sharing") != cfg.mcp_gateway.enabled
            ):
                raise OSError("Persisted routing no longer matches the confirmed change")
            if not boot_permit:
                await _validate_activation(cfg, record)
            from kiro_crew.mcp_gateway.launch_approval import target_stem

            # Configured follower names do not grant optional tools. Only a
            # declared launch refused by this rewrite blocks its activation.
            if refused & {target_stem(name) for name in served}:
                raise OSError("A declared MCP launch requires operator approval")
            await _mutate(rebuild_agent_config, refresh_forks=True)
            if served and (manager is None or not manager.is_running or not await manager.ping()):
                raise OSError("MCP broker activation was not confirmed")
            record.update(
                {
                    "status": "active",
                    "active": True,
                    "starts_blocked": False,
                    "routing": sorted(served),
                    "sharing": cfg.mcp_gateway.enabled,
                    "documents": await asyncio.to_thread(windows_settings.snapshot),
                    "failed_step": None,
                    "action": None,
                }
            )
            await _mutate(
                windows_settings.record_status,
                record,
                expected_transition=record.get("transition_id"),
            )
        except Exception:
            record.update(
                {
                    "status": "activation_failed",
                    "active": False,
                    "starts_blocked": True,
                    "failed_step": "broker_activation",
                    "action": "restart_and_retry",
                }
            )
            with contextlib.suppress(windows_settings.NativeSettingsSuperseded):
                await _mutate(
                    windows_settings.record_status,
                    record,
                    expected_transition=record.get("transition_id"),
                )


async def recover_current_settings(transition_id: str) -> None:
    """Explicit restart activates current saved files; it never replays a change."""
    from kiro_crew.config import KiroCrewConfig

    await wait_retirement()
    async with windows_settings.async_write_transaction():
        cfg = await _mutate(KiroCrewConfig.load)
        record = await asyncio.to_thread(windows_settings.status)
        record.update(
            {
                "status": "saved",
                "saved": True,
                "active": False,
                "starts_blocked": True,
                "routing": sorted(cfg.mcp_gateway.stub_servers),
                "sharing": cfg.mcp_gateway.enabled,
                "documents": await asyncio.to_thread(windows_settings.snapshot),
            }
        )
        await _mutate(windows_settings.record_status, record, expected_transition=transition_id)
