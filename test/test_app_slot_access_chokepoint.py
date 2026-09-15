"""``_deny_cross_app_slot_access`` refuses channel-backed slots for app callers.

The chokepoint guards every route that resolves a slot's transcript through
``slot_history_key`` (detail's transcript read first among them), so an app
owning a channel-backed slot must get the same anti-enumeration 404 there as a
slot it does not own -- otherwise the read route hands over the same foreign
conversation the send/export/fork/rewind boundaries refuse.
"""

from __future__ import annotations

import json
from types import SimpleNamespace

import pytest

from kiro_crew.dashboard.chat_handlers import _deny_cross_app_slot_access


@pytest.fixture(autouse=True)
def _quiet_sel(monkeypatch):
    """The gate audits every refusal; keep the SEL singleton out of these unit tests."""
    from unittest.mock import MagicMock

    from kiro_crew.dashboard import chat_handlers

    monkeypatch.setattr(chat_handlers, "sel", lambda: MagicMock())


def _slot(app="", linked="", channel_origin=False):
    # An unbound channel-born slot keeps the channel transcript's own stem as
    # its name -- that is how ``slot_transcript_key`` finds the transcript.
    key = "slack_1700000000.000100" if channel_origin else "s"
    return SimpleNamespace(
        _app=app, linked_session_key=linked, channel_origin=channel_origin, key=key
    )


def _request(app=""):
    return SimpleNamespace(get=lambda k, default="": app if k == "app" else default)


def _body(resp):
    return json.loads(resp.body)


def test_an_app_is_refused_its_own_channel_linked_slot() -> None:
    resp = _deny_cross_app_slot_access(
        _request("my-app"), _slot(app="my-app", linked="slack:1700000000.000100"), "s", "op"
    )
    assert resp is not None and resp.status == 404
    assert _body(resp)["code"] == "slot_not_found"


def test_an_app_is_refused_its_own_channel_origin_slot() -> None:
    slot = _slot(app="my-app", channel_origin=True)
    resp = _deny_cross_app_slot_access(_request("my-app"), slot, slot.key, "op")
    assert resp is not None and resp.status == 404
    assert _body(resp)["code"] == "slot_not_found"


def test_the_refusal_is_indistinguishable_from_the_not_owned_answer() -> None:
    slot = _slot(app="my-app", channel_origin=True)
    backed = _deny_cross_app_slot_access(_request("my-app"), slot, slot.key, "op")
    foreign = _deny_cross_app_slot_access(_request("my-app"), _slot(app="other-app"), "s", "op")
    assert backed is not None and foreign is not None
    assert backed.status == foreign.status
    assert backed.body == foreign.body


def test_an_app_still_passes_on_its_own_plain_slot() -> None:
    assert _deny_cross_app_slot_access(_request("my-app"), _slot(app="my-app"), "s", "op") is None


def test_the_dashboard_owner_passes_on_a_channel_linked_slot() -> None:
    slot = _slot(app="my-app", linked="slack:1700000000.000100")
    assert _deny_cross_app_slot_access(_request(""), slot, "s", "op") is None


def test_the_cancel_routes_opt_out_of_the_channel_backed_refusal() -> None:
    slot = _slot(app="my-app", linked="slack:1700000000.000100")
    assert (
        _deny_cross_app_slot_access(_request("my-app"), slot, "s", "op", allow_channel_backed=True)
        is None
    )


class TestChokepointMatchesTheOwnershipGate:
    """The chokepoint and ``_check_slot_app_ownership`` (the gate the send,
    continue, summary, resume and source-links routes go through) must refuse
    the same set of slots with a byte-identical 404, or a route's answer would
    tell an app which of its slots carry a channel link."""

    @pytest.mark.parametrize(
        ("slot", "key"),
        [
            (_slot(app="my-app", linked="cron:job-1"), "s"),
            # An unbound channel-born slot keeps the channel transcript's own
            # stem as its name -- that is how ``slot_transcript_key`` finds it.
            (_slot(app="my-app", channel_origin=True), "slack_1700000000.000100"),
            (_slot(app="other-app"), "s"),
            (_slot(app=""), "s"),
        ],
    )
    def test_both_refuse_with_the_same_body(self, slot, key, monkeypatch) -> None:
        from unittest.mock import MagicMock

        from kiro_crew.dashboard import chat_handlers

        monkeypatch.setattr(chat_handlers, "sel", lambda: MagicMock())
        slot.key = key
        choke = _deny_cross_app_slot_access(_request("my-app"), slot, key, "op")
        gate = chat_handlers._check_slot_app_ownership(slot, key, "my-app", "op")
        assert choke is not None and gate is not None
        assert choke.status == gate.status == 404
        assert choke.body == gate.body

    def test_both_pass_a_plain_owned_slot_and_the_dashboard(self) -> None:
        from kiro_crew.dashboard import chat_handlers

        plain = _slot(app="my-app")
        plain.key = "s"
        assert _deny_cross_app_slot_access(_request("my-app"), plain, "s", "op") is None
        assert chat_handlers._check_slot_app_ownership(plain, "s", "my-app", "op") is None
        linked = _slot(app="my-app", linked="cron:job-1")
        linked.key = "s"
        assert _deny_cross_app_slot_access(_request(""), linked, "s", "op") is None
        assert chat_handlers._check_slot_app_ownership(linked, "s", "", "op") is None


class TestDenialSurvivesAnAuditFailure:
    """A refusal must be a 404 even when the audit cannot be written. ``sel()``
    retries a failed SEL init on the caller's thread and can raise; if that
    raise escaped a denial arm the app would see a 500 for a slot it does not
    own and a 404 for a slot that does not exist -- the enumeration signal the
    404 hides. Every app-isolation denial goes through one helper that holds
    the audit best-effort, so this is pinned once per entry point."""

    @staticmethod
    def _sel_raises(monkeypatch):
        from kiro_crew.dashboard import chat_handlers

        def _boom():
            raise RuntimeError("SEL init failed")

        monkeypatch.setattr(chat_handlers, "sel", _boom)

    @pytest.mark.parametrize(
        "slot",
        [
            _slot(app="other-app"),
            _slot(app=""),
            _slot(app="my-app", linked="cron:job-1"),
            _slot(app="my-app", channel_origin=True),
        ],
    )
    def test_chokepoint_and_gate_still_answer_404(self, slot, monkeypatch) -> None:
        from kiro_crew.dashboard import chat_handlers

        self._sel_raises(monkeypatch)
        choke = _deny_cross_app_slot_access(_request("my-app"), slot, slot.key, "op")
        gate = chat_handlers._check_slot_app_ownership(slot, slot.key, "my-app", "op")
        missing = chat_handlers._slot_not_found()
        assert choke is not None and gate is not None
        assert choke.status == gate.status == 404
        assert choke.body == gate.body == missing.body

    def test_cancel_opt_out_and_post_await_recheck_still_answer_404(self, monkeypatch) -> None:
        from kiro_crew.dashboard import chat_handlers

        self._sel_raises(monkeypatch)
        missing = chat_handlers._slot_not_found()
        foreign = _slot(app="other-app")
        cancel = _deny_cross_app_slot_access(
            _request("my-app"), foreign, "s", "op", allow_channel_backed=True
        )
        assert cancel is not None and cancel.status == 404 and cancel.body == missing.body
        # The slot under ``name`` is a different object from the one authorized.
        state = SimpleNamespace(_slots={"s": _slot(app="my-app")})
        replaced = chat_handlers._reauthorize_after_await(
            state, _slot(app="my-app"), "s", "my-app", "op"
        )
        assert replaced is not None and replaced.status == 404
        assert replaced.body == missing.body

    @pytest.mark.asyncio
    async def test_fresh_resume_still_answers_404(self, tmp_path, monkeypatch) -> None:
        from aiohttp import web
        from aiohttp.test_utils import TestClient, TestServer
        from chat_test_helpers import _make_app, _make_state

        monkeypatch.setattr("kiro_crew.dashboard.state.config_dir", lambda: tmp_path)
        state = _make_state(tmp_path)
        state.conversation_log.append("dashboard:userconv", "user", "private")
        self._sel_raises(monkeypatch)
        app = _make_app(state)

        @web.middleware
        async def _as_app(request, handler):
            request["app"] = "my-app"
            return await handler(request)

        app.middlewares.insert(0, _as_app)
        async with TestClient(TestServer(app)) as client:
            resp = await client.post(
                "/api/chat/slots/userconv/resume", json={"key": "dashboard:userconv"}
            )
            assert resp.status == 404
            assert await resp.json() == {"error": "not found", "code": "slot_not_found"}
        assert "userconv" not in state._slots


@pytest.mark.asyncio
async def test_detail_refuses_when_the_bind_lands_during_its_reads(tmp_path, monkeypatch):
    """The chokepoint's check is synchronous and the detail route awaits many
    times after it (disk reads, the render). A channel/cron injection binding
    the slot during one of them hydrates the in-memory window from the
    channel; the response must not carry it."""
    from unittest.mock import MagicMock

    from aiohttp import web
    from aiohttp.test_utils import TestClient, TestServer
    from chat_test_helpers import _make_app, _make_state

    from kiro_crew.dashboard import chat_handlers
    from kiro_crew.dashboard.state import _ChatSlot

    mock_sel = MagicMock()
    monkeypatch.setattr(chat_handlers, "sel", lambda: mock_sel)
    state = _make_state(tmp_path)
    slot = _ChatSlot("s1")
    slot._app = "my-app"
    slot.append("user", "own message", "msg msg-u")
    state._slots[slot.key] = slot

    class _BindingRenderLock:
        # The render lock is the LAST await before the response leaves, so a
        # bind landing here has slipped past every earlier read.
        async def __aenter__(self):
            slot.linked_session_key = "cron:job-1"

        async def __aexit__(self, *exc):
            return False

    slot._detail_render_lock = _BindingRenderLock()

    app = _make_app(state)

    @web.middleware
    async def _as_app(request, handler):
        request["app"] = "my-app"
        return await handler(request)

    # Outermost, so the helper's owner-defaulting middleware sees the app set.
    app.middlewares.insert(0, _as_app)
    async with TestClient(TestServer(app)) as client:
        resp = await client.get("/api/chat/slots/s1")
        assert resp.status == 404
        assert (await resp.json())["code"] == "slot_not_found"
    denied = [c for c in mock_sel.log_api_access.call_args_list if c[1].get("outcome") == "denied"]
    assert len(denied) == 1
    assert denied[0][1]["operation"] == "slot_detail"
    assert denied[0][1]["source"] == "app_isolation"


# ── The bind side: every live rebind is guarded, and creation never rebinds ──
#
# The request-time guards above are sufficient only because an app-owned
# slot's routing is fixed at creation: nothing may bind ``linked_session_key``
# onto a LIVE slot an app owns. That is enforced at the bind sites by
# ``refuse_app_owned_rebind`` -- a convention a new injector could forget --
# so the two pins below hold it structurally.

#: Where ``linked_session_key`` is legitimately assigned WITHOUT the guard:
#: creation (``get_or_create_slot`` binds the slot it is constructing, before
#: any app can own it) and hydration (a restart re-reading a persisted binding
#: into a slot that never left disk). Every other assignment in the package is
#: a live rebind and must sit in a function that calls the guard.
_UNGUARDED_BIND_SITES = frozenset(
    {
        "dashboard/state.py",
        "dashboard/chat_persistence.py",
    }
)


def test_every_live_linked_session_key_bind_is_guarded() -> None:
    """A ``<slot>.linked_session_key = ...`` outside creation/hydration must
    live in a function whose body calls ``refuse_app_owned_rebind`` (a nested
    helper in the same function counts -- the deleted-job cron route guards
    both of its arms through one local closure)."""
    import ast

    from source_corpus import parsed_candidates, src_root

    offenders: list[str] = []
    for path, _text, tree in parsed_candidates(require_any=("linked_session_key",)):
        rel = path.relative_to(src_root()).as_posix()
        if rel in _UNGUARDED_BIND_SITES:
            continue
        for fn in ast.walk(tree):
            if not isinstance(fn, (ast.FunctionDef, ast.AsyncFunctionDef)):
                continue
            binds = [
                node.lineno
                for node in ast.walk(fn)
                if isinstance(node, ast.Assign)
                and any(
                    isinstance(t, ast.Attribute) and t.attr == "linked_session_key"
                    for t in node.targets
                )
            ]
            if not binds:
                continue
            guarded = any(
                isinstance(node, ast.Call)
                and getattr(node.func, "id", getattr(node.func, "attr", ""))
                == "refuse_app_owned_rebind"
                for node in ast.walk(fn)
            )
            if not guarded:
                offenders.extend(f"{rel}:{line} in {fn.name}()" for line in binds)
    assert not offenders, (
        f"linked_session_key is bound onto a live slot without refuse_app_owned_rebind "
        f"at {offenders}; an app-owned slot's routing must not move after creation "
        f"(see chat_utils.refuse_app_owned_rebind)"
    )


def test_bind_site_allowlist_names_only_creation_and_hydration() -> None:
    """The allowlist above is itself pinned: each listed module must still
    bind ``linked_session_key`` (a stale entry would silently widen the
    exemption to whatever moves into that file), and creation's bind must
    stay inside ``get_or_create_slot``."""
    import ast

    from source_corpus import parsed_candidates, src_root

    seen: dict[str, set[str]] = {}
    for path, _text, tree in parsed_candidates(require_any=("linked_session_key",)):
        rel = path.relative_to(src_root()).as_posix()
        if rel not in _UNGUARDED_BIND_SITES:
            continue
        for fn in ast.walk(tree):
            if not isinstance(fn, (ast.FunctionDef, ast.AsyncFunctionDef)):
                continue
            for node in ast.walk(fn):
                if isinstance(node, ast.Assign) and any(
                    isinstance(t, ast.Attribute) and t.attr == "linked_session_key"
                    for t in node.targets
                ):
                    seen.setdefault(rel, set()).add(fn.name)
    assert set(seen) == set(_UNGUARDED_BIND_SITES), (
        f"allowlisted bind sites drifted: expected {sorted(_UNGUARDED_BIND_SITES)}, "
        f"found binds in {sorted(seen)}"
    )
    assert seen["dashboard/state.py"] == {"get_or_create_slot"}, (
        f"state.py binds linked_session_key outside get_or_create_slot: "
        f"{sorted(seen['dashboard/state.py'])}"
    )


def test_get_or_create_slot_never_rebinds_an_existing_app_slot(tmp_path) -> None:
    """The constructor's channel-stem auto-bind runs on CREATION only. An
    existing, unlinked app-owned slot named like a channel stem stays unbound
    when the session map later learns that stem and a request-time route
    asks for the slot again by name -- ``get_or_create_slot`` returns the
    existing slot before it reaches the bind."""
    from chat_test_helpers import _make_state

    from kiro_crew.history import _safe_key

    channel_key = "slack:1783733803.877979"
    stem = _safe_key(channel_key)
    state = _make_state(tmp_path)
    # The map does not know the stem yet: the app mints a plain slot.
    state.sessions.channel_key_for_stem = lambda _stem: ""
    slot = state.get_or_create_slot(stem, app="my-app")
    assert slot._app == "my-app"
    assert slot.linked_session_key == ""

    # The channel thread appears; every later request-time lookup by name
    # returns the SAME slot, still unbound.
    state.sessions.channel_key_for_stem = lambda _stem: channel_key
    for app in ("my-app", ""):
        again = state.get_or_create_slot(stem, app=app)
        assert again is slot
        assert again.linked_session_key == ""
        assert again._app == "my-app"

    # Control: a slot CREATED once the map knows the stem IS auto-bound -- the
    # creation-time behaviour the pin above distinguishes from.
    del state._slots[stem]
    fresh = state.get_or_create_slot(stem, app="")
    assert fresh is not slot
    assert fresh.linked_session_key == channel_key
