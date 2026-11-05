"""boxd session environments, without boxd.

A fake ``boxd`` executable (a Python script) implements the CLI calls the
provider makes. Its ``machine exec`` really runs the command as a local child
with stdin and stdout streamed, which is what the real CLI does, so the guest
worker is installed, started and dialled exactly as on a real machine. Every
call is logged for the assertions.
"""

from __future__ import annotations

import asyncio
import contextlib
import functools
import json
import os
import stat
import subprocess
import sys
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace

import pytest

from kiro_crew.boxd_session import launch as boxd_launch
from kiro_crew.boxd_session.cli import BoxdCLI, BoxdError
from kiro_crew.remote_session import wire
from kiro_crew.remote_session.link import RemoteLink
from kiro_crew.session_environment import SessionEnvironmentUnavailable


@pytest.fixture(autouse=True)
def _developer_flag_on(_floor_monkeypatch):
    # Session Environments are reachable only with the developer flag.
    _floor_monkeypatch.setenv("KIROCREW_DEV_MODE", "1")


pytestmark = pytest.mark.skipif(sys.platform == "win32", reason="POSIX guest shell")

ENGINE = "import sys\nfor line in sys.stdin:\n    sys.stdout.write(line); sys.stdout.flush()\n"

FAKE_BOXD = r"""#!{python}
import json, os, subprocess, sys
state_path = os.environ["FAKE_BOXD_STATE"]
home = os.environ["FAKE_BOXD_HOME"]
def load():
    try:
        return json.load(open(state_path))
    except FileNotFoundError:
        return {{"machines": {{}}, "calls": []}}
state = load()
args = sys.argv[1:]
state["calls"].append(args)
def save():
    json.dump(state, open(state_path, "w"))
def out(obj):
    save(); print(json.dumps(obj)); sys.exit(0)
def fail(msg, code=1):
    save(); print("error: " + msg, file=sys.stderr); sys.exit(code)
if os.environ.get("FAKE_BOXD_SIGNED_OUT"):
    fail("not logged in; run boxd auth login")
m = state["machines"]
if args == ["auth", "--json"]:
    account = state.get("switched_to") or os.environ.get("FAKE_BOXD_ACCOUNT", "alice")
    if account == "unreadable": fail("auth unavailable")
    out({{"user": account, "orgs": [{{"name": account, "active": True}}]}})
if args[:2] == ["machine", "get"] and os.environ.get("FAKE_BOXD_SWITCH_ON_GET"):
    # An account switch lands mid-lookup: the new account cannot see the machine.
    state["switched_to"] = os.environ["FAKE_BOXD_SWITCH_ON_GET"]
    fail("VM '%s' not found" % args[2])
if args[:2] == ["machine", "list"]:
    out(list(m.values()))
if args[:2] == ["machine", "new"]:
    name = args[2]
    m[name] = {{"name": name, "id": "id-" + name, "status": "running", "args": args[3:]}}
    out({{"name": name, "id": "id-" + name}})
if args[:2] == ["machine", "get"]:
    if args[2] not in m: fail("VM '%s' not found" % args[2])
    out(m[args[2]])
if args[:2] == ["machine", "remove"]:
    if args[2] not in m: fail("VM '%s' not found" % args[2])
    del m[args[2]]; out({{"name": args[2], "status": "destroyed"}})
if args[:2] == ["machine", "hibernate"]:
    m[args[2]]["status"] = "hibernated"; out({{}})
if args[:3] == ["machine", "config", "set"]:
    m[args[3]][args[4]] = args[5]; out({{}})
if args[:2] == ["machine", "exec"]:
    if args[2] == "--tty": del args[2]  # a PTY exec; the test's pipe stands in for it
    if args[2] not in m: fail("VM '%s' not found" % args[2])
    m[args[2]]["status"] = "running"  # an exec wakes a machine
    save()
    cmd = args[args.index("--") + 1:]
    env = {{"HOME": home, "PATH": os.environ["PATH"],
           "XDG_RUNTIME_DIR": os.path.join(home, "runtime"),
           "DBUS_SESSION_BUS_ADDRESS": "unix:path=" + os.path.join(home, "runtime", "bus")}}
    sys.exit(subprocess.call(cmd, cwd=home, env=env))
fail("unknown command %r" % args, 2)
"""


def sync(fn):
    @functools.wraps(fn)
    def wrapper(*args, **kwargs):
        return asyncio.run(asyncio.wait_for(fn(*args, **kwargs), 90))

    return wrapper


@pytest.fixture
def fake(tmp_path, monkeypatch):
    home = tmp_path / "guest"
    home.mkdir()
    binary = tmp_path / "bin" / "boxd"
    binary.parent.mkdir()
    binary.write_text(FAKE_BOXD.format(python=sys.executable), encoding="utf-8")
    binary.chmod(binary.stat().st_mode | stat.S_IXUSR)
    state = tmp_path / "boxd-state.json"
    monkeypatch.setenv("FAKE_BOXD_STATE", str(state))
    monkeypatch.setenv("FAKE_BOXD_HOME", str(home))
    # HOME and PATH as a pair for the guest: python3 is this interpreter.
    monkeypatch.setenv("PATH", os.path.dirname(sys.executable) + os.pathsep + os.defpath)

    def read():
        return json.loads(state.read_text()) if state.exists() else {"machines": {}, "calls": []}

    return SimpleNamespace(bin=str(binary), home=home, read=read)


PROFILE = {"id": "bx-dev", "name": "boxd dev", "snapshot": "", "vcpu": 1, "workdir": ""}


def _envs(tmp_path, fake, **profile):
    prof = {**PROFILE, "workdir": str(fake.home / "work"), **profile}
    section = SimpleNamespace(boxd_profiles=[prof])
    (tmp_path / "data").mkdir(exist_ok=True)
    return boxd_launch.BoxdSessionEnvironments(section, tmp_path / "data", cli=BoxdCLI(fake.bin))


@sync
async def test_a_session_gets_a_machine_and_its_worker_is_dialled_through_exec(tmp_path, fake):
    envs = _envs(tmp_path, fake)
    prepared = await envs.prepare("dashboard:chat-1", "bx-dev")
    row = envs.registry.get("dashboard:chat-1")
    assert row is not None and row.name.startswith("kc-") and len(row.name) == 19
    assert prepared.workspace == row.name and prepared.profile_id == "bx-dev"
    assert (fake.home / "work").is_dir()
    created = fake.read()["machines"][row.name]
    assert created["args"] == ["--json", "--vcpu", "1"]
    launch = prepared.launch
    await launch.install()
    assert (fake.home / ".kirocrew-worker" / "worker.py").exists()
    worker_id = launch.new_worker_id()
    secret = wire.new_secret()
    port = await launch.start_worker(worker_id, [sys.executable, "-u", "-c", ENGINE], secret)
    link = RemoteLink(
        worker_id, secret, launch.dial(port), keepalive_seconds=1, stall_seconds=5, lease_seconds=20
    )
    try:
        link.start()
        await link.wait_connected(20)
        for size in (10, 1017, 200_000):
            line = json.dumps({"pad": "x" * size}).encode() + b"\n"
            await link.write(line)
            assert await asyncio.wait_for(link.stdout.readline(), 30) == line
    finally:
        await link.aclose()
        with contextlib.suppress(Exception):
            await launch.stop_worker(worker_id)
    # The session secret never appears in any boxd argv.
    assert not any(secret.hex() in " ".join(call) for call in fake.read()["calls"])
    again = await envs.prepare("dashboard:chat-1", "bx-dev")
    assert again.workspace == row.name
    assert sum(1 for c in fake.read()["calls"] if c[:2] == ["machine", "new"]) == 1


@sync
async def test_a_snapshot_profile_starts_from_it_and_keeps_its_size(tmp_path, fake):
    envs = _envs(tmp_path, fake, snapshot="my-workspace", vcpu=0)
    await envs.prepare("dashboard:chat-2", "bx-dev")
    name = envs.registry.get("dashboard:chat-2").name
    assert fake.read()["machines"][name]["args"] == ["--json", "--from-snapshot", "my-workspace"]


@sync
async def test_a_machine_deleted_outside_is_replaced_by_a_fresh_one(tmp_path, fake):
    envs = _envs(tmp_path, fake)
    first = await envs.prepare("dashboard:chat-3", "bx-dev")
    old = envs.registry.get("dashboard:chat-3").name
    # Reattaching its own machine is not a new workspace.
    assert first.created and not (await envs.prepare("dashboard:chat-3", "bx-dev")).created
    await (await envs.cli()).remove(old)
    fresh = await envs.prepare("dashboard:chat-3", "bx-dev")
    new = envs.registry.get("dashboard:chat-3").name
    assert new != old and new in fake.read()["machines"]
    # A replacement is: the session is told its workspace is empty.
    assert fresh.created


@sync
async def test_another_boxd_account_never_replaces_or_forgets_the_machine(
    tmp_path, fake, monkeypatch
):
    """Signed in to another account (or org) where the machine is not visible: the
    reopen is refused, the row and the machine stay, and nothing new is made."""
    envs = _envs(tmp_path, fake)
    await envs.prepare("dashboard:chat-9", "bx-dev")
    row = envs.registry.get("dashboard:chat-9")
    assert row.credential_id  # the account that created it is recorded (fingerprinted)
    # bob's account cannot see alice's machines: same store, but hide it.
    machines = fake.read()
    hidden = machines["machines"].pop(row.name)
    (tmp_path / "boxd-state.json").write_text(json.dumps(machines))
    monkeypatch.setenv("FAKE_BOXD_ACCOUNT", "bob")
    with pytest.raises(SessionEnvironmentUnavailable, match="nothing was deleted"):
        await envs.prepare("dashboard:chat-9", "bx-dev")
    # An account that cannot be read is no proof of a deletion either.
    monkeypatch.setenv("FAKE_BOXD_ACCOUNT", "unreadable")
    with pytest.raises(SessionEnvironmentUnavailable, match="nothing was deleted"):
        await envs.prepare("dashboard:chat-9", "bx-dev")
    assert envs.registry.get("dashboard:chat-9") == row
    assert sum(1 for c in fake.read()["calls"] if c[:2] == ["machine", "new"]) == 1
    # Back on the creating account, the same machine reopens.
    machines = fake.read()
    machines["machines"][row.name] = hidden
    (tmp_path / "boxd-state.json").write_text(json.dumps(machines))
    monkeypatch.setenv("FAKE_BOXD_ACCOUNT", "alice")
    assert not (await envs.prepare("dashboard:chat-9", "bx-dev")).created


@sync
@pytest.mark.parametrize("switched_to", ["bob", "unreadable"])
async def test_an_account_switch_during_the_lookup_never_replaces_the_machine(
    tmp_path, fake, monkeypatch, switched_to
):
    """The account changes between the identity read and the lookup: the new
    account's miss is not read as a deletion under the old one."""
    envs = _envs(tmp_path, fake)
    await envs.prepare("dashboard:chat-10", "bx-dev")
    row = envs.registry.get("dashboard:chat-10")
    monkeypatch.setenv("FAKE_BOXD_SWITCH_ON_GET", switched_to)
    with pytest.raises(SessionEnvironmentUnavailable, match="nothing was deleted"):
        await envs.prepare("dashboard:chat-10", "bx-dev")
    assert envs.registry.get("dashboard:chat-10") == row
    assert row.name in fake.read()["machines"]
    assert sum(1 for c in fake.read()["calls"] if c[:2] == ["machine", "new"]) == 1


@sync
async def test_a_signed_out_gateway_says_how_to_sign_in(tmp_path, fake, monkeypatch):
    monkeypatch.setenv("FAKE_BOXD_SIGNED_OUT", "1")
    envs = _envs(tmp_path, fake)
    with pytest.raises(SessionEnvironmentUnavailable, match="boxd auth login"):
        await envs.prepare("dashboard:chat-4", "bx-dev")


@sync
async def test_archiving_hibernates_with_a_backstop_then_the_sweep_deletes(tmp_path, fake):
    envs = _envs(tmp_path, fake)
    await envs.prepare("dashboard:chat-5", "bx-dev")
    name = envs.registry.get("dashboard:chat-5").name
    assert await envs.archive_session("dashboard:chat-5") is True
    machine = fake.read()["machines"][name]
    assert machine["status"] == "hibernated" and machine["delete-after"] == "7d"
    assert await envs.sweep_due() == 0
    # Reopening before the deadline clears both the stamp and boxd's own timer.
    await envs.prepare("dashboard:chat-5", "bx-dev")
    assert envs.registry.get("dashboard:chat-5").deletion_due_at == ""
    assert fake.read()["machines"][name]["delete-after"] == "off"
    await envs.archive_session("dashboard:chat-5")
    later = datetime.now(timezone.utc) + timedelta(days=8)
    assert await envs.sweep_due(now=later) == 1
    assert name not in fake.read()["machines"] and envs.registry.get("dashboard:chat-5") is None
    assert await envs.archive_session("dashboard:local") is False


@sync
async def test_a_failed_create_leaves_no_machine_or_row(tmp_path, fake, monkeypatch):
    envs = _envs(tmp_path, fake)

    async def boom(self, name, **kwargs):
        raise BoxdError("boxd machine new failed: quota")

    monkeypatch.setattr(BoxdCLI, "create", boom)
    with pytest.raises(BoxdError):
        await envs.prepare("dashboard:chat-9", "bx-dev")
    assert envs.registry.get("dashboard:chat-9") is None


@sync
async def test_a_missing_cli_refuses_before_any_row_is_reserved(tmp_path, monkeypatch):
    from kiro_crew.boxd_session import cli as boxd_cli

    def missing():
        raise BoxdError("the boxd CLI is not installed on the gateway machine")

    monkeypatch.setattr(boxd_cli, "resolve_boxd_bin", missing)
    section = SimpleNamespace(boxd_profiles=[{**PROFILE, "workdir": "/workspace/work"}])
    (tmp_path / "data").mkdir()
    envs = boxd_launch.BoxdSessionEnvironments(section, tmp_path / "data")
    with pytest.raises(BoxdError, match="not installed"):
        await envs.prepare("dashboard:chat-10", "bx-dev")
    assert envs.registry.get("dashboard:chat-10") is None


@sync
async def test_shutdown_hibernates_every_session_machine(tmp_path, fake, monkeypatch):
    from kiro_crew.boxd_session import cli as boxd_cli
    from kiro_crew.coder import retention
    from kiro_crew.config import live, paths

    envs = _envs(tmp_path, fake)
    monkeypatch.setattr(paths, "data_home", lambda: tmp_path / "data")
    monkeypatch.setattr(
        live,
        "snapshot",
        lambda: SimpleNamespace(
            session_environments=SimpleNamespace(coder_url="", **vars(envs._config))
        ),
    )
    monkeypatch.setattr(boxd_cli, "resolve_boxd_bin", lambda: fake.bin)
    prepared = await envs.prepare("dashboard:chat-11", "bx-dev")
    counts = await retention.stop_for_shutdown()
    assert counts["boxd"] == 1
    machine = fake.read()["machines"][prepared.workspace]
    assert machine["status"] == "hibernated"
    # Nothing is scheduled for deletion: the session resumes the same machine.
    assert envs.registry.get("dashboard:chat-11").deletion_due_at == ""


@sync
async def test_files_and_terminal_reach_the_machine_without_creating_it(
    tmp_path, fake, monkeypatch
):
    from kiro_crew.boxd_session import cli as boxd_cli
    from kiro_crew.coder import session_browse, session_launch
    from kiro_crew.config import live, paths

    envs = _envs(tmp_path, fake)
    home = tmp_path / "data"
    monkeypatch.setattr(paths, "data_home", lambda: home)
    monkeypatch.setattr(
        live, "snapshot", lambda: SimpleNamespace(session_environments=envs._config)
    )
    monkeypatch.setattr(boxd_cli, "resolve_boxd_bin", lambda: fake.bin)
    session_launch.SessionEnvironmentChoices(home / "coder" / "session-choices.json").set(
        "dashboard:chat-7", "bx-dev", "boxd"
    )
    with pytest.raises(session_browse.WorkspaceNotRunning):
        await session_browse.browse_session("dashboard:chat-7", "list", ".")
    assert not any(c[:2] == ["machine", "new"] for c in fake.read()["calls"])
    prepared = await envs.prepare("dashboard:chat-7", "bx-dev")
    (fake.home / "work" / "notes.txt").write_text("hi\n")
    await prepared.launch.install()
    listing = await session_browse.browse_session("dashboard:chat-7", "list", ".")
    assert "notes.txt" in json.dumps(listing)
    argv = await session_browse.remote_terminal_argv("dashboard:chat-7")
    assert argv[:5] == [fake.bin, "machine", "exec", "--tty", prepared.workspace]
    # The terminal opens in the folder Files used, not the machine's login folder.
    shell = await asyncio.to_thread(
        subprocess.run, argv, input=b"pwd\n", capture_output=True, timeout=30, check=True
    )
    assert shell.stdout.decode().strip() == str(fake.home / "work")


@sync
async def test_health_and_unsaved_work_never_wake_a_hibernated_machine(tmp_path, fake, monkeypatch):
    from kiro_crew.boxd_session import cli as boxd_cli
    from kiro_crew.coder import session_browse, session_launch
    from kiro_crew.config import live, paths

    envs = _envs(tmp_path, fake)
    home = tmp_path / "data"
    monkeypatch.setattr(paths, "data_home", lambda: home)
    monkeypatch.setattr(
        live, "snapshot", lambda: SimpleNamespace(session_environments=envs._config)
    )
    monkeypatch.setattr(boxd_cli, "resolve_boxd_bin", lambda: fake.bin)
    session_launch.SessionEnvironmentChoices(home / "coder" / "session-choices.json").set(
        "dashboard:chat-7", "bx-dev", "boxd"
    )
    prepared = await envs.prepare("dashboard:chat-7", "bx-dev")
    await prepared.launch.install()
    running = await session_browse.session_health("dashboard:chat-7")
    assert running["state"] == "running"
    await BoxdCLI(fake.bin).hibernate(prepared.workspace)
    execs = sum(c[:2] == ["machine", "exec"] for c in fake.read()["calls"])
    health = await session_browse.session_health("dashboard:chat-7")
    assert health == {
        "workspace": prepared.workspace,
        "workdir": str(fake.home / "work"),
        "state": "stopped",
    }
    assert await session_browse.unsaved_work("dashboard:chat-7") is None
    # Neither probe exec'd into it, so it is still hibernated, off compute.
    after = fake.read()
    assert sum(c[:2] == ["machine", "exec"] for c in after["calls"]) == execs
    assert after["machines"][prepared.workspace]["status"] == "hibernated"


@sync
async def test_a_boxd_choice_is_prepared_by_the_boxd_provider(monkeypatch, tmp_path):
    from kiro_crew.coder import session_launch
    from kiro_crew.config import live, paths

    monkeypatch.setattr(paths, "data_home", lambda: tmp_path)
    section = SimpleNamespace(
        coder_url="", profiles=[], sprites_profiles=[], boxd_profiles=[PROFILE]
    )
    monkeypatch.setattr(live, "snapshot", lambda: SimpleNamespace(session_environments=section))
    session_launch.SessionEnvironmentChoices(tmp_path / "coder" / "session-choices.json").set(
        "dashboard:chat-8", "bx-dev", "boxd"
    )
    seen = []

    async def prepare(self, key, profile_id, *, progress_key=None):
        seen.append((key, profile_id, progress_key))
        return "prepared"

    monkeypatch.setattr(boxd_launch.BoxdSessionEnvironments, "prepare", prepare)
    assert await session_launch.prepare_for_session("dashboard:chat-8") == "prepared"
    assert seen == [("dashboard:chat-8", "bx-dev", "dashboard:chat-8")]


def test_names_are_validated_before_any_argv_is_built():
    from kiro_crew.boxd_session.cli import check_machine_name
    from kiro_crew.boxd_session.shell import BoxdShell

    for bad in ("--json", "kc-XYZ", "other", "kc-0123456789abcdef; rm -rf /"):
        with pytest.raises(BoxdError):
            check_machine_name(bad)
    with pytest.raises(BoxdError):
        BoxdShell("/bin/boxd", "-y")
    assert BoxdShell("/b", "kc-0123456789abcdef").argv("echo hi") == [
        "/b",
        "machine",
        "exec",
        "kc-0123456789abcdef",
        "--",
        "sh",
        "-c",
        "echo hi",
    ]


def test_boxd_profiles_validate_and_share_one_id_space():
    from kiro_crew.config import session_environments as se

    good = {**PROFILE, "workdir": "/workspace/work"}
    assert se.validate_boxd_profiles([good]) == [good]
    with pytest.raises(se.SessionEnvironmentsError, match="vcpu"):
        se.validate_boxd_profiles([{**good, "vcpu": 3}])
    with pytest.raises(se.SessionEnvironmentsError, match="own size"):
        se.validate_boxd_profiles([{**good, "snapshot": "snap", "vcpu": 2}])
    with pytest.raises(se.SessionEnvironmentsError, match="snapshot"):
        se.validate_boxd_profiles([{**good, "snapshot": "--from-snapshot=x"}])
    with pytest.raises(se.SessionEnvironmentsError, match=r"^boxd_profiles\[0\]\.name"):
        se.validate_boxd_profiles([{**good, "name": ""}])
    with pytest.raises(se.SessionEnvironmentsError, match="both a Coder and a boxd"):
        se.validate_session_environments(
            {
                "coder_url": "https://c.example.com",
                "profiles": [{"id": "bx-dev", "name": "c", "template": "t", "workdir": "/w"}],
                "boxd_profiles": [good],
            }
        )
    assert se.load_boxd_profiles({"boxd_profiles": [good, {"id": "x"}]}) == [good]


@sync
async def test_the_readiness_check_lists_once_and_touches_no_machine(tmp_path, fake, monkeypatch):
    from kiro_crew.boxd_session import cli as boxd_cli
    from kiro_crew.dashboard.handlers import session_environments as handlers

    monkeypatch.setattr(boxd_cli, "resolve_boxd_bin", lambda: fake.bin)
    section = SimpleNamespace(boxd_profiles=[PROFILE, {**PROFILE, "id": "bx-two"}])
    assert await handlers._check_boxd(section) == [
        {"id": "bx-dev", "ok": True},
        {"id": "bx-two", "ok": True},
    ]
    assert fake.read()["calls"] == [["machine", "list", "--json"]]
    monkeypatch.setenv("FAKE_BOXD_SIGNED_OUT", "1")
    rows = await handlers._check_boxd(section)
    assert rows[0]["ok"] is False and "boxd auth login" in rows[0]["error"]
    assert await handlers._check_boxd(SimpleNamespace(boxd_profiles=[])) == []


def _live_section(monkeypatch, tmp_path, envs, fake):
    from kiro_crew.boxd_session import cli as boxd_cli
    from kiro_crew.config import live, paths

    monkeypatch.setattr(paths, "data_home", lambda: tmp_path / "data")
    section = SimpleNamespace(coder_url="", sprites_profiles=[], **vars(envs._config))
    monkeypatch.setattr(live, "snapshot", lambda: SimpleNamespace(session_environments=section))
    monkeypatch.setattr(boxd_cli, "resolve_boxd_bin", lambda: fake.bin)


@sync
async def test_the_archive_path_schedules_the_machine_and_a_reopen_supersedes_it(
    tmp_path, fake, monkeypatch
):
    from kiro_crew.coder import retention

    envs = _envs(tmp_path, fake)
    _live_section(monkeypatch, tmp_path, envs, fake)
    await envs.prepare("dashboard:chat-12", "bx-dev")
    retention.schedule_archived(["dashboard:chat-12"])
    await asyncio.gather(*retention._TASKS)
    assert envs.registry.get("dashboard:chat-12").deletion_due_at
    # An archive queued just before a reopen is superseded by it, not applied after.
    await envs.prepare("dashboard:chat-12", "bx-dev")
    retention.schedule_archived(["dashboard:chat-12"])
    await envs.prepare("dashboard:chat-12", "bx-dev")
    await asyncio.gather(*retention._TASKS)
    assert envs.registry.get("dashboard:chat-12").deletion_due_at == ""


@sync
async def test_a_history_reopen_keeps_the_machine_without_a_start(tmp_path, fake, monkeypatch):
    # Reopened from History and edited only through Files: no prepare ever runs,
    # so the reopen itself must cancel both deadlines, and wake nothing.
    from kiro_crew.coder import retention

    envs = _envs(tmp_path, fake)
    _live_section(monkeypatch, tmp_path, envs, fake)
    await envs.prepare("dashboard:chat-15", "bx-dev")
    name = envs.registry.get("dashboard:chat-15").name
    retention.schedule_archived(["dashboard:chat-15"])
    await asyncio.gather(*retention._TASKS)
    assert fake.read()["machines"][name]["delete-after"] == "7d"
    calls_before = len(fake.read()["calls"])
    retention.schedule_reopened(["dashboard:chat-15"])
    await asyncio.gather(*retention._TASKS)
    assert envs.registry.get("dashboard:chat-15").deletion_due_at == ""
    machine = fake.read()["machines"][name]
    assert machine["delete-after"] == "off" and machine["status"] == "hibernated"
    assert not any(
        c[:2] in (["machine", "new"], ["machine", "exec"])
        for c in fake.read()["calls"][calls_before:]
    )
    later = datetime.now(timezone.utc) + timedelta(days=8)
    assert await envs.sweep_due(now=later) == 0
    assert name in fake.read()["machines"]
    # An archive queued before the reopen is superseded by it.
    retention.schedule_archived(["dashboard:chat-15"])
    retention.schedule_reopened(["dashboard:chat-15"])
    await asyncio.gather(*retention._TASKS)
    assert envs.registry.get("dashboard:chat-15").deletion_due_at == ""


@sync
async def test_a_reopen_that_cannot_cancel_the_deletion_says_so(tmp_path, fake, monkeypatch):
    # A failed timer cancellation is reported, so a history resume refuses
    # rather than publish a tab whose machine both deadlines would delete.
    from kiro_crew.coder import retention

    envs = _envs(tmp_path, fake)
    _live_section(monkeypatch, tmp_path, envs, fake)
    await envs.prepare("dashboard:chat-16", "bx-dev")
    name = envs.registry.get("dashboard:chat-16").name
    retention.schedule_archived(["dashboard:chat-16"])
    await asyncio.gather(*retention._TASKS)

    async def refused(self, machine, value):
        raise BoxdError("boxd machine config set failed: try again")

    monkeypatch.setattr(BoxdCLI, "set_delete_after", refused)
    reason = await retention.keep_reopened(["dashboard:chat-16"])
    assert reason and "could not be cancelled" in reason
    assert envs.registry.get("dashboard:chat-16").deletion_due_at
    assert fake.read()["machines"][name]["delete-after"] == "7d"


@sync
async def test_a_reopen_whose_registry_write_fails_refuses_instead_of_publishing(
    tmp_path, fake, monkeypatch
):
    # A rotated credential makes the reopen record the one that answered. If that
    # registry write fails, the resume refuses: a tab published with its deadline
    # still standing would be swept with its files. Only a replacement machine
    # under the recorded name is "nothing to keep".
    from kiro_crew.coder import retention
    from kiro_crew.session_environments.persistent_vm import SessionResourceRegistry

    envs = _envs(tmp_path, fake)
    _live_section(monkeypatch, tmp_path, envs, fake)
    await envs.prepare("dashboard:chat-19", "bx-dev")
    name = envs.registry.get("dashboard:chat-19").name
    retention.schedule_archived(["dashboard:chat-19"])
    await asyncio.gather(*retention._TASKS)
    looked_up = type(envs).remote_get

    async def rotated(self, profile, name):
        record, _used = await looked_up(self, profile, name)
        return record, "rotated-credential"

    monkeypatch.setattr(type(envs), "remote_get", rotated)

    def unreadable(self, session_key, name, credential_id):
        raise SessionEnvironmentUnavailable("boxd session registry is unreadable")

    monkeypatch.setattr(SessionResourceRegistry, "set_credential_id", unreadable)
    reason = await retention.keep_reopened(["dashboard:chat-19"])
    assert reason and "unreadable" in reason
    assert fake.read()["machines"][name]["delete-after"] == "7d"


@sync
async def test_a_reopen_never_keeps_a_replacement_under_the_recorded_name(
    tmp_path, fake, monkeypatch
):
    # Like archive and the sweep, a reopen checks identity before it clears a
    # deletion timer: a machine that took the recorded name is left alone.
    from kiro_crew.coder import retention

    envs = _envs(tmp_path, fake)
    _live_section(monkeypatch, tmp_path, envs, fake)
    await envs.prepare("dashboard:chat-17", "bx-dev")
    name = envs.registry.get("dashboard:chat-17").name
    retention.schedule_archived(["dashboard:chat-17"])
    await asyncio.gather(*retention._TASKS)
    state = fake.read()
    state["machines"][name]["id"] = "id-someone-else"
    (tmp_path / "boxd-state.json").write_text(json.dumps(state))
    calls_before = len(fake.read()["calls"])
    assert await retention.keep_reopened(["dashboard:chat-17"]) is None
    assert fake.read()["machines"][name]["delete-after"] == "7d"
    assert not any(
        c[:3] == ["machine", "config", "set"] for c in fake.read()["calls"][calls_before:]
    )


@sync
async def test_a_blank_working_folder_runs_in_the_machines_login_home(tmp_path, fake, monkeypatch):
    # Blank in the profile: the session, Files and the terminal all use the
    # folder the machine reports as its login home. Coder still requires one.
    from kiro_crew.config import session_environments as se
    from kiro_crew.session_environments import browse as vm_browse

    assert se.validate_boxd_profiles([{**PROFILE, "workdir": ""}])[0]["workdir"] == ""
    with pytest.raises(se.SessionEnvironmentsError, match="workdir"):
        se.validate_profile({"id": "c", "name": "c", "template": "t", "workdir": ""})
    envs = _envs(tmp_path, fake, workdir="")
    _live_section(monkeypatch, tmp_path, envs, fake)
    prepared = await envs.prepare("dashboard:chat-16", "bx-dev")
    assert prepared.workdir == str(fake.home)
    assert envs.registry.get("dashboard:chat-16").home == str(fake.home)
    target = await vm_browse._resolve_persistent(
        "boxd", envs._config, tmp_path / "data", "dashboard:chat-16", "bx-dev"
    )
    assert target.workdir == str(fake.home)


@sync
async def test_the_sweeper_deletes_due_machines_without_a_coder_url(tmp_path, fake, monkeypatch):
    from kiro_crew.coder import retention

    envs = _envs(tmp_path, fake)
    _live_section(monkeypatch, tmp_path, envs, fake)
    prepared = await envs.prepare("dashboard:chat-13", "bx-dev")
    assert await retention.sweep_once() == 0  # nothing due: no CLI call made for it
    await envs.archive_session("dashboard:chat-13")
    envs.registry.set_deletion("dashboard:chat-13", "2000-01-01T00:00:00Z")
    assert await retention.sweep_once() == 1
    assert prepared.workspace not in fake.read()["machines"]


def test_the_dashboard_lists_machines_and_keeps_a_used_choice(tmp_path, fake, monkeypatch):
    from kiro_crew.config import paths
    from kiro_crew.dashboard.handlers import session_environments as handlers
    from kiro_crew.session_environments.persistent_vm import SessionResource

    monkeypatch.setattr(paths, "data_home", lambda: tmp_path / "data")
    envs = _envs(tmp_path, fake)
    assert handlers._remote_workspace_created("dashboard:chat-14") is False
    envs.registry.put(
        SessionResource(
            "dashboard:chat-14", "bx-dev", "kc-0123456789abcdef", "", "2026-01-01T00:00:00Z"
        )
    )
    rows = handlers._persistent_workspaces(tmp_path / "data")
    assert rows == [
        {
            "workspace": "kc-0123456789abcdef",
            "provider": "boxd",
            "kind": "session",
            "slot": "chat-14",
            "profile": "bx-dev",
            "state": "",
            "template": "bx-dev",
            "created_at": "2026-01-01T00:00:00Z",
            "deletion_due_at": "",
        }
    ]
    # A row exists from before the create call: the choice is fixed from then on.
    assert handlers._remote_workspace_created("dashboard:chat-14") is True
