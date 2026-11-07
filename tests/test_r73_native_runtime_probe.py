"""The runtime admission probe never turns a selectable row into security proof."""
from __future__ import annotations

from pathlib import Path

from scripts.probe_crew12_runtime_registration import probe


def _fake_host(tmp_path: Path, *, known: bool) -> Path:
    src = tmp_path / 'src'
    base = src / 'kiro_crew' / 'agent_sdk'
    base.mkdir(parents=True)
    (src / 'kiro_crew' / '__init__.py').write_text('')
    (base / '__init__.py').write_text('')
    (base / 'backends.py').write_text('''
from enum import Enum
class Routing(Enum):
    UNVERIFIED='unverified'
    TEST='test'
ACP_BACKENDS_KNOWN=frozenset({'kiro-byo'}) if '''+str(known)+''' else frozenset({'kas'})
_selectable=set(ACP_BACKENDS_KNOWN)
def selectable_backends():return frozenset(_selectable)
def register_selectable_backend(b):
    if b not in ACP_BACKENDS_KNOWN:raise ValueError('unknown')
    _selectable.add(b)
def routing_for(b):return Routing.TEST if b in ACP_BACKENDS_KNOWN else Routing.UNVERIFIED
''')
    (base / 'backends_launch.py').write_text('ACP_BACKEND_LAUNCH={"kiro-byo":object()}\n' if known else 'ACP_BACKEND_LAUNCH={}\n')
    (base / 'tool_gate.py').write_text('''
from kiro_crew.agent_sdk.backends import routing_for
from enum import Enum
class Verdict(Enum):
    ROUTED='routed'
    INDETERMINATE='indeterminate'
def routing_verdict(b):return (Verdict.ROUTED if routing_for(b).value=='test' else Verdict.INDETERMINATE,'declaration')
def is_enforced(b):return False
''')
    return src


def test_unknown_native_backend_is_refused(tmp_path):
    result = probe(_fake_host(tmp_path, known=False))
    assert result['state'] == 'blocked_unknown'
    assert result['registration']['allowed'] is False
    assert result['gatewayWorkerMcpE2E'] == 'not_verified'


def test_selectable_unenforced_backend_never_certified(tmp_path):
    result = probe(_fake_host(tmp_path, known=True))
    assert result['registration']['allowed'] is True
    assert result['hostRoutingVerdict'] == 'routed'
    assert result['state'] == 'registered_without_host_enforcement'
    assert result['toolAuthorizationProof'] == 'not_verified'
