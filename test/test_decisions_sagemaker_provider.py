"""Choosing a SageMaker endpoint as the decision model, from the Decisions card.

Pinned here:

* the provider route BUILDS the ``sagemaker://`` address from three checked fields
  and refuses every body that could add anything else to it;
* the profile must be one the local AWS config files name;
* the write is owner-only and audited, and it carries no consent to the new address;
* the GET names a configured SageMaker endpoint by its parts;
* the connection test sends one fixed question through the decision client and
  reports a fixed word, never the request or a credential.
"""

from __future__ import annotations

import json
from unittest.mock import AsyncMock, MagicMock

import pytest

from kiro_crew.config.sections import DECISION_PROVIDER_ENDPOINT_DEFAULT as DEFAULT_ENDPOINT
from kiro_crew.config.sections import DECISION_PROVIDER_MODEL_DEFAULT as DEFAULT_MODEL
from kiro_crew.decisions import consent, impl_sagemaker

GOOD = {"preset": "sagemaker", "endpoint_name": "decision-model-a", "region": "us-west-2"}
GOOD_ADDRESS = "sagemaker://decision-model-a?region=us-west-2"


def _request(*, app: str = "", user: str = "owner-1", owner: str = "owner-1", body=None):
    """A request shaped like a real DASHBOARD OWNER call (see test_decisions_consent.py)."""
    req = MagicMock()
    req.path = "/api/decisions/provider"
    store = {"app": app, "user": user}
    req.get = lambda key, default=None: store.get(key, default)
    req.__contains__ = lambda _self, key: key in store
    req.__getitem__ = lambda _self, key: store[key]
    state = MagicMock()
    state.owner_id = owner
    req.app = {"state": state}
    req.json = AsyncMock(return_value=body if body is not None else {})
    return req


@pytest.fixture
def audit(monkeypatch):
    import kiro_crew.dashboard.handlers as handlers_pkg

    rows: list[dict] = []
    fake = MagicMock()
    fake.log_api_access = lambda **kw: rows.append(kw)
    monkeypatch.setattr(handlers_pkg, "sel", lambda: fake)
    return rows


@pytest.fixture
def keystone(tmp_path, monkeypatch):
    path = tmp_path / "decisions_consent.json"
    monkeypatch.setattr("kiro_crew.config.loader.decisions_consent_path", lambda: path)
    return path


@pytest.fixture
def provider_config():
    """A reader for the ``decisions.provider`` the pinned data home's config.json holds."""
    from kiro_crew.config.loader import config_path

    path = config_path()

    def _provider() -> dict:
        data = json.loads(path.read_text(encoding="utf-8")) if path.exists() else {}
        return data.get("decisions", {}).get("provider", {})

    return _provider


@pytest.fixture(autouse=True)
def runtime(_floor_monkeypatch):
    """No local model server may start; the route's runtime calls are recorded."""
    from kiro_crew.decisions import local_runtime

    calls: list = []
    fake = MagicMock()
    fake.status = lambda: {
        "preset": "",
        "state": "idle",
        "port": 0,
        "bytes_done": 0,
        "bytes_total": 0,
        "error": "",
    }
    fake.installed_ids = lambda: []
    fake.activate = lambda m, port: calls.append(("activate", m.id))
    fake.deactivate = lambda **_: calls.append(("deactivate",))
    _floor_monkeypatch.setattr(local_runtime, "get_runtime", lambda: fake)
    return calls


@pytest.fixture(autouse=True)
def aws_profiles(tmp_path, _floor_monkeypatch):
    """AWS config files naming exactly one profile, ``research``; no credential file."""
    config = tmp_path / "aws-config"
    config.write_text("[default]\nregion = us-west-2\n\n[profile research]\n", encoding="utf-8")
    _floor_monkeypatch.setenv("AWS_CONFIG_FILE", str(config))
    _floor_monkeypatch.setenv("AWS_SHARED_CREDENTIALS_FILE", str(tmp_path / "no-credentials"))


@pytest.fixture(autouse=True)
def fresh_region_cache(_floor_monkeypatch):
    """The process-wide botocore region cache starts empty in every test."""
    from kiro_crew.dashboard.handlers import decisions_sagemaker as ds

    _floor_monkeypatch.setattr(ds, "_botocore_regions_cache", None, raising=False)


@pytest.fixture
def not_denied(monkeypatch):
    from kiro_crew.decisions import capability

    monkeypatch.setattr(capability, "is_decisions_denied", lambda *a, **k: False)
    monkeypatch.setattr(capability, "targets_denied", lambda *a, **k: False)


async def _put(body, **kw):
    from kiro_crew.dashboard.handlers.decisions import api_decisions_provider_put

    resp = await api_decisions_provider_put(_request(body=body, **kw))
    return resp.status, json.loads(resp.text)


class TestBuildEndpoint:
    @pytest.mark.parametrize(
        ("name", "region", "profile", "address"),
        [
            ("decision-model-a", "us-west-2", "", GOOD_ADDRESS),
            ("a", "eu-central-1", "research", "sagemaker://a?region=eu-central-1&profile=research"),
            (
                "x--y",
                "us-gov-west-1",
                "dev.team+1",
                "sagemaker://x--y?region=us-gov-west-1&profile=dev.team+1",
            ),
        ],
        ids=["no-profile", "with-profile", "plus-and-dots-in-profile"],
    )
    def test_the_address_round_trips_through_the_parser(self, name, region, profile, address):
        assert impl_sagemaker.build_endpoint(name, region, profile) == address
        target = impl_sagemaker.parse_endpoint(address)
        assert (target.endpoint_name, target.region, target.profile) == (name, region, profile)

    @pytest.mark.parametrize("name", ["x" * 63, "model-" + "x" * 57, "a" + "-" * 61 + "z"])
    def test_a_63_character_name_round_trips(self, name):
        address = impl_sagemaker.build_endpoint(name, "us-west-2")
        assert impl_sagemaker.parse_endpoint(address).endpoint_name == name

    @pytest.mark.parametrize("region", ["us-" + "x" * 58 + "-1", "us" + "-a" * 29 + "-12"])
    def test_a_63_character_region_round_trips(self, region):
        address = impl_sagemaker.build_endpoint("model", region)
        assert impl_sagemaker.parse_endpoint(address).region == region

    @pytest.mark.parametrize(
        ("name", "region", "field"),
        [
            ("model-" + "x" * 58, "us-west-2", "endpoint_name"),
            ("a" + "-" * 62 + "z", "us-west-2", "endpoint_name"),
            ("model", "us-" + "x" * 59 + "-1", "region"),
            ("model", "us" + "-a" * 30 + "-1", "region"),
        ],
    )
    def test_overlong_parts_are_refused_by_builder_and_parser(self, name, region, field):
        with pytest.raises(impl_sagemaker.SageMakerAddressFieldError) as exc:
            impl_sagemaker.build_endpoint(name, region)
        assert exc.value.field == field
        with pytest.raises(impl_sagemaker.SageMakerConfigError):
            impl_sagemaker.parse_endpoint(f"sagemaker://{name}?region={region}")


class TestSageMakerPut:
    @pytest.mark.asyncio
    @pytest.mark.parametrize(
        ("patch", "field"),
        [
            ({"endpoint_name": "model?region=us-east-1"}, "endpoint_name"),
            ({"endpoint_name": "model/invocations"}, "endpoint_name"),
            ({"endpoint_name": "user@model"}, "endpoint_name"),
            ({"endpoint_name": "model:443"}, "endpoint_name"),
            ({"endpoint_name": "-model"}, "endpoint_name"),
            ({"endpoint_name": "a" * 64}, "endpoint_name"),
            ({"endpoint_name": "model-" + "x" * 58}, "endpoint_name"),
            ({"region": "us-" + "x" * 59 + "-1"}, "region"),
            ({"region": "us" + "-a" * 30 + "-1"}, "region"),
            ({"endpoint_name": ""}, "endpoint_name"),
            ({"endpoint_name": 7}, "endpoint_name"),
            ({"region": "us-west-2&x="}, "region"),
            ({"region": "us-west-2&profile=other"}, "region"),
            ({"region": "evil.example"}, "region"),
            ({"region": "us-isob-east-1"}, "region"),
            ({"region": "US-WEST-2"}, "region"),
            ({"profile": "research&region=us-east-1"}, "profile"),
            ({"profile": "a/b"}, "profile"),
            ({"profile": "-lead"}, "profile"),
            ({"profile": None}, "profile"),
        ],
        ids=[
            "name-with-query",
            "name-with-path",
            "name-with-userinfo",
            "name-with-port",
            "name-leading-hyphen",
            "name-too-long",
            "hyphenated-name-too-long",
            "region-segment-too-long",
            "region-total-too-long",
            "name-empty",
            "name-not-a-string",
            "region-with-ampersand",
            "region-smuggling-a-key",
            "region-a-host",
            "region-isolated-partition",
            "region-upper-case",
            "profile-smuggling-a-key",
            "profile-with-slash",
            "profile-leading-hyphen",
            "profile-null",
        ],
    )
    async def test_a_bad_field_is_refused_by_name_and_nothing_is_written(
        self, patch, field, audit, provider_config, runtime
    ):
        status, body = await _put({**GOOD, **patch})
        assert status == 400
        assert body["code"] == "decisions_provider_sagemaker_invalid"
        assert body["field"] == field
        assert provider_config() == {}
        assert runtime == [], "a refused body starts and stops nothing"
        row = [r for r in audit if r["operation"] == "decisions_provider_put"][-1]
        assert (row["outcome"], row["error"]) == ("denied", "decisions_provider_sagemaker_invalid")

    @pytest.mark.asyncio
    @pytest.mark.parametrize(
        "body",
        [
            {**GOOD, "endpoint": "https://evil.example"},
            {**GOOD, "model": "other"},
            {"preset": "sagemaker", "region": "us-west-2"},
            {"preset": "sagemaker", "endpoint_name": "model"},
            {"preset": "sagemaker"},
        ],
        ids=["an-endpoint", "a-model", "no-name", "no-region", "bare"],
    )
    async def test_a_body_of_another_shape_is_refused(self, body, audit, provider_config):
        status, payload = await _put(body)
        assert status == 400
        assert payload["code"] == "decisions_provider_invalid_body"
        assert provider_config() == {}

    @pytest.mark.asyncio
    async def test_an_unknown_profile_is_refused(self, audit, provider_config, not_denied):
        status, body = await _put({**GOOD, "profile": "nobody"})
        assert status == 400
        assert (body["code"], body["field"]) == (
            "decisions_provider_sagemaker_unknown_profile",
            "profile",
        )
        assert provider_config() == {}

    @pytest.mark.asyncio
    async def test_without_boto3_the_route_says_so(self, audit, provider_config, monkeypatch):
        monkeypatch.setattr(impl_sagemaker, "boto3_installed", lambda: False)
        status, body = await _put(GOOD)
        assert status == 409
        assert body["code"] == "decisions_provider_sagemaker_unavailable"
        assert provider_config() == {}

    @pytest.mark.asyncio
    async def test_the_route_writes_the_address_it_built_and_audits_it(
        self, audit, provider_config, keystone, not_denied, runtime
    ):
        status, payload = await _put({**GOOD, "profile": "research"})
        assert status == 200
        address = f"{GOOD_ADDRESS}&profile=research"
        assert provider_config() == {
            "endpoint": address,
            "model": DEFAULT_MODEL,
            "timeout_ms": 5000,
        }
        assert payload["active"] == "sagemaker"
        assert payload["sagemaker"] == {
            "endpoint_name": "decision-model-a",
            "region": "us-west-2",
            "profile": "research",
        }
        assert runtime == [("deactivate",)], "a local model the owner left stops"
        row = [r for r in audit if r["operation"] == "decisions_provider_put"][-1]
        assert row["outcome"] == "allowed"
        assert f"endpoint={address}" in row["resources"]

    @pytest.mark.asyncio
    async def test_a_hand_set_model_and_timeout_survive_a_change_of_endpoint(
        self, audit, provider_config, keystone, not_denied
    ):
        from kiro_crew.config.loader import config_path, update_config_locked

        def _seed(data):
            data["decisions"] = {
                "provider": {"endpoint": GOOD_ADDRESS, "model": "my-model", "timeout_ms": 8000}
            }
            return data

        update_config_locked(config_path(), mutate=_seed)
        status, _ = await _put({**GOOD, "endpoint_name": "decision-model-b"})
        assert status == 200
        assert provider_config() == {
            "endpoint": "sagemaker://decision-model-b?region=us-west-2",
            "model": "my-model",
            "timeout_ms": 8000,
        }

    @pytest.mark.asyncio
    async def test_a_standing_consent_is_not_carried_to_the_endpoint(
        self, audit, provider_config, keystone, not_denied
    ):
        keystone.write_text(
            json.dumps({"enabled": True, "endpoint": DEFAULT_ENDPOINT, "browser_images": True}),
            encoding="utf-8",
        )
        status, _ = await _put(GOOD)
        assert status == 200
        state = consent.load_state()
        assert consent.consented_endpoint(state) == DEFAULT_ENDPOINT
        assert consent.permits(GOOD_ADDRESS, state) is False

    @pytest.mark.asyncio
    async def test_a_fleet_denial_of_the_hosted_side_refuses_it(
        self, audit, provider_config, monkeypatch
    ):
        from kiro_crew.decisions import capability

        monkeypatch.setattr(capability, "is_decisions_denied", lambda *, local=False: not local)
        status, body = await _put(GOOD)
        assert status == 403
        assert body["code"] == "decisions_capability_denied"
        assert provider_config() == {}

    @pytest.mark.asyncio
    async def test_a_non_owner_is_refused(self, audit, provider_config):
        status, body = await _put(GOOD, user="someone-else")
        assert status == 403
        assert provider_config() == {}
        status, _ = await _put(GOOD, app="some-app")
        assert status == 403


class TestSageMakerGet:
    @pytest.mark.asyncio
    async def test_a_configured_endpoint_is_named_by_its_parts(self, audit):
        from kiro_crew.config.loader import config_path, update_config_locked
        from kiro_crew.dashboard.handlers.decisions import api_decisions_provider_get

        def _seed(data):
            data["decisions"] = {"provider": {"endpoint": f"{GOOD_ADDRESS}&profile=research"}}
            return data

        update_config_locked(config_path(), mutate=_seed)
        payload = json.loads((await api_decisions_provider_get(_request())).text)
        assert payload["active"] == "sagemaker"
        assert payload["sagemaker"]["profile"] == "research"
        assert payload["sagemaker_available"] is True

    @pytest.mark.asyncio
    @pytest.mark.parametrize(
        "endpoint",
        ["sagemaker://model/path?region=us-west-2", "sagemaker://[my-endpoint?region=us-west-2"],
        ids=["a-path", "an-unbalanced-bracket"],
    )
    async def test_an_address_that_does_not_parse_stays_custom(self, endpoint, audit):
        from kiro_crew.config.loader import config_path, update_config_locked
        from kiro_crew.dashboard.handlers.decisions import api_decisions_provider_get

        def _seed(data):
            data["decisions"] = {"provider": {"endpoint": endpoint}}
            return data

        update_config_locked(config_path(), mutate=_seed)
        resp = await api_decisions_provider_get(_request())
        assert resp.status == 200
        payload = json.loads(resp.text)
        assert (payload["active"], payload["sagemaker"]) == ("custom", None)


def _ceiling(monkeypatch, *allowed_hosts):
    """A ceiling whose ``endpoints`` list names *allowed_hosts* (globs); None asks which hosts."""
    from fnmatch import fnmatchcase

    from kiro_crew.decisions import capability
    from kiro_crew.platform.governance_parts.matching import url_policy_hosts

    asked: list[str] = []

    def targets(*_a, point=None, model=None, endpoint=None, audit=True):
        asked.append(endpoint)
        hosts = url_policy_hosts(endpoint)
        return not all(any(fnmatchcase(h, g) for g in allowed_hosts) for h in hosts)

    monkeypatch.setattr(capability, "targets_denied", targets)
    return asked


async def _get() -> dict:
    from kiro_crew.dashboard.handlers.decisions import api_decisions_provider_get

    return json.loads((await api_decisions_provider_get(_request())).text)


SAGEMAKER_ONLY = ("runtime.sagemaker.*.amazonaws.com", "127.0.0.1")


class TestRefusedOptionIsNotOffered:
    @pytest.mark.asyncio
    async def test_no_ceiling_offers_hosted_and_sagemaker(self, audit):
        payload = await _get()
        assert (payload["hosted_permitted"], payload["sagemaker_permitted"]) == (True, True)

    @pytest.mark.asyncio
    async def test_a_ceiling_of_sagemaker_and_loopback_hides_the_hosted_default(
        self, audit, monkeypatch
    ):
        _ceiling(monkeypatch, *SAGEMAKER_ONLY)
        payload = await _get()
        assert payload["hosted_permitted"] is False
        assert payload["sagemaker_permitted"] is True
        assert payload["local_permitted"] is True

    @pytest.mark.asyncio
    async def test_one_permitted_region_is_enough_to_offer_sagemaker(self, audit, monkeypatch):
        asked = _ceiling(monkeypatch, "runtime.sagemaker.eu-west-3.amazonaws.com")
        assert (await _get())["sagemaker_permitted"] is True
        assert any("eu-west-3" in url for url in asked)

    @pytest.mark.asyncio
    @pytest.mark.parametrize(
        "allowed",
        [
            ("runtime.sagemaker.ca-west-1.amazonaws.com", "127.0.0.1"),
            ("runtime.sagemaker.*.amazonaws.com",),
            ("*.amazonaws.com", "127.0.0.1"),
        ],
    )
    async def test_a_region_the_static_list_lacked_still_offers_sagemaker(
        self, audit, monkeypatch, allowed
    ):
        from kiro_crew.dashboard.handlers import decisions_sagemaker as ds

        monkeypatch.setattr(ds, "SAGEMAKER_REGIONS", ("us-east-1",))
        monkeypatch.setattr(ds, "_botocore_regions", lambda: ("ca-west-1",))
        _ceiling(monkeypatch, *allowed)
        assert (await _get())["sagemaker_permitted"] is True

    @pytest.mark.asyncio
    async def test_the_real_botocore_metadata_covers_ca_west_1(self, audit, monkeypatch):
        pytest.importorskip("botocore")
        from kiro_crew.dashboard.handlers import decisions_sagemaker as ds

        monkeypatch.setattr(ds, "SAGEMAKER_REGIONS", ())
        _ceiling(monkeypatch, "runtime.sagemaker.ca-west-1.amazonaws.com")
        assert (await _get())["sagemaker_permitted"] is True

    def test_the_static_floor_names_the_newer_commercial_regions(self):
        from kiro_crew.dashboard.handlers.decisions_sagemaker import SAGEMAKER_REGIONS

        for region in ("ca-west-1", "ap-southeast-5", "ap-southeast-7", "mx-central-1"):
            assert region in SAGEMAKER_REGIONS

    @pytest.mark.asyncio
    async def test_no_permitted_sagemaker_host_hides_sagemaker_and_keeps_hosted(
        self, audit, monkeypatch
    ):
        _ceiling(monkeypatch, "api.typesafe.ai", "127.0.0.1")
        payload = await _get()
        assert (payload["hosted_permitted"], payload["sagemaker_permitted"]) == (True, False)

    @pytest.mark.asyncio
    async def test_a_configured_sagemaker_address_the_ceiling_refuses_reads_as_no_model(
        self, audit, monkeypatch
    ):
        from kiro_crew.config.loader import config_path, update_config_locked

        def _seed(data):
            data["decisions"] = {"provider": {"endpoint": GOOD_ADDRESS}}
            return data

        update_config_locked(config_path(), mutate=_seed)
        _ceiling(monkeypatch, "api.typesafe.ai", "127.0.0.1")
        payload = await _get()
        assert payload["active"] == "none"
        assert payload["sagemaker_permitted"] is False
        assert payload["configured_endpoint"] == GOOD_ADDRESS

    @pytest.mark.asyncio
    async def test_a_hosted_row_deny_withdraws_both_without_asking_the_list(
        self, audit, monkeypatch
    ):
        from kiro_crew.decisions import capability

        asked = _ceiling(monkeypatch, "*")
        monkeypatch.setattr(capability, "denied_sides", lambda *a, **k: (True, True))
        payload = await _get()
        assert (payload["hosted_permitted"], payload["sagemaker_permitted"]) == (False, False)
        assert asked == []

    @pytest.mark.asyncio
    async def test_the_configured_region_is_asked_first(self, audit, monkeypatch):
        from kiro_crew.config.loader import config_path, update_config_locked

        def _seed(data):
            data["decisions"] = {"provider": {"endpoint": GOOD_ADDRESS}}
            return data

        update_config_locked(config_path(), mutate=_seed)
        asked = _ceiling(monkeypatch, "*")
        payload = await _get()
        assert payload["sagemaker"]["region"] == "us-west-2"
        assert "us-west-2" in asked[-1]
        # One probe (the configured region answers, so no other region is asked) and
        # one check of the configured address's own host.
        assert len([u for u in asked if "sagemaker" in u]) == 2
        assert all("us-west-2" in u for u in asked if "sagemaker" in u)

    @pytest.mark.asyncio
    async def test_put_refuses_a_regional_host_the_ceiling_refuses(
        self, audit, provider_config, monkeypatch
    ):
        asked = _ceiling(monkeypatch, "runtime.sagemaker.eu-west-1.amazonaws.com")
        status, body = await _put(GOOD)
        assert status == 403 and body["code"] == "decisions_capability_denied"
        assert provider_config() == {}
        assert asked == [
            "https://runtime.sagemaker.us-west-2.amazonaws.com/endpoints/decision-model-a/invocations"
        ]
        status, _ = await _put({**GOOD, "region": "eu-west-1"})
        assert status == 200

    @pytest.mark.asyncio
    async def test_put_refuses_hosted_jev_where_the_list_refuses_its_host(
        self, audit, provider_config, monkeypatch
    ):
        _ceiling(monkeypatch, *SAGEMAKER_ONLY)
        status, body = await _put({"preset": "jev"})
        assert status == 403 and body["code"] == "decisions_capability_denied"
        assert provider_config() == {}
        status, _ = await _put({"preset": "none"})
        assert status == 200

    @pytest.mark.asyncio
    async def test_a_local_preset_is_never_asked_about_the_endpoints_list(
        self, audit, provider_config, monkeypatch
    ):
        asked = _ceiling(monkeypatch)
        status, _ = await _put({"preset": "laya"})
        assert status == 200
        # The answer's own payload asks about the hosted options; the preset is never asked.
        assert not any("127.0.0.1" in url for url in asked)


def _refusing_ceiling(monkeypatch):
    """A real ``targets_denied`` over a ceiling that refuses every ``endpoints:`` item.

    Returns the ``governance_decision`` rows written. The decisions rows themselves
    permit, so only the ``endpoints`` list refuses.
    """
    import kiro_crew.sel as sel_mod
    from kiro_crew.decisions import capability
    from kiro_crew.platform import governance_profiles
    from kiro_crew.platform.governance import Decision

    rows: list[dict] = []
    fake = MagicMock()
    fake.log_governance_decision = lambda **kw: rows.append(kw)
    monkeypatch.setattr(sel_mod, "sel", lambda: fake)

    def permits(scope, item, **_kw):
        return Decision(not item.startswith("endpoints:"), "test ceiling", rule="default")

    monkeypatch.setattr(capability, "governance_permits", permits)
    monkeypatch.setattr(governance_profiles, "governance_permits", permits)
    return rows


class TestOfferProbeDoesNotAudit:
    @pytest.mark.asyncio
    async def test_get_writes_no_endpoints_row_under_a_refusing_ceiling(self, audit, monkeypatch):
        rows = _refusing_ceiling(monkeypatch)
        payload = await _get()
        assert (payload["hosted_permitted"], payload["sagemaker_permitted"]) == (False, False)
        assert [r for r in rows if r["item"].startswith("endpoints:")] == []

    @pytest.mark.asyncio
    async def test_the_provider_put_still_writes_its_audit_row(
        self, audit, provider_config, monkeypatch
    ):
        rows = _refusing_ceiling(monkeypatch)
        status, body = await _put(GOOD)
        assert status == 403 and body["code"] == "decisions_capability_denied"
        written = [r for r in rows if r["item"].startswith("endpoints:")]
        assert written and all(r["outcome"] == "denied" for r in written)

    def test_the_unaudited_answer_is_the_audited_answer(self, monkeypatch):
        from kiro_crew.decisions import capability

        rows = _refusing_ceiling(monkeypatch)
        url = "https://runtime.sagemaker.us-west-2.amazonaws.com/endpoints/a/invocations"
        quiet = capability.targets_denied(point=None, model=None, endpoint=url, audit=False)
        assert rows == []
        loud = capability.targets_denied(point=None, model=None, endpoint=url)
        assert quiet is loud is True
        assert rows


class TestBotocoreRegionsAreComputedOnce:
    def test_a_second_read_does_not_build_another_session(self, monkeypatch):
        from kiro_crew.dashboard.handlers import decisions_sagemaker as ds

        calls: list[int] = []
        monkeypatch.setattr(ds, "_read_botocore_regions", lambda: calls.append(1) or ("ca-west-1",))
        assert ds._botocore_regions() == ds._botocore_regions() == ("ca-west-1",)
        assert len(calls) == 1

    def test_a_failed_read_is_not_cached(self, monkeypatch):
        from kiro_crew.dashboard.handlers import decisions_sagemaker as ds

        answers = iter([(), ("ca-west-1",)])
        monkeypatch.setattr(ds, "_read_botocore_regions", lambda: next(answers))
        assert ds._botocore_regions() == ()
        assert ds._botocore_regions() == ("ca-west-1",)

    @pytest.mark.asyncio
    async def test_one_settings_read_builds_at_most_one_session(self, audit, monkeypatch):
        from kiro_crew.dashboard.handlers import decisions_sagemaker as ds

        calls: list[int] = []
        monkeypatch.setattr(ds, "_read_botocore_regions", lambda: calls.append(1) or ("ca-west-1",))
        _ceiling(monkeypatch, "*")
        await _get()
        await _get()
        assert len(calls) == 1


def _seed_endpoint(endpoint: str) -> None:
    from kiro_crew.config.loader import config_path, update_config_locked

    def _seed(data):
        data["decisions"] = {"provider": {"endpoint": endpoint} if endpoint else {}}
        return data

    update_config_locked(config_path(), mutate=_seed)


def _configured(monkeypatch, endpoint: str) -> None:
    """Pin the endpoint the consent route reads from the live config."""
    from kiro_crew.decisions import gate

    monkeypatch.setattr(
        gate, "configured_endpoint", lambda config=None: endpoint or DEFAULT_ENDPOINT
    )


async def _consent(body):
    from kiro_crew.dashboard.handlers.decisions import api_decisions_consent_put

    resp = await api_decisions_consent_put(_request(body=body))
    return resp.status, json.loads(resp.text)


class TestConsentAsksTheEndpointsList:
    @pytest.mark.asyncio
    async def test_consent_on_is_refused_for_a_refused_hosted_host(
        self, audit, keystone, monkeypatch
    ):
        _ceiling(monkeypatch, *SAGEMAKER_ONLY)
        _configured(monkeypatch, "")
        status, body = await _consent({"enabled": True, "endpoint": DEFAULT_ENDPOINT})
        assert status == 403 and body["code"] == "decisions_capability_denied"
        assert not keystone.exists()
        assert audit[-1]["outcome"] == "denied"

    @pytest.mark.asyncio
    async def test_consent_on_is_refused_for_a_refused_sagemaker_regional_host(
        self, audit, keystone, monkeypatch
    ):
        asked = _ceiling(monkeypatch, "runtime.sagemaker.eu-west-1.amazonaws.com", "127.0.0.1")
        _configured(monkeypatch, GOOD_ADDRESS)
        status, body = await _consent({"enabled": True, "endpoint": GOOD_ADDRESS})
        assert status == 403 and body["code"] == "decisions_capability_denied"
        assert not keystone.exists()
        assert asked == [
            "https://runtime.sagemaker.us-west-2.amazonaws.com/endpoints/decision-model-a/invocations"
        ]

    @pytest.mark.asyncio
    async def test_a_permitted_host_still_consents(self, audit, keystone, monkeypatch):
        _ceiling(monkeypatch, "runtime.sagemaker.us-west-2.amazonaws.com")
        _configured(monkeypatch, GOOD_ADDRESS)
        status, _ = await _consent({"enabled": True, "endpoint": GOOD_ADDRESS})
        assert status == 200 and keystone.exists()

    @pytest.mark.asyncio
    async def test_a_local_preset_is_never_checked(self, audit, keystone, monkeypatch):
        from kiro_crew.decisions import local_models

        asked = _ceiling(monkeypatch)
        local = local_models.endpoint_for(8104)
        _configured(monkeypatch, local)
        status, _ = await _consent({"enabled": True, "endpoint": local})
        assert status == 200
        assert asked == []

    @pytest.mark.asyncio
    async def test_consent_off_is_always_allowed(self, audit, keystone, monkeypatch):
        asked = _ceiling(monkeypatch)
        _configured(monkeypatch, "")
        status, _ = await _consent({"enabled": False})
        assert status == 200
        assert asked == []


class TestMalformedAddressConsentFailsClosed:
    @pytest.mark.asyncio
    async def test_an_underscore_name_gets_the_audited_403_not_a_500(
        self, audit, keystone, monkeypatch
    ):
        bad = "sagemaker://my_endpoint?region=us-east-1"
        _ceiling(monkeypatch, "*")
        _configured(monkeypatch, bad)
        status, body = await _consent({"enabled": True, "endpoint": bad})
        assert status == 403 and body["code"] == "decisions_capability_denied"
        assert not keystone.exists()
        assert audit[-1]["outcome"] == "denied"


class TestRefusedDefaultReadsAsNoModel:
    @pytest.mark.asyncio
    @pytest.mark.parametrize("endpoint", [DEFAULT_ENDPOINT, ""], ids=["hosted-default", "unset"])
    async def test_get_reports_none_and_leaves_the_config_alone(
        self, endpoint, audit, provider_config, monkeypatch
    ):
        _ceiling(monkeypatch, *SAGEMAKER_ONLY)
        _seed_endpoint(endpoint)
        before = provider_config()
        payload = await _get()
        assert payload["active"] == "none"
        assert payload["configured_endpoint"] == DEFAULT_ENDPOINT
        assert provider_config() == before

    @pytest.mark.asyncio
    async def test_a_permitted_default_stays_hosted(self, audit, monkeypatch):
        _seed_endpoint("")
        assert (await _get())["active"] == "jev"

    @pytest.mark.asyncio
    async def test_a_sagemaker_address_keeps_its_own_reading(self, audit, monkeypatch):
        _ceiling(monkeypatch, *SAGEMAKER_ONLY)
        _seed_endpoint(GOOD_ADDRESS)
        assert (await _get())["active"] == "sagemaker"

    @pytest.mark.asyncio
    async def test_a_sagemaker_address_in_a_refused_region_reads_none_though_another_is_permitted(
        self, audit, provider_config, monkeypatch
    ):
        _ceiling(monkeypatch, "runtime.sagemaker.eu-west-1.amazonaws.com")
        _seed_endpoint(GOOD_ADDRESS)
        before = provider_config()
        payload = await _get()
        assert payload["sagemaker_permitted"] is True
        assert payload["configured_refused"] is True
        assert payload["active"] == "none"
        assert provider_config() == before

    @pytest.mark.asyncio
    async def test_a_refused_sagemaker_address_reads_none_and_leaves_the_config_alone(
        self, audit, provider_config, monkeypatch
    ):
        _ceiling(monkeypatch, "127.0.0.1")
        _seed_endpoint(GOOD_ADDRESS)
        before = provider_config()
        payload = await _get()
        assert payload["active"] == "none"
        assert payload["configured_endpoint"] == GOOD_ADDRESS
        assert provider_config() == before


class TestConnectionTest:
    async def _test(self, body=None, **kw):
        from kiro_crew.dashboard.handlers.decisions_sagemaker import (
            api_decisions_provider_sagemaker_test,
        )

        resp = await api_decisions_provider_sagemaker_test(
            _request(body=GOOD if body is None else body, **kw)
        )
        return resp.status, json.loads(resp.text)

    @pytest.mark.asyncio
    async def test_one_fixed_question_reaches_the_endpoint_and_nothing_is_written(
        self, audit, provider_config, not_denied, monkeypatch
    ):
        sent: list = []

        def fake_invoke(target, body, timeout_ms, **_):
            sent.append((target, body, timeout_ms))
            return {"answers": {"connection_test": {"type": "noul", "noul": 0.9}}}, "rid-1"

        monkeypatch.setattr(impl_sagemaker, "invoke_sync", fake_invoke)
        status, payload = await self._test()
        assert status == 200
        assert payload["ok"] is True and payload["error"] == "" and payload["latency_ms"] >= 0
        [(target, body, timeout_ms)] = sent
        assert (target.endpoint_name, target.region, target.profile) == (
            "decision-model-a",
            "us-west-2",
            "",
        )
        assert list(body["questions"]) == ["connection_test"]
        assert timeout_ms == 10000
        assert provider_config() == {}
        row = [r for r in audit if r["operation"] == "decisions_provider_sagemaker_test"][-1]
        assert row["outcome"] == "allowed" and "sagemaker=decision-model-a" in row["resources"]

    @pytest.mark.asyncio
    @pytest.mark.parametrize(
        ("error", "kind"),
        [
            (
                impl_sagemaker.SageMakerCredentialsError(
                    "AWS refused the credentials (ExpiredTokenException)"
                ),
                "credentials",
            ),
            (
                impl_sagemaker.SageMakerEndpointNotFound(
                    "no SageMaker endpoint by that name in that region"
                ),
                "not_found",
            ),
            (impl_sagemaker.SageMakerTimeout("no answer within the decision budget"), "timeout"),
            (
                impl_sagemaker.SageMakerModelError("model container answered with status 500", 500),
                "model_error",
            ),
        ],
        ids=["credentials", "not-found", "timeout", "model-error"],
    )
    async def test_a_failure_is_reported_as_a_fixed_word(
        self, error, kind, audit, not_denied, monkeypatch
    ):
        def fake_invoke(*_a, **_k):
            raise error

        monkeypatch.setattr(impl_sagemaker, "invoke_sync", fake_invoke)
        status, payload = await self._test()
        assert status == 200
        assert (payload["ok"], payload["error"], payload["message"]) == (False, kind, str(error))
        row = [r for r in audit if r["operation"] == "decisions_provider_sagemaker_test"][-1]
        assert (row["outcome"], row["error"]) == ("error", kind)

    @pytest.mark.asyncio
    async def test_an_answer_that_is_not_a_system_one_answer_quotes_nothing(
        self, audit, not_denied, monkeypatch
    ):
        secret = "do-not-echo-this-value"
        monkeypatch.setattr(
            impl_sagemaker, "invoke_sync", lambda *a, **k: ({"answers": {secret: secret}}, None)
        )
        status, payload = await self._test()
        assert (status, payload["ok"], payload["error"]) == (200, False, "protocol")
        assert secret not in json.dumps(payload)

    @pytest.mark.asyncio
    async def test_an_expired_sso_login_is_a_credentials_failure(
        self, audit, not_denied, monkeypatch
    ):
        from botocore.exceptions import UnauthorizedSSOTokenError

        class _Client:
            def invoke_endpoint(self, **_kwargs):
                raise UnauthorizedSSOTokenError()

        monkeypatch.setattr(impl_sagemaker, "_make_client", lambda *_a: _Client())
        impl_sagemaker._clear_clients()
        try:
            status, payload = await self._test()
        finally:
            impl_sagemaker._clear_clients()
        assert (status, payload["ok"], payload["error"]) == (200, False, "credentials")
        assert "UnauthorizedSSOTokenError" in payload["message"]
        row = [r for r in audit if r["operation"] == "decisions_provider_sagemaker_test"][-1]
        assert (row["outcome"], row["error"]) == ("error", "credentials")

    @pytest.mark.asyncio
    async def test_an_unexpected_error_is_failed_not_protocol(self, audit, not_denied, monkeypatch):
        secret = "do-not-echo-this-value"

        def fake_invoke(*_a, **_k):
            raise RuntimeError(secret)

        monkeypatch.setattr(impl_sagemaker, "invoke_sync", fake_invoke)
        status, payload = await self._test()
        assert (status, payload["ok"], payload["error"]) == (200, False, "failed")
        assert secret not in json.dumps(payload)

    @pytest.mark.asyncio
    async def test_the_test_is_refused_where_policy_refuses_the_endpoint(self, audit, monkeypatch):
        from kiro_crew.decisions import capability

        asked: list = []
        monkeypatch.setattr(capability, "is_decisions_denied", lambda *a, **k: False)

        def targets(*_a, point, model, endpoint):
            asked.append((point, endpoint))
            return True

        monkeypatch.setattr(capability, "targets_denied", targets)
        monkeypatch.setattr(impl_sagemaker, "invoke_sync", lambda *a, **k: pytest.fail("sent"))
        status, payload = await self._test()
        assert status == 403 and payload["code"] == "decisions_capability_denied"
        assert asked == [
            (
                None,
                "https://runtime.sagemaker.us-west-2.amazonaws.com/endpoints/decision-model-a/invocations",
            )
        ]

    @pytest.mark.asyncio
    async def test_a_bad_field_and_a_non_owner_are_refused_before_anything_is_sent(
        self, audit, not_denied, monkeypatch
    ):
        monkeypatch.setattr(impl_sagemaker, "invoke_sync", lambda *a, **k: pytest.fail("sent"))
        status, payload = await self._test({**GOOD, "region": "us-west-2&x="})
        assert (status, payload["field"]) == (400, "region")
        status, _ = await self._test({"preset": "jev"})
        assert status == 400
        status, _ = await self._test(user="someone-else")
        assert status == 403
