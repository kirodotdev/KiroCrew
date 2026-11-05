"""The diagnostics pickers find kiro-cli logs in the Windows ``kiro-log/logs/`` layout.

kiro-cli on Windows writes ``kiro-chat.log``, ``mcp.log`` and ``lsp.log`` under
``<TEMP>/kiro-log/logs/``, one level below the flat ``kiro-log/`` layout Linux
and macOS use, and Windows sets ``TEMP``/``TMP`` rather than ``TMPDIR``. The
pickers are pure path logic, so every case runs on every OS.
"""

from __future__ import annotations

import zipfile
from pathlib import Path

from conftest import make_dir_link
from kiro_crew import agent_scratch, diagnostics, release_channel

_NESTED = ("kiro-log", "logs")


def _write(path: Path, body: str = "kiro-cli trace line\n") -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(body, encoding="utf-8")
    return path


def _windows_env(monkeypatch, temp: Path) -> None:
    """A Windows-shaped environment: ``TEMP``/``TMP`` set, ``TMPDIR`` unset."""
    for key in ("XDG_RUNTIME_DIR", "TMPDIR", "KIRO_CHAT_LOG_FILE"):
        monkeypatch.delenv(key, raising=False)
    monkeypatch.setenv("TEMP", str(temp))
    monkeypatch.setenv("TMP", str(temp))
    # Keep the env-driven candidates real, but drop the fixed /tmp/kiro-log one so
    # a kiro-cli running on the test host never feeds its own logs into a case.
    host_tmp = Path("/tmp") / "kiro-log"
    real_dirs = diagnostics._kiro_log_dirs
    monkeypatch.setattr(
        diagnostics, "_kiro_log_dirs", lambda: [d for d in real_dirs() if d != host_tmp]
    )


def _isolate_bundle(monkeypatch, home: Path) -> None:
    """Point ``collect_bundle`` at a temp home, keeping the real log pickers."""
    home.mkdir(parents=True, exist_ok=True)
    monkeypatch.setattr("kiro_crew.diagnostics.config_dir", lambda: home)
    monkeypatch.setattr(agent_scratch, "config_dir", lambda: home)
    monkeypatch.setattr(diagnostics.Path, "home", classmethod(lambda cls: home))
    monkeypatch.setattr(diagnostics, "_macos_crash_reports", lambda: [])
    monkeypatch.setattr(diagnostics, "_kiro_cli_version", lambda: "kiro-cli 2.37.1")
    monkeypatch.setattr(release_channel, "distribution_version", lambda: None, raising=False)
    monkeypatch.setattr(release_channel, "recorded_channel", lambda: None, raising=False)


def test_host_chat_log_is_found_under_temp_logs(tmp_path, monkeypatch):
    temp = tmp_path / "temp"
    _windows_env(monkeypatch, temp)
    log = _write(temp.joinpath(*_NESTED, "kiro-chat.log"))

    assert diagnostics._kiro_cli_chat_log() == log


def test_flat_layout_wins_over_nested(tmp_path, monkeypatch):
    temp = tmp_path / "temp"
    _windows_env(monkeypatch, temp)
    flat = _write(temp / "kiro-log" / "kiro-chat.log")
    _write(temp.joinpath(*_NESTED, "kiro-chat.log"))

    assert diagnostics._kiro_cli_chat_log() == flat


def test_linked_logs_level_is_never_walked(tmp_path, monkeypatch):
    temp = tmp_path / "temp"
    _windows_env(monkeypatch, temp)
    off_tree = tmp_path / "off-tree"
    _write(off_tree / "kiro-chat.log", "OFFTREE\n")
    _write(off_tree / "mcp.log", "OFFTREE\n")
    (temp / "kiro-log").mkdir(parents=True)
    make_dir_link(temp.joinpath(*_NESTED), off_tree)

    assert diagnostics._kiro_log_levels(temp / "kiro-log") == [temp / "kiro-log"]
    # With the host /tmp candidate removed, the linked level is the only place a
    # log exists, so any picker that walked it would return a path.
    assert diagnostics._kiro_cli_chat_log() is None
    assert diagnostics._kiro_cli_extra_logs() == []
    assert "OFFTREE" not in diagnostics.read_kiro_cli_logs()


def test_linked_scratch_logs_level_is_never_walked(tmp_path, monkeypatch):
    home = tmp_path / "home"
    _windows_env(monkeypatch, tmp_path / "temp")
    _isolate_bundle(monkeypatch, home)
    root = agent_scratch.scratch_root()
    off_tree = tmp_path / "off-tree"
    _write(off_tree / "kiro-chat.log", "OFFTREEscratch\n")
    (root / "agent-x" / "kiro-log").mkdir(parents=True)
    make_dir_link(root.joinpath("agent-x", *_NESTED), off_tree)

    assert diagnostics._scratch_kiro_chat_logs() == []
    r = diagnostics.collect_bundle(output_dir=tmp_path / "out")
    with zipfile.ZipFile(r.zip_path) as z:
        assert not [n for n in z.namelist() if n.startswith("kiro-chat/")]
        assert b"OFFTREE" not in b"".join(z.read(n) for n in z.namelist())


def test_one_level_wins_for_mcp_and_lsp(tmp_path, monkeypatch):
    temp = tmp_path / "temp"
    _windows_env(monkeypatch, temp)
    flat_mcp = _write(temp / "kiro-log" / "mcp.log", "flat mcp\n")
    _write(temp.joinpath(*_NESTED, "lsp.log"), "nested lsp\n")
    _write(tmp_path / "second" / "kiro-log" / "mcp.log", "second base mcp\n")
    monkeypatch.setenv("TMP", str(tmp_path / "second"))

    assert diagnostics._kiro_cli_extra_logs() == [flat_mcp]


def test_nested_chat_log_never_reaches_the_protocol_log_reader(tmp_path, monkeypatch):
    temp = tmp_path / "temp"
    _windows_env(monkeypatch, temp)
    _write(temp.joinpath(*_NESTED, "kiro-chat.log"), "PRIVATEconversation\n")
    _write(temp.joinpath(*_NESTED, "mcp.log"), "initialize ok\n")

    out = diagnostics.read_kiro_cli_logs()
    assert "=== mcp.log" in out
    assert "PRIVATEconversation" not in out


def test_nested_mcp_log_reaches_the_protocol_log_reader(tmp_path, monkeypatch):
    temp = tmp_path / "temp"
    _windows_env(monkeypatch, temp)
    _write(temp.joinpath(*_NESTED, "mcp.log"), "initialize ok\n")

    assert diagnostics._kiro_cli_extra_logs() == [temp.joinpath(*_NESTED, "mcp.log")]
    assert "=== mcp.log" in diagnostics.read_kiro_cli_logs()


def test_bundle_carries_nested_host_and_scratch_chat_logs(tmp_path, monkeypatch):
    home = tmp_path / "home"
    temp = tmp_path / "temp"
    _windows_env(monkeypatch, temp)
    _isolate_bundle(monkeypatch, home)
    _write(temp.joinpath(*_NESTED, "kiro-chat.log"), "host log line\n")
    root = agent_scratch.scratch_root()
    _write(root.joinpath("agent-w", *_NESTED, "kiro-chat.log"), "scratch log line\n")

    r = diagnostics.collect_bundle(output_dir=tmp_path / "out")

    with zipfile.ZipFile(r.zip_path) as z:
        names = z.namelist()
        assert "kiro-chat.log" in names
        assert "kiro-chat/agent-w.log" in names
        assert "host log line" in z.read("kiro-chat.log").decode()
        assert "scratch log line" in z.read("kiro-chat/agent-w.log").decode()


def test_scratch_member_name_is_the_scratch_dir_for_both_layouts(tmp_path):
    flat = tmp_path / "agent-a" / "kiro-log" / "kiro-chat.log"
    nested = tmp_path.joinpath("agent-b", *_NESTED, "kiro-chat.log")
    named_logs = tmp_path / "logs" / "kiro-log" / "kiro-chat.log"

    assert diagnostics._scratch_log_dir_name(flat) == "agent-a"
    assert diagnostics._scratch_log_dir_name(nested) == "agent-b"
    assert diagnostics._scratch_log_dir_name(named_logs) == "logs"
