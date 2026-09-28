"""Contracts the snapshot family must keep whatever module each rule lives in.

``kiro_crew.snapshot`` is the command and API facade; component resolution, archive
format and staging, restore transactions and merge algorithms live in owner modules
behind it. These tests pin what a caller of the facade can observe -- the names it
exports, the CLI surface, the bundle a snapshot writes, and what importing it loads --
so moving code between those owners cannot change any of it unnoticed.
"""

from __future__ import annotations

import argparse
import json
import os
import shutil
import subprocess
import sys
import tarfile
import textwrap
from pathlib import Path
from unittest import mock

import pytest
from test_snapshot import _setup_fake_kirocrew, unpinnable_argv

from kiro_crew import pinned_fs
from kiro_crew import snapshot as snap

REPO_SRC = Path(__file__).resolve().parents[1] / "src"

#: Every attribute a caller may import or patch through ``kiro_crew.snapshot``; portability,
#: the dashboard handlers, the AWS Control backup, the CLI and the tests reach these by this
#: path. Derived by AST from ``src/kiro_crew/snapshot.py`` at 20ed6a1dc, the facade the
#: owner modules were split from, and not chosen by hand: every module-level ``def``,
#: ``class`` and assignment (the ``try``-bound ``_DASHBOARD_PORT`` included) plus every
#: name its module-level ``from ... import`` statements bind. Left out are the
#: ``__future__`` directive and the ``TYPE_CHECKING``-only import, neither of which is bound
#: at run time, and the names an ``import <stdlib module>`` binds; the three of those that
#: tests patch through the facade are pinned apart below.
FACADE_NAMES = frozenset(
    {
        "Any",
        "COMPONENTS",
        "COMPONENT_HELP",
        "COMPONENT_JSON_OBJECTS",
        "COMPONENT_TREES",
        "CORE_FILES",
        "CORE_FILES_FLAT",
        "Callable",
        "ComponentRefused",
        "ComponentSpec",
        "DB_COPIED",
        "DB_NOT_A_DATABASE",
        "DB_UNSAFE_SOURCE",
        "DatabaseCopyFailed",
        "EXPORT_MANIFEST_VERSION",
        "Enum",
        "ExitStack",
        "MANIFEST_VERSION",
        "MEMBER_BACKUPS_DIR_NAME",
        "MEMORY_STORES_DIR_NAME",
        "ManifestUnreadable",
        "NEVER_SNAPSHOT_FILES",
        "NamedStoresInUse",
        "NotificationCopyUnsupported",
        "PRODUCT_TREE_DATABASES",
        "Path",
        "PurePosixPath",
        "PureWindowsPath",
        "Purpose",
        "RECORD_CAP",
        "RedactionFailed",
        "RollbackIncomplete",
        "SECURITY_SENSITIVE_FILES",
        "SKIP_DB_UNPINNED_SOURCE",
        "SecretPolicy",
        "SourceComponentUnsound",
        "StoresInUse",
        "TYPE_CHECKING",
        "UndecodableRecord",
        "UnreadableRecord",
        "UnsafeComponentRoot",
        "VALID_COMPONENTS",
        "_ArchiveTooLarge",
        "_CONTROL_CHARS",
        "_CORE_FILE_COMPONENTS",
        "_DASHBOARD_PORT",
        "_DB_SIDECAR_GLOBS",
        "_DB_SUFFIXES",
        "_DERIVED_INDEXES",
        "_FIRST_EXPORT_VERSION_WITH_NAMED_STORES",
        "_FIRST_VERSION_WITH_NAMED_STORES",
        "_HOST_LOCAL_PATHS",
        "_JSON_OBJECT_LISTS",
        "_LOCKED_DOCUMENT_TREES",
        "_MAX_ARCHIVE_BYTES",
        "_MAX_ARCHIVE_MEMBERS",
        "_MERGE_ALLOWED_TABLES",
        "_NOTIFICATION_RECORD_CAP",
        "_NOTIFICATION_SOURCE_CAP",
        "_REPLACE_ONLY_COMPONENTS",
        "_SAFE_IDENTIFIER_RE",
        "_TELEMETRY_SALT_BYTES",
        "_TERMINATORS",
        "_TREE_DOCUMENT_VALIDATORS",
        "_WHOLE_TREE_COMPONENTS",
        "_allocate_rollback_dir",
        "_audit",
        "_backup_and_copy",
        "_backup_tree_or_refuse",
        "_build_snapshot",
        "_bundle_carries_named_stores",
        "_chain_is_link_free",
        "_clear_store_directories",
        "_component_payload_absent",
        "_components_absent_from_bundle",
        "_copy_database_consistently",
        "_copy_locked",
        "_copy_notifications",
        "_copy_tree_no_overwrite",
        "_copytree_safe",
        "_data_filter",
        "_default_snapshot_dir",
        "_dir_flags_nofollow",
        "_do_merge",
        "_do_replace",
        "_do_replace_mutations",
        "_drop_derived_indexes_absent_from_bundle",
        "_escape_one",
        "_estimate_selected_bytes",
        "_fsize",
        "_install_locked_document",
        "_install_notifications",
        "_is_gateway_running",
        "_is_host_local",
        "_list_components",
        "_lock_down_restored",
        "_manifest_components",
        "_mc_dir",
        "_merge_crons",
        "_merge_memory",
        "_merge_named_stores",
        "_merge_notifications",
        "_never_ships",
        "_notification_key",
        "_print_manifest",
        "_redacted_upload_copy",
        "_redactor",
        "_refuse_corrupt_source_databases",
        "_refuse_oversized_archive",
        "_refuse_unless_json_object",
        "_refuse_unless_sound",
        "_refuse_unless_valid_tree_document",
        "_refuse_unsafe_destination_roots",
        "_refuse_unsound_required_capture",
        "_rejection_recording_filter",
        "_remove_locked_document",
        "_report_redacted_bundle",
        "_report_redaction",
        "_report_skip",
        "_report_unmerged_databases",
        "_report_unredacted_upload",
        "_report_unresolved_payload",
        "_restage_databases",
        "_restore_everything_from_rollback",
        "_restore_locked_document",
        "_safe_name",
        "_save_locked_document_to",
        "_serialise_with_notification_writes",
        "_staging_ignore",
        "_staging_is_pinned",
        "_terminal_safe",
        "_tree_roots_replace_clears",
        "_trees_absent_from_bundle",
        "_usable_cron_shape",
        "_validate_identifier",
        "_want",
        "closing",
        "dataclass",
        "datetime",
        "hold_stores_for_read",
        "hold_stores_for_replace",
        "is_host_local_store_state",
        "is_product_tree_database",
        "memory_store_namespace_lock",
        "named_store_product_file",
        "pinned_fs",
        "platform_compat",
        "prepare_redacted_copy",
        "resolve_components",
        "restore_main",
        "safe_tree_root",
        "snapshot_main",
        "sqlite3",
        "strict_raw_records",
        "timezone",
    }
)


def test_the_facade_still_answers_every_name_it_exported() -> None:
    missing = sorted(n for n in FACADE_NAMES if not hasattr(snap, n))
    assert missing == [], f"kiro_crew.snapshot no longer exports: {missing}"


def test_the_facade_binds_the_real_stdlib_modules_tests_patch_through() -> None:
    """``snapshot.os.rename`` and ``snapshot.shutil.rmtree`` are patched by this path."""
    assert snap.os is os
    assert snap.shutil is shutil
    assert snap.tarfile is tarfile


def _parser_rows(main) -> list[tuple]:
    """The argument table *main* builds, captured at ``parse_args`` and never run."""
    captured: list[tuple] = []

    def _grab(self, args=None, namespace=None):
        captured.extend(
            (
                tuple(a.option_strings),
                a.dest,
                a.nargs,
                a.const,
                # The positional default is the operator's configured snapshot dir.
                "<configured>" if a.dest == "output_dir" else a.default,
                a.choices,
                a.help,
                type(a).__name__,
            )
            for a in self._actions
        )
        raise SystemExit(0)

    with mock.patch.object(argparse.ArgumentParser, "parse_args", _grab):
        with pytest.raises(SystemExit):
            main([])
    return captured


_HELP = "show this help message and exit"


def test_the_snapshot_command_line_is_unchanged() -> None:
    assert _parser_rows(snap.snapshot_main) == [
        (("-h", "--help"), "help", 0, None, argparse.SUPPRESS, None, _HELP, "_HelpAction"),
        ((), "output_dir", "?", None, "<configured>", None, None, "_StoreAction"),
        (("--keep",), "keep", None, None, 7, None, None, "_StoreAction"),
        (("--list",), "list_snapshots", 0, True, False, None, None, "_StoreTrueAction"),
        (
            ("--allow-unpinned-staging",),
            "allow_unpinned",
            0,
            True,
            False,
            None,
            "Stage by path name on a platform that cannot open a directory relative to a "
            "descriptor. Without this the snapshot is refused there rather than taken with a "
            "traversal an ancestor swap could redirect. The archive's MANIFEST.json records "
            "that it was staged unpinned.",
            "_StoreTrueAction",
        ),
        (("--components",), "components", None, None, None, None, None, "_StoreAction"),
        (("--purpose",), "purpose", None, None, "backup", None, None, "_StoreAction"),
        (("--to",), "to", None, None, None, None, argparse.SUPPRESS, "_StoreAction"),
    ]


def test_the_restore_command_line_is_unchanged() -> None:
    assert _parser_rows(snap.restore_main) == [
        (("-h", "--help"), "help", 0, None, argparse.SUPPRESS, None, _HELP, "_HelpAction"),
        ((), "snapshot", "?", None, None, None, None, "_StoreAction"),
        (("--mode",), "mode", None, None, None, ("replace", "merge"), None, "_StoreAction"),
        (("--dry-run",), "dry_run", 0, True, False, None, None, "_StoreTrueAction"),
        (
            ("--force",),
            "force",
            0,
            True,
            False,
            None,
            "Allow restore even if gateway is running",
            "_StoreTrueAction",
        ),
        (("--components",), "components", None, None, None, None, None, "_StoreAction"),
        (("--list-components",), "list_components", 0, True, False, None, None, "_StoreTrueAction"),
        (
            ("--allow-unpinned-staging",),
            "allow_unpinned",
            0,
            True,
            False,
            None,
            "Restore by path name on a platform that cannot open a directory relative to a "
            "descriptor. Without this the restore is refused there rather than run with a "
            "destination an ancestor swap could redirect.",
            "_StoreTrueAction",
        ),
    ]


_MANIFEST_KEYS = [
    "version",
    "created_at",
    "hostname",
    "user",
    "kirocrew_dir",
    "purpose",
    "components",
    "staging",
    "skipped",
    "contents",
]

_CONTENTS_KEYS = [
    "memory_db",
    "memory_index_db",
    "crons_json",
    "config_json",
    "notifications_jsonl",
    "workspace_files",
    "plan_memory_files",
    "skill_count",
    "memory_store_count",
]

# (bundle-relative name, tar type, mode) for every member, in archive order. Owner, group
# and their names are zeroed for every member by the extraction filter, asserted apart.
_COMPLETE_MEMBERS = [
    (".", tarfile.DIRTYPE, 0o755),
    ("MANIFEST.json", tarfile.REGTYPE, 0o644),
    ("config.json", tarfile.REGTYPE, 0o644),
    ("crons.json", tarfile.REGTYPE, 0o644),
    ("hooks.json", tarfile.REGTYPE, 0o644),
    ("memory.db", tarfile.REGTYPE, 0o644),
    ("notifications.jsonl", tarfile.REGTYPE, 0o644),
    ("plan_memory", tarfile.DIRTYPE, 0o755),
    ("plan_memory/plan1.json", tarfile.REGTYPE, 0o644),
    ("project_dir", tarfile.REGTYPE, 0o644),
    ("session_map.json", tarfile.REGTYPE, 0o644),
    ("skills", tarfile.DIRTYPE, 0o755),
    ("skills/my-skill", tarfile.DIRTYPE, 0o755),
    ("skills/my-skill/SKILL.md", tarfile.REGTYPE, 0o644),
    ("telemetry_salt", tarfile.REGTYPE, 0o600),
    ("workspace", tarfile.DIRTYPE, 0o755),
    ("workspace/doc.md", tarfile.REGTYPE, 0o644),
    ("workspace/knowledge", tarfile.DIRTYPE, 0o755),
    ("workspace/knowledge/kb.sqlite3", tarfile.REGTYPE, 0o644),
    ("workspace/memory", tarfile.DIRTYPE, 0o755),
    ("workspace/memory/history", tarfile.DIRTYPE, 0o755),
    ("workspace/memory/history/2026-01-01.md", tarfile.REGTYPE, 0o644),
    ("workspace/memory/preferences.md", tarfile.REGTYPE, 0o644),
    ("workspace/memory/projects.md", tarfile.REGTYPE, 0o644),
    ("workspace_dir", tarfile.REGTYPE, 0o644),
]

_PARTIAL_MEMBERS = [
    (".", tarfile.DIRTYPE, 0o755),
    ("MANIFEST.json", tarfile.REGTYPE, 0o644),
    ("crons.json", tarfile.REGTYPE, 0o644),
    ("memory.db", tarfile.REGTYPE, 0o644),
    ("workspace", tarfile.DIRTYPE, 0o755),
    ("workspace/knowledge", tarfile.DIRTYPE, 0o755),
    ("workspace/knowledge/kb.sqlite3", tarfile.REGTYPE, 0o644),
    ("workspace/memory", tarfile.DIRTYPE, 0o755),
    ("workspace/memory/history", tarfile.DIRTYPE, 0o755),
    ("workspace/memory/history/2026-01-01.md", tarfile.REGTYPE, 0o644),
    ("workspace/memory/preferences.md", tarfile.REGTYPE, 0o644),
    ("workspace/memory/projects.md", tarfile.REGTYPE, 0o644),
]


@pytest.fixture
def home(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    d = tmp_path / "home"
    d.mkdir()
    monkeypatch.setenv("KIROCREW_HOME", str(d))
    monkeypatch.setenv("KIROCREW_ASSUME_GATEWAY_RUNNING", "0")
    _setup_fake_kirocrew(d)
    return d


@pytest.mark.parametrize(
    ("extra", "root_prefix", "components", "members"),
    [
        ([], "kirocrew-snapshot-", list(snap.COMPONENTS), _COMPLETE_MEMBERS),
        (
            ["--components", "memory,crons"],
            "kirocrew-partial-",
            ["memory", "crons"],
            _PARTIAL_MEMBERS,
        ),
    ],
    ids=["complete", "partial"],
)
def test_the_bundle_a_snapshot_writes_is_unchanged(
    home: Path, tmp_path: Path, extra, root_prefix, components, members
) -> None:
    """Layout, member modes and ownership, manifest shape, and file bytes, end to end."""
    out = tmp_path / "out"
    assert snap.snapshot_main([str(out), *extra, *unpinnable_argv()]) == 0
    (archive,) = out.glob("kirocrew-snapshot-*.tar.gz")

    with tarfile.open(archive) as tf:
        infos = tf.getmembers()
        root = infos[0].name
        assert root.startswith(root_prefix)
        rows = [(m.name[len(root) :].lstrip("/") or ".", m.type, m.mode) for m in infos]
        assert rows == members
        assert {(m.uid, m.gid, m.uname, m.gname) for m in infos} == {(0, 0, "", "")}
        for m in infos:
            rel = m.name[len(root) :].lstrip("/")
            if not m.isfile() or rel == "MANIFEST.json" or rel.endswith(".db"):
                continue
            # A database is re-copied through the backup API, so only its presence is
            # the archive's promise; every other staged file is its live bytes.
            assert tf.extractfile(m).read() == (home / rel).read_bytes(), rel
        manifest = json.loads(tf.extractfile(f"{root}/MANIFEST.json").read())

    assert list(manifest) == _MANIFEST_KEYS
    assert list(manifest["contents"]) == _CONTENTS_KEYS
    assert manifest["version"] == 4
    assert manifest["purpose"] == "backup"
    assert manifest["components"] == {c: "unresolved" for c in components}
    assert list(manifest["components"]) == components
    assert manifest["staging"] == (
        "pinned" if pinned_fs.supports_pinned_tree_walk() else "unpinned"
    )
    assert manifest["skipped"] == []
    assert manifest["kirocrew_dir"] == str(home)


def test_importing_the_facade_keeps_the_lazy_owners_unloaded(tmp_path: Path) -> None:
    """``kiro_crew.snapshot`` is on the gateway's boot path; what it defers must stay deferred.

    The outbound redaction pass is imported only by a command that prepares an outbound
    copy, and the dashboard's notification worker only by a restore that writes
    notifications. Moving code behind the facade must not turn either into an import-time
    dependency. A fresh interpreter, because this test process has imported everything.
    """
    script = textwrap.dedent("""
        import sys
        import kiro_crew.snapshot
        deferred = ("kiro_crew.snapshot_redact", "kiro_crew.dashboard.state")
        loaded = [name for name in deferred if name in sys.modules]
        assert loaded == [], loaded
        """)
    env = dict(os.environ, KIROCREW_HOME=str(tmp_path / "home"), KIRO_HOME=str(tmp_path / "kiro"))
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
