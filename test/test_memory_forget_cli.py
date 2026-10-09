"""``kirocrew memory forget``: owner-only removal of one semantic key.

The verb wraps ``VectorMemoryStore.delete_semantic``, the call the owner-only
dashboard route makes. Two directions are pinned:

* **The owner's shell forgets.** A live row is tombstoned with source
  ``user_explicit``, its prior value is printed and kept as a revision, a winner
  can be recorded, and a second run reports the key absent with exit 1.
* **An agent session's shell is refused** before any store opens, whether the
  session is named by the environment or the identity ladder cannot answer.

The identity ladder is replaced with a fixed answer in most tests so the result
does not depend on the shell that runs pytest; one test drives the real ladder
through ``KIROCREW_SESSION_KEY``.
"""

from __future__ import annotations

import argparse
import json

import pytest

from kiro_crew import cli_commands as cc
from kiro_crew import mcp_caller
from kiro_crew.config import loader as loader_mod
from kiro_crew.config.loader import config_dir
from kiro_crew.memory_stores import DEFAULT_MEMORY_STORE, resolve_store_path
from kiro_crew.vector_memory import VectorMemoryStore

# Each test writes ``config.json`` into its own data home and drops the process-wide
# config cache, so two workers racing that global is a flake.
pytestmark = pytest.mark.xdist_group("memory_forget_cli")

_KEY = "pref.editor"
_WINNER = "pref.editor_choice"


@pytest.fixture
def store_path(monkeypatch: pytest.MonkeyPatch):
    """The default store's file, seeded with two live keys and then closed."""
    payload = {
        "memory_stores": {DEFAULT_MEMORY_STORE: {}},
        "default_memory_store": DEFAULT_MEMORY_STORE,
    }
    (config_dir() / "config.json").write_text(json.dumps(payload), encoding="utf-8")
    loader_mod._invalidate_config_cache()
    path = resolve_store_path(DEFAULT_MEMORY_STORE)
    path.parent.mkdir(parents=True, exist_ok=True)
    store = VectorMemoryStore(db_path=path)
    store.init()
    try:
        store.set_semantic(_KEY, "vim", 0.9, "test")
        store.set_semantic(_WINNER, "vim, with relative numbers", 0.9, "test")
    finally:
        store.close()
    return path


def _owner(monkeypatch: pytest.MonkeyPatch) -> None:
    """This process belongs to no agent session."""
    monkeypatch.setattr(mcp_caller, "resolve_own_identity", lambda **_: mcp_caller.OwnIdentity())


def _forget(key: str, *, superseded_by: str | None = None, reason: str | None = None) -> None:
    cc._memory_cmd(
        argparse.Namespace(
            mem_action="forget", key=key, superseded_by=superseded_by, reason=reason, store=None
        )
    )


def _read(path, key: str) -> dict | None:
    store = VectorMemoryStore(db_path=path)
    store.init()
    try:
        return store.get_semantic(key)
    finally:
        store.close()


def _events(path, key: str) -> list[dict]:
    store = VectorMemoryStore(db_path=path)
    store.init()
    try:
        return [e for e in store.get_events(limit=50) if e.get("memory_key") == key]
    finally:
        store.close()


class TestOwnerShell:
    def test_forget_removes_the_key_and_prints_its_value(self, store_path, monkeypatch, capsys):
        _owner(monkeypatch)
        _forget(_KEY)
        out = capsys.readouterr().out
        assert f"{_KEY}: vim" in out
        assert "Forgotten." in out
        assert _read(store_path, _KEY) is None
        assert _read(store_path, _WINNER) is not None
        deletes = [e for e in _events(store_path, _KEY) if e["event_type"] == "delete"]
        assert deletes and deletes[0]["source"] == "user_explicit"
        assert deletes[0]["old_value"] == json.dumps("vim")

    def test_second_run_reports_absent_and_exits_1(self, store_path, monkeypatch, capsys):
        _owner(monkeypatch)
        _forget(_KEY)
        capsys.readouterr()
        with pytest.raises(SystemExit) as exc:
            _forget(_KEY)
        assert exc.value.code == 1
        assert f"Already absent: {_KEY}" in capsys.readouterr().err

    def test_superseded_by_records_the_winner(self, store_path, monkeypatch, capsys):
        _owner(monkeypatch)
        _forget(_KEY, superseded_by=_WINNER, reason="merged")
        assert f"superseded by {_WINNER}" in capsys.readouterr().out
        assert _read(store_path, _KEY) is None
        recorded = [
            json.loads(e["new_value"])
            for e in _events(store_path, _KEY)
            if e["event_type"] == "delete" and e.get("new_value")
        ]
        assert recorded == [{"superseded_by": _WINNER, "reason": "merged"}]

    @pytest.mark.parametrize("winner", ["pref.not_there", _KEY])
    def test_superseded_by_needs_a_different_live_key(self, store_path, monkeypatch, winner):
        _owner(monkeypatch)
        with pytest.raises(SystemExit) as exc:
            _forget(_KEY, superseded_by=winner)
        assert exc.value.code == 1
        assert _read(store_path, _KEY) is not None

    def test_reason_without_winner_is_refused(self, store_path, monkeypatch):
        _owner(monkeypatch)
        with pytest.raises(SystemExit):
            _forget(_KEY, reason="merged")
        assert _read(store_path, _KEY) is not None


class TestAgentSessionShell:
    @pytest.mark.parametrize(
        "identity",
        [
            mcp_caller.OwnIdentity(session_key="dashboard:chat-1", source=mcp_caller.SOURCE_ENV),
            mcp_caller.OwnIdentity(session_key="chat-2", source=mcp_caller.SOURCE_PIDFILE),
            mcp_caller.OwnIdentity(source=mcp_caller.SOURCE_PIDFILE, failed=True),
        ],
        ids=["env", "pid-mapping", "unsettled"],
    )
    def test_refused_and_nothing_changes(self, store_path, monkeypatch, capsys, identity):
        monkeypatch.setattr(mcp_caller, "resolve_own_identity", lambda **_: identity)
        opened: list[object] = []
        monkeypatch.setattr(cc, "declared_store", lambda *a, **k: opened.append(a))
        with pytest.raises(SystemExit) as exc:
            _forget(_KEY)
        assert exc.value.code == 1
        assert "owner only" in capsys.readouterr().err
        assert opened == []
        assert _read(store_path, _KEY) is not None

    def test_session_key_in_env_is_refused(self, store_path, monkeypatch, capsys):
        """The real ladder: the variable the gateway puts in every agent shell."""
        monkeypatch.setenv("KIROCREW_SESSION_KEY", "dashboard:chat-3")
        with pytest.raises(SystemExit):
            _forget(_KEY)
        assert "owner only" in capsys.readouterr().err
        assert _read(store_path, _KEY) is not None


def test_parser_routes_forget_to_the_memory_command(monkeypatch, tmp_path):
    seen: list[argparse.Namespace] = []
    monkeypatch.setenv("KIROCREW_PROJECT_DIR", str(tmp_path))
    monkeypatch.setattr(cc, "_memory_cmd", seen.append)
    monkeypatch.setattr(
        "sys.argv",
        ["kirocrew", "memory", "forget", _KEY, "--superseded-by", _WINNER, "--reason", "x"],
    )
    from kiro_crew.cli import main

    main()
    assert len(seen) == 1
    args = seen[0]
    assert (args.mem_action, args.key, args.superseded_by, args.reason, args.store) == (
        "forget",
        _KEY,
        _WINNER,
        "x",
        None,
    )
