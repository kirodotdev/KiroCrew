"""The WeCom settings API: ``GET``/``PUT /api/wecom/config``.

WeCom (企业微信) has the same shape as the Telegram settings API with one
structural difference: it uses TWO credentials (WECOM_BOT_ID + WECOM_SECRET,
both in config_dir/.env, 0600) instead of a single bot token. Non-secret config
(enabled, allowed_users, soft_threshold_pct) lives in config.json under the
"wecom" key. GET returns masked previews + presence booleans; raw values are
write-only. The UI maps WECOM_SECRET onto the shared panel's primary secret
("bot_token") and WECOM_BOT_ID onto its second credential field ("bot_id").
"""

from __future__ import annotations

import asyncio
import json
import os
from typing import TYPE_CHECKING, Any

from aiohttp import web

if TYPE_CHECKING:
    from kiro_crew.dashboard.handlers.messaging import (
        DashboardState,
        _hot_apply_after_write,
        _LockedSectionWrite,
        _mask_secret,
        _sel,
        _threshold_pct_rejection,
        _write_env_off_loop,
        channel_restart_required,
        clean_session_folder,
        ensure_channel_folder,
        is_direct_local_request,
        live,
        read_config_text,
        run_to_completion,
        stored_folder_name,
    )


def _is_valid_wecom_userid(v: str) -> bool:
    """WeCom userid shape check (linear string ops, no regex).

    WeCom userids are 1-64 chars: ASCII letters, digits, and ``.-_@`` — the
    same charset the WeCom admin console accepts. ASCII-only on purpose:
    ``str.isalnum()`` alone would admit Unicode letters/digits, which can
    never match a real WeCom userid and would sit in the allow-list looking
    authoritative. Fail closed on anything else (whitespace, display names,
    zero-width blobs).
    """
    if not v or len(v) > 64:
        return False
    return all((ch.isascii() and ch.isalnum()) or ch in "._-@" for ch in v)


async def api_wecom_config_get(request: web.Request) -> web.Response:
    """GET /api/wecom/config — read WeCom config + masked credential status."""
    from kiro_crew.config.loader import (  # noqa: F811
        CRED_WECOM_BOT_ID,
        CRED_WECOM_SECRET,
        KiroCrewConfig,
    )

    cfg = KiroCrewConfig.load()
    creds = cfg.load_credentials()
    bot_id = creds.get(CRED_WECOM_BOT_ID, "")
    secret = creds.get(CRED_WECOM_SECRET, "")
    wc = cfg.wecom
    userids = [
        str(u.get("userid")) for u in wc.allowed_users if isinstance(u, dict) and u.get("userid")
    ]
    state: DashboardState = request.app["state"]
    return web.json_response(
        {
            # True only when the WS transport actually started this session —
            # NOT merely "credentials were present at boot".
            "connected": bool(getattr(state, "wecom_connected", False)),
            "connect_error": str(getattr(state, "wecom_connect_error", ""))[:120],
            # allowed_users is part of "configured" unless allow-all is on:
            # the transport fails closed and rejects every message while the
            # allow-list is empty (the owner fallback still needs a userid
            # entry to match on).
            "configured": bool(
                bot_id and secret and wc.enabled and (userids or wc.allow_all_users)
            ),
            # Remote sessions get a read-only view: config edits (PUT) are
            # loopback-only, so the UI disables all inputs and hides Save.
            "read_only": not is_direct_local_request(request),
            # Primary secret slot of the shared panel = WECOM_SECRET.
            "bot_token_set": bool(secret),
            "bot_token_preview": _mask_secret(secret),
            # Second credential slot = WECOM_BOT_ID.
            "bot_id_set": bool(bot_id),
            "bot_id_preview": _mask_secret(bot_id),
            "enabled": bool(wc.enabled),
            # Explicit opt-in: every org member may DM the bot (allow-list
            # bypassed). Never inferred from an empty allow-list.
            "allow_all_users": bool(wc.allow_all_users),
            # Projected for the tag editor UI; the save path re-attaches the
            # stored display names to surviving entries.
            "allowed_user_ids": userids,
            "soft_threshold_pct": int(wc.soft_threshold_pct),
            "session_folder": wc.session_folder,
        }
    )


async def api_wecom_config_save(request: web.Request) -> web.Response:
    """PUT /api/wecom/config — persist WeCom secrets (.env) + config (config.json).

    Every WeCom field is read once at gateway startup (credentials, enabled
    flag, and allow-list are consumed when ``maybe_start_wecom`` builds the
    transport), so any actual change returns ``restart_required``.

    Serialized with every other config.json writer via the repository-wide
    ``_get_config_lock()`` — this handler read-modify-writes the shared
    ``.env`` / ``config.json`` stores, so interleaving with ANY other config
    writer would silently lose writes.
    """
    # circular import: agents imports from dashboard.handlers at module load
    from kiro_crew.dashboard.handlers.agents import _get_config_lock  # noqa: F811

    async with _get_config_lock():
        # A save is a config.json + credential transaction: a cancelled request
        # (client gone, gateway shutting down) must not abandon it between its
        # phases -- see ``run_to_completion``.
        return await run_to_completion(_wecom_config_save_locked(request))


async def _wecom_config_save_locked(request: web.Request) -> web.Response:
    """Body of the WeCom save; caller holds ``_get_config_lock()``."""
    from kiro_crew.config.loader import (  # noqa: F811
        CRED_WECOM_BOT_ID,
        CRED_WECOM_SECRET,
        ConfigReadError,
        config_path,
    )

    caller = request.get("user", "dashboard")

    def _deny(msg: str, status: int = 400, *, code: str = "") -> web.Response:
        _sel().log_api_access(
            caller=caller,
            operation="wecom.config.update",
            outcome="denied",
            source="dashboard",
            error=msg,
        )
        # ``code`` is optional: most rejections in this handler are prose-only, and a
        # machine-readable code is added per field as one is retrofitted. The dashboard
        # renders ``error`` verbatim into a localized UI, so prose alone is
        # untranslatable by construction (RFC 9457 3.1.3).
        payload: dict[str, Any] = {"error": msg}
        if code:
            payload["code"] = code
        return web.json_response(payload, status=status)

    # Remote sessions are read-only: config writes are accepted only from the
    # machine running the gateway, so a remote or tunneled session (even with
    # a valid dashboard token) cannot alter WeCom access or plant credentials.
    if not is_direct_local_request(request):
        return _deny("read-only from remote sessions (local machine only)", status=403)

    try:
        body = await request.json()
    except Exception:
        return _deny("invalid JSON")
    if not isinstance(body, dict):
        return _deny("body must be an object")

    # ── Phase 1: validate everything and stage changes. No writes happen until
    # all validation passes, so a rejected field never leaves partial state. ──

    env_updates: dict[str, str | None] = {}
    # Two independent credential slots, each with the same set/clear contract
    # as the single-token channels (clear wins over a simultaneously-sent value).
    for field_key, clear_key, cred_key, label in (
        ("bot_token", "bot_token_clear", CRED_WECOM_SECRET, "bot secret"),
        ("bot_id", "bot_id_clear", CRED_WECOM_BOT_ID, "bot ID"),
    ):
        clear_flag = body.get(clear_key)
        if clear_flag is not None and not isinstance(clear_flag, bool):
            return _deny(f"{clear_key} must be a boolean")
        if clear_flag is True:
            env_updates[cred_key] = None
            continue
        raw = body.get(field_key)
        if isinstance(raw, str):
            cred_val = raw.strip()
            if cred_val.startswith(f"{cred_key}="):  # accidental env line paste
                cred_val = cred_val[len(cred_key) + 1 :].strip()
            if cred_val:
                if any(ch.isspace() for ch in cred_val):
                    return _deny(f"{label} must not contain whitespace")
                if len(cred_val) > 256:
                    return _deny(f"{label} is implausibly long")
                env_updates[cred_key] = cred_val

    # Config → config.json under "wecom" (staged, applied only after Phase 1).
    # Off-loop read: a large or slow config.json must not stall the gateway
    # event loop. Reading under _get_config_lock() keeps the snapshot current
    # relative to every other config writer.
    path = config_path()

    def _read_config() -> dict:
        data = json.loads(read_config_text(path)) if path.exists() else {}
        if not isinstance(data, dict):
            raise ValueError("config.json is not a JSON object")
        return data

    try:
        data = await asyncio.to_thread(_read_config)
    except Exception:
        return _deny("config.json is corrupt", status=500)
    if not isinstance(data.get("wecom"), dict):
        # Back-compat: seed from a COPY of the legacy "wechat" section (the
        # config key was renamed) so an existing install's allow-list /
        # thresholds / ws_url survive the first dashboard save instead of
        # being reset. Copy so the legacy block is never mutated in place.
        legacy = data.get("wechat")
        data["wecom"] = dict(legacy) if isinstance(legacy, dict) else {}
    wc_cfg = data["wecom"]
    staged: dict[str, object] = {}
    applied: list[str] = []

    if "enabled" in body:
        val = body.get("enabled")
        if not isinstance(val, bool):
            return _deny("enabled must be a boolean")
        if val != bool(wc_cfg.get("enabled", False)):
            staged["enabled"] = val
            applied.append("enabled")

    if "allow_all_users" in body:
        val = body.get("allow_all_users")
        if not isinstance(val, bool):
            return _deny("allow_all_users must be a boolean")
        if val != bool(wc_cfg.get("allow_all_users", False)):
            staged["allow_all_users"] = val
            applied.append("allow_all_users")

    if "allowed_user_ids" in body:
        raw_ids = body.get("allowed_user_ids")
        if not isinstance(raw_ids, list):
            return _deny("allowed_user_ids must be a list")
        # Preserve stored display names for entries that survive the edit —
        # the UI round-trips only userids, but ``{userid, name}`` is the
        # canonical config shape consumed by the transport allow-list.
        existing = {
            str(u.get("userid")): u
            for u in wc_cfg.get("allowed_users", [])
            if isinstance(u, dict) and u.get("userid")
        }
        new_users: list[dict] = []
        seen: set[str] = set()
        for item in raw_ids:
            s = str(item).strip()
            if not s:
                continue
            if not _is_valid_wecom_userid(s):
                return _deny(f"invalid WeCom userid: {s}")
            if s in seen:
                continue
            seen.add(s)
            new_users.append(existing.get(s) or {"userid": s, "name": ""})
        if new_users != list(wc_cfg.get("allowed_users", [])):
            staged["allowed_users"] = new_users
            applied.append("allowed_users")

    bad_pct = _threshold_pct_rejection(body, "soft_threshold_pct")
    if bad_pct is not None:
        return _deny(bad_pct[1], code=bad_pct[0])
    if "soft_threshold_pct" in body:
        pct = int(body["soft_threshold_pct"])
        if pct != int(wc_cfg.get("soft_threshold_pct", 80)):
            staged["soft_threshold_pct"] = pct
            applied.append("soft_threshold_pct")

    if "session_folder" in body:
        try:
            new_folder = clean_session_folder(body.get("session_folder"))
        except ValueError as exc:
            return _deny(str(exc))
        if new_folder != str(wc_cfg.get("session_folder", "") or ""):
            staged["session_folder"] = new_folder
            applied.append("session_folder")

    # No Phase 1.5 credential verification: validating WeCom credentials
    # requires opening the AI-bot WebSocket long-connection (no cheap REST
    # "whoami" like Telegram's getMe), so credentials are stored as given and
    # the status badge reports the truth after the next gateway restart.

    # ── Phase 2: commit. All validation passed, so writes are safe. ──
    # Through ``update_config_locked``: it holds the advisory lock on the sidecar
    # ``<path>.lock`` across the whole read-modify-write, so a writer in ANOTHER
    # PROCESS cannot land between our read and our write, and the staged keys are
    # merged into the file as re-read inside that lock. The same object undoes
    # exactly those keys if the .env write below fails.
    _cfg_write = _LockedSectionWrite(path, "wecom", staged, seed_from="wechat")
    # Hold the live-config watcher for the whole config+credential transaction:
    # the two files commit separately and a failed .env write rolls the config
    # back, so nothing between here and the end of the block may be applied to
    # the running transport (a widened allow-list must never go live on a save
    # that then fails). The release wakes the watcher on the committed state.
    with live.hold():
        if staged:
            wc_cfg.update(staged)
            # Off-loop: file IO, and it may wait on another holder of the lock.
            try:
                await _cfg_write.commit()
            except ConfigReadError:
                return _deny("config.json is corrupt", status=500)

        # Create the configured session folder now, on this user-initiated save,
        # so the reconcile path never has to write the folder store. Best-effort:
        # a failure leaves conversations unfiled until the next save.
        _folder_name = stored_folder_name(wc_cfg.get("session_folder"))
        if _folder_name:
            _state = request.app.get("state")
            if _state is not None:
                await ensure_channel_folder(
                    _state,
                    "wecom",
                    _folder_name,
                    relabel="session_folder" in staged,
                )
        if env_updates:
            # Off-loop: the .env write is blocking file IO (lock, temp write,
            # owner-only lockdown, replace) and must not block the event loop.
            #
            # Cancellation guard: see Teams save for the full rationale. Only
            # roll config back when the .env write actually failed, not when
            # cancellation arrived after the write already committed.
            _env_write_task_wc: asyncio.Task[None] = asyncio.ensure_future(
                _write_env_off_loop(env_updates)
            )
            try:
                await asyncio.shield(_env_write_task_wc)
            except asyncio.CancelledError:
                await asyncio.gather(_env_write_task_wc, return_exceptions=True)
                _env_exc_wc = (
                    _env_write_task_wc.exception() if not _env_write_task_wc.cancelled() else None
                )
                if _env_exc_wc is not None:
                    if staged:
                        await _cfg_write.rollback("WeCom")
                raise
            except BaseException:
                # Roll config back so a failed .env write cannot leave the NEW
                # metadata paired with the OLD credentials on disk.
                if staged:
                    await _cfg_write.rollback("WeCom")
                raise
            # Keep the live process environment in sync with the new .env state
            # (load_credentials() lets os.environ win over .env — see the Slack
            # save handler for the full rationale).
            for key, new_val in env_updates.items():
                if new_val is None:
                    os.environ.pop(key, None)
                else:
                    os.environ[key] = new_val

    _sel().log_api_access(
        caller=caller,
        operation="wecom.config.update",
        outcome="ok",
        source="dashboard",
        resources=",".join(applied + list(env_updates.keys())),
    )
    # The entire WeCom channel config is read once at gateway startup.
    # Answer only once the watcher has applied the write: a narrowed allow-list
    # is in force before the caller sees "saved", not one poll interval later.
    await _hot_apply_after_write()
    return web.json_response(
        {
            "ok": True,
            "restart_required": channel_restart_required(
                "wecom", staged.keys(), env_updates=env_updates
            ),
            "verify_warning": "",
        }
    )
