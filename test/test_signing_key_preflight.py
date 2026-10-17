"""A truncated ``token_signing.key`` refuses the boot instead of degrading silently.

A key file shorter than 32 bytes would make every boot sign with an ephemeral
secret, logging every dashboard session out at each restart and quarantining the
HMAC-certified tag grants, with one WARNING line saying why. The gateway refuses to
start on such a file and prints manual repair steps; the same probe backs
``kirocrew doctor``. Nothing here repairs the file: that is a deliberate operator
action (see the ``signing_key_preflight`` docstring).
"""

from __future__ import annotations

import asyncio
import logging
import os
import types
from pathlib import Path

import pytest

from kiro_crew.dashboard import token_secret as ts
from kiro_crew.doctor_checks import access as doctor_access
from kiro_crew.slack import gateway

# The real probe, captured before the autouse fixture below neutralises it.
_REAL_INSIDE = ts._inside_agent_sandbox

# A fixed, full-length key (D6: no unseeded draws). Distinct from every
# truncated fixture below and from any ephemeral secret the loader would mint.
_FULL_KEY = bytes(range(1, ts._MIN_KEY_BYTES + 1))


@pytest.fixture(autouse=True)
def _outside_the_agent_sandbox(_floor_monkeypatch):
    """These tests model the HOST (gateway boot, an operator's shell). The suite
    itself may run inside a Kiro Crew agent sandbox, where the launcher exports the
    marker and places the process under ``kirocrew-agents.slice``; neutralise both
    so a real file is judged by its size. The tests about the masked state set the
    signals back on purpose."""
    _floor_monkeypatch.delenv("KIROCREW_SANDBOX_ACTIVE", raising=False)
    _floor_monkeypatch.setattr(
        ts, "_inside_agent_sandbox", lambda cgroup_path="/proc/self/cgroup": False
    )
    _floor_monkeypatch.setattr(
        doctor_access.cli_doctor.sandbox, "agent_confinement_evidence", lambda: None
    )


class TestSigningKeyHealth:
    def test_a_full_key_is_ok(self, tmp_path: Path) -> None:
        key = tmp_path / ts._SECRET_KEY_FILE
        key.write_bytes(_FULL_KEY)
        assert ts.signing_key_health(key) == ("ok", key)

    def test_no_file_is_absent_not_a_fault(self, tmp_path: Path) -> None:
        key = tmp_path / ts._SECRET_KEY_FILE
        assert ts.signing_key_health(key) == ("absent", key)

    @pytest.mark.parametrize("size", [0, 1, ts._MIN_KEY_BYTES - 1])
    def test_a_short_file_is_short(self, tmp_path: Path, size: int) -> None:
        key = tmp_path / ts._SECRET_KEY_FILE
        key.write_bytes(b"\x01" * size)
        assert ts.signing_key_health(key) == ("short", key)

    def test_a_directory_at_the_path_is_other(self, tmp_path: Path) -> None:
        key = tmp_path / ts._SECRET_KEY_FILE
        key.mkdir()
        assert ts.signing_key_health(key) == ("other", key)

    @pytest.mark.skipif(os.name == "nt", reason="POSIX symlink")
    def test_a_symlink_is_judged_by_itself_not_its_target(self, tmp_path: Path) -> None:
        """``lstat``: a link to a full key is still not a regular key file."""
        real = tmp_path / "elsewhere"
        real.write_bytes(_FULL_KEY)
        key = tmp_path / ts._SECRET_KEY_FILE
        key.symlink_to(real)
        assert ts.signing_key_health(key) == ("other", key)

    def test_a_failing_lstat_is_unstatable_not_other(self, tmp_path: Path, monkeypatch) -> None:
        """EIO/ESTALE on the key leaf says nothing about whether a key is there,
        so it must not land in ``other``, the state the preflight never loads."""
        import errno

        key = tmp_path / ts._SECRET_KEY_FILE
        real_lstat = os.lstat

        def _eio(path, *args, **kwargs):
            if Path(path) == key:
                raise OSError(errno.EIO, "Input/output error")
            return real_lstat(path, *args, **kwargs)

        with monkeypatch.context() as patch:
            patch.setattr(ts.os, "lstat", _eio)
            answer = ts.signing_key_health(key)
        assert answer == ("unstatable", key)

    def test_the_probe_reads_nothing_and_writes_nothing(self, tmp_path: Path) -> None:
        key = tmp_path / ts._SECRET_KEY_FILE
        key.write_bytes(b"")
        before = key.stat()
        ts.signing_key_health(key)
        after = key.stat()
        assert (before.st_ino, before.st_size, before.st_mtime_ns) == (
            after.st_ino,
            after.st_size,
            after.st_mtime_ns,
        )
        assert sorted(p.name for p in tmp_path.iterdir()) == [ts._SECRET_KEY_FILE]

    def test_default_path_is_the_data_home_key(self, tmp_path: Path, monkeypatch) -> None:
        monkeypatch.setattr("kiro_crew.config.loader.config_dir", lambda: tmp_path)
        state, key = ts.signing_key_health()
        assert state == "absent"
        assert key == tmp_path / ts._SECRET_KEY_FILE

    def test_inside_the_agent_sandbox_the_key_is_masked_not_short(
        self, tmp_path: Path, monkeypatch
    ) -> None:
        """The launcher bind-mounts an EMPTY file over the key inside every agent
        sandbox. Read as a size, that is "short"; read correctly, it is unobservable,
        and the difference is whether an operator gets told to move a healthy key."""
        key = tmp_path / ts._SECRET_KEY_FILE
        key.write_bytes(b"")  # what the sandbox shows
        monkeypatch.setattr(ts, "_inside_agent_sandbox", lambda cgroup_path="": True)
        assert ts.signing_key_health(key) == ("masked", key)

    def test_caller_supplied_confinement_is_masked(self, tmp_path: Path) -> None:
        """macOS Seatbelt has no cgroup to read; ``doctor`` passes the kernel's
        verdict in as *confined*."""
        key = tmp_path / ts._SECRET_KEY_FILE
        key.write_bytes(b"")
        assert ts.signing_key_health(key, confined=True) == ("masked", key)

    def test_a_key_on_another_device_is_not_a_mask_without_confinement(
        self, tmp_path: Path, monkeypatch
    ) -> None:
        """A filesystem an operator mounted at the key leaf puts the file on a
        different device from its directory, exactly like the sandbox's bind mount.
        On a host that is not a mask, and a truncated key there must still be
        ``short`` so the boot refuses it."""
        key = tmp_path / ts._SECRET_KEY_FILE
        key.write_bytes(b"")
        real = os.lstat

        def _parent_on_another_device(path, *a, **kw):  # type: ignore[no-untyped-def]
            st = real(path, *a, **kw)
            if Path(path) == key.parent:
                return os.stat_result((*st[:2], st.st_dev + 1, *st[3:]))
            return st

        # Scoped to the assertion: ts.os is the shared os module, and the rebuilt
        # stat_result drops Windows' st_file_attributes, which tmp_path's rmtree
        # teardown reads through the same lstat.
        with monkeypatch.context() as m:
            m.setattr(ts.os, "lstat", _parent_on_another_device)
            health = ts.signing_key_health(key)
        assert health == ("short", key)

    def test_outside_the_sandbox_the_marker_is_not_assumed(
        self, tmp_path: Path, monkeypatch
    ) -> None:
        key = tmp_path / ts._SECRET_KEY_FILE
        key.write_bytes(b"")
        monkeypatch.delenv("KIROCREW_SANDBOX_ACTIVE", raising=False)
        assert ts.signing_key_health(key) == ("short", key)


class TestConfinementProbes:
    """The Linux signal behind ``"masked"`` that an in-sandbox process cannot scrub."""

    def test_the_marker_alone_answers_true(self, monkeypatch, tmp_path: Path) -> None:
        monkeypatch.setenv("KIROCREW_SANDBOX_ACTIVE", "1")
        empty = tmp_path / "cgroup"
        empty.write_text("0::/user.slice/user-1000.slice/session-1.scope\n")
        assert _REAL_INSIDE(str(empty)) is True

    def test_the_agents_slice_answers_true_without_the_marker(
        self, monkeypatch, tmp_path: Path
    ) -> None:
        monkeypatch.delenv("KIROCREW_SANDBOX_ACTIVE", raising=False)
        cg = tmp_path / "cgroup"
        cg.write_text(
            "0::/user.slice/user-1000.slice/kirocrew.slice/kirocrew-agents.slice/run-1.scope\n"
        )
        assert _REAL_INSIDE(str(cg)) is True

    def test_a_plain_session_answers_false(self, monkeypatch, tmp_path: Path) -> None:
        monkeypatch.delenv("KIROCREW_SANDBOX_ACTIVE", raising=False)
        cg = tmp_path / "cgroup"
        cg.write_text("0::/user.slice/user-1000.slice/session-1.scope\n")
        assert _REAL_INSIDE(str(cg)) is False

    def test_an_unreadable_cgroup_file_answers_false(self, monkeypatch, tmp_path: Path) -> None:
        monkeypatch.delenv("KIROCREW_SANDBOX_ACTIVE", raising=False)
        assert _REAL_INSIDE(str(tmp_path / "missing")) is False


class TestBootSecretLoad:
    """``load_boot_secret`` judges the SAME load the dashboard signs with."""

    @pytest.fixture(autouse=True)
    def _fresh_secret(self, tmp_path: Path, _floor_monkeypatch):
        _floor_monkeypatch.setattr(ts, "_SECRET", None)
        _floor_monkeypatch.setattr(ts, "_CREATE_BACKOFF_SECONDS", 0)
        _floor_monkeypatch.setattr("kiro_crew.config.loader.config_dir", lambda: tmp_path)

    def test_a_short_file_at_load_time_answers_short(self, tmp_path: Path) -> None:
        (tmp_path / ts._SECRET_KEY_FILE).write_bytes(b"\x01" * 5)
        assert ts.load_boot_secret() == "short"
        assert ts._SECRET is not None and len(ts._SECRET) == ts._MIN_KEY_BYTES
        assert (tmp_path / ts._SECRET_KEY_FILE).stat().st_size == 5, "never repaired"

    def test_a_persisted_key_answers_loaded_and_is_memoized(self, tmp_path: Path) -> None:
        key = _FULL_KEY
        (tmp_path / ts._SECRET_KEY_FILE).write_bytes(key)
        assert ts.load_boot_secret() == "loaded"
        assert ts._get_secret() == key

    def test_a_first_boot_publish_answers_loaded(self, tmp_path: Path) -> None:
        assert ts.load_boot_secret() == "loaded"
        assert (tmp_path / ts._SECRET_KEY_FILE).read_bytes() == ts._get_secret()

    def test_an_earlier_load_is_not_re_judged(self, monkeypatch) -> None:
        monkeypatch.setattr(ts, "_SECRET", b"\x00" * ts._MIN_KEY_BYTES)
        assert ts.load_boot_secret() == "already-loaded"

    def test_an_unreadable_short_key_answers_ephemeral_not_loaded(
        self, tmp_path: Path, monkeypatch
    ) -> None:
        """Every read of the key fails (mode 000, a foreign owner, a Windows sharing
        violation held across the budget). The fallback is ephemeral, and the load
        must not claim a persisted key it never confirmed."""
        path = tmp_path / ts._SECRET_KEY_FILE
        path.write_bytes(b"\x01" * 5)
        monkeypatch.setattr(ts, "_CREATE_MAX_ATTEMPTS", 2)

        def _unreadable(self: Path) -> bytes:
            raise PermissionError(13, "Permission denied", str(self))

        monkeypatch.setattr(Path, "read_bytes", _unreadable)
        monkeypatch.setattr(ts, "_final_key_read", lambda _p: (None, False))
        assert ts.load_boot_secret() == "ephemeral"
        assert ts._SECRET is not None and len(ts._SECRET) == ts._MIN_KEY_BYTES

    def test_a_sibling_filling_its_empty_file_during_the_load_answers_loaded(
        self, tmp_path: Path, monkeypatch
    ) -> None:
        """The in-place creator's empty-file window, seen by a concurrent boot: the
        loader's bounded retry reads the finished key, so the preflight that follows
        this verdict boots instead of refusing."""
        path = tmp_path / ts._SECRET_KEY_FILE
        path.write_bytes(b"")
        key = _FULL_KEY
        sleeps: list[float] = []

        def _sleep(seconds: float) -> None:
            # The sibling finishes on the loader's second backoff, well inside
            # the retry budget, so the verdict depends on the retry, not a clock.
            sleeps.append(seconds)
            if len(sleeps) == 2:
                path.write_bytes(key)

        # A module-local stub: the shared time module stays untouched.
        monkeypatch.setattr(ts, "time", types.SimpleNamespace(sleep=_sleep))
        assert ts.load_boot_secret() == "loaded"
        assert len(sleeps) < ts._CREATE_MAX_ATTEMPTS, "an in-loop retry read the key"
        assert ts._get_secret() == key

    def test_a_sibling_finishing_after_the_last_retry_is_adopted_not_approved(
        self, tmp_path: Path, monkeypatch
    ) -> None:
        """A sibling's in-place write lands after the loop's final read. The verdict
        must come from a read, so the finished key is returned and memoized; a
        separate stat would see a full file and approve a random ephemeral secret."""
        monkeypatch.setattr(ts, "_CREATE_MAX_ATTEMPTS", 3)
        path = tmp_path / ts._SECRET_KEY_FILE
        path.write_bytes(b"\x01" * 5)
        key = _FULL_KEY
        sleeps: list[float] = []

        def _sleep(seconds: float) -> None:
            sleeps.append(seconds)
            if len(sleeps) == ts._CREATE_MAX_ATTEMPTS:
                path.write_bytes(key)

        # A module-local stub: the shared time module stays untouched.
        monkeypatch.setattr(ts, "time", types.SimpleNamespace(sleep=_sleep))
        assert ts.load_boot_secret() == "loaded"
        assert len(sleeps) == ts._CREATE_MAX_ATTEMPTS, "the write landed after the last retry"
        assert ts._get_secret() == key

    @pytest.mark.skipif(
        os.name == "nt" or (hasattr(os, "geteuid") and os.geteuid() == 0),
        reason="needs POSIX permission bits that refuse this process",
    )
    def test_a_whole_key_this_process_cannot_read_boots_on_the_loaders_warning(
        self, tmp_path: Path, monkeypatch, capsys, caplog
    ) -> None:
        """End to end through the real probe and loader: the key lstats whole, every
        read is denied, and the loader falls back to an ephemeral secret. The boot
        goes ahead on the loader's WARNING (doctor reports the key): a Windows
        sharing violation held past the retry budget must not block the boot."""
        monkeypatch.setattr(ts, "_CREATE_MAX_ATTEMPTS", 2)
        key = tmp_path / ts._SECRET_KEY_FILE
        key.write_bytes(_FULL_KEY)
        key.chmod(0)
        try:
            assert ts.signing_key_health()[0] == "ok"
            with caplog.at_level(logging.WARNING, logger=ts.logger.name):
                gateway.signing_key_preflight()  # does not raise
            assert "using ephemeral secret" in caplog.text
            assert capsys.readouterr().err == ""
        finally:
            key.chmod(0o600)

    def test_the_final_read_reports_what_it_saw(self, tmp_path: Path) -> None:
        path = tmp_path / ts._SECRET_KEY_FILE
        assert ts._final_key_read(path) == (None, False)
        path.write_bytes(b"\x01" * 5)
        assert ts._final_key_read(path) == (None, True)
        key = _FULL_KEY
        path.write_bytes(key)
        assert ts._final_key_read(path) == (key, False)
        path.unlink()
        path.mkdir()
        assert ts._final_key_read(path) == (None, False)


class TestBootPreflight:
    @pytest.fixture(autouse=True)
    def _load_reports_healthy(self, _floor_monkeypatch):
        """The authoritative load is exercised in TestBootSecretLoad; here it is a
        stub so no test writes a key into the real data home."""
        _floor_monkeypatch.setattr(gateway.token_secret, "load_boot_secret", lambda: "loaded")

    def test_a_short_key_refuses_to_start_and_names_the_remedy(
        self, tmp_path: Path, monkeypatch, capsys, caplog
    ) -> None:
        key = tmp_path / ts._SECRET_KEY_FILE
        key.write_bytes(b"")
        monkeypatch.setattr(gateway.token_secret, "signing_key_health", lambda: ("short", key))
        monkeypatch.setattr(gateway.token_secret, "load_boot_secret", lambda: "short")

        with caplog.at_level(logging.CRITICAL, logger=gateway.logger.name):
            with pytest.raises(SystemExit) as exit_info:
                gateway.signing_key_preflight()

        assert exit_info.value.code == 1
        err = capsys.readouterr().err
        assert str(key) in err
        for step in ts.signing_key_remedy(key, gateway.BOOT_RESTART_HINT):
            assert step in err, "stderr must carry every manual step"
        assert err.index("1. Stop the gateway") < err.index("3. Delete it by hand")
        assert "refusing to start" in caplog.text
        assert str(key) in caplog.text
        assert key.exists() and key.stat().st_size == 0, "the preflight must not touch the file"

    @pytest.mark.parametrize("state", ["ok", "absent"])
    def test_a_load_that_fell_back_on_a_short_file_refuses_too(
        self, tmp_path: Path, monkeypatch, capsys, state: str
    ) -> None:
        """The ``lstat`` saw a healthy (or no) key, then an in-place rewrite left it
        short by the time the loader read it. The load is what the gateway will sign
        with, so its verdict refuses the boot."""
        key = tmp_path / ts._SECRET_KEY_FILE
        monkeypatch.setattr(gateway.token_secret, "signing_key_health", lambda: (state, key))
        monkeypatch.setattr(gateway.token_secret, "load_boot_secret", lambda: "short")
        with pytest.raises(SystemExit) as exit_info:
            gateway.signing_key_preflight()
        assert exit_info.value.code == 1
        assert str(key) in capsys.readouterr().err

    def test_a_short_probe_that_loads_a_full_key_boots(self, tmp_path: Path, monkeypatch) -> None:
        """A sibling gateway on a link-less home creates the key in place, so its
        empty file can be what the ``lstat`` sees. The loader waits that window out
        and reads the finished key; the boot follows the load, not the probe."""
        key = tmp_path / ts._SECRET_KEY_FILE
        monkeypatch.setattr(gateway.token_secret, "signing_key_health", lambda: ("short", key))
        loads: list[str] = []

        def _load() -> str:
            loads.append("load")
            return "loaded"

        monkeypatch.setattr(gateway.token_secret, "load_boot_secret", _load)
        gateway.signing_key_preflight()  # does not raise
        assert loads == ["load"], "a short probe must be re-judged by the load"

    def test_a_short_probe_with_an_earlier_load_still_refuses(
        self, tmp_path: Path, monkeypatch
    ) -> None:
        """When a caller memoized the secret first, the load has no verdict to give,
        so the probe decides."""
        key = tmp_path / ts._SECRET_KEY_FILE
        monkeypatch.setattr(gateway.token_secret, "signing_key_health", lambda: ("short", key))
        monkeypatch.setattr(gateway.token_secret, "load_boot_secret", lambda: "already-loaded")
        with pytest.raises(SystemExit):
            gateway.signing_key_preflight()

    def test_a_short_probe_whose_load_fell_back_unclassified_still_refuses(
        self, tmp_path: Path, monkeypatch
    ) -> None:
        """An unreadable short key: the loader falls back without being able to
        say why. Only a confirmed persisted load may clear the probe's short."""
        key = tmp_path / ts._SECRET_KEY_FILE
        monkeypatch.setattr(gateway.token_secret, "signing_key_health", lambda: ("short", key))
        monkeypatch.setattr(gateway.token_secret, "load_boot_secret", lambda: "ephemeral")
        with pytest.raises(SystemExit) as exit_info:
            gateway.signing_key_preflight()
        assert exit_info.value.code == 1

    @pytest.mark.parametrize("state", ["ok", "absent"])
    def test_a_healthy_probe_whose_load_fell_back_unclassified_boots(
        self, tmp_path: Path, monkeypatch, state: str
    ) -> None:
        """An unwritable home degrades to an ephemeral secret and still boots; the
        preflight judges truncation only."""
        key = tmp_path / ts._SECRET_KEY_FILE
        monkeypatch.setattr(gateway.token_secret, "signing_key_health", lambda: (state, key))
        monkeypatch.setattr(gateway.token_secret, "load_boot_secret", lambda: "ephemeral")
        gateway.signing_key_preflight()  # does not raise

    @pytest.mark.parametrize("state", ["ok"])
    def test_an_ephemeral_load_on_an_existing_key_boots(
        self, tmp_path: Path, monkeypatch, capsys, state: str
    ) -> None:
        """The probe saw a key file, then every read of it failed (read-denied, an
        I/O error, an exclusive writer). That is not a truncated key, so the boot
        goes ahead on the loader's WARNING instead of refusing."""
        key = tmp_path / ts._SECRET_KEY_FILE
        key.write_bytes(b"\x01" * ts._MIN_KEY_BYTES)
        monkeypatch.setattr(gateway.token_secret, "signing_key_health", lambda: (state, key))
        monkeypatch.setattr(gateway.token_secret, "load_boot_secret", lambda: "ephemeral")
        gateway.signing_key_preflight()  # does not raise
        assert capsys.readouterr().err == ""

    @pytest.mark.parametrize("state", ["other", "masked"])
    def test_shapes_the_preflight_does_not_judge_skip_the_load(
        self, tmp_path: Path, monkeypatch, state: str
    ) -> None:
        key = tmp_path / ts._SECRET_KEY_FILE
        monkeypatch.setattr(gateway.token_secret, "signing_key_health", lambda: (state, key))

        def _must_not_load() -> str:
            raise AssertionError("only ok/absent/short/unstatable keys are loaded by the preflight")

        monkeypatch.setattr(gateway.token_secret, "load_boot_secret", _must_not_load)
        gateway.signing_key_preflight()  # does not raise

    def test_an_unstatable_key_whose_load_goes_ephemeral_boots(
        self, tmp_path: Path, monkeypatch, capsys
    ) -> None:
        """The leaf answered EIO to ``lstat`` and the loader could not read it
        either: the boot goes ahead on the loader's WARNING, and doctor reports it."""
        key = tmp_path / ts._SECRET_KEY_FILE
        monkeypatch.setattr(gateway.token_secret, "signing_key_health", lambda: ("unstatable", key))
        monkeypatch.setattr(gateway.token_secret, "load_boot_secret", lambda: "ephemeral")
        gateway.signing_key_preflight()  # does not raise
        assert capsys.readouterr().err == ""

    def test_an_unstatable_key_that_loads_boots(self, tmp_path: Path, monkeypatch, capsys) -> None:
        """A transient stat error the load reads past is not a refusal."""
        key = tmp_path / ts._SECRET_KEY_FILE
        monkeypatch.setattr(gateway.token_secret, "signing_key_health", lambda: ("unstatable", key))
        monkeypatch.setattr(gateway.token_secret, "load_boot_secret", lambda: "loaded")
        gateway.signing_key_preflight()  # does not raise
        assert capsys.readouterr().err == ""

    @pytest.mark.parametrize("state", ["ok", "absent", "other", "masked"])
    def test_every_other_state_passes_through(
        self, tmp_path: Path, monkeypatch, capsys, state: str
    ) -> None:
        key = tmp_path / ts._SECRET_KEY_FILE
        monkeypatch.setattr(gateway.token_secret, "signing_key_health", lambda: (state, key))
        gateway.signing_key_preflight()  # does not raise
        assert capsys.readouterr().err == ""

    def test_the_preflight_runs_before_any_session_or_socket_work(self) -> None:
        """Order pin: the preflight sits directly after the persistence preflight
        in ``GatewayOrchestrator.run``, before orphan cleanup, so a poisoned key
        refuses the boot before anything is bound or spawned."""
        import inspect

        src = inspect.getsource(gateway.GatewayOrchestrator.run)
        persistence = src.index("Persistence preflight failed")
        signing = src.index("signing_key_preflight")
        cleanup = src.index("cleanup_orphaned_sessions")
        assert persistence < signing < cleanup


class TestPreflightNeverRunsOnTheEventLoop:
    """With no worker thread at boot, the preflight must refuse the boot, not run inline:
    its lstat and key load on the event loop would wedge boot on a stalled data home."""

    @pytest.mark.asyncio
    async def test_no_worker_thread_exits_instead_of_running_inline(
        self, monkeypatch, tmp_path: Path, capsys
    ) -> None:
        from unittest.mock import MagicMock

        orch = gateway.GatewayOrchestrator.__new__(gateway.GatewayOrchestrator)
        monkeypatch.setattr(gateway.crash_guard, "install_loop_handler", MagicMock())
        monkeypatch.setattr(gateway.platform_compat, "raise_nofile_soft_limit", MagicMock())
        monkeypatch.setattr(gateway, "data_home", lambda: tmp_path)

        inline: list[str] = []
        monkeypatch.setattr(gateway, "signing_key_preflight", lambda: inline.append("ran"))

        async def _to_thread(fn, *args, **kwargs):
            return None  # the persistence probe passes

        def _no_thread(self) -> None:
            raise RuntimeError("can't start new thread")

        monkeypatch.setattr(gateway.asyncio, "to_thread", _to_thread)
        monkeypatch.setattr(gateway.threading.Thread, "start", _no_thread)
        with pytest.raises(SystemExit) as exit_info:
            await orch.run()
        assert exit_info.value.code == 1
        assert inline == [], "the preflight must never run on the event loop"
        assert "signing key preflight" in capsys.readouterr().err


class TestPreflightHasABootDeadline:
    """A key leaf on a disconnected mount must refuse the boot, not stall it."""

    @pytest.mark.asyncio
    async def test_a_stuck_preflight_refuses_the_boot_at_its_deadline(
        self, monkeypatch, tmp_path: Path, capsys
    ) -> None:
        import threading
        from unittest.mock import MagicMock

        orch = gateway.GatewayOrchestrator.__new__(gateway.GatewayOrchestrator)
        monkeypatch.setattr(gateway.crash_guard, "install_loop_handler", MagicMock())
        monkeypatch.setattr(gateway.platform_compat, "raise_nofile_soft_limit", MagicMock())
        monkeypatch.setattr(gateway, "data_home", lambda: tmp_path)

        async def _to_thread(fn, *args, **kwargs):
            return None  # the persistence probe passes

        release = threading.Event()
        entered = threading.Event()
        workers: list[threading.Thread] = []

        def _stuck() -> None:
            workers.append(threading.current_thread())
            entered.set()
            release.wait(30)  # a filesystem call that never returns

        async def _deadline_after_entry(aw, timeout):
            # The deadline fires only once the worker is inside the preflight, so
            # the verdict never depends on how fast the thread got scheduled.
            loop = asyncio.get_running_loop()
            assert await loop.run_in_executor(None, entered.wait, 30), "worker never ran"
            raise TimeoutError

        monkeypatch.setattr(gateway.asyncio, "to_thread", _to_thread)
        monkeypatch.setattr(gateway.asyncio, "wait_for", _deadline_after_entry)
        monkeypatch.setattr(gateway, "signing_key_preflight", _stuck)
        try:
            with pytest.raises(SystemExit) as exit_info:
                await orch.run()
        finally:
            release.set()
            for worker in workers:
                worker.join(5)
        assert exit_info.value.code == 1
        assert "did not finish within" in capsys.readouterr().err
        assert workers and workers[0].daemon, "a stuck worker must not block interpreter exit"

    @pytest.mark.asyncio
    async def test_the_preflight_refusal_reaches_the_loop(self, monkeypatch) -> None:
        def _refuse() -> None:
            raise SystemExit(1)

        monkeypatch.setattr(gateway, "signing_key_preflight", _refuse)
        with pytest.raises(SystemExit) as exit_info:
            await gateway._run_signing_key_preflight_bounded(timeout=5)
        assert exit_info.value.code == 1

    @pytest.mark.asyncio
    async def test_a_passing_preflight_returns(self, monkeypatch) -> None:
        ran: list[str] = []
        monkeypatch.setattr(gateway, "signing_key_preflight", lambda: ran.append("ok"))
        await gateway._run_signing_key_preflight_bounded(timeout=5)
        assert ran == ["ok"]


class TestSigningKeyRemedy:
    """The printed remedy is pasted into a shell, and the data home is operator input."""

    @pytest.mark.parametrize(
        "hostile",
        ["plain", "with space", "it's", 'dq"uote', "$(touch pwned)", "`id`", "a;b", "semi&&colon"],
    )
    def test_posix_operands_are_single_shell_words_and_literal(
        self, tmp_path: Path, monkeypatch, hostile: str
    ) -> None:
        import shlex

        monkeypatch.setattr("platform.system", lambda: "Linux")
        key = tmp_path / hostile / ts._SECRET_KEY_FILE
        remedy = ts.signing_key_remedy(key, "kirocrew restart")
        commands = [step.split("Run: ", 1)[1] for step in remedy if "Run: " in step]
        check, remove, restart = (shlex.split(c) for c in commands)
        assert check == ["wc", "-c", "<", str(key)], "the path must round-trip as one word"
        assert remove == ["rm", str(key)], "the path must round-trip as one word"
        assert restart == ["kirocrew", "restart"]

    @pytest.mark.parametrize("system", ["Linux", "Darwin", "Windows"])
    def test_the_remedy_is_manual_steps_with_the_writer_stopped_first(
        self, tmp_path: Path, monkeypatch, system: str
    ) -> None:
        """Any printed one-liner that checks the length and then removes the file
        leaves a gap a concurrent restore can land in. So nothing is chained: the
        gateway and any restore are stopped before a person checks and deletes, and
        no copy of the key bytes is made at another name the sandbox does not mask."""
        monkeypatch.setattr("platform.system", lambda: system)
        steps = ts.signing_key_remedy(tmp_path / ts._SECRET_KEY_FILE, "kirocrew restart")
        assert [s.split(".")[0] for s in steps] == ["1", "2", "3", "4"]
        assert steps[0].startswith("1. Stop the gateway") and "restoring" in steps[0]
        assert "less than 32" in steps[1] and "skip to step 4" in steps[1]
        assert steps[2].startswith("3. Delete it by hand")
        joined = "\n".join(steps)
        for chained in ("&&", "||", "-delete", "Where-Object", ";", "| Remove-Item"):
            assert chained not in joined, f"no automated check-then-remove ({chained!r})"
        for aside in ("mv ", "Rename-Item", "Move-Item", "cp "):
            assert aside not in joined, "the key is never copied or moved to another name"

    @pytest.mark.parametrize("system", ["Linux", "Darwin"])
    @pytest.mark.parametrize(
        "restart",
        [
            "sudo systemctl restart kirocrew",
            "systemctl --user restart kirocrew",
            "kirocrew restart",
        ],
    )
    def test_posix_restart_is_the_callers_service_aware_command(
        self, tmp_path: Path, monkeypatch, system: str, restart: str
    ) -> None:
        """A per-user unit or a foreground gateway needs a different restart than the
        system unit; the caller's ``restart_command_hint`` knows which is installed."""
        monkeypatch.setattr("platform.system", lambda: system)
        assert (
            ts.signing_key_remedy(tmp_path / "k", restart)[-1]
            == f"4. Start the gateway. Run: {restart}"
        )

    def test_doctor_passes_the_service_hint(self) -> None:
        import inspect

        assert "restart_command_hint()" in inspect.getsource(doctor_access._doctor_signing_key)

    def test_the_boot_refusal_never_probes_the_service_install(
        self, tmp_path: Path, monkeypatch, capsys
    ) -> None:
        """restart_command_hint() stats the unit file under HOME, which can block
        without bound on a disconnected mount; the boot must refuse, not hang."""

        def _probe(*_a, **_kw):
            raise AssertionError("boot preflight must not probe the service install")

        monkeypatch.setattr(gateway, "restart_command_hint", _probe)
        key = tmp_path / ts._SECRET_KEY_FILE
        key.write_bytes(b"")
        monkeypatch.setattr(gateway.token_secret, "signing_key_health", lambda: ("short", key))
        monkeypatch.setattr(gateway.token_secret, "load_boot_secret", lambda: "short")

        with pytest.raises(SystemExit):
            gateway.signing_key_preflight()
        assert gateway.BOOT_RESTART_HINT in capsys.readouterr().err

    def test_windows_uses_a_literal_path_with_quotes_doubled(
        self, tmp_path: Path, monkeypatch
    ) -> None:
        monkeypatch.setattr("platform.system", lambda: "Windows")
        key = tmp_path / "it's" / ts._SECRET_KEY_FILE
        remedy = ts.signing_key_remedy(key, "kirocrew restart")
        literal = "'" + str(key).replace("'", "''") + "'"
        assert remedy[1].endswith(f"Run: (Get-Item -LiteralPath {literal}).Length")
        assert remedy[2].endswith(f"Run: Remove-Item -LiteralPath {literal}")
        assert "$(" not in "\n".join(remedy), "no POSIX substitution in a PowerShell line"

    @pytest.mark.parametrize("quote", ["\u2018", "\u2019", "\u201a", "\u201b"])
    def test_windows_doubles_the_curly_quotes_powershell_also_ends_on(
        self, tmp_path: Path, monkeypatch, quote: str
    ) -> None:
        """PowerShell closes a single-quoted string on U+2018..U+201B as well as on
        ASCII ', so an O\u2019Brien data home must not end the literal early."""
        monkeypatch.setattr("platform.system", lambda: "Windows")
        key = tmp_path / f"O{quote}Brien" / ts._SECRET_KEY_FILE
        remedy = ts.signing_key_remedy(key, "kirocrew restart")
        literal = "'" + str(key).replace(quote, quote * 2) + "'"
        assert remedy[2].endswith(f"Run: Remove-Item -LiteralPath {literal}")


class TestDoctorSigningKey:
    def test_healthy_key_prints_the_path(self, tmp_path: Path, monkeypatch, capsys) -> None:
        key = tmp_path / ts._SECRET_KEY_FILE
        monkeypatch.setattr(ts, "signing_key_health", lambda **kw: ("ok", key))
        issues: list[str] = []
        doctor_access._doctor_signing_key(issues)
        out = capsys.readouterr().out
        assert "signing key: ✅" in out and str(key) in out
        assert issues == []

    def test_fresh_home_is_informational_not_a_warning(
        self, tmp_path: Path, monkeypatch, capsys
    ) -> None:
        key = tmp_path / ts._SECRET_KEY_FILE
        monkeypatch.setattr(ts, "signing_key_health", lambda **kw: ("absent", key))
        issues: list[str] = []
        doctor_access._doctor_signing_key(issues)
        out = capsys.readouterr().out
        assert "not created yet" in out
        assert "⚠" not in out
        assert issues == []

    def test_short_key_names_the_consequence_and_the_remedy(
        self, tmp_path: Path, monkeypatch, capsys
    ) -> None:
        key = tmp_path / ts._SECRET_KEY_FILE
        monkeypatch.setattr(ts, "signing_key_health", lambda **kw: ("short", key))
        issues: list[str] = []
        doctor_access._doctor_signing_key(issues)
        out = capsys.readouterr().out
        assert "⚠ signing key" in out
        assert "refuses to start" in out
        assert (
            len(issues) == 1 and "truncated" in issues[0]
        ), "the short state must fail doctor, since the gateway refuses to boot on it"
        for step in ts.signing_key_remedy(
            key, doctor_access.cli_doctor.common_service.restart_command_hint()
        ):
            assert step in out

    @pytest.mark.skipif(
        os.name == "nt" or (hasattr(os, "geteuid") and os.geteuid() == 0),
        reason="needs POSIX permission bits that refuse this process",
    )
    def test_a_short_key_this_process_cannot_open_gets_no_removal_step(
        self, tmp_path: Path, monkeypatch, capsys
    ) -> None:
        """The Linux agent sandbox masks the key with a mode-0 stand-in. With the
        marker popped and no cgroup scope, both confinement probes miss, so the size
        reads as short. A removal step relayed from there would delete the healthy
        host key; refusing it on an open the process is denied closes that gap."""
        key = tmp_path / ts._SECRET_KEY_FILE
        key.write_bytes(b"")
        key.chmod(0)
        try:
            assert ts.signing_key_health(key)[0] == "short", "both confinement probes missed"
            issues: list[str] = []
            monkeypatch.setattr(ts, "signing_key_health", lambda **kw: ("short", key))
            doctor_access._doctor_signing_key(issues)
            out = capsys.readouterr().out
            assert "rm " not in out and "Delete it by hand" not in out
            assert "run doctor from the host" in out
            assert len(issues) == 0, "indistinguishable from the sandbox mask, so not counted"
        finally:
            key.chmod(0o600)

    @pytest.mark.skipif(
        os.name == "nt" or (hasattr(os, "geteuid") and os.geteuid() == 0),
        reason="needs POSIX permission bits that refuse this process",
    )
    def test_a_whole_key_this_process_cannot_open_fails_doctor(
        self, tmp_path: Path, monkeypatch, capsys
    ) -> None:
        """The boot falls back to an ephemeral secret on a full key it cannot read,
        so doctor must not print a green row for it, and must say the gateway
        starts rather than refuses."""
        key = tmp_path / ts._SECRET_KEY_FILE
        key.write_bytes(_FULL_KEY)
        key.chmod(0)
        try:
            monkeypatch.setattr(ts, "signing_key_health", lambda **kw: ("ok", key))
            issues: list[str] = []
            doctor_access._doctor_signing_key(issues)
            out = capsys.readouterr().out
            assert "✅" not in out and "cannot open it" in out
            assert len(issues) == 1 and "cannot be read" in issues[0]
            assert "ephemeral secret" in issues[0] and "ephemeral secret" in out
            assert "refuses" not in issues[0] and "refuses" not in out
            assert "rm " not in out and "Delete it by hand" not in out, "never removed"
        finally:
            key.chmod(0o600)

    def test_an_unstatable_key_fails_doctor(self, tmp_path: Path, monkeypatch, capsys) -> None:
        key = tmp_path / ts._SECRET_KEY_FILE
        monkeypatch.setattr(ts, "signing_key_health", lambda **kw: ("unstatable", key))
        issues: list[str] = []
        doctor_access._doctor_signing_key(issues)
        out = capsys.readouterr().out
        assert "could not be stat'ed" in out
        assert len(issues) == 1 and "stat" in issues[0]
        assert "ephemeral secret" in issues[0] and "refuse" not in out + issues[0]

    def test_a_readable_whole_key_stays_green(self, tmp_path: Path, monkeypatch, capsys) -> None:
        key = tmp_path / ts._SECRET_KEY_FILE
        key.write_bytes(_FULL_KEY)
        monkeypatch.setattr(ts, "signing_key_health", lambda **kw: ("ok", key))
        issues: list[str] = []
        doctor_access._doctor_signing_key(issues)
        assert "✅" in capsys.readouterr().out and issues == []

    def test_open_is_denied_only_for_a_refused_open(self, tmp_path: Path) -> None:
        readable = tmp_path / "readable"
        readable.write_bytes(b"\x01" * 5)
        assert ts.open_is_denied(readable) is False
        assert ts.open_is_denied(tmp_path / "missing") is False
        if os.name != "nt":
            link = tmp_path / "link"
            link.symlink_to(readable)
            assert ts.open_is_denied(link) is False, "a symlink is not followed or reported denied"

    def test_masked_inside_the_sandbox_is_reported_not_diagnosed(
        self, tmp_path: Path, monkeypatch, capsys
    ) -> None:
        """An agent relaying doctor output must never be handed the removal
        remedy for a key it cannot see."""
        key = tmp_path / ts._SECRET_KEY_FILE
        monkeypatch.setattr(ts, "signing_key_health", lambda **kw: ("masked", key))
        issues: list[str] = []
        doctor_access._doctor_signing_key(issues)
        out = capsys.readouterr().out
        assert "masked inside the agent sandbox" in out
        assert "⚠" not in out and "-delete" not in out
        assert issues == []

    def test_a_seatbelt_confined_doctor_reports_masked_without_the_marker(
        self, tmp_path: Path, monkeypatch, capsys
    ) -> None:
        """On macOS there is no cgroup to read and ``cli.main()`` has already popped
        the marker; the kernel's Seatbelt verdict is what keeps a sandboxed doctor
        from reading the denied key as unreadable and printing destructive advice."""
        key = tmp_path / ts._SECRET_KEY_FILE
        key.write_bytes(b"")
        monkeypatch.setattr("kiro_crew.config.loader.config_dir", lambda: tmp_path)
        monkeypatch.setattr(
            doctor_access.cli_doctor.sandbox,
            "agent_confinement_evidence",
            lambda: "the kernel reports this process is Seatbelt-confined",
        )
        issues: list[str] = []
        doctor_access._doctor_signing_key(issues)
        out = capsys.readouterr().out
        assert "masked inside the agent sandbox" in out
        assert "⚠" not in out
        assert issues == []

    def test_other_shapes_are_reported_without_a_remedy_that_could_mislead(
        self, tmp_path: Path, monkeypatch, capsys
    ) -> None:
        key = tmp_path / ts._SECRET_KEY_FILE
        monkeypatch.setattr(ts, "signing_key_health", lambda **kw: ("other", key))
        issues: list[str] = []
        doctor_access._doctor_signing_key(issues)
        out = capsys.readouterr().out
        assert "⚠ signing key" in out
        assert "not a regular file" in out
        assert "-delete" not in out
