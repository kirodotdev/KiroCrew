"""A fake managed environment for the lane's tests: answers the lane's closed list of
scripts over a fake ssh, records every script, and refuses any other."""

from __future__ import annotations

import json

from kiro_crew.managed_env import install, machine, remote
from kiro_crew.managed_env.remote import RemoteResult


class FakeBox:
    def __init__(self) -> None:
        self.scripts: list[tuple[str, str]] = []
        self.account = machine.MACHINE_ACCOUNT_TYPE
        self.hook = "started"
        self.fail: set[str] = set()  # step names that answer a failure
        self.healthy = True

    def step_of(self, script: str) -> str:
        if script == install.install_script():
            return "install"
        if script.startswith(f"{remote.WRAPPER} config set "):
            return "machine-config"
        if script.startswith("set -e\nmkdir -p ") and install.HOOK in script:
            return "hook-write"
        if script == install.HOOK_RUN:
            return "hook-run"
        if script == machine.WHOAMI_SCRIPT:
            return "whoami"
        if remote.HEALTH_SCRIPT in script:
            return "health"
        raise AssertionError(f"a script outside the lane's closed list: {script[:80]!r}")

    def __call__(self, host: str, script: str, *, timeout: float, retry: bool = False, env=None):
        assert remote.sandbox_id_from_host(host), host
        step = self.step_of(script)
        self.scripts.append((host, step))
        if step in self.fail:
            return RemoteResult(1, "", "failed")
        if step == "install":
            return RemoteResult(0, install.INSTALL_DONE + "\n", "")
        if step == "machine-config":
            return RemoteResult(0, machine.CONFIG_DONE + "\n", "")
        if step == "hook-write":
            return RemoteResult(0, install.HOOK_WRITTEN + "\n", "")
        if step == "hook-run":
            return RemoteResult(0, f"{install.HOOK_NAME}: {self.hook}\n", "")
        if step == "whoami":
            return RemoteResult(0, json.dumps({"accountType": self.account}) + "\n", "")
        body = json.dumps({"ok": self.healthy})
        return RemoteResult(0, f"HTTP/1.0 200 OK\r\n\r\n{body}", "")

    def steps(self) -> list[str]:
        return [s for _, s in self.scripts]


class FakeRegistry:
    """The core's ``InstancesRegistry`` surface the lane uses."""

    class Row:
        def __init__(self, id: str, name: str, **fields) -> None:
            self.id, self.name = id, name
            self.via_instance_id = ""
            self.__dict__.update(fields)

    def __init__(self) -> None:
        self.rows: dict[str, FakeRegistry.Row] = {}

    def list(self):
        return list(self.rows.values())

    def add(self, *, name: str, **fields):
        rid = f"r{len(self.rows) + 1}"
        self.rows[rid] = self.Row(rid, name, **fields)
        return self.rows[rid]

    def update(self, rid: str, **fields):
        self.rows[rid].__dict__.update(fields)

    def remove_cascade(self, rid: str):
        self.rows.pop(rid, None)
        return [rid]
