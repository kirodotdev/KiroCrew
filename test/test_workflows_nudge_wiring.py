"""ctx.nudge → AutoNudge wiring (regression for
``RuntimeError('ctx.nudge is not available for this run (no nudge port wired)')``).

The authoring prompt and validator both advertise ``ctx.nudge`` as a legal
primitive, but ``WorkflowService`` historically built its ``WorkflowRunner``
without wiring the ``nudge`` port, so any authored script that called it crashed
at runtime. These tests pin the fix AND its security follow-up:

  * ``WorkflowService`` wires a ``nudge`` port that maps the run's ORIGINATING
    session key (``binding_key_for``) and routes through a gateway-injected
    ``nudge_authorizer`` — never arming AutoNudge directly.
  * A non-nudge-able session, an unwired authorizer, or an arm failure degrades
    to a logged no-op — it never crashes the run.
  * ``session_key`` is threaded end-to-end from ``start()`` into ``ctx.nudge``.
  * ``authorize_and_add_nudge`` is the shared chokepoint used by BOTH the REST
    handler and the workflow bridge: it enforces dashboard-slot existence,
    Discord allowlist + current-session match (deny-by-default), and the
    message-length limit BEFORE calling ``svc.add`` — so a caller-influenced
    session key can't spoof another session's loop.

All against fakes — no real AutoNudge service, model, or kiro-cli.
"""

from __future__ import annotations

import asyncio
import re
from types import SimpleNamespace

import pytest

import kiro_crew.autonudge_authz as autonudge_authz
from kiro_crew.autonudge import binding_key_for
from kiro_crew.autonudge_authz import authorize_and_add_nudge
from kiro_crew.workflows.service import WorkflowService

pytestmark = pytest.mark.asyncio


class FakeSessions:
    """Minimal SessionManager stand-in; the nudge scripts never call ctx.agent."""

    async def get_or_create(self, key, **kw):  # pragma: no cover - not exercised
        raise AssertionError("nudge workflow should not create agent sessions")

    def release(self, key, *, cleanup=False):
        pass


def _svc(authorizer=None) -> WorkflowService:
    # persist=False → no filesystem store; pool_agents=False → deterministic path.
    return WorkflowService(
        sessions=FakeSessions(), persist=False, pool_agents=False, nudge_authorizer=authorizer
    )


async def _wait_terminal(svc: WorkflowService, run_id: str, timeout: float = 3.0) -> dict:
    t = 0.0
    while t < timeout:
        snap = svc.status(run_id)
        if snap and snap["status"] != "running":
            return snap
        await asyncio.sleep(0.02)
        t += 0.02
    raise AssertionError("run did not finish")


# --------------------------------------------------------------------------- #
# binding_key_for — single source of truth
# --------------------------------------------------------------------------- #


async def test_binding_key_for_mapping() -> None:
    assert binding_key_for("dashboard:chat-1-9") == "chat-1-9"
    assert binding_key_for("slack:1720000000.1234") == "slack:1720000000.1234"
    assert binding_key_for("discord:agent:direct:42") == "discord:agent:direct:42"
    assert binding_key_for("cron:daily") is None
    assert binding_key_for("subagent:abc") is None
    assert binding_key_for("hook:xyz") is None
    assert binding_key_for("") is None


# --------------------------------------------------------------------------- #
# _nudge_port — the service bridge (delegates to the injected authorizer)
# --------------------------------------------------------------------------- #


async def test_nudge_port_maps_key_and_calls_authorizer() -> None:
    calls = []

    async def authorizer(*, slot_key, message, idle_secs, max_cycles):
        calls.append((slot_key, message, idle_secs, max_cycles))

    _svc(authorizer)._nudge_port(
        run_id="wf_t", session_key="dashboard:chat-2-7", idle_secs=90, message="poke", max_cycles=3
    )
    await asyncio.sleep(0)  # let the scheduled arm task run
    # dashboard:chat-2-7 → bare slot key chat-2-7 (never the raw namespaced key).
    assert calls == [("chat-2-7", "poke", 90, 3)]


async def test_nudge_port_noop_when_session_not_nudgeable() -> None:
    calls = []

    async def authorizer(*, slot_key, message, idle_secs, max_cycles):
        calls.append(slot_key)

    _svc(authorizer)._nudge_port(run_id="wf_t", session_key="cron:job", idle_secs=90, message="poke")
    await asyncio.sleep(0)
    assert calls == []  # not nudge-able → authorizer never invoked, no crash


async def test_nudge_port_noop_when_no_authorizer() -> None:
    # No authorizer wired (e.g. non-dashboard host) → logged no-op, no crash.
    _svc(None)._nudge_port(run_id="wf_t", session_key="dashboard:chat-1-1", idle_secs=90, message="poke")
    await asyncio.sleep(0)


async def test_nudge_port_swallows_authorizer_failure() -> None:
    async def boom(*, slot_key, message, idle_secs, max_cycles):
        raise RuntimeError("authz exploded")

    # Arm failure must not propagate out of the fire-and-forget task.
    _svc(boom)._nudge_port(run_id="wf_t", session_key="dashboard:chat-1-1", idle_secs=90, message="poke")
    await asyncio.sleep(0)


# --------------------------------------------------------------------------- #
# end-to-end: a workflow calling ctx.nudge routes through the authorizer
# --------------------------------------------------------------------------- #

NUDGE_SCRIPT = (
    'META = {"name": "mon"}\n'
    "async def workflow(ctx):\n"
    "    ctx.nudge(idle_secs=45, message='watch pr')\n"
    "    return 'ok'\n"
)


async def test_start_wires_nudge_end_to_end() -> None:
    calls = []

    async def authorizer(*, slot_key, message, idle_secs, max_cycles):
        calls.append((slot_key, message, idle_secs, max_cycles))
        return None  # success

    svc = _svc(authorizer)
    out = await svc.start(NUDGE_SCRIPT, session_key="dashboard:chat-9-1")
    snap = await _wait_terminal(svc, out["run_id"])
    assert snap["status"] == "finished", snap
    await asyncio.sleep(0)  # drain the scheduled arm task
    assert calls == [("chat-9-1", "watch pr", 45, 0)]
    # VISIBILITY: the armed outcome surfaces in the run's event stream.
    result = svc.result(out["run_id"]) or {}
    logs = [
        (e.get("data") or {}).get("message", "")
        for e in result.get("events", [])
        if e.get("type") == "log"
    ]
    assert any("ctx.nudge armed" in m for m in logs), logs


async def test_nudge_skip_is_visible_in_run_events() -> None:
    """Arbiter escalation: a skipped/denied nudge must be run-visible — the user
    must never believe a monitor is armed when nothing will fire."""

    async def authorizer(**kw):  # pragma: no cover - never reached (skip is earlier)
        return None

    svc = _svc(authorizer)
    # cron: origin is not nudge-able → the arm is skipped, run still succeeds.
    out = await svc.start(NUDGE_SCRIPT, session_key="cron:job-1")
    snap = await _wait_terminal(svc, out["run_id"])
    assert snap["status"] == "finished", snap
    result = svc.result(out["run_id"]) or {}
    logs = [
        (e.get("data") or {}).get("message", "")
        for e in result.get("events", [])
        if e.get("type") == "log"
    ]
    assert any("ctx.nudge NOT armed" in m for m in logs), logs


async def test_nudge_denial_is_visible_in_run_events() -> None:
    """An authorizer rejection (e.g. unknown slot) surfaces its reason in the
    run event stream, not only the server log."""

    async def denying_authorizer(**kw):
        return "unknown slot chat-9-1"

    svc = _svc(denying_authorizer)
    out = await svc.start(NUDGE_SCRIPT, session_key="dashboard:chat-9-1")
    snap = await _wait_terminal(svc, out["run_id"])
    assert snap["status"] == "finished", snap
    await asyncio.sleep(0)  # drain the arm task
    result = svc.result(out["run_id"]) or {}
    logs = [
        (e.get("data") or {}).get("message", "")
        for e in result.get("events", [])
        if e.get("type") == "log"
    ]
    assert any("ctx.nudge NOT armed: unknown slot chat-9-1" in m for m in logs), logs


async def test_slow_nudge_arm_drained_before_terminal() -> None:
    """The run's teardown drains in-flight ctx.nudge arms BEFORE the terminal
    transition: a slow authorizer's outcome must already be in the run record
    the moment the run reports terminal — no arm outlives the run unsupervised,
    and outcome logs can't land after terminal persistence."""

    async def slow_authorizer(**kw):
        await asyncio.sleep(0.3)  # slower than script execution
        return None

    svc = _svc(slow_authorizer)
    out = await svc.start(NUDGE_SCRIPT, session_key="dashboard:chat-9-1")
    snap = await _wait_terminal(svc, out["run_id"])
    assert snap["status"] == "finished", snap
    # Read IMMEDIATELY at terminal — no grace sleep. Without the per-run drain
    # the armed log would land ~0.3s after terminal and this would fail.
    result = svc.result(out["run_id"]) or {}
    events = result.get("events", [])
    logs = [
        (e.get("data") or {}).get("message", "")
        for e in events
        if e.get("type") == "log"
    ]
    assert any("ctx.nudge armed" in m for m in logs), logs
    # EVENT-STREAM CONTRACT: terminal events are last — the nudge outcome log
    # must appear BEFORE run_finished in the stream, never after it.
    types_and_msgs = [
        (e.get("type"), (e.get("data") or {}).get("message", "")) for e in events
    ]
    armed_idx = next(
        i for i, (t, m) in enumerate(types_and_msgs) if t == "log" and "ctx.nudge armed" in m
    )
    terminal_idx = next(
        i for i, (t, _m) in enumerate(types_and_msgs) if t in ("run_finished", "run_failed")
    )
    assert armed_idx < terminal_idx, types_and_msgs
    # The run's task bucket is emptied by the drain.
    assert out["run_id"] not in svc._nudge_tasks


# --------------------------------------------------------------------------- #
# AutoNudge add() cancellation safety (offloaded persistence)
# --------------------------------------------------------------------------- #


async def test_autonudge_add_survives_caller_cancellation(tmp_path, monkeypatch) -> None:
    """Cancelling an ``add()`` caller mid-write must NOT release the service
    lock while the executor write is in flight (a newer write could land first
    and then be clobbered by the stale snapshot). The mutate+persist runs
    shielded: the caller sees CancelledError, but the arm+persist completes."""
    import threading

    from kiro_crew.autonudge import AutoNudgeService

    svc = AutoNudgeService(base_dir=tmp_path)
    write_started = threading.Event()
    release_write = threading.Event()
    orig_write = svc._write_state

    def slow_write(payload):
        write_started.set()
        release_write.wait(timeout=5)
        orig_write(payload)

    monkeypatch.setattr(svc, "_write_state", slow_write)
    task = asyncio.ensure_future(svc.add("chat-cancel-1", "watch"))
    while not write_started.is_set():
        await asyncio.sleep(0.01)
    task.cancel()
    release_write.set()
    with pytest.raises(asyncio.CancelledError):
        await task
    # The shielded inner task completes: the loop is registered AND persisted.
    # Poll for the FILE — the in-memory mutation lands before the offloaded
    # write, so the file is the true completion signal.
    for _ in range(150):
        if (tmp_path / "autonudge.json").exists():
            break
        await asyncio.sleep(0.02)
    assert (tmp_path / "autonudge.json").exists(), (
        "shielded add did not persist after caller cancellation"
    )
    loop = svc.get_by_slot("chat-cancel-1")
    assert loop is not None
    svc.remove_sync(loop.id)  # cleanup: cancel the armed timer


# --------------------------------------------------------------------------- #
# authorize_and_add_nudge — shared authorization chokepoint (security)
# --------------------------------------------------------------------------- #


class FakeNudgeSvc:
    def __init__(self) -> None:
        self.added: list[tuple] = []

    async def add(
        self,
        *,
        slot_key,
        message,
        idle_secs=60,
        max_cycles=0,
        stop_sentinel_path="",
        max_runtime_secs=0,
        admission_check=None,
        gate=True,
    ):
        if admission_check is not None and not admission_check():
            raise AssertionError("test admission unexpectedly changed")
        self.added.append((slot_key, message, idle_secs, max_cycles))
        return SimpleNamespace(id="loop1", slot_key=slot_key, idle_secs=idle_secs, max_cycles=max_cycles)


class FakeDiscordDispatcher:
    def __init__(self, *, authorized: set[str], current: dict[str, str]) -> None:
        self._authorized = authorized
        self._current = current

    def is_authorized(self, user_id: str) -> bool:
        return user_id in self._authorized

    def current_session_key(self, user_id: str) -> str:
        return self._current.get(user_id, "")


def _state(*, slots=None, discord=None):
    transports = {}
    if discord is not None:
        transports["discord"] = SimpleNamespace(dispatcher=discord)
    return SimpleNamespace(_slots=slots or {}, sessions=None, channel_transports=transports)


async def test_authz_rejects_unknown_dashboard_slot() -> None:
    svc = FakeNudgeSvc()
    loop, error, status = await authorize_and_add_nudge(
        svc=svc, state=_state(slots={}), slot_key="chat-404", message="x", source="workflow"
    )
    assert loop is None and status == 404 and "unknown slot" in error
    assert svc.added == []  # never armed


async def test_authz_rejects_message_too_long() -> None:
    svc = FakeNudgeSvc()
    loop, error, status = await authorize_and_add_nudge(
        svc=svc,
        state=_state(slots={"chat-1-1": SimpleNamespace(workspace="default")}),
        slot_key="chat-1-1",
        message="x" * 8001,
        source="workflow",
    )
    assert loop is None and status == 400 and "too long" in error
    assert svc.added == []


async def test_authz_rejects_spoofed_discord_session() -> None:
    svc = FakeNudgeSvc()
    # User IS allowlisted, but the requested key is NOT their current session →
    # deny-by-default rejects the spoof (the core cross-session-injection guard).
    disp = FakeDiscordDispatcher(
        authorized={"42"}, current={"42": "discord:kirocrew:direct:42"}
    )
    loop, error, status = await authorize_and_add_nudge(
        svc=svc,
        state=_state(discord=disp),
        slot_key="discord:kirocrew:direct:99",  # someone else's user id
        message="pwn",
        source="workflow",
    )
    assert loop is None and status in (403, 404)
    assert svc.added == []


async def test_authz_rejects_unallowlisted_discord_user() -> None:
    svc = FakeNudgeSvc()
    disp = FakeDiscordDispatcher(authorized=set(), current={})
    loop, error, status = await authorize_and_add_nudge(
        svc=svc,
        state=_state(discord=disp),
        slot_key="discord:kirocrew:direct:42",
        message="pwn",
        source="workflow",
    )
    assert loop is None and status == 403 and "allowlist" in error
    assert svc.added == []


async def test_authz_arms_valid_dashboard_slot_and_audits(monkeypatch) -> None:
    audits = []
    monkeypatch.setattr(
        autonudge_authz,
        "sel",
        lambda: SimpleNamespace(log_tool_invocation=lambda **kw: audits.append(kw)),
    )
    svc = FakeNudgeSvc()
    loop, error, status = await authorize_and_add_nudge(
        svc=svc,
        state=_state(slots={"chat-1-1": SimpleNamespace(workspace="default")}),
        slot_key="chat-1-1",
        message="watch",
        idle_secs=60,
        max_cycles=0,
        source="workflow",
    )
    assert error is None and status == 200 and loop is not None
    assert svc.added and svc.added[0][0] == "chat-1-1"
    # AUDIT-OR-DENY ordering: a CRITICAL 'invoked' event precedes the arm, then
    # a best-effort 'success' terminal event — both tagged with the source.
    assert [a["outcome"] for a in audits] == ["invoked", "success"]
    assert audits[0]["critical"] is True
    assert all(a["source"] == "workflow" for a in audits)


async def test_authz_denies_arm_when_audit_unavailable(monkeypatch) -> None:
    """AUDIT-OR-DENY: if the critical 'invoked' SEL write fails, the loop must
    NOT be armed (fail closed) — an active loop may never exist unaudited."""

    def _raising(**kw):
        if kw.get("critical"):
            raise OSError("SEL disk full")
        raise AssertionError("only the critical invoked event should fire")

    monkeypatch.setattr(
        autonudge_authz,
        "sel",
        lambda: SimpleNamespace(log_tool_invocation=_raising),
    )
    svc = FakeNudgeSvc()
    loop, error, status = await authorize_and_add_nudge(
        svc=svc,
        state=_state(slots={"chat-1-1": SimpleNamespace(workspace="default")}),
        slot_key="chat-1-1",
        message="watch",
        source="workflow",
    )
    assert loop is None and status == 503 and "audit" in error
    assert svc.added == []  # fail closed: no audit ⇒ no loop


async def test_authz_success_audit_failure_keeps_loop(monkeypatch) -> None:
    """The terminal 'success' event is best-effort: the armed loop is already
    covered by the critical 'invoked' record, so a failing success write must
    not raise or roll back the arm."""
    calls = []

    def _flaky(**kw):
        calls.append(kw)
        if kw.get("outcome") == "success":
            raise OSError("SEL hiccup")

    monkeypatch.setattr(
        autonudge_authz,
        "sel",
        lambda: SimpleNamespace(log_tool_invocation=_flaky),
    )
    svc = FakeNudgeSvc()
    loop, error, status = await authorize_and_add_nudge(
        svc=svc,
        state=_state(slots={"chat-1-1": SimpleNamespace(workspace="default")}),
        slot_key="chat-1-1",
        message="watch",
        source="workflow",
    )
    assert error is None and status == 200 and loop is not None
    assert svc.added  # armed
    assert [c["outcome"] for c in calls] == ["invoked", "success"]


async def test_authz_audits_denials(monkeypatch) -> None:
    """Every rejection must leave a security audit trail (backend-security-controls):
    a denied nudge attempt emits outcome='denied', not silence."""
    audits = []
    monkeypatch.setattr(
        autonudge_authz,
        "sel",
        lambda: SimpleNamespace(log_tool_invocation=lambda **kw: audits.append(kw)),
    )
    svc = FakeNudgeSvc()
    loop, error, status = await authorize_and_add_nudge(
        svc=svc, state=_state(slots={}), slot_key="chat-404", message="x", source="workflow"
    )
    assert loop is None and status == 404
    assert svc.added == []
    assert audits and audits[0]["outcome"] == "denied" and audits[0]["source"] == "workflow"


async def test_authz_redacts_llm_influenced_message(monkeypatch) -> None:
    """The nudge message is LLM-influenced, persisted, and re-delivered to chat/
    Slack on every fire — credentials and exfiltration URLs must be redacted at
    the chokepoint BEFORE the loop is armed/persisted (backend-security-controls)."""
    monkeypatch.setattr(
        autonudge_authz,
        "sel",
        lambda: SimpleNamespace(log_tool_invocation=lambda **kw: None),
    )
    svc = FakeNudgeSvc()
    secret = "check AKIA" + "IOSFODNN7EXAMPLE and report"  # AWS access key id shape
    loop, error, status = await authorize_and_add_nudge(
        svc=svc,
        state=_state(slots={"chat-1-1": SimpleNamespace(workspace="default")}),
        slot_key="chat-1-1",
        message=secret,
        source="workflow",
    )
    assert error is None and status == 200 and loop is not None
    stored_message = svc.added[0][1]
    assert "AKIAIOSFODNN7EXAMPLE" not in stored_message, stored_message


# --------------------------------------------------------------------------- #
# authoring-prompt ↔ wired-ports parity (closes the advertised-but-unwired class)
# --------------------------------------------------------------------------- #

# ctx members implemented directly on _RunContext with no host port required.
_CORE_CTX = {"agent", "parallel", "pipeline", "phase", "log", "budget", "args", "now"}

_CTX_TOKEN_RE = re.compile(r"ctx\.([a-zA-Z_]+)")


async def test_author_prompt_advertises_only_wired_primitives() -> None:
    """PARITY CONTRACT: every ctx primitive named in ``_AUTHOR_SYSTEM`` must be
    either core (implemented on _RunContext without a port) or wired to a port by
    the production ``WorkflowService._runner``.

    The authoring prompt is the contract the model writes scripts against; the
    ``ctx.nudge`` crash this PR fixes existed precisely because the prompt
    advertised a primitive production never wired. Any port re-added to the
    prompt (ctx.approve / ctx.send_slack / ctx.send_message / ctx.workflow /
    ctx.cron / ctx.memory / ctx.learn ...) MUST be wired in ``_runner`` first,
    or this test fails — the drift can't silently reappear.
    """
    from kiro_crew.workflows.service import _AUTHOR_SYSTEM

    advertised = set(_CTX_TOKEN_RE.findall(_AUTHOR_SYSTEM))
    assert advertised, "coherence check: the authoring prompt names ctx primitives"

    async def authorizer(**kw):  # pragma: no cover - never fired here
        pass

    runner = _svc(authorizer)._runner("wf_parity")
    wired = {name for name, fn in runner._ports.items() if fn is not None}
    unwired = advertised - _CORE_CTX - wired
    assert not unwired, (
        f"authoring prompt advertises ctx primitives with no wired production port: "
        f"{sorted(unwired)} — wire them in WorkflowService._runner (ports=...) or "
        f"remove them from _AUTHOR_SYSTEM"
    )


UNWIRED_SCRIPT = (
    'META = {"name": "u"}\n'
    "async def workflow(ctx):\n"
    "    yes = await ctx.approve('go?')\n"
    "    return yes\n"
)


async def test_unwired_primitive_rejected_at_validation_in_production_shape() -> None:
    """ENFORCEMENT LAYER: a hand-written/rerun script referencing a primitive the
    production host does not wire (e.g. ``ctx.approve``) must fail at the
    validation boundary with a clear error — never start executing and die
    mid-run with ``RuntimeError("... no ... port wired")``."""

    async def authorizer(**kw):  # pragma: no cover - never fired here
        pass

    svc = _svc(authorizer)
    out = await svc.start(UNWIRED_SCRIPT)
    snap = await _wait_terminal(svc, out["run_id"])
    assert snap["status"] == "failed", snap
    err = snap.get("error", "")
    assert "ctx.approve is not available in this runtime" in err
    # Failed BEFORE exec: the run produced no agent/phase activity.
    result = svc.result(out["run_id"]) or {}
    types = [e.get("type") for e in result.get("events", [])]
    assert "run_failed" in types and "agent_started" not in types


SHADOWED_CTX_SCRIPT = (
    'META = {"name": "shadow"}\n'
    "def read(ctx):\n"
    "    return ctx.get('key', 0)\n"
    "async def workflow(ctx):\n"
    "    ctx.nudge(idle_secs=45, message='watch pr')\n"
    "    return read({'key': 41}) + 1\n"
)


async def test_shadowed_ctx_helper_not_treated_as_workflow_context() -> None:
    """SCOPE-AWARENESS: a helper whose OWN parameter is named ``ctx`` (holding a
    plain dict) must not be rejected by the exec-boundary ctx-surface check —
    only references bound to the workflow entrypoint's context are enforced."""

    async def authorizer(**kw):
        return None

    svc = _svc(authorizer)
    out = await svc.start(SHADOWED_CTX_SCRIPT, session_key="dashboard:chat-9-1")
    snap = await _wait_terminal(svc, out["run_id"])
    assert snap["status"] == "finished", snap
    result = svc.result(out["run_id"]) or {}
    assert result.get("result") == 42


async def test_drain_timeout_reports_undetermined_outcome() -> None:
    """If the teardown drain times out and cancels a pending arm wrapper, the
    run record must carry an explicit 'outcome undetermined' event BEFORE the
    drain returns — never a silent post-terminal arm (the underlying shielded
    add stays supervised by AutoNudgeService)."""
    import threading

    release = threading.Event()  # never set — the authorizer hangs

    async def hung_authorizer(**kw):
        while not release.is_set():
            await asyncio.sleep(0.01)
        return None  # pragma: no cover

    svc = _svc(hung_authorizer)
    notes: list[str] = []
    svc._nudge_port(
        run_id="wf_hang",
        session_key="dashboard:chat-1-1",
        idle_secs=60,
        message="watch",
        notify=notes.append,
    )
    await asyncio.sleep(0.02)  # let the arm start and block
    await svc._drain_nudge_tasks("wf_hang", timeout=0.05)
    release.set()
    assert any("ctx.nudge outcome undetermined" in n for n in notes), notes
    assert "wf_hang" not in svc._nudge_tasks
