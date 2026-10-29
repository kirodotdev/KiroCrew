"""Who may reach this Crew over Tailscale: computed, never typed.

Tailscale is the only access authority. The set of people who get in is
derived, and this module derives it:

* the owner (this machine's own Tailscale user);
* each person who accepted a Crew Tailscale invite, by the login Tailscale's
  API reported for them (``tailscale_invites``);
* each person the owner approved from the Waiting list, by stable Tailscale
  user id;
* once, at the first start, the logins and ``tag:`` entries already in
  ``dashboard.tailscale.allowed_logins`` (the migration seed). After that a hand
  edit of the config list admits nobody.

A same-tailnet person the Tailscale ACL lets reach this machine is not in the
set. The direct tailnet listener refuses them with a "waiting for the owner"
page and :func:`note_waiting` lists them (name, avatar and first sighting from
``whois``) so the owner can Approve or Dismiss them on People. The list is
bounded (:data:`MAX_WAITING`, oldest dropped) and rate-limited.

Removing someone (:func:`stop_access`) refuses their next request, closes their
dashboard sockets, cancels their queued work and stops their running turn
through the admission service's own cancel path, which frees the slot only once
the runtime has stopped. Finished work is kept.

The state file lives in ``<data_home>/.vault/shared-crew-tailscale/`` beside the
invite records, a directory every sandbox mode masks, so an agent's tools can
neither read nor plant an approval. A file that cannot be read admits nobody
but the owner. The computed set is published to
:mod:`kiro_crew.dashboard.tailnet_live_allowlist`, which the auth middleware
reads.
"""

from __future__ import annotations

import asyncio
import json
import logging
import threading
import time
from collections import deque
from typing import TYPE_CHECKING, Any, Awaitable, Callable, Optional

from kiro_crew.atomic_write import atomic_write
from kiro_crew.dashboard import tailnet_live_allowlist
from kiro_crew.dashboard.tailnet_live_allowlist import AccessSnapshot, _norm
from kiro_crew.shared_crew import tailscale_invite_store as store

if TYPE_CHECKING:
    from kiro_crew.dashboard.tailnet_direct import DirectPeer

logger = logging.getLogger(__name__)

ACCESS_FILE = "access.json"
SCHEMA = 1
MAX_WAITING = 50
#: A listed person's sighting is refreshed at most this often.
WAITING_REFRESH_SECS = 60.0
#: At most this many NEW people join the Waiting list per minute.
NEW_WAITING_PER_MINUTE = 10
#: A dismissed person is not listed again for this long.
DISMISS_SECS = 24 * 3600.0
MAX_DISMISSED = 200
_DISPLAY_MAX = 128

CODE_NOT_WAITING = "not_waiting"
CODE_NOT_APPROVED = "not_approved"
CODE_ACK_REQUIRED = "first_share_ack_required"
CODE_UNREADABLE = "access_unreadable"

OUTCOME_ACCESS_REMOVED = "stopped: access removed"


class AccessError(Exception):
    def __init__(self, code: str) -> None:
        super().__init__(code)
        self.code = code


class _Unreadable(Exception):
    pass


_LOCK = threading.RLock()
_STATE: Optional[dict[str, Any]] = None
_OWNER: dict[str, str] = {"login": "", "user_id": ""}
#: ``dashboard.shared_crew.local_authorization`` as the gateway started with it.
_MODE: dict[str, bool] = {"local_authorization": True}
_NEW_TIMES: deque[float] = deque()
_TOUCHED: dict[str, float] = {}

#: Seams the tests replace.
clock: Callable[[], float] = time.time


async def _no_socket_closer(_principal_ids: frozenset[str], _login: str) -> int:
    return 0


#: Closes the dashboard sockets of the given principals; the routes install the real one.
socket_closer: Callable[[frozenset[str], str], Awaitable[int]] = _no_socket_closer


async def _no_slot_work_stopper(_principal_ids: frozenset[str]) -> dict[str, int]:
    return {"queued": 0, "running": 0}


#: Stops the given principals' work that no admitted record covers (fair
#: admission off): their queued follow-ups and their running turns. The routes
#: install the real one.
slot_work_stopper: Callable[[frozenset[str]], Awaitable[dict[str, int]]] = _no_slot_work_stopper


def _empty() -> dict[str, Any]:
    return {
        "schema": SCHEMA,
        "seeded": False,
        "seed": {"logins": [], "tags": []},
        "approvals": {},
        "waiting": {},
        "dismissed": {},
        "first_share_ack": None,
    }


def _path() -> Any:
    return store.root() / ACCESS_FILE


def _load_strict() -> dict[str, Any]:
    """The state on disk; an absent file is empty, a damaged one raises ``_Unreadable``."""
    path = _path()
    try:
        raw = path.read_text(encoding="utf-8")
    except FileNotFoundError:
        return _empty()
    except (OSError, UnicodeDecodeError) as exc:
        raise _Unreadable(str(exc)) from exc
    try:
        data = json.loads(raw)
    except ValueError as exc:
        raise _Unreadable("not json") from exc
    if not isinstance(data, dict) or data.get("schema") != SCHEMA:
        raise _Unreadable("schema")
    out = _empty()
    out.update({k: v for k, v in data.items() if k in out})
    for key in ("approvals", "waiting", "dismissed"):
        if not isinstance(out[key], dict):
            raise _Unreadable(key)
    seed = out["seed"]
    if not isinstance(seed, dict) or not all(
        isinstance(seed.get(k), list) for k in ("logins", "tags")
    ):
        raise _Unreadable("seed")
    return out


def _save(state: dict[str, Any]) -> None:
    """Write *state*; on a failed write the cache is dropped, so the disk stays the truth."""
    global _STATE
    try:
        store.ensure_root()
        atomic_write(_path(), json.dumps(state, indent=2, sort_keys=True), restrict_to_owner=True)
    except BaseException:
        _STATE = None
        raise


_STATE_HOME = ""

#: ``allowed_logins`` as this start read it, split into logins and tags; None
#: until a readable list was seen. :func:`rebuild` admits only seed entries
#: still on it, so a seed withdrawal the disk refused to record still holds.
_SEED_LIMIT: Optional[tuple[frozenset[str], frozenset[str]]] = None


def _state() -> dict[str, Any]:
    """The cached state, loading it on first use (or for a new data home). Raises ``_Unreadable``."""
    global _STATE, _STATE_HOME
    home = str(store.root())
    if _STATE is None or _STATE_HOME != home:
        _STATE = None
        _STATE = _load_strict()
        _STATE_HOME = home
    return _STATE


def _home() -> str:
    from kiro_crew.config.paths import config_dir

    return str(config_dir())


def _split_seed(entries: tuple[str, ...]) -> tuple[list[str], list[str]]:
    from kiro_crew.dashboard.tailnet_direct import _split_allowlist

    logins, tags = _split_allowlist(entries)
    return sorted(logins), sorted(tags)


def _invite_logins() -> set[str]:
    try:
        members = store.load_state().get("members") or {}
    except Exception:  # noqa: BLE001 -- an unreadable invite file adds nobody
        return set()
    return {
        _norm(str(m.get("login") or ""))
        for m in members.values()
        if isinstance(m, dict) and not m.get("removed")
    } - {""}


def install(
    seed: Optional[tuple[str, ...]],
    *,
    owner_login: str = "",
    owner_user_id: str = "",
    local_authorization: bool = True,
) -> AccessSnapshot:
    """Load (seeding once from *seed*), publish the set and list refused people. Blocking.

    *seed* is ``dashboard.tailscale.allowed_logins`` as read; ``None`` (the
    section could not be read) seeds nothing and leaves seeding for a later start.
    With *local_authorization* off the published set trusts the network: every
    verified person is admitted and nobody is listed as waiting.
    """
    global _STATE, _SEED_LIMIT
    with _LOCK:
        _STATE = None
        _OWNER.update(login=_norm(owner_login), user_id=owner_user_id)
        _MODE["local_authorization"] = bool(local_authorization)
        if seed is not None:
            limit_logins, limit_tags = _split_seed(seed)
            _SEED_LIMIT = (frozenset(limit_logins), frozenset(limit_tags))
        try:
            state = _state()
            if not state["seeded"] and seed is not None:
                logins, tags = _split_seed(seed)
                state["seed"] = {"logins": logins, "tags": tags}
                state["seeded"] = True
                _save(state)
            elif seed is not None:
                # The seed only shrinks: a login removed from allowed_logins is
                # removed here too, and one added later admits nobody.
                logins, tags = _split_seed(seed)
                kept = {
                    "logins": [lg for lg in state["seed"]["logins"] if lg in logins],
                    "tags": [t for t in state["seed"]["tags"] if t in tags],
                }
                if kept != state["seed"]:
                    state["seed"] = kept
                    _save(state)
        except _Unreadable:
            logger.warning("shared crew access set is unreadable; only the owner is admitted")
        except Exception:
            logger.warning("shared crew access set could not be seeded", exc_info=True)
        tailnet_live_allowlist.install_waiting_sink(note_waiting)
        try:
            return rebuild()
        except Exception:
            # Whatever broke, publish a set that admits only the owner rather
            # than none, which would hand the decision back to allowed_logins.
            logger.warning("shared crew access set failed to build; only the owner is admitted")
            snap = AccessSnapshot(
                owner_login=_OWNER["login"],
                owner_user_id=_OWNER["user_id"],
                unreadable=True,
                home=_home(),
                network_trust=not _MODE["local_authorization"],
            )
            tailnet_live_allowlist.install(snap)
            return snap


def install_placeholder(*, local_authorization: bool = True) -> AccessSnapshot:
    """Publish a set that admits nobody but the owner, reading nothing. Non-blocking.

    The gateway publishes this before it binds and runs :func:`install` once the
    dashboard answers requests, so the boot path never waits on the store and a
    request in between is refused rather than judged by ``allowed_logins``.
    """
    with _LOCK:
        _MODE["local_authorization"] = bool(local_authorization)
        snap = AccessSnapshot(
            owner_login=_OWNER["login"],
            owner_user_id=_OWNER["user_id"],
            unreadable=True,
            home=_home(),
            network_trust=not _MODE["local_authorization"],
        )
        tailnet_live_allowlist.install(snap)
        return snap


def drop_from_seed(login: str) -> None:
    """Take *login* out of the seed for good, so a restart does not admit it again. Blocking.

    Raises ``AccessError(CODE_UNREADABLE)`` when the set cannot be read or saved,
    so a caller never reports a revoke the next start would undo.
    """
    key = _norm(login)
    if not key:
        return
    with _LOCK:
        try:
            state = _state()
            if key in state["seed"]["logins"]:
                state["seed"]["logins"] = [lg for lg in state["seed"]["logins"] if lg != key]
                _save(state)
        except (_Unreadable, OSError) as exc:
            raise AccessError(CODE_UNREADABLE) from exc


def local_authorization() -> bool:
    """Whether Crew itself approves and removes people (else Tailscale alone decides)."""
    with _LOCK:
        return _MODE["local_authorization"]


def owner_login() -> str:
    """This machine's own Tailscale login, or ``""`` when not known."""
    with _LOCK:
        return _OWNER["login"]


def set_owner(login: str, user_id: str) -> AccessSnapshot:
    with _LOCK:
        _OWNER.update(login=_norm(login), user_id=user_id)
        return rebuild()


def rebuild() -> AccessSnapshot:
    """Recompute the set from its sources and publish it. Blocking."""
    with _LOCK:
        owner_login, owner_id = _OWNER["login"], _OWNER["user_id"]
        try:
            state = _state()
        except _Unreadable:
            snap = AccessSnapshot(
                owner_login=owner_login,
                owner_user_id=owner_id,
                unreadable=True,
                home=_home(),
                network_trust=not _MODE["local_authorization"],
            )
            tailnet_live_allowlist.install(snap)
            return snap
        approvals = [a for a in state["approvals"].values() if isinstance(a, dict)]
        seed_logins = frozenset(state["seed"]["logins"])
        seed_tags = frozenset(state["seed"]["tags"])
        if _SEED_LIMIT is not None:
            seed_logins &= _SEED_LIMIT[0]
            seed_tags &= _SEED_LIMIT[1]
        snap = AccessSnapshot(
            owner_login=owner_login,
            owner_user_id=owner_id,
            member_logins=frozenset(set(seed_logins) | _invite_logins()),
            approved_user_ids=frozenset(str(a.get("user_id") or "") for a in approvals) - {""},
            approved_logins=frozenset(_norm(str(a.get("login") or "")) for a in approvals) - {""},
            tags=seed_tags,
            home=_home(),
            network_trust=not _MODE["local_authorization"],
        )
        tailnet_live_allowlist.install(snap)
        return snap


def _audit(operation: str, outcome: str, actor: str, *, login: str = "", detail: str = "") -> None:
    try:
        from kiro_crew.sel import sel

        sel().log_api_access(
            caller=actor or "crew",
            operation=f"shared_crew.access.{operation}",
            outcome=outcome,
            source="shared-crew-access",
            resources=f"login_sha256:{store.email_hash(login)}" if login else "",
            error=detail,
        )
    except Exception:  # noqa: BLE001 -- the audit never breaks the action
        logger.debug("access audit write failed", exc_info=True)


# ── waiting ──


def note_waiting(peer: DirectPeer) -> bool:
    """List a refused, ``whois``-verified person for the owner. Blocking; never raises.

    Returns whether the person is on the list now. A dismissed person, a full
    minute's quota of new people, or an unreadable file leaves them unlisted.
    """
    user_id = peer.user_id
    login = _norm(peer.login)
    now = clock()
    with _LOCK:
        try:
            state = _state()
        except _Unreadable:
            return False
        waiting = state["waiting"]
        row = waiting.get(user_id)
        if row is not None:
            if now - _TOUCHED.get(user_id, 0.0) >= WAITING_REFRESH_SECS:
                _TOUCHED[user_id] = now
                row.update(last_seen=now, login=login, attempts=int(row.get("attempts") or 0) + 1)
                _save_quiet(state)
            return True
        until = state["dismissed"].get(user_id)
        if isinstance(until, (int, float)) and until > now:
            return False
        while _NEW_TIMES and now - _NEW_TIMES[0] > 60.0:
            _NEW_TIMES.popleft()
        if len(_NEW_TIMES) >= NEW_WAITING_PER_MINUTE:
            return False
        _NEW_TIMES.append(now)
        _TOUCHED[user_id] = now
        waiting[user_id] = {
            "user_id": user_id,
            "login": login,
            "display_name": store.clean_alias(peer.display_name, _DISPLAY_MAX) or login,
            "avatar_url": store.clean_alias(peer.profile_pic_url, 512),
            "node": store.clean_alias(peer.node, _DISPLAY_MAX),
            "first_seen": now,
            "last_seen": now,
            "attempts": 1,
        }
        if len(waiting) > MAX_WAITING:
            oldest = sorted(waiting, key=lambda k: float(waiting[k].get("first_seen") or 0.0))
            for key in oldest[: len(waiting) - MAX_WAITING]:
                del waiting[key]
        # The refresh stamps cover only people still listed: an evicted, approved
        # or dismissed person's stamp goes with their row.
        for key in [k for k in _TOUCHED if k not in waiting]:
            del _TOUCHED[key]
        _save_quiet(state)
    _audit("waiting", "listed", "crew", login=login)
    return user_id in waiting


def _save_quiet(state: dict[str, Any]) -> None:
    try:
        _save(state)
    except OSError:
        logger.warning("saving the shared crew waiting list failed", exc_info=True)


# ── first share ──


def nobody_else_has_access() -> bool:
    snap = tailnet_live_allowlist.snapshot()
    return snap is None or snap.others == 0


def first_share_acknowledged() -> bool:
    with _LOCK:
        try:
            return bool(_state().get("first_share_ack"))
        except _Unreadable:
            return False


def require_first_share_ack() -> None:
    """Refuse the first invite or approve until the owner saw the warning. Blocking."""
    if nobody_else_has_access() and not first_share_acknowledged():
        raise AccessError(CODE_ACK_REQUIRED)


def acknowledge_first_share(actor: str) -> None:
    with _LOCK:
        try:
            state = _state()
        except _Unreadable as exc:
            raise AccessError(CODE_UNREADABLE) from exc
        if not state.get("first_share_ack"):
            state["first_share_ack"] = {"at": clock(), "by": actor}
            _save(state)
    _audit("first_share_ack", "granted", actor)


# ── owner actions ──


def approve(user_id: str, actor: str) -> dict[str, Any]:
    """Admit a waiting person by stable user id. Blocking."""
    require_first_share_ack()
    with _LOCK:
        try:
            state = _state()
        except _Unreadable as exc:
            raise AccessError(CODE_UNREADABLE) from exc
        row = state["waiting"].pop(user_id, None)
        if not isinstance(row, dict):
            raise AccessError(CODE_NOT_WAITING)
        login = str(row.get("login") or "")
        state["approvals"][user_id] = {
            "user_id": user_id,
            "login": login,
            "display_name": row.get("display_name") or login,
            "avatar_url": row.get("avatar_url") or "",
            "first_seen": row.get("first_seen") or 0.0,
            "approved_at": clock(),
            "approved_by": actor,
        }
        state["dismissed"].pop(user_id, None)
        _save(state)
        tailnet_live_allowlist.restore_user_id(user_id)
        tailnet_live_allowlist.clear_revoke(login)
        rebuild()
    restore(login, frozenset({user_id}))
    _audit("approve", "granted", actor, login=login)
    return state["approvals"][user_id]


def dismiss(user_id: str, actor: str) -> None:
    """Drop a waiting person from the list; they are not listed again for a day. Blocking."""
    with _LOCK:
        try:
            state = _state()
        except _Unreadable as exc:
            raise AccessError(CODE_UNREADABLE) from exc
        row = state["waiting"].pop(user_id, None)
        if not isinstance(row, dict):
            raise AccessError(CODE_NOT_WAITING)
        dismissed = state["dismissed"]
        dismissed[user_id] = clock() + DISMISS_SECS
        if len(dismissed) > MAX_DISMISSED:
            for key in sorted(dismissed, key=lambda k: float(dismissed[k]))[
                : len(dismissed) - MAX_DISMISSED
            ]:
                del dismissed[key]
        _save(state)
    _audit("dismiss", "granted", actor, login=str(row.get("login") or ""))


def withdraw_approval(user_id: str) -> None:
    """Drop *user_id*'s approval, if any, and revoke the id live. Blocking.

    For an invite revoke, which removes the person whatever let them in. Raises
    :class:`AccessError` when the store cannot be read or written, before
    anything is revoked, so the caller's revoke fails and can be retried.
    """
    if not user_id:
        return
    with _LOCK:
        try:
            state = _state()
        except _Unreadable as exc:
            raise AccessError(CODE_UNREADABLE) from exc
        if state["approvals"].pop(user_id, None) is not None:
            _save(state)
    tailnet_live_allowlist.revoke_user_id(user_id)


async def remove_approval(user_id: str, actor: str) -> dict[str, Any]:
    """Take an approved person out of the set and stop everything they have running."""

    def _approval(*, drop: bool) -> dict[str, Any]:
        with _LOCK:
            try:
                state = _state()
            except _Unreadable as exc:
                raise AccessError(CODE_UNREADABLE) from exc
            approvals = state["approvals"]
            row = approvals.pop(user_id, None) if drop else approvals.get(user_id)
            if not isinstance(row, dict):
                raise AccessError(CODE_NOT_APPROVED)
            if drop:
                _save(state)
                tailnet_live_allowlist.revoke_user_id(user_id)
                tailnet_live_allowlist.revoke(str(row.get("login") or ""))
            return row

    from kiro_crew.shared_crew.tailscale_invites import _commit_lock

    # The invite accept commit holds this lock while it writes a member row, so
    # a removal never lands between its read and its write and is undone.
    async with _commit_lock():
        row = await asyncio.to_thread(lambda: _approval(drop=False))
        login = _norm(str(row.get("login") or ""))

        member_logins: set[str] = set()

        def _mark(s: dict[str, Any]) -> None:
            # An invite they accepted would let them in again after a restart.
            for m in s["members"].values():
                if m.get("user_id") == user_id or (
                    login and _norm(str(m.get("login") or "")) == login
                ):
                    m["removed"] = True
                    member_logins.add(str(m.get("login") or ""))

        # The member rows are marked before the approval is deleted: a failed
        # write leaves the approval in place, so the owner can retry the removal.
        await asyncio.to_thread(store.update_state, _mark)
        row = await asyncio.to_thread(lambda: _approval(drop=True))
        # Every login they hold a live grant under: the member rows' and any the
        # daemon named for this user id (a renamed login).
        for other in member_logins | tailnet_live_allowlist.logins_for(user_id):
            tailnet_live_allowlist.revoke(other)
        try:
            await asyncio.to_thread(rebuild)
        except Exception:  # noqa: BLE001 -- the removal is committed; their work still stops
            logger.warning("access set rebuild after a removal failed", exc_info=True)
        # Still under the lock: an approval racing this removal waits for the
        # fence and the work stop, so it is never undone by them.
        result = await stop_access(str(row.get("login") or ""), frozenset({user_id}), actor=actor)
    return {"removed": user_id, **result}


_REMOVED: dict[str, float] = {}
_REMOVED_MAX = 512


def _note_removed(ids: frozenset[str]) -> None:
    with _LOCK:
        for pid in ids:
            _REMOVED[pid] = clock()
        while len(_REMOVED) > _REMOVED_MAX:
            _REMOVED.pop(next(iter(_REMOVED)))


def restore(login: str, user_ids: frozenset[str] = frozenset()) -> None:
    """A removed person was let in again (approved, or accepted a new invite)."""
    ids = principal_ids_for(login, user_ids)
    with _LOCK:
        for pid in ids:
            _REMOVED.pop(pid, None)


def principal_removed(principal_id: str) -> bool:
    """Whether work charged to *principal_id* must not start: its person was removed."""
    with _LOCK:
        return bool(principal_id) and principal_id in _REMOVED


def principal_ids_for(login: str, user_ids: frozenset[str]) -> frozenset[str]:
    """Every principal id a person may have worked under.

    A tailnet person is keyed by login on every transport. *user_ids* add every
    login the daemon named for them since start (a renamed login keeps its old
    work under the old id and charges new work to the new one).
    """
    from kiro_crew.shared_crew.contracts import Provenance
    from kiro_crew.shared_crew.identity import tailnet_principal

    logins = {login} if login else set()
    for uid in user_ids:
        logins |= tailnet_live_allowlist.logins_for(uid)
    return frozenset(tailnet_principal(lg, "", Provenance.TAILSCALE_SERVE).id for lg in logins)


async def stop_work(login: str, user_ids: frozenset[str], *, actor: str) -> dict[str, int]:
    """Cancel a removed person's queued work and stop their running turn now.

    Call after the person is out of the set, so a request racing this one is
    already refused. Admitted work is cancelled through the admission service,
    which frees its capacity once each runtime has stopped. Work no admitted
    record covers (fair admission off, the default) is stopped through
    :data:`slot_work_stopper`: queued follow-ups they sent are dropped and a
    turn they started is stopped. Work charged to them that still reaches the
    front of a queue is refused there.
    """
    from kiro_crew.shared_crew.admission_gate import installed_service
    from kiro_crew.shared_crew.admission_revoke import stop_principals

    ids = principal_ids_for(login, user_ids)
    _note_removed(ids)
    stopped = {"queued": 0, "running": 0}
    service = installed_service()
    if service is not None:
        try:
            stopped = await stop_principals(service, ids)
        except Exception:  # noqa: BLE001 -- reported, never raised past the removal
            logger.warning("stopping a removed person's work failed", exc_info=True)
    try:
        unadmitted = await slot_work_stopper(ids)
        stopped = {
            "queued": stopped["queued"] + int(unadmitted.get("queued") or 0),
            "running": stopped["running"] + int(unadmitted.get("running") or 0),
        }
    except Exception:  # noqa: BLE001 -- reported, never raised past the removal
        logger.warning("stopping a removed person's unadmitted work failed", exc_info=True)
    await asyncio.to_thread(
        _audit,
        "stop_work",
        "granted",
        actor,
        login=login,
        detail=f"{OUTCOME_ACCESS_REMOVED}; queued:{stopped['queued']} running:{stopped['running']}",
    )
    return {"cancelled_queued": stopped["queued"], "stopped_running": stopped["running"]}


async def stop_access(login: str, user_ids: frozenset[str], *, actor: str) -> dict[str, int]:
    """Close a removed person's dashboard sockets, then :func:`stop_work`."""
    ids = principal_ids_for(login, user_ids)
    closed = 0
    try:
        closed = int(await socket_closer(ids, login) or 0)
    except Exception:  # noqa: BLE001 -- the refusal already holds
        logger.warning("closing a removed person's sockets failed", exc_info=True)
    stopped = await stop_work(login, user_ids, actor=actor)
    return {"closed_connections": closed, **stopped}


# ── read ──


def view() -> dict[str, Any]:
    """The People access body: waiting, approved, seeded entries and the first-share state."""
    with _LOCK:
        try:
            state = _state()
        except _Unreadable:
            return {
                "readable": False,
                "waiting": [],
                "approved": [],
                "seeded": [],
                "first_share_ack": False,
            }
        waiting = sorted(
            (dict(r) for r in state["waiting"].values() if isinstance(r, dict)),
            key=lambda r: -float(r.get("first_seen") or 0.0),
        )
        approved = sorted(
            (dict(r) for r in state["approvals"].values() if isinstance(r, dict)),
            key=lambda r: -float(r.get("approved_at") or 0.0),
        )
        return {
            "readable": True,
            "waiting": waiting,
            "approved": approved,
            "seeded": list(state["seed"]["logins"]) + list(state["seed"]["tags"]),
            "first_share_ack": bool(state.get("first_share_ack")),
        }


def reset_for_tests() -> None:
    global _STATE, _SEED_LIMIT, socket_closer, slot_work_stopper, clock
    with _LOCK:
        _STATE = None
        _SEED_LIMIT = None
        _OWNER.update(login="", user_id="")
        _MODE["local_authorization"] = True
        _NEW_TIMES.clear()
        _TOUCHED.clear()
        _REMOVED.clear()
    socket_closer = _no_socket_closer
    slot_work_stopper = _no_slot_work_stopper
    clock = time.time


__all__ = [
    "AccessError",
    "acknowledge_first_share",
    "approve",
    "dismiss",
    "install",
    "local_authorization",
    "note_waiting",
    "owner_login",
    "principal_ids_for",
    "principal_removed",
    "rebuild",
    "remove_approval",
    "require_first_share_ack",
    "set_owner",
    "stop_access",
    "stop_work",
    "view",
    "withdraw_approval",
]
