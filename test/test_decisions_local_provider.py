"""Local System One models: the preset catalog, the loopback test, and the provider route.

Three properties are pinned here:

* only a LITERAL loopback address counts as local, because the client withholds the
  Jev key from a local endpoint and a name or a user-info trick must not earn that;
* the provider route writes an endpoint it BUILT, from a preset id and a port, and
  refuses every body that could smuggle a URL in;
* a switch carries a standing consent across only to a local endpoint -- switching
  back to hosted Jev, the direction that starts egress, never inherits one.
"""

from __future__ import annotations

import json
from unittest.mock import AsyncMock, MagicMock

import pytest

from kiro_crew.config.sections import DECISION_PROVIDER_ENDPOINT_DEFAULT as DEFAULT_ENDPOINT
from kiro_crew.config.sections import DECISION_PROVIDER_MODEL_DEFAULT as DEFAULT_MODEL
from kiro_crew.decisions import consent, local_models

# ---------------------------------------------------------------------------
# The loopback test
# ---------------------------------------------------------------------------


class TestIsLoopbackEndpoint:
    @pytest.mark.parametrize(
        "endpoint",
        [
            "http://127.0.0.1:8102/v1/systemone",
            "http://127.9.9.9:8102/v1/systemone",
            "https://127.0.0.1:8443/v1/systemone",
            "http://[::1]:8104/v1/systemone",
            "http://[::ffff:127.0.0.1]:8102/v1/systemone",
            "http://[::ffff:7f00:1]:8102/v1/systemone",
            "http://127.1:8102/v1/systemone",
            "http://0x7f.1:8102/v1/systemone",
            "http://2130706433:8102/v1/systemone",
            "http://0177.0.0.1:8102/v1/systemone",
            "http://127.0.0.1/v1/systemone",
            "  http://127.0.0.1:8102/v1/systemone  ",
            # IDNA-normalised by yarl, which is what aiohttp dials.
            "http://127\u30020\u30020\u30021:8102/v1/systemone",
            "http://127\uff0e0\uff0e0\uff0e1:8102/v1/systemone",
            "http://127\uff610\uff610\uff611:8102/v1/systemone",
            "http://\u2460\u2461\u2466.0.0.1:8102/v1/systemone",
        ],
    )
    def test_a_literal_loopback_address_is_local(self, endpoint):
        assert local_models.is_loopback_endpoint(endpoint) is True

    def test_mapped_loopback_is_local_even_where_ipaddress_says_otherwise(self, monkeypatch):
        """Older Pythons report ``::ffff:127.0.0.1`` as not loopback; the guard must not."""
        import ipaddress

        monkeypatch.setattr(
            ipaddress.IPv6Address,
            "is_loopback",
            property(lambda self: int(self) == 1),
        )
        assert (
            local_models.is_loopback_endpoint("http://[::ffff:127.0.0.1]:8102/v1/systemone") is True
        )

    @pytest.mark.parametrize(
        "endpoint",
        ["http://0.0.0.0:8102/v1/systemone", "http://[::]:8102/v1/systemone"],
        ids=["any-address-v4", "any-address-v6"],
    )
    def test_the_unspecified_address_dials_this_machine_and_is_local(self, endpoint):
        assert local_models.is_loopback_endpoint(endpoint) is True

    @pytest.mark.parametrize(
        "endpoint",
        ["http://[127.0.0.1:8102/v1/systemone", "http://[::1:8102/v1/systemone"],
        ids=["unbalanced-bracket-v4", "unbalanced-bracket-v6"],
    )
    def test_a_malformed_hand_written_address_is_custom_not_a_crash(self, endpoint):
        assert local_models.active_id(endpoint, None) == "custom"

    @pytest.mark.parametrize(
        "endpoint",
        [
            DEFAULT_ENDPOINT,
            "http://localhost:8102/v1/systemone",
            "http://127.0.0.1:8102@evil.example:80/v1/systemone",
            "http://user@127.0.0.1:8102/v1/systemone",
            "ftp://127.0.0.1:8102/v1/systemone",
            "http://10.0.0.5:8102/v1/systemone",
            "http://[::ffff:10.0.0.5]:8102/v1/systemone",
            "http://10.1:8102/v1/systemone",
            "http://deadbeef:8102/v1/systemone",
            "http://127.0.0.1:99999/v1/systemone",
            "127.0.0.1:8102",
            "",
            None,
            8102,
        ],
        ids=[
            "hosted-jev",
            "a-name",
            "userinfo-host-swap",
            "userinfo",
            "not-http",
            "private-not-loopback",
            "mapped-private-not-loopback",
            "shorthand-private-not-loopback",
            "hex-looking-name",
            "port-out-of-range",
            "no-scheme",
            "empty",
            "none",
            "not-a-string",
        ],
    )
    def test_anything_else_is_not(self, endpoint):
        assert local_models.is_loopback_endpoint(endpoint) is False


class TestEndpointFor:
    def test_builds_the_systemone_url_on_ipv4_loopback(self):
        assert local_models.endpoint_for(8102) == "http://127.0.0.1:8102/v1/systemone"

    @pytest.mark.parametrize("port", [0, 80, 1023, 65536, -1, True, "8102", 8102.0, None])
    def test_refuses_a_port_outside_the_range_or_not_an_int(self, port):
        with pytest.raises(ValueError):
            local_models.endpoint_for(port)

    def test_every_built_endpoint_is_local(self):
        for m in local_models.LOCAL_MODELS:
            assert local_models.is_loopback_endpoint(local_models.endpoint_for(m.default_port))


class TestCatalog:
    def test_ids_are_unique_and_none_is_the_hosted_preset(self):
        ids = [m.id for m in local_models.LOCAL_MODELS]
        assert len(ids) == len(set(ids))
        assert local_models.PRESET_JEV not in ids

    def test_thresholds_descend_so_the_first_met_is_the_largest(self):
        """The card recommends the FIRST preset whose threshold the machine meets."""
        thresholds = [m.recommended_total_ram_gb for m in local_models.LOCAL_MODELS]
        assert thresholds == sorted(thresholds, reverse=True)

    def test_a_threshold_is_four_times_the_measured_peak(self):
        """A preset recommended on a machine it would fill would starve other sessions."""
        for m in local_models.LOCAL_MODELS:
            assert m.recommended_total_ram_gb >= 4 * m.peak_ram_gb
            assert m.recommended_total_ram_gb < 4 * m.peak_ram_gb + 1

    def test_the_payload_carries_the_threshold(self):
        for m in local_models.LOCAL_MODELS:
            assert (
                local_models.as_payload(m)["recommended_total_ram_gb"] == m.recommended_total_ram_gb
            )

    def test_every_model_id_is_one_the_client_will_send(self):
        from kiro_crew.decisions.types import is_model_id

        for m in local_models.LOCAL_MODELS:
            assert is_model_id(m.model), m.model

    def test_every_launcher_and_lock_ships_beside_the_module(self):
        from kiro_crew.decisions.local_runtime import SERVERS_DIR

        for m in local_models.LOCAL_MODELS:
            assert (SERVERS_DIR / m.launcher).is_file(), m.launcher
            assert (SERVERS_DIR / m.requirements).is_file(), m.requirements

    def test_every_lock_pins_every_requirement_exactly(self):
        """A range would let a later release change what runs under the card's numbers."""
        from kiro_crew.decisions.local_runtime import SERVERS_DIR

        for m in local_models.LOCAL_MODELS:
            for line in (SERVERS_DIR / m.requirements).read_text(encoding="utf-8").splitlines():
                line = line.strip()
                if not line or line.startswith("#"):
                    continue
                # An environment marker after ";" narrows where a pin applies, not its version.
                spec = line.split(";", 1)[0]
                assert "==" in spec or "/archive/" in spec, line
                assert not any(op in spec for op in (">", "<", "~=", "!=")), line

    def test_every_weight_file_is_pinned_and_served_from_the_model_cdn(self):
        for m in local_models.LOCAL_MODELS:
            assert m.files, m.id
            for f in m.files:
                assert len(f.sha256) == 64 and int(f.sha256, 16) >= 0, f.path
                assert f.size > 0
                assert not f.path.startswith("/") and ".." not in f.path.split("/")
                if f.source:
                    # A mirrored base model: served from its own pinned upstream directory.
                    parts = f.source.split("/")
                    assert len(parts) >= 3 and ".." not in parts and len(parts[1]) == 40, f.source
                    assert m.file_url(f) == f"{local_models.MODEL_CDN}/{f.source}"
                else:
                    assert m.file_url(f) == f"{local_models.MODEL_CDN}/{m.id}/{m.revision}/{f.path}"
            paths = {f.path for f in m.files}
            assert paths & {"LICENSE", "LICENSE.md"}, "the license travels with the weights"
            assert "NOTICE" in paths
            # A mirrored upstream carries its own license and notice beside its files.
            for src_dir in {f.source.rsplit("/", 1)[0] for f in m.files if f.source}:
                served = {
                    f.source.rsplit("/", 1)[1]
                    for f in m.files
                    if f.source.startswith(src_dir + "/")
                }
                assert {"LICENSE", "NOTICE"} <= served, src_dir
            assert m.download_bytes == sum(f.size for f in m.files)

    def test_the_payload_carries_no_internal_detail(self):
        for m in local_models.LOCAL_MODELS:
            payload = local_models.as_payload(m)
            assert not {"files", "launcher", "requirements", "revision"} & set(payload)
            assert payload["download_bytes"] == m.download_bytes

    def test_the_quality_numbers_are_the_measured_ratios(self):
        """Correct answers matched against Jev's 200 of 231 (81 of 111 on hard)."""
        by_id = {m.id: m for m in local_models.LOCAL_MODELS}
        assert by_id["plumb-4b"].jev_relative_pct == round(100 * 206 / 200)
        assert by_id["plumb-4b"].hard_relative_pct == round(100 * 88 / 81)
        assert by_id["laya"].jev_relative_pct == round(100 * 134 / 200)
        assert by_id["laya"].hard_relative_pct == round(100 * 38 / 81)

    def test_get_accepts_only_a_known_id(self):
        assert local_models.get("laya").id == "laya"
        assert local_models.get("nope") is None
        assert local_models.get(None) is None


class TestActiveId:
    def test_the_hosted_default_is_jev(self):
        assert local_models.active_id(DEFAULT_ENDPOINT, DEFAULT_MODEL) == "jev"

    def test_a_local_endpoint_with_a_preset_model_is_that_preset(self):
        endpoint = local_models.endpoint_for(9000)
        assert local_models.active_id(endpoint, "plumb-4b") == "plumb-4b"
        assert local_models.active_id(endpoint, "english") == "laya"

    def test_a_hand_written_loopback_spelling_of_a_preset_is_custom(self):
        """Only the address the route builds is a preset; ``127.1`` or a query string
        naming the same server is hand-written and keeps the custom guidance."""
        preset = local_models.LOCAL_MODELS[0]
        built = local_models.endpoint_for(preset.default_port)
        assert local_models.active_id(built, preset.model) == preset.id
        for hand in (
            built.replace("127.0.0.1", "127.1"),
            built + "?x=1",
            built.replace("http://", "https://"),
        ):
            assert local_models.active_id(hand, preset.model) == "custom", hand

    def test_anything_else_is_custom(self):
        assert local_models.active_id("https://proxy.example/v1/systemone", "x") == "custom"
        assert local_models.active_id(local_models.endpoint_for(9000), "other") == "custom"


# ---------------------------------------------------------------------------
# The provider route
# ---------------------------------------------------------------------------


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
def config_file():
    """The config.json the pinned data home resolves to, and a reader for its provider."""
    from kiro_crew.config.loader import config_path

    path = config_path()

    def _provider() -> dict:
        data = json.loads(path.read_text(encoding="utf-8")) if path.exists() else {}
        return data.get("decisions", {}).get("provider", {})

    return path, _provider


class _FakeRuntime:
    """Stands in for the gateway's runtime: records what the route asked it to do."""

    def __init__(self):
        self.calls: list[tuple] = []
        self.state = {
            "preset": "",
            "state": "idle",
            "port": 0,
            "bytes_done": 0,
            "bytes_total": 0,
            "error": "",
        }
        self.installed: set[str] = set()

    def status(self):
        return dict(self.state)

    def installed_ids(self):
        return sorted(self.installed)

    def activate(self, m, port):
        self.calls.append(("activate", m.id, port))
        self.state.update(preset=m.id, state="downloading", port=port)

    def deactivate(self, *, wait=False):
        self.calls.append(("deactivate",))
        self.state.update(preset="", state="idle", port=0)

    #: A stopped worker still finishing a step on the preset's files.
    busy: set[str] = set()

    def remove(self, m):
        self.calls.append(("remove", m.id))
        if m.id in self.busy:
            return False
        self.installed.discard(m.id)
        return True


@pytest.fixture(autouse=True)
def runtime(monkeypatch):
    """No test here may download a model or start a server: the route drives a fake,
    and the port the route would pick is the preset's own."""
    from kiro_crew.decisions import local_runtime

    fake = _FakeRuntime()
    monkeypatch.setattr(local_runtime, "get_runtime", lambda: fake)
    monkeypatch.setattr(local_runtime, "free_port", lambda preferred: preferred)
    return fake


@pytest.fixture
def not_denied(monkeypatch):
    from kiro_crew.decisions import capability

    monkeypatch.setattr(capability, "is_decisions_denied", lambda *a, **k: False)


class TestProviderRouteOwnerOnly:
    @pytest.mark.asyncio
    @pytest.mark.parametrize("verb", ["get", "put"])
    async def test_a_non_owner_is_refused(self, verb, audit, config_file):
        from kiro_crew.dashboard.handlers import decisions as mod

        handler = (
            mod.api_decisions_provider_get if verb == "get" else mod.api_decisions_provider_put
        )
        resp = await handler(_request(user="someone-else", body={"preset": "laya"}))
        assert resp.status == 403
        assert config_file[1]() == {}, "a refused write wrote nothing"

    @pytest.mark.asyncio
    async def test_an_app_token_is_refused(self, audit, config_file):
        from kiro_crew.dashboard.handlers.decisions import api_decisions_provider_put

        resp = await api_decisions_provider_put(_request(app="some-app", body={"preset": "laya"}))
        assert resp.status == 403


class TestProviderRouteGet:
    @pytest.mark.asyncio
    async def test_lists_every_preset_and_reports_hosted_jev_by_default(self, audit):
        from kiro_crew.dashboard.handlers.decisions import api_decisions_provider_get

        resp = await api_decisions_provider_get(_request())
        payload = json.loads(resp.text)
        assert resp.status == 200
        assert [p["id"] for p in payload["presets"]] == [m.id for m in local_models.LOCAL_MODELS]
        assert payload["active"] == "jev"
        assert payload["configured_endpoint"] == DEFAULT_ENDPOINT
        assert payload["loopback"] is False
        assert payload["runtime"]["state"] == "idle"
        assert all(p["installed"] is False for p in payload["presets"])

    @pytest.mark.asyncio
    async def test_reports_what_is_downloaded_and_what_the_runtime_is_doing(self, audit, runtime):
        from kiro_crew.dashboard.handlers.decisions import api_decisions_provider_get

        runtime.installed = {"laya"}
        runtime.state.update(preset="plumb-4b", state="downloading", bytes_done=5, bytes_total=10)
        payload = json.loads((await api_decisions_provider_get(_request())).text)
        assert {p["id"]: p["installed"] for p in payload["presets"]} == {
            "plumb-4b": False,
            "strands-decider-2b": False,
            "laya": True,
        }
        assert payload["runtime"]["state"] == "downloading"
        assert payload["runtime"]["bytes_done"] == 5


class TestLocalModelStatus:
    @pytest.mark.asyncio
    async def test_reports_the_runtime_and_writes_no_audit_row(self, audit, runtime):
        from kiro_crew.dashboard.handlers.decisions import api_decisions_local_model_status

        runtime.installed = {"laya"}
        runtime.state.update(preset="laya", state="downloading", bytes_done=3, bytes_total=9)
        resp = await api_decisions_local_model_status(_request())
        assert resp.status == 200
        assert json.loads(resp.text) == {"runtime": runtime.status(), "installed": ["laya"]}
        assert audit == [], "polled every two seconds; an audit row each time would bury the trail"

    @pytest.mark.asyncio
    async def test_a_non_owner_is_refused(self, audit, runtime):
        from kiro_crew.dashboard.handlers.decisions import api_decisions_local_model_status

        resp = await api_decisions_local_model_status(_request(user="someone-else"))
        assert resp.status == 403


class TestProviderRoutePut:
    @pytest.mark.asyncio
    @pytest.mark.parametrize(
        "body",
        [
            {"preset": "laya", "endpoint": "https://evil.example/v1/systemone"},
            {"preset": "laya", "url": "http://127.0.0.1:1/v1/systemone"},
            {"preset": "jev", "port": 8102},
            {"preset": "laya", "port": 9001},
            {"preset": "nope"},
            {"preset": None},
            {},
            ["laya"],
            "laya",
        ],
        ids=[
            "an-endpoint",
            "a-url",
            "port-on-hosted",
            "port-on-local",
            "unknown-preset",
            "null-preset",
            "empty",
            "a-list",
            "a-string",
        ],
    )
    async def test_every_body_that_is_not_a_bare_preset_is_refused(
        self, body, audit, config_file, runtime
    ):
        from kiro_crew.dashboard.handlers.decisions import api_decisions_provider_put

        resp = await api_decisions_provider_put(_request(body=body))
        assert resp.status == 400
        assert json.loads(resp.text)["code"] == "decisions_provider_invalid_body"
        assert config_file[1]() == {}
        assert runtime.calls == [], "a refused body starts and stops nothing"

    @pytest.mark.asyncio
    async def test_an_unparseable_body_is_refused_and_audited(self, audit, config_file):
        from kiro_crew.dashboard.handlers.decisions import api_decisions_provider_put

        req = _request()
        req.json = AsyncMock(side_effect=json.JSONDecodeError("bad", "{", 0))
        resp = await api_decisions_provider_put(req)
        assert resp.status == 400
        assert json.loads(resp.text)["code"] == "invalid_json"
        assert config_file[1]() == {}
        rows = [r for r in audit if r["operation"] == "decisions_provider_put"]
        assert rows and rows[-1]["outcome"] == "denied"
        assert rows[-1]["error"] == "invalid_json"

    @pytest.mark.asyncio
    async def test_a_local_preset_writes_the_endpoint_it_built(
        self, audit, config_file, keystone, not_denied
    ):
        from kiro_crew.dashboard.handlers.decisions import api_decisions_provider_put

        resp = await api_decisions_provider_put(_request(body={"preset": "plumb-4b"}))
        assert resp.status == 200
        assert config_file[1]() == {
            "endpoint": "http://127.0.0.1:8102/v1/systemone",
            "model": "plumb-4b",
            "timeout_ms": local_models.get("plumb-4b").timeout_ms,
        }
        assert json.loads(resp.text)["active"] == "plumb-4b"

    @pytest.mark.asyncio
    async def test_choosing_a_local_preset_starts_it_on_the_written_port(
        self, audit, config_file, keystone, not_denied, runtime
    ):
        from kiro_crew.dashboard.handlers.decisions import api_decisions_provider_put

        resp = await api_decisions_provider_put(_request(body={"preset": "laya"}))
        assert runtime.calls == [("activate", "laya", 8104)]
        assert json.loads(resp.text)["runtime"]["state"] == "downloading"

    @pytest.mark.asyncio
    async def test_a_held_default_port_moves_the_preset_to_a_free_one(
        self, audit, config_file, keystone, not_denied, runtime, monkeypatch
    ):
        from kiro_crew.dashboard.handlers.decisions import api_decisions_provider_put
        from kiro_crew.decisions import local_runtime

        monkeypatch.setattr(local_runtime, "free_port", lambda preferred: 40123)
        await api_decisions_provider_put(_request(body={"preset": "laya"}))
        assert config_file[1]()["endpoint"] == local_models.endpoint_for(40123)
        assert runtime.calls == [("activate", "laya", 40123)]

    @pytest.mark.asyncio
    async def test_choosing_the_running_preset_again_keeps_its_port(
        self, audit, config_file, keystone, not_denied, runtime, monkeypatch
    ):
        """Its own server holds the port, so a free-port probe would move it away."""
        from kiro_crew.dashboard.handlers.decisions import api_decisions_provider_put
        from kiro_crew.decisions import local_runtime

        runtime.state.update(preset="laya", state="running", port=40500)
        monkeypatch.setattr(local_runtime, "free_port", lambda preferred: 1 / 0)
        await api_decisions_provider_put(_request(body={"preset": "laya"}))
        assert runtime.calls == [("activate", "laya", 40500)]

    @pytest.mark.asyncio
    async def test_a_retry_after_an_error_probes_for_a_port_again(
        self, audit, config_file, keystone, not_denied, runtime, monkeypatch
    ):
        """The error may be that another program took the port, so it is not reused."""
        from kiro_crew.dashboard.handlers.decisions import api_decisions_provider_put
        from kiro_crew.decisions import local_runtime

        runtime.state.update(
            preset="laya", state="error", port=40500, error="port 40500 is already in use"
        )
        monkeypatch.setattr(local_runtime, "free_port", lambda preferred: 40600)
        await api_decisions_provider_put(_request(body={"preset": "laya"}))
        assert runtime.calls == [("activate", "laya", 40600)]

    @pytest.mark.asyncio
    async def test_choosing_hosted_jev_stops_the_local_server(
        self, audit, config_file, keystone, not_denied, runtime
    ):
        from kiro_crew.dashboard.handlers.decisions import api_decisions_provider_put

        await api_decisions_provider_put(_request(body={"preset": "laya"}))
        await api_decisions_provider_put(_request(body={"preset": "jev"}))
        assert runtime.calls[-1] == ("deactivate",)

    @pytest.mark.asyncio
    async def test_a_failed_write_starts_nothing(
        self, audit, config_file, keystone, not_denied, runtime, monkeypatch
    ):
        import kiro_crew.dashboard.handlers.decisions as mod

        def _boom(*_a, **_k):
            raise OSError("disk full")

        monkeypatch.setattr(mod, "_write_provider", _boom)
        resp = await mod.api_decisions_provider_put(_request(body={"preset": "laya"}))
        assert resp.status == 500
        assert runtime.calls == []

    @pytest.mark.asyncio
    async def test_the_api_key_reference_is_left_as_it_was(
        self, audit, config_file, keystone, not_denied
    ):
        from kiro_crew.config.loader import update_config_locked
        from kiro_crew.dashboard.handlers.decisions import api_decisions_provider_put

        path, provider = config_file

        def _seed(data):
            data.setdefault("decisions", {})["provider"] = {"api_key": "secret://TYPESAFE_API_KEY"}
            return data

        update_config_locked(path, mutate=_seed)
        await api_decisions_provider_put(_request(body={"preset": "laya"}))
        assert provider()["api_key"] == "secret://TYPESAFE_API_KEY"

    @pytest.mark.asyncio
    async def test_hosted_jev_restores_the_shipped_defaults(
        self, audit, config_file, keystone, not_denied
    ):
        from kiro_crew.dashboard.handlers.decisions import api_decisions_provider_put

        await api_decisions_provider_put(_request(body={"preset": "laya"}))
        resp = await api_decisions_provider_put(_request(body={"preset": "jev"}))
        assert resp.status == 200
        assert config_file[1]() == {
            "endpoint": DEFAULT_ENDPOINT,
            "model": DEFAULT_MODEL,
            "timeout_ms": 1000,
        }

    @pytest.mark.asyncio
    async def test_the_write_is_audited_with_the_endpoint(
        self, audit, config_file, keystone, not_denied
    ):
        from kiro_crew.dashboard.handlers.decisions import api_decisions_provider_put

        await api_decisions_provider_put(_request(body={"preset": "laya"}))
        row = [r for r in audit if r["operation"] == "decisions_provider_put"][-1]
        assert row["outcome"] == "allowed"
        assert "endpoint=http://127.0.0.1:8104/v1/systemone" in row["resources"]


class TestConsentCarry:
    def _consent(self, keystone, endpoint):
        keystone.write_text(
            json.dumps({"enabled": True, "endpoint": endpoint, "tool_args": True}),
            encoding="utf-8",
        )

    @pytest.mark.asyncio
    async def test_a_standing_consent_follows_a_switch_to_a_local_preset(
        self, audit, config_file, keystone, not_denied
    ):
        from kiro_crew.dashboard.handlers.decisions import api_decisions_provider_put

        self._consent(keystone, DEFAULT_ENDPOINT)
        await api_decisions_provider_put(_request(body={"preset": "laya"}))
        state = consent.load_state()
        assert consent.consented_endpoint(state) == local_models.endpoint_for(8104)
        assert consent.consented_tool_args(state) is True, "a recorded scope is kept"

    @pytest.mark.asyncio
    async def test_switching_back_to_hosted_jev_never_inherits_consent(
        self, audit, config_file, keystone, not_denied
    ):
        from kiro_crew.dashboard.handlers.decisions import api_decisions_provider_put

        local = local_models.endpoint_for(8104)
        self._consent(keystone, local)
        await api_decisions_provider_put(_request(body={"preset": "jev"}))
        assert consent.consented_endpoint(consent.load_state()) == local
        assert consent.permits(DEFAULT_ENDPOINT, consent.load_state()) is False

    @pytest.mark.asyncio
    async def test_two_switches_never_interleave_write_and_carry(
        self, audit, config_file, keystone, not_denied, monkeypatch
    ):
        """Each switch's config write and consent carry land together, so the config
        and the keystone always end naming the same address."""
        import asyncio
        import time

        import kiro_crew.dashboard.handlers.decisions as mod
        from kiro_crew.dashboard.handlers.decisions import api_decisions_provider_put

        self._consent(keystone, DEFAULT_ENDPOINT)
        events: list[str] = []
        real_write, real_carry = mod._write_provider, mod._carry_consent

        def write(endpoint, *a, **k):
            events.append(f"write {endpoint}")
            return real_write(endpoint, *a, **k)

        def carry(endpoint):
            time.sleep(0.2)  # a slow carry is the window another switch would use
            events.append(f"carry {endpoint}")
            return real_carry(endpoint)

        monkeypatch.setattr(mod, "_write_provider", write)
        monkeypatch.setattr(mod, "_carry_consent", carry)
        await asyncio.gather(
            api_decisions_provider_put(_request(body={"preset": "laya"})),
            api_decisions_provider_put(_request(body={"preset": "plumb-4b"})),
        )
        assert [e.split()[0] for e in events] == ["write", "carry", "write", "carry"]
        assert events[0].split()[1] == events[1].split()[1]
        assert consent.consented_endpoint(consent.load_state()) == config_file[1]()["endpoint"]

    @pytest.mark.asyncio
    async def test_a_revocation_racing_the_switch_is_not_undone(
        self, audit, config_file, keystone, not_denied, monkeypatch
    ):
        """The keystone is re-read under the write lock, so a consent the owner turned
        off after this PUT last looked at it stays off."""
        from kiro_crew.dashboard.handlers.decisions import api_decisions_provider_put

        # What a stale read outside the lock would have seen: consent still on.
        stale = {"enabled": True, "endpoint": DEFAULT_ENDPOINT}
        monkeypatch.setattr(consent, "load_state", lambda: dict(stale))
        await api_decisions_provider_put(_request(body={"preset": "laya"}))
        assert consent.is_enabled(consent.read_state_strict()) is False

    @pytest.mark.asyncio
    async def test_no_consent_is_created_where_none_stood(
        self, audit, config_file, keystone, not_denied
    ):
        from kiro_crew.dashboard.handlers.decisions import api_decisions_provider_put

        await api_decisions_provider_put(_request(body={"preset": "laya"}))
        assert consent.is_enabled(consent.load_state()) is False

    @pytest.mark.asyncio
    async def test_a_failed_carry_still_reports_the_saved_provider(
        self, audit, config_file, keystone, not_denied, monkeypatch
    ):
        """The provider is written before consent is carried, so a keystone write
        that fails must not turn a saved switch into a "nothing changed" 500."""
        from kiro_crew.dashboard.handlers.decisions import api_decisions_provider_put

        self._consent(keystone, DEFAULT_ENDPOINT)

        def _refuse(*_a, **_k):
            raise consent.ConsentCorruptError("unreadable")

        monkeypatch.setattr(consent, "rebind_if_enabled", _refuse)
        resp = await api_decisions_provider_put(_request(body={"preset": "laya"}))
        assert resp.status == 200
        payload = json.loads(resp.text)
        assert payload["active"] == "laya"
        assert payload["loopback"] is True
        assert config_file[1]()["endpoint"] == local_models.endpoint_for(8104)
        assert consent.consented_endpoint(consent.load_state()) == DEFAULT_ENDPOINT
        errors = [r["error"] for r in audit if r["outcome"] == "error"]
        assert errors == ["consent_carry:ConsentCorruptError"]

    @pytest.mark.asyncio
    async def test_a_fleet_denial_stops_the_carry(self, audit, config_file, keystone, monkeypatch):
        from kiro_crew.dashboard.handlers.decisions import api_decisions_provider_put
        from kiro_crew.decisions import capability

        monkeypatch.setattr(capability, "is_decisions_denied", lambda *a, **k: True)
        self._consent(keystone, DEFAULT_ENDPOINT)
        resp = await api_decisions_provider_put(_request(body={"preset": "laya"}))
        assert resp.status == 403
        assert json.loads(resp.text)["code"] == "decisions_capability_denied"
        assert config_file[1]() == {}
        assert consent.consented_endpoint(consent.load_state()) == DEFAULT_ENDPOINT

    @pytest.mark.asyncio
    async def test_a_hosted_pin_still_lets_a_local_preset_take_over(
        self, audit, config_file, keystone, monkeypatch
    ):
        """A fleet that pins ``capabilities.decisions`` off still has a local preset,
        governed by ``capabilities.decisions_local``, that switches, consent and all."""
        from kiro_crew.dashboard.handlers.decisions import api_decisions_provider_put
        from kiro_crew.decisions import capability

        monkeypatch.setattr(
            capability, "is_decisions_denied", lambda *a, local=False, **k: not local
        )
        self._consent(keystone, DEFAULT_ENDPOINT)
        resp = await api_decisions_provider_put(_request(body={"preset": "laya"}))
        assert resp.status == 200
        payload = json.loads(resp.text)
        assert (payload["hosted_permitted"], payload["local_permitted"]) == (False, True)
        assert consent.consented_endpoint(consent.load_state()) == local_models.endpoint_for(8104)
        back = await api_decisions_provider_put(_request(body={"preset": "jev"}))
        assert back.status == 403
        assert config_file[1]()["endpoint"] == local_models.endpoint_for(8104)


class TestNoModel:
    """``{"preset": "none"}`` is the in-product way to stop a local model."""

    @pytest.mark.asyncio
    async def test_choosing_none_stops_the_server_and_names_no_model(
        self, audit, config_file, keystone, not_denied, runtime
    ):
        from kiro_crew.dashboard.handlers.decisions import api_decisions_provider_put

        await api_decisions_provider_put(_request(body={"preset": "laya"}))
        resp = await api_decisions_provider_put(_request(body={"preset": "none"}))
        assert resp.status == 200
        assert json.loads(resp.text)["active"] == "none"
        assert config_file[1]()["endpoint"] == local_models.ENDPOINT_NONE
        assert runtime.calls[-1] == ("deactivate",)

    @pytest.mark.asyncio
    async def test_none_is_allowed_when_the_fleet_denies_both_rows(
        self, audit, config_file, keystone, runtime, monkeypatch
    ):
        """The way out of a configured preset is never refused: it sends and runs nothing."""
        from kiro_crew.dashboard.handlers.decisions import api_decisions_provider_put
        from kiro_crew.decisions import capability

        monkeypatch.setattr(capability, "is_decisions_denied", lambda *a, **k: True)
        resp = await api_decisions_provider_put(_request(body={"preset": "none"}))
        assert resp.status == 200
        assert config_file[1]()["endpoint"] == local_models.ENDPOINT_NONE

    def test_the_gate_sends_nothing_with_no_model(self, keystone, not_denied):
        from types import SimpleNamespace

        from kiro_crew.decisions import gate

        keystone.write_text(
            json.dumps({"enabled": True, "endpoint": local_models.ENDPOINT_NONE}),
            encoding="utf-8",
        )
        provider = SimpleNamespace(endpoint=local_models.ENDPOINT_NONE, model="")
        cfg = SimpleNamespace(decisions=SimpleNamespace(provider=provider))
        assert gate._consented_for(cfg) is False

    def test_a_server_is_not_resumed_with_no_model(self, monkeypatch, runtime):
        from types import SimpleNamespace

        from kiro_crew.config import live
        from kiro_crew.decisions import local_runtime

        provider = SimpleNamespace(endpoint=local_models.ENDPOINT_NONE, model="")
        cfg = SimpleNamespace(decisions=SimpleNamespace(provider=provider))
        monkeypatch.setattr(live, "snapshot", lambda: cfg)
        assert local_runtime.resume_configured() == ""
        assert runtime.calls == []


class TestGateReadsTheRowOfTheConfiguredProvider:
    """The keystone read is the chokepoint every decision funnels through, so it is
    where a hosted pin must stop applying to a local preset."""

    @staticmethod
    def _config(endpoint: str, model: str):
        from types import SimpleNamespace

        provider = SimpleNamespace(endpoint=endpoint, model=model)
        return SimpleNamespace(decisions=SimpleNamespace(provider=provider))

    @pytest.mark.parametrize(
        ("endpoint", "model", "consented"),
        [
            (local_models.endpoint_for(8102), "plumb-4b", True),
            (DEFAULT_ENDPOINT, "jev-1", False),
            # Hand-written loopback: may be a tunnel to hosted Jev, so the hosted row.
            ("http://127.0.0.1:9000/proxy", "jev-1", False),
            # A preset's address the gateway does not run: whatever holds that port
            # is not ours, so the hosted row.
            (local_models.endpoint_for(8104), "english", False),
        ],
    )
    def test_a_hosted_pin_withdraws_everything_but_the_preset_this_gateway_runs(
        self, keystone, monkeypatch, runtime, endpoint, model, consented
    ):
        from kiro_crew.decisions import capability, gate

        runtime.state.update(preset="plumb-4b", state="running", port=8102)
        monkeypatch.setattr(
            capability, "is_decisions_denied", lambda *a, local=False, **k: not local
        )
        keystone.write_text(json.dumps({"enabled": True, "endpoint": endpoint}), encoding="utf-8")
        assert gate._consented_for(self._config(endpoint, model)) is consented

    def test_an_unattested_preset_address_answers_under_both_rows(
        self, keystone, monkeypatch, runtime
    ):
        """Local models pinned off, hosted Jev allowed: a server started by hand on the
        preset's port does not get decisions by borrowing the preset's address."""
        from kiro_crew.decisions import capability, gate

        endpoint = local_models.endpoint_for(8102)
        monkeypatch.setattr(capability, "is_decisions_denied", lambda *a, local=False, **k: local)
        keystone.write_text(json.dumps({"enabled": True, "endpoint": endpoint}), encoding="utf-8")
        assert gate._consented_for(self._config(endpoint, "plumb-4b")) is False

    @pytest.mark.parametrize("state", ["downloading", "installing", "starting", "error", "idle"])
    def test_nothing_is_sent_to_a_preset_address_until_its_server_is_attested(
        self, keystone, monkeypatch, runtime, not_denied, state
    ):
        """Consent carried and no fleet pin: while the preset is not RUNNING, whatever
        holds its port gets no decision payload."""
        from kiro_crew.decisions import gate

        endpoint = local_models.endpoint_for(8102)
        runtime.state.update(preset="plumb-4b", state=state, port=8102)
        keystone.write_text(json.dumps({"enabled": True, "endpoint": endpoint}), encoding="utf-8")
        assert gate._consented_for(self._config(endpoint, "plumb-4b")) is False
        runtime.state.update(state="running")
        assert gate._consented_for(self._config(endpoint, "plumb-4b")) is True


def _delete_request(preset_id, **kw):
    req = _request(**kw)
    req.path = f"/api/decisions/local-models/{preset_id}"
    req.match_info = {"id": preset_id}
    return req


class TestLocalModelDelete:
    @pytest.mark.asyncio
    async def test_refuses_while_a_stopped_worker_still_holds_the_files(self, audit, runtime):
        from kiro_crew.dashboard.handlers.decisions import api_decisions_local_model_delete

        runtime.installed = {"laya"}
        runtime.busy = {"laya"}
        resp = await api_decisions_local_model_delete(_delete_request("laya"))
        assert resp.status == 409
        assert json.loads(resp.text)["code"] == "decisions_local_model_in_use"

    @pytest.mark.asyncio
    async def test_a_delete_that_could_not_finish_is_an_audited_error(
        self, audit, runtime, monkeypatch
    ):
        from kiro_crew.dashboard.handlers.decisions import api_decisions_local_model_delete

        def _locked(m):
            raise PermissionError(13, "file in use", "/somewhere/model.safetensors")

        monkeypatch.setattr(runtime, "remove", _locked)
        resp = await api_decisions_local_model_delete(_delete_request("laya"))
        assert resp.status == 500
        assert json.loads(resp.text)["code"] == "decisions_local_model_delete_failed"
        row = [r for r in audit if r["operation"] == "decisions_local_model_delete"][-1]
        assert row["outcome"] == "error" and row["error"] == "PermissionError"
        assert "/somewhere" not in json.dumps(row) + resp.text

    @pytest.mark.asyncio
    async def test_removes_a_preset_that_is_not_in_use(self, audit, runtime):
        from kiro_crew.dashboard.handlers.decisions import api_decisions_local_model_delete

        runtime.installed = {"laya"}
        resp = await api_decisions_local_model_delete(_delete_request("laya"))
        assert resp.status == 200
        assert runtime.calls == [("remove", "laya")]
        assert {p["id"]: p["installed"] for p in json.loads(resp.text)["presets"]}["laya"] is False

    @pytest.mark.asyncio
    async def test_refuses_the_preset_the_runtime_is_serving(self, audit, runtime):
        from kiro_crew.dashboard.handlers.decisions import api_decisions_local_model_delete

        runtime.state.update(preset="laya", state="running", port=8104)
        resp = await api_decisions_local_model_delete(_delete_request("laya"))
        assert resp.status == 409
        assert json.loads(resp.text)["code"] == "decisions_local_model_in_use"
        assert runtime.calls == []

    @pytest.mark.asyncio
    async def test_refuses_the_preset_the_provider_names(
        self, audit, config_file, keystone, not_denied, runtime
    ):
        from kiro_crew.dashboard.handlers.decisions import (
            api_decisions_local_model_delete,
            api_decisions_provider_put,
        )

        await api_decisions_provider_put(_request(body={"preset": "laya"}))
        runtime.state.update(preset="", state="idle", port=0)  # a stopped server still counts
        resp = await api_decisions_local_model_delete(_delete_request("laya"))
        assert resp.status == 409

    @pytest.mark.asyncio
    async def test_an_unknown_id_is_a_404_and_touches_nothing(self, audit, runtime):
        from kiro_crew.dashboard.handlers.decisions import api_decisions_local_model_delete

        resp = await api_decisions_local_model_delete(_delete_request("../../etc"))
        assert resp.status == 404
        assert runtime.calls == []

    @pytest.mark.asyncio
    async def test_a_non_owner_is_refused(self, audit, runtime):
        from kiro_crew.dashboard.handlers.decisions import api_decisions_local_model_delete

        resp = await api_decisions_local_model_delete(_delete_request("laya", user="someone-else"))
        assert resp.status == 403
        assert runtime.calls == []


class TestDeleteAndSwitchAreSerialised:
    @pytest.mark.asyncio
    async def test_a_delete_waits_for_a_switch_in_flight(self, audit, runtime):
        """A switch holding the lock is activating a preset; the delete must re-check
        after it, not remove files from under the download that just started."""
        import asyncio

        import kiro_crew.dashboard.handlers.decisions as mod

        runtime.installed = {"laya"}
        async with mod._PROVIDER_SWITCH_LOCK:
            pending = asyncio.ensure_future(
                mod.api_decisions_local_model_delete(_delete_request("laya"))
            )
            await asyncio.sleep(0.05)
            assert not pending.done()
            # The switch lands: the runtime now prepares the preset being deleted.
            runtime.state.update(preset="laya", state="downloading", port=8104)
        resp = await asyncio.wait_for(pending, timeout=5)
        assert resp.status == 409
        assert ("remove", "laya") not in runtime.calls
