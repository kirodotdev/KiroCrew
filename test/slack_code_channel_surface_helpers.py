"""Fixtures and builders shared by the ``test_slack_code_channel_agent_surface*`` suites.

A PLAIN module, not a ``conftest.py``: each suite imports what it uses by name.
The fixtures are declared with an explicit ``name=`` so importing them does not
collide with the test parameter of the same name (flake8 F811); a suite imports
the ``*_fixture`` symbols and its tests keep requesting ``env`` / ``worktree``.
"""

from __future__ import annotations

import asyncio
import os
import subprocess
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest
from slack_code_channels_helpers import OWNER, make_env

from kiro_crew.dashboard.handlers import worktree as wt
from kiro_crew.slack.code_channels import commands, status, surface, tools, wrapup
from kiro_crew.slack.code_channels.store import Binding
from kiro_crew.slack.code_channels.surface_store import SurfaceStore
from kiro_crew.slack.code_channels.surface_store import set_store as set_surface_store
from kiro_crew.slack.code_channels.surface_store import store as surface_store
from kiro_crew.slack.code_channels.workspace import git_ops, service
from kiro_crew.slack.code_channels.workspace.store import WorkspaceRecord, WorkspaceStore

CH = "C0CODE"


OTHER_CH = "C0OTHER"


ORIGIN = "C0ORIGIN"


SLOT = "chat-1-1"


OTHER_SLOT = "chat-2-1"


SESSION = "dashboard:chat-1-1"


OTHER_SESSION = "dashboard:chat-2-1"


def _passthrough_spawn(argv: Any, mode: str = "standard", **_kw: Any) -> Any:
    return list(argv), dict(os.environ), None


@pytest.fixture(name="env")
def env_fixture(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> Any:
    monkeypatch.setenv("KIROCREW_HOME", str(tmp_path / "home"))
    monkeypatch.setattr(wt, "sandboxed_spawn_argv", _passthrough_spawn)
    monkeypatch.setattr(git_ops, "sandboxed_spawn_argv", _passthrough_spawn)
    gen = make_env(monkeypatch, tmp_path)
    e = next(gen)
    set_surface_store(SurfaceStore(tmp_path / "surface.json"))
    for mod in (tools, commands, wrapup):
        monkeypatch.setattr(mod, "live_config", e.cfg)
    monkeypatch.setattr(surface, "sel", lambda: e.sel)
    surface.set_slack_provider(lambda: e.slack)
    monkeypatch.setattr(surface, "dashboard_state", lambda: e.state)
    # Verified session key -> slot, as caller_access resolves it on a live dashboard.
    e.sessions = {SESSION: SLOT, OTHER_SESSION: OTHER_SLOT}
    monkeypatch.setattr(surface, "caller_slot_key", lambda _s, key: e.sessions.get(key, ""))
    e.allowed = {OWNER}
    from kiro_crew.slack import handler

    # The surface reads the owner check through the handler facade.
    monkeypatch.setattr(handler, "is_allowed_user", lambda u: u in e.allowed)
    e.notified = []
    e.state.notify = lambda *a, **k: e.notified.append((a, k))
    e.state.push_slots_update = lambda: None
    e.slack.blocks_posts = []
    e.slack.updates = []

    async def post_blocks(channel: str, blocks: list, text: str, thread_ts: Any = None) -> str:
        e.slack._n += 1
        e.slack.blocks_posts.append((channel, blocks, text, thread_ts))
        return f"300.{e.slack._n:06d}"

    async def update_message(channel: str, ts: str, text: str = "", blocks: Any = None) -> None:
        e.slack.updates.append((channel, ts, text, blocks))

    e.slack.post_blocks = post_blocks
    e.slack.update_message = update_message
    commands._seen = None
    surface._archived_here.clear()
    wrapup._requests.clear()
    wrapup._carrying.clear()
    wrapup._recoveries.clear()
    tools._canvas_locks.clear()
    tools._bar_locks.clear()
    tools._unsaved_canvas.clear()
    from kiro_crew.messaging import identity

    e.inbound_ok = True
    e.outbound_ok = True

    async def inbound(_channel: str) -> bool:
        return e.inbound_ok

    async def outbound(_channel: str) -> bool:
        return e.outbound_ok

    monkeypatch.setattr(identity, "channel_inbound_permitted", inbound)
    monkeypatch.setattr(identity, "channel_outbound_permitted", outbound)
    commands._gh_cache.clear()
    status._held.clear()
    yield e
    set_surface_store(None)
    surface.set_slack_provider(None)
    try:
        next(gen)
    except StopIteration:
        pass


def _bind(
    env: Any, channel: str = CH, slot: str = SLOT, anchor: str = "100.000001", project: str = ""
) -> Binding:
    binding = Binding(
        project_dir=project,
        channel_id=channel,
        team_id="T1",
        session_key=slot,
        anchor_ts=anchor,
        origin_channel_id=ORIGIN,
        origin_ts="1700000000.000100",
        title="Fix flaky login",
    )
    env.store.put_binding(binding)
    env.state.linked[anchor] = SimpleNamespace(key=slot, running=True)
    return binding


def _calls(env: Any, method: str) -> list[dict[str, Any]]:
    return [body for m, body in env.api.calls if m == method]


def _run(env: Any, tool: str, args: dict[str, Any], session: str = SESSION) -> tools.ToolOutcome:
    return asyncio.run(tools.run_tool(env.state, session, tool, args))


def _turn(binding: Binding, prompt: str = "", outcome: str = "ok") -> None:
    """One whole turn of the channel's session, running *prompt*."""
    asyncio.run(wrapup.on_turn_start(binding, SimpleNamespace(prompt=prompt)))
    asyncio.run(wrapup.on_turn_end(binding, SimpleNamespace(outcome=outcome)))


async def _aturn(binding: Binding, prompt: str = "", outcome: str = "ok") -> None:
    await wrapup.on_turn_start(binding, SimpleNamespace(prompt=prompt))
    await wrapup.on_turn_end(binding, SimpleNamespace(outcome=outcome))


def _tool_rows(env: Any, outcome: str) -> list[dict[str, Any]]:
    return [r for r in env.sel.ops("slack.code_channel.tool") if r["outcome"] == outcome]


async def _noop(_text: str) -> None:
    return None


def _slash(command: str, *, user: str = OWNER, text: str = "", trigger: str = "13345.1") -> dict:
    return {
        "command": command,
        "text": text,
        "channel_id": CH,
        "user_id": user,
        "team_id": "T1",
        "trigger_id": trigger,
        "response_url": "https://hooks.slack.com/commands/T1/1/x",
    }


def _click(user: str, ts: str, channel: str = CH, value: str | None = None) -> dict:
    return {
        "type": "block_actions",
        "team": {"id": "T1"},
        "user": {"id": user},
        "channel": {"id": channel},
        "message": {"ts": ts, "blocks": [{"type": "section"}]},
        "actions": [
            {"action_id": wrapup.ARCHIVE_ACTION_ID, "value": channel if value is None else value}
        ],
    }


def _wrap(env: Any) -> tuple[list[dict[str, Any]], str]:
    out = _run(env, tools.WRAP_UP, {"summary": "Fixed the flaky login test <!channel>"})
    assert out.ok, out.text
    return env.slack.blocks_posts[-1][1], surface_store().get(CH).summary_ts


def _git(*args: str, cwd: Path) -> str:
    return subprocess.run(
        ["git", *args], cwd=cwd, check=True, capture_output=True, text=True, encoding="utf-8"
    ).stdout.strip()


@pytest.fixture(name="worktree")
def worktree_fixture(tmp_path: Path) -> tuple[Path, Path]:
    root = tmp_path / "proj"
    root.mkdir()
    _git("init", "-q", "-b", "main", cwd=root)
    for key, value in (("user.email", "t@example.com"), ("user.name", "T"), ("gc.auto", "0")):
        _git("config", key, value, cwd=root)
    (root / "README.md").write_text("hello\n")
    _git("add", ".", cwd=root)
    _git("commit", "-q", "-m", "init", cwd=root)
    tree = _channel_tree(root)  # where a bind from this root cuts it
    _git("worktree", "add", "-q", "-b", BRANCH, str(tree), cwd=root)
    return root, tree


BRANCH = git_ops.channel_branch("Fix flaky login", "T1", CH)


def _channel_tree(root: Path, branch: str = BRANCH) -> Path:
    """The path a bind from *root* on *branch* creates its worktree at."""
    return root.parent / f"{root.name}-wt-{wt._dir_slug(branch)}"


def _register_workspace(root: Path, tree: Path, base: str = "") -> None:
    base = base or _git("rev-parse", "HEAD", cwd=root)
    WorkspaceStore().put(
        WorkspaceRecord(
            team_id="T1",
            channel_id=CH,
            session_key=SLOT,
            project_dir=str(root),
            repo_root=str(root),
            worktree=str(tree),
            branch=BRANCH,
            base_sha=base,
        )
    )


class _Request(dict):
    def __init__(self, *, headers: dict, body: Any, **flags: Any) -> None:
        super().__init__(**flags)
        self.headers = headers
        self._body = body
        self.app = {"state": "STATE"}

    async def json(self) -> Any:
        if isinstance(self._body, BaseException):
            raise self._body
        return self._body


def _flaky_archive_stamp(env: Any, monkeypatch: Any, failures: int) -> list[int]:
    """``update_binding(archived_at=...)`` raises *failures* times, then works."""
    real = env.store.update_binding
    seen = [0]

    def flaky(channel: str, **fields: Any) -> Any:
        if "archived_at" in fields:
            seen[0] += 1
            if seen[0] <= failures:
                raise OSError("busy")
        return real(channel, **fields)

    monkeypatch.setattr(env.store, "update_binding", flaky)
    monkeypatch.setattr(wrapup, "_RECORD_RETRY_DELAYS", (0.0, 0.0, 0.0))
    env.api.responses["conversations.info"] = {
        "ok": True,
        "channel": {"id": CH, "is_archived": True},
    }
    return seen


def _retarget_service(
    monkeypatch: Any,
    tmp_path: Path,
    events: list[tuple[str, str]],
    applies: bool,
    refuse_remove: bool | str = False,
) -> Any:
    from kiro_crew.slack.code_channels.workspace import core as workspace_core

    async def set_project(key: str, path: str) -> bool:
        events.append(("set_project", f"{key}->{path}"))
        return applies

    class Client:
        async def set_context_bar(self, *_a: Any, **_k: Any) -> dict:
            return {"ok": True}

    svc = service.WorkspaceService(
        lambda: Client(),
        store=WorkspaceStore(tmp_path / "shared.json"),
        set_project=set_project,
    )
    svc.store.put(WorkspaceStore().get("T1", CH))
    monkeypatch.setattr(workspace_core, "_SERVICE", svc)
    real_git = git_ops.run_git

    def run_git(args: list[str], cwd: Any, *a: Any, **k: Any) -> Any:
        if args[:2] == ["worktree", "remove"]:
            events.append(("worktree_remove", str(args[2])))
            if refuse_remove == "raise":
                raise RuntimeError("sandbox unavailable")
            if refuse_remove:
                return subprocess.CompletedProcess(args, 1, "", "locked")
        return real_git(args, cwd, *a, **k)

    monkeypatch.setattr(git_ops, "run_git", run_git)
    return svc


def _repo_with_channel_tree(base: Path) -> tuple[Path, Path]:
    base.mkdir(parents=True)
    _git("init", "-q", "-b", "main", cwd=base)
    for key, value in (("user.email", "t@example.com"), ("user.name", "T"), ("gc.auto", "0")):
        _git("config", key, value, cwd=base)
    (base / "README.md").write_text("hello\n")
    _git("add", ".", cwd=base)
    _git("commit", "-q", "-m", "init", cwd=base)
    return base, _channel_tree(base)
