"""Tests for kiro_crew.secrets.migrate — plaintext .env → vault importer."""

from __future__ import annotations

import os
import stat
from pathlib import Path
from unittest.mock import patch

import pytest

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
    writer's update is preserved — not clobbered by the stale snapshot."""
    config_dir = tmp_path / "config"
    ep = _write_env(tmp_path, "JIRA_API_TOKEN=original\n")

    # Simulate a concurrent writer: after the vault write completes and just
    # before the CAS re-read, another process rewrites .env with a new token.
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
    # Vault holds the snapshot value (written before the abort) — a re-run
    # reconciles idempotently.
    assert SecretVault(config_dir).get("JIRA_API_TOKEN").reveal() == "original"


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

    # Vault holds the value (written before the lock step), but .env is NOT
    # rewritten because the lock could not be taken.
    assert SecretVault(config_dir).get("JIRA_API_TOKEN").reveal() == "plaintext-token"
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
        return False  # another writer populated it first

    monkeypatch.setattr(SecretVault, "set_if_absent", _lost_race)

    with pytest.raises(MigrationConflictError):
        _migrate(config_dir, ep, dry_run=False)

    # .env plaintext untouched — not rewritten to a secret:// we did not store.
    assert ep.read_text() == "JIRA_API_TOKEN=env-plaintext\n"


def test_set_if_absent_does_not_overwrite(tmp_path: Path) -> None:
    """Vault.set_if_absent stores when absent (True) and leaves an existing
    value untouched (False)."""
    import asyncio

    vault = SecretVault(tmp_path / "config")
    loop = asyncio.new_event_loop()
    try:
        assert loop.run_until_complete(vault.set_if_absent("K", "first")) is True
        assert loop.run_until_complete(vault.set_if_absent("K", "second")) is False
    finally:
        loop.close()
    assert SecretVault(tmp_path / "config").get("K").reveal() == "first"


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
