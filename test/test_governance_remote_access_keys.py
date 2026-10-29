"""Remote-access governance keys: ``bind_address``, ``dashboard_origins`` and
``computer_use``, and tailnet access following ``capabilities.tailnet_origin``.

RFC ``rfc-crew-enterprise-edition``, appendix rows "Bind address and tunnel",
"Dashboard origins" and "Desktop control". In an enterprise build each is at its
minimal set until a policy names it (D3). In an ``oss`` build with no policy naming
the key, behaviour is unchanged.
"""

from __future__ import annotations

import asyncio
import dataclasses
from types import SimpleNamespace

import pytest

from kiro_crew.computer_use import enable_state
from kiro_crew.dashboard import tailnet, urls
from kiro_crew.platform import governance_profiles
from kiro_crew.platform.governance import SCOPE_CATALOG, compose_tier_ladder, parse_policy
from kiro_crew.platform.governance_parts.tiers import TIER_CENTRAL, TIER_HOME

_SUFFIX = "tail1234.ts.net"
_NAME = f"desk.{_SUFFIX}"


@pytest.fixture
def enterprise(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr("kiro_crew.edition.EDITION", "enterprise")


@pytest.fixture
def install_policy(monkeypatch: pytest.MonkeyPatch):
    """Install a composed context carrying *doc* (or no policy) and no profile."""

    def _install(doc: "dict | None", local: "dict | None" = None) -> None:
        """*doc* as the central tier and *local* as the home tier, folded for real."""
        from kiro_crew.platform import context as pc

        tiers = []
        for body, tier in ((doc, TIER_CENTRAL), (local, TIER_HOME)):
            if body is not None:
                parsed = parse_policy({"version": 1, "boot": {}, **body})
                tiers.append(dataclasses.replace(parsed, tier=tier))
        ceiling = compose_tier_ladder(*tiers) if tiers else None
        ctx = SimpleNamespace(governance=ceiling)
        monkeypatch.setattr(pc, "current_context", lambda: ctx)
        monkeypatch.setattr(pc, "installed_context", lambda: ctx)
        monkeypatch.setattr(governance_profiles, "resolve_active_scope", lambda *a, **k: None)

    _install(None)
    return _install


def _allow(*items: str) -> dict:
    return {"mode": "allow", "allow": list(items)}


def test_catalog_rows() -> None:
    assert SCOPE_CATALOG["bind_address"].matcher == "ip"
    assert SCOPE_CATALOG["dashboard_origins"].matcher == "origin"
    assert SCOPE_CATALOG["capabilities.computer_use"].kind == "capability"


# ── bind_address ────────────────────────────────────────────────────────


class TestBindAddress:
    def test_loopback_is_never_asked(self, enterprise, monkeypatch) -> None:
        monkeypatch.delenv("KIROCREW_BIND", raising=False)
        monkeypatch.setattr(
            "kiro_crew.edition_locks.policy_permits",
            lambda *a: pytest.fail("loopback must not be asked"),
        )
        assert urls.bind_address_for(local_only=True) == "127.0.0.1"

    def test_oss_env_bind_still_applies(self, install_policy, monkeypatch) -> None:
        monkeypatch.setenv("KIROCREW_BIND", "0.0.0.0")
        assert urls.bind_address_for(local_only=True) == "0.0.0.0"
        assert urls.bind_address_for(local_only=False) == "0.0.0.0"

    def test_oss_policy_narrows_the_env_bind(self, install_policy, monkeypatch) -> None:
        install_policy({"bind_address": _allow("10.0.0.5")})
        monkeypatch.setenv("KIROCREW_BIND", "0.0.0.0")
        assert urls.bind_address_for(local_only=True) == "127.0.0.1"

    def test_enterprise_unnamed_binds_loopback(self, enterprise, install_policy, monkeypatch):
        monkeypatch.setenv("KIROCREW_BIND", "0.0.0.0")
        assert urls.bind_address_for(local_only=True) == "127.0.0.1"
        install_policy({})
        assert urls.bind_address_for(local_only=True) == "127.0.0.1"
        assert urls.bind_address_for(local_only=False) == "127.0.0.1"

    def test_enterprise_policy_widens(self, enterprise, install_policy, monkeypatch) -> None:
        install_policy({"bind_address": _allow("0.0.0.0", "::")})
        monkeypatch.setenv("KIROCREW_BIND", "0.0.0.0")
        assert urls.bind_address_for(local_only=True) == "0.0.0.0"
        install_policy({"bind_address": _allow("0:0:0:0:0:0:0:0")})
        monkeypatch.setenv("KIROCREW_BIND", "::")
        assert urls.bind_address_for(local_only=True) == "::"
        install_policy({"bind_address": _allow("0.0.0.0", "::")})
        # Asked as canonical IP text, so a long spelling matches the entry.
        monkeypatch.setenv("KIROCREW_BIND", "0:0:0:0:0:0:0:0")
        assert urls.bind_address_for(local_only=True) == "0:0:0:0:0:0:0:0"

    def test_the_bind_decision_is_audited(self, enterprise, install_policy, monkeypatch) -> None:
        import kiro_crew.sel as sel_mod

        records: list[dict] = []
        fake = SimpleNamespace(log_governance_decision=lambda **kw: records.append(kw))
        monkeypatch.setattr(sel_mod, "sel", lambda: fake)
        monkeypatch.setenv("KIROCREW_BIND", "0.0.0.0")
        install_policy({})
        assert urls.bind_address_for(local_only=True) == "127.0.0.1"
        assert [(r["scope"], r["item"], r["outcome"]) for r in records] == [
            ("bind_address", "0.0.0.0", "denied")
        ]

    def test_the_oss_shortcut_is_audited(self, install_policy, monkeypatch) -> None:
        import kiro_crew.sel as sel_mod

        records: list[dict] = []
        fake = SimpleNamespace(log_governance_decision=lambda **kw: records.append(kw))
        monkeypatch.setattr(sel_mod, "sel", lambda: fake)
        monkeypatch.setenv("KIROCREW_BIND", "0.0.0.0")
        install_policy({})
        assert urls.bind_address_for(local_only=True) == "0.0.0.0"
        assert [(r["scope"], r["outcome"], r["layer"]) for r in records] == [
            ("bind_address", "allowed", "default")
        ]

    def test_a_profile_deny_does_not_narrow_the_bind(
        self, enterprise, install_policy, monkeypatch
    ) -> None:
        install_policy({"bind_address": _allow("0.0.0.0")})
        profile = governance_profiles.deny_all_profile("test")
        monkeypatch.setattr(governance_profiles, "resolve_active_scope", lambda *a, **k: profile)
        monkeypatch.setenv("KIROCREW_BIND", "0.0.0.0")
        assert urls.bind_address_for(local_only=True) == "0.0.0.0"

    def test_a_governance_error_binds_loopback(self, install_policy, monkeypatch) -> None:
        def _boom(*a, **k):
            raise RuntimeError("unreadable")

        install_policy({"bind_address": _allow("0.0.0.0")})
        monkeypatch.setattr("kiro_crew.platform.governance.resolve", _boom)
        monkeypatch.setenv("KIROCREW_BIND", "0.0.0.0")
        assert urls.bind_address_for(local_only=True) == "127.0.0.1"


# ── dashboard_origins ───────────────────────────────────────────────────

_FLOOR = {
    "http://127.0.0.1:5476",
    "http://localhost:5476",
    "http://[::1]:5476",
    "http://kirocrew.localhost:5476",
}


def _origins(monkeypatch, *, tailnet_host: str = "") -> set[str]:
    monkeypatch.setenv("KIROCREW_CORS_ORIGINS", "https://other.example.com")
    monkeypatch.setenv("KIROCREW_ALLOWED_LOOPBACK_PORTS", "8777")
    return urls.build_allowed_origins(
        5476,
        True,
        configured_host="crew.example.com",
        dashboard_url="https://crew.example.com",
        tailnet_host=tailnet_host,
    )


class TestDashboardOrigins:
    def test_oss_keeps_every_origin(self, install_policy, monkeypatch) -> None:
        got = _origins(monkeypatch)
        assert {"https://crew.example.com", "https://other.example.com"} <= got
        assert (got) >= {"http://localhost:8777"}

    def test_no_extras_asks_nothing(self, enterprise, monkeypatch) -> None:
        monkeypatch.delenv("KIROCREW_CORS_ORIGINS", raising=False)
        monkeypatch.delenv("KIROCREW_ALLOWED_LOOPBACK_PORTS", raising=False)
        monkeypatch.delenv("KIROCREW_HOME", raising=False)
        monkeypatch.setattr(
            "kiro_crew.edition_locks.policy_permits",
            lambda *a: pytest.fail("the floor must not be asked"),
        )
        assert urls.build_allowed_origins(5476, True) == _FLOOR

    def test_enterprise_unnamed_keeps_this_machine_only(
        self, enterprise, install_policy, monkeypatch
    ) -> None:
        got = _origins(monkeypatch, tailnet_host=_NAME)
        assert got == _FLOOR | {f"https://{_NAME}"}
        assert urls.build_allowed_hosts(got) == {
            "localhost",
            "127.0.0.1",
            "::1",
            "kirocrew.localhost",
            _NAME,
        }

    def test_enterprise_policy_names_one(self, enterprise, install_policy, monkeypatch) -> None:
        install_policy({"dashboard_origins": _allow("https://crew.example.com")})
        assert _origins(monkeypatch) == _FLOOR | {"https://crew.example.com"}

    def test_an_ipv6_origin_matches_its_own_entry(self, install_policy, monkeypatch) -> None:
        v6 = "https://[2001:db8::1]:8443"
        monkeypatch.setenv("KIROCREW_CORS_ORIGINS", v6)
        install_policy({"dashboard_origins": {"mode": "deny", "deny": [v6]}})
        assert (urls.build_allowed_origins(5476, True)).isdisjoint({v6})
        install_policy({"dashboard_origins": _allow(v6)})
        assert (urls.build_allowed_origins(5476, True)) >= {v6}

    @pytest.mark.parametrize(
        "spoof", ["https://evil.com/.example.com", "https://evil.com?.example.com"]
    )
    def test_oss_a_glob_cannot_be_met_by_a_path(self, install_policy, monkeypatch, spoof):
        monkeypatch.setenv("KIROCREW_CORS_ORIGINS", spoof)
        install_policy({"dashboard_origins": _allow("https://*.example.com")})
        got = urls.build_allowed_origins(5476, True)
        assert (got).isdisjoint({spoof})
        assert "evil.com" not in urls.build_allowed_hosts(got)

    def test_a_deny_judges_the_host_a_bad_port_would_trust(
        self, install_policy, monkeypatch
    ) -> None:
        monkeypatch.setenv("KIROCREW_CORS_ORIGINS", "https://evil.com:bad")
        install_policy({"dashboard_origins": {"mode": "deny", "deny": ["https://evil.com"]}})
        assert (urls.build_allowed_origins(5476, True)).isdisjoint({"https://evil.com:bad"})

    def test_a_deny_all_refuses_an_unusual_but_valid_host(
        self, install_policy, monkeypatch
    ) -> None:
        odd = "https://crew+test.example.com"
        monkeypatch.setenv("KIROCREW_CORS_ORIGINS", odd)
        install_policy({"dashboard_origins": {"mode": "deny", "deny": ["*"]}})
        assert (urls.build_allowed_origins(5476, True)).isdisjoint({odd})

    def test_an_unmatchable_item_is_refused_while_governed(
        self, install_policy, monkeypatch
    ) -> None:
        install_policy({"dashboard_origins": {"mode": "deny", "deny": ["https://x.example"]}})
        assert urls.origin_permitted("https://a b.example") is False
        records: list[dict] = []
        import kiro_crew.sel as sel_mod

        fake = SimpleNamespace(log_governance_decision=lambda **kw: records.append(kw))
        monkeypatch.setattr(sel_mod, "sel", lambda: fake)
        assert urls.origin_permitted("https://a b.example", audit_tool="t") is False
        assert [r["outcome"] for r in records] == ["denied"]
        install_policy(None)
        assert urls.origin_permitted("https://a b.example") is True

    @pytest.mark.parametrize("entry", ["https://evil.example.com/", "https://evil.example.com:443"])
    def test_a_deny_entry_in_another_spelling_still_denies(
        self, install_policy, monkeypatch, entry
    ) -> None:
        monkeypatch.setenv("KIROCREW_CORS_ORIGINS", "https://evil.example.com")
        install_policy({"dashboard_origins": {"mode": "deny", "deny": [entry]}})
        assert (urls.build_allowed_origins(5476, True)).isdisjoint({"https://evil.example.com"})

    def test_port_80_loopback_stays_trusted(self, enterprise, install_policy) -> None:
        from kiro_crew.dashboard.origin import check_origin

        install_policy({})
        req = _origin_request("http://localhost", "localhost")
        req.app = {"allowed_origins": urls.build_allowed_origins(80, True)}
        assert check_origin(req) is True

    @pytest.mark.parametrize(
        ("configured", "entry"),
        [
            ("https://[2001:db8::1]", "https://[2001:0db8:0:0:0:0:0:1]"),
            ("https://bücher.example", "https://xn--bcher-kva.example"),
            ("https://crew.example.com.", "https://crew.example.com"),
            ("HTTPS://Crew.Example.com:0443", "https://crew.example.com"),
        ],
    )
    def test_equal_spellings_meet_a_deny(self, install_policy, monkeypatch, configured, entry):
        monkeypatch.setenv("KIROCREW_CORS_ORIGINS", configured)
        install_policy({"dashboard_origins": {"mode": "deny", "deny": [entry]}})
        assert (urls.build_allowed_origins(5476, True)).isdisjoint({configured})

    def test_a_loopback_deny_in_long_form_refuses_the_fallback(self, install_policy) -> None:
        from kiro_crew.dashboard.origin import check_origin

        install_policy(
            {"dashboard_origins": {"mode": "deny", "deny": ["http://[0:0:0:0:0:0:0:1]:8777"]}}
        )
        assert check_origin(_origin_request("http://[::1]:8777", "[::1]:8777")) is False

    def test_enterprise_refuses_a_deny_list_at_load(self, enterprise) -> None:
        from kiro_crew.platform.context import PlatformCompositionError

        with pytest.raises(PlatformCompositionError, match="dashboard_origins.*allow list"):
            parse_policy({"version": 1, "boot": {}, "dashboard_origins": {"mode": "deny"}})

    def test_oss_still_loads_a_deny_list(self) -> None:
        parse_policy({"version": 1, "boot": {}, "dashboard_origins": {"mode": "deny"}})

    @pytest.mark.parametrize(
        "entry",
        [
            "https://*.example.com",
            "https://?.example.com",
            "https://[ab]c.example.com",
            "https://faß.de",
            "https://crew.example.com/",
            "https://crew.example.com/app",
            "https://u@crew.example.com",
            "https://crew.example.com:443",
            "http://crew.example.com:80",
            "HTTPS://crew.example.com",
            # No dashboard listens on port 0, and the origin set drops a ``:0``
            # (``urls.dashboard_origin``), so such an entry could never match.
            "https://crew.example.com:0",
        ],
    )
    def test_enterprise_refuses_an_inexact_entry_at_load(self, enterprise, entry) -> None:
        from kiro_crew.platform.context import PlatformCompositionError

        with pytest.raises(PlatformCompositionError, match="dashboard_origins.*not an exact"):
            parse_policy({"version": 1, "boot": {}, "dashboard_origins": _allow(entry)})

    @pytest.mark.parametrize(
        ("entry", "canonical"),
        [
            ("https://[2001:0db8:0:0:0:0:0:1]", "https://[2001:db8::1]"),
            ("https://10.1", "https://10.0.0.1"),
            ("https://[::ffff:192.0.2.1]", "https://[::ffff:c000:201]"),
            ("https://0x7f.1:8443", "https://127.0.0.1:8443"),
            ("https://[1:0:0:2:0:0:0:3]", "https://[1:0:0:2::3]"),
        ],
    )
    def test_enterprise_refuses_a_non_browser_spelling_and_names_it(
        self, enterprise, entry, canonical
    ) -> None:
        from kiro_crew.platform.context import PlatformCompositionError

        with pytest.raises(PlatformCompositionError, match="not in the form a browser") as exc:
            parse_policy({"version": 1, "boot": {}, "dashboard_origins": _allow(entry)})
        assert repr(canonical) in str(exc.value)

    @pytest.mark.parametrize(
        "entry", ["https://256.1.1.1", "http://127.0.0.1:99999", "https://a.0x"]
    )
    def test_enterprise_refuses_an_invalid_ip_or_port(self, enterprise, entry) -> None:
        from kiro_crew.platform.context import PlatformCompositionError

        with pytest.raises(PlatformCompositionError, match="not a valid origin"):
            parse_policy({"version": 1, "boot": {}, "dashboard_origins": _allow(entry)})

    @pytest.mark.parametrize(
        "entry",
        [
            "https://[2001:db8::1]",
            "https://10.0.0.1",
            "https://[::ffff:c000:201]",
            "https://[::1]:8443",
            "https://1.example.com",
            "https://crew.example.com:8443",
            "https://example.0xg",
            "https://foo.0xbar",
        ],
    )
    def test_enterprise_accepts_the_browser_spelling(
        self, enterprise, install_policy, monkeypatch, entry
    ) -> None:
        from kiro_crew.dashboard.origin import check_origin

        parse_policy({"version": 1, "boot": {}, "dashboard_origins": _allow(entry)})
        install_policy({"dashboard_origins": _allow(entry)})
        monkeypatch.setenv("KIROCREW_CORS_ORIGINS", entry)
        req = _origin_request(entry, entry.split("://", 1)[1])
        req.app = {"allowed_origins": urls.build_allowed_origins(5476, True)}
        req.remote = "10.0.0.5"
        assert check_origin(req) is True

    @pytest.mark.parametrize("entry", ["https://[2001:0db8:0:0:0:0:0:1]", "https://10.1"])
    def test_oss_loads_any_spelling(self, entry) -> None:
        parse_policy({"version": 1, "boot": {}, "dashboard_origins": _allow(entry)})

    @pytest.mark.parametrize(
        ("configured", "entry"),
        [
            ("https://crew.example.com", "https://crew.example.com"),
            ("https://Crew.Example.com:443", "https://crew.example.com"),
            ("https://xn--fa-hia.de", "https://xn--fa-hia.de"),
            ("https://crew.example.com:8443", "https://crew.example.com:8443"),
        ],
    )
    def test_enterprise_exact_entry_is_allowed(
        self, enterprise, install_policy, monkeypatch, configured, entry
    ) -> None:
        monkeypatch.setenv("KIROCREW_CORS_ORIGINS", configured)
        install_policy({"dashboard_origins": _allow(entry)})
        # Stored as the entry it was approved under, the form a browser sends.
        assert (urls.build_allowed_origins(5476, True)) >= {entry}

    @pytest.mark.parametrize(
        ("configured", "entry"),
        [
            ("https://fass.de", "https://xn--fa-hia.de"),
            ("https://faß.de", "https://xn--fa-hia.de"),
            ("https://crew.example.com.", "https://crew.example.com"),
            ("https://a.crew.example.com", "https://crew.example.com"),
            ("http://crew.example.com", "https://crew.example.com"),
            ("https://crew.example.com:8443", "https://crew.example.com"),
        ],
    )
    def test_enterprise_near_miss_is_refused(
        self, enterprise, install_policy, monkeypatch, configured, entry
    ) -> None:
        monkeypatch.setenv("KIROCREW_CORS_ORIGINS", configured)
        install_policy({"dashboard_origins": _allow(entry)})
        assert (urls.build_allowed_origins(5476, True)).isdisjoint({configured})

    def test_oss_still_loads_a_glob_entry(self, install_policy, monkeypatch) -> None:
        monkeypatch.setenv("KIROCREW_CORS_ORIGINS", "https://a.example.com")
        install_policy({"dashboard_origins": _allow("https://*.example.com")})
        assert (urls.build_allowed_origins(5476, True)) >= {"https://a.example.com"}

    @pytest.mark.parametrize(
        "odd",
        [
            "https://crew.example.com.evil.com",
            "https://crew+x.example.com",
            "https://[2001:db8::1]",
            "https://evil.com/crew.example.com",
        ],
    )
    def test_enterprise_odd_spellings_outside_the_list_are_refused(
        self, enterprise, install_policy, monkeypatch, odd
    ) -> None:
        monkeypatch.setenv("KIROCREW_CORS_ORIGINS", odd)
        install_policy({"dashboard_origins": _allow("https://crew.example.com")})
        got = urls.build_allowed_origins(5476, True)
        assert (got).isdisjoint({odd})
        assert got == _FLOOR

    def test_enterprise_check_origin_refuses_another_spelling(
        self, enterprise, install_policy
    ) -> None:
        from kiro_crew.dashboard.origin import check_origin

        install_policy({"dashboard_origins": _allow("https://crew.example.com")})
        allowed = _FLOOR | {"https://crew.example.com"}
        for origin, ok in (
            ("https://crew.example.com", True),
            ("https://crew.example.com.", False),
            ("https://CREW.example.com.", False),
        ):
            req = _origin_request(origin, "crew.example.com")
            req.app = {"allowed_origins": allowed}
            assert check_origin(req) is ok, origin

    def test_oss_check_origin_still_accepts_another_spelling(self, install_policy) -> None:
        from kiro_crew.dashboard.origin import check_origin

        req = _origin_request("https://crew.example.com.", "crew.example.com")
        req.app = {"allowed_origins": _FLOOR | {"https://crew.example.com"}}
        assert check_origin(req) is True

    def test_oss_idna_is_the_browser_form(self, install_policy, monkeypatch) -> None:
        from kiro_crew.platform.governance_parts import matching

        monkeypatch.setenv("KIROCREW_CORS_ORIGINS", "https://fass.de,https://xn--fa-hia.de")
        install_policy({"dashboard_origins": _allow("https://faß.de")})
        got = urls.build_allowed_origins(5476, True)
        assert got >= {"https://xn--fa-hia.de"}
        assert got.isdisjoint({"https://fass.de"})
        # Without the ``idna`` package a non-ASCII host matches nothing.
        monkeypatch.setattr(matching, "_browser_idna", lambda host: None)
        assert not matching._match_origin("https://xn--fa-hia.de", "https://faß.de")

    def test_the_origin_matcher_refuses_a_non_origin(self) -> None:
        from kiro_crew.platform.governance_parts.matching import _match_origin

        assert _match_origin("https://a.example.com", "https://*.example.com")
        assert not _match_origin("https://evil.com/.example.com", "https://*.example.com")
        assert not _match_origin("https://u@a.example.com", "https://*")

    def test_oss_policy_narrows(self, install_policy, monkeypatch) -> None:
        install_policy({"dashboard_origins": _allow("https://crew.example.com")})
        assert (_origins(monkeypatch)).isdisjoint({"https://other.example.com"})


class TestApprovedOriginSpelling:
    """An approved origin is stored in the form ``check_origin`` compares."""

    _CONFIGURED = "https://Crew.Example.com:443"

    def _request(self, monkeypatch, origin: str):
        from unittest.mock import MagicMock

        monkeypatch.setenv("KIROCREW_CORS_ORIGINS", self._CONFIGURED)
        monkeypatch.delenv("KIROCREW_ALLOWED_LOOPBACK_PORTS", raising=False)
        req = MagicMock()
        req.app = {"allowed_origins": urls.build_allowed_origins(5476, True)}
        req.headers = {"Origin": origin, "Host": "crew.example.com"}
        req.remote = "10.0.0.5"
        return req

    def test_enterprise_stores_the_browser_form(
        self, enterprise, install_policy, monkeypatch
    ) -> None:
        install_policy({"dashboard_origins": _allow("https://crew.example.com")})
        got = self._request(monkeypatch, "https://crew.example.com").app["allowed_origins"]
        assert got == _FLOOR | {"https://crew.example.com"}

    def test_enterprise_accepts_the_approved_origin(
        self, enterprise, install_policy, monkeypatch
    ) -> None:
        from kiro_crew.dashboard.origin import check_origin

        install_policy({"dashboard_origins": _allow("https://crew.example.com")})
        assert check_origin(self._request(monkeypatch, "https://crew.example.com")) is True

    @pytest.mark.parametrize(
        "near",
        [
            "https://crew.example.com.",
            "https://Crew.Example.com:443",
            "https://crew.example.com:443",
            "http://crew.example.com",
            "https://crew.example.co",
        ],
    )
    def test_enterprise_refuses_a_near_spelling(
        self, enterprise, install_policy, monkeypatch, near
    ) -> None:
        from kiro_crew.dashboard.origin import check_origin

        install_policy({"dashboard_origins": _allow("https://crew.example.com")})
        assert check_origin(self._request(monkeypatch, near)) is False

    def test_oss_keeps_the_configured_text(self, install_policy, monkeypatch) -> None:
        got = self._request(monkeypatch, "https://crew.example.com").app["allowed_origins"]
        assert self._CONFIGURED in got


# ── the same-origin loopback fallback ───────────────────────────────────


def _origin_request(origin: str, host: str):
    from unittest.mock import MagicMock

    req = MagicMock()
    req.app = {"allowed_origins": set(_FLOOR)}
    req.headers = {"Origin": origin, "Host": host}
    req.remote = "127.0.0.1"
    return req


class TestLoopbackFallback:
    _FWD = ("http://localhost:8777", "localhost:8777")

    def test_oss_still_accepts_a_forwarded_loopback_port(self, install_policy) -> None:
        from kiro_crew.dashboard.origin import check_origin

        assert check_origin(_origin_request(*self._FWD)) is True

    def test_a_policy_refusing_extras_refuses_it(self, install_policy) -> None:
        from kiro_crew.dashboard.origin import check_origin

        install_policy({"dashboard_origins": {"mode": "deny", "deny": ["*"]}})
        assert check_origin(_origin_request(*self._FWD)) is False
        # The bound port is the floor and stays trusted.
        assert check_origin(_origin_request("http://localhost:5476", "localhost:5476"))

    def test_the_bound_port_ipv6_loopback_stays_trusted(self, enterprise, install_policy):
        from kiro_crew.dashboard.origin import check_origin

        install_policy({})
        assert check_origin(_origin_request("http://[::1]:5476", "[::1]:5476")) is True

    def test_a_long_form_bound_port_ipv6_is_the_floor(self, enterprise, install_policy):
        from kiro_crew.dashboard.origin import check_origin

        install_policy({})
        req = _origin_request("http://[0:0:0:0:0:0:0:1]:5476", "[0:0:0:0:0:0:0:1]:5476")
        assert check_origin(req) is True
        req = _origin_request("http://[0:0:0:0:0:0:0:1]:8777", "[0:0:0:0:0:0:0:1]:8777")
        assert check_origin(req) is False

    def test_each_request_decision_is_audited(self, install_policy, monkeypatch) -> None:
        import kiro_crew.sel as sel_mod
        from kiro_crew.dashboard.origin import check_origin

        records: list[dict] = []
        fake = SimpleNamespace(log_governance_decision=lambda **kw: records.append(kw))
        monkeypatch.setattr(sel_mod, "sel", lambda: fake)
        install_policy({"dashboard_origins": _allow("http://localhost:8777")})
        assert check_origin(_origin_request(*self._FWD)) is True
        assert [(r["item"], r["outcome"]) for r in records] == [
            ("http://localhost:8777", "allowed")
        ]

    def test_enterprise_unnamed_refuses_it(self, enterprise, install_policy) -> None:
        from kiro_crew.dashboard.origin import check_origin

        install_policy({})
        assert check_origin(_origin_request(*self._FWD)) is False


# ── the tunnel origin ───────────────────────────────────────────────────

_TUNNEL_URL = "https://crew-1.tunnel.example"


def _connect_tunnel(monkeypatch, url: str = _TUNNEL_URL) -> set[str]:
    from kiro_crew.tunnel import setup as tunnel_setup

    hooks: dict = {}

    class _FakeManager:
        def __init__(self, port, **kwargs):
            hooks.update(kwargs)

        async def start(self) -> None:
            await hooks["on_connect"](url)

    monkeypatch.setattr(tunnel_setup, "TunnelManager", _FakeManager)
    monkeypatch.setattr(tunnel_setup, "publish_disabled", lambda: False)
    monkeypatch.setattr(tunnel_setup, "set_tunnel_url", lambda url: None)
    origins = set(_FLOOR)
    auth_mw = SimpleNamespace(_is_token_auth=True)
    asyncio.run(
        tunnel_setup.setup_tunnel(
            middlewares=[auth_mw],
            allowed_origins=origins,
            tunnel_name_mode="auto",
            tunnel_name_override="",
            port=5476,
            log_api_access=lambda **kw: None,
        )
    )
    return origins


class TestTunnelOrigin:
    def test_oss_trusts_the_tunnel(self, install_policy, monkeypatch) -> None:
        assert (_connect_tunnel(monkeypatch)) >= {_TUNNEL_URL}

    def test_enterprise_unnamed_does_not_trust_it(
        self, enterprise, install_policy, monkeypatch
    ) -> None:
        install_policy({})
        assert _connect_tunnel(monkeypatch) == _FLOOR

    def test_oss_policy_can_refuse_it(self, install_policy, monkeypatch) -> None:
        install_policy({"dashboard_origins": _allow("https://other.example.com")})
        assert _connect_tunnel(monkeypatch) == _FLOOR

    def test_enterprise_stores_it_in_the_browser_form(
        self, enterprise, install_policy, monkeypatch
    ) -> None:
        install_policy({"dashboard_origins": _allow(_TUNNEL_URL)})
        got = _connect_tunnel(monkeypatch, "https://Crew-1.Tunnel.Example:443")
        assert got == _FLOOR | {_TUNNEL_URL}


# ── tailnet: one policy boolean in an enterprise build ──────────────────

_TAILNET_ON = {"capabilities": {"tailnet_origin": {"enabled": True}}}
_TAILNET_OFF = {"capabilities": {"tailnet_origin": {"enabled": False}}}


class TestTailnetPolicy:
    """In an enterprise build tailnet follows ``capabilities.tailnet_origin`` alone."""

    @pytest.fixture
    def serve_cli(self, monkeypatch):
        """``tailscale serve``: status says the mount is free; writes are recorded."""
        import subprocess

        from kiro_crew.dashboard import tailnet_serve

        writes: list[list[str]] = []

        def _run(argv, *_a, **_k):
            if argv[1:4] == ["serve", "status", "--json"]:
                return subprocess.CompletedProcess(argv, 0, "{}", "")
            writes.append(argv)
            return subprocess.CompletedProcess(argv, 0, "", "")

        monkeypatch.setattr(tailnet_serve, "_cli_path", lambda: "/usr/bin/tailscale")
        monkeypatch.setattr(tailnet_serve.subprocess, "run", _run)
        return writes

    @pytest.fixture
    def no_daemon(self, monkeypatch):
        # Imported before patching, so a module that binds a name at import time
        # (``from ... import self_dns_name``) is loaded first.
        from kiro_crew import cli_commands, cli_server_requests  # noqa: F401
        from kiro_crew.dashboard import tailnet_serve

        def _fail(*_a, **_k):
            pytest.fail("a refusal reads no tailnet daemon")

        monkeypatch.setattr(tailnet, "self_dns_name", _fail)
        monkeypatch.setattr(tailnet, "probe_daemon", _fail)
        monkeypatch.setattr(tailnet_serve, "serve_state", _fail)

    @pytest.fixture
    def records(self, monkeypatch) -> list:
        import kiro_crew.sel as sel_mod

        got: list[dict] = []
        fake = SimpleNamespace(log_governance_decision=lambda **kw: got.append(kw))
        monkeypatch.setattr(sel_mod, "sel", lambda: fake)
        return got

    @staticmethod
    def _rows(records: list) -> list:
        return [
            (r["tool_name"], r["outcome"])
            for r in records
            if r["scope"] == "capabilities.tailnet_origin"
        ]

    # ── the decision ──

    def test_permit_turns_it_on(self, enterprise, install_policy) -> None:
        install_policy(_TAILNET_ON)
        assert tailnet.policy_off() is False

    @pytest.mark.parametrize("doc", [None, {}, _TAILNET_OFF], ids=["none", "silent", "deny"])
    def test_silent_or_deny_leaves_it_off(self, enterprise, install_policy, doc) -> None:
        install_policy(doc)
        assert tailnet.policy_off() is True

    @pytest.mark.parametrize("central", [None, {}], ids=["no-central", "silent-central"])
    def test_a_local_tier_cannot_turn_it_on(self, enterprise, install_policy, central) -> None:
        install_policy(central, local=_TAILNET_ON)
        assert tailnet.policy_off() is True

    def test_local_config_and_env_cannot_turn_it_on(
        self, enterprise, install_policy, monkeypatch
    ) -> None:
        monkeypatch.setenv("KIROCREW_CORS_ORIGINS", f"https://{_NAME}")
        monkeypatch.setattr(tailnet, "self_dns_name", lambda: _NAME)
        install_policy({})
        assert tailnet.policy_off() is True
        # dashboard.tailscale.enabled is the local switch: derivation still refuses.
        assert asyncio.run(tailnet.resolve_tailnet_host(True)) == ""

    def test_oss_reads_nothing_and_is_unchanged(self, install_policy, monkeypatch) -> None:
        monkeypatch.setattr(
            "kiro_crew.edition_locks.policy_permits", lambda *a, **k: pytest.fail("x")
        )
        install_policy(_TAILNET_OFF)
        assert tailnet.policy_off() is False

    # ── publish (dashboard card, mobile, CLI) ──

    @pytest.mark.parametrize("doc", [{}, _TAILNET_OFF], ids=["silent", "deny"])
    @pytest.mark.parametrize("tool", ["tailnet_publish", "tailnet_mobile_publish"])
    def test_publish_is_refused_and_audited(
        self, enterprise, install_policy, serve_cli, no_daemon, records, doc, tool
    ) -> None:
        from kiro_crew.dashboard import tailnet_serve

        install_policy(doc)
        result = tailnet_serve.publish(5476, audit_tool=tool)
        assert (result.ok, result.code) == (False, "policy_off")
        assert "Tailnet is off by policy." in result.detail
        assert serve_cli == []
        assert self._rows(records) == [(tool, "denied")]

    def test_permit_publishes(self, enterprise, install_policy, serve_cli, records) -> None:
        from kiro_crew.dashboard import tailnet_serve

        install_policy(_TAILNET_ON)
        assert tailnet_serve.policy_refusal(audit_tool="t") is None
        assert tailnet_serve.publish(5476).ok
        assert len(serve_cli) == 1
        assert ("tailnet_publish", "allowed") in self._rows(records)

    def test_oss_publishes_as_before(self, install_policy, serve_cli, monkeypatch) -> None:
        from kiro_crew.dashboard import tailnet_serve

        monkeypatch.setattr(tailnet_serve, "is_governance_pinned_off", lambda *a, **k: False)
        assert tailnet_serve.policy_refusal(audit_tool="t") is None
        assert tailnet_serve.publish(5476).ok
        assert len(serve_cli) == 1

    def test_cli_up_refuses_first_and_audits_once(
        self, enterprise, install_policy, no_daemon, records, monkeypatch, tmp_path, capsys
    ) -> None:
        import argparse

        from kiro_crew import cli_commands

        install_policy({})
        monkeypatch.setenv("KIROCREW_HOME", str(tmp_path))
        monkeypatch.setattr(cli_commands, "_marker_port", lambda: 0)
        with pytest.raises(SystemExit) as exc:
            cli_commands._tailnet(argparse.Namespace(tailnet_action="up"))
        assert exc.value.code == 1
        assert "Tailnet is off by policy." in capsys.readouterr().err
        assert self._rows(records) == [("tailnet_cli_up", "denied")]

    def test_cli_status_reads_no_daemon(
        self, enterprise, install_policy, no_daemon, monkeypatch, tmp_path, capsys
    ) -> None:
        import argparse

        from kiro_crew import cli_commands

        install_policy({})
        monkeypatch.setenv("KIROCREW_HOME", str(tmp_path))
        monkeypatch.setattr(cli_commands, "_marker_port", lambda: 5476)
        cli_commands._tailnet(argparse.Namespace(tailnet_action="status"))
        assert "Tailnet is off by policy." in capsys.readouterr().out

    # ── the mobile card ──

    def test_mobile_configure_is_refused_first(
        self, enterprise, install_policy, no_daemon, records, monkeypatch
    ) -> None:
        from kiro_crew.dashboard.handlers import tailnet_mobile

        install_policy({})
        monkeypatch.setattr(tailnet_mobile, "_guard", lambda request: None)
        audits: list[tuple] = []

        async def _audit(request, *args):
            audits.append(args)

        monkeypatch.setattr(tailnet_mobile, "_audit_async", _audit)
        resp = asyncio.run(tailnet_mobile.api_tailnet_mobile_configure(SimpleNamespace()))
        assert resp.status == 403
        assert audits == [("tailnet.mobile.configure", "denied", "policy-off")]
        assert self._rows(records) == [("tailnet_mobile_configure", "denied")]

    def test_the_card_says_off_by_policy_before_install_or_daemon(
        self, enterprise, install_policy, no_daemon
    ) -> None:
        from kiro_crew.dashboard.handlers import tailnet_mobile

        install_policy({})
        live = asyncio.run(tailnet_mobile._live_state(SimpleNamespace(app={}), 5476))
        assert live.step == "policy_off"
        code, sentence = tailnet_mobile._QR_REFUSALS["policy_off"]
        assert code == "policy_off" and "Tailnet is off by policy" in sentence

    # ── origin derivation and recovery ──

    @pytest.mark.parametrize("doc", [{}, _TAILNET_OFF], ids=["silent", "deny"])
    def test_startup_refusal_is_audited(
        self, enterprise, install_policy, no_daemon, records, caplog, doc
    ) -> None:
        # ``server.py`` and ``server_runtime/api_entrypoint.py`` both derive the
        # origin through ``resolve_tailnet_host``, so this covers both entry points.
        install_policy(doc)
        with caplog.at_level("WARNING"):
            assert asyncio.run(tailnet.resolve_tailnet_host(True)) == ""
        assert "Tailnet is off by policy." in caplog.text
        assert self._rows(records) == [("tailnet_origin_resolve", "denied")]

    def test_entry_points_derive_through_the_audited_path(self) -> None:
        from pathlib import Path

        src = Path(tailnet.__file__).resolve().parent
        for rel in ("server.py", "server_runtime/api_entrypoint.py"):
            assert "tailnet.resolve_tailnet_host(" in (src / rel).read_text(encoding="utf-8")

    def test_permit_derives_the_name(self, enterprise, install_policy, monkeypatch) -> None:
        monkeypatch.setattr(tailnet, "self_dns_name", lambda: _NAME)
        install_policy(_TAILNET_ON)
        assert asyncio.run(tailnet.resolve_tailnet_host(True)) == _NAME

    def test_recovery_adds_no_origin_while_off(
        self, enterprise, install_policy, records, monkeypatch
    ) -> None:
        monkeypatch.setattr(tailnet, "_ORIGIN_RECOVERY_INITIAL_SECS", 0)
        monkeypatch.setattr(tailnet, "_ORIGIN_RECOVERY_MAX_SECS", 0)
        monkeypatch.setattr(tailnet, "self_dns_name", lambda: _NAME)
        install_policy({})
        calls = {"n": 0}

        def _enabled() -> bool:
            calls["n"] += 1
            return calls["n"] < 3

        origins: set[str] = set()
        state = tailnet.TailnetOriginState(load_enabled=_enabled)
        asyncio.run(tailnet._recover_tailnet_origin({"allowed_origins": origins}, state))
        assert origins == set()
        assert ("tailnet_origin_recover", "denied") in self._rows(records)


# ── computer_use ────────────────────────────────────────────────────────

_ON = {"enabled": True}


class TestComputerUse:
    def test_oss_opt_in_still_applies(self, install_policy, monkeypatch) -> None:
        # No landed policy names the key: nothing is evaluated at all.
        monkeypatch.setattr(
            governance_profiles,
            "governance_permits",
            lambda *a, **k: pytest.fail("oss must not evaluate an unnamed key"),
        )
        assert enable_state.is_enabled(_ON) is True
        assert enable_state.locked_by_policy() is False
        assert enable_state.is_enabled({}) is False

    def test_oss_policy_pins_it_off(self, install_policy) -> None:
        install_policy({"capabilities": {"computer_use": {"enabled": False}}})
        assert enable_state.is_enabled(_ON) is False
        assert enable_state.locked_by_policy() is True

    def test_enterprise_unnamed_is_off(self, enterprise, install_policy) -> None:
        assert enable_state.is_enabled(_ON) is False
        install_policy({})
        assert enable_state.is_enabled(_ON) is False
        assert enable_state.locked_by_policy() is True

    def test_enterprise_policy_turns_it_on(self, enterprise, install_policy) -> None:
        install_policy({"capabilities": {"computer_use": {"enabled": True}}})
        assert enable_state.is_enabled(_ON) is True
        # The operator's own opt-in still has to say yes.
        assert enable_state.is_enabled({}) is False

    def test_a_profile_deny_is_not_a_policy_lock(
        self, enterprise, install_policy, monkeypatch
    ) -> None:
        install_policy({"capabilities": {"computer_use": {"enabled": True}}})
        profile = governance_profiles.deny_all_profile("test")
        monkeypatch.setattr(governance_profiles, "resolve_active_scope", lambda *a, **k: profile)
        assert enable_state.is_enabled(_ON) is True
