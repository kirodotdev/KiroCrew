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
from dataclasses import dataclass
from typing import TYPE_CHECKING, Any

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

    A candidate is additionally refused when the binary or its directory is
    writable by the gateway user (checked on POSIX; the Windows entry needs
    elevation to write). The pinned list mostly needs root, but two entries do
    not everywhere: Homebrew chowns ``/opt/homebrew/bin`` (and sometimes
    ``/usr/local/bin``) to the console user, and identity resolution makes
    this a request-triggered execution on the auth path — an agent that can
    write there must not be able to plant a ``tailscale`` the next
    credential-bearing request executes. A refused Homebrew install degrades
    exactly like a missing binary: the feature contributes nothing and the
    documented ``dashboard.url`` fallback still works.
    """
    # getattr: os.geteuid does not exist on Windows, and tests exercise this
    # path with IS_POSIX patched True on every platform.
    for candidate in _CLI_CANDIDATE_PATHS:
        if not (os.path.isfile(candidate) and os.access(candidate, os.X_OK)):
            continue
        if IS_POSIX and not _posix_candidate_trusted(candidate):
            logger.debug(
                "tailscale CLI at %s is in a location this deployment cannot "
                "trust; refusing to execute it (planted-binary defence). Use "
                "an explicit dashboard.url instead.",
                candidate,
            )
            continue
        return candidate
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
    except (json.JSONDecodeError, ValueError) as exc:
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
        # The common cause is a boot race: the gateway resolves once at startup
        # and tailscaled has not answered yet.
        logger.warning(
            "dashboard.tailscale.enabled is on, but no tailnet name could be "
            "resolved, so no tailnet origin was added and `tailscale serve` will "
            "still fail the Origin/Host check. Check `tailscale status`; if the "
            "daemon was still starting, restart the gateway once it reports "
            "Running."
        )
        return ""
    return name


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
    # Windows daemon/CLI behaviour is unverified (RFC OQ4): resolution is
    # POSIX-only and everything degrades to the token+IP path there.
    if not IS_POSIX:
        logger.debug("tailnet peer resolution is POSIX-only; skipping on this platform")
        return None
    # (b) explicit opt-in AND a non-empty allowlist. Identity trust is never
    # inferred, and an empty allowlist means trust was refused at config load.
    if not trust.trust_identity or not trust.allowed_logins:
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
    trust_identity: bool, allowed_logins: tuple[str, ...], pin_scope: str
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
    """
    trust = TailnetTrust(
        trust_identity=trust_identity,
        allowed_logins=allowed_logins,
        pin_scope=pin_scope,
    )
    if trust.trust_identity and await asyncio.to_thread(
        is_governance_pinned_off, audit_tool="tailnet_trust_startup"
    ):
        logger.warning(
            "dashboard.tailscale.trust_identity is on, but capabilities."
            "tailnet_origin is pinned OFF by your administrator's security "
            "policy — tailnet identity trust stays disabled and sessions keep "
            "the ordinary token+IP pin."
        )
        return TailnetTrust()
    return trust
