"""``capabilities.feature_videos_download`` ``hosts``: where feature content may come from.

RFC crew-enterprise-edition, appendix row "Feature content hosts". The key parses
with the catalog's matcher; a list that names X permits X and denies Y; a gate
pinned off denies every host; no policy, or no list, permits as before. Then the
call sites: the manifest walk, a mirror's redirect hops, and each clip request.
"""

from __future__ import annotations

import urllib.request
from types import SimpleNamespace
from unittest.mock import MagicMock

import pytest

from kiro_crew.platform.governance import CapabilityGate, parse_policy, resolve

#: (scope, inner key, matcher, an item the list allows, an item it does not)
_CASES = (
    (
        "capabilities.feature_videos_download",
        "hosts",
        "host",
        "mirror.corp.example",
        "d3j0sthz5doyui.cloudfront.net",
    ),
)
_IDS = [f"{scope}.{key}" for scope, key, *_ in _CASES]


def _policy(scope: str, body: dict) -> dict:
    return {
        "version": 1,
        "boot": {"fail_closed": True},
        "capabilities": {scope.split(".", 1)[1]: body},
    }


def _allow(scope: str, key: str, allowed: str) -> dict:
    return _policy(scope, {"scopes": {key: {"mode": "allow", "allow": [allowed]}}})


@pytest.fixture
def isolated_home(tmp_path, monkeypatch):
    monkeypatch.setenv("KIROCREW_HOME", str(tmp_path))
    return tmp_path


@pytest.fixture
def fake_sel(monkeypatch):
    import kiro_crew.sel as sel_mod

    fake = MagicMock()
    monkeypatch.setattr(sel_mod, "sel", lambda: fake)
    return fake


def _install_policy(monkeypatch, doc: dict | None) -> None:
    from kiro_crew.metrics import provider
    from kiro_crew.platform import context as pc

    ceiling = parse_policy(doc) if doc is not None else None
    ctx = SimpleNamespace(governance=ceiling)
    monkeypatch.setattr(pc, "current_context", lambda: ctx)
    # ``metrics.provider`` binds the name at import, so it is patched there too.
    monkeypatch.setattr(provider, "current_context", lambda: ctx)


# ── The keys, through the real evaluator ────────────────────────────────


class TestListKeys:
    @pytest.mark.parametrize(("scope", "key", "matcher", "yes", "no"), _CASES, ids=_IDS)
    def test_each_key_parses_with_its_catalog_matcher(self, scope, key, matcher, yes, no) -> None:
        gate = parse_policy(_allow(scope, key, yes)).get(scope)
        assert isinstance(gate, CapabilityGate)
        assert gate.enabled is True  # every one of these rows defaults on
        assert gate.scopes[key].matcher == matcher

    @pytest.mark.parametrize(("scope", "key", "matcher", "yes", "no"), _CASES, ids=_IDS)
    def test_the_list_permits_what_it_names_and_denies_the_rest(
        self, scope, key, matcher, yes, no
    ) -> None:
        ceiling = parse_policy(_allow(scope, key, yes))
        assert resolve(ceiling, None, scope, f"{key}:{yes}").permitted is True
        assert resolve(ceiling, None, scope, f"{key}:{no}").permitted is False

    @pytest.mark.parametrize(("scope", "key", "matcher", "yes", "no"), _CASES, ids=_IDS)
    def test_a_gate_pinned_off_denies_every_item(self, scope, key, matcher, yes, no) -> None:
        ceiling = parse_policy(_policy(scope, {"enabled": False}))
        assert resolve(ceiling, None, scope, f"{key}:{yes}").permitted is False

    @pytest.mark.parametrize(("scope", "key", "matcher", "yes", "no"), _CASES, ids=_IDS)
    def test_no_policy_or_no_list_permits_as_before(self, scope, key, matcher, yes, no) -> None:
        assert resolve(None, None, scope, f"{key}:{no}").permitted is True
        ceiling = parse_policy(_policy(scope, {"enabled": True}))
        assert resolve(ceiling, None, scope, f"{key}:{no}").permitted is True


# ── The call sites ──────────────────────────────────────────────────────


class TestFeatureVideoHosts:
    _SCOPE = "capabilities.feature_videos_download"

    def test_host_denied_follows_the_list(self, isolated_home, fake_sel, monkeypatch) -> None:
        from kiro_crew import feature_videos_manifest as fvm

        _install_policy(monkeypatch, _allow(self._SCOPE, "hosts", "mirror.corp.example"))
        assert fvm.host_denied("https://mirror.corp.example/fv/manifest.json") is False
        assert fvm.host_denied(f"{fvm.DEFAULT_CDN_BASE}/0.9.0/manifest.json") is True
        items = [c[1]["item"] for c in fake_sel.log_governance_decision.call_args_list]
        assert items == [
            "hosts:mirror.corp.example",
            "hosts:d3j0sthz5doyui.cloudfront.net",
        ]

    def test_host_denied_permits_without_a_policy(self, isolated_home, fake_sel, monkeypatch):
        from kiro_crew import feature_videos_manifest as fvm

        _install_policy(monkeypatch, None)
        assert fvm.host_denied(f"{fvm.DEFAULT_CDN_BASE}/0.9.0/manifest.json") is False

    def test_the_manifest_walk_makes_no_request_to_an_unlisted_host(
        self, isolated_home, fake_sel, monkeypatch
    ) -> None:
        from kiro_crew import feature_videos_manifest as fvm

        monkeypatch.delenv(fvm.MANIFEST_URL_ENV, raising=False)
        _install_policy(monkeypatch, _allow(self._SCOPE, "hosts", "mirror.corp.example"))
        fetched: list[str] = []
        monkeypatch.setattr(fvm, "_fetch_json", lambda url, **_k: fetched.append(url))
        assert fvm.fetch_manifest("0.9.3") == (None, {})
        assert fetched == []

    def test_a_mirror_redirect_to_an_unlisted_host_is_refused(
        self, isolated_home, fake_sel, monkeypatch
    ) -> None:
        """The operator-mirror fetch may follow a cross-host redirect; each hop is
        held to the list like the first url."""
        from kiro_crew import asset_downloader
        from kiro_crew import feature_videos_manifest as fvm

        _install_policy(monkeypatch, _allow(self._SCOPE, "hosts", "mirror.corp.example"))
        seen: dict = {}
        real = asset_downloader.build_opener

        def _capture(*a, **kw):
            seen.update(kw)
            raise OSError("no network in tests")

        monkeypatch.setattr(asset_downloader, "build_opener", _capture)
        assert fvm._fetch_json("https://mirror.corp.example/m.json", operator_url=True) is None
        assert seen["allow_cross_host_redirects"] is True
        guard = seen["redirect_allowed"]
        assert guard("https://mirror.corp.example/b/m.json") is True
        assert guard("https://bucket.storage.example/m.json") is False

        handler = next(
            h
            for h in real(allow_cross_host_redirects=True, redirect_allowed=guard).handlers
            if isinstance(h, asset_downloader._GuardedRedirect)
        )
        req = urllib.request.Request("https://mirror.corp.example/m.json")
        with pytest.raises(asset_downloader.RedirectRefused):
            handler.redirect_request(
                req, None, 302, "Found", {}, "https://bucket.storage.example/x"
            )
        assert (
            handler.redirect_request(req, None, 302, "Found", {}, "https://mirror.corp.example/y")
            is not None
        )

    def test_a_clip_on_an_unlisted_host_is_not_downloaded(
        self, isolated_home, fake_sel, monkeypatch
    ) -> None:
        from kiro_crew import asset_downloader
        from kiro_crew import feature_videos_cache as fvc
        from kiro_crew import feature_videos_manifest as fvm

        _install_policy(monkeypatch, _allow(self._SCOPE, "hosts", "mirror.corp.example"))

        def _never(*_a, **_k):
            raise AssertionError("an unlisted host must not be fetched")

        monkeypatch.setattr(asset_downloader, "download_to", _never)
        entry = SimpleNamespace(id="a", poster="a.jpg", file="a.mp4")
        current = fvm.VideoManifest(
            release="0.9.0", cdn_base=fvm.DEFAULT_CDN_BASE, generated_at="", entries=()
        )
        ok, err = fvc.FeatureVideoCache()._download_entry_into(MagicMock(), entry, current, 0)
        assert (ok, err) == (False, fvc.HOST_DENIED)


class TestUrlReadings:
    """Every reading of a url's host is asked, so parser disagreements and
    root-dot spellings cannot reach a host the list refused."""

    _SCOPE = "capabilities.feature_videos_download"

    def test_a_backslash_authority_is_refused(self, isolated_home, fake_sel, monkeypatch) -> None:
        from kiro_crew import feature_videos_manifest as fvm

        _install_policy(monkeypatch, _allow(self._SCOPE, "hosts", "mirror.corp.example"))
        assert fvm.host_denied("https://evil.example\\@mirror.corp.example/m.json") is True

    def test_a_root_dot_cannot_dodge_a_deny(self, isolated_home, fake_sel, monkeypatch) -> None:
        from kiro_crew import feature_videos_manifest as fvm

        doc = _policy(
            self._SCOPE, {"scopes": {"hosts": {"mode": "deny", "deny": ["evil.example"]}}}
        )
        _install_policy(monkeypatch, doc)
        assert fvm.host_denied("https://evil.example./m.json") is True
        assert fvm.host_denied("https://mirror.corp.example/m.json") is False

    def test_a_percent_encoded_redirect_target_is_refused(
        self, isolated_home, fake_sel, monkeypatch
    ) -> None:
        """urllib percent-decodes the authority, so the decoded host is asked too."""
        from kiro_crew import feature_videos_manifest as fvm

        doc = _policy(
            self._SCOPE, {"scopes": {"hosts": {"mode": "deny", "deny": ["evil.example"]}}}
        )
        _install_policy(monkeypatch, doc)
        assert fvm.host_denied("https://%65vil.example/manifest.json") is True

    @pytest.mark.parametrize("value", [["mirror.example.com"], "mirror.example.com", 1, None])
    def test_a_host_list_in_the_wrong_shape_fails_to_load(self, value) -> None:
        """A malformed host list must not load as no list, which permits every host."""
        from kiro_crew.platform.context import PlatformCompositionError
        from kiro_crew.platform.governance import parse_policy

        with pytest.raises(PlatformCompositionError, match="scopes.hosts must be an object"):
            parse_policy(_policy(self._SCOPE, {"scopes": {"hosts": value}}))

    def test_an_odd_ascii_redirect_target_is_refused_by_a_deny_glob(
        self, isolated_home, fake_sel, monkeypatch
    ) -> None:
        """A ``+`` is no DNS name character, but a resolver may still answer for it,
        so the host is asked as read rather than dropped."""
        from kiro_crew import feature_videos_manifest as fvm

        doc = _policy(
            self._SCOPE, {"scopes": {"hosts": {"mode": "deny", "deny": ["*.blocked.example"]}}}
        )
        _install_policy(monkeypatch, doc)
        assert fvm.host_denied("https://clip+one.blocked.example/manifest.json") is True
        assert fvm.host_denied("https://mirror.corp.example/manifest.json") is False

    def test_a_probe_error_logs_no_url(self, isolated_home, fake_sel, monkeypatch, caplog) -> None:
        import logging

        from kiro_crew import feature_videos_manifest as fvm

        def _boom(*_a, **_k):
            raise ValueError("https://user:s3cret@mirror.corp.example/")

        monkeypatch.setattr(fvm._governance_profiles, "vet_and_audit", _boom)
        with caplog.at_level(logging.DEBUG, logger=fvm.logger.name):
            assert fvm.host_denied("https://user:s3cret@mirror.corp.example/m.json") is True
        assert "s3cret" not in caplog.text


class TestUrllibIdna:
    """The manifest and media transfers use urllib, which IDNA-encodes a non-ASCII
    host with Python's own codec, so that is the host a deny names."""

    _SCOPE = "capabilities.feature_videos_download"

    @pytest.mark.parametrize(
        "url",
        ["https://stra\u00dfe.example/m.json", "https://stra%C3%9Fe.example/m.json"],
        ids=["sharp-s", "percent-encoded"],
    )
    def test_a_deny_names_the_host_urllib_connects_to(
        self, isolated_home, fake_sel, monkeypatch, url
    ) -> None:
        from kiro_crew import feature_videos_manifest as fvm

        doc = _policy(
            self._SCOPE, {"scopes": {"hosts": {"mode": "deny", "deny": ["strasse.example"]}}}
        )
        _install_policy(monkeypatch, doc)
        assert fvm.host_denied(url) is True
