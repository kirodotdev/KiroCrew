"""Route tests: two spellings of one spec directory.

A second index name, a symlinked spelling or a case alias on a case-folding
volume must resolve to the same spec: one decision record, one busy check, one
create. Covers claims, messages, handoff, delete and create against an alias,
and how the alias scan resolves and trusts slot keys.
"""

from __future__ import annotations

import asyncio
import contextlib
import inspect
import os
import sys
import types
from pathlib import Path

import pytest

from kiro_crew.apps.builtins.spec_builder.tests.routes_facade import routes

# _never_touch_the_real_state is an autouse fixture: pytest finds it by the name bound here.
from kiro_crew.apps.builtins.spec_builder.tests.routes_helpers import (  # noqa: F401
    _BASE,
    _create_client,
    _decision_record,
    _decision_spec,
    _macos_case_aliases,
    _make_client,
    _never_touch_the_real_state,
    _recorded_answers,
    _redirect_state,
    _slot_stub,
)


@pytest.mark.asyncio
async def test_an_alias_name_for_the_same_folder_cannot_re_answer(tmp_path, monkeypatch):
    """A name is a label the agent can mint more of; the directory is the spec.

    A record keyed on the NAME gives a second index entry pointing at the SAME spec
    directory its own empty record, so its cards render answerable and a click
    dispatches a conflicting answer over the same documents. Keyed on the directory,
    both names resolve to one record.
    """
    client = _make_client(monkeypatch, tmp_path)
    spec_dir, slot_key = _decision_spec(tmp_path)
    assert (
        await routes._claim_decision(
            "s", "transport", "HTTPS", expect_spec_dir=spec_dir, expect_slot_key=slot_key
        )
    )[0] == routes._CLAIM_RECORDED

    # The alias: a second, perfectly valid index entry for the same files.
    index = await routes._aload_index()
    alias_key = routes._new_slot_key("s-alias")
    index["s-alias"] = {**index["s"], "slot_key": alias_key}
    routes._save_index(index)

    dispatched: list = []
    monkeypatch.setattr(routes, "_dispatch_turn", lambda *a, **k: dispatched.append(a[2]))
    state, _slot = _slot_stub()
    client.app["state"] = state
    await client.start_server()
    try:
        resp = await client.post(
            f"{_BASE}/specs/s-alias/message",
            json={
                "spec_dir": spec_dir,
                "slot_key": alias_key,
                "decision_id": "transport",
                "decision_option": "Streaming",
                "text": "Decision - transport: Streaming",
            },
        )
        payload = await resp.json()
    finally:
        await client.close()

    assert resp.status == 409, payload
    assert payload.get("code") == "decision_already_answered", payload
    assert payload.get("answer") == "HTTPS"
    assert dispatched == [], "the alias dispatched a second answer for a settled decision"


def _add_case_alias(spec_dir: str, alias_slot_key: str) -> str:
    """Add an upgrade-state alias whose lexical ledger key differs."""
    index = routes._load_index()
    alias_dir = str(Path(spec_dir).with_name(Path(spec_dir).name.swapcase()))
    index["s-alias"] = {
        **index["s"],
        "spec_dir": alias_dir,
        "slot_key": alias_slot_key,
    }
    routes._save_index(index)
    return alias_dir


@pytest.mark.asyncio
async def test_macos_case_alias_cannot_mint_a_second_decision_ledger(tmp_path, monkeypatch):
    """Upgrade-state aliases with distinct lexical keys fail closed on claim."""
    _redirect_state(monkeypatch, tmp_path)
    _macos_case_aliases(monkeypatch)
    spec_dir, slot_key = _decision_spec(tmp_path)
    outcome, _held = await routes._claim_decision(
        "s",
        "transport",
        "HTTPS",
        expect_spec_dir=spec_dir,
        expect_slot_key=slot_key,
    )
    assert outcome == routes._CLAIM_RECORDED

    alias_slot_key = routes._new_slot_key("s-alias")
    alias_dir = _add_case_alias(spec_dir, alias_slot_key)
    outcome, _held = await routes._claim_decision(
        "s-alias",
        "transport",
        "Streaming",
        expect_spec_dir=alias_dir,
        expect_slot_key=alias_slot_key,
    )

    assert outcome == routes._CLAIM_ALIAS_CONFLICT
    store, usable = routes._read_decisions()
    assert usable is True
    assert routes._decision_key(alias_dir) not in store
    assert _decision_record(store, spec_dir) == {"transport": "HTTPS"}


@pytest.mark.asyncio
async def test_macos_single_path_rewrite_cannot_fork_the_decision_ledger(tmp_path, monkeypatch):
    """A protected old key fails closed after the sole index spelling changes."""
    client = _make_client(monkeypatch, tmp_path)
    _macos_case_aliases(monkeypatch)
    spec_dir, slot_key = _decision_spec(tmp_path)
    outcome, _held = await routes._claim_decision(
        "s",
        "transport",
        "HTTPS",
        expect_spec_dir=spec_dir,
        expect_slot_key=slot_key,
    )
    assert outcome == routes._CLAIM_RECORDED

    rewritten_dir = str(Path(spec_dir).with_name("S"))
    index = routes._load_index()
    index["s"]["spec_dir"] = rewritten_dir
    routes._save_index(index)

    outcome, _held = await routes._claim_decision(
        "s",
        "transport",
        "Streaming",
        expect_spec_dir=rewritten_dir,
        expect_slot_key=slot_key,
    )

    client.app["state"] = None
    await client.start_server()
    try:
        detail = await client.get(f"{_BASE}/specs/s")
        detail_body = await detail.json()
        deleted = await client.delete(f"{_BASE}/specs/s")
        delete_body = await deleted.json()
    finally:
        await client.close()

    assert outcome == routes._CLAIM_ALIAS_CONFLICT
    assert detail.status == 409, detail_body
    assert detail_body["code"] == "decision_directory_alias_conflict"
    assert deleted.status == 409, delete_body
    assert delete_body["code"] == "decision_directory_alias_conflict"
    store, usable = routes._read_decisions()
    assert usable is True
    assert routes._decision_key(rewritten_dir) not in store
    assert _decision_record(store, spec_dir) == {"transport": "HTTPS"}


@pytest.mark.asyncio
async def test_claim_uses_one_ledger_snapshot_for_alias_validation_and_write(tmp_path, monkeypatch):
    """A transient alias-check read cannot be followed by a write from a new snapshot."""
    _macos_case_aliases(monkeypatch)
    spec_dir, slot_key = _decision_spec(tmp_path)
    outcome, _held = await routes._claim_decision(
        "s",
        "transport",
        "HTTPS",
        expect_spec_dir=spec_dir,
        expect_slot_key=slot_key,
    )
    assert outcome == routes._CLAIM_RECORDED

    rewritten_dir = str(Path(spec_dir).with_name("S"))
    index = routes._load_index()
    index["s"]["spec_dir"] = rewritten_dir
    routes._save_index(index)
    read_decisions = routes._read_decisions
    reads = 0

    def _fail_once():
        nonlocal reads
        reads += 1
        return ({}, False) if reads == 1 else read_decisions()

    monkeypatch.setattr(routes, "_read_decisions", _fail_once)
    outcome, _held = await routes._claim_decision(
        "s",
        "transport",
        "Streaming",
        expect_spec_dir=rewritten_dir,
        expect_slot_key=slot_key,
    )
    monkeypatch.setattr(routes, "_read_decisions", read_decisions)

    assert outcome == routes._CLAIM_UNREADABLE
    assert reads == 1
    store, usable = routes._read_decisions()
    assert usable is True
    assert routes._decision_key(rewritten_dir) not in store
    assert _decision_record(store, spec_dir) == {"transport": "HTTPS"}


@pytest.mark.asyncio
async def test_macos_case_alias_conflict_refuses_detail_and_delete(tmp_path, monkeypatch):
    """Serving or deleting cannot strand a settled record under another spelling."""
    client = _make_client(monkeypatch, tmp_path)
    _macos_case_aliases(monkeypatch)
    spec_dir, slot_key = _decision_spec(tmp_path)
    outcome, _held = await routes._claim_decision(
        "s",
        "transport",
        "HTTPS",
        expect_spec_dir=spec_dir,
        expect_slot_key=slot_key,
    )
    assert outcome == routes._CLAIM_RECORDED
    alias_slot_key = routes._new_slot_key("s-alias")
    _add_case_alias(spec_dir, alias_slot_key)

    client.app["state"] = None
    await client.start_server()
    try:
        detail = await client.get(f"{_BASE}/specs/s")
        detail_body = await detail.json()
        deleted = await client.delete(f"{_BASE}/specs/s")
        delete_body = await deleted.json()
    finally:
        await client.close()

    assert detail.status == 409, detail_body
    assert detail_body["code"] == "decision_directory_alias_conflict"
    assert deleted.status == 409, delete_body
    assert delete_body["code"] == "decision_directory_alias_conflict"
    assert set(routes._load_index()) == {"s", "s-alias"}
    assert await _recorded_answers(spec_dir) == {"transport": "HTTPS"}


@pytest.mark.asyncio
async def test_alias_added_during_delete_prevents_the_index_pop(tmp_path, monkeypatch):
    """A late alias keeps an already-torn-down delete reserved for retry."""
    client = _make_client(monkeypatch, tmp_path)
    _macos_case_aliases(monkeypatch)
    spec_dir, slot_key = _decision_spec(tmp_path)
    outcome, _held = await routes._claim_decision(
        "s",
        "transport",
        "HTTPS",
        expect_spec_dir=spec_dir,
        expect_slot_key=slot_key,
    )
    assert outcome == routes._CLAIM_RECORDED

    async def _teardown_with_alias(*_args, **_kwargs) -> bool:
        alias_slot_key = routes._new_slot_key("s-alias")
        index = routes._load_index()
        alias = dict(index["s"])
        alias["spec_dir"] = str(Path(spec_dir).with_name("S"))
        alias["slot_key"] = alias_slot_key
        index["s-alias"] = alias
        routes._save_index(index)
        return True

    monkeypatch.setattr(routes, "_teardown_worker_slot", _teardown_with_alias)

    client.app["state"] = None
    await client.start_server()
    try:
        response = await client.delete(f"{_BASE}/specs/s")
        body = await response.json()
    finally:
        await client.close()

    assert response.status == 409, body
    assert body["code"] == "decision_directory_alias_conflict"
    assert "retry" in body["error"]
    index = routes._load_index()
    assert set(index) == {"s", "s-alias"}
    assert index["s"].get(routes._DELETING), "the torn-down spec was re-exposed"
    assert spec_dir in routes._load_deleted(), "the delete lost its tombstone"
    assert await _recorded_answers(spec_dir) == {"transport": "HTTPS"}


def test_malformed_index_path_cannot_crash_directory_identity(tmp_path, monkeypatch):
    """Agent-written NUL paths are rejected and samefile failures are contained."""
    _redirect_state(monkeypatch, tmp_path)
    routes._save_index({"broken": {"spec_dir": "\x00"}})

    assert routes._load_index() == {}
    assert routes._same_spec_dir("\x00", str(tmp_path)) is False


def test_a_symlinked_spelling_cannot_record_a_second_answer(tmp_path, monkeypatch):
    """The KEY does not collapse a symlinked spelling; the WRITE end refuses it.

    Collapsing it via resolve() buys a worse hole, because the spec directory belongs
    to the agent: swapping the directory for a symlink moves the key while the index
    identity still matches, so a settled record goes missing and the card re-opens. A
    key derived from mutable filesystem state is a key the agent can move.

    The alias-by-spelling guarantee therefore lives at the WRITE end. The key is
    lexical (two spellings ARE two keys), and `_claim_decision_locked` refuses a
    spec_dir that does not verify as itself -- so the aliased spelling cannot record
    anything, which is what the collapse existed to prevent.
    """
    _redirect_state(monkeypatch, tmp_path)
    real = tmp_path / "real-spec"
    real.mkdir()
    link = tmp_path / "link-spec"
    link.symlink_to(real, target_is_directory=True)

    # Lexical, so the spellings do not share a key...
    assert routes._decision_key(str(link)) != routes._decision_key(str(real))
    # ...and that is safe because the aliased spelling is refused at the write gate.
    assert (
        routes._verified_spec_dir(link) is None
    ), "a symlinked spelling verifies as itself, so it could mint a second record"
    assert (
        routes._verified_spec_dir(real) is not None
    ), "the real directory must still verify, or no spec could record an answer"


@pytest.mark.asyncio
async def test_a_claim_through_an_aliased_spelling_is_refused(tmp_path, monkeypatch):
    """The write gate that replaced the resolving key.

    Two index entries, one spelling the directory directly and one through a symlink.
    The key is lexical now, so the aliased entry has its OWN key -- which would be an
    empty record, exactly the alias hole. Once both spellings are live, claims fail
    closed before either key can be written.
    """
    _redirect_state(monkeypatch, tmp_path)
    real = tmp_path / "proj" / ".kiro" / "specs" / "real"
    real.mkdir(parents=True)
    link = tmp_path / "proj" / ".kiro" / "specs" / "alias"
    link.symlink_to(real, target_is_directory=True)

    entry = {
        "working_dir": str(tmp_path / "proj"),
        "spec_type": "feature",
        "status": "planning",
        "created_at": 1.0,
        "updated_at": 1.0,
    }
    real_entry = {
        **entry,
        "spec_dir": str(real),
        "slot_key": "spec-builder-real-aaaaaaaa",
    }
    routes._save_index({"real": real_entry})

    # The real spec records its answer normally.
    outcome, _held = await routes._claim_decision(
        "real",
        "transport",
        "HTTPS",
        expect_spec_dir=str(real),
        expect_slot_key="spec-builder-real-aaaaaaaa",
    )
    assert outcome == routes._CLAIM_RECORDED, outcome

    routes._save_index(
        {
            "real": real_entry,
            "alias": {
                **entry,
                "spec_dir": str(link),
                "slot_key": "spec-builder-alias-bbbbbbbb",
            },
        }
    )

    # The aliased spelling is refused rather than minting its own record.
    outcome, _held = await routes._claim_decision(
        "alias",
        "transport",
        "Streaming",
        expect_spec_dir=str(link),
        expect_slot_key="spec-builder-alias-bbbbbbbb",
    )
    assert outcome == routes._CLAIM_ALIAS_CONFLICT, (
        f"the aliased spelling recorded an answer ({outcome}), so a settled decision "
        "can be answered a second time through a symlink"
    )
    store, _usable = routes._read_decisions()
    assert (
        routes._decision_key(str(link)) not in store
    ), "a second ledger record was minted for the aliased spelling"
    assert _decision_record(store, str(real)) == {"transport": "HTTPS"}


@pytest.mark.asyncio
async def test_deleting_one_alias_leaves_the_shared_record_alone(tmp_path, monkeypatch):
    """The record belongs to the directory, so it outlives any one name pointing at it.

    Keying on the directory is what closes the alias hole, but it also means a single
    name's delete must not clear the record: the surviving alias still serves those
    documents, and a cleared record would hand it a clean slate for decisions that are
    already settled -- the alias hole again, reached through a delete.
    """
    client = _make_client(monkeypatch, tmp_path)
    spec_dir, slot_key = _decision_spec(tmp_path)
    assert (
        await routes._claim_decision(
            "s", "transport", "HTTPS", expect_spec_dir=spec_dir, expect_slot_key=slot_key
        )
    )[0] == routes._CLAIM_RECORDED

    index = await routes._aload_index()
    alias_key = routes._new_slot_key("s-alias")
    index["s-alias"] = {**index["s"], "slot_key": alias_key}
    routes._save_index(index)

    client.app["state"] = None
    await client.start_server()
    try:
        resp = await client.delete(f"{_BASE}/specs/s")
    finally:
        await client.close()

    assert resp.status == 200, await resp.text()
    assert await _recorded_answers(spec_dir) == {
        "transport": "HTTPS"
    }, "deleting one name cleared the record the surviving alias still needs"
    outcome, held = await routes._claim_decision(
        "s-alias",
        "transport",
        "Streaming",
        expect_spec_dir=spec_dir,
        expect_slot_key=alias_key,
    )
    assert outcome == routes._CLAIM_TAKEN, "the surviving alias could re-answer"
    assert held == "HTTPS"


@pytest.mark.asyncio
async def test_an_alias_mid_turn_blocks_the_answer(tmp_path, monkeypatch):
    """A running turn under ANY name on this directory blocks the answer.

    Aliases are separate sessions over the same documents, so checking only this
    request's own slot let one alias record and dispatch an answer while the other's
    agent was still working -- two concurrent agents, one set of files, and an answer
    delivered to whichever won.
    """
    client = _make_client(monkeypatch, tmp_path)
    spec_dir, slot_key = _decision_spec(tmp_path)

    index = await routes._aload_index()
    alias_key = routes._new_slot_key("s-alias")
    index["s-alias"] = {**index["s"], "slot_key": alias_key}
    routes._save_index(index)

    dispatched: list = []
    monkeypatch.setattr(routes, "_dispatch_turn", lambda *a, **k: dispatched.append(a[2]))

    # The ALIAS is mid-turn; the name being answered is idle.
    state, _slot = _slot_stub()
    busy = types.SimpleNamespace(key=alias_key, running=True, messages=[])
    real_get_slot = state.get_slot

    def _get_slot(key):
        return busy if key == alias_key else real_get_slot(key)

    state.get_slot = _get_slot

    client.app["state"] = state
    await client.start_server()
    try:
        resp = await client.post(
            f"{_BASE}/specs/s/message",
            json={
                "spec_dir": spec_dir,
                "slot_key": slot_key,
                "decision_id": "transport",
                "decision_option": "HTTPS",
                "text": "Decision - transport: HTTPS",
            },
        )
        payload = await resp.json()
    finally:
        await client.close()

    assert resp.status == 409, payload
    # `spec_busy_elsewhere`, not `decision_agent_busy`: the blocker is a DIFFERENT
    # session on these files, which refuses every kind of dispatch, not just answers.
    assert payload.get("code") == "spec_busy_elsewhere", payload
    assert dispatched == [], "an answer was dispatched while an alias's agent was working"
    assert (
        await _recorded_answers(spec_dir) == {}
    ), "the answer was recorded even though nothing was dispatched"


@pytest.mark.asyncio
async def test_an_alias_turn_that_starts_and_finishes_during_claim_defers_the_answer(
    tmp_path, monkeypatch
):
    """Final alias verification must observe a cleared task's generation.

    Dashboard chat does not take Spec Builder's directory lock. An alias turn can
    therefore fit wholly inside an off-loop ledger write and read idle at both ends;
    production clears its task back to ``None``, so only monotonic history can prevent
    concurrent decision dispatch.
    """
    client = _make_client(monkeypatch, tmp_path)
    spec_dir, slot_key = _decision_spec(tmp_path)
    index = await routes._aload_index()
    alias_key = _alias_of(index, "s", "s-alias")
    state, _slot = _slot_stub()

    from kiro_crew.dashboard.state import _ChatSlot

    alias_slot = _ChatSlot(alias_key)
    real_get_slot = state.get_slot

    def _get_slot(key):
        return alias_slot if key == alias_key else real_get_slot(key)

    state.get_slot = _get_slot
    state._background_tasks = set()
    dispatched: list[str] = []
    monkeypatch.setattr(routes, "_dispatch_turn", lambda *a, **k: dispatched.append(a[2]))
    real_mark_relayed = routes._mark_decision_relayed

    async def _mark_while_alias_turn_runs(*args, **kwargs):
        marked = await real_mark_relayed(*args, **kwargs)

        async def _alias_turn() -> None:
            return None

        alias_slot.task = asyncio.create_task(_alias_turn())
        await alias_slot.task
        # Normal queue teardown publishes idle by clearing the task. A snapshot of
        # only ``slot.task`` therefore sees ``None`` both before and after this turn.
        alias_slot.append("done", "", "done")
        alias_slot.task = None
        return marked

    monkeypatch.setattr(routes, "_mark_decision_relayed", _mark_while_alias_turn_runs)

    client.app["state"] = state
    await client.start_server()
    try:
        resp = await client.post(
            f"{_BASE}/specs/s/message",
            json={
                "spec_dir": spec_dir,
                "slot_key": slot_key,
                "decision_id": "transport",
                "decision_option": "HTTPS",
                "text": "Decision - transport: HTTPS",
            },
        )
        payload = await resp.json()
    finally:
        await client.close()

    assert resp.status == 503, payload
    assert payload.get("code") == "decision_delivery_pending", payload
    assert dispatched == []
    pending = await routes._pending_decisions(spec_dir)
    assert [(entry.get("status"), entry.get("option")) for entry in pending] == [
        ("pending", "HTTPS")
    ]


def _alias_of(index: dict, name: str, alias: str) -> str:
    """Add a second index entry for the same directory. Returns its slot key."""
    alias_key = routes._new_slot_key(alias)
    index[alias] = {**index[name], "slot_key": alias_key}
    routes._save_index(index)
    return alias_key


class _HeldTurnLock:
    """Expose when a handler reaches its directory-lock boundary."""

    def __init__(self) -> None:
        self.entered = asyncio.Event()
        self.release = asyncio.Event()

    async def __aenter__(self):
        self.entered.set()
        await self.release.wait()
        return self

    async def __aexit__(self, *_exc) -> None:
        return None


@pytest.mark.asyncio
async def test_an_alias_added_while_message_waits_for_the_turn_lock_is_seen(tmp_path, monkeypatch):
    """The alias snapshot must be taken after entering the directory lock.

    An agent may add an index alias while a request waits. If the request scans first,
    the alias can dispatch under the shared lock and the stale request then starts a
    second agent over the same files without seeing it.
    """
    client = _make_client(monkeypatch, tmp_path)
    spec_dir, slot_key = _decision_spec(tmp_path)
    state, _slot = _slot_stub()
    held_lock = _HeldTurnLock()
    monkeypatch.setattr(routes, "_turn_lock", lambda _key: held_lock)

    dispatched: list = []
    monkeypatch.setattr(routes, "_dispatch_turn", lambda *a, **k: dispatched.append(a[2]))

    request_task = None
    client.app["state"] = state
    await client.start_server()
    try:
        request_task = asyncio.create_task(
            client.post(
                f"{_BASE}/specs/s/message",
                json={"spec_dir": spec_dir, "slot_key": slot_key, "text": "just a note"},
            )
        )
        await held_lock.entered.wait()

        alias_key = _alias_of(await routes._aload_index(), "s", "s-alias")
        busy = types.SimpleNamespace(key=alias_key, running=True, messages=[])
        real_get_slot = state.get_slot
        state.get_slot = lambda key: busy if key == alias_key else real_get_slot(key)
        held_lock.release.set()

        resp = await request_task
        payload = await resp.json()
    finally:
        held_lock.release.set()
        if request_task is not None and not request_task.done():
            request_task.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await request_task
        await client.close()

    assert resp.status == 409, payload
    assert payload.get("code") == "spec_busy_elsewhere", payload
    assert dispatched == [], "the stale pre-lock alias snapshot admitted a second agent"


@pytest.mark.asyncio
async def test_an_alias_added_while_handoff_waits_for_the_turn_lock_is_seen(tmp_path, monkeypatch):
    """A handoff must discover aliases only after entering the directory lock."""
    client = _make_client(monkeypatch, tmp_path)
    spec_dir, _slot_key = _decision_spec(tmp_path, state={"phase": "tasks"})
    (Path(spec_dir) / "tasks.md").write_text("- [ ] a task\n")
    state, _slot = _slot_stub()
    held_lock = _HeldTurnLock()
    monkeypatch.setattr(routes, "_turn_lock", lambda _key: held_lock)
    monkeypatch.setattr(routes, "_autonudge_instance", lambda: object())

    authorized: list = []
    dispatched: list = []
    monkeypatch.setattr(routes, "_dispatch_turn", lambda *a, **k: dispatched.append(a[2]))

    async def _authorized(**_kw):
        authorized.append(True)
        return types.SimpleNamespace(id="loop-1"), "", 200

    monkeypatch.setattr(routes, "authorize_and_add_nudge", _authorized)

    request_task = None
    client.app["state"] = state
    await client.start_server()
    try:
        request_task = asyncio.create_task(
            client.post(f"{_BASE}/specs/s/handoff", json={"spec_dir": spec_dir})
        )
        await held_lock.entered.wait()

        alias_key = _alias_of(await routes._aload_index(), "s", "s-alias")
        busy = types.SimpleNamespace(key=alias_key, running=True, messages=[])
        real_get_slot = state.get_slot
        state.get_slot = lambda key: busy if key == alias_key else real_get_slot(key)
        held_lock.release.set()

        resp = await request_task
        payload = await resp.json()
    finally:
        held_lock.release.set()
        if request_task is not None and not request_task.done():
            request_task.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await request_task
        await client.close()

    assert resp.status == 409, payload
    assert payload.get("code") == "spec_busy_elsewhere", payload
    assert authorized == [], "the stale pre-lock alias snapshot armed a second agent"
    assert dispatched == [], "the stale pre-lock alias snapshot dispatched a second agent"


@pytest.mark.asyncio
@pytest.mark.parametrize("physically_equivalent", [True, False])
async def test_final_alias_scan_refuses_own_slot_directory_rewrite(
    tmp_path, monkeypatch, physically_equivalent
):
    """A rewritten current entry cannot move Stop onto a different lock key."""
    _redirect_state(monkeypatch, tmp_path)
    original_dir = "/physical/spec-a"
    rewritten_dir = "/alias/spec-b"
    slot_key = "spec-builder-s-1234abcd"
    routes._save_index(
        {
            "s": {
                "spec_dir": rewritten_dir,
                "slot_key": slot_key,
                "state": {"phase": "tasks"},
            }
        }
    )
    monkeypatch.setattr(
        routes,
        "_same_spec_dir",
        lambda left, right: physically_equivalent
        and {left, right} == {original_dir, rewritten_dir},
    )

    conflict = await routes._final_alias_conflict(
        types.SimpleNamespace(get_slot=lambda _key: None),
        original_dir,
        slot_key,
        {},
        {},
        own_name="s",
    )

    assert conflict == "s"


@pytest.mark.asyncio
async def test_final_alias_scan_refuses_own_slot_identity_rewrite(tmp_path, monkeypatch):
    """A rewritten current entry cannot move Stop onto a different slot."""
    _redirect_state(monkeypatch, tmp_path)
    spec_dir = "/physical/spec"
    original_slot = "spec-builder-s-1234abcd"
    routes._save_index(
        {
            "s": {
                "spec_dir": spec_dir,
                "slot_key": "spec-builder-s-deadbeef",
                "state": {"phase": "tasks"},
            }
        }
    )
    monkeypatch.setattr(routes, "_same_spec_dir", lambda left, right: left == right)

    conflict = await routes._final_alias_conflict(
        types.SimpleNamespace(get_slot=lambda _key: None),
        spec_dir,
        original_slot,
        {},
        {},
        own_name="s",
    )

    assert conflict == "s"


@pytest.mark.asyncio
async def test_an_alias_mid_turn_blocks_an_ordinary_message(tmp_path, monkeypatch):
    """Not just decision answers: ANY dispatch would be a second agent on these files.

    Gating the alias check on `decision_id` left the plain message path wide open -- the
    ordinary way a user talks to a spec -- so two agents could edit one spec's documents
    concurrently while the decision lock looked after itself.
    """
    client = _make_client(monkeypatch, tmp_path)
    spec_dir, slot_key = _decision_spec(tmp_path)
    alias_key = _alias_of(await routes._aload_index(), "s", "s-alias")

    dispatched: list = []
    monkeypatch.setattr(routes, "_dispatch_turn", lambda *a, **k: dispatched.append(a[2]))
    state, _slot = _slot_stub()
    busy = types.SimpleNamespace(key=alias_key, running=True, messages=[])
    real_get_slot = state.get_slot
    state.get_slot = lambda k: busy if k == alias_key else real_get_slot(k)

    client.app["state"] = state
    await client.start_server()
    try:
        resp = await client.post(
            f"{_BASE}/specs/s/message",
            json={"spec_dir": spec_dir, "slot_key": slot_key, "text": "just a note"},
        )
        payload = await resp.json()
    finally:
        await client.close()

    assert resp.status == 409, payload
    assert payload.get("code") == "spec_busy_elsewhere", payload
    assert dispatched == [], "a turn started while another view's agent was working"


@pytest.mark.asyncio
async def test_a_completed_alias_turn_during_repin_blocks_an_ordinary_message(
    tmp_path, monkeypatch
):
    """An ordinary message's final gate sees alias history across its await."""
    client = _make_client(monkeypatch, tmp_path)
    spec_dir, slot_key = _decision_spec(tmp_path)
    alias_key = _alias_of(await routes._aload_index(), "s", "s-alias")
    from kiro_crew.dashboard.state import _ChatSlot

    alias_slot = _ChatSlot(alias_key)
    state, _slot = _slot_stub()
    real_get_slot = state.get_slot
    state.get_slot = lambda key: alias_slot if key == alias_key else real_get_slot(key)
    real_touch = routes._touch_spec
    touches = 0

    async def _touch_while_alias_turn_completes(*args, **kwargs):
        nonlocal touches
        result = await real_touch(*args, **kwargs)
        touches += 1
        if touches == 2:
            alias_slot.task = asyncio.create_task(asyncio.sleep(0))
            await alias_slot.task
            alias_slot.append("done", "", "done")
            alias_slot.task = None
        return result

    monkeypatch.setattr(routes, "_touch_spec", _touch_while_alias_turn_completes)
    dispatched: list[str] = []
    monkeypatch.setattr(routes, "_dispatch_turn", lambda *a, **k: dispatched.append(a[2]))

    client.app["state"] = state
    await client.start_server()
    try:
        resp = await client.post(
            f"{_BASE}/specs/s/message",
            json={"spec_dir": spec_dir, "slot_key": slot_key, "text": "just a note"},
        )
        payload = await resp.json()
    finally:
        await client.close()

    assert resp.status == 409, payload
    assert payload.get("code") == "spec_busy_elsewhere", payload
    assert dispatched == []


@pytest.mark.asyncio
async def test_an_alias_missing_its_persisted_slot_key_refuses_a_message(tmp_path, monkeypatch):
    """An agent-writable index edit cannot hide an alias's active worker.

    A per-creation worker keeps running under the key minted when the alias was
    created. If the alias later drops ``slot_key`` from the index, falling back to
    its legacy name-derived key looks up a different, idle slot and admits a second
    agent over the same files. An ownership-invalid key has to make the alias
    occupied until its identity can be established again.
    """
    client = _make_client(monkeypatch, tmp_path)
    spec_dir, slot_key = _decision_spec(tmp_path)
    index = await routes._aload_index()
    alias_key = _alias_of(index, "s", "s-alias")

    edited = await routes._aload_index()
    edited["s-alias"].pop("slot_key")
    routes._save_index(edited)

    dispatched: list = []
    monkeypatch.setattr(routes, "_dispatch_turn", lambda *a, **k: dispatched.append(a[2]))
    state, _slot = _slot_stub()
    busy = types.SimpleNamespace(key=alias_key, running=True, messages=[])
    real_get_slot = state.get_slot
    state.get_slot = lambda key: busy if key == alias_key else real_get_slot(key)

    client.app["state"] = state
    await client.start_server()
    try:
        resp = await client.post(
            f"{_BASE}/specs/s/message",
            json={"spec_dir": spec_dir, "slot_key": slot_key, "text": "just a note"},
        )
        payload = await resp.json()
    finally:
        await client.close()

    assert resp.status == 409, payload
    assert payload.get("code") == "spec_busy_elsewhere", payload
    assert dispatched == [], "the missing alias identity hid its active worker"


@pytest.mark.asyncio
async def test_an_alias_mid_turn_blocks_a_handoff(tmp_path, monkeypatch):
    """...and a handoff, which starts an autonomous build -- the most expensive way to
    end up with two agents in one spec directory.

    Arming lives inside the turn lock and AFTER this check, so no loop is armed on
    this path at all: authorization is never reached, which leaves nothing to leak and
    no release to get right. Arming ahead of the lock instead leaves a bare 409 holding
    an active timer that dispatches the very build the refusal denied.
    """
    client = _make_client(monkeypatch, tmp_path)
    spec_dir, _slot_key = _decision_spec(tmp_path, state={"phase": "tasks"})
    (Path(spec_dir) / "tasks.md").write_text("- [ ] a task\n")
    alias_key = _alias_of(await routes._aload_index(), "s", "s-alias")

    state, _slot = _slot_stub()
    busy = types.SimpleNamespace(key=alias_key, running=True, messages=[])
    real_get_slot = state.get_slot
    state.get_slot = lambda k: busy if k == alias_key else real_get_slot(k)

    armed: list = []
    monkeypatch.setattr(routes, "_dispatch_turn", lambda *a, **k: armed.append(a[2]))
    monkeypatch.setattr(routes, "_autonudge_instance", lambda: object())

    authorized: list = []

    async def _authorized(**_kw):
        # (armed_loop, authz_err, status) -- the tuple the handler unpacks.
        authorized.append(True)
        return types.SimpleNamespace(id="loop-1"), "", 200

    monkeypatch.setattr(routes, "authorize_and_add_nudge", _authorized)

    released: list = []

    async def _remove(_slot_key, *, only_loop_id=None, stop_reason=""):
        released.append(only_loop_id)

    monkeypatch.setattr(routes, "_remove_nudge_loop_for_slot", _remove)

    client.app["state"] = state
    await client.start_server()
    try:
        resp = await client.post(f"{_BASE}/specs/s/handoff", json={"spec_dir": spec_dir})
        payload = await resp.json()
    finally:
        await client.close()

    assert resp.status == 409, payload
    assert payload.get("code") == "spec_busy_elsewhere", payload
    assert armed == [], "a build was armed while another view's agent was working"
    assert authorized == [], (
        "the handoff armed a nudge loop before checking whether an alias was mid-turn, "
        "so the refusal has a live timer to clean up again"
    )
    assert released == [], "nothing was armed, so nothing should have been released"


@pytest.mark.asyncio
async def test_a_completed_alias_turn_during_authorization_blocks_a_handoff(tmp_path, monkeypatch):
    """The final handoff gate sees a turn whose task returned to ``None``."""
    client = _make_client(monkeypatch, tmp_path)
    spec_dir, _slot_key = _decision_spec(tmp_path, state={"phase": "tasks"})
    (Path(spec_dir) / "tasks.md").write_text("- [ ] a task\n")
    alias_key = _alias_of(await routes._aload_index(), "s", "s-alias")
    from kiro_crew.dashboard.state import _ChatSlot

    alias_slot = _ChatSlot(alias_key)
    state, _slot = _slot_stub()
    real_get_slot = state.get_slot
    state.get_slot = lambda key: alias_slot if key == alias_key else real_get_slot(key)
    monkeypatch.setattr(routes, "_autonudge_instance", lambda: object())

    async def _authorized(**_kw):
        alias_slot.task = asyncio.create_task(asyncio.sleep(0))
        await alias_slot.task
        alias_slot.append("done", "", "done")
        alias_slot.task = None
        return types.SimpleNamespace(id="loop-alias-raced"), "", 200

    monkeypatch.setattr(routes, "authorize_and_add_nudge", _authorized)
    dispatched: list[str] = []
    monkeypatch.setattr(routes, "_dispatch_turn", lambda *a, **k: dispatched.append(a[2]))
    released: list[str] = []

    async def _remove(_slot_key, *, only_loop_id=None, stop_reason=""):
        released.append(only_loop_id)

    monkeypatch.setattr(routes, "_remove_nudge_loop_for_slot", _remove)

    client.app["state"] = state
    await client.start_server()
    try:
        resp = await client.post(f"{_BASE}/specs/s/handoff", json={"spec_dir": spec_dir})
        payload = await resp.json()
    finally:
        await client.close()

    assert resp.status == 409, payload
    assert payload.get("code") == "spec_busy_elsewhere", payload
    assert dispatched == []
    assert released == ["loop-alias-raced"]
    assert routes._load_index()["s"].get("status") == "planning"


@pytest.mark.asyncio
async def test_the_alias_set_excludes_our_own_slot(tmp_path, monkeypatch):
    """Our own slot is the same session, and must never read as a concurrent editor.

    That exclusion is what preserves same-slot queuing: a message to the session that is
    working is queued by _dispatch_turn, the long-standing behaviour. Only ANOTHER name's
    slot is a second agent on these documents, and only that refuses.
    """
    _redirect_state(monkeypatch, tmp_path)
    spec_dir, slot_key = _decision_spec(tmp_path)
    alias_key = _alias_of(await routes._aload_index(), "s", "s-alias")

    key = routes._decision_key(spec_dir)
    aliases = await routes._alias_slots(key, own_slot_key=slot_key)

    assert aliases == {alias_key: "s-alias"}
    assert slot_key not in aliases, "our own running slot would read as a concurrent editor"


@pytest.mark.asyncio
async def test_an_alias_with_an_armed_build_blocks_the_answer(tmp_path, monkeypatch):
    """A running turn is not the only way an alias occupies these documents.

    An autonudge execution loop sits IDLE between its nudge cycles, so a busy check that
    asks only whether the slot is running right now reads a live build as free: the other
    name dispatches, then the loop's timer fires, and two agents write one spec's files.
    The loop is as much an occupant as the turn it periodically starts.
    """
    client = _make_client(monkeypatch, tmp_path)
    spec_dir, slot_key = _decision_spec(tmp_path)
    alias_key = _alias_of(await routes._aload_index(), "s", "s-alias")

    dispatched: list = []
    monkeypatch.setattr(routes, "_dispatch_turn", lambda *a, **k: dispatched.append(a[2]))

    # The alias's slot is IDLE -- only its loop is armed, which is the whole point.
    state, _slot = _slot_stub()
    idle = types.SimpleNamespace(key=alias_key, running=False, messages=[])
    real_get_slot = state.get_slot
    state.get_slot = lambda k: idle if k == alias_key else real_get_slot(k)

    class _Svc:
        def get_by_slot(self, key):
            return types.SimpleNamespace(active=True) if key == alias_key else None

    monkeypatch.setattr(routes, "_autonudge_instance", lambda: _Svc())

    client.app["state"] = state
    await client.start_server()
    try:
        resp = await client.post(
            f"{_BASE}/specs/s/message",
            json={
                "spec_dir": spec_dir,
                "slot_key": slot_key,
                "decision_id": "transport",
                "decision_option": "HTTPS",
                "text": "Decision - transport: HTTPS",
            },
        )
        payload = await resp.json()
    finally:
        await client.close()

    assert resp.status == 409, payload
    assert payload.get("code") == "spec_busy_elsewhere", payload
    assert dispatched == [], "an answer was dispatched while an alias's build was armed"
    assert (
        await _recorded_answers(spec_dir) == {}
    ), "the answer was recorded even though nothing was dispatched"


@pytest.mark.asyncio
async def test_a_finished_alias_loop_does_not_block(tmp_path, monkeypatch):
    """...and a loop the service has already deactivated is NOT an occupant.

    The loop is capped, and the service flips `active` itself without telling this app, so
    treating any registry hit as busy would strand every later dispatch behind a build that
    finished.
    """
    client = _make_client(monkeypatch, tmp_path)
    spec_dir, slot_key = _decision_spec(tmp_path)
    alias_key = _alias_of(await routes._aload_index(), "s", "s-alias")

    dispatched: list = []
    monkeypatch.setattr(routes, "_dispatch_turn", lambda *a, **k: dispatched.append(a[2]))
    state, _slot = _slot_stub()
    idle = types.SimpleNamespace(key=alias_key, running=False, messages=[])
    real_get_slot = state.get_slot
    state.get_slot = lambda k: idle if k == alias_key else real_get_slot(k)

    class _Svc:
        def get_by_slot(self, key):
            return types.SimpleNamespace(active=False)

    monkeypatch.setattr(routes, "_autonudge_instance", lambda: _Svc())

    client.app["state"] = state
    await client.start_server()
    try:
        resp = await client.post(
            f"{_BASE}/specs/s/message",
            json={"spec_dir": spec_dir, "slot_key": slot_key, "text": "just a note"},
        )
    finally:
        await client.close()

    assert resp.status == 200, await resp.text()
    assert dispatched == ["just a note"]


def test_the_busy_check_reads_no_filesystem():
    """Source guard: `_busy_alias` runs ON the event loop, so it must ask the nudge
    registry by SLOT KEY and derive nothing itself.

    Not because `_slot_key` is itself a filesystem hop -- it reads the module-global
    `_SLOT_KEYS`, not the index. The reason is that key derivation belongs in ONE
    off-loop place (`_alias_slots_locked`), because that is what makes every alias key
    ownership-validated instead of trusted raw from an agent-writable file. Re-deriving
    per call here would also invite an unvalidated shortcut back in.
    """
    src = inspect.getsource(routes._busy_alias)
    assert "_exec_loop_active_for_slot(" in src, "the busy check ignores an armed loop"
    assert "_exec_loop_active(" not in src.replace(
        "_exec_loop_active_for_slot(", ""
    ), "the busy check resolves a loop BY NAME, re-deriving a key it was handed"
    assert "_slot_key(" not in src, "the busy check derives a key instead of using the resolved one"


def test_alias_keys_are_resolved_not_trusted_from_the_index():
    """Source guard: the alias scan must not read ``slot_key`` raw from the entry.

    index.json is agent-writable, so a raw read lets an alias hide from the busy
    scan. This is the one place the resolution happens, so pin it here.
    """
    src = inspect.getsource(routes._alias_slots_locked)
    assert "_slot_key(other)" in src, "alias keys are not resolved through the validated map"
    assert "_owns_slot_key(other, persisted)" in src
    assert "out[persisted]" not in src, "the alias scan trusts the raw persisted key"


def _two_names_one_dir(tmp_path, *, alias_key):
    """Index two names on ONE spec directory; the alias carries ``alias_key``.

    ``alias_key`` is written verbatim, which is the point: the field is
    agent-writable, so the tests drive the values an agent could actually put there.
    """
    spec_dir = tmp_path / "proj" / ".kiro" / "specs" / "shared"
    spec_dir.mkdir(parents=True)
    entry = {
        "working_dir": str(tmp_path / "proj"),
        "spec_dir": str(spec_dir),
        "spec_type": "feature",
        "status": "planning",
        "created_at": 1.0,
        "updated_at": 1.0,
    }
    index = {
        "mine": {**entry, "slot_key": "spec-builder-mine-aaaaaaaa"},
        "alias": {**entry, "slot_key": alias_key},
    }
    routes._save_index(index)
    return str(spec_dir)


@pytest.mark.asyncio
async def test_an_alias_with_no_slot_key_is_still_seen(tmp_path, monkeypatch):
    """An alias whose ``slot_key`` field is deleted is still seen by the scan.

    Skipping an entry with no key lets an agent delete its own ``slot_key`` and become
    invisible to every busy check -- then both names run a turn over the same
    documents.
    """
    _redirect_state(monkeypatch, tmp_path)
    spec_dir = _two_names_one_dir(tmp_path, alias_key="")
    dir_key = routes._decision_key(spec_dir)

    aliases = await routes._alias_slots(dir_key, own_slot_key="spec-builder-mine-aaaaaaaa")

    assert "alias" in aliases.values(), (
        "an alias with no slot_key vanished from the scan, so a concurrent turn "
        "under it would not block"
    )
    # Missing identity fails closed because a per-creation worker may still be running
    # under the removed key; guessing the legacy key would make it invisible.
    assert aliases == {None: "alias"}, aliases


@pytest.mark.asyncio
async def test_an_invalid_identity_cannot_claim_the_own_slot_exemption(tmp_path, monkeypatch):
    """The fallback key is not proof that it names the running creation.

    Removing a per-creation key makes ``_slot_key`` fall back to the legacy key. If
    that fallback is compared with ``own_slot_key`` before ownership validation, the
    invalid entry disappears from the busy scan even though its original worker may
    still be editing the directory.
    """
    _redirect_state(monkeypatch, tmp_path)
    spec_dir = Path(tmp_path, "spec")
    spec_dir.mkdir()
    routes._save_index(
        {
            "mine": {
                "working_dir": str(tmp_path / "proj"),
                "spec_dir": str(spec_dir),
                "spec_type": "feature",
                "status": "planning",
                "created_at": 1.0,
                "updated_at": 1.0,
            }
        }
    )

    aliases = await routes._alias_slots(
        routes._decision_key(str(spec_dir)), own_slot_key="spec-builder-mine"
    )

    assert aliases == {None: "mine"}, (
        "an ownership-invalid entry used its guessed legacy key to disappear as "
        f"the caller's own slot: {aliases}"
    )


@pytest.mark.asyncio
async def test_an_alias_that_copies_our_key_is_still_seen(tmp_path, monkeypatch):
    """Copying the caller's key must not make the alias read as "our own slot".

    Own-slot exclusion exists so a same-session message can queue rather than be
    refused. Keyed on a field the agent writes, it would be a way to borrow that
    exemption -- ownership validation rejects a key that does not encode the
    alias's own name, so the copy cannot be claimed.
    """
    _redirect_state(monkeypatch, tmp_path)
    spec_dir = _two_names_one_dir(tmp_path, alias_key="spec-builder-mine-aaaaaaaa")
    dir_key = routes._decision_key(spec_dir)

    aliases = await routes._alias_slots(dir_key, own_slot_key="spec-builder-mine-aaaaaaaa")

    assert aliases == {
        None: "alias"
    }, f"the alias borrowed our slot key to escape the busy scan: {aliases}"


@pytest.mark.asyncio
async def test_a_legitimate_per_creation_key_still_resolves(tmp_path, monkeypatch):
    """The boundary: a valid, owned key must survive resolution unchanged.

    Otherwise the scan would look for a slot the alias does not actually run in,
    and a genuinely busy alias would read as free.
    """
    _redirect_state(monkeypatch, tmp_path)
    spec_dir = _two_names_one_dir(tmp_path, alias_key="spec-builder-alias-bbbbbbbb")
    dir_key = routes._decision_key(spec_dir)

    aliases = await routes._alias_slots(dir_key, own_slot_key="spec-builder-mine-aaaaaaaa")

    assert aliases == {"spec-builder-alias-bbbbbbbb": "alias"}, aliases


@pytest.mark.asyncio
async def test_our_own_name_is_still_excluded(tmp_path, monkeypatch):
    """Resolution must not break same-session queuing.

    Our own slot is excluded so an ordinary message queues instead of being
    refused; resolving keys must leave that exclusion working.
    """
    _redirect_state(monkeypatch, tmp_path)
    spec_dir = _two_names_one_dir(tmp_path, alias_key="spec-builder-alias-bbbbbbbb")
    dir_key = routes._decision_key(spec_dir)

    aliases = await routes._alias_slots(dir_key, own_slot_key="spec-builder-alias-bbbbbbbb")

    assert aliases == {
        "spec-builder-mine-aaaaaaaa": "mine"
    }, f"own-slot exclusion stopped working after resolution: {aliases}"


@pytest.mark.asyncio
async def test_create_refuses_a_second_name_for_the_same_normalized_directory(
    tmp_path, monkeypatch
):
    """Create arbitration uses the directory identity, not only the label.

    Windows treats case variants as one path. Without a directory collision check,
    two names could each receive a slot and dispatch an agent into the same files.
    Patching ``normcase`` makes that filesystem identity deterministic on every host.
    """
    project = tmp_path / "proj"
    project.mkdir()
    client, state = _create_client(monkeypatch, tmp_path)
    monkeypatch.setattr(routes.os.path, "normcase", lambda path: path.lower())
    dispatched: list[str] = []
    monkeypatch.setattr(routes, "_dispatch_turn", lambda *_a, **_kw: dispatched.append("x"))

    client.app["state"] = state
    await client.start_server()
    try:
        first = await client.post(
            f"{_BASE}/specs",
            json={"name": "CaseSpec", "working_dir": str(project), "spec_type": "feature"},
        )
        second = await client.post(
            f"{_BASE}/specs",
            json={"name": "casespec", "working_dir": str(project), "spec_type": "feature"},
        )
        first_body = await first.json()
        second_body = await second.json()
    finally:
        await client.close()

    assert first.status == 201, first_body
    assert second.status == 409, second_body
    assert second_body["code"] == "spec_dir_in_use"
    assert set(routes._load_index()) == {"CaseSpec"}
    assert dispatched == ["x"], "both names dispatched agents into one directory"


@pytest.mark.asyncio
@pytest.mark.skipif(
    sys.platform == "win32",
    reason=(
        "a case-only spelling is not a simulable alias on Windows: the second "
        "create's leaf already exists, so Path.resolve() canonicalizes that "
        "spelling back onto the first spec directory and the two creates never "
        "hold the distinct resolved paths this scenario is about"
    ),
)
async def test_create_refuses_a_macos_case_alias_for_the_same_directory(tmp_path, monkeypatch):
    """Create asks the filesystem whether differently cased paths are aliases."""
    project = tmp_path / "proj"
    project.mkdir()
    client, state = _create_client(monkeypatch, tmp_path)
    monkeypatch.setattr(routes, "_CASE_FOLD_TURN_KEYS", True)
    monkeypatch.setattr(routes.os.path, "normcase", lambda path: path)
    monkeypatch.setattr(
        routes.os.path,
        "samefile",
        lambda left, right: os.path.normpath(left).casefold() == os.path.normpath(right).casefold(),
    )
    dispatched: list[str] = []
    monkeypatch.setattr(routes, "_dispatch_turn", lambda *_a, **_kw: dispatched.append("x"))

    client.app["state"] = state
    await client.start_server()
    try:
        responses = await asyncio.gather(
            client.post(
                f"{_BASE}/specs",
                json={
                    "name": "CaseSpec",
                    "working_dir": str(project),
                    "spec_type": "feature",
                },
            ),
            client.post(
                f"{_BASE}/specs",
                json={
                    "name": "casespec",
                    "working_dir": str(project),
                    "spec_type": "feature",
                },
            ),
        )
        bodies = [await response.json() for response in responses]
    finally:
        await client.close()

    statuses = [response.status for response in responses]
    assert sorted(statuses) == [201, 409], list(zip(statuses, bodies))
    refusal = bodies[statuses.index(409)]
    # The loser's refusal code depends on the filesystem, not on the race: the
    # casefolded turn lock serializes both creates, and when the two spellings
    # survive as distinct resolved paths (case-sensitive volumes) the loser hits
    # the ``samefile`` alias probe and is refused as
    # ``decision_directory_alias_conflict``. When the volume collapses the second
    # spelling onto the winner's directory, the resolved keys become lexically
    # equal, the alias scan skips them, and the lexical duplicate check refuses as
    # ``spec_dir_in_use``. Both refusals satisfy the invariant this test locks in —
    # exactly one create succeeds and both spellings map to one directory — so
    # either code is a correct outcome. Windows produced NEITHER (it refused on a
    # containment arm instead), which is why the shard is skipped rather than
    # having a third code added here; see the skipif above.
    assert refusal["code"] in {"spec_dir_in_use", "decision_directory_alias_conflict"}
    assert len(routes._load_index()) == 1
    assert dispatched == ["x"]


@pytest.mark.asyncio
@pytest.mark.parametrize("import_existing", [False, True])
async def test_create_refuses_an_equivalent_orphan_ledger_before_inserting(
    tmp_path, monkeypatch, import_existing
):
    """A protected record under another spelling cannot poison a new create."""
    client, state = _create_client(monkeypatch, tmp_path)
    _macos_case_aliases(monkeypatch)
    # Use two lexically distinct sibling paths and make the filesystem report them
    # as aliases. A case-only spelling is not a valid simulation on Windows:
    # ``Path.resolve()`` canonicalizes ``S`` back to the existing ``s`` directory,
    # so both spellings have the same ledger key and there is no alias to reject.
    monkeypatch.setattr(routes.os.path, "samefile", lambda _left, _right: True)
    spec_dir, slot_key = _decision_spec(tmp_path)
    outcome, _held = await routes._claim_decision(
        "s",
        "transport",
        "HTTPS",
        expect_spec_dir=spec_dir,
        expect_slot_key=slot_key,
    )
    assert outcome == routes._CLAIM_RECORDED
    routes._save_index({})
    dispatched: list[str] = []
    monkeypatch.setattr(routes, "_dispatch_turn", lambda *_a, **_kw: dispatched.append("x"))

    project = Path(spec_dir).parents[2]
    client.app["state"] = state
    await client.start_server()
    try:
        response = await client.post(
            f"{_BASE}/specs",
            json={
                "name": "alias",
                "working_dir": str(project),
                "spec_type": "feature",
                "import_existing": import_existing,
            },
        )
        body = await response.json()
    finally:
        await client.close()

    assert response.status == 409, body
    assert body["code"] == "decision_directory_alias_conflict"
    assert routes._load_index() == {}
    assert dispatched == []
    assert await _recorded_answers(spec_dir) == {"transport": "HTTPS"}
