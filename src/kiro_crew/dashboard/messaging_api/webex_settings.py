"""The Webex settings API: ``GET``/``PUT /api/webex/config``.

The same shape as the Slack settings API: the bot token lives in
config_dir/.env (0600, WEBEX_BOT_TOKEN); non-secret config (enabled,
allowed_emails) lives in config.json under the "webex" key. GET returns a
masked preview + presence boolean; raw token values are write-only.
"""

from __future__ import annotations

import asyncio
import json
import os
from typing import TYPE_CHECKING, Any, cast

from aiohttp import web

if TYPE_CHECKING:
    from kiro_crew.dashboard.handlers.messaging import (
        _WEBEX_VERIFY_TIMEOUT,
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


def _coerce_like(value: Any, stored: Any) -> Any:
    """*stored* rendered in *value*'s own type, for a no-op comparison.

    config.json can legitimately hold a ``null`` or a string where a field is a
    bool or an int (a hand-edited file, or a key written before the field gained
    its type), and an untyped ``!=`` against that reports a change on every save —
    which makes ``restart_required`` permanently true and tells the operator to
    restart for nothing. Coercing to the staged value's type is what keeps a
    genuine no-op reading as one. An unrecognised type is returned unchanged, so
    the comparison degrades to the untyped one rather than guessing.
    """
    if isinstance(value, bool):
        return bool(stored)
    if isinstance(value, int):
        try:
            return int(stored or 0)
        except (TypeError, ValueError):
            return stored
    if isinstance(value, str):
        return str(stored or "")
    if isinstance(value, list):
        try:
            return list(stored or [])
        except TypeError:
            # A hand-edited scalar where a list belongs (``"allowed_room_ids": 5``).
            # Returned unchanged so the comparison degrades to the untyped one and
            # reports a change, exactly as the int branch above does — the
            # alternative is `list(5)` raising out of the handler as a 500 that
            # persists nothing, on a request that may not even mention this field.
            return stored
    return stored


def _is_valid_webex_email(v: str) -> bool:
    """Loose email shape check using linear string ops (no regex).

    CodeQL flags ``[^@\\s]+@[^@\\s]+\\.[^@\\s]+`` as polynomially backtracking
    on adversarial input; exactly-one-``@``, non-empty local part, a dot in
    the domain (not at its edges), and no whitespace covers the same shape in
    O(n) without a regex engine.
    """
    if not v or len(v) > 254:
        return False
    if any(ch.isspace() for ch in v):
        return False
    local, sep, domain = v.partition("@")
    if not sep or not local or "@" in domain:
        return False
    return "." in domain[1:-1]


async def _validate_webex_token(token: str) -> str | None:
    """Check a pasted bot token against Webex before it is stored.

    ``GET /people/me`` is the cheapest authenticated call (the same identity
    call the client makes at connect time). Returns ``None`` when Webex
    accepts the token, or a short error string when it rejects it (401/403).
    Network failures propagate to the caller, which treats them as
    "unverifiable" rather than invalid — saves must not be blocked by being
    offline. Mirrors ``_validate_slack_token``.
    """
    import aiohttp

    async with aiohttp.ClientSession() as session:
        async with session.get(
            "https://webexapis.com/v1/people/me",
            headers={"Authorization": f"Bearer {token}"},
            timeout=aiohttp.ClientTimeout(total=_WEBEX_VERIFY_TIMEOUT),
        ) as resp:
            if 200 <= resp.status < 300:
                return None
            if resp.status in (401, 403):
                return f"invalid_token (http {resp.status})"
            # 5xx / 429 are Webex-side trouble, not a bad token.
            raise RuntimeError(f"webex verify http {resp.status}")


async def api_webex_config_get(request: web.Request) -> web.Response:
    """GET /api/webex/config — read Webex config + masked secret status."""
    from kiro_crew.config.loader import (  # noqa: F811
        CRED_WEBEX_BOT_TOKEN,
        KiroCrewConfig,
    )

    cfg = KiroCrewConfig.load()
    creds = cfg.load_credentials()
    token = creds.get(CRED_WEBEX_BOT_TOKEN, "") or cfg.webex.bot_token
    state: DashboardState = request.app["state"]
    return web.json_response(
        {
            # True only while the device WebSocket is actually connected +
            # authorized this session — NOT merely "a token was present at
            # boot" or "the transport registered". Kept truthful by the
            # client's on_state_change observer (see maybe_start_webex).
            "connected": bool(getattr(state, "webex_connected", False)),
            # Short reason from the most recent connection failure ("" when
            # connected / untried).
            "connect_error": str(getattr(state, "webex_connect_error", ""))[:120],
            "configured": bool(token and cfg.webex.enabled and cfg.webex.allowed_emails),
            # Remote sessions get a read-only view: config edits (PUT) are
            # loopback-only, so the UI disables all inputs and hides Save.
            "read_only": not is_direct_local_request(request),
            "bot_token_set": bool(token),
            "bot_token_preview": _mask_secret(token),
            "enabled": cfg.webex.enabled,
            "allowed_emails": list(cfg.webex.allowed_emails),
            "allow_group_rooms": bool(cfg.webex.allow_group_rooms),
            "allowed_room_ids": list(cfg.webex.allowed_room_ids),
            "reply_in_thread": bool(cfg.webex.reply_in_thread),
            "soft_threshold_pct": int(cfg.webex.soft_threshold_pct),
            "hard_threshold_pct": int(cfg.webex.hard_threshold_pct),
            "session_folder": cfg.webex.session_folder,
        }
    )


async def api_webex_config_save(request: web.Request) -> web.Response:
    """PUT /api/webex/config — persist the Webex token (.env) + config (config.json).

    The whole Webex channel config is read at gateway startup, so every
    change returns ``restart_required`` for the UI hint.
    """
    # A save is a config.json + credential transaction: a cancelled request
    # (client gone, gateway shutting down) must not abandon it between its
    # phases -- see ``run_to_completion``.
    return await run_to_completion(_webex_config_save(request))


async def _webex_config_save(request: web.Request) -> web.Response:
    """Body of the Webex save; runs to completion once started."""
    from kiro_crew.config.loader import (  # noqa: F811
        CRED_WEBEX_BOT_TOKEN,
        ConfigReadError,
        WebexConfig,
        _normalize_threshold_pair,
        _threshold_pct,
        config_path,
    )

    caller = request.get("user", "dashboard")

    def _deny(msg: str, status: int = 400) -> web.Response:
        _sel().log_api_access(
            caller=caller,
            operation="webex.config.update",
            outcome="denied",
            source="dashboard",
            error=msg,
        )
        return web.json_response({"error": msg}, status=status)

    # Remote sessions are read-only (same gate as the Slack config API): a
    # remote or tunneled session cannot alter channel access or plant tokens.
    if not is_direct_local_request(request):
        return _deny("read-only from remote sessions (local machine only)", status=403)

    try:
        body = await request.json()
    except Exception:
        return _deny("invalid JSON")
    if not isinstance(body, dict):
        return _deny("body must be an object")

    # ── Phase 1: validate everything and stage changes (no partial writes). ──
    env_updates: dict[str, str | None] = {}
    clear_flag = body.get("bot_token_clear")
    if clear_flag is not None and not isinstance(clear_flag, bool):
        return _deny("bot_token_clear must be a boolean")
    if clear_flag is True:
        env_updates[CRED_WEBEX_BOT_TOKEN] = None
    else:
        raw = body.get("bot_token")
        if isinstance(raw, str):
            tok = raw.strip()
            if tok.startswith(f"{CRED_WEBEX_BOT_TOKEN}="):  # accidentally pasted env line
                tok = tok[len(CRED_WEBEX_BOT_TOKEN) + 1 :].strip()
            if tok:
                if any(ch.isspace() for ch in tok):
                    return _deny("bot_token must not contain whitespace")
                env_updates[CRED_WEBEX_BOT_TOKEN] = tok

    # ── Phase 1 (continued): validate the config fields from the request
    # alone — the current config.json is NOT read here. The authoritative
    # read-modify-write happens entirely under the config lock in Phase 2,
    # so a concurrent save by another handler can never be clobbered by a
    # stale full-file snapshot.
    staged: dict[str, object] = {}

    if "enabled" in body:
        val = body.get("enabled")
        if not isinstance(val, bool):
            return _deny("enabled must be a boolean")
        staged["enabled"] = val

    if "allowed_emails" in body:
        try:
            new_emails = _clean_id_list(body.get("allowed_emails"), _is_valid_webex_email, "email")
        except ValueError as exc:
            return _deny(str(exc))
        staged["allowed_emails"] = new_emails

    for flag in ("allow_group_rooms", "reply_in_thread"):
        if flag in body:
            val = body.get(flag)
            if not isinstance(val, bool):
                return _deny(f"{flag} must be a boolean")
            staged[flag] = val

    if "allowed_room_ids" in body:
        rooms = body.get("allowed_room_ids")
        if not isinstance(rooms, list) or not all(isinstance(r, str) for r in rooms):
            return _deny("allowed_room_ids must be a list of strings")
        # De-duplicated, order preserved, blanks dropped. Not otherwise validated:
        # a Webex room id is an opaque base64 blob whose shape is the platform's to
        # define, and a format guess here would reject a legitimate id from a
        # cluster this code has never seen.
        seen: set[str] = set()
        cleaned: list[str] = []
        for raw in rooms:
            room = raw.strip()
            if room and room not in seen:
                seen.add(room)
                cleaned.append(room)
        staged["allowed_room_ids"] = cleaned

    # Range-validated here; CLAMPED in Phase 2, where the locked fresh read
    # supplies the counterpart. Reading the config here instead would be both a
    # torn read and a side-effecting one: ``KiroCrewConfig.load()`` normalizes and
    # writes the file back, which materializes every default into config.json and
    # makes the next no-op save report a change.
    for name in ("soft_threshold_pct", "hard_threshold_pct"):
        if name in body:
            pct = body.get(name)
            if not isinstance(pct, int) or isinstance(pct, bool) or not (1 <= pct <= 100):
                return _deny(f"{name} must be an integer between 1 and 100")
            staged[name] = pct

    if "session_folder" in body:
        try:
            staged["session_folder"] = clean_session_folder(body.get("session_folder"))
        except ValueError as exc:
            return _deny(str(exc))

    # ── Phase 1.5: verify a newly pasted token against Webex before storing.
    # Network failure is NOT a rejection: the save proceeds with a warning so
    # being offline never blocks config. Mirrors the Slack token verification.
    verify_warning = ""
    pending_tok = env_updates.get(CRED_WEBEX_BOT_TOKEN)
    if pending_tok:
        try:
            webex_err = await _validate_webex_token(pending_tok)
        except Exception:
            verify_warning = "Webex was unreachable, so the token was saved without verification."
        else:
            if webex_err:
                return _deny(f"bot_token rejected by Webex ({webex_err})")

    # ── Phase 2: commit. All validation passed, so writes are safe. The
    # read-modify-write of config.json happens ENTIRELY under the repo-wide
    # config lock (read fresh, merge only the webex section, write atomic),
    # so a concurrent save by another settings handler is never overwritten
    # by a stale snapshot taken before the lock.
    from kiro_crew.dashboard.handlers.agents import _get_config_lock  # noqa: F811

    applied: list[str] = []
    async with _get_config_lock():
        path = config_path()
        try:
            data = json.loads(read_config_text(path)) if path.exists() else {}
            if not isinstance(data, dict):
                raise ValueError("config.json is not a JSON object")
        except Exception:
            return _deny("config.json is corrupt", status=500)
        if not isinstance(data.get("webex"), dict):
            data["webex"] = {}
        webex_cfg = data["webex"]

        # Reduce staged fields to actual changes against the fresh read so
        # restart_required stays truthful on no-op saves.
        #
        # Generic over ``staged`` rather than one branch per field. A hand-written
        # branch list silently DROPS any field added to Phase 1 without a matching
        # branch here — the whole write is ``webex_cfg.update(changes)``, so a
        # missing branch means the value validates, reports success, and is never
        # persisted. The dataclass supplies each field's default, so the
        # comparison is against the same value a fresh config would read.
        defaults = WebexConfig()
        # Clamp the thresholds as a PAIR through the same helper the config
        # dataclass uses, so a soft value above the hard one cannot make the soft
        # nudge unreachable -- ``_maybe_notice`` tests ``pct >= hard`` first. Done
        # here because clamping needs the counterpart, and only this fresh read
        # under the config lock has an untorn view of it.
        if "soft_threshold_pct" in staged or "hard_threshold_pct" in staged:

            def _pct(name: str) -> int:
                """This request's value for *name*, or the STORED counterpart.

                The stored side is coerced defensively: ``config.json`` is a file
                an operator can hand-edit, so a non-numeric counterpart would make
                saving the OTHER threshold raise out of the handler as a 500 and
                persist nothing — a value this request never mentioned breaking a
                value it did. A malformed stored number falls back to the dataclass
                default, which is the same thing the loader does with it.
                """
                if name in staged:
                    return int(cast(int, staged[name]))
                try:
                    return int(webex_cfg.get(name, getattr(defaults, name)))
                except (TypeError, ValueError):
                    return int(getattr(defaults, name))

            soft, hard = _normalize_threshold_pair(
                _pct("soft_threshold_pct"), _pct("hard_threshold_pct")
            )
            if "soft_threshold_pct" in staged:
                staged["soft_threshold_pct"] = soft
            if "hard_threshold_pct" in staged:
                staged["hard_threshold_pct"] = hard

        changes: dict[str, object] = {}
        for key, value in staged.items():
            stored = webex_cfg.get(key, getattr(defaults, key, None))
            if value != _coerce_like(value, stored):
                changes[key] = value
        applied = list(changes.keys())
        # Any token set/clear also purges the legacy config.json
        # ``webex.bot_token`` fallback so a stale plaintext copy can't shadow
        # (or outlive) the .env credential. The config write commits BEFORE
        # the .env write — if we crash between the two, the legacy copy is
        # already gone rather than resurrected.
        if CRED_WEBEX_BOT_TOKEN in env_updates and webex_cfg.get("bot_token"):
            applied.append("bot_token_purged")
        # Blanked against the document the write lands on (see
        # ``_LockedSectionWrite``): a legacy copy a concurrent writer landed after
        # the snapshot is purged too, whenever this save updates the credential.
        blank_keys = ("bot_token",) if CRED_WEBEX_BOT_TOKEN in env_updates else ()

        # Through ``update_config_locked``: it holds the advisory lock on the
        # sidecar ``<path>.lock`` across the whole read-modify-write, so a writer
        # in ANOTHER PROCESS cannot land between our read and our write, and the
        # changes are merged into the file as re-read inside that lock. The same
        # object undoes exactly those keys if the .env write below fails.
        def _normalize_pair(section: dict) -> None:
            # The pair was normalized against the snapshot above; a concurrent
            # writer may have moved the counterpart since, so normalize once more
            # against the merged section -- exactly what the loader does on read,
            # so the file stores the pair the runtime will use.
            if "soft_threshold_pct" in changes or "hard_threshold_pct" in changes:
                soft, hard = _normalize_threshold_pair(
                    _threshold_pct(section.get("soft_threshold_pct"), defaults.soft_threshold_pct),
                    _threshold_pct(section.get("hard_threshold_pct"), defaults.hard_threshold_pct),
                )
                section["soft_threshold_pct"] = soft
                section["hard_threshold_pct"] = hard

        _cfg_write = _LockedSectionWrite(
            path, "webex", changes, blank_keys=blank_keys, finalize=_normalize_pair
        )
        # Hold the live-config watcher for the whole config+credential transaction:
        # the two files commit separately and a failed .env write rolls the config
        # back, so nothing between here and the end of the block may be applied to
        # the running gateway. The release wakes the watcher on the committed state.
        with live.hold():
            if changes or blank_keys:
                webex_cfg.update(changes)
                # Off-loop: file IO, and it may wait on another holder of the lock.
                try:
                    await _cfg_write.commit()
                except ConfigReadError:
                    return _deny("config.json is corrupt", status=500)

            # Create the configured session folder now, on this user-initiated save,
            # so the reconcile path never has to write the folder store. Best-effort:
            # a failure leaves conversations unfiled until the next save.
            _folder_name = stored_folder_name(webex_cfg.get("session_folder"))
            if _folder_name:
                _state = request.app.get("state")
                if _state is not None:
                    await ensure_channel_folder(
                        _state,
                        "webex",
                        _folder_name,
                        relabel="session_folder" in changes,
                    )
            if env_updates:
                # Off-loop: the .env write is blocking file IO (lock, temp write,
                # owner-only lockdown, replace) and must not block the event loop.
                #
                # Cancellation guard: see Teams save for the full rationale. Only
                # roll config back when the .env write actually failed, not when
                # cancellation arrived after the write already committed.
                _env_write_task_wx: asyncio.Task[None] = asyncio.ensure_future(
                    _write_env_off_loop(env_updates)
                )
                try:
                    await asyncio.shield(_env_write_task_wx)
                except asyncio.CancelledError:
                    await asyncio.gather(_env_write_task_wx, return_exceptions=True)
                    _env_exc_wx = (
                        _env_write_task_wx.exception()
                        if not _env_write_task_wx.cancelled()
                        else None
                    )
                    if _env_exc_wx is not None:
                        if changes or blank_keys:
                            await _cfg_write.rollback("Webex")
                    raise
                except BaseException:
                    # Roll config back so a failed .env write cannot leave the
                    # NEW metadata paired with the OLD token on disk.
                    if changes or blank_keys:
                        await _cfg_write.rollback("Webex")
                    raise
                # Keep the live process environment in sync (see the Slack save path).
                for key, new_val in env_updates.items():
                    if new_val is None:
                        os.environ.pop(key, None)
                    else:
                        os.environ[key] = new_val

    _sel().log_api_access(
        caller=caller,
        operation="webex.config.update",
        outcome="ok",
        source="dashboard",
        resources=",".join(applied + list(env_updates.keys())),
    )
    # The entire Webex channel config is read once at gateway startup.
    # Answer only once the watcher has applied the write: a narrowed allow-list
    # is in force before the caller sees "saved", not one poll interval later.
    await _hot_apply_after_write()
    return web.json_response(
        {
            "ok": True,
            "restart_required": channel_restart_required("webex", applied, env_updates=env_updates),
            "verify_warning": verify_warning,
        }
    )
