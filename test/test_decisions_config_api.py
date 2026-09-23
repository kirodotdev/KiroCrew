"""The dashboard's one writable decision setting, through the real handlers.

Consent to send conversation state is NOT a config path: it is the keystone
``decisions_consent.json`` behind ``PUT /api/decisions/consent``
(``test_decisions_consent.py``). What ``PATCH /api/config/kirocrew`` may write for
the seam is the sampling bucket alone, so these run the ACTUAL aiohttp handlers
against a temp ``config.json`` and pin:

* ``decisions.enabled`` is refused by the config PATCH, whatever value is sent --
  the switch has no config-path spelling at all;
* the bucket accepts only an in-range integer, and what it stores is what the
  config parses back;
* each ``model_route`` tier IS writable, because a tier value is an ordinary
  model id that grants nothing on its own -- the point validates it against what
  the provider advertises and keeps the session's model when it is absent -- so
  the card can offer the three pickers;
* ``history_budget_chars`` is NOT writable here: the number the card offers is the
  keystone CEILING behind the consent PUT, since ``config.json`` is agent-writable
  and this value decides how much conversation leaves the machine;
* ``provider.*`` is NOT writable here. The endpoint would let a caller choose
  where state is sent, and ``api_key`` reads back masked -- a PATCH beside a
  masked GET would let a caller overwrite a key it cannot read;
* GET masks ``provider.api_key``, which is what makes the Settings card safe to
  render.
"""

from __future__ import annotations

import json

import pytest
from aiohttp import web
from aiohttp.test_utils import TestClient, TestServer

from kiro_crew.config.sections import (
    DECISION_BUCKET_MAX,
    DECISION_BUCKET_MIN,
    DECISION_MODEL_ROUTE_TIERS,
)

_BASE_CONFIG = {
    "agents": {"kirocrew": {"kiro_agent": "kirocrew"}},
    "default_agent": "kirocrew",
}


def _app() -> web.Application:
    from kiro_crew.dashboard.handlers import api_kirocrew_config, api_kirocrew_config_patch

    app = web.Application()
    app.router.add_patch("/api/config/kirocrew", api_kirocrew_config_patch)
    app.router.add_get("/api/config/kirocrew", api_kirocrew_config)
    return app


@pytest.fixture
def config_file(tmp_path, monkeypatch):
    """A temp ``config.json`` both handlers read and the PATCH writes."""
    path = tmp_path / "config.json"
    path.write_text(json.dumps(_BASE_CONFIG), encoding="utf-8")
    monkeypatch.setattr("kiro_crew.config.loader.config_path", lambda: path)
    return path


async def _patch(client, path, value):
    return await client.patch("/api/config/kirocrew", json={"path": path, "value": value})


def _stored(path):
    return json.loads(path.read_text(encoding="utf-8")).get("decisions", {})


class TestEnabledIsNotAConfigPath:
    @pytest.mark.asyncio
    @pytest.mark.parametrize("value", [True, False, "true", 1])
    async def test_the_config_patch_refuses_the_switch(self, config_file, value):
        """The keystone is the only place consent is recorded; this route has no
        spelling for it, so an agent that can reach the config route gains nothing."""
        async with TestClient(TestServer(_app())) as client:
            resp = await _patch(client, "decisions.enabled", value)
            assert resp.status == 400
        assert "enabled" not in _stored(config_file)


class TestPatchBucket:
    @pytest.mark.asyncio
    @pytest.mark.parametrize("value", [DECISION_BUCKET_MIN, 1, 37, DECISION_BUCKET_MAX])
    async def test_an_in_range_bucket_is_stored(self, config_file, value):
        from kiro_crew.config.loader import KiroCrewConfig

        async with TestClient(TestServer(_app())) as client:
            assert (await _patch(client, "decisions.bucket", value)).status == 200
        assert _stored(config_file)["bucket"] == value
        assert KiroCrewConfig.load().decisions.bucket == value

    @pytest.mark.asyncio
    @pytest.mark.parametrize("value", [-1, DECISION_BUCKET_MAX + 1, 1000])
    async def test_an_out_of_range_bucket_is_refused_not_clamped(self, config_file, value):
        """The write gate refuses, so the stored value always reads as what is in force."""
        async with TestClient(TestServer(_app())) as client:
            resp = await _patch(client, "decisions.bucket", value)
            assert resp.status == 400
            expected = f"between {DECISION_BUCKET_MIN} and {DECISION_BUCKET_MAX}"
            assert expected in (await resp.json())["error"]
        assert "bucket" not in _stored(config_file)

    @pytest.mark.asyncio
    @pytest.mark.parametrize("value", ["lots", None, [10]])
    async def test_a_non_integer_bucket_is_refused(self, config_file, value):
        async with TestClient(TestServer(_app())) as client:
            resp = await _patch(client, "decisions.bucket", value)
            assert resp.status == 400
            assert "integer" in (await resp.json())["error"]


class TestModelRouteIsWritable:
    """The three tier pickers on the card write through this route."""

    @pytest.mark.asyncio
    @pytest.mark.parametrize("tier", DECISION_MODEL_ROUTE_TIERS)
    async def test_a_tier_accepts_inherit_and_stores_it(self, config_file, tier):
        """``""`` is INHERIT -- the turn keeps its session's model, the shipped default.

        The accepted value is the empty one on purpose: a concrete id would pin this
        test to an account entitlement, and naming one in code is what
        ``model-selection.md`` forbids.
        """
        async with TestClient(TestServer(_app())) as client:
            resp = await _patch(client, f"decisions.model_route.{tier}", "")
            assert resp.status == 200, await resp.text()
        assert _stored(config_file)["model_route"][tier] == ""

    @pytest.mark.asyncio
    async def test_a_tier_pin_resolves_the_provider_off_the_event_loop(
        self, config_file, monkeypatch
    ):
        """The validator's provider read must not run on the loop thread.

        A tier pin reaches ``_validate_role_model``, which asks
        ``_model_rejected_reason`` whether the account can use the id; that check reads
        the configured provider with ``KiroCrewConfig.load()`` -- a file read plus a
        schema validation -- when nobody hands it one, and the hook is called
        SYNCHRONOUSLY from the PATCH handler. So the handler resolves it with
        ``asyncio.to_thread`` and passes the answer down. Fails if that hop is removed,
        for this key and for the four ``agent.role_models`` keys sharing the hook.
        """
        import asyncio
        import threading

        from kiro_crew.dashboard.handlers import core as _core

        loop_thread = threading.current_thread()
        seen: list[threading.Thread] = []
        real = _core._active_provider_name

        def _spy() -> str:
            seen.append(threading.current_thread())
            return real()

        monkeypatch.setattr(_core, "_active_provider_name", _spy)
        async with TestClient(TestServer(_app())) as client:
            resp = await _patch(client, "decisions.model_route.simple", "")
            assert resp.status == 200, await resp.text()
        assert seen, "the validator never resolved the provider"
        assert asyncio.get_running_loop() is not None
        assert all(
            t is not loop_thread for t in seen
        ), "the provider was read on the event loop thread"

    @pytest.mark.asyncio
    @pytest.mark.parametrize("tier", DECISION_MODEL_ROUTE_TIERS)
    async def test_a_tier_refuses_a_value_outside_the_model_id_grammar(self, config_file, tier):
        """Same grammar the ``agent.role_models.*`` pins beside it enforce."""
        async with TestClient(TestServer(_app())) as client:
            resp = await _patch(client, f"decisions.model_route.{tier}", "a; rm -rf /")
            assert resp.status == 400
        assert _stored(config_file) == {}

    @pytest.mark.asyncio
    async def test_a_tier_the_question_never_offers_is_not_editable(self, config_file):
        """The editable set comes from the point's own closed answer domain.

        A path naming a fourth tier is refused rather than stored, so the route cannot
        record a pin that could never be answered.
        """
        assert "trivial" not in DECISION_MODEL_ROUTE_TIERS
        async with TestClient(TestServer(_app())) as client:
            resp = await _patch(client, "decisions.model_route.trivial", "")
            assert resp.status == 400
            assert "not editable" in (await resp.json())["error"]
        assert _stored(config_file) == {}

    @pytest.mark.asyncio
    async def test_the_whole_map_is_not_editable_in_one_write(self, config_file):
        """Per-tier only, so every value passes the grammar and the entitlement check."""
        async with TestClient(TestServer(_app())) as client:
            resp = await _patch(client, "decisions.model_route", {"simple": ""})
            assert resp.status == 400
            assert "not editable" in (await resp.json())["error"]
        assert _stored(config_file) == {}


class TestTheHistoryCeilingIsNotWritableHere:
    @pytest.mark.asyncio
    async def test_the_budget_cannot_be_written_from_the_config_route(self, config_file):
        """The card's number box writes the keystone ceiling, not this path.

        The config value is what the seam ASKS with and an agent may raise it; the
        ceiling that clamps it is on the keystone behind the owner-only consent PUT.
        Offering this path to the dashboard would put the owner's control in the file
        the agent can also write.
        """
        async with TestClient(TestServer(_app())) as client:
            resp = await _patch(client, "decisions.history_budget_chars", 4000)
            assert resp.status == 400
            assert "not editable" in (await resp.json())["error"]
        assert _stored(config_file) == {}


class TestProviderIsNotWritable:
    @pytest.mark.asyncio
    @pytest.mark.parametrize(
        "path,value",
        [
            ("decisions.provider.endpoint", "https://judge.example.invalid/v1"),
            ("decisions.provider.api_key", "literal-key"),
            ("decisions.provider.model", "jev-nightly"),
            ("decisions.provider.timeout_ms", 5000),
            ("decisions.provider", {"endpoint": "https://judge.example.invalid/v1"}),
        ],
    )
    async def test_the_provider_cannot_be_written_from_the_dashboard(
        self, config_file, path, value
    ):
        async with TestClient(TestServer(_app())) as client:
            resp = await _patch(client, path, value)
            assert resp.status == 400
            assert "not editable" in (await resp.json())["error"]
        assert _stored(config_file) == {}


class TestGet:
    @pytest.mark.asyncio
    async def test_the_api_key_reads_back_masked(self, config_file):
        from kiro_crew.dashboard.handlers.core import _SENSITIVE_MASK

        secret = "sk-" + "realsecret0123456789"
        config_file.write_text(
            json.dumps(
                {
                    **_BASE_CONFIG,
                    "decisions": {"enabled": True, "provider": {"api_key": secret}},
                }
            ),
            encoding="utf-8",
        )
        async with TestClient(TestServer(_app())) as client:
            body = await (await client.get("/api/config/kirocrew")).json()
        assert body["decisions"]["provider"]["api_key"] == _SENSITIVE_MASK
        assert secret not in json.dumps(body)

    @pytest.mark.asyncio
    async def test_the_bucket_reads_back_verbatim(self, config_file):
        """Not sensitive, so the Settings card gets its share to print."""
        async with TestClient(TestServer(_app())) as client:
            await _patch(client, "decisions.bucket", 25)
            body = await (await client.get("/api/config/kirocrew")).json()
        assert body["decisions"]["bucket"] == 25
