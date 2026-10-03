"""The Telegram settings API: ``GET``/``PUT /api/telegram/config``.

The bot token lives in config_dir/.env as TELEGRAM_BOT_TOKEN (0600), with
config.json's telegram.bot_token as a legacy fallback. Non-secret config
(enabled, allowed_user_ids, soft_threshold_pct) lives in config.json under
the "telegram" key. GET returns a masked preview + presence boolean; raw
token values are write-only (rotate at @BotFather if ever needed).
"""

from __future__ import annotations

import asyncio
import json
import os
from typing import TYPE_CHECKING, Any

from aiohttp import web

if TYPE_CHECKING:
    from kiro_crew.dashboard.handlers.messaging import (
        _TELEGRAM_TOKEN_RE,
        _TOKEN_VERIFY_TIMEOUT,
        TELEGRAM_ACTIVATIONS,
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


async def _validate_telegram_token(token: str) -> str | None:
    """Check a pasted bot token against Telegram before it is stored.

    Uses ``getMe`` — the cheapest authenticated Bot API call. Returns ``None``
    when Telegram accepts the token, or Telegram's error description (e.g.
    ``Unauthorized``) when it rejects it. Network failures propagate to the
    caller, which treats them as "unverifiable" rather than invalid — saves
    must not be blocked by being offline.
    """
    import aiohttp  # noqa: F811

    timeout = aiohttp.ClientTimeout(total=_TOKEN_VERIFY_TIMEOUT)
    async with aiohttp.ClientSession(timeout=timeout) as session:
        async with session.get(f"https://api.telegram.org/bot{token}/getMe") as resp:
            data = await resp.json(content_type=None)
            if isinstance(data, dict) and data.get("ok"):
                return None
            desc = ""
            if isinstance(data, dict):
                desc = str(data.get("description", "") or "")
            return (desc or "rejected")[:60]


async def api_telegram_config_get(request: web.Request) -> web.Response:
    """GET /api/telegram/config — read Telegram config + masked secret status."""
    from kiro_crew.config.loader import (  # noqa: F811
        CRED_TELEGRAM_BOT_TOKEN,
        KiroCrewConfig,
    )

    cfg = KiroCrewConfig.load()
    creds = cfg.load_credentials()
    token = creds.get(CRED_TELEGRAM_BOT_TOKEN, "") or cfg.telegram.bot_token
    tg = cfg.telegram
    state: DashboardState = request.app["state"]
    return web.json_response(
        {
            # True only when the long-polling transport actually started this
            # session — NOT merely "a token was present at boot".
            "connected": bool(getattr(state, "telegram_connected", False)),
            "connect_error": str(getattr(state, "telegram_connect_error", ""))[:120],
            # allowed_user_ids is part of "configured": the transport fails
            # closed and rejects every message while the allowlist is empty.
            "configured": bool(token and tg.enabled and tg.allowed_user_ids),
            # Remote sessions get a read-only view: config edits (PUT) are
            # loopback-only, so the UI disables all inputs and hides Save.
            "read_only": not is_direct_local_request(request),
            "bot_token_set": bool(token),
            "bot_token_preview": _mask_secret(token),
            "enabled": bool(tg.enabled),
            # Serialized as strings for the tag editor UI; the save path
            # accepts digit strings and stores canonical ints.
            "allowed_user_ids": [str(u) for u in tg.allowed_user_ids],
            "soft_threshold_pct": int(tg.soft_threshold_pct),
            "show_thinking": bool(tg.show_thinking),
            "voice_replies": bool(tg.voice_replies),
            "session_folder": tg.session_folder,
            # Forum per-topic config. chat_ids are serialized as strings for
            # the tag editor UI; they are NEGATIVE (e.g. "-1001234567890"),
            # so the save path accepts a leading minus (not a digits-only check).
            "allow_forum": bool(tg.allow_forum),
            "allowed_forum_chat_ids": [str(c) for c in tg.allowed_forum_chat_ids],
            "forum_activation": tg.forum_activation,
        }
    )


async def api_telegram_config_save(request: web.Request) -> web.Response:
    """PUT /api/telegram/config — persist Telegram secret (.env) + config (config.json).

    Every Telegram field is read once at gateway startup (token, enabled flag,
    allowlist are consumed in the orchestrator's constructor), so any actual
    change returns ``restart_required`` for the UI hint.

    Serialized with every other config.json writer via the repository-wide
    ``_get_config_lock()`` (also used by the MCP, memory, and agent
    handlers) — this handler read-modify-writes the shared ``.env`` /
    ``config.json`` stores, so interleaving with ANY other config writer
    (including the Slack save) would silently lose writes.
    """
    # circular import: agents imports from dashboard.handlers at module load
    from kiro_crew.dashboard.handlers.agents import _get_config_lock  # noqa: F811

    async with _get_config_lock():
        # A save is a config.json + credential transaction: a cancelled request
        # (client gone, gateway shutting down) must not abandon it between its
        # phases -- see ``run_to_completion``.
        return await run_to_completion(_telegram_config_save_locked(request))


async def _telegram_config_save_locked(request: web.Request) -> web.Response:
    """Body of the Telegram save; caller holds ``_get_config_lock()``."""
    from kiro_crew.config.loader import (  # noqa: F811
        CRED_TELEGRAM_BOT_TOKEN,
        ConfigReadError,
        config_path,
    )

    caller = request.get("user", "dashboard")

    def _deny(msg: str, status: int = 400, *, code: str = "") -> web.Response:
        """Refuse with *msg*, and with a machine-readable *code* when one is given.

        ``code`` is optional so the existing denials keep their exact bodies, and is
        added per field as one is retrofitted. It exists because backend-owned
        strings have no i18n catalog path: the dashboard renders ``error`` verbatim
        into a localized UI, so prose alone is untranslatable by construction (RFC
        9457 3.1.3) and a caller reacting to a specific refusal would have to match
        on that prose. New denials should supply one.
        """
        _sel().log_api_access(
            caller=caller,
            operation="telegram.config.update",
            outcome="denied",
            source="dashboard",
            error=msg,
        )
        payload: dict[str, Any] = {"error": msg}
        if code:
            payload["code"] = code
        return web.json_response(payload, status=status)

    # Remote sessions are read-only: config writes are accepted only from the
    # machine running the gateway, so a remote or tunneled session (even with
    # a valid dashboard token) cannot alter Telegram access or plant tokens.
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
        env_updates[CRED_TELEGRAM_BOT_TOKEN] = None
    else:
        raw = body.get("bot_token")
        if isinstance(raw, str):
            tok = raw.strip()
            if tok.startswith(f"{CRED_TELEGRAM_BOT_TOKEN}="):  # accidental env line
                tok = tok[len(CRED_TELEGRAM_BOT_TOKEN) + 1 :].strip()
            if tok:
                if any(ch.isspace() for ch in tok):
                    return _deny("bot_token must not contain whitespace")
                if not _TELEGRAM_TOKEN_RE.match(tok):
                    return _deny("bot_token must look like <bot_id>:<secret> from @BotFather")
                env_updates[CRED_TELEGRAM_BOT_TOKEN] = tok

    # Config → config.json under "telegram" (staged, applied only after Phase 1).
    # Off-loop read: a large or slow config.json must not stall the gateway
    # event loop (chat, heartbeats). Reading under _get_config_lock() keeps
    # the snapshot current relative to every other config writer.
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
    if not isinstance(data.get("telegram"), dict):
        data["telegram"] = {}
    tg_cfg = data["telegram"]
    staged: dict[str, object] = {}
    applied: list[str] = []

    if "enabled" in body:
        val = body.get("enabled")
        if not isinstance(val, bool):
            return _deny("enabled must be a boolean")
        if val != bool(tg_cfg.get("enabled", False)):
            staged["enabled"] = val
            applied.append("enabled")

    if "allowed_user_ids" in body:
        raw_ids = body.get("allowed_user_ids")
        if not isinstance(raw_ids, list):
            return _deny("allowed_user_ids must be a list")
        new_ids: list[int] = []
        for item in raw_ids:
            s = str(item).strip()
            if not s:
                continue
            if not s.isdigit():
                return _deny(f"invalid Telegram user ID: {s} (numeric IDs only)")
            uid = int(s)
            if uid not in new_ids:
                new_ids.append(uid)
        if new_ids != list(tg_cfg.get("allowed_user_ids", [])):
            staged["allowed_user_ids"] = new_ids
            applied.append("allowed_user_ids")

    bad_pct = _threshold_pct_rejection(body, "soft_threshold_pct")
    if bad_pct is not None:
        return _deny(bad_pct[1], code=bad_pct[0])
    if "soft_threshold_pct" in body:
        pct = int(body["soft_threshold_pct"])
        if pct != int(tg_cfg.get("soft_threshold_pct", 80)):
            staged["soft_threshold_pct"] = pct
            applied.append("soft_threshold_pct")

    if "show_thinking" in body:
        val = body.get("show_thinking")
        # Strict bool, like every other toggle on this handler: a truthy string
        # would silently enable a per-turn extra message nobody asked for.
        if not isinstance(val, bool):
            return _deny("show_thinking must be a boolean")
        if val != bool(tg_cfg.get("show_thinking", False)):
            staged["show_thinking"] = val
            applied.append("show_thinking")

    if "voice_replies" in body:
        val = body.get("voice_replies")
        # Strict bool for the same reason as show_thinking: a truthy string would
        # silently start uploading synthesized audio, one extra message per turn.
        if not isinstance(val, bool):
            return _deny("voice_replies must be a boolean")
        if val != bool(tg_cfg.get("voice_replies", False)):
            staged["voice_replies"] = val
            applied.append("voice_replies")

    if "forum_activation" in body:
        val = body.get("forum_activation")
        # Validated against the closed set HERE rather than left to the loader's
        # degrade-to-always: the loader's fallback exists for a config file edited
        # by hand, and silently storing an unusable value the operator picked in a
        # dropdown would report success for a setting that never took effect.
        if not isinstance(val, str) or val not in TELEGRAM_ACTIVATIONS:
            return _deny(
                "forum_activation must be one of " + ", ".join(sorted(TELEGRAM_ACTIVATIONS)),
                code="invalid_forum_activation",
            )
        if val != str(tg_cfg.get("forum_activation", "always") or "always"):
            staged["forum_activation"] = val
            applied.append("forum_activation")

    if "session_folder" in body:
        try:
            new_folder = clean_session_folder(body.get("session_folder"))
        except ValueError as exc:
            return _deny(str(exc))
        if new_folder != str(tg_cfg.get("session_folder", "") or ""):
            staged["session_folder"] = new_folder
            applied.append("session_folder")

    if "allow_forum" in body:
        val = body.get("allow_forum")
        if not isinstance(val, bool):
            return _deny("allow_forum must be a boolean")
        if val != bool(tg_cfg.get("allow_forum", False)):
            staged["allow_forum"] = val
            applied.append("allow_forum")

    if "allowed_forum_chat_ids" in body:
        raw_chat_ids = body.get("allowed_forum_chat_ids")
        if not isinstance(raw_chat_ids, list):
            return _deny("allowed_forum_chat_ids must be a list")
        new_chat_ids: list[int] = []
        for item in raw_chat_ids:
            s = str(item).strip()
            if not s:
                continue
            # Forum supergroup chat_ids are NEGATIVE (e.g. -1001234567890),
            # so accept an optional leading minus — the digits-only check used
            # for allowed_user_ids would wrongly reject every group id here.
            digits = s[1:] if s.startswith("-") else s
            if not digits.isdigit():
                return _deny(f"invalid Telegram chat ID: {s} (integer IDs only)")
            cid = int(s)
            if cid not in new_chat_ids:
                new_chat_ids.append(cid)
        if new_chat_ids != list(tg_cfg.get("allowed_forum_chat_ids", [])):
            staged["allowed_forum_chat_ids"] = new_chat_ids
            applied.append("allowed_forum_chat_ids")

    # Whenever the .env token is set or cleared, also drop the legacy
    # config.json ``telegram.bot_token`` fallback. The gateway (and GET above)
    # fall back to that field when .env is empty, so leaving it behind would
    # resurrect a removed credential on the next restart — an explicit clear
    # must actually revoke access, and a replacement must not shadow-keep the
    # old token. Staged here (write happens only in Phase 2).
    if CRED_TELEGRAM_BOT_TOKEN in env_updates and tg_cfg.get("bot_token"):
        tg_cfg.pop("bot_token", None)
        applied.append("legacy_bot_token_removed")

    # ── Phase 1.5: verify a newly pasted token against Telegram before storing.
    # A token Telegram rejects fails the save right here, where the user can
    # act on it. Network failure is NOT a rejection: the save proceeds with a
    # warning so being offline never blocks config.
    verify_warning = ""
    pending_tok = env_updates.get(CRED_TELEGRAM_BOT_TOKEN)
    if pending_tok:
        try:
            tg_err = await _validate_telegram_token(pending_tok)
        except Exception:
            verify_warning = (
                "Telegram was unreachable, so the token was saved without verification."
            )
        else:
            if tg_err:
                return _deny(f"bot_token rejected by Telegram ({tg_err})")

    # ── Phase 2: commit. All validation passed, so writes are safe. Order
    # matters for crash safety: config.json — which carries the legacy
    # ``bot_token`` fallback removal — is persisted FIRST, so there is no
    # failure window in which .env was already cleared but the legacy
    # fallback survives to silently resurrect the revoked credential on
    # restart. The inverse failure mode (config written, then a crash before
    # the .env update) is benign and visible: the .env token remains exactly
    # as GET reports it, and re-running the save completes the operation. ──
    # The purge is decided against the document the write lands on, not the
    # snapshot: whenever this save updates the credential, a legacy ``bot_token``
    # a concurrent writer landed after our read is dropped too, so the cleared
    # ``.env`` slot cannot leave a fallback behind. Absent, the drop is a no-op
    # and the write is skipped.
    purge_legacy_token = CRED_TELEGRAM_BOT_TOKEN in env_updates
    if staged or purge_legacy_token:
        tg_cfg.update(staged)
        # Through ``update_config_locked``: it holds the advisory lock on the
        # sidecar ``<path>.lock`` across the whole read-modify-write, so a writer
        # in ANOTHER PROCESS cannot land between our read and our write, and the
        # staged keys (plus the legacy ``bot_token`` purge) are merged into the
        # file as re-read inside that lock. Off-loop: file IO, and it may wait on
        # another holder of the lock.
        try:
            await _LockedSectionWrite(
                path, "telegram", staged, drop_keys=("bot_token",) if purge_legacy_token else ()
            ).commit()
        except ConfigReadError:
            return _deny("config.json is corrupt", status=500)

    # Create the configured session folder now, on this user-initiated save,
    # so the reconcile path never has to write the folder store. Best-effort:
    # a failure leaves conversations unfiled until the next save.
    _folder_name = stored_folder_name(tg_cfg.get("session_folder"))
    if _folder_name:
        _state = request.app.get("state")
        if _state is not None:
            await ensure_channel_folder(
                _state,
                "telegram",
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
        operation="telegram.config.update",
        outcome="ok",
        source="dashboard",
        resources=",".join(applied + list(env_updates.keys())),
    )
    # All Telegram fields are boot-read: token/enabled/allowlist are consumed
    # in the orchestrator's constructor and the dispatcher is built at boot.
    # Answer only once the watcher has applied the write: a narrowed allow-list
    # is in force before the caller sees "saved", not one poll interval later.
    await _hot_apply_after_write()
    return web.json_response(
        {
            "ok": True,
            "restart_required": channel_restart_required(
                "telegram", staged.keys(), env_updates=env_updates
            ),
            "verify_warning": verify_warning,
        }
    )
