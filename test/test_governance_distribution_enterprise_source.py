"""An enterprise build's policy source is a build-time constant (RFC D2).

``docs/request-for-change/rfc-crew-enterprise-edition.md``, decision D2: in an enterprise
build the governance policy URL is :data:`ENTERPRISE_POLICY_URL`. No environment variable,
config key or policy document field can point the build at another address, and the
central policy is REQUIRED: a fetch that cannot complete aborts boot, whatever any local
``on_unavailable`` says. Every oss case here pins that an oss build behaves as before.

The https transport is replaced with a fake through the shared ``transport`` fixture, so
nothing here opens a socket; the fixture restores the scheme table afterwards.
"""

from __future__ import annotations

import json

import pytest
from governance_distribution_helpers import _hermetic_governance_globals  # noqa: F401
from governance_distribution_helpers import install_ceiling_fixture  # noqa: F401
from governance_distribution_helpers import transport_fixture  # noqa: F401
from governance_distribution_helpers import (
    _body,
    _cache,
    _doc,
    _failing_fetcher,
    _static_fetcher,
    _write_policy,
)

from kiro_crew.platform import governance
from kiro_crew.platform import policy_distribution as pd
from kiro_crew.platform.context import PlatformCompositionError

_ELSEWHERE = "https://elsewhere.example.invalid/policy.json"

#: A value for every ``KIROCREW_POLICY_*`` distribution setting, each one different from
#: the default and each one a loosening or a redirect if it were honoured.
_LOCAL_ENV = {
    pd.POLICY_URL_ENV: _ELSEWHERE,
    pd.POLICY_REFRESH_ENV: "120",
    pd.POLICY_TIMEOUT_ENV: "3",
    pd.POLICY_MAX_AGE_ENV: "999999",
    pd.POLICY_UNAVAILABLE_ENV: governance.UNAVAILABLE_DEGRADE,
}


@pytest.fixture(name="enterprise")
def enterprise_fixture(monkeypatch):
    monkeypatch.setattr("kiro_crew.edition.EDITION", "enterprise")


def _set_local_env(monkeypatch) -> None:
    for name, value in _LOCAL_ENV.items():
        monkeypatch.setenv(name, value)


def _https(transport, fetcher) -> None:
    """Answer every https fetch with *fetcher* (restored by the fixture)."""
    transport(fetcher, scheme="https")


class TestResolvedSettings:
    def test_the_placeholder_is_on_a_reserved_domain(self):
        assert pd.ENTERPRISE_POLICY_URL.startswith("https://")
        assert ".invalid/" in pd.ENTERPRISE_POLICY_URL

    def test_oss_honours_the_env_url(self, monkeypatch):
        monkeypatch.setenv(pd.POLICY_URL_ENV, _ELSEWHERE)
        assert pd.resolve_distribution().source == _ELSEWHERE

    def test_oss_honours_a_declared_source(self):
        declared = governance.PolicyDistribution(source=_ELSEWHERE)
        assert pd.resolve_distribution(declared).source == _ELSEWHERE

    def test_enterprise_ignores_every_env_setting(self, enterprise, monkeypatch):
        _set_local_env(monkeypatch)
        resolved = pd.resolve_distribution()
        assert resolved == governance.PolicyDistribution(source=pd.ENTERPRISE_POLICY_URL)

    def test_enterprise_ignores_a_malformed_env_value_too(self, enterprise, monkeypatch):
        """Ignored means not read: a typo an oss build refuses is not even parsed."""
        monkeypatch.setenv(pd.POLICY_REFRESH_ENV, "15m")
        assert pd.resolve_distribution().source == pd.ENTERPRISE_POLICY_URL

    def test_enterprise_ignores_a_declared_source_and_disposition(self, enterprise):
        declared = governance.PolicyDistribution(
            source=_ELSEWHERE,
            refresh_interval_secs=900,
            max_cache_age_secs=3600,
            on_unavailable=governance.UNAVAILABLE_DEGRADE,
        )
        resolved = pd.resolve_distribution(declared)
        assert resolved.source == pd.ENTERPRISE_POLICY_URL
        assert resolved.on_unavailable == governance.UNAVAILABLE_FAIL_CLOSED
        # The cadence and the staleness bound of the block it is handed are kept: in
        # enterprise that block is only ever the fetched document's.
        assert resolved.refresh_interval_secs == 900
        assert resolved.max_cache_age_secs == 3600

    def test_enterprise_pins_the_source_with_no_env(self, enterprise):
        assert pd.host_pinned_source() == pd.ENTERPRISE_POLICY_URL
        assert pd._env_pins_the_source() is True

    def test_a_source_less_block_is_accepted_in_enterprise(self, enterprise):
        """The build names the address, so a block that only tunes cadence is not inert."""
        parsed = governance.PolicyDistribution.from_dict({"refresh_interval_secs": 900})
        assert parsed.refresh_interval_secs == 900

    def test_a_source_less_block_is_still_refused_in_oss(self):
        with pytest.raises(PlatformCompositionError, match="no 'source'"):
            governance.PolicyDistribution.from_dict({"refresh_interval_secs": 900})


class TestBoot:
    def test_env_and_local_config_cannot_redirect_the_fetch(
        self, enterprise, transport, monkeypatch, tmp_path
    ):
        local = _write_policy(
            tmp_path / "local.json",
            "local-file",
            distribution={"source": _ELSEWHERE, "on_unavailable": "degrade"},
        )
        home = _write_policy(
            tmp_path / "home.json", "home-file", distribution={"source": _ELSEWHERE}
        )
        monkeypatch.setenv("KIROCREW_SECURITY_POLICY", str(local))
        monkeypatch.setattr(governance, "_policy_home_path", lambda: home)
        _set_local_env(monkeypatch)
        seen: list = []
        _https(transport, _static_fetcher(pd.FetchedPolicy(body=_body("central")), seen))

        ceiling = governance.load_security_policy()

        assert ceiling is not None
        assert ceiling.identity_issuer == "central"
        assert [request.url for request in seen] == [pd.ENTERPRISE_POLICY_URL]

    def test_a_fetch_failure_aborts_boot(self, enterprise, transport, monkeypatch, tmp_path):
        """No fallback: a local ``degrade`` (env or document) does not soften it."""
        local = _write_policy(
            tmp_path / "local.json", "local-file", distribution={"on_unavailable": "degrade"}
        )
        monkeypatch.setenv("KIROCREW_SECURITY_POLICY", str(local))
        _set_local_env(monkeypatch)
        _https(transport, _failing_fetcher(TimeoutError("endpoint down")))

        with pytest.raises(PlatformCompositionError, match="requires its central policy"):
            governance.load_security_policy()

    def test_with_nothing_configured_the_tier_still_runs(self, enterprise, transport):
        """No source anywhere does not switch the tier off in enterprise."""
        _https(transport, _failing_fetcher(TimeoutError("endpoint down")))
        with pytest.raises(PlatformCompositionError, match="requires its central policy"):
            governance.load_security_policy()

    def test_oss_with_nothing_configured_stays_ungoverned(self):
        assert governance.load_security_policy() is None

    def test_oss_still_honours_the_env_url(self, transport, monkeypatch):
        seen: list = []
        source = transport(_static_fetcher(pd.FetchedPolicy(body=_body("central")), seen))
        monkeypatch.setenv(pd.POLICY_URL_ENV, source)
        ceiling = governance.load_security_policy()
        assert ceiling is not None and ceiling.identity_issuer == "central"
        assert [request.url for request in seen] == [source]


class TestFetchedDocument:
    def test_a_fetched_source_is_ignored_and_its_cadence_kept(self, enterprise, transport):
        body = _body(
            "central",
            distribution={
                "source": _ELSEWHERE,
                "refresh_interval_secs": 900,
                "on_unavailable": "degrade",
            },
        )
        _https(transport, _static_fetcher(pd.FetchedPolicy(body=body)))

        ceiling = governance.load_security_policy()

        # Not refused as a source migration: the build pins the address.
        assert ceiling is not None and ceiling.identity_issuer == "central"
        resolved = pd.resolve_distribution(ceiling.distribution)
        assert resolved.source == pd.ENTERPRISE_POLICY_URL
        assert resolved.refresh_interval_secs == 900
        assert resolved.on_unavailable == governance.UNAVAILABLE_FAIL_CLOSED

    def test_a_local_block_does_not_fill_a_gap_the_fetched_one_left(
        self, enterprise, transport, monkeypatch, tmp_path
    ):
        """The fold must not hand the refresher a local cadence or staleness bound."""
        local = _write_policy(
            tmp_path / "local.json",
            "local-file",
            distribution={"source": _ELSEWHERE, "refresh_interval_secs": 60},
        )
        monkeypatch.setenv("KIROCREW_SECURITY_POLICY", str(local))
        _https(transport, _static_fetcher(pd.FetchedPolicy(body=_body("central"))))

        ceiling = governance.load_security_policy()

        assert ceiling is not None
        assert ceiling.distribution == governance.PolicyDistribution()
        assert pd.resolve_distribution(ceiling.distribution).refresh_interval_secs == 0


class TestCacheOnlyChild:
    def test_a_cache_from_another_source_is_refused(self, enterprise, monkeypatch):
        _cache("planted", _ELSEWHERE)
        monkeypatch.setenv(pd.POLICY_CACHE_ONLY_ENV, "1")
        with pytest.raises(PlatformCompositionError, match="no cached ceiling"):
            pd.load_distributed_policy()

    def test_the_cache_from_the_build_source_is_adopted(self, enterprise, monkeypatch):
        _cache("central", pd.ENTERPRISE_POLICY_URL)
        monkeypatch.setenv(pd.POLICY_CACHE_ONLY_ENV, "1")
        ceiling = pd.load_distributed_policy()
        assert ceiling is not None and ceiling.identity_issuer == "central"

    def test_oss_cache_only_does_not_check_the_source(self, monkeypatch):
        """Unchanged in oss: the child has no source to check against."""
        _cache("central", _ELSEWHERE)
        monkeypatch.setenv(pd.POLICY_CACHE_ONLY_ENV, "1")
        ceiling = pd.load_distributed_policy()
        assert ceiling is not None and json.loads(pd.read_cache().body)["identity"]


class TestRunningProcess:
    """The refresher, the CLI helpers and the posture read the same resolution.

    ``install_ceiling`` comes before ``enterprise`` in each signature so its base
    context is composed as oss, before the edition flips.
    """

    def _install(self, install_ceiling):
        doc = _doc(
            "running",
            distribution={
                "source": _ELSEWHERE,
                "refresh_interval_secs": 900,
                "max_cache_age_secs": 3600,
                "on_unavailable": "degrade",
            },
        )
        install_ceiling(governance.parse_policy(doc))

    def test_refresh_fetches_the_build_source(
        self, transport, install_ceiling, enterprise, monkeypatch
    ):
        self._install(install_ceiling)
        _set_local_env(monkeypatch)
        seen: list = []
        _https(transport, _static_fetcher(pd.FetchedPolicy(body=_body("pushed")), seen))

        assert pd.refresh_now().status == pd.REFRESH_APPLIED
        assert [request.url for request in seen] == [pd.ENTERPRISE_POLICY_URL]

    def test_cadence_and_bound_come_from_the_installed_document(
        self, install_ceiling, enterprise, monkeypatch
    ):
        self._install(install_ceiling)
        _set_local_env(monkeypatch)
        assert pd.effective_refresh_interval() == 900
        assert pd.effective_max_cache_age() == 3600

    def test_the_posture_reports_the_build_source(self, install_ceiling, enterprise, monkeypatch):
        self._install(install_ceiling)
        _set_local_env(monkeypatch)
        posture = pd.distribution_posture()
        assert posture["configured"] is True
        assert posture["source_scheme"] == "https"
        assert posture["refresh_interval_seconds"] == 900
        assert posture["on_unavailable"] == governance.UNAVAILABLE_FAIL_CLOSED

    def test_with_nothing_installed_the_posture_is_still_configured(self, enterprise):
        posture = pd.distribution_posture()
        assert posture["configured"] is True
        assert posture["refresh_interval_seconds"] == 0
