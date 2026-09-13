"""``GET /api/sidecar/status`` shape and component derivation (§3.8).

The handler must return the exact ``CrewSidecarStatusResult`` shape and derive
every component from REAL readiness, never a hardcoded literal:

* ``dashboard`` from ``state.ready``,
* ``security`` from the safety-override engine being constructed,
* ``channels``/``apps`` ``down`` on a supervised sidecar by design,
* ``state`` ``ready`` iff dashboard AND security are ready, ``degraded`` when
  exactly one is, ``down`` when neither.
"""

from __future__ import annotations

import json
import os
from types import SimpleNamespace
from unittest.mock import MagicMock

import pytest

from kiro_crew.dashboard.handlers_system import _CREW_PROTOCOL, api_sidecar_status

pytestmark = pytest.mark.asyncio


def _request(*, ready: bool, supervised: bool, port: int = 34567) -> MagicMock:
    request = MagicMock()
    request.app = {"state": SimpleNamespace(ready=ready), "supervised": supervised}
    request.transport.get_extra_info.return_value = ("127.0.0.1", port)
    request.url = SimpleNamespace(port=port)
    return request


async def _body(request: MagicMock) -> dict:
    resp = await api_sidecar_status(request)
    return json.loads(resp.body)


async def test_shape_has_exactly_the_spec_keys() -> None:
    data = await _body(_request(ready=True, supervised=True))
    assert set(data) == {"state", "pid", "loopbackBase", "components", "protocol"}
    assert set(data["components"]) == {"channels", "dashboard", "apps", "security"}
    assert data["protocol"] == {"crewProtocol": _CREW_PROTOCOL}
    assert data["pid"] == os.getpid()
    assert data["loopbackBase"] == "http://127.0.0.1:34567"


async def test_supervised_ready_reports_channels_and_apps_down_by_design() -> None:
    data = await _body(_request(ready=True, supervised=True))
    # security is real (safety_override is constructable in the test process).
    assert data["components"]["security"] == "ready"
    assert data["components"]["dashboard"] == "ready"
    assert data["components"]["channels"] == "down"
    assert data["components"]["apps"] == "down"
    assert data["state"] == "ready"


async def test_unsupervised_reports_channels_and_apps_ready() -> None:
    data = await _body(_request(ready=True, supervised=False))
    assert data["components"]["channels"] == "ready"
    assert data["components"]["apps"] == "ready"


async def test_dashboard_not_ready_degrades_overall_state() -> None:
    # security ready, dashboard not -> exactly one up -> degraded.
    data = await _body(_request(ready=False, supervised=True))
    assert data["components"]["dashboard"] == "down"
    assert data["components"]["security"] == "ready"
    assert data["state"] == "degraded"


async def test_loopback_base_prefers_real_bound_socket_port() -> None:
    # sockname wins over the url port so --port auto reports the OS-assigned port.
    request = _request(ready=True, supervised=True, port=40001)
    request.transport.get_extra_info.return_value = ("127.0.0.1", 55555)
    request.url = SimpleNamespace(port=40001)
    data = await _body(request)
    assert data["loopbackBase"] == "http://127.0.0.1:55555"
