"""Every seam that hands a session a project directory is accounted for.

The structural pin over the project-identity class. Review found that class one
seam at a time -- the restart re-pin, the side panel, the crewmate threads, the
warm pool, the ``session_create`` child, the folder-inheritance arm -- each a
path that handed a session a directory without the identity the binding had
verified. A session's spawn directory is ``<slot>.project``, so every such path
is one of two shapes, both found by AST rather than by reading, and a new one of
either shape lands on this module before it lands on a review.
"""

from __future__ import annotations

import ast
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
    """(1) A WRITER of ``<slot>.project`` in the dashboard package either RECORDS the
    identity of the directory it validated -- ``record_project_identity`` /
    ``inherit_project_identity`` or a copy of another slot's ``project_identity``,
    the identity derived from the fenced pin (``sandbox.directory_identity_pinned``,
    the fenced resolve's ``identity_out``) or from a record, never from a by-name
    ``stat`` -- or it is one of the enumerated writers whose spelling is the
    gateway's own configuration or a restore of a persisted placement, never a
    directory a request named: left record-less on purpose and re-pinned, or
    refused, at the first spawn (``spawn_project_identity_repinned``).

    (2) A PRODUCER that hands a process a directory: a call of the provider factory
    anywhere in the package that passes ``cwd``, or a ``cwd_identity=`` keyword. A
    factory door is the allocation body itself, resolves through
    ``_resolve_cwd_identity``, or is enumerated as spawning into a directory no
    slot binds; a ``cwd_identity`` producer takes the identity from the record and
    never pins fresh at spawn.

    The lists below are the whole population on the head this module was written
    against; the AST finds the population, the lists say why each member is right.
    """

    DASHBOARD = Path(chat_folders.__file__).resolve().parent
    PACKAGE = Path(kiro_crew.__file__).resolve().parent
    RECORDERS = ("record_project_identity", "inherit_project_identity")
    DERIVATIONS = ("directory_identity_pinned", "identity_out", "inherit_project_identity")
    FACTORY_CALLEES = ("_provider_factory", "provider_factory", "factory")

    #: Writers that record the identity with the spelling, at the write.
    RECORDS_AT_THE_WRITE = {
        ("chat_fork.py", "fork_slot"),
        ("chat_handlers.py", "api_chat_slot_agent"),
        ("chat_handlers.py", "api_chat_slot_create"),
        ("chat_handlers.py", "api_chat_slot_project"),
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
        ("chat_handlers.py", "_hydrate_slot_from_history"): "a restore from persisted metadata",
        ("chat_persistence.py", "_rehydrate_slot_from_history"): (
            "a restore from persisted metadata"
        ),
        ("chat_persistence.py", "_apply_recent_session"): "a restore from persisted metadata",
        ("handlers/members.py", "api_member_thread"): (
            "the member thread's project is the member's own configured workspace"
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
        """Factory doors passing ``cwd``, ``cwd_identity=`` producers, and runtime or
        client constructions passing ``work_dir`` -- package-wide.

        One sweep per class: the package is parsed once and the three populations
        are read off the same walk. A construction is recorded per call (its line
        and whether it also passes ``work_dir_identity``), because one function may
        build the runtime twice -- a first spawn and a respawn -- and each is its
        own spawn door.
        """
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
                    if _callee(sub) in cls.FACTORY_CALLEES and "cwd" in keywords:
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
        """The door the two sweeps above do not see: a runtime or client CONSTRUCTED
        with ``work_dir`` starts an agent process there, and the spawn verifies the
        directory only against the ``work_dir_identity`` the construction carries
        -- built without it, the spawn is unexamined (review-caught: the provider's
        resume path built its replacement runtime without the identity the first
        spawn carried, so a bound session whose runtime died during ``session/load``
        respawned unverified). Every construction passing ``work_dir`` also passes
        ``work_dir_identity``, per call, or its function is listed as spawning where
        no slot binds."""
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
