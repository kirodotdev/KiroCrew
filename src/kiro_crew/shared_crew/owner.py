"""Who the Crew owner is, and the name shown for them.

The owner is the person at this machine. Their principal ids never change:
every local token subject they hold (:data:`LOCAL_BOOTSTRAP_SUBJECTS` and the
configured owner id) and every tailnet id of the owner's Tailscale login
(:func:`owner_principal_ids`) name the owner. The one name shown for the owner
is :func:`owner_name`: the signed-in Kiro account's e-mail, else the owner's
Tailscale name, else this machine's name (:func:`machine_name`). It labels the
owner's local principal and the mode route's ``owner_label``, so every surface
names the owner the same way.

Only a label depends on the name. No access or budget decision reads it.
"""

from __future__ import annotations

import asyncio
import logging
import socket
import subprocess
import threading
from collections.abc import Callable
from pathlib import PurePosixPath
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from kiro_crew.shared_crew.contracts import Principal

logger = logging.getLogger(__name__)

#: Token subjects of the gateway's machine-local bootstrap tokens, minted as
#: ``owner_id or <subject>``: the one local operator when no owner is configured.
LOCAL_BOOTSTRAP_SUBJECTS: tuple[str, ...] = ("local-app", "local-startup")

#: The subject the owner's principal is built from when no owner id is configured.
DEFAULT_OWNER_SUBJECT = "local-app"

#: The macOS computer-name tool, called by absolute path so a ``PATH`` entry the
#: gateway user can write cannot answer for it.
SCUTIL_PATH = str(PurePosixPath("/", "usr", "sbin", "scutil"))
_SCUTIL_TIMEOUT_SECS = 2.0
_NAME_MAX = 128

_LOCK = threading.Lock()
_owner_subject = ""
_tailnet_host_name = ""
#: The Tailscale ``DisplayName`` of the user who owns this machine's node.
_tailnet_display_name = ""
#: ``None`` until the macOS lookup ran once; ``""`` when it ran and found nothing.
_computer_name: str | None = None
#: The e-mail of the Kiro account this machine is signed in to. ``None`` until a
#: source answered (the start-up identity read or a usage reading); ``""`` when
#: one answered with no e-mail.
_login_email: str | None = None


def set_owner_subject(owner_id: str) -> None:
    """Record the configured owner id (gateway start). Empty falls back to the default."""
    global _owner_subject
    with _LOCK:
        _owner_subject = owner_id.strip()


def owner_subject() -> str:
    """The token subject the owner's principal is built from."""
    with _LOCK:
        return _owner_subject or DEFAULT_OWNER_SUBJECT


def set_tailnet_host_name(name: str) -> None:
    """Record the host name the local Tailscale daemon reports for this machine."""
    global _tailnet_host_name
    with _LOCK:
        _tailnet_host_name = _clean(name)


def set_tailnet_display_name(name: str) -> None:
    """Record the Tailscale display name of the user who owns this machine's node."""
    global _tailnet_display_name
    with _LOCK:
        _tailnet_display_name = _clean(name)


def _email_or_empty(raw: object) -> str:
    from kiro_crew.relay_client.protocol import is_display_email

    value = raw.strip() if isinstance(raw, str) else ""
    return value if is_display_email(value) else ""


def set_login_email(email: object) -> None:
    """Record the signed-in Kiro account's e-mail; anything that is not one records ``""``.

    Called with every usage reading the dashboard publishes, which names the
    account signed in now (``sessions._publish_usage``), so a sign-in change
    moves the label with the next reading and no request spawns ``kiro-cli``.
    """
    global _login_email
    with _LOCK:
        _login_email = _email_or_empty(email)


def login_email() -> str:
    """The cached signed-in Kiro account e-mail, or ``""``. Never reads anything."""
    with _LOCK:
        return _login_email or ""


async def warm_login_email() -> None:
    """Fill the e-mail from the signed-in identity once, at gateway start. Never raises.

    ``fetch_local_identity`` is the dashboard's one door to the signed-in
    identity: ``kiro-cli whoami`` on the kiro-cli route, the vault on Crew's own
    sign-in, which stores no e-mail (there the first usage reading brings it).
    A usage reading that landed first is newer, so it is kept.
    """
    global _login_email
    from kiro_crew.dashboard.handlers.sessions import fetch_local_identity

    try:
        identity = await fetch_local_identity()
    except Exception:  # noqa: BLE001 -- a label source; the next fallback answers
        logger.debug("shared crew: owner identity read failed", exc_info=True)
        return
    if identity is None:
        return
    email = _email_or_empty(identity.get("email"))
    with _LOCK:
        if _login_email is None:
            _login_email = email


def _clean(raw: object) -> str:
    if not isinstance(raw, str):
        return ""
    return "".join(ch for ch in raw.strip() if ch.isprintable())[:_NAME_MAX]


def _read_computer_name(run: Callable[..., Any] = subprocess.run) -> str:
    try:
        done = run(
            [SCUTIL_PATH, "--get", "ComputerName"],
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            timeout=_SCUTIL_TIMEOUT_SECS,
            check=False,
        )
    except (OSError, subprocess.SubprocessError):
        return ""
    if getattr(done, "returncode", 1) != 0:
        return ""
    return _clean(getattr(done, "stdout", ""))


def _on_event_loop() -> bool:
    """Whether this thread runs an asyncio loop, where a subprocess must not block."""
    try:
        asyncio.get_running_loop()
    except RuntimeError:
        return False
    return True


def machine_name(*, token_subject: str = "") -> str:
    """This machine's name, for the owner's label.

    macOS ``ComputerName`` (read once, never on an event-loop thread: there the
    lookup is left to :func:`warm_machine_name` and the fallbacks answer until it
    lands), else the Tailscale daemon's host name for this node, else the OS host
    name, else *token_subject*. Never raises.
    """
    global _computer_name
    from kiro_crew.platform_compat import IS_MACOS

    if IS_MACOS:
        with _LOCK:
            cached = _computer_name
        if cached is None and not _on_event_loop():
            cached = _read_computer_name()
            with _LOCK:
                _computer_name = cached
        if cached:
            return cached
    with _LOCK:
        tailnet = _tailnet_host_name
    if tailnet:
        return tailnet
    try:
        host = _clean(socket.gethostname())
    except OSError:
        host = ""
    return host or _clean(token_subject)


def warm_machine_name() -> threading.Thread:
    """Fill the ``ComputerName`` cache on a daemon thread (gateway start).

    The lookup spawns ``scutil`` and may take up to ``_SCUTIL_TIMEOUT_SECS``; run
    here, the first request that labels the owner reads the cache instead of
    waiting on that process on the event loop.
    """
    thread = threading.Thread(target=machine_name, name="shared-crew-owner-name", daemon=True)
    thread.start()
    return thread


def owner_name(*, token_subject: str = "") -> str:
    """The one name shown for the owner, everywhere. Cache reads only; never raises.

    First non-empty wins:

    1. A name the Kiro relay or control plane gives the owner. None does: the
       relay names only the caller of a stream (``userName``/``userEmail``) and
       the lease names the Crew, not its owner, so this step has no source yet.
    2. The e-mail of the Kiro account this machine is signed in to
       (:func:`login_email`).
    3. The owner's Tailscale display name, else their Tailscale login.
    4. This machine's name (:func:`machine_name`).
    """
    import kiro_crew.shared_crew.access_set as access_set

    email = login_email()
    if email:
        return email
    with _LOCK:
        tailnet_name = _tailnet_display_name
    return (
        tailnet_name
        or _clean(access_set.owner_login())
        or machine_name(token_subject=token_subject)
    )


def is_owner_subject(subject: str) -> bool:
    """Whether *subject* is a local token subject of the owner."""
    value = subject.strip()
    return bool(value) and (value in LOCAL_BOOTSTRAP_SUBJECTS or value == owner_subject())


def owner_principal() -> Principal:
    """The owner's local principal, labelled with :func:`owner_name`."""
    from kiro_crew.shared_crew.identity import local_operator_principal

    return local_operator_principal(owner_subject())


def owner_principal_ids(owner_id: str = "") -> frozenset[str]:
    """Every principal id the owner writes under, local token or tailnet login.

    *owner_id* is a token subject of the owner that the caller already knows;
    the configured owner subject and the bootstrap subjects are always included.
    """
    import kiro_crew.shared_crew.access_set as access_set

    return frozenset(
        local_owner_principal_ids(owner_id)
        | access_set.principal_ids_for(access_set.owner_login(), frozenset())
    )


def local_owner_principal_ids(owner_id: str = "") -> frozenset[str]:
    """The owner's principal ids for the local dashboard tokens only (no tailnet login)."""
    from kiro_crew.shared_crew.identity import local_operator_principal

    subjects = set(LOCAL_BOOTSTRAP_SUBJECTS) | {owner_subject()}
    if owner_id.strip():
        subjects.add(owner_id.strip())
    return frozenset(local_operator_principal(s).id for s in subjects)


def is_owner_principal(principal: Principal, owner_id: str = "") -> bool:
    """Whether *principal* is the owner, by id. Unknown and system principals never are."""
    from kiro_crew.shared_crew.contracts import PrincipalKind

    if principal.kind in (PrincipalKind.UNKNOWN, PrincipalKind.SYSTEM):
        return False
    return principal.id in owner_principal_ids(owner_id)


def writes_as_ids(caller: Principal, owner_id: str = "") -> frozenset[str]:
    """Every principal id *caller* writes as: the one answer to "is this row theirs?".

    The owner is one person under every id in :func:`owner_principal_ids`; any
    other caller, an Unknown or app (system) one included, has only their own
    id, so an Unknown caller never speaks for the owner. The edit rule
    (``edit_rule``), the mode route's ``viewer_ids`` and the session list's
    ``mine`` all read it.
    """
    if is_owner_principal(caller, owner_id):
        return owner_principal_ids(owner_id) | {caller.id}
    return frozenset({caller.id})


def reset_for_tests() -> None:
    global _owner_subject, _tailnet_host_name, _tailnet_display_name, _computer_name
    global _login_email
    with _LOCK:
        _owner_subject = ""
        _tailnet_host_name = ""
        _tailnet_display_name = ""
        _computer_name = None
        _login_email = None


__all__ = [
    "DEFAULT_OWNER_SUBJECT",
    "LOCAL_BOOTSTRAP_SUBJECTS",
    "SCUTIL_PATH",
    "is_owner_principal",
    "is_owner_subject",
    "login_email",
    "machine_name",
    "owner_name",
    "owner_principal",
    "owner_principal_ids",
    "owner_subject",
    "reset_for_tests",
    "set_login_email",
    "set_owner_subject",
    "set_tailnet_display_name",
    "set_tailnet_host_name",
    "warm_login_email",
    "writes_as_ids",
]
