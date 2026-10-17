"""The public-status audit keeps one entry per link only for its dedup window."""

from types import SimpleNamespace

import pytest

from kiro_crew.dashboard import state

WINDOW = state._PUBLIC_STATUS_GRANT_WINDOW_SECS
RECORDERS = [
    pytest.param(state._audit_public_status_grant, "_PUBLIC_STATUS_GRANT_AUDIT", id="grant"),
    pytest.param(state._audit_public_status_denied, "_PUBLIC_STATUS_DENY_AUDIT", id="denied"),
]


class _Clock:
    def __init__(self) -> None:
        self.now = 1000.0

    def monotonic(self) -> float:
        return self.now


class _Sink:
    def __init__(self) -> None:
        self.records: list[dict] = []

    def log_api_access(self, **kwargs: object) -> None:
        self.records.append(kwargs)


@pytest.fixture
def audit(monkeypatch, tmp_path):
    monkeypatch.setenv("KIROCREW_HOME", str(tmp_path))
    clock, sink = _Clock(), _Sink()
    monkeypatch.setattr(state, "time", SimpleNamespace(monotonic=clock.monotonic))
    monkeypatch.setattr(state, "sel", lambda: sink)
    state._PUBLIC_STATUS_GRANT_AUDIT.clear()
    state._PUBLIC_STATUS_DENY_AUDIT.clear()
    yield clock, sink
    state._PUBLIC_STATUS_GRANT_AUDIT.clear()
    state._PUBLIC_STATUS_DENY_AUDIT.clear()


@pytest.mark.parametrize("record, table", RECORDERS)
def test_links_from_past_windows_are_not_kept(audit, record, table):
    clock, _ = audit
    for i in range(10_000):
        record(f"https://github.com/example/repo/pull/{i}")
        clock.now += 1.0
    kept = len(getattr(state, table))
    assert kept == int(WINDOW), f"{kept} links kept for a {WINDOW:.0f} s window"


@pytest.mark.parametrize("record, table", RECORDERS)
def test_one_link_is_still_audited_once_per_window(audit, record, table):
    clock, sink = audit
    url = "https://github.com/example/repo/pull/1"
    record(url)
    clock.now += WINDOW - 1
    record(url)
    assert len(sink.records) == 1
    clock.now += 1
    record(url)
    assert len(sink.records) == 2
    record("https://github.com/example/repo/pull/2")
    clock.now += 1
    record(url)
    assert len(sink.records) == 3
    assert url in getattr(state, table)
