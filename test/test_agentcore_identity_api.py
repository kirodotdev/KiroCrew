"""This-crew AgentCore identity — GET/PUT /api/agentcore/identity.

Settings → Security on THIS gateway. App tokens cannot write the keystone.
A fleet override or signed document is refused, not rewritten.
"""

from __future__ import annotations

import asyncio
import json
from pathlib import Path
from typing import Any

from kiro_crew.dashboard.handlers import agentcore_identity as mod


class _Req:
    """Owner-shaped dashboard request unless *app* / *user* are overridden."""

    def __init__(
        self,
        body: Any = None,
        *,
        app: str | None = "",
        user: str = "local-app",
        owner: str = "",
    ) -> None:
        self._body = body
        self._store: dict[str, Any] = {"user": user}
        if app is not None:
            self._store["app"] = app
        self.app = {"state": type("S", (), {"owner_id": owner})()}

    def get(self, key: str, default: Any = None) -> Any:
        return self._store.get(key, default)

    def __contains__(self, key: object) -> bool:
        return key in self._store

    def __getitem__(self, key: str) -> Any:
        return self._store[key]

    async def json(self) -> Any:
        if isinstance(self._body, BaseException):
            raise self._body
        return self._body


def _isolate(monkeypatch, tmp_path: Path, *, env: dict[str, str] | None = None) -> Path:
    home = tmp_path / "security_policy.json"
    monkeypatch.setattr(mod, "_policy_home_path", lambda: home)
    monkeypatch.delenv("KIROCREW_SECURITY_POLICY", raising=False)
    monkeypatch.delenv("KIROCREW_AGENTCORE_WORKLOAD_NAME", raising=False)
    for key, value in (env or {}).items():
        monkeypatch.setenv(key, value)
    monkeypatch.setattr(mod, "current_context", lambda: type("C", (), {"governance": None})())
    monkeypatch.setattr(mod, "agentcore_posture", lambda _ceiling: None)
    monkeypatch.setattr(mod, "_audit", lambda *a, **k: None)
    monkeypatch.setattr(mod, "ensure_extra", lambda: "ok")
    monkeypatch.setattr(mod, "apply_agentcore_runtime", lambda: True)
    monkeypatch.setattr(mod, "_rebuild_agent_after_apply", lambda: None)
    monkeypatch.setattr(
        mod,
        "extra_snapshot",
        lambda last_code=None: {
            "extra_installed": last_code == "ok" or last_code is None,
            "extra_code": last_code if last_code is not None else "ok",
        },
    )
    return home


def test_get_unset_when_no_policy(tmp_path: Path, monkeypatch) -> None:
    _isolate(monkeypatch, tmp_path)
    resp = asyncio.run(mod.api_agentcore_identity_get(_Req()))
    assert resp.status == 200
    body = json.loads(resp.text)
    assert body["configured"] is False
    assert body["posture"] is None
    assert body["source"] == "unset"
    assert body["writable"] is True


def test_get_reads_home_file_not_running_ceiling(tmp_path: Path, monkeypatch) -> None:
    home = _isolate(monkeypatch, tmp_path)
    home.write_text(
        json.dumps(
            {
                "version": 1,
                "boot": {"require_sandbox": True, "allow_terminal": False, "fail_closed": True},
                "capabilities": {"agentcore": {"enabled": True, "posture": "workload"}},
            }
        ),
        encoding="utf-8",
    )
    resp = asyncio.run(mod.api_agentcore_identity_get(_Req()))
    body = json.loads(resp.text)
    assert body["configured"] is True
    assert body["posture"] == "workload"
    assert body["source"] == "policy"
    assert body["restart_required"] is True


def test_put_hot_applies_so_restart_is_not_required(tmp_path: Path, monkeypatch) -> None:
    home = _isolate(monkeypatch, tmp_path)
    resp = asyncio.run(
        mod.api_agentcore_identity_save(
            _Req({"posture": "workload", "workload_name": "kirocrew-e2e"})
        )
    )
    assert resp.status == 200
    body = json.loads(resp.text)
    assert body["posture"] == "workload"
    assert body["workload_name"] == "kirocrew-e2e"
    assert body["restart_required"] is False
    assert (
        json.loads(home.read_text(encoding="utf-8"))["capabilities"]["agentcore"]["workload_name"]
        == "kirocrew-e2e"
    )


def test_put_refuses_app_token(tmp_path: Path, monkeypatch) -> None:
    _isolate(monkeypatch, tmp_path)
    resp = asyncio.run(mod.api_agentcore_identity_save(_Req({"posture": "workload"}, app="board")))
    assert resp.status == 403
    body = json.loads(resp.text)
    assert body["code"] == "dashboard_owner_required"
    assert not (tmp_path / "security_policy.json").exists()


def test_put_none_without_file_is_noop(tmp_path: Path, monkeypatch) -> None:
    home = _isolate(monkeypatch, tmp_path)
    resp = asyncio.run(mod.api_agentcore_identity_save(_Req({"posture": "none"})))
    assert resp.status == 200
    assert not home.exists()


def test_put_writes_minimal_home_policy(tmp_path: Path, monkeypatch) -> None:
    home = _isolate(monkeypatch, tmp_path)
    resp = asyncio.run(
        mod.api_agentcore_identity_save(_Req({"posture": "login", "workload_name": "kirocrew-e2e"}))
    )
    assert resp.status == 200
    data = json.loads(home.read_text(encoding="utf-8"))
    assert data["version"] == 1
    assert data["boot"]["fail_closed"] is True
    assert data["capabilities"]["agentcore"] == {
        "enabled": True,
        "posture": "login",
        "workload_name": "kirocrew-e2e",
    }
    body = json.loads(resp.text)
    assert body["posture"] == "login"
    assert body["restart_required"] is False


def test_put_merges_without_wiping_other_capabilities(tmp_path: Path, monkeypatch) -> None:
    home = _isolate(monkeypatch, tmp_path)
    home.write_text(
        json.dumps(
            {
                "version": 1,
                "boot": {"require_sandbox": True, "allow_terminal": False, "fail_closed": True},
                "capabilities": {"cron": {"enabled": True}},
            }
        ),
        encoding="utf-8",
    )
    resp = asyncio.run(
        mod.api_agentcore_identity_save(
            _Req({"posture": "workload", "workload_name": "kirocrew-e2e"})
        )
    )
    assert resp.status == 200
    data = json.loads(home.read_text(encoding="utf-8"))
    assert data["capabilities"]["cron"] == {"enabled": True}
    assert data["capabilities"]["agentcore"] == {
        "enabled": True,
        "posture": "workload",
        "workload_name": "kirocrew-e2e",
    }


def test_put_refuses_fleet_override(tmp_path: Path, monkeypatch) -> None:
    _isolate(
        monkeypatch,
        tmp_path,
        env={"KIROCREW_SECURITY_POLICY": str(tmp_path / "fleet.json")},
    )
    resp = asyncio.run(mod.api_agentcore_identity_save(_Req({"posture": "workload"})))
    assert resp.status == 409
    body = json.loads(resp.text)
    assert body["code"] == "policy_not_writable"
    assert body["write_blocked"] == "fleet_override"


def test_put_refuses_signed_policy(tmp_path: Path, monkeypatch) -> None:
    home = _isolate(monkeypatch, tmp_path)
    home.write_text(
        json.dumps(
            {
                "version": 1,
                "boot": {"require_sandbox": True, "allow_terminal": False, "fail_closed": True},
                "identity": {"signature": "deadbeef"},
            }
        ),
        encoding="utf-8",
    )
    resp = asyncio.run(mod.api_agentcore_identity_save(_Req({"posture": "workload"})))
    assert resp.status == 409
    assert json.loads(resp.text)["write_blocked"] == "signed"
    data = json.loads(home.read_text(encoding="utf-8"))
    assert "agentcore" not in data.get("capabilities", {})


def test_put_rejects_bad_posture(tmp_path: Path, monkeypatch) -> None:
    _isolate(monkeypatch, tmp_path)
    resp = asyncio.run(mod.api_agentcore_identity_save(_Req({"posture": "yolo"})))
    assert resp.status == 400
    assert json.loads(resp.text)["code"] == "invalid_agentcore_posture"


def test_put_requires_workload_name_when_identity_is_on(tmp_path: Path, monkeypatch) -> None:
    _isolate(monkeypatch, tmp_path)
    resp = asyncio.run(mod.api_agentcore_identity_save(_Req({"posture": "workload"})))
    assert resp.status == 400
    assert json.loads(resp.text)["code"] == "workload_name_required"


def test_put_installs_extra_when_named(tmp_path: Path, monkeypatch) -> None:
    calls: list[str] = []

    def _record() -> str:
        calls.append("ensure")
        return "ok"

    home = _isolate(monkeypatch, tmp_path)
    monkeypatch.setattr(mod, "ensure_extra", _record)
    resp = asyncio.run(
        mod.api_agentcore_identity_save(
            _Req({"posture": "workload", "workload_name": "kirocrew-e2e"})
        )
    )
    assert resp.status == 200
    body = json.loads(resp.text)
    assert body["workload_name"] == "kirocrew-e2e"
    assert body["extra_code"] == "ok"
    assert body["extra_installed"] is True
    assert calls == ["ensure"]
    assert (
        json.loads(home.read_text(encoding="utf-8"))["capabilities"]["agentcore"]["posture"]
        == "workload"
    )


def test_put_none_does_not_install_extra(tmp_path: Path, monkeypatch) -> None:
    calls: list[str] = []
    _isolate(monkeypatch, tmp_path)
    monkeypatch.setattr(mod, "ensure_extra", lambda: calls.append("ensure") or "ok")
    resp = asyncio.run(mod.api_agentcore_identity_save(_Req({"posture": "none"})))
    assert resp.status == 200
    assert calls == []


def test_put_writes_workload_name(tmp_path: Path, monkeypatch) -> None:
    home = _isolate(monkeypatch, tmp_path)
    resp = asyncio.run(
        mod.api_agentcore_identity_save(
            _Req({"posture": "workload", "workload_name": "kirocrew-e2e"})
        )
    )
    assert resp.status == 200
    data = json.loads(home.read_text(encoding="utf-8"))
    assert data["capabilities"]["agentcore"]["workload_name"] == "kirocrew-e2e"
    assert json.loads(resp.text)["workload_name"] == "kirocrew-e2e"


def test_put_rejects_short_workload_name(tmp_path: Path, monkeypatch) -> None:
    _isolate(monkeypatch, tmp_path)
    resp = asyncio.run(
        mod.api_agentcore_identity_save(_Req({"posture": "workload", "workload_name": "ab"}))
    )
    assert resp.status == 400
    assert json.loads(resp.text)["code"] == "invalid_agentcore_workload_name"


def test_put_writes_gateway_url(tmp_path: Path, monkeypatch) -> None:
    home = _isolate(monkeypatch, tmp_path)
    resp = asyncio.run(
        mod.api_agentcore_identity_save(
            _Req(
                {
                    "posture": "workload",
                    "workload_name": "kirocrew-e2e",
                    "gateway_url": "https://gw.example.test/mcp",
                }
            )
        )
    )
    assert resp.status == 200
    data = json.loads(home.read_text(encoding="utf-8"))
    assert data["capabilities"]["agentcore"]["gateway_url"] == "https://gw.example.test/mcp"
    body = json.loads(resp.text)
    assert body["gateway_url"] == "https://gw.example.test/mcp"


def test_put_rejects_http_gateway_url(tmp_path: Path, monkeypatch) -> None:
    _isolate(monkeypatch, tmp_path)
    resp = asyncio.run(
        mod.api_agentcore_identity_save(
            _Req({"posture": "workload", "gateway_url": "http://insecure.example/mcp"})
        )
    )
    assert resp.status == 400
    assert json.loads(resp.text)["code"] == "invalid_agentcore_gateway_url"


def test_put_keeps_existing_gateway_url_when_omitted(tmp_path: Path, monkeypatch) -> None:
    home = _isolate(monkeypatch, tmp_path)
    home.write_text(
        json.dumps(
            {
                "version": 1,
                "boot": {"require_sandbox": True, "allow_terminal": False, "fail_closed": True},
                "capabilities": {
                    "agentcore": {
                        "enabled": True,
                        "posture": "workload",
                        "workload_name": "kirocrew-e2e",
                        "gateway_url": "https://gw.example.test/mcp",
                    }
                },
            }
        ),
        encoding="utf-8",
    )
    resp = asyncio.run(mod.api_agentcore_identity_save(_Req({"posture": "login"})))
    assert resp.status == 200
    data = json.loads(home.read_text(encoding="utf-8"))
    assert data["capabilities"]["agentcore"]["posture"] == "login"
    assert data["capabilities"]["agentcore"]["gateway_url"] == "https://gw.example.test/mcp"


def test_get_does_not_install_extra(tmp_path: Path, monkeypatch) -> None:
    calls: list[str] = []
    _isolate(monkeypatch, tmp_path)
    monkeypatch.setattr(mod, "ensure_extra", lambda: calls.append("ensure") or "ok")
    resp = asyncio.run(mod.api_agentcore_identity_get(_Req()))
    assert resp.status == 200
    assert calls == []
