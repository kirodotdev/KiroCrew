"""Account center — aggregate the deploy profile registry by AWS account.

The registry (``deploy/profiles.py``) is profile-shaped: a flat list of
``{name, region, account, verified_at, note}`` entries. The portal is
account-shaped: an account owns one or more profiles ("keys"), and the page
shows one row per account. This module is the fold between the two.

Everything here is read-only against AWS and free: the only call made is
``sts:GetCallerIdentity`` (via :func:`kiro_crew.aws_consent.probe_identity`,
shared with the consent surface so the two never disagree about what a
profile resolves to), plus ``aws configure get`` reads to classify HOW a
profile authenticates. Both go through the deploy engine's sandboxed CLI
chokepoint; neither reads a credential file.

Names-only invariant (inherited, load-bearing): this module stores profile
names, regions, probe outcomes and display metadata. It never reads, writes,
or caches credential material, and it never mutates the registry — the deploy
surface stays the single writer.

Probe results are cached in-process (:data:`_PROBE_TTL_SECS`) because the
page re-renders far more often than credentials change state; ``refresh=1``
bypasses the cache for the explicit "check again" click.
"""

from __future__ import annotations

import asyncio
import logging
import time
from dataclasses import dataclass, field
from typing import Any

from kiro_crew import aws_consent
from kiro_crew.deploy import engine
from kiro_crew.deploy import profiles as deploy_profiles
from kiro_crew.loop_lock import LoopBoundLock

logger = logging.getLogger(__name__)

#: How long an aggregated account snapshot stays served without re-probing.
#: Long enough to absorb page re-renders and tab switches, short enough that
#: an expired SSO session is noticed within minutes without the user clicking
#: anything.
_PROBE_TTL_SECS = 300.0

#: At most this many identity probes run concurrently. Each probe is an AWS
#: CLI subprocess; a registry of dozens of profiles must not fork them all at
#: once.
_PROBE_CONCURRENCY = 4

#: ``aws configure get`` reads used to classify a profile's auth mechanism.
#: Values are setting names passed to the CLI — the CLI parses the config
#: files itself, so the names-only invariant holds.
_KIND_SSO_SESSION = "sso_session"
_KIND_SSO_START_URL = "sso_start_url"
_KIND_CREDENTIAL_PROCESS = "credential_process"

#: Profile auth kinds, in the order the UI cares about them.
KIND_SSO = "sso"
KIND_CREDENTIAL_PROCESS = "credential-process"
KIND_OTHER = "other"

#: LoopBoundLock, not asyncio.Lock: a module-global asyncio primitive binds
#: to the import-time loop and raises when acquired from another (#4800).
_snapshot_lock = LoopBoundLock()
_snapshot: dict[str, Any] | None = None
_snapshot_at: float = 0.0


def _safe_field(text: str) -> str:
    """Scrub one externally-authored display string.

    The profile registry is agent-writable and probe output is verbatim CLI
    stdout/stderr, so every string this module serializes is untrusted text on
    an output path. Same double pass the app's HTTP error surface uses.

    This module is a STAGING point, not an egress boundary: it owns no output,
    and the snapshot reaches a client only through ``routes.py`` (the module
    registered as this app's redaction sink). It is allowlisted in
    ``NON_EGRESS_REDACTION_MODULES`` on exactly that ground.
    """
    from kiro_crew.security import redact_credentials, redact_exfiltration_urls

    text, _ = redact_credentials(text)
    text, _ = redact_exfiltration_urls(text)
    return text


@dataclass
class ProfileView:
    """One registry profile, folded with its live probe outcome."""

    name: str
    region: str
    kind: str = KIND_OTHER
    identity_ok: bool = False
    account: str = ""
    arn: str = ""
    detail: str = ""
    is_default: bool = False

    def to_dict(self) -> dict[str, Any]:
        # EVERY string field here is externally authored: `name`/`region` come
        # from the agent-writable profile registry, and `account`/`arn`/`detail`
        # are verbatim CLI stdout/stderr. Redact the whole set rather than the
        # fields that look risky - a per-field judgement is what let `region`
        # through the first time. Same double pass as every other egress
        # surface; `kind` and the booleans are backend-chosen enums.
        return {
            "name": _safe_field(self.name),
            "region": _safe_field(self.region),
            "kind": self.kind,
            "identityOk": self.identity_ok,
            "account": _safe_field(self.account),
            "arn": _safe_field(self.arn),
            "detail": _safe_field(self.detail),
            "default": self.is_default,
        }


@dataclass
class AccountView:
    """One AWS account row: every profile that resolved to it."""

    account: str
    profiles: list[ProfileView] = field(default_factory=list)

    @property
    def display_name(self) -> str:
        """The human name the row leads with.

        The registry default's profile name when it belongs to this account,
        else the first profile's — an account is always reached THROUGH a
        named key, and that name is what the user recognises. A nickname
        field can override this later without changing the payload shape.
        """
        chosen = next((p for p in self.profiles if p.is_default), None)
        chosen = chosen or (self.profiles[0] if self.profiles else None)
        return chosen.name if chosen else ""

    @property
    def health(self) -> str:
        """``ok`` | ``degraded`` | ``unknown`` — the one light per account.

        ``unknown`` is reserved for the pseudo-row of profiles that could not
        be resolved to any account (probe failed AND the registry never
        recorded one) — there is nothing to be healthy ABOUT. A known account
        with any failing profile is ``degraded``: the account itself was
        reachable once, and at least one of its keys is not working now.
        """
        if not self.account:
            return "unknown"
        if all(p.identity_ok for p in self.profiles):
            return "ok"
        return "degraded"

    def to_dict(self) -> dict[str, Any]:
        return {
            "account": _safe_field(self.account),
            "name": _safe_field(self.display_name),
            "health": self.health,
            "profiles": [p.to_dict() for p in self.profiles],
            # P1 fills these from real reads (storage scan, CE). Explicit
            # nulls rather than zeros: the page must render "not measured
            # yet", never a fake $0.00.
            "summary": {
                "storage": None,
                "sites": None,
                "tasks": None,
                "costMonthToDate": None,
            },
        }


async def classify_profile(name: str) -> str:
    """How ``name`` authenticates, from CLI config reads only.

    The name is re-validated HERE, not only at registration: the registry
    file is agent-writable config, so a hostile entry written out-of-band
    must not reach an argv (or, worse, the display command a user copies
    into a shell). An invalid name classifies as ``other`` — fail closed.

    ``aws configure get`` exits 0 with the value when the setting exists and
    non-zero when it does not — that exit code is the whole classification.
    The value itself is discarded: an SSO start URL or a credential_process
    command line is configuration the user wrote, but the portal only needs
    the SHAPE (which Reconnect path applies), so nothing beyond the kind is
    kept or returned.
    """
    if not aws_consent._PROFILE_RE.match(name or ""):
        return KIND_OTHER
    for setting, kind in (
        (_KIND_SSO_SESSION, KIND_SSO),
        (_KIND_SSO_START_URL, KIND_SSO),
        (_KIND_CREDENTIAL_PROCESS, KIND_CREDENTIAL_PROCESS),
    ):
        try:
            rc, out, _err = await asyncio.to_thread(
                engine.run_aws, ["configure", "get", setting], name, 10
            )
        except Exception:
            # A broken CLI resolution must degrade ONE profile's badge, not
            # crash the whole listing (the probe already reports health).
            logger.debug("classify_profile failed for %s", name, exc_info=True)
            return KIND_OTHER
        if rc == 0 and (out or "").strip():
            return kind
    return KIND_OTHER


async def configured_region(name: str) -> str:
    """The region this profile itself declares, or "" when it declares none.

    Registration needs this and must not substitute a default for it. A profile
    configured for eu-central-1 registered with an empty region falls back to
    ``DEFAULT_REGION`` in ``make_entry``, and the drive bucket then gets created
    in the WRONG region -- a mistake that is expensive to undo once objects
    exist. Unlike the account id (which is whatever a live probe resolves, so
    guessing it would seed a stale mapping), the region is a value the profile
    states about itself and is authoritative.

    Read through the same sandboxed ``aws configure get`` chokepoint the auth
    classifier uses, so the names-only invariant holds -- the CLI parses the
    config files, this never opens them. The value is validated against the
    shared region pattern before it is trusted: the config file is
    operator-writable text that would otherwise flow into an argv.
    """
    try:
        rc, out, _err = await asyncio.to_thread(
            engine.run_aws, ["configure", "get", "region"], name, 10
        )
    except Exception:
        logger.debug("configured_region failed for %s", name, exc_info=True)
        return ""
    if rc != 0:
        return ""
    value = (out or "").strip()
    return value if deploy_profiles._REGION_RE.match(value) else ""


def reconnect_plan(kind: str, name: str) -> dict[str, Any]:
    """What "Reconnect" can honestly offer for a profile of ``kind``.

    P0 answers the feasibility question without performing anything:

    * ``sso`` — re-auth is a device flow (``aws sso login``) that PRINTS a URL
      and code for the user's browser. The gateway can run it in a later
      phase; until that lands, the honest offer is the exact command.
    * ``credential-process`` — the process is the user's own tooling; only
      terminal guidance applies.
    * ``other`` — static keys or an ambient chain; nothing to re-run, point
      at ``aws configure``.

    The returned ``command`` is DISPLAY text for the guidance card — a user
    will paste it into a terminal, so the name is constrained to the AWS
    profile charset (no whitespace, no shell metacharacters) before it may
    appear here. A name failing that shape yields guidance with no command.
    """
    if not aws_consent._PROFILE_RE.match(name or ""):
        return {"method": "terminal", "kind": KIND_OTHER, "command": ""}
    # The charset gate above is a SHELL-SAFETY check, not a secret check: it
    # admits `[A-Za-z0-9._@=+-]` up to 128 chars, which is exactly the shape an
    # access key id or a base64 secret has, so a profile a user named after a
    # credential passes it and would render verbatim into this card and the
    # clipboard. Redact the interpolated name through the same chain every other
    # display field in this module uses.
    name = _safe_field(name)
    if kind == KIND_SSO:
        return {
            "method": "terminal",
            "kind": kind,
            "command": f"aws sso login --profile {name}",
        }
    if kind == KIND_CREDENTIAL_PROCESS:
        return {
            "method": "terminal",
            "kind": kind,
            "command": f"aws sts get-caller-identity --profile {name}",
        }
    return {
        "method": "terminal",
        "kind": kind,
        "command": f"aws configure --profile {name}",
    }


async def _fold_profile(entry: dict[str, str], default_name: str) -> ProfileView:
    """Probe + classify one registry entry into its view."""
    name = entry["name"]
    region = entry.get("region") or deploy_profiles.DEFAULT_REGION
    identity = await aws_consent.probe_identity(name, region)
    kind = await classify_profile(name)
    return ProfileView(
        name=name,
        region=region,
        kind=kind,
        identity_ok=identity.ok,
        # A failed probe says nothing about which account the profile is FOR;
        # fall back to the account the registry recorded at verify time so the
        # row stays grouped where the user last saw it.
        account=identity.account or entry.get("account", ""),
        arn=identity.arn,
        detail=identity.detail,
        is_default=(name == default_name),
    )


async def _build_snapshot() -> dict[str, Any]:
    reg = await asyncio.to_thread(deploy_profiles.load_registry)
    default_name = reg.get("default", "")
    entries = reg.get("profiles", [])

    sem = asyncio.Semaphore(_PROBE_CONCURRENCY)

    async def _bounded(entry: dict[str, str]) -> ProfileView:
        async with sem:
            return await _fold_profile(entry, default_name)

    views = list(await asyncio.gather(*(_bounded(e) for e in entries)))

    by_account: dict[str, AccountView] = {}
    for view in views:
        row = by_account.setdefault(view.account, AccountView(account=view.account))
        row.profiles.append(view)

    # Known accounts first (registry order within), the unresolved pseudo-row
    # last — it is the row that only offers Reconnect.
    ordered = sorted(by_account.values(), key=lambda a: (a.account == "",))
    healthy = sum(1 for v in views if v.identity_ok)
    return {
        "accounts": [a.to_dict() for a in ordered],
        "totals": {
            "accounts": sum(1 for a in ordered if a.account),
            "profiles": len(views),
            "profilesHealthy": healthy,
        },
        "generatedAt": deploy_profiles.now_iso(),
    }


async def list_accounts(*, refresh: bool = False) -> dict[str, Any]:
    """The aggregated account snapshot, cached for :data:`_PROBE_TTL_SECS`.

    The lock makes concurrent first-loads coalesce into one probe sweep
    instead of forking one sweep per open tab.
    """
    global _snapshot, _snapshot_at
    async with _snapshot_lock:
        fresh = _snapshot is not None and (time.monotonic() - _snapshot_at) < _PROBE_TTL_SECS
        if fresh and not refresh:
            assert _snapshot is not None
            return _snapshot
        snapshot = await _build_snapshot()
        _snapshot = snapshot
        _snapshot_at = time.monotonic()
        return snapshot


def _pick_profile(snapshot: dict[str, Any], account: str) -> tuple[str, str] | None:
    """Choose the working (profile, region) for ``account`` from a snapshot.

    Preference order: the registry default when it belongs to this account
    and probes healthy, then any healthy profile, then None — an account
    with no working key gets NO silent fallback to a different account's
    credentials.

    Factored out so the async reader and the sync one below cannot drift: this
    is the policy that decides WHICH credentials an operation runs under, and
    two copies of it is two places for that decision to diverge.
    """
    if not account:
        return None
    for row in snapshot.get("accounts", []):
        if row.get("account") != account:
            continue
        healthy = [p for p in row.get("profiles", []) if p.get("identityOk")]
        chosen = next((p for p in healthy if p.get("default")), None) or (
            healthy[0] if healthy else None
        )
        if chosen is None:
            return None
        return chosen["name"], chosen["region"]
    return None


async def resolve_account_profile(account: str) -> tuple[str, str] | None:
    """The working (profile, region) for operations on ``account``."""
    if not account:
        return None
    return _pick_profile(await list_accounts(), account)


def resolve_account_profile_cached(account: str) -> tuple[str, str] | None:
    """Sync :func:`resolve_account_profile`, served from the warm snapshot only.

    For a caller that is a WORKER THREAD and therefore cannot await: the Job SDK
    runners, which are plain ``def`` by the SDK's contract. The async twin cannot
    be reached from there — ``asyncio.run`` in a worker thread builds a second
    event loop, and a module-global asyncio primitive binds to the loop that
    imported it and raises when acquired from another (#4800, which is why
    ``_snapshot_lock`` is a :class:`LoopBoundLock` at all). So this reads the
    snapshot the loop already built rather than building one of its own.

    Returns None when the snapshot is absent or past its TTL, which the caller
    must treat as "no working connection for this account" and refuse. That is
    the honest answer: a probe sweep is exactly the async work this cannot do.
    In practice the owner-driven path has just warmed it — the route's
    pre-flight awaits :func:`resolve_account_profile` before starting the run.

    Staleness cannot cause a WRONG-ACCOUNT operation: every caller re-verifies
    the live account with ``sts:GetCallerIdentity`` through the package's sync
    chokepoint immediately before it acts (see ``backup._authorize_upload``).
    This resolves which credentials to TRY; that check decides whether they may
    be used.
    """
    snapshot = _snapshot
    if snapshot is None or (time.monotonic() - _snapshot_at) >= _PROBE_TTL_SECS:
        return None
    return _pick_profile(snapshot, account)


def invalidate_cache() -> None:
    """Drop the snapshot (tests, and any future registry-mutation hook)."""
    global _snapshot, _snapshot_at
    _snapshot = None
    _snapshot_at = 0.0
