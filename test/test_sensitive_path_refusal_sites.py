"""Agent-facing refusal sites word a resolver stall as a stall.

Each site now takes ``security.sensitive_path_refusal``'s reason: a stall is passed
through verbatim, a match keeps the site's own wording, and both still refuse. The
stall is stubbed on the owning module, the global the real producer reads.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from kiro_crew import security
from kiro_crew.artifacts import ArtifactError, ArtifactStore


def _stall(monkeypatch) -> None:
    def stalled(*args, **kwargs):
        raise security.PathResolutionStalled("/x", "/x")

    monkeypatch.setattr(security.paths, "_path_in_home_dirs", stalled)


def _match(monkeypatch) -> None:
    monkeypatch.setattr(security.paths, "_path_in_home_dirs", lambda *a, **k: True)


def _assert_stall(text: str) -> None:
    assert "NOT a match" in text
    assert "sensitive path:" not in text
    assert "not allowed" not in text


def test_artifact_root_stall_is_worded_as_a_stall(tmp_path: Path, monkeypatch) -> None:
    _stall(monkeypatch)
    with pytest.raises(ArtifactError) as info:
        ArtifactStore(root=tmp_path / "artifacts")
    assert security.is_unverifiable_path_refusal(str(info.value))
    _assert_stall(str(info.value))


def test_artifact_root_match_keeps_its_wording(tmp_path: Path, monkeypatch) -> None:
    _match(monkeypatch)
    with pytest.raises(ArtifactError, match="refusing to use sensitive path as artifact root"):
        ArtifactStore(root=tmp_path / "artifacts")


@pytest.mark.asyncio
async def test_artifact_file_helpers_word_an_on_loop_stall_as_a_stall(
    tmp_path: Path, monkeypatch
) -> None:
    store = ArtifactStore(root=tmp_path / "artifacts")
    target = tmp_path / "note.txt"
    target.write_text("hello", encoding="utf-8")
    _stall(monkeypatch)
    calls = (
        lambda: store._read_text(target),
        lambda: store._read_bytes(target),
        lambda: store._write_text(target, "edited"),
        lambda: store._write_bytes(target, b"edited"),
    )
    for call in calls:
        with pytest.raises(ArtifactError) as info:
            call()
        assert security.is_unverifiable_path_refusal(str(info.value))
        _assert_stall(str(info.value))
    assert target.read_text(encoding="utf-8") == "hello"


@pytest.mark.asyncio
async def test_artifact_file_helpers_keep_the_match_wording_on_the_loop(
    tmp_path: Path, monkeypatch
) -> None:
    store = ArtifactStore(root=tmp_path / "artifacts")
    target = tmp_path / "note.txt"
    target.write_text("hello", encoding="utf-8")
    _match(monkeypatch)
    with pytest.raises(ArtifactError, match="^refusing to read sensitive path: "):
        store._read_text(target)
    with pytest.raises(ArtifactError, match="^refusing to write sensitive path: "):
        store._write_bytes(target, b"edited")


def test_cron_script_resolution_stall_is_worded_as_a_stall(tmp_path: Path, monkeypatch) -> None:
    from kiro_crew.cron_script import resolve_script_path

    script = tmp_path / "job.py"
    script.write_text("def run(ctx):\n    pass\n", encoding="utf-8")
    _stall(monkeypatch)
    with pytest.raises(PermissionError) as info:
        resolve_script_path(f"{script}:run")
    assert security.is_unverifiable_path_refusal(str(info.value))
    _assert_stall(str(info.value))
    _match(monkeypatch)
    with pytest.raises(PermissionError, match="Script path blocked by security policy"):
        resolve_script_path(f"{script}:run")


def test_cron_script_vetting_stall_is_worded_as_a_stall(tmp_path: Path, monkeypatch) -> None:
    from kiro_crew.mcp_cron import _vet_script_file

    script = tmp_path / "job.py"
    script.write_text("def run(ctx):\n    pass\n", encoding="utf-8")
    _stall(monkeypatch)
    out = _vet_script_file(str(script))
    assert out is not None and out.startswith("Error: ")
    assert security.is_unverifiable_path_refusal(out.removeprefix("Error: "))
    _assert_stall(out)
    _match(monkeypatch)
    out = _vet_script_file(str(script))
    assert out is not None and "resolves to a sensitive credential path" in out


def test_mochi_pin_file_stall_is_worded_as_a_stall(tmp_path: Path, monkeypatch) -> None:
    from kiro_crew.apps.builtins.mochi import mcp_server

    monkeypatch.setattr(mcp_server, "_data_dir", lambda: tmp_path)
    target = tmp_path / "notes.md"
    target.write_text("hello", encoding="utf-8")
    _stall(monkeypatch)
    out = mcp_server._tool_pin_file({"path": str(target)})
    assert out.startswith("Error: pin_file failed: ")
    assert security.is_unverifiable_path_refusal(out.removeprefix("Error: pin_file failed: "))
    _assert_stall(out)
    assert not (tmp_path / "pinned-files.json").exists()
