"""``kirocrew knowledge`` returns a process exit code, not always 0.

An error path that only printed and returned None let the dispatcher fall
through to exit 0, so `kirocrew knowledge stats` on a library that is not
configured (or whose schema is behind this build) exited 0 while printing an
error -- a `kirocrew knowledge stats && deploy` chain could not tell a failed
read from an empty one. The command now returns 0 on success, 1 on a printed
error, and 2 for an unknown sub-action, and the dispatcher `sys.exit`s it.
"""

from __future__ import annotations

import argparse

import pytest

from kiro_crew.cli_knowledge import _knowledge


def _run(tmp_path, monkeypatch: pytest.MonkeyPatch, **kw) -> int:
    """Invoke ``_knowledge`` with the data home pointed at an empty tmp dir, so
    ``workspace/knowledge/knowledge.db`` does not exist (the not-configured path).

    The home is set via ``KIROCREW_HOME``, the env var ``config_dir()`` reads,
    rather than by patching ``config_dir`` -- that name is mirrored across the
    config facade and ``test_facade_mirror_guards`` refuses a direct owner patch
    of it.
    """
    home = tmp_path / "home"
    home.mkdir()
    monkeypatch.setenv("KIROCREW_HOME", str(home))
    return _knowledge(argparse.Namespace(**kw))


def test_stats_on_unconfigured_library_exits_nonzero(tmp_path, monkeypatch, capsys) -> None:
    rc = _run(tmp_path, monkeypatch, knowledge_action="stats", json=False)
    assert rc == 1
    assert "not configured" in capsys.readouterr().out.lower()


def test_stats_json_on_unconfigured_library_exits_nonzero(tmp_path, monkeypatch, capsys) -> None:
    rc = _run(tmp_path, monkeypatch, knowledge_action="stats", json=True)
    assert rc == 1
    assert "not_configured" in capsys.readouterr().out


def test_dedup_on_unconfigured_library_exits_nonzero(tmp_path, monkeypatch, capsys) -> None:
    rc = _run(tmp_path, monkeypatch, knowledge_action="dedup", apply=False)
    assert rc == 1
    assert "not configured" in capsys.readouterr().out.lower()


def test_unknown_sub_action_exits_two(tmp_path, monkeypatch, capsys) -> None:
    rc = _run(tmp_path, monkeypatch, knowledge_action=None)
    assert rc == 2
    assert "usage" in capsys.readouterr().out.lower()
