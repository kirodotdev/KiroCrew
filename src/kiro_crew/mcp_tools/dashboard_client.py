"""The port a Kiro Crew MCP tool reaches the dashboard through.

Every MCP server Kiro Crew ships talks to the gateway the same way: an
authenticated loopback request to an ``/api`` route, answered with JSON. This
module names that dependency as a port, :class:`DashboardClient`, so a tool body
takes it as a parameter instead of importing ``mcp_core``'s request helpers by
name. Two adapters satisfy it:

* :class:`LoopbackDashboardClient`, production: each verb is the matching
  ``mcp_core`` helper (``_get``/``_post``/``_patch``/``_put``/``_delete``), looked
  up as an attribute when the request is made, so the secret handshake, the
  caller and session-token headers, the refused-connection retry and the
  error-body decoding all stay where they are.
* :class:`InMemoryDashboardClient`, tests: answers from a route table and
  records every request, so a tool is tested through its own interface rather
  than by patching the helpers it happens to call.

Both apply the same refusal rule, :func:`_accepted`: a reply that is a JSON
object carrying a truthy ``error`` is raised as :class:`DashboardError`, never
returned. A body reads a refusal's ``code`` off the exception; one whose
reply must be read code-first (a code that decides the outcome even beside no
``error``) takes ``DashboardError.body`` back and reads the reply itself.

:func:`restrict_routes` is the third piece: a client that refuses, before
anything is sent, a request whose route its tool did not declare. The tool
table hands each tool one, which is what makes a tool's declared routes the
complete list of what it can reach.
"""

from __future__ import annotations

import copy
import re
from collections.abc import Iterable, Mapping
from dataclasses import dataclass, field
from typing import Any, Protocol

# Imported at module scope, not per call: the dashboard server imported it at
# startup before this port existed, and ``mcp_core`` resolves the gateway port
# from config and the environment when it is imported. Every verb below still
# reads its helper as an attribute at call time.
from kiro_crew import mcp_core


class DashboardError(Exception):
    """The dashboard refused a request: its JSON reply carried a truthy ``error``.

    ``error`` is the reply's ``error`` value exactly as sent (usually a string,
    already redacted by ``mcp_core._http_error_body`` on the loopback path),
    ``code`` its machine-readable ``code`` or ``""``, and ``body`` the whole
    reply. There is no HTTP status: the loopback helpers fold an error status
    into this reply rather than report it.
    """

    def __init__(self, body: Mapping[str, Any]) -> None:
        self.body: dict[str, Any] = dict(body)
        self.error: Any = self.body.get("error")
        code = self.body.get("code")
        self.code: str = code if isinstance(code, str) else ""
        super().__init__(str(self.error))


class UndeclaredRoute(RuntimeError):
    """A tool asked for a route its row does not declare. Nothing was sent."""


class UnroutedRequest(AssertionError):
    """The in-memory dashboard has no answer for this request."""


class DashboardClient(Protocol):
    """Loopback access to the gateway's ``/api`` routes.

    Each verb returns the decoded JSON reply, or raises :class:`DashboardError`
    when the dashboard refused. ``session_key`` is the identity the request
    carries as ``X-Session-Key``: ``None`` lets the transport resolve the frame's
    attribution key itself, and a tool that has verified its caller strictly
    must pass the key it verified. ``timeout`` (seconds) is the transport's
    default when ``None``.
    """

    def get(
        self, path: str, *, session_key: str | None = None, timeout: float | None = None
    ) -> Any: ...

    def post(
        self,
        path: str,
        body: dict[str, Any] | None = None,
        *,
        session_key: str | None = None,
        timeout: float | None = None,
    ) -> Any: ...

    def patch(
        self, path: str, body: dict[str, Any] | None = None, *, session_key: str | None = None
    ) -> Any: ...

    def put(
        self, path: str, body: dict[str, Any] | None = None, *, session_key: str | None = None
    ) -> Any: ...

    def delete(
        self, path: str, body: dict[str, Any] | None = None, *, session_key: str | None = None
    ) -> Any: ...


def _accepted(reply: Any) -> Any:
    """``reply`` unchanged, or :class:`DashboardError` when it is a refusal."""
    if isinstance(reply, dict) and reply.get("error"):
        raise DashboardError(reply)
    return reply


class LoopbackDashboardClient:
    """Production adapter: ``mcp_core``'s request helpers, read at call time."""

    def get(
        self, path: str, *, session_key: str | None = None, timeout: float | None = None
    ) -> Any:
        if timeout is None:
            return _accepted(mcp_core._get(path, session_key))
        return _accepted(mcp_core._get(path, session_key, timeout=timeout))

    def post(
        self,
        path: str,
        body: dict[str, Any] | None = None,
        *,
        session_key: str | None = None,
        timeout: float | None = None,
    ) -> Any:
        if timeout is None:
            return _accepted(mcp_core._post(path, body, session_key=session_key))
        return _accepted(mcp_core._post(path, body, timeout=timeout, session_key=session_key))

    def patch(
        self, path: str, body: dict[str, Any] | None = None, *, session_key: str | None = None
    ) -> Any:
        return _accepted(mcp_core._patch(path, body, session_key=session_key))

    def put(
        self, path: str, body: dict[str, Any] | None = None, *, session_key: str | None = None
    ) -> Any:
        return _accepted(mcp_core._put(path, body, session_key=session_key))

    def delete(
        self, path: str, body: dict[str, Any] | None = None, *, session_key: str | None = None
    ) -> Any:
        return _accepted(mcp_core._delete(path, body, session_key=session_key))


@dataclass(frozen=True)
class DashboardRequest:
    """One request the in-memory dashboard received."""

    method: str
    path: str
    body: Any = None
    session_key: str | None = None
    timeout: float | None = None

    @property
    def route(self) -> str:
        """``"METHOD /path"`` without the query string."""
        return f"{self.method} {self.path.split('?', 1)[0]}"


#: A route answer: a JSON value, an exception to raise, or a function of the
#: request returning either.
Reply = Any


def _route_pattern(route: str) -> tuple[str, re.Pattern[str]]:
    """``"METHOD /api/a/{x}/b"`` -> (method, regex over the path without its query).

    A ``{name}`` placeholder stands for a non-empty run of characters. It may
    span ``/``: an id the store holds is interpolated as-is in some paths, and a
    declared route must never refuse a request the tool body builds today.
    """
    method, _, template = route.strip().partition(" ")
    parts = re.split(r"\{[^{}]*\}", template)
    return method.upper(), re.compile(".+".join(re.escape(p) for p in parts))


def _route_matches(pattern: tuple[str, re.Pattern[str]], method: str, path: str) -> bool:
    return pattern[0] == method and pattern[1].fullmatch(path.split("?", 1)[0]) is not None


@dataclass
class InMemoryDashboardClient:
    """Test adapter: answers from ``routes`` and records every request.

    ``routes`` maps ``"METHOD /path"`` (``{name}`` placeholders allowed, no query
    string) to a :data:`Reply`. The first matching route answers, in insertion
    order. A JSON reply is deep-copied, as a fresh wire body would be, and then
    passes the same refusal rule as the production adapter, so a route answering
    ``{"error": "...", "code": "..."}`` raises :class:`DashboardError` in the
    tool. A request no route matches raises :class:`UnroutedRequest`.
    """

    routes: Mapping[str, Reply] = field(default_factory=dict)
    requests: list[DashboardRequest] = field(default_factory=list)

    def __post_init__(self) -> None:
        self._compiled = [(_route_pattern(key), reply) for key, reply in self.routes.items()]

    def _answer(self, request: DashboardRequest) -> Any:
        self.requests.append(request)
        for pattern, reply in self._compiled:
            if _route_matches(pattern, request.method, request.path):
                if callable(reply):
                    reply = reply(request)
                if isinstance(reply, BaseException):
                    raise reply
                return _accepted(copy.deepcopy(reply))
        raise UnroutedRequest(f"no in-memory route answers {request.route}")

    def get(
        self, path: str, *, session_key: str | None = None, timeout: float | None = None
    ) -> Any:
        return self._answer(DashboardRequest("GET", path, None, session_key, timeout))

    def post(
        self,
        path: str,
        body: dict[str, Any] | None = None,
        *,
        session_key: str | None = None,
        timeout: float | None = None,
    ) -> Any:
        return self._answer(
            DashboardRequest("POST", path, copy.deepcopy(body), session_key, timeout)
        )

    def patch(
        self, path: str, body: dict[str, Any] | None = None, *, session_key: str | None = None
    ) -> Any:
        return self._answer(DashboardRequest("PATCH", path, copy.deepcopy(body), session_key))

    def put(
        self, path: str, body: dict[str, Any] | None = None, *, session_key: str | None = None
    ) -> Any:
        return self._answer(DashboardRequest("PUT", path, copy.deepcopy(body), session_key))

    def delete(
        self, path: str, body: dict[str, Any] | None = None, *, session_key: str | None = None
    ) -> Any:
        return self._answer(DashboardRequest("DELETE", path, copy.deepcopy(body), session_key))

    def sent(self, route: str) -> list[DashboardRequest]:
        """The recorded requests matching ``route`` (same syntax as ``routes``)."""
        pattern = _route_pattern(route)
        return [r for r in self.requests if _route_matches(pattern, r.method, r.path)]


class _RouteScopedClient:
    """``inner``, refusing any request whose route is not in ``routes``."""

    def __init__(self, inner: DashboardClient, routes: Iterable[str]) -> None:
        self._inner = inner
        self._patterns = [_route_pattern(r) for r in routes]

    def _check(self, method: str, path: str) -> None:
        if not any(_route_matches(p, method, path) for p in self._patterns):
            raise UndeclaredRoute(
                f"{method} {path.split('?', 1)[0]} is not a route this tool declares"
            )

    def get(
        self, path: str, *, session_key: str | None = None, timeout: float | None = None
    ) -> Any:
        self._check("GET", path)
        return self._inner.get(path, session_key=session_key, timeout=timeout)

    def post(
        self,
        path: str,
        body: dict[str, Any] | None = None,
        *,
        session_key: str | None = None,
        timeout: float | None = None,
    ) -> Any:
        self._check("POST", path)
        return self._inner.post(path, body, session_key=session_key, timeout=timeout)

    def patch(
        self, path: str, body: dict[str, Any] | None = None, *, session_key: str | None = None
    ) -> Any:
        self._check("PATCH", path)
        return self._inner.patch(path, body, session_key=session_key)

    def put(
        self, path: str, body: dict[str, Any] | None = None, *, session_key: str | None = None
    ) -> Any:
        self._check("PUT", path)
        return self._inner.put(path, body, session_key=session_key)

    def delete(
        self, path: str, body: dict[str, Any] | None = None, *, session_key: str | None = None
    ) -> Any:
        self._check("DELETE", path)
        return self._inner.delete(path, body, session_key=session_key)


def restrict_routes(client: DashboardClient, routes: Iterable[str]) -> DashboardClient:
    """``client`` limited to ``routes`` (``"METHOD /path"``, ``{name}`` placeholders).

    A request outside them raises :class:`UndeclaredRoute` before it reaches the
    transport, so nothing is sent for it.
    """
    return _RouteScopedClient(client, routes)
