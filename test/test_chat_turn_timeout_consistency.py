"""Tests for CHAT_TURN_TIMEOUT applied uniformly across _run_chat dispatch sites.

Background: the constant was originally introduced in CR-268843007 as a 600s
recovery-path budget, scoped to a single subagent-injection failure path. It
was hoisted to a shared constant in CR-272778307 and added to chat_runner.py's
queue-drain path, but the primary user-typed turn (chat_handlers.py), the
cron injection path (handlers/messaging.py), the Slack/dashboard nudge path
(slack/gateway.py:_handle_nudge), and the cron-script delivery path
(slack/gateway.py:_deliver_script_result) remained unwrapped — depending on
the inner ACP _DEFAULT_PROMPT_TIMEOUT (7200s) instead.

This module verifies the cap value is correct AND that all eight dispatch
sites in the source tree are wrapped with ``asyncio.wait_for(...,
timeout=CHAT_TURN_TIMEOUT)``.

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
    """Return ``[(line_no, body_text)]`` for every ``asyncio.create_task(...)``
    call body in *path*.

    Why a hand-rolled balanced-paren scan instead of regex: nested call
    expressions like ``create_task(asyncio.wait_for(_run_chat(...)))`` go
    three levels deep with embedded commas, which regex does not handle
    cleanly. We tokenize ``(`` / ``)`` until the depth returns to zero.
    """
    text = path.read_text(encoding="utf-8")
    out: list[tuple[int, str]] = []
    i = 0
    while True:
        idx = text.find("asyncio.create_task(", i)
        if idx < 0:
            break
        # Position cursor after the opening paren we just found.
        body_start = idx + len("asyncio.create_task(")
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
    """Static guard: every ``asyncio.create_task(_run_chat(...))`` in the
    dashboard / slack / handler layer must be wrapped in ``asyncio.wait_for``.

    Catches regressions where a future contributor adds a new dispatch site
    without the wrap.  The check is intentionally simple: if the FIRST
    expression inside ``create_task(`` is ``_run_chat(``, that's a bare
    dispatch.

    This test has already paid for itself once: during the rebase of this CR
    onto ``origin/beta-braveheart`` it caught a 7th dispatch site
    (``_deliver_script_result`` in ``slack/gateway.py``) that had landed on
    the base branch and would otherwise have shipped unwrapped.
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
        "Found bare _run_chat dispatch(es) without CHAT_TURN_TIMEOUT wrapping:\n  "
        + "\n  ".join(offenders)
        + "\n\nWrap with asyncio.wait_for(_run_chat(...), timeout=CHAT_TURN_TIMEOUT)."
    )


def test_every_run_chat_dispatch_uses_chat_turn_timeout() -> None:
    """Positive guard: every ``_run_chat`` invocation inside a ``create_task``
    must reference ``CHAT_TURN_TIMEOUT`` within the same call body.

    This complements ``test_no_bare_run_chat_dispatch_in_source``:

    - The "no bare" test ensures no dispatch is bare — but a future
      contributor could technically wrap with ``wait_for(timeout=600)`` and
      pass that test.
    - This test ensures the wrap actually uses the shared constant, not a
      hard-coded value.

    Together they pin: every dispatch is wrapped AND every wrap uses the
    shared cap.
    """
    src_root = _src_root()

    offenders: list[str] = []
    for rel_path in _DISPATCH_FILES:
        path = src_root / rel_path
        for line_no, body in _find_create_task_dispatches(path):
            if "_run_chat(" not in body:
                continue
            if "CHAT_TURN_TIMEOUT" not in body:
                offenders.append(f"{rel_path}:{line_no}")

    assert not offenders, (
        "Found _run_chat dispatch(es) wrapped without CHAT_TURN_TIMEOUT:\n  "
        + "\n  ".join(offenders)
        + "\n\nUse asyncio.wait_for(_run_chat(...), timeout=CHAT_TURN_TIMEOUT) "
        + "so all dispatches share the same outer cap."
    )


def test_dispatch_site_count_matches_expectation() -> None:
    """Pin the expected number of ``_run_chat`` dispatch sites at 8.

    The CR description enumerates the sites (the eighth is the post-fan-out
    synthesis turn in ``chat_runner.py``). If a new dispatch lands (or one is
    removed), this test fails loudly so the contributor updates the CR
    description, the spec doc (``learn-cron-dashboard.md``), and the other
    tests in this module.

    Without this check, a new wrapped dispatch site would silently slip past
    review — the static guards above only fire on *missing* wraps, not on
    *additional* sites that need to be documented.
    """
    src_root = _src_root()

    total = 0
    for rel_path in _DISPATCH_FILES:
        path = src_root / rel_path
        for _line_no, body in _find_create_task_dispatches(path):
            if "_run_chat(" in body:
                total += 1

    assert total == 8, (
        f"Expected 8 _run_chat dispatch sites, found {total}.  "
        "If you added or removed one, update:\n"
        "  - the CR description / Mesh ticket\n"
        "  - docs/system-specs/modules/learn-cron-dashboard.md (Per-turn timeout section)\n"
        "  - this test's expected count"
    )
