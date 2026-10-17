"""The bounded live-observation channel behind Mate's ``find_ui`` ``live`` field.

The gateway asks ONE owner tab, over an owner-only ``guide_observe`` frame, what
it shows of a manifest-validated set of curated location ids; the tab answers on
the owner-only ``POST /api/guide/observe``. These tests pin: the same auth and
isolation as the other guide routes, id validation and caps, a reply that can
carry no text, expiry, the ``not_observed`` answers, and the build digest check.
"""

from __future__ import annotations

import asyncio
from typing import Any

import pytest
from test_guide_routes import OWNER, FakeState, _quiet_sel, _run, agent  # noqa: F401

from kiro_crew import guide_catalog
from kiro_crew.dashboard import guide_observe
from kiro_crew.dashboard.guide_observe import ObservationHub, observation_hub_for
from kiro_crew.dashboard.guide_runs import GuideError, GuideStore, guide_store_for

TARGET = "chat.older-sessions"
TOGGLE = "chat.sessions-sidebar-toggle"
SCOPES = ["chat.sessions-drawer", "chat.sessions-sidebar"]
DIGEST = guide_catalog.ui_build_manifest().build_digest


def _reply(p: Any, **over: Any) -> dict[str, Any]:
    body = {
        "tab_id": p.tab_id,
        "request_id": p.request_id,
        "build_digest": DIGEST,
        "document_epoch": "e1",
        "sequence": 1,
        "targets": [{"id": t, "status": "pointable"} for t in p.targets],
        "scopes": [{"id": s, "state": "closed"} for s in p.scopes],
        "predicates": [{"id": x, "state": "met"} for x in p.predicates],
    }
    body.update(over)
    return body


def _hub_run(fn: Any) -> Any:
    async def main() -> Any:
        return fn(ObservationHub(clock=lambda: 50.0))

    return asyncio.run(main())


# ── the hub: what a reply may carry ──


def test_a_well_formed_reply_resolves_the_request_with_ids_and_states_only() -> None:
    def go(hub: ObservationHub) -> Any:
        p = hub.begin(
            tab_id="tab-1",
            targets=(TARGET,),
            scopes=("chat.sessions-sidebar",),
            build_digest=DIGEST,
            predicates=("has_open_sessions",),
        )
        hub.deliver(_reply(p))
        return p.future.result()

    assert _hub_run(go) == {
        "status": "observed",
        "observed_at": 50.0,
        "targets": [{"id": TARGET, "status": "pointable"}],
        "scopes": [{"id": "chat.sessions-sidebar", "state": "closed"}],
        "predicates": [{"id": "has_open_sessions", "state": "met"}],
    }


@pytest.mark.parametrize(
    "over",
    [
        # Any field beyond the schema is refused whole: no text can ride along.
        {"label": "Older Sessions"},
        {"targets": [{"id": TARGET, "status": "pointable", "text": "Older Sessions"}]},
        {"targets": [{"id": TARGET, "status": "Older Sessions"}]},
        {"targets": [{"id": "settings.show", "status": "pointable"}]},
        {"targets": [{"id": TARGET, "status": "pointable"}, {"id": TARGET, "status": "hidden"}]},
        {"targets": []},
        {"scopes": [{"id": "chat.sessions-sidebar", "state": "yes"}]},
        {"scopes": [{"id": "chat.sessions-sidebar", "satisfied": True}]},
        {"predicates": [{"id": "has_open_sessions", "state": True}]},
        {"predicates": [{"id": "has_open_sessions", "state": "unmet", "why": "x"}]},
        {"predicates": []},
        {"document_epoch": "has space"},
        {"sequence": -1},
        {"sequence": True},
    ],
)
def test_a_reply_that_is_not_exactly_the_asked_ids_and_enum_states_is_refused(
    over: dict[str, Any],
) -> None:
    def go(hub: ObservationHub) -> Any:
        p = hub.begin(
            tab_id="tab-1",
            targets=(TARGET,),
            scopes=("chat.sessions-sidebar",),
            build_digest=DIGEST,
            predicates=("has_open_sessions",),
        )
        body = _reply(p)
        if "label" in over:
            body["label"] = over["label"]
        else:
            body.update(over)
        with pytest.raises(GuideError) as exc:
            hub.deliver(body)
        return exc.value.status, p.future.done()

    status, done = _hub_run(go)
    assert status == 400 and done is False


def test_a_missing_field_is_refused() -> None:
    def go(hub: ObservationHub) -> int:
        p = hub.begin(tab_id="tab-1", targets=(TARGET,), scopes=(), build_digest=DIGEST)
        body = _reply(p)
        del body["document_epoch"]
        with pytest.raises(GuideError) as exc:
            hub.deliver(body)
        return exc.value.status

    assert _hub_run(go) == 400


def test_a_foreign_late_or_replayed_reply_is_dropped() -> None:
    def go(hub: ObservationHub) -> list[str]:
        codes = []
        p = hub.begin(tab_id="tab-1", targets=(TARGET,), scopes=(), build_digest=DIGEST)
        for body in (_reply(p, tab_id="tab-2"), _reply(p, request_id="o_nope")):
            with pytest.raises(GuideError) as exc:
                hub.deliver(body)
            codes.append(exc.value.code)
        hub.deliver(_reply(p, sequence=5))
        hub.end(p.request_id)
        # The same tab may not answer a later request with an older sequence,
        # nor from another document (a reload has a new tab id anyway).
        for over in ({"sequence": 5}, {"sequence": 9, "document_epoch": "e2"}):
            q = hub.begin(tab_id="tab-1", targets=(TARGET,), scopes=(), build_digest=DIGEST)
            with pytest.raises(GuideError) as exc:
                hub.deliver(_reply(q, **over))
            codes.append(exc.value.code)
            hub.end(q.request_id)
        # Ended: an answer arriving after the wait is expired, and nothing is held.
        r = hub.begin(tab_id="tab-1", targets=(TARGET,), scopes=(), build_digest=DIGEST)
        hub.end(r.request_id)
        with pytest.raises(GuideError) as exc:
            hub.deliver(_reply(r, sequence=10))
        codes.append(exc.value.code)
        assert hub.pending_count() == 0
        return codes

    assert _hub_run(go) == [
        "observation_wrong_tab",
        "observation_expired",
        "observation_stale",
        "observation_stale",
        "observation_expired",
    ]


def test_a_reply_from_another_build_is_not_observed_and_its_states_unused() -> None:
    def go(hub: ObservationHub) -> Any:
        p = hub.begin(tab_id="tab-1", targets=(TARGET,), scopes=(), build_digest=DIGEST)
        hub.deliver(_reply(p, build_digest="sha256:" + "0" * 64))
        return p.future.result()

    assert _hub_run(go) == {"status": "not_observed", "reason": "build_mismatch"}


def test_the_latest_sender_expires() -> None:
    now = [0.0]
    hub = ObservationHub(clock=lambda: now[0])
    hub.note_sender("chat-1", "tab-1")
    hub.note_sender("chat-1", "bad tab")  # control/space characters: ignored
    assert hub.sender_for("chat-1") == "tab-1"
    now[0] = guide_observe.SENDER_TTL_SECONDS + 1
    assert hub.sender_for("chat-1") is None


# ── the routes ──


class TabState(FakeState):
    """A gateway whose one owner tab answers ``guide_observe`` frames."""

    def __init__(self, answer: Any = None) -> None:
        super().__init__()
        self.client: Any = None
        self.answer = answer
        self.replies: list[tuple[int, Any]] = []

    async def deliver_ws_owners(self, kind: str, payload: dict[str, Any]) -> int:
        self.frames.append((kind, payload))
        if kind == "guide_observe" and self.answer is not None:

            async def reply() -> None:
                await asyncio.sleep(0.01)
                body = self.answer(payload)
                r = await self.client.post("/api/guide/observe", json=body, headers=OWNER)
                self.replies.append((r.status, await r.json()))

            asyncio.get_running_loop().create_task(reply())
        return 1


def _tab_reply(payload: dict[str, Any], **over: Any) -> dict[str, Any]:
    body = {
        "tab_id": payload["tab_id"],
        "request_id": payload["request_id"],
        "build_digest": DIGEST,
        "document_epoch": "e1",
        "sequence": 1,
        "targets": [{"id": t, "status": "offscreen"} for t in payload["targets"]],
        "scopes": [{"id": s, "state": "open"} for s in payload["scopes"]],
        "predicates": [{"id": x, "state": "met"} for x in payload["predicates"]],
    }
    body.update(over)
    return body


def _observe(state: TabState, body: dict[str, Any], headers: dict[str, str]) -> tuple[int, Any]:
    async def go(c: Any) -> tuple[int, Any]:
        state.client = c
        r = await c.post("/api/guide/agent/observe", json=body, headers=headers)
        return r.status, await r.json()

    return _run(go, state)


def test_before_start_the_slots_latest_sender_tab_is_asked_and_answers() -> None:
    state = TabState(answer=_tab_reply)
    state.open_slot("chat-1")
    observation_hub_for(state).note_sender("chat-1", "tab-A")
    status, body = _observe(state, {"targets": [TARGET, TOGGLE]}, agent("dashboard:chat-1"))
    assert status == 200, body
    assert body["status"] == "observed"
    assert body["targets"] == [
        {"id": TARGET, "status": "offscreen"},
        {"id": TOGGLE, "status": "offscreen"},
    ]
    # The older-sessions plan's reveal scopes and predicates are asked for too.
    assert [s["id"] for s in body["scopes"]] == SCOPES
    assert [x["id"] for x in body["predicates"]] == ["full_dashboard", "has_open_sessions"]
    ((kind, frame),) = state.frames
    assert kind == "guide_observe"
    assert frame["tab_id"] == "tab-A" and set(frame) == {
        "request_id",
        "tab_id",
        "targets",
        "scopes",
        "predicates",
    }
    assert observation_hub_for(state).pending_count() == 0


def test_after_start_the_guides_owner_tab_is_asked_not_the_latest_sender() -> None:
    state = TabState(answer=_tab_reply)
    state.open_slot("chat-1")
    store = guide_store_for(state)
    g = store.start(
        slot_key="chat-1",
        session_key="dashboard:chat-1",
        actions=[{"id": "ui.show", "params": {"location_id": TARGET}}],
    )
    store.claim(
        guide_id=g["guide_id"], tab_id="tab-owner", revision=g["revision"], placements=["desktop"]
    )
    observation_hub_for(state).note_sender("chat-1", "tab-other")
    status, body = _observe(state, {"targets": [TARGET]}, agent("dashboard:chat-1"))
    assert status == 200 and body["status"] == "observed"
    assert state.frames[-1][1]["tab_id"] == "tab-owner"


def test_with_no_identifiable_tab_the_answer_is_not_observed_and_nothing_is_sent() -> None:
    state = TabState(answer=_tab_reply)
    state.open_slot("chat-1")
    status, body = _observe(state, {"targets": [TARGET]}, agent("dashboard:chat-1"))
    assert (status, body) == (200, {"status": "not_observed", "reason": "no_tab", "ui_lang": ""})
    assert state.frames == []


def test_a_turn_the_user_did_not_send_cannot_observe_their_tab(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A loop wake or an injected turn asks nothing of the tab and leaves the guide alone."""
    from kiro_crew.dashboard.handlers import guide as guide_routes

    monkeypatch.setattr(guide_routes, "OBSERVE_WAIT_SECONDS", 0.05)
    state = TabState(answer=None)
    state.open_slot("chat-1")._turn_user_sent = False
    store = guide_store_for(state)
    g = store.start(
        slot_key="chat-1",
        session_key="dashboard:chat-1",
        actions=[{"id": "ui.show", "params": {"location_id": TARGET}}],
    )
    g = store.claim(
        guide_id=g["guide_id"], tab_id="tab-owner", revision=g["revision"], placements=["desktop"]
    )
    status, body = _observe(state, {"targets": [TARGET]}, agent("dashboard:chat-1"))
    assert (status, body["code"]) == (403, "not_user_turn")
    assert state.frames == []
    held = store.status_for_caller(slot_key="chat-1")
    assert held["reason"] != "stale_tab" and held["revision"] == g["revision"]


def test_a_tab_that_does_not_answer_in_time_is_not_observed_and_marks_its_guide_stale(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from kiro_crew.dashboard.handlers import guide as guide_routes

    monkeypatch.setattr(guide_routes, "OBSERVE_WAIT_SECONDS", 0.05)
    state = TabState(answer=None)
    state.open_slot("chat-1")
    store = guide_store_for(state)
    g = store.start(
        slot_key="chat-1",
        session_key="dashboard:chat-1",
        actions=[{"id": "ui.show", "params": {"location_id": TARGET}}],
    )
    g = store.claim(
        guide_id=g["guide_id"], tab_id="tab-owner", revision=g["revision"], placements=["desktop"]
    )
    status, body = _observe(state, {"targets": [TARGET]}, agent("dashboard:chat-1"))
    assert (status, body) == (200, {"status": "not_observed", "reason": "stale_tab", "ui_lang": ""})
    held = store.status_for_caller(slot_key="chat-1")
    assert held["reason"] == "stale_tab" and held["revision"] == g["revision"]
    assert observation_hub_for(state).pending_count() == 0
    # The owner answering again (its heartbeat) clears it.
    store.heartbeat(guide_id=g["guide_id"], tab_id="tab-owner", revision=g["revision"])
    assert store.status_for_caller(slot_key="chat-1")["reason"] == ""


def test_a_tab_on_another_build_is_not_observed() -> None:
    state = TabState(answer=lambda p: _tab_reply(p, build_digest="sha256:" + "1" * 64))
    state.open_slot("chat-1")
    observation_hub_for(state).note_sender("chat-1", "tab-A")
    status, body = _observe(state, {"targets": [TARGET]}, agent("dashboard:chat-1"))
    assert (status, body) == (
        200,
        {"status": "not_observed", "reason": "build_mismatch", "ui_lang": ""},
    )


def test_a_reply_carrying_text_is_refused_and_the_answer_is_not_observed(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from kiro_crew.dashboard.handlers import guide as guide_routes

    monkeypatch.setattr(guide_routes, "OBSERVE_WAIT_SECONDS", 0.2)
    state = TabState(answer=lambda p: {**_tab_reply(p), "text": "Older Sessions"})
    state.open_slot("chat-1")
    observation_hub_for(state).note_sender("chat-1", "tab-A")
    status, body = _observe(state, {"targets": [TARGET]}, agent("dashboard:chat-1"))
    assert body == {"status": "not_observed", "reason": "stale_tab", "ui_lang": ""}
    assert state.replies and state.replies[0][0] == 400


@pytest.mark.parametrize(
    "body, code",
    [
        ({"targets": ["not.a-location"]}, "unknown_target"),
        # A generated (page / setting) location is not observable: no marker.
        ({"targets": ["setting:chat.link-previews"]}, "unknown_target"),
        ({"targets": [TARGET] * 0}, "invalid_targets"),
        ({"targets": [f"x{i}" for i in range(guide_observe.MAX_TARGETS + 1)]}, "too_many_targets"),
        ({"targets": [TARGET], "selector": "#x"}, "invalid_body"),
        ({"targets": [{"id": TARGET}]}, "unknown_target"),
    ],
)
def test_the_agent_may_name_only_capped_manifest_ids(body: dict[str, Any], code: str) -> None:
    state = TabState(answer=_tab_reply)
    state.open_slot("chat-1")
    observation_hub_for(state).note_sender("chat-1", "tab-A")
    status, out = _observe(state, body, agent("dashboard:chat-1"))
    assert status == 400 and out["code"] == code
    assert state.frames == []


@pytest.mark.parametrize(
    "headers, status, code",
    [
        (OWNER, 403, "internal_secret_required"),
        (agent("dashboard:chat-1", "internal-app"), 403, "app_caller"),
        (agent("subagent:abc"), 403, "subagent_caller"),
        (agent("dashboard:closed"), 409, "no_live_slot"),
    ],
)
def test_the_agent_half_is_as_strict_as_the_other_guide_routes(
    headers: dict[str, str], status: int, code: str
) -> None:
    state = TabState(answer=_tab_reply)
    state.open_slot("chat-1")
    got, out = _observe(state, {"targets": [TARGET]}, headers)
    assert (got, out.get("code")) == (status, code)


@pytest.mark.parametrize("headers", [{"X-Test-Auth": "app"}, {}])
def test_the_reply_route_is_owner_only(headers: dict[str, str]) -> None:
    state = TabState()

    async def go(c: Any) -> int:
        r = await c.post("/api/guide/observe", json={"request_id": "o_x"}, headers=headers)
        return r.status

    assert _run(go, state) in (401, 403)


# ── guide_status reasons ──


def _active(store: GuideStore) -> dict[str, Any]:
    g = store.start(
        slot_key="chat-1",
        session_key="dashboard:chat-1",
        actions=[{"id": "ui.show", "params": {"location_id": TARGET}}],
    )
    return store.claim(
        guide_id=g["guide_id"], tab_id="tab-1", revision=g["revision"], placements=["desktop"]
    )


@pytest.mark.parametrize(
    ("detail", "reason"),
    [("ambiguous", "ambiguous_target"), ("predicate_unmet", "predicate_unmet")],
)
def test_a_missing_targets_detail_is_its_own_reason(detail: str, reason: str) -> None:
    store = GuideStore(clock=lambda: 1000.0)
    g = _active(store)
    first = g["actions"][0]["step_ids"][0]
    g = store.progress(
        guide_id=g["guide_id"],
        tab_id="tab-1",
        revision=g["revision"],
        action_index=0,
        step_index=0,
        step_id=first,
        outcome="target_missing",
        detail=detail,
    )
    assert (g["status"], g["reason"]) == ("target_missing", reason)
    with pytest.raises(GuideError):
        store.progress(
            guide_id=g["guide_id"],
            tab_id="tab-1",
            revision=g["revision"],
            action_index=0,
            step_index=0,
            step_id=first,
            outcome="observed",
            detail=detail,
        )


def test_a_tab_refusing_a_guide_of_another_build_moves_only_the_reason() -> None:
    store = GuideStore(clock=lambda: 1000.0)
    g = store.start(
        slot_key="chat-1",
        session_key="dashboard:chat-1",
        actions=[{"id": "ui.show", "params": {"location_id": TARGET}}],
    )
    assert g["actions"][0]["build_digest"] == DIGEST
    r = store.refuse(
        guide_id=g["guide_id"], tab_id="tab-1", revision=g["revision"], reason="build_mismatch"
    )
    assert (r["status"], r["reason"], r["owner_tab"]) == ("offered", "build_mismatch", None)
    with pytest.raises(GuideError):
        store.refuse(guide_id=g["guide_id"], tab_id="tab-1", revision=r["revision"], reason="other")


def test_an_auto_location_is_observed_through_its_stamped_site(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Any
) -> None:
    import json

    committed = json.loads(guide_catalog._UI_INDEX_PATH.read_text(encoding="utf-8"))
    lid, site = "auto:page.schedule:k.retry", "auto:page.schedule:SchedulePage:k.retry"
    plan = {
        "version": 2,
        "label_key": "k.retry",
        "placements": [
            {
                "id": "any",
                "route": "/schedule",
                "steps": [{"id": f"any:{lid}", "location": site, "label_key": "k.retry"}],
            }
        ],
    }
    auto = {
        "artifact": "auto",
        "base_input_digest": committed["input_digest"],
        "base_build_digest": committed["build_digest"],
        "build_digest": "sha256:" + "a" * 64,
        "locations": [
            {"id": lid, "tier": "auto", "guide_policy": "point", "guide_plan": plan},
        ],
    }
    path = tmp_path / "ui-index.auto.json"
    path.write_text(json.dumps(auto), encoding="utf-8")
    monkeypatch.setattr(guide_catalog, "_UI_AUTO_INDEX_PATH", path)
    guide_catalog.ui_auto_tier.cache_clear()
    guide_catalog.ui_build_manifest.cache_clear()
    try:
        state = TabState(answer=_tab_reply)
        state.open_slot("chat-1")
        observation_hub_for(state).note_sender("chat-1", "tab-A")
        status, body = _observe(state, {"targets": [lid]}, agent("dashboard:chat-1"))
        assert status == 200, body
        # The tab was asked for the SITE (what it can find), the agent hears the location.
        ((_, frame),) = state.frames
        assert frame["targets"] == [site]
        assert body["targets"] == [{"id": lid, "status": "offscreen"}]
    finally:
        guide_catalog.ui_auto_tier.cache_clear()
        guide_catalog.ui_build_manifest.cache_clear()


# ── which language the caller's dashboard shows ──


def _ui_locale(state: FakeState, headers: dict[str, str]) -> tuple[int, Any]:
    async def go(c: Any) -> tuple[int, Any]:
        r = await c.get("/api/guide/agent/language", headers=headers)
        return r.status, await r.json()

    return _run(go, state)


@pytest.fixture
def no_configured_language(monkeypatch: pytest.MonkeyPatch) -> None:
    import kiro_crew.context as context

    monkeypatch.setattr(context, "ui_language_tag", lambda _cfg: "")


def test_the_tabs_ui_language_is_kept_only_as_a_shipped_tag(no_configured_language: None) -> None:
    state = FakeState()
    state.open_slot("chat-1")
    guide_observe.note_chat_sender(state, "chat-1", "tab-A", "zh-CN")
    assert _ui_locale(state, agent("dashboard:chat-1")) == (
        200,
        {"ui_lang": "zh-CN", "source": "tab"},
    )
    # Neither a non-catalog tag nor a send without the header replaces it.
    guide_observe.note_chat_sender(state, "chat-1", "tab-A", "xx-YY")
    guide_observe.note_chat_sender(state, "chat-1", "tab-A", None)
    guide_observe.note_chat_sender(state, "chat-1", "tab-A", "en<script>")
    assert _ui_locale(state, agent("dashboard:chat-1"))[1]["ui_lang"] == "zh-CN"
    guide_observe.note_chat_sender(state, "chat-1", "tab-A", "ja")
    assert _ui_locale(state, agent("dashboard:chat-1"))[1]["ui_lang"] == "ja"
    # Per conversation: another chat's tab says nothing about this one.
    state.open_slot("chat-2")
    assert _ui_locale(state, agent("dashboard:chat-2")) == (
        200,
        {"ui_lang": "", "source": "unknown"},
    )


def test_the_configured_language_answers_when_no_tab_has(monkeypatch: pytest.MonkeyPatch) -> None:
    import kiro_crew.context as context

    monkeypatch.setattr(context, "ui_language_tag", lambda _cfg: "de")
    state = FakeState()
    state.open_slot("chat-1")
    assert _ui_locale(state, agent("dashboard:chat-1"))[1] == {"ui_lang": "de", "source": "setting"}
    # The tab's own word wins: it is what that screen renders.
    guide_observe.note_chat_sender(state, "chat-1", None, "fr")
    assert _ui_locale(state, agent("dashboard:chat-1"))[1] == {"ui_lang": "fr", "source": "tab"}


def test_the_ui_language_expires_with_the_sender() -> None:
    now = [0.0]
    hub = ObservationHub(clock=lambda: now[0])
    hub.note_ui_lang("chat-1", "ko")
    assert hub.ui_lang_for("chat-1") == "ko"
    now[0] = guide_observe.SENDER_TTL_SECONDS + 1
    assert hub.ui_lang_for("chat-1") is None


def test_the_ui_locale_route_is_the_agent_half(no_configured_language: None) -> None:
    state = FakeState()
    state.open_slot("chat-1")
    guide_observe.note_chat_sender(state, "chat-1", "tab-A", "zh-CN")
    for headers in (OWNER, agent("dashboard:chat-1", auth="internal-app")):
        status, body = _ui_locale(state, headers)
        assert status == 403 and "ui_lang" not in body
