"""Tests for CHAT_TURN_TIMEOUT applied uniformly across _run_chat dispatch sites.

Background: the constant was originally introduced as a 600s
recovery-path budget, scoped to a single subagent-injection failure path. It
was later hoisted to a shared constant and added to chat_runner.py's
queue-drain path, but the primary user-typed turn (chat_handlers.py), the
cron injection path (handlers/messaging.py), the Slack/dashboard nudge path
(slack/gateway.py:_handle_nudge), and the cron-script delivery path
(slack/gateway.py:_deliver_script_result) remained unwrapped — depending on
the inner ACP _DEFAULT_PROMPT_TIMEOUT (7200s) instead.

This module verifies the cap value is correct AND that all helper-visible
dispatch sites in the source tree are wrapped with ``asyncio.wait_for(...,
timeout=CHAT_TURN_TIMEOUT)``. The structured dashboard monitor invokes
``_run_chat`` inside an authorization coroutine that is itself passed to
``spawn_guarded_turn``; its owning monitor tests pin that nested path.

Why source-level checks (not behavioral): a behavioral test that mocks
``_run_chat`` and patches ``CHAT_TURN_TIMEOUT`` to a tiny value can prove
``asyncio.wait_for`` raises ``TimeoutError`` — but that's stdlib behavior, not
verification of the application code. To test the wrap behaviorally would
require invoking each real handler entry point with a fully-mocked aiohttp
request, dashboard state, and slot — fragile, coupled to mock setup, and
still indirect. The source-level static checks below directly verify the
property we care about (every ``_run_chat`` dispatch is wrapped) and fail
loudly when a future contributor adds a new bare dispatch site.
"""

from __future__ import annotations

import ast
from pathlib import Path

# Source files known to contain ``_run_chat`` dispatches.  When a new
# dispatch site lands in another file, add it here.
_DISPATCH_FILES = (
    "src/kiro_crew/dashboard/chat_handlers.py",
    "src/kiro_crew/dashboard/chat_runner.py",
    "src/kiro_crew/dashboard/handlers/messaging.py",
    "src/kiro_crew/slack/gateway.py",
)


def _src_root() -> Path:
    """Return the package source root (parent of test/)."""
    return Path(__file__).resolve().parent.parent


def test_cap_matches_inner_acp_prompt_timeout() -> None:
    """CHAT_TURN_TIMEOUT must match acp/client.py:_DEFAULT_PROMPT_TIMEOUT.

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


def test_cap_value_is_seven_thousand_two_hundred() -> None:
    """Regression guard against silently changing the value back to 600s.

    The 600s value was sized for a recovery-path budget, not the master cap.
    7200s aligns with the ACP layer underneath. If you intend to change this,
    update docs/system-specs/modules/learn-cron-dashboard.md too.
    """
    from kiro_crew.constants import CHAT_TURN_TIMEOUT

    assert CHAT_TURN_TIMEOUT == 7200.0


def _find_create_task_dispatches(path: Path) -> list[tuple[int, str]]:
    """Return ``[(line_no, body_text)]`` for every dispatch call body in *path*.

    Two dispatch forms exist and both must be counted:

    * ``spawn_guarded_turn(state, slot, _run_chat(...))`` — the preferred form.
      The helper owns the ceiling AND retrieves the resulting exception, so a
      turn that hits the ceiling renders a card instead of vanishing.
    * ``asyncio.create_task(asyncio.wait_for(_run_chat(...), timeout=...))`` —
      the older inline form, still used by the two gateway sites that attach
      their own done-callback to consume the exception.

    Why a hand-rolled balanced-paren scan instead of regex: nested call
    expressions go three levels deep with embedded commas, which regex does not
    handle cleanly. We tokenize ``(`` / ``)`` until the depth returns to zero.
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

    A bare ``asyncio.create_task(_run_chat(...))`` has no wall-clock ceiling at
    the dashboard layer at all. Catches regressions where a future contributor
    adds a new dispatch site without either wrapping it or routing it through
    ``spawn_guarded_turn``.

    This test has already paid for itself once: during an earlier rebase it
    caught a dispatch site that had landed on the base branch and would
    otherwise have shipped unwrapped.
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

    A dispatch qualifies either by going through ``spawn_guarded_turn`` (which
    resolves the ceiling itself, clamps it against the transport timeout, and
    consumes the exception so a ceiling hit is visible) or by an inline
    ``wait_for`` that references ``CHAT_TURN_TIMEOUT`` rather than a
    hard-coded number.

    This complements ``test_no_bare_run_chat_dispatch_in_source``: that test
    ensures no dispatch is bare, while this one ensures the bound is the shared
    one. A contributor could otherwise wrap with ``wait_for(timeout=600)`` and
    pass the first test.
    """
    src_root = _src_root()

    offenders: list[str] = []
    for rel_path in _DISPATCH_FILES:
        path = src_root / rel_path
        for line_no, body in _find_create_task_dispatches(path):
            if "_run_chat(" not in body:
                continue
            # spawn_guarded_turn bodies do not name the constant; the helper
            # resolves it. Identify them by the absence of an inner wait_for.
            if "asyncio.wait_for(" not in body:
                continue
            # Two accepted bounds: the config-resolved ceiling (preferred —
            # follows agent.chat_turn_timeout_secs above the 2h default) or the
            # legacy shared constant.
            if (
                "chat_turn_timeout_secs(" not in body
                and "CHAT_TURN_TIMEOUT" not in body
            ):
                offenders.append(f"{rel_path}:{line_no}")

    assert not offenders, (
        "Found _run_chat dispatch(es) wrapped without the shared ceiling:\n  "
        + "\n  ".join(offenders)
        + "\n\nUse spawn_guarded_turn(...), or "
        "wait_for(..., timeout=chat_turn_timeout_secs())."
    )


def test_dispatch_sites_consume_their_exception() -> None:
    """Every dispatch must have something that retrieves the task's outcome.

    This is the regression that made long turns die silently: a task whose only
    done-callback was ``state._background_tasks.discard`` never had its
    ``TimeoutError`` retrieved, so hitting the ceiling produced no error card
    and no log the user would find — it surfaced only as a
    garbage-collection-time "Task exception was never retrieved" line.

    ``spawn_guarded_turn`` satisfies this by construction. An inline
    ``create_task`` site must attach its own callback that calls
    ``.exception()``; a site whose sole callback is the bare ``discard`` is the
    exact shape of the original defect.

    Uses the AST rather than a line window because a done-callback may be
    defined either above or below the dispatch it is attached to — a
    directional text scan gets the answer wrong depending on local style.
    """
    src_root = _src_root()

    offenders: list[str] = []
    for rel_path in _DISPATCH_FILES:
        tree = ast.parse((src_root / rel_path).read_text(encoding="utf-8"))
        # Map each function to its enclosing-function chain so a nested
        # dispatch can see a callback defined in an outer scope.
        for func in _iter_functions(tree):
            inline_sites = [
                node
                for node in ast.walk(func)
                if _is_inline_wrapped_run_chat_dispatch(node)
            ]
            if not inline_sites:
                continue
            consumes = any(
                isinstance(n, ast.Attribute) and n.attr == "exception"
                for n in ast.walk(func)
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


def _is_inline_wrapped_run_chat_dispatch(node: ast.AST) -> bool:
    """True for ``create_task(wait_for(_run_chat(...)))`` — the inline form.

    ``spawn_guarded_turn`` sites are excluded: the helper consumes the
    exception itself, which is the whole point of routing through it.
    """
    if not _calls_named(node, "create_task"):
        return False
    subtree = list(ast.walk(node))
    has_run_chat = any(_calls_named(n, "_run_chat") for n in subtree)
    has_wait_for = any(_calls_named(n, "wait_for") for n in subtree)
    return has_run_chat and has_wait_for


def test_dispatch_site_count_matches_expectation() -> None:
    """Pin the expected number of helper-visible ``_run_chat`` sites at 7.

    If a new dispatch lands (or one is removed), this fails loudly so the
    contributor updates the PR description, the spec doc
    (``learn-cron-dashboard.md``), and the other tests in this module.

    Without this check, a new dispatch site would slip past review — the
    static guards above only fire on *missing* ceilings, not on *additional*
    sites that need to be documented.
    """
    src_root = _src_root()

    total = 0
    for rel_path in _DISPATCH_FILES:
        path = src_root / rel_path
        for _line_no, body in _find_create_task_dispatches(path):
            if "_run_chat(" in body:
                total += 1

    assert total == 7, (
        f"Expected 7 helper-visible _run_chat dispatch sites, found {total}.  "
        "If you added or removed one, update:\n"
        "  - the PR description\n"
        "  - docs/system-specs/modules/learn-cron-dashboard.md (Per-turn timeout section)\n"
        "  - this test's expected count"
    )
