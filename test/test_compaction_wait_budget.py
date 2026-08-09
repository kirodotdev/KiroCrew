"""The compaction wait budget is a single shared constant.

Manual (/compact, !compact, channel commands) and automatic
(context-threshold) compaction perform the identical operation, so they share
one wait budget: ``kiro_crew.constants.COMPACT_WAIT_TIMEOUT_SECS``. A shorter
manual budget reports "Compaction timed out." on work that is still running
and subsequently succeeds — the budget expires, not the work (issue #2183).

These tests assert against the shared constant, never a literal value, so
they keep holding if the budget is later tuned.
"""

from __future__ import annotations

import ast
import inspect
from pathlib import Path

import pytest

from kiro_crew.constants import COMPACT_WAIT_TIMEOUT_SECS

_SRC_ROOT = Path(__file__).resolve().parent.parent / "src" / "kiro_crew"


def _wait_default(func) -> object:
    return inspect.signature(func).parameters["timeout"].default


def test_provider_abc_default_is_shared_budget():
    """The base LLMProvider default — inherited by every manual call site
    that passes no explicit timeout — is the shared budget."""
    from kiro_crew.providers.base import LLMProvider

    assert _wait_default(LLMProvider.wait_for_compaction) == COMPACT_WAIT_TIMEOUT_SECS


@pytest.mark.parametrize(
    "import_path",
    [
        "kiro_crew.providers.acp.AcpProvider",
        "kiro_crew.acp.client.AcpClient",
        "kiro_crew.acp.session_handle.AcpSessionHandle",
        "kiro_crew.acp.session_provider.AcpSessionProvider",
    ],
)
def test_every_implementation_default_is_shared_budget(import_path: str):
    """Every concrete wait_for_compaction implementation carries the same
    default, so no delegation layer silently shortens the wait."""
    module_path, cls_name = import_path.rsplit(".", 1)
    module = __import__(module_path, fromlist=[cls_name])
    cls = getattr(module, cls_name)
    assert _wait_default(cls.wait_for_compaction) == COMPACT_WAIT_TIMEOUT_SECS


def test_automatic_compaction_uses_shared_budget():
    """The automatic context-threshold path in session.py budgets with the
    same shared constant as the manual paths."""
    import kiro_crew.session as session_mod

    assert session_mod.COMPACT_WAIT_TIMEOUT_SECS is COMPACT_WAIT_TIMEOUT_SECS


def test_inner_status_wait_spends_the_remaining_shared_budget():
    """The in-place path's async status wait derives from what remains of the
    shared budget — no fixed slice may strand budget while a still-running
    compaction is abandoned and its session recycled."""
    from kiro_crew.session import _compact_result_wait_secs

    assert _compact_result_wait_secs(0.0) == COMPACT_WAIT_TIMEOUT_SECS
    # Shrinks as the /compact prompt turn consumes the budget.
    assert _compact_result_wait_secs(30.0) < _compact_result_wait_secs(0.0)


def test_inner_status_wait_spends_the_full_remaining_budget():
    """The inner wait never truncates the shared budget: at every elapsed
    point it gets AT LEAST the remaining budget, so a compaction completing
    in the final seconds is not abandoned early."""
    from kiro_crew.session import _compact_result_wait_secs

    step = COMPACT_WAIT_TIMEOUT_SECS / 20
    elapsed = 0.0
    while elapsed < COMPACT_WAIT_TIMEOUT_SECS:
        remaining = COMPACT_WAIT_TIMEOUT_SECS - elapsed
        assert _compact_result_wait_secs(elapsed) >= remaining
        elapsed += step


def test_inner_status_wait_lands_before_the_outer_cap():
    """The outer ``asyncio.wait_for`` carries the margin as headroom, so the
    inner timeout lands strictly before it and the graceful "no result"
    diagnostic stays reachable while the prompt phase is within budget."""
    from kiro_crew.session import (
        _COMPACT_RESULT_WAIT_MARGIN_SECS,
        _compact_result_wait_secs,
    )

    assert _COMPACT_RESULT_WAIT_MARGIN_SECS > 0
    outer_cap = COMPACT_WAIT_TIMEOUT_SECS + _COMPACT_RESULT_WAIT_MARGIN_SECS
    step = COMPACT_WAIT_TIMEOUT_SECS / 20
    elapsed = 0.0
    while elapsed < COMPACT_WAIT_TIMEOUT_SECS:
        assert elapsed + _compact_result_wait_secs(elapsed) < outer_cap
        elapsed += step


def test_inner_status_wait_never_below_floor_or_non_positive():
    """A prompt turn that ran long (or clock weirdness) clamps to the floor,
    never to zero or a negative timeout."""
    from kiro_crew.session import (
        _COMPACT_RESULT_WAIT_FLOOR_SECS,
        _compact_result_wait_secs,
    )

    assert _COMPACT_RESULT_WAIT_FLOOR_SECS > 0
    for elapsed in (COMPACT_WAIT_TIMEOUT_SECS, COMPACT_WAIT_TIMEOUT_SECS * 10):
        assert _compact_result_wait_secs(elapsed) == _COMPACT_RESULT_WAIT_FLOOR_SECS


def test_no_call_site_pins_a_shorter_wait():
    """Regression guard for issue #2183: no production call site may pass an
    explicit numeric-literal timeout below the shared budget — keyword or
    positional, int or float. Call sites inherit the
    shared default instead of restating the budget. Non-literal arguments
    (e.g. session.py's remaining-budget variable) are intentionally exempt:
    they are derived from the shared budget and covered by the tests above.
    """
    offenders: list[str] = []
    for path in sorted(_SRC_ROOT.rglob("*.py")):
        tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
        for node in ast.walk(tree):
            if not isinstance(node, ast.Call):
                continue
            func = node.func
            name = func.attr if isinstance(func, ast.Attribute) else getattr(func, "id", None)
            if name != "wait_for_compaction":
                continue
            args = list(node.args[:1]) + [kw.value for kw in node.keywords if kw.arg == "timeout"]
            for arg in args:
                try:
                    value = ast.literal_eval(arg)
                except (ValueError, SyntaxError):
                    continue  # non-literal (derived) timeouts are exempt
                if isinstance(value, (int, float)) and value < COMPACT_WAIT_TIMEOUT_SECS:
                    offenders.append(
                        f"{path.relative_to(_SRC_ROOT.parent.parent)}:{node.lineno}"
                        f" (timeout={value})"
                    )
    assert not offenders, (
        "Compaction wait shorter than the shared budget reintroduced (delete "
        f"the timeout argument so the shared default applies): {offenders}"
    )
