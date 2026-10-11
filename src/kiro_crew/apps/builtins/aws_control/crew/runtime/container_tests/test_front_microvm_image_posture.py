"""The front, started the way the MicroVM guest starts it, serves nothing anonymously.

Its sibling ``test_front_microvm_posture`` builds ``Settings`` by hand with
``require_auth_on_every_route=True`` and proves the GATE works. That leaves one
question open, and it is the one that matters on a live VM: does the guest's own
startup actually arrive at that setting?

So nothing here sets the flag. The environment is read out of
``Dockerfile.microvm`` -- the image layer the lane builds -- and handed to
``common.load()``, which is what the guest calls at process start. If the image
stops baking the flag, or the container stops reading the name the image sets,
these tests fail where the earlier ones still pass.

**Why the image and not the launch.** A MicroVM carries its own HTTPS endpoint,
reachable from the internet, and the ingress connector does not govern it: the
only control the platform enforces there is a port list, so a holder of a
correctly-scoped token is an arbitrary internet caller. A value supplied at
launch is one a caller can omit, and the posture of a lane with no network bound
must not depend on every launch asking for it.
"""

from __future__ import annotations

import re
from pathlib import Path

import httpx
import pytest
from container import common
from container.front.app import build_app

#: The image layer the MicroVM lane builds. Read from disk rather than restated,
#: so this file cannot agree with a Dockerfile that changed.
_MICROVM_DOCKERFILE = Path(common.__file__).resolve().parents[2] / "Dockerfile.microvm"

#: Every route an unauthenticated caller might try, including the liveness route.
#: The customer turn is a POST and is added separately.
_GET_PATHS = ["/health", "/api/health", "/crews", "/", "/does-not-exist"]
_TURN_PATH = "/v1/chat/completions"


def image_environment() -> dict[str, str]:
    """``SMC_*`` names the MicroVM image bakes, parsed from its own ``ENV``.

    A real parse of the directive, including its line continuations, because the
    point is to run the container the way the IMAGE configures it. A substring
    search would pass on a flag that appears only in a comment.
    """
    text = _MICROVM_DOCKERFILE.read_text(encoding="utf-8")
    # Join continuations first, then drop comment lines, so a commented-out ENV
    # cannot contribute and a real multi-line one is not cut in half.
    joined = re.sub(r"\\\s*\n\s*", " ", text)
    found: dict[str, str] = {}
    for line in joined.splitlines():
        if not line.startswith("ENV "):
            continue
        for pair in line[4:].split():
            if "=" not in pair:
                continue
            name, _, value = pair.partition("=")
            if name.startswith("SMC_"):
                found[name] = value
    return found


def guest_settings(tmp_path, *, control_secret: str | None = "CTRL") -> common.Settings:
    """``Settings`` as the guest's own startup produces them.

    Through ``common.load()``, reading the image's environment, so the path under
    test is the one a VM runs. Only the values a container cannot invent -- the
    data home and the crew's secret -- are supplied here.
    """
    data_home = tmp_path / "data"
    env = image_environment()
    assert env, f"{_MICROVM_DOCKERFILE.name} declares no SMC_* environment"
    return common.Settings(
        backend_port=int(env.get("SMC_BACKEND_PORT", "8765")),
        backend_run_dir=tmp_path / "run",
        front_port=int(env["SMC_FRONT_PORT"]),
        route_prefix=env.get("SMC_ROUTE_PREFIX", ""),
        control_secret=control_secret,
        data_home=data_home,
        config_dir=data_home,
        crew_name="crew",
        backup_bucket=None,
        backup_prefix="",
        single_principal=True,
        require_auth_on_every_route=env.get("SMC_REQUIRE_AUTH_ALL_ROUTES") == "1",
    )


def _client(settings: common.Settings, *, secret: str | None) -> httpx.AsyncClient:
    headers = {"X-SMC-Control-Secret": secret} if secret is not None else {}
    return httpx.AsyncClient(
        transport=httpx.ASGITransport(app=build_app(settings)),
        base_url="http://front",
        headers=headers,
    )


class TestTheImageBakesTheStrictPosture:
    def test_the_image_declares_the_flag(self):
        assert image_environment().get("SMC_REQUIRE_AUTH_ALL_ROUTES") == "1"

    def test_the_guests_own_startup_arrives_at_the_strict_setting(self, tmp_path):
        """The join this file exists for: the image sets it and ``load()`` reads
        it, so a VM is strict without anything at launch asking."""
        assert guest_settings(tmp_path).require_auth_on_every_route is True

    def test_the_front_binds_loopback_on_this_lane(self, tmp_path):
        """The other half of the same boundary. A caller who mints an endpoint
        credential names a PORT, so the front must neither hold the hook port nor
        answer on a routable address."""
        env = image_environment()
        assert env["SMC_FRONT_BIND"] == "127.0.0.1"
        assert env["SMC_FRONT_PORT"] != env["SMC_HOOK_PORT"]


class TestNoRouteAnswersAnonymously:
    @pytest.mark.asyncio
    @pytest.mark.parametrize("path", _GET_PATHS)
    async def test_a_get_without_the_secret_is_refused(self, tmp_path, path):
        async with _client(guest_settings(tmp_path), secret=None) as anon:
            assert (await anon.get(path)).status_code == 403

    @pytest.mark.asyncio
    async def test_the_turn_route_without_the_secret_is_refused(self, tmp_path):
        """The customer surface too. On a lane whose endpoint is reachable from
        the internet, an anonymous turn is an anonymous caller spending the
        owner's model credential."""
        async with _client(guest_settings(tmp_path), secret=None) as anon:
            response = await anon.post(_TURN_PATH, json={"messages": []})
        assert response.status_code == 403

    @pytest.mark.asyncio
    async def test_a_wrong_secret_is_refused_like_a_missing_one(self, tmp_path):
        async with _client(guest_settings(tmp_path), secret="WRONG") as wrong:
            assert (await wrong.get("/health")).status_code == 403
            assert (await wrong.post(_TURN_PATH, json={"messages": []})).status_code == 403

    @pytest.mark.asyncio
    async def test_every_refusal_is_the_same_refusal(self, tmp_path):
        """A caller who cannot authenticate must not be able to map this crew's
        surface by comparing answers: a 403 here and a 404 there is an
        enumeration oracle."""
        async with _client(guest_settings(tmp_path), secret=None) as anon:
            answers = [await anon.get(path) for path in _GET_PATHS]
        assert {reply.status_code for reply in answers} == {403}
        assert len({reply.text for reply in answers}) == 1

    @pytest.mark.asyncio
    async def test_the_authorised_caller_still_gets_liveness(self, tmp_path):
        """The gate must not deny its own authorised callers -- a crew nobody can
        probe is as unusable as one anybody can."""
        async with _client(guest_settings(tmp_path), secret="CTRL") as owner:
            response = await owner.get("/health")
        assert response.status_code == 200
        assert response.json() == {"status": "ok"}


class TestFargateIsUnchanged:
    def test_the_fargate_layers_bake_no_such_flag(self):
        """Absent means off, which is what that lane's private subnet and
        zero-ingress security group already assert."""
        runtime = _MICROVM_DOCKERFILE.parent
        for name in ("Dockerfile", "Dockerfile.crew"):
            assert "SMC_REQUIRE_AUTH_ALL_ROUTES" not in (runtime / name).read_text(encoding="utf-8")

    def test_an_environment_without_the_flag_leaves_the_setting_off(self, tmp_path):
        """``Settings`` built without the image's flag: the setting stays off, which
        is the claim a deployment makes by saying nothing."""
        data_home = tmp_path / "data"
        settings = common.Settings(
            backend_port=8765,
            backend_run_dir=tmp_path / "run",
            front_port=8080,
            route_prefix="",
            control_secret="CTRL",
            data_home=data_home,
            config_dir=data_home,
            crew_name="crew",
            backup_bucket=None,
            backup_prefix="",
            single_principal=True,
        )
        assert settings.require_auth_on_every_route is False

    @pytest.mark.asyncio
    async def test_this_field_changes_nothing_where_it_is_left_off(self, tmp_path):
        """A deployment that claims a network placement is unaffected by this
        setting: its posture is the one its placement asserts."""
        data_home = tmp_path / "data"
        settings = common.Settings(
            backend_port=8765,
            backend_run_dir=tmp_path / "run",
            front_port=8080,
            route_prefix="",
            control_secret="CTRL",
            data_home=data_home,
            config_dir=data_home,
            crew_name="crew",
            backup_bucket=None,
            backup_prefix="",
            single_principal=True,
        )
        async with _client(settings, secret=None) as anon:
            assert (await anon.get("/health")).status_code == 200
