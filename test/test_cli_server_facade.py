"""``kiro_crew.cli_server`` keeps the patch seams and the surface the one module had.

``kiro_crew.cli_server`` is a mirror facade (:mod:`kiro_crew._facade_mirror`) over the owners
in its ``_OWNERS``, lowest layer first: ``cli_server_peer``, ``cli_server_requests``,
``cli_server_stop``, ``cli_server_restart`` and ``cli_server_update``. It keeps the
status, gateway, run, service, sandbox and logs commands itself, and
every name an owner defines still resolves as ``kiro_crew.cli_server.<name>``.

``test/test_facade_mirror_guards.py`` holds every mirrored facade to the shared rules. These
tests pin what is particular to this one: every name the test trees patch through it reaches
every module holding it; the owner list and its layering; that every public name the module
held before the split still type-checks through it; that the owners log as
``kiro_crew.cli_server``; that the peer proof's exception and enum keep the module name they
print with; the frozen import inventory that stands in for the F401 check ``setup.cfg``
switches off for it; and that each owner's import path is a credential-mint surface to the
inline-payload gate, as the one module's was.
"""

from __future__ import annotations

import ast
import functools
import re
import sys
from pathlib import Path
from types import ModuleType

import facade_mirror_scan
import pytest
import source_corpus
import test_campaign_facades_base_public_names as base_names

from kiro_crew import _facade_mirror, cli_server, security
from kiro_crew.security import argv_floor, inline_payload

pytestmark = pytest.mark.xdist_group(name="tree_scan_cli_server_facade")

#: Checks ``test_facade_shared_checks.py`` runs against this module's helpers; each is
#: the same for every facade, so it is written once there and parametrized per module.
SHARED_FACADE_CHECKS = (
    "known_seams",
    "spelling_cases",
    "corpus_gate",
    "reach_routing",
    "reach_scan_can_fail",
    "owner_layering",
    "home_manifest",
    "logger_scan_can_fail",
)

FACADE = "kiro_crew.cli_server"
OWNERS = (
    "kiro_crew.cli_server_peer",
    "kiro_crew.cli_server_requests",
    "kiro_crew.cli_server_stop",
    "kiro_crew.cli_server_restart",
    "kiro_crew.cli_server_update",
)

#: Names the test trees are known to patch through the facade, one or more per kind of
#: seam: a helper each owner reads, a module constant an owner reads, a helper the facade's
#: own commands read, a name the facade only imports, and a module rebound wholesale. The
#: scan below must find all of them, so a scanner that stops matching goes red instead of
#: passing over nothing.
_KNOWN_SEAMS = frozenset(
    {
        "_owner_verified_secret_urlopen",
        "_pid_exited",
        "_stop_mcp_gateway_daemon",
        "_spawn_detached_gateway",
        "_wait_for_pids_exit",
        "_RESTART_TOKEN_WAIT",
        "_update_wheel",
        "_snapshot_memory_or_exit",
        "_probe_dashboard_health",
        "_should_reconcile_launchd_launcher",
        "loopback_urlopen",
        "read_local_secret",
        "config_dir",
        "lock_holder",
        "sel",
        "time",
    }
)

#: A test file worth parsing spells the facade itself, never only a ``cli_server_*`` owner.
_SPELLS_THE_FACADE = re.compile(
    r"kiro_crew\.cli_server(?![\w])"
    r"|from\s+kiro_crew\s+import\s+(?:\([^)]*?\bcli_server\b(?!_)|[^\n]*?\bcli_server\b(?!_))"
)

#: Every name the facade's import statements bound before its owners moved out. They stay
#: bound here because callers and tests read and patch them as attributes of this module.
_BASE_IMPORTS = frozenset("""
    ContextBuilder ConversationLog DASHBOARD_HTML_NOT_FOUND_MARKER DivergenceUnreadable
    EXTERNALLY_MANAGED_MESSAGES HistoryConsolidator HookManager KiroCrewConfig LessonStore
    LockProbeError MANAGED_BY_GIT MemoryStore NoReturn PATH_ONLY_INSTALL_NOTE Path
    PeerCredResult Platform RESTART_NOT_UP RESTART_REFUSED RESTART_UNCONFIRMED SERVICE_NAME
    SessionManager SkillsLoader TaskRunner UNREADABLE_UNPARSEABLE UTF8_TEXT VectorMemoryStore
    _KIROCREW_SERVER_SUBCOMMANDS __version__ _args_look_like_kirocrew _basename_stem
    _config_url_port _deadline_expired _gateway_owns_port _is_kirocrew_process _marker_port
    _session_work_dir activate_mise argparse asyncio build_frontend_sync build_provider_factory
    check_peer_is_self codecs config_dir config_path count_divergence_sync current_platform
    dashboard_origin dashboard_socket_path dep_sync deque derive_capability distribution
    ensure_dev_dist_symlink enum get_peer_pid hooks_config_from_config_dict http io
    is_bundled_kiro_cli is_default_home is_governance_pinned_off json lock_holder logging
    loopback_urlopen make_sync_embed_fn math model_file_present os owner_only_opener
    parse_duration pin_kiro_cli platform_compat read_local_secret register_skill_read_observer
    resolve_client_port resolve_client_port_ex resolve_client_port_src resolve_dashboard_host
    resolved_command_casing run_gateway run_marker run_preflight_checks sel service_controller
    shlex shutil signal socket store_embedding_space_is_stale subprocess svc_linux svc_macos sys
    tailnet_origin tailnet_serve tcp_verified_urlopen time unix_socket_urlopen urllib
    """.split())

#: The names the facade's own glue imports: the owner loop, the mirror, its manifest guard.
_GLUE_IMPORTS = frozenset({"TYPE_CHECKING", "_facade_mirror", "_importlib"})

#: The logger every owner names, so a record reads as the one module's did.
_LOGGER_NAME = FACADE


def _index() -> _facade_mirror.MirrorIndex:
    index = vars(cli_server)[_facade_mirror.INDEX_ATTRIBUTE]
    assert isinstance(index, _facade_mirror.MirrorIndex)
    return index


def _family() -> list[ModuleType]:
    return [cli_server, *(sys.modules[name] for name in _index().owners)]


def _source(module: ModuleType) -> str:
    assert module.__file__ is not None
    return Path(module.__file__).read_text(encoding="utf-8")


def _tree(module: ModuleType) -> ast.Module:
    return ast.parse(_source(module))


def patched_names(source: str) -> set[str]:
    """Every name *source* patches through the facade, read by the guards' own reader."""
    return facade_mirror_scan.patched_names(source, {FACADE: FACADE}).get(FACADE, set())


# --------------------------------------------------------------------------- #
# The patch scan
# --------------------------------------------------------------------------- #


#: Each spelling the patch scan must read, and each it must skip, keyed by case id.
_SPELLING_CASES = {
    "monkeypatch-from-import": (
        "from kiro_crew import cli_server\n"
        "def t(monkeypatch):\n    monkeypatch.setattr(cli_server, 'A', 1)\n",
        {"A"},
    ),
    "patch-object-import-as": (
        "import kiro_crew.cli_server as cs\n"
        "from unittest import mock\n"
        "def t():\n    with mock.patch.object(cs, 'B'):\n        pass\n",
        {"B"},
    ),
    "mock-patch-string": (
        "from unittest.mock import patch\n"
        "def t():\n    with patch('kiro_crew.cli_server.C'):\n        pass\n",
        {"C"},
    ),
    "dotted-setattr": (
        "def t(monkeypatch):\n    monkeypatch.setattr('kiro_crew.cli_server.D', 1)\n",
        {"D"},
    ),
    # An owner is not the facade, even bound under the facade's name.
    "owner-named-like-the-facade": (
        "from kiro_crew import cli_server_stop as cli_server\n"
        "def t(monkeypatch):\n    monkeypatch.setattr(cli_server, 'X', 1)\n",
        set(),
    ),
    "owner-string": (
        "from unittest.mock import patch\n"
        "def t():\n    with patch('kiro_crew.cli_server_update.Y'):\n        pass\n",
        set(),
    ),
}


def _test_tree_files() -> list[Path]:
    root = source_corpus.repo_root()
    test_root = root / "test"
    out = []
    for path in source_corpus.repo_files_named(".py"):
        rel = path.relative_to(root).as_posix()
        if rel == "conftest.py" or path.is_relative_to(test_root) or "/tests/" in rel:
            out.append(path)
    return out


@functools.lru_cache(maxsize=1)
def _corpus_patched_names() -> frozenset[str]:
    """Every name the test trees patch through the facade, scanned once per worker."""
    found: set[str] = set()
    for path in _test_tree_files():
        text = path.read_text(encoding="utf-8", errors="replace")
        if _SPELLS_THE_FACADE.search(text):
            found |= patched_names(text)
    return frozenset(found)


# --------------------------------------------------------------------------- #
# Reach: a patch through the facade lands on every module holding the name
# --------------------------------------------------------------------------- #


def _reach_gaps(names: set[str], index: _facade_mirror.MirrorIndex) -> list[str]:
    """``owner.name`` for each patched name an owner binds that the index does not route."""
    return [
        f"{module.__name__}.{name}"
        for name in sorted(names)
        for module in _family()[1:]
        if name in vars(module) and module.__name__ not in index.holders.get(name, ())
    ]


def test_a_facade_patch_of_every_scanned_name_reaches_every_module_holding_it() -> None:
    family = _family()
    missed: dict[str, list[str]] = {}
    for name in sorted(_corpus_patched_names()):
        if not hasattr(cli_server, name) or isinstance(getattr(cli_server, name), ModuleType):
            continue  # a shared module (os, sys, time) is one object in every holder
        sentinel = object()
        holders = [module for module in family if name in vars(module)]
        with pytest.MonkeyPatch.context() as patch:
            patch.setattr(cli_server, name, sentinel)
            stale = [m.__name__ for m in holders if vars(m).get(name) is not sentinel]
            if getattr(cli_server, name) is not sentinel:
                stale.append(FACADE)
        if stale:
            missed[name] = stale
    assert missed == {}
    assert _index().divergent() == ()


def test_a_facade_write_of_the_time_module_reaches_every_reader() -> None:
    """``test_cli_restart_readiness.py`` and ``test_cli_token.py`` swap the whole ``time`` binding through the facade
    (``patch.object(cli_server, "time", fake)``) for the readiness waits, which now
    live in ``cli_server_restart``: the swap must land there and in the stop owner."""
    sentinel = object()
    readers = [m for m in _family() if "time" in vars(m)]
    assert {m.__name__ for m in readers} >= {
        FACADE,
        "kiro_crew.cli_server_stop",
        "kiro_crew.cli_server_restart",
    }
    with pytest.MonkeyPatch.context() as patch:
        patch.setattr(cli_server, "time", sentinel)
        assert [m.__name__ for m in readers if vars(m)["time"] is not sentinel] == []
    assert all(vars(m)["time"] is not sentinel for m in readers)


# --------------------------------------------------------------------------- #
# The owners and their layering
# --------------------------------------------------------------------------- #


def test_the_mirror_indexes_exactly_the_owners_with_no_exclusion_and_no_forward() -> None:
    index = _index()
    assert index.owners == OWNERS == cli_server._OWNERS
    assert index.excluded == ()
    assert index.forward_reads == "sys_modules_first"
    assert dict(index.forwards) == {}
    assert dict(index.served) == dict(index.exports) == cli_server._EXPORTS
    assert index.divergent() == ()


def test_the_owners_are_every_cli_server_sibling() -> None:
    assert cli_server.__file__ is not None
    siblings = sorted(
        f"kiro_crew.{path.stem}"
        for path in Path(cli_server.__file__).parent.glob("cli_server_*.py")
    )
    assert siblings == sorted(OWNERS)


def _project_imports(tree: ast.Module) -> set[str]:
    found: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.ImportFrom) and node.module and not node.level:
            found.add(node.module)
            found.update(f"{node.module}.{alias.name}" for alias in node.names)
        elif isinstance(node, ast.Import):
            found.update(alias.name for alias in node.names)
    return found


def _layering_gaps(trees: dict[str, ast.Module]) -> dict[str, list[str]]:
    """Owner -> the family modules it imports that it may not: the facade, or an owner
    listed at or after it."""
    gaps: dict[str, list[str]] = {}
    for position, owner in enumerate(OWNERS):
        allowed = set(OWNERS[:position])
        reached = _project_imports(trees[owner]) & {FACADE, *OWNERS}
        bad = sorted(reached - allowed)
        if bad:
            gaps[owner] = bad
    return gaps


def test_the_layering_check_can_fail() -> None:
    trees = {name: _tree(sys.modules[name]) for name in OWNERS}
    trees[OWNERS[0]] = ast.parse("from kiro_crew.cli_server_update import _update\n")
    trees[OWNERS[1]] = ast.parse("import kiro_crew.cli_server\n")
    assert _layering_gaps(trees) == {
        OWNERS[0]: [OWNERS[4]],
        OWNERS[1]: [FACADE],
    }


# --------------------------------------------------------------------------- #
# Static visibility
# --------------------------------------------------------------------------- #


def _home_imports() -> dict[str, tuple[str, str]]:
    """Names an owner imports from a project module outside the family."""
    family = set(OWNERS)
    found: dict[str, tuple[str, str]] = {}
    for owner in OWNERS:
        for node in _tree(sys.modules[owner]).body:
            if isinstance(node, ast.ImportFrom) and node.module and not node.level:
                if not node.module.startswith("kiro_crew") or node.module in family:
                    continue
                for alias in node.names:
                    if f"{node.module}.{alias.name}" not in family:
                        found.setdefault(alias.asname or alias.name, (node.module, alias.name))
    return found


def _home_manifest() -> dict[str, tuple[str, str]]:
    """The facade's ``TYPE_CHECKING`` imports from outside the family."""
    out: dict[str, tuple[str, str]] = {}
    for node in _tree(cli_server).body:
        if isinstance(node, ast.If) and ast.unparse(node.test) == "TYPE_CHECKING":
            for stmt in node.body:
                if isinstance(stmt, ast.ImportFrom) and stmt.module not in set(OWNERS):
                    for alias in stmt.names:
                        out[alias.asname or alias.name] = (stmt.module or "", alias.name)
    return out


def _statically_bound(tree: ast.Module) -> set[str]:
    """Names a type checker sees on the module: every module-level binding, the
    ``TYPE_CHECKING`` imports included."""
    names = set(_facade_mirror._defined_names(tree))
    for stmt in _facade_mirror._module_level_statements(tree):
        if isinstance(stmt, (ast.Import, ast.ImportFrom)):
            names |= {alias.asname or alias.name.split(".")[0] for alias in stmt.names}
    return names


def test_every_base_public_name_type_checks_through_the_facade() -> None:
    missing = base_names._BASE_PUBLIC_NAMES[FACADE] - _statically_bound(_tree(cli_server))
    assert sorted(missing) == []


def test_every_served_name_type_checks_through_the_facade() -> None:
    """``cli.py`` imports the moved commands from here, so mypy must see each one."""
    assert sorted(set(_index().exports) - _statically_bound(_tree(cli_server))) == []


# --------------------------------------------------------------------------- #
# The logger and the import inventory
# --------------------------------------------------------------------------- #


def _logger_names(tree: ast.Module) -> list[str]:
    """The argument of every ``logging.getLogger(...)`` call, unparsed."""
    return [
        ast.unparse(node.args[0]) if node.args else ""
        for node in ast.walk(tree)
        if isinstance(node, ast.Call) and ast.unparse(node.func) == "logging.getLogger"
    ]


def test_the_owners_log_under_the_facade_name() -> None:
    """The moved commands log as the one module did: an owner's ``__name__`` would be a
    logger no filter, level or capture names (``caplog.at_level(logger=...)``)."""
    names = [n for owner in OWNERS for n in _logger_names(_tree(sys.modules[owner]))]
    assert len(names) >= 9, "the scan found too few logger calls -- it went blind"
    assert set(names) == {repr(_LOGGER_NAME)}


@pytest.mark.parametrize("name", ["_GatewayPeerRefused", "_ConnectedPeerVerdict"])
def test_the_peer_proofs_exception_and_enum_keep_the_facade_module(name: str) -> None:
    """A refusal that escapes, and a verdict that is logged, name the one import path."""
    peer = sys.modules["kiro_crew.cli_server_peer"]
    value = vars(peer)[name]
    assert getattr(cli_server, name) is value
    assert value.__module__ == FACADE


def unexpected_imports(source: str, owner_names: dict[str, set[str]]) -> list[str]:
    """Names *source* binds by a runtime import beyond the frozen inventory.

    The inventory is the BASE imports, the glue's own, and from each owner module only
    names that owner defines. Imports under ``if TYPE_CHECKING:`` bind nothing at run
    time and are the mirror guard's business, so they are skipped.
    """
    found: list[str] = []
    for node in ast.parse(source).body:
        if isinstance(node, ast.If) and ast.unparse(node.test) == "TYPE_CHECKING":
            continue
        if isinstance(node, ast.Import):
            bound = [(alias.asname or alias.name.split(".")[0], None) for alias in node.names]
        elif isinstance(node, ast.ImportFrom) and node.module != "__future__":
            bound = [(alias.asname or alias.name, node.module) for alias in node.names]
        else:
            continue
        for name, module in bound:
            if module in owner_names:
                if name not in owner_names[module]:
                    found.append(f"{module}.{name}")
            elif name not in _BASE_IMPORTS | _GLUE_IMPORTS:
                found.append(name)
    return found


def _owner_names() -> dict[str, set[str]]:
    return {owner: _facade_mirror._defined_names(_tree(sys.modules[owner])) for owner in OWNERS}


def test_the_facade_imports_nothing_beyond_its_frozen_inventory() -> None:
    assert unexpected_imports(_source(cli_server), _owner_names()) == []


def test_the_inventory_check_reports_a_planted_import() -> None:
    """The F401 stand-in must be able to fail: a stray import, and an owner import of a
    name that owner does not define, are both reported."""
    planted = (
        _source(cli_server) + "\nimport pickle\nfrom kiro_crew.cli_server_stop import _update\n"
    )
    assert unexpected_imports(planted, _owner_names()) == [
        "pickle",
        "kiro_crew.cli_server_stop._update",
    ]


# --------------------------------------------------------------------------- #
# The inline-payload mint fence
# --------------------------------------------------------------------------- #


def _mint_spellings(module: str) -> list[str]:
    stem = module.rpartition(".")[2]
    return [
        f"python -c 'from {module} import x; x()'",
        f"python -c 'import {module} as m; m.x()'",
        f"python -c 'from kiro_crew import {stem}; {stem}.x()'",
        f"python3 < src/kiro_crew/{stem}.py",
    ]


@pytest.mark.parametrize("module", [FACADE, *OWNERS])
def test_every_owner_import_path_is_a_mint_surface(module: str) -> None:
    """The token command mints, the peer proof sends the local secret, and the stop, restart
    and update commands read it and print fresh token URLs; they were mint surfaces as
    ``kiro_crew.cli_server``, and an inline payload that imports them from their owner is one
    still."""
    for cmd in _mint_spellings(module):
        assert argv_floor._is_credential_mint(cmd.lower(), raw_text=cmd), cmd
        assert inline_payload._MINT_SURFACE_RE.search(cmd.lower()), cmd


def test_a_restart_through_its_owner_is_denied() -> None:
    cmd = 'python -c "from kiro_crew.cli_server_restart import _restart; _restart()"'
    assert security.is_denied(cmd) is not None


@pytest.mark.parametrize("module", ["kiro_crew.cli_setup", "kiro_crew.cli_serverless"])
def test_a_neighbouring_module_is_not_a_mint_surface(module: str) -> None:
    """The fence names the facade and its ``cli_server_<role>`` owners, nothing else."""
    for cmd in _mint_spellings(module):
        assert not inline_payload._MINT_SURFACE_RE.search(cmd.lower()), cmd
