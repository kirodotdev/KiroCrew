"""The closed-list read ``crew-alive``, the guard's
user-asked set and one-shot read ticket, and the client's ``_require``. Set up Crew, the Resume
path, adoption and the panel are CR-B (test_managed_env_set_up_crew.py, vitest)."""

from __future__ import annotations

import socket
import subprocess

import pytest
from managed_env_fake_bff import FakeBff, FakeTime
from managed_env_helpers import fresh_shared_client, lane_values, no_real_install_ssh  # noqa: F401

from kiro_crew.managed_env import install
from kiro_crew.managed_env.client import BffClient
from kiro_crew.managed_env.guard import READ_TICKET_S, GuardRefused, SandboxGuard

FP = "fp-test0"
TOKEN = "tok-SECRET-0123456789"
ALIVE = install.COMMANDS_E[install.CMD_CREW_ALIVE][1]


class Clock:
    def __init__(self, t=1_000_000.0):
        self.t = t

    def __call__(self):
        return self.t


# ---- the one read: crew-alive under a real shell ----------------------------------------


@pytest.fixture
def box(tmp_path):
    """A fake box root: ``<root>/sandbox`` (its run/ dir), ``<root>/proc``, and a loopback
    listener on a free port (127.0.0.1 only, closed after) standing for whatever listens."""
    root = tmp_path / "box"
    (root / "sandbox" / "run").mkdir(parents=True)
    (root / "proc").mkdir()
    lsn = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    lsn.bind(("127.0.0.1", 0))
    lsn.listen(4)
    yield root, lsn.getsockname()[1]
    lsn.close()


def _alive(root, port):
    cmd = install.crew_alive_command(str(root / "sandbox"), str(root / "proc"), port)
    p = subprocess.run(
        ["bash", "-c", cmd], capture_output=True, text=True, encoding="utf-8", timeout=20
    )
    return p.returncode, p.stdout.strip()


def _pid(root, pid, cmdline):
    (root / "sandbox" / "run" / f"{install.HOOK_NAME}.pid").write_text(
        f"{pid}\n{install.GATEWAY_TOKEN}\n"
    )
    if cmdline is not None:
        (root / "proc" / str(pid)).mkdir()
        (root / "proc" / str(pid) / "cmdline").write_bytes(cmdline)


GATEWAY_CMDLINE = (
    b"/projects/.crew/venv/bin/python\x00-m\x00"
    + b"\x00".join(install.GATEWAY_TOKEN.encode().split(b" "))
    + b"\x00"
)


def test_crew_alive_reads_up_only_for_crews_own_gateway(box):
    root, port = box
    _pid(root, 4242, GATEWAY_CMDLINE)
    assert _alive(root, port) == (0, install.CREW_UP)


@pytest.mark.parametrize("how", ["no pidfile", "pid not a number", "no such pid", "other cmdline"])
def test_a_listener_on_the_port_that_isnt_crews_gateway_reads_down(box, how):
    root, port = box  # the port answers in every case: only Crew's own gateway reads up
    if how == "pid not a number":
        (root / "sandbox" / "run" / f"{install.HOOK_NAME}.pid").write_text("x1\n")
    elif how == "no such pid":
        _pid(root, 4243, None)
    elif how == "other cmdline":
        _pid(root, 4244, b"/usr/bin/python3\x00-m\x00http.server\x005476\x00")
    assert _alive(root, port) == (0, install.CREW_DOWN)


def test_crews_gateway_with_its_port_closed_reads_down(box):
    root, port = box
    _pid(root, 4245, GATEWAY_CMDLINE)
    s = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    s.bind(("127.0.0.1", 0))
    closed = s.getsockname()[1]
    s.close()  # nothing listens there now
    assert _alive(root, closed) == (0, install.CREW_DOWN)


def test_crew_alive_is_in_the_closed_list_and_commands_md():
    assert install.CMD_CREW_ALIVE == "crew-alive"
    text = install.commands_text()
    assert f"- `crew-alive`: {install.COMMANDS_E['crew-alive'][0]}\n\n```\n{ALIVE}\n```\n" in text
    # read-only: no write, kill, hook, install or config set in its text
    for word in ("rm ", "kill", "mv ", ">", "hook", "install", "config set", "chmod", "tee"):
        assert word not in ALIVE.replace("2>/dev/null", "").replace("3<>", ""), word


# ---- the guard: the user-asked set and the ticket ---------------------------------------------


def test_the_guard_grants_a_user_asked_id_resume_get_and_the_read_only(tmp_path):
    guard = SandboxGuard(tmp_path / "state")
    guard.accept_listed([{"execEnvironmentId": "env-9", "displayName": "my-box"}], FP)
    assert guard.ask("env-9", FP) and guard.is_asked("env-9")
    for op in ("resume", "get", "read"):
        guard.require_asked("env-9", op, lambda: FP)
    for op in ("stop", "delete", "update", "hook-run"):
        with pytest.raises(GuardRefused):
            guard.require_asked("env-9", op, lambda: FP)
    with pytest.raises(GuardRefused):
        guard.require_actionable("env-9", lambda: FP)  # still not actionable


@pytest.mark.parametrize("why", ["not listed", "recorded", "other sign-in"])
def test_a_click_asks_only_for_a_listed_box_with_no_record(tmp_path, why):
    guard = SandboxGuard(tmp_path / "state")
    guard.accept_listed(
        [
            {"execEnvironmentId": "env-9", "displayName": "my-box"},
            {"execEnvironmentId": "env-k", "displayName": "kc-0a1b2c"},
        ],
        FP,
    )
    sid = {"not listed": "env-7", "recorded": "env-k"}.get(why, "env-9")
    fp = "fp-other" if why == "other sign-in" else FP
    assert guard.ask(sid, fp) is False and not guard.is_asked(sid)


def test_a_sign_in_change_clears_the_user_asked_set(tmp_path):
    guard = SandboxGuard(tmp_path / "state")
    guard.accept_listed([{"execEnvironmentId": "env-9", "displayName": "my-box"}], FP)
    assert guard.ask("env-9", FP)
    with pytest.raises(GuardRefused):
        guard.require_asked("env-9", "resume", lambda: "fp-other")  # at call time
    assert not guard.is_asked("env-9")
    assert guard.ask("env-9", FP)
    guard.accept_listed([{"execEnvironmentId": "env-9", "displayName": "my-box"}], "fp-other")
    assert not guard.is_asked("env-9")  # another user's List clears it too


def test_a_ticket_armed_over_60_s_ago_is_refused(tmp_path):
    clock = Clock()
    guard = SandboxGuard(tmp_path / "state", now=clock)
    guard.accept_listed([{"execEnvironmentId": "env-9", "displayName": "my-box"}], FP)
    assert guard.ask("env-9", FP)
    guard.arm_ticket("env-9")
    clock.t += READ_TICKET_S - 1
    assert guard.take_ticket("env-9") is True
    assert guard.take_ticket("env-9") is False  # one use
    guard.arm_ticket("env-9")
    clock.t += READ_TICKET_S + 1
    assert guard.take_ticket("env-9") is False


def test_a_ticket_is_for_a_user_asked_id_only(tmp_path):
    guard = SandboxGuard(tmp_path / "state")
    guard.accept_listed([{"execEnvironmentId": "env-9", "displayName": "my-box"}], FP)
    guard.arm_ticket("env-9")  # armed, but nobody asked about env-9
    assert guard.take_ticket("env-9") is False


# ---- the client's _require -------------------------------------------------------------------


@pytest.fixture
def bff():
    b = FakeBff()
    yield b
    b.close()


def _client(bff, tmp_path, fp=FP):
    ft = FakeTime()
    guard = SandboxGuard(tmp_path / "state", now=ft.now)
    client = BffClient(lambda: TOKEN, guard, endpoint=bff.endpoint, sleep=ft.sleep, clock=ft.clock)
    client.accept_fingerprint = lambda: fp
    return client, guard


def test_the_client_takes_a_user_asked_id_for_resume_get_and_the_ticketed_read(bff, tmp_path):
    client, guard = _client(bff, tmp_path)
    sid = bff.add_other("STOPPED", "my-ide-box")
    client.list_sandboxes()
    assert guard.ask(sid, FP)
    client.resume_sandbox(sid)
    client.get_sandbox(sid)
    bff.sandboxes[sid]["status"] = "RUNNING"
    client.start_ssh_session(sid, read_ticket=True)
    assert bff.ops()[1:] == ["ResumeSandbox", "GetSandbox", "StartSshSession"]


@pytest.mark.parametrize("op", ["stop", "update", "delete", "ssh without the ticket"])
def test_the_client_refuses_a_user_asked_id_anything_else(bff, tmp_path, op):
    client, guard = _client(bff, tmp_path)
    sid = bff.add_other("RUNNING", "my-ide-box")
    client.list_sandboxes()
    assert guard.ask(sid, FP)
    call = {
        "stop": lambda: client.stop_sandbox(sid),
        "update": lambda: client.update_sandbox(sid, idle_timeout_minutes=60),
        "delete": lambda: client.delete_sandbox(sid),
        "ssh without the ticket": lambda: client.start_ssh_session(sid),
    }[op]
    with pytest.raises(GuardRefused):
        call()
    assert bff.ops() == ["ListSandboxes"]


def test_the_client_refuses_a_user_asked_id_after_a_sign_in_change(bff, tmp_path):
    client, guard = _client(bff, tmp_path)
    sid = bff.add_other("STOPPED", "my-ide-box")
    client.list_sandboxes()
    assert guard.ask(sid, FP)
    client.accept_fingerprint = lambda: "fp-other"
    with pytest.raises(GuardRefused):
        client.resume_sandbox(sid)
    assert bff.ops() == ["ListSandboxes"] and not guard.is_asked(sid)
