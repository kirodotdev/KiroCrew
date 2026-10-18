"""Shared fixtures for the Issue Radar crew route tests (``test_issue_radar_crew_routes*.py``).

``_CrewRouteCase`` is the base every test class extends: a temp data root behind one
``routes._scope`` patch, an enabled app, one connected repo and an isolated crew log. Beside
it: the route prefix and the repo identity, the registered-handler lookup ``_registered``,
the request builder ``_request``, the response decoder ``_payload``, the slot, session and
state fakes, and ``_tick``.
"""

import json
import os
import tempfile
import time
import unittest
import warnings
from pathlib import Path
from unittest import mock
from unittest.mock import AsyncMock

from aiohttp import web
from aiohttp.test_utils import make_mocked_request
from dashboard_owner_helpers import NoConfiguredOwner
from off_loop_helpers import off_loop

from kiro_crew.apps.builtins.issue_radar.backend import crew_routes, crew_store, routes, store
from kiro_crew.crew_log import emit as crew_log_emit
from kiro_crew.crew_log import projection as crew_log_projection
from kiro_crew.crew_log.schema import KIND_SESSION
from kiro_crew.crew_log.store import CrewLog

BASE = "/api/apps/issue-radar"
OWNER, REPO = "kirodotdev", "KiroCrew"  # brand-ok: the repository name


def _registered() -> dict[tuple[str, str], object]:
    """Every crew route the registrar installs, keyed by (method, sub-path)."""
    app = web.Application()
    crew_routes.register_crew_routes(app)
    return {
        (route.method, str(route.resource.canonical)[len(BASE) :]): route.handler
        for route in app.router.routes()
    }


def _request(
    method: str,
    path: str,
    *,
    query: dict | None = None,
    body: object = "",
    app: web.Application | None = None,
    headers: dict | None = None,
    internal_auth: bool = False,
) -> web.Request:
    """A real (mocked) aiohttp request for a handler under test.

    aiohttp's own ``make_mocked_request``, not a duck-typed stub: the handlers are
    annotated ``(web.Request) -> web.Response`` and a stand-in fails the mypy gate.
    ``body=None`` models a malformed payload — ``request.json()`` raising is exactly
    what the preamble's ``except -> 400`` branch is written for.

    ``internal_auth`` sets the key ``token_auth_middleware`` sets after it validates
    ``X-Internal-Secret``, which is how the routes tell an agent from the browser. A
    real request object matters here and not just for mypy: on a ``MagicMock``,
    ``request.get("internal_auth")`` is truthy for every request, so every test
    would take the agent branch and the browser branch would never be exercised.
    """
    full = f"{BASE}{path}"
    if query:
        full = full + "?" + "&".join(f"{k}={v}" for k, v in query.items())
    kwargs: dict = {"app": app} if app is not None else {}
    if headers:
        kwargs["headers"] = headers
    req = make_mocked_request(method, full, **kwargs)  # type: ignore[arg-type]
    if "state" not in req.app:
        req.app["state"] = NoConfiguredOwner()
    req["user"] = "local-app"
    req["app"] = ""
    if internal_auth:
        req["internal_auth"] = True
    if body is None:
        req.json = AsyncMock(side_effect=ValueError("not json"))  # type: ignore[method-assign]
    elif body != "":
        req.json = AsyncMock(return_value=body)  # type: ignore[method-assign]
    return req


def _payload(response: web.Response) -> dict:
    raw = response.body
    assert isinstance(raw, bytes)
    return json.loads(raw.decode("utf-8"))


class _Slot:
    """A crew's chat slot, stubbed at the one method the route uses.

    ``enqueue_or_run_prompt`` returns True when it STARTED a turn and False when it
    queued — the route maps that onto ``queued``, so the stub honours the same
    contract instead of always returning True.

    ``_trust`` is the in-memory grant that makes an unattended crew's tool calls
    auto-approve. It is a field on the real slot object, which is why stopping a crew
    has to reach the slot and not only the record.
    """

    def __init__(self, running: bool = False, trust: bool = False) -> None:
        self.running = running
        self._trust = trust
        self.prompts: list[str] = []
        self.runners: list[object] = []

    def enqueue_or_run_prompt(self, prompt: str, run_chat, state) -> bool:
        self.prompts.append(prompt)
        self.runners.append(run_chat)
        return not self.running


class _Provider:
    """A slot's live ACP provider, stubbed at the one attribute the resolver reads."""

    def __init__(self, session_id: str) -> None:
        self.session_id = session_id


class _Sessions:
    """The session manager, stubbed at the exact registry lookup the write route makes.

    ``get_provider`` is keyed by the namespaced session key (``dashboard:<slot>``),
    which is how the route reaches a crew's live crew log unit from its slot key.
    """

    def __init__(self) -> None:
        self._by_key: dict[str, _Provider] = {}

    def bind(self, session_key: str, session_id: str) -> None:
        self._by_key[session_key] = _Provider(session_id)

    def get_provider(self, session_key: str) -> _Provider | None:
        return self._by_key.get(session_key)


class _State:
    """The dashboard state, stubbed at the three members the routes touch."""

    def __init__(self, slot: _Slot | None = None) -> None:
        self._slot = slot
        self.pushes = 0
        self.sessions = _Sessions()

    def get_slot(self, key: str) -> _Slot | None:
        return self._slot

    def push_slots_update(self) -> None:
        self.pushes += 1


def _tick() -> None:
    """Let the crew log's millisecond clock move between two stamps."""
    # flake-ok: pre-existing 4 ms sleep that lets the crew log's millisecond clock move between two stamps, moved verbatim; tracked for a de-flake pass
    time.sleep(0.004)


class _CrewRouteCase(unittest.IsolatedAsyncioTestCase):
    """Base: a temp data root, an enabled app, a connected repo -- and an isolated
    crew log, since a crew's ledger is the fold of the crew log its slot runs on."""

    def setUp(self) -> None:
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.root = Path(tmp.name)
        home = tempfile.TemporaryDirectory()
        self.addCleanup(home.cleanup)
        # _scope is the single place that decides which data root a request's store
        # calls land in, so this one patch keeps every request's store writes in self.root.
        for patcher in (
            mock.patch.object(routes, "_scope", return_value=self.root),
            mock.patch.object(routes, "is_app_enabled", return_value=True),
            mock.patch.object(store, "is_repo_connected", return_value=True),
            # Session -> crew resolution searches the connected repos, and the real
            # reader parses the developer's own config.json — which is empty in CI
            # and arbitrary locally. Pinned to the one repo these tests use so the
            # search is deterministic on both.
            mock.patch.object(
                store,
                "list_connected_repos",
                return_value=[
                    {"owner": OWNER, "repo": REPO, "provider": "github", "host": "github.com"}
                ],
            ),
            mock.patch.dict(
                os.environ, {"KIROCREW_HOME": home.name, crew_log_emit.CREW_LOG_ENV: "1"}
            ),
        ):
            patcher.start()
            self.addCleanup(patcher.stop)
        crew_log_emit.reset_caches()
        crew_log_projection.forget_slot_folds()
        self.addCleanup(crew_log_projection.forget_slot_folds)
        self.addCleanup(crew_log_emit.reset_caches)
        self.addCleanup(crew_log_emit.drain_for_shutdown, 2.0)
        # The app every request carries unless a test builds its own: its state maps
        # each crew's slot to the live unit the crew records into.
        self.state = _State()
        self.app = web.Application()
        with warnings.catch_warnings():
            # The route reads the string key ``"state"`` the dashboard sets, so the
            # test must set that key; aiohttp's AppKey advice does not apply here.
            warnings.simplefilter("ignore", UserWarning)
            self.app["state"] = self.state
        self._units: dict[str, str] = {}

    async def call(
        self,
        method: str,
        path: str,
        *,
        query: dict | None = None,
        body: object = "",
        app: web.Application | None = None,
        session: str | None = None,
        internal_auth: bool = False,
    ) -> web.Response:
        handler = _registered()[(method, path)]
        headers = {"X-Session-Key": session} if session is not None else None
        return await handler(  # type: ignore[operator]
            _request(
                method,
                path,
                query=query,
                body=body,
                app=app if app is not None else self.app,
                headers=headers,
                internal_auth=internal_auth,
            )
        )

    # ── fixtures written straight through the store ──────────────────────────

    def crew(self, name: str = "Andromeda", **spec) -> dict:
        """A crew, the crew log unit its slot runs on, and the slot -> unit binding
        the write route resolves. ``live=False`` makes a crew whose slot has no
        live session, so a write has nowhere to record."""
        live = spec.pop("live", True)
        # Off the loop, as the write route runs it: an async test calling the store
        # on the event-loop thread gets one lock attempt, not a short wait.
        crew = off_loop(crew_store.create_crew, OWNER, REPO, {"name": name, **spec}, self.root)
        if live:
            self.unit(crew)
        return crew

    def unit(self, crew: dict, session_id: str = "") -> str:
        session_id = session_id or f"acp-{crew['id']}-{len(self._units) + 1}"
        CrewLog.create(
            KIND_SESSION,
            session_id,
            owner="owner",
            agent="kirocrew",
            slot=crew_store.slot_key_for(crew["id"]),
        )
        self._units[crew["id"]] = session_id
        self.state.sessions.bind(f"dashboard:{crew['slot_key']}", session_id)
        return session_id

    def work(self, crew_id: str, number: int, phase: str, **patch) -> dict:
        return off_loop(
            crew_store.commit_work_progress,
            OWNER,
            REPO,
            crew_id,
            number,
            {"phase": phase, **patch},
            "claim",
            "seeded",
            root=self.root,
            session_id=self._units[crew_id],
        )["item"]

    def progress(
        self, crew_id: str, number: int, kind: str = "ci", text: str = "progress", **patch
    ) -> dict:
        """A write that carries no phase: progress on an existing item."""
        return off_loop(
            crew_store.commit_work_progress,
            OWNER,
            REPO,
            crew_id,
            number,
            patch,
            kind,
            text,
            root=self.root,
            session_id=self._units[crew_id],
        )["item"]

    def ledger(self, crew_id: str = "") -> list[dict]:
        return off_loop(crew_store.read_events, OWNER, REPO, self.root, crew_id=crew_id)
