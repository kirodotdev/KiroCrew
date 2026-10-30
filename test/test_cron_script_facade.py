"""``kiro_crew.cron_script`` stays the one import path and patch surface of its owners.

The script-cron module is a :mod:`kiro_crew._facade_mirror` facade: it keeps the two
runners (``run_script_sandboxed``, ``run_command_sandboxed``) and the command-shell
probe, and the rest lives in the ``cron_script_<role>.py`` owners beside it. Callers,
tests, documents and the script launcher still reach every name as
``kiro_crew.cron_script.<name>``, and the moved code reads its names as plain module
globals, so a patch through the facade reaches that code only because the mirror writes
it into every module holding the name.

``test/test_facade_mirror_guards.py`` holds every mirrored facade to the shared rules
(one object per name, the manifest, owners never importing their facade, no shared
global rebound). These tests pin what is particular to this facade:

* every name the test trees patch through it reaches every module holding it;
* the owners are exactly the ``cron_script_*.py`` siblings, loaded lowest layer first;
* every module-level name the unsplit file bound still resolves through it;
* every name it serves from outside the family is named for type checkers;
* nothing outside the family imports an owner;
* the family logs under the facade's name, and the moved exception classes keep its
  ``__module__``, because a traceback names them by it;
* the names the script launcher imports in the child still resolve, and a grant's key
  names recorded through the facade reach the environment builder;
* the facade still runs as ``python -m``.
"""

from __future__ import annotations

import ast
import functools
import importlib
import os
import subprocess
import sys
from collections.abc import Iterable
from pathlib import Path
from types import ModuleType

import facade_mirror_scan
import pytest
import source_corpus

import kiro_crew
from kiro_crew import _facade_mirror
from kiro_crew import cron_script as facade
from kiro_crew.subprocess_utf8 import UTF8_TEXT

# Walks the test trees and src/kiro_crew; keep it on one worker.
pytestmark = pytest.mark.xdist_group(name="tree_scan_cron_script_facade")

FACADE = "kiro_crew.cron_script"
OWNER_PREFIX = "kiro_crew.cron_script_"
_SRC = Path(kiro_crew.__file__).resolve().parent
_THIS_FILE = Path(__file__).resolve()

#: Names the test trees are known to patch through the facade, one of each seam the
#: module has: a runner helper the facade keeps, functions and tables an owner holds
#: and the facade reads, names only an owner reads, names the family imports, and a
#: class the moved code constructs. The scan below must find all of them, so a scanner
#: that stops matching goes red instead of passing over nothing.
_KNOWN_SEAMS = frozenset(
    {
        "McpToolClient",
        "Path",
        "_POSIX_STRICT_CACHE",
        "_RUNNING_PROCS",
        "_clean_cron_env",
        "_resolve_command_shell",
        "_resolve_internal_secret",
        "_resolve_mcp_server",
        "_secret_env_precheck",
        "config_dir",
        "kill_running_process",
        "loopback_urlopen",
        "sel",
        "wrap_argv",
    }
)

#: Every module-level name ``cron_script.py`` bound before the split.
_BASE_MODULE_NAMES = frozenset("""
    Any CANONICAL_TEMP_KEYS CHILD_PERSISTENCE_EXIT_CODE CHILD_PERSISTENCE_PREFIX
    CRON_SCRIPT_CHILD_ENV Callable Done DoneError FileTooLargeError KeptMcpServers
    McpToolClient McpToolError Path Report ReportError STUB_SESSION_TOKEN_ENV
    SandboxUnavailableError ScriptContext SecretVault Skip SkipError TYPE_CHECKING UTF8_TEXT
    _AGENT_DENIED_ENV_KEYS _BRACE_OFF_FLAG _BRACE_OFF_SHELLS _CANCELLED_PROC_JOBS
    _CRON_ENV_DENY _GRANTED_ENV_KEYS _GRANT_EPOCHS_LOCK _KILL_ESCALATION_GRACE_SECS
    _MAX_BAD_OUTPUT_HEAD _MAX_COMMAND_OUTPUT _MAX_GRANTED_SCRIPT_BYTES
    _MAX_SCRIPT_STDERR_TAIL _PEM_BEGIN_LITERAL _PEM_KEY_MARKER_RE _PEM_SCAN_CARRY
    _PEM_SCAN_CHUNK _PEM_SEVERED_LABEL_RE _POSIX_STRICT_CACHE _PROCS_LOCK
    _REDACTED_CREDENTIAL_TAG _REDACT_STRADDLE_MARGIN _REFUSAL_CODE _RUNNING_PROCS
    _SANDBOX_UNAVAILABLE_PREFIX _SCRATCH_OWNER_ERROR _SECRET_ENV_DENIED_EXACT
    _SECRET_ENV_DENIED_PREFIXES _SECRET_ENV_MAX_ENTRIES _SECRET_ENV_NAME_RE
    _SECRET_ENV_VAULT_NAME_RE _SHELL_PROBE_LOCK _SPAWNING_JOBS _STDERR_TAIL_READ_SPAN
    _STREAM_HOLDBACK_JWT_MAX _abandon_spawn _allocate_cron_scratch _argv_for_form
    _begin_spawn _bundle_relative_spec _child_internal_secret _clean_cron_env _command_argv
    _default_temp_dir _discard_unused_cron_scratch _drain_after_kill _filter_grant_env
    _finish_spawn _grant_epoch _grant_epochs_guard _grant_epochs_path _grant_pin_key
    _kill_proc_group _mask_from_open_block _may_end_inside_begin_marker
    _no_command_shell_message _pem_open_label _pem_state_before _pin_digest _pin_payload
    _probe_one_form _publish_script_session_token _read_agent_spec _read_grant_epochs
    _read_script_body _record_cron_scratch_owner _refusal_code _resolve_command_shell
    _resolve_dial_port _resolve_internal_secret _resolve_mcp_server _resolve_safe_pgid
    _resolve_secret_env _safe_tail_redaction_window _scrub_grant_values _secret_env_precheck
    _shell_is_posix_strict _spawn_cancelled _split_script_spec _trusted_script_bundle_roots
    _unregister_proc _write_grant_epochs agent_scratch base64 bump_grant_epoch
    cgroup_scope_argv child_persistence_preflight commit_grant_epoch compute_secret_env_pin
    config_dir contextmanager data_home dataclass delivery_fingerprint field grant_epoch_ids
    hashlib hmac is_unverifiable_path_refusal json kill_running_process kiro_agents_dir
    logger logging loopback_urlopen lru_cache os parse_json_object_line peek_grant_epoch
    platform_compat popen_limited prevalidated_gh_env re read_local_secret redact
    refuse_unaudited_on_dead_fs replace_with_retry resolve_script_path resolve_serving_port
    run_command_sandboxed run_limited run_script_sandboxed safe_read_file_bytes_nolink
    sanitize_spec_env sel sensitive_path_refusal shutil signal subprocess sys tempfile
    threading time tmp_env urllib validate_secret_env_grant wrap_argv
    """.split())

#: The exception classes that moved: a traceback prints each by its module.
_MOVED_EXCEPTIONS = ("SkipError", "DoneError", "ReportError", "McpToolError")


def _index() -> _facade_mirror.MirrorIndex:
    index = vars(facade)[_facade_mirror.INDEX_ATTRIBUTE]
    assert isinstance(index, _facade_mirror.MirrorIndex)
    return index


def _owner_paths() -> dict[str, Path]:
    return {
        f"{OWNER_PREFIX}{path.stem[len('cron_script_'):]}": path
        for path in sorted(_SRC.glob("cron_script_*.py"))
    }


def _family() -> list[ModuleType]:
    return [facade, *(sys.modules[name] for name in _index().owners)]


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
        if "cron_script" not in text:
            continue
        names = facade_mirror_scan.patched_names(text, {FACADE: FACADE})
        found |= names.get(FACADE, set())
    return frozenset(found)


def test_the_patch_scan_finds_every_known_seam() -> None:
    """Non-vacuity: a scan that finds nothing would pass the reach test over nothing."""
    assert sorted(_KNOWN_SEAMS - _corpus_patched_names()) == []


def test_a_facade_patch_of_every_scanned_name_reaches_every_module_holding_it() -> None:
    """The reach the unsplit module had: a write to ``cron_script.<name>`` was what every
    reader of that name read. Each module of the family that binds a patched name must see
    the patched object, and the undo must restore all of them."""
    family = _family()
    missed: dict[str, list[str]] = {}
    for name in sorted(_corpus_patched_names()):
        if not hasattr(facade, name):
            continue
        sentinel = object()
        holders = [module for module in family if name in vars(module)]
        originals = {module.__name__: vars(module)[name] for module in holders}
        with pytest.MonkeyPatch.context() as patch:
            patch.setattr(facade, name, sentinel)
            stale = [m.__name__ for m in holders if vars(m).get(name) is not sentinel]
            if getattr(facade, name) is not sentinel:
                stale.append(FACADE)
        restored = [m.__name__ for m in holders if vars(m).get(name) is not originals[m.__name__]]
        if stale or restored:
            missed[name] = stale + [f"{module} not restored" for module in restored]
    assert missed == {}
    assert _index().divergent() == ()


def test_the_owners_are_every_cron_script_sibling_in_layer_order() -> None:
    """``_OWNERS`` lists every ``cron_script_*.py`` sibling, and each owner imports only
    owners listed before it -- the order the facade loads them and a read resolves in."""
    owners = list(_index().owners)
    assert sorted(owners) == sorted(_owner_paths())
    late: dict[str, list[str]] = {}
    for position, owner in enumerate(owners):
        tree = ast.parse(_owner_paths()[owner].read_text(encoding="utf-8"))
        imported = {
            node.module
            for node in ast.walk(tree)
            if isinstance(node, ast.ImportFrom) and node.module
        }
        later = sorted(m for m in imported if m in owners and owners.index(m) >= position)
        if later:
            late[owner] = later
    assert late == {}
    # Non-vacuity: the layering is real, an owner reads names from an owner below it.
    assert any(
        f"from {owners[0]} import" in _owner_paths()[owner].read_text(encoding="utf-8")
        for owner in owners[1:]
    )


def test_the_facade_keeps_the_default_read_path_and_its_two_runners() -> None:
    """``cron_script.py`` was a plain module, so no reader fetched a name of it through
    ``importlib.import_module``: the facade forwards no legacy table and keeps the default
    ``forward_reads``. It still defines the two runners the cron service calls."""
    index = _index()
    assert index.forward_reads == "sys_modules_first"
    assert dict(index.forwards) == {}
    for runner in ("run_script_sandboxed", "run_command_sandboxed"):
        assert getattr(facade, runner).__module__ == FACADE, runner


def test_every_base_module_name_resolves_through_the_facade() -> None:
    """Each name the unsplit module bound is still an attribute of the facade, and a name
    an owner defines resolves to that owner's own object."""
    assert sorted(name for name in _BASE_MODULE_NAMES if not hasattr(facade, name)) == []
    for name, owner in _index().exports.items():
        assert getattr(facade, name) is vars(sys.modules[owner])[name], name


def _home_imports() -> dict[str, tuple[str, str]]:
    """Served names an owner imports from a project module outside the family."""
    family = set(_index().owners)
    found: dict[str, tuple[str, str]] = {}
    for path in _owner_paths().values():
        for node in ast.parse(path.read_text(encoding="utf-8")).body:
            if not isinstance(node, ast.ImportFrom) or not node.module:
                continue
            if not node.module.startswith("kiro_crew") or node.module in family:
                continue
            for alias in node.names:
                if f"{node.module}.{alias.name}" not in family:
                    found.setdefault(alias.asname or alias.name, (node.module, alias.name))
    return found


def _home_manifest(source: str) -> dict[str, tuple[str, str]]:
    """The ``TYPE_CHECKING`` imports of *source* from outside the family."""
    family = set(_index().owners)
    out: dict[str, tuple[str, str]] = {}
    for node in ast.parse(source).body:
        if isinstance(node, ast.If) and ast.unparse(node.test) == "TYPE_CHECKING":
            for stmt in node.body:
                if isinstance(stmt, ast.ImportFrom) and stmt.module not in family:
                    for alias in stmt.names:
                        out[alias.asname or alias.name] = (stmt.module or "", alias.name)
    return out


def _facade_source() -> str:
    return Path(facade.__file__).read_text(encoding="utf-8")


def test_every_name_served_from_outside_the_family_is_visible_to_type_checkers() -> None:
    """At BASE the module bound these by import, so ``from kiro_crew.cron_script import
    config_dir`` type-checked. Where an owner holds one now and the facade does not bind
    it, the facade names it from its home module under ``TYPE_CHECKING``, and nothing else."""
    served = set(_index().exports)
    expected = {name: home for name, home in _home_imports().items() if name in served}
    assert expected, "no served name comes from outside the family -- the scan went blind"
    assert _home_manifest(_facade_source()) == expected


def test_the_type_checker_census_finds_a_dropped_name() -> None:
    """The census above can fail: a manifest missing one served name differs."""
    source = _facade_source()
    dropped = source.replace("    from kiro_crew.sel import sel  # noqa: F401\n", "")
    assert dropped != source
    assert "sel" not in _home_manifest(dropped)
    assert "sel" in _home_manifest(source)


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
    """Callers reach the owners' names through the facade, so a patch of it reaches them."""
    family = {f"{name.rpartition('.')[2]}.py" for name in (FACADE, *_index().owners)}
    sources = source_corpus.iter_candidate_sources(require_all=("cron_script_",))
    importers = _owner_importers(
        ((path, text) for path, text in sources if path.is_relative_to(_SRC)), _SRC
    )
    assert "cron_script_grants.py" in importers  # owners import the owners below them
    assert importers - family == set()


def test_the_owner_import_scan_finds_an_outside_importer(tmp_path: Path) -> None:
    root = tmp_path / "kiro_crew"
    sources = {
        root / "pkg" / "caller.py": "from kiro_crew.cron_script_paths import resolve_script_path\n",
        root / "other.py": "from kiro_crew import cron_script_procs as procs\n",
        root / "relative.py": "from .cron_script_grants import bump_grant_epoch\n",
        root / "deep" / "up.py": "from .. import cron_script_mcp\n",
        root / "facade_user.py": "from kiro_crew.cron_script import resolve_script_path\n",
    }
    assert _owner_importers(sources.items(), root) == {
        "pkg/caller.py",
        "other.py",
        "relative.py",
        "deep/up.py",
    }


def test_the_family_logs_under_the_facade_name() -> None:
    """One logger, still named for the facade module, held by every module that logs."""
    assert facade.logger.name == FACADE
    loggers = {m.__name__ for m in _family() if "logger" in vars(m)}
    assert len(loggers) >= 2  # several owners log; none under its own module name
    others = [
        m.__name__ for m in _family() if vars(m).get("logger", facade.logger) is not facade.logger
    ]
    assert others == []


def test_the_moved_exceptions_keep_the_facade_module() -> None:
    """A traceback names each by the path callers import it from, and that path resolves it."""
    for name in _MOVED_EXCEPTIONS:
        cls = getattr(facade, name)
        assert cls.__module__ == FACADE, name
        assert getattr(importlib.import_module(cls.__module__), cls.__qualname__) is cls, name
    assert facade.Skip is facade.SkipError
    assert facade.Done is facade.DoneError
    assert facade.Report is facade.ReportError


def test_the_launcher_imports_still_resolve_and_a_grant_reaches_the_env_builder(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The granted-run launcher imports ``child_persistence_preflight`` and the context
    names from the facade, then records the granted key names on
    ``kiro_crew.cron_script._GRANTED_ENV_KEYS``. That set is the one ``_clean_cron_env``
    reads, so a granted secret never reaches a descendant of the child."""
    launcher = Path(facade.__file__).read_text(encoding="utf-8")
    assert "from kiro_crew.cron_script import child_persistence_preflight" in launcher
    assert "from kiro_crew.cron_script import ScriptContext, Skip, Done, Report" in launcher
    assert "_kcs._GRANTED_ENV_KEYS.update(" in launcher
    for name in ("child_persistence_preflight", "ScriptContext", "Skip", "Done", "Report"):
        assert hasattr(facade, name), name
    env_owner = sys.modules[facade._clean_cron_env.__module__]
    assert facade._GRANTED_ENV_KEYS is vars(env_owner)["_GRANTED_ENV_KEYS"]
    monkeypatch.setattr(facade, "_GRANTED_ENV_KEYS", set())
    monkeypatch.setenv("KC28_GRANTED_PROBE", "value")
    assert facade._clean_cron_env().get("KC28_GRANTED_PROBE") == "value"
    facade._GRANTED_ENV_KEYS.update({"KC28_GRANTED_PROBE": "value"})
    assert "KC28_GRANTED_PROBE" not in facade._clean_cron_env()


def test_the_facade_runs_as_a_module(tmp_path: Path) -> None:
    """``python -m kiro_crew.cron_script`` imported cleanly before the split; the mirror
    installs on ``__main__`` too, because every member binds one logger by name."""
    env = dict(os.environ)
    src = str(_SRC.parent)
    env["PYTHONPATH"] = os.pathsep.join(p for p in (src, env.get("PYTHONPATH", "")) if p)
    out = subprocess.run(
        [sys.executable, "-m", FACADE],
        capture_output=True,
        check=False,
        cwd=tmp_path,
        env=env,
        timeout=90,
        **UTF8_TEXT,
    )
    assert out.returncode == 0, out.stdout + out.stderr


def test_the_test_tree_walk_reaches_this_file() -> None:
    walked = {path.resolve() for path in _test_tree_files()}
    assert _THIS_FILE in walked
    assert source_corpus.repo_root() / "test" / "test_cron_cancel.py" in walked


def test_leaf_test_scope_derives_this_file_as_a_corpus_gate(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """It reads the whole test tree, so a leaf-only diff must still run it."""
    root = source_corpus.repo_root()
    monkeypatch.syspath_prepend(str(root / "scripts"))
    leaf_test_scope = importlib.import_module("leaf_test_scope")
    assert _THIS_FILE.relative_to(root).as_posix() in leaf_test_scope.corpus_gates(root)
