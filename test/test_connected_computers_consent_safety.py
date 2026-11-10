"""Probe I/O, uncertain switches and consent-target identity regressions."""

from __future__ import annotations

import asyncio
import json
import os
import sys
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
from aiohttp.test_utils import make_mocked_request

from kiro_crew.connected_computers import confinement, desktop_host, disable, flag, local_routes
from kiro_crew.connected_computers.share import COORDINATE_FIELDS, connection_coordinates
from kiro_crew.testing.wait import FALLBACK_TIMEOUT_SECS


@pytest.mark.skipif(not sys.platform.startswith("linux"), reason="requires Linux no-follow opens")
@pytest.mark.parametrize("parent_link", [False, True])
def test_probe_writes_and_reads_never_follow_planted_links(tmp_path, parent_link, monkeypatch):
    root = tmp_path / "root"
    outside = tmp_path / "outside"
    root.mkdir()
    outside.mkdir()
    target = outside / "value-fixed"
    target.write_text("untouched", encoding="utf-8")
    probe_dir = root / "probe"
    if parent_link:
        probe_dir.symlink_to(outside, target_is_directory=True)
    else:
        probe_dir.mkdir()
        (probe_dir / "value-fixed").symlink_to(target)
    from kiro_crew.connected_computers import confinement_probe

    monkeypatch.setattr(confinement_probe, "secrets", SimpleNamespace(token_hex=lambda _n: "fixed"))
    assert confinement_probe.write(str(probe_dir / "value")) == "denied"
    assert confinement_probe.read(str(probe_dir / "value-fixed")) == "denied"
    assert target.read_text(encoding="utf-8") == "untouched"


@pytest.mark.skipif(
    not sys.platform.startswith("linux"), reason="requires Linux witness preparation"
)
def test_witness_preparation_refuses_a_link_planted_before_creation(tmp_path, monkeypatch):
    root = tmp_path / "root"
    root.mkdir()
    target = tmp_path / "outside"
    target.write_text("untouched", encoding="utf-8")
    real_open = os.open
    planted = []

    def plant(path, flags, mode=0o777, *, dir_fd=None):
        if flags & os.O_CREAT and dir_fd is not None:
            parent = os.readlink(f"/proc/self/fd/{dir_fd}")
            if parent.startswith(str(root) + os.sep):
                os.symlink(target, path, dir_fd=dir_fd)
                planted.append(path)
        return real_open(path, flags, mode, dir_fd=dir_fd)

    monkeypatch.setattr(os, "open", plant)
    with pytest.raises(FileExistsError):
        confinement._prepare(str(root), sys.platform)
    assert planted
    assert target.read_text(encoding="utf-8") == "untouched"
    assert list(root.iterdir()) == []


@pytest.mark.parametrize(
    "field,value",
    [
        ("degraded_sections", frozenset({"dashboard"})),
        ("degraded_sections", frozenset({"dashboard.feature_previews"})),
        ("_base_unreadable", True),
        ("_overlay_unreadable", True),
    ],
)
def test_degraded_config_is_unknown_for_revocation_and_off_for_new_work(monkeypatch, field, value):
    from kiro_crew.shared_crew import preview

    monkeypatch.setattr(preview, "preview_enabled", lambda: False)
    cfg = SimpleNamespace(dashboard=SimpleNamespace(feature_previews={flag.PREVIEW_NAME: True}))
    setattr(cfg, field, value)
    assert flag.local_switch(cfg) is None
    assert flag.local_enabled(cfg) is False
    assert flag.preview_on(cfg) is False


def test_shared_state_failure_is_unknown_for_revocation(monkeypatch):
    from kiro_crew.shared_crew import preview

    def unavailable():
        raise OSError("unreadable")

    monkeypatch.setattr(preview, "preview_enabled", unavailable)
    cfg = SimpleNamespace(dashboard=SimpleNamespace(feature_previews={flag.PREVIEW_NAME: True}))
    assert flag.local_switch(cfg) is None
    assert flag.local_enabled(cfg) is False


def test_config_watcher_does_not_revoke_from_degraded_defaults(monkeypatch):
    from kiro_crew.shared_crew import preview

    monkeypatch.setattr(preview, "preview_enabled", lambda: False)
    on = SimpleNamespace(dashboard=SimpleNamespace(feature_previews={flag.PREVIEW_NAME: True}))
    unknown = SimpleNamespace(dashboard=SimpleNamespace(feature_previews={}), _base_unreadable=True)
    tasks = set()
    monkeypatch.setattr(disable, "_TASKS", tasks)
    disable.apply(SimpleNamespace(old=on, new=unknown, touched=lambda *_args: True))
    assert tasks == set()


@pytest.mark.asyncio
async def test_switch_watcher_keeps_watching_unknown_without_revoking(monkeypatch):
    host = object.__new__(desktop_host.DesktopHost)
    host.controller = SimpleNamespace(off=AsyncMock())
    slept = asyncio.Event()
    release = asyncio.Event()
    answers = iter([None, False])
    host._switch_state = AsyncMock(side_effect=lambda: next(answers))

    async def wait(_seconds):
        slept.set()
        await asyncio.wait_for(release.wait(), FALLBACK_TIMEOUT_SECS)

    monkeypatch.setattr(desktop_host, "asyncio", SimpleNamespace(sleep=wait))
    watching = asyncio.create_task(host._watch_switch())
    try:
        await asyncio.wait_for(slept.wait(), FALLBACK_TIMEOUT_SECS)
        host.controller.off.assert_not_awaited()
        release.set()
        await asyncio.wait_for(watching, FALLBACK_TIMEOUT_SECS)
        host.controller.off.assert_awaited_once()
    finally:
        release.set()
        watching.cancel()
        await asyncio.wait_for(
            asyncio.gather(watching, return_exceptions=True), FALLBACK_TIMEOUT_SECS
        )


@pytest.mark.asyncio
@pytest.mark.parametrize("field", COORDINATE_FIELDS)
@pytest.mark.parametrize("surface", ["dashboard", "desktop"])
async def test_consent_rejects_changed_connection_before_enrollment(monkeypatch, field, surface):
    from kiro_crew.connected_computers import enroll, share

    instance = SimpleNamespace(**{name: "original" for name in COORDINATE_FIELDS})
    instance.id = "computer"
    snapshot = connection_coordinates(instance)
    controller = SimpleNamespace(store=object())
    enrollment = AsyncMock()
    monkeypatch.setattr(enroll, "enroll", enrollment)
    instance.__dict__[field] = "changed"
    monkeypatch.setattr(local_routes, "_consents", {})
    monkeypatch.setattr(local_routes, "local_enabled", lambda: True)
    if surface == "dashboard":
        from kiro_crew.connected_computers import controller as controller_module

        monkeypatch.setattr(controller_module, "get_controller", lambda: controller)
        monkeypatch.setattr(local_routes, "_instance", lambda _id: instance)
        monkeypatch.setattr(local_routes, "_native_refusal", lambda *_args: None)
        token = local_routes._mint_consent("computer", 0, snapshot)
        request = make_mocked_request("POST", local_routes.REDEEM_PATH)
        monkeypatch.setattr(
            request, "json", AsyncMock(return_value={"consent": token, "root": "/work", "mcp": []})
        )
        reply = await asyncio.wait_for(
            local_routes.api_local_consent_redeem(request), FALLBACK_TIMEOUT_SECS
        )
        answer = json.loads(reply.body)
        assert reply.status == 409
    else:
        host = object.__new__(desktop_host.DesktopHost)
        host.controller = controller
        host._enabled = lambda: True
        host._registry_lookup = lambda _id: None
        host.instances = {"computer": instance}
        host._consents = {"token": (instance.id, float("inf"), 0, snapshot)}
        answer = await asyncio.wait_for(
            host._op_share(
                1, {"instance_id": "computer", "consent": "token", "root": "/work", "mcp": []}
            ),
            FALLBACK_TIMEOUT_SECS,
        )
    assert answer["code"] == "connection_changed"
    enrollment.assert_not_awaited()
    with pytest.raises(share.ShareRefused):
        share.approved_connection(instance, snapshot)


@pytest.mark.parametrize("answer", [None, False, True])
def test_preview_readers_delegate_to_one_tristate_authority(monkeypatch, answer):
    from kiro_crew.config import live

    cfg = object()
    seen = []

    def read(passed=None):
        seen.append(passed)
        return answer

    monkeypatch.setattr(flag, "local_switch", read)
    monkeypatch.setattr(live, "snapshot", lambda: cfg)
    assert flag.preview_on(cfg) is (answer is True)
    assert flag.local_enabled(cfg) is (answer is True)
    assert flag.live_preview_on() is (answer is True)
    assert seen == [cfg, cfg, cfg]
