"""Hand an MCP stdio server's audit records to the gateway when its trust root is withheld.

``sel.sel`` returns a :class:`ForwardingLog` in a Kiro Crew MCP stdio server whose
harness withholds ``trust/`` (an enforcing harness's credential mask, on purpose: the
key must not be readable or forgeable from inside). Each record goes to the gateway
over the agent socket with this process's capability, and the gateway signs and
appends it (``dashboard.handlers.ipc_sel``). The gateway stamps the actor from the
capability, so this side sends no session, caller or source.

Fails closed: no capability, a refusal or a transport error raises
``PermissionError`` naming the trust root. A caller that lets that propagate refuses
the tool call; the audit sites that already swallow SEL errors on main (``_sel_audit``
and the guarded policy writes) log it and lose that record, as they do on main. Only the two kinds an MCP server writes on its
tool-call path are forwarded; any other SEL method raises the same way.
"""

from __future__ import annotations

import errno
import json
import urllib.error
import urllib.request
from typing import Any, NoReturn

#: The route the gateway serves the records on (``dashboard.handlers.ipc_sel.PATH``).
PATH = "/api/ipc/sel"
_TIMEOUT_SECS = 10.0
#: Per kind, the fields the gateway stamps itself and this side must not send.
_ACTOR_FIELDS = {
    "log_tool_invocation": ("session_key", "agent", "source"),
    "log_api_access": ("caller", "source", "forwarded"),
}
#: Per kind, the fields the route accepts (``dashboard.handlers.ipc_sel._ALLOWED``;
#: a test holds the two equal). Any other keyword is refused before it is encoded.
_ROUTE_FIELDS = {
    "log_tool_invocation": frozenset(
        {
            "tool_name",
            "outcome",
            "tool_kind",
            "request_id",
            "downstream_service",
            "resources",
            "error",
            "metadata",
            "critical",
        }
    ),
    "log_api_access": frozenset({"operation", "outcome", "resources", "error", "critical"}),
}


#: The route's caps (``dashboard.handlers.ipc_sel`` and ``apps.sel_routes``).
_MAX_FIELD = 500
_MAX_METADATA_KEYS = 16
_MAX_METADATA_KEY = 64
#: Encoded body budget: the route refuses a body over 16 KiB (``_MAX_BODY``); this
#: leaves room for the envelope.
_BODY_BUDGET = 16 * 1024 - 512


def _clip(value: Any, limit: int = _MAX_FIELD) -> Any:
    return value[:limit] if isinstance(value, str) else value


def _clipped(redacted: dict[str, Any]) -> dict[str, Any]:
    """*redacted* (``sel.forwardable_fields``'s output) within the route's per-field
    caps, so length alone never gets a record refused. Only already-redacted text is
    cut."""
    out: dict[str, Any] = {}
    for key, value in redacted.items():
        if key == "metadata" and isinstance(value, dict):
            meta: dict[str, Any] = {}
            # Keys and values are already redacted text or primitives (sel turned every
            # other value into redacted text); nothing here creates new text.
            for mkey, mvalue in list(value.items())[:_MAX_METADATA_KEYS]:
                meta[_clip(mkey, _MAX_METADATA_KEY)] = _clip(mvalue)
            out[key] = meta
        else:
            out[key] = _clip(value)
    return out


def _encoded(method: str, kwargs: dict[str, Any]) -> bytes:
    return json.dumps({"method": method, "kwargs": kwargs}, default=str).encode()


def _within_body_budget(method: str, kwargs: dict[str, Any]) -> bytes:
    """The encoded body, with the longest strings halved until it fits the route's cap.

    The caps above count characters, but JSON escapes a quote or backslash to 2 bytes
    and a non-ASCII character to 6 or 12, so a record of capped fields can still be
    over the body cap. Only already-redacted text is shortened, so nothing is exposed.
    """
    body = _encoded(method, kwargs)
    while len(body) > _BODY_BUDGET:
        slots: list[tuple[int, dict[str, Any], str]] = []
        for key, value in kwargs.items():
            if isinstance(value, str) and value:
                slots.append((len(json.dumps(value)), kwargs, key))
            elif isinstance(value, dict):
                slots.extend(
                    (len(json.dumps(v)), value, k)
                    for k, v in value.items()
                    if isinstance(v, str) and v
                )
        if not slots:
            break
        _size, holder, key = max(slots, key=lambda slot: slot[0])
        holder[key] = holder[key][: len(holder[key]) // 2]
        body = _encoded(method, kwargs)
    return body


class ForwardingLog:
    """Stands in for the SEL in an MCP stdio server whose trust root is withheld."""

    def __init__(self, trust_dir: str) -> None:
        self._trust_dir = trust_dir

    def log_tool_invocation(self, **fields: Any) -> None:
        self._forward("log_tool_invocation", fields)

    def log_api_access(self, **fields: Any) -> None:
        self._forward("log_api_access", fields)

    def __getattr__(self, name: str) -> Any:
        if name.startswith("_"):
            raise AttributeError(name)

        def _unforwarded(*_args: object, **_kwargs: object) -> NoReturn:
            self._refuse(f"{name} is not forwarded to the gateway")

        return _unforwarded

    def _refuse(self, reason: str) -> NoReturn:
        raise PermissionError(
            errno.EACCES,
            f"the SEL trust root is withheld from this MCP server and its audit record "
            f"could not be handed to the gateway ({reason}); the call is refused",
            self._trust_dir,
        )

    def _forward(self, method: str, fields: dict[str, Any]) -> None:
        from kiro_crew.agent_ipc_client import (
            capability_session_key,
            current_capability,
            open_with_capability,
        )
        from kiro_crew.port_resolution import resolve_client_port_src
        from kiro_crew.session_token_sig import session_token_header

        try:
            from kiro_crew.sel import forwardable_fields

            # The registered SEL sink redacts the full record; this side only clips it.
            kept = {k: v for k, v in fields.items() if k not in _ACTOR_FIELDS[method]}
            if not set(kept) <= _ROUTE_FIELDS[method]:
                raise TypeError("a field the route does not accept")
            kwargs = _clipped(forwardable_fields(kept))
            body = _within_body_budget(method, kwargs)
        except Exception as exc:  # noqa: BLE001 - a record that cannot be prepared is not sent
            self._refuse(f"the record could not be prepared: {type(exc).__name__}")
        try:
            capability, socket_path = current_capability(lambda: resolve_client_port_src(None)[0])
        except Exception as exc:  # noqa: BLE001 - any failure here is "no channel"
            self._refuse(f"no agent-socket capability: {type(exc).__name__}")
        if not capability:
            self._refuse("no agent-socket capability")
        session = capability_session_key()
        if not session:
            self._refuse("the gateway named no session for this capability")
        req = urllib.request.Request(
            f"http://127.0.0.1{PATH}",
            data=body,
            # The token as well as the key: on a shared (multi-session) runtime the
            # gateway names the caller only behind a verified session token.
            headers={
                "Content-Type": "application/json",
                "X-Session-Key": session,
                **session_token_header(),
            },
            method="POST",
        )
        try:
            with open_with_capability(
                req, _TIMEOUT_SECS, capability=capability, socket_path=socket_path
            ) as resp:
                status = resp.status
        except urllib.error.HTTPError as exc:
            self._refuse(f"the gateway refused the record (HTTP {exc.code})")
        except Exception as exc:  # noqa: BLE001 - transport errors all mean "not written"
            self._refuse(f"transport error: {type(exc).__name__}")
        if status != 204:
            self._refuse(f"the gateway answered HTTP {status}")
