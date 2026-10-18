"""``history_consolidation.py`` keeps its BASE surface while the stages of a pass live in owners.

``HistoryConsolidator`` is defined in :mod:`kiro_crew.history_consolidation`; twenty-three
of its methods have their bodies in the sibling owners ``history_consolidation_span``,
``_lease``, ``_memory``, ``_skills`` and ``_pass``, and the class binds each under its own
name. These
checks pin what callers, ``patch.object`` and the patch corpus rely on:

* every module-level name the one-module file bound still resolves through the facade,
  and a moved name is its owner's own object;
* the class keeps every member with its BASE kind, coroutine flag and signature, and a
  bound member is its owner's function, named and annotated as a method of the class;
* the mirror holds one object per name, so every name the test trees patch through the
  facade reaches every owner that reads it;
* an owner never imports the facade at run time and imports only the owners before it,
  and nothing else in ``src`` imports an owner;
* moved code logs on ``kiro_crew.history``, and every review rule that covers the facade
  covers its owners.

Each structural check has a self-test that shows it can fail.
"""

from __future__ import annotations

import ast
import importlib
import inspect
import re
import traceback
import typing
from pathlib import Path

import pytest

import kiro_crew
from kiro_crew import history_consolidation as facade
from kiro_crew.history_consolidation import HistoryConsolidator

pytestmark = pytest.mark.xdist_group(name="tree_scan_history_consolidation_facade")

_SRC = Path(kiro_crew.__file__).resolve().parent
_ROOT = _SRC.parents[1]
_FACADE = "kiro_crew.history_consolidation"
_PREFIX = _FACADE + "_"
_FACADE_PATH = _SRC / "history_consolidation.py"

#: Every module-level name the one-module file bound (frozen at the split).
_BASE_MODULE_NAMES = frozenset("""
    AUTO_SKILL_MAX_PROCEDURE_CHARS Any AttemptedSpan AutoSkillProvenance Awaitable
    Callable ClaimRefusal DISPLAY_ONLY_ROLES HistoryConsolidator
    LESSON_APPLIES_INSTRUCTION LESSON_APPLIES_ON_TOPIC NamedTuple SKILL_UPDATE
    TYPE_CHECKING ToolApprovalPolicy VERDICT_DUP VERDICT_NEW VERDICT_UPDATE
    _BoundedTable _CONSOLIDATION_BACKOFF_BASE_SECS _CONSOLIDATION_BACKOFF_MAX_SECS
    _CONSOLIDATION_MAX_ATTEMPTS _CONSOLIDATION_MAX_HEAD_SLICES _CONSOLIDATION_META_KEYS
    _CONSOLIDATION_PROMPT_BUDGET_CHARS _CONSOLIDATION_REFUSED
    _CONSOLIDATION_SUBOFFSET_META_KEYS _CONSOLIDATION_THRESHOLD
    _ConsolidationNotDispatched _ConsolidationRefusedSentinel _CountClaim
    _EMBED_BUDGET_SECS_PER_PASS _EmbedBudget _HISTORY_LOGGER _INHERIT_GLOBAL
    _InheritGlobal _LessonDeleteDecision _LESSON_PASSTHROUGH_FIELDS
    _MAX_EPISODIC_PER_CONSOLIDATION
    _MAX_LESSONS_PER_CONSOLIDATION _MAX_SEMANTIC_PER_CONSOLIDATION _PLACEHOLDER_BODIES
    _PersistenceDisabledMidRun _RunCommitState _SEEDED_PER_SWEEP _SEED_MAX_AGE_SECS
    _SEMANTIC_OMISSION_NOTICE _SEMANTIC_PROMPT_CAP_PER_CONSOLIDATION
    _SENSITIVE_TOOL_PATTERNS _SKILL_DETECTION_WINDOW _TOOL_ROLES _bounded_semantic_table
    _consolidation_chunk _consolidation_head_content _count_tool_call_messages
    _facade_metadata_dedupe_verdict _facade_sel _facade_stream_and_collect
    _facade_stream_and_collect_json _fmt_message _frontmatter_value _head_exceeds_budget
    _head_slice_ceiling_reached _is_plausible_memory_file _lesson_extraction_disabled
    _merge_trigger_lists _persistence_disabled _prompt_rows _recency _reroute_lesson_semantic_keys _session_facets
    _session_touched_sensitive _slice_boundary _slice_head_for_budget _strip_code_fence
    _strip_skill_frontmatter _time _withhold_unseen_deletes annotations asyncio
    authored_lesson_applies background_turn contextlib copy datetime
    extracted_lesson_applies frontmatter_value hashlib json live logging math re
    redact_credentials redact_exfiltration_urls run_in_embed_pool scope_is_admissible
    strip_image_refs timezone validate_skill_script
    Iterator _CONSOLIDATION_BUSY _CONSOLIDATION_LEASE_CEILING_SECS
    _CONSOLIDATION_LEASE_RENEW_SECS _LEASE_INCARNATION _LEASE_MONOTONIC _LEASE_PID
    _LEASE_START _LEASE_TOKEN _LeaseLostMidRun _lease_holder_is_live _no_pass_ran os
    platform_compat uuid
    """.split())
#: The members whose bodies moved to an owner.
_MOVED = frozenset("""
    _consolidate _call_llm _run_skill_detection _dedupe_candidate _dedupe_judge
    _merge_skill_update _skill_publication_guard _stage_skill_update _process_auto_skills
    _gated_lesson_scope _lesson_tier _lesson_delete_decision _save_lessons
    _write_structured_memory
    _acquire_consolidation_lease _renew_consolidation_lease _heartbeat_consolidation_lease
    _release_consolidation_lease _lease_is_current _lease_fence _advance_history_marker_now
    _advance_history_marker_fenced _mark_consolidated_fenced
    """.split())
#: name -> (kind, coroutine function, async generator, signature) of every member at BASE.
#: ``_INHERIT_GLOBAL``'s default repr carries an address and its module, so it is
#: normalized to ``<_InheritGlobal>``.
_BASE_MEMBERS: dict[str, tuple[str, bool, bool, str]] = {
    "__init__": (
        "function",
        False,
        False,
        "(self, log: 'ConversationLog', memory: 'MemoryStore', sessions: 'SessionManager | None' = None, lesson_store: 'LessonStore | None' = None, history_idle_secs: 'float' = 10800, vector_store: \"'VectorMemoryStore | None'\" = None, migrated: 'bool' = False, skills_loader: \"'SkillsLoader | None'\" = None, auto_skills_enabled: 'bool' = False, auto_refine_enabled: 'bool' = False, auto_min_tool_calls: 'int' = 5, auto_similarity_threshold: 'float' = 0.85, approval_required: 'bool' = True, max_auto_skills: 'int' = 100, stale_after_days: 'int' = 30, archive_after_days: 'int' = 90, generate_scripts: 'bool' = True, judge_model: 'str' = '') -> 'None'",
    ),
    "_acquire_consolidation_lease": (
        "function",
        False,
        False,
        "(self, key: 'str') -> 'str | None'",
    ),
    "_advance_history_marker": (
        "function",
        True,
        False,
        "(self, key: 'str', span: 'AttemptedSpan', lease: 'str | None' = None) -> 'None'",
    ),
    "_advance_history_marker_fenced": (
        "function",
        False,
        False,
        "(self, key: 'str', span: 'AttemptedSpan', lease: 'str | None') -> 'None'",
    ),
    "_advance_history_marker_now": (
        "function",
        False,
        False,
        "(self, key: 'str', span: 'AttemptedSpan') -> 'None'",
    ),
    "_busy": ("function", False, False, "(self, key: 'str') -> 'bool'"),
    "_call_llm": (
        "function",
        True,
        False,
        "(self, prompt: 'str', *, memory_store: 'str' = '', session_key: 'str' = '') -> 'dict | None'",
    ),
    "_claim_count": ("function", False, False, "(self, key: 'str') -> '_CountClaim | None'"),
    "_consolidate": (
        "function",
        True,
        False,
        "(self, key: 'str', include_history: 'bool' = True) -> '_ConsolidationRefusedSentinel | None'",
    ),
    "_count_off_loop": (
        "function",
        True,
        False,
        "(self, key: 'str', claim: '_CountClaim', *, floor: 'int | None') -> 'tuple[int, int] | None'",
    ),
    "_count_rows": (
        "function",
        False,
        False,
        "(self, key: 'str', floor: 'int | None') -> 'tuple[int, int] | None'",
    ),
    "_dedupe_candidate": (
        "function",
        False,
        False,
        "(self, slug: 'str', description: 'str', triggers: 'str') -> \"'tuple[str, str | None]'\"",
    ),
    "_dedupe_judge": ("function", True, False, "(self, prompt: 'str') -> 'str'"),
    "_forget_key": ("function", False, False, "(self, key: 'str') -> 'None'"),
    "_heartbeat_consolidation_lease": (
        "function",
        True,
        False,
        "(self, key: 'str', token: 'str', on_lost: 'Callable[[], None] | None' = None) -> 'None'",
    ),
    "_gated_lesson_scope": (
        "function",
        False,
        False,
        "(self, item: 'dict') -> 'tuple[str | None, bool]'",
    ),
    "_idle_pass": (
        "function",
        True,
        False,
        "(self, key: 'str', claim: '_CountClaim', now: 'float', stamp: 'tuple[CacheIdentity, int] | None') -> 'None'",
    ),
    "_inside_retry_deadline": (
        "function",
        False,
        False,
        "(self, key: 'str', now: 'float | None' = None) -> 'bool'",
    ),
    "_lesson_delete_decision": (
        "function",
        False,
        False,
        "(self, vector_store: \"'VectorMemoryStore'\", del_key: 'object') -> \"'_LessonDeleteDecision'\"",
    ),
    "_lease_fence": (
        "function",
        False,
        False,
        "(self, key: 'str', lease: 'str | None') -> 'Iterator[None]'",
    ),
    "_lease_is_current": ("function", False, False, "(self, key: 'str', token: 'str') -> 'bool'"),
    "_lesson_tier": ("function", False, False, "(self, item: 'dict') -> 'str | None'"),
    "_logger": ("property", False, False, "(self) -> 'logging.Logger'"),
    "_mark_consolidated_fenced": (
        "function",
        False,
        False,
        "(self, key: 'str', offset: 'int', generation: 'int | None', lease: 'str | None') -> 'None'",
    ),
    "_merge_skill_update": (
        "function",
        True,
        False,
        "(self, live_body: 'str', description: 'str', triggers: 'str', procedure_md: 'str') -> \"'str | None'\"",
    ),
    "_note_environment_failure": (
        "function",
        True,
        False,
        "(self, key: 'str', reason: 'str') -> 'None'",
    ),
    "_note_failed_attempt": (
        "function",
        True,
        False,
        "(self, key: 'str', span: 'AttemptedSpan', reason: 'str', lease: 'str | None' = None) -> 'bool'",
    ),
    "_process_auto_skills": (
        "function",
        False,
        False,
        "(self, result: 'dict', key: 'str', *, guard_publication: 'bool' = False, commit_state: '_RunCommitState | None' = None, refusal: 'ClaimRefusal | None' = None) -> 'None'",
    ),
    "_publication_hold_checked": (
        "function",
        False,
        False,
        "(self, key: 'str', commit_state: '_RunCommitState | None' = None)",
    ),
    "_release_consolidation_lease": (
        "function",
        False,
        False,
        "(self, key: 'str', token: 'str') -> 'None'",
    ),
    "_release_count": ("function", False, False, "(self, claim: '_CountClaim') -> 'None'"),
    "_renew_consolidation_lease": (
        "function",
        False,
        False,
        "(self, key: 'str', token: 'str') -> 'bool | None'",
    ),
    "_run_skill_detection": (
        "function",
        True,
        False,
        "(self, key: 'str', commit_state: '_RunCommitState | None' = None) -> 'None'",
    ),
    "_save_lessons": (
        "function",
        False,
        False,
        "(self, raw: 'object', vector_store: \"'VectorMemoryStore | None | _InheritGlobal'\" = <_InheritGlobal>, lesson_store: \"'LessonStore | None | _InheritGlobal'\" = <_InheritGlobal>, *, facets: \"'MemoryFacets | None'\" = None, key: 'str' = '', commit_state: '_RunCommitState | None' = None) -> 'None'",
    ),
    "_scan_unconsolidated_transcripts": ("function", False, False, "(self) -> 'dict[str, float]'"),
    "_seed_last_activity": ("function", True, False, "(self) -> 'None'"),
    "_session_end_eligibility": (
        "function",
        False,
        False,
        "(self, key: 'str') -> 'tuple[int, int, bool]'",
    ),
    "_session_end_pass": (
        "function",
        True,
        False,
        "(self, key: 'str', claim: '_CountClaim') -> 'None'",
    ),
    "_skill_publication_guard": (
        "function",
        False,
        False,
        "(self, key: 'str', *, enabled: 'bool', commit_state: '_RunCommitState | None' = None)",
    ),
    "_stage_skill_update": (
        "function",
        False,
        False,
        "(self, *, key: 'str', target_key: 'str', description: 'str', triggers: 'str', procedure_md: 'str', scripts: \"'list[dict] | None'\" = None, guard_publication: 'bool' = False, commit_state: '_RunCommitState | None' = None, refusal: 'ClaimRefusal | None' = None) -> 'None'",
    ),
    "_start_counted": (
        "function",
        False,
        False,
        "(self, claim: '_CountClaim', work: 'Awaitable[None]') -> 'None'",
    ),
    "_threshold_pass": (
        "function",
        True,
        False,
        "(self, key: 'str', claim: '_CountClaim') -> 'None'",
    ),
    "_transcript_stamp": (
        "function",
        False,
        False,
        "(self, key: 'str') -> 'tuple[CacheIdentity, int] | None'",
    ),
    "_write_structured_memory": (
        "function",
        False,
        False,
        "(self, result: 'dict', key: 'str', vector_store: \"'VectorMemoryStore | None | _InheritGlobal'\" = <_InheritGlobal>, *, facets: \"'MemoryFacets | None'\" = None, snapshot: 'dict | None' = None, visible_keys: 'frozenset[str] | None' = None, messages: 'list[dict] | None' = None, commit_state: '_RunCommitState | None' = None) -> 'None'",
    ),
    "check_idle_sessions": ("function", False, False, "(self) -> 'None'"),
    "consolidate_now": ("function", True, False, "(self, key: 'str') -> 'bool'"),
    "consolidate_session": ("function", False, False, "(self, key: 'str') -> 'None'"),
    "maybe_consolidate": ("function", False, False, "(self, key: 'str') -> 'None'"),
    "reconfigure": ("function", False, False, "(self, cfg: 'object') -> 'None'"),
    "retry_eligible": (
        "function",
        False,
        False,
        "(self, key: 'str', now: 'float | None' = None, message_count: 'int | None' = None) -> 'bool'",
    ),
}
#: Members whose hints ``typing.get_type_hints`` could not resolve in the one-module file
#: either: they name a class imported only under ``TYPE_CHECKING``.
_HINTS_UNRESOLVED_AT_BASE = frozenset(
    {
        "__init__",
        "_idle_pass",
        "_lesson_delete_decision",
        "_save_lessons",
        "_transcript_stamp",
        "_write_structured_memory",
    }
)
_INHERIT_GLOBAL_REPR = re.compile(r"<[\w.]*\._InheritGlobal object at 0x[0-9a-fA-F]+>")


def _index():
    return vars(facade)["__facade_mirror__"]


def _owner_paths() -> dict[str, Path]:
    return {f"kiro_crew.{p.stem}": p for p in sorted(_SRC.glob("history_consolidation_*.py"))}


def _kind(cls: type, name: str) -> tuple[str, object]:
    raw = inspect.getattr_static(cls, name)
    if isinstance(raw, property):
        return "property", raw.fget
    if isinstance(raw, staticmethod):
        return "staticmethod", raw.__func__
    if isinstance(raw, classmethod):
        return "classmethod", raw.__func__
    if inspect.isfunction(raw):
        return "function", raw
    return "attribute", None


def _member_table(cls: type) -> dict[str, tuple[str, bool, bool, str]]:
    out = {}
    skip = {"__dict__", "__weakref__", "__module__", "__doc__", "__slots__", "__annotations__"}
    for name in vars(cls):
        if name in skip:
            continue
        kind, fn = _kind(cls, name)
        if fn is None:
            continue
        signature = _INHERIT_GLOBAL_REPR.sub("<_InheritGlobal>", str(inspect.signature(fn)))
        out[name] = (
            kind,
            inspect.iscoroutinefunction(fn),
            inspect.isasyncgenfunction(fn),
            signature,
        )
    return out


def _bound_functions(cls: type) -> dict[str, object]:
    """Class members whose function lives in an owner, not in the class body.

    Selected by the owner prefix, not by ``cls.__module__``: ``history.py`` sets the
    class's ``__module__`` to ``kiro_crew.history``, which no member function carries.
    """
    out = {}
    for name in vars(cls):
        _kind_name, fn = _kind(cls, name)
        if fn is not None and getattr(fn, "__module__", "").startswith(_PREFIX):
            out[name] = fn
    return out


def _defined_at_top(source: str) -> set[str]:
    names: set[str] = set()
    for node in ast.parse(source).body:
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
            names.add(node.name)
        elif isinstance(node, ast.Assign):
            names.update(t.id for t in node.targets if isinstance(t, ast.Name))
        elif isinstance(node, ast.AnnAssign) and isinstance(node.target, ast.Name):
            names.add(node.target.id)
    return names


def _class_body_dependencies(source: str) -> list[str]:
    """What a function's source needs from the class body it was defined in."""
    found = []
    for node in ast.walk(ast.parse(source)):
        if isinstance(node, ast.Call) and isinstance(node.func, ast.Name):
            if node.func.id == "super" and not node.args:
                found.append("super()")
        elif isinstance(node, ast.Name) and node.id == "__class__":
            found.append("__class__")
        elif isinstance(node, ast.Attribute) and re.fullmatch(r"__\w+(?<!__)", node.attr):
            found.append(f"mangled {node.attr}")
    return found


def _is_type_checking(test: ast.expr) -> bool:
    return (isinstance(test, ast.Name) and test.id == "TYPE_CHECKING") or (
        isinstance(test, ast.Attribute) and test.attr == "TYPE_CHECKING"
    )


def _runtime_imports(tree: ast.Module) -> list[ast.Import | ast.ImportFrom]:
    """Every import that runs when the module or one of its functions runs.

    Everything but the body of an ``if TYPE_CHECKING:`` block counts, at any depth:
    ``try``/``except``, ``with``, ``match`` and function bodies included.
    """
    typing_only: set[int] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.If) and _is_type_checking(node.test):
            for child in node.body:
                typing_only.update(id(inner) for inner in ast.walk(child))
    return [
        node
        for node in ast.walk(tree)
        if isinstance(node, (ast.Import, ast.ImportFrom)) and id(node) not in typing_only
    ]


def _kiro_crew_modules(node: ast.Import | ast.ImportFrom, package: str) -> set[str]:
    """The top-level ``kiro_crew.<module>`` names *node* imports or imports from.

    A relative import resolves against *package*, the package of the importing module.
    ``from pkg import name`` counts ``pkg.name`` too, since ``name`` may be a submodule;
    each name is cut to its first two parts, which is where the facade and its owners
    live, so ``from kiro_crew.history_consolidation_span import AttemptedSpan`` counts
    as the owner itself.
    """
    if isinstance(node, ast.Import):
        candidates = [alias.name for alias in node.names]
    else:
        if node.level:
            base = package.rsplit(".", node.level - 1)[0] if node.level > 1 else package
            module = f"{base}.{node.module}" if node.module else base
        else:
            module = node.module or ""
        candidates = [module, *(f"{module}.{alias.name}" for alias in node.names)]
    return {".".join(name.split(".")[:2]) for name in candidates if name.startswith("kiro_crew.")}


def _package_of(path: Path) -> str:
    return ".".join(("kiro_crew", *path.relative_to(_SRC).parent.parts))


def _runtime_facade_imports(source: str, package: str = "kiro_crew") -> list[str]:
    """Run-time imports in *source* of the facade module itself, in any form."""
    return [
        module
        for node in _runtime_imports(ast.parse(source))
        for module in sorted(_kiro_crew_modules(node, package))
        if module == _FACADE
    ]


def _owner_imports(source: str, package: str = "kiro_crew") -> list[str]:
    """Owner modules *source* imports, in any form and any block."""
    return sorted(
        {
            module
            for node in ast.walk(ast.parse(source))
            if isinstance(node, (ast.Import, ast.ImportFrom))
            for module in _kiro_crew_modules(node, package)
            if module.startswith(_PREFIX)
        }
    )


# -- module surface -------------------------------------------------------------------


def test_every_base_module_name_still_resolves_through_the_facade() -> None:
    assert sorted(n for n in _BASE_MODULE_NAMES if not hasattr(facade, n)) == []


def test_the_facade_serves_no_name_the_one_module_file_lacked() -> None:
    """A moved method is a class attribute, never a module name: a facade patch of one
    raises, as it did before the split, instead of landing on an owner nobody reads."""
    assert sorted(set(facade._EXPORTS) - _BASE_MODULE_NAMES) == []
    assert sorted(name for name in _MOVED if hasattr(facade, name)) == []
    with pytest.raises(AttributeError):
        getattr(facade, "_consolidate")


def test_a_moved_name_is_the_object_its_owner_holds() -> None:
    checked = 0
    for dotted, path in _owner_paths().items():
        owner = importlib.import_module(dotted)
        for name in sorted(_BASE_MODULE_NAMES & _defined_at_top(path.read_text("utf-8"))):
            checked += 1
            assert getattr(facade, name) is vars(owner)[name], name
    assert checked >= 40, "the inventory check matched almost nothing -- name drift"


def test_the_mirror_holds_one_object_per_name_and_forwards_none() -> None:
    index = _index()
    assert type(facade).__name__ == "MirroredModule"
    assert index.divergent() == ()
    assert index.excluded == ()
    assert not index.forwards
    assert index.forward_reads == "sys_modules_first"
    assert facade._EXPORTS == dict(index.exports)
    assert facade._EXPORTS, "the purge probe needs one served name"


def test_the_facade_serves_only_names_its_owners_define() -> None:
    """Every BASE import is bound on the facade itself, so nothing is served from a
    module outside the family and mypy sees each served name in the manifest."""
    defined = {
        dotted: _defined_at_top(path.read_text("utf-8")) for dotted, path in _owner_paths().items()
    }
    strays = sorted(name for name, owner in _index().exports.items() if name not in defined[owner])
    assert strays == []


def test_the_owners_are_every_sibling_in_layer_order() -> None:
    owners = list(_index().owners)
    assert sorted(owners) == sorted(_owner_paths())
    late = {
        owner: sorted(
            set(_owner_imports(_owner_paths()[owner].read_text("utf-8"))) - set(owners[:i])
        )
        for i, owner in enumerate(owners)
    }
    assert late == {owner: [] for owner in owners}


def test_a_moved_exception_keeps_the_facade_module_and_other_moved_classes_name_their_owner() -> (
    None
):
    """An exception reaches tracebacks and logs by its dotted name, so it keeps the one the
    one-module file gave it. The other moved classes appear in no traceback, log or
    user-visible text, and name the owner that defines them; ``history.py`` renames
    ``AttemptedSpan`` itself."""
    for name in ("_ConsolidationNotDispatched", "_PersistenceDisabledMidRun"):
        exc = getattr(facade, name)
        assert exc.__module__ == _FACADE, name
        line = traceback.format_exception_only(exc, exc("x"))[-1]
        assert line.startswith(f"{_FACADE}.{name}: x"), line
    owned = {
        "_BoundedTable": "memory",
        "_EmbedBudget": "memory",
        "_InheritGlobal": "memory",
        "_LessonDeleteDecision": "memory",
        "_ConsolidationRefusedSentinel": "span",
        "_RunCommitState": "span",
    }
    for name, owner in owned.items():
        assert getattr(facade, name).__module__ == f"{_PREFIX}{owner}", name
    assert facade.AttemptedSpan.__module__ == "kiro_crew.history"


# -- the HistoryConsolidator class -----------------------------------------------------


def test_the_class_keeps_every_base_member_with_its_kind_and_signature() -> None:
    assert _member_table(HistoryConsolidator) == _BASE_MEMBERS


def test_the_inventory_check_sees_a_changed_signature(monkeypatch: pytest.MonkeyPatch) -> None:
    async def _call_llm(self, prompt, extra=0):  # pragma: no cover - never called
        return None

    monkeypatch.setattr(HistoryConsolidator, "_call_llm", _call_llm)
    assert _member_table(HistoryConsolidator)["_call_llm"] != _BASE_MEMBERS["_call_llm"]


def test_each_bound_member_is_its_owners_function() -> None:
    bound = _bound_functions(HistoryConsolidator)
    assert set(bound) == _MOVED
    for name, fn in bound.items():
        owner = importlib.import_module(fn.__module__)
        assert inspect.unwrap(vars(owner)[name]) is inspect.unwrap(fn), name


def test_a_bound_member_reads_as_a_method_of_the_class() -> None:
    for name, fn in _bound_functions(HistoryConsolidator).items():
        while fn is not None:
            assert fn.__qualname__ == f"HistoryConsolidator.{name}", name
            assert "self" not in fn.__annotations__, name
            fn = getattr(fn, "__wrapped__", None)


def test_type_hints_resolve_as_before() -> None:
    unresolved = set()
    for name, member in vars(HistoryConsolidator).items():
        fn = member.fget if isinstance(member, property) else getattr(member, "__func__", member)
        if not inspect.isfunction(fn):
            continue
        try:
            typing.get_type_hints(inspect.unwrap(fn))
        except NameError:
            unresolved.add(name)
    assert unresolved == _HINTS_UNRESOLVED_AT_BASE


def test_no_bound_member_depends_on_the_class_body_it_left() -> None:
    offenders = {
        name: deps
        for name, fn in _bound_functions(HistoryConsolidator).items()
        if (deps := _class_body_dependencies(inspect.getsource(inspect.unwrap(fn))))
    }
    assert offenders == {}


def test_the_class_body_check_can_fail() -> None:
    source = "def f(self):\n    super().f()\n    print(__class__)\n    return self.__hidden\n"
    assert sorted(_class_body_dependencies(source)) == ["__class__", "mangled __hidden", "super()"]


# -- patch reach -----------------------------------------------------------------------

_PATCH_TREES = (_ROOT / "test", _SRC / "apps" / "builtins")
_STRING_TARGET = re.compile(r"""["']kiro_crew\.history_consolidation\.([A-Za-z_]\w*)""")
_ALIASES = (
    re.compile(r"from kiro_crew import [^\n]*?\bhistory_consolidation\b(?: as (\w+))?"),
    re.compile(r"import kiro_crew\.history_consolidation as (\w+)"),
)


def _patched_names(text: str) -> set[str]:
    """Names *text* patches on the facade, by dotted string or on a module alias."""
    names = set(_STRING_TARGET.findall(text))
    aliases = set()
    for pattern in _ALIASES:
        for match in pattern.finditer(text):
            aliases.add(match.group(1) or "history_consolidation")
    for alias in aliases:
        call = re.compile(
            r"""(?:setattr|patch\.object)\(\s*""" + re.escape(alias) + r"""\s*,\s*["'](\w+)["']"""
        )
        names.update(call.findall(text))
    return names


def _corpus_patched_names() -> set[str]:
    found: set[str] = set()
    for tree in _PATCH_TREES:
        for path in tree.rglob("*.py"):
            text = path.read_text(encoding="utf-8", errors="replace")
            if "history_consolidation" in text:
                found |= _patched_names(text)
    return found


def test_every_name_the_tests_patch_through_the_facade_reaches_every_holder() -> None:
    names = _corpus_patched_names()
    assert {
        "_CONSOLIDATION_PROMPT_BUDGET_CHARS",
        "_EMBED_BUDGET_SECS_PER_PASS",
        "_facade_stream_and_collect",
        "_facade_stream_and_collect_json",
        "_persistence_disabled",
        "background_turn",
    } <= names
    index = _index()
    reached = 0
    for name in sorted(names):
        assert name not in index.excluded, name
        holders = [importlib.import_module(h) for h in index.holders.get(name, ())]
        if not holders:
            continue  # bound by the facade alone: an ordinary module attribute
        reached += 1
        originals = [vars(h)[name] for h in holders]
        sentinel = object()
        with pytest.MonkeyPatch.context() as mp:
            mp.setattr(facade, name, sentinel)
            assert [vars(h)[name] for h in holders] == [sentinel] * len(holders), name
            if name in vars(facade):
                assert vars(facade)[name] is sentinel, name
        assert [vars(h)[name] for h in holders] == originals, name
    assert reached >= 6


def test_a_name_read_on_both_sides_of_the_split_is_patched_on_both() -> None:
    """``_persistence_disabled`` is read by the class (``_publication_hold_checked``) and by
    the moved ``_consolidate``: one patch through the facade must reach both readers."""
    holders = set(_index().holders["_persistence_disabled"])
    assert {
        "kiro_crew.history_consolidation_span",
        "kiro_crew.history_consolidation_pass",
    } <= holders
    assert "_persistence_disabled" in vars(facade)


def test_the_patch_target_scan_can_fail() -> None:
    text = (
        "from kiro_crew import history_consolidation as hc\n"
        "monkeypatch.setattr(hc, '_persistence_disabled', f)\n"
        "patch('kiro_crew.history_consolidation._facade_sel')\n"
        "from kiro_crew import history_consolidation_pass\n"
    )
    assert _patched_names(text) == {"_persistence_disabled", "_facade_sel"}
    assert _patched_names("monkeypatch.setattr(other, 'x', 1)\n") == set()


# -- import direction ------------------------------------------------------------------


def test_no_owner_imports_the_facade_at_run_time() -> None:
    offenders = {
        dotted: found
        for dotted, path in _owner_paths().items()
        if (found := _runtime_facade_imports(path.read_text("utf-8")))
    }
    assert offenders == {}


def test_the_facade_import_check_can_fail() -> None:
    source = (
        "from typing import TYPE_CHECKING\n"
        "if TYPE_CHECKING:\n"
        "    from kiro_crew.history_consolidation import HistoryConsolidator\n"
        "from kiro_crew import history_consolidation\n"
        "import kiro_crew.history_consolidation\n"
        "def f():\n"
        "    from kiro_crew.history_consolidation import AttemptedSpan\n"
        "try:\n"
        "    pass\n"
        "except ImportError:\n"
        "    from . import history_consolidation as hc\n"
        "from kiro_crew import history_consolidation_span\n"
    )
    assert _runtime_facade_imports(source) == [_FACADE] * 4


def test_only_the_facade_imports_an_owner() -> None:
    allowed = {_FACADE_PATH, *_owner_paths().values()}
    offenders = {
        str(path.relative_to(_SRC)): found
        for path in sorted(_SRC.rglob("*.py"))
        if path not in allowed
        and "history_consolidation_" in (text := path.read_text(encoding="utf-8", errors="replace"))
        and (found := _owner_imports(text, _package_of(path)))
    }
    assert offenders == {}


def test_the_owner_import_check_can_fail() -> None:
    source = (
        "from kiro_crew.history_consolidation_span import AttemptedSpan\n"
        "from kiro_crew import history_consolidation_pass as _pass\n"
        "import kiro_crew.history_consolidation_skills\n"
        "from .history_consolidation_memory import _save_lessons\n"
    )
    assert _owner_imports(source) == [
        "kiro_crew.history_consolidation_memory",
        "kiro_crew.history_consolidation_pass",
        "kiro_crew.history_consolidation_skills",
        "kiro_crew.history_consolidation_span",
    ]
    relative = "from ..history_consolidation_span import _fmt_message\n"
    assert _owner_imports(relative, package="kiro_crew.dashboard") == [
        "kiro_crew.history_consolidation_span"
    ]
    assert _owner_imports("from kiro_crew import history_consolidation\n") == []


# -- logging and review reach ------------------------------------------------------------


def test_moved_code_logs_on_the_history_logger() -> None:
    """The class keeps the pre-split ``kiro_crew.history`` logger category, and no owner
    builds a logger of its own that a moved body could reach instead."""
    assert facade._HISTORY_LOGGER.name == "kiro_crew.history"
    assert HistoryConsolidator.__new__(HistoryConsolidator)._logger is facade._HISTORY_LOGGER
    for dotted in _index().owners:
        owner = importlib.import_module(dotted)
        loggers = sorted(n for n, v in vars(owner).items() if type(v).__name__ == "Logger")
        assert loggers == [], dotted


def _globstar(pattern: str) -> re.Pattern[str]:
    """An AUTOSDE pattern as a regex: ``**/`` spans zero or more directories, ``*``
    and ``?`` stay inside one path segment."""
    out, i = "", 0
    while i < len(pattern):
        if pattern.startswith("**/", i):
            out, i = out + "(?:[^/]+/)*", i + 3
        elif pattern.startswith("**", i):
            out, i = out + ".*", i + 2
        elif pattern[i] in "*?":
            out, i = out + ("[^/]*" if pattern[i] == "*" else "[^/]"), i + 1
        else:
            out, i = out + re.escape(pattern[i]), i + 1
    return re.compile(out + r"\Z")


def _rules_missing_owners(rules: list[dict]) -> tuple[set[str], dict[str, list[str]]]:
    """``(ids of rules matching the facade, {id: owner files those rules miss})``."""
    facade_path = "src/kiro_crew/history_consolidation.py"
    owners = [path.relative_to(_ROOT).as_posix() for path in _owner_paths().values()]
    matched: set[str] = set()
    missing: dict[str, list[str]] = {}
    for rule in rules:
        patterns = [_globstar(p) for p in rule.get("file-patterns", [])]
        if not any(p.match(facade_path) for p in patterns):
            continue
        matched.add(rule["id"])
        gaps = [o for o in owners if not any(p.match(o) for p in patterns)]
        if gaps:
            missing[rule["id"]] = gaps
    return matched, missing


def test_every_review_rule_on_the_facade_also_covers_its_owners() -> None:
    """A rule that reviews the facade reviews the code bound into it: an owner outside
    its patterns would take moved code out of that rule's sight."""
    import yaml

    rules = [
        rule
        for name in ("AUTOSDE.yaml", "website/AUTOSDE.yaml")
        for rule in yaml.safe_load((_ROOT / name).read_text(encoding="utf-8"))["custom-rules"]
    ]
    matched, missing = _rules_missing_owners(rules)
    assert "memory-store-seam" in matched
    assert missing == {}
    narrowed = [
        (
            {
                **rule,
                "file-patterns": [
                    p for p in rule["file-patterns"] if "history_consolidation_" not in p
                ],
            }
            if rule["id"] == "memory-store-seam"
            else rule
        )
        for rule in rules
    ]
    assert _rules_missing_owners(narrowed)[1].keys() == {"memory-store-seam"}
