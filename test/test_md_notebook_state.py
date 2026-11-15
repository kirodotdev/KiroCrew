"""The Notes builtin's one-time move of legacy state into its masked state directory.

An older build kept ``pat`` / ``vaults.json`` / ``settings.json`` at
``<crew-home>/workspace/md-notebook/<name>``. The live copies now sit inside the top-level
``md-notebook-staging`` directory, and :func:`md_notebook_state.migrate_legacy_state`
moves real state across, once. These tests pin what it moves, what it never moves, and
and that one pass seals promotion for good.
"""

from __future__ import annotations

import errno
import inspect
import os
from pathlib import Path

import pytest

import kiro_crew.sandbox as sb
from kiro_crew import md_notebook_state as mns

_POSIX_ONLY = pytest.mark.skipif(os.name != "posix", reason="POSIX link and mode semantics")


@pytest.fixture()
def dirs(tmp_path):
    crew = tmp_path / "crew"
    legacy = crew / "workspace" / "md-notebook"
    legacy.mkdir(parents=True)
    state = crew / mns.STATE_DIR_LEAF
    return state, legacy


def _stub(state: Path) -> None:
    """What the sandbox materialiser leaves before a spawn: the absent-equivalent stubs."""
    state.mkdir(parents=True, exist_ok=True)
    for name, content in mns.ABSENT_EQUIVALENT.items():
        (state / name).write_bytes(content)


class TestTheModuleAgreesWithTheSandbox:
    def test_the_state_directory_is_the_masked_one(self):
        assert mns.STATE_DIR_LEAF == sb._MD_NOTEBOOK_STAGING_LEAF
        assert mns.STATE_FILE_NAMES == sb._MD_NOTEBOOK_STATE_FILES

    def test_the_move_aside_suffix_is_the_lifecycle_s(self):
        from kiro_crew.apps import credential_masks

        assert mns.STAGED_DATA_SUFFIX == credential_masks.STAGED_DATA_SUFFIX

    def test_the_stub_bytes_are_the_materialisers(self):
        legacy = "/".join(mns.LEGACY_DIR_PARTS)
        expected = {}
        for name, content in mns.ABSENT_EQUIVALENT.items():
            expected[f"{sb._MD_NOTEBOOK_STAGING_LEAF}/{name}"] = content
            expected[f"{legacy}/{name}"] = content
        assert sb._MD_NOTEBOOK_PRECREATE_CONTENT == expected

    def test_the_legacy_names_are_the_retired_masked_ones(self):
        legacy = "/".join(mns.LEGACY_DIR_PARTS)
        assert {f"{legacy}/{n}" for n in mns.STATE_FILE_NAMES} == set(
            sb._MD_NOTEBOOK_RETIRED_STATE_LEAVES
        )
        assert set(sb._MD_NOTEBOOK_RETIRED_STATE_LEAVES) <= set(sb._CREW_HIDDEN_LEAVES)

    def test_the_module_pulls_in_no_app_code(self):
        """Spawn prep imports it, so it must not drag the apps package or aiohttp in."""
        import ast

        tree = ast.parse(inspect.getsource(mns))
        imported = set()
        for node in ast.walk(tree):
            if isinstance(node, ast.ImportFrom) and node.module:
                imported.add(node.module)
            elif isinstance(node, ast.Import):
                imported.update(alias.name for alias in node.names)
        assert imported, "the scan found no import at all"
        for module in imported:
            assert not module.startswith(("kiro_crew.apps", "aiohttp")), module


class TestTheFirstPass:
    def test_real_state_moves_over_the_stubs(self, dirs):
        state, legacy = dirs
        _stub(state)
        (legacy / "pat").write_text("ghp_real")
        (legacy / "vaults.json").write_text('[{"id": "v1"}]')
        (legacy / "settings.json").write_text('{"autoSync": true}')

        moved = mns.migrate_legacy_state(state, legacy)

        assert sorted(Path(p).name for p in moved) == ["pat", "settings.json", "vaults.json"]
        assert (state / "pat").read_text() == "ghp_real"
        assert (state / "vaults.json").read_text() == '[{"id": "v1"}]'
        assert (state / "settings.json").read_text() == '{"autoSync": true}'
        assert (legacy / "pat").read_bytes() == b"", "the legacy PAT was copied but not emptied"
        # The registry and settings hold no secret; they stay, masked, for a rollback.
        assert (legacy / "vaults.json").read_text() == '[{"id": "v1"}]'
        assert (legacy / "settings.json").read_text() == '{"autoSync": true}'
        assert (state / mns.MIGRATION_MARKER).is_file()
        if os.name == "posix":
            assert os.stat(state / "pat").st_mode & 0o077 == 0

    @_POSIX_ONLY
    def test_the_legacy_pat_is_emptied_in_place_not_unlinked(self, dirs):
        """A live namespace's bind mask sits on the inode; unlinking the name would
        detach that mask there and let an agent plant a link at the name."""
        state, legacy = dirs
        (legacy / "pat").write_text("ghp_real")
        inode = (legacy / "pat").stat().st_ino

        mns.migrate_legacy_state(state, legacy)

        assert (legacy / "pat").stat().st_ino == inode
        assert (legacy / "pat").read_bytes() == b""
        assert (state / "pat").read_text() == "ghp_real"

    def test_an_absent_destination_is_filled_too(self, dirs):
        state, legacy = dirs
        (legacy / "pat").write_text("ghp_real")

        mns.migrate_legacy_state(state, legacy)

        assert (state / "pat").read_text() == "ghp_real"

    def test_a_real_destination_wins(self, dirs):
        state, legacy = dirs
        _stub(state)
        (state / "pat").write_text("ghp_newer")
        (legacy / "pat").write_text("ghp_older")

        assert mns.migrate_legacy_state(state, legacy) == []

        assert (state / "pat").read_text() == "ghp_newer"
        assert (legacy / "pat").read_text() == "ghp_older", "the legacy copy stays, masked"

    def test_a_legacy_stub_is_not_worth_moving(self, dirs):
        state, legacy = dirs
        (legacy / "vaults.json").write_bytes(b"[]\n")

        assert mns.migrate_legacy_state(state, legacy) == []
        assert not (state / "vaults.json").exists()

    @_POSIX_ONLY
    def test_a_legacy_link_is_never_followed(self, dirs, tmp_path):
        state, legacy = dirs
        secret = tmp_path / "agent-chosen.json"
        secret.write_text('[{"id": "attacker"}]')
        (legacy / "vaults.json").symlink_to(secret)

        assert mns.migrate_legacy_state(state, legacy) == []
        assert not (state / "vaults.json").exists()

    @_POSIX_ONLY
    def test_a_linked_legacy_parent_is_refused(self, tmp_path):
        crew = tmp_path / "crew"
        victim = tmp_path / "victim"
        victim.mkdir()
        (victim / "settings.json").write_text('{"autoSync": true}')
        (crew / "workspace").mkdir(parents=True)
        (crew / "workspace" / "md-notebook").symlink_to(victim, target_is_directory=True)
        state = crew / mns.STATE_DIR_LEAF

        assert mns.migrate_legacy_state(state, crew / "workspace" / "md-notebook") == []
        assert not (state / "settings.json").exists()
        assert (victim / "settings.json").exists()
        # Sealed: a replacement for that link must never be promoted later.
        assert (state / mns.MIGRATION_MARKER).is_file()
        (crew / "workspace" / "md-notebook").unlink()
        planted = crew / "workspace" / "md-notebook"
        planted.mkdir()
        (planted / "settings.json").write_text('{"autoSync": true}')
        assert mns.migrate_legacy_state(state, planted) == []
        assert not (state / "settings.json").exists()

    def test_the_move_copies_so_another_filesystem_still_works(self, dirs, monkeypatch):
        """``workspace/`` can sit on another disk, where a rename across would fail with
        ``EXDEV``. No rename may take a legacy file as its source."""
        state, legacy = dirs
        (legacy / "pat").write_text("ghp_real")
        real_replace, real_rename = os.replace, os.rename

        def _guard(real):
            def _call(src, dst, *a, **k):
                if Path(src).parent == legacy:
                    raise OSError(errno.EXDEV, "Invalid cross-device link")
                return real(src, dst, *a, **k)

            return _call

        monkeypatch.setattr(os, "replace", _guard(real_replace))
        monkeypatch.setattr(os, "rename", _guard(real_rename))

        mns.migrate_legacy_state(state, legacy)

        assert (state / "pat").read_text() == "ghp_real"


class TestPromotionClosesAfterTheFirstPass:
    def test_a_pass_that_waited_on_the_lock_sees_the_seal(self, dirs, monkeypatch):
        """Two spawns can start a pass at once. One that waited while the other sealed
        must not copy legacy bytes over state the backend saved since."""
        state, legacy = dirs
        _stub(state)
        (legacy / "vaults.json").write_text('[{"id": "legacy"}]')
        real = mns.acquire_lock

        def _other_pass_seals_meanwhile(fd, **kwargs):
            (state / "vaults.json").write_text('[{"id": "saved-after-seal"}]')
            (state / mns.MIGRATION_MARKER).write_text("{}\n")
            return real(fd, **kwargs)

        monkeypatch.setattr(mns, "acquire_lock", _other_pass_seals_meanwhile)

        assert mns.migrate_legacy_state(state, legacy) == []
        assert (state / "vaults.json").read_text() == '[{"id": "saved-after-seal"}]'

    def test_the_seal_is_rechecked_under_the_lock_for_stub_equal_state(self, dirs, monkeypatch):
        """The user removed every vault after the other pass sealed, so the backend saved
        the stub bytes. Only the marker re-check under the lock keeps legacy bytes out."""
        state, legacy = dirs
        _stub(state)
        (legacy / "vaults.json").write_text('[{"id": "legacy"}]')
        real = mns.acquire_lock

        def _other_pass_seals_meanwhile(fd, **kwargs):
            (state / mns.MIGRATION_MARKER).write_text("{}\n")
            return real(fd, **kwargs)

        monkeypatch.setattr(mns, "acquire_lock", _other_pass_seals_meanwhile)

        assert mns.migrate_legacy_state(state, legacy) == []
        assert (state / "vaults.json").read_bytes() == mns.ABSENT_EQUIVALENT["vaults.json"]

    def test_a_hard_linked_legacy_pat_stops_the_pass_unsealed(self, dirs, tmp_path):
        """A second name on the PAT would keep the token out of the hard-link check's
        sight after a copy, so it is not moved; the registry still is."""
        state, legacy = dirs
        _stub(state)
        (legacy / "pat").write_text("ghp_real")
        os.link(legacy / "pat", tmp_path / "second-name")
        (legacy / "vaults.json").write_text('[{"id": "v1"}]')

        with pytest.raises(mns.SecondNameError):
            mns.migrate_legacy_state(state, legacy)

        assert (state / "pat").read_bytes() == mns.ABSENT_EQUIVALENT["pat"]
        assert (legacy / "pat").read_text() == "ghp_real"
        assert not (state / mns.MIGRATION_MARKER).exists()

        (tmp_path / "second-name").unlink()
        mns.migrate_legacy_state(state, legacy)
        assert (state / "pat").read_text() == "ghp_real"
        assert (state / mns.MIGRATION_MARKER).is_file()

    def test_the_legacy_pat_is_kept_when_the_copy_is_not_durable(self, dirs, monkeypatch):
        """The source goes only after the state directory is synced, so a power loss
        cannot lose both copies."""
        state, legacy = dirs
        (legacy / "pat").write_text("ghp_real")
        monkeypatch.setattr(mns, "fsync_dir", lambda *a, **k: (_ for _ in ()).throw(OSError("EIO")))

        mns.migrate_legacy_state(state, legacy)

        assert (state / "pat").read_text() == "ghp_real"
        assert (legacy / "pat").read_text() == "ghp_real"

    def test_a_bare_file_written_later_is_never_promoted(self, dirs):
        """The bare names sit in an agent-writable directory. After the first pass a
        settings file dropped there must never reach the fenced state directory."""
        state, legacy = dirs
        _stub(state)
        mns.migrate_legacy_state(state, legacy)
        (legacy / "settings.json").write_text('{"autoSync": true}')

        assert mns.migrate_legacy_state(state, legacy) == []
        assert (state / "settings.json").read_bytes() == b"{}\n"

    def test_a_failed_copy_is_retried_by_the_next_pass(self, dirs, monkeypatch):
        """A copy that fails leaves the pass unsealed, so the user's registry is not
        hidden for good by one full-disk moment."""
        state, legacy = dirs
        _stub(state)
        (legacy / "vaults.json").write_text('[{"id": "v1"}]')
        real = mns.atomic_write

        def _fail_state_file(path, *a, **k):
            if Path(path).name == "vaults.json":
                raise OSError("disk full")
            return real(path, *a, **k)

        monkeypatch.setattr(mns, "atomic_write", _fail_state_file)
        with pytest.raises(OSError, match="vaults.json"):
            mns.migrate_legacy_state(state, legacy)
        assert not (state / mns.MIGRATION_MARKER).exists()

        monkeypatch.setattr(mns, "atomic_write", real)
        mns.migrate_legacy_state(state, legacy)
        assert (state / "vaults.json").read_text() == '[{"id": "v1"}]'
        assert (state / mns.MIGRATION_MARKER).is_file()

    def test_a_source_that_cannot_be_read_leaves_the_pass_unsealed(self, dirs, monkeypatch):
        """A registry that answers EACCES for a moment is not absent: sealing over it
        would hide the user's vaults for good."""
        state, legacy = dirs
        (legacy / "vaults.json").write_text('[{"id": "v1"}]')
        real_open = os.open

        def _denied(path, *a, **k):
            if Path(path) == legacy / "vaults.json":
                raise PermissionError(errno.EACCES, "Permission denied")
            return real_open(path, *a, **k)

        monkeypatch.setattr(os, "open", _denied)
        with pytest.raises(OSError, match="vaults.json"):
            mns.migrate_legacy_state(state, legacy)
        assert not (state / mns.MIGRATION_MARKER).exists()

        monkeypatch.setattr(os, "open", real_open)
        mns.migrate_legacy_state(state, legacy)
        assert (state / "vaults.json").read_text() == '[{"id": "v1"}]'

    def test_a_marker_that_cannot_be_written_leaves_promotion_open_for_a_retry(
        self, dirs, monkeypatch
    ):
        """The marker is what closes promotion, so a pass that cannot seal is not
        treated as sealed: the next spawn runs the pass again."""
        state, legacy = dirs
        real = mns.atomic_write

        def _fail_marker(path, *a, **k):
            if Path(path).name == mns.MIGRATION_MARKER:
                raise OSError("disk full")
            return real(path, *a, **k)

        monkeypatch.setattr(mns, "atomic_write", _fail_marker)

        assert mns.sealed_default_legacy_state() is False
        with pytest.raises(OSError):
            mns.migrate_legacy_state(state, legacy)
        assert not (state / mns.MIGRATION_MARKER).exists()

    def test_the_backend_spawn_refuses_while_unsealed(self):
        """No backend write can race the move: the backend does not start until the
        pass is sealed (spawn prep refuses the same way on sandboxed hosts)."""
        from kiro_crew.apps import backend as backend_mod

        source = inspect.getsource(backend_mod._start_app_backend_body)
        guard = source.index("if not _md_notebook_state.sealed_default_legacy_state():")
        assert "return None" in source[guard : guard + 400]


class TestTheMoveRunsBeforeAnySandboxLaunches:
    def test_both_launch_paths_run_it(self):
        for fn in (sb.namespace_argv, sb.sandbox_exec_argv):
            assert "_migrate_md_notebook_legacy_state()" in inspect.getsource(fn), fn.__name__

    def test_the_notes_backend_spawn_runs_it(self):
        """A host with no sandbox wrap (Windows) reaches the backend without spawn prep."""
        from kiro_crew.apps import backend as backend_mod

        source = inspect.getsource(backend_mod._start_app_backend_body)
        assert "sealed_default_legacy_state()" in source

    @_POSIX_ONLY
    def test_a_namespace_spawn_moves_bare_state_first(self, tmp_path, monkeypatch):
        home = tmp_path / ".kiro" / "crew"
        legacy = home / "workspace" / "md-notebook"
        legacy.mkdir(parents=True)
        (legacy / "pat").write_text("ghp_real")
        monkeypatch.setattr(sb, "config_dir", lambda: home)
        monkeypatch.setattr(sb.Path, "home", staticmethod(lambda: tmp_path))
        monkeypatch.setenv("KIROCREW_HOME", str(home))
        monkeypatch.setattr("kiro_crew.sandbox._ssh_supports_accept_new", lambda: True)

        sb.namespace_argv(["/bin/true"])

        # Into ``md-notebook-staging/``, which every build masks, and no further: the move
        # into the app's own directory waits for the Notes backend's start, which checks
        # that no sandbox of an earlier build is alive.
        assert (home / mns.STATE_DIR_LEAF / "pat").read_text() == "ghp_real"
        assert (home / mns.STATE_DIR_LEAF / mns.MIGRATION_MARKER).exists()
        assert not (home / mns.STATE_DIR_LEAF / mns.APP_MOVE_MARKER).exists()
        # Its destination is created empty, so the sandbox about to start masks it.
        assert sorted(os.listdir(home.joinpath(*mns.APP_STATE_PARTS))) == []
        # The name it left now holds the stub its mask mounts on.
        assert (legacy / "pat").read_bytes() == b""

    @_POSIX_ONLY
    def test_a_failed_move_does_not_stop_the_spawn(self, tmp_path, monkeypatch, caplog):
        """One optional app's state move must not refuse every chat, cron and subagent
        spawn. The spawn goes on unsealed, every bare name still gets its stub (so its
        mask mounts), and the Notes backend stays off until a later pass seals."""
        home = tmp_path / ".kiro" / "crew"
        legacy = home / "workspace" / "md-notebook"
        legacy.mkdir(parents=True)
        (legacy / "vaults.json").write_text('[{"id": "v1"}]')
        monkeypatch.setattr(sb, "config_dir", lambda: home)
        monkeypatch.setattr(sb.Path, "home", staticmethod(lambda: tmp_path))
        monkeypatch.setenv("KIROCREW_HOME", str(home))
        monkeypatch.setattr("kiro_crew.sandbox._ssh_supports_accept_new", lambda: True)
        real = mns.atomic_write

        def _disk_full(path, *a, **k):
            if Path(path).name == "vaults.json":
                raise OSError(errno.ENOSPC, "No space left on device")
            return real(path, *a, **k)

        monkeypatch.setattr(mns, "atomic_write", _disk_full)

        with caplog.at_level("WARNING"):
            argv = sb.namespace_argv(["/bin/true"])

        assert argv
        assert "did not finish" in caplog.text
        state = home / mns.STATE_DIR_LEAF
        assert not (state / mns.MIGRATION_MARKER).exists()
        for name in mns.STATE_FILE_NAMES:
            assert (legacy / name).is_file(), name
        assert (legacy / "vaults.json").read_text() == '[{"id": "v1"}]'
        monkeypatch.setattr(mns, "default_state_dir", lambda: state)
        monkeypatch.setattr(mns, "default_legacy_dir", lambda: legacy)
        assert mns.sealed_default_legacy_state() is False

        # The next spawn, with the disk freed, moves it and seals.
        monkeypatch.setattr(mns, "atomic_write", real)
        sb.namespace_argv(["/bin/true"])
        assert (state / "vaults.json").read_text() == '[{"id": "v1"}]'
        assert (state / mns.MIGRATION_MARKER).is_file()


@pytest.fixture()
def app_dirs(tmp_path):
    """A sealed ``md-notebook-staging`` holding real state, and the app's data dir."""
    home = tmp_path / "crew"
    state = home / mns.STATE_DIR_LEAF
    state.mkdir(parents=True)
    (state / mns.MIGRATION_MARKER).write_text("{}\n")
    (state / "pat").write_text("ghp_real")
    (state / "vaults.json").write_text('[{"id": "v1"}]\n')
    return state, home.joinpath(*mns.APP_STATE_PARTS)


@_POSIX_ONLY
class TestSpawnPrepMakesTheAppStateDir:
    """``ensure_app_state_dir``: the empty ``data/state/`` every spawn masks until the move."""

    @pytest.fixture()
    def app_state(self, tmp_path):
        return tmp_path.joinpath("crew", *mns.APP_STATE_PARTS)

    def test_with_no_data_dir_the_chain_is_made_owner_only(self, app_state):
        mns.ensure_app_state_dir(app_state)
        for made in (app_state, app_state.parent, app_state.parent.parent):
            assert oct(made.stat().st_mode & 0o777) == oct(0o700), made

    def test_with_a_data_dir_only_state_is_made(self, app_state):
        app_state.parent.mkdir(parents=True)
        mns.ensure_app_state_dir(app_state)
        assert app_state.is_dir()
        mns.ensure_app_state_dir(app_state)

    def test_nothing_is_made_while_the_lifecycle_holds_the_data_aside(self, app_state):
        """The restore renames the kept tree to ``data/``; a ``data/`` made now would take
        it in, nested where no mask reaches."""
        aside = app_state.parent.parent.with_name(".md-notebook" + mns.STAGED_DATA_SUFFIX)
        aside.mkdir(parents=True)
        mns.ensure_app_state_dir(app_state)
        assert not app_state.parent.parent.exists()

    def test_a_data_dir_moved_aside_after_the_look_makes_nothing(self, app_state, monkeypatch):
        data = app_state.parent
        data.mkdir(parents=True)
        aside = data.parent.with_name(".md-notebook" + mns.STAGED_DATA_SUFFIX)
        real_mkdir = os.mkdir

        def _moved_first(path, *args, **kwargs):
            if Path(path) == app_state and data.exists():
                data.rename(aside)
            return real_mkdir(path, *args, **kwargs)

        monkeypatch.setattr(mns.os, "mkdir", _moved_first)
        mns.ensure_app_state_dir(app_state)
        assert not data.exists() and not (aside / "state").exists()


@_POSIX_ONLY
class TestTheMoveIntoTheAppDataDir:
    """The state leaves ``md-notebook-staging`` for the directory the app's manifest masks."""

    def test_every_file_is_copied_and_the_old_pat_emptied(self, app_dirs):
        state, app_state = app_dirs
        written = mns.move_state_to_app_data(app_state, state)

        assert sorted(Path(p).name for p in written) == sorted(mns.STATE_FILE_NAMES)
        assert (app_state / "pat").read_text() == "ghp_real"
        assert (app_state / "vaults.json").read_text() == '[{"id": "v1"}]\n'
        # A file the old directory never had arrives as its absent-equivalent.
        assert (app_state / "settings.json").read_bytes() == mns.ABSENT_EQUIVALENT["settings.json"]
        assert (state / "pat").read_bytes() == b"", "a second copy of the token was kept"
        # The registry stays, so a rollback keeps the vaults.
        assert (state / "vaults.json").read_text() == '[{"id": "v1"}]\n'
        assert (state / mns.APP_MOVE_MARKER).is_file()
        assert oct(app_state.stat().st_mode & 0o777) == oct(0o700)

    def test_it_runs_once(self, app_dirs):
        state, app_state = app_dirs
        mns.move_state_to_app_data(app_state, state)
        (app_state / "vaults.json").write_text('[{"id": "saved-later"}]\n')
        (state / "vaults.json").write_text('[{"id": "stale"}]\n')

        assert mns.move_state_to_app_data(app_state, state) == []
        assert (app_state / "vaults.json").read_text() == '[{"id": "saved-later"}]\n'

    def test_a_file_planted_in_the_app_dir_before_it_was_masked_is_replaced(self, app_dirs):
        state, app_state = app_dirs
        app_state.mkdir(parents=True)
        (app_state / "vaults.json").write_text('[{"id": "v1", "remote": "attacker"}]\n')
        (app_state / "settings.json").write_text('{"autoSync": true}\n')
        (app_state / mns.MIGRATION_MARKER).write_text("{}\n")

        mns.move_state_to_app_data(app_state, state)

        assert (app_state / "vaults.json").read_text() == '[{"id": "v1"}]\n'
        assert (app_state / "settings.json").read_bytes() == mns.ABSENT_EQUIVALENT["settings.json"]

    def test_it_waits_for_the_first_move_to_seal(self, app_dirs):
        state, app_state = app_dirs
        (state / mns.MIGRATION_MARKER).unlink()
        with pytest.raises(OSError, match="not sealed"):
            mns.move_state_to_app_data(app_state, state)
        assert not (app_state / "pat").exists()
        assert (state / "pat").read_text() == "ghp_real"

    @pytest.mark.skipif(not hasattr(os, "link"), reason="needs hard links")
    def test_a_pat_with_a_second_name_stops_it_unsealed(self, app_dirs, tmp_path):
        state, app_state = app_dirs
        os.link(state / "pat", tmp_path / "second-name")
        with pytest.raises(mns.SecondNameError):
            mns.move_state_to_app_data(app_state, state)
        assert not (state / mns.APP_MOVE_MARKER).exists()
        assert (state / "pat").read_text() == "ghp_real"

    def test_a_pass_that_stops_before_its_seal_keeps_the_old_pat_for_the_retry(
        self, app_dirs, monkeypatch
    ):
        """The seal is written before the old PAT is emptied, so no retry copies an empty one."""
        state, app_state = app_dirs
        real_write = mns.atomic_write

        def _seal_fails(path, *args, **kwargs):
            if Path(path).name == mns.APP_MOVE_MARKER:
                raise OSError("disk full")
            return real_write(path, *args, **kwargs)

        with monkeypatch.context() as patch:
            patch.setattr(mns, "atomic_write", _seal_fails)
            with pytest.raises(OSError, match="disk full"):
                mns.move_state_to_app_data(app_state, state)
        assert (state / "pat").read_text() == "ghp_real"
        assert not (state / mns.APP_MOVE_MARKER).exists()

        mns.move_state_to_app_data(app_state, state)
        assert (app_state / "pat").read_text() == "ghp_real"
        assert (state / "pat").read_bytes() == b""

    def test_a_sealed_pass_empties_an_old_pat_it_could_not_empty_before(
        self, app_dirs, monkeypatch
    ):
        state, app_state = app_dirs
        with monkeypatch.context() as patch:
            patch.setattr(
                mns, "_empty_in_place", lambda path: (_ for _ in ()).throw(OSError("busy"))
            )
            mns.move_state_to_app_data(app_state, state)
        assert (state / mns.APP_MOVE_MARKER).is_file(), "the copy is final once it is sealed"
        assert (state / "pat").read_text() == "ghp_real"

        assert mns.move_state_to_app_data(app_state, state) == []
        assert (state / "pat").read_bytes() == b""
        assert (app_state / "pat").read_text() == "ghp_real"

    @pytest.mark.parametrize("pass_", ["first", "sealed-retry"])
    def test_every_name_is_synced_before_the_old_pat_is_emptied(self, app_dirs, monkeypatch, pass_):
        """A power loss after the PAT is emptied must not come back without the copy's
        names or the seal: a lost marker would copy the emptied PAT over the moved one."""
        state, app_state = app_dirs
        home = state.parent
        if pass_ == "sealed-retry":
            with monkeypatch.context() as patch:
                patch.setattr(
                    mns, "_empty_in_place", lambda path: (_ for _ in ()).throw(OSError("busy"))
                )
                mns.move_state_to_app_data(app_state, state)
        events: list[tuple[str, Path]] = []
        real_sync, real_write, real_empty = mns.fsync_dir, mns.atomic_write, mns._empty_in_place

        def _sync(path, *args, **kwargs):
            events.append(("sync", Path(path)))
            return real_sync(path, *args, **kwargs)

        def _write(path, *args, **kwargs):
            events.append(("write", Path(path)))
            return real_write(path, *args, **kwargs)

        def _empty(path):
            events.append(("empty", Path(path)))
            return real_empty(path)

        monkeypatch.setattr(mns, "fsync_dir", _sync)
        monkeypatch.setattr(mns, "atomic_write", _write)
        monkeypatch.setattr(mns, "_empty_in_place", _empty)
        mns.move_state_to_app_data(app_state, state)

        emptied = events.index(("empty", state / "pat"))
        synced = {path for kind, path in events[:emptied] if kind == "sync"}
        chain = {app_state, *(p for p in app_state.parents if p == home or home in p.parents)}
        assert chain == {app_state, app_state.parent, app_state.parent.parent, home / "apps", home}
        assert chain | {state} <= synced
        if pass_ == "first":
            seal = events.index(("write", state / mns.APP_MOVE_MARKER))
            assert ("sync", state) in events[seal:emptied], "the seal's own name is synced"
            assert chain <= {p for kind, p in events[:seal] if kind == "sync"}
        assert (state / "pat").read_bytes() == b""

    def test_a_seal_that_cannot_be_synced_keeps_the_old_pat_for_the_next_pass(
        self, app_dirs, monkeypatch
    ):
        state, app_state = app_dirs
        real_sync = mns.fsync_dir

        def _sync(path, *args, **kwargs):
            if Path(path) == state:
                raise OSError("EIO")
            return real_sync(path, *args, **kwargs)

        with monkeypatch.context() as patch:
            patch.setattr(mns, "fsync_dir", _sync)
            with pytest.raises(OSError, match="EIO"):
                mns.move_state_to_app_data(app_state, state)
        assert (state / "pat").read_text() == "ghp_real"

        mns.move_state_to_app_data(app_state, state)
        assert (state / "pat").read_bytes() == b""
        assert (app_state / "pat").read_text() == "ghp_real"

    @pytest.mark.parametrize("pass_", ["first", "sealed-retry", "sealed"])
    def test_the_seal_s_name_is_synced_with_no_pat_to_empty(self, app_dirs, monkeypatch, pass_):
        """With no token to move nothing is emptied, yet the marker's entry is still synced
        before the pass returns: the backend writes over the copies once it does."""
        state, app_state = app_dirs
        (state / "pat").write_bytes(b"")
        if pass_ != "first":
            mns.move_state_to_app_data(app_state, state)
        if pass_ == "sealed-retry":
            # Still holding bytes, so the sealed pass takes the lock.
            (state / "pat").write_text("ghp_left")
        events: list[tuple[str, Path]] = []
        real_sync, real_write = mns.fsync_dir, mns.atomic_write

        def _sync(path, *args, **kwargs):
            events.append(("sync", Path(path)))
            return real_sync(path, *args, **kwargs)

        def _write(path, *args, **kwargs):
            events.append(("write", Path(path)))
            return real_write(path, *args, **kwargs)

        monkeypatch.setattr(mns, "fsync_dir", _sync)
        monkeypatch.setattr(mns, "atomic_write", _write)
        mns.move_state_to_app_data(app_state, state)

        start = events.index(("write", state / mns.APP_MOVE_MARKER)) if pass_ == "first" else 0
        assert ("sync", state) in events[start:], "the seal's name was never synced"

    def test_the_backend_gate_refuses_a_seal_it_cannot_sync(self, tmp_path, monkeypatch):
        """A marker that merely exists is not durable, so it does not start the backend."""
        home = tmp_path / "crew"
        state = home / mns.STATE_DIR_LEAF
        monkeypatch.setattr(mns, "_crew_home", lambda: home)
        state.mkdir(parents=True)
        for marker in (mns.MIGRATION_MARKER, mns.APP_MOVE_MARKER):
            (state / marker).write_text("{}\n")
        real_sync = mns.fsync_dir

        def _sync(path, *args, **kwargs):
            if Path(path) == state:
                raise OSError("EIO")
            return real_sync(path, *args, **kwargs)

        with monkeypatch.context() as patch:
            patch.setattr(mns, "fsync_dir", _sync)
            assert mns.sealed_default_legacy_state() is False
        assert mns.sealed_default_legacy_state() is True

    def test_it_waits_while_the_lifecycle_holds_the_data_aside(self, app_dirs):
        """A ``data/`` made now would take the restore's rename into it."""
        state, app_state = app_dirs
        aside = app_state.parent.parent.with_name(".md-notebook" + mns.STAGED_DATA_SUFFIX)
        aside.mkdir(parents=True)
        with pytest.raises(OSError, match="moved aside"):
            mns.move_state_to_app_data(app_state, state)
        assert not app_state.parent.exists()
        assert not (state / mns.APP_MOVE_MARKER).exists()
        assert (state / "pat").read_text() == "ghp_real"

    def test_every_directory_the_move_creates_is_owner_only(self, app_dirs):
        state, app_state = app_dirs
        mns.move_state_to_app_data(app_state, state)
        for created in (state.parent / "apps", app_state.parent.parent, app_state.parent):
            assert oct(created.stat().st_mode & 0o777) == oct(0o700), created

    def test_the_backend_gate_needs_both_moves(self, tmp_path, monkeypatch):
        home = tmp_path / "crew"
        monkeypatch.setattr(mns, "_crew_home", lambda: home)
        monkeypatch.setattr(mns, "older_sandboxes_masking_only_the_old_dir", lambda: [])
        (home / "workspace" / "md-notebook").mkdir(parents=True)
        (home / "workspace" / "md-notebook" / "pat").write_text("ghp_oldest")

        assert mns.sealed_default_legacy_state() is True
        assert (home.joinpath(*mns.APP_STATE_PARTS) / "pat").read_text() == "ghp_oldest"

    @pytest.mark.parametrize("older", [[4242], None], ids=["named", "unreadable"])
    def test_the_move_waits_while_an_older_sandbox_may_run(
        self, tmp_path, monkeypatch, caplog, older
    ):
        """A sandbox of an earlier build masks the old directory and not the new one, so
        while one may be alive the token stays where it is masked and Notes stays off."""
        home = tmp_path / "crew"
        monkeypatch.setattr(mns, "_crew_home", lambda: home)
        live = {"older": older}
        monkeypatch.setattr(mns, "older_sandboxes_masking_only_the_old_dir", lambda: live["older"])
        (home / mns.STATE_DIR_LEAF).mkdir(parents=True)
        (home / mns.STATE_DIR_LEAF / mns.MIGRATION_MARKER).write_text("{}\n")
        (home / mns.STATE_DIR_LEAF / "pat").write_text("ghp_real")
        app_state = home.joinpath(*mns.APP_STATE_PARTS)

        with caplog.at_level("WARNING"):
            assert mns.sealed_default_legacy_state() is False
        assert "not moving the Notes state" in caplog.text
        assert ("pid 4242" in caplog.text) is (older is not None)
        assert not (app_state / "pat").exists()
        assert (home / mns.STATE_DIR_LEAF / "pat").read_text() == "ghp_real"
        assert not (home / mns.STATE_DIR_LEAF / mns.APP_MOVE_MARKER).exists()

        # Once none is left, the next start moves it.
        live["older"] = []
        assert mns.sealed_default_legacy_state() is True
        assert (app_state / "pat").read_text() == "ghp_real"

    def test_a_sealed_move_does_not_read_the_process_table(self, tmp_path, monkeypatch):
        home = tmp_path / "crew"
        monkeypatch.setattr(mns, "_crew_home", lambda: home)
        (home / mns.STATE_DIR_LEAF).mkdir(parents=True)
        for marker in (mns.MIGRATION_MARKER, mns.APP_MOVE_MARKER):
            (home / mns.STATE_DIR_LEAF / marker).write_text("{}\n")

        def _must_not_scan():
            raise AssertionError("scanned after the move sealed")

        monkeypatch.setattr(mns, "older_sandboxes_masking_only_the_old_dir", _must_not_scan)
        assert mns.sealed_default_legacy_state() is True

    def test_the_backend_reads_its_state_from_the_app_data_dir(self, tmp_path, monkeypatch):
        from kiro_crew.apps.builtins.md_notebook import server_store

        monkeypatch.setattr(server_store, "_HOME", None)
        monkeypatch.setattr(mns, "_crew_home", lambda: tmp_path / "crew")
        assert server_store._staging_dir() == tmp_path / "crew" / "apps/md-notebook/data/state"
