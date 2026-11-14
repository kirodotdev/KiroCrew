"""Read-only 1Password backend, through 1Password's official Python SDK.

A person connects ONE 1Password vault with a Service Account token. The core
(:mod:`kiro_crew.secrets.backend`) stores the token in the local vault and hands
it here, in the gateway, where it goes to the SDK in-process
(``onepassword.client.Client.authenticate``): never to a log, an argv, an
environment or an API answer.

The SDK (``onepassword-sdk``, MIT) is the optional ``1password`` extra, and it
is imported lazily: only when a person connects or a reference resolves through
1Password, both of which need the ``secrets_vault`` preview on. Where it is not
installed, :meth:`OnePasswordBackend.availability` says whether this system
could have it (``sdk_missing``) or never can (``sdk_unsupported``: no wheel is
published for it). The local vault never depends on it.

Kiro Crew never writes to 1Password: this module calls ``vaults.list``,
``items.list`` and ``items.get_all`` and nothing else. Values are read on
demand; nothing from 1Password is cached or copied into the local vault.

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

#: A Service Account token starts ``ops_``.
_TOKEN_PREFIX = "ops_"
_MAX_VAULT_CHARS = 256

#: The oldest glibc the SDK's Linux wheels (manylinux_2_32) run on.
_MIN_GLIBC = (2, 32)

_KEY = "settings.secrets.backend_op_"


def _error(code: str, message: str, vaults: Optional[list[str]] = None) -> BackendError:
    """A 1Password-specific failure, worded by its own catalog key."""
    return BackendError(
        code, message, message_key=_KEY + "err_" + code, params={"vaults": ", ".join(vaults or [])}
    )


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


def _client_class() -> Any:
    """The SDK's ``Client``, imported now; a :class:`BackendError` when absent."""
    try:
        from onepassword.client import Client
    except ImportError:
        if _wheel_platform():
            raise BackendError(
                "sdk_missing",
                "The 1Password package is not installed in the gateway's Python environment.",
            ) from None
        raise BackendError("sdk_unsupported", "1Password cannot run on this system.") from None
    return Client


def _call(token: str, work: Callable[[Any], Awaitable[T]]) -> T:
    """Authenticate with *token* and run *work(client)*; map the SDK's failures.

    Blocking: runs its own event loop. The core runs it on a worker thread under
    its timeout and turns any other exception into ``unreachable``.
    """
    client_class = _client_class()

    async def run() -> T:
        # shortcut: one SDK client per call, so each call authenticates again;
        # keep a client per connection if service-account rate limits are hit.
        client = await client_class.authenticate(
            auth=token,
            integration_name=_INTEGRATION_NAME,
            integration_version=_INTEGRATION_VERSION,
        )
        return await work(client)

    try:
        return asyncio.run(run())
    except BackendError:
        raise
    except Exception as exc:  # noqa: BLE001 -- the SDK raises bare Exception
        if type(exc).__name__ == _RATE_LIMITED_TYPE:
            raise BackendError("rate_limited", "1Password is limiting requests.") from None
        if str(exc).lower().startswith(_REFUSED_TOKEN_PREFIX):
            raise BackendError(
                "unauthorized", "1Password did not accept the service account token."
            ) from None
        raise BackendError("unreachable", "1Password could not be reached.") from None


def _printable(text: str, cap: int = 128) -> str:
    return "".join(ch for ch in text if ch.isprintable())[:cap]


def pick_vault(vaults: list[dict[str, str]], wanted: str) -> dict[str, str]:
    """The one vault *wanted* names (by name or id), or the only one when empty.

    *vaults* is ``[{"id", "name"}]``. Fails closed on no match, several
    matches, or several vaults and no choice; the error lists the vault names
    to choose from (not secret).
    """
    names = sorted(_printable(row["name"]) for row in vaults)
    if not vaults:
        raise _error("no_vaults", "This token cannot read any 1Password vault.")
    if not wanted:
        if len(vaults) == 1:
            return vaults[0]
        raise _error("choose_vault", "This token can read several vaults. Name one.", names)
    matches = [row for row in vaults if wanted in (row["name"], row["id"])]
    if len(matches) == 1:
        return matches[0]
    if not matches:
        raise _error("vault_not_found", "No vault has that name.", names)
    raise _error("ambiguous_vault", "More than one vault has that name.", names)


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


class OnePasswordBackend(SecretBackend):
    """One connected 1Password vault, read through a service account."""

    id: ClassVar[str] = "1password"
    display_name: ClassVar[str] = "1Password"
    extra: ClassVar[str] = "1password"
    capabilities: ClassVar[Capabilities] = Capabilities(
        item_types=(items.ITEM_TYPE_LOGIN, items.ITEM_TYPE_NOTE, items.ITEM_TYPE_SECRET)
    )
    connect_form: ClassVar[tuple[FormField, ...]] = (
        FormField(
            "token",
            _KEY + "token_label",
            secret=True,
            placeholder_key=_KEY + "token_placeholder",
            hint_key=_KEY + "token_hint",
        ),
        FormField("vault", _KEY + "vault_label", required=False, hint_key=_KEY + "vault_hint"),
    )
    help_url: ClassVar[str] = "https://developer.1password.com/docs/service-accounts/get-started/"
    help_label_key: ClassVar[str] = _KEY + "help"

    def availability(self) -> Optional[BackendError]:
        try:
            _client_class()
        except BackendError as exc:
            return exc
        return None

    def connect(self, values: Mapping[str, str]) -> Connection:
        token = values.get("token", "")
        if not token.startswith(_TOKEN_PREFIX) or any(ch.isspace() for ch in token):
            raise _error("invalid_token", "That is not a 1Password service account token.")
        choice = values.get("vault", "")
        if len(choice) > _MAX_VAULT_CHARS:
            raise BackendError("invalid_input", "The vault name is not valid.")

        async def vaults(client: Any) -> list[dict[str, str]]:
            return [{"id": str(v.id), "name": str(v.title)} for v in await client.vaults.list()]

        chosen = pick_vault(_call(token, vaults), choice)
        return Connection(account=chosen["name"], data={"token": token, "vault_id": chosen["id"]})

    def list_items(self, conn: Mapping[str, str]) -> list[dict[str, Any]]:
        """One item list, then one batch read of the logins to show their username
        (the overview does not carry it); only :func:`items.public_view` of each
        reaches the listing."""
        vault_id = conn["vault_id"]

        async def work(client: Any) -> list[dict[str, Any]]:
            overviews = [
                o for o in await client.items.list(vault_id) if _category(o) in _CATEGORY_TYPES
            ]
            full = {
                item.id: item
                for item in await _get_all(
                    client, vault_id, [o.id for o in overviews if _category(o) == "Login"]
                )
            }
            out: list[dict[str, Any]] = []
            for overview in overviews:
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

        return _call(conn["token"], work)

    def get_items_many(
        self, conn: Mapping[str, str], names: list[str]
    ) -> dict[str, Optional[tuple[str, SecretValue]]]:
        """One item list, then one batch read of the matched items. An
        unsupported category reads as missing; a name two items share fails
        closed (``ambiguous``)."""
        vault_id = conn["vault_id"]

        async def work(client: Any) -> dict[str, Optional[tuple[str, SecretValue]]]:
            by_title: dict[str, list[str]] = {}
            for overview in await client.items.list(vault_id):
                if _category(overview) in _CATEGORY_TYPES:
                    by_title.setdefault(str(overview.title), []).append(str(overview.id))
            ids: dict[str, str] = {}
            for name in names:
                found = by_title.get(name, [])
                if len(found) > 1:
                    raise BackendError("ambiguous", "More than one 1Password item has this name.")
                if found:
                    ids[found[0]] = name
            out: dict[str, Optional[tuple[str, SecretValue]]] = {name: None for name in names}
            for item in await _get_all(client, vault_id, list(ids)):
                mapped = to_plaintext(item)
                if mapped is not None:
                    out[ids[str(item.id)]] = (mapped[0], SecretValue(mapped[1]))
            return out

        return _call(conn["token"], work)
