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
from contextlib import contextmanager
from types import SimpleNamespace
from typing import get_args
from unittest.mock import patch

import pytest

from kiro_crew.dashboard import tailnet
from kiro_crew.dashboard.handlers import tailnet_mobile
from kiro_crew.dashboard.tailnet import DaemonProbe

_PORT = 5476
_HOST = "desk.tail-abc.ts.net"


def _probe(
    *,
    name: str = _HOST,
    installed: bool = True,
    reachable: bool = True,
    logged_in: bool = True,
    https_enabled: bool | None = True,
    detail: str = "",
) -> DaemonProbe:
    return DaemonProbe(
        name=name,
        installed=installed,
        reachable=reachable,
        logged_in=logged_in,
        detail=detail,
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


_OWNER = "owner@example.com"


def _request(
    *,
    restricted: bool = False,
    port: int = _PORT,
    body: object = None,
    app_identity: str | None = "",
    user: str | None = _OWNER,
    tailnet_host: str = "",
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
            self._items: dict[str, object] = {}
            if app_identity is not None:
                self._items["app"] = app_identity
            if user is not None:
                self._items["user"] = user

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
    installed: bool = True,
    reachable: bool = True,
    logged_in: bool = True,
    https_enabled: bool | None = True,
    published: bool | None = True,
    trusted: bool = True,
    qr_session_until_restart: bool = True,
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
            tailscale=SimpleNamespace(enabled=trusted, keep_awake=True),
            qr_session_until_restart=qr_session_until_restart,
        )
    )
    probe = tailnet.DaemonProbe(
        name=name,
        installed=installed,
        reachable=reachable,
        logged_in=logged_in,
        detail=detail,
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
                resp = await tailnet_mobile.api_tailnet_mobile_qr(_request(tailnet_host=_HOST))
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
                resp = await tailnet_mobile.api_tailnet_mobile_qr(_request(tailnet_host=_HOST))
        assert resp.status == 200
        assert captured["extra"] == {"no_refresh": "1"}
        assert "boot" not in (captured["extra"] or {})

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
                patch.object(tailnet_mobile, "generate_token", side_effect=_fake_mint),
                patch.object(
                    tailnet_mobile, "render_qr_data_uri", return_value="data:image/png;base64,x"
                ),
            ):
                resp = await tailnet_mobile.api_tailnet_mobile_qr(_request(tailnet_host=_HOST))
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


class TestRestrictedSessionRefused:
    """An app-scoped session must not escalate out of its sandbox."""

    _MUTATIONS = [
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
