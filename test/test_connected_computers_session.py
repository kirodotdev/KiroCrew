"""Connected computers: starting a chat on a computer, and missing enforcement.

* B10 -- an offline computer refuses the start; the chat is not moved.
* B12 -- a removed or revoked computer refuses the start; nothing is rebound.
* flag -- a crafted start while the feature is off is refused before any
  registry or broker read.
"""

from __future__ import annotations

from types import SimpleNamespace
from typing import Any

import pytest

from kiro_crew.connected_computers import flag, provider
from kiro_crew.connected_computers.broker import DeviceOffline
from kiro_crew.connected_computers.devices import DeviceRegistry
from kiro_crew.connected_computers.identity import install_fingerprint
from kiro_crew.secrets.vault import SecretVault
from kiro_crew.session_environment import SessionEnvironmentUnavailable

pytestmark = pytest.mark.asyncio


def _fp(seed: int) -> str:
    return install_fingerprint(bytes([seed]) * 32)


class _NoLiveBroker:
    def __init__(self) -> None:
        self.asked: list[str] = []

    def live(self, device_id: str) -> Any:
        self.asked.append(device_id)
        return None


class _ExplodingRegistry:
    def get(self, device_id: str) -> Any:
        raise AssertionError("the registry must not be read while the feature is off")


@pytest.fixture(autouse=True)
def _isolated_home(tmp_path, _floor_monkeypatch):
    _floor_monkeypatch.setenv("KIROCREW_HOME", str(tmp_path / "data"))


@pytest.fixture
def feature_on(monkeypatch):
    monkeypatch.setattr(provider, "require_remote", lambda: None)


@pytest.fixture
def registry(tmp_path):
    return DeviceRegistry(SecretVault(tmp_path / "vault-home"))


async def test_b10_an_offline_computer_refuses_and_the_chat_stays_bound(feature_on, registry):
    rec = registry.enroll(base="laptop", fingerprint=_fp(1), label="Laptop", owner="me")
    broker = _NoLiveBroker()
    with pytest.raises(DeviceOffline, match="stays on that computer"):
        await provider.prepare_connected_session(
            "chat-1", rec.device_id, registry=registry, broker=broker  # type: ignore[arg-type]
        )
    # Only the bound computer was asked for; no other device was tried.
    assert broker.asked == [rec.device_id]


async def test_b12_a_removed_computer_refuses_and_is_not_rebound(feature_on, registry):
    rec = registry.enroll(base="laptop", fingerprint=_fp(1), label="Laptop", owner="me")
    registry.enroll(base="laptop", fingerprint=_fp(2), label="Laptop", owner="me")
    registry.remove(rec.device_id)
    broker = _NoLiveBroker()
    with pytest.raises(SessionEnvironmentUnavailable, match="files were not touched"):
        await provider.prepare_connected_session(
            "chat-1", rec.device_id, registry=registry, broker=broker  # type: ignore[arg-type]
        )
    assert broker.asked == []


async def test_b12_a_revoked_computer_refuses(feature_on, registry):
    rec = registry.enroll(base="laptop", fingerprint=_fp(1), label="Laptop", owner="me")
    registry.revoke(rec.device_id)
    with pytest.raises(SessionEnvironmentUnavailable, match="revoked"):
        await provider.prepare_connected_session(
            "chat-1", rec.device_id, registry=registry, broker=_NoLiveBroker()  # type: ignore[arg-type]
        )


async def test_an_unknown_device_is_refused_not_moved(feature_on, registry):
    with pytest.raises(SessionEnvironmentUnavailable, match="not moved elsewhere"):
        await provider.prepare_connected_session(
            "chat-1", "no-such-device", registry=registry, broker=_NoLiveBroker()  # type: ignore[arg-type]
        )


async def test_a_crafted_start_while_off_is_refused_before_any_read(monkeypatch):
    off = SimpleNamespace(dashboard=SimpleNamespace(feature_previews={}))
    monkeypatch.setattr(provider, "require_remote", lambda: flag.require_remote(off))
    with pytest.raises(flag.ConnectedComputersDisabled):
        await provider.prepare_connected_session(
            "chat-1",
            "laptop",
            registry=_ExplodingRegistry(),  # type: ignore[arg-type]
            broker=_NoLiveBroker(),  # type: ignore[arg-type]
        )
