"""Tests for :mod:`kiro_crew.diag.memory_snapshot` and the two places that write it.

Every host read is a fixture ``/proc`` tree, and the family scan and posture are
stubbed, so the assertions hold whatever the machine running the suite is doing.
"""

from __future__ import annotations

import zipfile
from pathlib import Path

import pytest

from kiro_crew import diagnostics, resource_status
from kiro_crew.diag import memory_snapshot as ms
from kiro_crew.diag import recorder as rec

#: Planted in a fixture process's argv. The snapshot must never carry it.
_ARGV_SECRET = "--token=ghp_memsnapshotSECRET0123456789abcdef"


def _proc(root: Path, pid: int, name: str, rss_pages: int, argv: str = "") -> None:
    d = root / str(pid)
    d.mkdir(parents=True)
    (d / "comm").write_text(name + "\n", encoding="utf-8")
    (d / "statm").write_text(f"999999 {rss_pages} 10 1 0 5 0\n", encoding="ascii")
    (d / "cmdline").write_bytes((name + "\0" + argv).encode())


def _procfs(root: Path) -> Path:
    root.mkdir(parents=True, exist_ok=True)
    (root / "meminfo").write_text(
        "MemTotal:       16000000 kB\n"
        "MemAvailable:    1600000 kB\n"
        "SwapTotal:             0 kB\n"
        "SwapFree:              0 kB\n",
        encoding="utf-8",
    )
    _proc(root, 101, "java", 1_000_000, _ARGV_SECRET)
    _proc(root, 202, "kiro-cli", 200_000, _ARGV_SECRET)
    _proc(root, 303, "python3.12", 50_000)
    _proc(root, 404, "bash", 10)
    return root


def _family(_root: Path):
    return {202: ("chat", 800_000), 303: ("mcp-server", 200_000), 9999: ("mcp-server", None)}, []


def _posture():
    return {"posture": "critical", "available_gb": 1.6, "pressure_gb": 4.0, "critical_gb": 2.0}


@pytest.fixture(autouse=True)
def _page_size(_floor_monkeypatch: pytest.MonkeyPatch) -> None:
    _floor_monkeypatch.setattr(ms, "_page_size", lambda: 4096)


def test_host_list_is_biggest_first_with_names_only(tmp_path: Path) -> None:
    rows = ms.host_top_processes(_procfs(tmp_path / "proc"), top=3)
    assert rows == [
        (101, "java", 4_000_000),
        (202, "kiro-cli", 800_000),
        (303, "python3.12", 200_000),
    ]


def test_render_marks_crew_processes_totals_kinds_and_never_writes_argv(tmp_path: Path) -> None:
    text = ms.render(
        _procfs(tmp_path / "proc"), reason="test", now=0.0, family_fn=_family, posture_fn=_posture
    )

    assert _ARGV_SECRET not in text
    assert "ghp_" not in text
    assert "host memory: total 15.3 GB available 1.5 GB" in text
    assert "posture: critical" in text
    lines = text.splitlines()
    java = next(line for line in lines if line.endswith(" java"))
    assert java.split() == ["101", "3906", "java"]
    cli = next(line for line in lines if line.endswith(" kiro-cli"))
    assert cli.split() == ["202", "781", "chat", "kiro-cli"]
    assert any(line.split() == ["chat", "count", "1", "rss_mb", "781"] for line in lines)
    assert any(line.split() == ["mcp-server", "count", "2", "rss_mb", "195"] for line in lines)
    assert any(line.split() == ["total", "count", "3", "rss_mb", "977"] for line in lines)


def test_render_says_the_list_is_unavailable_when_proc_is_not_there(tmp_path: Path) -> None:
    text = ms.render(tmp_path / "absent", now=0.0, family_fn=lambda _r: ({}, []), posture_fn=dict)
    assert "top processes by RSS: not available" in text
    assert "  none found" in text


def test_critical_snapshots_keep_only_the_newest(tmp_path: Path) -> None:
    diag = tmp_path / "diag"
    for i in range(ms.KEEP_CRITICAL + 2):
        assert ms.write_critical(diag, f"snap {i}\n", 1_700_000_000.0 + i) is not None
    kept = ms.latest_critical(diag, 100)
    assert len(kept) == ms.KEEP_CRITICAL
    assert kept[0].read_text(encoding="utf-8") == f"snap {ms.KEEP_CRITICAL + 1}\n"


def test_a_critical_snapshot_is_synced_to_disk(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from kiro_crew import atomic_write as aw

    calls: list[object] = []
    real_write = aw.atomic_write
    monkeypatch.setattr(
        aw,
        "atomic_write",
        lambda path, text, **kw: (
            calls.append(("file", kw.get("fsync"))),
            real_write(path, text, **kw),
        )[1],
    )
    monkeypatch.setattr(aw, "fsync_dir", lambda path, **kw: calls.append(("dir", Path(path).name)))

    ms.write_critical(tmp_path / "diag", "snap\n", 1_700_000_000.0)

    assert ("file", True) in calls
    assert ("dir", ms.CRITICAL_DIR_NAME) in calls


def test_a_restarted_recorder_keeps_the_gap_from_the_snapshot_on_disk(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(rec, "_read_posture", lambda: {**_posture(), "cpu_count": 4})
    monkeypatch.setattr(ms, "_family_kinds", _family)
    home = tmp_path / "home"
    home.mkdir()
    ms.write_critical(home / "diag", "from the previous gateway\n", 1_700_000_000.0)

    def _recorder(now: float) -> rec.Recorder:
        return rec.Recorder(
            config_dir=home,
            env={},
            clock=lambda: now,
            procfs=_procfs(tmp_path / f"proc-{int(now)}"),
            statvfs=None,
            mounts=(),
        )

    _recorder(1_700_000_000.0 + 60).sample_now()
    assert len(ms.latest_critical(home / "diag", 10)) == 1
    # The name keeps whole seconds only, so the gap counts from the second after it.
    _recorder(1_700_000_000.0 + ms.CRITICAL_MIN_GAP_SECS).sample_now()
    assert len(ms.latest_critical(home / "diag", 10)) == 1
    _recorder(1_700_000_001.0 + ms.CRITICAL_MIN_GAP_SECS).sample_now()
    assert len(ms.latest_critical(home / "diag", 10)) == 2


def test_a_snapshot_written_after_the_clock_stepped_back_is_kept(tmp_path: Path) -> None:
    diag = tmp_path / "diag"
    for i in range(ms.KEEP_CRITICAL):
        ms.write_critical(diag, f"later {i}\n", 1_700_000_000.0 + 3600 + i)
    written = ms.write_critical(diag, "after the step back\n", 1_700_000_000.0)

    kept = ms.latest_critical(diag, 100)
    assert written in kept
    assert len(kept) == ms.KEEP_CRITICAL


def test_the_recorder_and_resource_status_agree_on_the_critical_name() -> None:
    assert rec.POSTURE_CRITICAL == resource_status.POSTURE_CRITICAL
    assert rec.DIR_NAME == ms.DIAG_DIR_NAME


def test_recorder_writes_one_snapshot_per_entry_into_critical(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    posture = {**_posture(), "posture": "tight", "cpu_count": 4, "load_per_cpu": 0.1}
    monkeypatch.setattr(rec, "_read_posture", lambda: dict(posture))
    monkeypatch.setattr(ms, "_family_kinds", _family)
    clock = {"now": 1_700_000_000.0}
    home = tmp_path / "home"
    home.mkdir()
    recorder = rec.Recorder(
        config_dir=home,
        env={},
        clock=lambda: clock["now"],
        procfs=_procfs(tmp_path / "proc"),
        statvfs=None,
        mounts=(),
    )

    recorder.sample_now()
    assert ms.latest_critical(home / "diag", 10) == []

    posture["posture"] = "critical"
    recorder.sample_now()
    clock["now"] += 30
    recorder.sample_now()  # still critical: no second snapshot
    snaps = ms.latest_critical(home / "diag", 10)
    assert len(snaps) == 1
    body = snaps[0].read_text(encoding="utf-8")
    assert "reason: memory posture turned critical" in body
    assert "kiro-cli" in body and _ARGV_SECRET not in body

    # Leaving and re-entering inside the gap writes nothing new; after it, one more.
    posture["posture"] = "tight"
    recorder.sample_now()
    posture["posture"] = "critical"
    recorder.sample_now()
    assert len(ms.latest_critical(home / "diag", 10)) == 1
    clock["now"] += ms.CRITICAL_MIN_GAP_SECS
    posture["posture"] = "tight"
    recorder.sample_now()
    posture["posture"] = "critical"
    recorder.sample_now()
    assert len(ms.latest_critical(home / "diag", 10)) == 2


def test_bundle_carries_memory_txt_and_the_critical_snapshots(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    home = tmp_path / "home"
    home.mkdir()
    monkeypatch.setattr(diagnostics, "config_dir", lambda: home)
    monkeypatch.setattr(diagnostics, "_macos_crash_reports", lambda: [])
    monkeypatch.setattr(diagnostics, "_kiro_cli_chat_log", lambda: None)
    monkeypatch.setattr(diagnostics, "_kiro_cli_extra_logs", lambda: [])
    monkeypatch.setattr(diagnostics, "_scratch_kiro_chat_logs", lambda: [])
    monkeypatch.setattr(diagnostics, "_kiro_cli_version", lambda: "kiro-cli 0.0.0")
    monkeypatch.setattr(ms, "render", lambda *a, **k: "host memory: now\n")
    ms.write_critical(home / "diag", "host memory: at the critical moment\n", 1_700_000_000.0)

    result = diagnostics.collect_bundle(output_dir=tmp_path / "out")

    with zipfile.ZipFile(result.zip_path) as z:
        names = set(z.namelist())
        assert z.read("memory.txt").decode() == "host memory: now\n"
        critical = [n for n in names if n.startswith("memory-critical/")]
        assert critical == ["memory-critical/memory-critical-20231114T221320Z.txt"]
        assert "at the critical moment" in z.read(critical[0]).decode()


def test_outside_the_gateway_the_family_is_rooted_at_the_running_gateway(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from kiro_crew.diag import procs

    seen: list[int | None] = []

    def _scan(**kwargs):
        seen.append(kwargs.get("gateway_pid"))
        roster = procs.Roster(gateway_pid=kwargs.get("gateway_pid"))
        roster.nodes = {202: procs.ProcNode(pid=202, ppid=1, kind="gateway", rss_kb=800_000)}
        return roster

    monkeypatch.setattr(procs, "scan", _scan)
    text = ms.render(_procfs(tmp_path / "proc"), now=0.0, posture_fn=dict, find_gateway=lambda: 202)

    assert seen == [202]
    cli = next(line for line in text.splitlines() if line.endswith(" kiro-cli"))
    assert cli.split() == ["202", "781", "gateway", "kiro-cli"]


def test_outside_the_gateway_with_none_running_the_snapshot_says_so(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from kiro_crew.diag import procs

    def _scan(**_kwargs):
        raise AssertionError("must not root the family at the doctor process")

    monkeypatch.setattr(procs, "scan", _scan)
    text = ms.render(
        _procfs(tmp_path / "proc"), now=0.0, posture_fn=dict, find_gateway=lambda: None
    )

    assert "  none found" in text
    assert "note: no running gateway found" in text


def test_running_gateway_pid_reads_the_gateway_lock_holder(monkeypatch: pytest.MonkeyPatch) -> None:
    from kiro_crew import gateway_lock

    monkeypatch.setattr(
        gateway_lock,
        "lock_holder",
        lambda _home: gateway_lock.LockHolder(pid=4242, alive=True, source="flock_owner"),
    )
    assert ms.running_gateway_pid() == 4242

    def _indeterminate(home: Path):
        raise gateway_lock.LockProbeError(home, OSError("unreadable"))

    monkeypatch.setattr(gateway_lock, "lock_holder", _indeterminate)
    assert ms.running_gateway_pid() is None


def test_doctor_bundle_finds_the_gateway_and_the_dashboard_bundle_does_not(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    home = tmp_path / "home"
    home.mkdir()
    monkeypatch.setattr(diagnostics, "config_dir", lambda: home)
    monkeypatch.setattr(diagnostics, "_macos_crash_reports", lambda: [])
    monkeypatch.setattr(diagnostics, "_kiro_cli_chat_log", lambda: None)
    monkeypatch.setattr(diagnostics, "_kiro_cli_extra_logs", lambda: [])
    monkeypatch.setattr(diagnostics, "_scratch_kiro_chat_logs", lambda: [])
    monkeypatch.setattr(diagnostics, "_kiro_cli_version", lambda: "kiro-cli 0.0.0")
    finders: list[object] = []
    monkeypatch.setattr(ms, "render", lambda *a, **k: finders.append(k.get("find_gateway")) or "")

    diagnostics.collect_bundle(output_dir=tmp_path / "out")
    diagnostics.collect_bundle(output_dir=tmp_path / "out", outside_gateway=True)

    assert finders == [None, ms.running_gateway_pid]
