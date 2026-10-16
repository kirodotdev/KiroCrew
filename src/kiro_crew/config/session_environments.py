"""Validation for the persisted ``session_environments`` config section.

The ONE source both the loader (``SessionEnvironmentsConfig.from_raw``) and the
dashboard write gate (``PATCH /api/config/kirocrew``) use, so what a save accepts
and what a load keeps cannot drift apart.

The section stores CONFIGURATION ONLY -- a Coder deployment URL, workspace
profiles, and Sprites and boxd profiles. Nothing here registers a provider,
contacts a provider, or provisions anything. It carries no credentials: a
Sprites profile names the vault secret that holds its token, never the token.

Two entry points per shape:

* ``validate_*`` raises :class:`SessionEnvironmentsError` naming the offending
  field -- the API gate turns that into a 400 and nothing is written.
* :func:`load_session_environments` is the lenient load-time twin: an invalid
  ``coder_url`` resolves to ``""`` and an invalid or duplicate profile is dropped
  (with a warning), so one bad entry in a hand-edited file never discards the
  valid ones beside it.

This module imports nothing from the config package, so ``sections.py`` can depend
on it without an import cycle.
"""

from __future__ import annotations

import logging
import re
from typing import Any, Callable
from urllib.parse import urlsplit

logger = logging.getLogger("kiro_crew.config.loader")

#: Most profiles one config may hold.
MAX_PROFILES = 20
#: Profile id grammar: ASCII alphanumeric first, then alphanumerics, ``_`` or ``-``.
PROFILE_ID_MAX_LEN = 64
PROFILE_NAME_MAX_LEN = 128
PROFILE_TEMPLATE_MAX_LEN = 255
PROFILE_WORKDIR_MAX_LEN = 1024
CODER_URL_MAX_LEN = 2048

PROFILE_KEYS: tuple[str, ...] = ("id", "name", "template", "workdir")
#: Optional profile keys, with the value an omitted one takes.
PROFILE_OPTIONAL_KEYS: dict[str, bool] = {"compute_survives_stop": True}
SECTION_KEYS: tuple[str, ...] = ("coder_url", "profiles")
#: Optional section keys. An older dashboard that omits one keeps the saved
#: value: the locked writer in ``dashboard/handlers/core.py`` preserves it.
SECTION_OPTIONAL_KEYS: tuple[str, ...] = (
    "sprites_profiles",
    "boxd_profiles",
    "archived_retention_days",
    "default_profile",
    "stop_on_shutdown",
)
#: Days an archived session's stopped workspace is kept before it is deleted.
#: A stopped workspace still holds its disk, so the ceiling is a week.
ARCHIVED_RETENTION_DAYS_MAX = 7
ARCHIVED_RETENTION_DAYS_DEFAULT = 7

_PROFILE_ID_RE = re.compile(r"[A-Za-z0-9][A-Za-z0-9_-]{0,%d}" % (PROFILE_ID_MAX_LEN - 1))
# ASCII control characters: never meaningful in a label, template name or path,
# and a NUL in a path is refused by every OS call that would later use it.
_CONTROL_RE = re.compile(r"[\x00-\x1f\x7f]")


class SessionEnvironmentsError(ValueError):
    """A ``session_environments`` value failed validation; the message names the field."""


def validate_coder_url(value: object) -> str:
    """Return the normalized Coder URL, or raise :class:`SessionEnvironmentsError`.

    ``""`` means "not configured". Otherwise an ``http``/``https`` URL with a
    hostname and no userinfo, query or fragment. Surrounding whitespace is
    stripped; the URL is not contacted.
    """
    if not isinstance(value, str):
        raise SessionEnvironmentsError("coder_url must be a string")
    url = value.strip()
    if not url:
        return ""
    if len(url) > CODER_URL_MAX_LEN:
        raise SessionEnvironmentsError(f"coder_url must be at most {CODER_URL_MAX_LEN} characters")
    if _CONTROL_RE.search(url) or " " in url:
        raise SessionEnvironmentsError("coder_url must not contain spaces or control characters")
    try:
        parts = urlsplit(url)
        # ``.port`` raises ValueError on a non-numeric or out-of-range port.
        _ = parts.port
    except ValueError:
        raise SessionEnvironmentsError("coder_url must be a valid URL") from None
    if parts.scheme.lower() not in ("http", "https"):
        raise SessionEnvironmentsError("coder_url must be an http or https URL")
    if not parts.hostname:
        raise SessionEnvironmentsError("coder_url must include a hostname")
    if parts.username is not None or parts.password is not None or "@" in parts.netloc:
        raise SessionEnvironmentsError("coder_url must not include a username or password")
    # Test the raw characters, not only the parsed parts: ``https://h?`` parses
    # to an EMPTY query, and a bare ``?``/``#`` is still not a plain base URL.
    if "?" in url or parts.query:
        raise SessionEnvironmentsError("coder_url must not include a query string")
    if "#" in url or parts.fragment:
        raise SessionEnvironmentsError("coder_url must not include a fragment")
    return url


def _bounded_text(value: object, field: str, max_len: int) -> str:
    if not isinstance(value, str):
        raise SessionEnvironmentsError(f"{field} must be a string")
    text = value.strip()
    if not text:
        raise SessionEnvironmentsError(f"{field} must not be empty")
    if len(text) > max_len:
        raise SessionEnvironmentsError(f"{field} must be at most {max_len} characters")
    if _CONTROL_RE.search(text):
        raise SessionEnvironmentsError(f"{field} must not contain control characters")
    return text


def validate_archived_retention_days(value: object) -> int:
    """Days to keep an archived session's workspace: an int in 0..7 (0 deletes it now)."""
    if isinstance(value, bool) or not isinstance(value, int):
        raise SessionEnvironmentsError("archived_retention_days must be an integer")
    if not 0 <= value <= ARCHIVED_RETENTION_DAYS_MAX:
        raise SessionEnvironmentsError(
            f"archived_retention_days must be between 0 and {ARCHIVED_RETENTION_DAYS_MAX}"
        )
    return value


def load_archived_retention_days(raw: object) -> int:
    """Lenient load: the saved value, or the default when absent or invalid."""
    if not isinstance(raw, dict) or "archived_retention_days" not in raw:
        return ARCHIVED_RETENTION_DAYS_DEFAULT
    try:
        return validate_archived_retention_days(raw["archived_retention_days"])
    except SessionEnvironmentsError as exc:
        logger.warning("session_environments: %s; using %d", exc, ARCHIVED_RETENTION_DAYS_DEFAULT)
        return ARCHIVED_RETENTION_DAYS_DEFAULT


def validate_stop_on_shutdown(value: object) -> bool:
    """Whether the gateway stops its remote environments when it shuts down."""
    if not isinstance(value, bool):
        raise SessionEnvironmentsError("stop_on_shutdown must be true or false")
    return value


def load_stop_on_shutdown(raw: object) -> bool:
    """Lenient load: the saved choice, or True (stop them) when absent or invalid."""
    if not isinstance(raw, dict) or "stop_on_shutdown" not in raw:
        return True
    try:
        return validate_stop_on_shutdown(raw["stop_on_shutdown"])
    except SessionEnvironmentsError as exc:
        logger.warning("session_environments: %s; stopping them on shutdown", exc)
        return True


def validate_default_profile(value: object) -> str:
    """The profile id new dashboard sessions start on; "" is this machine.

    Shape only here: that the id names a saved profile is checked against the
    whole section (``validate_session_environments``) and again at bind time,
    where a dangling id falls back to this machine.
    """
    if value is None or value == "":
        return ""
    if not isinstance(value, str) or not _PROFILE_ID_RE.fullmatch(value):
        raise SessionEnvironmentsError("default_profile must be a profile id or empty")
    return value


def load_default_profile(raw: object) -> str:
    """Lenient load: the saved id, or "" (this machine) when absent or invalid."""
    if not isinstance(raw, dict) or "default_profile" not in raw:
        return ""
    try:
        return validate_default_profile(raw["default_profile"])
    except SessionEnvironmentsError as exc:
        logger.warning("session_environments: %s; new sessions run on this machine", exc)
        return ""


def validate_profile(value: object, index: int = 0, *, where: str = "") -> dict[str, Any]:
    """Return one normalized profile record, or raise :class:`SessionEnvironmentsError`.

    Strict shape: the keys ``id``, ``name``, ``template`` and ``workdir``, plus
    the optional ``compute_survives_stop`` (a bool, default True: the template's
    storage survives a workspace stop). An unknown key is refused rather than
    silently dropped, so a
    caller that thinks it stored something (a credential, say) learns it did
    not. ``where`` names the entry in messages, so the person is pointed at the
    profile that is actually wrong.
    """
    where = where or f"profiles[{index}]"
    if not isinstance(value, dict):
        raise SessionEnvironmentsError(f"{where} must be an object")
    unknown = sorted(
        str(k) for k in value if k not in PROFILE_KEYS and k not in PROFILE_OPTIONAL_KEYS
    )
    if unknown:
        raise SessionEnvironmentsError(f"{where} has unknown key(s): {unknown}")
    missing = [k for k in PROFILE_KEYS if k not in value]
    if missing:
        raise SessionEnvironmentsError(f"{where} is missing key(s): {missing}")

    profile_id = value["id"]
    if not isinstance(profile_id, str) or not _PROFILE_ID_RE.fullmatch(profile_id):
        raise SessionEnvironmentsError(
            f"{where}.id must start with an ASCII letter or digit and contain only "
            f"letters, digits, '_' or '-' (at most {PROFILE_ID_MAX_LEN} characters)"
        )
    name = _bounded_text(value["name"], f"{where}.name", PROFILE_NAME_MAX_LEN)
    template = _bounded_text(value["template"], f"{where}.template", PROFILE_TEMPLATE_MAX_LEN)
    workdir = _bounded_text(value["workdir"], f"{where}.workdir", PROFILE_WORKDIR_MAX_LEN)
    if not workdir.startswith("/"):
        raise SessionEnvironmentsError(f"{where}.workdir must be an absolute POSIX path")
    survives = value.get("compute_survives_stop", PROFILE_OPTIONAL_KEYS["compute_survives_stop"])
    if not isinstance(survives, bool):
        raise SessionEnvironmentsError(f"{where}.compute_survives_stop must be true or false")
    return {
        "id": profile_id,
        "name": name,
        "template": template,
        "workdir": workdir,
        "compute_survives_stop": survives,
    }


def validate_profiles(value: object) -> list[dict[str, Any]]:
    """Return the normalized profile list, or raise :class:`SessionEnvironmentsError`."""
    if not isinstance(value, list):
        raise SessionEnvironmentsError("profiles must be a list")
    if len(value) > MAX_PROFILES:
        raise SessionEnvironmentsError(f"profiles must hold at most {MAX_PROFILES} entries")
    out: list[dict[str, Any]] = []
    seen: set[str] = set()
    for index, entry in enumerate(value):
        profile = validate_profile(entry, index)
        if profile["id"] in seen:
            raise SessionEnvironmentsError(f"profiles[{index}].id {profile['id']!r} is a duplicate")
        seen.add(profile["id"])
        out.append(profile)
    return out


def validate_session_environments(value: object) -> dict[str, Any]:
    """Return the normalized whole section, or raise :class:`SessionEnvironmentsError`.

    Written atomically. Both Coder keys are required; the optional keys may be
    omitted by older clients, with omission preserved by the locked writer.
    Other keys are refused. A profile id names one profile across every
    provider's list, because a session's choice resolves by id.
    """
    if not isinstance(value, dict):
        raise SessionEnvironmentsError("session_environments must be an object")
    unknown = sorted(str(k) for k in value if k not in (*SECTION_KEYS, *SECTION_OPTIONAL_KEYS))
    if unknown:
        raise SessionEnvironmentsError(f"session_environments has unknown key(s): {unknown}")
    missing = [k for k in SECTION_KEYS if k not in value]
    if missing:
        raise SessionEnvironmentsError(f"session_environments is missing key(s): {missing}")
    result: dict[str, Any] = {
        "coder_url": validate_coder_url(value["coder_url"]),
        "profiles": validate_profiles(value["profiles"]),
    }
    if "sprites_profiles" in value:
        result["sprites_profiles"] = validate_sprites_profiles(value["sprites_profiles"])
    if "boxd_profiles" in value:
        result["boxd_profiles"] = validate_boxd_profiles(value["boxd_profiles"])
    saved_ids = validate_profile_ids(result)
    if "archived_retention_days" in value:
        result["archived_retention_days"] = validate_archived_retention_days(
            value["archived_retention_days"]
        )
    if "default_profile" in value:
        default = validate_default_profile(value["default_profile"])
        # Only an id this save keeps: a removed profile cannot stay the default.
        if default and default not in saved_ids:
            raise SessionEnvironmentsError(
                f"default_profile {default!r} is not one of the saved profiles"
            )
        result["default_profile"] = default
    if "stop_on_shutdown" in value:
        result["stop_on_shutdown"] = validate_stop_on_shutdown(value["stop_on_shutdown"])
    return result


def validate_profile_ids(section: dict[str, Any]) -> set[str]:
    """Check cross-provider identity on the final section under the write lock.

    Ignore malformed entries at this boundary; each list's own normalizer owns
    their shape. A leaf write must not erase unrelated or unmodelled settings.
    """
    providers: dict[str, str] = {}
    for key, provider in PROVIDER_PROFILE_LISTS:
        items = section.get(key, [])
        for profile in items if isinstance(items, list) else []:
            if not isinstance(profile, dict) or not isinstance(profile.get("id"), str):
                continue
            other = providers.setdefault(profile["id"], provider)
            if other != provider:
                raise SessionEnvironmentsError(
                    f"profile id {profile['id']!r} is used by both a {other} and a "
                    f"{provider} profile"
                )
    return set(providers)


def load_session_environments(raw: object) -> tuple[str, list[dict[str, Any]]]:
    """Lenient load-time normalization: ``(coder_url, profiles)``, never raises.

    A non-object section, an invalid URL, and each invalid, duplicate or
    over-cap profile fall back individually (``""`` / dropped) with a warning.
    """
    if not isinstance(raw, dict):
        if raw is not None:
            logger.warning("session_environments must be an object; using defaults")
        return "", []

    coder_url = ""
    if "coder_url" in raw:
        try:
            coder_url = validate_coder_url(raw["coder_url"])
        except SessionEnvironmentsError as exc:
            logger.warning("session_environments: %s; ignoring it", exc)

    profiles: list[dict[str, Any]] = []
    raw_profiles = raw.get("profiles", [])
    if not isinstance(raw_profiles, list):
        logger.warning("session_environments: profiles must be a list; ignoring it")
        raw_profiles = []
    seen: set[str] = set()
    for index, entry in enumerate(raw_profiles):
        if len(profiles) >= MAX_PROFILES:
            logger.warning(
                "session_environments: more than %d profiles; ignoring the rest", MAX_PROFILES
            )
            break
        try:
            profile = validate_profile(entry, index)
        except SessionEnvironmentsError as exc:
            logger.warning("session_environments: %s; dropping that profile", exc)
            continue
        if profile["id"] in seen:
            logger.warning(
                "session_environments: profiles[%d].id %r is a duplicate; dropping it",
                index,
                profile["id"],
            )
            continue
        seen.add(profile["id"])
        profiles.append(profile)
    return coder_url, profiles


#: Each provider's profile list in the section, with the provider's display name.
#: Profile ids are unique across all of them (``validate_session_environments``).
PROVIDER_PROFILE_LISTS: tuple[tuple[str, str], ...] = (
    ("profiles", "Coder"),
    ("sprites_profiles", "Sprites"),
    ("boxd_profiles", "boxd"),
)

SPRITES_PROFILE_KEYS: tuple[str, ...] = ("id", "name", "token_secret", "workdir")
#: Default vault secret name for a Sprites API token.
SPRITES_TOKEN_SECRET_DEFAULT = "SPRITES_TOKEN"
#: A vault secret name: what Settings > Secrets accepts as a name.
SECRET_NAME_RE = re.compile(r"[A-Za-z_][A-Za-z0-9_]{0,63}")

BOXD_PROFILE_KEYS: tuple[str, ...] = ("id", "name", "snapshot", "vcpu", "workdir")
#: 0 is the org's default size; the others are boxd's own vCPU sizes.
BOXD_VCPU_CHOICES: tuple[int, ...] = (0, 1, 2, 4, 8)
_SNAPSHOT_RE = re.compile(r"[A-Za-z0-9][A-Za-z0-9._-]{0,127}")


def _exact_keys(value: object, where: str, keys: tuple[str, ...]) -> dict[str, Any]:
    if not isinstance(value, dict):
        raise SessionEnvironmentsError(f"{where} must be an object")
    unknown = sorted(str(k) for k in value if k not in keys)
    if unknown:
        raise SessionEnvironmentsError(f"{where} has unknown key(s): {unknown}")
    missing = [k for k in keys if k not in value]
    if missing:
        raise SessionEnvironmentsError(f"{where} is missing key(s): {missing}")
    return value


def _common_fields(value: dict[str, Any], index: int, where: str, template: str) -> dict[str, Any]:
    """``id``, ``name`` and ``workdir``, checked by the Coder profile's own rules."""
    common = validate_profile(
        {
            "id": value["id"],
            "name": value["name"],
            "template": template,
            "workdir": value["workdir"],
        },
        index,
        where=where,
    )
    return {key: common[key] for key in ("id", "name", "workdir")}


def validate_sprites_profile(value: object, index: int = 0) -> dict[str, Any]:
    """Validate a saved Fly.io Sprites profile. No API call, no token read.

    The token itself is never part of the profile: ``token_secret`` names the
    gateway vault entry (Settings > Secrets) that holds it.
    """
    where = f"sprites_profiles[{index}]"
    raw = _exact_keys(value, where, SPRITES_PROFILE_KEYS)
    result = _common_fields(raw, index, where, "sprites")
    token_secret = raw["token_secret"]
    if not isinstance(token_secret, str) or not SECRET_NAME_RE.fullmatch(token_secret):
        raise SessionEnvironmentsError(
            f"{where}.token_secret must be a secret name (letters, digits and '_')"
        )
    result["token_secret"] = token_secret
    return result


def validate_boxd_profile(value: object, index: int = 0) -> dict[str, Any]:
    """Validate a saved boxd profile. No CLI call, no credential.

    The gateway machine's own ``boxd auth login`` authenticates, so a profile
    names only what to create: an optional snapshot to start from, a size, and
    the working directory.
    """
    where = f"boxd_profiles[{index}]"
    raw = _exact_keys(value, where, BOXD_PROFILE_KEYS)
    result = _common_fields(raw, index, where, "boxd")
    snapshot = raw["snapshot"]
    if not isinstance(snapshot, str) or (snapshot and not _SNAPSHOT_RE.fullmatch(snapshot)):
        raise SessionEnvironmentsError(f"{where}.snapshot must be a snapshot name or id, or empty")
    vcpu = raw["vcpu"]
    if isinstance(vcpu, bool) or vcpu not in BOXD_VCPU_CHOICES:
        raise SessionEnvironmentsError(f"{where}.vcpu must be one of {list(BOXD_VCPU_CHOICES)}")
    if snapshot and vcpu:
        # boxd keeps a snapshot's own size; naming one there is an error.
        raise SessionEnvironmentsError(f"{where}: a snapshot keeps its own size; set vcpu to 0")
    result["snapshot"] = snapshot
    result["vcpu"] = vcpu
    return result


def _validate_list(
    value: object, key: str, validate: Callable[[object, int], dict[str, Any]]
) -> list[dict[str, Any]]:
    if not isinstance(value, list):
        raise SessionEnvironmentsError(f"{key} must be a list")
    if len(value) > MAX_PROFILES:
        raise SessionEnvironmentsError(f"{key} must hold at most {MAX_PROFILES} entries")
    out: list[dict[str, Any]] = []
    seen: set[str] = set()
    for index, entry in enumerate(value):
        profile = validate(entry, index)
        if profile["id"] in seen:
            raise SessionEnvironmentsError(f"{key}[{index}].id {profile['id']!r} is a duplicate")
        seen.add(profile["id"])
        out.append(profile)
    return out


def _load_list(
    raw: object, key: str, validate: Callable[[object, int], dict[str, Any]]
) -> list[dict[str, Any]]:
    """Lenient twin of :func:`_validate_list`: keep each valid entry, drop the rest."""
    if not isinstance(raw, dict):
        return []
    items = raw.get(key, [])
    if not isinstance(items, list):
        logger.warning("session_environments: %s must be a list; ignoring it", key)
        return []
    out: list[dict[str, Any]] = []
    seen: set[str] = set()
    for index, item in enumerate(items):
        if len(out) >= MAX_PROFILES:
            logger.warning(
                "session_environments: more than %d %s; ignoring the rest", MAX_PROFILES, key
            )
            break
        try:
            profile = validate(item, index)
        except SessionEnvironmentsError as exc:
            # The message names the field, never its value.
            logger.warning("session_environments: %s; dropping that profile", exc)
            continue
        if profile["id"] in seen:
            logger.warning("session_environments: %s[%d] is a duplicate; dropping it", key, index)
            continue
        seen.add(profile["id"])
        out.append(profile)
    return out


def validate_sprites_profiles(value: object) -> list[dict[str, Any]]:
    """Strict atomic list validation for the Sprites profile editor."""
    return _validate_list(value, "sprites_profiles", validate_sprites_profile)


def load_sprites_profiles(raw: object) -> list[dict[str, Any]]:
    return _load_list(raw, "sprites_profiles", validate_sprites_profile)


def validate_boxd_profiles(value: object) -> list[dict[str, Any]]:
    """Strict atomic list validation for the boxd profile editor."""
    return _validate_list(value, "boxd_profiles", validate_boxd_profile)


def load_boxd_profiles(raw: object) -> list[dict[str, Any]]:
    return _load_list(raw, "boxd_profiles", validate_boxd_profile)
