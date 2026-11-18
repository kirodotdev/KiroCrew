"""The workload identity settings and the KAS sign-in answer built from them.

The Workload API call itself is covered in ``test_spiffe_workload_api.py``; here
``fetch_jwt_svid`` is replaced by a recording fake, so no socket is opened.
"""

from __future__ import annotations

import logging
from typing import Any

import pytest

from kiro_crew import workload_identity as wi
from kiro_crew.spiffe_workload_api import JwtSvid, WorkloadApiError

AUDIENCE = "https://runtime.example.test"
SUBJECT = "spiffe://example.test/kiro-crew/platform"
PROFILE_ARN = "arn:aws:codewhisperer:us-west-2:111122223333:profile/ABCDEF123456"
TOKEN = "header.payload.stand-in-signature"
NOW = 1_900_000_000.0


def _env(**override: str) -> dict[str, str]:
    env = {
        wi.ENV_MODE: "spiffe",
        wi.ENV_AUDIENCE: AUDIENCE,
        wi.ENV_PROFILE_ARN: PROFILE_ARN,
        wi.SPIFFE_ENDPOINT_SOCKET: "unix:///run/agent.sock",
    }
    env.update(override)
    return env


def _svid(*, issued_at: float = NOW - 60, expires_at: float = NOW + 3600) -> JwtSvid:
    return JwtSvid(
        token=TOKEN,
        spiffe_id=SUBJECT,
        audience=(AUDIENCE,),
        issuer="https://issuer.example.test",
        expires_at=expires_at,
        issued_at=issued_at,
    )


class FakeFetch:
    """Stands in for ``fetch_jwt_svid``: records each call, answers or raises."""

    def __init__(self, answer: Any) -> None:
        self.answer = answer
        self.calls: list[tuple[str, str, str]] = []

    async def __call__(self, endpoint: str, audience: str, *, spiffe_id: str = "") -> JwtSvid:
        self.calls.append((endpoint, audience, spiffe_id))
        if isinstance(self.answer, Exception):
            raise self.answer
        return self.answer


@pytest.fixture(autouse=True)
def _fresh_cache() -> Any:
    wi._reset_cache_for_tests()
    yield
    wi._reset_cache_for_tests()


def _use(monkeypatch: pytest.MonkeyPatch, answer: Any) -> FakeFetch:
    fake = FakeFetch(answer)
    monkeypatch.setattr(wi, "fetch_jwt_svid", fake)
    return fake


def _set_env(monkeypatch: pytest.MonkeyPatch, env: dict[str, str]) -> None:
    for name, value in env.items():
        monkeypatch.setenv(name, value)


# ── settings ──


def test_settings_are_off_without_the_switch() -> None:
    assert wi.WorkloadIdentitySettings.from_env({}) is None
    assert not wi.enabled({})
    assert not wi.enabled({wi.ENV_MODE: "  "})
    assert wi.enabled(_env())


def test_settings_read_every_variable() -> None:
    settings = wi.WorkloadIdentitySettings.from_env(_env(**{wi.ENV_SPIFFE_ID: SUBJECT}))
    assert settings == wi.WorkloadIdentitySettings(
        audience=AUDIENCE,
        profile_arn=PROFILE_ARN,
        endpoint="unix:///run/agent.sock",
        spiffe_id=SUBJECT,
    )


def test_the_override_socket_wins_over_the_standard_variable() -> None:
    settings = wi.WorkloadIdentitySettings.from_env(_env(**{wi.ENV_SOCKET: "/x/agent.sock"}))
    assert settings is not None and settings.endpoint == "/x/agent.sock"


@pytest.mark.parametrize(
    ("override", "names"),
    [
        ({wi.ENV_MODE: "oidc"}, wi.ENV_MODE),
        ({wi.ENV_AUDIENCE: ""}, wi.ENV_AUDIENCE),
        ({wi.ENV_AUDIENCE: "http://plain.test"}, wi.ENV_AUDIENCE),
        ({wi.ENV_PROFILE_ARN: "arn:aws:iam::111122223333:role/x"}, wi.ENV_PROFILE_ARN),
        ({wi.ENV_SPIFFE_ID: "https://not-spiffe"}, wi.ENV_SPIFFE_ID),
        ({wi.SPIFFE_ENDPOINT_SOCKET: ""}, wi.SPIFFE_ENDPOINT_SOCKET),
    ],
)
def test_settings_refusals_name_the_variable(override: dict[str, str], names: str) -> None:
    with pytest.raises(wi.WorkloadIdentityConfigError, match=names):
        wi.WorkloadIdentitySettings.from_env(_env(**override))


# ── the cached SVID and the KAS answer ──


@pytest.mark.asyncio
async def test_the_source_caches_until_the_margin_and_never_logs_the_token(
    monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    settings = wi.WorkloadIdentitySettings.from_env(_env(**{wi.ENV_SPIFFE_ID: SUBJECT}))
    assert settings is not None
    fake = _use(monkeypatch, _svid())
    caplog.set_level(logging.DEBUG, logger="kiro_crew.workload_identity")
    first = await wi.current_svid(settings, now=NOW)
    again = await wi.current_svid(settings, now=NOW + 60)
    assert again is first and len(fake.calls) == 1
    assert fake.calls[0] == ("unix:///run/agent.sock", AUDIENCE, SUBJECT)
    # Past half its 3,660 s lifetime the cached SVID is replaced.
    await wi.current_svid(settings, now=NOW + 1900)
    assert len(fake.calls) == 2
    assert TOKEN not in caplog.text
    assert f"sub={SUBJECT}" in caplog.text


@pytest.mark.asyncio
async def test_an_agent_failure_is_unavailable_without_any_token(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    settings = wi.WorkloadIdentitySettings.from_env(_env())
    assert settings is not None
    _use(monkeypatch, WorkloadApiError("the Workload API did not answer in time"))
    with pytest.raises(wi.WorkloadIdentityUnavailable, match="did not answer"):
        await wi.current_svid(settings, now=NOW)


@pytest.mark.asyncio
async def test_the_kas_answer_carries_the_profile_and_no_auth_method(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    settings = wi.WorkloadIdentitySettings.from_env(_env())
    assert settings is not None
    _use(monkeypatch, _svid(issued_at=0, expires_at=4_000_000_000))
    answer = await wi.kas_access_token_answer(settings)
    assert answer["accessToken"] == TOKEN
    assert answer["profileArn"] == PROFILE_ARN
    assert answer["provider"] == wi.KAS_PROVIDER
    assert answer["expiresAt"].startswith("2096-")
    assert "authMethod" not in answer


# ── the KAS callback and the spawn owner ──


@pytest.mark.asyncio
async def test_the_kas_callback_answers_from_the_workload_identity(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from kiro_crew.acp import kas_host_auth

    _set_env(monkeypatch, _env())
    _use(monkeypatch, _svid(issued_at=0, expires_at=4_000_000_000))
    answer = await kas_host_auth.answer_get_access_token()
    assert answer["accessToken"] == TOKEN and answer["profileArn"] == PROFILE_ARN


@pytest.mark.asyncio
async def test_the_kas_callback_fails_closed_with_a_token_free_category(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from kiro_crew.acp import kas_host_auth

    _set_env(monkeypatch, _env())
    _use(monkeypatch, WorkloadApiError("the Workload API answered with no JWT-SVID"))
    with pytest.raises(kas_host_auth.HostAuthCallbackError, match="workload identity unavailable"):
        await kas_host_auth.answer_get_access_token()
    monkeypatch.setenv(wi.ENV_PROFILE_ARN, "nope")
    with pytest.raises(kas_host_auth.HostAuthCallbackError, match="misconfigured") as caught:
        await kas_host_auth.answer_get_access_token()
    assert TOKEN not in str(caught.value)


def test_the_switch_makes_the_spawn_crew_owned(monkeypatch: pytest.MonkeyPatch) -> None:
    from kiro_crew import lts_sign_in
    from kiro_crew.auth import bridge

    def _no_resolver(**_: Any) -> Any:
        raise AssertionError("the sign-in resolver must not be asked while the switch is on")

    monkeypatch.setattr(lts_sign_in, "select", _no_resolver)
    monkeypatch.setenv(wi.ENV_MODE, "spiffe")
    assert bridge.vault_holds_identity(warn=False) is True


def test_without_the_switch_the_sign_in_resolver_still_decides(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from kiro_crew import lts_sign_in
    from kiro_crew.auth import bridge

    asked: list[Any] = []

    def _resolver(**kwargs: Any) -> Any:
        asked.append(kwargs)
        raise lts_sign_in.SignInError(
            "no sign-in", lts_sign_in.CAUSE_NO_SIGN_IN, source=lts_sign_in.Source.KIRO_CLI
        )

    monkeypatch.delenv(wi.ENV_MODE, raising=False)
    monkeypatch.setattr(lts_sign_in, "select", _resolver)
    assert bridge.vault_holds_identity(warn=False) is False
    assert len(asked) == 1
