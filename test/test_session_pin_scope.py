"""Session-pin truthfulness: the pin's effective scope must be reported, not asserted.

Two halves, and the seam between them is the point:

* ``is_proxied_request`` must report BOTH proxy shapes — a same-host tunnel
  (loopback peer) and an upstream proxy on another host (non-loopback peer) —
  because both make ``request.remote`` a shared address. Peer locality is not
  part of the test; that is what distinguishes it from
  ``is_direct_local_request``.
* The Security Posture ``token_auth`` row must render three distinct states,
  never render "nothing pinned" as if the control were effective — the same
  failure mode as a check that never ran being drawn as a check that passed —
  and must be derived from the LIVE bindings so a single tunnelled login does
  not pin the row to SHARED for the rest of the gateway's life.
"""

from __future__ import annotations

import time
from types import SimpleNamespace

import pytest

from kiro_crew.dashboard.origin import is_direct_local_request, is_proxied_request
from kiro_crew.security_posture import build_posture_snapshot


def _req(remote: str, headers: dict[str, str] | None = None):
    """Minimal request double: both predicates read only .remote and .headers."""
    return SimpleNamespace(remote=remote, headers=headers or {})


class TestProxiedPredicate:
    def test_plain_loopback_is_not_proxied(self) -> None:
        assert is_proxied_request(_req("127.0.0.1")) is False

    def test_plain_non_loopback_client_is_not_proxied(self) -> None:
        """A widened bind with a direct client sends no forwarding header."""
        assert is_proxied_request(_req("203.0.113.7")) is False

    @pytest.mark.parametrize(
        "header",
        ["Forwarded", "X-Forwarded-For", "X-Forwarded-Host", "X-Forwarded-Proto", "X-Real-IP"],
    )
    def test_same_host_proxy_is_proxied(self, header: str) -> None:
        assert is_proxied_request(_req("127.0.0.1", {header: "203.0.113.7"})) is True

    def test_ipv6_loopback_counts(self) -> None:
        assert is_proxied_request(_req("::1", {"X-Forwarded-For": "203.0.113.7"})) is True

    def test_UPSTREAM_proxy_on_another_host_is_also_proxied(self) -> None:
        """The case an earlier, loopback-scoped version of this got wrong.

        An nginx box / container bridge / LAN jump host in front of a widened
        ``KIROCREW_BIND`` presents a NON-loopback peer — but that peer is the
        proxy, not the client, so the address is just as shared. Reporting it as
        per-client would reproduce the untrue claim this predicate removes.
        """
        req = _req("10.0.0.5", {"X-Forwarded-For": "203.0.113.7"})
        assert is_proxied_request(req) is True
        # Distinct from is_direct_local_request, which stays loopback-scoped
        # because it answers "is this the local machine".
        assert is_direct_local_request(req) is False

    def test_empty_remote_without_headers_is_not_proxied(self) -> None:
        assert is_proxied_request(_req("")) is False


def _pin_detail() -> str:
    control = next(c for c in build_posture_snapshot()["controls"] if c["key"] == "token_auth")
    return next(i["detail"] for i in control["items"] if i["label"] == "IP pinning")


@pytest.fixture(autouse=True)
def _reset_pin_bindings():
    """Isolate the binding table — it is process-global by design."""
    from kiro_crew.dashboard import token_auth

    state = token_auth._state
    before = dict(state._ip_bindings)
    state._ip_bindings.clear()
    yield
    state._ip_bindings.clear()
    state._ip_bindings.update(before)


def _live() -> float:
    """An expiry far enough out that the binding counts as live."""
    return time.time() + 3600


class TestPostureReportsEffectivePinScope:
    def test_nothing_pinned_is_reported_as_not_known_yet_not_as_effective(self) -> None:
        """A pin nobody is exercising is not evidence the pin works."""
        from kiro_crew.dashboard.token_auth import proxied_pin_observed

        assert proxied_pin_observed() is None
        detail = _pin_detail()
        assert "not known yet" in detail
        # Must not claim the per-client property it has not observed.
        assert "client address that first used it" not in detail

    def test_direct_bind_reports_per_client(self) -> None:
        from kiro_crew.dashboard.token_auth import bind_token_ip, proxied_pin_observed

        bind_token_ip("t-direct", "203.0.113.7", _live(), False)
        assert proxied_pin_observed() is False
        assert "client address that first used it" in _pin_detail()

    def test_proxied_bind_reports_shared_pin(self) -> None:
        """The state the guide used to advertise as a mitigation."""
        from kiro_crew.dashboard.token_auth import bind_token_ip, proxied_pin_observed

        bind_token_ip("t-proxied", "127.0.0.1", _live(), True)
        assert proxied_pin_observed() is True
        detail = _pin_detail()
        assert "SHARED, not per-client" in detail
        assert "proxy's address" in detail

    def test_one_live_proxied_binding_dominates(self) -> None:
        """SHARED while ANY live session is proxied — the dangerous state wins."""
        from kiro_crew.dashboard.token_auth import bind_token_ip, proxied_pin_observed

        bind_token_ip("t-direct", "203.0.113.7", _live(), False)
        bind_token_ip("t-proxied", "127.0.0.1", _live(), True)
        assert proxied_pin_observed() is True

    def test_report_recovers_when_the_proxied_session_expires(self) -> None:
        """The reason this is derived rather than latched.

        A single tunnelled login must not pin the posture row to SHARED for the
        rest of the gateway's life. Once that session is no longer live and only
        direct ones remain, the row has to say per-client again — a stale SHARED
        is the same class of untrue claim this row exists to remove.
        """
        from kiro_crew.dashboard.token_auth import bind_token_ip, proxied_pin_observed

        bind_token_ip("t-proxied", "127.0.0.1", time.time() - 1, True)  # already expired
        bind_token_ip("t-direct", "203.0.113.7", _live(), False)
        assert proxied_pin_observed() is False
        assert "client address that first used it" in _pin_detail()

    def test_all_sessions_expired_reports_not_known_rather_than_stale(self) -> None:
        from kiro_crew.dashboard.token_auth import bind_token_ip, proxied_pin_observed

        bind_token_ip("t-proxied", "127.0.0.1", time.time() - 1, True)
        assert proxied_pin_observed() is None

    def test_answer_does_not_depend_on_when_eviction_ran(self) -> None:
        """Expiry is filtered directly, so lazy eviction cannot change the report."""
        from kiro_crew.dashboard import token_auth

        token_auth.bind_token_ip("t-proxied", "127.0.0.1", time.time() - 1, True)
        before_evict = token_auth.proxied_pin_observed()
        token_auth._state.evict_expired(time.time())
        assert token_auth.proxied_pin_observed() == before_evict is None

    def test_observation_never_changes_the_binding_itself(self) -> None:
        """The flag is reporting only — check_ip must be unaffected by it."""
        from kiro_crew.dashboard.token_auth import bind_token_ip, check_token_ip

        bind_token_ip("t-proxied", "127.0.0.1", _live(), True)
        assert check_token_ip("t-proxied", "127.0.0.1") is True
        assert check_token_ip("t-proxied", "203.0.113.7") is False
