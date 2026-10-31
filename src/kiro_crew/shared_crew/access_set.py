"""Who Tailscale lets reach this Crew, as Shared Crew derives it: computed, never typed.

Tailscale is the only access authority: the tailnet ACL and device sharing
decide who can reach this machine, and Crew approves nobody itself. On the
direct tailnet listener every person ``whois`` names is admitted (named by
Tailscale), and a tagged node only by a tag carried over below. This module
derives the set the ``tailscale serve`` path checks logins against:

* the owner (this machine's own Tailscale user);
* each person who accepted a Crew Tailscale invite, by the login Tailscale's
  API reported for them (``tailscale_invites``);
* once, at the first start, the logins and ``tag:`` entries already in
  ``dashboard.tailscale.allowed_logins`` (the migration seed). After that a hand
  edit of the config list admits nobody, and removing an entry removes it.

The state file lives in ``<data_home>/.vault/shared-crew-tailscale/`` beside the
invite records, a directory every sandbox mode masks. A file that cannot be read
admits nobody but the owner. The computed set is published to
:mod:`kiro_crew.dashboard.tailnet_live_allowlist`, which the auth middleware
reads.
"""

from __future__ import annotations

import json
import logging
import threading
import time
from typing import Any, Callable, Optional

from kiro_crew.atomic_write import atomic_write
from kiro_crew.dashboard import tailnet_live_allowlist
from kiro_crew.dashboard.tailnet_live_allowlist import AccessSnapshot, _norm
from kiro_crew.shared_crew import tailscale_invite_store as store

logger = logging.getLogger(__name__)

ACCESS_FILE = "access.json"
SCHEMA = 1

CODE_ACK_REQUIRED = "first_share_ack_required"
CODE_UNREADABLE = "access_unreadable"


class AccessError(Exception):
    def __init__(self, code: str) -> None:
        super().__init__(code)
        self.code = code


class _Unreadable(Exception):
    pass


_LOCK = threading.RLock()
_STATE: Optional[dict[str, Any]] = None
_OWNER: dict[str, str] = {"login": "", "user_id": ""}
#: Principals whose work must not start. Nothing in this build removes a person
#: (Tailscale does), so it stays empty; admission and the turn gate read it.
_REMOVED: set[str] = set()

#: Seams the tests replace.
clock: Callable[[], float] = time.time


def _empty() -> dict[str, Any]:
    return {
        "schema": SCHEMA,
        "seeded": False,
        "seed": {"logins": [], "tags": []},
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


def _owner_only(owner_login: str, owner_id: str) -> AccessSnapshot:
    return AccessSnapshot(
        owner_login=owner_login, owner_user_id=owner_id, unreadable=True, home=_home()
    )


def install(
    seed: Optional[tuple[str, ...]], *, owner_login: str = "", owner_user_id: str = ""
) -> AccessSnapshot:
    """Load (seeding once from *seed*) and publish the set. Blocking.

    *seed* is ``dashboard.tailscale.allowed_logins`` as read; ``None`` (the
    section could not be read) seeds nothing and leaves seeding for a later start.
    """
    global _STATE, _SEED_LIMIT
    with _LOCK:
        _STATE = None
        _OWNER.update(login=_norm(owner_login), user_id=owner_user_id)
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
        try:
            return rebuild()
        except Exception:
            # Whatever broke, publish a set that admits only the owner rather
            # than none, which would hand the decision back to allowed_logins.
            logger.warning("shared crew access set failed to build; only the owner is admitted")
            snap = _owner_only(_OWNER["login"], _OWNER["user_id"])
            tailnet_live_allowlist.install(snap)
            return snap


def install_placeholder() -> AccessSnapshot:
    """Publish a set that admits nobody but the owner, reading nothing. Non-blocking.

    The gateway publishes this before it binds and runs :func:`install` once the
    dashboard answers requests, so the boot path never waits on the store and a
    request in between is refused rather than judged by ``allowed_logins``.
    """
    with _LOCK:
        snap = _owner_only(_OWNER["login"], _OWNER["user_id"])
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
            snap = _owner_only(owner_login, owner_id)
            tailnet_live_allowlist.install(snap)
            return snap
        seed_logins = frozenset(state["seed"]["logins"])
        seed_tags = frozenset(state["seed"]["tags"])
        if _SEED_LIMIT is not None:
            seed_logins &= _SEED_LIMIT[0]
            seed_tags &= _SEED_LIMIT[1]
        snap = AccessSnapshot(
            owner_login=owner_login,
            owner_user_id=owner_id,
            member_logins=frozenset(set(seed_logins) | _invite_logins()),
            tags=seed_tags,
            home=_home(),
        )
        tailnet_live_allowlist.install(snap)
        return snap


def _audit(operation: str, outcome: str, actor: str) -> None:
    try:
        from kiro_crew.sel import sel

        sel().log_api_access(
            caller=actor or "crew",
            operation=f"shared_crew.access.{operation}",
            outcome=outcome,
            source="shared-crew-access",
            resources="",
        )
    except Exception:  # noqa: BLE001 -- the audit never breaks the action
        logger.debug("access audit write failed", exc_info=True)


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
    """Refuse the first invite until the owner saw the warning. Blocking."""
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


# ── principals ──


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


# ── read ──


def view() -> dict[str, Any]:
    """The People access body: the seeded entries and the first-share state."""
    with _LOCK:
        try:
            state = _state()
        except _Unreadable:
            return {"readable": False, "seeded": [], "first_share_ack": False}
        return {
            "readable": True,
            "seeded": list(state["seed"]["logins"]) + list(state["seed"]["tags"]),
            "first_share_ack": bool(state.get("first_share_ack")),
        }


def reset_for_tests() -> None:
    global _STATE, _SEED_LIMIT, clock
    with _LOCK:
        _STATE = None
        _SEED_LIMIT = None
        _OWNER.update(login="", user_id="")
        _REMOVED.clear()
    clock = time.time


__all__ = [
    "AccessError",
    "acknowledge_first_share",
    "drop_from_seed",
    "install",
    "install_placeholder",
    "owner_login",
    "principal_ids_for",
    "principal_removed",
    "rebuild",
    "require_first_share_ack",
    "set_owner",
    "view",
]
