"""``kiro_crew.mcp_core`` keeps the patch seams and the surface the one module had.

``kiro_crew.mcp_core`` is a mirror facade (:mod:`kiro_crew._facade_mirror`) over the
owners in ``kiro_crew.mcp_core_parts``. The tool handlers in ``mcp_tools``, the tests and
the documents still reach every name as ``kiro_crew.mcp_core.<name>``, and the moved code
reads its names as plain module globals, so a patch through the facade reaches that code
only because the mirror writes it into every module holding the name.

``test/test_facade_mirror_guards.py`` holds every mirror facade to the shared contract.
These tests pin what is particular to this one: every name the test trees patch through
it, by any spelling they use, reaches every module holding it; the owners are exactly the
``mcp_core_parts`` modules, each importing only the owners before it; a facade write of a
gateway-target cache moves the cache the resolver reads; and every project name the
facade imported before its owners moved out stays bound on it, so a handler that reads
``mcp_core.<name>`` still type-checks.
"""

from __future__ import annotations

import ast
import sys
from pathlib import Path
from types import ModuleType

import facade_mirror_scan
import pytest
import source_corpus

from kiro_crew import _facade_mirror, mcp_core

pytestmark = pytest.mark.xdist_group(name="tree_scan_mcp_core_facade")

#: Checks ``test_facade_shared_checks.py`` runs against this module's helpers; each is
#: the same for every facade, so it is written once there and parametrized per module.
SHARED_FACADE_CHECKS = ("known_seams",)

FACADE = "kiro_crew.mcp_core"
PARTS_DIR = Path(mcp_core.__file__).resolve().parent / "mcp_core_parts"

#: Names the test trees are known to patch through the facade, one per kind of seam: a
#: verb the facade keeps, an identity resolver, a governance vet and a crew body an owner
#: defines, a gateway-target cache an owner rebinds at run time, the retry schedule
#: ``_send`` reads, and names the facade only imports. The scan must find all of them, so
#: a scanner that stops matching goes red instead of passing over nothing.
_KNOWN_SEAMS = frozenset(
    {
        "_post",
        "_get",
        "_resolve_session_key",
        "_resolve_session_key_strict",
        "require_strict_session_key",
        "_vet_memory_writes_governance",
        "_do_select_crew",
        "_crew_machine_markers",
        "_API",
        "_API_UNIX_SOCKET",
        "_REFUSED_RETRY_BACKOFFS",
        "_api_urlopen",
        "loopback_urlopen",
        "sel",
        "time",
    }
)


#: The project names the facade imported before its owners moved out. A handler that
#: reads one as ``mcp_core.<name>`` keeps type-checking only while the facade binds it.
_BASE_PROJECT_IMPORTS = frozenset("""
    CORE_MCP_SERVER CallerContext ConversationLog DIRECTIVE_TOOLS DeferredTool
    HybridRetriever KiroCrewConfig KnowledgeStore MCP_CORE_SCHEMAS SEARCH_SCAN_WINDOW
    SkillsLoader UnknownMemoryStore binding_key_for build_tool_list build_tool_names
    call_tool_with_logging clear_vouch coerce_mcp_tool_args config_dir
    create_embedder_from_config current_caller dashboard_socket_path dedup_sweep
    dispatch drive_deferred external_client_identity_note host_pid_is_shared
    internal_caller is_incognito_transcript is_legacy_slack_key legacy_key list_agents
    loopback_urlopen map_deferred outbox_dir port_is_gateway_owned rank_triggered
    read_config_text read_local_secret record_activity redact redact_credentials
    redact_exfiltration_urls redact_local_paths refuse_if_markerless
    resolve_agent_bindings resolve_client_port_src resolve_own_identity
    run_mcp_stdio_loop sel session_key_from_env_token session_pid_mapping_path
    set_current_caller snippet_needles spawned_without_gateway_identity
    structured_monitor_binding_key_for summarize_result validate_tool_args vector_leg
    with_titles
""".split())


#: Base imports the facade serves from an owner instead of re-binding. The pid-reader
#: ratchet (``test_pid_reader_ratchet``) counts every import of the session-pid sidecar
#: path as a reader site, and no module reads this one through ``mcp_core``.
_SERVED_ONLY = frozenset({"session_pid_mapping_path"})


def _index() -> _facade_mirror.MirrorIndex:
    return mcp_core.__dict__[_facade_mirror.INDEX_ATTRIBUTE]


def _family() -> list[ModuleType]:
    return [mcp_core, *(sys.modules[name] for name in _index().owners)]


def _test_tree_files() -> list[Path]:
    root = source_corpus.repo_root()
    found = []
    for path in source_corpus.repo_files_named(".py"):
        rel = path.relative_to(root).as_posix()
        if rel == "conftest.py" or rel.startswith("test/"):
            found.append(path)
        elif rel.startswith("src/kiro_crew/apps/builtins/") and (
            "/tests/" in rel or path.name.startswith("test_") or path.name == "conftest.py"
        ):
            found.append(path)
    return found


def _spellings() -> dict[str, str]:
    """The facade's own name and every src module's module-level binding of it (each
    ``mcp_tools`` domain binds ``from kiro_crew import mcp_core``)."""
    spellings = {FACADE: FACADE}
    for path, _text, tree in source_corpus.parsed_candidates(require_any=("mcp_core",)):
        rel = path.relative_to(source_corpus.src_root()).with_suffix("")
        module = ".".join(("kiro_crew", *rel.parts)).removesuffix(".__init__")
        for node in tree.body:
            if isinstance(node, ast.ImportFrom) and node.module == "kiro_crew":
                for alias in node.names:
                    if alias.name == "mcp_core" and module != FACADE:
                        spellings[f"{module}.{alias.asname or alias.name}"] = FACADE
    return spellings


def _corpus_patched_names() -> set[str]:
    spellings = _spellings()
    names: set[str] = set()
    for path in _test_tree_files():
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


def test_the_spelling_table_reads_the_handler_bindings() -> None:
    """The handlers bind the facade at module level; a patch through that binding is a
    patch of the facade, so the scan must spell it."""
    assert "kiro_crew.mcp_tools.control.mcp_core" in _spellings()


def test_a_facade_patch_of_every_scanned_name_reaches_every_module_holding_it() -> None:
    """The reach the one module had: a write to ``mcp_core.<name>`` was what every
    reader of the name read. Every member of the family that binds a patched name sees
    the patched object, and the undo leaves the family one object per name again."""
    family = _family()
    missed: dict[str, list[str]] = {}
    checked = 0
    for name in sorted(_corpus_patched_names()):
        if not hasattr(mcp_core, name):
            continue  # a test that creates the name with raising=False
        holders = [module for module in family if name in vars(module)]
        sentinel = object()
        with pytest.MonkeyPatch.context() as patch:
            patch.setattr(mcp_core, name, sentinel)
            stale = [m.__name__ for m in holders if vars(m).get(name) is not sentinel]
            if getattr(mcp_core, name) is not sentinel:
                stale.append(FACADE)
        checked += 1
        if stale:
            missed[name] = stale
    assert missed == {}
    assert checked >= len(_KNOWN_SEAMS)
    assert _index().divergent() == ()


def test_the_owners_are_the_parts_package_in_layer_order() -> None:
    """``_OWNERS`` names every module of ``mcp_core_parts``, and each owner imports only
    owners listed before it: the order the facade loads them and a read resolves in."""
    owners = list(_index().owners)
    on_disk = {
        f"kiro_crew.mcp_core_parts.{p.stem}": p
        for p in PARTS_DIR.glob("*.py")
        if p.name != "__init__.py"
    }
    assert sorted(owners) == sorted(on_disk)
    late: dict[str, list[str]] = {}
    for position, owner in enumerate(owners):
        tree = ast.parse(on_disk[owner].read_text(encoding="utf-8"))
        imported = {n.module for n in ast.walk(tree) if isinstance(n, ast.ImportFrom)}
        later = sorted(m for m in imported if m in owners and owners.index(m) >= position)
        if later:
            late[owner] = later
    assert late == {}


def test_the_facade_keeps_the_default_read_path_and_forwards_no_table() -> None:
    """``mcp_core.py`` was a plain module: the facade forwards no legacy table and keeps
    the default ``forward_reads``, and every served name comes from the index."""
    index = _index()
    assert index.forward_reads == "sys_modules_first"
    assert dict(index.forwards) == {}
    assert dict(index.served) == dict(index.exports)


def test_a_facade_write_of_the_port_cache_moves_the_cache_the_resolver_reads() -> None:
    """``mcp_core._API_PORT = n`` pinned the port every request dialled, and still does,
    wherever ``_api_port`` lives: the cache is the gateway owner's, and the mirror writes
    it there."""
    gateway = sys.modules["kiro_crew.mcp_core_parts.gateway"]
    with pytest.MonkeyPatch.context() as patch:
        patch.setattr(mcp_core, "_API_PORT", 47123)
        assert mcp_core._api_port() == 47123
        assert vars(gateway)["_API_PORT"] == 47123
    assert "_API_PORT" not in vars(mcp_core)


def _static_bindings(path: Path) -> set[str]:
    """Names a module binds at import time with a module-level ``import``, outside
    ``if TYPE_CHECKING:`` -- the bindings a type checker reads off the module."""
    found: set[str] = set()
    for node in ast.parse(path.read_text(encoding="utf-8")).body:
        if isinstance(node, (ast.Import, ast.ImportFrom)):
            found |= {(a.asname or a.name).split(".")[0] for a in node.names}
    return found


def _home(value: object) -> str:
    if isinstance(value, ModuleType):
        return value.__name__
    return getattr(value, "__module__", "") or ""


def test_every_project_name_the_facade_imported_stays_bound_on_it() -> None:
    """A name the facade imported from the rest of ``kiro_crew`` is bound on it still,
    statically, so a handler reading ``mcp_core.<name>`` type-checks; the one exception,
    ``_SERVED_ONLY``, is served from the identity owner's binding. No other name the
    mirror serves comes from a project module outside the family."""
    bound = _static_bindings(Path(mcp_core.__file__))
    assert "loopback_urlopen" in bound, "the static reader went blind"
    assert len(_BASE_PROJECT_IMPORTS) == 60
    assert sorted(name for name in _BASE_PROJECT_IMPORTS if name not in bound) == sorted(
        _SERVED_ONLY
    )
    for name in _SERVED_ONLY:
        identity = sys.modules["kiro_crew.mcp_core_parts.identity"]
        assert getattr(mcp_core, name) is vars(identity)[name], name
    family = {FACADE, *_index().owners}
    defined = {
        name
        for owner in _index().owners
        for name in _facade_mirror._defined_names(
            ast.parse(Path(sys.modules[owner].__file__ or "").read_text(encoding="utf-8"))
        )
    }
    imported_by_owners = sorted(set(_index().exports) - defined)
    assert imported_by_owners, "no served name is an owner's import, so this case measured nothing"
    served_from_outside = {
        name: _home(getattr(mcp_core, name))
        for name in imported_by_owners
        if _home(getattr(mcp_core, name)).startswith("kiro_crew")
        and _home(getattr(mcp_core, name)) not in family
    }
    assert served_from_outside == {name: "kiro_crew.session_pid_sig" for name in _SERVED_ONLY}
