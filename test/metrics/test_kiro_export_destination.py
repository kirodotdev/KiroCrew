"""The Kiro telemetry destination: its gate, its region, its headers, its failures.

Three tiers, in the order a reader should take them.

* **The gate and the descriptor.** Every switch that decides whether this
  destination exists at all, and the exact descriptor the default provider hands
  the core when it does. These are the assertions that say nothing leaves the
  machine, so each one is driven by turning exactly ONE switch off.
* **The transport.** The real ``KiroExportSession`` against a mounted adapter
  that records what it was asked to send and answers canned responses. No
  socket is opened, nothing listens, and no address outside the process is
  contacted: the adapter replaces the HTTP layer underneath ``requests``
  entirely.
* **The payload.** What a receiving side would actually see, read back from the
  narrowed exporter the core wraps this destination's exporter in. Driven over a
  real ``MetricsData`` so the names, the point attributes and the resource keys
  are asserted on the object that gets serialized, not on a description of it.

The one link not covered here is ``OTLPMetricExporter`` itself, which lives in an
optional package extra a default CI environment does not install. The last tier
of ``test_otlp_wire_e2e.py`` covers that link for the operator destination, and
the ``requires_otlp`` test at the end of this file covers it for this one: a real
exporter, a real protobuf body, decoded and asserted, when the extra is present.
"""

import logging
import time
from datetime import datetime, timedelta, timezone

import pytest
import requests

from kiro_crew.auth.store import KasToken, TokenStore
from kiro_crew.metrics import kiro_export
from kiro_crew.metrics.product_metrics import PRODUCT_METRICS

US_ENDPOINT = "https://telemetry.us-east-1.kiro.dev/v1/metrics"
EU_ENDPOINT = "https://telemetry.eu-central-1.kiro.dev/v1/metrics"

#: Distinctive enough that finding it anywhere it must not be is unambiguous.
FAKE_TOKEN = "fake-access-token-must-never-be-logged-0123456789"
FAKE_ARN = "arn:aws:codewhisperer:us-east-1:FAKEACCOUNTID:profile/FAKEPROFILE"
FAKE_INSTALL_ID = "0123456789abcdef0123456789abcdef"

#: Captured at import, before the autouse fixture stubs it, so a test that needs
#: the real shared gate can put it back rather than reach into the module dict.
_REAL_OUTBOUND_PERMITTED = kiro_export.outbound_permitted


class _Cfg:
    """The telemetry-config shape this destination reads, and nothing else."""

    def __init__(self, **kwargs):
        self.enabled = kwargs.pop("enabled", True)
        self.otlp_endpoint = kwargs.pop("otlp_endpoint", "")
        self.kiro_export = kwargs.pop("kiro_export", True)
        self.kiro_export_region = kwargs.pop("kiro_export_region", "")
        self.export_interval_seconds = kwargs.pop("export_interval_seconds", 60)
        assert not kwargs, f"unexpected config keys: {sorted(kwargs)}"


@pytest.fixture(autouse=True)
def _clean_env(_floor_monkeypatch, tmp_path):
    """No ambient switch, no policy ceiling, no sign-in, and a data home of ours.

    Autouse because every assertion in this file is about what a switch does, and
    a switch left set by the developer's shell would make a passing run
    meaningless. Patches through ``_floor_monkeypatch`` rather than the shared
    ``monkeypatch``, which a test calling ``monkeypatch.undo()`` would lift.
    """
    for name in (
        kiro_export.DO_NOT_TRACK_ENV,
        kiro_export.DISABLE_ENV,
        kiro_export.TEST_MODE_ENV,
        "KIRO_API_KEY",
    ):
        _floor_monkeypatch.delenv(name, raising=False)
    # An opted-in host. The suite pins collection consent off for every test,
    # and this destination's send path asks consent before each export, so a
    # file about what an OPTED-IN host sends turns it on; the tests about
    # withdrawing it set it themselves.
    _floor_monkeypatch.setenv("KIROCREW_TELEMETRY", "1")
    _floor_monkeypatch.setenv("KIROCREW_HOME", str(tmp_path / "home"))
    import kiro_crew.config.paths as paths

    _floor_monkeypatch.setattr(paths, "_resolved_home", None)
    # The shared outbound gate says yes unless a test says otherwise. Its real
    # ladder reads the governance profile off disk and refuses on a CI runner and
    # on a non-default KIROCREW_HOME -- both true of every environment this file
    # runs in -- so leaving it live would make these assertions pass for the wrong
    # reason here and fail for the wrong reason in CI. The wiring to the REAL
    # function, and each refusal reaching this destination, have their own tests.
    from kiro_crew import beacon

    _floor_monkeypatch.setattr(
        kiro_export, "outbound_permitted", lambda: beacon.Verdict(True, "ready", "ready")
    )
    # A real opted-in config on disk, because the export path re-resolves the
    # whole destination from it per request. Written rather than patched so the
    # transport assertions run against the REAL resolver, and so a test can turn
    # a switch off mid-process the way an operator does -- by editing this file.
    _write_config(tmp_path / "home", kiro_export=True)


def _write_config(home, **telemetry):
    """Write a telemetry config under *home*, the way an operator's file reads."""
    import json

    from kiro_crew.config.loader import KiroCrewConfig
    from kiro_crew.config.loader_locations import config_path

    section = {"enabled": True}
    section.update(telemetry)
    path = config_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps({"telemetry": section}), encoding="utf-8")
    _clear_config_cache()
    assert KiroCrewConfig.load().telemetry.kiro_export is bool(
        telemetry.get("kiro_export", False)
    ), "the written config did not take effect"
    return path


def _clear_config_cache():
    """Drop the loader's fingerprint memo, so a rewrite in the same tick is seen."""
    from kiro_crew.config.validation import _CONFIG_CACHE

    _CONFIG_CACHE.clear()


def _token(**kwargs) -> KasToken:
    """A stored sign-in, live by default."""
    expires_in = kwargs.pop("expires_in", timedelta(hours=1))
    return KasToken(
        access_token=kwargs.pop("access_token", FAKE_TOKEN),
        expires_at=datetime.now(timezone.utc) + expires_in,
        provider=kwargs.pop("provider", "BuilderId"),
        identity=kwargs.pop("identity", "builder_id"),
        profile_arn=kwargs.pop("profile_arn", None),
        auth_method=kwargs.pop("auth_method", None),
        **kwargs,
    )


def _sign_in(monkeypatch, token):
    """Put *token* in the sign-in cache, or nothing there when it is None.

    Patches the store's own ``resolve``, so the expiry and shape rules the
    destination relies on are the product's real ones.
    """
    monkeypatch.setattr(TokenStore, "resolve", lambda self: token)


def _install_id(monkeypatch, value=FAKE_INSTALL_ID):
    from kiro_crew import beacon

    monkeypatch.setattr(beacon, "install_id", lambda *, create=True: value)


# ---------------------------------------------------------------------------
# tier 1 - the gate
# ---------------------------------------------------------------------------


def test_the_destination_is_off_by_default():
    """The whole point of phase two: the switch exists and starts off.

    Read off the config dataclass rather than from a literal here, so a default
    flipped in the section cannot pass this file.
    """
    from kiro_crew.config.sections_telemetry import TelemetryConfig

    assert TelemetryConfig().kiro_export is False
    assert kiro_export.endpoint_for(TelemetryConfig()) == ""


def test_both_new_keys_survive_a_config_load(tmp_path, monkeypatch):
    """A declared field is not a read one: the loader builds the section by hand.

    Without this, setting either key in `config.json` is silently replaced by its
    default on load -- the destination stays off however the file reads, and a
    `config get` dump followed by a `config set --file` writes the defaults back
    over the operator's choice.
    """
    import json

    import kiro_crew.config.paths as paths
    from kiro_crew.config.loader import KiroCrewConfig
    from kiro_crew.config.paths import config_dir

    monkeypatch.setenv("KIROCREW_HOME", str(tmp_path / "cfg-home"))
    monkeypatch.setattr(paths, "_resolved_home", None)
    path = config_dir() / "config.json"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        json.dumps(
            {
                "telemetry": {
                    "enabled": True,
                    "kiro_export": True,
                    "kiro_export_region": "eu-central-1",
                }
            }
        ),
        encoding="utf-8",
    )
    telemetry = KiroCrewConfig.load().telemetry
    assert telemetry.kiro_export is True
    assert telemetry.kiro_export_region == "eu-central-1"
    assert kiro_export.endpoint_for(telemetry) == EU_ENDPOINT


def test_the_switch_on_gives_the_us_endpoint():
    assert kiro_export.endpoint_for(_Cfg()) == US_ENDPOINT


@pytest.mark.parametrize(
    "name,value",
    [
        (kiro_export.DO_NOT_TRACK_ENV, "1"),
        # The convention is read inclusively: a host that sets the variable at
        # all has asked not to be tracked, whatever word it used.
        (kiro_export.DO_NOT_TRACK_ENV, "true"),
        (kiro_export.DO_NOT_TRACK_ENV, "yes"),
        (kiro_export.DISABLE_ENV, "0"),
        (kiro_export.DISABLE_ENV, "false"),
        (kiro_export.TEST_MODE_ENV, "1"),
    ],
)
def test_one_environment_switch_is_enough_to_remove_the_destination(monkeypatch, name, value):
    monkeypatch.setenv(name, value)
    assert kiro_export.endpoint_for(_Cfg()) == ""


@pytest.mark.parametrize("value", ["0", "false", "off", ""])
def test_do_not_track_set_to_an_explicit_off_does_not_disable(monkeypatch, value):
    """``DO_NOT_TRACK=0`` is the convention's way of saying "tracking is fine".

    Asserted because the inclusive reading above could easily have been written
    as "set to anything", which would make the documented opt-IN value an opt-out.
    """
    monkeypatch.setenv(kiro_export.DO_NOT_TRACK_ENV, value)
    assert kiro_export.endpoint_for(_Cfg()) == US_ENDPOINT


@pytest.mark.parametrize("value", ["1", "true", "yes", "anything-else"])
def test_the_product_switch_only_disables_on_an_explicit_off(monkeypatch, value):
    monkeypatch.setenv(kiro_export.DISABLE_ENV, value)
    assert kiro_export.endpoint_for(_Cfg()) == US_ENDPOINT


def test_a_test_mode_gateway_marks_itself_and_every_process_it_spawns(monkeypatch):
    """The marker the gateway sets, and the gate reading it, asserted together.

    An environment variable is what a spawned process inherits, so this is also
    the assertion that an agent spawned by a test-mode gateway does not export.
    """
    import os

    assert kiro_export.in_test_mode() is False
    assert kiro_export.endpoint_for(_Cfg()) == US_ENDPOINT
    try:
        kiro_export.mark_test_mode()
        assert kiro_export.in_test_mode() is True
        assert kiro_export.endpoint_for(_Cfg()) == ""
    finally:
        # Removed by hand: the autouse fixture's delenv records no undo for a
        # variable that was already absent, so monkeypatch would not take this
        # one back out and the rest of the worker's tests would inherit it.
        os.environ.pop(kiro_export.TEST_MODE_ENV, None)


def test_the_config_key_off_removes_the_destination():
    assert kiro_export.endpoint_for(_Cfg(kiro_export=False)) == ""


@pytest.mark.parametrize(
    "configured,expected",
    [
        ("", US_ENDPOINT),
        ("us-east-1", US_ENDPOINT),
        ("eu-central-1", EU_ENDPOINT),
        # Case and surrounding whitespace name the same region; reading them as
        # that region is not a guess about which region was meant.
        ("  us-east-1  ", US_ENDPOINT),
        ("EU-Central-1", EU_ENDPOINT),
        # Anything else disables rather than picking the closest.
        ("us-west-2", ""),
        ("eu", ""),
        ("us-east-1,eu-central-1", ""),
        ("../us-east-1", ""),
    ],
)
def test_the_region_is_a_closed_set(configured, expected):
    assert kiro_export.endpoint_for(_Cfg(kiro_export_region=configured)) == expected


def test_an_unknown_region_says_so_once_at_debug(caplog):
    with caplog.at_level(logging.DEBUG, logger=kiro_export.logger.name):
        assert kiro_export.endpoint_for(_Cfg(kiro_export_region="us-west-2")) == ""
    assert "us-west-2" in caplog.text
    assert "disabled" in caplog.text


def test_the_endpoint_is_https_and_not_reachable_by_configuration():
    """No setting may change the scheme or the host.

    The region is the only operator-supplied part of the URL, and it is compared
    against the closed set BEFORE it is interpolated -- so a value carrying a
    scheme, a host or a path cannot reach the template at all.
    """
    for region in kiro_export.REGIONS:
        endpoint = kiro_export.endpoint_for(_Cfg(kiro_export_region=region))
        assert endpoint.startswith("https://telemetry.")
        assert endpoint.endswith(".kiro.dev/v1/metrics")
    for hostile in (
        "us-east-1/../../evil.example",
        "us-east-1.evil.example",
        "us-east-1 evil.example",
    ):
        assert kiro_export.endpoint_for(_Cfg(kiro_export_region=hostile)) == ""


# ---------------------------------------------------------------------------
# tier 1 - the descriptor the core is handed
# ---------------------------------------------------------------------------


def _destinations(cfg):
    from kiro_crew.platform.defaults import DefaultTelemetryProvider

    return DefaultTelemetryProvider().otlp_destinations(cfg)


def test_the_descriptor_carries_the_allowlist_the_client_label_and_the_resource_narrowing():
    (dest,) = _destinations(_Cfg())
    assert dest.name == "kiro-telemetry"
    assert dest.endpoint == US_ENDPOINT
    assert dest.signals == frozenset({"metrics"})
    assert dest.allow == PRODUCT_METRICS
    assert dict(dest.point_attributes) == {"ClientType": "kiro-crew"}
    assert dest.resource_allow == frozenset(
        {"service.name", "service.version", "os.type", "process.pid"}
    )
    assert isinstance(dest.session, kiro_export.KiroExportSession)
    assert dest.delta_temporality is True


def test_the_operator_destination_is_unchanged_beside_it():
    """An operator's own collector keeps getting everything, with no narrowing."""
    operator, kiro = _destinations(_Cfg(otlp_endpoint="http://127.0.0.1:4318/v1/metrics"))
    assert operator.name == "telemetry.otlp_endpoint"
    assert operator.endpoint == "http://127.0.0.1:4318/v1/metrics"
    assert operator.allow is None
    assert dict(operator.point_attributes) == {}
    assert operator.resource_allow is None
    assert operator.session is None
    assert operator.delta_temporality is False
    assert kiro.name == "kiro-telemetry"


def test_the_operator_destination_survives_every_kiro_switch_being_off(monkeypatch):
    monkeypatch.setenv(kiro_export.DO_NOT_TRACK_ENV, "1")
    (operator,) = _destinations(_Cfg(otlp_endpoint="http://127.0.0.1:4318/v1/metrics"))
    assert operator.name == "telemetry.otlp_endpoint"


def test_no_destination_at_all_when_nothing_is_configured():
    assert _destinations(_Cfg(kiro_export=False)) == ()


# ---------------------------------------------------------------------------
# tier 1 - identity headers (RFC 4.4)
# ---------------------------------------------------------------------------


def test_builder_id_sends_a_bearer_and_omits_the_token_type(monkeypatch):
    _sign_in(monkeypatch, _token(provider="BuilderId", identity="builder_id"))
    assert kiro_export.identity_headers() == {"Authorization": f"Bearer {FAKE_TOKEN}"}


def test_identity_center_sends_the_profile_arn_and_omits_the_token_type(monkeypatch):
    _sign_in(
        monkeypatch,
        _token(provider="Enterprise", identity="identity_center", profile_arn=FAKE_ARN),
    )
    assert kiro_export.identity_headers() == {
        "Authorization": f"Bearer {FAKE_TOKEN}",
        "x-kiro-profile-arn": FAKE_ARN,
    }


def test_a_social_sign_in_omits_the_token_type(monkeypatch):
    _sign_in(monkeypatch, _token(provider="Google", identity="social", profile_arn=FAKE_ARN))
    headers = kiro_export.identity_headers()
    assert "x-kiro-token-type" not in headers
    assert headers["x-kiro-profile-arn"] == FAKE_ARN


def test_an_external_idp_names_itself_and_sends_the_arn(monkeypatch):
    _sign_in(
        monkeypatch,
        _token(
            provider="ExternalIdp",
            identity="external_idp",
            auth_method="external_idp",
            profile_arn=FAKE_ARN,
        ),
    )
    assert kiro_export.identity_headers() == {
        "Authorization": f"Bearer {FAKE_TOKEN}",
        "x-kiro-token-type": "EXTERNAL_IDP",
        "x-kiro-profile-arn": FAKE_ARN,
    }


def test_a_sign_in_with_no_profile_arn_simply_omits_that_header(monkeypatch):
    """The nearby input: a live token whose sign-in kind has no ARN to send."""
    _sign_in(
        monkeypatch,
        _token(provider="ExternalIdp", identity="external_idp", profile_arn=None),
    )
    headers = kiro_export.identity_headers()
    assert headers["x-kiro-token-type"] == "EXTERNAL_IDP"
    assert "x-kiro-profile-arn" not in headers


def test_an_api_key_names_itself(monkeypatch):
    _sign_in(monkeypatch, None)
    monkeypatch.setenv("KIRO_API_KEY", FAKE_TOKEN)
    assert kiro_export.identity_headers() == {
        "Authorization": f"Bearer {FAKE_TOKEN}",
        "x-kiro-token-type": "API_KEY",
    }


def test_a_stored_sign_in_wins_over_an_ambient_api_key(monkeypatch):
    """Same order the rest of the product resolves them in."""
    _sign_in(monkeypatch, _token(access_token="stored-token-value"))
    monkeypatch.setenv("KIRO_API_KEY", "ambient-key-value")
    assert kiro_export.identity_headers()["Authorization"] == "Bearer stored-token-value"


@pytest.mark.parametrize(
    "expires_in",
    [
        timedelta(hours=-1),  # already expired
        timedelta(seconds=30),  # inside the engine's own refresh margin
    ],
)
def test_a_token_inside_its_refresh_margin_sends_as_not_signed_in(monkeypatch, expires_in):
    """Export never refreshes, so a token the engine would renew is not sent."""
    _sign_in(monkeypatch, _token(expires_in=expires_in))
    _install_id(monkeypatch)
    headers = kiro_export.identity_headers()
    assert "Authorization" not in headers
    assert set(headers) == {"x-kiro-machineid"}


def test_export_never_refreshes_and_never_starts_a_sign_in(monkeypatch):
    """Named directly, because "do not refresh" is a rule, not an accident.

    Any refresh or sign-in would go through these two, so making them explode is
    what proves the export path does not reach them.
    """
    import kiro_crew.auth.refresh as refresh

    def _boom(*args, **kwargs):
        raise AssertionError("the export path must not refresh a token")

    monkeypatch.setattr(refresh, "ensure_fresh", _boom)
    _sign_in(monkeypatch, _token(expires_in=timedelta(hours=-1), refresh_token="rt"))
    _install_id(monkeypatch)
    assert set(kiro_export.identity_headers()) == {"x-kiro-machineid"}


def test_an_unreadable_sign_in_cache_sends_as_not_signed_in(monkeypatch):
    def _raise(self):
        raise OSError("vault unreadable")

    monkeypatch.setattr(TokenStore, "resolve", _raise)
    _install_id(monkeypatch)
    assert set(kiro_export.identity_headers()) == {"x-kiro-machineid"}


def test_not_signed_in_sends_a_hash_of_the_existing_install_id(monkeypatch):
    import hashlib

    _sign_in(monkeypatch, None)
    _install_id(monkeypatch)
    sent = kiro_export.identity_headers()["x-kiro-machineid"]
    assert sent != FAKE_INSTALL_ID, "the install id itself was sent"
    assert FAKE_INSTALL_ID not in sent
    assert len(sent) == 64 and set(sent) <= set("0123456789abcdef")
    plain = hashlib.sha256(FAKE_INSTALL_ID.encode("utf-8")).hexdigest()
    assert sent != plain, (
        "the hash carries no prefix, so it can be matched against a hash of the "
        "same install id taken anywhere else"
    )


def test_the_machine_id_header_never_creates_a_file(monkeypatch, tmp_path):
    """Reading an id that is not there must not mint one."""
    from kiro_crew import beacon
    from kiro_crew.config.paths import config_dir

    _sign_in(monkeypatch, None)
    calls = []
    real = beacon.install_id

    def _spy(*, create=True):
        calls.append(create)
        return real(create=create)

    monkeypatch.setattr(beacon, "install_id", _spy)
    assert kiro_export.identity_headers() == {}
    assert calls == [False], "the install id was read with create=True"
    assert not (config_dir() / beacon.INSTALL_ID_FILE).exists()


def test_no_identity_at_all_means_no_headers(monkeypatch):
    """Neither a sign-in nor a readable install id: the caller drops the interval.

    There is deliberately no fallback id here. A constant would put every install
    into one rate limit, and a per-process one would read a restart as a new
    machine.
    """
    _sign_in(monkeypatch, None)
    _install_id(monkeypatch, "")
    assert kiro_export.identity_headers() == {}


# ---------------------------------------------------------------------------
# tier 2 - the transport
# ---------------------------------------------------------------------------


class _Adapter(requests.adapters.BaseAdapter):
    """Replaces the HTTP layer under ``requests``: records, answers, sends nothing.

    No socket is created and no address is contacted. ``answers`` is a list of
    status codes or exceptions, consumed in order; the last one repeats.
    """

    def __init__(self, answers=(200,)):
        super().__init__()
        self.sent = []
        self._answers = list(answers)

    def send(self, request, **kwargs):
        self.sent.append((request, kwargs))
        answer = self._answers[min(len(self.sent), len(self._answers)) - 1]
        if isinstance(answer, Exception):
            raise answer
        response = requests.Response()
        response.status_code = answer
        response.url = request.url
        response.request = request
        # Server-controlled text, which this destination must never log.
        response._content = b"endpoint said no: secret-server-detail"
        return response

    def close(self):
        return None


class _Clock:
    """A clock the test moves by hand, so an hour costs nothing."""

    def __init__(self):
        self.now = 1000.0

    def __call__(self):
        return self.now

    def advance(self, seconds):
        self.now += seconds


def _session(answers=(200,), clock=None):
    session = kiro_export.KiroExportSession(clock=clock or time.monotonic)
    adapter = _Adapter(answers)
    session.mount("https://", adapter)
    return session, adapter


def _post(session, times=1):
    for _ in range(times):
        session.post(US_ENDPOINT, data=b"payload")


def test_the_session_sends_one_request_carrying_the_identity_headers(monkeypatch):
    _sign_in(monkeypatch, _token(profile_arn=FAKE_ARN))
    session, adapter = _session()
    _post(session)
    assert len(adapter.sent) == 1
    request, _ = adapter.sent[0]
    assert request.url == US_ENDPOINT
    assert request.headers["Authorization"] == f"Bearer {FAKE_TOKEN}"
    assert request.headers["x-kiro-profile-arn"] == FAKE_ARN
    assert "x-kiro-machineid" not in request.headers


def test_the_timeout_is_this_destinations_and_not_the_callers(monkeypatch):
    """A host's own OTLP timeout must not make the reader thread wait longer."""
    _sign_in(monkeypatch, _token())
    session, adapter = _session()
    session.post(US_ENDPOINT, data=b"payload", timeout=600)
    _, kwargs = adapter.sent[0]
    assert kwargs["timeout"] == 10.0
    assert kiro_export.TIMEOUT_SECONDS == 10.0


def _no_ambient_ca_bundle(monkeypatch):
    for name in ("REQUESTS_CA_BUNDLE", "CURL_CA_BUNDLE"):
        monkeypatch.delenv(name, raising=False)


def test_the_callers_tls_settings_do_not_reach_this_endpoint(monkeypatch):
    """Standard verification and no client certificate, whatever the caller passes.

    The OTLP exporter forwards the host's own collector TLS settings as ``verify``
    and ``cert``. A private CA bundle would fail every handshake with this public
    endpoint, and a client certificate is the operator collector's credential.
    ``requests`` replaces ``verify=True`` with a system bundle named by
    ``REQUESTS_CA_BUNDLE`` / ``CURL_CA_BUNDLE``, a host-wide trust setting rather
    than the collector's, so both are cleared to read ``True`` itself.
    """
    _no_ambient_ca_bundle(monkeypatch)
    _sign_in(monkeypatch, _token())
    session, adapter = _session()
    session.post(
        US_ENDPOINT,
        data=b"payload",
        verify="/etc/collector/private-ca.pem",
        cert=("/etc/collector/client.crt", "/etc/collector/client.key"),
    )
    _, kwargs = adapter.sent[0]
    assert kwargs["verify"] is True
    assert kwargs["cert"] is None


def test_the_real_exporter_cannot_hand_this_endpoint_the_hosts_tls_settings(monkeypatch):
    """The same, through the exporter that reads the OTLP TLS variables itself."""
    pytest.importorskip("opentelemetry.exporter.otlp.proto.http.metric_exporter")
    from opentelemetry.exporter.otlp.proto.http.metric_exporter import OTLPMetricExporter

    monkeypatch.setenv("OTEL_EXPORTER_OTLP_CERTIFICATE", "/etc/collector/private-ca.pem")
    monkeypatch.setenv("OTEL_EXPORTER_OTLP_CLIENT_CERTIFICATE", "/etc/collector/client.crt")
    monkeypatch.setenv("OTEL_EXPORTER_OTLP_CLIENT_KEY", "/etc/collector/client.key")
    _no_ambient_ca_bundle(monkeypatch)
    _sign_in(monkeypatch, _token())
    session, adapter = _session()
    exporter = OTLPMetricExporter(endpoint=US_ENDPOINT, session=session)
    exporter.export(_metrics_data([_ALLOWED_NAME], {"service.name": "kirocrew"}))
    assert adapter.sent, "the exporter sent nothing, so this test proves nothing"
    _, kwargs = adapter.sent[0]
    assert kwargs["verify"] is True
    assert kwargs["cert"] is None


def test_a_sign_in_landing_mid_process_is_used_by_the_next_export(monkeypatch):
    """The headers are resolved per request, which is why this is a Session."""
    _sign_in(monkeypatch, None)
    _install_id(monkeypatch)
    session, adapter = _session()
    _post(session)
    assert "x-kiro-machineid" in adapter.sent[0][0].headers

    _sign_in(monkeypatch, _token())
    _post(session)
    assert adapter.sent[1][0].headers["Authorization"] == f"Bearer {FAKE_TOKEN}"
    assert "x-kiro-machineid" not in adapter.sent[1][0].headers


def test_no_identity_means_no_request_is_sent(monkeypatch):
    _sign_in(monkeypatch, None)
    _install_id(monkeypatch, "")
    session, adapter = _session()
    _post(session)
    assert adapter.sent == []


def test_a_refused_token_is_dropped_without_a_second_request(monkeypatch):
    """401 must not be retried as not-signed-in: one interval, counted once."""
    _sign_in(monkeypatch, _token())
    _install_id(monkeypatch)
    session, adapter = _session(answers=(401,))
    _post(session)
    assert len(adapter.sent) == 1
    assert "x-kiro-machineid" not in adapter.sent[0][0].headers


def test_a_server_error_is_not_retried_inside_the_interval(monkeypatch):
    """The interval is dropped. The next one is a minute away, which is the budget."""
    _sign_in(monkeypatch, _token())
    session, adapter = _session(answers=(503,))
    _post(session)
    assert len(adapter.sent) == 1


def test_three_refusals_in_a_row_pause_the_destination_for_an_hour(monkeypatch):
    _sign_in(monkeypatch, _token())
    clock = _Clock()
    session, adapter = _session(answers=(400,), clock=clock)
    _post(session, times=3)
    assert len(adapter.sent) == 3

    # Paused: a whole hour of intervals sends nothing at all.
    for _ in range(59):
        clock.advance(60)
        _post(session)
    assert len(adapter.sent) == 3, "a request was sent during the pause"

    # The hour is up, so the next interval tries again.
    clock.advance(60)
    _post(session)
    assert len(adapter.sent) == 4


def test_a_success_in_between_breaks_the_refusal_streak(monkeypatch):
    _sign_in(monkeypatch, _token())
    clock = _Clock()
    session, adapter = _session(answers=(400, 400, 200, 400, 400), clock=clock)
    _post(session, times=5)
    assert len(adapter.sent) == 5, "the streak was not reset by the success"


def test_server_errors_do_not_count_toward_the_pause(monkeypatch):
    """Weather, not a configuration answer: it is retried next interval forever."""
    _sign_in(monkeypatch, _token())
    clock = _Clock()
    session, adapter = _session(answers=(500, 502, 503, 504, 500), clock=clock)
    _post(session, times=5)
    assert len(adapter.sent) == 5


def test_a_server_error_between_refusals_breaks_the_streak(monkeypatch):
    _sign_in(monkeypatch, _token())
    clock = _Clock()
    session, adapter = _session(answers=(400, 400, 500, 400, 400), clock=clock)
    _post(session, times=5)
    assert len(adapter.sent) == 5


def test_a_transport_failure_raises_nothing_into_the_reader_thread(monkeypatch):
    _sign_in(monkeypatch, _token())
    session, adapter = _session(answers=(requests.exceptions.ConnectionError("no route"),))
    response = session.post(US_ENDPOINT, data=b"payload")
    assert response.status_code == 200, "the exporter was handed a failure to raise on"
    assert len(adapter.sent) == 1


def test_an_outage_logs_one_warning_and_then_stays_at_debug(monkeypatch, caplog):
    _sign_in(monkeypatch, _token())
    clock = _Clock()
    session, _ = _session(answers=(500,), clock=clock)
    with caplog.at_level(logging.DEBUG, logger=kiro_export.logger.name):
        _post(session, times=20)
    warnings = [r for r in caplog.records if r.levelno == logging.WARNING]
    assert len(warnings) == 1, f"one warning per outage, got {len(warnings)}"


def test_recovery_logs_one_info_line(monkeypatch, caplog):
    _sign_in(monkeypatch, _token())
    clock = _Clock()
    session, _ = _session(answers=(500, 500, 200, 200), clock=clock)
    with caplog.at_level(logging.DEBUG, logger=kiro_export.logger.name):
        _post(session, times=4)
    infos = [r for r in caplog.records if r.levelno == logging.INFO]
    assert len(infos) == 1, f"one info line per recovery, got {len(infos)}"


def test_recovery_after_a_pause_reports_itself(monkeypatch, caplog):
    _sign_in(monkeypatch, _token())
    clock = _Clock()
    session, adapter = _session(answers=(400, 400, 400, 200), clock=clock)
    _post(session, times=3)
    clock.advance(kiro_export.PAUSE_SECONDS)
    with caplog.at_level(logging.DEBUG, logger=kiro_export.logger.name):
        _post(session)
    assert len(adapter.sent) == 4
    assert [r for r in caplog.records if r.levelno == logging.INFO]


def test_nothing_secret_reaches_a_log_line_even_at_debug(monkeypatch, caplog):
    """The token, the ARN, the machine id header and the endpoint's own words.

    Driven over every branch that logs: a success, a refusal, a pause, a
    transport failure and a recovery -- so a line added to any one of them is
    covered here rather than only the one a test happened to exercise.
    """
    _sign_in(monkeypatch, _token(profile_arn=FAKE_ARN))
    _install_id(monkeypatch)
    machine_id = kiro_export.machine_id_hash()
    clock = _Clock()
    session, _ = _session(
        answers=(
            200,
            400,
            400,
            400,
            requests.exceptions.ConnectionError("no route"),
            200,
        ),
        clock=clock,
    )
    with caplog.at_level(logging.DEBUG):
        _post(session, times=4)
        clock.advance(kiro_export.PAUSE_SECONDS)
        _post(session, times=2)
        # And the not-signed-in branch, whose header is the hash.
        _sign_in(monkeypatch, None)
        _post(session, times=2)
    text = caplog.text
    for secret in (FAKE_TOKEN, FAKE_ARN, machine_id, "secret-server-detail"):
        assert secret not in text, "a value that may only ride in a header was logged"
    assert "Bearer" not in text


def test_an_operator_collector_header_never_reaches_this_endpoint(monkeypatch):
    """The defect this closes: somebody else's credentials, sent to Kiro.

    ``OTEL_EXPORTER_OTLP_HEADERS`` is the documented way to authenticate an
    OPERATOR's own collector, and the OTLP exporter's constructor copies it onto
    the session it is handed -- this one. Driven through the real exporter
    constructor rather than by setting the header by hand, because what has to
    hold is that THAT path cannot leak, not that a dict was filtered.
    """
    _sign_in(monkeypatch, None)
    _install_id(monkeypatch)
    session, adapter = _session()
    # Exactly what the exporter's constructor does with the environment value.
    session.headers.update(
        {
            "Authorization": "Basic b3BlcmF0b3I6c2VjcmV0",
            "x-honeycomb-team": "operator-vendor-api-key",
            "X-Scope-OrgID": "operator-tenant",
        }
    )
    _post(session)
    sent, _ = adapter.sent[0]
    assert "x-honeycomb-team" not in sent.headers
    assert "X-Scope-OrgID" not in sent.headers
    # The identity header replaced the operator's Authorization rather than
    # riding beside it: this request has no sign-in, so it carries none.
    assert "Authorization" not in sent.headers
    assert "x-kiro-machineid" in sent.headers
    # The transport headers the request does not parse without are still there,
    # so the filter is an allowlist and not a blanket wipe.
    assert sent.headers.get("User-Agent")
    assert sent.headers.get("Accept-Encoding")


def test_an_operator_authorization_cannot_survive_beside_a_sign_in(monkeypatch):
    _sign_in(monkeypatch, _token())
    session, adapter = _session()
    session.headers["Authorization"] = "Basic b3BlcmF0b3I6c2VjcmV0"
    _post(session)
    sent, _ = adapter.sent[0]
    assert sent.headers["Authorization"] == f"Bearer {FAKE_TOKEN}"


def test_the_real_exporter_constructor_cannot_plant_a_header(monkeypatch):
    """Same claim as above, asserted against the actual exporter when present."""
    pytest.importorskip("opentelemetry.exporter.otlp.proto.http.metric_exporter")
    from opentelemetry.exporter.otlp.proto.http.metric_exporter import OTLPMetricExporter

    _sign_in(monkeypatch, None)
    _install_id(monkeypatch)
    monkeypatch.setenv(
        "OTEL_EXPORTER_OTLP_HEADERS",
        "authorization=Basic b3BlcmF0b3I6c2VjcmV0,x-honeycomb-team=vendor-key",
    )
    session, adapter = _session()
    OTLPMetricExporter(endpoint=US_ENDPOINT, session=session)
    assert any(
        "honeycomb" in name.lower() for name in session.headers
    ), "the exporter did not plant the env header, so this test proves nothing"
    _post(session)
    sent, _ = adapter.sent[0]
    assert not [name for name in sent.headers if "honeycomb" in name.lower()]
    assert "Authorization" not in sent.headers
    # The exporter's own Content-Type is a transport header and survives, so the
    # payload is still a readable OTLP request.
    assert sent.headers.get("Content-Type") == "application/x-protobuf"


def test_an_ambient_netrc_credential_is_not_applied(monkeypatch):
    """A `default` netrc entry matches every host, including this one."""
    _sign_in(monkeypatch, None)
    _install_id(monkeypatch)
    session, adapter = _session()
    assert session.auth is not None, "an unset auth is what makes requests read netrc"
    calls = []

    def _netrc(url):
        calls.append(url)
        return ("netrc-user", "netrc-password")

    monkeypatch.setattr(requests.sessions, "get_netrc_auth", _netrc)
    _post(session)
    sent, _ = adapter.sent[0]
    assert calls == [], "requests looked up netrc for this request"
    assert "Authorization" not in sent.headers
    assert "netrc" not in str(sent.headers)


def test_a_redirect_is_not_followed(monkeypatch):
    """The endpoint is a constant, so a redirect can only move a token off it."""
    _sign_in(monkeypatch, _token())
    session, adapter = _session(answers=(307,))
    session.post(US_ENDPOINT, data=b"payload")
    assert len(adapter.sent) == 1
    assert adapter.sent[0][0].url == US_ENDPOINT


def test_rebuild_auth_re_applies_nothing(monkeypatch):
    """Defence in depth: the one path that re-reads netrc mid-redirect."""
    _sign_in(monkeypatch, _token())
    session, _ = _session()
    monkeypatch.setattr(
        requests.sessions, "get_netrc_auth", lambda url: ("netrc-user", "netrc-password")
    )
    prepared = requests.Request("POST", US_ENDPOINT).prepare()
    prepared.headers["Authorization"] = f"Bearer {FAKE_TOKEN}"
    session.rebuild_auth(prepared, requests.Response())
    assert "Authorization" not in prepared.headers


@pytest.mark.parametrize(
    "code",
    ["env_opt_out", "governance", "ci", "non_default_home", "awaiting_privacy_ack"],
)
def test_every_shared_suppression_removes_the_destination(monkeypatch, code):
    """Whatever the product's one outbound gate refuses for, this destination goes.

    Driven by code rather than by cause, so a suppression ADDED to
    ``beacon.telemetry_permitted`` later is honoured here without a change: this
    destination reads the verdict and never its reasons.
    """
    from kiro_crew import beacon

    monkeypatch.setattr(
        kiro_export, "outbound_permitted", lambda: beacon.Verdict(False, "no", code)
    )
    assert kiro_export.endpoint_for(_Cfg()) == ""
    assert _destinations(_Cfg()) == ()
    # The operator's own collector is not governed by that gate.
    (operator,) = _destinations(_Cfg(otlp_endpoint="http://127.0.0.1:4318/v1/metrics"))
    assert operator.name == "telemetry.otlp_endpoint"


def _real_gate(monkeypatch):
    """Put the REAL `outbound_permitted` back, over the fixture's stub."""
    monkeypatch.setattr(kiro_export, "outbound_permitted", _REAL_OUTBOUND_PERMITTED)


def test_the_shared_gate_is_the_real_one_and_honours_the_existing_opt_out(monkeypatch):
    """The wiring, through the REAL `beacon.telemetry_permitted`.

    This is the finding that made the destination ask that function instead of
    carrying a ladder of its own: ``KIROCREW_TELEMETRY_DISABLED`` is how a host
    has opted out of outbound telemetry since before this destination existed,
    and a second gate that did not know about it would keep sending. CI, the home
    check and the first-send stamp are neutralised so the ladder reaches the env
    check, leaving the variable under test as the only thing deciding.
    """
    from kiro_crew import beacon

    _real_gate(monkeypatch)
    monkeypatch.setattr(beacon, "is_ci", lambda: False)
    monkeypatch.setattr(beacon, "is_default_home", lambda: True)
    monkeypatch.setattr(beacon, "is_governance_pinned_off", lambda **_: False)
    monkeypatch.setattr(beacon, "is_first_send", lambda: False)

    monkeypatch.delenv(beacon.DISABLE_ENV, raising=False)
    assert kiro_export.outbound_permitted().ok, "the real gate refused a clean host"
    assert kiro_export.endpoint_for(_Cfg()) == US_ENDPOINT

    monkeypatch.setenv(beacon.DISABLE_ENV, "1")
    verdict = kiro_export.outbound_permitted()
    assert not verdict.ok and verdict.code == "env_opt_out"
    assert kiro_export.endpoint_for(_Cfg()) == ""


def test_an_unresolvable_shared_gate_refuses(monkeypatch):
    """Fail closed: a gate that cannot be computed is not an absent one."""
    from kiro_crew import beacon

    def _raise(**_):
        raise RuntimeError("governance profile unreadable")

    _real_gate(monkeypatch)
    monkeypatch.setattr(beacon, "telemetry_permitted", _raise)
    verdict = kiro_export.outbound_permitted()
    assert not verdict.ok and verdict.code == "error"
    assert kiro_export.endpoint_for(_Cfg()) == ""


def test_a_posture_read_does_not_ask_the_shared_gate_at_all(monkeypatch):
    """A panel refetch neither audits nor depends on the shared gate.

    Disclosure answers with the CONFIGURED destination, so the gate is not
    consulted: its "no" can lift with no change by the person.
    """
    from kiro_crew import beacon

    seen = []

    def _permitted(**kwargs):
        seen.append(kwargs)
        return beacon.Verdict(False, "governance", "pinned off")

    _real_gate(monkeypatch)
    monkeypatch.setattr(beacon, "telemetry_permitted", _permitted)
    with kiro_export.disclosure_only():
        assert kiro_export.endpoint_for(_Cfg()) == US_ENDPOINT
    assert seen == [], "a posture read consulted the shared gate"
    assert kiro_export.endpoint_for(_Cfg()) == "", "enforcement must still ask it"
    assert seen, "the enforcement path did not ask the shared gate"


def test_a_configured_export_reads_as_egress_while_a_temporary_no_holds(monkeypatch):
    """Behind the shared gate, a configured export is still egress.

    The config route refuses to enable collection as local-only when this
    answers True. Answering False while a temporary "no" holds would let
    collection be enabled as local-only and then start sending when the gate
    lifts (a policy denial lifted, the first-run notice acknowledged).
    """
    from kiro_crew import beacon
    from kiro_crew.metrics.provider import otlp_egress_active

    monkeypatch.setattr(
        kiro_export, "outbound_permitted", lambda: beacon.Verdict(False, "ack", "not acked")
    )
    assert otlp_egress_active(_Cfg()) is True
    assert kiro_export.endpoint_for(_Cfg()) == ""
    # A permanent local no is still answered as no egress.
    assert otlp_egress_active(_Cfg(kiro_export=False)) is False
    monkeypatch.setenv(kiro_export.DO_NOT_TRACK_ENV, "1")
    assert otlp_egress_active(_Cfg()) is False


def test_the_shared_gate_is_not_paid_on_a_default_install(monkeypatch):
    """With the switch off -- the default -- the config is never even read.

    The gate stats the first-send stamp and loads the config, so it runs last and
    only for a host that asked for this destination.
    """
    called = []
    monkeypatch.setattr(kiro_export, "outbound_permitted", lambda: called.append(1))
    assert kiro_export.endpoint_for(_Cfg(kiro_export=False)) == ""
    assert called == []


def test_a_ceiling_pushed_to_a_running_gateway_stops_the_next_export(monkeypatch):
    """The build-time check is not enough, and this is why.

    A ceiling is pushed to a live gateway without rebuilding the recorder, and
    the metrics reader holds this session for the life of the process -- so a
    destination that only asked at build time would keep sending for hours after
    an administrator revoked it.
    """
    from kiro_crew import beacon

    _sign_in(monkeypatch, _token())
    session, adapter = _session()
    _post(session)
    assert len(adapter.sent) == 1, "the first export should have gone out"

    monkeypatch.setattr(
        kiro_export,
        "outbound_permitted",
        lambda: beacon.Verdict(False, "no", "governance"),
    )
    _post(session, times=5)
    assert len(adapter.sent) == 1, "an export left after the ceiling was pushed"


def test_a_policy_stop_is_not_counted_as_a_failure(monkeypatch, caplog):
    """It logs at debug, never warns, and does not spend the refusal streak.

    The endpoint did nothing wrong, so the next interval has to ask again rather
    than sit out an hour of a pause it did not earn.
    """
    from kiro_crew import beacon

    _sign_in(monkeypatch, _token())
    clock = _Clock()
    session, adapter = _session(clock=clock)
    monkeypatch.setattr(
        kiro_export, "outbound_permitted", lambda: beacon.Verdict(False, "no", "ci")
    )
    with caplog.at_level(logging.DEBUG, logger=kiro_export.logger.name):
        _post(session, times=10)
    assert adapter.sent == []
    assert not [r for r in caplog.records if r.levelno >= logging.WARNING]

    # Permission returns: the very next interval sends, with no pause to wait out.
    monkeypatch.setattr(
        kiro_export, "outbound_permitted", lambda: beacon.Verdict(True, "ready", "ready")
    )
    _post(session)
    assert len(adapter.sent) == 1


def test_turning_the_switch_off_mid_process_stops_the_next_export(monkeypatch, tmp_path):
    """The case a build-time check cannot see, and the reason the whole decision
    is re-resolved rather than one switch of it.

    Nothing rebuilds the metrics reader when a telemetry key changes: it holds
    its session and its endpoint for the life of the process, the gateway's
    config watcher is registered at boot only, and the periodic consent recheck
    re-reads ``telemetry.enabled`` alone. So an operator who turns the export off
    in an ordinary running session has to be obeyed on the export path itself.
    """
    _sign_in(monkeypatch, _token())
    session, adapter = _session()
    _post(session)
    assert len(adapter.sent) == 1, "the opted-in export should have gone out"

    _write_config(tmp_path / "home", kiro_export=False)
    _post(session, times=5)
    assert len(adapter.sent) == 1, "an export left after the switch was turned off"


def test_withdrawing_collection_consent_stops_the_next_export(monkeypatch, tmp_path):
    """The send path asks consent, so a reader's final flush after an opt-out sends nothing.

    ``endpoint_for`` leaves ``telemetry.enabled`` out on purpose (it also answers
    the "would enabling start egress" question), so this is the check that keeps
    the shutdown export a consent withdrawal triggers from leaving the machine.
    Driven both ways consent can be withdrawn: the config flag, and the
    ``KIROCREW_TELEMETRY`` pin that overrides it.
    """
    monkeypatch.delenv("KIROCREW_TELEMETRY", raising=False)
    _sign_in(monkeypatch, _token())
    session, adapter = _session()
    _post(session)
    assert len(adapter.sent) == 1

    _write_config(tmp_path / "home", enabled=False, kiro_export=True)
    _post(session, times=2)
    assert len(adapter.sent) == 1, "an export left after collection was turned off"

    _write_config(tmp_path / "home", kiro_export=True)
    _post(session)
    assert len(adapter.sent) == 2
    monkeypatch.setenv("KIROCREW_TELEMETRY", "0")
    _post(session, times=2)
    assert len(adapter.sent) == 2, "an export left with the consent pin set off"


def test_changing_the_region_mid_process_stops_the_old_endpoint(monkeypatch, tmp_path):
    """An in-flight reader must not keep posting to a region nobody chose any more.

    The exporter's URL is fixed when it is built, so the comparison is against
    the URL in hand rather than against a boolean: re-resolving to a DIFFERENT
    endpoint is as much a refusal of this request as resolving to none.
    """
    _sign_in(monkeypatch, _token())
    session, adapter = _session()
    _post(session)
    assert len(adapter.sent) == 1

    _write_config(tmp_path / "home", kiro_export=True, kiro_export_region="eu-central-1")
    _post(session, times=3)
    assert len(adapter.sent) == 1, "the old region's endpoint kept receiving exports"


def test_an_environment_opt_out_set_mid_process_stops_the_next_export(monkeypatch):
    """Same invariant, reached through the environment instead of the file."""
    _sign_in(monkeypatch, _token())
    session, adapter = _session()
    _post(session)
    assert len(adapter.sent) == 1

    monkeypatch.setenv(kiro_export.DO_NOT_TRACK_ENV, "1")
    _post(session, times=3)
    assert len(adapter.sent) == 1


def test_an_unreadable_configuration_stops_the_export(monkeypatch):
    """Fail closed: configuration that cannot be read resolves to no destination."""
    from kiro_crew.config.loader import KiroCrewConfig

    def _raise(*args, **kwargs):
        raise OSError("config unreadable")

    _sign_in(monkeypatch, _token())
    session, adapter = _session()
    monkeypatch.setattr(KiroCrewConfig, "load", staticmethod(_raise))
    _post(session, times=3)
    assert adapter.sent == []


def test_an_unresolvable_gate_stops_the_export_too(monkeypatch):
    """Fail closed per request, the same direction as at build time."""
    from kiro_crew import beacon

    def _raise(**_):
        raise RuntimeError("governance profile unreadable")

    _sign_in(monkeypatch, _token())
    session, adapter = _session()
    _real_gate(monkeypatch)
    monkeypatch.setattr(beacon, "telemetry_permitted", _raise)
    _post(session)
    assert adapter.sent == []


def test_the_session_opens_nothing_until_it_is_used():
    """Building a descriptor on a host that never exports must cost nothing."""
    session = kiro_export.build_session()
    assert isinstance(session, kiro_export.KiroExportSession)
    session.close()


# ---------------------------------------------------------------------------
# tier 3 - the payload a receiving side would see
# ---------------------------------------------------------------------------


def _metrics_data(names, resource_attrs, point_attributes=None):
    """A real ``MetricsData`` carrying one delta-sum point per name in *names*.

    The SDK's own export dataclasses, which ship in the hard dependency set, so
    this runs without the optional exporter extra.
    """
    from opentelemetry.sdk.metrics.export import (
        AggregationTemporality,
        Metric,
        MetricsData,
        NumberDataPoint,
        ResourceMetrics,
        ScopeMetrics,
        Sum,
    )
    from opentelemetry.sdk.resources import Resource
    from opentelemetry.sdk.util.instrumentation import InstrumentationScope

    metrics = [
        Metric(
            name=name,
            description="",
            unit="",
            data=Sum(
                data_points=[
                    NumberDataPoint(
                        attributes=(
                            {"outcome": "ok"}
                            if point_attributes is None
                            else dict(point_attributes)
                        ),
                        start_time_unix_nano=1,
                        time_unix_nano=2,
                        value=1,
                    )
                ],
                aggregation_temporality=AggregationTemporality.DELTA,
                is_monotonic=True,
            ),
        )
        for name in names
    ]
    return MetricsData(
        resource_metrics=[
            ResourceMetrics(
                resource=Resource.create(resource_attrs),
                scope_metrics=[
                    ScopeMetrics(
                        scope=InstrumentationScope("kiro_crew"),
                        metrics=metrics,
                        schema_url="",
                    )
                ],
                schema_url="",
            )
        ]
    )


class _CapturingExporter:
    """Stands in for the OTLP exporter: records the narrowed batch it is handed."""

    _preferred_temporality = None
    _preferred_aggregation = None

    def __init__(self):
        self.batches = []

    def export(self, metrics_data, timeout_millis=10_000, **kwargs):
        self.batches.append(metrics_data)
        return True

    def force_flush(self, timeout_millis=10_000):
        return True

    def shutdown(self, timeout_millis=30_000, **kwargs):
        return None


def _narrowed(names, resource_attrs, point_attributes=None):
    """What the Kiro destination's exporter receives for the given input batch."""
    from kiro_crew.metrics import destination_filter

    (dest,) = _destinations(_Cfg())
    inner = _CapturingExporter()
    wrapped = destination_filter.wrap_if_narrowed(dest, inner)
    assert wrapped is not inner, "the descriptor declared no narrowing"
    wrapped.export(_metrics_data(names, resource_attrs, point_attributes))
    assert len(inner.batches) == 1
    batch = inner.batches[0]
    out_names = []
    points = []
    resources = []
    for rm in batch.resource_metrics:
        resources.append(dict(rm.resource.attributes))
        for sm in rm.scope_metrics:
            for metric in sm.metrics:
                out_names.append(metric.name)
                points.extend(metric.data.data_points)
    return out_names, points, resources


#: One exportable name and one that is only ever local, so the assertion cannot
#: pass on a filter that drops everything or keeps everything.
_ALLOWED_NAME = "kirocrew.session.started"
_LOCAL_ONLY_NAME = "kirocrew.turn.duration_ms"


def test_only_allowlisted_metric_names_reach_this_destination():
    assert _ALLOWED_NAME in PRODUCT_METRICS
    assert _LOCAL_ONLY_NAME not in PRODUCT_METRICS
    names, _, _ = _narrowed([_ALLOWED_NAME, _LOCAL_ONLY_NAME], {"service.name": "kirocrew"})
    assert names == [_ALLOWED_NAME]


def test_every_data_point_carries_the_client_label():
    _, points, _ = _narrowed([_ALLOWED_NAME], {"service.name": "kirocrew"})
    assert points
    for point in points:
        assert point.attributes["ClientType"] == "kiro-crew"
        # The call site's own attributes survive beside it.
        assert point.attributes["outcome"] == "ok"


def test_the_resource_is_narrowed_to_four_per_process_constants():
    _, _, resources = _narrowed(
        [_ALLOWED_NAME],
        {
            "service.name": "kirocrew",
            "service.version": "1.2.3",
            "os.type": "linux",
            "process.pid": 4242,
            # Everything below must not reach this destination.
            "service.instance.id": FAKE_INSTALL_ID,
            "host.arch": "amd64",
            "host.cpu.logical_count": 64,
            "process.runtime.version": "3.12",
        },
    )
    assert resources
    for resource in resources:
        assert set(resource) == {
            "service.name",
            "service.version",
            "os.type",
            "process.pid",
        }
        assert "service.instance.id" not in resource
        assert not [key for key in resource if key.startswith("host.")]
        assert FAKE_INSTALL_ID not in str(resource)


def test_the_four_narrowed_resource_keys_exist_on_the_real_resource():
    """The narrowing can only remove keys, so each one must really be set.

    Asserted against the live resource builder rather than a list, so a key
    renamed there fails here instead of silently narrowing to nothing.
    """
    from kiro_crew.metrics.provider import _resource_attributes

    attrs = _resource_attributes("kiro")
    assert kiro_export.RESOURCE_ALLOW <= set(
        attrs
    ), f"missing from the shared resource: {sorted(kiro_export.RESOURCE_ALLOW - set(attrs))}"


#: The attribute keys each exported metric is reviewed to carry, from the RFC's
#: Appendix A. Held here as DATA, in one place, because three separate things have
#: to agree about it: this set, the table a person reads in the guide, and what
#: the narrowed exporter actually puts on the wire. The allowlist in
#: ``PRODUCT_METRICS`` gates metric NAMES; nothing in it gates attribute KEYS, so
#: without this pin a reviewed metric could grow an attribute at any call site and
#: export it with no review and no disclosure.
REVIEWED_ATTRIBUTE_KEYS = {
    "kirocrew.session.started": {"session_source", "harness"},
    "kirocrew.session.duration": {"end_reason", "session_source"},
    "kirocrew.session.idle_expired": {"turn_active", "orphaned"},
    "kirocrew.subagent.spawned": {"concurrency", "batched"},
    "kirocrew.context.compactions": {"success"},
    "kirocrew.cron.fires": {"kind", "trigger"},
    "kirocrew.artifact.created": {"kind", "source", "kind_auto"},
    "kirocrew.workflow.runs": {"authored", "replay"},
    "kirocrew.approval.decisions": {"decision", "security_deny"},
    "kirocrew.inventory.crons.active": set(),
    "kirocrew.inventory.monitor_loops.active": set(),
    "kirocrew.inventory.skills.installed": set(),
    "kirocrew.inventory.memory.migrated": set(),
    "kirocrew.inventory.knowledge.documents": set(),
    "kirocrew.inventory.lessons": set(),
    "kirocrew.inventory.mcp.servers": {"class"},
    "kirocrew.inventory.members": set(),
    "kirocrew.inventory.config.toggle": {"key"},
    "kirocrew.inventory.probe.failures": {"probe"},
}


def test_every_exported_metric_has_a_reviewed_attribute_key_set():
    """No exported name may be missing from the pin, and none may be invented.

    The entry condition for the two tests below: they are only a pin if they
    cover every name that can leave the machine. Adding a name to
    ``PRODUCT_METRICS`` without a reviewed key set reddens HERE, which is the
    moment to decide what that metric may carry.
    """
    assert set(REVIEWED_ATTRIBUTE_KEYS) == set(PRODUCT_METRICS), (
        f"exported with no reviewed key set: {sorted(set(PRODUCT_METRICS) - set(REVIEWED_ATTRIBUTE_KEYS))}; "
        f"pinned but not exported: {sorted(set(REVIEWED_ATTRIBUTE_KEYS) - set(PRODUCT_METRICS))}"
    )


def _guide_attribute_keys():
    """The Attributes column of the guide's exported-metrics table, parsed."""
    import re
    from pathlib import Path

    guide = Path(__file__).resolve().parents[2] / "docs" / "guides" / "telemetry-otlp-export.md"
    rows = re.findall(
        r"^\| `(kirocrew\.[a-z0-9_.]+)` \| (?:counter|gauge|histogram) \| ([^|]+)\|",
        guide.read_text(encoding="utf-8"),
        re.M,
    )
    parsed = {}
    for name, column in rows:
        column = column.strip()
        parsed[name] = set() if column == "none" else set(re.findall(r"`([a-z0-9_]+)`", column))
    return parsed


def test_the_guide_publishes_the_same_attribute_keys_it_is_pinned_to():
    """What a person is told each metric carries has to BE what it carries.

    Reddens from either side: a key added to ``REVIEWED_ATTRIBUTE_KEYS`` without
    the table, or a table row edited without the pin. To see it fail, add one key
    to any entry of ``REVIEWED_ATTRIBUTE_KEYS`` above and run this test.
    """
    assert _guide_attribute_keys() == REVIEWED_ATTRIBUTE_KEYS


def test_the_wire_carries_the_reviewed_keys_and_only_the_client_label_beside_them():
    """On the wire: every reviewed key survives, and nothing else is added.

    Driven through the real narrowing wrapper the core puts on this destination's
    exporter, one metric at a time, so a key dropped by the narrowing and a key
    invented by it both fail here. ``ClientType`` is the one addition this
    destination declares, and it is asserted as the ONLY one.

    Two one-line changes redden this, which is what makes it a pin rather than a
    restatement: drop a key from an entry in ``REVIEWED_ATTRIBUTE_KEYS`` (the
    extra key then arrives unreviewed, and the equality fails), or add a second
    entry to ``kiro_export.POINT_ATTRIBUTES`` (every point then carries a label
    this test did not sanction).
    """
    for name, keys in sorted(REVIEWED_ATTRIBUTE_KEYS.items()):
        _, points, _ = _narrowed(
            [name],
            {"service.name": "kirocrew"},
            point_attributes={key: "value" for key in keys},
        )
        assert points, f"{name} exported no point"
        for point in points:
            assert set(point.attributes) == keys | {"ClientType"}, (
                f"{name} carries {sorted(set(point.attributes))} on the wire, "
                f"reviewed for {sorted(keys)} plus ClientType"
            )


def test_an_unreviewed_key_reaches_the_wire_rather_than_being_filtered():
    """The negative control, which is what makes the test above load-bearing.

    The narrowing drops metric NAMES, never attribute keys -- so a key added at a
    call site really does leave the machine, and the pin above is the only thing
    standing between that edit and an undisclosed export. Asserting the leak path
    is open is what proves the pin is not decoration.
    """
    name = "kirocrew.session.started"
    _, points, _ = _narrowed(
        [name],
        {"service.name": "kirocrew"},
        point_attributes={"session_source": "chat", "unreviewed_new_key": "x"},
    )
    assert points
    assert "unreviewed_new_key" in set(
        points[0].attributes
    ), "an unreviewed key was filtered out, so the pin above guards nothing"


def test_the_guide_lists_exactly_what_leaves_the_machine():
    """The disclosure and the allowlist are one fact, so they are asserted as one.

    What a person is told leaves their machine has to BE what leaves it. Without
    this, adding a name to the allowlist is a one-line change that silently
    widens the export and leaves the published list stale.
    """
    import re
    from pathlib import Path

    guide = Path(__file__).resolve().parents[2] / "docs" / "guides" / "telemetry-otlp-export.md"
    listed = set(
        re.findall(r"^\| `(kirocrew\.[a-z0-9_.]+)` \|", guide.read_text(encoding="utf-8"), re.M)
    )
    assert listed == set(PRODUCT_METRICS), (
        f"listed but not exported: {sorted(listed - set(PRODUCT_METRICS))}; "
        f"exported but not listed: {sorted(set(PRODUCT_METRICS) - listed)}"
    )


def test_a_failure_in_this_destinations_exporter_leaves_the_local_sink_alone():
    """The destination boundary holds: a raising exporter is one lost interval.

    The two readers are independent by construction -- the wrapper sits on this
    destination's exporter only -- and this drives that: the wrapped exporter
    raises, and the batch the local exporter was handed is untouched.
    """
    from kiro_crew.metrics import destination_filter

    class _Raising(_CapturingExporter):
        def export(self, metrics_data, timeout_millis=10_000, **kwargs):
            raise RuntimeError("the endpoint's exporter blew up")

    (dest,) = _destinations(_Cfg())
    wrapped = destination_filter.wrap_if_narrowed(dest, _Raising())
    local = _CapturingExporter()
    batch = _metrics_data([_ALLOWED_NAME, _LOCAL_ONLY_NAME], {"service.name": "kirocrew"})
    with pytest.raises(RuntimeError):
        wrapped.export(batch)
    local.export(batch)
    (local_batch,) = local.batches
    local_names = [
        metric.name
        for rm in local_batch.resource_metrics
        for sm in rm.scope_metrics
        for metric in sm.metrics
    ]
    assert sorted(local_names) == sorted([_ALLOWED_NAME, _LOCAL_ONLY_NAME])


# ---------------------------------------------------------------------------
# tier 3 - the real exporter, when the optional extra is installed
# ---------------------------------------------------------------------------


def _otlp_available():
    try:
        import opentelemetry.exporter.otlp.proto.http.metric_exporter  # noqa: F401
        from opentelemetry.proto.collector.metrics.v1 import (  # noqa: F401
            metrics_service_pb2,
        )
    except Exception:  # noqa: BLE001
        return False
    return True


requires_otlp = pytest.mark.skipif(
    not _otlp_available(),
    reason='the OTLP wire tier needs the optional extra: pip install "kirocrew[otlp]"',
)


@requires_otlp
@pytest.mark.parametrize(
    "region,expected_url",
    [("", US_ENDPOINT), ("eu-central-1", EU_ENDPOINT)],
)
def test_one_export_posts_one_allowlisted_payload_and_leaves_the_local_sink_whole(
    monkeypatch, tmp_path, region, expected_url
):
    """The whole path: live build, real exporter, decoded protobuf, no network.

    The adapter underneath ``requests`` answers the POST, so nothing leaves the
    process and nothing listens for it.
    """
    from unittest.mock import patch

    from opentelemetry.proto.collector.metrics.v1 import metrics_service_pb2

    from kiro_crew.config.loader import KiroCrewConfig
    from kiro_crew.metrics import inventory_gauges as ig
    from kiro_crew.metrics import provider as pm

    _sign_in(monkeypatch, _token(profile_arn=FAKE_ARN))
    _install_id(monkeypatch)
    adapter = _Adapter((200,))

    def _session_factory():
        session = kiro_export.KiroExportSession()
        session.mount("https://", adapter)
        return session

    monkeypatch.setattr(kiro_export, "build_session", _session_factory)

    cfg = KiroCrewConfig()
    cfg.telemetry.enabled = True
    cfg.telemetry.kiro_export = True
    cfg.telemetry.kiro_export_region = region
    cfg.telemetry.export_interval_seconds = 3600  # only force_flush exports

    monkeypatch.setenv("KIROCREW_TELEMETRY", "1")
    ig.reset_for_testing()
    ig.mark_install_reporter()
    pm.reset_for_testing()
    try:
        with patch.object(KiroCrewConfig, "load", return_value=cfg):
            with patch.object(pm, "_default_metrics_dir", return_value=tmp_path):
                recorder = pm.get_recorder()
                assert recorder.enabled
                assert pm._provider is not None
                pm._provider.force_flush()
    finally:
        pm.reset_for_testing()
        ig.reset_for_testing()

    assert adapter.sent, "the destination posted nothing"
    names = set()
    resource_keys = set()
    client_types = set()
    temporalities = set()
    for request, kwargs in adapter.sent:
        assert request.url == expected_url
        assert kwargs["timeout"] == 10.0
        assert request.headers["Authorization"] == f"Bearer {FAKE_TOKEN}"
        assert request.headers["x-kiro-profile-arn"] == FAKE_ARN
        assert "x-kiro-machineid" not in request.headers
        payload = metrics_service_pb2.ExportMetricsServiceRequest()
        payload.ParseFromString(request.body)
        for rm in payload.resource_metrics:
            for attr in rm.resource.attributes:
                resource_keys.add(attr.key)
            for sm in rm.scope_metrics:
                for metric in sm.metrics:
                    names.add(metric.name)
                    if metric.WhichOneof("data") == "sum":
                        temporalities.add(metric.sum.aggregation_temporality)
                    for point in list(metric.sum.data_points) + list(metric.gauge.data_points):
                        values = {a.key: a.value.string_value for a in point.attributes}
                        client_types.add(values.get("ClientType"))

    assert names, "the payload carried no metric"
    assert names <= set(
        PRODUCT_METRICS
    ), f"not on the allowlist: {sorted(names - set(PRODUCT_METRICS))}"
    assert client_types == {"kiro-crew"}, f"ClientType is not on every point: {client_types}"
    from opentelemetry.proto.metrics.v1 import metrics_pb2

    assert temporalities <= {
        metrics_pb2.AggregationTemporality.AGGREGATION_TEMPORALITY_DELTA
    }, f"a sum left this destination non-delta: {temporalities}"
    assert (
        resource_keys == kiro_export.RESOURCE_ALLOW
    ), f"resource keys on the wire: {sorted(resource_keys)}"
    assert "service.instance.id" not in resource_keys
    assert not [key for key in resource_keys if key.startswith("host.")]

    # The local sink is untouched: it still has instruments this destination may
    # not have, and the token is nowhere in it.
    shards = sorted(tmp_path.glob("metrics-*.jsonl"))
    assert shards, "the local sink wrote nothing"
    local_text = "\n".join(shard.read_text(encoding="utf-8") for shard in shards)
    assert FAKE_TOKEN not in local_text
    assert FAKE_ARN not in local_text
    assert "ClientType" not in local_text, "a per-destination attribute reached the local sink"
    assert "kirocrew.process." in local_text, "the local sink lost its local-only instruments"


@requires_otlp
@pytest.mark.parametrize(
    "off",
    [
        {"enabled": False},
        {"kiro_export": False},
        {"kiro_export_region": "us-west-2"},
    ],
)
def test_nothing_is_posted_when_any_single_gate_is_off(monkeypatch, tmp_path, off):
    """Driven through the live build, because that is where egress would start."""
    from unittest.mock import patch

    from kiro_crew.config.loader import KiroCrewConfig
    from kiro_crew.metrics import inventory_gauges as ig
    from kiro_crew.metrics import provider as pm

    _sign_in(monkeypatch, _token())
    _install_id(monkeypatch)
    adapter = _Adapter((200,))

    def _session_factory():
        session = kiro_export.KiroExportSession()
        session.mount("https://", adapter)
        return session

    monkeypatch.setattr(kiro_export, "build_session", _session_factory)

    cfg = KiroCrewConfig()
    cfg.telemetry.enabled = True
    cfg.telemetry.kiro_export = True
    cfg.telemetry.export_interval_seconds = 3600
    for key, value in off.items():
        setattr(cfg.telemetry, key, value)

    monkeypatch.setenv("KIROCREW_TELEMETRY", "1")
    ig.reset_for_testing()
    pm.reset_for_testing()
    try:
        with patch.object(KiroCrewConfig, "load", return_value=cfg):
            with patch.object(pm, "_default_metrics_dir", return_value=tmp_path):
                pm.get_recorder()
                if pm._provider is not None:
                    pm._provider.force_flush()
    finally:
        pm.reset_for_testing()
        ig.reset_for_testing()

    assert adapter.sent == [], "metrics left the machine with a gate switched off"


@requires_otlp
@pytest.mark.parametrize(
    "env,value",
    [
        (kiro_export.DO_NOT_TRACK_ENV, "1"),
        (kiro_export.DISABLE_ENV, "0"),
        (kiro_export.TEST_MODE_ENV, "1"),
    ],
)
def test_nothing_is_posted_when_the_environment_says_no(monkeypatch, tmp_path, env, value):
    monkeypatch.setenv(env, value)
    test_nothing_is_posted_when_any_single_gate_is_off(monkeypatch, tmp_path, {})


# ---------------------------------------------------------------------------
# governance audit of the shared gate
# ---------------------------------------------------------------------------
#
# Every enforcement decision this destination takes on the administrator's
# telemetry ceiling leaves a governance decision record; the description of
# posture does not. Each guarantee below names the one-line change that reddens
# its test:
#
# * Build path audits every call -- drop ``audit_tool=AUDIT_TOOL_BUILD`` from the
#   latch-less branch of ``outbound_permitted``:
#   test_a_build_denial_writes_a_governance_record.
# * The first export compares against the build verdict, and steady exports add
#   nothing -- start ``_AuditLatch`` at 0, or delete the ``latch.differs`` check
#   so every export audits: test_steady_exports_write_no_audit_record.
# * A ceiling arriving mid-process writes exactly one denied record and stops the
#   send; lifting it writes one allowed record -- delete ``latch.record(...)``
#   (every later export re-audits) or the audited re-ask (no record at all):
#   test_a_ceiling_mid_process_writes_one_record_per_change.
# * Disclosure writes none -- remove the ``disclosure_only()`` block from
#   ``provider.otlp_egress_active``: test_a_posture_read_writes_no_audit_record.


def _audited_gate(monkeypatch):
    """The REAL shared gate, with the ceiling and its audit seam observable.

    Only the governance evaluation is replaced: ``vet_and_audit`` (the audited
    seam, one call = one record) records the call, ``governance_permits`` (the
    unaudited read) does not. Everything between this destination and those two
    -- ``outbound_permitted``, ``telemetry_permitted``,
    ``is_governance_pinned_off`` -- is the product's own code. CI, the home check
    and the first-send stamp are neutralised so the ceiling is the only thing
    deciding.
    """
    from types import SimpleNamespace

    from kiro_crew import beacon

    state = {"pinned": False}
    records = []

    def _decision():
        pinned = state["pinned"]
        return SimpleNamespace(
            permitted=not pinned, layer="policy" if pinned else "", reason="", rule=""
        )

    def _vet_and_audit(scope, item, *, tool_name, **_):
        decision = _decision()
        records.append((tool_name, "allowed" if decision.permitted else "denied"))
        return decision

    _real_gate(monkeypatch)
    monkeypatch.setattr(beacon, "is_ci", lambda: False)
    monkeypatch.setattr(beacon, "is_default_home", lambda: True)
    monkeypatch.setattr(beacon, "is_first_send", lambda: False)
    monkeypatch.setattr(beacon, "vet_and_audit", _vet_and_audit)
    monkeypatch.setattr(beacon, "governance_permits", lambda *a, **k: _decision())
    return state, records


def test_a_build_denial_writes_a_governance_record(monkeypatch):
    state, records = _audited_gate(monkeypatch)
    state["pinned"] = True
    assert _destinations(_Cfg()) == ()
    assert records == [(kiro_export.AUDIT_TOOL_BUILD, "denied")]

    state["pinned"] = False
    (dest,) = _destinations(_Cfg())
    assert dest.endpoint == US_ENDPOINT
    assert records[-1] == (kiro_export.AUDIT_TOOL_BUILD, "allowed")


def test_steady_exports_write_no_audit_record(monkeypatch):
    """The first export matches the build's verdict, and five more change nothing."""
    _, records = _audited_gate(monkeypatch)
    _sign_in(monkeypatch, _token())
    session, adapter = _session()
    _post(session, times=6)
    assert len(adapter.sent) == 6
    assert records == []


def test_a_ceiling_mid_process_writes_one_record_per_change(monkeypatch):
    state, records = _audited_gate(monkeypatch)
    _sign_in(monkeypatch, _token())
    session, adapter = _session()
    _post(session)
    assert len(adapter.sent) == 1 and records == []

    state["pinned"] = True
    _post(session)
    assert len(adapter.sent) == 1, "an export left after the ceiling arrived"
    assert records == [(kiro_export.AUDIT_TOOL_SEND, "denied")]
    _post(session, times=4)
    assert len(adapter.sent) == 1
    assert records == [(kiro_export.AUDIT_TOOL_SEND, "denied")], "a repeat denial re-audited"

    state["pinned"] = False
    _post(session)
    assert len(adapter.sent) == 2, "the export did not resume after the ceiling lifted"
    assert records == [
        (kiro_export.AUDIT_TOOL_SEND, "denied"),
        (kiro_export.AUDIT_TOOL_SEND, "allowed"),
    ]
    _post(session, times=3)
    assert len(adapter.sent) == 5 and len(records) == 2


def test_a_new_session_starts_from_the_build_verdict(monkeypatch):
    """The audit state lives on the session, so a rebuild does not inherit it."""
    state, records = _audited_gate(monkeypatch)
    _sign_in(monkeypatch, _token())
    first, _ = _session()
    state["pinned"] = True
    _post(first)
    assert records == [(kiro_export.AUDIT_TOOL_SEND, "denied")]
    state["pinned"] = False
    second, adapter = _session()
    _post(second)
    assert len(adapter.sent) == 1
    assert records == [(kiro_export.AUDIT_TOOL_SEND, "denied")]


def test_a_posture_read_writes_no_audit_record(monkeypatch):
    from kiro_crew.metrics.provider import otlp_egress_active

    state, records = _audited_gate(monkeypatch)
    assert otlp_egress_active(_Cfg()) is True
    state["pinned"] = True
    # Still egress: a policy pin can be lifted, so the configured export counts.
    assert otlp_egress_active(_Cfg()) is True
    assert records == []
