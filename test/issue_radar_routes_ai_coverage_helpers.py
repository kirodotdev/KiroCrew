"""The request builders and patch helpers the Issue Radar AI-route tests share.

``test_issue_radar_routes_ai_coverage.py`` and its ``_language``,
``_recommendations_and_tagging`` and ``_lists_and_investigation`` siblings import what
they use from here by name: ``LABELS``, the label set the test repository defines;
``_key``, a provider repository key; ``_get`` and ``_json_request``, mocked requests on
an app carrying a dashboard ``state`` stand-in, where an owner-gated write gets an
unconfigured owner and ``None`` models a malformed payload; ``_body``, a response's
decoded JSON; ``_connected``, ``_writable`` and ``_audits``, which patch the
connected-repository gate, the write gate and the SEL audit; ``_sessions``, a dashboard
``state`` stand-in whose session manager records release and destroy; and ``_stream`` and
``_oneshot``, which patch the one model call every AI route funnels through and
``_run_oneshot_model`` itself.
Nothing here runs a provider subprocess, calls a model or touches the network.
"""

import json
from types import SimpleNamespace
from unittest import mock
from unittest.mock import AsyncMock, MagicMock
from urllib.parse import urlencode

from aiohttp import web
from aiohttp.test_utils import make_mocked_request
from dashboard_owner_helpers import NoConfiguredOwner

from kiro_crew import llm_helpers
from kiro_crew.apps.builtins.issue_radar.backend import provider, routes, store

BASE = "/api/apps/issue-radar"

LABELS = [
    {"name": "bug", "color": "ee0000", "description": "a defect"},
    {"name": "docs", "color": "0000ee", "description": ""},
    {"name": "question", "color": "00ee00", "description": ""},
    {"name": "enhancement", "color": "cccccc", "description": ""},
    {"name": "good first issue", "color": "111111", "description": ""},
    {"name": "help wanted", "color": "222222", "description": ""},
    {"name": "wontfix", "color": "333333", "description": ""},
]


def _key(owner: str = "o", repo: str = "r") -> provider.RepoKey:
    return provider.key_from_parts(owner, repo)


def _app(state: object | None = None) -> web.Application:
    """An aiohttp app carrying a dashboard ``state`` stand-in (or none at all).

    The AI helpers read ``request.app.get("state")`` and raise when it is absent,
    which is the branch a gateway that has not finished booting would take.
    """
    app = web.Application()
    if state is not None:
        app["state"] = state
    return app


def _get(path: str, query: dict | None = None, state: object | None = None) -> web.Request:
    full = f"{BASE}/{path}"
    if query:
        full = f"{full}?{urlencode(query)}"
    return make_mocked_request("GET", full, app=_app(state))


#: Forge writes behind the dashboard-owner gate; their requests carry owner state.
_OWNER_GATED_WRITES = frozenset(
    {
        "labels/apply",
        "labels/apply-bulk",
        "labels/create",
        "issue/state",
        "issue/assignees",
        "investigation",
        "tagging",
        "recommendations",
    }
)


def _json_request(method: str, path: str, body: object, state: object | None = None) -> web.Request:
    """A request whose ``.json()`` resolves to ``body`` (or raises, for ``None``).

    ``None`` models a malformed payload: ``request.json()`` raising is exactly what
    each handler's ``except Exception -> 400`` branch is written for.
    """
    if state is None and path in _OWNER_GATED_WRITES:
        state = NoConfiguredOwner()
    req = make_mocked_request(method, f"{BASE}/{path}", app=_app(state))
    req["user"] = "local-app"
    req["app"] = ""
    if body is None:
        req.json = AsyncMock(side_effect=ValueError("not json"))  # type: ignore[method-assign]
    else:
        req.json = AsyncMock(return_value=body)  # type: ignore[method-assign]
    return req


def _body(response: web.Response) -> dict:
    raw = response.body
    assert isinstance(raw, bytes)
    return json.loads(raw.decode("utf-8"))


def _connected(value: bool = True):
    return mock.patch.object(store, "is_repo_connected", return_value=value)


def _writable(value: bool | None = True):
    return mock.patch.object(routes, "_repo_can_write", return_value=value)


def _sessions(text: str = "{}", *, release_exc=None, destroy_exc=None) -> SimpleNamespace:
    """A ``state`` stand-in whose session manager records release/destroy.

    ``release``/``destroy`` are the two calls the AI paths wrap in their own
    ``try/except``: a failure there must not turn a good summary into a 502, so the
    tests drive both the clean and the raising shape.
    """
    sessions = SimpleNamespace(
        get_or_create=AsyncMock(return_value=(MagicMock(name="provider"), True, False)),
        release=MagicMock(side_effect=release_exc),
        destroy=AsyncMock(side_effect=destroy_exc),
    )
    return SimpleNamespace(sessions=sessions, _text=text)


def _stream(text: str, *, raises: BaseException | None = None):
    """Patch the ONE model call every AI route funnels through."""
    if raises is not None:
        return mock.patch.object(
            llm_helpers, "stream_and_collect", new=AsyncMock(side_effect=raises)
        )
    return mock.patch.object(llm_helpers, "stream_and_collect", new=AsyncMock(return_value=text))


def _oneshot(text: str = "{}", *, raises: BaseException | None = None):
    """Patch ``_run_oneshot_model`` itself, for tests about a CALLER's validation."""
    if raises is not None:
        return mock.patch.object(routes, "_run_oneshot_model", new=AsyncMock(side_effect=raises))
    return mock.patch.object(routes, "_run_oneshot_model", new=AsyncMock(return_value=text))


def _audits():
    """Collect SEL audit calls instead of writing a real security event log."""
    return mock.patch.object(routes, "_audit", new=MagicMock())
