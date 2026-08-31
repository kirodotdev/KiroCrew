"""Tailnet mobile access: the guided sequence, and the refusals that keep it safe.

The sibling suites already pin the layers underneath — ``test_tailnet_origin.py``
that the READ path swallows everything, ``test_tailnet_serve.py`` that the WRITE
path reports the daemon verbatim. What this suite pins is the thing built on top
of them: *which single step is offered next*, and the two places where offering
the wrong thing would be destructive rather than merely unhelpful.

* :class:`TestProbeDistinguishesCauses` pins that the four ways there is no
  tailnet name stay APART. ``self_dns_name`` deliberately collapses them all to
  ``None``, which is right for its caller and useless for an onboarding UI: a
  user who has not installed Tailscale and a user whose MagicDNS is off need
  different errands, and rendering both as "Tailscale not working" is the whole
  defect this feature exists to remove.
* :class:`TestStepPrecedence` pins the ORDER. Precedence is the entire design —
  each earlier cause blocks every later one, so a host that is signed out must
  not be told to restart the gateway.
* :class:`TestUndeterminedIsNotFree` pins the load-bearing asymmetry:
  ``published=None`` means "could not tell", and publishing REPLACES whatever
  holds the mount. So an undeterminable state must land on ``occupied`` (refuse,
  print the manual command), never on ``publish``. Rendering "could not tell" as
  "free" is how an operator's own serve mapping gets silently overwritten.
* :class:`TestRestartIsNotReady` pins the boot race the logs already knew
  about and the UI never showed: a name resolvable NOW, absent from the running
  allowlist, is genuinely not trusted. It must be activated before it is ready.
* :class:`TestQrRefusals` pins that a credential is never minted for a URL
  nothing answers, and that the TTL cannot be talked upward past either ceiling.
* :class:`TestRestrictedSessionRefused` pins that an app-scoped session cannot
  publish this dashboard to a whole tailnet or mint itself a dashboard token —
  that would be an escalation straight out of the app sandbox.
* :class:`TestKeepAwakeProbe` pins that the sleep decision fails toward LETTING
  THE HOST SLEEP. An unresolvable probe must never pin a laptop awake, and the
  probe must be cached, because the poll it feeds runs every 15 seconds.
"""

from __future__ import annotations

import asyncio
import json
import time
from contextlib import contextmanager
from types import SimpleNamespace
from typing import get_args
from unittest.mock import patch

import pytest

from kiro_crew.dashboard import tailnet
from kiro_crew.dashboard.handlers import tailnet_mobile
from kiro_crew.dashboard.tailnet import DaemonProbe
from kiro_crew.dashboard.token_auth import (
    LINK_WINDOW_SECS,
    MAX_SESSION_TTL_SECS,
    _b64url_encode,
    generate_token,
)

_PORT = 5476
_HOST = "desk.tail-abc.ts.net"


def _owner_session_token(
    *, ttl_seconds: int = MAX_SESSION_TTL_SECS, peer_key: str = "", **extra: str
) -> str:
    """A real caller credential, as the auth middleware would have validated it."""
    return generate_token(
        _OWNER,
        ttl_seconds=ttl_seconds,
        peer_key=peer_key,
        extra=extra or None,
    )


def _probe(
    *,
    name: str = _HOST,
    login: str = "owner@example.com",
    installed: bool = True,
    reachable: bool = True,
    logged_in: bool = True,
    https_enabled: bool | None = True,
    detail: str = "",
    stopped: bool = False,
) -> DaemonProbe:
    return DaemonProbe(
        name=name,
        installed=installed,
        reachable=reachable,
        logged_in=logged_in,
        detail=detail,
        login=login,
        stopped=stopped,
        https_enabled=https_enabled,
    )


def _step(**kw) -> str:
    """``_derive_step`` with the all-clear as the default, so each test names only
    the one condition it is about."""
    args = {
        "pinned": False,
        "probe": _probe(),
        "trusted": True,
        "startup_host": _HOST,
        "published": True,
    }
    args.update(kw)
    return tailnet_mobile._derive_step(**args)  # type: ignore[arg-type]


class TestProbeDistinguishesCauses:
    """Four different remedies must not collapse into one message."""

    def test_no_cli_reports_not_installed(self) -> None:
        with patch.object(tailnet, "_cli_path", return_value=None):
            p = tailnet.probe_daemon()
        assert (p.installed, p.reachable, p.logged_in, p.name) == (False, False, False, "")

    def test_daemon_silent_reports_installed_but_unreachable(self) -> None:
        with (
            patch.object(tailnet, "_cli_path", return_value="/usr/bin/tailscale"),
            patch.object(tailnet, "_run_json_detail", return_value=(None, True)),
        ):
            p = tailnet.probe_daemon()
        assert p.installed is True
        assert p.reachable is False
        assert p.logged_in is False

    @pytest.mark.parametrize("state", ["NeedsLogin", "NoState", "NeedsMachineAuth"])
    def test_signed_out_backend_states_report_logged_out(self, state: str) -> None:
        with (
            patch.object(tailnet, "_cli_path", return_value="/usr/bin/tailscale"),
            patch.object(
                tailnet, "_run_json_detail", return_value=({"BackendState": state}, False)
            ),
        ):
            p = tailnet.probe_daemon()
        assert p.reachable is True
        assert p.logged_in is False
        assert p.name == ""

    def test_stopped_backend_state_is_its_own_not_usable_state(self) -> None:
        """``BackendState "Stopped"`` must not read as healthy (issue #7244).

        A stopped daemon answers status reads and is not in the needing-login
        set, so before this field it passed every probe check and the flow
        reported a ready tailnet whose URL nothing could reach. The remedy
        (start Tailscale) differs from signing in, so it is a distinct signal,
        not a ``logged_in`` overload.
        """
        with (
            patch.object(tailnet, "_cli_path", return_value="/usr/bin/tailscale"),
            patch.object(
                tailnet, "_run_json_detail", return_value=({"BackendState": "Stopped"}, False)
            ),
        ):
            p = tailnet.probe_daemon()
        assert p.stopped is True
        assert p.reachable is True  # the daemon DID answer; only Tailscale is down
        assert p.name == ""
        assert "stopped" in p.detail.lower()

    def test_running_backend_state_reports_not_stopped(self) -> None:
        with (
            patch.object(tailnet, "_cli_path", return_value="/usr/bin/tailscale"),
            patch.object(
                tailnet, "_run_json_detail", return_value=({"BackendState": "Running"}, False)
            ),
            patch.object(tailnet, "self_dns_name", return_value=_HOST),
        ):
            p = tailnet.probe_daemon()
        assert p.stopped is False

    def test_signed_in_without_name_reports_magicdns_gap(self) -> None:
        """Reachable + logged in + no name is the MagicDNS-off case, and it must be
        distinguishable from being signed out — the remedy is a different console."""
        with (
            patch.object(tailnet, "_cli_path", return_value="/usr/bin/tailscale"),
            patch.object(
                tailnet, "_run_json_detail", return_value=({"BackendState": "Running"}, False)
            ),
            patch.object(tailnet, "self_dns_name", return_value=None),
        ):
            p = tailnet.probe_daemon()
        assert (p.installed, p.reachable, p.logged_in) == (True, True, True)
        assert p.name == ""
        assert "MagicDNS" in p.detail

    def test_fully_ready_reports_the_name_and_no_detail(self) -> None:
        with (
            patch.object(tailnet, "_cli_path", return_value="/usr/bin/tailscale"),
            patch.object(
                tailnet, "_run_json_detail", return_value=({"BackendState": "Running"}, False)
            ),
            patch.object(tailnet, "self_dns_name", return_value=_HOST),
        ):
            p = tailnet.probe_daemon()
        assert p.name == _HOST
        assert p.detail == ""

    def test_ready_probe_derives_the_local_tailscale_login(self) -> None:
        status = {
            "BackendState": "Running",
            "Self": {"UserID": 42},
            "User": {"42": {"LoginName": "owner@example.com"}},
        }
        with (
            patch.object(tailnet, "_cli_path", return_value="/usr/bin/tailscale"),
            patch.object(tailnet, "_run_json_detail", return_value=(status, False)),
            patch.object(tailnet, "self_dns_name", return_value=_HOST),
        ):
            p = tailnet.probe_daemon()
        assert p.login == "owner@example.com"

    @pytest.mark.parametrize(
        "status",
        [
            {"BackendState": "Running"},
            {"BackendState": "Running", "Self": {"UserID": 42}, "User": []},
            {
                "BackendState": "Running",
                "Self": {"UserID": 42},
                "User": {"42": {"LoginName": "not an identity"}},
            },
        ],
    )
    def test_missing_or_malformed_local_login_is_never_guessed(self, status: dict) -> None:
        with (
            patch.object(tailnet, "_cli_path", return_value="/usr/bin/tailscale"),
            patch.object(tailnet, "_run_json_detail", return_value=(status, False)),
            patch.object(tailnet, "self_dns_name", return_value=_HOST),
        ):
            p = tailnet.probe_daemon()
        assert p.login == ""

    def test_matching_cert_domain_reports_https_enabled(self) -> None:
        with (
            patch.object(tailnet, "_cli_path", return_value="/usr/bin/tailscale"),
            patch.object(
                tailnet,
                "_run_json_detail",
                return_value=(
                    {"BackendState": "Running", "CertDomains": [_HOST]},
                    False,
                ),
            ),
            patch.object(tailnet, "self_dns_name", return_value=_HOST),
        ):
            p = tailnet.probe_daemon()
        assert p.https_enabled is True

    def test_explicit_empty_cert_domains_reports_https_disabled(self) -> None:
        with (
            patch.object(tailnet, "_cli_path", return_value="/usr/bin/tailscale"),
            patch.object(
                tailnet,
                "_run_json_detail",
                return_value=({"BackendState": "Running", "CertDomains": []}, False),
            ),
            patch.object(tailnet, "self_dns_name", return_value=_HOST),
        ):
            p = tailnet.probe_daemon()
        assert p.https_enabled is False

    @pytest.mark.parametrize("cert_domains", [None, {}, "unexpected"])
    def test_missing_or_malformed_cert_domains_stays_unknown(self, cert_domains: object) -> None:
        status = {"BackendState": "Running"}
        if cert_domains is not None:
            status["CertDomains"] = cert_domains
        with (
            patch.object(tailnet, "_cli_path", return_value="/usr/bin/tailscale"),
            patch.object(tailnet, "_run_json_detail", return_value=(status, False)),
            patch.object(tailnet, "self_dns_name", return_value=_HOST),
        ):
            p = tailnet.probe_daemon()
        assert p.https_enabled is None


class TestPeerCounting:
    """Whether there is a phone to reach this dashboard FROM.

    No amount of local state answers this, and it is the most likely way a new
    operator gets stuck: publishing succeeds and the QR renders perfectly on a
    tailnet of one, then the scan fails in the phone's browser with nothing on
    this machine to blame.
    """

    @staticmethod
    def _probe_with(status: dict) -> tailnet.DaemonProbe:
        with (
            patch.object(tailnet, "_cli_path", return_value="/usr/bin/tailscale"),
            patch.object(tailnet, "_run_json_detail", return_value=(status, False)),
            patch.object(tailnet, "self_dns_name", return_value=_HOST),
        ):
            return tailnet.probe_daemon()

    def test_tailnet_of_one_reports_zero_peers(self) -> None:
        p = self._probe_with({"BackendState": "Running"})
        assert (p.peer_count, p.peers_online) == (0, 0)

    def test_counts_peers_and_how_many_are_online(self) -> None:
        p = self._probe_with(
            {
                "BackendState": "Running",
                "Peer": {
                    "a": {"HostName": "phone", "Online": True},
                    "b": {"HostName": "laptop", "Online": False},
                    "c": {"HostName": "tablet", "Online": True},
                },
            }
        )
        assert p.peer_count == 3
        assert p.peers_online == 2

    def test_peers_present_but_all_offline_is_distinguishable(self) -> None:
        """A different message from "no devices at all": the operator has already
        done the phone half, so telling them to install Tailscale would be wrong."""
        p = self._probe_with({"BackendState": "Running", "Peer": {"a": {"Online": False}}})
        assert p.peer_count == 1
        assert p.peers_online == 0

    @pytest.mark.parametrize("peer", [None, [], "nope", 42])
    def test_malformed_peer_map_counts_as_zero_and_never_raises(self, peer: object) -> None:
        p = self._probe_with({"BackendState": "Running", "Peer": peer})
        assert (p.peer_count, p.peers_online) == (0, 0)

    def test_non_dict_peer_entries_are_skipped(self) -> None:
        p = self._probe_with(
            {"BackendState": "Running", "Peer": {"a": {"Online": True}, "b": "junk"}}
        )
        assert p.peer_count == 1

    def test_online_is_counted_strictly_not_truthily(self) -> None:
        """``Online`` absent or a non-bool must not read as online — an optimistic
        count would suppress the very advisory this exists to show."""
        p = self._probe_with(
            {
                "BackendState": "Running",
                "Peer": {"a": {}, "b": {"Online": "yes"}, "c": {"Online": 1}},
            }
        )
        assert p.peer_count == 3
        assert p.peers_online == 0

    def test_signed_out_probe_reports_no_peers(self) -> None:
        with (
            patch.object(tailnet, "_cli_path", return_value="/usr/bin/tailscale"),
            patch.object(
                tailnet,
                "_run_json_detail",
                return_value=(
                    {"BackendState": "NeedsLogin", "Peer": {"a": {"Online": True}}},
                    False,
                ),
            ),
        ):
            p = tailnet.probe_daemon()
        assert p.logged_in is False
        assert p.peer_count == 0, "a signed-out probe must not report a stale peer list"


class TestStepPrecedence:
    """Each earlier cause blocks every later one."""

    def test_pinned_outranks_everything_including_a_broken_daemon(self) -> None:
        assert _step(pinned=True, probe=_probe(installed=False), trusted=False) == "pinned"

    def test_install_outranks_the_config_switch(self) -> None:
        """A user without Tailscale must not be sent to a config toggle."""
        assert _step(probe=_probe(name="", installed=False), trusted=False) == "install"

    def test_unreachable_daemon_is_its_own_step(self) -> None:
        assert _step(probe=_probe(name="", reachable=False, logged_in=False)) == "start_daemon"

    def test_stopped_daemon_derives_start_daemon(self) -> None:
        assert _step(probe=_probe(name="", stopped=True)) == "start_daemon"

    def test_stopped_daemon_is_never_ready(self) -> None:
        """The issue-#7244 shape: serve status still returns the stale config
        with exit 0 while the daemon is stopped, so ``published=True`` arrives
        alongside a stopped probe — and must not derive ``ready`` (the card
        would show Active for a URL nothing can reach)."""
        assert _step(probe=_probe(stopped=True), published=True) == "start_daemon"

    def test_signed_out_is_its_own_step(self) -> None:
        assert _step(probe=_probe(name="", logged_in=False)) == "sign_in"

    def test_no_name_while_signed_in_asks_for_magicdns(self) -> None:
        assert _step(probe=_probe(name="")) == "enable_magicdns"

    def test_explicit_missing_cert_domain_requires_https_consent(self) -> None:
        assert _step(probe=_probe(https_enabled=False), published=False) == "enable_https"

    def test_unknown_cert_capability_defers_to_the_serve_write(self) -> None:
        assert _step(probe=_probe(https_enabled=None), published=False) == "publish"

    def test_existing_publication_is_stronger_than_a_stale_cert_snapshot(self) -> None:
        assert _step(probe=_probe(https_enabled=False), published=True) == "ready"

    def test_trust_off_precedes_publishing(self) -> None:
        """Publishing an untrusted origin yields a reachable dashboard that answers
        403 — the confusing state this feature removes, so config comes first."""
        assert _step(trusted=False, published=False) == "trust_off"

    def test_ready_only_when_everything_holds(self) -> None:
        assert _step() == "ready"

    def test_publish_offered_when_mount_is_provably_free(self) -> None:
        assert _step(published=False) == "publish"


class TestUndeterminedIsNotFree:
    """``published=None`` is "could not tell", and publishing overwrites."""

    def test_unknown_serve_state_refuses_rather_than_publishing(self) -> None:
        assert _step(published=None) == "occupied"

    def test_unknown_is_not_rendered_as_ready_either(self) -> None:
        assert _step(published=None) != "ready"


class TestRestartIsNotReady:
    """The boot race: resolvable now, absent from the startup allowlist."""

    def test_missing_startup_host_blocks_ready(self) -> None:
        assert _step(startup_host="") == "restart_gateway"

    def test_restart_step_beats_the_serve_state(self) -> None:
        """Even an already-published dashboard is not reachable if the running
        server has not put the name in its origin allowlist."""
        assert _step(startup_host="", published=True) == "restart_gateway"

    def test_a_changed_name_requires_a_restart(self) -> None:
        assert _step(startup_host="old.tail-abc.ts.net") == "restart_gateway"


class TestDurableMobileSetupConfig:
    """The explicit setup click upgrades ordinary installs atomically."""

    def test_enrolls_daemon_login_and_enables_restart_persistence(self) -> None:
        data = {
            "dashboard": {
                "tailscale": {
                    "enabled": True,
                    "allowed_logins": ["teammate@example.com"],
                }
            }
        }

        changed, restart, persistent = tailnet_mobile._apply_mobile_setup_config(
            data, "owner@example.com"
        )

        assert (changed, restart, persistent) == (True, True, True)
        dashboard = data["dashboard"]
        assert dashboard["qr_session_until_restart"] is True
        assert dashboard["qr_session_persist_across_restart"] is True
        assert dashboard["tailscale"] == {
            "enabled": True,
            "allowed_logins": ["teammate@example.com", "owner@example.com"],
            "trust_identity": True,
        }

    def test_second_setup_is_idempotent(self) -> None:
        data = {
            "dashboard": {
                "qr_session_until_restart": True,
                "qr_session_persist_across_restart": True,
                "tailscale": {
                    "enabled": True,
                    "trust_identity": True,
                    "allowed_logins": ["Owner@Example.com"],
                },
            }
        }
        assert tailnet_mobile._apply_mobile_setup_config(data, "owner@example.com") == (
            False,
            False,
            True,
        )

    def test_explicit_timed_session_opt_out_is_preserved(self) -> None:
        data = {
            "dashboard": {
                "qr_session_until_restart": False,
                "tailscale": {"enabled": True},
            }
        }
        changed, restart, persistent = tailnet_mobile._apply_mobile_setup_config(
            data, "owner@example.com"
        )
        assert (changed, restart, persistent) == (True, True, False)
        assert data["dashboard"]["qr_session_until_restart"] is False
        assert "qr_session_persist_across_restart" not in data["dashboard"]

    def test_malformed_allowlist_refuses_instead_of_overwriting_it(self) -> None:
        data = {"dashboard": {"tailscale": {"allowed_logins": "owner@example.com"}}}
        with pytest.raises(ValueError, match="allowed_logins"):
            tailnet_mobile._apply_mobile_setup_config(data, "owner@example.com")


_OWNER = "owner@example.com"


def _request(
    *,
    restricted: bool = False,
    port: int = _PORT,
    body: object = None,
    app_identity: str | None = "",
    user: str | None = _OWNER,
    tailnet_host: str = "",
    query_token: str = "",
    cookie_token: str = "",
    auth_token: str | None = None,
):
    """Minimal stand-in for the aiohttp request these handlers actually touch.

    ``app_identity`` models what the auth middleware writes into ``request["app"]``:
    ``""`` for a dashboard user (the default here), an app name for an app token,
    and ``None`` for the key being ABSENT, i.e. the middleware never ran. A real
    ``web.Request`` is a MutableMapping, which is why ``.get`` is implemented
    rather than only the ``.app`` attribute.

    ``user`` models the token subject the middleware resolved. It defaults to the
    configured owner so the success paths read normally; pass a different id to
    model a NON-owner dashboard user (a messaging-channel user who was handed a
    presigned dashboard link), and ``None`` for no resolved subject at all.
    ``tailnet_host`` models the name the RUNNING server trusted at startup. Empty
    (the default) means the fixed allowlist does not carry the resolvable name,
    so ``_derive_step`` reports ``restart_gateway``. Ready paths must set it.
    ``query_token`` / ``cookie_token`` model the raw credential material.
    ``auth_token`` models what the middleware published as the VALIDATED
    credential (``request["auth_token"]``): defaults to ``query_token or
    cookie_token`` (the normal middleware-prefer-query behaviour), but pass
    ``auth_token=cookie_token`` explicitly to model the fallback path where the
    query token was invalid and the middleware fell back to the cookie. Both
    ``query_token`` and ``cookie_token`` empty (the default) exercises the
    fail-closed no-readable-token path.
    """

    class _Req:
        def __init__(self) -> None:
            self.app = {
                "port": port,
                "state": SimpleNamespace(owner_id="owner@example.com"),
                "tailnet_host": tailnet_host,
                "tailnet_resolved_at": 1 if tailnet_host else 0,
            }
            self.remote = "127.0.0.1"
            self.headers: dict[str, str] = {}
            self.query: dict[str, str] = {"token": query_token} if query_token else {}
            self.cookies: dict[str, str] = (
                {f"mc_token_{port}": cookie_token} if cookie_token else {}
            )
            self._items: dict[str, object] = {}
            if app_identity is not None:
                self._items["app"] = app_identity
            if user is not None:
                self._items["user"] = user
            # Publish the validated credential, mirroring token_auth middleware.
            _auth = auth_token if auth_token is not None else (query_token or cookie_token)
            if _auth:
                self._items["auth_token"] = _auth

        def get(self, key: str, default: object = None) -> object:
            return self._items.get(key, default)

        def __contains__(self, key: str) -> bool:
            # A real web.Request is a MutableMapping, so authorization predicates
            # legitimately use `"app" in request` to tell an ABSENT key (middleware
            # never ran) from an empty one (a dashboard user). Without this the
            # stand-in raises TypeError and the gate under test never runs.
            return key in self._items

        def __getitem__(self, key: str) -> object:
            return self._items[key]

        async def json(self):
            if body is None:
                raise ValueError("no body")
            return body

    req = _Req()
    if restricted:
        req.app["state"] = SimpleNamespace(owner_id="owner@example.com", _restricted=True)
    return req


@pytest.fixture
def _unrestricted():
    with patch.object(tailnet_mobile, "_is_restricted_session", return_value=False) as m:
        yield m


@pytest.fixture
def _quiet_audit():
    with patch.object(tailnet_mobile, "_audit") as m:
        yield m


@contextmanager
def _machine(
    *,
    pinned: bool = False,
    name: str = _HOST,
    login: str = _OWNER,
    installed: bool = True,
    reachable: bool = True,
    logged_in: bool = True,
    https_enabled: bool | None = True,
    published: bool | None = True,
    trusted: bool = True,
    qr_session_until_restart: bool = True,
    qr_session_persist_across_restart: bool = False,
    trust_identity: bool = False,
    allowed_logins: tuple[str, ...] = (),
    detail: str = "",
):
    """Stub the four probes the REAL derivation reads, and let it run.

    Deliberately not a patch of ``_live_state`` or of the derived step: the
    property under test is that the QR mint consults ``_derive_step`` at all, so a
    test that injected a step would pass against the very bug this covers — an
    endpoint that never asks. Stubbing the inputs instead means each refusal below
    is produced by the same derivation the card renders from.
    """
    cfg = SimpleNamespace(
        dashboard=SimpleNamespace(
            tailscale=SimpleNamespace(
                enabled=trusted,
                keep_awake=True,
                trust_identity=trust_identity,
                allowed_logins=list(allowed_logins),
            ),
            qr_session_until_restart=qr_session_until_restart,
            qr_session_persist_across_restart=qr_session_persist_across_restart,
        )
    )
    probe = tailnet.DaemonProbe(
        name=name,
        installed=installed,
        reachable=reachable,
        logged_in=logged_in,
        detail=detail,
        login=login,
        https_enabled=https_enabled,
    )
    with (
        patch.object(tailnet_mobile.KiroCrewConfig, "load", classmethod(lambda cls: cfg)),
        patch.object(tailnet_mobile.tailnet, "is_governance_pinned_off", return_value=pinned),
        patch.object(tailnet_mobile.tailnet, "probe_daemon", return_value=probe),
        patch.object(
            tailnet_mobile.tailnet_serve,
            "serve_state",
            return_value=SimpleNamespace(published=published, configured=True, detail=detail),
        ),
    ):
        yield


class TestConfigureEndpoint:
    """One owner action lands the complete update-proof config shape."""

    @staticmethod
    def _effective_cfg(
        *,
        enabled: bool = True,
        trust_identity: bool = True,
        allowed_logins: list[str] | None = None,
        until_restart: bool = True,
        persistent: bool = True,
    ) -> SimpleNamespace:
        return SimpleNamespace(
            dashboard=SimpleNamespace(
                tailscale=SimpleNamespace(
                    enabled=enabled,
                    trust_identity=trust_identity,
                    allowed_logins=allowed_logins or ["owner@example.com"],
                ),
                qr_session_until_restart=until_restart,
                qr_session_persist_across_restart=persistent,
            )
        )

    @pytest.mark.asyncio
    async def test_writes_identity_and_persistence_in_one_locked_update(
        self, tmp_path, _unrestricted, _quiet_audit
    ) -> None:
        cfg_path = tmp_path / "config.json"
        cfg_path.write_text(
            json.dumps({"dashboard": {"tailscale": {"enabled": True}}}),
            encoding="utf-8",
        )
        with (
            patch.object(tailnet_mobile, "config_path", return_value=cfg_path),
            patch.object(tailnet_mobile.tailnet, "is_governance_pinned_off", return_value=False),
            patch.object(
                tailnet_mobile.tailnet,
                "probe_daemon",
                return_value=_probe(login="owner@example.com"),
            ),
            patch.object(
                tailnet_mobile.KiroCrewConfig,
                "load",
                classmethod(lambda cls: self._effective_cfg()),
            ),
        ):
            resp = await tailnet_mobile.api_tailnet_mobile_configure(_request())

        assert resp.status == 200
        payload = json.loads(resp.body)
        assert payload == {"restart_required": True}
        saved = json.loads(cfg_path.read_text(encoding="utf-8"))
        assert saved["dashboard"]["tailscale"]["trust_identity"] is True
        assert saved["dashboard"]["tailscale"]["allowed_logins"] == ["owner@example.com"]
        assert saved["dashboard"]["qr_session_until_restart"] is True
        assert saved["dashboard"]["qr_session_persist_across_restart"] is True

    @pytest.mark.asyncio
    async def test_effective_overlay_conflict_is_reported_after_base_write(
        self, tmp_path, _unrestricted, _quiet_audit
    ) -> None:
        """A higher-precedence local override cannot produce a false success."""
        cfg_path = tmp_path / "config.json"
        cfg_path.write_text("{}", encoding="utf-8")
        effective = self._effective_cfg(trust_identity=False, persistent=False)
        with (
            patch.object(tailnet_mobile, "config_path", return_value=cfg_path),
            patch.object(tailnet_mobile.tailnet, "is_governance_pinned_off", return_value=False),
            patch.object(
                tailnet_mobile.tailnet,
                "probe_daemon",
                return_value=_probe(login="owner@example.com"),
            ),
            patch.object(
                tailnet_mobile.KiroCrewConfig,
                "load",
                classmethod(lambda cls: effective),
            ),
        ):
            resp = await tailnet_mobile.api_tailnet_mobile_configure(_request())

        assert resp.status == 409
        payload = json.loads(resp.body)
        assert payload["code"] == "config_overlay_conflict"
        assert payload["fields"] == [
            "dashboard.tailscale.trust_identity",
            "dashboard.qr_session_persist_across_restart",
        ]
        assert set(payload) == {"error", "code", "fields"}
        # The base still receives the requested safe shape; removing the local
        # override later makes it effective without another setup mutation.
        saved = json.loads(cfg_path.read_text(encoding="utf-8"))
        assert saved["dashboard"]["tailscale"]["trust_identity"] is True
        assert saved["dashboard"]["qr_session_persist_across_restart"] is True

    @pytest.mark.asyncio
    async def test_effective_timed_opt_out_remains_a_nonpersistent_success(
        self, tmp_path, _unrestricted, _quiet_audit
    ) -> None:
        cfg_path = tmp_path / "config.json"
        cfg_path.write_text("{}", encoding="utf-8")
        effective = self._effective_cfg(until_restart=False, persistent=False)
        with (
            patch.object(tailnet_mobile, "config_path", return_value=cfg_path),
            patch.object(tailnet_mobile.tailnet, "is_governance_pinned_off", return_value=False),
            patch.object(tailnet_mobile.tailnet, "probe_daemon", return_value=_probe()),
            patch.object(
                tailnet_mobile.KiroCrewConfig,
                "load",
                classmethod(lambda cls: effective),
            ),
        ):
            resp = await tailnet_mobile.api_tailnet_mobile_configure(_request())

        assert resp.status == 200
        payload = json.loads(resp.body)
        assert payload == {"restart_required": True}

    @pytest.mark.asyncio
    async def test_retry_after_overlay_removal_still_requires_live_trust_restart(
        self, tmp_path, _unrestricted, _quiet_audit
    ) -> None:
        """The prior conflicting attempt already wrote the base file, but the
        running middleware still has the old disabled trust snapshot."""
        cfg_path = tmp_path / "config.json"
        cfg_path.write_text(
            json.dumps(
                {
                    "dashboard": {
                        "qr_session_until_restart": True,
                        "qr_session_persist_across_restart": True,
                        "tailscale": {
                            "enabled": True,
                            "trust_identity": True,
                            "allowed_logins": ["owner@example.com"],
                        },
                    }
                }
            ),
            encoding="utf-8",
        )
        with (
            patch.object(tailnet_mobile, "config_path", return_value=cfg_path),
            patch.object(tailnet_mobile.tailnet, "is_governance_pinned_off", return_value=False),
            patch.object(tailnet_mobile.tailnet, "probe_daemon", return_value=_probe()),
            patch.object(
                tailnet_mobile.KiroCrewConfig,
                "load",
                classmethod(lambda cls: self._effective_cfg()),
            ),
        ):
            resp = await tailnet_mobile.api_tailnet_mobile_configure(_request())

        payload = json.loads(resp.body)
        assert resp.status == 200
        assert payload == {"restart_required": True}

    @pytest.mark.asyncio
    async def test_idempotent_setup_needs_no_restart_when_live_trust_matches(
        self, tmp_path, _unrestricted, _quiet_audit
    ) -> None:
        cfg_path = tmp_path / "config.json"
        cfg_path.write_text(
            json.dumps(
                {
                    "dashboard": {
                        "qr_session_until_restart": True,
                        "qr_session_persist_across_restart": True,
                        "tailscale": {
                            "enabled": True,
                            "trust_identity": True,
                            "allowed_logins": ["owner@example.com"],
                        },
                    }
                }
            ),
            encoding="utf-8",
        )
        request = _request()
        request.app["tailnet_trust"] = tailnet.TailnetTrust(
            trust_identity=True,
            allowed_logins=("owner@example.com",),
        )
        with (
            patch.object(tailnet_mobile, "config_path", return_value=cfg_path),
            patch.object(tailnet_mobile.tailnet, "is_governance_pinned_off", return_value=False),
            patch.object(tailnet_mobile.tailnet, "probe_daemon", return_value=_probe()),
            patch.object(
                tailnet_mobile.KiroCrewConfig,
                "load",
                classmethod(lambda cls: self._effective_cfg()),
            ),
        ):
            resp = await tailnet_mobile.api_tailnet_mobile_configure(request)

        payload = json.loads(resp.body)
        assert resp.status == 200
        assert payload == {"restart_required": False}

    @pytest.mark.asyncio
    @pytest.mark.parametrize("probe_kw", [{"name": ""}, {"login": ""}])
    async def test_daemon_without_a_usable_identity_refuses_before_writing(
        self, probe_kw, _unrestricted, _quiet_audit
    ) -> None:
        with (
            patch.object(tailnet_mobile.tailnet, "is_governance_pinned_off", return_value=False),
            patch.object(
                tailnet_mobile.tailnet,
                "probe_daemon",
                return_value=_probe(**probe_kw),
            ),
            patch.object(tailnet_mobile, "update_config_locked") as write,
        ):
            resp = await tailnet_mobile.api_tailnet_mobile_configure(_request())
        assert resp.status == 409
        assert b"daemon_not_ready" in resp.body
        write.assert_not_called()


class TestQrRefusals:
    """A credential is never minted for a URL nothing answers.

    Every case drives the REAL ``_derive_step`` through ``_machine``, because the
    defect these cover is not any single missing check — it is that the mint used
    to re-implement two of the seven preconditions by hand and admit the rest.
    """

    @pytest.mark.asyncio
    async def test_no_tailnet_name_refuses(self, _unrestricted, _quiet_audit) -> None:
        with _machine(name=""):
            resp = await tailnet_mobile.api_tailnet_mobile_qr(_request(tailnet_host=_HOST))
        assert resp.status == 409
        assert b"no_name" in resp.body

    @pytest.mark.asyncio
    async def test_unpublished_refuses_before_minting(self, _unrestricted, _quiet_audit) -> None:
        """The refusal must come BEFORE generate_token — a token handed out for an
        unreachable URL is a live credential spent on nothing."""
        with _machine(published=False, detail="not ours"):
            with patch.object(tailnet_mobile, "generate_token") as mint:
                resp = await tailnet_mobile.api_tailnet_mobile_qr(_request(tailnet_host=_HOST))
        assert resp.status == 409
        assert b"not_published" in resp.body
        mint.assert_not_called()

    @pytest.mark.asyncio
    async def test_undetermined_serve_state_also_refuses(self, _unrestricted, _quiet_audit) -> None:
        with _machine(published=None, detail="unreadable"):
            resp = await tailnet_mobile.api_tailnet_mobile_qr(_request(tailnet_host=_HOST))
        assert resp.status == 409

    @pytest.mark.asyncio
    async def test_governance_pin_refuses_even_while_still_published(
        self, _unrestricted, _quiet_audit
    ) -> None:
        """The security half, and the reachable case that motivated the gate.

        Pinning the policy off does NOT tear down an existing publication, so the
        serve stays up and reachable. Without consulting the derivation, the mint
        happily issued a fresh OWNER credential over a tailnet the administrator's
        ceiling forbids — the pin was enforced on ``publish`` (via
        ``tailnet_serve.publish``) and merely *reported* by the status read.
        """
        with _machine(pinned=True, published=True):
            with patch.object(tailnet_mobile, "generate_token") as mint:
                resp = await tailnet_mobile.api_tailnet_mobile_qr(_request(tailnet_host=_HOST))
        assert resp.status == 409
        assert b"governance_pinned" in resp.body
        mint.assert_not_called()

    @pytest.mark.asyncio
    async def test_untrusted_origin_refuses(self, _unrestricted, _quiet_audit) -> None:
        """Published but ``dashboard.tailscale.enabled`` off: the gateway rejects its
        own tailnet origin, so the phone would open the link and be answered 403."""
        with _machine(trusted=False, published=True):
            resp = await tailnet_mobile.api_tailnet_mobile_qr(_request(tailnet_host=_HOST))
        assert resp.status == 409
        assert b"origin_not_trusted" in resp.body

    @pytest.mark.asyncio
    async def test_server_without_a_startup_origin_refuses(
        self, _unrestricted, _quiet_audit
    ) -> None:
        """Resolvable now, but the startup boundary does not trust the name yet."""
        with _machine(published=True):
            resp = await tailnet_mobile.api_tailnet_mobile_qr(_request(tailnet_host=""))
        assert resp.status == 409
        assert b"restart_required" in resp.body

    def test_every_non_ready_step_has_a_refusal(self) -> None:
        """``ready`` is the ONLY step that may mint.

        Pins the fail-closed direction structurally: a step added later without a
        refusal entry would otherwise fall through to the generic ``not_ready``
        branch, which is correct but silent. This makes the omission a test
        failure at the point the step is introduced.
        """
        steps = set(get_args(tailnet_mobile.Step))
        assert steps - {"ready"} == set(tailnet_mobile._QR_REFUSALS)
        assert "ready" not in tailnet_mobile._QR_REFUSALS

    @pytest.mark.asyncio
    async def test_the_default_session_lasts_until_the_gateway_restarts(
        self, _unrestricted, _quiet_audit
    ) -> None:
        """Default: the session is scoped to this process, not to a clock.

        Pinned because it IS the default — the shape a scan produces with no
        configuration at all is the one most likely to be changed by accident.
        """
        from kiro_crew.dashboard.boot_id import current_boot_id

        captured: dict[str, object] = {}

        def _fake_mint(_sub, ttl_seconds=0, **kw):
            captured["extra"] = kw.get("extra")
            return "tok"

        with _machine():
            with (
                patch.object(tailnet_mobile, "generate_token", side_effect=_fake_mint),
                patch.object(
                    tailnet_mobile, "render_qr_data_uri", return_value="data:image/png;base64,x"
                ),
            ):
                resp = await tailnet_mobile.api_tailnet_mobile_qr(
                    _request(tailnet_host=_HOST, cookie_token=_owner_session_token())
                )
        assert resp.status == 200
        assert captured["extra"] == {"boot": current_boot_id()}
        assert "no_refresh" not in (captured["extra"] or {})

    @pytest.mark.asyncio
    async def test_opting_out_restores_the_timed_ceiling(self, _unrestricted, _quiet_audit) -> None:
        """Opted out: no refresh chain, so ``session_exp`` is a real ceiling.

        The two shapes are mutually exclusive — a token carrying both would
        neither refresh nor last.
        """
        captured: dict[str, object] = {}

        def _fake_mint(_sub, ttl_seconds=0, **kw):
            captured["extra"] = kw.get("extra")
            return "tok"

        with _machine(qr_session_until_restart=False):
            with (
                patch.object(tailnet_mobile, "generate_token", side_effect=_fake_mint),
                patch.object(
                    tailnet_mobile, "render_qr_data_uri", return_value="data:image/png;base64,x"
                ),
            ):
                resp = await tailnet_mobile.api_tailnet_mobile_qr(
                    _request(tailnet_host=_HOST, cookie_token=_owner_session_token())
                )
        assert resp.status == 200
        assert captured["extra"] == {"no_refresh": "1"}
        assert "boot" not in (captured["extra"] or {})

    @pytest.mark.asyncio
    async def test_persistent_shape_drops_the_boot_bound(self, _unrestricted, _quiet_audit) -> None:
        """Opted in WITH identity trust: no ``boot``, so one scan outlives a restart.

        The refresh chain is still issued (no ``no_refresh``), so what bounds the
        session is the chain's own lifetime rather than this process's.
        """
        captured: dict[str, object] = {}

        def _fake_mint(_sub, ttl_seconds=0, **kw):
            captured["extra"] = kw.get("extra")
            return "tok"

        with _machine(
            qr_session_persist_across_restart=True,
            trust_identity=True,
            allowed_logins=("someone@example.com",),
        ):
            with (
                patch.object(tailnet_mobile, "generate_token", side_effect=_fake_mint),
                patch.object(
                    tailnet_mobile, "render_qr_data_uri", return_value="data:image/png;base64,x"
                ),
            ):
                resp = await tailnet_mobile.api_tailnet_mobile_qr(
                    _request(tailnet_host=_HOST, cookie_token=_owner_session_token())
                )
        assert resp.status == 200
        extra = captured["extra"] or {}
        assert "boot" not in extra
        assert "no_refresh" not in extra
        # The identity bound that replaces the process bound. Without it the
        # chain would rotate for any caller, which is the whole point of not
        # having a boot claim being safe.
        assert extra["require_peer"] == "1"

    @pytest.mark.asyncio
    async def test_persistent_shape_refused_without_identity_trust(
        self, _unrestricted, _quiet_audit
    ) -> None:
        """Opted in but identity trust off: stays boot-bound, and says so.

        Behind ``tailscale serve`` every request arrives from 127.0.0.1, so
        without a daemon-verified peer the cookie is a bearer credential any
        tailnet peer could replay - a session that outlives the process must not
        be handed out on that basis. Silently honouring the flag would leave the
        operator believing their phone survives restarts.
        """
        from kiro_crew.dashboard.boot_id import current_boot_id

        captured: dict[str, object] = {}

        def _fake_mint(_sub, ttl_seconds=0, **kw):
            captured["extra"] = kw.get("extra")
            return "tok"

        with _machine(qr_session_persist_across_restart=True):
            with (
                patch.object(tailnet_mobile, "generate_token", side_effect=_fake_mint),
                patch.object(
                    tailnet_mobile, "render_qr_data_uri", return_value="data:image/png;base64,x"
                ),
            ):
                resp = await tailnet_mobile.api_tailnet_mobile_qr(
                    _request(tailnet_host=_HOST, cookie_token=_owner_session_token())
                )
        assert resp.status == 200
        assert captured["extra"] == {"boot": current_boot_id()}

    @pytest.mark.asyncio
    async def test_persistent_shape_refused_when_timed_shape_is_in_force(
        self, _unrestricted, _quiet_audit
    ) -> None:
        """Persist + opted-out is contradictory: there is no chain to carry over."""
        captured: dict[str, object] = {}

        def _fake_mint(_sub, ttl_seconds=0, **kw):
            captured["extra"] = kw.get("extra")
            return "tok"

        with _machine(
            qr_session_until_restart=False,
            qr_session_persist_across_restart=True,
            trust_identity=True,
            allowed_logins=("someone@example.com",),
        ):
            with (
                patch.object(tailnet_mobile, "generate_token", side_effect=_fake_mint),
                patch.object(
                    tailnet_mobile, "render_qr_data_uri", return_value="data:image/png;base64,x"
                ),
            ):
                resp = await tailnet_mobile.api_tailnet_mobile_qr(
                    _request(tailnet_host=_HOST, cookie_token=_owner_session_token())
                )
        assert resp.status == 200
        assert captured["extra"] == {"no_refresh": "1"}

    @pytest.mark.asyncio
    async def test_unreadable_config_falls_back_to_the_default(
        self, _unrestricted, _quiet_audit
    ) -> None:
        """A config problem resolves to the DEFAULT, not to the other shape.

        The stubbed config says opted-OUT, and only the handler's own read fails,
        so a fallback that guessed "timed" would pass here by accident. Falling
        back to the default is the honest reading of "we could not read your
        override"; picking the timed shape instead would present as a phone that
        signs itself out for no reason the operator can see. Only the first load
        (``_live_state``'s) succeeds — making every load raise would refuse the
        request at the origin-trust gate long before the mint and prove nothing.
        """
        from kiro_crew.dashboard.boot_id import current_boot_id

        opted_out = SimpleNamespace(
            dashboard=SimpleNamespace(
                tailscale=SimpleNamespace(enabled=True, keep_awake=True),
                qr_session_until_restart=False,
            )
        )
        loads = {"n": 0}

        def _load_then_fail(_cls=None):
            loads["n"] += 1
            if loads["n"] == 1:
                return opted_out
            raise OSError("unreadable")

        captured: dict[str, object] = {}

        def _fake_mint(_sub, ttl_seconds=0, **kw):
            captured["extra"] = kw.get("extra")
            return "tok"

        with _machine():
            with (
                patch.object(
                    tailnet_mobile.KiroCrewConfig,
                    "load",
                    classmethod(lambda cls: _load_then_fail()),
                ),
                # This test pins the CONFIG fallback, not governance. The
                # capabilities.mobile_connect pre-check would lazily build the
                # platform context (its own KiroCrewConfig.load), consuming the
                # single successful load this fixture budgets for _live_state —
                # so neutralize it here; the governance path has its own pins
                # in test_mobile_connect_seam.py.
                patch.object(tailnet_mobile, "mint_denied_reason", return_value=""),
                patch.object(tailnet_mobile, "generate_token", side_effect=_fake_mint),
                patch.object(
                    tailnet_mobile, "render_qr_data_uri", return_value="data:image/png;base64,x"
                ),
            ):
                resp = await tailnet_mobile.api_tailnet_mobile_qr(
                    _request(tailnet_host=_HOST, cookie_token=_owner_session_token())
                )
        assert resp.status == 200
        assert loads["n"] >= 2, "the handler must do its own read, not reuse _live_state's"
        assert captured["extra"] == {"boot": current_boot_id()}

    @pytest.mark.asyncio
    async def test_ttl_cannot_be_talked_past_the_endpoint_ceiling(
        self, _unrestricted, _quiet_audit
    ) -> None:
        """A caller-supplied TTL is clamped by this endpoint's own ceiling, which is
        deliberately lower than the global session cap: behind `tailscale serve` the
        session cannot be pinned to the scanning device, so the token is the only
        credential."""
        captured: dict[str, int] = {}

        def _fake_mint(_sub, ttl_seconds=0, **_kw):
            captured["ttl"] = ttl_seconds
            return "tok"

        with _machine():
            with (
                patch.object(tailnet_mobile, "generate_token", side_effect=_fake_mint),
                patch.object(
                    tailnet_mobile, "render_qr_data_uri", return_value="data:image/png;base64,x"
                ),
            ):
                resp = await tailnet_mobile.api_tailnet_mobile_qr(
                    _request(body={"ttl": "500h"}, tailnet_host=_HOST)
                )
        assert resp.status == 200
        assert captured["ttl"] <= tailnet_mobile.MAX_QR_TTL_SECS
        assert captured["ttl"] <= tailnet_mobile.MAX_SESSION_TTL_SECS

    @pytest.mark.asyncio
    async def test_audit_record_never_carries_the_token(self, _unrestricted) -> None:
        """The audit trail records that a credential was issued, never the credential
        — a SEL row is durable, and a token in it would outlive the session."""
        with _machine():
            with (
                patch.object(tailnet_mobile, "generate_token", return_value="SECRET-TOKEN-VALUE"),
                patch.object(
                    tailnet_mobile, "render_qr_data_uri", return_value="data:image/png;base64,x"
                ),
                patch.object(tailnet_mobile, "_audit") as audit,
            ):
                resp = await tailnet_mobile.api_tailnet_mobile_qr(_request(tailnet_host=_HOST))
        assert resp.status == 200
        for call in audit.call_args_list:
            assert "SECRET-TOKEN-VALUE" not in " ".join(str(a) for a in call.args)


class TestQrCallerBounds:
    """The QR-minted token never out-scopes the session that authorized it.

    Mirrors the coverage of the sibling mobile-link mint
    (``test_mobile_login_link.py``): the mint reads the CALLER's own token
    bounds through the shared ``_caller_bounds`` helper and carries them into
    the credential. Behind ``tailscale serve`` every request reaches the
    gateway from 127.0.0.1, so the token cannot be device-pinned — its own
    bounds are the only limit that holds, which is what made this surface the
    laundering path: one POST from a deliberately bounded owner session used to
    mint a boot-bound, refresh-chained credential that outlived it.
    """

    @staticmethod
    def _capture(captured: dict):
        def _fake_mint(_sub, ttl_seconds=0, **kw):
            captured["ttl"] = ttl_seconds
            captured["extra"] = kw.get("extra")
            captured["peer_key"] = kw.get("peer_key", "")
            return "tok"

        return _fake_mint

    async def _mint(self, captured: dict, **request_kw):
        with _machine():
            with (
                patch.object(tailnet_mobile, "generate_token", side_effect=self._capture(captured)),
                patch.object(
                    tailnet_mobile, "render_qr_data_uri", return_value="data:image/png;base64,x"
                ),
            ):
                return await tailnet_mobile.api_tailnet_mobile_qr(
                    _request(tailnet_host=_HOST, **request_kw)
                )

    @pytest.mark.asyncio
    async def test_a_no_refresh_caller_mints_a_no_refresh_credential(
        self, _unrestricted, _quiet_audit
    ) -> None:
        """A caller whose session never grows a refresh chain must not mint a
        credential that does — that is the laundering this gate closes."""
        captured: dict[str, object] = {}
        resp = await self._mint(
            captured, cookie_token=_owner_session_token(ttl_seconds=600, no_refresh="1")
        )
        assert resp.status == 200
        extra = captured["extra"]
        assert isinstance(extra, dict) and extra["no_refresh"] == "1"

    @pytest.mark.asyncio
    async def test_a_short_lived_caller_caps_the_qr_ttl(self, _unrestricted, _quiet_audit) -> None:
        """The default QR TTL is above this caller's remaining lifetime, so the
        remaining lifetime wins — a short-lived session cannot lend more time
        than it has."""
        captured: dict[str, object] = {}
        resp = await self._mint(captured, cookie_token=_owner_session_token(ttl_seconds=600))
        assert resp.status == 200
        assert isinstance(captured["ttl"], int)
        assert 0 < captured["ttl"] <= 600 < tailnet_mobile.DEFAULT_QR_TTL_SECS

    @pytest.mark.asyncio
    async def test_the_reported_link_window_never_exceeds_the_lent_ttl(
        self, _unrestricted, _quiet_audit
    ) -> None:
        """``link_window_secs`` reports the clamped click window, not the
        constant — generate_token clamps the link-click ``exp`` to the session
        TTL, so a caller lending less than the nominal window mints a QR whose
        link dies with the ttl it lent, and the UI countdown must say so."""
        captured: dict[str, object] = {}
        resp = await self._mint(captured, cookie_token=_owner_session_token(ttl_seconds=120))
        assert resp.status == 200
        payload = json.loads(resp.body)
        assert 0 < payload["link_window_secs"] <= 120
        assert payload["link_window_secs"] == min(payload["ttl_secs"], LINK_WINDOW_SECS)

    @pytest.mark.asyncio
    async def test_a_caller_with_nothing_left_is_refused(self, _unrestricted, _quiet_audit) -> None:
        """An exhausted remaining lifetime refuses BEFORE minting, never rounds
        up — a floor of one second would let a dead session walk its expiry
        forward indefinitely, one mint at a time."""
        expired = _b64url_encode(json.dumps({"session_exp": time.time() - 100}).encode()) + ".sig"
        with _machine():
            with patch.object(tailnet_mobile, "generate_token") as mint:
                resp = await tailnet_mobile.api_tailnet_mobile_qr(
                    _request(tailnet_host=_HOST, cookie_token=expired)
                )
        assert resp.status == 403
        assert b"caller_session_expired" in resp.body
        mint.assert_not_called()

    @pytest.mark.asyncio
    async def test_a_caller_expiring_during_a_slow_body_read_is_refused(
        self, _unrestricted, _quiet_audit
    ) -> None:
        """The bounds are read AFTER every awaited step, so a stale snapshot
        cannot authorize the mint. A client controls how long ``request.json()``
        takes (it can trickle the body byte by byte), so a remaining-lifetime
        snapshot taken before that await would let a session in its last
        seconds stretch the mint past its own expiry — the wall clock moves
        while the handler waits, the snapshot does not."""
        caller = _owner_session_token(ttl_seconds=60)
        req = _request(tailnet_host=_HOST, cookie_token=caller)
        clock = patch(
            "kiro_crew.dashboard.handlers._shared.time.time",
            return_value=time.time() + 120,
        )

        async def _slow_body():
            # The caller's session expires while the body trickles in: from
            # here on, the shared helper's clock reads past ``session_exp``.
            clock.start()
            return {}

        req.json = _slow_body
        try:
            with _machine():
                with patch.object(tailnet_mobile, "generate_token") as mint:
                    resp = await tailnet_mobile.api_tailnet_mobile_qr(req)
        finally:
            clock.stop()
        assert resp.status == 403
        assert b"caller_session_expired" in resp.body
        mint.assert_not_called()

    @pytest.mark.asyncio
    async def test_an_unbounded_owner_session_is_unaffected(
        self, _unrestricted, _quiet_audit
    ) -> None:
        """The ordinary case must not regress: a full-length owner session mints
        the configured default shape at the default TTL."""
        from kiro_crew.dashboard.boot_id import current_boot_id

        captured: dict[str, object] = {}
        resp = await self._mint(captured, cookie_token=_owner_session_token())
        assert resp.status == 200
        assert captured["extra"] == {"boot": current_boot_id()}
        assert captured["ttl"] == tailnet_mobile.DEFAULT_QR_TTL_SECS

    @pytest.mark.asyncio
    async def test_a_callers_boot_claim_is_carried_verbatim(
        self, _unrestricted, _quiet_audit
    ) -> None:
        """Carried, never re-derived — the same rule the link→session exchange
        follows, so a bound is copied from the credential that authorized the
        mint rather than re-invented from process state."""
        captured: dict[str, object] = {}
        resp = await self._mint(
            captured, cookie_token=_owner_session_token(boot="boot-from-caller")
        )
        assert resp.status == 200
        extra = captured["extra"]
        assert isinstance(extra, dict) and extra["boot"] == "boot-from-caller"

    @pytest.mark.asyncio
    async def test_a_peer_bound_caller_carries_its_exact_device_key(
        self, _unrestricted, _quiet_audit
    ) -> None:
        """A child QR cannot turn a device-bound owner session into a bearer link."""
        peer_key = "ts:node:owner@example.com|desktop.tail.ts.net"
        captured: dict[str, object] = {}
        response = await self._mint(
            captured,
            cookie_token=_owner_session_token(
                require_peer="1",
                peer_key=peer_key,
            ),
        )
        assert response.status == 200
        extra = captured["extra"]
        assert isinstance(extra, dict) and extra["require_peer"] == "1"
        assert captured["peer_key"] == peer_key

    @pytest.mark.asyncio
    async def test_bounds_come_from_the_query_token_not_a_stray_cookie(
        self, _unrestricted, _quiet_audit
    ) -> None:
        """Bounds must be read from the credential the middleware validated. It
        prefers ``?token=`` over the cookie, so a bounded query token beside a
        permissive (unverified, attacker-settable) cookie must still bound the
        mint — reading the cookie first would drop ``no_refresh`` and raise the
        TTL ceiling back to the maximum."""
        captured: dict[str, object] = {}
        resp = await self._mint(
            captured,
            query_token=_owner_session_token(ttl_seconds=600, no_refresh="1"),
            cookie_token=_owner_session_token(),
        )
        assert resp.status == 200
        extra = captured["extra"]
        assert isinstance(extra, dict) and extra["no_refresh"] == "1"
        assert isinstance(captured["ttl"], int) and captured["ttl"] <= 600


class TestRestrictedSessionRefused:
    """An app-scoped session must not escalate out of its sandbox."""

    _MUTATIONS = [
        tailnet_mobile.api_tailnet_mobile_configure,
        tailnet_mobile.api_tailnet_mobile_publish,
        tailnet_mobile.api_tailnet_mobile_unpublish,
        tailnet_mobile.api_tailnet_mobile_qr,
    ]

    @pytest.mark.asyncio
    @pytest.mark.parametrize("handler", _MUTATIONS)
    async def test_every_mutation_refuses_an_app_token(self, handler) -> None:
        """The load-bearing gate.

        An app token is admitted by the middleware for whatever path prefixes its
        manifest ``permissions.api`` declares, and it carries no
        ``X-Session-Key`` — so the restricted-session predicate answers "not
        restricted" and cannot stop it. Without the app gate, an app declaring
        ``/api/tailnet/mobile`` could publish this dashboard to a whole tailnet
        and, from the QR endpoint, mint itself an OWNER-scoped dashboard-user
        token: a straight escape from the app sandbox.
        """
        with patch.object(tailnet_mobile, "_audit"):
            resp = await handler(_request(app_identity="some-installed-app"))
        assert resp.status == 403
        assert b"dashboard-user" in resp.body

    @pytest.mark.asyncio
    @pytest.mark.parametrize("handler", _MUTATIONS)
    async def test_every_mutation_refuses_when_middleware_never_ran(self, handler) -> None:
        """An ABSENT app key means the auth middleware did not run. Falling through
        then is the same escalation by another route, so it must deny."""
        with patch.object(tailnet_mobile, "_audit"):
            resp = await handler(_request(app_identity=None))
        assert resp.status == 403

    @pytest.mark.asyncio
    @pytest.mark.parametrize("handler", _MUTATIONS)
    async def test_every_mutation_refuses_a_restricted_session(self, handler) -> None:
        with (
            patch.object(tailnet_mobile, "_is_restricted_session", return_value=True),
            patch.object(tailnet_mobile, "_audit"),
        ):
            resp = await handler(_request(restricted=True))
        assert resp.status == 403

    @pytest.mark.asyncio
    async def test_qr_refuses_an_unresolved_port_rather_than_minting(
        self, _unrestricted, _quiet_audit
    ) -> None:
        """The published gate must not vanish when the port is falsy. Its sibling
        `publish` refuses at port 0; the two must not disagree about whether an
        unknown port is safe to hand a credential for."""
        with (
            patch.object(tailnet_mobile.tailnet, "self_dns_name", return_value=_HOST),
            patch.object(tailnet_mobile, "generate_token") as mint,
        ):
            resp = await tailnet_mobile.api_tailnet_mobile_qr(_request(port=0))
        assert resp.status == 409
        assert b"unknown_port" in resp.body
        mint.assert_not_called()

    @pytest.mark.asyncio
    async def test_missing_dashboard_state_is_also_refused(self) -> None:
        """No state means the guard cannot evaluate, which must deny rather than
        fall through to the action."""

        class _Req:
            app: dict = {"port": _PORT}
            remote = "127.0.0.1"
            headers: dict = {}

            def get(self, key: str, default: object = None) -> object:
                # Dashboard user, so the app gate passes and the STATE gate is
                # what this test exercises.
                return "" if key == "app" else default

        with patch.object(tailnet_mobile, "_audit"):
            resp = await tailnet_mobile.api_tailnet_mobile_publish(_Req())
        assert resp.status == 403


class TestKeepAwakeProbe:
    """The sleep decision must fail toward LETTING THE HOST SLEEP.

    Lives here rather than in ``test_power.py`` because the term under test is the
    tailnet one: a published dashboard keeps the system awake so a phone does not
    lose it when the laptop idles. The turn-based term is that suite's subject.
    """

    @pytest.fixture(autouse=True)
    def _clear_cache(self):
        from kiro_crew.dashboard import server as srv

        srv._tailnet_awake_cache = (0.0, False)
        yield
        srv._tailnet_awake_cache = (0.0, False)

    @pytest.mark.asyncio
    async def test_publishing_keeps_the_host_awake_without_the_turn_opt_in(self) -> None:
        """Publishing is itself the consent — an operator must not have to also find
        ``dashboard.prevent_sleep``, which is scoped to in-flight turns."""
        from kiro_crew.dashboard import server as srv

        cfg = SimpleNamespace(
            dashboard=SimpleNamespace(
                prevent_sleep=False,
                tailscale=SimpleNamespace(enabled=True, keep_awake=True),
            )
        )
        published = SimpleNamespace(published=True, configured=True, detail="ours")
        with (
            patch.object(srv.KiroCrewConfig, "load", classmethod(lambda cls: cfg)),
            patch.object(srv.tailnet_serve, "serve_state", return_value=published),
        ):
            assert await srv._should_prevent_sleep(SimpleNamespace(sessions=None), _PORT) is True

    @pytest.mark.asyncio
    async def test_keep_awake_off_lets_the_host_sleep(self) -> None:
        """The opt-OUT of the awake half, without having to unpublish."""
        from kiro_crew.dashboard import server as srv

        cfg = SimpleNamespace(
            dashboard=SimpleNamespace(
                prevent_sleep=False,
                tailscale=SimpleNamespace(enabled=True, keep_awake=False),
            )
        )
        with (
            patch.object(srv.KiroCrewConfig, "load", classmethod(lambda cls: cfg)),
            patch.object(srv.tailnet_serve, "serve_state") as serve,
        ):
            assert await srv._should_prevent_sleep(SimpleNamespace(sessions=None), _PORT) is False
        serve.assert_not_called()

    @pytest.mark.asyncio
    async def test_undetermined_serve_state_lets_the_host_sleep(self) -> None:
        """An unresolvable probe must never pin a laptop awake indefinitely."""
        from kiro_crew.dashboard import server as srv

        cfg = SimpleNamespace(
            dashboard=SimpleNamespace(
                prevent_sleep=False,
                tailscale=SimpleNamespace(enabled=True, keep_awake=True),
            )
        )
        unknown = SimpleNamespace(published=None, configured=None, detail="unreadable")
        with (
            patch.object(srv.KiroCrewConfig, "load", classmethod(lambda cls: cfg)),
            patch.object(srv.tailnet_serve, "serve_state", return_value=unknown),
        ):
            assert await srv._should_prevent_sleep(SimpleNamespace(sessions=None), _PORT) is False

    @pytest.mark.asyncio
    async def test_config_without_a_tailscale_section_does_not_raise(self) -> None:
        """A config object predating the section must resolve to "allow sleep", not
        propagate an AttributeError. The contract is fail-closed for ANY failure, and
        a raising probe inside the poll would be swallowed and retried forever."""
        from kiro_crew.dashboard import server as srv

        cfg = SimpleNamespace(dashboard=SimpleNamespace(prevent_sleep=False))
        with patch.object(srv.KiroCrewConfig, "load", classmethod(lambda cls: cfg)):
            assert await srv._should_prevent_sleep(SimpleNamespace(sessions=None), _PORT) is False

    @pytest.mark.asyncio
    async def test_probe_is_cached_so_a_15s_poll_does_not_spawn_a_cli_each_time(self) -> None:
        from kiro_crew.dashboard import server as srv

        published = SimpleNamespace(published=True, configured=True, detail="ours")
        with patch.object(srv.tailnet_serve, "serve_state", return_value=published) as serve:
            assert await srv._tailnet_publish_keeps_awake(_PORT) is True
            assert await srv._tailnet_publish_keeps_awake(_PORT) is True
        assert serve.call_count == 1, "the second poll must read the cache, not the daemon"

    @pytest.mark.asyncio
    async def test_unknown_port_short_circuits_without_probing(self) -> None:
        from kiro_crew.dashboard import server as srv

        with patch.object(srv.tailnet_serve, "serve_state") as serve:
            assert await srv._tailnet_publish_keeps_awake(0) is False
        serve.assert_not_called()

    @pytest.mark.asyncio
    async def test_a_raising_probe_lets_the_host_sleep(self) -> None:
        from kiro_crew.dashboard import server as srv

        with patch.object(
            srv.tailnet_serve, "serve_state", side_effect=RuntimeError("daemon exploded")
        ):
            assert await srv._tailnet_publish_keeps_awake(_PORT) is False


class TestOwnerOnly:
    """A dashboard session is not automatically the OWNER's dashboard session.

    Telegram, Teams and Slack each hand a presigned dashboard link to any ALLOWED
    user, minting a token whose ``sub`` is that user's own id. Such a caller is a
    legitimate dashboard user with ``request["app"] == ""``, so the app gate lets
    it through. The QR endpoint mints an OWNER-subject credential, so without an
    owner gate that caller could trade its own scoped session for an owner one.
    """

    _OTHER = "telegram-11893"

    @pytest.mark.asyncio
    async def test_non_owner_cannot_mint_a_qr_token(self, _unrestricted, _quiet_audit) -> None:
        published = SimpleNamespace(published=True, configured=True, detail="ours")
        with (
            patch.object(tailnet_mobile.tailnet, "self_dns_name", return_value=_HOST),
            patch.object(tailnet_mobile.tailnet_serve, "serve_state", return_value=published),
            patch.object(tailnet_mobile, "generate_token") as mint,
        ):
            resp = await tailnet_mobile.api_tailnet_mobile_qr(_request(user=self._OTHER))
        assert resp.status == 403
        mint.assert_not_called()

    @pytest.mark.asyncio
    async def test_non_owner_cannot_publish(self, _unrestricted, _quiet_audit) -> None:
        with patch.object(tailnet_mobile.tailnet_serve, "publish") as pub:
            resp = await tailnet_mobile.api_tailnet_mobile_publish(_request(user=self._OTHER))
        assert resp.status == 403
        pub.assert_not_called()

    @pytest.mark.asyncio
    async def test_non_owner_cannot_configure_persistent_access(
        self, _unrestricted, _quiet_audit
    ) -> None:
        with patch.object(tailnet_mobile, "update_config_locked") as write:
            resp = await tailnet_mobile.api_tailnet_mobile_configure(_request(user=self._OTHER))
        assert resp.status == 403
        write.assert_not_called()

    @pytest.mark.asyncio
    async def test_non_owner_cannot_unpublish(self, _unrestricted, _quiet_audit) -> None:
        with patch.object(tailnet_mobile.tailnet_serve, "unpublish") as unpub:
            resp = await tailnet_mobile.api_tailnet_mobile_unpublish(_request(user=self._OTHER))
        assert resp.status == 403
        unpub.assert_not_called()

    @pytest.mark.asyncio
    async def test_absent_subject_is_refused(self, _unrestricted, _quiet_audit) -> None:
        """No resolved subject means no owner claim, so it must not fall through."""
        with patch.object(tailnet_mobile, "generate_token") as mint:
            resp = await tailnet_mobile.api_tailnet_mobile_qr(_request(user=None))
        assert resp.status == 403
        mint.assert_not_called()

    @pytest.mark.asyncio
    async def test_owner_is_still_allowed(self, _unrestricted, _quiet_audit) -> None:
        """The gate must not lock the owner out of their own dashboard."""
        with _machine():
            with (
                patch.object(tailnet_mobile, "generate_token", return_value="tok"),
                patch.object(
                    tailnet_mobile, "render_qr_data_uri", return_value="data:image/png;base64,x"
                ),
            ):
                resp = await tailnet_mobile.api_tailnet_mobile_qr(_request(tailnet_host=_HOST))
        assert resp.status == 200


class TestStatusIsOwnerOnly:
    """The status READ is owner-only too, not just the mutations.

    Its body carries the MagicDNS hostname, the publish state and the tailnet's
    device counts. Making only the frontend owner-only still shipped those to any
    non-owner holding a presigned dashboard link, so the read refuses as well.
    """

    @staticmethod
    async def _status(**req_kw):
        """Drive the status GET with a real DaemonProbe, built the way the
        neighbouring probe tests build one (no invented fixture)."""
        with (
            patch.object(tailnet, "_cli_path", return_value="/usr/bin/tailscale"),
            patch.object(
                tailnet, "_run_json_detail", return_value=({"BackendState": "Running"}, False)
            ),
            patch.object(tailnet, "self_dns_name", return_value=_HOST),
            patch.object(
                tailnet_mobile.tailnet_serve,
                "serve_state",
                return_value=SimpleNamespace(published=True, configured=True, detail="ours"),
            ),
        ):
            return await tailnet_mobile.api_tailnet_mobile_status(_request(**req_kw))

    @pytest.mark.asyncio
    async def test_owner_can_read_the_card_state(self, _unrestricted, _quiet_audit) -> None:
        resp = await self._status()
        assert resp.status == 200
        assert json.loads(resp.body)["boot_id"]

    @pytest.mark.asyncio
    async def test_non_owner_read_is_refused(self, _unrestricted, _quiet_audit) -> None:
        resp = await self._status(user="telegram-11893")
        assert resp.status == 403

    @pytest.mark.asyncio
    async def test_app_token_read_is_refused(self, _unrestricted, _quiet_audit) -> None:
        resp = await self._status(app_identity="some-app")
        assert resp.status == 403

    @pytest.mark.asyncio
    async def test_refused_read_is_audited(self, _unrestricted) -> None:
        """A denial is a decision, so it leaves a record — unlike a successful poll.

        The 200 path deliberately does not audit (the card polls it every 30s and
        auditing a question would bury the decisions). A refusal is someone without
        owner rights reaching for this machine's network facts, which is exactly
        what the SEL is for.
        """
        with patch.object(tailnet_mobile, "_audit") as audit:
            resp = await self._status(user="telegram-11893")
        assert resp.status == 403
        assert audit.call_args is not None
        assert audit.call_args.args[2] == "denied"

    @pytest.mark.asyncio
    async def test_cold_denial_audit_is_offloaded(self, _unrestricted) -> None:
        """SEL initialization and DACL work must never run on aiohttp's loop."""
        real_to_thread = asyncio.to_thread
        with (
            patch.object(tailnet_mobile, "_audit") as audit,
            patch.object(
                tailnet_mobile.asyncio,
                "to_thread",
                side_effect=real_to_thread,
            ) as offload,
        ):
            resp = await self._status(user="telegram-11893")

        assert resp.status == 403
        assert any(call.args and call.args[0] is audit for call in offload.call_args_list)

    @pytest.mark.asyncio
    async def test_successful_read_is_not_audited(self, _unrestricted) -> None:
        """The anti-noise half of the same rule: a 200 poll writes no SEL row."""
        with patch.object(tailnet_mobile, "_audit") as audit:
            resp = await self._status()
        assert resp.status == 200
        audit.assert_not_called()

    @pytest.mark.asyncio
    async def test_refused_read_leaks_no_network_facts(self, _unrestricted, _quiet_audit) -> None:
        """The point of the gate: the hostname must not travel in the refusal."""
        resp = await self._status(user="telegram-11893")
        assert _HOST.encode() not in resp.body
