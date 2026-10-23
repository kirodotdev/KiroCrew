"""Archived sessions: stop, keep for the retention window, then delete."""

from __future__ import annotations

import asyncio
import functools
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace

import pytest

from kiro_crew import session_environment as se
from kiro_crew.coder import retention, session_browse, session_launch
from kiro_crew.coder.client import CoderClientError, CoderWorkspace
from kiro_crew.coder.registry import DELETING
from kiro_crew.config import live, paths
from kiro_crew.config.loader import KiroCrewConfig
from kiro_crew.config.sections_session import SessionEnvironmentsConfig
from kiro_crew.config.session_environments import (
    ARCHIVED_RETENTION_DAYS,
    SessionEnvironmentsError,
    load_session_environments,
    load_stop_on_shutdown,
    validate_profile,
    validate_session_environments,
    validate_stop_on_shutdown,
)
from kiro_crew.session_environment import SessionEnvironmentUnavailable
from kiro_crew.slack import gateway
from kiro_crew.testing.wait import await_until


@pytest.fixture(autouse=True)
def _developer_flag_on(_floor_monkeypatch):
    # Session Environments are reachable only with the developer flag.
    _floor_monkeypatch.setenv("KIROCREW_DEV_MODE", "1")


URL = "https://coder.example.test"
KEY = "dashboard:chat-1"


def sync(fn):
    @functools.wraps(fn)
    def wrapper(*args, **kwargs):
        return asyncio.run(asyncio.wait_for(fn(*args, **kwargs), 30))

    return wrapper


class FakeCoder:
    def __init__(self):
        self.workspaces: dict[str, CoderWorkspace] = {}
        self.calls: list[str] = []
        self.fail_delete = False

    async def current_user(self):
        return "devuser", "u-1"

    async def list_workspaces(self):
        return tuple(self.workspaces.values())

    async def get_workspace(self, name):
        self.calls.append(f"get {name}")
        return self.workspaces.get(name)

    async def create_workspace(self, *, name, template, preset, stop_after_minutes):
        self.calls.append(f"create {name}")
        ws = CoderWorkspace(f"uuid-{name}", name, "u-1", template, "running", "")
        self.workspaces[name] = ws
        return ws

    def _status(self, name, status):
        ws = self.workspaces[name]
        self.workspaces[name] = CoderWorkspace(ws.uuid, ws.name, ws.owner, ws.template, status, "")
        return self.workspaces[name]

    async def start_workspace(self, name):
        self.calls.append(f"start {name}")
        return self._status(name, "running")

    async def stop_workspace(self, name):
        self.calls.append(f"stop {name}")
        return self._status(name, "stopped")

    async def delete_workspace(self, name):
        self.calls.append(f"delete {name}")
        if self.fail_delete:
            raise CoderClientError("delete refused")
        self.workspaces.pop(name, None)

    def verbs(self):
        return [call.split()[0] for call in self.calls]


def _config(**extra):
    profile = SimpleNamespace(id="dev", name="Dev", template="micro", workdir="/w", **extra)
    return SimpleNamespace(coder_url=URL, profiles=[profile])


def _envs(tmp_path, fake):
    return session_launch.CoderSessionEnvironments(
        _config(), tmp_path, coder_bin="coder", client_factory=lambda b, u: fake
    )


async def _started(tmp_path, *, archived=False):
    fake = FakeCoder()
    envs = _envs(tmp_path, fake)
    await envs.prepare(KEY, "dev")
    row = envs.registry.get_by_session(KEY)
    if archived:
        await retention.archive_session(envs, KEY)
    return fake, envs, row


def _assert_due_in_a_week(stamp: str) -> None:
    due = datetime.fromisoformat(stamp.replace("Z", "+00:00")) - datetime.now(timezone.utc)
    assert timedelta(days=6, hours=23) < due <= timedelta(days=7)


def _later(**delta) -> datetime:
    return datetime.now(timezone.utc) + timedelta(**delta)


def _past_due() -> datetime:
    return _later(days=ARCHIVED_RETENTION_DAYS, minutes=1)


def _serve(monkeypatch, envs):
    async def environments():
        return envs

    monkeypatch.setattr(retention, "_environments", environments)


def test_retention_is_a_fixed_week_not_a_setting(tmp_path):
    assert ARCHIVED_RETENTION_DAYS == 7
    base = {"coder_url": URL, "profiles": []}
    with pytest.raises(SessionEnvironmentsError, match="archived_retention_days"):
        validate_session_environments({**base, "archived_retention_days": 2})
    assert not hasattr(SessionEnvironmentsConfig(), "archived_retention_days")
    # A stray attribute on the config never shortens it.
    envs = _envs(tmp_path, FakeCoder())
    envs._config.archived_retention_days = 0
    stamped = []
    envs.registry.get_by_session = lambda key: SimpleNamespace(deployment_id=URL)
    envs.registry.schedule_deletion = lambda key, due: stamped.append(due)
    asyncio.run(retention.archive_session(envs, KEY))
    _assert_due_in_a_week(stamped[0])


@sync
async def test_archive_stops_the_workspace_and_stamps_the_retention(tmp_path):
    fake, envs, row = await _started(tmp_path)
    fake.calls.clear()
    await retention.archive_session(envs, KEY)
    assert fake.verbs() == ["get", "stop"]
    _assert_due_in_a_week(envs.registry.get_by_session(KEY).deletion_due_at)


@sync
async def test_a_session_with_no_workspace_is_a_no_op(tmp_path):
    fake = FakeCoder()
    assert await retention.archive_session(_envs(tmp_path, fake), "dashboard:local") is None
    assert fake.calls == []


@sync
async def test_reopening_before_the_deadline_keeps_the_same_workspace(tmp_path):
    fake, envs, row = await _started(tmp_path, archived=True)
    fake.calls.clear()
    prepared = await envs.prepare(KEY, "dev")
    assert prepared.workspace == row.workspace_name
    assert "create" not in fake.verbs() and "start" in fake.verbs()
    assert envs.registry.get_by_session(KEY).deletion_due_at == ""
    # The cancelled stamp never deletes it later.
    assert await retention.sweep_due(envs, now=_later(days=30)) == 0


@sync
async def test_reopening_while_it_is_being_deleted_is_refused(tmp_path):
    _, envs, row = await _started(tmp_path, archived=True)
    envs.registry.begin_deletion(row.binding_id, _past_due())
    with pytest.raises(SessionEnvironmentUnavailable, match="being deleted"):
        await envs.prepare(KEY, "dev")


@sync
async def test_the_sweep_deletes_only_what_is_due_and_drops_the_row(tmp_path):
    fake, envs, row = await _started(tmp_path, archived=True)
    assert await retention.sweep_due(envs) == 0  # not yet
    fake.calls.clear()
    assert await retention.sweep_due(envs, now=_past_due()) == 1
    assert f"delete {row.workspace_name}" in fake.calls
    assert envs.registry.get_by_session(KEY) is None
    # Reopened afterwards, the session gets a fresh workspace.
    prepared = await envs.prepare(KEY, "dev")
    assert prepared.workspace != row.workspace_name


@sync
async def test_a_failed_delete_stays_deleting_and_retries(tmp_path):
    fake, envs, row = await _started(tmp_path, archived=True)
    fake.fail_delete = True
    assert await retention.sweep_due(envs, now=_past_due()) == 0
    assert envs.registry.get_by_session(KEY).state == DELETING
    fake.fail_delete = False
    assert await retention.sweep_due(envs, now=_past_due()) == 1
    assert envs.registry.get_by_session(KEY) is None


@sync
async def test_the_sweep_never_deletes_a_workspace_its_row_did_not_mint(tmp_path):
    fake, envs, row = await _started(tmp_path, archived=True)
    fake.workspaces[row.workspace_name] = CoderWorkspace(
        "someone-else", row.workspace_name, "u-2", "micro", "running", ""
    )
    fake.calls.clear()
    assert await retention.sweep_due(envs, now=_past_due()) == 1  # the row is forgotten...
    assert "delete" not in fake.verbs()  # ...and the workspace left alone
    assert row.workspace_name in fake.workspaces


@sync
async def test_the_sweep_never_deletes_for_a_row_with_no_recorded_uuid(tmp_path, monkeypatch):
    # Provisioning failed before the UUID was saved; the name may now be another
    # workspace with the same owner and template, so nothing is deleted for it.
    import dataclasses

    fake, envs, row = await _started(tmp_path, archived=True)
    begin = envs.registry.begin_deletion

    def unrecorded(binding_id, moment):
        claimed = begin(binding_id, moment)
        return dataclasses.replace(claimed, workspace_uuid="") if claimed else claimed

    monkeypatch.setattr(envs.registry, "begin_deletion", unrecorded)
    fake.calls.clear()
    assert await retention.sweep_due(envs, now=_past_due()) == 1
    assert "delete" not in fake.verbs()
    assert row.workspace_name in fake.workspaces


@sync
async def test_archive_runs_no_coder_command_with_the_developer_flag_off(
    tmp_path, monkeypatch, _floor_monkeypatch
):
    # A binding left from a dev-flag run is not stopped or scheduled for deletion
    # once the gateway restarts with the flag off.
    from kiro_crew.coder import session_launch

    fake, envs, _ = await _started(tmp_path)
    # The real lookup, so the flag check itself is under test.
    monkeypatch.setattr(live, "snapshot", lambda: SimpleNamespace(session_environments=_config()))
    monkeypatch.setattr(paths, "data_home", lambda: tmp_path)
    monkeypatch.setattr(session_launch, "CoderSessionEnvironments", lambda *_: envs)
    _floor_monkeypatch.delenv("KIROCREW_DEV_MODE")
    monkeypatch.setattr(retention, "_TASKS", set())
    fake.calls.clear()
    retention.schedule_archived([KEY])
    assert retention._TASKS == set()
    await retention._archive(((KEY, retention.session_lifecycle(KEY), 0),))
    assert await retention.sweep_once() == 0
    assert fake.calls == []
    assert envs.registry.get_by_session(KEY).deletion_due_at == ""


@sync
async def test_the_sweeper_pass_runs_the_sweep(tmp_path, monkeypatch):
    _, envs, _ = await _started(tmp_path, archived=True)
    _serve(monkeypatch, envs)
    monkeypatch.setattr(retention, "_now", _past_due)
    assert await retention.sweep_once() == 1


@sync
async def test_the_sweeper_pass_does_nothing_without_coder(monkeypatch):
    _serve(monkeypatch, None)
    assert await retention.sweep_once() == 0


@pytest.mark.parametrize("live_config", [True, False])
@sync
async def test_the_environments_follow_the_saved_coder_url(tmp_path, monkeypatch, live_config):
    saved = iter([_config(), SimpleNamespace(coder_url="")])

    def read():
        return SimpleNamespace(session_environments=next(saved))

    monkeypatch.setattr(live, "snapshot", read if live_config else lambda: None)
    monkeypatch.setattr(KiroCrewConfig, "load", read)
    monkeypatch.setattr(paths, "data_home", lambda: tmp_path)
    envs = await retention._environments()
    assert envs.url == URL and envs.data_home == tmp_path
    assert await retention._environments() is None


@sync
async def test_the_sweeper_logs_a_repeated_failure_once_and_keeps_running(monkeypatch, caplog):
    down = CoderClientError("down")
    results = iter([1, down, down, RuntimeError("bug"), 0, down, asyncio.CancelledError()])

    async def sweep_once():
        value = next(results)
        if isinstance(value, BaseException):
            raise value
        return value

    monkeypatch.setattr(retention, "sweep_once", sweep_once)
    caplog.set_level("INFO", logger=retention.logger.name)
    with pytest.raises(asyncio.CancelledError):
        await retention.run_sweeper(interval=0, initial_delay=0)
    failed = "Archived session sweep failed"
    assert [r.getMessage() for r in caplog.records] == [
        "Archived session workspaces deleted: 1",
        f"{failed}: down",
        failed,
        f"{failed}: down",
    ]


@sync
async def test_shutdown_stops_the_workspace_without_scheduling_deletion(tmp_path, monkeypatch):
    fake, envs, binding = await _started(tmp_path)
    fake.calls.clear()
    _serve(monkeypatch, envs)
    assert await retention.stop_for_shutdown() == {"coder": 1, "skipped": 0}
    assert f"stop {binding.workspace_name}" in fake.calls
    # A restart resumes the same workspace: nothing was stamped for deletion.
    assert envs.registry.get_by_session(KEY).deletion_due_at == ""


@sync
async def test_shutdown_never_raises_when_coder_fails(monkeypatch):
    async def boom():
        raise RuntimeError("provider unreachable")

    monkeypatch.setattr(retention, "_environments", boom)
    assert (await retention.stop_for_shutdown())["coder"] == 0


@sync
async def test_shutdown_leaves_an_already_stopped_workspace_alone(tmp_path, monkeypatch):
    fake, envs, binding = await _started(tmp_path)
    fake._status(binding.workspace_name, "stopped")
    fake.calls.clear()
    _serve(monkeypatch, envs)
    assert (await retention.stop_for_shutdown())["coder"] == 0
    assert "stop" not in fake.verbs()


@sync
async def test_gateway_shutdown_stop_is_bounded_and_cancels_the_sweeper(monkeypatch):
    async def hang():
        await asyncio.Event().wait()  # Cancelled by the shutdown deadline under test.

    sweeper = asyncio.ensure_future(hang())
    monkeypatch.setattr(gateway, "_CODER_RETENTION_TASK", sweeper)
    monkeypatch.setattr(retention, "stop_for_shutdown", hang)
    monkeypatch.setattr(retention, "SHUTDOWN_STOP_SECS", 0.05)
    await await_until(
        asyncio.ensure_future(gateway._stop_session_environments()),
        what="bounded environment shutdown",
    )
    await asyncio.sleep(0)
    assert sweeper.cancelled()


@sync
async def test_archive_warns_about_unsaved_work_before_stopping(monkeypatch):
    order: list[str] = []
    notes: list[tuple[str, str]] = []

    async def unsaved(key):
        order.append(f"check {key}")
        return "3 uncommitted files" if key == "dashboard:dirty" else None

    monkeypatch.setattr(session_browse, "unsaved_work", unsaved)
    monkeypatch.setattr(se, "session_runs_remotely", lambda key: key != "dashboard:local")
    await retention.warn_unsaved_work(
        ("dashboard:dirty", "dashboard:clean", "dashboard:local"),
        lambda title, body: notes.append((title, body)),
    )
    assert order == ["check dashboard:dirty", "check dashboard:clean"]
    assert len(notes) == 1 and "3 uncommitted files" in notes[0][1]


@sync
async def test_an_unsaved_work_check_failure_never_blocks_the_archive(monkeypatch):
    async def boom(key):
        raise RuntimeError("machine unreachable")

    monkeypatch.setattr(session_browse, "unsaved_work", boom)
    monkeypatch.setattr(se, "session_runs_remotely", lambda key: True)
    await retention.warn_unsaved_work(("dashboard:x",), lambda *a: pytest.fail("no notice"))


@sync
async def test_stop_on_shutdown_off_leaves_environments_running(monkeypatch):
    called = []

    async def stop():
        called.append(1)
        return {}

    async def off():
        return False

    monkeypatch.setattr(gateway, "_CODER_RETENTION_TASK", None)
    monkeypatch.setattr(retention, "stop_for_shutdown", stop)
    monkeypatch.setattr(retention, "stop_on_shutdown_enabled", off)
    await gateway._stop_session_environments()
    assert called == []


def test_stop_on_shutdown_is_a_boolean_that_defaults_on():
    base = {"coder_url": URL, "profiles": []}
    assert load_stop_on_shutdown(base) is True
    assert load_stop_on_shutdown({**base, "stop_on_shutdown": False}) is False
    assert load_stop_on_shutdown({**base, "stop_on_shutdown": "no"}) is True
    validated = validate_session_environments({**base, "stop_on_shutdown": False})
    assert validated["stop_on_shutdown"] is False
    for bad in ("true", 1, None):
        with pytest.raises(SessionEnvironmentsError, match="stop_on_shutdown"):
            validate_stop_on_shutdown(bad)


@pytest.mark.parametrize("operation", ["archive", "sweep", "shutdown"])
@pytest.mark.parametrize("has_namesake", [False, True])
@sync
async def test_deployment_change_preserves_rows_without_touching_compute(
    tmp_path, monkeypatch, operation, has_namesake
):
    fake, envs, row = await _started(tmp_path, archived=operation == "sweep")
    before = envs.registry.get_by_session(KEY)
    status = fake.workspaces[row.workspace_name].status
    other = FakeCoder()
    if has_namesake:
        other.workspaces[row.workspace_name] = fake.workspaces[row.workspace_name]
    envs._config.coder_url = "https://other-coder.example.test"
    envs._client_factory = lambda b, u: other

    def refuse_claim(*args):
        pytest.fail("must not claim a row from another deployment")

    monkeypatch.setattr(envs.registry, "begin_deletion", refuse_claim)
    _serve(monkeypatch, envs)
    if operation == "archive":
        assert await retention.archive_session(envs, KEY) is None
    elif operation == "sweep":
        assert await retention.sweep_due(envs, now=_past_due()) == 0
    else:
        assert (await retention.stop_for_shutdown())["coder"] == 0
    assert other.calls == []
    assert envs.registry.get_by_session(KEY) == before
    assert fake.workspaces[row.workspace_name].status == status


@sync
async def test_sweep_filters_old_deployments_but_still_deletes_current_rows(tmp_path):
    fake, envs, old = await _started(tmp_path, archived=True)
    envs._config.coder_url = "https://other-coder.example.test"
    await envs.prepare("dashboard:new", "dev")
    current = envs.registry.get_by_session("dashboard:new")
    await retention.archive_session(envs, "dashboard:new")
    fake.calls.clear()
    assert await retention.sweep_due(envs, now=_past_due()) == 1
    assert old.workspace_name in fake.workspaces
    assert current.workspace_name not in fake.workspaces
    assert envs.registry.get_by_session(KEY) is not None
    assert f"get {old.workspace_name}" not in fake.calls


async def _stop_destroys_compute(tmp_path):
    fake, envs, binding = await _started(tmp_path)
    envs._config.profiles[0].compute_survives_stop = False
    fake.calls.clear()
    return fake, envs, binding


@sync
async def test_shutdown_leaves_a_stop_destroys_compute_workspace_running(tmp_path, monkeypatch):
    fake, envs, binding = await _stop_destroys_compute(tmp_path)
    _serve(monkeypatch, envs)
    assert await retention.stop_for_shutdown() == {"coder": 0, "skipped": 1}
    assert "stop" not in fake.verbs()
    assert fake.workspaces[binding.workspace_name].status == "running"


@sync
async def test_archive_of_a_stop_destroys_compute_workspace_stamps_without_stopping(tmp_path):
    fake, envs, binding = await _stop_destroys_compute(tmp_path)
    await retention.archive_session(envs, KEY)
    assert "stop" not in fake.verbs()
    assert envs.registry.get_by_session(KEY).deletion_due_at
    # The retention deletion still happens at the due date.
    assert await retention.sweep_due(envs, now=_past_due()) == 1
    assert binding.workspace_name not in fake.workspaces


@pytest.mark.parametrize("change", ["removed", "retemplated"])
@sync
async def test_an_unknown_profile_is_never_stopped(tmp_path, monkeypatch, change):
    fake, envs, binding = await _started(tmp_path)
    if change == "removed":
        envs._config.profiles = []
    else:
        envs._config.profiles[0].template = "other"
    assert retention.compute_survives_stop(envs, binding) is False
    fake.calls.clear()
    await retention.archive_session(envs, KEY)
    assert "stop" not in fake.verbs()


def test_the_bound_profile_decides_over_a_sibling_on_the_same_template(tmp_path):
    envs = _envs(tmp_path, FakeCoder())
    keep = SimpleNamespace(id="keep", template="micro", compute_survives_stop=True)
    lose = SimpleNamespace(id="lose", template="micro", compute_survives_stop=False)
    envs._config.profiles = [keep, lose]
    binding = SimpleNamespace(session_key=KEY, template="micro")
    envs.choices.set(KEY, "keep")
    assert retention.compute_survives_stop(envs, binding) is True
    envs.choices.set(KEY, "lose")
    assert retention.compute_survives_stop(envs, binding) is False
    # Unbound: every profile on the template must agree.
    envs.choices.set(KEY, None)
    assert retention.compute_survives_stop(envs, binding) is False


@pytest.mark.parametrize("survives", [True, False])
@sync
async def test_the_unsaved_work_notice_says_whether_files_outlive_a_stop(monkeypatch, survives):
    async def unsaved(key):
        return "2 unpushed commits"

    notes: list[str] = []
    monkeypatch.setattr(session_browse, "unsaved_work", unsaved)
    monkeypatch.setattr(se, "session_runs_remotely", lambda key: True)
    await retention.warn_unsaved_work(
        ("dashboard:x",), lambda title, body: notes.append(body), survives_stop=survives
    )
    assert ("lost when that machine's compute stops" in notes[0]) is (not survives)
    assert ("before its retention runs out" in notes[0]) is survives


@sync
async def test_archive_warns_with_the_bound_profile_storage(tmp_path, monkeypatch):
    fake, envs, _ = await _started(tmp_path)
    envs._config.profiles[0].compute_survives_stop = False
    seen: list[bool] = []

    async def warn(keys, notify, *, survives_stop=True):
        seen.append(survives_stop)

    _serve(monkeypatch, envs)
    monkeypatch.setattr(retention, "warn_unsaved_work", warn)
    await retention._archive(((KEY, retention.session_lifecycle(KEY), 0),))
    assert seen == [False]
    assert "stop" not in fake.verbs()[-1:]


@pytest.mark.parametrize("bad", ["false", 0, None, 1])
def test_compute_survives_stop_must_be_a_bool(bad):
    profile = {"id": "dev", "name": "Dev", "template": "micro", "workdir": "/w"}
    assert validate_profile(profile)["compute_survives_stop"] is True
    validated = validate_profile({**profile, "compute_survives_stop": False})
    assert validated["compute_survives_stop"] is False
    with pytest.raises(SessionEnvironmentsError, match="compute_survives_stop"):
        validate_profile({**profile, "compute_survives_stop": bad})
    # A hand-edited non-bool drops the profile on load rather than guessing.
    assert load_session_environments(
        {"coder_url": URL, "profiles": [{**profile, "compute_survives_stop": bad}]}
    ) == (URL, [])


@pytest.mark.parametrize("operation", ["shutdown", "archive"])
@sync
async def test_a_binding_with_no_recorded_uuid_never_stops_a_namesake(
    tmp_path, monkeypatch, operation
):
    # A reservation whose provisioning failed recorded no UUID, and its name may now
    # belong to another workspace: stopping that one could destroy its files.
    import dataclasses

    fake, envs, binding = await _started(tmp_path)
    unrecorded = dataclasses.replace(binding, workspace_uuid="")
    fake.calls.clear()
    _serve(monkeypatch, envs)
    if operation == "shutdown":
        monkeypatch.setattr(envs.registry, "list_bindings", lambda: [unrecorded])
        assert (await retention.stop_for_shutdown())["coder"] == 0
    else:
        schedule = envs.registry.schedule_deletion

        def scheduled(key, due):
            schedule(key, due)
            return unrecorded

        monkeypatch.setattr(envs.registry, "schedule_deletion", scheduled)
        await retention.archive_session(envs, KEY)
    assert "stop" not in fake.verbs()
