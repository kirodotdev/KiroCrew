"""Which Region's built-in lease service a Crew sends its lease credential to.

With ``tunnel.relay_lease_url`` empty, a Crew that does not lease on its Kiro
sign-in (a team Crew's crew API key, a JWT-SVID lease) has no sign-in profile to
name a Region. A crew key and a JWT-SVID lease
(``RelaySettings.uses_workload_identity``) alike lease from
``tunnel.relay_lease_region``, never from the workload profile ARN's Region; with
no setting from us-east-1, as before; a Region it names that the built-in table
lacks posts nothing at all. A recording
session stands in for the network, so every test asserts the URL each call would
have gone to.
"""

from __future__ import annotations

import json
import logging
from pathlib import Path
from types import SimpleNamespace
from typing import Any, List, Optional, Tuple

import aiohttp
import pytest

from kiro_crew import workload_identity as wi
from kiro_crew.relay_client import lease as lease_module
from kiro_crew.relay_client.lease import LeaseHostTokenSource
from kiro_crew.relay_client.provider import (
    RelaySettings,
    RelaySettingsError,
    RelayTunnelProvider,
    lease_token_source,
)
from kiro_crew.relay_client.token_source import HostTokenRetryLater, HostTokenUnavailable

pytestmark = pytest.mark.asyncio

US = "https://management.us-east-1.kiro.dev/"
FRANKFURT = "https://management.eu-central-1.kiro.dev/"
KEY = "/run/stand-in/crew-key"
SVID = "/run/stand-in/svid"
ARN = "arn:aws:codewhisperer:{}:000000000000:profile/STANDINPROFILE"
REGION_SETTING = "tunnel.relay_lease_region"


class _Answer:
    """One recorded answer: a status and a JSON body, read the way ``_post`` reads it."""

    def __init__(self, status: int, body: dict) -> None:
        self.status, self.headers = status, {}
        raw = json.dumps(body).encode("utf-8")

        async def chunks(size: int) -> Any:
            yield raw

        self.content = SimpleNamespace(iter_chunked=chunks)

    async def __aenter__(self) -> "_Answer":
        return self

    async def __aexit__(self, *_: Any) -> None:
        return None


class _Recorder:
    """A client session that records each POST; unreachable unless ``answer`` is set."""

    closed = False

    def __init__(self) -> None:
        self.calls: List[Tuple[str, str, dict, dict]] = []
        self.answer: Optional[Tuple[int, dict]] = None

    def post(self, url: str, *, data: bytes, headers: dict, **_: Any) -> Any:
        target = headers["X-Amz-Target"].rsplit(".", 1)[-1]
        self.calls.append((url, target, dict(headers), json.loads(data)))
        if self.answer is None:
            raise aiohttp.ClientConnectionError("recorded, not sent")
        return _Answer(*self.answer)

    async def close(self) -> None:
        pass


@pytest.fixture
def recorder(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> _Recorder:
    session = _Recorder()
    monkeypatch.setattr(LeaseHostTokenSource, "_client_session", lambda self: session)
    monkeypatch.setattr(lease_module, "_data_home", lambda: tmp_path)
    monkeypatch.setattr(
        lease_module, "read_lease_credential", lambda ref, **_: "stand-in-credential"
    )

    def svid_unread(path: str, env: Any = None) -> str:
        # A JWT-SVID lease reads through lease_bearer: its variable checks stay, and
        # only the file read is stood in for.
        wi.Settings.from_env(env)
        return "stand-in-svid"

    monkeypatch.setattr(wi, "lease_bearer", svid_unread)
    return session


@pytest.fixture(autouse=True)
def _no_workload_identity(_floor_monkeypatch: pytest.MonkeyPatch) -> None:
    for name in (wi.ENV_MODE, wi.ENV_SVID_FILE, wi.ENV_AUDIENCE, wi.ENV_PROFILE_ARN):
        _floor_monkeypatch.delenv(name, raising=False)


def _workload(monkeypatch: pytest.MonkeyPatch, arn: str) -> None:
    monkeypatch.setenv(wi.ENV_MODE, "spiffe")
    monkeypatch.setenv(wi.ENV_SVID_FILE, SVID)
    monkeypatch.setenv(wi.ENV_AUDIENCE, "https://runtime.example.test")
    monkeypatch.setenv(wi.ENV_PROFILE_ARN, arn)


def _settings(**tunnel: Any) -> RelaySettings:
    values = {
        "relay_enabled": True,
        "relay_lease_url": "",
        "relay_lease_credential": KEY,
        "relay_lease_token_type": "API_KEY",
    }
    values.update({f"relay_{k}": v for k, v in tunnel.items()})
    return RelaySettings.from_config(SimpleNamespace(tunnel=SimpleNamespace(**values)))


def _workload_settings(**tunnel: Any) -> RelaySettings:
    return _settings(lease_credential=SVID, lease_token_type="", **tunnel)


async def _renew_then_release(settings: RelaySettings) -> None:
    source = lease_token_source(settings)
    assert source is not None
    try:
        with pytest.raises(HostTokenRetryLater):
            await source.current()
    finally:
        await source.aclose()


def _urls(recorder: _Recorder) -> List[Tuple[str, str]]:
    return [(url, target) for url, target, _, _ in recorder.calls]


def _both(url: str) -> List[Tuple[str, str]]:
    return [(url, "RenewCrewLease"), (url, "ReleaseCrewInstanceLease")]


async def test_a_frankfurt_team_key_crew_leases_and_releases_in_frankfurt(
    recorder: _Recorder,
) -> None:
    settings = _settings(lease_region="eu-central-1")
    settings.validate()
    await _renew_then_release(settings)
    assert _urls(recorder) == _both(FRANKFURT)


async def test_a_team_key_crew_with_no_region_still_leases_and_releases_in_us_east_1(
    recorder: _Recorder,
) -> None:
    # Set up before the setting existed: it keeps the built-in default.
    settings = _settings()
    settings.validate()
    assert settings.lease_region_problem() is None
    await _renew_then_release(settings)
    assert _urls(recorder) == _both(US)


async def test_a_us_east_1_team_key_crew_with_its_region_leases_in_us_east_1(
    recorder: _Recorder,
) -> None:
    await _renew_then_release(_settings(lease_region="us-east-1"))
    assert _urls(recorder) == _both(US)


async def test_an_eu_west_1_region_leases_from_the_frankfurt_control_plane(
    recorder: _Recorder,
) -> None:
    await _renew_then_release(_settings(lease_region="eu-west-1"))
    assert _urls(recorder) == _both(FRANKFURT)


async def test_a_padded_region_is_read_without_its_whitespace(recorder: _Recorder) -> None:
    await _renew_then_release(_settings(lease_region="  eu-central-1 "))
    assert _urls(recorder) == _both(FRANKFURT)


@pytest.mark.parametrize(
    ("arn_region", "tunnel", "url"),
    [
        ("eu-central-1", {}, US),
        ("eu-central-1", {"lease_region": "eu-central-1"}, FRANKFURT),
        ("eu-central-1", {"lease_region": "us-east-1"}, US),
        ("us-east-1", {"lease_region": "eu-central-1"}, FRANKFURT),
    ],
    ids=["no-setting", "setting-like-the-profile", "setting-unlike-the-profile", "us-profile"],
)
async def test_a_jwt_svid_crew_leases_from_its_setting_not_its_profile_region(
    recorder: _Recorder,
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
    arn_region: str,
    tunnel: dict,
    url: str,
) -> None:
    # An existing eu-ARN host with no setting keeps leasing from us-east-1, and a
    # setting unlike the ARN is not refused: the ARN never picks the Region.
    _workload(monkeypatch, ARN.format(arn_region))
    settings = _workload_settings(**tunnel)
    assert settings.uses_workload_identity
    with caplog.at_level(logging.WARNING, logger="kiro_crew.relay_client.provider"):
        settings.validate()
    assert settings.lease_region_problem() is None
    assert not any("workload profile" in r.getMessage() for r in caplog.records)
    await _renew_then_release(settings)
    assert _urls(recorder) == _both(url)
    for _, _, headers, body in recorder.calls:
        assert "TokenType" not in headers and "profileArn" not in body


async def test_a_jwt_svid_lease_with_no_lease_url_leases_from_the_built_in_service(
    recorder: _Recorder, monkeypatch: pytest.MonkeyPatch
) -> None:
    # lease_token_source hands the workload source its built-in URL; without it
    # every call would stop with NO_LEASE_URL.
    _workload(monkeypatch, ARN.format("us-east-1"))
    settings = _workload_settings()
    assert settings.uses_workload_identity
    settings.validate()
    source = lease_token_source(settings)
    assert source is not None
    try:
        with pytest.raises(HostTokenRetryLater) as held:
            await source.current()
    finally:
        await source.aclose()
    assert lease_module.NO_LEASE_URL not in str(held.value)
    assert _urls(recorder) == _both(US)


@pytest.mark.parametrize(
    ("tunnel", "url"),
    [({}, US), ({"lease_region": "us-east-1"}, US)],
    ids=["no-setting", "setting-unlike-the-profile"],
)
async def test_a_crew_key_on_a_workload_host_leases_from_its_setting_not_the_profile(
    recorder: _Recorder, monkeypatch: pytest.MonkeyPatch, tunnel: dict, url: str
) -> None:
    # The profile ARN is the host's JWT-SVID identity; a crew key is not that lease
    # and sends its own token type, its Region is the setting's, and a setting unlike
    # the ARN is not refused.
    _workload(monkeypatch, ARN.format("eu-central-1"))
    settings = _settings(**tunnel)
    assert not settings.uses_workload_identity
    settings.validate()
    assert settings.lease_region_problem() is None
    await _renew_then_release(settings)
    assert _urls(recorder) == _both(url)
    for _, _, headers, _ in recorder.calls:
        assert headers["TokenType"] == "API_KEY"


async def test_a_set_lease_url_wins_over_the_region_and_the_profile(
    recorder: _Recorder, monkeypatch: pytest.MonkeyPatch
) -> None:
    _workload(monkeypatch, ARN.format("eu-central-1"))
    await _renew_then_release(
        _settings(lease_url="https://lease.example.test", lease_region="us-east-1")
    )
    assert _urls(recorder) == _both("https://lease.example.test/")


async def test_a_release_alone_goes_to_the_crews_region_host(recorder: _Recorder) -> None:
    source = lease_token_source(_settings(lease_region="eu-central-1"))
    assert isinstance(source, LeaseHostTokenSource)
    with pytest.raises(HostTokenRetryLater):
        await source.current()
    recorder.calls.clear()
    await source.aclose()
    assert _urls(recorder) == [(FRANKFURT, "ReleaseCrewInstanceLease")]


@pytest.mark.parametrize(
    ("tunnel", "names"),
    [
        ({"lease_region": "xx-test-9"}, REGION_SETTING),
        ({"lease_region": "EU-CENTRAL-1"}, REGION_SETTING),
    ],
    ids=["unknown-region", "upper-case"],
)
async def test_a_team_key_crew_with_an_unusable_region_refuses_and_posts_nothing(
    recorder: _Recorder, tmp_path: Path, tunnel: dict, names: str
) -> None:
    settings = _settings(**tunnel)
    with pytest.raises(RelaySettingsError, match=names) as refused:
        settings.validate()
    # Not echoed: a credential pasted into the setting stays out of logs.
    assert tunnel["lease_region"] not in str(refused.value)
    assert settings.lease_region_problem() == str(refused.value)
    source = lease_token_source(settings)
    assert source is not None
    with pytest.raises(HostTokenUnavailable, match="relay_lease_region"):
        await source.current()
    await source.aclose()
    assert recorder.calls == []
    # Refused before the instance ID: no lease was ever held, so none is released.
    assert not (tmp_path / lease_module.INSTANCE_ID_FILE).exists()


async def test_a_jwt_svid_crew_with_an_unusable_region_refuses_and_posts_nothing(
    recorder: _Recorder, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    # A usable profile ARN does not stand in for an unusable setting.
    _workload(monkeypatch, ARN.format("eu-central-1"))
    settings = _workload_settings(lease_region="xx-test-9")
    with pytest.raises(RelaySettingsError, match=REGION_SETTING) as refused:
        settings.validate()
    assert wi.ENV_PROFILE_ARN not in str(refused.value)
    source = lease_token_source(settings)
    assert source is not None
    with pytest.raises(HostTokenUnavailable):
        await source.current()
    await source.aclose()
    assert recorder.calls == []
    assert not (tmp_path / lease_module.INSTANCE_ID_FILE).exists()


@pytest.mark.parametrize(
    ("arn", "region", "url"),
    [
        (ARN.format("xx-test-9"), "", US),
        (ARN.format("xx-test-9"), "eu-central-1", FRANKFURT),
        (ARN.format("eu-central-1").replace("STANDINPROFILE", "P" * 65), "", US),
        # A malformed ARN: the JWT-SVID read (lease_bearer) refuses it, so the lease
        # backs off and posts nothing, whatever the Region.
        ("not-a-profile-arn", "", None),
        ("not-a-profile-arn", "eu-central-1", None),
    ],
    ids=[
        "unknown-region",
        "unknown-region-with-setting",
        "long-profile-id",
        "bad-arn",
        "bad-arn-with-setting",
    ],
)
async def test_a_jwt_svid_crew_whose_profile_names_no_usable_region_leases_from_its_setting(
    recorder: _Recorder,
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
    arn: str,
    region: str,
    url: Optional[str],
) -> None:
    # The ARN is not read for the Region, so validation neither refuses nor warns.
    _workload(monkeypatch, arn)
    settings = _workload_settings(lease_region=region)
    with caplog.at_level(logging.WARNING, logger="kiro_crew.relay_client.provider"):
        settings.validate()
    assert settings.lease_region_problem() is None
    assert not any("workload profile" in r.getMessage() for r in caplog.records)
    if url is not None:
        await _renew_then_release(settings)
        assert _urls(recorder) == _both(url)
        return
    source = lease_token_source(settings)
    assert source is not None
    try:
        with pytest.raises(HostTokenRetryLater, match=wi.ENV_PROFILE_ARN):
            await source.current()
    finally:
        await source.aclose()
    assert recorder.calls == []


async def test_a_workload_crew_with_no_svid_file_setting_leases_from_us_east_1(
    recorder: _Recorder, monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    # With no SVID file variable the credential names no JWT-SVID file, so it is a
    # crew key (uses_workload_identity): nothing is logged about the profile ARN, and
    # the key leases from the setting's place, us-east-1.
    _workload(monkeypatch, ARN.format("eu-central-1"))
    monkeypatch.delenv(wi.ENV_SVID_FILE)
    settings = _workload_settings()
    assert not settings.uses_workload_identity
    with caplog.at_level(logging.WARNING, logger="kiro_crew.relay_client.provider"):
        settings.validate()
    assert not any("workload profile" in r.getMessage() for r in caplog.records)
    await _renew_then_release(settings)
    assert _urls(recorder) == _both(US)
    for _, _, headers, body in recorder.calls:
        assert "TokenType" not in headers and "profileArn" not in body


async def test_a_missing_credential_is_named_before_a_missing_region() -> None:
    with pytest.raises(RelaySettingsError, match="tunnel.relay_lease_credential is not set"):
        _settings(lease_credential="").validate()


async def test_the_lease_client_never_posts_to_a_built_in_url_without_a_region(
    recorder: _Recorder,
) -> None:
    source = LeaseHostTokenSource(lease_url="", credential=KEY, crew_version="0.0.0")
    with pytest.raises(HostTokenUnavailable, match="relay_lease_region"):
        await source.current()
    await source.aclose()
    assert recorder.calls == []


async def test_a_refused_start_logs_why_and_reports_it_in_the_status(
    recorder: _Recorder, caplog: pytest.LogCaptureFixture
) -> None:
    settings = _settings(lease_region="xx-test-9")
    provider = RelayTunnelProvider(settings, token_source=lease_token_source(settings))
    with caplog.at_level(logging.ERROR, logger="kiro_crew.relay_client.provider"):
        await provider.start()
    try:
        logged = [r.getMessage() for r in caplog.records]
        assert any(
            m.startswith("Remote Control relay not started: ") and REGION_SETTING in m
            for m in logged
        ), logged
        snapshot: Optional[dict] = provider.status_snapshot()
        assert snapshot is not None and REGION_SETTING in snapshot["error"]
    finally:
        await provider.stop()
    assert recorder.calls == []


_ABSENT = object()


@pytest.mark.parametrize(
    ("stored", "url"),
    [
        ("xx-test-9", None),
        ("EU-CENTRAL-1", None),
        (False, None),
        (0, None),
        ([], None),
        ({}, None),
        ("eu-central-1", FRANKFURT),
        (_ABSENT, US),
        # null and blank name no Region, so like absent they are the default, not a typo.
        (None, US),
        ("  ", US),
        ("\t", US),
    ],
    ids=[
        "typo",
        "upper-case",
        "false",
        "zero",
        "list",
        "object",
        "good",
        "absent",
        "null",
        "blank",
        "tab",
    ],
)
async def test_a_hand_edited_region_loads_as_written_and_a_bad_one_refuses(
    recorder: _Recorder, tmp_path: Path, monkeypatch: pytest.MonkeyPatch, stored: Any, url: Any
) -> None:
    # A value the person did not give (absent, null, empty or blank) leases from
    # us-east-1; any value they gave must be a known Region, or the relay refuses
    # and sends nothing.
    from kiro_crew.config.loader import KiroCrewConfig

    config_file = tmp_path / "config.json"
    tunnel: dict = {
        "relay_enabled": True,
        "relay_lease_credential": KEY,
        "relay_lease_token_type": "API_KEY",
    }
    if stored is not _ABSENT:
        tunnel["relay_lease_region"] = stored
    config_file.write_text(json.dumps({"tunnel": tunnel}), encoding="utf-8")
    monkeypatch.setattr("kiro_crew.config.loader.config_path", lambda: config_file)
    monkeypatch.setattr("kiro_crew.config.loader.config_local_path", lambda: tmp_path / "l.json")
    cfg = KiroCrewConfig.load()
    assert cfg.tunnel.relay_lease_region == ("" if stored in (_ABSENT, None) else str(stored))
    settings = RelaySettings.from_config(cfg)
    if url is None:
        with pytest.raises(RelaySettingsError, match=REGION_SETTING) as refused:
            settings.validate()
        assert f"{stored!s}," not in str(refused.value)
        provider = RelayTunnelProvider(settings, token_source=lease_token_source(settings))
        await provider.start()
        await provider.stop()
        assert recorder.calls == []
    else:
        settings.validate()
        await _renew_then_release(settings)
        assert _urls(recorder) == _both(url)


@pytest.mark.parametrize(
    ("tunnel", "hinted"),
    [({}, True), ({"lease_region": "eu-central-1"}, False)],
    ids=["default-region", "named-region"],
)
async def test_a_refusal_on_the_default_region_hints_at_the_setting(
    recorder: _Recorder, tunnel: dict, hinted: bool
) -> None:
    # A hint only: the service's own refusal comes first, unchanged.
    recorder.answer = (403, {"__type": "AccessDeniedException", "message": "stand-in refusal"})
    source = lease_token_source(_settings(**tunnel))
    assert source is not None
    try:
        with pytest.raises(HostTokenRetryLater) as held:
            await source.current()
    finally:
        await source.aclose()
    assert str(held.value).startswith("the lease service refused this Crew (")
    assert (lease_module.REGION_HINT in str(held.value)) is hinted


async def test_the_region_choices_are_the_built_in_table() -> None:
    import dataclasses

    from kiro_crew.auth.login.control_plane import CONTROL_PLANE_REGIONS
    from kiro_crew.config.integration_sections import TunnelConfig

    (region,) = [f for f in dataclasses.fields(TunnelConfig) if f.name == "relay_lease_region"]
    assert region.metadata["enum"] == ["", *CONTROL_PLANE_REGIONS]
