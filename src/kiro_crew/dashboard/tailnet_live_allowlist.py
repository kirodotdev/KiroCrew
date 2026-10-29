"""Who may reach this Crew over Tailscale, as the running gateway knows it now.

Two layers, read by :func:`kiro_crew.dashboard.tailnet.login_allowed` and the
direct tailnet listener (:mod:`kiro_crew.dashboard.tailnet_direct`):

* **The derived access set** (:class:`AccessSnapshot`). Shared Crew computes it
  from the owner, the people who accepted a Crew Tailscale invite and the people
  the owner approved, and installs it with :func:`install` at gateway start and
  after every change (``kiro_crew.shared_crew.access_set``). While one is
  installed it is the only authority: ``dashboard.tailscale.allowed_logins`` in
  ``config.json`` was read once as a migration seed and a later hand edit of it
  admits nobody.
* **The live overlay** (:func:`grant` / :func:`revoke`). A login an invite
  accept or revoke just changed, applied at once. A revoked login is refused
  even when the set still names it, so a removal holds from the next request.

With no set installed (a process that never started the gateway, and every
test that does not install one) the overlay sits on top of the caller's
``allowed_logins``, exactly as before.

Only the gateway's own owner-gated routes write here. Nothing here turns
identity trust on: with ``trust_identity`` off the ``tailscale serve`` path
checks no forwarded login at all.
"""

from __future__ import annotations

import threading
from dataclasses import dataclass, field
from typing import Any, Callable, Optional

_LOCK = threading.Lock()
_GRANTED: set[str] = set()
_REVOKED: set[str] = set()
_REVOKED_IDS: set[str] = set()
#: Every login the daemon's ``whois`` named for a stable user id since start, so
#: a removal reaches work charged under a login the person renamed away from.
_LOGINS_BY_USER: dict[str, set[str]] = {}
_LOGINS_BY_USER_MAX = 1024
_LOGINS_PER_USER_MAX = 8
_SNAPSHOT: Optional["AccessSnapshot"] = None

#: Called with a refused, daemon-verified person; returns whether they were
#: listed as waiting for the owner's approval. Installed by Shared Crew.
WaitingSink = Callable[[Any], bool]
_WAITING_SINK: Optional[WaitingSink] = None


def _norm(login: str) -> str:
    return login.strip().lower()


@dataclass(frozen=True)
class AccessSnapshot:
    """The derived access set: owner, invited and approved people. Never typed by hand."""

    owner_login: str = ""
    owner_user_id: str = ""
    #: Logins admitted by login: accepted invites and the migration seed.
    member_logins: frozenset[str] = field(default_factory=frozenset)
    #: Stable Tailscale user ids the owner approved.
    approved_user_ids: frozenset[str] = field(default_factory=frozenset)
    #: The login each approval was made under, for paths that see only a login.
    approved_logins: frozenset[str] = field(default_factory=frozenset)
    #: ``tag:<name>`` entries carried over from the migration seed.
    tags: frozenset[str] = field(default_factory=frozenset)
    #: The set could not be read: nobody but the owner is admitted.
    unreadable: bool = False
    #: The data home the set was computed for; another home ignores it.
    home: str = ""
    #: ``dashboard.shared_crew.local_authorization`` is off: Tailscale alone
    #: decides, so every verified person is admitted. Tagged nodes still are not.
    network_trust: bool = False

    @property
    def others(self) -> int:
        """How many people besides the owner the set admits."""
        owner = _norm(self.owner_login)
        logins = {lg for lg in self.member_logins | self.approved_logins if lg != owner}
        return len(logins) + len(self.tags)

    def admits_login(self, login: str) -> bool:
        """For a path that knows only a login (``tailscale serve``, mobile setup)."""
        key = _norm(login)
        if not key or self.unreadable:
            return bool(key) and key == _norm(self.owner_login)
        return (
            key == _norm(self.owner_login)
            or key in self.member_logins
            or key in self.approved_logins
        )

    def admits_person(self, user_id: str, login: str) -> bool:
        """For the direct listener, which has the daemon's stable user id."""
        if self.unreadable:
            return False
        if user_id and user_id in self.approved_user_ids:
            return True
        return _norm(login) in self.member_logins


def install(snapshot: Optional[AccessSnapshot]) -> None:
    """Make *snapshot* the access set (``None`` returns to the config allowlist)."""
    global _SNAPSHOT
    with _LOCK:
        _SNAPSHOT = snapshot


def _current_home() -> str:
    from kiro_crew.config.paths import config_dir

    return str(config_dir())


def snapshot() -> Optional[AccessSnapshot]:
    """The installed set, or ``None`` when none is installed for this data home."""
    with _LOCK:
        snap = _SNAPSHOT
    if snap is None or (snap.home and snap.home != _current_home()):
        return None
    return snap


def grant(login: str) -> None:
    key = _norm(login)
    if not key:
        return
    with _LOCK:
        _REVOKED.discard(key)
        _GRANTED.add(key)


def revoke(login: str) -> None:
    key = _norm(login)
    if not key:
        return
    with _LOCK:
        _GRANTED.discard(key)
        _REVOKED.add(key)


def clear_revoke(login: str) -> None:
    """Lift a live revoke of *login* without granting it (an approval admits by user id)."""
    key = _norm(login)
    with _LOCK:
        _REVOKED.discard(key)


def revoke_user_id(user_id: str) -> None:
    """Refuse one stable user id from the next request, whatever its login."""
    if not user_id:
        return
    with _LOCK:
        _REVOKED_IDS.add(user_id)


def restore_user_id(user_id: str) -> None:
    with _LOCK:
        _REVOKED_IDS.discard(user_id)


def user_id_revoked(user_id: str) -> bool:
    with _LOCK:
        return bool(user_id) and user_id in _REVOKED_IDS


def granted(login: str) -> bool:
    """Whether the live overlay granted *login* (an invite accept), and nothing revoked it."""
    key = _norm(login)
    with _LOCK:
        return bool(key) and key in _GRANTED and key not in _REVOKED


def decision(login: str) -> bool | None:
    """``True`` admitted, ``False`` refused, ``None`` when neither layer decides."""
    # ``network_trust`` is not read here: it widens only the direct tailnet
    # listener (``tailnet_direct.admission_refusal``). The ``tailscale serve``
    # path keeps the owner, accepted invites, approvals and the seeded list.
    key = _norm(login)
    snap = snapshot()
    with _LOCK:
        if key in _REVOKED:
            return False
        if key in _GRANTED:
            return True
    if snap is not None:
        return snap.admits_login(key)
    return None


def install_waiting_sink(sink: Optional[WaitingSink]) -> None:
    global _WAITING_SINK
    with _LOCK:
        _WAITING_SINK = sink


def note_waiting(peer: object) -> bool:
    """Offer a refused, verified person to the waiting list. Never raises."""
    with _LOCK:
        sink = _WAITING_SINK
    if sink is None:
        return False
    try:
        return bool(sink(peer))
    except Exception:  # noqa: BLE001 -- a waiting list that fails still refuses
        return False


def note_login(user_id: str, login: str) -> None:
    """Record that the daemon named *login* for *user_id*. Bounded; never raises."""
    key = _norm(login)
    if not user_id or not key:
        return
    with _LOCK:
        seen = _LOGINS_BY_USER.get(user_id)
        if seen is None:
            if len(_LOGINS_BY_USER) >= _LOGINS_BY_USER_MAX:
                _LOGINS_BY_USER.pop(next(iter(_LOGINS_BY_USER)))
            seen = _LOGINS_BY_USER[user_id] = set()
        if key in seen or len(seen) < _LOGINS_PER_USER_MAX:
            seen.add(key)


def logins_for(user_id: str) -> frozenset[str]:
    """Every login :func:`note_login` recorded for *user_id*."""
    with _LOCK:
        return frozenset(_LOGINS_BY_USER.get(user_id, ()))


def seen_logins() -> frozenset[str]:
    """Every login :func:`note_login` recorded, for any user id."""
    with _LOCK:
        return frozenset(lg for logins in _LOGINS_BY_USER.values() for lg in logins)


def reset_for_tests() -> None:
    global _SNAPSHOT, _WAITING_SINK
    with _LOCK:
        _GRANTED.clear()
        _REVOKED.clear()
        _REVOKED_IDS.clear()
        _LOGINS_BY_USER.clear()
        _SNAPSHOT = None
        _WAITING_SINK = None
