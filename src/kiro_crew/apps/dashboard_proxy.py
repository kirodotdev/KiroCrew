"""The ``dashboard_proxy`` capability: an app backend that mints a scoped owner token.

An app backend is third-party code, so it reads none of the gateway's secret files
(``sandbox.app_backend_secret_mask_targets``). An app the owner granted this capability
reads, over each of those paths, one value only it holds:

    D(app) = hex HMAC-SHA256(local secret,
                             b"dashboard-proxy\\0" + app + b"\\0" + install id + b"\\0" + boot id)

``/api/token/local`` takes ``D(app)`` as its second leg, from that app's own gateway-spawned
backend only (``member_memory_auth.delegated_app_provenance``), and mints an owner token
that carries ``via_app=<app>`` and is scoped by ``dashboard.delegated_scope``. The install
id is the install provenance of the current install (see :func:`install_identity`), so a
reinstall, or another package under the same name, gets a new value; the boot id ends every
value at a gateway restart.

The grant is the owner's. It lives in ``member-records/app-dashboard-proxy/<app>.json``,
which the OS sandbox masks in every mode and the agent file tools refuse, never under the
agent-writable ``apps/<app>/``. It is written only by the owner-authenticated grant route
and removed on a disable and on an uninstall. It is never pre-granted. It counts only while
the owner-enable marker stands and the owner has not disabled the app.

Nothing here logs ``D(app)``. This module imports nothing from the ``apps.manager`` facade at
load, so the uninstall owner can call it.
"""

from __future__ import annotations

import hashlib
import hmac
import json
import logging
import secrets
import threading
from datetime import datetime, timezone
from pathlib import Path
from types import SimpleNamespace
from typing import Any

from kiro_crew import member_records
from kiro_crew.apps.execution import APP_NAME_RE
from kiro_crew.atomic_write import atomic_write, read_bytes_with_retry

logger = logging.getLogger(__name__)

#: The record kind under ``member-records/``; one of ``member_records.RECORD_KINDS``.
APP_DASHBOARD_PROXY_KIND = "app-dashboard-proxy"

#: Binds a derived value to this capability, so the same secret and app name derive a
#: different value for any other use.
_DOMAIN = b"dashboard-proxy"

_RECORD_MAX_BYTES = 4096
_STAND_IN_SUFFIX = ".stand-in"

_secret_lock = threading.Lock()
_gateway_secret = ""


#: Apps whose last dashboard-token mint was refused; ignored once the app holds the grant.
#: shortcut: in memory, so a gateway restart forgets it until the app asks again; the
#: backend asks on every start, so the owner sees the state again within one retry.
_refused_mints: set[str] = set()
_refused_lock = threading.Lock()


def note_refused_mint(app_name: str) -> None:
    """Remember that *app_name* asked for a dashboard token and holds no grant."""
    if not APP_NAME_RE.fullmatch(app_name) or granted(app_name):
        return
    with _refused_lock:
        _refused_mints.add(app_name)


def needs_access(app_name: str) -> bool:
    """Whether the app's last mint was refused for want of a grant that is still absent."""
    with _refused_lock:
        asked = app_name in _refused_mints
    return asked and not granted(app_name)


def set_gateway_secret(secret: str) -> None:
    """Hand this gateway process's local secret to the derivation, once at startup.

    Held in memory only. The gateway generates the secret before it spawns the first app
    backend, so a boot-time spawn can already be given its derived value.
    """
    global _gateway_secret
    with _secret_lock:
        _gateway_secret = secret if isinstance(secret, str) else ""


def _secret() -> str:
    with _secret_lock:
        return _gateway_secret


def _record_dir() -> Path:
    return member_records.records_dir(APP_DASHBOARD_PROXY_KIND).resolve()


def _path(app_name: str, suffix: str) -> Path:
    """``member-records/app-dashboard-proxy/<app><suffix>``. Raises ``ValueError``."""
    if not isinstance(app_name, str) or not APP_NAME_RE.fullmatch(app_name):
        raise ValueError(f"unsafe app name: {app_name!r}")
    root = _record_dir()
    target = (root / f"{app_name}{suffix}").resolve()
    if target.parent != root:
        raise ValueError(f"app name {app_name!r} escapes {root}")
    return target


_INSTALL_SUFFIX = ".install.json"
_INSTALL_FIELDS = (
    "installedAt",
    "version",
    "origin",
    "sourceRegistry",
    "sourceUrl",
    "sourceCommit",
    "sourceSigner",
)


def record_install(
    app_name: str,
    *,
    version: str,
    registry: str,
    url: str,
    commit: str = "",
    signer: str = "",
) -> bool:
    """Seal the gateway's own record of a registry install of *app_name*.

    Called by the install path (``set_app_provenance``) with what the gateway resolved.
    ``apps/<app>/installed.json`` is writable from the sandbox, so provenance, the offer
    and the grant identity are read from this record only. Every call is a new install:
    it carries a fresh ``installedAt`` stamp, which a grant given for another install lacks.
    """
    stamp = f"{datetime.now(timezone.utc).strftime('%Y-%m-%dT%H:%M:%SZ')}#{secrets.token_hex(8)}"
    body = {
        "app": app_name,
        "installedAt": stamp,
        "version": version,
        "origin": "registry",
        "sourceRegistry": registry,
        "sourceUrl": url,
        "sourceCommit": commit,
        "sourceSigner": signer,
    }
    try:
        path = _path(app_name, _INSTALL_SUFFIX)
        path.parent.mkdir(parents=True, exist_ok=True)
        atomic_write(path, json.dumps(body) + "\n", fsync=True)
    except (OSError, ValueError):
        logger.warning("could not seal the install record for %r", app_name)
        return False
    return True


def forget_install(app_name: str) -> None:
    """Remove the sealed install record (uninstall). A no-op when none exists."""
    try:
        _path(app_name, _INSTALL_SUFFIX).unlink()
    except FileNotFoundError:
        pass
    except (OSError, ValueError):
        logger.warning("could not remove the install record for %r", app_name)


def _installed(app_name: str) -> Any:
    """The sealed install record as an object with the install fields, or ``None``."""
    try:
        raw = read_bytes_with_retry(_path(app_name, _INSTALL_SUFFIX), max_bytes=_RECORD_MAX_BYTES)
        data = json.loads(raw.decode("utf-8"))
    except (OSError, ValueError):
        return None
    if not isinstance(data, dict) or data.get("app") != app_name:
        return None
    return SimpleNamespace(**{k: str(data.get(k, "")) for k in _INSTALL_FIELDS})


def install_identity(app_name: str) -> str | None:
    """Who this install is, or ``None`` when the app is not installed with a record id.

    The install record id (``installedAt``) with the package it came from: the version and
    the registry provenance the gateway recorded at install. A reinstall writes a new
    ``installedAt``, and another package under the same name carries other provenance, so
    each gets another identity, which the old grant does not match.
    """
    try:
        rec = _installed(app_name)
    except Exception:
        return None
    if rec is None or not getattr(rec, "installedAt", ""):
        return None
    parts = (
        app_name,
        rec.installedAt,
        getattr(rec, "version", ""),
        getattr(rec, "origin", ""),
        getattr(rec, "sourceRegistry", ""),
        getattr(rec, "sourceUrl", ""),
        getattr(rec, "sourceCommit", ""),
        getattr(rec, "sourceSigner", ""),
    )
    return hashlib.sha256("\0".join(str(p) for p in parts).encode("utf-8")).hexdigest()


def registry_verified(app_name: str) -> bool:
    """Whether the gateway recorded a registry install for *app_name*.

    The record is sealed under ``member-records/`` by the gateway's own install path
    (:func:`record_install`, from ``set_app_provenance``): the install came from a registry,
    with the source it resolved to and the registry or signer that admitted it. Never the
    app's manifest or its ``installed.json``, which the app can write.
    """
    try:
        rec = _installed(app_name)
    except Exception:
        return False
    return bool(
        rec is not None
        and getattr(rec, "origin", "") == "registry"
        and getattr(rec, "sourceUrl", "")
        and (getattr(rec, "sourceRegistry", "") or getattr(rec, "sourceSigner", ""))
    )


def prompt_text(app_name: str) -> dict[str, str] | None:
    """The words the owner sees before granting, or ``None`` when no grant may be offered.

    Offered only for an app the gateway recorded as installed from a registry. It names the
    publisher and package from that record, not from the manifest. Never offered on a backend
    that cannot bind a stand-in.
    """
    if not _namespace_backend() or not registry_verified(app_name):
        return None
    rec = _installed(app_name)
    registry = getattr(rec, "sourceRegistry", "") or "the bundled catalog"
    source = getattr(rec, "sourceUrl", "")
    version = getattr(rec, "version", "")
    return {
        "app": app_name,
        "registry": registry,
        "source": source,
        "version": version,
        # What the grant lets it do is the scope line the dashboard shows after this text.
        "text": (
            f"Allow {app_name} {version} (registry {registry}, source {source}) "
            "to use this dashboard on your behalf?"
        ),
    }


def _marker_state(app_name: str) -> tuple[bool, bool]:
    """``(owner-enable marker stands, owner has not disabled the app)``."""
    from kiro_crew.apps.host_exec_grants import host_exec_granted
    from kiro_crew.apps.protected_app_state import owner_disabled

    return host_exec_granted(app_name), not owner_disabled(app_name)


def _stored_field(app_name: str, field: str) -> str | None:
    try:
        raw = read_bytes_with_retry(_path(app_name, ".json"), max_bytes=_RECORD_MAX_BYTES)
        data = json.loads(raw.decode("utf-8"))
    except (OSError, ValueError):
        return None
    if not isinstance(data, dict) or data.get("app") != app_name:
        return None
    value = data.get(field)
    return value if isinstance(value, str) and value else None


def _stored_identity(app_name: str) -> str | None:
    return _stored_field(app_name, "identity")


def _stored_grant_id(app_name: str) -> str | None:
    return _stored_field(app_name, "grant_id")


def grant_claim(app_name: str) -> str:
    """The value a delegated token and its refresh chain carry for the grant held now.

    ``HMAC(local secret, app + grant id)``: ``""`` while the app holds no grant. Each
    grant has a new id, so a token from before a revoke, a disable, an uninstall or a
    reinstall never matches the next grant, and a new gateway secret ends every one.
    """
    secret, grant_id = _secret(), _stored_grant_id(app_name)
    if not secret or not grant_id or not granted(app_name):
        return ""
    message = b"\0".join((b"dashboard-grant", app_name.encode(), grant_id.encode()))
    return hmac.new(secret.encode("utf-8"), message, hashlib.sha256).hexdigest()


def grant_current(app_name: str, claim: str) -> bool:
    """Whether *claim* is :func:`grant_claim` of the grant this app holds right now."""
    current = grant_claim(app_name)
    return bool(claim and current and hmac.compare_digest(current, claim))


def granted(app_name: str) -> bool:
    """Whether *app_name* holds the capability right now.

    The stored grant names this install, the owner-enable marker stands and the owner has
    not disabled the app. Read on every use, so a disable or an uninstall ends it at once.
    """
    stored = _stored_identity(app_name)
    current = install_identity(app_name)
    if not stored or not current or not hmac.compare_digest(stored, current):
        return False
    enabled, not_disabled = _marker_state(app_name)
    return bool(enabled and not_disabled)


def grant(app_name: str) -> tuple[bool, str]:
    """Record the owner's grant. Call only from the owner-authenticated grant route.

    Returns ``(True, "")`` or ``(False, reason)``; never raises for a refusal. Refused with
    ``platform-unsupported`` where the sandbox cannot bind a stand-in.
    """
    if not _namespace_backend():
        return False, "platform-unsupported"
    if not registry_verified(app_name):
        return False, "not-registry-verified"
    identity = install_identity(app_name)
    if not identity:
        return False, "not-installed"
    enabled, not_disabled = _marker_state(app_name)
    if not (enabled and not_disabled):
        return False, "not-enabled"
    try:
        path = _path(app_name, ".json")
        path.parent.mkdir(parents=True, exist_ok=True)
        body = {"app": app_name, "identity": identity, "grant_id": secrets.token_hex(16)}
        atomic_write(path, json.dumps(body) + "\n", fsync=True)
    except (OSError, ValueError):
        logger.warning("could not record the dashboard_proxy grant for %r", app_name)
        return False, "write-failed"
    return True, ""


def revoke(app_name: str) -> None:
    """Remove the grant and its stand-in source. A no-op when none exists."""
    for suffix in (".json", _STAND_IN_SUFFIX):
        try:
            _path(app_name, suffix).unlink()
        except FileNotFoundError:
            pass
        except (OSError, ValueError):
            logger.warning("could not remove the dashboard_proxy record for %r", app_name)


def stand_in_value(app_name: str) -> str | None:
    """``D(app)``, or ``None`` unless the app holds the grant and the secret is known."""
    secret = _secret()
    if not secret or not granted(app_name):
        return None
    identity = install_identity(app_name)
    if not identity:
        return None
    from kiro_crew.dashboard.boot_id import current_boot_id

    message = b"\0".join(
        (_DOMAIN, app_name.encode(), identity.encode(), current_boot_id().encode())
    )
    return hmac.new(secret.encode("utf-8"), message, hashlib.sha256).hexdigest()


def stand_in_pairs(app_name: str, masked_paths: tuple[str, ...]) -> tuple[tuple[str, str], ...]:
    """``(masked file, source file)`` for each masked secret file, or ``()`` without the grant.

    The source holds ``D(app)``, owner-only, in the sandbox-masked records directory; the
    namespace launcher copies it into each stand-in before the app starts.
    """
    value = stand_in_value(app_name)
    if value is None:
        return ()
    try:
        source = _path(app_name, _STAND_IN_SUFFIX)
        source.parent.mkdir(parents=True, exist_ok=True)
        atomic_write(source, value, fsync=True, restrict_to_owner=True)
    except (OSError, ValueError):
        logger.warning("could not stage the dashboard_proxy stand-in for %r", app_name)
        return ()
    return tuple((masked, str(source)) for masked in masked_paths)


def _namespace_backend() -> bool:
    from kiro_crew.sandbox_wrap import detect_backend

    return detect_backend(config_mode="standard") == "namespace"


def platform_supported() -> bool:
    """Whether this host can bind a stand-in, so a grant can be offered at all."""
    return _namespace_backend()


def backend_masks(
    app_name: str, masked_paths: tuple[str, ...]
) -> tuple[tuple[str, ...], tuple[tuple[str, str], ...]]:
    """``(paths to mask, stand-ins)`` for one running backend's spawn.

    An app without the grant: every path masked, every stand-in empty. A granted app: the
    same masks with its derived value over each of them (the Linux namespace sandbox, the
    only backend that can bind one). On any other backend a granted app is masked like an
    ungranted one: the grant is refused there (:func:`grant`), so none stands.

    shortcut: Seatbelt (macOS) has no stand-in to fill, so Crew Tunnel waits for an API
    path there. Windows has no mask at all, and a host with no sandbox has no boundary.
    """
    if not _namespace_backend():
        return masked_paths, ()
    return masked_paths, stand_in_pairs(app_name, masked_paths)


def sandboxed_backend() -> bool:
    """Whether app backends run inside a sandbox on this host (any backend but none)."""
    from kiro_crew.sandbox_wrap import detect_backend

    return detect_backend(config_mode="standard") != "none"


def verify_leg_two(app_name: str, provided: str) -> str:
    """The reason a derived value is refused, or ``""`` when it is ``D(app)``."""
    expected = stand_in_value(app_name)
    if expected is None:
        return "not-granted"
    if not provided or not hmac.compare_digest(expected, provided):
        return "wrong-value"
    return ""
