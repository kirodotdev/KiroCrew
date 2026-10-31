"""Tests that async code keeps blocking vector store calls off the event loop.

This file holds two package-wide AST guards, each with a seeded self-test: no ``async def``
calls a store's ``init()`` or ``close()`` inline, and none calls a ``_db_lock``-serialized
store method inline, with that method set derived from ``vector_memory.py`` and the
``vector_memory_runtime`` modules it delegates to. The lock rule itself is pinned in
``test_vector_memory_lock_discipline.py``.
"""

from __future__ import annotations

from pathlib import Path

import pytest
import source_corpus

# One xdist worker for the tests carrying this group, shared with ``test_vector_memory.py``
# and the topic files its docstring names. ``TestAsyncInitOffloadGuard`` and
# ``TestHandlerOffload1947`` read src/ through ``test/source_corpus.py``, so its memoized
# file list is walked once, not once per worker.
pytestmark = pytest.mark.xdist_group(name="tree_scan_test_vector_memory")


class TestAsyncInitOffloadGuard:
    """AST guard for blocking ``VectorMemoryStore`` lifecycle calls.

    An ``async def`` must never call ``init()`` or ``close()`` inline. The
    init path is blocking file IO whose Windows DACL writes can block on a
    network volume round-trip, and close serializes on the same store lock
    as ordinary database operations, so either can freeze the event
    loop. Async callers offload via ``asyncio.to_thread`` / ``run_in_executor``
    (which take the callable UNCALLED, so they never trip this guard).

    A prose-only contract drifts, so this test makes the next drift a CI failure
    instead of a code-review catch. Same shape as ``TestDbLockGuard``,
    including the seeded-violation self-test that keeps the guard armed.
    """

    @classmethod
    def _find_inline_async_lifecycle_calls(
        cls, tree, methods: set[str], where: str = ""
    ) -> list[str]:
        """Find direct lifecycle calls on constructor-bound vector stores.

        The receiver must be a name or ``self.<attr>`` assigned from
        ``VectorMemoryStore(...)`` in the same module. This keeps the generic
        ``close`` name useful without flagging unrelated transports, clients,
        files, and responses across the package.
        """
        import ast

        def _is_store_ctor(node: object) -> bool:
            return isinstance(node, ast.Call) and (
                (isinstance(node.func, ast.Name) and node.func.id == "VectorMemoryStore")
                or (isinstance(node.func, ast.Attribute) and node.func.attr == "VectorMemoryStore")
            )

        # Pass 1: collect module-local bindings created from the constructor.
        store_names: set = set()
        store_attrs: set = set()
        for node in ast.walk(tree):
            if isinstance(node, (ast.Assign, ast.AnnAssign)):
                value = node.value
                targets = node.targets if isinstance(node, ast.Assign) else [node.target]
                if value is not None and _is_store_ctor(value):
                    for target in targets:
                        if isinstance(target, ast.Name):
                            store_names.add(target.id)
                        elif isinstance(target, ast.Attribute) and isinstance(
                            target.value, ast.Name
                        ):
                            store_attrs.add(target.attr)

        # Pass 2: flag direct lifecycle calls on those bindings inside async defs.
        violations: list[str] = []

        class Visitor(ast.NodeVisitor):
            def __init__(self) -> None:
                self.name_stack: list = []
                self.async_stack: list = []

            def visit_ClassDef(self, node) -> None:
                self.name_stack.append(node.name)
                self.generic_visit(node)
                self.name_stack.pop()

            def _visit_func(self, node, is_async: bool) -> None:
                self.name_stack.append(node.name)
                self.async_stack.append(is_async)
                self.generic_visit(node)
                self.async_stack.pop()
                self.name_stack.pop()

            def visit_FunctionDef(self, node) -> None:
                self._visit_func(node, is_async=False)

            def visit_AsyncFunctionDef(self, node) -> None:
                self._visit_func(node, is_async=True)

            def visit_Call(self, node: ast.Call) -> None:
                func = node.func
                if (
                    isinstance(func, ast.Attribute)
                    and func.attr in methods
                    and self.async_stack
                    and self.async_stack[-1]
                ):
                    receiver = func.value
                    hit = (isinstance(receiver, ast.Name) and receiver.id in store_names) or (
                        isinstance(receiver, ast.Attribute)
                        and receiver.attr in store_attrs
                        and isinstance(receiver.value, ast.Name)
                    )
                    if hit:
                        loc = ".".join(self.name_stack) or "<module>"
                        violations.append(
                            f"{where}line {node.lineno} ({loc}): "
                            f"VectorMemoryStore.{func.attr}() "
                            "called inline in an async function — offload it via "
                            f"`await asyncio.to_thread(<store>.{func.attr})`"
                        )
                self.generic_visit(node)

        Visitor().visit(tree)
        return violations

    @classmethod
    def _find_inline_async_inits(cls, tree, where: str = "") -> list[str]:
        return cls._find_inline_async_lifecycle_calls(tree, {"init"}, where)

    @staticmethod
    def _candidates() -> list[tuple[Path, str, object]]:
        """The corpus files holding both needles, with taskrunner.py read as one file
        with its taskrunner_parts owners: ``TaskRunner``'s ``__init__`` stays in
        taskrunner.py while its coroutines live in the owners, so a store bound in one
        and closed in another is one module's binding."""
        import ast

        needles = ("VectorMemoryStore", "async")
        pkg = source_corpus.src_root()
        facade = pkg / "taskrunner.py"
        runner_parts = pkg / "taskrunner_parts"
        runner_owners = sorted(runner_parts.glob("[!_]*.py"))
        assert runner_owners, "expected the taskrunner_parts owners beside taskrunner.py"
        found: list[tuple[Path, str, object]] = [
            (path, text, tree)
            for path, text, tree in source_corpus.parsed_candidates(require_all=needles)
            if path != facade and path.parent != runner_parts
        ]
        joined = "".join(p.read_text(encoding="utf-8") for p in [facade, *runner_owners])
        if all(needle in joined for needle in needles):
            found.append((facade, joined, ast.parse(joined)))
        return found

    def test_no_async_function_calls_init_inline(self) -> None:
        # A violation needs a constructor binding (``VectorMemoryStore``) AND an
        # ``async`` def in the SAME module, so a file whose text lacks either can
        # never match: parse only the ones that hold both, off the shared corpus.
        pkg_root = source_corpus.src_root()
        violations: list = []
        for py, _text, tree in self._candidates():
            rel = py.relative_to(pkg_root.parent)
            violations.extend(self._find_inline_async_inits(tree, where=f"{rel}:"))
        assert not violations, "inline async VectorMemoryStore.init() call(s):\n" + "\n".join(
            violations
        )

    def test_guard_catches_a_seeded_violation(self) -> None:
        """The guard must fail on a seeded inline call and stay quiet on the
        offloaded / sync forms — otherwise a refactor that breaks its binding
        or async tracking would silently disarm it."""
        import ast

        seeded = ast.parse(
            "store = VectorMemoryStore()\n"
            "class S:\n"
            "    def __init__(self):\n"
            "        self.vm = VectorMemoryStore()\n"
            "    async def bad(self):\n"
            "        store.init()\n"
            "    async def bad_attr(self):\n"
            "        self.vm.init()\n"
            "    async def good(self):\n"
            "        await asyncio.to_thread(store.init)\n"
            "    def sync_ok(self):\n"
            "        store.init()\n"
            "    async def outer(self):\n"
            "        def helper():\n"
            "            store.init()\n"
            "        await asyncio.to_thread(helper)\n"
        )
        violations = self._find_inline_async_inits(seeded)
        # `bad` and `bad_attr` call init inline on the loop; `good` passes the
        # callable uncalled; `sync_ok` is a sync function; `helper` is a sync
        # function that runs at call time (inside to_thread).
        assert len(violations) == 2
        flagged = "\n".join(violations)
        assert "(S.bad)" in flagged
        assert "(S.bad_attr)" in flagged
        assert "S.good" not in flagged
        assert "S.sync_ok" not in flagged
        assert "S.outer" not in flagged

    def test_no_async_function_calls_close_inline_on_a_vector_store(self) -> None:
        # Same narrowing as the init guard: the receiver must be bound from
        # ``VectorMemoryStore(...)`` in the same module as the ``async`` def.
        root = TestHandlerOffload1947._package_root()
        violations: list[str] = []
        for path, _text, tree in self._candidates():
            violations.extend(
                self._find_inline_async_lifecycle_calls(
                    tree, {"close"}, where=f"{path.relative_to(root)}:"
                )
            )
        assert not violations, "inline async VectorMemoryStore.close() call(s):\n" + "\n".join(
            violations
        )

    def test_close_guard_is_receiver_aware_and_non_vacuous(self) -> None:
        import ast

        seeded = ast.parse(
            "store = VectorMemoryStore()\n"
            "async def bad():\n"
            "    store.close()\n"
            "async def unrelated(response):\n"
            "    response.close()\n"
            "async def good():\n"
            "    await asyncio.to_thread(store.close)\n"
        )
        violations = self._find_inline_async_lifecycle_calls(seeded, {"close"})
        assert len(violations) == 1
        assert "(bad)" in violations[0]
        assert "VectorMemoryStore.close()" in violations[0]


class TestHandlerOffload1947:
    """Async code must not call lock-serialized store methods inline on the
    event loop.

    Every plain fetch serializes on ``_db_lock``. A worker thread can
    hold that lock for seconds (backfill's locked FAISS rebuild, reconcile's
    bulk UPDATEs), so an async function that calls a locked method inline would
    freeze the whole gateway event loop — chat, heartbeats, every request — for
    the duration. Async callers must offload
    via ``asyncio.to_thread`` / ``run_in_executor`` / ``run_in_embed_pool``.

    Both the method set and the caller set are DERIVED, not hand-listed
    (a hand-maintained list re-introduces
    enforcement-by-convention one level up): the methods come from the AST of
    ``vector_memory.py`` and of the ``vector_memory_runtime`` modules it delegates
    to (public methods that reach a ``with <store>._db_lock:`` directly or
    transitively through store calls and runtime functions), and the scan covers
    every module in the ``kiro_crew`` package.
    """

    #: Lock-reaching methods exempt from this name-only scan. ``init`` is the
    #: one-time lifecycle call made before the store is shared across threads.
    #: ``close`` collides with unrelated clients, transports, and responses;
    #: TestAsyncInitOffloadGuard covers it with constructor-bound receivers.
    _EXEMPT = {"close", "init"}

    @staticmethod
    def _package_root() -> Path:
        import kiro_crew

        return Path(kiro_crew.__file__).resolve().parent

    #: A floor, not the list the scan enforces: the public lock-reaching set as
    #: it stood before the store delegated to ``vector_memory_runtime``. The
    #: enforced set stays derived. A derivation that stops following a delegate
    #: would shrink it and leave that delegate's async callers unflagged, so
    #: every name here must stay derived; a new locked method only adds to it.
    _FROZEN_LOCKED = frozenset("""
        append_history apply_consolidation backfill_missing_embeddings begin_space_change
        build_faiss_index consolidation_receipt count_by_facet count_lessons delete_episodic
        delete_lesson delete_semantic embed_episodic embed_lesson embed_semantic
        embed_semantic_retirement embedding_repair_state find_contradiction_candidates
        get_all_semantic get_context_preview get_episodic_context get_episodic_list get_events
        get_lessons get_lessons_context get_preferences_context get_rejection_stats
        get_retired_episodic get_semantic get_semantic_context has_any_decodable_lesson
        has_any_lesson has_episodic_text has_pending_embeddings has_stored_embeddings
        import_memory invalidate_episode_content list_by_facets load_faiss_index
        log_reject_event memory_stats migrate_from_markdown promote_episodic_patterns
        propose_semantic_delete read_editable_history read_history_entries
        rebuild_memory_index recall reconcile_embedding_space recorded_embedding_space
        recorded_rebuild_generation replace_today_history restore_episodic rotate_events
        save_faiss_index search_episodic search_memory search_semantic seed_item_if_absent
        set_embedding_dim set_semantic set_semantic_if_absent with_record_metadata
        write_episodic write_lesson
        """.split())

    @classmethod
    def _store_sources(cls) -> tuple[str, dict[str, str]]:
        """The facade's source and every owner module's, by stem: the
        ``vector_memory_runtime`` modules and the owners beside the facade."""
        import ast

        root = cls._package_root()
        facade = (root / "vector_memory.py").read_text(encoding="utf-8")
        runtime = {
            path.stem: path.read_text(encoding="utf-8")
            for path in sorted((root / "vector_memory_runtime").glob("*.py"))
            if path.stem != "__init__"
        }
        for node in ast.parse(facade).body:
            if isinstance(node, ast.ImportFrom) and node.module == "kiro_crew":
                for alias in node.names:
                    if alias.name.startswith("vector_memory_"):
                        path = root / f"{alias.name}.py"
                        runtime[alias.name] = path.read_text(encoding="utf-8")
        return facade, runtime

    @classmethod
    def _derive_locked_methods(cls) -> set[str]:
        """Public ``VectorMemoryStore`` methods that acquire ``_db_lock``,
        directly or transitively through other store calls, including the
        ``vector_memory_runtime`` functions the store delegates to."""
        facade, runtime = cls._store_sources()
        return cls._derive_locked_from_sources(facade, runtime)

    @classmethod
    def _derive_locked_from_sources(cls, facade: str, runtime: dict[str, str]) -> set[str]:
        """Fixpoint over one call graph spanning the store and its runtime modules.

        Nodes are ``VMS.<method>`` for store methods and ``<module>.<function>``
        for owner functions. The receiver is ``self`` in the store and in an owner
        function bound as a member (its first parameter is ``self``), and ``store``
        in a runtime function; a member bound to an owner function (``m = _x.f``)
        has that function as its one callee. A call to an owner function is resolved
        through the importing module's own ``from kiro_crew.vector_memory_runtime import
        mod`` and ``from kiro_crew import vector_memory_<x>`` aliases and
        ``from kiro_crew.vector_memory_runtime.mod import name`` names.
        """
        import ast

        package = "kiro_crew.vector_memory_runtime"

        def aliases(tree: ast.Module) -> tuple[dict[str, str], dict[str, str]]:
            modules: dict[str, str] = {}
            names: dict[str, str] = {}
            for node in tree.body:
                if isinstance(node, ast.ImportFrom) and node.module == package:
                    for alias in node.names:
                        modules[alias.asname or alias.name] = alias.name
                elif isinstance(node, ast.ImportFrom) and node.module == "kiro_crew":
                    for alias in node.names:
                        if alias.name in runtime:
                            modules[alias.asname or alias.name] = alias.name
                elif isinstance(node, ast.ImportFrom) and (node.module or "").startswith(
                    package + "."
                ):
                    owner = node.module.rsplit(".", 1)[-1]
                    for alias in node.names:
                        names[alias.asname or alias.name] = f"{owner}.{alias.name}"
            return modules, names

        def edges(fn, receiver, modules, names, local, own) -> tuple[bool, set[str]]:
            takes_lock = False
            called: set[str] = set()
            for sub in ast.walk(fn):
                if isinstance(sub, ast.withitem):
                    expr = sub.context_expr
                    if (
                        isinstance(expr, ast.Attribute)
                        and expr.attr == "_db_lock"
                        and isinstance(expr.value, ast.Name)
                        and expr.value.id == receiver
                    ):
                        takes_lock = True
                if not isinstance(sub, ast.Call):
                    continue
                func = sub.func
                if isinstance(func, ast.Attribute) and isinstance(func.value, ast.Name):
                    if func.value.id == receiver:
                        called.add(f"VMS.{func.attr}")
                    elif func.value.id in modules:
                        called.add(f"{modules[func.value.id]}.{func.attr}")
                elif isinstance(func, ast.Name):
                    if func.id in local:
                        called.add(f"{own}.{func.id}")
                    elif func.id in names:
                        called.add(names[func.id])
            return takes_lock, called

        callees: dict[str, set[str]] = {}
        direct: set[str] = set()

        tree = ast.parse(facade)
        modules, names = aliases(tree)
        klass = next(
            node
            for node in ast.walk(tree)
            if isinstance(node, ast.ClassDef) and node.name == "VectorMemoryStore"
        )
        for node in klass.body:
            if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
                locked, called = edges(node, "self", modules, names, set(), "")
                callees[f"VMS.{node.name}"] = called
                if locked:
                    direct.add(f"VMS.{node.name}")
            elif isinstance(node, ast.Assign) and len(node.targets) == 1:
                value = node.value
                if isinstance(value, ast.Call) and len(value.args) == 1:
                    value = value.args[0]
                if (
                    isinstance(node.targets[0], ast.Name)
                    and isinstance(value, ast.Attribute)
                    and isinstance(value.value, ast.Name)
                    and value.value.id in modules
                ):
                    callees[f"VMS.{node.targets[0].id}"] = {
                        f"{modules[value.value.id]}.{value.attr}"
                    }

        for stem, source in runtime.items():
            tree = ast.parse(source)
            modules, names = aliases(tree)
            local = {
                node.name
                for node in tree.body
                if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef))
            }
            for node in tree.body:
                if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
                    first = node.args.args[0].arg if node.args.args else ""
                    receiver = "self" if first == "self" else "store"
                    locked, called = edges(node, receiver, modules, names, local, stem)
                    callees[f"{stem}.{node.name}"] = called
                    if locked:
                        direct.add(f"{stem}.{node.name}")

        reaching = set(direct)
        changed = True
        while changed:
            changed = False
            for name, called in callees.items():
                if name not in reaching and called & reaching:
                    reaching.add(name)
                    changed = True

        return {
            name.removeprefix("VMS.")
            for name in reaching
            if name.startswith("VMS.") and not name.removeprefix("VMS.").startswith("_")
        } - cls._EXEMPT

    @classmethod
    def _find_inline_calls(cls, tree, locked: set[str], label: str) -> list[str]:
        """Violation string per inline call of a locked method inside an
        ``async def``. Shared by the real guard and its seeded self-test."""
        import ast

        violations: list[str] = []

        class Visitor(ast.NodeVisitor):
            def __init__(self) -> None:
                self.async_stack: list[str] = []

            def visit_AsyncFunctionDef(self, node) -> None:
                self.async_stack.append(node.name)
                self.generic_visit(node)
                self.async_stack.pop()

            def visit_FunctionDef(self, node) -> None:
                # A sync def's body runs wherever it is CALLED from; handlers
                # that offload a sync helper (run_in_executor on
                # _build_memory_graph) are the compliant pattern, so sync
                # bodies are out of scope here.
                saved, self.async_stack = self.async_stack, []
                self.generic_visit(node)
                self.async_stack = saved

            def visit_Call(self, node: ast.Call) -> None:
                func = node.func
                # Direct attribute call: <expr>.<method>(...) — inline execution.
                # Offloaded forms pass the method as an OBJECT
                # (asyncio.to_thread(store.get_events, ...)), which is an
                # Attribute argument, not a Call, so they do not match here.
                if self.async_stack and isinstance(func, ast.Attribute) and func.attr in locked:
                    violations.append(
                        f"{label} line {node.lineno} "
                        f"(async {self.async_stack[-1]}): inline .{func.attr}() call — "
                        "offload with asyncio.to_thread to keep a contended _db_lock "
                        "from blocking the event loop"
                    )
                self.generic_visit(node)

        Visitor().visit(tree)
        return violations

    def test_derived_method_set_is_sound(self) -> None:
        """The derivation must stay non-vacuous: known lock-takers present
        (direct AND transitive), known lock-free methods absent."""
        locked = self._derive_locked_methods()
        # Direct: write_episodic takes the lock in its own body. Transitive:
        # build_faiss_index and delete_semantic reach it only through other
        # self calls (get_all_semantic / get_semantic locked fetches).
        assert {
            "get_lessons",
            "write_episodic",
            "build_faiss_index",
            "delete_semantic",
            "memory_stats",
        } <= locked
        # Lock-free public methods must not be flagged, or the guard would
        # force pointless offloads. Lifecycle/generic-name exemptions are also
        # absent from the derived enforcement set.
        assert "validate_semantic" not in locked
        assert "embed_lesson" in locked
        assert not {"close", "init"} & locked

    def test_delegation_keeps_the_frozen_method_set(self) -> None:
        """Moving a body into ``vector_memory_runtime`` must not drop a method."""
        missing = self._FROZEN_LOCKED - self._derive_locked_methods()
        assert not missing, f"no longer derived as lock-reaching: {sorted(missing)}"

    def test_derivation_follows_delegation_into_runtime_modules(self) -> None:
        """A delegate that reaches the lock only through a runtime function, or
        through a store call made BY that function, is still lock-reaching."""
        import textwrap

        facade = textwrap.dedent("""
            from kiro_crew.vector_memory_runtime import reads as _reads

            class VectorMemoryStore:
                def _fetch(self):
                    with self._db_lock:
                        return 1

                def through_runtime(self):
                    return _reads.fetch_rows(self)

                def through_callback(self):
                    return _reads.back_to_store(self)

                def lock_free(self):
                    return _reads.pure(self)
            """)
        runtime = {
            "reads": textwrap.dedent("""
                from kiro_crew.vector_memory_runtime.other import helper

                def fetch_rows(store):
                    with store._db_lock:
                        return []

                def back_to_store(store):
                    return helper(store)

                def pure(store):
                    return 0
                """),
            "other": textwrap.dedent("""
                def helper(store):
                    return store._fetch()
                """),
        }
        assert self._derive_locked_from_sources(facade, runtime) == {
            "through_runtime",
            "through_callback",
        }
        # A member bound to an owner function, beside the facade or in the runtime
        # package, is lock-reaching when that function (receiver ``self``) is.
        bound_facade = facade.replace(
            "class VectorMemoryStore:",
            "from kiro_crew import vector_memory_owner as _owner\n\n"
            "class VectorMemoryStore:\n"
            "    bound_owner = _owner.locked\n"
            "    bound_static = staticmethod(_owner.pure)\n"
            "    bound_runtime = _reads.bound_fetch\n",
        )
        bound_runtime = {
            **runtime,
            "reads": runtime["reads"]
            + "\ndef bound_fetch(self):\n    with self._db_lock:\n        return []\n",
            "vector_memory_owner": textwrap.dedent("""
                def locked(self):
                    with self._db_lock:
                        return []

                def pure(text):
                    return text
                """),
        }
        assert self._derive_locked_from_sources(bound_facade, bound_runtime) == {
            "through_runtime",
            "through_callback",
            "bound_owner",
            "bound_runtime",
        }

    def test_guard_catches_seeded_violation(self) -> None:
        """The scanner must flag a known-bad inline call, so a visitor
        regression cannot silently turn the guard vacuous."""
        import ast
        import textwrap

        seeded = textwrap.dedent("""
            async def handler(store):
                return store.get_lessons()

            def sync_helper(store):
                return store.get_lessons()  # sync def: out of scope

            async def compliant(store):
                import asyncio
                return await asyncio.to_thread(store.get_lessons)
            """)
        violations = self._find_inline_calls(ast.parse(seeded), {"get_lessons"}, "<seeded>")
        assert len(violations) == 1
        assert "async handler" in violations[0]
        assert ".get_lessons()" in violations[0]

    def test_async_callers_offload_locked_methods(self) -> None:
        locked = self._derive_locked_methods()
        root = self._package_root()
        violations: list[str] = []
        # A violation is an ``async def`` holding ``<expr>.<locked>(...)``, so a module
        # whose text lacks ``async`` or every locked NAME cannot carry one: narrow to
        # those off the shared corpus (NFKC-folded, like the parser) and parse one
        # tree at a time, instead of a private ``rglob`` + ``ast.parse`` of the whole
        # package on every run of this one test (~6 s of CPU, measured).
        for path, _text, tree in source_corpus.parsed_candidates(
            require_all=("async",), require_any=tuple(sorted(locked))
        ):
            if path == root / "vector_memory.py":
                continue  # the store may call its own methods inline
            violations.extend(self._find_inline_calls(tree, locked, str(path.relative_to(root))))
        assert not violations, "\n".join(violations)
