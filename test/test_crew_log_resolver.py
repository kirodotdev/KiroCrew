"""The slot-key -> crew-log-unit resolver, and the emitters it unlocks.

Kept separate from ``test_crew_log_emit.py`` because these tests are about
a different question. That file asks whether an entry the runner hands over lands
correctly; this one asks whether a site that holds only a KEY can find the unit to
hand it to at all, and whether the eight families keyed that way say only what
their site observed.
"""

from __future__ import annotations

import json
from pathlib import Path
from types import SimpleNamespace

import pytest

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


# --- doubles ---------------------------------------------------------------


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

    A crew log that omits a fact is behind. One that files a fact under the wrong
    session is wrong, and no reader can tell.

    Asserted against the literal empty string rather than ``resolve.UNKNOWN``: the
    contract the emitter relies on is that this value is FALSY, since its own guard
    is ``not session_id``. Comparing to the constant would hold no matter what the
    constant became.
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


# --- approvals -------------------------------------------------------------


def test_an_approval_is_requested_before_it_is_decided():
    """A closer never precedes its opener, even though two tasks write them."""
    _open_session()
    emit.on_turn_started(SESSION, 1, "user")
    emit.on_approval_requested(SESSION, 1, approval_id="r1", tool="shell", reason="rm -rf build")
    emit.on_approval_decided(SESSION, 1, approval_id="r1", decision="approved")
    assert emit.flush()
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
    assert emit.flush()
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
    assert emit.flush()
    unnamed, named = _of("approval/requested")
    assert "tool" not in unnamed and "reason" not in unnamed
    assert unnamed["approval_id"] == "r1", "the request is still recorded"
    assert named["tool"] == "shell"


def test_a_long_approval_reason_is_clipped_rather_than_losing_the_entry():
    """An entry refused for one oversize field is a fact silently missing."""
    _open_session()
    emit.on_turn_started(SESSION, 1, "user")
    emit.on_approval_requested(SESSION, 1, approval_id="r1", tool="shell", reason="x" * 5000)
    assert emit.flush()
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
    assert emit.flush()
    (data,) = _of("approval/requested")
    assert "sk-live-ABCDEF1234567890abcdef" not in data["reason"]


# --- plan ------------------------------------------------------------------


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
    assert emit.flush()
    (data,) = _of("plan/updated")
    assert [row["state"] for row in data["items"]] == ["done", "open"]
    assert {row["state"] for row in data["items"]} <= {"done", "open"}


def test_no_task_list_writes_nothing_but_an_empty_one_records_a_cleared_plan():
    """Absent data and a plan of zero tasks are different facts."""
    _open_session()
    emit.on_turn_started(SESSION, 1, "user")
    emit.on_plan_updated(SESSION, 1, items=None)
    assert emit.flush()
    assert _of("plan/updated") == []
    emit.on_plan_updated(SESSION, 1, items=[])
    assert emit.flush()
    assert _of("plan/updated") == [{"turn": 1, "items": []}]


def test_the_two_unconditionally_called_emitters_do_no_work_with_the_flag_off(monkeypatch):
    """The runner calls these two on every plan change and every approval prompt.

    Both do real work before `_write` gets its own chance to no-op -- a redaction
    per task and a serialize probe per admitted row -- so relying on that guard
    alone spends event-loop time on a feature that is off by default. The subagent
    and background emitters need no guard of their own: their callers already check.

    Asserted by making the work itself fail if it runs, which is what keeps this
    from passing for the wrong reason once the bodies change.
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

    ``_clip`` bounds each text in characters while the store serializes with
    ``ensure_ascii`` -- six bytes for a BMP character, twelve for a surrogate pair.
    A dozen separately-legal rows of emoji therefore serialize past the 64 KiB entry
    ceiling, where the append is REFUSED and the whole update disappears. Eleven
    rows of 500 emoji is enough to cross it, so this is the shape a real todo list
    reaches, not a synthetic extreme.
    """
    _open_session()
    emit.on_turn_started(SESSION, 1, "user")
    emit.on_plan_updated(
        SESSION,
        1,
        items=[{"id": str(n), "text": "\U0001f600" * 500, "completed": False} for n in range(40)],
    )
    assert emit.flush()
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
    assert emit.flush()
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
    assert emit.flush()
    (data,) = _of("plan/updated")
    assert len(data["items"]) == emit._MAX_PLAN_ITEMS
    assert data["total"] == 500


def test_a_plan_entry_is_ignorable_because_nothing_later_depends_on_reading_it():
    _open_session()
    emit.on_turn_started(SESSION, 1, "user")
    emit.on_plan_updated(SESSION, 1, items=[{"id": "a", "text": "t", "completed": False}])
    assert emit.flush()
    (entry,) = [e for e in _body() if e["type"] == "plan/updated"]
    assert entry.get("ignorable") is True


# --- background ------------------------------------------------------------


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
    assert emit.flush()
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
    assert emit.flush()
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


def test_a_background_owner_is_resolved_before_the_call_not_after_it():
    """The resolver answers "now", so resolving in the teardown is the wrong now.

    A slot reset, switch or compaction during the model call cold-starts a NEW ACP
    session id. Resolving at teardown would hand this call's spend to the successor
    -- a session that never incurred it -- silently, in an append-only file. So the
    owner is pinned before the call and the teardown writes to that pin.
    """
    from kiro_crew.llm_helpers import _background_crew_log_owner, _record_background_crew_log

    _open_session()
    sessions = _Sessions({"dashboard:chat-7": _Provider(SESSION)})
    owner = _background_crew_log_owner(sessions, "dashboard:chat-7", "title")
    assert owner == SESSION
    # The slot is reset mid-call: the registry now serves a different unit.
    sessions._providers["dashboard:chat-7"] = _Provider("successor-sid")
    _record_background_crew_log(
        owner, "title", _Usage(credits=1.0), model="m", provider="acp", elapsed_ms=1
    )
    assert emit.flush()
    assert _entries(SESSION), "the spend stayed with the session that incurred it"
    assert _of("background/completed")[0]["kind"] == "title"
    assert _entries("successor-sid") == [], "and did not follow the successor"


def test_both_background_helpers_pin_the_owner_before_their_first_await():
    """Resolving after a suspension point names the successor, not the incurrer.

    The test above proves the resolver answers "now"; this one proves the call sites
    ask at the right now. Both helpers begin by acquiring a background session, and
    that acquisition suspends -- it can take the runtime lock and start a runtime --
    so an owner resolved after it can already belong to a slot that was reset while
    the acquisition waited. Asserted on the source rather than by racing an event
    loop, because the property is an ORDER in the function body and a future edit
    that moves the resolve back down would restore the bug silently.
    """
    import ast
    import inspect

    from kiro_crew import llm_helpers

    tree = ast.parse(inspect.getsource(llm_helpers))
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
    assert emit.flush()
    (data,) = _of("background/completed")
    assert data["kind"] == "memory_consolidation"
    assert data["credits"] == pytest.approx(2.5)
    assert data["tokens"] == {"input": 40}


# --- children --------------------------------------------------------------


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
    assert emit.flush()
    kinds = [e["type"] for e in _body()]
    assert kinds.index("subagent/spawned") < kinds.index("subagent/completed")
    (spawned,) = _of("subagent/spawned")
    assert spawned["turn"] == 4
    assert spawned["scope"] == {"memory": False, "lessons": True, "project": True}


def test_a_spawn_carries_no_ref_because_the_child_has_no_log_to_cite():
    """The schema describes one; no subagent path opens a crew log to point at.

    A ``ref`` written now would cite a file that does not exist, which a reader
    cannot distinguish from one that was deleted. Checked in BOTH places a citation
    could appear -- the envelope's own field and the entry's data -- because only
    the envelope form is a real reference and a `ref` key smuggled into data would
    read like one to anything scanning the line.
    """
    _open_session()
    emit.on_turn_started(SESSION, 1, "user")
    emit.on_subagent_spawned(SESSION, 1, agent_id="ab12", agent="kirocrew")
    assert emit.flush()
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
    assert emit.flush()
    (data,) = _of("subagent/spawned")
    assert "turn" not in data
    assert data["agent_id"] == "ab12", "the child is still recorded"


def test_a_completed_child_reports_no_tokens_and_no_credits():
    """Nothing in the subagent runtime measures either; zeros would be a claim."""
    _open_session()
    emit.on_subagent_completed(SESSION, agent_id="ab12", duration_ms=5)
    assert emit.flush()
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
    assert emit.flush()
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

    Skipping the pin on a ``from_queue`` pass costs nothing in the process that
    ACCEPTED the child, because the pin refuses to move. In a process that replayed
    the row there is no pin to keep, so skipping it drops that child's spawn, steer
    and terminal entries with nothing left to report the gap. The turn stays
    unobserved: the one that asked is gone with the process that held it, and the
    writers omit an unobserved ordinal rather than naming a turn that never ran.
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


@pytest.mark.asyncio
async def test_a_claim_the_store_cannot_take_is_pinned_before_the_row_is_left_queued(monkeypatch):
    """The asking turn is readable on THIS pass and on no later one.

    A store too busy to take the row leaves the caller a queued handle and lets the
    pump retry after the admit wait. That retry re-enters under ``from_queue``,
    where the turn that asked cannot be read -- so a pass that returns
    without pinning hands the retry an empty map, and the child's spawn entry
    carries no turn even though the process knew it all along. Pinning here is what
    the retry then keeps, because the pin refuses to move.
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
    finally:
        await mgr.cancel_all()


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
        assert emit.flush()
        assert _of("subagent/spawned") == [], "a run that never started has no opener"

        # The prerequisite met: the same call does write it.
        monkeypatch.setattr(_subagent, "create_agent_folder", lambda *_a, **_k: None)
        started = SubagentInfo(id="folder-ok-1", task="t", agent="", parent_session_key="web-1")
        emit.remember_child_origin(started.id, SESSION, 4)
        mgr._log_spawned(started)
        assert emit.flush()
        assert [e["agent_id"] for e in _of("subagent/spawned")] == ["folder-ok-1"]
    finally:
        await mgr.cancel_all()


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
    assert emit.flush()
    (plan,) = _of("plan/updated")
    leaked, ordinary = plan["items"]
    assert "ghp_" not in leaked["id"] and "REDACTED" in leaked["id"], leaked
    assert ordinary["id"] == "task-7", "redaction must not rewrite an ordinary id"


def test_a_batch_the_writer_already_holds_still_reads_as_owed():
    """The writer claims a session's entries by taking them OUT of the queue.

    Between that claim and the last append of the batch, the entries behind the one
    being written are still owed while none of them is in the queue -- so a caller
    asking whether this process owes the session anything would be told no, and the
    child repair would close a run whose real outcome is in the batch.
    """
    seen: list[bool] = []
    queue_state: list[bool] = []

    def _look_from_inside_the_batch() -> None:
        queue_state.append(SESSION in emit._pending)
        seen.append(emit._owes_entries(SESSION))

    with pytest.MonkeyPatch.context() as patch:
        patch.setattr(emit, "_start_drain", lambda: None)
        emit._buffer(SESSION, emit._PendingJob(_look_from_inside_the_batch, "looks"))
        emit._buffer(SESSION, emit._PendingJob(lambda: None, "queued behind it"))
        emit._drain_once()
    assert queue_state == [False], "the batch was not claimed out of the queue"
    assert seen == [True], "an entry claimed behind the running one was not owed"
    assert not emit._owes_entries(SESSION), "the claim outlived the batch it was taken for"


def test_a_pass_that_stopped_early_releases_what_it_never_reached():
    """The claim a retained tail leaves behind, and why it must not be permanent.

    A transient failure sends the rest of the batch back to the queue, so those
    entries are owed twice: once as queued work, once as a claim this pass took and
    never spent. The queue side clears itself when the retry lands. The claim side
    clears only if the pass releases what it never reached on the way out -- and a
    claim that outlives its batch makes the session owe something forever, which is
    the answer the child repair reads to decide a run is still live.

    The drain is held rather than left to the writer: a buffered entry is picked up
    on its own within milliseconds, and a batch of one leaves nothing unattempted,
    so a test that lets the writer run cannot reach this path at all.
    """
    failures = {"left": 1}

    def _fail_once() -> None:
        if failures["left"]:
            failures["left"] -= 1
            raise OSError("input/output error")

    with pytest.MonkeyPatch.context() as patch:
        patch.setattr(emit, "_start_drain", lambda: None)
        emit._buffer(SESSION, emit._PendingJob(_fail_once, "fails once"))
        emit._buffer(SESSION, emit._PendingJob(lambda: None, "never reached"))
        emit._drain_once()
        assert failures["left"] == 0, "the injected failure never fired, so nothing was retained"
        assert SESSION in emit._pending, "the tail was not retained, so this path was not taken"
        assert SESSION not in emit._claimed_sessions, "a claim outlived the pass that took it"
        emit._drain_once()
    assert not emit._owes_entries(SESSION), "the session still owes something after both landed"


def test_a_child_whose_closer_is_not_yet_handed_over_is_not_reported_gone():
    """The gap the two reads alone cannot cover, and what covers it.

    The normal completion path flips ``done`` in the run loop, which drops the
    child from the manager's running set, and hands the closer over later from the
    report task. In between, the child is absent from the running set AND the
    session owes nothing -- so liveness and debt, in either order, both say gone,
    and a repair reading there synthesises an ``unknown`` that then stands beside
    the real outcome in a file nothing rewrites.

    The child's own origin pin is what closes it: opened when the spawn is
    recorded, released only once the closer reaches the writer, so it is held
    across exactly that gap.
    """
    _open_session()
    emit.remember_child_origin("ab12", SESSION, 3)
    assert emit.open_child_origin("ab12") == (SESSION, 3)
    assert emit.flush(timeout=20.0), "the session must owe nothing, or debt would mask the pin"

    with pytest.MonkeyPatch.context() as patch:
        patch.setattr(emit, "_child_liveness", lambda _agent_id: False)
        probe = emit._child_gone_probe(SESSION)
        assert probe is not None
        assert probe("ab12") is False, "a child still holding its origin pin was reported gone"
        emit.forget_child_origin("ab12")
        assert probe("ab12") is True, "a released pin must stop vouching for the child"


def test_a_repair_is_not_blocked_by_its_own_job_being_the_one_in_flight(monkeypatch):
    """The repair runs INSIDE a claimed batch, so it must not read itself as debt.

    A resume submits its open-and-repair work through the same writer, which claims
    a session's entries before running them. Counting the job that is asking would
    make every child look live on exactly the path the repair exists for, so the
    dangling opener a torn-down writer left behind would never be closed.
    """
    _open_session()
    monkeypatch.setattr(emit, "_child_liveness", lambda _agent_id: False)
    answers: list[object] = []

    def _ask_from_inside_the_batch() -> None:
        probe = emit._child_gone_probe(SESSION)
        answers.append(probe("ab12") if probe is not None else None)

    emit._buffer(SESSION, emit._PendingJob(_ask_from_inside_the_batch, "repair asking"))
    assert emit.flush(timeout=20.0)
    assert answers == [True], "the asking job was counted as debt against itself"


def test_a_terminal_is_recorded_before_the_child_leaves_the_running_set():
    """The repair's probe runs on the writer's own thread, not the caller's.

    Flipping ``done`` first leaves an instant where the manager has stopped listing
    the child and the writer is owed nothing for it. A resume repair reading exactly
    there synthesises an ``unknown`` outcome that the real terminal then contradicts,
    in a file nothing rewrites. Handing the entry over first means one of the two
    always refuses.

    Pinned on the source order because the invariant IS an order between two
    statements, and the thread that would observe the gap is the writer's: a test on
    this thread cannot schedule itself into it, so it would pass either way.
    """
    import inspect

    from kiro_crew.subagent_manager.terminal import TerminalCoordinator

    body = inspect.getsource(TerminalCoordinator._report_terminal_impl)
    record = body.index("self._record_crew_log_terminal(info)")
    flip = body.index("info.done = True")
    assert record < flip, (
        "info.done is flipped before the terminal entry is handed over; this flip is "
        "the one for paths that reach a terminal without the run loop, and the normal "
        "path's earlier flip is covered by the origin pin instead"
    )


def test_the_closer_is_handed_over_before_its_child_s_origin_pin_is_released():
    """The pin must outlive the handover, or it leaves the gap it exists to cover.

    Releasing first restores the hole exactly: for the instant between the release
    and the entry reaching the writer, the child is not running, holds no pin and
    owes nothing, so a repair reading there closes it as ``unknown``. The release
    therefore happens after the emit, and reporting stays one-shot without the pop
    because every route to it is gated on ``_claim_finalize``.
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

    info = SimpleNamespace(id="ab12", elapsed=0.5, outcome="completed", error=None)
    with pytest.MonkeyPatch.context() as patch:
        patch.setattr(emit, "on_subagent_completed", _spy)
        TerminalCoordinator._record_crew_log_terminal(SimpleNamespace(), info)
    assert held == [True], "the pin was released before the closer was handed over"
    assert emit.child_origin("ab12")[0] == "", "the pin was not released afterwards"
    assert emit.flush(timeout=20.0)
    assert [d["agent_id"] for d in _of("subagent/completed")] == ["ab12"]


def test_a_plan_the_line_cannot_hold_records_a_prefix_not_a_subsequence():
    """``items`` is read as the FRONT of the plan, so a gap in it is a lie.

    Rows are admitted one at a time against the line's byte ceiling. Skipping only
    the row that does not fit would let a shorter row behind it in, and a reader
    diffing consecutive entries would see the plan reorder and a task vanish. The
    first row the line cannot hold therefore closes admission, while ``total`` keeps
    counting what the plan really had.
    """
    _open_session()
    # `text` is clipped in CHARACTERS while the line is refused in BYTES, so rows
    # that are each individually legal still overflow together: an emoji is four
    # bytes, and 512 of them is 2 KiB per row.
    wide = "\U0001f600" * 512
    items = [{"id": f"w{n}", "text": wide, "completed": False} for n in range(12)]
    items.append({"id": "tail", "text": "short enough to fit alone", "completed": False})
    emit.on_plan_updated(SESSION, 3, items=items)
    assert emit.flush()
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
    assert emit.flush()
    assert _of("subagent/completed") == []


def test_the_origin_map_is_bounded_by_uptime_not_by_the_number_of_live_turns():
    """Its entries deliberately OUTLIVE the turn that made them, so FIFO it is."""
    for n in range(emit._MAX_CHILD_ORIGINS + 50):
        emit.remember_child_origin(f"a{n}", SESSION, 1)
    assert len(emit._child_origin) == emit._MAX_CHILD_ORIGINS
    assert emit.open_child_origin("a0") == ("", 0), "the oldest is evicted first"
    assert emit.open_child_origin(f"a{emit._MAX_CHILD_ORIGINS + 49}") == (SESSION, 1)
