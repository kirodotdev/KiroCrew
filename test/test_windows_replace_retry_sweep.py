"""Tmp + rename writers whose final rename survives a Windows sharing violation.

Each writer here publishes a temp file over its target. On Windows that rename
raises ``PermissionError`` while another handle is open on the destination (an
AV scanner, the search indexer, a concurrent reader), so a bare ``os.replace``
loses the write on the first fault. These writers rename through
``atomic_write.replace_with_retry``.

The behaviour cases fake two faults with ``windows_sim.replace_sharing_violation``
and ``IS_WINDOWS`` forced on, then check the write lands and no temp is left. The
source sweep covers every converted writer, including the ones whose callers
need more setup than a unit test can give, by reading each function's own calls.
Both pin the wiring only, not real OS behaviour.
"""

from __future__ import annotations

import ast
import json
import os
from pathlib import Path

import pytest
from windows_sim import replace_sharing_violation

from kiro_crew import atomic_write as aw
from kiro_crew import platform_compat

_SRC = Path(__file__).resolve().parent.parent / "src" / "kiro_crew"


@pytest.fixture(autouse=True)
def _windows_without_backoff(_floor_monkeypatch: pytest.MonkeyPatch) -> None:
    _floor_monkeypatch.setattr(platform_compat, "IS_WINDOWS", True)
    _floor_monkeypatch.setattr(aw, "_REPLACE_BACKOFF_SECONDS", 0)


def _temps(directory: Path) -> list[str]:
    return sorted(p.name for p in directory.iterdir() if ".tmp" in p.name)


def test_mochi_queue_write_survives_a_contended_rename(tmp_path: Path) -> None:
    from kiro_crew.apps.builtins.mochi import queue_file as qf

    path = tmp_path / "queue.json"
    with replace_sharing_violation(match="queue.json", times=2) as sim:
        qf.write_queue_atomic(path, {"tasks": []})  # type: ignore[typeddict-item]

    assert sim["n"] == 3
    assert json.loads(path.read_text("utf-8")) == {"tasks": []}
    assert _temps(tmp_path) == []


def test_mochi_queue_write_removes_its_temp_when_the_hold_outlasts_the_retry(
    tmp_path: Path,
) -> None:
    from kiro_crew.apps.builtins.mochi import queue_file as qf

    path = tmp_path / "queue.json"
    path.write_text('{"tasks": ["old"]}', encoding="utf-8")
    with (
        replace_sharing_violation(match="queue.json", times=aw._REPLACE_MAX_ATTEMPTS),
        pytest.raises(PermissionError),
    ):
        qf.write_queue_atomic(path, {"tasks": []})  # type: ignore[typeddict-item]

    assert json.loads(path.read_text("utf-8")) == {"tasks": ["old"]}
    assert _temps(tmp_path) == []


def test_weixin_state_write_survives_a_contended_rename(tmp_path: Path) -> None:
    from kiro_crew.weixin import client

    path = tmp_path / "context.json"
    with replace_sharing_violation(match="context.json", times=2) as sim:
        client._atomic_json_write(path, {"token": "new"})

    assert sim["n"] == 3
    assert json.loads(path.read_text("utf-8")) == {"token": "new"}
    assert _temps(tmp_path) == []


def test_weixin_state_write_removes_its_temp_when_the_hold_outlasts_the_retry(
    tmp_path: Path,
) -> None:
    from kiro_crew.weixin import client

    path = tmp_path / "context.json"
    with (
        replace_sharing_violation(match="context.json", times=aw._REPLACE_MAX_ATTEMPTS),
        pytest.raises(PermissionError),
    ):
        client._atomic_json_write(path, {"token": "new"})

    assert not path.exists()
    assert _temps(tmp_path) == []


def test_mochi_activity_log_write_survives_a_contended_rename(tmp_path: Path) -> None:
    from kiro_crew.apps.builtins.mochi import activity_log as al

    path = tmp_path / "activity.json"
    store = {"date": "2026-11-02", "entries": []}
    with replace_sharing_violation(match="activity.json", times=2) as sim:
        assert al._write_store(path, store) is True  # type: ignore[arg-type]

    assert sim["n"] == 3
    assert json.loads(path.read_text("utf-8")) == store
    assert _temps(tmp_path) == []


def test_auto_improvement_json_write_survives_a_contended_rename(tmp_path: Path) -> None:
    from kiro_crew.apps.builtins.auto_improvement.backend import store

    path = tmp_path / "ruler.json"
    with replace_sharing_violation(match="ruler.json", times=2) as sim:
        store.write_json_atomic(path, {"calibrated": True})

    assert sim["n"] == 3
    assert json.loads(path.read_text("utf-8")) == {"calibrated": True}
    assert _temps(tmp_path) == []


def test_resolved_launch_record_survives_a_contended_rename(tmp_path: Path) -> None:
    from kiro_crew.mcp_gateway import resolve_once

    record = resolve_once.ResolvedRecord(
        package="pkg", entrypoint="/x/index.js", resolved_at=1.0, pinned=True
    )
    with replace_sharing_violation(match="record.", times=2) as sim:
        resolve_once.write_record(str(tmp_path), record)

    assert sim["n"] == 3
    assert resolve_once.read_record(str(tmp_path)) == record
    assert _temps(tmp_path) == []


def test_session_pulse_count_survives_a_contended_rename(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from kiro_crew.dashboard import session_pulse_counter as spc

    path = tmp_path / "pulse.json"
    monkeypatch.setattr(spc, "_path", lambda: path)
    with replace_sharing_violation(match="pulse.json", times=2) as sim:
        assert spc.increment_user_session_count() == 1

    assert sim["n"] == 3
    assert spc.get_user_session_count() == 1
    assert _temps(tmp_path) == []


def test_token_revocation_bump_survives_a_contended_rename(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from kiro_crew.config import loader
    from kiro_crew.dashboard import revocation_gen as rg

    monkeypatch.setattr(loader, "config_dir", lambda: tmp_path)
    monkeypatch.setattr(rg, "_gen", None, raising=False)
    with replace_sharing_violation(match=rg._REVOCATION_FILE, times=2) as sim:
        new_gen = rg.bump_revocation_gen()

    assert sim["n"] == 3
    assert (tmp_path / rg._REVOCATION_FILE).read_text("utf-8") == str(new_gen)
    assert _temps(tmp_path) == []


def test_pinned_source_write_by_name_survives_a_contended_rename(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from kiro_crew import hooks

    src = tmp_path / "prompt.md"
    src.write_text("old", encoding="utf-8")
    # No directory-handle APIs: the staged payload is renamed by name, as on Windows.
    monkeypatch.setattr(os, "supports_dir_fd", set(), raising=False)
    with replace_sharing_violation(match="prompt.md", times=2) as sim:
        assert hooks.safe_write_file_nolink(str(src), "new", str(tmp_path)) is True

    assert sim["n"] == 3
    assert src.read_text(encoding="utf-8") == "new"
    assert sorted(p.name for p in tmp_path.iterdir()) == ["prompt.md"]


def _save_during_backoff(target: Path, newer: str):  # type: ignore[no-untyped-def]
    """Fail the first rename onto *target* the way a scanner on the temp does, and
    land an outside save of *newer* on *target* while the retry backs off."""
    real_replace = os.replace
    calls = {"n": 0}

    def _patched(src, dst, *args, **kwargs):  # type: ignore[no-untyped-def]
        if os.path.basename(str(dst)) == target.name:
            calls["n"] += 1
            if calls["n"] == 1:
                with open(target, "a", encoding="utf-8") as handle:
                    handle.write(newer)
                raise PermissionError("[WinError 32] simulated sharing violation on the temp")
        return real_replace(src, dst, *args, **kwargs)

    return _patched, calls


def test_pinned_write_does_not_overwrite_a_save_landing_during_the_retry(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    import hashlib

    from kiro_crew import hooks

    src = tmp_path / "prompt.md"
    src.write_text("old", encoding="utf-8")
    base = hashlib.sha256(b"old").hexdigest()
    monkeypatch.setattr(os, "supports_dir_fd", set(), raising=False)
    patched, calls = _save_during_backoff(src, " plus a newer save")
    with pytest.MonkeyPatch.context() as mp:
        mp.setattr(os, "replace", patched)
        outcome = hooks.verified_replace_file_nolink(
            str(src), "stale edit", base, max_bytes=1 << 20, within_root=str(tmp_path)
        )

    assert outcome == "conflict"
    assert calls["n"] == 1  # the re-check stopped the retry before a second rename
    assert src.read_text(encoding="utf-8") == "old plus a newer save"
    assert sorted(p.name for p in tmp_path.iterdir()) == ["prompt.md"]


def test_transcript_repair_does_not_overwrite_an_append_landing_during_the_retry(
    tmp_path: Path,
) -> None:
    from kiro_crew import session_image_repair as sir

    path = tmp_path / "s.jsonl"
    path.write_text('{"kind": "Prompt"}\n', encoding="utf-8")
    st = path.stat()
    appended = '{"kind": "Prompt", "late": true}\n'
    patched, calls = _save_during_backoff(path, appended)
    with pytest.MonkeyPatch.context() as mp:
        mp.setattr(os, "replace", patched)
        with pytest.raises(sir.TranscriptMovedError):
            sir.apply_repair(
                path, [b'{"kind": "Prompt"}'], backup=False, expect=(st.st_size, st.st_mtime_ns)
            )

    assert calls["n"] == 1
    assert path.read_text(encoding="utf-8").endswith(appended)
    assert _temps(tmp_path) == []


def test_before_each_runs_before_every_attempt_and_its_raise_stops_the_rename(
    tmp_path: Path,
) -> None:
    src, dst = tmp_path / "a.tmp", tmp_path / "a.json"
    src.write_text("new", encoding="utf-8")
    seen: list[int] = []

    def _check() -> None:
        seen.append(len(seen))
        if len(seen) == 3:
            raise LookupError("changed")

    with replace_sharing_violation(match="a.json", times=5), pytest.raises(LookupError):
        aw.replace_with_retry_checked(src, dst, _check)

    assert seen == [0, 1, 2]
    assert src.exists() and not dst.exists()


# Every converted writer: (file under src/kiro_crew, the function that publishes).
_CONVERTED: list[tuple[str, str]] = [
    ("session_image_repair.py", "apply_repair"),
    ("dashboard/part_stream.py", "commit"),
    ("dashboard/session_pulse_counter.py", "increment_user_session_count"),
    ("dashboard/revocation_gen.py", "bump_revocation_gen"),
    ("ledger_wake.py", "_write_rate"),
    ("hook_runtime/pinned_writes.py", "_pinned_replace"),
    ("mcp_gateway/rewriter.py", "_store_fingerprint"),
    ("mcp_gateway/resolve_once.py", "write_record"),
    ("subagent_cost.py", "compact_cost_log"),
    ("acp/harness/pi.py", "_publish_gate_artifact"),
    ("decisions/local_runtime.py", "_write_receipt"),
    ("mcp_tools/control.py", "register_hook"),
    ("snapshot_restore.py", "_save"),
    ("eval/bench/safepath.py", "write_text_atomic_nofollow"),
    ("apps/builtins/auto_improvement/backend/store.py", "write_json_atomic"),
    ("apps/builtins/mochi/queue_file.py", "write_queue_atomic"),
    ("apps/builtins/mochi/activity_log.py", "_write_store"),
    ("apps/builtins/crew_companion/store.py", "_save_locked"),
    ("apps/builtins/design_tweak/backend/request_state.py", "atomic_write_json"),
    ("apps/builtins/code_review_sage/sage_lib/store.py", "atomic_write_locked"),
    ("weixin/client.py", "_atomic_json_write"),
    ("embeddings_download.py", "_install_file"),
    ("sel_rotation.py", "_prune_live_locked"),
    ("crewmate_stage.py", "_write_private"),
    ("browser_harness/recording.py", "_write_meta"),
    ("dashboard/handlers/crewmate_import_files.py", "_stage"),
    ("apps/builtin_adoption_fetch.py", "_download_archive_sync"),
]


def _call_name(node: ast.Call) -> str:
    func = node.func
    if isinstance(func, ast.Name):
        return func.id
    if isinstance(func, ast.Attribute):
        return func.attr
    return ""


def _is_by_name_rename(node: ast.Call) -> bool:
    """``os.replace(a, b)`` or ``a.replace(b)`` on a path; ``dir_fd`` renames are POSIX-only."""
    if _call_name(node) != "replace":
        return False
    if any(kw.arg in {"src_dir_fd", "dst_dir_fd"} for kw in node.keywords):
        return False
    func = node.func
    assert isinstance(func, ast.Attribute)
    owner = func.value
    if (
        isinstance(owner, ast.Name)
        and owner.id == "os"
        or (isinstance(owner, ast.Attribute) and owner.attr == "os")
    ):
        return len(node.args) == 2
    # ``tmp.replace(path)``: a Path method, not ``str.replace(old, new)``.
    return len(node.args) == 1 and not node.keywords


@pytest.mark.parametrize(("rel", "function"), _CONVERTED, ids=[r for r, _ in _CONVERTED])
def test_converted_writer_renames_through_the_retry(rel: str, function: str) -> None:
    tree = ast.parse((_SRC / rel).read_text(encoding="utf-8"))
    scopes = [
        n
        for n in ast.walk(tree)
        if isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef)) and n.name == function
    ]
    assert scopes, f"{rel}: no function {function!r}"
    calls = [n for scope in scopes for n in ast.walk(scope) if isinstance(n, ast.Call)]
    bare = [n.lineno for n in calls if _is_by_name_rename(n)]
    assert bare == [], f"{rel}: bare by-name rename at line(s) {bare}"
    assert any(
        _call_name(n) in {"replace_with_retry", "replace_with_retry_checked", "_replace"}
        for n in calls
    ), rel


def test_crewmate_stage_write_survives_a_contended_rename(tmp_path: Path) -> None:
    from kiro_crew import crewmate_stage

    target = tmp_path / "abc.crewmate"
    target.write_bytes(b"old")
    with replace_sharing_violation(match="abc.crewmate", times=2) as sim:
        crewmate_stage._write_private(target, b"new")

    assert sim["n"] == 3
    assert target.read_bytes() == b"new"
    assert _temps(tmp_path) == []


def _archive_pin(tmp_path: Path, body: bytes):  # type: ignore[no-untyped-def]
    import hashlib

    from kiro_crew.apps.builtin_adoption_pins import AdoptionPin

    source = tmp_path / "source.tar.gz"
    source.write_bytes(body)
    return AdoptionPin(
        name="demo",
        repository="https://github.com/example/demo",
        commit="0" * 40,
        version="1.0.0",
        tree_sha256="0" * 64,
        archive_sha256=hashlib.sha256(body).hexdigest(),
        min_app_sdk_level=1,
        legacy_route="",
        legacy_icon="",
        harness_archive=str(source),
    )


def test_adoption_archive_publish_survives_a_contended_rename(tmp_path: Path) -> None:
    from kiro_crew.apps import builtin_adoption_fetch as fetch

    pin = _archive_pin(tmp_path, b"archive-bytes")
    dest = tmp_path / "out" / "demo.tar.gz"
    dest.parent.mkdir()
    with replace_sharing_violation(match="demo.tar.gz", times=2) as sim:
        fetch._download_archive_sync(pin, dest)

    assert sim["n"] == 3
    assert dest.read_bytes() == b"archive-bytes"
    assert _temps(dest.parent) == [] and [p.name for p in dest.parent.iterdir()] == [dest.name]


def test_a_held_adoption_archive_is_a_local_failure_not_a_network_one(tmp_path: Path) -> None:
    from kiro_crew.apps import builtin_adoption_fetch as fetch

    pin = _archive_pin(tmp_path, b"archive-bytes")
    dest = tmp_path / "out" / "demo.tar.gz"
    dest.parent.mkdir()
    with (
        replace_sharing_violation(match="demo.tar.gz", times=aw._REPLACE_MAX_ATTEMPTS),
        pytest.raises(fetch.AdoptionRefused) as refused,
    ):
        fetch._download_archive_sync(pin, dest)

    assert refused.value.code == "stage_io_failed"
    assert list(dest.parent.iterdir()) == []
