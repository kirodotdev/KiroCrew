"""The Feishu settings API: ``GET``/``PUT /api/feishu/config``.

Feishu (飞书/Lark) has the same two-credential shape as WeCom: FEISHU_APP_ID +
FEISHU_APP_SECRET live in config_dir/.env (0600), non-secret config (enabled,
allowed_open_ids, allow_group, allowed_group_ids, soft_threshold_pct,
session_folder) lives in config.json under the "feishu" key. GET returns masked
previews + presence booleans; raw values are write-only. The UI maps
FEISHU_APP_SECRET onto the shared panel's primary secret ("bot_token") and
FEISHU_APP_ID onto its second credential field ("bot_id").

Group access is a SEPARATE axis from the DM allow-list here (unlike WeCom's
allow-all switch): allow_group gates group chats at all, and allowed_group_ids
names which ones. Both fail closed — allow_group with an empty list serves no
group, which is why the panel shows a hint rather than silently doing nothing.
"""

from __future__ import annotations

import asyncio
import functools
import importlib.util
import json
from typing import TYPE_CHECKING

from aiohttp import web

if TYPE_CHECKING:
    from kiro_crew.dashboard.handlers.messaging import (
        _CHANNEL_SDK_EXTRA,
        DashboardState,
        _hot_apply_after_write,
        _mask_secret,
        _pip_install_channel_available,
        _sel,
        _threshold_pct_rejection,
        _write_env_or_roll_back,
        channel_restart_required,
        clean_session_folder,
        ensure_channel_folder,
        is_direct_local_request,
        live,
        logger,
        pip_extra_install_command,
        read_config_text,
        run_to_completion,
        stored_folder_name,
    )


def _channel_sdk_status(channel: str) -> tuple[bool, bool, str]:
    """``(installed, install_supported, install_command)`` for *channel*'s extra.

    Computed HERE rather than read off the connection badge, because the badge
    cannot answer it in the case that matters. ``maybe_start_feishu`` returns at
    its first line when the channel is disabled, and the ``ImportError`` branch
    that records the missing SDK sits after that return — so a user who has not
    yet flipped the enable toggle gets no hint at all, and one who has must
    restart the gateway before the hint appears. This endpoint answers either
    way and without a restart.

    ``install_command`` is empty when it would be useless or actively wrong: the
    SDK is already importable, or no install channel exists in this
    build/interpreter (see :func:`_pip_install_channel_available` — on the
    bundled desktop interpreter a pip install writes into the code-signed bundle
    and is discarded on the next app update, so naming the command there is bad
    advice rather than merely unhelpful).

    Blocking: ``find_spec`` and the PEP 668 marker check both touch the
    filesystem, so call it from a worker thread on an async path.
    """
    entry = _CHANNEL_SDK_EXTRA.get(channel)
    if entry is None:
        # No optional extra for this channel: nothing is ever missing, so the
        # panel renders no card.
        return True, False, ""
    import_name, extra = entry
    if importlib.util.find_spec(import_name) is not None:
        return True, True, ""
    if not _pip_install_channel_available():
        return False, False, ""
    return False, True, pip_extra_install_command(extra)


def _is_valid_feishu_id(v: str, prefix: str) -> bool:
    """Feishu opaque-id shape check (linear string ops, no regex).

    Feishu ids are a fixed prefix (``ou_`` for a user open_id, ``oc_`` for a
    group chat_id) followed by an opaque ASCII alphanumeric body. Only the
    prefix and the charset are asserted, never a length equality: the body
    length is not contractual and a stricter check would reject valid ids from a
    future tenant. ASCII-only on purpose — ``str.isalnum()`` alone admits
    Unicode digits, which can never match a real Feishu id and would sit in the
    allow-list looking authoritative. Fail closed on anything else (whitespace,
    display names, a pasted @-mention, zero-width blobs).
    """
    if not v.startswith(prefix) or len(v) > 128:
        return False
    body = v[len(prefix) :]
    if not body:
        return False
    return all(ch.isascii() and ch.isalnum() for ch in body)


async def api_feishu_config_get(request: web.Request) -> web.Response:
    """GET /api/feishu/config — read Feishu config + masked credential status."""
    from kiro_crew.config.loader import (  # noqa: F811
        CRED_FEISHU_APP_ID,
        CRED_FEISHU_APP_SECRET,
        KiroCrewConfig,
    )

    # Off-loop: both calls are synchronous filesystem reads (config.json, then
    # .env) and the settings panel polls this endpoint every 15s, so on slow or
    # contended storage they would stall every other task on the gateway loop
    # rather than just this request. Read as ONE unit of work: the credential
    # read is a method on the config object, and splitting them into two hops
    # would let the two files be read either side of a concurrent save.
    def _read() -> "tuple[KiroCrewConfig, dict, tuple[bool, bool, str]]":
        loaded = KiroCrewConfig.load()
        # The SDK probe joins this same unit of work: find_spec and the PEP 668
        # marker are filesystem reads, and the panel polls this endpoint every
        # 15s, so giving them their own thread hop would double the cost of a
        # poll for no isolation benefit.
        return loaded, loaded.load_credentials(), _channel_sdk_status("feishu")

    cfg, creds, (sdk_installed, sdk_supported, sdk_command) = await asyncio.to_thread(_read)
    app_id = creds.get(CRED_FEISHU_APP_ID, "")
    app_secret = creds.get(CRED_FEISHU_APP_SECRET, "")
    fs = cfg.feishu
    state: DashboardState = request.app["state"]
    return web.json_response(
        {
            # True only while the WS receiver thread is alive this session —
            # NOT merely "credentials were present at boot". A refused app ends
            # that thread within seconds, which flips this back to false.
            "connected": bool(getattr(state, "feishu_connected", False)),
            "connect_error": str(getattr(state, "feishu_connect_error", ""))[:120],
            # allowed_open_ids is part of "configured": the transport fails
            # closed and rejects every DM while the allow-list is empty, so a
            # credentialed + enabled channel with no ids is not yet usable.
            "configured": bool(app_id and app_secret and fs.enabled and fs.allowed_open_ids),
            # Remote sessions get a read-only view: config edits (PUT) are
            # loopback-only, so the UI disables all inputs and hides Save.
            "read_only": not is_direct_local_request(request),
            # Primary secret slot of the shared panel = FEISHU_APP_SECRET.
            "bot_token_set": bool(app_secret),
            "bot_token_preview": _mask_secret(app_secret),
            # Second credential slot = FEISHU_APP_ID.
            "bot_id_set": bool(app_id),
            "bot_id_preview": _mask_secret(app_id),
            "enabled": bool(fs.enabled),
            "allowed_user_ids": list(fs.allowed_open_ids),
            "allow_group": bool(fs.allow_group),
            "allowed_group_ids": list(fs.allowed_group_ids),
            "soft_threshold_pct": int(fs.soft_threshold_pct),
            "session_folder": fs.session_folder,
            # The channel needs lark-oapi, which ships as the optional [feishu]
            # extra. False means the gateway process cannot import it and the
            # channel will be skipped at boot no matter how complete the rest of
            # this config is.
            "sdk_installed": sdk_installed,
            # False in the three environments where a pip install cannot work
            # (bundled desktop interpreter, no pip module, PEP 668
            # externally-managed): the panel shows an unsupported notice instead
            # of a command that would silently achieve nothing.
            "sdk_install_supported": sdk_supported,
            # Names THIS gateway's interpreter, because installing into the
            # wrong environment is the actual failure mode. Empty when the SDK is
            # present or no install channel exists.
            "sdk_install_command": sdk_command,
        }
    )


async def api_feishu_config_save(request: web.Request) -> web.Response:
    """PUT /api/feishu/config — persist Feishu secrets (.env) + config (config.json).

    Every Feishu field is read once at gateway startup (credentials, enabled
    flag, and both allow-lists are consumed when ``maybe_start_feishu`` builds
    the transport), so any actual change returns ``restart_required``.

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
        return await run_to_completion(_feishu_config_save_locked(request))


async def _feishu_config_save_locked(request: web.Request) -> web.Response:
    """Body of the Feishu save; caller holds ``_get_config_lock()``."""
    from kiro_crew.config.loader import (  # noqa: F811
        CRED_FEISHU_APP_ID,
        CRED_FEISHU_APP_SECRET,
        ConfigReadError,
        config_path,
        update_config_locked,
    )

    caller = request.get("user", "dashboard")

    def _audit_denial(msg: str) -> None:
        _sel().log_api_access(
            caller=caller,
            operation="feishu.config.update",
            outcome="denied",
            source="dashboard",
            error=msg,
        )

    def _deny(msg: str, *, code: str) -> web.Response:
        """Reject a bad request. ``code`` is the contract, ``msg`` is advisory.

        400 is a literal rather than a parameter, and the 403/500 replies below
        are written out at their own call sites for the same reason: a computed
        status puts a response in the error-code gate's unverifiable
        ``dynamic_status`` bucket, which the gate caps precisely because hoisting
        a status out of view looks like refactoring. The dashboard renders
        ``error`` verbatim into a localized UI, so prose alone would be
        untranslatable by construction (RFC 9457 3.1.3).
        """
        _audit_denial(msg)
        return web.json_response({"error": msg, "code": code}, status=400)

    # Remote sessions are read-only: config writes are accepted only from the
    # machine running the gateway, so a remote or tunneled session (even with a
    # valid dashboard token) cannot widen Feishu access or plant credentials.
    if not is_direct_local_request(request):
        message = "read-only from remote sessions (local machine only)"
        _audit_denial(message)
        return web.json_response({"error": message, "code": "remote_read_only"}, status=403)

    try:
        body = await request.json()
    except Exception:
        return _deny("invalid JSON", code="invalid_json")
    if not isinstance(body, dict):
        return _deny("body must be an object", code="body_not_object")

    # ── Phase 1: validate everything and stage changes. No writes happen until
    # all validation passes, so a rejected field never leaves partial state. ──

    env_updates: dict[str, str | None] = {}
    # Two independent credential slots, each with the same set/clear contract
    # as the single-token channels (clear wins over a simultaneously-sent value).
    for field_key, clear_key, cred_key, label in (
        ("bot_token", "bot_token_clear", CRED_FEISHU_APP_SECRET, "app secret"),
        ("bot_id", "bot_id_clear", CRED_FEISHU_APP_ID, "app ID"),
    ):
        clear_flag = body.get(clear_key)
        if clear_flag is not None and not isinstance(clear_flag, bool):
            return _deny(f"{clear_key} must be a boolean", code="clear_flag_not_bool")
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
                    return _deny(
                        f"{label} must not contain whitespace", code="credential_whitespace"
                    )
                if len(cred_val) > 256:
                    return _deny(f"{label} is implausibly long", code="credential_too_long")
                env_updates[cred_key] = cred_val

    # Config → config.json under "feishu" (staged, applied only after Phase 1).
    # Off-loop read: a large or slow config.json must not stall the gateway
    # event loop. Reading under _get_config_lock() keeps the snapshot current
    # relative to every other config writer.
    path = config_path()

    def _read_config() -> dict:
        data = json.loads(read_config_text(path)) if path.exists() else {}
        if not isinstance(data, dict):
            raise ValueError("config.json is not a JSON object")
        return data

    def _corrupt_config() -> web.Response:
        message = "config.json is corrupt"
        _audit_denial(message)
        return web.json_response({"error": message, "code": "config_corrupt"}, status=500)

    try:
        data = await asyncio.to_thread(_read_config)
    except Exception:
        return _corrupt_config()
    # A readable file whose TOP LEVEL is not an object (a hand-edited `[]`) is the
    # same class of problem as an unreadable one, and gets the same answer: the
    # alternative is `data.get` raising AttributeError into a 500 with a stack
    # trace and no indication of what to fix.
    if not isinstance(data, dict):
        return _corrupt_config()
    if not isinstance(data.get("feishu"), dict):
        data["feishu"] = {}
    fs_cfg = data["feishu"]
    staged: dict[str, object] = {}
    applied: list[str] = []

    def _stored_list(key: str) -> list:
        """Stored value only when it really is a list, else empty.

        ``dict.get(key, default)`` substitutes the default only for an ABSENT key:
        a hand-edited ``"allowed_open_ids": null`` returns None and would reach
        ``list(None)``, raising into a 500 from the save that is the way to repair
        the file. Mirrors the loader, which already coerces this shape for the
        runtime.
        """
        value = fs_cfg.get(key)
        return value if isinstance(value, list) else []

    def _stored_int(key: str, default: int) -> int:
        """Stored value only when it really is an int, else the default.

        ``bool`` is excluded deliberately: it is an ``int`` subclass, so a stored
        ``true`` would otherwise compare as the threshold 1.
        """
        value = fs_cfg.get(key)
        return value if isinstance(value, int) and not isinstance(value, bool) else default

    for flag_key in ("enabled", "allow_group"):
        if flag_key in body:
            val = body.get(flag_key)
            if not isinstance(val, bool):
                return _deny(f"{flag_key} must be a boolean", code="flag_not_bool")
            if val != bool(fs_cfg.get(flag_key, False)):
                staged[flag_key] = val
                applied.append(flag_key)

    # Both id lists share one validator, differing only in prefix. The wire name
    # for the DM list is the shared panel's ``allowed_user_ids``; on disk it is
    # ``allowed_open_ids``, which is what the transport reads.
    for wire_key, cfg_key, prefix, what in (
        ("allowed_user_ids", "allowed_open_ids", "ou_", "Feishu open_id"),
        ("allowed_group_ids", "allowed_group_ids", "oc_", "Feishu group chat_id"),
    ):
        if wire_key not in body:
            continue
        raw_ids = body.get(wire_key)
        if not isinstance(raw_ids, list):
            return _deny(f"{wire_key} must be a list", code="ids_not_list")
        new_ids: list[str] = []
        for item in raw_ids:
            s = str(item).strip()
            if not s:
                continue
            if not _is_valid_feishu_id(s, prefix):
                return _deny(f"invalid {what}: {s}", code="invalid_id")
            if s not in new_ids:
                new_ids.append(s)
        if new_ids != _stored_list(cfg_key):
            staged[cfg_key] = new_ids
            applied.append(cfg_key)

    bad_pct = _threshold_pct_rejection(body, "soft_threshold_pct")
    if bad_pct is not None:
        return _deny(bad_pct[1], code=bad_pct[0])
    if "soft_threshold_pct" in body:
        pct = int(body["soft_threshold_pct"])
        if pct != _stored_int("soft_threshold_pct", 80):
            staged["soft_threshold_pct"] = pct
            applied.append("soft_threshold_pct")

    if "session_folder" in body:
        try:
            new_folder = clean_session_folder(body.get("session_folder"))
        except ValueError as exc:
            return _deny(str(exc), code="invalid_session_folder")
        if new_folder != str(fs_cfg.get("session_folder", "") or ""):
            staged["session_folder"] = new_folder
            applied.append("session_folder")

    # No Phase 1.5 credential verification: a REST tenant-token probe would have
    # to pick a domain (open.feishu.cn vs open.larksuite.com) and would report a
    # false failure for whichever tenant it guessed wrong. Credentials are stored
    # as given, and the badge reports receiver liveness after the next restart —
    # a refused app ends the receiver thread within seconds, so a wrong secret
    # surfaces as "not connected" with a reason rather than silence.

    # ── Phase 2: commit. All validation passed, so writes are safe. ──
    #
    # Through ``update_config_locked``, not ``write_config_atomically``: it holds an
    # advisory lock on the sidecar ``<path>.lock`` for the entire read-modify-write,
    # so a concurrent ``kirocrew config set`` in ANOTHER PROCESS cannot land between
    # our read and our write. ``_get_config_lock()`` (held by the caller) only
    # serializes writers inside this one, and loader.py names that combination the
    # required path for a new config.json mutation.
    #
    # ``staged`` is applied to the config as re-read INSIDE the lock rather than to
    # the snapshot taken during validation, so a concurrent edit to an unrelated
    # section is preserved instead of being replaced by our older copy.
    prior_feishu: dict | None = None

    def _apply_staged(fresh: dict) -> dict:
        nonlocal prior_feishu
        section = fresh.get("feishu")
        # Captured HERE because this is the only point at which the pre-mutation
        # state is known to be current.
        prior_feishu = dict(section) if isinstance(section, dict) else None
        if not isinstance(section, dict):
            section = {}
            fresh["feishu"] = section
        section.update(staged)
        return fresh

    def _restore_feishu(fresh: dict) -> dict:
        """Undo the keys THIS request wrote, and only where we still own them.

        Two narrower than the obvious form, both deliberate. Rewriting the file
        from a whole-file snapshot would revert whatever a concurrent writer
        landed; restoring the whole ``feishu`` SECTION would still discard a
        concurrent ``kirocrew config set feishu.*`` that arrived between our write
        and this rollback. So the comparison is per key: a key whose stored value
        differs from the value we wrote has been changed by someone else since,
        and reverting it would destroy their edit to undo ours.
        """
        section = fresh.get("feishu")
        if not isinstance(section, dict):
            # Nothing of ours left to undo (the section is gone or was replaced
            # wholesale by another writer).
            return fresh
        before = prior_feishu if isinstance(prior_feishu, dict) else {}
        for key, written in staged.items():
            if section.get(key) != written:
                continue  # not ours any more
            if key in before:
                section[key] = before[key]
            else:
                section.pop(key, None)
        # Drop a section that only ever existed because we created it, so a failed
        # first-time save leaves no empty scaffold behind.
        if prior_feishu is None and not section:
            fresh.pop("feishu", None)
        return fresh

    async def _rollback_config() -> None:
        try:
            await asyncio.to_thread(
                functools.partial(update_config_locked, path, mutate=_restore_feishu)
            )
        except Exception:
            # A rollback that cannot run must not mask the original failure the
            # caller is already raising; the mismatch is logged instead.
            logger.exception("Feishu config rollback failed; config may lead .env")

    # Hold the live-config watcher for the whole config+credential transaction:
    # the two files commit separately and a failed .env write rolls the config
    # back, so nothing between here and the end of the block may be applied to
    # the running gateway. The release wakes the watcher on the committed state.
    with live.hold():
        if staged:
            # Off-loop: the locked read-modify-write does file IO and may block on a
            # concurrent holder of the lock, neither of which belongs on the loop.
            try:
                await asyncio.to_thread(
                    functools.partial(update_config_locked, path, mutate=_apply_staged)
                )
            except ConfigReadError:
                return _corrupt_config()

        if env_updates:
            # A failed .env write undoes the config commit above, so a refused
            # save changes nothing (see _write_env_or_roll_back).
            rollback = _rollback_config if staged else None
            await _write_env_or_roll_back(env_updates, rollback)

    # Create the configured session folder now, on this user-initiated save, so
    # the reconcile path never has to write the folder store. Best-effort: a
    # failure leaves conversations unfiled until the next save.
    #
    # AFTER the credential write, not before: the .env write can fail (or be
    # cancelled) and the config write above is rolled back when it does, but a
    # folder that has already been created, renamed or unhidden is NOT rolled
    # back. Reconciling here means a save that reported failure leaves no durable
    # folder change behind.
    # The staged value when we changed it, else what was already stored: `fs_cfg`
    # is the VALIDATION snapshot and is not mutated in place — the authoritative
    # update happens inside the lock.
    _effective_folder = staged.get("session_folder", fs_cfg.get("session_folder"))
    _folder_name = stored_folder_name(_effective_folder)
    if _folder_name:
        _state = request.app.get("state")
        if _state is not None:
            await ensure_channel_folder(
                _state,
                "feishu",
                _folder_name,
                relabel="session_folder" in staged,
            )

    _sel().log_api_access(
        caller=caller,
        operation="feishu.config.update",
        outcome="ok",
        source="dashboard",
        resources=",".join(applied + list(env_updates.keys())),
    )
    # The entire Feishu channel config is read once at gateway startup.
    # Answer only once the watcher has applied the write: a narrowed allow-list
    # is in force before the caller sees "saved", not one poll interval later.
    await _hot_apply_after_write()
    return web.json_response(
        {
            "ok": True,
            "restart_required": channel_restart_required(
                "feishu", staged.keys(), env_updates=env_updates
            ),
            "verify_warning": "",
        }
    )
