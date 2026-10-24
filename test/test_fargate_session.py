"""AWS Fargate session environments, without AWS.

The guest bootstrap runs for real, as a local child process: the "SSM forward"
in these tests is a plain loopback connection to it, which is exactly what the
forward hands the gateway. ECS is faked at the ``cloud.aws`` chokepoint, so
every ECS call the provider makes is visible and none reaches AWS.
"""

from __future__ import annotations

import asyncio
import contextlib
import functools
import hashlib
import json
import os
import stat
import sys
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock
from weakref import WeakValueDictionary

import pytest

from kiro_crew.coder import retention
from kiro_crew.fargate_session import bootstrap, fargate_profile
from kiro_crew.fargate_session import launch as fargate_launch
from kiro_crew.fargate_session import tasks
from kiro_crew.fargate_session.shell import FargateShell, FargateWorkerLaunch
from kiro_crew.remote_session import wire
from kiro_crew.remote_session.link import RemoteLink
from kiro_crew.session_environment import SessionEnvironmentUnavailable


@pytest.fixture(autouse=True)
def _developer_flag_on(_floor_monkeypatch):
    # Session Environments are reachable only with the developer flag.
    _floor_monkeypatch.setenv("KIROCREW_DEV_MODE", "1")


#: The real bootstrap runs ``/bin/sh`` like the Linux task it stands in for.
posix_guest = pytest.mark.skipif(sys.platform == "win32", reason="POSIX guest shell")

BOOTSTRAP = (
    Path(bootstrap.__file__).resolve().parents[1] / "remote_session" / "fargate_bootstrap.py"
)
SECRET = b"s" * 32


def sync(fn):
    @functools.wraps(fn)
    def wrapper(*args, **kwargs):
        async def run():
            try:
                return await asyncio.wait_for(fn(*args, **kwargs), 90)
            finally:
                if "provider" in kwargs:
                    for process in kwargs["provider"][1]["processes"]:
                        await _stop(process)

        return asyncio.run(run())

    return wrapper


async def _start_bootstrap(tmp_path: Path, *, compressed: bool = False, commit: str | None = None):
    """The real bootstrap binds its own ephemeral port and reports it.

    It enrols only ``SECRET`` unless ``commit`` names another SHA-256 ("" for none).
    """
    from kiro_crew import platform_compat
    from kiro_crew.fargate_session.forward import PortForward

    home = tmp_path / "guest"
    home.mkdir(parents=True, exist_ok=True)
    runtime = home / "runtime"
    runtime.mkdir(exist_ok=True)
    env = {
        "HOME": str(home),
        "PATH": os.path.dirname(sys.executable) + os.pathsep + os.environ.get("PATH", ""),
        "XDG_RUNTIME_DIR": str(runtime),
        "DBUS_SESSION_BUS_ADDRESS": f"unix:path={runtime / 'bus'}",
        "KIROCREW_BOOTSTRAP_PORT": "0",
        "KIROCREW_WORKDIR": str(home / "work"),
        "KIROCREW_BOOTSTRAP_COMMIT": (
            hashlib.sha256(SECRET).hexdigest() if commit is None else commit
        ),
    }
    argv = bootstrap.container_command() if compressed else ["python3", str(BOOTSTRAP)]
    argv = [sys.executable, *argv[1:]]
    process = await asyncio.create_subprocess_exec(
        *argv,
        env=env,
        cwd=str(home),
        stdout=asyncio.subprocess.DEVNULL,
        stderr=asyncio.subprocess.PIPE,
        start_new_session=True,
    )

    async def ready():
        assert process.stderr is not None
        while True:
            line = await process.stderr.readline()
            if not line:
                raise RuntimeError("bootstrap stderr closed before readiness")
            if line.startswith(b"crew-bootstrap: listening on 127.0.0.1:"):
                return int(line.rsplit(b":", 1)[1])

    try:
        port = await asyncio.wait_for(ready(), 30)
        forward = PortForward("local-test-child", port)
        forward._proc = SimpleNamespace(pid=process.pid, poll=lambda: process.returncode)
        forward._start = await asyncio.to_thread(platform_compat.process_start_time, process.pid)
    except BaseException:
        await _stop(process)
        raise

    async def connect():
        return await asyncio.open_connection("127.0.0.1", port, limit=64 * 1024 * 1024)

    connect.verify_peer = forward.verify_peer
    return process, connect, home


async def _enroll(connect, secret):
    await bootstrap.enroll(connect, secret, verify_peer=connect.verify_peer)


async def _stop(process) -> None:
    if process.returncode is None:
        process.kill()
    await asyncio.wait_for(process.wait(), 30)


# ── The bootstrap protocol, against a real bootstrap ─────────────────────


@posix_guest
@sync
async def test_the_compressed_container_command_runs_the_bootstrap(tmp_path):
    process, connect, home = await _start_bootstrap(tmp_path, compressed=True)
    try:
        await _enroll(connect, SECRET)
        code, out, _ = await bootstrap.run(
            connect, SECRET, "echo hi; pwd", verify_peer=connect.verify_peer
        )
        assert code == 0 and out.splitlines()[0] == b"hi"
        assert (home / "work").is_dir()  # the profile's workdir exists before any session
    finally:
        await _stop(process)


def test_the_task_overrides_stay_under_the_runtask_cap():
    """RunTask refuses overrides past 8 KiB, so a bootstrap that grows past it fails every launch."""
    request = tasks.run_task_request(
        {"cluster": "c", "task_definition": "td:1", "subnets": ["s"], "security_groups": ["g"]},
        "dashboard:chat-1",
        container="app",
        command=bootstrap.container_command(),
        environment={
            "KIROCREW_WORKDIR": "/" + "w" * 255,
            "KIROCREW_BOOTSTRAP_PORT": str(bootstrap.BOOTSTRAP_PORT),
            "KIROCREW_BOOTSTRAP_COMMIT": "0" * 64,
        },
    )
    size = len(json.dumps(request["overrides"], separators=(",", ":")).encode())
    assert size < 8192, f"RunTask overrides are {size} bytes; the cap is 8192"


@posix_guest
@sync
async def test_enrolment_happens_once_and_nothing_runs_without_the_secret(tmp_path):
    process, connect, home = await _start_bootstrap(tmp_path)
    try:
        # Before enrolment no request is accepted at all.
        with pytest.raises(bootstrap.BootstrapError):
            await bootstrap.run(connect, SECRET, "true", verify_peer=connect.verify_peer)
        await _enroll(connect, SECRET)
        # A second principal cannot take over the enrolled bootstrap.
        with pytest.raises(bootstrap.AlreadyEnrolled):
            await _enroll(connect, b"x" * 32)
        # And cannot run anything with a secret it does not hold.
        with pytest.raises(bootstrap.BootstrapError):
            await bootstrap.run(connect, b"x" * 32, "touch pwned", verify_peer=connect.verify_peer)
        assert not (home / "pwned").exists()
        code, out, _ = await bootstrap.run(
            connect, SECRET, "cat", stdin=b"payload", verify_peer=connect.verify_peer
        )
        assert (code, out) == (0, b"payload")
        code, _out, err = await bootstrap.run(
            connect, SECRET, "echo boom >&2; exit 3", verify_peer=connect.verify_peer
        )
        assert code == 3 and "boom" in err
    finally:
        await _stop(process)


@posix_guest
@sync
async def test_a_local_process_that_reaches_the_bootstrap_first_cannot_enrol(tmp_path):
    """Only the secret the launch committed to enrols, whoever connects first."""
    process, connect, home = await _start_bootstrap(tmp_path)
    try:
        # A process racing the gateway to the forward sends its own secret first.
        with pytest.raises(bootstrap.BootstrapError, match="ERR"):
            await _enroll(connect, b"x" * 32)
        with pytest.raises(bootstrap.BootstrapError):
            await bootstrap.run(connect, b"x" * 32, "touch pwned", verify_peer=connect.verify_peer)
        assert not (home / "pwned").exists()
        # The gateway's committed secret still enrols afterwards.
        await _enroll(connect, SECRET)
        assert (await bootstrap.run(connect, SECRET, "echo mine", verify_peer=connect.verify_peer))[
            :2
        ] == (0, b"mine\n")
    finally:
        await _stop(process)


@posix_guest
@sync
async def test_a_bootstrap_without_a_commitment_enrols_nothing(tmp_path):
    process, connect, _home = await _start_bootstrap(tmp_path, commit="")
    try:
        with pytest.raises(bootstrap.BootstrapError, match="ERR"):
            await _enroll(connect, SECRET)
    finally:
        await _stop(process)


@posix_guest
@sync
@pytest.mark.parametrize("fault", ["corrupt", "drop", "reset"])
async def test_a_reply_corrupted_in_the_tunnel_is_never_acted_on(tmp_path, fault):
    process, connect, home = await _start_bootstrap(tmp_path)
    try:
        await _enroll(connect, SECRET)
        flips = {"left": 1}

        async def faulty():
            reader, writer = await connect()
            original = reader.read

            async def read(n=-1):
                data = await original(n)
                # Flip one byte of the RES payload once, lose it, or reset the
                # tunnel under it, as a bad tunnel might.
                if flips["left"] and data.startswith(b"RES "):
                    flips["left"] -= 1
                    if fault == "reset":
                        raise ConnectionResetError("tunnel reset")
                    data = data[:6] + bytes([data[6] ^ 1]) + data[7:] if fault == "corrupt" else b""
                return data

            reader.read = read
            return reader, writer

        with pytest.raises(bootstrap.BootstrapError):
            await bootstrap.run(
                faulty, SECRET, 'echo x >> "$HOME/ran"; echo clean', verify_peer=connect.verify_peer
            )
        # The guest ran it once; the gateway never sent it again after the bad answer.
        assert flips["left"] == 0 and (home / "ran").read_text() == "x\n"
    finally:
        await _stop(process)


@posix_guest
@sync
async def test_a_tunnel_reset_after_the_enrolment_was_sent_is_a_bootstrap_error(tmp_path):
    """So the launch retries it and reaches its lost-reply recovery, not a raw OSError."""
    process, connect, _home = await _start_bootstrap(tmp_path)
    try:

        async def reset_reply():
            reader, writer = await connect()
            original = reader.read

            async def read(n=-1):
                data = await original(n)
                if data.startswith(b"ENROLLED "):
                    raise ConnectionResetError("tunnel reset")
                return data

            reader.read = read
            return reader, writer

        reset_reply.verify_peer = connect.verify_peer
        with pytest.raises(bootstrap.BootstrapError) as raised:
            await _enroll(reset_reply, SECRET)
        assert not isinstance(raised.value, bootstrap.AlreadyEnrolled)
        # The guest did commit the secret: a second enrolment is refused as already done.
        with pytest.raises(bootstrap.AlreadyEnrolled):
            await _enroll(connect, SECRET)
    finally:
        await _stop(process)


@posix_guest
@sync
async def test_no_request_is_sent_on_a_connection_that_is_not_the_tasks_forward(tmp_path):
    """A process that took the local port first never reads a command, its stdin or a pipe."""
    process, connect, home = await _start_bootstrap(tmp_path)
    try:
        await _enroll(connect, SECRET)

        async def foreign(_writer):
            raise SessionEnvironmentUnavailable("the connected peer is not the task's SSM forward")

        with pytest.raises(SessionEnvironmentUnavailable, match="not the task's SSM forward"):
            await bootstrap.run(connect, SECRET, 'touch "$HOME/ran"', verify_peer=foreign)
        with pytest.raises(SessionEnvironmentUnavailable, match="not the task's SSM forward"):
            await bootstrap.pipe(connect, SECRET, 12345, verify_peer=foreign)
        assert not (home / "ran").exists()
        # The same connection, attested, is served.
        code, _out, _err = await bootstrap.run(
            connect, SECRET, 'touch "$HOME/ran"', verify_peer=connect.verify_peer
        )
        assert code == 0 and (home / "ran").exists()
    finally:
        await _stop(process)


@posix_guest
@sync
@pytest.mark.parametrize("before_greeting", ["eof", "reset"])
async def test_a_call_that_never_reached_the_bootstrap_is_retried(tmp_path, before_greeting):
    process, connect, home = await _start_bootstrap(tmp_path)
    try:
        await _enroll(connect, SECRET)
        attempts = {"n": 0}

        async def flaky():
            attempts["n"] += 1
            if attempts["n"] == 1:
                raise ConnectionRefusedError("forward not up yet")
            if attempts["n"] == 2:
                reader = asyncio.StreamReader()
                if before_greeting == "eof":
                    reader.feed_eof()  # closed before the greeting
                else:
                    reader.set_exception(ConnectionResetError("tunnel reset"))
                return reader, SimpleNamespace(close=lambda: None)
            return await connect()

        code, out, _ = await bootstrap.run(
            flaky, SECRET, 'echo x >> "$HOME/ran"; echo clean', verify_peer=connect.verify_peer
        )
        assert (code, out) == (0, b"clean\n") and attempts["n"] == 3
        assert (home / "ran").read_text() == "x\n"
    finally:
        await _stop(process)


@posix_guest
@sync
async def test_a_cr_terminated_line_is_still_a_line(tmp_path):
    """The SSM forward can turn a write's final LF into CR; that must not wedge."""
    process, connect, _home = await _start_bootstrap(tmp_path)
    try:
        reader, writer = await connect()
        lines = bootstrap._Lines(reader)
        hello = await lines.line()
        writer.write(b"ENROLL " + SECRET.hex().encode() + b"\r")
        await writer.drain()
        answer = await lines.line()
        writer.close()
        assert hello.startswith(b"KCB1 ") and answer.startswith(b"ENROLLED ")
    finally:
        await _stop(process)


# ── Guest workers through the bootstrap: the real wire, end to end ────────


ENGINE = "import sys\nfor line in sys.stdin:\n    sys.stdout.write(line); sys.stdout.flush()\n"


@posix_guest
@sync
async def test_a_guest_worker_is_installed_started_and_dialled_through_the_bootstrap(tmp_path):
    process, connect, home = await _start_bootstrap(tmp_path)
    link = None
    worker_id = ""
    launch = FargateWorkerLaunch("fg-test", FargateShell(connect, SECRET, connect.verify_peer))
    try:
        await _enroll(connect, SECRET)
        await launch.install()
        assert (home / ".kirocrew-worker" / "worker.py").exists()
        worker_id = launch.new_worker_id()
        secret = wire.new_secret()
        port = await launch.start_worker(worker_id, ["python3", "-u", "-c", ENGINE], secret)
        link = RemoteLink(
            worker_id,
            secret,
            launch.dial(port),
            keepalive_seconds=1,
            stall_seconds=5,
            lease_seconds=20,
        )
        link.start()
        await link.wait_connected(20)
        for size in (10, 1017, 4089, 200_000):
            line = json.dumps({"pad": "x" * size}).encode() + b"\n"
            await link.write(line)
            assert await asyncio.wait_for(link.stdout.readline(), 30) == line
        with pytest.raises(Exception, match="dialled through its bootstrap"):
            launch.dial_argv(port)
    finally:
        if link is not None:
            await link.aclose()
        if worker_id:
            with contextlib.suppress(Exception):
                await launch.stop_worker(worker_id)
        await _stop(process)


# ── ECS calls, faked at the aws chokepoint ───────────────────────────────


PROFILE = {
    "id": "aws-dev",
    "name": "AWS development",
    "aws_profile": "sandbox",
    "region": "us-east-1",
    "cluster": "dev",
    "task_definition": "session-dev:1",
    "subnets": ["subnet-12345678"],
    "security_groups": ["sg-12345678"],
    "assign_public_ip": False,
    "workdir": "/workspace",
}


class FakeECS:
    def __init__(self) -> None:
        self.calls: list[list[str]] = []
        self.requests: list[dict] = []
        self.entry_point: list | None = None
        self.tasks: dict[str, dict] = {}
        self.seq = 0

    def __call__(self, args, profile="", region="", *, action, timeout=60):
        self.calls.append(list(args))
        assert (profile, region) == ("sandbox", "us-east-1")
        op = args[1]
        if op == "describe-task-definition":
            container = {"name": "workspace", "essential": True}
            if self.entry_point:
                container["entryPoint"] = self.entry_point
            return {"taskDefinition": {"containerDefinitions": [container]}}
        if op == "run-task":
            request = json.loads(args[args.index("--cli-input-json") + 1])
            self.requests.append(request)
            self.seq += 1
            task_id = f"task{self.seq:04d}abcdef"
            cluster = request["cluster"].rsplit("/", 1)[-1]  # ECS answers with the name
            arn = f"arn:aws:ecs:us-east-1:123456789012:task/{cluster}/{task_id}"
            self.tasks[arn] = {
                "taskArn": arn,
                "lastStatus": "RUNNING",
                "containers": [
                    {
                        "runtimeId": f"{task_id}-1",
                        "managedAgents": [{"name": "ExecuteCommandAgent", "lastStatus": "RUNNING"}],
                    }
                ],
                "tags": request["tags"],
            }
            return {"tasks": [{"taskArn": arn}]}
        if op == "describe-tasks":
            arn = args[args.index("--tasks") + 1]
            return {"tasks": [self.tasks[arn]] if arn in self.tasks else []}
        if op == "stop-task":
            arn = args[args.index("--task") + 1]
            if arn in self.tasks:
                self.tasks[arn]["lastStatus"] = "STOPPED"
            return {"task": {"taskArn": arn}}
        raise AssertionError(f"unexpected aws call {args}")

    def ops(self):
        return [c[1] for c in self.calls]


@pytest.fixture
def ecs(monkeypatch):
    from kiro_crew.cloud import aws

    fake = FakeECS()
    monkeypatch.setattr(aws, "checked_json", fake)
    return fake


@sync
async def test_run_task_replaces_only_the_command_and_carries_no_secret(ecs):
    container = await tasks.task_container(PROFILE)
    task = await tasks.run_task(
        PROFILE,
        "dashboard:chat-9",
        container=container,
        command=bootstrap.container_command(),
        environment={"KIROCREW_WORKDIR": "/workspace"},
    )
    request = ecs.requests[-1]
    assert request["enableExecuteCommand"] is True and request["launchType"] == "FARGATE"
    assert request["startedBy"] == tasks.STARTED_BY
    entry = request["overrides"]["containerOverrides"][0]
    assert entry["name"] == "workspace" and entry["command"][:2] == ["python3", "-c"]
    assert set(request["overrides"]) == {"containerOverrides"}  # no role or size override
    assert request["networkConfiguration"]["awsvpcConfiguration"] == {
        "subnets": ["subnet-12345678"],
        "securityGroups": ["sg-12345678"],
        "assignPublicIp": "DISABLED",
    }
    # The session key never goes to AWS, only its hash, and the whole-Crew
    # lane's ownership marker is absent, so its teardown never claims the task.
    flat = json.dumps(request)
    assert "chat-9" not in flat and "kirocrew:managed" not in flat
    assert request["tags"] == [
        {"key": tasks.SESSION_TAG, "value": tasks.session_tag("dashboard:chat-9")}
    ]
    assert len(json.dumps(request["overrides"])) < 8192  # RunTask's override cap
    running = await tasks.wait_running(PROFILE, task, sleep=lambda s: asyncio.sleep(0))
    assert running.ssm_target == f"ecs:dev_{task.task_id}_{task.task_id}-1"


@sync
async def test_the_ssm_target_names_the_cluster_even_when_the_profile_uses_its_arn(ecs):
    profile = {**PROFILE, "cluster": "arn:aws:ecs:us-east-1:123456789012:cluster/dev"}
    task = await tasks.run_task(
        profile, "k", container="workspace", command=["true"], environment={}
    )
    # The task ARN's cluster segment, not the profile's ARN: SSM needs the name.
    assert task.cluster == "dev"
    running = await tasks.wait_running(profile, task, sleep=lambda s: asyncio.sleep(0))
    assert running.ssm_target == f"ecs:dev_{task.task_id}_{task.task_id}-1"


def test_both_fargate_lanes_place_a_task_with_the_same_builder():
    from kiro_crew.cloud.fargate.runtask import Placement, network_configuration

    placement = Placement("dev", ("subnet-12345678",), ("sg-12345678",), True)
    request = tasks.run_task_request(
        {**PROFILE, "assign_public_ip": True}, "k", container="c", command=["true"], environment={}
    )
    assert request["networkConfiguration"] == network_configuration(placement)
    assert network_configuration(placement)["awsvpcConfiguration"]["assignPublicIp"] == "ENABLED"


@sync
async def test_an_image_with_an_entry_point_is_refused_before_anything_runs(ecs):
    ecs.entry_point = ["/docker-entrypoint.sh"]
    with pytest.raises(SessionEnvironmentUnavailable, match="entryPoint"):
        await tasks.task_container(PROFILE)
    assert "run-task" not in ecs.ops()


@sync
async def test_a_task_that_stops_while_starting_says_why(ecs):
    task = await tasks.run_task(
        PROFILE, "k", container="workspace", command=["true"], environment={}
    )
    ecs.tasks[task.arn].update(lastStatus="STOPPED", stoppedReason="CannotPullContainerError")
    with pytest.raises(SessionEnvironmentUnavailable, match="CannotPullContainerError"):
        await tasks.wait_running(PROFILE, task, sleep=lambda s: asyncio.sleep(0))


@sync
async def test_both_fargate_lanes_read_a_described_task_through_one_mapping(ecs, monkeypatch):
    from kiro_crew.cloud import fargate_engine

    task = await tasks.run_task(
        PROFILE, "dashboard:chat-9", container="workspace", command=["true"], environment={}
    )
    read: list[str] = []
    real = fargate_engine.sighting_from_task

    def counted(info):
        read.append(str(info.get("taskArn")))
        return real(info)

    monkeypatch.setattr(fargate_engine, "sighting_from_task", counted)
    info = await tasks.describe(PROFILE, task)
    assert tasks.carries_session_tag(info, "dashboard:chat-9")
    assert not tasks.carries_session_tag(info, "dashboard:chat-8")
    await tasks.wait_running(PROFILE, task, sleep=lambda s: asyncio.sleep(0))
    # Status and tags come from the Crew lane's sighting, never a second mapping.
    assert len(read) >= 3 and set(read) == {task.arn}


@sync
async def test_an_aws_refusal_becomes_a_session_environment_error(monkeypatch):
    from kiro_crew.cloud import aws

    def refuse(*_args, **_kwargs):
        raise aws.AWSError("AccessDenied", action="ecs:RunTask")

    monkeypatch.setattr(aws, "checked_json", refuse)
    with pytest.raises(SessionEnvironmentUnavailable, match="AWS refused ecs:RunTask"):
        await tasks.run_task(PROFILE, "k", container="c", command=["true"], environment={})


@sync
async def test_ecs_starting_no_task_is_reported(monkeypatch):
    from kiro_crew.cloud import aws

    monkeypatch.setattr(
        aws, "checked_json", lambda *a, **k: {"tasks": [], "failures": [{"reason": "RESOURCE"}]}
    )
    with pytest.raises(SessionEnvironmentUnavailable, match="ECS started no task"):
        await tasks.run_task(PROFILE, "k", container="c", command=["true"], environment={})


# ── The provider: launch, reuse, refuse, archive ─────────────────────────


@pytest.fixture
def provider(tmp_path, ecs, monkeypatch):
    """FargateSessionEnvironments whose 'SSM forward' reaches a local bootstrap."""
    state: dict = {"processes": [], "connect": None, "homes": []}

    class LocalForward:
        def __init__(self, target, remote_port, **_kw):
            self.target = target
            self.remote_port = remote_port

        async def connect(self):
            return await state["connect"]()

        async def stop(self):
            pass

        async def verify_peer(self, writer):
            await state["connect"].verify_peer(writer)

    async def fresh_bootstrap(commit):
        process, connect, home = await _start_bootstrap(
            tmp_path / f"b{len(state['processes'])}", commit=commit
        )
        state["processes"].append(process)
        state["homes"].append(home)
        state["connect"] = connect

    monkeypatch.setattr(fargate_launch, "PortForward", LocalForward)
    monkeypatch.setattr(fargate_launch, "_FORWARDS", {})
    monkeypatch.setattr(fargate_launch, "_ENROLL_GAP_SECS", 0.05)
    monkeypatch.setattr(retention, "_LIFECYCLES", WeakValueDictionary())
    original_run = tasks.run_task

    async def run_task(*args, **kwargs):
        task = await original_run(*args, **kwargs)
        # A new task boots a new bootstrap, committed as the launch requested.
        await fresh_bootstrap(kwargs["environment"]["KIROCREW_BOOTSTRAP_COMMIT"])
        return task

    monkeypatch.setattr(tasks, "run_task", run_task)
    (tmp_path / "home").mkdir(exist_ok=True)
    envs = fargate_launch.FargateSessionEnvironments(
        SimpleNamespace(fargate_profiles=[PROFILE]), tmp_path / "home"
    )
    yield envs, state


@pytest.fixture
def gateway_only(tmp_path, ecs, monkeypatch):
    """FargateSessionEnvironments against the fake ECS, with no guest bootstrap.

    For gateway-side paths that end before any task is reached (a failed first
    registry write, say), so they run on every platform the gateway runs on.
    """
    monkeypatch.setattr(fargate_launch, "_FORWARDS", {})
    monkeypatch.setattr(retention, "_LIFECYCLES", WeakValueDictionary())
    (tmp_path / "home").mkdir(exist_ok=True)
    return fargate_launch.FargateSessionEnvironments(
        SimpleNamespace(fargate_profiles=[PROFILE]), tmp_path / "home"
    )


@posix_guest
@sync
async def test_a_session_gets_a_task_and_keeps_it_while_it_lives(provider, ecs, tmp_path):
    envs, _state = provider
    first = await envs.prepare("dashboard:chat-1", "aws-dev")
    assert ecs.ops().count("run-task") == 1 and first.created
    row = envs.registry.get("dashboard:chat-1")
    assert row and row.runtime_id
    assert envs.load_secret(row.task_id) is not None
    secret_file = tmp_path / "home" / ".vault" / "secrets.enc"
    assert stat.S_IMODE(secret_file.stat().st_mode) == 0o600
    assert not (tmp_path / "home" / "fargate" / "secrets").exists()
    assert first.workdir == "/workspace" and first.profile_id == "aws-dev"
    assert first.profile_name == "AWS development" and first.workspace.startswith("task ")
    # The launch really reaches the task's bootstrap.
    assert await first.launch.shell.run("echo ok") == b"ok\n"
    again = await envs.prepare("dashboard:chat-1", "aws-dev")
    assert ecs.ops().count("run-task") == 1 and not again.created  # reused, not relaunched
    assert await again.launch.shell.run("echo still") == b"still\n"
    assert await envs.shell_for("dashboard:chat-1", "aws-dev") is not None
    assert await envs.shell_for("dashboard:other", "aws-dev") is None


@posix_guest
@sync
async def test_a_subagent_start_reports_its_stages_on_itself_not_its_parent(provider, ecs):
    # The task is keyed by the parent; the start stages go on the starting
    # session, the only one whose runtime start clears them.
    from kiro_crew import session_environment as se

    envs, _state = provider
    root, child = "dashboard:chat-root", "subagent:child-1"
    se.clear_start_stage(root)
    se.clear_start_stage(child)
    try:
        await envs.prepare(root, "aws-dev", progress_key=child)
        assert envs.registry.get(root) is not None and envs.registry.get(child) is None
        assert se.start_progress(root) is None
        assert se.start_progress(child)["stage"] == "connecting"
    finally:
        se.clear_start_stage(root)
        se.clear_start_stage(child)


@posix_guest
@sync
@pytest.mark.parametrize("failure", [OSError("vault busy"), ValueError("bad vault entry")])
async def test_a_vault_read_failure_never_stops_a_live_task(provider, ecs, monkeypatch, failure):
    envs, _state = provider
    await envs.prepare("dashboard:chat-vault", "aws-dev")
    row = envs.registry.get("dashboard:chat-vault")

    def unreadable(_name):
        raise failure

    monkeypatch.setattr(envs._secrets, "get", unreadable)
    with pytest.raises(SessionEnvironmentUnavailable, match="vault"):
        await envs.prepare("dashboard:chat-vault", "aws-dev")
    with pytest.raises(SessionEnvironmentUnavailable, match="vault"):
        await envs.shell_for("dashboard:chat-vault", "aws-dev")
    # The task and its files survive; nothing was stopped or relaunched.
    assert ecs.tasks[row.arn]["lastStatus"] == "RUNNING"
    assert "stop-task" not in ecs.ops() and ecs.ops().count("run-task") == 1
    assert envs.registry.get("dashboard:chat-vault") == row


@posix_guest
@sync
async def test_a_task_without_our_live_tag_is_neither_stopped_nor_adopted(provider, ecs):
    envs, _state = provider
    await envs.prepare("dashboard:chat-2", "aws-dev")
    old = envs.registry.get("dashboard:chat-2")
    ecs.tasks[old.arn]["tags"] = [{"key": tasks.SESSION_TAG, "value": "someone-else"}]
    prepared = await envs.prepare("dashboard:chat-2", "aws-dev")
    assert prepared.created
    assert envs.registry.get("dashboard:chat-2").arn != old.arn
    assert envs.load_secret(old.task_id) is None
    assert ecs.tasks[old.arn]["lastStatus"] == "RUNNING"
    assert "stop-task" not in ecs.ops()
    assert ecs.ops().count("run-task") == 2


@posix_guest
@sync
async def test_an_enrolment_whose_reply_was_lost_is_still_ours(provider, ecs, monkeypatch):
    envs, _state = provider
    original = bootstrap.enroll
    calls: list[str] = []

    async def lossy(connect, secret, **kwargs):
        try:
            await original(connect, secret, **kwargs)
        except bootstrap.AlreadyEnrolled:
            calls.append("refused")
            raise
        calls.append("enrolled")
        if calls == ["enrolled"]:
            # The guest committed the secret, but the ENROLLED reply never arrived.
            raise bootstrap.BootstrapError("the bootstrap closed the connection")

    monkeypatch.setattr(bootstrap, "enroll", lossy)
    prepared = await envs.prepare("dashboard:chat-3", "aws-dev")
    assert calls == ["enrolled", "refused"]  # the retry was answered ERR enrolled
    assert await prepared.launch.shell.run("echo ours") == b"ours\n"
    assert envs.registry.get("dashboard:chat-3") is not None
    assert "stop-task" not in ecs.ops()


@posix_guest
@sync
async def test_a_task_someone_else_enrolled_first_is_stopped(provider, ecs, monkeypatch):
    envs, _state = provider

    async def taken(connect, secret, **kwargs):
        # ERR enrolled, and the bootstrap does not hold our secret: our no-op is refused.
        raise bootstrap.AlreadyEnrolled("the task's bootstrap was already enrolled")

    monkeypatch.setattr(bootstrap, "enroll", taken)
    with pytest.raises(SessionEnvironmentUnavailable, match="someone else"):
        await envs.prepare("dashboard:chat-3", "aws-dev")
    assert envs.registry.get("dashboard:chat-3") is None
    assert "stop-task" in ecs.ops()
    assert all(t["lastStatus"] == "STOPPED" for t in ecs.tasks.values())


@sync
async def test_a_task_whose_first_record_fails_is_stopped_at_once(gateway_only, ecs, monkeypatch):
    """No row names the task, so nothing could stop it later: stop it now."""
    envs = gateway_only

    def full(_row):
        raise OSError(28, "No space left on device")

    monkeypatch.setattr(envs.registry, "put", full)
    with pytest.raises(OSError, match="No space left"):
        await envs.prepare("dashboard:chat-full", "aws-dev")
    assert ecs.ops().count("run-task") == 1 and "stop-task" in ecs.ops()
    assert all(t["lastStatus"] == "STOPPED" for t in ecs.tasks.values())


@sync
async def test_a_failed_stop_after_a_failed_record_keeps_the_record_error(
    gateway_only, ecs, monkeypatch
):
    envs = gateway_only
    stops = []

    def full(_row):
        raise OSError(28, "No space left on device")

    async def refused(*args, **_kwargs):
        stops.append(args[1].arn)
        raise SessionEnvironmentUnavailable("AWS refused ecs:StopTask")

    monkeypatch.setattr(envs.registry, "put", full)
    monkeypatch.setattr(tasks, "stop_task", refused)
    with pytest.raises(OSError, match="No space left"):
        await envs.prepare("dashboard:chat-full", "aws-dev")
    # The stop was tried on the task RunTask returned; its refusal hid nothing.
    assert stops == [task["taskArn"] for task in ecs.tasks.values()]


@posix_guest
@sync
async def test_a_stranger_racing_the_launch_cannot_take_the_task(provider, ecs, monkeypatch):
    envs, _state = provider
    original = bootstrap.enroll
    refused: list[BaseException] = []

    async def raced(connect, secret, **kwargs):
        try:
            await original(connect, b"\x5a" * 32, **kwargs)  # a local process got there first
        except bootstrap.BootstrapError as exc:
            refused.append(exc)
        await original(connect, secret, **kwargs)

    monkeypatch.setattr(bootstrap, "enroll", raced)
    prepared = await envs.prepare("dashboard:chat-race", "aws-dev")
    assert refused and not isinstance(refused[0], bootstrap.AlreadyEnrolled)
    assert await prepared.launch.shell.run("echo ours") == b"ours\n"
    assert "stop-task" not in ecs.ops()
    # The RunTask override carries the commitment, never the secret itself.
    row = envs.registry.get("dashboard:chat-race")
    environment = {
        e["name"]: e["value"]
        for e in ecs.requests[-1]["overrides"]["containerOverrides"][0]["environment"]
    }
    secret = envs.load_secret(row.task_id)
    assert environment["KIROCREW_BOOTSTRAP_COMMIT"] == hashlib.sha256(secret).hexdigest()
    assert secret.hex() not in json.dumps(ecs.requests[-1])


@posix_guest
@sync
async def test_a_stop_whose_record_changed_is_refused_not_reported(provider, ecs, monkeypatch):
    """A concurrent edit of the row means no stop and no retry intent: say so."""
    envs, _state = provider
    await envs.prepare("dashboard:chat-race-stop", "aws-dev")
    row = envs.registry.get("dashboard:chat-race-stop")
    monkeypatch.setattr(envs.registry, "mark_stopping", lambda *_args: False)
    with pytest.raises(SessionEnvironmentUnavailable, match="changed while stopping"):
        await envs.archive_session("dashboard:chat-race-stop")
    assert "stop-task" not in ecs.ops()
    assert ecs.tasks[row.arn]["lastStatus"] == "RUNNING"


@posix_guest
@sync
async def test_archiving_stops_the_task_at_once(provider, ecs, tmp_path):
    envs, _state = provider
    await envs.prepare("dashboard:chat-4", "aws-dev")
    row = envs.registry.get("dashboard:chat-4")
    assert await envs.archive_session("dashboard:chat-4") is True
    assert ecs.tasks[row.arn]["lastStatus"] == "STOPPED"
    assert envs.registry.get("dashboard:chat-4") is None
    assert envs.load_secret(row.task_id) is None
    assert await envs.archive_session("dashboard:local") is False


@sync
async def test_a_task_of_a_removed_profile_is_still_stopped_in_its_region(tmp_path, monkeypatch):
    from kiro_crew.cloud import aws

    seen: list[tuple[str, str, list]] = []
    arn = "arn:aws:ecs:eu-west-2:123456789012:task/dev/abc123"

    def checked(args, p="", r="", **_kwargs):
        seen.append((p, r, args))
        return {
            "tasks": [
                {
                    "taskArn": arn,
                    "tags": [{"key": tasks.SESSION_TAG, "value": tasks.session_tag("k")}],
                }
            ]
        }

    monkeypatch.setattr(aws, "checked_json", checked)
    envs = fargate_launch.FargateSessionEnvironments(SimpleNamespace(), tmp_path)
    envs._record_task(
        fargate_launch.SessionTask("k", "gone", arn, "abc123", "dev", "r"),
        {"aws_profile": "launch-profile"},
    )
    assert await envs.archive_session("k") is True
    assert [entry[2][1] for entry in seen] == ["describe-tasks", "stop-task"]
    assert all((p, r) == ("launch-profile", "eu-west-2") for p, r, _ in seen)
    assert envs.registry.get("k") is None


def test_an_unreadable_registry_fails_closed(tmp_path):
    registry = fargate_launch.SessionTaskRegistry(tmp_path / "session-tasks.json")
    assert registry.all() == []
    (tmp_path / "session-tasks.json").write_text("{not json", encoding="utf-8")
    with pytest.raises(SessionEnvironmentUnavailable, match="unreadable"):
        registry.get("k")
    (tmp_path / "session-tasks.json").write_text('{"version": 9, "tasks": {}}', encoding="utf-8")
    with pytest.raises(SessionEnvironmentUnavailable, match="unreadable"):
        registry.all()


def test_a_newer_task_row_is_never_removed_for_an_older_arn(tmp_path):
    registry = fargate_launch.SessionTaskRegistry(tmp_path / "session-tasks.json")
    registry.put(fargate_launch.SessionTask("k", "p", "arn-new", "new1", "dev"))
    assert registry.remove("k", "arn-old") is False
    assert registry.get("k").arn == "arn-new"
    assert registry.remove("k", "arn-new") is True and registry.get("k") is None


# ── Wiring: config, dispatch, listing, archive, shutdown ─────────────────


def _section(**values):
    from kiro_crew.config.session_environments import validate_session_environments

    return validate_session_environments({"coder_url": "", "profiles": [], **values})


def test_profile_ids_may_not_name_both_a_coder_and_a_fargate_profile():
    from kiro_crew.config.session_environments import SessionEnvironmentsError

    coder = {"id": "aws-dev", "name": "c", "template": "t", "workdir": "/w"}
    with pytest.raises(SessionEnvironmentsError, match="both a Coder and a Fargate"):
        _section(coder_url="https://c.example.com", profiles=[coder], fargate_profiles=[PROFILE])


def test_a_fargate_profile_may_be_the_default():
    saved = _section(fargate_profiles=[PROFILE], default_profile="aws-dev")
    assert saved["default_profile"] == "aws-dev" and saved["fargate_profiles"] == [PROFILE]


@pytest.mark.parametrize(
    "malformed",
    ["not-an-object", {**PROFILE, "id": "Not Valid!"}, {**PROFILE, "name": "Duplicate"}],
    ids=["not-an-object", "bad-entry", "duplicate-id"],
)
def test_one_hand_edited_fargate_entry_does_not_drop_the_valid_ones(malformed):
    """A save that omits the Fargate list carries it as the loader reads it."""
    from kiro_crew.dashboard.handlers.core_config_patch import (
        _keep_omitted_session_environment_keys,
    )

    saved = {
        "coder_url": "",
        "profiles": [],
        "fargate_profiles": [PROFILE, malformed],
        "default_profile": "aws-dev",
    }
    merged = _keep_omitted_session_environment_keys(saved, {"coder_url": "", "profiles": []})
    assert merged["fargate_profiles"] == [PROFILE]
    assert merged["default_profile"] == "aws-dev"


@pytest.mark.parametrize(
    ("field", "value", "message"),
    [
        ("aws_profile", "--debug", "AWS profile name"),
        ("region", "mars", "AWS region"),
        ("cluster", "dev; rm -rf", "ECS name or ARN"),
        ("task_definition", "--cli-input-json", "ECS name or ARN"),
        ("subnets", [], "1-16 IDs"),
        ("subnets", ["subnet-12345678", "subnet-12345678"], "duplicate"),
        ("security_groups", ["sg-nothex!"], "invalid ID"),
        ("assign_public_ip", "yes", "true or false"),
        ("workdir", "relative/dir", "absolute"),
    ],
)
def test_a_fargate_profile_is_checked_field_by_field(field, value, message):
    from kiro_crew.config.session_environments import (
        SessionEnvironmentsError,
        validate_fargate_profiles,
    )

    with pytest.raises(SessionEnvironmentsError, match=message):
        validate_fargate_profiles([{**PROFILE, field: value}])


def test_a_fargate_profile_refuses_unknown_and_missing_keys():
    from kiro_crew.config.session_environments import (
        SessionEnvironmentsError,
        validate_fargate_profiles,
    )

    with pytest.raises(SessionEnvironmentsError, match="unknown key"):
        validate_fargate_profiles([{**PROFILE, "secret_key": "x"}])
    with pytest.raises(SessionEnvironmentsError, match="missing key"):
        validate_fargate_profiles([{k: v for k, v in PROFILE.items() if k != "region"}])
    with pytest.raises(SessionEnvironmentsError, match="duplicate"):
        validate_fargate_profiles([PROFILE, PROFILE])
    with pytest.raises(SessionEnvironmentsError, match="at most"):
        validate_fargate_profiles([{**PROFILE, "id": f"p{i}"} for i in range(21)])


def test_loading_keeps_valid_fargate_profiles_and_never_logs_values(caplog):
    from kiro_crew.config.sections import SessionEnvironmentsConfig

    # A plain sentinel, not a credential shape: a log redactor that masks key
    # patterns would hide a key-shaped value even if the loader logged it.
    secret = "fargate-profile-value-never-logged"
    raw = {
        "coder_url": "https://c.example.com",
        "profiles": [{"id": "shared", "name": "c", "template": "t", "workdir": "/w"}],
        "fargate_profiles": [
            PROFILE,
            {**PROFILE, "id": "broken", "aws_profile": secret, "region": "x"},
            {**PROFILE, "id": "shared"},
            PROFILE,
        ],
    }
    loaded = SessionEnvironmentsConfig.from_raw(raw)
    # Ambiguous ids stay visible to the resolver, which refuses them at launch.
    assert [p["id"] for p in loaded.fargate_profiles] == ["aws-dev", "shared"]
    assert secret not in caplog.text
    assert SessionEnvironmentsConfig.from_raw({"fargate_profiles": "nope"}).fargate_profiles == []


def test_the_config_writer_accepts_the_fargate_list_and_classifies_it_as_code_exec():
    from kiro_crew import change_card_catalog
    from kiro_crew.dashboard.handlers import core

    spec = core._EDITABLE_CONFIG["session_environments.fargate_profiles"]
    assert spec["normalize"]([PROFILE]) == [PROFILE]
    assert "session_environments.fargate_profiles" in change_card_catalog._CODE_EXEC_SETTINGS


def test_fargate_profiles_are_offered_without_a_coder_url(monkeypatch):
    from kiro_crew.config import loader
    from kiro_crew.dashboard.handlers import session_environments as handlers

    section = SimpleNamespace(coder_url="", profiles=[], fargate_profiles=[PROFILE])
    monkeypatch.setattr(
        loader.KiroCrewConfig,
        "load",
        classmethod(lambda cls: SimpleNamespace(session_environments=section)),
    )
    assert handlers._profiles() == [
        {"id": "aws-dev", "name": "AWS development", "provider": "fargate", "workdir": "/workspace"}
    ]
    assert fargate_profile(section, "aws-dev") is PROFILE
    assert fargate_profile(section, "other") is None


@sync
async def test_a_fargate_choice_is_prepared_by_the_fargate_provider(monkeypatch, tmp_path):
    from kiro_crew.coder import session_launch
    from kiro_crew.config import live, paths

    monkeypatch.setattr(paths, "data_home", lambda: tmp_path)
    section = SimpleNamespace(coder_url="", profiles=[], fargate_profiles=[PROFILE])
    monkeypatch.setattr(live, "snapshot", lambda: SimpleNamespace(session_environments=section))
    session_launch.SessionEnvironmentChoices(tmp_path / "coder" / "session-choices.json").set(
        "dashboard:chat-5", "aws-dev", "fargate"
    )
    seen = []

    async def prepare(self, key, profile_id, *, progress_key=None):
        seen.append((key, profile_id, progress_key))
        return "prepared"

    monkeypatch.setattr(fargate_launch.FargateSessionEnvironments, "prepare", prepare)
    assert await session_launch.prepare_for_session("dashboard:chat-5") == "prepared"
    assert seen == [("dashboard:chat-5", "aws-dev", "dashboard:chat-5")]
    # A subagent reuses its root's task, but its start is reported on itself.
    choices = session_launch.SessionEnvironmentChoices(tmp_path / "coder" / "session-choices.json")
    assert choices.inherit("subagent:child-5", "dashboard:chat-5") == "aws-dev"
    assert await session_launch.prepare_for_session("subagent:child-5") == "prepared"
    assert seen[-1] == ("dashboard:chat-5", "aws-dev", "subagent:child-5")


@posix_guest
@sync
async def test_the_files_panel_reads_a_fargate_task_through_its_bootstrap(
    provider, ecs, monkeypatch, tmp_path
):
    from kiro_crew.coder import session_browse, session_launch
    from kiro_crew.config import live, paths

    envs, state = provider
    home = tmp_path / "home"
    monkeypatch.setattr(paths, "data_home", lambda: home)
    monkeypatch.setattr(
        live, "snapshot", lambda: SimpleNamespace(session_environments=envs._config)
    )
    session_launch.SessionEnvironmentChoices(home / "coder" / "session-choices.json").set(
        "dashboard:chat-6", "aws-dev", "fargate"
    )
    with pytest.raises(session_browse.WorkspaceNotRunning):
        await session_browse.browse_session("dashboard:chat-6", "list", ".")
    prepared = await envs.prepare("dashboard:chat-6", "aws-dev")
    # The task's workdir, as the profile names it, holds a file to list.
    workdir = state["homes"][0] / "work"
    (workdir / "notes.txt").write_text("hi\n", encoding="utf-8")
    monkeypatch.setitem(PROFILE, "workdir", str(workdir))
    await prepared.launch.install()
    listing = await session_browse.browse_session("dashboard:chat-6", "list", ".")
    assert "notes.txt" in json.dumps(listing)
    with pytest.raises(SessionEnvironmentUnavailable, match="no terminal"):
        await session_browse.remote_terminal_argv("dashboard:chat-6")


@sync
async def test_the_readiness_check_reads_each_fargate_task_definition(ecs, monkeypatch):
    from kiro_crew.cloud import ssm
    from kiro_crew.dashboard.handlers import session_environments as handlers

    monkeypatch.setattr(ssm, "session_manager_plugin_installed", lambda: True)
    with_entry = {**PROFILE, "id": "with-entry", "task_definition": "with-entry:1"}

    def per_profile(args, profile="", region="", *, action, timeout=60):
        ecs.entry_point = ["/init"] if "with-entry:1" in args else None
        return FakeECS.__call__(ecs, args, profile, region, action=action, timeout=timeout)

    monkeypatch.setattr("kiro_crew.cloud.aws.checked_json", per_profile)
    result = await handlers._check_fargate(SimpleNamespace(fargate_profiles=[PROFILE, with_entry]))
    assert result[0] == {"id": "aws-dev", "ok": True}
    assert result[1]["ok"] is False and "entryPoint" in result[1]["error"]
    assert "run-task" not in ecs.ops()  # a check never starts a task
    monkeypatch.setattr(ssm, "session_manager_plugin_installed", lambda: False)
    result = await handlers._check_fargate(SimpleNamespace(fargate_profiles=[PROFILE]))
    assert result == [
        {
            "id": "aws-dev",
            "ok": False,
            "error": "the AWS Session Manager plugin is not installed on the gateway",
        }
    ]
    assert await handlers._check_fargate(SimpleNamespace()) == []


def _record_task(home: Path, key: str = "dashboard:chat-7", runtime: str = "r1") -> None:
    registry = fargate_launch.SessionTaskRegistry(home / "fargate" / "session-tasks.json")
    registry.put(
        fargate_launch.SessionTask(
            key,
            "aws-dev",
            "arn:aws:ecs:us-east-1:1:task/dev/abc123def",
            "abc123def",
            "dev",
            runtime,
        )
    )


def test_the_workspace_list_and_the_lock_see_a_fargate_task(monkeypatch, tmp_path):
    from kiro_crew.config import paths
    from kiro_crew.dashboard.handlers import session_environments as handlers

    monkeypatch.setattr(paths, "data_home", lambda: tmp_path)
    assert handlers._remote_workspace_created("dashboard:chat-7") is False
    _record_task(tmp_path)
    assert handlers._remote_workspace_created("dashboard:chat-7") is True
    (tmp_path / "fargate" / "session-tasks.json").write_text("garbage", encoding="utf-8")
    # Unreadable: fail closed, as if the task existed.
    assert handlers._remote_workspace_created("dashboard:chat-7") is True


@sync
async def test_a_missing_coder_cli_keeps_the_fargate_and_git_answers(monkeypatch):
    """A Coder URL with no coder binary must not hide a Fargate profile's failure."""
    from kiro_crew.coder import session_launch
    from kiro_crew.config import loader
    from kiro_crew.dashboard.handlers import session_environments as handlers
    from kiro_crew.remote_session import git_gateway
    from kiro_crew.session_environment import SessionEnvironmentUnavailable

    async def owner(_request, _scope):
        return None

    async def fargate(_section):
        return [{"id": "aws-dev", "ok": False, "error": "plugin missing"}]

    async def git():
        return [{"host": "github.com", "ok": True}]

    def no_cli(*_args, **_kwargs):
        raise SessionEnvironmentUnavailable("the coder CLI is not installed")

    section = SimpleNamespace(coder_url="https://coder.example.test")
    monkeypatch.setattr(handlers, "require_owner_dashboard_request", owner)
    monkeypatch.setattr(handlers, "_check_fargate", fargate)
    monkeypatch.setattr(git_gateway, "git_readiness", git)
    monkeypatch.setattr(
        loader.KiroCrewConfig, "load", lambda: SimpleNamespace(session_environments=section)
    )
    monkeypatch.setattr(session_launch, "CoderSessionEnvironments", no_cli)
    body = json.loads((await handlers.api_session_environments_check(SimpleNamespace())).body)
    assert body["coder"] == {"ok": False, "error": "the coder CLI is not installed"}
    assert body["fargate"] == [{"id": "aws-dev", "ok": False, "error": "plugin missing"}]
    assert body["git"] == [{"host": "github.com", "ok": True}]


@sync
async def test_the_workspaces_route_lists_fargate_tasks(monkeypatch, tmp_path):
    from kiro_crew.config import paths
    from kiro_crew.dashboard.handlers import session_environments as handlers

    monkeypatch.setattr(paths, "data_home", lambda: tmp_path)
    _record_task(tmp_path, runtime="")

    async def owner(_request, _scope):
        return None

    monkeypatch.setattr(handlers, "require_owner_dashboard_request", owner)
    response = await handlers.api_session_environments_workspaces_get(SimpleNamespace())
    rows = json.loads(response.body)["workspaces"]
    assert rows == [
        {
            "workspace": "abc123def",
            "provider": "fargate",
            "kind": "session",
            "slot": "chat-7",
            "profile": "aws-dev",
            "state": "starting",
            "template": "aws-dev",
            "created_at": "",
            "deletion_due_at": "",
        }
    ]


@sync
async def test_archiving_a_fargate_session_stops_its_task_under_the_lifecycle_lock(monkeypatch):
    monkeypatch.setattr(retention, "_LIFECYCLES", WeakValueDictionary())
    monkeypatch.setattr(retention, "_TASKS", set())
    stopped: list[str] = []
    notices: list[tuple[str, str]] = []

    class Envs:
        async def has_task(self, key):
            return key == "dashboard:fg"

        async def archive_session(self, key):
            assert retention.session_lifecycle(key).lock.locked()
            stopped.append(key)
            return True

    async def no_coder():
        return None

    async def fargate_envs():
        return Envs()

    async def unsaved(_key):
        return "2 uncommitted files"

    from kiro_crew import session_environment
    from kiro_crew.coder import session_browse

    monkeypatch.setattr(retention, "_environments", no_coder)
    monkeypatch.setattr(retention, "_fargate_environments", fargate_envs)
    monkeypatch.setattr(
        session_environment, "session_runs_remotely", lambda key: key == "dashboard:fg"
    )
    monkeypatch.setattr(session_browse, "unsaved_work", unsaved)
    retention.schedule_archived(("dashboard:fg", "dashboard:local"), lambda *n: notices.append(n))
    await asyncio.gather(*retention._TASKS)
    assert stopped == ["dashboard:fg"]
    assert len(notices) == 1 and "AWS Fargate task" in notices[0][1]


@sync
async def test_a_resume_supersedes_a_queued_fargate_archive(monkeypatch):
    monkeypatch.setattr(retention, "_LIFECYCLES", WeakValueDictionary())
    monkeypatch.setattr(retention, "_TASKS", set())
    stopped: list[str] = []

    class Envs:
        async def has_task(self, _key):
            return True

        async def archive_session(self, key):
            stopped.append(key)

    async def no_coder():
        return None

    async def fargate_envs():
        return Envs()

    monkeypatch.setattr(retention, "_environments", no_coder)
    monkeypatch.setattr(retention, "_fargate_environments", fargate_envs)
    lifecycle = retention.session_lifecycle("dashboard:fg")
    async with lifecycle.lock:
        retention.schedule_archived(("dashboard:fg",))
        lifecycle.generation += 1  # what prepare does before it takes the lock
    await asyncio.gather(*retention._TASKS)
    assert stopped == []


@sync
async def test_a_damaged_fargate_registry_never_stalls_coder_archive_or_sweep(
    tmp_path, monkeypatch
):
    monkeypatch.setattr(retention, "_LIFECYCLES", WeakValueDictionary())
    monkeypatch.setattr(retention, "_TASKS", set())
    envs = fargate_launch.FargateSessionEnvironments(SimpleNamespace(), tmp_path)
    envs.registry.path.parent.mkdir(parents=True)
    envs.registry.path.write_text("{not json", encoding="utf-8")
    with pytest.raises(SessionEnvironmentUnavailable):
        envs.registry.all()  # fails closed, as designed
    coder = object()
    archived: list[str] = []
    swept: list[object] = []

    async def fargate_envs():
        return envs

    async def coder_envs():
        return coder

    async def archive_session(environments, key):
        assert environments is coder
        archived.append(key)

    async def sweep_due(environments):
        swept.append(environments)
        return 2

    async def survives(_environments, _key):
        return True

    monkeypatch.setattr(retention, "_fargate_environments", fargate_envs)
    monkeypatch.setattr(retention, "_environments", coder_envs)
    monkeypatch.setattr(retention, "_archive_session", archive_session)
    monkeypatch.setattr(retention, "_binding_survives_stop", survives)
    monkeypatch.setattr(retention, "sweep_due", sweep_due)
    retention.schedule_archived(("dashboard:coder",))
    await asyncio.gather(*retention._TASKS)
    assert archived == ["dashboard:coder"]
    assert await retention.sweep_once() == 2 and swept == [coder]


@sync
async def test_shutdown_skips_every_fargate_task(monkeypatch):
    stopped: list[tuple[str, str]] = []
    rows = [SimpleNamespace(session_key="a"), SimpleNamespace(session_key="b")]

    class Envs:
        registry = SimpleNamespace(all=lambda: rows)

        async def archive_session(self, key, reason):
            stopped.append((key, reason))
            return True

    async def no_coder():
        return None

    async def fargate_envs():
        return Envs()

    monkeypatch.setattr(retention, "_environments", no_coder)
    monkeypatch.setattr(retention, "_fargate_environments", fargate_envs)
    # The gateway's own SSM forward children end with it; the tasks do not.
    forwards = {"t1": SimpleNamespace(stop=AsyncMock()), "t2": SimpleNamespace(stop=AsyncMock())}
    stuck = SimpleNamespace(stop=AsyncMock(side_effect=OSError("already gone")))
    monkeypatch.setattr(fargate_launch, "_FORWARDS", {**forwards, "t3": stuck})
    assert await retention.stop_for_shutdown() == {"coder": 0, "boxd": 0, "skipped": 2}
    assert stopped == []
    for forward in [*forwards.values(), stuck]:
        forward.stop.assert_awaited_once()
    assert fargate_launch._FORWARDS == {}


@sync
async def test_forwards_end_with_the_gateway_even_when_the_registry_cannot_be_read(monkeypatch):
    def unreadable():
        raise ValueError("unknown session-tasks.json version")

    class Envs:
        registry = SimpleNamespace(all=unreadable)

    async def no_coder():
        return None

    async def fargate_envs():
        return Envs()

    monkeypatch.setattr(retention, "_environments", no_coder)
    monkeypatch.setattr(retention, "_fargate_environments", fargate_envs)
    forward = SimpleNamespace(stop=AsyncMock())
    monkeypatch.setattr(fargate_launch, "_FORWARDS", {"t1": forward})
    # The read still fails closed (nothing counted), but the forwards still end.
    assert await retention.stop_for_shutdown() == {"coder": 0, "boxd": 0, "skipped": 0}
    forward.stop.assert_awaited_once()
    assert fargate_launch._FORWARDS == {}


@sync
async def test_forwards_end_with_the_gateway_even_when_stop_on_shutdown_is_off(monkeypatch):
    """The switch keeps the tasks; the gateway's own forward children never outlive it."""
    from kiro_crew.slack import gateway

    sweeper = asyncio.get_running_loop().create_future()
    sweeper.set_result(None)
    forward = SimpleNamespace(stop=AsyncMock())
    monkeypatch.setattr(gateway, "_CODER_RETENTION_TASK", sweeper)
    monkeypatch.setattr(fargate_launch, "_FORWARDS", {"t1": forward})
    monkeypatch.setattr(retention, "stop_on_shutdown_enabled", AsyncMock(return_value=False))
    stop = AsyncMock(return_value={})
    monkeypatch.setattr(retention, "stop_for_shutdown", stop)
    await gateway._stop_session_environments()
    stop.assert_not_awaited()  # no environment is stopped
    forward.stop.assert_awaited_once()
    assert fargate_launch._FORWARDS == {}


@sync
async def test_shutdown_does_not_load_the_fargate_launcher_when_it_never_ran(monkeypatch):
    """No forward exists before the launcher is loaded, so shutdown leaves it unloaded."""
    import sys

    monkeypatch.delitem(sys.modules, "kiro_crew.fargate_session.launch")
    await retention.close_fargate_forwards()
    assert "kiro_crew.fargate_session.launch" not in sys.modules


# ── The bootstrap's own lifetime ─────────────────────────────────────────


def test_the_bootstrap_exits_once_no_gateway_has_used_it(monkeypatch):
    """A gateway that is gone for good cannot stop the task; the bootstrap stops itself."""
    from kiro_crew.remote_session import fargate_bootstrap as boot

    now = [1000.0]
    monkeypatch.setitem(boot._activity, "last", 1000.0)
    monkeypatch.setitem(boot._activity, "pipes", 0)
    monkeypatch.setattr(boot, "_running", set())

    # An open worker pipe (a live gateway) and a running command both count as use.
    boot._activity["pipes"] = 1
    assert boot._idle_for(5000.0) == 0.0
    boot._activity["pipes"] = 0
    boot._running.add(4242)
    assert boot._idle_for(5000.0) == 0.0
    boot._running.clear()
    assert boot._idle_for(1600.0) == 600.0

    exited: list[float] = []

    def sleep(_secs):
        now[0] += 30.0

    boot._idle_watch(
        900.0, exit_fn=lambda: exited.append(now[0]), sleep=sleep, clock=lambda: now[0]
    )
    assert exited and exited[0] - 1000.0 > 900.0


def test_authenticated_use_resets_the_bootstrap_idle_clock(monkeypatch):
    from kiro_crew.remote_session import fargate_bootstrap as boot

    monkeypatch.setitem(boot._activity, "last", 0.0)
    boot._touch()
    assert boot._idle_for(boot._activity["last"] + 1.0) == 1.0
