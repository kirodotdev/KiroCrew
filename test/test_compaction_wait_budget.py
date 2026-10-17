"""The compaction wait budget is one budget, shared by every caller.

Manual (/compact, !compact, channel commands) and automatic
(context-threshold) compaction perform the identical operation, so they share
one wait budget: ``kiro_crew.constants.COMPACT_WAIT_TIMEOUT_SECS``, raised for
all of them by ``session.compact_wait_secs``. A shorter manual budget reports
"Compaction timed out." on work that is still running and subsequently
succeeds — the budget expires, not the work.

These tests assert against the shared constant, never a literal value, so
they keep holding if the budget is later tuned.
"""

from __future__ import annotations

import ast
import inspect

import pytest
from source_corpus import parsed_candidates, src_root

from kiro_crew.constants import COMPACT_WAIT_TIMEOUT_SECS

# One xdist worker for the whole module: `test_no_call_site_pins_a_shorter_wait` scans
# src/ through the shared corpus, and under `--dist loadgroup` an unmarked module is
# spread across workers, each of which re-pays the corpus read. One group PER FILE.
pytestmark = pytest.mark.xdist_group(name="tree_scan_test_compaction_wait_budget")


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


# ── The budget is configurable (session.compact_wait_secs) ────
#
# The report's "No config key changes it" is the defect: both the outer budget
# and the inner status wait read the built-in constant, so no setting reaches
# either. A positive ``session.compact_wait_secs`` must change the EFFECTIVE
# budget, and it must reach the inner status wait too — the one the logged
# failures ("async status wait 300s") spend the whole budget in. 0 (the
# default) must keep the built-in budget, so default behavior is unchanged.


def test_configured_budget_zero_falls_back_to_built_in():
    """0 (the default) and any non-positive value resolve to the built-in
    budget, so an install that sets nothing behaves exactly as before."""
    from kiro_crew.session import _resolve_compact_wait_secs

    assert _resolve_compact_wait_secs(0.0) == COMPACT_WAIT_TIMEOUT_SECS
    assert _resolve_compact_wait_secs(-1.0) == COMPACT_WAIT_TIMEOUT_SECS


def test_configured_budget_positive_is_the_effective_budget():
    """A positive setting is used verbatim as the effective budget."""
    from kiro_crew.session import _resolve_compact_wait_secs

    assert _resolve_compact_wait_secs(600.0) == 600.0


def test_configured_budget_reaches_the_inner_status_wait():
    """The inner status wait derives from the EFFECTIVE budget, not the
    built-in constant — raising the setting raises the inner wait, which is
    where the measured timeouts occur."""
    from kiro_crew.session import _compact_result_wait_secs, _resolve_compact_wait_secs

    budget = _resolve_compact_wait_secs(600.0)
    assert _compact_result_wait_secs(0.0, budget) == 600.0
    # Still spends the FULL remaining raised budget, never a fixed slice.
    assert _compact_result_wait_secs(100.0, budget) == 500.0


@pytest.mark.asyncio
async def test_inner_wait_uses_the_snapshotted_budget_not_a_live_reread():
    """One compaction uses one effective budget: the inner status-wait DI
    callable derives from the budget passed to it, so a live
    ``session.compact_wait_secs`` change between the outer snapshot and the
    inner call cannot split one compaction across two budgets."""
    from kiro_crew.config.loader import KiroCrewConfig
    from kiro_crew.session import SessionManager

    cfg = KiroCrewConfig()
    cfg.session.compact_wait_secs = 600.0
    mgr = SessionManager(cfg)
    inner = mgr._compaction._deps.compact_result_wait_secs

    # The outer path snapshots the budget and hands it to the inner wait.
    snapshot = mgr._compaction._deps.compact_wait_timeout_secs()
    assert snapshot == 600.0

    # A live config change lands AFTER the snapshot. The inner wait is driven by
    # the snapshot passed to it, so the mutation does not reach it.
    cfg.session.compact_wait_secs = 60.0
    assert inner(0.0, snapshot) == 600.0
    assert inner(100.0, snapshot) == 500.0


def test_default_config_leaves_compaction_budget_at_built_in():
    """A freshly-defaulted config resolves to the built-in budget — the key
    exists but changes nothing until an operator sets it."""
    from kiro_crew.config.sections import SessionConfig
    from kiro_crew.session import _resolve_compact_wait_secs

    cfg = SessionConfig()
    assert cfg.compact_wait_secs == 0.0
    assert _resolve_compact_wait_secs(cfg.compact_wait_secs) == COMPACT_WAIT_TIMEOUT_SECS


def test_load_honours_a_configured_budget(tmp_path, monkeypatch):
    """A written ``session.compact_wait_secs`` survives the full load path and
    resolves to the operator's value — the builder must read the key, not drop
    it back to the default."""
    import json

    from kiro_crew.config import loader as loader_module
    from kiro_crew.config.loader import KiroCrewConfig
    from kiro_crew.session import _resolve_compact_wait_secs

    path = tmp_path / "config.json"
    path.write_text(json.dumps({"session": {"compact_wait_secs": 900.0}}), encoding="utf-8")
    monkeypatch.setattr(loader_module, "config_path", lambda: path)
    monkeypatch.setattr(loader_module, "config_local_path", lambda: tmp_path / "missing.local.json")

    cfg = KiroCrewConfig.load()

    assert cfg.session.compact_wait_secs == 900.0
    assert _resolve_compact_wait_secs(cfg.session.compact_wait_secs) == 900.0


def test_load_clamps_an_out_of_range_budget(tmp_path, monkeypatch):
    """A negative value collapses to the sentinel (built-in budget), a
    near-zero positive value is lifted to the floor, and an oversized value is
    capped, so a hand-edited typo cannot arm a near-zero or unbounded wait."""
    import json

    from kiro_crew.config import loader as loader_module
    from kiro_crew.config.loader import KiroCrewConfig
    from kiro_crew.config.sections import COMPACT_WAIT_SECS_MAX, COMPACT_WAIT_SECS_MIN
    from kiro_crew.session import _resolve_compact_wait_secs

    monkeypatch.setattr(loader_module, "config_local_path", lambda: tmp_path / "missing.local.json")

    neg = tmp_path / "neg.json"
    neg.write_text(json.dumps({"session": {"compact_wait_secs": -5.0}}), encoding="utf-8")
    monkeypatch.setattr(loader_module, "config_path", lambda: neg)
    cfg = KiroCrewConfig.load()
    assert cfg.session.compact_wait_secs == 0.0
    assert _resolve_compact_wait_secs(cfg.session.compact_wait_secs) == COMPACT_WAIT_TIMEOUT_SECS

    tiny = tmp_path / "tiny.json"
    tiny.write_text(json.dumps({"session": {"compact_wait_secs": 5.0}}), encoding="utf-8")
    monkeypatch.setattr(loader_module, "config_path", lambda: tiny)
    cfg = KiroCrewConfig.load()
    assert cfg.session.compact_wait_secs == COMPACT_WAIT_SECS_MIN

    big = tmp_path / "big.json"
    big.write_text(json.dumps({"session": {"compact_wait_secs": 10_000.0}}), encoding="utf-8")
    monkeypatch.setattr(loader_module, "config_path", lambda: big)
    cfg = KiroCrewConfig.load()
    assert cfg.session.compact_wait_secs == COMPACT_WAIT_SECS_MAX


def test_no_call_site_pins_a_shorter_wait():
    """Regression guard: no production call site may pass an
    explicit numeric-literal timeout below the shared budget — keyword or
    positional, int or float. Call sites pass the resolved budget instead of
    restating it. Non-literal arguments (``sessions.compact_wait_budget_secs()``,
    session.py's remaining-budget variable) are intentionally exempt:
    they are derived from the shared budget and covered by the tests above.
    """
    offenders: list[str] = []
    # Only a file whose text names `wait_for_compaction` can hold a call to it, so
    # the corpus parses those few files instead of the whole tree; the corpus
    # NFKC-folds both sides, as CPython does for identifiers. A module that fails
    # to parse propagates -- an unparseable file is a hole in this gate's coverage.
    root = src_root()
    for path, _text, tree in parsed_candidates(
        require_all=("wait_for_compaction",), skip_syntax_errors=False
    ):
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
                        f"src/kiro_crew/{path.relative_to(root).as_posix()}:{node.lineno}"
                        f" (timeout={value})"
                    )
    assert not offenders, (
        "Compaction wait shorter than the shared budget reintroduced (pass "
        f"timeout=sessions.compact_wait_budget_secs() instead of a literal): {offenders}"
    )


# ── Every call site honours the configured budget ─────────────
#
# One resolver: ``SessionManager.compact_wait_budget_secs()``. The automatic
# coordinator, the task runner's context-overflow compaction, the dashboard
# /compact and every chat channel's compact command and near-limit compaction
# all hold the session manager and read the budget there, so no caller can
# resolve ``session.compact_wait_secs`` differently (a second, live-snapshot
# resolver fell back to the built-in budget wherever no watcher was armed). A
# call site that leans on the provider's default instead waits the built-in
# budget whatever the operator set.


def test_there_is_one_budget_resolver():
    """No module-level resolver sits beside the manager's: a second one is
    the drift the single method exists to prevent."""
    import kiro_crew.session as session_mod

    assert not hasattr(session_mod, "compact_wait_budget_secs")
    assert callable(session_mod.SessionManager.compact_wait_budget_secs)


#: Variables a production call may pass that already hold a resolved budget:
#: the coordinator's remaining slice of its own resolved budget
#: (``session_compaction.py``).
_RESOLVED_BUDGET_NAMES = frozenset({"result_wait_used"})


def _call_name(func: ast.expr) -> str | None:
    return func.attr if isinstance(func, ast.Attribute) else getattr(func, "id", None)


def _sanctioned_budget(arg: ast.expr, enclosing: str | None) -> bool:
    """Whether *arg* is a budget that sees ``session.compact_wait_secs``.

    An allowlist, not a denylist of the built-in constant: a literal, the
    constant itself (``COMPACT_WAIT_TIMEOUT_SECS``) and any other expression all
    restate a fixed budget that never sees the key.
    """
    if (
        isinstance(arg, ast.Call)
        and isinstance(arg.func, ast.Attribute)
        and arg.func.attr == "compact_wait_budget_secs"
    ):
        return True  # the SessionManager method, the one resolver
    if isinstance(arg, ast.Name):
        # A delegating implementation forwards its own ``timeout`` parameter.
        if arg.id == "timeout" and enclosing == "wait_for_compaction":
            return True
        return arg.id in _RESOLVED_BUDGET_NAMES
    return False


def test_every_call_site_passes_a_configurable_budget():
    """Regression guard: every production ``wait_for_compaction`` call passes a
    budget derived from ``session.compact_wait_secs``. Relying on the provider
    default, or passing ``COMPACT_WAIT_TIMEOUT_SECS`` or any literal explicitly,
    waits the built-in budget whatever the operator set. Every caller holds the
    session manager and passes ``sessions.compact_wait_budget_secs()`` (a bare
    module-level resolver is refused), and the coordinator passes its own
    resolved budget."""
    offenders: list[str] = []
    root = src_root()
    for path, _text, tree in parsed_candidates(
        require_all=("wait_for_compaction",), skip_syntax_errors=False
    ):
        enclosing: dict[ast.AST, str] = {}
        for fn in ast.walk(tree):
            if isinstance(fn, (ast.FunctionDef, ast.AsyncFunctionDef)):
                for child in ast.walk(fn):
                    enclosing[child] = fn.name  # innermost def wins (walked later)
        for node in ast.walk(tree):
            if not isinstance(node, ast.Call) or _call_name(node.func) != "wait_for_compaction":
                continue
            args = list(node.args[:1]) + [kw.value for kw in node.keywords if kw.arg == "timeout"]
            where = f"src/kiro_crew/{path.relative_to(root).as_posix()}:{node.lineno}"
            if not args:
                offenders.append(f"{where} (no budget)")
            for arg in args:
                if not _sanctioned_budget(arg, enclosing.get(node)):
                    offenders.append(f"{where} (timeout={ast.unparse(arg)})")
    assert not offenders, (
        "wait_for_compaction() called with a budget that ignores "
        "session.compact_wait_secs; pass the session manager's "
        f"timeout=sessions.compact_wait_budget_secs(): {offenders}"
    )


def test_guard_rejects_the_built_in_constant_and_literals():
    """The guard above is only as good as what it refuses: the built-in
    constant and a literal must both fail it, a forwarded ``timeout`` outside an
    implementation must fail it, and the sanctioned forms must pass."""

    def arg(src: str) -> ast.expr:
        return ast.parse(src, mode="eval").body

    assert not _sanctioned_budget(arg("COMPACT_WAIT_TIMEOUT_SECS"), "handler")
    assert not _sanctioned_budget(arg("constants.COMPACT_WAIT_TIMEOUT_SECS"), "handler")
    assert not _sanctioned_budget(arg("900.0"), "handler")
    assert not _sanctioned_budget(arg("timeout"), "handler")
    assert not _sanctioned_budget(arg("compact_wait_budget_secs()"), "handler")
    assert _sanctioned_budget(arg("timeout"), "wait_for_compaction")
    assert _sanctioned_budget(arg("self.sessions.compact_wait_budget_secs()"), "handler")
    assert _sanctioned_budget(arg("sessions.compact_wait_budget_secs()"), "handler")


def test_manager_budget_follows_its_config():
    """``SessionManager.compact_wait_budget_secs()`` resolves the manager's own
    config, and the automatic coordinator reads the same method, so the two
    cannot drift apart."""
    from kiro_crew.config.loader import KiroCrewConfig
    from kiro_crew.session import SessionManager

    cfg = KiroCrewConfig()
    mgr = SessionManager(cfg)
    assert mgr.compact_wait_budget_secs() == COMPACT_WAIT_TIMEOUT_SECS

    cfg.session.compact_wait_secs = 900.0
    assert mgr.compact_wait_budget_secs() == 900.0
    assert mgr._compaction._deps.compact_wait_timeout_secs() == 900.0


def test_task_runner_overflow_compaction_uses_the_manager_budget():
    """The task runner's context-overflow compaction waits the session
    manager's budget, which holds in a standalone ``kirocrew run`` too: that
    process arms no live-config watcher, so a snapshot-based budget would wait
    the
    built-in budget there whatever the config file says."""
    import kiro_crew.task_executor as task_executor_mod

    tree = ast.parse(inspect.getsource(task_executor_mod))
    budgets = [
        kw.value
        for node in ast.walk(tree)
        if isinstance(node, ast.Call) and _call_name(node.func) == "wait_for_compaction"
        for kw in node.keywords
        if kw.arg == "timeout"
    ]
    assert len(budgets) == 1
    (budget,) = budgets
    assert isinstance(budget, ast.Call) and isinstance(budget.func, ast.Attribute)
    assert budget.func.attr == "compact_wait_budget_secs"
    assert isinstance(budget.func.value, ast.Name) and budget.func.value.id == "sessions"


# ── Post-failure turn budget ──────────────────────────────────
#
# A DIFFERENT budget with a different job: the constant above bounds how long a
# caller waits for compaction to finish, this one bounds how long a turn waits
# for the backend after compaction reported `failed`. It exists because that
# wait is otherwise unbounded in practice — the read loop drains to the
# caller's full prompt ceiling and never released the slot.


def test_post_failure_budget_is_one_constant_for_both_dispatch_paths():
    """The dedicated-process client and the shared-runtime handle must reap an
    abandoned post-compaction turn on the same schedule; a second literal would
    let one path keep hanging after the other was tuned."""
    from kiro_crew.acp import client, session_handle

    assert session_handle._COMPACTION_FAILED_TURN_BUDGET is client._COMPACTION_FAILED_TURN_BUDGET


def test_post_failure_budget_is_bounded_by_the_ordinary_silence_window():
    """A turn the backend has already reported a failure for must not outlive an
    ordinary silent turn — otherwise the hang it fixes just gets shorter."""
    from kiro_crew.acp.client import (
        _COMPACTION_FAILED_TURN_BUDGET,
        _STALE_TURN_TIMEOUT,
    )

    assert 0 < _COMPACTION_FAILED_TURN_BUDGET <= _STALE_TURN_TIMEOUT
