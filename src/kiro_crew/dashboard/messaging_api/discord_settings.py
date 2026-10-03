"""The Discord settings API: ``GET``/``PUT /api/discord/config``.

The bot token lives in config_dir/.env as DISCORD_BOT_TOKEN (0600), with
config.json's discord.bot_token as a legacy fallback. Non-secret config
(enabled, allowed_user_ids, allowed_thread_ids, soft_threshold_pct) lives
in config.json under the "discord" key. GET returns a masked preview +
presence boolean; raw token values are write-only (reset at the Developer
Portal if ever needed).
"""

from __future__ import annotations

import json
import os
from typing import TYPE_CHECKING, Any

from aiohttp import web

if TYPE_CHECKING:
    from kiro_crew.dashboard.handlers.messaging import (
        _DISCORD_TOKEN_RE,
        _TOKEN_VERIFY_TIMEOUT,
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
        read_config_text,
        run_to_completion,
        stored_folder_name,
    )


async def _validate_discord_token(token: str) -> str | None:
    """Check a pasted bot token against Discord before it is stored.

    Uses ``GET /users/@me`` — the cheapest authenticated REST call. Returns
    ``None`` when Discord accepts the token, or Discord's error message when
    it rejects it. Network failures propagate to the caller, which treats
    them as "unverifiable" rather than invalid — saves must not be blocked by
    being offline.
    """
    import aiohttp  # noqa: F811

    timeout = aiohttp.ClientTimeout(total=_TOKEN_VERIFY_TIMEOUT)
    async with aiohttp.ClientSession(timeout=timeout) as session:
        async with session.get(
            "https://discord.com/api/v10/users/@me",
            headers={"Authorization": f"Bot {token}"},
        ) as resp:
            if 200 <= resp.status < 300:
                return None
            desc = ""
            try:
                data = await resp.json(content_type=None)
                if isinstance(data, dict):
                    desc = str(data.get("message", "") or "")
            except Exception:
                pass
            return (desc or f"HTTP {resp.status}")[:60]


async def api_discord_config_get(request: web.Request) -> web.Response:
    """GET /api/discord/config — read Discord config + masked secret status."""
    from kiro_crew.config.loader import (  # noqa: F811
        CRED_DISCORD_BOT_TOKEN,
        KiroCrewConfig,
    )

    cfg = KiroCrewConfig.load()
    creds = cfg.load_credentials()
    token = creds.get(CRED_DISCORD_BOT_TOKEN, "") or cfg.discord.bot_token
    dc = cfg.discord
    state: DashboardState = request.app["state"]
    return web.json_response(
        {
            # True only when the Gateway WebSocket transport actually started
            # this session — NOT merely "a token was present at boot".
            "connected": bool(getattr(state, "discord_connected", False)),
            "connect_error": str(getattr(state, "discord_connect_error", ""))[:120],
            # allowed_user_ids is part of "configured": the transport fails
            # closed and rejects every message while the allowlist is empty.
            "configured": bool(token and dc.enabled and dc.allowed_user_ids),
            # Remote sessions get a read-only view: config edits (PUT) are
            # loopback-only, so the UI disables all inputs and hides Save.
            "read_only": not is_direct_local_request(request),
            "bot_token_set": bool(token),
            "bot_token_preview": _mask_secret(token),
            "enabled": bool(dc.enabled),
            "allowed_user_ids": [str(u) for u in dc.allowed_user_ids],
            "allowed_thread_ids": [str(t) for t in dc.allowed_thread_ids],
            "allowed_channel_ids": [str(c) for c in dc.allowed_channel_ids],
            "auto_thread": bool(dc.auto_thread),
            "reactions_enabled": bool(dc.reactions_enabled),
            "show_thinking": bool(dc.show_thinking),
            "soft_threshold_pct": int(dc.soft_threshold_pct),
            "session_folder": dc.session_folder,
        }
    )


async def api_discord_config_save(request: web.Request) -> web.Response:
    """PUT /api/discord/config — persist Discord secret (.env) + config (config.json).

    Every Discord field is read once at gateway startup (token, enabled flag,
    allowlist are consumed in the orchestrator's constructor), so any actual
    change returns ``restart_required`` for the UI hint.

    Serialized with every other config.json writer via the repository-wide
    ``_get_config_lock()`` (also used by the MCP, memory, and agent
    handlers) — this handler read-modify-writes the shared ``.env`` /
    ``config.json`` stores, so interleaving with ANY other config writer
    (including the Slack and Telegram saves) would silently lose writes.
    """
    # circular import: agents imports from dashboard.handlers at module load
    from kiro_crew.dashboard.handlers.agents import _get_config_lock  # noqa: F811

    async with _get_config_lock():
        # A save is a config.json + credential transaction: a cancelled request
        # (client gone, gateway shutting down) must not abandon it between its
        # phases -- see ``run_to_completion``.
        return await run_to_completion(_discord_config_save_locked(request))


async def _discord_config_save_locked(request: web.Request) -> web.Response:
    """Body of the Discord save; caller holds ``_get_config_lock()``."""
    from kiro_crew.config.loader import (  # noqa: F811
        CRED_DISCORD_BOT_TOKEN,
        ConfigReadError,
        config_path,
    )

    caller = request.get("user", "dashboard")

    def _deny(msg: str, status: int = 400, *, code: str = "") -> web.Response:
        _sel().log_api_access(
            caller=caller,
            operation="discord.config.update",
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
    # a valid dashboard token) cannot alter Discord access or plant tokens.
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
    clear_flag = body.get("bot_token_clear")
    if clear_flag is not None and not isinstance(clear_flag, bool):
        return _deny("bot_token_clear must be a boolean")
    if clear_flag is True:
        env_updates[CRED_DISCORD_BOT_TOKEN] = None
    else:
        raw = body.get("bot_token")
        if isinstance(raw, str):
            tok = raw.strip()
            if tok.startswith(f"{CRED_DISCORD_BOT_TOKEN}="):  # accidental env line
                tok = tok[len(CRED_DISCORD_BOT_TOKEN) + 1 :].strip()
            if tok.startswith("Bot "):  # accidental Authorization-header prefix
                tok = tok[4:].strip()
            if tok:
                if any(ch.isspace() for ch in tok):
                    return _deny("bot_token must not contain whitespace")
                if not _DISCORD_TOKEN_RE.match(tok):
                    return _deny(
                        "bot_token must be the bot token from the Discord "
                        "Developer Portal (Bot page → Reset Token)"
                    )
                env_updates[CRED_DISCORD_BOT_TOKEN] = tok

    # Config → config.json under "discord" (staged, applied only after Phase 1).
    path = config_path()
    try:
        data = json.loads(read_config_text(path)) if path.exists() else {}
        if not isinstance(data, dict):
            raise ValueError("config.json is not a JSON object")
    except Exception:
        return _deny("config.json is corrupt", status=500)
    if not isinstance(data.get("discord"), dict):
        data["discord"] = {}
    dc_cfg = data["discord"]
    staged: dict[str, object] = {}
    applied: list[str] = []

    if "enabled" in body:
        val = body.get("enabled")
        if not isinstance(val, bool):
            return _deny("enabled must be a boolean")
        if val != bool(dc_cfg.get("enabled", False)):
            staged["enabled"] = val
            applied.append("enabled")

    if "allowed_user_ids" in body:
        raw_ids = body.get("allowed_user_ids")
        if not isinstance(raw_ids, list):
            return _deny("allowed_user_ids must be a list")
        new_ids: list[str] = []
        for item in raw_ids:
            s = str(item).strip()
            if not s:
                continue
            # Discord user IDs are numeric snowflakes (17-20 digits today;
            # accept any all-digit string to stay future-proof).
            if not s.isdigit():
                return _deny(f"invalid Discord user ID: {s} (numeric IDs only)")
            if s not in new_ids:
                new_ids.append(s)
        if new_ids != [str(u) for u in dc_cfg.get("allowed_user_ids", [])]:
            staged["allowed_user_ids"] = new_ids
            applied.append("allowed_user_ids")

    if "allowed_thread_ids" in body:
        raw_ids = body.get("allowed_thread_ids")
        if not isinstance(raw_ids, list):
            return _deny("allowed_thread_ids must be a list")
        new_ids = []
        for item in raw_ids:
            s = str(item).strip()
            if not s:
                continue
            if not s.isdigit():
                return _deny(f"invalid Discord thread ID: {s} (numeric IDs only)")
            if s not in new_ids:
                new_ids.append(s)
        if new_ids != [str(t) for t in dc_cfg.get("allowed_thread_ids", [])]:
            staged["allowed_thread_ids"] = new_ids
            applied.append("allowed_thread_ids")

    if "allowed_channel_ids" in body:
        raw_ids = body.get("allowed_channel_ids")
        if not isinstance(raw_ids, list):
            return _deny("allowed_channel_ids must be a list")
        new_ids = []
        for item in raw_ids:
            s = str(item).strip()
            if not s:
                continue
            if not s.isdigit():
                return _deny(f"invalid Discord channel ID: {s} (numeric IDs only)")
            if s not in new_ids:
                new_ids.append(s)
        if new_ids != [str(c) for c in dc_cfg.get("allowed_channel_ids", [])]:
            staged["allowed_channel_ids"] = new_ids
            applied.append("allowed_channel_ids")

    if "auto_thread" in body:
        val = body.get("auto_thread")
        if not isinstance(val, bool):
            return _deny("auto_thread must be a boolean")
        if val != bool(dc_cfg.get("auto_thread", True)):
            staged["auto_thread"] = val
            applied.append("auto_thread")

    bad_pct = _threshold_pct_rejection(body, "soft_threshold_pct")
    if bad_pct is not None:
        return _deny(bad_pct[1], code=bad_pct[0])
    if "soft_threshold_pct" in body:
        pct = int(body["soft_threshold_pct"])
        if pct != int(dc_cfg.get("soft_threshold_pct", 80)):
            staged["soft_threshold_pct"] = pct
            applied.append("soft_threshold_pct")

    # Both render toggles are read per turn by the dispatcher, not at boot, so a
    # change takes effect on the next message. `channel_restart_required` keeps
    # `restart_required` honest about that; promising a restart the user does not
    # need is how a settings page trains people to restart for everything.
    for toggle in ("reactions_enabled", "show_thinking"):
        if toggle in body:
            val = body.get(toggle)
            if not isinstance(val, bool):
                return _deny(f"{toggle} must be a boolean")
            if val != bool(dc_cfg.get(toggle, toggle == "reactions_enabled")):
                staged[toggle] = val
                applied.append(toggle)

    if "session_folder" in body:
        try:
            new_folder = clean_session_folder(body.get("session_folder"))
        except ValueError as exc:
            return _deny(str(exc))
        if new_folder != str(dc_cfg.get("session_folder", "") or ""):
            staged["session_folder"] = new_folder
            applied.append("session_folder")

    # Whenever the .env token is set or cleared, also drop the legacy
    # config.json ``discord.bot_token`` fallback. The gateway (and GET above)
    # fall back to that field when .env is empty, so leaving it behind would
    # resurrect a removed credential on the next restart — an explicit clear
    # must actually revoke access, and a replacement must not shadow-keep the
    # old token. It also sits in agent-readable ``config.json``, so the copy is
    # worth strictly less than the .env one it shadows. Staged here (write
    # happens only in Phase 2), matching the Telegram and Webex saves.
    if CRED_DISCORD_BOT_TOKEN in env_updates and dc_cfg.get("bot_token"):
        dc_cfg.pop("bot_token", None)
        applied.append("legacy_bot_token_removed")

    # ── Phase 1.5: verify a newly pasted token against Discord before storing.
    # A token Discord rejects fails the save right here, where the user can
    # act on it. Network failure is NOT a rejection: the save proceeds with a
    # warning so being offline never blocks config.
    verify_warning = ""
    pending_tok = env_updates.get(CRED_DISCORD_BOT_TOKEN)
    if pending_tok:
        try:
            dc_err = await _validate_discord_token(pending_tok)
        except Exception:
            verify_warning = "Discord was unreachable, so the token was saved without verification."
        else:
            if dc_err:
                return _deny(f"bot_token rejected by Discord ({dc_err})")

    # ── Phase 2: commit. All validation passed, so writes are safe. Order
    # matters for crash safety: config.json — which carries the legacy
    # ``bot_token`` fallback removal — is persisted FIRST, so there is no
    # failure window in which .env was already cleared but the legacy fallback
    # survives to silently resurrect the revoked credential on restart. The
    # inverse failure mode (config written, then a crash before the .env
    # update) is benign and visible: the .env token remains exactly as GET
    # reports it, and re-running the save completes the operation. ──
    # The purge is decided against the document the write lands on, not the
    # snapshot: whenever this save updates the credential, a legacy ``bot_token``
    # a concurrent writer landed after our read is dropped too, so the cleared
    # ``.env`` slot cannot leave a fallback behind. Absent, the drop is a no-op
    # and the write is skipped.
    purge_legacy_token = CRED_DISCORD_BOT_TOKEN in env_updates
    if staged or purge_legacy_token:
        dc_cfg.update(staged)
        # Through ``update_config_locked``: it holds the advisory lock on the
        # sidecar ``<path>.lock`` across the whole read-modify-write, so a writer
        # in ANOTHER PROCESS cannot land between our read and our write, and the
        # staged keys (plus the legacy ``bot_token`` purge) are merged into the
        # file as re-read inside that lock. Off-loop: file IO, and it may wait on
        # another holder of the lock.
        try:
            await _LockedSectionWrite(
                path, "discord", staged, drop_keys=("bot_token",) if purge_legacy_token else ()
            ).commit()
        except ConfigReadError:
            return _deny("config.json is corrupt", status=500)

    # Create the configured session folder now, on this user-initiated save,
    # so the reconcile path never has to write the folder store. Best-effort:
    # a failure leaves conversations unfiled until the next save.
    _folder_name = stored_folder_name(dc_cfg.get("session_folder"))
    if _folder_name:
        _state = request.app.get("state")
        if _state is not None:
            await ensure_channel_folder(
                _state,
                "discord",
                _folder_name,
                relabel="session_folder" in staged,
            )
    if env_updates:
        # Off-loop: the .env write is blocking file IO (lock, temp write,
        # owner-only lockdown, replace) and must not block the event loop.
        await _write_env_off_loop(env_updates, config_kept=True)
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
        operation="discord.config.update",
        outcome="ok",
        source="dashboard",
        resources=",".join(applied + list(env_updates.keys())),
    )
    # The restart hint is answered from the schema: the token is hoisted from the
    # environment at connect time, so a credential write still needs a restart;
    # the config fields are applied by the watcher above.
    # Answer only once the watcher has applied the write: a narrowed allow-list
    # is in force before the caller sees "saved", not one poll interval later.
    await _hot_apply_after_write()
    return web.json_response(
        {
            "ok": True,
            "restart_required": channel_restart_required(
                "discord", staged.keys(), env_updates=env_updates
            ),
            "verify_warning": verify_warning,
        }
    )
