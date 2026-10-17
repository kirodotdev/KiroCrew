"""``kiro_prerequisite.py`` keeps its BASE surface while its code lives in sibling owners.

``kiro_crew.kiro_prerequisite`` was one module. Its code now lives in ten sibling
``kiro_prerequisite_<role>`` owners, and the module itself is a
:mod:`kiro_crew._facade_mirror` facade, so a patch of ``kiro_crew.kiro_prerequisite.<name>``
reaches every owner that reads the name. These tests freeze what the split must not
change, measured on the one-module file:

* every module-level name it bound still resolves through the facade, one object per name;
* ``KiroPrerequisiteService`` keeps every member with the same kind, coroutine-ness and
  signature, and each method bound from a collaborator IS that collaborator's function,
  reads as a method of the class (``__qualname__``, no ``self`` annotation at run time)
  and does not depend on the class body it left (no zero-argument ``super()``,
  ``__class__`` or name-mangled attribute);
* every owner logs to the facade's logger, nothing outside the family imports an owner,
  and every name the facade serves from outside the family is visible to type checkers.

Each structural check has a self-test that shows it can fail.
"""

from __future__ import annotations

import ast
import importlib
import inspect
import logging
from collections.abc import Iterable
from pathlib import Path

import pytest
import source_corpus

import kiro_crew
from kiro_crew import _facade_mirror
from kiro_crew import kiro_prerequisite as facade
from kiro_crew.kiro_prerequisite import KiroPrerequisiteService

# Walks src/kiro_crew for owner imports; keep it on one worker.
pytestmark = pytest.mark.xdist_group(name="tree_scan_kiro_prerequisite_service")

_SRC = Path(kiro_crew.__file__).resolve().parent
_FACADE = "kiro_crew.kiro_prerequisite"
_OWNER_PREFIX = "kiro_crew.kiro_prerequisite_"

#: Every module-level name ``kiro_prerequisite.py`` bound before the split.
_BASE_MODULE_NAMES = frozenset("""
    AGENT_FILENAME Any AuditWriter Awaitable BUNDLED_CLI_UPDATE_REFUSAL CRED_KIRO_API_KEY
    Callable Coroutine FAKE_ACP_TEST_MODE_ENV IDENTITY_PARK_GRACE_SECS
    KIRO_CLI_LOGIN_COMMAND KIRO_CLI_SSO_LOGIN_COMMAND KIRO_CLI_UPDATE_COMMAND
    KiroPrerequisiteService LITE_AGENT_FILENAME Mapping MutableMapping
    OFFICIAL_INSTALL_DOCS_URL Path PrerequisiteStatus ProcessResult ProcessRunner
    SandboxUnavailableError TrustedAcpExecutableSnapshot _ACP_SUBCOMMAND
    _ACP_UNSUPPORTED_MARKERS _API_KEY_FINGERPRINT_SEP _AUTH_FINGERPRINT_ABSENT
    _AUTH_FINGERPRINT_CACHE_SECS _AUTH_IDENTITY_TABLES _AUTH_SQLITE_DB _AUTH_SQLITE_FILES
    _AUTH_SQLITE_TIMEOUT_SECS _AUTH_STAGING_RELATIVE _AUTH_STATE_KEY_PREFIXES
    _AUTH_STATE_TABLE _AUTH_STORE_READ_ERROR _AuthStoreMapping _AuthWorkspace
    _CASE_INSENSITIVE_CLAIM_FIELDS _CREW_VAULT_FINGERPRINT_SEP _FORCED_PROBE_FLOOR_SECS
    _IDENTITY_CLAIM_FIELDS _IDENTITY_FINGERPRINT_READ_ID _IDENTITY_PROBE_ENV_KEYS
    _IDENTITY_PROBE_TIMEOUT_SECS _IDENTITY_PROXY_ENV_KEYS _KIRO_AUTH_SANDBOX_MODE
    _LEGACY_IDLE_OPERATION _LOGIN_TAIL _MAX_AUTH_EXECUTABLE_BYTES _MAX_AUTH_STORE_FILE_BYTES
    _MAX_CAPTURED_OUTPUT _MAX_VISIBLE_DETAIL _OPERATOR_OVERRIDE_ATTESTATIONS
    _PROBE_CACHE_SECS _PROBE_ENV_KEYS _PROBE_ERROR_MAX_CHARS _PROBE_TIMEOUT_SECS
    _PROCESS_GROUP_SUPERVISOR _PROCESS_GROUP_SUPERVISOR_CODE _PROCESS_GROUP_SUPERVISOR_ERROR
    _SEED_BASELINE_TIMEOUT_SECS _SETUP_COMPLETE_FILENAME _SPECS_STILL_MISSING_ERROR
    _SPEC_REJECTION_MARKER _SSO_LOGIN_TAIL _TERMINATION_GRACE_SECS _TURN_AUTH_MAX_AGE_SECS
    _UNVERIFIED_SANDBOX_MODE _UPDATE_TIMEOUT_SECS _WARM_UP_DELAY_SECS
    _WHOAMI_SIGNED_OUT_MARKER _WINDOWS_DESCENDANT_POLL_SECS _acp_executable_is_runnable
    _allowlisted_env _api_key_identity_claim _append_capped _atomic_write_secret_bytes
    _auth_store_mappings _binary_sha256 _canonical_candidate _claim_digest
    _combine_identity_fingerprints _crew_vault_fingerprint _crew_vault_holds_no_identity
    _default_spec_lister _ensure_auth_staging_parent _established_installation
    _existing_binary_digest _host_can_reap _identity_claims _identity_components
    _identity_probe_env _is_off_path_override _is_runnable_executable
    _kas_spec_load_failures _note_unsupervised_once _open_identity_db_readonly
    _platform_label _prepare_auth_workspace _prepare_sandboxed_spawn _probe_env
    _probe_failure_text _probe_filesystem_state _process_group_supervisor
    _project_identity_database _read_bounded_regular_file
    _register_operator_override_attestation _run_on_private_loop _run_process
    _sanitize_detail _store_choice_is_certain _terminal_audit_detail _terminate_process
    _track_windows_descendants _turn_auth_service _unlaunchable_mcp_servers _unlink_off_loop
    _unsupervised_noted _whoami_says_signed_out _without_api_key_component _write_audit
    asdict asyncio atomic_write config_dir contextlib corroborate_launcher_refusal
    create_subprocess_limited dataclass ensure_directory field find_kiro_cli_candidates
    functools hashlib hooks identity_component_dropped identity_fingerprint
    identity_park_grace_remaining identity_stamp_mismatch identity_store_is_relocated
    identity_stores is_bundled_kiro_cli json kiro_identity_store_path
    kiro_signed_out_confirmed kiro_spawn_executor known_kiro_cli_dirs launcher_refusal
    legacy_idle_operation logger logging login_commands_for mark_identity_parked
    mcp_entry_is_registry_governed os overlay_sets_api_key platform_compat
    pre_spawn_identity read_env_file_credential receives_kiro_cli_api_key redact
    redact_credentials redact_exfiltration_urls register_process_start_override_attestation
    register_turn_auth_service replace resource_limit_supervisor_argv sandbox
    sandboxed_spawn_argv shielded_prepare_off_loop shlex shutil
    snapshot_trusted_acp_executable spawn_identity_of spawn_pid spawn_supervised_oneshot
    spawned_under sqlite3 stamp_spawn_identity stat subprocess sys tempfile time urllib
    weakref
    """.split())

#: ``KiroPrerequisiteService`` members before the split: name -> (kind, coroutine,
#: ``inspect.signature``).
_BASE_MEMBERS: dict[str, tuple[str, bool, str]] = {
    "__init__": (
        "function",
        False,
        "(self, *, platform_name: 'str | None' = None, environ: 'MutableMapping[str, str] | None' = None, home: 'Path | None' = None, data_home: 'Path | None' = None, process_runner: 'ProcessRunner | None' = None, audit_writer: 'AuditWriter | None' = None, clock: 'Callable[[], float] | None' = None, spec_lister: 'Callable[[], list[tuple[str, Path]]] | None' = None, assume_ready: 'bool' = False, warm_up_delay: 'float' = 3.0) -> 'None'",
    ),
    "_agent_spec_overlay": (
        "function",
        True,
        "(self, result: 'dict[str, Any]') -> 'dict[str, Any]'",
    ),
    "_audited_identity_probe": (
        "function",
        True,
        "(self, executable: 'str', *, isolate_home: 'bool' = True) -> 'ProcessResult'",
    ),
    "_audited_probe": (
        "function",
        True,
        "(self, action: 'str', executable: 'str', args: 'list[str]') -> 'ProcessResult'",
    ),
    "_dashboard_snapshot": ("function", True, "(self) -> 'dict[str, Any]'"),
    "_kas_bundle_route": ("staticmethod", False, "() -> 'bool'"),
    "_mark_setup_complete": ("function", False, "(self) -> 'None'"),
    "_maybe_latch_interim_identity": ("function", False, "(self, fingerprint: 'str') -> 'None'"),
    "_missing_agent_specs": ("staticmethod", False, "() -> 'list[str]'"),
    "_probe": ("function", True, "(self, *, force: 'bool' = False) -> 'PrerequisiteStatus'"),
    "_probe_acp_support": ("function", True, "(self, executable: 'str') -> 'bool'"),
    "_probe_kas_spec_loading": ("function", True, "(self) -> 'tuple[list[str], str]'"),
    "_probe_spec_acceptance": (
        "function",
        True,
        "(self, executable: 'str') -> 'tuple[list[str], str]'",
    ),
    "_repair_agent_specs": ("function", True, "(self) -> 'str'"),
    "_repair_auxiliary_specs": ("function", True, "(self, missing: 'list[str]') -> 'str'"),
    "_run_auth_command": (
        "function",
        True,
        "(self, executable: 'str', args: 'list[str]', *, base_env: 'dict[str, str]', timeout_secs: 'float', isolate_home: 'bool' = True) -> 'ProcessResult'",
    ),
    "_set_terminal_audit": (
        "function",
        True,
        "(self, action: 'str', outcome: 'str', caller: 'str', error: 'str' = '') -> 'None'",
    ),
    "_snapshot_dict": ("function", False, "(self) -> 'dict[str, Any]'"),
    "_stamp_probe": ("function", False, "(self, identity: 'str') -> 'None'"),
    "close": ("function", True, "(self) -> 'None'"),
    "current_identity_fingerprint": (
        "function",
        True,
        "(self, *, allow_cached: 'bool' = True) -> 'str'",
    ),
    "identity_absence_already_swept": ("property", False, "(self) -> 'bool'"),
    "identity_absence_is_definitive": ("property", False, "(self) -> 'bool'"),
    "identity_absence_run": ("property", False, "(self) -> 'int'"),
    "identity_changed_since_probe": ("function", True, "(self) -> 'bool'"),
    "identity_changed_since_sessions": ("function", True, "(self) -> 'tuple[bool, str]'"),
    "identity_component_dropped": ("function", False, "(self, live: 'str') -> 'bool'"),
    "identity_observation_generation": ("property", False, "(self) -> 'int'"),
    "initial_setup_complete": ("property", False, "(self) -> 'bool'"),
    "mark_signed_out": ("function", False, "(self) -> 'None'"),
    "note_absence_swept": ("function", False, "(self, run: 'int') -> 'None'"),
    "note_sessions_reconciled": (
        "function",
        False,
        "(self, fingerprint: 'str', *, observations_before: 'int | None' = None) -> 'None'",
    ),
    "read_spawn_identity": ("function", True, "(self) -> 'str'"),
    "record_independent_backend_setup": ("function", True, "(self, backend: 'str') -> 'bool'"),
    "repair_agent_specs": ("function", True, "(self, caller: 'str' = '') -> 'dict[str, Any]'"),
    "seed_sessions_baseline": ("function", True, "(self) -> 'bool'"),
    "session_ready": ("function", True, "(self) -> 'bool'"),
    "signed_out_confirmed": ("function", True, "(self) -> 'bool'"),
    "snapshot": (
        "function",
        True,
        "(self, *, force: 'bool' = False, coalesce: 'bool' = False) -> 'dict[str, Any]'",
    ),
    "update_cli": ("function", True, "(self, caller: 'str' = '') -> 'dict[str, Any]'"),
    "verified_ready": ("function", True, "(self, *, max_age_secs: 'float') -> 'bool'"),
    "warm_up": ("function", False, "(self) -> 'asyncio.Task[None] | None'"),
}

#: Members bound from a collaborator -> the owner whose function the class attribute is.
_BOUND: dict[str, str] = {
    "_agent_spec_overlay": "kiro_crew.kiro_prerequisite_agent_specs",
    "_audited_identity_probe": "kiro_crew.kiro_prerequisite_probe",
    "_audited_probe": "kiro_crew.kiro_prerequisite_probe",
    "_maybe_latch_interim_identity": "kiro_crew.kiro_prerequisite_identity_tracking",
    "_missing_agent_specs": "kiro_crew.kiro_prerequisite_agent_specs",
    "_probe": "kiro_crew.kiro_prerequisite_probe",
    "_probe_acp_support": "kiro_crew.kiro_prerequisite_probe",
    "_probe_kas_spec_loading": "kiro_crew.kiro_prerequisite_agent_specs",
    "_probe_spec_acceptance": "kiro_crew.kiro_prerequisite_agent_specs",
    "_repair_agent_specs": "kiro_crew.kiro_prerequisite_agent_specs",
    "_repair_auxiliary_specs": "kiro_crew.kiro_prerequisite_agent_specs",
    "_run_auth_command": "kiro_crew.kiro_prerequisite_probe",
    "_set_terminal_audit": "kiro_crew.kiro_prerequisite_probe",
    "_stamp_probe": "kiro_crew.kiro_prerequisite_probe",
    "current_identity_fingerprint": "kiro_crew.kiro_prerequisite_identity_tracking",
    "identity_changed_since_probe": "kiro_crew.kiro_prerequisite_identity_tracking",
    "identity_changed_since_sessions": "kiro_crew.kiro_prerequisite_identity_tracking",
    "note_absence_swept": "kiro_crew.kiro_prerequisite_identity_tracking",
    "note_sessions_reconciled": "kiro_crew.kiro_prerequisite_identity_tracking",
    "read_spawn_identity": "kiro_crew.kiro_prerequisite_identity_tracking",
    "repair_agent_specs": "kiro_crew.kiro_prerequisite_agent_specs",
    "seed_sessions_baseline": "kiro_crew.kiro_prerequisite_identity_tracking",
    "signed_out_confirmed": "kiro_crew.kiro_prerequisite_probe",
    "update_cli": "kiro_crew.kiro_prerequisite_probe",
}


def _index() -> _facade_mirror.MirrorIndex:
    index = vars(facade)[_facade_mirror.INDEX_ATTRIBUTE]
    assert isinstance(index, _facade_mirror.MirrorIndex)
    return index


def _owner_modules() -> list[str]:
    return list(_index().owners)


def _source(module_name: str) -> str:
    path = importlib.import_module(module_name).__file__
    assert path is not None
    return Path(path).read_text(encoding="utf-8")


def _member(cls: type, name: str) -> tuple[str, object]:
    raw = inspect.getattr_static(cls, name)
    if isinstance(raw, property):
        return "property", raw.fget
    if isinstance(raw, staticmethod):
        return "staticmethod", raw.__func__
    if inspect.isfunction(raw):
        return "function", raw
    return type(raw).__name__, None


# --------------------------------------------------------------------------- #
# The facade
# --------------------------------------------------------------------------- #


def test_the_facade_serves_a_mirror_over_every_sibling_owner() -> None:
    assert isinstance(facade, _facade_mirror.MirroredModule)
    index = _index()
    on_disk = sorted(f"kiro_crew.{path.stem}" for path in _SRC.glob("kiro_prerequisite_*.py"))
    assert sorted(index.owners) == on_disk
    assert index.excluded == ()
    assert index.divergent() == ()


def test_every_base_module_name_resolves_through_the_facade() -> None:
    index = _index()
    missing = sorted(name for name in _BASE_MODULE_NAMES if not hasattr(facade, name))
    assert missing == []
    for name in sorted(_BASE_MODULE_NAMES):
        holders = index.holders.get(name, ())
        assert holders, f"{name} is no owner's name"
        assert all(
            getattr(importlib.import_module(holder), name) is getattr(facade, name)
            for holder in holders
        ), name


def test_every_owner_logs_as_the_facade() -> None:
    shared = logging.getLogger(_FACADE)
    assert facade.logger is shared
    loggers = {
        owner: vars(importlib.import_module(owner)).get("logger") for owner in _owner_modules()
    }
    assert {owner: log for owner, log in loggers.items() if log not in (None, shared)} == {}


def test_every_served_import_is_named_for_type_checkers() -> None:
    """A name the family imports and the facade serves is in a ``TYPE_CHECKING`` group.

    The facade binds none of them, so this group is what keeps
    ``from kiro_crew.kiro_prerequisite import CRED_KIRO_API_KEY`` visible to mypy.
    """
    index = _index()
    defined = set().union(
        *(_facade_mirror._defined_names(ast.parse(_source(o))) for o in index.owners)
    )
    family = {*index.owners, _FACADE}
    served_imports = {
        name
        for name in index.exports
        if name not in defined
        and name != "TYPE_CHECKING"
        and getattr(getattr(facade, name), "__name__", None) not in family
    }
    named: set[str] = set()
    for node in ast.parse(_source(_FACADE)).body:
        if isinstance(node, ast.If) and ast.unparse(node.test) == "TYPE_CHECKING":
            for sub in node.body:
                if isinstance(sub, ast.ImportFrom) and sub.module not in index.owners:
                    named |= {alias.asname or alias.name for alias in sub.names}
                elif isinstance(sub, ast.Import):
                    named |= {alias.asname or alias.name.split(".")[0] for alias in sub.names}
    assert served_imports, "the census found nothing to check"
    assert sorted(served_imports - named) == []
    assert sorted(named - served_imports) == []


# --------------------------------------------------------------------------- #
# The class
# --------------------------------------------------------------------------- #


def test_the_class_keeps_every_base_member_with_its_kind_and_signature() -> None:
    now: dict[str, tuple[str, bool, str]] = {}
    for name in vars(KiroPrerequisiteService):
        if name.startswith("__") and name != "__init__":
            continue
        kind, function = _member(KiroPrerequisiteService, name)
        assert callable(function), name
        now[name] = (
            kind,
            inspect.iscoroutinefunction(function),
            str(inspect.signature(function)),
        )
    assert now == _BASE_MEMBERS


@pytest.mark.parametrize("name", sorted(_BOUND))
def test_a_bound_member_is_its_collaborators_function(name: str) -> None:
    owner = importlib.import_module(_BOUND[name])
    kind, function = _member(KiroPrerequisiteService, name)
    assert function is getattr(owner, name)
    assert function.__module__ == owner.__name__
    assert function.__qualname__ == f"KiroPrerequisiteService.{name}"
    assert "self" not in function.__annotations__


def test_every_member_is_bound_or_defined_in_the_class_body() -> None:
    kept = {
        name
        for name, member in vars(KiroPrerequisiteService).items()
        if getattr(getattr(member, "fget", member), "__module__", None)
        == KiroPrerequisiteService.__module__
        and not (name.startswith("__") and name != "__init__")
    }
    assert kept | set(_BOUND) == set(_BASE_MEMBERS)
    assert not kept & set(_BOUND)


def _class_body_dependencies(source: str, names: set[str]) -> list[str]:
    """Uses in the named module-level functions that only work inside a class body."""
    found = []
    for node in ast.parse(source).body:
        if not isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)) or node.name not in names:
            continue
        for sub in ast.walk(node):
            if (
                isinstance(sub, ast.Call)
                and isinstance(sub.func, ast.Name)
                and sub.func.id == "super"
                and not sub.args
            ):
                found.append(f"{node.name}: super()")
            elif isinstance(sub, ast.Name) and sub.id == "__class__":
                found.append(f"{node.name}: __class__")
            elif (
                isinstance(sub, ast.Attribute)
                and sub.attr.startswith("__")
                and not sub.attr.endswith("__")
            ):
                found.append(f"{node.name}: {sub.attr}")
    return found


def test_a_bound_function_needs_no_class_body() -> None:
    by_owner: dict[str, set[str]] = {}
    for name, owner in _BOUND.items():
        by_owner.setdefault(owner, set()).add(name)
    assert by_owner
    for owner, names in sorted(by_owner.items()):
        assert _class_body_dependencies(_source(owner), names) == [], owner


def test_the_class_body_check_finds_each_shape() -> None:
    source = (
        "def a(self):\n    return super().x()\n"
        "def b(self):\n    return __class__\n"
        "def c(self):\n    return self.__hidden\n"
        "def d(self):\n    return super(type(self), self).x() + self.__dict__.get('k')\n"
    )
    assert _class_body_dependencies(source, {"a", "b", "c", "d"}) == [
        "a: super()",
        "b: __class__",
        "c: __hidden",
    ]


# --------------------------------------------------------------------------- #
# Who imports an owner
# --------------------------------------------------------------------------- #


def _imported_modules(node: ast.Import | ast.ImportFrom, module: str) -> list[str]:
    """Dotted names an import statement in *module* reaches, relative spellings resolved."""
    if isinstance(node, ast.Import):
        return [alias.name for alias in node.names]
    base = node.module or ""
    if node.level:
        package = module.split(".")[: -node.level]
        base = ".".join([*package, *([node.module] if node.module else [])])
    return [base, *(f"{base}.{alias.name}" for alias in node.names)]


def _owner_importers(sources: Iterable[tuple[Path, str]], root: Path) -> set[str]:
    """The files among *sources* (relative to *root*) whose code imports a family owner."""
    found = set()
    for path, text in sources:
        rel = path.relative_to(root)
        module = ".".join([root.name, *rel.with_suffix("").parts])
        for node in ast.walk(ast.parse(text)):
            if isinstance(node, (ast.Import, ast.ImportFrom)) and any(
                name.startswith(_OWNER_PREFIX) for name in _imported_modules(node, module)
            ):
                found.add(rel.as_posix())
    return found


def test_only_the_family_imports_an_owner() -> None:
    family = {f"{name.rpartition('.')[2]}.py" for name in (_FACADE, *_owner_modules())}
    sources = source_corpus.iter_candidate_sources(require_all=("kiro_prerequisite_",))
    importers = _owner_importers(
        ((path, text) for path, text in sources if path.is_relative_to(_SRC)), _SRC
    )
    assert "kiro_prerequisite_service.py" in importers  # the shell binds its collaborators
    assert importers - family == set()


def test_the_owner_import_scan_finds_an_outside_importer(tmp_path: Path) -> None:
    root = tmp_path / "kiro_crew"
    sources = {
        root / "pkg" / "caller.py": "from kiro_crew.kiro_prerequisite_probe import _probe\n",
        root / "other.py": "from kiro_crew import kiro_prerequisite_status as status\n",
        root / "relative.py": "from .kiro_prerequisite_identity import identity_fingerprint\n",
        root / "pkg" / "up.py": "from .. import kiro_prerequisite_process\n",
        root
        / "facade_user.py": "from kiro_crew.kiro_prerequisite import KiroPrerequisiteService\n",
    }
    assert _owner_importers(sources.items(), root) == {
        "pkg/caller.py",
        "other.py",
        "relative.py",
        "pkg/up.py",
    }
