"""Reading the credit-pill numbers out of ``kiro-cli``: the parse, the identity checks, the spawns.

Everything the usage refresh in :mod:`kiro_crew.dashboard.handlers.sessions` needs that
holds no state of its own: parsing the ``/usage`` table and its bonus grants, normalising a
text scrape, comparing two ``whoami`` identities, redacting the payload before it is cached,
the sandboxed argv the ``whoami`` and ``/usage`` spawns run under (pinned to the engine that
answers ``/usage`` locally), and the ``whoami`` fetch itself. The refresh loop, the cache it
publishes and the usage routes stay in the facade, which resolves ``kiro-cli`` through the
ACP layer. New parsing or identity rules for the usage reading go here.
"""

from __future__ import annotations

import asyncio
import logging
import os
import re

from kiro_crew.cloud.login_target import parse_whoami_output
from kiro_crew.executors import subprocess_executor
from kiro_crew.kiro_cli import kiro_cli_version_at, pin_kiro_cli
from kiro_crew.kiro_prerequisite import spawn_supervised_oneshot
from kiro_crew.platform_compat import _SUBPROCESS_NO_WINDOW, kill_and_reap
from kiro_crew.sandbox import (
    cgroup_scope_argv,
    configured_sandbox_mode,
    scrub_agent_subprocess_env,
    wrap_argv,
)
from kiro_crew.security import redact_credentials, redact_exfiltration_urls

# The family logs under the facade's name, which log filters and caplog assertions key on.
logger = logging.getLogger("kiro_crew.dashboard.handlers.sessions")


_MAX_BONUS_GRANTS = 32
_MAX_BONUS_NAME_CHARS = 100
_MAX_BONUS_CREDITS = 1_000_000.0
_MAX_BONUS_DAYS_LEFT = 3_650
_ANSI_ESCAPE_RE = re.compile(r"\x1b\[[0-9;?]*[a-zA-Z]")
_BONUS_DASH_RE = re.compile(r"^([\d.]+)/([\d.]+)\s+used\s+\((\d+)\s+days?\s+left\)$")
_BONUS_COLON_RE = re.compile(
    r"^(.+?):\s*([\d.]+)/([\d.]+)\s*\(expires\s+in\s+(\d+)\s+days?\)$",
    re.IGNORECASE,
)


def _safe_float(text: str) -> float | None:
    """Parse a float, returning None on malformed input instead of raising."""
    try:
        return float(text)
    except ValueError:
        return None


def _safe_int(text: str) -> int | None:
    """Parse a regex-constrained integer without letting huge input raise."""
    try:
        return int(text)
    except ValueError:
        return None


def _parse_usage(raw: str) -> dict[str, object]:
    """Parse structured fields from kiro-cli /usage output."""
    clean = _ANSI_ESCAPE_RE.sub("", raw)
    result: dict[str, object] = {"raw": ""}

    lines = clean.splitlines()
    usage_lines: list[str] = []
    capture = False
    for line in lines:
        if "Estimated Usage" in line:
            capture = True
        if capture:
            usage_lines.append(line)
    result["raw"] = "\n".join(usage_lines).strip()

    # Parse fields. First-wins on each field so a duplicate header or echoed
    # line later in the (untrusted) output can't overwrite a real value, and
    # malformed numbers skip the field via _safe_float rather than aborting.
    for line in usage_lines:
        if "resets on" in line and "resets" not in result:
            m = re.search(r"resets on (\S+)", line)
            if m:
                result["resets"] = m.group(1)
            if "|" in line:
                result["plan"] = line.rsplit("|", 1)[-1].strip()
        if "Credits used" in line and "credits_used" not in result:
            m = re.search(r"Credits used:\s*([\d.]+)", line)
            if m:
                v = _safe_float(m.group(1))
                if v is not None:
                    result["credits_used"] = v
        if "Est. cost" in line and "cost_usd" not in result:
            m = re.search(r"\$([\d.]+)", line)
            if m:
                v = _safe_float(m.group(1))
                if v is not None:
                    result["cost_usd"] = v
        if "covered in plan" in line and "credits_plan" not in result:
            m = re.search(r"\(([\d.]+)\s+of\s+([\d.]+)", line)
            if m:
                covered = _safe_float(m.group(1))
                plan = _safe_float(m.group(2))
                if covered is not None and plan is not None:
                    result["credits_covered"] = covered
                    result["credits_plan"] = plan
        if "billed at" in line and "overage_rate" not in result:
            m = re.search(r"\$([\d.]+)\s+per", line)
            if m:
                # Coerce to float so both sources emit one type for the
                # canonical shape and consumers never branch on source.
                rate = _safe_float(m.group(1))
                if rate is not None:
                    result["overage_rate"] = rate

    # Kiro CLI has shipped both "name: used/total (expires in N days)" and
    # "name - used/total used (N days left)". Parse both bounded formats and
    # retain every active grant; one malformed line must not poison the rest.
    bonus_credits: list[dict[str, object]] = []
    in_bonus_section = False
    for raw_line in usage_lines:
        line = raw_line.strip()
        if "bonus credits:" in line.casefold():
            in_bonus_section = True
            continue
        if not in_bonus_section:
            continue
        if line.startswith("Credits") or line.startswith("Overages:"):
            break
        if not line:
            continue
        if " - " in line:
            name, usage_text = line.rsplit(" - ", 1)
            match = _BONUS_DASH_RE.fullmatch(usage_text.strip())
            values = match.groups() if match else None
        else:
            match = _BONUS_COLON_RE.fullmatch(line)
            if match:
                name = match.group(1)
                values = match.group(2), match.group(3), match.group(4)
            else:
                name = ""
                values = None
        name = name.strip()
        if not values or not name or len(name) > _MAX_BONUS_NAME_CHARS:
            continue
        used = _safe_float(values[0])
        total = _safe_float(values[1])
        days_left = _safe_int(values[2])
        if (
            used is None
            or total is None
            or days_left is None
            or used < 0
            or total <= 0
            or used > _MAX_BONUS_CREDITS
            or total > _MAX_BONUS_CREDITS
            or days_left > _MAX_BONUS_DAYS_LEFT
            or not name.isprintable()
        ):
            continue
        bonus_credits.append({"name": name, "used": used, "total": total, "days_left": days_left})
        if len(bonus_credits) >= _MAX_BONUS_GRANTS:
            break
    # Preserve an observed empty section as an explicit empty list, so callers
    # can distinguish "no active grants" from an output format with no section.
    if in_bonus_section:
        result["bonus_credits"] = bonus_credits
    return result


def _normalize_text_usage(parsed: dict[str, object]) -> dict[str, object]:
    """Convert the text-scrape parse result to the canonical usage shape.

    Emits the canonical usage shape the dashboard consumes so it never branches
    on source:
      credits_used = TOTAL used, credits_overage = overage above plan,
      credits_covered = in-plan portion, credits_plan = limit, percentage.

    In the raw text, "Credits used:" is the OVERAGE field (0 for org accounts,
    and absent entirely on kiro-cli 2.11.x), while "(X of Y covered in plan)" is
    the in-plan covered/limit. Total = covered + overage. When the text carries
    no overage, this honestly reports covered==total.
    """
    covered = parsed.get("credits_covered")
    plan = parsed.get("credits_plan")
    if not isinstance(covered, (int, float)) or not isinstance(plan, (int, float)):
        # No usable credit plan — preserve whatever parsed (e.g. just {"raw": ...}).
        return dict(parsed)
    raw_used = parsed.get("credits_used")
    overage = float(raw_used) if isinstance(raw_used, (int, float)) else 0.0
    total = float(covered) + overage
    out: dict[str, object] = dict(parsed)
    out["credits_used"] = total
    out["credits_overage"] = overage
    out["credits_covered"] = float(covered)
    out["credits_plan"] = float(plan)
    out["percentage"] = round(total / plan * 100, 1) if plan else 0.0
    out["source"] = "text"
    return out


def _same_identity(cached: dict[str, object], identity: dict[str, object]) -> bool:
    """True only when ``cached`` provably belongs to the current ``identity``.

    Matches on BOTH the account email AND the SSO ``start_url`` (the IAM
    Identity Center instance): the same human email can appear across different
    Identity Center orgs, so email alone does not prove the same account —
    pairing it with the issuer URL does. Every compared field must be a
    non-empty string and equal; anything missing or mismatched is UNPROVEN
    (``False``). The current identity's values are routed through the same
    redaction the cached copy went through so the two are compared in the same
    form.

    This is the gate that stops an account switch A->B (with the API failing on
    B's refresh) from keeping A's usage AND A's email on screen — including the
    same-email-different-org case — without it, preserving the prior cache would
    leak the previous account's data.
    """

    def _red(v: object) -> object:
        return _redact_strings(v) if isinstance(v, str) else v

    for key in ("email", "start_url"):
        a = cached.get(key)
        b = _red(identity.get(key))
        if not (isinstance(a, str) and isinstance(b, str) and a and a == b):
            return False
    return True


#: Verdicts of :func:`_whoami_agreement` on the two whoami snapshots of one refresh.
_WHOAMI_SAME = "same"
_WHOAMI_DIFFERENT = "different"
_WHOAMI_UNPROVEN = "unproven"


def _whoami_agreement(before: dict[str, object], after: dict[str, object]) -> str:
    """Judge whether two whoami snapshots taken during one refresh PROVE one account.

    The refresh reads whoami at its top (the credential anchor) and again right
    before it publishes an API reading, and that reading may be published only
    on ``_WHOAMI_SAME``: a profile switch that lands between the two reads would
    otherwise cache the FIRST account's balance and email under the second
    account's session. The verdict is field-wise over the identity evidence --
    the account email, its SSO ``start_url`` and the profile ARN:

    * ``_WHOAMI_SAME`` -- at least one field is a non-empty string in BOTH
      snapshots and equal, and no field carried by either snapshot disagrees.
      That is at least the proof :func:`_same_identity` demands (email AND
      ``start_url``, so the same-email-different-org case is a mismatch) and
      also serves the shapes that carry less: a social login reports email and
      ARN, a Builder ID account reports its email alone.
    * ``_WHOAMI_DIFFERENT`` -- a field is carried by one snapshot and not the
      other, or carried by both with different values. That is a switch, or a
      session that lapsed mid-refresh; the reading belongs to whoever was
      signed in a moment ago and nothing of it may be published or kept.
    * ``_WHOAMI_UNPROVEN`` -- NEITHER snapshot carries any identity evidence
      (kiro-cli printed no identity both times). Nothing proves the account,
      so the API reading is not published: with no anchoring ARN the API
      accepted whichever stored token it found, and the sign-out half of a
      profile switch reads exactly like this while the outgoing account's
      token is still on disk. The caller falls back to kiro-cli's own
      ``/usage`` panel, which describes the signed-in account by construction.

    Two empty snapshots therefore never count as agreement: absence of a
    switch is not evidence of an account.
    """
    proven = False
    for key in ("email", "start_url", "_profile_arn"):
        a, b = before.get(key), after.get(key)
        if a is None and b is None:
            continue
        if isinstance(a, str) and isinstance(b, str) and a and a == b:
            proven = True
            continue
        return _WHOAMI_DIFFERENT
    return _WHOAMI_SAME if proven else _WHOAMI_UNPROVEN


#: Unavailable-marker ``reason`` for a sign-in whose whoami names its provider
#: and no user (see :func:`_account_type_only`).
_REASON_ACCOUNT_UNPROVEN = "account_unproven"


def _account_type_only(identity: dict[str, object]) -> bool:
    """True when an external identity provider whoami names no user.

    The external identity provider shape: ``{"accountType":"ExternalIdP",
    "email":""}``. A type names a provider, not a user, and nothing kiro-cli
    prints or stores for that sign-in is evidenced to name the user: the stored
    credential's stable claims (``client_id``, ``scopes``) belong to the IdP
    registration every user of it shares. No reading from either source can
    therefore be tied to the user (:func:`_whoami_agreement` never returns
    ``same`` for two such snapshots), so the refresh shows the account and
    reports the balance unavailable without publishing one.

    Only ExternalIdP. Any other type reporting only itself (a lapsed Builder ID
    or Identity Center sign-in, a future type) takes the ordinary path, which
    also refuses an unproven reading and keeps its ``signin_required`` remedy.
    """
    account_type = identity.get("account_type")
    return (
        isinstance(account_type, str)
        # Normalized like the API-key check: kiro-cli and its token store
        # spell the type ``ExternalIdP`` and ``ExternalIdp``.
        and re.sub(r"[^a-z0-9]", "", account_type.lower()) == "externalidp"
        and all(identity.get(key) is None for key in ("email", "start_url", "_profile_arn"))
    )


def _text_scrape_regresses_api_value(
    prev: object, new: dict[str, object], identity: dict[str, object]
) -> bool:
    """True when a fresh text-scrape would clobber a richer API-sourced value.

    The text scrape is overage-blind for org-managed accounts: recent kiro-cli
    dropped the overage line from ``/usage`` stdout, so ``_normalize_text_usage``
    caps ``credits_used`` at the plan (``covered + 0``) and reports zero overage.
    The API path (``GetUsageLimits``) still returns the true total. When the API
    call transiently fails and we fall back to the scrape, accepting that capped
    value would overwrite the good API number and flip the pill from the real
    figure (e.g. 41,336/10,000 = 413%) to a misleading 10,000/10,000 = 100%,
    hiding all overage — the observed oscillation bug.

    Guard against exactly that, but ONLY when it is safe to keep the prior value:
      * the cached value is ``source == "api"`` (authoritative), AND
      * it provably belongs to the CURRENT identity (``_same_identity``) — so an
        account switch never pins the previous account's usage/email, AND
      * it is the SAME billing cycle — BOTH ``resets`` dates present, non-empty
        and equal; a missing or changed date lets the lower scrape win, AND
      * it reports strictly more usage than the overage-blind scrape can see.
    Text-only environments (no API prior) update normally, and a genuine
    billing-cycle reset is reported by the primary API path, which runs first
    every cycle, so this never pins a stale-high value once the API recovers.
    """
    if not isinstance(prev, dict) or prev.get("source") != "api":
        return False
    if not _same_identity(prev, identity):
        return False
    # Preserve ONLY within the same, provable billing cycle: both reset dates
    # must be present, non-empty, and equal. A missing date on either side
    # (e.g. GetUsageLimits omitting nextDateReset) is unprovable, so let the
    # lower scrape win — a cycle rollover must never pin last cycle's total.
    # Both sources emit `resets` as "%Y-%m-%d", so equality is apples-to-apples.
    prev_resets = prev.get("resets")
    new_resets = new.get("resets")
    if not (
        isinstance(prev_resets, str)
        and prev_resets
        and isinstance(new_resets, str)
        and new_resets
        and prev_resets == new_resets
    ):
        return False
    prev_used = prev.get("credits_used")
    new_used = new.get("credits_used")
    if not isinstance(prev_used, (int, float)) or not isinstance(new_used, (int, float)):
        return False
    return prev_used > new_used


def _redact_strings(value: object) -> object:
    """Recursively redact credentials / exfil URLs from every string leaf.

    Walks dicts and lists so nested values cannot bypass redaction, used on
    untrusted kiro-cli output before it is cached and served to the dashboard.
    """
    if isinstance(value, str):
        value, _ = redact_exfiltration_urls(value)
        value, _ = redact_credentials(value)
        return value
    if isinstance(value, dict):
        return {k: _redact_strings(v) for k, v in value.items()}
    if isinstance(value, list):
        return [_redact_strings(v) for v in value]
    return value


def _wrap_argv_at_configured_tier(argv: list[str]) -> tuple[list[str], str | None]:
    """Sandbox-wrap a one-shot ``kiro-cli`` argv at the configured tier.

    BLOCKING on two counts, which is why both callers hand it to an executor
    rather than calling it inline: :func:`configured_sandbox_mode` stats (and on a
    cache miss re-reads and revalidates) ``config.json``, and ``wrap_argv`` ->
    ``detect_backend`` can cold-probe the sandbox backend with a synchronous
    ``subprocess.run(..., timeout=5)``. Both reads must therefore happen in the
    worker thread — resolving the mode on the loop and passing it in would leave
    half the blocking work behind.

    Exists so the mode resolution and the wrap cannot drift apart between the
    identity fetch and the usage scrape: they spawn the same binary and must take
    the same tier.

    ``is_kiro_cli=True`` is explicit because ``_spawns_kiro_cli``'s basename test
    only matches a literal ``kiro-cli``: a Windows ``kiro-cli.exe``, a wrapper
    shim, or a ``KIROCREW_KIRO_BIN`` pointing at a nonstandard launch path all
    read as "not kiro-cli". The positive classification is also the security gate
    for default Windows delegation to Kiro's internal sandbox; basename inference
    cannot grant it. Both callers here spawn kiro-cli by construction, and both ACP
    spawn paths pass the same flag for the same reason.
    """
    return wrap_argv(argv, mode=configured_sandbox_mode(), is_kiro_cli=True)


#: Lowest kiro-cli release verified to accept ``chat --agent-engine v2``. Below
#: it the scrape keeps the plain argv: an unknown flag fails the whole command,
#: while an unpinned scrape fails only for a user whose global engine is V3.
#: Lowering it once an older release is verified is a one-line change.
USAGE_SCRAPE_ENGINE_PIN_MIN_VERSION: tuple[int, int, int] = (2, 28, 0)


def _usage_scrape_argv(kiro_bin: str) -> list[str]:
    """The ``/usage`` scrape argv, with the V2 engine pinned where it is accepted.

    Without a pin kiro-cli uses the user's global ``chat.agentEngine``. On V3,
    ``/usage`` is not a slash command: it goes to the model as a prompt, spends
    a model turn and prints no usage table.

    The version read runs ``--version`` outside the sandbox, so it runs only on
    the :func:`pin_kiro_cli` binary, and only when that is the binary being
    spawned. *kiro_bin* may come off the inherited ``PATH``, which can lead with
    an agent-writable directory; a binary from there gets the plain argv and is
    never run unsandboxed. Blocking: the pin stats install directories, and the
    version read is a bounded spawn on its first call per binary identity.
    """
    argv = [kiro_bin, "chat", "--no-interactive"]
    version = None
    pinned, _unpinned = pin_kiro_cli()
    if pinned is not None and os.path.realpath(pinned) == os.path.realpath(kiro_bin):
        version = kiro_cli_version_at(pinned)
    if version is not None and version >= USAGE_SCRAPE_ENGINE_PIN_MIN_VERSION:
        argv += ["--agent-engine", "v2"]
    return argv + ["--agent", "kirocrew-lite", "/usage"]


def _wrap_argv_usage_scrape(kiro_bin: str) -> tuple[list[str], str | None]:
    """Executor entrypoint for the ``/usage`` scrape's wrap (see
    :func:`_wrap_argv_at_configured_tier` for why this runs off the loop)."""
    return _wrap_argv_at_configured_tier(_usage_scrape_argv(kiro_bin))


def _wrap_argv_whoami(kiro_bin: str) -> tuple[list[str], str | None]:
    """Executor entrypoint for the ``whoami`` identity fetch's wrap (see
    :func:`_wrap_argv_at_configured_tier` for why this runs off the loop)."""
    return _wrap_argv_at_configured_tier([kiro_bin, "whoami", "--format", "json"])


async def _fetch_whoami(kiro_bin: str) -> dict[str, object]:
    """Return the signed-in identity from ``kiro-cli whoami --format json``.

    Answers "who is this Kiro account?" — the credit API cannot: GetUsageLimits
    carries no identity, and the SSO token cache holds only opaque tokens (no
    email/openid scopes). kiro-cli resolves the identity itself, so it is the
    only local source of the account email.

    Returns a dict with any of ``email`` / ``account_type`` / ``start_url``, or
    ``{}`` on any failure — identity is decorative here, so it must never break
    the credit readout. A caller that must tell "could not read" apart from
    "no identity" uses :func:`_fetch_whoami_or_none`.
    """
    return (await _fetch_whoami_or_none(kiro_bin)) or {}


async def _fetch_whoami_or_none(kiro_bin: str) -> dict[str, object] | None:
    """Run ``kiro-cli whoami --format json`` and parse it, keeping failure distinct.

    Returns the parsed identity when whoami answered (``{}`` when it exited
    cleanly reporting none), and ``None`` when nothing is known — it timed out,
    could not start, or exited nonzero without printing an identity. stdout is
    untrusted: only the LEADING JSON object is parsed (kiro-cli appends a
    non-JSON "Profile:" block after it), values must be strings, and each is
    length-bounded before it can reach the cache/UI.
    """
    proc = None
    cleanup = None
    try:
        # Configured tier, not a hardcoded "standard": this is the same binary
        # chat spawns, so it must not demand stricter isolation than chat does.
        # Where the operator set agent.sandbox="off" (isolation deferred to
        # kiro-cli's own internal sandbox), the pinned "standard" tier could
        # silently diverge from chat and drop the identity this readout labels the
        # credit numbers with. The explicit Kiro classification also lets the
        # default Windows tier delegates through Kiro's internal sandbox.
        # Off the loop: see _wrap_argv_at_configured_tier for the two blocking reads.
        argv, cleanup = await asyncio.get_running_loop().run_in_executor(
            subprocess_executor(), _wrap_argv_whoami, kiro_bin
        )
        argv = cgroup_scope_argv(argv)
        # Supervised so the call ends what it leaves behind (see
        # spawn_supervised_oneshot).
        proc = await spawn_supervised_oneshot(
            argv,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
            env=scrub_agent_subprocess_env(),
            # Windows: the gateway has no console, so a helper spawned without
            # CREATE_NO_WINDOW gets a fresh console allocated and flashes on
            # screen ~every 30s. Suppress it (no-op on POSIX, where the flag is
            # 0). Same guard the ACP spawn paths and kiro_prerequisite apply.
            creationflags=_SUBPROCESS_NO_WINDOW,
        )
        out, err = await asyncio.wait_for(proc.communicate(), timeout=30)
        raw = (out or err or b"").decode(errors="replace")
        # The JSON scrape is shared with the cloud launch paths
        # (cloud/login_target.parse_whoami_output): leading object only, string
        # values, length-bounded. The ARN lives AFTER the JSON object.
        out_map: dict[str, object] = dict(parse_whoami_output(raw))
        if not out_map:
            # Nonzero without an identity: an expired or broken session, a CLI
            # fault -- nothing is known. A clean exit with no identity is known.
            return None if proc.returncode else {}
        # whoami's own profile ARN, printed in the trailing (non-JSON) "Profile:"
        # block. Private (leading underscore): used only to prove this identity
        # belongs to the same account the credit numbers came from, and stripped
        # before anything is cached or served.
        m = re.search(r"arn:aws:codewhisperer:[^\s\"']+", raw)
        if m:
            out_map["_profile_arn"] = m.group(0)[:200]
        return out_map
    except (asyncio.TimeoutError, ValueError, OSError):
        logger.debug("whoami identity fetch failed", exc_info=True)
        return None
    except Exception:
        logger.debug("whoami identity fetch failed (unexpected)", exc_info=True)
        return None
    finally:
        if proc is not None and proc.returncode is None:
            try:
                await kill_and_reap(proc, timeout=5)
            except Exception:
                pass
        if cleanup:
            try:
                os.remove(cleanup)
            except OSError:
                pass
