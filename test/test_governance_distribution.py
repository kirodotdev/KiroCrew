"""Central policy distribution — one ceiling, followed by the whole fleet.

Covers ``governance.PolicyDistribution`` (the parsed declaration), the
``platform.policy_distribution`` engine (source resolution, the transport seam,
the last-known-good cache, the boot dispositions, the fetch window and the live
refresh), the tier's place in ``load_security_policy``'s precedence, the profile
cache a ceiling swap invalidates, and ``TestTheDetailNeverNamesTheEndpoint``: no
failure detail, log line or traceback names the source.

The tier's hardening lives in siblings named ``test_governance_distribution_<topic>.py``:
``_fetch_checks`` (what a source and its answer must be before they are believed),
``_redaction`` (the rest of what the tier may say, log or hand an agent about its
source), ``_publish`` (the cache is published before the ceiling is installed, and
neither moves backward), ``_cache_trust`` (the cache as a trust root on disk) and
``_ladder`` (a refresh re-composes the whole tier ladder). The constants, document builders, fake fetchers
and fixtures the family shares live in ``governance_distribution_helpers.py``.

**Nothing here touches the network.** Every transport case either registers a
fake fetcher through the public ``register_policy_fetcher`` seam or drives a
``file://`` source under ``tmp_path``; the one plain-``http`` case is refused
before a socket is opened. Patching ``urllib.request.urlopen`` would NOT
intercept this module — it opens through the named ``_open`` seam.
"""

from __future__ import annotations

import json
import logging
import sys
import time
import urllib.error
import urllib.request

import pytest
from governance_distribution_helpers import _hermetic_governance_globals  # noqa: F401
from governance_distribution_helpers import install_ceiling_fixture  # noqa: F401
from governance_distribution_helpers import profiles_dir_fixture  # noqa: F401
from governance_distribution_helpers import readonly_chain_fixture  # noqa: F401
from governance_distribution_helpers import transport_fixture  # noqa: F401
from governance_distribution_helpers import (
    _DECLARED,
    _ENGINE_MODULE,
    _PAST_THE_WINDOW,
    _REFRESH_STATUSES,
    _TEST_SCHEME,
    _TEST_SOURCE,
    _body,
    _cache,
    _cache_raw,
    _doc,
    _failing_fetcher,
    _file_source,
    _sign,
    _static_fetcher,
    _write_policy,
    pd_format_exc,
)

from kiro_crew import security
from kiro_crew.platform import governance
from kiro_crew.platform import governance_health as health
from kiro_crew.platform import governance_profiles as gp
from kiro_crew.platform import policy_distribution as pd
from kiro_crew.platform.context import PlatformCompositionError, current_context

# ──────────────────────────────────────────────────────────────────────────
# The parsed declaration
# ──────────────────────────────────────────────────────────────────────────


class TestPolicyDistributionParsing:
    def test_a_full_block_round_trips(self):
        source = "https://config.corp.example/kirocrew/policy.json"
        ceiling = governance.parse_policy(
            _doc(
                distribution={
                    "source": source,
                    "refresh_interval_secs": 900,
                    "timeout_secs": 4.5,
                    "max_cache_age_secs": 86400,
                    "on_unavailable": governance.UNAVAILABLE_DEGRADE,
                }
            )
        )
        dist = ceiling.distribution
        assert dist.source == source
        assert dist.refresh_interval_secs == 900
        assert dist.timeout_secs == 4.5
        assert dist.max_cache_age_secs == 86400
        assert dist.on_unavailable == governance.UNAVAILABLE_DEGRADE
        assert dist.enabled is True

    def test_an_absent_block_changes_nothing(self):
        """Every existing policy must be byte-identical in behaviour."""
        dist = governance.parse_policy(_doc()).distribution
        assert dist == governance.PolicyDistribution()
        assert dist.enabled is False
        assert dist.effective_refresh_interval() == 0
        assert dist.on_unavailable == governance.UNAVAILABLE_FAIL_CLOSED

    @pytest.mark.parametrize(
        "declared,expected",
        [
            (0, 0),
            (1, governance.MIN_REFRESH_INTERVAL_SECS),
            (governance.MIN_REFRESH_INTERVAL_SECS - 1, governance.MIN_REFRESH_INTERVAL_SECS),
            (governance.MIN_REFRESH_INTERVAL_SECS, governance.MIN_REFRESH_INTERVAL_SECS),
            (governance.MIN_REFRESH_INTERVAL_SECS * 15, governance.MIN_REFRESH_INTERVAL_SECS * 15),
        ],
    )
    def test_refresh_interval_is_clamped_up_but_zero_stays_off(self, declared, expected):
        """0 means "fetch at boot only"; a small value means "poll", not "hammer"."""
        dist = governance.PolicyDistribution(
            source="https://x.invalid/p", refresh_interval_secs=declared
        )
        assert dist.effective_refresh_interval() == expected

    def test_timeout_falls_back_to_the_default(self):
        assert (
            governance.PolicyDistribution(source="https://x.invalid/p").effective_timeout()
            == governance.DEFAULT_FETCH_TIMEOUT_SECS
        )
        assert (
            governance.PolicyDistribution(
                source="https://x.invalid/p", timeout_secs=2.5
            ).effective_timeout()
            == 2.5
        )

    def test_no_staleness_bound_means_no_cache_is_ever_too_old(self):
        dist = governance.PolicyDistribution(source="https://x.invalid/p")
        assert dist.cache_too_old(0) is False
        assert dist.cache_too_old(10**9) is False

    def test_a_cache_past_the_bound_is_too_old(self):
        dist = governance.PolicyDistribution(source="https://x.invalid/p", max_cache_age_secs=60)
        assert dist.cache_too_old(59) is False
        assert dist.cache_too_old(60) is False
        assert dist.cache_too_old(61) is True

    def test_a_negative_age_reads_as_fresh(self):
        """NTP stepping the clock backwards must not refuse to boot the host."""
        dist = governance.PolicyDistribution(source="https://x.invalid/p", max_cache_age_secs=60)
        assert dist.cache_too_old(-5000) is False

    @pytest.mark.parametrize(
        "block,match",
        [
            # `str(raw or "")` would coerce this to "" and silently DISABLE central
            # distribution on a policy that plainly meant to configure it.
            ({"source": False}, "must be a string"),
            # bool is an int subclass: `true` must not parse as 1 second.
            ({"source": "https://x.invalid/p", "refresh_interval_secs": True}, "must be a number"),
            ({"source": "https://x.invalid/p", "refresh_interval_secs": 1.5}, "whole number"),
            ({"source": "https://x.invalid/p", "max_cache_age_secs": -1}, "must not be negative"),
            # Provenance is NOT configurable from the fetched document: a document
            # must not be the authority on whether it has to be authentic, so the key
            # is rejected outright rather than honoured.
            ({"source": "https://x.invalid/p", "require_signature": True}, "unknown key"),
            ({"source": "https://x.invalid/p", "sources": "typo"}, "unknown key"),
            ({"source": "https://x.invalid/p", "on_unavailable": "maybe"}, "must be one of"),
            # A block that tunes a fetch it never configures is a policy whose author
            # believed central distribution was on.
            ({"refresh_interval_secs": 900}, "no 'source'"),
        ],
    )
    def test_a_malformed_block_fails_closed(self, block, match):
        with pytest.raises(PlatformCompositionError, match=match):
            governance.parse_policy(_doc(distribution=block))

    def test_a_non_object_block_fails_closed(self):
        with pytest.raises(PlatformCompositionError, match="must be an object"):
            governance.parse_policy(_doc(distribution="https://x.invalid/p"))

    def test_a_profile_may_not_redirect_where_the_ceiling_comes_from(self):
        """Policy-only, and the two halves of that are asserted together.

        The key is in ``_STRUCTURAL_KEYS`` so ``_parse_controls`` skips it rather
        than rejecting it as an unknown scope — which alone would make a profile's
        copy silently INERT. The explicit raise is what turns that mechanical
        skip into a loud refusal, so neither assertion means much without the other.
        """
        assert "distribution" in governance._STRUCTURAL_KEYS
        with pytest.raises(PlatformCompositionError, match="policy-only"):
            governance.parse_profile(
                {"name": "app-x", "distribution": {"source": "https://evil.invalid/p"}}
            )

    def test_the_duplicated_constants_are_equal(self):
        """``governance`` is the trust root and must not import the fetch engine.

        So it names the env var and the cache leaf itself. Naming is not a
        behaviour, so the copies have nothing to drift into — as long as this holds.
        """
        assert governance._POLICY_DISTRIBUTION_URL_ENV == pd.POLICY_URL_ENV
        assert governance._POLICY_CACHE_LEAF == pd.CACHE_DIR_LEAF


# ──────────────────────────────────────────────────────────────────────────
# Source resolution — the env channel overlaid on the declaration
# ──────────────────────────────────────────────────────────────────────────


class TestSourceResolution:
    @pytest.mark.parametrize(
        "env_var,raw,attr,expected",
        [
            (
                pd.POLICY_URL_ENV,
                "https://canary.corp.example/p.json",
                "source",
                "https://canary.corp.example/p.json",
            ),
            (pd.POLICY_REFRESH_ENV, "1800", "refresh_interval_secs", 1800),
            (pd.POLICY_TIMEOUT_ENV, "2.5", "timeout_secs", 2.5),
            (pd.POLICY_MAX_AGE_ENV, "60", "max_cache_age_secs", 60),
            (
                pd.POLICY_UNAVAILABLE_ENV,
                governance.UNAVAILABLE_DEGRADE,
                "on_unavailable",
                governance.UNAVAILABLE_DEGRADE,
            ),
        ],
    )
    def test_the_env_overrides_one_setting_and_leaves_the_rest(
        self, monkeypatch, env_var, raw, attr, expected
    ):
        """Per-setting, so a host can be retuned without re-signing the document."""
        monkeypatch.setenv(env_var, raw)
        resolved = pd.resolve_distribution(_DECLARED)
        assert getattr(resolved, attr) == expected
        for other in (
            "source",
            "refresh_interval_secs",
            "timeout_secs",
            "max_cache_age_secs",
            "on_unavailable",
        ):
            if other != attr:
                assert getattr(resolved, other) == getattr(_DECLARED, other), other

    def test_an_unset_env_leaves_the_declaration_untouched(self):
        assert pd.resolve_distribution(_DECLARED) == _DECLARED

    def test_no_declaration_and_no_env_is_not_configured(self):
        assert pd.resolve_distribution(None).enabled is False

    @pytest.mark.parametrize(
        "env_var,raw",
        [
            (pd.POLICY_REFRESH_ENV, "15m"),
            (pd.POLICY_REFRESH_ENV, "-60"),
            (pd.POLICY_REFRESH_ENV, "90.5"),
            (pd.POLICY_TIMEOUT_ENV, "soon"),
            (pd.POLICY_MAX_AGE_ENV, "1 day"),
            (pd.POLICY_UNAVAILABLE_ENV, "maybe"),
        ],
    )
    def test_a_malformed_env_value_raises_rather_than_reading_as_unset(
        self, monkeypatch, env_var, raw
    ):
        """An operator who wrote ``=15m`` asked for a refresh.

        Silently giving them none is how a fleet stops following its admin with
        nothing anywhere reporting it.
        """
        monkeypatch.setenv(env_var, raw)
        with pytest.raises(PlatformCompositionError, match=env_var):
            pd.resolve_distribution(_DECLARED)

    def test_absent_headers_are_empty(self):
        assert pd.request_headers() == {}

    def test_a_json_object_becomes_the_request_headers(self, monkeypatch):
        monkeypatch.setenv(
            pd.POLICY_HEADERS_ENV, json.dumps({"Authorization": "Bearer t", "X-Fleet": "eu"})
        )
        assert pd.request_headers() == {"Authorization": "Bearer t", "X-Fleet": "eu"}

    @pytest.mark.parametrize(
        "raw",
        [
            "Authorization: Bearer t",  # not JSON at all
            '["Authorization", "Bearer t"]',  # a JSON array, not an object
            '{"X-Retry": 3}',  # a non-string value
        ],
    )
    def test_malformed_headers_raise(self, monkeypatch, raw):
        """A token that silently fails to be sent looks like an outage, not a typo."""
        monkeypatch.setenv(pd.POLICY_HEADERS_ENV, raw)
        with pytest.raises(PlatformCompositionError, match=pd.POLICY_HEADERS_ENV):
            pd.request_headers()


# ──────────────────────────────────────────────────────────────────────────
# Transport
# ──────────────────────────────────────────────────────────────────────────


class TestTransport:
    def test_the_builtin_schemes_are_registered(self):
        schemes = pd.registered_policy_schemes()
        assert "https" in schemes
        assert "file" in schemes
        assert "http" in schemes

    def test_an_unknown_scheme_raises_and_does_not_fall_back_to_the_cache(self):
        """A typo'd ``htps://`` will never start working, so it must be loud.

        Reading it as "unreachable" would quietly hand the host to a cached copy
        and the operator would never learn their source is unusable.
        """
        source = "htps://policy.corp.example/security_policy.json"
        _cache("cached", source, age_secs=_PAST_THE_WINDOW)
        assert pd.read_cache() is not None  # the fallback really is available
        with pytest.raises(PlatformCompositionError, match="htps"):
            pd.load_distributed_policy(governance.PolicyDistribution(source=source))

    @pytest.mark.parametrize(
        "url",
        [
            "http://127.0.0.1:9443/policy.json",
            "http://localhost/policy.json",
            "http://[::1]:9443/policy.json",
            "https://config.corp.example/policy.json",
            "file:///etc/kirocrew/policy.json",
        ],
    )
    def test_permitted_transports(self, url):
        pd._assert_transport_permitted(url)  # must not raise

    @pytest.mark.parametrize(
        "url",
        [
            "http://policy.corp.example/policy.json",
            "http://10.0.0.7:8080/policy.json",
            "http://127.0.0.1.evil.example/policy.json",
        ],
    )
    def test_plain_http_to_a_non_loopback_host_is_refused(self, url):
        """A clear-text ceiling is substitutable in transit by anyone on the path."""
        with pytest.raises(PlatformCompositionError, match="plain http"):
            pd._assert_transport_permitted(url)

    @pytest.mark.parametrize("host", ["127.0.0.2", "127.1.2.3", "[::1]", "localhost"])
    def test_every_loopback_address_is_permitted_not_just_the_common_one(self, host):
        """``ipaddress``, not a literal set of spellings.

        A hand-written set gets both ends wrong: it refuses a legitimate relay on
        ``127.0.0.2`` (the whole 127/8 block is this machine), and a ``"[::1]"``
        entry in it never matches, because ``urlsplit().hostname`` has already
        stripped the brackets by the time the check sees the host.
        """
        pd._assert_transport_permitted(f"http://{host}:8080/policy.json")

    @pytest.mark.parametrize("host", ["::1", "127.0.0.1", "localhost", "LOCALHOST"])
    def test_the_loopback_predicate_recognises_every_spelling(self, host):
        assert pd._is_loopback(host) is True

    @pytest.mark.parametrize("host", ["", "example.com", "10.0.0.1", "policy.corp.example"])
    def test_a_name_that_is_not_an_address_is_not_loopback(self, host):
        """Resolving it would make the decision depend on the network being distrusted."""
        assert pd._is_loopback(host) is False

    @pytest.mark.parametrize("scheme", ["https", "file", _TEST_SCHEME])
    def test_a_conflicting_registration_raises_rather_than_shadowing(self, transport, scheme):
        """``register_scope``'s refusal is the half worth copying most.

        This registry decides which code fetches the security ceiling, so a typo must not
        silently shadow a built-in — and there is deliberately no override flag, because
        the precedent has none either.
        """
        transport(_static_fetcher(pd.FetchedPolicy(body=_body("first"))))
        with pytest.raises(PlatformCompositionError, match="already registered"):
            pd.register_policy_fetcher(scheme, _static_fetcher(pd.FetchedPolicy(body=b"{}")))

    def test_a_plain_http_source_is_refused_by_the_real_loader(self):
        """The guard runs before a socket is opened, so the tier refuses at boot."""
        with pytest.raises(PlatformCompositionError, match="plain http"):
            pd.load_distributed_policy(
                governance.PolicyDistribution(source="http://policy.corp.example/policy.json")
            )

    def test_the_redirect_handler_refuses_rather_than_changing_origin(self):
        """``urlopen`` follows 3xx and its default handler permits an http target.

        The scheme guard cannot see that, because it validates the URL we ASK for.
        """
        request = urllib.request.Request("https://config.corp.example/policy.json")
        with pytest.raises(urllib.error.HTTPError, match="refusing to follow"):
            pd._NoRedirects().redirect_request(
                request, None, 302, "Found", {}, "http://evil.invalid/policy.json"
            )

    def test_a_redirect_is_not_swallowed_as_a_not_modified(self, monkeypatch):
        """Only a 304 may become ``not_modified``; every other HTTPError escapes.

        Driven through ``_fetch_http`` with the seam raising the handler's own error
        shape, because that is where the refusal has to survive: a 3xx folded into
        "unchanged" would silently keep serving whatever was cached.
        """
        url = "https://config.corp.example/policy.json"

        def refuse(request, timeout):
            raise urllib.error.HTTPError(
                url, 302, "refusing to follow a policy-source redirect", {}, None
            )

        monkeypatch.setattr(pd, "_open", refuse)
        with pytest.raises(urllib.error.HTTPError) as excinfo:
            pd._fetch_http(pd.FetchRequest(url=url, etag="v1"))
        assert excinfo.value.code == 302

    def test_an_oversized_file_is_rejected_not_truncated(self, readonly_chain, tmp_path):
        """Truncation would parse as a document narrower than the one published."""
        oversized = tmp_path / "huge.json"
        oversized.write_bytes(b"x" * (governance.MAX_POLICY_BYTES + 1))
        with pytest.raises(PlatformCompositionError, match="ceiling"):
            pd._fetch_file(pd.FetchRequest(url=oversized.as_uri()))

    def test_an_oversized_http_body_is_detected_rather_than_truncated(self, monkeypatch):
        """The read goes ONE BYTE past the cap, which is what makes it detectable.

        Reading exactly the cap would hand back a truncated document indistinguishable
        from a short one — and a hostile endpoint must not be able to OOM boot either,
        so the read is bounded rather than the length merely checked afterwards.
        """

        class _Response:
            status = 200
            headers: dict = {}

            def read(self, amount):
                return b"x" * amount

            def __enter__(self):
                return self

            def __exit__(self, *exc):
                return False

        monkeypatch.setattr(pd, "_open", lambda request, timeout: _Response())
        with pytest.raises(PlatformCompositionError, match="ceiling"):
            pd._fetch_http(pd.FetchRequest(url="https://config.corp.example/policy.json"))

    def test_a_file_source_may_not_name_a_remote_host(self):
        with pytest.raises(PlatformCompositionError, match="remote host"):
            pd._fetch_file(pd.FetchRequest(url="file://fileserver.corp.example/policy.json"))

    def test_a_file_source_revalidates_on_a_content_digest(self, readonly_chain, tmp_path):
        """An unchanged document costs no body read, so a stable policy is cheap."""
        source = _file_source(tmp_path / "policy.json", "published")
        first = pd._fetch_file(pd.FetchRequest(url=source))
        assert first.not_modified is False
        assert first.body == _body("published")
        again = pd._fetch_file(pd.FetchRequest(url=source, etag=first.etag))
        assert again.not_modified is True
        assert again.body == b""

    def test_a_not_modified_answer_revalidates_the_cache_without_new_bytes(self, transport):
        """A 304 proves the cached bytes ARE the published document right now."""
        seen: list = []
        source = transport(_static_fetcher(pd.FetchedPolicy(etag="v1", not_modified=True), seen))
        _cache("cached", source, age_secs=_PAST_THE_WINDOW, etag="v1")
        ceiling = pd.load_distributed_policy(governance.PolicyDistribution(source=source))
        assert ceiling is not None
        assert ceiling.identity_issuer == "cached"
        # The request was CONDITIONAL, which is what makes an unchanged document
        # cost no body…
        assert [request.etag for request in seen] == ["v1"]
        # …and the confirmation restarts the copy's age, so a stable policy cannot
        # trip a staleness bound for having been confirmed.
        revalidated = pd.read_cache()
        assert revalidated is not None
        assert revalidated.age_secs() < governance.MIN_REFRESH_INTERVAL_SECS

    def test_a_not_modified_answer_with_no_cache_is_an_error(self, transport):
        """A fetcher answered "unchanged" against validators we never sent.

        Reading that as success would adopt nothing at all.
        """
        source = transport(_static_fetcher(pd.FetchedPolicy(not_modified=True)))
        assert pd.read_cache() is None
        with pytest.raises(PlatformCompositionError, match="no ceiling could be established"):
            pd.load_distributed_policy(governance.PolicyDistribution(source=source))

    def test_an_empty_body_is_a_corrupted_push_not_an_unchanged_document(self, transport):
        source = transport(_static_fetcher(pd.FetchedPolicy(body=b"")))
        dist = governance.PolicyDistribution(source=source)
        with pytest.raises(PlatformCompositionError, match="empty document"):
            pd.fetch_once(dist)
        # And it stays a configuration error through the tier — never "unreachable".
        with pytest.raises(PlatformCompositionError, match="empty document"):
            pd.load_distributed_policy(dist)


# ──────────────────────────────────────────────────────────────────────────
# The last-known-good cache
# ──────────────────────────────────────────────────────────────────────────


class TestCache:
    def test_a_write_round_trips_through_a_read(self):
        pd.write_cache(
            _body("published"),
            source=_TEST_SOURCE,
            etag='W/"abc"',
            last_modified="Wed, 21 Oct 2026 07:28:00 GMT",
        )
        cached = pd.read_cache()
        assert cached is not None
        assert cached.body == _body("published")
        assert cached.source_digest == pd._source_digest(_TEST_SOURCE)
        assert cached.etag == 'W/"abc"'
        assert cached.last_modified == "Wed, 21 Oct 2026 07:28:00 GMT"
        assert cached.age_secs() < governance.MIN_REFRESH_INTERVAL_SECS

    def test_a_future_timestamp_reads_as_age_zero_not_as_a_negative_age(self):
        """The clock-step guard, at the place a stepped clock actually shows up.

        A copy written before NTP moved the clock backwards would otherwise carry a
        negative age, which reads as impossibly fresh — and, once compared against a
        staleness bound, as a reason to refuse to boot.
        """
        pd.write_cache(_body("published"), source=_TEST_SOURCE, now=time.time() + 10_000)
        cached = pd.read_cache()
        assert cached is not None
        assert cached.age_secs() == 0.0

    def test_a_cache_from_another_source_is_not_served(self, transport):
        """The repoint rule: a retired endpoint must not keep governing this host.

        Including — especially — a repoint made to replace a compromised source.
        Asserted through the loader with the new source unreachable and the stale
        copy young enough to be served, so the only way it could surface is by
        being honoured across the repoint.
        """
        source = transport(_failing_fetcher(TimeoutError("endpoint down")))
        _cache("retired", "https://old.corp.example/policy.json")
        with pytest.raises(PlatformCompositionError, match="no ceiling could be established"):
            pd.load_distributed_policy(governance.PolicyDistribution(source=source))

    def test_corrupt_metadata_reads_as_absent(self):
        directory = pd.cache_dir()
        directory.mkdir(parents=True, exist_ok=True)
        (directory / pd._CACHE_DOC_LEAF).write_bytes(_body("published"))
        (directory / pd._CACHE_META_LEAF).write_text("{ not json", encoding="utf-8")
        assert pd.read_cache() is None

    def test_incomplete_metadata_reads_as_absent(self):
        """No recorded source means no provenance, so a stray file cannot govern."""
        directory = pd.cache_dir()
        directory.mkdir(parents=True, exist_ok=True)
        (directory / pd._CACHE_DOC_LEAF).write_bytes(_body("published"))
        (directory / pd._CACHE_META_LEAF).write_text(
            json.dumps({"fetched_at": time.time()}), encoding="utf-8"
        )
        assert pd.read_cache() is None

    def test_missing_metadata_reads_as_absent(self):
        directory = pd.cache_dir()
        directory.mkdir(parents=True, exist_ok=True)
        (directory / pd._CACHE_DOC_LEAF).write_bytes(_body("published"))
        assert pd.read_cache() is None

    def test_a_missing_document_reads_as_absent(self):
        directory = pd.cache_dir()
        directory.mkdir(parents=True, exist_ok=True)
        (directory / pd._CACHE_META_LEAF).write_text(
            json.dumps({"source": _TEST_SOURCE, "fetched_at": time.time()}), encoding="utf-8"
        )
        assert pd.read_cache() is None

    def test_touch_restarts_the_age(self):
        """A 304 proves the cached bytes ARE the published document right now.

        Without this a fleet with a stable policy and a staleness bound would
        refuse to boot for having successfully confirmed that nothing changed.
        """
        _cache("published", _TEST_SOURCE, age_secs=_PAST_THE_WINDOW)
        stale = pd.read_cache()
        assert stale is not None and stale.age_secs() >= _PAST_THE_WINDOW
        pd.touch_cache(stale.meta(), etag="v2")
        fresh = pd.read_cache()
        assert fresh is not None
        assert fresh.age_secs() < governance.MIN_REFRESH_INTERVAL_SECS
        assert fresh.body == _body("published")
        assert fresh.etag == "v2"

    def test_touch_on_an_empty_cache_creates_nothing(self):
        # A metadata-only rewrite still needs SOMETHING to rewrite: with no cached
        # document it must not manufacture one out of the provenance alone.
        pd.touch_cache(
            pd.CachedMeta(source_digest=pd._source_digest(_TEST_SOURCE), fetched_at=time.time()),
            etag="v2",
        )
        assert pd.read_cache() is None

    def test_removing_the_cache_files_reads_as_absent(self):
        """Operator recovery is ``rm`` on the directory, so absence must read as absence."""
        pd.write_cache(_body("published"), source=_TEST_SOURCE)
        directory = pd.cache_dir()
        for leaf in (pd._CACHE_DOC_LEAF, pd._CACHE_META_LEAF):
            (directory / leaf).unlink()
        assert pd.read_cache() is None
        assert pd.read_cache_meta() is None


# ──────────────────────────────────────────────────────────────────────────
# Boot dispositions
# ──────────────────────────────────────────────────────────────────────────


class TestBootDispositions:
    def test_a_file_source_loads_and_populates_the_cache(self, readonly_chain, tmp_path):
        source = _file_source(tmp_path / "policy.json", "published")
        ceiling = pd.load_distributed_policy(governance.PolicyDistribution(source=source))
        assert ceiling is not None
        assert ceiling.identity_issuer == "published"
        cached = pd.read_cache()
        assert cached is not None
        assert cached.source_digest == pd._source_digest(source)
        assert json.loads(cached.body) == _doc("published")

    def test_an_unreachable_source_serves_the_cache_and_reports_degraded(self, transport):
        """The property that makes a central ceiling safe to depend on.

        A host keeps the ceiling it was last given rather than losing governance
        because a bucket had a bad minute — and it says so, so an operator can see
        the host is running on a copy.
        """
        source = transport(_failing_fetcher(TimeoutError("endpoint down")))
        _cache("cached", source, age_secs=_PAST_THE_WINDOW)
        ceiling = pd.load_distributed_policy(
            governance.PolicyDistribution(source=source, max_cache_age_secs=_PAST_THE_WINDOW * 10)
        )
        assert ceiling is not None
        assert ceiling.identity_issuer == "cached"
        incident = health.last_incident()
        assert incident is not None
        assert incident["kind"] == "degraded"
        assert str(incident["detail"]).startswith("policy_distribution:cache")

    def test_unreachable_with_no_cache_fails_closed(self, transport):
        """A fleet that pointed a host at a central ceiling meant it to bind.

        So "we could not tell" must not read as "run unbounded".
        """
        source = transport(_failing_fetcher(TimeoutError("endpoint down")))
        with pytest.raises(PlatformCompositionError, match="Refusing to run ungoverned"):
            pd.load_distributed_policy(governance.PolicyDistribution(source=source))
        incident = health.last_incident()
        assert incident is not None and incident["kind"] == "failed_closed"

    def test_unreachable_with_no_cache_may_degrade_instead(self, transport):
        """For a fleet that would rather have a host it can SEE is degraded."""
        source = transport(_failing_fetcher(TimeoutError("endpoint down")))
        outcome = pd.load_distributed_policy(
            governance.PolicyDistribution(
                source=source, on_unavailable=governance.UNAVAILABLE_DEGRADE
            )
        )
        assert outcome is None  # the caller falls through to the next tier
        incident = health.last_incident()
        assert incident is not None
        assert incident["kind"] == "degraded"
        assert incident["detail"] == "policy_distribution:unavailable"

    def test_a_cache_past_the_staleness_bound_is_refused(self, transport):
        source = transport(_failing_fetcher(TimeoutError("endpoint down")))
        _cache("cached", source, age_secs=_PAST_THE_WINDOW)
        with pytest.raises(PlatformCompositionError, match="Refusing to run ungoverned"):
            pd.load_distributed_policy(
                governance.PolicyDistribution(
                    source=source, max_cache_age_secs=governance.MIN_REFRESH_INTERVAL_SECS
                )
            )

    @pytest.mark.parametrize(
        "body",
        [
            b"{ not a policy",
            b'{"version": 99, "boot": {}}',
        ],
        ids=["unparseable", "wrong-version"],
    )
    def test_a_bad_push_fails_and_is_never_cached(self, transport, body):
        """A poisoned last-known-good would outlive the bad push.

        The host would keep failing after the document was corrected, so a refused
        document is not written.
        """
        source = transport(_static_fetcher(pd.FetchedPolicy(body=body, etag="v1")))
        with pytest.raises(PlatformCompositionError, match="not an availability failure"):
            pd.load_distributed_policy(governance.PolicyDistribution(source=source))
        assert pd.read_cache() is None

    @pytest.mark.parametrize(
        "body",
        [b"{ not a policy", b'{"version": 99, "boot": {}}'],
        ids=["unparseable", "wrong-version"],
    )
    def test_a_refused_document_does_not_degrade_to_a_lower_tier(self, transport, body):
        """``on_unavailable`` answers reachability, not validity.

        A document this host READ and REFUSED is a different question from one it could
        not reach, and letting the refusal take the ``degrade`` path would demote the
        host onto a policy the administrator superseded — the one outcome nobody asked
        for. So it raises even under ``degrade``.
        """
        source = transport(_static_fetcher(pd.FetchedPolicy(body=body)))
        with pytest.raises(PlatformCompositionError, match="not an availability failure"):
            pd.load_distributed_policy(
                governance.PolicyDistribution(
                    source=source, on_unavailable=governance.UNAVAILABLE_DEGRADE
                )
            )

    def test_a_refused_document_still_yields_to_a_usable_cache(self, transport):
        """The cache is a ceiling the fleet published and this host verified.

        Refusing the new bytes is not a reason to discard a good older copy — only a
        reason not to fall to a LOWER tier.
        """
        source = transport(_static_fetcher(pd.FetchedPolicy(body=b"{ not a policy")))
        _cache("last-good", source, age_secs=_PAST_THE_WINDOW)
        ceiling = pd.load_distributed_policy(
            governance.PolicyDistribution(source=source, refresh_interval_secs=60)
        )
        assert ceiling is not None and ceiling.identity_issuer == "last-good"

    def test_an_unsigned_document_is_refused_when_provenance_is_mandated(
        self, transport, monkeypatch
    ):
        """The opt-in is the ADMISSION policy's ``require_policy_signature``.

        Deliberately not a key in the fetched document: a document must not be the
        authority on whether it has to be authentic.
        """
        monkeypatch.setattr(governance, "_policy_signature_required", lambda: True)
        source = transport(_static_fetcher(pd.FetchedPolicy(body=_body("published"))))
        with pytest.raises(PlatformCompositionError, match="require_policy_signature"):
            pd.load_distributed_policy(governance.PolicyDistribution(source=source))
        assert pd.read_cache() is None

    def test_a_verified_signature_satisfies_the_mandate(self, transport, monkeypatch):
        """The other half, so the refusal above cannot pass for the wrong reason."""
        secret = "fleet-trust-key"
        monkeypatch.setattr(
            governance, "_policy_trust_settings", lambda *_a: (False, {"fleet-control": secret}, {})
        )
        signed = _sign(_doc("fleet-control"), secret)
        source = transport(
            _static_fetcher(pd.FetchedPolicy(body=json.dumps(signed).encode("utf-8")))
        )
        ceiling = pd.load_distributed_policy(governance.PolicyDistribution(source=source))
        assert ceiling is not None
        assert ceiling.signature_state == governance.SIGNATURE_VERIFIED


# ──────────────────────────────────────────────────────────────────────────
# The fetch window — this tier is on the per-app-call reload path
# ──────────────────────────────────────────────────────────────────────────


class TestFetchWindow:
    """``load_security_policy`` is re-run per app callback, not only at boot.

    Without a bound, an open dashboard would put one network round trip behind
    every app call — and, against a source that is down, one timeout.
    """

    def test_a_cache_inside_the_window_is_served_without_a_fetch(self, transport):
        seen: list = []
        source = transport(_static_fetcher(pd.FetchedPolicy(body=_body("pushed")), seen))
        _cache("cached", source)
        ceiling = pd.load_distributed_policy(governance.PolicyDistribution(source=source))
        assert ceiling is not None
        assert ceiling.identity_issuer == "cached"
        assert seen == []

    def test_a_second_caller_inside_the_window_does_not_go_back_to_the_network(self, transport):
        """The cooldown records the ATTEMPT, so an outage costs one round trip.

        Asserted with the cache removed rather than stale, because a cache that
        could still be served would leave the shortcut — not the cooldown — as the
        reason nothing was fetched.
        """
        seen: list = []
        source = transport(_static_fetcher(pd.FetchedPolicy(body=_body("pushed")), seen))
        dist = governance.PolicyDistribution(source=source)
        assert pd.load_distributed_policy(dist) is not None
        assert len(seen) == 1
        # Unlinked rather than reset: reopening the window is
        # the very thing under test here.
        for leaf in (pd._CACHE_DOC_LEAF, pd._CACHE_META_LEAF):
            (pd.cache_dir() / leaf).unlink()
        with pytest.raises(PlatformCompositionError, match="already attempted"):
            pd.load_distributed_policy(dist)
        assert len(seen) == 1

    def test_reopening_the_window_permits_another_fetch(self, transport):
        """The cooldown records the ATTEMPT, so it has to be resettable.

        A restart clears it in production; the reset exists so a test — and
        ``reset_process_state`` — need not wait out a real window.
        """
        seen: list = []
        source = transport(_static_fetcher(pd.FetchedPolicy(body=_body("pushed")), seen))
        dist = governance.PolicyDistribution(source=source)
        assert pd.load_distributed_policy(dist) is not None
        for leaf in (pd._CACHE_DOC_LEAF, pd._CACHE_META_LEAF):
            (pd.cache_dir() / leaf).unlink()
        pd.reset_fetch_window()
        assert pd.load_distributed_policy(dist) is not None
        assert len(seen) == 2

    def test_an_explicit_refresh_is_never_held_by_the_cooldown(self, transport, install_ceiling):
        """An operator (or the poller) asking for a fetch gets one."""
        seen: list = []
        source = transport(_static_fetcher(pd.FetchedPolicy(body=_body("pushed")), seen))
        install_ceiling(governance.parse_policy(_doc("running", distribution={"source": source})))
        assert pd.load_distributed_policy(governance.PolicyDistribution(source=source)) is not None
        assert len(seen) == 1
        assert pd.refresh_now().status == pd.REFRESH_APPLIED
        assert len(seen) == 2


# ──────────────────────────────────────────────────────────────────────────
# Precedence inside load_security_policy
# ──────────────────────────────────────────────────────────────────────────


class TestLoadSecurityPolicyPrecedence:
    def test_the_central_source_outranks_the_local_env_file(
        self, readonly_chain, monkeypatch, tmp_path
    ):
        """The env file is a SUBORDINATE: it tightens the fetched ceiling, never wins.

        While it outranked the central document the enterprise ceiling was advisory —
        any account that can set an environment variable could point it at a permissive
        file and the fleet's ceiling never bound. The identity stays the fetched
        document's, which is what "central is the authority" means on the wire.
        """
        local = _write_policy(
            tmp_path / "local.json", "local-file", tools={"mode": "deny", "deny": ["*"]}
        )
        monkeypatch.setenv("KIROCREW_SECURITY_POLICY", str(local))
        monkeypatch.setenv(
            pd.POLICY_URL_ENV, _file_source(tmp_path / "central.json", "central-push")
        )
        ceiling = governance.load_security_policy()
        assert ceiling is not None
        assert ceiling.identity_issuer == "central-push"
        # Fetched, not short-circuited: the env file does not stop the central tier
        # from being consulted at all.
        assert pd.read_cache() is not None

    def test_the_local_env_file_only_tightens_the_central_ceiling(
        self, readonly_chain, monkeypatch, tmp_path
    ):
        """Subordinate means "may add restrictions", not "is ignored"."""
        local = _write_policy(
            tmp_path / "local.json", "local-file", tools={"mode": "deny", "deny": ["danger"]}
        )
        monkeypatch.setenv("KIROCREW_SECURITY_POLICY", str(local))
        monkeypatch.setenv(
            pd.POLICY_URL_ENV, _file_source(tmp_path / "central.json", "central-push")
        )
        ceiling = governance.load_security_policy()
        assert ceiling is not None
        assert ceiling.identity_issuer == "central-push"
        assert "danger" in ceiling.controls["tools"].deny

    def test_the_central_tier_outranks_the_home_file(self, readonly_chain, monkeypatch, tmp_path):
        """Tiers below are what the fetched document REPLACES."""
        home = _write_policy(tmp_path / "home.json", "home-authored")
        monkeypatch.setattr(governance, "_policy_home_path", lambda: home)
        monkeypatch.setenv(
            pd.POLICY_URL_ENV, _file_source(tmp_path / "central.json", "central-push")
        )
        ceiling = governance.load_security_policy()
        assert ceiling is not None
        assert ceiling.identity_issuer == "central-push"

    def test_a_home_policy_declaring_a_source_bootstraps_its_own_successor(
        self, readonly_chain, monkeypatch, tmp_path
    ):
        """Self-refresh: a fleet places one bootstrap policy and it names its heir.

        This is what makes "push a change to every instance" a property of the
        policy rather than of whatever placed it.
        """
        source = _file_source(tmp_path / "central.json", "central-push")
        home = _write_policy(tmp_path / "home.json", "bootstrap", distribution={"source": source})
        monkeypatch.setattr(governance, "_policy_home_path", lambda: home)
        ceiling = governance.load_security_policy()
        assert ceiling is not None
        assert ceiling.identity_issuer == "central-push"

    def test_a_bundled_declaration_outranks_the_home_one(
        self, readonly_chain, monkeypatch, tmp_path
    ):
        """The peek follows the same precedence the tiers themselves do."""
        bundled_source = _file_source(tmp_path / "bundled-central.json", "from-bundled")
        home_source = _file_source(tmp_path / "home-central.json", "from-home")
        home = _write_policy(
            tmp_path / "home.json", "home-decl", distribution={"source": home_source}
        )
        monkeypatch.setattr(governance, "_policy_home_path", lambda: home)
        ceiling = governance.load_security_policy(
            bundled_loader=lambda: _doc("bundled-decl", distribution={"source": bundled_source})
        )
        assert ceiling is not None
        assert ceiling.identity_issuer == "from-bundled"

    def test_with_no_source_anywhere_the_tier_is_not_even_imported(self, monkeypatch, tmp_path):
        """Behaviour is byte-identical on every install that does not use this.

        Asserted as the absence of the IMPORT rather than of a fetch, because an
        inert tier that still pulled urllib onto the trust root's import path would
        be a cost every host pays for a feature it never configured.
        """
        home = _write_policy(tmp_path / "home.json", "home-only")
        monkeypatch.setattr(governance, "_policy_home_path", lambda: home)
        monkeypatch.delitem(sys.modules, _ENGINE_MODULE, raising=False)
        ceiling = governance.load_security_policy()
        assert ceiling is not None
        assert ceiling.identity_issuer == "home-only"
        assert _ENGINE_MODULE not in sys.modules

    def test_an_unreadable_home_file_still_raises_at_its_own_tier(self, monkeypatch, tmp_path):
        """The home read moved ABOVE the central tier; the disposition did not.

        The central tier needs to see whether a lower tier declares a source, so it
        reads the home file early — but the read is captured, not acted on.
        """
        home = tmp_path / "home.json"
        home.write_text("{ not json", encoding="utf-8")
        monkeypatch.setattr(governance, "_policy_home_path", lambda: home)
        with pytest.raises(PlatformCompositionError, match="is unreadable"):
            governance.load_security_policy()

    def test_a_bundled_policy_still_outranks_an_unreadable_home_file(self, monkeypatch, tmp_path):
        """The other half of "at the same point in precedence"."""
        home = tmp_path / "home.json"
        home.write_text("{ not json", encoding="utf-8")
        monkeypatch.setattr(governance, "_policy_home_path", lambda: home)
        ceiling = governance.load_security_policy(bundled_loader=lambda: _doc("bundled"))
        assert ceiling is not None
        assert ceiling.identity_issuer == "bundled"


# ──────────────────────────────────────────────────────────────────────────
# Live refresh
# ──────────────────────────────────────────────────────────────────────────


class TestLiveRefresh:
    def test_no_source_reports_not_configured(self):
        assert pd.refresh_now().status == pd.REFRESH_NOT_CONFIGURED

    def test_an_unchanged_document_keeps_the_installed_object(self, transport, install_ceiling):
        from dataclasses import replace

        source = transport(_static_fetcher(pd.FetchedPolicy(etag="v1", not_modified=True)))
        body = _body("running", distribution={"source": source})
        # Installed as the loader installs it: parsed through the distribution tier (so
        # it carries a real signature verdict, not ``parse_policy``'s "unchecked") and
        # tagged as the central tier. The 304 path re-folds the ladder and compares the
        # result with what is installed, so a bare-parse stand-in would read as
        # "another tier moved" for its tag or verdict alone.
        running = install_ceiling(
            replace(
                pd.parse_distributed_policy(body, source=source),
                tier=governance.TIER_CENTRAL,
            )
        )
        pd.write_cache(body, source=source, etag="v1")
        # Mirror what the loader does on every path that installs a ceiling: without
        # this the process does not know what it is running, which is a DIFFERENT
        # scenario (see the degraded-boot test below).
        pd._record_installed(body)
        outcome = pd.refresh_now()
        assert outcome.status == pd.REFRESH_UNCHANGED
        assert current_context().governance is running

    def test_a_304_adopts_a_cache_another_process_moved_ahead(self, transport, install_ceiling):
        """A 304 means "nothing newer than the CACHE", not "you are already running it".

        The cache is written by other processes too — gatewayd's per-app-call reload,
        an app backend's boot, and the ``kirocrew policy fetch`` the operator guide
        recommends for verifying a rollout. Without this, one ``policy fetch`` would
        cache v2 and this poller would then report ``unchanged`` forever while the
        gateway kept enforcing v1: the live-refresh guarantee silently withdrawn,
        under the most reassuring status there is.
        """
        source = transport(_static_fetcher(pd.FetchedPolicy(etag="v2", not_modified=True)))
        install_ceiling(governance.parse_policy(_doc("running", distribution={"source": source})))
        # This process installed v1 …
        pd._record_installed(_body("running", distribution={"source": source}))
        # … and something else cached v2 behind its back.
        pd.write_cache(_body("pushed", distribution={"source": source}), source=source, etag="v2")

        outcome = pd.refresh_now()

        assert outcome.status == pd.REFRESH_APPLIED
        governing = current_context().governance
        assert governing is not None and governing.identity_issuer == "pushed"

    def test_a_304_lets_a_degraded_boot_catch_up(self, transport, install_ceiling):
        """An EMPTY installed digest means "we do not know what we are running".

        That is the state a boot which DEGRADED to ungoverned leaves behind. Reading it
        as "skip" would strand such a host permanently below a ceiling another process
        had already cached, so the cache is adopted instead. Tier-1 precedence — the one
        case where not installing is correct — is guarded separately, by asking the
        loader ladder's own question rather than inferring it from a digest.
        """
        source = transport(_static_fetcher(pd.FetchedPolicy(etag="v2", not_modified=True)))
        install_ceiling(
            governance.parse_policy(_doc("degraded-boot", distribution={"source": source}))
        )
        pd.write_cache(_body("central", distribution={"source": source}), source=source, etag="v2")
        assert pd._installed_digest() == ""

        outcome = pd.refresh_now()

        assert outcome.status == pd.REFRESH_APPLIED
        governing = current_context().governance
        assert governing is not None and governing.identity_issuer == "central"

    def test_a_changed_valid_document_is_installed(self, transport, install_ceiling):
        source = transport(_static_fetcher(pd.FetchedPolicy(body=_body("pushed"), etag="v2")))
        install_ceiling(governance.parse_policy(_doc("running", distribution={"source": source})))
        outcome = pd.refresh_now()
        assert outcome.status == pd.REFRESH_APPLIED
        governing = current_context().governance
        assert governing is not None
        assert governing.identity_issuer == "pushed"
        # The bytes that were installed become the new last-known-good.
        cached = pd.read_cache()
        assert cached is not None and cached.etag == "v2"

    def test_an_unparseable_push_is_rejected_and_the_running_ceiling_survives(
        self, transport, install_ceiling
    ):
        """The asymmetry that stops one typo taking down a fleet that is already up.

        At boot there is nothing to fall back to; on a live refresh there is, so a
        candidate that does not clear the gates is refused and the ceiling already
        installed keeps governing.
        """
        source = transport(_static_fetcher(pd.FetchedPolicy(body=b"{ not a policy")))
        running = install_ceiling(
            governance.parse_policy(_doc("running", distribution={"source": source}))
        )
        outcome = pd.refresh_now()
        assert outcome.status == pd.REFRESH_REJECTED
        assert current_context().governance is running

    def test_a_push_that_does_not_compose_is_rejected_and_the_ceiling_survives(
        self, transport, install_ceiling, profiles_dir
    ):
        """Well-formed, but this host cannot run under it.

        A bound profile is looser than the candidate on an ordinal, so boot would
        have refused to start under it — and ``apply_ceiling`` runs the same floor
        gates, which is the whole reason a refresh validates before installing.
        """
        (profiles_dir / "weak.json").write_text(
            json.dumps(
                {
                    "name": "weak",
                    "bind": {"type": "surface", "id": "cron"},
                    "sandbox": {"min_level": "off"},
                }
            ),
            encoding="utf-8",
        )
        gp.reset_store()
        source = transport(
            _static_fetcher(pd.FetchedPolicy(body=_body("pushed", sandbox={"min_level": "strict"})))
        )
        running = install_ceiling(
            governance.parse_policy(_doc("running", distribution={"source": source}))
        )
        outcome = pd.refresh_now()
        assert outcome.status == pd.REFRESH_REJECTED
        assert "does not compose" in outcome.detail
        assert current_context().governance is running
        assert pd.read_cache() is None  # a refused ceiling is never cached

    def test_an_unreachable_source_keeps_the_running_ceiling(self, transport, install_ceiling):
        source = transport(_failing_fetcher(TimeoutError("endpoint down")))
        running = install_ceiling(
            governance.parse_policy(_doc("running", distribution={"source": source}))
        )
        outcome = pd.refresh_now()
        assert outcome.status == pd.REFRESH_UNREACHABLE
        assert current_context().governance is running
        incident = health.last_incident()
        assert incident is not None and incident["kind"] == "degraded"

    def test_a_misconfigured_source_is_rejected_rather_than_raised(
        self, monkeypatch, install_ceiling
    ):
        install_ceiling(
            governance.parse_policy(_doc("running", distribution={"source": _TEST_SOURCE}))
        )
        monkeypatch.setenv(pd.POLICY_REFRESH_ENV, "15m")
        outcome = pd.refresh_now()
        assert outcome.status == pd.REFRESH_REJECTED
        assert "misconfigured" in outcome.detail

    def test_refresh_never_raises_on_any_failure_path(self, transport, install_ceiling):
        """It runs on a background timer AND from an operator command.

        Neither wants an exception for "the endpoint was down", so every failure
        mode reports a named status instead — and none of them installs anything,
        so the running ceiling survives all of them.
        """
        running = install_ceiling(
            governance.parse_policy(_doc("running", distribution={"source": _TEST_SOURCE}))
        )
        hostile = [
            _failing_fetcher(TimeoutError("endpoint down")),
            _failing_fetcher(OSError("connection reset")),
            _failing_fetcher(RuntimeError("a fetcher raised something unexpected")),
            _static_fetcher(pd.FetchedPolicy(body=b"")),
            _static_fetcher(pd.FetchedPolicy(body=b"{ not a policy")),
            _static_fetcher(pd.FetchedPolicy(body=b'{"version": 99, "boot": {}}')),
            _static_fetcher(pd.FetchedPolicy(not_modified=True)),
        ]
        for fetcher in hostile:
            transport(fetcher)
            outcome = pd.refresh_now(force=True)  # must not raise
            assert outcome.status in _REFRESH_STATUSES, outcome
            assert outcome.status != pd.REFRESH_APPLIED, outcome
            assert current_context().governance is running

    def test_the_refresher_does_not_poll_without_a_source_or_an_interval(self, install_ceiling):
        """Both halves of "no loop", asserted so neither starts a stray thread."""
        assert pd.start_refresher() is False
        install_ceiling(
            governance.parse_policy(_doc("running", distribution={"source": _TEST_SOURCE}))
        )
        assert pd.start_refresher() is False  # interval 0 = fetch at boot only
        assert pd.refresher_running() is False

    def test_a_declared_interval_starts_one_loop_and_stops_cleanly(self, install_ceiling):
        """Idempotent start, so a process with two entry points polls once."""
        install_ceiling(
            governance.parse_policy(
                _doc(
                    "running",
                    distribution={
                        "source": _TEST_SOURCE,
                        "refresh_interval_secs": governance.MIN_REFRESH_INTERVAL_SECS,
                    },
                )
            )
        )
        try:
            assert pd.start_refresher() is True
            assert pd.refresher_running() is True
            assert pd.start_refresher() is False
        finally:
            pd.stop_refresher(2.0)
        assert pd.refresher_running() is False


# ──────────────────────────────────────────────────────────────────────────
# The ceiling swap and the one cache it has to invalidate
# ──────────────────────────────────────────────────────────────────────────


class TestCeilingSwapInvalidatesProfiles:
    @staticmethod
    def _count_reloads(monkeypatch) -> list:
        """Spy on (never replace) the store's reload, so the real one still runs."""
        reloads: list = []
        original = gp.ProfileStore._reload

        def spy(store, directory):
            reloads.append(directory)
            return original(store, directory)

        monkeypatch.setattr(gp.ProfileStore, "_reload", spy)
        return reloads

    def test_a_swap_bumps_the_generation_and_reloads_a_warm_store(
        self, monkeypatch, install_ceiling, profiles_dir
    ):
        """The ceiling is not boot-frozen, so a warm snapshot can go stale.

        Every profile is composed against a ceiling; serving one composed against
        the retired ceiling would apply a narrowing nobody currently declares.
        """
        from kiro_crew.platform.context import governance_generation

        (profiles_dir / "host.json").write_text(
            json.dumps({"name": "host", "bind": {"type": "surface", "id": "host"}}),
            encoding="utf-8",
        )
        gp.reset_store()
        # Spy BEFORE the install: a ceiling install now warms the store eagerly,
        # because ``safety_override``'s ceiling-install hook resolves ``approval_modes``
        # against the new ceiling and that read composes a profile. The invariant under
        # test is unchanged -- one reload per ceiling, and no snapshot composed against
        # the retired one is ever served -- only who triggers it.
        reloads = self._count_reloads(monkeypatch)
        install_ceiling(governance.parse_policy(_doc("first")))
        assert gp._STORE._ensure_fresh() is True
        before_token = gp._ceiling_token()
        before_generation = governance_generation()
        before_fingerprint = gp._STORE._fingerprint
        assert len(reloads) == 1

        install_ceiling(governance.parse_policy(_doc("second")))
        assert governance_generation() > before_generation
        assert gp._ceiling_token() != before_token
        assert gp._STORE._ensure_fresh() is True
        assert gp._STORE._fingerprint != before_fingerprint
        assert len(reloads) == 2

    def test_swapping_one_declared_fallback_for_another_still_busts_the_key(self, install_ceiling):
        """The case the fallback-declared boolean alone cannot catch.

        It is ``True`` on both sides of this swap, so without the generation the
        store would keep serving profiles built against the retired ceiling.
        """
        first = install_ceiling(
            governance.parse_policy(
                _doc("first", fallback={"tools": {"mode": "allow", "allow": ["read"]}})
            )
        )
        first_token = gp._ceiling_token()
        second = install_ceiling(
            governance.parse_policy(
                _doc("second", fallback={"tools": {"mode": "allow", "allow": ["grep"]}})
            )
        )
        second_token = gp._ceiling_token()
        assert first.fallback_profile is not None and second.fallback_profile is not None
        assert first_token[0] is True and second_token[0] is True
        assert first_token != second_token


class TestTheDetailNeverNamesTheEndpoint:
    """``kirocrew policy fetch`` is an ordinary shell command an agent can run.

    Every other consumer of these messages — the gateway log, a boot abort — is an
    operator surface and wants the address.
    """

    @pytest.mark.parametrize(
        "fetcher",
        [
            _failing_fetcher(TimeoutError("connect to https://secret.corp.example/p.json failed")),
            _static_fetcher(pd.FetchedPolicy(body=b"{ not a policy")),
            _static_fetcher(pd.FetchedPolicy(body=b"")),
        ],
        ids=["unreachable", "unparseable", "empty"],
    )
    def test_no_failure_path_returns_the_url(self, transport, install_ceiling, fetcher):
        source = transport(fetcher)
        install_ceiling(governance.parse_policy(_doc("running", distribution={"source": source})))
        outcome = pd.refresh_now()
        assert outcome.status != pd.REFRESH_APPLIED
        assert source not in outcome.detail
        assert "policy.example" not in outcome.detail

    def test_the_scheme_survives_so_the_message_still_says_something(self):
        redacted = pd._redact_source("could not fetch kctest://host/p.json: down", _TEST_SOURCE)
        assert "kctest://host/p.json" in redacted or "<the kctest policy source>" in redacted

    def test_the_log_never_carries_a_traceback_naming_the_source(self, caplog):
        """A traceback prints the exception MESSAGE, and a parse refusal names the source
        it refused — which for a pre-signed URL is itself the credential. The log ring is
        served by ``GET /api/logs`` and rendered in a dashboard the agent's own browser
        tooling can drive, so a log line is not a boundary here."""
        secret = "https://signed.corp.example/p.json?X-Amz-Signature=deadbeefcafe"
        _cache_raw(b"{ not a policy", source=secret)
        dist = governance.PolicyDistribution(source=secret)

        with caplog.at_level(logging.DEBUG, logger="kiro_crew.platform.policy_distribution"):
            pd._from_cache_on_outage(dist, pd.read_cache(), "the endpoint was down")

        emitted = "\n".join(r.getMessage() + (pd_format_exc(r) or "") for r in caplog.records)
        assert emitted, "the arm under test must log something, or this proves nothing"
        assert secret not in emitted
        assert "X-Amz-Signature" not in emitted
        assert "signed.corp.example" not in emitted

    def test_a_transport_error_carrying_a_credential_is_redacted_not_just_the_source(
        self, transport, caplog, monkeypatch
    ):
        """The exception text is the ENDPOINT's, not ours: an error page, or a proxy echoing
        the request back, can carry the credential. ``_redact_source`` only knows the
        configured source, so the reason has to go through the full sanitiser."""
        leaked = "AKIAIOSFODNN7EXAMPLE"
        source = transport(
            _failing_fetcher(TimeoutError(f"upstream said: aws_access_key_id={leaked}"))
        )
        # The per-window cooldown would otherwise divert the second call to a different
        # reason ("already attempted"), which never carries the endpoint's text at all.
        monkeypatch.setattr(pd, "_claim_fetch_slot", lambda window: True)

        with caplog.at_level(logging.DEBUG, logger="kiro_crew.platform.policy_distribution"):
            with pytest.raises(PlatformCompositionError) as caught:
                pd.load_distributed_policy(governance.PolicyDistribution(source=source))

        # The boot abort reaches stderr and any supervisor capturing it.
        assert leaked not in str(caught.value)
        emitted = "\n".join(r.getMessage() + (pd_format_exc(r) or "") for r in caplog.records)
        assert emitted, "the arm under test must log the reason, or this proves nothing"
        assert leaked not in emitted

    def test_a_304_against_an_empty_cache_does_not_name_the_source(self, transport, monkeypatch):
        """This reason interpolated the source with no redaction at all."""
        source = transport(_static_fetcher(pd.FetchedPolicy(etag="v1", not_modified=True)))
        monkeypatch.setattr(pd, "_claim_fetch_slot", lambda window: True)

        with pytest.raises(PlatformCompositionError) as caught:
            pd.load_distributed_policy(governance.PolicyDistribution(source=source))
        assert source not in str(caught.value)
        assert "policy.example" not in str(caught.value)

    def test_an_echoed_request_header_is_substituted_by_value(self, transport, monkeypatch):
        """``security.redact`` recognises credential SHAPES, and
        ``KIROCREW_POLICY_HEADERS`` is deliberately arbitrary: an opaque ``X-Fleet-Key``
        matches no pattern anyone can write. What makes it tractable is that the value is
        not a pattern to us — we sent it, so we can substitute the string itself."""
        opaque = "zqf7Wm2pLx9Kd4Rt8Nv1Ba6Ce0Yh"  # matches no known credential shape
        monkeypatch.setenv(pd.POLICY_HEADERS_ENV, json.dumps({"X-Fleet-Key": opaque}))
        assert (
            security.redact(opaque) == opaque
        ), "the premise: pattern matching alone cannot reach this value"

        source = transport(
            _failing_fetcher(TimeoutError(f"proxy echoed the request: X-Fleet-Key: {opaque}"))
        )
        monkeypatch.setattr(pd, "_claim_fetch_slot", lambda window: True)

        with pytest.raises(PlatformCompositionError) as caught:
            pd.load_distributed_policy(governance.PolicyDistribution(source=source))
        assert opaque not in str(caught.value)
        assert "X-Fleet-Key" in str(caught.value), "the header NAME is diagnostic, not secret"

    def test_a_short_header_value_is_not_substituted(self, monkeypatch):
        """Replacing a 1-3 character string would corrupt every message containing those
        characters — more damage than the leak it guards against, and not a credential at
        that length."""
        monkeypatch.setenv(pd.POLICY_HEADERS_ENV, json.dumps({"X-Ver": "2"}))
        assert pd._request_header_secrets() == ()
        assert pd._sanitize_detail("fetched 2 documents", "") == "fetched 2 documents"

    def test_longer_values_are_substituted_before_shorter_prefixes(self, monkeypatch):
        """Longest first, or a value that is a prefix of another leaves its remainder."""
        monkeypatch.setenv(
            pd.POLICY_HEADERS_ENV,
            json.dumps({"A": "secretvalue", "B": "secretvalueEXTRA"}),
        )
        cleaned = pd._sanitize_detail("saw secretvalueEXTRA here", "")
        assert "secretvalue" not in cleaned
        assert "EXTRA" not in cleaned

    def test_malformed_headers_do_not_break_sanitisation(self, monkeypatch):
        """``request_headers`` refuses malformed JSON by design, and that refusal must not
        become an exception on a logging path."""
        monkeypatch.setenv(pd.POLICY_HEADERS_ENV, "{not json")
        assert pd._request_header_secrets() == ()
        assert pd._sanitize_detail("a plain message", "") == "a plain message"

    def test_the_loader_fallthrough_is_redacted_too(self, transport, caplog):
        """The sibling arm: an unusable cache on the ordinary load path."""
        secret = "https://signed.corp.example/p.json?X-Amz-Signature=deadbeefcafe"
        source = transport(_static_fetcher(pd.FetchedPolicy(body=_body("central"))))
        _cache_raw(b"{ not a policy", source=source)
        declared = governance.PolicyDistribution(source=source)

        with caplog.at_level(logging.DEBUG, logger="kiro_crew.platform.policy_distribution"):
            pd.load_distributed_policy(declared)

        emitted = "\n".join(r.getMessage() + (pd_format_exc(r) or "") for r in caplog.records)
        assert "unusable" in emitted, "the fallthrough must have been taken"
        assert not any(
            r.exc_info for r in caplog.records
        ), "a traceback here would print the parse refusal, which names the source"
        assert secret not in emitted
