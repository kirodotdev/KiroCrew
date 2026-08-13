"""Forwarded-peer resolution and identity-pinned sessions (RFC Phase 3).

The attack matrix from the RFC's adversarial-review list, pinned as tests:
X-Forwarded-For injection, multi-value XFF, header/whois disagreement,
daemon-absent fallback, timeout fail-closed, tagged-node login-scope collapse,
and allowlist enforcement. Whois is always mocked at the ``_run_json`` /
subprocess seam — no test invokes a real ``tailscale`` binary.
"""

from __future__ import annotations

import json
import subprocess
from types import SimpleNamespace
from typing import Any
from unittest.mock import MagicMock

import pytest
from multidict import CIMultiDict

import kiro_crew.config.loader as loader
from kiro_crew.config.loader import _tailscale_config_from
from kiro_crew.dashboard import tailnet
from kiro_crew.dashboard.tailnet import (
    PIN_SCOPE_LOGIN,
    PIN_SCOPE_NODE,
    TAGGED_DEVICES_LOGIN,
    ForwardedPeer,
    TailnetTrust,
    login_allowed,
    peer_pin_key,
    resolve_forwarded_peer,
)

TRUST = TailnetTrust(
    trust_identity=True,
    allowed_logins=("you@example.com",),
    pin_scope=PIN_SCOPE_NODE,
)


@pytest.fixture(autouse=True)
def _clear_whois_cache(monkeypatch):
    """Isolate the process-global whois cache, and pin IS_POSIX=True so the
    resolution matrix is exercised identically on the Windows CI shard (the
    explicit Windows-degrade test overrides it back to False itself)."""
    monkeypatch.setattr(tailnet, "IS_POSIX", True)
    tailnet._whois_cache.clear()
    yield
    tailnet._whois_cache.clear()


def _req(remote: str, headers: dict[str, str] | None = None) -> Any:
    """Request double: resolution reads only ``.remote`` and ``.headers``."""
    return SimpleNamespace(remote=remote, headers=CIMultiDict(headers or {}))


def _whois_json(login: str = "you@example.com", node: str = "phone.tail.ts.net.") -> dict:
    return {"Node": {"Name": node}, "UserProfile": {"LoginName": login}}


def _patch_whois(monkeypatch, result: dict | None) -> MagicMock:
    """Mock the CLI at the ``_run_json_detail`` seam (result, transient=False)."""
    mock = MagicMock(return_value=(result, False))
    monkeypatch.setattr(tailnet, "_run_json_detail", mock)
    return mock


# ── RFC §2 conditions (a)–(f), each individually fail-closed ──


class TestResolutionMatrix:
    @pytest.mark.asyncio
    async def test_happy_path_resolves_the_daemon_verified_peer(self, monkeypatch) -> None:
        _patch_whois(monkeypatch, _whois_json())
        peer = await resolve_forwarded_peer(
            _req("127.0.0.1", {"X-Forwarded-For": "100.64.0.5"}), TRUST
        )
        assert peer == ForwardedPeer(
            login="you@example.com", node="phone.tail.ts.net", address="100.64.0.5"
        )

    @pytest.mark.asyncio
    async def test_xff_injection_from_non_loopback_peer_is_never_read(self, monkeypatch) -> None:
        """(a) A remote peer's forwarded header is an unverifiable claim."""
        whois = _patch_whois(monkeypatch, _whois_json())
        peer = await resolve_forwarded_peer(
            _req("203.0.113.7", {"X-Forwarded-For": "100.64.0.5"}), TRUST
        )
        assert peer is None
        whois.assert_not_called()

    @pytest.mark.asyncio
    async def test_trust_disabled_never_resolves(self, monkeypatch) -> None:
        whois = _patch_whois(monkeypatch, _whois_json())
        trust_off = TailnetTrust(trust_identity=False, allowed_logins=("you@example.com",))
        peer = await resolve_forwarded_peer(
            _req("127.0.0.1", {"X-Forwarded-For": "100.64.0.5"}), trust_off
        )
        assert peer is None
        whois.assert_not_called()

    @pytest.mark.asyncio
    async def test_empty_allowlist_never_resolves(self, monkeypatch) -> None:
        """(b) empty allowlist means trust was refused at load — belt and braces."""
        whois = _patch_whois(monkeypatch, _whois_json())
        trust = TailnetTrust(trust_identity=True, allowed_logins=())
        peer = await resolve_forwarded_peer(
            _req("127.0.0.1", {"X-Forwarded-For": "100.64.0.5"}), trust
        )
        assert peer is None
        whois.assert_not_called()

    @pytest.mark.asyncio
    async def test_comma_joined_xff_is_rejected_not_first_or_last(self, monkeypatch) -> None:
        """(c) two addresses in one header value = unattributable proxy chain."""
        whois = _patch_whois(monkeypatch, _whois_json())
        peer = await resolve_forwarded_peer(
            _req("127.0.0.1", {"X-Forwarded-For": "100.64.0.5, 100.64.0.6"}), TRUST
        )
        assert peer is None
        whois.assert_not_called()

    @pytest.mark.asyncio
    async def test_repeated_xff_headers_are_rejected(self, monkeypatch) -> None:
        """(c) two header instances are just as unattributable as a comma."""
        whois = _patch_whois(monkeypatch, _whois_json())
        headers = CIMultiDict()
        headers.add("X-Forwarded-For", "100.64.0.5")
        headers.add("X-Forwarded-For", "100.64.0.6")
        peer = await resolve_forwarded_peer(
            SimpleNamespace(remote="127.0.0.1", headers=headers), TRUST
        )
        assert peer is None
        whois.assert_not_called()

    @pytest.mark.asyncio
    async def test_missing_xff_resolves_nothing(self, monkeypatch) -> None:
        whois = _patch_whois(monkeypatch, _whois_json())
        assert await resolve_forwarded_peer(_req("127.0.0.1"), TRUST) is None
        whois.assert_not_called()

    @pytest.mark.asyncio
    @pytest.mark.parametrize("addr", ["203.0.113.7", "10.0.0.5", "not-an-ip", ""])
    async def test_address_outside_tailnet_ranges_is_rejected(self, monkeypatch, addr) -> None:
        """(d) only CGNAT 100.64/10 and the tailnet ULA reach the daemon."""
        whois = _patch_whois(monkeypatch, _whois_json())
        peer = await resolve_forwarded_peer(_req("127.0.0.1", {"X-Forwarded-For": addr}), TRUST)
        assert peer is None
        whois.assert_not_called()

    @pytest.mark.asyncio
    async def test_tailnet_ula_ipv6_address_is_accepted(self, monkeypatch) -> None:
        _patch_whois(monkeypatch, _whois_json())
        peer = await resolve_forwarded_peer(
            _req("::1", {"X-Forwarded-For": "fd7a:115c:a1e0::1"}), TRUST
        )
        assert peer is not None
        assert peer.address == "fd7a:115c:a1e0::1"

    @pytest.mark.asyncio
    async def test_header_whois_login_disagreement_is_a_rejection(self, monkeypatch) -> None:
        """(f) the daemon decides identity; a disagreeing header is a rejection."""
        _patch_whois(monkeypatch, _whois_json(login="you@example.com"))
        peer = await resolve_forwarded_peer(
            _req(
                "127.0.0.1",
                {
                    "X-Forwarded-For": "100.64.0.5",
                    "Tailscale-User-Login": "someone-else@example.com",
                },
            ),
            TRUST,
        )
        assert peer is None

    @pytest.mark.asyncio
    async def test_absent_login_header_costs_nothing(self, monkeypatch) -> None:
        """The header is only corroboration — absence is not a failure."""
        _patch_whois(monkeypatch, _whois_json())
        peer = await resolve_forwarded_peer(
            _req("127.0.0.1", {"X-Forwarded-For": "100.64.0.5"}), TRUST
        )
        assert peer is not None

    @pytest.mark.asyncio
    async def test_agreeing_login_header_passes(self, monkeypatch) -> None:
        _patch_whois(monkeypatch, _whois_json())
        peer = await resolve_forwarded_peer(
            _req(
                "127.0.0.1",
                {
                    "X-Forwarded-For": "100.64.0.5",
                    "Tailscale-User-Login": "You@Example.com",  # case-insensitive
                },
            ),
            TRUST,
        )
        assert peer is not None

    @pytest.mark.asyncio
    async def test_windows_degrades_to_none(self, monkeypatch) -> None:
        """RFC OQ4: resolution is POSIX-only; Windows falls to the token path."""
        whois = _patch_whois(monkeypatch, _whois_json())
        monkeypatch.setattr(tailnet, "IS_POSIX", False)
        peer = await resolve_forwarded_peer(
            _req("127.0.0.1", {"X-Forwarded-For": "100.64.0.5"}), TRUST
        )
        assert peer is None
        whois.assert_not_called()


class TestDaemonFailureModes:
    """(e) fail-closed on identity, fail-open on availability — every daemon
    failure is ``None``, exercised at the subprocess seam."""

    @pytest.fixture(autouse=True)
    def _fake_cli(self, monkeypatch):
        monkeypatch.setattr(tailnet, "_cli_path", lambda: "/usr/bin/tailscale")

    @pytest.mark.asyncio
    async def test_daemon_absent_is_none(self, monkeypatch) -> None:
        monkeypatch.setattr(tailnet, "_cli_path", lambda: None)
        assert (
            await resolve_forwarded_peer(
                _req("127.0.0.1", {"X-Forwarded-For": "100.64.0.5"}), TRUST
            )
            is None
        )

    @pytest.mark.asyncio
    async def test_non_zero_exit_is_none(self, monkeypatch) -> None:
        proc = SimpleNamespace(returncode=1, stdout="", stderr="no such peer")
        monkeypatch.setattr(tailnet.subprocess, "run", lambda *a, **k: proc)
        assert (
            await resolve_forwarded_peer(
                _req("127.0.0.1", {"X-Forwarded-For": "100.64.0.5"}), TRUST
            )
            is None
        )

    @pytest.mark.asyncio
    async def test_malformed_json_is_none(self, monkeypatch) -> None:
        proc = SimpleNamespace(returncode=0, stdout="not json{", stderr="")
        monkeypatch.setattr(tailnet.subprocess, "run", lambda *a, **k: proc)
        assert (
            await resolve_forwarded_peer(
                _req("127.0.0.1", {"X-Forwarded-For": "100.64.0.5"}), TRUST
            )
            is None
        )

    @pytest.mark.asyncio
    async def test_timeout_is_none(self, monkeypatch) -> None:
        def _boom(*a: Any, **k: Any) -> Any:
            raise subprocess.TimeoutExpired(cmd="tailscale", timeout=3.0)

        monkeypatch.setattr(tailnet.subprocess, "run", _boom)
        assert (
            await resolve_forwarded_peer(
                _req("127.0.0.1", {"X-Forwarded-For": "100.64.0.5"}), TRUST
            )
            is None
        )

    @pytest.mark.asyncio
    async def test_whois_missing_identity_fields_is_none(self, monkeypatch) -> None:
        proc = SimpleNamespace(returncode=0, stdout=json.dumps({"Node": {}}), stderr="")
        monkeypatch.setattr(tailnet.subprocess, "run", lambda *a, **k: proc)
        assert (
            await resolve_forwarded_peer(
                _req("127.0.0.1", {"X-Forwarded-For": "100.64.0.5"}), TRUST
            )
            is None
        )


class TestWhoisCache:
    def test_results_are_cached_by_address(self, monkeypatch) -> None:
        whois = _patch_whois(monkeypatch, _whois_json())
        assert tailnet._whois_cached("100.64.0.5") == tailnet._whois_cached("100.64.0.5")
        assert whois.call_count == 1

    def test_negative_results_are_cached_too(self, monkeypatch) -> None:
        whois = _patch_whois(monkeypatch, None)
        assert tailnet._whois_cached("100.64.0.9") is None
        assert tailnet._whois_cached("100.64.0.9") is None
        assert whois.call_count == 1

    def test_entry_count_is_bounded(self, monkeypatch) -> None:
        _patch_whois(monkeypatch, None)
        for i in range(tailnet._WHOIS_CACHE_MAX_ENTRIES + 50):
            tailnet._whois_cached(f"100.64.{i // 256}.{i % 256}")
        assert len(tailnet._whois_cache) <= tailnet._WHOIS_CACHE_MAX_ENTRIES


# ── RFC §3.1 pin keys ──


class TestPeerPinKey:
    PEER = ForwardedPeer(login="you@example.com", node="phone.tail.ts.net", address="100.64.0.5")

    def test_node_scope_key_shape(self) -> None:
        assert (
            peer_pin_key(self.PEER, PIN_SCOPE_NODE) == "ts:node:you@example.com|phone.tail.ts.net"
        )

    def test_login_scope_key_shape(self) -> None:
        assert peer_pin_key(self.PEER, PIN_SCOPE_LOGIN) == "ts:login:you@example.com"

    def test_unrecognised_scope_narrows_to_node(self) -> None:
        assert peer_pin_key(self.PEER, "everyone") == "ts:node:you@example.com|phone.tail.ts.net"

    def test_tagged_node_is_forced_to_node_scope_and_logged(self, caplog) -> None:
        """An ACL tag replaces the user identity, so login scope would collapse
        the pin across the entire tagged fleet (tailscale/tailscale#4605)."""
        tagged = ForwardedPeer(
            login=TAGGED_DEVICES_LOGIN, node="ci.tail.ts.net", address="100.64.0.6"
        )
        with caplog.at_level("WARNING", logger="kiro_crew.dashboard.tailnet"):
            key = peer_pin_key(tagged, PIN_SCOPE_LOGIN)
        assert key == f"ts:node:{TAGGED_DEVICES_LOGIN}|ci.tail.ts.net"
        assert any("overridden" in r.message for r in caplog.records)

    def test_two_tagged_nodes_get_distinct_keys_even_under_login_scope(self) -> None:
        """The 'tagged-devices in allowed_logins' hazard: node identity stays
        unique, so one tagged node's session is not replayable from another."""
        a = ForwardedPeer(login=TAGGED_DEVICES_LOGIN, node="ci-a.ts.net", address="100.64.0.6")
        b = ForwardedPeer(login=TAGGED_DEVICES_LOGIN, node="ci-b.ts.net", address="100.64.0.7")
        assert peer_pin_key(a, PIN_SCOPE_LOGIN) != peer_pin_key(b, PIN_SCOPE_LOGIN)


class TestLoginAllowlist:
    def test_membership_is_case_insensitive(self) -> None:
        assert login_allowed("You@Example.com", ("you@example.com",)) is True

    def test_non_member_is_denied(self) -> None:
        assert login_allowed("mallory@example.com", ("you@example.com",)) is False

    def test_empty_allowlist_allows_no_one(self) -> None:
        assert login_allowed("you@example.com", ()) is False

    def test_blank_entries_never_match(self) -> None:
        assert login_allowed("", ("", " ")) is False


# ── Config load validation (RFC §3/§3.1) ──


class TestConfigLoadValidation:
    def test_trust_with_empty_allowlist_is_refused_at_load(self, caplog) -> None:
        with caplog.at_level("ERROR", logger="kiro_crew.config.loader"):
            cfg = _tailscale_config_from({"enabled": True, "trust_identity": True})
        assert cfg.trust_identity is False
        assert any("allowed_logins" in r.message for r in caplog.records)

    def test_unrecognised_pin_scope_narrows_to_node_with_warning(self, caplog) -> None:
        with caplog.at_level("WARNING", logger="kiro_crew.config.loader"):
            cfg = _tailscale_config_from(
                {"trust_identity": True, "allowed_logins": ["a@b.c"], "pin_scope": "everyone"}
            )
        assert cfg.pin_scope == "node"
        assert cfg.trust_identity is True
        assert any("pin_scope" in r.message for r in caplog.records)

    def test_valid_config_passes_through(self) -> None:
        cfg = _tailscale_config_from(
            {
                "enabled": True,
                "trust_identity": True,
                "allowed_logins": ["you@example.com", "  ", 42],
                "pin_scope": "login",
            }
        )
        assert cfg.enabled is True
        assert cfg.trust_identity is True
        assert cfg.allowed_logins == ["you@example.com"]  # blanks/non-strings dropped
        assert cfg.pin_scope == "login"

    def test_defaults_are_all_off(self) -> None:
        cfg = _tailscale_config_from(None)
        assert cfg.enabled is False
        assert cfg.trust_identity is False
        assert cfg.allowed_logins == []
        assert cfg.pin_scope == "node"

    def test_non_list_allowed_logins_is_treated_as_empty(self, caplog) -> None:
        with caplog.at_level("ERROR", logger="kiro_crew.config.loader"):
            cfg = _tailscale_config_from({"trust_identity": True, "allowed_logins": "a@b.c"})
        assert cfg.trust_identity is False


# ── RFC §5: what stays closed ──


class TestVerifiedPeerStaysReadOnly:
    def test_verified_peer_request_is_not_direct_local(self) -> None:
        """A verified tailnet peer still sends X-Forwarded-For, so the
        config-write / secret-reveal predicate stays False for it."""
        from kiro_crew.dashboard.origin import is_direct_local_request

        req = SimpleNamespace(
            remote="127.0.0.1",
            headers={"X-Forwarded-For": "100.64.0.5", "Tailscale-User-Login": "you@example.com"},
        )
        assert is_direct_local_request(req) is False

    def test_messaging_config_surface_reports_read_only_for_verified_peer(
        self, monkeypatch, tmp_path
    ) -> None:
        """RFC §5 regression: a whois-verified tailnet peer receives
        ``read_only: true`` on the handlers/messaging.py config surfaces —
        identity trust must not unlock the config-write surfaces."""
        import kiro_crew.dashboard.handlers.messaging as messaging

        monkeypatch.setattr(loader, "env_path", lambda: tmp_path / ".env")
        monkeypatch.setattr(loader, "config_path", lambda: tmp_path / "config.json")

        class _PeerReq:
            remote = "127.0.0.1"
            headers = {
                "X-Forwarded-For": "100.64.0.5",
                "Tailscale-User-Login": "you@example.com",
            }

            def __init__(self, state: Any) -> None:
                self.app = {"state": state}

        state = MagicMock()
        state.teams_connected = False
        state.teams_connect_error = ""
        import asyncio

        resp = asyncio.run(messaging.api_teams_config_get(_PeerReq(state)))
        body = resp.body
        assert isinstance(body, (bytes, bytearray))
        assert json.loads(body)["read_only"] is True


# ── Review-round hardening (adversarial fleet findings) ──


class TestTransientFailureCaching:
    def test_timeout_is_cached_on_the_short_transient_ttl(self, monkeypatch) -> None:
        """A daemon-still-starting blip (timeout/spawn error) must not hold an
        identity-pinned session denied for the full cache window."""
        monkeypatch.setattr(tailnet, "_cli_path", lambda: "/usr/bin/tailscale")

        def _boom(*a: Any, **k: Any) -> Any:
            raise subprocess.TimeoutExpired(cmd="tailscale", timeout=3.0)

        monkeypatch.setattr(tailnet.subprocess, "run", _boom)
        assert tailnet._whois_cached("100.64.0.5") is None
        expiry, value = tailnet._whois_cache["100.64.0.5"]
        assert value is None
        import time as _time

        assert expiry - _time.monotonic() <= tailnet._WHOIS_TRANSIENT_TTL_SECS + 0.5

    def test_definitive_negative_keeps_the_full_ttl(self, monkeypatch) -> None:
        """The daemon answered 'no such peer' — that is worth the full TTL."""
        monkeypatch.setattr(tailnet, "_cli_path", lambda: "/usr/bin/tailscale")
        proc = SimpleNamespace(returncode=1, stdout="", stderr="no such peer")
        monkeypatch.setattr(tailnet.subprocess, "run", lambda *a, **k: proc)
        assert tailnet._whois_cached("100.64.0.6") is None
        expiry, _value = tailnet._whois_cache["100.64.0.6"]
        import time as _time

        assert expiry - _time.monotonic() > tailnet._WHOIS_TRANSIENT_TTL_SECS + 1


class TestIdentityCharset:
    """The pin-key namespace stays unambiguous because ``_IDENTITY_RE`` forbids
    the ``|`` separator and the ``:`` delimiter inside either component."""

    @pytest.mark.parametrize(
        "good", ["you@example.com", "tagged-devices", "phone.tail.ts.net", "User_1%2B"]
    )
    def test_accepts_plain_identity_tokens(self, good: str) -> None:
        assert tailnet._valid_identity(good) == good

    @pytest.mark.parametrize(
        "bad",
        ["a|b", "a:b", "a b", "a\tb", "", "  ", "a\x00b", "x" * 254, 42, None],
    )
    def test_rejects_separator_delimiter_and_junk(self, bad: object) -> None:
        assert tailnet._valid_identity(bad) is None

    def test_pin_keys_cannot_collide_across_login_node_split(self) -> None:
        """login='a@b', node='c' vs login='a', node='b@c' — the RFC's bare key
        shape collides on these; the '|' separator cannot."""
        a = ForwardedPeer(login="a@b", node="c", address="100.64.0.1")
        b = ForwardedPeer(login="a", node="b@c", address="100.64.0.2")
        assert peer_pin_key(a, PIN_SCOPE_NODE) != peer_pin_key(b, PIN_SCOPE_NODE)


class TestRefreshRotationRebind:
    """A rotated access token must not launder the identity pin (the refresh
    endpoint is bypassed by the middleware, so it re-binds explicitly)."""

    @pytest.mark.asyncio
    async def test_rotation_rebinds_when_a_peer_resolves(self, monkeypatch) -> None:
        from kiro_crew.dashboard import token_auth as _ta
        from kiro_crew.dashboard.handlers.auth_refresh import _rebind_rotated_token_to_peer

        _ta._state.clear_all()
        _patch_whois(monkeypatch, _whois_json())
        req = SimpleNamespace(
            remote="127.0.0.1",
            headers=CIMultiDict({"X-Forwarded-For": "100.64.0.5"}),
            app={"tailnet_trust": TRUST},
        )
        await _rebind_rotated_token_to_peer(req, "new-token", 9999999999.0)
        key, _exp, proxied = _ta._state._peer_bindings["new-token"]
        assert key == "ts:node:you@example.com|phone.tail.ts.net"
        assert proxied is False
        _ta._state.clear_all()

    @pytest.mark.asyncio
    async def test_rotation_stays_unbound_without_a_peer(self, monkeypatch) -> None:
        """No peer (trust off / daemon down / non-tailnet): byte-for-byte the
        pre-identity refresh behaviour — the token stays unbound."""
        from kiro_crew.dashboard import token_auth as _ta
        from kiro_crew.dashboard.handlers.auth_refresh import _rebind_rotated_token_to_peer

        _ta._state.clear_all()
        _patch_whois(monkeypatch, None)
        req = SimpleNamespace(
            remote="127.0.0.1",
            headers=CIMultiDict({"X-Forwarded-For": "100.64.0.5"}),
            app={"tailnet_trust": TRUST},
        )
        await _rebind_rotated_token_to_peer(req, "new-token", 9999999999.0)
        assert "new-token" not in _ta._state._peer_bindings
        req2 = SimpleNamespace(remote="127.0.0.1", headers=CIMultiDict(), app={})
        await _rebind_rotated_token_to_peer(req2, "other-token", 9999999999.0)
        assert "other-token" not in _ta._state._peer_bindings


class TestCliPathTrust:
    """A candidate the gateway user can write is refused — planted-binary
    defence for Homebrew-style user-owned prefixes on the request-triggered
    whois path."""

    def test_user_writable_binary_is_refused(self, monkeypatch, tmp_path) -> None:
        import os

        fake = tmp_path / "tailscale"
        fake.write_text("#!/bin/sh\n")
        fake.chmod(0o755)  # user-writable by construction (tmp_path is ours)
        monkeypatch.setattr(tailnet, "_CLI_CANDIDATE_PATHS", (str(fake),))
        monkeypatch.setattr(tailnet, "IS_POSIX", True)
        if getattr(os, "geteuid", lambda: 1)() == 0:
            pytest.skip("writability gate is bypassed for root")
        assert tailnet._cli_path() is None

    def test_group_writable_binary_is_refused_even_for_root(self, monkeypatch, tmp_path) -> None:
        """The mode check does not depend on who runs the gateway."""
        fake = tmp_path / "tailscale"
        fake.write_text("#!/bin/sh\n")
        fake.chmod(0o775)  # group-writable
        assert tailnet._posix_candidate_trusted(str(fake)) is False

    def test_missing_binary_is_none(self, monkeypatch, tmp_path) -> None:
        monkeypatch.setattr(tailnet, "_CLI_CANDIDATE_PATHS", (str(tmp_path / "absent"),))
        assert tailnet._cli_path() is None
