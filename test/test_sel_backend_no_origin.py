"""A backend spawned with no gateway origin must not silently lose its audit rows.

The spawn omits ``KIROCREW_GATEWAY_ORIGIN`` when no valid port is bound or the gateway
is bound to a specific interface (``apps/gateway_origin.py``). The key mask is applied
to every backend. So a backend is in one of two states, and ``audit_log()`` must land
the row as ``api_access`` in both:

* it has the route origin: ``sel()`` is the route client; or
* it has no origin: the key must not be masked, so the real writer still works.

The key's mask is emulated by a ``SecurityEventLog`` that fails to build, as the real
one does when ``trust/sel_hmac.key`` is hidden.
"""

from __future__ import annotations

from typing import Any

import pytest
from app_backend_helpers import app_env_fixture  # noqa: F401
from test_sel_backend_switch import _install_plain, _spy_spawn

from kiro_crew.apps.audit_sdk import AuditSDK
from kiro_crew.dashboard.token_auth import write_app_secret
from kiro_crew.sel import sel_hmac_key_path

_APP = "plain-app"


class _Writer:
    rows: list[dict[str, Any]] = []

    def __init__(self) -> None:
        pass

    def log_api_access(self, **kw: Any) -> None:
        type(self).rows.append(kw)


class _KeyMasked(_Writer):
    def __init__(self) -> None:
        raise RuntimeError("SEL key too short: trust/sel_hmac.key is masked")


def test_audit_log_from_a_backend_with_a_route_but_no_owner_marker_lands_as_a_row(
    app_env, tmp_path, monkeypatch
) -> None:
    """Origin and secret are set, but the token exchange needs the owner-enable marker.

    ``token_auth_tokens.validate_app_secret`` refuses a secret without
    ``host_exec_granted(app)``, so the route is NOT usable. Masking the key then loses every
    row (and refuses every ``critical`` write), so the key must stay and the row must land.
    """
    import kiro_crew.apps.backend as bmod

    _install_plain(tmp_path, _APP)
    write_app_secret(_APP, "per-app-secret")
    seen = _spy_spawn(bmod, monkeypatch)  # the route's origin and secret would be present
    monkeypatch.setattr("kiro_crew.apps.host_exec_grants.host_exec_granted", lambda _n: False)
    captured: dict[str, dict[str, str]] = {}

    def _popen(*_a: Any, **kw: Any) -> None:
        captured["env"] = dict(kw.get("env") or {})
        raise OSError("stop before the real spawn")

    monkeypatch.setattr(bmod.subprocess, "Popen", _popen)
    bmod.start_app_backend(_APP)
    child, masked = captured["env"], str(sel_hmac_key_path()) in seen["hidden"]
    # No marker: no app token, so no route. The child gets no origin (and keeps its secret).
    assert "KIROCREW_GATEWAY_ORIGIN" not in child and child.get("KIROCREW_PROXY_SECRET")

    for k in ("KIROCREW_SPAWNED", "KIROCREW_APP_NAME", "KIROCREW_PROXY_SECRET"):
        monkeypatch.setenv(k, child[k])
    monkeypatch.delenv("KIROCREW_GATEWAY_ORIGIN", raising=False)
    _Writer.rows = []
    monkeypatch.setattr("kiro_crew.sel.SecurityEventLog", _KeyMasked if masked else _Writer)

    AuditSDK(_APP).record("publish", "ok")

    assert [r["operation"] for r in _Writer.rows] == [f"{_APP}.publish"], (
        "audit row lost: key masked=%s, route unusable (no owner-enable marker)" % masked
    )


#: Every module that reads ``KIROCREW_GATEWAY_ORIGIN`` outside the spawn, and the
#: token exchange it uses with the origin. A new reader must be added here on purpose.
_ORIGIN_CONSUMERS = {
    "apps/sel_client.py": 'f"{self._origin}/api/apps/{urllib.parse.quote(self._app)}{path}"',
    "apps/builtins/dev_fleet/server.py": "GatewayPointerBroker(",
}


def test_every_origin_consumer_needs_the_app_token_a_no_marker_app_cannot_mint(
    app_env, monkeypatch
) -> None:
    """Dropping the origin from a no-marker spawn loses no working consumer.

    Each reader trades ``KIROCREW_PROXY_SECRET`` at ``POST /api/apps/<app>/token`` and
    ``validate_app_secret`` refuses that without the owner-enable marker. If a reader
    ever works without the token, the spawn must keep the origin for it.
    """
    from pathlib import Path

    import kiro_crew
    from kiro_crew.dashboard.token_auth import validate_app_secret

    root = Path(kiro_crew.__file__).parent
    readers = {
        p.relative_to(root).as_posix()
        for p in root.rglob("*.py")
        if 'environ.get("KIROCREW_GATEWAY_ORIGIN"' in p.read_text(encoding="utf-8")
        or 'env.get("KIROCREW_GATEWAY_ORIGIN"' in p.read_text(encoding="utf-8")
    }
    # The spawn module sets the variable and checks it for the key mask; it is not a reader.
    readers.discard("apps/backend.py")
    assert readers == set(_ORIGIN_CONSUMERS)
    for rel, marker in _ORIGIN_CONSUMERS.items():
        assert marker in (root / rel).read_text(encoding="utf-8"), rel
    # The pointer broker trades the secret at the same token route.
    broker = (root / "apps/builtins/dev_fleet/pointer_broker.py").read_text(encoding="utf-8")
    assert '/api/apps/{self._app}/token"' in broker and "X-App-Secret" in broker

    write_app_secret(_APP, "per-app-secret")
    monkeypatch.setattr("kiro_crew.apps.host_exec_grants.host_exec_granted", lambda _n: False)
    assert validate_app_secret(_APP, "per-app-secret") is False
    monkeypatch.setattr("kiro_crew.apps.host_exec_grants.host_exec_granted", lambda _n: True)
    assert validate_app_secret(_APP, "per-app-secret") is True


@pytest.mark.parametrize(
    "bound",
    [
        pytest.param({}, id="no-bound-port"),
        pytest.param(
            {"KIROCREW_BOUND_PORT": "8123", "KIROCREW_BOUND_HOST": "10.1.2.3"},
            id="non-loopback-bound-host",
        ),
    ],
)
def test_audit_log_from_a_backend_with_no_origin_lands_as_an_api_access_row(
    app_env, tmp_path, monkeypatch, bound
) -> None:
    import kiro_crew.apps.backend as bmod

    for var in ("KIROCREW_BOUND_PORT", "KIROCREW_BOUND_HOST"):
        monkeypatch.delenv(var, raising=False)
    for var, value in bound.items():
        monkeypatch.setenv(var, value)
    _install_plain(tmp_path, _APP)
    write_app_secret(_APP, "per-app-secret")
    seen = _spy_spawn(bmod, monkeypatch, route=False)
    captured: dict[str, dict[str, str]] = {}

    def _popen(*_a: Any, **kw: Any) -> None:
        captured["env"] = dict(kw.get("env") or {})
        raise OSError("stop before the real spawn")

    monkeypatch.setattr(bmod.subprocess, "Popen", _popen)
    bmod.start_app_backend(_APP)
    child, masked = captured["env"], str(sel_hmac_key_path()) in seen["hidden"]
    assert "KIROCREW_GATEWAY_ORIGIN" not in child  # the configs under test

    # Become the child: its env, and a log that fails to build when the key is masked.
    for k in ("KIROCREW_SPAWNED", "KIROCREW_APP_NAME", "KIROCREW_PROXY_SECRET"):
        if k in child:
            monkeypatch.setenv(k, child[k])
    monkeypatch.delenv("KIROCREW_GATEWAY_ORIGIN", raising=False)
    _Writer.rows = []
    monkeypatch.setattr("kiro_crew.sel.SecurityEventLog", _KeyMasked if masked else _Writer)

    AuditSDK(_APP).record("publish", "ok")

    assert [r["operation"] for r in _Writer.rows] == [f"{_APP}.publish"], (
        "audit row lost: key masked=%s, no gateway origin" % masked
    )
