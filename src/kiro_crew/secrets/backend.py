"""Third-party secret backends: the one contract, the registry, and the core around them.

The local vault (:class:`~kiro_crew.secrets.vault.SecretVault`) is the root and
the default. A backend is a password manager a person connects beside it, read
only: Kiro Crew lists its items and reads one when a ``secret://NAME#field``
reference needs it; creating, changing and deleting happen in the provider's own
app.

A provider implements :class:`SecretBackend` and nothing else. Everything that
makes a backend safe is here, in the core, and applies to every provider the
same way, because the provider is only ever called from inside it:

* the connection (a token and whatever else the connect form asks) is stored in
  the LOCAL vault under :func:`connection_item`, never handed to the agent, never
  listed, never writable through the generic secrets routes, never resolved;
* every provider call is bounded (:data:`CALL_TIMEOUT_SECS`) and any failure,
  typed or not, becomes a :class:`BackendError` with a stable code;
* a failing backend degrades to its own row showing ``unavailable``: the local
  listing and the resolution of local items never depend on it;
* resolution asks a backend only for a name the local vault does not hold, and
  never for a protected name (:func:`local_only`); a failure fails closed.

How to add a provider: docs/system-specs/modules/security.md, "Backends".
"""

from __future__ import annotations

import concurrent.futures
import json
import logging
import re
from abc import ABC, abstractmethod
from collections.abc import Callable, Iterable, Mapping
from dataclasses import asdict, dataclass, field
from typing import Any, ClassVar, Optional, TypeVar

from kiro_crew.secrets import items
from kiro_crew.secrets.vault import SecretValue, SecretVault

logger = logging.getLogger(__name__)

T = TypeVar("T")


#: Every provider call (list, read) is cut off after this long.
CALL_TIMEOUT_SECS = 20.0

#: A connect or Approve again waits this long: a person may have to approve in
#: the provider's own app first.
CONNECT_TIMEOUT_SECS = 120.0

#: A connect-form value may be at most this long.
MAX_FORM_VALUE_CHARS = 8192

#: The local-vault name prefix of every stored connection. It holds ``#``, which
#: ``POST /api/secrets`` refuses in a name, so no item a person stored can carry it.
CONNECTION_PREFIX = "kirocrew#backend:"

_BACKEND_ID_RE = re.compile(r"^[a-z0-9][a-z0-9_-]{0,31}$")
_FIELD_NAME_RE = re.compile(r"^[a-z][a-z0-9_]{0,31}$")
#: Every key a backend declares lives under the Secrets panel's catalog.
_I18N_KEY_RE = re.compile(r"^settings\.secrets\.[a-z0-9_.]+$")


# ── The contract ──


@dataclass(frozen=True)
class FormField:
    """One input of a backend's connect form, rendered by the generic panel."""

    #: The key the value arrives under in the connect body.
    name: str
    #: The i18n key of its label.
    label_key: str
    #: A secret is a password input, never echoed, never returned.
    secret: bool = False
    required: bool = True
    placeholder_key: str = ""


@dataclass(frozen=True)
class Capabilities:
    """What a backend offers. Sent to the panel as data.

    Every backend is read-only. A new capability lands with its first reader.
    """

    #: The vault item types it lists and resolves; any other row is dropped.
    item_types: tuple[str, ...] = (items.ITEM_TYPE_SECRET,)


@dataclass(frozen=True)
class Connection:
    """What a successful connect returns for the core to store.

    ``account`` names what is connected (a vault, an organisation) and is shown
    in the panel; it must not be secret. ``data`` holds the token and anything
    else the provider needs to read again; it never leaves the gateway.
    ``form`` is the form it came from: ``"main"`` or ``"fallback"``.
    """

    account: str
    data: dict[str, str] = field(default_factory=dict)
    form: str = "main"


class BackendError(Exception):
    """A backend call that failed. ``code`` is stable; the message is plain.

    Neither the message nor ``params`` may carry a token or a field value.
    Generic codes the panel words on its own: ``sdk_missing``,
    ``sdk_unsupported``, ``unreachable``, ``unauthorized``, ``rate_limited``,
    ``timeout``, ``invalid_input``, ``ambiguous``, and three for a backend whose
    main form signs in through the provider's desktop app: ``app_missing`` (the
    panel then offers the fallback form), ``not_approved`` and
    ``approval_expired`` (the panel offers Approve again). Any other code
    should carry ``message_key``, an i18n key the panel renders with ``params``.
    """

    def __init__(
        self,
        code: str,
        message: str,
        *,
        message_key: str = "",
        params: Optional[Mapping[str, str]] = None,
    ) -> None:
        super().__init__(message)
        self.code = code
        self.message_key = message_key
        self.params = dict(params or {})


class SecretBackend(ABC):
    """One third-party password manager, read-only. Implement this and register it.

    Every method is blocking and called from a worker thread, under a timeout,
    by the core. *conn* is the ``data`` its own :meth:`connect` returned. Raise
    :class:`BackendError` for a failure you can name; anything else reads as
    ``unreachable``. Never log, raise or return a token or a value except in
    :meth:`get_items_many`'s result.
    """

    #: Stable id: the route segment and the stored connection's name.
    id: ClassVar[str]
    #: The provider's own name, shown as is (a brand, never translated).
    display_name: ClassVar[str]
    capabilities: ClassVar[Capabilities] = Capabilities()
    connect_form: ClassVar[tuple[FormField, ...]]
    #: A second way to connect, shown only when :meth:`prefers_fallback` says so,
    #: after an ``app_missing`` failure, or when the person asks for it.
    fallback_form: ClassVar[tuple[FormField, ...]] = ()
    #: The i18n key of the link that opens the fallback form.
    fallback_label_key: ClassVar[str] = ""
    #: The main form signs in through the provider's desktop app: the panel says
    #: "Approve in the app" while it waits, and offers Approve again.
    app_sign_in: ClassVar[bool] = False
    #: Where a person gets the token, and the i18n key of that link.
    help_url: ClassVar[str] = ""
    help_label_key: ClassVar[str] = ""
    #: The ``setup.cfg`` extra that installs the provider's package, if any.
    extra: ClassVar[str] = ""

    def availability(self) -> Optional[BackendError]:
        """Why this backend cannot run on this install, or ``None`` when it can."""
        return None

    def prefers_fallback(self) -> bool:
        """Whether this install should open on the fallback form. Cheap; no provider call."""
        return False

    @abstractmethod
    def connect(self, values: Mapping[str, str]) -> Connection:
        """Check *values* (one form's fields) against the provider.

        The core sends the values of exactly one form, so the field names tell
        which. ``Approve again`` calls this once more with the stored ``data``'s
        main-form fields."""

    @abstractmethod
    def list_items(self, conn: Mapping[str, str]) -> list[dict[str, Any]]:
        """Each item's ``name``, ``type`` and public ``fields`` (``items.public_view``)."""

    @abstractmethod
    def get_items_many(
        self, conn: Mapping[str, str], names: list[str]
    ) -> dict[str, Optional[tuple[str, SecretValue]]]:
        """``(item_type, plaintext)`` per name in the local encoding, ``None`` when absent.

        A name two provider items share must raise (``ambiguous``), never pick one.
        """

    def disconnect(self, conn: Mapping[str, str]) -> None:
        """Release anything held at the provider. The core forgets the token either way."""
        return None


# ── The registry ──

_REGISTERED: dict[str, SecretBackend] = {}


def _builtin() -> list[SecretBackend]:
    from kiro_crew.secrets.onepassword import OnePasswordBackend

    return [OnePasswordBackend()]


def register_secret_backend(backend: SecretBackend) -> None:
    """Register a backend. Call once, at gateway start-up (a downstream edition's
    composition root). Refuses, loudly, anything the core could not serve."""
    _validate(backend)
    if backend.id in {b.id for b in _builtin()} or backend.id in _REGISTERED:
        raise ValueError(f"secret backend {backend.id!r} is already registered")
    _REGISTERED[backend.id] = backend


def _validate(backend: SecretBackend) -> None:
    if not isinstance(backend, SecretBackend):
        raise ValueError("a secret backend must subclass SecretBackend")
    backend_id = getattr(backend, "id", None)
    if not isinstance(backend_id, str) or not _BACKEND_ID_RE.match(backend_id):
        raise ValueError(f"secret backend id {backend_id!r} must match {_BACKEND_ID_RE.pattern}")
    if not isinstance(getattr(backend, "display_name", None), str) or not backend.display_name:
        raise ValueError(f"secret backend {backend_id!r} needs a display_name")
    caps = backend.capabilities
    if not caps.item_types or not set(caps.item_types) <= items.ITEM_TYPES:
        raise ValueError(f"secret backend {backend_id!r}: item_types must be vault item types")
    form = getattr(backend, "connect_form", ())
    if not form or len({f.name for f in form}) != len(form):
        raise ValueError(f"secret backend {backend_id!r} needs a connect form of unique fields")
    fallback = backend.fallback_form
    if len({f.name for f in fallback}) != len(fallback):
        raise ValueError(f"secret backend {backend_id!r}: fallback form fields must be unique")
    if fallback and not backend.fallback_label_key:
        raise ValueError(f"secret backend {backend_id!r}: a fallback form needs its link label")
    if backend.app_sign_in and any(f.secret for f in form):
        # Approve again reads the main form back from the stored connection.
        raise ValueError(f"secret backend {backend_id!r}: an app sign-in form holds no secret")
    for f in (*form, *fallback):
        if not _FIELD_NAME_RE.match(f.name):
            raise ValueError(f"secret backend {backend_id!r}: bad field name {f.name!r}")
    for key in i18n_keys(backend):
        if not _I18N_KEY_RE.match(key):
            raise ValueError(
                f"secret backend {backend_id!r}: i18n key {key!r} is not a Secrets key"
            )
    if backend.help_url and not backend.help_url.startswith("https://"):
        raise ValueError(f"secret backend {backend_id!r}: help_url must be https")


def i18n_keys(backend: SecretBackend) -> list[str]:
    """Every catalog key *backend* declares (its forms and links)."""
    fields = (*backend.connect_form, *backend.fallback_form)
    keys = [f.label_key for f in fields]
    keys += [f.placeholder_key for f in fields if f.placeholder_key]
    keys += [k for k in (backend.help_label_key, backend.fallback_label_key) if k]
    return keys


def registered() -> list[SecretBackend]:
    """Every backend, built-ins first, in the order resolution asks them."""
    return [*_builtin(), *_REGISTERED.values()]


def get(backend_id: str) -> Optional[SecretBackend]:
    return next((b for b in registered() if b.id == backend_id), None)


def reset_registered_for_tests() -> None:
    _REGISTERED.clear()


# ── Names the core keeps for itself ──


def connection_item(backend_id: str) -> str:
    """The local-vault name holding *backend_id*'s connection."""
    return CONNECTION_PREFIX + backend_id


def is_connection_item(name: str) -> bool:
    """Whether *name* holds a backend connection (registered or not)."""
    return name.startswith(CONNECTION_PREFIX)


def local_only(name: str) -> bool:
    """Whether a reference to *name* is answered by the local vault alone.

    A stored connection, the WakaTime OAuth tokens and the Connections OAuth
    entries are written by Kiro Crew's own flows; a provider item of the same
    name must never stand in for one.
    """
    from kiro_crew.config.loader import CRED_WAKATIME_OAUTH_TOKENS

    return (
        is_connection_item(name)
        or name == CRED_WAKATIME_OAUTH_TOKENS
        or name.startswith("CONNECTIONS_")
    )


# ── Calling a provider ──


def call(backend: SecretBackend, work: Callable[[], T], timeout: Optional[float] = None) -> T:
    """Run *work* (a call into *backend*) bounded by *timeout* (default
    :data:`CALL_TIMEOUT_SECS`); every failure is a BackendError.

    Blocking. The provider's own exception text is never logged or returned (it
    is not ours to vouch for); only its type name is logged.
    """
    unavailable = backend.availability()
    if unavailable is not None:
        raise unavailable
    # shortcut: a timed-out call keeps its worker thread until the provider
    # returns; give providers a cancellable client if that ever piles up.
    pool = concurrent.futures.ThreadPoolExecutor(max_workers=1, thread_name_prefix="secret-backend")
    try:
        return pool.submit(work).result(timeout=CALL_TIMEOUT_SECS if timeout is None else timeout)
    except BackendError:
        raise
    except concurrent.futures.TimeoutError:
        raise BackendError("timeout", f"{backend.display_name} did not answer in time.") from None
    except Exception as exc:  # noqa: BLE001 -- a provider's failure must not escape untyped
        logger.info("provider %s call raised %s", backend.id, type(exc).__name__)
        raise BackendError("unreachable", f"{backend.display_name} could not be reached.") from None
    finally:
        pool.shutdown(wait=False)


# ── The stored connection ──


def _unreadable(backend: SecretBackend) -> BackendError:
    return BackendError(
        "connection_unreadable", f"The saved {backend.display_name} connection cannot be read."
    )


def read_connection(vault: SecretVault, backend: SecretBackend) -> Optional[Connection]:
    """The stored connection, or ``None`` when there is none. Blocking.

    A stored entry that fails to decrypt or does not hold a connection raises
    ``connection_unreadable``: the listing shows it as that backend's own
    unavailable row, a resolve fails closed, and Disconnect still removes it.
    Only Kiro Crew writes this name (:data:`CONNECTION_PREFIX` holds ``#``, which
    every person-facing write refuses), so the entry is always ours.
    The vault's own "no entry opens" failure (``ValueError``: this entry is its
    only, damaged one, or the key opens nothing) reads the same way, so
    the listing still shows the row and its Disconnect.
    """
    name = connection_item(backend.id)
    try:
        found, unreadable = vault.get_items_readable([name])
    except ValueError:
        raise _unreadable(backend) from None
    if name in unreadable:
        raise _unreadable(backend)
    if name not in found:
        return None
    item_type, plaintext = found[name]
    try:
        data = json.loads(items.resolve_field(item_type, plaintext.reveal(), None))
    except (ValueError, items.ItemError):
        raise _unreadable(backend) from None
    account = data.get("account") if isinstance(data, dict) else None
    stored = data.get("data") if isinstance(data, dict) else None
    form = data.get("form", "main") if isinstance(data, dict) else None
    if (
        not isinstance(account, str)
        or form not in FORMS
        or not isinstance(stored, dict)
        or not all(isinstance(k, str) and isinstance(v, str) for k, v in stored.items())
    ):
        raise _unreadable(backend)
    return Connection(account=account, data=stored, form=form)


def connection_fields(conn: Connection) -> dict[str, str]:
    """The ``note`` item fields that store *conn* (read back by :func:`read_connection`)."""
    return {items.FIELD_TEXT: json.dumps(asdict(conn), separators=(",", ":"), sort_keys=True)}


#: The two forms a connect body may name in ``"form"``.
FORMS = ("main", "fallback")


def form_of(backend: SecretBackend, body: Mapping[str, Any]) -> tuple[str, tuple[FormField, ...]]:
    """Which form a connect *body* fills (``"form"``, default ``"main"``), and its fields."""
    name = body.get("form", "main")
    if name == "fallback" and backend.fallback_form:
        return name, backend.fallback_form
    if name != "main":
        raise BackendError("invalid_input", "That form is not valid.")
    return name, backend.connect_form


def form_values(backend: SecretBackend, body: Mapping[str, Any]) -> dict[str, str]:
    """The named form's values out of a request *body*; unknown keys are dropped."""
    values: dict[str, str] = {}
    for f in form_of(backend, body)[1]:
        raw = body.get(f.name, "")
        if not isinstance(raw, str) or len(raw) > MAX_FORM_VALUE_CHARS:
            raise BackendError("invalid_input", f"The {f.name} field is not valid.")
        if any(not ch.isprintable() for ch in raw):
            raise BackendError("invalid_input", f"The {f.name} field is not valid.")
        value = raw if f.secret else raw.strip()
        if f.required and not value.strip():
            raise BackendError("invalid_input", f"The {f.name} field is required.")
        values[f.name] = value
    return values


def approve_values(backend: SecretBackend, conn: Connection) -> dict[str, str]:
    """The main-form values *conn* was made from, for Approve again.

    Only an app sign-in connection can be approved again. Its main form holds
    no secret (:func:`_validate`), and the provider stores those fields in
    ``data`` under their names (an account name, never a token)."""
    if conn.form != "main" or not backend.app_sign_in:
        raise BackendError("invalid_input", "This connection cannot be approved again.")
    values = {f.name: conn.data.get(f.name, "") for f in backend.connect_form}
    if not all(values.values()):
        raise BackendError("invalid_input", "This connection cannot be approved again.")
    return values


# ── Listing ──


def _clean_items(rows: Iterable[Any], backend: SecretBackend) -> list[dict[str, Any]]:
    """Only well-formed rows of a declared item type, only public keys, one per name, sorted.

    A row is one ``secret://NAME``, so a name the provider gives several items
    is ONE row marked ``ambiguous`` (a reference to it fails closed), with no
    item's fields. A name with leading or trailing space is not listed: a
    reference cannot spell it, like a category the backend does not map.
    """
    by_name: dict[str, dict[str, Any]] = {}
    for row in rows:
        if not isinstance(row, dict):
            continue
        name, item_type = row.get("name"), row.get("type")
        if (
            not isinstance(name, str)
            or not name
            or name != name.strip()
            or item_type not in backend.capabilities.item_types
        ):
            continue
        if name in by_name:
            by_name[name].update(ambiguous=True, fields={})
            continue
        fields = row.get("fields")
        by_name[name] = {
            "name": name,
            "type": item_type,
            "source": backend.id,
            "fields": fields if isinstance(fields, dict) else {},
            "ambiguous": False,
        }
    return sorted(by_name.values(), key=lambda r: r["name"])


def error_view(backend: SecretBackend, exc: BackendError) -> dict[str, Any]:
    """``{code, message, message_key?, params?, install_command?}`` for the panel. Blocking."""
    out: dict[str, Any] = {"code": exc.code, "message": str(exc)}
    if exc.message_key:
        out["message_key"] = exc.message_key
    if exc.params:
        out["params"] = exc.params
    if exc.code == "sdk_missing" and backend.extra:
        from kiro_crew import extras

        if extras.pip_install_channel_available():
            # ``sdk_missing`` already means this system can have the package,
            # so the command names the pins alone, without their markers.
            specs = [s.split(";")[0].strip() for s in extras.requirements_for_extra(backend.extra)]
            command = extras.pip_install_command_for(*specs) if specs else ""
            if command:
                out["install_command"] = command
    return out


def backend_view(
    vault: SecretVault,
    backend: SecretBackend,
    taken: set[str],
    local: Optional[Callable[[str], bool]] = None,
) -> dict[str, Any]:
    """One backend's block of the listing. Blocking; never raises for a provider failure.

    *taken* is every name a reference would already answer from somewhere else
    (the local vault, an earlier backend); this backend's items of those names
    are marked ``shadowed`` and its other names are added to it.
    """
    view: dict[str, Any] = {
        "id": backend.id,
        "name": backend.display_name,
        "capabilities": asdict(backend.capabilities),
        "form": [asdict(f) for f in backend.connect_form],
        "fallback_form": [asdict(f) for f in backend.fallback_form],
        "fallback_label_key": backend.fallback_label_key,
        "fallback_first": False,
        "app_sign_in": backend.app_sign_in,
        "help_url": backend.help_url,
        "help_label_key": backend.help_label_key,
        "connected": False,
        "status": "not_connected",
    }
    try:
        conn = read_connection(vault, backend)
    except BackendError as exc:
        # Stored but unreadable: connected, so the panel offers Disconnect.
        view.update(connected=True, status="unavailable", error=error_view(backend, exc))
        return view
    if conn is not None:
        # A connection whose package went missing keeps its token: adding the
        # package back restores it without a new paste.
        view["connected"] = True
        view["account"] = conn.account
        view["connected_form"] = conn.form
    try:
        if conn is None:
            unavailable = backend.availability()
            if unavailable is not None:
                raise unavailable
            view["fallback_first"] = bool(backend.fallback_form) and backend.prefers_fallback()
            return view
        rows = _clean_items(call(backend, lambda: backend.list_items(conn.data)), backend)
    except BackendError as exc:
        view["status"] = "needs_approval" if exc.code == "approval_expired" else "unavailable"
        view["error"] = error_view(backend, exc)
        return view
    for row in rows:
        row["shadowed"] = (
            row["name"] in taken
            or (local is not None and local(row["name"]))
            or local_only(row["name"])
        )
    taken.update(row["name"] for row in rows)
    view["status"] = "connected"
    view["items"] = rows
    return view


def taken_locally(local_names: Iterable[str]) -> Callable[[str], bool]:
    """Whether a reference to a backend title is answered by the local vault.

    The local vault reads ``secret://NAME#FIELD`` with its NAME before the
    FIRST ``#``, so a local ``AWS`` answers ``secret://AWS#work#username`` too:
    a backend title ``AWS#work`` can never be reached and is listed as taken.
    """
    names = set(local_names)
    return lambda title: title in names or items.parse_reference(title)[0] in names


def backends_view(vault: SecretVault, local_names: Iterable[str]) -> list[dict[str, Any]]:
    """Every registered backend's block, in resolution order. Blocking."""
    taken = set(local_names)
    local = taken_locally(taken)
    return [backend_view(vault, backend, taken, local) for backend in registered()]


# ── Resolution ──


class ResolveFailure(Exception):
    """A backend that could not answer a resolve; the resolver fails closed."""

    def __init__(self, backend: SecretBackend, error: BackendError, names: list[str]) -> None:
        super().__init__(error.code)
        self.backend = backend
        self.error = error
        self.names = names


def resolve_missing(
    vault: SecretVault,
    names: Iterable[str],
    audit: Callable[[SecretBackend, list[str], str, str], None],
) -> dict[str, tuple[str, SecretValue]]:
    """Read *names* (missing from the local vault) from the connected backends, in order.

    Blocking. For a protected name nothing is asked, and with no connection no
    provider package is imported.
    A backend that fails raises :class:`ResolveFailure`; it never reads as
    missing. *audit(backend, names, outcome, code)* is called once per backend asked.
    """
    wanted = sorted({n for n in names if not local_only(n)})
    found: dict[str, tuple[str, SecretValue]] = {}
    if not wanted:
        return found
    for backend in registered():
        if not wanted:
            break
        asking = list(wanted)
        try:
            conn = read_connection(vault, backend)
            if conn is None:
                continue
            # An app sign-in read may ask the person to approve in the app, so it
            # waits as long as a connect does.
            wait = CONNECT_TIMEOUT_SECS if backend.app_sign_in and conn.form == "main" else None
            got = call(backend, lambda: backend.get_items_many(conn.data, asking), wait)
        except BackendError as exc:
            audit(backend, asking, "failed", exc.code)
            raise ResolveFailure(backend, exc, asking) from None
        audit(backend, asking, "ok", "")
        for name in asking:
            hit = got.get(name) if isinstance(got, dict) else None
            if (
                isinstance(hit, tuple)
                and len(hit) == 2
                and hit[0] in backend.capabilities.item_types
                and isinstance(hit[1], SecretValue)
            ):
                found[name] = hit
        wanted = [n for n in wanted if n not in found]
    return found
