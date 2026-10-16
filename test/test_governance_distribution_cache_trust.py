"""Policy distribution: the last-known-good cache as a trust root on disk.

A cache-only child adopts the cache without the control plane, and fails closed when
the cache is missing, unusable or past the staleness bound, wherever that bound is
declared; a torn copy is not believed, nor, under a signature mandate, a forged one;
the cache records which source a copy came from as an identity rather than the URL,
one a rotated signature keeps and a real repoint changes; the document and its
metadata are written under one lock; and the agent can neither read nor write the
cache, while an app backend that is shown it gets it read-only.

One topic of the ``test_governance_distribution*.py`` family; the constants, document
builders, fake fetchers and fixtures the family shares come from
``governance_distribution_helpers.py``. Nothing here touches the network either: see
``test_governance_distribution.py``'s docstring for how the transport is faked.
"""

from __future__ import annotations

import contextlib
import json
import os
import sys
import time

import pytest
from governance_distribution_helpers import _hermetic_governance_globals  # noqa: F401
from governance_distribution_helpers import install_ceiling_fixture  # noqa: F401
from governance_distribution_helpers import transport_fixture  # noqa: F401
from governance_distribution_helpers import (
    _PAST_THE_WINDOW,
    _TEST_SOURCE,
    _body,
    _cache,
    _doc,
    _failing_fetcher,
    _static_fetcher,
    _write_policy,
)

from kiro_crew import security
from kiro_crew.hooks import validate_file_path
from kiro_crew.platform import governance
from kiro_crew.platform import policy_distribution as pd
from kiro_crew.platform.context import PlatformCompositionError

# ──────────────────────────────────────────────────────────────────────────
# Keystone — the cache is a trust root
# ──────────────────────────────────────────────────────────────────────────


_CACHE_PATHS = [
    f"~/{prefix}/{pd.CACHE_DIR_LEAF}/{leaf}"
    for prefix in (".kiro/crew", ".kirocrew")
    for leaf in (pd._CACHE_DOC_LEAF, pd._CACHE_META_LEAF)
]


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
        monkeypatch.setattr(
            governance, "_policy_trust_settings", lambda *_a: (True, {issuer: key}, {})
        )

        ceiling = pd.parse_distributed_policy(body, source=_TEST_SOURCE)
        assert ceiling.signature_state == governance.SIGNATURE_VERIFIED


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

        Where the home is a symlink the two spellings do not compare equal, so a
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

    @pytest.fixture(autouse=True)
    # nosemgrep: kirocrew.test-autouse-shared-monkeypatch -- the class's in-process ssh probe, moved verbatim; tracked for a de-flake pass
    def _pinned_ssh_probe(self, monkeypatch):
        """Answer the launcher's ``ssh -V`` probe in-process.

        A namespace plan consults ``_ssh_supports_accept_new`` for the
        ``StrictHostKeyChecking`` flag, and the real probe spawns the host's
        ``ssh``. It is memoised, so it fires in whichever test on the worker
        first builds a plan — these tests read the mount lists, not the ssh
        flag, so the seam is pinned rather than left to find the host binary.
        """
        from kiro_crew import sandbox

        monkeypatch.setattr(sandbox, "_ssh_supports_accept_new", lambda: True)

    @staticmethod
    def _cache_path():
        from pathlib import Path as _Path

        return str(_Path.home() / ".kiro" / "crew" / pd.CACHE_DIR_LEAF)

    def test_the_linux_launcher_binds_it_read_only_instead_of_hiding_it(self):
        from kiro_crew import sandbox

        cache = self._cache_path()
        plan = sandbox._spawn_plan("namespace", "standard", extra_visible_dirs=(cache,))

        assert (
            cache in plan.readonly
        ), "an exposed cache must be bound read-only, not merely unhidden"
        assert (
            cache not in plan.sensitive_dirs
        ), "it also has to be READABLE — that is why it was exposed"

    @pytest.mark.skipif(
        not sys.platform.startswith("linux"), reason="the namespace launcher is Linux-only"
    )
    def test_the_seal_is_a_remount_because_ms_rdonly_is_ignored_on_a_bind(self, tmp_path):
        """Both mount calls are load-bearing: the bind alone grants write.

        The launcher program's seal stage runs over a read-only entry with a stand-in
        libc: it binds the path over itself, then remounts that bind ``MS_RDONLY``,
        and a refusal of EITHER mount refuses the spawn rather than running unsealed.
        """
        from test_sandbox_launcher_program import RecordingLibc, launch, payload, refusal

        from kiro_crew import sandbox_launcher_program as program

        cache = tmp_path / pd.CACHE_DIR_LEAF
        cache.mkdir()
        libc = RecordingLibc()
        program.seal_readonly(launch(tmp_path, payload(readonly_dirs=[str(cache)]), libc=libc))

        assert [call.target_path for call in libc.calls] == [str(cache), str(cache)]
        bind, remount = (call.flags for call in libc.calls)
        assert bind == program._MS_BIND
        sealed = program._MS_REMOUNT | program._MS_BIND | program._MS_RDONLY
        assert remount & sealed == sealed

        for fail_at, step in ((1, "exposing"), (2, "sealing")):
            failing = RecordingLibc(fail_at=fail_at)
            run = launch(tmp_path, payload(readonly_dirs=[str(cache)]), libc=failing)
            message = refusal(program.seal_readonly, run)
            assert message is not None, f"a refused {step} mount let the spawn run"
            assert message.startswith(f"sandbox: BLOCKED -- {step} read-only path {cache} failed")

    def test_an_unexposed_cache_gets_no_read_only_rule(self):
        """The ordinary spawn hides it; only the protected runtime parent is read-only."""
        from kiro_crew import sandbox

        readonly = sandbox._spawn_plan("namespace", "standard").readonly
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

        readonly = sandbox._spawn_plan("namespace", "strict", extra_visible_dirs=(aws,)).readonly
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
        """The race, run in the order that would corrupt the pair without the lock.

        The toucher's ``meta`` is captured BEFORE the new document is written, which is
        exactly what a 304 poll holds while a forced fetch lands underneath it. The lock
        alone does not help — these two calls never overlap, and the stale value was read
        before either took it — so what has to save the cache is the compare-and-swap
        INSIDE the lock: the touch is skipped because the metadata does not describe the
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
