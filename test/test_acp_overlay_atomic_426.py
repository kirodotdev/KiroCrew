"""ACP cli.json overlays are written atomically.

A plain ``write_text`` truncates-then-writes, so a crash or concurrent read
mid-write yields an empty/partial file and kiro-cli drops the effort / Tool
Search settings. The overlay writers route through ``atomic_write``
(temp file + ``os.replace``), so readers only ever see a complete file and no
``.tmp`` residue is left behind.
"""

from __future__ import annotations

import json
import os
from pathlib import Path

import pytest

from kiro_crew.providers import acp as acp_mod
from kiro_crew.providers.acp import _write_cli_overlay, _write_tool_search_overlay


def _cli_json(work_dir):
    return work_dir / ".kiro" / "settings" / "cli.json"


def test_cli_overlay_writes_valid_json_no_temp_residue(tmp_path):
    _write_cli_overlay(tmp_path, "claude-opus-5", "high")
    cli = _cli_json(tmp_path)
    data = json.loads(cli.read_text(encoding="utf-8"))  # complete + parseable
    assert "chat.modelDefaults" in data
    assert list(cli.parent.glob("*.tmp")) == []


def test_tool_search_overlay_merges_atomically(tmp_path):
    _write_cli_overlay(tmp_path, "claude-opus-5", "high")
    _write_tool_search_overlay(tmp_path, True)
    cli = _cli_json(tmp_path)
    data = json.loads(cli.read_text(encoding="utf-8"))
    # Both overlays present; second write did not clobber the first.
    assert "chat.modelDefaults" in data
    assert list(cli.parent.glob("*.tmp")) == []


def test_overlay_routes_through_atomic_write(tmp_path, monkeypatch):
    calls: list = []
    real = acp_mod.atomic_write
    monkeypatch.setattr(
        acp_mod, "atomic_write", lambda p, c, **k: (calls.append(p), real(p, c, **k))[1]
    )
    _write_cli_overlay(tmp_path, "gpt-5.6-sol", "medium")
    assert calls, "overlay write must go through atomic_write"


@pytest.mark.parametrize(
    "write",
    [
        lambda work_dir: _write_cli_overlay(work_dir, "claude-opus-5", "low"),
        lambda work_dir: _write_tool_search_overlay(work_dir, False),
    ],
    ids=["effort", "tool_search"],
)
def test_overlay_that_fails_to_read_is_left_whole(tmp_path, write):
    # A cli.json that exists but raises on read is not an absent one: rewriting
    # it from {} would drop the other writer's keys and the person's own.
    _write_cli_overlay(tmp_path, "claude-opus-5", "high")
    _write_tool_search_overlay(tmp_path, True)
    cli = _cli_json(tmp_path)
    data = json.loads(cli.read_text(encoding="utf-8"))
    data["chat.defaultModel"] = "claude-opus-5"
    cli.write_text(json.dumps(data), encoding="utf-8")
    before = cli.read_bytes()

    def _unreadable(_cli_json, _settings_dir):
        raise PermissionError("cli.json is unreadable")

    def _unreadable_pinned(_settings_fd, _cli_json):
        raise PermissionError("cli.json is unreadable")

    with pytest.MonkeyPatch.context() as mp:
        mp.setattr(acp_mod, "_read_named_cli_json", _unreadable)
        mp.setattr(acp_mod, "_read_pinned_cli_json", _unreadable_pinned)
        with pytest.raises(PermissionError):
            write(tmp_path)
    assert cli.read_bytes() == before


@pytest.mark.parametrize("pinned", [True, False], ids=["pinned", "by-name"])
def test_overlay_is_published_as_bytes_so_no_platform_rewrites_line_endings(
    tmp_path, monkeypatch, pinned
):
    # Text mode would turn "\n" into "\r\n" on Windows, and the ceiling would then
    # be measured on text shorter than the file that lands.
    from kiro_crew import atomic_write as atomic_write_module

    monkeypatch.setattr(atomic_write_module, "pinned_parent_replace_supported", lambda: pinned)
    published: list = []
    real = acp_mod.atomic_write

    def capture(path, content, **kwargs):
        published.append(content)
        return real(path, content, **kwargs)

    monkeypatch.setattr(acp_mod, "atomic_write", capture)
    _write_cli_overlay(tmp_path, "claude-opus-5", "high")
    _write_tool_search_overlay(tmp_path, True)

    assert published and all(isinstance(content, bytes) for content in published)
    assert b"\r" not in _cli_json(tmp_path).read_bytes()


def test_by_name_read_versions_the_descriptor_it_reads(tmp_path, monkeypatch):
    # The version a by-name publish is checked against must be the file the read got,
    # so one descriptor is opened, versioned and read. On Windows it is held without
    # delete sharing, so the name keeps that file until the bytes are read.
    from kiro_crew import platform_compat

    _write_cli_overlay(tmp_path, "claude-opus-5", "high")
    cli = _cli_json(tmp_path)
    real_open = platform_compat.open_file_no_reparse
    cli_json_opens: list[dict] = []

    def record_open(path, *args, **kwargs):
        if Path(path).name == "cli.json":
            cli_json_opens.append(kwargs)
        return real_open(path, *args, **kwargs)

    monkeypatch.setattr(platform_compat, "open_file_no_reparse", record_open)
    read_result = acp_mod._read_named_cli_json(cli, cli.parent.resolve())

    assert read_result is not None
    raw, before_read, _after_read = read_result
    on_disk = os.stat(cli, follow_symlinks=False)
    assert raw == cli.read_bytes()
    assert (before_read.st_dev, before_read.st_ino) == (on_disk.st_dev, on_disk.st_ino)
    assert cli_json_opens == [{"nonblocking": True, "links_only": True}]


def test_by_name_read_refuses_a_file_outside_the_lock_folder(tmp_path):
    _write_cli_overlay(tmp_path, "claude-opus-5", "high")
    elsewhere = tmp_path / "elsewhere"
    elsewhere.mkdir()

    with pytest.raises(OSError, match="workspace cli.json overlay"):
        acp_mod._read_named_cli_json(_cli_json(tmp_path), elsewhere.resolve())
