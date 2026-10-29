"""Dashboard endpoints for the Weixin (iLink) QR-login setup flow.

Two routes back the Settings > Messaging Channels > WeChat "Connect via QR" flow:

  POST /api/channels/weixin/qr/start   -> start an iLink QR session, return the
                                          QR image + a server-side session id.
  GET  /api/channels/weixin/qr/status  -> poll scan status; on ``confirmed``,
                                          persist the bot token to the cred
                                          store (~/.kiro/crew/.env) and the
                                          account_id/base_url to config.json,
                                          then enable the channel.

The secret (bot credential) is written server-side and never returned to the
client (only ``connected: true`` + the non-secret account_id), mirroring how every
other channel's credentials are handled.

Both the ``.env`` and ``config.json`` writes are minimal atomic writers keyed off
``env_path()`` / ``config_path()``, and every write here runs under the
repository-wide ``_get_config_lock()`` — the same lock the sibling channel savers
take — so a QR confirmation can never interleave with another config save.
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
import time
import uuid
from pathlib import Path
from typing import Any, Dict, Optional

from aiohttp import web

from kiro_crew import platform_compat
from kiro_crew.atomic_write import atomic_write
from kiro_crew.config import live
from kiro_crew.config.loader import (
    CRED_WEIXIN_TOKEN,
    KiroCrewConfig,
    _env_name_key,
    apply_env_credential_changes,
    config_path,
    env_bom_prefix,
    env_path,
    read_config_text,
    read_env_text,
)
from kiro_crew.dashboard.channel_folders import (
    channel_restart_required,
    clean_session_folder,
    ensure_channel_folder,
    stored_folder_name,
)
from kiro_crew.dashboard.chat_utils_awaits import drained_to_thread
from kiro_crew.dashboard.handlers.agents import _get_config_lock
from kiro_crew.dashboard.handlers.core import _hot_apply_after_write
from kiro_crew.dashboard.handlers.messaging import is_direct_local_request
from kiro_crew.hidden_leaf_staging import EnvStagingRefused, publish_env
from kiro_crew.weixin.client import ILINK_BASE_URL, WeixinClient

logger = logging.getLogger(__name__)

# In-memory QR sessions: session_id -> {client, qrcode, created_at}. Short-lived
# (a QR expires in minutes); pruned opportunistically on each start.
_SESSIONS: Dict[str, Dict[str, Any]] = {}
_SESSION_TTL_SECONDS = 600

#: Strong references to the expired clients' close tasks. The event loop keeps
#: only a weak reference to a task, so one nothing holds can be garbage-collected
#: before ``close()`` finishes. Each task removes itself when it is done.
_CLOSE_TASKS: set[asyncio.Task[Any]] = set()


def _render_qr_data_uri(scan_data: str) -> str:
    """Encode ``scan_data`` (the iLink login URL) into a PNG data URI.

    Runs in a worker thread: PNG encoding is CPU-bound and must not stall the
    gateway event loop. Delegates to :mod:`kiro_crew.qr`, the single owner of QR
    encoding, so this surface and tailnet mobile access cannot drift into two
    different encoders.
    """
    from kiro_crew.qr import render_qr_data_uri  # noqa: PLC0415 - heavy import, QR path only

    return render_qr_data_uri(scan_data)


def _prune_sessions() -> None:
    now = time.time()
    for sid, sess in list(_SESSIONS.items()):
        if now - sess.get("created_at", 0) >= _SESSION_TTL_SECONDS:
            client = sess.get("client")
            if client is not None:
                # best-effort close; the event loop owns the coroutine
                try:
                    task = asyncio.create_task(client.close())
                    _CLOSE_TASKS.add(task)
                    task.add_done_callback(_CLOSE_TASKS.discard)
                except Exception:
                    pass
            _SESSIONS.pop(sid, None)


def _atomic_write(path: Path, text: str, *, secret: bool = False) -> None:
    """Atomically replace ``path`` with ``text``.

    Delegates to :func:`kiro_crew.atomic_write.atomic_write`, which every
    atomic-write site in the repo is required to use — it allocates the temp file
    with ``mkstemp`` so concurrent writers to the same target cannot collide on a
    deterministic ``.tmp`` name (an ENOENT race).

    Permissions are never weakened. ``secret=True`` is the ``.env`` write: it
    goes through :func:`kiro_crew.hidden_leaf_staging.publish_env`, which stages
    the temp inside the masked staging directory rather than beside ``.env``
    and locks it down to its owner BEFORE any content byte is written — POSIX
    mode bits are meaningless against NTFS ACLs, so a lockdown applied to the
    final path after the write would leave the credential readable under the
    directory-inherited DACL for the whole write. A refused lockdown warns: a
    host where it cannot be applied must not abort a credential save that
    already succeeded. For a non-secret
    file the EXISTING mode is carried over, because the replacement would
    otherwise adopt the umask default and silently downgrade an
    already-restricted file — ``config.json`` can hold inline fallback
    credentials, so a 0600 → 0644 transition there would expose them to other
    local users.

    The ``.env`` text is published as UTF-8 bytes, which are never
    newline-translated, so the file lands byte-for-byte as *text* on every
    platform. The sign-in rollback depends on that: it compares the file with
    the text it published and restores the prior snapshot exactly, and a
    Windows text-mode write would turn each ``\\n`` into ``\\r\\n`` (and a
    snapshot's own ``\\r\\n`` into ``\\r\\r\\n``).
    """
    if secret:
        publish_env(path, text.encode("utf-8"))
        return
    preserved: Optional[int] = None
    try:
        preserved = path.stat().st_mode & 0o777
    except OSError:
        preserved = None  # new file: fall through to the repo-default umask mode
    atomic_write(path, text, mode=preserved)


def _same_env_entry(a: str, b: str) -> bool:
    """Whether two ``.env`` names address the same ``os.environ`` entry.

    Case-insensitive on Windows (where ``os.environ`` is), case-sensitive on
    POSIX -- the loader's one canonical-name rule.
    """
    return _env_name_key(a) == _env_name_key(b)


def _write_env_secret(key: str, value: str) -> tuple[Optional[str], str]:
    """Upsert ``KEY=VALUE`` in ~/.kiro/crew/.env (0600), preserving other lines.

    Returns ``(prior, published)``: the whole file as this call read it (BOM
    included; ``None`` when absent) and the exact text it published. Both come
    from the one read the replacement is built from, so a caller's rollback
    snapshot can never miss an edit the write itself kept, and needs no second
    read that could fail outside the caller's rollback protection.
    """
    ep = env_path()
    lines: list[str] = []
    found = False
    prior: Optional[str] = None
    if ep.exists():
        bom = env_bom_prefix(ep)
        body = read_env_text(ep, encoding="utf-8")
        prior = bom + body
        for line in body.splitlines():
            stripped = line.strip()
            if stripped and not stripped.startswith("#") and "=" in stripped:
                if _same_env_entry(stripped.split("=", 1)[0].strip(), key):
                    # Every case-equivalent spelling is one os.environ entry on
                    # Windows: rewrite the first under the declared name and drop
                    # the rest, or a leftover spelling earlier in the file would
                    # win boot's / the watcher's first-spelling rule and keep the
                    # OLD token in force. Case-sensitive on POSIX.
                    if not found:
                        lines.append(f"{key}={value}")
                        found = True
                    continue
            lines.append(line)
    if not found:
        lines.append(f"{key}={value}")
    published = env_bom_prefix(ep) + "\n".join(lines) + "\n"
    _atomic_write(ep, published, secret=True)
    return prior, published


def _read_env_value(key: str) -> Optional[str]:
    """Return the current value of ``key`` in ~/.kiro/crew/.env, or None if absent.

    Used to snapshot the previous credential so a failed two-file commit can be
    rolled back instead of destroying a working credential.
    """
    ep = env_path()
    if not ep.exists():
        return None
    first: Optional[str] = None
    for line in read_env_text(ep, encoding="utf-8").splitlines():
        stripped = line.strip()
        if stripped and not stripped.startswith("#") and "=" in stripped:
            k, _, value = stripped.partition("=")
            # Every case-equivalent spelling (Windows) is one os.environ entry,
            # and boot propagates the first NON-EMPTY one, so that is the value
            # in force: an empty spelling earlier in the file is skipped, as
            # boot skips it. Case-sensitive on POSIX.
            if _same_env_entry(k.strip(), key):
                if value:
                    return value
                if first is None:
                    first = value
    return first


def _delete_env_key(key: str) -> None:
    """Remove ``key`` from ~/.kiro/crew/.env, preserving every other line."""
    ep = env_path()
    if not ep.exists():
        return
    kept: list[str] = []
    for line in read_env_text(ep, encoding="utf-8").splitlines():
        stripped = line.strip()
        if stripped and not stripped.startswith("#") and "=" in stripped:
            # Every case-equivalent spelling on Windows, so no leftover spelling
            # keeps supplying the removed value. Case-sensitive on POSIX.
            if _same_env_entry(stripped.split("=", 1)[0].strip(), key):
                continue
        kept.append(line)
    _atomic_write(ep, env_bom_prefix(ep) + "\n".join(kept) + "\n", secret=True)


def _env_file_text(ep: Path) -> Optional[str]:
    """The whole ``.env`` as written (BOM included), or ``None`` when absent."""
    return (env_bom_prefix(ep) + read_env_text(ep, encoding="utf-8")) if ep.exists() else None


def _env_entry_lines(key: str, text: Optional[str]) -> list[str]:
    """Every line of ``.env`` *text* that sets *key*'s ``os.environ`` entry, in order.

    Case-equivalent spellings count on Windows (case-sensitive on POSIX), the
    same rule as :func:`_write_env_secret`. Lines are returned verbatim; a
    leading BOM is ignored. ``None`` (no file) has no lines.
    """
    if not text:
        return []
    out: list[str] = []
    for line in text.lstrip("\ufeff").splitlines():
        stripped = line.strip()
        if stripped and not stripped.startswith("#") and "=" in stripped:
            if _same_env_entry(stripped.split("=", 1)[0].strip(), key):
                out.append(line)
    return out


def _restore_env_entry(key: str, prior_lines: list[str], written_line: str) -> None:
    """Undo :func:`_write_env_secret` for *key*, touching only that entry's lines.

    The current file is re-read and every other line is kept as it is now, so an
    unrelated edit saved after the write survives the rollback. The entry's
    lines are restored only when the file still holds exactly *written_line* for
    it, i.e. nobody changed the entry since: the restore then puts back
    *prior_lines* (every spelling, in their original order, where the written
    line sits), or drops the entry when it had none. If the entry was changed
    independently, the restore is refused and an ``OSError`` raised, so the
    caller's rollback failure is logged rather than overwriting that change.
    """
    ep = env_path()
    if not ep.exists():
        if prior_lines:
            raise OSError(f"{ep} vanished; the prior WeChat credential was not restored")
        return
    current = read_env_text(ep, encoding="utf-8").splitlines()
    out: list[str] = []
    seen: list[str] = []
    for line in current:
        stripped = line.strip()
        if stripped and not stripped.startswith("#") and "=" in stripped:
            if _same_env_entry(stripped.split("=", 1)[0].strip(), key):
                if not seen:
                    out.extend(prior_lines)
                seen.append(line)
                continue
        out.append(line)
    if [s.strip() for s in seen] != [written_line]:
        raise OSError(
            f"the {key} entry in {ep} changed after the sign-in wrote it; "
            "not rolling it back over that change"
        )
    _atomic_write(ep, env_bom_prefix(ep) + "\n".join(out) + ("\n" if out else ""), secret=True)


def _commit_credential_and_config(cp: Path, serialized: str, token: str) -> None:
    """Write the credential then the config, rolling the credential back on failure.

    These are two separate files, so the pair cannot be replaced atomically.
    Snapshot the old credential and restore it if the second write fails (ENOSPC,
    EACCES, a crash between the two): otherwise the new credential would be left
    paired with stale account metadata — the channel would authenticate against
    the wrong account — and the previously working credential would be gone, with
    no way to recover it from the dashboard.

    Acquires the shared ``.env.lock`` cross-process advisory lock (same sidecar
    file used by ``secrets import --apply``) before any read or write of ``.env``,
    so the importer and this handler cannot interleave their read-modify-write
    cycles.  If the lock is already held by another process the function raises
    ``OSError`` with a descriptive message; the caller (``_persist``) propagates
    it as a 500 response, which is the same outcome as any other I/O failure here.
    The existing in-process ``_get_config_lock()`` (held by the caller one level
    up) continues to serialise same-process concurrent callers; the file lock adds
    the cross-process exclusion layer on top.
    """
    ep = env_path()
    # Lazy import: the migration importer is an optional CLI subsystem, so keep it
    # off the gateway boot-import path (weixin_qr is imported at server startup).
    # Only load it when a credential write actually happens.
    from kiro_crew.secrets.migrate import _env_lock_path

    lock_path = _env_lock_path(ep)
    lock_fd = platform_compat.open_create_or_existing(lock_path, os.O_RDWR, 0o600)
    try:
        if not platform_compat.try_acquire_lock(lock_fd, exclusive=True):
            raise OSError(
                f"{ep} is locked by another process (secrets import in progress); "
                "the WeChat credential was not written.  Retry the QR sign-in "
                "once the other operation finishes."
            )
        # Snapshot the token's own lines, every case-equivalent spelling in file
        # order, not one value: the write below collapses them (Windows) into
        # one line, so restoring a single value could not put back the layout
        # boot read -- an empty alias ahead of the working token would come back
        # as the only line and the working token would be gone. NOT the whole
        # file: the advisory lock does not exclude an operator's editor, and a
        # whole-file restore would delete an unrelated edit saved in between.
        # The snapshot and the published text both come from the write's own
        # single read (see _write_env_secret), so the snapshot holds every edit
        # the write kept and no extra read sits outside the rollback below.
        prior_text, written_text = _write_env_secret(CRED_WEIXIN_TOKEN, token)
        prior_entry = _env_entry_lines(CRED_WEIXIN_TOKEN, prior_text)
        try:
            _atomic_write(cp, serialized)
        except BaseException:
            try:
                if prior_text is not None and _env_file_text(ep) == written_text:
                    # Nothing else touched the file since the write: put the
                    # snapshot back byte for byte (layout, comments, BOM).
                    _atomic_write(ep, prior_text, secret=True)
                else:
                    _restore_env_entry(
                        CRED_WEIXIN_TOKEN, prior_entry, f"{CRED_WEIXIN_TOKEN}={token}"
                    )
            except Exception:
                # Surfacing the rollback failure would mask the original cause, so
                # log it and let the real error propagate.
                logger.error(
                    "weixin: config commit failed AND credential rollback failed; "
                    "the stored credential may not match config.json",
                    exc_info=True,
                )
            raise
        # ``load_credentials()`` lets ``os.environ`` win over ``.env``, and boot
        # copied the previous token there: left alone, the channel restart and
        # an in-app re-exec would both keep the replaced token. Routed through
        # the one shared live applier rather than a direct ``os.environ`` write,
        # so this write records ``.env`` ownership the same way every other
        # live write does -- a later removal of the line then revokes the token
        # instead of leaving it in force -- and inherits the Docker
        # ``_KIROCREW_CREDS_SCRUBBED`` guard (a scrubbed environ must not regain
        # a credential).
        apply_env_credential_changes({CRED_WEIXIN_TOKEN}, {CRED_WEIXIN_TOKEN: token})
    finally:
        platform_compat.release_lock(lock_fd)
        os.close(lock_fd)


def _stage_weixin_config(*, account_id: str, base_url: str) -> tuple[Path, str]:
    """Parse config.json and return ``(path, serialized)`` with the weixin patch.

    Parsing/serializing is separated from writing so the caller can validate the
    config BEFORE overwriting the credential: a corrupt ``config.json`` must not
    leave a replaced token behind with no matching config (which would lose the
    previously working credential).

    A corrupt file PROPAGATES rather than being reset to ``{}`` — resetting would
    silently replace every other setting with just the weixin section. Mirrors
    :func:`weixin_config_save`, which returns 500 on a parse failure.
    """
    cp = config_path()
    data: Dict[str, Any] = {}
    if cp.exists():
        data = json.loads(read_config_text(cp))
        if not isinstance(data, dict):
            raise ValueError("config.json is not a JSON object")
    weixin = data.get("weixin")
    if not isinstance(weixin, dict):
        weixin = {}
    weixin["enabled"] = True
    weixin["account_id"] = account_id
    if base_url:
        weixin["base_url"] = base_url
    data["weixin"] = weixin
    return cp, json.dumps(data, ensure_ascii=False, indent=2)


async def weixin_qr_start(request: web.Request) -> web.Response:
    """Begin an iLink QR-login session; return the QR image + session id.

    Loopback-only: a QR confirmation writes the bot credential into the shared
    credential store and enables the channel, so a remote (or tunneled) session
    must never be able to start one — it could scan the returned code with its own
    WeChat account and take over the channel.
    """
    if not is_direct_local_request(request):
        return web.json_response(
            {"error": "read-only from remote sessions (local machine only)"}, status=403
        )
    _prune_sessions()
    client = WeixinClient(token="", base_url=ILINK_BASE_URL)
    try:
        await client.connect()
        resp = await client.get_bot_qrcode()
    except Exception as exc:  # network / iLink error
        await client.close()
        logger.warning("weixin QR start failed: %s", exc)
        return web.json_response({"error": "qr_start_failed", "detail": str(exc)[:200]}, status=502)

    qrcode_token = resp.get("qrcode", "")
    scan_url = resp.get("qrcode_img_content", "")
    if not qrcode_token:
        await client.close()
        return web.json_response({"error": "invalid_qr_response"}, status=502)

    # Despite its name, iLink's `qrcode_img_content` is NOT image bytes — it is
    # the full scannable login URL, while `qrcode` is just the hex token (the
    # reference implementation encodes the URL itself, falling back to the
    # token). Render the QR PNG here, off the event loop, and hand the panel a
    # data URI so <img src> is actually loadable.
    scan_data = scan_url or qrcode_token
    try:
        img_data_uri = await asyncio.to_thread(_render_qr_data_uri, scan_data)
    except Exception:
        await client.close()
        logger.exception("weixin: QR image rendering failed")
        return web.json_response({"error": "qr_render_failed"}, status=500)

    session_id = uuid.uuid4().hex
    _SESSIONS[session_id] = {"client": client, "qrcode": qrcode_token, "created_at": time.time()}
    return web.json_response({"session_id": session_id, "qrcode_img_content": img_data_uri})


async def weixin_qr_status(request: web.Request) -> web.Response:
    """Poll scan status; on confirmation, persist creds + enable the channel.

    Loopback-only for the same reason as :func:`weixin_qr_start` — this is the
    handler that actually writes the credential.
    """
    if not is_direct_local_request(request):
        return web.json_response(
            {"error": "read-only from remote sessions (local machine only)"}, status=403
        )
    session_id = request.query.get("session_id", "")
    sess = _SESSIONS.get(session_id)
    if not sess:
        return web.json_response({"error": "unknown_session"}, status=404)

    client: WeixinClient = sess["client"]
    try:
        resp = await client.get_qrcode_status(sess["qrcode"])
    except Exception as exc:
        logger.warning("weixin QR status poll failed: %s", exc)
        return web.json_response({"status": "pending"})

    status = resp.get("status", "pending")
    if status != "confirmed":
        # "scaned" / "pending" / "expired" — surface as-is for the UI.
        if status == "expired":
            await client.close()
            _SESSIONS.pop(session_id, None)
        return web.json_response({"status": status})

    token = resp.get("bot_token", "")
    base_url = resp.get("baseurl") or ILINK_BASE_URL
    account_id = (
        resp.get("ilink_bot_id") or resp.get("account_id") or resp.get("ilink_user_id") or ""
    )
    if not token or not account_id:
        await client.close()
        _SESSIONS.pop(session_id, None)
        return web.json_response({"error": "confirmed_without_credentials"}, status=502)

    def _persist() -> None:
        # Parse + stage the config FIRST: if config.json is corrupt we must fail
        # before touching the credential, otherwise a re-login would replace a
        # working token and then abort, losing it.
        cp, serialized = _stage_weixin_config(account_id=account_id, base_url=base_url)
        _commit_credential_and_config(cp, serialized, token)

    try:
        # Both writes touch shared stores (.env + config.json) that other config
        # savers read-modify-write, so hold the repository-wide config lock across
        # BOTH — otherwise a concurrent channel save can interleave and silently
        # drop one side's write.
        #
        # The whole block runs OFF the loop: the file I/O plus, on Windows,
        # the owner-only DACL restrict_to_owner() applies.
        #
        # Hold the live-config watcher across the whole transaction, like every
        # sibling channel saver: ``.env`` and ``config.json`` commit separately
        # (and a failed config write rolls the credential back), so a poll landing
        # between them must not restart Weixin with the new token and the previous
        # account id. The release wakes the watcher on the committed state.
        # ``drained_to_thread`` keeps a cancelled request waiting for the worker
        # to finish, so the hold and the lock are never released while a write
        # or its rollback is still in flight.
        async with _get_config_lock():
            with live.hold():
                await drained_to_thread(_persist)
    except EnvStagingRefused as exc:
        # The .env's staging directory is unusable: nothing was written, and the
        # message names the directory and its fix.
        logger.warning("weixin: QR sign-in not saved: %s", exc)
        return web.json_response({"error": str(exc), "code": "env_staging_refused"}, status=409)
    except Exception as exc:
        # A corrupt config.json or an un-restrictable credential file must NOT be
        # reported as a successful sign-in.
        logger.exception("weixin: failed to persist QR sign-in")
        return web.json_response({"error": "persist_failed", "detail": str(exc)[:200]}, status=500)
    finally:
        await client.close()
        _SESSIONS.pop(session_id, None)

    logger.info(
        "weixin: QR login confirmed; account_id=%s persisted. Restart to connect.", account_id[:8]
    )
    return web.json_response({"status": "confirmed", "connected": True, "account_id": account_id})


def setup_weixin_routes(app: web.Application) -> None:
    """Register the Weixin QR-login routes."""
    app.router.add_post("/api/channels/weixin/qr/start", weixin_qr_start)
    app.router.add_get("/api/channels/weixin/qr/status", weixin_qr_status)
    app.router.add_get("/api/weixin/config", weixin_config_get)
    app.router.add_put("/api/weixin/config", weixin_config_save)


async def weixin_config_get(request: web.Request) -> web.Response:
    """GET /api/weixin/config — status + policy. Never returns the credential."""

    def _load() -> tuple[Any, bool]:
        cfg = KiroCrewConfig.load()
        # propagate=False: a post-boot dashboard read must not re-seed
        # os.environ. A setdefault here on a worker thread could restore a
        # credential the live watcher just popped (an external .env removal
        # racing this read), silently undoing the removal. Boot owns the one
        # propagation; the live applier owns every mutation after.
        creds = cfg.load_credentials(propagate=False)
        return cfg.weixin, bool(creds.get(CRED_WEIXIN_TOKEN, "") or cfg.weixin.token)

    # Off-loop: both config.json and the .env credential store are read from disk.
    wx, has_cred = await asyncio.to_thread(_load)
    state = request.app.get("state")
    return web.json_response(
        {
            # True only when the long-poll transport actually started this session.
            "connected": bool(getattr(state, "weixin_connected", False)),
            "connect_error": str(getattr(state, "weixin_connect_error", ""))[:120],
            "configured": bool(has_cred and wx.account_id and wx.enabled),
            "read_only": not is_direct_local_request(request),
            # Presence only — the credential itself is never serialized.
            "credential_set": has_cred,
            "enabled": bool(wx.enabled),
            "account_id": wx.account_id,
            "dm_policy": wx.dm_policy,
            "allowed_user_ids": [str(u) for u in wx.allowed_user_ids],
            "session_folder": wx.session_folder,
        }
    )


async def weixin_config_save(request: web.Request) -> web.Response:
    """PUT /api/weixin/config — persist policy fields to config.json.

    Serialized with every other config.json writer via the repository-wide
    ``_get_config_lock()``; loopback-only like the sibling channel savers.
    """
    if not is_direct_local_request(request):
        return web.json_response(
            {"error": "read-only from remote sessions (local machine only)"}, status=403
        )
    try:
        body = await request.json()
    except Exception:
        return web.json_response({"error": "invalid JSON"}, status=400)
    if not isinstance(body, dict):
        return web.json_response({"error": "body must be an object"}, status=400)

    if "enabled" in body and not isinstance(body["enabled"], bool):
        return web.json_response({"error": "enabled must be a boolean"}, status=400)
    if "dm_policy" in body and body["dm_policy"] not in ("open", "allowlist", "disabled"):
        return web.json_response({"error": "invalid dm_policy"}, status=400)
    if "allowed_user_ids" in body and not isinstance(body["allowed_user_ids"], list):
        return web.json_response({"error": "allowed_user_ids must be a list"}, status=400)
    if "session_folder" in body:
        try:
            session_folder = clean_session_folder(body["session_folder"])
        except ValueError as exc:
            return web.json_response(
                {"error": str(exc), "code": "invalid_session_folder"}, status=400
            )

    async with _get_config_lock():
        cp = config_path()

        def _read_config() -> Dict[str, Any]:
            return json.loads(read_config_text(cp)) if cp.exists() else {}

        # Off-loop read: a large or slow config.json must not stall the gateway
        # event loop. Reading under the lock keeps the snapshot current relative
        # to every other config writer (mirrors the Telegram saver).
        try:
            data: Dict[str, Any] = await asyncio.to_thread(_read_config)
        except Exception:
            return web.json_response({"error": "config.json is corrupt"}, status=500)
        if not isinstance(data, dict):
            return web.json_response({"error": "config.json is corrupt"}, status=500)
        if not isinstance(data.get("weixin"), dict):
            data["weixin"] = {}
        wx = data["weixin"]
        if "enabled" in body:
            wx["enabled"] = bool(body["enabled"])
        if "dm_policy" in body:
            wx["dm_policy"] = str(body["dm_policy"])
        if "allowed_user_ids" in body:
            wx["allowed_user_ids"] = [
                str(u).strip() for u in body["allowed_user_ids"] if str(u).strip()
            ]
        if "session_folder" in body:
            wx["session_folder"] = session_folder
        serialized = json.dumps(data, ensure_ascii=False, indent=2)
        await asyncio.to_thread(_atomic_write, cp, serialized)

        # Create the configured session folder now, on this user-initiated save,
        # so the reconcile path never has to write the folder store. Best-effort:
        # a failure leaves conversations unfiled until the next save.
        _folder_name = stored_folder_name(wx.get("session_folder"))
        if _folder_name:
            _state = request.app.get("state")
            if _state is not None:
                await ensure_channel_folder(
                    _state,
                    "weixin",
                    _folder_name,
                    relabel="session_folder" in body,
                )

    # Answer only after the watcher applied the write: this save carries the
    # allow-list, so a removed sender must be unauthorized on the running
    # transport before the caller sees "saved", not one poll interval later.
    await _hot_apply_after_write()
    # Per field: session_folder is re-read live by the channel-slot reconciler,
    # and the connection fields (token, account_id, base_url, enabled) are what a
    # restart is actually for.
    return web.json_response(
        {
            "ok": True,
            "restart_required": channel_restart_required("weixin", body.keys()),
        }
    )
