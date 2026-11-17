"""A crewmate's approval mode: trust by default, the user's own choice preserved.

The mode lives on the crew record (``agents.<name>.approval_mode``), which is
what the Crewmates-page profile card writes. ``""`` means the user never chose
one, and only then does the crewmate's thread open in ``trust``. Any stored
choice wins, and a grant already on the live slot is never overwritten.
"""

from __future__ import annotations

from unittest.mock import AsyncMock

import pytest
from aiohttp import web
from aiohttp.test_utils import TestClient, TestServer
from chat_test_helpers import _make_state
from test_members_dm_thread import CREW, _fake_config, _make_members_app

from kiro_crew.config.loader import KiroCrewAgentConfig, KiroCrewConfig
from kiro_crew.config.sections import coerce_member_approval_mode
from kiro_crew.members import member_slot_key


@pytest.fixture(autouse=True)
def _owner_caller(_floor_monkeypatch):
    _floor_monkeypatch.setattr(
        "kiro_crew.dashboard.handlers.source_providers.is_owner_dashboard_request",
        lambda request: True,
    )


def _config_with(mode: str):
    cfg = _fake_config([CREW])
    cfg.agents[CREW] = KiroCrewAgentConfig(kiro_agent="kirocrew", approval_mode=mode)
    return cfg


async def _open_thread(state, cfg, monkeypatch):
    monkeypatch.setattr("kiro_crew.dashboard.handlers.members.KiroCrewConfig.load", lambda: cfg)
    async with TestClient(TestServer(_make_members_app(state))) as client:
        resp = await client.post(f"/api/members/{CREW}/thread")
        assert resp.status == 200, await resp.text()
        return state._slots[(await resp.json())["slot_key"]]


def _policy_spy(state, monkeypatch):
    calls: list[tuple[str, str]] = []
    monkeypatch.setattr(
        state.sessions, "set_approval_policy", lambda key, policy: calls.append((key, policy))
    )
    return calls


class TestThreadOpenSeedsTheProfileMode:
    @pytest.mark.asyncio
    async def test_a_mate_with_no_choice_opens_in_trust(self, tmp_path, monkeypatch):
        state = _make_state(tmp_path)
        calls = _policy_spy(state, monkeypatch)
        slot = await _open_thread(state, _config_with(""), monkeypatch)
        assert slot._trust is True
        assert slot._trust_reads is False
        assert calls and calls[-1][1] == "auto"

    @pytest.mark.asyncio
    async def test_a_stored_normal_is_kept(self, tmp_path, monkeypatch):
        state = _make_state(tmp_path)
        calls = _policy_spy(state, monkeypatch)
        slot = await _open_thread(state, _config_with("normal"), monkeypatch)
        assert slot._trust is False
        assert slot._trust_reads is False
        assert calls == []

    @pytest.mark.asyncio
    async def test_a_stored_trust_reads_is_kept(self, tmp_path, monkeypatch):
        state = _make_state(tmp_path)
        _policy_spy(state, monkeypatch)
        slot = await _open_thread(state, _config_with("trust_reads"), monkeypatch)
        assert slot._trust is False
        assert slot._trust_reads is True

    @pytest.mark.asyncio
    async def test_a_grant_already_on_the_slot_is_not_overwritten(self, tmp_path, monkeypatch):
        state = _make_state(tmp_path)
        calls = _policy_spy(state, monkeypatch)
        live = state.get_or_create_slot(
            member_slot_key(CREW), agent=CREW, mode="member", workspace="default"
        )
        live._trust_reads = True
        slot = await _open_thread(state, _config_with(""), monkeypatch)
        assert slot is live
        assert slot._trust is False
        assert slot._trust_reads is True
        assert calls == []

    @pytest.mark.asyncio
    async def test_a_change_after_the_seed_survives_a_reopen(self, tmp_path, monkeypatch):
        """The seed runs once per live slot: the user turning trust off on the
        thread is not undone the next time the page opens it."""
        state = _make_state(tmp_path)
        _policy_spy(state, monkeypatch)
        cfg = _config_with("")
        slot = await _open_thread(state, cfg, monkeypatch)
        assert slot._trust is True
        slot._trust = False
        again = await _open_thread(state, cfg, monkeypatch)
        assert again is slot
        assert again._trust is False

    @pytest.mark.asyncio
    async def test_a_choice_saved_during_the_open_wins_over_the_entry_snapshot(
        self, tmp_path, monkeypatch
    ):
        """The handler loads the config at entry, then awaits. A Normal saved
        on the profile in that window must decide the seed, not the stale ""."""
        state = _make_state(tmp_path)
        _policy_spy(state, monkeypatch)
        # The entry read sees no choice; every later read sees the Normal the
        # profile saved meanwhile.
        reads: list[int] = []

        def load():
            reads.append(1)
            return _config_with("" if len(reads) == 1 else "normal")

        monkeypatch.setattr("kiro_crew.dashboard.handlers.members.KiroCrewConfig.load", load)
        async with TestClient(TestServer(_make_members_app(state))) as client:
            resp = await client.post(f"/api/members/{CREW}/thread")
            assert resp.status == 200, await resp.text()
            slot = state._slots[(await resp.json())["slot_key"]]
        assert slot._trust is False
        assert slot._trust_reads is False


class TestRosterCarriesTheProfileSettings:
    @pytest.mark.asyncio
    async def test_the_row_names_mode_and_effort(self, tmp_path, monkeypatch):
        cfg = _config_with("trust_reads")
        cfg.agents[CREW].reasoning_effort = "high"
        monkeypatch.setattr("kiro_crew.dashboard.handlers.members.KiroCrewConfig.load", lambda: cfg)
        state = _make_state(tmp_path)
        async with TestClient(TestServer(_make_members_app(state))) as client:
            resp = await client.get("/api/members")
            assert resp.status == 200
            row = (await resp.json())["members"][0]
        assert row["approval_mode"] == "trust_reads"
        assert row["reasoning_effort"] == "high"


class TestTheRecordField:
    @pytest.mark.parametrize("mode", ["normal", "trust_reads", "trust"])
    def test_known_modes_survive(self, mode):
        assert coerce_member_approval_mode(mode) == mode

    @pytest.mark.parametrize("junk", [None, "", "yolo", "Trust", 1, ["trust"]])
    def test_anything_else_reads_as_never_chosen(self, junk):
        assert coerce_member_approval_mode(junk) == ""

    def test_a_saved_choice_round_trips_through_the_config_file(self):
        cfg = KiroCrewConfig.load()
        cfg.agents["mate"] = KiroCrewAgentConfig(kiro_agent="kirocrew", approval_mode="normal")
        cfg.save()
        assert KiroCrewConfig.load().agents["mate"].approval_mode == "normal"


def _update_app() -> web.Application:
    from kiro_crew.dashboard.handlers import api_kirocrew_agent_update

    app = web.Application()
    app.router.add_put("/api/agents/{name}", api_kirocrew_agent_update)
    return app


@pytest.fixture()
def seeded_mate():
    cfg = KiroCrewConfig.load()
    cfg.agents["mate"] = KiroCrewAgentConfig(
        kiro_agent="kirocrew", workspace="default", memory_store="default"
    )
    cfg.save()
    return "mate"


class TestProfileWrite:
    @pytest.mark.asyncio
    async def test_the_profile_stores_the_choice(self, seeded_mate):
        async with TestClient(TestServer(_update_app())) as client:
            resp = await client.put(f"/api/agents/{seeded_mate}", json={"approval_mode": "normal"})
            assert resp.status == 200, await resp.text()
        assert KiroCrewConfig.load().agents[seeded_mate].approval_mode == "normal"

    @pytest.mark.asyncio
    async def test_an_unrelated_edit_keeps_the_stored_choice(self, seeded_mate):
        async with TestClient(TestServer(_update_app())) as client:
            assert (
                await client.put(f"/api/agents/{seeded_mate}", json={"approval_mode": "normal"})
            ).status == 200
            assert (
                await client.put(f"/api/agents/{seeded_mate}", json={"description": "hi"})
            ).status == 200
        assert KiroCrewConfig.load().agents[seeded_mate].approval_mode == "normal"

    @pytest.mark.asyncio
    @pytest.mark.parametrize("bad", ["yolo", "", "TRUST", 1])
    async def test_yolo_unset_and_junk_are_refused(self, seeded_mate, bad):
        async with TestClient(TestServer(_update_app())) as client:
            resp = await client.put(
                f"/api/agents/{seeded_mate}", json={"workspace": "other", "approval_mode": bad}
            )
            assert resp.status == 400
            assert (await resp.json())["code"] == "invalid_approval_mode"
        stored = KiroCrewConfig.load().agents[seeded_mate]
        assert stored.approval_mode == ""
        assert stored.workspace == "default"


class TestProfileWriteIsCompareAndSet:
    """The profile sends the value it last read; the server decides which pick wins."""

    @pytest.mark.asyncio
    async def test_a_fresh_write_is_applied(self, seeded_mate):
        async with TestClient(TestServer(_update_app())) as client:
            resp = await client.put(
                f"/api/agents/{seeded_mate}",
                json={"approval_mode": "normal", "expected_approval_mode": ""},
            )
            assert resp.status == 200, await resp.text()
        assert KiroCrewConfig.load().agents[seeded_mate].approval_mode == "normal"

    @pytest.mark.asyncio
    async def test_a_stale_write_is_refused_with_the_current_value(self, seeded_mate):
        async with TestClient(TestServer(_update_app())) as client:
            assert (
                await client.put(
                    f"/api/agents/{seeded_mate}",
                    json={"approval_mode": "normal", "expected_approval_mode": ""},
                )
            ).status == 200
            # Read before Normal landed: it still believes the mode is unset.
            resp = await client.put(
                f"/api/agents/{seeded_mate}",
                json={"approval_mode": "trust", "expected_approval_mode": ""},
            )
            assert resp.status == 409
            body = await resp.json()
        assert body["code"] == "approval_mode_conflict"
        assert body["approval_mode"] == "normal"
        assert KiroCrewConfig.load().agents[seeded_mate].approval_mode == "normal"

    @pytest.mark.asyncio
    async def test_a_refused_write_changes_no_other_field(self, seeded_mate):
        async with TestClient(TestServer(_update_app())) as client:
            resp = await client.put(
                f"/api/agents/{seeded_mate}",
                json={
                    "approval_mode": "trust",
                    "expected_approval_mode": "normal",
                    "workspace": "other",
                },
            )
            assert resp.status == 409
        stored = KiroCrewConfig.load().agents[seeded_mate]
        assert stored.approval_mode == ""
        assert stored.workspace == "default"


def _update_app_with_state(state) -> web.Application:
    app = _update_app()
    app["state"] = state
    return app


def _live_mate_slot(state):
    slot = state.get_or_create_slot("member-mate", agent="mate", mode="member", workspace="default")
    slot._trust = True
    slot._member_approval_seeded = True
    return slot


class TestProfileWriteMovesTheLiveThread:
    """The PUT owns both writes: the record and the crewmate's live DM slot."""

    @pytest.mark.asyncio
    async def test_a_fresh_put_updates_the_record_and_the_live_slot(self, tmp_path, seeded_mate):
        state = _make_state(tmp_path)
        slot = _live_mate_slot(state)
        async with TestClient(TestServer(_update_app_with_state(state))) as client:
            resp = await client.put(
                f"/api/agents/{seeded_mate}",
                json={"approval_mode": "normal", "expected_approval_mode": ""},
            )
            assert resp.status == 200, await resp.text()
        assert KiroCrewConfig.load().agents[seeded_mate].approval_mode == "normal"
        assert slot._trust is False
        assert slot._trust_reads is False

    @pytest.mark.asyncio
    async def test_a_stale_put_is_refused_and_leaves_the_slot_untouched(
        self, tmp_path, seeded_mate
    ):
        state = _make_state(tmp_path)
        slot = _live_mate_slot(state)
        async with TestClient(TestServer(_update_app_with_state(state))) as client:
            resp = await client.put(
                f"/api/agents/{seeded_mate}",
                json={"approval_mode": "normal", "expected_approval_mode": "trust"},
            )
            assert resp.status == 409
        assert KiroCrewConfig.load().agents[seeded_mate].approval_mode == ""
        assert slot._trust is True

    @pytest.mark.asyncio
    async def test_an_unrelated_edit_leaves_the_slot_alone(self, tmp_path, seeded_mate):
        state = _make_state(tmp_path)
        slot = _live_mate_slot(state)
        async with TestClient(TestServer(_update_app_with_state(state))) as client:
            resp = await client.put(f"/api/agents/{seeded_mate}", json={"description": "hi"})
            assert resp.status == 200, await resp.text()
        assert slot._trust is True


class TestCompareAndSetHoldsAcrossProcesses:
    @pytest.mark.asyncio
    async def test_a_cli_write_after_the_handler_read_wins(
        self, tmp_path, seeded_mate, monkeypatch
    ):
        """Another process sets Normal after the handler loaded its snapshot but
        before it saves. The check runs on the record on disk, under the
        cross-process lock, so the stale Trust is refused and the slot is left."""
        import kiro_crew.dashboard.handlers.agents as agents_handlers

        real = agents_handlers.persist_member_config

        def cli_write_then_persist(*args, **kwargs):
            other = KiroCrewConfig.load()
            other.agents[seeded_mate].approval_mode = "normal"
            other.save()
            return real(*args, **kwargs)

        monkeypatch.setattr(agents_handlers, "persist_member_config", cli_write_then_persist)
        state = _make_state(tmp_path)
        slot = _live_mate_slot(state)
        async with TestClient(TestServer(_update_app_with_state(state))) as client:
            resp = await client.put(
                f"/api/agents/{seeded_mate}",
                json={"approval_mode": "trust", "expected_approval_mode": ""},
            )
            assert resp.status == 409
            assert (await resp.json())["approval_mode"] == "normal"
        assert KiroCrewConfig.load().agents[seeded_mate].approval_mode == "normal"
        assert slot._trust is True


def _hold_shell(name: str, member_id: str = ""):
    """Record one held shell limit for *name*, as the upgrade does (``mate_held_rules``).

    With *member_id*, the Bot gets that id and the entry is recorded under its
    old name, so only the id ties the two.
    """
    import json

    from kiro_crew.config import paths as paths_mod

    key = name
    if member_id:
        cfg = KiroCrewConfig.load()
        cfg.agents[name].member_id = member_id
        cfg.save()
        key = "its-old-name"
    ledger = paths_mod.mate_agents_dir().parent / "migrated.json"
    ledger.parent.mkdir(parents=True, exist_ok=True)
    rules = [{"text": "its permissions rule to deny shell", "capability": "shell"}]
    held = {"held_rules": {key: {"member_id": member_id, "rules": rules}}}
    ledger.write_text(json.dumps({"migrated": [name], **held}), encoding="utf-8")
    return member_id, ledger


def _shell_call():
    from kiro_crew.providers.base import EVENT_PERMISSION_REQUEST, LLMEvent

    return LLMEvent(kind=EVENT_PERMISSION_REQUEST, title="ls", request_id="r1", tool_kind="execute")


class TestSavingThePermissionKeepsHeldLimits:
    """A Bot's approval mode does not decide an ``auto`` cron's approvals, so no pick
    of it, the strictest included, may end the limits the upgrade held for a person."""

    @pytest.mark.asyncio
    @pytest.mark.parametrize("mode", ["normal", "trust_reads", "trust"])
    async def test_a_saved_permission_keeps_the_hold_for_an_auto_cron(self, seeded_mate, mode):
        from kiro_crew import mate_held_rules
        from kiro_crew.cron_service.fields import auto_job_approver

        member_id, _ledger = _hold_shell(seeded_mate, "m-zxc")
        async with TestClient(TestServer(_update_app())) as client:
            resp = await client.put(
                f"/api/agents/{seeded_mate}",
                json={"approval_mode": mode, "expected_approval_mode": ""},
            )
            assert resp.status == 200, await resp.text()

        held = mate_held_rules.held_for(_shell_call(), agent=seeded_mate)
        assert held
        spec = {"allowedTools": ["execute_bash", "fs_read"]}
        kept = mate_held_rules.strip_held_approvals(spec, member_id, seeded_mate)
        assert "execute_bash" not in kept["allowedTools"]
        # The cron's own approver, asked inside the held mark, does not approve.
        fallback = AsyncMock(return_value=False)
        with mate_held_rules.held_call(held):
            assert await auto_job_approver(fallback)(_shell_call()) is False
        fallback.assert_awaited_once()
