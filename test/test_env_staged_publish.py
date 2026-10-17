"""The data home's ``.env`` publish temp must never sit at a name no mask covers.

``.env`` is a hidden leaf (``sandbox._CREW_HIDDEN_LEAVES``) masked by its exact name, and
it holds chat-channel tokens. A mask covers a PATH rather than an inode, and
``atomic_write`` stages its temp with ``mkstemp(dir=path.parent)``, so a plain
``atomic_write`` onto ``.env`` would put the whole file in the data-home root --
sandbox-visible and ``link(2)``-able by a same-uid agent -- for the length of the write,
and a crash there would leave the same bytes behind at an unmasked name.

Every ``.env`` writer publishes through ``hidden_leaf_staging.publish_env``, which stages
inside the masked ``auth-store-staging`` directory. These tests drive each writer and pin
that its temp, and anything a failed publish could leave, lives only in that directory.
"""

from __future__ import annotations

import errno
import sys
import tempfile
from pathlib import Path

import pytest

from kiro_crew import atomic_write as aw
from kiro_crew import hidden_leaf_staging
from kiro_crew.config import loader
from kiro_crew.sandbox_leaves import _AUTH_STORE_STAGING_LEAF, _CREW_HIDDEN_LEAVES

_SEED = "WEIXIN_TOKEN=old-value\nJIRA_API_TOKEN=plaintext-jira\nNOTE=keep\n"


@pytest.fixture
def crew_home(tmp_path, monkeypatch):
    home = tmp_path / "crew-home"
    home.mkdir(mode=0o700)
    monkeypatch.setenv("KIROCREW_HOME", str(home))
    ep = home / ".env"
    ep.write_text(_SEED, encoding="utf-8")
    ep.chmod(0o600)

    from kiro_crew import cli_setup
    from kiro_crew.dashboard.handlers import weixin_qr
    from kiro_crew.secrets import migrate

    for mod in (loader, cli_setup, weixin_qr, migrate):
        monkeypatch.setattr(mod, "env_path", lambda: ep, raising=False)
    monkeypatch.setattr(migrate, "config_dir", lambda: home / "vault-home", raising=False)
    return home


@pytest.fixture
def env_temps(monkeypatch, crew_home):
    """Record the directory of every temp ``atomic_write`` stages for the ``.env`` write."""
    seen: list[Path] = []
    real = tempfile.mkstemp

    def _recording(*args, **kwargs):
        fd, name = real(*args, **kwargs)
        seen.append(Path(name).parent)
        return fd, name

    monkeypatch.setattr(aw.tempfile, "mkstemp", _recording)
    return seen


def _dashboard_write():
    from kiro_crew.dashboard.handlers import messaging

    messaging._write_env_updates({"KIROCREW_OWNER_ID": "U0DASHBOARD1"})
    return "KIROCREW_OWNER_ID=U0DASHBOARD1"


def _weixin_upsert():
    from kiro_crew.dashboard.handlers import weixin_qr

    weixin_qr._write_env_secret("WEIXIN_TOKEN", "new-weixin-value")
    return "new-weixin-value"


def _weixin_delete():
    from kiro_crew.dashboard.handlers import weixin_qr

    weixin_qr._delete_env_key("WEIXIN_TOKEN")
    return None


def _setup_wizard(monkeypatch):
    from kiro_crew import cli_setup

    answers = ["y", "xapp-ok", "xoxb-ok", "U03T18B4Y23"]
    monkeypatch.setattr("builtins.input", lambda _p="": answers.pop(0))
    monkeypatch.setattr(sys.stdin, "isatty", lambda: False, raising=False)
    monkeypatch.setattr(sys.stdout, "isatty", lambda: False, raising=False)
    cli_setup._setup_slack_tokens()
    return "xoxb-ok"


def _secrets_import():
    from kiro_crew.secrets import migrate

    report = migrate.migrate_env_secrets(dry_run=False)
    assert "JIRA_API_TOKEN" in report.migrated
    return "JIRA_API_TOKEN=secret://JIRA_API_TOKEN"


WRITERS = {
    "dashboard-channel-save": lambda mp: _dashboard_write(),
    "weixin-upsert": lambda mp: _weixin_upsert(),
    "weixin-delete": lambda mp: _weixin_delete(),
    "setup-wizard": _setup_wizard,
    "secrets-import": lambda mp: _secrets_import(),
}


def _stray_env_copies(home: Path, ep: Path) -> list[str]:
    """Temp files left in the data-home root, where no mask covers them."""
    return [p.name for p in home.iterdir() if p.is_file() and p != ep and p.name.endswith(".tmp")]


@pytest.mark.parametrize("writer", sorted(WRITERS))
def test_env_temp_is_staged_only_under_the_masked_directory(
    writer, crew_home, env_temps, monkeypatch
):
    ep = crew_home / ".env"
    assert ep.name in _CREW_HIDDEN_LEAVES, ".env is no longer a hidden leaf"

    expected = WRITERS[writer](monkeypatch)

    text = ep.read_text(encoding="utf-8")
    if expected is not None:
        assert expected in text
    assert "NOTE=keep" in text, "the rewrite dropped an unrelated line"
    assert env_temps, "atomic_write staged nothing; the recorder is not wired to the writer"
    assert crew_home not in env_temps, (
        f"{writer} staged a .env temp in the data-home root {crew_home}: a same-uid "
        "sandboxed agent can read or link(2) it for the length of the write"
    )
    staging = crew_home / _AUTH_STORE_STAGING_LEAF
    assert staging in env_temps
    assert _stray_env_copies(crew_home, ep) == []
    assert list(staging.iterdir()) == [], f"{writer} left a staged temp behind"
    if sys.platform != "win32":
        assert ep.stat().st_mode & 0o077 == 0, "the published .env is not owner-only"


@pytest.mark.parametrize("writer", sorted(WRITERS))
def test_failed_env_publish_leaves_no_orphan_and_keeps_the_old_file(writer, crew_home, monkeypatch):
    ep = crew_home / ".env"
    before = ep.read_bytes()

    def _refuse(_src, _dst):
        raise OSError(errno.EIO, "simulated publish failure")

    monkeypatch.setattr(hidden_leaf_staging, "replace_with_retry", _refuse)
    try:
        WRITERS[writer](monkeypatch)
    except Exception:  # each writer answers a failed publish its own way
        pass

    assert ep.read_bytes() == before, "a failed publish changed the published .env"
    staging = crew_home / _AUTH_STORE_STAGING_LEAF
    assert list(staging.iterdir()) == [], f"{writer} left its staged copy behind"
    assert _stray_env_copies(crew_home, ep) == []


def test_unusable_staging_refuses_rather_than_writing_beside_env(crew_home, env_temps):
    """A planted non-directory at the staging name must NOT fall back to a sibling temp."""
    (crew_home / _AUTH_STORE_STAGING_LEAF).write_text("planted", encoding="utf-8")
    ep = crew_home / ".env"
    before = ep.read_bytes()

    from kiro_crew.dashboard.handlers import weixin_qr

    with pytest.raises(OSError):
        weixin_qr._write_env_secret("WEIXIN_TOKEN", "never-written")
    assert ep.read_bytes() == before
    assert crew_home not in env_temps


def test_dashboard_save_keeps_its_warn_lockdown_policy(crew_home, env_temps, monkeypatch):
    """A refused owner-only lockdown still warns rather than aborting a dashboard save."""
    real = aw.atomic_write

    def _fake_publish(path, content, **kwargs):
        assert kwargs.get("restrict_on_error") == "warn"
        return real(path, content, **kwargs)

    monkeypatch.setattr(hidden_leaf_staging, "atomic_write", _fake_publish)
    _dashboard_write()
    assert "KIROCREW_OWNER_ID=U0DASHBOARD1" in (crew_home / ".env").read_text(encoding="utf-8")
    assert crew_home not in env_temps


def test_secrets_import_keeps_fsync_and_runs_nothing_after_the_rename(crew_home, monkeypatch):
    """The import fsyncs the staged file, and the rename is the publish's last step."""
    calls: list[str] = []
    real_atomic = hidden_leaf_staging.atomic_write
    real_replace = hidden_leaf_staging.replace_with_retry

    def _record_atomic(path, content, **kwargs):
        assert kwargs.get("fsync") is True
        assert kwargs.get("restrict_on_error") == "raise"
        calls.append("write")
        return real_atomic(path, content, **kwargs)

    def _record_replace(src, dst):
        real_replace(src, dst)
        calls.append("rename")

    monkeypatch.setattr(hidden_leaf_staging, "atomic_write", _record_atomic)
    monkeypatch.setattr(hidden_leaf_staging, "replace_with_retry", _record_replace)
    _secrets_import()
    assert calls == ["write", "rename"]


def _plant_unusable_staging(home: Path) -> None:
    (home / _AUTH_STORE_STAGING_LEAF).write_text("planted", encoding="utf-8")


def test_wizard_prints_the_fix_when_staging_is_unusable(crew_home, monkeypatch, capsys):
    _plant_unusable_staging(crew_home)
    before = (crew_home / ".env").read_bytes()

    _setup_wizard(monkeypatch)  # must not raise

    out = capsys.readouterr().out
    assert "Nothing was saved" in out
    assert _AUTH_STORE_STAGING_LEAF in out
    assert "Credentials saved" not in out
    assert (crew_home / ".env").read_bytes() == before


def test_dashboard_save_answers_409_with_the_fix_when_staging_is_unusable(crew_home):
    import asyncio

    from aiohttp import web

    from kiro_crew.dashboard.handlers import messaging

    _plant_unusable_staging(crew_home)
    before = (crew_home / ".env").read_bytes()

    async def _save():
        await messaging._write_env_off_loop({"KIROCREW_OWNER_ID": "U0DASHBOARD1"})

    with pytest.raises(web.HTTPConflict) as info:
        asyncio.run(_save())
    assert _AUTH_STORE_STAGING_LEAF in info.value.text
    assert (crew_home / ".env").read_bytes() == before


def test_publish_env_names_the_refusal_with_one_type(crew_home):
    _plant_unusable_staging(crew_home)
    with pytest.raises(hidden_leaf_staging.EnvStagingRefused) as info:
        hidden_leaf_staging.publish_env(crew_home / ".env", "A=1\n")
    assert _AUTH_STORE_STAGING_LEAF in str(info.value)
    assert "was not changed" in str(info.value)


@pytest.mark.skipif(sys.platform == "win32", reason="POSIX mode bits")
def test_a_foreign_writable_staging_directory_refuses_with_the_same_type(crew_home, monkeypatch):
    from kiro_crew import platform_compat

    staging = crew_home / _AUTH_STORE_STAGING_LEAF
    staging.mkdir(mode=0o700)
    staging.chmod(0o777)
    # The narrowing does not stick, as on a mount that cannot express modes.
    monkeypatch.setattr(platform_compat, "restrict_dir_to_owner", lambda _p: None)
    with pytest.raises(hidden_leaf_staging.EnvStagingRefused) as info:
        hidden_leaf_staging.publish_env(crew_home / ".env", "A=1\n")
    # The owner-only chmod was already tried, so the fix must not be to run it again.
    assert "chmod" not in str(info.value)
    assert "move the data home" in str(info.value)
    assert (crew_home / ".env").read_text(encoding="utf-8") == _SEED


def test_the_refusal_speaks_about_the_env_save_not_the_auth_stores(crew_home):
    _plant_unusable_staging(crew_home)
    with pytest.raises(hidden_leaf_staging.EnvStagingRefused) as info:
        hidden_leaf_staging.publish_env(crew_home / ".env", "A=1\n")
    text = str(info.value)
    assert "save again" in text
    assert "signing key" not in text and "restart the gateway" not in text
    assert isinstance(info.value.__cause__, OSError)


def test_secrets_import_cli_names_the_staging_directory_not_the_vault(
    crew_home, monkeypatch, capsys
):
    import argparse

    from kiro_crew import cli_commands

    _plant_unusable_staging(crew_home)
    monkeypatch.setattr(
        cli_commands,
        "migrate_env_secrets",
        lambda **_kw: hidden_leaf_staging.publish_env(crew_home / ".env", "A=1\n"),
    )
    with pytest.raises(SystemExit) as info:
        cli_commands._handle_secrets(argparse.Namespace(secrets_action="import", apply=True))
    assert info.value.code == 1
    err = capsys.readouterr().err
    assert _AUTH_STORE_STAGING_LEAF in err
    assert "vault" not in err


def test_a_windows_lock_refusal_names_a_fix_that_works_there(crew_home, monkeypatch):
    """On Windows removing the directory cannot help, so the message must not offer it."""
    import os

    from kiro_crew import platform_compat

    class _NtOs:
        """The real ``os`` module, reporting ``name == "nt"`` to this module only."""

        name = "nt"

        def __getattr__(self, attr):
            return getattr(os, attr)

    def _refuse(_p):
        raise OSError(errno.EPERM, "SetSecurityInfo refused")

    monkeypatch.setattr(platform_compat, "restrict_dir_to_owner", _refuse)
    monkeypatch.setattr(hidden_leaf_staging, "os", _NtOs())
    with pytest.raises(hidden_leaf_staging.EnvStagingRefused) as info:
        hidden_leaf_staging.publish_env(crew_home / ".env", "A=1\n")
    text = str(info.value)
    assert "Windows refused" in text and "NTFS" in text
    assert "chmod" not in text and "Remove it" not in text
    assert (crew_home / ".env").read_text(encoding="utf-8") == _SEED


def test_the_refusal_text_carries_no_errno_prefix(crew_home):
    _plant_unusable_staging(crew_home)
    with pytest.raises(hidden_leaf_staging.EnvStagingRefused) as info:
        hidden_leaf_staging.publish_env(crew_home / ".env", "A=1\n")
    assert str(info.value).startswith(str(crew_home / ".env"))
    assert "[Errno" not in str(info.value)


@pytest.mark.skipif(sys.platform == "win32", reason="POSIX ownership")
def test_a_staging_directory_owned_by_another_account_refuses_with_the_fix(crew_home, monkeypatch):
    import os

    staging = crew_home / _AUTH_STORE_STAGING_LEAF
    staging.mkdir(mode=0o700)
    # The directory reads as owned by another uid, as one left behind by a root run.
    monkeypatch.setattr(hidden_leaf_staging.os, "getuid", lambda: os.lstat(staging).st_uid + 1)
    with pytest.raises(hidden_leaf_staging.EnvStagingRefused) as info:
        hidden_leaf_staging.publish_env(crew_home / ".env", "A=1\n")
    assert "another account owns" in str(info.value)
    assert (crew_home / ".env").read_text(encoding="utf-8") == _SEED
