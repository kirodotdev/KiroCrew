"""The slot-key -> crew-log-unit resolver, and the emitters it unlocks.

Kept separate from ``test_crew_log_emit.py`` because these tests are about
a different question. That file asks whether an entry the runner hands over lands
correctly; this one asks whether a site that holds only a KEY can find the unit to
hand it to at all, and whether the eight families keyed that way say only what
their site observed.
"""

from __future__ import annotations

import asyncio
import json
from pathlib import Path
from types import SimpleNamespace

import pytest
from crew_log_drain import assert_drained

from kiro_crew.crew_log import crew_log_path, emit, resolve

SESSION = "acp-sid-1"

#: Two tests here drive ``SubagentManager.spawn``, which refuses before it
#: registers anything whenever the machine it runs on looks short of memory. The
#: refusal is a done ``SubagentInfo``, so without this the failure would read as
#: the emitter having skipped an entry.
pytestmark = pytest.mark.usefixtures("healthy_host_memory")


@pytest.fixture(autouse=True)
def _isolated_home(tmp_path, monkeypatch):
    """Every test writes into its own data home, never the live one."""
    monkeypatch.setenv("KIROCREW_HOME", str(tmp_path / "home"))
    monkeypatch.setenv(emit.CREW_LOG_ENV, "1")
    monkeypatch.setattr(emit, "_retry_delay", lambda _attempts: 0.0)
    emit.reset_caches()
    emit._child_origin.clear()
    yield
    emit.drain_for_shutdown(timeout=2.0)
    emit.reset_caches()
    emit._child_origin.clear()


def _entries(session_id: str = SESSION) -> list[dict]:
    path: Path = crew_log_path("session", session_id)
    if not path.is_file():
        return []
    with path.open("r", encoding="utf-8") as handle:
        return [json.loads(line) for line in handle if line.strip()]


def _body(session_id: str = SESSION) -> list[dict]:
    return _entries(session_id)[1:]


def _of(kind: str) -> list[dict]:
    return [e["data"] for e in _body() if e["type"] == kind]


def _open_session() -> None:
    emit.on_session_opened(
        SESSION,
        agent="kirocrew",
        slot="chat-7",
        model="claude-opus-5",
        cwd="/home/dev/project",
        owner="default",
    )


# doubles


class _Provider:
    """A session provider, which is what ``session_id_of`` reads an id off."""

    def __init__(self, session_id: str) -> None:
        self.session_id = session_id


class _Sessions:
    """A registry that RECORDS every method reached, so a test can prove which."""

    def __init__(self, providers: dict[str, _Provider]) -> None:
        self._providers = providers
        self.calls: list[tuple[str, str]] = []

    def get_provider(self, key: str):
        self.calls.append(("get_provider", key))
        return self._providers.get(key)


# --- identity: which unit is this session key's work landing in NOW --------


def test_a_namespaced_key_resolves_through_the_live_registry():
    """A channel conversation's own session, addressed by the key it really has.

    This is the shape every production caller passes: the subagent manager and the
    background helpers hold ``info.parent_session_key`` or the key the caller was
    given, never a slot object.
    """
    sessions = _Sessions({"slack:1712.44": _Provider("slack-sid")})
    assert resolve.unit_for_session_key(sessions, "slack:1712.44") == "slack-sid"


def test_a_session_that_never_had_an_acp_session_resolves_to_unknown():
    """Unknown is an answer, and the emitter's no-op turns it into "do not write".

    A fact filed under the wrong session is worse than a missing one. Compared to the
    literal empty string, not ``resolve.UNKNOWN``, because the emitter's guard is
    ``not session_id`` and needs the value FALSY.
    """
    assert resolve.unit_for_session_key(_Sessions({}), "dashboard:chat-7") == ""


def test_resolution_only_ever_reads_the_live_registry():
    """No persisted map, and therefore no repair-on-read.

    ``SessionMap.get`` prunes an entry it finds stale, so consulting it would make
    describing a session mutate it. The double records every method reached, so a
    future edit that reaches for another one fails here.
    """
    sessions = _Sessions({"dashboard:chat-7": _Provider("sid")})
    resolve.unit_for_session_key(sessions, "dashboard:chat-7")
    assert [name for name, _ in sessions.calls] == ["get_provider"]


def test_a_bare_slot_name_is_retried_in_dashboard_form_but_a_namespaced_key_is_not():
    """The retry has a premise: a key with no colon cannot already be namespaced.

    Rewriting one that IS namespaced is how ``slack:<ts>`` becomes the nonexistent
    ``dashboard:slack:<ts>`` -- the exact mistake ``_history_key_for`` documents.
    """
    sessions = _Sessions({"dashboard:chat-7": _Provider("sid")})
    assert resolve.unit_for_session_key(sessions, "chat-7") == "sid"
    assert sessions.calls == [("get_provider", "chat-7"), ("get_provider", "dashboard:chat-7")]

    namespaced = _Sessions({})
    assert resolve.unit_for_session_key(namespaced, "slack:1712.44") == resolve.UNKNOWN
    assert namespaced.calls == [("get_provider", "slack:1712.44")]


def test_a_registry_that_raises_answers_unknown_rather_than_propagating():
    """Describing a session must never be why the work around it fails."""

    class _Angry:
        def get_provider(self, key: str):
            raise RuntimeError("registry is mid-teardown")

    assert resolve.unit_for_session_key(_Angry(), "dashboard:chat-7") == resolve.UNKNOWN


# approvals


def test_an_approval_is_requested_before_it_is_decided():
    """A closer never precedes its opener, even though two tasks write them."""
    _open_session()
    emit.on_turn_started(SESSION, 1, "user")
    emit.on_approval_requested(SESSION, 1, approval_id="r1", tool="shell", reason="rm -rf build")
    emit.on_approval_decided(SESSION, 1, approval_id="r1", decision="approved")
    assert_drained()
    kinds = [e["type"] for e in _body()]
    assert kinds.index("approval/requested") < kinds.index("approval/decided")


def test_a_host_decline_names_the_host_and_its_cause_and_a_human_one_names_neither():
    """Only the host's own auto-declines are attributable at this site.

    A decision that came back through the approval future was made by a person at
    the dashboard or in Slack, and the runner cannot see which -- so it says
    nothing rather than asserting ``user``.
    """
    _open_session()
    emit.on_turn_started(SESSION, 1, "user")
    emit.on_approval_decided(
        SESSION, 1, approval_id="r1", decision="rejected", by="host", cause="approval_timeout"
    )
    emit.on_approval_decided(SESSION, 1, approval_id="r2", decision="approved")
    assert_drained()
    host, human = _of("approval/decided")
    assert host["by"] == "host" and host["cause"] == "approval_timeout"
    assert host["decision"] == "rejected", "the cause must not displace the decision"
    assert "by" not in human and "cause" not in human


def test_an_approval_whose_tool_the_frame_did_not_name_omits_the_field():
    """A permission frame can arrive with no resolvable tool name.

    Writing `""` would record "the tool is the empty string", a value no reader can
    tell from a real one -- in a log whose whole worth is that it says only what was
    observed.
    """
    _open_session()
    emit.on_turn_started(SESSION, 1, "user")
    emit.on_approval_requested(SESSION, 1, approval_id="r1", tool="", reason="")
    emit.on_approval_requested(SESSION, 1, approval_id="r2", tool="shell", reason="ls")
    assert_drained()
    unnamed, named = _of("approval/requested")
    assert "tool" not in unnamed and "reason" not in unnamed
    assert unnamed["approval_id"] == "r1", "the request is still recorded"
    assert named["tool"] == "shell"


def test_a_long_approval_reason_is_clipped_rather_than_losing_the_entry():
    """An entry refused for one oversize field is a fact silently missing."""
    _open_session()
    emit.on_turn_started(SESSION, 1, "user")
    emit.on_approval_requested(SESSION, 1, approval_id="r1", tool="shell", reason="x" * 5000)
    assert_drained()
    (data,) = _of("approval/requested")
    assert len(data["reason"]) == emit._MAX_SHORT_TEXT
    assert data["reason"].endswith("\u2026"), "a clipped value must say it was cut"


def test_an_approval_reason_is_redacted_at_the_emitter_not_trusted_from_the_site():
    _open_session()
    emit.on_turn_started(SESSION, 1, "user")
    emit.on_approval_requested(
        SESSION,
        1,
        approval_id="r1",
        tool="shell",
        reason="curl -H 'Authorization: Bearer sk-live-ABCDEF1234567890abcdef'",
    )
    assert_drained()
    (data,) = _of("approval/requested")
    assert "sk-live-ABCDEF1234567890abcdef" not in data["reason"]


# plan


def test_a_plan_records_only_the_two_states_the_stream_carries():
    """The backend's todo model is a boolean, so a third state would be invented."""
    _open_session()
    emit.on_turn_started(SESSION, 1, "user")
    emit.on_plan_updated(
        SESSION,
        1,
        items=[
            {"id": "a", "text": "read the code", "completed": True},
            {"id": "b", "text": "write the test", "completed": False},
        ],
    )
    assert_drained()
    (data,) = _of("plan/updated")
    assert [row["state"] for row in data["items"]] == ["done", "open"]
    assert {row["state"] for row in data["items"]} <= {"done", "open"}


def test_no_task_list_writes_nothing_but_an_empty_one_records_a_cleared_plan():
    """Absent data and a plan of zero tasks are different facts."""
    _open_session()
    emit.on_turn_started(SESSION, 1, "user")
    emit.on_plan_updated(SESSION, 1, items=None)
    assert_drained()
    assert _of("plan/updated") == []
    emit.on_plan_updated(SESSION, 1, items=[])
    assert_drained()
    assert _of("plan/updated") == [{"turn": 1, "items": []}]


def test_the_two_unconditionally_called_emitters_do_no_work_with_the_flag_off(monkeypatch):
    """The runner calls these two on every plan change and every approval prompt.

    Both redact and serialize before `_write` can no-op, so they need their own flag
    guard. The work is patched to fail if it runs, so the test cannot pass by accident.
    """
    monkeypatch.setenv(emit.CREW_LOG_ENV, "0")
    assert not emit.enabled()

    def _boom(*_a, **_kw):
        raise AssertionError("did work with the flag off")

    monkeypatch.setattr(emit, "_safe_text", _boom)
    monkeypatch.setattr(emit, "_entry_line_fits", _boom)
    emit.on_plan_updated(SESSION, 1, items=[{"id": "a", "text": "t", "completed": False}])
    emit.on_approval_requested(SESSION, 1, approval_id="r1", tool="shell", reason="ls")


def test_a_plan_of_emoji_is_bounded_by_bytes_and_still_lands():
    """Character bounds do not bound bytes, and a refused entry is a lost fact.

    ``_clip`` bounds characters, but ``ensure_ascii`` writes twelve bytes per surrogate
    pair, so eleven rows of 500 emoji pass the 64 KiB entry ceiling and are refused.
    """
    _open_session()
    emit.on_turn_started(SESSION, 1, "user")
    emit.on_plan_updated(
        SESSION,
        1,
        items=[{"id": str(n), "text": "\U0001f600" * 500, "completed": False} for n in range(40)],
    )
    assert_drained()
    written = _of("plan/updated")
    assert written, "the entry must land rather than be refused whole"
    (data,) = written
    assert data["items"], "and it must carry some of the plan"
    assert data["total"] == 40, "while saying how many tasks there really were"
    assert len(data["items"]) < 40, "having dropped the rows that would not fit"


def test_one_oversize_plan_row_does_not_suppress_the_whole_entry():
    """The first row is admitted unmeasured so an entry always carries something.

    A single task whose clipped text alone approaches the ceiling would otherwise
    measure as not fitting, and an empty ``items`` would report a cleared plan --
    the one reading that is actively wrong.
    """
    _open_session()
    emit.on_turn_started(SESSION, 1, "user")
    emit.on_plan_updated(
        SESSION, 1, items=[{"id": "a", "text": "\U0001f600" * 500, "completed": True}]
    )
    assert_drained()
    (data,) = _of("plan/updated")
    assert len(data["items"]) == 1
    assert data["items"][0]["state"] == "done"


def test_a_runaway_plan_is_clipped_and_still_reports_its_real_size():
    _open_session()
    emit.on_turn_started(SESSION, 1, "user")
    emit.on_plan_updated(
        SESSION,
        1,
        items=[{"id": str(n), "text": f"t{n}", "completed": False} for n in range(500)],
    )
    assert_drained()
    (data,) = _of("plan/updated")
    assert len(data["items"]) == emit._MAX_PLAN_ITEMS
    assert data["total"] == 500


def test_a_plan_entry_is_ignorable_because_nothing_later_depends_on_reading_it():
    _open_session()
    emit.on_turn_started(SESSION, 1, "user")
    emit.on_plan_updated(SESSION, 1, items=[{"id": "a", "text": "t", "completed": False}])
    assert_drained()
    (entry,) = [e for e in _body() if e["type"] == "plan/updated"]
    assert entry.get("ignorable") is True


# background


class _Usage:
    def __init__(self, **fields) -> None:
        self.input_tokens = fields.get("input_tokens", 0)
        self.output_tokens = fields.get("output_tokens", 0)
        self.cache_read_tokens = fields.get("cache_read_tokens", 0)
        self.cache_creation_tokens = fields.get("cache_creation_tokens", 0)
        self.credits = fields.get("credits", 0.0)


def test_a_background_call_records_the_dimensions_that_were_billed_and_omits_the_rest():
    """A provider fills the dimensions it bills in; a zero is not a measurement."""
    _open_session()
    emit.on_background_completed(
        SESSION,
        kind="title",
        model="claude-haiku",
        provider="acp",
        credits=0.0,
        input_tokens=900,
        output_tokens=12,
        duration_ms=430,
    )
    assert_drained()
    (data,) = _of("background/completed")
    assert data["tokens"] == {"input": 900, "output": 12}
    assert "credits" not in data, "an unbilled dimension must be absent, not zero"
    assert data["ms"] == 430


def test_a_background_call_names_no_turn():
    """It runs after a turn ends, so naming the last one charges the wrong work."""
    _open_session()
    emit.on_turn_started(SESSION, 1, "user")
    emit.on_turn_completed(SESSION, 1, stop_reason="end_turn")
    emit.on_background_completed(SESSION, kind="summary", model="m", credits=0.4)
    assert_drained()
    (data,) = _of("background/completed")
    assert "turn" not in data
    assert data["credits"] == pytest.approx(0.4)


def test_a_background_call_with_no_owner_or_no_kind_resolves_no_owner():
    """Most background work is charged to nobody, and must stay that way.

    Titling is charged to the session it titles; a tip or a cron label is shared
    infrastructure, and picking a session for it would put someone else's spend in
    a user's log.
    """
    from kiro_crew.llm_helpers import _background_crew_log_owner

    sessions = _Sessions({"dashboard:chat-7": _Provider(SESSION)})
    assert _background_crew_log_owner(sessions, "", "title") == ""
    assert _background_crew_log_owner(sessions, "dashboard:chat-7", "") == ""
    assert sessions.calls == [], "an unnamed call must not even resolve a session"


def _unit_files(session_id: str) -> dict[str, int]:
    """Every file directly in a unit's directory (log segments, cuts), by size."""
    from kiro_crew.crew_log.store_paths import crew_log_dir

    directory = crew_log_dir("session", session_id)
    return {p.name: p.stat().st_size for p in directory.iterdir() if p.is_file()}


def test_a_reset_during_a_background_call_writes_to_the_newest_unit_for_the_pinned_one():
    """The owner is pinned before the call; the entry lands in the slot's newest unit.

    A slot reset, switch or compaction during the model call starts a NEW unit. A
    crew log is append-only and an older unit never grows (a write there retires the
    savepoints beside the newest unit), so the teardown writes into the successor and
    counts it in that unit's usage.
    """
    from kiro_crew.llm_helpers import _background_crew_log_owner, _record_background_crew_log

    _open_session()
    successor = "successor-sid"
    sessions = _Sessions({"dashboard:chat-7": _Provider(SESSION)})
    owner = _background_crew_log_owner(sessions, "dashboard:chat-7", "title")
    assert owner == SESSION
    # The slot is reset mid-call: the registry now serves a new unit.
    emit.on_session_opened(
        successor, agent="kirocrew", slot="chat-7", model="m", cwd="/w", owner="default"
    )
    sessions._providers["dashboard:chat-7"] = _Provider(successor)
    assert_drained()
    before = _unit_files(SESSION)
    _record_background_crew_log(
        owner,
        "title",
        _Usage(credits=1.0),
        model="m",
        provider="acp",
        elapsed_ms=1,
        sessions=sessions,
        crew_log_session_key="dashboard:chat-7",
    )
    assert_drained()
    assert _unit_files(SESSION) == before, "the superseded unit did not grow"
    (entry,) = [e for e in _body(successor) if e["type"] == "background/completed"]
    assert entry["data"]["kind"] == "title"


def test_a_background_call_with_no_reset_writes_to_the_pinned_unit():
    from kiro_crew.llm_helpers import _background_crew_log_owner, _record_background_crew_log

    _open_session()
    sessions = _Sessions({"dashboard:chat-7": _Provider(SESSION)})
    owner = _background_crew_log_owner(sessions, "dashboard:chat-7", "title")
    _record_background_crew_log(
        owner,
        "title",
        _Usage(credits=1.0),
        model="m",
        provider="acp",
        elapsed_ms=1,
        sessions=sessions,
        crew_log_session_key="dashboard:chat-7",
    )
    assert_drained()
    (data,) = _of("background/completed")
    assert data["kind"] == "title"


def test_both_background_helpers_pin_the_owner_before_their_first_await():
    """Resolving after a suspension point names the successor, not the incurrer.

    Acquiring a background session suspends, and the slot may be reset meanwhile.
    Checked on the source because the property is an ORDER in the function body.
    """
    import ast
    import importlib
    import inspect

    from kiro_crew import llm_helpers

    # The facade and every owner module it is composed from, parsed as one tree.
    modules = [llm_helpers, *map(importlib.import_module, llm_helpers._OWNERS)]
    tree = ast.Module(
        body=[node for mod in modules for node in ast.parse(inspect.getsource(mod)).body],
        type_ignores=[],
    )
    checked = []
    for node in ast.walk(tree):
        if not isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
            continue
        pins = [
            n.lineno
            for n in ast.walk(node)
            if isinstance(n, ast.Assign)
            and any(isinstance(t, ast.Name) and t.id == "_crew_log_owner" for t in n.targets)
        ]
        if not pins:
            continue
        awaits = [n.lineno for n in ast.walk(node) if isinstance(n, ast.Await)]
        assert awaits, f"{node.name} pins an owner but never awaits"
        assert min(pins) < min(awaits), (
            f"{node.name} resolves _crew_log_owner at line {min(pins)}, after its first "
            f"await at line {min(awaits)} -- the spend can land in the successor's crew log"
        )
        checked.append(node.name)
    assert len(checked) == 2, f"expected both background helpers, found {checked}"


def test_a_named_background_call_lands_in_the_owner_s_log():
    from kiro_crew.llm_helpers import _background_crew_log_owner, _record_background_crew_log

    _open_session()
    sessions = _Sessions({"dashboard:chat-7": _Provider(SESSION)})
    owner = _background_crew_log_owner(sessions, "dashboard:chat-7", "memory_consolidation")
    _record_background_crew_log(
        owner,
        "memory_consolidation",
        _Usage(credits=2.5, input_tokens=40),
        model="kirocrew-lite",
        provider="acp",
        elapsed_ms=77,
    )
    assert_drained()
    (data,) = _of("background/completed")
    assert data["kind"] == "memory_consolidation"
    assert data["credits"] == pytest.approx(2.5)
    assert data["tokens"] == {"input": 40}


# children


def test_a_child_is_spawned_then_closed_and_the_spawn_carries_its_scope():
    _open_session()
    emit.on_turn_started(SESSION, 4, "user")
    emit.on_subagent_spawned(
        SESSION,
        4,
        agent_id="ab12",
        agent="kirocrew",
        model="claude-opus-5",
        scope={"memory": False, "lessons": True, "project": True},
    )
    emit.on_subagent_completed(SESSION, agent_id="ab12", duration_ms=9100)
    assert_drained()
    kinds = [e["type"] for e in _body()]
    assert kinds.index("subagent/spawned") < kinds.index("subagent/completed")
    (spawned,) = _of("subagent/spawned")
    assert spawned["turn"] == 4
    assert spawned["scope"] == {"memory": False, "lessons": True, "project": True}


def test_a_spawn_carries_no_ref_because_the_child_has_no_log_to_cite():
    """The schema describes one; no subagent path opens a crew log to point at.

    A ``ref`` now would cite a missing file that looks deleted. Checked in both the
    envelope field and the entry's data, since a `ref` key in data reads like one.
    """
    _open_session()
    emit.on_turn_started(SESSION, 1, "user")
    emit.on_subagent_spawned(SESSION, 1, agent_id="ab12", agent="kirocrew")
    assert_drained()
    (entry,) = [e for e in _body() if e["type"] == "subagent/spawned"]
    assert "ref" not in entry
    assert "ref" not in entry["data"]


def test_a_spawn_with_no_asking_turn_omits_the_field_rather_than_writing_zero():
    """A slash command, a cron and a hook all dispatch children with nothing running.

    Turns are numbered from one, so a literal `0` would name a turn that never
    existed and match no `turn/started`. The child is still recorded: it is a real
    child of that session, and dropping it to keep a field populated would be the
    worse trade.
    """
    _open_session()
    emit.on_subagent_spawned(SESSION, 0, agent_id="ab12", agent="kirocrew")
    assert_drained()
    (data,) = _of("subagent/spawned")
    assert "turn" not in data
    assert data["agent_id"] == "ab12", "the child is still recorded"


def test_a_completed_child_reports_no_tokens_and_no_credits():
    """Nothing in the subagent runtime measures either; zeros would be a claim."""
    _open_session()
    emit.on_subagent_completed(SESSION, agent_id="ab12", duration_ms=5)
    assert_drained()
    (data,) = _of("subagent/completed")
    assert "tokens" not in data and "credits" not in data


def test_a_stopped_child_is_not_recorded_as_a_completion():
    """The runtime's own outcome separates the three; the log must not merge them.

    A user stop is not a success and not an error. It closes through the
    non-success closer carrying which one it was.
    """
    _open_session()
    emit.on_subagent_failed(SESSION, agent_id="ab12", reason="", outcome="stopped", duration_ms=12)
    emit.on_subagent_failed(SESSION, agent_id="cd34", reason="provider refused", outcome="failed")
    assert_drained()
    assert _of("subagent/completed") == []
    stopped, failed = _of("subagent/failed")
    assert stopped["outcome"] == "stopped" and "reason" not in stopped
    assert failed["outcome"] == "failed" and failed["reason"] == "provider refused"


def test_a_child_s_origin_is_pinned_once_so_a_queued_member_keeps_the_asking_turn():
    """A member held behind the stagger gate re-enters the spawn path.

    Re-pinning on the second pass would move the child onto whatever turn the
    parent had reached by then -- a turn ordinal re-derived after the fact, which
    is the one thing this log may not do.
    """
    emit.remember_child_origin("ab12", SESSION, 4)
    emit.remember_child_origin("ab12", SESSION, 9)
    assert emit.open_child_origin("ab12") == (SESSION, 4)


def test_a_replayed_queued_child_is_repinned_with_an_unobserved_turn(monkeypatch):
    """A restart empties the origin map, and the durable queue replays the row.

    Without a re-pin on the ``from_queue`` pass the child's spawn, steer and terminal
    entries are dropped. The turn stays unobserved: the asking turn died with its
    process, and writers omit an unobserved ordinal.
    """
    from types import SimpleNamespace

    from kiro_crew.subagent_manager.admission.gate import _GateMixin

    monkeypatch.setattr(resolve, "unit_for_session_key", lambda _sessions, _key: SESSION)
    stub = SimpleNamespace(_manager=SimpleNamespace(_sessions=None))
    info = SimpleNamespace(id="queued-1", parent_session_key="slot-key")

    # The replay: nothing is pinned, so this pass is the one that restores it.
    _GateMixin._record_crew_log_dispatch(stub, info, from_queue=True)
    assert emit.open_child_origin("queued-1") == (SESSION, 0)

    # The second pass inside the accepting process: the original pin stands.
    emit._child_origin.clear()
    emit.remember_child_origin("queued-1", SESSION, 7)
    _GateMixin._record_crew_log_dispatch(stub, info, from_queue=True)
    assert emit.open_child_origin("queued-1") == (SESSION, 7)


def test_a_replayed_queued_child_is_pinned_to_the_unit_that_asked(monkeypatch):
    """A restart moves the parent onto a new unit; the row still names the old one.

    The durable row carries the asking unit, turn and attempt (as a list once it
    has been through JSON). Resolving the parent's unit at replay time would file
    the child's spawn and closer in the successor's log, so a per-unit view would
    move that child's spend onto a unit that never asked for it.
    """
    from kiro_crew.subagent_manager.admission.gate import _GateMixin

    monkeypatch.setattr(resolve, "unit_for_session_key", lambda _sessions, _key: "successor")
    stub = SimpleNamespace(_manager=SimpleNamespace(_sessions=None))
    info = SimpleNamespace(id="queued-2", parent_session_key="slot-key")

    _GateMixin._record_crew_log_dispatch(stub, info, from_queue=True, asked=[SESSION, 5, 2])
    assert emit.open_child_origin("queued-2") == (SESSION, 5)
    assert emit.child_origin_attempt("queued-2") == 2


def test_a_dispatch_after_the_turn_s_closer_is_queued_names_no_turn(monkeypatch):
    """A cron or hook dispatching while ``turn/completed`` waits for the writer.

    That turn has ended. ``live_turn`` still reports it, because the tail repair
    must stand down for a closer on its way, but the pin must not: the spawn would
    be stamped with a turn that never asked for it, where the schema says a
    dispatch with no asking turn has no ``turn``.
    """
    from kiro_crew.subagent_manager.admission.gate import _GateMixin

    monkeypatch.setattr(resolve, "unit_for_session_key", lambda _sessions, _key: SESSION)
    stub = SimpleNamespace(_manager=SimpleNamespace(_sessions=None))
    info = SimpleNamespace(id="cron-1", parent_session_key="slot-key")

    emit._tracker().begin(SESSION, 3)
    release = emit._tracker().closing(SESSION, 3)
    try:
        assert emit.live_turn(SESSION) == 3, "the repair's reading is unchanged"
        assert emit.asking_turn(SESSION) == (0, 0)
        _GateMixin._record_crew_log_dispatch(stub, info, from_queue=False)
        assert emit.open_child_origin("cron-1") == (SESSION, 0)
    finally:
        release()


def test_a_child_of_a_rerun_turn_names_the_attempt_that_asked(monkeypatch):
    """A regenerate reruns turn 4; its child must be told apart from the first try's.

    ``turn/started`` is identified by ``(turn, attempt)``, so a ``subagent/spawned``
    that carries only ``turn`` cannot be joined to the try that dispatched it.
    """
    from kiro_crew.subagent_manager.admission.gate import _GateMixin

    monkeypatch.setattr(resolve, "unit_for_session_key", lambda _sessions, _key: SESSION)
    stub = SimpleNamespace(_manager=SimpleNamespace(_sessions=None))
    _open_session()
    emit.on_turn_started(SESSION, 4, "user", attempt=2)
    assert_drained()
    assert emit.asking_turn(SESSION) == (4, 2)

    info = SimpleNamespace(
        id="rerun-1",
        parent_session_key="slot-key",
        run_agent="kirocrew",
        model="",
        task="",
        include_memory=True,
        include_lessons=True,
        include_project=True,
    )
    _GateMixin._record_crew_log_dispatch(stub, info, from_queue=False)
    _GateMixin._record_crew_log_spawn_started(stub, info)
    assert_drained()
    (spawned,) = _of("subagent/spawned")
    assert (spawned["turn"], spawned["attempt"]) == (4, 2)


def test_a_rerun_reads_as_unobserved_until_its_own_attempt_is_derived(monkeypatch):
    """A rerun of an ordinal whose record is still open must not read the old try.

    ``begin`` keeps an existing record, and the attempt is derived later on the
    writer thread. A dispatch in between would be pinned to the earlier try.
    """
    jobs: list = []
    monkeypatch.setattr(emit, "_submit", lambda job, *_a, **_k: jobs.append(job))
    first = emit._tracker().begin(SESSION, 4)
    emit._tracker().note_attempt(SESSION, 4, 1, first)

    emit.on_turn_started(SESSION, 4, "user", attempt=2)
    assert len(jobs) == 1, "the attempt is still owed to the queued job"
    assert emit.asking_turn(SESSION) == (4, 0), "the earlier try's attempt leaked"


def test_a_rerun_before_the_earlier_closer_lands_keeps_its_turn_and_attempt():
    """A regenerate of turn 4 starts while try 1's closer is still queued.

    Reusing try 1's record would leave it marked closer-owed, so the rerun would
    read as having no asking turn, and try 1's release would then drop the record
    try 2 is running on and take its attempt with it.
    """
    tracker = emit._tracker()
    first = tracker.begin(SESSION, 4)
    tracker.note_attempt(SESSION, 4, 1, first)
    release_first = tracker.closing(SESSION, 4)

    rerun = tracker.begin(SESSION, 4)
    tracker.note_attempt(SESSION, 4, 2, rerun)
    assert emit.asking_turn(SESSION) == (4, 2), "the rerun is the asking turn"

    release_first()
    assert emit.asking_turn(SESSION) == (4, 2), "try 1's release dropped try 2's record"
    tracker.closing(SESSION, 4)()
    assert emit.live_turn(SESSION) == 0, "try 2's own release closes it"


def test_a_closer_that_found_no_record_cannot_drop_a_rerun_s_record():
    """Handed over with no record to mark, then a rerun opens one before the release."""
    tracker = emit._tracker()
    release_unmarked = tracker.closing(SESSION, 5)
    rerun = tracker.begin(SESSION, 5)
    tracker.note_attempt(SESSION, 5, 2, rerun)
    release_unmarked()
    assert emit.asking_turn(SESSION) == (5, 2), "the release dropped a record it never marked"
    tracker.closing(SESSION, 5)()
    assert emit.live_turn(SESSION) == 0


def test_an_earlier_start_s_job_cannot_stamp_a_rerun_s_record():
    """Turn 4 ends and is regenerated while its first start job is still queued.

    That job derives attempt 1. Stamping it on whatever record is live would hand the
    rerun's children attempt 1, and a child's pin cannot be corrected afterwards.
    """
    tracker = emit._tracker()
    first = tracker.begin(SESSION, 4)
    tracker.closing(SESSION, 4)
    rerun = tracker.begin(SESSION, 4)
    assert rerun is not first
    tracker.note_attempt(SESSION, 4, 1, first)
    assert emit.asking_turn(SESSION) == (4, 0), "the first try's job stamped the rerun"
    tracker.note_attempt(SESSION, 4, 2, rerun)
    assert emit.asking_turn(SESSION) == (4, 2)


def test_a_retried_start_job_keeps_the_attempt_it_derived(monkeypatch):
    """The writer retries a failed append by running the same job again.

    Deriving again would count the retry as a second try: ``turn/started`` and the
    attempt a child is pinned with would both name a try that never ran.
    """
    jobs: list = []
    appended: list[dict] = []
    failures = [OSError("transient")]

    class _Log:
        def append(self, _kind, payload, **_kw):
            if failures:
                raise failures.pop()
            appended.append(dict(payload))
            return SimpleNamespace(seq=1)

    monkeypatch.setattr(emit, "_submit", lambda job, *_a, **_k: jobs.append(job))
    monkeypatch.setattr(emit, "_handle", lambda _sid: _Log())
    monkeypatch.setattr(emit, "_note_eager", lambda *_a, **_k: None)
    emit.on_turn_started(SESSION, 4, "user")
    (job,) = jobs
    with pytest.raises(OSError):
        job()
    job()
    assert appended == [{"turn": 4, "actor": "user", "depth": 0}], "the retry wrote attempt 2"
    assert emit.asking_turn(SESSION) == (4, 1)


def test_an_over_long_unit_id_is_never_put_on_the_queued_row():
    """The provider authors the id; the durable row must not carry an unbounded one."""
    from kiro_crew.subagent_manager.admission.gate_reporting import crew_log_asking_origin

    huge = "s" * (emit._MAX_SESSION_ID_CHARS + 1)
    sessions = _Sessions({"dashboard:chat-7": _Provider(huge)})
    assert crew_log_asking_origin(sessions, "dashboard:chat-7") is None
    fits = "s" * emit._MAX_SESSION_ID_CHARS
    sessions = _Sessions({"dashboard:chat-7": _Provider(fits)})
    assert crew_log_asking_origin(sessions, "dashboard:chat-7") == (fits, 0, 0)


def test_a_spawn_whose_attempt_was_not_observed_omits_it():
    """Absent means unknown: the attempt is derived on the writer thread."""
    _open_session()
    emit.on_subagent_spawned(SESSION, 4, agent_id="ab12")
    emit.on_subagent_spawned(SESSION, 0, agent_id="cd34", attempt=3)
    assert_drained()
    first, second = _of("subagent/spawned")
    assert "attempt" not in first
    assert "attempt" not in second and "turn" not in second, "no attempt without a turn"


@pytest.mark.asyncio
async def test_a_claim_the_store_cannot_take_is_pinned_before_the_row_is_left_queued(monkeypatch):
    """The asking turn is readable on THIS pass and on no later one.

    A busy store leaves the row queued, and the pump's retry runs under ``from_queue``
    where the asking turn cannot be read. Pinning first gives the retry the turn,
    since the pin refuses to move.
    """
    from unittest.mock import AsyncMock, MagicMock

    from kiro_crew import taskq as _taskq
    from kiro_crew.subagent import SubagentManager

    sessions = MagicMock()
    sessions.get_pid = MagicMock(return_value=None)
    sessions.get_or_create = AsyncMock()
    sessions.get_agent = MagicMock(return_value="")
    sessions.get_agent_selection = MagicMock(return_value=("template", ""))
    sessions.get_approval_policy = MagicMock(return_value="auto")
    ctx = MagicMock()
    ctx.hooks.auto_approve_subagent_spawn = True
    mgr = SubagentManager(sessions=sessions, ctx_builder=ctx)
    await mgr.wait_taskq_ready()
    mgr._spawn_stagger_secs = 0.0
    store = mgr._admission.taskq_store()
    assert store is not None

    def _busy(*_a, **_k):
        raise _taskq.TaskStoreUnavailable("database is locked")

    monkeypatch.setattr(store, "claim", _busy)
    monkeypatch.setattr(resolve, "unit_for_session_key", lambda _sessions, _key: SESSION)
    _open_session()
    emit.on_turn_started(SESSION, 6)
    try:
        info = mgr.spawn("do it", parent_session_key="web-1")
        assert info is not None and info.queued and not info.done, info
        assert emit.open_child_origin(info.id) == (
            SESSION,
            6,
        ), "the row was left queued, so only this pass could pin the turn that asked"
        row = store.get(info.id)
        assert row is not None
        assert list(row.params["_crew_log_asked"])[:2] == [SESSION, 6], (
            "the durable row must name the asking unit, or a replay after a restart "
            "files the child under the parent's next unit"
        )
    finally:
        await mgr.cancel_all()
        # ``cancel_all`` leaves the durable task queue the constructor opened
        # (``tasks.db`` + ``-wal`` + ``-shm``) live; release it here.
        mgr.close()


@pytest.mark.asyncio
async def test_a_run_whose_memory_binding_cannot_be_persisted_records_no_spawn(monkeypatch):
    """The entry marks a run that started, and this one never does.

    Persisting the agent folder is a prerequisite: when it fails the run settles as
    a failure without ever allocating a provider. Writing the opener before that
    write states a start that did not happen, and the pin is never opened, so the
    terminal report closes nothing and the log keeps a claim it cannot retract.
    """
    from unittest.mock import AsyncMock, MagicMock

    from kiro_crew import subagent as _subagent
    from kiro_crew.subagent import SubagentInfo, SubagentManager

    sessions = MagicMock()
    sessions.get_pid = MagicMock(return_value=None)
    sessions.get_or_create = AsyncMock()
    sessions.get_agent = MagicMock(return_value="")
    sessions.get_approval_policy = MagicMock(return_value="auto")
    ctx = MagicMock()
    ctx.hooks.auto_approve_subagent_spawn = True
    mgr = SubagentManager(sessions=sessions, ctx_builder=ctx)
    await mgr.wait_taskq_ready()
    _open_session()

    def _cannot_persist(*_a, **_k):
        raise OSError("read-only file system")

    monkeypatch.setattr(_subagent, "create_agent_folder", _cannot_persist)
    failed = SubagentInfo(id="nofolder-1", task="t", agent="", parent_session_key="web-1")
    emit.remember_child_origin(failed.id, SESSION, 3)
    try:
        mgr._log_spawned(failed)
        assert failed.error.startswith("memory_unavailable:"), failed.error
        assert_drained()
        assert _of("subagent/spawned") == [], "a run that never started has no opener"

        # The prerequisite met: the same call does write it.
        monkeypatch.setattr(_subagent, "create_agent_folder", lambda *_a, **_k: None)
        started = SubagentInfo(id="folder-ok-1", task="t", agent="", parent_session_key="web-1")
        emit.remember_child_origin(started.id, SESSION, 4)
        mgr._log_spawned(started)
        assert_drained()
        assert [e["agent_id"] for e in _of("subagent/spawned")] == ["folder-ok-1"]
    finally:
        await mgr.cancel_all()
        # ``cancel_all`` leaves the durable task queue the constructor opened
        # (``tasks.db`` + ``-wal`` + ``-shm``) live; release it here.
        mgr.close()


def test_a_plan_id_is_redacted_like_every_other_field_the_agent_authored(monkeypatch):
    """The id is as agent-authored as the text beside it, and nothing later redacts.

    A task id echoed from something the agent read can carry a credential, and the
    write path hands ``data`` to the append as it stands -- so a secret that reaches
    the id reaches the file, where nothing rewrites it. An ordinary id is untouched.
    """
    _open_session()
    emit.on_plan_updated(
        SESSION,
        2,
        items=[
            {"id": "ghp_" + "a" * 36, "text": "read the token", "completed": False},
            {"id": "task-7", "text": "keep going", "completed": True},
        ],
    )
    assert_drained()
    (plan,) = _of("plan/updated")
    leaked, ordinary = plan["items"]
    assert "ghp_" not in leaked["id"] and "REDACTED" in leaked["id"], leaked
    assert ordinary["id"] == "task-7", "redaction must not rewrite an ordinary id"


def test_a_child_whose_closer_is_not_yet_handed_over_is_not_reported_gone():
    """The gap the two reads alone cannot cover, and what covers it.

    Between ``done`` dropping the child from the running set and the report task
    handing the closer over, liveness and debt both say gone. The child's origin pin,
    released only once the closer reaches the writer, covers that gap.
    """
    _open_session()
    emit.remember_child_origin("ab12", SESSION, 3)
    assert emit.open_child_origin("ab12") == (SESSION, 3)
    # The session must owe nothing, or debt would mask the pin.
    assert_drained()

    with pytest.MonkeyPatch.context() as patch:
        patch.setattr(emit, "_child_liveness", lambda _agent_id: False)
        probe = emit._child_gone_probe(SESSION)
        assert probe is not None
        assert probe("ab12") is False, "a child still holding its origin pin was reported gone"
        emit.forget_child_origin("ab12")
        assert probe("ab12") is True, "a released pin must stop vouching for the child"


def test_a_repair_is_not_blocked_by_its_own_job_being_the_one_in_flight(monkeypatch):
    """The repair runs INSIDE a claimed batch, so it must not read itself as debt.

    Counting the asking job would make every child look live, so a dangling opener
    would never close. Submitted from an event loop so the job runs from the writer's
    claimed batch rather than inline.
    """
    _open_session()
    monkeypatch.setattr(emit, "_child_liveness", lambda _agent_id: False)
    answers: list[object] = []

    def _ask_from_inside_the_batch() -> None:
        probe = emit._child_gone_probe(SESSION)
        answers.append(probe("ab12") if probe is not None else None)

    async def _submit() -> None:
        emit._submit(_ask_from_inside_the_batch, "repair asking", SESSION)

    asyncio.run(_submit())
    assert_drained()
    assert answers == [True], "the asking job was counted as debt against itself"


def test_a_terminal_is_recorded_before_the_child_leaves_the_running_set():
    """The repair's probe runs on the writer's own thread, not the caller's.

    Flipping ``done`` first opens an instant where a repair writes ``unknown`` before
    the real terminal. Checked on source order because only the writer's thread could
    observe the gap; a test on this thread would pass either way.
    """
    import inspect

    from kiro_crew.subagent_manager.terminal import TerminalCoordinator

    body = inspect.getsource(TerminalCoordinator._finish)
    record = body.index("self._record_crew_log_terminal(info)")
    flip = body.index("info.done = True")
    assert record < flip, (
        "info.done is flipped before the terminal entry is handed over; this flip is "
        "the one for paths that reach a terminal without the run loop, and the normal "
        "path's earlier flip is covered by the origin pin instead"
    )


def test_the_closer_is_handed_over_before_its_child_s_origin_pin_is_released():
    """The pin must outlive the handover, or it leaves the gap it exists to cover.

    Releasing first lets a repair close the child as ``unknown`` before the entry
    reaches the writer. Reporting stays one-shot because every route is gated on
    ``_claim_finalize``.
    """
    from kiro_crew.subagent_manager.terminal import TerminalCoordinator

    _open_session()
    emit.remember_child_origin("ab12", SESSION, 3)
    assert emit.open_child_origin("ab12") == (SESSION, 3)
    held: list[bool] = []
    real = emit.on_subagent_completed

    def _spy(session_id, **kw):
        held.append(bool(emit.child_origin(kw["agent_id"])[0]))
        return real(session_id, **kw)

    # `credits` stands in for the run accumulator the closer reads: a real
    # `SubagentInfo` always carries it, so a stand-in that omits it would make this
    # test fail on the attribute rather than on the ordering it exists to pin.
    info = SimpleNamespace(id="ab12", elapsed=0.5, outcome="completed", error=None, credits=1.25)
    with pytest.MonkeyPatch.context() as patch:
        patch.setattr(emit, "on_subagent_completed", _spy)
        TerminalCoordinator._record_crew_log_terminal(SimpleNamespace(), info)
    assert held == [True], "the pin was released before the closer was handed over"
    assert emit.child_origin("ab12")[0] == "", "the pin was not released afterwards"
    assert_drained()
    assert [d["agent_id"] for d in _of("subagent/completed")] == ["ab12"]


def test_a_turn_still_pending_at_the_terminal_is_billed_in_the_closer():
    """The closer carries the settled cost, the same figure the tombstone gets.

    A terminal reached with usage still pending (e.g. a cancel in the event consumer)
    settles it in the report; settling after the closer would under-report
    ``subagent/completed``.
    """
    from unittest.mock import MagicMock

    from kiro_crew.acp.types import AcpPromptStats
    from kiro_crew.subagent import SubagentInfo, _RunCreditAccounting
    from kiro_crew.subagent_manager.terminal import TerminalCoordinator

    _open_session()
    emit.remember_child_origin("ab12", SESSION, 3)
    assert emit.open_child_origin("ab12") == (SESSION, 3)

    info = SubagentInfo(id="ab12", task="bill the last turn", credits=1.0)
    runner = MagicMock(spec=[])
    runner.last_prompt_stats = AcpPromptStats()
    provider = MagicMock(spec=[])
    provider._handle = runner
    accounting = _RunCreditAccounting(info)
    accounting.begin(provider)
    runner.last_prompt_stats = AcpPromptStats(credits=0.5)
    info._credit_accounting = accounting
    info.elapsed = 0.5

    async def _guarded(_info, **_kw):
        return True

    coordinator = SimpleNamespace(
        _record_terminal_metric=lambda _info: None,
        _manager=SimpleNamespace(
            arm_report_in_flight=lambda _info: None,
            consume_report_hold=lambda _batch, _id: None,
        ),
        _report_terminal_guarded_impl=_guarded,
    )
    coordinator._record_crew_log_terminal = lambda i: TerminalCoordinator._record_crew_log_terminal(
        coordinator, i
    )
    coordinator._finish = lambda i: TerminalCoordinator._finish(coordinator, i)

    asyncio.run(
        TerminalCoordinator._report_terminal_impl(
            coordinator,
            info,
            source="test",
            injection_timeout_reason="",
            mark_delivered_on_success=False,
        )
    )
    assert info.credits == pytest.approx(1.5)
    assert_drained()
    (data,) = _of("subagent/completed")
    assert data["credits"] == pytest.approx(
        1.5
    ), "the closer was written before the last turn settled"


def test_a_plan_the_line_cannot_hold_records_a_prefix_not_a_subsequence():
    """``items`` is read as the FRONT of the plan, so a gap in it is a lie.

    The first row that does not fit the byte ceiling closes admission; skipping only
    it would let a shorter later row in. ``total`` still counts the whole plan.
    """
    _open_session()
    # `text` is clipped in CHARACTERS while the line is refused in BYTES, so rows
    # that are each individually legal still overflow together: an emoji is four
    # bytes, and 512 of them is 2 KiB per row.
    wide = "\U0001f600" * 512
    items = [{"id": f"w{n}", "text": wide, "completed": False} for n in range(12)]
    items.append({"id": "tail", "text": "short enough to fit alone", "completed": False})
    emit.on_plan_updated(SESSION, 3, items=items)
    assert_drained()
    (plan,) = _of("plan/updated")
    ids = [row["id"] for row in plan["items"]]
    assert ids, "no row was admitted at all, so this proves nothing about the order"
    assert len(ids) < len(items), (
        "every row fit, so the line ceiling was never reached -- the test no longer "
        "exercises the path it is about"
    )
    assert ids == [f"w{n}" for n in range(len(ids))], (
        f"expected the prefix before the row that does not fit, got {ids} -- a later "
        "short row admitted past a dropped one makes items a subsequence"
    )
    assert "tail" not in ids, "the short row behind the dropped one was admitted"
    assert plan["total"] == len(items), "total must count every task the plan really had"


def test_a_pinned_origin_is_invisible_until_the_run_actually_starts():
    """Accepted is not started, and only a started run may have an opener.

    A spawn clears the approval gate after it is registered, and a decline returns
    without ever running. Gating every later read on `opened` is what keeps a
    declined spawn from producing a steer or a terminal entry whose cause never
    got written.
    """
    emit.remember_child_origin("ab12", SESSION, 4)
    assert emit.child_origin("ab12") == ("", 0)
    assert emit.open_child_origin("ab12") == (SESSION, 4)
    assert emit.child_origin("ab12") == (SESSION, 4)


def test_opening_an_origin_twice_cannot_produce_two_openers():
    emit.remember_child_origin("ab12", SESSION, 4)
    assert emit.open_child_origin("ab12") == (SESSION, 4)
    assert emit.open_child_origin("ab12") == (SESSION, 4)
    assert emit.open_child_origin("never-pinned") == ("", 0)


def test_a_declined_spawn_releases_its_pin_and_closes_nothing():
    """The exact shape of a spawn refused at the approval gate.

    It was pinned when accepted and never opened, so its terminal report must
    close nothing -- and must still drop the pin rather than leaving it for the
    FIFO to evict much later.
    """
    emit.remember_child_origin("ab12", SESSION, 4)
    assert emit.forget_child_origin("ab12") == ("", 0), "an unopened pin closes nothing"
    assert emit.child_origin("ab12") == ("", 0), "and the pin is gone, not retained"


def test_forgetting_an_opened_origin_returns_it_once_and_then_answers_unknown():
    emit.remember_child_origin("ab12", SESSION, 4)
    emit.open_child_origin("ab12")
    assert emit.forget_child_origin("ab12") == (SESSION, 4)
    assert emit.forget_child_origin("ab12") == ("", 0)


def test_resetting_the_emitter_forgets_pinned_origins():
    """Every other map in the module is cleared on reset; this one is no different."""
    emit.remember_child_origin("ab12", SESSION, 4)
    emit.open_child_origin("ab12")
    emit.reset_caches()
    assert emit.child_origin("ab12") == ("", 0)


def test_an_unrecorded_dispatch_produces_no_closer():
    """An unknown origin yields an empty session id, which the emitter drops.

    That is how a child whose spawn was never recorded -- the flag came on
    mid-flight -- cannot appear in the log as an outcome with no cause.
    """
    _open_session()
    sid, _turn = emit.forget_child_origin("never-seen")
    assert sid == ""
    emit.on_subagent_completed(sid, agent_id="never-seen", duration_ms=5)
    assert_drained()
    assert _of("subagent/completed") == []


def test_the_origin_map_is_bounded_by_uptime_not_by_the_number_of_live_turns():
    """Its entries deliberately OUTLIVE the turn that made them, so FIFO it is."""
    for n in range(emit._MAX_CHILD_ORIGINS + 50):
        emit.remember_child_origin(f"a{n}", SESSION, 1)
    assert len(emit._child_origin) == emit._MAX_CHILD_ORIGINS
    assert emit.open_child_origin("a0") == ("", 0), "the oldest is evicted first"
    assert emit.open_child_origin(f"a{emit._MAX_CHILD_ORIGINS + 49}") == (SESSION, 1)


@pytest.mark.parametrize(
    ("outcome", "closer"), [("completed", "subagent/completed"), ("failed", "subagent/failed")]
)
def test_the_terminal_closer_names_the_session_the_child_ran_in(outcome, closer):
    """A reader finds the child's own transcript from the closer, after result.txt is gone.

    The run's ``_session_id`` is the session its last attempt ran in; the closer is the
    first entry written after that session exists (``subagent/spawned`` is written at
    admission, before the child has one).
    """
    from kiro_crew.subagent_manager.terminal import TerminalCoordinator

    _open_session()
    emit.remember_child_origin("ab12", SESSION, 3)
    assert emit.open_child_origin("ab12") == (SESSION, 3)
    info = SimpleNamespace(
        id="ab12", elapsed=0.5, outcome=outcome, error=None, credits=0.0, _session_id="child-sid-1"
    )
    TerminalCoordinator._record_crew_log_terminal(SimpleNamespace(), info)
    assert_drained()
    assert [d.get("child_sid") for d in _of(closer)] == ["child-sid-1"]


def test_a_child_that_never_got_a_session_closes_without_a_child_sid():
    from kiro_crew.subagent_manager.terminal import TerminalCoordinator

    _open_session()
    emit.remember_child_origin("ab12", SESSION, 3)
    assert emit.open_child_origin("ab12") == (SESSION, 3)
    info = SimpleNamespace(id="ab12", elapsed=0.5, outcome="failed", error="boom", credits=0.0)
    TerminalCoordinator._record_crew_log_terminal(SimpleNamespace(), info)
    assert_drained()
    (data,) = _of("subagent/failed")
    assert "child_sid" not in data


def test_a_recovery_that_never_got_a_session_keeps_the_earlier_attempt_s_child_sid():
    """A cancel-recovery attempt that fails before acquiring clears ``_session_id``; the
    closer must still name the session the child last ran in."""
    from kiro_crew.subagent_manager.terminal import TerminalCoordinator

    _open_session()
    emit.remember_child_origin("ab12", SESSION, 3)
    assert emit.open_child_origin("ab12") == (SESSION, 3)
    info = SimpleNamespace(
        id="ab12",
        elapsed=0.5,
        outcome="failed",
        error="boom",
        credits=0.0,
        _session_id="",
        _last_session_id="first-attempt-sid",
    )
    TerminalCoordinator._record_crew_log_terminal(SimpleNamespace(), info)
    assert_drained()
    (data,) = _of("subagent/failed")
    assert data["child_sid"] == "first-attempt-sid"


@pytest.mark.parametrize(
    ("current", "earlier", "kept"),
    [("attempt-1", "", "attempt-1"), ("", "attempt-1", "attempt-1"), ("", "", "")],
)
def test_each_attempt_remembers_the_last_session_before_clearing(current, earlier, kept):
    """``_run_inner_impl`` clears the cleanup identity first; the last session id survives."""
    import asyncio

    import kiro_crew.subagent as facade
    from kiro_crew.subagent_manager.run import RunEventCoordinator

    info = SimpleNamespace(_session_id=current, _last_session_id=earlier)

    class _Stop(Exception):
        pass

    def _stop(*_a, **_k):
        raise _Stop

    # Stop right after the identity reset: the first statement that reads the time.
    # The bound ``*_impl`` runs on the facade's globals, so its ``time`` is patched there.
    with pytest.MonkeyPatch.context() as patch:
        patch.setattr(facade, "time", SimpleNamespace(time=_stop, monotonic=_stop))
        with pytest.raises(_Stop):
            asyncio.run(RunEventCoordinator._run_inner_impl(SimpleNamespace(), info, "k", None))
    assert info._session_id == ""
    assert info._last_session_id == kept
