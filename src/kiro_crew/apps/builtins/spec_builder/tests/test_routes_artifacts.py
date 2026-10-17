"""Route tests: the user's direct authority over a spec's artifacts.

Document review through the redacted rendering, approval recorded against the
version approved, running one task at a time addressed by position and text,
relabelling and archiving, and the guards these mutations share: a complete,
current client identity, authentication, no index write from a stale snapshot,
and document reads off the event loop. An app token cannot post a message as the
user.
"""

from __future__ import annotations

import asyncio
import inspect
import json
import re
import time

import pytest
from aiohttp import web
from aiohttp.test_utils import TestClient, TestServer

from kiro_crew.apps.builtins.spec_builder.tests.routes_facade import routes

# _never_touch_the_real_state is an autouse fixture: pytest finds it by the name bound here.
from kiro_crew.apps.builtins.spec_builder.tests.routes_helpers import (  # noqa: F401
    _BASE,
    _OWNER_USER,
    _make_client,
    _never_touch_the_real_state,
    _redirect_state,
    _seed_spec,
    _spec_identity,
    _state_for,
)

# ── handlers offload filesystem work; delete tears down the slot ─────────────


def test_collect_spec_documents_returns_the_detail_bundle(tmp_path):
    """The bundled collector produces phase, docs, state and task metadata."""

    spec_dir = tmp_path / "spec"
    spec_dir.mkdir()
    (spec_dir / "requirements.md").write_text("# reqs")
    (spec_dir / "design.md").write_text("# design")
    (spec_dir / ".spec-state.json").write_text(json.dumps({"blocking": "waiting on you"}))

    phase, files, state, meta = routes._collect_spec_documents(spec_dir)
    assert phase == "design"  # newest present phase file wins
    assert files["requirements.md"] == "# reqs"
    assert state is not None and state["blocking"] == "waiting on you"
    # The hash is of the file AS STORED because it binds a phase approval to the
    # exact document reviewed.
    assert meta["docs"]["requirements.md"]["hash"] == routes._sha256_text("# reqs")
    assert set(meta["docs"]["requirements.md"]) == {"hash"}
    # No tasks.md here, so the list is empty rather than absent.
    assert meta["tasks"] == [] and meta["task_progress"] == {"done": 0, "total": 0}


# ── direct authority over the artifacts ──────────────────────────────────────
# Before these endpoints the user could only ASK the agent to change anything:
# a typo cost a model turn, an approval was a chat message that left no trace,
# and the task list could only be handed over whole. Each test below pins the
# guard that makes the new write path safe rather than merely present.


# ── gap 1: documents are reviewable through their redacted rendering ─────────


def test_read_spec_files_returns_only_the_raw_hash_for_a_redacted_document(tmp_path, monkeypatch):
    """The browser gets safe rendering plus the hash needed for approval."""
    monkeypatch.setattr(routes, "_redact", lambda t: t.replace("sk-secret", "[redacted]"))
    spec_dir = tmp_path / "spec"
    spec_dir.mkdir()
    (spec_dir / "requirements.md").write_text("token is sk-secret")
    (spec_dir / "design.md").write_text("nothing sensitive")

    files, docs, tasks = routes._read_spec_files(spec_dir)

    assert files["requirements.md"] == "token is [redacted]"
    # The hash is of the file AS STORED, never of the redacted copy: an approval
    # binds to the real version that was reviewed.
    assert docs["requirements.md"] == {"hash": routes._sha256_text("token is sk-secret")}
    assert set(docs["design.md"]) == {"hash"}
    assert tasks == []


# ── gap 2: approval is recorded against the version approved ─────────────────


@pytest.mark.asyncio
async def test_approve_records_the_version_and_the_user(tmp_path, monkeypatch):
    """Approval is recorded, not merely said in chat: the server must know that a
    phase was signed off, by whom, and against which text."""
    client = _make_client(monkeypatch, tmp_path)
    _seed_spec(tmp_path, files={"requirements.md": "# reviewed"})
    digest = routes._sha256_text("# reviewed")

    client.app["state"] = _state_for("live")[0]
    await client.start_server()
    try:
        resp = await client.post(
            f"{_BASE}/specs/live/approve",
            json={**_spec_identity(), "phase": "requirements", "hash": digest},
        )
        assert resp.status == 200, await resp.json()
        detail = await (await client.get(f"{_BASE}/specs/live")).json()
    finally:
        await client.close()

    stored = routes._load_index()["live"]["approvals"]["requirements"]
    assert stored["hash"] == digest and stored["user"] == _OWNER_USER and stored["at"] > 0
    assert detail["approvals"]["requirements"]["stale"] is False


@pytest.mark.asyncio
async def test_approve_refuses_a_hash_that_is_not_the_current_document(tmp_path, monkeypatch):
    """Approving a version nobody has seen records nothing meaningful, so the
    claim is checked against the file rather than trusted."""
    client = _make_client(monkeypatch, tmp_path)
    _seed_spec(tmp_path, files={"requirements.md": "# actually on disk"})

    client.app["state"] = _state_for("live")[0]
    await client.start_server()
    try:
        resp = await client.post(
            f"{_BASE}/specs/live/approve",
            json={
                **_spec_identity(),
                "phase": "requirements",
                "hash": routes._sha256_text("# something else"),
            },
        )
        body = await resp.json()
    finally:
        await client.close()

    assert resp.status == 409 and body["code"] == "doc_changed"
    assert "approvals" not in routes._load_index()["live"]


@pytest.mark.asyncio
async def test_approve_refuses_a_same_path_spec_recreated_during_hash_read(tmp_path, monkeypatch):
    """A directory path is reusable; the per-creation slot key is the identity."""
    client = _make_client(monkeypatch, tmp_path)
    _seed_spec(tmp_path, files={"requirements.md": "# reviewed"})
    old_key = routes._load_index()["live"]["slot_key"]
    real_read = routes._read_spec_text
    replaced = False

    def _replace_identity(spec_dir, fname):
        nonlocal replaced
        text = real_read(spec_dir, fname)
        if fname == "requirements.md" and not replaced:
            replaced = True
            index = routes._load_index()
            index["live"]["slot_key"] = old_key + "-replacement"
            routes._save_index(index)
        return text

    monkeypatch.setattr(routes, "_read_spec_text", _replace_identity)

    client.app["state"] = _state_for("live")[0]
    await client.start_server()
    try:
        resp = await client.post(
            f"{_BASE}/specs/live/approve",
            json={
                **_spec_identity(),
                "phase": "requirements",
                "hash": routes._sha256_text("# reviewed"),
            },
        )
        body = await resp.json()
    finally:
        await client.close()

    assert resp.status == 409 and body["code"] == "stale_client"
    assert "approvals" not in routes._load_index()["live"]


def test_approvals_report_stale_once_the_document_moves(tmp_path):
    """Why the approved VERSION is recorded rather than a bare flag: a document the
    agent rewrote after sign-off must stop reading as approved."""
    approved = routes._sha256_text("# as reviewed")
    record = {"requirements": {"hash": approved, "at": 5.0, "user": "tester"}}

    unchanged = routes._normalize_approvals(record, {"requirements.md": {"hash": approved}})
    assert unchanged["requirements"]["stale"] is False

    moved = routes._normalize_approvals(
        record, {"requirements.md": {"hash": routes._sha256_text("# rewritten since")}}
    )
    assert moved["requirements"]["stale"] is True

    # The document disappearing is also stale: nothing is left that it describes.
    assert routes._normalize_approvals(record, {})["requirements"]["stale"] is True


def test_approvals_from_the_index_are_normalized_not_trusted():
    """The index is reachable by the agent (it runs shell commands as the user),
    exactly like every other index field this module scrubs on egress. A forged
    or malformed record must not reach the browser as-is."""
    junk = {
        "requirements": {"hash": "not-a-digest", "at": "soon", "user": "x"},
        "design": "not even a dict",
        "tasks": {"hash": "0" * 64},  # not an approvable phase
        "../evil": {"hash": "0" * 64},
    }
    out = routes._normalize_approvals(junk, {})
    assert out == {}, out


@pytest.mark.asyncio
@pytest.mark.parametrize("phase", ["tasks", "new", "", "../requirements"])
async def test_approve_rejects_a_phase_outside_the_approvable_two(phase, tmp_path, monkeypatch):
    """There is no "approve tasks" step: approving the task list IS the handoff."""
    client = _make_client(monkeypatch, tmp_path)
    _seed_spec(tmp_path, files={"requirements.md": "# r"})

    client.app["state"] = _state_for("live")[0]
    await client.start_server()
    try:
        resp = await client.post(
            f"{_BASE}/specs/live/approve",
            json={**_spec_identity(), "phase": phase, "hash": "0" * 64},
        )
        body = await resp.json()
    finally:
        await client.close()

    assert resp.status == 400 and body["code"] == "invalid_phase"


# ── gap 3: one task at a time, addressed by position AND text ────────────────


def test_parse_tasks_enumerates_hashes_and_derives_progress():
    text = (
        "# Tasks\n\n"
        "- [x] wire the endpoint\n"
        "- [ ] add the tests\n"
        "1. [ ] update the docs\n"
        "- [ ]\n"  # no text: not something a user can run
        "just prose\n"
    )
    tasks = routes._parse_tasks(text)

    assert [t["index"] for t in tasks] == [0, 1, 2]
    assert [t["done"] for t in tasks] == [True, False, False]
    assert [t["text"] for t in tasks] == ["wire the endpoint", "add the tests", "update the docs"]
    assert tasks[1]["hash"] == routes._sha256_text("add the tests")
    # The gate's predicate reads the same parse, so the two cannot disagree.
    assert routes._has_open_task(text) is True


@pytest.mark.asyncio
async def test_task_run_dispatches_one_scoped_turn(tmp_path, monkeypatch):
    """Deliberately a single turn and NOT an autonudge loop: the whole-list handoff
    arms a loop that keeps going, while running one task must end where the user
    expects it to."""
    client = _make_client(monkeypatch, tmp_path)
    spec_dir, working_dir = _seed_spec(
        tmp_path, files={"tasks.md": "- [x] done already\n- [ ] add the tests\n"}
    )
    state, slots = _state_for("live")
    sent: list[str] = []
    monkeypatch.setattr(routes, "_dispatch_turn", lambda st, slot, msg: sent.append(msg))

    client.app["state"] = state
    await client.start_server()
    try:
        detail = await (await client.get(f"{_BASE}/specs/live")).json()
        target = detail["tasks"][1]
        resp = await client.post(
            f"{_BASE}/specs/live/task",
            json={**_spec_identity(), "index": target["index"], "hash": target["hash"]},
        )
        body = await resp.json()
    finally:
        await client.close()

    assert resp.status == 200, body
    assert detail["task_progress"] == {"done": 1, "total": 2}
    assert len(sent) == 1
    # Names the task by text and occurrence, and tells the agent to stop rather than continue.
    assert "add the tests" in sent[0]
    assert "checklist item 2" in sent[0]
    assert "Do NOT continue" in sent[0]
    assert str(spec_dir / "tasks.md") in sent[0] and str(working_dir) in sent[0]


@pytest.mark.asyncio
async def test_task_run_identifies_the_selected_duplicate_occurrence(tmp_path, monkeypatch):
    """Repeated labels still dispatch the exact checkbox occurrence the user clicked."""
    client = _make_client(monkeypatch, tmp_path)
    _seed_spec(tmp_path, files={"tasks.md": "- [ ] run the tests\n- [ ] run the tests\n"})
    state, _ = _state_for("live")
    sent: list[str] = []
    monkeypatch.setattr(routes, "_dispatch_turn", lambda st, slot, msg: sent.append(msg))

    client.app["state"] = state
    await client.start_server()
    try:
        detail = await (await client.get(f"{_BASE}/specs/live")).json()
        target = detail["tasks"][1]
        resp = await client.post(
            f"{_BASE}/specs/live/task",
            json={**_spec_identity(), "index": target["index"], "hash": target["hash"]},
        )
        body = await resp.json()
    finally:
        await client.close()

    assert resp.status == 200, body
    assert len(sent) == 1
    assert "checklist item 2" in sent[0]


@pytest.mark.asyncio
async def test_task_run_revalidates_the_task_after_slot_setup(tmp_path, monkeypatch):
    """An IDE edit during the awaited slot setup must win over the stale click."""
    client = _make_client(monkeypatch, tmp_path)
    spec_dir, _ = _seed_spec(tmp_path, files={"tasks.md": "- [ ] original task\n"})
    state, slots = _state_for("live")
    sent: list[str] = []

    async def _setup_then_edit(_state, _name, _meta):
        (spec_dir / "tasks.md").write_text("- [ ] replacement task\n")
        return slots[routes._slot_key("live")]

    monkeypatch.setattr(routes, "_ensure_worker_slot", _setup_then_edit)
    monkeypatch.setattr(routes, "_dispatch_turn", lambda st, slot, msg: sent.append(msg))

    client.app["state"] = state
    await client.start_server()
    try:
        resp = await client.post(
            f"{_BASE}/specs/live/task",
            json={**_spec_identity(), "index": 0, "hash": routes._sha256_text("original task")},
        )
        body = await resp.json()
    finally:
        await client.close()

    assert resp.status == 409 and body["code"] == "task_changed"
    assert sent == [], "dispatched the snapshot captured before slot setup"


@pytest.mark.asyncio
async def test_task_run_refuses_a_task_whose_text_moved(tmp_path, monkeypatch):
    """The reason position alone is not an identity: the agent rewrites tasks.md
    between polls, so a click on "task 2" could otherwise dispatch whatever ended
    up second."""
    client = _make_client(monkeypatch, tmp_path)
    _seed_spec(tmp_path, files={"tasks.md": "- [ ] the list was reordered\n"})
    state, _ = _state_for("live")
    sent: list[str] = []
    monkeypatch.setattr(routes, "_dispatch_turn", lambda st, slot, msg: sent.append(msg))

    client.app["state"] = state
    await client.start_server()
    try:
        resp = await client.post(
            f"{_BASE}/specs/live/task",
            json={
                **_spec_identity(),
                "index": 0,
                "hash": routes._sha256_text("what the user actually clicked"),
            },
        )
        body = await resp.json()
    finally:
        await client.close()

    assert resp.status == 409 and body["code"] == "task_changed"
    assert sent == [], "dispatched a task the user did not choose"


@pytest.mark.asyncio
async def test_task_run_uses_the_same_redacted_identity_the_detail_endpoint_serves(
    tmp_path, monkeypatch
):
    """Reloading produces a runnable raw identity and a redacted task label."""
    client = _make_client(monkeypatch, tmp_path)
    _seed_spec(tmp_path, files={"tasks.md": "- [ ] rotate sk-secret\n"})
    state, _ = _state_for("live")
    sent: list[str] = []
    monkeypatch.setattr(routes, "_redact", lambda text: text.replace("sk-secret", "[redacted]"))
    monkeypatch.setattr(routes, "_dispatch_turn", lambda st, slot, msg: sent.append(msg))

    client.app["state"] = state
    await client.start_server()
    try:
        detail = await (await client.get(f"{_BASE}/specs/live")).json()
        task = detail["tasks"][0]
        resp = await client.post(
            f"{_BASE}/specs/live/task",
            json={**_spec_identity(), "index": task["index"], "hash": task["hash"]},
        )
        body = await resp.json()
    finally:
        await client.close()

    assert resp.status == 200, body
    assert len(sent) == 1 and "rotate [redacted]" in sent[0]


@pytest.mark.asyncio
async def test_task_run_hashes_raw_text_when_redaction_hides_a_change(tmp_path, monkeypatch):
    """A stale click must not survive when only a redacted credential changes."""
    client = _make_client(monkeypatch, tmp_path)
    spec_dir, _ = _seed_spec(tmp_path, files={"tasks.md": "- [ ] rotate secret-old\n"})
    state, _ = _state_for("live")
    sent: list[str] = []
    monkeypatch.setattr(
        routes,
        "_redact",
        lambda text: re.sub(r"secret-(?:old|new)", "[redacted]", text),
    )
    monkeypatch.setattr(routes, "_dispatch_turn", lambda st, slot, msg: sent.append(msg))

    client.app["state"] = state
    await client.start_server()
    try:
        detail = await (await client.get(f"{_BASE}/specs/live")).json()
        task = detail["tasks"][0]
        (spec_dir / "tasks.md").write_text("- [ ] rotate secret-new\n")
        resp = await client.post(
            f"{_BASE}/specs/live/task",
            json={**_spec_identity(), "index": task["index"], "hash": task["hash"]},
        )
        body = await resp.json()
    finally:
        await client.close()

    assert task["text"] == "rotate [redacted]"
    assert resp.status == 409 and body["code"] == "task_changed"
    assert sent == []


@pytest.mark.asyncio
async def test_task_run_is_refused_while_the_whole_list_is_building(tmp_path, monkeypatch):
    """A loop already working the list and a single-task turn write the same files
    and check off the same boxes, so they must not overlap."""
    client = _make_client(monkeypatch, tmp_path)
    _seed_spec(tmp_path, files={"tasks.md": "- [ ] add the tests\n"}, extra={"status": "executing"})
    state, _ = _state_for("live")
    sent: list[str] = []
    monkeypatch.setattr(routes, "_dispatch_turn", lambda st, slot, msg: sent.append(msg))
    monkeypatch.setattr(routes, "_effective_status", _always_executing)

    client.app["state"] = state
    await client.start_server()
    try:
        resp = await client.post(
            f"{_BASE}/specs/live/task",
            json={**_spec_identity(), "index": 0, "hash": routes._sha256_text("add the tests")},
        )
        body = await resp.json()
    finally:
        await client.close()

    assert resp.status == 409 and body["code"] == "already_executing"
    assert sent == []


@pytest.mark.asyncio
async def test_task_run_refuses_a_handoff_that_claims_during_slot_setup(tmp_path, monkeypatch):
    """The handoff owns the spec as soon as its index claim lands, before its
    nudge loop or first turn makes the slot look busy."""
    client = _make_client(monkeypatch, tmp_path)
    _seed_spec(tmp_path, files={"tasks.md": "- [ ] add the tests\n"})
    state, _ = _state_for("live")
    real_ensure = routes._ensure_worker_slot
    sent: list[str] = []

    async def _ensure_after_handoff_claim(*args, **kwargs):
        slot = await real_ensure(*args, **kwargs)
        await routes._touch_spec(
            "live",
            status="executing",
            exec_arming_at=time.time(),
        )
        return slot

    monkeypatch.setattr(routes, "_ensure_worker_slot", _ensure_after_handoff_claim)
    monkeypatch.setattr(routes, "_dispatch_turn", lambda *_args: sent.append("sent"))

    client.app["state"] = state
    await client.start_server()
    try:
        resp = await client.post(
            f"{_BASE}/specs/live/task",
            json={**_spec_identity(), "index": 0, "hash": routes._sha256_text("add the tests")},
        )
        body = await resp.json()
    finally:
        await client.close()

    assert resp.status == 409 and body["code"] == "already_executing"
    assert sent == []


@pytest.mark.asyncio
async def test_concurrent_task_runs_dispatch_only_once(tmp_path, monkeypatch):
    """Two requests may pass the early status check before either owns the slot."""
    client = _make_client(monkeypatch, tmp_path)
    _seed_spec(tmp_path, files={"tasks.md": "- [ ] add the tests\n"})
    state, slots = _state_for("live")
    slot = slots[routes._slot_key("live")]
    real_ensure = routes._ensure_worker_slot
    first_waiting = asyncio.Event()
    release = asyncio.Event()
    arrivals = 0
    sent: list[str] = []

    async def _held_ensure(*args, **kwargs):
        nonlocal arrivals
        resolved = await real_ensure(*args, **kwargs)
        arrivals += 1
        if arrivals == 1:
            first_waiting.set()
        await release.wait()
        return resolved

    def _mark_running(_state, _slot, message):
        sent.append(message)
        slot.running = True

    monkeypatch.setattr(routes, "_ensure_worker_slot", _held_ensure)
    monkeypatch.setattr(routes, "_dispatch_turn", _mark_running)
    body = {**_spec_identity(), "index": 0, "hash": routes._sha256_text("add the tests")}

    client.app["state"] = state
    await client.start_server()
    try:
        first = asyncio.create_task(client.post(f"{_BASE}/specs/live/task", json=body))
        second = asyncio.create_task(client.post(f"{_BASE}/specs/live/task", json=body))
        await asyncio.wait_for(first_waiting.wait(), timeout=5)
        release.set()
        responses = await asyncio.gather(first, second)
        payloads = [await response.json() for response in responses]
    finally:
        release.set()
        await client.close()

    assert sorted(response.status for response in responses) == [200, 409]
    assert any(payload.get("code") == "agent_running" for payload in payloads)
    assert arrivals == 1, "the losing request materialized the slot before checking the winner"
    assert len(sent) == 1


async def _always_executing(name, meta, slot):
    return "executing"


@pytest.mark.asyncio
async def test_task_run_refuses_a_task_already_checked_off(tmp_path, monkeypatch):
    client = _make_client(monkeypatch, tmp_path)
    _seed_spec(tmp_path, files={"tasks.md": "- [x] already finished\n"})
    state, _ = _state_for("live")

    client.app["state"] = state
    await client.start_server()
    try:
        resp = await client.post(
            f"{_BASE}/specs/live/task",
            json={
                **_spec_identity(),
                "index": 0,
                "hash": routes._sha256_text("already finished"),
            },
        )
        body = await resp.json()
    finally:
        await client.close()

    assert resp.status == 409 and body["code"] == "task_done"


# ── gap 5: label and archive ─────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_title_relabels_without_touching_the_identity(tmp_path, monkeypatch):
    """A rename of the LABEL only. The name is simultaneously the on-disk directory,
    the git branch and the chat slot key -- and _owns_slot_key requires the key to
    ENCODE the name -- so renaming the identity would move a directory the IDE and
    CLI also read and orphan the spec's transcript, which is exactly what
    delete-and-recreate loses."""
    client = _make_client(monkeypatch, tmp_path)
    spec_dir, _ = _seed_spec(tmp_path)

    client.app["state"] = _state_for("live")[0]
    await client.start_server()
    try:
        resp = await client.post(
            f"{_BASE}/specs/live/title",
            json={**_spec_identity(), "title": "Checkout rewrite"},
        )
        assert resp.status == 200, await resp.json()
    finally:
        await client.close()

    entry = routes._load_index()["live"]
    assert entry["title"] == "Checkout rewrite"
    assert entry["spec_dir"] == str(spec_dir), "the directory moved"
    assert entry["slot_key"] == routes._slot_key("live"), "the transcript was orphaned"
    assert spec_dir.is_dir()


@pytest.mark.asyncio
async def test_archive_marks_the_spec_without_deleting_it(tmp_path, monkeypatch):
    """The non-destructive counterpart to delete: documents, transcript and index
    entry all stay. Without it the only way out of the rail is delete, which makes
    tidying up and destroying the work the same action."""
    client = _make_client(monkeypatch, tmp_path)
    spec_dir, _ = _seed_spec(tmp_path, files={"requirements.md": "# keep me"})

    client.app["state"] = _state_for("live")[0]
    await client.start_server()
    try:
        assert (
            await client.post(
                f"{_BASE}/specs/live/archive",
                json={**_spec_identity(), "archived": True},
            )
        ).status == 200

        listed = await (await client.get(f"{_BASE}/specs")).json()
        # Still reachable directly, and still on disk.
        detail = await (await client.get(f"{_BASE}/specs/live")).json()

        assert (
            await client.post(
                f"{_BASE}/specs/live/archive",
                json={**_spec_identity(), "archived": False},
            )
        ).status == 200
        restored = await (await client.get(f"{_BASE}/specs")).json()
    finally:
        await client.close()

    assert [(s["name"], s["archived"]) for s in listed["specs"]] == [("live", True)]
    assert detail["archived"] is True
    assert (spec_dir / "requirements.md").read_text() == "# keep me"
    assert [s["name"] for s in restored["specs"]] == ["live"]


@pytest.mark.asyncio
async def test_archive_is_refused_while_the_spec_is_building(tmp_path, monkeypatch):
    """Archiving a running spec would hide a loop that keeps editing files, leaving
    the user no surface to stop it from."""
    client = _make_client(monkeypatch, tmp_path)
    _seed_spec(tmp_path, extra={"status": "executing"})
    monkeypatch.setattr(routes, "_effective_status", _always_executing)

    client.app["state"] = _state_for("live")[0]
    await client.start_server()
    try:
        resp = await client.post(
            f"{_BASE}/specs/live/archive",
            json={**_spec_identity(), "archived": True},
        )
        body = await resp.json()
    finally:
        await client.close()

    assert resp.status == 409 and body["code"] == "spec_executing"
    assert routes._load_index()["live"].get("archived") is not True


# ── shared guards for the new mutations ──────────────────────────────────────


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("method", "path", "body"),
    [
        ("post", "approve", {"phase": "requirements", "hash": "0" * 64}),
        ("post", "task", {"index": 0, "hash": "0" * 64}),
        ("post", "title", {"title": "t"}),
        ("post", "archive", {"archived": True}),
        ("post", "duplicate", {"new_name": "a-copy"}),
    ],
)
async def test_new_mutations_refuse_a_stale_client_identity(
    method, path, body, tmp_path, monkeypatch
):
    """Every mutation carries the identity the CLIENT rendered, so a stale tab
    cannot drive a same-name spec that was deleted and recreated elsewhere. The
    per-creation slot key is the decisive field: two specs can share a directory
    across a delete + re-import, but never a key."""
    client = _make_client(monkeypatch, tmp_path)
    _seed_spec(tmp_path, files={"requirements.md": "# r", "tasks.md": "- [ ] t\n"})

    client.app["state"] = _state_for("live")[0]
    await client.start_server()
    try:
        resp = await getattr(client, method)(
            f"{_BASE}/specs/live/{path}",
            json={**body, "spec_dir": "/somewhere/else", "slot_key": "spec-builder-live-deadbeef"},
        )
        payload = await resp.json()
    finally:
        await client.close()

    assert resp.status == 409, payload
    assert payload["code"] == "stale_client"


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("method", "path", "body"),
    [
        ("post", "approve", {"phase": "requirements", "hash": "0" * 64}),
        ("post", "task", {"index": 0, "hash": "0" * 64}),
        ("post", "title", {"title": "t"}),
        ("post", "archive", {"archived": True}),
        ("post", "duplicate", {"new_name": "a-copy"}),
    ],
)
async def test_new_mutations_require_a_complete_client_identity(
    method, path, body, tmp_path, monkeypatch
):
    """A control rendered before detail loads must not mutate whatever creation
    currently happens to own the same name."""
    client = _make_client(monkeypatch, tmp_path)
    _seed_spec(tmp_path, files={"requirements.md": "# r", "tasks.md": "- [ ] t\n"})

    client.app["state"] = _state_for("live")[0]
    await client.start_server()
    try:
        resp = await getattr(client, method)(f"{_BASE}/specs/live/{path}", json=body)
        payload = await resp.json()
    finally:
        await client.close()

    assert resp.status == 409, payload
    assert payload["code"] == "stale_client"


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("method", "path"),
    [
        ("post", "approve"),
        ("post", "task"),
        ("post", "title"),
        ("post", "archive"),
        ("post", "duplicate"),
    ],
)
async def test_new_mutations_require_authentication(method, path, tmp_path, monkeypatch):
    """No auth middleware, so request['user'] is unset: every new write must 401
    rather than fall through to the filesystem."""
    _redirect_state(monkeypatch, tmp_path)
    app = web.Application()  # deliberately WITHOUT _auth_mw
    routes.register_routes(app)
    async with TestClient(TestServer(app)) as client:
        resp = await getattr(client, method)(f"{_BASE}/specs/live/{path}", json={})
        assert resp.status == 401


def test_new_mutations_do_not_write_the_index_from_a_stale_snapshot():
    """Extends the existing class guard to the new handlers: each awaits (a body
    read at minimum), so none of them may call _save_index directly."""
    for handler in (
        routes._handle_approve,
        routes._handle_run_task,
        routes._handle_title,
        routes._handle_archive,
        routes._handle_duplicate,
    ):
        assert "_save_index(" not in inspect.getsource(
            handler
        ), f"{handler.__name__} writes the index directly; it must go through _mutate_index"


def test_document_and_task_reads_stay_off_the_event_loop():
    """Source guard, same reason the detail handler has one: these handlers read
    and write caller-supplied paths, and the detail endpoint beside them is polled
    every 2.5s. Blocking filesystem work must ride a worker thread."""
    for handler, blocking in (
        (routes._approve_locked, "_current_hash"),
        (routes._handle_run_task, "_tasks"),
        (routes._duplicate_locked, "_copy"),
    ):
        src = inspect.getsource(handler)
        # Matched on the two tokens rather than one formatted call string: the
        # arguments wrap, so pinning the exact whitespace would fail on a reformat
        # while saying nothing about whether the work is still offloaded.
        assert (
            "asyncio.to_thread(" in src and blocking in src
        ), f"{handler.__name__} may be doing filesystem work inline"


@web.middleware
async def _app_auth_mw(request, handler):
    """Model an authenticated app token, which also carries a user identity."""
    request["user"] = "tester"
    request["app"] = "spec-builder"
    return await handler(request)


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "body",
    [
        {"text": "ordinary app-authored message"},
        {
            "text": "Decision - Transport: HTTPS",
            "decision_id": "transport",
            "decision_option": "HTTPS",
        },
    ],
)
async def test_app_tokens_cannot_mint_human_spec_messages(body, tmp_path, monkeypatch):
    """App tokens are authenticated but cannot authorize user-only directives."""
    _redirect_state(monkeypatch, tmp_path)
    app = web.Application(middlewares=[_app_auth_mw])
    routes.register_routes(app)

    async with TestClient(TestServer(app)) as client:
        response = await client.post(f"{_BASE}/specs/s/message", json=body)
        payload = await response.json()

    assert response.status == 403
    assert payload["code"] == "interactive_user_required"
