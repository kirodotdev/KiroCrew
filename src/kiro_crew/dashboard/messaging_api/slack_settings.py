"""The Slack settings API: ``GET``/``PUT /api/slack/config`` and the app manifest."""

from __future__ import annotations

import json
import os
import re
from typing import TYPE_CHECKING

from aiohttp import web

if TYPE_CHECKING:
    from kiro_crew.dashboard.handlers.messaging import (
        _SLACK_SECRET_FIELDS,
        _TOKEN_VERIFY_TIMEOUT,
        DashboardState,
        _clean_id_list,
        _hot_apply_after_write,
        _LockedSectionWrite,
        _mask_secret,
        _sel,
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


async def _validate_slack_token(key: str, token: str) -> str | None:
    """Check a pasted token against Slack before it is stored.

    Bot tokens are checked with ``auth.test``; app-level tokens with
    ``apps.connections.open`` (the same call the gateway makes at startup, so
    a token that passes here will connect at boot). Returns ``None`` when
    Slack accepts the token, or Slack's error code (e.g. ``invalid_auth``)
    when it rejects it. Network failures propagate to the caller, which
    treats them as "unverifiable" rather than invalid — saves must not be
    blocked by being offline.
    """
    from slack_sdk.errors import SlackApiError
    from slack_sdk.web.async_client import AsyncWebClient

    client = AsyncWebClient(token=token, timeout=_TOKEN_VERIFY_TIMEOUT)
    try:
        if key == "SLACK_APP_TOKEN":
            await client.apps_connections_open(app_token=token)
        else:
            await client.auth_test()
        return None
    except SlackApiError as exc:
        try:
            return str(exc.response.get("error", "") or "rejected")[:60]
        except Exception:
            return "rejected"


async def api_slack_config_get(request: web.Request) -> web.Response:
    """GET /api/slack/config — read Slack config + masked secret status."""
    from kiro_crew.config.loader import (  # noqa: F811
        CRED_OWNER_ID,
        CRED_SLACK_APP_TOKEN,
        CRED_SLACK_BOT_TOKEN,
        KiroCrewConfig,
    )

    cfg = KiroCrewConfig.load()
    creds = cfg.load_credentials()
    bot = creds.get(CRED_SLACK_BOT_TOKEN, "")
    app = creds.get(CRED_SLACK_APP_TOKEN, "")
    owner = creds.get(CRED_OWNER_ID, "")
    slack = cfg.slack
    state: DashboardState = request.app["state"]
    return web.json_response(
        {
            # True only when the socket-mode connect succeeded this session —
            # NOT merely "tokens were present at boot" (see DashboardState).
            "connected": bool(getattr(state, "slack_socket_connected", False)),
            # Short reason from the failed connect attempt ("invalid_auth",
            # a network error class name, or "" when connected / untried).
            "connect_error": str(getattr(state, "slack_connect_error", ""))[:120],
            "configured": bool(bot and app and owner),
            # Remote sessions get a read-only view: config edits (PUT) are
            # loopback-only, so the UI disables all inputs and hides Save.
            "read_only": not is_direct_local_request(request),
            "bot_token_set": bool(bot),
            "app_token_set": bool(app),
            "bot_token_preview": _mask_secret(bot),
            "app_token_preview": _mask_secret(app),
            "owner_id": owner,
            "command": slack.command,
            # allowed_users / open_channels are deliberately NOT exposed: the
            # runtime enforces owner-only access in this build (is_allowed_user
            # ignores both), so surfacing editors would create access rules
            # that are never honored. Re-add when multi-user Slack lands.
            "allowed_enterprise_ids": list(slack.allowed_enterprise_ids),
            "reactions_enabled": slack.reactions_enabled,
            "show_thinking": slack.show_thinking,
            "session_folder": slack.session_folder,
        }
    )


async def api_slack_config_save(request: web.Request) -> web.Response:
    """PUT /api/slack/config — persist Slack secrets (.env) + config (config.json).

    Token/owner changes need a gateway restart to reconnect Slack (creds are
    read at gateway startup); the response returns ``restart_required`` so the
    UI can surface a hint. Config-only changes take effect on the next message
    or restart.

    Serialized with every other config.json writer via the repository-wide
    ``_get_config_lock()`` (also used by the MCP, memory, and agent
    handlers) — this handler read-modify-writes the shared ``.env`` /
    ``config.json`` stores, so interleaving with ANY other config writer
    (including the Discord and Telegram saves) would silently lose writes.
    """
    # circular import: agents imports from dashboard.handlers at module load
    from kiro_crew.dashboard.handlers.agents import _get_config_lock  # noqa: F811

    async with _get_config_lock():
        # A save is a config.json + credential transaction: a cancelled request
        # (client gone, gateway shutting down) must not abandon it between its
        # phases -- see ``run_to_completion``.
        return await run_to_completion(_slack_config_save_locked(request))


async def _slack_config_save_locked(request: web.Request) -> web.Response:
    """Body of the Slack save; caller holds ``_get_config_lock()``."""
    from kiro_crew.config.loader import (  # noqa: F811
        CRED_OWNER_ID,
        ConfigReadError,
        config_path,
    )
    from kiro_crew.validation import USER_ID_RE  # noqa: F811

    caller = request.get("user", "dashboard")

    def _deny(msg: str, status: int = 400) -> web.Response:
        _sel().log_api_access(
            caller=caller,
            operation="slack.config.update",
            outcome="denied",
            source="dashboard",
            error=msg,
        )
        return web.json_response({"error": msg}, status=status)

    # Remote sessions are read-only: like /reveal, config writes are accepted
    # only from the machine running the gateway, so a remote or tunneled
    # session (even with a valid dashboard token) cannot alter Slack access
    # or plant new tokens.
    if not is_direct_local_request(request):
        return _deny("read-only from remote sessions (local machine only)", status=403)

    try:
        body = await request.json()
    except Exception:
        return _deny("invalid JSON")
    if not isinstance(body, dict):
        return _deny("body must be an object")

    # ── Phase 1: validate everything and stage changes. No writes happen until
    # all validation passes, so a rejected field never leaves partial state
    # (e.g. a token persisted while a bad channel ID 400s). ──

    # Secrets → .env (empty/omitted token = leave unchanged; explicit clear via
    # *_clear flag to avoid accidentally wiping a token on save).
    env_updates: dict[str, str | None] = {}
    for field_name, key in _SLACK_SECRET_FIELDS.items():
        clear_flag = body.get(f"{field_name}_clear")
        if clear_flag is not None and not isinstance(clear_flag, bool):
            return _deny(f"{field_name}_clear must be a boolean")
        if clear_flag is True:
            env_updates[key] = None
            continue
        raw = body.get(field_name)
        if isinstance(raw, str):
            tok = raw.strip()
            if tok.startswith(f"{key}="):  # strip an accidentally pasted env line
                tok = tok[len(key) + 1 :].strip()
            if tok:
                if any(ch.isspace() for ch in tok):
                    return _deny(f"{field_name} must not contain whitespace")
                env_updates[key] = tok

    if "owner_id" in body:
        owner = str(body.get("owner_id", "")).strip()
        if owner and not USER_ID_RE.match(owner):
            return _deny("owner_id must be a Slack member ID (starts with U or W)")
        # Only stage a real change: the UI sends the field on every save, and
        # staging an unchanged value would flag restart_required on every
        # config-only save.
        current_owner = os.environ.get(CRED_OWNER_ID, "").strip()
        if owner != current_owner:
            env_updates[CRED_OWNER_ID] = owner or None

    # Config → config.json under "slack" (staged, applied only after Phase 1).
    path = config_path()
    try:
        data = json.loads(read_config_text(path)) if path.exists() else {}
        if not isinstance(data, dict):
            raise ValueError("config.json is not a JSON object")
    except Exception:
        return _deny("config.json is corrupt", status=500)
    if not isinstance(data.get("slack"), dict):
        data["slack"] = {}
    slack_cfg = data["slack"]
    staged: dict[str, object] = {}
    applied: list[str] = []

    if "command" in body:
        cmd = str(body.get("command", "")).strip().lstrip("/").strip()
        if cmd and (len(cmd) > 32 or not all(c.isalnum() or c in "-_" for c in cmd)):
            return _deny("command must be alphanumeric/-/_ and at most 32 chars")
        # Empty input resets to the default rather than silently keeping the
        # old value, so the slash command can be cleared. Stage only on actual
        # change: the UI sends the field on
        # every save, and command is boot-read, so staging an unchanged value
        # would flag restart_required on every save.
        new_cmd = cmd or "kirocrew"
        if new_cmd != slack_cfg.get("command", "kirocrew"):
            staged["command"] = new_cmd
            applied.append("command")

    if "allowed_enterprise_ids" in body:
        try:
            new_ents = _clean_id_list(
                body.get("allowed_enterprise_ids"),
                lambda v: bool(re.fullmatch(r"[ET][A-Z0-9]+", v)),
                "enterprise ID",
            )
        except ValueError as exc:
            return _deny(str(exc))
        # Boot-read field: stage only on actual change (see command above).
        if new_ents != slack_cfg.get("allowed_enterprise_ids", []):
            staged["allowed_enterprise_ids"] = new_ents
            applied.append("allowed_enterprise_ids")

    for key in ("reactions_enabled", "show_thinking"):
        if key in body:
            val = body.get(key)
            if not isinstance(val, bool):
                return _deny(f"{key} must be a boolean")
            staged[key] = val
            applied.append(key)

    if "session_folder" in body:
        try:
            new_folder = clean_session_folder(body.get("session_folder"))
        except ValueError as exc:
            return _deny(str(exc))
        if new_folder != str(slack_cfg.get("session_folder", "") or ""):
            staged["session_folder"] = new_folder
            applied.append("session_folder")

    # ── Phase 1.5: verify newly pasted tokens against Slack before storing.
    # A token Slack rejects (invalid_auth etc.) fails the save right here,
    # where the user can act on it — instead of being stored and silently
    # failing at the next gateway startup. Network failure is NOT a rejection:
    # the save proceeds with a warning so being offline never blocks config.
    verify_warning = ""
    for field_name, key in _SLACK_SECRET_FIELDS.items():
        pending_tok = env_updates.get(key)
        if not pending_tok:
            continue  # cleared or unchanged — nothing to verify
        try:
            slack_err = await _validate_slack_token(key, pending_tok)
        except Exception:
            verify_warning = "Slack was unreachable, so the token was saved without verification."
            continue
        if slack_err:
            return _deny(f"{field_name} rejected by Slack ({slack_err})")

    # ── Phase 2: commit. All validation passed, so writes are safe. Config
    # first, then .env, with the config rolled back if the .env write fails --
    # the same two-file transaction as the Teams, Webex and WeCom saves. The
    # config write is the one that can still be refused after validation (the
    # file may have gone corrupt between the snapshot and the locked reread),
    # and a refusal must not leave a credential already committed to .env
    # beside config the caller was told did not save. ──
    _cfg_write = _LockedSectionWrite(path, "slack", staged)
    # Hold the live-config watcher for the whole config+credential transaction:
    # the two files commit separately and a failed .env write rolls the config
    # back, so nothing between here and the end of the block may be applied to
    # the running gateway (a widened allow-list must never go live on a save
    # that then fails). The release wakes the watcher on the committed state.
    with live.hold():
        if staged:
            slack_cfg.update(staged)
            # Through ``update_config_locked``: it holds the advisory lock on the
            # sidecar ``<path>.lock`` across the whole read-modify-write, so a
            # writer in ANOTHER PROCESS cannot land between our read and our
            # write, and the staged keys are merged into the file as re-read
            # inside that lock. The helper drains its worker, so a cancellation
            # arriving mid-write cannot release the config lock while the thread
            # is still replacing the file.
            try:
                await _cfg_write.commit()
            except ConfigReadError:
                return _deny("config.json is corrupt", status=500)
        if env_updates:
            # Off-loop: the .env write is blocking file IO (lock, temp write,
            # owner-only lockdown, replace) and must not block the event loop.
            try:
                await _write_env_off_loop(env_updates)
            except BaseException:
                # Roll config back so a failed .env write cannot leave the NEW
                # settings paired with the OLD credentials on disk.
                if staged:
                    await _cfg_write.rollback("Slack")
                raise
            # Keep the live process environment in sync with the new .env state.
            # load_credentials() lets os.environ win over .env, so without this a
            # replaced/cleared token would keep being reported as installed by
            # GET until restart, and spawned children would inherit the stale
            # value. The Slack socket connection itself still reconnects only on
            # restart, which restart_required below surfaces to the UI.
            for key, new_val in env_updates.items():
                if new_val is None:
                    os.environ.pop(key, None)
                else:
                    os.environ[key] = new_val

    # Create the configured session folder now, on this user-initiated save,
    # so the reconcile path never has to write the folder store. Best-effort:
    # a failure leaves conversations unfiled until the next save.
    _folder_name = stored_folder_name(slack_cfg.get("session_folder"))
    if _folder_name:
        _state = request.app.get("state")
        if _state is not None:
            await ensure_channel_folder(
                _state,
                "slack",
                _folder_name,
                relabel="session_folder" in staged,
            )

    _sel().log_api_access(
        caller=caller,
        operation="slack.config.update",
        outcome="ok",
        source="dashboard",
        resources=",".join(applied + list(env_updates.keys())),
    )
    # Only ``slack.command`` still needs a restart: it is registered in the Slack
    # app manifest, so no local reload can make Slack route a new trigger. Every
    # other slack.* field -- including the enterprise allow-list, which the
    # gateway re-reads through its validated reader on a config change -- is
    # applied live. A credential/owner write still needs one: those live in .env,
    # which the config watcher does not watch.
    # Answer only once the watcher has applied the write: a narrowed allow-list
    # is in force before the caller sees "saved", not one poll interval later.
    await _hot_apply_after_write()
    return web.json_response(
        {
            "ok": True,
            "restart_required": channel_restart_required(
                "slack", staged.keys(), env_updates=env_updates
            ),
            "verify_warning": verify_warning,
        }
    )
