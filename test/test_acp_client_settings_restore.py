"""Tests for ACP client: the moved-aside settings entry: no-clobber restore, the Windows parent pin
and the create-race loser poll.
"""

import asyncio
import json
import os
import threading
import types
from pathlib import Path

import pytest
from acp_client_helpers import _native_projection_for_fake_processes  # noqa: F401 - fixture
from acp_client_helpers import _pin_the_installed_kiro_cli_version  # noqa: F401 - fixture

import kiro_crew.acp.client as acp_client
from conftest import requires_symlinks
from kiro_crew.acp.client import AcpClient
from kiro_crew.acp.types import ACP_BACKEND_CLAUDE
from kiro_crew.testing.clock import ManualClock


class TestMovedAsideRestore:
    """Putting a moved-aside settings entry back is a no-clobber rename of ANY entry.

    Teardown and re-seed capture whatever sits at ``settings.local.json`` with an
    atomic move-aside, verify the moved bytes, and put a mismatch back. That
    put-back is a same-directory no-clobber rename: a symlink or an oversized file
    the user placed at the path is restored exactly as it is, with no copy and no
    size cap, and whatever raced into the vacated pathname is never overwritten.
    """

    _SERVED = ["global.anthropic.claude-opus-5[1m]"]

    @pytest.fixture(autouse=True)
    # nosemgrep: kirocrew.test-autouse-shared-monkeypatch -- no test in this module calls monkeypatch.undo()
    def _isolated_provenance(self, monkeypatch):
        """Per-test provenance registries and a warm advertised-model cache."""
        from kiro_crew import model_registry
        from kiro_crew.acp import seed_provenance

        monkeypatch.setattr(seed_provenance, "_RECORDS", {})
        monkeypatch.setattr(seed_provenance, "_LIVE", {})
        monkeypatch.setattr(seed_provenance, "_SHARERS", {})
        monkeypatch.setattr(
            model_registry, "_ADVERTISED_MODELS", {"claude_code": list(self._SERVED)}
        )

    @staticmethod
    def _authored(tmp_path: Path) -> tuple[AcpClient, Path]:
        """A client that has written Crew's seed, and the path it wrote."""
        client = AcpClient(
            work_dir=tmp_path, acp_backend=ACP_BACKEND_CLAUDE, permission_mode="default"
        )
        client._write_claude_local_settings()
        return client, tmp_path / ".claude" / "settings.local.json"

    @staticmethod
    def _teardown(client: AcpClient) -> None:
        """The discard-then-reset pair every production caller runs."""
        asyncio.run(client._discard_claude_settings_seed())
        client._reset_state()

    @staticmethod
    def _racer_lands_after_the_move(monkeypatch, path: Path, racer: str) -> None:
        """Land *racer* at *path* inside the move-aside window.

        ``_settings_path_holds`` runs on the moved entry between the move and the
        restore, so a write from its wrapper is a file that arrives while the
        pathname is vacant -- the race the no-clobber restore exists for.
        """
        real_holds = AcpClient._settings_path_holds

        def verify_then_a_racer_lands(candidate: Path, expectation) -> bool:
            held = real_holds(candidate, expectation)
            if candidate.name.endswith(".crew-gc"):
                path.write_text(racer, encoding="utf-8")
            return held

        monkeypatch.setattr(
            AcpClient, "_settings_path_holds", staticmethod(verify_then_a_racer_lands)
        )

    @staticmethod
    def _force_the_copy_fallback(monkeypatch) -> list[dict]:
        """Take the no-clobber rename primitive away and record every copy fallback call."""
        monkeypatch.setattr(acp_client.platform_compat, "RENAME_NOREPLACE_AVAILABLE", False)
        calls: list[dict] = []
        real_put_back = acp_client.pinned_fs.put_back_no_clobber

        def counting_put_back(*args, **kwargs):
            calls.append(dict(kwargs))
            return real_put_back(*args, **kwargs)

        monkeypatch.setattr(acp_client.pinned_fs, "put_back_no_clobber", counting_put_back)
        return calls

    @requires_symlinks
    def test_a_symlink_user_replacement_is_restored_not_stranded(self, tmp_path):
        """A dotfiles-style symlink at the settings path comes back as that symlink.

        The move-aside captures the link itself; the verifying open refuses to
        follow it, so the entry reads as not Crew's and must go back. A restore
        that reads bytes would refuse the link and leave the user's settings
        stranded under a ``.crew-gc`` name with the pathname vacant.
        """
        client, path = self._authored(tmp_path)
        target = tmp_path / "dotfiles-settings.json"
        target.write_text('{"permissions": {"defaultMode": "acceptEdits"}}', encoding="utf-8")
        path.unlink()
        path.symlink_to(target)

        self._teardown(client)

        assert path.is_symlink()
        assert path.resolve() == target.resolve()
        assert not list(path.parent.glob("*.crew-gc"))

    def test_a_large_user_replacement_is_restored(self, tmp_path):
        """A user file over a mebibyte is restored whole, not refused on size."""
        client, path = self._authored(tmp_path)
        large = b'{"permissions": {"allow": ["' + b"x" * (1 << 20) + b'"]}}'
        path.write_bytes(large)

        self._teardown(client)

        assert path.read_bytes() == large
        assert not list(path.parent.glob("*.crew-gc"))

    def test_a_raced_in_occupant_is_not_clobbered_by_the_restore(self, tmp_path, monkeypatch):
        """Positive control: a file recreated in the aside window survives the restore.

        The occupant is newer than the moved entry, so the rename refuses it and
        the moved entry stays recoverable beside it as ``.crew-gc`` litter.
        """
        client, path = self._authored(tmp_path)
        users_file = '{"permissions": {"defaultMode": "acceptEdits"}}'
        path.write_text(users_file, encoding="utf-8")
        racer = '{"permissions": {"allow": ["Bash(ls)"]}}'
        self._racer_lands_after_the_move(monkeypatch, path, racer)

        self._teardown(client)

        assert path.read_text(encoding="utf-8") == racer
        litter = list(path.parent.glob("*.crew-gc"))
        assert len(litter) == 1
        assert litter[0].read_text(encoding="utf-8") == users_file

    def test_crew_own_seed_restore_still_round_trips(self, tmp_path, monkeypatch):
        """Positive control: a refused durable revoke puts Crew's own seed back cleanly."""
        from kiro_crew.acp import seed_provenance

        client, path = self._authored(tmp_path)
        before = path.read_bytes()
        monkeypatch.setattr(seed_provenance, "forget", lambda _path, _owner: False)

        self._teardown(client)

        assert path.read_bytes() == before
        assert not list(path.parent.glob("*.crew-gc"))

    def test_restore_falls_back_when_rename_noreplace_is_unavailable(self, tmp_path, monkeypatch):
        """Crew's seed round-trips through copy fallback or Windows rename.

        The POSIX fallback is the validated byte copy, called with no size cap.
        """
        from kiro_crew.acp import seed_provenance

        calls = self._force_the_copy_fallback(monkeypatch)
        client, path = self._authored(tmp_path)
        before = path.read_bytes()
        monkeypatch.setattr(seed_provenance, "forget", lambda _path, _owner: False)

        self._teardown(client)

        assert path.read_bytes() == before
        assert not list(path.parent.glob("*.crew-gc"))
        expected_calls = 0 if acp_client.platform_compat.IS_WINDOWS else 1
        assert len(calls) == expected_calls
        if not acp_client.platform_compat.IS_WINDOWS:
            assert "max_bytes" not in calls[0]

    def test_the_copy_fallback_is_still_no_clobber(self, tmp_path, monkeypatch):
        """A raced-in occupant survives POSIX fallback and Windows rename."""
        calls = self._force_the_copy_fallback(monkeypatch)
        client, path = self._authored(tmp_path)
        users_file = '{"permissions": {"defaultMode": "acceptEdits"}}'
        path.write_text(users_file, encoding="utf-8")
        racer = '{"permissions": {"allow": ["Bash(ls)"]}}'
        self._racer_lands_after_the_move(monkeypatch, path, racer)

        self._teardown(client)

        assert path.read_text(encoding="utf-8") == racer
        litter = list(path.parent.glob("*.crew-gc"))
        assert len(litter) == 1
        assert litter[0].read_text(encoding="utf-8") == users_file
        assert len(calls) == (0 if acp_client.platform_compat.IS_WINDOWS else 1)

    def test_an_unopenable_parent_keeps_the_aside_and_copies_nothing_by_name(
        self, tmp_path, monkeypatch
    ):
        """An unopenable or unpinnable parent leaves the aside as litter.

        POSIX requires a parent descriptor; Windows requires a parent pin. A
        refusal fails closed without a by-name copy on either platform.
        """
        monkeypatch.setattr(acp_client.platform_compat, "RENAME_NOREPLACE_AVAILABLE", False)
        parent = tmp_path / ".claude"
        parent.mkdir()
        path = parent / "settings.local.json"
        aside = parent / "settings.local.json.0123456789abcdef.crew-gc"
        aside.write_bytes(b'{"permissions": {"defaultMode": "default"}}')
        moved = aside.stat()
        expect = (moved.st_dev, moved.st_ino)
        real_open = os.open
        dir_flags = acp_client.pinned_fs.dir_flags()

        def refuse_the_parent(p, flags, *args, **kwargs):
            if os.fspath(p) == os.fspath(parent) and flags == dir_flags:
                raise PermissionError(13, "parent cannot be opened", os.fspath(p))
            return real_open(p, flags, *args, **kwargs)

        monkeypatch.setattr(os, "open", refuse_the_parent)

        def refuse_pin(p):
            raise PermissionError(13, "parent cannot be pinned", os.fspath(p))

        monkeypatch.setattr(acp_client.platform_compat, "pin_directory", refuse_pin)
        put_backs: list[tuple] = []
        monkeypatch.setattr(
            acp_client.pinned_fs,
            "put_back_no_clobber",
            lambda *args, **kwargs: put_backs.append(args) or None,
        )

        restored = AcpClient._restore_aside_without_clobber(aside, path, expect)

        assert restored is False
        assert not path.exists()
        assert aside.read_bytes() == b'{"permissions": {"defaultMode": "default"}}'
        assert put_backs == []


class TestWindowsRestorePin:
    """Windows keeps the moved-aside restore inside one held parent."""

    @staticmethod
    def _moved_identity(aside: Path) -> tuple[int, int]:
        moved = aside.stat()
        return moved.st_dev, moved.st_ino

    def test_windows_restore_holds_a_parent_pin_across_the_rename(self, tmp_path, monkeypatch):
        parent = tmp_path / ".claude"
        parent.mkdir()
        path = parent / "settings.local.json"
        aside = parent / "settings.local.json.0123456789abcdef.crew-gc"
        payload = b'{"permissions": {"defaultMode": "default"}}'
        aside.write_bytes(payload)
        pin_target = parent / "pin-target"
        pin_target.touch()
        real_open = os.open
        real_rename = os.rename
        pinned_paths: list[Path] = []
        pin_fds: list[int] = []
        renames: list[tuple[Path, Path]] = []

        def pin_directory(candidate: Path) -> int:
            pinned_paths.append(candidate)
            pin = real_open(pin_target, os.O_RDONLY)
            pin_fds.append(pin)
            return pin

        def rename_while_pinned(source, destination) -> None:
            assert pin_fds, "rename ran before the parent was pinned"
            os.fstat(pin_fds[-1])
            renames.append((Path(source), Path(destination)))
            real_rename(source, destination)

        monkeypatch.setattr(acp_client.platform_compat, "IS_WINDOWS", True)
        monkeypatch.setattr(acp_client.platform_compat, "pin_directory", pin_directory)
        monkeypatch.setattr(acp_client.os, "rename", rename_while_pinned)

        restored = AcpClient._restore_aside_without_clobber(
            aside, path, self._moved_identity(aside)
        )

        assert restored is True
        assert pinned_paths == [parent]
        assert renames == [(aside, path)]
        with pytest.raises(OSError):
            os.fstat(pin_fds[0])
        assert path.read_bytes() == payload
        assert not aside.exists()

    def test_windows_restore_pin_failure_keeps_the_aside_and_does_not_rename(
        self, tmp_path, monkeypatch
    ):
        parent = tmp_path / ".claude"
        parent.mkdir()
        path = parent / "settings.local.json"
        aside = parent / "settings.local.json.0123456789abcdef.crew-gc"
        payload = b'{"permissions": {"defaultMode": "default"}}'
        aside.write_bytes(payload)
        renames: list[tuple[object, object]] = []

        def refuse_pin(_candidate: Path) -> int:
            raise NotADirectoryError("the parent name is a junction")

        def record_rename(source, destination) -> None:
            renames.append((source, destination))

        monkeypatch.setattr(acp_client.platform_compat, "IS_WINDOWS", True)
        monkeypatch.setattr(acp_client.platform_compat, "pin_directory", refuse_pin)
        monkeypatch.setattr(acp_client.os, "rename", record_rename)

        restored = AcpClient._restore_aside_without_clobber(
            aside, path, self._moved_identity(aside)
        )

        assert restored is False
        assert renames == []
        assert aside.read_bytes() == payload
        assert not path.exists()

    def test_windows_restore_refuses_a_cross_directory_aside(self, tmp_path, monkeypatch):
        source_parent = tmp_path / "source"
        destination_parent = tmp_path / "destination"
        source_parent.mkdir()
        destination_parent.mkdir()
        aside = source_parent / "settings.local.json.0123456789abcdef.crew-gc"
        path = destination_parent / "settings.local.json"
        aside.write_bytes(b"moved aside")
        pinned_paths: list[Path] = []
        renames: list[tuple[object, object]] = []

        monkeypatch.setattr(acp_client.platform_compat, "IS_WINDOWS", True)
        monkeypatch.setattr(
            acp_client.platform_compat,
            "pin_directory",
            lambda candidate: pinned_paths.append(candidate),
        )
        monkeypatch.setattr(
            acp_client.os,
            "rename",
            lambda source, destination: renames.append((source, destination)),
        )

        restored = AcpClient._rename_aside_noreplace(aside, path)

        assert restored is False
        assert pinned_paths == []
        assert renames == []
        assert aside.read_bytes() == b"moved aside"
        assert not path.exists()

    def test_windows_restore_under_the_pin_keeps_a_raced_in_occupant(self, tmp_path, monkeypatch):
        parent = tmp_path / ".claude"
        parent.mkdir()
        path = parent / "settings.local.json"
        aside = parent / "settings.local.json.0123456789abcdef.crew-gc"
        occupant = b'{"permissions": {"allow": ["Bash(ls)"]}}'
        moved_aside = b'{"permissions": {"defaultMode": "default"}}'
        path.write_bytes(occupant)
        aside.write_bytes(moved_aside)
        pin_target = parent / "pin-target"
        pin_target.touch()
        real_open = os.open
        pin_fds: list[int] = []
        rename_under_pin: list[bool] = []

        def pin_directory(_candidate: Path) -> int:
            pin = real_open(pin_target, os.O_RDONLY)
            pin_fds.append(pin)
            return pin

        def windows_rename(_source, destination) -> None:
            os.fstat(pin_fds[-1])
            rename_under_pin.append(True)
            if Path(destination).exists():
                raise FileExistsError(destination)
            raise AssertionError("the occupied destination should refuse the rename")

        monkeypatch.setattr(acp_client.platform_compat, "IS_WINDOWS", True)
        monkeypatch.setattr(acp_client.platform_compat, "pin_directory", pin_directory)
        monkeypatch.setattr(acp_client.os, "rename", windows_rename)

        restored = AcpClient._restore_aside_without_clobber(
            aside, path, self._moved_identity(aside)
        )

        assert restored is False
        assert rename_under_pin == [True]
        with pytest.raises(OSError):
            os.fstat(pin_fds[0])
        assert path.read_bytes() == occupant
        assert aside.read_bytes() == moved_aside


class TestCreateRaceLoserPoll:
    """The ``O_EXCL`` loser polls for the WINNER's record, not for any record.

    Two sessions pass the not-exists probe together; one wins the create and
    records its seed a moment later. The loser's poll bridges that persist
    window so a byte-identical sibling shares the surface instead of running
    toolless. The sidecar can also carry a STALE entry -- a killed session's
    record for a file since gone, with a different digest -- and that entry
    proves nothing about the winner: only a record naming the loser's own
    bytes may end the poll early.

    The poll is driven through a :class:`ManualClock` installed on
    ``acp_client`` ALONE (testing-conventions rule D2), never by rebinding the
    stdlib ``time.sleep``: ``acp_client.time`` IS the stdlib module, and the
    seed-provenance sidecar lock (``platform_lock_compat``) backs off through
    ``time.sleep`` whenever another holder has it. Under a stdlib patch that
    backoff counts as a poll and runs the "winner records now" hook BEFORE the
    loser's ``O_EXCL`` open, so the loser sees a live sibling owner and declines
    without ever reaching the create -- a red that depends on who else holds the
    lock, which on a loaded shard is anyone's persist.
    """

    _SERVED = ["global.anthropic.claude-opus-5[1m]"]

    @pytest.fixture(autouse=True)
    # nosemgrep: kirocrew.test-autouse-shared-monkeypatch -- no test in this module calls monkeypatch.undo()
    def _isolated_provenance(self, tmp_path, monkeypatch):
        """Per-test provenance registries, sidecar and a warm advertised-model cache."""
        from kiro_crew import model_registry
        from kiro_crew.acp import seed_provenance

        monkeypatch.setattr(seed_provenance, "_RECORDS", {})
        monkeypatch.setattr(seed_provenance, "_LIVE", {})
        monkeypatch.setattr(seed_provenance, "_SHARERS", {})
        monkeypatch.setattr(seed_provenance, "_sidecar_path", lambda: tmp_path / "seeds.json")
        monkeypatch.setattr(
            model_registry, "_ADVERTISED_MODELS", {"claude_code": list(self._SERVED)}
        )

    @staticmethod
    def _stale_record_for_a_vanished_file(path: Path) -> None:
        """A durable entry for *path* with a foreign digest and no file behind it.

        What a ``kill -9``'d session leaves: its record persisted, its file since
        removed, its holder identity dead. Written straight into the sidecar as
        the digest-only shape ``_read_disk_seeds`` accepts, so no live holder
        pins it and nothing in this process's memory knows of it.
        """
        from kiro_crew.acp import seed_provenance

        stale = '{"permissions": {"defaultMode": "bypassPermissions"}}'
        sidecar = seed_provenance._sidecar_path()
        sidecar.write_text(
            json.dumps(
                {
                    "seeds": {
                        os.fspath(path): {
                            "size": len(stale.encode("utf-8")),
                            "sha256": seed_provenance.digest(stale),
                        }
                    }
                }
            ),
            encoding="utf-8",
        )
        assert seed_provenance.recorded_durable(path) is not None
        assert not path.exists()

    @staticmethod
    def _winner_holds_the_name(monkeypatch, path: Path, payload: str) -> list[int]:
        """The winner's FILE lands under the loser's ``O_EXCL`` open; its record does not.

        The pre-write probe saw the name vacant, so the loser takes the create
        branch; the file appearing right before the real ``os.open`` makes that
        open raise ``FileExistsError`` for real. The winner's durable record is
        the caller's to publish, when it chooses.
        """
        real_open = os.open
        fired: list[int] = []

        def winner_created_the_file(p, flags, *args, **kwargs):
            if not fired and os.fspath(p) == os.fspath(path) and flags & os.O_EXCL:
                fired.append(1)
                path.parent.mkdir(parents=True, exist_ok=True)
                path.write_text(payload, encoding="utf-8")
            return real_open(p, flags, *args, **kwargs)

        monkeypatch.setattr(os, "open", winner_created_the_file)
        return fired

    class _PollClock(ManualClock):
        """The loser's poll clock: ``on_poll(n)`` runs after the n-th poll sleep.

        That is the winner's move between two probes. ``poll_cost`` is how far
        the clock moves per poll on top of the sleep itself, so a bounded
        deadline is reached in a handful of iterations. Installed on
        ``acp_client`` alone, so the sidecar lock's backoff still sleeps on the
        real clock and runs no hook.
        """

        def __init__(self, on_poll, *, poll_cost: float = 0.0) -> None:
            super().__init__()
            self._on_poll = on_poll
            self._poll_cost = poll_cost

        def sleep(self, seconds: float) -> None:
            super().sleep(seconds)
            self.advance(self._poll_cost)
            self._on_poll(len(self.sleeps))

    @classmethod
    def _poll_clock(cls, monkeypatch, on_poll, *, poll_cost: float = 0.0) -> ManualClock:
        clock = cls._PollClock(on_poll, poll_cost=poll_cost)
        clock.install(monkeypatch, acp_client)
        return clock

    @staticmethod
    def _hold_the_provenance_lock(
        release: threading.Event, held: threading.Event
    ) -> threading.Thread:
        """Hold the seed-provenance sidecar lock on another thread until *release*.

        The lock the loser's ``held_by_another`` probe takes before its create,
        held the way a sibling's persist would hold it. ``held`` is set once the
        lock is taken.
        """
        from kiro_crew import platform_lock_compat
        from kiro_crew.acp import seed_provenance
        from kiro_crew.config.paths import config_dir

        lock_file = config_dir() / seed_provenance._LOCK_FILENAME
        lock_file.parent.mkdir(parents=True, exist_ok=True)

        def hold() -> None:
            fd = os.open(os.fspath(lock_file), os.O_CREAT | os.O_RDWR, 0o600)
            try:
                platform_lock_compat.acquire_lock(fd, exclusive=True)
                held.set()
                release.wait(10)
                platform_lock_compat.release_lock(fd)
            finally:
                os.close(fd)

        holder = threading.Thread(target=hold, name="seed-lock-holder", daemon=True)
        holder.start()
        return holder

    def test_create_race_loser_waits_out_the_winners_record_not_a_stale_entry(
        self, tmp_path, monkeypatch
    ):
        """A stale durable entry does not end the poll; the winner's matching one does.

        The winner has created the file but its ``record()`` persist is still in
        flight when the loser starts polling. The sidecar already holds a stale
        record with a different digest for a file that is gone. A poll that
        treated ANY durable record as "the winner's bytes are settled" would
        decline on its first iteration and run this session toolless; the poll
        must keep waiting until a record naming its own bytes appears, then share.
        """
        from kiro_crew.acp import seed_provenance

        loser = AcpClient(
            work_dir=tmp_path, acp_backend=ACP_BACKEND_CLAUDE, permission_mode="default"
        )
        path = tmp_path / ".claude" / "settings.local.json"
        payload = loser._render_claude_settings_payload()
        self._stale_record_for_a_vanished_file(path)
        fired = self._winner_holds_the_name(monkeypatch, path, payload)

        def winner_records_on_the_third_poll(poll: int) -> None:
            if poll == 3:
                assert seed_provenance.record(path, payload, "winner-session") is True

        clock = self._poll_clock(monkeypatch, winner_records_on_the_third_poll)

        loser._write_claude_local_settings()

        assert fired, "the race window must have been exercised"
        assert len(clock.sleeps) == 3, "the poll must have outlasted the stale entry"
        assert loser._claude_settings_shared is True
        assert loser._permission_surface_governed is True
        assert loser._claude_settings_authored is False
        assert path.read_text(encoding="utf-8") == payload

    def test_a_loser_whose_bytes_can_never_match_declines_once_at_the_deadline(
        self, tmp_path, monkeypatch
    ):
        """Positive control: a mismatched loser burns the bounded deadline, then declines.

        The winner recorded a DIFFERENT payload. No record naming the loser's
        bytes can ever appear, so the poll runs to its 2 s deadline -- bounded,
        never unbounded -- and the loser declines exactly once. The clock is
        mocked so the deadline is a handful of iterations, not two real seconds.
        """
        from kiro_crew.acp import seed_provenance

        loser = AcpClient(
            work_dir=tmp_path,
            acp_backend=ACP_BACKEND_CLAUDE,
            permission_mode="bypassPermissions",
        )
        path = tmp_path / ".claude" / "settings.local.json"
        winners_payload = AcpClient(
            work_dir=tmp_path, acp_backend=ACP_BACKEND_CLAUDE, permission_mode="default"
        )._render_claude_settings_payload()
        assert winners_payload != loser._render_claude_settings_payload()
        fired = self._winner_holds_the_name(monkeypatch, path, winners_payload)
        declines: list[Path] = []

        def winner_records_on_the_first_poll(poll: int) -> None:
            if poll == 1:
                # The winner's record lands on the first poll and is settled from
                # then on -- a record that never names the loser's bytes.
                assert seed_provenance.record(path, winners_payload, "winner-session") is True

        # Half a mocked second per poll: four polls reach the 2 s deadline.
        clock = self._poll_clock(monkeypatch, winner_records_on_the_first_poll, poll_cost=0.45)
        monkeypatch.setattr(AcpClient, "_log_declined_share", lambda self, p: declines.append(p))

        loser._write_claude_local_settings()

        assert fired, "the race window must have been exercised"
        # Four 50 ms polls at half a mocked second each reach the 2 s deadline;
        # the fifth probe breaks on it, and nothing polls past that.
        assert clock.sleeps == (0.05,) * 4
        assert declines == [path]
        assert loser._claude_settings_shared is False
        assert loser._claude_settings_authored is False
        assert loser._permission_surface_governed is False
        assert path.read_text(encoding="utf-8") == winners_payload

    def test_a_contended_provenance_lock_is_waited_out_and_is_not_a_poll(
        self, tmp_path, monkeypatch
    ):
        """The sidecar lock's backoff is real time, not the loser's poll.

        Another holder has the seed-provenance lock when the loser's pre-create
        ``held_by_another`` probe takes it -- what a sibling's persist on a
        loaded shard looks like. The loser waits that out on the REAL clock,
        then runs the same race as the deadline control above: the winner's
        record lands on the first poll, four polls reach the deadline, one
        decline. The lock backoff adds no entry to the poll schedule and runs no
        poll hook, so the winner cannot be recorded before the create. With the
        stdlib ``time.sleep`` patched instead, the backoff runs the hook, the
        record lands first, and the loser declines under a live sibling owner
        without ever opening the file.
        """
        from kiro_crew.acp import seed_provenance

        loser = AcpClient(
            work_dir=tmp_path,
            acp_backend=ACP_BACKEND_CLAUDE,
            permission_mode="bypassPermissions",
        )
        path = tmp_path / ".claude" / "settings.local.json"
        winners_payload = AcpClient(
            work_dir=tmp_path, acp_backend=ACP_BACKEND_CLAUDE, permission_mode="default"
        )._render_claude_settings_payload()
        fired = self._winner_holds_the_name(monkeypatch, path, winners_payload)
        declines: list[Path] = []

        def winner_records_on_the_first_poll(poll: int) -> None:
            if poll == 1:
                assert seed_provenance.record(path, winners_payload, "winner-session") is True

        clock = self._poll_clock(monkeypatch, winner_records_on_the_first_poll, poll_cost=0.45)
        monkeypatch.setattr(AcpClient, "_log_declined_share", lambda self, p: declines.append(p))

        # The holder lets go only once the lock poll has BACKED OFF at least
        # once, so the contention is certain and the release is a signal, not a
        # timer. The backoff is seen at the seam the lock reads its clock through
        # (``platform_compat.time``, a module-level name); the stdlib stays as is.
        from kiro_crew import platform_compat

        release, held, backed_off = threading.Event(), threading.Event(), threading.Event()
        real_time = platform_compat.time
        loser_thread = threading.current_thread()

        class _BackoffSpy(types.ModuleType):
            def __getattr__(self, name):
                return getattr(real_time, name)

            @staticmethod
            def sleep(secs: float) -> None:
                # Only the LOSER's own backoff counts: a sleep from any other
                # thread would release the holder before the loser ever met it.
                if threading.current_thread() is loser_thread:
                    backed_off.set()
                real_time.sleep(secs)

        monkeypatch.setattr(platform_compat, "time", _BackoffSpy("time"))

        def let_go_once_backed_off() -> None:
            backed_off.wait(10)
            release.set()

        holder = self._hold_the_provenance_lock(release, held)
        assert held.wait(10), "the contender never took the lock"
        threading.Thread(target=let_go_once_backed_off, daemon=True).start()
        try:
            loser._write_claude_local_settings()
        finally:
            release.set()
            holder.join(10)

        assert backed_off.is_set(), "the loser never met the contended lock"
        assert not holder.is_alive()
        assert fired, "the race window must have been exercised"
        # The backoff sleeps are not in the poll schedule, and the hook ran only
        # after the create: the same four polls and one decline as uncontended.
        assert clock.sleeps == (0.05,) * 4
        assert declines == [path]
        assert loser._claude_settings_shared is False
        assert loser._claude_settings_authored is False
        assert path.read_text(encoding="utf-8") == winners_payload
