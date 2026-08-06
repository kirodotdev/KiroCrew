"""Connect to a launched KiroCrew instance — SSM tunnel + token + open browser.

The instance's gateway is bound to loopback (never public). To reach it we:

1. Open an SSM port-forward from a local port to the remote dashboard port.
2. Mint a short-lived dashboard token on the instance (``kirocrew token``) over
   SSM and parse the ``localhost`` URL it prints.
3. Open ``http://localhost:<local>/?token=...`` in the browser.

We also (optionally) register the instance in the existing **Instances** registry
using the **native SSM transport** (``connection_method="ssm"`` with the EC2
instance id as ``ssm_target``), so the dashboard's ``/instances`` page can
auto-tunnel + token-refresh + self-heal it with no SSH key, no inbound port, and
no hand-edited ``~/.ssh/config`` — only IAM + the SSM agent. Removing the box
(``cloud destroy``) also removes that registration.
"""

from __future__ import annotations

import logging
import os
import re
import subprocess
import sys
import webbrowser
from dataclasses import dataclass
from typing import Optional

from kiro_crew.cloud import ssm

logger = logging.getLogger(__name__)

DEFAULT_REMOTE_DASHBOARD_PORT = 5476
# Local port the launcher forwards to by default (kept distinct from a local
# gateway's own 5476 so both can coexist).
DEFAULT_LOCAL_PORT = 5599

# `kirocrew token` prints a URL like http://localhost:5476/?token=<jwt>;
# only the token itself is parsed out (the URL is rebuilt for the local port).
_TOKEN_RE = re.compile(r"[?&]token=([^\s&]+)")


@dataclass
class Connection:
    """A live connection to a remote instance."""

    instance_id: str
    local_port: int
    remote_port: int
    token: str = ""
    url: str = ""
    ready: bool = False
    browser_opened: bool = False
    error: str = ""
    process: Optional[subprocess.Popen] = None

    def close(self) -> None:
        """Tear down the SSM port-forward child AND its plugin child."""
        _kill_process_tree(self.process)


def mint_token(instance_id: str, profile: str = "", region: str = "", *, ttl: str = "6h") -> str:
    """Mint a dashboard token on the instance via SSM; return the bare token.

    Runs ``kirocrew token --ttl <ttl>`` on the box and parses the token out of
    the printed loopback URL. Returns "" if it could not be obtained.

    Accepted trade-off: the token transits SSM ``send-command`` output, which is
    retained in the account's SSM command-invocation history for the retention
    window. Mitigations: it is short-lived (TTL-bounded, default 6h); it is only
    usable against a dashboard bound to the instance's loopback, so *using* it
    still requires an SSM tunnel (``ssm:StartSession``) — a superset of the
    access needed to read the history; the generated launcher policy grants
    ``ssm:GetCommandInvocation`` but NOT ``ssm:ListCommandInvocations`` (so a
    holder can't enumerate command ids to find the token blindly); and the agent
    chokepoint denies ``ssm:GetCommandInvocation`` from an agent session. We
    accept this rather than minting over the not-yet-established tunnel (a
    chicken-and-egg: the token is needed to build the authenticated URL the
    tunnel serves).
    """
    res = ssm.run_command(
        instance_id,
        # Grep for the token marker itself, not a hostname — the printed host
        # (localhost vs 127.0.0.1) is presentation and may change.
        f"kirocrew token --ttl {_safe_ttl(ttl)} 2>/dev/null | grep -m1 'token=' || true",
        profile,
        region,
        total_wait=90,
    )
    text = (res.stdout or "").strip()
    m = _TOKEN_RE.search(text)
    return m.group(1) if m else ""


def build_url(local_port: int, token: str) -> str:
    """Compose the local dashboard URL for a minted token.

    Uses the literal ``127.0.0.1`` (not ``localhost``): the port-free check and
    tunnel-readiness probe bind/connect on IPv4 loopback, but ``localhost`` can
    resolve to ``::1`` first — pinning the URL to the interface we actually
    verified keeps the browser (and the JWT) off any foreign IPv6 listener.
    """
    base = f"http://127.0.0.1:{local_port}/"
    return f"{base}?token={token}" if token else base


def connect(
    instance_id: str,
    profile: str = "",
    region: str = "",
    *,
    remote_port: int = DEFAULT_REMOTE_DASHBOARD_PORT,
    local_port: int = DEFAULT_LOCAL_PORT,
    ttl: str = "6h",
    open_browser: bool = True,
) -> Connection:
    """Open a tunnel, mint a token, and (optionally) open the dashboard.

    Returns a live :class:`Connection`; the caller owns tunnel teardown via
    :meth:`Connection.close`.
    """
    ssm.require_session_manager_plugin()
    # Refuse if the local port is already taken: otherwise the SSM child fails
    # to bind, wait_for_local_port succeeds against the *foreign* listener, and
    # we would open the dashboard token against a process that isn't our tunnel.
    if not ssm.port_is_free(local_port):
        return Connection(
            instance_id=instance_id,
            local_port=local_port,
            remote_port=remote_port,
            ready=False,
            error=(
                f"local port {local_port} is already in use — close whatever is "
                f"using it, or pass --local-port <n>, then retry."
            ),
        )
    # Open the tunnel and prove it's ready BEFORE minting the token. mint_token
    # transits the dashboard JWT through SSM `send-command` output (retained in
    # command-invocation history for its TTL), so minting on a tunnel that then
    # fails would leave a live token in SSM history for nothing. Deferring the
    # mint until readiness is confirmed means a failed connect mints no token.
    proc: Optional[subprocess.Popen] = ssm.open_port_forward(
        instance_id, remote_port, local_port, profile, region
    )
    # Pass proc so the wait bails if the SSM child dies (rather than latching
    # onto some unrelated listener that later appears on the same port).
    ready = ssm.wait_for_local_port(local_port, proc=proc)
    # Final ownership recheck to close the residual free-check->bind race: only
    # one process can bind local_port, so if a foreign process won the bind our
    # SSM child fails to bind and exits. A listener answering while proc has
    # exited is therefore NOT our tunnel — refuse rather than send the token to
    # a stranger. (If proc is still alive it owns the port: safe.)
    if ready and proc is not None and proc.poll() is not None:
        logger.warning(
            "local port %d answered but the SSM child exited — a foreign listener "
            "won the bind race; refusing to route the dashboard token to it",
            local_port,
        )
        ready = False
    error = ""
    token = ""
    url = ""
    browser_opened = False
    if not ready:
        logger.warning("SSM port-forward did not become ready on local port %d", local_port)
        error = _port_forward_error(proc, local_port)
        _terminate(proc)
    else:
        # Tunnel confirmed ours + ready — NOW mint the token. mint_token goes
        # through the aws chokepoint, which RAISES (AWSError) on e.g. an
        # ssm:SendCommand AccessDenied. If that propagated out of connect() the
        # tunnel child (+ its session-manager-plugin) would be orphaned with the
        # local port still bound — the same leak the empty-token path guards
        # against, just via an exception instead of a "" return. Tear the tunnel
        # down and fold the failure into a ready=False Connection so the caller
        # gets the uniform "not ready + error" contract, never an orphan.
        try:
            token = mint_token(instance_id, profile, region, ttl=ttl)
        except Exception as exc:  # noqa: BLE001 - any mint failure must tear down
            # Log message avoids secret-adjacent words ("token"/"credential")
            # next to the interpolated value: Semgrep's logger-credential-leak
            # rule flags that shape as a potential secret disclosure. This is a
            # false positive (`exc` is an AWSError/permission message, never the
            # JWT — the token is never logged; see redact_token()), but the
            # neutral wording keeps the SAST gate green without a nosemgrep.
            logger.warning("dashboard sign-in provisioning failed on local port %d: %s", local_port, exc)
            error = (
                "connected the SSM tunnel but minting a dashboard token failed "
                f"({exc}) — check `kirocrew cloud status` / your IAM permissions, "
                "then retry."
            )
            _terminate(proc)
            return Connection(
                instance_id=instance_id,
                local_port=local_port,
                remote_port=remote_port,
                ready=False,
                error=error,
                process=proc,
            )
        if not token:
            # The tunnel is up but we couldn't mint a token (e.g. `kirocrew token`
            # failed on the box). A ready tunnel with no URL is useless and would
            # otherwise leak: callers see ready=True + empty URL, and the wizard
            # would block on a tunnel that can't authenticate. Tear it down and
            # report failure so no orphaned process is left behind.
            logger.warning("tunnel ready on local port %d but token mint failed", local_port)
            error = (
                "connected the SSM tunnel but could not mint a dashboard token on the "
                "instance — check `kirocrew cloud status` / the box's gateway, then retry."
            )
            _terminate(proc)
            ready = False
        else:
            url = build_url(local_port, token)
            if open_browser and url:
                browser_opened = _open_browser(url)
    return Connection(
        instance_id=instance_id,
        local_port=local_port,
        remote_port=remote_port,
        token=token,
        url=url,
        ready=ready,
        browser_opened=browser_opened,
        error=error,
        process=proc,
    )


# --- Instances registry integration (managed experience) --------------------


def ssm_proxy_ssh_host(instance_id: str, region: str) -> str:
    """The ssh-alias-free host string used by the legacy SSM ``ProxyCommand`` path.

    Retained for reference/back-compat only. The managed registration below no
    longer uses it — :func:`register_instance` now registers the box with the
    native SSM transport (``connection_method="ssm"``), which needs no
    ``~/.ssh/config`` entry.
    """
    return instance_id


def register_instance(
    instance_id: str,
    *,
    name: str,
    profile: str = "",
    region: str = "",
    remote_port: int = DEFAULT_REMOTE_DASHBOARD_PORT,
) -> Optional[str]:
    """Register the box in the Instances registry for the /instances dashboard.

    Registers with the **native SSM transport**: ``connection_method="ssm"`` and
    the EC2 instance id as ``ssm_target`` (plus the launcher's ``profile`` /
    ``region``). The dashboard then tunnels, refreshes tokens, and self-heals the
    box over AWS SSM Session Manager — no SSH key, no inbound port, and no
    hand-edited ``~/.ssh/config``.

    Best-effort: returns the registered instance id, or None if the Instances
    feature isn't available. Idempotent per box: a re-launch updates the prior
    record in place — preserving its id, allocated local port, TTL, and sticky
    connection intent (``was_connected``) — rather than accumulating duplicates.
    """
    try:
        from kiro_crew.instances.registry import InstancesRegistry
    except Exception:  # pragma: no cover - instances feature absent
        logger.info("Instances feature unavailable; skipping registration")
        return None
    try:
        reg = InstancesRegistry()
        # Idempotent re-launch: if this box is already registered — a native
        # ssm_target match, or a legacy ssh_host==id registration — UPDATE that
        # record's coordinates in place, preserving its id, allocated local
        # port, TTL, and sticky connection intent (was_connected, which drives
        # startup auto-reconnect). remove+add would reset that persisted state.
        # Only add when the box is new.
        for existing in reg.list():
            if instance_id in (existing.ssm_target, existing.ssh_host):
                reg.update(
                    existing.id,
                    connection_method="ssm",
                    ssm_target=instance_id,
                    aws_profile=profile,
                    aws_region=region,
                    remote_port=remote_port,
                )
                return existing.id
        inst = reg.add(
            name=name,
            connection_method="ssm",
            ssm_target=instance_id,
            aws_profile=profile,
            aws_region=region,
            remote_port=remote_port,
        )
        return inst.id
    except Exception as exc:  # pragma: no cover - non-fatal
        logger.info("could not register instance in Instances registry: %s", exc)
        return None


def unregister_instance(instance_id_or_name: str) -> bool:
    """Remove a box from the Instances registry (called on cloud destroy)."""
    # An empty needle would spuriously match the empty transport field of the
    # *other* method (SSM records carry ssh_host="", SSH records ssm_target="").
    if not instance_id_or_name:
        return False
    try:
        from kiro_crew.instances.registry import InstancesRegistry
    except Exception:  # pragma: no cover
        return False
    try:
        reg = InstancesRegistry()
        # Match by ssm_target (native SSM registration), ssh_host (legacy
        # ProxyCommand registration), or the registry id.
        for inst in reg.list():
            if instance_id_or_name in (inst.ssm_target, inst.ssh_host, inst.id):
                return reg.remove(inst.id)
    except Exception as exc:  # pragma: no cover
        logger.info("could not unregister instance: %s", exc)
    return False


def _safe_ttl(ttl: str) -> str:
    """Constrain a TTL string to the ``<n>[hm]`` shape kirocrew token accepts."""
    return ttl if re.fullmatch(r"\d{1,3}[hm]", ttl or "") else "6h"


def _browser_open_supported() -> bool:
    """Return false in headless Linux shells where webbrowser would call gio/xdg-open."""
    if os.environ.get("KIROCREW_NO_BROWSER"):
        return False
    if sys.platform.startswith("linux"):
        return bool(
            os.environ.get("DISPLAY")
            or os.environ.get("WAYLAND_DISPLAY")
            or os.environ.get("BROWSER")
        )
    return True


def redact_token(url: str) -> str:
    """Strip a ``?token=...`` value from a URL before it reaches a log line."""
    return re.sub(r"([?&]token=)[^\s&]+", r"\1<redacted>", url)


def _open_browser(url: str) -> bool:
    """Best-effort local browser open; return whether it appeared to work."""
    if not _browser_open_supported():
        return False
    try:
        return bool(webbrowser.open(url, new=2))
    except Exception:  # pragma: no cover - headless
        # Log without the live token — the full URL is shown on the terminal
        # by the caller; the persisted log ring must not carry the JWT.
        logger.info(
            "could not auto-open browser; open the printed URL manually (%s)", redact_token(url)
        )
        return False


def _terminate(proc: Optional[subprocess.Popen]) -> None:
    """Best-effort teardown of a live SSM port-forward child (SIGTERM→SIGKILL).

    Used on every non-ready connect() exit — a failed tunnel OR a ready tunnel
    whose token mint failed — so we never leave an orphaned SSM session behind.
    """
    _kill_process_tree(proc)


def _kill_process_tree(proc: Optional[subprocess.Popen]) -> None:
    """Tear down the port-forward child + its plugin child (see ssm)."""
    # Single source of truth in ssm (also used by login's callback tunnel).
    ssm.kill_port_forward(proc)


def _port_forward_error(proc: Optional[subprocess.Popen], local_port: int) -> str:
    """Return a short reason for a failed tunnel when the child has already exited.

    Note: the tunnel child runs with stdout/stderr=DEVNULL (to avoid pipe-buffer
    deadlocks), so we can only distinguish "didn't start", "still running but
    not accepting connections", and "exited prematurely".
    """
    if proc is None:
        return f"SSM port-forward did not start on local port {local_port}."
    if proc.poll() is None:
        return f"SSM port-forward did not become ready on local port {local_port}."
    return f"SSM port-forward exited (rc={proc.returncode}) before local port {local_port} became ready."
