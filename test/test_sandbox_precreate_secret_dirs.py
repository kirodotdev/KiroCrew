"""Every hidden directory on a sandbox mask list exists before the first namespace spawn.

On Linux a mask is a bind mount, and the launcher's ``sensitive_dirs`` loop skips a
target that does not exist. A store that creates its directory on first write would
then publish into every sandbox already running, unmasked.
:func:`sandbox._materialize_maskable_dirs` closes that by pre-creating the lazy ones.
These tests pin the two secret stores (the vault and the Kiro sign-in store), check
that each store still works on an empty pre-made directory, and pin that every leaf on
a mask list is classified, so a new lazy directory cannot join a mask list unmasked.

Every path is under ``tmp_path`` and every secret is a fixture value.
"""

from __future__ import annotations

import ast
import stat
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest
from source_corpus import repo_files_named, repo_root

from kiro_crew import sandbox
from kiro_crew.platform_compat import IS_WINDOWS

_PREFIX = ".kiro/crew/"


def _assert_mode(path: Path, mode: int) -> None:
    # POSIX mode bits only: Windows ignores the mkdir/open mode, and the mask this
    # list feeds belongs to the Linux namespace launcher.
    if not IS_WINDOWS:
        assert stat.S_IMODE(path.stat().st_mode) == mode


@pytest.fixture(autouse=True)
def fresh_home(tmp_path: Path, _floor_monkeypatch: pytest.MonkeyPatch) -> Path:
    """A data home with no stores in it, resolved by every module through ``KIROCREW_HOME``."""
    home = tmp_path / "crew"
    home.mkdir()
    _floor_monkeypatch.setenv("KIROCREW_HOME", str(home))
    _floor_monkeypatch.delenv("KIROCREW_MCP_APPS_SPOOL", raising=False)
    _floor_monkeypatch.setattr(sandbox, "config_dir", lambda: home)
    return home


# ── The two secret stores ─────────────────────────────────────────────────────


@pytest.mark.parametrize("leaf", [".vault", "kas"])
def test_the_secret_store_dir_is_precreated_owner_only(fresh_home: Path, leaf: str) -> None:
    target = fresh_home / leaf
    assert not target.exists()

    created = sandbox._materialize_maskable_dirs()

    assert target.is_dir() and not target.is_symlink()
    _assert_mode(target, 0o700)
    assert str(target) in created


def test_the_vault_saves_and_loads_in_a_premade_dir(fresh_home: Path) -> None:
    from kiro_crew.secrets import SecretVault

    sandbox._materialize_maskable_dirs()
    assert not any((fresh_home / ".vault").iterdir())

    SecretVault(fresh_home).set_sync("FIXTURE_NAME", "fixture-value-not-a-secret")

    assert SecretVault(fresh_home).get("FIXTURE_NAME").reveal() == "fixture-value-not-a-secret"
    key = fresh_home / ".vault" / ".vault_key"
    _assert_mode(key, 0o600)


def test_the_sign_in_store_saves_and_loads_in_a_premade_dir(fresh_home: Path) -> None:
    from kiro_crew.auth.store import KasToken, TokenStore

    sandbox._materialize_maskable_dirs()
    assert not any((fresh_home / "kas").iterdir())
    store = TokenStore(fresh_home)
    assert store.load("builder_id") is None

    store.save(
        KasToken(
            access_token="fixture-access-not-a-secret",
            expires_at=datetime.now(timezone.utc) + timedelta(hours=1),
            provider="BuilderId",
            identity="builder_id",
            refresh_token="fixture-refresh-not-a-secret",
            client_id="fixture-client",
            client_secret="fixture-client-secret",
        )
    )

    loaded = TokenStore(fresh_home).load("builder_id")
    assert loaded is not None
    assert loaded.access_token == "fixture-access-not-a-secret"
    _assert_mode(fresh_home / "kas", 0o700)


# ── The other pre-created stores read an empty root as an absent one ──────────


def test_cron_running_reads_an_empty_root_as_no_markers(fresh_home: Path) -> None:
    from kiro_crew import cron_inflight

    sandbox._materialize_maskable_dirs()

    assert cron_inflight.read_markers(fresh_home) == []
    assert cron_inflight.read_claim(fresh_home) == ""
    cron_inflight.write_marker(fresh_home, "job-1", "fixture job", run="r1")
    assert [m.job_id for m in cron_inflight.read_markers(fresh_home)] == ["job-1"]


def test_the_workflow_library_reads_an_empty_root_as_no_definitions(fresh_home: Path) -> None:
    from kiro_crew.workflows.library import WorkflowDefinitionLibrary

    sandbox._materialize_maskable_dirs()

    # No saved definitions; the package's read-only built-ins are not stored there.
    assert [d for d in WorkflowDefinitionLibrary().list() if not d.get("builtin")] == []


def test_the_mcp_apps_spool_accepts_an_empty_root(fresh_home: Path) -> None:
    from kiro_crew.mcp_gateway import apps

    sandbox._materialize_maskable_dirs()

    assert apps.spool_dir() == fresh_home / "mcp-apps"
    assert apps.sweep_spool() == 0
    _assert_mode(fresh_home / "mcp-apps", 0o700)


def test_an_empty_backup_dir_leaves_outbound_redaction_off(fresh_home: Path) -> None:
    from kiro_crew import snapshot_redact

    sandbox._materialize_maskable_dirs()

    assert (fresh_home / "backup").is_dir()
    assert snapshot_redact.outbound_redaction_enabled() is False


# ── The sweep invariant ───────────────────────────────────────────────────────

#: Directory leaves the gateway creates while it starts, before it can spawn a sandbox.
_MADE_AT_BOOT: dict[str, str] = {
    "diag": "the diagnostic recorder starts with the gateway and mkdirs it",
    "tasks": "the subagent manager opens the task store at start",
    "scratch": "every spawn allocates its own scratch dir under it first",
    "cron-history": "CronService.create() prepares the history dir at gateway start",
}

#: Directory leaves nothing in the tree writes, so there is no first write to race.
_NO_WRITER: dict[str, str] = {
    "ledgers": "retired root; precreating it would re-materialise the old name",
    "agentcore-inbound": "no producer left in the tree",
}

#: Directory leaves deliberately left out of the pre-create list for now. ``policy_cache``
#: is pre-created: an empty one is no cache to every reader, and the gateway publishes
#: into it (``test_policy_cache_sandbox_view.py``).
_HELD_OUT: dict[str, str] = {}

#: File leaves nothing in the tree reads or writes, so an absent one needs no mask target:
#: a sandbox that creates it feeds nothing. Kept true by
#: :func:`test_no_code_names_a_no_writer_file_leaf`.
_NO_WRITER_FILES: dict[str, str] = {
    "browser-cookies.txt": "retired browser cookie jar",
    "playwright-storage-state.json": "retired browser storage state",
    "playwright-extension-token": "retired browser extension token",
    "browser-mode-enabled": "retired Browser Mode switch",
    "browser-engine": "retired Browser Mode engine choice",
    ".kiro_cli_binary_trust.json": "retired installer-provenance record",
}

#: Leaves that are FILES: the ``isfile``-guarded loop and its own materialisers own them.
_FILE_LEAVES: frozenset[str] = frozenset(
    {
        ".env",
        "ops_mission_control_policy.json",
        "ops_mission_control_secrets.json",
        "refresh_chains.json",
        "token_signing.key",
        sandbox._LIVE_TARGET_LEAF,
        sandbox.AUTH_SQLITE_DB,
        *(f"{sandbox.AUTH_SQLITE_DB}{suffix}" for suffix in sandbox.AUTH_SQLITE_SIDECAR_SUFFIXES),
    }
)


def _masked_crew_leaves() -> set[str]:
    return {
        entry[len(_PREFIX) :]
        for entry in (*sandbox._STRICT_DIRS, *sandbox._STANDARD_DIRS, *sandbox._CC_DIRS)
        if entry.startswith(_PREFIX)
    }


def test_every_masked_directory_is_precreated_or_made_at_boot() -> None:
    precreated = set(sandbox._CREW_PRECREATE_HIDDEN_DIR_LEAVES)
    classified = (
        precreated
        | set(_MADE_AT_BOOT)
        | set(_NO_WRITER)
        | set(_HELD_OUT)
        | set(_NO_WRITER_FILES)
        | _FILE_LEAVES
    )
    # A nested leaf has an agent-writable ancestor, so a plain mkdir under the data home
    # is unsound for it; those are out of this list's reach by construction.
    unclassified = sorted(
        leaf for leaf in _masked_crew_leaves() if leaf not in classified and "/" not in leaf
    )
    assert not unclassified, (
        f"{unclassified} are on a sandbox mask list but are neither pre-created "
        "(_CREW_PRECREATE_HIDDEN_DIR_LEAVES) nor made at gateway start; a store that "
        "creates one on first write publishes it unmasked into every running sandbox"
    )


def test_the_classification_names_only_masked_leaves() -> None:
    masked = _masked_crew_leaves()
    sets = {
        "precreated": set(sandbox._CREW_PRECREATE_HIDDEN_DIR_LEAVES),
        "made_at_boot": set(_MADE_AT_BOOT),
        "no_writer": set(_NO_WRITER),
        "held_out": set(_HELD_OUT),
        "no_writer_files": set(_NO_WRITER_FILES),
        "files": set(_FILE_LEAVES),
    }
    for name, leaves in sets.items():
        assert leaves <= masked, f"{name} lists leaves no mask list carries: {leaves - masked}"
    names = list(sets)
    for i, a in enumerate(names):
        for b in names[i + 1 :]:
            assert not sets[a] & sets[b], f"{a} and {b} overlap: {sets[a] & sets[b]}"


#: The tables that name masked leaves without reading or writing them.
_LEAF_TABLE_MODULES = frozenset(
    {
        "src/kiro_crew/sandbox_leaves.py",
        "src/kiro_crew/sandbox_aliases.py",
        "src/kiro_crew/security/paths_lists.py",
    }
)


def test_no_code_names_a_no_writer_file_leaf() -> None:
    names = set(_NO_WRITER_FILES)
    root = repo_root()
    hits: list[str] = []
    for path in repo_files_named(".py"):
        parts = path.relative_to(root).parts
        rel = "/".join(parts)
        if parts[0] not in {"src", "scripts", "packages"} or rel in _LEAF_TABLE_MODULES:
            continue
        if "tests" in parts or "_vendor" in parts:
            continue
        text = path.read_text(encoding="utf-8")
        if not any(name in text for name in names):
            continue
        for node in ast.walk(ast.parse(text)):
            if (
                isinstance(node, ast.Constant)
                and isinstance(node.value, str)
                and node.value in names
            ):
                hits.append(f"{rel}:{node.lineno}: {node.value}")
    assert not hits, (
        "these names are in _NO_WRITER_FILES, so the Linux mask leaves an absent one "
        "unbound; code that reads or writes one must give it a mask target before every "
        "namespace spawn and move it out of that table:\n  " + "\n  ".join(hits)
    )
