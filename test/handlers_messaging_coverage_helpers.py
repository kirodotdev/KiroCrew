"""Request double and response helpers shared by the messaging-handler coverage tests
(``test_handlers_messaging_coverage*.py``).

``_Req`` presents the owner's own dashboard subject and an empty app claim unless a test
overrides ``"user"`` or ``"app"`` through ``extra=``; ``_state`` is a DashboardState double,
``_run`` drives one coroutine handler, ``_payload`` decodes its JSON response, and
``_BAD_JSON`` is the body that makes the double's ``json()`` raise.
"""

from __future__ import annotations

import asyncio
import json
from typing import Any
from unittest.mock import MagicMock

from aiohttp import web

#: The subject every request double presents, and the id ``_state`` reports as its
#: owner. These suites exercise body validation and response shape, not
#: authorization, so the caller they model is the owner's own dashboard session --
#: the one the owner gate admits. A test that means to model somebody else passes
#: its own ``extra={"user": ...}``.
_OWNER_SUBJECT = "U0OWNER0000"


class _Req:
    """Request double: state, JSON body, route/query fields and headers."""

    def __init__(
        self,
        state: Any = None,
        body: Any = None,
        *,
        match_info: dict[str, str] | None = None,
        query: dict[str, str] | None = None,
        remote: str = "127.0.0.1",
        extra: dict[str, Any] | None = None,
    ) -> None:
        self.app: dict[str, Any] = {"state": state}
        self._body = body
        self.match_info = match_info or {}
        self.query = query or {}
        self.headers: dict[str, str] = {}
        self.remote = remote
        self._extra = {"app": "", "user": _OWNER_SUBJECT, **(extra or {})}

    def __contains__(self, key: str) -> bool:
        return key in self._extra

    def __getitem__(self, key: str) -> Any:
        # The owner predicate reads ``request["app"]`` directly after testing
        # membership, so the double needs the read as well as the ``in``.
        return self._extra[key]

    async def json(self) -> Any:
        if isinstance(self._body, BaseException):
            raise self._body
        return self._body

    def get(self, key: str, default: Any = None) -> Any:
        return self._extra.get(key, default)


_BAD_JSON = ValueError("not json")


def _run(handler: Any, req: _Req) -> web.Response:
    """Drive one coroutine handler to completion and return its response."""
    return asyncio.run(handler(req))


def _payload(resp: web.Response) -> Any:
    body = resp.body
    assert isinstance(body, (bytes, bytearray))
    return json.loads(body)


def _state(**kw: Any) -> Any:
    """A DashboardState double with the JSON-serializable fields pinned."""
    state = MagicMock()
    state.owner_id = _OWNER_SUBJECT
    state.subagents = None
    state.slack_client = None
    state._native_cards = {}
    state._notification_log = []
    state._unread_count = 0
    state.ws_client_count.return_value = 0
    for key, val in kw.items():
        setattr(state, key, val)
    return state
