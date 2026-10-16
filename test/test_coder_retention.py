"""Archived sessions: stop, keep for the retention window, then delete."""

from __future__ import annotations

import asyncio
import functools
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace

import pytest

from kiro_crew.coder import retention, session_launch
from kiro_crew.coder.client import CoderClientError, CoderWorkspace
from kiro_crew.coder.registry import DELETING
from kiro_crew.config.session_environments import (
    ARCHIVED_RETENTION_DAYS_DEFAULT,
    SessionEnvironmentsError,
    load_archived_retention_days,
    validate_archived_retention_days,
    validate_session_environments,
)
from kiro_crew.session_environment import SessionEnvironmentUnavailable
from kiro_crew.testing.wait import await_until

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


def _config(days=3, **extra):
    profile = SimpleNamespace(id="dev", name="Dev", template="micro", workdir="/w", **extra)
    return SimpleNamespace(coder_url=URL, profiles=[profile], archived_retention_days=days)


def _envs(tmp_path, fake, days=3):
    return session_launch.CoderSessionEnvironments(
        _config(days), tmp_path, coder_bin="coder", client_factory=lambda b, u: fake
    )


async def _started(tmp_path, fake, days=3):
    envs = _envs(tmp_path, fake, days)
    await envs.prepare(KEY, "dev")
    return envs, envs.registry.get_by_session(KEY)


# ── Config ──────────────────────────────────────────────────────────────


def test_retention_is_zero_to_seven_days():
    assert validate_archived_retention_days(0) == 0
    assert validate_archived_retention_days(7) == 7
    for bad in (-1, 8, True, "3", 1.5):
        with pytest.raises(SessionEnvironmentsError, match="archived_retention_days"):
            validate_archived_retention_days(bad)


def test_retention_is_optional_and_lenient_on_load():
    base = {"coder_url": URL, "profiles": []}
    assert "archived_retention_days" not in validate_session_environments(base)
    assert (
        validate_session_environments({**base, "archived_retention_days": 2})[
            "archived_retention_days"
        ]
        == 2
    )
    assert load_archived_retention_days(base) == ARCHIVED_RETENTION_DAYS_DEFAULT
    assert load_archived_retention_days({**base, "archived_retention_days": 99}) == (
        ARCHIVED_RETENTION_DAYS_DEFAULT
    )


def test_a_config_without_the_setting_never_means_delete_now(tmp_path):
    envs = _envs(tmp_path, FakeCoder())
    del envs._config.archived_retention_days
    stamped = []

    def schedule(key, due):
        stamped.append(due)
        return None

    envs.registry.get_by_session = lambda key: SimpleNamespace(deployment_id=URL)
    envs.registry.schedule_deletion = schedule
    asyncio.run(retention.archive_session(envs, KEY))
    due = datetime.fromisoformat(stamped[0].replace("Z", "+00:00"))
    assert due - datetime.now(timezone.utc) > timedelta(days=ARCHIVED_RETENTION_DAYS_DEFAULT - 1)


# ── Archive and reopen ──────────────────────────────────────────────────


@sync
async def test_archive_stops_the_workspace_and_stamps_the_retention(tmp_path):
    fake = FakeCoder()
    envs, row = await _started(tmp_path, fake, days=3)
    fake.calls.clear()
    await retention.archive_session(envs, KEY)
    assert fake.verbs() == ["get", "stop"]
    stamped = envs.registry.get_by_session(KEY)
    due = datetime.fromisoformat(stamped.deletion_due_at.replace("Z", "+00:00"))
    assert timedelta(days=2, hours=23) < due - datetime.now(timezone.utc) <= timedelta(days=3)


@sync
async def test_a_session_with_no_workspace_is_a_no_op(tmp_path):
    fake = FakeCoder()
    assert await retention.archive_session(_envs(tmp_path, fake), "dashboard:local") is None
    assert fake.calls == []


@sync
async def test_reopening_before_the_deadline_keeps_the_same_workspace(tmp_path):
    fake = FakeCoder()
    envs, row = await _started(tmp_path, fake)
    await retention.archive_session(envs, KEY)
    fake.calls.clear()
    prepared = await envs.prepare(KEY, "dev")
    assert prepared.workspace == row.workspace_name
    assert "create" not in fake.verbs() and "start" in fake.verbs()
    assert envs.registry.get_by_session(KEY).deletion_due_at == ""
    # The cancelled stamp never deletes it later.
    later = datetime.now(timezone.utc) + timedelta(days=30)
    assert await retention.sweep_due(envs, now=later) == 0


@sync
async def test_reopening_while_it_is_being_deleted_is_refused(tmp_path):
    fake = FakeCoder()
    envs, row = await _started(tmp_path, fake, days=0)
    await retention.archive_session(envs, KEY)
    envs.registry.begin_deletion(row.binding_id, datetime.now(timezone.utc))
    with pytest.raises(SessionEnvironmentUnavailable, match="being deleted"):
        await envs.prepare(KEY, "dev")


# ── Sweep ───────────────────────────────────────────────────────────────


@sync
async def test_the_sweep_deletes_only_what_is_due_and_drops_the_row(tmp_path):
    fake = FakeCoder()
    envs, row = await _started(tmp_path, fake, days=3)
    await retention.archive_session(envs, KEY)
    assert await retention.sweep_due(envs) == 0  # not yet
    later = datetime.now(timezone.utc) + timedelta(days=3, minutes=1)
    fake.calls.clear()
    assert await retention.sweep_due(envs, now=later) == 1
    assert f"delete {row.workspace_name}" in fake.calls
    assert envs.registry.get_by_session(KEY) is None
    # Reopened afterwards, the session gets a fresh workspace.
    prepared = await envs.prepare(KEY, "dev")
    assert prepared.workspace != row.workspace_name


@sync
async def test_zero_days_deletes_on_the_next_pass_without_a_stop(tmp_path):
    fake = FakeCoder()
    envs, row = await _started(tmp_path, fake, days=0)
    fake.calls.clear()
    await retention.archive_session(envs, KEY)
    assert "stop" not in fake.verbs()
    assert await retention.sweep_due(envs) == 1
    assert row.workspace_name not in fake.workspaces


@sync
async def test_a_failed_delete_stays_deleting_and_retries(tmp_path):
    fake = FakeCoder()
    envs, row = await _started(tmp_path, fake, days=0)
    await retention.archive_session(envs, KEY)
    fake.fail_delete = True
    assert await retention.sweep_due(envs) == 0
    assert envs.registry.get_by_session(KEY).state == DELETING
    fake.fail_delete = False
    assert await retention.sweep_due(envs) == 1
    assert envs.registry.get_by_session(KEY) is None


@sync
async def test_the_sweep_never_deletes_a_workspace_its_row_did_not_mint(tmp_path):
    fake = FakeCoder()
    envs, row = await _started(tmp_path, fake, days=0)
    await retention.archive_session(envs, KEY)
    fake.workspaces[row.workspace_name] = CoderWorkspace(
        "someone-else", row.workspace_name, "u-2", "micro", "running", ""
    )
    fake.calls.clear()
    assert await retention.sweep_due(envs) == 1  # the row is forgotten...
    assert "delete" not in fake.verbs()  # ...and the workspace left alone
    assert row.workspace_name in fake.workspaces


@sync
async def test_the_sweeper_pass_runs_the_sweep(tmp_path, monkeypatch):
    fake = FakeCoder()
    envs, row = await _started(tmp_path, fake, days=0)
    await retention.archive_session(envs, KEY)

    async def coder_envs():
        return envs

    monkeypatch.setattr(retention, "_environments", coder_envs)
    assert await retention.sweep_once() == 1


@sync
async def test_the_sweeper_pass_does_nothing_without_coder(monkeypatch):
    async def nothing():
        return None

    monkeypatch.setattr(retention, "_environments", nothing)
    assert await retention.sweep_once() == 0


@sync
async def test_a_zero_day_archive_wakes_the_sweeper(monkeypatch):
    wake = asyncio.Event()
    monkeypatch.setattr(retention, "_wake", wake)
    retention.request_sweep()
    assert wake.is_set()


# ── Gateway shutdown ────────────────────────────────────────────────────


@sync
async def test_shutdown_stops_the_workspace_without_scheduling_deletion(tmp_path, monkeypatch):
    fake = FakeCoder()
    envs, binding = await _started(tmp_path, fake)
    fake.calls.clear()

    async def coder_envs():
        return envs

    monkeypatch.setattr(retention, "_environments", coder_envs)

    counts = await retention.stop_for_shutdown()

    assert counts == {"coder": 1, "skipped": 0}
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
    fake = FakeCoder()
    envs, binding = await _started(tmp_path, fake)
    fake._status(binding.workspace_name, "stopped")
    fake.calls.clear()

    async def coder_envs():
        return envs

    monkeypatch.setattr(retention, "_environments", coder_envs)
    assert (await retention.stop_for_shutdown())["coder"] == 0
    assert "stop" not in fake.verbs()


@sync
async def test_gateway_shutdown_stop_is_bounded_and_cancels_the_sweeper(monkeypatch):
    from kiro_crew.slack import gateway

    async def hang():
        await asyncio.sleep(3600)

    sweeper = asyncio.ensure_future(asyncio.sleep(3600))
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
    from kiro_crew import session_environment as se
    from kiro_crew.coder import session_browse

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
    from kiro_crew import session_environment as se
    from kiro_crew.coder import session_browse

    async def boom(key):
        raise RuntimeError("machine unreachable")

    monkeypatch.setattr(session_browse, "unsaved_work", boom)
    monkeypatch.setattr(se, "session_runs_remotely", lambda key: True)
    await retention.warn_unsaved_work(("dashboard:x",), lambda *a: pytest.fail("no notice"))


@sync
async def test_stop_on_shutdown_off_leaves_environments_running(monkeypatch):
    from kiro_crew.slack import gateway

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
    from kiro_crew.config.session_environments import (
        load_stop_on_shutdown,
        validate_stop_on_shutdown,
    )

    base = {"coder_url": URL, "profiles": []}
    assert load_stop_on_shutdown(base) is True
    assert load_stop_on_shutdown({**base, "stop_on_shutdown": False}) is False
    assert load_stop_on_shutdown({**base, "stop_on_shutdown": "no"}) is True
    assert (
        validate_session_environments({**base, "stop_on_shutdown": False})["stop_on_shutdown"]
        is False
    )
    for bad in ("true", 1, None):
        with pytest.raises(SessionEnvironmentsError, match="stop_on_shutdown"):
            validate_stop_on_shutdown(bad)


@pytest.mark.parametrize("operation", ["archive", "sweep", "shutdown"])
@pytest.mark.parametrize("has_namesake", [False, True])
@sync
async def test_deployment_change_preserves_rows_without_touching_compute(
    tmp_path, monkeypatch, operation, has_namesake
):
    fake = FakeCoder()
    envs, row = await _started(tmp_path, fake, days=0)
    if operation == "sweep":
        await retention.archive_session(envs, KEY)
    before = envs.registry.get_by_session(KEY)
    other = FakeCoder()
    if has_namesake:
        other.workspaces[row.workspace_name] = fake.workspaces[row.workspace_name]
    envs._config.coder_url = "https://other-coder.example.test"
    envs._client_factory = lambda b, u: other

    def refuse_claim(*args):
        pytest.fail("must not claim a row from another deployment")

    monkeypatch.setattr(envs.registry, "begin_deletion", refuse_claim)

    async def environments():
        return envs

    monkeypatch.setattr(retention, "_environments", environments)
    if operation == "archive":
        assert await retention.archive_session(envs, KEY) is None
    elif operation == "sweep":
        assert await retention.sweep_due(envs) == 0
    else:
        assert (await retention.stop_for_shutdown())["coder"] == 0
    assert other.calls == []
    assert envs.registry.get_by_session(KEY) == before
    assert fake.workspaces[row.workspace_name].status == "running"


@sync
async def test_sweep_filters_old_deployments_but_still_deletes_current_rows(tmp_path):
    fake = FakeCoder()
    envs, old = await _started(tmp_path, fake, days=0)
    await retention.archive_session(envs, KEY)
    envs._config.coder_url = "https://other-coder.example.test"
    await envs.prepare("dashboard:new", "dev")
    current = envs.registry.get_by_session("dashboard:new")
    await retention.archive_session(envs, "dashboard:new")
    fake.calls.clear()
    assert await retention.sweep_due(envs) == 1
    assert old.workspace_name in fake.workspaces
    assert current.workspace_name not in fake.workspaces
    assert envs.registry.get_by_session(KEY) is not None
    assert f"get {old.workspace_name}" not in fake.calls


# ── Templates whose stop destroys the compute ───────────────────────────


@sync
async def test_shutdown_leaves_a_stop_destroys_compute_workspace_running(tmp_path, monkeypatch):
    fake = FakeCoder()
    envs, binding = await _started(tmp_path, fake)
    envs._config.profiles[0].compute_survives_stop = False
    fake.calls.clear()

    async def coder_envs():
        return envs

    monkeypatch.setattr(retention, "_environments", coder_envs)
    assert await retention.stop_for_shutdown() == {"coder": 0, "skipped": 1}
    assert "stop" not in fake.verbs()
    assert fake.workspaces[binding.workspace_name].status == "running"


@sync
async def test_archive_of_a_stop_destroys_compute_workspace_stamps_without_stopping(tmp_path):
    fake = FakeCoder()
    envs, binding = await _started(tmp_path, fake, days=3)
    envs._config.profiles[0].compute_survives_stop = False
    fake.calls.clear()
    await retention.archive_session(envs, KEY)
    assert "stop" not in fake.verbs()
    assert envs.registry.get_by_session(KEY).deletion_due_at
    # The retention deletion still happens at the due date.
    later = datetime.now(timezone.utc) + timedelta(days=3, minutes=1)
    assert await retention.sweep_due(envs, now=later) == 1
    assert binding.workspace_name not in fake.workspaces


@pytest.mark.parametrize("change", ["removed", "retemplated"])
@sync
async def test_an_unknown_profile_is_never_stopped(tmp_path, monkeypatch, change):
    fake = FakeCoder()
    envs, binding = await _started(tmp_path, fake, days=3)
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
    from kiro_crew import session_environment as se
    from kiro_crew.coder import session_browse

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
    fake = FakeCoder()
    envs, _ = await _started(tmp_path, fake)
    envs._config.profiles[0].compute_survives_stop = False
    seen: list[bool] = []

    async def coder_envs():
        return envs

    async def warn(keys, notify, *, survives_stop=True):
        seen.append(survives_stop)

    monkeypatch.setattr(retention, "_environments", coder_envs)
    monkeypatch.setattr(retention, "warn_unsaved_work", warn)
    await retention._archive(((KEY, retention.session_lifecycle(KEY), 0),))
    assert seen == [False]
    assert "stop" not in fake.verbs()[-1:]


@pytest.mark.parametrize("bad", ["false", 0, None, 1])
def test_compute_survives_stop_must_be_a_bool(bad):
    from kiro_crew.config.session_environments import load_session_environments, validate_profile

    profile = {"id": "dev", "name": "Dev", "template": "micro", "workdir": "/w"}
    assert validate_profile(profile)["compute_survives_stop"] is True
    assert (
        validate_profile({**profile, "compute_survives_stop": False})["compute_survives_stop"]
        is False
    )
    with pytest.raises(SessionEnvironmentsError, match="compute_survives_stop"):
        validate_profile({**profile, "compute_survives_stop": bad})
    # A hand-edited non-bool drops the profile on load rather than guessing.
    assert load_session_environments(
        {"coder_url": URL, "profiles": [{**profile, "compute_survives_stop": bad}]}
    ) == (URL, [])
