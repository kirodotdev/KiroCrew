"""Route tests: the decision ledger store.

Where the ledger lives (on the security keystone, outside the agent-writable
index and the app's state dir), how a torn, unreadable, undecodable or full file
is treated, how its key is derived from the directory, and how delete and create
clear or keep a directory's recorded answers.
"""

from __future__ import annotations

import ast
import asyncio
import inspect
import json
import os
import textwrap
from pathlib import Path

import pytest

from kiro_crew.apps.builtins.spec_builder.tests.routes_facade import routes, routes_source

# _never_touch_the_real_state is an autouse fixture: pytest finds it by the name bound here.
from kiro_crew.apps.builtins.spec_builder.tests.routes_helpers import (  # noqa: F401
    _BASE,
    _create_client,
    _decision_record,
    _decision_spec,
    _final_answer,
    _macos_case_aliases,
    _make_client,
    _never_touch_the_real_state,
    _recorded_answers,
    _redirect_state,
    _slot_stub,
)


@pytest.mark.asyncio
async def test_a_full_ledger_refuses_rather_than_dispatching_unrecorded(tmp_path, monkeypatch):
    """The cap fails CLOSED. Dispatching an answer this backend could not record
    would produce exactly the re-answerable card the ledger exists to prevent."""
    client = _make_client(monkeypatch, tmp_path)
    spec_dir, slot_key = _decision_spec(
        tmp_path,
        state={"decisions": [{"id": "one-too-many", "title": "Last decision", "options": ["A"]}]},
    )
    # Simulate Windows, where the ledger contract case-folds every directory key.
    monkeypatch.setattr(routes.os.path, "normcase", lambda path: path.upper())
    routes._save_decisions(
        {
            routes._decision_key(spec_dir): {
                "name": "s",
                "answers": {
                    f"d{i}": _final_answer(f"d{i}", "A") for i in range(routes._MAX_RECORDED)
                },
            }
        }
    )

    dispatched: list = []
    monkeypatch.setattr(routes, "_dispatch_turn", lambda *a, **k: dispatched.append(a[2]))

    state, _slot = _slot_stub()
    client.app["state"] = state
    await client.start_server()
    try:
        resp = await client.post(
            f"{_BASE}/specs/s/message",
            json={
                "spec_dir": spec_dir,
                "slot_key": slot_key,
                "decision_id": "one-too-many",
                "decision_option": "A",
                "text": "A",
            },
        )
        payload = await resp.json()
    finally:
        await client.close()

    assert resp.status == 409, payload
    assert payload.get("code") == "decision_ledger_full", payload
    assert dispatched == []


@pytest.mark.asyncio
async def test_the_ledger_key_matches_the_id_the_detail_read_serves(tmp_path, monkeypatch):
    """The overlay matches the ledger KEY against the id from the state file, so
    the two must be normalized identically.

    Stripping an id that carries whitespace (or exceeds a tighter cap) on the wire
    but not in the state projection leaves a mismatch. It is silent in the
    worst way: the answer IS recorded, no card is ever locked, and the decision
    stays re-answerable forever."""
    client = _make_client(monkeypatch, tmp_path)
    raw_id = "  transport shape  "
    spec_dir, slot_key = _decision_spec(
        tmp_path,
        state={"decisions": [{"id": raw_id, "title": "Inbound transport", "options": ["HTTPS"]}]},
    )

    def _dispatch(*_args, **kwargs):
        kwargs["on_consumed"](True)

    monkeypatch.setattr(routes, "_dispatch_turn", _dispatch)

    state, _slot = _slot_stub()
    client.app["state"] = state
    await client.start_server()
    try:
        # The SPA echoes back the id the detail read gave it, verbatim.
        served = routes._normalize_spec_state(
            json.loads((Path(spec_dir) / ".spec-state.json").read_text())
        )
        assert served is not None
        served_id = served["decisions"][0]["id"]
        resp = await client.post(
            f"{_BASE}/specs/s/message",
            json={
                "spec_dir": spec_dir,
                "slot_key": slot_key,
                "decision_id": served_id,
                "decision_option": "HTTPS",
                "text": "Decision - Inbound transport: HTTPS",
            },
        )
        assert resp.status == 200, await resp.json()
        if state._background_tasks:
            await asyncio.gather(*list(state._background_tasks))
        detail = await (await client.get(f"{_BASE}/specs/s")).json()
    finally:
        await client.close()

    decision = detail["state"]["decisions"][0]
    assert (
        decision["locked"] is True
    ), "the recorded answer did not match the served id, so the card is still clickable"
    assert decision["answer"] == "HTTPS"


def test_the_decision_ledger_is_not_in_the_agent_writable_index():
    """The record must not live on ``index.json``.

    That file is agent-writable by design (its own docstrings say so), so a ledger
    field on it could be erased to re-open a settled decision or forged to lock one
    the user never answered -- and a forged entry also puts an answer on screen
    that nobody chose."""
    src = routes_source()
    assert (
        "answered_decisions" not in src
    ), "the ledger is back on an index entry, where the agent can rewrite it"
    write = inspect.getsource(routes._claim_decision_locked)
    assert "_mutate_index(" not in write, (
        "the claim writes through the index mutator again, so the record is " "agent-writable"
    )
    assert "_DECISIONS_LOCK" in write
    assert "_save_decisions(" in write


@pytest.mark.parametrize(
    "path",
    [
        "~/.kiro/crew/trust/spec-builder-decisions.json",
        "~/.kirocrew/trust/spec-builder-decisions.json",
        # The PARENT, which is the half a leaf-only entry missed.
        "~/.kiro/crew/trust",
        "~/.kirocrew/trust",
    ],
)
def test_the_decision_ledger_is_on_the_security_keystone(path):
    """Read+write keystone, like the Notes vault registry and the Ops Mission
    Control policy: app-owned, not a secret, but forging or erasing it defeats the
    app's safety property, so the agent must not reach it through the file tools
    (the shell is confined by the OS sandbox, not by a matcher over command text).

    The PARENT is gated too. Under this app's own state dir it was not: a directory
    below ``workspace/`` is not a sensitive path, so one ``ln -s`` naming it
    redirected every read and write, and this backend opens the path directly (as
    keystone writers must) so it would have followed the link."""
    from kiro_crew import security

    assert security.is_sensitive_path(path)


def test_the_ledger_is_not_under_the_apps_own_state_dir(tmp_path, monkeypatch):
    """Source/behaviour guard: the default location must be the gated trust root.

    Anything under the app's state dir has a replaceable parent, which is the defect
    this move fixes -- and it would come back silently, because the app opens the
    path directly and a symlinked parent reads and writes exactly like a real one."""
    monkeypatch.setattr(routes, "_DECISIONS_PATH", None)
    monkeypatch.setattr(routes, "_STATE_DIR", tmp_path / "state")

    path = routes._decisions_path()

    assert path.parent.name == "trust", path
    assert (tmp_path / "state") not in path.parents, path
    from kiro_crew import security

    assert security.is_sensitive_path(str(path))
    assert security.is_sensitive_path(str(path.parent))


@pytest.mark.asyncio
async def test_a_forged_ledger_for_another_spec_dir_is_ignored(tmp_path, monkeypatch):
    """A record is scoped to the spec_dir it was written for. A delete leaves the
    documents on disk, so a re-import under the same name at a different path is a
    DIFFERENT spec and must start with no answers."""
    _redirect_state(monkeypatch, tmp_path)
    spec_dir, _slot_key = _decision_spec(tmp_path)
    routes._save_decisions(
        {
            str(tmp_path / "somewhere-else"): {
                "name": "s",
                "answers": {"d": _final_answer("d", "A")},
            }
        }
    )

    assert await _recorded_answers(spec_dir) == {}


@pytest.mark.asyncio
async def test_deleting_a_spec_forgets_its_answers(tmp_path, monkeypatch):
    """Otherwise a re-import at the same name AND path inherits locks from the spec
    that was deleted, and its cards are settled before the user answers anything."""
    client = _make_client(monkeypatch, tmp_path)
    spec_dir, slot_key = _decision_spec(tmp_path)
    assert (
        await routes._claim_decision(
            "s", "transport", "HTTPS", expect_spec_dir=spec_dir, expect_slot_key=slot_key
        )
    )[0] == routes._CLAIM_RECORDED

    client.app["state"] = None
    await client.start_server()
    try:
        resp = await client.delete(f"{_BASE}/specs/s")
    finally:
        await client.close()

    assert resp.status == 200, await resp.text()
    assert await _recorded_answers(spec_dir) == {}


@pytest.mark.asyncio
async def test_a_torn_ledger_file_reads_as_nothing_recorded(tmp_path, monkeypatch):
    """Degrade toward answerable, never toward a locked card nobody can clear, and
    never toward raising inside a request."""
    _redirect_state(monkeypatch, tmp_path)
    spec_dir, _slot_key = _decision_spec(tmp_path)
    routes._decisions_path().parent.mkdir(parents=True, exist_ok=True)
    routes._decisions_path().write_text("{ not json")

    assert await _recorded_answers(spec_dir) == {}


@pytest.mark.asyncio
async def test_an_unreadable_ledger_refuses_the_write_instead_of_erasing_it(tmp_path, monkeypatch):
    """A corrupt read must not become data loss.

    Treating an unreadable file as an empty ledger and saving over it would erase
    every recorded answer -- for every spec -- and make settled decisions answerable
    again. Reads still fail soft (the card stays answerable, which is the safe
    direction); WRITES refuse."""
    client = _make_client(monkeypatch, tmp_path)
    spec_dir, slot_key = _decision_spec(tmp_path)
    routes._save_decisions(
        {
            "/elsewhere": {
                "name": "other",
                "answers": {"kept": _final_answer("kept", "yes")},
            }
        }
    )
    corrupt = routes._decisions_path().read_text()[:-3]  # truncate the JSON
    routes._decisions_path().write_text(corrupt)

    dispatched: list = []
    monkeypatch.setattr(routes, "_dispatch_turn", lambda *a, **k: dispatched.append(a[2]))

    state, _slot = _slot_stub()
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
    assert payload.get("code") == "decision_record_unreadable", payload
    assert dispatched == []
    # The unreadable file is left EXACTLY as it was rather than replaced.
    assert routes._decisions_path().read_text() == corrupt


@pytest.mark.asyncio
async def test_a_failed_index_removal_puts_the_answers_back(tmp_path, monkeypatch):
    """A spec that survives a failed delete keeps its answers.

    This is what fixes the ordering rather than compensating for it: the ledger is
    cleared only AFTER the index entry is actually gone, so a failure of the index write
    cannot leave the spec alive with its decisions answerable again. There is no restore
    path to get wrong, because nothing was removed.
    """
    client = _make_client(monkeypatch, tmp_path)
    spec_dir, slot_key = _decision_spec(tmp_path)
    assert (
        await routes._claim_decision(
            "s", "transport", "HTTPS", expect_spec_dir=spec_dir, expect_slot_key=slot_key
        )
    )[0] == routes._CLAIM_RECORDED

    real_mutate = routes._mutate_index
    calls: list[int] = []

    async def _fail_the_final_pop(mutate):
        calls.append(1)
        # The pop is the LAST index mutation of the delete; the reservation and
        # durable teardown boundary before it must still work.
        if len(calls) >= 3:
            return False
        return await real_mutate(mutate)

    monkeypatch.setattr(routes, "_mutate_index", _fail_the_final_pop)

    client.app["state"] = None
    await client.start_server()
    try:
        resp = await client.delete(f"{_BASE}/specs/s")
        payload = await resp.json()
    finally:
        await client.close()

    assert resp.status == 503, payload
    assert payload.get("code") == "index_write_failed", payload
    assert await _recorded_answers(spec_dir) == {
        "transport": "HTTPS"
    }, "the spec survived the failed delete with its recorded answers erased"


@pytest.mark.asyncio
async def test_a_raised_index_removal_still_restores_the_answers(tmp_path, monkeypatch):
    """``_mutate_index`` can RAISE as well as return False (a full data home), and both
    leave the spec in the index with its answers already cleared. The restore must run
    on either path, or a restart exposes the surviving spec with its settled decisions
    answerable again."""
    client = _make_client(monkeypatch, tmp_path)
    spec_dir, slot_key = _decision_spec(tmp_path)
    assert (
        await routes._claim_decision(
            "s", "transport", "HTTPS", expect_spec_dir=spec_dir, expect_slot_key=slot_key
        )
    )[0] == routes._CLAIM_RECORDED

    real_mutate = routes._mutate_index
    calls: list[int] = []

    async def _raise_on_the_final_pop(mutate):
        calls.append(1)
        if len(calls) >= 3:
            raise OSError(28, "No space left on device")
        return await real_mutate(mutate)

    monkeypatch.setattr(routes, "_mutate_index", _raise_on_the_final_pop)

    client.app["state"] = None
    await client.start_server()
    try:
        resp = await client.delete(f"{_BASE}/specs/s")
        payload = await resp.json()
    finally:
        await client.close()

    assert resp.status == 503, payload
    assert payload.get("code") == "index_write_failed", payload
    assert await _recorded_answers(spec_dir) == {"transport": "HTTPS"}


@pytest.mark.asyncio
async def test_the_ledger_parent_is_created_on_first_write(tmp_path, monkeypatch):
    """The ledger lives under ``trust/``, not this app's state dir, so on a fresh
    install its parent may not exist yet -- and the app's own mkdir is the only thing
    that creates it. Without this the FIRST answer of a new install fails to record."""
    _redirect_state(monkeypatch, tmp_path)
    # A parent that does not exist, exactly like a fresh install's trust root.
    ledger = tmp_path / "fresh-home" / "trust" / "spec-builder-decisions.json"
    monkeypatch.setattr(routes, "_DECISIONS_PATH", ledger)
    spec_dir, slot_key = _decision_spec(tmp_path)
    assert not ledger.parent.exists()

    outcome, _held = await routes._claim_decision(
        "s", "transport", "HTTPS", expect_spec_dir=spec_dir, expect_slot_key=slot_key
    )

    assert outcome == routes._CLAIM_RECORDED
    assert ledger.is_file()
    assert await _recorded_answers(spec_dir) == {"transport": "HTTPS"}


@pytest.mark.asyncio
async def test_a_delete_whose_cleanup_fails_still_deletes(tmp_path, monkeypatch):
    """The ledger cleanup is housekeeping, so it cannot fail a delete that has happened.

    The index entry and the conversation are already gone by then; refusing here would
    report a spec as alive when it is not. A leftover entry is bounded and only readable
    by a spec re-created at the same name AND path -- which, by the identity this ledger
    uses, is the same spec.
    """
    client = _make_client(monkeypatch, tmp_path)
    spec_dir, slot_key = _decision_spec(tmp_path)
    assert (
        await routes._claim_decision(
            "s", "transport", "HTTPS", expect_spec_dir=spec_dir, expect_slot_key=slot_key
        )
    )[0] == routes._CLAIM_RECORDED

    def _boom(_store):
        raise OSError("read-only data home")

    monkeypatch.setattr(routes, "_save_decisions", _boom)

    client.app["state"] = None
    await client.start_server()
    try:
        resp = await client.delete(f"{_BASE}/specs/s")
        payload = await resp.json()
    finally:
        await client.close()

    assert resp.status == 200, payload
    assert "s" not in await routes._aload_index()


@pytest.mark.asyncio
async def test_a_delete_clears_the_record_after_the_index_entry(tmp_path, monkeypatch):
    """...and on the happy path the entry IS cleared, so the ledger does not accumulate
    answers for specs that have been deleted."""
    client = _make_client(monkeypatch, tmp_path)
    spec_dir, slot_key = _decision_spec(tmp_path)
    assert (
        await routes._claim_decision(
            "s", "transport", "HTTPS", expect_spec_dir=spec_dir, expect_slot_key=slot_key
        )
    )[0] == routes._CLAIM_RECORDED

    client.app["state"] = None
    await client.start_server()
    try:
        resp = await client.delete(f"{_BASE}/specs/s")
    finally:
        await client.close()

    assert resp.status == 200, await resp.text()
    assert await _recorded_answers(spec_dir) == {}


def test_the_delete_clears_the_ledger_after_the_index_write():
    """Source guard on that order. Clearing FIRST is what let a crash strand a surviving
    spec with its answers erased."""
    src = inspect.getsource(routes._handle_delete)
    pop = src.index("await _mutate_index(", src.index("def _pop_if_same"))
    assert pop < src.index(
        "_forget_decisions("
    ), "the ledger is cleared before the index entry is removed"


def test_an_undecodable_record_is_unusable_not_an_exception(tmp_path, monkeypatch):
    """``read_text`` decodes, so non-UTF-8 bytes raise UnicodeDecodeError -- a
    ValueError, which the OSError handler does not catch.

    Every caller is built around the (store, usable) contract, so an undecodable file
    has to arrive as "exists but unusable": the detail read fails soft and a claim
    refuses. Left uncaught it left this function by raising, which is a 500 carrying no
    code the client could act on.
    """
    path = tmp_path / "spec-builder-decisions.json"
    path.write_bytes(b'{"/x": {"name": "s", "answers": {"d": "\xff\xfe not utf-8"}}}')
    monkeypatch.setattr(routes, "_DECISIONS_PATH", path)

    store, usable = routes._read_decisions()

    assert store == {}
    assert usable is False, "an undecodable ledger reported itself as writable"


def test_a_recorded_answer_round_trips_non_ascii(tmp_path, monkeypatch):
    """The ledger's encoding is a property of the FILE, not of the host reading it.

    ``atomic_write`` always emits UTF-8 while ``read_text()`` without an encoding decodes
    with the platform default -- the ANSI code page on Windows. Under that asymmetry an
    option carrying an em dash came back mojibake, so the locked card displayed an answer
    the user never chose. Asserting the exact string back is what pins the explicit
    encoding on the read; the undecodable-bytes test above cannot, since on a UTF-8 host
    both spellings behave alike.
    """
    monkeypatch.setattr(routes, "_DECISIONS_PATH", tmp_path / "spec-builder-decisions.json")
    option = "HTTPS — with mTLS (日本語, café)"
    # A real directory, keyed the way the code keys it: a hard-coded "/x" resolves to a
    # drive-anchored path on Windows, so the store key and the lookup key disagreed there.
    spec_dir = tmp_path / "the-spec"
    spec_dir.mkdir()
    key = routes._decision_key(str(spec_dir))

    routes._save_decisions(
        {key: {"name": "s", "answers": {"transport": _final_answer("transport", option)}}}
    )
    store, usable = routes._read_decisions()

    assert usable is True
    assert _decision_record(store, str(spec_dir)) == {"transport": option}


@pytest.mark.asyncio
async def test_a_renamed_spec_keeps_its_recorded_answers(tmp_path, monkeypatch):
    """The other side of the same coin: renaming the index entry cannot reopen a
    decision, because the record is not keyed on the name.

    This is what makes an index rewrite structurally unable to reach a settled answer,
    rather than something that has to be detected and refused.
    """
    _redirect_state(monkeypatch, tmp_path)
    spec_dir, slot_key = _decision_spec(tmp_path)
    assert (
        await routes._claim_decision(
            "s", "transport", "HTTPS", expect_spec_dir=spec_dir, expect_slot_key=slot_key
        )
    )[0] == routes._CLAIM_RECORDED

    index = await routes._aload_index()
    index["renamed"] = index.pop("s")
    routes._save_index(index)

    assert await _recorded_answers(spec_dir) == {"transport": "HTTPS"}
    outcome, held = await routes._claim_decision(
        "renamed",
        "transport",
        "Streaming",
        expect_spec_dir=spec_dir,
        expect_slot_key=index["renamed"]["slot_key"],
    )
    assert outcome == routes._CLAIM_TAKEN, "a rename reopened a settled decision"
    assert held == "HTTPS"


def test_the_ledger_key_touches_no_filesystem(tmp_path):
    """The property that makes a settled decision un-reopenable by the agent.

    Replaces test_nothing_canonicalizes_a_path_on_the_event_loop, which policed WHERE
    the resolving key could be computed. Purity makes that concern structurally
    impossible instead: the key cannot move, so it cannot be moved off a record, and
    there is no filesystem call to keep off the event loop.
    """
    real = tmp_path / "spec"
    real.mkdir()
    key_before = routes._decision_key(str(real))

    # Replace the directory with a symlink to somewhere else -- exactly the swap an
    # agent can perform on its own spec directory.
    elsewhere = tmp_path / "elsewhere"
    elsewhere.mkdir()
    real.rmdir()
    real.symlink_to(elsewhere, target_is_directory=True)

    assert routes._decision_key(str(real)) == key_before, (
        "swapping the directory moved the ledger key, so a settled decision would read "
        "as unanswered and the card would re-open"
    )
    # And the derivation itself calls nothing but lexical helpers. Checked on the AST,
    # not the text: a substring scan matches the docstring that explains the rule.
    fn = ast.parse(textwrap.dedent(inspect.getsource(routes._decision_key))).body[0]
    called = set()
    for inner in ast.walk(fn):
        if isinstance(inner, ast.Call):
            target = inner.func
            called.add(
                target.attr
                if isinstance(target, ast.Attribute)
                else getattr(target, "id", "<expr>")
            )
    assert called <= {
        "normcase",
        "normpath",
    }, f"the ledger key derivation calls more than lexical helpers: {sorted(called)}"


@pytest.mark.asyncio
async def test_a_delete_clears_the_record_for_that_folder_only(tmp_path, monkeypatch):
    """The delete clears by directory, so it cannot reach another spec's record."""
    client = _make_client(monkeypatch, tmp_path)
    spec_dir, slot_key = _decision_spec(tmp_path)
    other = str(tmp_path / "another-spec")
    assert (
        await routes._claim_decision(
            "s", "transport", "HTTPS", expect_spec_dir=spec_dir, expect_slot_key=slot_key
        )
    )[0] == routes._CLAIM_RECORDED
    store, _usable = routes._read_decisions()
    store[routes._decision_key(other)] = {
        "name": "other",
        "answers": {"kept": _final_answer("kept", "yes")},
    }
    routes._save_decisions(store)

    client.app["state"] = None
    await client.start_server()
    try:
        resp = await client.delete(f"{_BASE}/specs/s")
    finally:
        await client.close()

    assert resp.status == 200, await resp.text()
    assert await _recorded_answers(spec_dir) == {}
    assert await _recorded_answers(other) == {"kept": "yes"}


def test_an_unresolvable_directory_keeps_its_normalized_literal_key(tmp_path, monkeypatch):
    """A lexical key normalizes even when the directory cannot be resolved."""
    loop_a = tmp_path / "A"
    loop_b = tmp_path / "B"
    loop_a.symlink_to(loop_b)
    loop_b.symlink_to(loop_a)
    monkeypatch.setattr(routes.os.path, "normcase", lambda path: path.lower())

    assert routes._decision_key(str(loop_a)) == os.path.normpath(str(loop_a)).lower()


@pytest.mark.asyncio
async def test_creating_a_spec_clears_an_orphaned_record_at_its_path(tmp_path, monkeypatch):
    """The residue a failed delete-cleanup leaves must not settle the NEXT spec.

    A delete clears the ledger only after the index entry is gone, and only
    best-effort, so a crash in that window leaves answers for documents that no
    longer exist. Decision ids are agent-authored labels that recur across specs,
    so without this the new spec's own question renders locked to an answer the
    user never gave for it -- and answering it is refused.
    """
    project = tmp_path / "proj"
    project.mkdir()
    spec_dir = project / ".kiro" / "specs" / "reborn"
    client, state = _create_client(monkeypatch, tmp_path)

    # The residue: a record for this exact directory, with no index entry and no
    # documents -- precisely what a delete whose cleanup failed leaves behind.
    key = routes._decision_key(str(spec_dir))
    routes._save_decisions(
        {
            key: {
                "name": "reborn",
                "answers": {"transport": _final_answer("transport", "HTTPS")},
            }
        }
    )
    routes._save_index({})
    # The fixture must be readable through the redirect, or this test proves nothing.
    assert await _recorded_answers(str(spec_dir)) == {"transport": "HTTPS"}

    client.app["state"] = state
    await client.start_server()
    try:
        resp = await client.post(
            f"{_BASE}/specs",
            json={"name": "reborn", "working_dir": str(project), "spec_type": "feature"},
        )
        assert resp.status == 201, await resp.json()
    finally:
        await client.close()

    assert await _recorded_answers(str(spec_dir)) == {}, (
        "the new spec inherited the deleted spec's answers, so a question it never "
        "asked renders locked and cannot be answered"
    )


@pytest.mark.asyncio
async def test_creating_a_spec_keeps_a_live_alias_answers(tmp_path, monkeypatch):
    """The boundary on the other side: clearing must never reopen a settled decision.

    Two names can serve one directory. If another name is still indexed on it, its
    answers are live -- erasing them on a create would hand that spec a clean slate
    for decisions already settled, which is the reversal this design refuses.
    """
    project = tmp_path / "proj"
    project.mkdir()
    spec_dir = project / ".kiro" / "specs" / "shared"
    client, state = _create_client(monkeypatch, tmp_path)

    key = routes._decision_key(str(spec_dir))
    routes._save_decisions(
        {
            key: {
                "name": "alias",
                "answers": {"transport": _final_answer("transport", "HTTPS")},
            }
        }
    )
    # A live alias: a DIFFERENT name, indexed, pointing at the same directory.
    routes._save_index(
        {
            "alias": {
                "working_dir": str(project),
                "spec_dir": str(spec_dir),
                "spec_type": "feature",
                "status": "planning",
                "slot_key": "spec-builder-alias-1",
                "created_at": 1.0,
                "updated_at": 1.0,
            }
        }
    )
    assert await _recorded_answers(str(spec_dir)) == {"transport": "HTTPS"}

    client.app["state"] = state
    await client.start_server()
    try:
        resp = await client.post(
            f"{_BASE}/specs",
            json={"name": "shared", "working_dir": str(project), "spec_type": "feature"},
        )
        assert resp.status in (201, 409), await resp.json()
    finally:
        await client.close()

    assert await _recorded_answers(str(spec_dir)) == {"transport": "HTTPS"}, (
        "creating a second name on the directory erased the live alias's settled "
        "answer, reopening a decision the user already made"
    )


@pytest.mark.asyncio
async def test_importing_existing_documents_keeps_their_answers(tmp_path, monkeypatch):
    """import_existing adopts documents that already exist, so their answers stand.

    This is the case where the record was written FOR these files. Clearing here
    would reopen settled decisions on re-adoption, so the create-time clear is
    deliberately scoped to a spec whose documents are new.
    """
    project = tmp_path / "proj"
    spec_dir = project / ".kiro" / "specs" / "adopted"
    spec_dir.mkdir(parents=True)
    (spec_dir / "requirements.md").write_text("# theirs")
    client, state = _create_client(monkeypatch, tmp_path)

    key = routes._decision_key(str(spec_dir))
    routes._save_decisions(
        {
            key: {
                "name": "adopted",
                "answers": {"transport": _final_answer("transport", "HTTPS")},
            }
        }
    )
    routes._save_index({})
    assert await _recorded_answers(str(spec_dir)) == {"transport": "HTTPS"}

    client.app["state"] = state
    await client.start_server()
    try:
        resp = await client.post(
            f"{_BASE}/specs",
            json={
                "name": "adopted",
                "working_dir": str(project),
                "spec_type": "feature",
                "import_existing": True,
            },
        )
        assert resp.status == 201, await resp.json()
    finally:
        await client.close()

    assert await _recorded_answers(str(spec_dir)) == {
        "transport": "HTTPS"
    }, "adopting the documents an answer was recorded for dropped that answer"


def test_the_create_time_clear_is_gated_on_new_documents():
    """Source guard: the clear must sit behind ``not import_existing``.

    Ungating it would clear on adoption too, which is the reversal direction --
    and no unit test of the happy path would notice.
    """
    src = inspect.getsource(routes._handle_create)
    assert "if not import_existing:" in src, "the create-time ledger clear is not gated"
    gate = src.index("if not import_existing:")
    clear = src.index("_forget_decisions(", gate)
    body = src[gate:clear]
    # Every line between the gate and the clear must stay indented under it.
    for line in body.splitlines()[1:]:
        if line.strip():
            assert line.startswith(
                "        "
            ), "the ledger clear is not inside the import_existing gate"


@pytest.mark.asyncio
async def test_create_refuses_when_a_stale_record_survives_the_clear(tmp_path, monkeypatch):
    """A failed clear over a READABLE record must refuse, not warn and continue.

    Otherwise the create proceeds and the new spec inherits the answers the clear
    was there to remove -- the round-20 bug, reached through its own failure branch.
    """
    project = tmp_path / "proj"
    project.mkdir()
    spec_dir = project / ".kiro" / "specs" / "stuck"
    client, state = _create_client(monkeypatch, tmp_path)

    key = routes._decision_key(str(spec_dir))
    routes._save_decisions(
        {
            key: {
                "name": "stuck",
                "answers": {"transport": _final_answer("transport", "HTTPS")},
            }
        }
    )
    routes._save_index({})

    # The ledger write fails, exactly as a full disk or a read-only state dir would.
    def _boom(_store):
        raise OSError("no space left on device")

    monkeypatch.setattr(routes, "_save_decisions", _boom)

    client.app["state"] = state
    await client.start_server()
    try:
        resp = await client.post(
            f"{_BASE}/specs",
            json={"name": "stuck", "working_dir": str(project), "spec_type": "feature"},
        )
        status, payload = resp.status, await resp.json()
    finally:
        await client.close()

    assert status == 503, f"{status}: {payload}"
    assert payload["code"] == "decision_record_not_cleared", payload
    # And nothing was registered, so a retry is clean rather than a half-built spec.
    assert "stuck" not in routes._load_index(), (
        "the refused create still registered the spec, so its cards would render "
        "locked to the previous spec's answers"
    )


@pytest.mark.asyncio
async def test_create_refuses_when_the_ledger_is_unreadable(tmp_path, monkeypatch):
    """Create refuses rather than proceeding when the ledger clear cannot be read.

    Letting a create proceed because the store was unusable rests on the reasoning
    that nothing can read a stale answer out of a store nothing can read. That is
    wrong, and the error is worth keeping visible: unreadability is a property of ONE
    READ, not of the store. A transient failure -- a partial write, a momentary IO
    error -- leaves the old record intact on disk, so the probe returns empty, the
    create proceeds, and the record becomes readable again afterwards and overlays its
    answers onto the brand-new spec.

    So the refusal rests on the clear's own result, which is the only signal that
    actually reports whether the ledger is clean. The cost is that a corrupt
    decisions store blocks new spec creation -- loud, and the correct direction to
    fail for a trust root.
    """
    project = tmp_path / "proj"
    project.mkdir()
    client, state = _create_client(monkeypatch, tmp_path)
    routes._save_index({})
    monkeypatch.setattr(routes, "_read_decisions", lambda: ({}, False))

    client.app["state"] = state
    await client.start_server()
    try:
        resp = await client.post(
            f"{_BASE}/specs",
            json={"name": "fresh", "working_dir": str(project), "spec_type": "feature"},
        )
        status, payload = resp.status, await resp.json()
    finally:
        await client.close()

    assert status == 503, f"{status}: {payload}"
    assert payload["code"] == "decision_record_unreadable", payload
    assert "fresh" not in routes._load_index(), "the refused create still registered the spec"


@pytest.mark.asyncio
@pytest.mark.parametrize("import_existing", [False, True])
async def test_create_refuses_an_unreadable_ledger_before_inserting(
    tmp_path, monkeypatch, import_existing
):
    """A transient ledger read failure cannot admit a potentially poisoned alias."""
    client, state = _create_client(monkeypatch, tmp_path)
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
    routes._save_index({})
    monkeypatch.setattr(routes, "_read_decisions", lambda: ({}, False))
    dispatched: list[str] = []
    monkeypatch.setattr(routes, "_dispatch_turn", lambda *_a, **_kw: dispatched.append("x"))

    project = Path(spec_dir).parents[2]
    client.app["state"] = state
    await client.start_server()
    try:
        response = await client.post(
            f"{_BASE}/specs",
            json={
                "name": "S",
                "working_dir": str(project),
                "spec_type": "feature",
                "import_existing": import_existing,
            },
        )
        body = await response.json()
    finally:
        await client.close()

    assert response.status == 503, body
    assert body["code"] == "decision_record_unreadable"
    assert routes._load_index() == {}
    assert dispatched == []
