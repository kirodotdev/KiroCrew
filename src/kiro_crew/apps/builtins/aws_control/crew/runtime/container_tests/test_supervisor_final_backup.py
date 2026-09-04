"""Shutdown must upload once more, and only where that means something.

A task replacement is not a rare event: it happens on every deploy, every failed
health check and every platform update. The periodic sidecar cycle runs on a
timer, so a turn finishing between the last cycle and SIGTERM was never uploaded,
and the backend's own 25s drain flushes MORE state after that cycle. Without a
final pass the newest conversation is lost every time the task is replaced.

The ordering is the part worth testing rather than the mere existence of the
call: the cycle must run after the front and backend are drained (nothing is
writing) AND after the sidecar is stopped (exactly one writer). A final cycle
racing a live sidecar over the same keys would be a fix that introduced its own
defect.
"""

from __future__ import annotations

from container.supervisor import __main__ as sup


class _Rec:
    """A stand-in ProcessGroup that records when it was drained."""

    def __init__(self, name: str, log: list[str]) -> None:
        self.name = name
        self._log = log

    def terminate(self, _secs: float) -> None:
        self._log.append(f"drain:{self.name}")


def _settings(tmp_path, *, bucket: str | None):
    s = sup.Settings(
        backend_port=8765,
        backend_run_dir=tmp_path / "run",
        front_port=8080,
        route_prefix="",
        control_secret=None,
        data_home=tmp_path / "data",
        config_dir=tmp_path / "data" / "config",
        crew_name="crew1",
        backup_bucket=bucket,
        backup_prefix="crews",
        backup_interval_secs=0,
    )
    s.sessions_dir.mkdir(parents=True, exist_ok=True)
    return s


def test_the_final_cycle_runs_after_every_writer_is_stopped(tmp_path, monkeypatch):
    order: list[str] = []
    monkeypatch.setattr(sup, "_final_backup_cycle", lambda _s: order.append("final-cycle"))

    front = _Rec("front", order)
    backend = _Rec("backend", order)
    sidecar = _Rec("sidecar", order)

    sup._teardown(front, backend, sidecar, _settings(tmp_path, bucket="bkt"))

    assert order == ["drain:front", "drain:backend", "drain:sidecar", "final-cycle"], order


def test_chatbot_mode_runs_no_final_cycle(tmp_path, monkeypatch):
    """There is no bucket and never was a sidecar, so there is nothing to flush."""
    order: list[str] = []
    monkeypatch.setattr(sup, "_final_backup_cycle", lambda _s: order.append("final-cycle"))

    sup._teardown(
        _Rec("front", order), _Rec("backend", order), None, _settings(tmp_path, bucket=None)
    )

    assert "final-cycle" not in order, "a chatbot crew tried to upload on shutdown"
    assert order == ["drain:front", "drain:backend"], order


def test_the_final_cycle_uploads_what_the_periodic_one_missed(tmp_path, monkeypatch):
    """The whole point: a transcript written after the last cycle still lands."""
    from container.backup.store import InMemoryObjectStore

    settings = _settings(tmp_path, bucket="bkt")
    late = settings.sessions_dir / "dashboard_cust-1.jsonl"
    late.write_text('{"role":"user","content":"the last turn"}\n', encoding="utf-8")

    store = InMemoryObjectStore()
    monkeypatch.setattr("container.backup.sidecar._build_store", lambda _s: store)

    sup._final_backup_cycle(settings)

    # `list` is the store's public enumeration, the same one restore uses.
    landed = [k for k in store.list("") if k.endswith("dashboard_cust-1.jsonl")]
    assert landed, f"the late transcript was not uploaded; store has {sorted(store.list(''))}"


def test_a_failing_final_cycle_does_not_raise_on_the_way_out(tmp_path, monkeypatch):
    """The task is already leaving; a traceback here replaces an actionable log."""
    settings = _settings(tmp_path, bucket="bkt")

    def boom(*_a, **_k):
        raise RuntimeError("S3 is having a day")

    monkeypatch.setattr("container.backup.sidecar._build_store", lambda _s: object())
    monkeypatch.setattr("container.backup.sidecar.run_backup_cycle", boom)

    sup._final_backup_cycle(settings)  # must not raise
