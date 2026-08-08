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
import json
import logging
import os
import re
import subprocess
from typing import Any

from kiro_crew.platform.governance_profiles import (
    GOVERNANCE_ERROR_REASON,
    governance_permits,
    vet_and_audit,
)
from kiro_crew.sandbox import scrub_env

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
#: locations the official packages install into, all of which need root to write:
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
    """
    for candidate in _CLI_CANDIDATE_PATHS:
        if os.path.isfile(candidate) and os.access(candidate, os.X_OK):
            return candidate
    return None


def _run_json(args: list[str]) -> Any | None:
    """Run the CLI and parse stdout as JSON. ``None`` on ANY failure.

    Deliberately broad: the caller's contract is "a name or nothing", and every
    failure mode here (no binary, daemon down, timeout, non-zero exit, non-JSON
    output) means the same thing to it. Failures are logged at debug so a host
    without Tailscale does not emit noise on every start.
    """
    cli = _cli_path()
    if not cli:
        logger.debug("tailscale CLI not found; skipping tailnet origin derivation")
        return None
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
        return None
    if proc.returncode != 0:
        logger.debug(
            "tailscale %s exited %d: %s",
            " ".join(args),
            proc.returncode,
            (proc.stderr or "").strip()[:200],
        )
        return None
    try:
        return json.loads(proc.stdout or "")
    except (json.JSONDecodeError, ValueError) as exc:
        logger.debug("tailscale %s produced non-JSON output: %s", " ".join(args), exc)
        return None


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
