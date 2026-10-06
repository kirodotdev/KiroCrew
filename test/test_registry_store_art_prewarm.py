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
- the two call sites: the listing's cache miss and the explicit refresh (after its
  manifest-cache expiry, or the refresh would discard what it just fetched).
"""

from __future__ import annotations

import contextlib
import dataclasses
import json
import os
import subprocess
import sys
import threading
import time
from pathlib import Path
from typing import Any
from urllib.parse import parse_qs, urlparse

import pytest

from conftest import requires_o_nofollow, requires_symlinks
from kiro_crew.apps import registry as reg_mod
from kiro_crew.apps.registry import (
    _TRUST_INDEX,
    _TRUST_OWNER,
    _blob_cache_dir,
    _blob_cache_key,
    _copy_declared_art,
    _effective_registries,
    _entry_git_url,
    _manifest_cache_path,
    _manifest_source_coordinates,
    _merge_manifest,
    _open_pinned_asset,
    _prewarm_owner_tier_store_assets,
    _publish_pinned_asset,
    _read_manifest_cache,
    _servable_art_path,
    _store_art_cache_path,
)
from kiro_crew.config import loader as loader_mod
from kiro_crew.config.loader import KiroCrewConfig
from kiro_crew.platform.bootstrap import build_default_context
from kiro_crew.platform.defaults import DefaultAppsLoader
from kiro_crew.subprocess_utf8 import UTF8_TEXT

FORGE = "https://forge.example.com/org/app-registry.git"
SIBLING = "https://forge.example.com/org/some-private-app.git"
OTHER_HOST = "https://elsewhere.example.net/org/some-private-app.git"

MANIFEST: dict[str, Any] = {
    "name": "app",
    "displayName": "App",
    "description": "An app.",
    "iconPath": "ui/icon.svg",
    "heroImage": "ui/hero.png",
    "screenshots": ["ui/shot-1.png", "ui/shot-2.png"],
}

ART: dict[str, bytes] = {
    "ui/icon.svg": b"<svg/>",
    "ui/hero.png": b"\x89PNG hero",
    "ui/shot-1.png": b"\x89PNG one",
    "ui/shot-2.png": b"\x89PNG two",
}


class _Loader(DefaultAppsLoader):
    def __init__(self, rows) -> None:
        self._rows = rows

    def default_registries(self):
        return self._rows


def _pin_registry(monkeypatch, trust: str) -> Any:
    """Pin one registry at *trust* through the edition seam; return its config row."""
    cfg = KiroCrewConfig()
    cfg.registries = []
    monkeypatch.setattr(loader_mod.KiroCrewConfig, "load", staticmethod(lambda: cfg))
    base = build_default_context(KiroCrewConfig())
    ctx = dataclasses.replace(
        base,
        apps_loader=_Loader([{"name": "official", "repo": FORGE, "trust": trust}]),
    )
    monkeypatch.setattr(reg_mod, "current_context", lambda: ctx)
    (reg,) = _effective_registries()
    return reg


def _entry(**overrides: Any) -> dict[str, Any]:
    row = {
        "name": "app",
        "gitUrl": SIBLING,
        "repo": SIBLING,
        "branch": "main",
        "_registry": "official",
    }
    row.update(overrides)
    return row


def _fake_fetch(monkeypatch, tree: dict[str, bytes], manifest: dict[str, Any] | None = MANIFEST):
    """Stand in for ``_git_fetch_branch``: lay *tree* (and app.json) into ``dest``.

    Patched through the facade so every holder -- including ``store_art`` -- sees it.
    Returns the list of recorded calls.
    """
    calls: list[dict[str, Any]] = []

    async def _fetch(git_url, branch, dest, log_lines, *, clone_env, sandbox_mode, **kw):
        calls.append(
            {
                "git_url": git_url,
                "branch": branch,
                "dest": Path(dest),
                "clone_env": dict(clone_env),
                "sandbox_mode": sandbox_mode,
                "mask_local_git_config": kw.get("mask_local_git_config", False),
            }
        )
        dest = Path(dest)
        dest.mkdir(parents=True, exist_ok=True)
        if manifest is not None:
            (dest / "app.json").write_text(json.dumps(manifest), encoding="utf-8")
        for rel, data in tree.items():
            path = dest / rel
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_bytes(data)
        return None

    monkeypatch.setattr(reg_mod, "_git_fetch_branch", _fetch)
    return calls


def _route_cache_path(entry: dict[str, Any], merged_url: str) -> Path:
    """Where ``handle_blob_proxy`` looks for the URL ``_merge_manifest`` emitted.

    Reproduces the route's own resolution -- ``repo`` and ``path`` from the query,
    ``ref`` from the row's branch (the URL carries none), ``clone_url`` from the row --
    so the test asserts against what the proxy DOES with the URL, not against the
    prewarm's idea of it.
    """
    query = parse_qs(urlparse(merged_url).query)
    (repo,) = query["repo"]
    (path,) = query["path"]
    ref = entry.get("branch", "main")
    return _blob_cache_dir() / _blob_cache_key(repo, _entry_git_url(entry)) / ref / path


#: Lost-run ceiling on a barrier the production path always reaches (a fetch held
#: in flight, the copy worker entering, a cancelled row's cleanup). The wait returns
#: the moment the state is reached; the slowest path to these barriers is the
#: pre-batch and pre-copy thread hops, a few seconds with every executor job started
#: late. Reaching the ceiling fails naming the barrier, and it is a quarter of the
#: suite's 120 s ``--timeout``, so a miss is a readable failure, not a killed worker.
_BARRIER_CEILING_SECS = 30.0


async def _await_barrier(reached: Any, what: str, state: Any = lambda: "") -> None:
    """Wait for *reached* (an ``asyncio.Event``) within the ceiling, failing by name.

    Call it from the TEST, never from a fake the prewarm awaits: ``_worker`` logs and
    swallows any exception a row raises, ``pytest.fail`` included.
    """
    import asyncio

    loop = asyncio.get_running_loop()
    started = loop.time()
    try:
        await asyncio.wait_for(reached.wait(), timeout=_BARRIER_CEILING_SECS)
    except asyncio.TimeoutError:
        pytest.fail(
            f"{what} not reached after {loop.time() - started:.1f}s "
            f"(lost-run ceiling {_BARRIER_CEILING_SECS:.0f}s); {state()}"
        )


async def _settled(aw: Any, what: str) -> Any:
    """Await *aw* within the lost-run ceiling; past it fail naming ``what`` and the wait."""
    import asyncio

    loop = asyncio.get_running_loop()
    started = loop.time()
    try:
        return await asyncio.wait_for(aw, timeout=_BARRIER_CEILING_SECS)
    except asyncio.TimeoutError:
        pytest.fail(
            f"{what} still running after {loop.time() - started:.1f}s "
            f"(lost-run ceiling {_BARRIER_CEILING_SECS:.0f}s)"
        )


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
    reliable witness of it: ``test_external_registry.py`` reloads the registry facade,
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
        task must be cancelled, not abandoned. The outer ``wait_for`` turns a missing
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
            fetched = await asyncio.wait_for(_prewarm_owner_tier_store_assets(reg, rows), timeout=3)

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


# ---------------------------------------------------------------------------
# Cancellation settles the copy without recording a partial run
# ---------------------------------------------------------------------------


class TestCopyCancellation:
    """A budget cancellation must not leave the copy worker writing a partial result:
    no ``.unobtainable`` sidecar and no not-yet-copied path may be recorded when the
    copy is cancelled, and the caller settles the worker before the checkout is
    removed. A completed row still lands."""

    def test_a_preset_cancel_records_nothing_and_writes_nothing(self, monkeypatch, tmp_path):
        clone = tmp_path / "clone"
        (clone / "ui").mkdir(parents=True)
        for rel, data in ART.items():
            (clone / rel).parent.mkdir(parents=True, exist_ok=True)
            (clone / rel).write_bytes(data)
        entry = _entry()
        cancel = threading.Event()
        cancel.set()  # cancelled before the first asset

        written, unobtainable = _copy_declared_art(entry, clone, MANIFEST, cancel)

        assert written == 0
        assert unobtainable == frozenset()
        # No sidecar written on a cancelled run.
        assert not reg_mod._unobtainable_art_path(entry).exists()
        # No art landed.
        assert not any(p.is_file() for p in _blob_cache_dir().rglob("*"))

    def test_cancellation_mid_run_records_nothing_but_a_completed_row_lands(
        self, monkeypatch, tmp_path
    ):
        clone = tmp_path / "clone"
        (clone / "ui").mkdir(parents=True)
        for rel, data in ART.items():
            (clone / rel).parent.mkdir(parents=True, exist_ok=True)
            (clone / rel).write_bytes(data)
        # A manifest declaring a missing path: a full run WOULD record it. Cancelling
        # partway must NOT record it.
        manifest = dict(MANIFEST, screenshots=["ui/shot-1.png", "ui/missing.png"])
        entry = _entry()

        cancel = threading.Event()
        real_write = reg_mod.atomic_write
        seen: list[str] = []

        def _write_then_cancel(path, data, *args, **kwargs):
            seen.append(str(path))
            real_write(path, data, *args, **kwargs)
            cancel.set()  # after the first successful write, request cancellation

        monkeypatch.setattr(reg_mod, "atomic_write", _write_then_cancel)
        written, unobtainable = _copy_declared_art(entry, clone, manifest, cancel)

        # The run stopped early and recorded nothing (no sidecar), even though a path
        # was missing.
        assert unobtainable == frozenset()
        assert not reg_mod._unobtainable_art_path(entry).exists()

        # A completed (uncancelled) run of the same fixture records the missing path.
        monkeypatch.setattr(reg_mod, "atomic_write", real_write)
        written2, unobtainable2 = _copy_declared_art(entry, clone, manifest, threading.Event())
        assert "ui/missing.png" in unobtainable2

    @pytest.mark.asyncio
    async def test_a_budget_cancellation_settles_the_copy_before_cleanup(self, monkeypatch):
        """End to end: the batch budget cancels an in-flight copy. The worker settles
        (records nothing) and the scratch clone is removed after it returns -- no
        sidecar and no partial art land for the cancelled row."""
        import asyncio

        reg = _pin_registry(monkeypatch, _TRUST_OWNER)

        started = asyncio.Event()

        # A copy that blocks until cancelled: the cancel event stops it at the first
        # per-asset check, so it returns without recording.
        real_copy = reg_mod._copy_declared_art

        def _slow_copy(entry, mdir, manifest, cancel=None):
            started.set()
            # Busy-wait on the cooperative cancel flag, then defer to the real copy so
            # the cancelled-path early-return is exercised.
            while cancel is not None and not cancel.is_set():
                time.sleep(0.01)
            return real_copy(entry, mdir, manifest, cancel)

        _fake_fetch(
            monkeypatch, ART, dict(MANIFEST, screenshots=["ui/shot-1.png", "ui/missing.png"])
        )
        monkeypatch.setattr(reg_mod, "_copy_declared_art", _slow_copy)
        monkeypatch.setattr(reg_mod, "_PREWARM_BATCH_BUDGET", 0.2)
        entry = _entry()

        fetched = await asyncio.wait_for(_prewarm_owner_tier_store_assets(reg, [entry]), timeout=5)

        assert fetched == 0, "the cancelled row is not counted as fetched"
        # No sidecar and no partial art for the cancelled row.
        assert not reg_mod._unobtainable_art_path(entry).exists()
        assert not any(p.is_file() for p in _blob_cache_dir().rglob("*"))

    @pytest.mark.asyncio
    async def test_the_checkout_is_removed_only_after_the_worker_returned(self, monkeypatch):
        """The ordering the settle exists for: when a row is cancelled while its copy
        is in flight, the worker has RETURNED before the scratch checkout is removed.
        Observed at the removal call itself, so a cancellation that merely propagated
        out (leaving the thread live) is what this test turns red on.

        The cancel is sent once the copy worker is OBSERVED running, never by a short
        batch budget: the thread hops before the copy race any budget, and a budget
        that wins cancels a row with no worker to settle. The budget path and an outer
        cancel reach the same settle."""
        import asyncio

        reg = _pin_registry(monkeypatch, _TRUST_OWNER)
        loop = asyncio.get_running_loop()
        copy_entered = asyncio.Event()
        returned = threading.Event()
        removal_saw_worker_returned: list[bool] = []
        real_copy = reg_mod._copy_declared_art
        real_rmtree = reg_mod._rmtree_force_settled

        def _slow_copy(entry, mdir, manifest, cancel=None):
            loop.call_soon_threadsafe(copy_entered.set)
            while cancel is not None and not cancel.is_set():
                time.sleep(0.01)
            # Linger past the flag: a caller that does not wait for the return
            # reaches the removal while this thread is still here.
            time.sleep(0.2)
            result = real_copy(entry, mdir, manifest, cancel)
            returned.set()
            return result

        async def _rmtree(path):
            removal_saw_worker_returned.append(returned.is_set())
            await real_rmtree(path)

        _fake_fetch(monkeypatch, ART, dict(MANIFEST, screenshots=["ui/shot-1.png"]))
        monkeypatch.setattr(reg_mod, "_copy_declared_art", _slow_copy)
        monkeypatch.setattr(reg_mod, "_rmtree_force_settled", _rmtree)
        # A batch budget that never fires: the only cancellation is the one below.
        monkeypatch.setattr(reg_mod, "_PREWARM_BATCH_BUDGET", 3600.0)

        task = asyncio.ensure_future(_prewarm_owner_tier_store_assets(reg, [_entry()]))
        try:
            await _await_barrier(
                copy_entered, "the copy worker", lambda: f"task.done()={task.done()}"
            )
        finally:
            # On every path: a cancel is what sets the copy's flag and lets it return.
            task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await _settled(task, "the cancelled prewarm")

        assert removal_saw_worker_returned == [True], removal_saw_worker_returned


# ---------------------------------------------------------------------------
# The two call sites
# ---------------------------------------------------------------------------


class TestCallSites:
    @pytest.mark.asyncio
    async def test_the_listing_prewarms_on_a_fresh_fetch_and_never_on_a_cache_hit(
        self, monkeypatch
    ):
        reg = _pin_registry(monkeypatch, _TRUST_OWNER)
        rows = [_entry()]
        warmed: list[list[dict[str, Any]]] = []

        async def _prewarm(r, entries):
            assert r == reg
            warmed.append(entries)
            return 0

        async def _fetch(r):
            return rows

        monkeypatch.setattr(reg_mod, "_prewarm_owner_tier_store_assets", _prewarm)
        monkeypatch.setattr(reg_mod, "_fetch_and_cache_external_registry", _fetch)
        cache: dict[str, Any] = {"rows": None}
        monkeypatch.setattr(
            reg_mod,
            "_read_external_registry_cache",
            lambda name, *, ignore_ttl=False: cache["rows"],
        )

        assert await reg_mod._load_external_registries() == rows
        assert warmed == [rows], "a fresh fetch must hand its rows to the prewarm"

        # The cached path: the same rows, read back from disk, must NOT be prewarmed.
        cache["rows"] = [dict(r) for r in rows]
        await reg_mod._load_external_registries()
        assert len(warmed) == 1

    @pytest.mark.asyncio
    async def test_the_prewarm_runs_after_every_sibling_has_written_its_cache(self, monkeypatch):
        """Two registries fetched fresh and concurrently. The prewarm for either runs
        only after BOTH fetches have finished (and so written their caches): the
        provenance gate reads the sibling's cache, and a prewarm run inside the
        per-registry load would find it unwritten on a first load and skip every
        row as ambiguous."""
        import asyncio

        cfg = KiroCrewConfig()
        cfg.registries = []
        monkeypatch.setattr(loader_mod.KiroCrewConfig, "load", staticmethod(lambda: cfg))
        base = build_default_context(KiroCrewConfig())
        ctx = dataclasses.replace(
            base,
            apps_loader=_Loader(
                [
                    {"name": "official", "repo": FORGE, "trust": _TRUST_OWNER},
                    {"name": "second", "repo": OTHER_HOST, "trust": _TRUST_OWNER},
                ]
            ),
        )
        monkeypatch.setattr(reg_mod, "current_context", lambda: ctx)
        regs = _effective_registries()
        assert [r.name for r in regs] == ["official", "second"]

        fetched: set[str] = set()
        seen_at_prewarm: list[frozenset[str]] = []

        async def _fetch(r):
            # The slow sibling finishes last; a prewarm inside the per-registry
            # load would run for "official" while "second" is still unfetched.
            if r.name == "second":
                await asyncio.sleep(0.05)
            fetched.add(r.name)
            return [dict(_entry(), _registry=r.name)]

        async def _prewarm(r, entries):
            seen_at_prewarm.append(frozenset(fetched))
            return 0

        monkeypatch.setattr(reg_mod, "_fetch_and_cache_external_registry", _fetch)
        monkeypatch.setattr(reg_mod, "_prewarm_owner_tier_store_assets", _prewarm)
        monkeypatch.setattr(
            reg_mod,
            "_read_external_registry_cache",
            lambda name, *, ignore_ttl=False: None,
        )

        rows = await reg_mod._load_external_registries()

        assert len(rows) == 2
        assert seen_at_prewarm == [frozenset({"official", "second"})] * 2, seen_at_prewarm

    @pytest.mark.asyncio
    async def test_refresh_prewarms_after_expiring_the_manifest_caches(self, monkeypatch):
        reg = _pin_registry(monkeypatch, _TRUST_OWNER)
        rows = [_entry()]
        order: list[str] = []

        async def _fetch(r):
            return rows

        async def _prewarm(r, entries):
            assert r == reg and entries is rows
            order.append("prewarm")
            return 1

        async def _list():
            return rows

        monkeypatch.setattr(reg_mod, "_fetch_and_cache_external_registry", _fetch)
        monkeypatch.setattr(reg_mod, "_prewarm_owner_tier_store_assets", _prewarm)
        monkeypatch.setattr(reg_mod, "_expire_cache_file", lambda path: order.append("expire"))
        monkeypatch.setattr(
            reg_mod,
            "_read_external_registry_cache",
            lambda name, *, ignore_ttl=False: None,
        )
        monkeypatch.setattr(reg_mod, "list_registry", _list)

        result = await reg_mod.refresh_registries()

        assert result["ok"] is True
        assert "prewarm" in order and "expire" in order
        assert order.index("prewarm") > max(i for i, step in enumerate(order) if step == "expire")

    @pytest.mark.asyncio
    async def test_refresh_prewarms_only_after_every_registry_has_written_its_cache(
        self, monkeypatch
    ):
        """Two registries refreshed in one call. ``refresh_registries`` fetches them
        one after another, so a prewarm run inside the per-registry loop for the
        FIRST registry would read the SECOND's index cache before that refetch had
        written it -- and the provenance gate counts an absent sibling cache as a
        possible claimant, leaving every row cold. Both prewarms must therefore run
        only after both registries have been fetched and had their manifest caches
        expired, in refresh order, each with its own fresh rows."""
        cfg = KiroCrewConfig()
        cfg.registries = []
        monkeypatch.setattr(loader_mod.KiroCrewConfig, "load", staticmethod(lambda: cfg))
        base = build_default_context(KiroCrewConfig())
        ctx = dataclasses.replace(
            base,
            apps_loader=_Loader(
                [
                    {"name": "official", "repo": FORGE, "trust": _TRUST_OWNER},
                    {"name": "second", "repo": OTHER_HOST, "trust": _TRUST_OWNER},
                ]
            ),
        )
        monkeypatch.setattr(reg_mod, "current_context", lambda: ctx)
        regs = _effective_registries()
        assert [r.name for r in regs] == ["official", "second"]

        fetched: list[str] = []
        expired: list[str] = []
        rows_by_name = {r.name: [dict(_entry(), _registry=r.name)] for r in regs}
        seen_at_prewarm: list[tuple[str, frozenset[str], int, list[dict[str, Any]]]] = []

        async def _fetch(r):
            # The index cache is written by this fetch (the real one caches on
            # success); recording the name is the cache-written marker.
            fetched.append(r.name)
            return rows_by_name[r.name]

        async def _prewarm(r, entries):
            seen_at_prewarm.append((r.name, frozenset(fetched), len(expired), entries))
            return 0

        async def _list():
            return []

        monkeypatch.setattr(reg_mod, "_fetch_and_cache_external_registry", _fetch)
        monkeypatch.setattr(reg_mod, "_prewarm_owner_tier_store_assets", _prewarm)
        monkeypatch.setattr(reg_mod, "_expire_cache_file", lambda path: expired.append(str(path)))
        monkeypatch.setattr(
            reg_mod,
            "_read_external_registry_cache",
            lambda name, *, ignore_ttl=False: None,
        )
        monkeypatch.setattr(reg_mod, "list_registry", _list)

        result = await reg_mod.refresh_registries()

        assert result["ok"] is True
        assert result["refreshed"] == ["official", "second"]
        assert fetched == ["official", "second"]
        # One manifest expiry per registry row happened BEFORE either prewarm ran.
        assert len(expired) == 2, expired
        assert [name for name, _f, _e, _rows in seen_at_prewarm] == ["official", "second"]
        for name, fetched_at, expired_at, rows in seen_at_prewarm:
            assert fetched_at == frozenset({"official", "second"}), (name, fetched_at)
            assert expired_at == 2, (name, expired_at)
            assert rows is rows_by_name[name], "each prewarm gets its own fresh rows"


# ---------------------------------------------------------------------------
# The manifest size cap
# ---------------------------------------------------------------------------


class TestManifestSizeCap:
    @pytest.mark.asyncio
    async def test_an_oversize_manifest_is_refused_before_reading(self, monkeypatch):
        """An app.json over the cap makes the row return False and caches nothing.

        The ceiling is enforced by the pinned read's descriptor-size validation
        (``_open_pinned_asset`` fstat-gates the opened fd against
        ``max_bytes=_MANIFEST_MAX_BYTES``), so an
        oversize manifest is a source refusal exactly like a symlinked or absent one --
        no separate by-path ``os.stat`` pre-check, no distinct warning."""
        reg = _pin_registry(monkeypatch, _TRUST_OWNER)
        monkeypatch.setattr(reg_mod, "_MANIFEST_MAX_BYTES", 8)
        big_manifest = dict(MANIFEST, description="x" * 64)
        _fake_fetch(monkeypatch, ART, big_manifest)
        entry = _entry()

        assert await _prewarm_owner_tier_store_assets(reg, [entry]) == 0

        assert _read_manifest_cache(entry) is None
        assert not any(p.is_file() for p in _blob_cache_dir().rglob("*"))

    @pytest.mark.asyncio
    async def test_a_manifest_under_the_cap_is_read(self, monkeypatch):
        """Negative control: with the cap raised the same manifest IS read and cached,
        so the refusal above observes the cap and not an accident of the fixture."""
        reg = _pin_registry(monkeypatch, _TRUST_OWNER)
        monkeypatch.setattr(reg_mod, "_MANIFEST_MAX_BYTES", 1024 * 1024)
        big_manifest = dict(MANIFEST, description="x" * 64)
        _fake_fetch(monkeypatch, ART, big_manifest)
        entry = _entry()

        assert await _prewarm_owner_tier_store_assets(reg, [entry]) == 1
        assert _read_manifest_cache(entry) == big_manifest


# ---------------------------------------------------------------------------
# The rewarm-age import guard
# ---------------------------------------------------------------------------


class TestRewarmAgeGuard:
    def test_a_non_positive_window_is_refused(self):
        from kiro_crew.apps.registry_pipeline import store_art

        # A manifest TTL that does not exceed the index TTL is the misconfiguration
        # that turns every fresh fetch into a full credentialed re-clone.
        with pytest.raises(RuntimeError, match="_REWARM_AGE must be positive"):
            store_art._check_rewarm_age(100.0, 100.0)
        with pytest.raises(RuntimeError, match="_REWARM_AGE must be positive"):
            store_art._check_rewarm_age(100.0, 200.0)

    def test_a_positive_window_is_accepted(self):
        from kiro_crew.apps.registry_pipeline import store_art

        # Negative control: a manifest TTL wider than the index TTL passes, so the
        # guard is observing the sign of the difference and not always raising.
        store_art._check_rewarm_age(200.0, 100.0)


# ---------------------------------------------------------------------------
# The checkout hooks-path neutralizer
# ---------------------------------------------------------------------------


class TestFetchRefHooksNeutralizer:
    """Every git invocation ``_git_fetch_ref`` spawns -- init, remote add, fetch,
    checkout, branch, network or not -- must carry the hooks/fsmonitor neutralizer,
    so a repo shipping ``.githooks/post-checkout`` plus a global ``core.hooksPath``
    cannot run code during the local checkout step (which runs with the plain
    ``clone_env``, not the network transport env)."""

    @staticmethod
    async def _run_fetch(monkeypatch, tmp_path, neutralize=True):
        from kiro_crew.apps.registry_pipeline import checkout as co

        spawned: list[list[str]] = []

        class _Proc:
            returncode = 0

            async def communicate(self):
                return (b"", b"")

        async def _spawn(*args, **kwargs):
            spawned.append(list(args))
            return _Proc()

        async def _wrap(argv, *, mode, _prepare):
            return list(argv), None

        monkeypatch.setattr(co, "wrap_argv_async", _wrap)
        monkeypatch.setattr(co, "cgroup_scope_argv", lambda a: a)
        monkeypatch.setattr(co, "create_subprocess_limited", _spawn)
        if not neutralize:
            monkeypatch.setattr(co, "_HOOKS_NEUTRALIZER_ARGV", ())

        dest = tmp_path / "clone"
        err = await co._git_fetch_branch(
            "https://forge.example.com/org/app.git",
            "main",
            dest,
            [],
            clone_env={"BASE": "1"},
            sandbox_mode="strict",
        )
        return err, spawned

    @pytest.mark.asyncio
    async def test_every_spawn_carries_the_neutralizer(self, monkeypatch, tmp_path):
        import os

        err, spawned = await self._run_fetch(monkeypatch, tmp_path)
        assert err is None
        assert spawned, "no git process was spawned"
        for argv in spawned:
            assert argv[0] == "git"
            assert f"core.hooksPath={os.devnull}" in argv, argv
            assert "core.fsmonitor=false" in argv, argv
            # The neutralizer precedes the git subcommand.
            first_c = argv.index("-c")
            subcommand = next(tok for tok in argv[1:] if not tok.startswith("-") and "=" not in tok)
            assert first_c < argv.index(subcommand), argv

    @pytest.mark.asyncio
    async def test_the_neutralizer_can_be_absent(self, monkeypatch, tmp_path):
        """Negative control: with the neutralizer emptied no spawn carries it, proving
        the assertion above observes the injection and not an unconditional truth."""
        import os

        err, spawned = await self._run_fetch(monkeypatch, tmp_path, neutralize=False)
        assert err is None
        assert spawned
        assert all(f"core.hooksPath={os.devnull}" not in argv for argv in spawned)


# ---------------------------------------------------------------------------
# A clone-LEVEL failure backs the row off instead of stalling every fetch
# ---------------------------------------------------------------------------


def _failing_fetch(monkeypatch, error="git fetch failed (exit 128)"):
    """Stand in for ``_git_fetch_branch`` that always fails; returns the call log.

    A clone-level failure writes no manifest and no unobtainable record, so without
    the ``.clone-failed`` backoff the row would be re-cloned on every fresh fetch."""
    calls: list[str] = []

    async def _fetch(git_url, branch, dest, log_lines, *, clone_env, sandbox_mode, **kw):
        calls.append(git_url)
        return {"ok": False, "name": "app", "error": error}

    monkeypatch.setattr(reg_mod, "_git_fetch_branch", _fetch)
    return calls


class TestCloneFailureBackoff:
    """A clone that fails outright -- deleted repo, revoked credential, a forge that
    hangs to the batch budget -- writes no manifest and no unobtainable record, so a
    warm check that needs a manifest can never mark the row satisfied and the store
    listing that misses the index cache pays the whole clone attempt on every fresh
    index fetch, indefinitely. The prewarm records the failure beside the row's
    manifest cache and its pre-filter backs the row off for ``_CLONE_FAILURE_BACKOFF``."""

    @pytest.mark.asyncio
    async def test_a_failed_clone_records_and_the_next_prewarm_within_backoff_clones_nothing(
        self, monkeypatch
    ):
        reg = _pin_registry(monkeypatch, _TRUST_OWNER)
        calls = _failing_fetch(monkeypatch)
        entry = _entry()

        assert await _prewarm_owner_tier_store_assets(reg, [entry]) == 0
        assert len(calls) == 1
        record = reg_mod._clone_failure_path(entry)
        assert record.parent == _manifest_cache_path(entry).parent
        recorded = json.loads(record.read_text(encoding="utf-8"))
        assert isinstance(recorded["at"], (int, float))
        assert isinstance(recorded["reason"], str) and recorded["reason"]
        # No manifest and no art landed for a clone-level failure.
        assert _read_manifest_cache(entry) is None

        # Within the backoff window the row is skipped: no second clone.
        assert await _prewarm_owner_tier_store_assets(reg, [entry]) == 0
        assert len(calls) == 1

    def test_an_unrepresentable_timestamp_reads_as_absent(self, monkeypatch):
        """A forged ``at`` too large for a float must not crash the pre-filter."""
        _pin_registry(monkeypatch, _TRUST_OWNER)
        entry = _entry()
        record = reg_mod._clone_failure_path(entry)
        record.parent.mkdir(parents=True, exist_ok=True)
        record.write_text('{"at": 1' + "0" * 400 + ', "reason": "x"}', encoding="utf-8")
        assert reg_mod._read_clone_failure(entry) is None

    @pytest.mark.skipif(not hasattr(os, "mkfifo"), reason="needs mkfifo")
    def test_a_fifo_record_is_refused_without_blocking(self, monkeypatch):
        """A FIFO planted at the record name has no writer; the open must not block."""
        _pin_registry(monkeypatch, _TRUST_OWNER)
        entry = _entry()
        record = reg_mod._clone_failure_path(entry)
        record.parent.mkdir(parents=True, exist_ok=True)
        os.mkfifo(record)
        result: list[Any] = []
        worker = threading.Thread(
            target=lambda: result.append(reg_mod._read_clone_failure(entry)), daemon=True
        )
        worker.start()
        worker.join(timeout=5)
        if worker.is_alive():
            # Unblock the stuck open so the session does not hang, then fail.
            fd = os.open(record, os.O_WRONLY | os.O_NONBLOCK)
            os.close(fd)
            worker.join(timeout=5)
            pytest.fail("reading a FIFO clone-failure record blocked")
        assert result == [None]

    @pytest.mark.asyncio
    async def test_the_backoff_can_fail_negative_control(self, monkeypatch):
        """Negative control: with the failure record read as always-absent (the pre-fix
        state), the same failed row IS re-cloned on the next prewarm -- proving the skip
        observes the record and not an accident of the fixture."""
        reg = _pin_registry(monkeypatch, _TRUST_OWNER)
        calls = _failing_fetch(monkeypatch)
        entry = _entry()

        assert await _prewarm_owner_tier_store_assets(reg, [entry]) == 0
        assert len(calls) == 1

        monkeypatch.setattr(reg_mod, "_read_clone_failure", lambda entry: None)
        assert await _prewarm_owner_tier_store_assets(reg, [entry]) == 0
        assert len(calls) == 2, "without the backoff the dead row is re-cloned every fetch"

    @pytest.mark.asyncio
    async def test_a_record_older_than_the_backoff_is_ignored_and_the_row_retried(
        self, monkeypatch
    ):
        reg = _pin_registry(monkeypatch, _TRUST_OWNER)
        calls = _failing_fetch(monkeypatch)
        entry = _entry()

        assert await _prewarm_owner_tier_store_assets(reg, [entry]) == 0
        assert len(calls) == 1
        record = reg_mod._clone_failure_path(entry)
        stale = {"at": time.time() - reg_mod._CLONE_FAILURE_BACKOFF - 60, "reason": "clone failed"}
        record.write_text(json.dumps(stale), encoding="utf-8")

        # The window has lapsed, so the row is retried.
        assert await _prewarm_owner_tier_store_assets(reg, [entry]) == 0
        assert len(calls) == 2

    def test_a_record_within_the_backoff_reads_its_timestamp_and_older_reads_none(
        self, monkeypatch
    ):
        entry = _entry()
        record = reg_mod._clone_failure_path(entry)
        record.parent.mkdir(parents=True, exist_ok=True)

        fresh_at = time.time() - 60
        record.write_text(json.dumps({"at": fresh_at, "reason": "clone failed"}), encoding="utf-8")
        assert reg_mod._read_clone_failure(entry) == pytest.approx(fresh_at)

        old_at = time.time() - reg_mod._CLONE_FAILURE_BACKOFF - 1
        record.write_text(json.dumps({"at": old_at, "reason": "clone failed"}), encoding="utf-8")
        assert reg_mod._read_clone_failure(entry) is None

    @pytest.mark.asyncio
    async def test_a_malformed_record_is_ignored_and_the_row_retried(self, monkeypatch):
        reg = _pin_registry(monkeypatch, _TRUST_OWNER)
        calls = _failing_fetch(monkeypatch)
        entry = _entry()

        assert await _prewarm_owner_tier_store_assets(reg, [entry]) == 0
        record = reg_mod._clone_failure_path(entry)

        for body in ("not json", "[1, 2]", '{"reason": "x"}', '{"at": "soon"}', '{"at": true}'):
            record.write_text(body, encoding="utf-8")
            assert reg_mod._read_clone_failure(entry) is None, body

        # A malformed record leaves the row cold, so the next prewarm re-clones it.
        record.write_text("not json", encoding="utf-8")
        assert await _prewarm_owner_tier_store_assets(reg, [entry]) == 0
        assert len(calls) == 2

    @pytest.mark.asyncio
    async def test_a_successful_clone_removes_an_earlier_record(self, monkeypatch):
        reg = _pin_registry(monkeypatch, _TRUST_OWNER)
        entry = _entry()
        # Plant a record older than the backoff so the pre-filter lets the clone run;
        # a fresh record would (correctly) back the row off before any clone. The
        # clone then succeeds and its success path clears the stale record.
        record = reg_mod._clone_failure_path(entry)
        record.parent.mkdir(parents=True, exist_ok=True)
        stale = {"at": time.time() - reg_mod._CLONE_FAILURE_BACKOFF - 60, "reason": "clone failed"}
        record.write_text(json.dumps(stale), encoding="utf-8")
        calls = _fake_fetch(monkeypatch, ART)

        assert await _prewarm_owner_tier_store_assets(reg, [entry]) == 1
        assert len(calls) == 1
        assert not record.exists(), "a reaching clone must clear the dead-row record"
        assert _read_manifest_cache(entry) == MANIFEST

    @pytest.mark.asyncio
    async def test_a_budget_cancelled_row_writes_no_record(self, monkeypatch):
        """A row cancelled at the batch budget is not a dead row: its clone never
        returned an error, so no ``.clone-failed`` record is written and the next fresh
        fetch retries it immediately rather than backing it off."""
        import asyncio

        reg = _pin_registry(monkeypatch, _TRUST_OWNER)

        async def _hanging_fetch(
            git_url, branch, dest, log_lines, *, clone_env, sandbox_mode, **kw
        ):
            await asyncio.Event().wait()  # never returns; cancelled at the budget
            return None

        monkeypatch.setattr(reg_mod, "_git_fetch_branch", _hanging_fetch)
        monkeypatch.setattr(reg_mod, "_PREWARM_BATCH_BUDGET", 0.2)
        entry = _entry()

        fetched = await asyncio.wait_for(_prewarm_owner_tier_store_assets(reg, [entry]), timeout=5)
        assert fetched == 0
        assert not reg_mod._clone_failure_path(entry).exists()

    @pytest.mark.asyncio
    async def test_the_recorded_reason_carries_no_credential(self, monkeypatch):
        """The record's ``reason`` is a fixed class label, never raw git output: an
        owner-credentialed clone's error text can echo the expanded credential-bearing
        URL, so a secret in the error must not reach the record."""
        reg = _pin_registry(monkeypatch, _TRUST_OWNER)
        secret = "hunter2"
        # A hostile error string carrying a credential-bearing URL.
        _failing_fetch(
            monkeypatch,
            error=f"fatal: could not read from https://user:{secret}@forge.example.com/x.git",
        )
        entry = _entry()

        assert await _prewarm_owner_tier_store_assets(reg, [entry]) == 0
        recorded = json.loads(reg_mod._clone_failure_path(entry).read_text(encoding="utf-8"))
        assert secret not in recorded["reason"]
        assert "user:" not in recorded["reason"]
        # It is a constant allowlisted class or the fixed fallback, never a slice.
        assert recorded["reason"] in {
            label for _marker, label in reg_mod._GIT_FAILURE_CLASS_LABELS
        } | {"clone failed"}

    def test_the_reason_reduces_free_text_to_a_constant(self):
        """_clone_failure_reason maps a known class to its label and anything else to
        the fixed fallback -- never a slice of the input."""
        assert reg_mod._clone_failure_reason({"error": "fatal: could not resolve host"}) == (
            "host could not be resolved"
        )
        assert reg_mod._clone_failure_reason({"error": "git fetch failed (exit 128)"}) == (
            "clone failed"
        )
        assert reg_mod._clone_failure_reason("bare string") == "clone failed"

    def test_the_record_lands_where_the_manifest_gc_sweeps(self):
        entry = _entry()
        record = reg_mod._clone_failure_path(entry)
        assert record.parent == _manifest_cache_path(entry).parent
        assert record.name.endswith(".json")
        assert record.name.endswith(reg_mod._CLONE_FAILURE_SUFFIX)
        # Distinct from the unobtainable record and from a manifest cache file.
        assert record != reg_mod._unobtainable_art_path(entry)
        assert record != _manifest_cache_path(entry)


# ---------------------------------------------------------------------------
# The asset read is a pinned, no-follow descriptor walk
# ---------------------------------------------------------------------------


def _clone_with(tmp_path: Path, tree: dict[str, bytes]) -> Path:
    """Lay *tree* into a fresh clone directory under *tmp_path* and return it."""
    clone = tmp_path / "clone"
    for rel, data in tree.items():
        path = clone / rel
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(data)
    clone.mkdir(parents=True, exist_ok=True)
    return clone


class TestPinnedNoFollowAssetRead:
    """``_copy_declared_art`` reads each declared asset through
    ``_open_pinned_asset``: a no-follow descriptor walk from the clone directory that
    refuses a link at ANY path component and validates the same opened inode it reads
    from. This closes the TOCTOU in the old by-path read (``resolve()`` + ``stat`` +
    ``is_file`` + ``read_bytes`` by path), where a same-uid process could swap a
    component in the agent-writable clone tempdir for a symlink to a secret between
    the checks and the read. A link present at read time -- the observable a swap
    would create -- is refused regardless of timing, so the whole class of race is
    closed. Every refusal records the asset unobtainable exactly as an absent one."""

    @requires_symlinks
    def test_a_declared_asset_that_is_a_symlink_out_of_the_clone_is_refused(
        self, tmp_path, monkeypatch
    ):
        """Test 1: the asset is a symlink to a secret OUTSIDE the clone. Its bytes
        never reach the blob cache and the path is recorded unobtainable."""
        reg = _pin_registry(monkeypatch, _TRUST_OWNER)  # scopes the blob cache dir
        assert reg is not None
        secret = tmp_path / "secret.png"
        secret.write_bytes(b"\x89PNG SECRET-SENTINEL")
        clone = _clone_with(tmp_path, {"ui/icon.svg": ART["ui/icon.svg"]})
        (clone / "ui" / "hero.png").symlink_to(secret)  # declared as heroImage
        entry = _entry()

        written, unobtainable = _copy_declared_art(entry, clone, MANIFEST)

        assert "ui/hero.png" in unobtainable
        # The good regular file still landed; the link did not.
        assert _store_art_cache_path(entry, "ui/icon.svg").is_file()
        assert not _store_art_cache_path(entry, "ui/hero.png").exists()
        # The sentinel is nowhere in the blob cache.
        assert b"SECRET-SENTINEL" not in b"".join(
            p.read_bytes() for p in _blob_cache_dir().rglob("*") if p.is_file()
        )

    @requires_symlinks
    def test_the_by_path_read_would_follow_it_negative_control(self, tmp_path, monkeypatch):
        """Negative control for test 1: reverting to the pre-fix by-path read makes
        ``_open_pinned_asset`` follow the same link and the sentinel DOES land -- so
        the refusal above observes the no-follow walk, not an accident of the fixture."""
        reg = _pin_registry(monkeypatch, _TRUST_OWNER)
        assert reg is not None
        secret = tmp_path / "secret.png"
        secret.write_bytes(b"\x89PNG SECRET-SENTINEL")
        clone = _clone_with(tmp_path, {"ui/icon.svg": ART["ui/icon.svg"]})
        (clone / "ui" / "hero.png").symlink_to(secret)
        entry = _entry()

        # Pre-fix behaviour: resolve the path and read the bytes, following links.
        def _by_path(root, rel_parts):
            src = (root / Path(*rel_parts)).resolve()
            return src.read_bytes() if src.is_file() else None

        monkeypatch.setattr(reg_mod, "_open_pinned_asset", _by_path)
        _copy_declared_art(entry, clone, MANIFEST)

        cached = _store_art_cache_path(entry, "ui/hero.png")
        assert cached.is_file()
        assert cached.read_bytes() == b"\x89PNG SECRET-SENTINEL"

    @requires_symlinks
    def test_an_intermediate_symlinked_directory_is_refused(self, tmp_path):
        """Test 2: an intermediate directory component is a symlink to a directory
        outside the clone. The no-follow walk refuses it before reaching the file."""
        outside = tmp_path / "outside"
        outside.mkdir()
        (outside / "icon.svg").write_bytes(b"\x89PNG OUTSIDE-DIR")
        clone = _clone_with(tmp_path, {"keep.txt": b"x"})
        (clone / "ui").symlink_to(outside, target_is_directory=True)

        # ui/icon.svg walks through the symlinked ``ui`` directory.
        assert _open_pinned_asset(clone, ("ui", "icon.svg")) is None

    def test_a_hard_linked_asset_is_refused(self, tmp_path):
        """Test 3: a declared asset with st_nlink != 1 (hard-linked to a file
        elsewhere on the host) is refused; a single-link copy of the same bytes is
        accepted, proving the refusal observes the link count and not the content."""
        clone = _clone_with(tmp_path, {})
        (clone / "ui").mkdir(parents=True, exist_ok=True)
        target = clone / "ui" / "icon.svg"
        target.write_bytes(b"<svg/>")
        elsewhere = tmp_path / "elsewhere.svg"
        try:
            os.link(target, elsewhere)  # now st_nlink == 2 on target
        except OSError:
            pytest.skip("hard links not supported on this filesystem")

        assert _open_pinned_asset(clone, ("ui", "icon.svg")) is None

        # A single-link file of the same bytes IS accepted.
        elsewhere.unlink()
        assert _open_pinned_asset(clone, ("ui", "icon.svg")) == b"<svg/>"

    def test_a_regular_file_is_read_byte_identical(self, tmp_path):
        """Test 4: a plain regular file is read from the pinned fd byte-for-byte."""
        payload = b"\x89PNG" + bytes(range(256)) * 4
        clone = _clone_with(tmp_path, {"ui/hero.png": payload})
        assert _open_pinned_asset(clone, ("ui", "hero.png")) == payload

    def test_the_size_ceiling_is_inclusive(self, tmp_path, monkeypatch):
        """Test 5: exactly _ART_MAX_BYTES is accepted; one byte over is refused."""
        monkeypatch.setattr(reg_mod, "_ART_MAX_BYTES", 16)
        clone = _clone_with(tmp_path, {"ok.png": b"x" * 16, "big.png": b"x" * 17})
        assert _open_pinned_asset(clone, ("ok.png",)) == b"x" * 16
        assert _open_pinned_asset(clone, ("big.png",)) is None

    @requires_o_nofollow
    def test_the_final_open_carries_o_nofollow(self, tmp_path, monkeypatch):
        """Test 6: on a platform with O_NOFOLLOW, every ``os.open`` the walk issues
        carries the flag, so no component can be a followed link."""
        if os.open not in os.supports_dir_fd:
            pytest.skip("this platform has no dir_fd support; the no-follow walk uses lstat")
        clone = _clone_with(tmp_path, {"ui/icon.svg": b"<svg/>"})
        seen_flags: list[int] = []
        real_open = os.open

        def _spy_open(path, flags, *args, **kwargs):
            seen_flags.append(flags)
            return real_open(path, flags, *args, **kwargs)

        # Keep the spy in ``supports_dir_fd`` so ``_open_pinned_asset`` still selects
        # the dir_fd walk (its guard tests ``os.open in os.supports_dir_fd``).
        monkeypatch.setattr(os, "supports_dir_fd", os.supports_dir_fd | {_spy_open})
        monkeypatch.setattr(os, "open", _spy_open)
        assert _open_pinned_asset(clone, ("ui", "icon.svg")) == b"<svg/>"

        assert seen_flags, "no os.open was issued"
        assert all(flags & os.O_NOFOLLOW for flags in seen_flags), seen_flags

    def test_an_absent_asset_is_refused(self, tmp_path):
        """A declared path with no file at all is a None (source refusal), as before."""
        clone = _clone_with(tmp_path, {"ui/icon.svg": b"<svg/>"})
        assert _open_pinned_asset(clone, ("ui", "missing.png")) is None

    def test_empty_rel_parts_is_refused(self, tmp_path):
        clone = _clone_with(tmp_path, {"ui/icon.svg": b"<svg/>"})
        assert _open_pinned_asset(clone, ()) is None


# ---------------------------------------------------------------------------
# The DESTINATION publish is pinned too
# ---------------------------------------------------------------------------


class TestPinnedDestinationPublish:
    """The copy PUBLISHES through a pinned destination-parent descriptor
    (``_publish_pinned_asset``). ``_store_art_cache_path`` resolve-and-contains the
    destination, but the by-name ``mkdir`` + ``atomic_write`` that followed re-walked
    that parent by name: a same-uid process swapping an intermediate parent for a
    symlink to an outside directory between the check and the write would land the
    copy OUTSIDE the blob cache. The pinned publish refuses a link at any parent
    component; such a refusal is a destination-write failure, so the asset stays cold
    and is NOT recorded unobtainable."""

    @requires_symlinks
    @pytest.mark.asyncio
    async def test_a_symlinked_destination_parent_writes_nothing_outside(
        self, monkeypatch, tmp_path
    ):
        reg = _pin_registry(monkeypatch, _TRUST_OWNER)
        _fake_fetch(monkeypatch, ART)
        entry = _entry()

        # Pre-plant the row's blob-cache branch directory (the parent of the leaf
        # files) as a symlink to an outside directory. The pinned publish opens the
        # blob root and walks down with O_NOFOLLOW, so this link is refused.
        outside = tmp_path / "outside"
        outside.mkdir()
        icon_leaf = _store_art_cache_path(entry, "ui/icon.svg")
        branch_dir = icon_leaf.parent.parent  # <key>/<branch> -> parent is <key>
        branch_dir.parent.mkdir(parents=True, exist_ok=True)
        os.symlink(outside, branch_dir, target_is_directory=True)

        await _prewarm_owner_tier_store_assets(reg, [entry])

        # Nothing landed inside the outside directory.
        assert not any(p.is_file() for p in outside.rglob("*"))
        # The assets are recorded as destination-write failures: cold, NOT unobtainable.
        record = reg_mod._unobtainable_art_path(entry)
        recorded = json.loads(record.read_text(encoding="utf-8")) if record.exists() else []
        assert recorded == []

    def test_publish_refuses_a_linked_parent_directly(self, monkeypatch, tmp_path):
        """Direct ``_publish_pinned_asset``: an intermediate parent that is a symlink
        to an outside dir is refused (OSError) and the outside dir stays empty."""
        reg = _pin_registry(monkeypatch, _TRUST_OWNER)
        assert reg is not None
        entry = _entry()
        cache_path = _store_art_cache_path(entry, "ui/icon.svg")
        outside = tmp_path / "outside"
        outside.mkdir()
        key_dir = cache_path.parent.parent.parent  # blob_root/<key>
        key_dir.parent.mkdir(parents=True, exist_ok=True)
        os.symlink(outside, key_dir, target_is_directory=True)

        with pytest.raises(OSError):
            _publish_pinned_asset(cache_path, b"<svg/>")
        assert not any(p.is_file() for p in outside.rglob("*"))

    @requires_symlinks
    def test_publish_refuses_a_linked_cache_ancestor(self, monkeypatch, tmp_path):
        """The blob root is ``config_dir()/cache/blobs``. A by-name
        ``blob_root.mkdir(parents=True)`` before the pinned walk would create/follow the
        ``cache`` ANCESTOR by name first, so a linked ``cache`` (pointing outside the
        data home) would be followed before any O_NOFOLLOW walk ran. The publish now
        anchors the pinned walk at ``config_dir()`` and opens ``("cache", "blobs", ...)``
        through it, so a linked ``cache`` ancestor is refused and nothing lands in its
        target."""
        reg = _pin_registry(monkeypatch, _TRUST_OWNER)
        assert reg is not None
        # Point the data home and blob root at a private tree under tmp_path so the
        # planted ``cache`` symlink never touches the shared data home this class uses.
        home = tmp_path / "home"
        home.mkdir()
        monkeypatch.setattr(reg_mod, "config_dir", lambda: home)
        monkeypatch.setattr(reg_mod, "_blob_cache_dir", lambda: home / "cache" / "blobs")
        entry = _entry()
        cache_path = _store_art_cache_path(entry, "ui/icon.svg")
        outside = tmp_path / "outside"
        outside.mkdir()
        # Plant ``config_dir()/cache`` (the ancestor of the blob root) as a symlink to
        # the outside directory, before any part of the blob tree exists.
        cache_ancestor = home / "cache"
        assert not cache_ancestor.exists()
        os.symlink(outside, cache_ancestor, target_is_directory=True)

        with pytest.raises(OSError):
            _publish_pinned_asset(cache_path, b"<svg/>")
        assert not any(
            p.is_file() for p in outside.rglob("*")
        ), "a linked cache ancestor must not let the write land outside the data home"

    def test_publish_writes_a_regular_file_through_a_clean_chain(self, monkeypatch, tmp_path):
        """Negative-control shape: with no planted link the same publish lands the
        bytes at the computed cache path byte-for-byte."""
        reg = _pin_registry(monkeypatch, _TRUST_OWNER)
        assert reg is not None
        entry = _entry()
        cache_path = _store_art_cache_path(entry, "ui/icon.svg")
        _publish_pinned_asset(cache_path, b"<svg/>")
        assert cache_path.is_file()
        assert cache_path.read_bytes() == b"<svg/>"

    @requires_symlinks
    def test_the_by_name_arm_refuses_a_linked_blob_root(self, monkeypatch, tmp_path):
        """The by-name (no-``dir_fd``) publish arm — ``_publish_pinned_asset`` when
        ``pinned_fs.supports_pinned_walk()`` is False — lstat-refuses a LINKED blob
        root through ``open_pinned_descendant_dir`` (created with ``mkdir(exist_ok=True)``,
        which silently succeeds on a junction/symlink root) and writes nothing into the
        link target. Forced on POSIX by monkeypatching the pinned-walk probe, so the
        Windows arm runs here too."""
        import kiro_crew.pinned_fs as pinned_fs

        reg = _pin_registry(monkeypatch, _TRUST_OWNER)
        assert reg is not None
        monkeypatch.setattr(pinned_fs, "supports_pinned_walk", lambda: False)
        outside = tmp_path / "outside"
        outside.mkdir()
        blob_root = _blob_cache_dir()
        blob_root.parent.mkdir(parents=True, exist_ok=True)
        os.symlink(outside, blob_root, target_is_directory=True)
        entry = _entry()
        cache_path = _store_art_cache_path(entry, "ui/icon.svg")

        with pytest.raises(OSError):
            reg_mod._publish_pinned_asset(cache_path, b"<svg/>")
        assert not any(p.is_file() for p in outside.rglob("*")), "nothing written into the target"

    def test_the_by_name_arm_writes_through_a_real_root_negative_control(
        self, monkeypatch, tmp_path
    ):
        """Negative control: the SAME by-name arm, given a REAL (unlinked) blob root,
        writes the bytes — so the refusal above is the linked root, not the arm being
        inert."""
        import kiro_crew.pinned_fs as pinned_fs

        reg = _pin_registry(monkeypatch, _TRUST_OWNER)
        assert reg is not None
        monkeypatch.setattr(pinned_fs, "supports_pinned_walk", lambda: False)
        entry = _entry()
        cache_path = _store_art_cache_path(entry, "ui/icon.svg")

        reg_mod._publish_pinned_asset(cache_path, b"<svg/>")
        assert cache_path.is_file() and cache_path.read_bytes() == b"<svg/>"


# ---------------------------------------------------------------------------
# The prewarm batch is bounded in rows, tasks, and retained payload
# ---------------------------------------------------------------------------


class TestPrewarmBatchBounds:
    """A large owner-tier index must not exhaust memory. The prewarm caps how many
    rows one batch CONSIDERS (`_PREWARM_MAX_ROWS`), drives them through a FIXED pool
    of `_PREWARM_CONCURRENCY` workers pulling from a queue (never one task per row),
    and retains only the slim fields a fetch needs per candidate."""

    @pytest.mark.asyncio
    async def test_an_oversized_index_attempts_at_most_the_cap_and_logs_overflow(
        self, monkeypatch, caplog
    ):
        reg = _pin_registry(monkeypatch, _TRUST_OWNER)
        monkeypatch.setattr(reg_mod, "_PREWARM_MAX_ROWS", 5)
        attempted: list[str] = []

        async def _fetch(entry, registry_name):
            attempted.append(entry["name"])
            return True

        monkeypatch.setattr(reg_mod, "_fetch_owner_tier_store_assets", _fetch)
        rows = [
            _entry(
                name=f"app-{i}", gitUrl=f"{SIBLING[:-4]}-{i}.git", repo=f"{SIBLING[:-4]}-{i}.git"
            )
            for i in range(1000)
        ]

        with caplog.at_level("WARNING", logger="kiro_crew.apps.registry"):
            fetched = await _prewarm_owner_tier_store_assets(reg, rows)

        assert fetched == 5
        assert len(attempted) == 5, "no more than the cap is ever fetched"
        assert any("more rows than one batch considers" in r.getMessage() for r in caplog.records)
        # The warning reports the INDEX size and the cap -- not the candidate count
        # plus overflow, which is neither.
        assert any("(1000 rows, capped at 5)" in r.getMessage() for r in caplog.records), [
            r.getMessage() for r in caplog.records
        ]
        assert any("the 995 past the cap stay cold" in r.getMessage() for r in caplog.records)

    @pytest.mark.asyncio
    async def test_the_task_count_never_exceeds_the_concurrency(self, monkeypatch):
        import asyncio

        reg = _pin_registry(monkeypatch, _TRUST_OWNER)
        monkeypatch.setattr(reg_mod, "_PREWARM_CONCURRENCY", 3)
        monkeypatch.setattr(reg_mod, "_PREWARM_MAX_ROWS", 1000)
        live = 0
        peak = 0

        async def _fetch(entry, registry_name):
            nonlocal live, peak
            live += 1
            peak = max(peak, live)
            await asyncio.sleep(0.005)
            live -= 1
            return True

        monkeypatch.setattr(reg_mod, "_fetch_owner_tier_store_assets", _fetch)
        rows = [
            _entry(
                name=f"app-{i}", gitUrl=f"{SIBLING[:-4]}-{i}.git", repo=f"{SIBLING[:-4]}-{i}.git"
            )
            for i in range(40)
        ]

        fetched = await _prewarm_owner_tier_store_assets(reg, rows)

        assert fetched == 40
        assert 1 < peak <= 3, peak

    @pytest.mark.asyncio
    async def test_the_retained_row_drops_fields_a_fetch_does_not_read(self, monkeypatch):
        reg = _pin_registry(monkeypatch, _TRUST_OWNER)
        seen: list[dict] = []

        async def _fetch(entry, registry_name):
            seen.append(entry)
            return True

        monkeypatch.setattr(reg_mod, "_fetch_owner_tier_store_assets", _fetch)
        # A row carrying a large description and unrelated display fields.
        row = _entry(description="x" * 100_000, screenshotsDark=["a.png"], tags=["t"] * 50)

        await _prewarm_owner_tier_store_assets(reg, [row])

        (retained,) = seen
        assert "description" not in retained
        assert "tags" not in retained
        assert "screenshotsDark" not in retained
        # The fields a fetch and its caches DO read are kept.
        assert retained["name"] == "app"
        assert retained["repo"] == SIBLING
        assert retained["branch"] == "main"
        assert retained["_registry"] == "official"

    def test_the_slim_projection_keeps_only_the_fetch_fields(self):
        slim = reg_mod._slim_prewarm_row(
            {
                "name": "app",
                "gitUrl": "g",
                "repo": "r",
                "branch": "b",
                "subdirectory": "s",
                "commit": "c",
                "_registry": "official",
                "description": "x" * 1000,
                "iconPath": "ui/icon.svg",
            }
        )
        assert set(slim) == {
            "name",
            "gitUrl",
            "repo",
            "branch",
            "subdirectory",
            "commit",
            "_registry",
        }


class TestPrewarmRowFieldCap:
    """A retained prewarm-row field is a scalar string coordinate; an owner-tier index
    is external input and can pad one to an arbitrary length or make it a non-string.
    ``_slim_prewarm_row`` rejects such a row (returns None) so it never earns an
    owner-credentialed clone and the padded string never sits in memory for the batch's
    life. The reject is per-field and bounded by ``_PREWARM_FIELD_MAX_LEN``."""

    def test_an_over_length_field_rejects_the_row(self, monkeypatch):
        # Cap above the normal-length coordinates (SIBLING is ~50 chars) so ONLY the
        # padded branch exceeds it and drives the reject.
        monkeypatch.setattr(reg_mod, "_PREWARM_FIELD_MAX_LEN", 60)
        row = _entry(branch="b" * 100)
        assert reg_mod._slim_prewarm_row(row) is None

    def test_a_non_string_field_rejects_the_row(self, monkeypatch):
        row = _entry()
        row["branch"] = 7  # a non-string retained field
        assert reg_mod._slim_prewarm_row(row) is None

    def test_a_within_cap_string_row_is_kept_negative_control(self, monkeypatch):
        """Negative control: the same shape with every retained field a short string
        is kept, proving the rejects above observe the field and not the fixture. Uses
        the default cap so the normal-length coordinates in ``_entry`` all pass."""
        slim = reg_mod._slim_prewarm_row(_entry(branch="main"))
        assert slim is not None and slim["branch"] == "main"

    def test_a_missing_field_is_not_a_reject(self):
        """An ABSENT retained field is omitted from the projection, not a rejection --
        only a PRESENT non-string or over-length one rejects the row."""
        row = _entry()
        row.pop("subdirectory", None)  # absent, not present-and-bad
        slim = reg_mod._slim_prewarm_row(row)
        assert slim is not None and "subdirectory" not in slim

    @pytest.mark.asyncio
    async def test_a_row_with_an_over_length_field_is_skipped_and_not_cloned(
        self, monkeypatch, caplog
    ):
        reg = _pin_registry(monkeypatch, _TRUST_OWNER)
        monkeypatch.setattr(reg_mod, "_PREWARM_FIELD_MAX_LEN", 60)
        calls = _fake_fetch(monkeypatch, ART)
        # A valid repo key (so it passes the provenance gate) but an over-length branch.
        entry = _entry(branch="b" * 100)

        with caplog.at_level("WARNING", logger="kiro_crew.apps.registry"):
            assert await _prewarm_owner_tier_store_assets(reg, [entry]) == 0

        assert calls == [], "a field-rejected row must not be cloned"
        assert _read_manifest_cache(entry) is None
        assert any("over-length retained field" in r.getMessage() for r in caplog.records)

    @pytest.mark.asyncio
    async def test_the_field_cap_can_be_lifted_negative_control(self, monkeypatch):
        """Negative control: with the cap raised above the same branch the row IS
        cloned, proving the skip observes the cap and not an accident of the fixture."""
        reg = _pin_registry(monkeypatch, _TRUST_OWNER)
        monkeypatch.setattr(reg_mod, "_PREWARM_FIELD_MAX_LEN", 4096)
        calls = _fake_fetch(monkeypatch, ART)
        # A servable branch name under the raised cap.
        entry = _entry(branch="b" * 100)

        assert await _prewarm_owner_tier_store_assets(reg, [entry]) == 1
        assert len(calls) == 1


# ---------------------------------------------------------------------------
# _REWARM_AGE is derived from the checker's return, and non-servable is not recorded
# ---------------------------------------------------------------------------


class TestRewarmAgeDerivation:
    def test_the_module_constant_is_the_checkers_return(self):
        from kiro_crew.apps.registry_pipeline import caches, store_art

        assert store_art._REWARM_AGE == store_art._check_rewarm_age(
            caches._MANIFEST_CACHE_TTL, caches._EXTERNAL_REGISTRY_CACHE_TTL
        )
        # It is a positive window, so the derivation is not a coincidence of zero.
        assert store_art._REWARM_AGE > 0

    def test_the_checker_returns_the_window(self):
        from kiro_crew.apps.registry_pipeline import store_art

        assert store_art._check_rewarm_age(200.0, 100.0) == 100.0


class TestNonServablePathIsSkippedNotRecorded:
    """A path ``_store_art_cache_path`` rejects (a non-image, or one outside the
    proxy's grammar) is skipped WITHOUT being recorded unobtainable: the proxy would
    refuse a request for it whatever the clone held, so it never makes the row cold."""

    def test_a_non_image_declared_path_is_not_recorded(self, monkeypatch, tmp_path):
        reg = _pin_registry(monkeypatch, _TRUST_OWNER)
        assert reg is not None
        clone = tmp_path / "clone"
        (clone / "ui").mkdir(parents=True)
        (clone / "ui" / "script.js").write_bytes(b"alert(1)")
        manifest = dict(MANIFEST, iconPath="ui/script.js", heroImage="", screenshots=[])
        entry = _entry()

        written, unobtainable = _copy_declared_art(entry, clone, manifest)

        assert written == 0
        # Non-servable: skipped, not recorded.
        assert "ui/script.js" not in unobtainable
        assert unobtainable == frozenset()


# ---------------------------------------------------------------------------
# The blob cache is swept on the manifest-GC schedule
# ---------------------------------------------------------------------------


def _blob_gc_cutoff() -> float:
    """The instant a blob-cache file's mtime must predate to be GC-eligible."""
    return (
        time.time()
        - max(reg_mod._MANIFEST_CACHE_TTL, reg_mod._EXTERNAL_REGISTRY_CACHE_TTL)
        - reg_mod._BLOB_CACHE_GC_GRACE
    )


def _make_blob_file(rel: str, *, age_past_cutoff: float) -> Path:
    """Create ``<blob root>/<rel>`` and set its mtime *age_past_cutoff* seconds either
    side of the GC cutoff (negative = older = eligible, positive = younger = safe)."""
    path = _blob_cache_dir() / rel
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(b"\x89PNG blob")
    when = _blob_gc_cutoff() - age_past_cutoff
    os.utime(path, (when, when))
    return path


class TestBlobCacheReclaim:
    """The prewarm fills ``cache/blobs/<repo_key>/<branch>/<path>`` for every declared
    asset of every owner-tier row on each fresh fetch, and an index-driven
    ``branch``/``gitUrl`` change or a delisted row orphans those bytes forever: nothing
    but this age-based sweep reclaims ``cache/blobs``. It runs on the manifest-cache
    write path, on the same schedule as ``_gc_manifest_cache_dir``."""

    def test_an_orphan_older_than_the_grace_is_removed_and_empty_dirs_pruned(self, monkeypatch):
        reg = _pin_registry(monkeypatch, _TRUST_OWNER)  # scopes the cache dir
        assert reg is not None
        orphan = _make_blob_file("deadkey-0000/oldbranch/ui/icon.svg", age_past_cutoff=60)
        assert orphan.is_file()

        reg_mod._gc_blob_cache_dir()

        assert not orphan.exists(), "an orphan older than the grace must be removed"
        # Its now-empty ancestor directories are pruned bottom-up, up to (not
        # including) the blob root.
        assert not (_blob_cache_dir() / "deadkey-0000").exists()
        assert _blob_cache_dir().is_dir(), "the blob root itself is never removed"

    def test_a_young_file_survives_negative_control(self, monkeypatch):
        """Negative control: the SAME sweep leaves a file younger than the cutoff in
        place, proving the removal above observes the age and is not unconditional."""
        reg = _pin_registry(monkeypatch, _TRUST_OWNER)
        assert reg is not None
        young = _make_blob_file("livekey-1111/main/ui/icon.svg", age_past_cutoff=-60)

        reg_mod._gc_blob_cache_dir()

        assert young.is_file(), "a file younger than the cutoff must survive"

    @requires_symlinks
    def test_a_symlinked_directory_under_the_root_is_not_descended_or_deleted_through(
        self, monkeypatch, tmp_path
    ):
        """A symlinked directory planted under the blob root is neither followed (so an
        old file the link points AT is never reclaimed through it) nor removed as if it
        were an empty cache directory."""
        reg = _pin_registry(monkeypatch, _TRUST_OWNER)
        assert reg is not None
        outside = tmp_path / "outside"
        outside.mkdir()
        target = outside / "secret.svg"
        target.write_bytes(b"\x89PNG SECRET")
        when = _blob_gc_cutoff() - 3600  # older than the cutoff
        os.utime(target, (when, when))
        _blob_cache_dir().mkdir(parents=True, exist_ok=True)
        link = _blob_cache_dir() / "linkkey"
        os.symlink(outside, link, target_is_directory=True)

        reg_mod._gc_blob_cache_dir()

        # The link is untouched and the file it points at, though old, is not reclaimed
        # through it.
        assert link.is_symlink()
        assert target.is_file(), "an old file behind a symlinked dir must not be reclaimed"

    @requires_symlinks
    def test_a_linked_blob_root_is_refused_and_nothing_in_the_target_is_reclaimed(
        self, monkeypatch, tmp_path
    ):
        """F1: the blob root ITSELF is swapped for a symlink to an outside directory
        holding an old file. The sweep must refuse the linked root (open O_NOFOLLOW /
        lstat-refuse) and reclaim nothing in the link target, rather than resolving the
        link and deleting the target's aged files."""
        reg = _pin_registry(monkeypatch, _TRUST_OWNER)
        assert reg is not None
        outside = tmp_path / "outside"
        outside.mkdir()
        victim = outside / "old.svg"
        victim.write_bytes(b"\x89PNG VICTIM")
        when = _blob_gc_cutoff() - 3600  # older than the cutoff -> would be eligible
        os.utime(victim, (when, when))
        # Plant cache/blobs as a symlink to the outside dir (the parent exists, the
        # root does not, so the sweep meets a linked root).
        blob_root = _blob_cache_dir()
        blob_root.parent.mkdir(parents=True, exist_ok=True)
        os.symlink(outside, blob_root, target_is_directory=True)

        reg_mod._gc_blob_cache_dir()

        assert victim.is_file(), "a linked blob root must not let the sweep reclaim its target"
        assert blob_root.is_symlink(), "the linked root is left in place, not torn down"

    def test_the_linked_root_refusal_is_load_bearing_negative_control(self, monkeypatch):
        """Negative control: the SAME aged file under a REAL blob root IS reclaimed, so
        the survival above is the linked-root refusal, not the file being safe by age."""
        reg = _pin_registry(monkeypatch, _TRUST_OWNER)
        assert reg is not None
        victim = _make_blob_file("realkey-3333/main/old.svg", age_past_cutoff=3600)
        assert victim.is_file()

        reg_mod._gc_blob_cache_dir()

        assert not victim.exists(), "under a real root the same aged file is reclaimed"

    def test_the_sweep_runs_when_the_manifest_cache_is_written(self, monkeypatch):
        """The reclaim is wired to the manifest-cache write path, so a plain
        ``_write_manifest_cache`` sweeps the blob cache too."""
        reg = _pin_registry(monkeypatch, _TRUST_OWNER)
        assert reg is not None
        orphan = _make_blob_file("gonekey-2222/dev/ui/hero.png", age_past_cutoff=120)

        reg_mod._write_manifest_cache(_entry(name="unrelated"), {"name": "unrelated"})

        assert not orphan.exists(), "writing a manifest must sweep the blob cache"

    def test_a_bounded_call_stops_after_the_entry_cap(self, monkeypatch):
        """Every entry the sweep examines -- directory OR file -- debits
        ``_BLOB_CACHE_GC_MAX_ENTRIES`` BEFORE it is dispatched on type, so one call
        cannot walk an unbounded tree and a directory with more entries than the
        remaining budget is not fully listed. The remainder drains on the next write."""
        reg = _pin_registry(monkeypatch, _TRUST_OWNER)
        assert reg is not None
        # Twenty aged files in ONE directory, reached at depth two
        # (<root>/k/main/). With a small cap the sweep removes only as many as the
        # budget allows after the two directory entries above them are counted, so
        # some survive the first call.
        monkeypatch.setattr(reg_mod, "_BLOB_CACHE_GC_MAX_ENTRIES", 6)
        files = [_make_blob_file(f"k-0000/main/f{i}.png", age_past_cutoff=60) for i in range(20)]

        reg_mod._gc_blob_cache_dir()
        removed = sum(1 for f in files if not f.exists())
        assert 0 < removed < 20, f"a bounded call must sweep some but not all: {removed}"
        assert removed <= reg_mod._BLOB_CACHE_GC_MAX_ENTRIES, "never more than the entry cap"

        # Successive calls drain the rest.
        for _ in range(20):
            if all(not f.exists() for f in files):
                break
            reg_mod._gc_blob_cache_dir()
        assert all(not f.exists() for f in files), "the rest drains over later calls"

    def test_a_streamed_directory_is_not_fully_materialised(self, monkeypatch):
        """The sweep consumes the ``os.scandir`` iterator lazily and stops at the
        budget, so it never touches more entries than the cap even in a directory with
        far more children than that -- proven by counting the ``DirEntry`` objects the
        walk pulls from the iterator."""
        reg = _pin_registry(monkeypatch, _TRUST_OWNER)
        assert reg is not None
        monkeypatch.setattr(reg_mod, "_BLOB_CACHE_GC_MAX_ENTRIES", 4)
        # Fifty aged files directly under one key directory; the root holds just that
        # one directory entry, so the budget is spent inside the crowded directory.
        for i in range(50):
            _make_blob_file(f"k-0000/f{i}.png", age_past_cutoff=60)

        touched = 0
        real_scandir = os.scandir

        class _CountingScandir:
            def __init__(self, it):
                self._it = it

            def __iter__(self):
                return self

            def __next__(self):
                nonlocal touched
                entry = next(self._it)
                touched += 1
                return entry

            def close(self):
                self._it.close()

            def __enter__(self):
                return self

            def __exit__(self, *a):
                self._it.close()

        def _spy_scandir(arg):
            return _CountingScandir(real_scandir(arg))

        monkeypatch.setattr(os, "scandir", _spy_scandir)
        reg_mod._gc_blob_cache_dir()

        # The crowded directory of 50 was never fully listed: the walk pulled far
        # fewer than its 50 children before the budget stopped it. (The ``for`` loop
        # pulls one entry past the budget before the in-body check fires, so ``touched``
        # sits a hair above the cap, never near the full listing.)
        assert touched < 50, touched
        assert touched <= reg_mod._BLOB_CACHE_GC_MAX_ENTRIES + 2, touched

    def test_a_chain_deeper_than_the_ceiling_is_left_alone(self, monkeypatch, caplog):
        """A subtree nested deeper than ``_BLOB_CACHE_GC_MAX_DEPTH`` is left in place
        (logged once) rather than recursed into without bound, so an agent-writable
        tree cannot drive the sweep into unbounded recursion."""
        reg = _pin_registry(monkeypatch, _TRUST_OWNER)
        assert reg is not None
        monkeypatch.setattr(reg_mod, "_BLOB_CACHE_GC_MAX_DEPTH", 3)
        reg_mod._BLOB_CACHE_GC_DEPTH_LOGGED.discard(True)
        # A file six directories below the root, past the depth-3 ceiling.
        deep = _make_blob_file("a/b/c/d/e/deep.png", age_past_cutoff=3600)
        assert deep.is_file()

        with caplog.at_level("WARNING", logger="kiro_crew.apps.registry"):
            reg_mod._gc_blob_cache_dir()

        assert deep.is_file(), "a file past the depth ceiling must not be reclaimed"
        assert any("nested deeper" in r.getMessage() for r in caplog.records)

    def test_the_depth_ceiling_can_be_lifted_negative_control(self, monkeypatch):
        """Negative control: the SAME deep aged file IS reclaimed once the ceiling is
        raised above its depth, proving the survival above is the depth ceiling and not
        the file being safe by age."""
        reg = _pin_registry(monkeypatch, _TRUST_OWNER)
        assert reg is not None
        monkeypatch.setattr(reg_mod, "_BLOB_CACHE_GC_MAX_DEPTH", 16)
        deep = _make_blob_file("a/b/c/d/e/deep.png", age_past_cutoff=3600)

        reg_mod._gc_blob_cache_dir()

        assert not deep.exists(), "within the depth ceiling the same aged file is reclaimed"


class TestBlobCacheLstatSweep:
    """``_gc_blob_cache_sweep_lstat`` is the by-name sweep a platform that cannot pin
    a directory fd (Windows) falls back to. It is driven here directly, so its rules
    hold on every runner: only aged regular files go, a link is never followed or
    removed, an emptied subdirectory is pruned, and the entry budget and depth
    ceiling both stop the walk."""

    @staticmethod
    def _age(path: Path, seconds: float = 3600) -> None:
        old = time.time() - seconds
        os.utime(path, (old, old))

    def test_aged_files_go_and_fresh_files_stay(self, tmp_path):
        old = tmp_path / "repo" / "main" / "old.png"
        fresh = tmp_path / "repo" / "main" / "fresh.png"
        old.parent.mkdir(parents=True)
        old.write_bytes(b"old")
        fresh.write_bytes(b"fresh")
        self._age(old)

        reg_mod._gc_blob_cache_sweep_lstat(tmp_path, time.time() - 60, [100], depth=0)

        assert not old.exists()
        assert fresh.is_file()

    def test_an_emptied_subdirectory_is_pruned(self, tmp_path):
        old = tmp_path / "repo" / "main" / "old.png"
        old.parent.mkdir(parents=True)
        old.write_bytes(b"old")
        self._age(old)

        reg_mod._gc_blob_cache_sweep_lstat(tmp_path, time.time() - 60, [100], depth=0)

        assert not (tmp_path / "repo").exists()
        assert tmp_path.is_dir()

    @requires_symlinks
    def test_a_link_is_never_followed_or_removed(self, tmp_path):
        outside = tmp_path / "outside"
        outside.mkdir()
        target = outside / "victim.png"
        target.write_bytes(b"keep")
        self._age(target)
        root = tmp_path / "blobs"
        root.mkdir()
        (root / "dir-link").symlink_to(outside, target_is_directory=True)
        (root / "file-link.png").symlink_to(target)

        reg_mod._gc_blob_cache_sweep_lstat(root, time.time() - 60, [100], depth=0)

        assert target.is_file(), "a file reached through a link must survive"
        assert (root / "dir-link").is_symlink()
        assert (root / "file-link.png").is_symlink()

    def test_a_non_regular_entry_is_left_alone(self, tmp_path):
        if not hasattr(os, "mkfifo"):
            pytest.skip("needs a FIFO to stand in for a special file")
        fifo = tmp_path / "pipe"
        os.mkfifo(fifo)
        self._age(fifo)

        reg_mod._gc_blob_cache_sweep_lstat(tmp_path, time.time() - 60, [100], depth=0)

        assert fifo.exists()

    def test_the_entry_budget_stops_the_walk(self, tmp_path):
        for i in range(5):
            aged = tmp_path / f"f{i}.png"
            aged.write_bytes(b"x")
            self._age(aged)
        budget = [2]

        reg_mod._gc_blob_cache_sweep_lstat(tmp_path, time.time() - 60, budget, depth=0)

        assert budget == [0]
        assert len(list(tmp_path.iterdir())) == 3

    def test_a_subtree_past_the_depth_ceiling_is_left(self, monkeypatch, tmp_path):
        monkeypatch.setattr(reg_mod, "_BLOB_CACHE_GC_MAX_DEPTH", 2)
        reg_mod._BLOB_CACHE_GC_DEPTH_LOGGED.discard(True)
        deep = tmp_path / "a" / "b" / "deep.png"
        deep.parent.mkdir(parents=True)
        deep.write_bytes(b"x")
        self._age(deep)

        reg_mod._gc_blob_cache_sweep_lstat(tmp_path, time.time() - 60, [100], depth=0)

        assert deep.is_file()

    def test_a_missing_directory_is_a_no_op(self, tmp_path):
        reg_mod._gc_blob_cache_sweep_lstat(tmp_path / "absent", time.time() - 60, [100], depth=0)


# ---------------------------------------------------------------------------
# One row cannot fill the blob cache with screenshots
# ---------------------------------------------------------------------------


class TestPerRowScreenshotCap:
    """An owner-tier index is external input and a row's ``screenshots`` list can be
    unbounded; each accepted path is a clone read and a blob-cache file up to
    ``_ART_MAX_BYTES``. ``_declared_store_art`` caps each list field at
    ``_MAX_LIST_ART_PATHS_PER_FIELD`` so one row cannot fill the cache. The cap is
    applied identically to the copy and the warm check (both call it), so a dropped
    path can never keep the row cold."""

    def test_a_list_field_contributes_at_most_the_cap(self):
        cap = reg_mod._MAX_LIST_ART_PATHS_PER_FIELD
        manifest = dict(MANIFEST)
        manifest.pop("iconPath", None)
        manifest.pop("heroImage", None)
        manifest["screenshots"] = [f"ui/shot-{i}.png" for i in range(cap + 10)]
        manifest["screenshotsDark"] = [f"ui/dark-{i}.png" for i in range(cap + 10)]

        declared = reg_mod._declared_store_art(manifest)

        assert declared[:cap] == [f"ui/shot-{i}.png" for i in range(cap)]
        assert declared[cap : 2 * cap] == [f"ui/dark-{i}.png" for i in range(cap)]
        # Each list field contributed exactly the cap; nothing past it leaked in.
        assert len(declared) == 2 * cap
        assert "ui/shot-%d.png" % cap not in declared

    @pytest.mark.asyncio
    async def test_only_the_capped_screenshots_are_copied(self, monkeypatch, caplog):
        reg = _pin_registry(monkeypatch, _TRUST_OWNER)
        cap = reg_mod._MAX_LIST_ART_PATHS_PER_FIELD
        shots = [f"ui/shot-{i}.png" for i in range(cap + 5)]
        manifest = dict(MANIFEST, iconPath="ui/icon.svg", heroImage="", screenshots=shots)
        tree = {"ui/icon.svg": b"<svg/>", **{s: b"\x89PNG" for s in shots}}
        _fake_fetch(monkeypatch, tree, manifest)
        entry = _entry()

        with caplog.at_level("WARNING", logger="kiro_crew.apps.registry"):
            assert await _prewarm_owner_tier_store_assets(reg, [entry]) == 1

            # The first `cap` screenshots landed; the extras were never read or cached.
            assert _store_art_cache_path(entry, shots[cap - 1]).is_file()
            assert not _store_art_cache_path(entry, shots[cap]).exists()
            # The row is warm despite declaring more than the cap: the warm check uses
            # the same capped list, so the dropped paths do not keep it cold.
            assert reg_mod._store_assets_warm(entry, _read_manifest_cache(entry))

        # The overflow is counted and said out loud ONCE for this manifest snapshot;
        # the warm check re-derives the capped list without logging again.
        overflow = [r for r in caplog.records if "more screenshots than one row" in r.getMessage()]
        assert len(overflow) == 1
        assert "screenshots=5" in overflow[0].getMessage()

    def test_no_overflow_is_reported_at_or_under_the_cap(self):
        cap = reg_mod._MAX_LIST_ART_PATHS_PER_FIELD
        manifest = dict(MANIFEST, screenshots=[f"ui/s-{i}.png" for i in range(cap)])
        assert reg_mod._dropped_list_art_paths(manifest) == {}
        manifest["screenshotsDark"] = [f"ui/d-{i}.png" for i in range(cap + 2)]
        assert reg_mod._dropped_list_art_paths(manifest) == {"screenshotsDark": 2}

    def test_the_cap_can_be_lifted_negative_control(self, monkeypatch):
        """Negative control: raising the cap lets the same manifest declare more,
        proving the truncation above observes the constant and not the fixture."""
        monkeypatch.setattr(reg_mod, "_MAX_LIST_ART_PATHS_PER_FIELD", 100)
        manifest = dict(MANIFEST)
        manifest.pop("iconPath", None)
        manifest.pop("heroImage", None)
        manifest["screenshots"] = [f"ui/shot-{i}.png" for i in range(30)]
        manifest["screenshotsDark"] = []

        declared = reg_mod._declared_store_art(manifest)

        assert len([d for d in declared if d.startswith("ui/shot-")]) == 30


# ---------------------------------------------------------------------------
# The post-budget cleanup is bounded, and an over-budget cleanup is detached
# ---------------------------------------------------------------------------


class TestBoundedPostBudgetCleanup:
    """``_PREWARM_BATCH_BUDGET`` is the clone-cancellation deadline; after it fires the
    listing request otherwise awaits each cancelled clone's process-group kill and
    scratch ``rmtree`` with no bound, so a hanging forge on a slow filesystem stalls a
    cold App Store load for tens of seconds. The batch's cancellation/cleanup phase is
    wrapped in ``_PREWARM_CLEANUP_BUDGET``, and a cleanup still running at that deadline
    is detached to finish in the background so the request returns."""

    @pytest.mark.asyncio
    async def test_a_hanging_cleanup_returns_within_the_two_budgets_and_finishes_detached(
        self, monkeypatch, caplog
    ):
        import asyncio

        reg = _pin_registry(monkeypatch, _TRUST_OWNER)

        release = threading.Event()
        cleanup_finished = threading.Event()
        loop = asyncio.get_running_loop()
        started: list[float] = []

        async def _fetch(entry, registry_name):
            # The row's fetch, parked on its first step: a worker reaches it in the
            # same loop turn the batch starts, so the budget always lands here, never
            # in the thread hops a real fetch makes first. Cancelled at the budget,
            # its cleanup hangs past the cleanup budget, then finishes once released
            # -- standing in for a slow filesystem / process kill.
            started.append(loop.time())
            try:
                await asyncio.Event().wait()
            except asyncio.CancelledError:
                await loop.run_in_executor(None, release.wait)
                cleanup_finished.set()
                raise
            return True

        monkeypatch.setattr(reg_mod, "_fetch_owner_tier_store_assets", _fetch)
        monkeypatch.setattr(reg_mod, "_PREWARM_BATCH_BUDGET", 0.2)
        monkeypatch.setattr(reg_mod, "_PREWARM_CLEANUP_BUDGET", 0.2)
        entry = _entry()
        # Everything from the first ``await`` to the last assertion that precedes the
        # release runs under ``try/finally``: the fake cleanup parks a default-executor
        # worker thread on ``release.wait``, and an assertion that fired before
        # ``release.set()`` would strand that (non-daemon) thread and hang the test
        # session at interpreter exit. The finally releases it and drains the detached
        # cleanup regardless of how the body ended.
        try:
            # The outer wait is the lost-run guard: a missing bound fails by name
            # rather than hanging. The bound under test is ``elapsed`` below.
            with caplog.at_level("WARNING", logger="kiro_crew.apps.registry"):
                fetched = await _settled(
                    _prewarm_owner_tier_store_assets(reg, [entry]), "the budgeted prewarm"
                )
            # From the batch's start (the row's first step), so the bound covers the
            # two budgets the test is about and not the thread hops before them.
            elapsed = loop.time() - started[0]

            assert fetched == 0
            assert (
                elapsed < 2
            ), f"the request returned late ({elapsed:.2f}s); cleanup was not bounded"
            assert any("cleanup exceeded its" in r.getMessage() for r in caplog.records)
            # The detached cleanup is still pending; releasing it lets it finish, and
            # the pending set drains.
            assert reg_mod._PENDING_PREWARM_CLEANUPS
        finally:
            # Always unblock the executor worker, then settle the detached cleanup so
            # no task or thread outlives the test.
            release.set()
            pending = set(reg_mod._PENDING_PREWARM_CLEANUPS)
            if pending:
                await asyncio.wait(pending, timeout=_BARRIER_CEILING_SECS)
        assert cleanup_finished.is_set(), "the detached cleanup must still complete"
        assert not reg_mod._PENDING_PREWARM_CLEANUPS, "a finished cleanup removes itself"

    @pytest.mark.asyncio
    async def test_an_unbounded_cleanup_would_block_past_the_deadline_negative_control(
        self, monkeypatch
    ):
        """Negative control: emulate the pre-fix single ``wait_for`` (which awaits the
        cancelled clones' cleanup inline) and observe it blocks past the cleanup
        deadline -- proving the bound above is what returns the request promptly."""
        import asyncio

        release = asyncio.Event()

        async def _worker():
            try:
                await asyncio.Event().wait()
            except asyncio.CancelledError:
                # The pre-fix shape awaits this cleanup inline, unbounded.
                await release.wait()
                raise

        async def _prefix_wait():
            # The old code: one wait_for over the gather, whose timeout cancels the
            # worker and then awaits its (hanging) cleanup before raising.
            try:
                await asyncio.wait_for(asyncio.gather(_worker()), timeout=0.2)
            except asyncio.TimeoutError:
                return 0

        loop = asyncio.get_running_loop()
        started = loop.time()
        task = asyncio.ensure_future(_prefix_wait())
        await asyncio.sleep(0.6)
        blocked_past_deadline = not task.done()
        assert blocked_past_deadline, "the pre-fix shape should still be blocked on cleanup"
        release.set()
        await asyncio.wait_for(task, timeout=2)
        assert loop.time() - started > 0.5

    @pytest.mark.asyncio
    async def test_a_quick_cleanup_settles_within_the_budget_without_detaching(self, monkeypatch):
        """When the cancelled clones' cleanup settles within ``_PREWARM_CLEANUP_BUDGET``,
        nothing is detached: the pending set stays empty."""
        import asyncio

        reg = _pin_registry(monkeypatch, _TRUST_OWNER)

        async def _hang_fetch(git_url, branch, dest, log_lines, *, clone_env, sandbox_mode, **kw):
            Path(dest).mkdir(parents=True, exist_ok=True)
            await asyncio.Event().wait()
            return None

        monkeypatch.setattr(reg_mod, "_git_fetch_branch", _hang_fetch)
        monkeypatch.setattr(reg_mod, "_PREWARM_BATCH_BUDGET", 0.2)
        monkeypatch.setattr(reg_mod, "_PREWARM_CLEANUP_BUDGET", 2.0)
        assert not reg_mod._PENDING_PREWARM_CLEANUPS

        fetched = await asyncio.wait_for(
            _prewarm_owner_tier_store_assets(reg, [_entry()]), timeout=5
        )

        assert fetched == 0
        assert not reg_mod._PENDING_PREWARM_CLEANUPS, "a cleanup within budget is not detached"

    def test_the_cleanup_budget_is_at_least_the_process_kill_grace(self):
        """The cleanup budget's floor is ``checkout._KILL_GRACE_PERIOD`` (one clone's
        kill grace); a value below it would abandon cleanups about to finish."""
        assert reg_mod._PREWARM_CLEANUP_BUDGET >= reg_mod._KILL_GRACE_PERIOD


# ---------------------------------------------------------------------------
# An OUTER cancellation (gateway shutdown) settles the batch, not leaks it
# ---------------------------------------------------------------------------


class TestOuterCancellationSettlesTheBatch:
    """``_PREWARM_BATCH_BUDGET`` bounds the batch, but the whole prewarm can also be
    cancelled from OUTSIDE — a gateway shutdown mid cold load. ``asyncio.wait`` does
    NOT cancel the futures it waits on when it is itself cancelled, so without a guard
    the batch and its up-to-``_PREWARM_CONCURRENCY`` owner-credentialed clones would be
    left running detached with their scratch dirs. The prewarm cancels the batch and
    settles its cleanup before propagating the cancellation."""

    @pytest.mark.asyncio
    async def test_cancelling_the_prewarm_cancels_the_workers_and_settles_cleanup(
        self, monkeypatch
    ):
        import asyncio

        reg = _pin_registry(monkeypatch, _TRUST_OWNER)
        cancelled: list[str] = []
        cleaned: list[str] = []
        started = asyncio.Event()

        async def _hang_fetch(git_url, branch, dest, log_lines, *, clone_env, sandbox_mode, **kw):
            Path(dest).mkdir(parents=True, exist_ok=True)
            started.set()
            try:
                await asyncio.Event().wait()  # never returns on its own
            except asyncio.CancelledError:
                cancelled.append(git_url)
                raise
            return None

        real_rmtree = reg_mod._rmtree_force_settled

        async def _rmtree(path):
            cleaned.append(str(path))
            await real_rmtree(path)

        monkeypatch.setattr(reg_mod, "_git_fetch_branch", _hang_fetch)
        monkeypatch.setattr(reg_mod, "_rmtree_force_settled", _rmtree)
        # A long batch budget so the deadline never fires: the ONLY thing that ends
        # this run is the outer cancellation below.
        monkeypatch.setattr(reg_mod, "_PREWARM_BATCH_BUDGET", 3600.0)
        entry = _entry()

        task = asyncio.ensure_future(_prewarm_owner_tier_store_assets(reg, [entry]))
        await asyncio.wait_for(started.wait(), timeout=5)
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await asyncio.wait_for(task, timeout=5)

        # The in-flight clone was cancelled (not abandoned) and its scratch dir removed.
        assert cancelled, "the worker clone was not cancelled on outer cancellation"
        assert cleaned, "the cancelled clone's scratch dir was not cleaned up"

    @pytest.mark.asyncio
    async def test_a_bare_wait_would_leak_the_batch_negative_control(self, monkeypatch):
        """Negative control: the pre-fix shape (a bare ``asyncio.wait`` with no
        cancellation guard) leaves the inner batch RUNNING when the outer wait is
        cancelled — proving the guard above is what settles it. Reproduced inline so
        the control does not depend on reverting the production code."""
        import asyncio

        worker_cancelled: list[str] = []
        leaked: list[asyncio.Future] = []

        async def _worker():
            try:
                await asyncio.Event().wait()
            except asyncio.CancelledError:
                worker_cancelled.append("w")
                raise

        async def _prefix_prewarm():
            batch = asyncio.ensure_future(asyncio.gather(_worker()))
            leaked.append(batch)
            # The pre-fix code: no try/except around the wait, so an outer
            # cancellation raises straight out and never cancels ``batch``.
            await asyncio.wait({batch}, timeout=3600.0)
            return batch

        task = asyncio.ensure_future(_prefix_prewarm())
        await asyncio.sleep(0.05)
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await asyncio.wait_for(task, timeout=5)
        # The batch was NOT cancelled by the bare wait: the worker still runs.
        assert worker_cancelled == [], "the bare-wait shape should leak the running batch"
        # Clean up the leaked batch this control deliberately created.
        for fut in leaked:
            fut.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await fut

    @pytest.mark.asyncio
    async def test_an_outer_cancel_during_the_cleanup_window_propagates(self, monkeypatch):
        """The budget-elapsed path cancels the workers and then waits the cleanup
        budget with ``asyncio.wait({batch}, timeout=...)``. If an OUTER cancellation
        (gateway shutdown) lands during that cleanup wait it must PROPAGATE -- the
        prewarm coroutine raises ``CancelledError`` and does NOT return a count. A
        ``wait_for(shield(batch))`` + ``except CancelledError: pass`` would swallow it
        as though it were the settling batch's own re-raised cancellation."""
        import asyncio

        reg = _pin_registry(monkeypatch, _TRUST_OWNER)
        cleanup_entered = asyncio.Event()
        release = asyncio.Event()
        held: list[Any] = []

        async def _fetch(entry, registry_name):
            # The row's fetch, parked on its first step: a worker reaches it in the
            # same loop turn the batch starts, so the short budget always lands
            # here, never in the thread hops a real fetch makes first. Its cleanup
            # blocks, holding the prewarm in the cleanup wait for the outer cancel
            # below to land there.
            held.append(asyncio.current_task())
            try:
                await asyncio.Event().wait()
            except asyncio.CancelledError:
                cleanup_entered.set()
                await release.wait()
                raise
            return True

        monkeypatch.setattr(reg_mod, "_fetch_owner_tier_store_assets", _fetch)
        # Short batch budget -> the run reaches the cleanup wait quickly; a long
        # cleanup budget -> the cleanup wait is where the outer cancel lands.
        monkeypatch.setattr(reg_mod, "_PREWARM_BATCH_BUDGET", 0.1)
        monkeypatch.setattr(reg_mod, "_PREWARM_CLEANUP_BUDGET", 3600.0)

        task = asyncio.ensure_future(_prewarm_owner_tier_store_assets(reg, [_entry()]))
        try:
            await _await_barrier(cleanup_entered, "the cancelled row's cleanup")
            task.cancel()
            with pytest.raises(asyncio.CancelledError):
                await _settled(task, "the prewarm cancelled in its cleanup window")
        finally:
            # Let the blocked cleanup drain, even on a failed assertion, so the
            # test leaves nothing running behind it.
            release.set()
            if held:
                await asyncio.wait(held, timeout=_BARRIER_CEILING_SECS)


# ---------------------------------------------------------------------------
# app.json is read through the pinned no-follow walk, not by path
# ---------------------------------------------------------------------------


class TestPinnedManifestRead:
    """``_fetch_owner_tier_store_assets`` reads ``app.json`` through the SAME pinned
    no-follow descriptor walk the art files use (``_open_pinned_asset`` with
    ``max_bytes=_MANIFEST_MAX_BYTES``), not by path. The by-path
    ``resolve``/``stat``/``read_text`` it replaced was a TOCTOU: a same-uid process
    could swap ``app.json`` for a symlink to a secret between the containment check and
    the read, landing that secret in the agent-readable manifest cache. A link at the
    ``app.json`` component, a hard-linked manifest, or one over the ceiling is now a
    source refusal; a plain regular manifest reads unchanged."""

    @requires_symlinks
    @pytest.mark.asyncio
    async def test_a_symlinked_app_json_never_lands_its_target_in_the_manifest_cache(
        self, monkeypatch, tmp_path
    ):
        reg = _pin_registry(monkeypatch, _TRUST_OWNER)
        secret = tmp_path / "credentials.json"
        secret.write_text(json.dumps({"token": "MANIFEST-SENTINEL", "iconPath": "ui/icon.svg"}))
        _fake_fetch(monkeypatch, ART, manifest=None)
        real_fetch = reg_mod._git_fetch_branch

        async def _fetch_with_link(git_url, branch, dest, log_lines, **kw):
            result = await real_fetch(git_url, branch, dest, log_lines, **kw)
            (Path(dest) / "app.json").symlink_to(secret)
            return result

        monkeypatch.setattr(reg_mod, "_git_fetch_branch", _fetch_with_link)
        entry = _entry()

        assert await _prewarm_owner_tier_store_assets(reg, [entry]) == 0
        assert _read_manifest_cache(entry) is None
        assert not _manifest_cache_path(entry).exists()
        # The sentinel appears nowhere the prewarm wrote.
        for path in list(_manifest_cache_path(entry).parent.rglob("*")) + list(
            _blob_cache_dir().rglob("*")
        ):
            if path.is_file():
                assert b"MANIFEST-SENTINEL" not in path.read_bytes()

    @pytest.mark.asyncio
    async def test_a_hard_linked_app_json_is_refused(self, monkeypatch, tmp_path):
        reg = _pin_registry(monkeypatch, _TRUST_OWNER)
        elsewhere = tmp_path / "elsewhere.json"
        elsewhere.write_text(json.dumps(dict(MANIFEST)))
        _fake_fetch(monkeypatch, ART, manifest=None)
        real_fetch = reg_mod._git_fetch_branch

        async def _fetch_with_hardlink(git_url, branch, dest, log_lines, **kw):
            result = await real_fetch(git_url, branch, dest, log_lines, **kw)
            try:
                os.link(elsewhere, Path(dest) / "app.json")  # st_nlink == 2
            except OSError:
                pytest.skip("hard links not supported on this filesystem")
            return result

        monkeypatch.setattr(reg_mod, "_git_fetch_branch", _fetch_with_hardlink)
        entry = _entry()

        assert await _prewarm_owner_tier_store_assets(reg, [entry]) == 0
        assert _read_manifest_cache(entry) is None

    @pytest.mark.asyncio
    async def test_a_regular_app_json_reads_unchanged(self, monkeypatch):
        """A plain regular ``app.json`` is read byte-identically through the pinned
        walk and its every declared art file lands -- the read path change is
        transparent to the normal case."""
        reg = _pin_registry(monkeypatch, _TRUST_OWNER)
        calls = _fake_fetch(monkeypatch, ART)
        entry = _entry()

        assert await _prewarm_owner_tier_store_assets(reg, [entry]) == 1
        assert len(calls) == 1
        assert _read_manifest_cache(entry) == MANIFEST
        for rel in ART:
            assert _store_art_cache_path(entry, rel).is_file()

    @requires_symlinks
    @pytest.mark.asyncio
    async def test_the_pinned_manifest_read_can_fail_negative_control(self, monkeypatch, tmp_path):
        """Negative control: reverting to a by-path follow read makes the SAME
        symlinked ``app.json`` be followed and its target land in the manifest cache --
        so the refusal above observes the pinned read, not an accident of the fixture."""
        reg = _pin_registry(monkeypatch, _TRUST_OWNER)
        secret = tmp_path / "credentials.json"
        secret.write_text(json.dumps({"token": "MANIFEST-SENTINEL"}))
        _fake_fetch(monkeypatch, ART, manifest=None)
        real_fetch = reg_mod._git_fetch_branch

        async def _fetch_with_link(git_url, branch, dest, log_lines, **kw):
            result = await real_fetch(git_url, branch, dest, log_lines, **kw)
            (Path(dest) / "app.json").symlink_to(secret)
            return result

        monkeypatch.setattr(reg_mod, "_git_fetch_branch", _fetch_with_link)

        def _by_path(root, rel_parts, *, max_bytes=None):
            src = (root / Path(*rel_parts)).resolve()
            return src.read_bytes() if src.is_file() else None

        monkeypatch.setattr(reg_mod, "_open_pinned_asset", _by_path)
        entry = _entry()

        assert await _prewarm_owner_tier_store_assets(reg, [entry]) == 1
        assert _read_manifest_cache(entry) == {"token": "MANIFEST-SENTINEL"}

    def test_the_max_bytes_ceiling_is_inclusive_for_the_manifest(self, tmp_path, monkeypatch):
        """The pinned read validates the opened descriptor against the caller's
        ``max_bytes``: exactly the ceiling is accepted, one byte over refused."""
        monkeypatch.setattr(reg_mod, "_MANIFEST_MAX_BYTES", 16)
        clone = tmp_path / "clone"
        clone.mkdir(parents=True, exist_ok=True)
        (clone / "app.json").write_bytes(b"x" * 16)
        assert reg_mod._open_pinned_asset(clone, ("app.json",), max_bytes=16) == b"x" * 16
        (clone / "app.json").write_bytes(b"x" * 17)
        assert reg_mod._open_pinned_asset(clone, ("app.json",), max_bytes=16) is None


# ---------------------------------------------------------------------------
# The row cap bounds preprocessing, not just task creation
# ---------------------------------------------------------------------------


class TestRowCapBoundsPreprocessing:
    """The ``_PREWARM_MAX_ROWS`` cap is checked at the TOP of the per-row loop, before
    any per-row I/O (the provenance/cache/backoff reads), so an oversized index cannot
    make preprocessing unbounded: once the batch holds the cap's worth of candidates,
    every remaining row is counted as overflow and skipped without a read."""

    @pytest.mark.asyncio
    async def test_at_most_the_cap_many_manifest_cache_reads_for_a_3n_index(self, monkeypatch):
        reg = _pin_registry(monkeypatch, _TRUST_OWNER)
        cap = 5
        monkeypatch.setattr(reg_mod, "_PREWARM_MAX_ROWS", cap)
        reads: list[str] = []
        real_read = reg_mod._read_manifest_cache

        def _spy_read(entry):
            reads.append(entry.get("name", ""))
            return real_read(entry)

        monkeypatch.setattr(reg_mod, "_read_manifest_cache", _spy_read)

        async def _fetch(entry, registry_name):
            return True

        monkeypatch.setattr(reg_mod, "_fetch_owner_tier_store_assets", _fetch)
        rows = [
            _entry(
                name=f"app-{i}", gitUrl=f"{SIBLING[:-4]}-{i}.git", repo=f"{SIBLING[:-4]}-{i}.git"
            )
            for i in range(3 * cap)
        ]

        fetched = await _prewarm_owner_tier_store_assets(reg, rows)

        assert fetched == cap
        # The rows past the cap were never read: at most `cap` per-row cache reads for
        # a 3*cap-row index. (Warm rows would read then skip, so this is <= cap; here
        # every row is cold so it is exactly cap.)
        assert len(reads) == cap, f"expected at most {cap} reads, saw {len(reads)}: {reads}"

    @pytest.mark.asyncio
    async def test_the_cap_can_be_lifted_negative_control(self, monkeypatch):
        """Negative control: raising the cap past the index size lets every row be
        read, proving the bound above observes the cap and not the fixture size."""
        reg = _pin_registry(monkeypatch, _TRUST_OWNER)
        monkeypatch.setattr(reg_mod, "_PREWARM_MAX_ROWS", 1000)
        reads: list[str] = []
        real_read = reg_mod._read_manifest_cache

        def _spy_read(entry):
            reads.append(entry.get("name", ""))
            return real_read(entry)

        monkeypatch.setattr(reg_mod, "_read_manifest_cache", _spy_read)

        async def _fetch(entry, registry_name):
            return True

        monkeypatch.setattr(reg_mod, "_fetch_owner_tier_store_assets", _fetch)
        rows = [
            _entry(
                name=f"app-{i}", gitUrl=f"{SIBLING[:-4]}-{i}.git", repo=f"{SIBLING[:-4]}-{i}.git"
            )
            for i in range(15)
        ]

        await _prewarm_owner_tier_store_assets(reg, rows)

        assert len(reads) == 15, "with the cap lifted every row is read"


# ---------------------------------------------------------------------------
# The manifest-cache sidecars are read once through a pinned no-follow descriptor
# ---------------------------------------------------------------------------


class TestSidecarPinnedRead:
    """The ``.unobtainable``/``.clone-failed`` records live beside the manifest cache
    in the agent-writable ``by-source/`` directory. ``_read_pinned_sidecar`` opens each
    ONCE ``O_NOFOLLOW`` under its pinned parent, validates the opened descriptor
    (regular, single link, no larger than the ceiling) and reads at most the ceiling
    plus one byte -- closing the size-check-then-read race a by-name
    ``stat``/``read_text`` had, and refusing a symlinked sidecar rather than following
    it. Every refusal is a ``None`` return, which the record readers treat as absent."""

    def test_a_small_regular_sidecar_reads_with_its_mtime(self, tmp_path):
        path = tmp_path / "rec.json"
        path.write_text('["a", "b"]', encoding="utf-8")
        result = reg_mod._read_pinned_sidecar(path, max_bytes=1024)
        assert result is not None
        data, mtime = result
        assert data == b'["a", "b"]'
        assert mtime == pytest.approx(path.stat().st_mtime)

    def test_an_oversize_sidecar_is_refused(self, tmp_path):
        path = tmp_path / "rec.json"
        path.write_bytes(b"x" * 17)
        assert reg_mod._read_pinned_sidecar(path, max_bytes=16) is None
        # Exactly the ceiling is accepted (inclusive).
        path.write_bytes(b"x" * 16)
        result = reg_mod._read_pinned_sidecar(path, max_bytes=16)
        assert result is not None and result[0] == b"x" * 16

    @requires_symlinks
    def test_a_symlinked_sidecar_is_refused(self, tmp_path):
        secret = tmp_path / "secret.json"
        secret.write_text('["SENTINEL"]', encoding="utf-8")
        link = tmp_path / "rec.json"
        link.symlink_to(secret)
        assert reg_mod._read_pinned_sidecar(link, max_bytes=1024) is None

    def test_a_hard_linked_sidecar_is_refused(self, tmp_path):
        real = tmp_path / "rec.json"
        real.write_text('["a"]', encoding="utf-8")
        alias = tmp_path / "alias.json"
        try:
            os.link(real, alias)  # st_nlink == 2
        except OSError:
            pytest.skip("hard links not supported on this filesystem")
        assert reg_mod._read_pinned_sidecar(real, max_bytes=1024) is None

    def test_a_missing_sidecar_is_none(self, tmp_path):
        assert reg_mod._read_pinned_sidecar(tmp_path / "absent.json", max_bytes=1024) is None

    @requires_symlinks
    @pytest.mark.asyncio
    async def test_an_oversize_unobtainable_record_keeps_the_row_cold(self, monkeypatch):
        """Integration: an oversize ``.unobtainable`` record swapped in reads as the
        empty set, so the declared-missing path is NOT counted satisfied and the row
        stays cold (re-cloned) rather than the reader allocating the oversize file."""
        reg = _pin_registry(monkeypatch, _TRUST_OWNER)
        manifest = dict(MANIFEST, screenshots=["ui/missing.png"])
        calls = _fake_fetch(monkeypatch, ART, manifest)
        entry = _entry()

        assert await _prewarm_owner_tier_store_assets(reg, [entry]) == 1
        assert len(calls) == 1
        record = reg_mod._unobtainable_art_path(entry)
        assert json.loads(record.read_text(encoding="utf-8")) == ["ui/missing.png"]

        # Swap the record for one past the ceiling: the read refuses it (empty set), so
        # ui/missing.png is not satisfied and the row is cold again.
        monkeypatch.setattr(reg_mod, "_MANIFEST_MAX_BYTES", 8)
        record.write_text(json.dumps(["ui/missing.png"]) + " " * 64, encoding="utf-8")
        manifest_mtime = _manifest_cache_path(entry).stat().st_mtime
        assert reg_mod._read_unobtainable_art(entry, manifest_mtime) == frozenset()

    @requires_symlinks
    def test_a_symlinked_clone_failure_record_reads_as_absent(self, monkeypatch, tmp_path):
        """Integration: a symlinked ``.clone-failed`` record is refused, so the backoff
        reads as absent (retry) rather than following the link."""
        reg = _pin_registry(monkeypatch, _TRUST_OWNER)
        assert reg is not None
        entry = _entry()
        record = reg_mod._clone_failure_path(entry)
        record.parent.mkdir(parents=True, exist_ok=True)
        secret = tmp_path / "secret.json"
        secret.write_text(json.dumps({"at": time.time(), "reason": "x"}), encoding="utf-8")
        record.symlink_to(secret)
        assert reg_mod._read_clone_failure(entry) is None


# ---------------------------------------------------------------------------
# Persist before publish, and the row cap counts rows EXAMINED
# ---------------------------------------------------------------------------


class TestPersistBeforePublish:
    """Art is published only for a manifest that reached the cache: a refused manifest
    write ends the row cold, so it never reads warm beside a bare listing."""

    @pytest.mark.asyncio
    async def test_a_refused_manifest_write_publishes_no_art(self, monkeypatch, caplog):
        reg = _pin_registry(monkeypatch, _TRUST_OWNER)
        _fake_fetch(monkeypatch, ART, dict(MANIFEST, screenshots=["ui/shot-1.png"]))
        monkeypatch.setattr(reg_mod, "_write_manifest_cache", lambda entry, data: False)
        published: list[Path] = []
        real_publish = reg_mod._publish_pinned_asset
        monkeypatch.setattr(
            reg_mod,
            "_publish_pinned_asset",
            lambda p, d: (published.append(p), real_publish(p, d)),
        )
        fetched = await _prewarm_owner_tier_store_assets(reg, [_entry()])
        assert fetched == 0
        assert published == []

    @pytest.mark.asyncio
    async def test_a_persisted_manifest_publishes_art_negative_control(self, monkeypatch):
        reg = _pin_registry(monkeypatch, _TRUST_OWNER)
        _fake_fetch(monkeypatch, ART, dict(MANIFEST, screenshots=["ui/shot-1.png"]))
        published: list[Path] = []
        real_publish = reg_mod._publish_pinned_asset
        monkeypatch.setattr(
            reg_mod,
            "_publish_pinned_asset",
            lambda p, d: (published.append(p), real_publish(p, d)),
        )
        fetched = await _prewarm_owner_tier_store_assets(reg, [_entry()])
        assert fetched == 1
        assert published


class TestTheRowCapCountsExaminedRows:
    """``_PREWARM_MAX_ROWS`` bounds the rows a batch EXAMINES, not only the cold ones it
    keeps, so an index of many warm rows cannot spend unbounded cache reads."""

    @pytest.mark.asyncio
    async def test_warm_rows_past_the_cap_are_not_read(self, monkeypatch):
        reg = _pin_registry(monkeypatch, _TRUST_OWNER)
        monkeypatch.setattr(reg_mod, "_PREWARM_MAX_ROWS", 2)
        looked: list[str] = []
        monkeypatch.setattr(
            reg_mod,
            "_store_assets_warm",
            lambda entry, manifest: looked.append(entry["name"]) or True,
        )
        monkeypatch.setattr(reg_mod, "_read_manifest_cache", lambda entry: {"name": entry["name"]})
        rows = [
            _entry(name=f"warm-{i}", repo=f"{SIBLING}{i}", gitUrl=f"{SIBLING}{i}") for i in range(6)
        ]
        fetched = await _prewarm_owner_tier_store_assets(reg, rows)
        assert fetched == 0
        assert len(looked) <= 2, looked

    @pytest.mark.asyncio
    async def test_within_the_cap_every_row_is_read_negative_control(self, monkeypatch):
        reg = _pin_registry(monkeypatch, _TRUST_OWNER)
        monkeypatch.setattr(reg_mod, "_PREWARM_MAX_ROWS", 10)
        looked: list[str] = []
        monkeypatch.setattr(
            reg_mod,
            "_store_assets_warm",
            lambda entry, manifest: looked.append(entry["name"]) or True,
        )
        monkeypatch.setattr(reg_mod, "_read_manifest_cache", lambda entry: {"name": entry["name"]})
        rows = [
            _entry(name=f"warm-{i}", repo=f"{SIBLING}{i}", gitUrl=f"{SIBLING}{i}") for i in range(6)
        ]
        await _prewarm_owner_tier_store_assets(reg, rows)
        assert len(looked) == 6


# ---------------------------------------------------------------------------
# The manifest-cache source coordinates degrade a hostile row to safe defaults
# ---------------------------------------------------------------------------


class TestManifestSourceCoordinatesDegradeHostileValues:
    """``_manifest_source_coordinates`` feeds a cache KEY, so every value an
    external index controls must degrade to a safe, distinct-but-harmless default
    when it is not the expected type -- never crash the derivation. A row whose
    ``name``, ``branch`` and ``subdirectory`` are all non-strings (and an empty
    branch) exercises each defensive branch at once."""

    def test_non_string_name_branch_and_subdirectory_fall_back(self):
        coords = _manifest_source_coordinates(
            {
                "name": 123,
                "gitUrl": SIBLING,
                "repo": SIBLING,
                "branch": 456,
                "subdirectory": 789,
            }
        )
        origin, ref, subdirectory, name = coords
        # The origin is still the normalized credential-free clone URL.
        assert origin == SIBLING.removesuffix(".git")
        # Each non-string coordinate collapsed to its safe default.
        assert name == ""
        assert ref == "branch:main"  # a non-string branch defaults to main
        assert subdirectory == ""

    def test_an_empty_branch_also_defaults_to_main(self):
        """An empty-string branch is as unusable as a non-string one and takes the
        same ``main`` default, so the two share one identity component."""
        _origin, ref, _subdir, name = _manifest_source_coordinates(
            {"name": "app", "gitUrl": SIBLING, "repo": SIBLING, "branch": "", "subdirectory": ""}
        )
        assert ref == "branch:main"
        assert name == "app"

    def test_a_well_formed_row_is_unchanged_negative_control(self):
        """Negative control: a row with ordinary string coordinates keeps them, so
        the fallbacks above observe the bad types and not an unconditional rewrite."""
        _origin, ref, subdirectory, name = _manifest_source_coordinates(
            {
                "name": "app",
                "gitUrl": SIBLING,
                "repo": SIBLING,
                "branch": "dev",
                "subdirectory": "apps/app",
            }
        )
        assert name == "app"
        assert ref == "branch:dev"
        assert subdirectory == "apps/app"
