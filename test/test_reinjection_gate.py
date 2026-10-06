"""Static gate: a turn loop that can compact its session consumes ``needs_reinjection``.

``session_compaction`` arms a one-shot ``needs_reinjection`` flag after an in-place
compaction drops the session-start context. The turn loops are copies by design
(each channel owns its renderer and error arms), so each one has to read-and-clear
the flag itself (``consume_reinjection``), hand it to ``build_message`` and put it
back in the turn's ``finally`` when the turn never lands (``rearm_reinjection``).
A copy that skips this runs every later turn without the skills index, the member
section and ``[RESPONSE PREFERENCES]``, and nothing at runtime notices. Every
compaction-capable module is therefore held to the contract here, so a new copy
that skips any of the three steps fails this gate instead of relying on a
reviewer grepping call sites.

The runtime half lives in ``test_background_loops_compaction_reinjection.py`` and
the per-channel ``TestCompactionReinjection`` classes. This is the discovery half,
and it is AST only:

* A module is *compaction-capable* when it calls ``check_context_usage`` or
  ``compact_if_needed``. That is the per-module property the flag belongs to, so
  ``build_message`` callers that never compact (hooks, planners, workflow memory)
  are never examined.
* Such a module complies when it consumes the flag, forwards a value that is not
  the literal ``False`` as ``needs_reinjection=`` to ``build_message`` (called
  directly, or handed as the callable to an off-loop runner such as
  ``asyncio.to_thread`` or ``run_in_embed_pool``), and re-arms it. A module that
  imports and calls ``messaging.dispatch.drive_turn``, or imports
  ``messaging.dispatch.ChannelTurns`` and answers through it, delegates all three,
  and the pipeline itself is held to the same rule.
* Modules that implement compaction rather than run a turn loop are exempted by
  name, and the exemption list can only shrink.
"""

from __future__ import annotations

import ast
from pathlib import Path

import pytest

_SRC = Path(__file__).resolve().parent.parent / "src" / "kiro_crew"

#: Calls that can compact a session in place, and so arm the flag.
_TRIGGERS = frozenset({"check_context_usage", "compact_if_needed"})

#: Calls that read-and-clear the flag.
_CONSUMERS = frozenset({"consume_reinjection", "consume_needs_reinjection"})

#: The call that puts the flag back when the turn that consumed it never lands.
#: ``mark_needs_reinjection`` is deliberately not counted: it is also how compaction
#: ARMS the flag, so a loop that compacts would otherwise satisfy this by arming.
_REARMS = frozenset({"rearm_reinjection"})

#: The shared turn driver that consumes, forwards and re-arms for its callers.
_DELEGATE_MODULE = "kiro_crew.messaging.dispatch"
_DELEGATE = "drive_turn"
#: The pipeline a dispatcher holds and calls ``answer`` on, the same delegate.
_PIPELINE = "ChannelTurns"

#: Modules that implement compaction for a caller instead of running a turn loop.
#: Shrink-only: ``test_the_implementer_exemptions_can_only_shrink`` fails once an
#: entry stops compacting, so a stale exemption cannot hide a future turn loop.
_IMPLEMENTERS = frozenset({"session.py"})


def _callee(func: ast.expr) -> str | None:
    if isinstance(func, ast.Name):
        return func.id
    if isinstance(func, ast.Attribute):
        return func.attr
    return None


def _names_build_message(node: ast.expr) -> bool:
    return _callee(node) == "build_message"


def _forwards_the_flag(call: ast.Call) -> bool:
    """``build_message(..., needs_reinjection=<not literal False>)``, either shape."""
    targets_build_message = _names_build_message(call.func) or any(
        _names_build_message(arg) for arg in call.args
    )
    if not targets_build_message:
        return False
    for keyword in call.keywords:
        if keyword.arg != "needs_reinjection":
            continue
        pinned_false = isinstance(keyword.value, ast.Constant) and keyword.value.value is False
        if not pinned_false:
            return True
    return False


def _missing_parts(tree: ast.AST) -> list[str]:
    """What *tree* lacks of the consume / forward / re-arm contract; empty if none."""
    calls = [node for node in ast.walk(tree) if isinstance(node, ast.Call)]
    missing = []
    if not any(_callee(call.func) in _CONSUMERS for call in calls):
        missing.append("never consumes the flag (consume_reinjection)")
    if not any(_forwards_the_flag(call) for call in calls):
        missing.append("never forwards it to build_message as needs_reinjection=")
    if not any(_callee(call.func) in _REARMS for call in calls):
        missing.append("never re-arms it when the turn does not land (rearm_reinjection)")
    return missing


def _imports_from_the_pipeline(tree: ast.AST, name: str) -> bool:
    return any(
        isinstance(node, ast.ImportFrom)
        and node.module == _DELEGATE_MODULE
        and any(alias.name == name and alias.asname is None for alias in node.names)
        for node in ast.walk(tree)
    )


def _delegates(tree: ast.AST) -> bool:
    """Whether *tree* hands its turns to the shared pipeline.

    Either it imports ``drive_turn`` and calls it, or it imports ``ChannelTurns``
    and calls ``answer`` on one.
    """
    calls = [node for node in ast.walk(tree) if isinstance(node, ast.Call)]
    drives = _imports_from_the_pipeline(tree, _DELEGATE) and any(
        isinstance(call.func, ast.Name) and call.func.id == _DELEGATE for call in calls
    )
    answers = _imports_from_the_pipeline(tree, _PIPELINE) and any(
        isinstance(call.func, ast.Attribute) and call.func.attr == "answer" for call in calls
    )
    return drives or answers


def _compacts(tree: ast.AST) -> bool:
    return any(
        isinstance(node, ast.Call) and _callee(node.func) in _TRIGGERS for node in ast.walk(tree)
    )


def _violation(source: str) -> str | None:
    """Why a compaction-capable *source* breaks the contract, or ``None``."""
    tree = ast.parse(source)
    if not _compacts(tree) or _delegates(tree):
        return None
    missing = _missing_parts(tree)
    return "; ".join(missing) if missing else None


def _compacting_modules() -> dict[str, ast.Module]:
    found: dict[str, ast.Module] = {}
    for path in sorted(_SRC.rglob("*.py")):
        source = path.read_text(encoding="utf-8")
        # Cheap text prefilter: parsing the whole tree costs seconds.
        if not any(trigger in source for trigger in _TRIGGERS):
            continue
        tree = ast.parse(source)
        if _compacts(tree):
            found[path.relative_to(_SRC).as_posix()] = tree
    return found


def test_every_compacting_turn_loop_consumes_needs_reinjection():
    modules = _compacting_modules()
    # A positive control: a renamed trigger must not quietly empty the gate.
    assert {"dashboard/chat_runner.py", "task_executor.py", "slack/handler.py"} <= set(modules)
    offenders = []
    for name, tree in modules.items():
        if name in _IMPLEMENTERS or _delegates(tree):
            continue
        missing = _missing_parts(tree)
        if missing:
            offenders.append(f"{name} can compact a session but " + "; ".join(missing))
    assert not offenders, "\n".join(offenders)


def _named(path: Path, kind: type, name: str) -> list[ast.AST]:
    tree = ast.parse(path.read_text(encoding="utf-8"))
    return [node for node in ast.walk(tree) if isinstance(node, kind) and node.name == name]


def test_the_shared_driver_keeps_the_contract_it_carries_for_its_callers():
    """The pipeline forwards the flag; its bracket consumes it and re-arms it."""
    (pipeline,) = _named(_SRC / "messaging" / "dispatch.py", ast.ClassDef, _PIPELINE)
    runs = [
        node
        for node in pipeline.body
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)) and node.name == "_run"
    ]
    (bracket,) = _named(_SRC / "messaging" / "turn_bracket.py", ast.ClassDef, "TurnBracket")
    assert len(runs) == 1
    carried = ast.Module(body=[runs[0], bracket], type_ignores=[])
    assert _missing_parts(carried) == []


def test_the_implementer_exemptions_can_only_shrink():
    modules = _compacting_modules()
    stale = sorted(name for name in _IMPLEMENTERS if name not in modules)
    assert not stale, f"no longer compacts, drop from _IMPLEMENTERS: {stale}"


_CONTRACT_KEPT_OFF_LOOP = """
async def loop(sessions, ctx, key):
    flag = False
    try:
        flag = consume_reinjection(sessions, key)
        await asyncio.to_thread(ctx.build_message, "hi", needs_reinjection=flag)
        sessions.check_context_usage(key)
    finally:
        rearm_reinjection(sessions, key, consumed=flag, landed=False)
"""

_CONTRACT_KEPT_DIRECT = """
def loop(sessions, ctx, key):
    flag = consume_reinjection(sessions, key)
    ctx.build_message("hi", needs_reinjection=flag)
    compact_if_needed(key)
    rearm_reinjection(sessions, key, consumed=flag, landed=True)
"""

_DELEGATED = """
from kiro_crew.messaging.dispatch import drive_turn

async def loop(turn, sessions, ctx):
    sessions.check_context_usage(turn.key)
    await drive_turn(turn, sessions=sessions, ctx_builder=ctx)
"""

_NEVER_PASSES_THE_FLAG = """
def loop(sessions, ctx, key):
    flag = consume_reinjection(sessions, key)
    ctx.build_message("hi", resumed=False)
    sessions.check_context_usage(key)
    rearm_reinjection(sessions, key, consumed=flag, landed=True)
"""

_PINS_FALSE = """
def loop(sessions, ctx, key):
    consume_reinjection(sessions, key)
    ctx.build_message("hi", needs_reinjection=False)
    sessions.check_context_usage(key)
    rearm_reinjection(sessions, key, consumed=False, landed=True)
"""

_NEVER_REARMS = """
def loop(sessions, ctx, key):
    flag = consume_reinjection(sessions, key)
    ctx.build_message("hi", needs_reinjection=flag)
    sessions.check_context_usage(key)
"""

_ANSWERS_THROUGH_THE_PIPELINE = """
from kiro_crew.messaging.dispatch import ChannelTurns

async def loop(turns, asker, sessions):
    sessions.check_context_usage(asker.session_key)
    await turns.answer(asker, "hi", renderer)
"""

_IMPORTS_THE_PIPELINE_WITHOUT_ANSWERING = """
from kiro_crew.messaging.dispatch import ChannelTurns

def loop(sessions, ctx, key):
    ctx.build_message("hi")
    sessions.check_context_usage(key)
"""

_IMPORTS_THE_DRIVER_WITHOUT_CALLING_IT = """
from kiro_crew.messaging.dispatch import drive_turn

def loop(sessions, ctx, key):
    ctx.build_message("hi")
    sessions.check_context_usage(key)
"""

_NEVER_COMPACTS = """
def loop(ctx):
    ctx.build_message("hi", resumed=True)
"""


@pytest.mark.parametrize(
    "source",
    [
        _CONTRACT_KEPT_OFF_LOOP,
        _CONTRACT_KEPT_DIRECT,
        _DELEGATED,
        _ANSWERS_THROUGH_THE_PIPELINE,
        _NEVER_COMPACTS,
    ],
    ids=["off-loop", "direct", "delegated", "answers-through-the-pipeline", "never-compacts"],
)
def test_a_loop_that_keeps_the_contract_passes(source):
    assert _violation(source) is None


@pytest.mark.parametrize(
    "source,missing",
    [
        (_NEVER_PASSES_THE_FLAG, "never forwards"),
        (_PINS_FALSE, "never forwards"),
        (_NEVER_REARMS, "never re-arms"),
        (_IMPORTS_THE_DRIVER_WITHOUT_CALLING_IT, "never consumes"),
        (_IMPORTS_THE_PIPELINE_WITHOUT_ANSWERING, "never consumes"),
    ],
    ids=["no-keyword", "literal-false", "no-rearm", "imported-not-called", "pipeline-not-answered"],
)
def test_a_loop_that_breaks_the_contract_is_named(source, missing):
    violation = _violation(source)
    assert violation is not None and missing in violation
