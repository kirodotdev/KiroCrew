"""Change cards: when an applied card can still be undone.

The post-apply snapshot goes stale when the thing is edited, a capability apply whose
PUT never lands offers no Undo, a schedule card's Undo deletes only while the job still
matches what the card created, and MCP, skill-template and connection cards snapshot
exactly what their Undo restores.
"""

from __future__ import annotations

import asyncio
import json
from typing import Any
from unittest import mock

import change_cards_helpers
import pytest
from aiohttp import web
from aiohttp.test_utils import TestClient, TestServer
from change_cards_helpers import (
    _CAP_BEFORE,
    _CAP_IMPACT,
    _CAP_PARAMS,
    OWNER,
    SECRET_VALUE,
    SK,
    SLOT,
    FakeState,
    World,
    _app,
    _card_headers,
    _fake_auth,
    _propose_capability,
)

from kiro_crew import change_card_catalog as catalog
from kiro_crew.dashboard import change_cards as cards
from kiro_crew.dashboard.change_cards import CardStore
from kiro_crew.dashboard.handlers import change_cards as routes

# The shared fixtures, bound here by name so pytest registers them for this module.
world = change_cards_helpers.world


# ── undo staleness: the post-apply snapshot changes when the thing is edited ──


def test_a_replaced_secret_makes_an_earlier_snapshot_stale(tmp_path, monkeypatch):
    from kiro_crew.config import loader
    from kiro_crew.secrets import SecretVault

    monkeypatch.setattr(loader, "config_dir", lambda: tmp_path)
    vault = SecretVault(tmp_path)
    vault.set_sync("GH_TOKEN", SECRET_VALUE)
    params = {"name": "GH_TOKEN"}
    first = cards._sync_read_state("secret.save", params, [{}])
    assert first == cards._sync_read_state("secret.save", params, [{}])
    vault.set_sync("GH_TOKEN", SECRET_VALUE)  # same value, re-saved: a new entry
    second = cards._sync_read_state("secret.save", params, [{}])
    assert first["exists"] and second["exists"]
    assert cards.canonical(first) != cards.canonical(second)
    assert SECRET_VALUE not in cards.canonical(second)


def test_an_edited_crewmate_makes_an_earlier_snapshot_stale(monkeypatch):
    from dataclasses import dataclass

    from kiro_crew.dashboard.handlers import agents as agents_handlers

    @dataclass
    class Agent:
        member_id: str = "m1"
        kiro_agent: str = "scout"
        model: str = "auto"

    spec = {"description": "watch PRs", "tools": ["@a"]}
    monkeypatch.setattr(agents_handlers, "_agent_detail_candidates", lambda name: [("p", spec)])
    agent = Agent()
    first = cards._crewmate_revision(agent)
    assert first == cards._crewmate_revision(agent)
    spec["description"] = "watch issues"
    assert cards._crewmate_revision(agent) != first
    spec["description"] = "watch PRs"
    agent.model = "claude-sonnet-5.5"
    assert cards._crewmate_revision(agent) != first


def test_an_interrupted_apply_publishes_where_it_resumes(tmp_path):
    store = CardStore(tmp_path / "c.json")
    rec = {"status": "applying", "evidence": [{"member_id": "m-7", "name": "scout"}], "id": "c1"}
    assert store.public(rec)["resume"] == {
        "step": 1,
        "responses": [{"member_id": "m-7", "name": "scout"}],
    }
    assert "evidence" not in store.public(rec)
    assert "resume" not in store.public({**rec, "status": "applied"})


def test_a_schedule_fingerprint_sees_edits_not_runs():
    from kiro_crew.cron_service.model import CronJob, CronSchedule

    job = CronJob(
        id="j1",
        name="Standup",
        message="hi",
        schedule=CronSchedule(kind="cron", cron_expr="0 9 * * *"),
    )
    first = cards._cron_job_revision(job)
    job.last_run_ts, job.last_status, job.consecutive_failures = 123.0, "ok", 2
    assert cards._cron_job_revision(job) == first
    job.silent = True
    assert cards._cron_job_revision(job) != first
    job.silent = False
    job.channel = "C123"
    assert cards._cron_job_revision(job) != first


def test_a_waiting_poll_resumes_its_own_step_and_an_undo_resumes_too(tmp_path):
    store = CardStore(tmp_path / "c.json")
    plan = {"apply": [{}, {"repeat": True}], "undo": [{}, {}]}
    waiting = {
        "status": "applying",
        "evidence": [{"name": "x"}, {"state": "pending"}],
        "progress": {"op": "apply", "done": 1, "total": 2, "waiting": True},
        "plan": plan,
    }
    assert store.public(waiting)["resume"] == {"step": 1, "responses": [{"name": "x"}]}
    halfway = {"status": "applied", "evidence": [], "undo_evidence": [{}], "plan": plan}
    assert store.public(halfway)["undo_resume"] == {"step": 1, "responses": [{}]}
    assert "undo_resume" not in store.public({**halfway, "undo_evidence": []})


def test_an_interrupted_poll_keeps_its_resume_checkpoint(tmp_path):
    store = CardStore(tmp_path / "c.json")
    store._ensure_loaded()
    rec = {
        "id": "cc_poll",
        "slot_key": SLOT,
        "kind": "connection.connect",
        "revision": 1,
        "status": "pending",
        "params": {"slug": "github"},
        "plan": {"apply": [{"method": "POST"}, {"method": "POST", "repeat": True}], "undo": None},
        "evidence": [],
        "undo_evidence": [],
        "progress": None,
        "inflight": None,
        "expires_at": 10**12,
    }
    store._cards[rec["id"]] = rec
    store.begin_step(rec, revision=1, op="apply", index=0)
    assert store.record_success(rec, op="apply", index=0, evidence={}) == "more"
    store.begin_step(rec, revision=1, op="apply", index=1)
    assert store.record_success(rec, op="apply", index=1, evidence={"state": "pending"}) == "poll"
    store.begin_step(rec, revision=1, op="apply", index=1)  # the next poll ...
    store.abort_step(rec)  # ... is cut off before it answers
    assert store.public(rec)["resume"] == {"step": 1, "responses": [{}]}
    assert store.public(rec)["progress"]["waiting"] is True


def test_a_resumed_undo_rechecks_what_it_still_removes(monkeypatch):
    seen: list[Any] = []
    crewmate_now = {"exists": True, "member_id": "m1", "revision": "r1"}

    async def read_state(kind, params, evidence, *, state, app):
        seen.append(evidence)
        return dict(crewmate_now)

    monkeypatch.setattr(cards, "read_state", read_state)
    rec = {
        "kind": "crewmate.create",
        "params": {"name": "Scout", "goal": "g"},
        "evidence": [{"member_id": "m1", "name": "scout"}, {"id": "job1"}],
        "after": {
            "exists": True,
            "member_id": "m1",
            "revision": "r1",
            "schedule_exists": True,
            "schedule": "s1",
        },
    }
    # The schedule is already gone; the crewmate is unchanged: allowed.
    assert asyncio.run(cards.resumed_undo_is_stale(rec, 1, state=None, app=None)) is False
    assert seen[-1] == rec["evidence"][:1]
    crewmate_now["revision"] = "r2"  # edited while the Undo was interrupted
    assert asyncio.run(cards.resumed_undo_is_stale(rec, 1, state=None, app=None)) is True


def test_a_capability_undo_runs_its_second_step():
    rec = {"kind": "crewmate.capabilities", "params": {}, "evidence": [], "after": {}}
    assert asyncio.run(cards.resumed_undo_is_stale(rec, 1, state=None, app=None)) is False


# ── a capability apply whose PUT never lands applies nothing and offers no Undo ──


def test_applied_write_count_leaves_the_capability_preview_out():
    """The preview POST fronting a capability apply is a read, so the first
    successful step is zero writes applied; the PUT is the one write."""
    plan = catalog.build_preview(
        "crewmate.capabilities", _CAP_PARAMS, _CAP_BEFORE, {"impact": _CAP_IMPACT}
    )["apply"]
    assert [s["method"] for s in plan] == ["POST", "PUT"]
    assert catalog.applied_write_count(plan, 0) == 0  # nothing ran
    assert catalog.applied_write_count(plan, 1) == 0  # preview only
    assert catalog.applied_write_count(plan, 2) == 1  # preview + PUT -> one write
    # A poll (a GET) is likewise not a write; the connection POST mint is. The
    # connection apply plan is POST mint then a repeat GET mint.
    conn = [
        {"method": "POST", "path": "/api/connections/mint"},
        {"method": "GET", "path": "/api/connections/mint?slug=gh", "repeat": True},
    ]
    assert catalog.applied_write_count(conn, 2) == 1


def _capability_app(world: World, store: CardStore, put_response):
    """An app wiring the capability preview/PUT routes under the card hook.

    ``put_response`` is called with the request and returns the ``web.Response``
    the PUT answers with, so a test can make the PUT land or fail stale.
    """

    async def preview(request):
        await request.json()
        world.calls.append(("POST", "/api/agents/mate/capabilities/preview"))
        return web.json_response({"preview_token": "tok", "impact": _CAP_IMPACT})

    async def put_caps(request):
        await request.json()
        world.calls.append(("PUT", "/api/agents/mate/capabilities"))
        return await put_response(request)

    app = web.Application(middlewares=[_fake_auth, routes.change_card_middleware])
    app["state"] = FakeState(store)
    routes.register_change_card_routes(app)
    app.router.add_post("/api/agents/{name}/capabilities/preview", preview)
    app.router.add_put("/api/agents/{name}/capabilities", put_caps)
    return app


async def _apply_capability(c, card, *, put_lands: bool):
    """Drive the preview step then the PUT step through the real hook."""
    apply_plan = card["plan"]["apply"]
    r0 = await c.post(
        "/api/agents/mate/capabilities/preview",
        json=apply_plan[0]["body"],
        headers=_card_headers(card, op="apply", step=0),
    )
    assert r0.status == 200, await r0.text()
    # The PUT body is the plan body with the preview_token fill resolved.
    put_body = {**apply_plan[1]["body"], "preview_token": "tok"}
    r1 = await c.put(
        "/api/agents/mate/capabilities",
        json=put_body,
        headers=_card_headers(card, op="apply", step=1),
    )
    return r1


def test_a_capability_whose_put_fails_stale_applies_nothing_and_offers_no_undo(
    tmp_path, monkeypatch
):
    """Route-level: the preview lands, the PUT is refused ``stale_revision``
    (another tab moved the revision). The card must settle with no write applied
    and no Undo — never an Undo bound to the other tab's revision."""

    async def read_state(kind, params, evidence, *, state, app):
        # Pre-apply precheck (empty evidence) must match ``before`` so the apply
        # proceeds to the PUT. The completion read sees another tab's revision,
        # which must not become this card's Undo baseline.
        if not evidence:
            return dict(_CAP_BEFORE)
        return {"revision": "r-other-tab", "rows": []}

    async def read_context(kind, params, before, *, state, app):
        return {"impact": _CAP_IMPACT}

    monkeypatch.setattr(cards, "read_state", read_state)
    monkeypatch.setattr(cards, "read_context", read_context)
    store = CardStore(tmp_path / "c.json")

    async def stale(request):
        return web.json_response({"error": "stale", "code": "stale_revision"}, status=409)

    async def go(c):
        card = _propose_capability(store)
        r1 = await _apply_capability(c, card, put_lands=False)
        pending = await (await c.get(f"/api/cards/pending?slot={SLOT}", headers=OWNER)).json()
        return r1.status, pending

    world = World()

    async def main():
        client = TestClient(TestServer(_capability_app(world, store, stale)))
        await client.start_server()
        try:
            out = await go(client)
            if routes._BACKGROUND:
                await asyncio.gather(*list(routes._BACKGROUND))
            return out
        finally:
            await client.close()

    status, pending = asyncio.run(main())
    assert status == 409  # the PUT was refused
    card = pending["cards"][0]
    # Only the preview landed and it writes nothing: a retryable failure with
    # no Undo, never an Undo against r-other-tab.
    assert card["status"] == "failed"
    assert (card.get("plan") or {}).get("undo") is None


def test_a_capability_undo_binds_to_the_put_revision_not_a_fresh_read(tmp_path, monkeypatch):
    """Route-level: the PUT lands at ``r1``. If the post-apply read is at a
    different revision (another tab wrote after us) there is no Undo; when it
    agrees with ``r1`` the Undo PUT carries ``r1``, never the read's revision."""
    seen_after = {"revision": "r1"}

    async def read_state(kind, params, evidence, *, state, app):
        if not evidence:
            return dict(_CAP_BEFORE)  # pre-apply precheck
        return {"revision": seen_after["revision"], "rows": []}

    async def read_context(kind, params, before, *, state, app):
        return {"impact": _CAP_IMPACT}

    monkeypatch.setattr(cards, "read_state", read_state)
    monkeypatch.setattr(cards, "read_context", read_context)

    async def lands(request):
        return web.json_response({"ok": True, "revision": "r1"})

    def run(after_revision: str) -> dict[str, Any]:
        seen_after["revision"] = after_revision
        store = CardStore(tmp_path / f"c-{after_revision}.json")
        world = World()

        async def go(c):
            card = _propose_capability(store)
            r1 = await _apply_capability(c, card, put_lands=True)
            assert r1.status == 200, await r1.text()
            pending = await (await c.get(f"/api/cards/pending?slot={SLOT}", headers=OWNER)).json()
            return pending["cards"][0]

        async def main():
            client = TestClient(TestServer(_capability_app(world, store, lands)))
            await client.start_server()
            try:
                out = await go(client)
                if routes._BACKGROUND:
                    await asyncio.gather(*list(routes._BACKGROUND))
                return out
            finally:
                await client.close()

        return asyncio.run(main())

    # Post-apply read agrees with the PUT revision: Undo offered, bound to r1.
    matched = run("r1")
    assert matched["status"] == "applied"
    undo = (matched.get("plan") or {}).get("undo")
    assert undo is not None
    put = next(s for s in undo if s["method"] == "PUT")
    assert put["body"]["revision"] == "r1"

    # Post-apply read is a different revision (another tab wrote after us): the
    # exposed card offers no Undo and says the state changed since apply.
    moved = run("r2-another-tab")
    assert moved["status"] == "applied"
    assert (moved.get("plan") or {}).get("undo") is None
    assert moved["undo_unavailable_reason"] == "changed_since_apply"


# ── a schedule card's Undo deletes only while the job still matches ──


def test_schedule_undo_compare_and_delete_refuses_a_job_edited_in_the_window(tmp_path, monkeypatch):
    """The cron store's compare-and-delete re-reads and fingerprints the live
    job under the same lock as the delete, using the digest the card recorded at
    apply. A job edited after that digest was taken fails the match and the
    delete is refused, so the edit survives."""
    from kiro_crew.cron import CronService

    service = CronService(base_dir=tmp_path)
    job = service.add_job("daily", "run", cron_expr="0 9 * * *")
    snapshot = cards.cron_job_revision(service.get_job(job.id))

    service.update_job(job.id, name="renamed by another tab")

    audited: list[dict] = []
    from kiro_crew import sel as sel_mod

    class _Sel:
        def log_api_access(self, **kw):
            audited.append(kw)

    monkeypatch.setattr(sel_mod, "sel", lambda: _Sel())
    outcome = asyncio.run(
        service.remove_job_if_async(
            job.id,
            expected_revision=snapshot,
            revision_of=cards.cron_job_revision,
            actor="dashboard",
            source="api_cron_delete",
        )
    )
    assert outcome == "changed"
    # A refused delete is audited as refused, not as a missing job.
    assert audited and audited[-1]["outcome"] == "denied"
    assert "changed_since_apply" in audited[-1]["resources"]
    survivor = service.get_job(job.id)
    assert survivor is not None and survivor.name == "renamed by another tab"


def test_schedule_undo_compare_and_delete_removes_a_still_matching_job(tmp_path):
    """When nothing changed in the window, the compare-and-delete removes the
    card's own schedule exactly as the plain delete would."""
    from kiro_crew.cron import CronService

    service = CronService(base_dir=tmp_path)
    job = service.add_job("daily", "run", cron_expr="0 9 * * *")
    snapshot = cards.cron_job_revision(service.get_job(job.id))

    outcome = asyncio.run(
        service.remove_job_if_async(
            job.id,
            expected_revision=snapshot,
            revision_of=cards.cron_job_revision,
            actor="dashboard",
            source="api_cron_delete",
        )
    )
    assert outcome == "removed"
    assert service.get_job(job.id) is None


def test_schedule_card_undo_through_the_hook_refuses_a_delete_raced_by_another_tab(tmp_path):
    """Another tab edits the schedule while the hook flushes the Undo's admission;
    the real ``api_cron_delete`` refuses with ``changed_since_apply``."""
    from kiro_crew.cron import CronService

    service = CronService(base_dir=tmp_path / "crons")
    job = service.add_job("daily", "run", cron_expr="0 9 * * *")
    job_id = job.id

    class _State(FakeState):
        def __init__(self, store: CardStore, crons: CronService) -> None:
            super().__init__(store)
            self.crons = crons

        def push_refresh(self, _kind: str) -> None:
            self.frames.append(("refresh", {"kind": _kind}))

    store = CardStore(tmp_path / "c.json")
    state = _State(store, service)

    # The applied schedule.create card, built as the apply path leaves it: the
    # after-snapshot is the live job's digest and the undo plan deletes by id.
    params = catalog.validate_params(
        "schedule.create", {"name": "daily", "message": "run", "cron_expr": "0 9 * * *"}
    )
    preview = catalog.build_preview("schedule.create", params, {"exists": False}, {})
    rec = store.propose(
        slot_key=SLOT,
        session_key=SK,
        kind="schedule.create",
        params=params,
        reason="daily run",
        preview=preview,
        before={"exists": False},
        context={},
    )
    store.begin_step(rec, revision=rec["revision"], op="apply", index=0)
    store.record_success(rec, op="apply", index=0, evidence={"id": job_id})
    after = {
        "exists": True,
        "name": job.name,
        "revision": cards.cron_job_revision(service.get_job(job_id)),
    }
    undo, reason = catalog.build_undo(
        "schedule.create",
        params,
        {"exists": False},
        rec["evidence"],
        catalog.applied_write_count(rec["plan"]["apply"], len(rec["evidence"])),
        {},
    )
    assert undo is not None, reason
    store.complete_apply(rec, after=after, undo=undo, undo_reason=reason)
    card = store.public(rec)

    async def read_state(kind, p, evidence, *, state, app):
        job_now = service.get_job(job_id)
        if job_now is None:
            return {"exists": False}
        return {
            "exists": True,
            "name": job_now.name,
            "revision": cards.cron_job_revision(job_now),
        }

    def _app() -> web.Application:
        from kiro_crew.dashboard.handlers.cron import api_cron_delete

        app = web.Application(middlewares=[_fake_auth, routes.change_card_middleware])
        app["state"] = state
        routes.register_change_card_routes(app)
        app.router.add_delete("/api/crons/{job_id}", api_cron_delete)
        return app

    # The edit lands DURING the flush between the Undo's precheck and the
    # delete, exactly the window the compare-and-delete closes.
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
        with mock.patch.object(
            history, "delete_job_history_sync", wraps=history.delete_job_history_sync
        ) as del_hist:
            with mock.patch.object(cards, "read_state", read_state):
                client = TestClient(TestServer(_app()))
                await client.start_server()
                try:
                    with mock.patch.object(store, "flush", flush_then_edit):
                        resp = await client.delete(
                            f"/api/crons/{job_id}",
                            headers=_card_headers(card, op="undo", step=0),
                        )
                        data = await resp.json()
                    if routes._BACKGROUND:
                        await asyncio.gather(*list(routes._BACKGROUND))
                    return resp.status, data, del_hist.call_count
                finally:
                    await client.close()

    status, data, history_deletes = asyncio.run(main())
    assert edited["done"], "the racing edit must have run in the flush window"
    assert status == 409 and data["code"] == "changed_since_apply"
    # The other tab's job and its run history survive untouched: the delete was
    # refused before it could reach history cleanup.
    survivor = service.get_job(job_id)
    assert survivor is not None and survivor.name == "renamed by another tab"
    assert history_deletes == 0


@pytest.mark.parametrize(
    ("kind", "raw", "snapshot", "evidence"),
    [
        (
            "mcp.install",
            {"provider": "official", "id": "io.github/x"},
            {"exists": False, "name": "x"},
            [{"name": "x"}],
        ),
        (
            "mcp.add_custom",
            {"servers": {"a": {"command": "npx"}}},
            {"exists": {"a": False}},
            [{"added": ["a"]}],
        ),
        ("connection.connect", {"slug": "github"}, {"known": True, "granted": False}, [{}]),
        ("trust.app", {"name": "radar"}, {"trusted": False}, [{}]),
        (
            "denied_command",
            {"action": "add", "pattern": "curl evil.example", "note": "n"},
            {"exists": False},
            [{}],
        ),
        (
            "schedule.update",
            {"id": "j1", "fields": {"message": "new text"}},
            {"exists": True, "fields": {"message": "old text"}},
            [{}],
        ),
    ],
)
def test_a_card_whose_undo_would_delete_by_name_offers_no_undo(kind, raw, snapshot, evidence):
    # Their stores take no revision, so an Undo could delete an entry the user
    # put back under the same name since, or (schedule.update) overwrite an edit
    # another tab saved; the card says up front it has none.
    p = catalog.validate_params(kind, raw)
    preview = catalog.build_preview(kind, p, snapshot, {})
    assert preview["undo_unavailable_reason"] == catalog.UNDO_NO_IDENTITY_CHECK
    undo, reason = catalog.build_undo(kind, p, snapshot, evidence, 1, {"rule_id": "r1"})
    assert undo is None and reason == catalog.UNDO_NO_IDENTITY_CHECK


def test_mcp_scope_fingerprints_see_each_scope_not_secret_values(tmp_path, monkeypatch):
    from kiro_crew.dashboard.handlers import mcp as mcp_handlers

    crew, glob, agent = tmp_path / "crew.json", tmp_path / "global.json", tmp_path / "agent.json"

    def write(path, spec):
        path.write_text(json.dumps({"mcpServers": {"srv": spec} if spec else {}}))

    monkeypatch.setattr(
        mcp_handlers,
        "_uninstall_scope_files",
        lambda: [("kirocrew", crew), ("kiroGlobal", glob), ("agent", agent)],
    )
    monkeypatch.setattr(cards, "_mcp_value_key", lambda: b"k" * 32)
    write(crew, {"command": "node", "args": ["s.js"], "env": {"API_KEY": SECRET_VALUE}})
    write(glob, None)
    write(agent, {"command": "rendered"})
    first = cards._mcp_scopes("srv")
    assert set(first) == {"kirocrew"}  # the rendered agent file is not a scope
    assert SECRET_VALUE not in json.dumps(first)
    write(crew, {"command": "node", "args": ["s.js"], "env": {"API_KEY": "another-value"}})
    assert cards._mcp_scopes("srv") != first  # an edited env value is an edit
    write(crew, {"command": "node", "args": ["s.js"], "env": {"API_KEY": SECRET_VALUE}})
    assert cards._mcp_scopes("srv") == first
    write(glob, {"command": "other"})  # a same-named server added elsewhere
    second = cards._mcp_scopes("srv")
    assert second != first
    after = {"exists": {"srv": True}, "scopes": {"srv": first}}
    assert cards.undo_snapshot_changed(
        "mcp.add_custom", {"exists": {"srv": True}, "scopes": {"srv": second}}, after
    )
    # Already gone everywhere: a retry may finish the rest.
    gone = {"exists": {"srv": False}, "scopes": {"srv": {}}}
    assert not cards.undo_snapshot_changed("mcp.add_custom", gone, after)
    # Removed from one scope, unchanged in the other: the retry may go on.
    both = {"kirocrew": "d1", "kiroGlobal": "d2"}
    assert not cards.undo_snapshot_changed(
        "mcp.install", {"scopes": {"kiroGlobal": "d2"}}, {"scopes": both}
    )
    # A scope that cannot be read is never taken as gone.
    glob.write_text("{not json")
    assert cards._mcp_scopes("srv")["kiroGlobal"] == "unreadable"
    assert cards.undo_snapshot_changed(
        "mcp.install", {"scopes": {"kiroGlobal": "unreadable"}}, {"scopes": both}
    )


def test_memory_names_only_what_an_mcp_undo_removed():
    line = catalog.describe_manual_change(
        ("POST", "/api/mcp/apply"), {}, {"changes": [{"name": "a", "uninstall": True}]}
    )
    assert line == "ran MCP uninstall for a"
    assert catalog.describe_manual_change(("POST", "/api/mcp/apply"), {}, {"changes": []}) is None


def test_a_template_skill_card_snapshots_the_mapping_and_undo_restores_it(monkeypatch):
    from kiro_crew.dashboard.handlers import _shared
    from kiro_crew.dashboard.handlers import agents as agents_handlers

    spec = {"name": "t", "resources": ["skill://a/SKILL.md", "skill://b/SKILL.md"]}
    monkeypatch.setattr(agents_handlers, "_agent_detail_candidates", lambda name: [("p", spec)])
    monkeypatch.setattr(
        _shared,
        "agent_skill_views",
        lambda data, path, state, *a, **k: (
            [u.split("//")[1].split("/")[0] for u in data.get("resources") or []],
            [],
        ),
    )
    params = {"template": "t", "fields": {"skills": ["c"]}}
    before = cards._template_state_with_skills(params, None)
    assert before["fields"]["skills"] == ["a", "b"]
    undo, reason = catalog.build_undo("template.update", params, before, [{}], 1)
    assert reason is None and undo[0]["body"] == {"skills": ["a", "b"]}
    spec["resources"] = ["skill://a/SKILL.md"]  # a later skill edit
    assert cards.canonical(cards._template_state_with_skills(params, None)) != cards.canonical(
        before
    )


def test_a_disconnect_that_left_credentials_is_not_undone():
    rec = {"kind": "connection.connect"}
    survivors = asyncio.run(
        cards.undo_survivors(
            rec, state=None, app=None, response={"ok": True, "grantSurviving": ["token file"]}
        )
    )
    assert survivors == ["token file"]
    clean = asyncio.run(
        cards.undo_survivors(rec, state=None, app=None, response={"ok": True, "grantSurviving": []})
    )
    assert clean == []


def test_a_partial_disconnect_records_no_memory_and_its_retry_finishes(
    world, tmp_path, monkeypatch
):
    # Disconnect through the real middleware: the first run leaves the token
    # file behind, so the grant reads as gone while a credential survives.
    grant = {"granted": True}
    surviving = [["token file"], []]

    async def read_state(kind, params, evidence, *, state, app):
        assert kind == "connection.connect"
        return {"known": True, "granted": grant["granted"]}

    monkeypatch.setattr(cards, "read_state", read_state)
    store = CardStore(tmp_path / "c.json")
    store._ensure_loaded()
    undo_step = {
        "method": "POST",
        "path": "/api/connections/disconnect",
        "body": {"slug": "github"},
    }
    rec = {
        "id": "cc_disconnect1",
        "title": "Connect GitHub",
        "risk": "low",
        "slot_key": SLOT,
        "kind": "connection.connect",
        "revision": 1,
        "status": "applied",
        "params": {"slug": "github"},
        "plan": {"apply": [], "undo": [undo_step]},
        "after": {"known": True, "granted": True},
        "evidence": [{}],
        "undo_evidence": [],
        "progress": None,
        "inflight": None,
        "expires_at": 10**12,
    }
    store._cards[rec["id"]] = rec

    async def disconnect(request):
        grant["granted"] = False
        return web.json_response({"ok": True, "grantSurviving": surviving.pop(0)})

    async def main():
        app = _app(world, store)
        app.router.add_post("/api/connections/disconnect", disconnect)
        client = TestClient(TestServer(app))
        await client.start_server()
        try:
            card = {"id": rec["id"], "revision": 1}
            first = await client.post(
                undo_step["path"], json=undo_step["body"], headers=_card_headers(card, op="undo")
            )
            assert first.status == 200, await first.text()
            mid = (
                store._cards[rec["id"]]["status"],
                dict(store._cards[rec["id"]].get("error") or {}),
            )
            grant["granted"] = True  # reconnected meanwhile: never removed by this card
            refused = await client.post(
                undo_step["path"], json=undo_step["body"], headers=_card_headers(card, op="undo")
            )
            assert refused.status == 409, await refused.text()
            grant["granted"] = False
            retry = await client.post(
                undo_step["path"], json=undo_step["body"], headers=_card_headers(card, op="undo")
            )
            if routes._BACKGROUND:
                await asyncio.gather(*list(routes._BACKGROUND))
            return first.status, mid, (retry.status, await retry.text())
        finally:
            await client.close()

    first, (mid_status, mid_error), retry = asyncio.run(main())
    assert first == 200
    assert mid_status == "applied" and mid_error.get("code") == "undo_incomplete"
    assert retry[0] == 200, retry[1]
    assert store._cards[rec["id"]]["status"] == "undone"
    # Only the completed Undo is recorded; the partial one claims nothing.
    assert len([m for m in world.memory if "disconnected" in m]) == 1


def test_connection_undo_refuses_a_reauthorized_grant_after_partial_disconnect(
    world, tmp_path, monkeypatch
):
    # Both credential files survive the first Disconnect, so presence still
    # reads "granted". The user then re-authorizes: same presence, new token
    # artifact. The old card's Undo must refuse rather than revoke it.
    grant = {"stamp": [1, 10]}
    surviving = [["token file", "registration file"]]

    async def read_state(kind, params, evidence, *, state, app):
        assert kind == "connection.connect"
        return {"known": True, "granted": True, "grant": list(grant["stamp"])}

    monkeypatch.setattr(cards, "read_state", read_state)
    store = CardStore(tmp_path / "c.json")
    store._ensure_loaded()
    undo_step = {
        "method": "POST",
        "path": "/api/connections/disconnect",
        "body": {"slug": "github"},
    }
    rec = {
        "id": "cc_disconnect2",
        "title": "Connect GitHub",
        "risk": "low",
        "slot_key": SLOT,
        "kind": "connection.connect",
        "revision": 1,
        "status": "applied",
        "params": {"slug": "github"},
        "plan": {"apply": [], "undo": [undo_step]},
        "after": {"known": True, "granted": True, "grant": [1, 10]},
        "evidence": [{}],
        "undo_evidence": [],
        "progress": None,
        "inflight": None,
        "expires_at": 10**12,
    }
    store._cards[rec["id"]] = rec
    calls = []

    async def disconnect(request):
        calls.append(1)
        return web.json_response({"ok": True, "grantSurviving": surviving.pop(0)})

    async def main():
        app = _app(world, store)
        app.router.add_post("/api/connections/disconnect", disconnect)
        client = TestClient(TestServer(app))
        await client.start_server()
        try:
            card = {"id": rec["id"], "revision": 1}
            first = await client.post(
                undo_step["path"], json=undo_step["body"], headers=_card_headers(card, op="undo")
            )
            grant["stamp"] = [2, 20]  # re-authorized: the token artifact was rewritten
            refused = await client.post(
                undo_step["path"], json=undo_step["body"], headers=_card_headers(card, op="undo")
            )
            return first.status, refused.status
        finally:
            await client.close()

    first, refused = asyncio.run(main())
    assert first == 200
    assert refused == 409
    assert len(calls) == 1  # the new grant was never sent to Disconnect


def test_connection_read_state_carries_the_grant_fingerprint(monkeypatch):
    import kiro_crew.connections.status as status

    async def statuses():
        return [{"slug": "github", "grantPresent": True}]

    monkeypatch.setattr(status, "collect_connection_statuses", statuses)
    monkeypatch.setattr(cards, "_connection_grant_fingerprint", lambda slug: [7, 42])
    got = asyncio.run(
        cards.read_state("connection.connect", {"slug": "github"}, [], state=None, app=None)
    )
    assert got == {"known": True, "granted": True, "grant": [7, 42]}


def test_connection_grant_fingerprint_reads_the_provider_token_artifact(monkeypatch):
    from kiro_crew import mcp_grant
    from kiro_crew.connections import registry

    seen = []
    monkeypatch.setattr(
        registry,
        "get_visible_providers",
        lambda: [{"slug": "github", "mcp_url": "https://example.test/mcp"}],
    )

    def stamp(url):
        seen.append(url)
        return (123, 45)

    monkeypatch.setattr(mcp_grant, "grant_fingerprint", stamp)
    assert cards._connection_grant_fingerprint("github") == [123, 45]
    assert seen == ["https://example.test/mcp"]
    # An unknown provider and an unreadable artifact both read as "no grant".
    assert cards._connection_grant_fingerprint("nope") is None
    monkeypatch.setattr(mcp_grant, "grant_fingerprint", lambda url: None)
    assert cards._connection_grant_fingerprint("github") is None
