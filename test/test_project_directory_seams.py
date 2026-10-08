"""Every seam that hands a session a project directory is accounted for."""

from __future__ import annotations

import ast
import inspect
import textwrap
from pathlib import Path

import kiro_crew
from kiro_crew.dashboard import chat_folders

Functions = tuple[str, ast.AST, str]


def _functions(path: Path):
    """Every function in *path* with the module source, for AST walks."""
    source = path.read_text(encoding="utf-8")
    try:
        tree = ast.parse(source)
    except SyntaxError:
        return
    for node in ast.walk(tree):
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
            yield source, node


def _callee(call: ast.Call) -> str:
    func = call.func
    if isinstance(func, ast.Attribute):
        return func.attr
    if isinstance(func, ast.Name):
        return func.id
    return ""


def _is_test_path(rel: str) -> bool:
    parts = rel.split("/")
    return "tests" in parts or parts[-1].startswith("test_")


class TestEverySeamThatHandsASessionAProjectDirectoryIsAccountedFor:
    """(1) A WRITER of ``<slot>.project`` in the dashboard package either RECORDS the identity."""

    DASHBOARD = Path(chat_folders.__file__).resolve().parent
    PACKAGE = Path(kiro_crew.__file__).resolve().parent
    RECORDERS = ("record_project_identity", "inherit_project_identity")
    DERIVATIONS = ("directory_identity_pinned", "identity_out", "inherit_project_identity")
    FACTORY_CALLEES = ("_provider_factory", "provider_factory", "factory")

    #: Writers that record the identity with the spelling, at the write.
    RECORDS_AT_THE_WRITE = {
        ("chat_fork.py", "fork_slot"),
        ("chat_handlers.py", "api_chat_slot_create"),
        ("chat_handlers.py", "api_chat_slot_project"),
        ("chat_handlers.py", "switch_slot_agent"),
        ("session_control.py", "create_session"),
        ("session_directive_apply.py", "_set_project"),
    }
    #: Writers whose spelling is configuration or a restore -- never a request-named
    #: directory -- left record-less and re-pinned (or refused) at the first spawn.
    RE_PINNED_AT_FIRST_SPAWN = {
        ("channel_slots.py", "surface_channel_session"): (
            "the restore of a persisted placement when a channel conversation surfaces"
        ),
        ("chat_handlers.py", "api_chat_slot_workspace"): (
            "a workspace switch points the slot at that workspace's configured default project"
        ),
        (
            "slot_persistence/metadata_codec.py",
            "_read_project",
        ): "a restore from persisted metadata",
        ("handlers/members.py", "api_member_thread"): (
            "the member thread's project is the member's own configured workspace"
        ),
        ("handlers/members.py", "_bind_member_workspace"): (
            "a member thread re-bound on open to its crew's configured workspace"
        ),
    }
    #: Factory doors that pass ``cwd`` and are neither the allocation body nor resolvers.
    SPAWNS_WHERE_NO_SLOT_IS_BOUND = {
        ("session_pool.py", "_fill_warm_pool_loop"): (
            "pre-spawns into the pool's own directory with no binding; a pooled child is "
            "claimable only by a spawn carrying no identity (bypass_cwd_identity)"
        ),
        ("apps/builtins/auto_improvement/spine/agent_runner.py", "_run_async"): (
            "the auto-improvement spine spawns its runner into the throwaway worktree it "
            "created, through its own factory; no dashboard slot binds that directory"
        ),
    }
    #: Runtime / client constructions that pass ``work_dir`` for a directory no
    #: dashboard slot binds, so there is no recorded identity to carry.
    CONSTRUCTS_WHERE_NO_SLOT_IS_BOUND = {
        ("apps/builtins/code_review_sage/sage_lib/review_pool.py", "_ensure_runtime_locked"): (
            "the review pool spawns its runtimes into the review tree it owns; no dashboard "
            "slot binds that directory"
        ),
    }
    #: The constructors that start an agent process in a working directory.
    CONSTRUCTORS = {"AcpRuntime", "AcpClient"}
    #: Factory doors that resolve the identity of the directory they hand over.
    RESOLVES_AT_THE_DOOR = {
        ("session_allocation.py", "_get_or_create_impl"),
        ("session_allocation.py", "_get_or_bootstrap_run_runtime"),
    }

    _writers_cache: dict[tuple[str, str], tuple[ast.AST, str]] | None = None
    _producers_cache: (
        tuple[
            dict[tuple[str, str], str],
            dict[tuple[str, str], str],
            dict[tuple[str, str], list[tuple[int, bool]]],
        ]
        | None
    ) = None

    @classmethod
    def _writers(cls) -> dict[tuple[str, str], tuple[ast.AST, str]]:
        """Top-level dashboard functions that assign ``<x>.project`` (a Store; reads never match)."""
        if cls._writers_cache is not None:
            return cls._writers_cache
        found: dict[tuple[str, str], tuple[ast.AST, str]] = {}
        for path in sorted(cls.DASHBOARD.rglob("*.py")):
            rel = path.relative_to(cls.DASHBOARD).as_posix()
            source = path.read_text(encoding="utf-8")
            tree = ast.parse(source)
            for node in tree.body:
                if not isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
                    continue
                writes = any(
                    isinstance(sub, ast.Assign)
                    and any(
                        isinstance(t, ast.Attribute) and t.attr == "project" for t in sub.targets
                    )
                    for sub in ast.walk(node)
                )
                if writes:
                    found[(rel, node.name)] = (node, ast.get_source_segment(source, node) or "")
        cls._writers_cache = found
        return found

    @staticmethod
    def _records(node: ast.AST) -> bool:
        for sub in ast.walk(node):
            if isinstance(sub, ast.Assign) and any(
                isinstance(t, ast.Attribute) and t.attr == "project_identity" for t in sub.targets
            ):
                return True
            if isinstance(sub, ast.Call) and _callee(sub) in (
                "record_project_identity",
                "inherit_project_identity",
            ):
                return True
        return False

    @staticmethod
    def _copies_a_record(node: ast.AST) -> bool:
        return any(
            isinstance(sub, ast.Attribute)
            and sub.attr == "project_identity"
            and isinstance(sub.ctx, ast.Load)
            for sub in ast.walk(node)
        )

    @classmethod
    def _producers(
        cls,
    ) -> tuple[
        dict[tuple[str, str], str],
        dict[tuple[str, str], str],
        dict[tuple[str, str], list[tuple[int, bool]]],
    ]:
        """Factory doors passing ``cwd``, ``cwd_identity=`` producers."""
        if cls._producers_cache is not None:
            return cls._producers_cache
        doors: dict[tuple[str, str], str] = {}
        producers: dict[tuple[str, str], str] = {}
        constructions: dict[tuple[str, str], list[tuple[int, bool]]] = {}
        for path in sorted(cls.PACKAGE.rglob("*.py")):
            rel = path.relative_to(cls.PACKAGE).as_posix()
            if _is_test_path(rel):
                continue
            for source, node in _functions(path):
                door = producer = False
                for sub in ast.walk(node):
                    if not isinstance(sub, ast.Call):
                        continue
                    keywords = {k.arg for k in sub.keywords if k.arg}
                    # A factory called through ``_under_verified_hold(cwd, identity,
                    # factory, ...)`` is the same door: the step is the callable
                    # passed positionally, the factory's keywords travel with it.
                    stepped = _callee(sub) == "_under_verified_hold" and any(
                        isinstance(a, ast.Name)
                        and a.id in cls.FACTORY_CALLEES
                        or isinstance(a, ast.Attribute)
                        and a.attr in cls.FACTORY_CALLEES
                        for a in sub.args
                    )
                    if (_callee(sub) in cls.FACTORY_CALLEES or stepped) and "cwd" in keywords:
                        door = True
                    if "cwd_identity" in keywords:
                        producer = True
                    if _callee(sub) in cls.CONSTRUCTORS and "work_dir" in keywords:
                        constructions.setdefault((rel, node.name), []).append(
                            (sub.lineno, "work_dir_identity" in keywords)
                        )
                if door or producer:
                    segment = ast.get_source_segment(source, node) or ""
                    if door:
                        doors[(rel, node.name)] = segment
                    if producer:
                        producers[(rel, node.name)] = segment
        cls._producers_cache = (doors, producers, constructions)
        return doors, producers, constructions

    def test_every_writer_of_a_sessions_project_is_one_of_the_two_lists(self) -> None:
        writers = self._writers()
        expected = set(self.RECORDS_AT_THE_WRITE) | set(self.RE_PINNED_AT_FIRST_SPAWN)
        unaccounted = sorted(set(writers) - expected)
        assert not unaccounted, (
            "a function now writes a session's project without a place in this test: record "
            "the identity you validated (record_project_identity / inherit_project_identity) "
            "and list it in RECORDS_AT_THE_WRITE, or -- for a configured or restored spelling "
            f"only -- add it to RE_PINNED_AT_FIRST_SPAWN with the reason -- {unaccounted}"
        )
        gone = sorted(expected - set(writers))
        assert not gone, f"listed writers no longer write a session's project; prune them: {gone}"

    def test_every_recording_writer_records_a_derived_identity(self) -> None:
        writers = self._writers()
        for key in sorted(self.RECORDS_AT_THE_WRITE):
            node, segment = writers[key]
            assert self._records(node), f"{key} writes a session's project and records nothing"
            derived = any(name in segment for name in self.DERIVATIONS) or self._copies_a_record(
                node
            )
            assert derived, (
                f"{key} records an identity that comes from neither the fenced pin nor a "
                "record -- a by-name stat pins the wrong directory"
            )

    def test_every_re_pinned_writer_is_configuration_or_a_restore_and_records_nothing(
        self,
    ) -> None:
        writers = self._writers()
        for key, reason in self.RE_PINNED_AT_FIRST_SPAWN.items():
            assert reason.strip(), f"{key} needs its reason"
            node, _segment = writers[key]
            assert not self._records(
                node
            ), f"{key} now records an identity: move it to RECORDS_AT_THE_WRITE"

    def test_every_factory_door_that_passes_a_directory_is_accounted_for(self) -> None:
        doors, _producers, _constructions = self._producers()
        expected = set(self.RESOLVES_AT_THE_DOOR) | set(self.SPAWNS_WHERE_NO_SLOT_IS_BOUND)
        unaccounted = sorted(set(doors) - expected)
        assert not unaccounted, (
            "a new path hands a process a directory outside the allocation body: route it "
            "through SessionManager.get_or_create, resolve the identity with "
            f"_resolve_cwd_identity, or list why no slot binds that directory -- {unaccounted}"
        )
        gone = sorted(expected - set(doors))
        assert not gone, f"listed doors no longer pass a directory to the factory: {gone}"
        for key in sorted(self.RESOLVES_AT_THE_DOOR):
            assert "_resolve_cwd_identity" in doors[key], f"{key} hands out a directory unresolved"
        for key, reason in self.SPAWNS_WHERE_NO_SLOT_IS_BOUND.items():
            assert reason.strip(), f"{key} needs its reason"

    def test_every_cwd_identity_producer_takes_the_record_and_never_pins_fresh(self) -> None:
        _doors, producers, _constructions = self._producers()
        assert producers, "no producer passes cwd_identity any more; re-read the seam"
        for key, segment in sorted(producers.items()):
            assert (
                "spawn_project_identity_repinned" in segment or "_resolve_cwd_identity" in segment
            ), f"{key} hands the spawn an identity that is not the slot's record"
            assert (
                "directory_identity_pinned" not in segment
            ), f"{key} pins the directory fresh at spawn -- a swap after the binding passes"

    def test_every_runtime_construction_that_passes_a_directory_carries_its_identity(
        self,
    ) -> None:
        """The door the two sweeps above do not see."""
        _doors, _producers, constructions = self._producers()
        assert constructions, "no runtime construction passes work_dir any more; re-read the seam"
        unaccounted = sorted(
            f"{key[0]}:{key[1]}:{line}"
            for key, calls in constructions.items()
            if key not in self.CONSTRUCTS_WHERE_NO_SLOT_IS_BOUND
            for line, carries_identity in calls
            if not carries_identity
        )
        assert not unaccounted, (
            "a runtime or client is built for a working directory without the session's "
            "recorded identity: pass work_dir_identity exactly as the first spawn does, or "
            f"list why no slot binds that directory -- {unaccounted}"
        )
        gone = sorted(set(self.CONSTRUCTS_WHERE_NO_SLOT_IS_BOUND) - set(constructions))
        assert not gone, f"listed constructions no longer pass a directory: {gone}"
        for key, reason in self.CONSTRUCTS_WHERE_NO_SLOT_IS_BOUND.items():
            assert reason.strip(), f"{key} needs its reason"


class TestNoProbeOfABoundDirectorysNamePrecedesItsPin:
    """(2) The order the fence depends on, pinned at every entry point at once.

    Wherever a BOUND directory's name reaches the OS, a pathname probe ahead of
    the call that pins or verifies it (the readiness ``mkdir``, a link screen's
    by-name walk, the restore's ``is_dir``) follows a swapped-in link before the
    pin can refuse it. So each entry point's source is read: no probe of the name
    may precede its pin, and past the verify the name may reach only the calls
    listed in ``NAME_AFTER_THE_VERIFY``.
    """

    #: Every call that follows a name on disk. ``abspath`` is lexical and
    #: ``get_cwd`` reads the session map, so neither is a probe.
    PROBES = (
        "is_dir",
        "isdir",
        "exists",
        "resolve",
        "realpath",
        "stat",
        "lstat",
        "mkdir",
        "open",
        "scandir",
        "listdir",
        "readlink",
    )

    #: (module, qualified function) -> (the call that pins or verifies the
    #: directory, the names the directory travels under inside that function).
    ENTRY_POINTS = {
        ("kiro_crew.acp.client", "AcpClient._spawn"): (
            "verify_agent_workspace_for_spawn_async",
            ("_work_dir", "work_dir", "spawn_cwd", "cwd"),
        ),
        ("kiro_crew.acp.runtime", "AcpRuntime._spawn_admitted"): (
            "verify_agent_workspace_for_spawn_async",
            ("_work_dir", "work_dir", "spawn_cwd", "cwd"),
        ),
        ("kiro_crew.dashboard.chat_folders", "screen_and_resolve_project_dir"): (
            "_pinned_project_dir",
            ("spelling", "expanded", "project_dir", "project"),
        ),
        ("kiro_crew.session_allocation", "SessionAllocationService._get_or_create_impl"): (
            "_resolve_cwd_identity",
            ("stored_cwd", "effective_cwd", "cwd"),
        ),
    }

    #: Calls the bound directory's NAME reaches at the ACP spawn sites AFTER the
    #: verify, each with why. The tracked residual (its issue is in the value):
    #: preparation reads that take the name, which on POSIX a rename with a link
    #: planted at the spelling re-points between the verify and the spawn -- the
    #: hold pins the child's working directory there, not the spelling (on
    #: Windows the held chain pins both). The rest read nothing by name. An
    #: unlisted call fails.
    NAME_AFTER_THE_VERIFY = {
        "require_fork_governance": "tracked residual (#17395)",
        "delegated_workspace_exposes_sealed_target": "tracked residual (#17395)",
        "prepare_native_skill_projection": "tracked residual (#17395)",
        "assert_voice_runtime_outside_agent_workspace": "tracked residual (#17395)",
        "require_fresh_derived_spec": "tracked residual (#17395): the project-shadow check",
        "resolve_spawn": (
            "tracked residual (#17395): a harness adapter's own preparation reads the "
            "directory by name (the settings seed, the gate artifacts)"
        ),
        "inherits_default_resources": (
            "tracked residual (#17395): the projected launch record asks by name whether "
            "the work dir inherits the default resources"
        ),
        "projected_launch_paths": (
            "tracked residual (#17395): the projected launch record resolves the view's "
            "declared guides against the work dir by name"
        ),
        "SpawnContext": "lexical: the record that hands the adapter the name above",
        "LaunchRequest": (
            "lexical: the record that hands the launch tail the verified spelling and the hold"
        ),
        "launch": (
            "the tail: the verified spelling is the child's cwd and the held descriptor "
            "its chdir_fd on POSIX; the bind there is cross-checked against the hold"
        ),
        "verify_agent_workspace_for_spawn_async": "the verify itself",
        "bind_voice_safe_agent_workspace_async": (
            "its descriptor is cross-checked against the verified one "
            "(refuse_unless_bound_workspace_is_pinned_async)"
        ),
        "ensure_directory": "unbound only: gated on the identity being None",
        "str": "lexical",
    }

    #: The one function in ``acp/`` that creates the agent process: the launch tail
    #: both entry points hand their plan to. It takes the hold the entry point
    #: verified (``LaunchRequest.verified_workspace_fd``), cross-checks the bind
    #: against it and enters the child through it on POSIX.
    SPAWN_TAIL = ("kiro_crew.acp.launch", "launch")

    @staticmethod
    def _function_node(module_name: str, qualname: str) -> tuple[str, ast.AST]:
        import importlib

        module = importlib.import_module(module_name)
        source = Path(module.__file__).read_text(encoding="utf-8")
        tree = ast.parse(source)
        parts = qualname.split(".")
        node: ast.AST = tree
        for part in parts:
            found = None
            for child in ast.walk(node):
                if (
                    isinstance(child, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef))
                    and child.name == part
                ):
                    found = child
                    break
            assert found is not None, f"{module_name}.{qualname}: {part!r} not found"
            node = found
        return source, node

    @staticmethod
    def _names_in(node: ast.AST) -> set[str]:
        names: set[str] = set()
        for sub in ast.walk(node):
            if isinstance(sub, ast.Name):
                names.add(sub.id)
            elif isinstance(sub, ast.Attribute):
                names.add(sub.attr)
        return names

    def _probes_before_the_pin(self, module_name: str, qualname: str) -> list[str]:
        """Every probe of the directory's name before the pin call -- a call or a callable handed to a thread."""
        pin, aliases = self.ENTRY_POINTS[(module_name, qualname)]
        _source, fn = self._function_node(module_name, qualname)
        calls = [c for c in ast.walk(fn) if isinstance(c, ast.Call)]
        pin_line = min((c.lineno for c in calls if _callee(c) == pin), default=None)
        assert pin_line is not None, f"{qualname} no longer calls {pin}(): re-read the seam"
        offenders: list[str] = []
        for sub in ast.walk(fn):
            if isinstance(sub, ast.Attribute):
                probe, holder = sub.attr, sub.value
            elif isinstance(sub, ast.Call) and isinstance(sub.func, ast.Name):
                probe, holder = sub.func.id, sub
            else:
                continue
            if probe not in self.PROBES or sub.lineno >= pin_line:
                continue
            touched = self._names_in(holder) & set(aliases)
            if touched:
                offenders.append(f"{qualname}:{sub.lineno} {probe}({sorted(touched)})")
        return sorted(set(offenders))

    def test_no_entry_point_probes_the_directorys_name_before_the_pin(self) -> None:
        offenders = []
        for module_name, qualname in self.ENTRY_POINTS:
            offenders.extend(self._probes_before_the_pin(module_name, qualname))
        assert not offenders, (
            "a bound directory's name reaches the OS before the call that pins or "
            "verifies it; move the probe after the pin (the class docstring names the "
            f"shape) -- {offenders}"
        )

    def _name_handoffs_after_the_verify(self, module_name: str, qualname: str) -> dict[str, int]:
        """callee -> first line, for every call past the pin that is handed the name."""
        pin, aliases = self.ENTRY_POINTS[(module_name, qualname)]
        _source, fn = self._function_node(module_name, qualname)
        calls = [c for c in ast.walk(fn) if isinstance(c, ast.Call)]
        pin_line = min(c.lineno for c in calls if _callee(c) == pin)
        found: dict[str, int] = {}
        for call in calls:
            handed: set[str] = set()
            for arg in [*call.args, *(k.value for k in call.keywords)]:
                handed |= self._names_in(arg)
            if call.lineno < pin_line or not handed & set(aliases):
                continue
            name = _callee(call)
            if name == "to_thread" and call.args:  # the threaded callable is the reader
                name = _callee(ast.Call(func=call.args[0], args=[], keywords=[]))
            found.setdefault(name, call.lineno)
        return found

    def test_after_the_verify_the_name_reaches_only_the_listed_calls(self) -> None:
        """Past the verify, the name is handed only to listed calls; every tracked residual is present."""
        seen: set[str] = set()
        for module_name, qualname in self.ENTRY_POINTS:
            if not module_name.startswith("kiro_crew.acp."):
                continue
            found = self._name_handoffs_after_the_verify(module_name, qualname)
            unlisted = sorted(
                f"{qualname}:{line} {name}"
                for name, line in found.items()
                if name not in self.NAME_AFTER_THE_VERIFY
            )
            assert not unlisted, (
                "the bound directory's name reaches a call after the verify that "
                "NAME_AFTER_THE_VERIFY does not list; a by-name read there is the "
                f"#17395 residual -- read relative to the held descriptor, or list it with "
                f"its reason: {unlisted}"
            )
            seen |= set(found)
        tracked = {n for n, why in self.NAME_AFTER_THE_VERIFY.items() if why.startswith("tracked")}
        assert (
            tracked <= seen
        ), f"tracked residual no longer handed the name -- drop it from the list: {sorted(tracked - seen)}"

    def test_the_pin_itself_is_still_the_first_touch_of_the_name(self) -> None:
        """The pin call is present in each entry point and takes the directory's name."""
        for (module_name, qualname), (pin, aliases) in self.ENTRY_POINTS.items():
            _source, fn = self._function_node(module_name, qualname)
            pins = [c for c in ast.walk(fn) if isinstance(c, ast.Call) and _callee(c) == pin]
            assert pins, f"{qualname} does not call {pin}()"
            first = min(pins, key=lambda c: (c.lineno, c.col_offset))
            assert self._names_in(first) & set(aliases), (
                f"{qualname}:{first.lineno} {pin}() is not applied to the directory "
                f"({sorted(aliases)}); update the alias list or the seam"
            )

    def test_the_restore_seam_is_an_entry_point(self) -> None:
        """The eager respawn's restore of a stored directory is one of the entry points."""
        assert (
            "kiro_crew.session_allocation",
            "SessionAllocationService._get_or_create_impl",
        ) in self.ENTRY_POINTS

    #: The by-name touches of a possibly bound directory outside the spawn itself
    #: (provider factory, runtime preparation, saved-capability recheck): every call
    #: site in the package is DERIVED, not listed, and must sit inside
    #: ``verified_hold`` or carry a reason below -- a new site fails by construction.
    SINKS = ("prepare_runtime", "verify_saved")
    FACTORY_CALLEES = ("_provider_factory", "provider_factory", "factory")
    CHOKE_POINT = "verified_hold"
    #: Sinks outside the choke point, each with why no slot can bind its directory.
    SINKS_WHERE_NO_SLOT_IS_BOUND = {
        ("session_pool.py", "_fill_warm_pool_loop"): (
            "pre-spawns into the pool's own directory (the workspace default: configuration, "
            "not a binding); a pooled child is claimable only by a spawn carrying no identity"
        ),
        ("apps/builtins/auto_improvement/spine/agent_runner.py", "_run_async"): (
            "the auto-improvement spine spawns its runner into the throwaway worktree it "
            "created, through its own factory; no dashboard slot binds that directory"
        ),
    }

    @classmethod
    def _sink_sites(cls) -> list[tuple[str, str, int, str, bool]]:
        """Every sink call in ``src/kiro_crew``: (file, function, line, sink, inside the choke point)."""
        package = Path(kiro_crew.__file__).resolve().parent
        sites: list[tuple[str, str, int, str, bool]] = []
        for path in sorted(package.rglob("*.py")):
            rel = path.relative_to(package).as_posix()
            if _is_test_path(rel):
                continue
            tree = ast.parse(path.read_text(encoding="utf-8"))
            parents: dict[ast.AST, ast.AST] = {}
            for node in ast.walk(tree):
                for child in ast.iter_child_nodes(node):
                    parents[child] = node
            for call in ast.walk(tree):
                if not isinstance(call, ast.Call):
                    continue
                name = _callee(call)
                keywords = {k.arg for k in call.keywords if k.arg}
                sink = ""
                if name in cls.FACTORY_CALLEES and "cwd" in keywords:
                    sink = name
                elif name in cls.SINKS:
                    sink = name
                elif name == "to_thread" and call.args:
                    first = call.args[0]
                    target = first.id if isinstance(first, ast.Name) else getattr(first, "attr", "")
                    if target in cls.SINKS:
                        sink = f"to_thread({target})"
                if not sink:
                    continue
                func = "<module>"
                held = False
                node: ast.AST = call
                while node in parents:
                    node = parents[node]
                    if isinstance(node, (ast.AsyncWith, ast.With)) and any(
                        isinstance(item.context_expr, ast.Call)
                        and _callee(item.context_expr) == cls.CHOKE_POINT
                        for item in node.items
                    ):
                        held = True
                    if (
                        isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef))
                        and func == "<module>"
                    ):
                        func = node.name
                sites.append((rel, func, call.lineno, sink, held))
        return sites

    def test_every_name_touch_in_the_package_runs_inside_the_verified_hold(self) -> None:
        sites = self._sink_sites()
        assert sites, "no sink call site found: the scan or the callee names are stale"
        outside = [
            f"{rel}:{line} {func}() calls {sink} outside {self.CHOKE_POINT}"
            for rel, func, line, sink, held in sites
            if not held and (rel, func) not in self.SINKS_WHERE_NO_SLOT_IS_BOUND
        ]
        assert not outside, (
            "a bound directory's name reaches the runtime preparation, the provider "
            f"construction or the saved-capability recheck outside {self.CHOKE_POINT}; wrap "
            "the step in `async with verified_hold(cwd, identity):` after resolving the "
            f"identity, or list why no slot can bind that directory -- {outside}"
        )
        held = {(rel, func, sink) for rel, func, _line, sink, held in sites if held}
        for expected in (
            ("session_allocation.py", "_get_or_create_impl", "to_thread(prepare_runtime)"),
            ("session_allocation.py", "_get_or_create_impl", "factory"),
            ("session_allocation.py", "_get_or_create_impl", "to_thread(verify_saved)"),
            ("session_allocation.py", "_get_or_bootstrap_run_runtime", "_provider_factory"),
            ("session_allocation.py", "open_task_session", "to_thread(prepare_runtime)"),
        ):
            assert expected in held, f"{expected} is no longer a held sink: re-read the seam"
        for key, reason in self.SINKS_WHERE_NO_SLOT_IS_BOUND.items():
            assert reason.strip(), f"{key} needs its reason"
            assert any(
                (rel, func) == key for rel, func, *_ in sites
            ), f"{key} no longer calls a sink: drop it from SINKS_WHERE_NO_SLOT_IS_BOUND"

    @staticmethod
    def _acp_functions() -> list[tuple[str, str, ast.AST, bool]]:
        """``(module, qualname, node, is_method)`` for every function under ``acp/``."""
        package = Path(kiro_crew.__file__).resolve().parent
        found: list[tuple[str, str, ast.AST, bool]] = []
        for path in sorted((package / "acp").rglob("*.py")):
            tree = ast.parse(path.read_text(encoding="utf-8"))
            module = "kiro_crew." + ".".join(path.relative_to(package).with_suffix("").parts)
            for node in tree.body:
                if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
                    found.append((module, node.name, node, False))
                elif isinstance(node, ast.ClassDef):
                    for fn in node.body:
                        if isinstance(fn, (ast.FunctionDef, ast.AsyncFunctionDef)):
                            found.append((module, f"{node.name}.{fn.name}", fn, True))
        return found

    @staticmethod
    def _creates_the_process(fn: ast.AST) -> bool:
        """Whether *fn* calls ``create_subprocess_limited`` or hands it to a call (the
        ``functools.partial`` shape) -- not merely names it as a keyword value."""

        def _is_it(node: ast.AST) -> bool:
            return (isinstance(node, ast.Name) and node.id == "create_subprocess_limited") or (
                isinstance(node, ast.Attribute) and node.attr == "create_subprocess_limited"
            )

        return any(
            isinstance(call, ast.Call) and (_is_it(call.func) or any(_is_it(a) for a in call.args))
            for call in ast.walk(fn)
        )

    def test_every_acp_spawn_site_is_a_listed_hold_first_entry_point(self) -> None:
        """Derived from the code: the function in ``acp/`` that creates the process is the one
        listed tail, and every ACP class method that calls it is in ENTRY_POINTS (hold before
        any probe), so a new spawn site fails here before any lane reads it."""
        functions = self._acp_functions()
        tails = {(m, q) for m, q, fn, _method in functions if self._creates_the_process(fn)}
        assert tails == {self.SPAWN_TAIL}, (
            "the agent process is created somewhere other than the one launch tail; a second "
            f"creation site would spawn with no hold -- {sorted(tails ^ {self.SPAWN_TAIL})}"
        )
        tail_name = self.SPAWN_TAIL[1]
        spawners = {
            (m, q)
            for m, q, fn, method in functions
            if method
            and any(isinstance(c, ast.Call) and _callee(c) == tail_name for c in ast.walk(fn))
        }
        assert (
            spawners
        ), "no ACP spawn site calls the launch tail: its name changed, re-read the seam"
        unlisted = sorted(spawners - set(self.ENTRY_POINTS))
        assert not unlisted, (
            "an ACP spawn site is not a listed hold-first entry point; add it to ENTRY_POINTS "
            f"with its pin call and the names the directory travels under -- {unlisted}"
        )

    def test_the_launch_tail_enters_the_child_through_the_verified_hold(self) -> None:
        """Every entry point hands the tail the hold it verified, and the tail cross-checks the
        bind against it and derives the descriptor the child enters from it."""
        for module_name, qualname in self.ENTRY_POINTS:
            if not module_name.startswith("kiro_crew.acp."):
                continue
            _source, fn = self._function_node(module_name, qualname)
            handed = {
                kw.arg: kw.value
                for call in ast.walk(fn)
                if isinstance(call, ast.Call) and _callee(call) == "LaunchRequest"
                for kw in call.keywords
                if kw.arg in ("verified_workspace_fd", "spawn_cwd")
            }
            for field in ("verified_workspace_fd", "spawn_cwd"):
                value = handed.get(field)
                assert isinstance(value, ast.Name) and value.id == field, (
                    f"{qualname} does not hand the tail the verify's own {field} "
                    "(the hold and the verified spelling travel in the launch request)"
                )
        _source, tail = self._function_node(*self.SPAWN_TAIL)
        calls = [c for c in ast.walk(tail) if isinstance(c, ast.Call)]
        cross_checks = [
            c for c in calls if _callee(c) == "refuse_unless_bound_workspace_is_pinned_async"
        ]
        assert cross_checks, "the tail no longer cross-checks the bind against the verified hold"
        assert all(
            "verified_workspace_fd" in self._names_in(c) for c in cross_checks
        ), "the bind cross-check does not compare against the request's verified hold"
        factory = [
            c
            for c in calls
            if any(
                isinstance(a, ast.Attribute) and a.attr == "create_subprocess_limited"
                for a in c.args
            )
        ]
        assert len(factory) == 1, "the tail's process factory is not the one partial"
        chdir = [kw for kw in factory[0].keywords if kw.arg == "chdir_fd"]
        assert chdir and isinstance(chdir[0].value, ast.Name) and chdir[0].value.id == "chdir_fd", (
            "the child must enter through the descriptor the tail derived (the bind's, else the "
            "verified hold on POSIX), not a bare attribute"
        )
        derived = [
            n
            for n in ast.walk(tail)
            if isinstance(n, ast.Assign)
            and any(isinstance(t, ast.Name) and t.id == "chdir_fd" for t in n.targets)
        ]
        assert derived and any(
            "verified_workspace_fd" in self._names_in(n.value) for n in derived
        ), "the tail's descriptor is not derived from the verified hold"

    def test_the_choke_point_verifies_before_the_step_and_releases_after_it(self) -> None:
        """``verified_hold``: the off-loop verify before the ``yield``, the off-loop release in
        its ``finally``, nothing held for a directory with no identity."""
        from kiro_crew import session_allocation

        source = inspect.getsource(session_allocation.verified_hold)
        fn = ast.parse(textwrap.dedent(source)).body[0]
        verify_line = min(
            c.lineno
            for c in ast.walk(fn)
            if isinstance(c, ast.Call) and _callee(c) == "verify_agent_workspace_for_spawn_async"
        )
        yields = [n for n in ast.walk(fn) if isinstance(n, ast.Yield)]
        assert len(yields) == 1 and yields[0].lineno > verify_line, "the step runs before the hold"
        tries = [n for n in ast.walk(fn) if isinstance(n, ast.Try)]
        assert len(tries) == 1, "one try/finally guards the step"
        releases = [
            n
            for stmt in tries[0].finalbody
            for n in ast.walk(stmt)
            if isinstance(n, ast.Name) and n.id == "release_agent_workspace_fd"
        ]
        assert releases, "the hold is not released in the finally"
        executor = [
            c
            for stmt in tries[0].finalbody
            for c in ast.walk(stmt)
            if isinstance(c, ast.Call) and _callee(c) == "run_in_executor"
        ]
        assert executor, "the release runs on the loop"
        assert "identity is not None" in source, "a directory with no identity is not held"
