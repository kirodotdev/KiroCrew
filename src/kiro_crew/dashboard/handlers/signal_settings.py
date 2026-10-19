"""The Signal settings API: ``GET``/``PUT /api/signal/config``.

Like iMessage, the channel has NO credential to manage: signal-cli keeps the
account's keys in its own data directory, registered or linked out of band, so
there is nothing to mask, verify against a vendor, or write to ``.env``. The
panel edits the ``signal`` section of config.json and nothing else.

Two rules shape the save:

* **Identifiers stay strings with their ``+``.** ``account`` and every
  ``allowed_numbers`` entry is stored as the canonical text the transport
  compares against (``normalize_identifier``: formatting stripped, a leading
  ``00`` read as ``+``), never as a number, so a JSON round trip cannot drop the
  ``+`` and silently un-authorize the owner. A malformed entry is refused here
  rather than stored and then dropped, with a boot warning, by the transport.
* **The binary is not settable.** Which ``signal-cli`` runs is decided by
  ``signal_cli.bridge_path`` (``KIROCREW_SIGNAL_CLI`` or a fixed list), never by
  config.json, which an agent can write. The save accepts a closed set of
  fields and refuses every other key, so no spelling of a path field can be
  smuggled in; GET only reports WHETHER a binary resolved.

Applying a save needs no gateway restart: the config watcher hands a changed
``allowed_numbers`` to ``SignalTransport.reconfigure`` live, and a change to a
boot key (``enabled``, ``account``) makes the channel registry restart the
channel in process.

A standalone module rather than a ``messaging_api`` owner: an owner is composed
onto ``handlers.messaging``'s globals, which would grow that baselined file for
every name this module reads.
"""

from __future__ import annotations

import asyncio
import functools
from typing import Any

from aiohttp import web

from kiro_crew.config.loader import (
    ConfigReadError,
    KiroCrewConfig,
    config_path,
    update_config_locked,
)
from kiro_crew.dashboard.channel_folders import (
    channel_restart_required,
    clean_session_folder,
    ensure_channel_folder,
    stored_folder_name,
)
from kiro_crew.dashboard.chat_utils import run_to_completion
from kiro_crew.dashboard.handlers.core import _hot_apply_after_write
from kiro_crew.dashboard.origin import is_direct_local_request

# Only the dependency-free identity module at import time. ``handlers/__init__``
# imports this module on the gateway boot path, and the channel is opt-in, so the
# runtime (``bridge_path``, ``transport`` and everything ``transport`` loads) is
# imported inside the handlers that use it (`no-new-work-on-gateway-boot-path`,
# clause 5; pinned by ``test_signal_boot_path.py``).
from kiro_crew.signal_cli.identifiers import (
    account_identifier,
    coerce_identifier,
    identifier_problem,
    normalize_identifier,
)

#: Every field the save accepts. Anything else is refused, which is what keeps
#: the executable out of reach: there is no path field to spell.
SAVE_FIELDS = frozenset({"enabled", "account", "allowed_numbers", "session_folder"})

#: An allow-list longer than this is a paste accident, not a roster.
MAX_ALLOWED_NUMBERS = 256

_E164_HINT = "a +E.164 phone number such as +447700900123"


def _sel() -> Any:
    """Late-bound so a test patching ``kiro_crew.dashboard.handlers.sel`` reaches it."""
    import kiro_crew.dashboard.handlers as _pkg

    return _pkg.sel()


def _clean_identifier(raw: object, *, phone_only: bool, label: str) -> str:
    """One Signal identifier as the canonical string the transport compares.

    Raises ``ValueError`` when the entry is neither a ``+E.164`` number nor,
    where allowed, an ACI UUID. The message names the entry by ``label`` and
    never echoes the value: a number is personal data, and the response and
    the SEL denial both carry this text.
    """
    if raw is None or isinstance(raw, (bool, float)) or not isinstance(raw, (str, int)):
        raise ValueError(f"{label}: expected {_E164_HINT}")
    canonical = normalize_identifier(coerce_identifier(raw))
    if identifier_problem(raw) or not canonical:
        raise ValueError(f"{label} is invalid: expected {_E164_HINT}")
    if phone_only and not canonical.startswith("+"):
        # signal-cli's `-a` takes the account's phone number; an ACI names a
        # contact, never the account the child runs as.
        raise ValueError(f"the account must be {_E164_HINT}")
    return canonical


def _clean_account(raw: object) -> str:
    """``""`` clears the account; anything else must be a phone number."""
    if raw is None or (isinstance(raw, str) and not raw.strip()):
        return ""
    return _clean_identifier(raw, phone_only=True, label="the account")


def _clean_allowed_numbers(raw: object) -> list[str]:
    """The allow-list, canonicalized, blanks and duplicates dropped, order kept."""
    if not isinstance(raw, list):
        raise ValueError("allowed_numbers must be a list")
    if len(raw) > MAX_ALLOWED_NUMBERS:
        raise ValueError(f"allowed_numbers holds at most {MAX_ALLOWED_NUMBERS} entries")
    out: list[str] = []
    for position, item in enumerate(raw, start=1):
        if isinstance(item, str) and not item.strip():
            continue
        canonical = _clean_identifier(item, phone_only=False, label=f"allowed number {position}")
        if canonical not in out:
            out.append(canonical)
    return out


def _save_warning(section: dict) -> str:
    """Advisory text for a save that stored a channel which cannot work yet."""
    if not section.get("enabled"):
        return ""
    if not str(section.get("account", "") or ""):
        return "the channel is enabled but no account is set, so it will not start"
    if not section.get("allowed_numbers"):
        return "the allow-list is empty, so the channel will answer nobody"
    return ""


def _operator_mode(state: Any, sig: Any) -> str:
    """Who the operator is for the saved section: :func:`operator_mode`'s answer.

    The same rule ``SignalTransport.is_operator`` applies, over the saved
    allow-list and the configured account. While the channel runs for that
    same account, the identities its client has learned (the account's ACI)
    count too, so an allow-list naming the account by ACI reads as the
    account, exactly as the gateway reads it.
    """
    from kiro_crew.signal_cli.transport import allowed_numbers_from_config, operator_mode

    account = account_identifier(getattr(sig, "account", ""))
    self_ids = {account} if account else set()
    transports = getattr(state, "channel_transports", None)
    transport = transports.get("signal") if isinstance(transports, dict) else None
    client = getattr(transport, "client", None)
    if account and client is not None and client.is_self(account):
        self_ids |= set(client.self_ids())
    allowed = allowed_numbers_from_config(getattr(sig, "allowed_numbers", None)) or []
    return operator_mode(allowed, self_ids)


async def api_signal_config_get(request: web.Request) -> web.Response:
    """GET /api/signal/config: connection status plus the editable fields."""

    def _read() -> tuple[Any, bool]:
        # One thread hop for both filesystem reads: the config, and the probe of
        # the fixed install locations for a usable signal-cli.
        from kiro_crew.signal_cli.bridge_path import resolve_bridge_path

        return KiroCrewConfig.load().signal, bool(resolve_bridge_path())

    sig, cli_found = await asyncio.to_thread(_read)
    state = request.app["state"]
    return web.json_response(
        {
            # True only while signal-cli is actually up this session -- kept
            # truthful by the client's state observer (see maybe_start_signal).
            "connected": bool(getattr(state, "signal_connected", False)),
            "connect_error": str(getattr(state, "signal_connect_error", ""))[:120],
            # The account is required to launch signal-cli and the allow-list
            # is deny-by-default, so all three make a working channel.
            "configured": bool(sig.enabled and sig.account and sig.allowed_numbers),
            # Remote sessions get a read-only view: config edits (PUT) are
            # loopback-only, so the UI disables all inputs and hides Save.
            "read_only": not is_direct_local_request(request),
            # Whether a signal-cli executable resolved, never WHERE: the path is
            # not a setting, so the panel only says whether one was found.
            "cli_found": cli_found,
            "enabled": bool(sig.enabled),
            "account": str(sig.account or ""),
            "allowed_numbers": [str(n) for n in sig.allowed_numbers],
            "session_folder": sig.session_folder,
            # Who may approve tools and run /stop: "account", "sole_entry" or
            # "none" (see ``signal_cli.transport.operator_mode``). The panel
            # warns on "none" with more than one allowed number.
            "operator_mode": _operator_mode(state, sig),
        }
    )


async def api_signal_config_save(request: web.Request) -> web.Response:
    """PUT /api/signal/config: persist the ``signal`` section (config.json).

    A save is a locked read-modify-write: a cancelled request (client gone,
    gateway shutting down) must not abandon it halfway -- see
    ``run_to_completion``.
    """
    return await run_to_completion(_signal_config_save(request))


async def _signal_config_save(request: web.Request) -> web.Response:
    """Body of the Signal save; runs to completion once started."""
    caller = request.get("user", "dashboard")

    def _audit_denial(msg: str) -> None:
        _sel().log_api_access(
            caller=caller,
            operation="signal.config.update",
            outcome="denied",
            source="dashboard",
            error=msg,
        )

    def _deny(code: str, msg: str) -> web.Response:
        """Reject a bad request. ``code`` is the contract, ``msg`` is advisory."""
        _audit_denial(msg)
        return web.json_response({"error": msg, "code": code}, status=400)

    # Remote sessions are read-only (the gate every channel config API uses): a
    # remote or tunneled session cannot widen who may reach the agent.
    if not is_direct_local_request(request):
        message = "read-only from remote sessions (local machine only)"
        _audit_denial(message)
        return web.json_response({"error": message, "code": "remote_read_only"}, status=403)

    try:
        body = await request.json()
    except Exception:
        return _deny("invalid_json", "invalid JSON")
    if not isinstance(body, dict):
        return _deny("body_not_object", "body must be an object")

    unknown = sorted(str(k) for k in body if k not in SAVE_FIELDS)
    if unknown:
        return _deny(
            "unknown_field",
            f"not a Signal setting: {', '.join(unknown)[:120]} "
            "(the signal-cli binary is chosen by KIROCREW_SIGNAL_CLI, never here)",
        )

    # ── Phase 1: validate everything and stage changes (no partial writes).
    staged: dict[str, object] = {}
    if "enabled" in body:
        val = body.get("enabled")
        if not isinstance(val, bool):
            return _deny("enabled_not_bool", "enabled must be a boolean")
        staged["enabled"] = val
    if "account" in body:
        try:
            staged["account"] = _clean_account(body.get("account"))
        except ValueError as exc:
            return _deny("invalid_account", str(exc))
    if "allowed_numbers" in body:
        try:
            staged["allowed_numbers"] = _clean_allowed_numbers(body.get("allowed_numbers"))
        except ValueError as exc:
            return _deny("invalid_number", str(exc))
    if "session_folder" in body:
        try:
            staged["session_folder"] = clean_session_folder(body.get("session_folder"))
        except ValueError as exc:
            return _deny("invalid_session_folder", str(exc))

    # ── Phase 2: commit, entirely under both config locks: the in-process one
    # serializes the dashboard's writers, and ``update_config_locked`` holds the
    # ``<path>.lock`` sidecar across the read-modify-write so a concurrent
    # ``kirocrew config set`` in another process cannot land in between.
    from kiro_crew.dashboard.handlers.agents import _get_config_lock

    applied: list[str] = []
    stored: dict[str, Any] = {}

    def _apply_staged(fresh: dict) -> dict | None:
        """Merge the staged fields into the config read inside the lock.

        Returns ``None`` when nothing changed, so a no-op save leaves
        config.json untouched and ``restart_required`` stays truthful.
        """
        nonlocal applied
        if not isinstance(fresh.get("signal"), dict):
            fresh["signal"] = {}
        section = fresh["signal"]
        defaults: dict[str, object] = {
            "enabled": False,
            "account": "",
            "allowed_numbers": [],
            "session_folder": "",
        }
        changes = {k: v for k, v in staged.items() if v != section.get(k, defaults[k])}
        applied = sorted(changes)
        section.update(changes)
        # Read back AFTER the merge: the folder and the warning below describe
        # what was committed, not a pre-merge snapshot.
        stored.update(section)
        return fresh if changes else None

    async with _get_config_lock():
        path = config_path()
        write: asyncio.Task[dict] = asyncio.ensure_future(
            asyncio.to_thread(functools.partial(update_config_locked, path, mutate=_apply_staged))
        )
        try:
            await asyncio.shield(write)
        except asyncio.CancelledError:
            # Drain so the lock outlives the worker still replacing the file.
            await asyncio.gather(write, return_exceptions=True)
            raise
        except ConfigReadError:
            message = "config.json is corrupt"
            _audit_denial(message)
            return web.json_response({"error": message, "code": "config_corrupt"}, status=500)

        # Create the configured session folder now, on this user-initiated save,
        # so the reconcile path never has to write the folder store.
        folder_name = stored_folder_name(stored.get("session_folder"))
        if folder_name:
            state = request.app.get("state")
            if state is not None:
                await ensure_channel_folder(
                    state,
                    "signal",
                    folder_name,
                    relabel="session_folder" in applied,
                )

    _sel().log_api_access(
        caller=caller,
        operation="signal.config.update",
        outcome="ok",
        source="dashboard",
        resources=",".join(applied),
    )
    # Answer only once the watcher has applied the write: a narrowed allow-list
    # is in force before the caller sees "saved", not one poll interval later.
    await _hot_apply_after_write()
    return web.json_response(
        {
            "ok": True,
            "restart_required": channel_restart_required("signal", applied),
            "verify_warning": _save_warning(stored),
        }
    )
