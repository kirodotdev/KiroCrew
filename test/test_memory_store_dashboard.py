"""Store-scoped memory editing over HTTP: the gate, the fall-through, the boundary.

``?store=`` lets one dashboard request address a memory store the caller was never
bound to. That is a genuinely new reach, and four separate things have to hold for
it to be safe. This module pins each one at the surface a browser and an agent
actually reach, because three of the four fail in the PERMISSIVE direction and none
of them is visible in a successful response body:

* **The parameter's PRESENCE is the gate.** A request carrying it is asking the
  operator's question, so it takes ``require_owner_dashboard_request``. Gating on
  presence rather than on "the name differs from my binding" is what keeps
  ``?store=default`` -- the operator's own global memory -- from being waved through
  for an unbound caller.
* **The gate excludes an agent because an agent has no identity to present.** That
  is a CROSS-MODULE property: ``token_auth_middleware`` publishes ``request["user"]``
  on its cookie/query-token path and NEVER on its ``X-Internal-Secret`` branch, so
  ``is_owner_dashboard_request`` answers False for kiro-cli, the MCP servers and
  subagents. :class:`TestTheGateExcludesAnAgentBecauseItHasNoIdentity` pins it
  directly: if an identity is ever published on that branch, the whole parameter
  opens to every agent and nothing else in the tree would report it.
* **An undeclared name REFUSES rather than degrades.** ``resolve_store_path``
  deliberately degrades an unknown name onto the default store, which here would
  render the operator's own memory under a label no store carries -- and the request
  would look like it worked. So the 404 assertions also assert that the default
  store's own contents are absent from the refusal.
* **A store's writes never reach another store.** The isolation is the file
  boundary, so every store path in this module is composed by production's own
  resolvers rather than by a mapping the test invented.

Two operational notes. Every test pins ``KIROCREW_HOME`` under its own ``tmp_path``
before anything resolves a path, and :class:`_Env` asserts that pin held -- this
file plants a deliberately corrupt database and drives a restore, and both are
harmless only because the home is a scratch directory. Nothing here calls
``monkeypatch.undo()``: it reverts the whole fixture stack including that pin, which
is how a corrupt-file write once landed on a real 36 MB store.
"""

from __future__ import annotations

import asyncio
import json
from datetime import datetime, timezone
from pathlib import Path
from typing import Any
from unittest import mock
from urllib.parse import urlencode

import pytest
from aiohttp import streams, web
from aiohttp.test_utils import make_mocked_request

from kiro_crew import memory_backup, memory_stores
from kiro_crew.config import loader as loader_mod
from kiro_crew.config.loader import config_dir
from kiro_crew.context import ContextBuilder
from kiro_crew.dashboard.handlers import memory as memory_handlers
from kiro_crew.dashboard.handlers import memory_admin
from kiro_crew.dashboard.handlers.source_providers import is_owner_dashboard_request
from kiro_crew.memory import MemoryStore
from kiro_crew.memory_stores import (
    DEFAULT_MEMORY_STORE,
    declared_store_names,
    memory_index_path_for,
    memory_store_dir_for,
    owned_store_path,
    resolve_store_path,
)
from kiro_crew.vector_memory import VectorMemoryStore

# Every test writes ``config.json`` into its own data home and drops the process-wide
# config cache and the declared-store memo, so two workers racing those globals is a
# flake. One xdist group for the whole module, as ``test_memory_v2_facet_read.py`` does.
pytestmark = pytest.mark.xdist_group("memory_store_dashboard")


_FINANCE = "finance"
_OPS = "ops"

#: A name no test declares. The 404 assertions rest on it never being declared.
_UNDECLARED = "nosuchstore"

#: The caller's session key. Namespaced ``dashboard:`` so ``_recognize_session``
#: resolves it through the live-slot branch rather than the on-disk recovery probe,
#: and NOT ``dashboard:ui``, whose implicit trust would hide a binding lookup.
_SESSION_KEY = "dashboard:chat-1"

#: A signed machine-local dashboard subject. ``is_owner_dashboard_request`` accepts
#: it when no ``owner_id`` is configured, which is the ordinary local install.
_OWNER_SUBJECT = "local-app"

#: Semantic keys have an allowlist (``pref.*``, ``project.*``, ``user.*``,
#: ``lesson.*``); a key outside it is rejected by the store itself, so a test
#: asserting isolation with one would pass without writing anything.
_SILO_KEY = "pref.editor"
_GLOBAL_KEY = "user.name"

_GLOBAL_MARKER = "the operator's own global memory"
_FINANCE_MARKER = "the finance crew's own memory"

_EPISODE = "The finance crew rotated the deploy key on the release host."


class _Slot:
    """A live, unrestricted chat slot -- the two flags the write gate reads."""

    is_restricted = False
    blocks_reads = False


class _ConversationLog:
    """The one method ``context.store_of_session`` calls to read a binding."""

    def __init__(self, bindings: dict[str, str]) -> None:
        self._bindings = dict(bindings)

    def get_metadata(self, session_key: str) -> dict[str, str]:
        return {"memory_store": self._bindings.get(session_key, "")}


class _State:
    """The ``DashboardState`` surface these routes actually read.

    Six attributes, spelled out rather than mocked. A ``MagicMock`` answers every
    unwritten attribute truthily, which on this path means an authorization check
    nobody wrote passes -- and ``owner_id`` in particular decides whether the local
    bootstrap subject counts as the owner at all.
    """

    owner_id = ""
    consolidator = None
    sessions = None

    def __init__(self, bindings: dict[str, str]) -> None:
        self.context_builder = None
        self.conversation_log = _ConversationLog(bindings)
        self._slots = {_slot_name(key): _Slot() for key in bindings}
        self._restricted_keys: set[str] = set()


def _slot_name(session_key: str) -> str:
    """The slot half of a session key, the way every handler splits it."""
    return session_key.split(":", 1)[-1] if ":" in session_key else session_key


class _Env:
    """A pinned data home, the store declarations, the tiers, and every close.

    One object rather than a fixture per piece: each piece needs closing or
    reverting, and separate fixtures mean one piece's teardown can be skipped by
    another's failure. The ``finally`` in :func:`env` is the only place that runs.
    """

    def __init__(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        self.home = tmp_path / "data-home"
        self.home.mkdir()
        monkeypatch.setenv("KIROCREW_HOME", str(self.home))
        loader_mod._invalidate_config_cache()
        monkeypatch.setattr(memory_stores, "_DECLARED_MEMO", None)
        # THE guard for this file. Every store path below is composed from
        # ``config_dir()``, and this module deliberately writes a corrupt database
        # and drives a restore -- both are harmless only while that resolves under
        # ``tmp_path``. Asserted rather than assumed, because the pin is an env var
        # any earlier fixture could have laid something else over.
        resolved = config_dir()
        assert resolved == self.home, resolved
        assert tmp_path in resolved.parents, resolved
        self._monkeypatch = monkeypatch
        self._tiers: dict[str, VectorMemoryStore] = {}
        self._markdown: dict[str, MemoryStore] = {}
        self._closables: list[VectorMemoryStore] = []
        self._states: list[_State] = []
        self.unavailable: set[str] = set()
        monkeypatch.setattr(ContextBuilder, "ensure_store", staticmethod(self._ensure_store))

    # ── declaration ──

    def declare(self, *names: str) -> None:
        """Declare *names*, alongside the always-declared default store.

        ``memory_stores`` is an OBJECT keyed by store name; spelled as an ARRAY the
        schema drops the whole entry and every name degrades onto the default store,
        so a test written that way would assert against the operator-shaped file.
        """
        payload = {
            "memory_stores": {DEFAULT_MEMORY_STORE: {}, **{name: {} for name in names}},
            "default_memory_store": DEFAULT_MEMORY_STORE,
        }
        (self.home / "config.json").write_text(json.dumps(payload), encoding="utf-8")
        loader_mod._invalidate_config_cache()
        self._monkeypatch.setattr(memory_stores, "_DECLARED_MEMO", None)

    # ── the vector tier ──

    async def _ensure_store(self, store: str) -> VectorMemoryStore | None:
        """Stand in for ``ContextBuilder.ensure_store``, over the store's REAL file.

        The name -> file mapping is production's ``owned_store_path``, not a dict
        this module invented: the isolation under test IS the file boundary, so a
        stub that mapped two names to two objects of its own would prove only that
        its own mapping has two entries. The real builder is bypassed because it
        publishes into a process-global cache and pulls in the embedding stack.

        This is also the one place ``unavailable`` is honoured, so a test can make a
        silo's tier refuse to stand up without losing the ability to seed its file.
        """
        if store in self.unavailable:
            return None
        return self.tier(store)

    def tier(self, store: str) -> VectorMemoryStore:
        """A NAMED store's vector tier, opened once and shared with the handler.

        Named only: the global store is spelled ``""`` at the request seam, and every
        resolver in ``memory_stores`` RAISES on that, so the global tier is seeded
        through :meth:`seed_vector_file` and opened by the handler itself.
        """
        cached = self._tiers.get(store)
        if cached is not None:
            return cached
        path = owned_store_path(store)
        assert path is not None, store
        path.parent.mkdir(parents=True, exist_ok=True)
        tier = VectorMemoryStore(db_path=path)
        tier.init()
        self._tiers[store] = tier
        self._closables.append(tier)
        return tier

    def seed_vector_file(
        self,
        store: str,
        *keys: str,
        episodes: tuple[str, ...] = (),
        lessons: tuple[str, ...] = (),
    ) -> Path:
        """Write into *store*'s vector file, then CLOSE it.

        For the global store, and for the routes that resolve a store's PATH without
        opening it (the store list, the backup and restore pair). Closing matters
        twice: the store list's probe opens read-only, which cannot create the
        ``-shm`` sibling a WAL database needs, and the restore's rename of an open
        file fails on Windows.
        """
        path = resolve_store_path(store)
        path.parent.mkdir(parents=True, exist_ok=True)
        tier = VectorMemoryStore(db_path=path)
        tier.init()
        try:
            for key in keys:
                assert tier.set_semantic(key, f"value of {key}", 1.0, "user_explicit") is None
            for text in episodes:
                assert tier.write_episodic(text, source="test") is True
            for lesson in lessons:
                assert tier.write_lesson(lesson) is not None
        finally:
            tier.close()
        return path

    # ── the markdown tier ──

    def markdown(self, store: str) -> MemoryStore:
        """*store*'s markdown tier, over the SAME two roots the handler resolves.

        The markdown root and the FTS index are separate questions
        (``memory_store_dir_for`` vs ``memory_index_path_for``), and passing one
        path twice would put a silo's index inside the default store's tree -- so
        the seed asks both, exactly as ``markdown_memory_for_store`` does.
        """
        cached = self._markdown.get(store)
        if cached is not None:
            return cached
        if store == DEFAULT_MEMORY_STORE:
            mem = MemoryStore()
        else:
            mem = MemoryStore(
                workspace=memory_store_dir_for(store), index_db=memory_index_path_for(store)
            )
        mem.init()
        self._markdown[store] = mem
        return mem

    # ── request state ──

    def state(self, *, bound_to: str = "", session_key: str = _SESSION_KEY) -> _State:
        """A state whose *session_key* records a binding to *bound_to*."""
        state = _State({session_key: bound_to})
        self._states.append(state)
        return state

    def close(self) -> None:
        for tier in self._closables:
            tier.close()
        for state in self._states:
            # ``_get_vector_store_async`` publishes the GLOBAL store's standalone
            # tier here, so it is opened by the handler rather than by this class
            # and would otherwise outlive the test holding a sqlite handle.
            standalone = getattr(state, "_standalone_vector", None)
            if standalone is not None:
                standalone.close()


@pytest.fixture
def env(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    e = _Env(tmp_path, monkeypatch)
    try:
        yield e
    finally:
        e.close()


def _payload(raw: bytes) -> streams.StreamReader:
    """A readable request body. ``make_mocked_request``'s default reads as empty."""
    reader = streams.StreamReader(
        mock.Mock(_reading_paused=False), limit=len(raw) + 1, loop=asyncio.get_running_loop()
    )
    reader.feed_data(raw)
    reader.feed_eof()
    return reader


def _request(
    method: str,
    path: str,
    state: _State,
    *,
    query: dict[str, str] | None = None,
    session_key: str = _SESSION_KEY,
    owner: bool = False,
    body: Any = None,
    match_info: dict[str, str] | None = None,
) -> web.Request:
    """A request in one of exactly two authenticated shapes.

    Without *owner* the request carries NO ``user`` and NO ``app`` claim, which is
    precisely what ``token_auth_middleware``'s ``X-Internal-Secret`` branch leaves
    behind -- so the default shape drives the agent case rather than a synthetic
    one. With it, the two claims the cookie path publishes: an identity plus an
    EMPTY app claim, since a present non-empty one is an app token.
    """
    app = web.Application()
    app["state"] = state
    target = f"{path}?{urlencode(query)}" if query else path
    headers = {"X-Session-Key": session_key}
    kwargs: dict[str, Any] = {"headers": headers, "app": app}
    if body is not None:
        raw = json.dumps(body).encode()
        headers["Content-Type"] = "application/json"
        headers["Content-Length"] = str(len(raw))
        kwargs["payload"] = _payload(raw)
    if match_info is not None:
        kwargs["match_info"] = match_info
    request = make_mocked_request(method, target, **kwargs)
    if owner:
        request["user"] = _OWNER_SUBJECT
        request["app"] = ""
    return request


def _body(resp: web.Response) -> Any:
    return json.loads(resp.text or "")


def _text(resp: web.Response) -> str:
    return (resp.body or b"").decode()


def _keys(resp: web.Response) -> set[str]:
    return {entry["key"] for entry in _body(resp)["entries"]}


# ── 1 + 6. The gate is the point ──────────────────────────────────────────────


class TestNamingAStoreTakesTheOwnerGate:
    """``?store=`` present => owner-only, on a read and on a write alike.

    The refusal is the stronger of the two available answers: quietly ignoring the
    name and serving the caller's own store would tell a request that asked for
    another crew's rows that it received them.
    """

    @pytest.mark.asyncio
    async def test_a_get_naming_a_store_is_refused_without_an_owner_identity(self, env) -> None:
        env.declare(_FINANCE)
        env.tier(_FINANCE).set_semantic(_SILO_KEY, "vim", 1.0, "user_explicit")
        resp = await memory_handlers.api_memory_semantic(
            _request("GET", "/api/memory/semantic", env.state(), query={"store": _FINANCE})
        )
        assert resp.status == 403
        assert _body(resp)["code"] == "owner_only"
        # No rows, not even an empty page: a caller that was refused must not learn
        # the shape of the answer it asked for.
        assert "entries" not in _body(resp)
        assert _SILO_KEY not in _text(resp)

    @pytest.mark.asyncio
    async def test_a_put_naming_a_store_is_refused_before_it_writes(self, env) -> None:
        """The store resolves ahead of the write gate, so the 403 is the owner one.

        And the silo's markdown tree must not exist afterwards: the refusal has to
        land before ``markdown_memory_for_store`` stands the store up, or a caller
        that may not name a store still creates its directory.
        """
        env.declare(_FINANCE)
        resp = await memory_handlers.api_memory_preferences(
            _request(
                "PUT",
                "/api/memory/preferences",
                env.state(),
                query={"store": _FINANCE},
                body={"content": "planted by a caller with no identity"},
            )
        )
        assert resp.status == 403
        assert _body(resp)["code"] == "owner_only"
        assert not memory_store_dir_for(_FINANCE).exists()

    @pytest.mark.asyncio
    async def test_store_default_takes_the_gate_even_from_an_unbound_caller(self, env) -> None:
        """The value a mismatch-based rule would wave through.

        This caller is bound to no silo, so ``?store=default`` names exactly the
        store it would have read anyway -- and it is the operator's own global
        memory. A gate that fired only on a DIFFERENCE from the caller's binding
        would admit it, which is why the gate fires on PRESENCE.
        """
        env.declare(_FINANCE)
        resp = await memory_handlers.api_memory_preferences(
            _request(
                "GET",
                "/api/memory/preferences",
                env.state(bound_to=""),
                query={"store": DEFAULT_MEMORY_STORE},
            )
        )
        assert resp.status == 403
        assert _body(resp)["code"] == "owner_only"

    @pytest.mark.asyncio
    async def test_the_owner_reading_store_default_gets_the_global_store(self, env) -> None:
        """The positive half: ``?store=default`` addresses the global store.

        Asserted against BOTH neighbours -- it must carry the global store's
        content and must not carry the silo's -- so a resolver that answered the
        caller's binding instead of the named store fails here.
        """
        env.declare(_FINANCE)
        env.markdown(DEFAULT_MEMORY_STORE).write_preferences(_GLOBAL_MARKER)
        env.markdown(_FINANCE).write_preferences(_FINANCE_MARKER)
        resp = await memory_handlers.api_memory_preferences(
            _request(
                "GET",
                "/api/memory/preferences",
                env.state(bound_to=_FINANCE),
                query={"store": DEFAULT_MEMORY_STORE},
                owner=True,
            )
        )
        assert resp.status == 200
        assert _body(resp)["content"] == _GLOBAL_MARKER
        assert _FINANCE_MARKER not in _text(resp)


# ── 2. The cross-module property the whole parameter rests on ─────────────────


class TestTheGateExcludesAnAgentBecauseItHasNoIdentity:
    """``X-Internal-Secret`` grants access and publishes NO ``request["user"]``.

    This is the most important test in the file, and it is the one whose subject
    lives in another module. ``require_owner_dashboard_request`` excludes an agent
    POSITIVELY -- it requires an identity the agent has none of -- rather than by
    asking "is this not an agent". That only holds while
    ``token_auth_middleware``'s internal-secret branch keeps handing the request
    straight to the handler without naming a caller. If an identity is ever
    published there, every agent gains ``?store=`` and nothing else in the tree
    reports it, so the branch is driven here directly.
    """

    SECRET = "test-internal-secret"

    @staticmethod
    def _middleware(monkeypatch: pytest.MonkeyPatch):
        """The real middleware over the real internal-path sets.

        Built from ``server``'s own frozensets rather than a set this test invented:
        the claim is about the paths an agent actually calls, and a hand-written set
        would keep passing after a path moved between the two classes.
        """
        import kiro_crew.dashboard.token_auth as token_auth
        from kiro_crew.dashboard import server

        class _FakeSel:
            def log_api_access(self, **kw: Any) -> None:
                return None

        monkeypatch.setattr(token_auth, "_sel_fn", lambda: _FakeSel())
        return token_auth.token_auth_middleware(
            internal_paths=server._STRICT_INTERNAL_API_PATHS,
            mixed_internal_paths=server._MIXED_INTERNAL_API_PATHS,
            internal_secret=TestTheGateExcludesAnAgentBecauseItHasNoIdentity.SECRET,
        ), sorted(server._STRICT_INTERNAL_API_PATHS | server._MIXED_INTERNAL_API_PATHS)

    @staticmethod
    def _internal_request(path: str, state: _State) -> web.Request:
        """A loopback request holding the internal secret and nothing else.

        ``remote`` comes from the transport's peername, which ``make_mocked_request``
        leaves unset -- and the internal branch is only reachable from loopback, so
        without it the request would take the cookie path and prove nothing.
        """
        app = web.Application()
        app["state"] = state
        transport = mock.Mock()
        transport.get_extra_info = lambda name, default=None: (
            ("127.0.0.1", 4242) if name == "peername" else None
        )
        return make_mocked_request(
            "POST",
            path,
            headers={"X-Internal-Secret": TestTheGateExcludesAnAgentBecauseItHasNoIdentity.SECRET},
            app=app,
            transport=transport,
        )

    @pytest.mark.asyncio
    async def test_the_internal_secret_grant_reaches_the_handler(self, env, monkeypatch) -> None:
        """The control for the test below: the branch is TAKEN, not denied.

        Without this, "no identity was published" would also be satisfied by a
        request the middleware refused outright -- which would make the next test
        pass for the wrong reason forever.
        """
        middleware, paths = self._middleware(monkeypatch)
        reached: list[str] = []

        async def _handler(request: web.Request) -> web.Response:
            reached.append(request.path)
            return web.Response(text="ok")

        resp = await middleware(self._internal_request(paths[0], env.state()), _handler)
        assert resp.status == 200
        assert reached == [paths[0]]

    @pytest.mark.asyncio
    async def test_the_internal_secret_grant_publishes_no_user_identity(
        self, env, monkeypatch
    ) -> None:
        middleware, paths = self._middleware(monkeypatch)
        seen: list[tuple[str, bool]] = []

        async def _handler(request: web.Request) -> web.Response:
            seen.append((request.path, "user" in request))
            return web.Response(text="ok")

        for path in paths:
            resp = await middleware(self._internal_request(path, env.state()), _handler)
            assert resp.status == 200, path
        named = [path for path, has_user in seen if has_user]
        assert named == [], (
            "the internal-secret branch published an identity for these paths, so "
            f"is_owner_dashboard_request now answers True for an agent on them: {named}"
        )

    @pytest.mark.asyncio
    async def test_a_request_in_that_shape_is_not_the_dashboard_owner(self, env) -> None:
        """The other half of the join, asserted on the predicate itself."""
        agent = _request("GET", "/api/memory/semantic", env.state())
        assert "user" not in agent
        assert is_owner_dashboard_request(agent) is False
        # And the shape that IS the owner, so the predicate is not simply always
        # False on a mocked request.
        assert (
            is_owner_dashboard_request(
                _request("GET", "/api/memory/semantic", env.state(), owner=True)
            )
            is True
        )

    @pytest.mark.asyncio
    async def test_a_request_in_that_shape_cannot_name_a_store(self, env) -> None:
        """End to end: no identity, therefore no ``?store=``, on every route."""
        env.declare(_FINANCE)
        for handler, method, path in (
            (memory_handlers.api_memory_semantic, "GET", "/api/memory/semantic"),
            (memory_handlers.api_memory_episodic_list, "GET", "/api/memory/episodic"),
            (memory_handlers.api_memory_stats, "GET", "/api/memory/stats"),
            (memory_handlers.api_memory_events, "GET", "/api/memory/events"),
            (memory_handlers.api_memory_carve, "GET", "/api/memory/carve"),
            (memory_handlers.api_memory_projects, "GET", "/api/memory/projects"),
            (memory_handlers.api_memory_history, "GET", "/api/memory/history"),
        ):
            resp = await handler(_request(method, path, env.state(), query={"store": _FINANCE}))
            assert resp.status == 403, path
            assert _body(resp)["code"] == "owner_only", path


# ── 3. An absent parameter is byte-for-byte what it was ───────────────────────


class TestAnAbsentParameterDoesNotFollowTheSessionKeyHeader:
    """No ``?store=`` => the GLOBAL store, and ``X-Session-Key`` cannot redirect it.

    This class pins an authorization boundary, not a convenience. ``_read_session_key``
    reads ``X-Session-Key`` on the CALLER'S WORD -- it is unverified on TCP -- and
    ``store_of_session`` then reads whatever store that session recorded, with no check
    that the session belongs to the caller. So if an absent parameter resolved the
    binding, any holder of a non-owner dashboard token (an allowlisted Slack user who
    ran ``!dashboard``, or an app token scoped to the path) could read and write a
    crew's silo just by naming a session key bound to it, and the owner gate would
    never run, because the gate fires only on a PRESENT parameter.

    Every one of these routes served the global store unconditionally before store
    scoping existed, so answering the global store here is also what keeps the
    operator's own memory exactly where it was. Facet pages and counts follow the
    same rule: a session header carries no authority to choose a silo.
    """

    @pytest.mark.asyncio
    @pytest.mark.parametrize("query", [{}, {"count_by": "kind"}])
    async def test_a_non_owner_token_cannot_carve_a_header_named_silo(self, env, query) -> None:
        env.declare(_FINANCE)
        assert (
            env.tier(_FINANCE).set_semantic(_SILO_KEY, _FINANCE_MARKER, 1.0, "user_explicit")
            is None
        )
        state = env.state(bound_to=_FINANCE)
        state.owner_id = "operator"
        request = _request("GET", "/api/memory/carve", state, query=query)
        request["user"] = "another-dashboard-user"

        response = await memory_handlers.api_memory_carve(request)

        assert response.status == 409
        assert _body(response)["code"] == "facets_unsupported"
        assert _FINANCE_MARKER not in response.body.decode()
        assert "counts" not in _body(response)

    @pytest.mark.asyncio
    async def test_a_bound_session_key_does_not_redirect_the_markdown_tier(self, env) -> None:
        """The escalation, driven exactly as an attacker would: header only, no parameter."""
        env.declare(_FINANCE)
        env.markdown(DEFAULT_MEMORY_STORE).write_preferences(_GLOBAL_MARKER)
        env.markdown(_FINANCE).write_preferences(_FINANCE_MARKER)
        resp = await memory_handlers.api_memory_preferences(
            _request("GET", "/api/memory/preferences", env.state(bound_to=_FINANCE))
        )
        assert resp.status == 200
        # The GLOBAL document, and the silo's marker nowhere in the body.
        assert _body(resp)["content"] == _GLOBAL_MARKER
        assert _FINANCE_MARKER not in resp.body.decode()

    @pytest.mark.asyncio
    async def test_an_unbound_session_reads_the_global_store(self, env) -> None:
        env.declare(_FINANCE)
        env.markdown(DEFAULT_MEMORY_STORE).write_preferences(_GLOBAL_MARKER)
        env.markdown(_FINANCE).write_preferences(_FINANCE_MARKER)
        resp = await memory_handlers.api_memory_preferences(
            _request("GET", "/api/memory/preferences", env.state(bound_to=""))
        )
        assert resp.status == 200
        assert _body(resp)["content"] == _GLOBAL_MARKER

    @pytest.mark.asyncio
    async def test_a_bound_session_key_does_not_redirect_the_vector_tier(self, env) -> None:
        """The other tier, because it resolves through a different function.

        ``markdown_memory_for_store`` and ``vector_memory_for_store`` answer for the
        same store name through two independent paths, so one of them refusing the
        header is no evidence about the other.
        """
        env.declare(_FINANCE)
        assert env.tier(_FINANCE).set_semantic(_SILO_KEY, "vim", 1.0, "user_explicit") is None
        env.seed_vector_file(DEFAULT_MEMORY_STORE, _GLOBAL_KEY)
        bound = await memory_handlers.api_memory_semantic(
            _request("GET", "/api/memory/semantic", env.state(bound_to=_FINANCE))
        )
        assert _keys(bound) == {_GLOBAL_KEY}
        unbound = await memory_handlers.api_memory_semantic(
            _request("GET", "/api/memory/semantic", env.state(bound_to=""))
        )
        assert _keys(unbound) == {_GLOBAL_KEY}

    @pytest.mark.asyncio
    async def test_a_bound_session_key_does_not_redirect_a_write(self, env) -> None:
        """A read leaks; a write CORRUPTS, so the write half is pinned separately."""
        env.declare(_FINANCE)
        env.markdown(DEFAULT_MEMORY_STORE).write_preferences(_GLOBAL_MARKER)
        env.markdown(_FINANCE).write_preferences(_FINANCE_MARKER)
        resp = await memory_handlers.api_memory_preferences(
            _request(
                "PUT",
                "/api/memory/preferences",
                env.state(bound_to=_FINANCE),
                body={"content": "zzq-written-by-a-named-session-key"},
            )
        )
        assert resp.status == 200
        # The silo's document is untouched; the write landed on the global store.
        assert env.markdown(_FINANCE).read_preferences() == _FINANCE_MARKER
        assert (
            "zzq-written-by-a-named-session-key"
            in env.markdown(DEFAULT_MEMORY_STORE).read_preferences()
        )


class TestCarveRequiresAnOwnerSelectedStore:
    """An explicit owner choice reaches the silo; an absent parameter cannot."""

    @pytest.mark.asyncio
    async def test_carve_reads_the_owner_selected_silo(self, env) -> None:
        env.declare(_FINANCE)
        assert env.tier(_FINANCE).set_semantic(_SILO_KEY, "vim", 1.0, "user_explicit") is None
        resp = await memory_handlers.api_memory_carve(
            _request(
                "GET",
                "/api/memory/carve",
                env.state(bound_to=_OPS),
                query={"store": _FINANCE},
                owner=True,
            )
        )
        assert resp.status == 200
        assert _body(resp)["store"] == _FINANCE


# ── 4. An undeclared name refuses, and never degrades ─────────────────────────


class TestAnUndeclaredStoreIsRefusedNotDegraded:
    """404 ``unknown_memory_store``, with the default store's contents ABSENT.

    ``resolve_store_path`` degrades an unknown name onto the default store, so the
    failure this guards against is not an exception -- it is a 200 carrying the
    operator's own memory under a label no store carries. Each assertion therefore
    pins the status AND the absence of the global store's own content, which is what
    would appear if the refusal became a degrade.
    """

    @pytest.mark.asyncio
    async def test_the_markdown_tier_refuses(self, env) -> None:
        env.declare(_FINANCE)
        env.markdown(DEFAULT_MEMORY_STORE).write_preferences(_GLOBAL_MARKER)
        resp = await memory_handlers.api_memory_preferences(
            _request(
                "GET",
                "/api/memory/preferences",
                env.state(bound_to=_FINANCE),
                query={"store": _UNDECLARED},
                owner=True,
            )
        )
        assert resp.status == 404
        assert _body(resp)["code"] == "unknown_memory_store"
        assert _GLOBAL_MARKER not in _text(resp)
        assert "content" not in _body(resp)

    @pytest.mark.asyncio
    async def test_the_vector_tier_refuses(self, env) -> None:
        env.declare(_FINANCE)
        env.seed_vector_file(DEFAULT_MEMORY_STORE, _GLOBAL_KEY)
        resp = await memory_handlers.api_memory_semantic(
            _request(
                "GET",
                "/api/memory/semantic",
                env.state(bound_to=""),
                query={"store": _UNDECLARED},
                owner=True,
            )
        )
        assert resp.status == 404
        assert _body(resp)["code"] == "unknown_memory_store"
        assert _GLOBAL_KEY not in _text(resp)
        assert "entries" not in _body(resp)

    @pytest.mark.asyncio
    async def test_a_malformed_name_gets_the_same_answer_as_an_unknown_one(self, env) -> None:
        """Deliberately indistinguishable: telling them apart would report to a
        caller which names are declared."""
        env.declare(_FINANCE)
        for name in ("../work", "Finance", "con", "a" * 200):
            resp = await memory_handlers.api_memory_semantic(
                _request(
                    "GET",
                    "/api/memory/semantic",
                    env.state(),
                    query={"store": name},
                    owner=True,
                )
            )
            assert resp.status == 404, name
            assert _body(resp)["code"] == "unknown_memory_store", name


# ── 5. Isolation is the file boundary ─────────────────────────────────────────


class TestOneStoresWritesNeverReachAnother:
    """Write through the API into A, read B through the API, see nothing.

    Each test also reads A back, because "B is empty" is equally true of a write
    that never landed anywhere -- and that version of the test passes while the
    feature is entirely broken.
    """

    @pytest.mark.asyncio
    async def test_the_markdown_tier_is_isolated(self, env) -> None:
        env.declare(_FINANCE, _OPS)
        state = env.state(bound_to=_OPS)
        for route, handler in (
            ("/api/memory/preferences", memory_handlers.api_memory_preferences),
            ("/api/memory/projects", memory_handlers.api_memory_projects),
        ):
            written = await handler(
                _request(
                    "PUT",
                    route,
                    state,
                    query={"store": _FINANCE},
                    owner=True,
                    body={"content": _FINANCE_MARKER},
                )
            )
            assert written.status == 200, route
            assert _body(written)["ok"] is True, route
            # A: the write landed. B: it did not travel. The default store too,
            # since a resolver that lost the name would land there.
            for store, expected in ((_FINANCE, True), (_OPS, False), (DEFAULT_MEMORY_STORE, False)):
                read = await handler(
                    _request("GET", route, state, query={"store": store}, owner=True)
                )
                assert read.status == 200, (route, store)
                assert (_FINANCE_MARKER in _body(read)["content"]) is expected, (route, store)

    @pytest.mark.asyncio
    async def test_the_vector_tier_is_isolated(self, env) -> None:
        env.declare(_FINANCE, _OPS)
        state = env.state(bound_to=_OPS)
        written = await memory_handlers.api_memory_semantic_write(
            _request(
                "PUT",
                "/api/memory/semantic",
                state,
                query={"store": _FINANCE},
                owner=True,
                body={"key": _SILO_KEY, "value": "vim"},
            )
        )
        assert written.status == 200
        # The episode goes in behind the API, which has no episodic write route.
        assert env.tier(_FINANCE).write_episodic(_EPISODE, source="test") is True
        for store, expected in ((_FINANCE, True), (_OPS, False), (DEFAULT_MEMORY_STORE, False)):
            semantic = await memory_handlers.api_memory_semantic(
                _request("GET", "/api/memory/semantic", state, query={"store": store}, owner=True)
            )
            assert semantic.status == 200, store
            assert (_SILO_KEY in _keys(semantic)) is expected, store
            episodic = await memory_handlers.api_memory_episodic_list(
                _request("GET", "/api/memory/episodic", state, query={"store": store}, owner=True)
            )
            assert episodic.status == 200, store
            texts = {entry["text"] for entry in _body(episodic)["entries"]}
            assert (_EPISODE in texts) is expected, store

    @pytest.mark.asyncio
    async def test_a_delete_in_one_store_leaves_the_others_row_alone(self, env) -> None:
        """The same boundary in the destructive direction, which the read tests
        cannot observe: both stores hold the same KEY, and only one loses it."""
        env.declare(_FINANCE, _OPS)
        state = env.state(bound_to=_OPS)
        for store in (_FINANCE, _OPS):
            assert env.tier(store).set_semantic(_SILO_KEY, store, 1.0, "user_explicit") is None
        deleted = await memory_handlers.api_memory_semantic_delete(
            _request(
                "DELETE",
                f"/api/memory/semantic/{_SILO_KEY}",
                state,
                query={"store": _FINANCE},
                owner=True,
                match_info={"key": _SILO_KEY},
            )
        )
        assert deleted.status == 200
        surviving = await memory_handlers.api_memory_semantic(
            _request("GET", "/api/memory/semantic", state, query={"store": _OPS}, owner=True)
        )
        assert _keys(surviving) == {_SILO_KEY}
        gone = await memory_handlers.api_memory_semantic(
            _request("GET", "/api/memory/semantic", state, query={"store": _FINANCE}, owner=True)
        )
        assert _keys(gone) == set()


# ── 7. A tier that cannot be stood up is reported, never substituted ──────────


class TestAStoreThatCannotBeStoodUpIsReported:
    """503 ``store_unavailable``, and NOT the global store's rows.

    Falling back there serves the operator's own population under a crew's name,
    which is invisible in the response -- so the assertion is both the status and
    the absence of the global store's own key.
    """

    @pytest.mark.asyncio
    async def test_a_silo_whose_vector_tier_is_unavailable_answers_503(self, env) -> None:
        env.declare(_FINANCE)
        env.seed_vector_file(DEFAULT_MEMORY_STORE, _GLOBAL_KEY)
        env.unavailable.add(_FINANCE)
        resp = await memory_handlers.api_memory_semantic(
            _request(
                "GET",
                "/api/memory/semantic",
                env.state(),
                query={"store": _FINANCE},
                owner=True,
            )
        )
        assert resp.status == 503
        assert _body(resp)["code"] == "store_unavailable"
        assert "entries" not in _body(resp)
        assert _GLOBAL_KEY not in _text(resp)

    @pytest.mark.asyncio
    async def test_the_admin_routes_report_it_the_same_way(self, env) -> None:
        env.declare(_FINANCE)
        env.unavailable.add(_FINANCE)
        resp = await memory_admin.api_memory_retired(
            _request(
                "GET", "/api/memory/retired", env.state(), query={"store": _FINANCE}, owner=True
            )
        )
        assert resp.status == 503
        assert _body(resp)["code"] == "store_unavailable"


# ── 8. A restore name cannot leave its store's backup directory ───────────────


@pytest.mark.asyncio
@pytest.mark.parametrize("keep,remaining", [(30, 9), (3, 3)])
async def test_dashboard_backup_uses_configured_retention(env, keep, remaining):
    env.declare()
    path = env.seed_vector_file(DEFAULT_MEMORY_STORE, _GLOBAL_KEY)
    config_path = env.home / "config.json"
    config = json.loads(config_path.read_text(encoding="utf-8"))
    config["memory"] = {"backup_enabled": False, "backup_keep": keep}
    config_path.write_text(json.dumps(config), encoding="utf-8")
    loader_mod._invalidate_config_cache()
    assert loader_mod.KiroCrewConfig.load().memory.backup_keep == keep
    assert loader_mod.KiroCrewConfig.load().memory.backup_keep == keep
    for day in range(1, 9):
        assert memory_backup.backup_store(path, now=datetime(2020, 1, day, tzinfo=timezone.utc))
    response = await memory_admin.api_memory_backup(
        _request("POST", "/api/memory/backup", env.state(), owner=True, body={"store": "default"})
    )
    assert response.status == 200
    assert _body(response) == {"backed_up": 1, "skipped": 0, "pruned": 9 - remaining, "failed": 0}
    assert len(memory_backup.list_backups(path)) == remaining


class TestARestoreNameStaysInItsStoresBackupDirectory:
    """A caller-supplied filename is resolved inside one directory, or refused.

    The decoy matters: an accepted traversal would restore a REAL, integrity-clean
    database from one directory up, so the refusal has to be asserted against a
    name that would otherwise work rather than against one that would 404 anyway.
    """

    @pytest.mark.asyncio
    async def test_a_traversing_backup_name_is_refused(self, env) -> None:
        env.declare(_FINANCE)
        db_path = env.seed_vector_file(_FINANCE, _SILO_KEY)
        backup = memory_backup.backup_store(db_path)
        assert backup is not None
        # A valid database one level ABOVE the backup directory. Without the
        # containment check ``backups/../decoy.db`` resolves onto it, passes
        # ``is_file()`` and its integrity check, and gets restored.
        decoy = db_path.parent / "decoy.db"
        decoy.write_bytes(backup.read_bytes())
        for name in (
            "../decoy.db",
            "../../memory.db",
            "sub/decoy.db",
            "..\\decoy.db",
            str(decoy),
            str(backup),
            ".",
            "..",
            "",
        ):
            resp = await memory_admin.api_memory_restore(
                _request(
                    "POST",
                    "/api/memory/restore",
                    env.state(),
                    owner=True,
                    body={"store": _FINANCE, "name": name},
                )
            )
            assert resp.status == 400, name
            assert _body(resp)["code"] == "invalid_backup_name", name
            # Nothing was displaced, so the refusal landed before the move.
            assert list(db_path.parent.glob(f"{db_path.name}.superseded.*")) == [], name

    @pytest.mark.asyncio
    async def test_the_stores_own_backup_is_accepted(self, env) -> None:
        """The control. Without it every refusal above is also satisfied by a route
        that refuses every name it is given."""
        env.declare(_FINANCE)
        db_path = env.seed_vector_file(_FINANCE, _SILO_KEY)
        backup = memory_backup.backup_store(db_path)
        assert backup is not None
        resp = await memory_admin.api_memory_restore(
            _request(
                "POST",
                "/api/memory/restore",
                env.state(),
                owner=True,
                body={"store": _FINANCE, "name": backup.name},
            )
        )
        assert resp.status == 200
        assert _body(resp)["ok"] is True
        assert _body(resp)["pending"] is True
        assert _body(resp)["restart_required"] is True
        assert _body(resp)["superseded"] is None
        assert list(db_path.parent.glob(f"{db_path.name}.superseded.*")) == []
        applied = memory_backup.apply_pending_member_restores()
        assert applied[_FINANCE].startswith(f"{db_path.name}.superseded.")
        assert db_path.with_name(applied[_FINANCE]).is_file()
        assert {path.name for path in db_path.parent.glob(f"{db_path.name}.superseded.*")} <= {
            applied[_FINANCE] + suffix for suffix in ("", "-wal", "-shm")
        }

    @pytest.mark.asyncio
    async def test_a_backup_listing_never_carries_a_filesystem_path(self, env) -> None:
        """The name is the handle; a path would disclose the data-home layout."""
        env.declare(_FINANCE)
        db_path = env.seed_vector_file(_FINANCE, _SILO_KEY)
        taken = datetime(2026, 9, 9, 12, 34, 56, 789012, tzinfo=timezone.utc)
        assert memory_backup.backup_store(db_path, now=taken) is not None
        resp = await memory_admin.api_memory_backups(
            _request(
                "GET", "/api/memory/backups", env.state(), query={"store": _FINANCE}, owner=True
            )
        )
        assert resp.status == 200
        rows = _body(resp)["backups"]
        assert len(rows) == 1
        assert rows[0]["name"].startswith(f"{db_path.stem}.")
        assert rows[0]["taken_at"] == "2026-09-09T12:34:56Z"
        # The whole wire shape, so a path field cannot be added without failing here.
        assert set(rows[0]) == {"name", "size_bytes", "taken_at"}
        assert str(env.home) not in _text(resp)
        assert memory_stores.MEMORY_STORES_DIR_NAME not in _text(resp)


# ── 9. One damaged store must not hide the healthy ones ───────────────────────


class TestTheStoreListSurvivesOneDamagedStore:
    """``GET /api/memory/stores`` is best-effort PER STORE.

    One silo with a corrupt file, a stale WAL sibling or a permission problem must
    not cost the operator the whole picker -- it is the surface they would reach for
    precisely when a store has gone wrong.
    """

    _BROKEN = "broken"

    def _damage(self, env) -> Path:
        """Plant a file that is not a database at *broken*'s own path.

        The path is asserted to sit under this test's own home BEFORE the write.
        This is the exact operation that once truncated a real 36 MB store, and the
        only thing standing between the two cases is where the path resolves.
        """
        path = resolve_store_path(self._BROKEN)
        assert env.home in path.parents, path
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(b"this is not a database")
        return path

    @pytest.mark.asyncio
    async def test_an_unreadable_store_does_not_hide_the_healthy_ones(self, env) -> None:
        env.declare(_FINANCE, self._BROKEN)
        env.seed_vector_file(DEFAULT_MEMORY_STORE, _GLOBAL_KEY)
        env.seed_vector_file(
            _FINANCE,
            _SILO_KEY,
            episodes=(_EPISODE,),
            lessons=("Rebase before merging.",),
        )
        self._damage(env)

        resp = await memory_admin.api_memory_stores(
            _request("GET", "/api/memory/stores", env.state(), owner=True)
        )
        assert resp.status == 200
        rows = {row["name"]: row for row in _body(resp)["stores"]}
        # The order is the shared enumeration's, not a re-sort: two passes over one
        # install that disagree on order stop describing the same list.
        assert [row["name"] for row in _body(resp)["stores"]] == declared_store_names()

        assert rows[self._BROKEN]["exists"] is False
        # NULL counts, not zeros: zero reads as "this store is empty", and the
        # operator would go looking for the memory rather than for the file.
        assert rows[self._BROKEN]["semantic_count"] is None
        assert rows[self._BROKEN]["episodic_count"] is None
        assert rows[self._BROKEN]["lessons_count"] is None
        assert rows[self._BROKEN]["lineage"] is None

        assert rows[DEFAULT_MEMORY_STORE]["is_default"] is True
        assert rows[DEFAULT_MEMORY_STORE]["lineage"] == "v1"
        assert rows[DEFAULT_MEMORY_STORE]["facets_supported"] is False
        assert rows[DEFAULT_MEMORY_STORE]["semantic_count"] == 1

        assert rows[_FINANCE]["exists"] is True
        assert rows[_FINANCE]["lineage"] == "crew"
        assert rows[_FINANCE]["facets_supported"] is True
        assert rows[_FINANCE]["episodic_count"] == 1
        # A lesson IS a semantic row, so ``lessons_count`` is a subset of
        # ``semantic_count`` rather than a fourth partition.
        assert rows[_FINANCE]["lessons_count"] == 1
        assert rows[_FINANCE]["semantic_count"] == 2

    @pytest.mark.asyncio
    async def test_the_listing_is_owner_gated_with_no_store_parameter_to_carry(self, env) -> None:
        """It enumerates every silo, so there is no reading of it that is not the
        operator's -- and it takes no ``?store=`` to make the gate fire."""
        env.declare(_FINANCE)
        resp = await memory_admin.api_memory_stores(
            _request("GET", "/api/memory/stores", env.state())
        )
        assert resp.status == 403
        assert _body(resp)["code"] == "owner_only"
        assert _FINANCE not in _text(resp)
