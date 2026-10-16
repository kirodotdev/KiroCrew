"""A session moved to another profile before its workspace existed.

The first turn reserves a registry row on profile A's template, then the create
fails, so no workspace exists. The owner may still switch the session to profile
B on another template, and the next turn creates the workspace from B. The row
must then name B's template, because browse, health and the retention sweep
verify the live workspace against the row.
"""

from __future__ import annotations

import asyncio
import functools
from types import SimpleNamespace

import pytest

from kiro_crew.coder import retention, session_launch
from kiro_crew.coder.client import CoderClientError, CoderWorkspace
from kiro_crew.session_environment import SessionEnvironmentUnavailable

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
        self.fail_create = False

    async def current_user(self):
        return "devuser", "u-1"

    async def get_workspace(self, name):
        self.calls.append(f"get {name}")
        return self.workspaces.get(name)

    async def create_workspace(self, *, name, template, preset, stop_after_minutes):
        self.calls.append(f"create {name} {template}")
        if self.fail_create:
            raise CoderClientError("template refused")
        ws = CoderWorkspace(f"uuid-{name}", name, "u-1", template, "running", "")
        self.workspaces[name] = ws
        return ws

    async def stop_workspace(self, name):
        self.calls.append(f"stop {name}")
        ws = self.workspaces[name]
        self.workspaces[name] = CoderWorkspace(ws.uuid, name, ws.owner, ws.template, "stopped", "")
        return self.workspaces[name]

    async def delete_workspace(self, name):
        self.calls.append(f"delete {name}")
        self.workspaces.pop(name, None)


def _envs(tmp_path, fake):
    profiles = [
        SimpleNamespace(id="small", name="Small", template="micro", workdir="/workspace/app"),
        SimpleNamespace(id="big", name="Big", template="large", workdir="/workspace/app"),
    ]
    config = SimpleNamespace(coder_url=URL, profiles=profiles, archived_retention_days=0)
    return session_launch.CoderSessionEnvironments(
        config, tmp_path, coder_bin="coder", client_factory=lambda b, u: fake
    )


async def _failed_then_switched(tmp_path, fake):
    envs = _envs(tmp_path, fake)
    fake.fail_create = True
    with pytest.raises(SessionEnvironmentUnavailable, match="could not start"):
        await envs.prepare(KEY, "small")
    reserved = envs.registry.get_by_session(KEY)
    assert reserved.template == "micro" and not reserved.workspace_uuid
    fake.fail_create = False
    prepared = await envs.prepare(KEY, "big")
    return envs, reserved, prepared


@sync
async def test_the_create_after_a_switch_records_the_new_template(tmp_path):
    fake = FakeCoder()
    envs, reserved, prepared = await _failed_then_switched(tmp_path, fake)
    row = envs.registry.get_by_session(KEY)
    assert prepared.workspace == reserved.workspace_name
    assert fake.workspaces[row.workspace_name].template == "large"
    assert row.template == "large"
    assert row.workspace_uuid == f"uuid-{row.workspace_name}"
    # The same session keeps its one reserved identity across the switch.
    assert row.binding_id == reserved.binding_id


@sync
async def test_the_sweep_deletes_the_workspace_created_after_a_switch(tmp_path):
    fake = FakeCoder()
    envs, _reserved, prepared = await _failed_then_switched(tmp_path, fake)
    await retention.archive_session(envs, KEY)
    fake.calls.clear()
    assert await retention.sweep_due(envs) == 1
    assert f"delete {prepared.workspace}" in fake.calls
    assert prepared.workspace not in fake.workspaces


@sync
async def test_an_existing_workspace_keeps_the_row_template(tmp_path):
    # A workspace already exists under the reserved name (an earlier create
    # reached Coder but its answer was lost): the row keeps its template so the
    # switch is refused instead of retargeting the row away from that workspace.
    fake = FakeCoder()
    envs = _envs(tmp_path, fake)
    fake.fail_create = True
    with pytest.raises(SessionEnvironmentUnavailable):
        await envs.prepare(KEY, "small")
    reserved = envs.registry.get_by_session(KEY)
    fake.workspaces[reserved.workspace_name] = CoderWorkspace(
        "uuid-orphan", reserved.workspace_name, "u-1", "micro", "running", ""
    )
    with pytest.raises(SessionEnvironmentUnavailable, match="different template"):
        await envs.prepare(KEY, "big")
    assert envs.registry.get_by_session(KEY).template == "micro"
