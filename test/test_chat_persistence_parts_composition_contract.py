"""The composition contract of ``kiro_crew.dashboard.chat_persistence_parts``.

The dashboard slot save and restore orchestration lives in the parts of this package and
runs on the globals of ``kiro_crew.dashboard.chat_persistence``, which composes them with
``kiro_crew._facade_compose.compose``. ``test_chat_persistence_composition_contract.py``
pins the facade's surface, its ``slot_persistence`` rule owners and the bytes a save
writes; this file pins what the composition must keep true:

* every function and method a part defines runs on the facade's globals and reports the
  facade as its module, with the defaults it had, and every global it reads is bound on
  the facade (a bytecode sweep with a planted self-test);
* a composed name is its part's object, nothing but the facade imports a part, a part
  imports project names only for the type checker, and no part binds the facade's
  process state;
* async functions sit only in parts the off-loop read gate scans, and only the save
  transaction dispatches a save;
* a patch on the facade reaches each part, and the facade imports nothing beyond its
  frozen inventory, which stands in for the F401 check ``setup.cfg`` switches off for it.
"""

from __future__ import annotations

import ast
import asyncio
import builtins
import dis
import importlib
import types
from pathlib import Path
from types import SimpleNamespace

from kiro_crew.dashboard import chat_persistence as cp

FACADE = "kiro_crew.dashboard.chat_persistence"
PARTS_PACKAGE = "kiro_crew.dashboard.chat_persistence_parts"
PARTS_DIR = Path(cp.__file__).resolve().with_name("chat_persistence_parts")
#: The parts the facade composes, in the order it passes them.
PARTS = ("save_transaction", "restore_drivers", "slot_builders", "member_store")

#: Every name a part defines, and the part that holds it.
_COMPOSED: dict[str, str] = {
    "_member_private_selection": "member_store",
    "_member_restore_identity": "member_store",
    "_pin_private_agent_assignment": "member_store",
    "member_store_ownership_holds": "member_store",
    "pin_private_agent_store": "member_store",
    "release_prewarmed_session": "member_store",
    "_bounded_text": "restore_drivers",
    "_carry_unread_slot_keys": "restore_drivers",
    "_display_tail": "restore_drivers",
    "_kept_past_budget": "restore_drivers",
    "_may_restore_as_row": "restore_drivers",
    "_open_slot_restore_plan": "restore_drivers",
    "_over_restore_budget": "restore_drivers",
    "_PERSON_ORIGINS": "restore_drivers",
    "_prefetch_open_slot_inputs": "restore_drivers",
    "_record_unrestored_open_tabs": "restore_drivers",
    "_recover_mcp_app_claims_async": "restore_drivers",
    "_report_restore_budget": "restore_drivers",
    "_report_restore_with_no_seed": "restore_drivers",
    "_restore_open_slots_steps": "restore_drivers",
    "_restore_recent_sessions_steps": "restore_drivers",
    "_short_scalar": "restore_drivers",
    "_TAIL_CLS_BOUND": "restore_drivers",
    "_TAIL_META_KEYS": "restore_drivers",
    "_TAIL_SCALAR_BOUND": "restore_drivers",
    "_window_can_wait": "restore_drivers",
    "EAGER_WINDOW_RESTORES": "restore_drivers",
    "LAZY_TAIL_ROWS": "restore_drivers",
    "restore_open_slots": "restore_drivers",
    "restore_open_slots_async": "restore_drivers",
    "restore_recent_sessions": "restore_drivers",
    "restore_recent_sessions_async": "restore_drivers",
    "TAIL_TEXT_BOUND": "restore_drivers",
    "_save_slot_to_history": "save_transaction",
    "save_all_slots_to_history": "save_transaction",
    "save_slot_off_loop": "save_transaction",
    "wait_for_unwinding_turns": "save_transaction",
    "_apply_recent_session": "slot_builders",
    "_apply_restored_open_slot": "slot_builders",
    "_IDENTITY_UNRESOLVED": "slot_builders",
    "_load_rehydrated_window": "slot_builders",
    "_PendingWindow": "slot_builders",
    "_prefetch_recent_session": "slot_builders",
    "_prefetch_rehydrate_inputs": "slot_builders",
    "_rehydrate_slot_from_history": "slot_builders",
    "rehydrate_slot_from_history_async": "slot_builders",
}

#: The facade's process state: fixtures reset it there, and ``global`` writes land there.
_PROCESS_STATE = frozenset(
    {
        "_entry_cache",
        "_entry_cache_lock",
        "_entry_cache_bytes",
        "_entry_cache_bounds_cached",
        "_entry_cache_bounds_read_warned",
        "_entry_cache_config_sub",
        "_ENTRY_MAX_CACHEABLE_BYTES",
        "_reasoning_effort_values",
        "_reasoning_effort_ordered",
        "_reasoning_effort_marked",
    }
)

#: What the conftest isolation fixtures reset on the facade, as they did before the split.
_CONFTEST_RESETS = frozenset(
    {
        "_entry_cache",
        "_entry_cache_bounds_cached",
        "_entry_cache_bounds_read_warned",
        "_entry_cache_bytes",
        "_reasoning_effort_ordered",
        "_reasoning_effort_values",
    }
)

#: A part's runtime project import: what a module-level statement evaluates when it loads.
_RUNTIME_PROJECT_IMPORTS = {("restore_drivers", "kiro_crew.dashboard.state", "SlotOrigin")}

#: The saves only the save transaction (and the facade) may start.
_SAVERS = frozenset({"save_slot_off_loop", "_save_slot_to_history", "save_all_slots_to_history"})

#: The rule-owner modules the facade binds by alias; parts read them through its globals.
_OWNER_ALIASES = {
    "_metadata_codec": "kiro_crew.dashboard.slot_persistence.metadata_codec",
    "_metadata_line": "kiro_crew.dashboard.slot_persistence.metadata_line",
    "_transcript_merge": "kiro_crew.dashboard.slot_persistence.transcript_merge",
    "_write_guards": "kiro_crew.dashboard.slot_persistence.write_guards",
}

#: Every name the facade's import statements bound before the parts were composed.
_BASE_IMPORTS = frozenset("""
    ARTIFACT_SLUG_RE ATTACHMENT_LIST_MAX_ITEMS ATTACHMENT_PATH_MAX_LEN AUTOCOMPACT_PCT_MAX
    AUTOCOMPACT_PCT_MIN Any CHAT_ENTRY_CACHE_BYTES_DEFAULT CHAT_ENTRY_CACHE_ENTRIES_DEFAULT
    COLOR_HEX_RE Callable ConversationLog DashboardState EFFORT_LEVELS EFFORT_VALUES
    EXECUTION_CONTEXT_KEY HUMAN_TURN_META_KEY ImageBudget Iterable Iterator KiroCrewConfig
    MEMORY_MODES METADATA_LINE_CORRUPT Mapping OrderedDict Path RESTORE_SLOT_BUDGET
    ROWS_ONLY_DEFERRED_META_KEYS ROWS_ONLY_OWNED_META_KEYS SLOT_OWNED_META_KEYS
    STRICTEST_MEMORY_MODE Sequence SlotOrigin UnknownMemoryStore _ChatSlot
    _FLUSH_SNAPSHOT_RETRIES _LOCAL_TURN_PROMPT_MAX_ATTACHMENTS _LOCAL_TURN_PROMPT_MAX_BYTES
    _LOCAL_TURN_PROMPT_MAX_FIELD_CHARS _LOCAL_TURN_PROMPT_META_KEYS _LOCAL_TURN_PROMPT_ROLES
    _MAX_DISMISSED_SOURCE_LINKS _META_LAST_USER_AT _PENDING_MEMORY_MODE_LOCK
    _RESTART_INTERRUPTION_KIND _RESTART_INTERRUPTION_MSG _RETIRED_MODES _TITLE_ORIGINS
    _TRANSIENT_ROLES _approx_window_payload_bytes _archive_dropped_lines _archive_lines
    _attach_variants _build_kiro_model_map _build_message_entry_uncached
    _capped_dismissed_line _deletion_during_read _diff_dropped_message_lines
    _display_verdicts _foreign_tail_ts _frozen_prefix_and_foreign_appends
    _interleave_foreign_lines _is_app_owned_channel_row _keep_owed_after_refusal
    _latest_stamp _latest_turn_was_deliberately_stopped _line_is_this_slots
    _load_restore_cfg _local_turn_generation _local_turn_prompt _metadata_codec
    _metadata_line _newest_human_turn_ts _normalize_model _normalize_slot_key
    _note_authorized_elsewhere _queue_snapshot_is_stale _read_mcp_app_claims
    _read_open_slots_keys _read_open_slots_snapshot _rebase_rehydrated_refresh_mark
    _recent_session_slot_name _reconcile_local_turn_marker _reconcile_mcp_app_claims
    _record_pending_memory_mode _recover_mcp_app_claims _redact_meta_for_role
    _rehydrate_slot_title _rehydrate_title_low_signal _rehydrate_title_origin
    _rehydrate_title_refresh_mark _rehydrated_refresh_mark _restore_dismissed_source_links
    _restore_model_fields _restored_agent_name _restored_mode _sanitize_open_slot_key
    _stable_durable_queue _sync_dashboard_slots _tighten_carried_execution _transcript_merge
    _validate_autocompact_pct _validate_turn_timeout_secs _window_holds_row _write_guards
    agent_model_map annotations apply_pending_slot_memory_mode asyncio atomic_write
    canonical_memory_mode carry_provenance carry_unowned_metadata chain
    committed_filtered_note_ids config_dir crew_bound_on_disk deque
    drop_committed_restored_notes drop_records_without_placeholders durable_row_count
    effective_session_key file_lock hashlib is_channel_session_key is_stop_event_row
    is_turn_interrupted islice json kiro_agents_dir_path latest_transcript_ts
    live_loop_slot_keys local_turn_prompt_within_bounds logging mcp_apps_render
    model_registry named_store_or_empty notify_left_in_history os pending_slot_memory_mode
    persist_inline_images queue_persist_signature re read_session_execution
    redact_credentials redact_display_content redact_exfiltration_urls
    redact_log_via_context register_guarded_history_write remember_unknown_row_fields
    row_mid same_text_modulo_images sanitize_restored_deferred_notes sanitize_restored_queue
    sel serialize_deferred_notes session_agent_selection_name session_key_for
    session_transcript_remains session_was_deleted slot_closed_since slot_history_key
    slot_transcript_key stored_loop_slot_keys stricter_memory_mode threading time
    transcript_sort_key union_deferred_notes update_metadata_off_loop
    with_bounded_redaction_records
    """.split())

#: The names the composition's own glue imports: the shared helper and the package.
_GLUE_IMPORTS = frozenset({"_facade_compose", "_parts"})


def _part(stem: str) -> types.ModuleType:
    return importlib.import_module(f"{PARTS_PACKAGE}.{stem}")


def _tree(path: Path) -> ast.Module:
    return ast.parse(path.read_text(encoding="utf-8"))


def _is_type_checking(node: ast.stmt) -> bool:
    return isinstance(node, ast.If) and ast.unparse(node.test) == "TYPE_CHECKING"


def _defined(tree: ast.Module) -> set[str]:
    """Names a module defines at its top level: a def, a class or an assignment."""
    names: set[str] = set()
    for node in tree.body:
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
            names.add(node.name)
        elif isinstance(node, (ast.Assign, ast.AnnAssign)):
            targets = node.targets if isinstance(node, ast.Assign) else [node.target]
            names |= {n.id for t in targets for n in ast.walk(t) if isinstance(n, ast.Name)}
    return names


def composed_functions(module: types.ModuleType) -> dict[str, types.FunctionType]:
    """``qualname -> function`` for every function *module*'s file defines, methods included."""
    found: dict[str, types.FunctionType] = {}
    for value in vars(module).values():
        if isinstance(value, types.FunctionType) and value.__code__.co_filename == module.__file__:
            found[value.__qualname__] = value
        elif isinstance(value, type) and value.__module__ == module.__name__:
            for member in vars(value).values():
                fn = member.__func__ if isinstance(member, (staticmethod, classmethod)) else member
                if (
                    isinstance(fn, types.FunctionType)
                    and fn.__code__.co_filename == module.__file__
                ):
                    found[fn.__qualname__] = fn
    return found


def global_reads(code: types.CodeType) -> set[str]:
    """Every global *code* and the code nested in it loads, stores or deletes."""
    names: set[str] = set()
    for instruction in dis.get_instructions(code):
        if instruction.opname in {"LOAD_GLOBAL", "STORE_GLOBAL", "DELETE_GLOBAL"}:
            names.add(instruction.argval)
    for const in code.co_consts:
        if isinstance(const, types.CodeType):
            names |= global_reads(const)
    return names


def unbound_reads(functions: dict[str, types.FunctionType]) -> dict[str, list[str]]:
    """``qualname -> globals it reads that neither its globals nor the builtins bind``."""
    missing: dict[str, list[str]] = {}
    for qualname, fn in sorted(functions.items()):
        names = sorted(
            n
            for n in global_reads(fn.__code__)
            if n not in fn.__globals__ and n not in vars(builtins)
        )
        if names:
            missing[qualname] = names
    return missing


def read_names(*sources: str) -> set[str]:
    """Every name the family's code reads: a bare load, or ``cp.<name>`` in a rule owner."""
    found: set[str] = set()
    for source in sources:
        for node in ast.walk(ast.parse(source)):
            if isinstance(node, ast.Name) and isinstance(node.ctx, ast.Load):
                found.add(node.id)
            elif (
                isinstance(node, ast.Attribute)
                and isinstance(node.value, ast.Name)
                and node.value.id == "cp"
            ):
                found.add(node.attr)
    return found


def unexpected_imports(
    source: str, part_names: dict[str, set[str]], family_reads: set[str]
) -> list[str]:
    """Names *source* binds by an import beyond the frozen inventory.

    The inventory is the BASE imports, the glue's own, and from each part only names
    that part defines. An import beyond them is accounted for when the family's code --
    the facade, its parts, or a ``slot_persistence`` owner through ``cp.<name>`` -- reads
    the name, so a later change can import what it uses here.
    """
    found: list[str] = []
    for node in ast.parse(source).body:
        if isinstance(node, ast.Import):
            bound = [(alias.asname or alias.name.split(".")[0], None) for alias in node.names]
        elif isinstance(node, ast.ImportFrom):
            bound = [(alias.asname or alias.name, node.module) for alias in node.names]
        else:
            continue
        for name, module in bound:
            if module in part_names:
                if name not in part_names[module]:
                    found.append(f"{module}.{name}")
            elif name not in _BASE_IMPORTS | _GLUE_IMPORTS | family_reads:
                found.append(name)
    return found


def _family_sources() -> list[str]:
    owners = Path(cp.__file__).resolve().with_name("slot_persistence")
    paths = [Path(cp.__file__), *sorted(PARTS_DIR.glob("*.py")), *sorted(owners.glob("*.py"))]
    return [path.read_text(encoding="utf-8") for path in paths]


def _part_names() -> dict[str, set[str]]:
    return {f"{PARTS_PACKAGE}.{stem}": _defined(_tree(PARTS_DIR / f"{stem}.py")) for stem in PARTS}


class TestPackage:
    def test_the_package_holds_exactly_the_composed_parts(self) -> None:
        assert {path.stem for path in PARTS_DIR.glob("*.py")} == {*PARTS, "__init__"}

    def test_the_package_init_holds_only_its_docstring(self) -> None:
        body = _tree(PARTS_DIR / "__init__.py").body
        assert len(body) == 1 and isinstance(body[0], ast.Expr)
        assert isinstance(body[0].value, ast.Constant) and isinstance(body[0].value.value, str)

    def test_the_facade_composes_every_part_through_the_shared_helper(self) -> None:
        tree = _tree(Path(cp.__file__))
        helper = [
            node
            for node in tree.body
            if isinstance(node, ast.ImportFrom)
            and node.module == "kiro_crew._facade_compose"
            and [(a.name, a.asname) for a in node.names] == [("compose", "_facade_compose")]
        ]
        calls = [
            node.value
            for node in tree.body
            if isinstance(node, ast.Expr)
            and isinstance(node.value, ast.Call)
            and ast.unparse(node.value.func) == "_facade_compose"
        ]
        assert len(helper) == 1 and len(calls) == 1
        args = calls[0].args
        assert ast.unparse(args[0]) == "globals()" and len(args) == 2 and not calls[0].keywords
        assert [ast.unparse(part) for part in args[1].elts] == [f"_parts.{p}" for p in PARTS]

    def test_the_parts_are_composed_before_the_rule_owners_load(self) -> None:
        """The composition contract keeps the ``slot_persistence`` imports last."""
        body = _tree(Path(cp.__file__)).body
        compose_at = next(
            i
            for i, node in enumerate(body)
            if isinstance(node, ast.Expr) and "_facade_compose(" in ast.unparse(node)
        )
        owners_at = next(
            i
            for i, node in enumerate(body)
            if isinstance(node, ast.ImportFrom)
            and (node.module or "").startswith("kiro_crew.dashboard.slot_persistence")
        )
        assert compose_at < owners_at


class TestComposedSurface:
    def test_every_part_defined_name_is_listed_and_is_its_parts_object(self) -> None:
        defined = {
            name: stem for stem in PARTS for name in _defined(_tree(PARTS_DIR / f"{stem}.py"))
        }
        assert defined == _COMPOSED
        wrong = [
            name
            for name, stem in sorted(_COMPOSED.items())
            if getattr(cp, name) is not vars(_part(stem))[name]
        ]
        assert wrong == []

    def test_every_composed_function_and_method_runs_on_the_facade(self) -> None:
        checked = 0
        for stem in PARTS:
            for qualname, fn in composed_functions(_part(stem)).items():
                assert fn.__globals__ is vars(cp), f"{stem}.{qualname}"
                assert fn.__module__ == FACADE, f"{stem}.{qualname}"
                checked += 1
        methods = composed_functions(_part("slot_builders"))
        assert {"_PendingWindow.__init__", "_PendingWindow.read", "_PendingWindow.__call__"} <= set(
            methods
        )
        assert cp._PendingWindow.read is methods["_PendingWindow.read"]
        assert checked >= len([v for v in _COMPOSED if callable(getattr(cp, v))])

    def test_a_composed_function_keeps_its_signature_defaults_and_no_closure(self) -> None:
        """Every default is the value its expression names on the facade -- the very object
        a body compares with ``is`` -- and no top-level function closes over a cell."""
        for stem in PARTS:
            for node in _tree(PARTS_DIR / f"{stem}.py").body:
                if not isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
                    continue
                assert node.decorator_list == [], f"{stem}.{node.name}"
                fn = getattr(cp, node.name)
                assert fn.__closure__ is None, f"{stem}.{node.name}"
                positional = [(d, v) for d, v in zip(node.args.defaults, fn.__defaults__ or ())]
                keyword = [
                    (d, fn.__kwdefaults__[a.arg])
                    for a, d in zip(node.args.kwonlyargs, node.args.kw_defaults)
                    if d is not None
                ]
                for expr, value in [*positional, *keyword]:
                    if isinstance(expr, ast.Name):
                        assert value is vars(cp)[expr.id], f"{stem}.{node.name}: {expr.id}"
                    else:
                        assert value == ast.literal_eval(expr), f"{stem}.{node.name}"

    def test_the_identity_sentinel_default_is_the_facades_object(self) -> None:
        sentinel = cp._IDENTITY_UNRESOLVED
        assert cp._apply_restored_open_slot.__kwdefaults__["member_identity"] is sentinel
        assert (
            cp._rehydrate_slot_from_history.__kwdefaults__["_prefetched_member_identity"]
            is sentinel
        )
        assert cp._apply_recent_session.__kwdefaults__["member_identity"] is sentinel


class TestBytecodeSweep:
    def test_every_global_a_composed_function_reads_is_bound_on_the_facade(self) -> None:
        functions = {
            f"{stem}.{qualname}": fn
            for stem in PARTS
            for qualname, fn in composed_functions(_part(stem)).items()
        }
        assert len(functions) >= 40
        assert unbound_reads(functions) == {}

    def test_the_sweep_reports_a_planted_unbound_read_in_a_function_and_a_method(self) -> None:
        planted = types.ModuleType("planted_part")
        planted.__file__ = "<planted_part>"
        code = compile(
            "def reads():\n"
            "    def nested():\n"
            "        return planted_name_nobody_binds\n"
            "    return nested\n"
            "def method(self):\n"
            "    return another_planted_name\n",
            planted.__file__,
            "exec",
        )
        reads, method = (
            types.FunctionType(const, vars(cp))
            for const in code.co_consts
            if isinstance(const, types.CodeType)
        )
        method.__qualname__ = "Planted.method"
        planted.reads = reads
        planted.Planted = type("Planted", (), {"method": method, "__module__": "planted_part"})
        found = composed_functions(planted)
        assert set(found) == {"reads", "Planted.method"}
        assert unbound_reads(found) == {
            "Planted.method": ["another_planted_name"],
            "reads": ["planted_name_nobody_binds"],
        }

    def test_the_rule_owner_aliases_stay_bound_and_are_what_the_parts_read(self) -> None:
        reads = set().union(
            *(
                global_reads(fn.__code__)
                for stem in PARTS
                for fn in composed_functions(_part(stem)).values()
            )
        )
        for alias, module in _OWNER_ALIASES.items():
            assert vars(cp)[alias] is importlib.import_module(module), alias
        assert {"_metadata_line", "_transcript_merge", "_write_guards"} <= reads


class TestPlacement:
    def test_nothing_but_the_facade_imports_a_part(self) -> None:
        root = Path(cp.__file__).resolve().parents[1]
        importers = []
        for path in sorted(root.rglob("*.py")):
            text = path.read_text(encoding="utf-8", errors="replace")
            if "chat_persistence_parts" not in text:
                continue
            for node in ast.walk(ast.parse(text)):
                modules = []
                if isinstance(node, ast.Import):
                    modules = [alias.name for alias in node.names]
                elif isinstance(node, ast.ImportFrom):
                    modules = [node.module or ""]
                    modules += [f"{node.module}.{alias.name}" for alias in node.names]
                if any(m == PARTS_PACKAGE or m.startswith(PARTS_PACKAGE + ".") for m in modules):
                    importers.append(path.relative_to(root).as_posix())
        assert sorted(set(importers)) == ["dashboard/chat_persistence.py"]

    def test_a_part_imports_project_names_only_for_the_type_checker(self) -> None:
        runtime: set[tuple[str, str, str]] = set()
        for stem in PARTS:
            for node in _tree(PARTS_DIR / f"{stem}.py").body:
                if _is_type_checking(node):
                    for sub in node.body:
                        assert isinstance(sub, ast.ImportFrom) and sub.module == FACADE, stem
                    continue
                if isinstance(node, ast.ImportFrom) and (node.module or "").startswith("kiro_crew"):
                    runtime |= {(stem, node.module, alias.name) for alias in node.names}
                elif isinstance(node, ast.Import):
                    assert not any(a.name.startswith("kiro_crew") for a in node.names), stem
        assert runtime == _RUNTIME_PROJECT_IMPORTS

    def test_no_part_binds_the_facades_process_state(self) -> None:
        assert _PROCESS_STATE <= set(vars(cp))
        for stem in PARTS:
            assert _PROCESS_STATE & set(vars(_part(stem))) == set(), stem
            rebound = {
                name
                for node in ast.walk(_tree(PARTS_DIR / f"{stem}.py"))
                if isinstance(node, ast.Global)
                for name in node.names
            }
            assert rebound <= set(vars(cp)) - set(_COMPOSED), stem

    def test_the_conftest_isolation_resets_names_that_live_on_the_facade(self) -> None:
        # test/conftest.py binds most of its fixtures from plain modules beside it.
        here = Path(__file__).resolve()
        floor = set(here.parent.glob("conftest_floor_*.py"))
        parts = sorted(set(here.parent.glob("conftest_*.py")) - floor)
        conftest = "\n".join(
            p.read_text(encoding="utf-8") for p in (here.with_name("conftest.py"), *parts)
        )
        reset = {
            node.attr
            for node in ast.walk(ast.parse(conftest))
            if isinstance(node, ast.Attribute)
            and ast.unparse(node.value) == "_cp"
            and isinstance(node.ctx, ast.Store)
        }
        reset |= {
            node.func.value.attr
            for node in ast.walk(ast.parse(conftest))
            if isinstance(node, ast.Call)
            and isinstance(node.func, ast.Attribute)
            and node.func.attr == "clear"
            and isinstance(node.func.value, ast.Attribute)
            and ast.unparse(node.func.value.value) == "_cp"
        }
        assert reset == _CONFTEST_RESETS
        assert reset <= set(vars(cp))
        assert all(reset.isdisjoint(vars(_part(stem))) for stem in PARTS)

    def test_async_functions_sit_only_in_parts_the_off_loop_gate_reads(self) -> None:
        gate = (
            Path(__file__)
            .resolve()
            .with_name("test_persist_off_loop.py")
            .read_text(encoding="utf-8")
        )
        holders = {
            stem
            for stem in PARTS
            if any(
                isinstance(node, ast.AsyncFunctionDef)
                for node in ast.walk(_tree(PARTS_DIR / f"{stem}.py"))
            )
        }
        assert holders == set(PARTS)
        unread = [
            stem
            for stem in sorted(holders)
            if f'"chat_persistence_parts" / "{stem}.py"' not in gate
        ]
        assert unread == []

    def test_only_the_save_transaction_dispatches_a_save(self) -> None:
        dispatchers = set()
        for stem in PARTS:
            for node in ast.walk(_tree(PARTS_DIR / f"{stem}.py")):
                if isinstance(node, ast.Call):
                    func = node.func
                    called = (
                        func.attr if isinstance(func, ast.Attribute) else getattr(func, "id", "")
                    )
                    if called in _SAVERS:
                        dispatchers.add(stem)
        assert dispatchers == {"save_transaction"}


class TestPatchReach:
    """A patch on the facade reaches the composed code of every part."""

    def test_the_shutdown_save_dispatches_through_the_facade(self, monkeypatch) -> None:
        saved: list[object] = []
        monkeypatch.setattr(
            cp, "_save_slot_to_history", lambda state, slot, force: saved.append(slot)
        )
        slot = SimpleNamespace(key="k")
        state = SimpleNamespace(
            _slots={"k": slot},
            _persist_open_slots=lambda: None,
            _persist_context_snapshots=lambda: None,
        )
        cp.save_all_slots_to_history(state)
        assert saved == [slot]

    def test_the_restore_budget_is_read_through_the_facade(self, monkeypatch) -> None:
        state = SimpleNamespace(live_slot_count=lambda: 3)
        monkeypatch.setattr(cp, "RESTORE_SLOT_BUDGET", 4)
        assert cp._over_restore_budget(state, "k", set()) is False
        monkeypatch.setattr(cp, "RESTORE_SLOT_BUDGET", 3)
        assert cp._over_restore_budget(state, "k", set()) is True

    def test_a_rehydrate_normalizes_the_key_through_the_facade(self, monkeypatch) -> None:
        asked: list[str] = []
        monkeypatch.setattr(cp, "_normalize_slot_key", lambda raw: asked.append(raw) or raw)
        state = SimpleNamespace(
            conversation_log=SimpleNamespace(get_metadata=lambda key: {}), _slots={}
        )
        assert cp._rehydrate_slot_from_history(state, "k") is None
        assert asked == ["k"]

    def test_a_prewarm_release_classifies_the_pick_through_the_facade(self, monkeypatch) -> None:
        asked: list[tuple[str, object]] = []
        monkeypatch.setattr(
            cp,
            "_member_private_selection",
            lambda agent, config: asked.append((agent, config)) or ("", ""),
        )
        config = SimpleNamespace()
        assert (
            asyncio.run(cp.release_prewarmed_session(SimpleNamespace(), "k", "m", config)) is False
        )
        assert asked == [("m", config)]

    def test_the_process_state_is_read_only_by_the_code_the_facade_keeps(self) -> None:
        """The state stays beside the code that mutates it, so a fixture's rebind on the
        facade is the binding that code reads; no part reads it directly."""
        reads = {
            f"{stem}.{qualname}": global_reads(fn.__code__) & _PROCESS_STATE
            for stem in PARTS
            for qualname, fn in composed_functions(_part(stem)).items()
        }
        assert {name: hit for name, hit in reads.items() if hit} == {}
        for keeper in (
            "_build_message_entry",
            "_entry_cache_bounds",
            "update_reasoning_effort_values",
        ):
            assert getattr(cp, keeper).__code__.co_filename == cp.__file__, keeper


class TestImportInventory:
    def test_the_facade_imports_nothing_beyond_its_frozen_inventory(self) -> None:
        source = Path(cp.__file__).read_text(encoding="utf-8")
        family_reads = read_names(*_family_sources())
        assert unexpected_imports(source, _part_names(), family_reads) == []

    def test_the_inventory_check_reports_a_planted_import(self) -> None:
        """The F401 stand-in must be able to fail: a stray import, and a part import of a
        name that part does not define, are both reported, while an import the family's
        code reads is accounted for."""
        source = Path(cp.__file__).read_text(encoding="utf-8")
        planted = source + (
            "\nimport shutil\nimport fnmatch\n"
            f"from {PARTS_PACKAGE}.member_store import restore_open_slots\n"
        )
        family_reads = read_names(*_family_sources(), "_uses = fnmatch.fnmatch\n")
        assert unexpected_imports(planted, _part_names(), family_reads) == [
            "shutil",
            f"{PARTS_PACKAGE}.member_store.restore_open_slots",
        ]
