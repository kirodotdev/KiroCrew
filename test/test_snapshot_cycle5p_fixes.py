"""One operator authorization must approve exactly one destination, even under a race."""

from __future__ import annotations

import json
import multiprocessing as mp
import os
import stat
import sys
from pathlib import Path

import pytest

from kiro_crew import snapshot_remote as remote

ACCOUNT = "123456789012"
REGION = "us-west-2"
BUCKET = "kirocrew-backup-123456789012-us-west-2"


def _write_token(tmp_home: Path) -> Path:
    token = remote.authorization_token_path()
    token.parent.mkdir(parents=True, exist_ok=True)
    token.write_text(json.dumps({"account": ACCOUNT, "region": REGION}), encoding="utf-8")
    return token


def _attempt(home: str, src: str, barrier, results) -> None:  # pragma: no cover - child
    os.environ["KIROCREW_HOME"] = home
    sys.path.insert(0, src)
    from kiro_crew import snapshot_remote as child_remote

    barrier.wait()
    try:
        child_remote.consume_authorization(ACCOUNT, REGION, BUCKET)
        results.append("won")
    except Exception as e:
        results.append(f"refused:{type(e).__name__}")


class TestOneAuthorizationApprovesOneDestination:
    def test_concurrent_setups_do_not_both_consume_it(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """The deletion is what makes it single-use, so the read must be serialised.

        Cross-PROCESS by necessity: two `backup setup` invocations are two processes, and
        an advisory lock does not serialise threads sharing one descriptor. Threads here
        would pass while the real case still raced.
        """
        monkeypatch.setenv("KIROCREW_HOME", str(tmp_path))
        token = _write_token(tmp_path)
        src = str(Path(remote.__file__).resolve().parents[2])

        ctx = mp.get_context("spawn")
        n = 4
        with ctx.Manager() as mgr:
            results = mgr.list()
            barrier = mgr.Barrier(n)
            procs = [
                ctx.Process(target=_attempt, args=(str(tmp_path), src, barrier, results))
                for _ in range(n)
            ]
            for p in procs:
                p.start()
            for p in procs:
                p.join(120)
                assert p.exitcode == 0, "a racing setup crashed instead of refusing"
            outcomes = list(results)

        assert (
            outcomes.count("won") == 1
        ), f"one authorization approved {outcomes.count('won')} destinations: {outcomes}"
        assert not token.is_file()

    def test_the_loser_is_told_it_was_consumed_not_that_the_json_is_broken(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Without the in-lock re-check the loser still refuses -- with the wrong reason.

        It would fall through to the body read, find the file gone, and report the
        authorization as unreadable JSON, sending the operator to inspect a file that no
        longer exists instead of telling them a concurrent setup took it.
        """
        monkeypatch.setenv("KIROCREW_HOME", str(tmp_path))
        token = _write_token(tmp_path)

        # Exactly the losing case: the token exists when we look, and a competitor holds
        # the lock and consumes it before we get in. Patching the critical section is how
        # that ordering is expressed without a production seam that exists only for tests.
        import contextlib

        @contextlib.contextmanager
        def consumed_by_a_competitor():
            token.unlink()
            yield

        monkeypatch.setattr(remote, "_authorization_held", consumed_by_a_competitor)

        with pytest.raises(remote.DestinationError) as caught:
            remote.consume_authorization(ACCOUNT, REGION, BUCKET)

        message = str(caught.value)
        assert "consumed by another" in message, message
        assert "JSON" not in message, "an absent token was reported as a malformed one: " + message

    def test_the_lock_is_actually_held_while_the_token_is_consumed(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Mutual exclusion, observed rather than raced for.

        The multi-process test above is real evidence but it depends on the OS scheduling
        the racers close together: under heavy load an UNSERIALISED build can serialise by
        accident and look correct. This asserts the property directly -- while the consume
        step runs, a second attempt to take the same lock must fail -- so it holds whatever
        the scheduler does.
        """
        monkeypatch.setenv("KIROCREW_HOME", str(tmp_path))
        token = _write_token(tmp_path)
        lock_path = token.parent / (token.name + ".lock")

        probe: dict[str, bool] = {}
        real_consume = remote._consume_verified_authorization

        def probing(*args, **kw):
            # A separate open file description, so flock conflicts even in-process.
            fd = os.open(lock_path, os.O_RDWR | os.O_CREAT, 0o600)
            try:
                probe["free"] = remote.platform_compat.try_acquire_lock(fd, exclusive=True)
                if probe["free"]:
                    remote.platform_compat.release_lock(fd)
            finally:
                os.close(fd)
            return real_consume(*args, **kw)

        monkeypatch.setattr(remote, "_consume_verified_authorization", probing)
        remote.consume_authorization(ACCOUNT, REGION, BUCKET)

        assert probe, "the consume step never ran"
        assert probe["free"] is False, (
            "the lock was free while the token was being consumed, so two setups can "
            "interleave regardless of what the scheduler happens to do"
        )

    def test_a_refused_attempt_does_not_burn_the_token(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Serialising must not change when the token is spent -- only on success."""
        monkeypatch.setenv("KIROCREW_HOME", str(tmp_path))
        token = _write_token(tmp_path)

        with pytest.raises(remote.DestinationError):
            remote.consume_authorization("111122223333", REGION, BUCKET)

        assert token.is_file(), "a mismatched attempt consumed the authorization"

    def test_the_lock_file_is_owner_only(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setenv("KIROCREW_HOME", str(tmp_path))
        token = _write_token(tmp_path)

        remote.consume_authorization(ACCOUNT, REGION, BUCKET)

        lock = token.parent / (token.name + ".lock")
        assert lock.is_file()
        if os.name != "nt":
            assert stat.S_IMODE(lock.stat().st_mode) == 0o600

    def test_an_existing_loose_lock_file_is_tightened(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """`os.open`'s mode applies only when it CREATES the file.

        A lock file left readable by a previous run -- or planted -- keeps those
        permissions unless they are re-asserted, which is the only case where this call
        does anything.
        """
        monkeypatch.setenv("KIROCREW_HOME", str(tmp_path))
        token = _write_token(tmp_path)
        lock = token.parent / (token.name + ".lock")
        lock.touch()
        lock.chmod(0o666)

        remote.consume_authorization(ACCOUNT, REGION, BUCKET)

        if os.name != "nt":
            assert (
                stat.S_IMODE(lock.stat().st_mode) == 0o600
            ), "a pre-existing world-readable lock file kept its permissions"

    def test_the_lock_is_not_taken_on_the_file_it_deletes(self) -> None:
        """A lock on a path that stops existing protects nobody still waiting on it."""
        import inspect

        src = inspect.getsource(remote._authorization_held)
        assert '+ ".lock"' in src, "the lock must be a dedicated sibling"

    def test_validation_and_deletion_run_inside_the_lock(self) -> None:
        import inspect

        src = inspect.getsource(remote.consume_authorization)
        assert "with _authorization_held():" in src
        held = src.index("with _authorization_held():")
        assert (
            src.index("_consume_verified_authorization(") > held
        ), "the validate-and-delete step must run inside the critical section"
