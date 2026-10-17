"""Every front route authenticates its caller, and the bind is a lane decision.

Two properties of a deployment whose compute carries its own internet-reachable
endpoint, pinned here rather than inside ``test_front_proxy``, which is about
forwarding.

**Why the posture is strict here.** A Lambda MicroVM has no security group and
its HTTPS endpoint is reachable from the internet; a caller holding one IAM
action mints a credential that names a PORT, and the ingress connector does not
govern it. All three measured on a real VM. The request's own credential is the
only boundary such a lane has, so the container authenticates for itself.

**What is NOT asserted here**: that the refusal is 403 on some paths and 404 on
others. It is deliberately identical for every unauthorised request, which is the
first test below.

**Which deployment this describes.** The posture is a property of the DEPLOYMENT,
not of the image, because one image serves lanes bounded by different things:
``require_auth_on_every_route`` is how a deployment with no network bound says it
has none. The flag is on throughout this file and off by default. It may only be
turned on for a deployment whose callers send the secret, so the gateway-side
change that sends it lands with the flag rather than after it.
``test_front_control_audit`` pins the posture where a network bound decides who
can reach the port.
"""

from __future__ import annotations

import httpx
import pytest
from container import common
from container.front.app import build_app


def _settings(tmp_path, *, control_secret: str | None = "CTRL") -> common.Settings:
    data_home = tmp_path / "data"
    return common.Settings(
        backend_port=18765,
        backend_run_dir=tmp_path / "run",
        front_port=8080,
        route_prefix="",
        control_secret=control_secret,
        data_home=data_home,
        config_dir=data_home,
        crew_name="crew",
        backup_bucket=None,
        backup_prefix="",
        single_principal=True,
        require_auth_on_every_route=True,
    )


def _client(settings: common.Settings, *, secret: str | None) -> httpx.AsyncClient:
    headers = {"X-SMC-Control-Secret": secret} if secret is not None else {}
    return httpx.AsyncClient(
        transport=httpx.ASGITransport(app=build_app(settings)),
        base_url="http://front",
        headers=headers,
    )


#: Every route an unauthorised caller might try, including the liveness route and
#: the index. The turn route is a POST and is added separately.
_PATHS = ["/health", "/api/health", "/crews", "/", "/does-not-exist"]


@pytest.mark.asyncio
async def test_no_route_answers_without_the_secret(tmp_path):
    """And every refusal is the SAME refusal.

    Identical body and status for a real route, the health route and a path that
    does not exist, so a caller who cannot authenticate cannot map the crew's
    surface by comparing answers. That is the property, not the status code: a
    403 here and a 404 there would be an enumeration oracle.
    """
    settings = _settings(tmp_path)
    async with _client(settings, secret=None) as anon:
        answers = [await anon.get(p) for p in _PATHS]
        answers.append(
            await anon.post(
                "/v1/chat/completions",
                json={"model": "crew", "id": "s", "messages": [], "stream": False},
            )
        )
    assert [r.status_code for r in answers] == [403] * len(answers)
    bodies = {r.text for r in answers}
    assert len(bodies) == 1, f"refusals differ by route, which maps the surface: {bodies}"
    assert answers[0].json()["code"] == "control_forbidden"


@pytest.mark.asyncio
async def test_a_wrong_secret_is_refused_like_a_missing_one(tmp_path):
    settings = _settings(tmp_path)
    async with _client(settings, secret="not-the-secret") as wrong:
        health = await wrong.get("/health")
    async with _client(settings, secret=None) as anon:
        missing = await anon.get("/health")
    assert health.status_code == missing.status_code == 403
    assert health.text == missing.text


@pytest.mark.asyncio
async def test_health_serves_the_authorised_caller(tmp_path):
    """The gate is not a removal: the route still works for whoever holds the secret."""
    settings = _settings(tmp_path)
    async with _client(settings, secret="CTRL") as client:
        bare = await client.get("/health")
    assert bare.status_code == 200
    assert bare.json() == {"status": "ok"}


@pytest.mark.asyncio
async def test_health_fails_closed_with_no_secret_configured(tmp_path):
    """A deployment that configured no secret serves nothing, health included.

    The same direction ``_control_authorized`` already took for control routes:
    an absent secret cannot be matched, so nothing can pass. Worth its own test
    because this is the one case where "the caller sent the right thing" and "the
    deployment has anything to check" come apart.
    """
    settings = _settings(tmp_path, control_secret=None)
    async with _client(settings, secret="anything") as client:
        assert (await client.get("/health")).status_code == 403


def test_front_bind_defaults_to_the_fargate_posture_and_is_settable(monkeypatch, tmp_path):
    """The bind is read from the environment, with Fargate's answer as the default.

    Pinned because the DEFAULT is what keeps an existing Fargate deployment
    unchanged by this field existing, and the override is what takes the crew off
    a MicroVM's public endpoint. A regression in either direction is silent: the
    container still starts and still answers, on the wrong interface.
    """
    monkeypatch.delenv("SMC_FRONT_BIND", raising=False)
    monkeypatch.setenv("SMC_DATA_HOME", str(tmp_path))
    assert common.load().front_bind == "0.0.0.0"
    monkeypatch.setenv("SMC_FRONT_BIND", "127.0.0.1")
    assert common.load().front_bind == "127.0.0.1"
