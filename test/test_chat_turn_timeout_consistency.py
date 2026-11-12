"""Tests for CHAT_TURN_TIMEOUT applied uniformly across _run_chat dispatch sites.

Every dispatch site must use ``spawn_guarded_turn`` or
``bounded_chat_turn(..., state=..., slot=...)``. The structured monitor's nested path is
pinned by its own tests.

The checks read source because the helpers own the timeout, cancellation order and
visible outcome; driving every real handler would need a full mocked request and slot.
"""

from __future__ import annotations

import ast
from pathlib import Path

_CHAT_API = Path(__file__).resolve().parent.parent / "src" / "kiro_crew" / "dashboard" / "chat_api"
_GATEWAY_RUNTIME = (
    Path(__file__).resolve().parent.parent / "src" / "kiro_crew" / "slack" / "gateway_runtime"
)

# Source files known to contain ``_run_chat`` dispatches.  When a new
# dispatch site lands in another file, add it here. The chat handlers and the
# Slack gateway are scanned whole: each facade and every owner it composes.
_DISPATCH_FILES = (
    "src/kiro_crew/dashboard/chat_handlers.py",
    *(f"src/kiro_crew/dashboard/chat_api/{path.name}" for path in sorted(_CHAT_API.glob("*.py"))),
    *sorted(
        f"src/kiro_crew/dashboard/chat_turn/{_owner.name}"
        for _owner in (
            Path(__file__).resolve().parent.parent / "src/kiro_crew/dashboard/chat_turn"
        ).glob("*.py")
        if _owner.name != "__init__.py"
    ),
    "src/kiro_crew/dashboard/chat_runner.py",
    "src/kiro_crew/dashboard/handlers/messaging.py",
    "src/kiro_crew/dashboard/session_deliver.py",
    "src/kiro_crew/slack/gateway.py",
    *(
        f"src/kiro_crew/slack/gateway_runtime/{path.name}"
        for path in sorted(_GATEWAY_RUNTIME.glob("*.py"))
    ),
)


def _src_root() -> Path:
    """Return the package source root (parent of test/)."""
    return Path(__file__).resolve().parent.parent


def test_cap_matches_inner_acp_prompt_timeout() -> None:
    """CHAT_TURN_TIMEOUT must match acp/client_timeouts.py:_DEFAULT_PROMPT_TIMEOUT.

    The dashboard layer's outer wall-clock cap should never bound below the
    transport layer's promised "longest legitimate turn" budget, otherwise
    legitimate long-running agentic turns die at the wall.
    """
    from kiro_crew.acp import client as acp_client
    from kiro_crew.constants import CHAT_TURN_TIMEOUT

    assert CHAT_TURN_TIMEOUT == acp_client._DEFAULT_PROMPT_TIMEOUT, (
        "CHAT_TURN_TIMEOUT must match _DEFAULT_PROMPT_TIMEOUT in acp/client.py — "
        "if you bump one, bump the other."
    )


def test_cap_value_is_four_hours() -> None:
    """The turn cap is 14400s and matches ``AgentConfig.chat_turn_timeout_secs``.

    It covers a 90-minute test command plus a fix and a re-run, and matches the ACP layer.
    Changing it means updating docs/system-specs/modules/learn-cron-dashboard.md.
    """
    from kiro_crew.config.loader import AgentConfig
    from kiro_crew.constants import CHAT_TURN_TIMEOUT

    assert CHAT_TURN_TIMEOUT == 14400.0
    assert AgentConfig().chat_turn_timeout_secs == CHAT_TURN_TIMEOUT


def _find_create_task_dispatches(path: Path) -> list[tuple[int, str]]:
    """Return ``[(line_no, body_text)]`` for every dispatch call body in *path*.

    Two dispatch forms exist and both must be counted:

    * ``spawn_guarded_turn(state, slot, _run_chat(...))``: owns the ceiling and the exception.
    * ``asyncio.create_task(bounded_chat_turn(_run_chat(...), state=..., slot=...))``: the
      off-loop form; its caller owns the done callback that consumes the exception.

    A balanced-paren scan, because the calls nest three deep with commas regex cannot split.
    """
    text = path.read_text(encoding="utf-8")
    out: list[tuple[int, str]] = []
    for opener in ("asyncio.create_task(", "spawn_guarded_turn("):
        i = 0
        while True:
            idx = text.find(opener, i)
            if idx < 0:
                break
            # Position cursor after the opening paren we just found.
            body_start = idx + len(opener)
            depth = 1
            cursor = body_start
            while cursor < len(text) and depth > 0:
                ch = text[cursor]
                if ch == "(":
                    depth += 1
                elif ch == ")":
                    depth -= 1
                cursor += 1
            # cursor now sits one past the matching close paren; -1 to exclude it
            body = text[body_start : cursor - 1]
            line_no = text[:idx].count("\n") + 1
            out.append((line_no, body))
            i = cursor
    return out


def test_no_bare_run_chat_dispatch_in_source() -> None:
    """Static guard: no ``_run_chat`` dispatch may run unbounded.

    A bare ``asyncio.create_task(_run_chat(...))`` has no wall-clock ceiling at all.
    """
    src_root = _src_root()

    offenders: list[str] = []
    for rel_path in _DISPATCH_FILES:
        path = src_root / rel_path
        for line_no, body in _find_create_task_dispatches(path):
            stripped = body.lstrip()
            if stripped.startswith("_run_chat("):
                offenders.append(f"{rel_path}:{line_no}")

    assert not offenders, (
        "Found bare _run_chat dispatch(es) with no turn ceiling:\n  "
        + "\n  ".join(offenders)
        + "\n\nRoute it through spawn_guarded_turn(state, slot, _run_chat(...))."
    )


def test_every_run_chat_dispatch_is_ceiling_bounded() -> None:
    """Positive guard: every dispatch is bounded by the shared ceiling.

    ``bounded_chat_turn`` needs both ``state`` and ``slot`` so the deadline cancels only the
    lease owner; an inline ``wait_for(timeout=600)`` would pass the bare-dispatch test.
    """
    src_root = _src_root()

    offenders: list[str] = []
    for rel_path in _DISPATCH_FILES:
        path = src_root / rel_path
        for line_no, body in _find_create_task_dispatches(path):
            if "_run_chat(" not in body:
                continue
            if "bounded_chat_turn(" in body:
                if "state=" not in body or "slot=" not in body:
                    offenders.append(f"{rel_path}:{line_no}")
                continue
            # spawn_guarded_turn bodies do not name the constant; the helper
            # resolves it. Identify them by the absence of an inner wait_for.
            if "asyncio.wait_for(" not in body:
                continue
            # Two accepted bounds: the config-resolved ceiling (preferred —
            # follows agent.chat_turn_timeout_secs above the 2h default) or the
            # legacy shared constant.
            if "chat_turn_timeout_secs(" not in body and "CHAT_TURN_TIMEOUT" not in body:
                offenders.append(f"{rel_path}:{line_no}")

    assert not offenders, (
        "Found _run_chat dispatch(es) wrapped without the shared ceiling:\n  "
        + "\n  ".join(offenders)
        + "\n\nUse spawn_guarded_turn(...), or owner-aware "
        "bounded_chat_turn(..., state=..., slot=...)."
    )


def test_dispatch_sites_consume_their_exception() -> None:
    """Every dispatch must have something that retrieves the task's outcome.

    A task whose only callback is ``state._background_tasks.discard`` never retrieves its
    ``TimeoutError``, so the turn dies with no card. A ``create_task(bounded_chat_turn(...))``
    site needs a callback that calls ``.exception()``.

    AST, not a line window: the callback may be defined above or below the dispatch.
    """
    src_root = _src_root()

    offenders: list[str] = []
    for rel_path in _DISPATCH_FILES:
        tree = ast.parse((src_root / rel_path).read_text(encoding="utf-8"))
        # Map each function to its enclosing-function chain so a nested
        # dispatch can see a callback defined in an outer scope.
        for func in _iter_functions(tree):
            inline_sites = [
                node for node in ast.walk(func) if _is_create_task_wrapped_run_chat_dispatch(node)
            ]
            if not inline_sites:
                continue
            consumes = any(
                isinstance(n, ast.Attribute) and n.attr == "exception" for n in ast.walk(func)
            )
            if not consumes:
                offenders.extend(f"{rel_path}:{s.lineno}" for s in inline_sites)

    # A nested function is walked both on its own and as part of its parent, so
    # the same site can be recorded twice; report each once.
    offenders = sorted(set(offenders))
    assert not offenders, (
        "Found _run_chat dispatch(es) whose exception is never retrieved — a "
        "turn that hits the ceiling there dies with no error card:\n  "
        + "\n  ".join(offenders)
        + "\n\nRoute it through spawn_guarded_turn(...), which consumes the "
        "outcome and renders a card naming the limit."
    )


def _iter_functions(tree: ast.AST):
    """Yield every function/coroutine definition in *tree*, outermost first."""
    for node in ast.walk(tree):
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
            yield node


def _calls_named(node: ast.AST, name: str) -> bool:
    """True if *node* is a call whose callee ends in *name*."""
    if not isinstance(node, ast.Call):
        return False
    func = node.func
    if isinstance(func, ast.Name):
        return func.id == name
    if isinstance(func, ast.Attribute):
        return func.attr == name
    return False


def _is_create_task_wrapped_run_chat_dispatch(node: ast.AST) -> bool:
    """True for a ``create_task`` wrapping a bounded ``_run_chat`` turn.

    ``spawn_guarded_turn`` sites are excluded: the helper consumes the
    exception itself. ``bounded_chat_turn`` and legacy ``wait_for`` sites leave
    exception retrieval to their outer task's done callback.
    """
    if not _calls_named(node, "create_task"):
        return False
    subtree = list(ast.walk(node))
    has_run_chat = any(_calls_named(n, "_run_chat") for n in subtree)
    has_wrapper = any(
        _calls_named(n, "bounded_chat_turn") or _calls_named(n, "wait_for") for n in subtree
    )
    return has_run_chat and has_wrapper


def test_dispatch_site_count_matches_expectation() -> None:
    """Pin the expected number of helper-visible ``_run_chat`` sites at 4.

    The guards above catch only missing ceilings; a new site must also be added to
    ``learn-cron-dashboard.md`` and this module.
    """
    src_root = _src_root()

    total = 0
    for rel_path in _DISPATCH_FILES:
        path = src_root / rel_path
        for _line_no, body in _find_create_task_dispatches(path):
            if "_run_chat(" in body:
                total += 1

    assert total == 4, (
        f"Expected 4 helper-visible _run_chat dispatch sites, found {total}.  "
        "If you added or removed one, update:\n"
        "  - the PR description\n"
        "  - docs/system-specs/modules/learn-cron-dashboard.md (Per-turn timeout section)\n"
        "  - this test's expected count"
    )
