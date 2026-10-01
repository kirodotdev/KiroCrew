"""Who owns each snapshot rule, and that the facade hands out that owner's objects.

``kiro_crew.snapshot`` is the command and API facade; the rules it applies live in four
owner modules. These tests pin the ownership map itself: every name is defined in
exactly one module, the facade re-exports the owner's object rather than a copy and a
patch on the facade reaches the owner that calls it, the owners depend on one another in
one direction only and reach the facade only when a call reads a seam through it, and
every module is registered with the family source scans.
"""

from __future__ import annotations

import ast
import inspect
import os
import subprocess
import sys
import textwrap
from pathlib import Path

import pytest
from test_snapshot import SNAPSHOT_FAMILY

from kiro_crew import _sqlite_compat
from kiro_crew import snapshot as snap
from kiro_crew import snapshot_archive, snapshot_components, snapshot_merge, snapshot_restore

SRC = Path(snap.__file__).parent
REPO_SRC = SRC.parent

#: The owner of every name the facade exports, by module. A name moving between owners
#: is a change to this table, made on purpose, rather than something a re-export hides.
OWNERS: dict[str, tuple[str, ...]] = {
    "kiro_crew.snapshot_components": (
        "COMPONENTS",
        "COMPONENT_HELP",
        "COMPONENT_JSON_OBJECTS",
        "COMPONENT_JSON_VALIDATORS",
        "COMPONENT_TREES",
        "CORE_FILES",
        "CORE_FILES_FLAT",
        "ComponentRefused",
        "ComponentSpec",
        "NEVER_SNAPSHOT_FILES",
        "PRODUCT_TREE_DATABASES",
        "Purpose",
        "SECURITY_SENSITIVE_FILES",
        "SecretPolicy",
        "UnsafeComponentRoot",
        "VALID_COMPONENTS",
        "_CORE_FILE_COMPONENTS",
        "_DERIVED_INDEXES",
        "_HOST_LOCAL_PATHS",
        "_JSON_OBJECT_LISTS",
        "_LOCKED_DOCUMENT_TREES",
        "_REPLACE_ONLY_COMPONENTS",
        "_TREE_DOCUMENT_VALIDATORS",
        "_WHOLE_TREE_COMPONENTS",
        "_is_host_local",
        "_facade",
        "_mc_dir",
        "_never_ships",
        "_slack_workspace_record_defect",
        "_tree_roots_replace_clears",
        "_want",
        "is_product_tree_database",
        "resolve_components",
        "safe_tree_root",
    ),
    "kiro_crew.snapshot_archive": (
        "DB_COPIED",
        "DB_NOT_A_DATABASE",
        "DB_UNSAFE_SOURCE",
        "DatabaseCopyFailed",
        "EXPORT_MANIFEST_VERSION",
        "MANIFEST_VERSION",
        "ManifestUnreadable",
        "SKIP_DB_UNPINNED_SOURCE",
        "_ArchiveTooLarge",
        "_CONTROL_CHARS",
        "_DB_SIDECAR_GLOBS",
        "_DB_SUFFIXES",
        "_FIRST_EXPORT_VERSION_WITH_NAMED_STORES",
        "_FIRST_VERSION_WITH_NAMED_STORES",
        "_MAX_ARCHIVE_BYTES",
        "_MAX_ARCHIVE_MEMBERS",
        "_bundle_carries_named_stores",
        "_chain_is_link_free",
        "_copy_database_consistently",
        "_copytree_safe",
        "_data_filter",
        "_dir_flags_nofollow",
        "_escape_one",
        "_manifest_components",
        "_print_manifest",
        "_refuse_oversized_archive",
        "_refuse_unsound_required_capture",
        "_rejection_recording_filter",
        "_report_skip",
        "_restage_databases",
        "_safe_name",
        "_staging_ignore",
        "_staging_is_pinned",
        "_terminal_safe",
    ),
    "kiro_crew.snapshot_restore": (
        "NamedStoresInUse",
        "RollbackIncomplete",
        "SourceComponentUnsound",
        "_allocate_rollback_dir",
        "_backup_and_copy",
        "_backup_tree_or_refuse",
        "_clear_store_directories",
        "_component_payload_absent",
        "_bundle_record_names_workspace",
        "_components_absent_from_bundle",
        "_do_replace",
        "_do_replace_mutations",
        "_drop_derived_indexes_absent_from_bundle",
        "_install_locked_document",
        "_lock_down_restored",
        "_record_without_its_map",
        "_refuse_corrupt_source_databases",
        "_refuse_legacy_slack_links_without_record",
        "_refuse_unless_json_object",
        "_refuse_unless_sound",
        "_refuse_unless_valid_tree_document",
        "_refuse_unsafe_destination_roots",
        "_remove_locked_document",
        "_restore_everything_from_rollback",
        "_restore_locked_document",
        "_save_locked_document_to",
        "_trees_absent_from_bundle",
    ),
    "kiro_crew.snapshot_merge": (
        "NotificationCopyUnsupported",
        "_MERGE_ALLOWED_TABLES",
        "_NOTIFICATION_RECORD_CAP",
        "_NOTIFICATION_SOURCE_CAP",
        "_SAFE_IDENTIFIER_RE",
        "_TELEMETRY_SALT_BYTES",
        "_TERMINATORS",
        "_copy_locked",
        "_copy_tree_no_overwrite",
        "_install_notifications",
        "_merge_crons",
        "_merge_memory",
        "_merge_named_stores",
        "_merge_notifications",
        "_notification_key",
        "_open_notification_file",
        "_report_unmerged_databases",
        "_serialise_with_notification_writes",
        "_usable_cron_shape",
        "_validate_identifier",
    ),
    # What the facade itself defines: the two commands, the outbound redaction seam
    # (every redaction call name stays in this one module), the manifest writer, the
    # merge-mode driver, and the notification copy whose docstring records the ruling
    # for platforms without O_NOFOLLOW.
    "kiro_crew.snapshot": (
        "RedactionFailed",
        "SlackPairingUnstable",
        "_DASHBOARD_PORT",
        "_SLACK_PAIRING_ATTEMPTS",
        "_audit",
        "_build_snapshot",
        "_copy_notifications",
        "_default_snapshot_dir",
        "_do_merge",
        "_estimate_selected_bytes",
        "_fsize",
        "_is_gateway_running",
        "_list_components",
        "_redacted_upload_copy",
        "_redactor",
        "_report_redacted_bundle",
        "_report_redaction",
        "_report_unredacted_upload",
        "_report_unresolved_payload",
        "_stage_slack_pairing_stable",
        "_staged_bytes",
        "prepare_redacted_copy",
        "restore_main",
        "snapshot_main",
    ),
}

_MODULES = {
    "kiro_crew.snapshot": snap,
    "kiro_crew.snapshot_components": snapshot_components,
    "kiro_crew.snapshot_archive": snapshot_archive,
    "kiro_crew.snapshot_restore": snapshot_restore,
    "kiro_crew.snapshot_merge": snapshot_merge,
}

#: Which owners each owner may import. The facade imports all of them; nothing imports
#: the facade back, and the redaction pass stays out of every module's import time.
ALLOWED_IMPORTS: dict[str, frozenset[str]] = {
    "kiro_crew.snapshot_components": frozenset(),
    "kiro_crew.snapshot_archive": frozenset({"kiro_crew.snapshot_components"}),
    "kiro_crew.snapshot_restore": frozenset(
        {"kiro_crew.snapshot_components", "kiro_crew.snapshot_archive"}
    ),
    "kiro_crew.snapshot_merge": frozenset(
        {"kiro_crew.snapshot_components", "kiro_crew.snapshot_archive"}
    ),
}

_OWNER_MODULES = tuple(m for m in OWNERS if m != "kiro_crew.snapshot")


def _module_path(module: str) -> Path:
    return SRC / f"{module.rsplit('.', 1)[1]}.py"


def _top_level_definitions(path: Path) -> set[str]:
    names: set[str] = set()
    for node in ast.parse(path.read_text(encoding="utf-8")).body:
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
            names.add(node.name)
        elif isinstance(node, (ast.Assign, ast.AnnAssign)):
            targets = node.targets if isinstance(node, ast.Assign) else [node.target]
            names.update(t.id for t in targets if isinstance(t, ast.Name))
    return names


@pytest.mark.parametrize("module", sorted(OWNERS))
def test_each_owner_defines_exactly_its_declared_names(module: str) -> None:
    defined = _top_level_definitions(_module_path(module))
    # A module-level `try` binds the dashboard port where the facade defines it.
    if module == "kiro_crew.snapshot":
        defined.add("_DASHBOARD_PORT")
    assert defined == set(OWNERS[module]), (
        f"{module} defines {sorted(defined - set(OWNERS[module]))} beyond its declared "
        f"names and lacks {sorted(set(OWNERS[module]) - defined)}"
    )


def test_no_name_has_two_owners() -> None:
    seen: dict[str, str] = {}
    for module, names in OWNERS.items():
        for name in names:
            assert name not in seen, f"{name} is declared by both {seen[name]} and {module}"
            seen[name] = module


@pytest.mark.parametrize("module", _OWNER_MODULES)
def test_the_facade_hands_out_the_owners_objects(module: str) -> None:
    owner = _MODULES[module]
    for name in OWNERS[module]:
        assert getattr(snap, name) is getattr(owner, name), f"snapshot.{name} is a copy"
        obj = getattr(owner, name)
        if inspect.isfunction(obj) or inspect.isclass(obj):
            assert obj.__module__ == module, f"{name} reports {obj.__module__}"


def test_every_snapshot_module_is_registered_with_the_family_scans() -> None:
    """A module added to the family and left off the list would escape every source scan."""
    on_disk = {p.name for p in SRC.glob("snapshot*.py")} - {"snapshot_redact.py"}
    assert on_disk == set(SNAPSHOT_FAMILY)
    assert set(SNAPSHOT_FAMILY) == {_module_path(m).name for m in OWNERS}


def _imported_modules(tree: ast.AST) -> set[str]:
    """Every ``kiro_crew`` module a tree imports, at module scope or inside a function."""
    found: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.ImportFrom) and node.module:
            if node.module == "kiro_crew":
                found.update(f"kiro_crew.{a.name}" for a in node.names)
            elif node.module.startswith("kiro_crew."):
                found.add(node.module)
        elif isinstance(node, ast.Import):
            found.update(a.name for a in node.names if a.name.startswith("kiro_crew."))
        elif (
            isinstance(node, ast.Call)
            and isinstance(node.func, ast.Attribute)
            and node.func.attr == "import_module"
            and node.args
            and isinstance(node.args[0], ast.Constant)
        ):
            found.add(str(node.args[0].value))
    return found


@pytest.mark.parametrize("module", _OWNER_MODULES)
def test_owners_depend_downward_only_and_never_on_the_facade(module: str) -> None:
    tree = ast.parse(_module_path(module).read_text(encoding="utf-8"))
    family = {m for m in _imported_modules(tree) if m.startswith("kiro_crew.snapshot")}
    assert "kiro_crew.snapshot" not in family, f"{module} reaches back into the facade"
    assert "kiro_crew.snapshot_redact" not in family, f"{module} imports the redaction pass"
    assert (
        family <= ALLOWED_IMPORTS[module]
    ), f"{module} imports {sorted(family - ALLOWED_IMPORTS[module])}, which sit above it"


def test_only_the_snapshot_family_imports_an_owner_module() -> None:
    """Owners are reached only through ``kiro_crew.snapshot``, so ``_facade()`` finds it loaded.

    ``_facade()`` looks the facade up in ``sys.modules`` instead of importing it. That holds
    only while every production path into an owner runs through the facade, which imports
    them all: a module outside the family that imported an owner directly could reach one
    in a process that never loaded the facade.
    """
    owners = set(_OWNER_MODULES)
    stems = {m.rpartition(".")[2] for m in owners}
    family = {_module_path(m) for m in OWNERS}
    direct: list[str] = []
    for path in sorted(SRC.rglob("*.py")):
        if path in family or "tests" in path.relative_to(SRC).parts:
            continue
        text = path.read_text(encoding="utf-8")
        if not any(stem in text for stem in stems):
            continue
        tree = ast.parse(text)
        imported = _imported_modules(tree) & owners
        imported |= {
            f"kiro_crew.{name}"
            for node in ast.walk(tree)
            if isinstance(node, ast.ImportFrom) and node.level
            for name in [node.module or "", *(a.name for a in node.names)]
            if name in stems
        }
        if imported:
            direct.append(f"{path.relative_to(SRC).as_posix()}: {sorted(imported)}")
    assert direct == [], f"imported without the facade: {direct}"


@pytest.mark.parametrize("module", sorted(OWNERS))
def test_no_module_binds_the_config_class_at_import(module: str) -> None:
    """The redaction switch is read from its own file, never from ``config.json``."""
    assert not hasattr(_MODULES[module], "KiroCrewConfig")


@pytest.mark.parametrize("module", sorted(OWNERS))
def test_every_sqlite_binding_is_the_resolved_driver(module: str) -> None:
    mod = _MODULES[module]
    if hasattr(mod, "sqlite3"):
        assert mod.sqlite3 is _sqlite_compat.sqlite3


def test_the_facade_binds_stdlib_sqlite_past_a_husk_driver(tmp_path: Path) -> None:
    """The one ``sqlite3`` binding is the facade's, so a pruned ``pysqlite3`` husk is harmless.

    The owners reach the driver through the facade when they open a database, as the claim
    for this split asked, so the facade's binding is the only one to check. The same
    fresh-interpreter shape as ``test_sqlite_compat_resolver``: the husk has to be in place
    before the first ``import kiro_crew``.
    """
    sites = [m for m in OWNERS if hasattr(_MODULES[m], "sqlite3")]
    assert sites == ["kiro_crew.snapshot"]
    script = textwrap.dedent("""
        import importlib
        import sqlite3
        import sys
        from types import ModuleType

        sys.modules['pysqlite3'] = ModuleType('pysqlite3')
        for name in {sites!r}:
            module = importlib.import_module(name)
            assert module.sqlite3 is sqlite3, name
            module.sqlite3.connect(':memory:').close()
        """).format(sites=sites)
    env = dict(os.environ, KIROCREW_HOME=str(tmp_path), KIRO_HOME=str(tmp_path / "kiro"))
    env.update(TMPDIR=str(tmp_path), TMP=str(tmp_path), TEMP=str(tmp_path))
    env["PYTHONPATH"] = os.pathsep.join([str(REPO_SRC), env.get("PYTHONPATH", "")]).rstrip(
        os.pathsep
    )
    result = subprocess.run(
        [sys.executable, "-c", script],
        cwd=tmp_path,
        env=env,
        capture_output=True,
        text=True,
        encoding="utf-8",
        timeout=120,
    )
    assert result.returncode == 0, result.stdout + result.stderr


def test_a_helper_patched_on_the_facade_is_the_one_its_owner_calls(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """The seam stays on the facade: patching the facade name reaches the owner's call site.

    ``_do_replace`` reads ``_do_replace_mutations`` through the facade when it calls it, so
    the facade is the one patch target. The owner's own binding is not consulted, which is
    what keeps a single patch sufficient.
    """
    seen: list[str] = []
    home = tmp_path / "home"
    bundle = tmp_path / "bundle"
    home.mkdir()
    bundle.mkdir()

    monkeypatch.setattr(snap, "_do_replace_mutations", lambda *_a, **_k: seen.append("facade"))
    snap._do_replace(bundle, home, ["crons"], allow_unpinned=True)
    assert seen == ["facade"], "the facade patch did not reach the owner's call site"

    monkeypatch.setattr(
        snapshot_restore, "_do_replace_mutations", lambda *_a, **_k: seen.append("owner")
    )
    snap._do_replace(bundle, home, ["crons"], allow_unpinned=True)
    assert seen == ["facade", "facade"], "the owner's own binding was consulted"
