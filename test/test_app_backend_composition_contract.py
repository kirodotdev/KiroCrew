"""The app backend's composition: one facade over private supervision owners.

``kiro_crew.apps.backend`` is the backend's only import path and patch surface, and its
responsibilities live in ``kiro_crew.apps.backend_runtime``. For that split to be
invisible to every caller, each property below is pinned in the direction that would
catch a regression rather than in the direction that restates the code.

* Every name of the one-module backend resolves on the facade, to the object its owner
  holds (:data:`FROZEN_NAMES`, frozen rather than derived, because a list derived from
  the facade agrees with any facade).
* A name and the symbol it denotes cannot come apart: every module holding a name holds
  the same object -- the process table and its locks included -- and a write through
  the facade reaches all of them, which is what the suite's ``monkeypatch.setattr(bmod,
  ...)``, ``mock.patch("kiro_crew.apps.backend....")`` and ``bmod._processes.clear()``
  sites rely on.
* The owners form one acyclic stack under the facade and none imports it. The
  constructs repository guards pin to ``backend.py`` stay there, and exactly four call
  sites reach them through ``backend_runtime._facade()`` at call time.
* No test patches a forwarded name with ``create=True``, the one spelling the facade
  cannot undo.
"""

from __future__ import annotations

import ast
import functools
import importlib
import importlib.util
import inspect
import itertools
import json
import re
import sys
import types
from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path
from types import ModuleType, SimpleNamespace
from typing import Any, NamedTuple
from unittest import mock

import pytest
import source_corpus

from kiro_crew.apps import backend
from kiro_crew.apps.backend_runtime import (
    pidfile,
    ports,
    probe,
    provisioning,
    registration,
    restart,
    stale_reap,
    startup,
    supervision,
    termination,
    tracking,
)

# One group per file: the patch-spelling guard parses every test module, and the
# production re-export scan parses part of the package, once per worker otherwise.
pytestmark = pytest.mark.xdist_group(name="tree_scan_app_backend_composition")

_REPO_ROOT = Path(__file__).resolve().parents[1]
_RUNTIME_DIR = _REPO_ROOT / "src/kiro_crew/apps/backend_runtime"
_RUNTIME_PACKAGE = "kiro_crew.apps.backend_runtime"

#: The owners in the facade's declared layer order, lowest first.
PARTS: tuple[ModuleType, ...] = (
    tracking,
    probe,
    pidfile,
    ports,
    provisioning,
    termination,
    stale_reap,
    registration,
    restart,
    supervision,
    startup,
)

#: The backend's module-level surface: every name the one-module ``backend.py`` bound,
#: dunders excluded. A name leaves this list only when the symbol it names is
#: deliberately deleted.
FROZEN_NAMES: tuple[str, ...] = (
    "ActivationVerdict",
    "Any",
    "AppProcess",
    "ContextVar",
    "DEV_FLEET_APP_NAME",
    "GOVERNANCE_ERROR_REASON",
    "HealthProbeOutcome",
    "Iterator",
    "KIROCREW_SPAWNED_ENV",
    "KIROCREW_SPAWNED_VALUE",
    "KIROCREW_SPAWN_INSTANCE_ENV",
    "Literal",
    "MD_NOTEBOOK_APP_NAME",
    "Path",
    "PlatformCompositionError",
    "PortUnavailableError",
    "RLIMIT_PROFILE_BUILD",
    "RLIMIT_PROFILE_TOOL",
    "REQUIREMENTS_TXT_MAX_BYTES",
    "UTF8_TEXT",
    "_BOOT_SPAWN_MAX_WORKERS",
    "_BUILD_CAPABLE_APPS",
    "_BackendShutdownEvent",
    "_DEPS_ABI_NAME",
    "_DEPS_PIP_STDERR_TAIL",
    "_DEPS_PRIOR_NAME",
    "_DEPS_REQ_MAX_BYTES",
    "_DEPS_SPILL_HARD_CAP",
    "_DEPS_STAGING_NAME",
    "_DEPS_STAGING_SWEEP_RE",
    "_DEPS_STAMP_MAX_BYTES",
    "_DEPS_STAMP_NAME",
    "_DEV_FLEET_DEFERRED",
    "_HEALTH_CHECK_INTERVAL",
    "_HEALTH_CHECK_RETRIES",
    "_HEALTH_CHECK_TIMEOUT",
    "_HEALTH_PATH_RE",
    "_HEALTH_WATCH_FAILURES",
    "_HEALTH_WATCH_INTERVAL",
    "_LIFECYCLE_START",
    "_LIFECYCLE_STOP",
    "_MAX_PORT",
    "_MIN_PORT",
    "_PID_ANCESTRY_MAX_DEPTH",
    "_PORT_PROBE_TIMEOUT",
    "_PROBE_DETAIL_MAX_CHARS",
    "_PinnedDir",
    "_REAP_POLL_INTERVAL",
    "_REAP_SIGTERM_GRACE",
    "_RESTART_ON_EXIT_FAST_ATTEMPTS",
    "_RESTART_ON_EXIT_INITIAL_DELAY",
    "_RESTART_ON_EXIT_MAX_DELAY",
    "_RESTART_STABLE_SWEEPS",
    "_RESTART_STEADY_INTERVAL",
    "_SETTLE_UNRESOLVED_WARN_AFTER",
    "_SPAWN_SURVIVAL_CHECKS",
    "_SPAWN_SURVIVAL_INTERVAL",
    "_SpawnOwnershipLost",
    "_abi_shebang_of",
    "_activation_denied",
    "_advance_lifecycle_locked",
    "_allocated_ports",
    "_app_activation_denied",
    "_app_enabled_state",
    "_audit_provision_failure",
    "_await_inflight_spawn",
    "_capped_spill",
    "_capture_adopted_owners",
    "_claim_port",
    "_clear_failed_spawn_state",
    "_default_marker_environment",
    "_demote",
    "_deps_abi_tag",
    "_deps_boot_module",
    "_deps_boot_path",
    "_deps_digest",
    "_deps_tree_stamp_current",
    "_drain_exited_root_tree",
    "_drop_disabled_app_resources",
    "_find_free_port",
    "_find_node_binary",
    "_find_npm_binary",
    "_forget_app_pid",
    "_forget_app_pid_if",
    "_gate_mcp_registration",
    "_gateway_shutdown_event",
    "_health_check_loop",
    "_health_failure_hint",
    "_health_probe",
    "_health_probe_url",
    "_health_reconcile_lock",
    "_health_warn_lock",
    "_is_asgi_entry",
    "_is_shell_entry",
    "_lifecycle_generation",
    "_listening_pids",
    "_lock",
    "_open_contained_nofollow",
    "_pid_alive",
    "_pid_is_self_or_descendant_of",
    "_pidfile_lock",
    "_pidfile_path",
    "_pinned_ancestors",
    "_pinned_remove_entry",
    "_port_is_listening",
    "_preclaim_fixed_ports",
    "_probe_adoption_health",
    "_probe_failure_detail",
    "_proc_start_time",
    "_processes",
    "_promote",
    "_provision_app_deps_locked",
    "_read_installed",
    "_read_pidfile",
    "_reap_orphaned_backend_group",
    "_reap_stale_app_backends",
    "_rebind_adopted_owners",
    "_record_app_pid",
    "_requirements_volatile",
    "_reserve_free_port",
    "_resolve_nvm_path",
    "_restart_attempts",
    "_restart_exited_backend",
    "_retry_mcp_reconcile",
    "_revoke_if_ceiling_closed",
    "_set_backend_health",
    "_settle_superseding_start",
    "_shebang_argv",
    "_signal_backend_tree",
    "_spawn_owns_listener",
    "_spawn_publication_owner",
    "_start_adopted_health_watch",
    "_start_app_backend",
    "_start_app_backend_body",
    "_start_backends_concurrently",
    "_start_health_supervisor",
    "_supervise_backend_health",
    "_survived_spawn",
    "_terminate_retired_spawn",
    "_undo_promotion_of_disabled_app",
    "_wait_for_pids",
    "_warn_bad_health_path",
    "_warned_health_paths",
    "_warned_unconfined_cache",
    "_watch_backend_health",
    "_watch_backend_health_sweeps",
    "_write_pidfile",
    "_write_staging_marker",
    "annotations",
    "app_admission_denied",
    "app_backend_lifecycle_flock",
    "app_backend_visible_targets",
    "app_deps_dir",
    "app_dir",
    "app_enabled_state",
    "app_execution_denied",
    "atomic_write",
    "carveout_shadowed_by_foreign_mask",
    "cgroup_scope_argv",
    "concurrent",
    "config_dir",
    "contextlib",
    "dataclass",
    "field",
    "file_entry_point_refusal",
    "get_app_backend_port",
    "get_app_manifest",
    "get_app_process",
    "group_vouching_available",
    "hashlib",
    "health_reconcile_lock",
    "hmac",
    "http",
    "is_builtin_app",
    "is_module_style_entry_point",
    "json",
    "list_app_processes",
    "list_apps",
    "logger",
    "logging",
    "loopback_urlopen",
    "minimal_env",
    "os",
    "path_command_is_abi_matched",
    "pinned_fs",
    "platform",
    "platform_compat",
    "popen_limited",
    "provision_app_deps",
    "re",
    "recorded_backend_port",
    "redact_credentials",
    "redact_exfiltration_urls",
    "requirements_in_tree",
    "resolve_app_python",
    "retire_windows_app_tracking",
    "run_limited",
    "sel",
    "shipped_builtin_app_root",
    "shipped_builtin_module_path",
    "shutdown_event",
    "shutil",
    "signal_orphaned_spawn_group",
    "socket",
    "spawned_backend_names",
    "spawned_backend_owns_pid",
    "start_app_backend",
    "start_deferred_app_backends",
    "start_enabled_app_backends",
    "stat",
    "stop_app_backend",
    "subprocess",
    "sys",
    "sysconfig",
    "tempfile",
    "third_party_ceiling_closed",
    "threading",
    "time",
    "unstopped_backend_port",
    "urllib",
    "uuid",
    "wrap_argv",
)

#: Where each owner reaches a construct the facade keeps, and the one name it reads
#: there. Nothing else in the backend runtime refers to the facade.
FACADE_CALL_SITES: frozenset[tuple[str, str, str]] = frozenset(
    {
        ("termination", "_drain_exited_root_tree", "_pid_alive"),
        ("stale_reap", "_reap_stale_app_backends", "_pid_alive"),
        ("restart", "_restart_exited_backend", "_start_app_backend"),
        ("startup", "_start_backends_concurrently", "start_app_backend"),
    }
)

#: Every function the facade defines for the backend: the spawn transaction and the
#: entry-point helpers only it uses, and ``_pid_alive``. Repository guards read the
#: spawn in ``backend.py`` by path -- ``_start_app_backend_body`` in
#: ``test_internal_python_isolation.py``, ``_resolve_nvm_path`` in
#: ``test_spawn_audit.py``, ``_pid_alive`` in ``test_windows_kill_probe_audit.py``, and
#: the spawn's environment and record constructions as the facade's own text in six
#: more -- and the single-flight wrapper and its failure cleanup are the same
#: transaction.
FACADE_RESIDENTS: frozenset[str] = frozenset(
    {
        "start_app_backend",
        "_start_app_backend",
        "_clear_failed_spawn_state",
        "_start_app_backend_body",
        "_resolve_nvm_path",
        "_find_node_binary",
        "_find_npm_binary",
        "_is_asgi_entry",
        "_is_shell_entry",
        "_shebang_argv",
        "_abi_shebang_of",
        "_deps_boot_path",
        "_pid_alive",
    }
)

#: The composition machinery the facade defines at module level besides the residents
#: (its ``__getattr__`` sits under the ``TYPE_CHECKING`` guard's ``else``).
FACADE_MACHINERY: frozenset[str] = frozenset(
    {"_part", "_holder_tables", "_holders", "__dir__", "_Facade"}
)

#: The residents the facade calls into from the spawn transaction, and the text
#: read-only guards read in the facade's source: ``(guard, text)``.
GUARDED_FACADE_TEXT: tuple[tuple[str, str], ...] = (
    ("test_frontend_edition_build.py", '_platform_extra["KIROCREW_EDITION_DIR"]'),
    ("test_dev_fleet_app.py", "KIROCREW_HOME=str(config_dir())"),
    ("test_dev_fleet_app.py", '"KIROCREW_PROJECT_DIR"'),
    ("test_sandbox_dev_fleet_live_target.py", "_shipped_md_notebook"),
    ("test_app_bridges.py", "resolve_app_python("),
    ("test_app_backend.py", '_platform_extra["KIROCREW_DEVFLEET_REPO"]'),
)

#: Text the same guards require to be ABSENT from the facade's source. They read only
#: ``backend.py``, so the owners are held to it here: ``(guard, text)``.
FORBIDDEN_BACKEND_TEXT: tuple[tuple[str, str], ...] = (
    ("test_app_bridges.py", '".venv" / "bin" / "python3").is_file()'),
    ("test_sandbox_dev_fleet_live_target.py", "_shipped_dev_fleet"),
    ("test_frontend_edition_build.py", '_platform_extra["KIROCREW_ALLOW_EDITION"]'),
)

#: Names the owners rebind with ``global``, and the one module that may hold each.
GLOBAL_REBINDS: dict[str, ModuleType] = {
    "_warned_unconfined_cache": backend,
    "_DEV_FLEET_DEFERRED": startup,
}

_ABSENT = object()

#: A pid no platform allocates, for every fabricated process a case hands the backend:
#: a stubbed signal path that regressed would then find nothing to hit.
_UNALLOCATABLE_PID = 99_999_999_999


def _shared_names() -> list[str]:
    """Every non-dunder name held by the facade and an owner, or by two owners."""
    seen: dict[str, int] = {}
    for module in (backend, *PARTS):
        for name in vars(module):
            if not name.startswith("__"):
                seen[name] = seen.get(name, 0) + 1
    return sorted(name for name, count in seen.items() if count > 1)


def _holders(name: str) -> list[ModuleType]:
    """The modules whose own namespace binds ``name``."""
    return [module for module in (backend, *PARTS) if name in vars(module)]


def _bindings(name: str, holders: list[ModuleType]) -> list[object]:
    """What each of *holders* binds ``name`` to, ``None`` where it binds nothing."""
    return [vars(module).get(name) for module in holders]


def _put_back(name: str, original: object, holders: list[ModuleType]) -> None:
    """Write *original* straight into every holder.

    The cleanup of a case that drives the facade's undo: it goes around that undo, so
    a regression in it fails the one case instead of every later test in the process.
    """
    for module in holders:
        setattr(module, name, original)


@contextmanager
def _patched(kind: str, name: str, value: object) -> Iterator[None]:
    """Patch ``backend.<name>`` to *value* the way *kind* spells it, for one block."""
    if kind == "monkeypatch.setattr":
        with pytest.MonkeyPatch.context() as patched:
            patched.setattr(backend, name, value)
            yield
    else:
        with mock.patch.object(backend, name, new=value):
            yield


def _unwinds_like_a_flat_module(
    name: str, holders: list[ModuleType], steps: list[tuple[str, object]], label: str
) -> None:
    """Enter *steps* outermost first; every exit must restore what its enter replaced.

    That is what each spelling does on a module that binds the name itself, whatever
    the values: the same object written twice, or the original written back inside a
    patch.
    """
    if not steps:
        return
    (kind, value), rest = steps[0], steps[1:]
    before = _bindings(name, holders)
    with _patched(kind, name, value):
        assert _bindings(name, holders) == [value] * len(holders), label
        _unwinds_like_a_flat_module(name, holders, rest, label)
        assert _bindings(name, holders) == [value] * len(holders), label
    assert _bindings(name, holders) == before, label


def _source(module: ModuleType) -> str:
    return Path(module.__file__ or "").read_text(encoding="utf-8")


def _top_level_defs(module: ModuleType) -> set[str]:
    return {
        node.name
        for node in ast.parse(_source(module)).body
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef))
    }


@pytest.fixture
def clean_tables() -> Iterator[None]:
    """Empty lifecycle tables for one case, each restored afterwards whatever happens."""
    tables = (
        backend._processes,
        backend._allocated_ports,
        backend._restart_attempts,
        backend._lifecycle_generation,
    )
    saved = [dict(table) for table in tables]
    for table in tables:
        table.clear()
    try:
        yield
    finally:
        for table, before in zip(tables, saved):
            table.clear()
            table.update(before)


# ---------------------------------------------------------------------------
# The surface
# ---------------------------------------------------------------------------


class TestTheSurfaceSurvivesTheSplit:
    def test_the_frozen_inventory_is_not_empty(self) -> None:
        # An emptied list would make the case below pass while checking nothing.
        assert len(FROZEN_NAMES) > 200

    @pytest.mark.parametrize("name", FROZEN_NAMES)
    def test_every_name_the_module_bound_still_resolves_to_its_owners_object(
        self, name: str
    ) -> None:
        value = getattr(backend, name, _ABSENT)
        assert value is not _ABSENT, f"backend.{name} no longer resolves"
        for module in _holders(name):
            assert (
                vars(module)[name] is value
            ), f"backend.{name} answers a different object than {module.__name__} holds"

    def test_the_part_order_is_the_facades_and_covers_the_package(self) -> None:
        # The facade resolves a read from the first owner in this order that holds the
        # name, so an owner missing from it is one whose names the facade cannot reach.
        assert backend._PART_MODULES == tuple(part.__name__ for part in PARTS)
        on_disk = {path.stem for path in _RUNTIME_DIR.glob("*.py") if path.stem != "__init__"}
        assert on_disk == {part.__name__.rpartition(".")[2] for part in PARTS}

    def test_an_exported_name_is_read_from_its_owner_on_every_access(self) -> None:
        # Not bound in the facade, so a value written straight into the owner is what
        # the facade answers. (It is not what the owner's IMPORTERS see -- only a write
        # through the facade reaches them -- which is why tests patch the facade.)
        assert "_pidfile_path" in backend._EXPORTS
        assert "_pidfile_path" not in vars(backend)
        replacement = object()
        with pytest.MonkeyPatch.context() as patched:
            patched.setattr(pidfile, "_pidfile_path", replacement)
            assert backend._pidfile_path is replacement

    def test_a_global_rebind_has_exactly_one_holder(self) -> None:
        # A ``global`` rebind writes its module's namespace directly and never reaches
        # ``_Facade``, so a rebound name held by a second module would freeze there.
        # The two flags the backend rebinds are each held by the one module that
        # rebinds them, and nothing else is rebound.
        rebound: dict[str, set[str]] = {}
        for module in (backend, *PARTS):
            for node in ast.walk(ast.parse(_source(module))):
                if isinstance(node, ast.Nonlocal):
                    rebound.setdefault("<nonlocal>", set()).add(module.__name__)
                if isinstance(node, ast.Global):
                    for name in node.names:
                        rebound.setdefault(name, set()).add(module.__name__)
        assert rebound == {name: {m.__name__} for name, m in GLOBAL_REBINDS.items()}
        for name, module in GLOBAL_REBINDS.items():
            assert _holders(name) == [module], name

    def test_a_missing_name_is_an_attribute_error(self) -> None:
        # ``hasattr``, ``getattr(..., default)`` and ``mock.patch`` all rely on it.
        assert not hasattr(backend, "_no_such_backend_name")
        with pytest.raises(AttributeError):
            backend.no_such_backend_name  # noqa: B018

    def test_dir_lists_the_exported_names(self) -> None:
        assert set(FROZEN_NAMES) <= set(dir(backend))

    def test_a_star_import_carries_exactly_the_public_names_of_the_inventory(
        self, tmp_path: Path
    ) -> None:
        # ``__all__`` is derived, and a star import consults it and never the module
        # ``__getattr__``. The machinery binds only private names, so nothing it needs
        # leaks into a star importer's namespace and nothing public goes missing. The
        # star import runs in a real module loaded from ``tmp_path``, the one place a
        # star import is legal.
        public = {name for name in FROZEN_NAMES if not name.startswith("_")}
        assert set(backend.__all__) == public
        probe_path = tmp_path / "backend_star_probe.py"
        probe_path.write_text("from kiro_crew.apps.backend import *\n", encoding="utf-8")
        spec = importlib.util.spec_from_file_location("backend_star_probe", probe_path)
        assert spec and spec.loader
        star = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(star)
        carried = {name for name in vars(star) if not name.startswith("__")}
        assert carried == public
        for name in public:
            assert vars(star)[name] is getattr(backend, name)

    def test_the_pinned_residents_stay_in_the_facade(self) -> None:
        for name in FACADE_RESIDENTS:
            assert name in vars(backend), f"{name} left backend.py"
            assert getattr(backend, name).__module__ == backend.__name__
            assert name not in backend._EXPORTS
            assert not any(name in vars(part) for part in PARTS)
        # ... and the facade defines nothing else for the backend.
        assert _top_level_defs(backend) == FACADE_RESIDENTS | FACADE_MACHINERY
        assert callable(vars(backend)["__getattr__"])

    def test_the_text_the_guards_read_by_path_is_in_the_facade(self) -> None:
        source = _source(backend)
        for guard, text in GUARDED_FACADE_TEXT:
            assert text in source, f"{guard} reads {text!r} in backend.py"
        assert source.count("resolve_app_python(") >= 2

    def test_every_record_construction_is_in_the_facade(self) -> None:
        # ``test_app_off_contract.py`` reads the spawn and adoption record constructions
        # (their ``gateway_started`` and ``admitted_builtin`` provenance) in the facade's
        # source alone, so a construction written in an owner would escape its
        # ratchets. Every one stays in the spawn body.
        def constructions(module: ModuleType) -> list[ast.Call]:
            return [
                node
                for node in ast.walk(ast.parse(_source(module)))
                if isinstance(node, ast.Call)
                and isinstance(node.func, ast.Name)
                and node.func.id == "AppProcess"
            ]

        facade_sites = constructions(backend)
        started = [s for s in facade_sites if any(k.arg == "gateway_started" for k in s.keywords)]
        assert len(started) == 2
        for part in PARTS:
            assert constructions(part) == [], f"{part.__name__} constructs an AppProcess"

    def test_the_text_the_guards_forbid_is_in_no_backend_module(self) -> None:
        for module in (backend, *PARTS):
            source = _source(module)
            for guard, text in FORBIDDEN_BACKEND_TEXT:
                assert text not in source, f"{guard} forbids {text!r}; {module.__name__} has it"

    def test_only_the_provisioning_owner_calls_a_redactor(self) -> None:
        # ``NON_EGRESS_REDACTION_MODULES`` names ``apps/backend_runtime/provisioning.py``
        # as the backend's one redactor caller (the pip stderr scrub), and
        # ``test_security_posture.py`` fails on a caller missing from it.
        call = re.compile(r"\bStreamRedactor\(|\b\w*redact\w*\(|\.redact\(")
        callers = {module.__name__ for module in (backend, *PARTS) if call.search(_source(module))}
        assert callers == {provisioning.__name__}


# ---------------------------------------------------------------------------
# One symbol per name, and writes that reach every binding of it
# ---------------------------------------------------------------------------

#: An exported name several owners import: defined in ``probe``.
_MULTI_HOLDER = "_health_probe"


class TestOneNamespaceForWrites:
    @pytest.mark.parametrize("name", _shared_names())
    def test_every_module_holding_a_name_holds_the_same_object(self, name: str) -> None:
        values = {id(vars(module)[name]) for module in _holders(name)}
        assert (
            len(values) == 1
        ), f"{name} names different objects in {[m.__name__ for m in _holders(name)]}"

    @pytest.mark.parametrize("name", _shared_names())
    def test_a_write_through_the_facade_reaches_every_holder_and_is_undone(self, name: str) -> None:
        holders = [module for module in _holders(name) if module is not backend]
        original = getattr(backend, name)
        sentinel = object()
        with pytest.MonkeyPatch.context() as patched:
            patched.setattr(backend, name, sentinel)
            assert getattr(backend, name) is sentinel
            for module in holders:
                assert vars(module)[name] is sentinel, f"{module.__name__}.{name} was missed"
        assert getattr(backend, name) is original
        for module in holders:
            assert vars(module)[name] is original, f"{module.__name__}.{name} not restored"

    def test_the_multi_holder_case_is_what_it_claims(self) -> None:
        holders = [module for module in _holders(_MULTI_HOLDER) if module is not backend]
        assert _MULTI_HOLDER in backend._EXPORTS and len(holders) > 3

    def test_the_process_table_is_one_object_every_owner_reads(self) -> None:
        # Seven consumer fixtures ``.clear()`` it through the facade, and a provenance
        # test replaces it outright: both only work while every owner holds the one
        # dict the facade answers.
        holders = _holders("_processes")
        assert tracking in holders and len(holders) > 5
        assert all(vars(module)["_processes"] is backend._processes for module in holders)
        assert "_processes" in backend._ALSO_HELD

    def test_mock_patch_of_an_exported_name_restores_every_holder(self) -> None:
        # ``mock.patch`` sees a name the facade does not bind as non-local, so its exit
        # deletes the name and writes the original back; both halves go through here.
        holders = [module for module in _holders(_MULTI_HOLDER) if module is not backend]
        original = getattr(backend, _MULTI_HOLDER)
        with mock.patch.object(backend, _MULTI_HOLDER) as fake:
            for module in holders:
                assert vars(module)[_MULTI_HOLDER] is fake
        for module in holders:
            assert vars(module)[_MULTI_HOLDER] is original

    def test_a_dotted_string_patch_restores_every_holder(self) -> None:
        # The spelling most of the suite uses: ``mock.patch("kiro_crew.apps.backend.X")``.
        holders = [module for module in _holders(_MULTI_HOLDER) if module is not backend]
        original = getattr(backend, _MULTI_HOLDER)
        with mock.patch(f"{backend.__name__}.{_MULTI_HOLDER}") as fake:
            for module in holders:
                assert vars(module)[_MULTI_HOLDER] is fake
        for module in holders:
            assert vars(module)[_MULTI_HOLDER] is original

    def test_every_nesting_of_the_patch_harnesses_unwinds_like_a_flat_module(self) -> None:
        # Four deep over ``mock.patch`` and ``monkeypatch.setattr``, with the original
        # and one fake as the values, so a patch writing back what an enclosing patch
        # replaced, or the same object twice, is covered. Each harness restores what it
        # read at its own enter, so no nesting depends on the facade pairing anything.
        holders = [module for module in _holders(_MULTI_HOLDER) if module is not backend]
        original = getattr(backend, _MULTI_HOLDER)
        fake = object()
        kinds = ("mock.patch", "monkeypatch.setattr")
        steps = [(kind, value) for kind in kinds for value in (original, fake)]
        try:
            for depth in range(1, 5):
                for sequence in itertools.product(steps, repeat=depth):
                    label = " > ".join(
                        f"{kind}({'original' if value is original else 'fake'})"
                        for kind, value in sequence
                    )
                    _unwinds_like_a_flat_module(_MULTI_HOLDER, holders, list(sequence), label)
        finally:
            _put_back(_MULTI_HOLDER, original, holders)

    def test_a_name_the_facade_also_binds_unwinds_through_every_holder(self) -> None:
        # ``_processes`` is bound here for the spawn AND imported by every owner that
        # acts on the table; ``mock.patch`` reads it as local and writes it back.
        name = "_processes"
        assert name in backend._ALSO_HELD and name in vars(backend)
        holders = _holders(name)
        assert len(holders) > 3
        original = getattr(backend, name)
        with mock.patch.object(backend, name, create=True) as fake:
            assert _bindings(name, holders) == [fake] * len(holders)
        assert _bindings(name, holders) == [original] * len(holders)

    def test_create_true_on_a_forwarded_name_deletes_it_from_every_holder(self) -> None:
        # The one patch spelling the facade cannot undo: ``mock.patch`` sees a forwarded
        # name as non-local, and under ``create=True`` its exit is the delete alone. The
        # guard in ``TestPatchSpellings`` keeps that spelling out of the suite, with this
        # case its one allowlisted site: it keeps the guard's premise true, and fails
        # the day the facade can undo it.
        holders = [module for module in _holders(_MULTI_HOLDER) if module is not backend]
        original = getattr(backend, _MULTI_HOLDER)
        try:
            with mock.patch.object(backend, _MULTI_HOLDER, create=True):
                pass
            assert _bindings(_MULTI_HOLDER, holders) == [None] * len(holders)
        finally:
            _put_back(_MULTI_HOLDER, original, holders)

    def test_shadowing_a_builtin_through_the_facade_reaches_every_part(self) -> None:
        # One namespace for writes includes the builtins a module can shadow.
        def fake_sorted(*args: Any, **kwargs: Any) -> list[Any]:
            return []

        with pytest.MonkeyPatch.context() as patched:
            patched.setattr(backend, "sorted", fake_sorted, raising=False)
            for module in (backend, *PARTS):
                assert vars(module)["sorted"] is fake_sorted
        for module in (backend, *PARTS):
            assert "sorted" not in vars(module)

    def test_patching_the_facades_sys_does_not_redirect_its_own_resolution(self) -> None:
        # ``backend.sys`` is part of the surface (the spawn reads ``sys.executable``),
        # and patching it must not change where the facade finds its owners: the
        # machinery reads ``sys`` through an alias.
        def fake_url(port: int, health_path: str) -> str:
            return "http://127.0.0.1:1/patched"

        with pytest.MonkeyPatch.context() as patched:
            patched.setattr(backend, "sys", mock.MagicMock(executable="/opt/test/python"))
            assert backend._pidfile_path is pidfile._pidfile_path
            patched.setattr(backend, "_health_probe_url", fake_url)
            assert probe._health_probe_url is fake_url

    def test_a_delete_and_restore_through_the_facade_round_trips(self) -> None:
        # ``mock.patch`` undoes a name the facade does not bind by DELETING it and then
        # writing the original back, so both halves have to reach every holder.
        holders = [module for module in _holders(_MULTI_HOLDER) if module is not backend]
        original = getattr(backend, _MULTI_HOLDER)
        with pytest.MonkeyPatch.context() as patched:
            patched.delattr(backend, _MULTI_HOLDER)
            assert not hasattr(backend, _MULTI_HOLDER)
            for module in holders:
                assert _MULTI_HOLDER not in vars(module)
        for module in holders:
            assert vars(module)[_MULTI_HOLDER] is original

    def test_every_part_logs_through_the_facades_logger(self) -> None:
        # Log routing, filters and ``caplog.at_level(..., logger="kiro_crew.apps.backend")``
        # captures key on the facade's name.
        for part in PARTS:
            assert part.logger is backend.logger, part.__name__
        assert backend.logger.name == backend.__name__ == "kiro_crew.apps.backend"


# ---------------------------------------------------------------------------
# A patch on the facade reaches the call site in whichever owner makes the call
# ---------------------------------------------------------------------------


class _Sel:
    """A SEL stand-in that records nothing and never touches a data home."""

    def log_api_access(self, **kwargs: Any) -> None:
        return None


class TestAPatchOnTheFacadeReachesEveryCallSite:
    def test_one_probe_patch_reaches_adoption_the_startup_poll_and_the_stop(
        self, monkeypatch: pytest.MonkeyPatch, clean_tables: None
    ) -> None:
        # ``_health_probe`` is defined in ``probe`` and called from ``ports`` (the
        # adoption probe), ``supervision`` (the startup poll) and ``termination`` (a
        # stop under a withdrawn ceiling), each through the binding it imported.
        seen: list[tuple[int, str]] = []

        def fake_probe(port: int, health_path: str, **kwargs: Any) -> Any:
            seen.append((port, health_path))
            return backend.HealthProbeOutcome(None, "refused by the test")

        monkeypatch.setattr(backend, "_health_probe", fake_probe)
        monkeypatch.setattr(backend, "sel", lambda: _Sel())
        # ports: the adoption probe.
        assert backend._probe_adoption_health(9101, "/adopt") is False
        # supervision: one startup attempt, then exhaustion.
        polled = backend.AppProcess(app_name="poll-app", port=9102)
        backend._processes["poll-app"] = polled
        monkeypatch.setattr(backend, "_HEALTH_CHECK_RETRIES", 1)
        monkeypatch.setattr(backend, "_HEALTH_CHECK_INTERVAL", 0)
        monkeypatch.setattr(backend, "_revoke_if_ceiling_closed", lambda *a: "proceed")
        monkeypatch.setattr(backend, "_set_backend_health", lambda *a, **k: True)
        assert backend._health_check_loop(polled, "/poll") is None
        # termination: an adopted record none of whose PIDs confirm, stopped under a
        # withdrawn ceiling, is settled by one probe of its port.
        adopted = backend.AppProcess(
            app_name="stop-app",
            port=9103,
            adopted_pids=[_UNALLOCATABLE_PID],
            adopted_start_times={_UNALLOCATABLE_PID: "t"},
        )
        backend._processes["stop-app"] = adopted
        monkeypatch.setattr(backend, "_proc_start_time", lambda pid: None)
        monkeypatch.setattr(backend, "_forget_app_pid", lambda name: None)
        monkeypatch.setattr(backend.platform_compat, "pid_exists", lambda pid: False)
        assert backend.stop_app_backend("stop-app", _retry_if_serving="/stop") is True
        assert seen == [(9101, "/adopt"), (9102, "/poll"), (9103, "/stop")]

    def test_a_replaced_process_table_is_what_every_owner_reads(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        table = {"t-app": backend.AppProcess(app_name="t-app", port=9110, healthy=True)}
        monkeypatch.setattr(backend, "_processes", table)
        assert backend.get_app_backend_port("t-app") == 9110  # tracking
        assert backend.recorded_backend_port("t-app") == 9110  # ports
        assert [row["app_name"] for row in backend.list_app_processes()] == ["t-app"]
        assert backend.spawned_backend_names() == []  # an unspawned record
        for module in _holders("_processes"):
            assert vars(module)["_processes"] is table

    @pytest.mark.skipif(
        sys.platform == "win32", reason="only the POSIX drain arm polls member liveness"
    )
    def test_the_liveness_probe_is_resolved_on_the_facade_when_the_drain_runs(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        # ``termination._drain_exited_root_tree`` reaches the facade's ``_pid_alive``
        # through ``backend_runtime._facade()``, so a patch of the facade's own binding
        # is what the drain polls: once in the grace loop, once in the final census.
        polled: list[int] = []
        monkeypatch.setattr(backend, "group_vouching_available", lambda: True)
        monkeypatch.setattr(
            backend,
            "signal_orphaned_spawn_group",
            lambda pgid, sig, instance, expected=None: (
                {_UNALLOCATABLE_PID: None},
                {_UNALLOCATABLE_PID: None},
            ),
        )
        monkeypatch.setattr(backend, "_pid_alive", lambda pid: polled.append(pid) or False)
        monkeypatch.setattr(backend.platform_compat, "pgroup_exists", lambda pgid: False)
        root = SimpleNamespace(pid=_UNALLOCATABLE_PID)
        assert backend._drain_exited_root_tree("drain-app", root, None, "token") is True
        assert polled == [_UNALLOCATABLE_PID, _UNALLOCATABLE_PID]

    def test_the_restart_spawns_through_the_facades_start(
        self, monkeypatch: pytest.MonkeyPatch, clean_tables: None
    ) -> None:
        # ``restart._restart_exited_backend`` reaches the facade's ``_start_app_backend``
        # through ``backend_runtime._facade()`` when it runs.
        exited = backend.AppProcess(
            app_name="r-app",
            pid=_UNALLOCATABLE_PID,
            proc=SimpleNamespace(pid=_UNALLOCATABLE_PID, poll=lambda: 1, returncode=1),
        )
        backend._processes["r-app"] = exited
        replacement = backend.AppProcess(app_name="r-app", port=9120)
        started: list[str] = []

        def fake_start(app_name: str) -> Any:
            started.append(app_name)
            backend._processes[app_name] = replacement
            return replacement

        monkeypatch.setattr(backend, "_app_enabled_state", lambda name: True)
        monkeypatch.setattr(
            backend, "shutdown_event", SimpleNamespace(is_set=lambda: False, wait=lambda d: False)
        )
        monkeypatch.setattr(backend, "_forget_app_pid_if", lambda *a: None)
        monkeypatch.setattr(backend, "_drain_exited_root_tree", lambda *a: True)
        monkeypatch.setattr(backend, "_activation_denied", lambda *a: backend.ActivationVerdict())
        monkeypatch.setattr(backend, "sel", lambda: _Sel())
        monkeypatch.setattr(backend, "_start_app_backend", fake_start)
        assert backend._restart_exited_backend(exited, 1) is True
        assert started == ["r-app"]

    def test_the_boot_wave_submits_the_facades_start(self, monkeypatch: pytest.MonkeyPatch) -> None:
        # ``startup._start_backends_concurrently`` reaches the facade's
        # ``start_app_backend`` through ``backend_runtime._facade()`` when it submits.
        monkeypatch.setattr(backend, "_preclaim_fixed_ports", lambda names: None)
        monkeypatch.setattr(
            backend,
            "start_app_backend",
            lambda name: backend.AppProcess(app_name=name, port=9130),
        )
        assert backend._start_backends_concurrently(["boot-app"]) == ["boot-app"]

    def test_a_spawn_body_patch_is_what_the_single_flight_start_runs(
        self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path, clean_tables: None
    ) -> None:
        # The facade's own transaction: the public start calls the body the facade
        # binds, so the suite's many body replacements keep reaching it.
        monkeypatch.setenv("KIROCREW_HOME", str(tmp_path))
        manifest = SimpleNamespace(backend=SimpleNamespace(entryPoint="server.py"))
        monkeypatch.setattr(backend, "get_app_manifest", lambda name: manifest)
        record = backend.AppProcess(app_name="body-app", port=9140)

        def fake_body(app_name: str, seen_manifest: Any) -> Any:
            assert seen_manifest is manifest
            return record

        monkeypatch.setattr(backend, "_start_app_backend_body", fake_body)
        assert backend.start_app_backend("body-app") is record


# ---------------------------------------------------------------------------
# Every global a backend function reads is one a facade write reaches
# ---------------------------------------------------------------------------


def _function_global_reads(module: ModuleType) -> list[tuple[str, str]]:
    """``(function, name)`` for each module-global name a function in *module* reads.

    Read off the module's symbol table, so a nested function, a method, a lambda and a
    comprehension are all covered: whatever resolves ``name`` as a global of *module*.
    """
    import builtins
    import symtable

    found: list[tuple[str, str]] = []

    def walk(table: symtable.SymbolTable, where: str) -> None:
        for child in table.get_children():
            label = f"{where}.{child.get_name()}" if where else child.get_name()
            for symbol in child.get_symbols():
                if symbol.is_global() and (symbol.is_referenced() or symbol.is_assigned()):
                    found.append((label, symbol.get_name()))
            walk(child, label)

    walk(symtable.symtable(_source(module), module.__file__ or "", "exec"), "")
    return [(fn, name) for fn, name in found if name not in vars(builtins)]


class TestEverySeamReachesItsCallSite:
    @pytest.mark.parametrize("module", (backend, *PARTS), ids=lambda m: m.__name__)
    def test_a_facade_write_of_any_global_a_function_reads_reaches_that_function(
        self, module: ModuleType
    ) -> None:
        # A function resolves a global through ITS module's namespace, so a patch of
        # ``backend.<name>`` is seen there only if the facade writes that module. For the
        # facade's own functions the name must be bound here (a forwarded name read as a
        # bare global would never see the write); for an owner's, the owner must be one
        # of the holders a facade write reaches.
        unreached: list[str] = []
        for function, name in _function_global_reads(module):
            if module is backend:
                if name not in vars(backend) or name in backend._EXPORTS:
                    unreached.append(f"{function}:{name}")
            elif module.__name__ not in backend._holders(name):
                unreached.append(f"{function}:{name}")
        assert unreached == [], f"{module.__name__} reads names a facade write misses"

    def test_the_seam_reader_sees_nested_scopes(self) -> None:
        reads = _function_global_reads(supervision)
        assert ("_watch_backend_health_sweeps", "_HEALTH_WATCH_INTERVAL") in reads
        assert any(name == "_processes" for _fn, name in reads)
        facade_reads = _function_global_reads(backend)
        assert ("_start_app_backend_body", "_warned_unconfined_cache") in facade_reads


# ---------------------------------------------------------------------------
# Layering and placement
# ---------------------------------------------------------------------------


def _engine_imports(source: str) -> set[str]:
    """Every backend module an owner's source imports: a lower owner, or the facade.

    Walks the whole tree, so an import inside a function counts, and resolves a
    relative import against the runtime package, so ``from .. import backend`` and
    ``from . import tracking`` are seen for what they name.
    """
    engine = {backend.__name__, *(part.__name__ for part in PARTS)}
    found: set[str] = set()
    for node in ast.walk(ast.parse(source)):
        if isinstance(node, ast.ImportFrom):
            base = node.module or ""
            if node.level:
                base = importlib.util.resolve_name("." * node.level + base, _RUNTIME_PACKAGE)
            for alias in node.names:
                for candidate in (f"{base}.{alias.name}", base):
                    if candidate in engine:
                        found.add(candidate)
                        break
        elif isinstance(node, ast.Import):
            found.update(alias.name for alias in node.names if alias.name in engine)
    return found


def _facade_reads(source: str) -> set[tuple[str, str]]:
    """``(enclosing function, attribute)`` for each ``_facade().<attr>`` in *source*."""
    tree = ast.parse(source)
    parents: dict[ast.AST, ast.AST] = {}
    for node in ast.walk(tree):
        for child in ast.iter_child_nodes(node):
            parents[child] = node
    found: set[tuple[str, str]] = set()
    for node in ast.walk(tree):
        if not (isinstance(node, ast.Call) and getattr(node.func, "id", None) == "_facade"):
            continue
        parent = parents.get(node)
        attribute = parent.attr if isinstance(parent, ast.Attribute) else "<bare>"
        scope = parents.get(node)
        while scope is not None and not isinstance(scope, (ast.FunctionDef, ast.AsyncFunctionDef)):
            scope = parents.get(scope)
        found.add((scope.name if scope is not None else "<module>", attribute))
    return found


class TestLayering:
    def test_the_import_reader_sees_every_spelling_of_an_engine_import(self) -> None:
        # A reader that saw only absolute module strings would pass an owner importing
        # the facade lazily or relatively, which is the violation it exists to catch.
        source = (
            "from . import tracking\n"
            "from .probe import _health_probe\n"
            "def f():\n"
            "    from ..backend import _pid_alive\n"
            "    from .. import backend\n"
            f"import {_RUNTIME_PACKAGE}.startup\n"
        )
        assert _engine_imports(source) == {
            f"{_RUNTIME_PACKAGE}.tracking",
            f"{_RUNTIME_PACKAGE}.probe",
            f"{_RUNTIME_PACKAGE}.startup",
            backend.__name__,
        }

    def test_an_owner_imports_only_owners_below_it_and_never_the_facade(self) -> None:
        order = [part.__name__ for part in PARTS]
        for index, part in enumerate(PARTS):
            imported = _engine_imports(_source(part))
            assert backend.__name__ not in imported, f"{part.__name__} imports the facade"
            later = imported - set(order[:index])
            assert later == set(), f"{part.__name__} imports an owner at or above it: {later}"

    def test_exactly_the_pinned_call_sites_reach_the_facade(self) -> None:
        # Each reaches one facade resident, from one function, at call time. Nothing
        # else names the facade, so no other owner depends on it at all.
        found = {
            (part.__name__.rpartition(".")[2], function, attribute)
            for part in PARTS
            for function, attribute in _facade_reads(_source(part))
        }
        assert found == FACADE_CALL_SITES
        assert {attribute for _module, _function, attribute in found} <= FACADE_RESIDENTS
        for part in PARTS:
            constants = {
                node.value
                for node in ast.walk(ast.parse(_source(part)))
                if isinstance(node, ast.Constant) and isinstance(node.value, str)
            }
            assert backend.__name__ not in constants, f"{part.__name__} spells the facade"

    def test_the_facade_helper_is_only_ever_called_in_place(self) -> None:
        # An alias (``f = _facade``) or a stored module would hide a fifth call site
        # from the reader above, so every reference to ``_facade`` in an owner is its
        # import or the callee of a call.
        for part in PARTS:
            tree = ast.parse(_source(part))
            callees = {
                id(node.func)
                for node in ast.walk(tree)
                if isinstance(node, ast.Call) and isinstance(node.func, ast.Name)
            }
            stray = [
                node.lineno
                for node in ast.walk(tree)
                if isinstance(node, ast.Name) and node.id == "_facade" and id(node) not in callees
            ]
            assert stray == [], f"{part.__name__} refers to _facade at lines {stray}"

    def test_the_call_site_reader_can_fail(self) -> None:
        assert _facade_reads("def f():\n    return _facade()._pid_alive(1)\n") == {
            ("f", "_pid_alive")
        }
        assert _facade_reads("x = _facade()\n") == {("<module>", "<bare>")}
        assert _facade_reads("def f():\n    return facade()._pid_alive(1)\n") == set()

    def test_the_runtime_helper_answers_the_facade(self) -> None:
        from kiro_crew.apps import backend_runtime

        assert backend_runtime._FACADE == backend.__name__
        assert backend_runtime._facade() is backend

    def test_the_runtime_helper_imports_only_a_purged_facade(self) -> None:
        # ``sys.modules`` answers first; ``import_module`` is asked only on a miss, and
        # then for the facade by name. Run as a copy of the helper bound to stand-in
        # ``sys`` and ``importlib``, so the process's module table is never touched
        # while other threads may be resolving the facade.
        from kiro_crew.apps import backend_runtime

        stand_in = ModuleType(backend.__name__)
        imported: list[str] = []

        def fake_import(name: str) -> ModuleType:
            imported.append(name)
            return stand_in

        def detached(modules: dict[str, ModuleType]) -> Any:
            scope = {
                "sys": SimpleNamespace(modules=modules),
                "importlib": SimpleNamespace(import_module=fake_import),
                "_FACADE": backend_runtime._FACADE,
            }
            return types.FunctionType(backend_runtime._facade.__code__, scope)()

        assert detached({backend.__name__: backend}) is backend
        assert imported == []
        assert detached({}) is stand_in
        assert imported == [backend.__name__]

    def test_the_facade_resolves_owners_by_name_not_by_module_object(self) -> None:
        # A table of module objects is a second place a module is stored; an owner
        # purged and imported again would then be reached through the stale copy.
        for table in (backend._EXPORTS, backend._ALSO_HELD):
            for holders in table.values():
                assert all(isinstance(holder, str) for holder in holders)
        assert inspect.getsource(backend._part).count("importlib.import_module(") == 1
        assert importlib.import_module(backend._PART_MODULES[0]) is tracking


# ---------------------------------------------------------------------------
# The facade's own code, as a type checker and a patched importlib see it
# ---------------------------------------------------------------------------


def _bare_loads(source: str) -> list[tuple[int, str]]:
    """``(line, name)`` for every bare-global read of a forwarded name in *source*.

    Any ``ast.Name`` in Load context whose id is a key of ``_EXPORTS``, at any depth,
    outside an ``import`` / ``from ... import`` node -- so the ``TYPE_CHECKING``
    imports are allowed and every other bare use is not.
    """

    class _Loads(ast.NodeVisitor):
        def __init__(self) -> None:
            self.found: list[tuple[int, str]] = []

        def visit_Import(self, node: ast.Import) -> None:
            return

        visit_ImportFrom = visit_Import  # type: ignore[assignment]

        def visit_Name(self, node: ast.Name) -> None:
            if isinstance(node.ctx, ast.Load) and node.id in backend._EXPORTS:
                self.found.append((node.lineno, node.id))

    loads = _Loads()
    loads.visit(ast.parse(source))
    return loads.found


def _type_checking_imports(source: str) -> dict[str, str]:
    """``name -> module`` for each ``from ... import`` under a module-level type guard."""
    found: dict[str, str] = {}
    for statement in ast.parse(source).body:
        if not isinstance(statement, ast.If):
            continue
        test = ast.unparse(statement.test)
        if test not in ("TYPE_CHECKING", "typing.TYPE_CHECKING", "_typing.TYPE_CHECKING"):
            continue
        for node in statement.body:
            if isinstance(node, ast.ImportFrom) and node.module:
                found.update(dict.fromkeys((a.asname or a.name for a in node.names), node.module))
    return found


class TestTheFacadesOwnCode:
    def test_the_facade_reads_no_forwarded_name_as_a_bare_global(self) -> None:
        # A function defined here resolves a bare global through this module's own
        # namespace, which ``__getattr__`` never sees: a forwarded name read that way
        # is a ``NameError`` on whatever path reaches it.
        assert _bare_loads(_source(backend)) == []

    def test_the_bare_read_check_can_fail(self) -> None:
        assert _bare_loads("def f():\n    return _pidfile_path()\n") == [(2, "_pidfile_path")]
        assert (
            _bare_loads(
                "if TYPE_CHECKING:\n"
                "    from kiro_crew.apps.backend_runtime.pidfile import _pidfile_path\n"
                "def f():\n"
                "    return _part('x')._pidfile_path()\n"
            )
            == []
        )

    def test_the_type_checker_sees_every_exported_name(self) -> None:
        # ``__getattr__`` is hidden from the checker, so the names it serves at run
        # time are declared to it under ``TYPE_CHECKING``; a name missing there would
        # type as an error at a correct call site, and an extra one would hide a stale
        # name.
        declared = _type_checking_imports(_source(backend))
        assert declared == {name: holders[0] for name, holders in backend._EXPORTS.items()}

    def test_the_type_checking_reader_can_fail(self) -> None:
        source = _source(backend)
        assert source.count("        _pidfile_path,\n") == 1
        dropped = _type_checking_imports(source.replace("        _pidfile_path,\n", ""))
        assert "_pidfile_path" not in dropped
        assert _type_checking_imports(
            "from a import x\nif DEBUG:\n    from b import y\n"
            "if typing.TYPE_CHECKING:\n    from c import z as w\n"
        ) == {"w": "c"}

    def test_a_loaded_owner_is_read_written_and_restored_without_import_module(
        self,
    ) -> None:
        owner = sys.modules[backend._EXPORTS[_MULTI_HOLDER][0]]
        original = vars(owner)[_MULTI_HOLDER]
        sentinel = object()
        with mock.patch.object(
            importlib, "import_module", side_effect=AssertionError("resolution imported")
        ) as refused:
            assert getattr(backend, _MULTI_HOLDER) is original
            with pytest.MonkeyPatch.context() as patched:
                patched.setattr(backend, _MULTI_HOLDER, sentinel)
                assert vars(owner)[_MULTI_HOLDER] is sentinel
                assert getattr(backend, _MULTI_HOLDER) is sentinel
            assert vars(owner)[_MULTI_HOLDER] is original
            assert refused.call_count == 0
            # The refusal sits on the binding the miss path calls.
            with pytest.raises(AssertionError, match="resolution imported"):
                backend._part("kiro_crew._backend_absent_probe")
            assert refused.call_count == 1

    def test_the_facade_call_sites_resolve_without_import_module(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        # The boot wave reaches the facade's start through ``backend_runtime._facade()``.
        # A test that stubs ``importlib.import_module`` for its own reasons must not be
        # able to answer it.
        from kiro_crew.apps import backend_runtime

        monkeypatch.setattr(backend, "_preclaim_fixed_ports", lambda names: None)
        monkeypatch.setattr(
            backend, "start_app_backend", lambda name: backend.AppProcess(app_name=name)
        )
        with mock.patch.object(
            importlib, "import_module", side_effect=AssertionError("facade imported")
        ) as refused:
            assert backend_runtime._facade() is backend
            started = backend._start_backends_concurrently(["resolve-app"])
        assert refused.call_count == 0
        assert started == ["resolve-app"]


# ---------------------------------------------------------------------------
# A reload of the facade re-executes the whole backend
# ---------------------------------------------------------------------------

_RELOAD_PROBE = """
import importlib, json, sys
from kiro_crew.apps import backend
from kiro_crew.apps.backend_runtime import probe, tracking
before_record = tracking.AppProcess
before_table = tracking._processes
importlib.reload(backend)
modules = [backend, *(sys.modules[m] for m in backend._PART_MODULES)]
report = {
    "source": backend.__file__,
    "rebuilt": tracking.AppProcess is not before_record,
    "fresh_table": tracking._processes is not before_table,
    "facade_follows": backend.AppProcess is tracking.AppProcess
    and backend._processes is tracking._processes,
    "probe_follows": backend._health_probe is probe._health_probe,
    "split": sorted(
        name
        for name in set().union(*(vars(m) for m in modules))
        if not name.startswith("__")
        and len({id(vars(m)[name]) for m in modules if name in vars(m)}) > 1
    ),
}
print(json.dumps(report))
"""


def test_a_reload_of_the_facade_reloads_every_owner(tmp_path: Path) -> None:
    # The one-module backend re-evaluated every module-level value, the process table
    # included, on ``importlib.reload``. Run in a child interpreter, so the reload's new
    # objects never reach this worker's other tests.
    import os
    import subprocess

    from kiro_crew.subprocess_utf8 import UTF8_TEXT

    # The child imports this checkout's package, as the parent does through pytest's
    # ``pythonpath``, not whichever ``kiro_crew`` its interpreter would otherwise find.
    source = _REPO_ROOT / "src"
    env = {
        **os.environ,
        "KIROCREW_HOME": str(tmp_path / "data"),
        "PYTHONPATH": os.pathsep.join([str(source), os.environ.get("PYTHONPATH", "")]),
    }
    completed = subprocess.run(
        [sys.executable, "-c", _RELOAD_PROBE],
        cwd=tmp_path,
        env=env,
        capture_output=True,
        timeout=90,
        check=False,
        **UTF8_TEXT,
    )
    assert completed.returncode == 0, completed.stderr
    report = json.loads(completed.stdout.strip().splitlines()[-1])
    assert Path(report.pop("source")).resolve() == Path(backend.__file__).resolve()
    assert report == {
        "rebuilt": True,
        "fresh_table": True,
        "facade_follows": True,
        "probe_follows": True,
        "split": [],
    }


# ---------------------------------------------------------------------------
# The patch spellings the suite may use on the facade
# ---------------------------------------------------------------------------

#: The patch callables by the dotted path they resolve to: which one, and the index of
#: ``create`` among its positional parameters.
_PATCH_CALLABLES = {
    "unittest.mock.patch": ("patch", 3),
    "unittest.mock.patch.object": ("object", 4),
    "unittest.mock.patch.multiple": ("multiple", 2),
}

#: ``patch.multiple`` keywords that configure the patch rather than name an attribute.
_MULTIPLE_OPTIONS = frozenset({"target", "spec", "create", "spec_set", "autospec", "new_callable"})

#: What a hit names when the patched attribute cannot be read off the source.
_DYNAMIC = "<dynamic>"

#: The one deliberate ``create=True`` patch of a forwarded name, keyed by file and the
#: test enclosing it: the premise case, which shows that such a patch still deletes the
#: name and puts every holder back itself.
_ALLOWED_CREATE_TRUE = frozenset(
    {
        (
            "test/test_app_backend_composition_contract.py",
            "TestOneNamespaceForWrites."
            "test_create_true_on_a_forwarded_name_deletes_it_from_every_holder",
        )
    }
)

#: A value an expression may denote: a dotted ``path`` (a module, or an attribute reached
#: from one), a ``str``, or the known leading ``prefix`` of a string.
_Value = tuple[str, str]


class _Hit(NamedTuple):
    function: str
    name: str
    line: int


class _Resolver:
    """What the names in one module's source may denote, read off its AST alone.

    A name is bound by an import, or by a plain assignment in its function's scope or an
    enclosing one, followed to a fixed point; a name bound more than once may denote any
    of its values. An expression the reader cannot follow denotes nothing.
    """

    def __init__(
        self,
        tree: ast.Module,
        module: str | None,
        reexports: frozenset[str],
        *,
        is_package: bool = False,
    ) -> None:
        self._tree = tree
        # A package's ``__init__`` resolves ``from . import x`` against itself.
        self._package = (module if is_package else module.rpartition(".")[0]) if module else None
        self._reexports = reexports
        self._parents: dict[ast.AST, ast.AST] = {}
        self._bindings: dict[ast.AST, dict[str, list[ast.AST | frozenset[_Value]]]] = {}
        stack: list[ast.AST] = [tree]
        while stack:
            node = stack.pop()
            for child in ast.iter_child_nodes(node):
                self._parents[child] = node
                stack.append(child)
            if isinstance(node, (ast.Import, ast.ImportFrom)):
                for name, path in self._imported(node):
                    self._bind(node, name, frozenset({("path", path)}))
            elif isinstance(node, ast.Assign):
                for target in node.targets:
                    if isinstance(target, ast.Name):
                        self._bind(node, target.id, node.value)
            elif isinstance(node, ast.AnnAssign) and node.value is not None:
                if isinstance(node.target, ast.Name):
                    self._bind(node, node.target.id, node.value)
        self._following: set[tuple[int, str]] = set()
        self._known: dict[tuple[int, str], set[_Value]] = {}
        # Set while resolving a name whose chain looped back on itself: a result
        # computed then is partial, so it is returned but not remembered.
        self._cut = False

    def _imported(self, node: ast.Import | ast.ImportFrom) -> list[tuple[str, str]]:
        if isinstance(node, ast.Import):
            return [
                (alias.asname, alias.name) if alias.asname else (alias.name.split(".")[0],) * 2
                for alias in node.names
            ]
        module = node.module or ""
        if node.level:
            if self._package is None:
                return []
            module = importlib.util.resolve_name("." * node.level + module, self._package)
        return [(alias.asname or alias.name, f"{module}.{alias.name}") for alias in node.names]

    def _bind(self, statement: ast.AST, name: str, value: ast.AST | frozenset[_Value]) -> None:
        self._bindings.setdefault(self.scope_of(statement), {}).setdefault(name, []).append(value)

    def scope_of(self, node: ast.AST) -> ast.AST:
        """The function whose body holds *node*, or the module; a decorator is outside."""
        child, parent = node, self._parents.get(node)
        while parent is not None:
            if isinstance(parent, (ast.FunctionDef, ast.AsyncFunctionDef, ast.Lambda)):
                decorators = getattr(parent, "decorator_list", [])
                if not any(child is decorator for decorator in decorators):
                    return parent
            child, parent = parent, self._parents.get(parent)
        return self._tree

    def function_of(self, node: ast.AST) -> str:
        """The dotted class and function names enclosing *node*, ``<module>`` for none."""
        names = []
        parent = self._parents.get(node)
        while parent is not None:
            if isinstance(parent, (ast.ClassDef, ast.FunctionDef, ast.AsyncFunctionDef)):
                names.append(parent.name)
            parent = self._parents.get(parent)
        return ".".join(reversed(names)) or "<module>"

    def module_names(self) -> list[str]:
        return list(self._bindings.get(self._tree, {}))

    def is_facade(self, value: _Value) -> bool:
        kind, text = value
        return kind == "path" and (text == backend.__name__ or text in self._reexports)

    def values(self, expr: ast.AST | None, scope: ast.AST) -> set[_Value]:
        if expr is None:
            return set()
        if isinstance(expr, ast.Constant):
            return {("str", expr.value)} if isinstance(expr.value, str) else set()
        if isinstance(expr, ast.Name):
            return self._name(expr.id, scope)
        if isinstance(expr, ast.Attribute):
            found: set[_Value] = set()
            for value in self.values(expr.value, scope):
                if value[0] == "path":
                    found.add(("path", f"{value[1]}.{expr.attr}"))
                    if expr.attr == "__name__" and self.is_facade(value):
                        found.add(("str", backend.__name__))
            return found
        if isinstance(expr, ast.Call) and len(expr.args) == 1:
            if ("path", "importlib.import_module") in self.values(expr.func, scope):
                return {
                    ("path", text)
                    for kind, text in self.values(expr.args[0], scope)
                    if kind == "str"
                }
            return set()
        if isinstance(expr, ast.JoinedStr):
            return self._joined(expr.values, scope)
        if isinstance(expr, ast.FormattedValue):
            plain = expr.conversion == -1 and expr.format_spec is None
            return self.values(expr.value, scope) if plain else set()
        if isinstance(expr, ast.BinOp) and isinstance(expr.op, ast.Add):
            return self._joined([expr.left, expr.right], scope)
        return set()

    def _joined(self, parts: list[ast.expr], scope: ast.AST) -> set[_Value]:
        text = ""
        for part in parts:
            values = self.values(part, scope)
            strings = {value for kind, value in values if kind == "str"}
            prefixes = {value for kind, value in values if kind == "prefix"}
            if len(strings) == 1 and not prefixes:
                text += strings.pop()
                continue
            if len(prefixes) == 1 and not strings:
                text += prefixes.pop()
            return {("prefix", text)} if text else set()
        return {("str", text)}

    def _name(self, name: str, scope: ast.AST) -> set[_Value]:
        key = (id(scope), name)
        if key in self._known:
            return self._known[key]
        if key in self._following:
            self._cut = True
            return set()
        self._following.add(key)
        outer_cut, self._cut = self._cut, False
        try:
            found: set[_Value] = set()
            for binding_scope in self._chain(scope):
                bound = self._bindings.get(binding_scope, {}).get(name)
                if bound is not None:
                    for value in bound:
                        found |= (
                            value
                            if isinstance(value, frozenset)
                            else self.values(value, binding_scope)
                        )
                    break
            if not self._cut:
                self._known[key] = found
            return found
        finally:
            self._following.discard(key)
            self._cut = outer_cut or self._cut

    def _chain(self, scope: ast.AST) -> Iterator[ast.AST]:
        while scope is not self._tree:
            yield scope
            scope = self.scope_of(scope)
        yield self._tree

    def hits(self, call: ast.Call) -> list[str]:
        """The forwarded names *call* patches with ``create`` not literally ``False``."""
        scope = self.scope_of(call)
        callable_ = next(
            (
                _PATCH_CALLABLES[text]
                for kind, text in self.values(call.func, scope)
                if text in _PATCH_CALLABLES
            ),
            None,
        )
        if callable_ is None:
            return []
        kind, create_at = callable_
        keywords = {keyword.arg: keyword.value for keyword in call.keywords if keyword.arg}
        splat = any(keyword.arg is None for keyword in call.keywords)
        create = keywords.get(
            "create", call.args[create_at] if len(call.args) > create_at else None
        )
        if create is None and splat:
            # A ``**`` splat may carry ``create``: unknown is not literally False.
            create = ast.Name(id="<splat>")
        if create is None or (isinstance(create, ast.Constant) and create.value is False):
            return []
        target = keywords.get("target", call.args[0] if call.args else None)
        targets = self.values(target, scope)
        prefix = backend.__name__ + "."
        found: set[str] = set()
        if kind == "patch":
            for value_kind, text in targets:
                rest = text[len(prefix) :] if text.startswith(prefix) else None
                if rest is None or "." in rest:
                    continue
                found.add(rest if value_kind == "str" else _DYNAMIC)
        elif kind == "object":
            if any(self.is_facade(value) for value in targets):
                attribute = keywords.get("attribute", call.args[1] if len(call.args) > 1 else None)
                names = {
                    text
                    for value_kind, text in self.values(attribute, scope)
                    if value_kind == "str"
                }
                found = names if names else {_DYNAMIC}
        elif (
            any(self.is_facade(value) for value in targets) or ("str", backend.__name__) in targets
        ):
            found = {name for name in keywords if name not in _MULTIPLE_OPTIONS}
            if any(keyword.arg is None for keyword in call.keywords):
                found.add(_DYNAMIC)
        return sorted(name for name in found if name == _DYNAMIC or name in backend._EXPORTS)


def _module_name(path: Path) -> str | None:
    """The dotted import name of a file under ``src/``; a file elsewhere has none."""
    relative = path.relative_to(_REPO_ROOT)
    if relative.parts[0] != "src":
        return None
    parts = list(relative.with_suffix("").parts[1:])
    if parts[-1] == "__init__":
        parts.pop()
    return ".".join(parts)


def _is_package(path: Path) -> bool:
    return path.name == "__init__.py"


def _in_a_test_directory(path: Path) -> bool:
    return any(part.endswith("tests") for part in path.relative_to(_REPO_ROOT).parts[:-1])


def _python_files() -> Iterator[tuple[Path, str]]:
    """``(path, text)`` for every ``.py`` file the checkout holds that names the backend.

    Enumerated the way git sees the checkout (``source_corpus.repo_files``), so a
    nested worktree or scratch copy never stands in for the file it copies.
    """
    for path in source_corpus.repo_files_named(".py"):
        relative = path.relative_to(_REPO_ROOT).as_posix()
        if not relative.startswith(("test/", "src/")):
            continue
        text = path.read_text(encoding="utf-8", errors="replace")
        if "backend" in text:
            yield path, text


@functools.lru_cache(maxsize=1)
def _facade_reexports() -> frozenset[str]:
    """Dotted paths that name the facade through a production module importing it.

    Read off the source the same way the guard reads a test, to a fixed point, so a
    module re-exporting another's alias is covered, and a new re-export needs no edit
    here.
    """
    candidates: list[tuple[str, bool, ast.Module]] = []
    for path, text in _python_files():
        if _module_name(path) is None or _in_a_test_directory(path):
            continue
        if "kiro_crew.apps" in text or "apps" in path.relative_to(_REPO_ROOT).parts:
            candidates.append((_module_name(path) or "", _is_package(path), ast.parse(text)))
    found: frozenset[str] = frozenset()
    while True:
        grown = set(found)
        for module, is_package, tree in candidates:
            resolver = _Resolver(tree, module, found, is_package=is_package)
            for name in resolver.module_names():
                if any(resolver.is_facade(v) for v in resolver.values(ast.Name(id=name), tree)):
                    grown.add(f"{module}.{name}")
        if grown == found:
            return found
        found = frozenset(grown)


def _may_pass_create(call: ast.Call) -> bool:
    """Whether *call* could hand a patch a ``create`` that is not literally ``False``.

    A ``create`` keyword that is not the literal ``False``, a ``**`` splat that could
    carry one, or three positional arguments or more -- where ``create`` could sit
    positionally under any alias of a patch callable. Only these calls are worth
    resolving; which callable a call names is the resolver's question.
    """
    for keyword in call.keywords:
        if keyword.arg == "create":
            return not (isinstance(keyword.value, ast.Constant) and keyword.value.value is False)
    return any(keyword.arg is None for keyword in call.keywords) or len(call.args) >= 3


def _create_true_patches_of_forwarded_names(
    source: str,
    module: str | None = None,
    reexports: frozenset[str] | None = None,
    *,
    is_package: bool = False,
) -> list[_Hit]:
    """Every ``mock.patch`` of a forwarded name in *source* whose ``create`` is not
    literally ``False``.

    Any spelling the reader can resolve counts: ``patch``, ``patch.object`` and
    ``patch.multiple`` reached through any import alias, called or used as a
    decorator, with a positional or keyword target and attribute, a keyword
    ``create`` -- or a positional one to a callable spelled ``patch``, ``object`` or
    ``multiple`` -- and a string target built from the facade's name. A patch of the
    facade whose attribute it cannot resolve is a ``<dynamic>`` hit, never a pass. A
    name the facade binds itself is safe -- ``mock.patch`` sees it as local and
    writes the original back.
    """
    tree = ast.parse(source)
    calls = [
        node for node in ast.walk(tree) if isinstance(node, ast.Call) and _may_pass_create(node)
    ]
    if not calls:
        return []
    resolver = _Resolver(
        tree,
        module,
        _facade_reexports() if reexports is None else reexports,
        is_package=is_package,
    )
    return [
        _Hit(resolver.function_of(call), name, call.lineno)
        for call in calls
        for name in resolver.hits(call)
    ]


def _patch_sources() -> Iterator[tuple[Path, str]]:
    """``(path, text)`` for every test module of the repository that may patch the facade.

    ``test/`` and every directory under ``src/`` whose name ends in ``tests``, handed
    to the AST reader when the text names ``backend`` and a patch helper. The word
    ``create`` is not required: it can arrive positionally or through a ``**`` splat.
    """
    for path, text in _python_files():
        relative = path.relative_to(_REPO_ROOT).as_posix()
        if not (relative.startswith("test/") or _in_a_test_directory(path)):
            continue
        if "patch" in text or "mock" in text:
            yield path, text


#: Imports most reader cases share.
_CASE_IMPORTS = "from unittest import mock\nfrom kiro_crew.apps import backend\n"

#: ``(id, source, module, expected names)`` for the reader: each form the guard must
#: catch beside a spelling of it the guard must leave alone. ``@EXP@`` is a forwarded
#: name, ``@BOUND@`` one the facade binds itself, ``@FACADE@`` the facade's dotted name.
_READER_CASES: list[tuple[str, str, str | None, list[str]]] = [
    (
        "patch.object",
        _CASE_IMPORTS + "mock.patch.object(backend, '@EXP@', create=True)\n",
        None,
        ["@EXP@"],
    ),
    (
        "create=False",
        _CASE_IMPORTS + "mock.patch.object(backend, '@EXP@', create=False)\n",
        None,
        [],
    ),
    ("no create", _CASE_IMPORTS + "mock.patch.object(backend, '@EXP@')\n", None, []),
    (
        "create positionally",
        _CASE_IMPORTS + "mock.patch.object(backend, '@EXP@', mock.DEFAULT, None, True)\n",
        None,
        ["@EXP@"],
    ),
    (
        "False positionally",
        _CASE_IMPORTS + "mock.patch.object(backend, '@EXP@', mock.DEFAULT, None, False)\n",
        None,
        [],
    ),
    (
        "create not a literal",
        _CASE_IMPORTS + "flag = True\nmock.patch.object(backend, '@EXP@', create=flag)\n",
        None,
        ["@EXP@"],
    ),
    (
        "a name the facade binds",
        _CASE_IMPORTS + "mock.patch.object(backend, '@BOUND@', create=True)\n",
        None,
        [],
    ),
    (
        "a name no module holds",
        _CASE_IMPORTS + "mock.patch.object(backend, '_no_such_backend_helper', create=True)\n",
        None,
        [],
    ),
    (
        "another module",
        _CASE_IMPORTS
        + "from kiro_crew import config\nmock.patch.object(config, '@EXP@', create=True)\n",
        None,
        [],
    ),
    (
        "another module's .backend",
        _CASE_IMPORTS
        + "from kiro_crew import config\n"
        + "mock.patch.object(config.backend, '@EXP@', create=True)\n",
        None,
        [],
    ),
    (
        "the facade module imported by its path",
        "from unittest import mock\nimport kiro_crew.apps.backend as reg\n"
        "mock.patch.object(reg, '@EXP@', create=True)\n",
        None,
        ["@EXP@"],
    ),
    (
        "the facade module reached through its package",
        "from unittest import mock\nimport kiro_crew.apps\n"
        "mock.patch.object(kiro_crew.apps.backend, '@EXP@', create=True)\n",
        None,
        ["@EXP@"],
    ),
    (
        "patch as an alias",
        "from unittest.mock import patch as P\nfrom kiro_crew.apps import backend\n"
        "P.object(backend, '@EXP@', create=True)\n",
        None,
        ["@EXP@"],
    ),
    (
        "a local function named patch",
        "from kiro_crew.apps import backend\ndef patch(*a, **k):\n    return None\n"
        "patch.object(backend, '@EXP@', create=True)\n",
        None,
        [],
    ),
    (
        "mock as an alias",
        "from unittest import mock as M\nfrom kiro_crew.apps import backend\n"
        "M.patch.object(backend, '@EXP@', create=True)\n",
        None,
        ["@EXP@"],
    ),
    (
        "unittest.mock as an alias",
        "import unittest.mock as um\num.patch('@FACADE@.@EXP@', create=True)\n",
        None,
        ["@EXP@"],
    ),
    (
        "unittest.mock by its path",
        "import unittest.mock\nfrom kiro_crew.apps import backend\n"
        "unittest.mock.patch.multiple(backend, create=True, @EXP@=None)\n",
        None,
        ["@EXP@"],
    ),
    (
        "a third-party mock",
        "import mock\nfrom kiro_crew.apps import backend\n"
        "mock.patch.object(backend, '@EXP@', create=True)\n",
        None,
        [],
    ),
    (
        "as a decorator",
        _CASE_IMPORTS
        + "@mock.patch.object(backend, '@EXP@', create=True)\ndef test_x(fake):\n    pass\n",
        None,
        ["@EXP@"],
    ),
    (
        "a decorator with create=False",
        _CASE_IMPORTS
        + "@mock.patch.object(backend, '@EXP@', create=False)\ndef test_x(fake):\n    pass\n",
        None,
        [],
    ),
    (
        "a dotted string",
        "from unittest import mock\nmock.patch('@FACADE@.@EXP@', create=True)\n",
        None,
        ["@EXP@"],
    ),
    (
        "a deeper dotted string",
        "from unittest import mock\nmock.patch('@FACADE@.os.utime', create=True)\n",
        None,
        [],
    ),
    (
        "another dotted string",
        "from unittest import mock\nmock.patch('kiro_crew.config.@EXP@', create=True)\n",
        None,
        [],
    ),
    (
        "an f-string of __name__",
        _CASE_IMPORTS + "mock.patch(f'{backend.__name__}.@EXP@', create=True)\n",
        None,
        ["@EXP@"],
    ),
    (
        "an f-string of another __name__",
        _CASE_IMPORTS
        + "from kiro_crew import config\nmock.patch(f'{config.__name__}.@EXP@', create=True)\n",
        None,
        [],
    ),
    (
        "an f-string of a constant",
        "from unittest import mock\nFACADE = '@FACADE@'\n"
        "mock.patch(f'{FACADE}.@EXP@', create=True)\n",
        None,
        ["@EXP@"],
    ),
    (
        "an f-string of another constant",
        "from unittest import mock\nOTHER = 'kiro_crew.config'\n"
        "mock.patch(f'{OTHER}.@EXP@', create=True)\n",
        None,
        [],
    ),
    (
        "a constant concatenated",
        "from unittest import mock\nFACADE = '@FACADE@'\n"
        "mock.patch(FACADE + '.@EXP@', create=True)\n",
        None,
        ["@EXP@"],
    ),
    (
        "__name__ concatenated",
        _CASE_IMPORTS + "mock.patch(backend.__name__ + '.@EXP@', create=True)\n",
        None,
        ["@EXP@"],
    ),
    (
        "another constant concatenated",
        "from unittest import mock\nOTHER = 'kiro_crew.config'\n"
        "mock.patch(OTHER + '.@EXP@', create=True)\n",
        None,
        [],
    ),
    (
        "keyword target and attribute",
        _CASE_IMPORTS + "mock.patch.object(target=backend, attribute='@EXP@', create=True)\n",
        None,
        ["@EXP@"],
    ),
    (
        "keyword target elsewhere",
        _CASE_IMPORTS
        + "from kiro_crew import config\n"
        + "mock.patch.object(target=config, attribute='@EXP@', create=True)\n",
        None,
        [],
    ),
    (
        "keyword string target",
        "from unittest import mock\nmock.patch(target='@FACADE@.@EXP@', create=True)\n",
        None,
        ["@EXP@"],
    ),
    (
        "an unresolved attribute",
        _CASE_IMPORTS + "def test_x(attr):\n    mock.patch.object(backend, attr, create=True)\n",
        None,
        [_DYNAMIC],
    ),
    (
        "an unresolved attribute, create=False",
        _CASE_IMPORTS + "def test_x(attr):\n    mock.patch.object(backend, attr, create=False)\n",
        None,
        [],
    ),
    (
        "a resolved local attribute",
        _CASE_IMPORTS
        + "def test_x():\n    name = '@EXP@'\n    mock.patch.object(backend, name, create=True)\n",
        None,
        ["@EXP@"],
    ),
    (
        "an f-string it cannot finish",
        _CASE_IMPORTS
        + "def test_x(attr):\n    mock.patch(f'{backend.__name__}.{attr}', create=True)\n",
        None,
        [_DYNAMIC],
    ),
    (
        "an aliased patch with a positional create",
        "from unittest.mock import patch as P\nfrom kiro_crew.apps import backend\n"
        "P.object(backend, '@EXP@', None, None, True)\n",
        None,
        ["@EXP@"],
    ),
    (
        "an aliased dotted-string patch with a positional create",
        "from unittest.mock import patch as P\nP('@FACADE@.@EXP@', None, None, True)\n",
        None,
        ["@EXP@"],
    ),
    (
        "a splat that may carry create",
        _CASE_IMPORTS + "def test_x(opts):\n    mock.patch.object(backend, '@EXP@', **opts)\n",
        None,
        ["@EXP@"],
    ),
    (
        "a splat on another module",
        _CASE_IMPORTS
        + "from kiro_crew import config\n"
        + "def test_x(opts):\n    mock.patch.object(config, '@EXP@', **opts)\n",
        None,
        [],
    ),
    (
        "an alias whose chain loops back on itself",
        _CASE_IMPORTS
        + "a = backend\na = b\nb = a\n"
        + "mock.patch.object(a, '@EXP@', create=True)\n"
        + "mock.patch.object(b, '@EXP@', create=True)\n",
        None,
        ["@EXP@", "@EXP@"],
    ),
    (
        "**kwargs in patch.multiple",
        _CASE_IMPORTS + "def test_x(kw):\n    mock.patch.multiple(backend, create=True, **kw)\n",
        None,
        [_DYNAMIC],
    ),
    (
        "**kwargs elsewhere",
        _CASE_IMPORTS
        + "from kiro_crew import config\n"
        + "def test_x(kw):\n    mock.patch.multiple(config, create=True, **kw)\n",
        None,
        [],
    ),
    (
        "patch.multiple of a dotted string",
        "from unittest import mock\nmock.patch.multiple('@FACADE@', create=True, @EXP@=None)\n",
        None,
        ["@EXP@"],
    ),
    (
        "patch.multiple of another string",
        "from unittest import mock\n"
        "mock.patch.multiple('kiro_crew.config', create=True, @EXP@=None)\n",
        None,
        [],
    ),
    (
        "import_module",
        "import importlib\nfrom unittest import mock\n"
        "facade = importlib.import_module('@FACADE@')\n"
        "mock.patch.object(facade, '@EXP@', create=True)\n",
        None,
        ["@EXP@"],
    ),
    (
        "import_module elsewhere",
        "import importlib\nfrom unittest import mock\n"
        "other = importlib.import_module('kiro_crew.config')\n"
        "mock.patch.object(other, '@EXP@', create=True)\n",
        None,
        [],
    ),
    (
        "a chain of assignments",
        _CASE_IMPORTS + "a = backend\nb = a\nmock.patch.object(b, '@EXP@', create=True)\n",
        None,
        ["@EXP@"],
    ),
    (
        "an assignment inside the test",
        _CASE_IMPORTS
        + "def test_x():\n    reg = backend\n    mock.patch.object(reg, '@EXP@', create=True)\n",
        None,
        ["@EXP@"],
    ),
    (
        "a relative import in a package",
        "from unittest import mock\nfrom ... import backend\n"
        "mock.patch.object(backend, '@EXP@', create=True)\n",
        "kiro_crew.apps.builtins.tests.test_case",
        ["@EXP@"],
    ),
    (
        "a relative import elsewhere",
        "from unittest import mock\nfrom ..crew import backend\n"
        "mock.patch.object(backend, '@EXP@', create=True)\n",
        "kiro_crew.apps.builtins.tests.test_case",
        [],
    ),
]

#: A production module that re-exports the facade under another name, for the two
#: cases below: the scan finds none today, so the reader is shown one.
_SYNTHETIC_REEXPORTS = frozenset({"kiro_crew.apps.routes.backend_mod"})

_REEXPORT_CASES: list[tuple[str, str, list[str]]] = [
    (
        "an assigned re-export",
        "from unittest import mock\nfrom kiro_crew.apps import routes\n"
        "def test_x():\n    reg = routes.backend_mod\n"
        "    mock.patch.object(reg, '@EXP@', create=True)\n",
        ["@EXP@"],
    ),
    (
        "an assigned other attribute",
        "from unittest import mock\nfrom kiro_crew.apps import routes\n"
        "def test_x():\n    reg = routes.backend_other\n"
        "    mock.patch.object(reg, '@EXP@', create=True)\n",
        [],
    ),
]


def _case(template: str) -> str:
    return (
        template.replace("@EXP@", _MULTI_HOLDER)
        .replace("@BOUND@", "_start_app_backend_body")
        .replace("@FACADE@", backend.__name__)
    )


class TestPatchSpellings:
    def test_the_case_names_are_what_they_claim(self) -> None:
        assert _MULTI_HOLDER in backend._EXPORTS
        assert "_start_app_backend_body" in vars(backend)
        assert "_start_app_backend_body" not in backend._EXPORTS
        assert "_no_such_backend_helper" not in backend._EXPORTS
        assert not hasattr(backend, "_no_such_backend_helper")

    def test_every_production_alias_the_scan_finds_is_the_facade(self) -> None:
        # Production code imports names from the facade (``routes.py``, ``teardown.py``,
        # the dashboard server) or imports it inside a function (``bridges.py``), so the
        # scan finds no module-level alias of the module itself; a
        # new one extends the guard's reach with no edit here, and whatever it finds
        # must really be the facade at run time.
        for dotted in _facade_reexports():
            module, _, name = dotted.rpartition(".")
            assert getattr(importlib.import_module(module), name) is backend, dotted

    @pytest.mark.parametrize(
        ("is_package", "expected"),
        [(True, [_MULTI_HOLDER]), (False, [])],
        ids=["a package __init__", "a plain module of the same name"],
    )
    def test_a_relative_import_resolves_against_the_right_package(
        self, is_package: bool, expected: list[str]
    ) -> None:
        # ``from ... import backend`` in ``kiro_crew/apps/builtins/tests/__init__.py``
        # names ``kiro_crew.apps.backend``; in a plain ``kiro_crew/apps/builtins/tests.py``
        # it names ``kiro_crew.backend``, which is not the facade.
        source = _case(
            "from unittest import mock\nfrom ... import backend\n"
            "mock.patch.object(backend, '@EXP@', create=True)\n"
        )
        hits = _create_true_patches_of_forwarded_names(
            source, "kiro_crew.apps.builtins.tests", is_package=is_package
        )
        assert [hit.name for hit in hits] == expected

    @pytest.mark.parametrize(
        ("source", "module", "expected"),
        [(case[1], case[2], case[3]) for case in _READER_CASES],
        ids=[case[0] for case in _READER_CASES],
    )
    def test_the_reader_flags_every_spelling_and_only_those(
        self, source: str, module: str | None, expected: list[str]
    ) -> None:
        # A reader that missed a spelling would pass a suite using it; one that flagged
        # a safe spelling would stop a patch that undoes cleanly.
        hits = _create_true_patches_of_forwarded_names(_case(source), module)
        assert [hit.name for hit in hits] == [_case(name) for name in expected]

    @pytest.mark.parametrize(
        ("source", "expected"),
        [(case[1], case[2]) for case in _REEXPORT_CASES],
        ids=[case[0] for case in _REEXPORT_CASES],
    )
    def test_the_reader_follows_a_production_re_export(
        self, source: str, expected: list[str]
    ) -> None:
        hits = _create_true_patches_of_forwarded_names(
            _case(source), None, reexports=_SYNTHETIC_REEXPORTS
        )
        assert [hit.name for hit in hits] == [_case(name) for name in expected]

    def test_no_test_patches_a_forwarded_name_with_create_true(self) -> None:
        # ``mock.patch`` undoes a name the facade forwards by deleting it, and under
        # ``create=True`` the delete is the whole undo: the name is gone from every
        # backend module for the rest of the run, and a later test fails far from
        # here. The scan must find exactly the allowlisted premise case, so the
        # allowlist can neither hide a second site nor outlive the one it names.
        hits = [
            (path.relative_to(_REPO_ROOT).as_posix(), hit)
            for path, text in _patch_sources()
            for hit in _create_true_patches_of_forwarded_names(
                text, _module_name(path), is_package=_is_package(path)
            )
        ]
        found = {(path, hit.function) for path, hit in hits}
        # One hit exactly: the premise case holds a single patch, so a second site
        # added inside the same function is not hidden by the allowlist.
        assert len(hits) == len(_ALLOWED_CREATE_TRUE), hits
        unexpected = [
            f"{path}:{hit.line} {hit.function} patches {hit.name}"
            for path, hit in hits
            if (path, hit.function) not in _ALLOWED_CREATE_TRUE
        ]
        assert found == _ALLOWED_CREATE_TRUE, (
            "mock.patch(..., create=True) of a name kiro_crew.apps.backend forwards to "
            "its owner deletes that name from every backend module when the patch "
            "exits. Drop create=True (the name exists) or patch it with "
            f"monkeypatch.setattr: {unexpected}; allowlisted but not found: "
            f"{sorted(_ALLOWED_CREATE_TRUE - found)}"
        )
