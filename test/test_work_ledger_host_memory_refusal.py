"""A work-ledger ``create`` is refused while host memory is critical.

``docs/request-for-change/rfc-work-ledger-resource-pressure.md`` §5 test 6, the part
the owner kept: critical refuses and writes nothing; tight, unknown, ample and
``agent.admission_gate: false`` admit.
"""

from __future__ import annotations

from types import SimpleNamespace

import pytest

from kiro_crew import resource_status
from kiro_crew import work_ledger as wl
from kiro_crew.resource_status import (
    POSTURE_AMPLE,
    POSTURE_CRITICAL,
    POSTURE_TIGHT,
    POSTURE_UNKNOWN,
    AdmissionDecision,
    ResourceStatus,
)

CONDUCTOR = "chat-conductor"

#: The real refusal, captured at import: the suite floor pins it off per test.
_REAL_REFUSAL = wl._refuse_if_host_critical


@pytest.fixture(autouse=True)
def _own_home(tmp_path, _floor_monkeypatch):
    _floor_monkeypatch.setenv("KIROCREW_HOME", str(tmp_path / "home"))


def _board_with_open_item() -> None:
    wl.ensure_conductor(CONDUCTOR, goal="drive the fleet")
    wl.apply_conductor_action(CONDUCTOR, "create", title="t", acceptance={})


def _store_bytes() -> dict[str, bytes]:
    root = wl.conductor_dir(CONDUCTOR)
    return {
        str(p.relative_to(root)): p.read_bytes() for p in sorted(root.rglob("*")) if p.is_file()
    }


def _verdict(monkeypatch, decision: AdmissionDecision) -> None:
    """Put the real refusal back (the suite floor pins it off) and feed it *decision*."""
    monkeypatch.setattr(wl, "_refuse_if_host_critical", _REAL_REFUSAL)
    monkeypatch.setattr(resource_status, "admission_check", lambda cfg=None: decision)


def test_a_create_at_critical_is_refused_names_what_lifts_it_and_writes_nothing(monkeypatch):
    _board_with_open_item()
    before = _store_bytes()
    _verdict(
        monkeypatch, AdmissionDecision(False, POSTURE_CRITICAL, 1.0, "host memory is critical")
    )
    with pytest.raises(wl.WorkLedgerError) as caught:
        wl.apply_conductor_action(CONDUCTOR, "create", title="one more", acceptance={})
    assert caught.value.code == wl.CODE_HOST_MEMORY_CRITICAL
    assert "until memory is no longer critical" in str(caught.value)
    assert _store_bytes() == before


@pytest.mark.parametrize("posture", [POSTURE_TIGHT, POSTURE_UNKNOWN, POSTURE_AMPLE])
def test_a_create_is_admitted_below_critical(monkeypatch, posture):
    _board_with_open_item()
    _verdict(monkeypatch, AdmissionDecision(True, posture, 3.0))
    made = wl.apply_conductor_action(CONDUCTOR, "create", title="one more", acceptance={})
    assert made["item"].item_id


def test_a_disabled_admission_gate_admits_at_critical(monkeypatch):
    status = ResourceStatus(
        available_gb=1.5,
        cpu_count=8,
        load_per_cpu=0.5,
        posture=POSTURE_CRITICAL,
        pressure_gb=4.0,
        critical_gb=2.0,
    )
    cfg = SimpleNamespace(agent=SimpleNamespace(admission_gate=False))
    monkeypatch.setattr(resource_status, "probe", lambda _cfg=None: status)
    assert resource_status.admission_check(cfg).admitted
