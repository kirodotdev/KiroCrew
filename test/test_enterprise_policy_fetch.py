"""The enterprise build's policy transport against a fake control plane.

The fake answers behind ``policy_distribution._open``, the one seam the network call goes
through, so nothing opens a socket. The sign-in is a real vault entry in a temporary data
home. Each enterprise failure is driven through boot to ``refuse_policy_boot`` to pin
that it exits with a message naming what failed.
"""

from __future__ import annotations

import io
import json
import os
import ssl
import urllib.error
from datetime import datetime, timedelta, timezone
from email.message import Message

import pytest
from governance_distribution_helpers import _hermetic_governance_globals  # noqa: F401
from governance_distribution_helpers import install_ceiling_fixture  # noqa: F401

from kiro_crew import enterprise_boot
from kiro_crew.auth.store import KasToken, TokenStore
from kiro_crew.platform import enterprise_policy_fetch as epf
from kiro_crew.platform import governance
from kiro_crew.platform import policy_distribution as pd
from kiro_crew.platform.context import PlatformCompositionError

_ARN = "arn:aws:codewhisperer:us-east-1:123456789012:profile/ABCDEFGHIJKL"
_EU_ARN = "arn:aws:codewhisperer:eu-central-1:123456789012:profile/ABCDEFGHIJKL"
_TOKEN = "fake-access-token-value"
_US_HOST = "https://management.us-east-1.kiro.dev/"
_EU_HOST = "https://management.eu-central-1.kiro.dev/"

_POLICY = """\
version: "0.1"
rules:
  - id: crew-channels
    capability: messaging_channel
    effect: allow
    match: ["slack"]
  - id: crew-channels-deny
    capability: messaging_channel
    effect: deny
    exclude: ["slack"]
  - id: other-client
    capability: shell
    effect: deny
"""


def _envelope(
    policy: str = _POLICY, *, status: str = "ENABLED", digest: str = "", configuration=()
) -> bytes:
    hexdigest = epf.body_digest(policy, tuple(configuration))
    return json.dumps(
        {
            "enterpriseManagementStatus": status,
            "manifest": {
                "hash": "sha256:" + (digest or hexdigest),
                "versionId": "v1:" + hexdigest,
                "generatedAt": "2026-10-26T00:00:00Z",
            },
            "body": {"policy": policy, "configuration": list(configuration)},
        }
    ).encode("utf-8")


class _Resp:
    def __init__(self, body: bytes, status: int = 200) -> None:
        self.status = status
        self._body = io.BytesIO(body)

    def read(self, n: int = -1) -> bytes:
        return self._body.read(n)

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False


class FakeControlPlane:
    """Records each request and answers with the next configured reply."""

    def __init__(self) -> None:
        self.requests: list = []
        self.reply: object = _envelope()

    def __call__(self, req, timeout):
        self.requests.append(req)
        reply = self.reply
        if isinstance(reply, BaseException):
            raise reply
        if isinstance(reply, tuple):
            status, body = reply
            return _Resp(body, status)
        return _Resp(reply)  # type: ignore[arg-type]

    def payload(self, index: int = -1) -> dict:
        return json.loads(self.requests[index].data.decode("utf-8"))


def _http_error(code: int, body: dict) -> urllib.error.HTTPError:
    return urllib.error.HTTPError(
        _US_HOST, code, "error", Message(), io.BytesIO(json.dumps(body).encode("utf-8"))
    )


@pytest.fixture(name="vault")
def vault_fixture(tmp_path, monkeypatch):
    store = TokenStore(tmp_path / "data")
    monkeypatch.setattr("kiro_crew.auth.bridge.default_token_store", lambda: store)
    monkeypatch.delenv("KIRO_API_KEY", raising=False)

    def sign_in(profile_arn: str | None = _ARN, *, auth_method: str = "identity_center") -> None:
        store.save(
            KasToken(
                access_token=_TOKEN,
                expires_at=datetime.now(timezone.utc) + timedelta(hours=1),
                provider="Enterprise",
                identity="external_idp" if auth_method == "external_idp" else "identity_center",
                profile_arn=profile_arn,
                auth_method=auth_method,
                token_endpoint="https://idp.example.invalid/token",
            )
        )

    return sign_in


@pytest.fixture(name="cps")
def cps_fixture(monkeypatch):
    fake = FakeControlPlane()
    monkeypatch.setattr(pd, "_open", fake)
    return fake


@pytest.fixture(name="enterprise")
def enterprise_fixture(monkeypatch):
    monkeypatch.setattr("kiro_crew.edition.EDITION", "enterprise")


@pytest.fixture(name="kills", autouse=True)
def kills_fixture(_floor_monkeypatch):
    """Record the stop a running gateway sends itself on an account change."""
    sent: list = []
    _floor_monkeypatch.setattr(epf.signal, "raise_signal", lambda sig: sent.append(sig))
    return sent


def _restart(monkeypatch) -> None:
    """A new process: it pins whichever profile is signed in when it starts."""
    monkeypatch.delenv(epf.PROFILE_PIN_ENV, raising=False)
    monkeypatch.setattr(epf, "_pinned", "")
    monkeypatch.setattr(pd, "_claim_fetch_slot", lambda window: True)


def _boot_exit_message(capsys) -> str:
    """Boot as the CLI does and return what the refused boot printed."""
    with pytest.raises(SystemExit) as exited:
        try:
            governance.load_security_policy()
        except PlatformCompositionError as exc:
            enterprise_boot.refuse_policy_boot(exc)
            raise
    assert exited.value.code == enterprise_boot.EXIT_CODE
    err = capsys.readouterr().err
    assert _TOKEN not in err
    return err


def _request(etag: str = "") -> pd.FetchRequest:
    return pd.FetchRequest(url=pd.ENTERPRISE_POLICY_URL, etag=etag)


class TestSuccess:
    def test_boot_installs_the_translated_policy(self, enterprise, vault, cps):
        vault()
        ceiling = governance.load_security_policy()
        assert ceiling is not None
        doc = json.loads(pd.read_cache().body)
        assert doc["channels"] == {"members": {"mode": "allow", "allow": ["slack"]}}
        assert doc["distribution"] == {"refresh_interval_secs": 300}
        assert "shell" not in doc

    def test_the_request_is_the_signed_in_user_reading_as_crew(self, enterprise, vault, cps):
        vault()
        epf.fetch(_request())
        (req,) = cps.requests
        assert req.full_url == _US_HOST
        assert req.get_method() == "POST"
        headers = {k.lower(): v for k, v in req.header_items()}
        assert headers["authorization"] == f"Bearer {_TOKEN}"
        assert headers["x-amz-target"] == epf.TARGET
        assert headers["content-type"] == "application/x-amz-json-1.0"
        assert "tokentype" not in headers
        assert cps.payload() == {
            "profileArn": _ARN,
            "clientSurface": "CREW",
            "clientVersion": epf.__version__,
        }

    def test_an_external_idp_sign_in_sends_its_token_type(self, enterprise, vault, cps):
        vault(auth_method="external_idp")
        epf.fetch(_request())
        headers = {k.lower(): v for k, v in cps.requests[0].header_items()}
        assert headers["tokentype"] == "EXTERNAL_IDP"

    def test_an_unchanged_answer_keeps_the_cached_copy(self, enterprise, vault, cps):
        vault()
        first = epf.fetch(_request())
        cps.reply = json.dumps(
            {
                "enterpriseManagementStatus": "UNCHANGED",
                "manifest": {"versionId": epf.cached_version(first.etag)},
            }
        ).encode()
        second = epf.fetch(_request(first.etag))
        assert second.not_modified and second.etag == first.etag
        assert cps.payload()["manifestVersionId"] == epf.cached_version(first.etag)

    def test_another_profiles_cached_version_is_not_sent(self, enterprise, vault, cps, monkeypatch):
        vault()
        first = epf.fetch(_request())
        vault(_EU_ARN)
        _restart(monkeypatch)
        epf.fetch(_request(first.etag))
        assert "manifestVersionId" not in cps.payload()

    def test_disabled_answers_a_document_that_governs_nothing(self, enterprise, vault, cps):
        vault()
        cps.reply = json.dumps({"enterpriseManagementStatus": "DISABLED"}).encode()
        doc = json.loads(epf.fetch(_request()).body)
        assert "channels" not in doc
        assert governance.parse_policy(doc) is not None


_DISABLED = json.dumps({"enterpriseManagementStatus": "DISABLED"}).encode()


class TestDisabledRefresh:
    """Smoke test N2: an unchanged DISABLED answer installs nothing."""

    @pytest.fixture(name="installs")
    def installs_fixture(self, monkeypatch):
        """Each ceiling a refresh installs. Composing needs a booted gateway, so the
        compose steps are stubbed: what is under test is whether an install happens."""
        installed: list = []
        monkeypatch.setattr(pd, "validate_ceiling", lambda ceiling: None)
        monkeypatch.setattr(pd, "apply_ceiling", installed.append)
        monkeypatch.setattr(pd, "_recompose_differs", lambda ceiling: False)
        return installed

    def test_disabled_twice_installs_once(self, enterprise, vault, cps, installs):
        vault()
        cps.reply = _DISABLED
        assert pd.refresh_now(force=True).status == pd.REFRESH_APPLIED
        outcome = pd.refresh_now()
        assert outcome.status == pd.REFRESH_UNCHANGED, outcome.detail
        assert len(installs) == 1
        assert "manifestVersionId" not in cps.payload()

    def test_disabled_then_a_policy_installs(self, enterprise, vault, cps, installs):
        vault()
        cps.reply = _DISABLED
        governance.load_security_policy()
        cps.reply = _envelope()
        outcome = pd.refresh_now()
        assert outcome.status == pd.REFRESH_APPLIED, outcome.detail
        assert len(installs) == 1
        assert "channels" in json.loads(pd.read_cache().body)

    def test_a_policy_then_disabled_installs(self, enterprise, vault, cps, installs):
        vault()
        governance.load_security_policy()
        cps.reply = _DISABLED
        outcome = pd.refresh_now()
        assert outcome.status == pd.REFRESH_APPLIED, outcome.detail
        assert len(installs) == 1
        assert "channels" not in json.loads(pd.read_cache().body)

    def test_another_profiles_disabled_tag_is_not_unchanged(
        self, enterprise, vault, cps, monkeypatch
    ):
        vault()
        cps.reply = _DISABLED
        first = epf.fetch(_request())
        vault(_EU_ARN)
        _restart(monkeypatch)
        second = epf.fetch(_request(first.etag))
        assert not second.not_modified and second.body and second.etag != first.etag


class TestWrongHost:
    def test_the_host_comes_from_the_region_table(self, enterprise, vault, cps, monkeypatch):
        """No environment variable or local setting moves the address."""
        monkeypatch.setenv(pd.POLICY_URL_ENV, "https://elsewhere.example.invalid/p.json")
        monkeypatch.setenv(pd.POLICY_HEADERS_ENV, '{"Authorization": "Bearer other-secret"}')
        monkeypatch.setenv("KIROCREW_EM_ENDPOINT", "https://elsewhere.example.invalid/")
        vault(_EU_ARN)
        governance.load_security_policy()
        (req,) = cps.requests
        assert req.full_url == _EU_HOST
        assert req.get_header("Authorization") == f"Bearer {_TOKEN}"

    def test_an_unknown_region_still_uses_a_table_host(self, enterprise, vault, cps):
        vault("arn:aws:codewhisperer:ap-south-9:123456789012:profile/ABCDEFGHIJKL")
        epf.fetch(_request())
        assert cps.requests[0].full_url == _US_HOST

    def test_a_certificate_for_another_host_exits(self, enterprise, vault, cps, capsys):
        vault()
        cps.reply = urllib.error.URLError(ssl.SSLCertVerificationError("hostname mismatch"))
        err = _boot_exit_message(capsys)
        assert "TLS certificate did not verify" in err

    def test_a_redirect_exits(self, enterprise, vault, cps, capsys):
        vault()
        cps.reply = _http_error(302, {})
        assert "redirect" in _boot_exit_message(capsys)


class TestAuthFailure:
    def test_a_refused_token_exits(self, enterprise, vault, cps, capsys):
        vault()
        cps.reply = _http_error(
            403, {"__type": "com.amazon#AccessDeniedException", "message": f"bad {_TOKEN}"}
        )
        err = _boot_exit_message(capsys)
        assert "refused the Kiro sign-in" in err
        assert "AccessDeniedException" in err

    def test_no_sign_in_exits_without_calling_the_service(self, enterprise, vault, cps, capsys):
        err = _boot_exit_message(capsys)
        assert "no Kiro sign-in was found" in err
        assert "kas login --provider idc" in err
        assert cps.requests == []

    def test_an_api_key_does_not_stand_in_for_the_sign_in(
        self, enterprise, vault, cps, monkeypatch, capsys
    ):
        monkeypatch.setenv("KIRO_API_KEY", "an-api-key-value")
        assert "no Kiro sign-in was found" in _boot_exit_message(capsys)
        assert cps.requests == []

    def test_a_sign_in_with_no_profile_exits(self, enterprise, vault, cps, capsys):
        vault(None, auth_method="external_idp")
        assert "has no profile" in _boot_exit_message(capsys)

    def test_an_account_with_no_enterprise_management_exits(self, enterprise, vault, cps, capsys):
        vault()
        cps.reply = json.dumps({"enterpriseManagementStatus": "NOT_ELIGIBLE"}).encode()
        assert "has no enterprise management" in _boot_exit_message(capsys)


class TestUnreachable:
    @pytest.mark.parametrize(
        "error",
        [
            urllib.error.URLError(OSError("Name or service not known")),
            TimeoutError("timed out"),
            ConnectionRefusedError("refused"),
        ],
    )
    def test_an_unreachable_service_exits(self, enterprise, vault, cps, capsys, error):
        vault()
        cps.reply = error
        assert "could not be reached" in _boot_exit_message(capsys)

    def test_a_server_error_exits(self, enterprise, vault, cps, capsys):
        vault()
        cps.reply = _http_error(500, {"__type": "InternalServerException"})
        assert "HTTP 500 InternalServerException" in _boot_exit_message(capsys)


class TestBadFormat:
    @pytest.mark.parametrize(
        "body, expected",
        [
            (b"not json", "not JSON"),
            (b"[]", "not a JSON object"),
            (b'{"enterpriseManagementStatus": "MAYBE"}', "unknown status"),
            (b'{"enterpriseManagementStatus": "ENABLED", "body": {}}', "carries no policy"),
            (
                b'{"enterpriseManagementStatus": "UNCHANGED", "manifest": {"versionId": "v1:x"}}',
                "version not sent",
            ),
        ],
    )
    def test_a_malformed_answer_exits(self, enterprise, vault, cps, capsys, body, expected):
        vault()
        cps.reply = body
        assert expected in _boot_exit_message(capsys)

    def test_a_policy_that_is_not_yaml_rules_exits(self, enterprise, vault, cps, capsys):
        vault()
        cps.reply = _envelope("rules: {not: a list}")
        assert "cannot be applied" in _boot_exit_message(capsys)

    def test_an_unrepresentable_policy_exits(self, enterprise, vault, cps, capsys):
        vault()
        cps.reply = _envelope(
            "rules:\n"
            "  - {capability: messaging_channel, effect: deny, exclude: ['s*']}\n"
            "  - {capability: messaging_channel, effect: deny, match: ['slack']}\n"
        )
        assert "inside an allowed glob" in _boot_exit_message(capsys)

    def test_an_oversized_answer_exits(self, enterprise, vault, cps, capsys):
        vault()
        cps.reply = b"x" * (epf.MAX_ENVELOPE_BYTES + 1)
        assert "exceeds" in _boot_exit_message(capsys)


class TestShaMismatch:
    def test_a_body_that_does_not_match_its_hash_exits(self, enterprise, vault, cps, capsys):
        vault()
        cps.reply = _envelope(digest="0" * 64)
        assert "does not match its own sha256 hash" in _boot_exit_message(capsys)

    def test_an_answer_with_no_hash_is_accepted(self, enterprise, vault, cps):
        vault()
        cps.reply = json.dumps(
            {"enterpriseManagementStatus": "ENABLED", "body": {"policy": _POLICY}}
        ).encode()
        doc = json.loads(epf.fetch(_request()).body)
        assert doc["channels"]["members"]["allow"] == ["slack"]

    def test_the_hash_covers_the_configuration(self, enterprise, vault, cps, capsys):
        vault()
        envelope = json.loads(_envelope())
        envelope["body"]["configuration"] = [
            {"key": "k", "value": "v", "source": "s", "composeBehavior": "c"}
        ]
        cps.reply = json.dumps(envelope).encode()
        assert "sha256" in _boot_exit_message(capsys)


class TestProfileBoundCache:
    def test_a_profile_switch_does_not_serve_the_previous_cache(
        self, enterprise, vault, cps, monkeypatch
    ):
        """A fresh cache from another profile is not the shortcut: the new profile is read."""
        vault()
        governance.load_security_policy()
        vault(_EU_ARN)
        _restart(monkeypatch)
        cps.reply = _envelope("rules: []\n")
        governance.load_security_policy()
        assert [r.full_url for r in cps.requests] == [_US_HOST, _EU_HOST]
        assert "channels" not in json.loads(pd.read_cache().body)

    def test_the_same_profile_is_served_from_a_fresh_cache(self, enterprise, vault, cps):
        vault()
        governance.load_security_policy()
        governance.load_security_policy()
        assert len(cps.requests) == 1

    def test_another_profiles_cache_is_not_the_outage_fallback(
        self, enterprise, vault, cps, capsys, monkeypatch
    ):
        vault()
        governance.load_security_policy()
        vault(_EU_ARN)
        _restart(monkeypatch)
        cps.reply = TimeoutError("down")
        assert "could not be reached" in _boot_exit_message(capsys)

    def test_another_processes_cache_for_another_profile_is_not_adopted(
        self, enterprise, vault, cps, monkeypatch
    ):
        """The concurrent-writer handoff checks the profile as well as the source."""
        from kiro_crew.platform import policy_distribution_boot as boot

        vault()
        governance.load_security_policy()
        winner = pd.read_cache()
        vault(_EU_ARN)
        _restart(monkeypatch)
        dist = pd.resolve_distribution()
        outcome = boot._load_from_cached_winner(dist, winner)
        assert outcome.ceiling is None
        assert "source or sign-in" in outcome.reason

    def test_the_outage_fallback_refuses_a_changed_account(self, enterprise, vault, cps):
        """A switch while this process runs: the pinned profile's cache is not served."""
        from kiro_crew.platform import policy_distribution_boot as boot

        vault()
        governance.load_security_policy()
        cached = pd.read_cache()
        vault(_EU_ARN)
        dist = pd.resolve_distribution()
        with pytest.raises(PlatformCompositionError, match="account changed"):
            boot._from_cache_on_outage(dist, cached, "the fetch failed")


class TestPinnedProfile:
    """The profile is pinned when the process starts; a later sign-in change is refused."""

    def test_a_cache_only_child_adopts_only_its_gateways_profile(
        self, enterprise, vault, cps, monkeypatch
    ):
        """Local review case 1: another process published B's copy; a child of A refuses it."""
        vault(_EU_ARN)
        governance.load_security_policy()  # another process, pinned to B, writes B's copy
        pinned_b = os.environ[epf.PROFILE_PIN_ENV]
        vault()
        _restart(monkeypatch)
        epf.pinned_scope()  # the gateway pins A
        pinned_a = os.environ[epf.PROFILE_PIN_ENV]
        assert pinned_a != pinned_b
        monkeypatch.setenv(pd.POLICY_CACHE_ONLY_ENV, "1")  # its child inherits A's pin
        with pytest.raises(PlatformCompositionError, match="no cached ceiling"):
            pd.load_distributed_policy()
        monkeypatch.setenv(epf.PROFILE_PIN_ENV, pinned_b)  # B's own child adopts it
        assert pd.load_distributed_policy() is not None

    def test_a_child_with_no_pin_adopts_nothing(self, enterprise, vault, cps, monkeypatch):
        vault()
        governance.load_security_policy()
        _restart(monkeypatch)
        monkeypatch.setenv(pd.POLICY_CACHE_ONLY_ENV, "1")
        with pytest.raises(PlatformCompositionError, match="no cached ceiling"):
            pd.load_distributed_policy()

    def test_a_running_gateway_stops_on_an_account_change(
        self, install_ceiling, enterprise, vault, cps, kills, monkeypatch
    ):
        """Local review case 2: the next install after a switch refuses and stops Crew."""
        monkeypatch.setattr(pd, "central_ceiling_installed", lambda: True)  # a running gateway
        vault()
        epf.pinned_scope()
        install_ceiling(governance.parse_policy(json.loads(epf.fetch(_request()).body)))
        vault(_EU_ARN)
        outcome = pd.refresh_now(force=True)
        assert outcome.status == pd.REFRESH_REJECTED
        assert "account changed" in outcome.detail
        assert kills == [epf.signal.SIGTERM]

    def test_a_reexec_pins_its_own_sign_in(self, enterprise, vault, cps, monkeypatch):
        """An in-app restart keeps the environment, and with it the old pin; it must boot."""
        vault()
        governance.load_security_policy()
        old_pin = os.environ[epf.PROFILE_PIN_ENV]
        vault(_EU_ARN)
        monkeypatch.setattr(epf, "_pinned", "")  # a fresh module, same environment
        monkeypatch.setattr(pd, "_claim_fetch_slot", lambda window: True)
        assert governance.load_security_policy() is not None
        assert cps.requests[-1].full_url == _EU_HOST
        assert os.environ[epf.PROFILE_PIN_ENV] != old_pin

    def test_a_sign_out_is_refused_as_an_account_change(
        self, install_ceiling, enterprise, vault, cps, kills, monkeypatch
    ):
        """Not retried as unreachable: the next refresh refuses it and stops Crew."""
        monkeypatch.setattr(pd, "central_ceiling_installed", lambda: True)
        vault()
        epf.pinned_scope()
        install_ceiling(governance.parse_policy(json.loads(epf.fetch(_request()).body)))
        # A real sign-out: the vault holds no identity any more.
        from kiro_crew.auth.bridge import default_token_store

        store = default_token_store()
        store.delete(store.resolve().identity)
        assert store.holds_no_identity()
        outcome = pd.refresh_now(force=True)
        assert outcome.status == pd.REFRESH_REJECTED
        assert "account changed" in outcome.detail
        assert kills == [epf.signal.SIGTERM]

    def test_the_pin_does_not_follow_a_switch(self, enterprise, vault, cps):
        vault()
        first = epf.pinned_scope()
        vault(_EU_ARN)
        assert epf.pinned_scope() == first
        assert epf.signed_in_scope() != first


class TestSwitchDuringASuccessfulFetch:
    """The profile is in the cache's source identity, so a reply read for A after a
    switch to B is recorded as A's and is refused for B on every path."""

    def _switching(self, cps, vault, reply):
        def answer(req, timeout):
            cps.requests.append(req)
            vault(_EU_ARN)  # the switch lands while the request is in flight
            return _Resp(reply)

        return answer

    @pytest.mark.parametrize(
        "reply",
        [_envelope(), json.dumps({"enterpriseManagementStatus": "DISABLED"}).encode()],
        ids=["enabled", "disabled"],
    )
    def test_boot_refuses_the_reply(self, enterprise, vault, cps, monkeypatch, capsys, reply):
        vault()
        monkeypatch.setattr(pd, "_open", self._switching(cps, vault, reply))
        assert "account changed" in _boot_exit_message(capsys)
        assert pd.read_cache() is None

    def test_a_refresh_refuses_an_unchanged_reply(self, enterprise, vault, cps, monkeypatch):
        vault()
        governance.load_security_policy()
        version = epf.cached_version(pd.read_cache().etag)
        unchanged = json.dumps(
            {"enterpriseManagementStatus": "UNCHANGED", "manifest": {"versionId": version}}
        ).encode()
        monkeypatch.setattr(pd, "_open", self._switching(cps, vault, unchanged))
        outcome = pd.refresh_now()
        assert outcome.status == pd.REFRESH_REJECTED
        assert "account changed" in outcome.detail

    def test_a_refresh_refuses_an_enabled_reply_and_keeps_the_cache(
        self, enterprise, vault, cps, monkeypatch
    ):
        vault()
        governance.load_security_policy()
        before = pd.read_cache().body
        monkeypatch.setattr(pd, "_open", self._switching(cps, vault, _envelope()))
        outcome = pd.refresh_now(force=True)
        assert outcome.status == pd.REFRESH_REJECTED
        assert "account changed" in outcome.detail
        assert pd.read_cache().body == before

    def test_no_sign_in_matches_no_cache(self, enterprise, vault, cps, monkeypatch):
        """Signed out: the identity matches no recorded copy, so nothing cached is served."""
        from kiro_crew.platform import policy_distribution_source as src

        vault()
        governance.load_security_policy()
        body = pd.read_cache().body
        monkeypatch.setattr("kiro_crew.auth.bridge.default_token_store", lambda: None)
        _restart(monkeypatch)
        assert src._source_identity(pd.ENTERPRISE_POLICY_URL) != src._source_identity(
            pd.ENTERPRISE_POLICY_URL, body
        )
        assert src._source_identity(pd.ENTERPRISE_POLICY_URL, b"{}") != src._source_identity(
            pd.ENTERPRISE_POLICY_URL
        )

    def test_oss_sources_are_unchanged(self):
        from kiro_crew.platform import policy_distribution_source as src

        for url in ("https://p.example/x.json", "file:///etc/p.json"):
            assert src._source_identity(url) == url == src._source_identity(url, b"{}")


class TestTranslation:
    @pytest.mark.parametrize(
        "excludes, expected",
        [
            ([["s*"], ["slack"]], ["slack"]),
            ([["slack", "teams"], ["SLACK"]], ["slack"]),
            ([["s*"], ["s*", "teams"]], ["s*"]),
            ([["slack"], ["teams"]], []),
        ],
    )
    def test_excludes_intersect_by_what_they_admit(self, excludes, expected):
        from kiro_crew.platform import enterprise_policy_translate as t

        rules = [
            {"capability": "messaging_channel", "effect": "deny", "exclude": e} for e in excludes
        ]
        assert t.ruleset(rules, "messaging_channel") == {"mode": "allow", "allow": expected}

    def test_a_denied_glob_removes_the_literals_it_matches(self):
        from kiro_crew.platform import enterprise_policy_translate as t

        rules = [
            {"capability": "messaging_channel", "effect": "deny", "exclude": ["slack", "teams"]},
            {"capability": "messaging_channel", "effect": "deny", "match": ["s*"]},
        ]
        assert t.ruleset(rules, "messaging_channel") == {"mode": "allow", "allow": ["teams"]}

    def test_a_denied_glob_beside_an_allowed_glob_is_refused(self):
        from kiro_crew.platform import enterprise_policy_translate as t

        rules = [
            {"capability": "messaging_channel", "effect": "deny", "exclude": ["t*"]},
            {"capability": "messaging_channel", "effect": "deny", "match": ["s*"]},
        ]
        with pytest.raises(t.Unrepresentable):
            t.ruleset(rules, "messaging_channel")

    def test_a_lone_deny_match_leaves_the_capability_unnamed(self):
        """A deny-list would replace the enterprise baseline and open every other channel."""
        from kiro_crew.platform import enterprise_policy_translate as t

        rules = [{"capability": "messaging_channel", "effect": "deny", "match": ["discord"]}]
        assert t.ruleset(rules, "messaging_channel") is None
        assert "channels" not in t.translate(
            "rules:\n  - {capability: messaging_channel, effect: deny, match: [discord]}\n"
        )

    def test_two_different_globs_are_refused(self):
        from kiro_crew.platform import enterprise_policy_translate as t

        rules = [
            {"capability": "messaging_channel", "effect": "deny", "exclude": e}
            for e in (["s*"], ["*k"])
        ]
        with pytest.raises(t.Unrepresentable):
            t.ruleset(rules, "messaging_channel")


_FLOOR = ["normal", "trust", "trust_reads"]


def _translate(*rules: str) -> dict:
    from kiro_crew.platform import enterprise_policy_translate as t

    if not rules:
        return t.translate("rules: []\n")
    return t.translate("rules:\n" + "".join(f"  - {r}\n" for r in rules))


class TestSwitchRows:
    """The on/off policy capabilities ``messaging`` and ``cron`` land as ``{"enabled": bool}``."""

    @pytest.mark.parametrize("capability", ["messaging", "cron"])
    def test_allow_turns_it_on(self, capability):
        doc = _translate(f"{{capability: {capability}, effect: allow}}")
        assert doc["capabilities"] == {capability: {"enabled": True}}

    @pytest.mark.parametrize("capability", ["messaging", "cron"])
    def test_deny_all_turns_it_off(self, capability):
        doc = _translate(
            f"{{capability: {capability}, effect: allow}}",
            f"{{capability: {capability}, effect: deny}}",
        )
        assert doc["capabilities"] == {capability: {"enabled": False}}

    @pytest.mark.parametrize("capability", ["messaging", "cron"])
    def test_unnamed_keeps_the_baseline(self, capability):
        assert "capabilities" not in _translate("{capability: shell, effect: allow}")

    @pytest.mark.parametrize("capability", ["messaging", "cron"])
    def test_ask_reads_as_deny(self, capability):
        doc = _translate(
            f"{{capability: {capability}, effect: allow}}",
            f"{{capability: {capability}, effect: ask}}",
        )
        assert doc["capabilities"] == {capability: {"enabled": False}}

    @pytest.mark.parametrize("effect", ["allow", "deny"])
    @pytest.mark.parametrize("extra", ["exclude: [x]", "match: [x]", "when: 'client == \"crew\"'"])
    @pytest.mark.parametrize("capability", ["messaging", "cron"])
    def test_an_unknown_shape_turns_it_off(self, capability, extra, effect):
        """The binary grant shape is unconfirmed: a shape not known here turns it off.

        Unnamed would not be safe: ``cron`` is an open control (RFC D3), so an unnamed one keeps Crew's default and a restricting
        policy would widen it.
        """
        doc = _translate(
            f"{{capability: {capability}, effect: deny}}",
            f"{{capability: {capability}, effect: {effect}, {extra}}}",
        )
        assert doc["capabilities"] == {capability: {"enabled": False}}

    def test_an_unknown_cron_shape_denies_in_an_enterprise_build(self, enterprise):
        """End to end: an unconditional cron deny beside a conditional allow stays a deny."""
        doc = _translate(
            "{capability: cron, effect: deny}",
            "{capability: cron, effect: allow, when: 'client == \"crew\"'}",
        )
        ceiling = governance.parse_policy(doc)
        assert not governance.resolve(ceiling, None, "capabilities.cron", "").permitted

    def test_an_unknown_effect_is_refused(self):
        from kiro_crew.platform import enterprise_policy_translate as t

        with pytest.raises(t.TranslationError):
            _translate("{capability: cron, effect: maybe}")


class TestPermissionModeRow:
    """The policy capability ``permission_mode`` lands in ``approval_modes`` with its floor kept."""

    def test_allow_turns_yolo_on(self):
        doc = _translate(
            "{capability: permission_mode, effect: allow, match: [yolo]}",
            "{capability: permission_mode, effect: deny, exclude: [yolo]}",
        )
        assert doc["approval_modes"] == {"mode": "allow", "allow": _FLOOR + ["yolo"]}

    def test_deny_all_keeps_only_the_floor(self):
        doc = _translate("{capability: permission_mode, effect: deny}")
        assert doc["approval_modes"] == {"mode": "allow", "allow": _FLOOR}

    def test_unnamed_keeps_the_baseline(self):
        assert "approval_modes" not in _translate("{capability: shell, effect: deny}")

    def test_ask_reads_as_deny(self):
        doc = _translate("{capability: permission_mode, effect: ask}")
        assert doc["approval_modes"] == {"mode": "allow", "allow": _FLOOR}


class TestTranslatedRowsResolve:
    """The translated controls parse in an enterprise build and answer as intended."""

    @pytest.mark.parametrize(
        "rules, cron, messaging, yolo",
        [
            # ``capabilities.cron`` is an open control (RFC D3):
            # unnamed, it keeps Crew's default; a policy deny below still closes it.
            ([], True, False, False),
            (
                [
                    "{capability: cron, effect: allow}",
                    "{capability: messaging, effect: allow}",
                    "{capability: permission_mode, effect: deny, exclude: [yolo]}",
                ],
                True,
                True,
                True,
            ),
            (
                [
                    "{capability: cron, effect: deny}",
                    "{capability: messaging, effect: ask}",
                    "{capability: permission_mode, effect: deny}",
                ],
                False,
                False,
                False,
            ),
        ],
    )
    def test_decisions(self, enterprise, rules, cron, messaging, yolo):
        ceiling = governance.parse_policy(_translate(*rules))
        assert governance.resolve(ceiling, None, "capabilities.cron", "").permitted is cron
        assert (
            governance.resolve(ceiling, None, "capabilities.messaging", "").permitted is messaging
        )
        assert governance.resolve(ceiling, None, "approval_modes", "yolo").permitted is yolo
        for mode in _FLOOR:
            assert governance.resolve(ceiling, None, "approval_modes", mode).permitted


def _setting(value: object, *, raw: bool = False, key: str = "crew_sandbox_min_level") -> dict:
    """One ``body.configuration`` entry as the control plane sends it: the value as JSON."""
    return {
        "key": key,
        "value": value if raw else json.dumps(value),
        "source": "profile#" + _ARN,
        "composeBehavior": "OVERRIDE",
    }


class TestSandboxMinLevel:
    """``crew_sandbox_min_level`` from ``body.configuration`` (RFC D7): tighten-only."""

    @pytest.mark.parametrize(
        "value, floor",
        [("off", None), ("standard", None), ("cc", "cc"), ("strict", "strict")],
    )
    def test_each_level(self, value, floor, caplog):
        from kiro_crew.platform import enterprise_policy_translate as t

        doc = t.translate("rules: []\n", [_setting(value)])
        assert doc.get("sandbox") == (None if floor is None else {"min_level": floor})
        assert "sets no sandbox floor" not in caplog.text

    @pytest.mark.parametrize(
        "entry",
        [
            _setting("bogus"),
            _setting("STRICT"),
            _setting(3),
            _setting(None),
            _setting(["cc"]),
            _setting("cc", raw=True),  # not JSON
            _setting("", raw=True),
        ],
    )
    def test_an_unknown_value_sets_no_floor_and_warns(self, entry, caplog):
        """Not a level the organisation picked, so Crew adds no floor of its own."""
        from kiro_crew.platform import enterprise_policy_translate as t

        doc = t.translate("rules: []\n", [entry])
        assert "sandbox" not in doc
        assert "crew_sandbox_min_level" in caplog.text and "sets no sandbox floor" in caplog.text

    def test_an_unknown_value_beside_a_known_one_keeps_the_known_one(self):
        from kiro_crew.platform import enterprise_policy_translate as t

        doc = t.translate("rules: []\n", [_setting("bogus"), _setting("cc")])
        assert doc["sandbox"] == {"min_level": "cc"}

    def test_absent_keeps_crews_default(self):
        from kiro_crew.platform import enterprise_policy_translate as t

        assert "sandbox" not in t.translate("rules: []\n")
        assert "sandbox" not in t.translate("rules: []\n", [_setting("strict", key="other")])

    def test_several_entries_take_the_strictest(self):
        from kiro_crew.platform import enterprise_policy_translate as t

        for entries in (
            [_setting("off"), _setting("strict")],
            [_setting("strict"), _setting("cc")],
        ):
            assert t.translate("rules: []\n", entries)["sandbox"] == {"min_level": "strict"}

    @pytest.mark.parametrize("local", ["off", "auto", "standard", "cc", "strict"])
    @pytest.mark.parametrize("policy", ["off", "standard", "cc", "strict"])
    def test_the_stricter_of_local_and_policy_wins(self, enterprise, local, policy):
        """Policy only raises; a stricter local level is kept; nothing goes below standard."""
        from kiro_crew import sandbox_tiers
        from kiro_crew.platform import enterprise_policy_translate as t
        from kiro_crew.platform.governance import _ORDINAL_SCALES

        ceiling = governance.parse_policy(t.translate("rules: []\n", [_setting(policy)]))
        governed = governance.resolve_ordinal(ceiling, None, "sandbox.min_level")
        floor = governed.value if governed is not None else None
        level = sandbox_tiers._clamp_sandbox_mode_to_floor(local, floor)
        level = sandbox_tiers._SANDBOX_MODE_ALIASES.get(level, level)
        scale = _ORDINAL_SCALES["sandbox"]
        # An enterprise build already reads a local ``off`` as Crew's default.
        local_level = "standard" if local in ("off", "auto") else local
        assert level == max(local_level, policy, key=scale.index)

    def test_a_stricter_local_profile_wins_over_the_policy(self, enterprise):
        from kiro_crew.platform import enterprise_policy_translate as t
        from kiro_crew.platform.governance import OrdinalControl

        ceiling = governance.parse_policy(t.translate("rules: []\n", [_setting("cc")]))

        class _Profile:
            def get(self, scope):
                return OrdinalControl("sandbox", "strict") if scope == "sandbox.min_level" else None

        assert governance.resolve_ordinal(ceiling, _Profile(), "sandbox.min_level").value == (
            "strict"
        )

    def test_boot_installs_the_floor_and_the_cache_keeps_it(self, enterprise, vault, cps):
        vault()
        cps.reply = _envelope(configuration=[_setting("cc")])
        ceiling = governance.load_security_policy()
        assert governance.resolve_ordinal(ceiling, None, "sandbox.min_level").value == "cc"
        cached = pd.read_cache()
        assert json.loads(cached.body)["sandbox"] == {"min_level": "cc"}
        cps.reply = json.dumps(
            {
                "enterpriseManagementStatus": "UNCHANGED",
                "manifest": {"versionId": epf.cached_version(cached.etag)},
            }
        ).encode()
        assert epf.fetch(_request(cached.etag)).not_modified

    def test_an_older_builds_cache_is_not_kept_on_upgrade(self, enterprise, vault, cps):
        """A cache from a build that ignored the settings has no floor. The upgraded
        build sends no version, so the service answers in full and the floor applies."""
        vault()
        cps.reply = _envelope(configuration=[_setting("strict")])
        fresh = epf.fetch(_request())
        scope = fresh.etag.partition(":")[0]
        old_etag = f"{scope}:{epf.cached_version(fresh.etag)}"  # the pre-revision shape
        again = epf.fetch(_request(old_etag))
        assert "manifestVersionId" not in cps.payload()
        assert not again.not_modified
        assert json.loads(again.body)["sandbox"] == {"min_level": "strict"}
        epf.fetch(_request(fresh.etag))
        assert cps.payload()["manifestVersionId"] == epf.cached_version(fresh.etag)

    def test_the_hash_covers_the_setting(self, enterprise, vault, cps, capsys):
        vault()
        envelope = json.loads(_envelope(configuration=[_setting("strict")]))
        envelope["body"]["configuration"][0]["value"] = json.dumps("off")
        cps.reply = json.dumps(envelope).encode()
        assert "does not match its own sha256 hash" in _boot_exit_message(capsys)

    def test_a_reply_with_no_hash_still_applies_the_setting(self, enterprise, vault, cps):
        vault()
        cps.reply = json.dumps(
            {
                "enterpriseManagementStatus": "ENABLED",
                "body": {"policy": _POLICY, "configuration": [_setting("strict")]},
            }
        ).encode()
        assert json.loads(epf.fetch(_request()).body)["sandbox"] == {"min_level": "strict"}

    @pytest.mark.parametrize(
        "configuration",
        [
            [{"key": "crew_sandbox_min_level"}],
            [{"key": "other", "value": 3}, "not an object"],
            {"not": "a list"},
            [{"key": "crew_sandbox_min_level", "value": 7}],
        ],
    )
    def test_a_badly_shaped_entry_with_no_hash_sets_no_floor(
        self, enterprise, vault, cps, configuration
    ):
        """As on main: with no hash, entry shapes are not checked, so an entry Crew
        does not read never refuses a reply, and a bad one for the setting sets no floor."""
        vault()
        cps.reply = json.dumps(
            {
                "enterpriseManagementStatus": "ENABLED",
                "body": {"policy": _POLICY, "configuration": configuration},
            }
        ).encode()
        doc = json.loads(epf.fetch(_request()).body)
        assert "sandbox" not in doc
        assert doc["channels"]["members"]["allow"] == ["slack"]

    def test_a_badly_shaped_entry_with_a_hash_still_refuses_the_reply(
        self, enterprise, vault, cps, capsys
    ):
        vault()
        envelope = json.loads(_envelope())
        envelope["body"]["configuration"] = [{"key": "crew_sandbox_min_level"}]
        cps.reply = json.dumps(envelope).encode()
        assert "configuration entries are malformed" in _boot_exit_message(capsys)

    def test_oss_ignores_the_setting(self, vault, cps):
        vault()
        cps.reply = _envelope(configuration=[_setting("strict")])
        assert governance.load_security_policy() is None
        assert cps.requests == []


class TestSignInCommands:
    def _main(self, monkeypatch, tmp_path, argv):
        from kiro_crew import cli

        def failed_policy(_cfg):
            raise PlatformCompositionError("no Kiro sign-in was found")

        monkeypatch.setenv("KIROCREW_HOME", str(tmp_path))
        monkeypatch.setattr(cli, "boot_platform", failed_policy)
        monkeypatch.setattr(cli, "maybe_reexec", lambda _argv: None)
        monkeypatch.setattr(
            cli.platform_compat, "limit_malloc_arenas", cli.platform_compat.MallocArenaCap
        )
        monkeypatch.setattr(cli, "_setup_cli_logging", lambda *_args: None)
        monkeypatch.setattr(cli.sys, "argv", ["kirocrew", *argv])
        return cli

    def test_kas_login_runs_when_the_policy_load_fails(self, enterprise, monkeypatch, tmp_path):
        cli = self._main(monkeypatch, tmp_path, ["kas", "whoami"])
        ran: list = []
        monkeypatch.setattr(
            "kiro_crew.cli_kas_login.kas_login_cmd", lambda args: ran.append(args.kas_action) or 0
        )
        with pytest.raises(SystemExit) as exited:
            cli.main()
        assert exited.value.code in (0, None)
        assert ran == ["whoami"]

    def test_kas_route_runs_when_the_policy_load_fails(self, enterprise, monkeypatch, tmp_path):
        """A remote sign-in runs `kas route` before `kas login`; exit 1 would read as unknown."""
        cli = self._main(monkeypatch, tmp_path, ["kas", "route"])
        ran: list = []
        monkeypatch.setattr(
            "kiro_crew.cli_kas_login.kas_login_cmd", lambda args: ran.append(args.kas_action) or 0
        )
        with pytest.raises(SystemExit) as exited:
            cli.main()
        assert exited.value.code in (0, None)
        assert ran == ["route"]

    def test_other_commands_still_exit(self, enterprise, monkeypatch, tmp_path, capsys):
        cli = self._main(monkeypatch, tmp_path, ["kas", "status"])
        with pytest.raises(SystemExit) as exited:
            cli.main()
        assert exited.value.code == enterprise_boot.EXIT_CODE
        assert "no Kiro sign-in was found" in capsys.readouterr().err

    def test_oss_does_not_exempt_the_sign_in_verbs(self, monkeypatch, tmp_path, capsys):
        cli = self._main(monkeypatch, tmp_path, ["kas", "whoami"])
        with pytest.raises(SystemExit) as exited:
            cli.main()
        assert exited.value.code == 1
        assert "no Kiro sign-in was found" in capsys.readouterr().err


class TestOss:
    def test_oss_refuses_the_scheme(self, vault, cps):
        vault()
        with pytest.raises(PlatformCompositionError, match="only in an enterprise build"):
            epf.fetch(_request())
        assert cps.requests == []

    def test_oss_boot_is_unchanged(self, vault, cps):
        vault()
        assert governance.load_security_policy() is None
        assert cps.requests == []

    def test_oss_ignores_a_policy_granting_the_new_rows(self, vault, cps):
        """The new policy rows change nothing in an oss build: no fetch, no policy."""
        vault()
        cps.reply = _envelope(
            "rules:\n"
            "  - {capability: cron, effect: deny}\n"
            "  - {capability: messaging, effect: deny}\n"
            "  - {capability: permission_mode, effect: deny}\n"
        )
        assert governance.load_security_policy() is None
        assert cps.requests == []

    def test_oss_naming_the_scheme_is_a_configuration_error(self, vault, cps, monkeypatch):
        monkeypatch.setenv(pd.POLICY_URL_ENV, pd.ENTERPRISE_POLICY_URL)
        with pytest.raises(PlatformCompositionError, match="only in an enterprise build"):
            governance.load_security_policy()
        assert cps.requests == []


class TestBootWithoutCompanion:
    """An enterprise build with no companion boots on the core defaults, every gate on.

    Each test runs the real ``bootstrap_context`` with no ``kirocrew.plugins`` entry
    point, against the fake control plane, so the policy fetch, the built-in
    baseline and the local tiers are the shipped ones.
    """

    @pytest.fixture(autouse=True)
    def _restore_selectable_backends(self):
        """Boot narrows the process-wide selectable-backend set to the policy's; undo it.

        ``apply_selectable_denials`` assigns ``baseline - denied``, so an empty denial
        restores the full set for the tests that run after this one.
        """
        from kiro_crew.agent_sdk.backends import apply_selectable_denials

        yield
        apply_selectable_denials(set())

    @staticmethod
    def _boot(monkeypatch):
        from kiro_crew.config.loader import KiroCrewConfig
        from kiro_crew.platform import bootstrap

        monkeypatch.setattr(bootstrap, "plugin_entry_points", lambda: [])
        return bootstrap.bootstrap_context(KiroCrewConfig())

    def test_boot_fetches_the_policy_and_composes_the_core_defaults(
        self, enterprise, vault, cps, monkeypatch
    ):
        from kiro_crew.platform.defaults import DefaultIdentityProvider

        vault()
        ctx = self._boot(monkeypatch)
        assert ctx.is_enterprise
        assert isinstance(ctx.identity, DefaultIdentityProvider)
        assert len(cps.requests) == 1
        assert ctx.governance is not None

    def test_a_scope_the_policy_does_not_name_is_denied(self, enterprise, vault, cps, monkeypatch):
        vault()
        ceiling = self._boot(monkeypatch).governance
        # Named by the remote policy: it answers.
        assert governance.resolve(ceiling, None, "channels", "slack").permitted
        assert not governance.resolve(ceiling, None, "channels", "discord").permitted
        # Not named: the built-in baseline holds it at its minimal set.
        assert not governance.resolve(ceiling, None, "network.egress", "example.com").permitted
        assert not governance.resolve(ceiling, None, "capabilities.browse", "").permitted
        # Not named, but an open control (RFC D3): it keeps Crew's default.
        assert governance.gate_decision(ceiling, None, "Running: ls").permitted

    def test_a_local_policy_only_narrows(self, enterprise, vault, cps, monkeypatch, tmp_path):
        local = tmp_path / "local-policy.json"
        local.write_text(
            json.dumps(
                {
                    "version": 1,
                    "boot": {},
                    "network": {"egress": {"mode": "deny", "deny": []}},
                    "channels": {"members": {"mode": "deny", "deny": ["slack"]}},
                }
            ),
            encoding="utf-8",
        )
        monkeypatch.setenv("KIROCREW_SECURITY_POLICY", str(local))
        vault()
        ceiling = self._boot(monkeypatch).governance
        # Asked to widen egress: still the baseline's denial.
        assert not governance.resolve(ceiling, None, "network.egress", "example.com").permitted
        # Asked to narrow what the remote policy allowed: narrowed.
        assert not governance.resolve(ceiling, None, "channels", "slack").permitted

    @pytest.mark.parametrize("signed_in", [True, False])
    def test_a_failed_fetch_exits(self, enterprise, vault, cps, monkeypatch, capsys, signed_in):
        if signed_in:
            vault()
            cps.reply = _http_error(500, {"message": "boom"})
        with pytest.raises(SystemExit) as exited:
            try:
                self._boot(monkeypatch)
            except PlatformCompositionError as exc:
                enterprise_boot.refuse_policy_boot(exc)
                raise
        assert exited.value.code == enterprise_boot.EXIT_CODE
        err = capsys.readouterr().err
        assert "governance rules" in err
        assert _TOKEN not in err
