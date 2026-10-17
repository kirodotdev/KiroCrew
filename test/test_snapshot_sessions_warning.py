"""Restoring config says, once, that the chats themselves are not carried.

The dashboard lists chats from `sessions/*.jsonl`. No component carries
`sessions/`, so a device move loses the chat history silently. Until the chats
can ride, a restore that brings `config` prints ONE warning.
"""

from __future__ import annotations

from pathlib import Path

from test_snapshot import _setup_fake_kirocrew, unpinnable_argv

from kiro_crew import snapshot as snap

WARNING = "chat history is not included in this restore"


def _home(tmp_path: Path, monkeypatch) -> Path:
    home = tmp_path / "home"
    home.mkdir()
    monkeypatch.setenv("KIROCREW_HOME", str(home))
    _setup_fake_kirocrew(home)
    (home / "sessions").mkdir()
    (home / "sessions" / "dashboard_chat-a.jsonl").write_text('{"_type": "metadata"}\n')
    return home


def _snapshot(tmp_path: Path, *components: str) -> Path:
    out = tmp_path / "out"
    argv = [str(out), *(("--components", ",".join(components)) if components else ())]
    assert snap.snapshot_main([*argv, *unpinnable_argv()]) == 0
    return next(out.glob("kirocrew-snapshot-*.tar.gz"))


def _restore(bundle: Path, mode: str, *components: str) -> int:
    argv = [str(bundle), "--mode", mode, "--force"]
    if components:
        argv += ["--components", ",".join(components)]
    return snap.restore_main([*argv, *unpinnable_argv()])


def test_restoring_config_warns_once(tmp_path, monkeypatch, capsys):
    _home(tmp_path, monkeypatch)
    bundle = _snapshot(tmp_path)
    capsys.readouterr()
    assert _restore(bundle, "replace") == 0
    assert capsys.readouterr().out.count(WARNING) == 1


def test_a_merge_of_config_warns_once_even_with_a_live_resume_map(tmp_path, monkeypatch, capsys):
    # session_map.json is the kiro-cli resume map, not the chat list, so its
    # presence on either side says nothing about whether the chats arrived.
    _home(tmp_path, monkeypatch)
    bundle = _snapshot(tmp_path, "config")
    capsys.readouterr()
    assert _restore(bundle, "merge", "config") == 0
    assert capsys.readouterr().out.count(WARNING) == 1


def test_it_warns_even_when_the_bundle_has_no_resume_map(tmp_path, monkeypatch, capsys):
    home = _home(tmp_path, monkeypatch)
    (home / "session_map.json").unlink()
    bundle = _snapshot(tmp_path)
    capsys.readouterr()
    assert _restore(bundle, "replace") == 0
    assert capsys.readouterr().out.count(WARNING) == 1


def test_no_warning_when_config_is_not_restored(tmp_path, monkeypatch, capsys):
    _home(tmp_path, monkeypatch)
    bundle = _snapshot(tmp_path)
    capsys.readouterr()
    assert _restore(bundle, "replace", "crons") == 0
    assert WARNING not in capsys.readouterr().out
