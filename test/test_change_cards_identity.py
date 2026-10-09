"""A crewmate card's Undo never touches a crewmate -- or a schedule -- other
than the one the card wrote.

The card records the immutable ``member_id`` (and the schedule fingerprint);
Undo is offered only while they still match, and the delete/update routes
re-check them under the lock they write under. Each race test is paired with
the matched success it must still allow; reverting a guard turns its race test
red. No network, no real gateway.
"""

from __future__ import annotations

import asyncio
import json
from pathlib import Path
from typing import Any
from unittest import mock
from unittest.mock import MagicMock, patch

import pytest
from aiohttp import web
from aiohttp.test_utils import TestClient, TestServer
from guide_route_helpers import in_dashboard_turn

from kiro_crew import change_card_catalog as catalog
from kiro_crew.dashboard import change_cards as cards
from kiro_crew.dashboard.change_cards import CardStore
from kiro_crew.dashboard.handlers import change_cards as routes
from kiro_crew.dashboard.handlers.undo_identity_guard import REQ_CARD_UNDO_CREWMATE_EXPECT
from kiro_crew.dashboard.state import _ChatSlot

SLOT = "chat-1"
SK = f"dashboard:{SLOT}"


# ── F1: after_shows_the_write keys the Undo offer on the created member_id ──


def test_create_undo_offered_only_when_the_live_member_id_still_matches():
    p = catalog.validate_params("crewmate.create", {"name": "scout", "goal": "watch prod"})
    evidence = [{"name": "scout", "member_id": "m-aaa"}]

    # Same crewmate the card made (its id) → Undo may be offered.
    same = {"exists": True, "member_id": "m-aaa", "revision": "r1"}
    assert cards.after_shows_the_write("crewmate.create", p, same, evidence) is True

    # The owner deleted "scout" and recreated it: same name, DIFFERENT id. Undo
    # must be withheld so it cannot delete the replacement.
    replaced = {"exists": True, "member_id": "m-bbb", "revision": "r2"}
    assert cards.after_shows_the_write("crewmate.create", p, replaced, evidence) is False

    # Gone entirely, or an id that cannot be read now: not a confident match.
    assert cards.after_shows_the_write("crewmate.create", p, {"exists": False}, evidence) is False
    assert (
        cards.after_shows_the_write(
            "crewmate.create", p, {"exists": True, "member_id": ""}, evidence
        )
        is False
    )


def test_create_undo_withheld_when_the_card_recorded_no_member_id():
    # Fail closed: with no created id to bind to, there is no safe baseline.
    p = catalog.validate_params("crewmate.create", {"name": "scout", "goal": "g"})
    after = {"exists": True, "member_id": "m-xyz", "revision": "r1"}
    assert cards.after_shows_the_write("crewmate.create", p, after, [{"name": "scout"}]) is False
    assert cards.after_shows_the_write("crewmate.create", p, after, []) is False


# ── F1: the delete route re-enforces the id under the config deletion lock ──


@pytest.fixture(autouse=True)
def _owner_caller(_floor_monkeypatch):
    _floor_monkeypatch.setattr(
        "kiro_crew.dashboard.handlers.source_providers.is_owner_dashboard_request",
        lambda request: True,
    )


def _delete_request(name: str, expected_member_id: str | None):
    """A real ``api_kirocrew_agent_delete`` request. ``request.get`` is a real
    dict read so the guard sees the armed key, not a MagicMock."""
    attrs: dict[str, Any] = {"user": "dashboard"}
    if expected_member_id is not None:
        attrs[REQ_CARD_UNDO_CREWMATE_EXPECT] = expected_member_id
    request = MagicMock(spec=web.Request)
    request.method = "DELETE"
    request.match_info = {"name": name}
    request.app = {"state": None}
    request.get = lambda key, default=None: attrs.get(key, default)
    return request


def _write_template(agents_dir: Path, stem: str) -> None:
    (agents_dir / f"{stem}.json").write_text(
        json.dumps({"name": stem, "model": "", "tools": []}), encoding="utf-8"
    )


def _seed_crew(member_id: str) -> None:
    from kiro_crew.config.loader import KiroCrewAgentConfig, KiroCrewConfig

    cfg = KiroCrewConfig()
    cfg.agents = {
        "kirocrew": KiroCrewAgentConfig(kiro_agent="kirocrew"),
        "scout": KiroCrewAgentConfig(kiro_agent="kirocrew", member_id=member_id),
    }
    cfg.default_agent = "kirocrew"
    cfg.save()


@pytest.mark.asyncio
async def test_delete_route_refuses_when_the_live_member_id_differs(tmp_path):
    """The racing recreate: the card armed the Undo with the id it created, but
    the live "scout" is a replacement with another id. The delete must refuse
    with ``changed_since_apply`` and leave the replacement in place."""
    agents_dir = tmp_path / "agents"
    agents_dir.mkdir()
    _write_template(agents_dir, "kirocrew")
    _seed_crew(member_id="m-REPLACEMENT")

    from kiro_crew.config.loader import KiroCrewConfig

    with patch("kiro_crew.agent.KIRO_AGENTS_DIR", agents_dir):
        resp = await routes_delete(_delete_request("scout", expected_member_id="m-ORIGINAL"))

    assert resp.status == 409
    assert json.loads(resp.body)["code"] == "changed_since_apply"
    # The replacement crewmate the card never made is untouched.
    assert "scout" in KiroCrewConfig.load().agents


@pytest.mark.asyncio
async def test_delete_route_removes_when_the_member_id_still_matches(tmp_path):
    """Matched success: the live id is the one the card created, so the Undo
    deletes exactly the crewmate it made."""
    agents_dir = tmp_path / "agents"
    agents_dir.mkdir()
    _write_template(agents_dir, "kirocrew")
    _seed_crew(member_id="m-ORIGINAL")

    from kiro_crew.config.loader import KiroCrewConfig

    with patch("kiro_crew.agent.KIRO_AGENTS_DIR", agents_dir):
        resp = await routes_delete(_delete_request("scout", expected_member_id="m-ORIGINAL"))

    assert resp.status == 200
    assert "scout" not in KiroCrewConfig.load().agents


@pytest.mark.asyncio
async def test_delete_route_is_unconditional_without_the_guard(tmp_path):
    """An ordinary delete (no card, key absent) deletes as before — the guard is
    opt-in and adds no behaviour to a plain DELETE."""
    agents_dir = tmp_path / "agents"
    agents_dir.mkdir()
    _write_template(agents_dir, "kirocrew")
    _seed_crew(member_id="m-ORIGINAL")

    from kiro_crew.config.loader import KiroCrewConfig

    with patch("kiro_crew.agent.KIRO_AGENTS_DIR", agents_dir):
        resp = await routes_delete(_delete_request("scout", expected_member_id=None))

    assert resp.status == 200
    assert "scout" not in KiroCrewConfig.load().agents


# ── F1: the window the OLD guard left open — a replacement persisted AFTER the
#    pre-delete snapshot but BEFORE the locked mutate re-reads the raw record ──


def _repersist_scout_member_id(member_id: str) -> None:
    """Rewrite the on-disk ``scout`` record's ``member_id``, as a same-name
    recreate in another process would leave it. Takes the config sidecar lock via
    ``save()``, exactly as a real recreate's write does, so it is a faithful stand
    -in for the cross-process write the ``update_config_locked`` read must catch."""
    from kiro_crew.config.loader import KiroCrewConfig

    cfg = KiroCrewConfig.load()
    cfg.agents["scout"].member_id = member_id
    cfg.save()


@pytest.mark.asyncio
async def test_delete_route_refuses_a_replacement_persisted_inside_the_lock_window(tmp_path):
    """Rewrite scout's member_id on disk, as a same-name recreate would."""
    agents_dir = tmp_path / "agents"
    agents_dir.mkdir()
    _write_template(agents_dir, "kirocrew")
    _seed_crew(member_id="m-ORIGINAL")

    from kiro_crew.config.loader import KiroCrewConfig
    from kiro_crew.dashboard.handlers import agents as handlers

    real_update = handlers.update_config_locked
    fired = {"done": False}

    def _update_after_a_racing_recreate(*args, **kwargs):
        # The replacement is NOT on disk when the route snapshots; it lands here,
        # just before the real locked read-modify-write re-reads the raw doc.
        if not fired["done"]:
            fired["done"] = True
            _repersist_scout_member_id("m-REPLACEMENT")
        return real_update(*args, **kwargs)

    reclaim = MagicMock()
    avatars = MagicMock()
    prune = MagicMock(return_value=False)
    with (
        patch("kiro_crew.agent.KIRO_AGENTS_DIR", agents_dir),
        patch.object(handlers, "update_config_locked", _update_after_a_racing_recreate),
        patch.object(handlers, "_reclaim_deleted_member_crew_log", reclaim),
        patch.object(handlers, "_remove_avatar_files", avatars),
        patch.object(handlers, "_prune_private_copy_of_deleted_crew", prune),
    ):
        resp = await routes_delete(_delete_request("scout", expected_member_id="m-ORIGINAL"))

    assert fired["done"], "the racing recreate must have landed in the lock window"
    assert resp.status == 409
    assert json.loads(resp.body)["code"] == "changed_since_apply"
    # The replacement the card never made survives, still wearing its own id.
    live = KiroCrewConfig.load().agents
    assert "scout" in live and live["scout"].member_id == "m-REPLACEMENT"
    # No teardown keyed to the delete ran: the mutate aborted the write first.
    reclaim.assert_not_called()
    avatars.assert_not_called()
    prune.assert_not_called()


@pytest.mark.asyncio
async def test_delete_route_removes_when_a_window_write_keeps_the_same_member_id(tmp_path):
    """An in-window rewrite that keeps the member_id still deletes."""
    agents_dir = tmp_path / "agents"
    agents_dir.mkdir()
    _write_template(agents_dir, "kirocrew")
    _seed_crew(member_id="m-ORIGINAL")

    from kiro_crew.config.loader import KiroCrewConfig
    from kiro_crew.dashboard.handlers import agents as handlers

    real_update = handlers.update_config_locked
    fired = {"done": False}

    def _update_after_a_same_id_rewrite(*args, **kwargs):
        if not fired["done"]:
            fired["done"] = True
            _repersist_scout_member_id("m-ORIGINAL")  # same id, identity intact
        return real_update(*args, **kwargs)

    reclaim = MagicMock()
    with (
        patch("kiro_crew.agent.KIRO_AGENTS_DIR", agents_dir),
        patch.object(handlers, "update_config_locked", _update_after_a_same_id_rewrite),
        patch.object(handlers, "_reclaim_deleted_member_crew_log", reclaim),
    ):
        resp = await routes_delete(_delete_request("scout", expected_member_id="m-ORIGINAL"))

    assert fired["done"]
    assert resp.status == 200
    assert "scout" not in KiroCrewConfig.load().agents
    # The delete committed, so its crew-log reclaim ran as on any ordinary delete.
    reclaim.assert_called_once()


async def routes_delete(request):
    from kiro_crew.dashboard.handlers.agents import api_kirocrew_agent_delete

    return await api_kirocrew_agent_delete(request)


# ── F2: _arm_card_undo_cron_guard arms the right fingerprint per step ──


def _rec(kind: str, after: dict[str, Any]) -> dict[str, Any]:
    return {"kind": kind, "after": after, "evidence": [{"member_id": after.get("member_id")}]}


def test_arm_guard_schedule_create_cron_delete_uses_after_revision():
    req: dict[str, Any] = {}
    step = {"method": "DELETE", "path": "/api/crons/j1"}
    routes._arm_card_undo_cron_guard(
        req, _rec("schedule.create", {"revision": "digest-sched"}), "undo", step
    )
    assert req[routes.REQ_CARD_UNDO_CRON_EXPECT] == "digest-sched"


def test_arm_guard_crewmate_create_cron_delete_uses_after_schedule():
    # The crewmate.create Undo's cron DELETE must compare against the SCHEDULE
    # digest (after["schedule"]), the field F2 adds — not after["revision"]
    # (which for a crewmate is the crew's own config/spec fingerprint).
    req: dict[str, Any] = {}
    step = {"method": "DELETE", "path": "/api/crons/j1"}
    rec = _rec("crewmate.create", {"schedule": "digest-sched", "revision": "digest-crew"})
    routes._arm_card_undo_cron_guard(req, rec, "undo", step)
    assert req[routes.REQ_CARD_UNDO_CRON_EXPECT] == "digest-sched"


def test_arm_guard_crewmate_create_cron_delete_fails_closed_without_a_fingerprint():
    # No schedule digest recorded → the cron delete must NOT run unconditionally.
    # An impossible sentinel is armed so the compare-and-delete refuses (409).
    req: dict[str, Any] = {}
    step = {"method": "DELETE", "path": "/api/crons/j1"}
    routes._arm_card_undo_cron_guard(req, _rec("crewmate.create", {}), "undo", step)
    assert req[routes.REQ_CARD_UNDO_CRON_EXPECT] == routes._CRON_FINGERPRINT_MISSING
    # And that sentinel can never equal a live job's fingerprint.
    assert routes._CRON_FINGERPRINT_MISSING != "0" * 16


def test_arm_guard_crewmate_create_agent_delete_arms_the_member_id():
    req: dict[str, Any] = {}
    step = {"method": "DELETE", "path": "/api/agents/scout"}
    rec = _rec("crewmate.create", {"member_id": "m-aaa", "schedule": "d"})
    routes._arm_card_undo_cron_guard(req, rec, "undo", step)
    assert req[REQ_CARD_UNDO_CREWMATE_EXPECT] == "m-aaa"
    # The cron key is not armed on the agent-delete step.
    assert routes.REQ_CARD_UNDO_CRON_EXPECT not in req


def test_arm_guard_inert_outside_an_undo_or_a_delete():
    req: dict[str, Any] = {}
    rec = _rec("crewmate.create", {"member_id": "m", "schedule": "d"})
    routes._arm_card_undo_cron_guard(
        req, rec, "apply", {"method": "DELETE", "path": "/api/agents/s"}
    )
    routes._arm_card_undo_cron_guard(req, rec, "undo", {"method": "POST", "path": "/api/crons"})
    assert req == {}


# ── F2: crewmate.create Undo's cron DELETE races a tab, through the real hook ──


class _State:
    owner_id = ""

    def __init__(self, store: CardStore, crons: Any) -> None:
        self._slots = {SLOT: in_dashboard_turn(_ChatSlot(SLOT))}
        self.frames: list[tuple[str, dict[str, Any]]] = []
        self._change_card_store = store
        self.crons = crons
        self.ws_frames: list[tuple[str, dict[str, Any]]] = []

    def get_slot(self, name: str):
        return self._slots.get(name)

    async def deliver_ws_owners(self, kind: str, payload: dict[str, Any]) -> int:
        self.frames.append((kind, payload))
        return 1

    def broadcast_ws(self, kind: str, payload: dict[str, Any]) -> None:
        self.ws_frames.append((kind, json.loads(json.dumps(payload))))

    def push_refresh(self, kind: str) -> None:
        self.frames.append(("refresh", {"kind": kind}))


@web.middleware
async def _fake_auth(request: web.Request, handler):
    request["user"] = "local-app"
    request["app"] = ""
    return await handler(request)


def _card_headers(card, op="apply", step=0):
    return {
        "X-Test-Auth": "owner",
        "X-Card-Id": card["id"],
        "X-Card-Revision": str(card["revision"]),
        "X-Card-Op": op,
        "X-Card-Step": str(step),
    }


def _applied_crewmate_create_card(store: CardStore, service, job_id: str, member_id: str):
    """Build a ``crewmate.create`` card as the apply path leaves it: step 0
    created the crew (member_id), step 1 created the schedule (job_id), and
    ``after`` carries both the created id and the live schedule fingerprint."""
    params = catalog.validate_params(
        "crewmate.create",
        {"name": "scout", "goal": "watch prod", "schedule": {"cron_expr": "0 9 * * *"}},
    )
    preview = catalog.build_preview(
        "crewmate.create",
        params,
        {"exists": False, "inherited_auto_approve": []},
        {},
    )
    rec = store.propose(
        slot_key=SLOT,
        session_key=SK,
        kind="crewmate.create",
        params=params,
        reason="make scout",
        preview=preview,
        before={"exists": False, "inherited_auto_approve": []},
        context={},
    )
    store.begin_step(rec, revision=rec["revision"], op="apply", index=0)
    store.record_success(
        rec, op="apply", index=0, evidence={"name": "scout", "member_id": member_id}
    )
    store.begin_step(rec, revision=rec["revision"], op="apply", index=1)
    store.record_success(rec, op="apply", index=1, evidence={"id": job_id})
    sched = cards.cron_job_revision(service.get_job(job_id))
    after = {
        "exists": True,
        "member_id": member_id,
        "revision": "crew-digest",
        "schedule_exists": True,
        "schedule": sched,
    }
    undo, reason = catalog.build_undo(
        "crewmate.create",
        params,
        {"exists": False, "inherited_auto_approve": []},
        rec["evidence"],
        catalog.applied_write_count(rec["plan"]["apply"], len(rec["evidence"])),
        {},
    )
    assert undo is not None, reason
    # The first inverse step is the cron DELETE this test drives.
    assert undo[0]["method"] == "DELETE" and undo[0]["path"].startswith("/api/crons/")
    store.complete_apply(rec, after=after, undo=undo, undo_reason=reason)
    return store.public(rec), sched, after


def test_crewmate_create_cron_undo_refuses_a_schedule_edited_in_the_window(tmp_path):
    """A schedule edited before the Undo's cron delete survives with its history."""
    from kiro_crew.cron import CronService

    service = CronService(base_dir=tmp_path / "crons")
    job = service.add_job("scout schedule", "watch prod", cron_expr="0 9 * * *")
    job_id = job.id

    store = CardStore(tmp_path / "c.json")
    state = _State(store, service)
    card, sched, after = _applied_crewmate_create_card(store, service, job_id, member_id="m-aaa")

    async def read_state(kind, p, evidence, *, state, app):
        # The precheck's own ``undo_snapshot_changed`` sees the UNCHANGED
        # snapshot (identical to ``after``), so it passes; the edit lands later,
        # during the flush, so ONLY the lock-time compare-and-delete can catch
        # it. This isolates the F2 guard from the step-0 precheck.
        return dict(after)

    def _app() -> web.Application:
        from kiro_crew.dashboard.handlers.cron import api_cron_delete

        app = web.Application(middlewares=[_fake_auth, routes.change_card_middleware])
        app["state"] = state
        routes.register_change_card_routes(app)
        app.router.add_delete("/api/crons/{job_id}", api_cron_delete)
        return app

    real_flush = store.flush
    edited = {"done": False}

    async def flush_then_edit(*a, **k):
        out = await real_flush(*a, **k)
        if not edited["done"]:
            edited["done"] = True
            service.update_job(job_id, name="renamed by another tab")
        return out

    async def main():
        history = service.get_history()
        with (
            mock.patch.object(
                history, "delete_job_history", wraps=history.delete_job_history
            ) as del_hist,
            mock.patch.object(cards, "read_state", read_state),
        ):
            client = TestClient(TestServer(_app()))
            await client.start_server()
            try:
                with mock.patch.object(store, "flush", flush_then_edit):
                    resp = await client.delete(
                        f"/api/crons/{job_id}", headers=_card_headers(card, op="undo", step=0)
                    )
                    data = await resp.json()
                if routes._BACKGROUND:
                    await asyncio.gather(*list(routes._BACKGROUND))
                return resp.status, data, del_hist.await_count
            finally:
                await client.close()

    status, data, history_deletes = asyncio.run(main())
    assert edited["done"], "the racing edit must have run in the flush window"
    assert status == 409 and data["code"] == "changed_since_apply"
    survivor = service.get_job(job_id)
    assert survivor is not None and survivor.name == "renamed by another tab"
    assert history_deletes == 0


def test_crewmate_create_cron_undo_removes_a_still_matching_schedule(tmp_path):
    """Matched success: nothing changed in the window, so the Undo's cron DELETE
    removes the schedule the create made exactly as a plain delete would."""
    from kiro_crew.cron import CronService

    service = CronService(base_dir=tmp_path / "crons")
    job = service.add_job("scout schedule", "watch prod", cron_expr="0 9 * * *")
    job_id = job.id

    store = CardStore(tmp_path / "c.json")
    state = _State(store, service)
    card, sched, after = _applied_crewmate_create_card(store, service, job_id, member_id="m-aaa")

    async def read_state(kind, p, evidence, *, state, app):
        return dict(after)

    def _app() -> web.Application:
        from kiro_crew.dashboard.handlers.cron import api_cron_delete

        app = web.Application(middlewares=[_fake_auth, routes.change_card_middleware])
        app["state"] = state
        routes.register_change_card_routes(app)
        app.router.add_delete("/api/crons/{job_id}", api_cron_delete)
        return app

    async def main():
        with mock.patch.object(cards, "read_state", read_state):
            client = TestClient(TestServer(_app()))
            await client.start_server()
            try:
                resp = await client.delete(
                    f"/api/crons/{job_id}", headers=_card_headers(card, op="undo", step=0)
                )
                data = await resp.json()
                if routes._BACKGROUND:
                    await asyncio.gather(*list(routes._BACKGROUND))
                return resp.status, data
            finally:
                await client.close()

    status, data = asyncio.run(main())
    assert status == 200 and data["ok"] is True
    assert service.get_job(job_id) is None


def test_agent_delete_expectation_uses_create_evidence_not_a_later_snapshot():
    req = {}
    rec = _rec("crewmate.create", {"member_id": "replacement"})
    rec["evidence"] = [{"member_id": "original"}]
    routes._arm_card_undo_cron_guard(
        req, rec, "undo", {"method": "DELETE", "path": "/api/agents/scout"}
    )
    assert req[REQ_CARD_UNDO_CREWMATE_EXPECT] == "original"


def test_agent_delete_without_creation_identity_still_arms_a_refusal():
    req = {}
    rec = _rec("crewmate.create", {"member_id": "replacement"})
    rec["evidence"] = []
    routes._arm_card_undo_cron_guard(
        req, rec, "undo", {"method": "DELETE", "path": "/api/agents/scout"}
    )
    assert req[REQ_CARD_UNDO_CREWMATE_EXPECT] == ""


# ── crewmate.update: the Undo's PUT writes back only to the crewmate it changed ──


def test_update_snapshot_carries_the_member_id(tmp_path):
    """A crewmate deleted and re-made under the same name with the same field
    value reads as a change, so the old card's Undo is refused."""
    _seed_crew(member_id="m-ORIGINAL")
    p = catalog.validate_params(
        "crewmate.update", {"name": "scout", "fields": {"description": "card text"}}
    )
    snap = asyncio.run(cards.read_state("crewmate.update", p, [], state=None, app=None))
    assert snap["member_id"] == "m-ORIGINAL"
    replaced = {**snap, "member_id": "m-REPLACEMENT"}
    assert cards.undo_snapshot_changed("crewmate.update", replaced, snap)


def test_arm_guard_crewmate_update_put_arms_the_applied_member_id():
    req: dict[str, Any] = {}
    step = {"method": "PUT", "path": "/api/agents/scout"}
    routes._arm_card_undo_cron_guard(
        req, _rec("crewmate.update", {"member_id": "m-aaa"}), "undo", step
    )
    assert req[REQ_CARD_UNDO_CREWMATE_EXPECT] == "m-aaa"
    # Not armed outside an Undo.
    other: dict[str, Any] = {}
    routes._arm_card_undo_cron_guard(
        other, _rec("crewmate.update", {"member_id": "m-aaa"}), "apply", step
    )
    assert other == {}


def _update_request(name: str, body: dict[str, Any], expected_member_id: str | None):
    attrs: dict[str, Any] = {"user": "dashboard"}
    if expected_member_id is not None:
        attrs[REQ_CARD_UNDO_CREWMATE_EXPECT] = expected_member_id

    async def _json():
        return body

    request = MagicMock(spec=web.Request)
    request.method = "PUT"
    request.match_info = {"name": name}
    request.app = {"state": None}
    request.json = _json
    request.get = lambda key, default=None: attrs.get(key, default)
    return request


async def _routes_update(request):
    from kiro_crew.dashboard.handlers.agents import api_kirocrew_agent_update

    return await api_kirocrew_agent_update(request)


@pytest.mark.asyncio
async def test_update_route_refuses_an_undo_onto_a_replacement_crewmate(tmp_path):
    from kiro_crew.config.loader import KiroCrewConfig

    _seed_crew(member_id="m-REPLACEMENT")
    resp = await _routes_update(
        _update_request("scout", {"description": "old"}, expected_member_id="m-ORIGINAL")
    )
    assert resp.status == 409
    assert json.loads(resp.body)["code"] == "changed_since_apply"
    assert KiroCrewConfig.load().agents["scout"].description != "old"


@pytest.mark.asyncio
async def test_update_route_writes_when_the_member_id_matches_or_is_unarmed(tmp_path):
    from kiro_crew.config.loader import KiroCrewConfig

    _seed_crew(member_id="m-ORIGINAL")
    resp = await _routes_update(
        _update_request("scout", {"description": "old"}, expected_member_id="m-ORIGINAL")
    )
    assert resp.status == 200
    assert KiroCrewConfig.load().agents["scout"].description == "old"
    resp = await _routes_update(
        _update_request("scout", {"description": "plain"}, expected_member_id=None)
    )
    assert resp.status == 200
    assert KiroCrewConfig.load().agents["scout"].description == "plain"


# ── an ordinary (no-card) save is never refused by the card hook ──


def test_an_ordinary_save_without_a_declared_length_passes_through_untouched():
    async def main():
        seen: list[bytes] = []

        async def handler(request):
            seen.append(await request.read())
            return web.json_response({"ok": True})

        request = MagicMock(spec=web.Request)
        request.headers = {}
        request.can_read_body = True
        request.content_length = None
        request.read = mock.AsyncMock(return_value=b"x" * 10)
        with (
            patch.object(routes, "route_key", return_value=next(iter(catalog.MEMORY_ROUTES))),
            patch.object(routes, "_is_owner_browser", return_value=True),
            patch.object(routes, "_read_body", side_effect=AssertionError("must not buffer")),
        ):
            resp = await routes.change_card_middleware(request, handler)
        return resp.status, seen

    status, seen = asyncio.run(main())
    assert status == 200 and seen == [b"x" * 10]
