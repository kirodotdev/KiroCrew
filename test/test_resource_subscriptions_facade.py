"""``kiro_crew.mcp_gateway.resource_subscriptions`` keeps the one module's surface and seams.

The module is a mirror facade (:mod:`kiro_crew._facade_mirror`) over two owners:
``resource_subscriptions_budgets`` (the per-call answer budgets) and
``resource_subscriptions_retire`` (the process-wide handle and retirement). Both read
the facade's own names at call time, so a patch through the facade reaches them.
``test/test_facade_mirror_guards.py`` holds every mirror facade to the shared contract;
these tests pin what is particular to this one.
"""

from __future__ import annotations

import ast
import sys
from pathlib import Path
from types import ModuleType

import facade_mirror_scan
import pytest
import source_corpus

from kiro_crew import _facade_mirror
from kiro_crew.mcp_gateway import resource_subscriptions as rs

pytestmark = pytest.mark.xdist_group(name="tree_scan_resource_subscriptions_facade")

#: Checks ``test_facade_shared_checks.py`` runs against this module's helpers.
SHARED_FACADE_CHECKS = ("known_seams",)

FACADE = "kiro_crew.mcp_gateway.resource_subscriptions"
OWNERS = (f"{FACADE}_budgets", f"{FACADE}_retire")

#: Names the test trees are known to patch through the facade, one per kind of seam: an
#: owner's function another owner function calls, an owner's limit, a facade helper an
#: owner reads at call time, the global an owner rebinds, and a class an owner annotates.
_KNOWN_SEAMS = frozenset(
    {"retire_session", "RETIRE_WAIT_SECS", "default_store", "_ACTIVE", "_Budget"}
)

#: Every module-level name the one module bound before the split.
_BASE_MODULE_NAMES = frozenset("""
    Any Awaitable BUDGET_MARGIN_SECS Callable EVENT_MAY_HAVE_CHANGED EVENT_PAUSED
    EVENT_REMOVED EVENT_UPDATED EventSink HANDSHAKE_TIMEOUT_SECS
    HOLDER_CALL_FLOOR_SECS HolderNotLive HolderPaused HolderRecord HolderSpec
    Iterable MAX_SUBSCRIPTIONS_PER_SESSION Optional PAUSED_NOT_SUBSCRIBABLE
    PING_INTERVAL_SECS Path PoolKey Protocol RECONNECT_DELAY_SECS REFUSED_DISABLED
    REFUSED_GATEWAY_UNAVAILABLE REFUSED_NOT_SUBSCRIBABLE REFUSED_PINNED_BACKENDS
    REFUSED_SERVER_NOT_ATTACHED REFUSED_SESSION_LIMIT REFUSED_STORE_UNAVAILABLE
    REFUSED_URI_TOO_LONG RESOURCE_URI_MAX_LEN RETIRE_WAIT_SECS RecordingSink
    ResourceEvent ResourceHolder ResourceSubscriptionService
    ResourceSubscriptionStore STORE_DIR_LEAF STORE_LEAF SUBSCRIBE_BUDGET_SECS
    SUBSCRIBE_COMMIT_RESERVE_SECS SessionClosedCheck Settings StoreUnreadable
    SubscribeRefused SubscriptionMeta TracebackType TypeVar UNSUBSCRIBE_BUDGET_SECS
    _ACTIVE _Budget _LATE_HOLDER_CLOSES _LATE_RETIRES _Listener _MutationResult
    _UNRETIRED _copy_record _finish_mutation _holder_call_within _holder_failure
    _locked_within _log_late_retire_failure _log_safe_exc_info _log_safe_identifier
    _tool_call_error_text _unshared active_service annotations asyncio dataclass
    default_store describe_session inspect logger logging new_subscription_id pin_id
    retire_session retire_sessions_with_stems retire_within_bound set_active_service
    start_active_service stop_active_service uuid
    """.split())


def _index() -> _facade_mirror.MirrorIndex:
    return vars(rs)[_facade_mirror.INDEX_ATTRIBUTE]


def _family() -> list[ModuleType]:
    return [rs, *(sys.modules[name] for name in _index().owners)]


def _corpus_patched_names() -> set[str]:
    spellings = {FACADE: FACADE}
    names: set[str] = set()
    for path in source_corpus.repo_files_named(".py"):
        rel = path.relative_to(source_corpus.repo_root()).as_posix()
        if not (rel == "conftest.py" or rel.startswith("test/")):
            continue
        text = path.read_text(encoding="utf-8", errors="replace")
        if not facade_mirror_scan._mentions(text, spellings):
            continue
        names |= facade_mirror_scan.text_patched_names(text, spellings).get(FACADE, set())
        if facade_mirror_scan.needs_syntax_tree(text, spellings):
            try:
                events = facade_mirror_scan.patch_events(text, spellings)
            except SyntaxError:
                continue
            names |= facade_mirror_scan.names_by_target(events, {FACADE}).get(FACADE, set())
    return names


def test_a_facade_patch_of_every_scanned_name_reaches_every_module_holding_it() -> None:
    family = _family()
    missed: dict[str, list[str]] = {}
    checked = 0
    for name in sorted(_corpus_patched_names()):
        if not hasattr(rs, name):
            continue  # a test that creates the name with raising=False
        holders = [module for module in family if name in vars(module)]
        sentinel = object()
        with pytest.MonkeyPatch.context() as patch:
            patch.setattr(rs, name, sentinel)
            stale = [m.__name__ for m in holders if vars(m).get(name) is not sentinel]
            if getattr(rs, name) is not sentinel:
                stale.append(FACADE)
        checked += 1
        if stale:
            missed[name] = stale
    assert missed == {}
    assert checked >= len(_KNOWN_SEAMS)
    assert _index().divergent() == ()


def test_the_owners_are_the_two_siblings_and_never_import_the_facade_at_run_time() -> None:
    assert isinstance(rs, _facade_mirror.MirroredModule)
    assert _index().owners == OWNERS and _index().excluded == ()
    on_disk = sorted(
        f"kiro_crew.mcp_gateway.{p.stem}"
        for p in Path(rs.__file__).parent.glob("resource_subscriptions_*.py")
    )
    assert on_disk == sorted(OWNERS)
    for owner in OWNERS:
        tree = ast.parse(Path(sys.modules[owner].__file__ or "").read_text(encoding="utf-8"))
        runtime = {
            node.module
            for node in tree.body
            if isinstance(node, ast.ImportFrom) and node.module is not None
        }
        assert FACADE not in runtime and not runtime & set(OWNERS), owner


def test_every_base_name_resolves_and_a_moved_name_is_its_owners_object() -> None:
    assert sorted(name for name in _BASE_MODULE_NAMES if not hasattr(rs, name)) == []
    retire = sys.modules[OWNERS[1]]
    budgets = sys.modules[OWNERS[0]]
    assert rs.retire_session is retire.retire_session
    assert rs.start_active_service is retire.start_active_service
    assert rs._Budget is budgets._Budget and rs._locked_within is budgets._locked_within


def test_the_active_service_is_one_binding_served_live() -> None:
    """``_ACTIVE`` is rebound at run time, so only its owner binds it."""
    assert "_ACTIVE" not in vars(rs)
    marker = object()
    with pytest.MonkeyPatch.context() as patch:
        patch.setattr(rs, "_ACTIVE", marker)
        assert rs.active_service() is marker and rs._ACTIVE is marker
    assert rs.active_service() is None


def test_the_family_logs_through_one_logger_named_for_the_facade() -> None:
    assert rs.logger.name == FACADE
    assert [m.__name__ for m in _family() if vars(m).get("logger") is not rs.logger] == []
