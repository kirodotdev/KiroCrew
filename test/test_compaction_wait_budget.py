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

import inspect
import re
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
    from kiro_crew.session import (
        _COMPACT_RESULT_WAIT_MARGIN_SECS,
        _compact_result_wait_secs,
    )

    assert (
        _compact_result_wait_secs(0.0)
        == COMPACT_WAIT_TIMEOUT_SECS - _COMPACT_RESULT_WAIT_MARGIN_SECS
    )
    # Shrinks as the /compact prompt turn consumes the budget.
    assert _compact_result_wait_secs(30.0) < _compact_result_wait_secs(0.0)


def test_inner_status_wait_fires_before_the_outer_cap():
    """The margin keeps the inner timeout landing strictly before the outer
    ``asyncio.wait_for``, so the graceful "no result" diagnostic stays
    reachable at every elapsed point where the budget is not nearly spent."""
    from kiro_crew.session import (
        _COMPACT_RESULT_WAIT_FLOOR_SECS,
        _COMPACT_RESULT_WAIT_MARGIN_SECS,
        _compact_result_wait_secs,
    )

    assert _COMPACT_RESULT_WAIT_MARGIN_SECS > 0
    step = COMPACT_WAIT_TIMEOUT_SECS / 20
    elapsed = 0.0
    while True:
        remaining = COMPACT_WAIT_TIMEOUT_SECS - elapsed
        if remaining <= _COMPACT_RESULT_WAIT_FLOOR_SECS + _COMPACT_RESULT_WAIT_MARGIN_SECS:
            break
        assert _compact_result_wait_secs(elapsed) < remaining
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


def test_no_hardcoded_120s_wait_reappears():
    """Regression guard for issue #2183: no call site may re-pin the old
    120-second wait. Call sites inherit the shared default instead of
    restating the budget."""
    pattern = re.compile(r"wait_for_compaction\(\s*timeout\s*=\s*120(?:\.0?)?\s*\)")
    offenders = [
        f"{path.relative_to(_SRC_ROOT.parent.parent)}:{i}"
        for path in sorted(_SRC_ROOT.rglob("*.py"))
        for i, line in enumerate(path.read_text(encoding="utf-8").splitlines(), 1)
        if pattern.search(line)
    ]
    assert not offenders, (
        "Hardcoded 120s compaction wait reintroduced (delete the timeout "
        f"argument so the shared default applies): {offenders}"
    )
