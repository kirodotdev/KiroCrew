"""This-crew AgentCore identity — GET/PUT /api/agentcore/identity.

Settings → Security on THIS gateway. App tokens cannot write the keystone.
A fleet override, signed document, or required-signature
fleet is refused, not rewritten.
"""

from __future__ import annotations

import asyncio
import inspect
import json
import os
import subprocess
import sys
from pathlib import Path
from typing import Any
from unittest.mock import AsyncMock

import pytest

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
    monkeypatch.delenv("KIROCREW_POLICY_URL", raising=False)
    monkeypatch.delenv("KIROCREW_POLICY_CACHE_ONLY", raising=False)
    monkeypatch.delenv("KIROCREW_AGENTCORE_WORKLOAD_NAME", raising=False)
    monkeypatch.delenv("KIROCREW_AGENTCORE_GATEWAY_URL", raising=False)
    monkeypatch.delenv("KIROCREW_AGENTCORE_POSTURE", raising=False)
    for key, value in (env or {}).items():
        monkeypatch.setenv(key, value)
    monkeypatch.setattr(
        mod,
        "current_context",
        lambda: type("C", (), {"governance": None, "profile": "standalone"})(),
    )
    monkeypatch.setattr(mod, "agentcore_posture", lambda _ceiling: None)
    monkeypatch.setattr(mod, "_audit", lambda *a, **k: None)
    monkeypatch.setattr(mod, "ensure_extra", lambda: "ok")
    monkeypatch.setattr(mod, "apply_agentcore_runtime", lambda: True)
    monkeypatch.setattr(mod, "_rebuild_agent_after_apply", lambda: True)
    monkeypatch.setattr(mod, "_drop_live_identity", AsyncMock())
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


def test_get_uses_env_posture_when_no_ceiling(tmp_path: Path, monkeypatch) -> None:
    """A live env-attached identity must not render as Off."""
    _isolate(monkeypatch, tmp_path, env={"KIROCREW_AGENTCORE_POSTURE": "workload"})
    resp = asyncio.run(mod.api_agentcore_identity_get(_Req()))
    assert resp.status == 200
    body = json.loads(resp.text)
    assert body["configured"] is True
    assert body["posture"] == "workload"
    assert body["source"] == "env"
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


def test_get_policy_source_does_not_fill_omitted_fields_from_env(
    tmp_path: Path, monkeypatch
) -> None:
    """A policy row that omitted name/URL must not pick up stale launch env."""
    home = _isolate(
        monkeypatch,
        tmp_path,
        env={
            "KIROCREW_AGENTCORE_WORKLOAD_NAME": "stale-env-workload",
            "KIROCREW_AGENTCORE_GATEWAY_URL": (
                "https://stale.gateway.bedrock-agentcore.us-west-2.amazonaws.com/mcp"
            ),
        },
    )
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
    assert body["source"] == "policy"
    assert body["workload_name"] == ""
    assert body["gateway_url"] == ""


def test_get_env_source_still_shows_launch_env_fields(tmp_path: Path, monkeypatch) -> None:
    env_url = "https://demo-gw.gateway.bedrock-agentcore.us-west-2.amazonaws.com/mcp"
    _isolate(
        monkeypatch,
        tmp_path,
        env={
            "KIROCREW_AGENTCORE_POSTURE": "workload",
            "KIROCREW_AGENTCORE_WORKLOAD_NAME": "env-workload",
            "KIROCREW_AGENTCORE_GATEWAY_URL": env_url,
        },
    )
    resp = asyncio.run(mod.api_agentcore_identity_get(_Req()))
    body = json.loads(resp.text)
    assert body["source"] == "env"
    assert body["workload_name"] == "env-workload"
    assert body["gateway_url"] == env_url


def test_snapshot_reads_the_policy_row_once(tmp_path: Path, monkeypatch) -> None:
    """GET must not stitch posture from one generation onto name/URL from the next."""
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
    calls = {"n": 0}
    real = mod._file_row

    def _count() -> dict[str, Any] | None:
        calls["n"] += 1
        return real()

    monkeypatch.setattr(mod, "_file_row", _count)
    resp = asyncio.run(mod.api_agentcore_identity_get(_Req()))
    assert resp.status == 200
    assert calls["n"] == 1


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


def test_get_refuses_app_token(tmp_path: Path, monkeypatch) -> None:
    _isolate(monkeypatch, tmp_path)
    resp = asyncio.run(mod.api_agentcore_identity_get(_Req(app="board")))
    assert resp.status == 403
    assert json.loads(resp.text)["code"] == "dashboard_user_required"


def test_put_refuses_app_token(tmp_path: Path, monkeypatch) -> None:
    _isolate(monkeypatch, tmp_path)
    resp = asyncio.run(mod.api_agentcore_identity_save(_Req({"posture": "workload"}, app="board")))
    assert resp.status == 403
    body = json.loads(resp.text)
    assert body["code"] == "dashboard_user_required"
    assert not (tmp_path / "security_policy.json").exists()


def test_put_refuses_companion_ceiling(tmp_path: Path, monkeypatch) -> None:
    _isolate(monkeypatch, tmp_path)
    monkeypatch.setattr(
        mod,
        "current_context",
        lambda: type("C", (), {"governance": None, "profile": "enterprise"})(),
    )
    resp = asyncio.run(
        mod.api_agentcore_identity_save(
            _Req({"posture": "workload", "workload_name": "kirocrew-e2e"})
        )
    )
    assert resp.status == 409
    body = json.loads(resp.text)
    assert body["write_blocked"] == "companion"
    assert not (tmp_path / "security_policy.json").exists()


def test_put_none_without_file_persists_disabled_row(tmp_path: Path, monkeypatch) -> None:
    home = _isolate(monkeypatch, tmp_path)
    resp = asyncio.run(mod.api_agentcore_identity_save(_Req({"posture": "none"})))
    assert resp.status == 200
    data = json.loads(home.read_text(encoding="utf-8"))
    assert data["capabilities"]["agentcore"] == {"enabled": False}


def test_put_none_without_file_keeps_supplied_identity_fields(tmp_path: Path, monkeypatch) -> None:
    home = _isolate(monkeypatch, tmp_path)
    resp = asyncio.run(
        mod.api_agentcore_identity_save(
            _Req(
                {
                    "posture": "none",
                    "workload_name": "kirocrew-e2e",
                    "gateway_url": (
                        "https://demo-gw.gateway.bedrock-agentcore.us-west-2.amazonaws.com/mcp"
                    ),
                }
            )
        )
    )
    assert resp.status == 200
    data = json.loads(home.read_text(encoding="utf-8"))
    assert data["capabilities"]["agentcore"] == {
        "enabled": False,
        "workload_name": "kirocrew-e2e",
        "gateway_url": ("https://demo-gw.gateway.bedrock-agentcore.us-west-2.amazonaws.com/mcp"),
    }


def test_write_home_document_replaces_via_sensitive_tmp(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    home = _isolate(monkeypatch, tmp_path)
    replaced: list[tuple[str, str]] = []
    real = mod.replace_with_retry

    def _spy(src: str | os.PathLike[str], dst: str | os.PathLike[str]) -> None:
        replaced.append((Path(src).name, Path(dst).name))
        assert Path(src).name == "security_policy.json.tmp"
        assert Path(src).read_text(encoding="utf-8")
        real(src, dst)

    monkeypatch.setattr(mod, "replace_with_retry", _spy)
    resp = asyncio.run(mod.api_agentcore_identity_save(_Req({"posture": "none"})))
    assert resp.status == 200
    assert replaced == [("security_policy.json.tmp", "security_policy.json")]
    assert not home.with_name("security_policy.json.tmp").exists()


def test_write_home_document_keeps_live_file_when_stage_fails(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    home = _isolate(monkeypatch, tmp_path)
    original = (
        '{"version": 1, "boot": {"require_sandbox": true, '
        '"allow_terminal": false, "fail_closed": true}, '
        '"capabilities": {"cron": {"enabled": true}}}\n'
    )
    home.write_text(original, encoding="utf-8")

    def _write(_fd: int, _data: bytes) -> int:
        raise OSError("ENOSPC")

    monkeypatch.setattr(mod.os, "write", _write)
    resp = asyncio.run(mod.api_agentcore_identity_save(_Req({"posture": "none"})))
    assert resp.status == 500
    assert json.loads(resp.text)["code"] == "write_failed"
    assert home.read_text(encoding="utf-8") == original
    assert not home.with_name("security_policy.json.tmp").exists()


def test_write_home_document_refuses_preexisting_tmp_symlink(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    home = _isolate(monkeypatch, tmp_path)
    original = (
        '{"version": 1, "boot": {"require_sandbox": true, '
        '"allow_terminal": false, "fail_closed": true}, '
        '"capabilities": {"cron": {"enabled": true}}}\n'
    )
    home.write_text(original, encoding="utf-8")
    evil = tmp_path / "evil-policy"
    tmp = home.with_name("security_policy.json.tmp")
    tmp.symlink_to(evil)
    resp = asyncio.run(mod.api_agentcore_identity_save(_Req({"posture": "none"})))
    assert resp.status == 500
    assert json.loads(resp.text)["code"] == "write_failed"
    assert home.read_text(encoding="utf-8") == original
    assert tmp.is_symlink()
    assert not evil.exists()


def test_write_home_document_reclaims_abandoned_tmp_file(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    home = _isolate(monkeypatch, tmp_path)
    original = (
        '{"version": 1, "boot": {"require_sandbox": true, '
        '"allow_terminal": false, "fail_closed": true}, '
        '"capabilities": {"cron": {"enabled": true}}}\n'
    )
    home.write_text(original, encoding="utf-8")
    tmp = home.with_name("security_policy.json.tmp")
    tmp.write_text("abandoned previous save", encoding="utf-8")
    resp = asyncio.run(mod.api_agentcore_identity_save(_Req({"posture": "none"})))
    assert resp.status == 200
    assert not tmp.exists()
    data = json.loads(home.read_text(encoding="utf-8"))
    assert data["capabilities"]["agentcore"] == {"enabled": False}


def test_write_home_document_refuses_zero_byte_write(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    home = _isolate(monkeypatch, tmp_path)
    original = (
        '{"version": 1, "boot": {"require_sandbox": true, '
        '"allow_terminal": false, "fail_closed": true}, '
        '"capabilities": {"cron": {"enabled": true}}}\n'
    )
    home.write_text(original, encoding="utf-8")

    def _write(_fd: int, _data: bytes) -> int:
        return 0

    monkeypatch.setattr(mod.os, "write", _write)
    resp = asyncio.run(mod.api_agentcore_identity_save(_Req({"posture": "none"})))
    assert resp.status == 500
    assert json.loads(resp.text)["code"] == "write_failed"
    assert home.read_text(encoding="utf-8") == original
    assert not home.with_name("security_policy.json.tmp").exists()


def test_write_home_document_closes_fd_before_failed_unlink() -> None:
    """Windows cannot unlink an open handle; the except path must close first."""
    src = inspect.getsource(mod._write_home_document)
    except_at = src.index("except OSError:")
    close_at = src.find("os.close(fd)", except_at)
    unlink_at = src.find("tmp.unlink()", except_at)
    assert 0 <= close_at < unlink_at


def test_write_home_document_unlinks_tmp_when_replace_fails(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    home = _isolate(monkeypatch, tmp_path)
    original = (
        '{"version": 1, "boot": {"require_sandbox": true, '
        '"allow_terminal": false, "fail_closed": true}, '
        '"capabilities": {"cron": {"enabled": true}}}\n'
    )
    home.write_text(original, encoding="utf-8")

    def _boom(_src: str | os.PathLike[str], _dst: str | os.PathLike[str]) -> None:
        raise OSError("sharing violation")

    monkeypatch.setattr(mod, "replace_with_retry", _boom)
    resp = asyncio.run(mod.api_agentcore_identity_save(_Req({"posture": "none"})))
    assert resp.status == 500
    assert json.loads(resp.text)["code"] == "write_failed"
    assert home.read_text(encoding="utf-8") == original
    assert not home.with_name("security_policy.json.tmp").exists()


def test_write_home_document_restores_live_file_when_dest_lockdown_fails(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Post-replace lockdown failure must not leave the new policy installed."""
    home = _isolate(monkeypatch, tmp_path)
    original = (
        '{"version": 1, "boot": {"require_sandbox": true, '
        '"allow_terminal": false, "fail_closed": true}, '
        '"capabilities": {"cron": {"enabled": true}}}\n'
    )
    home.write_text(original, encoding="utf-8")

    def _restrict(path: Path | str) -> None:
        if Path(path).name == "security_policy.json":
            raise OSError("dest lockdown failed")

    monkeypatch.setattr(mod.platform_compat, "restrict_to_owner", _restrict)
    resp = asyncio.run(mod.api_agentcore_identity_save(_Req({"posture": "none"})))
    assert resp.status == 500
    assert json.loads(resp.text)["code"] == "write_failed"
    assert home.read_text(encoding="utf-8") == original
    assert not home.with_name("security_policy.json.tmp").exists()


def test_write_home_document_fsyncs_parent_after_replace(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    home = _isolate(monkeypatch, tmp_path)
    synced: list[Path] = []
    monkeypatch.setattr(mod, "_fsync_parent", lambda path: synced.append(path))
    resp = asyncio.run(mod.api_agentcore_identity_save(_Req({"posture": "none"})))
    assert resp.status == 200
    assert home in synced


def test_write_home_document_fsyncs_parent_after_first_write_rollback(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    home = _isolate(monkeypatch, tmp_path)
    synced: list[Path] = []

    def _restrict(path: Path | str) -> None:
        if Path(path).name == "security_policy.json":
            raise OSError("dest lockdown failed")

    monkeypatch.setattr(mod.platform_compat, "restrict_to_owner", _restrict)
    monkeypatch.setattr(mod, "_fsync_parent", lambda path: synced.append(path))
    resp = asyncio.run(mod.api_agentcore_identity_save(_Req({"posture": "none"})))
    assert resp.status == 500
    assert home in synced
    assert home.exists() is False


def test_write_home_document_retries_first_write_unlink_on_windows_share(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A sharing hold on first-write rollback must not leave the new policy."""
    home = _isolate(monkeypatch, tmp_path)
    monkeypatch.setattr(mod.platform_compat, "IS_WINDOWS", True)
    monkeypatch.setattr("kiro_crew.atomic_write._REPLACE_BACKOFF_SECONDS", 0)
    real_unlink = Path.unlink
    hits = {"n": 0}

    def _unlink(self: Path, *args: Any, **kwargs: Any) -> None:
        if self.name == "security_policy.json" and hits["n"] < 2:
            hits["n"] += 1
            raise PermissionError("sharing violation")
        return real_unlink(self, *args, **kwargs)

    def _restrict(path: Path | str) -> None:
        if Path(path).name == "security_policy.json":
            raise OSError("dest lockdown failed")

    monkeypatch.setattr(Path, "unlink", _unlink)
    monkeypatch.setattr(mod.platform_compat, "restrict_to_owner", _restrict)
    resp = asyncio.run(mod.api_agentcore_identity_save(_Req({"posture": "none"})))
    assert resp.status == 500
    assert json.loads(resp.text)["code"] == "write_failed"
    assert hits["n"] == 2
    assert home.exists() is False


def test_write_home_document_retries_in_place_restore_on_windows_share(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """In-place restore must retry and verify before write_failed returns."""
    home = _isolate(monkeypatch, tmp_path)
    original = (
        '{"version": 1, "boot": {"require_sandbox": true, '
        '"allow_terminal": false, "fail_closed": true}, '
        '"capabilities": {"cron": {"enabled": true}}}\n'
    )
    home.write_text(original, encoding="utf-8")
    replaces = {"n": 0}
    restores = {"n": 0}
    real_replace = mod.replace_with_retry
    real_restore = mod._restore_previous_in_place

    def _restrict(path: Path | str) -> None:
        if Path(path).name == "security_policy.json":
            raise OSError("dest lockdown failed")

    def _replace(src: Path, dest: Path) -> None:
        replaces["n"] += 1
        if replaces["n"] == 1:
            return real_replace(src, dest)
        raise OSError("restore replace failed")

    def _restore(path: Path, previous: bytes) -> None:
        restores["n"] += 1
        if restores["n"] == 1:
            raise OSError("in-place restore sharing violation")
        return real_restore(path, previous)

    monkeypatch.setattr(mod.platform_compat, "IS_WINDOWS", True)
    monkeypatch.setattr(mod, "_REPLACE_BACKOFF_SECONDS", 0)
    monkeypatch.setattr(mod.platform_compat, "restrict_to_owner", _restrict)
    monkeypatch.setattr(mod, "replace_with_retry", _replace)
    monkeypatch.setattr(mod, "_restore_previous_in_place", _restore)
    resp = asyncio.run(mod.api_agentcore_identity_save(_Req({"posture": "none"})))
    assert resp.status == 500
    assert json.loads(resp.text)["code"] == "write_failed"
    assert restores["n"] == 2
    assert home.read_text(encoding="utf-8") == original


def test_write_home_document_unlinks_new_file_when_dest_lockdown_fails(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    home = _isolate(monkeypatch, tmp_path)

    def _restrict(path: Path | str) -> None:
        if Path(path).name == "security_policy.json":
            raise OSError("dest lockdown failed")

    monkeypatch.setattr(mod.platform_compat, "restrict_to_owner", _restrict)
    resp = asyncio.run(mod.api_agentcore_identity_save(_Req({"posture": "none"})))
    assert resp.status == 500
    assert json.loads(resp.text)["code"] == "write_failed"
    assert home.exists() is False
    assert not home.with_name("security_policy.json.tmp").exists()


def test_write_home_document_restores_prior_when_rollback_replace_fails(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """tmp+replace restore failure must not leave the rejected policy installed."""
    home = _isolate(monkeypatch, tmp_path)
    original = (
        '{"version": 1, "boot": {"require_sandbox": true, '
        '"allow_terminal": false, "fail_closed": true}, '
        '"capabilities": {"cron": {"enabled": true}}}\n'
    )
    home.write_text(original, encoding="utf-8")
    replaces = {"n": 0}
    real_replace = mod.replace_with_retry

    def _restrict(path: Path | str) -> None:
        if Path(path).name == "security_policy.json":
            raise OSError("dest lockdown failed")

    def _replace(src: Path, dest: Path) -> None:
        replaces["n"] += 1
        if replaces["n"] == 1:
            return real_replace(src, dest)
        raise OSError("restore replace failed")

    monkeypatch.setattr(mod.platform_compat, "restrict_to_owner", _restrict)
    monkeypatch.setattr(mod, "replace_with_retry", _replace)
    resp = asyncio.run(mod.api_agentcore_identity_save(_Req({"posture": "none"})))
    assert resp.status == 500
    assert json.loads(resp.text)["code"] == "write_failed"
    assert home.exists() is True
    assert home.read_text(encoding="utf-8") == original
    assert not home.with_name("security_policy.json.tmp").exists()


def test_write_home_document_aborts_when_existing_policy_cannot_be_snapshotted(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A dest that cannot be snapshotted must not publish or be unlinked."""
    home = _isolate(monkeypatch, tmp_path)
    original = (
        '{"version": 1, "boot": {"require_sandbox": true, '
        '"allow_terminal": false, "fail_closed": true}, '
        '"capabilities": {"cron": {"enabled": true}}}\n'
    )
    home.write_text(original, encoding="utf-8")
    real_read = Path.read_bytes

    def _read(self: Path) -> bytes:
        if self == home:
            raise OSError("snapshot failed")
        return real_read(self)

    monkeypatch.setattr(Path, "read_bytes", _read)
    resp = asyncio.run(mod.api_agentcore_identity_save(_Req({"posture": "none"})))
    assert resp.status == 500
    assert json.loads(resp.text)["code"] == "write_failed"
    assert home.read_text(encoding="utf-8") == original
    assert not home.with_name("security_policy.json.tmp").exists()


def test_write_home_document_unlinks_tmp_when_lockdown_fails(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    home = _isolate(monkeypatch, tmp_path)
    original = (
        '{"version": 1, "boot": {"require_sandbox": true, '
        '"allow_terminal": false, "fail_closed": true}, '
        '"capabilities": {"cron": {"enabled": true}}}\n'
    )
    home.write_text(original, encoding="utf-8")

    def _boom(_path: Path | str) -> None:
        raise OSError("lockdown failed")

    monkeypatch.setattr(mod.platform_compat, "restrict_to_owner", _boom)
    resp = asyncio.run(mod.api_agentcore_identity_save(_Req({"posture": "none"})))
    assert resp.status == 500
    assert json.loads(resp.text)["code"] == "write_failed"
    assert home.read_text(encoding="utf-8") == original
    assert not home.with_name("security_policy.json.tmp").exists()


def test_write_home_document_retries_replace_and_unlinks_on_publish_fail() -> None:
    src = inspect.getsource(mod._write_home_document)
    restrict_at = src.index("restrict_to_owner(tmp)")
    write_at = src.index("os.write(fd, view)")
    fsync_at = src.index("os.fsync(fd)")
    replace_at = src.index("replace_with_retry(", fsync_at)
    except_at = src.index("except OSError:", replace_at)
    unlink_at = src.find("tmp.unlink()", except_at)
    assert restrict_at < write_at < fsync_at < replace_at < except_at < unlink_at


def test_write_home_document_lockdown_before_content(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _isolate(monkeypatch, tmp_path)
    order: list[str] = []
    real_restrict = mod.platform_compat.restrict_to_owner
    real_write = os.write

    def _restrict(path: Path | str) -> None:
        order.append("restrict")
        real_restrict(path)

    def _write(fd: int, data: bytes | memoryview) -> int:
        order.append("write")
        return real_write(fd, data)

    monkeypatch.setattr(mod.platform_compat, "restrict_to_owner", _restrict)
    monkeypatch.setattr(mod.os, "write", _write)
    resp = asyncio.run(mod.api_agentcore_identity_save(_Req({"posture": "none"})))
    assert resp.status == 200
    assert order.index("restrict") < order.index("write")


def test_write_home_document_fsyncs_before_close(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _isolate(monkeypatch, tmp_path)
    order: list[str] = []
    real_fsync = os.fsync
    real_close = os.close

    def _fsync(fd: int) -> None:
        order.append("fsync")
        real_fsync(fd)

    def _close(fd: int) -> None:
        order.append("close")
        real_close(fd)

    monkeypatch.setattr(mod.os, "fsync", _fsync)
    monkeypatch.setattr(mod.os, "close", _close)
    resp = asyncio.run(mod.api_agentcore_identity_save(_Req({"posture": "none"})))
    assert resp.status == 200
    assert "fsync" in order
    assert order.index("fsync") < order.index("close")


def test_write_home_document_retries_short_write(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    home = _isolate(monkeypatch, tmp_path)
    real = os.write
    calls = {"n": 0}

    def _write(fd: int, data: bytes | memoryview) -> int:
        calls["n"] += 1
        if calls["n"] == 1 and len(data) > 1:
            return real(fd, data[:1])
        return real(fd, data)

    monkeypatch.setattr(mod.os, "write", _write)
    resp = asyncio.run(mod.api_agentcore_identity_save(_Req({"posture": "none"})))
    assert resp.status == 200
    assert calls["n"] >= 2
    data = json.loads(home.read_text(encoding="utf-8"))
    assert data["capabilities"]["agentcore"] == {"enabled": False}


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


def test_put_refuses_when_policy_signature_is_required(tmp_path: Path, monkeypatch) -> None:
    applied: list[str] = []
    home = _isolate(monkeypatch, tmp_path)
    monkeypatch.setattr(mod, "_policy_signature_required", lambda: True)
    monkeypatch.setattr(mod, "apply_agentcore_runtime", lambda: applied.append("apply") or True)
    resp = asyncio.run(
        mod.api_agentcore_identity_save(
            _Req({"posture": "workload", "workload_name": "kirocrew-e2e"})
        )
    )
    assert resp.status == 409
    body = json.loads(resp.text)
    assert body["code"] == "policy_not_writable"
    assert body["write_blocked"] == "signature_required"
    assert applied == []
    assert not home.exists()
    get_resp = asyncio.run(mod.api_agentcore_identity_get(_Req()))
    assert json.loads(get_resp.text)["writable"] is False
    assert json.loads(get_resp.text)["write_blocked"] == "signature_required"


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


_GATEWAY_URL = "https://demo-gw.gateway.bedrock-agentcore.us-west-2.amazonaws.com/mcp"


def test_put_writes_gateway_url(tmp_path: Path, monkeypatch) -> None:
    home = _isolate(monkeypatch, tmp_path)
    resp = asyncio.run(
        mod.api_agentcore_identity_save(
            _Req(
                {
                    "posture": "workload",
                    "workload_name": "kirocrew-e2e",
                    "gateway_url": _GATEWAY_URL,
                }
            )
        )
    )
    assert resp.status == 200
    data = json.loads(home.read_text(encoding="utf-8"))
    assert data["capabilities"]["agentcore"]["gateway_url"] == _GATEWAY_URL
    body = json.loads(resp.text)
    assert body["gateway_url"] == _GATEWAY_URL


def test_put_rejects_non_gateway_https_url(tmp_path: Path, monkeypatch) -> None:
    """Login must not persist a host that would receive the user JWT."""
    _isolate(monkeypatch, tmp_path)
    resp = asyncio.run(
        mod.api_agentcore_identity_save(
            _Req(
                {
                    "posture": "login",
                    "workload_name": "kirocrew-e2e",
                    "gateway_url": "https://attacker.example/mcp",
                }
            )
        )
    )
    assert resp.status == 400
    assert json.loads(resp.text)["code"] == "invalid_agentcore_gateway_url"


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


def test_get_after_save_off_keeps_retained_gateway_url(tmp_path: Path, monkeypatch) -> None:
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
    off = asyncio.run(mod.api_agentcore_identity_save(_Req({"posture": "none"})))
    assert off.status == 200
    got = asyncio.run(mod.api_agentcore_identity_get(_Req()))
    assert got.status == 200
    body = json.loads(got.text)
    assert body["posture"] is None
    assert body["gateway_url"] == "https://gw.example.test/mcp"
    on = asyncio.run(
        mod.api_agentcore_identity_save(
            _Req({"posture": "workload", "workload_name": "kirocrew-e2e"})
        )
    )
    assert on.status == 200
    data = json.loads(home.read_text(encoding="utf-8"))
    assert data["capabilities"]["agentcore"]["gateway_url"] == "https://gw.example.test/mcp"


def test_get_does_not_install_extra(tmp_path: Path, monkeypatch) -> None:
    calls: list[str] = []
    _isolate(monkeypatch, tmp_path)
    monkeypatch.setattr(mod, "ensure_extra", lambda: calls.append("ensure") or "ok")
    resp = asyncio.run(mod.api_agentcore_identity_get(_Req()))
    assert resp.status == 200
    assert calls == []


def test_put_preserves_existing_agentcore_scopes(tmp_path: Path, monkeypatch) -> None:
    home = _isolate(monkeypatch, tmp_path)
    scopes = {"mcp": {"mode": "allow", "allow": ["@gw"]}}
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
                        "scopes": scopes,
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
    assert data["capabilities"]["agentcore"]["scopes"] == scopes
    assert data["capabilities"]["agentcore"]["workload_name"] == "kirocrew-e2e"


def test_put_resets_live_sessions_after_successful_apply(tmp_path: Path, monkeypatch) -> None:
    dropped: list[object] = []

    async def _record(request: object, **_kw: object) -> None:
        dropped.append(request)

    home = _isolate(monkeypatch, tmp_path)
    monkeypatch.setattr(mod, "_drop_live_identity", _record)
    req = _Req({"posture": "workload", "workload_name": "kirocrew-e2e"})
    resp = asyncio.run(mod.api_agentcore_identity_save(req))
    assert resp.status == 200
    assert dropped == [req]
    assert json.loads(home.read_text(encoding="utf-8"))["version"] == 1


def test_put_workload_revokes_and_returns_503_when_apply_fails(tmp_path: Path, monkeypatch) -> None:
    """Persist + reload failure must still drop the previous Gateway identity."""
    dropped: list[object] = []

    async def _record(request: object, **_kw: object) -> None:
        dropped.append(request)

    _isolate(monkeypatch, tmp_path)
    monkeypatch.setattr(mod, "apply_agentcore_runtime", lambda: False)
    monkeypatch.setattr(mod, "_drop_live_identity", _record)
    req = _Req({"posture": "workload", "workload_name": "kirocrew-e2e"})
    resp = asyncio.run(mod.api_agentcore_identity_save(req))
    assert resp.status == 503
    body = json.loads(resp.text)
    assert body["code"] == "runtime_apply_failed"
    assert body["restart_required"] is True
    assert dropped == [req]


def test_put_login_revokes_and_returns_503_when_apply_fails(tmp_path: Path, monkeypatch) -> None:
    """Workload→Login persist + reload failure must still drop IAM Gateway access."""
    dropped: list[object] = []

    async def _record(request: object, **_kw: object) -> None:
        dropped.append(request)

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
                        "gateway_url": (
                            "https://demo-gw.gateway.bedrock-agentcore.us-west-2.amazonaws.com/mcp"
                        ),
                    }
                },
            }
        ),
        encoding="utf-8",
    )
    monkeypatch.setattr(mod, "apply_agentcore_runtime", lambda: False)
    monkeypatch.setattr(mod, "_drop_live_identity", _record)
    req = _Req({"posture": "login"})
    resp = asyncio.run(mod.api_agentcore_identity_save(req))
    assert resp.status == 503
    body = json.loads(resp.text)
    assert body["code"] == "runtime_apply_failed"
    assert body["restart_required"] is True
    assert dropped == [req]


def test_put_off_revokes_and_returns_503_when_apply_fails(tmp_path: Path, monkeypatch) -> None:
    """Off persist + reload failure must still drop live Gateway sessions."""
    dropped: list[object] = []

    async def _record(request: object, **_kw: object) -> None:
        dropped.append(request)

    _isolate(monkeypatch, tmp_path)
    monkeypatch.setattr(mod, "apply_agentcore_runtime", lambda: False)
    monkeypatch.setattr(mod, "_drop_live_identity", _record)
    req = _Req({"posture": "none"})
    resp = asyncio.run(mod.api_agentcore_identity_save(req))
    assert resp.status == 503
    body = json.loads(resp.text)
    assert body["code"] == "runtime_apply_failed"
    assert body["restart_required"] is True
    assert dropped == [req]


def test_put_rebuild_failure_returns_503_and_still_revokes(tmp_path: Path, monkeypatch) -> None:
    dropped: list[object] = []

    async def _record(request: object, **_kw: object) -> None:
        dropped.append(request)

    home = _isolate(monkeypatch, tmp_path)
    monkeypatch.setattr(mod, "_rebuild_agent_after_apply", lambda: False)
    monkeypatch.setattr(mod, "_drop_live_identity", _record)
    req = _Req({"posture": "workload", "workload_name": "kirocrew-e2e"})
    resp = asyncio.run(mod.api_agentcore_identity_save(req))
    assert resp.status == 503
    body = json.loads(resp.text)
    assert body["code"] == "agent_rebuild_failed"
    assert body["restart_required"] is True
    assert dropped == [req]
    written = json.loads(home.read_text(encoding="utf-8"))
    assert written["capabilities"]["agentcore"]["posture"] == "workload"


def test_put_session_drop_failure_returns_503_after_persist(tmp_path: Path, monkeypatch) -> None:
    """A provider-factory reload error must not 500 after the home file wrote."""

    async def _boom(request: object, **_kw: object) -> None:
        raise RuntimeError("reload_provider_factory failed")

    home = _isolate(monkeypatch, tmp_path)
    monkeypatch.setattr(mod, "_drop_live_identity", _boom)
    resp = asyncio.run(
        mod.api_agentcore_identity_save(
            _Req({"posture": "workload", "workload_name": "kirocrew-e2e"})
        )
    )
    assert resp.status == 503
    body = json.loads(resp.text)
    assert body["code"] == "agent_rebuild_failed"
    assert body["restart_required"] is True
    written = json.loads(home.read_text(encoding="utf-8"))
    assert written["capabilities"]["agentcore"]["posture"] == "workload"


def test_rebuild_agent_after_apply_returns_false_on_error(monkeypatch) -> None:
    import kiro_crew.agent as agent_mod

    def _boom() -> None:
        raise RuntimeError("rebuild failed")

    monkeypatch.setattr(agent_mod, "rebuild_agent_config", _boom)
    assert mod._rebuild_agent_after_apply() is False


def test_rebuild_agent_after_apply_returns_true(monkeypatch) -> None:
    import kiro_crew.agent as agent_mod

    monkeypatch.setattr(agent_mod, "rebuild_agent_config", lambda: None)
    assert mod._rebuild_agent_after_apply() is True


def test_put_rejects_unsupported_policy_version(tmp_path: Path, monkeypatch) -> None:
    home = _isolate(monkeypatch, tmp_path)
    original = {
        "version": 2,
        "boot": {"require_sandbox": True, "allow_terminal": False, "fail_closed": True},
        "capabilities": {},
    }
    home.write_text(json.dumps(original), encoding="utf-8")
    resp = asyncio.run(
        mod.api_agentcore_identity_save(
            _Req({"posture": "workload", "workload_name": "kirocrew-e2e"})
        )
    )
    assert resp.status == 400
    assert json.loads(resp.text)["code"] == "invalid_policy"
    assert json.loads(home.read_text(encoding="utf-8")) == original


def test_put_rejects_malformed_capabilities(tmp_path: Path, monkeypatch) -> None:
    home = _isolate(monkeypatch, tmp_path)
    original = {
        "version": 1,
        "boot": {"require_sandbox": True, "allow_terminal": False, "fail_closed": True},
        "capabilities": ["not-a-dict"],
    }
    home.write_text(json.dumps(original), encoding="utf-8")
    resp = asyncio.run(
        mod.api_agentcore_identity_save(
            _Req({"posture": "workload", "workload_name": "kirocrew-e2e"})
        )
    )
    assert resp.status == 400
    assert json.loads(resp.text)["code"] == "invalid_policy"
    assert json.loads(home.read_text(encoding="utf-8")) == original


def test_put_workload_resets_old_proxy_before_rebuild(tmp_path: Path, monkeypatch) -> None:
    """Workload Save must not stop the proxy rebuild just started."""
    order: list[object] = []

    async def _proxy() -> None:
        order.append("proxy")

    def _rebuild() -> bool:
        order.append("rebuild")
        return True

    async def _drop(request: object, *, reset_proxy: bool = True) -> None:
        order.append(("sessions", reset_proxy))

    _isolate(monkeypatch, tmp_path)
    monkeypatch.setattr(mod, "_reset_workload_proxy", _proxy)
    monkeypatch.setattr(mod, "_rebuild_agent_after_apply", _rebuild)
    monkeypatch.setattr(mod, "_drop_live_identity", _drop)
    resp = asyncio.run(
        mod.api_agentcore_identity_save(
            _Req({"posture": "workload", "workload_name": "kirocrew-e2e"})
        )
    )
    assert resp.status == 200
    assert order == ["proxy", "rebuild", ("sessions", False)]


def test_put_off_resets_proxy_after_rebuild(tmp_path: Path, monkeypatch) -> None:
    order: list[object] = []

    async def _proxy() -> None:
        order.append("proxy")

    def _rebuild() -> bool:
        order.append("rebuild")
        return True

    async def _drop(request: object, *, reset_proxy: bool = True) -> None:
        order.append(("sessions", reset_proxy))

    _isolate(monkeypatch, tmp_path)
    monkeypatch.setattr(mod, "_reset_workload_proxy", _proxy)
    monkeypatch.setattr(mod, "_rebuild_agent_after_apply", _rebuild)
    monkeypatch.setattr(mod, "_drop_live_identity", _drop)
    resp = asyncio.run(mod.api_agentcore_identity_save(_Req({"posture": "none"})))
    assert resp.status == 200
    assert order == ["proxy", "rebuild", ("sessions", True)]


def test_drop_live_identity_resets_sessions_and_proxy(monkeypatch) -> None:
    reset: list[str] = []

    async def _reset(request: object, *, await_shutdown: bool = False) -> int:
        reset.append(("sessions", await_shutdown))
        return 0

    monkeypatch.setattr(
        "kiro_crew.dashboard.handlers.sessions._reset_all_sessions",
        _reset,
    )
    monkeypatch.setattr(
        "kiro_crew.platform.agentcore_sigv4.reset_workload_proxy",
        lambda: reset.append("proxy"),
    )
    asyncio.run(mod._drop_live_identity(_Req()))
    assert reset == [("sessions", True), "proxy"]


def test_drop_live_identity_resets_proxy_when_sessions_raise(monkeypatch) -> None:
    reset: list[str] = []

    async def _reset(request: object, *, await_shutdown: bool = False) -> int:
        reset.append(("sessions", await_shutdown))
        raise RuntimeError("session cleanup failed")

    monkeypatch.setattr(
        "kiro_crew.dashboard.handlers.sessions._reset_all_sessions",
        _reset,
    )
    monkeypatch.setattr(
        "kiro_crew.platform.agentcore_sigv4.reset_workload_proxy",
        lambda: reset.append("proxy"),
    )
    with pytest.raises(RuntimeError, match="session cleanup failed"):
        asyncio.run(mod._drop_live_identity(_Req()))
    assert reset == [("sessions", True), "proxy"]


def test_reset_all_sessions_await_shutdown_finishes_before_return() -> None:
    from kiro_crew.dashboard.handlers.sessions import _reset_all_sessions

    async def _body() -> None:
        order: list[str] = []
        hold = asyncio.Event()

        class _Prov:
            async def shutdown(self) -> None:
                await hold.wait()
                order.append("shutdown")

        class _Sessions:
            count = 1
            _pool_started = True
            _held: list[Any] = [_Prov()]

            async def reload_provider_factory(self) -> None:
                order.append("reload")
                for p in self._held:
                    await p.shutdown()

            async def drain_all_providers(self) -> list[Any]:
                order.append("drain")
                out = self._held
                self._held = []
                self.count = 0
                return out

            async def drain_warm_pool(self) -> list[Any]:
                return []

            async def start_pool(self, blocking: bool = False) -> None:
                order.append("pool")

        class _State:
            sessions = _Sessions()
            _background_tasks: set[Any] = set()

            def broadcast_ws(self, *_a: Any, **_k: Any) -> None:
                return None

            def push_refresh(self, *_a: Any, **_k: Any) -> None:
                return None

            def push_slots_update(self) -> None:
                return None

        class _ResetReq:
            app = {"state": _State()}

        async def _run() -> None:
            await _reset_all_sessions(_ResetReq(), await_shutdown=True)  # type: ignore[arg-type]
            order.append("return")

        task = asyncio.create_task(_run())
        await asyncio.sleep(0.05)
        assert "return" not in order
        hold.set()
        await task
        assert order == ["drain", "reload", "shutdown", "pool", "return"]

    asyncio.run(_body())


def test_put_refuses_distribution_url(tmp_path: Path, monkeypatch) -> None:
    _isolate(
        monkeypatch,
        tmp_path,
        env={"KIROCREW_POLICY_URL": "https://policy.example.test/p.json"},
    )
    resp = asyncio.run(mod.api_agentcore_identity_save(_Req({"posture": "workload"})))
    assert resp.status == 409
    body = json.loads(resp.text)
    assert body["code"] == "policy_not_writable"
    assert body["write_blocked"] == "distribution"


def test_put_refuses_cache_only_distribution(tmp_path: Path, monkeypatch) -> None:
    _isolate(monkeypatch, tmp_path, env={"KIROCREW_POLICY_CACHE_ONLY": "1"})
    resp = asyncio.run(mod.api_agentcore_identity_save(_Req({"posture": "workload"})))
    assert resp.status == 409
    body = json.loads(resp.text)
    assert body["code"] == "policy_not_writable"
    assert body["write_blocked"] == "distribution"


def test_persist_identity_write_locks_the_home_file(tmp_path: Path, monkeypatch) -> None:
    home = _isolate(monkeypatch, tmp_path)
    locked: list[tuple[bool, bool]] = []

    class _Lock:
        def __enter__(self) -> None:
            return None

        def __exit__(self, *exc: object) -> None:
            return None

    def _file_lock(_fd: int, *, exclusive: bool = False, required: bool = False) -> _Lock:
        locked.append((exclusive, required))
        return _Lock()

    monkeypatch.setattr(mod.platform_compat, "file_lock", _file_lock)
    opened: list[int] = []
    real_open = os.open

    def _open(path: object, flags: int, mode: int = 0o777, **kwargs: object) -> int:
        opened.append(flags)
        return real_open(path, flags, mode, **kwargs)

    monkeypatch.setattr(mod.os, "open", _open)
    assert mod._persist_identity_write("workload", gateway_url=None, workload_name="crew") is None
    assert locked == [(True, True)]
    assert opened
    assert opened[0] & os.O_CREAT
    if hasattr(os, "O_NOFOLLOW"):
        assert opened[0] & os.O_NOFOLLOW
    if hasattr(os, "O_BINARY"):
        assert all(flags & os.O_BINARY for flags in opened if flags & os.O_CREAT)
    written = json.loads(home.read_text(encoding="utf-8"))
    assert written["capabilities"]["agentcore"]["posture"] == "workload"


def test_persist_rejects_signed_document_under_lock(tmp_path: Path, monkeypatch) -> None:
    home = _isolate(monkeypatch, tmp_path)
    original = {
        "version": 1,
        "boot": {"require_sandbox": True, "allow_terminal": False, "fail_closed": True},
        "identity": {"signature": "deadbeef"},
    }
    home.write_text(json.dumps(original), encoding="utf-8")
    with pytest.raises(mod._PolicyNotWritable) as exc:
        mod._persist_identity_write("none", gateway_url=None, workload_name=None)
    assert exc.value.reason == "signed"
    assert json.loads(home.read_text(encoding="utf-8")) == original


def test_dashboard_handlers_import_does_not_load_identity(tmp_path: Path) -> None:
    """Gateway boot imports ``dashboard.handlers``; identity stays lazy."""
    import kiro_crew

    src = str(Path(kiro_crew.__file__).resolve().parents[1])
    env = dict(os.environ)
    env["PYTHONPATH"] = src + os.pathsep + env.get("PYTHONPATH", "")
    env.pop("COV_CORE_SOURCE", None)
    env.pop("COVERAGE_PROCESS_START", None)
    env["PYTHONDONTWRITEBYTECODE"] = "1"
    proc = subprocess.run(
        [
            sys.executable,
            "-c",
            "import json, sys\n"
            "import kiro_crew.dashboard.handlers  # noqa: F401\n"
            "print(json.dumps({\n"
            "  'identity': 'kiro_crew.dashboard.handlers.agentcore_identity' in sys.modules,\n"
            "  'consent': 'kiro_crew.dashboard.handlers.agentcore_consent' in sys.modules,\n"
            "  'inspect': 'kiro_crew.dashboard.handlers.agentcore_inspect' in sys.modules,\n"
            "}))\n",
        ],
        capture_output=True,
        text=True,
        encoding="utf-8",
        cwd=tmp_path,
        env=env,
        timeout=120,
    )
    assert proc.returncode == 0, proc.stderr
    payload = json.loads(proc.stdout.strip().splitlines()[-1])
    assert payload["identity"] is False
    assert payload["consent"] is False
    assert payload["inspect"] is False


def test_system_routes_lazy_load_identity() -> None:
    from kiro_crew.dashboard.routes import system as routes

    src = inspect.getsource(routes.register)
    assert "handlers.api_agentcore_identity_get" not in src
    assert "handlers.api_agentcore_consent_get" not in src
    assert "_lazy_agentcore" in src


def test_identity_save_serializes_persist_and_apply() -> None:
    """Overlapping PUTs must not stop the proxy the later write just started."""
    src = inspect.getsource(mod.api_agentcore_identity_save)
    lock_at = src.index("async with _get_config_lock():")
    persist_at = src.index("_persist_identity_write")
    apply_at = src.index("apply_agentcore_runtime")
    drop_at = src.index("_drop_live_identity")
    assert lock_at < persist_at < apply_at < drop_at


def test_identity_handlers_offload_owner_gate_and_audit() -> None:
    """SEL first-use mkdirs; owner + audit must not run on the loop."""
    gate = inspect.getsource(mod._owner_gate)
    audit = inspect.getsource(mod._audit_async)
    assert "asyncio.to_thread(_refuse_non_owner" in gate
    assert "asyncio.to_thread(" in audit
    for fn in (mod.api_agentcore_identity_get, mod.api_agentcore_identity_save):
        src = inspect.getsource(fn)
        assert "await _owner_gate(" in src
        assert "await _audit_async(" in src
        assert "_refuse_non_owner(" not in src
        assert "_audit(" not in src.replace("_audit_async(", "")
