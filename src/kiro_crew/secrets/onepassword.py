"""Read-only 1Password backend, through 1Password's official Python SDK.

A person connects their 1Password account in one of two ways. The main way
signs in through the 1Password desktop app on this computer (the SDK's
``DesktopAuth``): they type the account name, click Connect and approve in the
app; Kiro Crew stores only the account name. The fallback, offered when the app
is missing or when the person asks, is a Service Account token: the core
(:mod:`kiro_crew.secrets.backend`) stores it in the local vault and hands it
here, in the gateway, where it goes to the SDK in-process
(``onepassword.client.Client.authenticate``): never to a log, an argv, an
environment or an API answer.

The app approves a process for ten idle minutes and drops it when locked
(``DesktopSessionExpiredException``); the panel then shows Approve again. Jobs
that run while nobody can approve need the token way.

The SDK (``onepassword-sdk``, MIT) is the optional ``1password`` extra, and it
is imported lazily: only when a person connects, a connection is listed, or a
reference resolves through 1Password. Where it is not
installed, :meth:`OnePasswordBackend.availability` says whether this system
could have it (``sdk_missing``) or never can (``sdk_unsupported``: no wheel is
published for it). The local vault never depends on it.

Kiro Crew never writes to 1Password: this module calls ``vaults.list``,
``items.list`` and ``items.get_all`` and nothing else, over every vault the
connection can read. Values are read on demand; nothing from 1Password is
cached or copied into the local vault.

Item mapping (1Password category -> vault item type, :mod:`.items`):

``Login``           -> ``login``: the ``username`` and ``password`` fields, the
                       item's notes and its http(s) websites.
``SecureNote``      -> ``note``: the item's notes.
``Password``        -> ``secret``: the ``password`` field.
``ApiCredentials``  -> ``secret``: the ``credential`` field.

Every other category is not listed and not resolved.
"""

from __future__ import annotations

import asyncio
import json
import platform
import sys
import threading
import time
from collections.abc import Awaitable, Callable, Mapping
from typing import Any, ClassVar, Optional, TypeVar

from kiro_crew.secrets import items
from kiro_crew.secrets.backend import (
    BackendError,
    Capabilities,
    Connection,
    FormField,
    SecretBackend,
)
from kiro_crew.secrets.vault import SecretValue

T = TypeVar("T")

#: Sent to 1Password with every SDK client, as its integration name.
_INTEGRATION_NAME = "Kiro Crew"
_INTEGRATION_VERSION = "1"

_CATEGORY_TYPES = {
    "Login": items.ITEM_TYPE_LOGIN,
    "SecureNote": items.ITEM_TYPE_NOTE,
    "Password": items.ITEM_TYPE_SECRET,
    "ApiCredentials": items.ITEM_TYPE_SECRET,
}

#: The start of the message onepassword-sdk 0.4.2 raises (a bare ``Exception``)
#: for a token it refuses, as measured against the real service: a malformed
#: token ("... base64 decoding failed ...") and a well-formed one 1Password does
#: not know ("... deserialization failed ...") both begin with it.
_REFUSED_TOKEN_PREFIX = "invalid service account token"
#: The type the SDK raises (``onepassword.errors``) when 1Password throttles.
_RATE_LIMITED_TYPE = "RateLimitExceededException"
#: The type the SDK raises (``onepassword.errors``) when the app's approval of
#: this process has run out (ten idle minutes, or the app was locked).
_EXPIRED_TYPE = "DesktopSessionExpiredException"

#: A Service Account token starts ``ops_``.
_TOKEN_PREFIX = "ops_"
_MAX_ACCOUNT_CHARS = 256

#: The oldest glibc the SDK's Linux wheels (manylinux_2_32) run on.
_MIN_GLIBC = (2, 32)

_KEY = "settings.secrets.backend_op_"


def _error(code: str, message: str) -> BackendError:
    """A 1Password-specific failure, worded by its own catalog key."""
    return BackendError(code, message, message_key=_KEY + "err_" + code)


# ── Whether the SDK is here ──


def _wheel_platform() -> bool:
    """Whether onepassword-sdk 0.4.2 publishes a wheel for this system.

    Its wheels: Linux x86_64/aarch64 on glibc 2.32+, macOS x86_64/arm64, Windows
    x64, CPython 3.9-3.13. No musl, no Windows arm64 (measured with ``pip
    download --only-binary``). Kept in step with the extra's markers in
    ``setup.cfg``.
    """
    if sys.version_info >= (3, 14):
        return False
    system = platform.system()
    machine = platform.machine().lower()
    if system == "Windows":
        return machine in ("amd64", "x86_64")
    if machine not in ("x86_64", "amd64", "aarch64", "arm64"):
        return False
    if system == "Darwin":
        return True
    if system != "Linux":
        return False
    libc, version = platform.libc_ver()
    if libc != "glibc":
        return False
    try:
        return tuple(int(part) for part in version.split(".")[:2]) >= _MIN_GLIBC
    except ValueError:
        return False


def _sdk() -> tuple[Any, Any]:
    """The SDK's ``Client`` and ``DesktopAuth``, imported now; a :class:`BackendError` when absent."""
    try:
        from onepassword.client import Client, DesktopAuth
    except ImportError:
        if _wheel_platform():
            raise BackendError(
                "sdk_missing",
                "The 1Password package is not installed in the gateway's Python environment.",
            ) from None
        raise BackendError("sdk_unsupported", "1Password cannot run on this system.") from None
    return Client, DesktopAuth


#: One approved desktop-app client per account name, in this process, with the
#: time of its last successful use. The app approves a PROCESS for ten idle
#: minutes; keeping the client lets a listing read without asking again.
# shortcut: per process (the dashboard and the MCP gateway each approve once);
# share one approved helper if two prompts per sign-in is too many.
_APPROVED: dict[str, tuple[Any, float]] = {}
#: How long the 1Password app keeps an idle approval. Past it the SDK re-inits
#: the client on its next call, which shows the app's Approve prompt, so a kept
#: client idle this long is treated as lapsed before the SDK is called.
APPROVAL_IDLE_SECS = 600.0
# shortcut: the app locking inside the ten minutes is not seen here; the SDK's
# re-init then prompts even on a listing. Upgrade if the SDK exposes a session check.
_now = time.monotonic
_APPROVED_LOCK = threading.Lock()
#: Failures after which the kept client is no good: the next read must approve again.
_DROPS_APPROVAL = frozenset({"approval_expired", "app_closed", "app_missing", "not_approved"})


def _map_failure(exc: Exception, *, desktop: bool, signing_in: bool = False) -> BackendError:
    """The SDK's failure, as a stable code. Its own text is never passed on.

    *signing_in* is the app's own sign-in step: a refusal there (the person
    clicked Deny, or the app is locked) reads ``not_approved``."""
    name = type(exc).__name__
    if name == _RATE_LIMITED_TYPE:
        return BackendError("rate_limited", "1Password is limiting requests.")
    if desktop:
        if isinstance(exc, FileNotFoundError):
            return BackendError("app_missing", "The 1Password app is not on this computer.")
        if name == _EXPIRED_TYPE:
            return BackendError("approval_expired", "Approve again in the 1Password app.")
        if isinstance(exc, RuntimeError):
            return _error("app_closed", "Open 1Password and turn on Integrate with other apps.")
        if signing_in:
            return BackendError("not_approved", "1Password did not approve.")
        return BackendError("unreachable", "1Password could not be reached.")
    if str(exc).lower().startswith(_REFUSED_TOKEN_PREFIX):
        return BackendError("unauthorized", "1Password did not accept the service account token.")
    return BackendError("unreachable", "1Password could not be reached.")


def _call(conn: Mapping[str, str], work: Callable[[Any], Awaitable[T]], *, ask: bool) -> T:
    """Run *work(client)* on a client for *conn*; map the SDK's failures.

    A token connection authenticates on every call. An app connection reuses
    this process's approved client while it was used in the last
    :data:`APPROVAL_IDLE_SECS`; past that, or with none kept, it asks the app to
    approve only when *ask* (a connect, Approve again, or a reference being
    resolved), and otherwise reads as ``approval_expired`` without calling the
    SDK. Blocking: runs its own event loop.
    """
    client_class, desktop_auth = _sdk()
    account = conn.get("account", "")
    desktop = bool(account)

    async def run() -> T:
        if not desktop:
            # shortcut: one SDK client per call, so each call authenticates again;
            # keep a client per connection if service-account rate limits are hit.
            client = await client_class.authenticate(
                auth=conn["token"],
                integration_name=_INTEGRATION_NAME,
                integration_version=_INTEGRATION_VERSION,
            )
            return await work(client)
        with _APPROVED_LOCK:
            kept = _APPROVED.get(account)
            if kept is not None and _now() - kept[1] >= APPROVAL_IDLE_SECS:
                # Lapsed: calling the SDK now would re-init and prompt on its own.
                del _APPROVED[account]
                kept = None
        client = kept[0] if kept is not None else None
        if client is None:
            if not ask:
                raise BackendError("approval_expired", "Approve again in the 1Password app.")
            try:
                client = await client_class.authenticate(
                    auth=desktop_auth(account_name=account),
                    integration_name=_INTEGRATION_NAME,
                    integration_version=_INTEGRATION_VERSION,
                )
            except Exception as exc:  # noqa: BLE001 -- the SDK raises bare Exception
                raise _map_failure(exc, desktop=True, signing_in=True) from None
            with _APPROVED_LOCK:
                _APPROVED[account] = (client, _now())
        result = await work(client)
        with _APPROVED_LOCK:
            if account in _APPROVED and _APPROVED[account][0] is client:
                _APPROVED[account] = (client, _now())
        return result

    try:
        return asyncio.run(run())
    except BackendError as exc:
        if desktop and exc.code in _DROPS_APPROVAL:
            with _APPROVED_LOCK:
                _APPROVED.pop(account, None)
        raise
    except Exception as exc:  # noqa: BLE001 -- the SDK raises bare Exception
        mapped = _map_failure(exc, desktop=desktop)
        if desktop and mapped.code in _DROPS_APPROVAL:
            with _APPROVED_LOCK:
                _APPROVED.pop(account, None)
        raise mapped from None


def forget_approvals() -> None:
    """Drop every approved client in this process (a disconnect, and tests)."""
    with _APPROVED_LOCK:
        _APPROVED.clear()


def _printable(text: str, cap: int = 128) -> str:
    return "".join(ch for ch in text if ch.isprintable())[:cap]


async def _vault_ids(client: Any) -> list[str]:
    """Every vault the connection can see."""
    return [str(v.id) for v in await client.vaults.list()]


# ── Item mapping ──


def _category(item: Any) -> str:
    category = getattr(item, "category", "")
    return str(getattr(category, "value", category))


def _field(item: Any, field_id: str) -> str:
    for field in getattr(item, "fields", None) or []:
        if getattr(field, "id", None) == field_id:
            value = getattr(field, "value", "")
            return value if isinstance(value, str) else ""
    return ""


def _urls(item: Any) -> list[str]:
    out = []
    for site in getattr(item, "websites", None) or []:
        url = getattr(site, "url", None)
        if isinstance(url, str) and url.startswith(("http://", "https://")):
            out.append(url)
    return out[: items.MAX_LOGIN_URLS]


def to_plaintext(item: Any) -> Optional[tuple[str, str]]:
    """``(item_type, plaintext)`` for an SDK ``Item``, in the local vault's
    encoding (:mod:`.items`), or ``None`` for an unsupported category."""
    category = _category(item)
    item_type = _CATEGORY_TYPES.get(category)
    if item_type is None:
        return None
    notes = getattr(item, "notes", "")
    notes = notes if isinstance(notes, str) else ""
    if item_type == items.ITEM_TYPE_LOGIN:
        fields = {
            items.FIELD_USERNAME: _field(item, "username"),
            items.FIELD_PASSWORD: _field(item, "password"),
            "urls": _urls(item),
            items.FIELD_NOTES: notes,
        }
        return item_type, json.dumps(fields, sort_keys=True, ensure_ascii=False)
    if item_type == items.ITEM_TYPE_NOTE:
        return item_type, json.dumps({items.FIELD_TEXT: notes}, ensure_ascii=False)
    if category == "ApiCredentials":
        return item_type, _field(item, "credential")
    return item_type, _field(item, "password")


async def _get_all(client: Any, vault_id: str, ids: list[str]) -> list[Any]:
    """The full items for *ids*; any per-item error fails the whole read."""
    if not ids:
        return []
    response = await client.items.get_all(vault_id, ids)
    out = []
    for one in response.individual_responses:
        if one.error is not None or one.content is None:
            raise BackendError("unreachable", "1Password could not be reached.")
        out.append(one.content)
    return out


# ── The backend ──


async def _overviews(client: Any, conn: Mapping[str, str]) -> list[tuple[str, Any]]:
    """``(vault_id, overview)`` for every item of a mapped category in the connection's vaults."""
    out: list[tuple[str, Any]] = []
    for vault_id in await _vault_ids(client):
        for overview in await client.items.list(vault_id):
            if _category(overview) in _CATEGORY_TYPES:
                out.append((vault_id, overview))
    return out


async def _read(client: Any, wanted: list[tuple[str, str]]) -> list[Any]:
    """The full items for ``(vault_id, item_id)`` pairs, one batch per vault."""
    by_vault: dict[str, list[str]] = {}
    for vault_id, item_id in wanted:
        by_vault.setdefault(vault_id, []).append(item_id)
    out: list[Any] = []
    for vault_id, ids in by_vault.items():
        out.extend(await _get_all(client, vault_id, ids))
    return out


class OnePasswordBackend(SecretBackend):
    """A connected 1Password account, read-only.

    The main form signs in through the 1Password desktop app (the SDK's
    ``DesktopAuth``): the person types the account name and approves in the
    app; no token is stored, only the name. The fallback form takes a Service
    Account token, for an install without the app and for jobs that run while
    nobody is there to approve.
    """

    id: ClassVar[str] = "1password"
    display_name: ClassVar[str] = "1Password"
    extra: ClassVar[str] = "1password"
    capabilities: ClassVar[Capabilities] = Capabilities(
        item_types=(items.ITEM_TYPE_LOGIN, items.ITEM_TYPE_NOTE, items.ITEM_TYPE_SECRET)
    )
    connect_form: ClassVar[tuple[FormField, ...]] = (FormField("account", _KEY + "account_label"),)
    fallback_form: ClassVar[tuple[FormField, ...]] = (
        FormField(
            "token", _KEY + "token_label", secret=True, placeholder_key=_KEY + "token_placeholder"
        ),
    )
    fallback_label_key: ClassVar[str] = _KEY + "use_token"
    app_sign_in: ClassVar[bool] = True
    help_url: ClassVar[str] = (
        "https://start.1password.com/developer-tools/infrastructure-secrets/serviceaccount/"
    )

    def availability(self) -> Optional[BackendError]:
        try:
            _sdk()
        except BackendError as exc:
            return exc
        return None

    def prefers_fallback(self) -> bool:
        """Whether the 1Password app is missing here, asked the way the SDK asks."""
        try:
            from onepassword.desktop_core import find_1password_lib_path
        except ImportError:
            return False
        try:
            find_1password_lib_path()
        except (OSError, FileNotFoundError):
            return True
        return False

    def connect(self, values: Mapping[str, str]) -> Connection:
        async def vault_names(client: Any) -> list[str]:
            return [_printable(str(v.title)) for v in await client.vaults.list()]

        if "token" in values:
            token = values["token"]
            if not token.startswith(_TOKEN_PREFIX) or any(ch.isspace() for ch in token):
                raise _error("invalid_token", "That is not a 1Password service account token.")
            names = _call({"token": token}, vault_names, ask=True)
            if not names:
                raise _error("no_vaults", "This token cannot read any 1Password vault.")
            return Connection(
                account=", ".join(sorted(names))[:128], data={"token": token}, form="fallback"
            )
        account = values.get("account", "")
        if not account or len(account) > _MAX_ACCOUNT_CHARS:
            raise BackendError("invalid_input", "The account name is not valid.")
        # A fresh connect or Approve again always asks the app.
        with _APPROVED_LOCK:
            _APPROVED.pop(account, None)
        _call({"account": account}, vault_names, ask=True)
        return Connection(account=account, data={"account": account}, form="main")

    def disconnect(self, conn: Mapping[str, str]) -> None:
        if conn.get("account"):
            with _APPROVED_LOCK:
                _APPROVED.pop(conn["account"], None)

    def list_items(self, conn: Mapping[str, str]) -> list[dict[str, Any]]:
        """One item list per vault, then one batch read of the logins to show
        their username (the overview does not carry it); only
        :func:`items.public_view` of each reaches the listing. An app connection
        never asks the app from here: a lapsed approval reads ``approval_expired``."""

        async def work(client: Any) -> list[dict[str, Any]]:
            overviews = await _overviews(client, conn)
            full = {
                item.id: item
                for item in await _read(
                    client,
                    [(vid, str(o.id)) for vid, o in overviews if _category(o) == "Login"],
                )
            }
            out: list[dict[str, Any]] = []
            for _vid, overview in overviews:
                fields: dict[str, Any] = {}
                login = full.get(overview.id)
                mapped = to_plaintext(login) if login is not None else None
                if mapped is not None:
                    fields = items.public_view(*mapped)
                out.append(
                    {
                        "name": str(overview.title),
                        "type": _CATEGORY_TYPES[_category(overview)],
                        "fields": fields,
                    }
                )
            return out

        return _call(conn, work, ask=False)

    def get_items_many(
        self, conn: Mapping[str, str], names: list[str]
    ) -> dict[str, Optional[tuple[str, SecretValue]]]:
        """One item list per vault, then one batch read of the matched items. An
        unsupported category reads as missing; a name two items share (in any
        vaults) fails closed (``ambiguous``). A reference may ask the app to
        approve this process."""

        async def work(client: Any) -> dict[str, Optional[tuple[str, SecretValue]]]:
            by_title: dict[str, list[tuple[str, str]]] = {}
            for vault_id, overview in await _overviews(client, conn):
                by_title.setdefault(str(overview.title), []).append((vault_id, str(overview.id)))
            ids: dict[str, str] = {}
            wanted: list[tuple[str, str]] = []
            for name in names:
                found = by_title.get(name, [])
                if len(found) > 1:
                    raise BackendError("ambiguous", "More than one 1Password item has this name.")
                if found:
                    ids[found[0][1]] = name
                    wanted.append(found[0])
            out: dict[str, Optional[tuple[str, SecretValue]]] = {name: None for name in names}
            for item in await _read(client, wanted):
                mapped = to_plaintext(item)
                if mapped is not None:
                    out[ids[str(item.id)]] = (mapped[0], SecretValue(mapped[1]))
            return out

        return _call(conn, work, ask=True)
