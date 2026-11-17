"""A personal Crew's lease calls carry its profile ARN; every other Crew's are today's bytes."""

from __future__ import annotations

import json
import logging
from pathlib import Path
from types import SimpleNamespace
from typing import Any, Dict, Optional

import pytest
from aiohttp import web
from test_relay_client_lease import CREDENTIAL, FakeLeaseService, StepClock, answer, make_source

from kiro_crew import __version__
from kiro_crew.relay_client import lease as lease_module
from kiro_crew.relay_client.lease import RELEASE_TARGET, RENEW_TARGET, LeaseHostTokenSource
from kiro_crew.relay_client.provider import RelaySettings, lease_token_source

pytestmark = pytest.mark.asyncio

ARN = "arn:aws:codewhisperer:us-east-1:000000000000:profile/TESTPROFILE"


class RawLeaseService(FakeLeaseService):
    """The fake lease service, also keeping each request's raw body."""

    @staticmethod
    async def _recorded(request: web.Request) -> Dict[str, Any]:
        recorded = await FakeLeaseService._recorded(request)
        recorded["raw"] = await request.read()
        return recorded


def todays_renew(instance_id: str, name: str = "Work laptop", version: str = "0.9.1.0") -> dict:
    return {
        "crewInstanceId": instance_id,
        "name": name,
        "crewVersion": version,
        "protocolVersion": "1",
        "hostKind": "CREW",
        "relay": {"connectionStatus": "NOT_CONNECTED"},
        "connectedCount": 0,
    }


def raw(body: dict) -> bytes:
    return json.dumps(body).encode("utf-8")


async def one_renew_and_release(lease: RawLeaseService, source: LeaseHostTokenSource) -> tuple:
    lease.queue(200, answer(0))
    try:
        await source.current()
    finally:
        await source.aclose()
    (renew,), (release,) = lease.requests, lease.releases
    # VerifyCrewConfig, or any other operation, is never called.
    assert (renew["target"], release["target"]) == (RENEW_TARGET, RELEASE_TARGET)
    assert renew["auth"] == release["auth"] == f"Bearer {CREDENTIAL}"
    return renew, release


@pytest.mark.parametrize("token_type", [None, "API_KEY"])
async def test_a_crew_with_no_profile_reader_sends_todays_bytes(
    tmp_path: Path, token_type: Optional[str]
) -> None:
    async with RawLeaseService() as lease:
        source = make_source(lease, tmp_path, StepClock(), token_type=token_type)
        renew, release = await one_renew_and_release(lease, source)
    instance_id = renew["body"]["crewInstanceId"]
    assert renew["raw"] == raw(todays_renew(instance_id))
    assert release["raw"] == raw({"crewInstanceId": instance_id})
    assert renew["token_type"] == release["token_type"] == token_type


async def test_the_default_config_sends_todays_bytes(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(lease_module, "_data_home", lambda: tmp_path)
    async with RawLeaseService() as lease:
        tunnel = SimpleNamespace(
            relay_enabled=True,
            relay_lease_url=lease.url,
            relay_lease_credential=str(tmp_path / "credential"),
            relay_lease_token_type="",
        )
        (tmp_path / "credential").write_text(CREDENTIAL, encoding="ascii")
        source = lease_token_source(RelaySettings.from_config(SimpleNamespace(tunnel=tunnel)))
        assert isinstance(source, LeaseHostTokenSource)
        renew, release = await one_renew_and_release(lease, source)
    instance_id = renew["body"]["crewInstanceId"]
    name = lease_module.default_display_name()
    assert renew["raw"] == raw(todays_renew(instance_id, name, __version__))
    assert release["raw"] == raw({"crewInstanceId": instance_id})
    assert renew["token_type"] is None and release["token_type"] is None


@pytest.mark.parametrize("token_type", [None, "EXTERNAL_IDP"])
async def test_a_personal_crew_sends_its_profile_arn_on_renew_and_release(
    tmp_path: Path, caplog: pytest.LogCaptureFixture, token_type: Optional[str]
) -> None:
    caplog.set_level(logging.DEBUG)
    async with RawLeaseService() as lease:
        source = make_source(
            lease, tmp_path, StepClock(), token_type=token_type, profile_arn=lambda: ARN
        )
        renew, release = await one_renew_and_release(lease, source)
    instance_id = renew["body"]["crewInstanceId"]
    assert renew["raw"] == raw({**todays_renew(instance_id), "profileArn": ARN})
    assert release["raw"] == raw({"crewInstanceId": instance_id, "profileArn": ARN})
    # IAM Identity Center sends no TokenType and an outside identity provider sends
    # EXTERNAL_IDP, as today.
    assert renew["token_type"] == release["token_type"] == token_type
    assert ARN not in caplog.text and CREDENTIAL not in caplog.text


def _broken() -> Optional[str]:
    raise RuntimeError(ARN)


@pytest.mark.parametrize("reader", [lambda: None, lambda: "", _broken])
async def test_a_personal_crew_with_no_profile_arn_yet_sends_todays_bytes_and_says_so_once(
    tmp_path: Path, caplog: pytest.LogCaptureFixture, reader: Any
) -> None:
    caplog.set_level(logging.DEBUG, logger=lease_module.__name__)
    async with RawLeaseService() as lease:
        source = make_source(lease, tmp_path, StepClock(), profile_arn=reader)
        renew, release = await one_renew_and_release(lease, source)
    instance_id = renew["body"]["crewInstanceId"]
    assert renew["raw"] == raw(todays_renew(instance_id))
    assert release["raw"] == raw({"crewInstanceId": instance_id})
    said = [r for r in caplog.records if "no profile ARN yet" in r.getMessage()]
    assert [r.levelno for r in said] == [logging.INFO]
    assert ARN not in caplog.text


async def test_a_team_crew_on_a_crew_api_key_cannot_take_a_profile_arn(tmp_path: Path) -> None:
    with pytest.raises(ValueError, match="no profile ARN"):
        LeaseHostTokenSource(
            lease_url="https://lease.example.com",
            credential=str(tmp_path / "credential"),
            crew_version="0.9.1.0",
            token_type="API_KEY",
            profile_arn=lambda: ARN,
        )
