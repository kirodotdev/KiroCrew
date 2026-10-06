"""A ``cron:`` caller on ``POST /api/chat/mode`` sets one posture on one slot it created.

``ScriptContext.set_session_mode`` is the cron path to ``api_chat_mode``
(``src/kiro_crew/dashboard/chat_handlers.py``). The handler holds a caller that
presents an attested ``cron:<job id>`` key to three rules, each audited:

1. the mode is ``trust`` or ``trust_reads``; ``yolo``, ``normal`` and anything
   else answer ``403 mode_not_allowed`` before governance or the safety override
   is consulted;
2. a slot is named; the owner's all-slots request answers ``400 slot_required``;
3. the slot was created by that same cron, live, or the call answers ``403
   not_creator`` through the creator fence the chat routes already apply.

An admitted call is the ordinary slot-scoped grant, recorded under the cron's own
key, and leaves a live global grant alone exactly as the owner's slot-scoped
``trust`` does. Owner and app callers are not judged by any of the three rules.

Every test drives the real handler through an aiohttp ``TestClient``. The auth
middleware is stood in by a cron or an owner request, the attested-key read is
stood in by the header, and ``safety_override`` is a recording fake so a refusal
can prove it was never touched.
"""

from __future__ import annotations

from types import SimpleNamespace
from unittest.mock import MagicMock, patch

import pytest
from aiohttp import web
from aiohttp.test_utils import TestClient, TestServer
from chat_test_helpers import _make_state

from kiro_crew.dashboard import chat_handlers
from kiro_crew.dashboard.chat_handlers import api_chat_mode
from kiro_crew.dashboard.state import SlotOrigin
from kiro_crew.safety_override import reset_singleton

_JOB = "nightly-dispatcher"
_CRON_KEY = f"cron:{_JOB}"
_OTHER_CRON_KEY = "cron:weekly-digest"


@web.middleware
async def _internal_request(request: web.Request, handler):
    """Stand in for the internal-auth branch of the auth middleware.

    The secret validated and no app was derived, so ``request["app"]`` stays
    absent as it does for a person's own cron. The session key is whatever the
    test sent in ``X-Session-Key``.
    """
    request["internal_auth"] = True
    request["peer_verified"] = True
    return await handler(request)


@web.middleware
async def _dashboard_owner_request(request: web.Request, handler):
    """Stand in for an owner's dashboard request (no owner configured, so the
    local bootstrap subject passes ``deny_non_dashboard_caller``)."""
    request["app"] = ""
    request["user"] = "local-app"
    return await handler(request)


async def _cron_slot_creator_from_header(request: web.Request) -> str:
    """The attested-key read, stood in by the header.

    The real ``cron_slot_creator`` reads the scope the chat-route gate resolved
    from the signed token; ``test_chat_routes_cron_switch.py`` and the booted
    gateway tests pin that read. Here the header is the attestation.
    """
    if request.get("internal_auth") is not True:
        return ""
    key = request.headers.get("X-Session-Key", "")
    return key if key.startswith("cron:") else ""


def _make_mode_app(state, middleware) -> web.Application:
    app = web.Application(middlewares=[middleware])
    app["state"] = state
    app.router.add_post("/api/chat/mode", api_chat_mode)
    return app


class _FakeOverride:
    """Recording stand-in for the SafetyOverride singleton."""

    def __init__(self, *, active: bool = False) -> None:
        self.active = active
        self.activate_calls: list[str] = []
        self.deactivate_calls: list[str] = []
        self.is_declared = False

    def activate(self, source: str) -> _FakeOverride:
        self.activate_calls.append(source)
        self.active = True
        return self

    def deactivate(self, source: str) -> None:
        self.deactivate_calls.append(source)
        self.active = False

    def is_active(self) -> bool:
        return self.active


@pytest.fixture(autouse=True)
def _hermetic_home(tmp_path, monkeypatch):
    monkeypatch.setenv("KIROCREW_HOME", str(tmp_path))


@pytest.fixture(autouse=True)
def _isolate_safety_override():
    reset_singleton()
    yield
    reset_singleton()


@pytest.fixture
def state(tmp_path, monkeypatch):
    monkeypatch.setattr("kiro_crew.dashboard.state.config_dir", lambda: tmp_path)
    st = _make_state(tmp_path)
    st.broadcast_ws = MagicMock()
    st.push_slots_update = MagicMock()
    st.owner_id = ""
    return st


@pytest.fixture
def override(monkeypatch):
    fake = _FakeOverride()
    monkeypatch.setattr(chat_handlers, "safety_override", lambda: fake)
    return fake


@pytest.fixture
def audit(monkeypatch) -> list[dict]:
    """Every SEL access record, from the handler and from the shared refusals."""
    events: list[dict] = []
    recorder = SimpleNamespace(log_api_access=lambda **kw: events.append(kw))
    monkeypatch.setattr(chat_handlers, "sel", lambda: recorder)
    monkeypatch.setattr("kiro_crew.sel.sel", lambda: recorder)
    return events


@pytest.fixture
def attested(monkeypatch):
    monkeypatch.setattr(chat_handlers, "cron_slot_creator", _cron_slot_creator_from_header)


def _cron_client(state) -> TestClient:
    return TestClient(TestServer(_make_mode_app(state, _internal_request)))


def _owner_client(state) -> TestClient:
    return TestClient(TestServer(_make_mode_app(state, _dashboard_owner_request)))


def _cron_slot(state, key: str = "nightly-1", created_by: str = _CRON_KEY):
    slot = state.get_or_create_slot(key, origin=SlotOrigin.CRON)
    slot._created_by = created_by
    return slot


def _owner_slot(state, key: str = "owner-1"):
    return state.get_or_create_slot(key, origin=SlotOrigin.USER)


async def _as_cron(state, body: dict, key: str = _CRON_KEY):
    async with _cron_client(state) as client:
        resp = await client.post("/api/chat/mode", json=body, headers={"X-Session-Key": key})
        return resp.status, await resp.json()


async def _as_owner(state, body: dict):
    async with _owner_client(state) as client:
        resp = await client.post("/api/chat/mode", json=body)
        return resp.status, await resp.json()


def _denials(audit: list[dict]) -> list[dict]:
    return [e for e in audit if e.get("outcome") == "denied"]


def _mode_changes(audit: list[dict]) -> list[dict]:
    return [e for e in audit if str(e.get("operation", "")).startswith("mode_change:")]


# ── admitted: the two slot-scoped trust modes on the cron's own slot ──


@pytest.mark.asyncio
async def test_a_cron_sets_trust_on_the_slot_it_created(state, override, audit, attested):
    slot = _cron_slot(state)

    status, body = await _as_cron(state, {"slot": slot.key, "mode": "trust"})

    assert (status, body) == (200, {"ok": True, "mode": "trust"})
    assert slot._trust is True
    assert slot._trust_reads is False
    state.sessions.set_approval_policy.assert_any_call(f"dashboard:{slot.key}", "auto")
    (event,) = _mode_changes(audit)
    assert event["caller"] == _CRON_KEY
    assert event["operation"] == "mode_change:trust"
    assert event["outcome"] == "enabled"
    assert event["resources"] == slot.key
    assert _denials(audit) == []


@pytest.mark.asyncio
async def test_a_cron_trust_does_not_persist_linked_channel_trust(state, override, audit, attested):
    slot = _cron_slot(state)
    slot._slack_channel = "C1"
    channel = SimpleNamespace(trusted=False, _save=MagicMock())
    state.channel_manager = SimpleNamespace(_channels={"C1": channel})

    status, body = await _as_cron(state, {"slot": slot.key, "mode": "trust"})

    assert (status, body) == (200, {"ok": True, "mode": "trust"})
    assert slot._trust is True
    assert channel.trusted is False
    channel._save.assert_not_called()
    (event,) = _mode_changes(audit)
    assert event["resources"] == slot.key


@pytest.mark.asyncio
async def test_a_cron_sets_trust_reads_on_the_slot_it_created(state, override, audit, attested):
    slot = _cron_slot(state)

    status, body = await _as_cron(state, {"slot": slot.key, "mode": "trust_reads"})

    assert (status, body) == (200, {"ok": True, "mode": "trust_reads"})
    assert slot._trust is False
    assert slot._trust_reads is True
    state.sessions.set_approval_policy.assert_any_call(f"dashboard:{slot.key}", "")
    (event,) = _mode_changes(audit)
    assert event["caller"] == _CRON_KEY
    assert event["operation"] == "mode_change:trust_reads"
    assert event["resources"] == slot.key


@pytest.mark.asyncio
async def test_a_cron_grant_leaves_an_unrelated_slot_alone(state, override, audit, attested):
    mine = _cron_slot(state)
    other = _owner_slot(state)

    status, _ = await _as_cron(state, {"slot": mine.key, "mode": "trust"})

    assert status == 200
    assert mine._trust is True
    assert other._trust is False
    assert other._trust_reads is False


@pytest.mark.asyncio
async def test_a_cron_slot_scoped_trust_leaves_a_live_global_grant_alone(
    state, override, audit, attested
):
    """The issue's standing rule: a slot-scoped trust never revokes the global grant."""
    override.active = True
    slot = _cron_slot(state)

    status, _ = await _as_cron(state, {"slot": slot.key, "mode": "trust"})

    assert status == 200
    assert override.deactivate_calls == []
    assert override.is_active() is True


@pytest.mark.asyncio
@pytest.mark.parametrize("mode", ["trust", "trust_reads"])
async def test_a_cron_slot_scoped_trust_leaves_a_declared_global_grant_alone(
    state, override, audit, attested, mode
):
    """A grant DECLARED in owner config is exempt from the slot-scoped narrowing
    for the owner, so the owner's slot-scoped trust ends it. A cron's never does:
    ``set_session_mode`` promises the global override is left alone, declared or
    not, and a scheduled run is not the operator choosing another mode."""
    override.active = True
    override.is_declared = True
    slot = _cron_slot(state)

    status, body = await _as_cron(state, {"slot": slot.key, "mode": mode})

    assert (status, body) == (200, {"ok": True, "mode": mode})
    assert override.deactivate_calls == []
    assert override.is_active() is True
    assert getattr(slot, f"_{mode}") is True
    (event,) = _mode_changes(audit)
    assert event["caller"] == _CRON_KEY


@pytest.mark.asyncio
async def test_the_owner_slot_scoped_trust_still_ends_a_declared_global_grant(
    state, override, audit, attested
):
    """The contrast that pins the owner's existing behaviour: selecting another
    approval mode is the one documented action that ends a declared grant."""
    override.active = True
    override.is_declared = True
    slot = _owner_slot(state)

    status, _ = await _as_owner(state, {"slot": slot.key, "mode": "trust"})

    assert status == 200
    assert override.deactivate_calls == ["dashboard"]
    assert override.is_active() is False


# ── refused: any other mode, before governance and the override ──


@pytest.mark.asyncio
@pytest.mark.parametrize("mode", ["yolo", "normal", "TRUST", "", "auto"])
async def test_a_cron_is_refused_any_other_mode(state, override, audit, attested, mode):
    slot = _cron_slot(state)
    slot._trust_reads = True

    status, body = await _as_cron(state, {"slot": slot.key, "mode": mode})

    assert status == 403
    assert body["code"] == "mode_not_allowed"
    assert body["ok"] is False
    # Nothing moved: the posture, the policy, the override.
    assert slot._trust is False
    assert slot._trust_reads is True
    assert override.activate_calls == []
    assert override.deactivate_calls == []
    assert _mode_changes(audit) == []
    (denial,) = _denials(audit)
    assert denial["error"] == "mode_not_allowed"
    assert denial["operation"] == "chat.control"
    assert f"slot={slot.key}" in denial["resources"]
    assert f"mode={mode!r}" in denial["resources"]


@pytest.mark.asyncio
async def test_a_cron_yolo_never_reaches_governance(state, override, audit, attested):
    """The cron rule answers first; a policy that permits yolo does not change that."""
    slot = _cron_slot(state)
    policy = MagicMock(return_value=True)
    with patch.object(chat_handlers, "yolo_policy_permits", policy):
        status, body = await _as_cron(state, {"slot": slot.key, "mode": "yolo"})

    assert status == 403
    assert body["code"] == "mode_not_allowed"
    policy.assert_not_called()
    assert override.activate_calls == []


@pytest.mark.asyncio
@pytest.mark.parametrize("mode", [[], ["trust"], {"mode": "trust"}])
async def test_a_cron_sending_a_non_string_mode_is_refused_not_crashed(
    state, override, audit, attested, mode
):
    """A body whose ``mode`` is not even a string is judged by the same rule as
    ``yolo``: the audited 403, never an unhandled ``TypeError`` from hashing it."""
    slot = _cron_slot(state)

    status, body = await _as_cron(state, {"slot": slot.key, "mode": mode})

    assert status == 403
    assert body["code"] == "mode_not_allowed"
    assert slot._trust is False
    assert slot._trust_reads is False
    assert override.deactivate_calls == []
    assert _mode_changes(audit) == []
    (denial,) = _denials(audit)
    assert denial["error"] == "mode_not_allowed"
    assert f"mode={mode!r}" in denial["resources"]


@pytest.mark.asyncio
async def test_a_cron_without_a_mode_is_refused_the_default(state, override, audit, attested):
    """An absent mode is the handler's ``normal``, which a cron may not set."""
    slot = _cron_slot(state)
    slot._trust = True
    state.sessions.set_approval_policy.reset_mock()

    status, body = await _as_cron(state, {"slot": slot.key})

    assert status == 403
    assert body["code"] == "mode_not_allowed"
    assert slot._trust is True
    state.sessions.set_approval_policy.assert_not_called()
    assert override.deactivate_calls == []


# ── refused: no slot ──


@pytest.mark.asyncio
@pytest.mark.parametrize("body", [{"mode": "trust"}, {"mode": "trust", "slot": ""}])
async def test_a_cron_must_name_a_slot(state, override, audit, attested, body):
    a = _cron_slot(state, "nightly-1")
    b = _cron_slot(state, "nightly-2")
    owner = _owner_slot(state)

    status, answer = await _as_cron(state, body)

    assert status == 400
    assert answer["code"] == "slot_required"
    assert answer["ok"] is False
    for slot in (a, b, owner):
        assert slot._trust is False
        assert slot._trust_reads is False
    assert override.deactivate_calls == []
    assert _mode_changes(audit) == []
    (denial,) = _denials(audit)
    assert denial["error"] == "slot_required"


# ── refused: a slot the cron did not create ──


@pytest.mark.asyncio
async def test_a_cron_is_refused_on_the_owners_slot(state, override, audit, attested):
    owner = _owner_slot(state)
    assert owner._created_by == ""
    state.sessions.set_approval_policy.reset_mock()

    status, body = await _as_cron(state, {"slot": owner.key, "mode": "trust"})

    assert status == 403
    assert body["code"] == "not_creator"
    assert owner._trust is False
    state.sessions.set_approval_policy.assert_not_called()
    assert override.deactivate_calls == []
    assert _mode_changes(audit) == []
    (denial,) = _denials(audit)
    assert denial["error"] == "not_creator"
    assert denial["operation"] == "chat.control"
    assert denial["resources"] == f"/api/chat/mode slot={owner.key}"


@pytest.mark.asyncio
async def test_a_cron_is_refused_on_another_jobs_slot(state, override, audit, attested):
    theirs = _cron_slot(state, "weekly-1", created_by=_OTHER_CRON_KEY)

    status, body = await _as_cron(state, {"slot": theirs.key, "mode": "trust_reads"})

    assert status == 403
    assert body["code"] == "not_creator"
    assert theirs._trust_reads is False
    (denial,) = _denials(audit)
    assert denial["error"] == "not_creator"


@pytest.mark.asyncio
async def test_a_cron_naming_a_slot_that_is_not_live_gets_unknown_slot(
    state, override, audit, attested
):
    """The mode route mints nothing, so a dead key is simply unknown here."""
    status, body = await _as_cron(state, {"slot": "never-opened", "mode": "trust"})

    assert status == 400
    assert body == {"ok": False, "error": "unknown slot"}
    assert override.deactivate_calls == []
    assert _denials(audit) == []


@pytest.mark.asyncio
async def test_the_fence_judges_the_slot_the_grant_is_written_to(state, override, audit, attested):
    """The fence runs on the resolved live slot, after the unknown-slot check."""
    owner = _owner_slot(state)
    fence = MagicMock(wraps=chat_handlers.cron_creator_refusal)
    with patch.object(chat_handlers, "cron_creator_refusal", fence):
        status, body = await _as_cron(state, {"slot": owner.key, "mode": "trust"})

    assert (status, body["code"]) == (403, "not_creator")
    fence.assert_called_once()
    args = fence.call_args.args
    assert args[1] is state
    assert args[2] == owner.key
    assert args[3] == _CRON_KEY


# ── everyone else keeps today's behaviour ──


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("mode", "expected_trust", "expected_trust_reads"),
    [("trust", True, False), ("trust_reads", False, True), ("normal", False, False)],
)
async def test_the_owner_still_sets_every_slot_scoped_mode(
    state, override, audit, attested, mode, expected_trust, expected_trust_reads
):
    slot = _owner_slot(state)
    slot._trust = mode == "normal"

    status, body = await _as_owner(state, {"slot": slot.key, "mode": mode})

    assert (status, body) == (200, {"ok": True, "mode": mode})
    assert slot._trust is expected_trust
    assert slot._trust_reads is expected_trust_reads
    assert _denials(audit) == []
    (event,) = _mode_changes(audit)
    assert event["caller"] == "dashboard:mode"
    assert event["operation"] == f"mode_change:{mode}"


@pytest.mark.asyncio
async def test_the_owner_still_sets_a_mode_on_every_slot_at_once(state, override, audit, attested):
    a = _owner_slot(state, "owner-1")
    b = _cron_slot(state, "nightly-1")

    status, body = await _as_owner(state, {"mode": "trust_reads"})

    assert (status, body) == (200, {"ok": True, "mode": "trust_reads"})
    assert a._trust_reads is True
    assert b._trust_reads is True
    assert _denials(audit) == []


@pytest.mark.asyncio
async def test_the_owner_still_reaches_a_cron_created_slot(state, override, audit, attested):
    slot = _cron_slot(state)

    status, _ = await _as_owner(state, {"slot": slot.key, "mode": "trust"})

    assert status == 200
    assert slot._trust is True
    assert _denials(audit) == []


@pytest.mark.asyncio
async def test_the_owner_still_arms_yolo(state, override, audit, attested):
    _owner_slot(state)
    with patch.object(chat_handlers, "yolo_policy_permits", return_value=True):
        status, body = await _as_owner(state, {"mode": "yolo"})

    assert (status, body) == (200, {"ok": True, "mode": "yolo"})
    assert override.activate_calls == ["dashboard"]
    assert _denials(audit) == []
