"""Read-only interface to the local Tailscale daemon.

Answers one question for now: *what MagicDNS name does this machine have on its
tailnet?* — so the dashboard can accept its own tailnet origin without the
operator hand-writing ``dashboard.url``. RFC:
``docs/request-for-change/rfc-tailnet-dashboard-access.md`` §4.

Two properties are load-bearing and neither is optional:

**Nothing here raises.** A missing binary, a stopped daemon, a timeout, a
non-zero exit, malformed JSON, an unexpected schema — every one returns ``None``.
The dashboard must start on a host that has never heard of Tailscale, so this
module is a pure enrichment: it either contributes a name or contributes nothing.

**The name is validated before it is returned.** It arrives from a subprocess and
its destination is the CSRF origin allowlist and the DNS-rebinding ``Host``
barrier, so an unvalidated value would be an origin-injection primitive. See
:func:`_valid_magicdns_name`: structure is checked as a strict allowlist, and the
name must additionally sit under the tailnet's own MagicDNS suffix *as the daemon
reports it* — not a suffix hardcoded here, because upstream documents the suffix
as tailnet-specific (its own example is ``userfoo.tailscale.net``, not
``ts.net``).
"""

from __future__ import annotations

import asyncio
import ipaddress
import json
import logging
import os
import re
import subprocess
import threading
import time
from collections import OrderedDict
from contextlib import suppress
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Any, Callable, Mapping

from kiro_crew import github_runner
from kiro_crew.dashboard.urls import is_loopback
from kiro_crew.executors import subprocess_executor
from kiro_crew.platform.governance_profiles import (
    GOVERNANCE_ERROR_REASON,
    governance_permits,
    vet_and_audit,
)
from kiro_crew.platform_compat import IS_POSIX
from kiro_crew.sandbox import scrub_env

if TYPE_CHECKING:
    from aiohttp import web

logger = logging.getLogger(__name__)

#: Hard ceiling on a daemon call. Startup path, so this is latency the user
#: waits through — it must be short, and it must be a real timeout rather than a
#: hope, because `tailscale status` blocks while the daemon is starting up.
_CLI_TIMEOUT_SECS = 3.0

#: Background recovery stays cheap even when an explicitly enabled daemon is
#: unavailable for hours.  The first retry covers the common Windows service /
#: daemon boot race quickly; the ceiling limits steady-state subprocess work.
_ORIGIN_RECOVERY_INITIAL_SECS = 2.0
_ORIGIN_RECOVERY_MAX_SECS = 60.0
_ORIGIN_RUNTIME_STATE_KEY = "tailnet_origin_state"

#: Where the CLI is accepted from — **vetted absolute paths only, never ``PATH``**.
#: A ``PATH`` lookup would make the binary itself attacker-selectable: an agent
#: that can write into any writable ``PATH`` entry (``~/.local/bin`` is on PATH on
#: a normal dev box) could plant a ``tailscale`` executable, and the next gateway
#: start with this feature enabled would execute it. The arguments were never
#: agent-influenced, but the *binary* was — so resolution is pinned to the
#: locations the official packages install into. Most need root to write, but
#: not all (Homebrew chowns its prefix to the console user), so
#: :func:`_cli_path` additionally refuses any candidate the gateway user can
#: write:
#:
#: * ``/usr/bin`` — Linux distro packages
#: * ``/usr/local/bin`` — Linux tarball, macOS Homebrew on Intel
#: * ``/opt/homebrew/bin`` — macOS Homebrew on Apple silicon
#: * the app bundle — the macOS app ships the binary inside and does not always
#:   symlink it
#: * ``C:\Program Files\Tailscale`` — the Windows installer
#:
#: A non-standard install is not auto-derived. That is the deliberate trade: it
#: keeps using explicit ``dashboard.url``, which is the path it uses today.
_CLI_CANDIDATE_PATHS = (
    "/usr/bin/tailscale",
    "/usr/local/bin/tailscale",
    "/opt/homebrew/bin/tailscale",
    "/Applications/Tailscale.app/Contents/MacOS/Tailscale",
    r"C:\Program Files\Tailscale\tailscale.exe",
)

#: MagicDNS names are DNS labels joined by dots, all lowercase. Deliberately
#: strict: no scheme, no port, no path, no userinfo, no whitespace, no trailing
#: dot (stripped before the match), no uppercase.
_DNS_LABEL = r"[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?"
_MAGICDNS_RE = re.compile(rf"^{_DNS_LABEL}(?:\.{_DNS_LABEL})+$")


def _cli_path() -> str | None:
    """Locate the ``tailscale`` CLI, or ``None`` if it is not installed.

    Deliberately does **not** consult ``PATH`` — see ``_CLI_CANDIDATE_PATHS``.

    A candidate is additionally refused when its platform's ownership policy
    cannot establish trusted provenance. POSIX keeps the stricter local check
    below; Windows reuses the shared executable validator, which reads the
    binary and parent ACLs because mode bits carry no useful signal there. A
    refusal degrades exactly like a missing binary: the feature contributes
    nothing and the documented ``dashboard.url`` fallback still works.
    """
    # getattr: os.geteuid does not exist on Windows, and tests exercise this
    # path with IS_POSIX patched True on every platform.
    for candidate in _CLI_CANDIDATE_PATHS:
        if not (os.path.isfile(candidate) and os.access(candidate, os.X_OK)):
            continue
        validated = candidate
        if IS_POSIX:
            if not _posix_candidate_trusted(candidate):
                logger.debug(
                    "tailscale CLI at %s is in a location this deployment cannot "
                    "trust; refusing to execute it (planted-binary defence). Use "
                    "an explicit dashboard.url instead.",
                    candidate,
                )
                continue
        else:
            try:
                validated = github_runner.validate_provider_executable(candidate)
            except ValueError as exc:
                logger.debug(
                    "tailscale CLI at %s failed executable trust validation: %s. "
                    "Use an explicit dashboard.url instead.",
                    candidate,
                    exc,
                )
                continue
        return validated
    return None


def _posix_candidate_trusted(candidate: str) -> bool:
    """Whether *candidate* is safe to execute (POSIX planted-binary defence).

    Refused when the binary or its directory is group/world-writable, when the
    gateway user can write either (a file the executing user owns is always
    effectively writable), or — when running as root, for whom every path is
    writable so the access check says nothing — when either is not root-owned.
    A root gateway therefore accepts only distro-style root-owned installs;
    everything refused degrades like a missing binary.
    """
    directory = os.path.dirname(candidate)
    try:
        st_file = os.stat(candidate)
        st_dir = os.stat(directory)
    except OSError:
        return False
    group_world_write = 0o022
    if (st_file.st_mode | st_dir.st_mode) & group_world_write:
        return False
    # getattr: os.geteuid does not exist on Windows, and tests exercise this
    # path with IS_POSIX patched True on every platform.
    euid = getattr(os, "geteuid", lambda: -1)()
    if euid == 0:
        return st_file.st_uid == 0 and st_dir.st_uid == 0
    return not (os.access(candidate, os.W_OK) or os.access(directory, os.W_OK))


def _run_json(args: list[str]) -> Any | None:
    """Run the CLI and parse stdout as JSON. ``None`` on ANY failure.

    Deliberately broad: the caller's contract is "a name or nothing", and every
    failure mode here (no binary, daemon down, timeout, non-zero exit, non-JSON
    output) means the same thing to it. Failures are logged at debug so a host
    without Tailscale does not emit noise on every start.
    """
    return _run_json_detail(args)[0]


def _run_json_detail(args: list[str]) -> tuple[Any | None, bool]:
    """Run the CLI and parse stdout as JSON. ``(parsed, transient)``.

    ``transient`` is ``True`` only when the CLI could not be run or did not
    answer in time (spawn failure, timeout) — the "daemon still starting up"
    class, expected to clear within seconds. A completed run (any exit code,
    any output) and a missing binary are definitive for this host right now.
    The whois cache uses the flag to keep a transient failure on a much
    shorter TTL, so one startup blip does not hold an identity-pinned session
    denied for a full cache window.
    """
    cli = _cli_path()
    if not cli:
        logger.debug("tailscale CLI not found; skipping tailnet origin derivation")
        return None, False
    try:
        proc = subprocess.run(  # noqa: S603 - vetted absolute binary, fixed argv, no shell
            [cli, *args],
            capture_output=True,
            text=True,
            timeout=_CLI_TIMEOUT_SECS,
            check=False,
            # Defence in depth behind the pinned binary above: even a legitimate
            # `tailscale` has no business reading the gateway's credentials out of
            # the inherited environment. Uses the repo's own scrubber rather than a
            # second, narrower allowlist, so this spawn cannot drift away from the
            # policy every other spawn follows — and so it stays cross-OS safe.
            env=scrub_env(),
        )
    except (OSError, subprocess.SubprocessError) as exc:
        logger.debug("tailscale %s failed to run: %s", " ".join(args), exc)
        return None, True
    if proc.returncode != 0:
        logger.debug(
            "tailscale %s exited %d: %s",
            " ".join(args),
            proc.returncode,
            (proc.stderr or "").strip()[:200],
        )
        return None, False
    try:
        return json.loads(proc.stdout or ""), False
    except ValueError as exc:
        logger.debug("tailscale %s produced non-JSON output: %s", " ".join(args), exc)
        return None, False


def _valid_magicdns_name(raw: object, magic_dns_suffix: object) -> str | None:
    """Return *raw* as a trusted MagicDNS name, or ``None`` if it is not one.

    Two independent checks, and they defend different things.

    **Structure** is the injection defense. An allowlist, not a denylist: the
    destination is the CSRF origin set and the ``Host`` barrier, so the question
    is not "does this look dangerous" but "is this provably a bare hostname".
    Rejected — a non-string, empty, over 253 bytes, or anything carrying a
    scheme / port / path / credentials / whitespace / uppercase.

    **Suffix self-consistency** is the "is this actually ours" check. The name
    must sit under the tailnet's own MagicDNS suffix *as reported by the same
    status output* (``CurrentTailnet.MagicDNSSuffix``). Checking against the
    daemon's own answer rather than a hardcoded suffix matters: upstream
    documents the suffix as tailnet-specific and its own example is
    ``userfoo.tailscale.net``, not ``ts.net``, so a hardcoded list would reject
    legitimate tailnets and would rot as Tailscale adds suffixes. It also means a
    self-hosted control plane works without a special case. ``CurrentTailnet`` is
    nil when the node is not connected, which lands here as a missing suffix and
    is refused — no tailnet means no origin to add.
    """
    if not isinstance(raw, str) or not isinstance(magic_dns_suffix, str):
        return None
    name = raw.strip().rstrip(".")
    # Upstream documents MagicDNSSuffix as carrying "no surrounding dots", but
    # normalise rather than trust the shape of a value we did not build.
    suffix = magic_dns_suffix.strip().strip(".").lower()
    if not name or not suffix or len(name) > 253:
        return None
    # Cheap structural rejections before the regex, so the reason is obvious in
    # a debug log rather than a bare "did not match".
    if any(ch in name for ch in "/:@?# \t\r\n\\"):
        return None
    if name != name.lower():
        return None
    # Must be a host UNDER the suffix, not the suffix itself and not a name that
    # merely contains it (`desk.tail.ts.net.evil.com` must not pass).
    if not name.endswith(f".{suffix}"):
        return None
    if not _MAGICDNS_RE.match(name):
        return None
    return name


def self_dns_name() -> str | None:
    """This machine's MagicDNS name on its tailnet, or ``None``.

    ``None`` covers every "not applicable" case as well as every failure:
    Tailscale absent, daemon not running, machine not logged in (``CurrentTailnet``
    is nil), MagicDNS disabled for the tailnet, or a name that does not validate
    against the tailnet's own suffix.
    """
    status = _run_json(["status", "--json"])
    if not isinstance(status, dict):
        return None
    self_node = status.get("Self")
    if not isinstance(self_node, dict):
        return None
    # CurrentTailnet is nil when the node is not connected to a tailnet. The
    # legacy top-level MagicDNSSuffix is upstream-deprecated, so it is only a
    # fallback for an older daemon, never the primary read.
    tailnet = status.get("CurrentTailnet")
    suffix: object = None
    if isinstance(tailnet, dict):
        suffix = tailnet.get("MagicDNSSuffix")
    if not isinstance(suffix, str) or not suffix.strip():
        suffix = status.get("MagicDNSSuffix")
    name = _valid_magicdns_name(self_node.get("DNSName"), suffix)
    if name is None:
        logger.debug("tailscale status returned no usable Self.DNSName for this tailnet")
    return name


def tailnet_origin() -> str | None:
    """The HTTPS origin to trust for this machine's tailnet name, or ``None``.

    No port: ``tailscale serve`` fronts the dashboard on 443, so the browser's
    ``Origin`` carries no port component.
    """
    name = self_dns_name()
    return f"https://{name}" if name else None


#: ``BackendState`` values that mean the daemon is running but this machine is
#: not signed in to a tailnet. Upstream's own enum (``ipn.State``); only the
#: not-signed-in half is listed because that is the one an operator fixes by
#: signing in, and treating an unknown future value as "signed in" would send
#: them to the wrong remedy.
_BACKEND_STATES_NEEDING_LOGIN = frozenset({"NeedsLogin", "NoState", "NeedsMachineAuth"})


@dataclass(frozen=True)
class DaemonProbe:
    """Why this machine does or does not have a usable tailnet name.

    Exists because :func:`self_dns_name` deliberately collapses every failure to
    ``None`` — right for its caller (which only wants "a name or nothing") and
    useless for an onboarding UI, which has to tell the operator WHICH thing to
    go fix. These negatives have different remedies and must not be
    rendered as one "Tailscale not working":

    * ``installed=False`` — install Tailscale.
    * ``reachable=False`` — the daemon is not answering; start it.
    * ``stopped=True`` — the daemon answers but Tailscale is stopped
      (``BackendState "Stopped"``, e.g. after ``tailscale down``); bring it up.
    * ``logged_in=False`` — signed out; sign in.
    * all true but ``name=""`` — signed in, but MagicDNS is off for the tailnet.
    * ``https_enabled=False`` — the tailnet has not granted certificate
      provisioning for this machine's MagicDNS name yet.

    ``peer_count`` / ``peers_online`` describe the OTHER devices on this tailnet,
    and they answer a question no amount of local state can: whether there is a
    phone to reach this dashboard FROM. Publishing succeeds and the QR renders
    perfectly on a tailnet of one, and then the scan fails in the browser with an
    unexplained "cannot connect" — the single most likely way a new operator gets
    stuck, because nothing on this machine is wrong.

    Keeps this module's "nothing here raises" invariant: every failure lands in
    a field, never in an exception.
    """

    name: str
    installed: bool
    reachable: bool
    logged_in: bool
    detail: str
    #: ``BackendState "Stopped"``: the daemon answered but Tailscale is stopped,
    #: so the tailnet is unreachable and nothing can be published or withdrawn.
    #: A separate field rather than a ``_BACKEND_STATES_NEEDING_LOGIN`` entry
    #: because the remedy differs — start Tailscale, not sign in — and folding it
    #: into ``reachable`` would misname the state: the daemon IS answering.
    stopped: bool = False
    #: Login owning this machine, validated from ``Self.UserID`` -> ``User``.
    #: Kept server-side; the status API does not expose it to the renderer.
    login: str = ""
    peer_count: int = 0
    peers_online: int = 0
    #: ``None`` means this older/unexpected status document did not expose the
    #: field. It must not be treated as ``False``: older clients may still be
    #: able to publish, and the authoritative ``tailscale serve`` write will
    #: report any unmet requirement. ``False`` is reserved for an explicit
    #: ``CertDomains`` list that does not contain this host.
    https_enabled: bool | None = None


def _count_peers(status: dict) -> tuple[int, int]:
    """``(peers, online)`` for the tailnet's OTHER devices. Never raises.

    Counted from the status document already parsed by the caller rather than by
    a second daemon call. A malformed or absent ``Peer`` map counts as zero, which
    is the same reading as a tailnet of one — both mean "we cannot show that a
    second device exists", and the advisory that follows is phrased as an absence
    of evidence rather than as a certainty.
    """
    raw = status.get("Peer")
    if not isinstance(raw, dict):
        return 0, 0
    peers = [p for p in raw.values() if isinstance(p, dict)]
    online = sum(1 for p in peers if p.get("Online") is True)
    return len(peers), online


def probe_daemon() -> DaemonProbe:
    """Diagnose the local daemon for the onboarding card. Never raises.

    A LIVE read, unlike the startup value ``GET /api/tailnet/status`` reports.
    The two are deliberately different questions — "what can this machine do
    next" versus "what does the running server already trust" — and conflating
    them is how a resolvable name gets rendered as an origin that is actually in
    the allowlist.
    """
    if _cli_path() is None:
        return DaemonProbe(
            name="",
            installed=False,
            reachable=False,
            logged_in=False,
            detail="Tailscale is not installed in a standard location.",
        )
    status, _transient = _run_json_detail(["status", "--json"])
    if not isinstance(status, dict):
        return DaemonProbe(
            name="",
            installed=True,
            reachable=False,
            logged_in=False,
            detail="Tailscale is installed, but its daemon did not answer.",
        )
    backend_state = status.get("BackendState")
    # "Stopped" is the daemon running with Tailscale down (`tailscale down`), so
    # it passes the needing-login test below — the machine may well still be
    # signed in — while nothing on the tailnet can reach this host and no serve
    # write can take effect. Modeled as its own state, checked first, because a
    # stopped daemon blocks everything the later branches would send the
    # operator to fix.
    if backend_state == "Stopped":
        return DaemonProbe(
            name="",
            installed=True,
            reachable=True,
            logged_in=True,
            detail="Tailscale is stopped, so this machine is not connected to its tailnet.",
            stopped=True,
        )
    logged_in = not (
        isinstance(backend_state, str) and backend_state in _BACKEND_STATES_NEEDING_LOGIN
    )
    if not logged_in:
        return DaemonProbe(
            name="",
            installed=True,
            reachable=True,
            logged_in=False,
            detail="Tailscale is running but this machine is not signed in.",
        )
    peer_count, peers_online = _count_peers(status)
    login = _self_login(status)
    name = self_dns_name() or ""
    if not name:
        return DaemonProbe(
            name="",
            installed=True,
            reachable=True,
            logged_in=True,
            detail=(
                "Signed in, but no MagicDNS name is available for this machine — "
                "MagicDNS may be disabled for this tailnet."
            ),
            login=login,
            peer_count=peer_count,
            peers_online=peers_online,
        )
    # Tailscale's public ``ipnstate.Status`` contract defines CertDomains as the
    # DNS names for which the control plane will help provision TLS certificates.
    # An explicit empty/mismatching list is therefore a reliable first-use HTTPS
    # prerequisite; an absent or malformed field stays unknown so an older CLI is
    # allowed to reach the authoritative Serve attempt instead of being blocked
    # forever by a field it never emitted.
    raw_cert_domains = status.get("CertDomains")
    https_enabled: bool | None = None
    if isinstance(raw_cert_domains, list):
        cert_domains = {
            value.strip().rstrip(".").lower()
            for value in raw_cert_domains
            if isinstance(value, str) and value.strip()
        }
        https_enabled = name in cert_domains
    return DaemonProbe(
        name=name,
        installed=True,
        reachable=True,
        logged_in=True,
        detail="",
        login=login,
        peer_count=peer_count,
        peers_online=peers_online,
        https_enabled=https_enabled,
    )


def is_governance_pinned_off(*, audit_tool: str = "") -> bool:
    """Return whether an enterprise ceiling pins ``capabilities.tailnet_origin`` off.

    A close mirror of ``beacon.is_governance_pinned_off``, deliberately: the two
    answer the same shape of question about the same archetype, and a second,
    subtly-different probe is how two chokepoints on one scope come to disagree.
    The differences from beacon are only the scope name and the audit tool names.

    Pass ``audit_tool`` (a tool name) from an ENFORCEMENT call site so the
    decision routes through the audited ``vet_and_audit`` seam, which writes a
    ``governance_decision`` SEL record for the grant or the denial.
    :func:`resolve_tailnet_host` and both write chokepoints do this, so a
    suppressed derivation and a refused write each leave a forensic record.

    It is deliberately NOT the default. This probe is also a pure READ from
    ``GET /api/tailnet/status``, which the dashboard's tailnet card refetches;
    auditing there would append HMAC-chained SEL rows on mere inspection, at a
    multiple of the one decision per boot that actually governs anything — audit
    the decision that *does* something, not the question.

    The Level-1 POLICY answer, resolved through the standard chokepoint helper so
    this decision comes from the same evaluator as every other governed surface.
    Public because the dashboard card must distinguish "off because the operator
    left the switch off" (flippable) from "off because an administrator pinned it"
    (not flippable, and a config write would be refused).

    FAIL-CLOSED on an evaluation error, for the same asymmetry beacon documents.
    The two dispositions look symmetric and are not:

    * The wrong-DENY costs the operator a convenience: ``tailscale serve`` keeps
      failing the Origin check exactly as it does today with the feature off, and
      an explicit ``dashboard.url`` still works. Nothing is lost that was not
      already the status quo.
    * The wrong-PERMIT **widens the CSRF origin allowlist and the DNS-rebinding
      ``Host`` barrier on a fleet that forbade it** — it grows the set of origins
      the gateway will accept authenticated, state-changing requests from. That is
      a security boundary, not a feature, which puts this with
      ``capabilities.publish`` / ``theme_install`` / ``telemetry``
      (``fail_closed=True``) rather than with the advisory probes.

    ``fail_closed=True`` also makes ``governance_permits`` audit the degrade as a
    critical SEL event, so an operator can see that a ceiling stopped being
    evaluable — precisely the condition under which a silent degrade-to-permit
    would be indefensible.

    Two failure sources are distinguished, because conflating them produces a
    different bug in each direction:

    * A **degrade** (identified by the ``GOVERNANCE_ERROR_REASON`` prefix, not by
      ``rule == "default"`` alone — ``_PERMIT_NOT_GOVERNED`` carries that rule
      too) means no level decided, i.e. the ceiling is unevaluable. Treated as
      pinned, per the fail-closed rationale above.
    * A **profile-layer deny** means the evaluator answered, but from Level 2.
      NOT treated as a pin: ``resolve_active_scope`` returns a synthetic deny-all
      profile (``_deny_all_unloaded:…``) when the profile store is unprimed and
      another thread holds its non-blocking reload lock, so on a host with **no
      policy at all** that transient race would otherwise make the startup
      warning, the 403 and the CLI refusal all blame an administrator who does not
      exist. It arrives as an ordinary ``Decision``, so no ``except`` can catch it
      — which is why this keys on ``layer``, not on ``permitted`` alone. Level-2
      profiles are also per-surface and narrow-only, while this probe runs once at
      gateway startup and carries no session, so a profile denial is not the
      question being asked either way.
    """
    try:
        if audit_tool:
            # The audited seam: evaluate + write the governance_decision SEL row
            # from ONE code path, so this scope's three chokepoints cannot drift
            # apart in audit shape.
            decision = vet_and_audit(
                "capabilities.tailnet_origin",
                "",
                session_key="",
                tool_name=audit_tool,
                log_warning=False,
                fail_closed=True,
            )
        else:
            decision = governance_permits(
                "capabilities.tailnet_origin", "", log_warning=False, fail_closed=True
            )
    except Exception:
        # governance_permits swallows its own errors, so reaching here means the
        # import or the call itself failed — the ceiling is unevaluable, which is
        # the same condition as a degrade. Fail closed for the same reason.
        logger.debug("tailnet governance probe failed; treating as pinned", exc_info=True)
        return True
    if getattr(decision, "permitted", True):
        return False
    if str(getattr(decision, "reason", "")).startswith(GOVERNANCE_ERROR_REASON):
        return True
    return getattr(decision, "layer", "") == "policy"


async def resolve_tailnet_host(enabled: bool) -> str:
    """Async entry point for the startup path: the name, or ``""``.

    Exists so the **blocking subprocess never runs on the event loop**.
    :func:`self_dns_name` shells out with a multi-second timeout, and
    ``tailscale status`` genuinely blocks while the daemon is coming up; running
    that inline would stall every other session and can trip the loop-stall
    watchdog. Offloaded to a thread, and short-circuited before the thread hop
    when the feature is off so a host without Tailscale pays nothing.

    Takes *enabled* as an argument rather than reading config, to keep this
    module free of a config import (and the import cycle that would invite).
    """
    if not enabled:
        return ""
    # Chokepoint (a) — THE ACTION. A ceiling pinning ``capabilities.tailnet_origin``
    # off stops the derivation itself, ahead of the daemon call: nothing is spawned
    # and no origin is contributed, so the pin closes both halves an administrator
    # objects to (running the tailnet CLI, and widening the origin allowlist).
    # Probed in a thread because resolving the ceiling reads the trust-root policy
    # file and the active profile from disk — the same reason the daemon call
    # below is offloaded, and this runs on the startup event loop.
    if await asyncio.to_thread(is_governance_pinned_off, audit_tool="tailnet_origin_resolve"):
        # Deliberately a DIFFERENT warning from the enabled-but-unresolved one
        # below, because the remedy is different and pointing the operator at
        # `tailscale status` would be a wild goose chase: nothing is wrong with
        # their daemon, and no restart or boot-race retry will change the outcome.
        logger.warning(
            "dashboard.tailscale.enabled is on, but capabilities.tailnet_origin is "
            "pinned OFF by your administrator's security policy, so no tailnet "
            "origin was derived and the Tailscale daemon was not consulted. This "
            "setting cannot re-enable it — ask your administrator, or reach the "
            "dashboard through an explicitly configured dashboard.url."
        )
        return ""
    name: str | None = await asyncio.to_thread(self_dns_name)
    if not name:
        # Debug-level silence is right for a host that never opted in, but the
        # operator who set ``dashboard.tailscale.enabled`` gets the same bare 403
        # this feature exists to remove, with nothing above debug saying why.
        # The common cause is a boot race: tailscaled has not answered yet.  The
        # server owns a bounded background retry after this startup probe.
        logger.warning(
            "dashboard.tailscale.enabled is on, but no tailnet name could be "
            "resolved, so no tailnet origin was added and `tailscale serve` will "
            "still fail the Origin/Host check for now. Background recovery will "
            "retry without delaying gateway startup; check `tailscale status` if "
            "it does not become active after the daemon reports Running."
        )
        return ""
    return name


# ---------------------------------------------------------------------------
# Runtime origin recovery — the startup probe above remains the fast path.  An
# explicitly enabled gateway that loses a boot race can add one validated
# origin later without mutating aiohttp's frozen application mapping.
# ---------------------------------------------------------------------------


def _origin_resolved_now() -> int:
    """Epoch timestamp for a successful runtime activation."""

    return int(time.time())


@dataclass
class TailnetOriginState:
    """Mutable tailnet state stored before aiohttp freezes the app mapping."""

    host: str = ""
    resolved_at: int = 0
    load_enabled: Callable[[], bool] | None = field(
        default=None,
        repr=False,
        compare=False,
    )
    task: asyncio.Task[None] | None = field(default=None, repr=False, compare=False)


def running_tailnet_origin(
    app: web.Application | Mapping[str, object],
) -> tuple[str, int]:
    """Return the origin the running gateway currently trusts.

    The legacy scalar keys remain as an initial snapshot for compatibility with
    integrations that inspect the app. Runtime-aware callers use the mutable
    value object so recovery never mutates aiohttp's frozen application mapping.
    """

    state = app.get(_ORIGIN_RUNTIME_STATE_KEY)
    if isinstance(state, TailnetOriginState):
        return state.host, state.resolved_at
    host = str(app.get("tailnet_host") or "")
    raw_resolved_at = app.get("tailnet_resolved_at")
    try:
        resolved_at = int(raw_resolved_at) if isinstance(raw_resolved_at, (str, int, float)) else 0
    except (TypeError, ValueError):
        resolved_at = 0
    return host, resolved_at


async def _origin_configured_enabled(load_enabled: Callable[[], bool]) -> bool | None:
    """Read the live opt-in off-loop; ``None`` means fail closed and retry."""

    try:
        return bool(await asyncio.to_thread(load_enabled))
    except Exception:
        logger.debug("tailnet origin recovery: config unreadable", exc_info=True)
        return None


async def _origin_governance_pinned(*, audit_tool: str = "") -> bool:
    """Evaluate the live governance ceiling off-loop, failing closed."""

    try:
        return await asyncio.to_thread(
            is_governance_pinned_off,
            audit_tool=audit_tool,
        )
    except Exception:  # pragma: no cover - the underlying probe is itself guarded
        logger.debug("tailnet origin recovery: governance probe failed", exc_info=True)
        return True


async def _recover_tailnet_origin(app: web.Application, state: TailnetOriginState) -> None:
    """Retry until one validated origin is activated or the opt-in is removed."""

    load_enabled = state.load_enabled
    if load_enabled is None:
        logger.error(
            "tailnet origin recovery stopped: config loader is unavailable; "
            "the request boundary remains unchanged"
        )
        return

    delay = _ORIGIN_RECOVERY_INITIAL_SECS
    while True:
        await asyncio.sleep(delay)

        enabled = await _origin_configured_enabled(load_enabled)
        if enabled is False:
            logger.info("tailnet origin recovery stopped because the setting is off")
            return
        if enabled is None or await _origin_governance_pinned(audit_tool="tailnet_origin_recover"):
            delay = min(delay * 2, _ORIGIN_RECOVERY_MAX_SECS)
            continue

        host = await asyncio.to_thread(self_dns_name)
        if not host:
            delay = min(delay * 2, _ORIGIN_RECOVERY_MAX_SECS)
            continue

        # Re-check both controls immediately before widening the request boundary.
        # No await occurs between the final decision and the set/state update.
        enabled = await _origin_configured_enabled(load_enabled)
        if enabled is False:
            return
        if enabled is None or await _origin_governance_pinned(audit_tool="tailnet_origin_recover"):
            delay = min(delay * 2, _ORIGIN_RECOVERY_MAX_SECS)
            continue

        allowed_origins = app.get("allowed_origins")
        if not isinstance(allowed_origins, set):
            logger.error(
                "tailnet origin recovery stopped: allowed_origins is unavailable; "
                "the request boundary remains unchanged"
            )
            return

        allowed_origins.add(f"https://{host}")
        state.host = host
        state.resolved_at = _origin_resolved_now()
        logger.info(
            "tailnet origin recovered in background: trusting https://%s "
            "(bind and auth unchanged)",
            host,
        )
        return


async def _run_origin_recovery_guarded(
    app: web.Application,
    state: TailnetOriginState,
) -> None:
    """Keep an unexpected recovery failure from becoming an orphaned task error."""

    try:
        await _recover_tailnet_origin(app, state)
    except asyncio.CancelledError:
        raise
    except Exception:
        logger.exception(
            "tailnet origin background recovery stopped unexpectedly; "
            "the request boundary remains unchanged"
        )


async def _start_origin_recovery(app: web.Application) -> None:
    """aiohttp startup hook: schedule recovery without delaying listener setup."""

    state = app.get(_ORIGIN_RUNTIME_STATE_KEY)
    if (
        not isinstance(state, TailnetOriginState)
        or state.host
        or (state.task is not None and not state.task.done())
    ):
        return
    state.task = asyncio.create_task(
        _run_origin_recovery_guarded(app, state),
        name="tailnet-origin-recovery",
    )


async def _stop_origin_recovery(app: web.Application) -> None:
    """aiohttp cleanup hook: leave no background task behind."""

    state = app.get(_ORIGIN_RUNTIME_STATE_KEY)
    if not isinstance(state, TailnetOriginState) or state.task is None:
        return
    state.task.cancel()
    with suppress(asyncio.CancelledError):
        await state.task
    state.task = None


def install_tailnet_origin_recovery(
    app: web.Application,
    *,
    enabled: bool,
    initial_host: str,
    load_enabled: Callable[[], bool],
) -> None:
    """Seed runtime state and register recovery for an unresolved opt-in."""

    try:
        resolved_at = int(app.get("tailnet_resolved_at") or 0) if initial_host else 0
    except (TypeError, ValueError):
        resolved_at = 0
    if initial_host and not resolved_at:
        resolved_at = _origin_resolved_now()
    state = TailnetOriginState(
        host=initial_host,
        resolved_at=resolved_at,
        load_enabled=load_enabled,
    )
    app[_ORIGIN_RUNTIME_STATE_KEY] = state
    # Retain the startup snapshot for compatibility. Runtime consumers call
    # ``running_tailnet_origin`` instead of mutating these keys after app freeze.
    app["tailnet_host"] = initial_host
    app["tailnet_resolved_at"] = resolved_at
    if enabled and not initial_host:
        app.on_startup.append(_start_origin_recovery)
        app.on_cleanup.append(_stop_origin_recovery)


# ---------------------------------------------------------------------------
# Forwarded-peer resolution (RFC §2–§3.1) — daemon-verified identity behind
# `tailscale serve`, so the session pin can bind to a person's device instead
# of the tunnel's loopback address.
#
# The organizing rule (RFC §1): the immediate peer decides whether a forwarded
# header may be read at all; the local daemon, not the header, decides who the
# peer is; the header is only corroboration.
# ---------------------------------------------------------------------------

#: The address ranges a tailnet peer can legitimately arrive from. Anything
#: outside these is not a tailnet address and is never sent to the daemon.
_TAILNET_RANGES = (
    ipaddress.ip_network("100.64.0.0/10"),
    ipaddress.ip_network("fd7a:115c:a1e0::/48"),
)

#: The login `tailscale whois` reports for EVERY ACL-tagged node
#: (tailscale/tailscale#4605). Under ``pin_scope: "login"`` that single value
#: would collapse the pin across the entire tagged fleet, so a resolved login
#: equal to this is ALWAYS pinned at node scope — a hard override, not a
#: preference.
TAGGED_DEVICES_LOGIN = "tagged-devices"

PIN_SCOPE_NODE = "node"
PIN_SCOPE_LOGIN = "login"
PIN_SCOPES = (PIN_SCOPE_NODE, PIN_SCOPE_LOGIN)

_FORWARDED_FOR_HEADER = "X-Forwarded-For"
#: Only this module may read this header, and only to corroborate — the daemon
#: decides identity. A header is not a credential.
_USER_LOGIN_HEADER = "Tailscale-User-Login"

#: whois results are cached by address so a request storm cannot fork a daemon
#: call per request. Short TTL: peer identity is stable over seconds, and a
#: short window bounds how long a stale daemon answer can outlive reality.
_WHOIS_CACHE_TTL_SECS = 30.0
#: A TRANSIENT failure (spawn error, timeout — the daemon-still-starting class)
#: is cached far shorter, so a single blip does not hold an identity-pinned
#: session denied for a full cache window. Definitive answers — including a
#: definitive "no such peer" — keep the full TTL.
_WHOIS_TRANSIENT_TTL_SECS = 2.0

#: Bounded entry count — a flood of distinct spoofed source addresses must not
#: grow the cache without limit.
_WHOIS_CACHE_MAX_ENTRIES = 256


@dataclass(frozen=True)
class ForwardedPeer:
    """A daemon-verified tailnet peer behind the local `tailscale serve` proxy."""

    login: str
    node: str
    address: str


@dataclass(frozen=True)
class TailnetTrust:
    """The operator's identity-trust opt-in, as validated at config load.

    Carried as a plain value object (not read from config here) so this module
    stays free of a config import — the same rule :func:`resolve_tailnet_host`
    follows for ``enabled``.
    """

    trust_identity: bool = False
    allowed_logins: tuple[str, ...] = ()
    pin_scope: str = PIN_SCOPE_NODE
    #: The operator wrote a tailnet identity policy that config load could not
    #: read (see ``DEGRADED_TAILSCALE``). Distinct from ``trust_identity=False``,
    #: which means they never asked for one: an unreadable narrowing must DENY,
    #: not resolve to "no restriction".
    #:
    #: The deny is still the ALLOWLIST doing its job, not a second code path --
    #: a peer is admitted only by ``login_allowed``, so whoever the allowlist
    #: does not name is refused. How much of the parsed allowlist survives to be
    #: named is decided by ``tailnet_effective_allowed_logins`` at the caller,
    #: because it depends on WHICH file failed: a lost overlay may have been the
    #: narrowing, so nothing is enforceable from the base, while a malformed
    #: field inside a readable file leaves the entries that parsed usable. Do
    #: NOT assume this flag implies an empty ``allowed_logins``.
    identity_unknown: bool = False

    @property
    def enforces_identity(self) -> bool:
        """Whether a forwarded tailnet peer must be resolved and allowlisted.

        The one predicate every gate asks, so "may this be pinned", "may this
        rotate" and "may this authenticate" cannot answer differently — a
        request admitted by one and refused by another is the drift this
        property exists to prevent.
        """
        return self.identity_unknown or (self.trust_identity and bool(self.allowed_logins))


_whois_lock = threading.Lock()
#: address → (monotonic expiry, resolved (login, node) or None). Negative
#: results are cached too: a stopped daemon must not be re-probed per request.
_whois_cache: OrderedDict[str, tuple[float, tuple[str, str] | None]] = OrderedDict()


#: Charset allowlist for identity components (login, node name) accepted from
#: the daemon. An allowlist, not a denylist, mirroring ``_valid_magicdns_name``:
#: the destinations are the session pin key and the SEL ``caller`` field, so the
#: question is "is this provably a plain identity token". Covers email-shaped
#: and provider-handle logins, DNS node names, and the literal
#: ``tagged-devices``. Deliberately EXCLUDES ``|`` (the pin-key separator) and
#: ``:`` (the pin-key namespace delimiter), which is what makes the composed
#: key unambiguous.
_IDENTITY_RE = re.compile(r"^[A-Za-z0-9@._%+-]{1,253}$")


def _valid_identity(raw: object) -> str | None:
    """Return *raw* as a usable identity component, or ``None``.

    The value arrives from a subprocess and its destinations are the session
    pin key and the SEL audit ``caller`` field — strict allowlist, see
    ``_IDENTITY_RE``.
    """
    if not isinstance(raw, str):
        return None
    s = raw.strip()
    if not _IDENTITY_RE.match(s):
        return None
    return s


def _self_login(status: dict) -> str:
    """Return the login owning ``Self`` in ``tailscale status --json``.

    Tailscale keys the top-level ``User`` map by ``Self.UserID``. JSON object
    keys are strings even when the daemon's Go type uses an integer id, so the
    lookup is normalised instead of relying on one client version's decoded
    representation. Missing or malformed identity is never guessed: mobile
    setup then refuses to enable persistent identity trust.
    """
    self_node = status.get("Self")
    users = status.get("User")
    if not isinstance(self_node, dict) or not isinstance(users, dict):
        return ""
    user_id = self_node.get("UserID")
    if not isinstance(user_id, (str, int)) or isinstance(user_id, bool):
        return ""
    profile = users.get(str(user_id))
    if not isinstance(profile, dict):
        # Older decoded mappings and test doubles can retain an integer key.
        profile = users.get(user_id)
    if not isinstance(profile, dict):
        return ""
    return _valid_identity(profile.get("LoginName")) or ""


def _whois_node(addr: str) -> tuple[tuple[str, str] | None, bool]:
    """Ask the local daemon who *addr* is. ``((login, node) | None, transient)``.

    Every failure (no binary, daemon down, timeout, non-zero exit, malformed
    JSON, unexpected schema) is ``None`` — the module's "nothing here raises"
    invariant. The second element reports whether the failure was TRANSIENT
    (could not run / timed out) so the cache can retry it sooner.
    """
    data, transient = _run_json_detail(["whois", "--json", addr])
    if not isinstance(data, dict):
        return None, transient
    profile = data.get("UserProfile")
    node = data.get("Node")
    login = _valid_identity(profile.get("LoginName") if isinstance(profile, dict) else None)
    name_raw: object = None
    if isinstance(node, dict):
        name_raw = node.get("Name") or node.get("ComputedName")
    name = _valid_identity(name_raw)
    if login is None or name is None:
        logger.debug("tailscale whois for %s returned no usable identity", addr)
        return None, False
    return (login, name.rstrip(".")), False


def _whois_cached(addr: str) -> tuple[str, str] | None:
    """Cache wrapper around :func:`_whois_node`, TTL'd and bounded.

    Runs in a worker thread (the daemon call blocks). The lock is held across
    the miss path deliberately: under a request storm every concurrent miss for
    the same address waits for the ONE in-flight daemon call and then reads the
    fresh cache entry, instead of each forking its own subprocess.
    """
    with _whois_lock:
        now = time.monotonic()
        hit = _whois_cache.get(addr)
        if hit is not None and hit[0] > now:
            _whois_cache.move_to_end(addr)
            return hit[1]
        result, transient = _whois_node(addr)
        ttl = _WHOIS_TRANSIENT_TTL_SECS if transient else _WHOIS_CACHE_TTL_SECS
        _whois_cache[addr] = (now + ttl, result)
        _whois_cache.move_to_end(addr)
        while len(_whois_cache) > _WHOIS_CACHE_MAX_ENTRIES:
            _whois_cache.popitem(last=False)
        return result


def _forwarded_peer_candidate(request: web.Request, trust: TailnetTrust) -> str | None:
    """The cheap, synchronous gate: RFC §2 conditions (a)–(d), fail-closed.

    Returns the single forwarded tailnet address worth asking the daemon
    about, or ``None``. No I/O — safe to run inline on the event loop.
    """
    # (b) explicit opt-in AND a non-empty allowlist. Identity trust is never
    # inferred, and an empty allowlist means trust was refused at config load.
    # An UNREADABLE policy also enforces: the allowlist is unknown, and
    # ``login_allowed`` against the empty tuple then denies every peer.
    if not trust.enforces_identity:
        return None
    # (a) the immediate peer must be the local proxy. A remote peer's forwarded
    # header is an unverifiable claim and is never read.
    if not is_loopback(request.remote or ""):
        return None
    # (c) EXACTLY one forwarded address. Two or more — whether as repeated
    # headers or one comma-joined value — is a proxy chain this design cannot
    # attribute; reject rather than trust the first or the last.
    values = request.headers.getall(_FORWARDED_FOR_HEADER, [])
    if len(values) != 1:
        return None
    raw = values[0].strip()
    if not raw or "," in raw:
        return None
    # (d) the address must parse and sit inside the tailnet ranges.
    try:
        candidate = ipaddress.ip_address(raw)
    except ValueError:
        return None
    if not any(candidate in net for net in _TAILNET_RANGES):
        return None
    return str(candidate)


def is_forwarded_tailnet_request(request: web.Request, trust: TailnetTrust) -> bool:
    """Whether this request arrived as a tailnet peer behind the local proxy.

    The discriminator a caller needs to fail closed WITHOUT locking anyone out:
    under an unreadable identity policy a peer that could not be attributed must
    be denied, but a request that was never a forwarded tailnet request in the
    first place (loopback, the operator's own browser) resolves to no peer for
    the same reason and must be left alone. Denying on "no peer resolved"
    without asking this first would take the dashboard away from the one person
    who can repair the config.

    Deliberately WEAKER than :func:`_forwarded_peer_candidate`, which answers a
    different question -- "is there exactly one address I may attribute an
    identity to". Attribution demands a single unambiguous address, so it
    rejects a multi-value or comma-joined chain. DENIAL must not: an ambiguous
    chain is still a forwarded tailnet request, so answering "not forwarded"
    there let a caller add a second ``X-Forwarded-For`` header and skip the deny
    entirely, with a valid token doing the rest. Unattributable and absent are
    different things, and only this predicate has to tell them apart.

    So: loopback immediate peer, plus at least one forwarded address anywhere in
    the chain that parses and sits inside the tailnet ranges. A chain carrying
    no tailnet address at all is some other proxy's business and is left alone
    -- widening past the tailnet policy is not this gate's job.

    Synchronous and I/O-free -- the daemon is not consulted, so this is safe to
    ask inline on the event loop.
    """
    # Same opt-in gate _forwarded_peer_candidate applies, repeated rather than
    # inherited: without it an ordinary install (no identity policy at all)
    # would start answering True and make the caller's deny branch reachable.
    if not trust.enforces_identity:
        return False
    # A remote peer's forwarded header is an unverifiable claim. Reading it here
    # would let anyone who can reach the port trigger the refusal for everyone.
    if not is_loopback(request.remote or ""):
        return False
    for value in request.headers.getall(_FORWARDED_FOR_HEADER, []):
        for part in value.split(","):
            raw = part.strip()
            if not raw:
                continue
            try:
                candidate = ipaddress.ip_address(raw)
            except ValueError:
                continue
            if any(candidate in net for net in _TAILNET_RANGES):
                return True
    return False


async def resolve_forwarded_peer(request: web.Request, trust: TailnetTrust) -> ForwardedPeer | None:
    """Resolve the daemon-verified peer behind a local proxy, or ``None``.

    ``None`` covers every unresolvable case — trust off, non-loopback peer,
    zero/multiple forwarded addresses, non-tailnet address, daemon absent or
    down, timeout, malformed output, or a corroborating header that disagrees
    with the daemon. The caller falls through to the existing token+IP path:
    fail-closed on identity, fail-open on availability.

    The blocking daemon call is offloaded to a worker thread so it never runs
    on the event loop; the WebSocket path resolves once here at upgrade, never
    per frame.
    """
    addr = _forwarded_peer_candidate(request, trust)
    if addr is None:
        return None
    # (e) the daemon decides identity. Offloaded onto the DEDICATED subprocess
    # executor, not asyncio.to_thread's shared default pool: waiters can hold a
    # worker for up to the daemon timeout behind _whois_lock, and starving the
    # process-wide default pool with header-driven work would stall unrelated
    # gateway offloads (the cross-starvation subprocess_executor exists to stop).
    resolved = await asyncio.get_running_loop().run_in_executor(
        subprocess_executor(), _whois_cached, addr
    )
    if resolved is None:
        return None
    login, node = resolved
    # (f) the header is only corroboration. Absent costs nothing; a
    # disagreement is a rejection, not a warning — a proxy relaying an
    # attacker-chosen header must not win over the daemon.
    header_login = (request.headers.get(_USER_LOGIN_HEADER) or "").strip()
    if header_login and header_login.lower() != login.lower():
        logger.warning(
            "tailnet peer %s: %s header (%r) disagrees with whois login; rejecting identity",
            addr,
            _USER_LOGIN_HEADER,
            header_login[:64],
        )
        return None
    return ForwardedPeer(login=login, node=node, address=addr)


def peer_pin_key(peer: ForwardedPeer, pin_scope: str) -> str:
    """The session pin key for a resolved peer, per RFC §3.1.

    ``node`` scope (the default, and anything unrecognised — a typo may only
    ever narrow): ``ts:node:<login>|<node>``. ``login`` scope:
    ``ts:login:<login>``. Two shape rules keep the key namespace unambiguous:
    the scope tag in the prefix (logins are emails and contain ``@``, so the
    RFC's bare shapes cannot be told apart when classifying a mismatch), and a
    ``|`` separator that ``_IDENTITY_RE`` forbids inside either component, so
    ``login="a@b", node="c"`` can never collide with ``login="a",
    node="b@c"``. Keys are only ever compared for full-string equality, never
    parsed.

    Hard override: an ACL-tagged node reports the literal ``tagged-devices``
    login for EVERY tagged device, so login scope would make one leaked
    CI-runner session replayable from the whole tagged fleet. A tagged node is
    therefore always pinned at node scope, and the override is logged.
    """
    if peer.login == TAGGED_DEVICES_LOGIN:
        if pin_scope == PIN_SCOPE_LOGIN:
            logger.warning(
                "tailnet peer %s is an ACL-tagged node (login %r); pin_scope "
                "'login' is overridden to node scope for it",
                peer.node,
                TAGGED_DEVICES_LOGIN,
            )
        return f"ts:node:{peer.login}|{peer.node}"
    if pin_scope == PIN_SCOPE_LOGIN:
        return f"ts:login:{peer.login}"
    return f"ts:node:{peer.login}|{peer.node}"


def peer_pin_key_for_claim(peer: ForwardedPeer, claimed_key: str) -> str:
    """Render *peer* in the scope encoded by an existing signed pin claim.

    The claim, not today's config, owns an already-issued session's scope. If
    the operator later changes ``pin_scope``, silently reinterpreting a
    node-bound token as login-bound (or the reverse) would either widen it or
    log the correct device out. Unknown claim shapes default to node scope, the
    narrower direction; the caller's exact comparison then rejects them.
    """
    scope = PIN_SCOPE_LOGIN if claimed_key.startswith("ts:login:") else PIN_SCOPE_NODE
    return peer_pin_key(peer, scope)


def login_allowed(login: str, allowed_logins: tuple[str, ...]) -> bool:
    """Whether *login* is on the operator's allowlist. Case-insensitive.

    Deny-by-default: an empty allowlist allows no one (and also disables
    resolution upstream — see :func:`_forwarded_peer_candidate`).
    """
    candidate = login.strip().lower()
    if not candidate:
        return False
    return any(candidate == entry.strip().lower() for entry in allowed_logins if entry.strip())


async def governed_tailnet_trust(
    trust_identity: bool,
    allowed_logins: tuple[str, ...],
    pin_scope: str,
    *,
    identity_unknown: bool = False,
    unreadable_files: tuple[str, ...] = (),
) -> TailnetTrust:
    """Build the identity-trust value object, with the governance ceiling applied.

    ONE code path for both server startup surfaces (dashboard and headless API)
    — a prior round of the tailnet feature shipped a bug from exactly this
    dual-site drift, so the trust construction lives here rather than being
    duplicated at each call site. Takes plain values rather than a config
    object to keep this module free of a config import.

    An enterprise ceiling pinning ``capabilities.tailnet_origin`` off disables
    identity trust too: the config-set surfaces refuse the enabling WRITE, but
    a value already stored must not keep request-time whois calls and identity
    pinning alive under a policy that forbids the tailnet integration. The
    probe runs in a thread (it reads the trust-root policy from disk) and is
    audited as a governance decision.

    ``identity_unknown`` says config load could not read the operator's tailnet
    policy. It is passed as a plain bool for the same reason the others are —
    the caller owns the config read. The ceiling still wins over it: an
    administrator who forbids the tailnet integration outright wants no whois
    calls at all, and with the integration off there is no allowlist left to
    fail closed on.

    ``unreadable_files`` names the config file(s) involved, for the refusal to
    quote. It matters more than it looks: the file is often
    ``config.local.json`` rather than ``config.json``, and an operator who has
    just lost REMOTE dashboard access needs the right filename in the one log
    line they can still reach.
    """
    trust = TailnetTrust(
        trust_identity=trust_identity,
        allowed_logins=allowed_logins,
        pin_scope=pin_scope,
        identity_unknown=identity_unknown,
    )
    if trust.enforces_identity and await asyncio.to_thread(
        is_governance_pinned_off, audit_tool="tailnet_trust_startup"
    ):
        logger.warning(
            "dashboard.tailscale.trust_identity is on, but capabilities."
            "tailnet_origin is pinned OFF by your administrator's security "
            "policy — tailnet identity trust stays disabled and sessions keep "
            "the ordinary token+IP pin."
        )
        return TailnetTrust()
    if trust.identity_unknown:
        named = ", ".join(unreadable_files) or "dashboard.tailscale in config.json"
        logger.error(
            "tailnet identity policy could not be read (%s), so the login "
            "allowlist is unknown — forwarded tailnet peers are DENIED until it "
            "is fixed and the gateway restarted. Access from this machine "
            "itself is unaffected, so on a headless host repair over SSH (or an "
            "SSH port-forward to the dashboard), not over the tailnet.",
            named,
        )
    return trust
