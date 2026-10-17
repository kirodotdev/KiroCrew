"""Project AWS profile binding: ``GET``/``PUT /api/aws/project-profile``.

The composer's project pill shows which AWS named profile a project directory is
bound to (``aws.project_profiles``) and whether that profile's sign-in still
works; the project picker reads the profile names it can offer and writes a
binding through the same locked ``config.json`` writer every dashboard setting
uses.

Both verbs are dashboard-OWNER actions, like the paid-service consent pair: a
binding decides which account an agent's AWS commands reach by default, so it is
the operator's choice and never an app's or an allow-listed messaging user's. The
read is gated too, because it spawns the AWS CLI for the health probe. Both go
through the shared ``require_owner_dashboard_request`` gate, which audits every
refusal to the security event log.

Names only, in both directions. The response carries profile NAMES and a
closed-set health word -- never an account id, an ARN or the CLI's own failure
text -- and the write stores a name the AWS profile-name charset admits. The
health probe is ``sts:GetCallerIdentity`` through ``aws_consent.probe_identity``,
the AWS Control app's own probe, so the two surfaces never disagree.
"""

from __future__ import annotations

import asyncio
import logging
import os
from typing import Any

from aiohttp import web

from kiro_crew.aws_project_profiles import (
    PROJECT_DIR_MAX_LEN,
    PROJECT_PROFILES_KEY,
    resolve_project_binding,
)
from kiro_crew.config.loader import (
    ConfigReadError,
    ConfigWriteRefused,
    KiroCrewConfig,
    config_path,
    update_config_locked,
)
from kiro_crew.constants import AWS_PROFILE_NAME_RE
from kiro_crew.dashboard.chat_utils import run_config_write
from kiro_crew.dashboard.handlers._shared import require_owner_dashboard_request

logger = logging.getLogger(__name__)

_CODE_INVALID_JSON = "invalid_json"
_CODE_INVALID_PATH = "invalid_project_path"
_CODE_INVALID_PROFILE = "invalid_aws_profile"

#: Health words the chip renders. ``expired`` is an SSO profile whose sign-in no
#: longer resolves (``aws sso login`` fixes it); ``unavailable`` is any other
#: profile that does not resolve; ``ok`` resolved.
STATUS_OK = "ok"
STATUS_EXPIRED = "expired"
STATUS_UNAVAILABLE = "unavailable"

#: Most profile names offered in the picker.
_CHOICES_MAX = 100


def _project_path(raw: object) -> str | None:
    """*raw* as a normalized absolute directory path, or ``None`` when unusable."""
    if not isinstance(raw, str):
        return None
    # Not stripped: surrounding whitespace is part of a directory name, and dropping it
    # would bind a different directory than the one the caller named.
    path = raw
    if not path or len(path) > PROJECT_DIR_MAX_LEN or "\x00" in path:
        return None
    if not os.path.isabs(path):
        return None
    return os.path.normpath(path)


async def _deny(request: web.Request, operation: str) -> web.Response | None:
    """The shared owner gate, which audits every refusal to the security event log."""
    denied = await require_owner_dashboard_request(request, operation)
    if denied is not None:
        logger.warning(
            "refused %s: binding an AWS profile is a dashboard owner action (app=%s)",
            operation,
            request.get("app"),
        )
    return denied


def _registry_names() -> list[str]:
    """Profile names the AWS Control registry holds, charset-checked."""
    from kiro_crew.deploy import profiles as deploy_profiles

    try:
        registry = deploy_profiles.load_registry()
    except Exception:
        logger.debug("project profile: registry read failed", exc_info=True)
        return []
    names = []
    for entry in registry.get("profiles") or []:
        name = entry.get("name") if isinstance(entry, dict) else None
        if isinstance(name, str) and AWS_PROFILE_NAME_RE.match(name):
            names.append(name)
    return names


def _read_binding(path: str) -> dict[str, Any]:
    """The binding for *path* from the live config file, plus the picker's choices."""
    mapping = KiroCrewConfig.load().aws.project_profiles
    binding = resolve_project_binding(mapping, path)
    choices: list[str] = []
    for name in [*_registry_names(), *mapping.values()]:
        if name not in choices:
            choices.append(name)
    return {
        "path": path,
        "profile": binding[1] if binding else None,
        "boundDir": binding[0] if binding else None,
        "profiles": sorted(choices[:_CHOICES_MAX], key=str.lower),
    }


async def _profile_status(profile: str) -> str:
    """``ok`` | ``expired`` | ``unavailable`` for *profile*, from the shared probe."""
    from kiro_crew import aws_consent
    from kiro_crew.apps.builtins.aws_control.backend import accounts
    from kiro_crew.deploy import profiles as deploy_profiles

    try:
        region = await accounts.configured_region(profile) or deploy_profiles.DEFAULT_REGION
        identity = await aws_consent.probe_identity(profile, region)
        if identity.ok:
            return STATUS_OK
        kind = await accounts.classify_profile(profile)
    except Exception:
        logger.debug("project profile: health probe failed", exc_info=True)
        return STATUS_UNAVAILABLE
    return STATUS_EXPIRED if kind == accounts.KIND_SSO else STATUS_UNAVAILABLE


async def api_aws_project_profile_get(request: web.Request) -> web.Response:
    """GET /api/aws/project-profile?path=<dir> -- the binding and its health."""
    denied = await _deny(request, "aws_project_profile.read")
    if denied:
        return denied
    path = _project_path(request.rel_url.query.get("path"))
    if path is None:
        return web.json_response(
            {"error": "path must be an absolute directory", "code": _CODE_INVALID_PATH},
            status=400,
        )
    payload = await asyncio.to_thread(_read_binding, path)
    profile = payload["profile"]
    payload["status"] = await _profile_status(profile) if profile else None
    return web.json_response(payload)


def _resolved(path: str) -> str:
    """*path* as the matcher compares it: symlinks resolved, case folded where the OS folds."""
    return os.path.normcase(os.path.realpath(os.path.normpath(path)))


def _apply_binding(doc: dict, path: str, profile: str) -> dict | None:
    """Set (or, for ``""``, remove) the binding for exactly *path* in *doc*."""
    aws = doc.get("aws")
    if not isinstance(aws, dict):
        aws = {}
    raw = aws.get("project_profiles")
    current = dict(raw) if isinstance(raw, dict) else {}
    # Every spelling of the same directory goes -- a trailing separator, or a symlink
    # that resolves to it -- so one entry is left for it and the match cannot keep
    # answering with an older alias. Runs in the config writer's worker thread. A key
    # the PUT itself would refuse (a hand edit) is kept for the writer to refuse.
    target = _resolved(path)
    kept = {
        key: value
        for key, value in current.items()
        if not (_project_path(key) is not None and _resolved(key) == target)
    }
    if profile:
        kept[path] = profile
    if kept == current:
        return None
    aws["project_profiles"] = kept
    doc["aws"] = aws
    return doc


async def api_aws_project_profile_put(request: web.Request) -> web.Response:
    """PUT /api/aws/project-profile -- bind ``{path, profile}``; ``profile: ""`` unbinds."""
    denied = await _deny(request, "aws_project_profile.write")
    if denied:
        return denied
    try:
        body = await request.json()
    except Exception:
        return web.json_response({"error": "invalid JSON", "code": _CODE_INVALID_JSON}, status=400)
    if not isinstance(body, dict):
        return web.json_response(
            {"error": "body must be a JSON object", "code": _CODE_INVALID_JSON}, status=400
        )
    path = _project_path(body.get("path"))
    if path is None:
        return web.json_response(
            {"error": "path must be an absolute directory", "code": _CODE_INVALID_PATH},
            status=400,
        )
    profile = body.get("profile")
    if profile is None:
        profile = ""
    if not isinstance(profile, str) or (profile and not AWS_PROFILE_NAME_RE.match(profile)):
        return web.json_response(
            {
                "error": (
                    "profile must be '' or 1 to 128 letters, digits, '_', '.', '+' or '-', "
                    "not starting with '-'"
                ),
                "code": _CODE_INVALID_PROFILE,
            },
            status=400,
        )

    try:
        await run_config_write(
            update_config_locked,
            config_path(),
            mutate=lambda doc: _apply_binding(doc, path, profile),
        )
    except ConfigReadError:
        logger.warning("%s PUT: config.json is unreadable", PROJECT_PROFILES_KEY, exc_info=True)
        return web.json_response(
            {"error": "failed to read config file", "code": "config_unreadable"}, status=500
        )
    except ConfigWriteRefused as exc:
        return web.json_response({"error": str(exc), "code": "config_write_refused"}, status=400)
    except OSError:
        logger.warning("%s PUT: config.json write failed", PROJECT_PROFILES_KEY, exc_info=True)
        return web.json_response(
            {"error": "failed to write config file", "code": "config_write_failed"}, status=500
        )
    # The name only: a binding never holds a credential.
    logger.info(
        "%s: %s %s", PROJECT_PROFILES_KEY, path, f"bound to {profile!r}" if profile else "unbound"
    )
    payload = await asyncio.to_thread(_read_binding, path)
    return web.json_response(payload)
