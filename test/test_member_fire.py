"""Fire (design: Crew Member = Custom Agent + Wrapper, rollout step 5).

The reverse of hire: the wrapper row, the member's own agent file and its
pristine copy go; the private memory store is archived under the retirement
marker; what the member LIVED -- its DM thread, activity, briefing, rules -- is
archived, not destroyed, unless the request says ``purge``.
"""

from __future__ import annotations

import asyncio
import json
import os
import shutil
import threading
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
import test_member_hire as _hire
from aiohttp import web
from aiohttp.test_utils import TestClient, TestServer

from kiro_crew import agent_state, member_templates, members
from kiro_crew.config.loader import KiroCrewConfig

APP = _hire.APP
_store_hire = _hire._store_hire
_owner_caller = _hire._owner_caller
agents_dir = _hire.agents_dir
store_app = _hire.store_app

MEMBER = "Pager-triage"
SLUG = "pager-triage"


class _Log:
    """A conversation log that knows which transcripts exist. Keys match by
    prefix: a member's canonical history key carries its private-store
    generation (``dashboard:member-<slug>.memory-<store>``), minted at hire,
    after the log was built."""

    def __init__(self, *keys: str):
        self.keys = set(keys)

    def has_log(self, key: str) -> bool:
        return any(key == k or key.startswith(k + ".memory-") for k in self.keys)


def _canonical(slug: str, member: str = MEMBER) -> tuple[str, str, str]:
    """The slot key the thread open would bind for *member* NOW, its store and
    its history key -- what a binding must carry to be this member's."""
    from kiro_crew.dashboard.chat_utils import _history_key_for
    from kiro_crew.dashboard.handlers import members as members_handlers

    slot_key, store = members_handlers._member_thread_slot(KiroCrewConfig.load(), member, slug)
    return slot_key, store, _history_key_for(slot_key)


def _hist(slug: str, member: str = MEMBER) -> str:
    return _canonical(slug, member)[2]


def _app(state: MagicMock | None = None) -> web.Application:
    from kiro_crew.dashboard.handlers import (
        api_member_fire,
        api_member_hire,
        api_member_role_update_get,
        api_member_rules_put,
        api_members,
    )

    @web.middleware
    async def _auth(request: web.Request, handler):
        request.setdefault("app", "")
        request.setdefault("user", "local-app")
        return await handler(request)

    app = web.Application(middlewares=[_auth])
    app["state"] = state or MagicMock(sessions=None, _slots={}, conversation_log=None)
    app.router.add_post("/api/members", api_member_hire)
    app.router.add_get("/api/members", api_members)
    app.router.add_get("/api/members/{member}/role-update", api_member_role_update_get)
    app.router.add_post("/api/members/{member}/fire", api_member_fire)
    app.router.add_put("/api/members/{slug}/rules", api_member_rules_put)
    return app


async def _hire_pager(client) -> None:
    resp = await client.post("/api/members", json=_store_hire("Pager triage"))
    assert resp.status == 200, await resp.text()


def _live_in(slug: str) -> Path:
    """Give the member lived state: activity, a rules file, a DM binding."""
    assert members.record_activity(MEMBER, "s1", "persistent", project="p")
    members.write_member_rules(slug, member=MEMBER, text="Always page the on-call first.")
    if MEMBER in KiroCrewConfig.load().agents:
        slot_key, store, _hist_key = _canonical(slug)
    else:  # a retire exercised without a hire: the plain V1 slot
        slot_key, store = f"member-{slug}", ""
    members.write_dm_binding(slug, member=MEMBER, slot_key=slot_key, memory_store=store)
    return members.member_dir(slug)


class TestFire:
    @pytest.mark.asyncio
    async def test_gate_fire_retires_the_member_and_archives_what_it_lived(
        self, agents_dir: Path, store_app: Path
    ):
        """The step-5 gate. Row, copy, pristine copy gone; store archived (not
        erased); activity, rules and the thread pointer moved to
        members/.retired with a fired.json; the binding gone; the thread's
        history key answered so it can be found in the History tab."""
        state = MagicMock(
            sessions=None, _slots={}, conversation_log=_Log(f"dashboard:member-{SLUG}")
        )
        async with TestClient(TestServer(_app(state))) as client:
            await _hire_pager(client)
            space = _live_in(SLUG)
            assert (space / "activity.jsonl").exists()
            row = KiroCrewConfig.load().agents[MEMBER]
            store = row.memory_store
            hist = _hist(SLUG)
            resp = await client.post(f"/api/members/{MEMBER}/fire", json={})
            assert resp.status == 200, await resp.text()
            body = await resp.json()
            roster = {
                r["name"]: r for r in (await (await client.get("/api/members")).json())["members"]
            }
        assert body == {
            "ok": True,
            "thread": {"state": "archived"},
            "lived_state": "archived",
        }
        cfg = KiroCrewConfig.load()
        assert MEMBER not in cfg.agents
        assert MEMBER not in roster
        # The definition and its pristine copy are gone; the lineage too.
        assert not (agents_dir / f"{MEMBER}.json").exists()
        assert agent_state.get_fork_info(MEMBER) is None
        assert not member_templates.pristine_copy_path(MEMBER).exists()
        # ... and so is its enrollment: a fired crewmate is not a member.
        assert agent_state.get_crewmate_record(MEMBER, strict=True) is None
        # The shared materialized template is untouched.
        assert (agents_dir / f"{APP}--triage.json").exists()
        # The private store is archived under the retirement marker, never erased:
        # the files stay, and the store refuses use as archived.
        from kiro_crew.memory_stores import (
            UnknownMemoryStore,
            memory_stores_root,
            require_member_memory_not_archived,
        )

        assert (memory_stores_root() / store).is_dir()
        with pytest.raises(UnknownMemoryStore):
            require_member_memory_not_archived(store, expected_owner=MEMBER)
        # Lived state moved to .retired with the record; the binding is gone.
        assert not space.exists()
        retired = [p for p in members.retired_root().iterdir() if p.name.startswith(f"{SLUG}--")]
        assert len(retired) == 1
        assert (retired[0] / "activity.jsonl").exists()
        # The rules archive under the PROTECTED rules subtree (trust/), not in the
        # agent-writable members root beside the activity.
        assert not (retired[0] / "rules.json").exists()
        rules_archive = [
            p for p in members.retired_rules_root().iterdir() if p.name.startswith(f"{SLUG}--")
        ]
        assert len(rules_archive) == 1
        assert json.loads(rules_archive[0].read_text())["member"] == MEMBER
        assert "trust" in rules_archive[0].parts and "members" not in rules_archive[0].parts
        assert members.read_member_rules(SLUG, MEMBER) == ""
        record = json.loads((retired[0] / members.FIRED_RECORD_FILE).read_text())
        assert record["member"] == MEMBER
        assert record["display_name"] == "Pager triage"
        assert record["thread_history_key"] == hist
        assert record["template"] == f"{APP}/triage"
        assert record["fired_at"].endswith("Z")
        assert members.read_dm_binding(SLUG) is None

    @pytest.mark.asyncio
    async def test_the_open_thread_is_closed_like_the_tab_does_before_the_row_goes(
        self, agents_dir: Path, store_app: Path
    ):
        """A live DM session must not keep running against a row that is about
        to vanish: the slot is closed first, through the tab's own close path."""
        closed: list[tuple[str, object]] = []
        fake_slot = object()
        slots: dict[str, object] = {}
        state = MagicMock(sessions=None, _slots=slots, conversation_log=None)

        async def fake_close(st, slot, name):
            # The row is still there when the thread closes.
            assert MEMBER in KiroCrewConfig.load().agents
            closed.append((name, slot))

        async with TestClient(TestServer(_app(state))) as client:
            await _hire_pager(client)
            _live_in(SLUG)
            slot_key = _canonical(SLUG)[0]
            slots[slot_key] = fake_slot
            with patch("kiro_crew.dashboard.chat_handlers.close_slot", fake_close):
                resp = await client.post(f"/api/members/{MEMBER}/fire", json={})
                assert resp.status == 200, await resp.text()
        assert closed == [(slot_key, fake_slot)]
        assert MEMBER not in KiroCrewConfig.load().agents

    @pytest.mark.asyncio
    async def test_a_thread_that_cannot_close_leaves_the_member_whole(
        self, agents_dir: Path, store_app: Path
    ):
        from kiro_crew.dashboard.chat_handlers import SlotCloseError

        slots: dict[str, object] = {}
        state = MagicMock(sessions=None, _slots=slots, conversation_log=None)

        async def refuse(st, slot, name):
            raise SlotCloseError("history save failed", "history_save_failed")

        async with TestClient(TestServer(_app(state))) as client:
            await _hire_pager(client)
            space = _live_in(SLUG)
            slots[_canonical(SLUG)[0]] = object()
            with patch("kiro_crew.dashboard.chat_handlers.close_slot", refuse):
                resp = await client.post(f"/api/members/{MEMBER}/fire", json={})
                assert resp.status == 500
                assert (await resp.json())["code"] == "thread_close_failed"
        assert MEMBER in KiroCrewConfig.load().agents
        assert (agents_dir / f"{MEMBER}.json").exists()
        assert space.exists()
        assert members.read_dm_binding(SLUG) is not None

    @pytest.mark.asyncio
    async def test_purge_removes_the_lived_state_and_reports_the_thread_honestly(
        self, agents_dir: Path, store_app: Path
    ):
        """Purge is the explicit request: no .retired copy. The transcript goes
        through the history-delete path; when that path refuses (patched to,
        here -- a scheduled job's unreadable claim on the transcript) the thread
        is reported KEPT, never claimed gone, and the answer says WHY so the
        notice names the repair instead of guessing at one."""
        state = MagicMock(
            sessions=None, _slots={}, conversation_log=_Log(f"dashboard:member-{SLUG}")
        )
        with patch(
            "kiro_crew.dashboard.handlers.members._purge_thread_history",
            return_value="cron_claim_unreadable",
        ):
            async with TestClient(TestServer(_app(state))) as client:
                await _hire_pager(client)
                space = _live_in(SLUG)
                _hist(SLUG)
                resp = await client.post(f"/api/members/{MEMBER}/fire", json={"purge": True})
                assert resp.status == 200, await resp.text()
                body = await resp.json()
        assert body["lived_state"] == "purged"
        assert body["thread"] == {"state": "kept", "kept_reason": "cron_claim_unreadable"}
        assert not space.exists()
        assert not members.retired_root().exists() or not any(
            p.name.startswith(f"{SLUG}--") for p in members.retired_root().iterdir()
        )
        assert members.read_dm_binding(SLUG) is None
        assert members.read_member_rules(SLUG, MEMBER) == ""
        assert MEMBER not in KiroCrewConfig.load().agents

    @pytest.mark.asyncio
    async def test_purge_deletes_the_transcript_when_the_history_path_allows(
        self, agents_dir: Path, store_app: Path
    ):
        state = MagicMock(
            sessions=None, _slots={}, conversation_log=_Log(f"dashboard:member-{SLUG}")
        )
        with patch(
            "kiro_crew.dashboard.handlers.members._purge_thread_history", return_value=""
        ) as purge:
            async with TestClient(TestServer(_app(state))) as client:
                await _hire_pager(client)
                _live_in(SLUG)
                hist = _hist(SLUG)
                resp = await client.post(f"/api/members/{MEMBER}/fire", json={"purge": True})
                assert resp.status == 200, await resp.text()
                assert (await resp.json())["thread"]["state"] == "purged"
        purge.assert_called_once_with(state, hist)

    @pytest.mark.asyncio
    async def test_a_bound_thread_with_no_transcript_reads_as_none_not_kept(
        self, agents_dir: Path, store_app: Path
    ):
        """Opened but nothing said: History holds no transcript, so a purge has
        nothing to delete and must not report a thread it kept."""
        state = MagicMock(sessions=None, _slots={}, conversation_log=_Log())
        async with TestClient(TestServer(_app(state))) as client:
            await _hire_pager(client)
            _live_in(SLUG)
            _hist(SLUG)
            resp = await client.post(f"/api/members/{MEMBER}/fire", json={"purge": True})
            assert resp.status == 200, await resp.text()
            body = await resp.json()
        assert body["thread"] == {"state": "none"}
        assert body["lived_state"] == "purged"

    @pytest.mark.asyncio
    async def test_a_fired_crewmate_is_not_resurrected_by_a_row_under_its_id(
        self, agents_dir: Path, store_app: Path
    ):
        """Membership is the enrollment record, not the config row: a row that
        reappears under a fired member's id -- a sync, a restart replaying an
        old config, a hand edit -- is a session agent until a hire enrolls it,
        so the fired crewmate never comes back onto the roster by itself."""
        async with TestClient(TestServer(_app())) as client:
            await _hire_pager(client)
            assert agent_state.get_crewmate_record(MEMBER, strict=True) is not None
            resp = await client.post(f"/api/members/{MEMBER}/fire", json={})
            assert resp.status == 200, await resp.text()
            assert agent_state.get_crewmate_record(MEMBER, strict=True) is None
            # A row under the same id comes back the way a sync would put it there.
            from kiro_crew.config.loader import config_path

            raw = json.loads(config_path().read_text())
            raw.setdefault("agents", {})[MEMBER] = {
                "display_name": "Pager triage",
                "memory_store": "default",
            }
            config_path().write_text(json.dumps(raw))
            assert MEMBER in KiroCrewConfig.load().agents
            roster = (await (await client.get("/api/members")).json())["members"]
        assert MEMBER not in {r["name"] for r in roster}
        assert agent_state.get_crewmate_record(MEMBER, strict=True) is None

    @pytest.mark.asyncio
    async def test_a_rules_save_cannot_land_behind_a_concurrent_fire(
        self, agents_dir: Path, store_app: Path
    ):
        """The rules PUT checks membership and writes under the config lock the
        fire holds from its row delete through its cleanup. A save that read
        the row a moment before the fire would otherwise land its rules AFTER
        the fire archived them: a live rules file for a member the roster does not
        hold, inherited by the next hire on the slug. Here the PUT is held
        inside its write; the fire must wait for it, then archive what it
        wrote, and nothing is left live."""
        loop = asyncio.get_running_loop()
        in_write = asyncio.Event()
        proceed = threading.Event()
        real_write = members.write_member_rules

        def slow_write(*args, **kwargs):
            loop.call_soon_threadsafe(in_write.set)
            assert proceed.wait(10)
            return real_write(*args, **kwargs)

        owner = patch(
            "kiro_crew.dashboard.handlers.members.require_owner_dashboard_request",
            new=AsyncMock(return_value=None),
        )
        async with TestClient(TestServer(_app())) as client:
            await _hire_pager(client)
            _live_in(SLUG)
            with owner, patch("kiro_crew.members.write_member_rules", side_effect=slow_write):
                put = asyncio.create_task(
                    client.put(
                        f"/api/members/{SLUG}/rules",
                        json={"member": MEMBER, "rules": "Never page after midnight."},
                    )
                )
                await asyncio.wait_for(in_write.wait(), 10)
                fire = asyncio.create_task(client.post(f"/api/members/{MEMBER}/fire", json={}))
                await asyncio.sleep(0.3)
                # The fire waits for the save's hold instead of racing past it.
                assert not fire.done()
                proceed.set()
                put_resp = await put
                fire_resp = await fire
            assert put_resp.status == 200, await put_resp.text()
            assert fire_resp.status == 200, await fire_resp.text()
        assert MEMBER not in KiroCrewConfig.load().agents
        # What the save wrote went with the fire into the archive; nothing live.
        assert members.read_member_rules(SLUG, MEMBER) == ""
        assert not members.member_rules_path(SLUG).exists()
        archived = [
            p for p in members.retired_rules_root().iterdir() if p.name.startswith(f"{SLUG}--")
        ]
        assert len(archived) == 1
        assert json.loads(archived[0].read_text())["rules"] == "Never page after midnight."

    @pytest.mark.asyncio
    async def test_activity_landing_after_the_archive_cannot_survive_a_completed_fire(
        self, agents_dir: Path, store_app: Path
    ):
        """A routing decision in flight while the member is fired records
        activity into ``members/<slug>``. Landing between the archive and the
        marker clear, it recreated the live space and the fire cleared its
        marker over it -- the next member hired onto the slug inherited a
        stranger's entry. Two guards close it: the activity writer refuses to
        recreate a space whose member has a fire pending, and the fire
        re-checks the space before the marker goes, answering fire_incomplete
        (resumable) when something recreated it regardless."""
        from kiro_crew.dashboard.handlers import members as handlers

        async with TestClient(TestServer(_app())) as client:
            await _hire_pager(client)
            _live_in(SLUG)
            # 1. The writer's guard: with the marker up and the space gone, a
            #    record_activity call writes nothing and creates nothing.
            handlers._write_fire_marker(MEMBER, {"slug": SLUG, "member": MEMBER})
            space = members.member_dir(SLUG)
            shutil.rmtree(space)
            assert members.record_activity(MEMBER, "s-late", "persistent", project="p") is False
            assert not space.exists()
            assert handlers._clear_fire_marker(MEMBER)
            # Without a marker the same call records (the guard is the marker).
            assert members.record_activity(MEMBER, "s-ok", "persistent", project="p") is True
            assert space.is_dir()
            # 2. The fire's guard: a space recreated after the archive (by a
            #    writer that got past the check) leaves the marker in place.
            real_retire = members.retire_member_space

            def retire_then_recreate(*args, **kwargs):
                archived = real_retire(*args, **kwargs)
                space.mkdir(parents=True, exist_ok=True)
                (space / "activity.jsonl").write_text('{"member": "stranger"}\n')
                return archived

            with patch("kiro_crew.members.retire_member_space", side_effect=retire_then_recreate):
                resp = await client.post(f"/api/members/{MEMBER}/fire", json={})
            assert resp.status == 500, await resp.text()
            body = await resp.json()
            assert body["code"] == "fire_incomplete"
            assert handlers._fire_marker_path(MEMBER).exists()
            assert MEMBER not in KiroCrewConfig.load().agents
            # The resumed fire finds the space beside its archive and says so
            # (the existing resume contract: the operator removes the
            # recreated space, nothing is guessed); with it gone, it finishes.
            resp = await client.post(f"/api/members/{MEMBER}/fire", json={})
            assert resp.status == 500
            assert "exists again beside its archive" in (await resp.json())["error"]
            shutil.rmtree(space)
            resp = await client.post(f"/api/members/{MEMBER}/fire", json={})
            assert resp.status == 200, await resp.text()
        assert not space.exists()
        assert not handlers._fire_marker_path(MEMBER).exists()

    @pytest.mark.asyncio
    async def test_a_dangling_link_planted_at_the_space_after_the_archive_keeps_the_marker(
        self, agents_dir: Path, store_app: Path, tmp_path: Path
    ):
        """The last check before the marker clears must see the LITERAL entry at
        ``members/<slug>``. Resolved first, a dangling link there read as
        "gone": the marker cleared with the link in place, and the next member
        hired onto the slug wrote its lived state through it -- wherever the
        planter pointed it. The check is an ``lstat`` of the literal name
        relative to the pinned members root: a link counts as present, the fire
        answers fire_incomplete, and the resumed fire removes the link as a
        link (its target untouched) before it finishes."""
        from kiro_crew.dashboard.handlers import members as handlers

        elsewhere = tmp_path / "victim"
        space = members.member_dir(SLUG)
        assert members.member_space_exists(SLUG) is False
        space.parent.mkdir(parents=True, exist_ok=True)
        space.symlink_to(elsewhere, target_is_directory=True)  # dangling: no target yet
        assert members.member_space_exists(SLUG) is True
        space.unlink()
        async with TestClient(TestServer(_app())) as client:
            await _hire_pager(client)
            _live_in(SLUG)
            real_retire = members.retire_member_space

            def retire_then_plant(*args, **kwargs):
                archived = real_retire(*args, **kwargs)
                space.symlink_to(elsewhere, target_is_directory=True)
                return archived

            with patch("kiro_crew.members.retire_member_space", side_effect=retire_then_plant):
                resp = await client.post(f"/api/members/{MEMBER}/fire", json={})
            assert resp.status == 500, await resp.text()
            body = await resp.json()
            assert body["code"] == "fire_incomplete"
            assert handlers._fire_marker_path(MEMBER).exists()
            assert space.is_symlink()
            # The resume retires the link AS a link and finishes; nothing was
            # created at the link's target.
            resp = await client.post(f"/api/members/{MEMBER}/fire", json={})
            assert resp.status == 200, await resp.text()
        assert not space.exists() and not space.is_symlink()
        assert not elsewhere.exists()
        assert not handlers._fire_marker_path(MEMBER).exists()
        assert members.member_space_exists(SLUG) is False

    @pytest.mark.asyncio
    async def test_a_member_that_never_lived_leaves_nothing_to_archive(
        self, agents_dir: Path, store_app: Path
    ):
        """A local hire seeds no briefing; a member that never opened its thread
        or wrote a rule has no space at all, so there is nothing to archive."""
        async with TestClient(TestServer(_app())) as client:
            resp = await client.post("/api/members", json=_hire._hire("Plain"))
            assert resp.status == 200, await resp.text()
            resp = await client.post("/api/members/Plain/fire")
            assert resp.status == 200, await resp.text()
            body = await resp.json()
        assert body["thread"] == {"state": "none"}
        assert body["lived_state"] == "none"
        assert not members.retired_root().exists() or not any(members.retired_root().iterdir())

    @pytest.mark.asyncio
    async def test_the_default_member_cannot_be_fired_and_bad_bodies_are_refused(
        self, agents_dir: Path, store_app: Path
    ):
        async with TestClient(TestServer(_app())) as client:
            resp = await client.post("/api/members/default/fire", json={})
            assert resp.status == 409
            assert (await resp.json())["code"] == "cannot_fire_default"
            resp = await client.post("/api/members/nobody/fire", json={})
            assert resp.status == 404
            await _hire_pager(client)
            resp = await client.post(f"/api/members/{MEMBER}/fire", json={"purge": "yes"})
            assert resp.status == 400
            assert (await resp.json())["code"] == "invalid_purge"
            resp = await client.post(f"/api/members/{MEMBER}/fire", json=[])
            assert resp.status == 400
        assert MEMBER in KiroCrewConfig.load().agents
        assert "default" in KiroCrewConfig.load().agents

    @pytest.mark.asyncio
    async def test_only_an_enrolled_crewmate_can_be_fired(self, agents_dir: Path, store_app: Path):
        """A row that is not a crewmate -- a synced file, an app's materialized
        agent, a plain crew -- is a session agent: the fire answers 404 for it
        and removes nothing (no row, no agent file, no marker). An unreadable
        enrollment record is 503, never read as "not a member"."""
        from kiro_crew.config.loader import config_path
        from kiro_crew.dashboard.handlers import members as handlers

        async with TestClient(TestServer(_app())) as client:
            raw = json.loads(config_path().read_text())
            raw["agents"]["Plain-crew"] = {"kiro_agent": _hire.SOURCE, "memory_store": "default"}
            config_path().write_text(json.dumps(raw))
            before = sorted(p.name for p in agents_dir.iterdir())
            resp = await client.post("/api/members/Plain-crew/fire", json={})
            assert resp.status == 404, await resp.text()
            assert (await resp.json())["code"] == "member_not_found"
            assert "Plain-crew" in KiroCrewConfig.load().agents
            assert sorted(p.name for p in agents_dir.iterdir()) == before
            assert not handlers._fire_marker_path("Plain-crew").exists()
            # An enrolled crewmate whose record cannot be read is refused, not fired.
            await _hire_pager(client)
            with patch(
                "kiro_crew.dashboard.handlers.members.enrolled_member_ids",
                side_effect=ValueError("agent_state_file_invalid"),
            ):
                resp = await client.post(f"/api/members/{MEMBER}/fire", json={})
            assert resp.status == 503, await resp.text()
            assert (await resp.json())["code"] == "members_unavailable"
            assert MEMBER in KiroCrewConfig.load().agents

    @pytest.mark.asyncio
    async def test_a_member_un_enrolled_inside_the_window_is_not_fired(
        self, agents_dir: Path, store_app: Path
    ):
        """The enrollment check runs before the lock; a fire or a delete in
        another process can un-enroll the member in between. The delete's own
        locked mutation re-checks the record, so a row without a record of this
        generation is left alone and the marker cleared."""
        from kiro_crew.dashboard.handlers import members as handlers

        async with TestClient(TestServer(_app())) as client:
            await _hire_pager(client)
            real_write = handlers._write_fire_marker

            def write_then_un_enroll(member, intent):
                real_write(member, intent)
                agent_state.clear_crewmate_record(member)

            with patch(
                "kiro_crew.dashboard.handlers.members._write_fire_marker",
                side_effect=write_then_un_enroll,
            ):
                resp = await client.post(f"/api/members/{MEMBER}/fire", json={})
            assert resp.status == 409, await resp.text()
            body = await resp.json()
            assert body["code"] == "member_changed"
            assert "not an enrolled crewmate" in body["error"]
        cfg = KiroCrewConfig.load()
        assert MEMBER in cfg.agents
        assert (agents_dir / f"{cfg.agents[MEMBER].kiro_agent}.json").exists()
        assert not handlers._fire_marker_path(MEMBER).exists()

    @pytest.mark.asyncio
    async def test_a_member_that_changed_under_the_request_is_not_fired(
        self, agents_dir: Path, store_app: Path
    ):
        """The row must still be the one the request saw when the lock is
        taken: a same-id member re-hired in between is somebody else's."""
        from kiro_crew.config.loader import update_config_locked

        real_load = KiroCrewConfig.load
        calls: list[int] = []

        def load_then_swap(*args, **kwargs):
            cfg = real_load(*args, **kwargs)
            calls.append(1)
            if len(calls) == 1:
                # Between the first read and the locked re-read: a replacement.
                def mutate(doc):
                    doc["agents"][MEMBER]["memory_store"] = "member-pager-triage-replacement"
                    return doc

                update_config_locked(mutate=mutate)
            return cfg

        async with TestClient(TestServer(_app())) as client:
            await _hire_pager(client)
            with patch(
                "kiro_crew.dashboard.handlers.members.KiroCrewConfig.load",
                side_effect=load_then_swap,
            ):
                resp = await client.post(f"/api/members/{MEMBER}/fire", json={})
                assert resp.status == 409, await resp.text()
                assert (await resp.json())["code"] == "member_changed"
        assert MEMBER in KiroCrewConfig.load().agents
        assert (agents_dir / f"{MEMBER}.json").exists()

    @pytest.mark.asyncio
    async def test_the_same_name_can_be_hired_again_after_a_fire(
        self, agents_dir: Path, store_app: Path
    ):
        """The id is free again and the new colleague starts from nothing: no
        activity, no rules, no binding of the fired one leaks into it."""
        async with TestClient(TestServer(_app())) as client:
            await _hire_pager(client)
            _live_in(SLUG)
            resp = await client.post(f"/api/members/{MEMBER}/fire", json={})
            assert resp.status == 200, await resp.text()
            await _hire_pager(client)
        row = KiroCrewConfig.load().agents[MEMBER]
        assert row.kiro_agent == MEMBER
        assert (agents_dir / f"{MEMBER}.json").exists()
        assert members.read_activity(SLUG) == []
        assert members.read_dm_binding(SLUG) is None
        assert member_templates.pristine_copy_path(MEMBER).exists()

    def test_retire_refuses_a_link_planted_at_the_member_directory(
        self, agents_dir: Path, tmp_path: Path
    ):
        """The member directory is agent-writable: a symlink planted at its name
        is removed as a link, never moved or followed into the archive."""
        elsewhere = tmp_path / "elsewhere"
        elsewhere.mkdir()
        (elsewhere / "secret.txt").write_text("x")
        root = members.members_root()
        root.mkdir(parents=True, exist_ok=True)
        link = root / SLUG
        link.symlink_to(elsewhere, target_is_directory=True)
        archived = members.retire_member_space(SLUG, member=MEMBER, record={})
        assert archived is None
        assert not link.exists() and not link.is_symlink()
        assert (elsewhere / "secret.txt").exists()
        assert not members.retired_root().exists() or not any(members.retired_root().iterdir())

    def test_the_fire_markers_live_in_a_gateway_only_leaf(self):
        """The resume TRUSTS a marker's purge, slug and history key, so nothing an
        agent runs may write one: the markers' root is a top-level leaf of the
        data home (never under ``trust/``, which is sandbox-VISIBLE), on the
        sandbox hidden and pre-create lists and on the file-tool secret list --
        pinned via the writer's own constant so a rename cannot unmask it."""
        from kiro_crew import sandbox
        from kiro_crew.config.paths import data_home
        from kiro_crew.dashboard.handlers import members as handlers
        from kiro_crew.security import paths as security_paths

        leaf = handlers.FIRE_MARKERS_DIR_NAME
        assert "/" not in leaf
        assert leaf in sandbox._CREW_HIDDEN_LEAVES
        assert leaf in sandbox._CREW_PRECREATE_HIDDEN_DIR_LEAVES
        assert leaf in security_paths._CREW_SECRET_LEAVES
        assert leaf not in sandbox._CREW_SANDBOX_VISIBLE_LEAVES
        path = handlers._fire_marker_path(MEMBER)
        assert path.parent == data_home().resolve() / leaf
        assert "trust" not in path.parts

    def test_retire_never_follows_a_link_planted_at_a_trust_child(self, agents_dir: Path):
        """``trust/`` is sandbox-writable, so an agent can replace
        ``trust/member-rules`` (or ``member-bindings``) with a directory link to
        the data home; a rules file for a slug named ``config`` would then
        resolve to ``config.json`` and the fire's unlink or replace would
        destroy it. The trust children are taken literally in both flows: the
        fire refuses and the linked target is untouched."""
        from kiro_crew.config.paths import data_home

        home = data_home()
        (home / "config.json").write_text("{}", encoding="utf-8")
        trust = home / "trust"
        trust.mkdir(exist_ok=True)
        planted = trust / members.RULES_DIR_NAME
        if planted.exists() and not planted.is_symlink():
            for child in planted.iterdir():
                child.unlink()
            planted.rmdir()
        planted.symlink_to(home, target_is_directory=True)
        try:
            for flag in (True, False):
                with patch.object(members, "_DIR_FD_SUPPORTED", flag):
                    with pytest.raises(members.MemberSlugError):
                        members.member_rules_path("config")
                    with pytest.raises(members.MemberSlugError):
                        members.retire_member_space(
                            "config", member="config", record={}, purge=True
                        )
                    with pytest.raises(members.MemberSlugError):
                        members.retire_member_space("config", member="config", record={})
            assert (home / "config.json").read_text(encoding="utf-8") == "{}"
            assert planted.is_symlink()
        finally:
            planted.unlink()
        # The same for the bindings directory: nothing is unlinked through the link.
        bindings = trust / members.DM_BINDINGS_DIR_NAME
        if bindings.exists() and not bindings.is_symlink():
            for child in bindings.iterdir():
                child.unlink()
            bindings.rmdir()
        bindings.symlink_to(home, target_is_directory=True)
        try:
            # An obstacle at the trust child is a refusal, never a quiet "nothing
            # to remove": a fire that cleared its marker over it would leave the
            # next member on the slug unable to bind its thread.
            for flag in (True, False):
                with patch.object(members, "_DIR_FD_SUPPORTED", flag):
                    with pytest.raises(members.MemberSlugError):
                        members.remove_dm_binding_of("config", "config")
            assert (home / "config.json").exists()
        finally:
            bindings.unlink()

    def test_a_windows_junction_at_a_trust_child_or_the_members_root_is_refused_too(
        self, agents_dir: Path
    ):
        """On Windows an agent can plant a directory JUNCTION where it cannot
        plant a symlink; ``Path.is_symlink`` is False for one, so a check on it
        alone would treat ``trust/member-rules`` -> data home as a real directory
        and the purge of a member named ``config`` would delete ``config.json``
        through it. Every literal-path check in the retirement flow goes through
        ``platform_compat.is_link_or_junction``. Junctions cannot be created
        here, so the platform probe is made to report the planted names as
        junctions and the flows must refuse exactly as they do for a symlink."""
        from kiro_crew import platform_compat
        from kiro_crew.config.paths import data_home

        home = data_home()
        (home / "config.json").write_text("{}", encoding="utf-8")
        trust = home / "trust"
        trust.mkdir(exist_ok=True)
        rules = trust / members.RULES_DIR_NAME
        rules.mkdir(exist_ok=True)
        root = members.members_root()
        root.mkdir(parents=True, exist_ok=True)
        (root / "config").mkdir(exist_ok=True)
        real = platform_compat.is_link_or_junction

        def junction_at(*planted: Path):
            def probe(path):
                return Path(path) in planted or real(path)

            return probe

        # The trust child is taken literally by BOTH flows; the members root by
        # the path flow (the pinned flow opens it ``O_NOFOLLOW`` relative to the
        # data home and never consults a probe -- and runs where junctions do
        # not exist).
        for planted, flow in ((rules, True), (rules, False), (root, False)):
            with (
                patch.object(platform_compat, "is_link_or_junction", junction_at(planted)),
                patch.object(members, "_DIR_FD_SUPPORTED", flow),
            ):
                with pytest.raises(members.MemberSlugError):
                    members.retire_member_space("config", member="config", record={}, purge=True)
                with pytest.raises(members.MemberSlugError):
                    members.retire_member_space("config", member="config", record={})
        assert (home / "config.json").read_text(encoding="utf-8") == "{}"
        assert (root / "config").is_dir()
        # And a junction planted at the member's own name is unlinked as a
        # link, never removed through (the path flow's unlink goes through
        # ``unlink_link_or_junction``).
        removed: list[Path] = []
        with (
            patch.object(platform_compat, "is_link_or_junction", junction_at(root / "config")),
            patch.object(
                platform_compat, "unlink_link_or_junction", lambda p: removed.append(Path(p))
            ),
            patch.object(members, "_DIR_FD_SUPPORTED", False),
        ):
            members.retire_member_space("config", member="config", record={}, purge=True)
        assert removed == [root / "config"]
        assert (
            root / "config"
        ).is_dir()  # the "junction" itself was handed to the unlinker, not rmtree'd

    def test_retire_refuses_a_plain_file_at_the_member_directory(self, agents_dir: Path):
        """A regular file at ``members/<slug>`` is not a space to archive or
        purge -- and left there, the next member on the slug cannot create its
        directory, so its activity writes fail. Both flows refuse (the fire
        then keeps its marker and says so) instead of returning as if there
        were nothing to retire."""
        root = members.members_root()
        root.mkdir(parents=True, exist_ok=True)
        obstacle = root / SLUG
        obstacle.write_text("not a directory")
        for purge in (False, True):
            with pytest.raises(members.MemberSlugError, match="not a directory"):
                members.retire_member_space(SLUG, member=MEMBER, record={}, purge=purge)
            assert obstacle.is_file()
        # The path flow (what runs where dir_fd is unsupported) refuses the same way.
        with patch.object(members, "_DIR_FD_SUPPORTED", False):
            with pytest.raises(members.MemberSlugError, match="not a directory"):
                members.retire_member_space(SLUG, member=MEMBER, record={})
        assert obstacle.is_file()

    def test_a_link_planted_at_the_rules_name_is_removed_as_a_link(self, agents_dir: Path):
        """A symlink at ``trust/member-rules/<slug>.json`` is not a rules file to
        archive -- but left in place it survives the fire, and the next member
        hired on the slug inherits whatever rules the link resolves to. Both
        flows unlink the LINK (relative to the pinned directory; its target is
        never followed, read or moved); anything else that is not a regular
        file is refused, so the fire keeps its marker over the obstacle."""
        rules = members.member_rules_path(SLUG)
        rules.parent.mkdir(parents=True, exist_ok=True)
        # One target outside the subtree, one INSIDE it (another member's rules,
        # which a resolving path flow would otherwise move as if it were ours).
        outside = agents_dir.parent / "victim-rules.json"
        outside.write_text(json.dumps({"member": "victim", "text": "theirs"}))
        victim = rules.parent / "victim.json"
        victim.write_text(json.dumps({"member": "victim", "text": "theirs"}))
        for flow, target in ((True, outside), (False, outside), (True, victim), (False, victim)):
            rules.symlink_to(target)
            with patch.object(members, "_DIR_FD_SUPPORTED", flow):
                assert members.retire_member_space(SLUG, member=MEMBER, record={}) is None
            assert not rules.is_symlink() and not rules.exists()
            assert json.loads(target.read_text())["text"] == "theirs"
            assert not any(members.retired_rules_root().glob(f"{SLUG}--*"))
        # Anything else at the name is refused, never guessed at.
        rules.mkdir()
        for flow in (True, False):
            with patch.object(members, "_DIR_FD_SUPPORTED", flow):
                with pytest.raises(members.MemberSlugError, match="not a regular file"):
                    members.retire_member_space(SLUG, member=MEMBER, record={})
            assert rules.is_dir()
        rules.rmdir()

    @pytest.mark.asyncio
    @pytest.mark.skipif(not Path("/proc/self/fd").is_dir(), reason="fd-to-path via /proc")
    async def test_the_archive_is_durable_before_the_marker_is_cleared(
        self, agents_dir: Path, store_app: Path
    ):
        """Crash consistency. The marker is the fire's ONLY resume anchor: if its
        unlink reached disk ahead of the archive mutations (renames and unlinks
        are directory entries, durable only once their directory is synced;
        ``fired.json``'s bytes only once the file is), a power loss after the
        200 would leave the row gone, the space back at its live name or the
        record missing, and nothing to resume from. So by the time the marker
        is cleared every directory the retirement touched -- the members root
        and its archive root, the archive entry, the rules and bindings
        directories under trust/, the agents directory the private copy left,
        the pristine copies' directory -- and the record's own bytes have been
        fsync'd; the marker directory is synced after the unlink."""
        from kiro_crew.dashboard.handlers import members as handlers

        synced: list[str] = []
        real_fsync = os.fsync

        def _recording_fsync(fd):
            try:
                synced.append(os.readlink(f"/proc/self/fd/{fd}"))
            except OSError:
                synced.append(f"<fd {fd}>")
            return real_fsync(fd)

        at_clear: list[list[str]] = []
        real_clear = handlers._clear_fire_marker

        def _snapshot_then_clear(member):
            at_clear.append(list(synced))
            return real_clear(member)

        async with TestClient(TestServer(_app())) as client:
            await _hire_pager(client)
            space = _live_in(SLUG)
            assert space.is_dir()
            with (
                patch.object(os, "fsync", _recording_fsync),
                patch.object(handlers, "_clear_fire_marker", _snapshot_then_clear),
            ):
                resp = await client.post(f"/api/members/{MEMBER}/fire", json={})
                assert resp.status == 200, await resp.text()
        assert len(at_clear) == 1, "the marker is cleared exactly once"
        before = {str(Path(p_).resolve()) for p_ in at_clear[0]}
        retired = [p for p in members.retired_root().iterdir() if p.name.startswith(f"{SLUG}--")]
        assert len(retired) == 1
        entry = retired[0]
        want = {
            str(members.members_root().resolve()),
            str(members.retired_root().resolve()),
            str(entry.resolve()),
            str((entry / members.FIRED_RECORD_FILE).resolve()),
            str(members.retired_rules_root().resolve()),
            str(members.retired_rules_root().parent.resolve()),
            str(members.dm_binding_path(SLUG).parent.resolve()),
            str(member_templates.pristine_copy_path(MEMBER).parent.resolve()),
            str(agents_dir.resolve()),
        }
        missing = {w for w in want if w not in before}
        assert not missing, f"not synced before the marker was cleared: {sorted(missing)}"
        marker_dir = handlers._fire_marker_path(MEMBER).parent
        after = {str(Path(p_).resolve()) for p_ in synced}
        assert str(marker_dir.resolve()) in after, "the marker directory is synced after the unlink"
        assert str(marker_dir.resolve()) not in before, "…and not before it (nothing to sync yet)"

    @pytest.mark.asyncio
    async def test_a_marker_that_cannot_be_cleared_is_reported_not_swallowed(
        self, agents_dir: Path, store_app: Path
    ):
        """Every other step done, the marker's unlink fails: that is a fire still
        PENDING to every creation on the slug (``fire_pending``), so it is
        answered as resumable ``fire_incomplete``, never 200. The retry's only
        remaining work is the unlink, after which the slug is free again."""
        from kiro_crew.dashboard.handlers import members as handlers

        real_unlink = Path.unlink

        def _marker_stuck(self, *args, **kwargs):
            # Only the marker's own unlink fails; every other file goes.
            if self.parent.name == "member-fires":
                raise PermissionError("busy")
            return real_unlink(self, *args, **kwargs)

        async with TestClient(TestServer(_app())) as client:
            await _hire_pager(client)
            space = _live_in(SLUG)
            with patch.object(Path, "unlink", _marker_stuck):
                resp = await client.post(f"/api/members/{MEMBER}/fire", json={})
                assert resp.status == 500, await resp.text()
                body = await resp.json()
                assert body["code"] == "fire_incomplete"
            # Everything but the marker is done.
            assert MEMBER not in KiroCrewConfig.load().agents
            assert not space.exists()
            assert handlers._read_fire_marker(MEMBER) is not None
            # Still pending: a same-slug hire is refused...
            resp = await client.post("/api/members", json=_store_hire("Pager triage"))
            assert resp.status == 409 and (await resp.json())["code"] == "fire_pending"
            # ...until the fire is re-run and the marker goes.
            resp = await client.post(f"/api/members/{MEMBER}/fire", json={})
            assert resp.status == 200, await resp.text()
            assert handlers._read_fire_marker(MEMBER) is None
            resp = await client.post("/api/members", json=_store_hire("Pager triage"))
            assert resp.status == 200, await resp.text()

    def test_retire_refuses_a_planted_archive_root(self, agents_dir: Path, tmp_path: Path):
        """``members/.retired`` is agent-writable ground: a symlink planted at
        its name is refused, never followed -- the archive (and the protected
        rules beside it) must not land wherever the link points. Nothing is
        moved when the root is refused."""
        elsewhere = tmp_path / "elsewhere"
        elsewhere.mkdir()
        space = _live_in(SLUG)
        root = members.members_root()
        (root / members.RETIRED_DIR_NAME).symlink_to(elsewhere, target_is_directory=True)
        with pytest.raises(members.MemberSlugError):
            members.retired_root()
        with pytest.raises(members.MemberSlugError):
            members.retire_member_space(SLUG, member=MEMBER, record={})
        assert space.is_dir() and (space / "activity.jsonl").exists()
        assert not any(elsewhere.iterdir())
        assert members.read_member_rules(SLUG, MEMBER) == "Always page the on-call first."
        # A plain file at the name is refused the same way.
        (root / members.RETIRED_DIR_NAME).unlink()
        (root / members.RETIRED_DIR_NAME).write_text("x")
        with pytest.raises(members.MemberSlugError):
            members.retired_root()

    def test_retire_removes_only_a_binding_that_names_the_member(self, agents_dir: Path):
        """Slugification is lossy: a binding under this slug naming ANOTHER
        member is that member's thread, not this fire's to remove. A binding
        that does not read (tampered) attributes nothing and goes."""
        members.write_dm_binding(SLUG, member="pager-Triage", slot_key=f"member-{SLUG}")
        members.retire_member_space(SLUG, member=MEMBER, record={})
        assert members.read_dm_binding(SLUG)["member"] == "pager-Triage"
        members.write_dm_binding(SLUG, member=MEMBER, slot_key=f"member-{SLUG}")
        members.retire_member_space(SLUG, member=MEMBER, record={})
        assert members.read_dm_binding(SLUG) is None
        members.dm_binding_path(SLUG).parent.mkdir(parents=True, exist_ok=True)
        members.dm_binding_path(SLUG).write_text("not json")
        assert members.remove_dm_binding_of(SLUG, MEMBER) is True
        assert not members.dm_binding_path(SLUG).exists()

    @pytest.mark.skipif(not members._DIR_FD_SUPPORTED, reason="dir_fd unlink is POSIX")
    def test_a_link_planted_at_the_binding_name_never_removes_another_members_binding(
        self, agents_dir: Path
    ):
        """``dm_binding_path`` resolves, so a planted ``<fired>.json -> <victim>.json``
        would make the resolved name the VICTIM's file. The fire unlinks the
        literal ``<slug>.json`` relative to the pinned bindings directory after an
        ``lstat``: the planted link goes (it attributes nothing), the victim's
        binding stays untouched."""
        victim_slug = "victim-member"
        members.write_dm_binding(
            victim_slug, member="Victim-Member", slot_key=f"member-{victim_slug}"
        )
        bindings = members.dm_binding_path(victim_slug).parent
        planted = bindings / f"{SLUG}.json"
        planted.symlink_to(bindings / f"{victim_slug}.json")
        # Resolved, the fired slug's path IS the victim's file -- the trap.
        assert members.dm_binding_path(SLUG) == members.dm_binding_path(victim_slug)
        assert members.remove_dm_binding_of(SLUG, MEMBER) is True
        assert not planted.is_symlink() and not planted.exists()
        assert members.read_dm_binding(victim_slug)["member"] == "Victim-Member"
        # A regular binding naming another member is still not ours to remove;
        # our own is.
        members.write_dm_binding(SLUG, member="pager-Triage", slot_key=f"member-{SLUG}")
        assert members.remove_dm_binding_of(SLUG, MEMBER) is False
        members.write_dm_binding(SLUG, member=MEMBER, slot_key=f"member-{SLUG}")
        assert members.remove_dm_binding_of(SLUG, MEMBER) is True
        assert members.read_dm_binding(SLUG) is None

    def test_an_obstacle_at_the_binding_name_refuses_the_fire(self, agents_dir: Path):
        """Only ABSENCE is a quiet ``False``. A directory (or any non-regular,
        non-link entry) planted at ``trust/member-bindings/<slug>.json`` is an
        obstacle the next member on the slug cannot bind its thread over
        (``write_dm_binding`` renames onto it and fails on every attempt): both
        flows raise, so the fire keeps its marker and says so instead of
        clearing it. A planted link is removed as a link in both flows."""
        members.write_dm_binding(SLUG, member=MEMBER, slot_key=f"member-{SLUG}")
        leaf = members.dm_binding_path(SLUG)
        leaf.unlink()
        for flow in (True, False):
            leaf.mkdir()
            with patch.object(members, "_DIR_FD_SUPPORTED", flow):
                with pytest.raises(members.MemberSlugError, match="not a regular file"):
                    members.remove_dm_binding_of(SLUG, MEMBER)
                with pytest.raises(members.MemberSlugError, match="not a regular file"):
                    members.retire_member_space(SLUG, member=MEMBER, record={})
            assert leaf.is_dir()
            leaf.rmdir()
            # Absent: quietly nothing to remove.
            with patch.object(members, "_DIR_FD_SUPPORTED", flow):
                assert members.remove_dm_binding_of(SLUG, MEMBER) is False
            # A link: removed as a link, its target untouched.
            target = leaf.parent / "elsewhere.json"
            target.write_text(json.dumps({"member": "someone"}))
            leaf.symlink_to(target)
            with patch.object(members, "_DIR_FD_SUPPORTED", flow):
                assert members.remove_dm_binding_of(SLUG, MEMBER) is True
            assert not leaf.is_symlink() and not leaf.exists()
            assert json.loads(target.read_text())["member"] == "someone"
            target.unlink()

    @pytest.mark.asyncio
    async def test_a_member_sharing_its_slug_with_a_live_colleague_is_not_fired(
        self, agents_dir: Path, store_app: Path
    ):
        """``members/<slug>/``, the rules and the binding are keyed by the lossy
        slug, so retiring them would take the colleague's along. The fire is
        refused before anything is closed or removed; the colleague renames
        (or is fired) first."""
        from kiro_crew.config.loader import KiroCrewAgentConfig, update_config_locked

        async with TestClient(TestServer(_app())) as client:
            await _hire_pager(client)
            _live_in(SLUG)

            def add_twin(doc):
                twin = KiroCrewAgentConfig(kiro_agent="kirocrew", memory_store="default")
                from dataclasses import asdict

                doc["agents"]["pager-Triage"] = asdict(twin)
                return doc

            update_config_locked(mutate=add_twin)
            resp = await client.post(f"/api/members/{MEMBER}/fire", json={})
            assert resp.status == 409, await resp.text()
            body = await resp.json()
            assert body["code"] == "slug_collision" and "pager-Triage" in body["error"]
            # The repair is named by the labels the navigation shows, and the
            # page gets the same fact structured, to say it in the user's language.
            assert "Your Crewmates" in body["error"] and "Agent Capabilities" in body["error"]
            assert body["collision"] == {"member": "pager-Triage", "slug": SLUG}
        assert MEMBER in KiroCrewConfig.load().agents
        assert members.member_dir(SLUG).is_dir()
        assert members.read_dm_binding(SLUG)["member"] == MEMBER

    @pytest.mark.asyncio
    async def test_a_replacement_member_cannot_purge_its_predecessors_thread(
        self, agents_dir: Path, store_app: Path
    ):
        """Same id, new generation: a member deleted and hired again under the
        same name inherits nothing of its predecessor's thread. The
        predecessor's binding names the SAME member id but the OLD canonical
        slot (its private-store generation), so the fire must not take it as
        the current member's -- a purge would delete the predecessor's
        transcript. The binding is accepted only when it points at the slot the
        thread open would bind for the member as it is NOW."""
        from kiro_crew.dashboard.handlers import api_kirocrew_agent_delete

        purged: list[str] = []
        state = MagicMock(
            sessions=None, _slots={}, conversation_log=_Log(f"dashboard:member-{SLUG}")
        )
        app = _app(state)
        app.router.add_delete("/api/agents/{name}", api_kirocrew_agent_delete)
        with (
            patch(
                "kiro_crew.dashboard.handlers.members._purge_thread_history",
                side_effect=lambda st, key: purged.append(key) or "",
            ),
            patch("kiro_crew.dashboard.chat_handlers.close_slot") as close,
        ):
            async with TestClient(TestServer(app)) as client:
                await _hire_pager(client)
                _live_in(SLUG)
                old_slot, old_store, old_hist = _canonical(SLUG)
                assert old_store, "the hire provisions a private generation"
                # Delete (not fire) leaves the binding behind; hire the same name again.
                resp = await client.delete(f"/api/agents/{MEMBER}")
                assert resp.status == 200, await resp.text()
                assert members.read_dm_binding(SLUG)["slot_key"] == old_slot
                await _hire_pager(client)
                new_slot, new_store, _new_hist = _canonical(SLUG)
                assert new_store != old_store and new_slot != old_slot
                resp = await client.post(f"/api/members/{MEMBER}/fire", json={"purge": True})
                assert resp.status == 200, await resp.text()
                body = await resp.json()
                close.assert_not_called()
        # The predecessor's thread was neither closed as ours nor purged.
        assert body["thread"] == {"state": "none"}
        assert purged == []

    @pytest.mark.asyncio
    async def test_a_binding_naming_a_ghost_of_the_slug_is_not_this_members_thread(
        self, agents_dir: Path, store_app: Path
    ):
        """A binding left by a deleted same-slug member names nobody live: the
        fire closes no slot for it and reports no thread, and the stale binding
        goes with the space."""
        slots = {f"member-{SLUG}": MagicMock()}
        state = MagicMock(
            sessions=None, _slots=slots, conversation_log=_Log(f"dashboard:member-{SLUG}")
        )
        async with TestClient(TestServer(_app(state))) as client:
            await _hire_pager(client)
            _live_in(SLUG)
            members.write_dm_binding(SLUG, member="pager-Triage", slot_key=f"member-{SLUG}")
            with patch("kiro_crew.dashboard.chat_handlers.close_slot") as close:
                resp = await client.post(f"/api/members/{MEMBER}/fire", json={})
                assert resp.status == 200, await resp.text()
                close.assert_not_called()
                body = await resp.json()
        assert body["thread"] == {"state": "none"}
        # The ghost's binding is not this member's to remove; it stays.
        assert members.read_dm_binding(SLUG)["member"] == "pager-Triage"

    @pytest.mark.asyncio
    async def test_a_fire_interrupted_after_the_row_went_is_resumed_by_the_next_fire(
        self, agents_dir: Path, store_app: Path
    ):
        """The intent is recorded before the row goes. A cleanup step that
        fails afterwards answers 500 ``fire_incomplete`` (never a silent
        success) and leaves the marker; firing the same member again finishes
        the archive, and the marker is cleared only then."""
        from kiro_crew.dashboard.handlers import members as handlers

        async with TestClient(TestServer(_app())) as client:
            await _hire_pager(client)
            space = _live_in(SLUG)
            with patch(
                "kiro_crew.dashboard.handlers.members.members_mod.retire_member_space",
                side_effect=OSError("disk full"),
            ):
                resp = await client.post(f"/api/members/{MEMBER}/fire", json={})
                assert resp.status == 500, await resp.text()
                body = await resp.json()
                assert body["code"] == "fire_incomplete"
            # The row is gone, the files are not, the intent is on disk.
            assert MEMBER not in KiroCrewConfig.load().agents
            assert space.is_dir()
            marker = handlers._read_fire_marker(MEMBER)
            assert marker["slug"] == SLUG and marker["record"]["display_name"] == "Pager triage"
            # The retry resumes from the marker instead of answering 404.
            resp = await client.post(f"/api/members/{MEMBER}/fire", json={})
            assert resp.status == 200, await resp.text()
            assert (await resp.json())["lived_state"] == "archived"
        assert not space.exists()
        assert handlers._read_fire_marker(MEMBER) is None
        retired = [p for p in members.retired_root().iterdir() if p.name.startswith(f"{SLUG}--")]
        assert len(retired) == 1
        assert (
            json.loads((retired[0] / members.FIRED_RECORD_FILE).read_text())["display_name"]
            == "Pager triage"
        )
        # And with nothing pending, an unknown member is still a 404.
        async with TestClient(TestServer(_app())) as client:
            resp = await client.post("/api/members/nobody/fire", json={})
            assert resp.status == 404

    @pytest.mark.asyncio
    async def test_the_fire_holds_the_thread_lock_the_open_takes(
        self, agents_dir: Path, store_app: Path
    ):
        """Thread open and fire serialize on one lock, so an open cannot
        re-bind the slug between the fire's binding read and its retirement."""
        from kiro_crew.dashboard.handlers import members as handlers

        observed: list[bool] = []
        real = members.retire_member_space

        def observe(*args, **kwargs):
            observed.append(handlers._dm_thread_lock.locked())
            return real(*args, **kwargs)

        async with TestClient(TestServer(_app())) as client:
            await _hire_pager(client)
            with patch(
                "kiro_crew.dashboard.handlers.members.members_mod.retire_member_space", observe
            ):
                resp = await client.post(f"/api/members/{MEMBER}/fire", json={})
                assert resp.status == 200, await resp.text()
        assert observed == [True]
        assert not handlers._dm_thread_lock.locked()

    @pytest.mark.asyncio
    async def test_a_hire_is_refused_while_a_fire_of_that_id_is_pending(
        self, agents_dir: Path, store_app: Path
    ):
        """An interrupted fire's marker means lived state under this id is still
        on disk waiting to be archived; a new member would inherit it (or lose
        its own files to the resumed cleanup). The hire says so; the resumed
        fire clears the way."""
        async with TestClient(TestServer(_app())) as client:
            await _hire_pager(client)
            _live_in(SLUG)
            with patch(
                "kiro_crew.dashboard.handlers.members.members_mod.retire_member_space",
                side_effect=OSError("disk full"),
            ):
                resp = await client.post(f"/api/members/{MEMBER}/fire", json={})
                assert resp.status == 500
            resp = await client.post("/api/members", json=_store_hire("Pager triage"))
            assert resp.status == 409, await resp.text()
            assert (await resp.json())["code"] == "fire_pending"
            assert MEMBER not in KiroCrewConfig.load().agents
            resp = await client.post(f"/api/members/{MEMBER}/fire", json={})
            assert resp.status == 200, await resp.text()
            await _hire_pager(client)
        assert members.read_activity(SLUG) == []

    @pytest.mark.asyncio
    async def test_the_cleanup_runs_in_the_same_config_lock_hold_as_the_row_removal(
        self, agents_dir: Path, store_app: Path
    ):
        """Steps 3 and 4 run with the config lock still held, so a hire of the
        same id (which takes the lock to publish) cannot interleave and have
        this fire remove the copy, pristine copy or space it just received."""
        observed: list[bool] = []
        real = members.retire_member_space

        def observe(*args, **kwargs):
            observed.append(_hire._agents._get_config_lock().locked())
            return real(*args, **kwargs)

        async with TestClient(TestServer(_app())) as client:
            await _hire_pager(client)
            with patch(
                "kiro_crew.dashboard.handlers.members.members_mod.retire_member_space", observe
            ):
                resp = await client.post(f"/api/members/{MEMBER}/fire", json={})
                assert resp.status == 200, await resp.text()
        assert observed == [True]

    @pytest.mark.asyncio
    async def test_a_resumed_fire_never_touches_a_member_that_exists_again(
        self, agents_dir: Path, store_app: Path
    ):
        """A row under the id while a marker is pending (a hand-edited config:
        the hire refuses) is somebody else's member; the resume refuses, leaves
        the marker and touches none of the new member's files."""
        from kiro_crew.dashboard.handlers import members as handlers

        async with TestClient(TestServer(_app())) as client:
            await _hire_pager(client)
            space = _live_in(SLUG)
            intent = {
                "member": MEMBER,
                "slug": SLUG,
                "copy_name": MEMBER,
                "purge": False,
                "record": {"display_name": "Pager triage"},
            }
            handlers._write_fire_marker(MEMBER, intent)
            state = client.server.app["state"]
            resp = await handlers._finish_fire(MagicMock(), state, MEMBER, intent, False)
            assert resp.status == 409
            assert json.loads(resp.text)["code"] == "member_changed"
        assert MEMBER in KiroCrewConfig.load().agents
        assert (agents_dir / f"{MEMBER}.json").exists()
        assert member_templates.pristine_copy_path(MEMBER).exists()
        assert space.is_dir() and members.read_dm_binding(SLUG)["member"] == MEMBER
        assert handlers._read_fire_marker(MEMBER) is not None

    def test_a_directory_swapped_for_a_link_mid_retire_is_not_followed(
        self, agents_dir: Path, tmp_path: Path
    ):
        """Every step of the move is relative to a pinned descriptor of the
        members root and of the archive root: an entry that reads as a link at
        rename time is unlinked, never renamed; a plain path check followed by a
        path rename would act on whatever the swapped-in link points at."""
        if not members._DIR_FD_SUPPORTED:
            pytest.skip("dir_fd-relative rename is POSIX")
        elsewhere = tmp_path / "elsewhere"
        elsewhere.mkdir()
        (elsewhere / "secret.txt").write_text("x")
        space = _live_in(SLUG)
        assert space.is_dir()
        real_lstat = members.os.lstat

        def swap_then_lstat(*args, **kwargs):
            # The swap lands between the caller's view of a directory and the
            # pinned lstat: the pinned flow sees the link and unlinks it.
            if args and args[0] == SLUG and not getattr(swap_then_lstat, "done", False):
                swap_then_lstat.done = True
                import shutil

                shutil.rmtree(space)
                space.symlink_to(elsewhere, target_is_directory=True)
            return real_lstat(*args, **kwargs)

        with patch.object(members.os, "lstat", swap_then_lstat):
            archived = members.retire_member_space(SLUG, member=MEMBER, record={})
        assert not space.exists() and not space.is_symlink()
        assert (elsewhere / "secret.txt").exists()
        # The rules (archived from trust/) still produced an archive dir with the record.
        assert archived is not None and (archived / members.FIRED_RECORD_FILE).is_file()
        assert not any(p.name == "secret.txt" for p in archived.iterdir())

    @pytest.mark.parametrize("purge", [False, True])
    def test_a_colleagues_directory_swapped_onto_the_slug_mid_retire_is_neither_archived_nor_purged(
        self, agents_dir: Path, purge: bool
    ):
        """A rename of a COLLEAGUE's space onto this slug is one ``mv`` in an
        agent-writable tree, and it can land between the pinned ``lstat`` and the
        step that acts. Every acting step checks it acted on the very directory
        the lstat saw (``st_dev``/``st_ino``): the purge stops before a byte is
        removed, the archive renames the swapped directory back and refuses, and
        the fire keeps its marker. Nothing of the colleague's is lost or
        misattributed."""
        if not members._DIR_FD_SUPPORTED:
            pytest.skip("dir_fd-relative rename is POSIX")
        space = _live_in(SLUG)
        colleague = members.members_root() / "colleague"
        colleague.mkdir()
        (colleague / "notes.md").write_text("the colleague's")
        real_lstat = members.os.lstat

        def swap_then_lstat(*args, **kwargs):
            # The lstat sees the member's own directory; then the swap lands.
            result = real_lstat(*args, **kwargs)
            if args and args[0] == SLUG and not getattr(swap_then_lstat, "done", False):
                swap_then_lstat.done = True
                import shutil

                shutil.rmtree(space)
                colleague.rename(space)
            return result

        with patch.object(members.os, "lstat", swap_then_lstat):
            with pytest.raises(members.MemberSlugError, match="changed while"):
                members.retire_member_space(SLUG, member=MEMBER, record={}, purge=purge)
        # The swapped-in directory is back at the slug, intact, and nothing of it
        # was archived.
        assert (space / "notes.md").read_text() == "the colleague's"
        retired = members.members_root() / members.RETIRED_DIR_NAME
        assert not retired.exists() or not any(
            (d / "notes.md").exists() for d in retired.iterdir() if d.is_dir()
        )

    def test_a_link_planted_at_the_members_root_is_refused_never_followed(
        self, agents_dir: Path, tmp_path: Path
    ):
        """The root itself is agent-writable ground's parent: an agent that
        replaces ``members`` with a symlink to an outside tree holding a
        ``<slug>`` directory would have a purge delete, and an archive land in,
        that tree. The root is opened as the literal child of the data home
        with ``O_NOFOLLOW``; a link there refuses the fire before anything moves."""
        if not members._DIR_FD_SUPPORTED:
            pytest.skip("dir_fd-relative open is POSIX")
        import shutil

        elsewhere = tmp_path / "elsewhere"
        (elsewhere / SLUG).mkdir(parents=True)
        (elsewhere / SLUG / "secret.txt").write_text("x")
        root = members.members_root()
        if root.exists():
            shutil.rmtree(root)
        root.symlink_to(elsewhere, target_is_directory=True)
        with pytest.raises(members.MemberSlugError):
            members.retire_member_space(SLUG, member=MEMBER, record={}, purge=True)
        with pytest.raises(members.MemberSlugError):
            members.retire_member_space(SLUG, member=MEMBER, record={})
        assert (elsewhere / SLUG / "secret.txt").exists()
        assert not (elsewhere / members.RETIRED_DIR_NAME).exists()
        root.unlink()

    @pytest.mark.asyncio
    async def test_an_archive_interrupted_after_the_rename_resumes_into_the_same_entry(
        self, agents_dir: Path, store_app: Path
    ):
        """The archive entry's name is recorded in the marker BEFORE the rename.
        An attempt that dies between the rename and ``fired.json`` (ENOSPC) leaves
        the marker naming that entry; the resume writes the record into it, so
        the archive is recorded exactly once -- never a second entry beside an
        unrecorded first, never a cleared marker over an unrecorded archive."""
        from kiro_crew.dashboard.handlers import members as handlers

        async with TestClient(TestServer(_app())) as client:
            await _hire_pager(client)
            space = _live_in(SLUG)
            target = (
                "kiro_crew.members._write_fired_record_at"
                if members._DIR_FD_SUPPORTED
                else "kiro_crew.members._write_fired_record"
            )
            with patch(target, side_effect=OSError("no space left on device")):
                resp = await client.post(f"/api/members/{MEMBER}/fire", json={})
                assert resp.status == 500, await resp.text()
                assert (await resp.json())["code"] == "fire_incomplete"
            marker = handlers._read_fire_marker(MEMBER)
            name = marker["archive_name"]
            assert name.startswith(f"{SLUG}--")
            # Renamed, unrecorded: the entry exists under the recorded name.
            entry = members.retired_root() / name
            assert entry.is_dir() and not (entry / members.FIRED_RECORD_FILE).exists()
            assert not space.exists()
            resp = await client.post(f"/api/members/{MEMBER}/fire", json={})
            assert resp.status == 200, await resp.text()
            assert (await resp.json())["lived_state"] == "archived"
        assert handlers._read_fire_marker(MEMBER) is None
        retired = [p for p in members.retired_root().iterdir() if p.name.startswith(f"{SLUG}--")]
        assert [p.name for p in retired] == [name]
        assert json.loads((entry / members.FIRED_RECORD_FILE).read_text())["member"] == MEMBER

    @pytest.mark.asyncio
    async def test_a_purge_asked_on_resume_removes_the_archived_entry(
        self, agents_dir: Path, store_app: Path
    ):
        """An archive that was renamed and then interrupted, resumed with
        ``purge: true``, removes THAT entry -- the recorded one -- not a fresh
        name beside it."""
        from kiro_crew.dashboard.handlers import members as handlers

        async with TestClient(TestServer(_app())) as client:
            await _hire_pager(client)
            _live_in(SLUG)
            target = (
                "kiro_crew.members._write_fired_record_at"
                if members._DIR_FD_SUPPORTED
                else "kiro_crew.members._write_fired_record"
            )
            with patch(target, side_effect=OSError("no space left on device")):
                assert (await client.post(f"/api/members/{MEMBER}/fire", json={})).status == 500
            name = handlers._read_fire_marker(MEMBER)["archive_name"]
            assert (members.retired_root() / name).is_dir()
            resp = await client.post(f"/api/members/{MEMBER}/fire", json={"purge": True})
            assert resp.status == 200, await resp.text()
            assert (await resp.json())["lived_state"] == "purged"
        assert not (members.retired_root() / name).exists()
        assert not any(p.name.startswith(f"{SLUG}--") for p in members.retired_root().iterdir())

    @pytest.mark.asyncio
    async def test_a_resume_after_the_rules_moved_sees_the_archived_rules(
        self, agents_dir: Path, store_app: Path
    ):
        """The rules step runs after the directory move and before the binding
        removal; a fire interrupted right after it has the rules already under
        ``trust/member-rules/.retired/<name>.json``. A resume finds nothing at
        the live name and must not read that as "no rules": one that purges
        removes the archived entry (a purge that reported the rules gone while
        they sat in the trust subtree was the finding), one that archives counts
        them as retired. Both flows."""
        from kiro_crew.dashboard.handlers import members as handlers

        for flow, purge in ((True, True), (False, True), (True, False)):
            with patch.object(members, "_DIR_FD_SUPPORTED", flow):
                async with TestClient(TestServer(_app())) as client:
                    await _hire_pager(client)
                    _live_in(SLUG)
                    with patch(
                        "kiro_crew.members.remove_dm_binding_of",
                        side_effect=OSError("binding directory vanished"),
                    ):
                        resp = await client.post(f"/api/members/{MEMBER}/fire", json={})
                        assert resp.status == 500, await resp.text()
                    name = handlers._read_fire_marker(MEMBER)["archive_name"]
                    archived_rules = members.retired_rules_root() / f"{name}.json"
                    assert archived_rules.is_file(), "the rules moved before the interruption"
                    assert not members.member_rules_path(SLUG).exists()
                    resp = await client.post(f"/api/members/{MEMBER}/fire", json={"purge": purge})
                    assert resp.status == 200, await resp.text()
                    body = await resp.json()
                if purge:
                    assert body["lived_state"] == "purged"
                    assert not archived_rules.exists(), "a purge removes the archived rules"
                else:
                    assert body["lived_state"] == "archived"
                    assert archived_rules.is_file(), "an archive keeps the archived rules"
                    assert (members.retired_root() / name / "fired.json").is_file()
                assert handlers._read_fire_marker(MEMBER) is None
                assert MEMBER not in KiroCrewConfig.load().agents
                # Clean slate for the next flow.
                for p in members.retired_rules_root().glob(f"{SLUG}--*.json"):
                    p.unlink()
                shutil.rmtree(members.retired_root(), ignore_errors=True)

    @pytest.mark.asyncio
    async def test_an_unreadable_fire_marker_fails_closed(self, agents_dir: Path, store_app: Path):
        """A marker that cannot be read is not "no marker": lived state under
        that id may still be waiting. The hire refuses the id (409
        ``fire_pending``) and a fire of the vanished member says the record must
        be fixed (500 ``fire_marker_unreadable``) instead of answering 404."""
        from kiro_crew.dashboard.handlers import members as handlers

        path = handlers._fire_marker_path(MEMBER)
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text("{not json", encoding="utf-8")
        async with TestClient(TestServer(_app())) as client:
            resp = await client.post("/api/members", json=_store_hire("Pager triage"))
            assert resp.status == 409, await resp.text()
            assert (await resp.json())["code"] == "fire_pending"
            assert MEMBER not in KiroCrewConfig.load().agents
            resp = await client.post(f"/api/members/{MEMBER}/fire", json={})
            assert resp.status == 500, await resp.text()
            assert (await resp.json())["code"] == "fire_marker_unreadable"
            # A marker that reads but names no member is the same.
            path.write_text(json.dumps({"purge": True}), encoding="utf-8")
            resp = await client.post("/api/members", json=_store_hire("Pager triage"))
            assert resp.status == 409
            assert (await resp.json())["code"] == "fire_pending"
            # And a marker DIRECTORY that cannot be listed at all (permissions,
            # not a directory) is not an empty one: the same 409, never a 500.
            with patch.object(
                Path, "iterdir", side_effect=PermissionError("member-fires: permission denied")
            ):
                resp = await client.post("/api/members", json=_store_hire("Pager triage"))
                assert resp.status == 409, await resp.text()
                body = await resp.json()
                assert body["code"] == "fire_pending" and "could not be listed" in body["error"]
        path.unlink()

    def test_the_fire_marker_is_written_through_the_no_follow_path(
        self, agents_dir: Path, tmp_path: Path
    ):
        """A link planted at the marker's own name is refused, never written
        through: the intent lands via ``write_file_pinned`` (pinned parent,
        ``O_NOFOLLOW`` at the name, no by-name temp file), so a same-user
        process cannot aim the gateway's write at a file of its choosing."""
        from kiro_crew.dashboard.handlers import members as handlers

        victim = tmp_path / "victim.json"
        victim.write_text("keep me", encoding="utf-8")
        path = handlers._fire_marker_path(MEMBER)
        path.parent.mkdir(parents=True, exist_ok=True)
        path.symlink_to(victim)
        with pytest.raises(OSError):
            handlers._write_fire_marker(MEMBER, {"slug": SLUG})
        assert victim.read_text(encoding="utf-8") == "keep me"
        # Nor is a planted link READ through: an unreadable marker fails closed.
        with pytest.raises(handlers.FireMarkerUnreadable):
            handlers._read_fire_marker(MEMBER)
        # A link INSIDE the root (resolving to a sibling) passes containment and
        # is refused by the pinned write, never followed.
        path.unlink()
        sibling = path.parent / "sibling.json"
        sibling.write_text("keep me too", encoding="utf-8")
        path.symlink_to(sibling)
        with pytest.raises(OSError):
            handlers._write_fire_marker(MEMBER, {"slug": SLUG})
        assert sibling.read_text(encoding="utf-8") == "keep me too"
        sibling.unlink()
        assert not list(path.parent.glob("*.tmp"))
        path.unlink()
        handlers._write_fire_marker(MEMBER, {"slug": SLUG})
        assert handlers._read_fire_marker(MEMBER) == {"slug": SLUG}
        path.unlink()

    @pytest.mark.asyncio
    async def test_a_replacement_landing_inside_the_delete_write_is_not_fired(
        self, agents_dir: Path, store_app: Path
    ):
        """The identity check runs again INSIDE the delete's cross-process
        locked mutation (``expect``): a row another process replaced after the
        locked re-read but before the write is not the fired member, and its
        store is not retired."""
        from kiro_crew.dashboard.handlers import agents as agents_handlers

        real = agents_handlers._delete_crew_record

        async def swap_then_delete(request, name, **kwargs):
            from kiro_crew.config.loader import update_config_locked

            def mutate(doc):
                doc["agents"][name]["memory_store"] = "member-pager-triage-replacement"
                return doc

            update_config_locked(mutate=mutate)
            return await real(request, name, **kwargs)

        async with TestClient(TestServer(_app())) as client:
            await _hire_pager(client)
            with patch.object(agents_handlers, "_delete_crew_record", swap_then_delete):
                resp = await client.post(f"/api/members/{MEMBER}/fire", json={})
                assert resp.status == 409, await resp.text()
                assert (await resp.json())["code"] == "member_changed"
        row = KiroCrewConfig.load().agents[MEMBER]
        assert row.memory_store == "member-pager-triage-replacement"
        assert (agents_dir / f"{MEMBER}.json").exists()

    @pytest.mark.asyncio
    async def test_a_same_slug_member_published_inside_the_delete_write_is_not_fired(
        self, agents_dir: Path, store_app: Path
    ):
        """The slug check ran against a pre-lock read. A create in another
        process can publish a same-slug row between that read and the delete's
        write (its admit refuses only a PENDING fire, and this fire's marker
        can land after that admit ran): the member SPACE this fire archives or
        purges is then shared, so the delete re-checks the other rows INSIDE
        its cross-process locked mutation (``expect_others``) and refuses --
        the row stays, the marker is cleared, and the newcomer's lived state is
        untouched."""
        from kiro_crew.dashboard.handlers import agents as agents_handlers
        from kiro_crew.dashboard.handlers import members as handlers

        real = agents_handlers._delete_crew_record

        async def publish_a_slugmate_then_delete(request, name, **kwargs):
            from kiro_crew.config.loader import update_config_locked

            def mutate(doc):
                # Another id on the same slug ("pager-triage"), as a plain
                # create in another process would publish it.
                doc["agents"]["pager-Triage"] = {"kiro_agent": "kirocrew"}
                return doc

            update_config_locked(mutate=mutate)
            return await real(request, name, **kwargs)

        async with TestClient(TestServer(_app())) as client:
            await _hire_pager(client)
            space = _live_in(SLUG)
            with patch.object(
                agents_handlers, "_delete_crew_record", publish_a_slugmate_then_delete
            ):
                resp = await client.post(f"/api/members/{MEMBER}/fire", json={"purge": True})
                assert resp.status == 409, await resp.text()
                body = await resp.json()
                assert body["code"] == "slug_collision"
                assert "pager-Triage" in body["error"]
        cfg = KiroCrewConfig.load()
        assert MEMBER in cfg.agents and "pager-Triage" in cfg.agents
        assert (agents_dir / f"{MEMBER}.json").exists()
        assert space.is_dir()
        assert handlers._read_fire_marker(MEMBER) is None
        assert not any(p.name.startswith(f"{SLUG}--") for p in members.retired_root().iterdir())

    @pytest.mark.asyncio
    async def test_a_recorded_purge_is_a_boolean_not_a_truthy_string(
        self, agents_dir: Path, store_app: Path
    ):
        """The marker is a file. A resume reads ``purge`` from it, and a
        hand-edited ``"false"`` is not a purge the user asked for: only
        ``true`` removes the lived state."""
        from kiro_crew.dashboard.handlers import members as handlers

        async with TestClient(TestServer(_app())) as client:
            await _hire_pager(client)
            space = _live_in(SLUG)
            with patch(
                "kiro_crew.dashboard.handlers.members.members_mod.retire_member_space",
                side_effect=OSError("disk full"),
            ):
                assert (await client.post(f"/api/members/{MEMBER}/fire", json={})).status == 500
            marker = handlers._read_fire_marker(MEMBER)
            handlers._write_fire_marker(MEMBER, dict(marker, purge="false"))
            resp = await client.post(f"/api/members/{MEMBER}/fire", json={})
            assert resp.status == 200, await resp.text()
            assert (await resp.json())["lived_state"] == "archived"
        assert not space.exists()
        assert any(p.name.startswith(f"{SLUG}--") for p in members.retired_root().iterdir())

    @pytest.mark.asyncio
    async def test_the_creates_pre_lock_admission_runs_off_the_event_loop(
        self, agents_dir: Path, store_app: Path
    ):
        """The admit hook scans the pending-fire markers on disk; the create
        calls it before its locked write, on the loop inside the in-process
        config lock. It runs on a worker thread there."""
        import threading

        from kiro_crew.dashboard.handlers import members as handlers

        loop_thread = threading.get_ident()
        seen: list[int] = []
        real = handlers._pending_fires_for

        def _record(candidate):
            seen.append(threading.get_ident())
            return real(candidate)

        async with TestClient(TestServer(_app())) as client:
            with patch.object(handlers, "_pending_fires_for", side_effect=_record):
                resp = await client.post("/api/members", json=_store_hire("Pager triage"))
                assert resp.status == 200, await resp.text()
        assert seen and all(t != loop_thread for t in seen)

    @pytest.mark.asyncio
    async def test_a_pending_fire_blocks_every_creation_path_on_its_slug(
        self, agents_dir: Path, store_app: Path
    ):
        """Lived state is slug-keyed, so a pending fire of ``Pager-triage``
        blocks a hire of ``pager-triage`` (another id, same slug) and the plain
        ``POST /api/agents`` create too -- neither may become the way a
        replacement inherits the interrupted fire's rules, activity or thread."""
        from kiro_crew.dashboard.handlers import api_kirocrew_agents_create
        from kiro_crew.dashboard.handlers import members as handlers

        app = _app()
        app.router.add_post("/api/agents", api_kirocrew_agents_create)
        async with TestClient(TestServer(app)) as client:
            await _hire_pager(client)
            with patch(
                "kiro_crew.dashboard.handlers.members.members_mod.retire_member_space",
                side_effect=OSError("disk full"),
            ):
                resp = await client.post(f"/api/members/{MEMBER}/fire", json={})
                assert resp.status == 500 and (await resp.json())["code"] == "fire_incomplete"
            assert handlers._read_fire_marker(MEMBER) is not None
            # Same slug, different id, through the hire.
            resp = await client.post("/api/members", json=_hire._store_hire("pager triage"))
            assert resp.status == 409, await resp.text()
            assert (await resp.json())["code"] == "fire_pending"
            # And through the plain create.
            resp = await client.post(
                "/api/agents", json={"name": "pager-triage", "kiro_agent": "kirocrew"}
            )
            assert resp.status == 409, await resp.text()
            assert (await resp.json())["code"] == "fire_pending"
            # An unrelated slug is untouched.
            resp = await client.post(
                "/api/agents", json={"name": "Scribe", "kiro_agent": "kirocrew"}
            )
            assert resp.status == 200, await resp.text()
            # Finishing the fire frees the slug.
            resp = await client.post(f"/api/members/{MEMBER}/fire", json={})
            assert resp.status == 200, await resp.text()
            resp = await client.post(
                "/api/agents", json={"name": "pager-triage", "kiro_agent": "kirocrew"}
            )
            assert resp.status == 200, await resp.text()

    def test_a_space_recreated_beside_its_archive_refuses_the_resume(
        self, agents_dir: Path, tmp_path: Path
    ):
        """An earlier attempt renamed the space away; an agent recreated
        ``members/<slug>``. Skipping the source would clear the marker with
        live state still on disk, so the retire refuses (the marker stays)."""
        from kiro_crew.members import new_archive_name

        _live_in(SLUG)
        name = new_archive_name(SLUG)
        archived = members.retire_member_space(SLUG, member=MEMBER, record={}, archive_name=name)
        assert archived is not None and archived.name == name
        # The agent recreates the space.
        members.record_activity(MEMBER, "s2", "persistent", project="p")
        assert members.member_dir(SLUG).is_dir()
        with pytest.raises(members.MemberSlugError):
            members.retire_member_space(SLUG, member=MEMBER, record={}, archive_name=name)
        assert members.member_dir(SLUG).is_dir()  # nothing moved, nothing lost

    def test_the_record_is_not_written_over_a_planted_hard_link(
        self, agents_dir: Path, tmp_path: Path
    ):
        """A hard link planted at ``fired.json`` IS the linked file; a truncating
        open would destroy it. The record is opened without truncation, the
        descriptor checked for a plain single-link file, and refused otherwise."""
        from kiro_crew.members import new_archive_name

        victim = tmp_path / "other-members-file.json"
        victim.write_text("precious", encoding="utf-8")
        _live_in(SLUG)
        name = new_archive_name(SLUG)
        entry = members.retired_root() / name
        entry.mkdir(parents=True)
        os.link(victim, entry / members.FIRED_RECORD_FILE)
        with pytest.raises(members.MemberSlugError):
            members.retire_member_space(SLUG, member=MEMBER, record={}, archive_name=name)
        assert victim.read_text(encoding="utf-8") == "precious"

    def test_the_path_flow_never_follows_a_link_planted_at_the_record_name(
        self, agents_dir: Path, tmp_path: Path
    ):
        """Where ``dir_fd`` is unsupported (Windows) ``O_NOFOLLOW`` is 0, so an
        open by name would follow a symlink planted at ``fired.json`` and write
        the record into the file it names. The path flow never opens an
        existing leaf: a planted link is refused (like the pinned flow's hard
        link), its target untouched; a plain record is published by replace."""
        from kiro_crew.members import new_archive_name

        victim = tmp_path / "other-members-file.json"
        victim.write_text("precious", encoding="utf-8")
        # A resumed fire: the space is already archived under *name* and the
        # agent planted a link at the record's name inside the archive entry.
        name = new_archive_name(SLUG)
        entry = members.retired_root() / name
        entry.mkdir(parents=True)
        os.symlink(victim, entry / members.FIRED_RECORD_FILE)
        with patch.object(members, "_DIR_FD_SUPPORTED", False):
            with pytest.raises(members.MemberSlugError):
                members.retire_member_space(SLUG, member=MEMBER, record={}, archive_name=name)
            assert victim.read_text(encoding="utf-8") == "precious"
            assert (entry / members.FIRED_RECORD_FILE).is_symlink()
            # A hard link at the name is refused the same way.
            (entry / members.FIRED_RECORD_FILE).unlink()
            os.link(victim, entry / members.FIRED_RECORD_FILE)
            with pytest.raises(members.MemberSlugError):
                members.retire_member_space(SLUG, member=MEMBER, record={}, archive_name=name)
            assert victim.read_text(encoding="utf-8") == "precious"
            # With the obstacle gone the record lands, as a plain file, by replace.
            (entry / members.FIRED_RECORD_FILE).unlink()
            archived = members.retire_member_space(
                SLUG, member=MEMBER, record={"why": "done"}, archive_name=name
            )
            assert archived is not None
            leaf = archived / members.FIRED_RECORD_FILE
            assert leaf.is_file() and not leaf.is_symlink()
            assert json.loads(leaf.read_text(encoding="utf-8"))["why"] == "done"
            assert not [p for p in archived.iterdir() if p.name.startswith(".fired-")]
        assert victim.read_text(encoding="utf-8") == "precious"
