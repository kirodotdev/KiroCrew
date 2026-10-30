"""In-Crew Tailscale invites: share this machine, learn who accepted, revoke.

The owner connects a Tailscale API access key once. After that the People
surface can invite a teammate by email or make an invite link, and Crew does the
rest:

1. :func:`connect` checks the key and finds this machine's device id by node key
   (then tailnet address) from the local daemon, never by name;
2. :func:`create_invites` asks Tailscale for the share invites and records each
   with the optional alias and avatar the owner chose;
3. :func:`sync` reads the device's invites back. For each newly accepted one it
   binds the accepting user's stable Tailscale id and real login to the alias,
   grants it live (:mod:`kiro_crew.dashboard.tailnet_live_allowlist`) and
   recomputes the derived access set (``access_set``), which admits that login.
   The login is also mirrored into ``dashboard.tailscale.allowed_logins`` so the
   ``tailscale serve`` path turns identity trust on; that list is not where the
   direct listener reads access from. The accepting login is used even when it
   differs from the invited email: Tailscale lets an invite be accepted from any
   account, and the account that accepted is the one that connects;
4. :func:`revoke` deletes the invite at Tailscale, which ends the share, takes
   the login out of the set and refuses it live on the ``tailscale serve`` path.

Alias and avatar are display only. Access always comes from the verified login.
Every invite, accept and revoke is written to the security event log with the
actor, a digest of the invited email and the outcome.
"""

from __future__ import annotations

import asyncio
import logging
import re
import weakref
from dataclasses import dataclass
from typing import Any, Callable, Optional

from kiro_crew.shared_crew import access_set
from kiro_crew.shared_crew import tailscale_invite_store as store
from kiro_crew.shared_crew.tailscale_api import (
    CODE_NOT_FOUND,
    CODE_RATE_LIMITED,
    DeviceInvite,
    TailscaleApi,
    TailscaleApiError,
    api_key_id,
)

logger = logging.getLogger(__name__)

#: Where the owner shares this machine by hand when no key is connected.
ADMIN_MACHINES_URL = "https://login.tailscale.com/admin/machines"
ADMIN_KEYS_URL = "https://login.tailscale.com/admin/settings/keys"
MAX_EMAILS = 20
_EMAIL_RE = re.compile(r"\A[^@\s]{1,128}@[^@\s]{1,253}\Z")
#: How soon before expiry the status read warns the owner.
EXPIRY_WARN_SECS = 7 * 24 * 3600

#: Poll schedule while invites are pending.
POLL_BASE_SECS = 60.0
POLL_MAX_SECS = 900.0
#: Revoked invite ids remembered so an in-flight sync admits nobody by them. The
#: share is deleted in Tailscale at revoke, so only a sync already holding it
#: can see it again; the newest ids are kept.
REVOKED_IDS_KEPT = 512
#: A People page open syncs at most this often.
SYNC_MIN_INTERVAL_SECS = 15.0

CODE_NOT_CONNECTED = "not_connected"
CODE_BAD_KEY = "invalid_key_format"
CODE_DEVICE_NOT_FOUND = "device_not_found"
CODE_DAEMON = "daemon_unavailable"
CODE_BAD_EMAIL = "invalid_email"
CODE_BACKING_OFF = "backing_off"


class InviteError(Exception):
    def __init__(self, code: str, retry_after: float = 0.0) -> None:
        super().__init__(code)
        self.code = code
        self.retry_after = retry_after


@dataclass(frozen=True)
class DaemonSelf:
    node_key: str
    addresses: tuple[str, ...]
    tailnet: str
    login: str


def _read_daemon_self() -> Optional[DaemonSelf]:
    from kiro_crew.dashboard import tailnet

    status = tailnet._run_json(["status", "--json"])
    if not isinstance(status, dict) or not isinstance(status.get("Self"), dict):
        return None
    me = status["Self"]
    ips = me.get("TailscaleIPs")
    current = status.get("CurrentTailnet")
    name = current.get("Name") if isinstance(current, dict) else ""
    return DaemonSelf(
        node_key=str(me.get("PublicKey") or ""),
        addresses=tuple(a for a in ips if isinstance(a, str)) if isinstance(ips, list) else (),
        tailnet=str(name or status.get("MagicDNSSuffix") or ""),
        login=tailnet._self_login(status),
    )


#: Seams the tests replace: the daemon read and the API client.
daemon_self_reader: Callable[[], Optional[DaemonSelf]] = _read_daemon_self
api_factory: Callable[[str], TailscaleApi] = lambda key: TailscaleApi(key)  # noqa: E731


def _norm_login(login: str) -> str:
    return str(login or "").strip().lower()


def _nodekey(raw: str) -> str:
    return raw.strip().lower().removeprefix("nodekey:")


def _audit(operation: str, outcome: str, actor: str, *, email: str = "", detail: str = "") -> None:
    resources = f"email_sha256:{store.email_hash(email)}" if email else ""
    try:
        from kiro_crew.sel import sel

        sel().log_api_access(
            caller=actor or "crew",
            operation=f"shared_crew.invite.{operation}",
            outcome=outcome,
            source="shared-crew-invites",
            resources=resources,
            error=detail,
        )
    except Exception:  # noqa: BLE001 -- the audit never breaks the action
        logger.debug("invite audit write failed", exc_info=True)


async def _audit_async(*args: Any, **kwargs: Any) -> None:
    await asyncio.to_thread(_audit, *args, **kwargs)


def _api() -> tuple[TailscaleApi, str]:
    key = store.load_credential()
    if not key:
        raise InviteError(CODE_NOT_CONNECTED)
    device_id = str(store.load_state().get("device_id") or "")
    if not device_id:
        raise InviteError(CODE_NOT_CONNECTED)
    return api_factory(key), device_id


# ── connect ──


async def connect(key: str, actor: str) -> dict[str, Any]:
    key = key.strip()
    key_id = api_key_id(key)
    if not key_id:
        raise InviteError(CODE_BAD_KEY)
    me = await asyncio.to_thread(daemon_self_reader)
    if me is None or not (me.node_key or me.addresses):
        raise InviteError(CODE_DAEMON)
    api = api_factory(key)
    try:
        devices = await api.list_devices()
    except TailscaleApiError as exc:
        await _audit_async("connect", "denied", actor, detail=exc.code)
        raise
    want = _nodekey(me.node_key)
    device = next((d for d in devices if want and _nodekey(d.node_key) == want), None)
    if device is None:
        mine = set(me.addresses)
        device = next((d for d in devices if mine & set(d.addresses)), None)
    if device is None:
        await _audit_async("connect", "denied", actor, detail=CODE_DEVICE_NOT_FOUND)
        raise InviteError(CODE_DEVICE_NOT_FOUND)
    try:
        expires = await api.key_expiry(key_id)
    except TailscaleApiError:
        expires = ""
    await asyncio.to_thread(store.save_credential, key)

    def _mutate(state: dict[str, Any]) -> None:
        state.update(
            device_id=device.node_id,
            tailnet=me.tailnet,
            key_expires=expires,
            connected_at=store.now(),
            backoff_until=0.0,
            backoff_secs=0.0,
        )

    await asyncio.to_thread(store.update_state, _mutate)
    await _audit_async("connect", "granted", actor)
    return await status_view()


async def disconnect(actor: str) -> None:
    await asyncio.to_thread(store.delete_credential)

    def _mutate(state: dict[str, Any]) -> None:
        state.update(device_id="", key_expires="", connected_at=0.0)

    await asyncio.to_thread(store.update_state, _mutate)
    await _audit_async("disconnect", "granted", actor)


# ── views ──


def _expiry_epoch(raw: str) -> float:
    from datetime import datetime

    try:
        return datetime.fromisoformat(raw.replace("Z", "+00:00")).timestamp()
    except ValueError:
        return 0.0


def _invite_row(row: dict[str, Any], members: dict[str, Any]) -> dict[str, Any]:
    member = members.get(str(row.get("accepted_user_id") or ""))
    out = {
        "id": row.get("id"),
        "kind": "email" if row.get("email") else "link",
        "email": row.get("email") or "",
        "multi_use": bool(row.get("multi_use")),
        "alias": row.get("alias") or "",
        "avatar_id": row.get("avatar_id") or "",
        "created_at": row.get("created_at") or 0.0,
        "status": "accepted" if member else "pending",
        "accepted_login": member.get("login", "") if member else "",
    }
    if not row.get("email"):
        # The owner copies a link invite; an email invite went by mail.
        out["invite_url"] = row.get("invite_url") or ""
    return out


def _member_row(member: dict[str, Any]) -> dict[str, Any]:
    return {
        "user_id": member.get("user_id"),
        "login": member.get("login"),
        "alias": member.get("alias") or "",
        "avatar_id": member.get("avatar_id") or "",
        "profile_pic_url": member.get("profile_pic_url") or "",
        "invite_id": member.get("invite_id") or "",
        "accepted_at": member.get("accepted_at") or 0.0,
    }


async def status_view() -> dict[str, Any]:
    connected = await asyncio.to_thread(store.credential_present)
    state = await asyncio.to_thread(store.load_state)
    expires = str(state.get("key_expires") or "")
    expiry = _expiry_epoch(expires) if expires else 0.0
    now = store.now()
    members = state.get("members") or {}
    return {
        "connected": bool(connected and state.get("device_id")),
        "tailnet": state.get("tailnet") or "",
        "key_expires": expires,
        "key_expired": bool(expiry and expiry <= now),
        "key_expires_soon": bool(expiry and now < expiry <= now + EXPIRY_WARN_SECS),
        "admin_share_url": ADMIN_MACHINES_URL,
        "admin_keys_url": ADMIN_KEYS_URL,
        "invites": [
            _invite_row(row, members)
            for row in sorted(
                (state.get("invites") or {}).values(),
                key=lambda r: -float(r.get("created_at") or 0.0),
            )
        ],
        "members": [_member_row(m) for m in members.values() if not m.get("removed")],
        "last_sync_at": state.get("last_sync_at") or 0.0,
    }


def members_by_login() -> dict[str, dict[str, Any]]:
    """Each accepted person's display record, keyed by lower-cased verified login."""
    return {
        str(m.get("login") or "").strip().lower(): _member_row(m)
        for m in (store.load_state().get("members") or {}).values()
        if m.get("login") and not m.get("removed")
    }


# ── create, resend ──


def clean_emails(raw: object) -> list[str]:
    if not isinstance(raw, list) or len(raw) > MAX_EMAILS:
        raise InviteError(CODE_BAD_EMAIL)
    out: list[str] = []
    for entry in raw:
        if not isinstance(entry, str) or not _EMAIL_RE.match(entry.strip()):
            raise InviteError(CODE_BAD_EMAIL)
        email = entry.strip()
        if email.lower() not in {e.lower() for e in out}:
            out.append(email)
    return out


async def create_invites(
    emails: list[str], *, multi_use: bool, alias: str, avatar_id: str, actor: str
) -> list[dict[str, Any]]:
    api, device_id = await asyncio.to_thread(_api)
    try:
        made = await api.create_invites(device_id, emails, multi_use=multi_use and not emails)
    except TailscaleApiError as exc:
        for email in emails or [""]:
            await _audit_async("create", "error", actor, email=email, detail=exc.code)
        raise
    alias = store.clean_alias(alias)
    avatar_id = avatar_id if store.valid_avatar_id(avatar_id) else ""
    created_at = store.now()

    def _mutate(state: dict[str, Any]) -> None:
        for invite in made:
            state["invites"][invite.id] = {
                "id": invite.id,
                "email": invite.email,
                "multi_use": invite.multi_use or (multi_use and not emails),
                "invite_url": invite.invite_url,
                "alias": alias,
                "avatar_id": avatar_id,
                "created_at": created_at,
                "accepted_user_id": "",
            }

    state = await asyncio.to_thread(store.update_state, _mutate)
    for invite in made:
        await _audit_async("create", "granted", actor, email=invite.email)
    members = state.get("members") or {}
    return [_invite_row(state["invites"][i.id], members) for i in made]


async def resend(invite_id: str, actor: str) -> None:
    api, _ = await asyncio.to_thread(_api)
    row = ((await asyncio.to_thread(store.load_state)).get("invites") or {}).get(invite_id)
    try:
        await api.resend_invite(invite_id)
    except TailscaleApiError as exc:
        await _audit_async(
            "resend", "error", actor, email=(row or {}).get("email", ""), detail=exc.code
        )
        raise
    await _audit_async("resend", "granted", actor, email=(row or {}).get("email", ""))


# ── accept: sync with Tailscale ──

AllowlistWriter = Callable[[str, bool], Any]


async def _default_allowlist_writer(login: str, add: bool) -> bool:
    from kiro_crew.shared_crew import tailscale_allowlist

    return await tailscale_allowlist.write(login, add=add)


#: Writes ``dashboard.tailscale.allowed_logins``; returns whether a restart is
#: needed for it to apply. Replaced in tests.
allowlist_writer: Callable[[str, bool], Any] = _default_allowlist_writer


#: One accept-or-revoke commit at a time, per event loop: a revoke landing while
#: ``sync`` admits the same invite runs before or after it, never between.
_COMMIT_LOCKS: weakref.WeakKeyDictionary[asyncio.AbstractEventLoop, asyncio.Lock] = (
    weakref.WeakKeyDictionary()
)


def _commit_lock() -> asyncio.Lock:
    loop = asyncio.get_running_loop()
    lock = _COMMIT_LOCKS.get(loop)
    if lock is None:
        lock = _COMMIT_LOCKS[loop] = asyncio.Lock()
    return lock


def _member_invite_ids(member: dict[str, Any]) -> set[str]:
    """Every invite a member came in by: the recorded list, and the latest one."""
    ids = {str(i) for i in member.get("invite_ids") or () if i}
    if member.get("invite_id"):
        ids.add(str(member["invite_id"]))
    return ids


def _new_accepts(
    state: dict[str, Any], invites: list[DeviceInvite]
) -> list[tuple[DeviceInvite, dict[str, Any]]]:
    out = []
    for invite in invites:
        if not (invite.accepted and invite.accepted_by):
            continue
        row = state["invites"].get(invite.id)
        if row is None:
            # Made in the admin console, not by Crew: still a person who may connect.
            row = {"id": invite.id, "email": invite.email, "alias": "", "avatar_id": ""}
        uid = invite.accepted_by.user_id
        known = state["members"].get(uid)
        # A person the owner removed comes back only through a new invite, even
        # when their login changed since; matched by stable user id against every
        # invite they ever came in by.
        if known and known.get("removed") and invite.id in _member_invite_ids(known):
            continue
        if known and not known.get("removed") and known.get("login") == invite.accepted_by.login:
            continue
        out.append((invite, row))
    return out


def _unrecorded_accepts(
    state: dict[str, Any], invites: list[DeviceInvite], skip: set[str]
) -> dict[str, set[str]]:
    """Accepted invite ids a current member holds that their row does not record yet."""
    out: dict[str, set[str]] = {}
    for invite in invites:
        if invite.id in skip or not (invite.accepted and invite.accepted_by):
            continue
        uid = invite.accepted_by.user_id
        member = state["members"].get(uid)
        if isinstance(member, dict) and invite.id not in _member_invite_ids(member):
            out.setdefault(uid, set()).add(invite.id)
    return out


async def sync(*, actor: str = "crew", force: bool = False) -> dict[str, Any]:
    """Read invites back from Tailscale and admit each newly accepted person."""
    api, device_id = await asyncio.to_thread(_api)
    state = await asyncio.to_thread(store.load_state)
    now = store.now()
    if not force and now < float(state.get("backoff_until") or 0.0):
        raise InviteError(CODE_BACKING_OFF, float(state["backoff_until"]) - now)
    try:
        invites = await api.list_invites(device_id)
    except TailscaleApiError as exc:
        prior = float(state.get("backoff_secs") or 0.0)
        wait = max(exc.retry_after, min(POLL_MAX_SECS, prior * 2 or POLL_BASE_SECS))

        def _back(s: dict[str, Any]) -> None:
            s.update(backoff_secs=wait, backoff_until=store.now() + wait)

        await asyncio.to_thread(store.update_state, _back)
        if exc.code == CODE_RATE_LIMITED:
            raise InviteError(CODE_RATE_LIMITED, wait) from exc
        raise
    known = _known_invite_ids(state)
    async with _commit_lock():
        return await _commit_accepts(invites, known, actor)


def _known_invite_ids(state: dict[str, Any]) -> set[str]:
    """Invite ids Crew tracks: its own rows, and the invite each member came in by.

    A console-created invite has no row, only a member; revoking it drops that
    member, so a sync fetched before the revoke finds it gone here too.
    """
    ids = set(state.get("invites") or {})
    for member in (state.get("members") or {}).values():
        if isinstance(member, dict):
            ids |= _member_invite_ids(member)
    return ids


async def _commit_accepts(
    invites: list[DeviceInvite], known: set[str], actor: str
) -> dict[str, Any]:
    """Admit each newly accepted person, against the state as it is now.

    Runs under :func:`_commit_lock`. The state is read again here: an invite
    Crew made that is gone from it now (*known* held it when the list was
    fetched) was revoked meanwhile and admits nobody.
    """
    state = await asyncio.to_thread(store.load_state)
    # Gone from the rows since the fetch, or revoked at any time: a revoked id
    # is recorded durably, because a member row can still name it (a removed
    # member keeps every invite they came in by) and so hide it from the diff.
    revoked = (known - _known_invite_ids(state)) | set(state.get("revoked_invite_ids") or ())
    accepts = [(i, r) for i, r in _new_accepts(state, invites) if i.id not in revoked]
    # An accept skipped because its person is already a member is still theirs:
    # recorded, so a later removal refuses it like every invite they came in by.
    seen = _unrecorded_accepts(state, invites, {i.id for i, _r in accepts} | revoked)
    admitted: list[dict[str, Any]] = []
    for invite, row in accepts:
        by = invite.accepted_by
        assert by is not None
        admitted.append(
            {
                "user_id": by.user_id,
                "login": by.login,
                "alias": row.get("alias") or "",
                "avatar_id": row.get("avatar_id") or "",
                "profile_pic_url": by.profile_pic_url,
                "invite_id": invite.id,
                "accepted_at": store.now(),
                # The config mirror is written after this row is durable, and
                # retried on the next sync until it lands.
                "mirror_pending": True,
                "_email": row.get("email", ""),
            }
        )
    live_ids = {i.id for i in invites}

    replaced_logins: list[tuple[str, str]] = []

    def _mutate(s: dict[str, Any]) -> None:
        for uid, invite_ids in seen.items():
            member_row = s["members"].get(uid)
            if isinstance(member_row, dict):
                member_row["invite_ids"] = sorted(_member_invite_ids(member_row) | invite_ids)
                for invite_id in invite_ids:
                    invite_row = s["invites"].get(invite_id)
                    if invite_row is not None:
                        invite_row["accepted_user_id"] = uid
        for member in admitted:
            prior = s["members"].get(member["user_id"]) or {}
            # A rename replaces the row; keep the old login reachable for removal.
            replaced_logins.append((member["user_id"], str(prior.get("login") or "")))
            # A later accept never drops a display choice the owner made.
            member["alias"] = member["alias"] or prior.get("alias", "")
            member["avatar_id"] = member["avatar_id"] or prior.get("avatar_id", "")
            member["invite_ids"] = sorted(_member_invite_ids(prior) | _member_invite_ids(member))
            # Every login this person came in under, kept on disk so a revoke after
            # a restart still withdraws the ones they renamed away from.
            member["logins"] = sorted(
                (
                    {str(lg) for lg in prior.get("logins") or ()}
                    | {prior.get("login"), member["login"]}
                )
                - {None, ""}
            )
            s["members"][member["user_id"]] = {k: v for k, v in member.items() if k != "_email"}
            row = s["invites"].get(member["invite_id"])
            if row is not None:
                row["accepted_user_id"] = member["user_id"]
        # An invite absent from Tailscale's list that nobody accepted has expired
        # or was revoked in the console.
        # Only an invite this sync knew before it fetched: one created since is
        # not in that list yet, and stays.
        for invite_id in list(s["invites"]):
            if (
                invite_id in known
                and invite_id not in live_ids
                and not s["invites"][invite_id].get("accepted_user_id")
            ):
                del s["invites"][invite_id]
        s.update(backoff_secs=0.0, backoff_until=0.0, last_sync_at=store.now())

    # The membership row is durable before any grant (config mirror, live set),
    # so a revoke always finds the person, whichever later write fails.
    await asyncio.to_thread(store.update_state, _mutate)
    from kiro_crew.dashboard import tailnet_live_allowlist

    for uid, old_login in replaced_logins:
        tailnet_live_allowlist.note_login(uid, old_login)
    for member in admitted:
        tailnet_live_allowlist.note_login(member["user_id"], member["login"])
        tailnet_live_allowlist.grant(member["login"])
        await _audit_async(
            "accept",
            "granted",
            actor,
            email=member["_email"],
            detail=f"login_sha256:{store.email_hash(member['login'])}",
        )
    if admitted:
        await asyncio.to_thread(access_set.rebuild)
    restart = await _write_pending_mirrors(actor)
    return {"admitted": [m["login"] for m in admitted], "restart_required": restart}


async def _write_pending_mirrors(actor: str) -> bool:
    """Mirror each member still marked ``mirror_pending`` into ``allowed_logins``.

    Runs under :func:`_commit_lock`, after the membership write. A failed write
    keeps the mark, so the next sync tries again; a removed member is skipped.
    """
    state = await asyncio.to_thread(store.load_state)
    pending = [
        (uid, str(m.get("login") or ""))
        for uid, m in (state.get("members") or {}).items()
        if isinstance(m, dict) and m.get("mirror_pending") and not m.get("removed")
    ]
    restart = False
    done: set[str] = set()
    for uid, login in pending:
        if not login:
            continue
        try:
            restart = bool(await allowlist_writer(login, True)) or restart
        except Exception as exc:  # noqa: BLE001 -- the mark stays; the next sync retries
            logger.warning("adding an accepted login to the allowlist failed", exc_info=True)
            await _audit_async("accept", "error", actor, detail=type(exc).__name__)
            continue
        done.add(uid)
    if done:

        def _clear(s: dict[str, Any]) -> None:
            for uid in done:
                row = s["members"].get(uid)
                if isinstance(row, dict):
                    row.pop("mirror_pending", None)

        await asyncio.to_thread(store.update_state, _clear)
    return restart


def pending_count() -> int:
    """Invites still worth a sync: unaccepted ones, and every reusable link."""
    state = store.load_state()
    rows = (state.get("invites") or {}).values()
    return sum(1 for r in rows if r.get("multi_use") or not r.get("accepted_user_id"))


# ── revoke ──


async def revoke(invite_id: str, actor: str) -> dict[str, Any]:
    result, _logins = await revoke_with_logins(invite_id, actor)
    return result


async def revoke_with_logins(invite_id: str, actor: str) -> tuple[dict[str, Any], frozenset[str]]:
    """:func:`revoke`, plus every login it withdrew (for closing their open sockets)."""
    api, _ = await asyncio.to_thread(_api)
    async with _commit_lock():
        row, members, withdrawn = await _revoke_locked(api, invite_id, actor)
    await _audit_async("revoke", "granted", actor, email=row.get("email", ""))
    return {"revoked": invite_id, "member_removed": bool(members)}, withdrawn


async def _revoke_locked(
    api: TailscaleApi, invite_id: str, actor: str
) -> tuple[dict[str, Any], list[dict[str, Any]], frozenset[str]]:
    """Delete the share and drop every member it admitted; ``(row, members, logins withdrawn)``."""
    state = await asyncio.to_thread(store.load_state)
    row = (state.get("invites") or {}).get(invite_id) or {}
    all_members = state.get("members") or {}
    member_ids = {str(row.get("accepted_user_id") or "")} | {
        uid
        for uid, m in all_members.items()
        if isinstance(m, dict) and invite_id in _member_invite_ids(m)
    }
    members = [all_members[uid] for uid in sorted(member_ids) if uid in all_members]
    try:
        await api.delete_invite(invite_id)
    except TailscaleApiError as exc:
        if exc.code != CODE_NOT_FOUND:
            await _audit_async(
                "revoke", "error", actor, email=row.get("email", ""), detail=exc.code
            )
            raise
    from kiro_crew.dashboard import tailnet_live_allowlist

    # This machine's own user (and every login the daemon named for it) is never
    # revoked, even when that account once accepted the invite being revoked.
    snap = tailnet_live_allowlist.snapshot()
    owner_id = snap.owner_user_id if snap is not None else ""
    owner_logins = {_norm_login(access_set.owner_login())} | {
        _norm_login(lg) for lg in tailnet_live_allowlist.logins_for(owner_id)
    }
    owner_logins.discard("")
    withdrawn: set[str] = set()
    for member in members:
        # Every login they came in under: the current one, the ones recorded on
        # disk at each accept, and any the daemon named for them since start.
        aliases = (
            {str(member.get("login") or "")}
            | {str(lg) for lg in member.get("logins") or ()}
            | set(tailnet_live_allowlist.logins_for(str(member.get("user_id") or "")))
        )
        for alias in sorted({a for a in aliases if a}):
            if _norm_login(alias) in owner_logins:
                continue
            withdrawn.add(_norm_login(alias))
            tailnet_live_allowlist.revoke(alias)
            # Durable before the config mirror: a seed entry would admit them again
            # at the next start even when the config edit below fails.
            await asyncio.to_thread(access_set.drop_from_seed, alias)
            try:
                await allowlist_writer(alias, False)
            except Exception:  # noqa: BLE001 -- the live revoke already holds
                logger.warning("removing a revoked login from the allowlist failed", exc_info=True)

    def _mutate(s: dict[str, Any]) -> None:
        s["invites"].pop(invite_id, None)
        # Durable: a sync already holding this invite as accepted admits nobody by it.
        ids = [i for i in s.get("revoked_invite_ids") or () if i != invite_id]
        s["revoked_invite_ids"] = (ids + [invite_id])[-REVOKED_IDS_KEPT:]
        for uid in member_ids:
            # Kept, marked removed, with every invite they came in by: another
            # invite they accepted stays refused, and only a new one lets them in.
            member_row = s["members"].get(uid)
            if isinstance(member_row, dict):
                member_row["removed"] = True
                member_row["invite_ids"] = sorted(_member_invite_ids(member_row) | {invite_id})

    await asyncio.to_thread(store.update_state, _mutate)
    if members:
        await asyncio.to_thread(access_set.rebuild)
    return row, members, frozenset(withdrawn)


# ── background poll ──


async def poll_forever(sleep: Callable[[float], Any] = asyncio.sleep) -> None:
    """Sync while connected: often while invites are pending, slowly otherwise.

    A slow round still runs with nothing pending here, so an invite made and
    accepted in the admin console is found too. Failures back off.
    """
    delay = POLL_BASE_SECS
    while True:
        await sleep(delay)
        try:
            if not await asyncio.to_thread(store.credential_present):
                delay = POLL_BASE_SECS
                continue
            pending = await asyncio.to_thread(pending_count)
            await sync()
            delay = POLL_BASE_SECS if pending else POLL_MAX_SECS
        except InviteError as exc:
            delay = min(POLL_MAX_SECS, max(POLL_BASE_SECS, exc.retry_after or delay * 2))
        except TailscaleApiError as exc:
            delay = min(POLL_MAX_SECS, max(exc.retry_after, delay * 2))
        except asyncio.CancelledError:
            raise
        except Exception:  # noqa: BLE001 -- the poller outlives one bad round
            logger.warning("invite poll failed", exc_info=True)
            delay = min(POLL_MAX_SECS, delay * 2)
