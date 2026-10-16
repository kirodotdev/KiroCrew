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
import json
import os
import stat
import sys
from pathlib import Path
from types import SimpleNamespace
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


async def _start_bootstrap(tmp_path: Path, *, compressed: bool = False):
    """The real bootstrap binds its own ephemeral port and reports it."""
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
        code, out, _ = await bootstrap.run(connect, SECRET, "echo hi; pwd")
        assert code == 0 and out.splitlines()[0] == b"hi"
        assert (home / "work").is_dir()  # the profile's workdir exists before any session
    finally:
        await _stop(process)


@posix_guest
@sync
async def test_enrolment_happens_once_and_nothing_runs_without_the_secret(tmp_path):
    process, connect, home = await _start_bootstrap(tmp_path)
    try:
        # Before enrolment no request is accepted at all.
        with pytest.raises(bootstrap.BootstrapError):
            await bootstrap.run(connect, SECRET, "true")
        await _enroll(connect, SECRET)
        # A second principal cannot take over the enrolled bootstrap.
        with pytest.raises(bootstrap.AlreadyEnrolled):
            await _enroll(connect, b"x" * 32)
        # And cannot run anything with a secret it does not hold.
        with pytest.raises(bootstrap.BootstrapError):
            await bootstrap.run(connect, b"x" * 32, "touch pwned")
        assert not (home / "pwned").exists()
        code, out, _ = await bootstrap.run(connect, SECRET, "cat", stdin=b"payload")
        assert (code, out) == (0, b"payload")
        code, _out, err = await bootstrap.run(connect, SECRET, "echo boom >&2; exit 3")
        assert code == 3 and "boom" in err
    finally:
        await _stop(process)


@posix_guest
@sync
async def test_a_reply_corrupted_in_the_tunnel_is_never_acted_on(tmp_path):
    process, connect, _home = await _start_bootstrap(tmp_path)
    try:
        await _enroll(connect, SECRET)
        flips = {"left": 1}

        async def corrupting():
            reader, writer = await connect()
            original = reader.read

            async def read(n=-1):
                data = await original(n)
                # Flip one byte of the RES payload once, as a bad tunnel might.
                if flips["left"] and data.startswith(b"RES "):
                    flips["left"] -= 1
                    data = data[:6] + bytes([data[6] ^ 1]) + data[7:]
                return data

            reader.read = read
            return reader, writer

        code, out, _ = await bootstrap.run(corrupting, SECRET, "echo clean")
        assert (code, out) == (0, b"clean\n") and flips["left"] == 0  # retried, verified
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
    launch = FargateWorkerLaunch("fg-test", FargateShell(connect, SECRET), tmp_path / "secrets")
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

    async def fresh_bootstrap():
        process, connect, home = await _start_bootstrap(tmp_path / f"b{len(state['processes'])}")
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
        await fresh_bootstrap()  # a new task boots a new bootstrap
        return task

    monkeypatch.setattr(tasks, "run_task", run_task)
    (tmp_path / "home").mkdir(exist_ok=True)
    envs = fargate_launch.FargateSessionEnvironments(
        SimpleNamespace(fargate_profiles=[PROFILE]), tmp_path / "home"
    )
    yield envs, state


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
async def test_a_task_someone_else_enrolled_first_is_stopped(provider, ecs, monkeypatch):
    envs, _state = provider
    original = bootstrap.enroll

    async def raced(connect, secret, **kwargs):
        await original(connect, b"z" * 32, **kwargs)  # another principal got there first
        await original(connect, secret, **kwargs)

    monkeypatch.setattr(bootstrap, "enroll", raced)
    with pytest.raises(SessionEnvironmentUnavailable, match="someone else"):
        await envs.prepare("dashboard:chat-3", "aws-dev")
    assert envs.registry.get("dashboard:chat-3") is None
    assert all(t["lastStatus"] == "STOPPED" for t in ecs.tasks.values())


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

    secret = "AKIAEXAMPLEEXAMPLE00"
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
        "dashboard:chat-5", "aws-dev"
    )
    seen = []

    async def prepare(self, key, profile_id):
        seen.append((key, profile_id))
        return "prepared"

    monkeypatch.setattr(fargate_launch.FargateSessionEnvironments, "prepare", prepare)
    assert await session_launch.prepare_for_session("dashboard:chat-5") == "prepared"
    assert seen == [("dashboard:chat-5", "aws-dev")]


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
        "dashboard:chat-6", "aws-dev"
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
    monkeypatch.setattr(session_environment, "session_runs_remotely", lambda _key: True)
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
    assert await retention.stop_for_shutdown() == {"coder": 0, "fargate": 0, "skipped": 2}
    assert stopped == []


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
