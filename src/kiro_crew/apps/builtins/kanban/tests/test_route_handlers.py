"""Behavior of the Kanban HTTP handlers.

``test_routes.py`` next door asserts only that the routes are REGISTERED, which
leaves every handler body unexercised. These drive the handlers through a real
aiohttp server so the request parsing, the validation branches, and the
machine-readable error codes are covered by something that fails when they
change.

The store is injected on app state with a ``tmp_path`` root, so nothing here
touches the operator's board file.
"""

from __future__ import annotations

import asyncio
from types import SimpleNamespace
from typing import Any

import pytest
from aiohttp import web
from aiohttp.test_utils import TestClient, TestServer

from kiro_crew.apps.builtins.kanban.backend import routes
from kiro_crew.apps.builtins.kanban.backend.store import KanbanStore, create_task

BASE = "/api/apps/kanban"


@pytest.fixture(autouse=True)
def _app_enabled(monkeypatch):
    """Present the app as enabled so these tests exercise handlers, not the gate.

    The gate itself is covered by TestAppEnablementGate, which overrides this.
    Without it every handler would 403, since the app is not installed in a
    test's data home.
    """
    monkeypatch.setattr(routes, "is_app_enabled", lambda _name: True)


async def _client(tmp_path, *, sessions=None) -> tuple[TestClient, KanbanStore]:
    """A live server whose handlers resolve a tmp-rooted store.

    ``sessions`` is absent by default, which is what keeps the refine handler on
    its heuristic path instead of reaching for a model in most tests.
    """
    store = KanbanStore(tmp_path / "kanban")
    app = web.Application()
    # Handlers read request.app["state"] and cache the store on it; pre-seeding
    # the attribute is what keeps them off the real data home.
    state = SimpleNamespace(_kanban_store=store)
    if sessions is not None:
        state.sessions = sessions
    app["state"] = state
    routes.register_routes(app)
    client = TestClient(TestServer(app))
    await client.start_server()
    return client, store


def _must_get(store: KanbanStore, task_id: str):
    """Fetch a task that the test requires to exist."""
    task = store.get_task(task_id)
    assert task is not None, f"task {task_id} vanished from the store"
    return task


def _seed(store: KanbanStore, **kw) -> str:
    task = create_task(
        title=kw.get("title", "Seeded task"),
        description=kw.get("description", ""),
        prompt=kw.get("prompt", ""),
        status=kw.get("status", "todo"),
        tags=kw.get("tags"),
        priority=kw.get("priority", "medium"),
    )
    store.add_task(task)
    return task.id


# ── refine ──────────────────────────────────────────────────────────────────


class TestAppEnablementGate:
    """A disabled board must not be drivable by a direct authenticated request."""

    @pytest.mark.asyncio
    async def test_a_disabled_app_refuses_every_route(self, tmp_path, monkeypatch):
        monkeypatch.setattr(routes, "is_app_enabled", lambda _name: False)
        client, store = await _client(tmp_path)
        tid = _seed(store)
        for method, path in (
            ("get", f"{BASE}/tasks"),
            ("post", f"{BASE}/tasks"),
            ("patch", f"{BASE}/tasks/{tid}"),
            ("delete", f"{BASE}/tasks/{tid}"),
            ("post", f"{BASE}/tasks/{tid}/move"),
            ("post", f"{BASE}/tasks/{tid}/run"),
            ("post", f"{BASE}/reconcile"),
        ):
            res = await getattr(client, method)(path, json={"prompt": "x", "title": "x"})
            assert res.status == 403, f"{method} {path} was reachable while disabled"
            assert (await res.json())["code"] == "app_disabled"

    @pytest.mark.asyncio
    async def test_an_unreadable_enablement_state_denies(self, tmp_path, monkeypatch):
        """Deny-by-default: a state file that cannot be read closes the surface."""

        def boom(_name):
            raise OSError("installed.json unreadable")

        monkeypatch.setattr(routes, "is_app_enabled", boom)
        client, _ = await _client(tmp_path)
        res = await client.get(f"{BASE}/tasks")
        assert res.status == 403
        assert (await res.json())["code"] == "app_disabled"


# ── list ────────────────────────────────────────────────────────────────────


class TestList:
    @pytest.mark.asyncio
    async def test_empty_board_lists_nothing(self, tmp_path):
        client, _ = await _client(tmp_path)
        body = await (await client.get(f"{BASE}/tasks")).json()
        assert body == {"tasks": [], "total": 0}

    @pytest.mark.asyncio
    async def test_lists_every_task_with_a_total(self, tmp_path):
        client, store = await _client(tmp_path)
        _seed(store, title="one")
        _seed(store, title="two")
        body = await (await client.get(f"{BASE}/tasks")).json()
        assert body["total"] == 2
        assert {t["title"] for t in body["tasks"]} == {"one", "two"}


class TestCreate:
    @pytest.mark.asyncio
    async def test_creates_and_persists_a_task(self, tmp_path):
        client, store = await _client(tmp_path)
        res = await client.post(f"{BASE}/tasks", json={"title": "New task"})
        assert res.status == 201
        created = await res.json()
        assert created["title"] == "New task"
        assert store.get_task(created["id"]) is not None

    @pytest.mark.asyncio
    async def test_a_requested_running_status_is_coerced_to_todo(self, tmp_path):
        """Only the run path may put a card in Running.

        `running` means a live agent turn. Honouring it on create minted a card
        with no execution, which reconcile skips because there is nothing to
        grade -- so it sat in Running forever, in a lane the move endpoint's own
        `status_not_manually_settable` guard refuses to let anyone set.
        """
        client, store = await _client(tmp_path)
        res = await client.post(f"{BASE}/tasks", json={"title": "T", "status": "running"})
        assert res.status == 201
        created = await res.json()
        assert created["status"] == "todo"
        assert _must_get(store, created["id"]).status == "todo"

    @pytest.mark.asyncio
    @pytest.mark.parametrize("status", ["backlog", "todo", "done", "failed"])
    async def test_a_manually_settable_status_is_honoured(self, tmp_path, status):
        """The guard closes one lane; it must not close the other four."""
        client, _ = await _client(tmp_path)
        res = await client.post(f"{BASE}/tasks", json={"title": "T", "status": status})
        assert (await res.json())["status"] == status

    @pytest.mark.asyncio
    async def test_an_unknown_status_falls_back_to_todo(self, tmp_path):
        client, _ = await _client(tmp_path)
        res = await client.post(f"{BASE}/tasks", json={"title": "T", "status": "nonsense"})
        assert (await res.json())["status"] == "todo"

    @pytest.mark.asyncio
    @pytest.mark.parametrize(
        ("body", "code"),
        [
            ({"title": ["not", "a", "string"]}, "title_not_string"),
            ({"title": "T", "prompt": 7}, "prompt_not_string"),
            ({"title": "T", "description": {"a": 1}}, "description_not_string"),
            ({"title": "T", "tags": "a,b"}, "tags_not_string_list"),
            ({"title": "T", "tags": [1, 2]}, "tags_not_string_list"),
        ],
    )
    async def test_a_malformed_field_is_a_client_error_not_a_500(self, tmp_path, body, code):
        """Field parsing must stay inside the ``_BadRequest`` handler.

        These rejections are raised by ``_str_field`` / ``_tags_field`` rather than
        coerced, so parsing them outside the handler turned a client's bad shape
        into an HTTP 500 with a traceback in the gateway log.
        """
        client, _ = await _client(tmp_path)
        res = await client.post(f"{BASE}/tasks", json=body)
        assert res.status == 400
        assert (await res.json())["code"] == code

    @pytest.mark.asyncio
    async def test_optional_fields_are_carried_through(self, tmp_path):
        client, _ = await _client(tmp_path)
        res = await client.post(
            f"{BASE}/tasks",
            json={
                "title": "Full task",
                "description": "desc",
                "prompt": "do it",
                "status": "backlog",
                "tags": ["a", "b"],
                "priority": "high",
            },
        )
        created = await res.json()
        assert created["status"] == "backlog"
        assert created["tags"] == ["a", "b"]
        assert created["priority"] == "high"

    @pytest.mark.asyncio
    async def test_a_whitespace_only_title_is_refused(self, tmp_path):
        client, _ = await _client(tmp_path)
        res = await client.post(f"{BASE}/tasks", json={"title": "   "})
        assert res.status == 400
        assert (await res.json())["code"] == "title_required"

    @pytest.mark.asyncio
    async def test_malformed_body_is_refused_with_a_code(self, tmp_path):
        client, _ = await _client(tmp_path)
        res = await client.post(
            f"{BASE}/tasks", data="{", headers={"Content-Type": "application/json"}
        )
        assert res.status == 400
        assert (await res.json())["code"] == "invalid_json"


async def _drain_namers(client: TestClient) -> None:
    """Wait out the background naming jobs this app has in flight.

    The jobs remove themselves from the set as they finish, so gather over a
    snapshot rather than the live set.
    """
    jobs = list(client.app.get(routes._NAMER_JOBS_KEY) or ())
    if jobs:
        await asyncio.gather(*jobs)


class TestCreateNamesInTheBackground:
    """Creating from a bare prompt must not block on the naming model.

    The point of the split is latency: ``run_bg_oneliner`` spins up an ephemeral
    ``_bg`` session per call, and the user used to sit through that before their
    card appeared at all.
    """

    @pytest.mark.asyncio
    async def test_the_card_is_returned_before_the_model_answers(self, tmp_path, monkeypatch):
        """The strong form of the requirement: 201 arrives while the model is
        still thinking, not after."""
        release = asyncio.Event()

        async def slow_namer(sessions, prompt, **kwargs):
            await release.wait()
            return "TITLE: Named at last\nDESCRIPTION: Eventually."

        monkeypatch.setattr(routes, "run_bg_oneliner", slow_namer)
        client, store = await _client(tmp_path, sessions=object())

        res = await client.post(f"{BASE}/tasks", json={"prompt": "ship the thing"})
        assert res.status == 201
        created = await res.json()
        # Still unset: the handler answered without waiting on the model.
        assert not release.is_set()
        assert created["refining"] is True
        assert created["title"] == "ship the thing"  # provisional, from the prompt
        assert store.get_task(created["id"]) is not None

        release.set()
        await _drain_namers(client)
        named = _must_get(store, created["id"])
        assert named.title == "Named at last"
        assert named.description == "Eventually."
        assert named.refining is False

    @pytest.mark.asyncio
    async def test_cancelling_the_namer_still_clears_the_flag(self, tmp_path, monkeypatch):
        """Cancellation is the exit path that OUTLIVES the process.

        The gateway shutting down mid-naming cancels the job. Re-raising without
        clearing `refining` left the flag true ON DISK, so the card came back
        showing "Refining…" after a restart with no job left to ever clear it.
        """
        started = asyncio.Event()

        async def hangs(sessions, prompt, **kwargs):
            started.set()
            await asyncio.Event().wait()  # never resolves; only cancellation ends it
            return ""

        monkeypatch.setattr(routes, "run_bg_oneliner", hangs)
        client, store = await _client(tmp_path, sessions=object())

        created = await (await client.post(f"{BASE}/tasks", json={"prompt": "ship it"})).json()
        assert created["refining"] is True
        await asyncio.wait_for(started.wait(), timeout=5)

        # Cancel exactly the way gateway shutdown does, and let the job settle.
        jobs = set(client.app[routes._NAMER_JOBS_KEY])
        assert jobs, "the namer job should still be in flight"
        for job in jobs:
            job.cancel()
        await asyncio.gather(*jobs, return_exceptions=True)

        # On disk, not just in memory: the restart is what this protects.
        assert _must_get(store, created["id"]).refining is False
        assert _must_get(store, created["id"]).title == "ship it"  # provisional title kept

    @pytest.mark.asyncio
    async def test_a_cancelled_offload_falls_back_to_an_inline_write(self, tmp_path, monkeypatch):
        """The flag must reach disk even when the thread hop cannot run.

        Clearing `refining` is offloaded so a board is never rewritten on the event
        loop, but the loop being torn down can cancel that hop as well -- and a
        flag left true on disk outlives the process, where a brief inline write
        does not.
        """
        started = asyncio.Event()

        async def hangs(sessions, prompt, **kwargs):
            started.set()
            await asyncio.Event().wait()
            return ""

        monkeypatch.setattr(routes, "run_bg_oneliner", hangs)
        client, store = await _client(tmp_path, sessions=object())
        created = await (await client.post(f"{BASE}/tasks", json={"prompt": "ship it"})).json()
        assert created["refining"] is True
        await asyncio.wait_for(started.wait(), timeout=5)

        real_to_thread = routes.asyncio.to_thread
        refused: list[str] = []

        async def refuse_the_hop(fn, /, *args, **kwargs):
            # Stand in for a loop that is going away: the offload never lands.
            if getattr(fn, "__name__", "") == "update_task":
                refused.append("update_task")
                raise asyncio.CancelledError
            return await real_to_thread(fn, *args, **kwargs)

        monkeypatch.setattr(routes.asyncio, "to_thread", refuse_the_hop)
        jobs = set(client.app[routes._NAMER_JOBS_KEY])
        assert jobs, "the namer job should still be in flight"
        for job in jobs:
            job.cancel()
        await asyncio.gather(*jobs, return_exceptions=True)

        assert refused, "the offload must be attempted before the inline fallback"
        assert _must_get(store, created["id"]).refining is False

    @pytest.mark.asyncio
    async def test_a_naming_failure_clears_the_flag_and_keeps_the_provisional_title(
        self, tmp_path, monkeypatch
    ):
        """A card stuck showing "Refining…" forever is worse than a plain title."""

        async def boom(sessions, prompt, **kwargs):
            raise RuntimeError("no model")

        monkeypatch.setattr(routes, "run_bg_oneliner", boom)
        client, store = await _client(tmp_path, sessions=object())
        created = await (await client.post(f"{BASE}/tasks", json={"prompt": "do a thing"})).json()
        await _drain_namers(client)
        settled = _must_get(store, created["id"])
        assert settled.refining is False
        assert settled.title == "do a thing"

    @pytest.mark.asyncio
    async def test_an_explicit_title_is_taken_as_given_and_spawns_no_namer(self, tmp_path):
        client, _ = await _client(tmp_path)
        created = await (
            await client.post(f"{BASE}/tasks", json={"title": "I named it myself", "prompt": "p"})
        ).json()
        assert created["refining"] is False
        assert not (client.app.get(routes._NAMER_JOBS_KEY) or ())

    @pytest.mark.asyncio
    async def test_neither_title_nor_prompt_is_still_refused(self, tmp_path):
        client, _ = await _client(tmp_path)
        res = await client.post(f"{BASE}/tasks", json={"prompt": "   "})
        assert res.status == 400
        assert (await res.json())["code"] == "title_required"

    @pytest.mark.asyncio
    async def test_a_user_rename_outranks_the_namer(self, tmp_path, monkeypatch):
        """The user retitling the card while the model thinks must win — the late
        reply cannot overwrite what they typed."""
        release = asyncio.Event()

        async def slow_namer(sessions, prompt, **kwargs):
            await release.wait()
            return "TITLE: Model wins\nDESCRIPTION: Model description."

        monkeypatch.setattr(routes, "run_bg_oneliner", slow_namer)
        client, store = await _client(tmp_path, sessions=object())
        created = await (await client.post(f"{BASE}/tasks", json={"prompt": "raw prompt"})).json()

        patched = await client.patch(f"{BASE}/tasks/{created['id']}", json={"title": "Human wins"})
        assert patched.status == 200
        assert (await patched.json())["refining"] is False

        release.set()
        await _drain_namers(client)
        assert _must_get(store, created["id"]).title == "Human wins"

    @pytest.mark.asyncio
    async def test_a_card_deleted_while_naming_is_not_an_error(self, tmp_path, monkeypatch):
        release = asyncio.Event()

        async def slow_namer(sessions, prompt, **kwargs):
            await release.wait()
            return "TITLE: Too late\nDESCRIPTION: Gone."

        monkeypatch.setattr(routes, "run_bg_oneliner", slow_namer)
        client, store = await _client(tmp_path, sessions=object())
        created = await (await client.post(f"{BASE}/tasks", json={"prompt": "transient"})).json()
        assert (await client.delete(f"{BASE}/tasks/{created['id']}")).status == 200

        release.set()
        await _drain_namers(client)  # must not raise
        assert store.get_task(created["id"]) is None

    @pytest.mark.asyncio
    async def test_a_gateway_with_no_session_manager_still_creates_and_settles(self, tmp_path):
        """No model reachable at all: the heuristics name it and the flag clears."""
        client, store = await _client(tmp_path)  # no sessions on state
        created = await (await client.post(f"{BASE}/tasks", json={"prompt": "offline task"})).json()
        assert created["refining"] is True
        await _drain_namers(client)
        settled = _must_get(store, created["id"])
        assert settled.refining is False
        assert settled.title == "offline task"


# ── get / delete ────────────────────────────────────────────────────────────


class TestGetAndDelete:

    @pytest.mark.asyncio
    async def test_delete_removes_it_from_the_store(self, tmp_path):
        client, store = await _client(tmp_path)
        tid = _seed(store)
        res = await client.delete(f"{BASE}/tasks/{tid}")
        assert res.status == 200
        assert (await res.json()) == {"deleted": True}
        assert store.get_task(tid) is None

    @pytest.mark.asyncio
    async def test_delete_of_an_unknown_id_is_404_with_a_code(self, tmp_path):
        client, _ = await _client(tmp_path)
        res = await client.delete(f"{BASE}/tasks/nope")
        assert res.status == 404
        assert (await res.json())["code"] == "task_not_found"


# ── update ──────────────────────────────────────────────────────────────────


class TestUpdate:
    @pytest.mark.asyncio
    async def test_updates_the_named_fields_only(self, tmp_path):
        client, store = await _client(tmp_path)
        tid = _seed(store, title="before", description="keep me")
        body = await (await client.patch(f"{BASE}/tasks/{tid}", json={"title": "after"})).json()
        assert body["title"] == "after"
        assert body["description"] == "keep me"

    @pytest.mark.asyncio
    async def test_a_non_string_title_is_refused_not_silently_ignored(self, tmp_path):
        """A wrong-typed title is a client error, not a no-op.

        Ignoring it returned 200 with the old title, so a buggy caller was told
        its edit succeeded while nothing changed.
        """
        client, store = await _client(tmp_path)
        tid = _seed(store, title="original")
        resp = await client.patch(f"{BASE}/tasks/{tid}", json={"title": 123})
        assert resp.status == 400
        assert (await resp.json())["code"] == "title_not_string"
        assert _must_get(store, tid).title == "original"  # unchanged on disk

    @pytest.mark.asyncio
    async def test_a_blank_title_is_refused_so_the_card_survives(self, tmp_path):
        """Clearing the title used to delete the card and its history.

        An empty title is dropped as invalid when the board next loads, so the
        card and every execution behind it vanished on reload.
        """
        client, store = await _client(tmp_path)
        tid = _seed(store, title="original")
        resp = await client.patch(f"{BASE}/tasks/{tid}", json={"title": "   "})
        assert resp.status == 400
        assert (await resp.json())["code"] == "title_empty"
        assert _must_get(store, tid).title == "original"

    @pytest.mark.asyncio
    async def test_an_array_body_is_refused_with_a_code(self, tmp_path):
        """A non-object body reached .get() as a 500; it is a 400."""
        client, store = await _client(tmp_path)
        tid = _seed(store, title="original")
        resp = await client.patch(f"{BASE}/tasks/{tid}", json=[1, 2])
        assert resp.status == 400
        assert (await resp.json())["code"] == "body_not_object"

    @pytest.mark.asyncio
    async def test_a_non_string_tag_is_refused(self, tmp_path):
        client, store = await _client(tmp_path)
        tid = _seed(store, title="original")
        resp = await client.patch(f"{BASE}/tasks/{tid}", json={"tags": ["ok", 7]})
        assert resp.status == 400
        assert (await resp.json())["code"] == "tags_not_string_list"

    @pytest.mark.asyncio
    async def test_an_unknown_priority_is_ignored(self, tmp_path):
        client, store = await _client(tmp_path)
        tid = _seed(store, priority="medium")
        body = await (await client.patch(f"{BASE}/tasks/{tid}", json={"priority": "urgent"})).json()
        assert body["priority"] == "medium"

    @pytest.mark.asyncio
    async def test_update_of_an_unknown_id_is_404_with_a_code(self, tmp_path):
        client, _ = await _client(tmp_path)
        res = await client.patch(f"{BASE}/tasks/nope", json={"title": "x"})
        assert res.status == 404
        assert (await res.json())["code"] == "task_not_found"

    @pytest.mark.asyncio
    async def test_malformed_body_is_refused_with_a_code(self, tmp_path):
        client, store = await _client(tmp_path)
        tid = _seed(store)
        res = await client.patch(
            f"{BASE}/tasks/{tid}", data="{", headers={"Content-Type": "application/json"}
        )
        assert res.status == 400
        assert (await res.json())["code"] == "invalid_json"


# ── move ────────────────────────────────────────────────────────────────────


class TestMove:
    @pytest.mark.asyncio
    async def test_moves_between_manual_columns(self, tmp_path):
        client, store = await _client(tmp_path)
        tid = _seed(store, status="todo")
        body = await (await client.post(f"{BASE}/tasks/{tid}/move", json={"status": "done"})).json()
        assert body["status"] == "done"

    @pytest.mark.asyncio
    async def test_running_is_not_a_manually_settable_column(self, tmp_path):
        client, store = await _client(tmp_path)
        tid = _seed(store)
        res = await client.post(f"{BASE}/tasks/{tid}/move", json={"status": "running"})
        assert res.status == 400
        assert (await res.json())["code"] == "status_not_manually_settable"

    @pytest.mark.asyncio
    async def test_a_running_card_refuses_every_manual_move(self, tmp_path):
        """A run owns the lane until its watcher settles it.

        Accepting a manual Done wrote the lane while leaving the execution row
        unsettled, and reconcile only ever visits `running` cards -- so a process
        that died there left `result: null` on disk for good, and one that lived
        overwrote the user's move with the watcher's real verdict. The refusal
        names a code so the UI can say why.
        """
        client, store = await _client(tmp_path)
        tid = _running_with_execution(store, session_key="s1")
        for target in ("done", "failed", "todo", "backlog"):
            res = await client.post(f"{BASE}/tasks/{tid}/move", json={"status": target})
            assert res.status == 409, target
            assert (await res.json())["code"] == "task_is_running"
        task = _must_get(store, tid)
        assert task.status == "running"  # untouched on disk
        assert task.executions[-1].result is None  # and still owned by its run

    @pytest.mark.asyncio
    async def test_a_settled_card_still_moves(self, tmp_path):
        """The refusal is scoped to `running` -- a run that finished releases it."""
        client, store = await _client(tmp_path)
        tid = _running_with_execution(store, session_key="s1")
        await routes._settle_task(store, tid, _exec_id(store, tid), "succeeded")
        body = await (await client.post(f"{BASE}/tasks/{tid}/move", json={"status": "todo"})).json()
        assert body["status"] == "todo"

    @pytest.mark.asyncio
    async def test_a_missing_status_is_refused(self, tmp_path):
        client, store = await _client(tmp_path)
        tid = _seed(store)
        res = await client.post(f"{BASE}/tasks/{tid}/move", json={})
        assert res.status == 400
        assert (await res.json())["code"] == "status_not_manually_settable"

    @pytest.mark.asyncio
    async def test_move_of_an_unknown_id_is_404_with_a_code(self, tmp_path):
        client, _ = await _client(tmp_path)
        res = await client.post(f"{BASE}/tasks/nope/move", json={"status": "done"})
        assert res.status == 404
        assert (await res.json())["code"] == "task_not_found"

    @pytest.mark.asyncio
    async def test_malformed_body_is_refused_with_a_code(self, tmp_path):
        client, store = await _client(tmp_path)
        tid = _seed(store)
        res = await client.post(
            f"{BASE}/tasks/{tid}/move",
            data="{",
            headers={"Content-Type": "application/json"},
        )
        assert res.status == 400
        assert (await res.json())["code"] == "invalid_json"


# ── executions ──────────────────────────────────────────────────────────────


class TestModelTextRedaction:
    """Model-authored card text is untrusted and must be scrubbed.

    The naming model's reply is persisted to ``board.json`` and rendered verbatim
    on the card, so a credential it echoes back -- or an exfiltration URL carried
    in from the request text -- would land in the UI and on disk.
    """

    @pytest.mark.asyncio
    async def test_a_credential_in_the_model_reply_is_redacted(self, monkeypatch):
        async def _reply(*_a, **_k):
            return "TITLE: use token ghp_ABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789\nDESCRIPTION: ok"

        monkeypatch.setattr(routes, "run_bg_oneliner", _reply)
        title, _desc = await routes._name_intent(object(), "name this")
        assert "ghp_ABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789" not in title

    @pytest.mark.asyncio
    async def test_the_description_is_scrubbed_too_not_just_the_title(self, monkeypatch):
        """Both model-authored fields go through the redaction, not only the title."""

        async def _reply(*_a, **_k):
            return (
                "TITLE: report\n"
                "DESCRIPTION: authenticate with ghp_ABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789"
            )

        monkeypatch.setattr(routes, "run_bg_oneliner", _reply)
        _title, desc = await routes._name_intent(object(), "name this")
        assert "ghp_ABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789" not in desc

    @pytest.mark.asyncio
    async def test_redaction_leaves_ordinary_text_untouched(self, monkeypatch):
        async def _reply(*_a, **_k):
            return "TITLE: Add a retry to the uploader\nDESCRIPTION: Wrap the S3 put in a backoff."

        monkeypatch.setattr(routes, "run_bg_oneliner", _reply)
        title, desc = await routes._name_intent(object(), "name this")
        assert title == "Add a retry to the uploader"
        assert desc == "Wrap the S3 put in a backoff."


class TestRun:
    @pytest.mark.asyncio
    async def test_two_concurrent_runs_produce_one_execution(self, tmp_path, monkeypatch):
        """Only one of two simultaneous Run requests may claim the task.

        Both used to read the same non-running record, both dispatched a turn,
        and the loser's whole-record write discarded the winner's execution from
        the history. The claim is now made inside one locked update.
        """
        client, store = await _client(tmp_path)
        tid = _seed(store, title="race me", prompt="go")

        started: list[str] = []

        async def _slow_session(_a, _state, _task, execution_id, _prompt):
            # Yield so the two requests genuinely interleave around the claim.
            await asyncio.sleep(0.02)
            started.append(execution_id)
            return "sess-1"

        monkeypatch.setattr(routes, "_create_kanban_session", _slow_session)

        first, second = await asyncio.gather(
            client.post(f"{BASE}/tasks/{tid}/run"),
            client.post(f"{BASE}/tasks/{tid}/run"),
        )
        codes = sorted([first.status, second.status])
        assert codes == [202, 409]  # exactly one winner
        assert len(started) == 1  # only one turn was dispatched
        assert len(_must_get(store, tid).executions) == 1  # history not overwritten

    @pytest.mark.asyncio
    async def test_unknown_id_is_404_with_a_code(self, tmp_path):
        client, _ = await _client(tmp_path)
        res = await client.post(f"{BASE}/tasks/nope/run")
        assert res.status == 404
        assert (await res.json())["code"] == "task_not_found"

    @pytest.mark.asyncio
    async def test_an_already_running_task_is_refused_as_conflict(self, tmp_path):
        client, store = await _client(tmp_path)
        tid = _seed(store, status="running")
        res = await client.post(f"{BASE}/tasks/{tid}/run")
        assert res.status == 409
        assert (await res.json())["code"] == "task_already_running"

    @pytest.mark.asyncio
    async def test_a_failed_session_launch_settles_the_task_and_reports_a_code(
        self, tmp_path, monkeypatch
    ):
        client, store = await _client(tmp_path)
        tid = _seed(store, title="will fail", prompt="do the thing")

        async def _boom(*_a, **_k):
            raise RuntimeError("no session for you")

        monkeypatch.setattr(routes, "_create_kanban_session", _boom)
        res = await client.post(f"{BASE}/tasks/{tid}/run")
        assert res.status == 500
        assert (await res.json())["code"] == "execution_start_failed"
        # The task must not be left stuck in `running`.
        assert _must_get(store, tid).status == "failed"

    @pytest.mark.asyncio
    async def test_a_successful_launch_reports_the_session_key(self, tmp_path, monkeypatch):
        client, store = await _client(tmp_path)
        tid = _seed(store, title="runnable", prompt="do the thing")

        async def _ok(*_a, **_k):
            return "sess-123"

        monkeypatch.setattr(routes, "_create_kanban_session", _ok)
        res = await client.post(f"{BASE}/tasks/{tid}/run")
        assert res.status == 202
        body = await res.json()
        assert body["session_key"] == "sess-123"
        assert body["status"] == "running"


# ── reconcile ───────────────────────────────────────────────────────────────


async def _client_with_slots(tmp_path, slots) -> tuple[TestClient, KanbanStore]:
    store = KanbanStore(tmp_path / "kanban")
    app = web.Application()
    app["state"] = SimpleNamespace(_kanban_store=store, _slots=slots)
    routes.register_routes(app)
    client = TestClient(TestServer(app))
    await client.start_server()
    return client, store


def _finished_task(exc: BaseException | None = None) -> asyncio.Future:
    """A completed Future standing in for a slot's finished turn.

    The real slot exposes its terminal state only through the asyncio Task that
    ``running`` is derived from, so a fake that carries an ad-hoc error attribute
    would assert against a shape production never produces.
    """
    fut: asyncio.Future = asyncio.Future()
    if exc is not None:
        fut.set_exception(exc)
    else:
        fut.set_result(None)
    return fut


def _running_with_execution(
    store: KanbanStore, *, session_key=None, result=None, age_secs: float = 0.0
) -> str:
    """Seed a task parked in `running` with one execution.

    ``age_secs`` backdates the execution's launch, which is what reconcile reads
    to tell a run whose session is still being created from an abandoned row.
    """
    from dataclasses import replace

    from kiro_crew.apps.builtins.kanban.backend.store import (
        attach_session_key,
        settle_execution,
        start_execution,
    )

    tid = _seed(store, title="running task")
    task = _must_get(store, tid)
    new_task, execution = start_execution(task)
    if age_secs:
        aged = [
            replace(ex, started_at=ex.started_at - age_secs) if ex.id == execution.id else ex
            for ex in new_task.executions
        ]
        new_task = replace(new_task, executions=aged)
    if session_key:
        new_task = attach_session_key(new_task, execution.id, session_key)
    if result:
        new_task = settle_execution(new_task, execution.id, result)
        # Force the status back to running so reconcile sees the inconsistency.
        new_task = replace(new_task, status="running")
    store.update_task(tid, lambda _: new_task)
    return tid


class TestReconcile:
    @pytest.mark.asyncio
    async def test_an_empty_board_reconciles_nothing(self, tmp_path):
        client, _ = await _client_with_slots(tmp_path, {})
        body = await (await client.post(f"{BASE}/reconcile")).json()
        assert body == {"reconciled": 0, "running": 0}

    @pytest.mark.asyncio
    async def test_a_task_with_no_execution_is_left_alone(self, tmp_path):
        client, store = await _client_with_slots(tmp_path, {})
        _seed(store, status="running")  # running but never executed
        body = await (await client.post(f"{BASE}/reconcile")).json()
        assert body["reconciled"] == 0
        assert body["running"] == 1

    @pytest.mark.asyncio
    async def test_an_abandoned_execution_that_never_got_a_session_is_cancelled(self, tmp_path):
        client, store = await _client_with_slots(tmp_path, {})
        tid = _running_with_execution(store, age_secs=routes._SESSION_ATTACH_GRACE_SECS + 1)
        body = await (await client.post(f"{BASE}/reconcile")).json()
        assert body["reconciled"] == 1
        # A cancelled run means the work is still outstanding, so it returns to todo.
        assert _must_get(store, tid).status == "todo"

    @pytest.mark.asyncio
    async def test_a_run_whose_session_is_still_being_created_is_left_alone(self, tmp_path):
        """Reconcile must not cancel the run that is starting as it looks.

        The execution row is written and the card flipped to `running` BEFORE the
        session exists, so a reconcile landing in that window saw a sessionless
        execution and settled a live run as cancelled -- the card dropped back to
        To Do while its agent turn kept going.
        """
        client, store = await _client_with_slots(tmp_path, {})
        tid = _running_with_execution(store)  # launched just now, key not attached yet
        body = await (await client.post(f"{BASE}/reconcile")).json()
        assert body == {"reconciled": 0, "running": 1}
        task = _must_get(store, tid)
        assert task.status == "running"
        assert task.executions[-1].result is None

    @pytest.mark.asyncio
    async def test_a_vanished_slot_is_cancelled(self, tmp_path):
        client, store = await _client_with_slots(tmp_path, {})
        tid = _running_with_execution(store, session_key="gone")
        body = await (await client.post(f"{BASE}/reconcile")).json()
        assert body["reconciled"] == 1
        assert _must_get(store, tid).status == "todo"

    @pytest.mark.asyncio
    async def test_a_finished_slot_with_no_error_settles_as_succeeded(self, tmp_path):
        slot = SimpleNamespace(running=False, task=_finished_task())
        client, store = await _client_with_slots(tmp_path, {"s1": slot})
        tid = _running_with_execution(store, session_key="s1")
        body = await (await client.post(f"{BASE}/reconcile")).json()
        assert body["reconciled"] == 1
        assert _must_get(store, tid).status == "done"

    @pytest.mark.asyncio
    async def test_a_finished_slot_carrying_an_error_settles_as_failed(self, tmp_path):
        slot = SimpleNamespace(running=False, task=_finished_task(RuntimeError("it exploded")))
        client, store = await _client_with_slots(tmp_path, {"s1": slot})
        tid = _running_with_execution(store, session_key="s1")
        body = await (await client.post(f"{BASE}/reconcile")).json()
        assert body["reconciled"] == 1
        assert _must_get(store, tid).status == "failed"

    @pytest.mark.asyncio
    async def test_a_cancelled_turn_settles_as_cancelled(self, tmp_path):
        """A stopped turn is not a successful one."""
        fut: asyncio.Future = asyncio.Future()
        fut.cancel()
        slot = SimpleNamespace(running=False, task=fut)
        client, store = await _client_with_slots(tmp_path, {"s1": slot})
        tid = _running_with_execution(store, session_key="s1")
        assert (await (await client.post(f"{BASE}/reconcile")).json())["reconciled"] == 1
        # A cancelled execution returns the card to To Do rather than marking it done.
        assert _must_get(store, tid).status == "todo"

    @pytest.mark.asyncio
    async def test_a_slot_exposing_no_error_attribute_still_reports_failure(self, tmp_path):
        """A real slot defines no error attribute, so failure must come from the task."""
        slot = SimpleNamespace(running=False, task=_finished_task(RuntimeError("boom")))
        client, store = await _client_with_slots(tmp_path, {"s1": slot})
        tid = _running_with_execution(store, session_key="s1")
        await client.post(f"{BASE}/reconcile")
        assert _must_get(store, tid).status == "failed"

    @pytest.mark.asyncio
    async def test_a_still_running_slot_is_left_running(self, tmp_path):
        slot = SimpleNamespace(running=True, task=None)
        client, store = await _client_with_slots(tmp_path, {"s1": slot})
        tid = _running_with_execution(store, session_key="s1")
        body = await (await client.post(f"{BASE}/reconcile")).json()
        assert body["reconciled"] == 0
        assert body["running"] == 1
        assert _must_get(store, tid).status == "running"


# ── watching an execution ───────────────────────────────────────────────────


def _exec_id(store: KanbanStore, task_id: str) -> str:
    """The id of the task's only execution."""
    task = _must_get(store, task_id)
    assert task.executions, "task has no execution to watch"
    return task.executions[-1].id


def _fake_state(store: KanbanStore, slots: dict | None = None) -> Any:
    """A stand-in for DashboardState carrying only what the watcher reads.

    Typed ``Any`` on purpose: the watcher duck-types ``_kanban_store`` and
    ``_slots`` off state, and constructing a real DashboardState here would drag
    a gateway's worth of setup into a unit test.
    """
    return SimpleNamespace(_kanban_store=store, _slots=slots if slots is not None else {})


class TestWatchExecution:
    """Settling a card from the turn's own Task handle.

    The regression this guards: an agent turn that answers in ~2 seconds ends
    before the watcher's first look, and the slot clears ``task`` when a turn
    ends. Polling the slot therefore saw "no task" and recorded a SUCCESSFUL run
    as cancelled — the card bounced back to To Do with a completed transcript
    sitting behind it. Holding the handle removes the window entirely.
    """

    @pytest.mark.asyncio
    async def test_a_turn_that_already_finished_settles_as_done(self, tmp_path):
        store = KanbanStore(tmp_path / "kanban")
        state = _fake_state(store)
        tid = _running_with_execution(store, session_key="s1")
        # Already-completed handle: this is the fast-turn case.
        await routes._watch_execution(state, tid, _exec_id(store, tid), "s1", _finished_task())
        assert _must_get(store, tid).status == "done"

    @pytest.mark.asyncio
    async def test_a_turn_that_raised_settles_as_failed_with_the_reason(self, tmp_path):
        store = KanbanStore(tmp_path / "kanban")
        state = _fake_state(store)
        tid = _running_with_execution(store, session_key="s1")
        await routes._watch_execution(
            state, tid, _exec_id(store, tid), "s1", _finished_task(RuntimeError("boom"))
        )
        task = _must_get(store, tid)
        assert task.status == "failed"
        assert "boom" in (task.executions[-1].error or "")

    @pytest.mark.asyncio
    async def test_a_cancelled_turn_settles_as_cancelled(self, tmp_path):
        store = KanbanStore(tmp_path / "kanban")
        state = _fake_state(store)
        tid = _running_with_execution(store, session_key="s1")
        fut: asyncio.Future = asyncio.Future()
        fut.cancel()
        await routes._watch_execution(state, tid, _exec_id(store, tid), "s1", fut)
        assert _must_get(store, tid).status == "todo"  # cancelled returns it to To Do

    @pytest.mark.asyncio
    async def test_a_turn_still_in_flight_is_awaited_not_settled_early(self, tmp_path):
        """The watcher must not settle a card while its turn is still running."""
        store = KanbanStore(tmp_path / "kanban")
        state = _fake_state(store)
        tid = _running_with_execution(store, session_key="s1")

        async def slow_turn() -> None:
            await asyncio.sleep(0.05)

        turn = asyncio.create_task(slow_turn())
        watcher = asyncio.create_task(
            routes._watch_execution(state, tid, _exec_id(store, tid), "s1", turn)
        )
        await asyncio.sleep(0.01)
        assert _must_get(store, tid).status == "running"  # still in flight
        await watcher
        assert _must_get(store, tid).status == "done"

    @pytest.mark.asyncio
    async def test_a_provider_failure_rendered_into_the_chat_settles_as_failed(self, tmp_path):
        """A failed turn must not be filed as Done.

        ``_run_chat`` renders a provider failure or a refused tool into the
        conversation as an ``error`` row and then returns NORMALLY, so the turn's
        asyncio Task completes cleanly. Classifying from the Task alone reported
        "succeeded" and moved the card to Done for a run the user can see failed;
        the outcome has to come from what the turn RECORDED.
        """
        store = KanbanStore(tmp_path / "kanban")
        slot = SimpleNamespace(
            total_messages=2,
            messages=[
                {"role": "user", "content": "go"},
                {"role": "error", "content": "provider 503: upstream unavailable"},
            ],
            _stop_generation=0,
            task=None,
        )
        state = _fake_state(store, slots={"s1": slot})
        tid = _running_with_execution(store, session_key="s1")
        # Baseline 1: the error row arrived after dispatch. The handle is clean.
        await routes._watch_execution(
            state,
            tid,
            _exec_id(store, tid),
            "s1",
            _finished_task(),
            baseline_total=1,
            stop_gen=0,
        )
        task = _must_get(store, tid)
        assert task.status == "failed"
        assert "503" in (task.executions[-1].error or "")

    @pytest.mark.asyncio
    async def test_an_error_predating_the_turn_is_not_blamed_on_it(self, tmp_path):
        """Only rows appended AFTER dispatch belong to this turn."""
        store = KanbanStore(tmp_path / "kanban")
        slot = SimpleNamespace(
            total_messages=2,
            messages=[
                {"role": "error", "content": "an older, unrelated failure"},
                {"role": "assistant", "content": "done"},
            ],
            _stop_generation=0,
            task=None,
        )
        state = _fake_state(store, slots={"s1": slot})
        tid = _running_with_execution(store, session_key="s1")
        # Baseline equals total_messages: nothing was appended since dispatch.
        await routes._watch_execution(
            state,
            tid,
            _exec_id(store, tid),
            "s1",
            _finished_task(),
            baseline_total=2,
            stop_gen=0,
        )
        assert _must_get(store, tid).status == "done"

    @pytest.mark.asyncio
    async def test_a_user_stop_settles_as_cancelled_not_done(self, tmp_path):
        """A Stop bumps ``_stop_generation``; that outranks a clean handle."""
        store = KanbanStore(tmp_path / "kanban")
        slot = SimpleNamespace(
            total_messages=1,
            messages=[{"role": "user", "content": "go"}],
            _stop_generation=4,  # was 3 at dispatch
            task=None,
        )
        state = _fake_state(store, slots={"s1": slot})
        tid = _running_with_execution(store, session_key="s1")
        await routes._watch_execution(
            state,
            tid,
            _exec_id(store, tid),
            "s1",
            _finished_task(),
            baseline_total=1,
            stop_gen=3,
        )
        assert _must_get(store, tid).status == "todo"  # cancelled returns it to To Do

    @pytest.mark.asyncio
    async def test_a_timed_out_turn_is_cancelled_not_left_running(self, tmp_path, monkeypatch):
        """A card reading Failed must not have a live turn still working behind it.

        The watcher previously awaited a ``shield``ed turn, so the 30-minute cap
        marked the card Failed while the agent kept going invisibly.
        """
        monkeypatch.setattr(routes, "_WATCH_TIMEOUT_SECS", 0.01)
        store = KanbanStore(tmp_path / "kanban")
        state = _fake_state(store)
        tid = _running_with_execution(store, session_key="s1")

        async def endless() -> None:
            await asyncio.sleep(30)

        turn = asyncio.create_task(endless())
        await routes._watch_execution(state, tid, _exec_id(store, tid), "s1", turn)
        assert _must_get(store, tid).status == "failed"
        await asyncio.sleep(0)
        assert turn.cancelled() or turn.done()  # the turn was stopped, not abandoned

    @pytest.mark.asyncio
    async def test_a_queued_prompt_with_no_handle_falls_back_to_the_slot(
        self, tmp_path, monkeypatch
    ):
        """A prompt that rode in behind another turn owns no handle, so the slot
        is the only signal — that path must keep working."""
        monkeypatch.setattr(routes.asyncio, "sleep", _no_sleep)
        store = KanbanStore(tmp_path / "kanban")
        slot = SimpleNamespace(running=False, task=_finished_task())
        state = _fake_state(store, {"s1": slot})
        tid = _running_with_execution(store, session_key="s1")
        await routes._watch_execution(state, tid, _exec_id(store, tid), "s1")
        assert _must_get(store, tid).status == "done"

    @pytest.mark.asyncio
    async def test_a_recovery_notice_is_progress_not_a_verdict(self, tmp_path):
        """A recovered turn's notice must not settle the card.

        The runner recovers a stalled turn by appending an ``error`` row that reads
        as progress ("⟳ Recovering a stalled turn…") and re-dispatching the work as
        a NEW turn on the same slot, with no user message. The recovering turn's
        own coroutine returns normally, so settling on that row files a Failed card
        while the agent goes on to finish the job. The watcher follows the
        successor and judges it on its own record instead.
        """
        store = KanbanStore(tmp_path / "kanban")
        slot = SimpleNamespace(
            total_messages=2,
            messages=[
                {"role": "user", "content": "go"},
                {"role": "error", "content": "⟳ Recovering a stalled turn…"},
            ],
            _stop_generation=0,
            task=None,
        )

        async def successor() -> None:
            slot.messages.append({"role": "assistant", "content": "finished the work"})
            slot.total_messages = 3

        slot.task = asyncio.create_task(successor())
        state = _fake_state(store, slots={"s1": slot})
        tid = _running_with_execution(store, session_key="s1")
        await routes._watch_execution(
            state,
            tid,
            _exec_id(store, tid),
            "s1",
            _finished_task(),
            baseline_total=1,
            stop_gen=0,
        )
        task = _must_get(store, tid)
        assert task.status == "done"
        assert task.executions[-1].result == "succeeded"

    @pytest.mark.asyncio
    async def test_an_exhausted_recovery_budget_still_settles_as_failed(self, tmp_path):
        """Following a successor must not swallow an unrecoverable slot.

        Each recovery path stops queueing a continuation once its retry budget is
        spent, so there is no successor to follow — ``slot.task`` still holds the
        turn we awaited — and the terminal notice is the verdict.
        """
        store = KanbanStore(tmp_path / "kanban")
        turn = _finished_task()
        slot = SimpleNamespace(
            total_messages=2,
            messages=[
                {"role": "user", "content": "go"},
                {"role": "error", "content": "Session stuck — please start a new chat."},
            ],
            _stop_generation=0,
            task=turn,
        )
        state = _fake_state(store, slots={"s1": slot})
        tid = _running_with_execution(store, session_key="s1")
        await routes._watch_execution(
            state, tid, _exec_id(store, tid), "s1", turn, baseline_total=1, stop_gen=0
        )
        task = _must_get(store, tid)
        assert task.status == "failed"
        assert "start a new chat" in (task.executions[-1].error or "")

    @pytest.mark.asyncio
    async def test_a_denied_approval_the_agent_worked_around_is_not_a_failure(self, tmp_path):
        """An `error` row the turn SURVIVED must not outrank the answer it gave.

        A kanban run is unattended, so it dispatches under the deny-fast approval
        window: a tool nobody decides on is auto-denied and rendered as an `error`
        row mid-turn. The agent then adapts and finishes, all inside the SAME turn
        -- there is no successor to follow -- so position is the discriminator. The
        runner appends `[partial] [notice] [continued answer]`, so an `error` row
        with an answer after it was survived, not fatal.
        """
        store = KanbanStore(tmp_path / "kanban")
        slot = SimpleNamespace(
            total_messages=4,
            messages=[
                {"role": "user", "content": "go"},
                {"role": "error", "content": "Tool `execute_bash` was not approved in time."},
                {"role": "assistant", "content": "Took the read-only route instead; done."},
            ],
            _stop_generation=0,
            task=None,
        )
        state = _fake_state(store, slots={"s1": slot})
        tid = _running_with_execution(store, session_key="s1")
        await routes._watch_execution(
            state,
            tid,
            _exec_id(store, tid),
            "s1",
            _finished_task(),
            baseline_total=1,
            stop_gen=0,
        )
        task = _must_get(store, tid)
        assert task.status == "done"
        assert task.executions[-1].result == "succeeded"

    @pytest.mark.asyncio
    async def test_a_failure_after_partial_output_is_still_a_failure(self, tmp_path):
        """Position must not become "any answer wins".

        A turn that streams part of an answer and THEN dies has its partial
        persisted first and the error row last -- the same shape as a clean
        failure, and still terminal.
        """
        store = KanbanStore(tmp_path / "kanban")
        slot = SimpleNamespace(
            total_messages=4,
            messages=[
                {"role": "user", "content": "go"},
                {"role": "assistant", "content": "Starting on it…"},
                {"role": "error", "content": "provider 503: upstream unavailable"},
            ],
            _stop_generation=0,
            task=None,
        )
        state = _fake_state(store, slots={"s1": slot})
        tid = _running_with_execution(store, session_key="s1")
        await routes._watch_execution(
            state,
            tid,
            _exec_id(store, tid),
            "s1",
            _finished_task(),
            baseline_total=1,
            stop_gen=0,
        )
        task = _must_get(store, tid)
        assert task.status == "failed"
        assert "503" in (task.executions[-1].error or "")


async def _no_sleep(_secs: float) -> None:
    """Collapse the fallback path's polling delay so the test is not slow."""
    return None


class _DispatchSlot:
    """A chat slot recording how a kanban turn was dispatched."""

    def __init__(self, key: str, *, running: bool = False) -> None:
        self.key = key
        self.title = ""
        self.running = running
        self.total_messages = 0
        self._stop_generation = 0
        self.task: Any = None
        self.appended: list[tuple[str, str]] = []
        self.dispatched: list[str] = []

    def enqueue_or_run_prompt(self, prompt: str, run_chat_coro: Any, state: Any) -> bool:
        # Mirrors the real method's contract closely enough for these assertions:
        # it records WHICH callable the caller handed it, which is the whole point
        # — `_run_chat` passed directly bypasses the background-turn cap.
        self.dispatched.append(getattr(run_chat_coro, "__name__", repr(run_chat_coro)))
        coro = run_chat_coro(state, self, prompt)
        self.task = asyncio.get_event_loop().create_task(coro)
        return True

    def append(self, kind: str, text: str, _css: str = "") -> None:
        self.appended.append((kind, text))


class _DispatchState:
    """A DashboardState stand-in that records app-ownership and cap charges."""

    def __init__(self, store: KanbanStore, slot: _DispatchSlot) -> None:
        self._kanban_store = store
        self._slots: dict[str, Any] = {}
        self._slot = slot
        self.created_with_app: list[str] = []
        self.capped: list[str] = []
        self.permit_timeout = False

    def get_or_create_slot(self, name: str = "", app: str = "", **_kw: Any) -> _DispatchSlot:
        self.created_with_app.append(app)
        self._slot.key = name
        self._slots[name] = self._slot
        return self._slot

    async def run_background_turn(self, slot: Any, coro: Any) -> Any:
        self.capped.append(str(getattr(slot, "key", "")))
        if self.permit_timeout:
            coro.close()
            raise TimeoutError("queued behind the background-turn cap")
        return await coro

    def push_slots_update(self) -> None:
        pass


def _dispatch_state(store: KanbanStore, slot: _DispatchSlot) -> Any:
    """A ``_DispatchState`` handed over as ``Any``, like ``_fake_state``.

    ``_create_kanban_session`` is annotated for a real ``DashboardState``, and
    building one here would drag a gateway's worth of setup into a unit test. The
    duck-typed surface it actually touches is four attributes wide.
    """
    return _DispatchState(store, slot)


class TestDispatchGoesThroughTheAppOwnedCap:
    """Kanban turns must run under the app's own execution controls.

    ``_ChatSlot.unattended`` is decided by app-OWNERSHIP, so a slot created
    without ``app=`` silently opts every card's turn out of both the
    background-turn cap and the deny-fast approval window — and a board can put
    five cards on the runtime at once, so the cap's counters would report the
    truth about fewer turns than were really running.
    """

    @pytest.mark.asyncio
    async def test_the_slot_is_created_app_owned(self, tmp_path, monkeypatch):
        store = KanbanStore(tmp_path / "kanban")
        slot = _DispatchSlot("kanban-abc")
        state = _dispatch_state(store, slot)
        monkeypatch.setattr(routes, "_run_chat", _noop_run_chat)
        task = create_task(title="T", prompt="do it")

        await routes._create_kanban_session(None, state, task, "e1", "do it")

        assert state.created_with_app == [routes.APP_NAME]

    @pytest.mark.asyncio
    async def test_two_ids_sharing_a_prefix_get_their_own_slots(self, tmp_path, monkeypatch):
        """The slot name carries the FULL task id, so cards cannot share a session.

        A truncated id collides between two valid tasks with the same leading
        characters, and the collision hands them ONE slot: the second run either
        lands in the first card's transcript or is refused as already-running.
        """
        store = KanbanStore(tmp_path / "kanban")
        state = _dispatch_state(store, _DispatchSlot("seed"))
        monkeypatch.setattr(routes, "_run_chat", _noop_run_chat)
        shared = "dead0beef"
        first = create_task(title="A", prompt="a")
        second = create_task(title="B", prompt="b")
        first.id = f"{shared}-1111-2222-3333-444444444444"
        second.id = f"{shared}-5555-6666-7777-888888888888"

        await routes._create_kanban_session(None, state, first, "e1", "a")
        await routes._create_kanban_session(None, state, second, "e2", "b")

        names = list(state._slots)
        assert len(names) == 2, names
        assert names == [f"kanban-{first.id}", f"kanban-{second.id}"]

    @pytest.mark.asyncio
    async def test_the_turn_is_charged_against_the_background_cap(self, tmp_path, monkeypatch):
        store = KanbanStore(tmp_path / "kanban")
        slot = _DispatchSlot("kanban-abc")
        state = _dispatch_state(store, slot)
        monkeypatch.setattr(routes, "_run_chat", _noop_run_chat)
        task = create_task(title="T", prompt="do it")

        await routes._create_kanban_session(None, state, task, "e1", "do it")
        if slot.task is not None:
            await slot.task

        # The wrapper, not `_run_chat` itself, is what reaches the slot.
        assert slot.dispatched == ["_capped_run_chat"]
        assert state.capped == [slot.key]

    @pytest.mark.asyncio
    async def test_a_turn_denied_a_permit_says_so_in_its_own_session(self, tmp_path, monkeypatch):
        store = KanbanStore(tmp_path / "kanban")
        slot = _DispatchSlot("kanban-abc")
        state = _dispatch_state(store, slot)
        state.permit_timeout = True
        monkeypatch.setattr(routes, "_run_chat", _noop_run_chat)
        task = create_task(title="T", prompt="do it")

        await routes._create_kanban_session(None, state, task, "e1", "do it")
        if slot.task is not None:
            await slot.task

        # A refused turn and a finished one must not look the same from outside.
        assert [k for k, _ in slot.appended] == ["error"]


class TestBusySlotIsRefusedNotQueued:
    """A second run must not be graded by the turn already in flight.

    The baselines are snapshotted for the turn the call starts, so queueing
    behind an active turn left the watcher grading THAT turn: its error or Stop
    settled this execution, recording an outcome that belonged to different work.
    """

    @pytest.mark.asyncio
    async def test_dispatch_refuses_while_the_reused_slot_is_busy(self, tmp_path, monkeypatch):
        store = KanbanStore(tmp_path / "kanban")
        slot = _DispatchSlot("kanban-abc", running=True)
        state = _dispatch_state(store, slot)
        monkeypatch.setattr(routes, "_run_chat", _noop_run_chat)
        task = create_task(title="T", prompt="do it")

        with pytest.raises(RuntimeError, match="already running"):
            await routes._create_kanban_session(None, state, task, "e2", "do it again")

        assert slot.dispatched == [], "nothing may be queued behind the active turn"

    @pytest.mark.asyncio
    async def test_a_refused_run_settles_the_card_as_failed(self, tmp_path, monkeypatch):
        """The refusal must not leak a card stuck in ``running``.

        ``_create_kanban_session`` raising routes into the run handler's existing
        failure path, which settles the execution — the alternative (returning
        ``None``) would leave an unsettled execution and a permanently running card.
        """
        client, store = await _client(tmp_path)
        created = await (await client.post(f"{BASE}/tasks", json={"title": "T"})).json()
        tid = created["id"]

        async def _busy(*_a, **_k):
            raise RuntimeError("this task's session is already running a turn")

        monkeypatch.setattr(routes, "_create_kanban_session", _busy)
        res = await client.post(f"{BASE}/tasks/{tid}/run")

        assert res.status == 500
        task = store.get_task(tid)
        assert task is not None
        assert task.status != "running", "a refused run must not leave the card running"
        assert task.executions[-1].result == "failed"


async def _noop_run_chat(_state: Any, _slot: Any, _prompt: str) -> None:
    """Stand-in for the real turn: the dispatch path is what these tests pin."""
    return None
