"""The commands that make one owner request of the running dashboard and print its answer.

``kirocrew token`` mints a sign-in URL (``_token``, with the dashboard health probe it
runs first and the session URLs it prints), ``kirocrew logout`` revokes the sign-in
tokens, and ``kirocrew update approve`` approves an update the dashboard asked for. Each
presents this install's local secret through the peer proof in ``cli_server_peer``.

Split out of ``kiro_crew.cli_server``, which stays the import path and the patch target: every
name here is reached as ``kiro_crew.cli_server.<name>``, and a patch through that facade
reaches this module (``kiro_crew._facade_mirror``). Nothing here imports the facade.
New owner-request commands go here.
"""

from __future__ import annotations

import argparse
import json
import sys
import urllib.error
import urllib.request

from kiro_crew.cli_server_peer import (
    _CLI_LOOPBACK,
    _GatewayPeerRefused,
    _owner_verified_secret_urlopen,
)
from kiro_crew.config import KiroCrewConfig
from kiro_crew.config.loader import read_local_secret
from kiro_crew.dashboard import tailnet_serve
from kiro_crew.dashboard.handlers.core import DASHBOARD_HTML_NOT_FOUND_MARKER
from kiro_crew.dashboard.origin import dashboard_origin, resolve_dashboard_host
from kiro_crew.dashboard.tailnet import is_governance_pinned_off, tailnet_origin
from kiro_crew.dashboard.token_auth import parse_duration
from kiro_crew.loopback_http import loopback_urlopen
from kiro_crew.port_resolution import resolve_client_port
from kiro_crew.preflight import run_preflight_checks


def _probe_dashboard_health(port: int) -> None:
    """Warn on stderr if the gateway is serving a stale dashboard.

    Best-effort: a cookieless GET / checks the response body for the
    "Dashboard HTML not found" marker that a stale gateway serves when its
    static assets have been pruned (e.g. by an update). If detected, a warning
    is printed to stderr so callers know the token won't yield a working
    dashboard. Network errors are silently ignored.
    """
    try:
        req = urllib.request.Request(f"http://{_CLI_LOOPBACK}:{port}/", method="GET")
        with loopback_urlopen(req, timeout=2) as resp:  # nosemgrep
            body = resp.read(8192).decode("utf-8", errors="replace")
            if DASHBOARD_HTML_NOT_FOUND_MARKER.lower() in body.lower():
                print(
                    "⚠️  Warning: gateway is serving a stale dashboard "
                    "(assets missing — likely an update pruned the "
                    "running install). Restart the gateway to fix.",
                    file=sys.stderr,
                )
    except Exception:
        pass


# How long the token command waits for the gateway to answer. A gateway whose
# event loop is starved by a CPU-bound thread can take 12-14 s to answer while
# still being healthy.
_TOKEN_REQUEST_TIMEOUT = 20.0

# Second line of the "could not reach" failure: what to run so a gateway listens
# on this host. Both spellings are given because they are different remedies --
# `kirocrew gateway` runs one in the foreground for this login, `kirocrew
# service install` registers one that survives logout and reboot, which is what
# a host that is only ever reached remotely needs.
_GATEWAY_NOT_LISTENING_HINT = (
    "   No gateway is listening on this host. Start one with `kirocrew gateway`, "
    "or install it as a service that survives logout with `kirocrew service install`."
)


def _token(args: argparse.Namespace) -> None:
    """Print a dashboard URL with a fresh auth token.

    Diagnostics discipline: **stdout carries only the URL(s)**; every failure
    reason goes to **stderr**. stdout here is a parsed machine interface — the
    remote-mint path (:func:`kiro_crew.instances.token_mint.mint_remote_token`)
    runs this over SSH and regex-extracts the JWT from stdout, so mixing error
    prose into stdout both violates the Unix convention and hides the reason
    from any caller that only captures stderr (which is how a failed remote
    mint surfaces as a useless ``<no stderr>``).
    """
    # Seam-supplied pre-launch checks (CPP IdentityProvider seam) — e.g. a
    # companion SSO-session freshness prompt before minting a token. Public
    # default = no checks; see kiro_crew.preflight.
    run_preflight_checks()

    ttl = parse_duration(args.ttl)
    if ttl is None:
        print(f"❌ Invalid TTL: {args.ttl} (use e.g. 1h, 30m)", file=sys.stderr)
        sys.exit(1)

    port = resolve_client_port(args.port)
    # /api/token/local mints owner credentials, so the secret must reach only
    # this install's own gateway. The authoritative proof is bound to the live
    # connection in _owner_verified_secret_urlopen (_connected_peer_verdict on
    # TCP, SO_PEERCRED on the unix socket): it reads the exact kernel 4-tuple to
    # name the peer, confirms it is this account's recorded gateway, writes the
    # secret only on a positive MATCH, and audits allowed/denied itself. No
    # pre-flight port snapshot precedes it -- that check was both weaker (a port
    # snapshot a foreign listener can defeat on restart) and a second, redundant
    # way for a legitimate sign-in to fail.
    secret = read_local_secret(port, dial_host=_CLI_LOOPBACK)
    if not secret:
        print("❌ Gateway not running — start it with: kirocrew gateway", file=sys.stderr)
        sys.exit(1)

    # /api/token/local MINTS owner tokens, so the request below carries a
    # credential a foreign local listener must never receive, and it goes out
    # over a transport that authenticates the CONNECTED PEER of the actual send
    # connection (see _owner_verified_secret_urlopen): the unix socket kernel-
    # verifies the peer at connect time (SO_PEERCRED + recorded gateway), and
    # where there is no unix socket -- native Windows or a socket-degraded POSIX
    # host -- the send goes over loopback TCP whose connected-peer verdict reads
    # the kernel 4-tuple to prove the far end is this install's recorded gateway
    # owned by this account. Either way the proof is bound to the connection the
    # secret rides, so a port-rebind cannot win a check-to-connect gap; an
    # unprovable peer is refused (fail closed) rather than sent to. So no pre-
    # send gate is needed here; the live connected-peer verdict is the single
    # authority.
    url = f"http://{_CLI_LOOPBACK}:{port}/api/token/local?ttl={args.ttl}"
    epp = getattr(args, "embed_parent_port", None)
    if epp:
        url += f"&embed_parent_port={int(epp)}"
    req = urllib.request.Request(url, headers={"X-Local-Secret": secret})
    try:
        with _owner_verified_secret_urlopen(
            req, port, timeout=_TOKEN_REQUEST_TIMEOUT, operation="gateway_token"
        ) as resp:
            data = json.loads(resp.read())
            token = data.get("token", "")
    except _GatewayPeerRefused as exc:
        # The gateway is live but its connection-bound ownership proof refused
        # this peer -- report the refusal, not "could not reach gateway".
        # _GatewayPeerRefused is NOT an OSError, so it reaches here unwrapped.
        print(f"❌ Gateway ownership could not be verified on port {port}: {exc}", file=sys.stderr)
        sys.exit(1)
    except urllib.error.HTTPError as exc:
        # The gateway answered; its body names the reason (for example the
        # host-provenance refusal on /api/token/local). Reporting that as
        # "could not reach gateway" sends the operator to the wrong remedy.
        try:
            detail = str(json.loads(exc.read().decode("utf-8", "replace")).get("error") or "")
        except Exception:
            detail = ""
        print(
            f"❌ Gateway refused the token request (HTTP {exc.code}): {detail or exc.reason}",
            file=sys.stderr,
        )
        sys.exit(1)
    except Exception as exc:
        from kiro_crew.loopback_http import deadline_expired

        if deadline_expired(exc):
            # Something is listening and took the request, so the gateway is
            # alive. Saying "could not reach" sends the operator to restart a
            # healthy gateway. The line names no other probe command: the
            # status command's own short timeout reports a busy gateway as down.
            print(
                f"❌ Gateway on port {port} is up but did not answer within "
                f"{_TOKEN_REQUEST_TIMEOUT:g} s (busy). Try again in a moment.",
                file=sys.stderr,
            )
        else:
            # Nothing answered on the port: no gateway is listening. On a
            # host an installer provisioned without ever starting one, this
            # is the first thing the operator sees -- through a remote mint --
            # and the exception text alone names no remedy. The leading
            # phrase is the documented stable classification (cli.md) and
            # stays; the remedy is a second line.
            print(
                f"❌ Could not reach gateway on port {port}: {exc}\n"
                f"{_GATEWAY_NOT_LISTENING_HINT}",
                file=sys.stderr,
            )
        sys.exit(1)

    if not token:
        print("❌ Gateway returned empty token", file=sys.stderr)
        sys.exit(1)
    _probe_dashboard_health(port)

    _emit_session_urls(port, token)


def _emit_session_urls(port: int, token: str) -> None:
    """Print every origin the operator can open this session on.

    Extracted from ``_token`` so the URL set is testable without standing up a
    gateway and minting a real session: the interesting behaviour is which origins
    get a line, and that is pure given the config and the Tailscale lookup.
    """
    # Print the SAME canonical loopback host the gateway uses for its auto-open
    # and !dashboard links. resolve_dashboard_host() returns "localhost" for the
    # loopback case — it resolves in every browser and through SSH tunnels (unlike
    # *.localhost names, which Safari / the macOS resolver do not map). Emitting a
    # host the gateway does NOT serve on would land the browser on a different
    # origin, splitting the SPA's per-origin localStorage so all dashboard
    # settings appear reset. Keeping the host consistent avoids that.
    host = resolve_dashboard_host(local_only=True)
    print(f"http://{host}:{port}?token={token}")
    cfg = KiroCrewConfig.load()
    origin = dashboard_origin(cfg.dashboard.url)
    if origin and "localhost" not in origin:
        print()
        print(f"{origin}/?token={token}")

    # The tailnet origin, when one is trusted. Without this the flow dead-ends: the
    # gateway derives `https://<MagicDNS name>` itself precisely so the operator does
    # NOT have to hand-write dashboard.url, but that means the loop above has nothing
    # to print for it -- and the URL `tailnet up` shows carries no session, so a phone
    # opening it lands on a login it cannot complete. The operator was left splicing a
    # query string onto a hostname by hand, or setting the very config key the feature
    # exists to avoid.
    #
    # Gated on the setting, not attempted unconditionally: `tailnet_origin()` shells out
    # to the Tailscale CLI with a multi-second timeout, and `kirocrew to`+`ken` is a
    # foreground command an operator runs constantly.
    if cfg.dashboard.tailscale.enabled:
        # The stored flag is not the last word: an enterprise ceiling can pin
        # `capabilities.tailnet_origin` off, and then the gateway derives no tailnet
        # origin at startup no matter what the config says -- so a URL printed here
        # would answer 403. Checking the pin also avoids executing the Tailscale CLI on
        # a host where policy has already settled the question.
        #
        # Called WITHOUT `audit_tool` on purpose. That argument routes the decision
        # through the audited seam, which appends an HMAC-chained SEL record; the
        # helper's own contract reserves it for ENFORCEMENT call sites, and this is a
        # read-shaped question ("would such a URL work?") on a foreground command an
        # operator runs constantly. `tailnet status` reads it the same way.
        if is_governance_pinned_off():
            print()
            print(
                "⚠️  dashboard.tailscale.enabled is on, but your administrator has "
                "pinned tailnet access off (capabilities.tailnet_origin), so the "
                "gateway will not trust a tailnet origin and no tailnet URL is "
                "printed.",
                file=sys.stderr,
            )
            return
        tailnet_url = tailnet_origin()
        if tailnet_url and tailnet_url != origin:
            # Ownership of the 443/ mount, not just "a tailnet name exists". The URL
            # carries a bearer session, so printing it when something ELSE is serving
            # that name hands the operator a link that delivers their token to a
            # foreign service, which can then replay it against the dashboard.
            #
            # That state is reachable, not theoretical: `tailnet up` deliberately
            # REFUSES to overwrite a foreign 443/ handler, so a host can sit with the
            # trust flag on and a resolvable name while the mount belongs to something
            # unrelated. The flag and the name were never evidence of ownership.
            #
            # `published` is tri-state and only True is safe: False means nothing is
            # serving, None means the serve config could not be read -- and unknown
            # ownership is exactly the risk, so it fails closed.
            state = tailnet_serve.serve_state(port)
            if state.published is True:
                print()
                print(f"{tailnet_url}/?token={token}")
            else:
                print()
                print(
                    "⚠️  Not printing a tailnet URL: this dashboard is not verified to "
                    f"be the service published at {tailnet_url} "
                    f"({state.detail}). The URL carries a session, so handing it out "
                    "while another service holds that name would leak it. Run "
                    "`kirocrew tailnet up` to publish this dashboard, then re-run.",
                    file=sys.stderr,
                )
        elif not tailnet_url:
            # Say why rather than printing nothing: from the operator's side "the
            # tailnet line is missing" and "my tailnet name does not resolve" look
            # identical, and the second one also means the gateway trusted no tailnet
            # origin at startup -- so the dashboard would answer 403 there anyway.
            print()
            print(
                "⚠️  dashboard.tailscale.enabled is on, but no tailnet name resolves "
                "right now, so there is no tailnet URL to hand out (and the gateway "
                "will not trust one either). Check `tailscale status`.",
                file=sys.stderr,
            )


def _logout(port: int) -> None:
    """Revoke all dashboard sessions by calling the gateway's /api/logout endpoint."""
    # /api/logout acts on owner authority, so the secret must reach only this
    # install's own gateway. The authoritative proof is bound to the live
    # connection in _owner_verified_secret_urlopen (_connected_peer_verdict on
    # TCP, SO_PEERCRED on the unix socket), which writes the secret only on a
    # positive MATCH and audits allowed/denied itself. No pre-flight port
    # snapshot precedes it -- that check was weaker and a second redundant way
    # for a legitimate sign-in to fail.
    secret = read_local_secret(port, dial_host=_CLI_LOOPBACK)
    if not secret:
        print("❌ Gateway not running — start it with: kirocrew gateway")
        sys.exit(1)

    url = f"http://{_CLI_LOOPBACK}:{port}/api/logout"
    req = urllib.request.Request(
        url,
        method="POST",
        headers={"X-Local-Secret": secret, "Content-Type": "application/json"},
        data=b"{}",
    )
    try:
        with _owner_verified_secret_urlopen(
            req, port, timeout=5, operation="gateway_logout"
        ) as resp:
            data = json.loads(resp.read())
            if data.get("ok"):
                print("✅ All dashboard sessions revoked.")
            else:
                print(f"❌ Failed to revoke sessions: {data.get('error', 'unknown error')}")
                sys.exit(1)
    except _GatewayPeerRefused as e:
        # The gateway is live but its connection-bound ownership proof refused
        # this peer. _GatewayPeerRefused is NOT an OSError, so it reaches here
        # unwrapped instead of being folded into the URLError arm below and
        # mislabelled "gateway not running" against a live gateway.
        print(f"❌ Failed to revoke sessions: gateway ownership not verified: {e}")
        sys.exit(1)
    except urllib.error.HTTPError as e:
        print(f"❌ Failed to revoke sessions: HTTP {e.code}")
        sys.exit(1)
    except (urllib.error.URLError, OSError) as e:
        # A timeout means the gateway took the request and is still working, so
        # it is up but busy, not gone -- saying "not running" would send the
        # operator to restart a healthy gateway. A refused connection keeps the
        # not-running remedy.
        from kiro_crew.loopback_http import deadline_expired

        if deadline_expired(e):
            print(
                f"❌ Gateway on port {port} is up but did not answer in time "
                "(busy) — try again in a moment."
            )
        else:
            print("❌ Gateway not running — start it with: kirocrew gateway")
        sys.exit(1)


def _update_approve() -> None:
    """Approve a pending in-app update armed from the dashboard (RFC OQ7).

    The proof of host identity is READING THE NONCE FILE: it lives in the data
    home with owner-only permissions, so presenting its nonce back to the
    gateway demonstrates filesystem access as the gateway's own user — the
    step a remote dashboard bearer cannot perform. The gateway then runs the
    shadow apply itself and restarts; progress lands on the dashboard's
    update overlay.
    """
    from kiro_crew.platform.update_capability import MANAGED_BY_ELECTRON, derive_capability
    from kiro_crew.platform.update_stepup import read_pending

    # A packaged desktop install has no host-side approval: the app's own
    # updater owns the bytes, and the human click in Settings › About is the
    # approval. Saying so and exiting cleanly beats hunting for a nonce that
    # this shape never writes.
    if derive_capability().managed_by == MANAGED_BY_ELECTRON:
        print("ℹ️  This is a packaged desktop install.")
        print("   Approve the update in the app: Settings › About › Install & restart.")
        return

    print("👻 Approving the pending in-app update…\n")
    # Default read: never writes. This runs in the CLI process, outside the
    # gateway's nonce mutex — expiry cleanup here could race a gateway
    # re-arm and delete a fresh request. Cleanup is the gateway's job.
    pending = read_pending()
    if pending is None:
        print("❌ No armed update request (it may have expired).")
        print("   Arm one from the dashboard's About panel first, then re-run this.")
        sys.exit(1)
    print(f"  📦 v{pending.version} ({pending.channel} channel), expires in {pending.expires_in}s")

    port = resolve_client_port(None)
    url = f"http://127.0.0.1:{port}/api/update/approve"
    payload = json.dumps({"nonce": pending.nonce}).encode("utf-8")
    headers = {"Content-Type": "application/json"}
    # The local secret authenticates this CLI to a token-auth-enabled gateway.
    # X-Internal-Secret is the header the middleware's internal-route branch
    # validates (X-Local-Secret is a different, route-specific mechanism used
    # by /api/token/local). Reading the secret is itself host-local evidence,
    # the same class as the nonce file. An absent secret still works on a
    # default loopback install where no token auth runs.
    secret = read_local_secret(port, dial_host="127.0.0.1")
    if secret:
        headers["X-Internal-Secret"] = secret
    req = urllib.request.Request(url, data=payload, headers=headers, method="POST")
    try:
        # The nonce + local secret must reach only a listener whose CONNECTED
        # PEER the transport can authenticate: the peer-verified unix socket
        # (SO_PEERCRED + recorded gateway) where it exists, else loopback TCP
        # whose connected-peer verdict reads the kernel 4-tuple to prove the far
        # end is this install's recorded gateway owned by this account (native
        # Windows / socket-degraded POSIX). Either way the proof is bound to the
        # send connection, so a port-rebind cannot win a check-to-connect gap;
        # an unprovable peer is refused rather than handed both credentials.
        # Shares the one send helper with the token-minting path. See
        # _owner_verified_secret_urlopen.
        with _owner_verified_secret_urlopen(
            req, port, timeout=15, operation="gateway_update_approve"
        ) as resp:
            body = json.loads(resp.read())
    except _GatewayPeerRefused as e:
        # The gateway is live but its connection-bound ownership proof refused
        # this peer. _GatewayPeerRefused is NOT an OSError, so it reaches here
        # unwrapped instead of being mislabelled a transport failure.
        print(f"❌ Gateway ownership could not be verified on port {port}: {e}")
        sys.exit(1)
    except urllib.error.HTTPError as e:
        try:
            detail = json.loads(e.read()).get("error", "")
        except Exception:
            detail = ""
        print(
            f"❌ Gateway refused the approval (HTTP {e.code})" + (f": {detail}" if detail else "")
        )
        sys.exit(1)
    except (urllib.error.URLError, OSError) as e:
        # A timeout means the gateway took the request and is still working, so
        # it is up but busy, not gone. A refused connection keeps the
        # not-running remedy. The request carries a single-use nonce the gateway
        # may already have consumed, so the busy line does not invite a retry
        # (a re-run would read the nonce as gone and look like a stale arm).
        from kiro_crew.loopback_http import deadline_expired

        if deadline_expired(e):
            print(
                f"❌ Gateway on port {port} is up but busy (did not answer); it may "
                "have taken the request — check the dashboard before re-running."
            )
        else:
            print("❌ Gateway is not running — start it, or update directly with: kirocrew update")
        sys.exit(1)
    print(f"\n✅ Approved. The gateway is applying v{body.get('version', pending.version)}")
    print("   and will restart itself; watch progress in the dashboard.")
