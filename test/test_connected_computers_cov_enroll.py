"""Connected computers: the SSH enrollment call's answers, refusals and failures.

No ssh runs: ``asyncio.create_subprocess_exec`` returns a scripted process and
the spawn environment is a pass-through, so each test fixes exactly what the
remote printed and how it exited.
"""

from __future__ import annotations

import asyncio
import json
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from kiro_crew.connected_computers import enroll
from kiro_crew.testing.wait import FALLBACK_TIMEOUT_SECS, finished

pytestmark = pytest.mark.asyncio

PEER_KEY = "ab" * 32
INSTANCE = SimpleNamespace(ssh_host="remote", remote_bin="", remote_port=0)


class _Proc:
    def __init__(self, returncode: int, stdout: bytes, *, raise_on_wait=None) -> None:
        self.returncode = returncode
        self.stdout = stdout
        self.raise_on_wait = raise_on_wait
        self.sent: list[bytes] = []

    async def communicate(self, data):
        self.sent.append(data)
        if self.raise_on_wait is not None:
            raise self.raise_on_wait
        return self.stdout, b""


@pytest.fixture
def ssh(monkeypatch):
    """Script the next ssh child; returns the spawn record and a setter."""
    state = SimpleNamespace(proc=None, spawn_error=None, argv=None, kwargs=None, reaped=[])

    async def spawn(*argv, **kwargs):
        state.argv = list(argv)
        state.kwargs = kwargs
        if state.spawn_error is not None:
            raise state.spawn_error
        return state.proc

    async def reap(proc):
        state.reaped.append(proc)

    monkeypatch.setattr(asyncio, "create_subprocess_exec", spawn)
    monkeypatch.setattr("kiro_crew.platform_compat.kill_and_reap", reap)
    monkeypatch.setattr(
        "kiro_crew.instances.token_mint.ssh_spawn_argv_env", lambda argv: (argv, {"E": "1"})
    )
    return state


def _line(obj) -> bytes:
    return json.dumps(obj).encode("utf-8") + b"\n"


async def _call(payload=None, *, subcommand=enroll.ENROLL_SUBCOMMAND, instance=INSTANCE):
    return await finished(
        enroll.run_remote(instance, subcommand, payload or {}, timeout=FALLBACK_TIMEOUT_SECS),
        "the scripted remote call",
    )


class TestAnswer:
    def test_the_last_object_line_wins_over_banners_and_broken_lines(self):
        out = b"Welcome to the host\n" + _line({"ok": True, "n": 1}) + b"{not json\n  \n"
        assert enroll._answer(out) == {"ok": True, "n": 1}

    def test_no_object_line_is_unavailable(self):
        with pytest.raises(enroll.EnrollmentUnavailable, match="gave no answer"):
            enroll._answer(b"banner only\n[1, 2]\n")

    def test_only_broken_object_lines_are_unavailable(self):
        with pytest.raises(enroll.EnrollmentUnavailable):
            enroll._answer(b"{oops\n{\n")


class TestRunRemote:
    async def test_an_unknown_subcommand_is_refused_before_any_spawn(self, ssh):
        with pytest.raises(ValueError, match="unknown remote subcommand"):
            await _call(subcommand="connected-computer devices")
        assert ssh.argv is None

    async def test_a_connection_with_no_ssh_host_is_refused(self, ssh):
        with pytest.raises(enroll.EnrollmentFailed, match="no SSH host") as info:
            await _call(instance=SimpleNamespace(ssh_host=""))
        assert info.value.code == ""
        assert ssh.argv is None

    async def test_an_ssh_that_cannot_start_is_unavailable(self, ssh):
        ssh.spawn_error = FileNotFoundError("ssh")
        with pytest.raises(enroll.EnrollmentUnavailable, match="FileNotFoundError"):
            await _call()

    async def test_the_request_rides_stdin_and_the_answer_comes_back(self, ssh):
        answer = {"ok": True, "device_id": "laptop"}
        ssh.proc = _Proc(0, b"motd\n" + _line(answer))
        payload = {"base": "laptop", "label": "My laptop"}
        assert await _call(payload) == answer
        assert "-n" not in ssh.argv
        assert ssh.argv[0] == "ssh" and "remote" in ssh.argv
        assert "My laptop" not in " ".join(ssh.argv), "nothing typed reaches the argv"
        assert ssh.proc.sent == [json.dumps(payload).encode("utf-8") + b"\n"]
        assert ssh.kwargs["env"] == {"E": "1"}
        assert ssh.kwargs["stdin"] is asyncio.subprocess.PIPE

    async def test_ssh_failing_to_connect_is_unavailable(self, ssh):
        ssh.proc = _Proc(255, b"")
        with pytest.raises(enroll.EnrollmentUnavailable, match="over SSH"):
            await _call()

    async def test_a_refusal_carries_a_valid_code(self, ssh):
        ssh.proc = _Proc(2, _line({"ok": False, "error": "access was revoked", "code": "revoked"}))
        with pytest.raises(enroll.EnrollmentFailed) as info:
            await _call()
        assert not isinstance(info.value, enroll.EnrollmentUnavailable)
        assert str(info.value) == "access was revoked"
        assert info.value.code == "revoked"
        assert info.value.terminal is True

    async def test_an_invalid_code_is_dropped_and_a_long_reason_is_cut(self, ssh):
        ssh.proc = _Proc(2, _line({"ok": False, "error": "x" * 500, "code": "Not-A-Code!"}))
        with pytest.raises(enroll.EnrollmentFailed) as info:
            await _call()
        assert str(info.value) == "x" * 300
        assert info.value.code == ""
        assert info.value.terminal is False

    async def test_an_exit_zero_without_ok_is_a_refusal_with_a_default_reason(self, ssh):
        ssh.proc = _Proc(0, _line({"status": "maybe"}))
        with pytest.raises(enroll.EnrollmentFailed, match="the remote gateway refused"):
            await _call()

    async def test_a_nonzero_exit_with_ok_true_is_still_a_refusal(self, ssh):
        ssh.proc = _Proc(1, _line({"ok": True}))
        with pytest.raises(enroll.EnrollmentFailed):
            await _call()

    async def test_a_cancelled_call_reaps_the_child_and_stays_cancelled(self, ssh):
        ssh.proc = _Proc(0, b"", raise_on_wait=asyncio.CancelledError())
        with pytest.raises(asyncio.CancelledError):
            await _call()
        assert ssh.reaped == [ssh.proc]


def _enrolled_answer(**overrides):
    answer = {
        "ok": True,
        "device_id": "laptop",
        "gateway_id": "gw-1",
        "gateway_label": "Remote box",
        "credential": "cred-1",
        "expires": 4_000_000_000.0,
        "peer_key": PEER_KEY,
        "cgen": 3,
    }
    answer.update(overrides)
    return answer


class TestEnrollAndRenew:
    async def test_enroll_sends_a_fresh_request_and_reads_the_answer(self, monkeypatch):
        remote = AsyncMock(return_value=_enrolled_answer())
        monkeypatch.setattr(enroll, "run_remote", remote)
        got = await finished(
            enroll.enroll(INSTANCE, base="laptop", fingerprint="fp", label="Laptop"), "enroll"
        )
        assert got == enroll.Enrolled(
            device_id="laptop",
            gateway_id="gw-1",
            gateway_label="Remote box",
            credential="cred-1",
            expires=4_000_000_000.0,
            peer_key=PEER_KEY,
            cgen=3,
        )
        args, kwargs = remote.call_args
        assert args[1] == enroll.ENROLL_SUBCOMMAND
        assert args[2] == {"base": "laptop", "fingerprint": "fp", "label": "Laptop", "renew": False}
        assert kwargs["timeout"] > 0

    async def test_renew_presents_the_old_credential(self, monkeypatch):
        remote = AsyncMock(return_value=_enrolled_answer(gateway_label=None))
        monkeypatch.setattr(enroll, "run_remote", remote)
        got = await finished(
            enroll.renew(INSTANCE, base="b", fingerprint="fp", label="L", credential="old"),
            "renew",
        )
        assert got.gateway_label == ""
        sent = remote.call_args.args[2]
        assert sent["renew"] is True and sent["credential"] == "old"

    async def test_renew_without_a_credential_never_calls_the_remote(self, monkeypatch):
        remote = AsyncMock()
        monkeypatch.setattr(enroll, "run_remote", remote)
        with pytest.raises(enroll.EnrollmentFailed, match="no credential to renew"):
            await finished(
                enroll.renew(INSTANCE, base="b", fingerprint="fp", label="L", credential=""),
                "renew without credential",
            )
        remote.assert_not_awaited()

    @pytest.mark.parametrize(
        "drop,overrides",
        [
            ("credential", {}),
            ("device_id", {}),
            ("", {"expires": "soon"}),
            ("", {"expires": None}),
        ],
    )
    async def test_an_incomplete_answer_is_refused(self, monkeypatch, drop, overrides):
        answer = _enrolled_answer(**overrides)
        answer.pop(drop, None)
        monkeypatch.setattr(enroll, "run_remote", AsyncMock(return_value=answer))
        with pytest.raises(enroll.EnrollmentFailed, match="incomplete"):
            await finished(enroll._enroll(INSTANCE, {}), "incomplete answer")

    @pytest.mark.parametrize("peer_key", ["", "AB" * 32, "ab" * 31])
    async def test_an_answer_without_a_usable_peer_key_is_refused(self, monkeypatch, peer_key):
        answer = _enrolled_answer(peer_key=peer_key)
        monkeypatch.setattr(enroll, "run_remote", AsyncMock(return_value=answer))
        with pytest.raises(enroll.EnrollmentFailed, match="incomplete"):
            await finished(enroll._enroll(INSTANCE, {}), "no peer key")

    async def test_forget_sends_the_scoped_request(self, monkeypatch):
        remote = AsyncMock(return_value={"ok": True})
        monkeypatch.setattr(enroll, "run_remote", remote)
        assert (
            await finished(
                enroll.forget(INSTANCE, device_id="laptop", fingerprint="fp", cgen=4), "forget"
            )
            is None
        )
        args = remote.call_args.args
        assert args[1] == enroll.FORGET_SUBCOMMAND
        assert args[2] == {"device_id": "laptop", "fingerprint": "fp", "cgen": 4}

    async def test_forget_names_the_gateway_that_made_the_enrollment(self, monkeypatch):
        remote = AsyncMock(return_value={"ok": True})
        monkeypatch.setattr(enroll, "run_remote", remote)
        await finished(
            enroll.forget(
                INSTANCE, device_id="laptop", fingerprint="fp", cgen=4, gateway_id="gw-a"
            ),
            "forget",
        )
        assert remote.call_args.args[2]["gateway_id"] == "gw-a"
