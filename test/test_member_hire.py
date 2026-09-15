"""Hire a crew member from a local Custom Agent file — POST /api/members.

Rollout step 2 of *Crew Member = Custom Agent + Wrapper Layer*: the "adopt"
path. A hire (1) creates the wrapper row through the same validated create
path ``POST /api/agents`` uses, minting the id from the display name, then
(2) copies the source definition into a member-owned agent file whose declared
``name`` is the copy's stem (derived from the member id) and rebinds the row to
it — the private-copy fork the crew editor already uses on first edit.

The step-2 GATE: two members hired from ONE file coexist, each with its own
row and its own copy.
"""

from __future__ import annotations

import json
import os
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import MagicMock, patch

import pytest
from aiohttp import web
from aiohttp.test_utils import TestClient, TestServer
from member_memory_helpers import patch_private_memory_supported

from kiro_crew import agent_state
from kiro_crew.config.loader import KiroCrewAgentConfig, KiroCrewConfig
from kiro_crew.dashboard.handlers import agents as _agents

SOURCE = "reviewer"


@pytest.fixture(autouse=True)
def _owner_caller(monkeypatch):
    monkeypatch.setattr(
        "kiro_crew.dashboard.handlers.source_providers.is_owner_dashboard_request",
        lambda request: True,
    )
    patch_private_memory_supported(monkeypatch)


@pytest.fixture
def agents_dir(tmp_path: Path):
    d = tmp_path / "agents"
    d.mkdir()
    spec = {
        "name": SOURCE,
        "description": "Reviews pull requests.",
        "prompt": "You review code.",
        "model": "claude-x",
        "tools": ["ReadFile"],
    }
    (d / f"{SOURCE}.json").write_text(json.dumps(spec), encoding="utf-8")
    cfg = KiroCrewConfig()
    cfg.agents = {"default": KiroCrewAgentConfig(kiro_agent="kirocrew")}
    cfg.default_agent = "default"
    cfg.save()
    # `list_agents()` (the create path's existence probe) and the fork's spec
    # reads both resolve the agents directory through kiro_crew.agent.
    with (
        patch("kiro_crew.agent.KIRO_AGENTS_DIR", d),
        patch(
            "kiro_crew.dashboard.handlers.agents.list_agents",
            lambda *a, **k: [SimpleNamespace(name=SOURCE), SimpleNamespace(name="kirocrew")],
        ),
    ):
        yield d


def _app() -> web.Application:
    from kiro_crew.dashboard.handlers import (
        api_kirocrew_agent_update,
        api_kirocrew_agents,
        api_kirocrew_agents_create,
        api_member_hire,
        api_members,
    )

    @web.middleware
    async def _auth(request: web.Request, handler):
        request.setdefault("app", "")
        request.setdefault("user", "local-app")
        return await handler(request)

    app = web.Application(middlewares=[_auth])
    app["state"] = MagicMock(sessions=None)
    app.router.add_post("/api/members", api_member_hire)
    app.router.add_post("/api/agents", api_kirocrew_agents_create)
    app.router.add_get("/api/members", api_members)
    app.router.add_put("/api/agents/{name}", api_kirocrew_agent_update)
    app.router.add_get("/api/agents", api_kirocrew_agents)
    return app


def _hire(display_name: str, **extra) -> dict:
    body = {"display_name": display_name, "source": {"kind": "local", "agent": SOURCE}}
    body.update(extra)
    return body


class TestHire:
    @pytest.mark.asyncio
    async def test_hire_creates_row_and_member_owned_copy(self, agents_dir: Path):
        async with TestClient(TestServer(_app())) as client:
            resp = await client.post("/api/members", json=_hire("Checkout triage", role="Oncall"))
            assert resp.status == 200, await resp.text()
            data = await resp.json()
        # The answer is what a caller reads: the minted id and the copy. The
        # label, store and source are read back from the roster row.
        assert data == {"ok": True, "id": "Checkout-triage"}
        # The copy: a real file whose declared name equals its stem, carrying
        # the source definition; the source itself is untouched.
        copy = json.loads((agents_dir / "Checkout-triage.json").read_text(encoding="utf-8"))
        assert copy["name"] == "Checkout-triage"
        assert copy["prompt"] == "You review code."
        assert json.loads((agents_dir / f"{SOURCE}.json").read_text())["name"] == SOURCE
        # Lineage for the fork refresh, and the row rebound to the copy.
        assert agent_state.get_fork_info("Checkout-triage") == {
            "forked_from": SOURCE,
            "private_to": "Checkout-triage",
        }
        row = KiroCrewConfig.load().agents["Checkout-triage"]
        assert row.kiro_agent == "Checkout-triage"
        assert row.display_name == "Checkout triage"
        assert row.role == "Oncall"

    @pytest.mark.asyncio
    async def test_gate_two_members_from_one_file_coexist(self, agents_dir: Path):
        async with TestClient(TestServer(_app())) as client:
            a = await (await client.post("/api/members", json=_hire("Checkout triage"))).json()
            b = await (await client.post("/api/members", json=_hire("Payments triage"))).json()
            roster = {
                r["name"]: r for r in (await (await client.get("/api/members")).json())["members"]
            }
        assert a["id"] == "Checkout-triage" and b["id"] == "Payments-triage"
        assert a["ok"] and b["ok"]
        # Two rows, two copies, one untouched source.
        cfg = KiroCrewConfig.load()
        assert cfg.agents["Checkout-triage"].kiro_agent == "Checkout-triage"
        assert cfg.agents["Payments-triage"].kiro_agent == "Payments-triage"
        assert (agents_dir / "Checkout-triage.json").exists()
        assert (agents_dir / "Payments-triage.json").exists()
        assert json.loads((agents_dir / f"{SOURCE}.json").read_text())["name"] == SOURCE
        # Both listed, each under its own label; both created here.
        assert roster["Checkout-triage"]["display_name"] == "Checkout triage"
        assert roster["Payments-triage"]["display_name"] == "Payments triage"
        assert roster["Checkout-triage"]["source"] == "kirocrew"
        # Lineage on the roster: each is bound to its OWN copy of the source,
        # and the row says which template that copy came from. The built-in
        # default, bound to a shared template directly, reports none.
        assert roster["Checkout-triage"]["kiro_agent"] == "Checkout-triage"
        assert roster["Checkout-triage"]["template_origin"] == SOURCE
        assert roster["Payments-triage"]["template_origin"] == SOURCE
        # ``default`` is a session agent: it stays usable and off the roster.
        assert "default" not in roster

    @pytest.mark.asyncio
    async def test_same_display_name_twice_is_409_not_a_second_copy(self, agents_dir: Path):
        async with TestClient(TestServer(_app())) as client:
            assert (await client.post("/api/members", json=_hire("Triage"))).status == 200
            resp = await client.post("/api/members", json=_hire("Triage"))
            assert resp.status == 409
            assert (await resp.json())["code"] == "agent_exists"
            # "triage" and "Triage" are distinct ids (the grammar is case-sensitive)
            # but share one SLUG -- the key of the member's space, rules and thread
            # binding -- so the hire refuses the second before anything is written.
            resp = await client.post("/api/members", json=_hire("triage"))
            assert resp.status == 409
            assert (await resp.json())["code"] == "slug_collision"
        assert sorted(p.stem for p in agents_dir.glob("*.json")) == ["Triage", SOURCE]

    @pytest.mark.asyncio
    async def test_copy_stem_is_suffixed_when_a_file_already_owns_the_id(self, agents_dir: Path):
        """An unrelated template already named like the member: the copy takes
        the next free stem and the row binds to THAT, never to the stranger."""
        (agents_dir / "Triage.json").write_text(json.dumps({"name": "Triage", "prompt": "other"}))
        with patch(
            "kiro_crew.dashboard.handlers.agents.list_agents",
            lambda *a, **k: [SimpleNamespace(name=SOURCE), SimpleNamespace(name="Triage")],
        ):
            async with TestClient(TestServer(_app())) as client:
                data = await (await client.post("/api/members", json=_hire("Triage"))).json()
        assert data["id"] == "Triage"
        assert KiroCrewConfig.load().agents["Triage"].kiro_agent == "Triage-2"
        assert (
            json.loads((agents_dir / "Triage-2.json").read_text())["prompt"] == "You review code."
        )
        assert json.loads((agents_dir / "Triage.json").read_text())["prompt"] == "other"
        assert KiroCrewConfig.load().agents["Triage"].kiro_agent == "Triage-2"

    @pytest.mark.asyncio
    async def test_no_moment_exists_where_a_row_is_bound_to_the_shared_source(
        self, agents_dir: Path
    ):
        """The copy is made BEFORE the row exists, inside the create's lock hold,
        and the row is published already bound to it. A concurrent thread open
        can therefore never resolve a member bound to the shared template."""
        real_copy = _agents.__dict__["_write_private_copy"]
        real_persist = _agents.__dict__["persist_member_config"]
        seen: dict[str, object] = {}

        def copy_then_look(*args, **kwargs):
            result = real_copy(*args, **kwargs)
            # The copy exists; the row does not, yet.
            seen["row_at_copy"] = "Triage" in KiroCrewConfig.load().agents
            seen["file_at_copy"] = (agents_dir / "Triage.json").exists()
            return result

        def persist_then_look(cfg, name, **kwargs):
            # What is about to be published is bound to the COPY, never the source.
            seen["binding_at_persist"] = cfg.agents[name].kiro_agent
            return real_persist(cfg, name, **kwargs)

        with (
            patch("kiro_crew.dashboard.handlers.agents._write_private_copy", copy_then_look),
            patch("kiro_crew.dashboard.handlers.agents.persist_member_config", persist_then_look),
        ):
            async with TestClient(TestServer(_app())) as client:
                resp = await client.post("/api/members", json=_hire("Triage"))
                assert resp.status == 200, await resp.text()
        assert seen == {"row_at_copy": False, "file_at_copy": True, "binding_at_persist": "Triage"}
        assert KiroCrewConfig.load().agents["Triage"].kiro_agent == "Triage"

    @pytest.mark.asyncio
    async def test_a_failed_copy_leaves_no_member(self, agents_dir: Path):
        """Atomic: the copy fails, nothing is published -- no row, no private
        memory, no file -- and the copy's error is the answer. A retry is a
        clean create, not a 409 on a phantom."""
        with patch(
            "kiro_crew.dashboard.handlers.agents._write_private_copy",
            side_effect=RuntimeError("disk"),
        ):
            async with TestClient(TestServer(_app())) as client:
                resp = await client.post("/api/members", json=_hire("Triage"))
                assert resp.status == 500
                data = await resp.json()
                assert data["code"] == "fork_failed"
                assert "rolled_back" not in data
            assert set(KiroCrewConfig.load().agents) == {"default"}
            assert not (agents_dir / "Triage.json").exists()
            assert agent_state.get_fork_info("Triage") is None
        async with TestClient(TestServer(_app())) as client:
            resp = await client.post("/api/members", json=_hire("Triage"))
            assert resp.status == 200, await resp.text()
        cfg = KiroCrewConfig.load()
        assert set(cfg.agents) == {"default", "Triage"}
        assert cfg.agents["Triage"].kiro_agent == "Triage"
        assert (agents_dir / "Triage.json").exists()

    @pytest.mark.asyncio
    async def test_a_source_that_vanishes_between_resolve_and_copy_is_404_with_nothing_written(
        self, agents_dir: Path
    ):
        """Step 1 saw the file; it is gone by the copy (a concurrent uninstall).
        The copy's own 404 is the answer and nothing is left behind."""
        real_copy = _agents.__dict__["_write_private_copy"]

        def vanish_then_copy(*args, **kwargs):
            (agents_dir / f"{SOURCE}.json").unlink()
            return real_copy(*args, **kwargs)

        with patch("kiro_crew.dashboard.handlers.agents._write_private_copy", vanish_then_copy):
            async with TestClient(TestServer(_app())) as client:
                resp = await client.post("/api/members", json=_hire("Triage"))
                assert resp.status == 404, await resp.text()
                data = await resp.json()
        assert data["code"] == "template_not_found"
        assert set(KiroCrewConfig.load().agents) == {"default"}
        assert not (agents_dir / "Triage.json").exists()

    @pytest.mark.asyncio
    async def test_a_row_that_fails_to_persist_unwinds_the_copy(self, agents_dir: Path):
        """The copy exists when the row's publish fails (disk, store): the copy
        and its lineage are taken back, so a retry does not find a stranded
        file already claiming the id."""
        with patch(
            "kiro_crew.dashboard.handlers.agents.persist_member_config",
            side_effect=OSError("disk full"),
        ):
            async with TestClient(TestServer(_app())) as client:
                resp = await client.post("/api/members", json=_hire("Triage"))
                assert resp.status == 409, await resp.text()
                assert (await resp.json())["code"] == "member_memory_unavailable"
        assert set(KiroCrewConfig.load().agents) == {"default"}
        assert not (agents_dir / "Triage.json").exists()
        assert agent_state.get_fork_info("Triage") is None
        async with TestClient(TestServer(_app())) as client:
            resp = await client.post("/api/members", json=_hire("Triage"))
            assert resp.status == 200, await resp.text()
        assert (agents_dir / "Triage.json").exists()

    @pytest.mark.asyncio
    async def test_a_hire_that_loses_the_create_guard_never_touches_the_winners_record(
        self, agents_dir: Path
    ):
        """Two gateways hire the same id. The loser is refused by the create
        guard INSIDE the locked publication -- and enrollment runs only after
        that guard admitted the row, so the loser writes no record and, when it
        unwinds, removes none: the winner's committed record (its own
        generation) is exactly as it was."""
        from kiro_crew.memory_stores import MemberAlreadyExists

        # The winner: a row published by "another gateway", already enrolled.
        agent_state.set_crewmate_record(
            "Triage", generation="member-winner", template="reviewer", hired_at="w"
        )
        winner = dict(agent_state.get_crewmate_record("Triage", strict=True))

        def _lose(*args, **kwargs):
            # The guard refuses before the document is written: after_write
            # (the enrollment) must never run.
            assert kwargs.get("after_write") is not None
            raise MemberAlreadyExists("Crew Member 'Triage' was created concurrently")

        with patch("kiro_crew.dashboard.handlers.agents.persist_member_config", _lose):
            async with TestClient(TestServer(_app())) as client:
                resp = await client.post("/api/members", json=_hire("Triage"))
                assert resp.status == 409, await resp.text()
                assert (await resp.json())["code"] == "agent_exists"
        assert agent_state.get_crewmate_record("Triage", strict=True) == winner
        # The loser's copy is unwound; nothing else moved.
        assert not (agents_dir / "Triage.json").exists()

    @pytest.mark.asyncio
    async def test_a_row_whose_enrollment_fails_after_it_landed_is_removed_again(
        self, agents_dir: Path
    ):
        """The record is written after the row is durable. If THAT write fails
        (an unreadable sidecar), the row is a member nobody can list or open:
        the hire removes it again -- by its own generation, inside the lock --
        and answers 500 enrollment_failed; a retry is clean."""
        calls = {"n": 0}
        real_set = agent_state.set_crewmate_record

        def _fail_once(*args, **kwargs):
            calls["n"] += 1
            if calls["n"] == 1:
                raise ValueError("agent_state_file_invalid")
            return real_set(*args, **kwargs)

        with patch(
            "kiro_crew.dashboard.handlers.members.agent_state.set_crewmate_record", _fail_once
        ):
            async with TestClient(TestServer(_app())) as client:
                resp = await client.post("/api/members", json=_hire("Triage"))
                assert resp.status == 500, await resp.text()
                assert (await resp.json())["code"] == "enrollment_failed"
                assert set(KiroCrewConfig.load().agents) == {"default"}
                assert agent_state.get_crewmate_record("Triage", strict=True) is None
                assert not (agents_dir / "Triage.json").exists()
                resp = await client.post("/api/members", json=_hire("Triage"))
                assert resp.status == 200, await resp.text()
        assert agent_state.get_crewmate_record("Triage", strict=True) is not None
        assert (agents_dir / "Triage.json").exists()

    @pytest.mark.asyncio
    async def test_an_unwind_that_cannot_remove_the_file_keeps_its_lineage(self, agents_dir: Path):
        """The copy's lineage is what makes every ownership check read it as
        PRIVATE. Pruned while the file stays -- a row took it, or the unlink
        failed -- the copy would read as a shared template."""

        from kiro_crew.dashboard.handlers.agents import _unwind_private_copy

        dest = agents_dir / "Triage.json"
        dest.write_text(json.dumps({"name": "Triage"}), encoding="utf-8")
        agent_state.set_fork_info("Triage", forked_from=SOURCE, private_to="Triage")
        # A row bound to the copy: the file and its lineage both stay.
        cfg = KiroCrewConfig.load()
        cfg.agents["Triage"] = KiroCrewAgentConfig(kiro_agent="Triage")
        cfg.save()
        _unwind_private_copy(dest, "Triage")
        assert dest.exists()
        assert agent_state.get_fork_info("Triage")["private_to"] == "Triage"
        # No row, but the unlink fails: lineage still stays with the file.
        del cfg.agents["Triage"]
        cfg.save()
        with patch.object(Path, "unlink", side_effect=OSError("busy")):
            _unwind_private_copy(dest, "Triage")
        assert dest.exists()
        assert agent_state.get_fork_info("Triage")["private_to"] == "Triage"
        # The ordinary case: file gone, then lineage.
        _unwind_private_copy(dest, "Triage")
        assert not dest.exists()
        assert agent_state.get_fork_info("Triage") is None

    @pytest.mark.asyncio
    async def test_the_unwind_checks_for_a_binder_under_the_config_lock(self, agents_dir: Path):
        """A failed hire unwinds its copy from OUTSIDE any lock. The reference
        check and the unlink are one critical section under the config lock:
        a binder in another process cannot land between them, so the binding
        the check reads is the binding the unlink is judged against -- the
        document handed to the locked mutation, not a file read made before
        the lock. A caller already inside a locked mutation hands its
        document over instead of taking the lock a second time."""

        from kiro_crew.dashboard.handlers.agents import _unwind_private_copy

        dest = agents_dir / "Triage.json"
        dest.write_text(json.dumps({"name": "Triage"}), encoding="utf-8")
        agent_state.set_fork_info("Triage", forked_from=SOURCE, private_to="Triage")
        # The file on disk has NO binding; the locked document does (a binder
        # that won the lock first). The unwind must judge by the document.
        real = _agents.update_config_locked
        seen: list[dict] = []

        def _locked(*args, **kwargs):
            mutate = kwargs["mutate"]

            def _with_binder(data: dict):
                data.setdefault("agents", {})["other"] = {"kiro_agent": "Triage"}
                seen.append(data)
                return mutate(data)

            return real(*args, mutate=_with_binder)

        with patch.object(_agents, "update_config_locked", side_effect=_locked):
            _unwind_private_copy(dest, "Triage")
        assert seen, "the reference check did not run under the config lock"
        assert dest.exists()
        assert agent_state.get_fork_info("Triage")["private_to"] == "Triage"
        # In-lock callers pass their document: a binding there keeps the file
        # even though the config on disk names none...
        _unwind_private_copy(dest, "Triage", doc={"agents": {"x": {"kiro_agent": "Triage"}}})
        assert dest.exists()
        # ...and a document without one lets the unwind proceed.
        _unwind_private_copy(dest, "Triage", doc={"agents": {}})
        assert not dest.exists()
        assert agent_state.get_fork_info("Triage") is None
        # An unreadable config fails closed: the file stays.
        dest.write_text(json.dumps({"name": "Triage"}), encoding="utf-8")
        with patch.object(_agents, "update_config_locked", side_effect=OSError("locked out")):
            _unwind_private_copy(dest, "Triage")
        assert dest.exists()

    @pytest.mark.asyncio
    async def test_a_name_that_shares_another_members_slug_is_refused_before_anything_is_written(
        self, agents_dir: Path
    ):
        """The slug keys the member's space, rules and thread binding and is
        lossy: two members on one slug would inherit each other's briefing and
        be refused their thread. The hire says so before it writes."""
        async with TestClient(TestServer(_app())) as client:
            resp = await client.post("/api/members", json=_hire("Triage"))
            assert resp.status == 200, await resp.text()
            before = sorted(p.name for p in agents_dir.iterdir())
            resp = await client.post("/api/members", json=_hire("triage"))
            assert resp.status == 409, await resp.text()
            body = await resp.json()
            assert body["code"] == "slug_collision"
            assert "Triage" in body["error"] and "triage" in body["error"]
        assert sorted(p.name for p in agents_dir.iterdir()) == before
        assert set(KiroCrewConfig.load().agents) == {"default", "Triage"}

    @pytest.mark.asyncio
    async def test_a_cancelled_hire_finishes_its_transaction(self, agents_dir: Path):
        """The handler is cancelled (gateway shutdown, client gone) while the
        create -- copy plus row -- is in flight. The transaction still reaches
        its own end: the member is whole, never a copy without its row."""
        import asyncio

        from aiohttp.test_utils import make_mocked_request

        from kiro_crew.dashboard.handlers import api_member_hire

        real_create = _agents.__dict__["_create_crew"]
        entered = asyncio.Event()
        release = asyncio.Event()

        async def slow_create(request, body, **kwargs):
            entered.set()
            await release.wait()
            return await real_create(request, body, **kwargs)

        app = _app()
        request = make_mocked_request("POST", "/api/members", app=app, payload=None)
        request["user"] = "local-app"
        body = _hire("Triage")

        async def _json():
            return body

        request.json = _json  # type: ignore[method-assign]
        with patch(
            "kiro_crew.dashboard.handlers.members._agents_handlers._create_crew", slow_create
        ):
            handler = asyncio.ensure_future(api_member_hire(request))
            await entered.wait()
            handler.cancel()  # the outer request is gone mid-hire
            await asyncio.sleep(0)
            release.set()
            with pytest.raises(asyncio.CancelledError):
                await handler
        cfg = KiroCrewConfig.load()
        assert cfg.agents["Triage"].kiro_agent == "Triage"
        assert (agents_dir / "Triage.json").exists()

    @pytest.mark.asyncio
    async def test_the_source_must_not_be_another_members_private_copy(self, agents_dir: Path):
        """The create's foreign-copy check runs on the SOURCE: hiring from a file
        the sidecar names as another crew's private copy is refused."""
        async with TestClient(TestServer(_app())) as client:
            resp = await client.post("/api/members", json=_hire("Triage"))
            assert resp.status == 200, await resp.text()
            resp = await client.post(
                "/api/members",
                json={"display_name": "Copycat", "source": {"kind": "local", "agent": "Triage"}},
            )
            assert resp.status == 409, await resp.text()
            assert (await resp.json())["code"] == "foreign_private_copy"
        assert "Copycat" not in KiroCrewConfig.load().agents

    @pytest.mark.asyncio
    async def test_a_copy_recorded_between_the_lineage_check_and_the_write_is_refused(
        self, agents_dir: Path
    ):
        """The in-process lock does not hold a second gateway. A plain create
        bound to ``Triage`` -- a name no template holds when its lineage check
        runs (tolerated: an edition may resolve it) -- must not publish when
        another gateway's hire copies a template to exactly that name and
        records its member as the copy's owner before this create's write. The
        locked publication re-checks lineage against the sidecar and the
        document as they are AT the write and refuses, with nothing written."""
        from kiro_crew.config.loader import update_config_locked

        real = _agents.__dict__["_foreign_private_copy_owner"]
        calls: list[tuple[bool, str, str]] = []

        def check_then_plant(crew, target):
            calls.append((_agents._get_config_lock().locked(), crew, target))
            out = real(crew, target)
            if len(calls) == 1:
                # "Another gateway" hires Triage right after this create's
                # pre-lock check: the copy, its lineage and its row all land.
                spec = json.loads((agents_dir / f"{SOURCE}.json").read_text(encoding="utf-8"))
                (agents_dir / "Triage.json").write_text(
                    json.dumps({**spec, "name": "Triage"}), encoding="utf-8"
                )
                agent_state.set_fork_info("Triage", forked_from=SOURCE, private_to="Triage")

                def mutate(doc):
                    doc["agents"]["Triage"] = {"kiro_agent": "Triage"}
                    return doc

                update_config_locked(mutate=mutate)
            return out

        with patch(
            "kiro_crew.dashboard.handlers.agents._foreign_private_copy_owner", check_then_plant
        ):
            async with TestClient(TestServer(_app())) as client:
                resp = await client.post(
                    "/api/agents", json={"name": "Helper", "kiro_agent": "Triage"}
                )
                assert resp.status == 409, await resp.text()
                assert (await resp.json())["code"] == "foreign_private_copy"
        # Once before anything is written, once inside the locked publication.
        assert calls == [(True, "Helper", "Triage"), (True, "Helper", "Triage")]
        cfg = KiroCrewConfig.load()
        assert "Helper" not in cfg.agents and "Triage" in cfg.agents
        assert all(rec.owner_member != "Helper" for rec in cfg.memory_stores.values())
        # The other gateway's copy is untouched: file, lineage and row.
        assert (agents_dir / "Triage.json").exists()
        assert agent_state.get_fork_info("Triage") == {
            "forked_from": SOURCE,
            "private_to": "Triage",
        }

    @pytest.mark.asyncio
    async def test_a_row_another_process_bound_to_the_copy_refuses_the_hire(self, agents_dir: Path):
        """The document is the other half of the same fact: a row that another
        process bound to THIS hire's fresh copy between the copy and the write
        (its own lineage check predates the copy's lineage) means two members
        would share one private definition. The locked publication sees that
        row and refuses; the copy is left in place because a row now binds it
        (the unwind's own rule), so the squatter keeps a resolvable template."""
        from kiro_crew.config.loader import update_config_locked

        real_persist = _agents.__dict__["persist_member_config"]

        def plant_then_persist(cfg, name, **kwargs):
            # The copy exists (bound to nobody yet); "another process" binds it.
            assert (agents_dir / "Triage.json").exists()

            def mutate(doc):
                doc["agents"]["Squatter"] = {"kiro_agent": "Triage"}
                return doc

            update_config_locked(mutate=mutate)
            return real_persist(cfg, name, **kwargs)

        with patch("kiro_crew.dashboard.handlers.agents.persist_member_config", plant_then_persist):
            async with TestClient(TestServer(_app())) as client:
                resp = await client.post("/api/members", json=_hire("Triage"))
                assert resp.status == 409, await resp.text()
                assert (await resp.json())["code"] == "foreign_private_copy"
        cfg = KiroCrewConfig.load()
        assert "Triage" not in cfg.agents and cfg.agents["Squatter"].kiro_agent == "Triage"
        assert all(rec.owner_member != "Triage" for rec in cfg.memory_stores.values())
        assert (agents_dir / "Triage.json").exists()

    @pytest.mark.asyncio
    async def test_the_slug_check_runs_inside_the_config_lock(self, agents_dir: Path):
        """The check is the create's ``admit`` hook: it runs with the lock held,
        against the snapshot the row is published from and with the id the
        create minted -- a pre-lock check would let ``Triage`` and ``triage``
        both pass and then serialize into two rows on one slug."""
        from kiro_crew.dashboard.handlers import members as members_handlers

        real = members_handlers.__dict__["_slug_collision_refusal"]
        observed: list[tuple[bool, str, set[str]]] = []

        def observe(cfg, candidate):
            observed.append((_agents._get_config_lock().locked(), candidate, set(cfg)))
            return real(cfg, candidate)

        with patch("kiro_crew.dashboard.handlers.members._slug_collision_refusal", observe):
            async with TestClient(TestServer(_app())) as client:
                resp = await client.post("/api/members", json=_hire("Triage"))
                assert resp.status == 200, await resp.text()
                resp = await client.post("/api/members", json=_hire("triage"))
                assert resp.status == 409
                assert (await resp.json())["code"] == "slug_collision"
        # Twice for the hire that publishes -- on the parsed snapshot and
        # again on the document inside the locked write -- once for the one
        # refused before anything is written.
        assert observed == [
            (True, "Triage", {"default"}),
            (True, "Triage", {"default"}),
            (True, "triage", {"default", "Triage"}),
        ]
        assert not _agents._get_config_lock().locked()

    @pytest.mark.asyncio
    async def test_the_slug_check_sees_a_row_another_process_published(self, agents_dir: Path):
        """The in-process lock does not hold a second gateway or the CLI. A
        same-slug row that lands in the document between this hire's parsed
        check and its write is seen by the check the locked publication runs
        against the document, and the hire is refused with nothing written."""
        from kiro_crew.config.loader import update_config_locked
        from kiro_crew.dashboard.handlers import members as members_handlers

        real = members_handlers.__dict__["_slug_collision_refusal"]
        planted: list[str] = []

        def plant_then_check(cfg, candidate):
            # The first call is the check on the parsed snapshot.
            if not planted:
                # "Another process" publishes ``triage`` right after the parsed check.
                def mutate(doc):
                    doc["agents"]["triage"] = {"kiro_agent": "kirocrew"}
                    return doc

                planted.append("triage")
                out = real(cfg, candidate)
                update_config_locked(mutate=mutate)
                return out
            return real(cfg, candidate)

        with patch(
            "kiro_crew.dashboard.handlers.members._slug_collision_refusal", plant_then_check
        ):
            async with TestClient(TestServer(_app())) as client:
                resp = await client.post("/api/members", json=_hire("Triage"))
                assert resp.status == 409, await resp.text()
                assert (await resp.json())["code"] == "slug_collision"
        agents = KiroCrewConfig.load().agents
        assert "triage" in agents and "Triage" not in agents
        assert not (agents_dir / "Triage.json").exists()
        assert agent_state.get_fork_info("Triage") is None

    @pytest.mark.asyncio
    async def test_concurrent_hires_on_one_slug_publish_exactly_one_member(self, agents_dir: Path):
        """``Triage`` and ``triage`` hired at once: whichever serializes second
        sees the first's row under the lock and is refused."""
        import asyncio

        async with TestClient(TestServer(_app())) as client:
            first, second = await asyncio.gather(
                client.post("/api/members", json=_hire("Triage")),
                client.post("/api/members", json=_hire("triage")),
            )
            statuses = sorted([first.status, second.status])
            assert statuses == [200, 409], [await first.text(), await second.text()]
            refused = first if first.status == 409 else second
            assert (await refused.json())["code"] == "slug_collision"
        names = set(KiroCrewConfig.load().agents) - {"default"}
        assert len(names) == 1 and names <= {"Triage", "triage"}
        assert sorted(p.stem for p in agents_dir.glob("*.json")) == sorted([*names, SOURCE])

    @pytest.mark.asyncio
    async def test_the_copy_is_reserved_and_written_under_the_config_file_lock(
        self, agents_dir: Path
    ):
        """The bindings are read and the copy created while the config FILE
        lock (the cross-process one, ``<config>.lock``) is held, the way the
        fork and publish paths do: a writer in another process cannot bind the
        destination between the read and the file's first byte."""
        from kiro_crew.config.loader import config_path
        from kiro_crew.platform_compat import try_acquire_lock

        real = _agents.__dict__["_write_private_copy"]
        observed: list[bool] = []

        def observe(*args, **kwargs):
            lock_path = config_path().with_name(config_path().name + ".lock")
            fd = os.open(lock_path, os.O_RDWR | os.O_CREAT, 0o600)
            try:
                # A second descriptor cannot take the lock while the update holds it.
                observed.append(not try_acquire_lock(fd, exclusive=True))
            finally:
                os.close(fd)
            return real(*args, **kwargs)

        with patch("kiro_crew.dashboard.handlers.agents._write_private_copy", observe):
            async with TestClient(TestServer(_app())) as client:
                resp = await client.post("/api/members", json=_hire("Triage"))
                assert resp.status == 200, await resp.text()
        assert observed == [True]

    @pytest.mark.asyncio
    async def test_a_dotted_template_the_listing_offers_can_be_hired(self, agents_dir: Path):
        """The source is a template FILE (the template-name grammar allows
        ``reviewer.v2``); the row is bound to the copy, whose stem is the
        member id, so the binding stays inside the agent-name grammar."""
        (agents_dir / "reviewer.v2.json").write_text(
            json.dumps({"name": "reviewer.v2", "prompt": "v2"}), encoding="utf-8"
        )
        async with TestClient(TestServer(_app())) as client:
            resp = await client.post(
                "/api/members",
                json={
                    "display_name": "Triage",
                    "source": {"kind": "local", "agent": "reviewer.v2"},
                },
            )
            assert resp.status == 200, await resp.text()
        row = KiroCrewConfig.load().agents["Triage"]
        assert row.kiro_agent == "Triage"
        assert json.loads((agents_dir / "Triage.json").read_text())["prompt"] == "v2"
        assert agent_state.get_fork_info("Triage")["forked_from"] == "reviewer.v2"

    @pytest.mark.asyncio
    async def test_gate_a_confirmed_hire_is_named_by_its_owner_and_enrolled(self, agents_dir: Path):
        """The hire gate under explicit enrollment: every hire carries the name
        the owner typed (there is no other way to have one), the same template
        hired twice under two names is two crewmates with two private files
        and the shared source untouched, and each is ENROLLED -- the sealed
        record the roster reads, stamped with the row's private store."""
        async with TestClient(TestServer(_app())) as client:
            a = await client.post("/api/members", json=_hire("Nia", role="Code Reviewer"))
            assert a.status == 200, await a.text()
            b = await client.post("/api/members", json=_hire("Code Reviewer", role="Code Reviewer"))
            assert b.status == 200, await b.text()
            roster = {
                r["name"]: r for r in (await (await client.get("/api/members")).json())["members"]
            }
        cfg = KiroCrewConfig.load()
        assert cfg.agents["Nia"].display_name == ""  # label == id: stored empty
        # The name may equal the role when the owner typed it.
        assert cfg.agents["Code-Reviewer"].display_name == "Code Reviewer"
        assert (
            cfg.agents["Nia"].kiro_agent == "Nia"
            and cfg.agents["Code-Reviewer"].kiro_agent == "Code-Reviewer"
        )
        assert json.loads((agents_dir / f"{SOURCE}.json").read_text())["name"] == SOURCE
        assert set(roster) == {"Nia", "Code-Reviewer"}
        assert "named_by_user" not in roster["Nia"]  # no such flag: every name is a person's
        records = agent_state.all_crewmate_records()
        assert set(records) == {"Nia", "Code-Reviewer"}
        assert records["Nia"]["generation"] == cfg.agents["Nia"].memory_store
        assert records["Nia"]["template"] == SOURCE and records["Nia"]["hired_at"]

    @pytest.mark.asyncio
    async def test_a_hire_without_a_name_is_refused_before_anything_is_written(
        self, agents_dir: Path
    ):
        """A crewmate is named by its owner BEFORE it exists: no name, no hire --
        there is no fallback to the role or to the source file, and nothing is
        minted, copied, provisioned or enrolled."""
        before = sorted(p.name for p in agents_dir.iterdir())
        async with TestClient(TestServer(_app())) as client:
            for body in (
                {"source": {"kind": "local", "agent": SOURCE}},
                {"source": {"kind": "local", "agent": SOURCE}, "role": "Code Reviewer"},
            ):
                resp = await client.post("/api/members", json=body)
                assert resp.status == 400, await resp.text()
                assert (await resp.json())["code"] == "missing_display_name"
            roster = (await (await client.get("/api/members")).json())["members"]
        assert roster == []
        assert sorted(p.name for p in agents_dir.iterdir()) == before
        assert set(KiroCrewConfig.load().agents) == {"default"}
        assert agent_state.all_crewmate_records() == {}

    @pytest.mark.asyncio
    async def test_a_rename_keeps_the_id_and_the_enrollment(self, agents_dir: Path):
        """A hired crewmate is already named by its owner; a later inline rename
        changes the label only -- the id, the binding, the private store and
        the enrollment record stay."""
        from kiro_crew.dashboard.handlers import api_kirocrew_agent_update

        app = _app()
        app.router.add_put("/api/agents/{name}", api_kirocrew_agent_update)
        async with TestClient(TestServer(app)) as client:
            resp = await client.post("/api/members", json=_hire("Checkout triage"))
            assert resp.status == 200, await resp.text()
            before = KiroCrewConfig.load().agents["Checkout-triage"]
            resp = await client.put(
                "/api/agents/Checkout-triage", json={"display_name": "Payments triage"}
            )
            assert resp.status == 200, await resp.text()
            roster = {
                r["name"]: r for r in (await (await client.get("/api/members")).json())["members"]
            }
        after = KiroCrewConfig.load().agents["Checkout-triage"]
        assert after.display_name == "Payments triage"
        assert after.kiro_agent == before.kiro_agent and after.memory_store == before.memory_store
        assert set(roster) == {"Checkout-triage"}
        assert roster["Checkout-triage"]["display_name"] == "Payments triage"
        assert (
            agent_state.get_crewmate_record("Checkout-triage")["generation"] == after.memory_store
        )

    @pytest.mark.asyncio
    async def test_a_typed_name_that_collides_is_still_a_409(self, agents_dir: Path):
        """Suffixing is for names nobody typed. A user who typed a taken name
        is told, the same contract the create has always had."""
        async with TestClient(TestServer(_app())) as client:
            assert (await client.post("/api/members", json=_hire("Judge"))).status == 200
            resp = await client.post("/api/members", json=_hire("Judge"))
            assert resp.status == 409
            assert (await resp.json())["code"] == "agent_exists"
            resp = await client.post("/api/members", json=_hire("Judge", named_by_user=False))
            # There is no client-side switch for the duplicate guard; a stray
            # field is ignored.
            assert resp.status == 409

    @pytest.mark.asyncio
    async def test_a_hire_colliding_with_a_session_agent_names_the_path(self, agents_dir: Path):
        """The upgrader's case: the empty roster says "hire one by name", they
        pick their existing agent and type its own name -- the id is taken by a
        row that is NOT a crewmate. The 409 says so (``existing.crewmate`` is
        false) and offers the one non-destructive remedy (another name), never
        deletion of the existing agent nor a memory migration this route does
        not do; the same collision with a real crewmate keeps the classic
        sentence and ``existing.crewmate`` true."""
        async with TestClient(TestServer(_app())) as client:
            plain = await client.post("/api/agents", json={"name": "Helper", "kiro_agent": SOURCE})
            assert plain.status == 200, await plain.text()
            resp = await client.post("/api/members", json=_hire("Helper"))
            assert resp.status == 409
            body = await resp.json()
            assert body["code"] == "agent_exists"
            assert body["existing"] == {"id": "Helper", "crewmate": False}
            assert "not a crewmate" in body["error"]
            assert "Choose another name" in body["error"]
            # The remedy is non-destructive: no deletion advice, no migration promise.
            assert "delete" not in body["error"].lower() and "move" not in body["error"].lower()
            assert "stay as they are" in body["error"]
            # A collision with an enrolled crewmate: the classic text, kind true.
            assert (await client.post("/api/members", json=_hire("Judge"))).status == 200
            resp = await client.post("/api/members", json=_hire("Judge"))
            assert resp.status == 409
            body = await resp.json()
            assert body["error"] == "Agent 'Judge' already exists"
            assert body["existing"] == {"id": "Judge", "crewmate": True}
            # The plain create's own collision is unchanged in text.
            resp = await client.post("/api/agents", json={"name": "Helper", "kiro_agent": SOURCE})
            assert resp.status == 409
            body = await resp.json()
            assert body["error"] == "Agent 'Helper' already exists"
            assert body["existing"]["crewmate"] is False

    @pytest.mark.asyncio
    async def test_the_hire_re_corroborates_its_copy_at_the_spawn_gate(
        self, agents_dir: Path, monkeypatch
    ):
        """A refresh pass that interleaved between the copy's lineage record and
        the row's persist saw a fork with no binding and recorded it as failed;
        the hire re-runs the pass once the row is on disk (as the fork endpoint
        does after its rebind), so the member is not blocked at the spawn gate."""
        import kiro_crew.agent as agent_mod

        # The interleave's outcome, seeded directly: the copy's name is already
        # in the failure set before the create answers.
        monkeypatch.setattr(agent_mod, "_fork_refresh_failed", frozenset({"Triage"}))
        assert "Triage" in agent_mod._fork_refresh_failed
        async with TestClient(TestServer(_app())) as client:
            resp = await client.post("/api/members", json=_hire("Triage"))
            assert resp.status == 200, await resp.text()
        assert "Triage" not in agent_mod._fork_refresh_failed
        agent_mod.require_fork_governance("Triage")


class TestHireValidation:
    @pytest.mark.asyncio
    async def test_a_hand_edited_non_string_binding_lists_with_no_lineage(self, agents_dir: Path):
        """The roster's lineage lookup keys the fork sidecar by the row's
        binding, which is free text in a hand-editable config: a row whose
        ``kiro_agent`` is a list must list (with no lineage), not turn
        ``GET /api/members`` into a 500 for every member."""
        from kiro_crew.config.loader import config_path

        async with TestClient(TestServer(_app())) as client:
            resp = await client.post("/api/members", json=_hire("Triage"))
            assert resp.status == 200, await resp.text()
            raw = json.loads(config_path().read_text())
            raw["agents"]["Broken"] = {"kiro_agent": [], "memory_store": "default"}
            config_path().write_text(json.dumps(raw))
            # A hand-written row is a session agent until enrolled; enrolled
            # here so the roster's lineage rendering of it is what is tested.
            agent_state.set_crewmate_record(
                "Broken", generation="default", template="", hired_at=""
            )
            resp = await client.get("/api/members")
            assert resp.status == 200, await resp.text()
            roster = {r["name"]: r for r in (await resp.json())["members"]}
        assert roster["Triage"]["template_origin"] == SOURCE
        assert roster["Broken"]["template_origin"] == ""

    @pytest.mark.asyncio
    @pytest.mark.parametrize(
        "body, code",
        [
            ({"display_name": "x"}, "invalid_source"),
            ({"display_name": "x", "source": "reviewer"}, "invalid_source"),
            (
                {"display_name": "x", "source": {"kind": "store", "agent": SOURCE}},
                "unsupported_source_kind",
            ),
            # Unhashable kinds: a 400, not a TypeError out of the frozenset test.
            (
                {"display_name": "x", "source": {"kind": ["local"], "agent": SOURCE}},
                "unsupported_source_kind",
            ),
            (
                {"display_name": "x", "source": {"kind": {"k": 1}, "agent": SOURCE}},
                "unsupported_source_kind",
            ),
            (
                {"display_name": "x", "source": {"kind": "local", "agent": "../etc"}},
                "invalid_source_agent",
            ),
            ({"display_name": "x", "source": {"kind": "local"}}, "invalid_source_agent"),
            (
                {"display_name": 3, "source": {"kind": "local", "agent": SOURCE}},
                "invalid_display_name",
            ),
            # The optional text fields must be text: a list would persist and
            # then break the member on use (thread open, route_crew).
            (
                {
                    "display_name": "x",
                    "workspace": [],
                    "source": {"kind": "local", "agent": SOURCE},
                },
                "invalid_workspace",
            ),
            (
                {
                    "display_name": "x",
                    "description": [],
                    "source": {"kind": "local", "agent": SOURCE},
                },
                "invalid_description",
            ),
            (
                {
                    "display_name": "x",
                    "triggers": {"a": 1},
                    "source": {"kind": "local", "agent": SOURCE},
                },
                "invalid_triggers",
            ),
            (
                {
                    "display_name": "x",
                    "session_color": 7,
                    "source": {"kind": "local", "agent": SOURCE},
                },
                "invalid_session_color",
            ),
        ],
    )
    async def test_refused_bodies_write_nothing(self, agents_dir: Path, body, code):
        before = sorted(p.name for p in agents_dir.iterdir())
        async with TestClient(TestServer(_app())) as client:
            resp = await client.post("/api/members", json=body)
            assert resp.status == 400, await resp.text()
            assert (await resp.json())["code"] == code
        assert sorted(p.name for p in agents_dir.iterdir()) == before
        assert set(KiroCrewConfig.load().agents) == {"default"}

    @pytest.mark.asyncio
    async def test_a_blank_display_name_is_refused_and_writes_nothing(self, agents_dir: Path):
        """``display_name: "  "`` is nobody's name: refused like a missing one,
        before anything is minted or written."""
        before = sorted(p.name for p in agents_dir.iterdir())
        async with TestClient(TestServer(_app())) as client:
            for blank in ("  ", "", "\t\n"):
                resp = await client.post(
                    "/api/members",
                    json={"display_name": blank, "source": {"kind": "local", "agent": SOURCE}},
                )
                assert resp.status == 400, await resp.text()
                assert (await resp.json())["code"] == "missing_display_name"
        assert sorted(p.name for p in agents_dir.iterdir()) == before
        assert set(KiroCrewConfig.load().agents) == {"default"}
        assert agent_state.all_crewmate_records() == {}

    @pytest.mark.asyncio
    async def test_unknown_source_file_is_404_before_anything_is_written(self, agents_dir: Path):
        """The create step accepts an unlisted template with a warning (an
        edition may resolve it); a hire promises a COPY of the file, so a name
        with no file behind it is refused up front and no row is created."""
        before = sorted(p.name for p in agents_dir.iterdir())
        async with TestClient(TestServer(_app())) as client:
            resp = await client.post(
                "/api/members",
                json={"display_name": "Ghost", "source": {"kind": "local", "agent": "no-such"}},
            )
            assert resp.status == 404
            data = await resp.json()
        assert data["code"] == "template_not_found"
        assert "rolled_back" not in data
        assert set(KiroCrewConfig.load().agents) == {"default"}
        assert sorted(p.name for p in agents_dir.iterdir()) == before

    @pytest.mark.asyncio
    async def test_non_owner_is_refused(self, agents_dir: Path, monkeypatch):
        monkeypatch.setattr(
            "kiro_crew.dashboard.handlers.source_providers.is_owner_dashboard_request",
            lambda request: False,
        )
        async with TestClient(TestServer(_app())) as client:
            resp = await client.post("/api/members", json=_hire("Triage"))
            assert resp.status in (401, 403)
        assert set(KiroCrewConfig.load().agents) == {"default"}
