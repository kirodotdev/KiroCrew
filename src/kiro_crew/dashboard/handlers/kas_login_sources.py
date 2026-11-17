"""Which stored Kiro sign-in sessions run as, for the Kiro sign-in card.

``GET /api/kas-login/sources`` answers what the card cannot learn from
``GET /api/kas-login``: whether kiro-cli is signed in beside Kiro Crew's own
sign-in, which of the two sessions use, and whether they are one account.

A session uses Kiro Crew's stored sign-in when there is one and kiro-cli's only
when there is none (:func:`kiro_crew.lts_sign_in.select`). This handler reads the
same two stores by presence (:func:`kiro_crew.lts_sign_in.stored_sign_ins`) and
changes nothing about that choice.

What the answer may carry is fixed by :data:`_ACCOUNT_FIELDS`: an account's kind,
the host of its sign-in portal, and its e-mail when kiro-cli reports one. Never a
token, a profile ARN or any other identifier. The e-mail goes to the dashboard
owner only and is never logged.

``kiro-cli whoami`` is the one source of kiro-cli's account name and it can take
many seconds, so it never runs inside a request: the first request for a stored
kiro-cli sign-in starts it and answers ``name_pending``, and a later request
returns what it read. A read that fails names nothing and says so
(``name_unreadable``); whether kiro-cli is signed in comes from the store read
alone, so a slow or failed ``whoami`` is never "signed out".

Whether the two are one account is :func:`kiro_crew.lts_sign_in.same_account`'s
answer alone, given what each side names (its kind and portal host).
"""

from __future__ import annotations

import asyncio
import logging
from typing import Any, Optional
from urllib.parse import urlsplit

from aiohttp import web

from kiro_crew.dashboard.handlers.source_providers import is_owner_dashboard_request

logger = logging.getLogger(__name__)

#: ``request.app`` key of the one cached ``whoami`` reading (see :func:`_kiro_cli_name`).
_NAME_CACHE_KEY = "kas_login_sources_kiro_cli_name"

#: Every field an account may be described with, and its longest value.
_ACCOUNT_FIELDS = {"account_type": 60, "portal_host": 200, "email": 254}

ACTIVE_KIRO_CREW = "kiro_crew"
ACTIVE_KIRO_CLI = "kiro_cli"
ACTIVE_NONE = "none"
ACTIVE_UNKNOWN = "unknown"


def _portal_host(start_url: object) -> str:
    """The host of a sign-in portal URL, ``""`` when it has none."""
    if not isinstance(start_url, str) or not start_url:
        return ""
    try:
        return (urlsplit(start_url).hostname or "")[: _ACCOUNT_FIELDS["portal_host"]]
    except ValueError:
        return ""


def _account(identity: Optional[dict[str, object]], *, with_email: bool) -> dict[str, str]:
    """The fields of *identity* this route may publish, bounded. Nothing else passes."""
    identity = identity or {}
    out: dict[str, str] = {}
    account_type = identity.get("account_type")
    if isinstance(account_type, str) and account_type:
        out["account_type"] = account_type[: _ACCOUNT_FIELDS["account_type"]]
    host = _portal_host(identity.get("start_url"))
    if host:
        out["portal_host"] = host
    email = identity.get("email")
    if with_email and isinstance(email, str) and email:
        out["email"] = email[: _ACCOUNT_FIELDS["email"]]
    return out


def _kiro_cli_usable(entry: Any) -> bool:
    """Could a session run on kiro-cli's stored sign-in *entry*?

    LTS: only while its access token is live. The gate presents that token as
    stored and Kiro Crew never renews it (:func:`kiro_crew.lts_sign_in.resolve`).
    oss: whenever one is stored, the way the relay decides
    (``acp.kas_route.kiro_cli_signed_out``, by presence); kiro-cli renews its own
    token when it runs.
    """
    from kiro_crew import edition

    return entry is not None and (entry.live or not edition.is_lts())


def _read_stores() -> tuple[Any, dict[str, object], bool]:
    """Both stores, Kiro Crew's account fields, and whether kiro-cli's sign-in would run.

    Blocking file IO -- call off the loop.
    """
    from kiro_crew import lts_sign_in
    from kiro_crew.agent_sdk.kas_bundle import relay_runs_on_kiro_cli_sign_in
    from kiro_crew.dashboard.handlers.kiro_vault_identity import vault_identity_snapshot_sync

    stored = lts_sign_in.stored_sign_ins()
    crew_fields: dict[str, object] = {}
    if stored.vault is not None:
        crew_fields = vault_identity_snapshot_sync()[1]
    relay = False
    if _kiro_cli_usable(stored.kiro_cli):
        relay = relay_runs_on_kiro_cli_sign_in()
    return stored, crew_fields, relay


async def _whoami() -> Optional[dict[str, object]]:
    """kiro-cli's own account, as ``kiro-cli whoami`` names it, or ``None``."""
    from kiro_crew.agent_sdk.drivers.acp import resolve_kiro_bin_for_spawn
    from kiro_crew.dashboard.handlers.sessions import _fetch_whoami_or_none

    kiro_bin = await resolve_kiro_bin_for_spawn()
    if not kiro_bin:
        return None
    return await _fetch_whoami_or_none(kiro_bin)


async def _fill_name(cache: dict[str, Any]) -> None:
    try:
        cache["identity"] = await _whoami()
    except Exception:  # noqa: BLE001 - a name that cannot be read is no name
        logger.debug("kiro-cli account name could not be read", exc_info=True)
    # ``None`` is "nothing is known"; ``{}`` is whoami answering that it names no one.
    cache["unreadable"] = cache["identity"] is None


def _kiro_cli_name(app: Any, stamp: str) -> tuple[Optional[dict[str, object]], bool, bool]:
    """The cached ``whoami`` reading for the kiro-cli sign-in *stamp* names, whether
    it is still being read, and whether the finished read failed.

    One reading is kept per gateway process, for the stored entry it was read for;
    a different entry (another account, or a renewed token) starts a new read.
    shortcut: a failed read is kept until the stored entry changes; add a retry
    if people report a name that stays missing.
    """
    cache = app.get(_NAME_CACHE_KEY)
    if not isinstance(cache, dict) or cache.get("stamp") != stamp:
        cache = {"stamp": stamp, "identity": None, "unreadable": False}
        cache["task"] = asyncio.ensure_future(_fill_name(cache))
        app[_NAME_CACHE_KEY] = cache
    pending = not cache["task"].done()
    return cache["identity"], pending, bool(cache["unreadable"]) and not pending


async def api_kas_login_sources(request: web.Request) -> web.Response:
    """GET /api/kas-login/sources -- which stored sign-in sessions run as."""
    from kiro_crew import edition, lts_sign_in

    try:
        stored, crew_fields, relay = await asyncio.to_thread(_read_stores)
    except Exception:  # noqa: BLE001 - never an anonymous 500 for a status read
        logger.debug("could not read the stored sign-ins", exc_info=True)
        return web.json_response(
            {"error": "Could not read the stored sign-ins.", "code": "sign_in_sources_failed"},
            status=500,
        )
    owner = is_owner_dashboard_request(request)
    crew = _account(crew_fields, with_email=False)
    cli: dict[str, str] = {}
    identity: Optional[dict[str, object]] = None
    pending = unreadable = False
    if stored.kiro_cli is not None:
        identity, pending, unreadable = _kiro_cli_name(request.app, stored.kiro_cli.stamp)
        cli = _account(identity, with_email=owner)
    same: Optional[bool] = None
    if stored.vault is not None and stored.kiro_cli is not None:
        named = _account(identity, with_email=False)
        same = lts_sign_in.same_account(
            stored.vault.kind,
            stored.vault.profile_arn,
            stored.kiro_cli.kind,
            stored.kiro_cli.profile_arn,
            vault_type=crew.get("account_type", ""),
            cli_type=named.get("account_type", ""),
            vault_portal=crew.get("portal_host", ""),
            cli_portal=named.get("portal_host", ""),
        )
    if stored.vault is not None:
        active = ACTIVE_KIRO_CREW
    elif not stored.vault_readable or not stored.kiro_cli_readable:
        active = ACTIVE_UNKNOWN
    elif relay:
        active = ACTIVE_KIRO_CLI
    else:
        active = ACTIVE_NONE
    # Offer the switch only to a kiro-cli sign-in the LTS gate would then accept: one
    # with no profile, or one outside the account the lock pinned, would close it.
    # oss has neither rule (no gate, no account lock), so its answer is unchanged.
    # Only the owner may run the sign-out the switch needs, so no one else is offered it.
    lts = edition.is_lts()
    policy = lts_sign_in.LTS if lts else lts_sign_in.OSS
    can_use_kiro_cli = bool(
        owner
        and stored.vault is not None
        and relay
        and stored.kiro_cli is not None
        and lts_sign_in.profile_eligible(stored.kiro_cli.profile_arn, policy)
    )
    if can_use_kiro_cli and lts and stored.kiro_cli is not None:
        try:
            can_use_kiro_cli = await asyncio.to_thread(
                lts_sign_in.account_lock_allows, stored.kiro_cli.profile_arn
            )
        except Exception:  # noqa: BLE001 - a lock that cannot be read offers nothing
            logger.debug("could not read the account lock", exc_info=True)
            can_use_kiro_cli = False
    return web.json_response(
        {
            "active": active,
            "kiro_crew": {
                "stored": (stored.vault is not None) if stored.vault_readable else None,
                **crew,
            },
            "kiro_cli": {
                "stored": (stored.kiro_cli is not None) if stored.kiro_cli_readable else None,
                "usable": _kiro_cli_usable(stored.kiro_cli),
                **cli,
            },
            "name_pending": pending,
            "name_unreadable": unreadable,
            "same_account": same,
            "can_use_kiro_cli": can_use_kiro_cli,
        }
    )
