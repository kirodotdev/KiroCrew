"""The core dashboard handlers' facade keeps the patch seams the one module had.

``kiro_crew.dashboard.handlers.core`` is a mirror facade (:mod:`kiro_crew._facade_mirror`)
over the ``kiro_crew.dashboard.handlers.core_<role>`` owners. It keeps the page, asset,
health and theme routes, the session sub-agent routes, logout, shutdown and the app token,
and its import header. Callers, tests and documents still reach every name as
``kiro_crew.dashboard.handlers.core.<name>``, and the moved code reads its names as plain
module globals, so a patch through the facade reaches that code only because the mirror
writes it into every module holding the name.

``test/test_facade_mirror_guards.py`` holds every mirrored facade to the shared rules (one
object per name, the manifest, owners never importing their facade, no shared global
rebound). These tests pin what is particular to this facade: every name the test trees
patch through it reaches every module holding it; the owners are exactly the ``core_*.py``
siblings, loaded lowest layer first; the routes it keeps and what they read from an owner;
the global its page route rebinds; the family logs under the facade's name; the import
inventory that stands in for the F401 check ``setup.cfg`` switches off for it; the
model-pin validator still reads the ACP client's entitlement helpers at call time; and
no module outside the family imports an owner.
"""

from __future__ import annotations

import ast
import functools
import importlib
import inspect
import logging
import sys
from collections.abc import Iterable
from pathlib import Path
from types import ModuleType, SimpleNamespace

import facade_mirror_scan
import pytest
import source_corpus

import kiro_crew
from kiro_crew import _facade_mirror
from kiro_crew.dashboard.handlers import core

pytestmark = pytest.mark.xdist_group(name="tree_scan_core_handlers_facade")

#: Checks ``test_facade_shared_checks.py`` runs against this module's helpers; each is
#: the same for every facade, so it is written once there and parametrized per module.
SHARED_FACADE_CHECKS = ("known_seams", "corpus_gate")

FACADE = "kiro_crew.dashboard.handlers.core"
OWNER_PREFIX = "kiro_crew.dashboard.handlers.core_"
HANDLERS_DIR = Path(core.__file__).resolve().parent
_SRC = Path(kiro_crew.__file__).resolve().parent
_THIS_FILE = Path(__file__).resolve()

#: Names the test trees are known to patch through the facade, one of each seam the
#: routes have: the SEL accessor, the page shell cache and bundle path, the STT
#: availability probes and prerequisite commands, the model-pin evidence reader, the
#: same-user peer check and the token mint. The scan below must find all of them, so a
#: scanner that stops matching goes red instead of passing over nothing.
_KNOWN_SEAMS = frozenset(
    {
        "_sel",
        "_INDEX_HTML_CACHE",
        "_DIST_INDEX",
        "availability_detail",
        "_stt_prereq_commands",
        "_transcribe_extra_importable",
        "_active_advertised_ids",
        "check_peer_is_self",
        "generate_token",
    }
)

#: Every owner, lowest layer first: the order the facade imports them and a read resolves in.
_OWNERS = tuple(
    f"{OWNER_PREFIX}{role}"
    for role in (
        "security",
        "stt_access",
        "stt_settings",
        "stt_status",
        "stt_speech",
        "config_view",
        "config_models",
        "config_schema",
        "config_patch",
        "local_token",
    )
)

#: The routes and helpers the facade keeps as its own code.
_KEPT_FUNCTIONS = frozenset(
    {
        "_resolve_index_html",
        "index",
        "logo",
        "api_branding",
        "_liveness_payload",
        "api_health",
        "api_version",
        "api_live",
        "api_ready",
        "_theme_payload",
        "api_theme_boot",
        "api_theme_config",
        "pwa_file",
        "_invalid_session_path_id",
        "api_session_agents_list",
        "api_session_agent_result",
        "api_session_agent_stream",
        "api_logout",
        "api_shutdown",
        "api_app_token",
    }
)

#: What the routes the facade keeps read from an owner, bound here by import.
_KEPT_ROUTE_READS = {
    f"{OWNER_PREFIX}security": {"_sel"},
    f"{OWNER_PREFIX}local_token": {"_unix_peer_is_self"},
}

#: Every name the facade's import statements bound before its owners moved out. They
#: stay bound here because callers and tests read and patch them as attributes of it.
_BASE_IMPORTS = frozenset("""
    AUTOCOMPACT_PCT_MAX AUTOCOMPACT_PCT_MIN Any CHAT_TURN_TIMEOUT_MAX CHAT_TURN_TIMEOUT_MIN
    COMPACT_WAIT_SECS_MAX COMPACT_WAIT_SECS_MIN COMPLETION_KEEP_CHARS_MIN
    ClientConnectionResetError ConfigReadError ConfigWriteRefused Coroutine
    DECISION_BUCKET_MAX DECISION_BUCKET_MIN DECISION_MODEL_ROUTE_TIERS
    DEDUP_EVERY_N_SWEEPS_MAX DashboardState EFFORT_LEVELS EMBED_RATE_LIMIT_MAX
    EXTERNAL_TERMINAL_APPS EXTRACTION_POOL_SIZE_MAX EXTRACTION_POOL_SIZE_MIN
    FOLDER_INGEST_CHUNK_BUDGET_MAX FOLDER_SORT_MODES IMPORT_CHUNK_BUDGET_MAX Iterable
    JUDGE_PROVIDERS KEBAB_RE KiroCrewConfig MAX_IDLE_EVICT_SECS MAX_INTERVAL_MS
    MAX_SESSION_TTL_SECS MAX_SUBAGENTS_FIXED_FLOOR MCP_PROBE_TIMEOUT_MAX
    MCP_PROBE_TIMEOUT_MIN MIN_IDLE_EVICT_SECS MIN_PARTIAL_INTERVAL_MS MIN_SILENCE_MS
    POOL_TTL_SECS_MAX POOL_TTL_SECS_MIN PROVIDER_LOCAL Path PeerCredResult
    QUALIFIED_MODEL_PATTERN RECENT_TINT_COUNT_MAX RECENT_TINT_COUNT_MIN
    RESULT_FILE_MAX_BYTES SESSION_SORT_MODES SESSION_TIMEOUT_MAX SESSION_TIMEOUT_MIN
    SOFT_STOP_BUDGET_MAX SOFT_STOP_BUDGET_MIN STT_LANGUAGE_AUTO SUBAGENT_AUTO_MAX_CEILING
    SUBAGENT_MAX_TURNS_CEILING SWEEP_CHUNK_BUDGET_MAX SttConfig
    TITLE_REFRESH_EVERY_TURNS_MAX TRANSCRIBE_LANGUAGES_MAX TRANSCRIBE_LANGUAGES_MIN
    VOCABULARIES_ACCESS_DENIED VOCABULARIES_LIST_FAILED VOCABULARIES_LIST_PERMISSION
    VocabularyListError _CU_MAX_SCREENSHOT_MAX_PX _CU_MAX_TREE_NODES_LIMIT
    _CU_MIN_SCREENSHOT_MAX_PX _STREAMING_PROVIDERS _VALID_STT_PROVIDERS
    _clamp_compact_wait_secs _ct_eq _find_ffmpeg _metrics_provider _owner_denial_response
    _pip_install_channel_available _redact_external _resolution _unix_request_socket
    _whisper_language ack_explicit_write annotations asyncio audio_exceeds_secs
    availability_detail aws_consent batch_duration_cap_secs beacon
    build_posture_snapshot_async check_host check_peer_is_self coerce_dict_section
    config_path copy difflib discovery_executor drained_to_thread ensure_ffmpeg_in_path
    ffmpeg_source functools gateway_id generate_token guard_owner_surface_routes
    is_direct_local_request is_valid_id json kiro_crew list_custom_vocabularies logging math
    os owner_surface_guard parse_duration pip_extra_install_command platform platform_compat
    posture_counts_async re require_owner_dashboard_request run_config_write
    selectable_backend_values selectable_stored_backend_values shlex shutil stt
    stt_capabilities stt_decoder stt_models stt_telemetry transcribe_language_options
    transcribe_vocabulary_name unicodedata update_config_locked web
    """.split())

#: The names the facade's own glue imports: the owner loop, the mirror, its class swap.
_GLUE_IMPORTS = frozenset({"TYPE_CHECKING", "_facade_mirror", "_importlib", "_sys"})


def _index() -> _facade_mirror.MirrorIndex:
    index = vars(core)[_facade_mirror.INDEX_ATTRIBUTE]
    assert isinstance(index, _facade_mirror.MirrorIndex)
    return index


def _owner_paths() -> dict[str, Path]:
    return {
        f"{OWNER_PREFIX}{path.stem[len('core_'):]}": path
        for path in sorted(HANDLERS_DIR.glob("core_*.py"))
    }


def _family() -> list[ModuleType]:
    return [core, *(sys.modules[name] for name in _index().owners)]


def _tree(module_name: str) -> ast.Module:
    path = Path(importlib.import_module(module_name).__file__)
    return ast.parse(path.read_text(encoding="utf-8"))


def _defined_by(module_name: str) -> set[str]:
    return _facade_mirror._defined_names(_tree(module_name))


def _is_type_checking(node: ast.stmt) -> bool:
    return isinstance(node, ast.If) and ast.unparse(node.test) == "TYPE_CHECKING"


def _test_tree_files() -> list[Path]:
    root = source_corpus.repo_root()
    out = []
    for path in source_corpus.repo_files_named(".py"):
        rel = path.relative_to(root).as_posix()
        if rel == "conftest.py" or rel.startswith("test/") or "/tests/" in rel:
            out.append(path)
    return out


@functools.lru_cache(maxsize=1)
def _corpus_patched_names() -> frozenset[str]:
    """Every name the test trees patch through the facade, scanned once per worker."""
    found: set[str] = set()
    for path in _test_tree_files():
        text = path.read_text(encoding="utf-8", errors="replace")
        if "handlers" not in text or "core" not in text:
            continue
        names = facade_mirror_scan.patched_names(text, {FACADE: FACADE})
        found |= names.get(FACADE, set())
    return frozenset(found)


def read_names(*sources: str) -> set[str]:
    """Every name the family's code reads: a bare load, or the base of an attribute read."""
    found: set[str] = set()
    for source in sources:
        for node in ast.walk(ast.parse(source)):
            if isinstance(node, ast.Name) and isinstance(node.ctx, ast.Load):
                found.add(node.id)
    return found


def unexpected_imports(
    source: str, owner_names: dict[str, set[str]], family_reads: set[str]
) -> list[str]:
    """Names *source* binds by a runtime import beyond the import inventory.

    The inventory is the BASE imports, the glue's own, and from each owner module only
    names that owner defines. An import beyond them is accounted for when the family's
    code reads the name, so a later change can import what it uses here. Imports under
    ``if TYPE_CHECKING:`` bind nothing at run time and are the mirror guard's business,
    so they are skipped.
    """
    found: list[str] = []
    for node in ast.parse(source).body:
        if _is_type_checking(node):
            continue
        if isinstance(node, ast.Import):
            bound = [(alias.asname or alias.name.split(".")[0], None) for alias in node.names]
        elif isinstance(node, ast.ImportFrom):
            bound = [(alias.asname or alias.name, node.module) for alias in node.names]
        else:
            continue
        for name, module in bound:
            if module in owner_names:
                if name not in owner_names[module]:
                    found.append(f"{module}.{name}")
            elif name not in _BASE_IMPORTS | _GLUE_IMPORTS | family_reads:
                found.append(name)
    return found


def _family_sources() -> list[str]:
    paths = [Path(importlib.import_module(m).__file__) for m in (FACADE, *_OWNERS)]
    return [path.read_text(encoding="utf-8") for path in paths]


def _owner_names() -> dict[str, set[str]]:
    return {owner: _defined_by(owner) for owner in _OWNERS}


def test_a_facade_patch_of_every_scanned_name_reaches_every_module_holding_it() -> None:
    """The reach the unsplit module had: a write to ``core.<name>`` was what every reader
    of that name read. Each module of the family that binds a patched name must see the
    patched object, and the undo must restore all of them."""
    family = _family()
    missed: dict[str, list[str]] = {}
    for name in sorted(_corpus_patched_names()):
        if not hasattr(core, name):
            continue
        sentinel = object()
        holders = [module for module in family if name in vars(module)]
        before = {module.__name__: vars(module)[name] for module in holders}
        with pytest.MonkeyPatch.context() as patch:
            patch.setattr(core, name, sentinel)
            stale = [m.__name__ for m in holders if vars(m).get(name) is not sentinel]
            if getattr(core, name) is not sentinel:
                stale.append(FACADE)
        stale += [m.__name__ for m in holders if vars(m).get(name) is not before[m.__name__]]
        if stale:
            missed[name] = stale
    assert missed == {}
    assert _index().divergent() == ()


def test_the_owners_are_every_core_sibling_in_layer_order() -> None:
    """``_OWNERS`` lists every ``core_*.py`` sibling, and each owner imports only owners
    listed before it -- the order the facade loads them and a read resolves in."""
    index = _index()
    assert (index.owners, index.excluded, dict(index.forwards)) == (_OWNERS, (), {})
    assert sorted(_OWNERS) == sorted(_owner_paths())
    late: dict[str, list[str]] = {}
    for position, owner in enumerate(_OWNERS):
        imported = {
            node.module
            for node in ast.walk(_tree(owner))
            if isinstance(node, ast.ImportFrom) and node.module
        }
        later = sorted(m for m in imported if m in _OWNERS and _OWNERS.index(m) >= position)
        if later or FACADE in imported:
            late[owner] = later + ([FACADE] if FACADE in imported else [])
    assert late == {}


def test_the_facade_keeps_its_routes_and_binds_what_they_read_from_an_owner() -> None:
    """The routes kept here read these owner names as their own globals, so they are bound
    here by import, the same object as the owner's; every other owner-defined name is
    served from its owner."""
    defined = {
        node.name
        for node in _tree(FACADE).body
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef))
    }
    assert _KEPT_FUNCTIONS <= defined
    bound = {
        node.module: {alias.asname or alias.name for alias in node.names}
        for node in _tree(FACADE).body
        if isinstance(node, ast.ImportFrom) and node.module in _OWNERS
    }
    assert all(names <= bound.get(owner, set()) for owner, names in _KEPT_ROUTE_READS.items())
    for owner, names in bound.items():
        module = importlib.import_module(owner)
        assert all(vars(core)[name] is vars(module)[name] for name in names)
    owner_defined = {name for owner in _OWNERS for name in _defined_by(owner)}
    bound_here = owner_defined & set(vars(core))
    # ``logger`` is the facade's own: each owner binds the same named logger.
    kept_reads = {name for names in _KEPT_ROUTE_READS.values() for name in names}
    assert kept_reads | {"logger"} <= bound_here
    served = owner_defined - bound_here
    assert served and served <= set(_index().exports)
    assert all(getattr(core, name) is not None for name in served)


def test_the_page_cache_is_the_facade_s_own_global() -> None:
    """``_resolve_index_html`` rebinds ``_INDEX_HTML_CACHE`` with ``global``, which writes
    one namespace, so the name and every function reading it stay in the facade and no
    owner binds it."""
    binders = [m.__name__ for m in _family() if "_INDEX_HTML_CACHE" in vars(m)]
    assert binders == [FACADE]
    rebinds = [
        node
        for node in ast.walk(_tree(FACADE))
        if isinstance(node, ast.Global) and "_INDEX_HTML_CACHE" in node.names
    ]
    assert len(rebinds) == 1
    readers = [
        f"{module.__name__}.{name}"
        for module in _family()[1:]
        for name, value in vars(module).items()
        if inspect.isfunction(value)
        and value.__module__ == module.__name__
        and "_INDEX_HTML_CACHE" in value.__code__.co_names
    ]
    assert readers == []


def test_every_owner_logs_under_the_facade_logger_name() -> None:
    """Log capture and filters key on ``kiro_crew.dashboard.handlers.core``."""
    logged = 0
    for owner in _OWNERS:
        for call in ast.walk(_tree(owner)):
            if (
                isinstance(call, ast.Call)
                and isinstance(call.func, ast.Attribute)
                and call.func.attr == "getLogger"
            ):
                assert [ast.literal_eval(arg) for arg in call.args] == [FACADE], owner
        module = importlib.import_module(owner)
        if "logger" in vars(module):
            assert module.logger is core.logger is logging.getLogger(FACADE)
            logged += 1
    assert logged >= 5


def test_the_facade_imports_nothing_beyond_its_inventory() -> None:
    source = Path(core.__file__).read_text(encoding="utf-8")
    family_reads = read_names(*_family_sources())
    assert unexpected_imports(source, _owner_names(), family_reads) == []


def test_the_inventory_check_reports_a_planted_import() -> None:
    """The F401 stand-in must be able to fail: a stray import, and an owner import of a
    name that owner does not define, are both reported, while an import the family's code
    reads is accounted for."""
    source = Path(core.__file__).read_text(encoding="utf-8")
    planted = source + (
        "\nimport fnmatch\nimport sched\n"
        "from kiro_crew.dashboard.handlers.core_security import api_stt_config\n"
    )
    family_reads = read_names(*_family_sources(), "_uses = fnmatch.fnmatch\n")
    assert unexpected_imports(planted, _owner_names(), family_reads) == [
        "sched",
        "kiro_crew.dashboard.handlers.core_security.api_stt_config",
    ]


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
                name.startswith(OWNER_PREFIX) for name in _imported_modules(node, module)
            ):
                found.add(rel.as_posix())
    return found


def test_only_the_family_imports_an_owner() -> None:
    """Callers keep importing the facade: an owner import from outside the family would
    bind its own copy of a name and stop seeing a patch the facade fans out."""
    family = {
        f"dashboard/handlers/{name.rpartition('.')[2]}.py" for name in (FACADE, *_index().owners)
    }
    sources = source_corpus.iter_candidate_sources(require_all=("core_",))
    importers = _owner_importers(
        ((path, text) for path, text in sources if path.is_relative_to(_SRC)), _SRC
    )
    assert "dashboard/handlers/core_config_patch.py" in importers  # owners import lower owners
    assert importers - family == set()


def test_the_owner_import_scan_finds_an_outside_importer(tmp_path: Path) -> None:
    root = tmp_path / "kiro_crew"
    sources = {
        root / "pkg" / "caller.py": "from kiro_crew.dashboard.handlers.core_security import _sel\n",
        root / "other.py": "from kiro_crew.dashboard.handlers import core_stt_speech as speech\n",
        root / "dashboard" / "handlers" / "rel.py": "from .core_auth_x import a\n",
        root / "dashboard" / "handlers" / "deep" / "up.py": "from .. import core_security\n",
        root / "facade_user.py": "from kiro_crew.dashboard.handlers.core import _sel\n",
    }
    assert _owner_importers(sources.items(), root) == {
        "pkg/caller.py",
        "other.py",
        "dashboard/handlers/rel.py",
        "dashboard/handlers/deep/up.py",
    }


def _pin_request(ids: list[dict[str, str]]) -> SimpleNamespace:
    provider = SimpleNamespace(available_models=lambda: ids, backend="kiro")
    sessions = SimpleNamespace(active_providers=lambda: [provider])
    return SimpleNamespace(app={"state": SimpleNamespace(sessions=sessions)})


def test_the_pin_validator_reads_the_acp_entitlement_helpers_at_call_time(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The validator reaches ``advertised_model_ids`` through the SDK driver's call-time
    delegation, so a patch of that name on the ACP client still decides the answer, as it did
    when the validator imported the client itself. (``model_is_unusable`` is not a name the
    client facade forwards, so it is pinned structurally below rather than patched here.)"""
    from kiro_crew.acp import client as acp_client
    from kiro_crew.dashboard import chat_handlers

    monkeypatch.setattr(chat_handlers, "_model_rejected_reason", lambda value, backend: "")
    request = _pin_request([{"modelId": "picked"}])
    assert core._validate_role_model("picked", request) is None
    monkeypatch.setattr(acp_client, "advertised_model_ids", lambda entries: ["patched-only"])
    refusal = core._validate_role_model("picked", request)
    assert refusal is not None and "patched-only" in refusal


def test_the_driver_delegations_read_the_acp_client_at_call_time() -> None:
    """``advertised_model_ids`` and ``model_is_unusable`` on the ACP driver import the client's
    function inside the call, never at module scope, so a patch of the client's name made
    after import is the one the pin validator runs; and the validator reaches both through
    the driver, function-locally, without naming the ACP layer itself."""
    from kiro_crew.agent_sdk.drivers import acp as acp_driver

    driver = ast.parse(Path(acp_driver.__file__).read_text(encoding="utf-8"))
    names = ("advertised_model_ids", "model_is_unusable")
    module_scope = {
        alias.asname or alias.name
        for node in driver.body
        if isinstance(node, ast.ImportFrom) and node.module == "kiro_crew.acp.client"
        for alias in node.names
    }
    delegations = {}
    for node in driver.body:
        if isinstance(node, ast.FunctionDef) and node.name in names:
            delegations[node.name] = [
                (alias.name, alias.asname)
                for stmt in node.body
                if isinstance(stmt, ast.ImportFrom) and stmt.module == "kiro_crew.acp.client"
                for alias in stmt.names
            ]
    assert delegations == {name: [(name, "_impl")] for name in names}
    assert not module_scope & {"_impl", *names}
    validator = _tree("kiro_crew.dashboard.handlers.core_config_models")
    local = {
        alias.name
        for fn in validator.body
        if isinstance(fn, ast.FunctionDef)
        for stmt in fn.body
        if isinstance(stmt, ast.ImportFrom) and stmt.module == "kiro_crew.agent_sdk.drivers.acp"
        for alias in stmt.names
    }
    assert set(names) <= local
    assert not [
        node.module
        for node in ast.walk(validator)
        if isinstance(node, ast.ImportFrom) and (node.module or "").startswith("kiro_crew.acp")
    ]


def test_the_test_tree_walk_reaches_this_file() -> None:
    walked = {path.resolve() for path in _test_tree_files()}
    assert _THIS_FILE in walked
    assert source_corpus.repo_root() / "test" / "test_dashboard_handlers_core_coverage.py" in walked
