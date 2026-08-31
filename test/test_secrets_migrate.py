"""Tests for kiro_crew.secrets.migrate — plaintext .env → vault importer."""

from __future__ import annotations

import os
import re
import stat
import sys
from pathlib import Path
from unittest.mock import patch

import pytest

import kiro_crew
from kiro_crew import cli
from kiro_crew.secrets import SecretVault
from kiro_crew.secrets.migrate import (
    MigrationConflictError,
    MigrationReport,
    format_report,
    migrate_env_secrets,
)


def _migrate(config_dir: Path, ep: Path, **kwargs):
    """Run the importer against a specific ``.env`` in tests.

    ``migrate_env_secrets`` intentionally takes no caller path for EITHER the
    ``.env`` it reads (``env_path()``) or the vault home it writes
    (``config_dir()``), so tests point it at their temp home by patching both
    resolvers rather than passing paths.
    """
    with (
        patch("kiro_crew.secrets.migrate.env_path", return_value=ep),
        patch("kiro_crew.secrets.migrate.config_dir", return_value=config_dir),
    ):
        return migrate_env_secrets(**kwargs)


# A migratable Jira token + a non-Jira credential (must NOT migrate) + an
# unrecognized operator setting.
_ENV_BODY = (
    "# comment line\n"
    "JIRA_API_TOKEN=plaintext-token\n"
    "SLACK_BOT_TOKEN=xoxb-123\n"
    "HTTP_PROXY=http://proxy.local:8080\n"
    "\n"
)


def _write_env(tmp_path: Path, body: str = _ENV_BODY) -> Path:
    ep = tmp_path / ".env"
    ep.write_text(body)
    return ep


def test_dry_run_reports_and_writes_nothing(tmp_path: Path) -> None:
    """Default dry run identifies the Jira token but changes nothing."""
    config_dir = tmp_path / "config"
    ep = _write_env(tmp_path)
    original = ep.read_text()

    report = _migrate(config_dir, ep, dry_run=True)

    assert report.dry_run is True
    assert report.migrated == ["JIRA_API_TOKEN"]
    # .env untouched, vault store never created.
    assert ep.read_text() == original
    assert SecretVault(config_dir).list_names() == []


def test_apply_writes_vault_and_rewrites_env(tmp_path: Path) -> None:
    """--apply stores the Jira token and rewrites only its .env line."""
    config_dir = tmp_path / "config"
    ep = _write_env(tmp_path)

    report = _migrate(config_dir, ep, dry_run=False)

    assert report.dry_run is False
    assert report.migrated == ["JIRA_API_TOKEN"]

    vault = SecretVault(config_dir)
    assert vault.list_names() == ["JIRA_API_TOKEN"]
    assert vault.get("JIRA_API_TOKEN").reveal() == "plaintext-token"

    text = ep.read_text()
    assert "JIRA_API_TOKEN=secret://JIRA_API_TOKEN" in text
    # The Jira plaintext is gone; the non-Jira credential and unrecognized key
    # are preserved verbatim (their consumers are not vault-aware yet).
    assert "plaintext-token" not in text
    assert "SLACK_BOT_TOKEN=xoxb-123" in text
    assert "HTTP_PROXY=http://proxy.local:8080" in text
    assert "# comment line" in text


def test_non_jira_credential_not_migrated(tmp_path: Path) -> None:
    """A non-Jira credential key is left untouched (consumer not vault-aware)."""
    config_dir = tmp_path / "config"
    ep = _write_env(tmp_path, "SLACK_BOT_TOKEN=xoxb-123\nKIRO_API_KEY=k-abc\n")

    report = _migrate(config_dir, ep, dry_run=False)

    assert report.migrated == []
    assert SecretVault(config_dir).list_names() == []
    text = ep.read_text()
    assert "SLACK_BOT_TOKEN=xoxb-123" in text
    assert "KIRO_API_KEY=k-abc" in text
    assert "secret://" not in text


def test_source_file_not_deleted(tmp_path: Path) -> None:
    """The .env file is rewritten in place, never removed."""
    config_dir = tmp_path / "config"
    ep = _write_env(tmp_path)

    _migrate(config_dir, ep, dry_run=False)

    assert ep.exists()


def test_env_written_at_mode_600(tmp_path: Path) -> None:
    """After apply the .env is owner-only (0o600)."""
    if os.name != "posix":
        return  # POSIX-only permission bits.
    config_dir = tmp_path / "config"
    ep = _write_env(tmp_path)

    _migrate(config_dir, ep, dry_run=False)

    assert stat.S_IMODE(ep.stat().st_mode) == 0o600


def test_apply_is_idempotent(tmp_path: Path) -> None:
    """Re-running apply after a migration is a no-op."""
    config_dir = tmp_path / "config"
    ep = _write_env(tmp_path)

    _migrate(config_dir, ep, dry_run=False)
    after_first = ep.read_text()

    report2 = _migrate(config_dir, ep, dry_run=False)

    assert report2.migrated == []
    assert report2.already_referenced == ["JIRA_API_TOKEN"]
    assert ep.read_text() == after_first


def test_stale_plaintext_behind_secret_ref_not_queued(tmp_path: Path) -> None:
    """A stale plaintext line followed by a secret:// line for the same key
    must NOT be re-migrated: the later reference wins, so the good vault entry
    is never overwritten with the stale value."""
    config_dir = tmp_path / "config"
    # Pre-seed the vault with the authoritative secret, as a prior migration
    # would have left it.
    from kiro_crew.secrets.migrate import migrate_env_secrets as _m  # noqa: F401

    good_env = tmp_path / "good.env"
    good_env.write_text("JIRA_API_TOKEN=good-secret\n")
    _migrate(config_dir, good_env, dry_run=False)
    assert SecretVault(config_dir).get("JIRA_API_TOKEN").reveal() == "good-secret"

    # Now a .env where a STALE plaintext line precedes the authoritative
    # secret:// reference for the same key.
    ep = _write_env(
        tmp_path,
        "JIRA_API_TOKEN=stale-plaintext\nJIRA_API_TOKEN=secret://JIRA_API_TOKEN\n",
    )
    before = ep.read_text()

    report = _migrate(config_dir, ep, dry_run=False)

    # Nothing migrated; the stale plaintext never entered the queue.
    assert report.migrated == []
    assert report.already_referenced == ["JIRA_API_TOKEN"]
    # The good vault entry is intact — NOT overwritten by "stale-plaintext".
    assert SecretVault(config_dir).get("JIRA_API_TOKEN").reveal() == "good-secret"
    # .env untouched (nothing to rewrite).
    assert ep.read_text() == before


def test_per_host_jira_token_migrated(tmp_path: Path) -> None:
    """A JIRA_TOKEN_<HEX> per-host key is recognized and migrated."""
    config_dir = tmp_path / "config"
    host_key = "acme.atlassian.net".encode().hex().upper()
    ep = _write_env(tmp_path, f"JIRA_TOKEN_{host_key}=per-host-plain\n")

    report = _migrate(config_dir, ep, dry_run=False)

    assert report.migrated == [f"JIRA_TOKEN_{host_key}"]
    assert SecretVault(config_dir).get(f"JIRA_TOKEN_{host_key}").reveal() == "per-host-plain"
    assert f"JIRA_TOKEN_{host_key}=secret://JIRA_TOKEN_{host_key}" in ep.read_text()


def test_unrecognized_keys_left_untouched(tmp_path: Path) -> None:
    """A file with only unrecognized keys migrates nothing."""
    config_dir = tmp_path / "config"
    ep = _write_env(tmp_path, "HTTP_PROXY=http://p:1\nMY_FLAG=on\n")

    report = _migrate(config_dir, ep, dry_run=True)

    assert report.migrated == []
    assert SecretVault(config_dir).list_names() == []


def test_missing_env_file_is_noop(tmp_path: Path) -> None:
    """A non-existent .env yields an empty report and no vault store."""
    config_dir = tmp_path / "config"
    ep = tmp_path / "does-not-exist.env"

    report = _migrate(config_dir, ep, dry_run=False)

    assert report.migrated == []
    assert report.already_referenced == []
    assert SecretVault(config_dir).list_names() == []


def test_format_report_dry_run_mentions_apply(tmp_path: Path) -> None:
    """The dry-run report tells the user to re-run with --apply."""
    report = MigrationReport(env_file=tmp_path / ".env", dry_run=True, migrated=["JIRA_API_TOKEN"])
    out = format_report(report)
    assert "--apply" in out
    assert "secret://JIRA_API_TOKEN" in out


def test_format_report_apply_explains_removal(tmp_path: Path) -> None:
    """The apply report explains the plaintext was rewritten in place."""
    report = MigrationReport(env_file=tmp_path / ".env", dry_run=False, migrated=["JIRA_API_TOKEN"])
    out = format_report(report)
    assert "Migrated 1 secret" in out
    assert "REWRITTEN" in out


def test_env_override_key_is_skipped(tmp_path: Path, monkeypatch) -> None:
    """A key with a nonempty process-environment override is NOT migrated:
    the env value is runtime-authoritative, so migrating the stale .env value
    would let vault-first resolution shadow the live override."""
    config_dir = tmp_path / "config"
    ep = _write_env(tmp_path, "JIRA_API_TOKEN=stale-file-value\n")
    monkeypatch.setenv("JIRA_API_TOKEN", "live-env-value")
    before = ep.read_text()

    report = _migrate(config_dir, ep, dry_run=False)

    assert report.migrated == []
    assert SecretVault(config_dir).list_names() == []
    # .env line left untouched (still plaintext, no secret:// rewrite).
    assert ep.read_text() == before


def test_env_override_empty_does_not_skip(tmp_path: Path, monkeypatch) -> None:
    """An EMPTY env override is not authoritative, so the .env value migrates."""
    config_dir = tmp_path / "config"
    ep = _write_env(tmp_path, "JIRA_API_TOKEN=file-value\n")
    monkeypatch.setenv("JIRA_API_TOKEN", "")

    report = _migrate(config_dir, ep, dry_run=False)

    assert report.migrated == ["JIRA_API_TOKEN"]
    assert SecretVault(config_dir).get("JIRA_API_TOKEN").reveal() == "file-value"


def test_per_host_env_var_does_not_skip_migration(tmp_path: Path, monkeypatch) -> None:
    """`load_credentials` overlays the env only for _CREDENTIAL_KEYS (the global
    JIRA_API_TOKEN), NOT per-host JIRA_TOKEN_<HEX> keys. So a same-named env var
    for a per-host key is NOT runtime-authoritative — Jira still reads the
    .env/vault value — and migration must NOT skip it (skipping would strand the
    stale file token). The env-override skip applies only to the global token."""
    config_dir = tmp_path / "config"
    host_key = "acme.atlassian.net".encode().hex().upper()
    ep = _write_env(tmp_path, f"JIRA_TOKEN_{host_key}=per-host-plain\n")
    # An env var of the same name exists, but it does NOT override per-host keys.
    monkeypatch.setenv(f"JIRA_TOKEN_{host_key}", "irrelevant-env-value")

    report = _migrate(config_dir, ep, dry_run=False)

    assert report.migrated == [f"JIRA_TOKEN_{host_key}"]
    assert SecretVault(config_dir).get(f"JIRA_TOKEN_{host_key}").reveal() == "per-host-plain"
    assert f"JIRA_TOKEN_{host_key}=secret://JIRA_TOKEN_{host_key}" in ep.read_text()


def test_concurrent_env_write_aborts_without_clobber(tmp_path: Path, monkeypatch) -> None:
    """If .env changes under a concurrent writer between the snapshot read and
    the rewrite, the rewrite aborts (MigrationConflictError) and the concurrent
    writer's update is preserved — not clobbered by the stale snapshot.

    Crucially, because the vault write now runs INSIDE the lock+CAS critical
    section (after the CAS confirms .env is unchanged), a concurrent .env edit
    that arrives before the CAS check means the vault write NEVER ran — the
    vault holds no entry for the key after the abort.
    """
    config_dir = tmp_path / "config"
    ep = _write_env(tmp_path, "JIRA_API_TOKEN=original\n")

    # Simulate a concurrent writer: BEFORE the CAS re-read fires (which now
    # also precedes the vault write), mutate the file so it differs.
    real_read_bytes = Path.read_bytes
    state = {"calls": 0}

    def _racing_read_bytes(self):
        # First call = the snapshot read; let it through. Before the second
        # call (the CAS re-read) fires, mutate the file so it differs.
        if self == ep:
            state["calls"] += 1
            if state["calls"] == 2:
                ep.write_text("JIRA_API_TOKEN=written-by-other-writer\n")
        return real_read_bytes(self)

    monkeypatch.setattr(Path, "read_bytes", _racing_read_bytes)

    with pytest.raises(MigrationConflictError):
        _migrate(config_dir, ep, dry_run=False)

    # The concurrent writer's value survived; our stale snapshot did NOT
    # overwrite it.
    assert ep.read_text() == "JIRA_API_TOKEN=written-by-other-writer\n"
    # Vault has NO entry for the key — the CAS abort fires BEFORE _store_all()
    # runs, so the vault never holds the stale snapshot value.  This is the
    # fix for the "stale vault authoritative after aborted rewrite" bug: a
    # re-run will re-snapshot the current .env value and store the fresh one.
    assert SecretVault(config_dir).get("JIRA_API_TOKEN") is None


def test_concurrent_env_change_before_vault_store_aborts_without_vault_write(
    tmp_path: Path, monkeypatch
) -> None:
    """A concurrent .env change that occurs BEFORE the vault store (i.e. before
    the CAS check inside the lock section) must abort WITHOUT writing anything
    to the vault.

    This is the key ordering invariant introduced by the reorder fix: the vault
    write (`_store_all`) now runs INSIDE the lock+CAS critical section, AFTER
    the CAS confirms the .env snapshot is still fresh. A concurrent edit that
    beats us to the .env therefore triggers a CAS abort before any vault write
    runs — so the vault never ends up holding a value derived from a stale
    snapshot that `.env` has already moved past.
    """
    config_dir = tmp_path / "config"
    ep = _write_env(tmp_path, "JIRA_API_TOKEN=original-token\n")

    # Simulate a concurrent writer that rotates .env BETWEEN the snapshot read
    # and the CAS re-read (which now also precedes the vault write).  We use a
    # set_if_absent spy to confirm it is NEVER called when the CAS aborts.
    vault_write_calls: list[str] = []
    real_set_if_absent = SecretVault.set_if_absent

    async def _spy_set_if_absent(self, name, value):
        vault_write_calls.append(name)
        return await real_set_if_absent(self, name, value)

    monkeypatch.setattr(SecretVault, "set_if_absent", _spy_set_if_absent)

    real_read_bytes = Path.read_bytes
    state = {"calls": 0}

    def _racing_read_bytes(self):
        if self == ep:
            state["calls"] += 1
            if state["calls"] == 2:
                # Rotate the token before the CAS re-read confirms the snapshot.
                ep.write_text("JIRA_API_TOKEN=rotated-token\n")
        return real_read_bytes(self)

    monkeypatch.setattr(Path, "read_bytes", _racing_read_bytes)

    with pytest.raises(MigrationConflictError):
        _migrate(config_dir, ep, dry_run=False)

    # The vault write must NEVER have been called — the CAS abort fires before
    # _store_all() runs, so no stale value reaches the vault.
    assert vault_write_calls == [], (
        f"set_if_absent was called for {vault_write_calls!r} despite CAS abort — "
        "the ordering fix is not in effect"
    )
    assert SecretVault(config_dir).get("JIRA_API_TOKEN") is None

    # The concurrent writer's rotated value is intact; nothing was clobbered.
    assert ep.read_text() == "JIRA_API_TOKEN=rotated-token\n"


def test_concurrent_env_write_after_verify_caught_by_final_cas(tmp_path: Path, monkeypatch) -> None:
    """A concurrent .env writer that does NOT take the .env lock (today the
    WeChat/Weixin token handler, which serializes only on an in-process
    asyncio.Lock) can rewrite the .env AFTER the early CAS + vault store +
    re-verify but BEFORE the atomic rewrite. Without a final check the rewrite
    would clobber that writer's change with our stale snapshot. The final CAS
    re-read immediately before atomic_write catches it and aborts, so the
    concurrent write survives.

    The apply path reads ep.read_bytes three times: (1) the snapshot, (2) the
    early CAS (before the vault store), (3) the final CAS (right before the
    write). We let 1 and 2 pass unchanged — so the vault store and re-verify DO
    run — then mutate .env just before call 3 fires.
    """
    config_dir = tmp_path / "config"
    ep = _write_env(tmp_path, "JIRA_API_TOKEN=original-token\n")

    real_read_bytes = Path.read_bytes
    state = {"calls": 0}

    def _racing_read_bytes(self):
        if self == ep:
            state["calls"] += 1
            # Before the THIRD read (the final CAS) fires, a lock-less writer
            # rewrites the .env. Calls 1 (snapshot) and 2 (early CAS) see the
            # original bytes, so the store + re-verify complete normally.
            if state["calls"] == 3:
                ep.write_text("JIRA_API_TOKEN=rotated-by-weixin\n")
        return real_read_bytes(self)

    monkeypatch.setattr(Path, "read_bytes", _racing_read_bytes)

    with pytest.raises(MigrationConflictError):
        _migrate(config_dir, ep, dry_run=False)

    # The concurrent writer's value survived — the final CAS aborted the
    # rewrite instead of clobbering it with our stale snapshot.
    assert ep.read_text() == "JIRA_API_TOKEN=rotated-by-weixin\n"


def test_key_already_in_vault_is_not_overwritten_but_env_rewritten(
    tmp_path: Path,
) -> None:
    """A key already present in the vault with a value MATCHING the .env
    plaintext is the idempotent re-run case: --apply must NOT re-write the vault
    entry, but it MUST rewrite the .env line to secret://KEY so the two stay
    consistent. (A DIFFERING vault value is a conflict — see
    test_vault_value_differs_from_env_aborts.)"""
    import asyncio

    config_dir = tmp_path / "config"
    # Pre-seed the vault with the token; .env carries the SAME value (a prior
    # migration already stored it, or a re-run of the same import).
    vault = SecretVault(config_dir)
    asyncio.new_event_loop().run_until_complete(vault.set("JIRA_API_TOKEN", "same-value"))

    ep = _write_env(tmp_path, "JIRA_API_TOKEN=same-value\n")

    _migrate(config_dir, ep, dry_run=False)

    # Vault entry is UNCHANGED.
    assert SecretVault(config_dir).get("JIRA_API_TOKEN").reveal() == "same-value"
    # .env line is rewritten to the secret:// reference for consistency.
    assert ep.read_text() == "JIRA_API_TOKEN=secret://JIRA_API_TOKEN\n"


def test_vault_value_differs_from_env_aborts(tmp_path: Path) -> None:
    """If the vault already holds a DECRYPTABLE value that DIFFERS from the .env
    plaintext, the importer must ABORT — silently rewriting the .env line to
    secret://KEY would discard the .env plaintext while Jira resolves to the
    different vault value (e.g. this run stored `A`, a concurrent edit rewrote
    the vault to `B`; a retry must not swap `A` away for `B` unasked)."""
    import asyncio

    config_dir = tmp_path / "config"
    # Vault holds `B`; .env still carries a different plaintext `A`.
    vault = SecretVault(config_dir)
    asyncio.new_event_loop().run_until_complete(vault.set("JIRA_API_TOKEN", "vault-value-B"))

    ep = _write_env(tmp_path, "JIRA_API_TOKEN=env-plaintext-A\n")

    with pytest.raises(MigrationConflictError):
        _migrate(config_dir, ep, dry_run=False)

    # Neither value is discarded: vault keeps B, .env keeps its plaintext A.
    assert SecretVault(config_dir).get("JIRA_API_TOKEN").reveal() == "vault-value-B"
    assert ep.read_text() == "JIRA_API_TOKEN=env-plaintext-A\n"


def test_lock_held_by_another_process_aborts_cleanly(tmp_path: Path, monkeypatch) -> None:
    """If the .env sidecar lock is already held (another process mid-write), the
    migration aborts with MigrationConflictError rather than blocking or
    clobbering — a re-run reconciles."""
    config_dir = tmp_path / "config"
    ep = tmp_path / ".env"
    ep.write_text("JIRA_API_TOKEN=plaintext-token\n")

    # Force the exclusive-lock acquire to fail, simulating another holder.
    monkeypatch.setattr(
        "kiro_crew.secrets.migrate.platform_compat.try_acquire_lock",
        lambda fd, *, exclusive=False: False,
    )

    with pytest.raises(MigrationConflictError):
        _migrate(config_dir, ep, dry_run=False)

    # Vault has NO entry — the vault write (_store_all) now runs INSIDE the
    # lock section, so a lock-acquisition failure means no vault write occurred.
    # .env is untouched as before.
    assert SecretVault(config_dir).get("JIRA_API_TOKEN") is None
    assert ep.read_text() == "JIRA_API_TOKEN=plaintext-token\n"


def test_undecryptable_vault_entry_aborts_without_rewrite(tmp_path: Path, monkeypatch) -> None:
    """If the vault LISTS a key but its entry does not decrypt (missing/
    mismatched vault key, e.g. a restored store), the importer must ABORT rather
    than treat it as authoritative and rewrite the usable .env plaintext to
    secret://KEY — which would strand Jira on an unusable vault entry."""
    config_dir = tmp_path / "config"
    ep = tmp_path / ".env"
    ep.write_text("JIRA_API_TOKEN=still-usable-plaintext\n")

    # Vault lists the key, but decrypting it raises (corrupt/mismatched entry).
    monkeypatch.setattr(SecretVault, "list_names", lambda self: ["JIRA_API_TOKEN"])

    def _boom(self, name):
        raise ValueError("InvalidTag: cannot decrypt")

    monkeypatch.setattr(SecretVault, "get", _boom)

    with pytest.raises(MigrationConflictError):
        _migrate(config_dir, ep, dry_run=False)

    # The usable plaintext is untouched — not rewritten to a broken secret://.
    assert ep.read_text() == "JIRA_API_TOKEN=still-usable-plaintext\n"


def test_handle_secrets_reports_conflict_cleanly(tmp_path: Path, monkeypatch, capsys) -> None:
    """A MigrationConflictError from the importer must surface at the CLI as a
    concise stderr error with a nonzero exit — never an uncaught traceback."""
    import argparse

    from kiro_crew import cli_commands

    monkeypatch.setattr(cli_commands, "config_dir", lambda: str(tmp_path / "config"))

    def _boom(*, dry_run):
        raise MigrationConflictError("simulated concurrent write; no rewrite performed")

    monkeypatch.setattr(cli_commands, "migrate_env_secrets", _boom)

    args = argparse.Namespace(secrets_action="import", apply=True)
    with pytest.raises(SystemExit) as exc:
        cli_commands._handle_secrets(args)

    assert exc.value.code != 0
    err = capsys.readouterr().err
    assert "error:" in err.lower()
    assert "concurrent write" in err.lower()


def test_handle_secrets_reports_corrupt_vault_cleanly(tmp_path: Path, monkeypatch, capsys) -> None:
    """A truncated/corrupt vault store makes the importer raise ValueError/OSError
    (from list_names/decrypt), not MigrationConflictError. It must still surface
    at the CLI as a concise stderr error with a nonzero exit, never a traceback."""
    import argparse

    from kiro_crew import cli_commands

    monkeypatch.setattr(cli_commands, "config_dir", lambda: str(tmp_path / "config"))

    def _boom(*, dry_run):
        raise ValueError("secrets.enc: Expecting value: line 1 column 1")

    monkeypatch.setattr(cli_commands, "migrate_env_secrets", _boom)

    args = argparse.Namespace(secrets_action="import", apply=True)
    with pytest.raises(SystemExit) as exc:
        cli_commands._handle_secrets(args)

    assert exc.value.code != 0
    err = capsys.readouterr().err
    assert "error:" in err.lower()
    assert "vault" in err.lower()


def test_concurrent_vault_writer_wins_aborts(tmp_path: Path, monkeypatch) -> None:
    """If another writer stores the key in the vault during the import (the
    set_if_absent presence check fails), the importer aborts rather than
    overwriting the newer value or rewriting the .env line to point at a value
    it did not store."""
    config_dir = tmp_path / "config"
    ep = tmp_path / ".env"
    ep.write_text("JIRA_API_TOKEN=env-plaintext\n")

    async def _lost_race(self, name, value):
        return None  # another writer populated it first (set_if_absent returns None)

    monkeypatch.setattr(SecretVault, "set_if_absent", _lost_race)

    with pytest.raises(MigrationConflictError):
        _migrate(config_dir, ep, dry_run=False)

    # .env plaintext untouched — not rewritten to a secret:// we did not store.
    assert ep.read_text() == "JIRA_API_TOKEN=env-plaintext\n"


def test_set_if_absent_does_not_overwrite(tmp_path: Path) -> None:
    """Vault.set_if_absent stores when absent (returns the entry dict) and leaves an
    existing value untouched (returns None)."""
    import asyncio

    vault = SecretVault(tmp_path / "config")
    loop = asyncio.new_event_loop()
    try:
        result_first = loop.run_until_complete(vault.set_if_absent("K", "first"))
        result_second = loop.run_until_complete(vault.set_if_absent("K", "second"))
    finally:
        loop.close()
    # Fresh write returns the encrypted entry dict (truthy, non-None).
    assert result_first is not None and isinstance(
        result_first, dict
    ), "set_if_absent must return the entry dict on a fresh write"
    # Key already present: returns None.
    assert result_second is None, "set_if_absent must return None when key already exists"
    assert SecretVault(tmp_path / "config").get("K").reveal() == "first"


def test_crlf_env_preserves_line_endings(tmp_path: Path) -> None:
    """A CRLF `.env` keeps CRLF on every line after migration — the rewrite must
    not silently convert unchanged (or rewritten) lines to LF."""
    config_dir = tmp_path / "config"
    ep = tmp_path / ".env"
    ep.write_bytes(b"# c\r\nJIRA_API_TOKEN=plain\r\nOTHER=keep\r\n")

    _migrate(config_dir, ep, dry_run=False)

    raw = ep.read_bytes()
    assert b"\r\n" in raw
    assert b"\n" not in raw.replace(b"\r\n", b"")  # no bare LF introduced
    assert b"JIRA_API_TOKEN=secret://JIRA_API_TOKEN\r\n" in raw
    assert b"OTHER=keep\r\n" in raw


def test_apply_survives_non_utf8_env_bytes(tmp_path: Path) -> None:
    """A .env containing non-UTF-8 bytes (decoded via surrogateescape) must not
    crash the atomic rewrite: new_text is re-encoded with surrogateescape so
    the original bytes round-trip byte-for-byte instead of raising
    UnicodeEncodeError under strict UTF-8."""
    config_dir = tmp_path / "config"
    ep = tmp_path / ".env"
    # A migratable token line plus a comment carrying a raw non-UTF-8 byte.
    ep.write_bytes(b"# host \xff comment\nJIRA_API_TOKEN=plaintext-token\n")

    report = _migrate(config_dir, ep, dry_run=False)

    assert "JIRA_API_TOKEN" in report.migrated
    # Vault stored the token; the non-UTF-8 comment byte survived the rewrite.
    assert SecretVault(config_dir).get("JIRA_API_TOKEN").reveal() == "plaintext-token"
    raw = ep.read_bytes()
    assert b"\xff" in raw
    assert b"JIRA_API_TOKEN=secret://JIRA_API_TOKEN" in raw


def test_vault_entry_deleted_between_store_and_rewrite_aborts(tmp_path: Path, monkeypatch) -> None:
    """If a vault entry is deleted after _store_all() writes it but before the
    .env atomic_write (the store→rewrite window), the importer must raise
    MigrationConflictError and leave the .env plaintext untouched — the
    original token must not be lost to a dangling secret:// reference."""
    config_dir = tmp_path / "config"
    ep = tmp_path / ".env"
    ep.write_text("JIRA_API_TOKEN=plaintext-token\n")

    # Simulate a concurrent delete: the real vault.get() returns None, meaning
    # the entry was removed after the vault write but before the .env rewrite.
    real_get = SecretVault.get

    def _get_returns_none(self, name):
        if name == "JIRA_API_TOKEN":
            return None
        return real_get(self, name)

    monkeypatch.setattr(SecretVault, "get", _get_returns_none)

    with pytest.raises(MigrationConflictError, match="deleted during migration"):
        _migrate(config_dir, ep, dry_run=False)

    # .env plaintext is unchanged — not rewritten to a dangling secret://.
    assert ep.read_text() == "JIRA_API_TOKEN=plaintext-token\n"


def test_vault_entry_replaced_between_store_and_rewrite_aborts(tmp_path: Path, monkeypatch) -> None:
    """If a vault entry's value is changed after _store_all() but before the
    .env rewrite, the importer must raise MigrationConflictError and leave the
    .env plaintext untouched — the original value must not be silently discarded
    while .env is rewritten to point at a now-different vault entry."""
    from kiro_crew.secrets.vault import SecretValue

    config_dir = tmp_path / "config"
    ep = tmp_path / ".env"
    ep.write_text("JIRA_API_TOKEN=original-token\n")

    # Simulate a concurrent replacement: the vault write (set_if_absent) stores
    # "original-token" normally, but by the time the re-verify loop calls
    # vault.get() the entry holds "replacement-token" instead — as if a
    # concurrent dashboard edit or second import overwrote it.  We model this
    # by patching vault.get() to return the replacement value; no nested event
    # loop is needed since vault.get() is synchronous.
    real_get = SecretVault.get

    def _get_returns_replacement(self, name):
        if name == "JIRA_API_TOKEN":
            return SecretValue("replacement-token")
        return real_get(self, name)

    monkeypatch.setattr(SecretVault, "get", _get_returns_replacement)

    with pytest.raises(MigrationConflictError, match="changed during migration"):
        _migrate(config_dir, ep, dry_run=False)

    # .env plaintext is unchanged — not rewritten while the vault holds a
    # different value.
    assert ep.read_text() == "JIRA_API_TOKEN=original-token\n"


def test_vault_delete_during_held_lock_cannot_strand_plaintext(tmp_path: Path, monkeypatch) -> None:
    """hold_cross_process_lock() protects the re-verify + atomic_write pair.

    A concurrent vault DELETE that arrives AFTER _store_all() writes the value
    but BEFORE the .env rewrite must NOT strand the plaintext: the importer
    holds the vault cross-process flock for the entire re-verify + rewrite
    window, so any DELETE that tries to acquire the same flock is queued until
    AFTER atomic_write commits.

    We can't simulate a true concurrent DELETE via OS-level locking in a single
    process, so we verify the STRUCTURAL guarantee instead: the
    hold_cross_process_lock() context manager is entered before vault.get() is
    called and is still held when atomic_write() runs.  We do this by patching
    hold_cross_process_lock() to record entry/exit events and patching
    vault.get() + atomic_write to record when they run relative to the lock.
    """
    from contextlib import contextmanager

    from kiro_crew.secrets.vault import SecretVault

    config_dir = tmp_path / "config"
    ep = tmp_path / ".env"
    ep.write_text("JIRA_API_TOKEN=plaintext-token\n")

    events: list[str] = []

    real_hold = SecretVault.hold_cross_process_lock

    @contextmanager
    def _tracking_hold(self):
        events.append("lock_acquired")
        try:
            with real_hold(self):
                yield
        finally:
            events.append("lock_released")

    real_get = SecretVault.get

    def _tracking_get(self, name):
        events.append(f"vault_get:{name}")
        return real_get(self, name)

    real_atomic = None

    def _tracking_atomic_write(path, content, **kwargs):
        events.append("atomic_write")
        real_atomic(path, content, **kwargs)

    import kiro_crew.atomic_write as _atomic_mod
    import kiro_crew.secrets.migrate as _migrate_mod

    real_atomic = _atomic_mod.atomic_write
    monkeypatch.setattr(SecretVault, "hold_cross_process_lock", _tracking_hold)
    monkeypatch.setattr(SecretVault, "get", _tracking_get)
    monkeypatch.setattr(_migrate_mod, "atomic_write", _tracking_atomic_write)

    _migrate(config_dir, ep, dry_run=False)

    # Events will contain two lock cycles:
    # [0] lock_acquired / lock_released — from _store_all()'s set_if_absent
    #     (which uses _cross_process_lock → hold_cross_process_lock internally)
    # [1] lock_acquired / vault_get / atomic_write / lock_released — the outer
    #     critical section added by the fix
    #
    # We verify that vault.get() and atomic_write both occur INSIDE the second
    # (outermost explicit) lock acquisition — i.e. between the last
    # lock_acquired and its matching lock_released.
    assert events.count("lock_acquired") >= 2, "expected ≥2 lock acquisitions"
    write_idx = events.index("atomic_write")
    get_idx = next(i for i, e in enumerate(events) if e.startswith("vault_get:"))
    # Find the lock_acquired that precedes atomic_write (looking backwards).
    outer_acquire_idx = max(
        i for i, e in enumerate(events) if e == "lock_acquired" and i < write_idx
    )
    # Find the lock_released that follows atomic_write (looking forwards).
    outer_release_idx = next(
        i for i, e in enumerate(events) if e == "lock_released" and i > write_idx
    )
    assert outer_acquire_idx < get_idx, "vault.get() must run after the outer lock is acquired"
    assert get_idx < write_idx, "atomic_write must run after vault.get()"
    assert write_idx < outer_release_idx, "lock must be held through atomic_write"


# ── Fix 1: lock inside .vault ────────────────────────────────────────────────


def test_env_lock_path_is_inside_vault_dir(tmp_path: Path) -> None:
    """_env_lock_path must return a path inside the sandbox-hidden .vault dir.

    A lock outside .vault lives in the agent-writable config dir, so an
    unconfined agent can delete/replace the held lock inode and defeat
    serialisation. Inside .vault, every sandbox tier bind-mount-hides the dir.
    """
    from kiro_crew.secrets.migrate import _env_lock_path

    ep = tmp_path / "config" / ".env"
    ep.parent.mkdir(parents=True, exist_ok=True)
    lock = _env_lock_path(ep)
    vault_dir = ep.parent / ".vault"
    assert vault_dir.is_dir(), "vault dir must be created by _env_lock_path"
    assert lock.parent == vault_dir, f"lock {lock} must be inside {vault_dir}"
    assert lock.name == ".env.lock"


# ── Fix 2: orphan vault rollback on failed apply ─────────────────────────────


def test_atomic_write_failure_rolls_back_vault_entries(tmp_path: Path, monkeypatch) -> None:
    """When atomic_write raises after vault entries are stored, every entry
    written by this run must be deleted so the vault has NO orphan entries and
    a re-run starts from the original plaintext.  The .env must be unchanged."""
    config_dir = tmp_path / "config"
    ep = tmp_path / ".env"
    ep.write_text("JIRA_API_TOKEN=plaintext-tok\n")

    def _boom(path, data, **kw):
        raise OSError("disk full")

    monkeypatch.setattr("kiro_crew.secrets.migrate.atomic_write", _boom)

    with pytest.raises(OSError):
        _migrate(config_dir, ep, dry_run=False)

    # Vault must have NO entry — the rollback deleted what _store_all wrote.
    assert SecretVault(config_dir).get("JIRA_API_TOKEN") is None
    # .env is untouched — the atomic rewrite never ran.
    assert ep.read_text() == "JIRA_API_TOKEN=plaintext-tok\n"


def test_partial_store_failure_rolls_back_the_first_key(tmp_path: Path, monkeypatch) -> None:
    """A multi-key import that stores key 1 then hits a conflict on key 2 raises
    from INSIDE _store_all. That raise must still roll back key 1 — otherwise it
    stays an orphan vault entry that vault-first resolution could later use to
    shadow a rotated plaintext. Assert the first key is deleted after the abort."""
    config_dir = tmp_path / "config"
    ep = tmp_path / ".env"
    # Two migratable global-style keys (JIRA per-host tokens are migrated).
    hostA = "a.atlassian.net".encode().hex().upper()
    hostB = "b.atlassian.net".encode().hex().upper()
    keyA = f"JIRA_TOKEN_{hostA}"
    keyB = f"JIRA_TOKEN_{hostB}"
    ep.write_text(f"{keyA}=tok-a\n{keyB}=tok-b\n")

    # set_if_absent: store the first key for real, then simulate a concurrent
    # writer winning the second key (returns False → _store_all raises).
    real_set_if_absent = SecretVault.set_if_absent
    seen: list[str] = []

    async def _second_key_conflicts(self, name, value):
        seen.append(name)
        if len(seen) == 1:
            return await real_set_if_absent(self, name, value)  # first key writes
        return None  # second key: another writer won → _store_all raises

    monkeypatch.setattr(SecretVault, "set_if_absent", _second_key_conflicts)

    with pytest.raises(MigrationConflictError):
        _migrate(config_dir, ep, dry_run=False)

    # The first key was stored then ROLLED BACK — no orphan entry survives.
    v = SecretVault(config_dir)
    assert v.get(keyA) is None
    assert v.get(keyB) is None
    # .env untouched.
    assert ep.read_text() == f"{keyA}=tok-a\n{keyB}=tok-b\n"


def test_rollback_does_not_delete_concurrently_overwritten_vault_entry(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Rollback must NOT delete a vault entry that a concurrent writer overwrote
    between our set_if_absent call and the failure.  Deleting the concurrent
    writer's newer value would cause silent data loss.

    The fix: rollback now uses _compare_and_delete_sync with ciphertext-identity
    comparison — it receives the exact encrypted entry dict set_if_absent returned,
    and only deletes when the stored dict matches exactly. A concurrent writer
    produces a different ciphertext dict (random nonce) so its entry is never
    deleted.

    This test verifies the rollback path at the integration level: it patches
    _compare_and_delete_sync to return False (simulating 'concurrent overwrite
    detected — entry dicts differ') and asserts that the vault entry is NOT
    deleted, while the unit tests (test_compare_and_delete_sync_skips_changed_entry
    and test_compare_and_delete_sync_deletes_matching_entry) verify the primitive
    itself against real ciphertext.
    """
    config_dir_path = tmp_path / "config"
    ep = tmp_path / ".env"
    ep.write_text("JIRA_API_TOKEN=our-value\n")

    def _boom(path, data, **kw):
        raise OSError("disk full")

    monkeypatch.setattr("kiro_crew.secrets.migrate.atomic_write", _boom)

    # Patch _compare_and_delete_sync to return False (simulating concurrent
    # overwrite detected: stored entry dict differs from expected_entry) and
    # track calls. expected is now a dict[str, str] entry, not a plaintext str.
    _cad_calls: list[tuple] = []

    def _fake_cad(self, name, expected):
        _cad_calls.append((name, expected))
        return False  # simulate: stored dict differs, do NOT delete

    monkeypatch.setattr(SecretVault, "_compare_and_delete_sync", _fake_cad)

    with pytest.raises(OSError):
        _migrate(config_dir_path, ep, dry_run=False)

    # _compare_and_delete_sync must have been called for our key.
    assert any(
        k == "JIRA_API_TOKEN" for k, _ in _cad_calls
    ), "_compare_and_delete_sync was not called for JIRA_API_TOKEN"
    # The expected argument must be a dict (the ciphertext entry), not a string.
    for k, expected in _cad_calls:
        if k == "JIRA_API_TOKEN":
            assert isinstance(
                expected, dict
            ), f"_compare_and_delete_sync expected arg must be an entry dict, got {type(expected)}"
    # Because _compare_and_delete_sync returned False, the vault entry must
    # NOT have been deleted.  The .env is untouched.
    assert ep.read_text() == "JIRA_API_TOKEN=our-value\n"


def test_compare_and_delete_sync_skips_changed_entry(tmp_path: Path) -> None:
    """_compare_and_delete_sync must return False (and leave the entry intact)
    when the stored ciphertext entry dict differs from expected_entry — i.e. a
    concurrent writer has overwrote the entry between our set_if_absent and the
    rollback.  Even the same plaintext value encrypts to a different dict
    (random nonce), so any re-encryption by another writer produces a different
    expected_entry and rollback must NOT delete it."""
    import asyncio as _asyncio

    v = SecretVault(tmp_path / "vault")
    loop = _asyncio.new_event_loop()
    try:
        # Capture the entry dict that set_if_absent wrote.
        original_entry = loop.run_until_complete(v.set_if_absent("KEY", "original"))
    finally:
        loop.close()
    assert original_entry is not None

    # Simulate a concurrent writer: overwrite with the same plaintext value.
    # Because _encrypt_entry uses a random nonce, this produces a DIFFERENT
    # ciphertext dict from the one set_if_absent returned.
    v._set_sync("KEY", "original")

    # Read back the new stored entry to confirm it differs from original_entry.
    stored_entries = v._load_entries()
    assert stored_entries["KEY"] != original_entry, (
        "Test setup: re-encryption must produce a different ciphertext dict "
        "(random nonce guarantee)"
    )

    # compare_and_delete with the OLD entry dict -> ciphertext differs -> must NOT delete.
    deleted = v._compare_and_delete_sync("KEY", original_entry)
    assert (
        not deleted
    ), "_compare_and_delete_sync must return False when stored ciphertext differs from expected"
    entry = v.get("KEY")
    assert (
        entry is not None and entry.reveal() == "original"
    ), "Entry must not be deleted when ciphertext identity does not match"


def test_compare_and_delete_sync_deletes_matching_entry(tmp_path: Path) -> None:
    """_compare_and_delete_sync must return True and delete the entry when the
    stored ciphertext entry dict exactly matches expected_entry (no concurrent
    overwrite — same dict that set_if_absent wrote)."""
    import asyncio as _asyncio

    v = SecretVault(tmp_path / "vault")
    loop = _asyncio.new_event_loop()
    try:
        entry = loop.run_until_complete(v.set_if_absent("KEY", "our-value"))
    finally:
        loop.close()
    assert entry is not None, "set_if_absent must return entry dict on fresh write"

    deleted = v._compare_and_delete_sync("KEY", entry)
    assert deleted, "_compare_and_delete_sync must return True when entry dict matches"
    assert v.get("KEY") is None, "Entry must be deleted when ciphertext identity matches"


def test_rollback_deletes_unchanged_vault_entry(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Rollback MUST delete a vault entry when the value is still the one THIS
    run wrote (no concurrent overwrite).  _compare_and_delete_sync compares
    under the flock and deletes atomically.
    """
    config_dir_path = tmp_path / "config"
    ep = tmp_path / ".env"
    ep.write_text("JIRA_API_TOKEN=plaintext-tok\n")

    def _boom(path, data, **kw):
        raise OSError("disk full")

    monkeypatch.setattr("kiro_crew.secrets.migrate.atomic_write", _boom)

    with pytest.raises(OSError):
        _migrate(config_dir_path, ep, dry_run=False)

    # Value was unchanged between write and rollback -> rollback MUST delete it.
    v = SecretVault(config_dir_path)
    assert (
        v.get("JIRA_API_TOKEN") is None
    ), "Rollback should have deleted the vault entry when the value was unchanged"
    # .env untouched.
    assert ep.read_text() == "JIRA_API_TOKEN=plaintext-tok\n"


def test_rollback_no_deadlock_under_concurrent_flock(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Verify _compare_and_delete_sync does not deadlock when called from the
    rollback path.  The old vault.delete() path would re-acquire the cross-process
    flock inside _write_store, which deadlocks on POSIX if the caller already holds
    hold_cross_process_lock().  The new path calls _compare_and_delete_sync via
    asyncio.to_thread, which acquires the flock exactly once inside _write_store
    and never holds it externally.  This test confirms the rollback completes
    without hanging.
    """
    config_dir_path = tmp_path / "config"
    ep = tmp_path / ".env"
    ep.write_text("JIRA_API_TOKEN=deadlock-test\n")

    def _boom(path, data, **kw):
        raise OSError("disk full")

    monkeypatch.setattr("kiro_crew.secrets.migrate.atomic_write", _boom)

    # Call the rollback path directly (no background worker): the fixed
    # _compare_and_delete_sync acquires the cross-process flock exactly once
    # inside _write_store and never while the caller holds it, so the rollback
    # runs to completion and _migrate raises the injected OSError rather than
    # hanging. A direct call avoids leaking a daemon worker / per-test lock
    # state if this ever regressed (the old probe left the thread running).
    with pytest.raises(OSError, match="disk full"):
        _migrate(config_dir_path, ep, dry_run=False)


def test_rollback_delete_failure_records_orphaned_key_and_propagates_error(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    """When _compare_and_delete_sync raises during rollback (e.g. ENOSPC),
    the failing key must be recorded in report.orphaned_vault_keys so the caller
    can surface it to the operator, AND the original error must still propagate.

    This covers Finding 3: the old ``except Exception: pass`` silently swallowed
    ENOSPC, leaving an authoritative vault entry that could shadow future rotations
    without any diagnostic.
    """
    import logging

    config_dir_path = tmp_path / "config"
    ep = tmp_path / ".env"
    ep.write_text("JIRA_API_TOKEN=secret-val\n")

    # Make atomic_write fail so the rollback path is exercised.
    def _boom(path, data, **kw):
        raise OSError("disk full")

    monkeypatch.setattr("kiro_crew.secrets.migrate.atomic_write", _boom)

    # Make _compare_and_delete_sync raise to simulate ENOSPC / I/O error.
    def _raise_enospc(self, name, expected):
        raise OSError("no space left on device")

    monkeypatch.setattr(SecretVault, "_compare_and_delete_sync", _raise_enospc)

    with caplog.at_level(logging.WARNING, logger="kiro_crew.secrets.migrate"):
        with pytest.raises(OSError, match="disk full"):
            _migrate(config_dir_path, ep, dry_run=False)

    # The orphaned key must be surfaced via a WARNING log message.
    warning_msgs = [r.message for r in caplog.records if r.levelno >= logging.WARNING]
    assert any(
        "JIRA_API_TOKEN" in str(m) for m in warning_msgs
    ), f"Expected WARNING naming orphaned key; got: {warning_msgs}"
    assert any(
        "Settings > Secrets" in str(m) for m in warning_msgs
    ), "WARNING must name a cleanup surface that exists (the CLI has no `secrets rm`)"
    # The remediation half of this format string carried a fourth `%s` for the
    # key name; dropping it without dropping its argument would leave the
    # message half-interpolated (logging swallows the mismatch to stderr).
    assert not any("%s" in str(m) or "%r" in str(m) for m in warning_msgs), (
        f"WARNING left an uninterpolated placeholder — format args and "
        f"placeholders disagree: {warning_msgs}"
    )


class TestRemediationNamesOnlyRealCliSubcommands:
    """Every ``kirocrew secrets <verb>`` a shipped message tells an operator to
    run must be a verb the CLI actually registers.

    The defect this pins (#6889): the importer's remediation strings named
    ``kirocrew secrets set`` and ``kirocrew secrets rm``, but the ``secrets``
    parser registers only ``import`` — so an operator who followed the message
    at the exact moment a migration aborted got ``invalid choice`` and no way
    forward. A dead instruction on a data-loss-adjacent path is worse than no
    instruction, because it costs the operator a round trip before they start
    looking for the real surface.

    Scope is all of ``src/``, not just ``migrate.py``: the same wording is
    reachable from the resolution path, and a per-file check would let the next
    site re-introduce it. The oracle is the REAL parser (``cli.main()``), not a
    hand-maintained list, so a verb that later ships genuinely passes with no
    edit here.
    """

    _CMD_RE = re.compile(r"kirocrew secrets ([a-z][a-z0-9-]*)")

    @classmethod
    def _referenced_verbs(cls) -> dict[str, list[str]]:
        """Map each ``secrets`` verb named under ``src/`` to the files naming it."""
        src_root = Path(kiro_crew.__file__).resolve().parent
        found: dict[str, list[str]] = {}
        for path in sorted(src_root.rglob("*.py")):
            for verb in cls._CMD_RE.findall(path.read_text(encoding="utf-8")):
                found.setdefault(verb, []).append(str(path.relative_to(src_root)))
        return found

    @staticmethod
    def _verb_is_registered(verb: str, monkeypatch, tmp_path, capsys) -> bool:
        """True when ``kirocrew secrets <verb> --help`` parses.

        ``--help`` makes argparse exit 0 during parsing, so the verb's handler
        never runs (nothing is read, written or migrated). An unregistered verb
        exits 2 with ``invalid choice`` instead.

        ``main()`` mutates PROCESS-GLOBAL state before it reaches argparse, and
        none of it is monkeypatch's to restore, so a bare call would leak into
        every later test in the same worker:

        * ``ensure_utf8_console`` overwrites ``PYTHONUTF8`` /
          ``PYTHONIOENCODING`` on EVERY platform (not only Windows — see
          ``_ensure_utf8_process_environment``, "Overwrite inherited settings
          deliberately") and on Windows also replaces ``sys.stdout`` /
          ``sys.stderr``, which fights pytest's capture. Stubbed: this probe
          reads argparse's exit code, never the encoding of what it printed.
        * ``main()`` unconditionally pops ``KIROCREW_SANDBOX_ACTIVE`` and
          ``KIROCREW_SANDBOX_LEVEL`` to refuse an inherited sandbox-bypass
          marker. ``delenv`` first so MONKEYPATCH owns the deletion and restores
          any inherited value at teardown, leaving the pop a no-op.
        """
        monkeypatch.setattr(cli.platform_compat, "ensure_utf8_console", lambda: None)
        for marker in ("KIROCREW_SANDBOX_ACTIVE", "KIROCREW_SANDBOX_LEVEL"):
            monkeypatch.delenv(marker, raising=False)
        monkeypatch.setenv("KIROCREW_HOME", str(tmp_path / "data"))
        monkeypatch.setenv("KIROCREW_PROJECT_DIR", str(tmp_path))
        monkeypatch.setattr(cli, "boot_platform", lambda *_a, **_k: None)
        monkeypatch.setattr(cli, "_setup_cli_logging", lambda *_a, **_k: None)
        monkeypatch.setattr(sys, "argv", ["kirocrew", "secrets", verb, "--help"])
        with pytest.raises(SystemExit) as excinfo:
            cli.main()
        capsys.readouterr()
        return excinfo.value.code == 0

    def test_probe_does_not_clobber_inherited_encoding_env(self, monkeypatch, tmp_path, capsys):
        """The probe must not leave the worker's UTF-8 contract rewritten.

        ``main()`` overwrites both names on every platform, and monkeypatch
        cannot restore what it never set — so dropping the
        ``ensure_utf8_console`` stub silently hands every later test in this
        worker a different encoding environment. Sentinels catch that.
        """
        monkeypatch.setenv("PYTHONUTF8", "sentinel-utf8")
        monkeypatch.setenv("PYTHONIOENCODING", "sentinel-ioencoding")
        self._verb_is_registered("import", monkeypatch, tmp_path, capsys)
        assert os.environ["PYTHONUTF8"] == "sentinel-utf8"
        assert os.environ["PYTHONIOENCODING"] == "sentinel-ioencoding"

    def test_oracle_rejects_a_verb_that_does_not_exist(self, monkeypatch, tmp_path, capsys):
        """Guard the guard: the probe must be able to FAIL, or the scan below
        is vacuous and would pass on any wording at all."""
        assert not self._verb_is_registered("no-such-verb-xyz", monkeypatch, tmp_path, capsys)

    def test_the_scan_finds_something(self):
        """A regex that stops matching (a rewording to prose, say) would make
        this file silently stop guarding anything."""
        assert self._referenced_verbs(), "no `kirocrew secrets <verb>` reference found under src/"

    def test_every_referenced_verb_is_registered(self, monkeypatch, tmp_path, capsys):
        dead = {
            verb: files
            for verb, files in self._referenced_verbs().items()
            if not self._verb_is_registered(verb, monkeypatch, tmp_path, capsys)
        }
        assert not dead, (
            "shipped remediation text tells the operator to run a `kirocrew secrets` "
            f"subcommand the CLI does not register: {dead}. Point the message at a real "
            "surface (Settings > Secrets in the dashboard, or `kirocrew secrets import`)."
        )
