"""``kirocrew security`` and ``kirocrew policy``: the security and governance inspection commands.

``_security`` prints the deny lists, audits history and memory, lists security events
in a window (``parse_time_selector`` reads ``--since`` and ``--until``) and verifies
the event log; ``_policy`` shows, validates and explains the governance policy and
profile and where they came from. ``_policy_fetch`` (``policy fetch``) is the one verb
that writes: it fetches the central policy and caches it as this host's own.

An owner of the mirror facade ``kiro_crew.cli_commands``: callers and tests reach every
name here as ``kiro_crew.cli_commands.<name>``, and a patch through the facade reaches
this module. This module never imports the facade.
"""

from __future__ import annotations

import argparse
import json
import re
import sys
from collections import Counter
from datetime import datetime, timedelta, timezone

from kiro_crew.cli_commands_memory import _finding_store_suffix
from kiro_crew.config import config_dir
from kiro_crew.config.loader import read_config_text
from kiro_crew.security import (
    BUILTIN_DENIED_RULES,
    BUILTIN_DENY_PATTERNS,
    scan_history,
    scan_memory,
)
from kiro_crew.sel import sel
from kiro_crew.terminal_safe import safe_terminal_line

_TIME_SELECTOR_RE = re.compile(r"(?i)\A(?P<value>\d+)(?P<unit>[smhdw])\Z")


_TIME_SELECTOR_UNITS = {
    "s": 1,
    "m": 60,
    "h": 3600,
    "d": 86400,
    "w": 604800,
}


def parse_time_selector(raw: str, *, now: datetime | None = None) -> datetime | None:
    """Resolve a ``--since``/``--until`` selector to an aware UTC datetime.

    Accepts a relative AGE (``30m``, ``2h``, ``7d``, ``1w``) meaning "that long
    ago", or an absolute ISO 8601 instant (``YYYY-MM-DD``,
    ``2026-08-21T04:00:00Z``). Empty input means "no bound" and returns ``None``.

    A bare ISO date/time with no offset is read as UTC — the audit log is written
    in UTC, so interpreting it as local time would silently shift the window by
    the host's offset. ``Z`` is normalized because ``fromisoformat`` only accepts
    it from Python 3.11 and this package supports 3.10.

    Raises ``ValueError`` with the accepted forms spelled out, so a typo gets a
    usable message instead of an empty result the caller reads as "no events".
    An absurd but well-formed age (``999999999999999999w``) overflows
    ``timedelta``; that surfaces as the same ``ValueError`` rather than an
    uncaught ``OverflowError`` traceback, so the CLI still exits 2 with guidance.
    """
    text = (raw or "").strip()
    if not text:
        return None
    match = _TIME_SELECTOR_RE.match(text)
    if match:
        seconds = int(match.group("value")) * _TIME_SELECTOR_UNITS[match.group("unit").lower()]
        try:
            return (now or datetime.now(tz=timezone.utc)) - timedelta(seconds=seconds)
        except (OverflowError, OSError):
            raise ValueError(
                f"{raw!r} is too far in the past to represent. Use a smaller age "
                "(30m, 2h, 7d, 1w) or an ISO 8601 instant."
            ) from None
    iso = text[:-1] + "+00:00" if text.endswith(("Z", "z")) else text
    try:
        parsed = datetime.fromisoformat(iso)
    except ValueError:
        raise ValueError(
            f"cannot read {raw!r} as a time. Use a relative age (30m, 2h, 7d, 1w) "
            "or an ISO 8601 instant (2026-08-21, 2026-08-21T04:00:00Z)."
        ) from None
    if parsed.tzinfo is None:
        return parsed.replace(tzinfo=timezone.utc)
    return parsed.astimezone(timezone.utc)


def _security(args: argparse.Namespace) -> None:
    """Security audit and deny list commands."""

    action = getattr(args, "sec_action", None)
    if action == "deny-list":
        print("🔒 Built-in deny patterns (always enforced):")
        for p in BUILTIN_DENY_PATTERNS:
            print(f"  ✗ {p}")
        cfg_path = config_dir() / "config.json"
        if cfg_path.exists():
            try:
                data = json.loads(read_config_text(cfg_path))
            except (OSError, ValueError) as exc:
                # The built-in list above is already printed; say plainly that the
                # user-configured half could not be read instead of a traceback.
                print(f"\n⚠️  Could not read user-configured deny patterns from {cfg_path}: {exc}")
                data = {}
            hooks = data.get("hooks") if isinstance(data, dict) else None
            extra = hooks.get("auto_deny_tools", []) if isinstance(hooks, dict) else []
            if extra:
                print("\n🔧 User-configured deny patterns:")
                for p in extra:
                    print(f"  ✗ {p}")
    elif action == "audit":
        history_dir = config_dir() / "history"
        findings = scan_history(history_dir)
        if findings:
            print(f"⚠️  {len(findings)} suspicious entries found:\n")
            for f in findings:
                print(f"  📄 {f['file']}")
                print(f"     {f['warning']}")
                print(f"     {f['snippet'][:120]}…\n")
        else:
            print("✅ No suspicious tool usage found in recent history.")

        mem_findings = scan_memory()
        if mem_findings:
            print(f"\n⚠️  {len(mem_findings)} suspicious memory entries:\n")
            for f in mem_findings:
                print(f"  [{f['type']}] {f['key']}{_finding_store_suffix(f)}: {f['warning']}")
                print(f"    {f['value'][:120]}\n")
        elif not findings:
            pass
        else:
            print("✅ No suspicious content in vector memory.")
    elif action == "events":

        limit = getattr(args, "limit", 20)
        try:
            since = parse_time_selector(getattr(args, "since", "") or "")
            until = parse_time_selector(getattr(args, "until", "") or "")
        except ValueError as exc:
            print(f"❌ {exc}")
            sys.exit(2)
        if since and until and since >= until:
            print("❌ --since must be earlier than --until")
            sys.exit(2)
        events = sel().recent(limit=limit, since=since, until=until)
        window = ""
        if since or until:
            window = (
                f" in [{since.isoformat() if since else '-'}, "
                f"{until.isoformat() if until else 'now'})"
            )
        if not events:
            print(f"No security events recorded{window}.")
            return
        print(f"📋 Last {len(events)} security event(s){window}:\n")

        def _safe(key: str, default: str = "") -> str:
            """One row field, coerced to text and stripped of live controls.

            Two separate hazards meet here. The row can hold CALLER text: SEL's own
            ``_REDACTED_TEXT_FIELDS`` names ``operation``, ``resources`` and
            ``error``, and ``log_api_access`` documents ``outcome`` the same way
            because an installed app reaches it through ``ctx.audit``. Those passes
            police credentials and length, never control sequences, so an ESC/OSC
            payload would execute in the owner's terminal -- the one place this
            trail is read. And the row need not be a string at all: the log is
            sandbox read-write (``_CREW_SANDBOX_VISIBLE_LEAVES``) while ``recent()``
            validates only that each line is a dict, so a forged line with a
            non-string field would abort the whole command inside ``re.sub``.
            Coercing before sanitizing answers both, and keeps one field's bad
            value from hiding every other event.
            """
            value = e.get(key, default)
            return safe_terminal_line(value if isinstance(value, str) else str(value))

        for e in events:
            ts = _safe("timestamp", "?")[:19]
            etype = _safe("event_type", "?")
            op = _safe("operation", "?")
            outcome = _safe("outcome", "?")
            src = _safe("source", "?")
            caller = _safe("caller_identity", "?")
            print(f"  {ts}  [{src}] {etype}: {op} → {outcome}  (caller: {caller})")
            if e.get("error"):
                print(f"    error: {_safe('error')[:120]}")
            # ``resources`` names WHAT the decision was about -- the file a scanner
            # held back, the destination class a grant covered. Without it the line
            # says a refusal happened and never says what was refused, which is the
            # one thing the owner reading this is trying to learn.
            if e.get("resources"):
                print(f"    resources: {_safe('resources')[:120]}")
            if e.get("downstream_service"):
                print(f"    downstream: {_safe('downstream_service')}")
    elif action == "verify":

        # detailed=True: a segment dir that refused to pin (or was swapped
        # mid-verification) leaves the ROTATED segments unchecked, and the
        # command whose job is to surface tampering must not call that run
        # "intact" over the live log alone.
        result = sel().verify_integrity(detailed=True)
        if not result.history_verifiable:
            # The live-log clause is derived from the SAME pass's counts, so
            # it can never claim "intact" over entries that did not verify.
            if result.total and result.total != result.valid:
                live = (
                    f"the live log shows tampered entries: "
                    f"{result.valid}/{result.total} entries valid"
                )
            elif result.total:
                live = f"the live log verified intact: {result.valid}/{result.total} entries"
            else:
                live = "no events to verify"
            print(
                f"⚠️  Audit history UNVERIFIABLE: {result.reason}. "
                f"Rotated segments were not checked — {live}."
            )
        elif result.total == 0:
            print("No security events to verify.")
        elif result.total == result.valid:
            print(f"✅ HMAC chain intact: {result.total} entries verified.")
        else:
            print(
                f"⚠️  HMAC chain COMPROMISED: {result.valid}/{result.total} entries "
                f"valid, {result.total - result.valid} tampered."
            )
    else:
        print("Usage: kirocrew security {audit|deny-list|events|verify}")


def _print_denied_command_summary(*, ids: bool) -> None:
    """Print the built-in denied-command catalog as grouped counts (or, with
    ``--ids``, each category's rule ids).

    The built-in rules are visible and configurable to the USER (Settings
    → Security renders them in category accordions, backed by
    ``GET /api/security/denied-commands``) but are invisible to the AGENT --
    ``policy show`` reports everything except them, so without this an agent
    planning a multi-step task has no way to learn a class of work is hard-denied
    before committing to a plan that turns out to be impossible.

    Deliberately just counts + ids, not the full 139 regex patterns: enough
    for planning ("this class of work is blocked") and for citing a rule id
    when relaying a refusal, without bloating the output the way dumping
    every pattern would.
    """
    by_category: dict[str, list] = {}
    for rule in BUILTIN_DENIED_RULES:
        by_category.setdefault(rule.category, []).append(rule)
    counts = Counter({cat: len(rules) for cat, rules in by_category.items()})
    print(
        f"   • commands.denied: {len(BUILTIN_DENIED_RULES)} rules "
        f"in {len(by_category)} categories"
    )
    if ids:
        for cat, rules in sorted(by_category.items(), key=lambda kv: -len(kv[1])):
            rule_ids = ", ".join(r.id for r in rules)
            print(f"       {cat}({len(rules)}): {rule_ids}")
    else:
        summary = " ".join(f"{cat}({n})" for cat, n in counts.most_common())
        print(f"       {summary}")
        print("     (add --ids for rule ids, or see Settings → Security)")


def _policy(args: argparse.Namespace) -> None:
    """Governance policy + profile inspection (read-only; safe to expose to LLM).

    Mirrors the ``security`` command shape.  Boot already ran (cli.main calls
    ``boot_platform`` first), so ``current_context().governance`` carries the
    effective ceiling.  No mutation — purely diagnostic, so it is MCP-safe.
    """
    from kiro_crew.platform.context import current_context
    from kiro_crew.platform.governance import (
        CAPABILITY,
        SCOPE_CATALOG,
        gate_decision,
        resolve,
    )
    from kiro_crew.platform.governance_profiles import (
        get_store_profile,
        resolve_active_scope,
    )

    action = getattr(args, "policy_action", None)
    ceiling = getattr(current_context(), "governance", None)

    if action == "show":
        if ceiling is None:
            print("No enterprise security policy is active (editable secure-defaults).")
            _print_denied_command_summary(ids=getattr(args, "ids", False))
            return
        # Report the PROVEN provenance, not the claimed one: printing a bare
        # issuer implied a trust decision nothing had made.  signature_summary()
        # distinguishes verified / signed-but-unverified / unsigned so an operator
        # can tell an established issuer from a decorative one.
        print(f"🛡️  Security policy v{ceiling.version}")
        print(f"   provenance: {ceiling.signature_summary()}")
        print(
            f"   boot: require_sandbox={ceiling.boot.require_sandbox} "
            f"allow_terminal={ceiling.boot.allow_terminal} fail_closed={ceiling.boot.fail_closed}"
        )
        if not ceiling.controls:
            print("   (no governed scopes)")
        for scope in sorted(ceiling.controls):
            print(f"   • {scope}: {ceiling.controls[scope]}")
        _print_denied_command_summary(ids=getattr(args, "ids", False))

    elif action == "validate":
        ok = True
        if ceiling is None:
            print("Policy: none (editable secure-defaults) — nothing to validate.")
        else:
            print(f"Policy: v{ceiling.version} OK ({len(ceiling.controls)} governed scopes).")
            # A capability the policy does not name is UNGOVERNED, and an
            # ungoverned control is permitted — omission never denies (see the
            # CAPABILITY-DEFAULT CONTRACT in platform/governance_parts/catalog.py). That is
            # the same rule every other archetype follows, but it is the one authors
            # most often get wrong, because a partial `capabilities` block LOOKS
            # like a complete statement. Report the gap so an unpinned row reads
            # as a choice instead of an oversight.
            unnamed = sorted(
                scope
                for scope, spec in SCOPE_CATALOG.items()
                if spec.kind == CAPABILITY and scope not in ceiling.controls
            )
            if unnamed and len(unnamed) < sum(
                1 for spec in SCOPE_CATALOG.values() if spec.kind == CAPABILITY
            ):
                print(
                    f"   ⚠️  governs capabilities but leaves {len(unnamed)} "
                    "row(s) UNGOVERNED (therefore permitted):"
                )
                for scope in unnamed:
                    print(f"        {scope}")
                print(
                    "      Omission does not deny. Name each row explicitly "
                    "(enabled true or false) if you meant to decide it."
                )
        # Force-load every profile; the store records invalid ones as deny-all.
        from kiro_crew.platform.governance_profiles import _profiles_dir

        pdir = _profiles_dir()
        if pdir.is_dir():
            for f in sorted(pdir.glob("*.json")):
                prof = get_store_profile(f.stem)
                status = "OK" if prof and not prof.name.startswith("_deny") else "INVALID→deny-all"
                if status != "OK":
                    ok = False
                print(f"   profile {f.name}: {status}")
        else:
            print("   (no profiles directory)")
        print("✅ valid" if ok else "⚠️  some profiles failed validation (fell back to deny-all)")

    elif action == "explain":
        scope = args.scope
        if scope not in SCOPE_CATALOG:
            print(f"Unknown scope {scope!r}. Known: {', '.join(sorted(SCOPE_CATALOG))}")
            return
        profile = resolve_active_scope(args.session_key, agent=args.agent, app=args.app)
        decision = resolve(ceiling, profile, scope, args.item)
        verdict = "ALLOWED" if decision.permitted else "DENIED"
        print(f"{verdict}: {scope} → {args.item!r}")
        print(f"   surface session: {args.session_key!r}")
        print(f"   active profile : {profile.name if profile else '(none — policy only)'}")
        print(f"   rule/layer     : {decision.rule} / {decision.layer or '—'}")
        print(f"   reason         : {decision.reason}")
        # Also show the raw title-classified path (mirrors the live gate).
        gate = gate_decision(ceiling, profile, args.item)
        print(f"   gate verdict   : {'ALLOWED' if gate.permitted else 'DENIED'} ({gate.reason})")

    elif action == "profile":
        prof = get_store_profile(args.name)
        if prof is None:
            print(f"No profile named {args.name!r} in ~/.kiro/crew/profiles/.")
            return
        bind = f"{prof.bind.type}:{prof.bind.id}" if prof.bind else "(unbound)"
        print(f"📄 Profile {prof.name!r}  bind={bind}  extends={prof.extends or '—'}")
        if not prof.controls:
            print("   (no governed scopes — inherits policy ceiling unchanged)")
        for scope in sorted(prof.controls):
            print(f"   • {scope}: {prof.controls[scope]}")

    elif action == "source":
        _print_policy_source()

    elif action == "fetch":
        _policy_fetch(force=getattr(args, "force", False))

    else:
        print(
            "Usage: kirocrew policy {show|validate|explain <scope> <item>|"
            "profile <name>|source|fetch}"
        )


def _print_policy_source() -> None:
    """Report whether this host follows a centrally distributed ceiling.

    Prints the source's SCHEME rather than its URL, matching what the dashboard
    viewer exposes: this command is reachable from a shell the agent may drive, and
    the endpoint is the fleet's control plane. An operator who needs the URL reads
    it from the policy file or the environment, out of band.
    """
    from kiro_crew.platform.policy_distribution import (
        POLICY_URL_ENV,
        distribution_posture,
        registered_policy_schemes,
    )

    posture = distribution_posture()
    if posture.get("error_code"):
        print(
            "⚠️  Central policy distribution is misconfigured; see the gateway log "
            "for the reason and check the 'distribution' block in your policy."
        )
        return
    if not posture.get("configured"):
        print("Central policy distribution: not configured (this host uses a local policy).")
        print(
            f"   Set {POLICY_URL_ENV}, or add a 'distribution' block to the policy, to enable it."
        )
        print(f"   Transports available: {', '.join(registered_policy_schemes())}")
        return

    interval = posture.get("refresh_interval_seconds") or 0
    print("🌐 Central policy distribution: ACTIVE")
    print(f"   transport        : {posture.get('source_scheme') or '—'}")
    print(f"   refresh          : {f'every {interval}s' if interval else 'at boot only'}")
    print(f"   polling now      : {'yes' if posture.get('refresher_running') else 'no'}")
    max_age = posture.get("max_cache_age_seconds") or 0
    print(f"   staleness bound  : {f'{max_age}s' if max_age else 'none'}")
    print(f"   if unavailable   : {posture.get('on_unavailable')}")
    if posture.get("cache_present"):
        print(f"   cached copy      : {posture.get('cache_age_seconds')}s old")
    else:
        print("   cached copy      : none (an outage would leave this host with no ceiling)")
    if posture.get("last_refresh_status"):
        print(
            f"   last refresh     : {posture['last_refresh_status']} "
            f"({posture.get('last_refresh_age_seconds')}s ago)"
        )


def _policy_fetch(*, force: bool) -> None:
    """Fetch the central policy now, applying it when it is usable.

    Exits non-zero on a refusal or an unreachable source so this is usable as a
    fleet-verification step in a config-management run: an admin rolling a change
    needs a check that FAILS on the host that did not take it, not one that prints
    a warning into a log nobody reads.

    **What it can and cannot claim.** ``refresh_now`` installs the ceiling in the calling
    process, and this process exits immediately — so a bare "applied" would overclaim: a
    running gateway is a different process and keeps its own ceiling until its refresher
    polls. What this command really establishes is that the endpoint serves a document
    this host accepts, and that the document is now the host's last-known-good. The
    message says which of those happened and when a running gateway takes it, because a
    boot-only source (no ``refresh_interval_secs``) has no next cycle to take it on.
    """
    from kiro_crew.platform.policy_distribution import (
        REFRESH_APPLIED,
        REFRESH_NOT_CONFIGURED,
        REFRESH_REJECTED,
        REFRESH_UNCHANGED,
        effective_refresh_interval,
        refresh_now,
    )

    outcome = refresh_now(force=force)
    if outcome.status == REFRESH_NOT_CONFIGURED:
        print("Central policy distribution is not configured; nothing to fetch.")
        return
    if outcome.status == REFRESH_UNCHANGED:
        print("✅ The central policy is unchanged; this host is current.")
        if outcome.detail:
            print(f"   {outcome.detail}")
        return
    if outcome.status == REFRESH_APPLIED:
        print("✅ Fetched a valid governance ceiling and cached it as this host's own.")
        if outcome.signature_state:
            print(f"   provenance: {outcome.signature_state}")
        interval = effective_refresh_interval()
        if interval:
            print(
                f"   A running gateway adopts it within {interval}s, on its next refresh; "
                "a newly started one adopts it immediately."
            )
        else:
            print(
                "   This source is boot-only (no refresh_interval_secs), so a gateway "
                "already running keeps its current ceiling until it is restarted. Set a "
                "refresh interval if a push should bind without one."
            )
        return
    # Rejected or unreachable. The running ceiling is untouched either way, which
    # is worth saying: an operator reading a failure needs to know whether the host
    # is now ungoverned (it is not).
    # "was refused", not "refused": the policy is the object of the refusal, not the
    # thing doing it.
    label = "was refused" if outcome.status == REFRESH_REJECTED else "could not be reached"
    print(f"❌ The central policy {label}: {outcome.detail}")
    print("   The ceiling already in effect is unchanged.")
    raise SystemExit(1)
