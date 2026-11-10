"""Route tests: a spec whose worktree was removed or whose branch changed.

The spec used to read as brand new (phase ``new``), its detail failed with a 409
that blamed another app, and the user could not reach Delete. The list and detail
now report ``stale`` (``worktree_missing`` / ``branch_changed``), the detail is
served for a missing worktree, and Delete still works.
"""

from __future__ import annotations

import os
import subprocess
from pathlib import Path

import pytest

from kiro_crew.apps.builtins.spec_builder.tests.routes_facade import routes

# _never_touch_the_real_state is an autouse fixture: pytest finds it by the name bound here.
from kiro_crew.apps.builtins.spec_builder.tests.routes_helpers import (  # noqa: F401
    _BASE,
    _make_client,
    _never_touch_the_real_state,
)

_GIT_ENV = {
    "GIT_AUTHOR_NAME": "t",
    "GIT_AUTHOR_EMAIL": "t@example.com",
    "GIT_COMMITTER_NAME": "t",
    "GIT_COMMITTER_EMAIL": "t@example.com",
}


def _git(*args: str, cwd: Path) -> None:
    subprocess.run(
        ["git", *args], cwd=cwd, check=True, capture_output=True, env={**os.environ, **_GIT_ENV}
    )


class _Slot:
    _app = routes.APP_NAME
    running = False
    messages: list = []
    project = ""
    _titled = False
    task = None


class _State:
    def __init__(self):
        self._slots: dict = {}

    def get_slot(self, key):
        return self._slots.get(key)

    def get_or_create_slot(self, name, app=""):
        slot = _Slot()
        slot.key = name
        self._slots[name] = slot
        return slot

    def push_slot_title(self, *_args):
        pass


def _worktree_spec(tmp_path: Path) -> tuple[Path, Path, Path]:
    """A repo, a linked worktree on ``spec/demo`` holding a committed requirements.md,
    and the index entry for it. Returns ``(repo, worktree, spec_dir)``."""
    root = Path(os.path.realpath(tmp_path))
    repo = root / "repo"
    repo.mkdir()
    _git("init", "-b", "main", cwd=repo)
    (repo / "a").write_text("x")
    _git("add", "a", cwd=repo)
    _git("commit", "-m", "init", cwd=repo)
    wt = root / "wt"
    _git("worktree", "add", "-b", "spec/demo", str(wt), cwd=repo)
    spec_dir = wt / ".kiro" / "specs" / "demo"
    spec_dir.mkdir(parents=True)
    (spec_dir / "requirements.md").write_text("# req\n")
    _git("add", "-A", cwd=wt)
    _git("commit", "-m", "spec", cwd=wt)
    routes._save_index(
        {
            "demo": {
                "working_dir": str(wt),
                "spec_dir": str(spec_dir),
                "spec_type": "feature",
                "status": "planning",
                "slot_key": routes._new_slot_key("demo"),
                "worktree_branch": "spec/demo",
                "repo_root": str(repo),
                "created_at": 1.0,
                "updated_at": 1.0,
            }
        }
    )
    return repo, wt, spec_dir


def _break(mode: str, repo: Path, wt: Path) -> None:
    if mode == "worktree_removed":
        _git("worktree", "remove", "--force", str(wt), cwd=repo)
    elif mode == "branch_changed":
        _git("checkout", "-b", "other", "main", cwd=wt)


async def _fetch(client, monkeypatch):
    import kiro_crew.dashboard.chat_persistence as persistence

    async def _archived(*_a, **_k):
        return None

    monkeypatch.setattr(persistence, "save_slot_off_loop", _archived)
    client.app["state"] = _State()
    await client.start_server()


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("mode", "stale"),
    [("intact", ""), ("worktree_removed", "worktree_missing"), ("branch_changed", "branch_changed")],
)
async def test_list_and_detail_report_why_a_spec_is_stale(tmp_path, monkeypatch, mode, stale):
    client = _make_client(monkeypatch, tmp_path)
    repo, wt, _spec_dir = _worktree_spec(tmp_path)
    _break(mode, repo, wt)
    await _fetch(client, monkeypatch)
    try:
        listed = await (await client.get(f"{_BASE}/specs")).json()
        detail = await client.get(f"{_BASE}/specs/demo")
        body = await detail.json()
    finally:
        await client.close()
    assert listed["specs"][0]["stale"] == stale
    assert detail.status == 200, "detail must be served, not a 409 blaming another app"
    assert body["stale"] == stale


@pytest.mark.asyncio
@pytest.mark.parametrize("mode", ["worktree_removed", "branch_changed"])
async def test_a_stale_spec_can_still_be_deleted(tmp_path, monkeypatch, mode):
    client = _make_client(monkeypatch, tmp_path)
    repo, wt, _spec_dir = _worktree_spec(tmp_path)
    _break(mode, repo, wt)
    await _fetch(client, monkeypatch)
    try:
        deleted = await client.delete(f"{_BASE}/specs/demo")
        listed = await (await client.get(f"{_BASE}/specs")).json()
    finally:
        await client.close()
    assert deleted.status == 200
    assert listed["specs"] == []


def test_a_detached_head_is_not_reported_as_a_branch_change(tmp_path):
    repo, wt, _spec_dir = _worktree_spec(tmp_path)
    _git("checkout", "--detach", cwd=wt)
    meta = routes._load_index()["demo"]
    assert routes._stale_reason(meta) == ""
