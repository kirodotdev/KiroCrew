"""An upgrade must not be the first thing to touch a store with no fresh copy of it.

`memory.db` is the one file here that cannot be rebuilt from anywhere else, and the
unattended sweep only guarantees a copy from up to ``MIN_BACKUP_INTERVAL_HOURS`` ago.
An upgrade rewrites the code that opens that file, so the window between the last
scheduled copy and the new binary is the window where a bad upgrade costs a day of
memory. `kirocrew update` closes it by taking its own copy first, and by refusing to
dispatch at all when that copy did not land.

The copy sits beside each updater rather than at the top of the command, and these tests
pin both halves of that: every path that rewrites the install copies first, and every
path that rewrites nothing copies nothing — the interval guard is bypassed for this
copy, so a no-op run would otherwise spend a retention slot each time it runs.
"""

from __future__ import annotations

import json
from pathlib import Path
from types import SimpleNamespace

import pytest

from kiro_crew import cli_server, memory_backup
from kiro_crew.memory_stores import DEFAULT_MEMORY_STORE, resolve_store_path
from kiro_crew.vector_memory import VectorMemoryStore

pytestmark = pytest.mark.xdist_group("memory_backup")


@pytest.fixture
def home(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """A data home declaring the default store."""
    (tmp_path / "config.json").write_text(
        json.dumps(
            {
                "memory_stores": {DEFAULT_MEMORY_STORE: {}},
                "default_memory_store": DEFAULT_MEMORY_STORE,
            }
        ),
        encoding="utf-8",
    )
    monkeypatch.setenv("KIROCREW_HOME", str(tmp_path))
    import kiro_crew.config.paths as paths

    monkeypatch.setattr(paths, "_resolved_home", None, raising=False)
    return tmp_path


@pytest.fixture
def live_store(home: Path):
    """A populated default store, left OPEN — the state a real update runs against."""
    store = VectorMemoryStore()
    store.init()
    try:
        store.set_semantic("project.name", "kiro-crew", 1.0, "user_explicit")
        yield store
    finally:
        store.close()


@pytest.fixture
def policy_updater(monkeypatch: pytest.MonkeyPatch) -> list[int]:
    """An install whose update is owned by a policy-defined command, stubbed out.

    The shortest real updater in `_update`: it runs first, it rewrites this install, and
    it is reached without a git checkout or a release feed. Each test reads the returned
    list to see what the store looked like AT THE MOMENT the updater ran.
    """
    from kiro_crew.platform import update_provider

    at_dispatch: list[int] = []
    db = resolve_store_path(DEFAULT_MEMORY_STORE)

    async def _apply():
        at_dispatch.append(len(memory_backup.list_backups(db)))
        return True

    monkeypatch.setattr(
        update_provider, "resolve_provider", lambda: SimpleNamespace(can_apply=lambda: True)
    )
    monkeypatch.setattr(update_provider, "apply_policy_update", _apply)
    return at_dispatch


def test_the_copy_lands_before_the_updater_and_ignores_the_interval(
    live_store, policy_updater, capsys
) -> None:
    """A copy from the scheduled sweep is not the copy an upgrade needs.

    The sweep's own guard SKIPS anything newer than ``MIN_BACKUP_INTERVAL_HOURS``, so
    without the bypass the update would run behind a copy up to a day old. The count is
    read inside the updater: a copy taken after it starts protects nothing.
    """
    db = resolve_store_path(DEFAULT_MEMORY_STORE)
    memory_backup.back_up_all_stores(keep=7)  # the scheduled sweep, minutes ago
    assert len(memory_backup.list_backups(db)) == 1

    cli_server._update()

    assert policy_updater == [2]
    out = capsys.readouterr().out
    assert "Memory snapshot: 1 store(s) copied" in out
    assert str(memory_backup.backup_dir_for(db)) in out


def test_a_failed_copy_aborts_and_never_reaches_the_updater(
    live_store, policy_updater, monkeypatch: pytest.MonkeyPatch, capsys
) -> None:
    """A store that could not be copied is the LAST one to upgrade around."""

    def _fails(path, **kw):
        raise memory_backup.MemoryBackupFailed("no space left on device")

    monkeypatch.setattr(memory_backup, "backup_store", _fails)

    with pytest.raises(SystemExit) as exc:
        cli_server._update()

    assert exc.value.code == 1
    assert policy_updater == []
    out = capsys.readouterr().out
    assert "Pre-update memory snapshot failed" in out
    assert "Not updating" in out


def test_a_store_the_sweep_never_visited_aborts_too(
    live_store, policy_updater, monkeypatch: pytest.MonkeyPatch, capsys
) -> None:
    """A store the sweep cannot resolve is one that got no copy, not one it skipped.

    ``owned_store_path`` answers ``None`` for an active name it cannot confirm, and the
    copy loop never sees that store at all — so before the count included it, the sweep
    reported `failed: 0` while a store holding data had no copy, and the update ran.
    """
    monkeypatch.setattr(memory_backup, "owned_store_path", lambda name: None)

    with pytest.raises(SystemExit) as exc:
        cli_server._update()

    assert exc.value.code == 1
    assert policy_updater == []
    assert "1 store(s) not copied" in capsys.readouterr().out


def test_a_copy_that_raises_aborts_the_same_way(
    live_store, policy_updater, monkeypatch: pytest.MonkeyPatch, capsys
) -> None:
    """Memory being mid-recovery raises out of the sweep instead of counting a failure."""

    def _unavailable(*a, **kw):
        raise RuntimeError("memory is being restored")

    monkeypatch.setattr(memory_backup, "back_up_all_stores", _unavailable)

    with pytest.raises(SystemExit) as exc:
        cli_server._update()

    assert exc.value.code == 1
    assert policy_updater == []
    assert "memory is being restored" in capsys.readouterr().out


def test_the_configured_retention_is_the_one_the_copy_prunes_to(
    live_store, policy_updater, monkeypatch: pytest.MonkeyPatch
) -> None:
    """An operator keeping 30 days of copies must not have them pruned back to 7.

    The default ``keep`` belongs to the sweep's own signature; every other caller passes
    ``memory.backup_keep``, and this one has to as well or the update silently deletes
    the history the operator configured.
    """
    from kiro_crew.config import KiroCrewConfig

    config = KiroCrewConfig.load()
    config.memory.backup_keep = 30
    config.save()

    seen: list[int] = []
    real = memory_backup.prune_backups
    monkeypatch.setattr(
        memory_backup,
        "prune_backups",
        lambda path, keep=7: (seen.append(keep), real(path, keep))[1],
    )

    cli_server._update()

    assert seen == [30]


def test_an_install_with_nothing_to_copy_still_updates(home: Path, policy_updater) -> None:
    """A store that was never opened has no bytes to lose, so it is not a reason to stop.

    Without this the very first `kirocrew update` on a fresh install would refuse.
    """
    cli_server._update()

    assert policy_updater == [0]


def test_a_path_that_updates_nothing_takes_no_copy(
    live_store, monkeypatch: pytest.MonkeyPatch, capsys
) -> None:
    """An install whose own packager owns the upgrade only prints guidance.

    The copy is taken where an updater runs, and the interval guard is bypassed for it,
    so copying here would spend a retention slot every time the command is run.
    """
    from kiro_crew.platform import update_provider

    monkeypatch.setattr(update_provider, "resolve_provider", lambda: None)
    monkeypatch.setattr(
        cli_server,
        "derive_capability",
        lambda **kw: SimpleNamespace(managed_by="docker", defers=True, unavailable_reason=""),
    )

    db = resolve_store_path(DEFAULT_MEMORY_STORE)
    before = memory_backup.list_backups(db)

    cli_server._update()

    assert memory_backup.list_backups(db) == before
    out = capsys.readouterr().out
    assert "managed externally" in out
    assert "Memory snapshot" not in out


def _serve_feed(monkeypatch: pytest.MonkeyPatch, version: str) -> None:
    """Pin the CDN bases, channel and installer command, and serve one release feed."""
    monkeypatch.setattr(
        "kiro_crew.platform.update_layout.cdn_bases",
        lambda: ("https://cdn.example.com", "https://cdn.example.com"),
    )
    monkeypatch.setattr("kiro_crew.platform.update_layout.release_channel", lambda: "stable")
    monkeypatch.setattr(
        "kiro_crew.platform.update_layout.wheel_update_command",
        lambda channel=None: "curl -fsSL https://cdn.example.com/cli.sh | sh",
    )
    monkeypatch.setattr(
        "kiro_crew.platform.update_governance.update_blocked_reason", lambda url: ""
    )
    feed = json.dumps(
        {
            "schema": "kirocrew-cli-artifact-manifest-v1",
            "channel": "stable",
            "version": version,
        }
    ).encode("utf-8")
    monkeypatch.setattr("urllib.request.urlopen", lambda *a, **k: _Resp(feed))


class _Resp:
    """The minimum urlopen answer `_update_wheel` reads: a context manager with read()."""

    def __init__(self, payload: bytes) -> None:
        self._payload = payload

    def __enter__(self) -> _Resp:
        return self

    def __exit__(self, *exc: object) -> None:
        return None

    def read(self, _n: int | None = None) -> bytes:
        return self._payload


def test_an_up_to_date_wheel_install_takes_no_copy(
    live_store, monkeypatch: pytest.MonkeyPatch, capsys
) -> None:
    """`_update_wheel` decides "already newest" itself, after the dispatch that called it.

    So the copy cannot sit at the wheel branch: it belongs past the version comparison,
    or a host running the command daily on an up-to-date install spends a slot a day.
    Driven through `_update`, so the branch that dispatches to the wheel updater is in
    the path and cannot be where the copy is taken.
    """
    from kiro_crew import __version__ as local_version
    from kiro_crew.platform import update_provider

    monkeypatch.setattr(update_provider, "resolve_provider", lambda: None)
    monkeypatch.setattr(
        cli_server,
        "derive_capability",
        lambda **kw: SimpleNamespace(managed_by="wheel", defers=False, unavailable_reason=""),
    )
    _serve_feed(monkeypatch, local_version)

    db = resolve_store_path(DEFAULT_MEMORY_STORE)
    before = memory_backup.list_backups(db)

    cli_server._update()

    assert memory_backup.list_backups(db) == before
    out = capsys.readouterr().out
    assert "Already on the latest version" in out
    assert "Memory snapshot" not in out


def test_a_provider_that_cannot_apply_here_takes_no_copy(
    live_store, monkeypatch: pytest.MonkeyPatch, capsys
) -> None:
    """A check-only provider reports versions; it never rewrites this install.

    Neither does any provider on Windows, where every provider command refuses — and
    `can_apply()` is the one answer that already covers both.
    """
    from kiro_crew.platform import update_provider

    async def _no_apply_command():
        return False  # what CommandProvider.apply() answers with no apply_command

    monkeypatch.setattr(
        update_provider,
        "resolve_provider",
        lambda: SimpleNamespace(can_apply=lambda: False, apply=_no_apply_command),
    )

    db = resolve_store_path(DEFAULT_MEMORY_STORE)
    before = memory_backup.list_backups(db)

    with pytest.raises(SystemExit) as exc:
        cli_server._update()

    assert exc.value.code == 1
    assert memory_backup.list_backups(db) == before
    out = capsys.readouterr().out
    assert "policy-defined update command failed" in out
    assert "Memory snapshot" not in out


def test_a_wheel_install_that_cannot_self_update_takes_no_copy(
    live_store, monkeypatch: pytest.MonkeyPatch, capsys
) -> None:
    """The installer is a POSIX shell script, so the wheel path refuses on Windows.

    That refusal sits below the version comparison, so a newer release is announced and
    then declined — and a copy taken above it would be spent on an update that cannot run.
    """
    from kiro_crew.platform.update_layout import InstallLayout

    _serve_feed(monkeypatch, "9999.0.0")
    monkeypatch.setattr("kiro_crew.platform.wheel_engine.running_from_managed_venv", lambda: False)
    monkeypatch.setattr(cli_server.sys, "platform", "win32")

    db = resolve_store_path(DEFAULT_MEMORY_STORE)
    before = memory_backup.list_backups(db)

    with pytest.raises(SystemExit) as exc:
        cli_server._update_wheel(
            InstallLayout(
                kind="wheel", proj="", is_git=False, is_externally_managed=False, guidance=""
            )
        )

    assert exc.value.code == 1
    assert memory_backup.list_backups(db) == before
    out = capsys.readouterr().out
    assert "not supported on Windows" in out
    assert "Memory snapshot" not in out
