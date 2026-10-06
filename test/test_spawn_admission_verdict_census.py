"""The complete set of verdicts a subagent spawn can get at defaults, pinned.

Every release so far has added a gate somewhere between a spawn request and its
start -- a posture tier on top of the floor, a refusal for a row the store could
not hold -- and each one surfaced as "subagents stopped starting" on somebody's
laptop before anyone noticed it was new. So the set is pinned the way
``test_denied_commands_security.py`` pins its rules: a gate added, removed or
relabelled turns this file red, and the change has to say so here.

Two halves:

* the STATIC census reads the gate's own source: every SEL ``outcome`` the
  spawn gate can write, and every wait label it can put on a queued row;
* the BEHAVIOURAL census drives the real ``SubagentManager.spawn_async`` through
  each condition at the shipped defaults and records what came back.

The rule both enforce (critique B1): a CAPACITY verdict -- memory, the macOS
kernel memory-pressure hold, a full cap, a paused cap, the stagger -- always
queues; only the policy gates (memory identity, cwd, governance, the parent
spec's allowlist) and a store that cannot hold the row refuse. The one capacity
terminal is the pressure hold's bound: a start it kept past
``_PRESSURE_HOLD_MAX_WAIT_SECS`` is ended "never started", after its caller was
told it was queued, never as the answer to its submission.
"""

from __future__ import annotations

import ast
import asyncio
import pathlib
import threading
import time
from unittest.mock import MagicMock, patch

import pytest
from overload_fakes import STORE_OPEN_CEILING_SECS, mock_ctx, mock_sessions, wait_taskq_open

import kiro_crew.subagent as subagent_mod
import kiro_crew.subagent_wait_reasons as wait_reasons
from kiro_crew import platform_compat
from kiro_crew.subagent import SubagentManager

pytestmark = pytest.mark.usefixtures("healthy_host_memory")

_ADMISSION = pathlib.Path(subagent_mod.__file__).parent / "subagent_manager" / "admission"
_PARENT = "dashboard:census"
#: The one ceiling for every wait here (a verdict landing, the teardown drain).
#: A lost-run guard, never the barrier. It is the shared store-open ceiling: a
#: hosted Windows runner stalls every xdist worker for several seconds at a
#: time, and a shorter private ceiling turns that stall into a failure of
#: whichever wait it lands in. It stays under the tests' ``timeout(60)``.
_WAIT_SECS = STORE_OPEN_CEILING_SECS

#: Every SEL outcome ``spawn_impl`` can write, by what it means. A new entry is a
#: new verdict: say which kind it is, and if it is a capacity one it must queue.
_POLICY_REFUSALS = frozenset(
    {
        "rejected_empty_task",
        "rejected_invalid_cwd",
        "denied",  # governance, and the parent spec's availableAgents
        "rejected",  # the spawn-approval prompt said no, or had no surface
        "rejected_spawn",
    }
)
_STORE_REFUSALS = frozenset({"refused_task_store"})
_CAPACITY_OUTCOMES = frozenset({"deferred_low_memory"})
_DIAGNOSTICS = frozenset({"memory_check_unavailable", "auto_approved_spawn"})

#: Every wait label a queued spawn can carry, and which of them are deferrals.
#: The posture tier (``posture_critical``) is deliberately absent: spawns admit
#: on the floor alone, and only cron still defers on posture. So is a paused
#: execution cap (``adaptive_cap_zero``): the adaptive controller reads no memory
#: or loop lag and never takes the cap to 0, so a 0 cap is a capacity wait.
_WAIT_REASONS = frozenset({"concurrency_limit", "low_memory", "memory_pressure"})
_DEFERRALS = frozenset({"low_memory", "memory_pressure"})


def _string_constants(node: ast.AST) -> set[str]:
    """The strings *node* can be: its literals, and a module constant it names
    (``outcome=SEL_...``), resolved through ``kiro_crew.subagent``, the namespace
    the admission package's functions run in."""
    found: set[str] = set()
    for c in ast.walk(node):
        if isinstance(c, ast.Constant) and isinstance(c.value, str):
            found.add(c.value)
        elif isinstance(c, ast.Name) and c.id.isupper():
            value = getattr(subagent_mod, c.id, None)
            if isinstance(value, str):
                found.add(value)
    return found


def _spawn_impl() -> ast.FunctionDef:
    tree = ast.parse((_ADMISSION / "gate.py").read_text(encoding="utf-8"))
    for node in ast.walk(tree):
        if isinstance(node, ast.FunctionDef) and node.name == "spawn_impl":
            return node
    raise AssertionError("spawn_impl is gone from gate.py: re-point this census")


# --- the static census ------------------------------------------------------


def test_every_outcome_the_spawn_gate_can_write_is_accounted_for() -> None:
    outcomes: set[str] = set()
    for node in ast.walk(_spawn_impl()):
        if isinstance(node, ast.Call):
            for keyword in node.keywords:
                if keyword.arg == "outcome":
                    outcomes |= _string_constants(keyword.value)
    assert outcomes == _POLICY_REFUSALS | _STORE_REFUSALS | _CAPACITY_OUTCOMES | _DIAGNOSTICS


#: Every ``spawn_run`` outcome written on the spawn path OUTSIDE ``spawn_impl``,
#: none of them a capacity refusal.
_OUTSIDE_THE_GATE = frozenset(
    {
        "refused_task_store",  # ``spawn_async``: the row could not be written
        "dedicated_start_below_floor",  # the run's top-up waited, then started anyway
        "spawned",  # the pump released a start
        "rejected",  # the approval prompt said no at release
        "submission_lost",  # a wave member that never arrived
        "digest_hold_expired",  # a wave digest released on its deadline
        "deferred_memory_pressure",  # the macOS kernel pressure hold kept a start
        "dedicated_start_under_memory_pressure",  # the top-up waited, then started
        # The pressure hold's bound ended a start it kept, after its caller was
        # told it was queued: the terminal of a wait, not a refusal.
        "never_started_memory_pressure",
    }
)

#: Every refusal text ``spawn_impl`` can return, as its literal shape (``{}`` for
#: an interpolated value, ``<name>`` for a message a helper composed). A refusal
#: that writes no SEL outcome still lands here, so a new one turns this red.
_REFUSAL_TEXTS = frozenset(
    {
        "spawn refused: task must be a non-empty string",
        "spawn refused: gateway admission is closed",
        "memory_unavailable: the parent's memory mode could not be established",
        "memory_unavailable: {}",
        "spawn refused: {}",  # the cwd allowlist, and the parent spec's allowlist
        "spawn refused by governance: {}",
        "spawn denied by governance",
        "<gov_spawn_err>",
        "spawn denied by agent spec",
        "<allowlist_err>",
        "spawn refused: task store unavailable ({})",
        "spawn refused: the task store could not record this start's memory wait ({})",
        "<owner_err>",  # an app spawn of another app's agent
        "<err>",  # an agent name that does not resolve
        "spawn rejected: no approval mechanism configured",
        # The pressure hold's terminal: ended, never started, past its bound. The
        # caller was already told the start was queued (test below).
        "<MEMORY_PRESSURE_NEVER_STARTED>",
        # The floor's terminal for a wait with no durable row, past
        # ``agent.subagent_queue_max_wait_secs``: also the end of a wait the
        # caller was told was queued (test_subagent_queue_max_wait.py).
        "<QUEUED_WAIT_EXPIRED_TEXT>",
    }
)
_CAPACITY_WORDS = ("GB", "memory available", "low memory", "posture", "concurrency", "capacity")


def _text_shapes(node: ast.AST) -> set[str]:
    if isinstance(node, ast.Constant) and isinstance(node.value, str):
        return {node.value}
    if isinstance(node, ast.JoinedStr):
        return {"".join(v.value if isinstance(v, ast.Constant) else "{}" for v in node.values)}
    if isinstance(node, ast.IfExp):
        return _text_shapes(node.body) | _text_shapes(node.orelse)
    if isinstance(node, ast.Name):
        return {f"<{node.id}>"}
    raise AssertionError(f"a refusal text of a new shape: {ast.unparse(node)}")


def test_every_refusal_the_spawn_gate_can_return_is_accounted_for() -> None:
    texts: set[str] = set()
    for node in ast.walk(_spawn_impl()):
        if isinstance(node, ast.Call):
            for keyword in node.keywords:
                if keyword.arg == "error":
                    texts |= _text_shapes(keyword.value)
        elif isinstance(node, ast.Assign):
            for target in node.targets:
                if isinstance(target, ast.Attribute) and target.attr == "error":
                    texts |= _text_shapes(node.value)
    assert texts == _REFUSAL_TEXTS
    for text in texts:
        assert not any(word in text for word in _CAPACITY_WORDS), text


def test_every_outcome_on_the_spawn_path_is_accounted_for() -> None:
    """The gate is not the only writer: the accept, the run's top-up, the pump's
    release and the wave bookkeeping write ``spawn_run`` outcomes too."""
    root = pathlib.Path(subagent_mod.__file__).parent
    outcomes: set[str] = set()
    for path in [*sorted((root / "subagent_manager").rglob("*.py")), root / "subagent.py"]:
        for node in ast.walk(ast.parse(path.read_text(encoding="utf-8"))):
            if not isinstance(node, ast.Call):
                continue
            kw = {k.arg: k.value for k in node.keywords}
            tool = kw.get("tool_name")
            if isinstance(tool, ast.Constant) and tool.value == "spawn_run" and "outcome" in kw:
                outcomes |= _string_constants(kw["outcome"])
    gate = _POLICY_REFUSALS | _STORE_REFUSALS | _CAPACITY_OUTCOMES | _DIAGNOSTICS
    assert outcomes == gate | _OUTSIDE_THE_GATE


def test_no_capacity_outcome_is_a_refusal() -> None:
    """The capacity outcomes are deferrals by name, and none of the memory or
    posture refusals an older gate wrote can come back."""
    assert all(o.startswith("deferred_") for o in _CAPACITY_OUTCOMES)
    gate = (_ADMISSION / "gate.py").read_text(encoding="utf-8")
    for retired in ("refused_low_memory", "refused_memory_critical", "deferred_memory_critical"):
        assert retired not in gate, retired


def test_the_wait_reasons_are_the_complete_set() -> None:
    declared = {
        value
        for name, value in vars(wait_reasons).items()
        if name.startswith("QUEUED_REASON_") and isinstance(value, str)
    }
    assert declared == _WAIT_REASONS
    assert set(wait_reasons.DEFERRED_QUEUED_REASONS) == _DEFERRALS
    assert set(wait_reasons.QUEUED_KIND_TEXT) == _WAIT_REASONS
    # Every label is one the admission package actually puts on a row.
    used: set[str] = set()
    for path in _ADMISSION.glob("*.py"):
        tree = ast.parse(path.read_text(encoding="utf-8"))
        used |= {
            n.id for n in ast.walk(tree) if isinstance(n, ast.Name) and n.id.startswith("QUEUED_")
        }
    assert {getattr(wait_reasons, name) for name in used if name.startswith("QUEUED_REASON_")} == (
        _WAIT_REASONS
    )


def test_the_spawn_gate_does_not_read_the_posture_tier() -> None:
    """Cron keeps its posture gate (``TestCronAdmissionDeferral``); spawns do not."""
    names = {
        n.id if isinstance(n, ast.Name) else n.attr
        for n in ast.walk(_spawn_impl())
        if isinstance(n, (ast.Name, ast.Attribute))
    }
    assert not {"cached_admission_check", "admission_check"} & names
    assert not hasattr(subagent_mod, "cached_admission_check")
    cron = pathlib.Path(subagent_mod.__file__).with_name("cron.py").read_text(encoding="utf-8")
    assert "admission_check" in cron


# --- the behavioural census ---------------------------------------------------


async def _manager(monkeypatch: pytest.MonkeyPatch) -> tuple[SubagentManager, list[str]]:
    monkeypatch.setattr(subagent_mod, "Stats", MagicMock())
    monkeypatch.setattr(subagent_mod, "sel", MagicMock())
    mgr = SubagentManager(sessions=mock_sessions(), ctx_builder=mock_ctx(), max_concurrent=4)
    await wait_taskq_open(mgr)
    mgr._spawn_stagger_secs = 0.0
    mgr._last_spawn_ts = 0.0
    mgr._taskq_admit_wait_secs = 3600.0  # no re-check pass inside a scenario
    started: list[str] = []

    async def held(info) -> None:  # a start that stays running until teardown
        started.append(info.id)
        await asyncio.Event().wait()

    monkeypatch.setattr(mgr, "_run", held)
    return mgr, started


async def _teardown(mgr: SubagentManager) -> None:
    mgr._shutting_down = True
    tasks = [t for t in mgr._tasks.values() if not t.done()]
    for t in tasks:
        t.cancel()
    await asyncio.wait_for(asyncio.gather(*tasks, return_exceptions=True), _WAIT_SECS)
    mgr._taskq.close()


async def _until(pred, what: str) -> None:
    loop = asyncio.get_running_loop()
    deadline = loop.time() + _WAIT_SECS
    while not pred():
        assert loop.time() < deadline, what
        await asyncio.sleep(0.01)


async def _verdict(mgr: SubagentManager, started: list[str], task: str = "census", **kw):
    """``("started", "")``, ``("queued", reason)`` or ``("refused", error)``."""
    info = await mgr.spawn_async(task, parent_session_key=_PARENT, **kw)
    assert info is not None
    await _until(lambda: info.id in started or info.queued or info.done, "no verdict")
    if info.done:
        return "refused", info.error
    if info.queued:
        return "queued", info.queued_reason
    assert info.id in started, "neither started, queued nor refused"
    return "started", ""


@pytest.mark.asyncio
@pytest.mark.timeout(60)
async def test_every_capacity_condition_queues_and_only_policy_refuses(monkeypatch) -> None:
    mgr, started = await _manager(monkeypatch)
    verdicts: dict[str, tuple[str, str]] = {}
    try:
        verdicts["healthy"] = await _verdict(mgr, started)
        # The posture tier at critical is not a spawn verdict at all.
        critical = MagicMock(
            return_value=MagicMock(admitted=False, posture="critical", available_gb=1.2)
        )
        # Patched where a re-added gate would read it too (the name ``spawn_impl``
        # resolves in ``kiro_crew.subagent``'s namespace), not only at its source.
        with (
            patch("kiro_crew.resource_status.cached_admission_check", critical),
            patch("kiro_crew.resource_status.admission_check", critical),
            patch("kiro_crew.subagent.cached_admission_check", critical, create=True),
        ):
            verdicts["posture_critical"] = await _verdict(mgr, started)
        critical.assert_not_called()
        with monkeypatch.context() as low:
            low.setattr(subagent_mod, "check_memory_available", lambda *a, **k: (False, 0.5))
            verdicts["low_memory"] = await _verdict(mgr, started)
            verdicts["low_memory_non_durable"] = await _verdict(
                mgr, started, _memory_mode="incognito"
            )
        # The macOS kernel reports WARN while the healthy start, a dedicated
        # runtime of ours, still runs: the hold keeps the next root start.
        with monkeypatch.context() as kernel:
            kernel.setattr(platform_compat, "memory_pressure_level", lambda: 2)
            verdicts["memory_pressure"] = await _verdict(mgr, started)
            verdicts["memory_pressure_non_durable"] = await _verdict(
                mgr, started, _memory_mode="incognito"
            )
        mgr._spawn_stagger_secs = 3600.0
        mgr._last_spawn_ts = time.monotonic()
        verdicts["stagger"] = await _verdict(mgr, started)
        mgr._spawn_stagger_secs = 0.0
        mgr._last_spawn_ts = 0.0
        pinned = mgr._max_concurrent
        mgr._max_concurrent = 0
        verdicts["zero_cap"] = await _verdict(mgr, started)
        mgr._max_concurrent = pinned
        # The policy refusals, which stay refusals.
        verdicts["empty_task"] = await _verdict(mgr, started, task="   ")
        verdicts["cwd"] = await _verdict(mgr, started, cwd="/definitely/not/an/allowed/root")
        with patch("kiro_crew.subagent._vet_spawn_governance", return_value="spawning is off"):
            verdicts["governance"] = await _verdict(mgr, started)
        verdicts["memory_identity"] = await _verdict(mgr, started, _memory_mode="unknown")
    finally:
        await _teardown(mgr)

    kinds = {name: kind for name, (kind, _detail) in verdicts.items()}
    assert kinds == {
        "healthy": "started",
        "posture_critical": "started",
        "low_memory": "queued",
        "low_memory_non_durable": "queued",
        "memory_pressure": "queued",
        "memory_pressure_non_durable": "queued",
        "stagger": "queued",
        "zero_cap": "queued",
        "empty_task": "refused",
        "cwd": "refused",
        "governance": "refused",
        "memory_identity": "refused",
    }
    reasons = {detail for kind, detail in verdicts.values() if kind == "queued"}
    assert reasons == _WAIT_REASONS
    assert verdicts["low_memory_non_durable"] == ("queued", "low_memory")
    assert verdicts["memory_pressure"] == ("queued", "memory_pressure")
    assert verdicts["memory_pressure_non_durable"] == ("queued", "memory_pressure")
    assert verdicts["zero_cap"] == ("queued", "concurrency_limit")
    assert "governance" in verdicts["governance"][1]
    assert "memory_unavailable" in verdicts["memory_identity"][1]


@pytest.mark.asyncio
@pytest.mark.timeout(60)
@pytest.mark.parametrize("mode", ["persistent", "incognito", "no-store"])
async def test_the_memory_read_re_entry_is_not_a_second_submission(monkeypatch, mode) -> None:
    """An event-loop spawn stops at its memory read and re-enters the gate with
    the reading (``MemoryReadPoint``). That second pass is the same submission:
    it must not count the batch member again. It does re-run governance (a
    change made during the read must hold) on every path, a row
    ``spawn_async`` already committed included, and it reuses the parent's
    declaration read off the loop instead of reading the spec again on it."""
    mgr, started = await _manager(monkeypatch)
    store = mgr._taskq
    if mode == "no-store":
        mgr._taskq = None
    vetted: list[str] = []
    real_vet = subagent_mod._vet_spawn_governance

    def _vet(parent_session_key, agent, app=""):
        vetted.append(parent_session_key)
        return real_vet(parent_session_key, agent, app=app)

    monkeypatch.setattr(subagent_mod, "_vet_spawn_governance", _vet)
    loop_thread = threading.get_ident()
    policy_reads: list[int] = []
    real_policy = subagent_mod.parent_spawn_policy

    def _policy(parent_session_key):
        policy_reads.append(threading.get_ident())
        return real_policy(parent_session_key)

    monkeypatch.setattr(subagent_mod, "parent_spawn_policy", _policy)
    try:
        info = await mgr.spawn_async(
            "census",
            parent_session_key=_PARENT,
            batch_id="wave",
            batch_total=2,
            _memory_mode="persistent" if mode == "no-store" else mode,
        )
        await _until(lambda: info.id in started, "the healthy start never ran")
        assert mgr._batch_submitted["wave"][0] == 1, mgr._batch_submitted
        # Vetted before the read and again after it, a committed row's too.
        assert vetted == [_PARENT] * 2
        assert loop_thread not in policy_reads, "the parent spec was read on the loop"
    finally:
        mgr._taskq = store
        await _teardown(mgr)
