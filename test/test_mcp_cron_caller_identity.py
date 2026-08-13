"""``kirocrew-cron`` resolves the calling session from the injected caller block.

The server used to read identity from its own process environment. On a pooled
backend one process serves many sessions, so process environment can only ever
name one of them -- and gatewayd forwards no session-identifying variable to a
shared backend at all, so what it actually read there was EMPTY. Every
session-scoped path then took the empty branch, and those branches disagreed with
each other: the per-job ownership gate allowed, ``cron_list`` skipped its filter,
``cron_add`` stored an ownerless row, and only ``cron_remove_all`` refused. Two of
those are fail-open, which made the ownership gate dead code for exactly the
callers it exists to separate.

What these tests pin, in the order the fix depends on them:

1. The caller block WINS over the process environment -- asserted with the
   environment naming a DIFFERENT session, because a test that merely reads the
   block back would also pass if the block were being ignored in favour of an
   environment that happened to agree.
2. With no block, the environment still resolves, so a non-gateway launch is not
   regressed.
3. The forgeable source is no longer consulted for an authorization decision.
4. One rule for an unidentifiable caller: writes refuse, and its read scope is
   empty rather than waved through.
5. A row with no recorded owner is outside every session's scope, for reading and
   writing alike, and its refusal is indistinguishable from "no such job". Every
   creation path that has no session to name keeps writing such rows (the CLI, the
   importer), so this is a permanent scope rule, not a drainable exemption.
"""

from __future__ import annotations

import uuid

import pytest

from kiro_crew import mcp_cron, mcp_shared, session_pid_sig
from kiro_crew.cron import CronService
from kiro_crew.mcp_caller import CallerContext, set_current_caller
from kiro_crew.mcp_cron import _authz_session_key, _call_tool_inner

_MUTATING_TOOLS = ("cron_update", "cron_remove", "cron_pause", "cron_resume", "cron_trigger")


@pytest.fixture(autouse=True)
def _isolate(monkeypatch, tmp_path):
    """A private cron store, and no ambient identity of any kind.

    Identity is granted per test rather than by a shared fixture: half of this
    module is about what happens when there is none.
    """
    monkeypatch.setattr("kiro_crew.cron._DEFAULT_DIR", tmp_path)
    monkeypatch.setattr("kiro_crew.mcp_cron.config_dir", lambda: tmp_path)
    monkeypatch.setenv("KIROCREW_HOME", str(tmp_path))
    monkeypatch.delenv("KIROCREW_SESSION_KEY", raising=False)
    monkeypatch.delenv("KIROCREW_HOST_PID", raising=False)
    monkeypatch.delenv("KIROCREW_CHANNEL_ID", raising=False)
    monkeypatch.delenv("KIROCREW_CLI", raising=False)
    set_current_caller(None)
    yield tmp_path
    set_current_caller(None)


def _as_session(key: str, *, channel: str = "") -> None:
    """Arrive the way a pooled forwarded call does: carrying a caller block."""
    set_current_caller(CallerContext(session_key=key, session_type="dashboard", channel_id=channel))


def _add_job(name: str) -> str:
    result = _call_tool_inner("cron_add", {"name": name, "message": "go", "every": 120})
    assert "Added job" in result, result
    return result.split()[2]


# --- 1..3: where identity comes from ---------------------------------------


def test_the_caller_block_beats_the_process_environment(monkeypatch) -> None:
    """The whole bug in one assertion.

    The environment names a DIFFERENT session than the block. A pooled backend's
    environment can only ever name one session (or, in practice, none), so the
    block has to outrank it rather than merely be consulted.
    """
    monkeypatch.setenv("KIROCREW_SESSION_KEY", "dashboard:from-env")
    _as_session("dashboard:from-block")

    assert _authz_session_key() == "dashboard:from-block"


def test_no_caller_block_falls_back_to_the_process_environment(monkeypatch) -> None:
    """A non-gateway launch has no block to read and must not be regressed."""
    monkeypatch.setenv("KIROCREW_SESSION_KEY", "dashboard:from-env")

    assert _authz_session_key() == "dashboard:from-env"


def test_the_forgeable_pid_walk_is_not_an_authorization_source(monkeypatch) -> None:
    """``session_pid_<pid>.txt`` must not decide who may delete whose job.

    ``mcp_core`` documents that file as "agent-writable and therefore forgeable",
    and the LENIENT resolver ends its fallback chain by walking ``/proc``
    ancestors over it. Labelling an audit row from it is tolerable; an
    authorization decision is not. Proven by making the walk succeed loudly and
    asserting the answer is still empty.
    """
    monkeypatch.setattr(session_pid_sig, "read_session_pid_txt", lambda *a, **k: "dashboard:forged")

    assert _authz_session_key() == ""


# --- 4: one rule for a caller the gateway cannot name ----------------------


@pytest.mark.parametrize("tool", _MUTATING_TOOLS)
def test_an_unidentified_caller_cannot_write(tool: str, monkeypatch) -> None:
    """Every mutating tool gives the same refusal.

    An unidentified caller can still be sharing a pooled backend with identified
    ones: gatewayd forwards with ``caller=None`` when the stub's Register carries
    no session key and peer resolution fails, so "no identity" does NOT imply a
    1:1 transport. Granting it authority over stored rows would reach another
    session's jobs.
    """
    _as_session("dashboard:owner")
    job_id = _add_job(f"unident-{uuid.uuid4().hex[:8]}")
    set_current_caller(None)

    result = _call_tool_inner(tool, {"job_id": job_id})

    assert "cannot determine which session is calling" in result
    # The row is untouched -- a refusal, not a partial write.
    assert CronService(base_dir=mcp_cron.config_dir()).get_job(job_id) is not None


def test_an_unidentified_caller_cannot_create() -> None:
    """``cron_add`` refuses rather than minting another ownerless row."""
    result = _call_tool_inner(
        "cron_add", {"name": f"anon-{uuid.uuid4().hex[:8]}", "message": "go", "every": 120}
    )

    assert "cannot determine which session is calling" in result
    assert CronService(base_dir=mcp_cron.config_dir()).list_jobs(include_disabled=True) == []


def test_an_unidentified_caller_reads_nothing() -> None:
    """A read is scoped, not refused -- and its scope is empty.

    Same fact as the write refusal: gatewayd forwards ``caller=None`` when a stub
    registers without a session key and peer resolution fails, so this caller may
    be sharing a pooled backend with identified ones. It is handed neither their
    rows nor the admin surface's.
    """
    _as_session("dashboard:owner")
    owned = f"owned-{uuid.uuid4().hex[:8]}"
    _add_job(owned)
    svc = CronService(base_dir=mcp_cron.config_dir())
    ownerless = f"cli-made-{uuid.uuid4().hex[:8]}"
    svc.add_job(name=ownerless, message="go", every_secs=120, session_key="")

    set_current_caller(None)
    listed = _call_tool_inner("cron_list", {})

    assert ownerless not in listed
    assert owned not in listed


def test_the_cli_keeps_its_admin_bypass(monkeypatch) -> None:
    """The refusal never strands an operator; it names this route."""
    _as_session("dashboard:owner")
    job_id = _add_job(f"cli-{uuid.uuid4().hex[:8]}")
    set_current_caller(None)
    monkeypatch.setenv("KIROCREW_CLI", "1")

    assert "cannot determine which session is calling" not in _call_tool_inner(
        "cron_pause", {"job_id": job_id}
    )


# --- 4b: the gate the fix makes reachable at all --------------------------


def test_one_session_cannot_see_or_touch_another_sessions_job() -> None:
    """The separation the ownership gate exists for, now that identity arrives.

    Before the fix both sessions resolved an empty key, so ``cron_list`` skipped
    its filter and the per-job gate returned "allow" -- this assertion could not
    have held for any pair of callers.
    """
    _as_session("dashboard:alice")
    name = f"alice-{uuid.uuid4().hex[:8]}"
    job_id = _add_job(name)

    _as_session("dashboard:bob")
    assert name not in _call_tool_inner("cron_list", {})
    # Anti-enumeration: Bob is told it does not exist, not that it is not his.
    assert "job not found" in _call_tool_inner("cron_pause", {"job_id": job_id}).lower()


def test_the_channel_default_also_comes_from_the_caller_block() -> None:
    """``KIROCREW_CHANNEL_ID`` had the same defect as the session key.

    Process environment can only name one session's channel, and gatewayd forwards
    none to a shared backend -- so a pooled ``cron_add`` defaulted the delivery
    channel to nothing. The block carries ``channelId`` in the same envelope.
    """
    _as_session("slack:thread", channel="C0FROMBLOCK")
    job_id = _add_job(f"chan-{uuid.uuid4().hex[:8]}")

    job = CronService(base_dir=mcp_cron.config_dir()).get_job(job_id)
    assert job is not None and job.channel == "C0FROMBLOCK"


# --- 5: the rows written before the fix -----------------------------------


def test_a_job_created_now_always_records_an_owner() -> None:
    """``cron_add`` through this server never adds to the ownerless set again.

    It cannot shrink that set -- the CLI and the importer keep writing to it -- but
    it stops this server from contributing, so an MCP-created job is always
    attributable to the session that asked for it.
    """
    _as_session("dashboard:alice")
    job_id = _add_job(f"owned-{uuid.uuid4().hex[:8]}")

    job = CronService(base_dir=mcp_cron.config_dir()).get_job(job_id)
    assert job is not None and job.session_key == "dashboard:alice"


def test_an_ownerless_row_is_outside_every_sessions_scope() -> None:
    """A row with no recorded owner: not readable and not writable by a session.

    Every creation path that has no session to name writes one -- ``kirocrew cron
    add`` from the CLI, the onboarding importer, and ``cron_add`` on a pooled
    backend before this change. So this set keeps growing, and it is a permanent
    scope rule rather than a drainable exemption for legacy rows.

    An earlier revision of this change kept such rows VISIBLE, arguing a row with
    no owner has no owner's privacy to breach. That is wrong: an identified session
    is not necessarily the operator, because an allowlisted Slack or Telegram
    participant gets a session of their own, and a job's ``message`` and
    ``command``/``script`` are arbitrary payloads.
    """
    svc = CronService(base_dir=mcp_cron.config_dir())
    name = f"unowned-{uuid.uuid4().hex[:8]}"
    unowned = svc.add_job(name=name, message="go", every_secs=120, session_key="")

    _as_session("dashboard:alice")
    assert name not in _call_tool_inner("cron_list", {})
    for tool in _MUTATING_TOOLS:
        result = _call_tool_inner(tool, {"job_id": unowned.id})
        assert "job not found" in result.lower(), (tool, result)
    # Still there, and still enabled: refused, not partially applied.
    after = CronService(base_dir=mcp_cron.config_dir()).get_job(unowned.id)
    assert after is not None and after.enabled is True


def test_the_refusal_for_an_ownerless_row_is_not_an_existence_oracle() -> None:
    """It must read exactly like the answer for an id that does not exist.

    The row is outside the caller's scope now, so a distinct message would confirm
    the existence of a job it cannot see -- an enumeration oracle over the admin
    surface. An earlier revision named the row and pointed at the CLI, which was
    safe only while such rows were listed to the caller.
    """
    svc = CronService(base_dir=mcp_cron.config_dir())
    unowned = svc.add_job(
        name=f"unowned-{uuid.uuid4().hex[:8]}", message="go", every_secs=120, session_key=""
    )
    _as_session("dashboard:alice")

    real = _call_tool_inner("cron_pause", {"job_id": unowned.id})
    fake = _call_tool_inner("cron_pause", {"job_id": "deadbeef"})

    assert real.replace(unowned.id, "X") == fake.replace("deadbeef", "X")


def test_every_cron_tool_audit_names_the_calling_session() -> None:
    """A SUCCESSFUL authorization is on the trail, with the identity it authorized.

    ``call_tool_with_logging`` already records every cron tool invocation, but
    ``mcp_cron`` passed it the hardcoded label "mcp_cron", so the record could not
    say WHO was authorized -- and a ``cron_list`` where the caller owns everything
    withholds nothing, so the scoping helper deliberately adds no event either.
    Together that left the authorized case attributable to nobody. ``mcp_core``
    already resolved the real caller here for exactly this reason; cron was the
    outlier.

    Asserted through the PUBLIC ``_call_tool`` entry point, not the inner one, since
    the wrapper is the thing under test.
    """
    _as_session("dashboard:alice")
    _add_job(f"mine-{uuid.uuid4().hex[:8]}")

    events: list[dict] = []

    class _Recorder:
        def log_tool_invocation(self, **kw: object) -> None:
            events.append(dict(kw))

        def __getattr__(self, _name: str):
            return lambda *a, **k: None

    original = mcp_cron.sel
    original_shared = mcp_shared.sel
    mcp_cron.sel = lambda: _Recorder()  # type: ignore[assignment]
    mcp_shared.sel = lambda: _Recorder()  # type: ignore[assignment]
    try:
        mcp_cron._call_tool("cron_list", {})
    finally:
        mcp_cron.sel = original  # type: ignore[assignment]
        mcp_shared.sel = original_shared  # type: ignore[assignment]

    listed = [e for e in events if e.get("tool_name") == "cron_list"]
    assert listed, events
    assert all(e["session_key"] == "dashboard:alice" for e in listed), listed
    # And exactly one: the caller owns everything it can see, so the scoping
    # helper must not add a duplicate alongside the wrapper's record.
    assert len(listed) == 1, listed


def test_the_list_scoping_decision_lands_on_the_audit_trail() -> None:
    """Withholding rows from a read is an authorization decision, so it is logged.

    Every other one in ``mcp_cron`` already was -- both refusal helpers, and
    ``cron_remove_all``'s ``scoped`` outcome -- and this was the least visible of
    them: an unidentifiable caller has EVERYTHING withheld and left no trace.

    ``denied`` when nothing survives the filter, ``scoped`` when some rows do, and
    NO event when the caller owns everything it could see: ``cron_list`` is called
    often and a row-for-row event per call would bury the trail.
    """
    svc = CronService(base_dir=mcp_cron.config_dir())
    svc.add_job(
        name=f"theirs-{uuid.uuid4().hex[:8]}",
        message="go",
        every_secs=120,
        session_key="dashboard:bob",
    )

    events: list[dict] = []

    class _Recorder:
        def log_tool_invocation(self, **kw: object) -> None:
            events.append(dict(kw))

        def __getattr__(self, _name: str):  # other SEL calls are not under test
            return lambda *a, **k: None

    original = mcp_cron.sel
    mcp_cron.sel = lambda: _Recorder()  # type: ignore[assignment]
    try:
        # Unidentified: everything withheld -> denied.
        set_current_caller(None)
        _call_tool_inner("cron_list", {})
        assert [e for e in events if e.get("outcome") == "denied"], events

        # Identified, owns nothing of what exists -> also denied, but named.
        events.clear()
        _as_session("dashboard:alice")
        _call_tool_inner("cron_list", {})
        denied = [e for e in events if e.get("outcome") == "denied"]
        assert denied and denied[0]["session_key"] == "dashboard:alice", events

        # Owns some -> scoped.
        events.clear()
        _add_job(f"mine-{uuid.uuid4().hex[:8]}")
        _call_tool_inner("cron_list", {})
        assert [e for e in events if e.get("outcome") == "scoped"], events

        # Owns everything visible -> no event at all.
        events.clear()
        svc2 = CronService(base_dir=mcp_cron.config_dir())
        for job in svc2.list_jobs(include_disabled=True):
            if job.session_key != "dashboard:alice":
                svc2.remove_job(job.id)
        _call_tool_inner("cron_list", {})
        assert [e for e in events if e.get("tool_name") == "cron_list"] == [], events
    finally:
        mcp_cron.sel = original  # type: ignore[assignment]


def test_the_scope_of_an_empty_key_is_empty_not_the_ownerless_rows() -> None:
    """Guards the one comparison that would silently reopen the hole.

    ``_owned_by`` filters on ``j.session_key == session_key``, and an ownerless row
    stores ``""`` -- so an empty key handed to it would select EVERY ownerless row
    rather than nothing. ``cron_list`` and ``cron_remove_all`` both reach it, the
    latter only after refusing an unidentifiable caller, so the guard is defence
    for a default that fails open rather than dead code.
    """
    svc = CronService(base_dir=mcp_cron.config_dir())
    unowned = svc.add_job(
        name=f"unowned-{uuid.uuid4().hex[:8]}", message="go", every_secs=120, session_key=""
    )

    assert mcp_cron._owned_by([unowned], "") == []


def test_remove_all_never_sweeps_an_ownerless_row() -> None:
    """The bulk path uses the same one scope as everything else.

    A wider scope here would have deleted every CLI-created job the moment any
    session ran cron_remove_all -- the same fail-open as the per-job gate, wearing
    a different sleeve. It happened in an earlier revision of this change, which is
    why reads and writes now share ONE function.
    """
    svc = CronService(base_dir=mcp_cron.config_dir())
    kept = svc.add_job(
        name=f"cli-{uuid.uuid4().hex[:8]}", message="go", every_secs=120, session_key=""
    )
    _as_session("dashboard:alice")
    mine = _add_job(f"mine-{uuid.uuid4().hex[:8]}")

    assert "Removed 1 job(s)" in _call_tool_inner("cron_remove_all", {})

    after = CronService(base_dir=mcp_cron.config_dir())
    assert after.get_job(kept.id) is not None
    assert after.get_job(mine) is None
