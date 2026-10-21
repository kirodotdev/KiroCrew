"""The owner-tier store-art prewarm: what it fetches, where it puts it, what it refuses.

An ``owner``-tier registry lists apps in private repositories, so the credential-free
browse-time fetch can never show their icon, hero or screenshots before install.
``store_art._prewarm_owner_tier_store_assets`` fetches them once, with owner
credentials, on the rows a FRESH index fetch just returned, and writes the manifest
and blob caches the store then reads. These tests pin:

- the trust boundary: no clone for an ``index``-tier registry, for a row tagged for
  another registry, for an untrusted host, or for a row read from the cache;
- the cache contract: the bytes land at exactly the path the blob proxy computes for
  the URL ``_merge_manifest`` emits, so a later browse is a cache hit;
- the art gates: a hostile ``app.json`` cannot make the prewarm read or cache a file
  outside the clone, a non-image, a hidden path or an oversize file;
- the warm check: a warm row is skipped and a cold one refetched, rows are fetched
  concurrently but bounded, the batch budget returns what landed, and which assets are
  recorded unobtainable.

The batch's two call sites, its row and screenshot caps, its post-budget cleanup and its
cancellation live in ``test_registry_store_art_prewarm_batch.py``; the pinned reads and
publishes in ``test_registry_store_art_prewarm_pinned_io.py``; the rewarm window, the
clone-failure backoff and the blob-cache sweep in ``test_registry_store_art_prewarm_rewarm.py``.
The helpers they share live in ``registry_store_art_prewarm_helpers.py``.
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
import time
from pathlib import Path
from typing import Any

import pytest
from registry_store_art_prewarm_helpers import (
    ART,
    FORGE,
    MANIFEST,
    OTHER_HOST,
    SIBLING,
    _await_barrier,
    _entry,
    _fake_fetch,
    _pin_registry,
    _route_cache_path,
    _settled,
)

from conftest import requires_symlinks
from kiro_crew.apps import registry as reg_mod
from kiro_crew.apps.registry import (
    _TRUST_INDEX,
    _TRUST_OWNER,
    _blob_cache_dir,
    _blob_cache_key,
    _copy_declared_art,
    _manifest_cache_path,
    _merge_manifest,
    _prewarm_owner_tier_store_assets,
    _read_manifest_cache,
    _servable_art_path,
    _store_art_cache_path,
)
from kiro_crew.subprocess_utf8 import UTF8_TEXT
from kiro_crew.testing.wait import finished

# ---------------------------------------------------------------------------
# What lands where
# ---------------------------------------------------------------------------


class TestOwnerTierPrewarm:
    @pytest.mark.asyncio
    async def test_fresh_rows_warm_the_manifest_and_every_declared_art_file(self, monkeypatch):
        reg = _pin_registry(monkeypatch, _TRUST_OWNER)
        calls = _fake_fetch(monkeypatch, ART)
        entry = _entry()

        assert await _prewarm_owner_tier_store_assets(reg, [entry]) == 1

        assert len(calls) == 1
        assert calls[0]["git_url"] == SIBLING
        assert calls[0]["branch"] == "main"
        assert _read_manifest_cache(entry) == MANIFEST

        merged = _merge_manifest(entry, MANIFEST)
        urls = [merged["iconUrl"], merged["heroImage"], *merged["screenshots"]]
        assert len(urls) == len(ART)
        for url, (rel, data) in zip(urls, ART.items(), strict=True):
            assert url.startswith("/api/apps/blob?")
            cached = _route_cache_path(entry, url)
            assert cached.is_file(), f"{rel} not where the blob proxy reads it"
            assert cached.read_bytes() == data

    @pytest.mark.asyncio
    async def test_the_clone_runs_under_the_owner_posture_and_is_audited(self, monkeypatch):
        """Owner credentials + context sandbox mode, with a grant record of its own."""
        reg = _pin_registry(monkeypatch, _TRUST_OWNER)
        calls = _fake_fetch(monkeypatch, ART)
        grants: list[tuple[str, str]] = []
        monkeypatch.setattr(
            reg_mod, "_sel_credential_grant", lambda op, url: grants.append((op, url))
        )

        await _prewarm_owner_tier_store_assets(reg, [_entry()])

        (call,) = calls
        # ``anonymous_git_env`` pins a batch-mode ssh with no identity and no agent;
        # ``minimal_env`` does neither. Either marker present means the wrong env.
        assert "IdentityAgent=none" not in call["clone_env"].get("GIT_SSH_COMMAND", "")
        assert call["clone_env"].get("GIT_CONFIG_GLOBAL") != os.devnull
        assert call["sandbox_mode"] == reg_mod._context_clone_sandbox_mode(SIBLING)
        # The prewarm -- and ONLY the prewarm -- asks ``_git_fetch_branch`` to run its
        # local checkout steps with system/global git config masked: this throwaway
        # checkout is read for a few image files, so an LFS pointer in an image's
        # place merely records the asset unobtainable, whereas on the install path
        # the same masking would check out pointer files instead of content.
        assert call["mask_local_git_config"] is True
        assert grants == [("prewarm_store_art_owner_tier", SIBLING)]

    @pytest.mark.asyncio
    async def test_the_clone_asks_for_the_agent_socket(self, monkeypatch):
        """The owner-tier clone keeps the owner's ssh agent, as the install clone does."""
        reg = _pin_registry(monkeypatch, _TRUST_OWNER)
        calls = _fake_fetch(monkeypatch, ART)

        await _prewarm_owner_tier_store_assets(reg, [_entry()])

        (call,) = calls
        assert call["forward_ssh_auth_sock"] is True

    @pytest.mark.asyncio
    async def test_a_failed_fetch_leaves_no_manifest_behind(self, monkeypatch):
        reg = _pin_registry(monkeypatch, _TRUST_OWNER)

        async def _fetch(git_url, branch, dest, log_lines, **kw):
            return {"ok": False, "error": "auth"}

        monkeypatch.setattr(reg_mod, "_git_fetch_branch", _fetch)
        entry = _entry()

        assert await _prewarm_owner_tier_store_assets(reg, [entry]) == 0
        assert _read_manifest_cache(entry) is None
        assert not _blob_cache_dir().exists() or not any(_blob_cache_dir().rglob("*.svg"))


# ---------------------------------------------------------------------------
# The trust boundary
# ---------------------------------------------------------------------------


class TestTrustBoundary:
    @pytest.mark.asyncio
    async def test_an_index_tier_registry_never_clones(self, monkeypatch):
        reg = _pin_registry(monkeypatch, _TRUST_INDEX)
        calls = _fake_fetch(monkeypatch, ART)
        entry = _entry()

        assert await _prewarm_owner_tier_store_assets(reg, [entry]) == 0
        assert calls == []
        assert _read_manifest_cache(entry) is None

    @pytest.mark.asyncio
    async def test_a_row_tagged_for_another_registry_is_not_this_fetchs_row(self, monkeypatch):
        reg = _pin_registry(monkeypatch, _TRUST_OWNER)
        calls = _fake_fetch(monkeypatch, ART)

        assert await _prewarm_owner_tier_store_assets(reg, [_entry(_registry="other")]) == 0
        assert calls == []

    @pytest.mark.asyncio
    async def test_an_untrusted_host_is_refused_and_the_refusal_audited(self, monkeypatch):
        reg = _pin_registry(monkeypatch, _TRUST_OWNER)
        calls = _fake_fetch(monkeypatch, ART)
        decisions: list[tuple[str, str, bool, str]] = []
        monkeypatch.setattr(
            reg_mod,
            "_sel_credential_decision",
            lambda op, url, *, granted, reason="": decisions.append((op, url, granted, reason)),
        )
        entry = _entry(gitUrl=OTHER_HOST, repo=OTHER_HOST)

        assert await _prewarm_owner_tier_store_assets(reg, [entry]) == 0
        assert calls == []
        assert decisions == [
            ("prewarm_store_art_owner_tier", OTHER_HOST, False, "host_not_trusted")
        ]

    @pytest.mark.asyncio
    async def test_an_unservable_branch_never_reaches_a_clone(self, monkeypatch):
        reg = _pin_registry(monkeypatch, _TRUST_OWNER)
        calls = _fake_fetch(monkeypatch, ART)

        assert await _prewarm_owner_tier_store_assets(reg, [_entry(branch="--upload-pack=x")]) == 0
        assert calls == []

    @pytest.mark.asyncio
    async def test_empty_input_is_a_no_op(self, monkeypatch):
        reg = _pin_registry(monkeypatch, _TRUST_OWNER)
        calls = _fake_fetch(monkeypatch, ART)
        assert await _prewarm_owner_tier_store_assets(reg, []) == 0
        assert calls == []


# ---------------------------------------------------------------------------
# The transport allowlist: a plaintext repo never receives owner credentials
# ---------------------------------------------------------------------------


class TestTransportAllowlist:
    """``_looks_like_git_url`` admits plaintext ``http://`` and ``git://``, and the
    trust gates above it are scheme-blind, so an owner-tier row naming such a repo
    would be cloned with owner credentials over an unauthenticated transport. The
    prewarm applies ``_is_supported_registry_transport`` (the same allowlist the
    index fetch uses) BEFORE any credential is offered: a non-https/ssh row is
    refused, never cloned, never granted credentials, and stays cold."""

    @pytest.mark.asyncio
    async def test_a_plaintext_http_row_is_refused_before_credentials(self, monkeypatch):
        reg = _pin_registry(monkeypatch, _TRUST_OWNER)
        calls = _fake_fetch(monkeypatch, ART)
        grants: list[tuple[str, str]] = []
        monkeypatch.setattr(
            reg_mod, "_sel_credential_grant", lambda op, url: grants.append((op, url))
        )
        insecure = "http://forge.example.com/org/some-private-app.git"
        entry = _entry(gitUrl=insecure, repo=insecure)

        assert await _prewarm_owner_tier_store_assets(reg, [entry]) == 0
        assert calls == [], "a plaintext http row must not be cloned"
        assert grants == [], "no credential may be granted for an unsupported transport"
        assert _read_manifest_cache(entry) is None
        assert not any(p.is_file() for p in _blob_cache_dir().rglob("*"))

    @pytest.mark.asyncio
    async def test_a_git_protocol_row_is_refused(self, monkeypatch):
        reg = _pin_registry(monkeypatch, _TRUST_OWNER)
        calls = _fake_fetch(monkeypatch, ART)
        insecure = "git://forge.example.com/org/some-private-app.git"
        entry = _entry(gitUrl=insecure, repo=insecure)

        assert await _prewarm_owner_tier_store_assets(reg, [entry]) == 0
        assert calls == []

    @pytest.mark.asyncio
    async def test_the_https_row_still_clones_negative_control(self, monkeypatch):
        """Negative control: the SAME row on https IS cloned, proving the refusal
        above observes the scheme and not an accident of the fixture. Reverting the
        fix (dropping the allowlist gate) would let the http row above clone too."""
        reg = _pin_registry(monkeypatch, _TRUST_OWNER)
        calls = _fake_fetch(monkeypatch, ART)
        entry = _entry()  # SIBLING is https://

        assert await _prewarm_owner_tier_store_assets(reg, [entry]) == 1
        assert len(calls) == 1


# ---------------------------------------------------------------------------
# Every credential refusal on the prewarm path leaves a denied SEL record
# ---------------------------------------------------------------------------


class TestEveryRefusalIsAudited:
    """F4: an owner-credential refusal is a security-relevant decision an incident
    responder reads to see every stopped escalation. The host-not-trusted refusal
    already emitted a denied ``_sel_credential_decision``; the unsupported-transport
    and unservable-branch refusals in ``_fetch_owner_tier_store_assets`` and the
    ambiguous-provenance / no-repo-key skips in the prewarm loop now do too, through
    the same helper with a distinct reason each."""

    def _spy_decisions(self, monkeypatch):
        decisions: list[tuple[str, bool, str]] = []
        monkeypatch.setattr(
            reg_mod,
            "_sel_credential_decision",
            lambda op, url, *, granted, reason="": decisions.append((op, granted, reason)),
        )
        return decisions

    @pytest.mark.asyncio
    async def test_unsupported_transport_emits_a_denied_decision(self, monkeypatch):
        reg = _pin_registry(monkeypatch, _TRUST_OWNER)
        _fake_fetch(monkeypatch, ART)
        decisions = self._spy_decisions(monkeypatch)
        insecure = "http://forge.example.com/org/some-private-app.git"
        entry = _entry(gitUrl=insecure, repo=insecure)

        assert await _prewarm_owner_tier_store_assets(reg, [entry]) == 0
        assert decisions == [("prewarm_store_art_owner_tier", False, "unsupported_transport")]

    @pytest.mark.asyncio
    async def test_unservable_branch_emits_a_denied_decision(self, monkeypatch):
        reg = _pin_registry(monkeypatch, _TRUST_OWNER)
        _fake_fetch(monkeypatch, ART)
        decisions = self._spy_decisions(monkeypatch)

        assert await _prewarm_owner_tier_store_assets(reg, [_entry(branch="--upload-pack=x")]) == 0
        assert decisions == [("prewarm_store_art_owner_tier", False, "unservable_branch")]

    @pytest.mark.asyncio
    async def test_no_repo_key_emits_a_denied_decision(self, monkeypatch):
        reg = _pin_registry(monkeypatch, _TRUST_OWNER)
        _fake_fetch(monkeypatch, ART)
        decisions = self._spy_decisions(monkeypatch)
        entry = _entry()
        entry.pop("repo", None)

        assert await _prewarm_owner_tier_store_assets(reg, [entry]) == 0
        assert decisions == [("prewarm_store_art_owner_tier", False, "no_repo_key")]

    @pytest.mark.asyncio
    async def test_ambiguous_provenance_emits_a_denied_decision(self, monkeypatch):
        reg = _pin_registry(monkeypatch, _TRUST_OWNER)
        _fake_fetch(monkeypatch, ART)
        monkeypatch.setattr(
            reg_mod, "_prewarm_provenance_ambiguous", lambda name, repo, claims: True
        )
        decisions = self._spy_decisions(monkeypatch)

        assert await _prewarm_owner_tier_store_assets(reg, [_entry()]) == 0
        assert decisions == [("prewarm_store_art_owner_tier", False, "ambiguous_provenance")]

    @pytest.mark.asyncio
    async def test_a_clean_row_records_a_grant_not_a_denial_negative_control(self, monkeypatch):
        """Negative control: a row that passes every gate records a GRANT (via
        ``_sel_credential_grant``) and no denied decision, proving the denied events
        above are the refusals and not emitted unconditionally."""
        reg = _pin_registry(monkeypatch, _TRUST_OWNER)
        _fake_fetch(monkeypatch, ART)
        decisions = self._spy_decisions(monkeypatch)
        grants: list[str] = []
        monkeypatch.setattr(reg_mod, "_sel_credential_grant", lambda op, url: grants.append(op))

        assert await _prewarm_owner_tier_store_assets(reg, [_entry()]) == 1
        assert decisions == [], "a clean row emits no denied decision"
        assert grants == ["prewarm_store_art_owner_tier"], "it records the grant instead"


# ---------------------------------------------------------------------------
# The provenance gate: an ambiguous repo key never prewarms
# ---------------------------------------------------------------------------


class TestAmbiguousRepoKeyProvenanceGate:
    """The blob proxy refuses owner credentials for a ``repo`` key unless exactly one
    configured source claims it, because the entry is selected by repo alone and is
    provenance-blind. If the prewarm cached such a row's bytes anyway, a request
    reachable through a DIFFERENT source for the same key would be served the cached
    bytes before that gate ever ran -- a cross-registry confused-deputy read. So the
    prewarm applies a provenance predicate and skips (counts as not fetched) any row
    whose key is ambiguous. A sibling registry whose cache is ABSENT or unreadable is
    treated as a possible claimant, so a first-refresh/GC'd sibling cannot make a
    shared repo look unique."""

    @pytest.mark.asyncio
    async def test_an_ambiguous_repo_key_is_skipped_and_nothing_is_cloned(self, monkeypatch):
        reg = _pin_registry(monkeypatch, _TRUST_OWNER)
        calls = _fake_fetch(monkeypatch, ART)
        # The provenance predicate reports ambiguous: the row must not be cloned.
        monkeypatch.setattr(
            reg_mod, "_prewarm_provenance_ambiguous", lambda name, repo, claims: True
        )
        entry = _entry()

        assert await _prewarm_owner_tier_store_assets(reg, [entry]) == 0
        assert calls == [], "an ambiguous-provenance row must not be cloned"
        assert _read_manifest_cache(entry) is None
        assert not any(p.is_file() for p in _blob_cache_dir().rglob("*"))

    @pytest.mark.asyncio
    async def test_an_unambiguous_key_is_prewarmed(self, monkeypatch):
        reg = _pin_registry(monkeypatch, _TRUST_OWNER)
        calls = _fake_fetch(monkeypatch, ART)
        # Not ambiguous, so the prewarm runs.
        monkeypatch.setattr(
            reg_mod, "_prewarm_provenance_ambiguous", lambda name, repo, claims: False
        )
        entry = _entry()

        assert await _prewarm_owner_tier_store_assets(reg, [entry]) == 1
        assert len(calls) == 1
        assert _read_manifest_cache(entry) == MANIFEST

    @pytest.mark.asyncio
    async def test_the_skip_observes_the_predicate_negative_control(self, monkeypatch):
        """Negative control: the SAME row and fixture, but the predicate reports
        not-ambiguous, so it IS cloned -- proving the skip observes the predicate and
        not an accident of the fixture."""
        reg = _pin_registry(monkeypatch, _TRUST_OWNER)
        calls = _fake_fetch(monkeypatch, ART)
        monkeypatch.setattr(
            reg_mod, "_prewarm_provenance_ambiguous", lambda name, repo, claims: False
        )
        entry = _entry()

        assert await _prewarm_owner_tier_store_assets(reg, [entry]) == 1
        assert len(calls) == 1

    @pytest.mark.asyncio
    @pytest.mark.parametrize("bad_repo", ["", None, 7, ["https://x.example/a"]])
    async def test_a_row_without_a_usable_repo_key_is_refused_not_waved_through(
        self, monkeypatch, bad_repo
    ):
        """Deny by default: a row that cannot state a repo claim at all (empty,
        missing or non-string key) never buys the owner-credentialed clone, even
        when the predicate would have said "not ambiguous" for a real key. The
        ambiguity gate is the only thing between an index row and a credentialed
        clone, so a row that slips past it on a type check is the exact
        confused-deputy read it exists to refuse."""
        reg = _pin_registry(monkeypatch, _TRUST_OWNER)
        calls = _fake_fetch(monkeypatch, ART)
        monkeypatch.setattr(
            reg_mod, "_prewarm_provenance_ambiguous", lambda name, repo, claims: False
        )
        entry = _entry()
        if bad_repo is None:
            entry.pop("repo", None)
        else:
            entry["repo"] = bad_repo

        assert await _prewarm_owner_tier_store_assets(reg, [entry]) == 0
        assert calls == [], "a row with no usable repo key must not be cloned"


class TestProvenancePredicateTreatsAbsentSiblingCacheAsAmbiguous:
    """``_prewarm_provenance_ambiguous`` is the F2 fix: a sibling registry with NO
    readable cache is a POSSIBLE claimant, not a non-claimant. The pre-fix count of
    declaring sources contributed 0 for a missing cache, so a first-refresh or GC'd
    sibling made a shared repo look unique. Now the row is warmable only when every
    OTHER source has a readable cache that does NOT declare the key."""

    def _two_registries(self, monkeypatch, sibling_cache):
        """Pin two registries ('official' + 'sibling'); the sibling's cache read
        returns *sibling_cache* (None = absent/unreadable, a list = readable)."""
        this_reg = type("R", (), {"repo": FORGE, "branch": None})()
        sibling_reg = type("R", (), {"repo": OTHER_HOST, "branch": None})()
        monkeypatch.setattr(reg_mod, "_public_registry_name", lambda r: r.repo)
        monkeypatch.setattr(reg_mod, "_effective_registries", lambda: [this_reg, sibling_reg])
        monkeypatch.setattr(reg_mod, "_load_registry_file", lambda: [])
        monkeypatch.setattr(reg_mod, "_external_registry_cache_identity", lambda r: r.repo)
        monkeypatch.setattr(
            reg_mod,
            "_read_external_registry_cache",
            lambda name, *, ignore_ttl=False: (sibling_cache if name == OTHER_HOST else []),
        )
        return this_reg.repo

    def test_a_sibling_with_no_cache_makes_the_key_ambiguous(self, monkeypatch):
        this_name = self._two_registries(monkeypatch, sibling_cache=None)
        assert (
            reg_mod._prewarm_provenance_ambiguous(
                this_name, SIBLING, reg_mod._prewarm_foreign_claims(this_name)
            )
            is True
        )

    def test_a_sibling_cache_not_declaring_the_repo_is_unambiguous(self, monkeypatch):
        this_name = self._two_registries(monkeypatch, sibling_cache=[{"repo": "unrelated"}])
        assert (
            reg_mod._prewarm_provenance_ambiguous(
                this_name, SIBLING, reg_mod._prewarm_foreign_claims(this_name)
            )
            is False
        )

    def test_a_sibling_cache_declaring_the_repo_is_ambiguous(self, monkeypatch):
        this_name = self._two_registries(monkeypatch, sibling_cache=[{"repo": SIBLING}])
        assert (
            reg_mod._prewarm_provenance_ambiguous(
                this_name, SIBLING, reg_mod._prewarm_foreign_claims(this_name)
            )
            is True
        )

    def test_the_bundled_registry_file_counts_as_a_claimant(self, monkeypatch):
        self._two_registries(monkeypatch, sibling_cache=[{"repo": "unrelated"}])
        # Now the bundled file also declares the key: ambiguous even though the
        # sibling's readable cache does not.
        monkeypatch.setattr(reg_mod, "_load_registry_file", lambda: [{"repo": SIBLING}])
        assert (
            reg_mod._prewarm_provenance_ambiguous(
                FORGE, SIBLING, reg_mod._prewarm_foreign_claims(FORGE)
            )
            is True
        )

    def test_a_read_failure_fails_closed_ambiguous(self, monkeypatch):
        this_name = self._two_registries(monkeypatch, sibling_cache=[{"repo": "unrelated"}])

        def _boom():
            raise RuntimeError("registry read failed")

        monkeypatch.setattr(reg_mod, "_effective_registries", _boom)
        assert (
            reg_mod._prewarm_provenance_ambiguous(
                this_name, SIBLING, reg_mod._prewarm_foreign_claims(this_name)
            )
            is True
        )

    def test_a_batch_reads_each_sibling_cache_once(self, monkeypatch):
        """N rows cost one pass over the sources, not N: the batch reads the
        foreign claims once (`_prewarm_foreign_claims`) and every row is a pure
        lookup against them."""
        this_name = self._two_registries(monkeypatch, sibling_cache=[{"repo": "unrelated"}])
        reads: list[str] = []
        inner = reg_mod._read_external_registry_cache

        def _counting(name, *, ignore_ttl=False):
            reads.append(name)
            return inner(name, ignore_ttl=ignore_ttl)

        monkeypatch.setattr(reg_mod, "_read_external_registry_cache", _counting)
        claims = reg_mod._prewarm_foreign_claims(this_name)
        for repo in (SIBLING, "https://x.example/one", "https://x.example/two"):
            assert reg_mod._prewarm_provenance_ambiguous(this_name, repo, claims) is False
        assert reads == [OTHER_HOST], "one sibling, one read, however many rows"


class TestBothGatesShareOneCountingCore:
    """The blob proxy's `_repo_key_owner_count` and the prewarm's
    `_prewarm_foreign_claims` are both thin wrappers over `sources._repo_key_claims`.
    They read ONE source union (the bundled file plus every effective registry) and
    differ only in the `strict` flag and the `except_registry` exclusion, so the one
    single-owner credential rule cannot silently drift into two."""

    def _pin_two_named(self, monkeypatch, *, sibling_cache, official_cache=()):
        """Pin 'official' (repo=FORGE) and 'sibling' (repo=OTHER_HOST) with names
        DISTINCT from their repos, so the prewarm's `except_registry` exclusion of
        'official' is observable in the cache-read sequence. Each registry's cache
        read returns its configured list (``official_cache`` defaults to empty,
        ``sibling_cache`` may be None for absent/unreadable); the bundled file is
        empty unless a test overrides it."""
        official = type("R", (), {"name": "official", "repo": FORGE, "branch": None})()
        sibling = type("R", (), {"name": "sibling", "repo": OTHER_HOST, "branch": None})()
        caches = {"official": list(official_cache), "sibling": sibling_cache}
        monkeypatch.setattr(reg_mod, "_public_registry_name", lambda r: r.name)
        monkeypatch.setattr(reg_mod, "_effective_registries", lambda: [official, sibling])
        monkeypatch.setattr(reg_mod, "_load_registry_file", lambda: [])
        monkeypatch.setattr(reg_mod, "_external_registry_cache_identity", lambda r: r.name)
        monkeypatch.setattr(
            reg_mod,
            "_read_external_registry_cache",
            lambda name, *, ignore_ttl=False: caches.get(name, []),
        )

    def _spy_reads(self, monkeypatch):
        """Record the registry names each source read consults. Returns a list the
        caller clears between the two gate calls, so the two sequences never mix
        (a second spy layered on the first would append to both)."""
        reads: list[str] = []
        inner = reg_mod._read_external_registry_cache

        def _counting(name, *, ignore_ttl=False):
            reads.append(name)
            return inner(name, ignore_ttl=ignore_ttl)

        monkeypatch.setattr(reg_mod, "_read_external_registry_cache", _counting)
        return reads

    def test_the_two_gates_read_the_same_source_union(self, monkeypatch):
        """The proxy count consults EVERY effective registry; the prewarm consults
        the same set minus the one it is attributing to. So the prewarm's read
        sequence is exactly the proxy's with the attributed registry excluded --
        one union, one `except_registry` delta."""
        self._pin_two_named(monkeypatch, sibling_cache=[{"repo": "unrelated"}])
        reads = self._spy_reads(monkeypatch)

        reg_mod._repo_key_owner_count(SIBLING)
        proxy_reads = list(reads)
        assert proxy_reads == ["official", "sibling"], "proxy counts every source"

        reads.clear()
        reg_mod._prewarm_foreign_claims("official")
        prewarm_reads = list(reads)
        assert prewarm_reads == ["sibling"], "prewarm excludes the attributed registry"
        # The prewarm's set is the proxy's set minus exactly the excluded registry.
        assert prewarm_reads == [r for r in proxy_reads if r != "official"]

    def test_both_gates_route_through_the_shared_core(self, monkeypatch):
        """Patching `_repo_key_claims` diverts BOTH gates, proving neither keeps its
        own counting loop."""
        self._pin_two_named(monkeypatch, sibling_cache=[{"repo": "unrelated"}])
        seen: list[dict[str, Any]] = []

        def _fake_core(*, strict, except_registry=None):
            seen.append({"strict": strict, "except_registry": except_registry})
            return False, [[SIBLING]]

        monkeypatch.setattr(reg_mod, "_repo_key_claims", _fake_core)

        assert reg_mod._repo_key_owner_count(SIBLING) == 1
        unresolvable, claims = reg_mod._prewarm_foreign_claims("official")
        assert (unresolvable, claims) == (False, [SIBLING])
        assert seen == [
            {"strict": False, "except_registry": None},
            {"strict": True, "except_registry": "official"},
        ], "the proxy calls strict=False/no-exclusion; the prewarm strict=True/excludes itself"

    def test_strict_is_the_only_delta_on_an_absent_sibling_cache(self, monkeypatch):
        """The ONE semantic difference: a sibling whose cache is ABSENT. The prewarm
        (strict=True) reads it as a possible claimant -> unresolvable; the proxy
        (strict=False) does not count it, so a key only 'official' declares still
        counts as a single owner. Same source union, same repo, opposite reading of
        the missing cache -- exactly the `strict` flag."""
        self._pin_two_named(monkeypatch, sibling_cache=None)  # sibling cache absent
        # The bundled file declares the key so 'official' has one claimant to count.
        monkeypatch.setattr(reg_mod, "_load_registry_file", lambda: [{"repo": SIBLING}])

        # Proxy: absent sibling contributes nothing, bundled declares it -> count 1.
        assert reg_mod._repo_key_owner_count(SIBLING) == 1

        # Prewarm: the absent sibling could publish the key -> unresolvable (ambiguous).
        unresolvable, _ = reg_mod._prewarm_foreign_claims("official")
        assert unresolvable is True
        assert (
            reg_mod._prewarm_provenance_ambiguous("official", SIBLING, (unresolvable, [])) is True
        )

    def test_the_strict_delta_negative_control(self, monkeypatch):
        """Negative control: give the sibling a READABLE cache that does not declare
        the key. Now the strict reading has nothing to fail closed on, so the prewarm
        agrees with the proxy -- both see a single, unambiguous owner. This proves the
        prior test's divergence was the absent cache, not the fixture.

        The key is declared by 'official's OWN cache: the proxy counts that one source
        (grant), and the prewarm excludes 'official' as the registry it attributes to,
        so with the sibling readable and silent no foreign source claims the key
        (warm). Both land on a single owner."""
        self._pin_two_named(
            monkeypatch,
            official_cache=[{"repo": SIBLING}],
            sibling_cache=[{"repo": "unrelated"}],
        )

        assert reg_mod._repo_key_owner_count(SIBLING) == 1
        unresolvable, claims = reg_mod._prewarm_foreign_claims("official")
        assert unresolvable is False
        assert (
            reg_mod._prewarm_provenance_ambiguous("official", SIBLING, (unresolvable, claims))
            is False
        )


class TestClaimCapsBoundMemory:
    """F3: `_repo_key_claims` retains untrusted external-cache repo strings, so it caps
    the total retained (`_MAX_FOREIGN_CLAIMS`) and each string's length
    (`_MAX_CLAIM_STRING_LEN`). Both caps fail closed (unresolvable, ambiguous for every
    row): overflowing the total, AND an over-long string -- which cannot be dropped,
    because `_normalize_git_target` would strip its padding and it could then match a
    real URL, so a drop would undercount the claimants (fail-open)."""

    def _one_sibling(self, monkeypatch, sibling_cache):
        """Pin 'official' + 'sibling'; the sibling's cache read returns *sibling_cache*.
        'official' is the attributed registry, so only the sibling's claims are retained
        by the prewarm wrapper, while the proxy count consults both."""
        official = type("R", (), {"name": "official", "repo": FORGE, "branch": None})()
        sibling = type("R", (), {"name": "sibling", "repo": OTHER_HOST, "branch": None})()
        caches = {"official": [], "sibling": sibling_cache}
        monkeypatch.setattr(reg_mod, "_public_registry_name", lambda r: r.name)
        monkeypatch.setattr(reg_mod, "_effective_registries", lambda: [official, sibling])
        monkeypatch.setattr(reg_mod, "_load_registry_file", lambda: [])
        monkeypatch.setattr(reg_mod, "_external_registry_cache_identity", lambda r: r.name)
        monkeypatch.setattr(
            reg_mod,
            "_read_external_registry_cache",
            lambda name, *, ignore_ttl=False: caches.get(name, []),
        )

    def test_an_oversized_cache_is_unresolvable_and_bounded(self, monkeypatch):
        monkeypatch.setattr(reg_mod, "_MAX_FOREIGN_CLAIMS", 100)
        # A sibling cache far larger than the cap.
        big = [{"repo": f"https://x.example/{i}"} for i in range(1000)]
        self._one_sibling(monkeypatch, big)

        unresolvable, sources = reg_mod._repo_key_claims(strict=True, except_registry="official")
        assert unresolvable is True, "overflow past the cap is fail-closed unresolvable"
        # Bounded memory: nothing retained past the cap is handed back.
        assert sum(len(s) for s in sources) <= 100

        # The prewarm wrapper surfaces it as unresolvable -> every row ambiguous.
        u, claims = reg_mod._prewarm_foreign_claims("official")
        assert (u, claims) == (True, [])
        assert reg_mod._prewarm_provenance_ambiguous("official", SIBLING, (u, claims)) is True
        # The blob proxy fails closed too: unresolvable maps to the ambiguous count.
        assert reg_mod._repo_key_owner_count("https://x.example/1") == 2

    def test_a_cache_within_the_cap_is_resolvable_negative_control(self, monkeypatch):
        """Negative control: the SAME shape, but the cache fits under the cap, so the
        call is resolvable — proving the unresolvable verdict above is the overflow,
        not the fixture."""
        monkeypatch.setattr(reg_mod, "_MAX_FOREIGN_CLAIMS", 100)
        small = [{"repo": f"https://x.example/{i}"} for i in range(10)]
        self._one_sibling(monkeypatch, small)

        u, claims = reg_mod._prewarm_foreign_claims("official")
        assert u is False
        assert len(claims) == 10

    def test_an_over_long_repo_string_is_unresolvable(self, monkeypatch):
        monkeypatch.setattr(reg_mod, "_MAX_CLAIM_STRING_LEN", 32)
        long_repo = "https://x.example/" + "a" * 100  # over the length cap
        ok_repo = "https://x.example/short"
        self._one_sibling(monkeypatch, [{"repo": long_repo}, {"repo": ok_repo}])

        # The over-long string is NOT dropped (that would be fail-open: normalization
        # strips the padding and it could match a real URL). It is an overflow, so the
        # whole call is unresolvable and every row ambiguous.
        u, claims = reg_mod._prewarm_foreign_claims("official")
        assert (u, claims) == (True, [])
        assert reg_mod._prewarm_provenance_ambiguous("official", ok_repo, (u, claims)) is True
        assert reg_mod._prewarm_provenance_ambiguous("official", long_repo, (u, claims)) is True
        # The blob proxy fails closed too: unresolvable maps to the ambiguous count.
        assert reg_mod._repo_key_owner_count(ok_repo) == 2
        assert reg_mod._repo_key_owner_count(long_repo) == 2

    def test_a_string_within_the_length_cap_is_kept_negative_control(self, monkeypatch):
        """Negative control: raise the length cap above the same string and it is
        retained and resolvable — so the unresolvable verdict above is the length cap,
        not the fixture."""
        monkeypatch.setattr(reg_mod, "_MAX_CLAIM_STRING_LEN", 4096)
        long_repo = "https://x.example/" + "a" * 100
        self._one_sibling(monkeypatch, [{"repo": long_repo}])

        u, claims = reg_mod._prewarm_foreign_claims("official")
        assert u is False
        assert claims == [long_repo], "within the length cap the string is kept"

    def test_an_oversized_cache_is_not_fully_traversed(self, monkeypatch):
        """The cap is applied while STREAMING the source rows, so an oversized cache
        stops at the cap instead of its whole repo list being materialised first.
        Each row counts a ``repo`` access; the count must not exceed ``cap + 1`` (the
        one row that trips the cap is read, the rest are never pulled)."""
        cap = 100
        monkeypatch.setattr(reg_mod, "_MAX_FOREIGN_CLAIMS", cap)
        visited = {"n": 0}

        class _CountingRow(dict):
            # Every ``e.get("repo")`` / ``e["repo"]`` the streaming comprehension makes
            # debits the counter, so the total is exactly how many rows were pulled.
            def get(self, key, default=None):
                if key == "repo":
                    visited["n"] += 1
                return super().get(key, default)

            def __getitem__(self, key):
                if key == "repo":
                    visited["n"] += 1
                return super().__getitem__(key)

        big = [_CountingRow(repo=f"https://x.example/{i}") for i in range(1000)]
        self._one_sibling(monkeypatch, big)

        unresolvable, _sources = reg_mod._repo_key_claims(strict=True, except_registry="official")
        assert unresolvable is True
        # A fully-materialised list would touch all 1000 rows; streaming stops at the
        # cap. ``.get`` (the isinstance filter) and ``[]`` (the value) each touch a
        # row, so allow a small constant factor over the cap, but nowhere near 1000.
        assert visited["n"] <= (cap + 1) * 2, visited["n"]
        assert visited["n"] < 1000, "the whole oversized cache must not be traversed"


# ---------------------------------------------------------------------------
# The art gates
# ---------------------------------------------------------------------------


HOSTILE_PATHS = (
    "../../outside.png",  # traversal
    "/etc/absolute.png",  # absolute
    ".git/config.png",  # hidden segment
    "ui/script.js",  # not an image
    "ui/with space.png",  # outside the proxy's path grammar
    "ui/missing.png",  # declared, not in the clone
)


class TestArtGates:
    def test_the_gate_refuses_what_the_proxy_refuses(self):
        for hostile in HOSTILE_PATHS[:-1]:
            assert not _servable_art_path(hostile), hostile
        assert _servable_art_path("ui/icon.svg")
        assert _servable_art_path("./ui/icon.svg")
        assert _servable_art_path("ui/missing.png")  # grammar only; existence is checked later

    def test_the_cache_path_is_the_proxys_for_the_emitted_url(self):
        entry = _entry(subdirectory="apps/app")
        merged = _merge_manifest(entry, MANIFEST)
        assert _store_art_cache_path(entry, "ui/icon.svg") == _route_cache_path(
            entry, merged["iconUrl"]
        )
        # And None for a path the proxy would refuse, so nothing is ever written there.
        for hostile in HOSTILE_PATHS[:-1]:
            assert _store_art_cache_path(entry, hostile) is None, hostile

    @requires_symlinks
    def test_a_symlink_loop_under_the_cache_root_never_propagates(self):
        """POSIX ``Path.resolve`` reports a loop as ``RuntimeError``, which must
        degrade to None, never propagate: this path is reached bare from
        ``refresh_registries``, where an exception is a 500 on the refresh route.
        On Windows the non-strict resolve either returns the path as written,
        which is still under the cache root, or raises ``OSError`` (WinError
        1921), which also degrades to None. Every answer keeps the write inside
        the root, and none raises."""
        entry = _entry()
        good = _store_art_cache_path(entry, "ui/icon.svg")
        assert good is not None
        # Plant the loop at the row's own cache directory so the resolve walks it.
        good.parent.parent.mkdir(parents=True, exist_ok=True)
        os.symlink(good.parent.name, str(good.parent))
        result = _store_art_cache_path(entry, "ui/icon.svg")
        if sys.platform == "win32":
            assert result is None or result.is_relative_to(_blob_cache_dir().resolve())
        else:
            assert result is None

    @pytest.mark.asyncio
    async def test_a_hostile_manifest_caches_only_its_servable_art(self, monkeypatch, tmp_path):
        reg = _pin_registry(monkeypatch, _TRUST_OWNER)
        outside = tmp_path / "outside.png"
        outside.write_bytes(b"secret")
        manifest = dict(MANIFEST, screenshots=[*HOSTILE_PATHS, "ui/link.png", "ui/shot-1.png"])
        tree = {
            "ui/icon.svg": ART["ui/icon.svg"],
            "ui/hero.png": ART["ui/hero.png"],
            "ui/shot-1.png": ART["ui/shot-1.png"],
            "ui/script.js": b"alert(1)",
            "ui/with space.png": b"x",
            ".git/config.png": b"x",
        }
        calls = _fake_fetch(monkeypatch, tree, manifest)

        # A symlink inside the clone pointing outside it: laid down by the fetch stub's
        # caller, after the tree, so it is what the prewarm sees.
        real_fetch = reg_mod._git_fetch_branch

        async def _fetch_with_link(git_url, branch, dest, log_lines, **kw):
            result = await real_fetch(git_url, branch, dest, log_lines, **kw)
            (Path(dest) / "ui" / "link.png").symlink_to(outside)
            return result

        monkeypatch.setattr(reg_mod, "_git_fetch_branch", _fetch_with_link)
        entry = _entry()

        assert await _prewarm_owner_tier_store_assets(reg, [entry]) == 1
        assert len(calls) == 1
        assert _read_manifest_cache(entry) == manifest

        cached = sorted(
            p.relative_to(_blob_cache_dir()).as_posix()
            for p in _blob_cache_dir().rglob("*")
            if p.is_file()
        )
        key = _blob_cache_key(SIBLING, SIBLING)
        assert cached == sorted(
            f"{key}/main/{rel}" for rel in ("ui/icon.svg", "ui/hero.png", "ui/shot-1.png")
        )
        assert b"secret" not in b"".join(
            p.read_bytes() for p in _blob_cache_dir().rglob("*") if p.is_file()
        )

    @pytest.mark.asyncio
    async def test_an_oversize_file_is_skipped(self, monkeypatch):
        reg = _pin_registry(monkeypatch, _TRUST_OWNER)
        monkeypatch.setattr(reg_mod, "_ART_MAX_BYTES", 8)
        _fake_fetch(monkeypatch, dict(ART, **{"ui/hero.png": b"x" * 9}))
        entry = _entry()

        await _prewarm_owner_tier_store_assets(reg, [entry])

        assert _store_art_cache_path(entry, "ui/icon.svg").is_file()
        assert not _store_art_cache_path(entry, "ui/hero.png").exists()

    @pytest.mark.asyncio
    async def test_the_gates_can_fail(self, monkeypatch):
        """Negative control: with the gate bypassed the hostile path IS cached.

        Proves the assertions above observe the gate and not an accident of the
        fixture (a path the stub never wrote would be absent under any gate).
        """
        reg = _pin_registry(monkeypatch, _TRUST_OWNER)
        manifest = dict(MANIFEST, screenshots=["ui/script.js"])
        _fake_fetch(monkeypatch, dict(ART, **{"ui/script.js": b"alert(1)"}), manifest)
        monkeypatch.setattr(reg_mod, "_servable_art_path", lambda path: True)
        entry = _entry()

        await _prewarm_owner_tier_store_assets(reg, [entry])

        key = _blob_cache_key(SIBLING, SIBLING)
        assert (_blob_cache_dir() / key / "main" / "ui" / "script.js").is_file()

    @pytest.mark.asyncio
    async def test_a_symlinked_manifest_is_refused_not_followed(self, monkeypatch, tmp_path):
        """``app.json`` itself is a symlink to a JSON file outside the clone.

        Whatever the manifest read returns is written into the manifest cache, which
        the agent can read; so ``app.json`` is read through the same pinned no-follow
        descriptor walk as every art file it declares, and a link at the ``app.json``
        component is refused -- the row is dropped (no manifest cached, no art cached)
        rather than caching the target's bytes. Both layouts: manifest at the clone
        root and under a ``subdirectory``.
        """
        reg = _pin_registry(monkeypatch, _TRUST_OWNER)
        secret = tmp_path / "credentials.json"
        secret.write_text(json.dumps({"token": "hunter2", "iconPath": "ui/icon.svg"}))
        _fake_fetch(monkeypatch, ART, manifest=None)
        real_fetch = reg_mod._git_fetch_branch

        for subdirectory in ("", "apps/app"):

            async def _fetch_with_link(git_url, branch, dest, log_lines, **kw):
                result = await real_fetch(git_url, branch, dest, log_lines, **kw)
                target_dir = Path(dest) / subdirectory if subdirectory else Path(dest)
                target_dir.mkdir(parents=True, exist_ok=True)
                (target_dir / "app.json").symlink_to(secret)
                return result

            monkeypatch.setattr(reg_mod, "_git_fetch_branch", _fetch_with_link)
            entry = _entry(subdirectory=subdirectory) if subdirectory else _entry()

            assert await _prewarm_owner_tier_store_assets(reg, [entry]) == 0
            assert not _manifest_cache_path(entry).exists()
            assert not any(p.is_file() for p in _blob_cache_dir().rglob("*"))

    @pytest.mark.asyncio
    async def test_the_manifest_containment_can_fail(self, monkeypatch, tmp_path):
        """Negative control: with the pinned no-follow read bypassed the symlinked
        ``app.json`` IS followed and the target's bytes land in the manifest cache --
        so the refusal above observes the pinned read, not a fixture that never wrote
        a readable file."""
        reg = _pin_registry(monkeypatch, _TRUST_OWNER)
        secret = tmp_path / "credentials.json"
        secret.write_text(json.dumps({"token": "hunter2"}))
        _fake_fetch(monkeypatch, ART, manifest=None)
        real_fetch = reg_mod._git_fetch_branch

        async def _fetch_with_link(git_url, branch, dest, log_lines, **kw):
            result = await real_fetch(git_url, branch, dest, log_lines, **kw)
            (Path(dest) / "app.json").symlink_to(secret)
            return result

        monkeypatch.setattr(reg_mod, "_git_fetch_branch", _fetch_with_link)

        # Bypass: read the manifest by path, following the symlink, as the pre-fix
        # ``read_text`` did.
        def _by_path(root, rel_parts, *, max_bytes=None):
            src = (root / Path(*rel_parts)).resolve()
            return src.read_bytes() if src.is_file() else None

        monkeypatch.setattr(reg_mod, "_open_pinned_asset", _by_path)
        entry = _entry()

        assert await _prewarm_owner_tier_store_assets(reg, [entry]) == 1
        assert _read_manifest_cache(entry) == {"token": "hunter2"}


_GRAMMAR_PROBE = """
import json, sys
from pathlib import Path
# The child imports the package outside this test's fixtures, so it must not see the
# real home: refuse to import anything unless the home is under the test's tmp_path.
root = Path(sys.argv[1]).resolve()
assert root in Path.home().resolve().parents, f"the probe's home {Path.home()} is not isolated"
from kiro_crew.apps import routes
from kiro_crew.apps.registry_pipeline import indexes, store_art
print(json.dumps({
    "path": routes._SAFE_PATH_RE is store_art._SAFE_PATH_RE,
    "branch": indexes._SAFE_BRANCH_RE is store_art._SAFE_BRANCH_RE,
}))
"""

#: Lost-run ceiling on the child interpreter that imports the app routes cold: a few
#: seconds on a loaded runner, so only a wedged import reaches it, and it is half the
#: suite's 120 s ``--timeout``.
_GRAMMAR_PROBE_CEILING_SECS = 60.0


def _grammar_identities_in_a_fresh_interpreter(tmp_path: Path) -> dict[str, bool]:
    """Which consumers share the store-art compiled grammars, read in a child interpreter.

    The sharing is an import-time binding, and this worker's own modules are not a
    reliable witness of it: ``test_external_registry_clone_diagnostics.py`` reloads the registry facade,
    which re-executes ``store_art`` but not ``routes``, and once ``re``'s cache has
    dropped the pattern the reload compiles a NEW object, so an in-process identity
    check reads whichever tests ran before it. A child imports each module once.
    """
    source = Path(reg_mod.__file__).resolve().parents[2]
    home = tmp_path / "home"
    home.mkdir()
    env = {
        **os.environ,
        "HOME": str(home),
        "USERPROFILE": str(home),
        "KIROCREW_HOME": str(tmp_path / "data"),
        "KIROCREW_WORKSPACE": str(tmp_path / "workspace"),
        "PYTHONPATH": os.pathsep.join([str(source), os.environ.get("PYTHONPATH", "")]),
    }
    try:
        completed = subprocess.run(
            [sys.executable, "-c", _GRAMMAR_PROBE, str(tmp_path)],
            cwd=tmp_path,
            env=env,
            capture_output=True,
            timeout=_GRAMMAR_PROBE_CEILING_SECS,
            check=False,
            **UTF8_TEXT,
        )
    except subprocess.TimeoutExpired:
        pytest.fail(
            f"the grammar probe did not finish within {_GRAMMAR_PROBE_CEILING_SECS:.0f}s "
            "(lost-run ceiling)"
        )
    assert completed.returncode == 0, completed.stderr
    return json.loads(completed.stdout.strip().splitlines()[-1])


class TestOneGrammar:
    """The path and branch grammars have ONE spelling each; the consumers share the
    compiled object, so a change to one side cannot leave the other behind."""

    def test_the_prewarm_and_the_proxy_share_the_path_grammar(self, tmp_path):
        identities = _grammar_identities_in_a_fresh_interpreter(tmp_path)
        assert identities["path"], "the proxy has its own path grammar"
        # The proxy's trailing-newline defect the ``\\Z`` anchor closes stays closed
        # for the prewarm too.
        assert not _servable_art_path("ui/icon.svg\n")

    def test_the_prewarm_and_the_index_fetch_share_the_branch_grammar(self, tmp_path):
        identities = _grammar_identities_in_a_fresh_interpreter(tmp_path)
        assert identities["branch"], "the index fetch has its own branch grammar"


# ---------------------------------------------------------------------------
# Warm rows are not re-cloned
# ---------------------------------------------------------------------------


class TestWarmth:
    @pytest.mark.asyncio
    async def test_a_warm_row_is_skipped_and_a_cold_one_refetched(self, monkeypatch):
        reg = _pin_registry(monkeypatch, _TRUST_OWNER)
        calls = _fake_fetch(monkeypatch, ART)
        entry = _entry()

        assert await _prewarm_owner_tier_store_assets(reg, [entry]) == 1
        assert await _prewarm_owner_tier_store_assets(reg, [entry]) == 0
        assert len(calls) == 1

        # A missing art file makes the row cold again.
        _store_art_cache_path(entry, "ui/hero.png").unlink()
        assert await _prewarm_owner_tier_store_assets(reg, [entry]) == 1
        assert len(calls) == 2

        # So does a manifest that would expire before the next index fetch.
        past = time.time() - reg_mod._REWARM_AGE - 60
        os.utime(_manifest_cache_path(entry), (past, past))
        assert await _prewarm_owner_tier_store_assets(reg, [entry]) == 1
        assert len(calls) == 3

    @pytest.mark.asyncio
    async def test_rows_are_fetched_concurrently_but_bounded(self, monkeypatch):
        import asyncio

        reg = _pin_registry(monkeypatch, _TRUST_OWNER)
        width = reg_mod._PREWARM_CONCURRENCY
        live = 0
        peak = 0
        full_width = asyncio.Event()
        release = asyncio.Event()

        async def _fetch(git_url, branch, dest, log_lines, **kw):
            nonlocal live, peak
            live += 1
            peak = max(peak, live)
            if live == width:
                full_width.set()
            # Held in flight until the pool is SEEN at full width: each worker reaches
            # this fake only after several thread hops, so a fixed hold let them
            # arrive one at a time (peak 1 on Windows, where a 10 ms sleep is under the
            # loop clock's 15.6 ms resolution and ends at the next wake-up).
            await release.wait()
            dest = Path(dest)
            dest.mkdir(parents=True, exist_ok=True)
            (dest / "app.json").write_text(json.dumps(dict(MANIFEST, iconPath="")), "utf-8")
            live -= 1
            return None

        entered = 0
        real_row = reg_mod._fetch_owner_tier_store_assets

        async def _row(entry, registry_name):
            # A worker dequeues a row and enters its fetch in one step, so while the
            # held fetches keep their workers busy this counts every worker running.
            nonlocal entered
            entered += 1
            return await real_row(entry, registry_name)

        monkeypatch.setattr(reg_mod, "_git_fetch_branch", _fetch)
        monkeypatch.setattr(reg_mod, "_fetch_owner_tier_store_assets", _row)
        rows = [
            _entry(
                name=f"app-{i}", gitUrl=f"{SIBLING[:-4]}-{i}.git", repo=f"{SIBLING[:-4]}-{i}.git"
            )
            for i in range(10)
        ]
        task = asyncio.ensure_future(_prewarm_owner_tier_store_assets(reg, rows))
        try:
            await _await_barrier(
                full_width, f"{width} fetches in flight at once", lambda: f"live={live} peak={peak}"
            )
            # Every in-flight fetch is held, so no worker can have moved to a further
            # row: the rows begun are exactly the workers the pool runs.
            assert entered == width, f"{entered} rows begun at once, the bound is {width}"
        finally:
            release.set()
        assert await _settled(task, "the prewarm after the release") == 10
        assert 1 < peak <= reg_mod._PREWARM_CONCURRENCY

    @pytest.mark.asyncio
    async def test_the_batch_budget_returns_what_landed_and_cancels_the_rest(
        self, monkeypatch, caplog
    ):
        """The listing that missed the index cache waits at most the batch budget.

        Row A lands at once; row B's clone never returns. The prewarm must come back
        once the budget is spent with the count of what landed, and the stalled row's
        task must be cancelled, not abandoned. The outer bound turns a missing
        budget into a red test rather than a hang.
        """
        import asyncio

        reg = _pin_registry(monkeypatch, _TRUST_OWNER)
        cancelled: list[str] = []

        async def _fetch(entry, registry_name):
            if entry["name"] == "a":
                return True
            try:
                await asyncio.Event().wait()
            except asyncio.CancelledError:
                cancelled.append(entry["name"])
                raise
            return True

        monkeypatch.setattr(reg_mod, "_fetch_owner_tier_store_assets", _fetch)
        monkeypatch.setattr(reg_mod, "_PREWARM_BATCH_BUDGET", 0.2)
        rows = [
            _entry(name="a", gitUrl=f"{SIBLING[:-4]}-a.git", repo=f"{SIBLING[:-4]}-a.git"),
            _entry(name="b", gitUrl=f"{SIBLING[:-4]}-b.git", repo=f"{SIBLING[:-4]}-b.git"),
        ]

        with caplog.at_level("WARNING", logger="kiro_crew.apps.registry"):
            fetched = await finished(
                _prewarm_owner_tier_store_assets(reg, rows), "the prewarm returning at its budget"
            )

        assert fetched == 1
        assert cancelled == ["b"]
        (record,) = [r for r in caplog.records if "batch budget" in r.getMessage()]
        assert "1 row(s) fetched, 1 left cold" in record.getMessage()
        assert "'official'" in record.getMessage()


# ---------------------------------------------------------------------------
# A declared path the clone cannot supply does not keep the row cold
# ---------------------------------------------------------------------------


class TestUnobtainableArtRecord:
    """A manifest may declare a servable-shaped image the repository does not ship (or
    ships oversize, or as a non-regular file). No clone can ever put that file in the
    blob cache, so a warm check that only looks for the file would re-clone the row
    with owner credentials on every fresh index fetch, forever. The prewarm records
    such paths beside the manifest cache and the warm check counts them as satisfied
    while that manifest cache is fresh."""

    @pytest.mark.asyncio
    async def test_a_missing_declared_image_costs_one_clone_per_manifest_lifetime(
        self, monkeypatch
    ):
        reg = _pin_registry(monkeypatch, _TRUST_OWNER)
        manifest = dict(MANIFEST, screenshots=["ui/shot-1.png", "ui/missing.png"])
        calls = _fake_fetch(monkeypatch, ART, manifest)
        entry = _entry()

        assert await _prewarm_owner_tier_store_assets(reg, [entry]) == 1
        assert len(calls) == 1
        assert _read_manifest_cache(entry) == manifest
        record = reg_mod._unobtainable_art_path(entry)
        assert record.parent == _manifest_cache_path(entry).parent
        assert json.loads(record.read_text(encoding="utf-8")) == ["ui/missing.png"]
        # The record never puts bytes where the proxy reads.
        assert not _store_art_cache_path(entry, "ui/missing.png").exists()

        # The same fresh rows again: the row is warm, nothing is cloned.
        assert await _prewarm_owner_tier_store_assets(reg, [entry]) == 0
        assert len(calls) == 1

    @pytest.mark.asyncio
    async def test_an_oversize_declared_image_is_recorded_too(self, monkeypatch):
        reg = _pin_registry(monkeypatch, _TRUST_OWNER)
        monkeypatch.setattr(reg_mod, "_ART_MAX_BYTES", 8)
        calls = _fake_fetch(monkeypatch, dict(ART, **{"ui/hero.png": b"x" * 9}))
        entry = _entry()

        assert await _prewarm_owner_tier_store_assets(reg, [entry]) == 1
        record = reg_mod._unobtainable_art_path(entry)
        assert json.loads(record.read_text(encoding="utf-8")) == ["ui/hero.png"]
        assert not _store_art_cache_path(entry, "ui/hero.png").exists()

        assert await _prewarm_owner_tier_store_assets(reg, [entry]) == 0
        assert len(calls) == 1

    @pytest.mark.asyncio
    async def test_a_record_older_than_the_manifest_cache_is_ignored(self, monkeypatch):
        reg = _pin_registry(monkeypatch, _TRUST_OWNER)
        manifest = dict(MANIFEST, screenshots=["ui/missing.png"])
        calls = _fake_fetch(monkeypatch, ART, manifest)
        entry = _entry()

        assert await _prewarm_owner_tier_store_assets(reg, [entry]) == 1
        record = reg_mod._unobtainable_art_path(entry)
        past = _manifest_cache_path(entry).stat().st_mtime - 60
        os.utime(record, (past, past))

        # The record describes an earlier manifest; the row is cold and re-cloned.
        assert await _prewarm_owner_tier_store_assets(reg, [entry]) == 1
        assert len(calls) == 2

    @pytest.mark.asyncio
    async def test_a_malformed_record_is_the_empty_set(self, monkeypatch):
        reg = _pin_registry(monkeypatch, _TRUST_OWNER)
        manifest = dict(MANIFEST, screenshots=["ui/missing.png"])
        calls = _fake_fetch(monkeypatch, ART, manifest)
        entry = _entry()

        assert await _prewarm_owner_tier_store_assets(reg, [entry]) == 1
        record = reg_mod._unobtainable_art_path(entry)
        manifest_mtime = _manifest_cache_path(entry).stat().st_mtime

        for body in ("not json", '{"ui/missing.png": true}', "[1, 2]"):
            record.write_text(body, encoding="utf-8")
            assert reg_mod._read_unobtainable_art(entry, manifest_mtime) == frozenset()

        # Cold again, because nothing is known to be unobtainable.
        assert await _prewarm_owner_tier_store_assets(reg, [entry]) == 1
        assert len(calls) == 2
        # And the rewrite restores a well-formed record.
        assert json.loads(record.read_text(encoding="utf-8")) == ["ui/missing.png"]

    @pytest.mark.asyncio
    async def test_a_record_is_removed_once_every_path_is_obtainable(self, monkeypatch):
        reg = _pin_registry(monkeypatch, _TRUST_OWNER)
        calls = _fake_fetch(monkeypatch, ART)
        entry = _entry()
        record = reg_mod._unobtainable_art_path(entry)
        record.parent.mkdir(parents=True, exist_ok=True)
        record.write_text('["ui/hero.png"]', encoding="utf-8")

        assert await _prewarm_owner_tier_store_assets(reg, [entry]) == 1
        assert len(calls) == 1
        assert not record.exists()

    def test_the_record_lands_where_the_manifest_gc_sweeps(self):
        entry = _entry()
        record = reg_mod._unobtainable_art_path(entry)
        assert record.parent == _manifest_cache_path(entry).parent
        assert record.name.endswith(".json")
        assert record.name.endswith(reg_mod._UNOBTAINABLE_ART_SUFFIX)


# ---------------------------------------------------------------------------
# A Git LFS pointer checked out in an image's place is not published
# ---------------------------------------------------------------------------


class TestLfsPointerIsUnobtainable:
    """A repository tracking an image through Git LFS checks out a tiny text POINTER
    in the image's place under the prewarm's plain clone (no ``git lfs``). Publishing
    that pointer would serve a few lines of ASCII as an ``<img>`` source. The copy
    sniffs the pointer preamble and records the asset unobtainable — never publishing
    it — exactly like an absent one, the code-side half of the publishing guide's
    'LFS is unsupported' contract."""

    _POINTER = (
        b"version https://git-lfs.github.com/spec/v1\n"
        b"oid sha256:1111111111111111111111111111111111111111111111111111111111111111\n"
        b"size 12345\n"
    )

    def test_an_lfs_pointer_is_recorded_unobtainable_and_not_published(
        self, monkeypatch, tmp_path, caplog
    ):
        reg = _pin_registry(monkeypatch, _TRUST_OWNER)  # scopes the blob cache dir
        assert reg is not None
        clone = tmp_path / "clone"
        (clone / "ui").mkdir(parents=True)
        # icon is a real image; hero is an LFS pointer standing in for the image.
        (clone / "ui" / "icon.svg").write_bytes(b"<svg/>")
        (clone / "ui" / "hero.png").write_bytes(self._POINTER)
        manifest = dict(MANIFEST, iconPath="ui/icon.svg", heroImage="ui/hero.png", screenshots=[])
        entry = _entry()

        with caplog.at_level("INFO", logger="kiro_crew.apps.registry"):
            written, unobtainable = _copy_declared_art(entry, clone, manifest)

        # The real image landed; the LFS pointer did not and is recorded unobtainable.
        assert written == 1
        assert _store_art_cache_path(entry, "ui/icon.svg").is_file()
        assert "ui/hero.png" in unobtainable
        assert not _store_art_cache_path(entry, "ui/hero.png").exists()
        # The pointer bytes are nowhere in the blob cache.
        assert self._POINTER not in b"".join(
            p.read_bytes() for p in _blob_cache_dir().rglob("*") if p.is_file()
        )
        # Logged once for the row.
        lfs_lines = [r for r in caplog.records if "Git LFS-tracked" in r.getMessage()]
        assert len(lfs_lines) == 1

    def test_it_logs_once_per_row_for_several_lfs_images(self, monkeypatch, tmp_path, caplog):
        reg = _pin_registry(monkeypatch, _TRUST_OWNER)
        assert reg is not None
        clone = tmp_path / "clone"
        (clone / "ui").mkdir(parents=True)
        for rel in ("ui/a.png", "ui/b.png", "ui/c.png"):
            (clone / rel).write_bytes(self._POINTER)
        manifest = dict(
            MANIFEST, iconPath="", heroImage="", screenshots=["ui/a.png", "ui/b.png", "ui/c.png"]
        )
        entry = _entry()

        with caplog.at_level("INFO", logger="kiro_crew.apps.registry"):
            written, unobtainable = _copy_declared_art(entry, clone, manifest)

        assert written == 0
        assert unobtainable == frozenset({"ui/a.png", "ui/b.png", "ui/c.png"})
        lfs_lines = [r for r in caplog.records if "Git LFS-tracked" in r.getMessage()]
        assert len(lfs_lines) == 1, "the LFS notice is one line per row, not per pointer"

    def test_the_lfs_sniff_can_fail_negative_control(self, monkeypatch, tmp_path):
        """Negative control: a file whose bytes are NOT the LFS preamble (an ordinary
        image that merely mentions the word 'version') IS published — proving the
        refusal above observes the preamble and not any fixture accident."""
        reg = _pin_registry(monkeypatch, _TRUST_OWNER)
        assert reg is not None
        clone = tmp_path / "clone"
        (clone / "ui").mkdir(parents=True)
        not_a_pointer = b"\x89PNG\r\n version https://git-lfs.github.com/spec/v1 in a comment"
        (clone / "ui" / "hero.png").write_bytes(not_a_pointer)
        manifest = dict(MANIFEST, iconPath="", heroImage="ui/hero.png", screenshots=[])
        entry = _entry()

        written, unobtainable = _copy_declared_art(entry, clone, manifest)

        assert written == 1
        assert "ui/hero.png" not in unobtainable
        cached = _store_art_cache_path(entry, "ui/hero.png")
        assert cached.is_file() and cached.read_bytes() == not_a_pointer


# ---------------------------------------------------------------------------
# A destination-write failure is not a source refusal
# ---------------------------------------------------------------------------


class TestDestinationWriteFailureIsNotUnobtainable:
    """A blob-cache WRITE failure is a transient destination problem, not a confirmed
    source refusal: the asset must stay cold (no cache file, NOT recorded as
    unobtainable) so the next fresh fetch retries it, rather than being pinned missing
    for the manifest's lifetime. Only a genuinely absent/refused SOURCE is recorded."""

    @pytest.mark.asyncio
    async def test_a_write_failure_leaves_the_asset_cold_while_a_missing_one_is_recorded(
        self, monkeypatch
    ):
        reg = _pin_registry(monkeypatch, _TRUST_OWNER)
        # ui/icon.svg's source exists but its WRITE fails; ui/missing.png's source is
        # absent from the clone (a genuine source refusal).
        manifest = dict(MANIFEST, iconPath="ui/icon.svg", screenshots=["ui/missing.png"])
        _fake_fetch(
            monkeypatch,
            {"ui/icon.svg": ART["ui/icon.svg"], "ui/hero.png": ART["ui/hero.png"]},
            manifest,
        )
        entry = _entry()

        real_atomic_write = reg_mod.atomic_write

        def _flaky_write(path, data, *args, **kwargs):
            # ``as_posix``: the blob-cache path carries the OS separator, and this
            # must fail the SAME asset on Windows.
            if Path(path).as_posix().endswith("ui/icon.svg"):
                raise OSError("blob cache write refused")
            return real_atomic_write(path, data)

        monkeypatch.setattr(reg_mod, "atomic_write", _flaky_write)

        assert await _prewarm_owner_tier_store_assets(reg, [entry]) == 1

        # The write-failed asset is neither written nor recorded unobtainable.
        assert not _store_art_cache_path(entry, "ui/icon.svg").exists()
        record = reg_mod._unobtainable_art_path(entry)
        recorded = json.loads(record.read_text(encoding="utf-8")) if record.exists() else []
        assert "ui/icon.svg" not in recorded
        # The genuinely absent source IS recorded.
        assert "ui/missing.png" in recorded
        # A cold (not recorded) asset keeps the row cold, so the next fetch retries it.
        assert not reg_mod._store_assets_warm(entry, _read_manifest_cache(entry))

    def test_a_write_failure_is_not_recorded_direct(self, monkeypatch, tmp_path):
        """Direct _copy_declared_art: a write failure records nothing for that asset;
        a missing source is recorded. Negative control below reverts the split."""
        clone = tmp_path / "clone"
        (clone / "ui").mkdir(parents=True)
        (clone / "ui" / "icon.svg").write_bytes(b"<svg/>")
        manifest = dict(MANIFEST, iconPath="ui/icon.svg", heroImage="ui/gone.png", screenshots=[])
        entry = _entry()

        def _flaky_write(path, data, *args, **kwargs):
            raise OSError("dest write refused")

        monkeypatch.setattr(reg_mod, "atomic_write", _flaky_write)
        written, unobtainable = _copy_declared_art(entry, clone, manifest)

        assert written == 0
        # icon.svg source is good but its write failed -> not recorded.
        assert "ui/icon.svg" not in unobtainable
        # gone.png source is absent -> recorded.
        assert "ui/gone.png" in unobtainable

    def test_the_split_can_fail_negative_control(self, monkeypatch, tmp_path):
        """Negative control: emulate the pre-fix behaviour (record before the copy,
        one except covering read+write) and observe that a write failure WOULD be
        recorded -- proving the assertions above observe the split."""
        clone = tmp_path / "clone"
        (clone / "ui").mkdir(parents=True)
        (clone / "ui" / "icon.svg").write_bytes(b"<svg/>")

        # Reproduce the pre-fix logic inline: add before the write, keep on failure.
        def _prefix_copy(entry, root, mdir, manifest):
            unobtainable = set()
            for asset in reg_mod._declared_store_art(manifest):
                cache_path = _store_art_cache_path(entry, asset)
                if cache_path is None:
                    continue
                unobtainable.add(asset)  # pre-fix: recorded before the copy attempt
                src = (mdir / asset).resolve()
                if not src.is_file():
                    continue
                try:
                    raise OSError("dest write refused")  # the write leg fails
                except OSError:
                    continue  # pre-fix: keeps the asset in unobtainable
            return 0, frozenset(unobtainable)

        manifest = dict(MANIFEST, iconPath="ui/icon.svg", heroImage="", screenshots=[])
        entry = _entry()
        _, unobtainable = _prefix_copy(entry, clone, clone, manifest)
        # Under the pre-fix behaviour the write-failed asset IS wrongly recorded.
        assert "ui/icon.svg" in unobtainable
