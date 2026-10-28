"""``kirocrew-cron`` resolves the calling session from the injected caller block.

Identity must come from the injected caller block, not the process environment.
On a pooled backend one process serves many sessions, so process environment can
only ever name one of them -- and gatewayd forwards no session-identifying
variable to a shared backend at all, so reading it there yields EMPTY. A
session-scoped path that took that empty branch would make the branches disagree:
the per-job ownership gate would allow, ``cron_list`` would skip its filter,
``cron_add`` would store an ownerless row, and only ``cron_remove_all`` would
refuse. Two of those are fail-open, which would make the ownership gate dead code
for exactly the callers it exists to separate.

What these tests pin, in the order the fix depends on them:

1. The caller block WINS over the process environment -- asserted with the
   environment naming a DIFFERENT session, because a test that merely reads the
   block back would also pass if the block were being ignored in favour of an
   environment that happened to agree.
2. With no block, the environment still resolves, so a non-gateway launch is not
   regressed.
3. The forgeable source is not consulted for an authorization decision.
4. One rule for an unidentifiable caller: writes refuse, and its read scope is
   empty rather than waved through.
5. A row with no recorded owner is outside every session's scope, for reading and
   writing alike, and its refusal is indistinguishable from "no such job". Every
   creation path that has no session to name keeps writing such rows (the CLI, the
   importer), so this is a permanent scope rule, not a drainable exemption.
6. A row whose own ``session_key`` names the same crew member the caller's
   vouched key names belongs to that member, so any session whose key names the
   member reaches it: read and write alike, through the one scope function. The
   match is key structure on BOTH sides — never ``job.member_id``, which is the
   execution member ``cron_add`` lets the caller choose and so is no evidence of
   who owns a row. A key that names no member gains nothing, a row created
   outside the member's sessions stays out of reach however it is labelled, an
   ownerless row stays outside every scope, and an unidentifiable caller still
   reaches nothing.
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


#: The forgery, spelled the way a config reaches this process.
#:
#: Every test below sets it from OUTSIDE, which is the only thing an attacker
#: has to do: a spawned server's environment is shaped by the spec that spawns
#: it, and the two channels the spec has -- an ``env`` block and a ``command``
#: like ``sh -c 'KIROCREW_CLI=1 exec kirocrew-cron'`` -- differ only in which
#: one a filter happens to inspect. By the time the value is in ``os.environ``
#: they are the same fact, which is why asserting on the CONSUMER covers both
#: channels at once and why no test here needs to spawn anything.
_FORGED = ("KIROCREW_CLI", "1")


@pytest.mark.parametrize("tool", _MUTATING_TOOLS)
def test_a_forged_cli_flag_does_not_unlock_another_sessions_job(tool, monkeypatch) -> None:
    """The mutation gate. Forging the flag must not reach Alice's row.

    This is the assertion that fails before the fix: the gate's first statement
    was ``if _caller_is_cli(): return None``, so setting two characters in the
    environment returned "authorized" for every one of these tools without the
    caller naming a session at all.
    """
    _as_session("dashboard:alice")
    job_id = _add_job(f"alice-{uuid.uuid4().hex[:8]}")

    set_current_caller(None)
    monkeypatch.setenv(*_FORGED)

    assert "cannot determine which session is calling" in _call_tool_inner(
        tool, {"job_id": job_id, "message": "x"}
    )


def test_a_forged_cli_flag_does_not_widen_the_list(monkeypatch) -> None:
    """The read gate, over BOTH kinds of row the filter withholds."""
    _as_session("dashboard:alice")
    owned = f"alice-{uuid.uuid4().hex[:8]}"
    _add_job(owned)
    svc = CronService(base_dir=mcp_cron.config_dir())
    ownerless = f"cli-made-{uuid.uuid4().hex[:8]}"
    svc.add_job(name=ownerless, message="go", every_secs=120, session_key="")

    set_current_caller(None)
    monkeypatch.setenv(*_FORGED)
    listed = _call_tool_inner("cron_list", {})

    assert owned not in listed
    assert ownerless not in listed


def test_a_forged_cli_flag_does_not_sweep_every_sessions_jobs(monkeypatch) -> None:
    """The destructive one. A forged flag must not delete the whole store."""
    _as_session("dashboard:alice")
    alice = f"alice-{uuid.uuid4().hex[:8]}"
    _add_job(alice)
    _as_session("dashboard:bob")
    bob = f"bob-{uuid.uuid4().hex[:8]}"
    _add_job(bob)

    set_current_caller(None)
    monkeypatch.setenv(*_FORGED)

    assert "cannot determine which session is calling" in _call_tool_inner("cron_remove_all", {})
    svc = CronService(base_dir=mcp_cron.config_dir())
    survived = {j.name for j in svc.list_jobs(include_disabled=True)}
    assert survived == {alice, bob}


def test_a_forged_cli_flag_does_not_mint_an_ownerless_row(monkeypatch) -> None:
    """``cron_add``'s branch. The flag let an unidentified caller store a row
    with no owner -- the very shape that is unreachable from MCP afterwards."""
    set_current_caller(None)
    monkeypatch.setenv(*_FORGED)

    result = _call_tool_inner(
        "cron_add", {"name": f"forged-{uuid.uuid4().hex[:8]}", "message": "go", "every": 120}
    )

    assert "cannot determine which session is calling" in result
    svc = CronService(base_dir=mcp_cron.config_dir())
    assert svc.list_jobs(include_disabled=True) == []


def test_the_flag_changes_no_answer_this_server_gives(monkeypatch) -> None:
    """The regression that a per-site test cannot see.

    Each assertion above pins one consumer. Someone reintroducing the fast path
    at a SIXTH decision would leave all of them green, so this pins the property
    the fix actually establishes: the variable is inert. Stated as behaviour
    rather than as a grep for the name, it also survives a regression that reads
    the value under a different spelling -- and it still allows the comments that
    explain to the next reader why nothing consults it.
    """
    _as_session("dashboard:alice")
    job_id = _add_job(f"alice-{uuid.uuid4().hex[:8]}")

    probes = (
        ("cron_list", {}),
        ("cron_remove_all", {}),
        *((tool, {"job_id": job_id, "message": "x"}) for tool in _MUTATING_TOOLS),
        ("cron_add", {"name": "n", "message": "go", "every": 120}),
    )

    for tool, args in probes:
        set_current_caller(None)
        monkeypatch.delenv("KIROCREW_CLI", raising=False)
        without = _call_tool_inner(tool, args)

        set_current_caller(None)
        monkeypatch.setenv(*_FORGED)
        with_flag = _call_tool_inner(tool, args)

        assert without == with_flag, f"{tool} answered differently with the flag set"


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

    Asserted through ``_call_tool_locally``, the validated host dispatcher used by
    the authenticated gateway, since its audit wrapper is the thing under test.
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
        mcp_cron._call_tool_locally("cron_list", {})
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
                svc2.remove_job(job.id, actor="test", source="test")
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


# --- 6: the scoping decision has to be legible in the ANSWER ----------------


def test_a_scoped_empty_list_does_not_read_as_an_empty_registry() -> None:
    """The distinction this whole file is about.

    Bob owns nothing, Alice owns a job. Bob's answer must not be the string a
    caller gets when the registry is genuinely empty, because that reads as
    "nothing is scheduled" and sends the reader looking for a broken store. The
    inequality below is the actual invariant; the wording can change freely
    underneath it.
    """
    _as_session("dashboard:alice")
    _add_job(f"alice-{uuid.uuid4().hex[:8]}")

    _as_session("dashboard:bob")
    out = _call_tool_inner("cron_list", {})

    assert out != "No cron jobs."
    assert out == mcp_cron._SCOPED_EMPTY
    # It has to leave the reader somewhere to go, or it only relabels the dead end.
    assert "cron list" in out and "Schedule page" in out


def test_a_genuinely_empty_registry_still_says_so() -> None:
    """The other half of the same invariant: two states, two strings.

    Without this, collapsing both branches back onto one message would still pass
    the test above.
    """
    _as_session("dashboard:alice")
    assert _call_tool_inner("cron_list", {}) == "No cron jobs."


def test_an_unidentified_caller_is_told_its_scope_is_empty_not_the_store() -> None:
    """The branch where the MOST is withheld gets the clearest answer.

    ``caller=None`` on a pooled connection withholds every row, so this caller is
    the one most likely to misread an empty list as an empty registry.
    """
    _as_session("dashboard:owner")
    _add_job(f"owned-{uuid.uuid4().hex[:8]}")

    set_current_caller(None)
    out = _call_tool_inner("cron_list", {})

    assert out == mcp_cron._SCOPED_EMPTY
    assert out != "No cron jobs."


def test_the_tool_description_warns_that_the_list_is_scoped() -> None:
    """A caller should not have to hit the empty case to learn the rule.

    The message fixes the symptom after the fact; the description is what stops a
    caller concluding "no scheduled work exists" from a filtered result it never
    knew was filtered.
    """
    spec = next(t for t in mcp_cron._list_tools() if t["name"] == "cron_list")
    desc = spec["description"]

    assert "SCOPED TO THE CALLING SESSION" in desc
    assert "not that none are scheduled" in desc


# --- 7: channel-agent confinement ------------------------------------------
#
# A channel agent (session key ``channel:<channel_id>:<agent_id>``) is confined
# to channel posts. cron_add/cron_update let it schedule a durable job that runs
# as a MORE privileged agent, so both are denied here at MCP dispatch -- keyed on
# the verified caller identity, because an auto-approved call (cron in the
# agent's allowedTools) fires no permission event for channel.py's guard to see.


def test_channel_agent_cannot_create_cron() -> None:
    """A channel agent's ``cron_add`` is refused and mints no job."""
    _as_session("channel:C123:reviewer")

    result = _call_tool_inner(
        "cron_add",
        {
            "name": f"esc-{uuid.uuid4().hex[:8]}",
            "message": "go",
            "agent": "kirocrew",
            "approval_mode": "auto",
            "every": 60,
        },
    )

    assert "not available to channel agents" in result
    assert CronService(base_dir=mcp_cron.config_dir()).list_jobs(include_disabled=True) == []


def test_channel_agent_cannot_update_cron() -> None:
    """A channel agent's ``cron_update`` is refused before it can re-point an
    existing job's ``agent_id`` -- and the refusal precedes the ownership check,
    so it does not even reveal whether the job exists."""
    _as_session("channel:C123:reviewer")

    result = _call_tool_inner("cron_update", {"job_id": "any-job-id", "agent": "kirocrew"})

    assert "not available to channel agents" in result


def test_channel_agent_cron_denied_needs_no_permission_event() -> None:
    """The denial is at MCP dispatch, not the permission-request event.

    ``_call_tool_inner`` is the dispatch path a call reaches AFTER approval (an
    auto-approved MCP tool emits no permission event at all), so a refusal here
    proves the boundary holds even when channel.py's interactive guard never
    ran. Even with no ``agent`` override -- a plain self-scoped schedule -- the
    channel agent is contained, matching how ``send_*`` / ``session_*`` are
    treated (all-or-nothing, not argument-conditional)."""
    _as_session("channel:C123:reviewer")

    result = _call_tool_inner(
        "cron_add", {"name": f"self-{uuid.uuid4().hex[:8]}", "message": "remind", "every": 60}
    )

    assert "not available to channel agents" in result


def test_slack_human_session_may_still_schedule_cron() -> None:
    """Only the ``channel:`` orchestrator-agent namespace is confined.

    A ``slack:``/``discord:`` session is an allow-listed HUMAN participant, not a
    contained agent, so its own recurring scheduling is the legitimate flow the
    fix must not break. This is what keeps the denial from being the over-broad
    'restrict every caller to its own agent' model that would break scheduling
    for a different crew."""
    _as_session("slack:1785370133.085469")

    result = _call_tool_inner(
        "cron_add", {"name": f"human-{uuid.uuid4().hex[:8]}", "message": "go", "every": 120}
    )

    assert "Added job" in result
    assert "not available to channel agents" not in result


def test_dashboard_session_may_schedule_for_another_crew() -> None:
    """The dashboard flow the issue is careful to preserve: a chat session
    scheduling a job that runs as a DIFFERENT crew is allowed -- the confinement
    keys on the channel-agent namespace, not on the ``agent`` argument."""
    _as_session("dashboard:owner")

    result = _call_tool_inner(
        "cron_add",
        {
            "name": f"crew-{uuid.uuid4().hex[:8]}",
            "message": "go",
            "agent": "kirocrew-conductor",
            "every": 120,
        },
    )

    assert "Added job" in result


# --- 6: the crew-member scope -----------------------------------------------
#
# A member is one principal across every key its pinned DM thread presents. The
# gate matches the member the CALLER's key names against the member the ROW's own
# session key names -- both read off key structure (_authz_member_id). It must
# never read job.member_id for this: cron_add takes that field from the CALLER
# ("Crew Member responsible for this schedule... Omit to inherit"), and
# bind_cron_memory persists the choice beside the creator's owning session, so it
# records who a job RUNS AS -- a label any caller may fill -- and not who owns it.


def _bind_member(job_id: str, member: str) -> None:
    """Record on the stored row the execution member ``cron_add`` can be told.

    ``cron_add`` accepts a caller-chosen ``member_id`` and ``bind_cron_memory``
    persists exactly that choice on the row next to the creator's own session.
    Seeding the field this way -- from OUTSIDE the creating session -- is the
    attack's own shape, which is the point: the gate must reach the same verdict
    with the field filled as with it empty, because a caller-chosen field decides
    nothing. These tests stay on the ownership contract rather than re-standing-up
    the member config the full capture path resolves.
    """
    svc = CronService(base_dir=mcp_cron.config_dir())
    job = svc.get_job(job_id)
    assert job is not None
    job.member_id = member
    svc._save()


def test_a_member_reaches_its_own_job_from_a_later_session() -> None:
    """The member is the principal; the creating session was only one of its keys.

    A member's pinned DM thread outlives any one session: the thread's slot key
    travels under more than one layer prefix, and the session a resumed turn
    presents is not the string the creating turn presented. The row's own session
    key and the caller's key name the same member, so the gate accepts the
    member's later session for reads and for every mutating verb alike -- with
    the stored ``member_id`` whatever the creating call left on it.
    """
    _as_session("dashboard:member-alice")
    name = f"alice-{uuid.uuid4().hex[:8]}"
    job_id = _add_job(name)
    _bind_member(job_id, "alice")

    # A later session of the same member, spelled at the other layer prefix.
    _as_session("dashboard_member-alice")
    assert name in _call_tool_inner("cron_list", {})
    updated = _call_tool_inner("cron_update", {"job_id": job_id, "message": "sweep again"})
    assert "Updated job" in updated, updated
    assert "job not found" not in updated.lower()
    removed = _call_tool_inner("cron_remove", {"job_id": job_id})
    assert f"Removed job: {job_id}" in removed, removed


def test_another_member_stays_outside_the_scope() -> None:
    """The arm widens nothing across members, and the refusal stays the shared one.

    Bob learns nothing about Alice's row: it is withheld from the list, and the
    mutation answers the exact string an absent id gets, so the gate keeps its
    anti-enumeration shape.
    """
    _as_session("dashboard:member-alice")
    name = f"alice-{uuid.uuid4().hex[:8]}"
    job_id = _add_job(name)
    _bind_member(job_id, "alice")

    _as_session("dashboard:member-bob")
    assert name not in _call_tool_inner("cron_list", {})
    for tool in _MUTATING_TOOLS:
        hers = _call_tool_inner(tool, {"job_id": job_id, "message": "x"})
        absent = _call_tool_inner(tool, {"job_id": "deadbeef", "message": "x"})
        assert hers.replace(job_id, "X") == absent.replace("deadbeef", "X"), (tool, hers)


def test_a_different_store_of_the_same_member_stays_out_of_scope() -> None:
    """The member arm matches SLOTS, not slugs: a store suffix is part of who owns a row.

    A member's DM slot key carries an optional ``.memory-<store>`` suffix that
    is part of the slot's identity (``read_dm_binding_for_slot`` keeps memory
    store generations apart on purpose). The arm may span the slot's two layer
    prefixes -- ``dashboard:member-alice`` and ``dashboard_member-alice`` are the
    same slot -- but not the slot's store generations: a job created in one
    store stays out of the member's other stores, both directions, for reads,
    every mutating verb and the bulk sweep.
    """
    _as_session("dashboard:member-alice.memory-alpha")
    name = f"alice-alpha-{uuid.uuid4().hex[:8]}"
    job_id = _add_job(name)
    _bind_member(job_id, "alice")

    _as_session("dashboard_member-alice.memory-beta")
    assert name not in _call_tool_inner("cron_list", {})
    _call_tool_inner("cron_remove_all", {})
    assert (
        CronService(base_dir=mcp_cron.config_dir()).get_job(job_id) is not None
    ), "cron_remove_all swept a row owned by a different store of the same member"
    for tool in _MUTATING_TOOLS:
        other = _call_tool_inner(tool, {"job_id": job_id, "message": "x"})
        absent = _call_tool_inner(tool, {"job_id": "deadbeef", "message": "x"})
        assert other.replace(job_id, "X") == absent.replace("deadbeef", "X"), (tool, other)
    # The storeless spelling is yet another slot: it gains nothing either.
    _as_session("dashboard:member-alice")
    assert name not in _call_tool_inner("cron_list", {})
    # And the reverse direction: the storeless creator's row stays out of the store.
    _as_session("dashboard:member-alice")
    plain = f"alice-plain-{uuid.uuid4().hex[:8]}"
    plain_id = _add_job(plain)
    _as_session("dashboard:member-alice.memory-alpha")
    assert plain not in _call_tool_inner("cron_list", {})
    plain_row = CronService(base_dir=mcp_cron.config_dir()).get_job(plain_id)
    assert plain_row is not None and plain_row.enabled is True


def test_a_key_that_names_no_member_gains_nothing() -> None:
    """The member arm keys on the caller's own member, derived from its key.

    An ordinary chat slot names no member, so it reads no member-bound row and
    touches none -- the widening is a principal match, not a relaxation for
    whoever happens to be calling.
    """
    _as_session("dashboard:member-alice")
    name = f"alice-{uuid.uuid4().hex[:8]}"
    job_id = _add_job(name)
    _bind_member(job_id, "alice")

    _as_session("dashboard:chat-4242")
    assert name not in _call_tool_inner("cron_list", {})
    assert "job not found" in _call_tool_inner("cron_remove", {"job_id": job_id}).lower()


def test_the_row_reaches_the_member_on_its_own_key_not_the_labelled_field() -> None:
    """The widening's positive half: the ROW's key is what names the member.

    A member-created row reaches the member's later session through the member
    its OWN session key names -- the stored ``member_id`` is not consulted (and
    could not be: the caller chooses it). A row that records no execution member
    at all still belongs to the member whose session created it.
    """
    _as_session("dashboard:member-alice")
    name = f"alice-{uuid.uuid4().hex[:8]}"
    job_id = _add_job(name)

    _as_session("dashboard_member-alice")
    assert name in _call_tool_inner("cron_list", {})
    updated = _call_tool_inner("cron_update", {"job_id": job_id, "message": "sweep again"})
    assert "Updated job" in updated, updated


@pytest.mark.parametrize(
    "creator_key",
    [
        "dashboard:chat-4242",  # an ordinary chat session names no member
        "dashboard:member-bob",  # another member's verified DM session
        "dashboard_member-bob.memory-alpha",  # the other prefix, with a store suffix
    ],
)
def test_a_row_created_elsewhere_stays_out_of_the_member_it_names(creator_key: str) -> None:
    """The gate's own negative: a self-filled ``member_id`` authorizes nothing.

    ``cron_add`` lets the CALLER pick the row's ``member_id`` and
    ``bind_cron_memory`` persists that pick beside the creator's own session, so
    the field says who a job RUNS AS -- a label the caller may fill with any
    member's id. Ownership must therefore come from the row's own session key:
    neither a chat session's row nor Bob's row becomes Alice's because either was
    labelled with her id. Read, every mutating verb and the bulk sweep all stay
    out, and the per-row refusals keep the shared anti-enumeration wording.
    """
    _as_session(creator_key)
    name = f"labelled-{uuid.uuid4().hex[:8]}"
    job_id = _add_job(name)
    _bind_member(job_id, "alice")

    _as_session("dashboard:member-alice")
    assert name not in _call_tool_inner("cron_list", {})
    # The bulk sweep is a write through the one scope: it must not take the row.
    _call_tool_inner("cron_remove_all", {})
    assert (
        CronService(base_dir=mcp_cron.config_dir()).get_job(job_id) is not None
    ), "cron_remove_all removed a row labelled with the caller's member id"
    for tool in _MUTATING_TOOLS:
        hers = _call_tool_inner(tool, {"job_id": job_id, "message": "x"})
        absent = _call_tool_inner(tool, {"job_id": "deadbeef", "message": "x"})
        assert hers.replace(job_id, "X") == absent.replace("deadbeef", "X"), (tool, hers)
    after = CronService(base_dir=mcp_cron.config_dir()).get_job(job_id)
    assert after is not None and after.enabled is True


def test_an_ownerless_row_stays_out_of_scope_even_when_member_bound() -> None:
    """The admin surface's rows stay outside every session's scope, both ways.

    ``kirocrew cron add`` and the importer write rows with no owning session, and
    such a row may still name a member it runs as. The member arm requires a
    session owner to widen through: a row outside every session's scope is a
    permanent rule, and the CLI and the Schedule page remain its surface.
    """
    svc = CronService(base_dir=mcp_cron.config_dir())
    name = f"cli-made-{uuid.uuid4().hex[:8]}"
    unowned = svc.add_job(name=name, message="go", every_secs=120, session_key="")
    _bind_member(unowned.id, "alice")

    _as_session("dashboard:member-alice")
    assert name not in _call_tool_inner("cron_list", {})
    for tool in _MUTATING_TOOLS:
        result = _call_tool_inner(tool, {"job_id": unowned.id})
        assert "job not found" in result.lower(), (tool, result)
    after = CronService(base_dir=mcp_cron.config_dir()).get_job(unowned.id)
    assert after is not None and after.enabled is True


def test_an_unidentifiable_caller_still_reaches_nothing() -> None:
    """No vouched identity, no member arm: the empty scope covers bound rows too.

    A caller the gateway cannot name may share a pooled backend with identified
    sessions; a member binding on a row says nothing about an unidentified
    caller, so the answer stays "nothing at all".
    """
    _as_session("dashboard:member-alice")
    bound = f"bound-{uuid.uuid4().hex[:8]}"
    bound_id = _add_job(bound)
    _bind_member(bound_id, "alice")
    plain = f"plain-{uuid.uuid4().hex[:8]}"
    _add_job(plain)

    set_current_caller(None)
    listed = _call_tool_inner("cron_list", {})
    assert bound not in listed
    assert plain not in listed
    assert "cannot determine which session is calling" in _call_tool_inner("cron_remove_all", {})


def test_cron_remove_all_sweeps_the_member_rows_from_any_of_its_sessions() -> None:
    """The batch read goes through the same one scope function.

    From the member's later session, ``cron_remove_all`` removes the rows the
    member owns -- the bound one and the one this session created -- and leaves
    another member's row alone.
    """
    _as_session("dashboard:member-alice")
    older = f"alice-older-{uuid.uuid4().hex[:8]}"
    older_id = _add_job(older)
    _bind_member(older_id, "alice")
    _as_session("dashboard_member-alice")
    newer = f"alice-newer-{uuid.uuid4().hex[:8]}"
    _add_job(newer)
    _as_session("dashboard:member-bob")
    bobs = f"bob-{uuid.uuid4().hex[:8]}"
    bobs_id = _add_job(bobs)
    _bind_member(bobs_id, "bob")

    _as_session("dashboard_member-alice")
    result = _call_tool_inner("cron_remove_all", {})
    assert "Removed" in result, result
    survived = {
        j.name for j in CronService(base_dir=mcp_cron.config_dir()).list_jobs(include_disabled=True)
    }
    assert survived == {bobs}, survived
