"""Central policy distribution — one ceiling, followed by the whole fleet.

Covers ``governance.PolicyDistribution`` (the parsed declaration), the
``platform.policy_distribution`` engine (source resolution, the transport seam,
the last-known-good cache, the boot dispositions and the live refresh), the
tier's place in ``load_security_policy``'s precedence, and the keystone that
keeps the cache unwritable by the agent.

**Nothing here touches the network.** Every transport case either registers a
fake fetcher through the public ``register_policy_fetcher`` seam or drives a
``file://`` source under ``tmp_path``; the one plain-``http`` case is refused
before a socket is opened. Patching ``urllib.request.urlopen`` would NOT
intercept this module — it opens through the named ``_open`` seam.
"""

from __future__ import annotations

import contextlib
import hashlib
import hmac
import json
import logging
import os
import stat
import sys
import time
import traceback
import urllib.error
import urllib.request
from pathlib import Path

import pytest

from kiro_crew import platform_compat, security
from kiro_crew.hooks import validate_file_path
from kiro_crew.platform import governance
from kiro_crew.platform import governance_health as health
from kiro_crew.platform import governance_profiles as gp
from kiro_crew.platform import policy_distribution as pd
from kiro_crew.platform.context import PlatformCompositionError, current_context

#: The import the boot tier must NOT perform on a host that configures no source.
_ENGINE_MODULE = "kiro_crew.platform.policy_distribution"

#: A scheme no built-in handles, so a fake fetcher registered for it cannot be
#: confused with (or shadowed by) the real https/http/file transports.
_TEST_SCHEME = "kctest"
_TEST_SOURCE = f"{_TEST_SCHEME}://policy.example/security_policy.json"

#: A cache age comfortably past the widest fetch window, for the tests whose
#: subject is the fetch rather than the shortcut over it.
_PAST_THE_WINDOW = governance.MIN_REFRESH_INTERVAL_SECS * 100

#: Every status ``refresh_now`` may report. Named states rather than a boolean
#: because the CLI, the viewer and the audit trail all have to tell "nothing
#: changed" apart from "a push was refused".
_REFRESH_STATUSES = (
    pd.REFRESH_NOT_CONFIGURED,
    pd.REFRESH_UNCHANGED,
    pd.REFRESH_APPLIED,
    pd.REFRESH_REJECTED,
    pd.REFRESH_UNREACHABLE,
)


# ──────────────────────────────────────────────────────────────────────────
# Helpers
# ──────────────────────────────────────────────────────────────────────────


def _doc(marker: str = "", **extra: object) -> dict:
    """A minimal valid policy document, tagged by ``identity.issuer``.

    The issuer is the marker because it survives ``parse_policy`` verbatim onto
    ``GovernanceCeiling.identity_issuer``, so a precedence assertion can name the
    document that won without parsing a control out of it.
    """
    body: dict = {"version": 1, "boot": {"fail_closed": True}}
    if marker:
        body["identity"] = {"issuer": marker}
    body.update(extra)
    return body


def _body(marker: str = "", **extra: object) -> bytes:
    return json.dumps(_doc(marker, **extra)).encode("utf-8")


def _write_policy(path: Path, marker: str = "", **extra: object) -> Path:
    path.write_text(json.dumps(_doc(marker, **extra)), encoding="utf-8")
    return path


def _cache(marker: str, source: str, *, age_secs: float = 0.0, etag: str = "") -> None:
    """Seed the last-known-good cache with a document of a chosen age.

    The age is the load path's control flow, not decoration: a copy younger than
    the fetch window is served as a shortcut and no transport is consulted at all,
    so a test about fetching has to age its cache past that window.
    """
    pd.write_cache(_body(marker), source=source, etag=etag, now=time.time() - age_secs)


#: The uid/gid mode-bit tests below monkeypatch ``os.getuid`` and friends, and none of
#: them exist on Windows -- ``monkeypatch.setattr`` would raise before the assertion ran.
#: The off-POSIX contract is what the two ``fails_closed_off_posix`` tests cover, and they
#: stay unguarded, so neither platform's answer is left untested.
_POSIX_IDS_ONLY = pytest.mark.skipif(
    not hasattr(os, "getgroups"),
    reason="POSIX uid/gid mode-bit semantics; os.getgroups does not exist on Windows",
)


class _FakeRequest:
    """The one attribute ``_NoRedirects.redirect_request`` reads off a urllib request."""

    def __init__(self, url: str) -> None:
        self.full_url = url


def _cache_raw(body: bytes, *, source: str, etag: str = "") -> None:
    """Seed the cache with bytes that are NOT a valid policy.

    For the arms that must survive an unusable cached copy, where the point is the
    parse REFUSAL and what it is allowed to say about the source.
    """
    pd.write_cache(body, source=source, etag=etag)


def pd_format_exc(record: logging.LogRecord) -> str:
    """The traceback a handler would print for *record*, or "" when it carries none.

    ``getMessage()`` alone cannot see an ``exc_info=True`` leak: the exception text is
    formatted by the handler, not stored in the message, so a test that reads only the
    message would pass while the log ring printed the source.
    """
    if not record.exc_info:
        return ""
    return "".join(traceback.format_exception(*record.exc_info))


def _file_source(path: Path, marker: str = "", **extra: object) -> str:
    """A published ``file://`` document, returned as its absolute URL.

    Written READ-ONLY because that is a precondition of the transport rather than a
    nicety. The ANCESTOR half of that precondition cannot be satisfied under ``tmp_path``
    at all — ``/tmp`` is world-writable, so the walk always finds a way in — which is the
    control working, not a test problem. Callers whose subject is something else
    (precedence, dispositions, the digest validator) use the ``readonly_chain`` fixture to
    stub the ancestor walk; the control itself is tested against real paths in
    ``TestAFileSourceMustNotBeAgentWritable``.
    """
    written = _write_policy(path, marker, **extra)
    written.chmod(0o444)
    return written.as_uri()


def _sign(doc: dict, secret: str) -> dict:
    """*doc* with a valid ``identity.signature`` for its declared issuer."""
    signed = json.loads(json.dumps(doc))
    signed.setdefault("identity", {})["signature"] = hmac.new(
        secret.encode("utf-8"), governance.policy_signing_payload(signed), hashlib.sha256
    ).hexdigest()
    return signed


def _static_fetcher(result: pd.FetchedPolicy, seen: list | None = None):
    """A fetcher that always answers *result*, recording the requests it saw."""

    def fetch(request: pd.FetchRequest) -> pd.FetchedPolicy:
        if seen is not None:
            seen.append(request)
        return result

    return fetch


def _failing_fetcher(exc: BaseException):
    def fetch(request: pd.FetchRequest) -> pd.FetchedPolicy:
        raise exc

    return fetch


# ──────────────────────────────────────────────────────────────────────────
# Fixtures
# ──────────────────────────────────────────────────────────────────────────


@pytest.fixture(autouse=True)
def _hermetic_governance_globals(monkeypatch):
    """Pin the three process globals this module perturbs.

    ``governance_health`` records the last incident and ``governance_profiles``
    caches profiles, both for the life of the worker, so a mark or a snapshot must
    not leak in either direction. The two env vars are deleted because they select
    a policy tier: an operator (or CI image) with either set would otherwise have
    every precedence assertion here read a file this module never wrote.

    The fetch window is reopened at SETUP, so no test here depends on the previous
    one having torn itself down — the rootdir conftest reopens it after every test,
    which leaves the first test in a worker exposed to whatever ran before pytest
    installed that guard.
    """
    monkeypatch.delenv("KIROCREW_SECURITY_POLICY", raising=False)
    monkeypatch.delenv("KIROCREW_ADMISSION_POLICY", raising=False)
    pd.reset_fetch_window()
    health.reset()
    gp.reset_store()
    yield
    health.reset()
    gp.reset_store()


@pytest.fixture
def transport():
    """Register a fake transport, restoring the module's scheme table after.

    ``_FETCHERS`` is a module global shared by every test on this worker, so the
    snapshot/restore is not tidiness — a leaked scheme would silently answer some
    later test's fetch. Yields ``register(fetcher) -> url``.
    """
    snapshot = dict(pd._FETCHERS)
    try:

        def register(fetcher, scheme: str = _TEST_SCHEME) -> str:
            # Assigned directly rather than through ``register_policy_fetcher``: a test
            # that walks several fetchers (each failure mode in turn) re-registers the
            # same scheme, and the public seam RAISES on that by design — the behaviour
            # ``test_a_conflicting_fetcher_registration_raises_rather_than_shadowing``
            # pins. The table is snapshotted and restored below, so this cannot leak.
            with pd._FETCHER_LOCK:
                pd._FETCHERS[scheme] = fetcher
            return f"{scheme}://policy.example/security_policy.json"

        yield register
    finally:
        with pd._FETCHER_LOCK:
            pd._FETCHERS.clear()
            pd._FETCHERS.update(snapshot)


@pytest.fixture
def readonly_chain(monkeypatch):
    """Treat a ``tmp_path`` source as non-writable, leaf and ancestors both.

    For tests whose subject is NOT that precondition. Two separate reasons a real
    ``file://`` source under ``tmp_path`` cannot satisfy it: ``/tmp`` is world-writable,
    so the ancestor walk always finds a way in; and off POSIX both predicates answer
    ``True`` unconditionally, because mode bits carry no answer there — so without this
    every ``file://`` test would refuse on Windows for a reason it is not testing.

    Both halves are exercised for real, per platform, by the tests that mean them:
    ``TestAFileSourceMustNotBeAgentWritable`` (POSIX mode bits, real paths) and
    ``TestAnUnverifiableSourceIsTreatedAsWritable`` (the off-POSIX answer).
    """
    monkeypatch.setattr(pd, "path_writable_by_current_user", lambda path: False)
    monkeypatch.setattr(pd, "stat_writable_by_current_user", lambda st: False)


@pytest.fixture
def install_ceiling():
    """Install a process context carrying a given ceiling; returns ``install``.

    The base context is composed while the fixture is set up — i.e. BEFORE the
    test body configures a source — because ``current_context()`` builds the
    standalone default by calling ``load_security_policy()``. Composing it lazily
    inside ``apply_ceiling`` would make the tier under test run as a side effect
    of merely reading the context.
    """
    from dataclasses import replace

    from kiro_crew.config.loader import KiroCrewConfig
    from kiro_crew.platform import context as context_mod
    from kiro_crew.platform.bootstrap import build_default_context

    base = build_default_context(KiroCrewConfig.load())

    def install(ceiling):
        context_mod.set_context(replace(base, governance=ceiling))
        return ceiling

    return install


@pytest.fixture
def profiles_dir(tmp_path, monkeypatch):
    """An empty profiles directory the store reads instead of the real home."""
    directory = tmp_path / "profiles"
    directory.mkdir()
    monkeypatch.setattr(gp, "_PROFILES_DIR", directory)
    gp.reset_store()
    return directory


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


_DECLARED = governance.PolicyDistribution(
    source="https://config.corp.example/policy.json",
    refresh_interval_secs=900,
    timeout_secs=7.0,
    max_cache_age_secs=3600,
    on_unavailable=governance.UNAVAILABLE_FAIL_CLOSED,
)


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
            governance, "_policy_trust_settings", lambda: (False, {"fleet-control": secret})
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
    def test_the_local_env_file_outranks_a_configured_central_source(self, monkeypatch, tmp_path):
        """Tier 1 is the ROLLBACK LEVER for a bad central push.

        One document governing every host is the widest blast radius in this model,
        so an operator recovering from a push needs a channel that outranks the
        thing that broke and does not wait on the endpoint being fixed.
        """
        local = _write_policy(tmp_path / "local.json", "local-rollback")
        monkeypatch.setenv("KIROCREW_SECURITY_POLICY", str(local))
        monkeypatch.setenv(
            pd.POLICY_URL_ENV, _file_source(tmp_path / "central.json", "central-push")
        )
        ceiling = governance.load_security_policy()
        assert ceiling is not None
        assert ceiling.identity_issuer == "local-rollback"
        # Not merely outranked — never fetched, so a broken endpoint cannot even
        # slow the recovery down.
        assert pd.read_cache() is None

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
        source = transport(_static_fetcher(pd.FetchedPolicy(etag="v1", not_modified=True)))
        running = install_ceiling(
            governance.parse_policy(_doc("running", distribution={"source": source}))
        )
        body = _body("running", distribution={"source": source})
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
        """The ceiling is no longer boot-frozen, so a warm snapshot can go stale.

        Every profile is composed against a ceiling; serving one composed against
        the retired ceiling would apply a narrowing nobody currently declares.
        """
        from kiro_crew.platform.context import governance_generation

        (profiles_dir / "host.json").write_text(
            json.dumps({"name": "host", "bind": {"type": "surface", "id": "host"}}),
            encoding="utf-8",
        )
        gp.reset_store()
        install_ceiling(governance.parse_policy(_doc("first")))
        reloads = self._count_reloads(monkeypatch)
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


# ──────────────────────────────────────────────────────────────────────────
# Keystone — the cache is a trust root
# ──────────────────────────────────────────────────────────────────────────


_CACHE_PATHS = [
    f"~/{prefix}/{pd.CACHE_DIR_LEAF}/{leaf}"
    for prefix in (".kiro/crew", ".kirocrew")
    for leaf in (pd._CACHE_DOC_LEAF, pd._CACHE_META_LEAF)
]


class TestTierOneOutranksTheCentralSource:
    """The rollback lever has to win against a live REFRESH, not only against a boot."""

    def test_a_local_policy_file_stops_the_refresh_before_it_fetches(
        self, transport, install_ceiling, monkeypatch, tmp_path
    ):
        seen: list = []
        source = transport(_static_fetcher(pd.FetchedPolicy(body=_body("pushed")), seen))
        running = install_ceiling(
            governance.parse_policy(_doc("running", distribution={"source": source}))
        )
        pd._record_installed(_body("running", distribution={"source": source}))
        monkeypatch.setenv(
            "KIROCREW_SECURITY_POLICY", str(_write_policy(tmp_path / "rollback.json", "rollback"))
        )

        outcome = pd.refresh_now()

        assert outcome.status == pd.REFRESH_REJECTED
        assert "outranks" in outcome.detail
        assert current_context().governance is running
        # Not even attempted: the operator gets a reason, not a fetch then a refusal.
        assert seen == []

    def test_apply_ceiling_refuses_outright_as_the_hard_guard(self, monkeypatch, tmp_path):
        """Belt to the braces above, so no future path can install over tier 1."""
        monkeypatch.setenv(
            "KIROCREW_SECURITY_POLICY", str(_write_policy(tmp_path / "rollback.json", "rollback"))
        )
        with pytest.raises(PlatformCompositionError, match="rollback lever"):
            pd.apply_ceiling(governance.parse_policy(_doc("pushed")))

    def test_an_env_path_that_does_not_exist_is_not_tier_one(self, monkeypatch, tmp_path):
        """A stale variable naming a deleted file must not freeze the fleet policy."""
        monkeypatch.setenv("KIROCREW_SECURITY_POLICY", str(tmp_path / "gone.json"))
        assert pd.tier1_local_policy() == ""

    def test_the_background_loop_stops_itself_when_a_rollback_lands(
        self, install_ceiling, monkeypatch, tmp_path
    ):
        """Polling on would fetch and refuse every cycle."""
        install_ceiling(
            governance.parse_policy(_doc("running", distribution={"source": _TEST_SOURCE}))
        )
        assert pd._Refresher._next_interval(900) == 900
        monkeypatch.setenv(
            "KIROCREW_SECURITY_POLICY", str(_write_policy(tmp_path / "rollback.json", "rollback"))
        )
        assert pd._Refresher._next_interval(900) == 0


class TestFileTransportIsBounded:
    @pytest.mark.skipif(
        not hasattr(os, "mkfifo"),
        reason=(
            "no os.mkfifo on Windows, and there is no substitute that reaches the same "
            "check: the S_ISREG guard runs on an already-open handle, and a Windows named "
            "pipe cannot be handed to os.open by path this way. The guard itself is "
            "platform-independent code, so POSIX coverage is the whole of it."
        ),
    )
    def test_a_non_regular_file_is_refused(self, tmp_path):
        """A FIFO would make the read block forever — a boot that hangs, not fails."""
        fifo = tmp_path / "policy.fifo"
        os.mkfifo(fifo)
        with pytest.raises(PlatformCompositionError, match="not a regular file"):
            pd.fetch_once(governance.PolicyDistribution(source=fifo.as_uri()))

    def test_the_size_is_judged_on_the_open_handle(self, readonly_chain, tmp_path, monkeypatch):
        """Stat-then-read is two trips to a path that can change in between.

        The bytes read must be the bytes measured, or a file swapped in after the stat
        defeats the ceiling.
        """
        path = tmp_path / "policy.json"
        path.write_bytes(b"x" * (governance.MAX_POLICY_BYTES + 64))
        path.chmod(0o444)
        with pytest.raises(PlatformCompositionError, match="exceeds"):
            pd.fetch_once(governance.PolicyDistribution(source=path.as_uri()))


@pytest.mark.skipif(
    sys.platform == "win32",
    reason=(
        "every assertion here is about POSIX mode bits, and off POSIX the predicates "
        "answer True unconditionally -- so a Windows run would pass the refusals for the "
        "platform reason rather than the one under test, and could not run the acceptance "
        "half at all. The off-POSIX contract is pinned by "
        "TestAnUnverifiableSourceIsTreatedAsWritable instead. Guarded rather than listed "
        "in windows-expected-failures.txt: that list is a burn-down backlog, and this is "
        "a platform boundary until the DACL can be read."
    ),
)
class TestAFileSourceMustNotBeAgentWritable:
    """A distribution source this account can rewrite is one an agent can rewrite.

    It runs as the same uid, so a writable path would let it publish itself a ceiling that
    the refresher installs without even a restart. The field manual already tells operators
    to distribute to a read-only, root-owned path; this makes that a precondition.
    """

    @pytest.mark.parametrize("mode", [0o644, 0o664, 0o666, 0o600])
    def test_a_writable_source_is_refused(self, tmp_path, mode):
        path = _write_policy(tmp_path / "policy.json", "published")
        path.chmod(mode)
        with pytest.raises(PlatformCompositionError, match="writable by the account"):
            pd.fetch_once(governance.PolicyDistribution(source=path.as_uri()))

    def test_a_read_only_source_is_accepted(self, readonly_chain, tmp_path):
        """The other half, so the refusal above cannot pass for the wrong reason."""
        source = _file_source(tmp_path / "policy.json", "published")
        assert pd.fetch_once(governance.PolicyDistribution(source=source)).body == _body(
            "published"
        )

    def test_a_read_only_file_in_a_writable_directory_is_refused(self, tmp_path):
        """The ancestor half is what makes the check mean anything.

        A ``0444`` file inside a directory this account can write is replaceable by
        unlink-and-recreate, so an agent could publish a forged read-only document and a
        leaf-only check would accept it. NOT stubbed here — this is the control's own test.
        """
        path = _write_policy(tmp_path / "policy.json", "forged")
        path.chmod(0o444)
        with pytest.raises(PlatformCompositionError, match="writable by the account"):
            pd.fetch_once(governance.PolicyDistribution(source=path.as_uri()))

    def test_the_walk_answers_false_for_a_genuinely_root_owned_chain(self):
        """The other direction, against a real path, so the refusal is not vacuous."""
        from kiro_crew import platform_compat

        # /etc is root-owned on every supported POSIX host; skip where it is not present.
        if not os.path.isfile("/etc/hostname"):
            pytest.skip("no /etc/hostname on this host")
        assert platform_compat.path_writable_by_current_user("/etc/hostname") is False

    def test_the_walk_finds_a_writable_ancestor_above_a_read_only_leaf(self, tmp_path):
        from kiro_crew import platform_compat

        nested = tmp_path / "a" / "b"
        nested.mkdir(parents=True)
        leaf = nested / "policy.json"
        leaf.write_text("{}", encoding="utf-8")
        leaf.chmod(0o444)
        nested.chmod(0o555)
        try:
            assert platform_compat.path_writable_by_current_user(leaf) is True
        finally:
            nested.chmod(0o755)

    def test_the_message_names_the_channel_designed_for_a_local_file(self, tmp_path):
        """An operator refused here needs to be told where to go instead."""
        path = _write_policy(tmp_path / "policy.json", "published")
        path.chmod(0o644)
        with pytest.raises(PlatformCompositionError, match="KIROCREW_SECURITY_POLICY"):
            pd.fetch_once(governance.PolicyDistribution(source=path.as_uri()))


class TestTheFileValidatorIsAContentDigest:
    def test_a_same_size_swap_with_a_preserved_mtime_is_detected(self, readonly_chain, tmp_path):
        """An ``mtime:size`` validator misses this, and misses it FOREVER.

        ``cp -p``, a restored backup or a deliberate ``touch`` all reproduce it, and every
        poll would then report "unchanged" while the previous — potentially looser —
        ceiling stayed enforced.
        """
        path = tmp_path / "policy.json"
        source = _file_source(path, "fleet")
        first = pd._fetch_file(pd.FetchRequest(url=source))
        before = os.stat(path)

        swapped = first.body.replace(b'"fleet"', b'"EVIL!"')
        assert len(swapped) == len(first.body), "the swap must be size-preserving to be a test"
        path.chmod(0o644)
        path.write_bytes(swapped)
        path.chmod(0o444)
        os.utime(path, ns=(before.st_atime_ns, before.st_mtime_ns))
        assert os.stat(path).st_mtime_ns == before.st_mtime_ns
        assert os.stat(path).st_size == before.st_size

        again = pd._fetch_file(pd.FetchRequest(url=source, etag=first.etag))

        assert again.not_modified is False
        assert json.loads(again.body)["identity"]["issuer"] == "EVIL!"

    def test_identical_bytes_still_cost_no_body(self, readonly_chain, tmp_path):
        source = _file_source(tmp_path / "policy.json", "published")
        first = pd._fetch_file(pd.FetchRequest(url=source))
        again = pd._fetch_file(pd.FetchRequest(url=source, etag=first.etag))
        assert again.not_modified is True and again.body == b""


class TestLoopbackBypassesTheProxy:
    """urllib has NO implicit loopback exemption.

    With ``HTTP_PROXY`` set, a request to 127.0.0.1 is sent to the proxy in absolute
    form *with the request headers* — handing the fleet's credential to the proxy and
    letting it answer with a substituted ceiling. Asserted on the pure handler-chain
    function, so no socket is opened and no DNS lookup happens.
    """

    @pytest.mark.parametrize("url", ["http://127.0.0.1:8080/p.json", "http://localhost/p.json"])
    def test_a_loopback_url_gets_a_proxy_bypass(self, url):
        handlers = pd._opener_handlers(url)
        assert any(isinstance(h, urllib.request.ProxyHandler) for h in handlers)

    @pytest.mark.parametrize(
        "url", ["https://config.corp.example/p.json", "http://config.corp.example/p.json"]
    )
    def test_a_remote_url_keeps_the_proxy(self, url):
        """There a corporate proxy is the intended path, so it must NOT be bypassed."""
        handlers = pd._opener_handlers(url)
        assert not any(isinstance(h, urllib.request.ProxyHandler) for h in handlers)

    def test_every_chain_refuses_redirects(self):
        """The redirect guard is not conditional — a 3xx must never change origin."""
        for url in ("https://config.corp.example/p.json", "http://127.0.0.1/p.json"):
            assert any(isinstance(h, pd._NoRedirects) for h in pd._opener_handlers(url))


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


class TestCacheOnlyMode:
    """An app backend inherits the ceiling without being handed the control plane."""

    def test_the_cache_is_adopted_with_no_source_and_no_fetch(self, transport, monkeypatch):
        seen: list = []
        source = transport(_static_fetcher(pd.FetchedPolicy(body=_body("fetched")), seen))
        _cache("from-the-gateway", source)
        monkeypatch.setenv(pd.POLICY_CACHE_ONLY_ENV, "1")
        # No source configured at all: this child was deliberately not given one.
        ceiling = pd.load_distributed_policy(None)
        assert ceiling is not None and ceiling.identity_issuer == "from-the-gateway"
        assert seen == [], "cache-only mode must never reach the transport"

    def test_a_cache_from_another_source_is_still_adopted(self, monkeypatch):
        """The parent applied the repoint rule when it wrote the cache.

        The child is inheriting a decision, not making one, so re-applying the check
        against a source it was not given would only ever refuse a valid ceiling.
        """
        _cache("from-the-gateway", "https://whatever.the.parent.decided/p.json")
        monkeypatch.setenv(pd.POLICY_CACHE_ONLY_ENV, "1")
        ceiling = pd.load_distributed_policy(None)
        assert ceiling is not None and ceiling.identity_issuer == "from-the-gateway"

    def test_no_cache_fails_closed_rather_than_dropping_to_a_local_tier(self, monkeypatch):
        """The flag means "there is a fleet ceiling to inherit".

        The gateway only sets it when its OWN ceiling came from this tier, so an absent
        cache here is not "this host has no central policy" — it is "the parent had one
        and could not pass it on", which a successful fetch with a failed cache WRITE
        produces. Falling through would start arbitrary third-party code under a local or
        absent ceiling on a host the administrator governs, silently.
        """
        monkeypatch.setenv(pd.POLICY_CACHE_ONLY_ENV, "1")
        with pytest.raises(PlatformCompositionError, match="no cached ceiling"):
            pd.load_distributed_policy(None)

    def test_a_cache_past_the_staleness_bound_fails_closed(self, monkeypatch):
        _cache("stale", _TEST_SOURCE, age_secs=_PAST_THE_WINDOW)
        monkeypatch.setenv(pd.POLICY_CACHE_ONLY_ENV, "1")
        with pytest.raises(PlatformCompositionError, match="staleness bound"):
            pd.load_distributed_policy(
                governance.PolicyDistribution(source=_TEST_SOURCE, max_cache_age_secs=60)
            )

    def test_an_unusable_cache_fails_closed(self, monkeypatch):
        pd.write_cache(b'{"version": 99, "boot": {}}', source=_TEST_SOURCE)
        monkeypatch.setenv(pd.POLICY_CACHE_ONLY_ENV, "1")
        with pytest.raises(PlatformCompositionError, match="unusable in cache-only mode"):
            pd.load_distributed_policy(None)

    def test_the_flag_is_only_set_when_there_is_a_ceiling_to_inherit(self):
        """A gateway that itself degraded has nothing to pass on.

        Flagging that child would refuse to start an app on a host running perfectly
        well, so the predicate the gateway gates on must be false there.
        """
        assert pd.central_ceiling_installed() is False
        pd._record_installed(_body("central"))
        assert pd.central_ceiling_installed() is True


class TestTheCachePairCannotTear:
    def test_a_document_that_does_not_match_its_metadata_reads_as_absent(self):
        """Two files, two writes: overlapping writers can pair NEW bytes with OLD metadata.

        That pair is not merely stale — the metadata's ``source`` is what the repoint rule
        trusts, so a tear could hand a repointed host the retired endpoint's document
        under the new endpoint's name.
        """
        pd.write_cache(_body("published"), source=_TEST_SOURCE, etag="v1")
        assert pd.read_cache() is not None
        # Replace the document alone, exactly as an interleaved writer would.
        (pd.cache_dir() / "policy.json").write_bytes(_body("substituted"))
        assert pd.read_cache() is None

    def test_metadata_without_a_digest_is_still_accepted(self):
        """A cache written by an older build has no digest; it must not read as torn."""
        pd.write_cache(_body("published"), source=_TEST_SOURCE)
        meta_path = pd.cache_dir() / "policy.meta.json"
        meta = json.loads(meta_path.read_text())
        del meta["digest"]
        meta_path.write_text(json.dumps(meta))
        cached = pd.read_cache()
        assert cached is not None and cached.body == _body("published")


class TestNonFiniteNumbersAreRejected:
    """NaN slips past every range check — each comparison against it is False.

    It would then reach ``int()`` and raise an uncaught ValueError at boot instead of a
    validation error here.
    """

    @pytest.mark.parametrize("literal", ["NaN", "Infinity", "-Infinity"])
    def test_the_policy_block_refuses_them(self, literal):
        doc = json.loads('{"source": "https://x.invalid/p", "timeout_secs": ' + literal + "}")
        with pytest.raises(PlatformCompositionError, match="finite"):
            governance.PolicyDistribution.from_dict(doc)

    @pytest.mark.parametrize("raw", ["nan", "inf", "-inf"])
    def test_the_env_channel_refuses_them(self, monkeypatch, raw):
        monkeypatch.setenv(pd.POLICY_TIMEOUT_ENV, raw)
        with pytest.raises(PlatformCompositionError, match="finite"):
            pd.resolve_distribution(_DECLARED)


class TestTheDeniedEnvListCoversEveryVariable:
    """One owner for the set, and two consumers that match it differently.

    ``sandbox`` scrubs by ``startswith``, but ``cron_script._CRON_ENV_DENY`` tests exact
    membership and ``mcp_cron`` builds ``\\b``-anchored regexes — so a PREFIX entry
    silently matches nothing in either of those, which is how a bearer token would reach
    an agent-authored cron script.
    """

    def test_every_distribution_variable_is_denied_to_agents_by_exact_name(self):
        from kiro_crew import sandbox

        denied = set(sandbox._AGENT_DENIED_ENV_KEYS)
        missing = [v for v in pd.POLICY_DISTRIBUTION_ENV_VARS if v not in denied]
        assert not missing, (
            "these policy variables are not denied to agent subprocesses by exact "
            f"name: {missing}"
        )

    def test_the_cron_scrub_actually_drops_them(self, monkeypatch):
        """The consumer with the exact-match rule, exercised rather than assumed."""
        from kiro_crew import cron_script

        for var in pd.POLICY_DISTRIBUTION_ENV_VARS:
            monkeypatch.setenv(var, "secret-value")
        scrubbed = cron_script._clean_cron_env()
        leaked = [v for v in pd.POLICY_DISTRIBUTION_ENV_VARS if v in scrubbed]
        assert not leaked, f"a cron script would still read: {leaked}"


class TestCacheOnlyChildrenStillReachTheTier:
    def test_the_loader_enters_the_tier_with_no_source_configured(self, monkeypatch, tmp_path):
        """A cache-only child is given NO source by design.

        Gating the tier's entry on a source would skip it and drop that child to a local
        or absent ceiling — exactly the looser-ceiling failure cache-only mode prevents.
        """
        home = tmp_path / "security_policy.json"
        _write_policy(home, "local-tier-4")
        monkeypatch.setattr(governance, "_policy_home_path", lambda: home)
        _cache("from-the-gateway", "https://config.corp.example/p.json")
        monkeypatch.setenv(pd.POLICY_CACHE_ONLY_ENV, "1")

        ceiling = governance.load_security_policy()

        assert ceiling is not None
        assert ceiling.identity_issuer == "from-the-gateway", (
            "the cache-only child fell through to the local tier instead of adopting "
            "the ceiling the gateway cached"
        )

    def test_the_duplicated_cache_only_constant_matches(self):
        assert governance._POLICY_DISTRIBUTION_CACHE_ONLY_ENV == pd.POLICY_CACHE_ONLY_ENV


class TestTheHostnameAloneIsRedacted:
    """Plenty of transport errors never quote the whole URL."""

    @pytest.mark.parametrize(
        "message",
        [
            "hostname 'config.corp.example' doesn't match either of 'a', 'b'",
            "[Errno -2] Name or service not known: config.corp.example",
            "connection to config.corp.example timed out",
        ],
        ids=["tls-mismatch", "dns", "timeout"],
    )
    def test_a_host_only_message_is_redacted(self, message):
        source = "https://config.corp.example/kirocrew/security_policy.json"
        redacted = pd._redact_source(message, source)
        assert "config.corp.example" not in redacted
        assert "policy source" in redacted

    def test_a_full_url_still_gets_the_more_informative_substitution(self):
        source = "https://config.corp.example/p.json"
        redacted = pd._redact_source(f"could not fetch {source}: down", source)
        assert source not in redacted
        assert "<the https policy source>" in redacted


class TestEndpointAuthoredTextIsSanitised:
    """The bytes in a parser error are not ours.

    ``json``'s errors quote the offending text, so a document that echoes back the
    request's ``Authorization`` header — an endpoint reflecting its own request, a proxy
    error page — would carry that credential into ``kirocrew policy fetch`` and into
    ``GET /api/logs``.
    """

    def test_a_credential_echoed_by_the_endpoint_does_not_reach_the_detail(
        self, transport, install_ceiling
    ):
        leaky = b'{"Authorization": "Bearer AKIAIOSFODNN7EXAMPLE" oops'
        source = transport(_static_fetcher(pd.FetchedPolicy(body=leaky)))
        install_ceiling(governance.parse_policy(_doc("running", distribution={"source": source})))
        outcome = pd.refresh_now()
        assert outcome.status == pd.REFRESH_REJECTED
        assert "AKIAIOSFODNN7EXAMPLE" not in outcome.detail

    def test_the_sanitiser_runs_both_passes(self):
        """The source elision AND the shared credential chain, not one or the other."""
        source = "https://config.corp.example/p.json"
        text = f"fetching {source} returned AKIAIOSFODNN7EXAMPLE"
        clean = pd._sanitize_detail(text, source)
        assert source not in clean
        assert "config.corp.example" not in clean
        assert "AKIAIOSFODNN7EXAMPLE" not in clean


class TestTheCacheIsPublishedBeforeTheCeilingIsInstalled:
    def test_a_concurrent_child_cannot_read_the_retired_ceiling(self, transport, install_ceiling):
        """A cache-only child adopts whatever the cache holds.

        Installing first leaves a window where the gateway enforces the new ceiling and a
        fresh app backend adopts the retired one.
        """
        pushed = _body("pushed")
        source = transport(_static_fetcher(pd.FetchedPolicy(body=pushed, etag="v2")))
        install_ceiling(governance.parse_policy(_doc("running", distribution={"source": source})))
        seen_at_install: dict = {}

        import kiro_crew.platform.context as ctx_mod

        original = ctx_mod.set_context

        def spy(ctx):
            # What the cache holds AT THE MOMENT the new ceiling goes live.
            cached = pd.read_cache()
            seen_at_install["body"] = cached.body if cached else None
            return original(ctx)

        try:
            ctx_mod.set_context = spy
            assert pd.refresh_now().status == pd.REFRESH_APPLIED
        finally:
            ctx_mod.set_context = original

        assert (
            seen_at_install["body"] == pushed
        ), "the cache still held the retired ceiling when the new one was installed"

    def test_a_document_that_does_not_compose_is_never_published(
        self, transport, install_ceiling, profiles_dir
    ):
        """Publishing before validating would hand a child a ceiling the host refused."""
        _cache("last-good", _TEST_SOURCE)
        (profiles_dir / "host.json").write_text(
            json.dumps(
                {
                    "name": "host",
                    "bind": {"type": "surface", "id": "host"},
                    "sandbox": {"min_level": "off"},
                }
            ),
            encoding="utf-8",
        )
        gp.reset_store()
        pushed = _body("pushed", sandbox={"min_level": "strict"})
        source = transport(_static_fetcher(pd.FetchedPolicy(body=pushed)))
        running = install_ceiling(
            governance.parse_policy(_doc("running", distribution={"source": _TEST_SOURCE}))
        )
        del source

        assert pd.refresh_now().status == pd.REFRESH_REJECTED
        assert current_context().governance is running
        cached = pd.read_cache()
        assert (
            cached is not None and cached.body != pushed
        ), "a refused document reached the cache, where a cache-only child would adopt it"


class TestTheStalenessBoundReachesACacheOnlyChild:
    def test_a_policy_declared_bound_is_forwarded_not_just_the_env_one(
        self, install_ceiling, monkeypatch
    ):
        """The bound is as likely to be declared in the published document.

        Reading only the env var would leave the child with no bound at all on such a
        fleet, so it would accept an arbitrarily stale ceiling.
        """
        monkeypatch.delenv(pd.POLICY_MAX_AGE_ENV, raising=False)
        install_ceiling(
            governance.parse_policy(
                _doc(
                    "running",
                    distribution={"source": _TEST_SOURCE, "max_cache_age_secs": 3600},
                )
            )
        )
        assert pd.effective_max_cache_age() == 3600

    def test_the_env_still_wins_when_it_is_set(self, install_ceiling, monkeypatch):
        monkeypatch.setenv(pd.POLICY_MAX_AGE_ENV, "120")
        install_ceiling(
            governance.parse_policy(
                _doc(
                    "running",
                    distribution={"source": _TEST_SOURCE, "max_cache_age_secs": 3600},
                )
            )
        )
        assert pd.effective_max_cache_age() == 120


class TestThePublishIsConfirmedBeforeInstalling:
    def test_a_failed_cache_write_keeps_the_running_ceiling(
        self, transport, install_ceiling, monkeypatch
    ):
        """``write_cache`` is best-effort, but a cache-only child inherits FROM it.

        A swallowed write failure would leave the gateway enforcing the new, possibly
        tighter ceiling while every app backend spawned afterwards adopted the older,
        looser one.
        """
        source = transport(_static_fetcher(pd.FetchedPolicy(body=_body("pushed"), etag="v2")))
        running = install_ceiling(
            governance.parse_policy(_doc("running", distribution={"source": source}))
        )
        monkeypatch.setattr(pd, "write_cache", lambda *a, **k: None)

        outcome = pd.refresh_now()

        assert outcome.status == pd.REFRESH_REJECTED
        assert "policy cache" in outcome.detail
        assert current_context().governance is running

    def test_the_confirmation_checks_the_recorded_source_not_only_the_bytes(
        self, transport, install_ceiling, monkeypatch
    ):
        """A repoint that publishes IDENTICAL bytes cannot be confirmed by body alone.

        It is the recorded source the next boot's repoint rule judges, so a metadata write
        that failed while the document write succeeded leaves the old source's cache
        passing a body-only check — a refresh reporting success that a restart then
        discards, failing closed on a host that had just been told it was fine.
        """
        body = _body("pushed")
        new_source = transport(_static_fetcher(pd.FetchedPolicy(body=body, etag="v2")))
        running = install_ceiling(
            governance.parse_policy(_doc("running", distribution={"source": new_source}))
        )
        # The same bytes, still recorded against the endpoint this host has moved OFF.
        pd.write_cache(body, source="https://retired.corp.example/p.json", etag="v1")
        # The document write lands; the metadata write does not, so the recorded source
        # stays the retired one.
        monkeypatch.setattr(pd, "write_cache", lambda *a, **k: None)

        outcome = pd.refresh_now()

        assert outcome.status == pd.REFRESH_REJECTED
        assert "policy cache" in outcome.detail
        assert current_context().governance is running


class TestOwningTheSourceIsEnoughToBeWritable:
    """An owner may ``chmod`` its own file, so a ``0444`` file this account owns is one it
    can make writable and then rewrite — which is the exact move the threat model
    describes, because the agent subprocess runs as the same uid. Requiring ``S_IWUSR``
    accepted precisely the source an agent could take over with one ``chmod``.
    """

    @_POSIX_IDS_ONLY
    def test_a_read_only_file_we_own_is_writable(self, tmp_path):
        target = tmp_path / "policy.json"
        target.write_text("{}", encoding="utf-8")
        target.chmod(0o444)
        assert platform_compat.stat_writable_by_current_user(os.stat(target)) is True

    @_POSIX_IDS_ONLY
    def test_a_read_only_file_owned_by_someone_else_is_not(self, monkeypatch):
        """The control has to stay a control: the field manual's root-owned path passes."""
        monkeypatch.setattr(platform_compat.os, "getuid", lambda: 1000)
        monkeypatch.setattr(platform_compat.os, "geteuid", lambda: 1000)
        monkeypatch.setattr(platform_compat.os, "getgid", lambda: 1000)
        monkeypatch.setattr(platform_compat.os, "getegid", lambda: 1000)
        monkeypatch.setattr(platform_compat.os, "getgroups", lambda: [1000])
        st = os.stat_result((stat.S_IFREG | 0o444, 0, 0, 1, 0, 0, 0, 0, 0, 0))
        assert platform_compat.stat_writable_by_current_user(st) is False

    @_POSIX_IDS_ONLY
    def test_running_as_root_makes_everything_writable(self, monkeypatch):
        """Root writes anything the mode says, so no source can be shown safe from it."""
        monkeypatch.setattr(platform_compat.os, "getuid", lambda: 0)
        monkeypatch.setattr(platform_compat.os, "geteuid", lambda: 0)
        st = os.stat_result((stat.S_IFREG | 0o444, 0, 0, 1, 12345, 12345, 0, 0, 0, 0))
        assert platform_compat.stat_writable_by_current_user(st) is True

    @_POSIX_IDS_ONLY
    def test_a_directory_we_own_makes_a_leaf_inside_it_replaceable(self, tmp_path):
        """Owning a directory means being able to chmod it, hence to unlink and recreate
        what is inside — so the ancestor walk must answer True for it."""
        nested = tmp_path / "a"
        nested.mkdir()
        nested.chmod(0o555)
        leaf = nested / "policy.json"
        try:
            leaf.write_text("{}", encoding="utf-8")
        except PermissionError:
            nested.chmod(0o755)
            leaf.write_text("{}", encoding="utf-8")
            nested.chmod(0o555)
        leaf.chmod(0o444)
        try:
            assert platform_compat.path_writable_by_current_user(leaf) is True
        finally:
            nested.chmod(0o755)

    @_POSIX_IDS_ONLY
    def test_an_acl_grant_the_mode_bits_hide_is_still_writable(self, tmp_path, monkeypatch):
        """A POSIX **ACL** entry for a named user does not appear in ``st_mode`` at all — the
        group bits show the ACL *mask*, not that entry — so a mode-only check reports "not
        writable" for a source this account can in fact rewrite. ``os.access`` with
        ``effective_ids`` goes through ``faccessat(AT_EACCESS)``, which the kernel evaluates
        against the full ACL.

        The ACL itself is the kernel's to honour, not this code's to reproduce, and creating
        the distinguishing case needs a file owned by a second uid. What is pinned here is
        that the kernel IS asked: a component the mode bits call read-only but ``os.access``
        calls writable comes back writable.
        """
        target = tmp_path / "a" / "policy.json"
        target.parent.mkdir()
        target.write_text("{}", encoding="utf-8")

        # Owned by someone else and mode-clean, so every mode-bit arm answers "no".
        real_stat = os.stat

        def foreign(path, *a, **kw):
            st = real_stat(path, *a, **kw)
            fields = list(st)
            fields[4] = os.getuid() + 1  # st_uid
            fields[5] = 999999  # st_gid, a group we are not in
            # Decided from the REAL mode: os.path.isfile would re-enter this stub.
            is_dir = stat.S_ISDIR(st.st_mode)
            fields[0] = (stat.S_IFDIR | 0o555) if is_dir else (stat.S_IFREG | 0o444)
            return os.stat_result(tuple(fields))

        monkeypatch.setattr(platform_compat.os, "stat", foreign)
        monkeypatch.setattr(platform_compat.os, "getgroups", lambda: [])
        # And the kernel reports no ACL grant either, so the baseline really is "no".
        monkeypatch.setattr(platform_compat.os, "access", lambda p, mode, **kw: False)
        assert platform_compat.path_writable_by_current_user(target) is False

        # Now the kernel reports an ACL grant on the leaf. Nothing about the mode changed.
        monkeypatch.setattr(
            platform_compat.os,
            "access",
            lambda p, mode, **kw: str(p) == str(target.resolve()),
        )
        assert platform_compat.path_writable_by_current_user(target) is True

    @_POSIX_IDS_ONLY
    def test_an_acl_grant_on_an_ANCESTOR_is_also_caught(self, tmp_path, monkeypatch):
        """Write on a directory means unlink-and-recreate, so the walk must ask about every
        component, not just the leaf."""
        target = tmp_path / "a" / "policy.json"
        target.parent.mkdir()
        target.write_text("{}", encoding="utf-8")
        parent = str(target.parent.resolve())

        real_stat = os.stat

        def foreign(path, *a, **kw):
            st = real_stat(path, *a, **kw)
            fields = list(st)
            fields[4] = os.getuid() + 1
            fields[5] = 999999
            # Decided from the REAL mode: os.path.isfile would re-enter this stub.
            is_dir = stat.S_ISDIR(st.st_mode)
            fields[0] = (stat.S_IFDIR | 0o555) if is_dir else (stat.S_IFREG | 0o444)
            return os.stat_result(tuple(fields))

        monkeypatch.setattr(platform_compat.os, "stat", foreign)
        monkeypatch.setattr(platform_compat.os, "getgroups", lambda: [])
        monkeypatch.setattr(platform_compat.os, "access", lambda p, mode, **kw: str(p) == parent)
        assert platform_compat.path_writable_by_current_user(target) is True

    @_POSIX_IDS_ONLY
    @pytest.mark.skipif(
        not hasattr(os, "symlink") or os.name == "nt",
        reason="planting the link needs SeCreateSymbolicLinkPrivilege on Windows",
    )
    def test_a_writable_link_to_a_read_only_target_is_refused(self, tmp_path, monkeypatch):
        """Resolving first and walking only the target was the gap. Re-pointing a symlink needs
        no permission on the link and none on the target — it needs write on the LINK's parent,
        which the resolved chain never visits. So a root-owned, read-only document reached
        through a link in a directory this account can write is a source this account controls.
        """
        target_dir = tmp_path / "readonly"
        target_dir.mkdir()
        target = _write_policy(target_dir / "policy.json", "published")
        link_dir = tmp_path / "writable"
        link_dir.mkdir()
        link = link_dir / "policy.json"
        link.symlink_to(target)

        # EXACTLY ONE component reads as ours: the link's parent. Everything else — the
        # target, its directory, and every shared ancestor including the world-writable
        # /tmp — reads as foreign and read-only. That is what makes the test discriminate:
        # the two chains share everything above `tmp_path`, so leaving those real would let
        # a resolved-only walk pass on /tmp's own mode and prove nothing.
        real_stat = os.stat
        ours = str(link_dir)

        def foreign_except_the_links_parent(path, *a, **kw):
            st = real_stat(path, *a, **kw)
            if str(path) == ours:
                return st
            fields = list(st)
            fields[4] = os.getuid() + 1
            fields[5] = 999999
            fields[0] = (
                (stat.S_IFDIR | 0o555) if stat.S_ISDIR(st.st_mode) else (stat.S_IFREG | 0o444)
            )
            return os.stat_result(tuple(fields))

        monkeypatch.setattr(platform_compat.os, "stat", foreign_except_the_links_parent)
        monkeypatch.setattr(platform_compat.os, "getgroups", lambda: [])
        # os.access would answer from the real filesystem, where everything here is ours.
        monkeypatch.setattr(platform_compat.os, "access", lambda p, mode, **kw: False)

        assert (
            platform_compat.path_writable_by_current_user(link) is True
        ), "the link's own parent is writable, so the source is replaceable"
        # The control: the target itself, reached directly, is genuinely not replaceable.
        assert platform_compat.path_writable_by_current_user(target) is False

    @_POSIX_IDS_ONLY
    def test_a_file_source_we_own_is_therefore_refused(self, tmp_path):
        """End to end: the transport declines it, whatever its mode bits say."""
        path = _write_policy(tmp_path / "policy.json", "published")
        path.chmod(0o444)
        with pytest.raises(PlatformCompositionError, match="writable by the account"):
            pd.fetch_once(governance.PolicyDistribution(source=path.as_uri()))


class TestAPublishCannotRollTheCeilingBackward:
    """A fetch is slow, and a refresh is not the only writer. A refresher that fetched v2
    over a slow link while ``policy fetch --force`` published v3 would write v2 over v3 and
    then INSTALL v2 — the ceiling moving backward to a looser document, which is the one
    direction this tier must never move on its own.
    """

    def test_a_lost_swap_neither_overwrites_the_cache_nor_installs(
        self, transport, install_ceiling, monkeypatch
    ):
        source = transport(_static_fetcher(pd.FetchedPolicy(body=_body("v2"), etag="v2")))
        running = install_ceiling(
            governance.parse_policy(_doc("running", distribution={"source": source}))
        )
        pd.write_cache(_body("v1"), source=source, etag="v1")

        # A concurrent writer publishes v3 while this refresh is fetching. Hooked on the
        # fetch itself so the ordering is the real one: observed-before, published-during.
        real_fetch = pd.fetch_once

        def fetch_then_race(dist, cached=None):
            result = real_fetch(dist, cached)
            pd.write_cache(_body("v3"), source=source, etag="v3")
            return result

        monkeypatch.setattr(pd, "fetch_once", fetch_then_race)
        outcome = pd.refresh_now()

        cached = pd.read_cache()
        assert cached is not None
        assert cached.body == _body("v3"), "the newer document must survive the race"
        assert current_context().governance is running, "v2 must not have been installed"
        assert outcome.status == pd.REFRESH_UNCHANGED

    def test_the_snapshot_is_taken_before_the_fetch_not_after(
        self, transport, install_ceiling, monkeypatch
    ):
        """Reading it afterwards would already include the concurrent write, so the swap
        would pass and overwrite the newer document — the ordering IS the control."""
        seen: list[str] = []
        source = transport(_static_fetcher(pd.FetchedPolicy(body=_body("v2"), etag="v2")))
        install_ceiling(governance.parse_policy(_doc("running", distribution={"source": source})))
        pd.write_cache(_body("v1"), source=source, etag="v1")

        real_write = pd.write_cache

        def record(body, **kw):
            if "expect_pair" in kw:
                seen.append(kw["expect_pair"])
            return real_write(body, **kw)

        real_fetch = pd.fetch_once

        def fetch_then_race(dist, cached=None):
            result = real_fetch(dist, cached)
            real_write(_body("v3"), source=source, etag="v3")
            return result

        monkeypatch.setattr(pd, "fetch_once", fetch_then_race)
        monkeypatch.setattr(pd, "write_cache", record)
        pd.refresh_now()

        assert seen, "the publish must carry an expectation at all"
        assert seen[0] == (
            pd._body_digest(_body("v1")),
            pd._source_digest(source),
        ), "the expectation must be the PRE-fetch pair, not the racer's"

    def test_an_uncontended_refresh_still_publishes_and_installs(self, transport, install_ceiling):
        """The control: with no racer the swap passes and the refresh applies as before."""
        source = transport(_static_fetcher(pd.FetchedPolicy(body=_body("v2"), etag="v2")))
        install_ceiling(governance.parse_policy(_doc("running", distribution={"source": source})))
        pd.write_cache(_body("v1"), source=source, etag="v1")

        outcome = pd.refresh_now()

        assert outcome.status == pd.REFRESH_APPLIED
        cached = pd.read_cache()
        assert cached is not None and cached.body == _body("v2")

    def test_a_first_publish_expects_no_cache(self, transport, install_ceiling, monkeypatch):
        """With nothing cached the expectation is the empty digest, so a writer that
        appears during the fetch still wins rather than being overwritten."""
        seen: list[str] = []
        source = transport(_static_fetcher(pd.FetchedPolicy(body=_body("v2"), etag="v2")))
        install_ceiling(governance.parse_policy(_doc("running", distribution={"source": source})))
        assert pd.read_cache() is None

        real_write = pd.write_cache

        def record(body, **kw):
            seen.append(kw.get("expect_pair", "<absent>"))
            return real_write(body, **kw)

        monkeypatch.setattr(pd, "write_cache", record)
        pd.refresh_now()
        assert seen and seen[0] == ("", ""), "an absent cache must be expected as absent"

    def test_a_failed_write_is_still_reported_as_a_cache_problem_not_a_race(
        self, transport, install_ceiling, monkeypatch
    ):
        """The two reasons a publish did not happen want different answers, and re-reading
        is how they are told apart."""
        source = transport(_static_fetcher(pd.FetchedPolicy(body=_body("v2"), etag="v2")))
        running = install_ceiling(
            governance.parse_policy(_doc("running", distribution={"source": source}))
        )
        monkeypatch.setattr(pd, "write_cache", lambda *a, **k: False)

        outcome = pd.refresh_now()

        assert outcome.status == pd.REFRESH_REJECTED
        assert "policy cache" in outcome.detail
        assert current_context().governance is running


class TestARollbackCannotDestroyANewerCeiling:
    """The publish and the failure are separated by ``apply_ceiling``, so another process
    can publish in between — and rolling back over it would destroy a valid, newer ceiling
    nobody asked us to touch. The restore is a compare-and-swap on the document THIS
    refresh published, mirroring the publish it undoes.
    """

    def test_a_newer_document_published_after_our_publish_is_left_alone(
        self, transport, install_ceiling, monkeypatch
    ):
        source = transport(_static_fetcher(pd.FetchedPolicy(body=_body("v2"), etag="v2")))
        running = install_ceiling(
            governance.parse_policy(_doc("running", distribution={"source": source}))
        )
        pd.write_cache(_body("v1"), source=source, etag="v1")

        # Publish v3 from "another process" at the moment the install refuses — that is
        # exactly the window between this refresh's publish and its rollback.
        def refuse_and_race(_ceiling):
            pd.write_cache(_body("v3"), source=source, etag="v3")
            raise RuntimeError("profile floor")

        monkeypatch.setattr(pd, "apply_ceiling", refuse_and_race)
        outcome = pd.refresh_now()

        assert outcome.status == pd.REFRESH_REJECTED
        assert current_context().governance is running
        cached = pd.read_cache()
        assert cached is not None
        assert cached.body == _body("v3"), "a rollback must not destroy a newer ceiling"

    def test_an_uncontended_rollback_still_restores(self, transport, install_ceiling, monkeypatch):
        """The control: with no racer the swap passes and the prior copy comes back."""
        source = transport(_static_fetcher(pd.FetchedPolicy(body=_body("v2"), etag="v2")))
        install_ceiling(governance.parse_policy(_doc("running", distribution={"source": source})))
        pd.write_cache(_body("v1"), source=source, etag="v1")
        monkeypatch.setattr(
            pd, "apply_ceiling", lambda c: (_ for _ in ()).throw(RuntimeError("profile floor"))
        )

        pd.refresh_now()

        cached = pd.read_cache()
        assert cached is not None and cached.body == _body("v1")


class TestTheSignatureAuditLabelIsRedacted:
    """The signature verifier records its label in the audit trail, and it does so inside
    ``governance`` — so a caller sanitising this module's *raised* text cannot reach it.
    For the file tiers that label is the operator's own path and belongs in the record;
    for this tier it is a URL that may be the credential, so it is redacted before it
    ever crosses over.
    """

    def test_an_unverified_signature_does_not_log_the_url(self, caplog):
        secret = "https://signed.corp.example/p.json?X-Amz-Signature=deadbeefcafe"
        body = _body("central", identity={"issuer": "corp", "signature": "not-a-signature"})

        with caplog.at_level(logging.DEBUG):
            try:
                pd.parse_distributed_policy(body, source=secret)
            except PlatformCompositionError:
                pass

        emitted = "\n".join(r.getMessage() + (pd_format_exc(r) or "") for r in caplog.records)
        assert secret not in emitted
        assert "X-Amz-Signature" not in emitted
        assert "signed.corp.example" not in emitted

    def test_the_agent_reachable_log_line_carries_neither_label_nor_reason(self, caplog):
        """The log ring is served by ``GET /api/logs``, which the agent's own browser
        tooling can drive; the SEL record is on the keystone and is not reachable that
        way. So the reason and the policy label belong in the record, and the warning
        carries only the state plus a pointer to it."""
        from kiro_crew.platform import governance as gov

        with caplog.at_level(logging.WARNING, logger="kiro_crew.platform.governance"):
            gov._audit_policy_signature(
                gov.SIGNATURE_UNVERIFIED,
                "no trust key for issuer 'corp'",
                "https://signed.corp.example/p.json?X-Amz-Signature=deadbeefcafe",
            )

        warnings = [r.getMessage() for r in caplog.records if r.levelno >= logging.WARNING]
        assert warnings, "an unverified signature must still be visible to an operator"
        joined = "\n".join(warnings)
        assert "UNVERIFIED" in joined
        assert "signed.corp.example" not in joined
        assert "X-Amz-Signature" not in joined
        assert "corp" not in joined, "the issuer the DOCUMENT claimed is not ours to echo"

    def test_the_raised_text_carries_no_url_either(self):
        secret = "https://signed.corp.example/p.json?X-Amz-Signature=deadbeefcafe"
        with pytest.raises(PlatformCompositionError) as caught:
            pd.parse_distributed_policy(b"{ not json", source=secret)
        assert secret not in str(caught.value)
        assert "signed.corp.example" not in str(caught.value)
        assert "https" in str(caught.value), "the scheme still says which tier failed"


class TestTheFetchCommandDoesNotOverclaim:
    """``refresh_now`` installs the ceiling in the CALLING process, and
    ``kirocrew policy fetch`` exits immediately — so a bare "applied" would claim
    something the command cannot do. A running gateway is a different process and keeps
    its own ceiling until its refresher polls; with a boot-only source there is no poll.
    """

    @staticmethod
    def _run(capsys, *, interval, monkeypatch):
        from kiro_crew import cli_commands
        from kiro_crew.platform import policy_distribution as dist_mod

        monkeypatch.setattr(
            dist_mod,
            "refresh_now",
            lambda *, force=False: pd.RefreshOutcome(pd.REFRESH_APPLIED, signature_state=""),
        )
        monkeypatch.setattr(dist_mod, "effective_refresh_interval", lambda: interval)
        cli_commands._policy_fetch(force=False)
        return capsys.readouterr().out

    def test_it_reports_the_cache_write_not_an_install(self, capsys, monkeypatch):
        out = self._run(capsys, interval=900, monkeypatch=monkeypatch)
        assert "cached it as this host's own" in out
        assert "and applied" not in out, "the CLI process's own install is not the claim"

    def test_a_polling_source_names_when_a_running_gateway_takes_it(self, capsys, monkeypatch):
        out = self._run(capsys, interval=900, monkeypatch=monkeypatch)
        assert "900s" in out
        assert "next refresh" in out

    def test_a_boot_only_source_says_a_running_gateway_will_NOT_take_it(self, capsys, monkeypatch):
        """The case with no next cycle at all, which is the one that misleads."""
        out = self._run(capsys, interval=0, monkeypatch=monkeypatch)
        assert "boot-only" in out
        assert "restarted" in out
        assert "next refresh" not in out

    def test_the_interval_accessor_reports_zero_for_a_boot_only_source(self, transport):
        source = transport(_static_fetcher(pd.FetchedPolicy(body=_body("central"))))
        declared = governance.PolicyDistribution.from_dict(
            {"source": source, "refresh_interval_secs": 0}
        )
        assert declared.effective_refresh_interval() == 0


class TestABootPublishFailureDoesNotLeaveALooserCache:
    """``write_cache`` is best-effort for the GATEWAY — it is governed by what it fetched
    either way — but a cache-only app backend resolves its ceiling FROM that file. An
    unpublished tightening therefore leaves a stale, looser document as what every child
    spawned afterwards adopts: the failure the cache-only handoff exists to prevent,
    reached through a swallowed write error.
    """

    def test_a_disagreeing_stale_cache_is_removed(self, transport, monkeypatch):
        source = transport(_static_fetcher(pd.FetchedPolicy(body=_body("tightened"))))
        pd.write_cache(_body("loose"), source=source, etag="v1", now=time.time() - 100_000)
        monkeypatch.setattr(pd, "write_cache", lambda *a, **k: False)

        ceiling = pd.load_distributed_policy(governance.PolicyDistribution(source=source))

        assert ceiling is not None and ceiling.identity_issuer == "tightened"
        assert (
            pd.read_cache() is None
        ), "a child must find no cache and fail closed, not adopt the superseded ceiling"

    def test_an_agreeing_cache_is_left_alone(self, transport, monkeypatch):
        """An equal document is not stale, and the write may simply have been redundant."""
        source = transport(_static_fetcher(pd.FetchedPolicy(body=_body("same"))))
        pd.write_cache(_body("same"), source=source, etag="v1", now=time.time() - 100_000)
        monkeypatch.setattr(pd, "write_cache", lambda *a, **k: False)

        pd.load_distributed_policy(governance.PolicyDistribution(source=source))

        cached = pd.read_cache()
        assert cached is not None and cached.body == _body("same")

    def test_a_stale_cache_that_cannot_be_removed_fails_boot(self, transport, monkeypatch):
        """Nothing can then make the cache agree with the ceiling being adopted, so this is
        a boot refusal rather than something to log and continue past."""
        source = transport(_static_fetcher(pd.FetchedPolicy(body=_body("tightened"))))
        pd.write_cache(_body("loose"), source=source, etag="v1", now=time.time() - 100_000)
        monkeypatch.setattr(pd, "write_cache", lambda *a, **k: False)
        monkeypatch.setattr(
            pd.Path,
            "unlink",
            lambda self, missing_ok=False: (_ for _ in ()).throw(OSError("read-only filesystem")),
        )

        with pytest.raises(PlatformCompositionError) as caught:
            pd.load_distributed_policy(governance.PolicyDistribution(source=source))
        assert "cache" in str(caught.value)

    def test_no_prior_cache_is_not_a_failure(self, transport, monkeypatch):
        """With nothing cached there is nothing a child could adopt instead, so a failed
        write costs the outage fallback and nothing else."""
        source = transport(_static_fetcher(pd.FetchedPolicy(body=_body("central"))))
        monkeypatch.setattr(pd, "write_cache", lambda *a, **k: False)

        ceiling = pd.load_distributed_policy(governance.PolicyDistribution(source=source))
        assert ceiling is not None and ceiling.identity_issuer == "central"


class TestALiveRefreshCannotMigrateTheSource:
    """A candidate that MOVES ``distribution.source`` is refused, not installed.

    Nothing else would notice: the refresher re-reads the installed ceiling each cycle, so
    it would start polling the new address and the migration would look like it worked —
    until a restart, where the bootstrap declaration still names the OLD source and the
    cache, recorded against the new one, is discarded by the repoint rule. A fleet that had
    retired the old address would have hosts that run fine and cannot reboot.
    """

    def test_a_candidate_that_moves_the_source_is_refused(self, transport, install_ceiling):
        source = transport(_static_fetcher(pd.FetchedPolicy(body=_body("v1"))))
        moved = _body("v2", distribution={"source": "https://elsewhere.corp.example/p.json"})
        running = install_ceiling(
            governance.parse_policy(_doc("running", distribution={"source": source}))
        )
        transport(_static_fetcher(pd.FetchedPolicy(body=moved, etag="v2")))

        outcome = pd.refresh_now()

        assert outcome.status == pd.REFRESH_REJECTED
        assert current_context().governance is running
        assert pd.POLICY_URL_ENV in outcome.detail, "the operator needs the durable channel"
        assert "elsewhere.corp.example" not in outcome.detail, "and not the new address"

    def test_the_refused_document_is_not_cached(self, transport, install_ceiling):
        """It must not become the last-known-good either, or the next boot adopts a
        document naming a source this host will not resolve."""
        source = transport(_static_fetcher(pd.FetchedPolicy(body=_body("v1"))))
        install_ceiling(governance.parse_policy(_doc("running", distribution={"source": source})))
        moved = _body("v2", distribution={"source": "https://elsewhere.corp.example/p.json"})
        transport(_static_fetcher(pd.FetchedPolicy(body=moved, etag="v2")))

        pd.refresh_now()

        cached = pd.read_cache()
        assert cached is None or cached.body != moved

    def test_the_same_source_carried_forward_is_fine(self, transport, install_ceiling):
        """The ordinary case: a published document repeats its own source, and that is not
        a migration. Refusing it would break every fleet using the self-refresh channel."""
        source = transport(_static_fetcher(pd.FetchedPolicy(body=_body("v1"))))
        install_ceiling(governance.parse_policy(_doc("running", distribution={"source": source})))
        carried = _body("v2", distribution={"source": source})
        transport(_static_fetcher(pd.FetchedPolicy(body=carried, etag="v2")))

        assert pd.refresh_now().status == pd.REFRESH_APPLIED

    def test_a_document_declaring_no_source_is_fine(self, transport, install_ceiling):
        """A published document need not repeat the block at all."""
        source = transport(_static_fetcher(pd.FetchedPolicy(body=_body("v1"))))
        install_ceiling(governance.parse_policy(_doc("running", distribution={"source": source})))
        transport(_static_fetcher(pd.FetchedPolicy(body=_body("v2"), etag="v2")))

        assert pd.refresh_now().status == pd.REFRESH_APPLIED

    @pytest.mark.parametrize(
        "declared",
        [None, 123, [], {}, "", "   "],
        ids=["null", "int", "list", "dict", "empty", "blank"],
    )
    def test_an_unusable_declaration_reads_as_no_source(self, declared):
        body = json.dumps({"version": 1, "distribution": {"source": declared}}).encode("utf-8")
        assert pd._declared_source(body) == ""

    def test_a_document_that_is_not_json_yields_no_source(self):
        assert pd._declared_source(b"{ not json") == ""


class TestBootAppliesTheSameGatesAsARefresh:
    """Without this, boot would cache and install a document ``refresh_now`` refuses — and
    the refresher would then reject it on every cycle for the lifetime of the process,
    logging a rejection forever while the host ran on it.
    """

    def test_a_document_that_moves_the_source_is_refused_at_boot(self, transport):
        moved = _body("v1", distribution={"source": "https://elsewhere.corp.example/p.json"})
        source = transport(_static_fetcher(pd.FetchedPolicy(body=moved)))

        with pytest.raises(PlatformCompositionError) as caught:
            pd.load_distributed_policy(governance.PolicyDistribution(source=source))

        assert pd.POLICY_URL_ENV in str(caught.value)
        assert pd.read_cache() is None, "a refused document must not become last-known-good"

    def test_a_document_refused_by_the_profile_floor_is_not_cached_at_boot(
        self, transport, monkeypatch
    ):
        source = transport(_static_fetcher(pd.FetchedPolicy(body=_body("tight"))))
        monkeypatch.setattr(
            pd,
            "validate_ceiling",
            lambda c: (_ for _ in ()).throw(PlatformCompositionError("a profile is looser")),
        )

        with pytest.raises(PlatformCompositionError) as caught:
            pd.load_distributed_policy(governance.PolicyDistribution(source=source))

        assert "does not compose" in str(caught.value)
        assert pd.read_cache() is None, (
            "caching it would make the next boot fail on the same document instead of "
            "re-fetching a corrected one"
        )

    def test_a_usable_cache_still_salvages_the_boot(self, transport, monkeypatch):
        """A read-and-refused document is not an availability failure, but the cache is
        still a ceiling this fleet published and this host verified."""
        source = transport(_static_fetcher(pd.FetchedPolicy(body=_body("tight"))))
        pd.write_cache(_body("previous"), source=source, etag="v1", now=time.time() - 100_000)
        monkeypatch.setattr(
            pd,
            "validate_ceiling",
            lambda c: (_ for _ in ()).throw(PlatformCompositionError("a profile is looser")),
        )

        ceiling = pd.load_distributed_policy(governance.PolicyDistribution(source=source))
        assert ceiling is not None and ceiling.identity_issuer == "previous"

    def test_an_ordinary_document_still_boots_and_caches(self, transport):
        """The control: the gates must not refuse the normal case."""
        source = transport(_static_fetcher(pd.FetchedPolicy(body=_body("central"))))
        ceiling = pd.load_distributed_policy(governance.PolicyDistribution(source=source))
        assert ceiling is not None and ceiling.identity_issuer == "central"
        cached = pd.read_cache()
        assert cached is not None and cached.body == _body("central")


class TestAFailedRollbackLeavesNoCacheRatherThanARejectedOne:
    """The bytes on disk at that moment are the ones this host just REFUSED, so the
    invalidate-then-write ordering is what makes a partial failure safe: a failed restoring
    write leaves the cache ABSENT, and a cache-only child fails closed instead of adopting
    a rejected ceiling.
    """

    def test_a_restore_that_cannot_write_leaves_no_cache(
        self, transport, install_ceiling, monkeypatch
    ):
        source = transport(_static_fetcher(pd.FetchedPolicy(body=_body("v2"), etag="v2")))
        running = install_ceiling(
            governance.parse_policy(_doc("running", distribution={"source": source}))
        )
        pd.write_cache(_body("v1"), source=source, etag="v1")

        monkeypatch.setattr(
            pd, "apply_ceiling", lambda c: (_ for _ in ()).throw(RuntimeError("profile floor"))
        )
        # The restoring write fails AFTER the invalidation. Only the restore path is broken,
        # so the publish earlier in the refresh still succeeds.
        calls: list[int] = []

        real_write_file = pd._write_cache_file

        def fail_on_restore(path, data):
            calls.append(1)
            if len(calls) > 2:  # the publish writes two files first
                raise OSError("read-only filesystem")
            return real_write_file(path, data)

        monkeypatch.setattr(pd, "_write_cache_file", fail_on_restore)
        outcome = pd.refresh_now()

        assert outcome.status == pd.REFRESH_REJECTED
        assert current_context().governance is running
        assert (
            pd.read_cache() is None
        ), "the refused document must not survive as the last-known-good"

    def test_the_lost_fallback_is_reported_as_an_incident(
        self, transport, install_ceiling, monkeypatch
    ):
        """``refresh_now`` cannot raise, so this log and mark are the only signal."""
        marks: list = []
        source = transport(_static_fetcher(pd.FetchedPolicy(body=_body("v2"), etag="v2")))
        install_ceiling(governance.parse_policy(_doc("running", distribution={"source": source})))
        pd.write_cache(_body("v1"), source=source, etag="v1")
        monkeypatch.setattr(
            pd, "apply_ceiling", lambda c: (_ for _ in ()).throw(RuntimeError("profile floor"))
        )
        monkeypatch.setattr(
            pd,
            "mark_governance_incident",
            lambda kind, detail="": marks.append((kind, detail)),
        )

        real_write_file = pd._write_cache_file
        calls: list[int] = []

        def fail_on_restore(path, data):
            calls.append(1)
            if len(calls) > 2:
                raise OSError("read-only filesystem")
            return real_write_file(path, data)

        monkeypatch.setattr(pd, "_write_cache_file", fail_on_restore)
        pd.refresh_now()

        assert any(kind == "degraded" and "restore" in detail for kind, detail in marks)


class TestTheEnvironmentChannelMayOwnTheAddress:
    """The ordinary two-channel split: whatever provisions the host owns the address (and
    any credential in it), while the fleet publishes the cadence and the staleness bound in
    the document. Both guards had to learn about it — one aborted boot on such a document,
    the other refused every published document on a canary pinned elsewhere by env.
    """

    def test_settings_without_a_source_are_fine_when_the_env_supplies_one(self, monkeypatch):
        monkeypatch.setenv(pd.POLICY_URL_ENV, "https://config.corp.example/p.json")
        parsed = governance.PolicyDistribution.from_dict({"refresh_interval_secs": 900})
        assert parsed.refresh_interval_secs == 900

    def test_settings_without_a_source_still_fail_closed_with_no_env(self, monkeypatch):
        """The control: a block that tunes a fetch it never configures is how a fleet ends
        up ungoverned while its policy file reads as managed."""
        monkeypatch.delenv(pd.POLICY_URL_ENV, raising=False)
        with pytest.raises(PlatformCompositionError) as caught:
            governance.PolicyDistribution.from_dict({"refresh_interval_secs": 900})
        assert pd.POLICY_URL_ENV in str(caught.value), "the message must name the way out"

    def test_a_source_less_declaration_reaches_the_engine_not_just_the_parser(self, monkeypatch):
        """Round 22 made ``from_dict`` accept such a block; it was then discarded for having
        no source, so the settings were parsed and thrown away. ``on_unavailable`` is the one
        that bites: a fleet that chose ``degrade`` silently got the ``fail_closed`` default
        and aborted startup on the first outage."""
        monkeypatch.setenv(pd.POLICY_URL_ENV, "https://config.corp.example/p.json")
        doc = {
            "version": 1,
            "distribution": {"on_unavailable": governance.UNAVAILABLE_DEGRADE},
        }
        declared = governance._declared_distribution(doc)
        assert declared is not None, "the declaration must survive to the engine"
        assert declared.on_unavailable == governance.UNAVAILABLE_DEGRADE

        # And it must survive the env overlay, which is what actually reaches the tier.
        assert pd.resolve_distribution(declared).on_unavailable == (governance.UNAVAILABLE_DEGRADE)

    def test_a_source_less_declaration_stays_inert_with_no_env_pin(self, monkeypatch):
        """The control: with no address from either channel the tier must not even be
        imported, and an empty block is inert by any reading."""
        monkeypatch.delenv(pd.POLICY_URL_ENV, raising=False)
        assert governance._declared_distribution({"version": 1, "distribution": {}}) is None

    def test_an_outage_degrades_rather_than_aborting_under_that_split(self, transport, monkeypatch):
        """End to end: the disposition the fleet published is the one that applies."""
        source = transport(_failing_fetcher(TimeoutError("the endpoint is down")))
        monkeypatch.setenv(pd.POLICY_URL_ENV, source)
        declared = governance._declared_distribution(
            {"version": 1, "distribution": {"on_unavailable": governance.UNAVAILABLE_DEGRADE}}
        )

        # None, not a raise: degrade falls through to the next tier.
        assert pd.load_distributed_policy(declared) is None

    def test_a_canary_pinned_by_env_is_not_treated_as_a_migration(
        self, transport, install_ceiling, monkeypatch
    ):
        """``resolve_distribution`` already lets the env win per setting, so the document's
        own source is ignored — refusing the document over it would break every canary."""
        source = transport(_static_fetcher(pd.FetchedPolicy(body=_body("v1"))))
        install_ceiling(governance.parse_policy(_doc("running", distribution={"source": source})))
        monkeypatch.setenv(pd.POLICY_URL_ENV, source)
        # The fleet publishes its CANONICAL address; this host is pinned to the test source.
        published = _body("v2", distribution={"source": "https://canonical.corp.example/p.json"})
        transport(_static_fetcher(pd.FetchedPolicy(body=published, etag="v2")))

        assert pd.refresh_now().status == pd.REFRESH_APPLIED

    def test_the_migration_guard_still_fires_without_an_env_pin(
        self, transport, install_ceiling, monkeypatch
    ):
        """The control: with the document's declaration authoritative, a move is a move."""
        monkeypatch.delenv(pd.POLICY_URL_ENV, raising=False)
        source = transport(_static_fetcher(pd.FetchedPolicy(body=_body("v1"))))
        install_ceiling(governance.parse_policy(_doc("running", distribution={"source": source})))
        moved = _body("v2", distribution={"source": "https://elsewhere.corp.example/p.json"})
        transport(_static_fetcher(pd.FetchedPolicy(body=moved, etag="v2")))

        assert pd.refresh_now().status == pd.REFRESH_REJECTED


class TestAMaterialisedControlIsReDerivedAfterAnInstall:
    """Most governed controls are LIVE evaluations — the sandbox floor is re-read per spawn,
    the governance gate per tool call — so a ceiling swap binds them with nothing further.
    A few are materialised once, when an action is taken, and outlive the decision. A
    published tailnet origin is the case that matters: its gate fires when ``publish`` is
    CALLED, so a ceiling that later pins the capability off does not retract what is already
    serving. Before live refresh existed the ceiling only changed at boot and that could not
    arise.
    """

    def test_a_hook_runs_after_the_install(self, transport, install_ceiling):
        ran: list[str] = []
        source = transport(_static_fetcher(pd.FetchedPolicy(body=_body("v2"), etag="v2")))
        install_ceiling(governance.parse_policy(_doc("running", distribution={"source": source})))
        pd.register_post_install_hook(
            lambda: ran.append(current_context().governance.identity_issuer)
        )

        assert pd.refresh_now().status == pd.REFRESH_APPLIED
        assert ran == ["v2"], "the hook must see the ceiling it is meant to enforce"

    def test_hooks_run_on_an_UNCHANGED_poll_so_a_failure_is_retried(
        self, transport, install_ceiling
    ):
        """They are best-effort, so a transient failure — the tailnet daemon busy for one
        cycle — would otherwise never be retried: the document does not change, every later
        poll returns UNCHANGED, and a control the ceiling forbids stays materialised until
        someone restarts the host."""
        ran: list[int] = []
        source = transport(_static_fetcher(pd.FetchedPolicy(etag="v1", not_modified=True)))
        body = _body("running", distribution={"source": source})
        install_ceiling(governance.parse_policy(_doc("running", distribution={"source": source})))
        pd.write_cache(body, source=source, etag="v1")
        pd._record_installed(body)
        pd.register_post_install_hook(lambda: ran.append(1))

        assert pd.refresh_now().status == pd.REFRESH_UNCHANGED
        assert ran == [1], "a confirming poll must re-derive materialised controls"

        pd.reset_fetch_window()
        assert pd.refresh_now().status == pd.REFRESH_UNCHANGED
        assert ran == [1, 1], "and again on the next one, which is what makes it a retry"

    def test_no_hook_runs_when_the_install_is_refused(
        self, transport, install_ceiling, monkeypatch
    ):
        """A rejected candidate materialises nothing, so there is nothing to re-derive."""
        ran: list[int] = []
        source = transport(_static_fetcher(pd.FetchedPolicy(body=_body("v2"), etag="v2")))
        install_ceiling(governance.parse_policy(_doc("running", distribution={"source": source})))
        pd.register_post_install_hook(lambda: ran.append(1))
        monkeypatch.setattr(
            pd, "apply_ceiling", lambda c: (_ for _ in ()).throw(RuntimeError("profile floor"))
        )

        assert pd.refresh_now().status == pd.REFRESH_REJECTED
        assert ran == []

    def test_a_failing_hook_is_reported_as_a_governance_incident(
        self, transport, install_ceiling, monkeypatch, caplog
    ):
        """A swallowed failure is a control the ceiling calls for and the host is not applying,
        with nothing on any surface saying so. The mark is what puts it on ``security_posture``
        and the dashboard rather than leaving it in a log nobody reads."""
        marks: list = []
        source = transport(_static_fetcher(pd.FetchedPolicy(body=_body("v2"), etag="v2")))
        install_ceiling(governance.parse_policy(_doc("running", distribution={"source": source})))
        monkeypatch.setattr(
            pd, "mark_governance_incident", lambda kind, detail="": marks.append((kind, detail))
        )
        pd.register_post_install_hook(lambda: (_ for _ in ()).throw(OSError("disk full")))

        with caplog.at_level(logging.ERROR, logger="kiro_crew.platform.policy_distribution"):
            assert pd.refresh_now().status == pd.REFRESH_APPLIED

        assert any(k == "degraded" and "post_install_hook" in d for k, d in marks)
        assert any(
            r.levelno >= logging.ERROR for r in caplog.records
        ), "a control not being applied is an error, not a warning"

    def test_a_failing_hook_does_not_unwind_the_install(self, transport, install_ceiling):
        """A control that cannot be re-derived must not stop the ceiling that WAS installed
        from being reported as installed — the refresh already succeeded. Rolling back would
        restore the OLD, looser ceiling: strictly less protection than the new one with a single
        materialised control stale, since the tightened ceiling still binds every call that
        control does not pre-approve."""
        ran: list[int] = []
        source = transport(_static_fetcher(pd.FetchedPolicy(body=_body("v2"), etag="v2")))
        install_ceiling(governance.parse_policy(_doc("running", distribution={"source": source})))
        pd.register_post_install_hook(lambda: (_ for _ in ()).throw(RuntimeError("no daemon")))
        pd.register_post_install_hook(lambda: ran.append(1))

        assert pd.refresh_now().status == pd.REFRESH_APPLIED
        assert ran == [1], "a later hook must still run after an earlier one raised"


class TestAllowedToolsIsReDerivedWhenTheCeilingTightens:
    """``allowedTools`` is kiro-cli's blanket auto-approve list, and it is materialised: the
    writers consult the ceiling when they WRITE, and kiro-cli then reads the FILE. So a ceiling
    that comes to deny a tool mid-flight does not narrow a list already on disk, and every
    session started afterwards keeps auto-approving what the fleet now forbids — the tool
    short-circuits inside the harness and never reaches Kiro Crew's own PreToolUse gate.
    """

    @staticmethod
    def _hook(monkeypatch):
        from kiro_crew import agent as agent_mod

        rebuilds: list[int] = []
        monkeypatch.setattr(agent_mod, "rebuild_agent_config", lambda **kw: rebuilds.append(1))
        monkeypatch.setattr(agent_mod, "_projected_ceiling_generation", None, raising=False)
        return agent_mod, rebuilds

    def test_a_seeded_baseline_rebuilds_nothing_until_the_ceiling_moves(self, monkeypatch):
        """Boot has already projected under the current ceiling, and the baseline records
        that — before the poller starts, not on the hook's first call."""
        agent_mod, rebuilds = self._hook(monkeypatch)
        agent_mod.prime_ceiling_projection()
        for _ in range(3):
            agent_mod.reproject_for_ceiling_change()
        assert rebuilds == []

    def test_a_moved_ceiling_re_derives_the_config(self, monkeypatch, install_ceiling):
        agent_mod, rebuilds = self._hook(monkeypatch)
        agent_mod.prime_ceiling_projection()

        install_ceiling(governance.parse_policy(_doc("tightened")))
        agent_mod.reproject_for_ceiling_change()

        assert rebuilds == [1], "a ceiling swap must re-derive the on-disk auto-approvals"

    def test_a_ceiling_that_moved_before_the_first_call_is_not_missed(
        self, monkeypatch, install_ceiling
    ):
        """The reason the baseline is seeded at registration. The FIRST poll can itself
        install a new ceiling, and a first-call baseline would record that generation and skip
        the very rebuild it needed."""
        agent_mod, rebuilds = self._hook(monkeypatch)
        agent_mod.prime_ceiling_projection()

        # The first poll installs a tightened ceiling, then the hook runs for the first time.
        install_ceiling(governance.parse_policy(_doc("tightened")))
        agent_mod.reproject_for_ceiling_change()

        assert rebuilds == [1]

    def test_a_failed_rebuild_is_retried_on_the_next_poll(self, monkeypatch, install_ceiling):
        """The memo advances only after a SUCCESSFUL rebuild. Marking the generation
        synchronised on failure loses the retry, leaving forbidden auto-approvals on disk for
        the process lifetime."""
        from kiro_crew import agent as agent_mod

        attempts: list[int] = []

        def flaky(**kw):
            attempts.append(1)
            if len(attempts) == 1:
                raise OSError("could not write the agent config")

        monkeypatch.setattr(agent_mod, "rebuild_agent_config", flaky)
        monkeypatch.setattr(agent_mod, "_projected_ceiling_generation", None, raising=False)
        agent_mod.prime_ceiling_projection()
        install_ceiling(governance.parse_policy(_doc("tightened")))

        with pytest.raises(OSError):
            agent_mod.reproject_for_ceiling_change()
        # The next confirming poll tries again, because the generation was never marked.
        agent_mod.reproject_for_ceiling_change()
        assert len(attempts) == 2

        # And once it succeeds, it stops.
        agent_mod.reproject_for_ceiling_change()
        assert len(attempts) == 2

    def test_an_unseeded_baseline_rebuilds_rather_than_skipping(self, monkeypatch):
        """The safe direction if some other entry point starts the poller: a redundant
        rewrite costs a file write, a skipped one costs the tighten."""
        agent_mod, rebuilds = self._hook(monkeypatch)
        agent_mod.reproject_for_ceiling_change()
        assert rebuilds == [1]

    def test_it_is_registered_as_a_post_install_hook(self):
        """The wiring, not just the function: the gateway registers it beside the tailnet
        revocation, before the poller starts."""
        from pathlib import Path as _Path

        import kiro_crew.slack.gateway as gw

        src = _Path(gw.__file__).read_text(encoding="utf-8")
        assert "register_post_install_hook(reproject_for_ceiling_change)" in src
        assert src.index("register_post_install_hook(reproject_for_ceiling_change)") < src.index(
            "await asyncio.to_thread(start_refresher)"
        ), "registered before the poller, so the first install already re-derives"


class TestASignatureMandateBoundsAForgedCache:
    """The OS seal is what stops a same-uid process WRITING the cache; a signature mandate is
    what stops a written one being BELIEVED. The two are independent, and the second is the
    one that still holds where the first cannot be applied — on a host the operator has
    explicitly opted into running unconfined (``sandbox_allow_no_isolation``), where an app
    backend has the whole filesystem rather than just this directory.
    """

    def test_a_forged_cache_is_refused_when_a_signature_is_mandated(self, monkeypatch):
        """The boot path: whoever wrote it, an unsigned document is not adopted."""
        pd.write_cache(_body("forged"), source=_TEST_SOURCE, etag="v1")
        monkeypatch.setattr(governance, "_policy_signature_required", lambda: True)

        with pytest.raises(PlatformCompositionError) as caught:
            pd.parse_distributed_policy(pd.read_cache().body, source=_TEST_SOURCE)
        assert "require_policy_signature" in str(caught.value)

    def test_a_cache_only_child_refuses_a_forged_cache_under_the_mandate(self, monkeypatch):
        """And the child that resolves its ceiling FROM the cache, which is the process the
        exposure is about."""
        pd.write_cache(_body("forged"), source=_TEST_SOURCE, etag="v1")
        monkeypatch.setenv(pd.POLICY_CACHE_ONLY_ENV, "1")
        monkeypatch.setattr(governance, "_policy_signature_required", lambda: True)

        with pytest.raises(PlatformCompositionError) as caught:
            pd.load_distributed_policy(None)
        assert "cache-only" in str(caught.value) or "unusable" in str(caught.value)

    def test_a_correctly_signed_cache_is_still_adopted(self, monkeypatch):
        """The control: the mandate must not refuse a legitimate document."""
        issuer, key = "corp", "s3cr3t-trust-key"
        doc = _doc("published")
        doc["identity"] = {"issuer": issuer}
        doc["identity"]["signature"] = governance.hmac_signature(
            key, governance.policy_signing_payload(doc)
        )
        body = json.dumps(doc).encode("utf-8")
        pd.write_cache(body, source=_TEST_SOURCE, etag="v1")
        monkeypatch.setattr(governance, "_policy_trust_settings", lambda: (True, {issuer: key}))

        ceiling = pd.parse_distributed_policy(body, source=_TEST_SOURCE)
        assert ceiling.signature_state == governance.SIGNATURE_VERIFIED


class TestMalformedInputIsRefusedNotCrashedOn:
    """Two shapes a typo produces that made a library call raise out of the very check meant
    to reject it: a bracketed host ``urlsplit`` cannot parse, and a JSON integer too large for
    ``math.isfinite`` to convert to a float.
    """

    @pytest.mark.parametrize(
        "source", ["https://[::1", "https://[bad]:x/p.json"], ids=["unclosed", "not-an-address"]
    )
    def test_a_malformed_declared_source_is_a_composition_error(self, source):
        """An operator who mistyped the address must get an error naming the key, not an
        uncaught ValueError traceback out of boot."""
        with pytest.raises(PlatformCompositionError) as caught:
            governance.PolicyDistribution.from_dict({"source": source})
        assert "distribution.source" in str(caught.value)

    def test_the_sanitiser_survives_a_malformed_source(self):
        """This one matters most: a sanitiser that crashes on a malformed source takes the
        error REPORT down with it, so the operator sees a traceback about URL parsing instead
        of the problem they actually have. The env channel is not validated by ``from_dict``,
        so a malformed address does reach here."""
        assert pd._sanitize_detail("could not fetch it", "https://[::1") == "could not fetch it"
        assert pd._split_url("https://[::1").scheme == ""

    def test_a_malformed_env_source_is_refused_as_an_unknown_scheme(self, monkeypatch):
        """An unparseable URL yields an all-empty split, which the fetcher table already
        handles: no scheme means no registered transport."""
        monkeypatch.setenv(pd.POLICY_URL_ENV, "https://[::1")
        with pytest.raises(PlatformCompositionError) as caught:
            pd.fetch_once(pd.resolve_distribution(None))
        assert "scheme" in str(caught.value)

    def test_the_posture_survives_a_malformed_env_source(self, monkeypatch):
        """It is polled by every open Security tab, so a crash here is a broken panel."""
        monkeypatch.setenv(pd.POLICY_URL_ENV, "https://[::1")
        posture = pd.distribution_posture()
        assert posture["configured"] is True and posture["source_scheme"] == ""

    @pytest.mark.parametrize(
        "source",
        ["https://host:notaport/p.json", "https://host:99999999/p.json"],
        ids=["non-numeric", "out-of-range"],
    )
    def test_a_malformed_port_is_a_composition_error(self, source):
        """``.port`` is a LAZILY parsed property, so guarding ``urlsplit`` does not cover it."""
        with pytest.raises(PlatformCompositionError) as caught:
            governance.PolicyDistribution.from_dict({"source": source})
        assert "distribution.source" in str(caught.value)

    @pytest.mark.parametrize(
        "source",
        ["https://host:notaport/p.json", "https://host:99999999/p.json"],
        ids=["non-numeric", "out-of-range"],
    )
    def test_the_source_identity_survives_a_malformed_port(self, source):
        """The env channel is not validated by ``from_dict``, so a malformed port does reach
        the engine — and ``_source_digest`` is on the boot path."""
        digest = pd._source_digest(source)
        assert digest and digest == pd._source_digest(source), "stable, and no raise"
        assert digest != pd._source_digest(
            "https://host/p.json"
        ), "an unusable port is still part of the location, so it is not the same source"

    @pytest.mark.parametrize(
        "key",
        ["refresh_interval_secs", "timeout_secs", "max_cache_age_secs"],
    )
    def test_a_huge_json_integer_is_REFUSED_not_crashed_on(self, key):
        """``math.isfinite`` converts to a float first, so a 310-digit integer raised
        OverflowError inside the check meant to screen it — and ``float()`` on the way out would
        have raised three lines later. The verdict is now a clean refusal (it is far past what the
        platform can wait for); what this pins is that it is a COMPOSITION error rather than an
        OverflowError escaping from a library."""
        with pytest.raises(PlatformCompositionError) as caught:
            governance.PolicyDistribution.from_dict({"source": _TEST_SOURCE, key: int("1" * 310)})
        assert f"distribution.{key}" in str(caught.value)

    @pytest.mark.parametrize("key", ["refresh_interval_secs", "timeout_secs", "max_cache_age_secs"])
    def test_a_duration_beyond_what_the_platform_can_WAIT_for_is_refused(self, key):
        """The refresher passes the interval to ``Event.wait`` and the fetch passes the timeout
        to a socket, and both raise OverflowError above ``threading.TIMEOUT_MAX`` — silently
        killing the poller thread, so the host simply stops receiving policy updates. Round 27
        removed the ``float()`` round-trip that used to reject these by accident."""
        with pytest.raises(PlatformCompositionError) as caught:
            governance.PolicyDistribution.from_dict(
                {"source": _TEST_SOURCE, key: int(governance.MAX_DURATION_SECS) + 1}
            )
        assert "wait for" in str(caught.value)

    def test_the_ceiling_itself_is_accepted(self):
        """It bounds typos rather than intentions — ~292 years — so the boundary is inclusive."""
        parsed = governance.PolicyDistribution.from_dict(
            {"source": _TEST_SOURCE, "refresh_interval_secs": int(governance.MAX_DURATION_SECS)}
        )
        assert parsed.refresh_interval_secs == int(governance.MAX_DURATION_SECS)

    def test_the_ENVIRONMENT_channel_is_bounded_by_the_same_constant(self, monkeypatch):
        """A merely LARGE finite value passes the isfinite screen — ``1e12`` is a perfectly good
        float — and then raises inside ``Event.wait``. Both channels read one constant so they
        cannot drift apart."""
        monkeypatch.setenv(pd.POLICY_URL_ENV, "https://config.corp.example/p.json")
        monkeypatch.setenv(pd.POLICY_REFRESH_ENV, str(int(pd.MAX_DURATION_SECS) + 1))
        with pytest.raises(PlatformCompositionError) as caught:
            pd.resolve_distribution(None)
        assert "wait for" in str(caught.value)

    def test_an_oversized_wait_really_does_raise(self):
        """The premise, asserted rather than assumed: this is why the bound exists."""
        import threading as _threading

        with pytest.raises(OverflowError):
            _threading.Event().wait(governance.MAX_DURATION_SECS + 1)

    def test_a_non_finite_FLOAT_is_still_refused(self):
        """The control: the check must keep doing its job for the type that has such values.
        NaN slips past every range comparison, which is why it is screened here."""
        for bad in (float("nan"), float("inf")):
            with pytest.raises(PlatformCompositionError) as caught:
                governance.PolicyDistribution.from_dict(
                    {"source": _TEST_SOURCE, "timeout_secs": bad}
                )
            assert "finite" in str(caught.value)

    def test_a_huge_integer_in_the_cached_document_yields_no_bound(self):
        """``_cached_max_cache_age``'s contract is that anything unusable yields 0, and its
        ``try`` does not cover the arithmetic below it."""
        body = json.dumps(
            {"version": 1, "distribution": {"max_cache_age_secs": int("1" * 310)}}
        ).encode("utf-8")
        assert pd._cached_max_cache_age(body) == int("1" * 310)

        # And a non-finite float there still reads as no bound.
        assert pd._cached_max_cache_age(b'{"distribution": {"max_cache_age_secs": 1e999}}') == 0


class TestAPublishedSourceMayNotCarryACredential:
    """The ``distribution`` block lands in the cache VERBATIM — the document has to be
    byte-identical for its signature to verify — and that file is readable by an app
    backend, which is arbitrary third-party code. So a credential placed in a *declared*
    source would be published to every host and then handed to every app. The module's rule
    was already that the per-machine credential travels in ``KIROCREW_POLICY_HEADERS``,
    which "a published document must not" carry; this makes it enforceable.
    """

    @pytest.mark.parametrize(
        "source",
        [
            "https://user:pass@config.corp.example/p.json",
            "https://user@config.corp.example/p.json",
            "https://config.corp.example/p.json?X-Amz-Signature=deadbeefcafe",
            "https://config.corp.example/p.json?token=abc",
        ],
        ids=["userinfo-both", "userinfo-user", "presigned", "query-token"],
    )
    def test_a_declared_source_with_a_credential_shape_is_refused(self, source):
        with pytest.raises(PlatformCompositionError) as caught:
            governance.PolicyDistribution.from_dict({"source": source})
        message = str(caught.value)
        assert (
            pd.POLICY_URL_ENV in message and pd.POLICY_HEADERS_ENV in message
        ), "the refusal must name the channels that DO carry a credential"

    @pytest.mark.parametrize(
        "source",
        [
            "https://config.corp.example/p.json",
            "https://config.corp.example:8443/deep/path/p.json",
            "file:///etc/kirocrew/security_policy.json",
            "kctest://policy.example/security_policy.json",
        ],
        ids=["plain", "port-and-path", "file", "custom-scheme"],
    )
    def test_an_ordinary_declared_source_is_accepted(self, source):
        assert governance.PolicyDistribution.from_dict({"source": source}).source == source

    def test_the_environment_channel_is_deliberately_unrestricted(self, monkeypatch):
        """That is where a pre-signed URL belongs: it is set by whatever provisions the
        host and it never lands in the document."""
        presigned = "https://config.corp.example/p.json?X-Amz-Signature=deadbeefcafe"
        monkeypatch.setenv(pd.POLICY_URL_ENV, presigned)
        assert pd.resolve_distribution(None).source == presigned


class TestTheCacheSwapComparesTheSourceToo:
    """Identical bytes can be published for a DIFFERENT source — a repoint whose document
    did not change — and a body-only swap would let an in-flight refresh overwrite that
    provenance with its own. The next boot's repoint rule then discards a perfectly good
    cache as not belonging to the source it resolves.
    """

    def test_the_pair_distinguishes_a_repoint_that_did_not_change_the_bytes(self):
        body = _body("central")
        pd.write_cache(body, source="https://old.corp.example/p.json", etag="v1")
        old_pair = pd._cache_pair(pd.read_cache_meta())

        pd.write_cache(body, source="https://new.corp.example/p.json", etag="v1")
        new_pair = pd._cache_pair(pd.read_cache_meta())

        assert old_pair[0] == new_pair[0], "the bytes are identical, by construction"
        assert old_pair != new_pair, "so only the source half can tell them apart"

    def test_a_swap_expecting_the_old_source_does_not_overwrite_the_repointed_cache(self):
        body = _body("central")
        pd.write_cache(body, source="https://old.corp.example/p.json", etag="v1")
        stale_expectation = pd._cache_pair(pd.read_cache_meta())

        # The repoint lands, with the same bytes under the new source.
        pd.write_cache(body, source="https://new.corp.example/p.json", etag="v2")

        published = pd.write_cache(
            body,
            source="https://old.corp.example/p.json",
            etag="v1",
            expect_pair=stale_expectation,
        )

        assert published is False, "the swap must lose"
        meta = pd.read_cache_meta()
        assert meta is not None
        assert meta.source_digest == pd._source_digest("https://new.corp.example/p.json")
        assert meta.etag == "v2", "the repointed provenance must survive intact"

    def test_no_cache_is_the_empty_pair(self):
        assert pd._cache_pair(None) == ("", "")


class TestBootCannotRollTheCeilingBackwardEither:
    """The live refresh got this in round 15; boot is not exempt. A slow boot fetch of v2
    racing a ``policy fetch --force`` that publishes v3 would overwrite the cache with v2 and
    install it — rolling the ceiling backward to a possibly looser document on the one path
    where nothing is running yet to notice.
    """

    @staticmethod
    def _race(transport, monkeypatch, *, racer: bytes):
        """A boot fetch of v2 with *racer* published during the fetch."""
        source = transport(_static_fetcher(pd.FetchedPolicy(body=_body("v2"), etag="v2")))
        pd.write_cache(_body("v1"), source=source, etag="v1", now=time.time() - 100_000)
        real_fetch = pd.fetch_once

        def fetch_then_race(dist, cached=None):
            result = real_fetch(dist, cached)
            pd.write_cache(racer, source=source, etag="v3")
            return result

        monkeypatch.setattr(pd, "fetch_once", fetch_then_race)
        return source

    def test_the_winner_is_adopted_not_overwritten(self, transport, monkeypatch):
        source = self._race(transport, monkeypatch, racer=_body("v3"))

        ceiling = pd.load_distributed_policy(governance.PolicyDistribution(source=source))

        assert (
            ceiling is not None and ceiling.identity_issuer == "v3"
        ), "boot must install the newer document, not the one it fetched"
        cached = pd.read_cache()
        assert cached is not None and cached.body == _body(
            "v3"
        ), "and must not overwrite it with the older bytes"

    def test_an_uncontended_boot_still_installs_what_it_fetched(self, transport):
        """The control: with no racer the swap passes and boot behaves as before."""
        source = transport(_static_fetcher(pd.FetchedPolicy(body=_body("v2"), etag="v2")))
        pd.write_cache(_body("v1"), source=source, etag="v1", now=time.time() - 100_000)

        ceiling = pd.load_distributed_policy(governance.PolicyDistribution(source=source))
        assert ceiling is not None and ceiling.identity_issuer == "v2"
        cached = pd.read_cache()
        assert cached is not None and cached.body == _body("v2")

    def test_a_winner_this_host_cannot_run_is_refused_not_installed(self, transport, monkeypatch):
        """ "Someone else published it" is not evidence this host can run under it, so the
        winner goes through the same gates the fetched document does."""
        source = self._race(transport, monkeypatch, racer=_body("v3"))
        # Selective: v2 must still clear the gate, or the boot short-circuits into the
        # older cache before the race is ever reached and the test proves nothing.
        real_validate = pd.validate_ceiling

        def refuse_only_the_winner(ceiling):
            if ceiling.identity_issuer == "v3":
                raise PlatformCompositionError("a profile is looser")
            return real_validate(ceiling)

        monkeypatch.setattr(pd, "validate_ceiling", refuse_only_the_winner)

        with pytest.raises(PlatformCompositionError) as caught:
            pd.load_distributed_policy(governance.PolicyDistribution(source=source))
        assert "another process cached" in str(caught.value)

    def test_a_winner_cached_against_a_DIFFERENT_source_is_refused(self, transport, monkeypatch):
        """The CAS loss is exactly what makes this reachable: the winner was published by
        another process, which may have been configured for a different source. Re-gating parse,
        migration and composition but not provenance would let source B's ceiling govern a host
        pointed at source A."""
        source = transport(_static_fetcher(pd.FetchedPolicy(body=_body("v2"), etag="v2")))
        pd.write_cache(_body("v1"), source=source, etag="v1", now=time.time() - 100_000)
        real_fetch = pd.fetch_once

        def fetch_then_race(dist, cached=None):
            result = real_fetch(dist, cached)
            # Another process, pointed elsewhere, publishes its own ceiling.
            pd.write_cache(_body("elsewhere"), source="https://other.corp.example/p.json")
            return result

        monkeypatch.setattr(pd, "fetch_once", fetch_then_race)

        with pytest.raises(PlatformCompositionError) as caught:
            pd.load_distributed_policy(governance.PolicyDistribution(source=source))
        assert "different source" in str(caught.value)

    def test_a_rejected_winner_leaves_the_host_UNgoverned_not_flagged(self, transport, monkeypatch):
        """``central_ceiling_installed`` is what the gateway flags an app backend cache-only on.
        Recording the fetched bytes before the publish resolved meant a lost swap whose winner
        was then REJECTED left the host degraded while every child was told to resolve its
        ceiling from a cache holding the document this host had just refused."""
        source = self._race(transport, monkeypatch, racer=_body("v3"))
        real_validate = pd.validate_ceiling

        def refuse_only_the_winner(ceiling):
            if ceiling.identity_issuer == "v3":
                raise PlatformCompositionError("a profile is looser")
            return real_validate(ceiling)

        monkeypatch.setattr(pd, "validate_ceiling", refuse_only_the_winner)

        with pytest.raises(PlatformCompositionError):
            pd.load_distributed_policy(governance.PolicyDistribution(source=source))

        assert (
            not pd.central_ceiling_installed()
        ), "a refused winner must not leave children resolving from the cache that holds it"

    def test_a_winner_that_moves_the_source_is_refused_too(self, transport, monkeypatch):
        moved = _body("v3", distribution={"source": "https://elsewhere.corp.example/p.json"})
        source = self._race(transport, monkeypatch, racer=moved)

        with pytest.raises(PlatformCompositionError) as caught:
            pd.load_distributed_policy(governance.PolicyDistribution(source=source))
        assert pd.POLICY_URL_ENV in str(caught.value)


class TestTheCacheNeverStoresTheSourceItself:
    """The URL can BE the credential — a pre-signed link carries its signature in the
    query string — and the app backend reads this file. The cache records only WHICH
    source a copy came from, which is an equality test, and an equality test does not
    need the plaintext.
    """

    def test_the_metadata_on_disk_carries_no_url(self):
        secret = "https://signed.corp.example/p.json?X-Amz-Signature=deadbeefcafe"
        pd.write_cache(_body("central"), source=secret, etag="v1")

        raw = (pd.cache_dir() / pd._CACHE_META_LEAF).read_text(encoding="utf-8")

        assert secret not in raw
        assert "X-Amz-Signature" not in raw
        assert "signed.corp.example" not in raw
        assert pd._source_digest(secret) in raw

    def test_a_rotated_presigned_signature_keeps_the_last_known_good(self, transport):
        """A pre-signed URL is the documented shape for the environment channel, and its
        signature ROTATES by design. Hashing the whole URL made every rotation look like a
        repoint, so the cache was discarded as a retired endpoint's copy — and a host that then
        hit a transient outage had no last-known-good and aborted startup under the fail-closed
        default. Nobody had to do anything for that."""
        base = "https://config.corp.example/policy.json"
        first = f"{base}?X-Amz-Date=20260101&X-Amz-Signature=aaaa1111"
        rotated = f"{base}?X-Amz-Date=20260102&X-Amz-Signature=bbbb2222"

        assert pd._source_digest(first) == pd._source_digest(rotated)

        pd.write_cache(_body("central"), source=first, etag="v1")
        cached = pd.read_cache()
        assert cached is not None
        assert cached.source_digest == pd._source_digest(
            rotated
        ), "the rotated URL must still recognise its own cache"

    def test_a_rotation_does_not_survive_an_outage_as_a_repoint(self, transport):
        """End to end: the source is down, the credential rotated, and the cache still serves."""
        source = transport(_failing_fetcher(TimeoutError("the endpoint is down")))
        # A real presigning shape, not a lone ``?sig=``: that name is only treated as a
        # signature when the query is positively a SAS, because on its own it is as likely to
        # be somebody's selector.
        pd.write_cache(_body("central"), source=f"{source}?X-Amz-Signature=old", etag="v1")

        ceiling = pd.load_distributed_policy(
            governance.PolicyDistribution(source=f"{source}?X-Amz-Signature=new")
        )
        assert ceiling is not None and ceiling.identity_issuer == "central"

    @pytest.mark.parametrize(
        ("a", "b"),
        [
            ("https://a.corp.example/p.json", "https://b.corp.example/p.json"),
            ("https://a.corp.example/p.json", "https://a.corp.example/other.json"),
            ("https://a.corp.example/p.json", "https://a.corp.example:8443/p.json"),
            ("https://a.corp.example/p.json", "http://a.corp.example/p.json"),
        ],
        ids=["host", "path", "port", "scheme"],
    )
    def test_a_real_repoint_is_still_a_different_identity(self, a, b):
        """The location is what "same source" means, so every part of it still counts."""
        assert pd._source_digest(a) != pd._source_digest(b)

    def test_the_identity_is_case_normalised_where_the_url_is(self):
        """Scheme and host are case-insensitive; the path is not."""
        assert pd._source_digest("HTTPS://A.CORP.EXAMPLE/p.json") == pd._source_digest(
            "https://a.corp.example/p.json"
        )
        assert pd._source_digest("https://a.corp.example/P.json") != pd._source_digest(
            "https://a.corp.example/p.json"
        )

    def test_the_basic_auth_password_is_dropped_but_the_USERNAME_is_not(self):
        """The same selector/credential split the query gets. With basic auth the username names
        the account, and two tenants at one host and path are two different documents —
        collapsing them would leave tenant A's possibly looser ceiling in force after a repoint
        to tenant B. The password is the rotating half."""
        base = "config.corp.example/p.json"
        assert pd._source_digest(f"https://u:p1@{base}") == pd._source_digest(
            f"https://u:p2@{base}"
        ), "a rotated password is not a repoint"
        assert pd._source_digest(f"https://tenantA:p@{base}") != pd._source_digest(
            f"https://tenantB:p@{base}"
        ), "a different account IS a different document"
        assert pd._source_digest(f"https://tenantA:p@{base}") != pd._source_digest(
            f"https://{base}"
        ), "and so is no account at all"

    @pytest.mark.parametrize(
        ("a", "b"),
        [
            ("?policy=team-a", "?policy=team-b"),
            ("?v=1", "?v=2"),
            ("?policy=team-a", ""),
        ],
        ids=["policy-selector", "version-selector", "present-vs-absent"],
    )
    def test_a_query_that_SELECTS_the_document_stays_in_the_identity(self, a, b):
        """A query can select the document as well as authenticate the request. Treating
        ``?policy=team-a`` and ``?policy=team-b`` as one source would let a repoint to a
        STRICTER policy keep serving the looser one — indefinitely on a boot-only source, which
        never re-fetches once a ceiling is established."""
        base = "https://config.corp.example/p.json"
        assert pd._source_digest(base + a) != pd._source_digest(base + b)

    @pytest.mark.parametrize(
        ("a", "b"),
        [
            ("?X-Amz-Date=1&X-Amz-Signature=aaa", "?X-Amz-Date=2&X-Amz-Signature=bbb"),
            ("?X-Goog-Signature=aaa", "?X-Goog-Signature=bbb"),
            ("?sig=aaa&sv=2020-10-02", "?sig=bbb&sv=2020-10-02"),
        ],
        ids=["aws-sigv4", "gcs-v4", "azure-sas"],
    )
    def test_a_signature_that_AUTHENTICATES_the_request_does_not(self, a, b):
        base = "https://config.corp.example/p.json"
        assert pd._source_digest(base + a) == pd._source_digest(base + b)

    def test_a_selector_survives_alongside_a_rotating_signature(self):
        """The two must compose: the same document keeps its identity across a rotation, and a
        different document does not borrow it."""
        base = "https://config.corp.example/p.json"
        a1 = f"{base}?policy=team-a&X-Amz-Signature=aaa"
        a2 = f"{base}?policy=team-a&X-Amz-Signature=bbb"
        b1 = f"{base}?policy=team-b&X-Amz-Signature=aaa"
        assert pd._source_digest(a1) == pd._source_digest(a2)
        assert pd._source_digest(a1) != pd._source_digest(b1)

    def test_parameter_order_is_not_a_repoint(self):
        """A URL is not required to preserve order, and a re-issue that merely reorders must not
        discard the cache."""
        base = "https://config.corp.example/p.json"
        assert pd._source_digest(f"{base}?a=1&b=2") == pd._source_digest(f"{base}?b=2&a=1")

    @pytest.mark.parametrize("name", ["sp", "sr", "ss", "si", "st", "se", "sv", "sig"])
    def test_a_short_sas_name_is_kept_when_the_query_is_not_a_sas(self, name):
        """Azure's SAS names are short and generic enough to be somebody else's selector:
        ``?sp=team-a`` reads perfectly well as "policy = team-a". Stripping them unconditionally
        collapsed it with ``?sp=team-b``, which is the exact failure the namespaced families
        (``x-amz-*``, ``x-goog-*``) cannot cause."""
        base = "https://config.corp.example/p.json"
        assert pd._source_digest(f"{base}?{name}=team-a") != pd._source_digest(
            f"{base}?{name}=team-b"
        )

    def test_a_real_sas_is_identified_by_signature_AND_service_version(self):
        """Both, because every Azure SAS carries them and neither is something a plain selector
        needs. With both present the whole SAS set is presentation, so a rotation is not a
        repoint — including the ``sp`` permissions field that changes with a re-issue."""
        base = "https://config.corp.example/p.json"
        assert pd._source_digest(f"{base}?sig=aaa&sv=2020-10-02&sp=r&se=2026-01-01") == (
            pd._source_digest(f"{base}?sig=bbb&sv=2020-10-02&sp=r&se=2026-06-01")
        )

    def test_a_selector_alongside_a_real_sas_still_counts(self):
        """Identifying the SAS must not swallow a parameter outside it."""
        base = "https://config.corp.example/p.json?sig=aaa&sv=2020-10-02"
        assert pd._source_digest(f"{base}&policy=team-a") != pd._source_digest(
            f"{base}&policy=team-b"
        )

    def test_an_unknown_presigning_scheme_fails_toward_a_fetch(self):
        """The residual, stated as a test: a signature parameter this list does not know stays
        in the identity, so a rotation discards the cache and costs one fetch. That is the safe
        direction — the opposite mistake keeps a superseded ceiling in force."""
        base = "https://config.corp.example/p.json"
        assert pd._source_digest(f"{base}?bespoke-sig=aaa") != pd._source_digest(
            f"{base}?bespoke-sig=bbb"
        )

    def test_the_repoint_rule_still_works_off_the_digest(self, transport):
        """The field's only job: refusing a retired endpoint's copy after a repoint."""
        first = transport(_static_fetcher(pd.FetchedPolicy(body=_body("old"))))
        pd.write_cache(_body("old"), source=first, etag="v1")
        assert pd.read_cache() is not None

        second = transport(_static_fetcher(pd.FetchedPolicy(body=_body("new"))), scheme="kctest2")
        ceiling = pd.load_distributed_policy(governance.PolicyDistribution(source=second))
        assert (
            ceiling is not None and ceiling.identity_issuer == "new"
        ), "the cache from the retired source must not have been served"

    def test_a_legacy_plaintext_cache_is_hashed_on_read_not_discarded(self):
        """An upgrade must not throw away the last-known-good copy."""
        directory = pd.cache_dir()
        directory.mkdir(parents=True, exist_ok=True)
        (directory / pd._CACHE_DOC_LEAF).write_bytes(_body("central"))
        (directory / pd._CACHE_META_LEAF).write_text(
            json.dumps(
                {
                    "source": _TEST_SOURCE,  # the pre-digest spelling
                    "fetched_at": time.time(),
                    "etag": "v1",
                    "last_modified": "",
                    "digest": pd._body_digest(_body("central")),
                }
            ),
            encoding="utf-8",
        )
        cached = pd.read_cache()
        assert cached is not None
        assert cached.source_digest == pd._source_digest(_TEST_SOURCE)

    def test_metadata_with_neither_spelling_reads_as_absent(self):
        directory = pd.cache_dir()
        directory.mkdir(parents=True, exist_ok=True)
        (directory / pd._CACHE_DOC_LEAF).write_bytes(_body("central"))
        (directory / pd._CACHE_META_LEAF).write_text(
            json.dumps({"fetched_at": time.time()}), encoding="utf-8"
        )
        assert pd.read_cache() is None


class TestAnUnconditional304IsNotSuccess:
    """``--force`` skips the validators deliberately, so a fetcher that answers "unchanged"
    anyway has answered nothing. Reporting that as UNCHANGED made ``kirocrew policy fetch
    --force`` exit 0 having established nothing — and exiting 0 is exactly what a
    config-management run reads as "this host took the change".
    """

    def test_a_forced_fetch_answered_304_is_a_rejection(self, transport, install_ceiling):
        source = transport(_static_fetcher(pd.FetchedPolicy(etag="v1", not_modified=True)))
        install_ceiling(governance.parse_policy(_doc("running", distribution={"source": source})))
        pd.write_cache(_body("running"), source=source, etag="v1")

        outcome = pd.refresh_now(force=True)

        assert outcome.status == pd.REFRESH_REJECTED
        assert "no validators were sent" in outcome.detail

    def test_the_cli_exits_non_zero_on_it(self, transport, install_ceiling, capsys):
        """Which is the whole point: a verification step must fail on the host that did not
        take the change."""
        from kiro_crew import cli_commands

        source = transport(_static_fetcher(pd.FetchedPolicy(etag="v1", not_modified=True)))
        install_ceiling(governance.parse_policy(_doc("running", distribution={"source": source})))
        pd.write_cache(_body("running"), source=source, etag="v1")

        with pytest.raises(SystemExit) as caught:
            cli_commands._policy_fetch(force=True)
        assert caught.value.code == 1

    def test_an_ordinary_304_against_a_matching_cache_is_still_unchanged(
        self, transport, install_ceiling
    ):
        """The control: a real 304 answered against validators we DID send is success."""
        source = transport(_static_fetcher(pd.FetchedPolicy(etag="v1", not_modified=True)))
        body = _body("running", distribution={"source": source})
        install_ceiling(governance.parse_policy(_doc("running", distribution={"source": source})))
        pd.write_cache(body, source=source, etag="v1")
        pd._record_installed(body)

        assert pd.refresh_now().status == pd.REFRESH_UNCHANGED


class TestTheCachedDocumentsOwnStalenessBoundSurvivesARestart:
    """``max_cache_age_secs`` is as likely to be set in the published policy as in the
    bootstrap declaration, and at BOOT the resolved distribution comes from the env or a
    lower tier. So a fleet that set the bound only in its own document had it applied
    while the process ran and dropped on the next restart — exactly the
    restart-during-an-outage case the bound exists for.
    """

    def test_a_bound_declared_only_in_the_cached_document_is_enforced(self, transport):
        """The restart-during-an-outage case: the source is unreachable, so the cache is
        the only answer available, and the bound decides whether it is an acceptable one.
        Refusing means failing closed — which is the point."""
        source = transport(_failing_fetcher(TimeoutError("the endpoint is down")))
        # The bootstrap declaration sets NO bound; the cached document sets its own.
        body = _body("central", distribution={"source": source, "max_cache_age_secs": 300})
        pd.write_cache(body, source=source, etag="v1", now=time.time() - 10_000)

        with pytest.raises(PlatformCompositionError):
            pd.load_distributed_policy(governance.PolicyDistribution(source=source))

    def test_the_same_cache_IS_served_when_the_document_declares_no_bound(self, transport):
        """The control, so the refusal above cannot pass for the wrong reason: identical
        age, identical outage, and the only difference is the declaration."""
        source = transport(_failing_fetcher(TimeoutError("the endpoint is down")))
        body = _body("central", distribution={"source": source})
        pd.write_cache(body, source=source, etag="v1", now=time.time() - 10_000)

        ceiling = pd.load_distributed_policy(governance.PolicyDistribution(source=source))
        assert ceiling is not None and ceiling.identity_issuer == "central"

    def test_a_document_within_its_own_bound_is_still_served(self, transport):
        source = transport(_failing_fetcher(TimeoutError("the endpoint is down")))
        body = _body("central", distribution={"source": source, "max_cache_age_secs": 10_000})
        pd.write_cache(body, source=source, etag="v1", now=time.time() - 300)

        ceiling = pd.load_distributed_policy(governance.PolicyDistribution(source=source))
        assert ceiling is not None and ceiling.identity_issuer == "central"

    def test_the_tighter_of_the_two_wins(self, transport):
        """Tightest-wins, matching the governance model's own rule."""
        source = transport(_failing_fetcher(TimeoutError("down")))
        body = _body("central", distribution={"source": source, "max_cache_age_secs": 10_000})
        pd.write_cache(body, source=source, etag="v1", now=time.time() - 5_000)
        # The bootstrap bound is the tighter one here.
        declared = governance.PolicyDistribution(source=source, max_cache_age_secs=600)

        with pytest.raises(PlatformCompositionError):
            pd.load_distributed_policy(declared)

    @pytest.mark.parametrize(
        "declared",
        [None, "not a number", True, -1, 0, 1.5, float("inf"), float("nan")],
        ids=["null", "string", "bool", "negative", "zero", "fractional", "inf", "nan"],
    )
    def test_an_unusable_declaration_means_no_bound_of_its_own(self, declared):
        """Refused here rather than raised: this runs BEFORE the document is validated, so
        a malformed one must reach the parse that reports it properly."""
        body = json.dumps({"version": 1, "distribution": {"max_cache_age_secs": declared}}).encode(
            "utf-8"
        )
        assert pd._cached_max_cache_age(body) == 0

    def test_a_document_that_is_not_even_json_yields_no_bound(self):
        assert pd._cached_max_cache_age(b"{ not json") == 0


class TestARedirectRefusalNamesNoTarget:
    def test_only_the_scheme_of_the_target_is_reported(self):
        """The target comes from the ENDPOINT's ``Location`` header, so it is neither ours
        to publish nor covered by ``_redact_source`` — and a redirect to a pre-signed URL
        would carry its signature into the boot abort and the log ring."""
        target = "https://evil.corp.example/p.json?X-Amz-Signature=deadbeefcafe"
        handler = pd._NoRedirects()

        with pytest.raises(urllib.error.HTTPError) as caught:
            handler.redirect_request(
                _FakeRequest("https://config.corp.example/p.json"), None, 302, "Found", {}, target
            )

        message = str(caught.value)
        assert target not in message
        assert "X-Amz-Signature" not in message
        assert "evil.corp.example" not in message
        assert "https" in message, "the scheme still distinguishes a downgrade from a move"


class TestARejectedPushIsNotLeftAsTheLastKnownGood:
    """The publish is confirmed BEFORE the install, so the bytes are on disk before
    ``apply_ceiling`` has had its say — and that step refuses for reasons the earlier
    validation cannot see (a profile, the trust root, or a tier-1 pin that moved between
    the two). This module promises a document failing a mandated check never becomes what
    the next boot adopts, and every cache-only child adopts it too.
    """

    def test_the_previous_document_is_restored_when_the_install_refuses(
        self, transport, install_ceiling, monkeypatch
    ):
        source = transport(_static_fetcher(pd.FetchedPolicy(body=_body("pushed"), etag="v2")))
        running = install_ceiling(
            governance.parse_policy(_doc("running", distribution={"source": source}))
        )
        pd.write_cache(_body("good"), source=source, etag="v1")

        # Refuse at the INSTALL, after the publish has already happened.
        monkeypatch.setattr(
            pd, "apply_ceiling", lambda c: (_ for _ in ()).throw(RuntimeError("profile floor"))
        )
        outcome = pd.refresh_now()

        assert outcome.status == pd.REFRESH_REJECTED
        assert current_context().governance is running
        cached = pd.read_cache()
        assert cached is not None
        assert cached.body == _body(
            "good"
        ), "a rejected push must not survive as the document the next boot adopts"

    def test_the_restored_age_is_the_last_successful_fetch_not_this_failure(
        self, transport, install_ceiling, monkeypatch
    ):
        """Otherwise a failing refresh would keep resetting the staleness clock, and a
        fleet's ``max_cache_age_secs`` would never notice a source that stopped working."""
        source = transport(_static_fetcher(pd.FetchedPolicy(body=_body("pushed"), etag="v2")))
        install_ceiling(governance.parse_policy(_doc("running", distribution={"source": source})))
        pd.write_cache(_body("good"), source=source, etag="v1", now=time.time() - 5_000)

        monkeypatch.setattr(
            pd, "apply_ceiling", lambda c: (_ for _ in ()).throw(RuntimeError("profile floor"))
        )
        pd.refresh_now()

        cached = pd.read_cache()
        assert cached is not None and cached.body == _body("good")
        assert cached.age_secs() > 4_000, "the restore must not restart the staleness clock"

    def test_with_nothing_to_restore_the_rejected_bytes_are_REMOVED(
        self, transport, install_ceiling, monkeypatch
    ):
        """The first-refresh case, and the answer is deletion rather than "leave it".

        Leaving them is not the same trade-off as keeping a stale-but-composable copy:
        these are the bytes this host just REFUSED. Kept, the next boot serves them from
        cache instead of re-fetching, so a source the administrator has since corrected
        does not reach this host until the window expires — and a cache-only child adopts
        the refused ceiling meanwhile. With no cache, boot fetches: it either gets the fix
        or fails exactly as it would have anyway.
        """
        source = transport(_static_fetcher(pd.FetchedPolicy(body=_body("pushed"), etag="v2")))
        running = install_ceiling(
            governance.parse_policy(_doc("running", distribution={"source": source}))
        )
        assert pd.read_cache() is None, "the premise: nothing cached for this source yet"
        monkeypatch.setattr(
            pd, "apply_ceiling", lambda c: (_ for _ in ()).throw(RuntimeError("profile floor"))
        )
        outcome = pd.refresh_now()

        assert outcome.status == pd.REFRESH_REJECTED
        assert current_context().governance is running
        assert pd.read_cache() is None, "a refused document must not be the last-known-good"

    def test_a_cache_from_another_source_is_not_a_rollback_target(
        self, transport, install_ceiling, monkeypatch
    ):
        """Restoring it would re-cache a retired endpoint's document under the new
        endpoint's name, which is what the repoint rule exists to refuse."""
        source = transport(_static_fetcher(pd.FetchedPolicy(body=_body("pushed"), etag="v2")))
        install_ceiling(governance.parse_policy(_doc("running", distribution={"source": source})))
        pd.write_cache(_body("retired"), source="https://retired.corp.example/p.json", etag="v0")

        monkeypatch.setattr(
            pd, "apply_ceiling", lambda c: (_ for _ in ()).throw(RuntimeError("profile floor"))
        )
        pd.refresh_now()

        # Not restored, and not left in place either: a retired endpoint's document is not
        # this source's last-known-good, so there is nothing to roll back TO and the
        # refused bytes go the same way as in the no-prior-copy case above.
        cached = pd.read_cache()
        assert cached is None or cached.body != _body(
            "retired"
        ), "a retired source's copy must never become this source's last-known-good"


class TestTheBootReRaiseIsSanitised:
    @pytest.mark.parametrize(
        "fetcher",
        [
            _static_fetcher(pd.FetchedPolicy(body=b"")),
            _failing_fetcher(PlatformCompositionError("policy source names scheme 'htps'")),
        ],
        ids=["empty-document", "config-error"],
    )
    def test_a_configuration_error_does_not_name_the_endpoint(self, transport, fetcher):
        """A boot abort's text reaches stderr and any supervisor capturing it.

        The module's rule is that the URL is emitted nowhere, and a re-raise is not an
        exception to it.
        """
        source = transport(fetcher)
        with pytest.raises(PlatformCompositionError) as caught:
            pd.load_distributed_policy(governance.PolicyDistribution(source=source))
        assert source not in str(caught.value)
        assert "policy.example" not in str(caught.value)


class TestTheCacheIsHiddenFromAgentSubprocesses:
    """``is_sensitive_path`` gates the agent's TOOL CALLS, not an OS ``open()``.

    A spawned ``python -c`` never routes through that gate, so the cache is
    bind-mount-hidden in every sandbox tier as well. It matters more here than for the
    policy FILE this cache copies: on a fleet using the environment channel there is no
    ``security_policy.json`` on disk at all, so the cache is the only on-disk copy of
    the ceiling.
    """

    @pytest.mark.parametrize("prefix", [".kiro/crew", ".kirocrew"])
    def test_every_sandbox_tier_masks_the_cache_directory(self, prefix):
        from kiro_crew import sandbox

        entry = f"{prefix}/{pd.CACHE_DIR_LEAF}"
        for name, dirs in (
            ("strict", sandbox._STRICT_DIRS),
            ("standard", sandbox._STANDARD_DIRS),
            ("cc", sandbox._CC_DIRS),
        ):
            assert entry in dirs, f"{entry} is not hidden in the {name} sandbox tier"

    def test_the_leaf_matches_the_engine(self):
        from kiro_crew import sandbox

        assert sandbox._POLICY_CACHE_LEAF == pd.CACHE_DIR_LEAF

    def test_a_relocated_data_home_is_masked_by_its_resolved_path(self, monkeypatch, tmp_path):
        """Those entries are ``$HOME``-relative, and ``KIROCREW_HOME`` moves the cache.

        The limitation is pre-existing and shared with the vault entries, but this one
        directory must not inherit it: on an env-channel fleet the cache is the ONLY
        on-disk copy of the ceiling, and its metadata records the source the next boot
        trusts.
        """
        from kiro_crew import sandbox

        relocated = tmp_path / "relocated-home"
        monkeypatch.setenv("KIROCREW_HOME", str(relocated))
        masked = sandbox._relocated_policy_cache_dirs()
        assert masked == [os.path.realpath(relocated / pd.CACHE_DIR_LEAF)]

    def test_the_default_layout_adds_no_duplicate_rule(self, monkeypatch, tmp_path):
        """The ``$HOME``-relative entries already cover it there.

        Compared with ``normpath`` rather than ``realpath``: this runs on the event loop
        for every async spawn, and a link-resolving syscall on a stalled NFS home would
        freeze the gateway. So the de-duplication holds where the two spellings match
        textually — which is the ordinary case, including dot segments — and a symlinked
        home is handled by the test below instead.
        """
        from kiro_crew import sandbox

        # A REAL (non-symlinked) home, so the test states the de-duplication contract
        # rather than whatever the CI host's home happens to be. ``config_dir()`` resolves
        # links internally, so on a symlinked home the two sides can never compare equal —
        # that case is the test below.
        home = tmp_path / "home"
        (home / ".kiro" / "crew").mkdir(parents=True)
        monkeypatch.setattr(sandbox.Path, "home", staticmethod(lambda: home))

        monkeypatch.setenv("KIROCREW_HOME", str(home / ".kiro" / "crew"))
        assert sandbox._relocated_policy_cache_dirs() == []

        # normpath still collapses the spellings a path can differ by without a syscall.
        monkeypatch.setenv("KIROCREW_HOME", str(home / ".kiro" / "." / "crew"))
        assert sandbox._relocated_policy_cache_dirs() == []

    def test_a_symlinked_home_costs_a_redundant_rule_never_a_missing_one(
        self, monkeypatch, tmp_path
    ):
        """The one-directional cost of dropping ``realpath``.

        Where the home is a symlink the two spellings no longer compare equal, so a
        default layout reports as relocated and the resolved path is masked IN ADDITION
        to the ``$HOME``-relative one. Redundant coverage of a directory that must be
        masked either way — the comparison was only ever de-duplication, so it cannot
        produce a gap.
        """
        from kiro_crew import sandbox

        real = tmp_path / "real-home"
        (real / ".kiro" / "crew").mkdir(parents=True)
        link = tmp_path / "linked-home"
        link.symlink_to(real)

        monkeypatch.setattr(sandbox.Path, "home", staticmethod(lambda: link))
        monkeypatch.setenv("KIROCREW_HOME", str(real / ".kiro" / "crew"))

        extra = sandbox._relocated_policy_cache_dirs()
        assert extra == [os.path.normpath(str(real / ".kiro" / "crew" / pd.CACHE_DIR_LEAF))]
        # And the $HOME-relative spelling is still covered by the dir lists themselves.
        assert f".kiro/crew/{pd.CACHE_DIR_LEAF}" in sandbox._STANDARD_DIRS


@pytest.mark.skipif(
    sys.platform == "win32",
    reason=(
        "asserts on the Linux namespace launcher and the macOS seatbelt profile, neither "
        "of which Windows has -- and ``_build_launcher_script`` calls POSIX-only "
        "os.getuid. Guarded rather than listed in windows-expected-failures.txt: that "
        "list is a burn-down backlog, and an OS sandbox Windows does not implement is a "
        "permanent boundary. Same route as test_sandbox_mount_checked.py (#2041)."
    ),
)
class TestAnExposedCacheIsStillReadOnly:
    """``extra_visible_dirs`` cancels a target's whole rule set — including the WRITE deny.

    ``apps/backend.py`` has to expose the cache (cache-only mode resolves the fleet
    ceiling from it and fails closed without it), and an app backend is arbitrary
    third-party code. Since the metadata records the source the next boot trusts, a
    writable exposure lets an app pick the ceiling for every later boot on the host —
    so the read-only seal is a property of the DIRECTORY, decided in ``sandbox``, and a
    caller cannot re-open it by passing the path.
    """

    @staticmethod
    def _cache_path():
        from pathlib import Path as _Path

        return str(_Path.home() / ".kiro" / "crew" / pd.CACHE_DIR_LEAF)

    def test_the_linux_launcher_binds_it_read_only_instead_of_hiding_it(self):
        from kiro_crew import sandbox

        cache = self._cache_path()
        script = sandbox._build_launcher_script("standard", extra_visible_dirs=(cache,))
        readonly = json.loads(script.split("READONLY_DIRS = ", 1)[1].split("\n", 1)[0])
        hidden = json.loads(script.split("SENSITIVE_DIRS = ", 1)[1].split("\n", 1)[0])

        assert cache in readonly, "an exposed cache must be bound read-only, not merely unhidden"
        assert cache not in hidden, "it also has to be READABLE — that is why it was exposed"

    def test_the_seal_is_a_remount_because_ms_rdonly_is_ignored_on_a_bind(self):
        """Both mount calls are load-bearing: the bind alone grants write."""
        from kiro_crew import sandbox

        script = sandbox._build_launcher_script(
            "standard", extra_visible_dirs=(self._cache_path(),)
        )
        loop = script.split("for d in READONLY_DIRS:", 1)[1].split("\n\n", 1)[0]

        assert "_MS_REMOUNT | _MS_BIND | _MS_RDONLY" in loop
        assert loop.count("_mount_or_die(") == 2

    def test_an_unexposed_cache_gets_no_read_only_rule(self):
        """The ordinary spawn hides it; only the protected runtime parent is read-only."""
        from kiro_crew import sandbox

        script = sandbox._build_launcher_script("standard")
        readonly = json.loads(script.split("READONLY_DIRS = ", 1)[1].split("\n", 1)[0])
        assert set(readonly) >= set(sandbox._voice_runtime_parent_paths())
        assert self._cache_path() not in readonly

    def test_macos_keeps_the_write_and_link_denies_when_it_drops_the_read_deny(self):
        from kiro_crew import sandbox

        cache = self._cache_path()
        profile = sandbox._build_seatbelt_profile("standard", extra_visible_dirs=(cache,))

        assert f'(deny file-write* (subpath "{cache}"))' in profile
        assert f'(deny file-link (subpath "{cache}"))' in profile
        assert f'(deny file-read* (subpath "{cache}"))' not in profile

    def test_another_exposed_dir_is_not_sealed(self):
        """Only the governance cache. A blanket write deny has its own blast radius —
        one on ``.aws`` would break a tool legitimately refreshing a cached token."""
        from pathlib import Path as _Path

        from kiro_crew import sandbox

        aws = str(_Path.home() / ".aws")
        profile = sandbox._build_seatbelt_profile("strict", extra_visible_dirs=(aws,))
        assert f'(deny file-write* (subpath "{aws}"))' not in profile

        script = sandbox._build_launcher_script("strict", extra_visible_dirs=(aws,))
        readonly = json.loads(script.split("READONLY_DIRS = ", 1)[1].split("\n", 1)[0])
        assert set(readonly) >= set(sandbox._voice_runtime_parent_paths())
        assert aws not in readonly

    @pytest.mark.parametrize("prefix", [".kiro/crew", ".kirocrew"])
    def test_it_holds_for_every_spelling_the_dir_lists_carry(self, prefix):
        """Including the legacy ``~/.kirocrew`` entry the deny lists must keep covering."""
        from pathlib import Path as _Path

        from kiro_crew import sandbox

        path = str(_Path.home() / prefix / pd.CACHE_DIR_LEAF)
        assert sandbox._is_policy_cache_dir(path)
        assert f'(deny file-write* (subpath "{path}"))' in sandbox._build_seatbelt_profile(
            "standard", extra_visible_dirs=(path,)
        )

    def test_a_sibling_directory_is_not_mistaken_for_the_cache(self):
        from kiro_crew import sandbox

        assert not sandbox._is_policy_cache_dir("/home/u/.kiro/crew")
        assert not sandbox._is_policy_cache_dir("/home/u/.kiro/crew/policy_cache_backup")
        assert sandbox._is_policy_cache_dir("/home/u/.kiro/crew/policy_cache/")


class TestAnUnverifiableSourceIsTreatedAsWritable:
    """Off POSIX the mode bits carry no answer, and "cannot tell" has to round to unsafe.

    The caller REFUSES a writable source, so a ``False`` here would not abstain — it
    would assert the source is safe and admit every Windows ``file://`` source
    unchecked, including one an agent just planted.
    """

    def test_the_stat_predicate_fails_closed_off_posix(self, monkeypatch, tmp_path):
        """A read-only file, so only the off-POSIX guard can produce the True."""
        target = tmp_path / "policy.json"
        target.write_text("{}", encoding="utf-8")
        target.chmod(0o444)
        monkeypatch.setattr(platform_compat, "IS_POSIX", False)
        assert platform_compat.stat_writable_by_current_user(os.stat(target)) is True

    def test_the_ancestor_walk_fails_closed_off_posix(self, monkeypatch, tmp_path):
        target = tmp_path / "policy.json"
        target.write_text("{}", encoding="utf-8")
        monkeypatch.setattr(platform_compat, "IS_POSIX", False)
        assert platform_compat.path_writable_by_current_user(target) is True

    @_POSIX_IDS_ONLY
    def test_our_primary_group_counts_even_when_getgroups_omits_it(self, monkeypatch):
        """POSIX leaves it unspecified whether the effective gid is in the SUPPLEMENTARY
        list. A process that reached its gid through ``setegid``, or one in a container
        built without ``initgroups``, has a primary group ``getgroups()`` never mentions —
        so a membership test alone calls a group-writable file we CAN replace safe."""
        monkeypatch.setattr(platform_compat.os, "getgroups", lambda: [])
        monkeypatch.setattr(platform_compat.os, "getegid", lambda: 4242)
        monkeypatch.setattr(platform_compat.os, "getgid", lambda: 4242)
        st = os.stat_result((stat.S_IFREG | 0o664, 0, 0, 1, os.getuid() + 1, 4242, 0, 0, 0, 0))
        assert platform_compat.stat_writable_by_current_user(st) is True

    @_POSIX_IDS_ONLY
    def test_the_real_gid_counts_too(self, monkeypatch):
        """A process holding a real gid can regain it, so the union is the honest answer."""
        monkeypatch.setattr(platform_compat.os, "getgroups", lambda: [])
        monkeypatch.setattr(platform_compat.os, "getegid", lambda: 1)
        monkeypatch.setattr(platform_compat.os, "getgid", lambda: 4243)
        st = os.stat_result((stat.S_IFREG | 0o664, 0, 0, 1, os.getuid() + 1, 4243, 0, 0, 0, 0))
        assert platform_compat.stat_writable_by_current_user(st) is True

    @_POSIX_IDS_ONLY
    def test_a_group_we_are_not_in_is_still_not_writable(self, monkeypatch):
        """The control has to stay a control: a real read-only source must pass."""
        monkeypatch.setattr(platform_compat.os, "getgroups", lambda: [7, 8])
        monkeypatch.setattr(platform_compat.os, "getegid", lambda: 9)
        monkeypatch.setattr(platform_compat.os, "getgid", lambda: 9)
        st = os.stat_result((stat.S_IFREG | 0o664, 0, 0, 1, os.getuid() + 1, 4244, 0, 0, 0, 0))
        assert platform_compat.stat_writable_by_current_user(st) is False

    @_POSIX_IDS_ONLY
    def test_the_effective_uid_counts_too(self, monkeypatch):
        monkeypatch.setattr(platform_compat.os, "getuid", lambda: 1000)
        monkeypatch.setattr(platform_compat.os, "geteuid", lambda: 2000)
        st = os.stat_result((stat.S_IFREG | 0o644, 0, 0, 1, 2000, 0, 0, 0, 0, 0))
        assert platform_compat.stat_writable_by_current_user(st) is True


class TestABootOnlySourceIsNotRefetched:
    """``refresh_interval_secs: 0`` means boot-only, and the loader path must honour it.

    ``load_security_policy`` is re-run per app callback by ``mcp_gateway/app_call.py``, and
    ``_fetch_window`` floors a zero interval at ``MIN_REFRESH_INTERVAL_SECS`` so it never
    reads as "fetch every call". But a floor is still a cadence: an operator who froze the
    ceiling for the process lifetime got a 60-second poller anyway, one that could hand an
    app callback a LOOSENED document while the gateway kept the ceiling it booted on.
    """

    def test_boot_fetches_once_and_later_callers_do_not(self, transport, monkeypatch):
        calls: list[str] = []

        def counting(request):
            calls.append(request.url)
            return pd.FetchedPolicy(body=_body("central"))

        source = transport(counting)
        declared = governance.PolicyDistribution.from_dict(
            {"source": source, "refresh_interval_secs": 0}
        )
        assert pd.load_distributed_policy(declared) is not None
        assert len(calls) == 1, "boot itself must still establish the ceiling"

        # Age the cache well past the 60s floor `_fetch_window` clamps a zero interval
        # to, and clear the cooldown as it would have expired by then. Without this the
        # ordinary freshness shortcut would carry the test and the freeze would be
        # unobserved. No `max_cache_age_secs` is declared, so no staleness bound applies.
        meta = pd.read_cache_meta()
        assert meta is not None
        pd.touch_cache(meta, now=time.time() - 100_000)
        monkeypatch.setattr(pd, "_claim_fetch_slot", lambda window: True)
        for _ in range(3):
            assert pd.load_distributed_policy(declared) is not None
        assert len(calls) == 1, "a boot-only source must not be re-fetched after boot"

    def test_a_configured_interval_still_refetches(self, transport, monkeypatch):
        """The freeze is specific to a zero interval; an ordinary cadence is unchanged."""
        calls: list[str] = []

        def counting(request):
            calls.append(request.url)
            return pd.FetchedPolicy(body=_body("central"))

        source = transport(counting)
        declared = governance.PolicyDistribution.from_dict(
            {"source": source, "refresh_interval_secs": 900}
        )
        assert pd.load_distributed_policy(declared) is not None
        monkeypatch.setattr(pd, "_claim_fetch_slot", lambda window: True)
        # Age the cache past the window so the shortcut cannot apply.
        meta = pd.read_cache_meta()
        assert meta is not None
        pd.touch_cache(meta, now=time.time() - 100_000)
        assert pd.load_distributed_policy(declared) is not None
        assert len(calls) == 2

    def test_the_staleness_bound_still_applies_in_boot_only_mode(self, transport, monkeypatch):
        """Not re-fetching is not the same as trusting bytes of any age."""
        calls: list[str] = []

        def counting(request):
            calls.append(request.url)
            return pd.FetchedPolicy(body=_body("central"))

        source = transport(counting)
        declared = governance.PolicyDistribution.from_dict(
            {"source": source, "refresh_interval_secs": 0, "max_cache_age_secs": 300}
        )
        assert pd.load_distributed_policy(declared) is not None
        assert len(calls) == 1

        meta = pd.read_cache_meta()
        assert meta is not None
        pd.touch_cache(meta, now=time.time() - 10_000)
        # Past max_cache_age_secs, so the cache is no longer an acceptable answer even in
        # boot-only mode: the tier goes back out rather than serving it. (The fetch
        # cooldown is cleared, since by that age it would have long expired.)
        monkeypatch.setattr(pd, "_claim_fetch_slot", lambda window: True)
        assert pd.load_distributed_policy(declared) is not None
        assert len(calls) == 2, "a cache past the staleness bound must not be served"

    def test_a_file_source_is_therefore_refused_off_posix(self, monkeypatch, tmp_path):
        """The end-to-end consequence: the transport declines rather than trusting it."""
        monkeypatch.setattr(platform_compat, "IS_POSIX", False)
        monkeypatch.setattr(pd, "IS_POSIX", False, raising=False)
        doc = tmp_path / "policy.json"
        doc.write_text(json.dumps(_doc("central")))
        os.chmod(doc, 0o444)

        with pytest.raises(Exception) as excinfo:
            pd._fetch_file(pd.FetchRequest(url=doc.as_uri()))
        assert "writ" in str(excinfo.value).lower()


class TestTheCachePairIsWrittenUnderOneLock:
    """Each file is atomic; the PAIR is not, and two writers is the ordinary case.

    A forced ``write_cache`` interleaving with another process's 304 ``touch_cache``
    persists NEW bytes against the OLD digest that the toucher read beforehand.
    Readers detect that and discard the cache as torn, so the cost is the
    last-known-good copy vanishing during the outage that made the refresh fail.
    """

    def test_both_writers_serialise_on_the_same_lock_file(self, tmp_path, monkeypatch):
        taken: list[str] = []
        real = pd._cache_write_lock

        @contextlib.contextmanager
        def spy(directory):
            taken.append(str(directory))
            with real(directory):
                yield

        monkeypatch.setattr(pd, "_cache_write_lock", spy)
        body = _body("central")
        pd.write_cache(body, source="https://example.invalid/p.json", etag="v1")
        meta = pd.read_cache_meta()
        assert meta is not None
        pd.touch_cache(meta, etag="v2")

        assert len(taken) == 2, "both the pair write and the metadata touch must lock"
        assert taken[0] == taken[1], "and on the SAME lock, or they do not serialise"

    def test_an_interleaved_touch_cannot_pair_an_old_digest_with_new_bytes(self):
        """The race, run in the order that used to corrupt the pair.

        The toucher's ``meta`` is captured BEFORE the new document is written, which is
        exactly what a 304 poll holds while a forced fetch lands underneath it. The lock
        alone does not help — these two calls never overlap, and the stale value was read
        before either took it — so what has to save the cache is the compare-and-swap
        INSIDE the lock: the touch is skipped because the metadata no longer describes the
        document this caller validated.
        """
        source = "https://example.invalid/p.json"
        pd.write_cache(_body("first"), source=source, etag="v1")
        stale_meta = pd.read_cache_meta()
        assert stale_meta is not None

        pd.write_cache(_body("second"), source=source, etag="v2")
        pd.touch_cache(stale_meta)

        cached = pd.read_cache()
        assert cached is not None, "the stale touch discarded a perfectly good cache"
        assert cached.body == _body("second"), "the newer document must survive"
        assert cached.meta().digest == pd._body_digest(cached.body)
        assert cached.meta().etag == "v2", "and so must the writer's own validators"

    def test_a_touch_still_restarts_the_age_when_nothing_moved(self):
        """The compare-and-swap must not disable the ordinary 304 path.

        A fleet with a stable policy and a staleness bound depends on this: without the
        age restarting, it would refuse to boot for having confirmed nothing changed.
        """
        source = "https://example.invalid/p.json"
        pd.write_cache(_body("stable"), source=source, etag="v1", now=time.time() - 5_000)
        aged = pd.read_cache()
        assert aged is not None and aged.age_secs() > 4_000

        meta = pd.read_cache_meta()
        assert meta is not None
        pd.touch_cache(meta, etag="v1")

        refreshed = pd.read_cache()
        assert refreshed is not None
        assert refreshed.age_secs() < 60, "an unchanged document's age must restart"

    @pytest.mark.skipif(
        not hasattr(os, "symlink") or os.name == "nt",
        reason=(
            "planting the link needs SeCreateSymbolicLinkPrivilege, which the Windows CI "
            "account does not hold. The guard's Windows arm is "
            "platform_compat.is_link_or_junction, which has its own coverage."
        ),
    )
    def test_the_lock_file_is_neither_leaf_nor_a_followed_link(self, tmp_path):
        """It carries no content, so it must not be readable as a policy — and a planted
        link must not get its target truncated by the next write."""
        assert pd._CACHE_LOCK_LEAF not in (pd._CACHE_DOC_LEAF, pd._CACHE_META_LEAF)

        directory = pd.cache_dir()
        directory.mkdir(parents=True, exist_ok=True)
        victim = tmp_path / "victim"
        victim.write_text("do not truncate me")
        (directory / pd._CACHE_LOCK_LEAF).symlink_to(victim)

        with pytest.raises(OSError):
            with pd._cache_write_lock(directory):
                pass
        assert victim.read_text() == "do not truncate me"

    def test_a_write_that_cannot_lock_keeps_the_existing_good_copy(self, monkeypatch):
        """Proceeding unlocked could tear a good pair; skipping preserves it."""
        pd.write_cache(_body("good"), source="https://example.invalid/p.json", etag="v1")
        before = pd.read_cache()
        assert before is not None

        @contextlib.contextmanager
        def refuse(_directory):
            raise OSError("no lock for you")
            yield  # pragma: no cover

        monkeypatch.setattr(pd, "_cache_write_lock", refuse)
        pd.write_cache(_body("clobbered"), source="https://example.invalid/p.json", etag="v2")

        after = pd.read_cache()
        assert after is not None
        assert after.body == before.body, "a lock failure must not destroy the cache"


class TestA304RevalidatesAgainstTheTrustRoot:
    def test_a_newly_mandated_signature_is_caught_without_a_new_document(
        self, transport, install_ceiling, monkeypatch
    ):
        """ "Unchanged" is a statement about the DOCUMENT.

        The trust root is a separate input that moves on its own schedule: a fleet turning
        on ``require_policy_signature`` makes the running ceiling untrusted without the
        endpoint publishing anything, and a 304 would otherwise let it stand indefinitely.
        """
        source = transport(_static_fetcher(pd.FetchedPolicy(etag="v1", not_modified=True)))
        body = _body("running", distribution={"source": source})
        install_ceiling(governance.parse_policy(_doc("running", distribution={"source": source})))
        pd.write_cache(body, source=source, etag="v1")
        pd._record_installed(body)
        assert pd.refresh_now().status == pd.REFRESH_UNCHANGED

        # The fleet now mandates provenance. Nothing was published.
        monkeypatch.setattr(governance, "_policy_signature_required", lambda: True)
        outcome = pd.refresh_now()

        assert outcome.status == pd.REFRESH_REJECTED
        assert "trust root" in outcome.detail


class TestKeystone:
    def test_the_boot_check_passes(self):
        governance.assert_governance_paths_protected()

    def test_dropping_the_cache_entry_fails_the_boot_check(self, monkeypatch):
        """The entry is what the guarantee rests on, so its loss must be loud.

        An agent able to write the cache would not need to touch
        ``security_policy.json`` to replace its own ceiling — it would publish
        itself one, with the recorded provenance to make the next boot adopt it.
        """
        monkeypatch.setattr(
            security,
            "_SENSITIVE_HOME_DIRS",
            [d for d in security._SENSITIVE_HOME_DIRS if not d.endswith(pd.CACHE_DIR_LEAF)],
        )
        with pytest.raises(PlatformCompositionError, match=pd.CACHE_DIR_LEAF):
            governance.assert_governance_paths_protected()

    @pytest.mark.parametrize("path", _CACHE_PATHS)
    def test_the_cache_is_read_and_write_blocked_under_both_home_prefixes(self, path):
        """Read matters as much as write: the cache is a verbatim copy of the policy.

        Leaving it readable would hand over the exact deny patterns an evasion
        needs, which is why the entry gates the whole directory rather than a file.
        """
        assert security.is_sensitive_path(path)
        assert validate_file_path(path) is None


# ──────────────────────────────────────────────────────────────────────────
# Read-only posture
# ──────────────────────────────────────────────────────────────────────────


class TestPosture:
    def test_an_unconfigured_host_reports_nothing_configured(self):
        posture = pd.distribution_posture()
        assert posture["configured"] is False
        assert posture["error_code"] == ""
        assert posture["source_scheme"] == ""

    def test_the_url_appears_nowhere_in_the_serialized_posture(self, install_ceiling):
        """The dashboard is reachable by the agent's own browser tooling.

        The endpoint is the fleet's control plane, so naming it would tell a
        prompt-injected agent exactly where to aim. The scheme alone answers what
        an operator asks: is this host centrally governed, and is it encrypted.
        """
        host = "config.corp.example"
        source = f"https://{host}/kirocrew/policy.json"
        install_ceiling(
            governance.parse_policy(
                _doc(
                    "running",
                    distribution={
                        "source": source,
                        "refresh_interval_secs": 900,
                        "max_cache_age_secs": 86400,
                    },
                )
            )
        )
        posture = pd.distribution_posture()
        assert posture["configured"] is True
        assert posture["source_scheme"] == "https"
        assert posture["refresh_interval_seconds"] == 900
        assert posture["max_cache_age_seconds"] == 86400
        assert posture["error_code"] == ""
        serialized = json.dumps(posture)
        assert host not in serialized
        assert source not in serialized

    def test_a_misconfigured_source_reports_a_code_and_still_no_url(self, monkeypatch):
        """A status panel must not raise, and an exception's prose leaks the URL."""
        host = "config.corp.example"
        monkeypatch.setenv(pd.POLICY_URL_ENV, f"https://{host}/kirocrew/policy.json")
        monkeypatch.setenv(pd.POLICY_REFRESH_ENV, "15m")
        posture = pd.distribution_posture()
        assert posture["error_code"] == pd.POSTURE_ERROR_MISCONFIGURED
        assert posture["configured"] is False
        assert host not in json.dumps(posture)
