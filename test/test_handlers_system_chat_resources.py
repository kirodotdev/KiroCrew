"""Tests for the chat resource monitor API (task 3.1).

Two routes are covered:

* ``GET /api/system/chat-resources`` — the authenticated snapshot endpoint. The
  behaviours a naive handler gets wrong and that this task owns:
  - the payload is snake_case JSON and each entry's ``pids`` is a SORTED list, not
    the frozenset the dataclass carries (a set is not JSON-serializable, and a
    churning order defeats client diffing);
  - the sampler is a MODULE SINGLETON, so rapid polls share ONE walk — the sampler
    is asked for a snapshot once per poll, and the sampler's own cache decides
    fresh-vs-cached (Req 4.3);
  - a sampler exception becomes a JSON 500, never a crash;
  - an unauthenticated browser request is refused by the shared dashboard auth
    middleware, exactly like the sibling ``/api/system`` GET (Req 4.2).

* ``POST /api/spawn/cancel`` — the pinned per-run subagent stop the monitor's
  subagent rows call. Happy path delegates to ``SubagentManager.cancel`` and
  returns ``{"ok": true, "cancelled": bool}``; a missing ``agent_id`` is a 400.

The sampler and subagent manager are faked so the tests are hermetic and never
touch the host's real process table.
"""

from __future__ import annotations

from dataclasses import dataclass, field

import pytest
from aiohttp import web
from aiohttp.test_utils import TestClient, TestServer

from kiro_crew.dashboard import handlers_system as hs


# ── fakes ────────────────────────────────────────────────────────────────────
@dataclass
class _FakeEntry:
    """A minimal stand-in shaped like ``resource_monitor.EntrySample``.

    Only the fields the serializer touches are needed; ``pids`` is deliberately an
    UNSORTED frozenset so the sort-to-list behaviour is actually exercised.
    """

    kind: str = "chat"
    session_key: str = "dashboard:chat-1"
    label: str = "My chat"
    agent: str = "kirocrew"
    pid: int = 4242
    proc_count: int = 3
    rss_mb: float | None = 128.0
    cpu_pct: float | None = None
    uptime_s: float | None = 12.5
    slot: str = "chat-1"
    subagent_id: str = ""
    pids: frozenset[int] = field(default_factory=lambda: frozenset({9, 4242, 17}))


@dataclass
class _FakeSnapshot:
    """A stand-in shaped like ``resource_monitor.ResourceSnapshot``."""

    entries: list = field(default_factory=lambda: [_FakeEntry()])
    posture: str = "ample"
    available_gb: float = 12.0
    host_total_gb: float | None = 16.0
    cpu_count: int | None = 8
    cgroup_used_gb: float | None = None
    cgroup_limit_gb: float | None = None
    sampling_supported: bool = True
    captured_at: float = 1234.5
    interval_s: float = 2.0


class _FakeSampler:
    """Records how many times ``snapshot()`` is invoked and returns a fixed result.

    The count is the assertion for the caching contract: the handler asks the
    sampler once per HTTP request, and the sampler (not the handler) owns the
    fresh-vs-cached decision — so a real sampler under this handler walks /proc at
    most once per interval no matter how fast the page polls.
    """

    def __init__(self) -> None:
        self.calls = 0

    async def snapshot(self) -> _FakeSnapshot:
        self.calls += 1
        return _FakeSnapshot()


class _BoomSampler:
    async def snapshot(self):
        raise RuntimeError("proc walk exploded")


class _FakeSubagents:
    """A stand-in ``SubagentManager`` exposing only ``cancel``."""

    def __init__(self, *, cancelled: bool = True) -> None:
        self._cancelled = cancelled
        self.cancel_calls: list[str] = []

    async def cancel(self, agent_id: str) -> bool:
        self.cancel_calls.append(agent_id)
        return self._cancelled


class _FakeState:
    def __init__(self, *, subagents=None) -> None:
        self.subagents = subagents


def _app(state: _FakeState) -> web.Application:
    app = web.Application()
    app["state"] = state
    return app


class _Req(dict):
    """Minimal request: the handler reads ``request.app["state"]`` and the
    token-auth middleware's ``request["app"]`` marker (``""`` for a dashboard
    user, the app name for an app token)."""

    def __init__(self, app: web.Application, *, caller_app: str = "") -> None:
        super().__init__()
        self._app = app
        self["app"] = caller_app

    @property
    def app(self) -> web.Application:
        return self._app


@pytest.fixture(autouse=True)
def _reset_singleton():
    hs._chat_resource_sampler = None
    yield
    hs._chat_resource_sampler = None


# ── GET /api/system/chat-resources ─────────────────────────────────────────────
@pytest.mark.asyncio
async def test_authenticated_snapshot_shape(monkeypatch):
    """A 200 carries snake_case keys and pids serialized as a SORTED list."""
    sampler = _FakeSampler()
    monkeypatch.setattr(hs, "_get_chat_resource_sampler", lambda state: sampler)

    resp = await hs.api_chat_resources(_Req(_app(_FakeState())))
    assert resp.status == 200
    import json

    data = json.loads(resp.text)

    # snake_case top-level keys with captured_at + interval_s present (Req 4.4).
    assert data["captured_at"] == 1234.5
    assert data["interval_s"] == 2.0
    assert data["sampling_supported"] is True
    assert data["posture"] == "ample"

    entry = data["entries"][0]
    assert set(entry) >= {"kind", "session_key", "rss_mb", "cpu_pct", "proc_count", "slot", "pids"}
    assert entry["kind"] == "chat"
    assert entry["cpu_pct"] is None  # None survives as null, not 0
    # pids is a JSON list, sorted — not the unordered frozenset the dataclass held.
    assert entry["pids"] == [9, 17, 4242]


@pytest.mark.asyncio
async def test_cache_respected_across_rapid_requests(monkeypatch):
    """Rapid polls ask the sampler once per request; caching is the sampler's job.

    The handler must not sneak a second sampler in per request (which would reset
    the CPU-delta baseline and defeat the staleness cache). Three quick calls =>
    exactly three snapshot() invocations on the SAME sampler instance, and a real
    sampler bounds those internally.
    """
    sampler = _FakeSampler()
    monkeypatch.setattr(hs, "_get_chat_resource_sampler", lambda state: sampler)

    req = _Req(_app(_FakeState()))
    for _ in range(3):
        resp = await hs.api_chat_resources(req)
        assert resp.status == 200
    assert sampler.calls == 3


@pytest.mark.asyncio
async def test_singleton_is_reused_across_requests():
    """The real accessor builds ONE sampler and reuses it (state carried between calls)."""
    state = _FakeState()
    first = hs._get_chat_resource_sampler(state)
    second = hs._get_chat_resource_sampler(state)
    assert first is second


@pytest.mark.asyncio
async def test_sampler_failure_returns_json_500(monkeypatch):
    """A sampler exception becomes a JSON 500 in the system-routes error shape."""
    monkeypatch.setattr(hs, "_get_chat_resource_sampler", lambda state: _BoomSampler())

    resp = await hs.api_chat_resources(_Req(_app(_FakeState())))
    assert resp.status == 500
    import json

    body = json.loads(resp.text)
    assert "error" in body
    # The dashboard localises on `code`; prose alone is untranslatable.
    assert body["code"] == "resource_snapshot_failed"


@pytest.mark.asyncio
async def test_unauthenticated_request_is_rejected():
    """A browser GET with no token is refused by the shared dashboard middleware.

    Mirrors the sibling ``/api/system`` GET: the route carries no auth logic of its
    own, it inherits ``token_auth_middleware``. Driven end-to-end through the real
    middleware so a future move that stopped guarding this path fails here (Req 4.2).
    """
    from kiro_crew.dashboard.token_auth import token_auth_middleware

    reached = {"n": 0}

    async def _handler(request: web.Request) -> web.Response:
        reached["n"] += 1
        return web.json_response({"ok": True})

    app = web.Application(
        middlewares=[
            token_auth_middleware(
                internal_paths=frozenset(),
                internal_secret="secret",
                local_only=True,
            )
        ]
    )
    app.router.add_get("/api/system/chat-resources", _handler)

    async with TestClient(TestServer(app)) as client:
        resp = await client.get("/api/system/chat-resources")

    assert resp.status in (401, 403)
    assert reached["n"] == 0  # the handler never ran


# ── POST /api/spawn/cancel ─────────────────────────────────────────────────────
def _messaging_req(app: web.Application, body, *, caller_app: str = ""):
    """A request whose ``.json()`` returns *body*, whose app carries state, and
    whose ``["app"]`` marker is the middleware's (``""`` = dashboard user)."""

    class _R(dict):
        def __init__(self) -> None:
            super().__init__()
            self._app = app
            self["app"] = caller_app

        @property
        def app(self):
            return self._app

        async def json(self):
            if isinstance(body, Exception):
                raise body
            return body

    return _R()


@pytest.mark.asyncio
async def test_spawn_cancel_happy_path():
    """A valid agent_id delegates to SubagentManager.cancel and echoes the bool."""
    from kiro_crew.dashboard.handlers import messaging

    subs = _FakeSubagents(cancelled=True)
    app = _app(_FakeState(subagents=subs))

    resp = await messaging.api_spawn_cancel(_messaging_req(app, {"agent_id": "agent-7"}))
    assert resp.status == 200
    import json

    assert json.loads(resp.text) == {"ok": True, "cancelled": True}
    assert subs.cancel_calls == ["agent-7"]


@pytest.mark.asyncio
async def test_spawn_cancel_reports_already_finished_as_false():
    """An already-done run cancels to false but is still a well-formed 200."""
    from kiro_crew.dashboard.handlers import messaging

    subs = _FakeSubagents(cancelled=False)
    app = _app(_FakeState(subagents=subs))

    resp = await messaging.api_spawn_cancel(_messaging_req(app, {"agent_id": "agent-9"}))
    assert resp.status == 200
    import json

    assert json.loads(resp.text) == {"ok": True, "cancelled": False}


@pytest.mark.asyncio
async def test_spawn_cancel_missing_agent_id_is_400():
    """A missing/blank agent_id is a 400 and never reaches cancel."""
    from kiro_crew.dashboard.handlers import messaging

    subs = _FakeSubagents()
    app = _app(_FakeState(subagents=subs))

    for body in ({}, {"agent_id": ""}, {"agent_id": None}):
        resp = await messaging.api_spawn_cancel(_messaging_req(app, body))
        assert resp.status == 400
    assert subs.cancel_calls == []


@pytest.mark.asyncio
async def test_spawn_cancel_without_subagents_is_503():
    """No subagent manager => 503, matching stop-all's unavailable code."""
    from kiro_crew.dashboard.handlers import messaging

    app = _app(_FakeState(subagents=None))
    resp = await messaging.api_spawn_cancel(_messaging_req(app, {"agent_id": "x"}))
    assert resp.status == 503


# ── chat title enrichment + dedicated-subagent matching ───────────────────────
class _FakeSlot:
    def __init__(self, display_title: str) -> None:
        self.display_title = display_title


class _SlotState(_FakeState):
    """A state whose ``get_slot`` answers from a fixed mapping."""

    def __init__(self, slots: dict[str, _FakeSlot]) -> None:
        super().__init__()
        self._slots = slots

    def get_slot(self, name: str):
        return self._slots.get(name)


def test_serialize_replaces_chat_label_with_slot_title():
    """A chat entry is labelled with the slot's CURRENT display title, read per
    response (not baked into the sampler's cached snapshot), so the monitor shows
    the name the user knows rather than an internal session key."""
    snap = _FakeSnapshot(entries=[_FakeEntry(label="dashboard:chat-1", slot="chat-1")])
    state = _SlotState({"chat-1": _FakeSlot("Fix the flaky login test")})
    data = hs._serialize_snapshot(snap, state)
    assert data["entries"][0]["label"] == "Fix the flaky login test"


def test_serialize_keeps_sampler_label_when_slot_is_unknown():
    """No slot (gone, or under construction) → the sampler's label survives."""
    snap = _FakeSnapshot(entries=[_FakeEntry(label="dashboard:chat-1", slot="chat-1")])
    data = hs._serialize_snapshot(snap, _SlotState({}))
    assert data["entries"][0]["label"] == "dashboard:chat-1"
    # Non-chat rows are never relabelled, even when a same-named slot exists.
    snap = _FakeSnapshot(entries=[_FakeEntry(kind="gateway", label="gateway", slot="chat-1")])
    data = hs._serialize_snapshot(snap, _SlotState({"chat-1": _FakeSlot("X")}))
    assert data["entries"][0]["label"] == "gateway"


class _Info:
    def __init__(self, pid, *, done=False, reaped=False, sharing=False) -> None:
        self._pid = pid
        self.done = done
        self.reaped = reaped
        self._session_sharing = sharing


class _Roster:
    def __init__(self, infos) -> None:
        self.all_agents = infos


class _Rt:
    def __init__(self, pid) -> None:
        self.pid = pid


def test_subagent_lookup_matches_only_live_dedicated_records():
    """A finished run whose pid the kernel recycled, or a session-sharing record
    (whose pid is the HOST runtime's), must not claim a live runtime — that would
    relabel a chat/worker as a subagent and offer a Stop that cancels the wrong
    thing. Only a live, unreaped, dedicated record matches."""
    live = _Info(500)
    roster = _Roster(
        [
            _Info(500, done=True),  # finished: pid may be recycled
            _Info(500, reaped=True),
            _Info(500, sharing=True),  # shared: pid is the host runtime's
            live,
        ]
    )
    lookup = hs._subagent_lookup_for(_FakeState(subagents=roster))
    assert lookup(_Rt(500)) is live
    # With only the stale/shared records present, nothing matches.
    stale = _Roster([_Info(500, done=True), _Info(500, sharing=True)])
    lookup = hs._subagent_lookup_for(_FakeState(subagents=stale))
    assert lookup(_Rt(500)) is None
    assert lookup(_Rt(501)) is None


# ── app-token isolation ───────────────────────────────────────────────────────
class _RecordingSel:
    def __init__(self) -> None:
        self.events: list[dict] = []

    def log_api_access(self, **kw) -> None:
        self.events.append(kw)


@pytest.mark.asyncio
async def test_app_token_cannot_read_the_snapshot(monkeypatch):
    """An app whose ``permissions.api`` grants ``/api/system`` reaches every child
    route by prefix; the snapshot names every chat's title and every subagent's
    task, so an app caller is refused with an audited 403 before any sampling."""
    rec = _RecordingSel()
    monkeypatch.setattr(hs, "sel", lambda: rec)
    sampler = _FakeSampler()
    monkeypatch.setattr(hs, "_get_chat_resource_sampler", lambda state: sampler)

    resp = await hs.api_chat_resources(_Req(_app(_FakeState()), caller_app="some-app"))
    assert resp.status == 403
    import json

    assert json.loads(resp.text)["code"] == "app_token_forbidden"
    assert sampler.calls == 0
    assert rec.events and rec.events[0]["outcome"] == "denied"
    assert rec.events[0]["caller"] == "some-app"


@pytest.mark.asyncio
async def test_app_token_cannot_cancel_a_subagent(monkeypatch):
    """Same isolation on the cancel route: an app granted ``/api/spawn`` must not
    cancel a run it did not start (mirrors ``stop-all``)."""
    from kiro_crew.dashboard.handlers import messaging

    rec = _RecordingSel()
    monkeypatch.setattr(messaging, "_sel", lambda: rec)
    subs = _FakeSubagents()
    app = _app(_FakeState(subagents=subs))

    resp = await messaging.api_spawn_cancel(
        _messaging_req(app, {"agent_id": "agent-7"}, caller_app="some-app")
    )
    assert resp.status == 403
    import json

    assert json.loads(resp.text)["code"] == "app_token_forbidden"
    assert subs.cancel_calls == []
    assert rec.events and rec.events[0]["outcome"] == "denied"


def test_serialize_redacts_every_label_not_just_chat_titles():
    """A subagent's task text (and any other label) is externally sourced prose and
    can carry a pasted credential; it must pass the redaction chain before egress."""
    secret = "AKIAIOSFODNN7EXAMPLE"
    snap = _FakeSnapshot(
        entries=[
            _FakeEntry(
                kind="subagent", label=f"rotate key {secret} now", slot="", subagent_id="a1"
            ),
            _FakeEntry(kind="worker", label=f"pool token={secret}", slot=""),
        ]
    )
    data = hs._serialize_snapshot(snap, _FakeState())
    for entry in data["entries"]:
        assert secret not in entry["label"]
        assert entry["label"]  # redacted, not blanked


def test_serialize_carries_the_slots_stop_state_as_stop_pending():
    """A chat row says whether its slot has a stop in flight, so the page can keep
    the Stop control locked: a second press while a soft cancel is pending is
    the hard-kill escalation, which also drops the slot's queued prompts."""

    class _StopSlot(_FakeSlot):
        def __init__(self, title: str, stop_state: str) -> None:
            super().__init__(title)
            self._stop_state = stop_state

    state = _SlotState(
        {
            "pending": _StopSlot("Pending", "soft_pending"),
            "killing": _StopSlot("Killing", "killing"),
            "idle": _StopSlot("Idle", "idle"),
        }
    )
    snap = _FakeSnapshot(
        entries=[
            _FakeEntry(label="k1", slot="pending"),
            _FakeEntry(label="k2", slot="killing"),
            _FakeEntry(label="k3", slot="idle"),
            _FakeEntry(label="k4", slot="missing"),
            _FakeEntry(kind="worker", label="w"),
        ]
    )
    out = {e["label"]: e["stop_pending"] for e in hs._serialize_snapshot(snap, state)["entries"]}
    assert out == {"Pending": True, "Killing": True, "Idle": False, "k4": False, "w": False}
