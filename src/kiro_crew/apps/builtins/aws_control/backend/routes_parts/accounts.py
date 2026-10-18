"""Account routes: the account list, reconnect plans, the profile registry, the
bill and the IAM policy."""

from __future__ import annotations

import asyncio
import os

from aiohttp import web

from kiro_crew import aws_consent
from kiro_crew.apps.builtins.aws_control.backend import accounts as accounts_mod
from kiro_crew.apps.builtins.aws_control.backend import costs as costs_mod
from kiro_crew.apps.builtins.aws_control.backend.routes_parts.common import (
    _account_target,
    _aws_failed,
    _bad_request,
    _body,
    _consent,
    _not_found,
    _safe_error,
    _unavailable,
    _unsupported,
    logger,
)
from kiro_crew.deploy import profiles as deploy_profiles
from kiro_crew.deploy.engine import AWSError

# --------------------------------------------------------------------------
# Accounts + reconnect (foundations)
# --------------------------------------------------------------------------


async def _handle_accounts(request: web.Request) -> web.Response:
    refresh = request.rel_url.query.get("refresh") == "1"
    snapshot = await accounts_mod.list_accounts(refresh=refresh)
    return web.json_response(snapshot)


async def _handle_reconnect_plan(request: web.Request) -> web.Response:
    """Classification + guidance, no action. Registered profiles only, so
    attacker-shaped names are never echoed into a guidance card."""
    name = request.match_info.get("name", "")
    reg = await asyncio.to_thread(deploy_profiles.load_registry)
    entry = deploy_profiles.get_entry(reg, name)
    if entry is None:
        return _not_found("unknown profile", "unknown_profile")
    kind = await accounts_mod.classify_profile(name)
    plan = accounts_mod.reconnect_plan(kind, name)
    return web.json_response(plan)


#: Ceiling on registered profiles, matching the deploy registry's own cap. The
#: portal must not be the surface that grows the registry past what deploy will
#: accept, or a profile registered here becomes unusable there.
_MAX_REGISTERED = 50


async def _handle_profiles_available(request: web.Request) -> web.Response:  # noqa: ARG001
    """Profile NAMES the AWS CLI knows, annotated with whether we registered them.

    This is the answer to "why can I not see my accounts": the portal only ever
    showed the REGISTERED set, and nothing on the page listed what else exists,
    so an operator with many local profiles had no way in.

    Names only, and not read from disk -- ``discover_aws_profiles`` shells
    ``aws configure list-profiles``, which enumerates the sections itself, so no
    credential file is ever opened here. The call is local and free, so it needs
    no consent gate. Names still go through the redactor: a profile name is
    operator-authored text on a display path, the same reason the account
    snapshot redacts its own.
    """
    available = await asyncio.to_thread(deploy_profiles.discover_aws_profiles)
    # Discovery is POSIX-only, so the two reasons it returns nothing get
    # different answers. On Windows the platform is the more useful thing to
    # report and the payload already carries it, so that case keeps its 200 and
    # its own copy (which names WSL). Anywhere else, `None` means the scan could
    # not run: the frontend has a retryable notice for a failed scan and reads a
    # 200 as the authoritative list, so returning one here is what turns "could
    # not look" into "you have no profiles".
    supported = os.name != "nt"
    if available is None and supported:
        return _unavailable(
            "could not list this machine's AWS profiles",
            "profiles_unavailable",
        )
    reg = await asyncio.to_thread(deploy_profiles.load_registry)
    registered = {str(p.get("name", "")) for p in reg.get("profiles", [])}
    rows = [
        {"name": accounts_mod._safe_field(name), "registered": name in registered}
        for name in (available or [])
        if aws_consent._PROFILE_RE.match(name)
    ]
    return web.json_response(
        {
            "profiles": rows,
            "registeredCount": len(registered),
            "max": _MAX_REGISTERED,
            # `profiles` is empty in two cases, and `supported` is what tells
            # them apart. False: Windows, where discovery cannot run at all, so
            # the UI says that instead of implying the operator has none. True:
            # the scan ran and the machine really has nothing left to register.
            # A scan that could NOT run where it should have never reaches here,
            # because it answered 503 above.
            "supported": supported,
        }
    )


async def _handle_profiles_register(request: web.Request) -> web.Response:
    """Register selected LOCAL profiles into the registry the portal reads.

    Three refusals matter more than the happy path:

    * A name must be one ``aws configure list-profiles`` actually reports. The
      registry is agent-writable and its names reach an argv, so accepting an
      arbitrary string here would let a caller plant one -- the same class the
      read-side validation in ``accounts.py`` closes from the other end. When
      that listing could not run at all the batch is refused on that fact, not
      as unknown: an unchecked name and an absent one are different refusals,
      and only one of them tells the operator something true.
    * The name must match the shared profile pattern, checked before it is
      compared against anything.
    * The registry cap is enforced across the whole batch, so a partial batch
      registers the prefix that fits rather than silently overflowing.

    Registration records NO account id: the account is whatever a live identity
    probe resolves, and writing a guessed one would seed the very stale mapping
    the drive routes re-probe to avoid. The REGION is the opposite case and is
    recorded: it is a value the profile states about itself, and leaving it empty
    makes ``make_entry`` substitute ``DEFAULT_REGION``, which would create the
    drive bucket in the wrong region for any profile configured elsewhere.
    """
    body = await _body(request)
    raw = body.get("names")
    if not isinstance(raw, list) or not raw:
        return _bad_request("names must be a non-empty list", "invalid_names")
    requested = [str(n) for n in raw][:_MAX_REGISTERED]

    discovered = await asyncio.to_thread(deploy_profiles.discover_aws_profiles)
    if discovered is None:
        # Nothing can be checked against a scan that did not run, and refusing
        # the names as absent is the one answer the operator cannot act on: the
        # profiles ARE on the machine, and the message would say they are not.
        # Refuse the batch on the real reason instead, split the way the listing
        # above splits it -- 503 asks for a retry, which clears a failed scan and
        # never clears a platform that cannot scan at all. The listing answers
        # Windows with a 200 because a list plus `supported: false` is a complete
        # answer; registering is an action this platform cannot perform, so here
        # it is an error either way and only the retry semantics differ.
        if os.name == "nt":
            return _unsupported(
                "profile discovery is not supported on Windows, so no name can be checked",
                "unsupported_platform",
            )
        return _unavailable(
            "could not list this machine's AWS profiles, so no name can be checked",
            "profiles_unavailable",
        )
    available = set(discovered)
    unknown = [n for n in requested if n not in available]
    if unknown:
        # Deliberately does not echo the rejected names: they are caller-supplied
        # and this response is a display surface.
        return _bad_request(
            f"{len(unknown)} name(s) are not profiles on this machine",
            "unknown_profile",
        )
    if any(not aws_consent._PROFILE_RE.match(n) for n in requested):
        return _bad_request("a profile name is not in the accepted form", "invalid_names")

    # Read each profile's own declared region BEFORE taking the registry lock:
    # these are subprocess round trips and must not be held under it.
    regions = {n: await accounts_mod.configured_region(n) for n in requested}

    added: list[str] = []
    skipped: list[str] = []

    def _mutate() -> None:
        with deploy_profiles.locked_registry() as reg:
            existing = {str(p.get("name", "")) for p in reg.get("profiles", [])}
            for name in requested:
                if name in existing:
                    skipped.append(name)
                    continue
                if len(reg["profiles"]) >= _MAX_REGISTERED:
                    skipped.append(name)
                    continue
                reg["profiles"].append(deploy_profiles.make_entry(name, regions.get(name, "")))
                existing.add(name)
                added.append(name)
            if not reg.get("default") and reg["profiles"]:
                reg["default"] = reg["profiles"][0]["name"]

    await asyncio.to_thread(_mutate)
    # The snapshot is TTL-cached, so without this the page the operator just
    # registered from would keep showing the old set for up to five minutes.
    accounts_mod.invalidate_cache()
    return web.json_response({"added": len(added), "skipped": len(skipped)})


async def _handle_profiles_unregister(request: web.Request) -> web.Response:
    """Drop selected profiles from the registry the portal reads.

    Registry-only, by construction: nothing on this path reaches
    ``create_aws_profile`` (the module's one ``aws configure`` writer) or the
    AWS CLI, so ``~/.aws/config``, ``~/.aws/credentials``, and every AWS
    resource the account holds are untouched. The drive bucket keeps billing
    until the operator deletes it in AWS; the share, library, and backup
    ledgers stay because they describe that bucket, not the key, and must
    render unchanged when the key is registered again.

    Names are validated against the shared profile pattern but NOT against
    ``discover_aws_profiles``: a profile already deleted from ``~/.aws`` is
    exactly the stale entry the operator most wants gone.

    Every consent grant naming a removed profile is withdrawn BEFORE the
    registry entry goes, so a later re-registration under the same name
    starts unconsented even when the request dies between the two writes: a
    withdrawal that fails leaves the profile registered and the operator
    retries, whereas the reverse order would leave an unregistered profile
    still holding an authorization. A grant re-recorded for the profile after
    the withdrawal but before the registry write is inert on its own -- every
    AWS Control call resolves account-to-profile through the registry, and the
    deploy engine refuses an unregistered profile -- and the operator can see
    and withdraw it from the usage receipts.
    """
    body = await _body(request)
    raw = body.get("names")
    if not isinstance(raw, list) or not raw:
        return _bad_request("names must be a non-empty list", "invalid_names")
    requested = [str(n) for n in raw][:_MAX_REGISTERED]
    if any(not aws_consent._PROFILE_RE.match(n) for n in requested):
        return _bad_request("a profile name is not in the accepted form", "invalid_names")

    targets = set(requested)
    removed: list[str] = []
    skipped: list[str] = []

    def _mutate() -> None:
        with deploy_profiles.locked_registry() as reg:
            kept: list[dict[str, str]] = []
            for entry in reg.get("profiles", []):
                name = str(entry.get("name", ""))
                if name in targets:
                    removed.append(name)
                else:
                    kept.append(entry)
            skipped.extend(sorted(targets - set(removed)))
            reg["profiles"] = kept
            # The default must keep naming a registered profile: the nightly
            # backup and the deploy engine both resolve through it.
            if reg.get("default") not in {str(p.get("name", "")) for p in kept}:
                reg["default"] = str(kept[0].get("name", "")) if kept else ""

    registered = {
        str(p.get("name", ""))
        for p in (await asyncio.to_thread(deploy_profiles.load_registry)).get("profiles", [])
    }
    if not targets & registered:
        # A registry that never held any of the names has nothing to remove;
        # the names are caller-supplied and are deliberately not echoed.
        return _not_found("no such registered profile", "unknown_profile")

    withdrawn: list[str] = []
    for name in sorted(targets & registered):
        try:
            withdrawn.extend(await asyncio.to_thread(aws_consent.revoke_for_profile, name))
        except (OSError, ValueError):
            # The consent store could not be read (or parsed) or rewritten, so the
            # profile is left registered rather than removed while a grant it
            # names may still be on disk. 500, not 502: no AWS call was made, and
            # the fix is local (the store's file).
            logger.warning(
                "consent withdrawal failed for profile %r; profile left registered", name
            )
            return web.json_response(
                {"error": "consent could not be withdrawn", "code": "consent_unwritable"},
                status=500,
            )
    await asyncio.to_thread(_mutate)
    # Same TTL cache as registration: the page must not show a removed key for
    # up to five minutes after the operator removed it.
    accounts_mod.invalidate_cache()
    return web.json_response(
        {"removed": len(removed), "skipped": len(skipped), "consentWithdrawn": sorted(withdrawn)}
    )


# --------------------------------------------------------------------------
# Costs (Bill)
# --------------------------------------------------------------------------


async def _handle_costs(request: web.Request) -> web.Response:
    target = await _account_target(request)
    if isinstance(target, web.Response):
        return target
    account, profile, region = target
    cached = await asyncio.to_thread(costs_mod.read_cached, account)
    refresh = request.rel_url.query.get("refresh") == "1"
    if costs_mod.is_fresh(cached) and not refresh:
        return web.json_response({"fresh": True, **(cached or {})})
    denied = await _consent(aws_consent.SERVICE_COST_EXPLORER, profile, region)
    if denied:
        # Stale-but-present beats nothing: label the age, keep the page alive.
        if cached:
            return web.json_response({"fresh": False, "consentMissing": True, **cached})
        return denied
    try:
        result = await asyncio.to_thread(costs_mod.fetch_month_costs, profile, region, account)
    except AWSError as exc:
        if cached:
            return web.json_response({"fresh": False, "fetchError": _safe_error(exc), **cached})
        return _aws_failed(exc)
    return web.json_response({"fresh": True, **result})


# --------------------------------------------------------------------------
# IAM policy (local render — what the user pastes into their account)
# --------------------------------------------------------------------------


async def _handle_iam_policy(request: web.Request) -> web.Response:
    from kiro_crew.deploy import iam

    policy = await asyncio.to_thread(iam.policy_json, tier="drive")
    return web.json_response({"policy": policy})
